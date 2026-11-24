from __future__ import annotations

import json
import logging
import tempfile
import threading
import unittest
from pathlib import Path

from app.recovery import CrashRecovery, DiagnosticHooks, _LogStream


class CrashRecoveryTests(unittest.TestCase):
    def recovery(self, root: Path, now: list[float]) -> CrashRecovery:
        return CrashRecovery(
            root / "session_recovery.json",
            root / "logs" / "crashes",
            version="0.2.0",
            clock=lambda: now[0],
        )

    def test_clean_exit_resets_crash_streak(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            now = [100.0]
            recovery = self.recovery(root, now)

            status = recovery.begin()
            now[0] = 115.0
            recovery.heartbeat()
            recovery.finish()

            marker = json.loads((root / "session_recovery.json").read_text(encoding="utf-8"))
            self.assertFalse(status.unclean_exit)
            self.assertFalse(marker["active"])
            self.assertEqual(marker["last_heartbeat"], 115.0)
            self.assertEqual(marker["consecutive_crashes"], 0)

    def test_two_consecutive_unclean_sessions_enable_safe_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            now = [100.0]
            first = self.recovery(root, now)
            first.begin()

            now[0] = 200.0
            second = self.recovery(root, now)
            second_status = second.begin()

            now[0] = 300.0
            third = self.recovery(root, now)
            third_status = third.begin()

            self.assertTrue(second_status.unclean_exit)
            self.assertEqual(second_status.consecutive_crashes, 1)
            self.assertFalse(second_status.safe_mode)
            self.assertEqual(third_status.consecutive_crashes, 2)
            self.assertTrue(third_status.safe_mode)

    def test_exception_writes_diagnostic_and_preserves_active_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            now = [100.0]
            recovery = self.recovery(root, now)
            recovery.begin()
            try:
                raise RuntimeError("test crash")
            except RuntimeError as error:
                report = recovery.record_exception(type(error), error, error.__traceback__)
            recovery.finish()

            marker = json.loads((root / "session_recovery.json").read_text(encoding="utf-8"))
            self.assertTrue(report.is_file())
            self.assertIn("RuntimeError: test crash", report.read_text(encoding="utf-8"))
            self.assertTrue(marker["active"])
            self.assertIn("RuntimeError: test crash", marker["last_error"])

    def test_diagnostic_hooks_capture_thread_exception_and_faulthandler_log(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            now = [200.0]
            recovery = self.recovery(root, now)
            recovery.begin()
            hooks = DiagnosticHooks(recovery)
            hooks.install(enable_faulthandler=True)
            try:
                self.assertTrue(hooks.faulthandler_path.is_file())
                self.assertIn(recovery.session_id, hooks.faulthandler_path.read_text(encoding="utf-8"))

                def boom() -> None:
                    raise ValueError("worker boom")

                worker = threading.Thread(target=boom, name="test-worker")
                worker.start()
                worker.join(timeout=3.0)
                reports = list((root / "logs" / "crashes").glob("error-*.log"))
                self.assertEqual(len(reports), 1)
                body = reports[0].read_text(encoding="utf-8")
                self.assertIn("ValueError: worker boom", body)
                self.assertIn("thread=test-worker", body)
                self.assertFalse(recovery.fatal)
            finally:
                hooks.close()

    def test_record_qt_fatal_and_log_stream(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            now = [300.0]
            recovery = self.recovery(root, now)
            recovery.begin()
            report = recovery.record_qt_fatal("QPixmap: fatal test")
            self.assertTrue(report.is_file())
            self.assertIn("QPixmap: fatal test", report.read_text(encoding="utf-8"))
            self.assertTrue(recovery.fatal)
            marker = json.loads((root / "session_recovery.json").read_text(encoding="utf-8"))
            self.assertIn("QtFatal: QPixmap: fatal test", marker["last_error"])

            captured: list[str] = []

            class _Capture(logging.Handler):
                def emit(self, record: logging.LogRecord) -> None:
                    captured.append(record.getMessage())

            test_logger = logging.getLogger("stderr.test")
            handler = _Capture()
            test_logger.addHandler(handler)
            try:
                stream = _LogStream("stderr.test")
                stream.write("first line\nsecond")
                stream.flush()
                self.assertEqual(captured, ["first line", "second"])
            finally:
                test_logger.removeHandler(handler)


if __name__ == "__main__":
    unittest.main()
