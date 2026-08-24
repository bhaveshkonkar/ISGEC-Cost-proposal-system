import os
import re
import uuid
import html as html_lib
from email.message import EmailMessage as MIMEEmailMessage
from email.utils import parseaddr, make_msgid
import aiosmtplib
from imap_tools import MailBox, AND
from app.config import (
    IMAP_HOST,
    IMAP_PORT,
    IMAP_USER,
    IMAP_PASSWORD,
    MAIL_FOLDER,
    SMTP_HOST,
    SMTP_PORT,
    SENDER_NAME,
    UPLOAD_DIR,
)

ATTACHMENT_EXTS = (".pdf", ".docx", ".doc", ".xlsx", ".xls", ".csv", ".txt", ".png", ".jpg", ".jpeg")


def is_configured() -> bool:
    return bool(IMAP_USER and IMAP_PASSWORD)


def _html_to_text(raw_html: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", raw_html or "", flags=re.I)
    text = re.sub(r"</(p|div|li|tr|h[1-6]|blockquote)>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_lib.unescape(text)
    return re.sub(r"\n{3,}", "\n\n", text)


def _extract_body(msg) -> str:
    body = msg.text or ""
    if not body.strip() and msg.html:
        body = _html_to_text(msg.html)
    return body


def _clean_body(raw: str) -> str:
    text = re.sub(r"\r\n", "\n", raw or "")
    quoted_idx = text.find("-----Original Message-----")
    if quoted_idx > 0:
        text = text[:quoted_idx]
    signature_idx = text.find("--\n")
    if signature_idx > 0:
        text = text[:signature_idx]
    return text.strip()


def fetch_unread(limit: int = 10) -> list[dict]:
    if not is_configured():
        raise RuntimeError("IMAP_USER / IMAP_PASSWORD not configured in .env")

    emails = []
    with MailBox(IMAP_HOST, port=IMAP_PORT).login(IMAP_USER, IMAP_PASSWORD, initial_folder=MAIL_FOLDER) as mailbox:
        for msg in mailbox.fetch(AND(seen=False), mark_seen=True, limit=limit):
            from_name, from_addr = parseaddr(msg.from_)
            if from_addr.lower() == IMAP_USER.lower():
                continue
            attachments = []
            os.makedirs(UPLOAD_DIR, exist_ok=True)
            for att in msg.attachments:
                if not att.filename:
                    continue
                ext = os.path.splitext(att.filename)[1].lower()
                if ext not in ATTACHMENT_EXTS:
                    continue
                saved_path = os.path.join(UPLOAD_DIR, f"mail_{uuid.uuid4().hex[:8]}_{att.filename}")
                with open(saved_path, "wb") as f:
                    f.write(att.payload)
                attachments.append({"filename": att.filename, "path": saved_path})

            emails.append({
                "message_id": (msg.headers.get("message-id") or [make_msgid()])[0].strip(),
                "uid": int(msg.uid),
                "from_name": from_name,
                "from_addr": from_addr,
                "subject": msg.subject or "(no subject)",
                "body_text": _clean_body(_extract_body(msg)),
                "attachments": attachments,
                "received_at": msg.date,
                "in_reply_to": (msg.headers.get("in-reply-to") or [""])[0].strip(),
                "references": " ".join(msg.headers.get("references") or []),
            })
    return emails


async def send_email(to_addr: str, subject: str, html_body: str, in_reply_to: str = "") -> str:
    mime = MIMEEmailMessage()
    mime["From"] = f"{SENDER_NAME} <{IMAP_USER}>"
    mime["To"] = to_addr
    mime["Subject"] = subject
    mime["Message-ID"] = make_msgid()
    if in_reply_to:
        mime["In-Reply-To"] = in_reply_to
        mime["References"] = in_reply_to
    mime.set_content(html_body, subtype="html")

    await aiosmtplib.send(
        mime,
        hostname=SMTP_HOST,
        port=SMTP_PORT,
        username=IMAP_USER,
        password=IMAP_PASSWORD,
        start_tls=True,
    )
    return mime["Message-ID"]
