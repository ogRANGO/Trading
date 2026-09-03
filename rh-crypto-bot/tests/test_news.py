from __future__ import annotations

import time

import numpy as np
import pandas as pd

from botcore.agents.base import AgentContext
from botcore.agents.news import NewsAgent, _score_headlines
from botcore.brokers.base import Quote
from botcore.config import AgentCfg, Settings
from botcore.data.news import Headline, _parse_rss, fetch_headlines
from botcore.store.db import open_db

_RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item><title>NVDA beats earnings, shares surge to record</title>
        <link>http://x/1</link><pubDate>{pd}</pubDate>
        <description>strong growth, analysts raise targets</description></item>
  <item><title>Regulator opens probe; company halts guidance</title>
        <link>http://x/2</link><pubDate>{pd}</pubDate>
        <description>lawsuit weighs on outlook</description></item>
</channel></rss>"""


def _now_rfc822():
    return time.strftime("%a, %d %b %Y %H:%M:%S +0000", time.gmtime())


def test_parse_rss_extracts_items():
    items = _parse_rss(_RSS.format(pd=_now_rfc822()), "yahoo")
    assert len(items) == 2
    assert "beats earnings" in items[0].title
    assert items[0].ts > 0


def test_parse_rss_bad_xml_is_empty():
    assert _parse_rss("<not xml", "yahoo") == []


def test_score_headlines_signed():
    now = time.time()
    assert _score_headlines([Headline("beats surge record upgrade", "", now)], now) > 0.5
    assert _score_headlines([Headline("plunge lawsuit fraud halt", "", now)], now) < -0.5
    assert _score_headlines([Headline("company holds annual meeting", "", now)], now) == 0.0


def test_score_headlines_recency_weights_fresh_over_stale():
    now = time.time()
    # a fresh bullish headline should outweigh a stale bearish one
    items = [Headline("surge beats record upgrade", "", now),
             Headline("plunge lawsuit fraud halt", "", now - 96 * 3600)]
    assert _score_headlines(items, now) > 0.2


def test_news_agent_emits_signals(tmp_path, monkeypatch):
    import botcore.agents.news as mod

    monkeypatch.setattr(mod, "fetch_headlines",
                        lambda sym, since, settings=None, **kw: (
                            [Headline("NVDA beats, surges to record, upgrade", "", time.time())]
                            if sym == "NVDA" else
                            [Headline("SEC probe, lawsuit, halt, fraud", "", time.time())]
                        ))
    conn = open_db(tmp_path / "n.db")
    bars = {s: pd.DataFrame({"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0], "volume": [1.0]})
            for s in ("NVDA", "AMD")}
    ctx = AgentContext(bars=bars, quotes={}, positions={}, equity=1e5,
                       universe=["NVDA", "AMD"], now=time.time(), conn=conn,
                       settings=Settings(_env_file=None), klass="equity")
    sigs = {s.symbol: s for s in NewsAgent(AgentCfg(poll_minutes=1, engine="lexicon")).signals(ctx)}
    assert sigs["NVDA"].direction == 1
    assert sigs["AMD"].direction == -1


def test_news_agent_throttles(tmp_path, monkeypatch):
    import botcore.agents.news as mod
    calls = {"n": 0}

    def fake(sym, since, settings=None, **kw):
        calls["n"] += 1
        return []

    monkeypatch.setattr(mod, "fetch_headlines", fake)
    conn = open_db(tmp_path / "n.db")
    ctx = AgentContext(bars={}, quotes={}, positions={}, equity=1e5, universe=["NVDA"],
                       now=1000.0, conn=conn, settings=Settings(_env_file=None), klass="equity")
    ag = NewsAgent(AgentCfg(poll_minutes=60))
    ag.signals(ctx)
    ctx.now = 1000.0 + 120        # 2 min later -> still within the 60-min poll window
    ag.signals(ctx)
    assert calls["n"] == 1        # only polled once


def test_fetch_headlines_network_failure_is_empty(monkeypatch):
    import botcore.data.news as mod

    def boom(*a, **k):
        raise RuntimeError("no net")

    monkeypatch.setattr(mod.httpx, "get", boom)
    assert fetch_headlines("AAPL", 0.0) == []
