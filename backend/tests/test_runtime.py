import json
import os
import unittest
from unittest.mock import patch

from app.runtime import EquaxisRuntimeClient, HttpAgentRuntimeClient


class RuntimeAdapterTests(unittest.TestCase):
    def test_generic_agent_connector_uses_normalized_agent_run_contract(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self):
                return json.dumps({"run": {"id": "external-1", "status": "queued"}}).encode()

        client = HttpAgentRuntimeClient("https://agent.example", "secret")
        with patch("app.runtime.urlopen", return_value=Response()) as open_url:
            result = client.submit(
                "tenant-a",
                "project-a",
                "summarize",
                [
                    {
                        "node_id": "draft",
                        "prompt": "write it",
                        "role": "writer",
                        "depends_on": [],
                        "max_retries": 1,
                        "model": "agent-model",
                    }
                ],
                "run-key",
            )

        request = open_url.call_args.args[0]
        self.assertEqual(request.full_url, "https://agent.example/api/agent-runs")
        self.assertEqual(request.get_header("Authorization"), "Bearer secret")
        self.assertEqual(request.get_header("X-tenant-id"), "tenant-a")
        body = json.loads(request.data.decode())
        self.assertEqual(body["nodes"][0]["prompt"], "write it")
        self.assertEqual(body["nodes"][0]["model"], "agent-model")
        self.assertEqual(result["id"], "external-1")

    def test_generic_connector_reads_only_generic_runtime_settings(self):
        with patch.dict(
            os.environ,
            {
                "AGENT_RUNTIME_URL": "https://generic.example/",
                "AGENT_RUNTIME_TOKEN": "generic-token",
                "AGENT_RUNTIME_TIMEOUT_SECONDS": "12",
            },
            clear=False,
        ):
            client = HttpAgentRuntimeClient.from_env()

        self.assertEqual(client.base_url, "https://generic.example")
        self.assertEqual(client.token, "generic-token")
        self.assertEqual(client.timeout_seconds, 12)

    def test_equaxis_connector_can_use_local_anonymous_auth(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self):
                return b'{"run":{"id":"run-1","status":"done"}}'

        client = EquaxisRuntimeClient("http://host.docker.internal:8000", "")
        with patch("app.runtime.urlopen", return_value=Response()) as open_url:
            client.get("tenant-id", "project-id", "run-1")

        request = open_url.call_args.args[0]
        self.assertIsNone(request.get_header("Authorization"))
