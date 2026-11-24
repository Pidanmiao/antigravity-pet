"""A compact, pet-adjacent control surface with progressive disclosure."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QPoint, QRect, QTimer, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QApplication,
    QSizePolicy,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFrame,
    QGraphicsDropShadowEffect,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)


class PetControlPanel(QWidget):
    """Quick interaction first; secondary settings are explicitly revealed."""

    def __init__(
        self,
        *,
        on_mode: Callable[[str], None],
        on_size: Callable[[float], None],
        on_feed: Callable[[], None],
        on_say: Callable[[], None],
        on_snap: Callable[[], None],
        on_awareness: Callable[[bool], None],
        on_read_window_title: Callable[[bool], None],
        on_idle_detection: Callable[[bool], None],
        on_topmost: Callable[[bool], None],
        on_fullscreen_hide: Callable[[bool], None],
        on_passthrough: Callable[[bool], None],
        on_autostart: Callable[[bool], None],
        on_activity_level: Callable[[str], None],
        on_walk_speed: Callable[[float], None],
        on_reduced_motion: Callable[[bool], None],
        on_hover_pause: Callable[[bool], None],
        on_desktop_surfaces: Callable[[bool], None],
        on_action: Callable[[str], None],
        on_hide: Callable[[], None],
        on_privacy: Callable[[], None],
        on_quit: Callable[[], None],
        on_form: Callable[[str], None] | None = None,
        on_follow_model: Callable[[bool], None] | None = None,
        on_open_meter: Callable[[], None] | None = None,
        on_hud: Callable[[bool], None] | None = None,
        on_hud_visibility: Callable[[str], None] | None = None,
        on_hud_auto_hide_seconds: Callable[[int], None] | None = None,
        on_autonomy: Callable[[bool], None] | None = None,
        forms: tuple[tuple[str, str], ...] = (("carbon", "Carbon"), ("hydergon", "Hydrogen"), ("neon", "Neon")),
    ) -> None:
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowStaysOnTopHint,
        )
        self.setObjectName("petControlPanel")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedWidth(258)
        self._mode_buttons: dict[str, QToolButton] = {}
        self._size_buttons: dict[float, QToolButton] = {}
        self._activity_buttons: dict[str, QToolButton] = {}
        self._speed_buttons: dict[float, QToolButton] = {}
        self._mode_group = QButtonGroup(self)
        self._size_group = QButtonGroup(self)
        self._activity_group = QButtonGroup(self)
        self._speed_group = QButtonGroup(self)
        self._form_buttons: dict[str, QToolButton] = {}
        self._form_group = QButtonGroup(self)
        self._expanded = False
        self._pet_rect: QRect | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 8)
        self._root_layout = root
        surface = QFrame()
        surface.setObjectName("controlSurface")
        shadow = QGraphicsDropShadowEffect(surface)
        shadow.setBlurRadius(22)
        shadow.setOffset(0, 6)
        shadow.setColor(QColor(3, 19, 37, 130))
        surface.setGraphicsEffect(shadow)
        root.addWidget(surface)

        layout = QVBoxLayout(surface)
        layout.setContentsMargins(14, 13, 14, 13)
        layout.setSpacing(9)

        header = QHBoxLayout()
        header.setSpacing(8)
        title = QLabel("小A")
        title.setObjectName("panelTitle")
        header.addWidget(title)
        self.title_label = title
        self.state_label = QLabel("待机")
        self.state_label.setObjectName("stateLabel")
        header.addWidget(self.state_label)
        header.addStretch(1)
        close = QToolButton()
        close.setObjectName("closeButton")
        close.setText("×")
        close.setToolTip("关闭")
        close.setAccessibleName("关闭")
        close.clicked.connect(self.hide)
        header.addWidget(close)
        layout.addLayout(header)

        quick_row = QHBoxLayout()
        quick_row.setSpacing(5)
        for label, callback in (
            ("投喂", on_feed),
            ("说话", on_say),
            ("开心", lambda: on_action("HAPPY")),
            ("休息", lambda: on_action("SLEEPING")),
        ):
            button = self._quick_button(label)
            button.clicked.connect(callback)
            quick_row.addWidget(button)
        layout.addLayout(quick_row)

        layout.addWidget(self._divider())
        mode_label = QLabel("移动")
        mode_label.setObjectName("compactLabel")
        layout.addWidget(mode_label)
        mode_row = QHBoxLayout()
        mode_row.setSpacing(5)
        for label, value in (("散步", "wander"), ("跟随", "follow"), ("静止", "still")):
            button = self._choice_button(label)
            button.clicked.connect(lambda _checked=False, key=value: on_mode(key))
            self._mode_group.addButton(button)
            self._mode_buttons[value] = button
            mode_row.addWidget(button)
        layout.addLayout(mode_row)

        form_label = QLabel("形态")
        form_label.setObjectName("compactLabel")
        layout.addWidget(form_label)
        # Two columns so four (or more) form names never get elided.
        form_row = QGridLayout()
        form_row.setHorizontalSpacing(5)
        form_row.setVerticalSpacing(5)
        for index, (value, label) in enumerate(forms):
            button = self._choice_button(label)
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            if on_form is not None:
                button.clicked.connect(lambda _checked=False, key=value: on_form(key))
            self._form_group.addButton(button)
            self._form_buttons[value] = button
            form_row.addWidget(button, index // 2, index % 2)
        layout.addLayout(form_row)
        agy_row = QGridLayout()
        agy_row.setHorizontalSpacing(8)
        agy_row.setVerticalSpacing(6)
        agy_row.setColumnStretch(0, 1)
        agy_row.setColumnStretch(1, 1)
        self.follow_model = self._toggle("跟随模型", on_follow_model or (lambda _enabled: None))
        self.follow_model.setToolTip("根据 Antigravity 当前使用的模型（Carbon / Hydrogen / Neon / Claude）自动切换形态")
        self.autonomy = self._toggle("自主行为", on_autonomy or (lambda _enabled: None))
        self.autonomy.setToolTip("模型干活时陪着干活、跳上 Antigravity 窗口、闲久了打盹、限额低会犯困、久坐提醒")

        hud_row = QHBoxLayout()
        hud_row.setContentsMargins(0, 0, 0, 0)
        hud_row.setSpacing(5)
        hud_label = QLabel("HUD")
        hud_label.setObjectName("inlineLabel")
        self.hud_visibility = QComboBox()
        self.hud_visibility.setObjectName("hudSelect")
        self.hud_visibility.setToolTip("选择 HUD 悬浮条显示方式：显示 / 自动（悬停或点击桌宠后显示）/ 隐藏")
        for label, value in (("显示", "show"), ("自动", "auto"), ("隐藏", "hide")):
            self.hud_visibility.addItem(label, value)
        self.hud_visibility.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._on_hud = on_hud
        self._on_hud_visibility = on_hud_visibility
        self._on_hud_auto_hide_seconds = on_hud_auto_hide_seconds
        self.hud_visibility.currentIndexChanged.connect(self._handle_hud_visibility_changed)
        hud_row.addWidget(hud_label)
        hud_row.addWidget(self.hud_visibility, 1)

        self._hud_delay_row = QWidget()
        delay_policy = self._hud_delay_row.sizePolicy()
        delay_policy.setRetainSizeWhenHidden(True)
        self._hud_delay_row.setSizePolicy(delay_policy)
        delay_layout = QHBoxLayout(self._hud_delay_row)
        delay_layout.setContentsMargins(0, 0, 0, 0)
        delay_layout.setSpacing(5)
        delay_label = QLabel("时长")
        delay_label.setObjectName("inlineLabel")
        self.hud_delay = QSpinBox()
        self.hud_delay.setObjectName("hudDelaySpin")
        self.hud_delay.setRange(1, 60)
        self.hud_delay.setValue(5)
        self.hud_delay.setSuffix(" 秒")
        self.hud_delay.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.hud_delay.setToolTip("自动模式下，鼠标离开桌宠与 HUD 后延迟隐藏的秒数")
        self.hud_delay.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.hud_delay.valueChanged.connect(self._handle_hud_delay_changed)
        delay_layout.addWidget(delay_label)
        delay_layout.addWidget(self.hud_delay, 1)
        self._hud_delay_row.setVisible(False)

        agy_row.addWidget(self.follow_model, 0, 0)
        agy_row.addLayout(hud_row, 0, 1)
        agy_row.addWidget(self.autonomy, 1, 0)
        agy_row.addWidget(self._hud_delay_row, 1, 1)
        layout.addLayout(agy_row)
        meter = self._quick_button("Token Meter 面板")
        if on_open_meter is not None:
            meter.clicked.connect(on_open_meter)
        layout.addWidget(meter)

        footer = QHBoxLayout()
        footer.setSpacing(5)
        self.more_button = QToolButton()
        self.more_button.setObjectName("moreButton")
        self.more_button.setText("更多设置")
        self.more_button.clicked.connect(self._toggle_advanced)
        footer.addWidget(self.more_button, 1)
        hide = QToolButton()
        hide.setObjectName("moreButton")
        hide.setText("隐藏")
        hide.clicked.connect(on_hide)
        footer.addWidget(hide)
        layout.addLayout(footer)

        self._advanced = QWidget()
        advanced_layout = QVBoxLayout(self._advanced)
        advanced_layout.setContentsMargins(0, 1, 0, 0)
        advanced_layout.setSpacing(8)
        advanced_layout.addWidget(self._divider())

        size_label = QLabel("大小")
        size_label.setObjectName("compactLabel")
        advanced_layout.addWidget(size_label)
        size_row = QHBoxLayout()
        size_row.setSpacing(5)
        for label, value in (("小", 0.55), ("中", 0.70), ("大", 0.90)):
            button = self._choice_button(label)
            button.clicked.connect(lambda _checked=False, size=value: on_size(size))
            self._size_group.addButton(button)
            self._size_buttons[value] = button
            size_row.addWidget(button)
        advanced_layout.addLayout(size_row)

        action_row = QHBoxLayout()
        action_row.setSpacing(5)
        for label, action in (("发呆", "THINKING"), ("扫地", "SWEEPING"), ("爬窗口", "CLIMB"), ("坐窗沿", "EDGE_SIT")):
            button = self._quick_button(label)
            button.clicked.connect(lambda _checked=False, value=action: on_action(value))
            action_row.addWidget(button)
        action_row.addStretch(1)
        advanced_layout.addLayout(action_row)

        rhythm_label = QLabel("陪伴节奏")
        rhythm_label.setObjectName("compactLabel")
        advanced_layout.addWidget(rhythm_label)
        rhythm_row = QHBoxLayout()
        rhythm_row.setSpacing(5)
        for label, value in (("安静", "calm"), ("适中", "balanced"), ("活泼", "lively")):
            button = self._choice_button(label)
            button.clicked.connect(lambda _checked=False, key=value: on_activity_level(key))
            self._activity_group.addButton(button)
            self._activity_buttons[value] = button
            rhythm_row.addWidget(button)
        advanced_layout.addLayout(rhythm_row)

        speed_label = QLabel("走路速度")
        speed_label.setObjectName("compactLabel")
        advanced_layout.addWidget(speed_label)
        speed_row = QHBoxLayout()
        speed_row.setSpacing(5)
        for label, value in (("慢", 64.0), ("适中", 82.0), ("快", 104.0)):
            button = self._choice_button(label)
            button.clicked.connect(lambda _checked=False, speed=value: on_walk_speed(speed))
            self._speed_group.addButton(button)
            self._speed_buttons[value] = button
            speed_row.addWidget(button)
        advanced_layout.addLayout(speed_row)

        self.awareness = self._toggle("本地感知", on_awareness)
        self.topmost = self._toggle("窗口置顶", on_topmost)
        self.read_window_title = self._toggle("读取标题", on_read_window_title)
        self.fullscreen_hide = self._toggle("全屏隐藏", on_fullscreen_hide)
        self.idle_detection = self._toggle("检测空闲", on_idle_detection)
        self.passthrough = self._toggle("鼠标穿透", on_passthrough)
        self.autostart = self._toggle("开机自启", on_autostart)
        self.reduced_motion = self._toggle("减少动态", on_reduced_motion)
        self.hover_pause = self._toggle("悬停暂停", on_hover_pause)
        self.desktop_surfaces = self._toggle("窗口落脚", on_desktop_surfaces)
        toggle_grid = QGridLayout()
        toggle_grid.setHorizontalSpacing(10)
        toggle_grid.setVerticalSpacing(2)
        for index, toggle in enumerate(
            (
                self.awareness,
                self.topmost,
                self.read_window_title,
                self.fullscreen_hide,
                self.idle_detection,
                self.passthrough,
                self.autostart,
                self.reduced_motion,
                self.hover_pause,
                self.desktop_surfaces,
            )
        ):
            toggle_grid.addWidget(toggle, index // 2, index % 2)
        advanced_layout.addLayout(toggle_grid)

        tools = QHBoxLayout()
        tools.setSpacing(5)
        for label, callback, object_name in (
            ("回到屏幕内", on_snap, "utilityButton"),
            ("隐私", on_privacy, "utilityButton"),
            ("退出", on_quit, "dangerButton"),
        ):
            button = QPushButton(label)
            button.setObjectName(object_name)
            button.clicked.connect(callback)
            tools.addWidget(button)
        advanced_layout.addLayout(tools)
        self._advanced.setVisible(False)
        layout.addWidget(self._advanced)

        self.setStyleSheet(
            "#controlSurface{background:#141327;border-radius:14px;}"
            "#panelTitle{color:#f6fbff;font-size:16px;font-weight:700;}"
            "#stateLabel{padding:2px 7px;background:#2a2550;border-radius:8px;color:#d9d2ff;font-size:10px;font-weight:600;}"
            "QLabel#compactLabel{color:#b8b0e8;font-size:10px;font-weight:600;}"
            "QFrame#divider{background:rgba(200,190,255,38);min-height:1px;max-height:1px;}"
            "QToolButton#quickButton{min-height:31px;padding:0 7px;background:#25214a;border:none;border-radius:9px;"
            "color:#ece8ff;font-size:11px;font-weight:600;}"
            "QToolButton#quickButton:hover{background:#36306b;color:#ffffff;}"
            "QToolButton#choiceButton{min-height:29px;padding:0 8px;background:#1d1a3a;border:none;border-radius:8px;"
            "color:#ddd8ff;font-size:11px;font-weight:600;}"
            "QToolButton#choiceButton:hover{background:#2e2860;}"
            "QToolButton#choiceButton:checked{background:#8b7cf6;color:#0f0c24;}"
            "QToolButton#moreButton{min-height:28px;padding:0 8px;background:transparent;border:none;"
            "border-radius:8px;color:#bdb5ee;font-size:11px;font-weight:600;}"
            "QToolButton#closeButton{min-width:28px;min-height:28px;padding:0;background:transparent;border:none;"
            "border-radius:8px;color:#f6fbff;font-size:16px;font-weight:700;}"
            "QToolButton#moreButton:hover,QToolButton#closeButton:hover{background:#2a2550;color:#ffffff;}"
            "QCheckBox{min-height:20px;color:#e4e0ff;font-size:11px;spacing:6px;}"
            "QCheckBox::indicator{width:26px;height:14px;border-radius:7px;background:#2e2a55;}"
            "QCheckBox::indicator:checked{background:#8b7cf6;}"
            "QLabel#inlineLabel{color:#e4e0ff;font-size:11px;}"
            "QComboBox#hudSelect,QSpinBox#hudDelaySpin{min-height:22px;max-height:22px;padding:0 6px;background:#1d1a3a;"
            "border:1px solid #352f66;border-radius:6px;color:#ece8ff;font-size:11px;font-weight:600;}"
            "QComboBox#hudSelect:hover,QSpinBox#hudDelaySpin:hover{border-color:#8b7cf6;background:#25214a;}"
            "QComboBox#hudSelect::drop-down{border:none;width:16px;}"
            "QComboBox#hudSelect::down-arrow{image:none;width:0;height:0;border-left:4px solid transparent;"
            "border-right:4px solid transparent;border-top:5px solid #d9d2ff;}"
            "QComboBox#hudSelect QAbstractItemView{background:#1b1836;color:#ece8ff;border:1px solid #3c3572;"
            "selection-background-color:#8b7cf6;selection-color:#0f0c24;outline:0;}"
            "QSpinBox#hudDelaySpin::up-button,QSpinBox#hudDelaySpin::down-button{width:14px;border:none;background:transparent;}"
            "QSpinBox#hudDelaySpin::up-arrow{image:none;width:0;height:0;border-left:3px solid transparent;"
            "border-right:3px solid transparent;border-bottom:4px solid #d9d2ff;}"
            "QSpinBox#hudDelaySpin::down-arrow{image:none;width:0;height:0;border-left:3px solid transparent;"
            "border-right:3px solid transparent;border-top:4px solid #d9d2ff;}"
            "QPushButton#utilityButton,QPushButton#dangerButton{min-height:29px;padding:0 8px;border:none;border-radius:8px;"
            "font-size:11px;font-weight:600;}"
            "QPushButton#utilityButton{background:#e9e5ff;color:#1d1a3a;}"
            "QPushButton#utilityButton:hover{background:#ffffff;}"
            "QPushButton#dangerButton{background:#874457;color:#fff5f6;}"
            "QPushButton#dangerButton:hover{background:#aa566c;}"
        )

    @staticmethod
    def _divider() -> QFrame:
        divider = QFrame()
        divider.setObjectName("divider")
        return divider

    @staticmethod
    def _choice_button(text: str) -> QToolButton:
        button = QToolButton()
        button.setObjectName("choiceButton")
        button.setText(text)
        button.setCheckable(True)
        button.setAutoExclusive(True)
        return button

    @staticmethod
    def _quick_button(text: str) -> QToolButton:
        button = QToolButton()
        button.setObjectName("quickButton")
        button.setText(text)
        return button

    @staticmethod
    def _toggle(text: str, callback: Callable[[bool], None]) -> QCheckBox:
        checkbox = QCheckBox(text)
        checkbox.clicked.connect(callback)
        return checkbox

    def _handle_hud_visibility_changed(self, index: int) -> None:
        value = str(self.hud_visibility.itemData(index) or "show")
        self._hud_delay_row.setVisible(value == "auto")
        if self._on_hud_visibility is not None:
            self._on_hud_visibility(value)
        elif self._on_hud is not None:
            self._on_hud(value != "hide")

    def _handle_hud_delay_changed(self, seconds: int) -> None:
        if self._on_hud_auto_hide_seconds is not None:
            self._on_hud_auto_hide_seconds(int(seconds))

    def _toggle_advanced(self) -> None:
        self._expanded = not self._expanded
        self._advanced.setVisible(self._expanded)
        self.more_button.setText("收起设置" if self._expanded else "更多设置")
        # Hiding a child queues a layout recalculation.  Resize on the next Qt
        # turn so the compact hint is already current instead of retaining the
        # expanded tool window's old height.
        QTimer.singleShot(0, self._fit_to_content)

    def _fit_to_content(self) -> None:
        # A top-level transparent Tool window does not reliably shrink from
        # ``adjustSize`` after a child is hidden on Windows.  Re-activate the
        # layout and apply its fresh height explicitly, otherwise the old empty
        # glass area remains below the compact panel.
        self._root_layout.invalidate()
        self._root_layout.activate()
        target_height = self.sizeHint().height()
        # Qt may still hold the expanded layout's minimum height until the next
        # event cycle, so release that stale bound before asking the window to
        # shrink to its current size hint.
        self.setMinimumHeight(0)
        self.resize(self.width(), target_height)
        if self._pet_rect is not None:
            self._move_near_pet(self._pet_rect)

    def sync(
        self,
        *,
        mode: str,
        size: float,
        awareness: bool,
        read_window_title: bool,
        idle_detection: bool,
        topmost: bool,
        fullscreen_hide: bool,
        passthrough: bool,
        autostart: bool,
        activity_level: str,
        walk_speed: float,
        reduced_motion: bool,
        hover_pause: bool,
        desktop_surfaces: bool,
        state_name: str,
        form: str | None = None,
        follow_model: bool | None = None,
        hud_enabled: bool | None = None,
        hud_visibility: str | None = None,
        hud_auto_hide_seconds: int | None = None,
        autonomy: bool | None = None,
    ) -> None:
        if autonomy is not None:
            self.autonomy.setChecked(autonomy)
        if form in self._form_buttons:
            self._form_buttons[form].setChecked(True)
        if follow_model is not None:
            self.follow_model.setChecked(follow_model)
        resolved_vis = hud_visibility
        if resolved_vis is None and hud_enabled is not None:
            resolved_vis = "show" if hud_enabled else "hide"
        if resolved_vis is not None:
            idx = self.hud_visibility.findData(resolved_vis)
            if idx >= 0:
                self.hud_visibility.blockSignals(True)
                self.hud_visibility.setCurrentIndex(idx)
                self.hud_visibility.blockSignals(False)
            self._hud_delay_row.setVisible(resolved_vis == "auto")
        if hud_auto_hide_seconds is not None:
            clamped = max(1, min(60, int(hud_auto_hide_seconds)))
            if not self.hud_delay.hasFocus():
                self.hud_delay.blockSignals(True)
                self.hud_delay.setValue(clamped)
                self.hud_delay.blockSignals(False)
        if mode in self._mode_buttons:
            self._mode_buttons[mode].setChecked(True)
        closest_size = min(self._size_buttons, key=lambda value: abs(value - size))
        self._size_buttons[closest_size].setChecked(True)
        if activity_level in self._activity_buttons:
            self._activity_buttons[activity_level].setChecked(True)
        closest_speed = min(self._speed_buttons, key=lambda value: abs(value - walk_speed))
        self._speed_buttons[closest_speed].setChecked(True)
        self.awareness.setChecked(awareness)
        self.read_window_title.setChecked(read_window_title)
        self.idle_detection.setChecked(idle_detection)
        self.topmost.setChecked(topmost)
        self.fullscreen_hide.setChecked(fullscreen_hide)
        self.passthrough.setChecked(passthrough)
        self.autostart.setChecked(autostart)
        self.reduced_motion.setChecked(reduced_motion)
        self.hover_pause.setChecked(hover_pause)
        self.desktop_surfaces.setChecked(desktop_surfaces)
        self.state_label.setText(state_name)

    def popup_near_pet(self, pet_rect: QRect) -> None:
        self._pet_rect = QRect(pet_rect)
        self.adjustSize()
        self._move_near_pet(self._pet_rect)
        self.show()
        self.raise_()
        self.activateWindow()
        self.setFocus()

    def popup_at(self, point: QPoint) -> None:
        """Fallback used from the tray when there is no visible pet anchor."""

        self._pet_rect = None
        self.adjustSize()
        screen = QApplication.screenAt(point) or QApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            self.move(self._clamp(QPoint(point.x() + 12, point.y() + 12), area))
        else:
            self.move(point + QPoint(12, 12))
        self.show()
        self.raise_()
        self.activateWindow()
        self.setFocus()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.hide()
            event.accept()
            return
        super().keyPressEvent(event)

    def contextMenuEvent(self, event) -> None:
        self.hide()
        event.accept()

    def _move_near_pet(self, pet_rect: QRect) -> None:
        screen = QApplication.screenAt(pet_rect.center()) or QApplication.primaryScreen()
        if screen is None:
            self.move(pet_rect.topRight() + QPoint(14, 8))
            return
        area = screen.availableGeometry()
        gap = 14
        aligned_y = pet_rect.top() + max(0, min(24, pet_rect.height() - self.height()))
        centered_x = pet_rect.center().x() - self.width() // 2
        candidates = (
            QPoint(pet_rect.right() + gap, aligned_y),
            QPoint(pet_rect.left() - self.width() - gap, aligned_y),
            QPoint(centered_x, pet_rect.top() - self.height() - gap),
            QPoint(centered_x, pet_rect.bottom() + gap),
        )
        for candidate in candidates:
            rect = QRect(candidate, self.size())
            if area.contains(rect):
                self.move(candidate)
                return

        # On a very small screen, pick the clamped candidate that hides the
        # least of the pet rather than anchoring blindly over its centre.
        def overlap(candidate: QPoint) -> int:
            rect = QRect(self._clamp(candidate, area), self.size())
            intersection = rect.intersected(pet_rect)
            return intersection.width() * intersection.height()

        self.move(min(candidates, key=overlap))
        self.move(self._clamp(self.pos(), area))

    def _clamp(self, point: QPoint, area: QRect) -> QPoint:
        return QPoint(
            min(max(area.left(), point.x()), area.right() - self.width() + 1),
            min(max(area.top(), point.y()), area.bottom() - self.height() + 1),
        )
