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
from app.runtime import (
    AgentRuntimeClient,
    EquaxisRuntimeClient,
    HttpAgentRuntimeClient,
    RuntimeClientError,
)
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
        if mode in {"equaxis", "agent_http"}:
            return None
        raise RuntimeError("EXECUTION_MODE must be 'mock', 'live', 'equaxis', or 'agent_http'")
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


def build_runtime_client() -> AgentRuntimeClient | None:
    mode = os.getenv("EXECUTION_MODE", "mock")
    if mode == "equaxis":
        return EquaxisRuntimeClient.from_env()
    if mode == "agent_http":
        return HttpAgentRuntimeClient.from_env()
    return None


def execute_run(
    tenant_id: str,
    run_id: str,
    task: str,
    project_id: str | None = None,
    idempotency_key: str | None = None,
) -> None:
    time.sleep(0.02)
    if not store.start(tenant_id, run_id):
        return
    run = store.get(tenant_id, run_id)
    if run is None:
        return
    if os.getenv("EXECUTION_MODE", "mock") in {"equaxis", "agent_http"}:
        _execute_external_run(
            tenant_id,
            run_id,
            task,
            project_id,
            idempotency_key or run_id,
            run,
        )
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


def _execute_external_run(
    tenant_id: str,
    run_id: str,
    task: str,
    project_id: str | None,
    idempotency_key: str,
    run: dict[str, Any],
) -> None:
    try:
        runtime = build_runtime_client()
        if runtime is None:
            raise RuntimeClientError("external agent runtime adapter is not configured")
        external = runtime.submit(
            tenant_id,
            project_id,
            task,
            run["steps"],
            idempotency_key,
        )
        external_id = str(external.get("id") or "")
        if not external_id:
            raise RuntimeClientError("external agent runtime response did not include a run id")
        if not store.set_runtime_run_id(tenant_id, run_id, external_id):
            return
        prefix = "EQUAXIS" if os.getenv("EXECUTION_MODE") == "equaxis" else "AGENT_RUNTIME"
        timeout = _runtime_float(f"{prefix}_RUN_TIMEOUT_SECONDS", 3600.0)
        interval = _runtime_float(f"{prefix}_POLL_SECONDS", 0.5)
        deadline = time.monotonic() + timeout
        snapshot = external
        while True:
            _sync_external_steps(tenant_id, run_id, snapshot)
            status_name = _normalize_runtime_status(snapshot.get("status"))
            if status_name in {"succeeded", "failed", "cancelled"}:
                if status_name == "succeeded":
                    store.succeed(tenant_id, run_id)
                elif status_name == "cancelled":
                    store.cancel(tenant_id, run_id)
                else:
                    store.fail(
                        tenant_id,
                        run_id,
                        str(snapshot.get("summary") or "external agent runtime failed"),
                    )
                return
            if time.monotonic() >= deadline:
                try:
                    runtime.cancel(tenant_id, project_id, external_id)
                except RuntimeClientError:
                    pass
                store.fail(tenant_id, run_id, "external agent runtime polling timed out")
                return
            time.sleep(interval)
            snapshot = runtime.get(tenant_id, project_id, external_id)
    except RuntimeClientError as error:
        store.fail(tenant_id, run_id, str(error))


def _runtime_float(name: str, default: float) -> float:
    try:
        return max(0.01, float(os.getenv(name, str(default))))
    except ValueError:
        return default


def _normalize_runtime_status(value: Any) -> str:
    status_name = str(value or "queued").lower()
    return {
        "done": "succeeded",
        "success": "succeeded",
        "successful": "succeeded",
        "complete": "succeeded",
        "completed": "succeeded",
        "error": "failed",
    }.get(status_name, status_name)


def _sync_external_steps(tenant_id: str, run_id: str, snapshot: dict[str, Any]) -> None:
    local = store.get(tenant_id, run_id)
    if local is None:
        return
    positions = {step["node_id"]: position for position, step in enumerate(local["steps"])}
    for external_step in snapshot.get("steps", []):
        if not isinstance(external_step, dict):
            continue
        node_id = external_step.get("node_id") or external_step.get("name")
        position = positions.get(node_id)
        if position is None:
            continue
        external_status = _normalize_runtime_status(external_step.get("status"))
        current = local["steps"][position]["status"]
        if external_status in {"running", "succeeded", "failed"} and current == "queued":
            if not store.start_step(tenant_id, run_id, position):
                continue
        if external_status == "succeeded":
            store.succeed_step(
                tenant_id,
                run_id,
                position,
                external_step.get("output") or external_step.get("result") or "",
                int(external_step.get("latency_ms") or 0),
                int(external_step.get("input_tokens") or 0),
                int(external_step.get("output_tokens") or 0),
            )
        elif external_status == "failed":
            store.fail_step(
                tenant_id,
                run_id,
                position,
                str(external_step.get("error") or "external agent runtime step failed"),
            )


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
            "prompt": node.prompt,
            "role": node.role or "",
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
        Thread(
            target=execute_run,
            args=(tenant, run["id"], request.task, request.project_id, request.idempotency_key),
            daemon=True,
        ).start()
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
    tenant = require_tenant(tenant_id)
    runtime_run_id = store.runtime_run_id(tenant, run_id)
    project_id = store.project_id(tenant, run_id)
    run = store.cancel(tenant, run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
    if runtime_run_id and os.getenv("EXECUTION_MODE", "mock") in {"equaxis", "agent_http"}:
        try:
            runtime = build_runtime_client()
            if runtime is not None:
                runtime.cancel(tenant, project_id, runtime_run_id)
        except RuntimeClientError:
            # The local durable ledger is already cancelled; the poller will
            # stop applying external snapshots. Avoid leaking provider details.
            pass
    return {"run": run}
