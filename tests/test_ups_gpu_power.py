"""Guards on the two properties that can damage hardware or lose data.

FAIL-BEFORE: with `min(HARD_MAX, watts)` reduced to `watts`, the ceiling test
fails (600 reaches nvidia-smi). With the `set_limit(BATTERY_WATTS)` call at the
top of `main()` removed, the fail-low test fails (startup leaves the boot
service's limit in place while the UPS may already be on battery).
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parent.parent / "bin" / "ups-gpu-power"


def _load():
    spec = importlib.util.spec_from_loader(
        "ups_gpu_power", loader=None, origin=str(SOURCE)
    )
    module = importlib.util.module_from_spec(spec)
    exec(compile(SOURCE.read_text(), str(SOURCE), "exec"), module.__dict__)
    return module


@pytest.fixture
def mod():
    return _load()


@pytest.fixture
def calls(monkeypatch, mod):
    seen = []

    def fake_run(argv, **kwargs):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    return seen


def _watts(argv):
    return int(argv[argv.index("-pl") + 1])


@pytest.mark.parametrize("asked", [600, 575, 10_000])
def test_ceiling_holds_above_550(mod, calls, asked):
    """600 W killed the card on 2026-09-11; no path may reach it."""
    assert mod.set_limit(asked) is True
    assert _watts(calls[-1]) == mod.HARD_MAX == 550


@pytest.mark.parametrize("asked", [0, 100, -5])
def test_floor_holds_below_the_cards_minimum(mod, calls, asked):
    assert mod.set_limit(asked) is True
    assert _watts(calls[-1]) == mod.HARD_MIN == 150


def test_policy_values_pass_through_unclamped(mod, calls):
    for watts in (mod.MAINS_WATTS, mod.BATTERY_WATTS):
        mod.set_limit(watts)
        assert _watts(calls[-1]) == watts


def test_battery_cap_is_well_below_mains(mod):
    assert mod.BATTERY_WATTS < mod.MAINS_WATTS <= mod.HARD_MAX


def test_startup_fails_low_before_reading_a_present_ups(mod, monkeypatch, calls):
    """The UPS is THERE but has not answered yet -- the cap must already be low.

    This is the case the guard exists for. Patching find_ups to None instead
    would pass through the not-present branch inside the loop and prove
    nothing about startup.
    """
    monkeypatch.setattr(mod, "find_ups", lambda: "/dev/hidraw-test")
    monkeypatch.setattr(mod, "log", lambda *_: None)

    def explode(_path):
        raise KeyboardInterrupt

    monkeypatch.setattr(mod, "serve", explode)
    with pytest.raises(KeyboardInterrupt):
        mod.main()
    assert calls, "nothing was applied before the UPS was read"
    assert _watts(calls[0]) == mod.BATTERY_WATTS


def test_missing_ups_holds_the_battery_cap(mod, monkeypatch, calls):
    """A UPS that is unplugged is not a reason to run at 550 W."""
    monkeypatch.setattr(mod, "find_ups", lambda: None)
    monkeypatch.setattr(mod, "log", lambda *_: None)

    laps = []

    def stop(_):
        laps.append(1)
        if len(laps) >= 3:
            raise KeyboardInterrupt

    monkeypatch.setattr(mod.time, "sleep", stop)
    with pytest.raises(KeyboardInterrupt):
        mod.main()
    assert {_watts(c) for c in calls} == {mod.BATTERY_WATTS}
    # Re-asserted every lap, not just at startup: while the UPS is missing a
    # stray `nvidia-smi -pl 600` from anywhere else must not survive 5 s.
    assert len(calls) >= 1 + len(laps)


def test_failed_nvidia_smi_is_reported_not_swallowed(mod, monkeypatch):
    monkeypatch.setattr(
        mod.subprocess, "run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 9, "", "boom"),
    )
    monkeypatch.setattr(mod, "log", lambda *_: None)
    assert mod.set_limit(550) is False


def test_ups_is_resolved_by_vid_pid_on_this_box(mod):
    """Integration: the real device must be found, not a hardcoded hidrawN."""
    assert mod.find_ups() == "/dev/hidraw4"


def test_psu_wall_draw_is_readable_without_privileges(mod):
    watts = mod._psu_watts()
    assert watts is not None and 50 < watts < 1200


@pytest.mark.parametrize(
    "byte, expected, what",
    [
        (0b0000_0011, True, "mains, charging"),
        (0b0000_0001, True, "mains, charged"),
        (0b0000_0100, False, "battery, discharging"),
        # The discriminating case: Charging set while mains is ABSENT. Reading
        # bit 1 instead of bit 0 inverts the policy here and runs 550 W on
        # battery. Layout is from the device's own report descriptor:
        # report 11 bit0 ACPresent, bit1 Charging, bit2 Discharging.
        (0b0000_0010, False, "no mains but Charging asserted"),
        (0b0000_0110, False, "no mains, charging and discharging both set"),
    ],
)
def test_ac_present_reads_the_acpresent_bit_only(mod, monkeypatch, byte, expected, what):
    monkeypatch.setattr(mod, "feature", lambda fd, rid, size: bytes([rid, byte]))
    assert mod.ac_present(-1) is expected, what


def test_ac_present_is_unknown_when_the_ups_does_not_answer(mod, monkeypatch):
    """None, not False -- the caller must distinguish 'battery' from 'no idea'."""
    monkeypatch.setattr(mod, "feature", lambda fd, rid, size: None)
    assert mod.ac_present(-1) is None


# ---------------------------------------------------------------------------
# serve(): the pushed-report loop. Driven over a real pipe, so select/read and
# the transition logic are exercised rather than assumed.
# ---------------------------------------------------------------------------


def _drive(mod, monkeypatch, calls, frames, initial_ac):
    """Run serve() over a real select()able fd, one HID report per read.

    The pipe supplies genuine readiness so select() is exercised, but reads are
    served one frame at a time: a pipe is a byte stream and would hand back two
    concatenated reports in a single read, which hidraw never does -- it
    returns exactly one report per read. Modelling the stream faithfully would
    test the harness, not the daemon. Frames exhausted -> b"" -> the EOF path.
    """
    import os as _os
    from collections import deque

    read_fd, write_fd = _os.pipe()
    _os.write(write_fd, b"\x00" * len(frames) if frames else b"\x00")
    _os.close(write_fd)
    pending = deque(frames)

    monkeypatch.setattr(mod.os, "open", lambda *a, **k: read_fd)
    monkeypatch.setattr(mod.os, "close", lambda fd: None)
    # Reads past exhaustion return EOF. A daemon that does not treat EOF as a
    # lost connection spins here forever (select reports a closed descriptor
    # readable indefinitely), so the harness turns that hang into a failure
    # rather than letting the suite stall.
    spins = []

    def _read(fd, n):
        if pending:
            return pending.popleft()
        spins.append(1)
        assert len(spins) < 5, "serve() ignored EOF and is spinning on a dead descriptor"
        return b""

    monkeypatch.setattr(mod.os, "read", _read)
    monkeypatch.setattr(mod, "feature", lambda fd, rid, size: bytes([rid, initial_ac]))
    monkeypatch.setattr(mod, "log", lambda *_: None)
    try:
        # AssertionError is an OSError-sibling here only via the spin guard;
        # keep them distinct so a spin reports as a spin.
        with pytest.raises(OSError):
            mod.serve("/dev/hidraw-test")
    finally:
        _os.close(read_fd)
    return [_watts(c) for c in calls]


def test_serve_drops_the_cap_on_a_pushed_transfer_to_battery(mod, monkeypatch, calls):
    """The whole reason this is a daemon: the cut arrives as a pushed report."""
    # Start on mains (bit 0 set), then the UPS pushes ACPresent=0, Discharging=1.
    applied = _drive(mod, monkeypatch, calls, [bytes([11, 0b0000_0100])], initial_ac=0b0000_0001)
    assert applied[0] == mod.MAINS_WATTS, "the initial feature read must settle on mains"
    assert mod.BATTERY_WATTS in applied, "the pushed transfer must drop the cap"
    assert applied[-1] == mod.BATTERY_WATTS


def test_serve_restores_the_cap_when_mains_returns(mod, monkeypatch, calls):
    applied = _drive(
        mod,
        monkeypatch,
        calls,
        [bytes([11, 0b0000_0100]), bytes([11, 0b0000_0011])],
        initial_ac=0b0000_0001,
    )
    assert applied[-1] == mod.MAINS_WATTS


def test_serve_ignores_reports_that_are_not_the_status_report(mod, monkeypatch, calls):
    """Report 8 (capacity) and 19 (load) also arrive; neither may move the cap."""
    applied = _drive(
        mod,
        monkeypatch,
        calls,
        [bytes([8, 55, 0x10, 0x0E]), bytes([19, 42])],
        initial_ac=0b0000_0001,
    )
    assert set(applied) == {mod.MAINS_WATTS}, "only report 11 carries mains state"


def test_serve_does_not_reapply_an_unchanged_state(mod, monkeypatch, calls):
    """A repeated report must not re-run nvidia-smi on every push."""
    same = bytes([11, 0b0000_0011])
    applied = _drive(mod, monkeypatch, calls, [same, same, same], initial_ac=0b0000_0001)
    assert applied == [mod.MAINS_WATTS], "one apply, not four"
