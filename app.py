from __future__ import annotations

import argparse
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, jsonify, render_template, request, send_file

from media import Library, MediaError, check_tools


def create_app(root=None):
    app = Flask(__name__)
    library = Library(root or Path(__file__).parent)
    library.scan()
    app.extensions['library'] = library

    @app.before_request
    def local_requests():
        # JSON writes + same-origin checks prevent other pages from submitting work.
        if request.method == 'POST':
            origin = request.headers.get('Origin')
            if origin and urlsplit(origin).netloc != request.host:
                return jsonify(error='不允許跨來源操作。'), 403
            if not request.is_json:
                return jsonify(error='請使用 JSON 格式。'), 415

    @app.errorhandler(MediaError)
    def invalid_media(error):
        return jsonify(error=str(error)), 400

    @app.errorhandler(404)
    def missing(_):
        return jsonify(error='找不到檔案或頁面。'), 404

    @app.get('/')
    def home():
        return render_template('index.html')

    @app.get('/api/videos')
    def videos():
        return jsonify(videos=library.scan())

    @app.get('/api/videos/<source_id>/metadata')
    def metadata(source_id):
        return jsonify(library.metadata(source_id))

    @app.get('/api/videos/<source_id>/index')
    def index(source_id):
        return jsonify(library.begin_index(source_id))

    @app.get('/api/videos/<source_id>/media')
    def media(source_id):
        return send_file(library.source(source_id)['path'], conditional=True)

    @app.get('/api/videos/<source_id>/frames/<int:number>')
    def frame(source_id, number):
        return send_file(library.frame(source_id, number), mimetype='image/png', conditional=True)

    @app.post('/api/videos/<source_id>/preview')
    def preview(source_id):
        return jsonify(id=library.create_job('preview', source_id)), 202

    @app.post('/api/videos/<source_id>/stickers')
    def sticker(source_id):
        options = request.get_json(silent=True)
        if not isinstance(options, dict):
            raise MediaError('請提供貼圖設定。')
        return jsonify(id=library.create_job('sticker', source_id, options)), 202

    @app.get('/api/jobs/<job_id>')
    def job(job_id):
        return jsonify(library.job(job_id))

    @app.get('/api/jobs/<job_id>/file')
    def job_file(job_id):
        return send_file(library.job_file(job_id), conditional=True,
                         as_attachment=request.args.get('download') == '1')

    return app


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='本機 Telegram 影片貼圖工具')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--directory', type=Path, default=Path(__file__).parent, help='影片所在資料夾')
    args = parser.parse_args()
    check_tools()
    application = create_app(args.directory)
    print(f'開啟 http://127.0.0.1:{args.port} 即可開始製作貼圖。', flush=True)
    try:
        application.run(host='127.0.0.1', port=args.port, threaded=True, debug=False, use_reloader=False)
    finally:
        application.extensions['library'].close()
