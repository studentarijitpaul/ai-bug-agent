"""Gmail API Client for sending approval reports and reading replies.

Uses least-privilege OAuth 2.0 scopes:
- https://www.googleapis.com/auth/gmail.send (send emails only)
- https://www.googleapis.com/auth/gmail.readonly (read emails and metadata only)
"""

import abc
import base64
import email
import json
import logging
import os
import re
from dataclasses import dataclass, field
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Minimum required scopes
GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
]


class GmailError(Exception):
    """Base exception for Gmail operations."""
    pass


class GmailAuthError(GmailError):
    """Raised on authentication or token expiration errors."""
    pass


@dataclass
class InboundEmail:
    """Represents an incoming email message with authenticated headers."""
    message_id: str
    thread_id: str
    sender: str  # Extracted raw email address
    subject: str
    body: str
    date: str
    headers: Dict[str, str] = field(default_factory=dict)


class GmailClientBase(abc.ABC):
    """Abstract interface for Gmail client."""

    @abc.abstractmethod
    def send_email(self, to: str, subject: str, body_text: str, body_html: Optional[str] = None) -> Tuple[str, str]:
        """Send an email. Returns (message_id, thread_id)."""
        pass

    @abc.abstractmethod
    def fetch_inbound_replies(self, query: str = "is:unread") -> List[InboundEmail]:
        """Fetch unread/matching replies with authenticated headers."""
        pass


class MockGmailClient(GmailClientBase):
    """Mock Gmail client for testing and offline execution."""

    def __init__(self, approver_email: str = "approver@example.com"):
        self.approver_email = approver_email
        self.sent_messages: List[Dict[str, Any]] = []
        self.inbox: List[InboundEmail] = []
        self.fail_mode: Optional[str] = None

    def send_email(self, to: str, subject: str, body_text: str, body_html: Optional[str] = None) -> Tuple[str, str]:
        if self.fail_mode == "auth_error":
            raise GmailAuthError("Simulated OAuth authentication failure")
        if self.fail_mode == "send_error":
            raise GmailError("Simulated network send error")

        msg_id = f"mock-msg-{len(self.sent_messages) + 1}"
        thread_id = f"mock-thread-{len(self.sent_messages) + 1}"
        self.sent_messages.append({
            "message_id": msg_id,
            "thread_id": thread_id,
            "to": to,
            "subject": subject,
            "body_text": body_text,
            "body_html": body_html,
        })
        return msg_id, thread_id

    def fetch_inbound_replies(self, query: str = "is:unread") -> List[InboundEmail]:
        if self.fail_mode == "fetch_error":
            raise GmailError("Simulated fetch error")
        return list(self.inbox)

    def queue_reply(self, message_id: str, sender: str, subject: str, body: str) -> None:
        """Helper to inject an email into the mock inbox."""
        self.inbox.append(InboundEmail(
            message_id=message_id,
            thread_id=f"thread-{message_id}",
            sender=sender,
            subject=subject,
            body=body,
            date="2026-10-25T12:00:00Z",
        ))


class GmailClient(GmailClientBase):
    """Production Gmail API client using official google-api-python-client & OAuth 2.0."""

    def __init__(
        self,
        credentials_file: str = "credentials.json",
        token_file: str = "token.json",
        scopes: Optional[List[str]] = None,
        credentials_json: Optional[str] = None,
        token_json: Optional[str] = None,
    ):
        self.credentials_file = credentials_file
        self.token_file = token_file
        self.scopes = scopes or GMAIL_SCOPES
        self.credentials_json = credentials_json or os.environ.get("GMAIL_CREDENTIALS_JSON")
        self.token_json = token_json or os.environ.get("GMAIL_TOKEN_JSON")
        self._service = None

    def _get_service(self):
        """Build authorized Gmail resource."""
        if self._service is not None:
            return self._service

        try:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build
        except ImportError:
            raise GmailError(
                "Missing Google client dependencies. Install with: pip install google-api-python-client google-auth-oauthlib"
            )

        creds = None
        if self.token_json:
            try:
                token_info = json.loads(self.token_json)
                if self.credentials_json:
                    client_info = json.loads(self.credentials_json)
                    client_info = client_info.get("installed", client_info.get("web", client_info))
                    token_info.setdefault("client_id", client_info.get("client_id"))
                    token_info.setdefault("client_secret", client_info.get("client_secret"))
                    token_info.setdefault("token_uri", client_info.get("token_uri", "https://oauth2.googleapis.com/token"))
                creds = Credentials.from_authorized_user_info(token_info, self.scopes)
            except Exception as e:
                raise GmailAuthError(f"GMAIL_TOKEN_JSON is invalid: {type(e).__name__}") from None
        elif os.path.exists(self.token_file):
            try:
                creds = Credentials.from_authorized_user_file(self.token_file, self.scopes)
            except Exception as e:
                logger.warning(f"Failed to read existing token file {self.token_file}: {e}")

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try:
                    logger.info("Refreshing expired Gmail OAuth token...")
                    creds.refresh(Request())
                except Exception as e:
                    raise GmailAuthError(f"Failed to refresh OAuth token: {e}")
            else:
                raise GmailAuthError(
                    "No valid Gmail refresh token is available. Interactive OAuth is disabled; "
                    "supply GMAIL_TOKEN_JSON with an authorized-user OAuth JSON containing a refresh_token."
                )

            # Store credentials securely
            try:
                Path(self.token_file).parent.mkdir(parents=True, exist_ok=True)
                with open(self.token_file, "w", encoding="utf-8") as token_out:
                    token_out.write(creds.to_json())
            except Exception:
                logger.error("Failed saving refreshed Gmail OAuth credentials.")

        try:
            self._service = build("gmail", "v1", credentials=creds, cache_discovery=False)
            return self._service
        except Exception as e:
            raise GmailError(f"Failed to initialize Gmail API service: {e}")

    def send_email(self, to: str, subject: str, body_text: str, body_html: Optional[str] = None) -> Tuple[str, str]:
        service = self._get_service()
        msg = MIMEMultipart("alternative")
        msg["To"] = to
        msg["Subject"] = subject
        msg.attach(MIMEText(body_text, "plain", "utf-8"))
        if body_html:
            msg.attach(MIMEText(body_html, "html", "utf-8"))

        raw_b64 = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
        try:
            sent = service.users().messages().send(userId="me", body={"raw": raw_b64}).execute()
            msg_id = sent.get("id", "")
            thread_id = sent.get("threadId", "")
            return msg_id, thread_id
        except Exception as e:
            raise GmailError(f"Failed to send email to {to}: {e}")

    def fetch_inbound_replies(self, query: str = "is:unread") -> List[InboundEmail]:
        service = self._get_service()
        try:
            res = service.users().messages().list(userId="me", q=query).execute()
            messages = res.get("messages", [])
        except Exception as e:
            raise GmailError(f"Failed listing messages with query '{query}': {e}")

        results: List[InboundEmail] = []
        for m in messages:
            msg_id = m.get("id")
            if not msg_id:
                continue
            try:
                full_msg = service.users().messages().get(userId="me", id=msg_id, format="full").execute()
                payload = full_msg.get("payload", {})
                headers_list = payload.get("headers", [])
                headers_map = {h["name"].lower(): h["value"] for h in headers_list if "name" in h and "value" in h}

                sender_raw = headers_map.get("from", "")
                # Extract pure email from From header (e.g., 'User <user@example.com>')
                email_match = re.search(r"<([^>]+)>", sender_raw)
                clean_sender = email_match.group(1).lower().strip() if email_match else sender_raw.lower().strip()

                subject = headers_map.get("subject", "")
                date_str = headers_map.get("date", "")
                thread_id = full_msg.get("threadId", "")

                # Extract body
                body = self._extract_body(payload)

                results.append(InboundEmail(
                    message_id=msg_id,
                    thread_id=thread_id,
                    sender=clean_sender,
                    subject=subject,
                    body=body,
                    date=date_str,
                    headers=headers_map,
                ))
            except Exception as e:
                logger.warning(f"Failed fetching details for message {msg_id}: {e}")

        return results

    def _extract_body(self, payload: Dict[str, Any]) -> str:
        """Extract plain text body recursively from payload parts."""
        mime_type = payload.get("mimeType", "")
        body_data = payload.get("body", {}).get("data")
        if body_data and (mime_type == "text/plain" or not mime_type):
            return base64.urlsafe_b64decode(body_data).decode("utf-8", errors="replace")

        parts = payload.get("parts", [])
        for part in parts:
            part_mime = part.get("mimeType", "")
            part_data = part.get("body", {}).get("data")
            if part_mime == "text/plain" and part_data:
                return base64.urlsafe_b64decode(part_data).decode("utf-8", errors="replace")

        # Fallback to HTML if no plain text
        for part in parts:
            part_data = part.get("body", {}).get("data")
            if part_data:
                return base64.urlsafe_b64decode(part_data).decode("utf-8", errors="replace")

        return ""
