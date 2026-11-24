"""Qt port of agy-hud-bar: a small always-on-top telemetry card.

A narrow vertical card, one metric per row: ⚡ tok/s, the 5-hour and weekly
quota (each with a bar), cache hit rate, context used.

It either rides along under/above the pet ("attached") or floats freely where
the user drops it ("floating"), e.g. next to Antigravity's composer.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, Qt, Signal
from PySide6.QtGui import QAction, QActionGroup, QColor, QMouseEvent, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import QApplication, QFrame, QGridLayout, QHBoxLayout, QLabel, QMenu, QVBoxLayout, QWidget

from .telemetry import LiveSnapshot, format_reset_countdown, format_tokens

TAIL = 9  # height of the dialog tail that points at the pet
APPEARANCES = {
    "dialog": ("对话框（指向桌宠的气泡卡片）", ""),
    "capsule": ("暗色胶囊", "background:rgba(21,23,28,240);border:1px solid rgba(255,255,255,44);"),
    "seamless": ("无边框融合（透明底）", "background:rgba(0,0,0,2);border:1px solid rgba(0,0,0,0);"),
    "glass": ("轻透毛玻璃", "background:rgba(20,22,27,184);border:1px solid rgba(255,255,255,56);"),
}
SOURCE_NAMES = {
    "ls": "语言服务实时",
    "pet_live": "AGY 桌宠插件实时",
    "hud_live": "Token Meter 侧边栏",
    "transcript": "本地会话日志（离线）",
}


def _rgb(hex_color: str) -> str:
    return f"color:{hex_color};"


class QuotaBar(QWidget):
    """A thin rounded bar showing how much of a quota window is left."""

    def __init__(self) -> None:
        super().__init__()
        self.setFixedSize(64, 6)
        self.fraction: float | None = None
        self.color = QColor("#34D399")

    def set_value(self, percent: float | None, color: str) -> None:
        self.fraction = None if percent is None else max(0.0, min(1.0, percent / 100.0))
        self.color = QColor(color)
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255, 255, 255, 34))
        painter.drawRoundedRect(QRectF(self.rect()), 3, 3)
        if self.fraction:
            fill = QRectF(0, 0, self.width() * self.fraction, self.height())
            painter.setBrush(self.color)
            painter.drawRoundedRect(fill, 3, 3)


class HudBar(QWidget):
    settings_changed = Signal()
    interaction_changed = Signal(bool)

    def __init__(
        self,
        *,
        settings: dict,
        on_open_meter: Callable[[], None],
        on_refresh: Callable[[], None],
        on_pin: Callable[[str], None],
        on_hide: Callable[[], None],
        on_visibility: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setWindowTitle("AGY HUD")
        self.settings = settings
        self._on_open_meter = on_open_meter
        self._on_refresh = on_refresh
        self._on_pin = on_pin
        self._on_hide = on_hide
        self._on_visibility = on_visibility
        self._drag_from: QPoint | None = None
        self._conversations: list[tuple[str, str]] = []
        self._pet_rect: QRect | None = None
        self._hovered = False
        self._menu_open = False

        self._tail_dir: str | None = None  # "down" (HUD above pet), "up" (below) or None
        self._outer = QVBoxLayout(self)
        self._outer.setContentsMargins(0, 0, 0, 0)
        outer = self._outer
        self.pill = QFrame()
        self.pill.setObjectName("pill")
        outer.addWidget(self.pill)
        self.card = self._build_card()
        outer.addWidget(self.card)
        # One metric per row; the quota is two rows (5-hour, weekly) with bars.
        grid = QGridLayout(self.pill)
        grid.setContentsMargins(10, 6, 10, 7)
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(3)

        self.dot = QLabel("●")
        self.speed = QLabel("— tok/s")
        self.quota_5h = QLabel("—")
        self.quota_week = QLabel("—")
        self.quota_5h_bar = QuotaBar()
        self.quota_week_bar = QuotaBar()
        self.cache = QLabel("—%")
        self.context = QLabel("—")
        head = QHBoxLayout()
        head.setSpacing(4)
        head.addWidget(self.dot)
        head.addWidget(self._muted("⚡", "#38BDF8"))
        head.addWidget(self.speed)
        head.addStretch(1)
        grid.addLayout(head, 0, 0, 1, 3)
        for row, (caption, bar, value) in enumerate(
            (("5h", self.quota_5h_bar, self.quota_5h), ("周", self.quota_week_bar, self.quota_week)), start=1
        ):
            grid.addWidget(self._muted(caption), row, 0)
            grid.addWidget(bar, row, 1, Qt.AlignmentFlag.AlignVCenter)
            grid.addWidget(value, row, 2, Qt.AlignmentFlag.AlignRight)
            value.setStyleSheet(_rgb("#9CA3AF") + "font-weight:600;")
        for row, (caption, value) in enumerate((("缓存", self.cache), ("上下文", self.context)), start=3):
            grid.addWidget(self._muted(caption), row, 0)
            grid.addWidget(value, row, 1, 1, 2, Qt.AlignmentFlag.AlignRight)
        for label, color in ((self.speed, "#E5E7EB"), (self.cache, "#2DD4BF"), (self.context, "#C4B5FD")):
            label.setStyleSheet(_rgb(color) + "font-weight:600;")
        self.dot.setStyleSheet("color:#6B7280;font-size:8px;")
        self.setStyleSheet("QLabel{font-family:'Segoe UI','Microsoft YaHei UI';font-size:12px;background:transparent;border:none;}")
        self.apply_appearance()
        self.pill.setToolTip("正在读取 Antigravity 实时遥测数据…")

    # ----- construction helpers ---------------------------------------------

    @staticmethod
    def _muted(text: str, color: str = "#9CA3AF") -> QLabel:
        label = QLabel(text)
        label.setStyleSheet(_rgb(color))
        return label

    def _build_card(self) -> QFrame:
        card = QFrame()
        card.setObjectName("card")
        card.setFixedWidth(290)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 7, 12, 8)
        layout.setSpacing(3)
        head = QHBoxLayout()
        head.setSpacing(5)
        self.card_dot = QLabel("●")
        self.card_title = QLabel("等待 Antigravity 会话…")
        self.card_title.setStyleSheet("color:#12395b;font-weight:700;font-size:11px;")
        self.card_model = QLabel("")
        self.card_model.setStyleSheet("color:#6b7f95;font-size:10px;")
        head.addWidget(self.card_dot)
        head.addWidget(self.card_title, 1)
        layout.addLayout(head)
        layout.addWidget(self.card_model)
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(1)
        self.card_values: dict[str, QLabel] = {}
        captions = (("speed", "⚡ 速率"), ("cache", "🎯 缓存"), ("context", "🧠 上下文"))
        for index, (key, caption) in enumerate(captions):
            cap = QLabel(caption)
            cap.setStyleSheet("color:#6b7f95;font-size:11px;")
            value = QLabel("—")
            value.setStyleSheet("color:#12395b;font-weight:700;font-size:12px;")
            self.card_values[key] = value
            grid.addWidget(cap, index // 2, (index % 2) * 2)
            grid.addWidget(value, index // 2, (index % 2) * 2 + 1)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        layout.addLayout(grid)
        # Two quota rows: 5-hour window above, weekly below.
        quota = QGridLayout()
        quota.setHorizontalSpacing(6)
        quota.setVerticalSpacing(1)
        for row, (key, caption) in enumerate((("quota_5h", "🔋 5h 限额"), ("quota_week", "📅 周限额"))):
            cap = QLabel(caption)
            cap.setStyleSheet("color:#6b7f95;font-size:11px;")
            value = QLabel("—")
            value.setStyleSheet("color:#12395b;font-weight:700;font-size:12px;")
            reset = QLabel("")
            reset.setStyleSheet("color:#8a9bb0;font-size:10px;")
            self.card_values[key] = value
            self.card_values[key + "_reset"] = reset
            quota.addWidget(cap, row, 0)
            quota.addWidget(value, row, 1)
            quota.addWidget(reset, row, 2, Qt.AlignmentFlag.AlignRight)
        quota.setColumnStretch(2, 1)
        layout.addLayout(quota)
        return card

    @property
    def dialog(self) -> bool:
        return self.settings.get("hud_appearance", "capsule") == "dialog"

    def _sync_tail_margins(self) -> None:
        top = TAIL if self.dialog and self._tail_dir == "up" else 0
        bottom = TAIL if self.dialog and self._tail_dir == "down" else 0
        self._outer.setContentsMargins(0, top, 0, bottom)

    def paintEvent(self, _event) -> None:
        if not self.dialog:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        body = QRectF(self.card.geometry()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(body, 10, 10)
        if self._tail_dir in {"up", "down"} and self._pet_rect is not None:
            tip_x = min(max(self._pet_rect.center().x() - self.x(), body.left() + 18), body.right() - 18)
            if self._tail_dir == "down":
                base_y, tip_y = body.bottom() - 1, body.bottom() + TAIL - 1
            else:
                base_y, tip_y = body.top() + 1, body.top() - TAIL + 1
            tail = QPainterPath()
            tail.addPolygon(QPolygonF([QPointF(tip_x - 8, base_y), QPointF(tip_x + 8, base_y), QPointF(tip_x, tip_y)]))
            path = path.united(tail)
        painter.setPen(QPen(QColor(18, 57, 91, 60), 1))
        painter.setBrush(QColor(250, 253, 255, 248))
        painter.drawPath(path)

    def apply_appearance(self) -> None:
        key = self.settings.get("hud_appearance", "capsule")
        style = APPEARANCES.get(key, APPEARANCES["capsule"])[1]
        self.pill.setVisible(not self.dialog)
        self.card.setVisible(self.dialog)
        self.card.setStyleSheet("QFrame#card{background:transparent;border:none;}")
        self._sync_tail_margins()
        self.pill.setStyleSheet(f"QFrame#pill{{{style}border-radius:9px;}}")
        locked = self.settings.get("hud_mode") == "floating" and self.settings.get("hud_locked", False)
        self.setCursor(Qt.CursorShape.ArrowCursor if locked else Qt.CursorShape.SizeAllCursor)
        self.adjustSize()

    # ----- data ---------------------------------------------------------------

    def update_snapshot(self, snap: LiveSnapshot | None) -> None:
        if snap is None:
            self.dot.setStyleSheet("color:#6B7280;font-size:8px;")
            self.card_dot.setStyleSheet("color:#9ca3af;font-size:8px;")
            self.card_title.setText("Antigravity 没有在运行")
            self.pill.setToolTip("等待连接 Antigravity 会话数据…\n右键可切换模式或退出")
            self.card.setToolTip(self.pill.toolTip())
            return
        self._conversations = snap.conversations
        self.dot.setStyleSheet(f"color:{'#22D3EE' if snap.running else '#34D399'};font-size:8px;")
        rate = snap.display_rate
        self.speed.setText(f"{rate:.1f} tok/s" if rate else "— tok/s")

        for label, bar, kind in ((self.quota_5h, self.quota_5h_bar, "5h"), (self.quota_week, self.quota_week_bar, "week")):
            q, _reset = snap.quota_for(kind)
            label.setText(f"{q:.0f}%" if q is not None else "—")
            color = "#9CA3AF" if q is None else "#F87171" if q <= 15 else "#FBBF24" if q <= 35 else "#34D399"
            label.setStyleSheet(_rgb(color) + "font-weight:600;")
            bar.set_value(q, color)
        self.cache.setText(f"{snap.cache_hit_pct:.1f}%")
        pct = snap.context_pct
        self.context.setText(f"{format_tokens(snap.context_used_tokens)} · {pct:.0f}%")
        ctx_color = "#F87171" if pct >= 80 else "#FBBF24" if pct >= 55 else "#C4B5FD"
        self.context.setStyleSheet(_rgb(ctx_color) + "font-weight:600;")

        self._update_card(snap)

        def fmt_rate(value: float | None) -> str:
            return f"{value:.1f} tok/s" if value else "—"

        ttft = f"{snap.latest_ttft_ms / 1000:.2f}s" if snap.latest_ttft_ms else "—"
        quota_lines = []
        for kind, caption in (("5h", "5 小时"), ("week", "每周")):
            q, reset = snap.quota_for(kind)
            if q is None:
                quota_lines.append(f"{caption} —")
            else:
                countdown = format_reset_countdown(reset)
                quota_lines.append(f"{caption} {q:.1f}%" + (f"（{countdown}）" if countdown else ""))
        quota_detail = " | ".join(quota_lines)
        mode = "[已锁定会话]" if self.settings.get("pinned_conversation") else "[自动跟随当前对话]"
        self.pill.setToolTip(
            "\n".join(
                (
                    f"会话: {snap.title} {mode}",
                    f"当前模型: {snap.selected_model_label}   最近一步: {snap.last_step_model_label}",
                    "────────────────────────────",
                    f"⚡ 生成速率: 最新 {self.speed.text()} | 会话均值 {fmt_rate(snap.avg_decode_tps)} | 端到端 {fmt_rate(snap.latest_e2e_tps)} | TTFT {ttft}",
                    f"🔋 限额剩余: {quota_detail}",
                    f"🎯 缓存命中: 累计 {snap.cache_hit_pct:.1f}%",
                    f"🧠 上下文占用: {snap.context_used_tokens:,} / {snap.context_limit_tokens:,} tokens ({pct:.2f}%)",
                    f"📊 会话累计总消耗: {format_tokens(snap.cumulative_total_tokens)} tokens",
                    f"数据来源: {SOURCE_NAMES.get(snap.source, snap.source)}",
                    "────────────────────────────",
                    "双击打开 Token Meter · 右键打开设置菜单",
                )
            )
        )
        self.card.setToolTip(self.pill.toolTip())
        self.adjustSize()
        if self._pet_rect is not None:
            self.follow_pet(
                self._pet_rect,
                getattr(self, "_sprite_top", None),
                prefer_above=getattr(self, "_prefer_above", False),
            )

    def _update_card(self, snap: LiveSnapshot) -> None:
        self.card_dot.setStyleSheet(f"color:{'#0ea5e9' if snap.running else '#10b981'};font-size:8px;")
        title = (snap.title or "当前会话").strip()
        metrics = self.card_title.fontMetrics()
        prefix = "生成中 · " if snap.running else ""
        self.card_title.setText(metrics.elidedText(prefix + title, Qt.TextElideMode.ElideRight, 238))
        pinned = "（已锁定）" if self.settings.get("pinned_conversation") else ""
        self.card_model.setText(f"{snap.model_for_form or '未知模型'} {pinned}".strip())
        rate = snap.display_rate
        self.card_values["speed"].setText(f"{rate:.1f} tok/s" if rate else "—")
        for key, kind in (("quota_5h", "5h"), ("quota_week", "week")):
            quota, reset = snap.quota_for(kind)
            self.card_values[key].setText(f"{quota:.0f}%" if quota is not None else "—")
            quota_color = "#9ca3af" if quota is None else "#dc2626" if quota <= 15 else "#d97706" if quota <= 35 else "#059669"
            self.card_values[key].setStyleSheet(f"color:{quota_color};font-weight:700;font-size:12px;")
            self.card_values[key + "_reset"].setText(format_reset_countdown(reset) if quota is not None else "暂无数据")
        self.card_values["cache"].setText(f"{snap.cache_hit_pct:.1f}%")
        pct = snap.context_pct
        self.card_values["context"].setText(f"{format_tokens(snap.context_used_tokens)}·{pct:.0f}%")
        ctx_color = "#dc2626" if pct >= 80 else "#d97706" if pct >= 55 else "#7c3aed"
        self.card_values["context"].setStyleSheet(f"color:{ctx_color};font-weight:700;font-size:12px;")

    # ----- positioning ------------------------------------------------------

    @property
    def attached(self) -> bool:
        return self.settings.get("hud_mode", "attached") != "floating"

    def follow_pet(self, pet_rect: QRect, sprite_top: int | None = None, *, prefer_above: bool = False) -> None:
        """Sit just below the pet; if the pet stands on the screen edge, sit
        above ``sprite_top`` (the caller moves it above the speech bubble while
        one is showing)."""
        self._pet_rect = QRect(pet_rect)
        self._sprite_top = sprite_top
        self._prefer_above = prefer_above
        if not self.attached:
            if self._tail_dir is not None:
                self._tail_dir = None
                self._sync_tail_margins()
                self.adjustSize()
                self.update()
            return
        screen = QApplication.screenAt(pet_rect.center()) or QApplication.primaryScreen()
        area = screen.availableGeometry() if screen else QRect(0, 0, 1920, 1080)
        below = pet_rect.bottom() + 2
        # Decide the side first so the size includes the tail on that side.
        place_below = not prefer_above and below + self.height() + TAIL <= area.bottom()
        tail_dir = "up" if place_below else "down"
        if tail_dir != self._tail_dir:
            self._tail_dir = tail_dir
            self._sync_tail_margins()
            self.adjustSize()
        x = pet_rect.center().x() - self.width() // 2
        if place_below:
            y = below
        else:
            top = sprite_top if sprite_top is not None else pet_rect.top()
            y = top - self.height() - 2
        x = max(area.left(), min(x, area.right() - self.width() + 1))
        y = max(area.top(), min(y, area.bottom() - self.height() + 1))
        if (x, y) != (self.x(), self.y()):
            self.move(x, y)
        if self.dialog:
            self.update()  # the tail tracks the pet's centre

    def restore_floating_position(self) -> None:
        x, y = self.settings.get("hud_x"), self.settings.get("hud_y")
        screen = QApplication.primaryScreen()
        area = screen.availableGeometry() if screen else QRect(0, 0, 1920, 1080)
        if isinstance(x, int) and isinstance(y, int) and area.adjusted(-60, -10, 0, 0).contains(QPoint(x, y)):
            self.move(x, y)
        else:
            self.move(area.center().x() - self.width() // 2, area.bottom() - 78)

    # ----- mouse ------------------------------------------------------------

    @property
    def interactive(self) -> bool:
        return self._hovered or self._menu_open

    def enterEvent(self, event) -> None:
        self._hovered = True
        self.interaction_changed.emit(True)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hovered = False
        self.interaction_changed.emit(self.interactive)
        super().leaveEvent(event)

    def hideEvent(self, event) -> None:
        self._hovered = False
        super().hideEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and not self.attached and not self.settings.get("hud_locked"):
            self._drag_from = event.globalPosition().toPoint() - self.pos()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag_from is not None:
            self.move(event.globalPosition().toPoint() - self._drag_from)

    def mouseReleaseEvent(self, _event: QMouseEvent) -> None:
        if self._drag_from is not None:
            self._drag_from = None
            self.settings["hud_x"], self.settings["hud_y"] = self.x(), self.y()
            self.settings_changed.emit()

    def mouseDoubleClickEvent(self, _event: QMouseEvent) -> None:
        self._on_open_meter()

    def contextMenuEvent(self, event) -> None:
        menu = QMenu(self)

        def toggle(text: str, checked: bool, callback: Callable[[], None]) -> QAction:
            action = menu.addAction(text)
            action.setCheckable(True)
            action.setChecked(checked)
            action.triggered.connect(callback)
            return action

        toggle("跟随桌宠", self.attached, lambda: self._set_mode("attached"))
        toggle("独立悬浮（可拖到输入框旁）", not self.attached, lambda: self._set_mode("floating"))
        menu.addSeparator()
        pinned = self.settings.get("pinned_conversation", "")
        toggle("自动跟随最新活跃对话", not pinned, lambda: self._on_pin(""))
        if self._conversations:
            sub = menu.addMenu("指定监控某个会话")
            for cid, title in self._conversations:
                act = sub.addAction(title or cid[:8])
                act.setCheckable(True)
                act.setChecked(cid == pinned)
                act.triggered.connect(lambda _c=False, value=cid: self._on_pin(value))
        menu.addSeparator()
        style = menu.addMenu("外观融合模式")
        group = QActionGroup(style)
        for key, (label, _css) in APPEARANCES.items():
            act = style.addAction(label)
            act.setCheckable(True)
            act.setChecked(self.settings.get("hud_appearance", "capsule") == key)
            group.addAction(act)
            act.triggered.connect(lambda _c=False, value=key: self._set_appearance(value))
        if not self.attached:
            toggle("锁定当前坐标（防误拖动）", bool(self.settings.get("hud_locked")), self._toggle_lock)
        if self._on_visibility is not None:
            vis_menu = menu.addMenu("显示模式")
            vis_group = QActionGroup(vis_menu)
            current_vis = self.settings.get("hud_visibility") or (
                "show" if self.settings.get("hud_enabled", True) else "hide"
            )
            for key, label in (("show", "显示"), ("auto", "自动"), ("hide", "隐藏")):
                act = vis_menu.addAction(label)
                act.setCheckable(True)
                act.setChecked(current_vis == key)
                vis_group.addAction(act)
                act.triggered.connect(lambda _c=False, value=key: self._on_visibility(value))
        menu.addAction("打开 Token Meter", self._on_open_meter)
        menu.addAction("立即刷新数据", self._on_refresh)
        menu.addSeparator()
        menu.addAction("隐藏悬浮条", self._on_hide)
        self._menu_open = True
        self.interaction_changed.emit(True)
        try:
            menu.exec(event.globalPos())
        finally:
            self._menu_open = False
            self.interaction_changed.emit(self.interactive)

    def _set_mode(self, mode: str) -> None:
        self.settings["hud_mode"] = mode
        if mode == "floating":
            if not isinstance(self.settings.get("hud_x"), int):
                self.settings["hud_x"], self.settings["hud_y"] = self.x(), self.y()
            self.restore_floating_position()
        elif self._pet_rect is not None:
            self.follow_pet(self._pet_rect)
        self.apply_appearance()
        self.settings_changed.emit()

    def _set_appearance(self, key: str) -> None:
        self.settings["hud_appearance"] = key
        self.apply_appearance()
        self.settings_changed.emit()

    def _toggle_lock(self) -> None:
        self.settings["hud_locked"] = not bool(self.settings.get("hud_locked"))
        self.apply_appearance()
        self.settings_changed.emit()
