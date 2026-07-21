# MyTubeNow

A small Flask app with account creation and login that accepts a YouTube video URL, fetches video metadata, and exports MP4 or MP3 files with `yt-dlp`.

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

User accounts are stored in `instance/mytubenow.sqlite3` by default. Set `MYTUBENOW_DATABASE` to point at a different SQLite file.

To add users who select the product-updates checkbox to a Brevo contact list, set these variables before starting the app:

```powershell
$env:BREVO_API_KEY="your-api-key"
$env:BREVO_LIST_ID="your-numeric-list-id"
```

The app uses Brevo's direct contact endpoint and does not send a double-opt-in confirmation email.

## YouTube authentication

If YouTube requires authentication for the server's IP address, provide a Netscape-format cookie file through `YTDLP_COOKIE_FILE`:

```powershell
$env:YTDLP_COOKIE_FILE="C:\secure\youtube-cookies.txt"
```

On a systemd deployment, use an absolute path such as `/home/ubuntu/MyTubeNow/secrets/youtube-cookies.txt`. The application uses the cookie file for both video previews and conversions. Keep it outside source control, restrict it to the service user, and treat it as an account credential.

## Pro subscriptions

Free accounts can complete one conversion per IP address in a rolling 24-hour period. Successful conversions are recorded in SQLite and records older than 24 hours are automatically removed. Pro accounts have unlimited conversions for €4.99 per month.

Set a Mollie API key and the public HTTPS URL of the app before starting it:

```powershell
$env:MOLLIE_API_KEY="test_your-mollie-api-key"
$env:MYTUBENOW_PUBLIC_URL="https://your-public-host.example"
```

Mollie must be able to reach `MYTUBENOW_PUBLIC_URL/mollie/webhook`, so a localhost URL is not sufficient for checkout testing. Use a secure tunnel or a deployed test environment and a Mollie test API key. The first €4.99 payment establishes the recurring mandate and covers the first month; the recurring monthly subscription begins one month later.

Signed-in users can manage their membership from `/settings`. Canceling calls Mollie's subscription cancellation endpoint, stops future renewals, and preserves unlimited conversions through the recorded paid-through date. The cancellation form requires an explicit confirmation and a session-bound CSRF token.

The app reads the client IP from Flask's `request.remote_addr`. If it is deployed behind a reverse proxy, configure the trusted proxy to pass the real client address and apply Flask/Werkzeug proxy handling only for proxies you control.

## Notes

- MP4 exports may need FFmpeg to merge video and audio streams.
- MP3 exports require FFmpeg for audio conversion.
- The app first checks your system `PATH`, then falls back to the FFmpeg binary bundled by `imageio-ffmpeg`.
- YouTube may block private, age-restricted, region-restricted, or unavailable videos.
