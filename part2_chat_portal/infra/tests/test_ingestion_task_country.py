"""Country contract tests for the Part 2 task API and runner Lambdas."""

import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch


INFRA_DIR = Path(__file__).resolve().parents[1]


def _load_module(name: str, relative_path: str):
    path = INFRA_DIR / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载测试模块: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TASKS_API = _load_module("tasks_api_country_test", "tasks-api-lambda/handler.py")
RUNNER = _load_module("runner_country_test", "runner-lambda/handler.py")


def _response_body(response: dict) -> dict:
    return json.loads(response["body"])


def _parameter_values(call) -> dict[str, str]:
    parameters = call.args[1]
    return {
        item["name"]: item["value"]["stringValue"]
        for item in parameters
    }


class TasksApiCountryTests(unittest.TestCase):
    def test_country_defaults_and_whitespace_normalization(self):
        for value in (None, "", " \t\n "):
            with self.subTest(value=value):
                self.assertEqual(TASKS_API._normalize_country(value), "UK")

        self.assertEqual(
            TASKS_API._normalize_country("  Latin \n  America  "),
            "Latin America",
        )
        self.assertEqual(TASKS_API._normalize_country("德国"), "德国")

    def test_create_task_persists_custom_country_without_whitelist(self):
        task = {
            "task_id": "task-1",
            "name": "custom-country",
            "prompt": "搜索候选商品",
            "country": "Latin America",
            "mode": "one_time",
            "schedule_expr": None,
            "status": "active",
        }
        with (
            patch.object(TASKS_API, "_execute", return_value=[]) as execute,
            patch.object(TASKS_API, "_create_run", return_value="run-1"),
            patch.object(TASKS_API, "_invoke_runner"),
            patch.object(TASKS_API, "_get_task", return_value=task),
        ):
            response = TASKS_API.create_task({
                "name": "custom-country",
                "prompt": "搜索候选商品",
                "country": "  Latin \n America ",
                "mode": "one_time",
            })

        self.assertEqual(response["statusCode"], 201)
        self.assertEqual(_response_body(response)["task"]["country"], "Latin America")
        self.assertIn("country", execute.call_args.args[0])
        self.assertEqual(
            _parameter_values(execute.call_args)["country"],
            "Latin America",
        )

    def test_create_task_omitted_or_empty_country_persists_uk(self):
        for body in (
            {"name": "legacy", "prompt": "搜索候选商品", "mode": "one_time"},
            {
                "name": "empty",
                "prompt": "搜索候选商品",
                "country": "   ",
                "mode": "one_time",
            },
        ):
            with self.subTest(body=body):
                with (
                    patch.object(TASKS_API, "_execute", return_value=[]) as execute,
                    patch.object(TASKS_API, "_create_run", return_value="run-1"),
                    patch.object(TASKS_API, "_invoke_runner"),
                    patch.object(TASKS_API, "_get_task", return_value={"country": "UK"}),
                ):
                    response = TASKS_API.create_task(body)

                self.assertEqual(response["statusCode"], 201)
                self.assertEqual(_parameter_values(execute.call_args)["country"], "UK")

    def test_country_over_64_characters_is_rejected_before_write(self):
        with patch.object(TASKS_API, "_execute") as execute:
            response = TASKS_API.create_task({
                "name": "too-long",
                "prompt": "搜索候选商品",
                "country": "X" * 65,
                "mode": "one_time",
            })

        self.assertEqual(response["statusCode"], 400)
        self.assertIn("64", _response_body(response)["error"])
        execute.assert_not_called()

    def test_all_task_reads_select_country(self):
        self.assertIn("COALESCE(t.country, 'UK') AS country", TASKS_API.TASK_COLUMNS)

    def test_task_response_endpoints_preserve_country(self):
        task = {
            "task_id": "task-1",
            "name": "custom-country",
            "prompt": "搜索候选商品",
            "country": "德国",
            "mode": "recurring",
            "schedule_expr": "rate(6 hours)",
            "status": "active",
            "created_at": "2026-08-13T00:00:00Z",
            "updated_at": "2026-08-13T00:00:00Z",
        }

        list_row = {
            **task,
            "last_run_id": None,
            "last_run_status": None,
            "last_run_started_at": None,
            "last_run_finished_at": None,
        }
        with patch.object(TASKS_API, "_execute", return_value=[list_row]):
            listed = _response_body(TASKS_API.list_tasks())
        self.assertEqual(listed["tasks"][0]["country"], "德国")

        with (
            patch.object(TASKS_API, "_get_task", return_value=task),
            patch.object(TASKS_API, "_execute", return_value=[]),
        ):
            detailed = _response_body(TASKS_API.get_task("task-1"))
        self.assertEqual(detailed["task"]["country"], "德国")

        paused_task = {**task, "status": "paused"}
        with (
            patch.object(TASKS_API, "_get_task", side_effect=[task, paused_task]),
            patch.object(TASKS_API, "_set_schedule_state"),
            patch.object(TASKS_API, "_execute"),
        ):
            paused = _response_body(TASKS_API.set_task_state("task-1", "pause"))
        self.assertEqual(paused["task"]["country"], "德国")


class RunnerCountryTests(unittest.TestCase):
    def test_runtime_prompt_is_generic_and_preserves_original_prompt(self):
        original_prompt = "搜索 desk organizer\n保留原始换行。"
        prompt = RUNNER._build_runtime_prompt("德国", original_prompt)

        self.assertIn("任务国家/站点：德国", prompt)
        self.assertIn("明确支持该国家/站点的 Skill", prompt)
        self.assertIn("优先于原始任务文本中的冲突描述", prompt)
        self.assertIn("域名、币种、筛选规则和入库 site", prompt)
        self.assertIn("不得回退到其他国家/站点", prompt)
        self.assertTrue(prompt.endswith(original_prompt))
        self.assertNotIn("ebay.", prompt)

    def test_runtime_invocation_uses_stored_country_for_portal_and_scheduler(self):
        task = {
            "task_id": "task-1",
            "prompt": "原始任务指令",
            "country": "DE custom",
            "status": "active",
        }
        for event in (
            {"task_id": "task-1", "run_id": "run-1"},
            {"task_id": "task-1"},
        ):
            with self.subTest(event=event):
                with (
                    patch.object(RUNNER, "PART1_RUNTIME_ARN", "runtime-arn"),
                    patch.object(RUNNER, "_load_task", return_value=task),
                    patch.object(RUNNER, "_begin_run"),
                    patch.object(RUNNER, "EventWriter", return_value=MagicMock()),
                    patch.object(RUNNER, "_invoke_runtime_stream", return_value=[]) as invoke,
                    patch.object(RUNNER, "_consume"),
                    patch.object(RUNNER, "_collect_stats", return_value={}),
                    patch.object(RUNNER, "_finish_run"),
                ):
                    response = RUNNER.lambda_handler(event, None)

                self.assertTrue(response["ok"])
                runtime_payload = invoke.call_args.args[1]
                self.assertEqual(runtime_payload["run_id"], response["run_id"])
                self.assertIn("任务国家/站点：DE custom", runtime_payload["prompt"])
                self.assertTrue(runtime_payload["prompt"].endswith("原始任务指令"))

    def test_missing_database_country_defensively_defaults_to_uk(self):
        prompt = RUNNER._build_runtime_prompt(None, "原始任务")
        self.assertIn("任务国家/站点：UK", prompt)
        whitespace_prompt = RUNNER._build_runtime_prompt(" \n\t ", "原始任务")
        self.assertIn("任务国家/站点：UK", whitespace_prompt)


if __name__ == "__main__":
    unittest.main()
