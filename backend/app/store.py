"""Tenant-scoped run stores used by the minimal data plane."""

from __future__ import annotations

from pathlib import Path
from threading import Lock
from typing import Any
from uuid import uuid4

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}


def _snapshot(record: dict[str, Any]) -> dict[str, Any]:
    return {
        key: [dict(step) for step in value] if key == "steps" else value
        for key, value in record.items()
        if key not in {"tenant_id", "project_id", "task", "cancelled"}
    }


class InMemoryRunStore:
    """Fast deterministic store used only by local unit tests."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._runs: dict[str, dict[str, Any]] = {}
        self._idempotency: dict[tuple[str, str], str] = {}

    def clear(self) -> None:
        with self._lock:
            self._runs.clear()
            self._idempotency.clear()

    def submit(
        self,
        tenant_id: str,
        task: str,
        project_id: str | None,
        idempotency_key: str,
        node_names: list[str],
    ) -> tuple[dict[str, Any], bool]:
        key = (tenant_id, idempotency_key)
        with self._lock:
            if existing_id := self._idempotency.get(key):
                return _snapshot(self._runs[existing_id]), False
            run_id = str(uuid4())
            record = {
                "id": run_id,
                "tenant_id": tenant_id,
                "project_id": project_id,
                "task": task,
                "status": "queued",
                "summary": "queued for mock execution",
                "created_at": None,
                "finished_at": None,
                "cancelled": False,
                "steps": [
                    {
                        "node_id": node_name,
                        "status": "queued",
                        "output": "",
                        "error": "",
                        "latency_ms": 0,
                    }
                    for node_name in node_names
                ],
            }
            self._runs[run_id] = record
            self._idempotency[key] = run_id
            return _snapshot(record), True

    def get(self, tenant_id: str, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._runs.get(run_id)
            return _snapshot(record) if record and record["tenant_id"] == tenant_id else None

    def cancel(self, tenant_id: str, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._runs.get(run_id)
            if record is None or record["tenant_id"] != tenant_id:
                return None
            if record["status"] not in TERMINAL_STATUSES:
                record["cancelled"] = True
                record["status"] = "cancelled"
                record["summary"] = "cancelled by API request"
                for step in record["steps"]:
                    if step["status"] in {"queued", "running"}:
                        step["status"] = "cancelled"
            return _snapshot(record)

    def start(self, tenant_id: str, run_id: str) -> bool:
        with self._lock:
            record = self._runs.get(run_id)
            if not record or record["tenant_id"] != tenant_id or record["cancelled"]:
                return False
            record["status"] = "running"
            record["summary"] = "mock execution running"
            return True

    def start_step(self, tenant_id: str, run_id: str, position: int) -> bool:
        with self._lock:
            record = self._runs.get(run_id)
            if not record or record["tenant_id"] != tenant_id or record["cancelled"]:
                return False
            record["steps"][position]["status"] = "running"
            return True

    def succeed_step(self, tenant_id: str, run_id: str, position: int) -> bool:
        with self._lock:
            record = self._runs.get(run_id)
            if not record or record["tenant_id"] != tenant_id or record["cancelled"]:
                return False
            step = record["steps"][position]
            step["status"] = "succeeded"
            step["output"] = f"mock execution completed step {step['node_id']}"
            step["latency_ms"] = 20
            return True

    def succeed(self, tenant_id: str, run_id: str) -> bool:
        with self._lock:
            record = self._runs.get(run_id)
            if not record or record["tenant_id"] != tenant_id or record["cancelled"]:
                return False
            record["status"] = "succeeded"
            record["summary"] = "mock execution completed"
            return True

    def close(self) -> None:
        return None


class PostgresRunStore:
    """Durable store with database-enforced tenant scoping."""

    def __init__(self, dsn: str, migrations_dir: Path) -> None:
        self.pool = ConnectionPool(
            conninfo=dsn,
            min_size=1,
            max_size=5,
            open=True,
            kwargs={"row_factory": dict_row},
        )
        self.pool.wait(timeout=20)
        self._migrate(migrations_dir)

    def _migrate(self, migrations_dir: Path) -> None:
        with self.pool.connection() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations (version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
            )
            connection.execute("SELECT pg_advisory_xact_lock(718246001)")
            for migration in sorted(migrations_dir.glob("*.sql")):
                version = migration.name
                already_applied = connection.execute(
                    "SELECT 1 FROM schema_migrations WHERE version = %s", (version,)
                ).fetchone()
                if already_applied:
                    continue
                connection.execute(migration.read_text(encoding="utf-8"))
                connection.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (version,))

    @staticmethod
    def _set_tenant(connection: Any, tenant_id: str) -> None:
        connection.execute("SELECT set_config('app.current_tenant_id', %s, true)", (tenant_id,))

    def submit(
        self,
        tenant_id: str,
        task: str,
        project_id: str | None,
        idempotency_key: str,
        node_names: list[str],
    ) -> tuple[dict[str, Any], bool]:
        with self.pool.connection() as connection:
            self._set_tenant(connection, tenant_id)
            run_id = str(uuid4())
            inserted = connection.execute(
                """
                INSERT INTO agent_runs (id, tenant_id, project_id, idempotency_key, task, status, summary)
                VALUES (%s, %s, %s, %s, %s, 'queued', 'queued for mock execution')
                ON CONFLICT (tenant_id, idempotency_key) DO NOTHING
                RETURNING id
                """,
                (run_id, tenant_id, project_id, idempotency_key, task),
            ).fetchone()
            if inserted:
                with connection.cursor() as cursor:
                    cursor.executemany(
                        """
                        INSERT INTO agent_run_steps (run_id, tenant_id, position, node_id, status)
                        VALUES (%s, %s, %s, %s, 'queued')
                        """,
                        [(run_id, tenant_id, position, name) for position, name in enumerate(node_names)],
                    )
                return self._load(connection, tenant_id, run_id), True
            existing = connection.execute(
                "SELECT id FROM agent_runs WHERE tenant_id = %s AND idempotency_key = %s",
                (tenant_id, idempotency_key),
            ).fetchone()
            return self._load(connection, tenant_id, str(existing["id"])), False

    def get(self, tenant_id: str, run_id: str) -> dict[str, Any] | None:
        with self.pool.connection() as connection:
            self._set_tenant(connection, tenant_id)
            return self._load(connection, tenant_id, run_id)

    def cancel(self, tenant_id: str, run_id: str) -> dict[str, Any] | None:
        with self.pool.connection() as connection:
            self._set_tenant(connection, tenant_id)
            cancelled = connection.execute(
                """
                UPDATE agent_runs
                SET cancelled = true, status = 'cancelled', summary = 'cancelled by API request', finished_at = now()
                WHERE id = %s AND tenant_id = %s AND status NOT IN ('succeeded', 'failed', 'cancelled')
                RETURNING id
                """,
                (run_id, tenant_id),
            ).fetchone()
            if cancelled:
                connection.execute(
                    """
                    UPDATE agent_run_steps SET status = 'cancelled'
                    WHERE run_id = %s AND tenant_id = %s AND status IN ('queued', 'running')
                    """,
                    (run_id, tenant_id),
                )
            return self._load(connection, tenant_id, run_id)

    def start(self, tenant_id: str, run_id: str) -> bool:
        return self._update_run(tenant_id, run_id, "running", "mock execution running")

    def start_step(self, tenant_id: str, run_id: str, position: int) -> bool:
        return self._update_step(tenant_id, run_id, position, "running")

    def succeed_step(self, tenant_id: str, run_id: str, position: int) -> bool:
        with self.pool.connection() as connection:
            self._set_tenant(connection, tenant_id)
            updated = connection.execute(
                """
                UPDATE agent_run_steps AS step
                SET status = 'succeeded', output = 'mock execution completed step ' || step.node_id, latency_ms = 20
                WHERE step.run_id = %s AND step.tenant_id = %s AND step.position = %s
                  AND EXISTS (
                    SELECT 1 FROM agent_runs run
                    WHERE run.id = step.run_id AND run.tenant_id = step.tenant_id AND run.cancelled = false
                  )
                RETURNING position
                """,
                (run_id, tenant_id, position),
            ).fetchone()
            return updated is not None

    def succeed(self, tenant_id: str, run_id: str) -> bool:
        with self.pool.connection() as connection:
            self._set_tenant(connection, tenant_id)
            updated = connection.execute(
                """
                UPDATE agent_runs SET status = 'succeeded', summary = 'mock execution completed', finished_at = now()
                WHERE id = %s AND tenant_id = %s AND cancelled = false AND status = 'running'
                RETURNING id
                """,
                (run_id, tenant_id),
            ).fetchone()
            return updated is not None

    def _update_run(self, tenant_id: str, run_id: str, status: str, summary: str) -> bool:
        with self.pool.connection() as connection:
            self._set_tenant(connection, tenant_id)
            updated = connection.execute(
                """
                UPDATE agent_runs SET status = %s, summary = %s
                WHERE id = %s AND tenant_id = %s AND cancelled = false AND status = 'queued'
                RETURNING id
                """,
                (status, summary, run_id, tenant_id),
            ).fetchone()
            return updated is not None

    def _update_step(self, tenant_id: str, run_id: str, position: int, status: str) -> bool:
        with self.pool.connection() as connection:
            self._set_tenant(connection, tenant_id)
            updated = connection.execute(
                """
                UPDATE agent_run_steps AS step SET status = %s
                WHERE step.run_id = %s AND step.tenant_id = %s AND step.position = %s
                  AND EXISTS (
                    SELECT 1 FROM agent_runs run
                    WHERE run.id = step.run_id AND run.tenant_id = step.tenant_id AND run.cancelled = false
                  )
                RETURNING position
                """,
                (status, run_id, tenant_id, position),
            ).fetchone()
            return updated is not None

    @staticmethod
    def _load(connection: Any, tenant_id: str, run_id: str) -> dict[str, Any] | None:
        run = connection.execute(
            """
            SELECT id, status, summary, created_at, finished_at
            FROM agent_runs WHERE id = %s AND tenant_id = %s
            """,
            (run_id, tenant_id),
        ).fetchone()
        if run is None:
            return None
        steps = connection.execute(
            """
            SELECT node_id, status, output, error, latency_ms
            FROM agent_run_steps WHERE run_id = %s AND tenant_id = %s ORDER BY position
            """,
            (run_id, tenant_id),
        ).fetchall()
        return {**dict(run), "steps": [dict(step) for step in steps]}

    def close(self) -> None:
        self.pool.close()
