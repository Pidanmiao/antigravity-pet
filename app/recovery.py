"""Crash markers, rotating logs and safe recovery for the desktop pet."""

from __future__ import annotations

import faulthandler
import json
import logging
import os
import sys
import threading
import time
import traceback
import uuid
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable, TextIO


@dataclass(frozen=True, slots=True)
class RecoveryStatus:
    unclean_exit: bool = False
    consecutive_crashes: int = 0
    safe_mode: bool = False


def configure_logging(path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if any(getattr(handler, "baseFilename", None) == str(path) for handler in root.handlers):
        return
    handler = RotatingFileHandler(
        path,
        maxBytes=1_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root.addHandler(handler)


MAX_CRASH_REPORTS = 20
MAX_SESSION_ERROR_REPORTS = 20
FAULTHANDLER_MAX_BYTES = 512 * 1024


class CrashRecovery:
    """Detect unclean exits and keep a small atomic session marker."""

    def __init__(
        self,
        marker_path: Path,
        crash_reports_dir: Path,
        *,
        version: str,
        clock: Callable[[], float] = time.time,
        max_crash_reports: int = MAX_CRASH_REPORTS,
        max_session_error_reports: int = MAX_SESSION_ERROR_REPORTS,
    ) -> None:
        self.marker_path = Path(marker_path)
        self.crash_reports_dir = Path(crash_reports_dir)
        self.version = str(version)
        self.clock = clock
        self.max_crash_reports = max(1, int(max_crash_reports))
        self.max_session_error_reports = max(1, int(max_session_error_reports))
        self.session_id = uuid.uuid4().hex
        self.status = RecoveryStatus()
        self._fatal = False
        self._last_report: Path | None = None
        self._error_report_count = 0

    @property
    def fatal(self) -> bool:
        return self._fatal

    @property
    def last_report(self) -> Path | None:
        return self._last_report

    def begin(self) -> RecoveryStatus:
        previous = self._read_marker()
        unclean = bool(previous.get("active"))
        previous_count = self._safe_int(previous.get("consecutive_crashes"))
        count = previous_count + 1 if unclean else 0
        self.status = RecoveryStatus(
            unclean_exit=unclean,
            consecutive_crashes=count,
            safe_mode=count >= 2,
        )
        now = self.clock()
        self._write_marker(
            {
                "active": True,
                "session_id": self.session_id,
                "version": self.version,
                "started_at": now,
                "last_heartbeat": now,
                "consecutive_crashes": count,
                "safe_mode": self.status.safe_mode,
            }
        )
        return self.status

    def heartbeat(self) -> None:
        marker = self._read_marker()
        if marker.get("session_id") != self.session_id:
            return
        marker["last_heartbeat"] = self.clock()
        self._write_marker(marker)

    def record_exception(
        self,
        exc_type: type[BaseException],
        error: BaseException,
        tb: object,
        *,
        context: str = "",
    ) -> Path:
        self._fatal = True
        if self._last_report is not None:
            return self._last_report
        self.crash_reports_dir.mkdir(parents=True, exist_ok=True)
        timestamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(self.clock()))
        report = self.crash_reports_dir / f"crash-{timestamp}.log"
        formatted = "".join(traceback.format_exception(exc_type, error, tb))
        ctx_line = f"context={context}\n" if context else ""
        report.write_text(
            f"Antigravity Desktop Pet {self.version}\n"
            f"session={self.session_id}\n"
            f"pid={os.getpid()}\n"
            f"time={self.clock():.3f}\n"
            f"{ctx_line}\n{formatted}",
            encoding="utf-8",
        )
        marker = self._read_marker()
        marker.update(
            {
                "active": True,
                "last_error": f"{exc_type.__name__}: {error}",
                "last_error_at": self.clock(),
                "crash_report": str(report),
            }
        )
        self._write_marker(marker)
        self._last_report = report
        self._prune_crash_reports()
        logging.getLogger(__name__).critical("Unhandled exception (%s)\n%s", context or "main", formatted)
        flush_logging()
        return report

    def record_error_report(
        self,
        exc_type: type[BaseException],
        error: BaseException,
        tb: object,
        *,
        context: str = "",
    ) -> Path | None:
        """Persist a non-fatal background/unraisable exception report."""
        formatted = "".join(traceback.format_exception(exc_type, error, tb))
        if self._error_report_count >= self.max_session_error_reports:
            if self._error_report_count == self.max_session_error_reports:
                self._error_report_count += 1
                logging.getLogger(__name__).warning(
                    "Non-fatal error report cap (%d) reached for this session; further reports suppressed",
                    self.max_session_error_reports,
                )
                flush_logging()
            return None
        self._error_report_count += 1
        self.crash_reports_dir.mkdir(parents=True, exist_ok=True)
        timestamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(self.clock()))
        report = self.crash_reports_dir / f"error-{timestamp}-{uuid.uuid4().hex[:6]}.log"
        ctx_line = f"context={context}\n" if context else ""
        report.write_text(
            f"Antigravity Desktop Pet {self.version}\n"
            f"session={self.session_id}\n"
            f"pid={os.getpid()}\n"
            f"time={self.clock():.3f}\n"
            f"{ctx_line}\n{formatted}",
            encoding="utf-8",
        )
        self._prune_crash_reports()
        logging.getLogger(__name__).error("Captured error (%s) -> %s\n%s", context or "unknown", report, formatted)
        flush_logging()
        return report

    def record_qt_fatal(self, message: str) -> Path:
        """Persist a Qt fatal abort message before the process terminates."""
        self._fatal = True
        self.crash_reports_dir.mkdir(parents=True, exist_ok=True)
        timestamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(self.clock()))
        report = self.crash_reports_dir / f"qt-fatal-{timestamp}.log"
        report.write_text(
            f"Antigravity Desktop Pet {self.version}\n"
            f"session={self.session_id}\n"
            f"pid={os.getpid()}\n"
            f"time={self.clock():.3f}\n"
            f"source=qt-fatal\n\n{message}\n",
            encoding="utf-8",
        )
        marker = self._read_marker()
        marker.update(
            {
                "active": True,
                "last_error": f"QtFatal: {message[:200]}",
                "last_error_at": self.clock(),
                "crash_report": str(report),
            }
        )
        self._write_marker(marker)
        self._last_report = report
        self._prune_crash_reports()
        logging.getLogger("qt").critical("QtFatal: %s (saved to %s)", message, report)
        flush_logging()
        return report

    def _prune_crash_reports(self) -> None:
        if not self.crash_reports_dir.is_dir():
            return
        candidates: list[tuple[int, str, Path]] = []
        for pattern in ("crash-*.log", "error-*.log", "qt-fatal-*.log"):
            for path in self.crash_reports_dir.glob(pattern):
                try:
                    mtime_ns = path.stat().st_mtime_ns
                except OSError:
                    continue
                candidates.append((mtime_ns, path.name, path))
        if len(candidates) <= self.max_crash_reports:
            return
        candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
        for _mtime, _name, stale in candidates[self.max_crash_reports :]:
            try:
                stale.unlink()
            except OSError:
                pass

    def finish(self) -> None:
        if self._fatal:
            return
        marker = self._read_marker()
        if marker.get("session_id") != self.session_id:
            return
        marker.update(
            {
                "active": False,
                "clean_exit_at": self.clock(),
                "consecutive_crashes": 0,
                "safe_mode": False,
            }
        )
        self._write_marker(marker)

    def _read_marker(self) -> dict[str, object]:
        try:
            value = json.loads(self.marker_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _write_marker(self, value: dict[str, object]) -> None:
        self.marker_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.marker_path.with_suffix(self.marker_path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.marker_path)

    @staticmethod
    def _safe_int(value: object) -> int:
        return max(0, int(value)) if isinstance(value, int) and not isinstance(value, bool) else 0


def flush_logging() -> None:
    for handler in list(logging.getLogger().handlers):
        try:
            handler.flush()
        except Exception:
            pass


class _LogStream:
    """Fallback stderr sink for GUI pythonw processes where sys.stderr is None."""

    def __init__(self, logger_name: str = "stderr") -> None:
        self._logger = logging.getLogger(logger_name)
        self._buffer = ""
        self._active = False

    def write(self, text: str) -> int:
        if not text:
            return 0
        if self._active:
            return len(text)
        self._active = True
        try:
            self._buffer += text
            while "\n" in self._buffer:
                line, self._buffer = self._buffer.split("\n", 1)
                if line.strip():
                    self._logger.warning("%s", line)
        finally:
            self._active = False
        return len(text)

    def flush(self) -> None:
        if self._active:
            return
        self._active = True
        try:
            if self._buffer.strip():
                self._logger.warning("%s", self._buffer.strip())
            self._buffer = ""
        finally:
            self._active = False

    def isatty(self) -> bool:
        return False


class DiagnosticHooks:
    """Install native faulthandler, thread/unraisable, stderr, and Qt log hooks."""

    def __init__(
        self,
        recovery: CrashRecovery,
        *,
        faulthandler_max_bytes: int = FAULTHANDLER_MAX_BYTES,
    ) -> None:
        self.recovery = recovery
        self.faulthandler_max_bytes = max(1024, int(faulthandler_max_bytes))
        self._fault_file: TextIO | None = None
        self._fault_enabled = False
        self._orig_threading_hook: Any = None
        self._orig_unraisable_hook: Any = None
        self._replaced_stderr = False
        self._qt_handler_installed = False

    @property
    def faulthandler_path(self) -> Path:
        return self.recovery.crash_reports_dir / "faulthandler.log"

    def _rotate_faulthandler_if_needed(self) -> None:
        path = self.faulthandler_path
        try:
            if path.is_file() and path.stat().st_size >= self.faulthandler_max_bytes:
                backup = path.with_name(f"{path.name}.1")
                path.replace(backup)
        except OSError:
            pass

    def install(self, *, enable_faulthandler: bool = True) -> None:
        self.recovery.crash_reports_dir.mkdir(parents=True, exist_ok=True)
        if sys.stderr is None:
            sys.stderr = _LogStream("stderr")  # type: ignore[assignment]
            self._replaced_stderr = True

        if enable_faulthandler:
            try:
                self._rotate_faulthandler_if_needed()
                self._fault_file = self.faulthandler_path.open("a", encoding="utf-8")
                self._fault_file.write(
                    f"\n--- session={self.recovery.session_id} pid={os.getpid()} "
                    f"time={time.strftime('%Y-%m-%d %H:%M:%S')} ---\n"
                )
                self._fault_file.flush()
                faulthandler.enable(file=self._fault_file, all_threads=True)
                self._fault_enabled = True
            except Exception:
                logging.getLogger(__name__).warning("Unable to enable faulthandler", exc_info=True)

        self._orig_threading_hook = threading.excepthook
        threading.excepthook = self._handle_thread_exception

        self._orig_unraisable_hook = sys.unraisablehook
        sys.unraisablehook = self._handle_unraisable_exception

    def install_qt_handler(self) -> None:
        from PySide6.QtCore import QtMsgType, qInstallMessageHandler

        qt_logger = logging.getLogger("qt")

        def handle_qt_message(msg_type: QtMsgType, context: Any, message: str) -> None:
            location = ""
            if context is not None and getattr(context, "file", None):
                location = f" ({context.file}:{getattr(context, 'line', 0)})"
            text = f"{message}{location}"
            if msg_type == QtMsgType.QtFatalMsg:
                self.recovery.record_qt_fatal(text)
            elif msg_type == QtMsgType.QtCriticalMsg:
                qt_logger.error("%s", text)
                flush_logging()
            elif msg_type == QtMsgType.QtWarningMsg:
                qt_logger.warning("%s", text)
            elif msg_type == QtMsgType.QtInfoMsg:
                qt_logger.info("%s", text)

        self._qt_handler_cb = handle_qt_message
        qInstallMessageHandler(handle_qt_message)
        self._qt_handler_installed = True

    def _handle_thread_exception(self, args: threading.ExceptHookArgs) -> None:
        if args.exc_type is SystemExit:
            return
        thread_name = args.thread.name if args.thread is not None else "unknown"
        if args.exc_value is not None:
            self.recovery.record_error_report(
                args.exc_type,
                args.exc_value,
                args.exc_traceback,
                context=f"thread={thread_name}",
            )

    def _handle_unraisable_exception(self, args: sys.UnraisableHookArgs) -> None:
        if args.exc_type is None or args.exc_value is None:
            return
        detail = args.err_msg or "unraisable"
        obj_repr = repr(args.object)[:120] if args.object is not None else ""
        ctx = f"{detail} {obj_repr}".strip()
        self.recovery.record_error_report(
            args.exc_type,
            args.exc_value,
            args.exc_traceback,
            context=ctx,
        )

    def close(self) -> None:
        if self._qt_handler_installed:
            try:
                from PySide6.QtCore import qInstallMessageHandler

                qInstallMessageHandler(None)
            except Exception:
                pass
            self._qt_handler_installed = False
        if self._orig_threading_hook is not None:
            threading.excepthook = self._orig_threading_hook
            self._orig_threading_hook = None
        if self._orig_unraisable_hook is not None:
            sys.unraisablehook = self._orig_unraisable_hook
            self._orig_unraisable_hook = None
        if self._fault_enabled:
            try:
                faulthandler.disable()
            except Exception:
                pass
            self._fault_enabled = False
        if self._fault_file is not None:
            try:
                self._fault_file.flush()
                self._fault_file.close()
            except Exception:
                pass
            self._fault_file = None
        if self._replaced_stderr:
            sys.stderr = None  # type: ignore[assignment]
            self._replaced_stderr = False
        flush_logging()
