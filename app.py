import os
import sys
import re
import mimetypes
import subprocess
import shutil

from flask import Flask, request, jsonify, render_template, Response, stream_with_context
import yt_dlp
from yt_dlp.utils import DownloadError
from yt_dlp.postprocessor.ffmpeg import FFmpegPostProcessor

app = Flask(__name__)

STREAM_CHUNK_SIZE = 65536


def _parse_quality(quality):
    audio_only = False
    if quality == 'Audio Only':
        format_str = 'bestaudio/best'
        audio_only = True
    elif quality == 'Best Available':
        format_str = 'bestvideo+bestaudio/best'
    else:
        try:
            height = int(str(quality).replace('p', ''))
            format_str = f'bestvideo[height<={height}]+bestaudio/best[height<={height}]/best'
        except ValueError:
            format_str = 'bestvideo+bestaudio/best'
    return format_str, audio_only


def _sanitize_filename(title, ext):
    raw_filename = f"{title or 'video'}.{ext}"
    clean_filename = raw_filename.encode('ascii', 'ignore').decode('ascii')
    clean_filename = "".join(c for c in clean_filename if c.isalnum() or c in " .-_()")
    if not clean_filename.strip():
        clean_filename = f"video.{ext}"
    return clean_filename


def _needs_merge(info):
    requested = info.get('requested_formats')
    return bool(requested and len(requested) > 1)


def _estimate_content_length(info, audio_only=False):
    if audio_only:
        return None
    if _needs_merge(info):
        return None
    size = info.get('filesize') or info.get('filesize_approx')
    if size:
        return size
    requested = info.get('requested_formats') or []
    if len(requested) == 1:
        fmt = requested[0]
        return fmt.get('filesize') or fmt.get('filesize_approx')
    return None


def _output_ext(info, audio_only=False):
    if audio_only:
        return 'mp3'
    return info.get('ext') or 'mp4'


def _mime_type(ext, audio_only=False):
    if audio_only:
        return 'audio/mpeg'
    guessed = mimetypes.guess_type(f"video.{ext}")[0]
    return guessed or 'application/octet-stream'


def _extract_metadata(url, format_str, audio_only=False):
    opts = {
        'quiet': True,
        'no_warnings': True,
        'skip_download': True,
        'format': format_str,
    }
    if not audio_only:
        opts['merge_output_format'] = 'mp4'
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)




def _selected_format(info):
    requested = info.get('requested_formats')
    if requested:
        return requested[0]
    return info


def _append_input_args(cmd, fmt, info):
    url = fmt.get('url', '')
    headers = fmt.get('http_headers') or info.get('http_headers') or {}
    if headers and url.startswith(('http://', 'https://')):
        header_str = ''.join(f'{key}: {val}\r\n' for key, val in headers.items())
        cmd.extend(['-headers', header_str])
    cmd.extend(['-i', url])


def _build_ffmpeg_audio_cmd(info):
    ffpp = FFmpegPostProcessor()
    if not ffpp.available:
        raise RuntimeError('FFmpeg is required for audio extraction.')

    fmt = _selected_format(info)
    cmd = [ffpp.executable, '-hide_banner', '-loglevel', 'error', '-y']
    _append_input_args(cmd, fmt, info)
    cmd.extend(['-vn', '-c:a', 'libmp3lame', '-b:a', '192k', '-f', 'mp3', 'pipe:1'])
    return cmd


def _build_ffmpeg_merge_cmd(info):
    ffpp = FFmpegPostProcessor()
    if not ffpp.available:
        raise RuntimeError('FFmpeg is required to merge separate video and audio streams.')

    formats = info['requested_formats']
    cmd = [ffpp.executable, '-hide_banner', '-loglevel', 'error', '-y']

    for fmt in formats:
        _append_input_args(cmd, fmt, info)

    for index, fmt in enumerate(formats):
        stream_number = fmt.get('manifest_stream_number', 0)
        cmd.extend(['-map', f'{index}:{stream_number}'])

    ext = info.get('ext', 'mp4')
    cmd.extend(['-c', 'copy'])
    if ext == 'mp4':
        cmd.extend(['-movflags', 'frag_keyframe+empty_moov+default_base_moof'])
    cmd.extend(['-f', ext, 'pipe:1'])
    return cmd


def _terminate_process(proc):
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _stream_process_output(proc):
    try:
        while True:
            chunk = proc.stdout.read(STREAM_CHUNK_SIZE)
            if not chunk:
                break
            yield chunk
    except GeneratorExit:
        pass
    finally:
        if proc.stdout:
            proc.stdout.close()
        _terminate_process(proc)


def _attachment_response(info, url, format_str, audio_only=False):
    ext = _output_ext(info, audio_only)
    filename = _sanitize_filename(info.get('title'), ext)
    content_length = _estimate_content_length(info, audio_only)
    mime = _mime_type(ext, audio_only)

    headers = {
        'Content-Disposition': f'attachment; filename="{filename}"',
        'Cache-Control': 'no-store',
    }
    if content_length:
        headers['Content-Length'] = str(content_length)

    if audio_only:
        cmd = _build_ffmpeg_audio_cmd(info)
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        return Response(
            stream_with_context(_stream_process_output(proc)),
            mimetype=mime,
            headers=headers,
            direct_passthrough=False,
        )
    elif _needs_merge(info):
        cmd = _build_ffmpeg_merge_cmd(info)
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        return Response(
            stream_with_context(_stream_process_output(proc)),
            mimetype=mime,
            headers=headers,
            direct_passthrough=False,
        )
    else:
        # PROGRESSIVE FORMAT - stream directly
        import urllib.request
        direct_url = info.get('url')
        if not direct_url:
            raise RuntimeError('Could not resolve direct media URL.')
            
        http_headers = info.get('http_headers') or {}
        req = urllib.request.Request(direct_url, headers=http_headers)

        def _stream_direct():
            try:
                with urllib.request.urlopen(req, timeout=10) as response:
                    while True:
                        chunk = response.read(STREAM_CHUNK_SIZE)
                        if not chunk:
                            break
                        yield chunk
            except Exception:
                pass

        return Response(
            stream_with_context(_stream_direct()),
            mimetype=mime,
            headers=headers,
            direct_passthrough=False,
        )


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/analyze', methods=['POST'])
def analyze():
    data = request.json
    url = data.get('url')

    if not url:
        return jsonify({'error': 'URL is required'}), 400

    opts = {
        'quiet': True,
        'skip_download': True,
        'no_warnings': True,
        'extract_flat': False
    }

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)

            title = info.get('title', 'Unknown Title')
            thumbnail = info.get('thumbnail')
            duration = info.get('duration')
            uploader = info.get('uploader')
            extractor = info.get('extractor_key')

            resolutions = set()
            for f in info.get('formats', []):
                h = f.get('height')
                if h and h >= 360:
                    resolutions.add(h)

            sorted_res = sorted(list(resolutions), reverse=True)
            if not sorted_res:
                sorted_res = ['Best Available']

            return jsonify({
                'title': title,
                'thumbnail': thumbnail,
                'duration': duration,
                'uploader': uploader,
                'platform': extractor,
                'resolutions': sorted_res
            })

    except DownloadError as e:
        return jsonify({'error': f'Failed to process URL: {str(e)}'}), 400
    except Exception:
        return jsonify({'error': 'An unexpected error occurred.'}), 500


@app.route('/api/download', methods=['GET'])
def download_file():
    url = request.args.get('url')
    quality = str(request.args.get('quality', 'best'))

    if not url:
        return "URL is required", 400

    format_str, audio_only = _parse_quality(quality)

    try:
        info = _extract_metadata(url, format_str, audio_only)
        return _attachment_response(info, url, format_str, audio_only)
    except DownloadError as e:
        return f"Failed to process URL: {str(e)}", 400
    except RuntimeError as e:
        return str(e), 500
    except Exception as e:
        return f"Error: {str(e)}", 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=True)
