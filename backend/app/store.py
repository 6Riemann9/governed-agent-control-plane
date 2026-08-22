"""Tenant-scoped run stores used by the minimal data plane."""

from __future__ import annotations

from pathlib import Path
from threading import Lock
from typing import Any
from uuid import uuid4

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}


def _normalize_step_specs(step_specs: list[str | dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = []
    for spec in step_specs:
        if isinstance(spec, str):
            normalized.append(
                {"node_id": spec, "depends_on": [], "max_retries": 0}
            )
            continue
        normalized.append(
            {
                "node_id": spec["node_id"],
                "depends_on": list(spec.get("depends_on", [])),
                "max_retries": int(spec.get("max_retries", 0)),
            }
        )
    return normalized


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
        step_specs: list[str | dict[str, Any]],
    ) -> tuple[dict[str, Any], bool]:
        steps = _normalize_step_specs(step_specs)
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
                "summary": "queued for execution",
                "created_at": None,
                "finished_at": None,
                "cancelled": False,
                "steps": [
                    {
                        "node_id": step["node_id"],
                        "depends_on": step["depends_on"],
                        "max_retries": step["max_retries"],
                        "attempts": 0,
                        "status": "queued",
                        "output": "",
                        "error": "",
                        "latency_ms": 0,
                        "input_tokens": 0,
                        "output_tokens": 0,
                    }
                    for step in steps
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
            record["summary"] = "execution running"
            return True

    def start_step(self, tenant_id: str, run_id: str, position: int) -> bool:
        with self._lock:
            record = self._runs.get(run_id)
            if not record or record["tenant_id"] != tenant_id or record["cancelled"]:
                return False
            step = record["steps"][position]
            if step["status"] not in {"queued", "failed"}:
                return False
            step["status"] = "running"
            step["attempts"] += 1
            step["error"] = ""
            return True

    def succeed_step(
        self,
        tenant_id: str,
        run_id: str,
        position: int,
        output: str | None = None,
        latency_ms: int = 20,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> bool:
        with self._lock:
            record = self._runs.get(run_id)
            if not record or record["tenant_id"] != tenant_id or record["cancelled"]:
                return False
            step = record["steps"][position]
            step["status"] = "succeeded"
            step["output"] = output or f"mock execution completed step {step['node_id']}"
            step["latency_ms"] = latency_ms
            step["input_tokens"] = input_tokens
            step["output_tokens"] = output_tokens
            return True

    def fail_step(self, tenant_id: str, run_id: str, position: int, error: str) -> bool:
        with self._lock:
            record = self._runs.get(run_id)
            if not record or record["tenant_id"] != tenant_id or record["cancelled"]:
                return False
            step = record["steps"][position]
            step["status"] = "failed"
            step["error"] = error
            return True

    def succeed(self, tenant_id: str, run_id: str) -> bool:
        with self._lock:
            record = self._runs.get(run_id)
            if not record or record["tenant_id"] != tenant_id or record["cancelled"]:
                return False
            record["status"] = "succeeded"
            record["summary"] = "execution completed"
            return True

    def fail(self, tenant_id: str, run_id: str, error: str) -> bool:
        with self._lock:
            record = self._runs.get(run_id)
            if not record or record["tenant_id"] != tenant_id or record["cancelled"]:
                return False
            record["status"] = "failed"
            record["summary"] = error
            for step in record["steps"]:
                if step["status"] == "queued":
                    step["status"] = "cancelled"
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
        step_specs: list[str | dict[str, Any]],
    ) -> tuple[dict[str, Any], bool]:
        steps = _normalize_step_specs(step_specs)
        with self.pool.connection() as connection:
            self._set_tenant(connection, tenant_id)
            run_id = str(uuid4())
            inserted = connection.execute(
                """
                INSERT INTO agent_runs (id, tenant_id, project_id, idempotency_key, task, status, summary)
                VALUES (%s, %s, %s, %s, %s, 'queued', 'queued for execution')
                ON CONFLICT (tenant_id, idempotency_key) DO NOTHING
                RETURNING id
                """,
                (run_id, tenant_id, project_id, idempotency_key, task),
            ).fetchone()
            if inserted:
                with connection.cursor() as cursor:
                    cursor.executemany(
                        """
                        INSERT INTO agent_run_steps
                            (run_id, tenant_id, position, node_id, depends_on, max_retries, status)
                        VALUES (%s, %s, %s, %s, %s, %s, 'queued')
                        """,
                        [
                            (
                                run_id,
                                tenant_id,
                                position,
                                step["node_id"],
                                step["depends_on"],
                                step["max_retries"],
                            )
                            for position, step in enumerate(steps)
                        ],
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
        return self._update_run(tenant_id, run_id, "running", "execution running")

    def start_step(self, tenant_id: str, run_id: str, position: int) -> bool:
        with self.pool.connection() as connection:
            self._set_tenant(connection, tenant_id)
            updated = connection.execute(
                """
                UPDATE agent_run_steps AS step
                SET status = 'running', attempts = attempts + 1, error = ''
                WHERE step.run_id = %s AND step.tenant_id = %s AND step.position = %s
                  AND step.status IN ('queued', 'failed')
                  AND EXISTS (
                    SELECT 1 FROM agent_runs run
                    WHERE run.id = step.run_id AND run.tenant_id = step.tenant_id
                      AND run.cancelled = false
                  )
                RETURNING position
                """,
                (run_id, tenant_id, position),
            ).fetchone()
            return updated is not None

    def succeed_step(
        self,
        tenant_id: str,
        run_id: str,
        position: int,
        output: str | None = None,
        latency_ms: int = 20,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> bool:
        with self.pool.connection() as connection:
            self._set_tenant(connection, tenant_id)
            updated = connection.execute(
                """
                UPDATE agent_run_steps AS step
                SET status = 'succeeded',
                    output = %s,
                    latency_ms = %s,
                    input_tokens = %s,
                    output_tokens = %s
                WHERE step.run_id = %s AND step.tenant_id = %s AND step.position = %s
                  AND EXISTS (
                    SELECT 1 FROM agent_runs run
                    WHERE run.id = step.run_id AND run.tenant_id = step.tenant_id AND run.cancelled = false
                  )
                RETURNING position
                """,
                (output or self._mock_output(connection, tenant_id, run_id, position), latency_ms, input_tokens, output_tokens, run_id, tenant_id, position),
            ).fetchone()
            return updated is not None

    @staticmethod
    def _mock_output(connection: Any, tenant_id: str, run_id: str, position: int) -> str:
        node = connection.execute(
            "SELECT node_id FROM agent_run_steps WHERE run_id = %s AND tenant_id = %s AND position = %s",
            (run_id, tenant_id, position),
        ).fetchone()
        return f"mock execution completed step {node['node_id']}"

    def fail_step(self, tenant_id: str, run_id: str, position: int, error: str) -> bool:
        with self.pool.connection() as connection:
            self._set_tenant(connection, tenant_id)
            updated = connection.execute(
                """
                UPDATE agent_run_steps SET status = 'failed', error = %s
                WHERE run_id = %s AND tenant_id = %s AND position = %s
                  AND EXISTS (
                    SELECT 1 FROM agent_runs run
                    WHERE run.id = agent_run_steps.run_id AND run.tenant_id = agent_run_steps.tenant_id
                      AND run.cancelled = false
                  )
                RETURNING position
                """,
                (error, run_id, tenant_id, position),
            ).fetchone()
            return updated is not None

    def succeed(self, tenant_id: str, run_id: str) -> bool:
        with self.pool.connection() as connection:
            self._set_tenant(connection, tenant_id)
            updated = connection.execute(
                """
                UPDATE agent_runs SET status = 'succeeded', summary = 'execution completed', finished_at = now()
                WHERE id = %s AND tenant_id = %s AND cancelled = false AND status = 'running'
                RETURNING id
                """,
                (run_id, tenant_id),
            ).fetchone()
            return updated is not None

    def fail(self, tenant_id: str, run_id: str, error: str) -> bool:
        with self.pool.connection() as connection:
            self._set_tenant(connection, tenant_id)
            updated = connection.execute(
                """
                UPDATE agent_runs SET status = 'failed', summary = %s, finished_at = now()
                WHERE id = %s AND tenant_id = %s AND cancelled = false AND status = 'running'
                RETURNING id
                """,
                (error, run_id, tenant_id),
            ).fetchone()
            if updated:
                connection.execute(
                    """
                    UPDATE agent_run_steps SET status = 'cancelled'
                    WHERE run_id = %s AND tenant_id = %s AND status = 'queued'
                    """,
                    (run_id, tenant_id),
                )
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
            SELECT node_id, depends_on, max_retries, attempts, status, output, error,
                   latency_ms, input_tokens, output_tokens
            FROM agent_run_steps WHERE run_id = %s AND tenant_id = %s ORDER BY position
            """,
            (run_id, tenant_id),
        ).fetchall()
        return {**dict(run), "steps": [dict(step) for step in steps]}

    def close(self) -> None:
        self.pool.close()
