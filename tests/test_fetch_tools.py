"""Le build Windows ne doit pas embarquer les DLL FFmpeg bloquées par Smart App Control."""
from __future__ import annotations

import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from scripts import fetch_tools


class StaticFfmpegPackageTests(unittest.TestCase):
    def test_windows_fetch_keeps_only_self_contained_ffmpeg_programs(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("ffmpeg-n8.1/bin/ffmpeg.exe", b"static ffmpeg")
            zf.writestr("ffmpeg-n8.1/bin/ffprobe.exe", b"static ffprobe")
            zf.writestr("ffmpeg-n8.1/bin/avformat-62.dll", b"shared library")
            zf.writestr("ffmpeg-n8.1/bin/swscale-9.dll", b"shared library")

        with tempfile.TemporaryDirectory() as tmp:
            binary_dir = Path(tmp)
            stale = binary_dir / "avformat-62.dll"
            stale.write_bytes(b"DLL from previous shared build")
            with mock.patch.object(fetch_tools, "BIN", str(binary_dir)), \
                 mock.patch.object(fetch_tools, "download", return_value=archive.getvalue()) as download:
                fetch_tools.fetch_ffmpeg("windows")

            self.assertNotIn("shared", fetch_tools.FFMPEG["windows"])
            self.assertIn("gpl-8.1.zip", fetch_tools.FFMPEG["windows"])
            self.assertEqual((binary_dir / "ffmpeg.exe").read_bytes(), b"static ffmpeg")
            self.assertEqual((binary_dir / "ffprobe.exe").read_bytes(), b"static ffprobe")
            self.assertFalse(stale.exists())
            self.assertFalse(list(binary_dir.glob("av*.dll")))
            self.assertFalse(list(binary_dir.glob("sw*.dll")))
            download.assert_called_once_with(fetch_tools.FFMPEG["windows"])

    def test_incomplete_static_package_fails_the_windows_build(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("ffmpeg/bin/ffmpeg.exe", b"only ffmpeg")

        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(fetch_tools, "BIN", tmp), \
             mock.patch.object(fetch_tools, "download", return_value=archive.getvalue()):
            with self.assertRaisesRegex(RuntimeError, "ffmpeg.exe et ffprobe.exe"):
                fetch_tools.fetch_ffmpeg("windows")


if __name__ == "__main__":
    unittest.main()
