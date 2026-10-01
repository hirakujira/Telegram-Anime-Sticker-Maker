"""Source-frame indexing and FFmpeg rendering; no full image sequence required."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from fractions import Fraction

EXTENSIONS = {'.mp4', '.mkv', '.mov', '.webm', '.avi', '.m4v', '.mpeg', '.mpg', '.mts', '.m2ts'}
MAX_BYTES = 256_000  # Conservative interpretation of Telegram's 256 KB limit.
OUTPUT_FPS = 30


class MediaError(ValueError):
    pass


def run(args, *, timeout=600, cancel_event=None):
    try:
        with subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as process:
            deadline = time.monotonic() + timeout
            try:
                while True:
                    if cancel_event is not None and cancel_event.is_set():
                        raise MediaError('工作已中止。')
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise MediaError('處理逾時，請嘗試較短的影片。')
                    try:
                        stdout, stderr = process.communicate(timeout=min(.25, remaining))
                        break
                    except subprocess.TimeoutExpired:
                        continue
            except BaseException:
                process.kill()
                process.communicate()
                raise
    except subprocess.TimeoutExpired as exc:
        raise MediaError('處理逾時，請嘗試較短的影片。') from exc
    except OSError as exc:
        raise MediaError(f'無法執行 {args[0]}：{exc}') from exc
    if process.returncode:
        lines = stderr.strip().splitlines()
        raise MediaError('\n'.join(lines[-8:]) or '影片處理失敗。')
    return stdout


def probe(path, *, cancel_event=None):
    return json.loads(run(['ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(path)],
                          cancel_event=cancel_event))


def video_stream(data):
    streams = [s for s in data['streams'] if s['codec_type'] == 'video' and not s.get('disposition', {}).get('attached_pic')]
    if not streams:
        raise MediaError('找不到影片影像軌。')
    return streams[0]


def ratio(value, default=1.0):
    try:
        parsed = float(Fraction(str(value).replace(':', '/')))
        return parsed if math.isfinite(parsed) and parsed > 0 else default
    except (ValueError, ZeroDivisionError):
        return default


def geometry(stream):
    width, height = stream['width'], stream['height']
    sar = ratio(stream.get('sample_aspect_ratio', '1:1'))
    display_width = max(1, int(math.floor(width * sar + .5)))
    rotation = next((float(d['rotation']) for d in stream.get('side_data_list', []) if 'rotation' in d), None)
    if rotation is None:
        # Legacy rotate tags use the opposite convention to the display matrix.
        rotation = -float(stream.get('tags', {}).get('rotate', 0))
    quarter = round(rotation / 90)
    if abs(rotation - quarter * 90) > .01:
        raise MediaError('目前僅支援 90 度倍數的影片旋轉。')
    filters = [f'scale={display_width}:{height}:flags=lanczos', 'setsar=1']
    turn = quarter % 4
    if turn == 1:
        filters.append('transpose=cclock')
    elif turn == 2:
        filters += ['hflip', 'vflip']
    elif turn == 3:
        filters.append('transpose=clock')
    if turn % 2:
        display_width, height = height, display_width
    return display_width, height, filters


def integer(value, name):
    if isinstance(value, bool) or not isinstance(value, int):
        raise MediaError(f'{name}必須是整數。')
    return value


class Library:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.cache = self.root / '.cache'
        self.output = self.root / 'stickers'
        self.cache.mkdir(exist_ok=True)
        self.output.mkdir(exist_ok=True)
        self.lock = threading.RLock()
        self.frame_lock = threading.Lock()
        self.stopping = threading.Event()
        self.sources = {}
        self.indices = {}
        self.jobs = {}
        self.preview_jobs = {}
        self.index_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='index')
        self.work_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='render')

    def _run(self, args, *, timeout=600):
        return run(args, timeout=timeout, cancel_event=self.stopping)

    def scan(self):
        found = {}
        for path in sorted(self.root.iterdir(), key=lambda p: p.name.casefold()):
            if path.suffix.lower() not in EXTENSIONS or not path.is_file() or path.is_symlink():
                continue
            stat = path.stat()
            identity = f'{path.name}|{stat.st_size}|{stat.st_mtime_ns}'
            source_id = hashlib.sha256(identity.encode()).hexdigest()[:24]
            found[source_id] = {'id': source_id, 'name': path.name, 'bytes': stat.st_size,
                                'mtime_ns': stat.st_mtime_ns, 'path': path}
        with self.lock:
            self.sources = found
        return [{k: v for k, v in s.items() if k not in {'path', 'mtime_ns'}} for s in found.values()]

    def source(self, source_id):
        with self.lock:
            source = self.sources.get(source_id)
        if not source:
            raise MediaError('找不到影片，請重新掃描。')
        try:
            stat = source['path'].stat()
        except OSError as exc:
            raise MediaError('影片已移除，請重新掃描。') from exc
        if stat.st_size != source['bytes'] or stat.st_mtime_ns != source['mtime_ns']:
            raise MediaError('影片已變更，請重新掃描後再選取。')
        return source

    def metadata(self, source_id):
        source = self.source(source_id)
        data = probe(source['path'], cancel_event=self.stopping)
        stream = video_stream(data)
        width, height, filters = geometry(stream)
        fps = ratio(stream.get('avg_frame_rate'), ratio(stream.get('r_frame_rate'), 30))
        duration = float(stream.get('duration') or data['format'].get('duration') or 0)
        if not math.isfinite(duration) or duration <= 0:
            raise MediaError('無法取得影片長度。')
        return {'id': source_id, 'name': source['name'], 'width': width, 'height': height,
                'duration': duration, 'fps': fps, 'stream': stream['index'], 'filters': filters,
                'time_base': stream['time_base'], 'format_start': float(data['format'].get('start_time', 0))}

    def begin_index(self, source_id):
        self.source(source_id)
        with self.lock:
            if source_id not in self.indices:
                self.indices[source_id] = {'status': 'indexing', 'message': '讀取原始影格時間…'}
                self.index_pool.submit(self._index, source_id)
            return dict(self.indices[source_id])

    def _index(self, source_id):
        try:
            source = self.source(source_id)
            cached = self.cache / f'{source_id}-index-v2.json'
            if cached.exists():
                data = json.loads(cached.read_text())
            else:
                meta = self.metadata(source_id)
                raw = json.loads(self._run(['ffprobe', '-v', 'error', '-select_streams', str(meta['stream']),
                    '-show_frames', '-show_entries', 'frame=best_effort_timestamp,best_effort_timestamp_time,duration_time,pkt_duration_time',
                    '-of', 'json', str(source['path'])], timeout=7200))
                frames = raw.get('frames', [])
                if not frames:
                    raise MediaError('影片沒有可用影格。')
                pts = [int(f['best_effort_timestamp']) for f in frames]
                time_base = float(Fraction(meta['time_base']))
                stamps = [p * time_base for p in pts]
                origin = stamps[0]
                stamps = [t - origin for t in stamps]
                if any(not math.isfinite(t) for t in stamps) or any(b <= a for a, b in zip(stamps, stamps[1:])):
                    raise MediaError('影格時間戳不連續，無法提供精準選格。')
                last = frames[-1]
                tail = float(last.get('duration_time') or last.get('pkt_duration_time') or 0)
                if tail <= 0:
                    tail = stamps[-1] - stamps[-2] if len(stamps) > 1 else 1 / meta['fps']
                data = {'metadata': meta, 'times': stamps, 'pts': pts, 'origin': origin,
                        'end_time': stamps[-1] + tail, 'count': len(stamps)}
                self.source(source_id)
                temporary = cached.with_suffix('.tmp')
                temporary.write_text(json.dumps(data))
                temporary.replace(cached)
            self.source(source_id)
            with self.lock:
                self.indices[source_id] = {'status': 'ready', **data}
        except Exception as exc:
            with self.lock:
                self.indices[source_id] = {'status': 'failed', 'message': str(exc)}

    def index(self, source_id):
        self.source(source_id)
        with self.lock:
            index = self.indices.get(source_id)
        if not index or index['status'] != 'ready':
            raise MediaError('影格索引尚未完成。')
        return index

    def frame(self, source_id, number):
        source = self.source(source_id)
        index = self.index(source_id)
        if not 1 <= number <= index['count']:
            raise MediaError('影格編號超出範圍。')
        target = self.cache / f'{source_id}-frame-{number}.png'
        # Serializes duplicate extraction and bounds simultaneous decoders.
        with self.frame_lock:
            if not target.exists():
                temporary = target.with_suffix('.tmp.png')
                interval = self.interval(index, number, number)
                filters = [interval['trim'], *index['metadata']['filters']]
                try:
                    self._run(['ffmpeg', '-v', 'error', '-nostdin', '-y', '-noautorotate', '-copyts',
                         '-ss', str(interval['seek']), '-i', str(source['path']),
                         '-map', f"0:{index['metadata']['stream']}", '-vf', ','.join(filters),
                         '-frames:v', '1', '-update', '1', str(temporary)], timeout=7200)
                    self.source(source_id)
                    if not temporary.exists():
                        raise MediaError('無法擷取指定影格。')
                    temporary.replace(target)
                finally:
                    temporary.unlink(missing_ok=True)
                cached = sorted(self.cache.glob('*-frame-*.png'), key=lambda p: p.stat().st_mtime)
                for old in cached[:-128]:
                    if old != target:
                        old.unlink(missing_ok=True)
            os.utime(target, None)
        return target

    def create_job(self, kind, source_id, options=None):
        self.source(source_id)
        if kind == 'preview':
            with self.lock:
                existing = self.preview_jobs.get(source_id)
                if existing and self.jobs[existing]['status'] != 'failed':
                    return existing
            spec = None
        else:
            spec = self.spec(source_id, options or {})
        job_id = uuid.uuid4().hex
        with self.lock:
            self.jobs[job_id] = {'id': job_id, 'kind': kind, 'source_id': source_id,
                                 'status': 'queued', 'message': '排隊等待處理…', 'progress': 0}
            if kind == 'preview':
                self.preview_jobs[source_id] = job_id
        self.work_pool.submit(self._work, job_id, spec)
        return job_id

    def update_job(self, job_id, **changes):
        with self.lock:
            self.jobs[job_id].update(changes)

    def job(self, job_id):
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                raise MediaError('找不到工作；程式重新啟動後，請重新製作。')
            return {k: v for k, v in job.items() if k != 'path'}

    def job_file(self, job_id):
        with self.lock:
            job = self.jobs.get(job_id)
            if not job or job['status'] != 'ready':
                raise MediaError('檔案尚未完成。')
            return Path(job['path'])

    def spec(self, source_id, options):
        index = self.index(source_id)
        first = integer(options.get('start'), '起點影格')
        last = integer(options.get('end'), '終點影格')
        if not 1 <= first <= last <= index['count']:
            raise MediaError('影格範圍必須在影片內，且起點不可大於終點。')
        if isinstance(options.get('speed'), bool):
            raise MediaError('速度必須是大於零的數字。')
        try:
            speed = float(options.get('speed', 1))
        except (TypeError, ValueError) as exc:
            raise MediaError('速度必須是大於零的數字。') from exc
        if not math.isfinite(speed) or speed <= 0:
            raise MediaError('速度必須是大於零的有限數字。')
        finish = index['times'][last] if last < index['count'] else index['end_time']
        source_duration = finish - index['times'][first - 1]
        duration = source_duration / speed
        if duration > 3 + 1e-9:
            required = math.ceil(source_duration / 3 * 1000) / 1000
            raise MediaError(f'成品將長達 {duration:.3f} 秒；請縮短範圍或將速度調至至少 {required:g}×。')
        mode = options.get('mode', 'square')
        if mode not in {'square', 'original'}:
            raise MediaError('請選擇正方形或原始比例。')
        meta = index['metadata']
        width, height = meta['width'], meta['height']
        side = min(width, height)
        x = integer(options.get('x', width - side), '左側裁切像素')
        y = integer(options.get('y', (height - side) // 2), '上側裁切像素')
        if mode == 'square' and not (0 <= x <= width - side and 0 <= y <= height - side):
            raise MediaError('裁切位置超出原圖範圍。')
        dimensions = (512, 512) if mode == 'square' else (
            (512, max(1, round(height / width * 512))) if width >= height
            else (max(1, round(width / height * 512)), 512))
        return {'first': first, 'last': last, 'speed': speed, 'duration': duration,
                'frames': max(1, math.ceil(duration * OUTPUT_FPS - 1e-8)),
                'mode': mode, 'x': x, 'y': y, 'side': side, 'dimensions': dimensions, 'metadata': meta,
                **self.interval(index, first, last)}

    @staticmethod
    def interval(index, first, last):
        """Seek near the cut, then trim by exact original integer presentation timestamps."""
        start_pts = index['pts'][first - 1]
        time_base = float(Fraction(index['metadata']['time_base']))
        end_pts = index['pts'][last] if last < index['count'] else max(
            index['pts'][-1] + 1, round((index['origin'] + index['end_time']) / time_base))
        seek = max(0, index['origin'] + index['times'][first - 1] - index['metadata']['format_start'] - 1)
        return {'trim': f'trim=start_pts={start_pts}:end_pts={end_pts}', 'seek': seek}

    def _work(self, job_id, spec):
        target = None
        try:
            job = self.job(job_id)
            source = self.source(job['source_id'])
            self.update_job(job_id, status='running', progress=5)
            if job['kind'] == 'preview':
                target = self.cache / f"{source['id']}-preview-v1.mp4"
                if not target.exists():
                    temporary = target.with_suffix('.tmp.mp4')
                    meta = self.metadata(source['id'])
                    filters = ['setpts=PTS-STARTPTS', *meta['filters'], "scale=w='min(1280,iw)':h='min(1280,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2", 'setsar=1']
                    self.update_job(job_id, message='轉換瀏覽器預覽影片…')
                    try:
                        self._run(['ffmpeg', '-v', 'error', '-nostdin', '-y', '-noautorotate', '-i', str(source['path']),
                             '-map', f"0:{meta['stream']}", '-vf', ','.join(filters), '-an', '-c:v', 'libx264',
                             '-preset', 'veryfast', '-crf', '25', '-pix_fmt', 'yuv420p',
                             '-fps_mode', 'vfr', '-movflags', '+faststart', str(temporary)], timeout=7200)
                        self.source(source['id'])
                        temporary.replace(target)
                    finally:
                        temporary.unlink(missing_ok=True)
                self.update_job(job_id, path=str(target), status='ready', progress=100, message='預覽影片已就緒。')
                return
            target = self.output / f"{source['path'].stem}-{spec['first']}-{spec['last']}-{job_id[:8]}.webm"
            temporary = self.cache / f'{job_id}.webm'
            try:
                self.encode(source['path'], temporary, spec, lambda **kw: self.update_job(job_id, **kw))
                self.source(source['id'])
                details = self.validate(temporary, spec)
                temporary.replace(target)
                self.update_job(job_id, path=str(target), filename=target.name, status='ready', progress=100,
                                message='貼圖完成，可以預覽與下載。', **details)
            finally:
                temporary.unlink(missing_ok=True)
        except Exception as exc:
            self.update_job(job_id, status='failed', message=str(exc))

    def encode(self, source_path, target, spec, progress):
        width, height = spec['dimensions']
        filters = [spec['trim'],
                   f"setpts=(PTS-STARTPTS)/{spec['speed']:.12g}", *spec['metadata']['filters']]
        if spec['mode'] == 'square':
            filters.append(f"crop={spec['side']}:{spec['side']}:{spec['x']}:{spec['y']}:exact=1")
        filters += [f'scale={width}:{height}:flags=lanczos', 'setsar=1',
                    f"tpad=stop_mode=clone:stop_duration={spec['duration']:.12g}", f'fps={OUTPUT_FPS}']
        base = ['ffmpeg', '-v', 'error', '-nostdin', '-y', '-noautorotate', '-copyts',
                '-ss', str(spec['seek']), '-i', str(source_path),
                '-map', f"0:{spec['metadata']['stream']}", '-vf', ','.join(filters), '-an', '-sn', '-dn',
                '-map_metadata', '-1', '-frames:v', str(spec['frames']), '-c:v', 'libvpx-vp9',
                '-pix_fmt', 'yuv420p', '-deadline', 'good', '-cpu-used', '4', '-row-mt', '1']
        for attempt, crf in enumerate([30, 36, 42, 48, 54, 60, 63]):
            progress(message=f'編碼貼圖，壓縮品質 CRF {crf}…', progress=10 + attempt * 10)
            self._run([*base, '-b:v', '0', '-crf', str(crf), str(target)], timeout=7200)
            if target.stat().st_size <= MAX_BYTES:
                return
        # Finite fallback: two-pass bitrate encoding with a container-overhead margin.
        bitrate = max(1000, int((MAX_BYTES - 12_000) * 8 / (spec['frames'] / OUTPUT_FPS) * .85))
        log = self.cache / f'{target.stem}-pass'
        progress(message='進行兩階段壓縮以符合容量限制…', progress=85)
        try:
            self._run([*base, '-b:v', str(bitrate), '-pass', '1', '-passlogfile', str(log), '-f', 'null', os.devnull], timeout=7200)
            self._run([*base, '-b:v', str(bitrate), '-pass', '2', '-passlogfile', str(log), str(target)], timeout=7200)
        finally:
            for path in self.cache.glob(log.name + '*'):
                path.unlink(missing_ok=True)
        if target.stat().st_size > MAX_BYTES:
            raise MediaError('多次壓縮後仍超過 256 KB，請縮短片段後重試。')

    @staticmethod
    def validate(path, spec):
        data = probe(path)
        stream = video_stream(data)
        duration = float(data['format']['duration'])
        size = path.stat().st_size
        width, height = stream['width'], stream['height']
        fps = ratio(stream.get('avg_frame_rate'), ratio(stream.get('r_frame_rate'), float('inf')))
        if (stream['codec_name'] != 'vp9' or 'webm' not in data['format']['format_name']
                or (width, height) != tuple(spec['dimensions']) or max(width, height) != 512
                or duration > 3.000001 or duration <= 0 or fps > 30.001 or size > MAX_BYTES
                or any(s['codec_type'] == 'audio' for s in data['streams'])):
            raise MediaError('成品未通過 Telegram 格式驗證，請調整片段後重試。')
        return {'duration': duration, 'bytes': size, 'width': width, 'height': height, 'fps': fps}

    def close(self):
        self.stopping.set()
        self.index_pool.shutdown(wait=True, cancel_futures=True)
        self.work_pool.shutdown(wait=True, cancel_futures=True)


def check_tools():
    for tool in ('ffmpeg', 'ffprobe'):
        if not shutil.which(tool):
            raise MediaError(f'找不到 {tool}，請先安裝並加入 PATH。')
    encoders = run(['ffmpeg', '-hide_banner', '-encoders'])
    for codec in ('libvpx-vp9', 'libx264'):
        if codec not in encoders:
            raise MediaError(f'ffmpeg 缺少 {codec} 編碼器。')
