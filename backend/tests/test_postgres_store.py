import os
import unittest
from pathlib import Path

from app.store import PostgresRunStore


@unittest.skipUnless(os.getenv("DATABASE_URL"), "DATABASE_URL is required for Postgres integration tests")
class PostgresRunStoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = PostgresRunStore(
            os.environ["DATABASE_URL"], Path(__file__).parents[1] / "migrations"
        )

    @classmethod
    def tearDownClass(cls):
        cls.store.close()

    def test_idempotency_and_tenant_scoping(self):
        tenant = "postgres-test-tenant"
        run, created = self.store.submit(
            tenant, "durable task", "project-a", "postgres-test-key", ["draft"]
        )
        self.assertTrue(created)
        duplicate, created_again = self.store.submit(
            tenant, "durable task", "project-a", "postgres-test-key", ["draft"]
        )
        self.assertFalse(created_again)
        self.assertEqual(duplicate["id"], run["id"])
        self.assertIsNone(self.store.get("another-tenant", run["id"]))

    def test_cancel_persists_terminal_state(self):
        tenant = "postgres-cancel-tenant"
        run, _ = self.store.submit(tenant, "cancel task", None, "postgres-cancel-key", ["step"])
        cancelled = self.store.cancel(tenant, run["id"])
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(self.store.get(tenant, run["id"])["steps"][0]["status"], "cancelled")

    def test_dag_and_retry_metadata_persists(self):
        tenant = "postgres-dag-tenant"
        run, _ = self.store.submit(
            tenant,
            "dag task",
            None,
            "postgres-dag-key",
            [
                {
                    "node_id": "review",
                    "depends_on": ["draft"],
                    "max_retries": 2,
                    "model": "tenant-model",
                    "max_tokens": 77,
                },
                {"node_id": "draft", "depends_on": [], "max_retries": 0},
            ],
        )
        steps = self.store.get(tenant, run["id"])["steps"]
        self.assertEqual(steps[0]["depends_on"], ["draft"])
        self.assertEqual(steps[0]["max_retries"], 2)
        self.assertEqual(steps[0]["model"], "tenant-model")
        self.assertEqual(steps[0]["max_tokens"], 77)
        self.assertEqual(steps[0]["attempts"], 0)
