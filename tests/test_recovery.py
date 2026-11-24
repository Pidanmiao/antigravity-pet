from __future__ import annotations

import json
import logging
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from app.recovery import CrashRecovery, DiagnosticHooks, _LogStream, replace_with_retry


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

    def test_crash_report_pruning_and_session_error_cap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            now = [400.0]
            recovery = CrashRecovery(
                root / "session_recovery.json",
                root / "logs" / "crashes",
                version="0.2.0",
                clock=lambda: now[0],
                max_crash_reports=3,
                max_session_error_reports=4,
            )
            recovery.begin()
            written = []
            for idx in range(6):
                now[0] += 1.0
                try:
                    raise RuntimeError(f"err-{idx}")
                except RuntimeError as error:
                    written.append(
                        recovery.record_error_report(type(error), error, error.__traceback__, context=f"loop-{idx}")
                    )
            self.assertEqual(sum(1 for item in written if item is not None), 4)
            self.assertIsNone(written[4])
            self.assertIsNone(written[5])
            remaining = list((root / "logs" / "crashes").glob("error-*.log"))
            self.assertEqual(len(remaining), 3)

    def test_faulthandler_rotates_when_over_size_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            now = [500.0]
            recovery = self.recovery(root, now)
            recovery.begin()
            fault_path = root / "logs" / "crashes" / "faulthandler.log"
            fault_path.parent.mkdir(parents=True, exist_ok=True)
            fault_path.write_text("X" * 2048, encoding="utf-8")

            hooks = DiagnosticHooks(recovery, faulthandler_max_bytes=1024)
            hooks.install(enable_faulthandler=True)
            try:
                backup = fault_path.with_name("faulthandler.log.1")
                self.assertTrue(backup.is_file())
                self.assertEqual(backup.stat().st_size, 2048)
                self.assertLess(fault_path.stat().st_size, 512)
                self.assertIn(recovery.session_id, fault_path.read_text(encoding="utf-8"))
            finally:
                hooks.close()

    def test_replace_with_retry_recovers_from_transient_permission_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            src = root / "state.json.tmp"
            dst = root / "state.json"
            src.write_text("{\"ok\": true}", encoding="utf-8")

            calls = {"n": 0}
            real_replace = Path.replace

            def flaky_replace(self_path: Path, target: Path):
                calls["n"] += 1
                if calls["n"] < 3:
                    raise PermissionError(13, "Access is denied")
                return real_replace(self_path, target)

            with patch.object(Path, "replace", flaky_replace):
                replace_with_retry(src, dst, retries=4, base_delay=0.001)

            self.assertEqual(calls["n"], 3)
            self.assertEqual(dst.read_text(encoding="utf-8"), "{\"ok\": true}")

    def test_heartbeat_survives_persistent_permission_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            now = [600.0]
            recovery = self.recovery(root, now)
            recovery.begin()
            now[0] = 615.0
            with patch.object(Path, "replace", side_effect=PermissionError(13, "Access is denied")):
                # Must not crash the main event loop when another process locks the file.
                recovery.heartbeat()
            self.assertFalse((root / "session_recovery.json.tmp").exists())


if __name__ == "__main__":
    unittest.main()
