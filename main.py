
import os
import re
import tempfile
from pathlib import Path

import yt_dlp
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

app = FastAPI(title="YouTubeDownloader Server")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://youtubedownloader-web.isikeren318.workers.dev"
    ],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


class DownloadRequest(BaseModel):
    url: str
    format: str = "mp4"
    quality: str = "720"


def validate_url(url: str):
    if not re.fullmatch(
        r"https?://(www\.)?(youtube\.com|youtu\.be)/.+",
        url,
        flags=re.IGNORECASE,
    ):
        raise HTTPException(
            status_code=400,
            detail="Geçerli bir YouTube bağlantısı gir.",
        )


@app.get("/")
def home():
    return {
        "status": "online",
        "message": "YouTubeDownloader sunucusu çalışıyor!"
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/download")
def download(request: DownloadRequest):
    validate_url(request.url)

    fmt = request.format.lower()

    if fmt not in ("mp4", "mp3"):
        raise HTTPException(
            status_code=400,
            detail="Şimdilik MP4 ve MP3 destekleniyor.",
        )

    temp_dir = tempfile.mkdtemp(prefix="ytd_")
    output_template = str(Path(temp_dir) / "%(title).150s.%(ext)s")

    options = {
        "outtmpl": output_template,
        "noplaylist": True,
        "restrictfilenames": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 20,
        "retries": 2,
        "max_filesize": 200 * 1024 * 1024,
    }

    if fmt == "mp3":
        options["format"] = "bestaudio/best"
        options["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ]
    else:
        try:
            height = int(request.quality)
        except ValueError:
            height = 720

        height = max(144, min(height, 1080))

        options["format"] = (
            f"bestvideo[height<={height}]+bestaudio/"
            f"best[height<={height}]/best"
        )
        options["merge_output_format"] = "mp4"

    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            ydl.download([request.url])

        extension = ".mp3" if fmt == "mp3" else ".mp4"

        files = list(Path(temp_dir).glob(f"*{extension}"))

        if not files:
            raise RuntimeError("Çıktı dosyası oluşturulamadı.")

        output_file = files[0]

        import shutil

        return FileResponse(
            path=str(output_file),
            filename=output_file.name,
            media_type=(
                "audio/mpeg"
                if fmt == "mp3"
                else "video/mp4"
            ),
            background=BackgroundTask(
                shutil.rmtree,
                temp_dir,
                ignore_errors=True,
            ),
        )

    except Exception as error:
        import shutil
        shutil.rmtree(temp_dir, ignore_errors=True)

        raise HTTPException(
            status_code=502,
            detail=f"Video indirilemedi: {str(error)[:250]}",
        )
