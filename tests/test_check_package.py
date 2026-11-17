import copy
import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "skills/b2b-outreach/scripts/check_package.py"
SPEC = importlib.util.spec_from_file_location("check_package", SCRIPT)
CHECKER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECKER)


class PackageChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "manifest.json"
        self.account = "sender@example.invalid"
        self.day = date(2026, 11, 17)
        self.data = {
            "schema_version": 1, "campaign_id": "fictional", "package_id": "p-1",
            "sender": self.account, "to": ["buyer@example.invalid"], "cc": [], "bcc": [],
            "recipient_id": "person-1", "country": "RU", "language": "ru",
            "message_kind": "first_contact", "subject": "Обсуждение производственной задачи",
            "body": self.make_file("email.txt", "Добрый день!\n".encode()),
            "expected_attachment_kinds": ["letter", "presentation"],
            "attachments": [],
        }
        for kind in self.data["expected_attachment_kinds"]:
            entry = self.make_file(kind + ".pdf", ("synthetic fixture " + kind).encode())
            entry.update(kind=kind, recipient_id="person-1", language="ru")
            if kind == "letter":
                entry["document_date"] = self.day.isoformat()
            self.data["attachments"].append(entry)

    def make_file(self, name, content):
        (self.root / name).write_bytes(content)
        return {"path": name, "sha256": hashlib.sha256(content).hexdigest()}

    def run_check(self, data=None, **kwargs):
        self.path.write_text(json.dumps(self.data if data is None else data), encoding="utf-8")
        return CHECKER.check(self.path, kwargs.pop("account", self.account),
                             kwargs.pop("send_date", self.day), **kwargs)

    def assert_hold(self, data=None, **kwargs):
        with self.assertRaises(CHECKER.InvalidPackage):
            self.run_check(data, **kwargs)

    def test_good_package_does_not_claim_authorization(self):
        result = self.run_check()
        self.assertEqual(result["status"], "integrity_checked")
        self.assertEqual(result["send_authorized"], "not_checked")
        self.assertFalse(result["reviewed_digest_matched"])

    def test_frozen_digest_matches(self):
        digest = self.run_check()["package_sha256"]
        self.assertTrue(self.run_check(expected_digest=digest)["reviewed_digest_matched"])

    def test_same_filename_changed_bytes_rejected(self):
        (self.root / "letter.pdf").write_bytes(b"replacement")
        self.assert_hold()

    def test_body_changed_rejected(self):
        (self.root / "email.txt").write_bytes(b"new promise")
        self.assert_hold()

    def test_invalid_utf8_and_nul_body_rejected(self):
        for content in [b"\xff\x00\x00", b"hello\x00world"]:
            with self.subTest(content=content):
                self.data["body"] = self.make_file("email.txt", content)
                self.assert_hold()

    def test_malformed_mailboxes_rejected(self):
        for value in ["buyer@example..invalid", "buyer@-invalid.example", "buy\x00er@example.invalid",
                      ".buyer@example.invalid", "buyer..name@example.invalid", "buyer@example.invalid\n"]:
            with self.subTest(value=value):
                self.data["to"] = [value]
                self.assert_hold()

    def test_plus_address_and_idn_domain_supported(self):
        self.data["to"] = ["buyer+project@example.invalid"]
        self.run_check()
        self.data["to"] = ["buyer@пример.рф"]
        self.run_check()

    def test_changed_subject_recipient_thread_or_rehashed_file_breaks_digest(self):
        digest = self.run_check()["package_sha256"]
        for key, value in [("subject", "Other subject"), ("to", ["other@example.invalid"]),
                           ("thread_id", "different-thread")]:
            with self.subTest(key=key):
                data = copy.deepcopy(self.data)
                data[key] = value
                self.assert_hold(data, expected_digest=digest)
        self.data["body"] = self.make_file("email.txt", b"changed and rehashed")
        self.assert_hold(expected_digest=digest)

    def test_wrong_account_rejected(self):
        self.assert_hold(account="other@example.invalid")

    def test_multiple_recipients_or_hidden_recipients_rejected(self):
        for key, value in [("to", ["a@example.invalid", "b@example.invalid"]),
                           ("to", ["a@example.invalid,b@example.invalid"]),
                           ("cc", ["cc@example.invalid"]), ("bcc", ["bcc@example.invalid"])]:
            with self.subTest(key=key):
                data = copy.deepcopy(self.data)
                data[key] = value
                self.assert_hold(data)

    def test_wrong_date_or_missing_send_day_rejected(self):
        self.assert_hold(send_date=date(2026, 11, 18))
        self.assert_hold(send_date=None)

    def test_wrong_language_or_recipient_rejected(self):
        for key, value in [("language", "en"), ("recipient_id", "person-2")]:
            with self.subTest(key=key):
                data = copy.deepcopy(self.data)
                data["attachments"][0][key] = value
                self.assert_hold(data)

    def test_missing_duplicate_or_additional_attachment_rejected(self):
        for entries in [self.data["attachments"][:1],
                        self.data["attachments"] * 2,
                        [self.data["attachments"][0]] * 2]:
            with self.subTest(count=len(entries)):
                data = copy.deepcopy(self.data)
                data["attachments"] = entries
                self.assert_hold(data)

    def test_reply_requires_thread(self):
        self.data["message_kind"] = "reply"
        self.assert_hold()
        self.data["thread_id"] = "synthetic-thread"
        self.run_check()

    def test_path_escape_and_symlink_escape_rejected(self):
        self.data["body"]["path"] = "../outside.txt"
        self.assert_hold()
        (self.root / "outside-link").symlink_to(self.root.parent)
        self.data["body"]["path"] = "outside-link/outside.txt"
        self.assert_hold()

    def test_package_without_attachments(self):
        self.data["attachments"] = []
        self.data["expected_attachment_kinds"] = []
        self.assertEqual(self.run_check(send_date=None)["attachment_count"], 0)

    def test_cli_returns_hold_on_malformed_input(self):
        for value in [[], {"schema_version": True}, self.data | {"attachments": [None]}]:
            with self.subTest(value_type=type(value).__name__):
                self.path.write_text(json.dumps(value), encoding="utf-8")
                result = subprocess.run([sys.executable, str(SCRIPT), str(self.path),
                                         "--account", self.account, "--send-date", self.day.isoformat()],
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(json.loads(result.stdout)["status"], "hold")
                self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
