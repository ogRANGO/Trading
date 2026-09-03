from __future__ import annotations

import logging
import sys

from botcore.logging_setup import configure_logging


def _reset():
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)


def test_rotating_file_handler_installed(tmp_path):
    _reset()
    configure_logging(log_dir=tmp_path, max_mb=5, backups=5)
    root = logging.getLogger()
    rots = [h for h in root.handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
    assert len(rots) == 1
    assert rots[0].maxBytes == 5_000_000
    assert rots[0].backupCount == 5
    _reset()


def test_noisy_loggers_quieted(tmp_path):
    _reset()
    configure_logging(log_dir=tmp_path)
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("apscheduler.scheduler").level == logging.WARNING
    _reset()


def test_never_logs_to_stdout(tmp_path):
    _reset()
    configure_logging(log_dir=tmp_path, console=True)
    for h in logging.getLogger().handlers:
        assert getattr(h, "stream", None) is not sys.stdout
    _reset()


def test_record_reaches_file(tmp_path):
    _reset()
    configure_logging(log_dir=tmp_path)
    logging.getLogger("botcore.test").warning("hello-file-42")
    for h in logging.getLogger().handlers:
        h.flush()
    assert "hello-file-42" in (tmp_path / "paper.log").read_text()
    _reset()


def test_no_file_handler_when_log_dir_none():
    _reset()
    configure_logging(log_dir=None)
    root = logging.getLogger()
    assert not [h for h in root.handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
    _reset()
