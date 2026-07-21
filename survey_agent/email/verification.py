"""Gmail verification code reader — fetches 2FA codes from Gmail via IMAP.

Used by the survey agent to complete email-based 2FA challenges on sites
like YouGov, EurekaSurveys, Prime Opinion, etc.

Setup:
1. User enables 2-step verification in Google account
2. User creates an App Password: https://myaccount.google.com/apppasswords
3. User runs: `survey-agent email setup --email user@gmail.com --password <app-password>`
4. The agent can now read verification emails sent during login flows

Security:
- App password is stored in macOS Keychain (not plaintext)
- Only IMAP access (no send/forward/delete permissions)
- Read-only on INBOX
"""

from __future__ import annotations

import imaplib
import logging
import os
import re
import time
from dataclasses import dataclass
from email import policy
from email.header import decode_header
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from typing import Any

logger = logging.getLogger(__name__)

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993


@dataclass
class VerificationEmail:
    """A verification email with a code."""

    subject: str
    sender: str
    date: str
    code: str
    body_preview: str


class GmailVerificationReader:
    """Reads verification codes from Gmail via IMAP.

    Requires Gmail App Password (NOT the user's regular password).
    App passwords are created at https://myaccount.google.com/apppasswords
    """

    def __init__(self, email: str, app_password: str):
        self._email = email
        self._password = app_password
        self._conn: imaplib.IMAP4_SSL | None = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

    def connect(self) -> None:
        """Open IMAP connection to Gmail."""
        if self._conn is not None:
            return
        logger.info(f"Connecting to {IMAP_HOST}:{IMAP_PORT} as {self._email}")
        self._conn = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        self._conn.login(self._email, self._password)
        self._conn.select("INBOX")
        logger.info("IMAP connection established")

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
                self._conn.logout()
            except Exception:
                pass
            self._conn = None

    def get_recent_emails(
        self,
        sender_pattern: str | None = None,
        subject_pattern: str | None = None,
        since_minutes: int = 10,
        limit: int = 20,
    ) -> list[VerificationEmail]:
        """Fetch recent emails matching sender/subject patterns.

        Args:
            sender_pattern: substring to match in From header (case-insensitive)
            subject_pattern: regex to match in Subject header
            since_minutes: only emails received in the last N minutes
            limit: max emails to fetch
        """
        if self._conn is None:
            self.connect()

        # Build search query
        criteria = [f'(SINCE "{time.strftime("%d-%b-%Y", time.gmtime(time.time() - since_minutes * 60))}")']
        if sender_pattern:
            criteria.append(f'FROM "{sender_pattern}"')

        query = " ".join(criteria)
        logger.info(f"IMAP search: {query}")
        typ, data = self._conn.search(None, query)
        if typ != "OK" or not data[0]:
            return []

        msg_ids = data[0].split()[-limit:]
        results = []
        for msg_id in reversed(msg_ids):  # newest first
            try:
                typ, msg_data = self._conn.fetch(msg_id, "(RFC822)")
                if typ != "OK":
                    continue
                raw = msg_data[0][1]
                msg = BytesParser(policy=policy.default).parsebytes(raw)
                email = self._parse_email(msg)
                if email is None:
                    continue
                # Apply subject pattern filter
                if subject_pattern and not re.search(subject_pattern, email.subject, re.I):
                    continue
                results.append(email)
            except Exception as e:
                logger.debug(f"Failed to fetch message {msg_id}: {e}")
                continue
        return results

    def find_verification_code(
        self,
        sender_pattern: str | None = None,
        subject_pattern: str | None = None,
        timeout: int = 60,
        poll_interval: int = 5,
    ) -> str | None:
        """Poll for verification code. Waits up to timeout seconds.

        Args:
            sender_pattern: substring to match in From header
            subject_pattern: regex to match in Subject header
            timeout: max seconds to wait
            poll_interval: seconds between checks

        Returns:
            The verification code if found, None if timeout
        """
        deadline = time.time() + timeout
        attempt = 0
        # Use a moving SINCE window — start with 2 min, expand to full timeout
        while time.time() < deadline:
            attempt += 1
            since_minutes = min(max(2, (timeout - (deadline - time.time())) / 60 * 1.5), timeout // 60 + 1)
            try:
                emails = self.get_recent_emails(
                    sender_pattern=sender_pattern,
                    subject_pattern=subject_pattern,
                    since_minutes=int(since_minutes) + 1,
                    limit=5,
                )
            except Exception as e:
                logger.warning(f"IMAP fetch failed: {e}")
                time.sleep(poll_interval)
                continue

            for email in emails:
                code = self._extract_code(email)
                if code:
                    logger.info(f"Found verification code {code} from {email.sender}")
                    return code

            logger.debug(f"Attempt {attempt}: no code yet, polling again in {poll_interval}s")
            time.sleep(poll_interval)
        return None

    @staticmethod
    def _parse_email(msg: Any) -> VerificationEmail | None:
        """Extract subject, sender, date, body from email.message."""
        try:
            subject = msg.get("Subject", "")
            if subject:
                decoded = decode_header(subject)
                subject = "".join(
                    s.decode(c or "utf-8", errors="ignore") if isinstance(s, bytes) else s
                    for s, c in decoded
                )
            sender = msg.get("From", "")
            date = msg.get("Date", "")
            # Get body text
            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        body = part.get_content()
                        break
                    if part.get_content_type() == "text/html" and not body:
                        body = part.get_content()
            else:
                body = msg.get_content()
            body_preview = (body or "")[:500]
            return VerificationEmail(
                subject=subject,
                sender=sender,
                date=date,
                code="",  # filled in by _extract_code
                body_preview=body_preview,
            )
        except Exception as e:
            logger.debug(f"Failed to parse email: {e}")
            return None

    @staticmethod
    def _extract_code(email: VerificationEmail) -> str:
        """Extract verification code from email subject/body.

        Looks for:
        - 4-8 digit codes
        - Codes with separators (123 456, 123-456, etc.)
        - Common keywords: "code:", "verification", "pin", "OTP"
        """
        text = f"{email.subject}\n{email.body_preview}"
        # Look for explicit "code: 123456" patterns
        for pattern in [
            r"(?:code|verification|pin|otp|token)[:\s]+(\d{4,8})",
            r"(?:code|verification|pin|otp|token)[:\s]+(\d{3}[\s\-]?\d{3,5})",
            r"\b(\d{6})\b",  # 6-digit code (most common)
            r"\b(\d{4,8})\b",  # any 4-8 digit code
        ]:
            match = re.search(pattern, text, re.I)
            if match:
                code = match.group(1).replace(" ", "").replace("-", "")
                if len(code) >= 4:
                    return code
        return ""


def get_gmail_credentials() -> tuple[str, str] | None:
    """Retrieve Gmail credentials from macOS Keychain.

    Returns (email, app_password) or None if not configured.
    """
    import keyring

    service = "survey-agent/gmail"
    email = os.environ.get("GMAIL_EMAIL", "")
    if not email:
        email = keyring.get_password(service, "email") or ""
    if not email:
        return None
    password = keyring.get_password(service, email)
    if not password:
        return None
    return email, password


def store_gmail_credentials(email: str, app_password: str) -> None:
    """Store Gmail app password in macOS Keychain."""
    import keyring

    service = "survey-agent/gmail"
    keyring.set_password(service, "email", email)
    keyring.set_password(service, email, app_password)
