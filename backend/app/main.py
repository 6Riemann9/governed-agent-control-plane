"""A deliberately small AgentRun API for the first Kubernetes MVP.

It implements the HTTP contract consumed by the Operator. Runs live in memory
and use a deterministic mock executor, so this is suitable for a demo but not
for a durable production deployment.
"""

import time
from datetime import UTC, datetime
from threading import Lock, Thread
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field


def now() -> str:
    return datetime.now(UTC).isoformat()


class RunNode(BaseModel):
    name: str = Field(min_length=1, max_length=63)
    prompt: str = ""
    role: str | None = None
    model: str | None = None
    dependsOn: list[str] = Field(default_factory=list)
    maxRetries: int | None = Field(default=None, ge=0, le=5)


class SubmitRun(BaseModel):
    task: str = Field(min_length=1, max_length=16_000)
    nodes: list[RunNode] = Field(default_factory=list, max_length=32)
    project_id: str | None = Field(default=None, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=255)


class RunStore:
    def __init__(self) -> None:
        self._lock = Lock()
        self._runs: dict[str, dict[str, Any]] = {}
        self._idempotency: dict[tuple[str, str], str] = {}

    def clear(self) -> None:
        with self._lock:
            self._runs.clear()
            self._idempotency.clear()

    def submit(self, tenant_id: str, request: SubmitRun) -> tuple[dict[str, Any], bool]:
        key = (tenant_id, request.idempotency_key)
        with self._lock:
            if existing_id := self._idempotency.get(key):
                return self._snapshot(self._runs[existing_id]), False

            run_id = str(uuid4())
            node_names = [node.name for node in request.nodes] or ["run"]
            record = {
                "id": run_id,
                "tenant_id": tenant_id,
                "project_id": request.project_id,
                "task": request.task,
                "status": "queued",
                "summary": "queued for mock execution",
                "created_at": now(),
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
            return self._snapshot(record), True

    def get(self, tenant_id: str, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._runs.get(run_id)
            if record is None or record["tenant_id"] != tenant_id:
                return None
            return self._snapshot(record)

    def cancel(self, tenant_id: str, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._runs.get(run_id)
            if record is None or record["tenant_id"] != tenant_id:
                return None
            if record["status"] not in {"succeeded", "failed", "cancelled"}:
                record["cancelled"] = True
                record["status"] = "cancelled"
                record["summary"] = "cancelled by API request"
                record["finished_at"] = now()
                for step in record["steps"]:
                    if step["status"] in {"queued", "running"}:
                        step["status"] = "cancelled"
            return self._snapshot(record)

    def execute(self, run_id: str) -> None:
        time.sleep(0.02)
        with self._lock:
            record = self._runs.get(run_id)
            if record is None or record["cancelled"]:
                return
            record["status"] = "running"
            record["summary"] = "mock execution running"

        for step_index in range(len(record["steps"])):
            with self._lock:
                if record["cancelled"]:
                    return
                step = record["steps"][step_index]
                step["status"] = "running"
            time.sleep(0.02)
            with self._lock:
                if record["cancelled"]:
                    return
                step["status"] = "succeeded"
                step["output"] = f"mock execution completed step {step['node_id']}"
                step["latency_ms"] = 20

        with self._lock:
            if record["cancelled"]:
                return
            record["status"] = "succeeded"
            record["summary"] = "mock execution completed"
            record["finished_at"] = now()

    @staticmethod
    def _snapshot(record: dict[str, Any]) -> dict[str, Any]:
        return {
            key: [dict(step) for step in value] if key == "steps" else value
            for key, value in record.items()
            if key not in {"tenant_id", "project_id", "task", "cancelled"}
        }


app = FastAPI(title="Governed Agent API", version="0.1.0")
store = RunStore()


def require_tenant(tenant_id: str | None) -> str:
    if not tenant_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="X-Tenant-Id is required")
    return tenant_id


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/agent-runs")
async def submit_run(
    request: SubmitRun,
    tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
) -> dict[str, Any]:
    tenant = require_tenant(tenant_id)
    run, created = store.submit(tenant, request)
    if created:
        Thread(target=store.execute, args=(run["id"],), daemon=True).start()
    return {"run": run}


@app.get("/api/agent-runs/{run_id}")
async def get_run(
    run_id: str,
    tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
) -> dict[str, Any]:
    run = store.get(require_tenant(tenant_id), run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
    return {"run": run}


@app.post("/api/agent-runs/{run_id}/cancel")
async def cancel_run(
    run_id: str,
    tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
) -> dict[str, Any]:
    run = store.cancel(require_tenant(tenant_id), run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
    return {"run": run}
