from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from agy.characters import form_for_model_label, next_form, normalize_form
from agy.reactions import ReactionTracker
from agy.telemetry import (
    ConversationUsage,
    LiveSnapshot,
    TelemetryCollector,
    build_model_rows,
    format_tokens,
    parse_iso_ms,
    parse_rpc_steps,
    parse_transcript_lines,
    prettify_model,
    snapshot_from_hud_live,
    summarize_conversation,
)

ROOT = Path(__file__).resolve().parents[1]


def rpc_step(model: str, created: str, viewable: str, finished: str, *, inp=1000, cache=3000, out=500, think=200) -> dict:
    return {
        "type": "CORTEX_STEP_TYPE_PLANNER_RESPONSE",
        "metadata": {
            "createdAt": created,
            "viewableAt": viewable,
            "finishedGeneratingAt": finished,
            "modelUsage": {
                "model": model,
                "inputTokens": str(inp),
                "cacheReadTokens": str(cache),
                "outputTokens": str(out),
                "thinkingOutputTokens": str(think),
                "responseOutputTokens": str(out - think),
            },
        },
        "plannerResponse": {"toolCalls": [{"name": "read_file"}]},
    }


class CharacterTests(unittest.TestCase):
    def test_model_labels_map_to_forms(self) -> None:
        self.assertEqual(form_for_model_label("Gemini 4 Neon (High)"), "neon")
        self.assertEqual(form_for_model_label("Orchestrator (Carbon)"), "carbon")
        self.assertEqual(form_for_model_label("Gemini 4 Hydrogen (XHigh)"), "hydergon")
        self.assertEqual(form_for_model_label("gemini-4-carbon-medium"), "carbon")
        self.assertEqual(form_for_model_label("Gemini 4 Argon (High)"), "argon")
        self.assertEqual(form_for_model_label("Claude Opus 5.5 (High)"), "claude")

    def test_normalize_and_cycle(self) -> None:
        self.assertEqual(normalize_form("bogus"), "carbon")
        self.assertEqual(next_form("carbon"), "hydergon")
        self.assertEqual(next_form("neon"), "argon")
        self.assertEqual(next_form("argon"), "claude")
        self.assertEqual(next_form("claude"), "carbon")


class ParsingTests(unittest.TestCase):
    def test_iso_with_nanoseconds(self) -> None:
        self.assertEqual(parse_iso_ms("2026-11-18T22:35:41.097379600Z"), parse_iso_ms("2026-11-18T22:35:41.097379Z"))
        self.assertIsNone(parse_iso_ms("nope"))

    def test_rpc_steps_speed_and_split(self) -> None:
        traj = {
            "steps": [
                {"type": "CORTEX_STEP_TYPE_USER_INPUT", "userInput": {"items": [{"text": "<USER_REQUEST>做个桌宠</USER_REQUEST>"}]}},
                rpc_step("M1", "2026-01-01T00:00:00Z", "2026-01-01T00:00:02Z", "2026-01-01T00:00:07Z"),
                {"type": "CORTEX_STEP_TYPE_PLANNER_RESPONSE", "metadata": {"modelUsage": {"model": "M1"}}},
            ]
        }
        steps, title = parse_rpc_steps(traj, {"M1": "Gemini 4 Neon (High)"})
        self.assertEqual(title, "做个桌宠")
        self.assertEqual(len(steps), 1)  # zero-usage step skipped
        step = steps[0]
        self.assertEqual(step.model_label, "Gemini 4 Neon (High)")
        self.assertEqual(step.total_tokens, 4500)
        self.assertEqual((step.thinking_tokens, step.response_tokens), (200, 300))
        self.assertAlmostEqual(step.decode_tps, 100.0)  # 500 tok / 5 s
        self.assertAlmostEqual(step.e2e_tps, 500 / 7)
        self.assertEqual(step.ttft_ms, 2000)
        self.assertEqual(step.tool_calls, ("read_file",))

    def test_transcript_carries_model_backward_and_forward(self) -> None:
        lines = [
            json.dumps({"type": "USER_INPUT", "content": "你好", "created_at": "2026-01-01T00:00:00Z"}),
            json.dumps({"type": "PLANNER_RESPONSE", "created_at": "2026-01-01T00:00:01Z", "input_tokens": 100, "cache_read_tokens": 0, "output_tokens": 50}),
            json.dumps({"type": "GENERIC", "created_at": "2026-01-01T00:00:03Z"}),
            json.dumps({"type": "PLANNER_RESPONSE", "created_at": "2026-01-01T00:00:04Z", "input_tokens": 10, "cache_read_tokens": 90, "output_tokens": 20, "model": "gemini-4-carbon-medium"}),
            "{broken",
        ]
        steps, title = parse_transcript_lines(lines)
        self.assertEqual(title, "你好")
        self.assertEqual([s.model_id for s in steps], ["gemini-4-carbon-medium"] * 2)
        self.assertEqual(steps[0].model_label, "Gemini 4 Carbon Medium")
        self.assertAlmostEqual(steps[0].decode_tps, 25.0)  # 50 tok over 2 s gap
        self.assertIsNone(steps[1].decode_tps)

    def test_summary_cache_context_and_rows(self) -> None:
        traj = {"steps": [
            rpc_step("A", "2026-01-01T00:00:00Z", "2026-01-01T00:00:01Z", "2026-01-01T00:00:06Z", inp=1000, cache=0, out=500),
            rpc_step("B", "2026-01-01T00:01:00Z", "2026-01-01T00:01:01Z", "2026-01-01T00:01:03Z", inp=1000, cache=3000, out=400),
        ]}
        steps, _ = parse_rpc_steps(traj, {"A": "Gemini 4 Carbon (High)", "B": "Gemini 4 Neon (High)"})
        conv = ConversationUsage("c1", "t", status="CASCADE_RUN_STATUS_RUNNING", steps=steps)
        snap = summarize_conversation(conv, source="ls", selected_model_label="Gemini 4 Neon (High)", quota=(12.0, None))
        self.assertTrue(snap.running)
        self.assertAlmostEqual(snap.cache_hit_pct, 60.0)
        self.assertEqual(snap.context_used_tokens, 4200)  # latest step prompt + response (excludes thinking)
        self.assertEqual(snap.cumulative_total_tokens, 5900)
        self.assertAlmostEqual(snap.avg_decode_tps, 900 / 7)
        self.assertEqual(snap.model_for_form, "Gemini 4 Neon (High)")
        rows = build_model_rows([conv])
        self.assertEqual([r.label for r in rows], ["Gemini 4 Neon (High)", "Gemini 4 Carbon (High)"])
        self.assertAlmostEqual(sum(r.share_pct for r in rows), 100.0)
        self.assertAlmostEqual(rows[0].peak_decode_tps, 200.0)

    def test_hud_live_payload(self) -> None:
        payload = json.loads((Path(__file__).parent / "fixtures" / "hud_live.json").read_text(encoding="utf-8"))
        snap = snapshot_from_hud_live(payload)
        self.assertEqual(snap.conversation_id, "active-1")
        self.assertEqual(snap.quota_remaining_pct, 75.0)
        self.assertEqual(len(snap.conversations), 2)
        pinned = snapshot_from_hud_live(payload, pinned_id="other-2")
        self.assertEqual(pinned.conversation_id, "other-2")

    def test_formatting(self) -> None:
        self.assertEqual(format_tokens(1234), "1.2k")
        self.assertEqual(format_tokens(3_786_963), "3.79M")
        self.assertEqual(prettify_model("MODEL_PLACEHOLDER_M322"), "Model M322")
        self.assertEqual(prettify_model("MODEL_OPENAI_GPT_OSS_120B_MEDIUM"), "Openai Gpt Oss 120B Medium")

    def test_context_excludes_thinking_and_limit_follows_active_model(self) -> None:
        from agy.telemetry import DEFAULT_CONTEXT_LIMIT, infer_context_limit, parse_annotation_pbtxt

        # Transcript with thinking_tokens excludes thinking from context_used_tokens
        lines = [
            json.dumps({"type": "USER_INPUT", "content": "测试上下文"}),
            json.dumps({
                "type": "PLANNER_RESPONSE",
                "created_at": "2026-01-01T00:00:01Z",
                "input_tokens": 1000,
                "cache_read_tokens": 9000,
                "output_tokens": 1500,
                "thinking_tokens": 1200,
                "model": "gemini-4-carbon-high",
            }),
        ]
        steps, _ = parse_transcript_lines(lines)
        self.assertEqual(steps[0].context_tokens, 10300)
        self.assertEqual(steps[0].total_tokens, 11500)

        # Single-model context limit inference avoids cross-model contamination
        self.assertEqual(infer_context_limit("Gemini 4 Carbon (High)", "Claude Opus 4.5"), DEFAULT_CONTEXT_LIMIT)
        self.assertEqual(infer_context_limit("Claude Opus 4.5", "Gemini 4 Carbon (High)"), 200_000)
        self.assertEqual(infer_context_limit("未选择模型", "Claude Opus 4.5"), 200_000)

        # Idle selector switch updates limit without leaking previous model
        payload = {
            "active": {
                "conversationId": "c1",
                "title": "t",
                "status": "CASCADE_RUN_STATUS_IDLE",
                "selectedModelLabel": "Gemini 4 Carbon (High)",
                "lastStepModelLabel": "Claude Opus 4.5",
                "contextUsedTokens": 12000,
            },
            "conversations": [],
        }
        snap = snapshot_from_hud_live(payload)
        self.assertEqual(snap.context_limit_tokens, DEFAULT_CONTEXT_LIMIT)
        payload["active"]["status"] = "CASCADE_RUN_STATUS_RUNNING"
        running_snap = snapshot_from_hud_live(payload)
        self.assertEqual(running_snap.context_limit_tokens, 200_000)

        # parse_annotation_pbtxt supports raw UTF-8 and octal escapes
        raw_utf8 = 'title:"桌面宠物上下文" last_user_view_time:{seconds:1795362000 nanos:500000000}'.encode("utf-8")
        ms, title = parse_annotation_pbtxt(raw_utf8)
        self.assertEqual(title, "桌面宠物上下文")
        self.assertEqual(ms, 1795362000500)
        raw_oct = r'last_user_view_time:{nanos:123000000 seconds:1700000000} title:"\346\265\213\350\257\225"'
        ms2, title2 = parse_annotation_pbtxt(raw_oct)
        self.assertEqual(title2, "测试")
        self.assertEqual(ms2, 1700000000123)


class CollectorFallbackTests(unittest.TestCase):
    def _root(self, folder: str) -> Path:
        root = Path(folder)
        logs = root / "brain" / "conv-1" / ".system_generated" / "logs"
        logs.mkdir(parents=True)
        (logs / "transcript.jsonl").write_text(
            "\n".join(
                json.dumps(o)
                for o in (
                    {"type": "USER_INPUT", "content": "离线会话"},
                    {"type": "PLANNER_RESPONSE", "created_at": "2026-01-01T00:00:00Z", "input_tokens": 100, "cache_read_tokens": 100, "output_tokens": 10, "model": "gemini-4-neon-high"},
                )
            ),
            encoding="utf-8",
        )
        return root

    def test_fresh_hud_file_wins_over_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = self._root(folder)
            hud = root / "sidecar_data" / "token-meter" / "dashboard" / "data"
            hud.mkdir(parents=True)
            (hud / "hud_live.json").write_text((Path(__file__).parent / "fixtures" / "hud_live.json").read_text(encoding="utf-8"), encoding="utf-8")
            collector = TelemetryCollector(root, use_language_server=False)
            self.assertEqual(collector.live_snapshot().source, "hud_live")
            stale = time.time() - 120
            os.utime(hud / "hud_live.json", (stale, stale))
            snap = collector.live_snapshot()
            self.assertEqual(snap.source, "transcript")
            self.assertEqual(snap.title, "离线会话")
            self.assertAlmostEqual(snap.cache_hit_pct, 50.0)
            report = collector.meter_report()
            self.assertEqual(report.source, "transcript")
            self.assertEqual(report.models[0].label, "Gemini 4 Neon High")

    def test_nothing_available(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            collector = TelemetryCollector(Path(folder), use_language_server=False)
            self.assertIsNone(collector.live_snapshot())
            self.assertEqual(collector.meter_report().models, [])

    def test_focus_tracking_annotations_hook_and_project_switch(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            root = base / "antigravity"
            hud_dir = root / "sidecar_data" / "token-meter" / "dashboard" / "data"
            hook_dir = root / "sidecar_data" / "agy-pet"
            ann_dir = root / "annotations"
            proj_dir = base / "config" / "projects"
            for d in (hud_dir, hook_dir, ann_dir, proj_dir):
                d.mkdir(parents=True)

            now_s = int(time.time())
            now_ms = now_s * 1000
            payload = {
                "selectedModelLabel": "Gemini 4 Carbon (High)",
                "active": {"conversationId": "conv-a", "title": "会话A", "status": "CASCADE_RUN_STATUS_IDLE", "contextUsedTokens": 90000},
                "conversations": [
                    {
                        "conversationId": "conv-a",
                        "title": "会话A",
                        "status": "CASCADE_RUN_STATUS_IDLE",
                        "lastModifiedTime": "2026-11-22T15:20:00Z",
                        "contextUsedTokens": 90000,
                        "projectId": "proj-1",
                        "workspaceUris": ["file:///d%3A/proj-1"],
                    },
                    {
                        "conversationId": "conv-b",
                        "title": "会话B",
                        "status": "CASCADE_RUN_STATUS_IDLE",
                        "lastModifiedTime": "2026-11-22T15:10:00Z",
                        "contextUsedTokens": 12000,
                        "projectId": "proj-1",
                        "workspaceUris": ["file:///d%3A/proj-1"],
                    },
                ],
            }
            (hud_dir / "hud_live.json").write_text(json.dumps(payload), encoding="utf-8")

            collector = TelemetryCollector(root, use_language_server=False)
            # 1) User clicked conv-b in sidebar -> annotation last_user_view_time switches focus
            (ann_dir / "conv-a.pbtxt").write_text(f'title:"会话A" last_user_view_time:{{seconds:{now_s - 300}}}', encoding="utf-8")
            (ann_dir / "conv-b.pbtxt").write_text(f'title:"会话B" last_user_view_time:{{seconds:{now_s - 60}}}', encoding="utf-8")
            snap1 = collector.live_snapshot()
            self.assertEqual(snap1.conversation_id, "conv-b")
            self.assertEqual(snap1.context_used_tokens, 12000)

            # 2) SessionStart / PreInvocation hook starts a brand-new 0-step conv-c
            (hook_dir / "active_session.json").write_text(
                json.dumps({"conversationId": "conv-c", "lastUserInput": "新问题", "updatedMs": now_ms - 10_000}),
                encoding="utf-8",
            )
            snap2 = collector.live_snapshot()
            self.assertEqual(snap2.conversation_id, "conv-c")
            self.assertEqual(snap2.title, "新问题")
            self.assertEqual(snap2.context_used_tokens, 0)

            # 3) User switches back to conv-a without sending -> newer view time beats older hook
            (ann_dir / "conv-a.pbtxt").write_text(f'title:"会话A" last_user_view_time:{{seconds:{now_s}}}', encoding="utf-8")
            snap3 = collector.live_snapshot()
            self.assertEqual(snap3.conversation_id, "conv-a")

            # 4) User explicitly pins conv-b -> pinning overrides focus sorting
            snap4 = collector.live_snapshot(pinned_id="conv-b")
            self.assertEqual(snap4.conversation_id, "conv-b")

            # 5) Switching to a project with no existing sessions -> resets context to 0
            # Sidecar permission entries must be ignored even if touched later.
            (ann_dir / "conv-a.pbtxt").write_text(f'title:"会话A" last_user_view_time:{{seconds:{now_s - 30}}}', encoding="utf-8")
            (proj_dir / "proj-empty.json").write_text(
                json.dumps({"id": "proj-empty", "name": "empty-repo", "projectResources": {"resources": [{"folderUri": "file:///d%3A/empty-repo"}]}}),
                encoding="utf-8",
            )
            (proj_dir / "sidecar-only.json").write_text(
                json.dumps({"id": "sidecar-only", "name": "agy-pet/pet-bridge", "projectResources": {}, "sidecar": {"sidecarId": "agy-pet/pet-bridge"}}),
                encoding="utf-8",
            )
            snap5 = collector.live_snapshot()
            self.assertEqual(snap5.context_used_tokens, 0)
            self.assertIn("empty-repo", snap5.title)


class ServiceIntervalTests(unittest.TestCase):
    def test_polls_fast_only_while_streaming(self) -> None:
        from agy.service import TelemetryService

        self.assertEqual(TelemetryService.interval_for(None), TelemetryService.OFFLINE_INTERVAL)
        idle = LiveSnapshot(source="ls", status="CASCADE_RUN_STATUS_IDLE")
        running = LiveSnapshot(source="ls", status="CASCADE_RUN_STATUS_RUNNING")
        self.assertEqual(TelemetryService.interval_for(idle), TelemetryService.IDLE_INTERVAL)
        self.assertEqual(TelemetryService.interval_for(running), TelemetryService.RUNNING_INTERVAL)


class IncrementalFetchTests(unittest.TestCase):
    def test_only_tail_is_refetched_and_result_matches_full(self) -> None:
        steps = [rpc_step(f"M{i % 2}", "2026-01-01T00:00:00Z", "2026-01-01T00:00:01Z", "2026-01-01T00:00:03Z", out=100 + i) for i in range(6)]

        class FakeLs:
            endpoint = object()

            def __init__(self) -> None:
                self.calls: list[tuple[str, dict]] = []
                self.steps = list(steps[:4])

            def rpc(self, method, body=None, *, timeout=None):
                self.calls.append((method, dict(body or {})))
                if method == "GetCascadeTrajectory":
                    return {"trajectory": {"steps": list(self.steps)}}
                if method == "GetCascadeTrajectorySteps":
                    return {"steps": self.steps[body.get("stepOffset", 0):]}
                raise AssertionError(method)

        fake = FakeLs()
        collector = TelemetryCollector(Path("."), ls_client=fake)
        first, _ = collector._fetch_steps("c")
        self.assertEqual(len(first), 4)
        fake.steps = list(steps)  # two new steps arrived
        second, _ = collector._fetch_steps("c")
        self.assertEqual(fake.calls[-1], ("GetCascadeTrajectorySteps", {"cascadeId": "c", "stepOffset": 2}))
        self.assertEqual([s.output_tokens for s in second], [100 + i for i in range(6)])
        fake.steps = list(steps[:1])  # trajectory shrank -> full refetch
        third, _ = collector._fetch_steps("c")
        self.assertEqual(fake.calls[-1][0], "GetCascadeTrajectory")
        self.assertEqual(len(third), 1)


class ReactionTests(unittest.TestCase):
    def snap(self, **kw) -> LiveSnapshot:
        base = dict(source="ls", conversation_id="c", title="t", status="CASCADE_RUN_STATUS_IDLE", selected_model_label="Gemini 4 Carbon (High)")
        base.update(kw)
        return LiveSnapshot(**base)

    def test_run_cycle_quota_context_and_follow(self) -> None:
        tracker = ReactionTracker()
        self.assertEqual(tracker.observe(self.snap(), now=0, follow_model=False, current_form="carbon"), [])
        started = tracker.observe(self.snap(status="CASCADE_RUN_STATUS_RUNNING"), now=1, follow_model=False, current_form="carbon")
        self.assertEqual([r.kind for r in started], ["started"])
        done = tracker.observe(self.snap(cumulative_total_tokens=1200), now=2, follow_model=False, current_form="carbon")
        self.assertEqual(done[0].kind, "finished")
        self.assertIn("1.2k", done[0].text)
        low = tracker.observe(self.snap(quota_remaining_pct=20.0), now=3, follow_model=False, current_form="carbon")
        self.assertEqual([r.kind for r in low], ["quota_low"])
        self.assertEqual(tracker.observe(self.snap(quota_remaining_pct=19.0), now=4, follow_model=False, current_form="carbon"), [])
        crit = tracker.observe(self.snap(quota_remaining_pct=5.0, context_used_tokens=900_000), now=5, follow_model=False, current_form="carbon")
        self.assertEqual(sorted(r.kind for r in crit), ["context_high", "quota_critical"])
        follow = tracker.observe(self.snap(selected_model_label="Gemini 4 Neon (High)"), now=6, follow_model=True, current_form="carbon")
        self.assertEqual([(r.kind, r.form) for r in follow], [("form", "neon")])


class PetWindowAgyTests(unittest.TestCase):
    def test_form_switch_hud_and_meter(self) -> None:
        from PySide6.QtWidgets import QApplication

        from agy.telemetry import MeterReport
        from app.paths import AppPaths
        from pet.window import PetWindow
        from settings.config import ConfigManager

        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as folder:
            paths = AppPaths(app_dir=Path(folder), bundle_dir=ROOT)
            config = ConfigManager(paths.config_path)
            self.assertFalse(config.data["agy"]["telemetry_enabled"])  # conftest default
            window = PetWindow(paths, config)
            try:
                self.assertEqual(window.form_id, "carbon")
                bottom = window.y() + window.height()
                window.set_form("neon", manual=True)
                self.assertEqual(window.form_id, "neon")
                self.assertEqual(window.asset_registry.character_id, "neon")
                self.assertEqual(window.y() + window.height(), bottom)
                self.assertEqual(json.loads(paths.config_path.read_text(encoding="utf-8"))["agy"]["form"], "neon")
                window.physics.launch(window.x(), window.y(), 0.0, 0.0)
                window.set_form("hydergon")
                self.assertEqual(window.form_id, "neon")  # deferred mid-air
                window.physics.cancel()
                window.tick()
                self.assertEqual(window.form_id, "hydergon")

                window.set_follow_model(True)
                window._on_live_snapshot(
                    LiveSnapshot(source="ls", conversation_id="c", title="会话", status="CASCADE_RUN_STATUS_IDLE",
                                 selected_model_label="Gemini 4 Carbon (High)", quota_remaining_pct=50.0,
                                 latest_decode_tps=88.0, cache_hit_pct=91.0, context_used_tokens=200_000)
                )
                self.assertEqual(window.form_id, "carbon")
                carbon_snapshot = window.telemetry.last_snapshot = LiveSnapshot(
                    source="ls", conversation_id="c", title="会话", status="CASCADE_RUN_STATUS_IDLE",
                    selected_model_label="Gemini 4 Carbon (High)", quota_remaining_pct=50.0,
                    latest_decode_tps=88.0, cache_hit_pct=91.0, context_used_tokens=200_000,
                )
                window.set_form("neon", manual=True)
                self.assertFalse(window._agy()["follow_model"])
                window.set_follow_model(True)  # replays the carbon snapshot
                self.assertEqual(window.form_id, "carbon")
                window._on_live_snapshot(carbon_snapshot)
                self.assertEqual(window.hud_bar.speed.text(), "88.0 tok/s")
                self.assertEqual(window.hud_bar.quota_5h.text(), "50%")
                self.assertTrue(window.hud_bar.isVisible())
                window.set_hud_enabled(False)
                self.assertFalse(window.hud_bar.isVisible())
                window.token_meter.update_report(
                    MeterReport("transcript", build_model_rows([]), 0, None, None)
                )
                app.processEvents()
            finally:
                window.telemetry.stop()
                window.hud_bar.close()
                window.token_meter.close()
                window.control_panel.close()
                window.food_panel.close()
                window.tray.hide()
                window.close()

    def test_external_config_hot_reload_preserves_position(self) -> None:
        from PySide6.QtWidgets import QApplication

        from app.paths import AppPaths
        from pet.window import PetWindow
        from settings.config import ConfigManager

        QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as folder:
            paths = AppPaths(app_dir=Path(folder), bundle_dir=ROOT)
            config = ConfigManager(paths.config_path)
            window = PetWindow(paths, config)
            try:
                window.move(321, 222)
                window.save_snapshot()
                center_before = 321 + window.width() // 2
                raw = json.loads(paths.config_path.read_text(encoding="utf-8"))
                raw["agy"]["form"] = "neon"
                raw["agy"]["hud_appearance"] = "glass"
                raw["agy"]["pinned_conversation"] = "conv-99"
                # Ensure mtime_ns advances deterministically on fast filesystems
                paths.config_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
                os.utime(paths.config_path, ns=(time.time_ns() + 2_000_000_000, time.time_ns() + 2_000_000_000))

                window.save_snapshot()
                self.assertEqual(window.form_id, "neon")
                self.assertEqual(window.telemetry.pinned_conversation, "conv-99")
                self.assertEqual(window.hud_bar.settings.get("hud_appearance"), "glass")
                saved = json.loads(paths.config_path.read_text(encoding="utf-8"))
                # A form switch keeps her feet in place: same centre and bottom.
                self.assertLessEqual(abs(saved["x"] + window.width() // 2 - center_before), 1)
                self.assertEqual(saved["y"] + window.height(), 222 + window.height())
                self.assertEqual(saved["agy"]["form"], "neon")
            finally:
                window.telemetry.stop()
                window.hud_bar.close()
                window.token_meter.close()
                window.control_panel.close()
                window.food_panel.close()
                window.tray.hide()
                window.close()

    def test_hud_auto_hide_mode_and_legacy_config_migration(self) -> None:
        from PySide6.QtCore import QEvent, QPointF
        from PySide6.QtGui import QEnterEvent
        from PySide6.QtWidgets import QApplication

        from app.paths import AppPaths
        from pet.window import PetWindow
        from settings.config import ConfigManager

        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as folder:
            paths = AppPaths(app_dir=Path(folder), bundle_dir=ROOT)
            paths.config_path.write_text(
                json.dumps({"agy": {"hud_enabled": False}}, ensure_ascii=False),
                encoding="utf-8",
            )
            config = ConfigManager(paths.config_path)
            self.assertEqual(config.section("agy")["hud_visibility"], "hide")
            self.assertEqual(config.section("agy")["hud_auto_hide_seconds"], 5)

            window = PetWindow(paths, config)
            try:
                window.show()
                app.processEvents()
                self.assertFalse(window.hud_bar.isVisible())

                window.set_hud_auto_hide_seconds(3)
                window.set_hud_visibility("auto")
                app.processEvents()
                self.assertTrue(window.hud_bar.isVisible())
                self.assertTrue(window._hud_auto_hide_timer.isActive())
                self.assertEqual(window._hud_auto_hide_timer.interval(), 3000)

                window._on_hud_auto_hide_timeout()
                app.processEvents()
                self.assertFalse(window.hud_bar.isVisible())

                enter = QEnterEvent(QPointF(10, 10), QPointF(10, 10), QPointF(10, 10))
                window.enterEvent(enter)
                app.processEvents()
                self.assertTrue(window.hud_bar.isVisible())
                self.assertFalse(window._hud_auto_hide_timer.isActive())

                window.leaveEvent(QEvent(QEvent.Type.Leave))
                app.processEvents()
                self.assertTrue(window._hud_auto_hide_timer.isActive())

                window.hud_bar.enterEvent(enter)
                app.processEvents()
                self.assertFalse(window._hud_auto_hide_timer.isActive())

                window.hud_bar.leaveEvent(QEvent(QEvent.Type.Leave))
                app.processEvents()
                self.assertTrue(window._hud_auto_hide_timer.isActive())

                window._on_hud_auto_hide_timeout()
                app.processEvents()
                self.assertFalse(window.hud_bar.isVisible())

                window.set_hud_visibility("show")
                app.processEvents()
                self.assertTrue(window.hud_bar.isVisible())
                self.assertFalse(window._hud_auto_hide_timer.isActive())
            finally:
                window.telemetry.stop()
                window.hud_bar.close()
                window.token_meter.close()
                window.control_panel.close()
                window.food_panel.close()
                window.tray.hide()
                window.close()

    def test_ls_discover_only_reads_cmdline_for_matching_process_names(self) -> None:
        from types import SimpleNamespace
        from unittest.mock import patch

        import psutil

        from agy.telemetry import LanguageServerClient

        cmdline_calls: list[str] = []

        class _FakeProc:
            def __init__(self, name: str, cmd: list[str], port: int | None = None) -> None:
                self.info = {"name": name}
                self._name = name
                self._cmd = cmd
                self._port = port

            def cmdline(self) -> list[str]:
                cmdline_calls.append(self._name)
                return list(self._cmd)

            def net_connections(self, kind: str = "tcp"):
                if self._port is None:
                    return []
                return [SimpleNamespace(status=psutil.CONN_LISTEN, laddr=SimpleNamespace(port=self._port))]

        seen_attrs: list[object] = []

        def fake_process_iter(attrs=None):
            seen_attrs.append(attrs)
            return [
                _FakeProc("System", []),
                _FakeProc("svchost.exe", []),
                _FakeProc("language_server_windows_x64.exe", ["--csrf_token=tok-123"], port=54321),
            ]

        client = LanguageServerClient()
        with (
            patch.object(psutil, "process_iter", side_effect=fake_process_iter),
            patch.object(client, "_post", return_value={"userStatus": {}}),
        ):
            endpoint = client.discover()

        self.assertEqual(seen_attrs, [["name"]])
        self.assertEqual(cmdline_calls, ["language_server_windows_x64.exe"])
        self.assertIsNotNone(endpoint)
        self.assertEqual(endpoint.port, 54321)
        self.assertEqual(endpoint.csrf_token, "tok-123")


if __name__ == "__main__":
    unittest.main()
