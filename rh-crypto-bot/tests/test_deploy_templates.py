"""The launchd plist templates must render to valid, correctly-configured plists."""

from __future__ import annotations

import plistlib
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
DEPLOY = REPO / "deploy"


def _render(name: str) -> dict:
    txt = (DEPLOY / f"{name}.plist.template").read_text()
    txt = txt.replace("__REPO__", "/opt/rhbot").replace("__UID__", "501")
    return plistlib.loads(txt.encode())


def test_paper_plist_shape():
    p = _render("com.rhcryptobot.paper")
    assert p["Label"] == "com.rhcryptobot.paper"
    assert p["RunAtLoad"] is True
    assert p["KeepAlive"]["SuccessfulExit"] is False   # exit 0 -> stay stopped
    assert p["KeepAlive"]["Crashed"] is True
    assert p["ProgramArguments"][-1] == "/opt/rhbot/deploy/run.sh"
    assert p["WorkingDirectory"] == "/opt/rhbot"
    assert "paper.log" not in p["StandardOutPath"]     # app log is separate + rotating


def test_watchdog_plist_shape():
    p = _render("com.rhcryptobot.watchdog")
    assert p["Label"] == "com.rhcryptobot.watchdog"
    assert p["StartInterval"] == 300
    assert p["ProgramArguments"][-1] == "/opt/rhbot/scripts/watchdog.sh"


def test_publish_plist_shape():
    p = _render("com.rhcryptobot.publish")
    assert p["Label"] == "com.rhcryptobot.publish"
    assert p["StartInterval"] == 120
    assert p["ProgramArguments"][-1] == "/opt/rhbot/scripts/publish_status.sh"
    assert p["WorkingDirectory"] == "/opt/rhbot"


def test_launchd_install_renders_every_template():
    """Every deploy/*.plist.template must be rendered by `launchd.sh install`.

    The publish agent sat stale for a day because install only knew about two of the
    three plists, so a repo move silently left it pointing at a dead path (exit 127)
    while the dashboard feed froze.
    """
    txt = (REPO / "scripts" / "launchd.sh").read_text()
    # label -> shell var holding it, e.g. PAPER="com.rhcryptobot.paper"
    var_for = {m.group(2): m.group(1)
               for m in re.finditer(r'^(\w+)="(com\.rhcryptobot\.[\w.]+)"$', txt, re.M)}
    for f in DEPLOY.glob("*.plist.template"):
        label = f.name.replace(".plist.template", "")
        assert label in var_for, f"{label} has no variable in launchd.sh"
        assert f'render "$REPO/deploy/${var_for[label]}.plist.template"' in txt, \
            f"{label} is never rendered by launchd.sh install"


def test_publish_status_site_default_beside_repo():
    """Default site dir must track where the repo actually lives.

    Monorepo layout: the monitor page + status.json live at the repo root, one
    level above the bot package, so the default is the parent of the bot repo.
    """
    txt = (REPO / "scripts" / "publish_status.sh").read_text()
    assert 'SITE="${MONITOR_SITE_DIR:-$(dirname "$REPO")}"' in txt


def test_no_unrendered_placeholders():
    for f in DEPLOY.glob("*.template"):
        rendered = f.read_text().replace("__REPO__", "x").replace("__UID__", "1")
        assert "__" not in rendered, f


@pytest.mark.parametrize("script", ["deploy/run.sh", "scripts/watchdog.sh", "scripts/launchd.sh",
                                    "scripts/paper.sh", "scripts/publish_status.sh"])
def test_shell_scripts_parse(script):
    r = subprocess.run(["bash", "-n", str(REPO / script)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_run_sh_refuses_when_dead():
    txt = (REPO / "deploy" / "run.sh").read_text()
    # DEAD marker check must gate the exec with a clean exit
    assert "DEAD" in txt
    assert txt.index('-f "$dead"') < txt.index("exec caffeinate")


def test_watchdog_sh_checks_dead():
    txt = (REPO / "scripts" / "watchdog.sh").read_text()
    assert "DEAD" in txt
    assert '"$dead_present" -eq 1' in txt


def test_launchd_sh_has_revive():
    txt = (REPO / "scripts" / "launchd.sh").read_text()
    assert "revive)" in txt and "--confirm" in txt
    assert "initial_equity" in txt   # revive re-anchors the deposit floor
