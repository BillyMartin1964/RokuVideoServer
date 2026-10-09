import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from services import ffmpeg_service, video_model_service as thumbnails


@unittest.skipUnless(shutil.which('ffmpeg'), 'ffmpeg is required')
class ThumbnailTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.video = cls.root / 'sample.mp4'
        cls.ffmpeg = shutil.which('ffmpeg')
        # Opening preview is red, target starts black, actual content is blue.
        subprocess.run([
            cls.ffmpeg, '-v', 'error', '-f', 'lavfi', '-i',
            'color=red:s=96x64:r=2:d=10', '-f', 'lavfi', '-i',
            'color=black:s=96x64:r=2:d=81', '-f', 'lavfi', '-i',
            'color=blue:s=96x64:r=2:d=19', '-filter_complex',
            '[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]', '-map', '[v]',
            '-c:v', 'mpeg4', '-y', str(cls.video),
        ], check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def assert_blue(self, path):
        pixel = subprocess.run([
            self.ffmpeg, '-v', 'error', '-i', str(path), '-vf',
            'format=rgb24,crop=1:1:296:166', '-f', 'rawvideo', '-',
        ], check=True, capture_output=True).stdout
        self.assertGreater(pixel[2], 150)
        self.assertLess(pixel[0], 40)

    def test_capture_skips_black_and_opening_preview(self):
        target = self.root / 'normal.jpg'
        with patch.object(ffmpeg_service, 'FFMPEG_PATH', self.ffmpeg):
            self.assertIs(thumbnails.run_ffmpeg_thumbnail(str(self.video), str(target), 90), True)
        self.assert_blue(target)

    def test_preroll_retry_still_captures_target(self):
        target = self.root / 'retry.jpg'
        real_run = subprocess.run
        calls = []

        def fail_first(cmd, **kwargs):
            calls.append(cmd)
            if len(calls) == 1:
                return subprocess.CompletedProcess(cmd, 1, '', 'damaged keyframe')
            return real_run(cmd, **kwargs)

        with patch.object(ffmpeg_service, 'FFMPEG_PATH', self.ffmpeg), patch.object(
            thumbnails.subprocess, 'run', side_effect=fail_first
        ):
            self.assertIs(thumbnails.run_ffmpeg_thumbnail(str(self.video), str(target), 90), True)
        self.assertEqual(len(calls), 2)
        self.assert_blue(target)

    def test_no_frame_does_not_accept_old_output(self):
        target = self.root / 'old.jpg'
        target.write_bytes(b'previous output')
        with patch.object(ffmpeg_service, 'FFMPEG_PATH', self.ffmpeg):
            self.assertFalse(thumbnails.run_ffmpeg_thumbnail(str(self.video), str(target), 20))
        self.assertEqual(target.read_bytes(), b'previous output')

    def test_broken_container_stops_without_retry(self):
        broken = self.root / 'broken.mp4'
        broken.write_bytes(self.video.read_bytes()[:64])
        real_run = subprocess.run
        with patch.object(ffmpeg_service, 'FFMPEG_PATH', self.ffmpeg), patch.object(
            thumbnails.subprocess, 'run', wraps=real_run
        ) as run:
            self.assertEqual(thumbnails.run_ffmpeg_thumbnail(
                str(broken), str(self.root / 'broken.jpg'), 90
            ), thumbnails.FFMPEG_THUMBNAIL_FATAL_STRUCTURE)
        self.assertEqual(run.call_count, 1)

    def test_failed_source_retried_only_after_change(self):
        source = self.root / 'changed.mp4'
        source.write_bytes(b'bad')
        with patch.object(thumbnails, 'THUMB_CACHE_DIR', str(self.root)), patch.object(
            ffmpeg_service, 'FFMPEG_PATH', self.ffmpeg
        ), patch.object(ffmpeg_service, 'probe_video_metadata', return_value={}), patch.object(
            thumbnails, 'run_ffmpeg_thumbnail', return_value=thumbnails.FFMPEG_THUMBNAIL_FATAL_STRUCTURE
        ) as capture, patch.object(thumbnails, 'ensure_default_poster', return_value=True):
            thumbnails.generate_thumbnail(str(source))
            thumbnails.generate_thumbnail(str(source))
            self.assertEqual(capture.call_count, 1)
            source.write_bytes(b'replacement')
            thumbnails.generate_thumbnail(str(source))
            self.assertEqual(capture.call_count, 2)


if __name__ == '__main__':
    unittest.main()
