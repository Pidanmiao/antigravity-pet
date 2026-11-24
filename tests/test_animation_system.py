from __future__ import annotations

import json
import unittest
from pathlib import Path

from animation.asset_registry import AssetRegistry
from animation.clip import (
    ActionSpec,
    Anchor,
    AnimationClip,
    AnimationPhase,
    AnimationProgram,
    ClipFrame,
    PetAction,
)
from animation.player import AnimationPlayer
from animation.state_machine import ActionStateMachine


def make_specs() -> dict[PetAction, ActionSpec]:
    specs: dict[PetAction, ActionSpec] = {}
    for action in PetAction:
        specs[action] = ActionSpec(
            action=action,
            asset_id=action.value.lower(),
            loop=action in {PetAction.IDLE, PetAction.WALKING, PetAction.DRAGGING},
            duration_ms=None if action in {PetAction.IDLE, PetAction.WALKING, PetAction.DRAGGING} else 100,
            priority={
                PetAction.IDLE: 10,
                PetAction.WALKING: 30,
                PetAction.POKE_REACT: 80,
                PetAction.FALLING: 90,
                PetAction.DRAGGING: 100,
            }.get(action, 40),
            interruptible=action not in {PetAction.POKE_REACT, PetAction.FALLING, PetAction.DRAGGING},
            return_state=PetAction.IDLE,
            anchor=Anchor(),
            effect="breath",
            quality="source",
            requires_real_frames=False,
        )
    return specs


class ActionStateMachineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.machine = ActionStateMachine(make_specs())

    def test_non_interruptible_poke_blocks_walking(self) -> None:
        self.assertIsNotNone(
            self.machine.request(PetAction.POKE_REACT, now=1.0, reason="tap")
        )
        self.assertIsNone(
            self.machine.request(PetAction.WALKING, now=1.01, reason="movement")
        )
        self.assertEqual(self.machine.current, PetAction.POKE_REACT)

    def test_falling_can_queue_dizzy(self) -> None:
        self.machine.request(PetAction.FALLING, now=1.0, reason="throw", force=True)
        self.machine.queue_after_current(PetAction.DIZZY)
        change = self.machine.update(now=1.11)
        self.assertIsNotNone(change)
        self.assertEqual(self.machine.current, PetAction.DIZZY)

    def test_finite_action_returns_to_idle(self) -> None:
        self.machine.request(PetAction.HAPPY, now=2.0, reason="pat")
        self.machine.update(now=2.11)
        self.assertEqual(self.machine.current, PetAction.IDLE)


class AnimationPlayerTests(unittest.TestCase):
    @staticmethod
    def clip(
        action: PetAction,
        source_name: str,
        *,
        phase: AnimationPhase = AnimationPhase.BODY,
        duration_ms: int | None = None,
    ) -> AnimationClip:
        frame = ClipFrame(Path(source_name), object())  # type: ignore[arg-type]
        return AnimationClip(
            action=action,
            asset_id=source_name,
            frames=(frame,),
            loop=True,
            duration_ms=duration_ms,
            frame_duration_ms=100,
            anchor=Anchor(),
            effect="breath",
            quality="source",
            requires_real_frames=False,
            phase=phase,
        )

    def test_crossfade_keeps_previous_layer_only_for_same_art(self) -> None:
        player = AnimationPlayer()
        player.play(self.clip(PetAction.IDLE, "shared.png"), crossfade=False)
        player.tick(0.1)
        player.play(self.clip(PetAction.HAPPY, "shared.png"))
        during = player.snapshot()
        self.assertEqual(during.current.action, PetAction.HAPPY)
        self.assertIsNotNone(during.previous)
        player.tick(0.2)
        after = player.snapshot()
        self.assertIsNone(after.previous)

    def test_different_pose_does_not_leave_a_ghost_layer(self) -> None:
        player = AnimationPlayer()
        player.play(self.clip(PetAction.IDLE, "idle.png"), crossfade=False)
        player.tick(0.1)
        player.play(self.clip(PetAction.HAPPY, "happy.png"))
        self.assertIsNone(player.snapshot().previous)

    def test_program_runs_enter_body_and_exit_before_next_action(self) -> None:
        player = AnimationPlayer()
        walking = AnimationProgram(
            action=PetAction.WALKING,
            enter=self.clip(
                PetAction.WALKING,
                "walk_stand.png",
                phase=AnimationPhase.ENTER,
                duration_ms=50,
            ),
            body=self.clip(PetAction.WALKING, "walk_loop.png"),
            exit=self.clip(
                PetAction.WALKING,
                "walk_stand.png",
                phase=AnimationPhase.EXIT,
                duration_ms=50,
            ),
        )
        player.play(walking, crossfade=False)
        self.assertEqual(player.snapshot().phase, AnimationPhase.ENTER)
        player.tick(0.06)
        self.assertEqual(player.snapshot().phase, AnimationPhase.BODY)
        self.assertEqual(player.snapshot().current.asset_id, "walk_loop.png")

        player.play(self.clip(PetAction.IDLE, "idle.png"), crossfade=False)
        self.assertEqual(player.snapshot().phase, AnimationPhase.EXIT)
        player.tick(0.06)
        self.assertEqual(player.snapshot().phase, AnimationPhase.BODY)
        self.assertEqual(player.snapshot().current.action, PetAction.IDLE)


class FrameCycleTests(unittest.TestCase):
    def test_one_shot_clip_with_cycle_loops_its_frames(self) -> None:
        frames = tuple(ClipFrame(Path(f"f{i}.png"), object()) for i in range(3))  # type: ignore[arg-type]

        def make(cycle: bool) -> AnimationClip:
            return AnimationClip(
                action=PetAction.TALKING, asset_id="talk", frames=frames, loop=False, duration_ms=2000,
                frame_duration_ms=100, anchor=Anchor(), effect="talk", quality="source",
                requires_real_frames=False, cycle=cycle,
            )

        for cycle, expected in ((True, "f1.png"), (False, "f2.png")):
            player = AnimationPlayer()
            player.play(make(cycle), crossfade=False)
            player.tick(0.45)  # frame index 4
            self.assertEqual(player.snapshot().current.frame.source.name, expected)

    def test_frame_ms_from_manifest_sets_cycle_pace(self) -> None:
        root = Path(__file__).resolve().parents[1]
        base = root / "assets" / "manifests" / "actions_base.json"
        registry = AssetRegistry(root / "carbon", base_manifest=base)
        spec = registry.specs[PetAction.TALKING]
        self.assertEqual(spec.frame_ms, 380)
        self.assertEqual(registry._frame_duration_ms(spec, 4), 380)
        self.assertEqual(registry._frame_duration_ms(spec, 1), 2900)


class AssetManifestTests(unittest.TestCase):
    def test_every_form_declares_required_states_and_formal_walk_cycle(self) -> None:
        root = Path(__file__).resolve().parents[1]
        base = root / "assets" / "manifests" / "actions_base.json"
        for form in ("carbon", "hydergon", "neon", "argon", "claude"):
            with self.subTest(form=form):
                registry = AssetRegistry(root / form, base_manifest=base)
                self.assertIn(registry.character_id, {form, "hydrogen"})
                self.assertTrue(set(PetAction).issubset(registry.specs))
                walking = registry.specs[PetAction.WALKING]
                self.assertFalse(walking.requires_real_frames)
                manifest = registry.manifest
                self.assertEqual(manifest["format_version"], 4)
                self.assertEqual(manifest["assets"]["walk_side"]["frame_count"], 6)
                self.assertEqual(len(manifest["assets"]["walk_side_right"]["frames"]["238"]), 6)
                self.assertEqual(manifest["actions"]["WALKING"]["transition"]["enter"]["asset"], "walk_start_left")
                self.assertEqual(manifest["actions"]["SLEEPING"]["transition"]["exit"]["asset"], "sleep_wake")
                self.assertEqual(manifest["assets"]["idle_glance"]["frame_count"], 7)
                self.assertTrue(manifest["actions"]["FALLING"]["loop"])
                # Character manifests say "assets/processed/..." but the files
                # live in the form folder; every referenced frame must resolve.
                for asset in manifest["assets"].values():
                    for frames in asset["frames"].values():
                        for raw in frames:
                            path = registry._resolve_frame_path(raw)
                            self.assertTrue(path.is_file(), path)
                            self.assertIn(form, path.parts)

    def test_every_action_loads_frames_for_every_form(self) -> None:
        from PySide6.QtWidgets import QApplication

        QApplication.instance() or QApplication([])
        root = Path(__file__).resolve().parents[1]
        base = root / "assets" / "manifests" / "actions_base.json"
        for form in ("carbon", "hydergon", "neon", "argon", "claude"):
            registry = AssetRegistry(root / form, base_manifest=base)
            for action in PetAction:
                for direction in ("left", "right", "up", "down"):
                    program = registry.program_for(action, height=238, direction=direction)
                    self.assertTrue(program.body.frames, (form, action, direction))
                    self.assertFalse(program.body.frames[0].pixmap.isNull())

    def test_sleep_frames_are_normalized_to_standing_scale(self) -> None:
        from PySide6.QtWidgets import QApplication

        QApplication.instance() or QApplication([])
        root = Path(__file__).resolve().parents[1]
        base = root / "assets" / "manifests" / "actions_base.json"
        max_allowed_width = round(238 * 1.05)
        for form in ("carbon", "hydergon", "neon", "argon", "claude"):
            with self.subTest(form=form):
                registry = AssetRegistry(root / form, base_manifest=base)
                program = registry.program_for(PetAction.SLEEPING, height=238, direction="right")
                for clip in (program.body, program.enter, program.exit):
                    self.assertIsNotNone(clip)
                    for frame in clip.frames:
                        self.assertLessEqual(frame.pixmap.width(), max_allowed_width)
                # Horizontal lying sleep frames shrink below standing height.
                self.assertLess(program.body.frames[0].pixmap.height(), 200)
                # Standing yawn frame at the start of sleep_enter keeps full standing height.
                self.assertEqual(program.enter.frames[0].pixmap.height(), 238)


if __name__ == "__main__":
    unittest.main()
