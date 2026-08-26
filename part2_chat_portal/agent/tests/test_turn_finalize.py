"""流尾后台收尾的 stdlib 回归测试（用轻量 stub 导入 Runtime 入口）。"""

import asyncio
import importlib.util
import sys
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch


class _FakeApp:
    def __init__(self):
        self._lock = threading.Lock()
        self._next_id = 0
        self.active: set[int] = set()
        self.completed: list[int] = []
        self.completed_event = threading.Event()

    def entrypoint(self, fn):
        return fn

    def add_async_task(self, _name, _metadata=None):
        with self._lock:
            self._next_id += 1
            task_id = self._next_id
            self.active.add(task_id)
            return task_id

    def complete_async_task(self, task_id):
        with self._lock:
            if task_id not in self.active:
                return False
            self.active.remove(task_id)
            self.completed.append(task_id)
            self.completed_event.set()
            return True

    def run(self):
        return None


def _module(name: str, **attrs):
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    return mod


def _load_main():
    """只 stub import 依赖；被测线程池/锁/调度逻辑使用真实 stdlib。"""

    class _Placeholder:
        def __init__(self, *args, **kwargs):
            pass

    memory_backend = _module("memory_backend")
    memory_backend.enabled = lambda: True

    async def _memorize(*_args):
        return None

    memory_backend.memorize = _memorize
    stubs = {
        "boto3": _module("boto3", client=lambda *_args, **_kwargs: _Placeholder()),
        "botocore": _module("botocore"),
        "botocore.config": _module("botocore.config", Config=_Placeholder),
        "bedrock_agentcore": _module("bedrock_agentcore"),
        "bedrock_agentcore.runtime": _module(
            "bedrock_agentcore.runtime", BedrockAgentCoreApp=_FakeApp
        ),
        "strands": _module("strands", Agent=_Placeholder),
        "strands.models": _module("strands.models"),
        "strands.models.openai": _module(
            "strands.models.openai", OpenAIModel=_Placeholder
        ),
        "strands.tools": _module("strands.tools"),
        "strands.tools.mcp": _module("strands.tools.mcp", MCPClient=_Placeholder),
        "strands_tools": _module("strands_tools"),
        "strands_tools.code_interpreter": _module(
            "strands_tools.code_interpreter", AgentCoreCodeInterpreter=_Placeholder
        ),
        "mcp": _module("mcp"),
        "mcp.client": _module("mcp.client"),
        "mcp.client.streamable_http": _module(
            "mcp.client.streamable_http", streamablehttp_client=lambda *_args, **_kwargs: None
        ),
        "memory_backend": memory_backend,
    }
    previous = {name: sys.modules.get(name) for name in stubs}
    sys.modules.update(stubs)
    try:
        path = Path(__file__).parents[1] / "main.py"
        spec = importlib.util.spec_from_file_location("_chat_agent_main_test", path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        return module
    finally:
        for name, original in previous.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original


class TurnFinalizeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.main = _load_main()

    def setUp(self):
        self.main.app.completed_event.clear()
        self.main.app.completed.clear()
        self.main.app.active.clear()
        self.main._SESSION_USER.clear()

    def _wait_for(self, predicate, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        self.fail("等待后台收尾状态超时")

    def test_slow_memory_does_not_delay_schedule_or_persistence(self):
        persisted = threading.Event()
        memory_started = threading.Event()
        memory_release = threading.Event()

        def persist(_session_id, _messages):
            persisted.set()

        async def memorize(_user_id, _prompt, _reply):
            memory_started.set()
            while not memory_release.is_set():
                await asyncio.sleep(0.01)

        started = time.monotonic()
        with (
            patch.object(self.main, "_persist_new", persist),
            patch.object(self.main, "_memory_user", return_value="analyst"),
            patch.object(self.main.memory_backend, "memorize", memorize),
        ):
            self.main._schedule_finalize("web-" + "a" * 36, [{"role": "assistant"}], "p", "r")
            self.assertLess(time.monotonic() - started, 0.2)
            self.assertTrue(persisted.wait(1))
            self.assertTrue(memory_started.wait(1))
            self.assertEqual(len(self.main.app.active), 1)
            memory_release.set()
            self.assertTrue(self.main.app.completed_event.wait(1))

        self.assertFalse(self.main.app.active)
        self.assertEqual(len(self.main.app.completed), 1)

    def test_next_turn_persists_while_previous_memory_is_slow(self):
        persist_lengths: list[int] = []
        second_persisted = threading.Event()
        first_memory_started = threading.Event()
        memory_release = threading.Event()
        memory_calls = 0
        memory_calls_lock = threading.Lock()

        def persist(_session_id, messages):
            persist_lengths.append(len(messages))
            if len(persist_lengths) == 2:
                second_persisted.set()

        async def memorize(_user_id, _prompt, _reply):
            nonlocal memory_calls
            with memory_calls_lock:
                memory_calls += 1
                call_number = memory_calls
            if call_number == 1:
                first_memory_started.set()
                while not memory_release.is_set():
                    await asyncio.sleep(0.01)

        session_id = "web-" + "b" * 36
        with (
            patch.object(self.main, "_persist_new", persist),
            patch.object(self.main, "_memory_user", return_value="analyst"),
            patch.object(self.main.memory_backend, "memorize", memorize),
        ):
            self.main._schedule_finalize(session_id, [1, 2], "p1", "r1")
            self.assertTrue(first_memory_started.wait(1))
            self.main._schedule_finalize(session_id, [1, 2, 3, 4], "p2", "r2")
            self.assertTrue(second_persisted.wait(1))
            self.assertEqual(persist_lengths, [2, 4])
            memory_release.set()
            self._wait_for(lambda: len(self.main.app.completed) == 2)

        self.assertFalse(self.main.app.active)
        self.assertEqual(memory_calls, 2)

    def test_submit_failure_completes_tracked_task(self):
        class _BrokenExecutor:
            def submit(self, *_args, **_kwargs):
                raise RuntimeError("executor closed")

        with patch.object(self.main, "_PERSIST_EXECUTOR", _BrokenExecutor()):
            self.main._schedule_finalize("web-" + "c" * 36, [], "p", "r")

        self.assertFalse(self.main.app.active)
        self.assertEqual(len(self.main.app.completed), 1)


if __name__ == "__main__":
    unittest.main()
