# TubeNow

A small Flask app that accepts a YouTube video URL, fetches video metadata, and exports MP4 or MP3 files with `yt-dlp`.

Only download content you own, have permission to download, or are otherwise legally allowed to save.

## Requirements

- Python 3.11+
- FFmpeg available on your `PATH`, or the bundled `imageio-ffmpeg` dependency installed from `requirements.txt`

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

Open `http://127.0.0.1:5000` in your browser.

## Notes

- MP4 exports may need FFmpeg to merge video and audio streams.
- MP3 exports require FFmpeg for audio conversion.
- The app first checks your system `PATH`, then falls back to the FFmpeg binary bundled by `imageio-ffmpeg`.
- YouTube may block private, age-restricted, region-restricted, or unavailable videos.
