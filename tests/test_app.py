import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import app as app_module


class MyTubeNowTestCase(unittest.TestCase):
    def setUp(self):
        self.database_dir = tempfile.TemporaryDirectory()
        app_module.app.config.update(
            TESTING=True,
            DATABASE=str(Path(self.database_dir.name) / "test.sqlite3"),
            MOLLIE_API_KEY="test_key",
            PUBLIC_URL="https://example.test",
        )
        app_module.init_db()
        self.client = app_module.app.test_client()

        with app_module.app.app_context():
            db = app_module.get_db()
            cursor = db.execute(
                """
                INSERT INTO users (username, email, password_hash)
                VALUES ('viewer@example.com', 'viewer@example.com', 'unused')
                """
            )
            self.user_id = cursor.lastrowid
            db.commit()

        with self.client.session_transaction() as session:
            session["user_id"] = self.user_id

    def tearDown(self):
        self.database_dir.cleanup()

    @staticmethod
    def fake_download(url, export_format):
        output_dir = Path(tempfile.mkdtemp())
        output_path = output_dir / f"video.{export_format}"
        output_path.write_bytes(b"converted")
        cleanup = SimpleNamespace(cleanup=lambda: shutil.rmtree(output_dir, ignore_errors=True))
        return output_path, cleanup

    @staticmethod
    def fake_video(url):
        return {
            "title": "Test video",
            "duration": 60,
            "thumbnail": None,
            "uploader": "Test",
            "webpage_url": url,
        }

    def test_second_conversion_from_same_ip_is_blocked(self):
        form = {"url": "https://youtu.be/test", "format": "mp3"}
        with (
            patch.object(app_module, "get_ffmpeg_location", return_value="ffmpeg"),
            patch.object(app_module, "download_media", side_effect=self.fake_download),
        ):
            first = self.client.post(
                "/download", data=form, environ_base={"REMOTE_ADDR": "203.0.113.7"}
            )
        self.assertEqual(first.status_code, 200)
        first.close()

        with patch.object(app_module, "extract_video_info", side_effect=self.fake_video):
            second = self.client.post(
                "/download",
                data=form,
                environ_base={"REMOTE_ADDR": "203.0.113.7"},
                follow_redirects=True,
            )

        self.assertIn(b"Sign up to &#39;pro&#39; for more conversions", second.data)
        self.assertIn(b'id="pro-modal"', second.data)
        self.assertIn(b'data-pro-modal="true"', second.data)
        with app_module.app.app_context():
            count = app_module.get_db().execute(
                "SELECT COUNT(*) AS total FROM conversions"
            ).fetchone()["total"]
        self.assertEqual(count, 1)

    def test_signed_out_header_has_login_button(self):
        signed_out_client = app_module.app.test_client()
        response = signed_out_client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'data-login-launcher', response.data)
        self.assertIn(b'>Log In</button>', response.data)
        self.assertIn(b'id="login-panel"', response.data)

    def test_failed_conversion_does_not_consume_free_allowance(self):
        form = {"url": "https://youtu.be/test", "format": "mp3"}
        with (
            patch.object(app_module, "get_ffmpeg_location", return_value="ffmpeg"),
            patch.object(app_module, "download_media", side_effect=RuntimeError("failed")),
        ):
            response = self.client.post(
                "/download", data=form, environ_base={"REMOTE_ADDR": "203.0.113.8"}
            )
        self.assertEqual(response.status_code, 302)
        with app_module.app.app_context():
            count = app_module.get_db().execute(
                "SELECT COUNT(*) AS total FROM conversions"
            ).fetchone()["total"]
        self.assertEqual(count, 0)

    def test_paid_payment_activates_one_idempotent_subscription(self):
        with app_module.app.app_context():
            db = app_module.get_db()
            db.execute(
                "UPDATE users SET mollie_customer_id = 'cst_test', pro_status = 'pending' WHERE id = ?",
                (self.user_id,),
            )
            db.execute(
                """
                INSERT INTO mollie_payments (user_id, mollie_payment_id, status)
                VALUES (?, 'tr_test', 'open')
                """,
                (self.user_id,),
            )
            db.commit()

        calls = []

        def fake_mollie(method, path, **kwargs):
            calls.append((method, path, kwargs))
            if method == "GET" and path == "/payments/tr_test":
                return {"id": "tr_test", "status": "paid"}
            if method == "POST" and path == "/customers/cst_test/subscriptions":
                return {"id": "sub_test", "status": "active"}
            raise AssertionError(f"Unexpected Mollie call: {method} {path}")

        with patch.object(app_module, "mollie_request", side_effect=fake_mollie):
            first = self.client.post("/mollie/webhook", data={"id": "tr_test"})
            second = self.client.post("/mollie/webhook", data={"id": "tr_test"})

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        subscription_calls = [call for call in calls if call[0] == "POST"]
        self.assertEqual(len(subscription_calls), 1)
        with app_module.app.app_context():
            user = app_module.get_db().execute(
                "SELECT pro_status, mollie_subscription_id FROM users WHERE id = ?",
                (self.user_id,),
            ).fetchone()
        self.assertEqual(user["pro_status"], "active")
        self.assertEqual(user["mollie_subscription_id"], "sub_test")

    def test_pro_account_can_convert_repeatedly_from_same_ip(self):
        with app_module.app.app_context():
            db = app_module.get_db()
            db.execute(
                "UPDATE users SET pro_status = 'active' WHERE id = ?",
                (self.user_id,),
            )
            db.commit()

        form = {"url": "https://youtu.be/test", "format": "mp3"}
        with (
            patch.object(app_module, "get_ffmpeg_location", return_value="ffmpeg"),
            patch.object(app_module, "download_media", side_effect=self.fake_download),
        ):
            first = self.client.post(
                "/download", data=form, environ_base={"REMOTE_ADDR": "203.0.113.9"}
            )
            second = self.client.post(
                "/download", data=form, environ_base={"REMOTE_ADDR": "203.0.113.9"}
            )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        first.close()
        second.close()
        with app_module.app.app_context():
            count = app_module.get_db().execute(
                "SELECT COUNT(*) AS total FROM conversions"
            ).fetchone()["total"]
        self.assertEqual(count, 2)

    def test_checkout_uses_first_recurring_mollie_payment(self):
        calls = []

        def fake_mollie(method, path, **kwargs):
            calls.append((method, path, kwargs))
            if path == "/customers":
                return {"id": "cst_new"}
            if path == "/payments":
                return {
                    "id": "tr_new",
                    "status": "open",
                    "_links": {"checkout": {"href": "https://www.mollie.com/checkout/test"}},
                }
            raise AssertionError(f"Unexpected Mollie call: {method} {path}")

        with patch.object(app_module, "mollie_request", side_effect=fake_mollie):
            response = self.client.post("/pro/checkout")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, "https://www.mollie.com/checkout/test")
        payment_payload = next(call[2]["payload"] for call in calls if call[1] == "/payments")
        self.assertEqual(payment_payload["amount"], {"currency": "EUR", "value": "4.99"})
        self.assertEqual(payment_payload["sequenceType"], "first")
        self.assertEqual(payment_payload["customerId"], "cst_new")
        self.assertEqual(payment_payload["redirectUrl"], "https://example.test/pro/return")
        self.assertEqual(payment_payload["webhookUrl"], "https://example.test/mollie/webhook")
        with app_module.app.app_context():
            user = app_module.get_db().execute(
                "SELECT pro_status, mollie_customer_id FROM users WHERE id = ?",
                (self.user_id,),
            ).fetchone()
        self.assertEqual(user["pro_status"], "pending")
        self.assertEqual(user["mollie_customer_id"], "cst_new")


if __name__ == "__main__":
    unittest.main()
