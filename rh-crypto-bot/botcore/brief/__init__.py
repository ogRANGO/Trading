"""Pre-market intelligence brief for the paper bots.

READ-ONLY toward the trading system. This package must never import
``botcore.brokers`` / ``botcore.engine`` / ``botcore.execution``, never open a
bot DB for write, never touch ``data/HALT`` / ``data/DEAD``, and never place an
order. Enforced by ``tests/test_brief.py::test_no_execution_imports``.

Entry point: ``python -m botcore.brief run --slot=overnight|premarket|weekend``.
"""

__all__ = ["__version__"]
__version__ = "0.1.0"
