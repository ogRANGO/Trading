"""Best-effort push notifications: ntfy.sh (free, no account) + optional SMTP email.

Nothing here ever raises into the caller — a dead network must not stop the engine.
ntfy is a plain HTTP POST to ``https://ntfy.sh/<topic>``; pick an unguessable topic
and subscribe in the ntfy phone app. Email is opt-in via a
``smtp+tls://user:pass@host:port`` URL.
"""

from __future__ import annotations

import logging
import smtplib
import time
from email.message import EmailMessage
from typing import Dict, Optional, Sequence
from urllib.parse import unquote, urlparse

import httpx

log = logging.getLogger(__name__)

_DASHES = str.maketrans({"—": "-", "–": "-", "‘": "'", "’": "'",
                         "“": '"', "”": '"'})


def _ascii(s: str) -> str:
    return s.translate(_DASHES).encode("ascii", "replace").decode("ascii")


class Notifier:
    def __init__(
        self,
        *,
        ntfy_base_url: str = "https://ntfy.sh",
        ntfy_topic: str = "",
        smtp_url: str = "",
        email_to: str = "",
        min_interval_s: float = 900.0,
        client: Optional[httpx.Client] = None,
    ) -> None:
        self.ntfy_base_url = (ntfy_base_url or "https://ntfy.sh").rstrip("/")
        self.ntfy_topic = (ntfy_topic or "").strip()
        self.smtp_url = (smtp_url or "").strip()
        self.email_to = (email_to or "").strip()
        self.min_interval_s = float(min_interval_s)
        self._client = client or httpx.Client(timeout=5.0)
        self._owns_client = client is None
        self._last: Dict[str, float] = {}

    def close(self) -> None:
        if self._owns_client:
            try:
                self._client.close()
            except Exception:  # noqa: BLE001
                pass

    # -- channels ------------------------------------------------------- #
    def ntfy(
        self, message: str, *, title: str = "", priority: str = "default",
        tags: Sequence[str] = (),
    ) -> bool:
        """POST ``message`` to ``{base}/{topic}``. Returns True on 2xx, never raises.
        No-op (returns False) when no topic is configured."""
        if not self.ntfy_topic:
            return False
        headers: Dict[str, str] = {}
        if title:
            headers["Title"] = _ascii(title)          # HTTP headers are latin-1 only
        if priority and priority != "default":
            headers["Priority"] = priority
        if tags:
            headers["Tags"] = ",".join(tags)
        try:
            r = self._client.post(
                f"{self.ntfy_base_url}/{self.ntfy_topic}",
                content=message.encode("utf-8"),
                headers=headers,
            )
            return r.status_code < 300
        except Exception as exc:  # noqa: BLE001
            log.warning("ntfy failed: %s", exc)
            return False

    def email(self, subject: str, body: str) -> bool:
        """Best-effort SMTP send via ``smtp_url``. Returns True on success, never raises."""
        if not (self.smtp_url and self.email_to):
            return False
        try:
            u = urlparse(self.smtp_url)
            use_ssl = u.scheme in ("smtp+ssl", "smtps")
            host = u.hostname or "localhost"
            port = u.port or (465 if use_ssl else 587)
            user = unquote(u.username) if u.username else ""
            pw = unquote(u.password) if u.password else ""

            msg = EmailMessage()
            msg["Subject"] = subject
            msg["From"] = user or self.email_to
            msg["To"] = self.email_to
            msg.set_content(body)

            smtp = (
                smtplib.SMTP_SSL(host, port, timeout=10)
                if use_ssl
                else smtplib.SMTP(host, port, timeout=10)
            )
            with smtp:
                if not use_ssl:
                    smtp.starttls()
                if user:
                    smtp.login(user, pw)
                smtp.send_message(msg)
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("email failed: %s", exc)
            return False

    # -- fan-out ------------------------------------------------------- #
    def notify(
        self, message: str, *, title: str = "", priority: str = "default",
        tags: Sequence[str] = (), key: Optional[str] = None, email_too: bool = False,
    ) -> None:
        """Send to every configured channel. Rate-limited per ``key``. Swallows all errors."""
        try:
            if key is not None:
                now = time.time()
                if now - self._last.get(key, 0.0) < self.min_interval_s:
                    return
                self._last[key] = now
            self.ntfy(message, title=title, priority=priority, tags=tags)
            if email_too:
                self.email(title or "bot alert", message)
        except Exception:  # noqa: BLE001
            log.exception("notify failed")


_notifier: Optional[Notifier] = None


def get_notifier(settings) -> Notifier:
    """Process-wide singleton built from ``Settings``."""
    global _notifier
    if _notifier is None:
        _notifier = Notifier(
            ntfy_base_url=getattr(settings, "ntfy_base_url", "https://ntfy.sh"),
            ntfy_topic=settings.ntfy_topic,
            smtp_url=settings.smtp_url,
            email_to=settings.alert_email_to,
            min_interval_s=getattr(settings, "notify_min_interval_s", 900.0),
        )
    return _notifier


def reset_notifier() -> None:
    """Test hook: drop the singleton (and close its client)."""
    global _notifier
    if _notifier is not None:
        _notifier.close()
    _notifier = None
