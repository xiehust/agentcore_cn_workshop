"""长期记忆 REST 读路径的降级与响应契约测试。"""

import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

from botocore.config import Config


INFRA_DIR = Path(__file__).resolve().parents[1]


def _load_tasks_api():
    path = INFRA_DIR / "tasks-api-lambda/handler.py"
    spec = importlib.util.spec_from_file_location("tasks_api_memories_test", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载测试模块: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TASKS_API = _load_tasks_api()


def _response_body(response: dict) -> dict:
    return json.loads(response["body"])


class ChatMemoriesTests(unittest.TestCase):
    def test_memory_client_has_short_timeout_and_no_retry(self):
        client = MagicMock()
        with patch.object(TASKS_API.boto3, "client", return_value=client) as create:
            TASKS_API._clients.pop("lambda-memory", None)
            self.assertIs(TASKS_API._memory_client(), client)

        service, = create.call_args.args
        config = create.call_args.kwargs["config"]
        self.assertEqual(service, "lambda")
        self.assertIsInstance(config, Config)
        self.assertLess(config.read_timeout, 29)
        self.assertEqual(config.retries, {"max_attempts": 0})

    def test_memory_timeout_degrades_to_json_response(self):
        memory_client = MagicMock()
        memory_client.invoke.side_effect = TimeoutError("Aurora 仍在冷恢复")
        with (
            patch.object(TASKS_API, "MEMORY_LAMBDA_NAME", "memory-lambda"),
            patch.object(TASKS_API, "_memory_client", return_value=memory_client),
        ):
            response = TASKS_API.list_user_memories("vivian")

        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(
            _response_body(response),
            {"memories": [], "degraded": True},
        )

    def test_memory_rows_are_filtered_and_sorted(self):
        payload = {
            "status": "ok",
            "result": [
                {
                    "id": "old",
                    "payload": {"data": "旧偏好", "created_at": "2026-08-01T00:00:00Z"},
                },
                {"id": "empty", "payload": {"data": ""}},
                {
                    "id": "new",
                    "payload": {"data": "新偏好", "created_at": "2026-08-02T00:00:00Z"},
                },
            ],
        }
        memory_client = MagicMock()
        memory_client.invoke.return_value = {
            "Payload": io.BytesIO(json.dumps(payload).encode("utf-8"))
        }
        with (
            patch.object(TASKS_API, "MEMORY_LAMBDA_NAME", "memory-lambda"),
            patch.object(TASKS_API, "_memory_client", return_value=memory_client),
        ):
            response = TASKS_API.list_user_memories("vivian")

        body = _response_body(response)
        self.assertEqual([item["id"] for item in body["memories"]], ["new", "old"])
        args = json.loads(memory_client.invoke.call_args.kwargs["Payload"])
        self.assertEqual(args["args"]["filters"], {"user_id": "vivian"})


if __name__ == "__main__":
    unittest.main()
