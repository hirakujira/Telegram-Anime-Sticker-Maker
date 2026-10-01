#!/usr/bin/env python3
"""Recover a Bahamut HLS download from a Downie .downiepart plist.

The plist supplies a short-lived signed segment URL and request headers. The
script fetches the matching media playlist, downloads its segments, and remuxes
the resulting MPEG-TS stream to MP4 with FFmpeg.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import http.client
import json
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
except ImportError:
    print("Missing dependency: install it with `python -m pip install -r requirements.txt`", file=sys.stderr)
    raise SystemExit(2)


ALLOWED_HEADERS = {
    "Accept", "Origin", "Referer", "Sec-Fetch-Dest", "Sec-Fetch-Mode",
    "Sec-Fetch-Site", "User-Agent",
}
SEGMENT_NAME = re.compile(r"media_(b\d+)_\d+\.ts")


@dataclass
class Segment:
    url: str
    sequence: int
    encrypted: bool
    iv: bytes | None


def parse_attributes(value: str) -> dict[str, str]:
    parts = re.findall(r'(?:^|,)([^,=]+)=((?:"[^"]*")|[^,]*)', value)
    return {name.strip(): item.strip().strip('"') for name, item in parts}


def parse_playlist(text: str, playlist_url: str) -> list[Segment]:
    lines = [line.strip() for line in text.lstrip("\ufeff").splitlines()]
    if not lines or lines[0] != "#EXTM3U":
        raise ValueError("URL did not return a valid M3U8 playlist")
    if any(line.startswith("#EXT-X-STREAM-INF:") for line in lines):
        raise ValueError("This is a master playlist, not a media playlist")
    if any(line.startswith("#EXT-X-MAP:") for line in lines):
        raise ValueError("fMP4 playlists are not supported")

    sequence = 0
    encrypted = False
    active_iv: bytes | None = None
    segments = []
    for line in lines[1:]:
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            sequence = int(line.partition(":")[2])
        elif line.startswith("#EXT-X-KEY:"):
            attrs = parse_attributes(line.partition(":")[2])
            method = attrs.get("METHOD", "").upper()
            if method == "NONE":
                encrypted = False
                active_iv = None
            elif method == "AES-128":
                encrypted = True
                iv_text = attrs.get("IV")
                if iv_text:
                    if not re.fullmatch(r"0[xX][0-9a-fA-F]{1,32}", iv_text):
                        raise ValueError("Invalid AES-128 IV")
                    active_iv = int(iv_text[2:], 16).to_bytes(16, "big")
                else:
                    active_iv = None
            else:
                raise ValueError(f"Unsupported encryption method: {method or '(missing)'}")
        elif line and not line.startswith("#"):
            segments.append(Segment(urllib.parse.urljoin(playlist_url, line), sequence, encrypted, active_iv))
            sequence += 1
    if not segments:
        raise ValueError("Playlist contains no media segments")
    return segments


def decrypt_segment(data: bytes, key: bytes, iv: bytes) -> bytes:
    if not data or len(data) % 16:
        raise ValueError("Encrypted segment length is not a non-zero multiple of 16 bytes")
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    padded = decryptor.update(data) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    return unpadder.update(padded) + unpadder.finalize()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def find_plist(source: Path) -> Path:
    source = source.expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(f"Source not found: {source}")
    if source.is_file() and source.suffix == ".plist":
        return source
    if source.is_dir() and source.name.endswith(".downiepart"):
        matches = list(source.glob("*.plist"))
        if len(matches) == 1:
            return matches[0]
        raise ValueError(f"Expected one plist in {source}, found {len(matches)}")
    raise ValueError("Pass a .downiepart folder or its .plist file")


def load_source(plist_path: Path) -> tuple[str, str, dict[str, str]]:
    metadata = plistlib.loads(plist_path.read_bytes())
    chunks = metadata.get("chunksInProgress", [])
    if not chunks:
        raise ValueError("The plist has no segment URL; make a fresh Downie attempt")
    chunk = chunks[0].get("chunk", {})
    segment_url = chunk.get("url", "")
    parsed = urllib.parse.urlsplit(segment_url)
    match = SEGMENT_NAME.fullmatch(parsed.path.rsplit("/", 1)[-1])
    if not match:
        raise ValueError("This plist does not contain a supported Bahamut HLS segment")
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Expected an HTTPS CDN segment URL")
    playlist_url = segment_url.rsplit("/", 1)[0] + f"/chunklist_{match.group(1)}.m3u8"
    saved_headers = metadata.get("additionalHTTPHeaderFields", {})
    headers = {
        name: item["value"]
        for name, item in saved_headers.items()
        if name in ALLOWED_HEADERS and isinstance(item, dict)
        and isinstance(item.get("value"), str)
    }
    return playlist_url, segment_url, headers


def same_cdn(url: str, hostname: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname != hostname
            or parsed.username or parsed.password or parsed.port not in (None, 443)):
        raise ValueError("Playlist refers to a URL outside its HTTPS CDN host")


def fetch(opener, url: str, headers: dict[str, str], hostname: str, label: str) -> bytes:
    same_cdn(url, hostname)
    request = urllib.request.Request(url, headers=headers)
    for attempt in range(4):
        try:
            with opener.open(request, timeout=30) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code not in (408, 429) and exc.code < 500:
                raise RuntimeError(f"{label}: HTTP {exc.code}; the signed link may have expired") from None
            problem = f"HTTP {exc.code}"
        except (urllib.error.URLError, TimeoutError) as exc:
            problem = str(exc.reason) if isinstance(exc, urllib.error.URLError) else "timed out"
        except (http.client.IncompleteRead, http.client.RemoteDisconnected, ConnectionResetError) as exc:
            problem = type(exc).__name__
        if attempt < 3:
            time.sleep(attempt + 1)
    raise RuntimeError(f"{label}: {problem}")


def valid_ts(data: bytes) -> bool:
    return bool(data and len(data) % 188 == 0 and
                all(data[i] == 0x47 for i in range(0, min(len(data), 564), 188)))


def valid_cached_segment(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size == 0 or path.stat().st_size % 188:
        return False
    with path.open("rb") as source:
        head = source.read(564)
    return all(head[i] == 0x47 for i in range(0, len(head), 188))


def cache_identity(playlist_url: str, segments: list[Segment], key: bytes | None) -> str:
    parsed = urllib.parse.urlsplit(playlist_url)
    # Ignore the short-lived hdntl token, so a fresh Downie attempt can resume
    # the same media. Keep the content path, rendition, IVs, and key digest.
    stable_path = "/".join(part for part in parsed.path.split("/") if not part.startswith("hdntl="))
    details = {
        "host": parsed.hostname,
        "path": stable_path,
        "key_sha256": hashlib.sha256(key).hexdigest() if key else None,
        "segments": [
            [segment.sequence, urllib.parse.urlsplit(segment.url).path.rsplit("/", 1)[-1],
             segment.encrypted, segment.iv.hex() if segment.iv else None]
            for segment in segments
        ],
    }
    return hashlib.sha256(json.dumps(details, sort_keys=True).encode()).hexdigest()


def output_path(plist_path: Path, output: Path | None) -> Path:
    return (output.expanduser() if output else
            plist_path.parent.parent / f"{plist_path.parent.name.removesuffix('.downiepart')}.mp4")


@contextmanager
def output_lock(target: Path):
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.with_name(target.name + ".lock")
    with lock_path.open("a+") as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError(f"Another recovery is already running for {target}") from None
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def key_url_from_playlist(text: str, playlist_url: str) -> str | None:
    keys = set()
    for line in text.splitlines():
        if not line.startswith("#EXT-X-KEY:"):
            continue
        attrs = parse_attributes(line.partition(":")[2])
        if attrs.get("METHOD", "").upper() == "AES-128":
            if not attrs.get("URI"):
                raise ValueError("AES-128 key URL is missing")
            keys.add(urllib.parse.urljoin(playlist_url, attrs["URI"]))
    if len(keys) > 1:
        raise ValueError("This script does not support playlists with rotating keys")
    return next(iter(keys), None)


def recover(source: Path, output: Path | None, workers: int, dry_run: bool) -> Path | None:
    if dry_run:
        return _recover_unlocked(source, output, workers, True)
    target = output_path(find_plist(source), output)
    with output_lock(target):
        return _recover_unlocked(source, output, workers, False)


def _recover_unlocked(source: Path, output: Path | None, workers: int, dry_run: bool) -> Path | None:
    plist_path = find_plist(source)
    playlist_url, saved_segment_url, headers = load_source(plist_path)
    hostname = urllib.parse.urlsplit(playlist_url).hostname
    assert hostname is not None
    opener = urllib.request.build_opener(NoRedirect())
    playlist = fetch(opener, playlist_url, headers, hostname, "Playlist").decode("utf-8-sig")
    if "#EXT-X-ENDLIST" not in playlist:
        raise ValueError("The media playlist is incomplete or still live")
    segments = parse_playlist(playlist, playlist_url)
    if saved_segment_url not in {segment.url for segment in segments}:
        raise ValueError("The inferred playlist does not match the saved Downie segment")
    key_url = key_url_from_playlist(playlist, playlist_url)
    if any(segment.encrypted for segment in segments) and key_url is None:
        raise ValueError("Playlist requires an AES-128 key but has no key URL")
    for segment in segments:
        same_cdn(segment.url, hostname)
    if key_url:
        same_cdn(key_url, hostname)

    target = output_path(plist_path, output)
    print(f"Found {len(segments)} segments on {hostname}; output: {target}", flush=True)
    if dry_run:
        return None
    if target.exists():
        raise FileExistsError(f"Output already exists: {target}")
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        raise RuntimeError("FFmpeg and FFprobe must be installed and available in PATH")
    target.parent.mkdir(parents=True, exist_ok=True)
    ts_part = target.with_name(target.name + ".ts.part")
    mp4_part = target.with_name(target.name + ".mp4.part")
    cache_dir = target.with_name(target.name + ".segments")
    key = fetch(opener, key_url, headers, hostname, "AES-128 key") if key_url else None
    if key is not None and len(key) != 16:
        raise ValueError("The HLS key is not 16 bytes")
    identity = cache_identity(playlist_url, segments, key)
    manifest = cache_dir / "manifest.json"
    if cache_dir.exists():
        if not manifest.is_file() or json.loads(manifest.read_text()) != {"identity": identity}:
            raise ValueError(f"Segment cache belongs to a different download: {cache_dir}")
    else:
        cache_dir.mkdir()
        manifest.write_text(json.dumps({"identity": identity}))
    cached_count = sum(valid_cached_segment(cache_dir / f"{segment.sequence:06d}.ts") for segment in segments)
    if cached_count:
        print(f"Resuming with {cached_count}/{len(segments)} cached segments", flush=True)

    def get_segment(segment) -> Path:
        destination = cache_dir / f"{segment.sequence:06d}.ts"
        if valid_cached_segment(destination):
            return destination
        for attempt in range(3):
            data = fetch(opener, segment.url, headers, hostname, f"Segment {segment.sequence}")
            try:
                if segment.encrypted:
                    assert key is not None
                    data = decrypt_segment(data, key, segment.iv or segment.sequence.to_bytes(16, "big"))
                if not valid_ts(data):
                    raise ValueError("invalid MPEG-TS packets")
            except ValueError as exc:
                if attempt == 2:
                    raise ValueError(f"Segment {segment.sequence} could not be validated: {exc}") from exc
                time.sleep(attempt + 1)
                continue
            partial = destination.with_suffix(".ts.part")
            partial.write_bytes(data)
            partial.replace(destination)
            return destination
        raise AssertionError("Unreachable segment retry state")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for start in range(0, len(segments), workers * 2):
            batch = segments[start:start + workers * 2]
            list(pool.map(get_segment, batch))
            print(f"Ready {start + len(batch)}/{len(segments)} segments", flush=True)

    # Rebuild the transport stream from verified, ordered cached segments.
    ts_part.unlink(missing_ok=True)
    mp4_part.unlink(missing_ok=True)
    with ts_part.open("wb") as destination:
        for segment in segments:
            with (cache_dir / f"{segment.sequence:06d}.ts").open("rb") as source:
                shutil.copyfileobj(source, destination)

    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-i", str(ts_part),
             "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", "-bsf:a", "aac_adtstoasc",
             "-movflags", "+faststart", "-f", "mp4", str(mp4_part)],
            capture_output=True, text=True,
        )
        if result.returncode:
            raise RuntimeError("FFmpeg failed: " + result.stderr.strip()[-1000:])
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(mp4_part)],
            capture_output=True, text=True, check=True,
        )
        info = json.loads(probe.stdout)
        if not any(stream.get("codec_type") == "video" for stream in info.get("streams", [])):
            raise RuntimeError("The MP4 has no video stream")
        if float(info.get("format", {}).get("duration", 0)) <= 0:
            raise RuntimeError("The MP4 has no valid duration")
        mp4_part.replace(target)
        ts_part.unlink()
        try:
            shutil.rmtree(cache_dir)
        except OSError as exc:
            print(f"Warning: could not remove segment cache: {exc}", file=sys.stderr)
    except Exception:
        mp4_part.unlink(missing_ok=True)
        raise
    print(f"Saved: {target}")
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Downie .downiepart directory or its .plist file")
    parser.add_argument("-o", "--output", type=Path, help="MP4 path (default: next to .downiepart)")
    parser.add_argument("-j", "--workers", type=int, default=6, help="parallel downloads (default: 6)")
    parser.add_argument("--dry-run", action="store_true", help="inspect the playlist without downloading")
    args = parser.parse_args()
    if not 1 <= args.workers <= 16:
        parser.error("--workers must be between 1 and 16")
    try:
        recover(args.source, args.output, args.workers, args.dry_run)
    except (OSError, ValueError, RuntimeError, UnicodeError, subprocess.CalledProcessError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
