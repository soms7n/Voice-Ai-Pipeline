"""Minimal control-plane operations for a real Exotel/Pipecat smoke test."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import Settings, get_settings
from .database import transaction


class DomainError(Exception):
    pass


def _id() -> str:
    return str(uuid.uuid4())


def _now() -> str:
    return datetime.now(UTC).isoformat()


def normalize_phone(value: str) -> str:
    digits = "".join(character for character in value if character.isdigit())
    if len(digits) == 10 and digits[0] in "6789":
        return f"+91{digits}"
    if len(digits) == 12 and digits.startswith("91") and digits[2] in "6789":
        return f"+{digits}"
    raise DomainError("phone must be a valid Indian mobile number")


def create_campaign(name: str) -> dict[str, Any]:
    campaign_id = _id()
    with transaction() as connection:
        connection.execute("INSERT INTO campaigns(id, name, status) VALUES (?, ?, ?)", (campaign_id, name.strip(), "draft"))
    return {"id": campaign_id, "name": name.strip()}


def import_contacts(campaign_id: str, contacts: list[dict[str, Any]]) -> dict[str, Any]:
    added, rejected = [], []
    with transaction() as connection:
        if not connection.execute("SELECT 1 FROM campaigns WHERE id = ?", (campaign_id,)).fetchone():
            raise DomainError("campaign not found")
        for index, contact in enumerate(contacts):
            try:
                phone = normalize_phone(str(contact["phone"]))
                name = str(contact["full_name"]).strip()
                if not name:
                    raise DomainError("full_name is required")
                consent = str(contact.get("consent_status", "unknown"))
                if consent not in {"granted", "service_basis", "unknown", "withdrawn"}:
                    raise DomainError("invalid consent_status")
                contact_id = _id()
                connection.execute(
                    """INSERT INTO contacts(id, campaign_id, full_name, phone_e164, language,
                    consent_status, consent_source, opted_out_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (contact_id, campaign_id, name, phone, contact.get("language", "English"), consent,
                     contact.get("consent_source"), _now() if consent == "withdrawn" else None),
                )
                added.append({"row": index, "contact_id": contact_id, "phone": phone})
            except (KeyError, sqlite3.IntegrityError, DomainError) as error:
                rejected.append({"row": index, "reason": str(error)})
    return {"added": added, "rejected": rejected}


def _media_token(attempt_id: str, settings: Settings) -> str:
    if not settings.internal_webhook_token:
        raise DomainError("INTERNAL_WEBHOOK_TOKEN is required for live calls")
    signature = hmac.new(settings.internal_webhook_token.encode(), attempt_id.encode(), hashlib.sha256).hexdigest()
    return f"{attempt_id}.{signature}"


def verify_media_token(token: str, settings: Settings) -> str:
    try:
        attempt_id, supplied_signature = token.rsplit(".", 1)
    except ValueError as error:
        raise DomainError("invalid media token") from error
    expected_signature = _media_token(attempt_id, settings).rsplit(".", 1)[1]
    if not hmac.compare_digest(supplied_signature, expected_signature):
        raise DomainError("invalid media token")
    return attempt_id


def _multipart(fields: list[tuple[str, str]]) -> tuple[bytes, str]:
    boundary = f"----campaigner-{uuid.uuid4().hex}"
    output = []
    for name, value in fields:
        output += [f"--{boundary}\r\n".encode(), f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(), value.encode(), b"\r\n"]
    output.append(f"--{boundary}--\r\n".encode())
    return b"".join(output), boundary


def _dial_exotel(phone: str, attempt_id: str, settings: Settings) -> tuple[str, str]:
    required = {"PUBLIC_BASE_URL": settings.public_base_url, "EXOTEL_API_KEY": settings.exotel_api_key,
                "EXOTEL_API_TOKEN": settings.exotel_api_token, "EXOTEL_ACCOUNT_SID": settings.exotel_account_sid,
                "EXOTEL_CALLER_ID": settings.exotel_caller_id}
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise DomainError(f"live dialing is not configured: {', '.join(missing)}")
    ws_base = settings.public_base_url.replace("https://", "wss://", 1)
    body, boundary = _multipart([
        ("From", phone), ("CallerId", settings.exotel_caller_id or ""),
        ("StreamUrl", f"{ws_base}/media?token={_media_token(attempt_id, settings)}&sample-rate=8000"),
        ("StreamType", "bidirectional"), ("StatusCallback", f"{settings.public_base_url}/webhooks/exotel/status"),
        ("StatusCallbackEvents[]", "ringing"), ("StatusCallbackEvents[]", "answered"),
        ("StatusCallbackEvents[]", "terminal"), ("CustomField", attempt_id),
    ])
    credentials = base64.b64encode(f"{settings.exotel_api_key}:{settings.exotel_api_token}".encode()).decode()
    request = Request(f"{settings.exotel_base_url}/v1/Accounts/{settings.exotel_account_sid}/Calls/connect", data=body,
                      headers={"Authorization": f"Basic {credentials}", "Content-Type": f"multipart/form-data; boundary={boundary}"}, method="POST")
    try:
        print(request)
        with urlopen(request, timeout=20) as response:  # noqa: S310 - administrator-supplied Exotel host
            payload = json.loads(response.read().decode())
    except HTTPError as error:
        details = error.read().decode("utf-8", errors="replace")
        print("Exotel HTTP status:", error.code)
        print("Exotel error response:", details[:1000])
        raise DomainError(f"Exotel rejected the call request ({error.code})") from error
    except (URLError, json.JSONDecodeError) as error:
        raise DomainError("Exotel call request failed") from error
    call = payload.get("Call", {})
    if not call.get("Sid"):
        raise DomainError("Exotel response did not include a call SID")
    return str(call["Sid"]), str(call.get("Status", "queued"))


def dial_contact(contact_id: str) -> dict[str, Any]:
    settings = get_settings()
    with transaction(settings.database_path) as connection:
        contact = connection.execute("SELECT * FROM contacts WHERE id = ?", (contact_id,)).fetchone()
        if not contact:
            raise DomainError("contact not found")
        if contact["opted_out_at"] or contact["consent_status"] not in {"granted", "service_basis"}:
            raise DomainError("contact is not eligible to call")
        attempt_id = _id()
        connection.execute("INSERT INTO call_attempts(id, campaign_id, contact_id) VALUES (?, ?, ?)", (attempt_id, contact["campaign_id"], contact_id))
    try:
        # print(settings)
        call_sid, call_status = (f"MOCK-{uuid.uuid4().hex} + {settings.call_provider}", "queued") if settings.call_provider != "exotel" else _dial_exotel(contact["phone_e164"], attempt_id, settings)
    except Exception:
        with transaction(settings.database_path) as connection:
            connection.execute("UPDATE call_attempts SET telephony_status = 'failed', updated_at = ? WHERE id = ?", (_now(), attempt_id))
        raise
    with transaction(settings.database_path) as connection:
        connection.execute("UPDATE call_attempts SET exotel_call_sid = ?, telephony_status = ?, updated_at = ? WHERE id = ?", (call_sid, call_status, _now(), attempt_id))
    return {"attempt_id": attempt_id, "call_sid": call_sid, "status": call_status}


def receive_status(payload: dict[str, Any]) -> dict[str, Any]:
    call_sid = str(payload.get("CallSid") or payload.get("call_sid") or "")
    call_status = str(payload.get("Status") or payload.get("status") or "")
    if not call_sid or not call_status:
        raise DomainError("CallSid and Status are required")
    event_key = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    with transaction() as connection:
        attempt = connection.execute("SELECT id FROM call_attempts WHERE exotel_call_sid = ?", (call_sid,)).fetchone()
        if not attempt and payload.get("CustomField"):
            attempt = connection.execute(
                "SELECT id FROM call_attempts WHERE id = ? AND exotel_call_sid IS NULL", (str(payload["CustomField"]),)
            ).fetchone()
            if attempt:
                connection.execute("UPDATE call_attempts SET exotel_call_sid = ? WHERE id = ?", (call_sid, attempt["id"]))
        try:
            connection.execute("INSERT INTO telephony_events(id, call_attempt_id, event_key, payload_json) VALUES (?, ?, ?, ?)", (_id(), attempt["id"] if attempt else None, event_key, json.dumps(payload)))
        except sqlite3.IntegrityError:
            return {"duplicate": True}
        if attempt:
            connection.execute("UPDATE call_attempts SET telephony_status = ?, updated_at = ? WHERE id = ?", (call_status, _now(), attempt["id"]))
    return {"duplicate": False, "matched": bool(attempt)}


def bind_media_stream(attempt_id: str, call_sid: str, stream_sid: str) -> None:
    with transaction() as connection:
        attempt = connection.execute("SELECT exotel_call_sid FROM call_attempts WHERE id = ?", (attempt_id,)).fetchone()
        if not attempt:
            raise DomainError("media stream does not match a dialed call")
        # The stream can open before dial_contact has stored the SID; the token is HMAC-signed,
        # so adopt the SID if it is still empty, but never accept a different one.
        if attempt["exotel_call_sid"] not in (None, call_sid):
            raise DomainError("media stream does not match a dialed call")
        try:
            connection.execute(
                "UPDATE call_attempts SET exotel_call_sid = ?, exotel_stream_sid = ?, updated_at = ? WHERE id = ?",
                (call_sid, stream_sid, _now(), attempt_id),
            )
        except sqlite3.IntegrityError as error:
            raise DomainError("call or stream SID is already bound to another attempt") from error


def record_outcome(attempt_id: str, disposition: str, details: dict[str, Any]) -> dict[str, bool]:
    with transaction() as connection:
        if not connection.execute("SELECT 1 FROM call_attempts WHERE id = ?", (attempt_id,)).fetchone():
            raise DomainError("call attempt not found")
        try:
            connection.execute(
                "INSERT INTO conversation_outcomes(id, call_attempt_id, disposition, details_json) VALUES (?, ?, ?, ?)",
                (_id(), attempt_id, disposition, json.dumps(details)),
            )
        except sqlite3.IntegrityError:
            return {"duplicate": True}
    return {"duplicate": False}