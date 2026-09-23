import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from core.flood_camera_monitoring.demo.controller import DemoStreamController
from core.flood_camera_monitoring.demo.manifest import load_scenario


class DemoControllerTests(TestCase):
    def test_auto_segments_have_no_expected_state_but_forced_states_do(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "normal.mp4").touch()
            (root / "auto.mp4").touch()
            (root / "flooded.mp4").touch()
            manifest = root / "scenario.json"
            manifest.write_text(
                json.dumps(
                    {
                        "scenario_id": "controller-label-test",
                        "segment_seconds": 2,
                        "phases": [
                            {
                                "name": "auto",
                                "file": "auto.mp4",
                                "label": None,
                                "duration_seconds": 2,
                            },
                            {
                                "name": "normal",
                                "file": "normal.mp4",
                                "label": "normal",
                                "duration_seconds": 2,
                            },
                            {
                                "name": "flooded",
                                "file": "flooded.mp4",
                                "label": "flooded",
                                "duration_seconds": 2,
                            },
                        ],
                    }
                )
            )
            controller = DemoStreamController(
                load_scenario(manifest),
                root / "work",
                public_hls_url="http://public/hls/playlist.m3u8",
                internal_base_url="http://internal",
            )
            controller.hls_dir.mkdir(parents=True)
            (controller.hls_dir / "seg_000000000.ts").touch()

            controller.state = "auto"
            self.assertIsNone(controller._latest_segment()["expected_state"])

            controller.state = "normal"
            self.assertEqual(controller._latest_segment()["expected_state"], "normal")

            controller.state = "flooded"
            self.assertEqual(controller._latest_segment()["expected_state"], "flooded")

    def test_every_state_change_creates_a_new_session(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "normal.mp4").touch()
            (root / "auto.mp4").touch()
            (root / "flooded.mp4").touch()
            manifest = root / "scenario.json"
            manifest.write_text(
                json.dumps(
                    {
                        "scenario_id": "controller-test",
                        "segment_seconds": 2,
                        "phases": [
                            {
                                "name": "auto",
                                "file": "auto.mp4",
                                "label": None,
                                "duration_seconds": 2,
                            },
                            {
                                "name": "normal",
                                "file": "normal.mp4",
                                "label": "normal",
                                "duration_seconds": 2,
                            },
                            {
                                "name": "flooded",
                                "file": "flooded.mp4",
                                "label": "flooded",
                                "duration_seconds": 2,
                            },
                        ],
                    }
                )
            )
            controller = DemoStreamController(
                load_scenario(manifest),
                root / "work",
                public_hls_url="http://public/hls/playlist.m3u8",
                internal_base_url="http://internal",
            )
            controller.hls_dir.mkdir(parents=True)
            controller._normalized = {
                "auto": root / "auto.mp4",
                "normal": root / "normal.mp4",
                "flooded": root / "flooded.mp4",
            }

            with patch.object(controller, "_promote") as promote:
                promote.side_effect = lambda state, source: (
                    setattr(controller, "state", state),
                    setattr(controller, "session_id", f"session-{promote.call_count}"),
                )
                first = controller.set_state("normal")
                second = controller.set_state("normal")

            self.assertNotEqual(first["session_id"], second["session_id"])
            self.assertEqual(second["demo_state"], "normal")

    def test_failed_active_source_preparation_preserves_current_session(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            source.touch()
            manifest = root / "scenario.json"
            manifest.write_text(json.dumps({
                "scenario_id": "atomic-test", "segment_seconds": 2,
                "phases": [
                    {"name": "auto", "file": "source.mp4", "label": None, "duration_seconds": 2},
                    {"name": "normal", "file": "source.mp4", "label": "normal", "duration_seconds": 2},
                    {"name": "flooded", "file": "source.mp4", "label": "flooded", "duration_seconds": 2},
                ],
            }))
            controller = DemoStreamController(
                load_scenario(manifest), root / "work",
                public_hls_url="http://public/hls/playlist.m3u8",
                internal_base_url="http://internal", video_resolver=lambda key: source,
            )
            controller.state = "normal"
            controller.session_id = "current-session"
            controller._normalized["normal"] = source
            with (
                patch.object(controller, "_normalize_phase"),
                patch.object(controller, "_promote", side_effect=RuntimeError("not ready")),
                self.assertRaisesRegex(Exception, "not ready"),
            ):
                controller.prepare_source("normal", "attachment")

            self.assertEqual(controller.session_id, "current-session")
            self.assertEqual(controller._normalized["normal"], source)
            self.assertEqual(controller._sources["normal"]["status"], "error")
