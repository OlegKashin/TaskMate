"""Small, bounded mailbox adapters. Network access is isolated for testing."""

import base64
import imaplib
import re
import smtplib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import parseaddr, parsedate_to_datetime

import httpx

from app.core.errors import AppError


@dataclass
class MailAttachment:
    filename: str
    mime_type: str
    content: bytes


@dataclass
class MailRecord:
    external_id: str
    thread_id: str | None
    sender: str
    sender_name: str
    subject: str
    text: str
    received_at: datetime
    message_id_header: str | None
    attachments: list[MailAttachment] = field(default_factory=list)


def parse_mail(raw: bytes, fallback_id: str, thread_id: str | None = None) -> MailRecord:
    mail = BytesParser(policy=policy.default).parsebytes(raw)
    name, sender = parseaddr(str(mail.get("From", "")))
    try:
        received = parsedate_to_datetime(str(mail.get("Date", "")))
        if received.tzinfo is None:
            received = received.replace(tzinfo=UTC)
        received = received.astimezone(UTC)
    except (ValueError, TypeError, IndexError):
        received = datetime.now(UTC)
    parts = [mail] if not mail.is_multipart() else list(mail.walk())
    text = next(
        (
            part.get_content()
            for part in parts
            if part.get_content_type() == "text/plain"
            and part.get_content_disposition() != "attachment"
        ),
        "",
    )
    if not isinstance(text, str):
        text = ""
    attachments = [
        MailAttachment(
            part.get_filename() or "attachment",
            part.get_content_type(),
            part.get_payload(decode=True) or b"",
        )
        for part in parts
        if part.get_content_disposition() == "attachment" or part.get_filename()
    ]
    header_id = str(mail.get("Message-ID", "")).strip() or None
    return MailRecord(
        header_id or fallback_id,
        thread_id,
        sender,
        name,
        str(mail.get("Subject", "")),
        text,
        received,
        header_id,
        attachments,
    )


def _require_http(response):
    try:
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise AppError("MAIL_PROVIDER_ERROR", "Mail provider request failed", 502) from exc


def gmail_messages(
    token: str, since: datetime, labels: list[str], limit: int = 1000
) -> list[MailRecord]:
    headers = {"Authorization": f"Bearer {token}"}
    params = {
        "q": f"after:{int((since - timedelta(days=1)).timestamp())}",
        "maxResults": min(limit, 100),
    }
    if labels:
        params["labelIds"] = labels
    records = []
    fetched = 0
    while True:
        listing = _require_http(
            httpx.get(
                "https://gmail.googleapis.com/gmail/v1/users/me/messages",
                headers=headers,
                params=params,
                timeout=20,
            )
        )
        items = listing.get("messages", [])
        fetched += len(items)
        if fetched > limit:
            raise AppError("MAIL_BATCH_TOO_LARGE", "Mailbox batch exceeds sync limit", 503)
        for item in items:
            details = _require_http(
                httpx.get(
                    f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{item['id']}",
                    headers=headers,
                    params={"format": "raw"},
                    timeout=20,
                )
            )
            raw = base64.urlsafe_b64decode(details["raw"] + "===")
            record = parse_mail(raw, item["id"], details.get("threadId"))
            if details.get("internalDate"):
                record.received_at = datetime.fromtimestamp(
                    int(details["internalDate"]) / 1000, UTC
                )
            if record.received_at > since:
                records.append(record)
        if not listing.get("nextPageToken"):
            break
        params["pageToken"] = listing["nextPageToken"]
    return records


IMAP_HOSTS = {"yandex": "imap.yandex.com", "mailru": "imap.mail.ru"}
SMTP_HOSTS = {"yandex": "smtp.yandex.com", "mailru": "smtp.mail.ru"}


def _xoauth2(username: str, token: str) -> bytes:
    return f"user={username}\x01auth=Bearer {token}\x01\x01".encode()


def imap_messages(
    provider: str,
    username: str,
    secret: str,
    since: datetime,
    folders: list[str],
    *,
    oauth: bool = True,
    host: str | None = None,
    limit: int = 1000,
) -> list[MailRecord]:
    hostname = host or IMAP_HOSTS.get(provider)
    if not hostname:
        raise AppError("MAIL_NOT_CONFIGURED", "IMAP host is missing", 503)
    records = []
    try:
        with imaplib.IMAP4_SSL(hostname, 993, timeout=20) as client:
            if oauth:
                client.authenticate("XOAUTH2", lambda _: _xoauth2(username, secret))
            else:
                client.login(username, secret)
            for folder in folders or ["INBOX"]:
                status, _ = client.select(folder, readonly=True)
                if status != "OK":
                    raise AppError("MAIL_FOLDER_ERROR", "Cannot open selected folder", 502)
                validity = client.response("UIDVALIDITY")[1]
                uidvalidity = validity[0].decode() if validity and validity[0] else "unknown"
                date = (since - timedelta(days=1)).strftime("%d-%b-%Y")
                status, data = client.uid("search", None, "SINCE", date)
                if status != "OK":
                    raise AppError("MAIL_PROVIDER_ERROR", "IMAP search failed", 502)
                uids = (data[0] or b"").split()
                if len(uids) > limit:
                    raise AppError("MAIL_BATCH_TOO_LARGE", "Mailbox batch exceeds sync limit", 503)
                for uid in uids:
                    status, fetched = client.uid("fetch", uid, "(RFC822 INTERNALDATE)")
                    if status != "OK":
                        raise AppError("MAIL_PROVIDER_ERROR", "IMAP fetch failed", 502)
                    payload = next((part for part in fetched if isinstance(part, tuple)), None)
                    raw = payload[1] if payload else None
                    if raw:
                        record = parse_mail(raw, f"{uidvalidity}:{uid.decode()}")
                        match = re.search(rb'INTERNALDATE "([^"]+)"', payload[0])
                        if match:
                            record.received_at = datetime.strptime(
                                match.group(1).decode(), "%d-%b-%Y %H:%M:%S %z"
                            ).astimezone(UTC)
                        if record.received_at > since:
                            records.append(record)
    except (imaplib.IMAP4.error, OSError) as exc:
        raise AppError("MAIL_PROVIDER_ERROR", "IMAP connection failed", 502) from exc
    return records


def send_reply(
    provider: str,
    username: str,
    secret: str,
    recipient: str,
    subject: str,
    body: str,
    in_reply_to: str | None,
    thread_id: str | None = None,
    *,
    oauth: bool = True,
    host: str | None = None,
) -> str:
    if not recipient or "@" not in recipient:
        raise AppError("INVALID_RECIPIENT", "Original sender has no email address", 422)
    mail = EmailMessage()
    mail["From"], mail["To"] = username, recipient
    mail["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    if in_reply_to:
        mail["In-Reply-To"] = in_reply_to
        mail["References"] = in_reply_to
    mail.set_content(body)
    if provider == "gmail":
        payload = {"raw": base64.urlsafe_b64encode(mail.as_bytes()).decode().rstrip("=")}
        if thread_id:
            payload["threadId"] = thread_id
        result = _require_http(
            httpx.post(
                "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
                headers={"Authorization": f"Bearer {secret}"},
                json=payload,
                timeout=20,
            )
        )
        return str(result["id"])
    hostname = host or SMTP_HOSTS.get(provider)
    if not hostname:
        raise AppError("MAIL_NOT_CONFIGURED", "SMTP host is missing", 503)
    try:
        with smtplib.SMTP_SSL(hostname, 465, timeout=20) as client:
            if oauth:
                client.auth("XOAUTH2", lambda _: _xoauth2(username, secret).decode())
            else:
                client.login(username, secret)
            client.send_message(mail)
    except (smtplib.SMTPException, OSError) as exc:
        raise AppError("MAIL_PROVIDER_ERROR", "SMTP send failed", 502) from exc
    return str(mail["Message-ID"]) if mail.get("Message-ID") else "sent"
