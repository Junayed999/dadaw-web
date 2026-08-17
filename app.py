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
import requests

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
    provider = get_provider(url)
    return provider.extract_info(url, format_str, audio_only=audio_only, analyze_only=False)

class YTDLPProvider:
    @staticmethod
    def extract_info(url, format_str=None, audio_only=False, analyze_only=False):
        opts = {
            'quiet': True,
            'no_warnings': True,
            'skip_download': True,
            'extractor_args': {
                'youtube': ['player_client=ios,tv,web_embedded']
            }
        }
        
        if analyze_only:
            opts['extract_flat'] = False
        else:
            opts['format'] = format_str
            if not audio_only:
                opts['merge_output_format'] = 'mp4'
                
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

def extract_youtube_video_id(url):
    match = re.search(r'(?:v=|\/)([0-9A-Za-z_-]{11}).*', url)
    return match.group(1) if match else None

class ExternalYouTubeProvider:
    DEFAULT_PIPED_INSTANCES = [
        "https://pipedapi.kavin.rocks",
        "https://pipedapi.tokhmi.xyz",
        "https://pipedapi.moomoo.me",
        "https://pipedapi.syncpundit.io",
        "https://api-piped.mha.fi",
        "https://piped-api.garudalinux.org",
        "https://pipedapi.rivo.lol",
        "https://pipedapi.leptons.xyz",
        "https://piped-api.lunar.icu",
        "https://ytapi.dc09.ru",
        "https://pipedapi.colinslegacy.com",
        "https://yapi.vyper.me",
        "https://api.looleh.xyz",
        "https://piped-api.cfe.re",
        "https://pipedapi.r4fo.com",
        "https://pipedapi-libre.kavin.rocks",
        "https://piped-api.privacy.com.de",
        "https://pipedapi.adminforge.de",
        "https://api.piped.yt"
    ]

    @classmethod
    def get_configured_instances(cls):
        instances = []
        env_instances = os.environ.get('PIPED_API_INSTANCES')
        single_env = os.environ.get('EXTERNAL_PROVIDER_URL')
        
        if env_instances:
            for item in env_instances.split(','):
                item = item.strip()
                if item and item not in instances:
                    instances.append(item)
                    
        if single_env:
            single_env = single_env.strip()
            if single_env and single_env not in instances:
                instances.insert(0, single_env)
                
        for inst in cls.DEFAULT_PIPED_INSTANCES:
            if inst not in instances:
                instances.append(inst)
                
        return instances

    @staticmethod
    def extract_info(url, format_str=None, audio_only=False, analyze_only=False):
        video_id = extract_youtube_video_id(url)
        if not video_id:
            raise RuntimeError("Could not extract YouTube video ID from the provided URL.")
        
        piped_instances = ExternalYouTubeProvider.get_configured_instances()
            
        data = None
        failure_summary = {}

        for instance in piped_instances:
            api_endpoint = f"{instance.rstrip('/')}/streams/{video_id}"
            category = "Unknown Error"
            try:
                response = requests.get(
                    api_endpoint,
                    headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'},
                    timeout=6
                )
                
                if response.status_code != 200:
                    category = f"HTTP {response.status_code}"
                    app.logger.warning(f"Piped instance {instance} returned {category}")
                    failure_summary[category] = failure_summary.get(category, 0) + 1
                    continue

                try:
                    json_data = response.json()
                except Exception as json_err:
                    category = "Invalid JSON Response"
                    app.logger.warning(f"Piped instance {instance} returned invalid JSON: {json_err}")
                    failure_summary[category] = failure_summary.get(category, 0) + 1
                    continue

                if not isinstance(json_data, dict):
                    category = "Non-dict JSON Payload"
                    app.logger.warning(f"Piped instance {instance} payload is not a dict")
                    failure_summary[category] = failure_summary.get(category, 0) + 1
                    continue

                if 'error' in json_data:
                    err_msg = json_data.get('message', json_data['error'])
                    category = f"API Error ({err_msg})"
                    app.logger.warning(f"Piped instance {instance} reported API error: {err_msg}")
                    failure_summary[category] = failure_summary.get(category, 0) + 1
                    continue

                video_streams = json_data.get('videoStreams', [])
                audio_streams = json_data.get('audioStreams', [])
                if not video_streams and not audio_streams:
                    category = "No Stream Data"
                    app.logger.warning(f"Piped instance {instance} contained no audio or video streams")
                    failure_summary[category] = failure_summary.get(category, 0) + 1
                    continue

                data = json_data
                app.logger.info(f"Successfully fetched streams from Piped instance: {instance}")
                break

            except requests.exceptions.Timeout:
                category = "Timeout"
                app.logger.warning(f"Piped instance {instance} timed out")
                failure_summary[category] = failure_summary.get(category, 0) + 1
            except requests.exceptions.SSLError:
                category = "SSL Error"
                app.logger.warning(f"Piped instance {instance} encountered SSL Error")
                failure_summary[category] = failure_summary.get(category, 0) + 1
            except requests.exceptions.ConnectionError:
                category = "Connection Error"
                app.logger.warning(f"Piped instance {instance} connection failed")
                failure_summary[category] = failure_summary.get(category, 0) + 1
            except Exception as e:
                category = f"Unexpected ({type(e).__name__})"
                app.logger.warning(f"Piped instance {instance} failed with: {str(e)}")
                failure_summary[category] = failure_summary.get(category, 0) + 1
                
        if not data:
            summary_str = ", ".join([f"{cat}: {count}" for cat, count in failure_summary.items()])
            raise RuntimeError(f"All configured Piped instances failed ({summary_str}). YouTube extraction is currently unavailable.")

        info = {
            'title': data.get('title', 'Unknown Title'),
            'duration': data.get('duration', 0),
            'uploader': data.get('uploader', 'Unknown Uploader'),
            'thumbnail': data.get('thumbnailUrl', ''),
            'extractor_key': 'youtube',
            'formats': []
        }
        
        video_streams = data.get('videoStreams', [])
        audio_streams = data.get('audioStreams', [])
        
        for audio in audio_streams:
            info['formats'].append({
                'format_id': 'audio_' + str(audio.get('bitrate', 0)),
                'url': audio.get('url'),
                'ext': 'mp3' if 'mp3' in str(audio.get('codec')).lower() else 'm4a',
                'vcodec': 'none',
                'acodec': audio.get('codec'),
                'http_headers': {'User-Agent': 'Mozilla/5.0'} 
            })
            
        for video in video_streams:
            height = 0
            quality_str = video.get('quality', '')
            if 'p' in quality_str:
                try:
                    height = int(quality_str.replace('p', ''))
                except ValueError:
                    pass
                    
            info['formats'].append({
                'format_id': 'video_' + str(video.get('bitrate', 0)),
                'url': video.get('url'),
                'ext': 'mp4' if 'mp4' in str(video.get('mimeType')).lower() else 'webm',
                'vcodec': video.get('codec'),
                'acodec': 'none' if video.get('videoOnly') else 'unknown',
                'height': height,
                'http_headers': {'User-Agent': 'Mozilla/5.0'}
            })
            
        if analyze_only:
            return info
            
        if audio_only:
            best_audio = max(audio_streams, key=lambda x: x.get('bitrate', 0)) if audio_streams else None
            if not best_audio:
                raise RuntimeError("No audio streams found via Piped.")
            info['requested_formats'] = [{
                'url': best_audio.get('url'),
                'manifest_stream_number': 0,
                'http_headers': {'User-Agent': 'Mozilla/5.0'}
            }]
            info['ext'] = 'mp3' if 'mp3' in str(best_audio.get('codec')).lower() else 'm4a'
            return info
            
        target_height = float('inf')
        if format_str and 'height<=' in format_str:
            match = re.search(r'height<=(\d+)', format_str)
            if match:
                target_height = int(match.group(1))
                
        valid_videos = [v for v in video_streams if v.get('videoOnly') == True]
        if not valid_videos:
            valid_videos = video_streams
            
        valid_videos.sort(key=lambda x: x.get('bitrate', 0), reverse=True)
        
        best_video = None
        for v in valid_videos:
            quality_str = v.get('quality', '')
            height = 0
            if 'p' in quality_str:
                try:
                    height = int(quality_str.replace('p', ''))
                except: pass
            if height <= target_height:
                best_video = v
                break
                
        if not best_video and valid_videos:
            best_video = valid_videos[-1] 
            
        best_audio = max(audio_streams, key=lambda x: x.get('bitrate', 0)) if audio_streams else None
        
        if best_video and best_audio and best_video.get('videoOnly'):
            info['requested_formats'] = [
                {
                    'url': best_video.get('url'),
                    'manifest_stream_number': 0,
                    'http_headers': {'User-Agent': 'Mozilla/5.0'}
                },
                {
                    'url': best_audio.get('url'),
                    'manifest_stream_number': 1,
                    'http_headers': {'User-Agent': 'Mozilla/5.0'}
                }
            ]
            info['ext'] = 'mp4'
        elif best_video:
            info['requested_formats'] = [{
                'url': best_video.get('url'),
                'manifest_stream_number': 0,
                'http_headers': {'User-Agent': 'Mozilla/5.0'}
            }]
            info['ext'] = 'mp4'
        else:
            raise RuntimeError("No suitable video stream found via Piped.")
            
        return info

def get_provider(url):
    youtube_domains = ['youtube.com', 'youtu.be', 'm.youtube.com', 'music.youtube.com']
    if any(domain in url.lower() for domain in youtube_domains):
        return ExternalYouTubeProvider()
    return YTDLPProvider()




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

    try:
        provider = get_provider(url)
        info = provider.extract_info(url, analyze_only=True)

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
    except RuntimeError as e:
        return jsonify({'error': str(e)}), 400
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
