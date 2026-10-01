import io
import http.client
from pathlib import Path
import plistlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

import recover_downiepart


class FakeOpener:
    def __init__(self, responses):
        self.responses = responses
        self.fail_url = None
        self.calls = {}

    def open(self, request, timeout=30):
        url = request.full_url
        self.calls[url] = self.calls.get(url, 0) + 1
        if url == self.fail_url:
            raise http.client.IncompleteRead(b"partial", 8)
        return io.BytesIO(self.responses[url])


class RecoveryTests(unittest.TestCase):
    def test_same_output_cannot_run_twice(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "result.mp4"
            with recover_downiepart.output_lock(target):
                with self.assertRaisesRegex(RuntimeError, "already running"):
                    with recover_downiepart.output_lock(target):
                        pass

    def test_incomplete_read_is_retried(self):
        url = "https://bahamut.akamaized.net/segment.ts"
        opener = FakeOpener({url: b"complete"})
        original_open = opener.open

        def fail_once(request, timeout=30):
            opener.open = original_open
            raise http.client.IncompleteRead(b"partial", 8)

        opener.open = fail_once
        with patch.object(recover_downiepart.time, "sleep"):
            self.assertEqual(recover_downiepart.fetch(opener, url, {}, "bahamut.akamaized.net", "Segment 0"), b"complete")
        self.assertEqual(opener.calls[url], 1)

    def test_failed_segment_is_resumed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "sample.downiepart"
            source.mkdir()
            subprocess.run([
                "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=160x90:rate=25",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100", "-t", "2",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "25", "-c:a", "aac",
                "-f", "hls", "-hls_time", "1", "-hls_list_size", "0",
                "-hls_segment_filename", str(root / "seg%d.ts"), str(root / "source.m3u8"),
            ], check=True)
            media = sorted(root.glob("seg*.ts"))
            self.assertGreaterEqual(len(media), 2)
            base = "https://bahamut.akamaized.net/test/720p/hdntl=example/"
            key, iv = b"0123456789abcdef", bytes(range(16))
            responses = {base + "key_b1200000.m3u8key": key}
            playlist = ["#EXTM3U", "#EXT-X-KEY:METHOD=AES-128,URI=\"key_b1200000.m3u8key\",IV=0x" + iv.hex()]
            for index, path in enumerate(media):
                name = f"media_b1200000_{index}.ts"
                padder = padding.PKCS7(128).padder()
                padded = padder.update(path.read_bytes()) + padder.finalize()
                encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
                responses[base + name] = encryptor.update(padded) + encryptor.finalize()
                playlist.extend(["#EXTINF:1,", name])
            playlist.append("#EXT-X-ENDLIST")
            responses[base + "chunklist_b1200000.m3u8"] = ("\n".join(playlist) + "\n").encode()
            with (source / "source.plist").open("wb") as destination:
                plistlib.dump({"chunksInProgress": [{"chunk": {"url": base + "media_b1200000_0.ts"}}]}, destination)

            opener = FakeOpener(responses)
            output = root / "result.mp4"
            opener.fail_url = base + "media_b1200000_1.ts"
            with patch.object(recover_downiepart.urllib.request, "build_opener", return_value=opener), patch.object(recover_downiepart.time, "sleep"):
                with self.assertRaises(RuntimeError):
                    recover_downiepart.recover(source, output, 1, False)
                self.assertTrue((root / "result.mp4.segments" / "000000.ts").is_file())
                first_calls = opener.calls[base + "media_b1200000_0.ts"]
                opener.fail_url = None
                recover_downiepart.recover(source, output, 1, False)
            self.assertEqual(opener.calls[base + "media_b1200000_0.ts"], first_calls)
            self.assertTrue(output.is_file())
            self.assertFalse((root / "result.mp4.segments").exists())
            subprocess.run(["ffmpeg", "-v", "error", "-i", str(output), "-f", "null", "-"], check=True)


if __name__ == "__main__":
    unittest.main()
