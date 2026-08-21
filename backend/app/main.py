"""HTTP contract for the governed AgentRun control plane."""

import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Thread
from typing import Any

from fastapi import FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field

from app.store import InMemoryRunStore, PostgresRunStore


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


def build_store() -> InMemoryRunStore | PostgresRunStore:
    backend = os.getenv("RUN_STORE", "memory")
    if backend == "memory":
        return InMemoryRunStore()
    if backend == "postgres":
        database_url = os.getenv("DATABASE_URL")
        if not database_url:
            raise RuntimeError("DATABASE_URL is required when RUN_STORE=postgres")
        return PostgresRunStore(database_url, Path(__file__).parents[1] / "migrations")
    raise RuntimeError("RUN_STORE must be 'memory' or 'postgres'")


store = build_store()


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    store.close()


app = FastAPI(title="Governed Agent API", version="0.1.0", lifespan=lifespan)


def require_tenant(tenant_id: str | None) -> str:
    if not tenant_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="X-Tenant-Id is required")
    return tenant_id


def execute_mock(tenant_id: str, run_id: str) -> None:
    time.sleep(0.02)
    if not store.start(tenant_id, run_id):
        return
    run = store.get(tenant_id, run_id)
    if run is None:
        return
    for position in range(len(run["steps"])):
        if not store.start_step(tenant_id, run_id, position):
            return
        time.sleep(0.02)
        if not store.succeed_step(tenant_id, run_id, position):
            return
    store.succeed(tenant_id, run_id)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/agent-runs")
async def submit_run(
    request: SubmitRun,
    tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
) -> dict[str, Any]:
    tenant = require_tenant(tenant_id)
    run, created = store.submit(
        tenant,
        request.task,
        request.project_id,
        request.idempotency_key,
        [node.name for node in request.nodes] or ["run"],
    )
    if created:
        Thread(target=execute_mock, args=(tenant, run["id"]), daemon=True).start()
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
