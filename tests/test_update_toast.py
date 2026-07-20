"""ui/update_toast.py：右下角新版本浮层的状态机与点击行为。

Pilot 驱动真实 Textual 事件循环（与 test_ui.py 同一模式），不 mock 渲染。
"""

from __future__ import annotations

import unittest

from textual.app import App, ComposeResult

from pickup import i18n
from pickup.ui.update_toast import UpdateToast

i18n.set_lang("en")


def _make_app(calls: list):
    class T(App):
        def compose(self) -> ComposeResult:
            yield UpdateToast(
                on_update=lambda: calls.append("update"),
                on_restart=lambda: calls.append("restart"),
                on_retry=lambda: calls.append("retry"),
                on_dismiss=lambda v: calls.append(("dismiss", v)),
                id="update-toast",
            )

    return T()


class UpdateToastTests(unittest.IsolatedAsyncioTestCase):
    async def test_starts_hidden(self) -> None:
        calls: list = []
        app = _make_app(calls)
        async with app.run_test(size=(80, 24)) as pilot:
            toast = app.query_one(UpdateToast)
            self.assertFalse(toast.has_class("-visible"))

    async def test_show_available_reveals_toast_with_version_text(self) -> None:
        calls: list = []
        app = _make_app(calls)
        async with app.run_test(size=(80, 24)) as pilot:
            toast = app.query_one(UpdateToast)
            toast.show_available("0.21.0")
            await pilot.pause()
            self.assertTrue(toast.has_class("-visible"))
            body_text = toast.query_one("#toast-body").render().plain
            self.assertIn("0.21.0", body_text)

    async def test_click_body_in_available_state_triggers_update(self) -> None:
        calls: list = []
        app = _make_app(calls)
        async with app.run_test(size=(80, 24)) as pilot:
            toast = app.query_one(UpdateToast)
            toast.show_available("0.21.0")
            await pilot.pause()
            await pilot.click("#toast-body")
            await pilot.pause()
            self.assertEqual(calls, ["update"])

    async def test_click_close_in_available_state_dismisses_with_version(self) -> None:
        calls: list = []
        app = _make_app(calls)
        async with app.run_test(size=(80, 24)) as pilot:
            toast = app.query_one(UpdateToast)
            toast.show_available("0.22.0")
            await pilot.pause()
            await pilot.click("#toast-close")
            await pilot.pause()
            self.assertEqual(calls, [("dismiss", "0.22.0")])

    async def test_close_hitbox_hidden_outside_available_state(self) -> None:
        calls: list = []
        app = _make_app(calls)
        async with app.run_test(size=(80, 24)) as pilot:
            toast = app.query_one(UpdateToast)
            toast.show_updating()
            await pilot.pause()
            close = toast.query_one("#toast-close")
            self.assertFalse(close.display)

    async def test_click_body_while_done_triggers_restart(self) -> None:
        calls: list = []
        app = _make_app(calls)
        async with app.run_test(size=(80, 24)) as pilot:
            toast = app.query_one(UpdateToast)
            toast.show_done("0.21.0")
            await pilot.pause()
            await pilot.click("#toast-body")
            await pilot.pause()
            self.assertEqual(calls, ["restart"])

    async def test_click_body_while_failed_triggers_retry(self) -> None:
        calls: list = []
        app = _make_app(calls)
        async with app.run_test(size=(80, 24)) as pilot:
            toast = app.query_one(UpdateToast)
            toast.show_failed()
            await pilot.pause()
            await pilot.click("#toast-body")
            await pilot.pause()
            self.assertEqual(calls, ["retry"])

    async def test_click_body_while_updating_is_a_no_op(self) -> None:
        calls: list = []
        app = _make_app(calls)
        async with app.run_test(size=(80, 24)) as pilot:
            toast = app.query_one(UpdateToast)
            toast.show_updating()
            await pilot.pause()
            await pilot.click("#toast-body")
            await pilot.pause()
            self.assertEqual(calls, [])

    async def test_hide_returns_to_hidden_state(self) -> None:
        calls: list = []
        app = _make_app(calls)
        async with app.run_test(size=(80, 24)) as pilot:
            toast = app.query_one(UpdateToast)
            toast.show_available("0.21.0")
            await pilot.pause()
            toast.hide()
            await pilot.pause()
            self.assertFalse(toast.has_class("-visible"))


if __name__ == "__main__":
    unittest.main()
