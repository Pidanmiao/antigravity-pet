from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPoint, QRect, Qt
from PySide6.QtGui import QContextMenuEvent, QKeyEvent
from PySide6.QtWidgets import QApplication

from pet.renderer import Bubble
from settings.panel import PetControlPanel


def _panel() -> PetControlPanel:
    return PetControlPanel(
        on_mode=lambda _mode: None,
        on_size=lambda _size: None,
        on_feed=lambda: None,
        on_say=lambda: None,
        on_snap=lambda: None,
        on_awareness=lambda _enabled: None,
        on_read_window_title=lambda _enabled: None,
        on_idle_detection=lambda _enabled: None,
        on_topmost=lambda _enabled: None,
        on_fullscreen_hide=lambda _enabled: None,
        on_passthrough=lambda _enabled: None,
        on_autostart=lambda _enabled: None,
        on_activity_level=lambda _level: None,
        on_walk_speed=lambda _speed: None,
        on_reduced_motion=lambda _enabled: None,
        on_hover_pause=lambda _enabled: None,
        on_desktop_surfaces=lambda _enabled: None,
        on_action=lambda _action: None,
        on_hide=lambda: None,
        on_privacy=lambda: None,
        on_quit=lambda: None,
    )


class BubbleTimingTests(unittest.TestCase):
    def test_bubble_has_a_short_enter_and_exit_fade(self) -> None:
        bubble = Bubble(text="hello", appeared_at=1.0, until=3.0)
        self.assertAlmostEqual(bubble.opacity_at(1.065), 0.5, places=2)
        self.assertEqual(bubble.opacity_at(1.2), 1.0)
        self.assertAlmostEqual(bubble.opacity_at(2.9), 0.5, places=2)
        self.assertEqual(bubble.opacity_at(3.0), 0.0)


class CompactControlPanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def test_popup_stays_compact_and_avoids_pet_when_screen_has_room(self) -> None:
        panel = _panel()
        try:
            panel.sync(
                mode="wander",
                size=0.70,
                awareness=True,
                read_window_title=True,
                idle_detection=True,
                topmost=True,
                fullscreen_hide=False,
                passthrough=False,
                autostart=False,
                activity_level="balanced",
                walk_speed=82.0,
                reduced_motion=False,
                hover_pause=True,
                desktop_surfaces=True,
                state_name="待机",
            )
            area = self.application.primaryScreen().availableGeometry()
            pet = QRect(
                area.center().x() - 52,
                area.top() + max(120, area.height() // 3),
                104,
                180,
            )
            if area.width() < panel.width() + pet.width() + 42:
                self.skipTest("screen is too narrow for a non-overlapping compact popup")

            panel.popup_near_pet(pet)
            self.application.processEvents()
            self.assertLessEqual(panel.width(), 258)
            self.assertFalse(panel.geometry().intersects(pet))

            collapsed_height = panel.height()
            panel._toggle_advanced()
            self.application.processEvents()
            self.assertGreater(panel.height(), collapsed_height)
            panel._toggle_advanced()
            self.application.processEvents()
            self.assertLessEqual(panel.height(), collapsed_height)
        finally:
            panel.hide()

    def test_escape_and_right_click_close_control_panel(self) -> None:
        panel = _panel()
        try:
            panel.popup_at(QPoint(120, 120))
            self.application.processEvents()
            self.assertTrue(panel.isVisible())

            esc = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier)
            panel.keyPressEvent(esc)
            self.application.processEvents()
            self.assertTrue(esc.isAccepted())
            self.assertFalse(panel.isVisible())

            panel.popup_at(QPoint(120, 120))
            self.application.processEvents()
            self.assertTrue(panel.isVisible())

            ctx = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, QPoint(10, 10), QPoint(130, 130))
            panel.contextMenuEvent(ctx)
            self.application.processEvents()
            self.assertTrue(ctx.isAccepted())
            self.assertFalse(panel.isVisible())
        finally:
            panel.hide()

    def test_hud_visibility_dropdown_reveals_delay_only_in_auto_mode(self) -> None:
        seen_vis: list[str] = []
        seen_delay: list[int] = []
        panel = PetControlPanel(
            on_mode=lambda _mode: None,
            on_size=lambda _size: None,
            on_feed=lambda: None,
            on_say=lambda: None,
            on_snap=lambda: None,
            on_awareness=lambda _enabled: None,
            on_read_window_title=lambda _enabled: None,
            on_idle_detection=lambda _enabled: None,
            on_topmost=lambda _enabled: None,
            on_fullscreen_hide=lambda _enabled: None,
            on_passthrough=lambda _enabled: None,
            on_autostart=lambda _enabled: None,
            on_activity_level=lambda _level: None,
            on_walk_speed=lambda _speed: None,
            on_reduced_motion=lambda _enabled: None,
            on_hover_pause=lambda _enabled: None,
            on_desktop_surfaces=lambda _enabled: None,
            on_action=lambda _action: None,
            on_hide=lambda: None,
            on_privacy=lambda: None,
            on_quit=lambda: None,
            on_hud_visibility=seen_vis.append,
            on_hud_auto_hide_seconds=seen_delay.append,
        )
        try:
            panel.popup_at(QPoint(120, 120))
            self.application.processEvents()
            height_show = panel.height()
            self.assertTrue(panel._hud_delay_row.isHidden())

            panel.hud_visibility.setCurrentIndex(panel.hud_visibility.findData("auto"))
            self.application.processEvents()
            self.assertEqual(seen_vis, ["auto"])
            self.assertFalse(panel._hud_delay_row.isHidden())
            self.assertEqual(panel.height(), height_show)

            panel.hud_delay.setValue(9)
            self.application.processEvents()
            self.assertEqual(seen_delay, [9])

            panel.hud_visibility.setCurrentIndex(panel.hud_visibility.findData("hide"))
            self.application.processEvents()
            self.assertEqual(seen_vis, ["auto", "hide"])
            self.assertTrue(panel._hud_delay_row.isHidden())
        finally:
            panel.hide()


if __name__ == "__main__":
    unittest.main()
