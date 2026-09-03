from __future__ import annotations

import pytest

from botcore.config import ExitCfg
from botcore.strategy.exitplan import assess, build_plan, check_exit, update_trail


CFG = ExitCfg(hard_stop_atr_mult=2.0, target_atr_mult=4.0, trail_atr_mult=3.0, time_stop_bars=10)


def test_build_plan_levels():
    p = build_plan(entry_price=100.0, atr=2.0, opened_index=5, cfg=CFG)
    assert p.hard_stop == pytest.approx(96.0)
    assert p.target == pytest.approx(108.0)
    assert p.time_stop_index == 15
    assert p.effective_stop == pytest.approx(96.0)  # no trail yet


def test_trailing_stop_only_ratchets_up():
    p = build_plan(100.0, 2.0, 0, CFG)
    update_trail(p, bar_high=110.0, atr_now=2.0, cfg=CFG)
    first = p.trail_stop
    assert first == pytest.approx(104.0)  # 110 - 3*2
    update_trail(p, bar_high=105.0, atr_now=2.0, cfg=CFG)  # lower high
    assert p.trail_stop == first          # never lowers
    update_trail(p, bar_high=120.0, atr_now=2.0, cfg=CFG)
    assert p.trail_stop == pytest.approx(114.0)


def test_check_exit_priority_hard_stop_first():
    p = build_plan(100.0, 2.0, 0, CFG)
    update_trail(p, 120.0, 2.0, CFG)  # trail at 114
    # bar craters through both hard stop and trail
    res = check_exit(p, bar_low=90.0, bar_high=121.0, bar_index=1)
    assert res == ("hard_stop", pytest.approx(96.0))


def test_check_exit_target_and_time():
    p = build_plan(100.0, 2.0, 0, ExitCfg(hard_stop_atr_mult=2, target_atr_mult=4, trail_atr_mult=0, time_stop_bars=10))
    assert check_exit(p, 99.0, 109.0, 1) == ("target", pytest.approx(108.0))
    assert check_exit(p, 99.0, 100.5, 1) is None
    reason, px = check_exit(p, 99.0, 100.5, 10)
    assert reason == "time_stop" and px is None


def test_assess_flags_bad_stop():
    plan = build_plan(100.0, 2.0, 0, CFG)
    plan.hard_stop = 105.0  # above price -> invalid
    plan.trail_stop = None
    a = assess(entry_price=100.0, current_price=101.0, plan=plan)
    assert a.status == "MISSING_STOP"
    assert a.issues

    ok = assess(100.0, 101.0, build_plan(100.0, 2.0, 0, CFG))
    assert ok.status == "OK"
    assert ok.stop_distance_pct is not None and ok.stop_distance_pct > 0


def test_assess_no_plan():
    a = assess(100.0, 100.0, None)
    assert a.status == "NO_PLAN"


def test_entry_fee_round_trips():
    from botcore.strategy.exitplan import ExitPlan

    p = build_plan(100.0, 2.0, 0, CFG)
    assert p.entry_fee == 0.0
    p.entry_fee = 1.23
    assert ExitPlan.from_dict(p.as_dict()).entry_fee == pytest.approx(1.23)
    # tolerate an old serialised plan with no entry_fee key
    d = p.as_dict()
    d.pop("entry_fee")
    assert ExitPlan.from_dict(d).entry_fee == 0.0


# --------------------------------------------------------------------------- #
# level-based plans, partial TP1, intraday clock
# --------------------------------------------------------------------------- #
from datetime import datetime, timedelta, timezone

from botcore.strategy.exitplan import ExitPlan, entries_allowed, take_tp1


def _cfg(**kw):
    base = dict(hard_stop_atr_mult=2.0, target_atr_mult=0.0, trail_atr_mult=0.0,
                time_stop_bars=16)
    base.update(kw)
    return ExitCfg(**base)


def _et(hh, mm=0):
    """A UTC instant that lands at hh:mm US/Eastern in September (EDT, UTC-4).

    Built with a timedelta so evening hours roll into the next UTC day instead
    of overflowing the hour field.
    """
    return datetime(2026, 9, 2, 0, 0, tzinfo=timezone.utc) + timedelta(hours=hh + 4, minutes=mm)


def test_explicit_levels_override_atr_multiples():
    cfg = _cfg(tp1_fraction=0.25)
    plan = build_plan(100.0, 1.0, 0, cfg, stop=97.5, target=110.0, tp1=104.0)
    assert plan.hard_stop == 97.5, "the OB wick, not entry - 2*ATR"
    assert plan.target == 110.0
    assert plan.tp1 == 104.0 and plan.tp1_fraction == 0.25


def test_atr_fallback_when_no_levels_given():
    cfg = _cfg()
    plan = build_plan(100.0, 1.0, 0, cfg)
    assert plan.hard_stop == pytest.approx(98.0)
    assert plan.tp1 is None and plan.tp1_fraction == 0.0


def test_nonsense_levels_are_ignored():
    cfg = _cfg(tp1_fraction=0.25)
    # stop above entry and tp1 below entry are both incoherent for a long
    plan = build_plan(100.0, 1.0, 0, cfg, stop=105.0, tp1=95.0)
    assert plan.hard_stop == pytest.approx(98.0), "bad stop falls back to ATR"
    assert plan.tp1 is None and plan.tp1_fraction == 0.0


def test_tp1_fires_once_then_stop_moves_to_breakeven():
    cfg = _cfg(tp1_fraction=0.25, be_after_tp1=True)
    plan = build_plan(100.0, 1.0, 0, cfg, stop=98.0, tp1=103.0)

    hit = check_exit(plan, 99.0, 103.5, 1)
    assert hit == ("tp1", 103.0)

    take_tp1(plan)
    assert plan.tp1_done
    assert plan.hard_stop == pytest.approx(100.0), "breakeven = entry price"

    # it must not fire a second time
    again = check_exit(plan, 100.5, 104.0, 2)
    assert again is None or again[0] != "tp1"


def test_be_after_tp1_false_leaves_the_stop_alone():
    cfg = _cfg(tp1_fraction=0.5, be_after_tp1=False)
    plan = build_plan(100.0, 1.0, 0, cfg, stop=98.0, tp1=103.0)
    take_tp1(plan)
    assert plan.hard_stop == pytest.approx(98.0)


def test_flat_by_close_outranks_every_other_exit():
    cfg = _cfg(flat_by_et="15:55", tp1_fraction=0.25)
    plan = build_plan(100.0, 1.0, 0, cfg, stop=98.0, tp1=103.0)
    # a bar that would trigger the hard stop AND tp1 -- flat_close still wins
    hit = check_exit(plan, 90.0, 110.0, 1, cfg=cfg, now=_et(15, 56))
    assert hit == ("flat_close", None)


def test_before_the_bell_normal_priority_applies():
    cfg = _cfg(flat_by_et="15:55", tp1_fraction=0.25)
    plan = build_plan(100.0, 1.0, 0, cfg, stop=98.0, tp1=103.0)
    assert check_exit(plan, 90.0, 110.0, 1, cfg=cfg, now=_et(11, 0))[0] == "hard_stop"


def test_flat_by_close_is_off_for_24_7_markets():
    cfg = _cfg(flat_by_et=None)
    plan = build_plan(100.0, 1.0, 0, cfg, stop=98.0)
    assert check_exit(plan, 99.0, 100.0, 1, cfg=cfg, now=_et(23, 0)) is None


def test_entry_cutoff_blocks_late_entries_only():
    cfg = _cfg(entry_cutoff_et="15:15")
    assert entries_allowed(cfg, _et(10, 0))
    assert not entries_allowed(cfg, _et(15, 30))
    assert entries_allowed(_cfg(entry_cutoff_et=None), _et(15, 30))
    assert entries_allowed(cfg, None), "no clock supplied -> do not block"


def test_tp1_state_survives_a_round_trip_through_the_db():
    cfg = _cfg(tp1_fraction=0.25)
    plan = build_plan(100.0, 1.0, 0, cfg, stop=98.0, tp1=103.0)
    take_tp1(plan)
    back = ExitPlan.from_dict(plan.as_dict())
    assert back.tp1_done and back.tp1 == 103.0
    assert back.tp1_fraction == 0.25 and back.hard_stop == pytest.approx(100.0)


def test_plans_persisted_before_tp1_existed_still_load():
    legacy = {"entry_price": 100.0, "atr_at_entry": 1.0, "opened_index": 0,
              "hard_stop": 98.0, "target": None, "trail_stop": None,
              "time_stop_index": None, "high_water": 100.0}
    plan = ExitPlan.from_dict(legacy)
    assert plan.tp1 is None and plan.tp1_fraction == 0.0 and not plan.tp1_done
