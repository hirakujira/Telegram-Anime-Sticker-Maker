import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from app import create_app
from media import Library, MAX_BYTES, MediaError, geometry, probe, run, video_stream


def wait_until(operation, ready, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = operation()
        if result.get('status') == 'failed':
            raise AssertionError(result['message'])
        if ready(result):
            return result
        time.sleep(.03)
    raise AssertionError('工作逾時')


def pixel(path, x, y):
    result = subprocess.run(['ffmpeg', '-v', 'error', '-i', str(path), '-vf',
        f'crop=1:1:{x}:{y}:exact=1,format=rgb24', '-frames:v', '1', '-f', 'rawvideo', '-'],
        capture_output=True, check=True)
    return tuple(result.stdout[:3])


class MediaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix='sticker-tests-')
        cls.root = Path(cls.temporary.name)
        cls.color = cls.root / '左右測試.mp4'
        run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'color=c=red:s=640x360:r=30:d=4',
             '-f', 'lavfi', '-i', 'sine=frequency=440:duration=4', '-vf',
             'drawbox=x=320:y=0:w=320:h=360:color=blue:t=fill', '-c:v', 'libx264',
             '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest', str(cls.color)])
        cls.vfr = cls.root / 'variable.mkv'
        run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'testsrc2=size=160x120:rate=10:duration=1',
             '-vf', r'setpts=if(lt(N\,5)\,N\,5+2*(N-5))',
             '-fps_mode', 'vfr', '-c:v', 'ffv1', str(cls.vfr)])
        cls.rotated = cls.root / 'rotated.mp4'
        run(['ffmpeg', '-v', 'error', '-y', '-display_rotation:v:0', '-90', '-i', str(cls.color),
             '-c', 'copy', str(cls.rotated)])
        cls.app = create_app(cls.root)
        cls.client = cls.app.test_client()
        cls.library = cls.app.extensions['library']
        videos = cls.client.get('/api/videos').get_json()['videos']
        cls.ids = {item['name']: item['id'] for item in videos}
        for source_id in cls.ids.values():
            wait_until(lambda: cls.library.begin_index(source_id), lambda r: r['status'] == 'ready')

    @classmethod
    def tearDownClass(cls):
        cls.library.close()
        cls.temporary.cleanup()

    def source_id(self):
        return self.ids[self.color.name]

    def make(self, **overrides):
        options = {'start': 1, 'end': 30, 'speed': 1, 'mode': 'square', 'x': 280, 'y': 0, **overrides}
        response = self.client.post(f'/api/videos/{self.source_id()}/stickers', json=options)
        self.assertEqual(response.status_code, 202, response.get_json())
        job_id = response.get_json()['id']
        result = wait_until(lambda: self.library.job(job_id), lambda r: r['status'] == 'ready')
        return self.library.job_file(job_id), result, job_id

    def test_index_and_inclusive_endpoints(self):
        index = self.library.index(self.source_id())
        self.assertEqual(index['count'], 120)
        self.assertAlmostEqual(index['times'][0], 0)
        self.assertAlmostEqual(index['times'][29], 29/30, places=5)
        spec = self.library.spec(self.source_id(), {'start': 2, 'end': 4})
        self.assertAlmostEqual(spec['duration'], .1, places=5)
        self.assertEqual(spec['frames'], 3)
        frame = self.library.frame(self.source_id(), 120)
        self.assertTrue(frame.exists())
        self.assertEqual(frame, self.library.frame(self.source_id(), 120))
        with self.assertRaises(MediaError):
            self.library.frame(self.source_id(), 121)

    def test_right_crop_and_original_ratio_and_download(self):
        right, result, job_id = self.make()
        left, _, _ = self.make(x=0)
        self.assertGreater(pixel(right, 256, 256)[2], 200)
        self.assertGreater(pixel(left, 256, 256)[0], 200)
        self.assertEqual((result['width'], result['height']), (512, 512))
        self.assertLessEqual(result['bytes'], MAX_BYTES)
        self.assertAlmostEqual(result['duration'], 1, places=3)
        data = probe(right)
        self.assertEqual(video_stream(data)['codec_name'], 'vp9')
        self.assertFalse(any(s['codec_type'] == 'audio' for s in data['streams']))
        original, details, _ = self.make(mode='original')
        self.assertEqual((details['width'], details['height']), (512, 288))
        response = self.client.get(f'/api/jobs/{job_id}/file?download=1')
        self.assertEqual(response.status_code, 200)
        self.assertIn('attachment', response.headers['Content-Disposition'])
        response.close()

    def test_speed_single_frame_and_three_second_boundary(self):
        _, boundary, _ = self.make(end=90)
        self.assertAlmostEqual(boundary['duration'], 3, places=3)
        _, faster, _ = self.make(end=120, speed=2)
        self.assertAlmostEqual(faster['duration'], 2, places=3)
        _, single, _ = self.make(start=120, end=120, speed=.5)
        self.assertAlmostEqual(single['duration'], 2/30, delta=.001)
        with self.assertRaisesRegex(MediaError, '至少 1.334'):
            self.library.spec(self.source_id(), {'start': 1, 'end': 120})

    def test_invalid_parameters(self):
        for changes in [{'start': 0}, {'end': 121}, {'start': 20, 'end': 10},
                        {'start': 1.5}, {'x': 281}, {'x': -1}, {'y': 1}, {'speed': 0},
                        {'speed': 'nan'}, {'speed': 'inf'}, {'speed': True}, {'mode': 'stretch'}]:
            with self.subTest(changes=changes):
                with self.assertRaises(MediaError):
                    self.library.spec(self.source_id(), {'start': 1, 'end': 30, **changes})

    def test_variable_timestamps_and_native_preview(self):
        source_id = self.ids[self.vfr.name]
        index = self.library.index(source_id)
        self.assertEqual(index['count'], 10)
        self.assertAlmostEqual(index['times'][5]-index['times'][4], .1, places=4)
        self.assertAlmostEqual(index['times'][6]-index['times'][5], .2, places=4)
        spec = self.library.spec(source_id, {'start': 6, 'end': 7, 'speed': 2})
        self.assertAlmostEqual(spec['duration'], .2, places=4)
        job_id = self.library.create_job('preview', source_id)
        self.assertEqual(job_id, self.library.create_job('preview', source_id))
        wait_until(lambda: self.library.job(job_id), lambda r: r['status'] == 'ready')
        data = probe(self.library.job_file(job_id))
        self.assertEqual(video_stream(data)['codec_name'], 'h264')
        self.assertFalse(any(s['codec_type']=='audio' for s in data['streams']))
        spec['duration'] = .2
        sticker_job = self.library.create_job('sticker', source_id, {'start': 6, 'end': 7, 'speed': 2})
        result = wait_until(lambda: self.library.job(sticker_job), lambda r: r['status']=='ready')
        self.assertAlmostEqual(result['duration'], .2, places=3)

    def test_rotated_geometry_and_sar(self):
        source_id = self.ids[self.rotated.name]
        meta = self.library.metadata(source_id)
        self.assertEqual((meta['width'], meta['height']), (360, 640))
        frame = self.library.frame(source_id, 1)
        stream = video_stream(probe(frame))
        self.assertEqual((stream['width'], stream['height']), (360, 640))
        job = self.library.create_job('sticker', source_id, {'start': 1, 'end': 10, 'x': 0, 'y': 280})
        wait_until(lambda: self.library.job(job), lambda r: r['status']=='ready')
        # Clockwise rotation moves the right-hand blue half to the bottom.
        self.assertGreater(pixel(self.library.job_file(job), 256, 256)[2], 200)
        width, height, filters = geometry({'width': 320, 'height': 240, 'sample_aspect_ratio': '2:1'})
        self.assertEqual((width, height), (640, 240))
        self.assertIn('scale=640:240:flags=lanczos', filters)

    def test_stream_range_and_request_errors(self):
        response = self.client.get(f'/api/videos/{self.source_id()}/media', headers={'Range': 'bytes=0-99'})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(len(response.data), 100)
        response.close()
        self.assertEqual(self.client.get('/api/videos/missing/metadata').status_code, 400)
        self.assertEqual(self.client.post(f'/api/videos/{self.source_id()}/stickers', data='{}').status_code, 415)
        self.assertEqual(self.client.post(f'/api/videos/{self.source_id()}/stickers', json={},
                                         headers={'Origin':'https://example.com'}).status_code, 403)
        self.assertEqual(self.client.post(f'/api/videos/{self.source_id()}/stickers', json=[]).status_code, 400)

    def test_fast_seek_matches_source_frames_with_nonzero_timestamps(self):
        for offset in (0, 5):
            path = self.root / f'seek-{offset}.mp4'
            run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'testsrc2=size=160x120:rate=30:duration=4',
                 '-c:v', 'libx264', '-output_ts_offset', str(offset), str(path)])
            source_id = next(s['id'] for s in self.library.scan() if s['name']==path.name)
            index = wait_until(lambda: self.library.begin_index(source_id), lambda r:r['status']=='ready')
            for number in (1, 61, 119):
                frame = self.library.frame(source_id, number)
                raw = subprocess.run(['ffmpeg', '-v', 'error', '-noautorotate', '-i', str(path), '-vf',
                    ','.join([f'trim=start_frame={number-1}:end_frame={number}', *index['metadata']['filters']]),
                    '-frames:v', '1', '-pix_fmt', 'rgb24', '-f', 'rawvideo', '-'], capture_output=True, check=True).stdout
                rendered = subprocess.run(['ffmpeg', '-v', 'error', '-i', str(frame), '-frames:v', '1',
                    '-pix_fmt', 'rgb24', '-f', 'rawvideo', '-'], capture_output=True, check=True).stdout
                self.assertEqual(rendered, raw, f'offset={offset}, frame={number}')
            job = self.library.create_job('sticker',source_id,{'start':91,'end':120})
            result = wait_until(lambda:self.library.job(job),lambda r:r['status']=='ready')
            self.assertAlmostEqual(result['duration'],1,places=3)

    def test_corrupt_source_and_source_changes(self):
        broken = self.root/'broken.mp4'
        broken.write_bytes(b'not a video')
        self.library.scan()
        broken_id = next(s['id'] for s in self.library.sources.values() if s['name']==broken.name)
        with self.assertRaises(MediaError):
            self.library.metadata(broken_id)
        self.library.begin_index(broken_id)
        deadline=time.monotonic()+10
        while self.library.indices[broken_id]['status']=='indexing' and time.monotonic()<deadline:
            time.sleep(.03)
        self.assertEqual(self.library.indices[broken_id]['status'], 'failed')
        broken.write_bytes(b'changed')
        with self.assertRaisesRegex(MediaError, '變更'):
            self.library.source(broken_id)
        broken.unlink()
        self.library.scan()

    def test_compression_retries_are_bounded_and_failed_jobs_have_no_download(self):
        spec = self.library.spec(self.source_id(), {'start':1,'end':30})
        target = self.library.cache/'oversize.webm'
        calls=[]
        def oversized(args, **kwargs):
            calls.append(args)
            if args[-1] != '/dev/null':
                Path(args[-1]).write_bytes(b'x'*(MAX_BYTES+1))
            return ''
        with patch('media.run', side_effect=oversized):
            with self.assertRaisesRegex(MediaError, '超過 256 KB'):
                self.library.encode(self.color,target,spec,lambda **kw:None)
        self.assertEqual(len(calls),9)
        target.unlink()
        with patch.object(self.library,'encode',side_effect=MediaError('測試編碼失敗')):
            job=self.library.create_job('sticker',self.source_id(),{'start':1,'end':3})
            deadline=time.monotonic()+10
            while self.library.job(job)['status'] not in {'failed','ready'} and time.monotonic()<deadline:
                time.sleep(.03)
            self.assertEqual(self.library.job(job)['status'],'failed')
            with self.assertRaises(MediaError):
                self.library.job_file(job)

    def test_running_process_stops_on_shutdown(self):
        event = threading.Event()
        timer = threading.Timer(.1, event.set)
        started = time.monotonic()
        timer.start()
        try:
            with self.assertRaisesRegex(MediaError, '中止'):
                run([sys.executable, '-c', 'import time; time.sleep(30)'], cancel_event=event)
        finally:
            timer.cancel()
        self.assertLess(time.monotonic() - started, 2)


if __name__ == '__main__':
    unittest.main()
