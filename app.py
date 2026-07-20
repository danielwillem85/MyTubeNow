import os
import shutil
import tempfile
import uuid
from pathlib import Path
from urllib.parse import urlparse

import imageio_ffmpeg
from flask import Flask, flash, redirect, render_template, request, send_file, url_for
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError


app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("FLASK_SECRET_KEY", "dev-secret-change-me")

YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
}


def is_youtube_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False

    return parsed.scheme in {"http", "https"} and parsed.netloc.lower() in YOUTUBE_HOSTS


def get_ffmpeg_location() -> str | None:
    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        return system_ffmpeg

    try:
        return imageio_ffmpeg.get_ffmpeg_exe()
    except RuntimeError:
        return None


def extract_video_info(url: str) -> dict:
    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
    }

    with YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=False)

    return {
        "title": info.get("title") or "Untitled video",
        "duration": info.get("duration"),
        "thumbnail": info.get("thumbnail"),
        "uploader": info.get("uploader"),
        "webpage_url": info.get("webpage_url") or url,
    }


def format_duration(seconds: int | None) -> str:
    if not seconds:
        return "Unknown length"

    minutes, sec = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)

    if hours:
        return f"{hours}:{minutes:02d}:{sec:02d}"

    return f"{minutes}:{sec:02d}"


def download_media(url: str, export_format: str) -> tuple[Path, tempfile.TemporaryDirectory]:
    temp_dir = tempfile.TemporaryDirectory(prefix="tubenow-", ignore_cleanup_errors=True)
    output_template = str(Path(temp_dir.name) / "%(title).180B-%(id)s.%(ext)s")

    common_options = {
        "outtmpl": output_template,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "restrictfilenames": True,
        "ffmpeg_location": get_ffmpeg_location(),
    }

    if export_format == "mp4":
        options = {
            **common_options,
            "format": "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/best",
            "merge_output_format": "mp4",
        }
        expected_extension = ".mp4"
    elif export_format == "mp3":
        options = {
            **common_options,
            "format": "bestaudio/best",
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }
            ],
        }
        expected_extension = ".mp3"
    else:
        temp_dir.cleanup()
        raise ValueError("Unsupported export format")

    with YoutubeDL(options) as ydl:
        ydl.download([url])

    exported_files = sorted(Path(temp_dir.name).glob(f"*{expected_extension}"))
    if not exported_files:
        temp_dir.cleanup()
        raise FileNotFoundError(f"Could not create a {export_format.upper()} file.")

    return exported_files[0], temp_dir


@app.template_filter("duration")
def duration_filter(seconds: int | None) -> str:
    return format_duration(seconds)


@app.route("/", methods=["GET", "POST"])
def index():
    video = None
    submitted_url = ""

    if request.method == "POST":
        submitted_url = request.form.get("url", "").strip()

        if not submitted_url:
            flash("Paste a YouTube video link first.", "error")
        elif not is_youtube_url(submitted_url):
            flash("Use a valid YouTube or youtu.be URL.", "error")
        else:
            try:
                video = extract_video_info(submitted_url)
                submitted_url = video["webpage_url"]
            except DownloadError:
                flash("Could not read that video. Check the link or try another public video.", "error")
            except Exception:
                flash("Something went wrong while fetching the video details.", "error")

    return render_template("index.html", video=video, submitted_url=submitted_url)


@app.route("/download", methods=["GET", "POST"])
def download():
    if request.method == "GET":
        return redirect(url_for("index"))

    url = request.form.get("url", "").strip()
    export_format = request.form.get("format", "").strip().lower()
    download_token = request.form.get("download_token", "").strip()

    if not is_youtube_url(url):
        flash("Use a valid YouTube or youtu.be URL.", "error")
        return redirect(url_for("index"))

    if export_format not in {"mp4", "mp3"}:
        flash("Choose MP4 or MP3 export.", "error")
        return redirect(url_for("index"))

    if not get_ffmpeg_location():
        flash("FFmpeg is required for MP4 merging and MP3 conversion. Install FFmpeg and try again.", "error")
        return redirect(url_for("index"))

    try:
        file_path, temp_dir = download_media(url, export_format)
    except DownloadError:
        flash("The export failed. The video may be unavailable, private, or blocked.", "error")
        return redirect(url_for("index"))
    except Exception:
        flash("The export failed before a file could be created.", "error")
        return redirect(url_for("index"))

    download_name = f"{file_path.stem}-{uuid.uuid4().hex[:8]}{file_path.suffix}"
    response = send_file(file_path, as_attachment=True, download_name=download_name)
    if download_token:
        response.set_cookie("tubenow_download", download_token, max_age=120, samesite="Lax")
    response.call_on_close(temp_dir.cleanup)
    return response


if __name__ == "__main__":
    app.run(debug=True)
