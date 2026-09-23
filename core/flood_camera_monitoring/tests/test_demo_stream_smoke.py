import json
import shutil
import subprocess
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, skipUnless

from core.flood_camera_monitoring.demo.controller import DemoStreamController
from core.flood_camera_monitoring.demo.manifest import load_scenario


@skipUnless(shutil.which("ffmpeg"), "FFmpeg is required for the HLS smoke test")
class DemoStreamSmokeTests(TestCase):
    def test_generates_an_auto_hls_playlist(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self._make_video(root / "auto.mp4", "green")
            self._make_video(root / "normal.mp4", "blue")
            self._make_video(root / "flooded.mp4", "red")
            manifest = root / "scenario.json"
            manifest.write_text(
                json.dumps(
                    {
                        "scenario_id": "smoke-test",
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
                ),
                encoding="utf-8",
            )
            controller = DemoStreamController(
                load_scenario(manifest),
                root / "work",
                public_hls_url="http://public/hls/playlist.m3u8",
                internal_base_url="http://internal",
            )
            try:
                controller.initialize()
                deadline = time.monotonic() + 10
                snapshot = controller.snapshot()
                while snapshot["segment"] is None and time.monotonic() < deadline:
                    time.sleep(0.2)
                    snapshot = controller.snapshot()

                self.assertEqual(snapshot["status"], "ready")
                self.assertTrue((controller.hls_dir / "playlist.m3u8").is_file())
                self.assertIsNone(snapshot["segment"]["expected_state"])
                self.assertTrue(snapshot["segment"]["internal_url"].endswith(".ts"))
            finally:
                controller.close()

    @staticmethod
    def _make_video(path: Path, color: str) -> None:
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"color=c={color}:s=64x64:r=10:d=1",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(path),
            ],
            check=True,
        )
