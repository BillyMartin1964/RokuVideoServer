import tempfile
import unittest
from pathlib import Path

from models.video_model import create_video_model


class VideoSizeTests(unittest.TestCase):
    def test_invalid_primary_size_uses_positive_alias(self):
        # The physical path may be unavailable while a drive is unmounted.
        for primary in (0, None, "", "invalid", -1):
            for alias in ("filesize", "size"):
                with self.subTest(primary=primary, alias=alias):
                    model = create_video_model({"fileSize": primary, alias: 5_000_000_000})
                    self.assertEqual(model.fileSize, 5_000_000_000)

    def test_primary_positive_size_takes_precedence(self):
        model = create_video_model({"fileSize": 123, "size": 456})
        self.assertEqual(model.fileSize, 123)

    def test_missing_size_is_recovered_from_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "video.mp4"
            path.write_bytes(b"video data")
            model = create_video_model({"fileSize": 0, "fullPath": str(path)})
            self.assertEqual(model.fileSize, path.stat().st_size)

    def test_signed_legacy_size_is_preserved_without_valid_alias(self):
        model = create_video_model({"fileSize": -1_294_967_296})
        self.assertEqual(model.fileSize, 3_000_000_000)
