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
