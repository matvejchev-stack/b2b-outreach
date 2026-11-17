#!/usr/bin/env python3
"""Read-only checks of a single-recipient outreach package. Never sends mail."""

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from datetime import date
from pathlib import Path


class InvalidPackage(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise InvalidPackage(message)


def text_field(obj, key):
    value = obj.get(key)
    require(isinstance(value, str) and value.strip(), f"{key}: nonempty string required")
    return value


def mailbox(value):
    if not isinstance(value, str) or len(value) > 254 or value.count("@") != 1:
        return False
    local, domain = value.split("@")
    if not local or len(local) > 64 or local.startswith(".") or local.endswith(".") or ".." in local:
        return False
    if not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+", local):
        return False
    try:
        labels = domain.encode("idna").decode("ascii").split(".")
    except UnicodeError:
        return False
    return len(labels) > 1 and all(
        re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
        for label in labels
    )


def file_hash(entry, root):
    require(isinstance(entry, dict), "file entry must be an object")
    relative = text_field(entry, "path")
    path = (root / relative).resolve()
    require(not Path(relative).is_absolute() and path.is_relative_to(root),
            "file path must remain inside package directory")
    require(path.is_file(), "package file is missing")
    expected = text_field(entry, "sha256")
    require(re.fullmatch(r"[a-f0-9]{64}", expected), "invalid sha256")
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    require(actual == expected, "file sha256 mismatch")
    return path


def check(manifest_path, account, send_date=None, expected_digest=None):
    """Return an integrity digest; does not validate permission, quota or content."""
    manifest_path = Path(manifest_path).resolve()
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    require(isinstance(data, dict), "manifest must be an object")
    require(type(data.get("schema_version")) is int and data["schema_version"] == 1,
            "unsupported schema_version")
    text_field(data, "package_id")
    text_field(data, "campaign_id")
    sender = text_field(data, "sender")
    require(mailbox(sender) and mailbox(account), "plain sender/account email required")
    require(sender == account, "sender differs from actual account")
    to = data.get("to")
    require(isinstance(to, list) and len(to) == 1 and mailbox(to[0]),
            "exactly one plain To email required")
    require(data.get("cc", []) == [] and data.get("bcc", []) == [], "Cc/Bcc must be empty")
    text_field(data, "recipient_id")
    require(re.fullmatch(r"[A-Z]{2}", text_field(data, "country")), "two-letter country required")
    language = text_field(data, "language")
    subject = text_field(data, "subject")
    require("\n" not in subject and "\r" not in subject, "subject must be a single line")
    message_kind = text_field(data, "message_kind")
    require(message_kind in {"first_contact", "reply", "follow_up", "referral"}, "unknown message_kind")
    if message_kind in {"reply", "follow_up"}:
        text_field(data, "thread_id")

    root = manifest_path.parent
    body_path = file_hash(data.get("body"), root)
    require(body_path.stat().st_size > 0, "body file is empty")
    try:
        body = body_path.read_text(encoding="utf-8")
    except UnicodeError:
        raise InvalidPackage("body file must be UTF-8 text") from None
    require("\x00" not in body, "body file contains a NUL character")
    attachments = data.get("attachments")
    require(isinstance(attachments, list), "attachments must be an array")
    expected_kinds = data.get("expected_attachment_kinds")
    require(isinstance(expected_kinds, list) and
            all(isinstance(x, str) and x for x in expected_kinds), "expected_attachment_kinds required")
    kinds = []
    paths = set()
    for entry in attachments:
        path = file_hash(entry, root)
        require(path != body_path and path not in paths, "duplicate attachment file")
        paths.add(path)
        require(path.stat().st_size > 0, "attachment file is empty")
        kind = text_field(entry, "kind")
        kinds.append(kind)
        require(entry.get("recipient_id") == data["recipient_id"], "attachment recipient mismatch")
        attachment_language = text_field(entry, "language")
        neutral = entry.get("language_neutral") is True and kind not in {"letter", "presentation"}
        require(attachment_language == language or neutral, "attachment language mismatch")
        if kind == "letter":
            require(send_date is not None, "--send-date required for an official letter")
            require(date.fromisoformat(text_field(entry, "document_date")) == send_date,
                    "letter date differs from send date")
    require(Counter(kinds) == Counter(expected_kinds), "attachment kinds/count differ from plan")

    # All fields participate: changing To, subject, body, thread or file metadata
    # changes the digest even if new per-file hashes are supplied.
    canonical = json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if expected_digest is not None:
        require(re.fullmatch(r"[a-f0-9]{64}", expected_digest), "invalid expected digest")
        require(digest == expected_digest, "package differs from reviewed digest")
    return {
        "status": "integrity_checked",
        "package_sha256": digest,
        "reviewed_digest_matched": expected_digest is not None,
        "attachment_count": len(attachments),
        "send_authorized": "not_checked",
        "content_and_visual_review": "not_checked",
        "history_and_quota": "not_checked",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--account", required=True, help="Actual connected sender account")
    parser.add_argument("--send-date", type=date.fromisoformat, help="Planned local send day, YYYY-MM-DD")
    parser.add_argument("--expected-digest", help="Digest recorded when this exact package was reviewed")
    args = parser.parse_args()
    try:
        result = check(args.manifest, args.account, args.send_date, args.expected_digest)
    except (InvalidPackage, OSError, UnicodeError, ValueError, TypeError, AttributeError) as error:
        # Avoid emitting recipient details or file contents into shared logs.
        reason = str(error) if isinstance(error, InvalidPackage) else "invalid or unreadable input"
        print(json.dumps({"status": "hold", "reason": reason}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
