"""`botcore.serve --once` must print ONLY its JSON result to stdout (logs -> stderr)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _run_once(tmp_path, **env_over):
    env = dict(os.environ)
    env.update(BOT_MODE="paper", BROKER="sim", DB_PATH=str(tmp_path / "bot.db"))
    for k in ("NTFY_TOPIC", "SMTP_URL", "ALERT_EMAIL_TO", "LOG_DIR"):
        env.pop(k, None)
    env.update(env_over)
    return subprocess.run(
        [sys.executable, "-m", "botcore.serve", "--once"],
        capture_output=True, text=True, env=env, timeout=180, cwd=str(REPO),
    )


def test_once_stdout_is_clean_json(tmp_path):
    r = _run_once(tmp_path)
    assert r.returncode == 0, r.stderr[-2000:]
    payload = json.loads(r.stdout)             # must parse — no log lines mixed in
    assert "entries" in payload and "exits" in payload


def test_once_reports_dead_when_marker_present(tmp_path):
    (tmp_path / "DEAD").write_text('{"reason": "equity <= deposit floor", "iso": "x"}')
    r = _run_once(tmp_path)
    assert r.returncode == 0, r.stderr[-2000:]
    payload = json.loads(r.stdout)
    assert payload["dead"] is True
    assert "floor" in payload["certificate"]["reason"]


def test_once_degrades_when_alpaca_key_missing(tmp_path):
    r = _run_once(tmp_path, BROKER="alpaca", ALPACA_KEY_ID="", ALPACA_SECRET_KEY="")
    assert r.returncode == 0, r.stderr[-2000:]
    payload = json.loads(r.stdout)
    assert payload["engine"] == "down"
    assert "ALPACA_KEY_ID" in payload["error"]
