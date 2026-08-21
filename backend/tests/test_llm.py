import json
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

from app.llm import LLMError, OpenAICompatibleExecutor


class OpenAICompatibleExecutorTests(unittest.TestCase):
    def setUp(self):
        self.executor = OpenAICompatibleExecutor(
            "https://xindu.xyz/v1", "test-key", "deepseek-v4-flash", 128, 10
        )

    @patch("app.llm.urlopen")
    def test_parses_completion_and_usage(self, urlopen):
        response = MagicMock()
        response.read.return_value = json.dumps(
            {
                "choices": [{"message": {"content": "  useful result  "}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 7},
            }
        ).encode()
        urlopen.return_value.__enter__.return_value = response

        completion = self.executor.complete("test task", "draft", "")

        self.assertEqual(completion.content, "useful result")
        self.assertEqual(completion.input_tokens, 12)
        self.assertEqual(completion.output_tokens, 7)
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://xindu.xyz/v1/chat/completions")
        self.assertNotIn("test-key", request.data.decode())

    @patch("app.llm.urlopen")
    def test_redacts_provider_error_body(self, urlopen):
        error = HTTPError("https://xindu.xyz/v1/chat/completions", 401, "Unauthorized", {}, None)
        urlopen.side_effect = error

        with self.assertRaisesRegex(LLMError, "HTTP 401"):
            self.executor.complete("test task", "draft", "")
        error.close()
