import calendar
import ipaddress
import os
import secrets
import shutil
import sqlite3
import tempfile
import uuid
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

import imageio_ffmpeg
import requests
from flask import Flask, abort, flash, g, redirect, render_template, request, send_file, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError


app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("FLASK_SECRET_KEY", "dev-secret-change-me")
app.config["DATABASE"] = os.environ.get(
    "MYTUBENOW_DATABASE",
    str(Path(app.instance_path) / "mytubenow.sqlite3"),
)
app.config["BREVO_API_KEY"] = os.environ.get("BREVO_API_KEY")
app.config["BREVO_LIST_ID"] = os.environ.get("BREVO_LIST_ID")
app.config["MOLLIE_API_KEY"] = os.environ.get("MOLLIE_API_KEY")
app.config["PUBLIC_URL"] = os.environ.get("MYTUBENOW_PUBLIC_URL")
app.config["YTDLP_COOKIE_FILE"] = os.environ.get("YTDLP_COOKIE_FILE")

MOLLIE_API_URL = "https://api.mollie.com/v2"
FREE_CONVERSIONS_PER_24_HOURS = 1

YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
}


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        db_path = Path(app.config["DATABASE"])
        db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(db_path)
        connection.row_factory = sqlite3.Row
        g.db = connection

    return g.db


def init_db() -> None:
    with app.app_context():
        db = get_db()
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        user_columns = {
            row["name"] for row in db.execute("PRAGMA table_info(users)").fetchall()
        }
        for column_name, definition in {
            "mollie_customer_id": "TEXT",
            "mollie_subscription_id": "TEXT",
            "pro_status": "TEXT NOT NULL DEFAULT 'free'",
            "pro_started_at": "TEXT",
            "pro_access_until": "TEXT",
            "pro_canceled_at": "TEXT",
        }.items():
            if column_name not in user_columns:
                db.execute(f"ALTER TABLE users ADD COLUMN {column_name} {definition}")

        db.execute(
            """
            CREATE TABLE IF NOT EXISTS conversions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                ip_address TEXT NOT NULL,
                export_format TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'complete',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (id)
            )
            """
        )
        conversion_columns = {
            row["name"] for row in db.execute("PRAGMA table_info(conversions)").fetchall()
        }
        if "status" not in conversion_columns:
            db.execute(
                "ALTER TABLE conversions ADD COLUMN status TEXT NOT NULL DEFAULT 'complete'"
            )
        db.execute(
            """
            CREATE INDEX IF NOT EXISTS conversions_ip_created_at
            ON conversions (ip_address, created_at)
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS mollie_payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                mollie_payment_id TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'open',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                processed_at TEXT,
                FOREIGN KEY (user_id) REFERENCES users (id)
            )
            """
        )
        db.commit()


@app.teardown_appcontext
def close_db(error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


@app.before_request
def load_logged_in_user() -> None:
    user_id = session.get("user_id")
    g.user = None

    if user_id is not None:
        get_db().execute(
            """
            UPDATE users
            SET pro_status = 'free', mollie_subscription_id = NULL,
                pro_canceled_at = NULL
            WHERE id = ?
              AND pro_status = 'active'
              AND pro_canceled_at IS NOT NULL
              AND pro_access_until IS NOT NULL
              AND date(pro_access_until) <= date('now')
            """,
            (user_id,),
        )
        get_db().commit()
        g.user = get_db().execute(
            """
            SELECT id, email, pro_status, mollie_customer_id,
                   mollie_subscription_id, pro_access_until, pro_canceled_at
            FROM users WHERE id = ?
            """,
            (user_id,),
        ).fetchone()


def get_request_ip() -> str:
    raw_ip = request.remote_addr or "unknown"
    try:
        return str(ipaddress.ip_address(raw_ip))
    except ValueError:
        return raw_ip[:45]


def reserve_free_conversion(user_id: int, ip_address: str, export_format: str) -> int | None:
    db = get_db()
    try:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "DELETE FROM conversions WHERE created_at < datetime('now', '-24 hours')"
        )
        row = db.execute(
            """
            SELECT COUNT(*) AS total
            FROM conversions
            WHERE ip_address = ? AND created_at >= datetime('now', '-24 hours')
            """,
            (ip_address,),
        ).fetchone()
        if int(row["total"]) >= FREE_CONVERSIONS_PER_24_HOURS:
            db.rollback()
            return None

        cursor = db.execute(
            """
            INSERT INTO conversions (user_id, ip_address, export_format, status)
            VALUES (?, ?, ?, 'pending')
            """,
            (user_id, ip_address, export_format),
        )
        db.commit()
        return int(cursor.lastrowid)
    except Exception:
        db.rollback()
        raise


def complete_conversion(
    user_id: int,
    ip_address: str,
    export_format: str,
    reservation_id: int | None,
) -> None:
    get_db().execute(
        "DELETE FROM conversions WHERE created_at < datetime('now', '-24 hours')"
    )
    if reservation_id is None:
        get_db().execute(
            """
            INSERT INTO conversions (user_id, ip_address, export_format, status)
            VALUES (?, ?, ?, 'complete')
            """,
            (user_id, ip_address, export_format),
        )
    else:
        get_db().execute(
            "UPDATE conversions SET status = 'complete' WHERE id = ?",
            (reservation_id,),
        )
    get_db().commit()


def release_conversion(reservation_id: int | None) -> None:
    if reservation_id is not None:
        get_db().execute("DELETE FROM conversions WHERE id = ?", (reservation_id,))
        get_db().commit()


def mollie_request(
    method: str,
    path: str,
    *,
    payload: dict | None = None,
    idempotency_key: str | None = None,
) -> dict:
    api_key = app.config.get("MOLLIE_API_KEY")
    if not api_key:
        raise RuntimeError("MOLLIE_API_KEY is not configured.")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key

    response = requests.request(
        method,
        f"{MOLLIE_API_URL}{path}",
        headers=headers,
        json=payload,
        timeout=15,
    )
    response.raise_for_status()
    return response.json()


def public_url(endpoint: str) -> str:
    base_url = (app.config.get("PUBLIC_URL") or "").rstrip("/")
    if not base_url:
        raise RuntimeError("MYTUBENOW_PUBLIC_URL is not configured.")
    return f"{base_url}{url_for(endpoint)}"


def home_canonical_url() -> str:
    base_url = (app.config.get("PUBLIC_URL") or request.url_root).rstrip("/")
    return f"{base_url}/"


def next_month(day: date) -> date:
    year = day.year + (1 if day.month == 12 else 0)
    month = 1 if day.month == 12 else day.month + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def paid_through_date(payment: dict) -> str:
    paid_at = payment.get("paidAt") or ""
    try:
        paid_on = date.fromisoformat(paid_at[:10])
    except ValueError:
        paid_on = date.today()
    return next_month(paid_on).isoformat()


def get_csrf_token() -> str:
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]


app.jinja_env.globals["csrf_token"] = get_csrf_token


def require_valid_csrf_token() -> None:
    submitted_token = request.form.get("csrf_token", "")
    expected_token = session.get("csrf_token", "")
    if not expected_token or not secrets.compare_digest(submitted_token, expected_token):
        abort(400, description="Invalid form token.")


def ensure_mollie_customer(user: sqlite3.Row) -> str:
    if user["mollie_customer_id"]:
        return user["mollie_customer_id"]

    customer = mollie_request(
        "POST",
        "/customers",
        payload={
            "name": user["email"],
            "email": user["email"],
            "metadata": {"user_id": user["id"]},
        },
        idempotency_key=f"mytubenow-customer-{user['id']}",
    )
    get_db().execute(
        "UPDATE users SET mollie_customer_id = ? WHERE id = ?",
        (customer["id"], user["id"]),
    )
    get_db().commit()
    return customer["id"]


def activate_pro_from_payment(payment: dict) -> None:
    payment_id = payment.get("id")
    payment_row = get_db().execute(
        "SELECT user_id, processed_at FROM mollie_payments WHERE mollie_payment_id = ?",
        (payment_id,),
    ).fetchone()
    if payment_row is None:
        return

    get_db().execute(
        "UPDATE mollie_payments SET status = ? WHERE mollie_payment_id = ?",
        (payment.get("status", "unknown"), payment_id),
    )
    get_db().commit()

    if payment.get("status") != "paid" or payment_row["processed_at"]:
        return

    user = get_db().execute(
        """
        SELECT id, mollie_customer_id, mollie_subscription_id
        FROM users WHERE id = ?
        """,
        (payment_row["user_id"],),
    ).fetchone()
    if user is None:
        return

    subscription_id = user["mollie_subscription_id"]
    if not subscription_id:
        subscription = mollie_request(
            "POST",
            f"/customers/{user['mollie_customer_id']}/subscriptions",
            payload={
                "amount": {"currency": "EUR", "value": "4.99"},
                "interval": "1 month",
                "startDate": next_month(date.today()).isoformat(),
                "description": "MyTubeNow Pro membership",
                "webhookUrl": public_url("mollie_webhook"),
                "metadata": {"user_id": user["id"], "kind": "pro_recurring"},
            },
            idempotency_key=f"mytubenow-subscription-{payment_id}",
        )
        subscription_id = subscription["id"]

    get_db().execute(
        """
        UPDATE users
        SET pro_status = 'active', mollie_subscription_id = ?,
            pro_started_at = COALESCE(pro_started_at, CURRENT_TIMESTAMP),
            pro_access_until = ?, pro_canceled_at = NULL
        WHERE id = ?
        """,
        (subscription_id, paid_through_date(payment), user["id"]),
    )
    get_db().execute(
        """
        UPDATE mollie_payments SET processed_at = CURRENT_TIMESTAMP
        WHERE mollie_payment_id = ?
        """,
        (payment_id,),
    )
    get_db().commit()


def process_mollie_payment(payment: dict) -> None:
    payment_id = payment.get("id")
    if not payment_id:
        return

    initial_payment = get_db().execute(
        "SELECT user_id FROM mollie_payments WHERE mollie_payment_id = ?",
        (payment_id,),
    ).fetchone()
    if initial_payment is not None:
        if payment.get("status") == "paid":
            activate_pro_from_payment(payment)
        else:
            payment_status = payment.get("status", "unknown")
            get_db().execute(
                "UPDATE mollie_payments SET status = ? WHERE mollie_payment_id = ?",
                (payment_status, payment_id),
            )
            if payment_status in {"failed", "canceled", "expired"}:
                get_db().execute(
                    """
                    UPDATE users SET pro_status = 'free'
                    WHERE id = ? AND pro_status = 'pending'
                    """,
                    (initial_payment["user_id"],),
                )
            get_db().commit()
        return

    subscription_id = payment.get("subscriptionId")
    if not subscription_id:
        return

    user = get_db().execute(
        "SELECT id FROM users WHERE mollie_subscription_id = ?",
        (subscription_id,),
    ).fetchone()
    if user is None:
        return

    if payment.get("status") == "paid":
        new_status = "active"
    elif payment.get("status") in {"failed", "canceled", "expired", "charged_back"}:
        new_status = "free"
    else:
        return

    if new_status == "active":
        get_db().execute(
            """
            UPDATE users
            SET pro_status = 'active', pro_access_until = ?
            WHERE id = ?
            """,
            (paid_through_date(payment), user["id"]),
        )
    else:
        get_db().execute(
            "UPDATE users SET pro_status = ? WHERE id = ?",
            (new_status, user["id"]),
        )
    get_db().commit()


def user_has_active_pro(user_id: int) -> bool:
    row = get_db().execute(
        "SELECT pro_status FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    return row is not None and row["pro_status"] == "active"


def create_user(email: str, password: str, password_confirmation: str) -> tuple[bool, str]:
    email = email.strip().lower()

    if "@" not in email or "." not in email.rsplit("@", 1)[-1]:
        return False, "Enter a valid email address."

    if len(password) < 8:
        return False, "Password must be at least 8 characters."

    if password != password_confirmation:
        return False, "Passwords do not match."

    try:
        get_db().execute(
            "INSERT INTO users (username, email, password_hash) VALUES (?, ?, ?)",
            (email, email, generate_password_hash(password)),
        )
        get_db().commit()
    except sqlite3.IntegrityError:
        return False, "That email is already registered."

    return True, "Account created. You can export videos now."


def add_brevo_contact(email: str) -> None:
    api_key = app.config.get("BREVO_API_KEY")
    raw_list_id = app.config.get("BREVO_LIST_ID")

    if not api_key or not raw_list_id:
        raise RuntimeError("Brevo API settings are missing.")

    try:
        list_id = int(raw_list_id)
    except (TypeError, ValueError) as error:
        raise RuntimeError("BREVO_LIST_ID must be a number.") from error

    response = requests.post(
        "https://api.brevo.com/v3/contacts",
        headers={
            "api-key": api_key,
            "accept": "application/json",
        },
        json={
            "email": email.strip().lower(),
            "listIds": [list_id],
            "updateEnabled": True,
        },
        timeout=10,
    )
    response.raise_for_status()


def is_youtube_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False

    return parsed.scheme in {"http", "https"} and parsed.netloc.lower() in YOUTUBE_HOSTS


def get_yt_dlp_auth_options() -> dict:
    configured_path = (app.config.get("YTDLP_COOKIE_FILE") or "").strip()
    if not configured_path:
        return {}

    cookie_path = Path(configured_path).expanduser()
    if not cookie_path.is_file():
        raise RuntimeError("YTDLP_COOKIE_FILE does not point to a readable file.")
    if not os.access(cookie_path, os.R_OK):
        raise RuntimeError("YTDLP_COOKIE_FILE is not readable by the application user.")

    return {"cookiefile": str(cookie_path)}


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
        "compat_opts": {"no-certifi"},
        **get_yt_dlp_auth_options(),
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
    temp_dir = tempfile.TemporaryDirectory(prefix="mytubenow-", ignore_cleanup_errors=True)
    output_template = str(Path(temp_dir.name) / "%(title).180B-%(id)s.%(ext)s")

    common_options = {
        "outtmpl": output_template,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "restrictfilenames": True,
        "ffmpeg_location": get_ffmpeg_location(),
        "compat_opts": {"no-certifi"},
        **get_yt_dlp_auth_options(),
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
    auth_modal = request.args.get("auth") if g.user is None else None
    pro_modal = (
        request.args.get("pro") == "1"
        and g.user is not None
        and g.user["pro_status"] == "free"
    )
    auth_form = session.pop("auth_form", {})
    submitted_url = session.pop("pending_fetch_url", "") or auth_form.get("pending_url", "")

    if request.method == "POST":
        submitted_url = request.form.get("url", "").strip()

        if g.user is None:
            auth_modal = "login"
            flash("Log in or create an account to fetch this video.", "error")
        elif not submitted_url:
            flash("Paste a YouTube video link first.", "error")
        elif not is_youtube_url(submitted_url):
            flash("Use a valid YouTube or youtu.be URL.", "error")
        else:
            try:
                video = extract_video_info(submitted_url)
                submitted_url = video["webpage_url"]
            except DownloadError:
                app.logger.exception("yt-dlp could not extract video metadata.")
                flash("Could not read that video. Check the link or try another public video.", "error")
            except Exception:
                app.logger.exception("Could not fetch video metadata.")
                flash("Something went wrong while fetching the video details.", "error")

    elif submitted_url and g.user is not None:
        if not is_youtube_url(submitted_url):
            flash("Use a valid YouTube or youtu.be URL.", "error")
        else:
            try:
                video = extract_video_info(submitted_url)
                submitted_url = video["webpage_url"]
            except DownloadError:
                app.logger.exception("yt-dlp could not extract video metadata.")
                flash("Could not read that video. Check the link or try another public video.", "error")
            except Exception:
                app.logger.exception("Could not fetch video metadata.")
                flash("Something went wrong while fetching the video details.", "error")

    return render_template(
        "index.html",
        video=video,
        submitted_url=submitted_url,
        auth_modal=auth_modal if auth_modal in {"login", "signup"} else None,
        pro_modal=pro_modal,
        auth_form=auth_form,
        canonical_url=home_canonical_url(),
    )


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if g.user is not None:
        return redirect(url_for("index"))

    if request.method == "GET":
        return redirect(url_for("index", auth="signup"))

    form = {
        "email": request.form.get("email", "").strip(),
        "marketing_opt_in": request.form.get("marketing_opt_in") == "yes",
    }
    pending_url = request.form.get("pending_url", "").strip()

    password = request.form.get("password", "")
    password_confirmation = request.form.get("password_confirmation", "")
    ok, message = create_user(form["email"], password, password_confirmation)

    if ok:
        subscription_failed = False
        if form["marketing_opt_in"]:
            try:
                add_brevo_contact(form["email"])
            except (requests.RequestException, RuntimeError):
                app.logger.exception("Could not add an opted-in user to the Brevo contact list.")
                subscription_failed = True

        user = get_db().execute(
            "SELECT id FROM users WHERE email = ?",
            (form["email"].lower(),),
        ).fetchone()
        session.clear()
        session["user_id"] = user["id"]
        if pending_url:
            session["pending_fetch_url"] = pending_url
        flash(message, "success")
        if subscription_failed:
            flash(
                "Your account was created, but we could not add you to product updates. Please try again later.",
                "error",
            )
        return redirect(url_for("index"))

    session["auth_form"] = {**form, "pending_url": pending_url}
    flash(message, "error")
    return redirect(url_for("index", auth="signup"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if g.user is not None:
        return redirect(url_for("index"))

    if request.method == "GET":
        return redirect(url_for("index", auth="login"))

    form = {"email": request.form.get("email", "").strip()}
    pending_url = request.form.get("pending_url", "").strip()

    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    user = get_db().execute(
        "SELECT id, password_hash FROM users WHERE email = ?",
        (email,),
    ).fetchone()

    if user is None or not check_password_hash(user["password_hash"], password):
        session["auth_form"] = {**form, "pending_url": pending_url}
        flash("Email or password is incorrect.", "error")
        return redirect(url_for("index", auth="login"))

    session.clear()
    session["user_id"] = user["id"]
    if pending_url:
        session["pending_fetch_url"] = pending_url
    return redirect(url_for("index"))


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("You are logged out.", "success")
    return redirect(url_for("index"))


@app.route("/settings")
def settings():
    if g.user is None:
        flash("Log in to manage your account settings.", "error")
        return redirect(url_for("index", auth="login"))
    return render_template("settings.html")


@app.route("/settings/pro/cancel", methods=["POST"])
def cancel_pro_subscription():
    if g.user is None:
        return redirect(url_for("index", auth="login"))
    require_valid_csrf_token()

    if g.user["pro_status"] != "active":
        flash("There is no active Pro subscription to cancel.", "error")
        return redirect(url_for("settings"))
    if g.user["pro_canceled_at"]:
        flash("Your Pro subscription is already canceled.", "success")
        return redirect(url_for("settings"))
    if not g.user["mollie_customer_id"] or not g.user["mollie_subscription_id"]:
        flash("We could not find the Mollie subscription. Please contact support.", "error")
        return redirect(url_for("settings"))

    subscription_path = (
        f"/customers/{g.user['mollie_customer_id']}"
        f"/subscriptions/{g.user['mollie_subscription_id']}"
    )
    access_until = g.user["pro_access_until"]

    try:
        if not access_until:
            subscription = mollie_request("GET", subscription_path)
            access_until = subscription.get("nextPaymentDate")
        mollie_request("DELETE", subscription_path)
    except requests.HTTPError as error:
        if error.response is None or error.response.status_code != 404:
            app.logger.exception("Could not cancel Mollie subscription.")
            flash("We could not cancel your subscription right now. Please try again.", "error")
            return redirect(url_for("settings"))
    except (requests.RequestException, RuntimeError, KeyError):
        app.logger.exception("Could not cancel Mollie subscription.")
        flash("We could not cancel your subscription right now. Please try again.", "error")
        return redirect(url_for("settings"))

    if not access_until:
        access_until = next_month(date.today()).isoformat()
    get_db().execute(
        """
        UPDATE users
        SET pro_canceled_at = CURRENT_TIMESTAMP, pro_access_until = ?
        WHERE id = ?
        """,
        (access_until, g.user["id"]),
    )
    get_db().commit()
    flash(
        f"Your Pro subscription is canceled. Unlimited access remains available through {access_until}.",
        "success",
    )
    return redirect(url_for("settings"))


@app.route("/pro/checkout", methods=["POST"])
def pro_checkout():
    if g.user is None:
        flash("Log in before signing up for Pro.", "error")
        return redirect(url_for("index", auth="login"))

    if g.user["pro_status"] == "active":
        flash("Your Pro membership is already active.", "success")
        return redirect(url_for("index"))
    if g.user["pro_status"] == "pending":
        flash("Your Pro payment is still being confirmed.", "success")
        return redirect(url_for("index"))

    try:
        redirect_url = public_url("pro_return")
        webhook_url = public_url("mollie_webhook")
        customer_id = ensure_mollie_customer(g.user)
        payment = mollie_request(
            "POST",
            "/payments",
            payload={
                "amount": {"currency": "EUR", "value": "4.99"},
                "customerId": customer_id,
                "sequenceType": "first",
                "description": "MyTubeNow Pro - first month",
                "redirectUrl": redirect_url,
                "webhookUrl": webhook_url,
                "metadata": {"user_id": g.user["id"], "kind": "pro_initial"},
            },
        )
        checkout_url = payment.get("_links", {}).get("checkout", {}).get("href")
        if not checkout_url:
            raise RuntimeError("Mollie did not return a checkout URL.")

        get_db().execute(
            """
            INSERT OR IGNORE INTO mollie_payments
                (user_id, mollie_payment_id, status)
            VALUES (?, ?, ?)
            """,
            (g.user["id"], payment["id"], payment.get("status", "open")),
        )
        get_db().execute(
            "UPDATE users SET pro_status = 'pending' WHERE id = ?",
            (g.user["id"],),
        )
        get_db().commit()
        session["mollie_payment_id"] = payment["id"]
        return redirect(checkout_url, code=303)
    except (requests.RequestException, RuntimeError, KeyError):
        app.logger.exception("Could not start Mollie Pro checkout.")
        flash("Pro checkout is unavailable right now. Please try again later.", "error")
        return redirect(url_for("index", pro="1"))


@app.route("/pro/return")
def pro_return():
    if g.user is None:
        return redirect(url_for("index", auth="login"))

    if g.user["pro_status"] == "active":
        session.pop("mollie_payment_id", None)
        flash("Your Pro membership is active.", "success")
        return redirect(url_for("index"))

    payment_id = session.get("mollie_payment_id")
    if not payment_id:
        flash("We could not find your Pro payment.", "error")
        return redirect(url_for("index", pro="1"))

    try:
        payment = mollie_request("GET", f"/payments/{payment_id}")
        process_mollie_payment(payment)
    except (requests.RequestException, RuntimeError, KeyError):
        app.logger.exception("Could not confirm Mollie Pro payment.")
        if user_has_active_pro(g.user["id"]):
            session.pop("mollie_payment_id", None)
            flash("Welcome to MyTubeNow Pro. Unlimited conversions are active.", "success")
            return redirect(url_for("index"))
        flash("Your payment is still being confirmed. Please refresh shortly.", "error")
        return redirect(url_for("index"))

    status = payment.get("status")
    if status == "paid":
        if user_has_active_pro(g.user["id"]):
            session.pop("mollie_payment_id", None)
            flash("Welcome to MyTubeNow Pro. Unlimited conversions are active.", "success")
        else:
            flash("Payment received. Your Pro membership is being activated.", "success")
        return redirect(url_for("index"))
    if status in {"open", "pending", "authorized"}:
        flash("Your payment is still being confirmed.", "error")
        return redirect(url_for("index"))
    else:
        flash("The Pro payment was not completed.", "error")
    return redirect(url_for("index", pro="1"))


@app.route("/mollie/webhook", methods=["POST"])
def mollie_webhook():
    payment_id = request.form.get("id", "").strip()
    if not payment_id:
        return "", 200

    try:
        payment = mollie_request("GET", f"/payments/{payment_id}")
        process_mollie_payment(payment)
    except (requests.RequestException, RuntimeError, KeyError):
        app.logger.exception("Could not process Mollie webhook for %s.", payment_id)
        return "", 500
    return "", 200


@app.route("/download", methods=["GET", "POST"])
def download():
    if g.user is None:
        flash("Log in or create an account to export this video.", "error")
        return redirect(url_for("index", auth="login"))

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

    ip_address = get_request_ip()
    reservation_id = None
    if g.user["pro_status"] != "active":
        reservation_id = reserve_free_conversion(g.user["id"], ip_address, export_format)
        if reservation_id is None:
            session["pending_fetch_url"] = url
            flash("Sign up to 'pro' for more conversions", "error")
            return redirect(url_for("index", pro="1"))

    try:
        file_path, temp_dir = download_media(url, export_format)
    except DownloadError:
        release_conversion(reservation_id)
        app.logger.exception("yt-dlp could not export the requested video.")
        flash("The export failed. The video may be unavailable, private, or blocked.", "error")
        return redirect(url_for("index"))
    except Exception:
        release_conversion(reservation_id)
        app.logger.exception("The video export failed before a file was created.")
        flash("The export failed before a file could be created.", "error")
        return redirect(url_for("index"))

    complete_conversion(g.user["id"], ip_address, export_format, reservation_id)

    download_name = f"{file_path.stem}-{uuid.uuid4().hex[:8]}{file_path.suffix}"
    response = send_file(file_path, as_attachment=True, download_name=download_name)
    if download_token:
        response.set_cookie("mytubenow_download", download_token, max_age=120, samesite="Lax")
    response.call_on_close(temp_dir.cleanup)
    return response


init_db()


if __name__ == "__main__":
    app.run(debug=True)
