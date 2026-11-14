
import os
import re
import shutil
import tempfile
from pathlib import Path
from threading import BoundedSemaphore

import yt_dlp
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask


app = FastAPI(title="YouTubeDownloader Server", version="2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://youtubedownloader-web.isikeren318.workers.dev"
    ],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
    expose_headers=["Content-Disposition", "Content-Length"],
)

VIDEO_FORMATS = {
    "mp4", "mkv", "webm", "mov", "avi",
    "flv", "wmv", "m4v", "3gp", "ts",
    "mpg", "mpeg", "ogv", "gif"
}

AUDIO_FORMATS = {
    "mp3", "wav", "flac", "ogg", "opus",
    "m4a", "aac", "wma", "aiff", "alac",
    "ac3", "amr", "mp2"
}

EXTENSIONS = {
    "alac": "m4a",
}

MIME_TYPES = {
    "mp4": "video/mp4",
    "mkv": "video/x-matroska",
    "webm": "video/webm",
    "mov": "video/quicktime",
    "avi": "video/x-msvideo",
    "gif": "image/gif",
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "flac": "audio/flac",
    "ogg": "audio/ogg",
    "opus": "audio/ogg",
    "m4a": "audio/mp4",
    "aac": "audio/aac",
}

# Ücretsiz sunucuyu aşırı yüklememek için
download_slots = BoundedSemaphore(value=1)

# Bunlar ücretsiz sunucu için güvenlik/kaynak sınırlarıdır.
MAX_SOURCE_SIZE = 150 * 1024 * 1024
MAX_DURATION = 15 * 60
MAX_OUTPUT_SIZE = 300 * 1024 * 1024


class DownloadRequest(BaseModel):
    url: str = Field(min_length=1, max_length=500)
    format: str = "mp4"
    quality: str = "720p"


def validate_url(url):
    pattern = (
        r"^https?://"
        r"(?:(?:www|m|music)\.)?"
        r"(?:youtube\.com|youtube-nocookie\.com|youtu\.be)"
        r"(?:/|$)"
    )

    if not re.match(pattern, url, re.IGNORECASE):
        raise HTTPException(
            status_code=400,
            detail="Geçerli bir YouTube bağlantısı gir."
        )


def parse_quality(value, audio=False):
    if audio:
        number = re.search(r"\d+", value)
        bitrate = int(number.group()) if number else 192
        return max(64, min(bitrate, 320))

    number = re.search(r"\d+", value)

    if not number:
        return 1080

    height = int(number.group())
    return max(144, min(height, 2160))


def run_ffmpeg(args):
    import subprocess

    result = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
        + args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=180,
        check=False,
    )

    if result.returncode != 0:
        error = result.stderr.decode("utf-8", errors="replace")
        raise RuntimeError(
            "FFmpeg dönüştürme hatası: " + error[-500:]
        )


def video_options(fmt, height):
    scale = f"scale=-2:min({height}\\,ih)"

    if fmt in {"mp4", "mov", "m4v", "mkv"}:
        return [
            "-vf", scale,
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "24",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "128k",
        ]

    if fmt == "webm":
        return [
            "-vf", scale,
            "-c:v", "libvpx",
            "-deadline", "realtime",
            "-cpu-used", "5",
            "-b:v", "1M",
            "-c:a", "libvorbis",
        ]

    if fmt == "avi":
        return [
            "-vf", scale,
            "-c:v", "mpeg4",
            "-q:v", "5",
            "-c:a", "libmp3lame",
        ]

    if fmt in {"mpg", "mpeg", "ts"}:
        return [
            "-vf", scale,
            "-c:v", "mpeg2video",
            "-c:a", "mp2",
        ]

    if fmt == "flv":
        return [
            "-vf", scale,
            "-c:v", "flv",
            "-c:a", "libmp3lame",
        ]

    if fmt == "wmv":
        return [
            "-vf", scale,
            "-c:v", "wmv2",
            "-c:a", "wmav2",
        ]

    if fmt == "ogv":
        return [
            "-vf", scale,
            "-c:v", "libtheora",
            "-c:a", "libvorbis",
        ]

    if fmt == "3gp":
        return [
            "-vf", "scale=352:288",
            "-r", "15",
            "-c:v", "mpeg4",
            "-c:a", "aac",
            "-ar", "22050",
            "-ac", "1",
        ]

    if fmt == "gif":
        return [
            "-an",
            "-vf", "fps=10,scale=480:-1:flags=lanczos",
            "-loop", "0",
        ]

    raise RuntimeError("Video formatı desteklenmiyor.")


def audio_options(fmt, bitrate):
    rate = f"{bitrate}k"

    codecs = {
        "mp3": ["-c:a", "libmp3lame", "-b:a", rate],
        "wav": ["-c:a", "pcm_s16le"],
        "flac": ["-c:a", "flac"],
        "ogg": ["-c:a", "libvorbis", "-b:a", rate],
        "opus": ["-c:a", "libopus", "-b:a", rate],
        "m4a": ["-c:a", "aac", "-b:a", rate],
        "aac": ["-c:a", "aac", "-b:a", rate],
        "wma": ["-c:a", "wmav2", "-b:a", rate],
        "aiff": ["-c:a", "pcm_s16be"],
        "alac": ["-c:a", "alac"],
        "ac3": ["-c:a", "ac3", "-b:a", rate],
        "amr": [
            "-c:a", "libopencore_amrnb",
            "-ar", "8000",
            "-ac", "1",
        ],
        "mp2": ["-c:a", "mp2", "-b:a", rate],
    }

    if fmt not in codecs:
        raise RuntimeError("Ses formatı desteklenmiyor.")

    return ["-vn"] + codecs[fmt]


def cleanup(path):
    shutil.rmtree(path, ignore_errors=True)
    download_slots.release()


@app.get("/")
def home():
    return {
        "status": "online",
        "message": "YouTubeDownloader sunucusu çalışıyor!",
        "version": "2.0",
        "video_formats": sorted(VIDEO_FORMATS),
        "audio_formats": sorted(AUDIO_FORMATS),
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/download")
def download(request: DownloadRequest):
    validate_url(request.url)

    fmt = request.format.lower().strip()

    if fmt not in VIDEO_FORMATS | AUDIO_FORMATS:
        raise HTTPException(
            status_code=400,
            detail="İstenen dosya formatı desteklenmiyor."
        )

    if not download_slots.acquire(blocking=False):
        raise HTTPException(
            status_code=503,
            detail="Sunucu şu anda başka bir indirme yapıyor."
        )

    temp_dir = tempfile.mkdtemp(prefix="ytd_")

    try:
        temp_path = Path(temp_dir)
        source_template = str(temp_path / "source.%(ext)s")

        is_audio = fmt in AUDIO_FORMATS

        quality = parse_quality(
            request.quality,
            audio=is_audio
        )

        options = {
            "outtmpl": source_template,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "socket_timeout": 25,
            "retries": 2,
            "max_filesize": MAX_SOURCE_SIZE,
            "restrictfilenames": True,
            "format": (
                "bestaudio/best"
                if is_audio
                else (
                    f"bestvideo[height<={quality}]+bestaudio/"
                    f"best[height<={quality}]/best"
                )
            ),
            "merge_output_format": "mp4",
        }

        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(
                request.url,
                download=False
            )

            if not info:
                raise RuntimeError("Video bilgileri alınamadı.")

            duration = info.get("duration")

            if duration and duration > MAX_DURATION:
                raise RuntimeError(
                    "Ücretsiz sunucuda en fazla 15 dakikalık "
                    "videolar destekleniyor."
                )

            ydl.download([request.url])

        sources = [
            p for p in temp_path.iterdir()
            if p.is_file() and p.name.startswith("source.")
        ]

        if not sources:
            raise RuntimeError(
                "İndirilen kaynak dosya bulunamadı."
            )

        source = max(
            sources,
            key=lambda p: p.stat().st_size
        )

        if source.stat().st_size > MAX_SOURCE_SIZE:
            raise RuntimeError("Kaynak dosya çok büyük.")

        extension = EXTENSIONS.get(fmt, fmt)
        output = temp_path / f"converted.{extension}"

        if is_audio:
            args = [
                "-i", str(source),
                *audio_options(fmt, quality),
                str(output)
            ]
        else:
            args = [
                "-i", str(source),
                *video_options(fmt, quality),
                str(output)
            ]

        run_ffmpeg(args)

        if not output.exists() or output.stat().st_size == 0:
            raise RuntimeError("Çıktı dosyası oluşturulamadı.")

        if output.stat().st_size > MAX_OUTPUT_SIZE:
            raise RuntimeError(
                "Dönüştürülen dosya sunucu sınırını aşıyor."
            )

        title = info.get("title") or "video"
        safe_title = re.sub(
            r"[^A-Za-z0-9_-]+", "_", title
        ).strip("_")[:70] or "video"

        return FileResponse(
            path=str(output),
            filename=f"{safe_title}.{extension}",
            media_type=MIME_TYPES.get(
                fmt, "application/octet-stream"
            ),
            background=BackgroundTask(cleanup, temp_dir),
        )

    except HTTPException:
        cleanup(temp_dir)
        raise

    except Exception as error:
        cleanup(temp_dir)

        raise HTTPException(
            status_code=502,
            detail=f"Video indirilemedi: {str(error)[:500]}"
        )
