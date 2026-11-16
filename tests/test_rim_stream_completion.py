"""实际面板回调回归：流式全文与阶段失败说明只显示在同一个气泡中。"""
from types import SimpleNamespace
from unittest.mock import Mock
import unittest

from agent.loop import LoopAction, LoopActionKind
from tests.test_rim_panel_cancellation import panel_methods


class StreamCompletionTests(unittest.TestCase):
    def panel(self, text, action):
        panel = panel_methods("_on_stream_done")()
        panel._controller = object()
        panel._streaming_text = text
        panel._stream_replace_start = 0
        panel._reasoning_text = ""
        panel._loop = SimpleNamespace(handle_stream_done=Mock(return_value=action))
        panel._remove_streaming_bubble = Mock()
        panel._finalize_streaming_bubble = Mock()
        panel._finish_from_action = Mock()
        panel._execute_action = Mock()
        scope = panel._on_stream_done.__func__.__globals__
        scope.update(LoopActionKind=LoopActionKind, LoopAction=LoopAction, log_warning=Mock())
        return panel

    def test_streamed_failure_updates_existing_bubble(self):
        text = "结果：实体未生成。失败原因……"
        action = LoopAction(kind=LoopActionKind.FINISH, success=False,
                            summary=text + "\n\n重建未完成：截面不闭合", from_stream=False)
        panel = self.panel(text, action)
        panel._on_stream_done({"choices": [{"message": {"content": text}}]})
        panel._finalize_streaming_bubble.assert_called_once()
        panel._execute_action.assert_called_once_with(action)
        panel._finish_from_action.assert_not_called()
        self.assertEqual(panel._streaming_text, action.summary)
        self.assertEqual(panel._streaming_text.count(text), 1)
        self.assertTrue(action.from_stream)  # _finish_from_action 不再追加第二条 Agent 消息
        self.assertFalse(action.success)

    def test_nonstreamed_failure_is_appended_normally(self):
        action = LoopAction(kind=LoopActionKind.FINISH, success=False, summary="失败")
        panel = self.panel("", action)
        panel._on_stream_done({"choices": [{"message": {"content": "失败"}}]})
        panel._remove_streaming_bubble.assert_called_once()
        panel._finalize_streaming_bubble.assert_not_called()
        self.assertFalse(action.from_stream)
        panel._execute_action.assert_called_once_with(action)

    def test_success_and_tool_preamble_are_not_duplicated(self):
        for kind in (LoopActionKind.FINISH, LoopActionKind.EXECUTE_TOOLS):
            with self.subTest(kind=kind):
                action = LoopAction(kind=kind, success=True, summary="完成", from_stream=True)
                panel = self.panel("完成", action)
                panel._on_stream_done({"choices": [{"message": {"content": "完成"}}]})
                panel._finalize_streaming_bubble.assert_called_once()
                panel._execute_action.assert_called_once_with(action)


if __name__ == "__main__":
    unittest.main()
