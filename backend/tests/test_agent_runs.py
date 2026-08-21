import time
import unittest

from fastapi.testclient import TestClient

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
