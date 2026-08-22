"""HTTP contract for the governed AgentRun control plane."""

import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Thread
from typing import Any

from fastapi import FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field

from app.llm import Completion, LLMError, OpenAICompatibleExecutor
from app.store import InMemoryRunStore, PostgresRunStore


class RunNode(BaseModel):
    name: str = Field(min_length=1, max_length=63)
    prompt: str = ""
    role: str | None = None
    model: str | None = None
    maxTokens: int | None = Field(default=None, ge=1, le=32768)
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


def build_executor() -> OpenAICompatibleExecutor | None:
    mode = os.getenv("EXECUTION_MODE", "mock")
    if mode == "mock":
        return None
    if mode != "live":
        raise RuntimeError("EXECUTION_MODE must be 'mock' or 'live'")
    api_key = os.getenv("LLM_API_KEY")
    if not api_key:
        raise LLMError("live execution is enabled but LLM_API_KEY is not configured")
    return OpenAICompatibleExecutor(
        os.getenv("LLM_BASE_URL", "https://xindu.xyz/v1"),
        api_key,
        os.getenv("LLM_MODEL", "deepseek-v4-flash"),
        int(os.getenv("LLM_MAX_TOKENS", "512")),
        int(os.getenv("LLM_TIMEOUT_SECONDS", "45")),
    )


def execute_run(tenant_id: str, run_id: str, task: str) -> None:
    time.sleep(0.02)
    if not store.start(tenant_id, run_id):
        return
    run = store.get(tenant_id, run_id)
    if run is None:
        return
    try:
        executor = build_executor()
    except LLMError as error:
        store.fail(tenant_id, run_id, str(error))
        return
    order = _topological_order(run["steps"])
    if order is None:
        store.fail(tenant_id, run_id, "invalid DAG: duplicate, unknown, or cyclic dependency")
        return
    completed_steps = []
    for position in order:
        step = run["steps"][position]
        max_retries = int(step.get("max_retries", 0))
        attempts = int(step.get("attempts", 0))
        while True:
            if not store.start_step(tenant_id, run_id, position):
                return
            attempts += 1
            started = time.monotonic()
            try:
                completion = _complete(executor, task, step, completed_steps)
            except LLMError as error:
                store.fail_step(tenant_id, run_id, position, str(error))
                if attempts <= max_retries:
                    continue
                store.fail(tenant_id, run_id, str(error))
                return
            latency_ms = max(1, int((time.monotonic() - started) * 1000))
            if not store.succeed_step(
                tenant_id,
                run_id,
                position,
                completion.content,
                latency_ms,
                completion.input_tokens,
                completion.output_tokens,
            ):
                return
            step["status"] = "succeeded"
            step["attempts"] = attempts
            step["output"] = completion.content
            completed_steps.append(step)
            break
    store.succeed(tenant_id, run_id)


def _topological_order(steps: list[dict[str, Any]]) -> list[int] | None:
    positions = {}
    for position, step in enumerate(steps):
        node_id = step["node_id"]
        if node_id in positions:
            return None
        positions[node_id] = position
    indegree = [0] * len(steps)
    dependents = [[] for _ in steps]
    for position, step in enumerate(steps):
        dependencies = step.get("depends_on", [])
        for dependency in dependencies:
            dependency_position = positions.get(dependency)
            if dependency_position is None:
                return None
            indegree[position] += 1
            dependents[dependency_position].append(position)
    ready = [position for position, count in enumerate(indegree) if count == 0]
    order = []
    while ready:
        position = ready.pop(0)
        order.append(position)
        for dependent in dependents[position]:
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)
    return order if len(order) == len(steps) else None


def _complete(
    executor: OpenAICompatibleExecutor | None,
    task: str,
    step: dict[str, Any],
    completed_steps: list[dict[str, Any]],
) -> Completion:
    node_name = step["node_id"]
    if executor is None:
        time.sleep(0.02)
        return Completion(f"mock execution completed step {node_name}", 0, 0)
    prior_output = "\n\n".join(step["output"] for step in completed_steps if step["output"])
    options = {}
    if step.get("model"):
        options["model"] = step["model"]
    if step.get("max_tokens"):
        options["max_tokens"] = step["max_tokens"]
    return executor.complete(task, node_name, prior_output, **options)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/agent-runs")
async def submit_run(
    request: SubmitRun,
    tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
) -> dict[str, Any]:
    tenant = require_tenant(tenant_id)
    step_specs = [
        {
            "node_id": node.name,
            "depends_on": node.dependsOn,
            "max_retries": node.maxRetries or 0,
            "model": node.model,
            "max_tokens": node.maxTokens or 0,
        }
        for node in request.nodes
    ] or [{"node_id": "run", "depends_on": [], "max_retries": 0}]
    if _topological_order(step_specs) is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="nodes must have unique names and valid acyclic dependencies",
        )
    run, created = store.submit(
        tenant,
        request.task,
        request.project_id,
        request.idempotency_key,
        step_specs,
    )
    if created:
        Thread(target=execute_run, args=(tenant, run["id"], request.task), daemon=True).start()
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
