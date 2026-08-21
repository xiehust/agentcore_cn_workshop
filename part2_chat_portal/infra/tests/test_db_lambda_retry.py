"""db Lambda 的连接重试、绝对截止点与不重放写入契约测试。"""

import importlib.util
import os
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import MagicMock, call, patch


INFRA_DIR = Path(__file__).resolve().parents[1]
HANDLER_PATH = INFRA_DIR / "db-lambda" / "handler.py"


def _load_handler():
    pg8000 = types.ModuleType("pg8000")
    native = types.ModuleType("pg8000.native")
    native.DatabaseError = type("DatabaseError", (Exception,), {})
    native.Connection = MagicMock()
    pg8000.native = native
    spec = importlib.util.spec_from_file_location("db_lambda_retry_test", HANDLER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载测试模块: {HANDLER_PATH}")
    module = importlib.util.module_from_spec(spec)
    with (
        patch.dict(os.environ, {
            "DB_HOST": "db.example",
            "DB_SECRET_ARN": "secret-arn",
        }),
        patch.dict(sys.modules, {"pg8000": pg8000, "pg8000.native": native}),
    ):
        spec.loader.exec_module(module)
    return module


DB = _load_handler()


class DbLambdaRetryTests(unittest.TestCase):
    def setUp(self):
        DB._CONN = None

    def test_health_check_failure_reconnects_before_application_sql(self):
        stale = MagicMock()
        stale.run.side_effect = OSError("旧连接已断开")
        fresh = MagicMock()
        fresh.run.side_effect = [[[1]], [["ok"]]]
        fresh.columns = [{"name": "value"}]
        DB._CONN = stale

        with (
            patch.object(DB, "_connect", return_value=fresh) as connect,
            patch.object(DB.time, "monotonic", return_value=10.0),
        ):
            rows, columns = DB._run("SELECT :value", {"value": "ok"}, 30)

        self.assertEqual(rows, [["ok"]])
        self.assertEqual(columns, [{"name": "value"}])
        stale.run.assert_called_once_with("SELECT 1")
        connect.assert_called_once_with(40.0)
        self.assertEqual(fresh.run.call_args_list, [
            call("SELECT 1"),
            call("SELECT :value", value="ok"),
        ])

    def test_ambiguous_application_failure_is_never_replayed(self):
        conn = MagicMock()
        conn.run.side_effect = OSError("提交结果未知")

        with (
            patch.object(DB, "_prepare_connection", return_value=conn) as prepare,
            patch.object(DB.time, "monotonic", return_value=20.0),
        ):
            with self.assertRaisesRegex(OSError, "提交结果未知"):
                DB._run("INSERT INTO t VALUES (:value)", {"value": 1}, 30)

        prepare.assert_called_once_with(50.0)
        conn.run.assert_called_once_with("INSERT INTO t VALUES (:value)", value=1)
        self.assertIsNone(DB._CONN)

    def test_exhausted_deadline_prevents_application_sql(self):
        conn = MagicMock()

        with (
            patch.object(DB, "_deadline", return_value=42.0),
            patch.object(DB, "_prepare_connection", return_value=conn) as prepare,
            patch.object(DB.time, "monotonic", return_value=42.0),
        ):
            with self.assertRaisesRegex(TimeoutError, "未执行应用 SQL"):
                DB._run("UPDATE t SET value = 1", {}, 30)

        prepare.assert_called_once_with(42.0)
        conn.run.assert_not_called()

    def test_connect_retries_with_remaining_absolute_budget(self):
        DB._CREDS = ("user", "password")
        connection = DB.pg8000.native.Connection
        connection.reset_mock()
        connected = MagicMock()
        connection.side_effect = [OSError("冷恢复中"), connected]

        with (
            patch.object(DB.time, "monotonic", side_effect=[1.0, 3.0, 4.0]),
            patch.object(DB.time, "sleep") as sleep,
        ):
            result = DB._connect(5.0)

        self.assertIs(result, connected)
        self.assertEqual(connection.call_count, 2)
        sleep.assert_called_once_with(2.0)


if __name__ == "__main__":
    unittest.main()
