from __future__ import annotations

import httpx
import pytest

from botcore.config import Settings
from botcore.notify.push import Notifier, get_notifier, reset_notifier


class FakeResp:
    def __init__(self, code=200):
        self.status_code = code


class FakeClient:
    def __init__(self, raise_exc=None, code=200):
        self.calls = []
        self._raise = raise_exc
        self._code = code

    def post(self, url, content=None, headers=None):
        self.calls.append({"url": url, "content": content, "headers": headers or {}})
        if self._raise:
            raise self._raise
        return FakeResp(self._code)

    def close(self):
        pass


def test_ntfy_posts_expected_shape():
    fc = FakeClient()
    n = Notifier(ntfy_topic="rhbot-secret", client=fc)
    ok = n.ntfy("hello world", title="Alert", priority="urgent", tags=["rotating_light", "x"])
    assert ok is True
    call = fc.calls[0]
    assert call["url"] == "https://ntfy.sh/rhbot-secret"
    assert call["content"] == b"hello world"
    assert call["headers"]["Title"] == "Alert"
    assert call["headers"]["Priority"] == "urgent"
    assert call["headers"]["Tags"] == "rotating_light,x"


def test_ntfy_title_is_ascii_folded():
    fc = FakeClient()
    n = Notifier(ntfy_topic="t", client=fc)
    n.ntfy("body", title="BOT KILLED — DEAD")   # em-dash breaks latin-1 HTTP headers
    hdr = fc.calls[0]["headers"]["Title"]
    hdr.encode("latin-1")                       # must not raise
    assert "—" not in hdr and "-" in hdr


def test_ntfy_disabled_when_no_topic():
    fc = FakeClient()
    n = Notifier(ntfy_topic="", client=fc)
    assert n.ntfy("x") is False
    assert fc.calls == []


def test_ntfy_never_raises_on_network_error():
    fc = FakeClient(raise_exc=httpx.ConnectError("boom"))
    n = Notifier(ntfy_topic="t", client=fc)
    assert n.ntfy("x") is False  # swallowed


def test_ntfy_false_on_non_2xx():
    n = Notifier(ntfy_topic="t", client=FakeClient(code=503))
    assert n.ntfy("x") is False


def test_notify_rate_limits_by_key():
    fc = FakeClient()
    n = Notifier(ntfy_topic="t", client=fc, min_interval_s=10_000)
    n.notify("first", key="halt")
    n.notify("second", key="halt")
    n.notify("other", key="watchdog")
    assert len(fc.calls) == 2  # halt deduped, watchdog allowed


def test_notify_no_key_is_not_rate_limited():
    fc = FakeClient()
    n = Notifier(ntfy_topic="t", client=fc, min_interval_s=10_000)
    n.notify("a")
    n.notify("b")
    assert len(fc.calls) == 2


def test_email_noop_without_config():
    n = Notifier(ntfy_topic="t", client=FakeClient())
    assert n.email("s", "b") is False


def test_email_never_raises_on_bad_url(monkeypatch):
    n = Notifier(smtp_url="smtp+tls://nope", email_to="x@y.com", client=FakeClient())
    assert n.email("s", "b") is False


def test_get_notifier_singleton_and_reset():
    reset_notifier()
    s = Settings(_env_file=None, ntfy_topic="abc")
    a = get_notifier(s)
    b = get_notifier(s)
    assert a is b
    reset_notifier()
    assert get_notifier(s) is not a
