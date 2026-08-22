"""HTTP adapter for the existing Equaxis AgentRun runtime.

The control plane deliberately does not embed or reimplement the Equaxis
runtime.  This adapter speaks the existing AgentRun HTTP contract so the
Operator can keep using the already-provisioned product bridge and
``subagent-runtime`` process.
"""

from __future__ import annotations

import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from typing import Any, Protocol


class RuntimeClientError(RuntimeError):
    """A bounded, provider-safe error returned by the existing runtime."""


class AgentRuntimeClient(Protocol):
    """The narrow seam every external Agent connector must implement."""

    def submit(
        self,
        tenant_id: str,
        project_id: str | None,
        task: str,
        nodes: list[dict[str, Any]],
        idempotency_key: str,
    ) -> dict[str, Any]: ...

    def get(
        self, tenant_id: str, project_id: str | None, runtime_run_id: str
    ) -> dict[str, Any]: ...

    def cancel(
        self, tenant_id: str, project_id: str | None, runtime_run_id: str
    ) -> dict[str, Any]: ...


class EquaxisRuntimeClient:
    """Small stdlib-only client for the existing Equaxis AgentRun API."""

    def __init__(self, base_url: str, token: str, timeout_seconds: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout_seconds = timeout_seconds

    @classmethod
    def from_env(cls) -> "EquaxisRuntimeClient":
        return cls._from_env(
            "EQUAXIS_RUNTIME_URL",
            "EQUAXIS_RUNTIME_TOKEN",
            "EQUAXIS_RUNTIME_TIMEOUT_SECONDS",
            "Equaxis",
            require_token=False,
        )

    @classmethod
    def _from_env(
        cls,
        url_name: str,
        token_name: str,
        timeout_name: str,
        provider_name: str,
        require_token: bool = True,
    ) -> "EquaxisRuntimeClient":
        base_url = os.getenv(url_name, "").strip()
        token = os.getenv(token_name, "").strip()
        if not base_url:
            raise RuntimeClientError(
                f"{url_name} is required for the {provider_name} runtime"
            )
        if require_token and not token:
            raise RuntimeClientError(
                f"{token_name} is required for the {provider_name} runtime"
            )
        try:
            timeout = float(os.getenv(timeout_name, "30"))
        except ValueError as error:
            raise RuntimeClientError(f"{timeout_name} must be numeric") from error
        return cls(base_url, token, timeout)

    def submit(
        self,
        tenant_id: str,
        project_id: str | None,
        task: str,
        nodes: list[dict[str, Any]],
        idempotency_key: str,
    ) -> dict[str, Any]:
        payload_nodes = []
        for node in nodes:
            payload: dict[str, Any] = {
                "name": node["node_id"],
                "prompt": node.get("prompt") or task,
                "dependsOn": list(node.get("depends_on", [])),
                "maxRetries": int(node.get("max_retries", 0)),
            }
            if node.get("role"):
                payload["role"] = node["role"]
            if node.get("model"):
                payload["model"] = node["model"]
            payload_nodes.append(payload)
        body = {
            "task": task,
            "nodes": payload_nodes,
            "project_id": project_id if self.token else None,
            "idempotency_key": idempotency_key,
        }
        return self._request(
            "POST",
            "/api/agent-runs",
            tenant_id,
            project_id,
            body,
            idempotency_key=idempotency_key,
        )

    def get(
        self, tenant_id: str, project_id: str | None, runtime_run_id: str
    ) -> dict[str, Any]:
        return self._request(
            "GET", f"/api/agent-runs/{runtime_run_id}", tenant_id, project_id
        )

    def cancel(
        self, tenant_id: str, project_id: str | None, runtime_run_id: str
    ) -> dict[str, Any]:
        return self._request(
            "POST", f"/api/agent-runs/{runtime_run_id}/cancel", tenant_id, project_id
        )

    def _request(
        self,
        method: str,
        path: str,
        tenant_id: str,
        project_id: str | None,
        body: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        headers = {
            "Accept": "application/json",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
            headers["X-Tenant-Id"] = tenant_id
            if project_id:
                headers["X-Project-Id"] = project_id
        if idempotency_key:
            headers["X-Idempotency-Key"] = idempotency_key
        data = json.dumps(body).encode("utf-8") if body is not None else None
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = Request(
            f"{self.base_url}{path}", headers=headers, data=data, method=method
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read()
        except HTTPError as error:
            raise RuntimeClientError(f"agent runtime returned HTTP {error.code}") from error
        except (URLError, TimeoutError, OSError) as error:
            raise RuntimeClientError("agent runtime is unavailable") from error
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeClientError("agent runtime returned invalid JSON") from error
        if not isinstance(decoded, dict):
            raise RuntimeClientError("agent runtime returned an invalid response")
        run = decoded.get("run")
        if isinstance(run, dict):
            return run
        return decoded


class HttpAgentRuntimeClient(EquaxisRuntimeClient):
    """Generic AgentRun-compatible connector for a separately hosted agent.

    Any agent service that implements the normalized AgentRun HTTP contract can
    use this connector.  Equaxis remains a named connector because it is the
    first and currently deployed implementation of that contract.
    """

    @classmethod
    def from_env(cls) -> "HttpAgentRuntimeClient":
        return cls._from_env(
            "AGENT_RUNTIME_URL",
            "AGENT_RUNTIME_TOKEN",
            "AGENT_RUNTIME_TIMEOUT_SECONDS",
            "external agent",
        )
