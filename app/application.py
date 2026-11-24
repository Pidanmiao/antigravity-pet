from __future__ import annotations

import gc
import logging
import os
import sys

from PySide6 import __version__ as PYSIDE_VERSION
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from app.paths import AppPaths
from app.recovery import CrashRecovery, DiagnosticHooks, RecoveryStatus, configure_logging, flush_logging
from app.single_instance import SingleInstance
from app.version import VERSION
from app.windows_integration import register_application_restart
from pet.window import PetWindow
from settings.config import ConfigManager


def run(argv: list[str] | None = None) -> int:
    """Create the Qt application, then hand control to the pet window."""
    application = QApplication(argv if argv is not None else sys.argv)
    application.setQuitOnLastWindowClosed(False)
    register_application_restart()
    instance = SingleInstance()
    if not instance.acquire():
        return 0

    paths = AppPaths.discover()
    paths.ensure_user_dirs()
    paths.migrate_legacy_user_data()
    configure_logging(paths.log_path)
    recovery = CrashRecovery(
        paths.recovery_path,
        paths.crash_reports_dir,
        version=VERSION,
    )
    recovery_status = recovery.begin()
    diagnostics = DiagnosticHooks(recovery)
    diagnostics.install()
    diagnostics.install_qt_handler()
    logger = logging.getLogger(__name__)
    logger.info(
        "AGY Pet starting version=%s pid=%d python=%s pyside6=%s session=%s unclean_exit=%s crashes=%d safe_mode=%s",
        VERSION,
        os.getpid(),
        sys.version.split()[0],
        PYSIDE_VERSION,
        recovery.session_id,
        recovery_status.unclean_exit,
        recovery_status.consecutive_crashes,
        recovery_status.safe_mode,
    )
    flush_logging()

    def handle_fatal(
        exc_type: type[BaseException],
        error: BaseException,
        tb: object,
    ) -> None:
        report = recovery.record_exception(exc_type, error, tb)
        logger.critical("Fatal error saved to %s", report)
        flush_logging()
        try:
            from PySide6.QtWidgets import QMessageBox

            QMessageBox.critical(
                None,
                "AGY 桌宠需要重新启动",
                "桌宠遇到了意外，已经保存恢复信息。\n"
                "重新打开会恢复上次状态；若连续失败，将自动进入安全模式。\n\n"
                f"诊断记录：{report}",
            )
        finally:
            application.exit(1)

    sys.excepthook = handle_fatal
    config = ConfigManager(paths.config_path)
    if recovery_status.safe_mode:
        config.apply_runtime_overrides(
            {
                "mode": "still",
                "awareness": {"enabled": False},
                "comfort": {
                    "reduced_motion": True,
                    "desktop_surfaces": False,
                },
            }
        )
    # Keep cyclic garbage collection strictly on the main Qt GUI thread.
    # Under Python 3.14 + PySide6 6.12 on Windows, cyclic GC triggered inside
    # background worker threads (e.g. agy-telemetry) can race with QPainter /
    # Shiboken wrappers on the GUI thread and cause native access violations.
    gc_was_enabled = gc.isenabled()
    gc.disable()
    try:
        window = PetWindow(paths, config, recovery_status=recovery_status)
    except BaseException:
        exc_type, error, tb = sys.exc_info()
        if exc_type is not None and error is not None:
            recovery.record_exception(exc_type, error, tb)
        if gc_was_enabled:
            gc.enable()
        diagnostics.close()
        instance.close()
        raise
    instance.set_activation_handler(window.activate_from_shortcut)

    def _on_about_to_quit() -> None:
        logger.info("AGY Pet shutting down cleanly session=%s", recovery.session_id)
        instance.close()
        recovery.finish()
        diagnostics.close()

    application.aboutToQuit.connect(_on_about_to_quit)

    gc_timer = QTimer(application)
    gc_timer.timeout.connect(gc.collect)
    gc_timer.start(15_000)

    heartbeat = QTimer(application)
    heartbeat.timeout.connect(recovery.heartbeat)
    heartbeat.start(15_000)

    # Packaging/installer smoke tests need a deterministic, clean exit without
    # automating a frameless desktop window.  The hook is inert for normal users
    # and deliberately capped so an accidental value cannot create a long job.
    smoke_exit_raw = os.environ.get("DAFEIYU_SMOKE_EXIT_MS")
    if smoke_exit_raw:
        try:
            smoke_exit_ms = max(250, min(60_000, int(smoke_exit_raw)))
        except ValueError:
            smoke_exit_ms = 0
        if smoke_exit_ms:
            QTimer.singleShot(smoke_exit_ms, application.quit)
    try:
        return application.exec()
    finally:
        if gc_was_enabled:
            gc.enable()
        diagnostics.close()
