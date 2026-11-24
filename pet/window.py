from __future__ import annotations

import ctypes
import os
import random
import subprocess
import sys
import json
import time
from collections import deque
from pathlib import Path

from PySide6.QtCore import QPoint, QRect, QTimer, Qt
from PySide6.QtGui import QColor, QIcon, QImage
from PySide6.QtWidgets import (
    QApplication,
    QMenu,
    QMessageBox,
    QSystemTrayIcon,
    QWidget,
)

from app.paths import AppPaths
from app.recovery import RecoveryStatus
from agy.autonomy import AgyAutonomy, AutonomyCommand, AutonomyInput, WindowTarget, plan_hop
from agy.characters import FORMS, FORM_IDS, form_by_id, normalize_form
from agy.hud_bar import HudBar
from agy.reactions import ReactionTracker
from agy.service import TelemetryService
from agy.telemetry import TelemetryCollector
from agy.token_meter import TokenMeterWindow
from agy.windows import AntigravityWindowFinder
from awareness.privacy import PrivacyPolicy
from awareness.sensor import ContextSensor
from animation.asset_registry import EDGE_ASSETS, AssetRegistry
from animation.expressions import CALM, ExpressionDirector, ExpressionInput
from animation.clip import PetAction
from animation.player import AnimationPlayer
from animation.state_machine import ActionStateMachine
from behavior.activity_director import (
    ActivityCommand,
    ActivityCueType,
    ActivityDirector,
    MicroMotionDirector,
)
from behavior.behavior_engine import BehaviorEngine
from behavior.classifier import AppClassifier
from behavior.cooldown import Cooldown
from behavior.events import EventType
from behavior.scheduler import ContextEventScheduler
from dialogue.local_rules import DialogueManager
from dialogue.personality import normalize_personality
from pet.climb import JumpPlan, pick_jump, uncovered_top_span, ClimbController, Rect as ClimbRect, is_exposed, plan_climb
from pet.interaction import FoodPanel, InteractionController, parse_food_menu
from pet.movement import MovementController
from pet.physics import PhysicsBounds, ThrowPhysics
from pet.renderer import Bubble, PetRenderer
from pet.state import PetStateStore
from pet.surfaces import DesktopSurfaceTracker
from settings.config import ConfigManager, clamp_hud_auto_hide_seconds, normalize_hud_visibility
from settings.panel import PetControlPanel


BUBBLE_HEIGHT = 66
MARGIN = 4
BASE_SPRITE_HEIGHT = 340
SIZE_LEVELS = {"小": 0.55, "中": 0.70, "大": 0.90}
TICK_MS = 20

# Bubble name-tag colour per form (readable on the white bubble).
ACCENTS = {"carbon": "#4b5563", "hydergon": "#2b8fd6", "neon": "#c026d3", "argon": "#8b5cf6", "claude": "#ea580c"}
# Agents that bring their own form when they report through the control API.

STATE_NAMES = {
    PetAction.IDLE: "待机",
    PetAction.BLINK: "眨眨眼",
    PetAction.GLANCE: "四处看看",
    PetAction.THINKING: "发呆",
    PetAction.WALKING: "散步",
    PetAction.HAPPY: "开心",
    PetAction.HEAD_PAT: "被摸摸",
    PetAction.TALKING: "说话",
    PetAction.ANGRY: "生气",
    PetAction.POKE_REACT: "被戳",
    PetAction.TAIL_REACT: "尾巴被碰",
    PetAction.EATING: "吃东西",
    PetAction.SWEEPING: "扫地",
    PetAction.SLEEPING: "睡觉",
    PetAction.DRAGGING: "抓取中",
    PetAction.FALLING: "落下",
    PetAction.LANDING: "着陆",
    PetAction.DIZZY: "眩晕",
    PetAction.WORKING: "陪你干活",
    PetAction.CELEBRATE: "庆祝",
    PetAction.TIRED: "没精神",
    PetAction.STRETCH: "伸懒腰",
    PetAction.TRANSFORM: "变身中",
    PetAction.WAVE: "打招呼",
    PetAction.PERCH: "坐在窗口上",
    PetAction.CLIMBING: "爬窗户",
}


class PetWindow(QWidget):
    """The transparent desktop pet window and the wiring between its modules."""

    def __init__(
        self,
        paths: AppPaths,
        config: ConfigManager,
        *,
        recovery_status: RecoveryStatus | None = None,
    ) -> None:
        flags = Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
        if config.get("topmost", True):
            flags |= Qt.WindowType.WindowStaysOnTopHint
        super().__init__(None, flags)
        self.paths = paths
        self.config = config
        self.recovery_status = recovery_status or RecoveryStatus()
        self.rng = random.Random()
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowTitle("AGY 桌宠")

        self.current_height = self._height_for_size(
            self._normalized_size(config.get("size", 0.7))
        )
        self.form_id = normalize_form(self._agy().get("form"))
        self._pending_form: str | None = None
        self.asset_registry = self._load_registry(self.form_id)
        self.action_machine = ActionStateMachine(self.asset_registry.specs)
        self.player = AnimationPlayer()
        self._resize_window()

        comfort = config.section("comfort")
        self.movement = MovementController(
            config.get("mode", "wander"),
            speed=float(comfort.get("walk_speed", 82.0)),
            rng=self.rng,
        )
        self.physics = ThrowPhysics()
        self.climb = ClimbController()
        self._explore_next = time.monotonic() + 90.0
        self._perched_since: float | None = None
        self._descend_x: float | None = None
        self._explored_handle: int | None = None
        self._gentle_landing = False
        self._climb_pos = (0.0, 0.0)
        self._perch_after_landing = False
        self._hop_token = 0  # guards the delayed set_target(None) of older hops
        self._roam_next = 0.0  # next stroll / jump while standing on a window
        self._top_bounds_cache: tuple[int, float, tuple[float, float] | None] | None = None
        self._climb_metrics_cache: dict[tuple, tuple[float, float] | None] = {}
        self.interaction = InteractionController()
        self.renderer = PetRenderer(BUBBLE_HEIGHT, MARGIN)
        self.renderer.set_reduced_motion(bool(comfort.get("reduced_motion", False)))
        self.renderer.set_motion_scale(float(comfort.get("motion_scale", 0.4)))
        self.expressions = ExpressionDirector()
        self.surface_tracker = DesktopSurfaceTracker(
            enabled=bool(comfort.get("desktop_surfaces", True))
        )
        self.bubble = Bubble()
        self._last_line = ""
        self._last_tick_at = time.monotonic()
        self._last_state_update = self._last_tick_at
        self._drag_offset: QPoint | None = None
        self._drag_last_pos: QPoint | None = None
        self._caught_throw = False
        self._pending_click_region = "body"
        self._hovered = False
        self._recent_pokes: deque[float] = deque(maxlen=8)
        self._recent_feedings: deque[float] = deque(maxlen=6)

        self.dialogue = DialogueManager(paths.assets_dir / "dialogue", self.rng)
        self._apply_form_voice(self.form_id)
        self.state_store = PetStateStore(paths.state_path)
        self.pet_state = self.state_store.load()
        self._first_session_today = self.pet_state.begin_session()
        activity_level = str(comfort.get("activity_level", "balanced"))
        self.activity_director = ActivityDirector(self.rng, activity_level=activity_level)
        self.micro_motion_director = MicroMotionDirector(
            self.rng,
            activity_level=activity_level,
        )

        self._click_timer = QTimer(self)
        self._click_timer.setSingleShot(True)
        self._click_timer.timeout.connect(self._do_click_reaction)
        self._hold_timer = QTimer(self)
        self._hold_timer.setSingleShot(True)
        self._hold_timer.timeout.connect(self._do_long_press_reaction)
        self._long_press_handled = False
        self.food_panel = FoodPanel(self.on_food)
        self.food_panel.set_menu(
            parse_food_menu(self._form_voice.get("food_menu")), ACCENTS.get(self.form_id, "#339ad5")
        )
        self.control_panel = PetControlPanel(
            on_mode=self.set_mode,
            on_size=self.set_size,
            on_feed=self._open_food_panel,
            on_say=self._say_daily_line,
            on_snap=self.snap_into_screen,
            on_awareness=self.set_awareness_enabled,
            on_read_window_title=lambda enabled: self._set_awareness_flag("read_window_title", enabled),
            on_idle_detection=lambda enabled: self._set_awareness_flag("idle_detection", enabled),
            on_topmost=self.set_topmost,
            on_fullscreen_hide=lambda enabled: self._set_awareness_flag("hide_on_fullscreen", enabled),
            on_passthrough=self.set_passthrough,
            on_autostart=self.set_autostart,
            on_activity_level=self.set_activity_level,
            on_walk_speed=self.set_walk_speed,
            on_reduced_motion=self.set_reduced_motion,
            on_hover_pause=self.set_hover_pause,
            on_desktop_surfaces=self.set_desktop_surfaces,
            on_action=self._preview_action,
            on_hide=self._hide_from_panel,
            on_privacy=self._show_privacy_notice,
            on_quit=self.quit_app,
            on_form=lambda form: self.set_form(form, manual=True),
            on_follow_model=self.set_follow_model,
            on_open_meter=self.open_token_meter,
            on_hud=self.set_hud_enabled,
            on_hud_visibility=self.set_hud_visibility,
            on_hud_auto_hide_seconds=self.set_hud_auto_hide_seconds,
            on_autonomy=self.set_autonomy,
            forms=tuple((form.id, form.name) for form in FORMS),
        )
        self._create_tray()
        self._create_agy()

        self._create_awareness()
        self._sync_animation_clip(crossfade=False)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(TICK_MS)

        self.persistence_timer = QTimer(self)
        self.persistence_timer.timeout.connect(self.save_snapshot)
        self.persistence_timer.start(30_000)

        self._restore_position()
        self.show()
        self.snap_into_screen()
        self._sync_awareness_timer()
        self._sync_surface_timer()
        QTimer.singleShot(1_600, self._show_startup_recovery_line)
        self._sync_hud_visibility()

    # ----- window and rendering -------------------------------------------------

    @staticmethod
    def _height_for_size(multiplier: float) -> int:
        return round(BASE_SPRITE_HEIGHT * float(multiplier))

    @staticmethod
    def _normalized_size(value: object) -> float:
        try:
            candidate = float(value)
        except (TypeError, ValueError):
            return 0.70
        for multiplier in SIZE_LEVELS.values():
            if abs(candidate - multiplier) < 0.01:
                return multiplier
        return 0.70

    def _resize_window(self) -> None:
        widths = []
        for direction in ("left", "right", "up", "down"):
            # Special poses such as sleeping and being held can legitimately be
            # wider than the standing sprite.  Reserve their width up front so
            # switching action never crops arms, tail or the lower body.
            for action in PetAction:
                clip = self.asset_registry.clip_for(
                    action,
                    height=self.current_height,
                    direction=direction,
                )
                widths.extend(frame.pixmap.width() for frame in clip.frames)
        if not widths:
            raise RuntimeError("当前桌宠尺寸没有可用的精灵图。")
        horizontal_margin = int(self.current_height * 0.062) + 6
        self.setFixedSize(
            max(widths) + horizontal_margin * 2,
            self.current_height + BUBBLE_HEIGHT + MARGIN * 2 + 10,
        )

    def _restore_position(self) -> None:
        x, y = self.config.get("x"), self.config.get("y")
        if isinstance(x, int) and isinstance(y, int):
            self.move(x, y)
            return
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        geometry = screen.availableGeometry()
        self.move(
            geometry.right() - self.width() + 1 - 80,
            geometry.bottom() - self.height() + 1 - 60,
        )

    def _physics_bounds(self, lookahead_seconds: float = 0.0) -> PhysicsBounds:
        if self.physics.active:
            center_x, center_y = self.physics.projected_center(
                lookahead_seconds,
                self.width(),
                self.height(),
            )
            probe = QPoint(round(center_x), round(center_y))
        else:
            probe = self.frameGeometry().center()
        screen = QApplication.screenAt(probe) or self.screen() or QApplication.primaryScreen()
        if screen is None:
            return PhysicsBounds(0.0, 0.0, float(self.width()), float(self.height()))
        geometry = screen.availableGeometry()
        work_areas = [candidate.availableGeometry() for candidate in QApplication.screens()]
        left = min((area.left() for area in work_areas), default=geometry.left())
        right = max((area.right() for area in work_areas), default=geometry.right())
        top = min((area.top() for area in work_areas), default=geometry.top())
        return PhysicsBounds(
            float(left),
            float(top),
            float(right),
            float(geometry.bottom()),
            platforms=self.surface_tracker.physics_platforms(
                work_area_top=float(top),
                widget_height=float(self.height()),
            ),
        )

    def _launch_throw(self, velocity_x: float, velocity_y: float, *, reason: str) -> None:
        self.surface_tracker.detach()
        self.movement.rest_after_drag()
        self.physics.launch(self.x(), self.y(), velocity_x, velocity_y)
        self._request_action(
            PetAction.FALLING,
            reason=reason,
            force=True,
            immediate=True,
        )

    def paintEvent(self, _event) -> None:
        self._update_expression()
        self.renderer.paint(
            self,
            snapshot=self.player.snapshot(),
            bubble=self.bubble,
        )

    _EXPRESSION_CUES = {
        PetAction.HAPPY: "happy",
        PetAction.CELEBRATE: "happy",
        PetAction.ANGRY: "pout",
        PetAction.DIZZY: "worried",
    }

    def _update_expression(self) -> None:
        """Pick the idle face and hand it to the renderer (idle frame only)."""
        registry = getattr(self, "asset_registry", None)
        if not bool(self.config.section("comfort").get("expressions", True)) or registry is None:
            self.renderer.expression = None
            return
        if not registry.expression_names():
            self.renderer.expression = None
            return
        now = time.monotonic()
        telemetry = getattr(self, "telemetry", None)
        snapshot = telemetry.last_snapshot if telemetry is not None else None
        state = self.pet_state
        self.expressions.update(
            ExpressionInput(
                now=now,
                hovered=self._hovered,
                running=bool(snapshot is not None and snapshot.running),
                quota_pct=getattr(snapshot, "quota_remaining_pct", None),
                mood=state.mood,
                energy=state.energy,
                annoyance=state.annoyance,
                user_idle_seconds=getattr(self, "_last_user_idle", None),
            )
        )
        idle_asset = registry.resolve_asset_id(registry.specs[PetAction.IDLE].asset_id)
        layer = self.player.snapshot().current
        height = layer.frame.pixmap.height() if layer is not None and layer.asset_id == idle_asset else 238

        def face(name: str):
            return None if name == CALM else registry.expression_pixmap(name, height)

        director = self.expressions
        self.renderer.expression = (
            idle_asset,
            face(director.current),
            face(director.previous),
            director.fade(now),
        )

    def _set_direction(self, direction: str, facing: int | None = None) -> None:
        if self.movement.set_direction(direction, facing):
            self._sync_animation_clip()

    def _sync_animation_clip(
        self,
        *,
        crossfade: bool = True,
        immediate: bool = False,
    ) -> None:
        current = self.action_machine.current
        direction = self.movement.direction
        if current in EDGE_ASSETS and self.surface_tracker.attached:
            direction = "edge"
        elif current == PetAction.CLIMBING and self.climb.plan is not None:
            direction = self.climb.plan.facing
        program = self.asset_registry.program_for(
            current,
            height=self.current_height,
            direction=direction,
        )
        self.player.play(program, crossfade=crossfade, immediate=immediate)
        # Sitting over the edge sits lower than standing on it: re-place now.
        self.surface_tracker.refresh_soon()
        self._refresh_control_panel()

    def _request_action(
        self,
        action: PetAction,
        *,
        now: float | None = None,
        reason: str,
        force: bool = False,
        immediate: bool = False,
    ) -> bool:
        now = time.monotonic() if now is None else now
        change = self.action_machine.request(action, now=now, reason=reason, force=force)
        if change is None:
            return False
        if self._edge_change(change):
            # Sitting over the edge sits lower: cut, don't crossfade, or the
            # outgoing pose shows sunk into (or floating above) the window.
            immediate = True
            spec = self.action_machine.specs.get(change.current)
            if change.previous in EDGE_ASSETS and spec is not None and not spec.loop:
                # A poke, a word, a blink: then back to sitting on the edge.
                self.action_machine.queue_after_current(change.previous)
        cue = self._EXPRESSION_CUES.get(action)
        if cue and hasattr(self, "expressions"):
            self.expressions.cue(cue, now)
        self._sync_animation_clip(immediate=immediate)
        return True

    def _refresh_control_panel(self) -> None:
        awareness = self.config.section("awareness")
        comfort = self.config.section("comfort")
        self.control_panel.sync(
            mode=self.movement.mode,
            size=self._normalized_size(self.config.get("size", 0.70)),
            awareness=bool(awareness.get("enabled", True)),
            read_window_title=bool(awareness.get("read_window_title", True)),
            idle_detection=bool(awareness.get("idle_detection", True)),
            topmost=bool(self.config.get("topmost", True)),
            fullscreen_hide=bool(awareness.get("hide_on_fullscreen", False)),
            passthrough=bool(self.config.get("passthrough", False)),
            autostart=bool(self.config.get("autostart", False)),
            activity_level=str(comfort.get("activity_level", "balanced")),
            walk_speed=float(comfort.get("walk_speed", 82.0)),
            reduced_motion=bool(comfort.get("reduced_motion", False)),
            hover_pause=bool(comfort.get("hover_pause", True)),
            desktop_surfaces=bool(comfort.get("desktop_surfaces", True)),
            state_name=self._display_state_name(),
            form=self.form_id,
            follow_model=bool(self._agy().get("follow_model", False)),
            hud_enabled=bool(self._agy().get("hud_enabled", True)),
            hud_visibility=self._hud_visibility(),
            hud_auto_hide_seconds=self._hud_auto_hide_seconds(),
            autonomy=bool(self._agy().get("autonomy", True)),
        )

    def _display_state_name(self) -> str:
        if self.action_machine.current != PetAction.IDLE:
            return STATE_NAMES.get(self.action_machine.current, "待机")
        return {
            "tired": "有点困",
            "annoyed": "闹脾气",
            "restless": "想走走",
            "cheerful": "心情很好",
            "affectionate": "很亲近",
            "steady": "待机",
        }.get(self.pet_state.visible_condition(), "待机")

    def _open_food_panel(self) -> None:
        self._interrupt_activity()
        self.food_panel.popup_at(
            self.x() + self.width() / 2,
            self.y() + BUBBLE_HEIGHT,
        )

    def _say_daily_line(self) -> None:
        self.say(self.dialogue.pick_daily(personality=self._personality()))

    def _preview_action(self, raw_action: str) -> None:
        if raw_action in {"CLIMB", "EDGE_SIT"}:
            self._panel_climb(sit_after=raw_action == "EDGE_SIT")
            return
        try:
            action = PetAction.coerce(raw_action)
        except ValueError:
            return
        self._interrupt_activity()
        if action == PetAction.SLEEPING:
            self.movement.rest_after_drag()
        self.physics.cancel()
        self._request_action(action, reason="panel_preview", force=True, immediate=True)

    def _hide_from_panel(self) -> None:
        self.control_panel.hide()
        self.hide()

    # ----- main animation loop --------------------------------------------------

    def tick(self) -> None:
        now = time.monotonic()
        delta_seconds = min(0.10, max(0.001, now - self._last_tick_at))
        self._last_tick_at = now
        self.player.tick(delta_seconds)
        change = self.action_machine.update(now=now)
        if change is not None:
            self._sync_animation_clip(immediate=self._edge_change(change))
        if not self.physics.active and not self.interaction.active:
            attachment = self.surface_tracker.follow_attachment(
                now=now,
                widget_x=float(self.x()),
                widget_width=float(self.width()),
                widget_height=float(self.height()),
                sink=self._edge_sit_sink(),
            )
            if attachment is not None:
                if attachment.lost:
                    self._launch_throw(0.0, 0.0, reason="surface_lost")
                    self._schedule_routine_line("window_lost", delay_ms=900)
                else:
                    self.move(round(attachment.x), round(attachment.y))
        walking = False
        movable_actions = {PetAction.IDLE, PetAction.THINKING, PetAction.WALKING}
        cue = self.activity_director.current_cue
        activity_allows_movement = (
            not self.activity_director.active
            or (cue is not None and cue.kind == ActivityCueType.WALK)
        )
        hover_paused = self._hovered and bool(
            self.config.section("comfort").get("hover_pause", True)
        )
        if self.physics.active:
            self._tick_throw(delta_seconds, now)
        elif self.climb.active and not self.interaction.active:
            walking = self._tick_climb(delta_seconds, now)
        elif self._descend_x is not None and not self.interaction.active:
            walking = self._tick_descend(delta_seconds, now)
        elif (
            not self.interaction.active
            and not hover_paused
            and activity_allows_movement
            and self.action_machine.current in movable_actions
        ):
            walking = self.movement.update(
                self,
                now=now,
                delta_seconds=delta_seconds,
                set_direction=self._set_direction,
                energy=self.pet_state.energy,
                horizontal_bounds=self._top_bounds(now),
            )
            if self.surface_tracker.attached:
                self.surface_tracker.record_pet_position(
                    widget_x=float(self.x()),
                    widget_width=float(self.width()),
                )
        if walking:
            self._request_action(PetAction.WALKING, now=now, reason="movement")
        elif not self.interaction.dragging and self.action_machine.current == PetAction.WALKING:
            self._request_action(PetAction.IDLE, now=now, reason="movement_stop", force=True)
        self._tick_activity(now)
        self._tick_micro_motion(now)
        if self._pending_form and not self.physics.active and not self.interaction.active and not self.climb.active:
            pending, self._pending_form = self._pending_form, None
            self.set_form(pending, announce=False)
        self._follow_hud()
        if now >= getattr(self, "_next_config_poll", 0.0):
            self._next_config_poll = now + 1.5
            self._sync_external_config()
        self._tick_autonomy(now)
        self._tick_explore(now)
        if now - self._last_state_update >= 5.0:
            self.pet_state.advance(
                now - self._last_state_update,
                activity=self.action_machine.current.value.lower(),
            )
            self._last_state_update = now
        self.update()

    def _tick_throw(self, delta_seconds: float, now: float) -> None:
        step = self.physics.step(
            delta_seconds,
            width=self.width(),
            height=self.height(),
            bounds=self._physics_bounds(delta_seconds),
        )
        self.move(round(step.x), round(step.y))
        if not step.settled:
            return

        self._settle_landing(step.landed_surface_id, hard=step.hard_landing, now=now)

    def _settle_landing(self, surface_id: str | None, *, hard: bool, now: float, quiet: bool = False) -> None:
        """Feet down after a throw, a hop or a climb: attach, land, maybe sit."""
        self.movement.rest_after_drag(now)
        self._set_direction("down")
        if surface_id:
            self.surface_tracker.attach(
                surface_id,
                widget_x=float(self.x()),
                widget_width=float(self.width()),
            )
        else:
            self.surface_tracker.detach()
        hard = hard and not self._gentle_landing
        self._gentle_landing = False
        self._top_bounds_cache = None
        if surface_id:
            self._roam_next = now + self.rng.uniform(25.0, 50.0)
        if self._request_action(
            PetAction.LANDING,
            now=now,
            reason="hard_landing" if hard else "landing",
            force=True,
            immediate=True,
        ) and hard:
            self.action_machine.queue_after_current(PetAction.DIZZY)
        if surface_id and self._perch_after_landing:
            self.action_machine.queue_after_current(PetAction.PERCH)
        self._perch_after_landing = False
        if quiet:
            return
        if surface_id:
            self._schedule_routine_line("window_land", delay_ms=1_100)
        elif hard:
            self._schedule_routine_line("hard_landing", delay_ms=2_200)
        else:
            self._schedule_routine_line("soft_landing", delay_ms=1_000)

    def _tick_activity(self, now: float) -> None:
        hover_paused = self._hovered and bool(
            self.config.section("comfort").get("hover_pause", True)
        )
        command = self.activity_director.update(
            now=now,
            state=self.pet_state,
            can_start=(
                self.action_machine.current == PetAction.IDLE
                and not self.interaction.active
                and not self.movement.is_walking
                and not self.control_panel.isVisible()
                and not self.food_panel.isVisible()
                and not hover_paused
            ),
            is_walking=self.movement.is_walking,
            allow_movement=self.movement.mode == "wander",
        )
        if command is not None:
            self._execute_activity_command(command, now)

    def _tick_micro_motion(self, now: float) -> None:
        comfort = self.config.section("comfort")
        hover_paused = self._hovered and bool(comfort.get("hover_pause", True))
        action = self.micro_motion_director.update(
            now=now,
            state=self.pet_state,
            can_start=(
                self.action_machine.current == PetAction.IDLE
                and not self.activity_director.active
                and not self.interaction.active
                and not self.movement.is_walking
                and not self.physics.active
                and not self.control_panel.isVisible()
                and not self.food_panel.isVisible()
                and not hover_paused
            ),
            reduced_motion=bool(comfort.get("reduced_motion", False)),
        )
        if action is not None:
            self._request_action(action, now=now, reason="idle_micro_motion")

    def _execute_activity_command(self, command: ActivityCommand, now: float) -> None:
        reason = f"activity_{command.activity.value}"
        if command.cue_type == ActivityCueType.COMPLETE:
            self.pet_state.finish_activity(command.activity.value)
            return
        if command.cue_type == ActivityCueType.WALK:
            started = self.movement.request_short_wander(
                self,
                body_widths=command.distance_body_widths,
                now=now,
                away_from_cursor=command.away_from_cursor,
                horizontal_bounds=self._top_bounds(now),
            )
            if not started:
                self.activity_director.skip_walk(now=now)
            return
        if command.cue_type == ActivityCueType.SPEAK:
            personality = self._personality()
            condition = self.pet_state.visible_condition()
            line = None
            if condition != "steady" and self.rng.random() < 0.72:
                line = self.dialogue.pick_state(condition, personality=personality)
            if line is None:
                line = (
                    self.dialogue.pick_inner_voice(personality=personality)
                    if command.inner
                    else self.dialogue.pick_daily(personality=personality)
                )
            self.say(line, inner=command.inner, from_activity=True)
            return
        if command.action is None:
            self.activity_director.interrupt(now=now)
            return
        changed = self._request_action(command.action, now=now, reason=reason)
        if not changed and self.action_machine.current != command.action:
            self.activity_director.interrupt(now=now)

    def _interrupt_activity(self, *, delay_seconds: float | None = None) -> None:
        now = time.monotonic()
        interrupted = self.activity_director.interrupt(
            now=now,
            delay_seconds=delay_seconds,
        )
        self.micro_motion_director.defer(now, minimum_seconds=3.0)
        self.movement.cancel_target()
        if interrupted is not None and self.action_machine.current == PetAction.SLEEPING:
            self._request_action(PetAction.IDLE, reason="activity_interrupted", force=True)

    def say(
        self,
        text: str | None,
        *,
        inner: bool = False,
        from_activity: bool = False,
        preserve_action: bool = False,
    ) -> bool:
        if not from_activity:
            self._interrupt_activity()
        if not text or text == self._last_line:
            return False
        self._last_line = text
        if not preserve_action:
            action = PetAction.THINKING if inner else PetAction.TALKING
            self._request_action(action, reason="inner_voice" if inner else "speech")
        now = time.monotonic()
        display_seconds = min(5.4, max(2.9, 1.9 + len(text) * 0.18))
        self.bubble = Bubble(
            text=f"（{text}）" if inner else text,
            until=now + display_seconds,
            appeared_at=now,
            inner=inner,
            state_name=STATE_NAMES.get(self.action_machine.current, ""),
        )
        self.update()
        return True

    def _schedule_routine_line(self, situation: str, *, delay_ms: int) -> None:
        def speak() -> None:
            line = self.dialogue.pick_routine(
                situation,
                personality=self._personality(),
            )
            self.say(line)

        QTimer.singleShot(max(0, int(delay_ms)), speak)

    def _show_startup_recovery_line(self) -> None:
        if self.recovery_status.safe_mode:
            situation = "safe_mode"
        elif self.recovery_status.unclean_exit:
            situation = "recovered"
        elif self._first_session_today:
            situation = "first_today"
        else:
            line = self.dialogue.pick_relationship(
                self.pet_state.affinity,
                personality=self._personality(),
            )
            self.say(line)
            return
        self.say(self.dialogue.pick_routine(situation, personality=self._personality()))

    def save_snapshot(self) -> None:
        """Atomically persist the small recoverable state during long sessions."""
        self._sync_external_config()
        self.config.set("x", self.x(), save=False)
        self.config.set("y", self.y(), save=False)
        self.config.save()
        self.state_store.save(self.pet_state)

    # ----- user interaction -----------------------------------------------------

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        if hasattr(self, "hud_bar"):
            self._reveal_hud_temporarily(start_countdown=True)
        self._interrupt_activity()
        position = event.globalPosition().toPoint()
        self._descend_x = None
        if self.climb.active:
            # Grabbed off the wall: treat it like catching her mid-air.
            self.climb.cancel()
            self._launch_throw(0.0, 0.0, reason="climb_grabbed")
        self._caught_throw = self.physics.active
        if self._caught_throw:
            self.physics.cancel()
            self.movement.cancel_target()
            self._request_action(
                PetAction.DRAGGING,
                reason="midair_catch",
                force=True,
                immediate=True,
            )
        self._drag_last_pos = position
        self._drag_offset = None
        self._long_press_handled = False
        self.interaction.press(
            position,
            event.position().x(),
            event.position().y(),
            0.0,
            BUBBLE_HEIGHT + MARGIN,
            self.width(),
            self.current_height,
        )
        if self.interaction.body_region in {"head", "face"}:
            self._hold_timer.start(620)

    def mouseMoveEvent(self, event) -> None:
        if not event.buttons() & Qt.MouseButton.LeftButton:
            return
        position = event.globalPosition().toPoint()
        if not self.interaction.move(position):
            return
        if self._drag_offset is None:
            self._click_timer.stop()
            self._hold_timer.stop()
            self._drag_offset = position - QPoint(self.x(), self.y())
            self.physics.cancel()
            self.surface_tracker.detach()
            self._request_action(
                PetAction.DRAGGING,
                reason="drag_start",
                force=True,
                immediate=True,
            )
        self.move(position - self._drag_offset)
        if self._drag_last_pos is not None:
            delta = position - self._drag_last_pos
            if abs(delta.x()) > 10:
                self._set_direction("left" if delta.x() < 0 else "right")
        self._drag_last_pos = position
        self.update()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        outcome = self.interaction.release(event.globalPosition().toPoint())
        self._hold_timer.stop()
        caught_throw = self._caught_throw
        self._caught_throw = False
        self._drag_offset = None
        self._drag_last_pos = None
        if outcome is None:
            return
        if outcome.kind == "drag":
            self.pet_state.drag()
            throw_speed = (
                outcome.velocity_x * outcome.velocity_x + outcome.velocity_y * outcome.velocity_y
            ) ** 0.5
            self._launch_throw(
                outcome.velocity_x,
                outcome.velocity_y,
                reason="fast_release" if throw_speed >= 900.0 else "drag_release",
            )
            drag_key = "drag_fast" if throw_speed >= 900.0 else "drag_gentle"
            self.say(
                self.dialogue.pick_interaction(drag_key, personality=self._personality())
                or self.dialogue.pick_interaction("drag", personality=self._personality()),
                preserve_action=True,
            )
            return
        if caught_throw:
            self.pet_state.drag()
            self._launch_throw(0.0, 0.0, reason="caught_release")
            return
        if self._long_press_handled:
            self._long_press_handled = False
            return
        self._pending_click_region = outcome.body_region
        self._click_timer.start(280)

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self._click_timer.stop()
        self._hold_timer.stop()
        self.food_panel.popup_at(
            self.x() + self.width() / 2,
            self.y() + BUBBLE_HEIGHT,
        )

    def _do_click_reaction(self) -> None:
        region = self._pending_click_region
        personality = self._personality()
        if region == "head":
            self.pet_state.pet_head()
            self._request_action(PetAction.HEAD_PAT, reason="head_pat")
            self.say(
                self.dialogue.pick_interaction("head", personality=personality),
                preserve_action=True,
            )
            return

        now = time.monotonic()
        self._recent_pokes.append(now)
        recent_count = sum(1 for timestamp in self._recent_pokes if now - timestamp <= 5.0)
        intensity = {"face": 1.15, "body": 1.0, "tail": 2.0}.get(region, 1.0)
        self.pet_state.tap(intensity)
        if recent_count >= 3:
            self._request_action(PetAction.ANGRY, reason="repeated_poke")
            self.say(
                self.dialogue.pick_routine("repeated_poke", personality=personality),
                preserve_action=True,
            )
            return
        if region == "tail":
            self._request_action(PetAction.TAIL_REACT, reason="tail_touch")
            self.say(
                self.dialogue.pick_routine("tail_touch", personality=personality),
                preserve_action=True,
            )
            return
        reaction = PetAction.ANGRY if self.pet_state.annoyance >= 0.26 else PetAction.POKE_REACT
        self._request_action(reaction, reason=f"poke_{region}")
        self.say(
            self.dialogue.pick_interaction(region, personality=personality)
            or self.dialogue.pick_interaction("tap", personality=personality),
            preserve_action=True,
        )

    def _do_long_press_reaction(self) -> None:
        if not self.interaction.active or self.interaction.dragging:
            return
        if self.interaction.body_region not in {"head", "face"}:
            return
        self._long_press_handled = True
        self.pet_state.pet_head()
        self._request_action(PetAction.HEAD_PAT, reason="long_head_pat")
        self.say(
            self.dialogue.pick_interaction("head_hold", personality=self._personality())
            or self.dialogue.pick_interaction("head", personality=self._personality()),
            preserve_action=True,
        )

    def on_food(self, food: str) -> None:
        """Feed her ``food`` (an id from the current form's menu)."""
        self._interrupt_activity()
        self.food_panel.hide()
        item = self.food_panel.item(food)
        taste = item.taste if item is not None else "normal"
        self.pet_state.feed(taste)
        own_lines = self._personality() == "standard" and item is not None and bool(item.lines)
        if taste == "dislike":
            # Not her thing: she leans away instead of eating it.
            self._request_action(PetAction.POKE_REACT, reason="feed_dislike", force=True)
            if own_lines:
                line = self.rng.choice(item.lines)
            else:
                line = self.dialogue.pick_routine("repeated_feed", personality=self._personality())
            self.say(line, preserve_action=True)
            return
        now = time.monotonic()
        self._recent_feedings.append(now)
        self._request_action(PetAction.EATING, reason="feed")
        recent_count = sum(1 for timestamp in self._recent_feedings if now - timestamp <= 20.0)
        if recent_count >= 3:
            line = self.dialogue.pick_routine("repeated_feed", personality=self._personality())
        else:
            if taste == "favorite":
                self.action_machine.queue_after_current(PetAction.HAPPY)
            generic = self.dialogue.pick_interaction("food", personality=self._personality())
            if own_lines and (taste == "favorite" or self.rng.random() < 0.6 or not generic):
                line = self.rng.choice(item.lines)
            else:
                line = generic
        self.say(line, preserve_action=True)

    # ----- local environment awareness -----------------------------------------

    def _create_awareness(self) -> None:
        classifier = AppClassifier(self.paths.assets_dir / "app_categories.json")
        privacy = PrivacyPolicy(self.paths.assets_dir / "privacy_rules.json")
        self.context_sensor = ContextSensor(classifier, privacy)
        self.awareness_timer = QTimer(self)
        self.awareness_timer.timeout.connect(self._poll_context)
        self.surface_timer = QTimer(self)
        self.surface_timer.timeout.connect(self._poll_surface)
        self._hidden_by_fullscreen = False
        self._rebuild_behavior_components()

    def _rebuild_behavior_components(self) -> None:
        awareness = self.config.section("awareness")
        self.context_scheduler = ContextEventScheduler(
            min_dwell_seconds=float(awareness.get("min_dwell_seconds", 15)),
        )
        self.cooldown = Cooldown(
            float(awareness.get("global_cooldown_seconds", 150)),
            float(awareness.get("context_cooldown_seconds", 900)),
        )
        self.behavior_engine = BehaviorEngine(self.dialogue, self.cooldown)

    def _sync_awareness_timer(self) -> None:
        awareness = self.config.section("awareness")
        if awareness.get("enabled", True):
            interval = max(500, int(awareness.get("poll_interval_ms", 1000)))
            self.awareness_timer.start(interval)
        else:
            self.awareness_timer.stop()

    def _sync_surface_timer(self) -> None:
        if bool(self.config.section("comfort").get("desktop_surfaces", True)):
            self.surface_timer.start(800)
        else:
            self.surface_timer.stop()

    def _poll_surface(self) -> None:
        ignored = {int(self.winId()), int(self.control_panel.winId()), int(self.food_panel.winId())}
        self.surface_tracker.observe_foreground(ignored_handles=ignored)

    def _poll_context(self) -> None:
        awareness = self.config.section("awareness")
        privacy = self.config.section("privacy")
        snapshot = self.context_sensor.capture(
            read_window_title=bool(awareness.get("read_window_title", True)),
            idle_detection=bool(awareness.get("idle_detection", True)),
            custom_private_process_names=privacy.get("custom_process_names", []),
        )
        if snapshot is None:
            return
        self.surface_tracker.observe_window(snapshot.window_handle)
        self._last_user_idle = (
            float(snapshot.idle_seconds) if awareness.get("idle_detection", True) else None
        )
        self.activity_director.observe_context(
            category=snapshot.category,
            idle_seconds=snapshot.idle_seconds,
            private=snapshot.is_private,
            late_night=time.localtime().tm_hour >= 23,
        )
        events = self.context_scheduler.observe(snapshot)
        for event in events:
            if event.type == EventType.FULLSCREEN_ENTER and awareness.get(
                "hide_on_fullscreen", True
            ):
                if self.isVisible():
                    self._hidden_by_fullscreen = True
                    self.control_panel.hide()
                    self.hide()
            elif event.type == EventType.FULLSCREEN_EXIT and self._hidden_by_fullscreen:
                self._hidden_by_fullscreen = False
                self.show()
                self.raise_()

        reaction = self.behavior_engine.react(
            snapshot,
            events,
            personality=self._personality(),
        )
        if reaction is not None:
            self.say(reaction.text, inner=reaction.inner)

    def set_awareness_enabled(self, enabled: bool) -> None:
        self.config.set_nested("awareness", "enabled", bool(enabled))
        self._rebuild_behavior_components()
        self._sync_awareness_timer()
        self._refresh_control_panel()
        if enabled:
            self.say("本地环境感知已开启。")
        else:
            self.say("本地环境感知已暂停。")

    def _set_awareness_flag(self, key: str, enabled: bool) -> None:
        self.config.set_nested("awareness", key, bool(enabled))
        self._refresh_control_panel()

    # ----- menus, tray, and persistence ----------------------------------------

    def contextMenuEvent(self, event) -> None:
        if self.control_panel.isVisible():
            self.control_panel.hide()
            event.accept()
            return
        self._interrupt_activity()
        self._refresh_control_panel()
        self.control_panel.popup_near_pet(self.frameGeometry())
        event.accept()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape and self.control_panel.isVisible():
            self.control_panel.hide()
            event.accept()
            return
        super().keyPressEvent(event)

    def _create_tray(self) -> None:
        icon = QIcon(str(self.paths.icon_path))
        self.tray = QSystemTrayIcon(icon, self)
        tray_menu = QMenu()
        tray_menu.addAction("显示/隐藏", self.toggle_visible)
        tray_menu.addAction("打开快速控制", self._open_control_panel_from_tray)
        tray_menu.addAction(
            "暂停/启用环境感知",
            lambda: self.set_awareness_enabled(
                not bool(self.config.section("awareness").get("enabled", True))
            ),
        )
        form_menu = tray_menu.addMenu("切换形态")
        self._tray_form_actions = {}
        for form in FORMS:
            action = form_menu.addAction(f"{form.name} · {form.tagline}")
            action.setCheckable(True)
            action.triggered.connect(lambda _checked=False, key=form.id: self.set_form(key, manual=True))
            self._tray_form_actions[form.id] = action
        form_menu.addSeparator()
        self._tray_follow_action = form_menu.addAction("跟随 Antigravity 当前模型")
        self._tray_follow_action.setCheckable(True)
        self._tray_follow_action.triggered.connect(self.set_follow_model)
        form_menu.aboutToShow.connect(self._sync_tray_forms)
        tray_menu.addAction("Token Meter 面板", self.open_token_meter)
        tray_menu.addAction(
            "显示/隐藏 HUD 悬浮条",
            lambda: self.set_hud_enabled(not bool(self._agy().get("hud_enabled", True))),
        )
        tray_menu.addSeparator()
        tray_menu.addAction("退出", self.quit_app)
        self.tray.setContextMenu(tray_menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

    def _on_tray_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.toggle_visible()

    def _open_control_panel_from_tray(self) -> None:
        self._interrupt_activity()
        self._refresh_control_panel()
        self.control_panel.popup_at(self.pos())

    def set_mode(self, mode: str) -> None:
        self._interrupt_activity(delay_seconds=1.5)
        self.movement.set_mode(mode)
        self.config.set("mode", self.movement.mode)
        self._refresh_control_panel()

    def set_activity_level(self, level: str) -> None:
        level = ActivityDirector._normalized_activity_level(level)
        now = time.monotonic()
        self.activity_director.set_activity_level(level, now=now)
        self.micro_motion_director.set_activity_level(level, now=now)
        self.config.set_nested("comfort", "activity_level", level)
        self._refresh_control_panel()

    def set_walk_speed(self, speed: float) -> None:
        speed = min((64.0, 82.0, 104.0), key=lambda value: abs(value - float(speed)))
        self.movement.set_speed(speed)
        self.config.set_nested("comfort", "walk_speed", speed)
        self._refresh_control_panel()

    def set_reduced_motion(self, enabled: bool) -> None:
        enabled = bool(enabled)
        self.renderer.set_reduced_motion(enabled)
        self.config.set_nested("comfort", "reduced_motion", enabled)
        self._refresh_control_panel()
        self.update()

    def set_hover_pause(self, enabled: bool) -> None:
        self.config.set_nested("comfort", "hover_pause", bool(enabled))
        self._refresh_control_panel()

    def set_desktop_surfaces(self, enabled: bool) -> None:
        dropped = self.surface_tracker.set_enabled(bool(enabled))
        self.config.set_nested("comfort", "desktop_surfaces", bool(enabled))
        self._sync_surface_timer()
        self._refresh_control_panel()
        if dropped and not self.physics.active and not self.interaction.active:
            self._launch_throw(0.0, 0.0, reason="surfaces_disabled")

    def set_size(self, multiplier: float) -> None:
        self.physics.cancel()
        multiplier = self._normalized_size(multiplier)
        self.current_height = self._height_for_size(multiplier)
        self.config.set("size", multiplier)
        self._resize_window()
        self._sync_animation_clip(crossfade=False, immediate=True)
        self.snap_into_screen()

    def snap_into_screen(self) -> None:
        self.physics.cancel()
        self.surface_tracker.detach()
        self.movement.cancel_target()
        screen = (
            QApplication.screenAt(self.frameGeometry().center())
            or self.screen()
            or QApplication.primaryScreen()
        )
        if screen is None:
            return
        geometry = screen.availableGeometry()
        max_x = geometry.right() - self.width() + 1
        max_y = geometry.bottom() - self.height() + 1
        self.move(max(geometry.left(), min(max_x, self.x())), max_y)

    def enterEvent(self, event) -> None:
        self._hovered = True
        if hasattr(self, "hud_bar"):
            self._reveal_hud_temporarily()
        if bool(self.config.section("comfort").get("hover_pause", True)):
            self._interrupt_activity(delay_seconds=5.0)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hovered = False
        if hasattr(self, "hud_bar"):
            self._schedule_hud_auto_hide()
        super().leaveEvent(event)

    def set_passthrough(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if sys.platform != "win32":
            QMessageBox.information(self, "鼠标穿透", "鼠标穿透目前仅在 Windows 上可用。")
            return
        try:
            user32 = ctypes.windll.user32
            handle = int(self.winId())
            get_style = user32.GetWindowLongPtrW
            set_style = user32.SetWindowLongPtrW
            style = get_style(handle, -20)
            layered = 0x80000
            transparent = 0x20
            new_style = style | layered | (transparent if enabled else 0)
            if not enabled:
                new_style &= ~transparent
            set_style(handle, -20, new_style)
            self.config.set("passthrough", enabled)
            self._refresh_control_panel()
            if enabled:
                self.say("我隐身了！右键托盘图标可以解除。")
        except OSError as error:
            QMessageBox.warning(self, "鼠标穿透", f"设置失败：{error}")

    def set_topmost(self, enabled: bool) -> None:
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, bool(enabled))
        self.config.set("topmost", bool(enabled))
        self.show()
        self._refresh_control_panel()

    def set_autostart(self, enabled: bool) -> None:
        enabled = bool(enabled)
        startup_dir = Path(
            os.environ.get(
                "APPDATA",
                str(Path.home() / "AppData" / "Roaming"),
            )
        ) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
        link = startup_dir / "AGY 桌宠.lnk"
        try:
            if enabled:
                pythonw = self.paths.app_dir / ".venv" / "Scripts" / "pythonw.exe"
                interpreter = Path(sys.executable)
                system_pythonw = interpreter.with_name("pythonw.exe")
                target = str(
                    interpreter
                    if getattr(sys, "frozen", False)
                    else (pythonw if pythonw.exists() else system_pythonw if system_pythonw.exists() else interpreter)
                )
                arguments = "" if getattr(sys, "frozen", False) else f'"{self.paths.app_dir / "main.py"}"'
                quote = lambda value: value.replace("'", "''")
                script = (
                    "$shortcut=(New-Object -ComObject WScript.Shell).CreateShortcut('"
                    + quote(str(link))
                    + "');$shortcut.TargetPath='"
                    + quote(target)
                    + "';$shortcut.Arguments='"
                    + quote(arguments)
                    + "';$shortcut.WorkingDirectory='"
                    + quote(str(self.paths.app_dir))
                    + "';$shortcut.Save()"
                )
                subprocess.run(
                    ["powershell", "-NoProfile", "-Command", script],
                    check=True,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                self.say("已设置开机自启，明天见。")
            elif link.exists():
                link.unlink()
                self.say("已取消开机自启。")
            self.config.set("autostart", enabled)
            self._refresh_control_panel()
        except (OSError, subprocess.CalledProcessError) as error:
            QMessageBox.warning(self, "开机自启", f"设置失败：{error}")

    def toggle_visible(self) -> None:
        if self.isVisible():
            self._hidden_by_fullscreen = False
            self.control_panel.hide()
            self.hide()
        else:
            self.show()
            self.raise_()
        self._sync_hud_visibility()

    def activate_from_shortcut(self) -> None:
        """Bring the existing pet back when its shortcut is clicked again."""
        self._hidden_by_fullscreen = False
        self.show()
        self.raise_()
        self.activateWindow()

    def _show_privacy_notice(self) -> None:
        QMessageBox.information(
            self,
            "本地隐私规则",
            "环境感知只在本机读取前台进程名、窗口标题、空闲时长和全屏状态。\n\n"
            "开启“窗口落脚”时，只额外读取前台窗口句柄和外框位置，用来判断顶部落点与随动；不会读取窗口内容。\n\n"
            "密码管理器、支付/银行、远程桌面、聊天和邮件应用，以及无痕窗口默认不保留标题，也不会触发台词。"
            "\n\n不会截图、不会读取浏览历史、不会把感知数据发送到网络。",
        )

    # ----- Antigravity: forms, telemetry, HUD and Token Meter -------------------

    def _agy(self) -> dict:
        """Live (mutable) agy settings; HudBar edits the same dict."""
        section = self.config.data.get("agy")
        if not isinstance(section, dict):
            section = {}
            self.config.data["agy"] = section
        return section

    def _agy_set(self, key: str, value: object) -> None:
        self._agy()[key] = value
        self.config.save()

    def _load_registry(self, form_id: str) -> AssetRegistry:
        return AssetRegistry(
            self.paths.characters_root / form_id,
            base_manifest=self.paths.base_manifest_path,
        )

    def set_form(self, form_id: str, *, manual: bool = False, announce: bool = True, persist: bool = True) -> None:
        form_id = normalize_form(form_id)
        if manual and self._agy().get("follow_model"):
            self._agy_set("follow_model", False)
        if form_id == self.form_id:
            self._refresh_control_panel()
            return
        if self.physics.active or self.interaction.active:
            self._pending_form = form_id
            return
        registry = self._load_registry(form_id)
        bottom = self.y() + self.height()
        center_x = self.x() + self.width() // 2
        self.asset_registry = registry
        self.form_id = form_id
        self._apply_form_voice(form_id)
        if persist:
            self._agy_set("form", form_id)
        self._resize_window()
        self.move(center_x - self.width() // 2, bottom - self.height())
        if not (self.surface_tracker.attached and self.action_machine.current in EDGE_ASSETS):
            # Sitting on a window edge she just changes outfit and keeps sitting.
            self._request_action(PetAction.TRANSFORM, reason="form_switch", force=True, immediate=True)
        self._sync_animation_clip(crossfade=False, immediate=True)
        if announce:
            lines = self._form_voice.get("announce") if isinstance(self._form_voice.get("announce"), list) else []
            form = form_by_id(form_id)
            text = self.rng.choice(lines) if lines else f"{form.name} 形态！{form.tagline}。"
            self.say(str(text), preserve_action=True)

    def _apply_form_voice(self, form_id: str) -> None:
        """Load assets/dialogue/forms/<form>.json: each form talks differently."""
        try:
            voice = json.loads((self.paths.assets_dir / "dialogue" / "forms" / f"{form_id}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            voice = {}
        if not isinstance(voice, dict):
            voice = {}
        self._form_voice = voice
        form = form_by_id(form_id)
        speaker = str(voice.get("speaker") or form.name)
        if hasattr(self, "renderer"):
            self.renderer.speaker_name = speaker
            self.renderer.speaker_accent = QColor(ACCENTS.get(form_id, "#339ad5"))
        if hasattr(self, "control_panel"):
            self.control_panel.title_label.setText(speaker)
        if hasattr(self, "dialogue"):
            self.dialogue.set_form_lines(voice.get("dialogue"))
        if hasattr(self, "food_panel"):
            self.food_panel.hide()
            self.food_panel.set_menu(parse_food_menu(voice.get("food_menu")), ACCENTS.get(form_id, "#339ad5"))
        agy_lines = voice.get("agy") if isinstance(voice.get("agy"), dict) else {}
        for target in (getattr(self, "reactions", None), getattr(self, "autonomy", None)):
            if target is not None:
                target.set_lines(agy_lines)

    def set_follow_model(self, enabled: bool) -> None:
        self._agy_set("follow_model", bool(enabled))
        self._refresh_control_panel()
        if enabled:
            self.reactions.reset_follow()
            self.say("我会跟着 Antigravity 当前的模型换形态。")
            snapshot = self.telemetry.last_snapshot
            if snapshot is not None:
                self._on_live_snapshot(snapshot)

    def _sync_tray_forms(self) -> None:
        for form_id, action in self._tray_form_actions.items():
            action.setChecked(form_id == self.form_id)
        self._tray_follow_action.setChecked(bool(self._agy().get("follow_model", False)))

    def _create_agy(self) -> None:
        settings = self._agy()
        self.reactions = ReactionTracker(self.rng)
        self.autonomy = AgyAutonomy(self.rng)
        self._apply_form_voice(self.form_id)
        self.agy_windows = AntigravityWindowFinder()
        self._autonomy_next = 0.0
        self._next_config_poll = 0.0
        self._last_user_idle: float | None = None
        self.telemetry = TelemetryService(
            TelemetryCollector(use_language_server=bool(settings.get("use_language_server", True))),
            parent=self,
        )
        self.telemetry.pinned_conversation = str(settings.get("pinned_conversation") or "")
        self.hud_bar = HudBar(
            settings=settings,
            on_open_meter=self.open_token_meter,
            on_refresh=self.telemetry.refresh_now,
            on_pin=self._pin_conversation,
            on_hide=lambda: self.set_hud_visibility("hide"),
            on_visibility=self.set_hud_visibility,
        )
        self.hud_bar.settings_changed.connect(self.config.save)
        self.hud_bar.interaction_changed.connect(self._on_hud_interaction_changed)
        self._hud_temp_visible = False
        self._hud_auto_hide_timer = QTimer(self)
        self._hud_auto_hide_timer.setSingleShot(True)
        self._hud_auto_hide_timer.timeout.connect(self._on_hud_auto_hide_timeout)
        self.token_meter = TokenMeterWindow(
            on_request_report=self.telemetry.request_report,
            on_toggle_subagents=self._set_include_subagents,
            icon=QIcon(str(self.paths.icon_path)),
        )
        self.telemetry.live_updated.connect(self._on_live_snapshot)
        self.telemetry.report_ready.connect(self.token_meter.update_report)
        self._hud_anchor: tuple[int, int, int, int] | None = None
        if settings.get("telemetry_enabled", True):
            self.telemetry.start()
        if settings.get("hud_mode") == "floating":
            self.hud_bar.restore_floating_position()

    def _set_include_subagents(self, enabled: bool) -> None:
        self.telemetry.include_subagents = bool(enabled)
        self.telemetry.request_report()

    def _pin_conversation(self, conversation_id: str) -> None:
        self._agy_set("pinned_conversation", conversation_id)
        self.telemetry.pinned_conversation = conversation_id
        self.telemetry.refresh_now()

    def _sync_external_config(self) -> None:
        prev_visibility = self._hud_visibility()
        prev_delay = self._hud_auto_hide_seconds()
        if not hasattr(self, "telemetry") or not self.config.reload_if_external_change():
            return
        settings = self._agy()
        pinned = str(settings.get("pinned_conversation") or "")
        if pinned != self.telemetry.pinned_conversation:
            self.telemetry.pinned_conversation = pinned
            self.telemetry.refresh_now()
        follow = bool(settings.get("follow_model", False))
        if follow:
            self.reactions.reset_follow()
            snapshot = self.telemetry.last_snapshot
            if snapshot is not None:
                self._on_live_snapshot(snapshot)
        else:
            target_form = normalize_form(settings.get("form"))
            if target_form != self.form_id:
                self.set_form(target_form, manual=False, persist=False)
        was_attached = self.hud_bar.attached
        self.hud_bar.settings = settings
        if was_attached and not self.hud_bar.attached:
            self.hud_bar.restore_floating_position()
        self.hud_bar.apply_appearance()
        new_visibility = self._hud_visibility()
        new_delay = self._hud_auto_hide_seconds()
        if new_visibility != prev_visibility:
            if new_visibility == "auto":
                self._reveal_hud_temporarily(start_countdown=True)
            else:
                self._hud_auto_hide_timer.stop()
                self._hud_temp_visible = False
                self._sync_hud_visibility()
        else:
            if new_visibility == "auto" and new_delay != prev_delay and self._hud_auto_hide_timer.isActive():
                self._hud_auto_hide_timer.start(new_delay * 1000)
            self._sync_hud_visibility()
        self._hud_anchor = None
        self._follow_hud()
        if self.telemetry.last_snapshot is not None:
            self.hud_bar.update_snapshot(self.telemetry.last_snapshot)
        if not settings.get("autonomy", True) and self.action_machine.current in {PetAction.WORKING, PetAction.PERCH}:
            self._request_action(PetAction.IDLE, reason="autonomy_off", force=True)
        self._refresh_control_panel()
        self._sync_tray_forms()

    def open_token_meter(self) -> None:
        self.token_meter.update_live(self.telemetry.last_snapshot)
        self.token_meter.show()
        self.token_meter.raise_()
        self.token_meter.activateWindow()

    def _hud_visibility(self) -> str:
        settings = self._agy()
        return normalize_hud_visibility(
            settings.get("hud_visibility"),
            fallback_enabled=bool(settings.get("hud_enabled", True)),
        )

    def _hud_auto_hide_seconds(self) -> int:
        return clamp_hud_auto_hide_seconds(self._agy().get("hud_auto_hide_seconds", 5))

    def _want_hud_visible(self) -> bool:
        settings = self._agy()
        visibility = self._hud_visibility()
        if visibility == "hide":
            return False
        if visibility == "auto" and not self._hud_temp_visible:
            return False
        return settings.get("hud_mode") == "floating" or self.isVisible()

    def _reveal_hud_temporarily(self, *, start_countdown: bool = False) -> None:
        if self._hud_visibility() != "auto":
            return
        self._hud_temp_visible = True
        self._sync_hud_visibility()
        if self._hovered or self.hud_bar.interactive:
            self._hud_auto_hide_timer.stop()
        elif start_countdown:
            self._hud_auto_hide_timer.start(self._hud_auto_hide_seconds() * 1000)

    def _schedule_hud_auto_hide(self) -> None:
        if self._hud_visibility() != "auto" or not self._hud_temp_visible:
            return
        if self._hovered or self.hud_bar.interactive:
            self._hud_auto_hide_timer.stop()
            return
        self._hud_auto_hide_timer.start(self._hud_auto_hide_seconds() * 1000)

    def _on_hud_auto_hide_timeout(self) -> None:
        if self._hud_visibility() != "auto":
            return
        if self._hovered or self.hud_bar.interactive:
            return
        self._hud_temp_visible = False
        self._sync_hud_visibility()

    def _on_hud_interaction_changed(self, interactive: bool) -> None:
        if self._hud_visibility() != "auto":
            return
        if interactive:
            self._hud_auto_hide_timer.stop()
        else:
            self._schedule_hud_auto_hide()

    def set_hud_visibility(self, visibility: str) -> None:
        normalized = normalize_hud_visibility(visibility)
        self._agy_set("hud_visibility", normalized)
        self._agy_set("hud_enabled", normalized != "hide")
        if normalized == "auto":
            self._reveal_hud_temporarily(start_countdown=True)
        else:
            self._hud_auto_hide_timer.stop()
            self._hud_temp_visible = False
            self._sync_hud_visibility()
        self._refresh_control_panel()

    def set_hud_auto_hide_seconds(self, seconds: int) -> None:
        clamped = clamp_hud_auto_hide_seconds(seconds)
        self._agy_set("hud_auto_hide_seconds", clamped)
        if (
            self._hud_visibility() == "auto"
            and self._hud_temp_visible
            and self._hud_auto_hide_timer.isActive()
        ):
            self._hud_auto_hide_timer.start(clamped * 1000)
        self._refresh_control_panel()

    def set_hud_enabled(self, enabled: bool) -> None:
        self.set_hud_visibility("show" if enabled else "hide")

    def _sync_hud_visibility(self) -> None:
        want = self._want_hud_visible()
        if want and not self.hud_bar.isVisible():
            self._hud_anchor = None
            if self.hud_bar.attached:
                bubble_visible = self.bubble.visible
                perched = self.surface_tracker.attached
                self.hud_bar.follow_pet(
                    self._sprite_rect(),
                    sprite_top=self._hud_ceiling(bubble_visible),
                    prefer_above=perched,
                )
            self.hud_bar.show()
            self._follow_hud()
        elif not want and self.hud_bar.isVisible():
            self.hud_bar.hide()

    # ----- autonomy ---------------------------------------------------------------

    def set_autonomy(self, enabled: bool) -> None:
        self._agy_set("autonomy", bool(enabled))
        self._refresh_control_panel()
        if not enabled and self.action_machine.current in {PetAction.WORKING, PetAction.PERCH}:
            self._request_action(PetAction.IDLE, reason="autonomy_off", force=True)

    def _tick_autonomy(self, now: float) -> None:
        if not hasattr(self, "autonomy") or now < self._autonomy_next:
            return
        self._autonomy_next = now + 0.5
        settings = self._agy()
        if not settings.get("autonomy", True):
            return
        snapshot = self.telemetry.last_snapshot
        busy = (
            self.interaction.active
            or self.physics.active
            or self.climb.active
            or self.control_panel.isVisible()
            or self.food_panel.isVisible()
        )
        can_hop = (
            bool(settings.get("hop_to_window", True))
            and self.surface_tracker.enabled
            and self.movement.mode != "still"
        )
        window = None
        if can_hop and snapshot is not None and snapshot.running and not self.surface_tracker.attached:
            window = self.agy_windows.find()
            # Only hop onto Antigravity while the user is looking at it; a
            # covered window would leave the pet sitting on thin air.
            foreground = getattr(self.surface_tracker.reader, "foreground_handle", None)
            if window is not None and callable(foreground) and int(foreground() or 0) != window.handle:
                window = None
        commands = self.autonomy.update(
            AutonomyInput(
                now=now,
                snapshot=snapshot,
                pet_action=self.action_machine.current.value,
                busy=busy,
                perched=self.surface_tracker.attached,
                user_idle_seconds=self._last_user_idle,
                window=window,
                can_hop=can_hop,
            )
        )
        skip_say = False
        for command in commands:
            if command.kind == "hop":
                skip_say = not self._hop_to(command)
            elif command.kind == "say":
                if not skip_say:
                    self.say(command.text, preserve_action=True)
                skip_say = False
            elif command.kind == "action" and command.action:
                self._autonomy_action(command, now)

    def _autonomy_action(self, command: AutonomyCommand, now: float) -> None:
        try:
            action = PetAction.coerce(command.action)
        except ValueError:
            return
        if action == self.action_machine.current or self.climb.active:
            return
        if action not in {PetAction.IDLE, PetAction.PERCH}:
            self.activity_director.interrupt(now=now)
            self.micro_motion_director.defer(now, minimum_seconds=3.0)
            self.movement.cancel_target()
        self._request_action(action, now=now, reason=f"autonomy_{command.reason}", force=command.force)

    def _hop_to(self, command: AutonomyCommand) -> bool:
        """Jump onto the Antigravity window's top edge; False if unreachable."""
        target = command.window
        if target is None or self.physics.active or self.interaction.active or self.climb.active:
            return False
        if command.reason != "panel" and self._start_climb(target.handle):
            return True
        bounds = self._physics_bounds()
        velocity = plan_hop(
            start_x=float(self.x()),
            start_y=float(self.y()),
            widget_width=float(self.width()),
            widget_height=float(self.height()),
            target=target,
            gravity=self.physics.gravity,
            max_speed=self.physics.max_jump_speed,
            work_area_top=bounds.top,
        )
        if velocity is None or not self.surface_tracker.set_target(target.handle):
            return False
        vx, vy = velocity
        self._set_direction("left" if vx < 0 else "right")
        self.activity_director.interrupt(now=time.monotonic())
        self.movement.cancel_target()
        self.surface_tracker.detach()
        self.movement.rest_after_drag()
        self.physics.launch(self.x(), self.y(), vx, vy, planned_jump=True)
        self._request_action(PetAction.FALLING, reason="autonomy_hop", force=True, immediate=True)
        self._clear_target_later()
        return True

    def _clear_target_later(self, delay_ms: int = 4_000) -> None:
        """Drop the landing target after a hop, unless a newer hop owns it."""
        self._hop_token += 1
        token = self._hop_token

        def clear() -> None:
            if token == self._hop_token:
                self.surface_tracker.set_target(None)

        QTimer.singleShot(delay_ms, clear)

    # ----- climbing and sitting on window edges -----------------------------------

    def _edge_change(self, change) -> bool:
        """True when a change enters or leaves sitting on the window edge."""
        return self.surface_tracker.attached and bool(
            {change.previous, change.current} & set(EDGE_ASSETS)
        )

    def _edge_sit_sink(self) -> float:
        """How far below the window's top edge she sits with her legs hanging."""
        asset = EDGE_ASSETS.get(self.action_machine.current)
        if asset is None or not self.surface_tracker.attached:
            return 0.0
        layer = self.player.snapshot().current
        if layer is None or layer.asset_id != asset or layer.frame.pixmap.isNull():
            return 0.0
        resolved = self.asset_registry.resolve_asset_id(asset)
        seat = self.asset_registry.manifest.get("seat_line") or {}
        if resolved not in seat:
            return 0.0  # no edge art yet: she stands on the edge instead
        seat_y = float(seat[resolved])
        # Standing puts the feet (anchor y) on the edge; drop the seat there.
        return max(0.0, (layer.anchor.y - seat_y) * layer.frame.pixmap.height())

    def _frame_size(self) -> tuple[float, float]:
        layer = self.player.snapshot().current
        if layer is None or layer.frame.pixmap.isNull():
            return float(self.current_height) * 0.6, float(self.current_height)
        pixmap = layer.frame.pixmap
        return float(pixmap.width()), float(pixmap.height())

    def _start_climb(self, handle: int | None, *, sit_after: bool = False) -> bool:
        """Walk to the nearer side of window ``handle`` and climb it."""
        if not handle or self.physics.active or self.interaction.active or self.surface_tracker.attached:
            return False
        rect = self.surface_tracker.read_rect(int(handle))
        if rect is None:
            return False
        bounds = self._physics_bounds()
        sprite_width, sprite_height = self._frame_size()
        plan = plan_climb(
            handle=int(handle),
            window=ClimbRect(rect.left, rect.top, rect.right, rect.bottom),
            work_area=ClimbRect(bounds.left, bounds.top, bounds.right, bounds.bottom),
            widget_x=float(self.x()),
            widget_y=float(self.y()),
            widget_width=float(self.width()),
            widget_height=float(self.height()),
            feet_offset=float(MARGIN + 7),
            sprite_width=sprite_width,
            sprite_height=sprite_height,
            **self._climb_reach(),
        )
        if plan is None:
            return False
        now = time.monotonic()
        self.activity_director.interrupt(now=now)
        self.micro_motion_director.defer(now, minimum_seconds=3.0)
        self.movement.cancel_target()
        self.climb.start(plan, sit_after=sit_after)
        self._climb_pos = (float(self.x()), float(self.y()))
        return True

    def _tick_climb(self, delta_seconds: float, now: float) -> bool:
        """Advance the climb; True while she is still walking to the wall."""
        plan = self.climb.plan
        if plan is None:
            return False
        rect = self.surface_tracker.read_rect(plan.handle, now=now)
        window = None if rect is None else ClimbRect(rect.left, rect.top, rect.right, rect.bottom)
        x, y = self._climb_pos
        was_on_wall = self.climb.on_wall
        step = self.climb.step(delta_seconds, x=x, y=y, window=window)
        if step.phase == "aborted":
            self.climb.cancel()
            self._launch_throw(0.0, 0.0, reason="climb_lost")
            return False
        self._climb_pos = (step.x, step.y)
        self.move(round(step.x), round(step.y))
        if step.phase == "approach":
            self._set_direction(step.facing)
            if self.action_machine.current != PetAction.WALKING:
                # Whatever she was doing (typing, celebrating), she walks now.
                self._request_action(PetAction.WALKING, now=now, reason="climb_approach", force=True)
            return True
        if not was_on_wall or self.action_machine.current != PetAction.CLIMBING:
            # Hands on the wall: the pulls start in step with frame 0.
            self.movement.set_direction(step.facing)
            self._request_action(PetAction.CLIMBING, now=now, reason="climb", force=True, immediate=True)
        if step.phase == "done":
            self._finish_climb(rect, now)
        return False

    def _finish_climb(self, rect, now: float) -> None:
        """Pulled up over the edge: she now stands on the window's top."""
        plan, sit_after = self.climb.plan, self.climb.sit_after
        self.climb.cancel()
        if plan is None or rect is None or not self.surface_tracker.set_target(plan.handle):
            self._launch_throw(0.0, 0.0, reason="climb_lost")
            return
        self._perch_after_landing = sit_after
        self._gentle_landing = True
        self._settle_landing(f"window:{plan.handle}", hard=False, now=now)
        self.surface_tracker.set_target(None)

    def _climb_reach(self) -> dict[str, float]:
        """``reach`` / ``hand_rise`` for plan_climb, measured on the climb frames."""
        key = (self.form_id, self.current_height)
        if key not in self._climb_metrics_cache:
            self._climb_metrics_cache[key] = self._measure_climb_frames()
        metrics = self._climb_metrics_cache[key]
        if metrics is None:
            return {}
        reach, rise = metrics
        return {"reach": reach, "hand_rise": rise}

    def _measure_climb_frames(self) -> tuple[float, float] | None:
        """Hands on the wall: centre-to-wall distance and feet-to-hand height.

        The left-facing climb frames have the wall on the left, so the hands
        are the leftmost opaque pixels and the highest pixels near that side.
        """
        try:
            clip = self.asset_registry.clip_for(PetAction.CLIMBING, height=self.current_height, direction="left")
        except Exception:
            return None
        reaches, rises = [], []
        for frame in clip.frames:
            image = frame.pixmap.toImage().convertToFormat(QImage.Format.Format_ARGB32)
            width, height = image.width(), image.height()
            if width < 8 or height < 8 or image.bytesPerLine() != width * 4:
                continue
            alpha = bytes(image.constBits())[3::4]  # one byte per pixel, row-major
            wall = next((x for x in range(width) if max(alpha[x::width]) > 40), None)
            if wall is None:
                continue
            band = max(2, round(width * 0.22))  # the strip next to the wall
            top = next(
                (y for y in range(height) if max(alpha[y * width + wall : y * width + min(width, wall + band)]) > 40),
                None,
            )
            if top is None:
                continue
            feet = clip.anchor.y * height
            reaches.append(width * clip.anchor.x - wall)
            rises.append(feet - top)
        if not reaches:
            return None
        return max(4.0, min(reaches)), max(rises)

    def _climb_candidate(self) -> int | None:
        """The window to climb from the panel: the last foreground app window."""
        handle = self.surface_tracker.candidate_handle
        if handle:
            return handle
        window = self.agy_windows.find() if hasattr(self, "agy_windows") else None
        return window.handle if window is not None else None

    def _panel_climb(self, *, sit_after: bool) -> None:
        self._interrupt_activity()
        if sit_after and self.surface_tracker.attached:
            self._request_action(PetAction.PERCH, reason="panel_edge_sit", force=True, immediate=True)
            return
        if self.climb.active:
            return
        nearest = self._nearest_climbable()
        if nearest is not None and self._start_climb(nearest, sit_after=sit_after):
            self._explored_handle = nearest
            return
        handle = self._climb_candidate()
        if self._start_climb(handle, sit_after=sit_after):
            self._explored_handle = handle
            return
        # Too far or no floor under the window: jump up instead.
        rect = self.surface_tracker.read_rect(int(handle)) if handle else None
        if rect is not None:
            target = WindowTarget(int(handle), round(rect.left), round(rect.top), round(rect.right), round(rect.bottom))
            if self._hop_to(AutonomyCommand(kind="hop", window=target, reason="panel")):
                self._perch_after_landing = sit_after
                self._explored_handle = int(handle)
                return
        self.say("这里没有能爬的窗口呀", preserve_action=True)

    # ----- exploring nearby windows on her own -----------------------------------

    def _nearest_climbable(self) -> int | None:
        """The closest window she could climb right now (None if there is none)."""
        if self.surface_tracker.attached:
            return None
        bounds = self._physics_bounds()
        sprite_width, sprite_height = self._frame_size()
        best: tuple[float, int] | None = None
        above: list[ClimbRect] = []  # windows stacked over the current one
        for surface in self.surface_tracker.nearby_windows():
            rect = ClimbRect(surface.left, surface.top, surface.right, surface.bottom)
            plan = plan_climb(
                handle=surface.handle,
                window=ClimbRect(surface.left, surface.top, surface.right, surface.bottom),
                work_area=ClimbRect(bounds.left, bounds.top, bounds.right, bounds.bottom),
                widget_x=float(self.x()),
                widget_y=float(self.y()),
                widget_width=float(self.width()),
                widget_height=float(self.height()),
                feet_offset=float(MARGIN + 7),
                sprite_width=sprite_width,
                sprite_height=sprite_height,
            )
            covered = plan is None or not is_exposed(plan, rect, above)
            above.append(rect)
            if covered:
                continue
            distance = abs(plan.climb_x - self.x())
            if best is None or distance < best[0]:
                best = (distance, surface.handle)
        return best[1] if best is not None else None

    def _top_bounds(self, now: float | None = None) -> tuple[float, float] | None:
        """Walking range on the window she stands on: its uncovered top edge."""
        bounds = self.surface_tracker.movement_bounds(widget_width=float(self.width()))
        handle = self.surface_tracker.attached_handle
        if bounds is None or handle is None:
            return bounds
        now = time.monotonic() if now is None else now
        cache = self._top_bounds_cache
        if cache is not None and cache[0] == handle and now - cache[1] < 1.0:
            span = cache[2]
        else:
            span = None
            above: list[ClimbRect] = []
            for surface in self.surface_tracker.nearby_windows(now=now):
                rect = ClimbRect(surface.left, surface.top, surface.right, surface.bottom)
                if surface.handle == handle:
                    span = uncovered_top_span(rect, above)
                    break
                above.append(rect)
            self._top_bounds_cache = (handle, now, span)
        if span is None:
            return bounds
        margin = min(38.0, max(18.0, (span[1] - span[0]) * 0.055))
        lower = max(bounds[0], span[0] + margin - self.width() / 2.0)
        upper = min(bounds[1], span[1] - margin - self.width() / 2.0)
        if upper < lower:
            return bounds
        return lower, upper

    def _walk_on_top(self, now: float) -> bool:
        """Get up and stroll to another spot along the window top."""
        bounds = self._top_bounds(now)
        if bounds is None:
            return False
        lower, upper = bounds
        x = float(self.x())
        body = max(60.0, self._frame_size()[0])
        options = [t for t in (self.rng.uniform(lower, upper) for _ in range(6)) if abs(t - x) >= body * 0.8]
        if not options:
            return False
        if self.action_machine.current != PetAction.IDLE:
            self._request_action(PetAction.IDLE, now=now, reason="top_stroll", force=True, immediate=True)
        self.movement.rest_until = 0.0
        self.movement.target = (options[0], float(self.y()))
        return True

    def _jump_candidates(self) -> list[tuple[int, ClimbRect]]:
        return [
            (surface.handle, ClimbRect(surface.left, surface.top, surface.right, surface.bottom))
            for surface in self.surface_tracker.nearby_windows()
        ]

    def _plan_jump(self, *, from_floor: bool) -> JumpPlan | None:
        bounds = self._physics_bounds()
        sprite_width, sprite_height = self._frame_size()
        feet = float(self.y() + self.height() - (MARGIN + 7))
        return pick_jump(
            windows=self._jump_candidates(),
            work_area=ClimbRect(bounds.left, bounds.top, bounds.right, bounds.bottom),
            feet_y=feet,
            center_x=float(self.x()) + self.width() / 2.0,
            sprite_width=sprite_width,
            sprite_height=sprite_height,
            exclude=self.surface_tracker.attached_handle,
            max_gap=sprite_width * (2.5 if from_floor else 2.0),
            max_up=sprite_height * (1.6 if from_floor else 0.9),
            max_down=0.0 if from_floor else sprite_height * 2.5,
        )

    def _jump_to(self, jump: JumpPlan, *, sit_after: bool = False) -> bool:
        """Ballistic hop onto ``jump``'s window top (from the floor or a window)."""
        if self.physics.active or self.interaction.active or self.climb.active:
            return False
        rect = jump.window
        target = WindowTarget(jump.handle, round(rect.left), round(rect.top), round(rect.right), round(rect.bottom))
        bounds = self._physics_bounds()
        fraction = (jump.land_center_x - rect.left) / max(1.0, rect.width)
        velocity = plan_hop(
            start_x=float(self.x()),
            start_y=float(self.y()),
            widget_width=float(self.width()),
            widget_height=float(self.height()),
            target=target,
            gravity=self.physics.gravity,
            max_speed=self.physics.max_jump_speed,
            work_area_top=bounds.top,
            landing_fraction=max(0.0, min(1.0, fraction)),
            apex_clearance=40.0,
        )
        if velocity is None or not self.surface_tracker.set_target(jump.handle):
            return False
        now = time.monotonic()
        source = self.surface_tracker.attached_handle
        if source is not None:
            # Do not land straight back on the window she leaves.
            self.surface_tracker.ignore_platform(source, seconds=1.5, now=now)
        vx, vy = velocity
        self._set_direction("left" if vx < 0 else "right")
        self.activity_director.interrupt(now=now)
        self.movement.cancel_target()
        self.surface_tracker.detach()
        self.movement.rest_after_drag()
        self._perch_after_landing = sit_after
        self._gentle_landing = True
        self.physics.launch(self.x(), self.y(), vx, vy, planned_jump=True)
        self._request_action(PetAction.FALLING, reason="window_jump", force=True, immediate=True)
        self._clear_target_later()
        self._explored_handle = jump.handle
        self._perched_since = None
        return True

    def _tick_explore(self, now: float) -> None:
        """Climb or hop onto nearby windows, stroll along their tops, jump
        across to neighbouring windows, and later come back down."""
        attached = self.surface_tracker.attached
        if not attached:
            self._perched_since = None
        elif self._perched_since is None:
            self._perched_since = now
        if now < self._explore_next:
            return
        self._explore_next = now + 1.0
        if not bool(self._agy().get("explore_windows", True)) or not self.surface_tracker.enabled:
            return
        snapshot = self.telemetry.last_snapshot
        running = snapshot is not None and bool(snapshot.running)
        own = (
            attached
            and self._explored_handle is not None
            and self.surface_tracker.attached_handle == self._explored_handle
        )
        if running and attached:
            # Working along with Antigravity on a window top: stay put.
            self._perched_since = now
            return
        if self.movement.target is not None and self.action_machine.current not in {PetAction.IDLE, PetAction.WALKING}:
            self.movement.cancel_target()
        calm = {PetAction.IDLE, PetAction.PERCH} | ({PetAction.WORKING} if running else set())
        if (
            self.action_machine.current not in calm
            or self.movement.mode != "wander"
            or self.movement.target is not None
            or self.interaction.active
            or self.physics.active
            or self.climb.active
            or self._descend_x is not None
            or self._hovered
            or self.control_panel.isVisible()
            or self.food_panel.isVisible()
            or self.activity_director.active
        ):
            return
        if attached:
            # Only climb back down from a window she went up by herself (or
            # from the panel); Antigravity's window belongs to autonomy.
            if own and self._perched_since is not None and now - self._perched_since >= self._explore_stay:
                self._start_descend()
                return
            if now < self._roam_next:
                return
            self._roam_next = now + self.rng.uniform(14.0, 32.0)
            if self.action_machine.current == PetAction.PERCH:
                # Sitting quietly: stay at least a while (6 minutes after a
                # run, like autonomy), and then only sometimes get up.
                seated = now - (self._perched_since or now)
                linger = self.autonomy.settings.perch_linger_seconds if not own else 90.0
                if seated < linger or self.rng.random() < 0.5:
                    return
            roll = self.rng.random()
            if roll < 0.35:
                jump = self._plan_jump(from_floor=False)
                if jump is not None and self._jump_to(jump, sit_after=self.rng.random() < 0.4):
                    self._explore_stay = self.rng.uniform(120.0, 300.0)
                    return
            if roll < 0.85:
                self._walk_on_top(now)
            return
        self._explore_next = now + (self.rng.uniform(90.0, 200.0) if running else self.rng.uniform(45.0, 110.0))
        sit_after = self.rng.random() < 0.6
        handle = self._nearest_climbable()
        if handle is not None and self._start_climb(handle, sit_after=sit_after):
            self._explored_handle = handle
            self._explore_stay = self.rng.uniform(120.0, 300.0)
            return
        jump = self._plan_jump(from_floor=True)
        if jump is not None and self._jump_to(jump, sit_after=sit_after):
            self._explore_stay = self.rng.uniform(120.0, 300.0)

    _explore_stay = 180.0

    def _start_descend(self) -> None:
        """Walk to the nearer end of the window top, then hop off."""
        bounds = self._top_bounds()
        if bounds is None:
            return
        lower, upper = bounds
        x = float(self.x())
        self._descend_x = lower if x - lower <= upper - x else upper
        self.movement.cancel_target()

    def _tick_descend(self, delta_seconds: float, now: float) -> bool:
        target = self._descend_x
        handle = self.surface_tracker.attached_handle
        if target is None or handle is None:
            self._descend_x = None
            return False
        x = float(self.x())
        gap = target - x
        step = 85.0 * delta_seconds
        if abs(gap) > step:
            self.move(round(x + (step if gap > 0 else -step)), self.y())
            self.surface_tracker.record_pet_position(widget_x=float(self.x()), widget_width=float(self.width()))
            self._set_direction("right" if gap > 0 else "left")
            if self.action_machine.current != PetAction.WALKING:
                self._request_action(PetAction.WALKING, now=now, reason="descend_walk", force=True)
            return True
        # At the end of the window: a small hop outward, then fall to the floor.
        self._descend_x = None
        outward = 1.0 if gap >= 0 else -1.0
        self.surface_tracker.ignore_platform(handle, seconds=3.0, now=now)
        self.surface_tracker.set_target(None)
        self._gentle_landing = True
        self._launch_throw(300.0 * outward, -280.0, reason="descend")
        self._perched_since = None
        return False

    def _sprite_rect(self) -> QRect:
        """Global rectangle of the sprite area (window minus bubble strip)."""
        top = self.y() + BUBBLE_HEIGHT + MARGIN
        return QRect(self.x(), top, self.width(), self.y() + self.height() - top)

    def _follow_hud(self) -> None:
        if not hasattr(self, "hud_bar"):
            return
        if self.hud_bar.isVisible() != self._want_hud_visible():
            self._sync_hud_visibility()
        if not self.hud_bar.isVisible() or not self.hud_bar.attached:
            return
        bubble_visible = self.bubble.visible
        # Sitting on a window: never drop the HUD over that window's title bar.
        perched = self.surface_tracker.attached
        anchor = (self.x(), self.y(), self.width(), self.height(), bubble_visible, perched)
        if anchor == self._hud_anchor:
            return
        self._hud_anchor = anchor
        self.hud_bar.follow_pet(
            self._sprite_rect(),
            sprite_top=self._hud_ceiling(bubble_visible),
            prefer_above=perched,
        )

    def _hud_ceiling(self, bubble_visible: bool) -> int:
        """Lowest y the HUD may reach when it has to sit above the pet.

        The bubble strip fills the window's top ``BUBBLE_HEIGHT`` pixels and the
        sprite's head starts right under it, so a HUD squeezed between them
        covers either the text or the head.  Without a bubble it rests on the
        head; with one it moves above the window.
        """
        if bubble_visible:
            return self.y() - 2
        # Renderer floor is ``height - MARGIN - 7``; sprites are current_height tall.
        head_top = self.y() + self.height() - MARGIN - 7 - self.current_height
        return max(self.y() + BUBBLE_HEIGHT, head_top) - 2

    def _on_live_snapshot(self, snapshot) -> None:
        self.hud_bar.update_snapshot(snapshot)
        self._hud_anchor = None
        if self.token_meter.isVisible():
            self.token_meter.update_live(snapshot)
        settings = self._agy()
        if not settings.get("reactions", True) and not settings.get("follow_model"):
            return
        reactions = self.reactions.observe(
            snapshot,
            now=time.monotonic(),
            follow_model=bool(settings.get("follow_model", False)),
            current_form=self.form_id,
        )
        for reaction in reactions:
            if reaction.form:
                self.set_form(reaction.form, announce=False)
            if not settings.get("reactions", True):
                continue
            busy = self.interaction.active or self.physics.active or self.action_machine.current == PetAction.SLEEPING
            action_name = reaction.action
            if action_name == "WORKING" and not settings.get("autonomy", True):
                action_name = "THINKING"  # WORKING loops; only autonomy ends it
            if (
                action_name not in {"WORKING", "PERCH"}
                and self.surface_tracker.attached
                and self.action_machine.current in EDGE_ASSETS
            ):
                # Sitting on a window edge: cheer with words, don't stand up.
                action_name = None
            if action_name and not busy:
                try:
                    self._request_action(PetAction.coerce(action_name), reason=f"agy_{reaction.kind}")
                except ValueError:
                    pass
            self.say(reaction.text, preserve_action=True)

    def _personality(self) -> str:
        return normalize_personality(self.config.get("personality", "standard"))

    def quit_app(self) -> None:
        self.save_snapshot()
        self.telemetry.stop()
        self.hud_bar.hide()
        self.token_meter.hide()
        self.tray.hide()
        QApplication.quit()

    def closeEvent(self, event) -> None:
        self.quit_app()
        event.accept()
