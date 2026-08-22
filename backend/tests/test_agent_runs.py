import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.llm import Completion, LLMError
from app.main import app, store


class AgentRunApiTests(unittest.TestCase):
    def setUp(self):
        store.clear()
        self.client = TestClient(app)
        self.headers = {"X-Tenant-Id": "tenant-a"}

    def _submit(self, key="demo-1", nodes=None):
        return self.client.post(
            "/api/agent-runs",
            headers=self.headers,
            json={
                "task": "summarize the release notes",
                "project_id": "project-a",
                "idempotency_key": key,
                "nodes": nodes or [{"name": "draft", "role": "writer"}],
            },
        )

    def test_submit_is_idempotent_and_completes(self):
        first = self._submit()
        self.assertEqual(first.status_code, 200)
        run_id = first.json()["run"]["id"]

        duplicate = self._submit()
        self.assertEqual(duplicate.status_code, 200)
        self.assertEqual(duplicate.json()["run"]["id"], run_id)

        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            result = self.client.get(f"/api/agent-runs/{run_id}", headers=self.headers)
            self.assertEqual(result.status_code, 200)
            if result.json()["run"]["status"] == "succeeded":
                break
            time.sleep(0.02)

        run = result.json()["run"]
        self.assertEqual(run["status"], "succeeded")
        self.assertEqual(run["steps"][0]["status"], "succeeded")
        self.assertIn("mock execution", run["steps"][0]["output"])

    def test_tenant_isolation_and_cancellation(self):
        response = self._submit(
            key="cancel-me",
            nodes=[{"name": f"step-{index}"} for index in range(10)],
        )
        run_id = response.json()["run"]["id"]

        other_tenant = self.client.get(
            f"/api/agent-runs/{run_id}", headers={"X-Tenant-Id": "tenant-b"}
        )
        self.assertEqual(other_tenant.status_code, 404)

        cancelled = self.client.post(
            f"/api/agent-runs/{run_id}/cancel", headers=self.headers
        )
        self.assertEqual(cancelled.status_code, 200)
        self.assertEqual(cancelled.json()["run"]["status"], "cancelled")

    def test_dag_dependencies_control_execution_order(self):
        seen = []

        class RecordingExecutor:
            def complete(self, task, node_name, prior_output):
                seen.append((node_name, prior_output))
                return Completion(f"done:{node_name}", 1, 1)

        with patch("app.main.build_executor", return_value=RecordingExecutor()):
            response = self._submit(
                key="dag-order",
                nodes=[
                    {"name": "review", "dependsOn": ["draft"]},
                    {"name": "draft"},
                ],
            )
            run_id = response.json()["run"]["id"]
            run = self._wait_for_terminal(run_id)

        self.assertEqual(run["status"], "succeeded")
        self.assertEqual([node for node, _ in seen], ["draft", "review"])
        self.assertEqual(seen[1][1], "done:draft")

    def test_provider_failure_retries_until_max_retries(self):
        class FlakyExecutor:
            def __init__(self):
                self.calls = 0

            def complete(self, task, node_name, prior_output):
                self.calls += 1
                if self.calls == 1:
                    raise LLMError("HTTP 503")
                return Completion("recovered", 2, 3)

        executor = FlakyExecutor()
        with patch("app.main.build_executor", return_value=executor):
            response = self._submit(
                key="retry-once",
                nodes=[{"name": "draft", "maxRetries": 1}],
            )
            run_id = response.json()["run"]["id"]
            run = self._wait_for_terminal(run_id)

        self.assertEqual(run["status"], "succeeded")
        self.assertEqual(executor.calls, 2)
        self.assertEqual(run["steps"][0]["attempts"], 2)
        self.assertEqual(run["steps"][0]["output"], "recovered")

    def _wait_for_terminal(self, run_id):
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            result = self.client.get(f"/api/agent-runs/{run_id}", headers=self.headers)
            run = result.json()["run"]
            if run["status"] in {"succeeded", "failed", "cancelled"}:
                return run
            time.sleep(0.02)
        self.fail(f"run {run_id} did not reach a terminal state")
