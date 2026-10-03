"""Guards for the properties of power-ledger that can lose an outage or hurt the UPS.

FAIL-BEFORE (each planted in a scratch copy and run through testq; red here, green restored):
- runtime read from data[3:5] ........................ test_pushed_reports_decode
- big-endian instead of little ....................... test_periodic_row_decodes_the_three_reads
- `n >= size + 1` check dropped ...................... test_feature_rejects_a_short_reply
- the two intervals swapped .......................... test_cadence_follows_mains_and_battery
- the three GETs fired on a change row ............... test_change_row_sends_no_feature_reads
- EOF treated as `continue` (a CPU spin) ............. test_eof_ends_serve_instead_of_spinning
- loop left on OSError ............................... test_run_reconnects_and_marks_the_loss
- try/except around the write removed ................ test_a_failed_write_does_not_end_the_loop
- journal_mode=WAL removed ........................... test_writer_commits_while_a_reader_is_open
- immutable=1 added to the reader .................... test_reader_sees_rows_still_in_the_wal
- the UPS node opened O_RDWR ......................... test_source_never_writes_to_the_ups
- marks ignored by the outage derivation ............. test_the_0930_shutdown_is_not_a_5731_second_outage
- gap rule dropped ................................... test_a_hole_inside_an_outage_closes_it
- first row treated as the exact start ............... test_start_after_a_restart_is_unknown
- hole before the first battery row ignored .......... test_start_after_a_hole_is_unknown
- /3600 replaced by /60 .............................. test_watt_hours_are_trapezoids
- ts made a key with INSERT OR REPLACE ............... test_a_backward_clock_step_keeps_history
- `age > STALE_SECONDS` inverted ..................... test_status_exit_codes
- gap exclusion dropped from the energy sum .......... test_energy_excludes_holes
- rate limit on other-bit changes removed ............ test_other_bit_changes_are_rate_limited
"""

from __future__ import annotations

import calendar
import errno
import os
import signal
import socket
import sqlite3
import time
import types
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parent.parent / "bin" / "power-ledger"


def _load():
    import importlib.util

    spec = importlib.util.spec_from_loader("power_ledger", loader=None, origin=str(SOURCE))
    module = importlib.util.module_from_spec(spec)
    exec(compile(SOURCE.read_text(), str(SOURCE), "exec"), module.__dict__)
    return module


@pytest.fixture
def mod():
    return _load()


class Hung(Exception):
    """A test ran far longer than any of them should: a spin, not a slow machine."""


@pytest.fixture(autouse=True)
def hang_guard():
    """serve() loops until the device goes away, so a defect that stops it noticing
    EOF would hang the whole run. Fail that test instead."""

    def alarm(*_):
        raise Hung

    old = signal.signal(signal.SIGALRM, alarm)
    signal.setitimer(signal.ITIMER_REAL, 30)
    yield
    signal.setitimer(signal.ITIMER_REAL, 0)
    signal.signal(signal.SIGALRM, old)


@pytest.fixture
def utc():
    old = os.environ.get("TZ")
    os.environ["TZ"] = "UTC"
    time.tzset()
    yield
    if old is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old
    time.tzset()


def ut(y, mo, d, h=0, mi=0, s=0):
    return calendar.timegm((y, mo, d, h, mi, s))


# ------------------------------------------------------------ fake UPS device

#: Payloads read off the real unit on 2026-10-03 (evidence/hid_feature_dump.txt), after the report id.
REPORTS = {
    0x0B: bytes.fromhex("13"),
    0x08: bytes.fromhex("64 58 02 2c 01"),  # 100 %, 600 s, limit 300 s
    0x13: bytes.fromhex("36"),  # 54 %
    0x0F: bytes.fromhex("1f 09"),  # 2335 -> 233.5 V
    0x0A: bytes.fromhex("20 02"),  # 544 -> 54.4 V
}


class FakeFcntl:
    """Stands in for the fcntl module: answers GET_FEATURE like hidraw, records each request."""

    def __init__(self, reports=None):
        self.reports = dict(REPORTS if reports is None else reports)
        self.requests: list[int] = []
        self.ids: list[int] = []

    def ioctl(self, fd, request, buf, mutate=True):
        self.requests.append(request)
        self.ids.append(buf[0])
        payload = self.reports.get(buf[0])
        if payload is None:
            raise OSError(errno.EIO, "no such report")
        size = len(buf) - 1
        if len(payload) > size:
            raise OSError(errno.EOVERFLOW, "Value too large for defined data type")
        buf[1 : 1 + len(payload)] = payload
        return len(payload) + 1


class Gpu:
    def read(self):
        return 151, 151


@pytest.fixture
def device(mod, monkeypatch):
    """A fake hidraw node: a SEQPACKET socket pair keeps one report per read."""
    ups, test_end = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    ups.setblocking(False)
    fcntl = FakeFcntl()
    fd = ups.detach()  # serve() owns and closes this descriptor
    monkeypatch.setattr(mod, "_open", lambda path: fd)
    monkeypatch.setattr(mod, "fcntl", fcntl)
    monkeypatch.setattr(mod, "psu_watts", lambda: 476)
    yield types.SimpleNamespace(peer=test_end, fcntl=fcntl)
    test_end.close()


def push(device, *reports):
    for report in reports:
        device.peer.send(bytes.fromhex(report))


def rows_of(path, where=""):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in db.execute(f"SELECT * FROM samples {where} ORDER BY rowid")]
    finally:
        db.close()


def marks_of(path):
    db = sqlite3.connect(path)
    try:
        return [w for (w,) in db.execute("SELECT what FROM marks ORDER BY rowid")]
    finally:
        db.close()


# ------------------------------------------------------------ finding the devices


UPS_UEVENT = "DRIVER=hid-generic\nHID_ID=0003:00000764:00000601\nHID_NAME=CyberPower Systems OLS2000EC\n"
OTHER_UEVENT = "DRIVER=hid-generic\nHID_ID=0003:0000046D:0000C548\nHID_NAME=Logitech Bolt\n"


def fake_sysfs(monkeypatch, mod, tree):
    """Serve `tree` ({path: text}) to open() and os.listdir() inside the module."""
    import io

    real_listdir = os.listdir

    def listdir(path):
        prefix = str(path).rstrip("/") + "/"
        names = {p[len(prefix):].split("/", 1)[0] for p in tree if p.startswith(prefix)}
        if names:
            return sorted(names)
        if str(path).startswith("/sys/"):
            raise FileNotFoundError(path)
        return real_listdir(path)

    def fake_open(path, *args, **kwargs):
        if path in tree:
            return io.StringIO(tree[path])
        raise FileNotFoundError(path)

    monkeypatch.setattr(mod.os, "listdir", listdir)
    monkeypatch.setitem(mod.__dict__, "open", fake_open)


def test_ups_is_found_by_vid_pid_not_by_position(mod, monkeypatch):
    fake_sysfs(monkeypatch, mod, {
        "/sys/class/hidraw/hidraw0/device/uevent": OTHER_UEVENT,
        "/sys/class/hidraw/hidraw3/device/uevent": UPS_UEVENT,
    })
    assert mod.find_ups() == "/dev/hidraw3"  # the node number moves; the first node is not the UPS here
    fake_sysfs(monkeypatch, mod, {"/sys/class/hidraw/hidraw0/device/uevent": OTHER_UEVENT})
    assert mod.find_ups() is None
    fake_sysfs(monkeypatch, mod, {})
    assert mod.find_ups() is None


def test_psu_is_found_by_name_not_by_index(mod, monkeypatch):
    fake_sysfs(monkeypatch, mod, {
        "/sys/class/hwmon/hwmon0/name": "nvme\n",
        "/sys/class/hwmon/hwmon0/power1_input": "5000000\n",  # another device that also exports power1
        "/sys/class/hwmon/hwmon5/name": "corsairpsu\n",
        "/sys/class/hwmon/hwmon5/power1_input": "476400000\n",
    })
    assert mod.psu_watts() == 476
    fake_sysfs(monkeypatch, mod, {"/sys/class/hwmon/hwmon0/name": "nvme\n"})
    assert mod.psu_watts() is None


def test_psu_scan_survives_a_hwmon_without_a_name(mod, monkeypatch):
    fake_sysfs(monkeypatch, mod, {
        "/sys/class/hwmon/hwmon0/temp1_input": "40000\n",  # no `name` file at all
        "/sys/class/hwmon/hwmon1/name": "corsairpsu\n",
        "/sys/class/hwmon/hwmon1/power1_input": "476000000\n",
    })
    assert mod.psu_watts() == 476


# ------------------------------------------------------------ decoding


def test_pushed_reports_decode(mod):
    led = mod.Ledger()
    assert led.on_report(bytes.fromhex("0b 13"), 0.0) is False
    assert (led.f0b, led.row(0, 5)["mains"]) == (0x13, 1)
    led.on_report(bytes.fromhex("08 64 58 02 2c 01"), 1.0)
    assert (led.charge, led.runtime) == (100, 600)
    led.on_report(bytes.fromhex("08 64 2c 01 2c 01"), 2.0)
    assert led.runtime == 300  # the limit field that follows is not the runtime
    before = (led.f0b, led.charge, led.runtime)
    assert led.on_report(bytes.fromhex("2c 00"), 3.0) is False  # an unknown report id
    assert led.on_report(bytes.fromhex("08 64"), 3.0) is False  # a truncated battery report
    assert (led.f0b, led.charge, led.runtime) == before


def test_periodic_row_decodes_the_three_reads(mod, device, tmp_path):
    push(device)
    device.peer.close()
    store = mod.Store(str(tmp_path / "l.db"))
    with pytest.raises(OSError):
        mod.serve("/dev/test", store, Gpu())
    (first,) = rows_of(tmp_path / "l.db")
    assert (first["load"], first["vin"], first["vbat"]) == (54, 2335, 544)
    assert (first["charge"], first["runtime"], first["mains"], first["f0b"]) == (100, 600, 1, 0x13)
    assert (first["psu_w"], first["gpu_w"], first["gpu_limit_w"], first["kind"]) == (476, 151, 151, 0)


def test_feature_rejects_a_short_reply(mod, monkeypatch):
    class Short(FakeFcntl):
        def ioctl(self, fd, request, buf, mutate=True):
            super().ioctl(fd, request, buf, mutate)
            return len(buf) - 1  # the device sent one byte fewer than asked

    monkeypatch.setattr(mod, "fcntl", FakeFcntl())
    assert mod.feature(0, 0x13, 1) == bytes.fromhex("36")
    monkeypatch.setattr(mod, "fcntl", Short())
    assert mod.feature(0, 0x13, 1) is None
    monkeypatch.setattr(mod, "fcntl", FakeFcntl())
    assert mod.feature(0, 0x08, 3) is None  # EOVERFLOW: asked for fewer bytes than the report has


# ------------------------------------------------------------ cadence and glue


def test_cadence_follows_mains_and_battery(mod):
    led = mod.Ledger()
    assert led.interval() == 5.0  # state unknown: the dense cadence
    led.on_report(bytes.fromhex("0b 13"), 100.0)
    assert led.interval() == 60.0
    led.periodic_done(100.0)
    assert led.deadline == 160.0 and not led.due(159.9) and led.due(160.0)
    assert led.on_report(bytes.fromhex("0b 12"), 110.0) is True  # bit 0 fell: battery
    assert led.interval() == 5.0
    assert led.deadline == 115.0  # pulled forward from 160


def test_other_bit_changes_are_rate_limited(mod):
    led = mod.Ledger()
    led.on_report(bytes.fromhex("0b 13"), 0.0)
    assert led.on_report(bytes.fromhex("0b 11"), 10.0) is True  # bit 1 moved
    assert led.on_report(bytes.fromhex("0b 13"), 20.0) is False  # moved back 10 s later: no row
    assert led.on_report(bytes.fromhex("0b 11"), 71.0) is True  # a minute on
    assert led.on_report(bytes.fromhex("0b 10"), 72.0) is True  # mains lost: always, rate limit or not


def test_change_row_sends_no_feature_reads(mod, device, tmp_path):
    push(device, "0b 13", "0b 12")  # a repeat, then bit 0 falling
    device.peer.close()
    store = mod.Store(str(tmp_path / "l.db"))
    with pytest.raises(OSError):
        mod.serve("/dev/test", store, Gpu())
    first, change = rows_of(tmp_path / "l.db")
    assert (first["kind"], change["kind"]) == (0, 1)
    assert (change["mains"], change["f0b"], change["charge"], change["runtime"]) == (0, 0x12, 100, 600)
    assert (change["load"], change["vin"], change["vbat"]) == (None, None, None)
    assert (change["psu_w"], change["gpu_w"]) == (476, 151)
    # the seed (0x0b, 0x08) and the one periodic row (0x13, 0x0f, 0x0a); nothing for the change row
    assert device.fcntl.ids == [0x0B, 0x08, 0x13, 0x0F, 0x0A]


def test_eof_ends_serve_instead_of_spinning(mod, device, tmp_path):
    class Spun(Exception):
        pass

    def alarm(*_):
        raise Spun

    device.peer.close()
    store = mod.Store(str(tmp_path / "l.db"))
    old = signal.signal(signal.SIGALRM, alarm)
    signal.setitimer(signal.ITIMER_REAL, 3)
    try:
        with pytest.raises(OSError, match="closed"):
            mod.serve("/dev/test", store, Gpu())
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)


def _run_with(mod, monkeypatch, tmp_path, serves, finds):
    """Drive run() with scripted find_ups/serve; stops when `serves` is exhausted."""
    sleeps = []
    fake_time = types.SimpleNamespace(time=time.time, monotonic=time.monotonic, sleep=sleeps.append)
    fake_signal = types.SimpleNamespace(signal=lambda *a: None, SIGTERM=15, SIGINT=2)
    monkeypatch.setattr(mod, "time", fake_time)
    monkeypatch.setattr(mod, "signal", fake_signal)
    finds = iter(finds)
    serves = iter(serves)
    monkeypatch.setattr(mod, "find_ups", lambda: next(finds))
    monkeypatch.setattr(mod, "serve", lambda path, store, nvml: next(serves)(path, store))
    db = str(tmp_path / "l.db")
    with pytest.raises(SystemExit):
        mod.run(db)
    return db, sleeps


def test_run_reconnects_and_marks_the_loss(mod, monkeypatch, tmp_path):
    def first(path, store):
        store.safe(store.mark, f"ups-found {os.path.basename(path)}")
        raise OSError("boom")

    def second(path, store):
        store.safe(store.mark, f"ups-found {os.path.basename(path)}")
        raise SystemExit(0)

    db, sleeps = _run_with(mod, monkeypatch, tmp_path, [first, second], ["/dev/hidraw4", None, "/dev/hidraw0"])
    marks = marks_of(db)
    assert marks[1:] == ["ups-found hidraw4", "ups-lost boom", "ups-found hidraw0", "stop"]
    assert sleeps == [mod.RECONNECT_DELAY, mod.RECONNECT_DELAY]  # after the loss, and while it was gone


def test_the_start_mark_carries_the_boot_id(mod, monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "boot_id", lambda: "abc-123")
    db, _ = _run_with(mod, monkeypatch, tmp_path, [lambda p, s: (_ for _ in ()).throw(SystemExit(0))], ["/dev/hidraw0"])
    assert marks_of(db)[0].startswith("start boot=abc-123 ")
    assert marks_of(db)[-1] == "stop"


def test_a_failed_write_does_not_end_the_loop(mod, device, tmp_path):
    push(device, "0b 12")
    device.peer.close()
    store = mod.Store(str(tmp_path / "l.db"))
    real_add, calls = store.add, []

    def flaky(row):
        calls.append(row["kind"])
        if len(calls) == 1:
            raise sqlite3.OperationalError("database or disk is full")
        real_add(row)

    store.add = flaky
    with pytest.raises(OSError):  # the EOF at the end, not the sqlite error
        mod.serve("/dev/test", store, Gpu())
    assert calls == [0, 1]
    assert store.dropped == 1
    (only,) = rows_of(tmp_path / "l.db")
    assert only["kind"] == 1  # the periodic row was dropped, the change row after it was written


def test_nvml_absence_is_null_not_a_crash(mod, monkeypatch, capsys):
    def missing(name):
        raise OSError("libnvidia-ml.so.1: cannot open shared object file")

    monkeypatch.setattr(mod.ctypes, "CDLL", missing)
    gpu = mod.Nvml()
    assert gpu.read() == (None, None)
    assert gpu.read() == (None, None)
    assert capsys.readouterr().out.count("NVML unavailable") == 1  # logged on the change, not every row


# ------------------------------------------------------------ the store


def test_writer_commits_while_a_reader_is_open(mod, tmp_path):
    path = str(tmp_path / "l.db")
    store = mod.Store(path)
    store.db.execute("PRAGMA busy_timeout=1000")
    store.add({"ts": 1, "kind": 0})
    reader = sqlite3.connect(path)
    reader.execute("BEGIN")
    reader.execute("SELECT count(*) FROM samples").fetchall()  # holds a read transaction open
    store.add({"ts": 2, "kind": 0})  # in a rollback journal this waits for the reader and fails
    reader.close()
    assert store.db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert store.db.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL
    assert store.db.execute("PRAGMA user_version").fetchone()[0] == 1
    assert len(rows_of(path)) == 2


def test_reader_sees_rows_still_in_the_wal(mod, tmp_path):
    path = str(tmp_path / "l.db")
    store = mod.Store(path)
    store.db.execute("PRAGMA wal_autocheckpoint=0")
    for ts in range(5):
        store.add({"ts": ts, "kind": 0})
    assert os.path.getsize(path + "-wal") > 0  # the rows are in the log, not the main file
    db = mod.open_ro(path)
    assert db.execute("SELECT count(*) FROM samples").fetchone()[0] == 5
    db.close()


def test_a_backward_clock_step_keeps_history(mod, tmp_path):
    store = mod.Store(str(tmp_path / "l.db"))
    for ts in (1000, 1060, 1000):
        store.add({"ts": ts, "kind": 0})
    assert [r["ts"] for r in rows_of(tmp_path / "l.db")] == [1000, 1060, 1000]


def test_the_store_refuses_a_schema_it_does_not_know(mod, tmp_path):
    path = str(tmp_path / "l.db")
    mod.Store(path)
    db = sqlite3.connect(path)
    db.execute("PRAGMA user_version=2")
    db.close()
    with pytest.raises(SystemExit, match="schema version 2"):
        mod.Store(path)


# ------------------------------------------------------------ the UPS is never written


def test_source_never_writes_to_the_ups(mod):
    text = SOURCE.read_text()
    for forbidden in ("HIDIOCSFEATURE", "O_RDWR", "O_WRONLY", "os.write("):
        assert forbidden not in text, forbidden
    # the one request the file builds is GET_FEATURE: nr 0x07, direction read|write
    for size in (2, 3, 6):
        request = mod._hidiocgfeature(size)
        assert request & 0xFF == 0x07 and request >> 30 == 3
    assert "O_RDONLY" in text


def test_every_request_the_service_makes_is_a_get(mod, device, tmp_path):
    push(device, "0b 13", "0b 12")
    device.peer.close()
    store = mod.Store(str(tmp_path / "l.db"))
    with pytest.raises(OSError):
        mod.serve("/dev/test", store, Gpu())
    assert device.fcntl.requests, "the service made no request at all"
    assert all(r & 0xFF == 0x07 for r in device.fcntl.requests)


# ------------------------------------------------------------ outages


def R(ts, mains, charge=100, runtime=600, load=20, psu=300, _n=[0]):
    _n[0] += 1
    return (ts, _n[0], mains, charge, runtime, load, psu)


NOW = ut(2027, 1, 1)


def test_an_outage_with_exact_ends(mod, utc):
    t = ut(2026, 10, 3, 16, 29, 27)
    rows = [R(t - 60, 1), R(t, 0, load=19), R(t + 40, 1, charge=98)]
    (ep,) = mod.derive_outages(rows, [], NOW)
    assert (ep["start"], ep["end"], ep["duration"], ep["exact"]) == (t, t + 40, 40, True)
    assert (ep["charge_start"], ep["charge_end"], ep["max_load"]) == (100, 98, 19)


def test_a_four_second_outage_is_four_seconds(mod, utc):
    t = ut(2026, 9, 28, 11, 29, 23)
    (ep,) = mod.derive_outages([R(t - 60, 1), R(t, 0), R(t + 4, 1)], [], NOW)
    assert (ep["duration"], ep["exact"]) == (4, True)


def test_the_0930_shutdown_is_not_a_5731_second_outage(mod, utc):
    t = ut(2026, 9, 30, 22, 54, 28)
    rows = [R(t - 30, 1)] + [R(x, 0, charge=100 if x < t + 100 else 99, runtime=2460, load=11, psu=220) for x in range(t, t + 113, 4)]
    rows.append(R(ut(2026, 10, 1, 0, 29, 59), 1, charge=87))
    marks = [(t + 116, "stop"), (ut(2026, 10, 1, 0, 29, 53), "start boot=x pid=1 schema=1")]
    (ep,) = mod.derive_outages(rows, marks, NOW)
    assert ep["end"] is None and ep["end_note"] == "logger stop 22:56:24"
    assert (ep["duration"], ep["exact"]) == (112, False)
    line = mod.format_outage(ep)
    assert ">=112s" in line and "end unknown (logger stop 22:56:24)" in line and "5731" not in line
    assert "charge 100->99%" in line and "min rt 2460s" in line and "peak load 11%" in line


def test_a_hole_inside_an_outage_closes_it(mod, utc):
    t = ut(2026, 9, 21, 11, 13, 48)
    rows = [R(t - 60, 1), R(t, 0), R(t + 20, 0), R(t + 40, 0), R(t + 60, 0), R(t + 460, 0), R(t + 500, 1)]
    first, second = mod.derive_outages(rows, [], NOW)
    assert first["end"] is None and first["end_note"].startswith("gap from 11:14:48")
    assert second["start_known"] is False  # the hole hides when it began
    assert second["end"] == t + 500


def test_an_outage_still_running_is_ongoing(mod, utc):
    t = ut(2026, 10, 4, 3, 0, 0)
    rows = [R(t - 60, 1), R(t, 0), R(t + 5, 0), R(t + 10, 0)]
    (ongoing,) = mod.derive_outages(rows, [], t + 20)
    assert ongoing["end_note"] == "ongoing" and ongoing["start_known"]
    (silent,) = mod.derive_outages(rows, [], t + 10_000)
    assert silent["end_note"] == "no rows since 03:00:10"


def test_start_after_a_restart_is_unknown(mod, utc):
    t = ut(2026, 10, 4, 3, 0, 0)
    rows = [R(t + 5, 0), R(t + 10, 0), R(t + 30, 1)]
    (ep,) = mod.derive_outages(rows, [(t, "start boot=x pid=1 schema=1")], NOW)
    assert ep["start_known"] is False
    assert "start unknown (first row 03:00:05)" in mod.format_outage(ep)


def test_start_after_a_hole_is_unknown(mod, utc):
    t = ut(2026, 10, 4, 3, 0, 0)
    rows = [R(t - 3000, 1), R(t, 0), R(t + 5, 0), R(t + 30, 1)]  # a hole, with no mark to explain it
    (ep,) = mod.derive_outages(rows, [], NOW)
    assert ep["start_known"] is False


def test_watt_hours_are_trapezoids(mod, utc):
    t = ut(2026, 10, 4, 3, 0, 0)
    (ep,) = mod.derive_outages([R(t - 60, 1), R(t, 0, psu=600), R(t + 30, 0, psu=600), R(t + 60, 1, psu=600)], [], NOW)
    assert ep["wh"] == pytest.approx(10.0) and ep["partial"] is False  # 600 W for 60 s
    rows = [R(t - 60, 1), R(t, 0, psu=600), R(t + 20, 0, psu=600), R(t + 40, 0, psu=None), R(t + 60, 1, psu=600)]
    (ep,) = mod.derive_outages(rows, [], NOW)
    assert ep["partial"] is True and ep["wh"] == pytest.approx(600 * 20 / 3600)
    assert "(PSU DC) partial" in mod.format_outage(ep)


# ------------------------------------------------------------ energy


def test_energy_is_a_trapezoid_over_the_day(mod, utc):
    t = ut(2026, 10, 3, 6, 0, 0)
    rows = [(t + 60 * i, 600) for i in range(61)]
    (day,) = mod.energy_by_day(rows, t, t + 3600)
    assert day[0] == "2026-10-03"
    assert day[1] == pytest.approx(600.0)  # 600 W for an hour is 0.600 kWh
    assert day[2] == pytest.approx(1.0)


def test_energy_excludes_holes(mod, utc):
    t = ut(2026, 10, 3, 6, 0, 0)
    rows = [(t + 60 * i, 600) for i in range(31)] + [(t + 1800 + 400 + 60 * i, 600) for i in range(31)]
    (day,) = mod.energy_by_day(rows, t, t + 4000)
    assert day[1] == pytest.approx(600.0)  # two half hours; the 400 s hole between them is not counted


# ------------------------------------------------------------ the CLI


def _db_with(mod, path, *ages):
    store = mod.Store(str(path))
    now = int(time.time())
    for age in ages:
        store.add({"ts": now - age, "kind": 0, "mains": 1, "f0b": 0x13, "charge": 100, "runtime": 600, "load": 20})
    return store


def _code(mod, argv):
    """The exit code of a CLI call, whether main() returns it or exits with it."""
    try:
        return mod.main(argv)
    except SystemExit as exc:
        return exc.code


def test_status_exit_codes(mod, tmp_path, capsys):
    assert _code(mod, ["status", "--db", str(tmp_path / "missing.db")]) == 1
    _db_with(mod, tmp_path / "empty.db")
    assert _code(mod, ["status", "--db", str(tmp_path / "empty.db")]) == 1
    _db_with(mod, tmp_path / "fresh.db", 10)
    assert _code(mod, ["status", "--db", str(tmp_path / "fresh.db")]) == 0
    _db_with(mod, tmp_path / "old.db", 400)
    assert _code(mod, ["status", "--db", str(tmp_path / "old.db")]) == 2
    assert "STALE" in capsys.readouterr().out


def test_tail_prints_oldest_first_with_dashes_for_null(mod, tmp_path, capsys, utc):
    store = mod.Store(str(tmp_path / "l.db"))
    store.add({"ts": 1_790_000_000, "kind": 0, "mains": 1, "f0b": 0x13, "charge": 100, "runtime": 600, "load": 54,
               "vin": 2335, "vbat": 544, "psu_w": 476, "gpu_w": 151, "gpu_limit_w": 151})
    store.add({"ts": 1_790_000_060, "kind": 1, "mains": 0, "f0b": 0x12, "charge": 100, "runtime": 600, "psu_w": 480})
    assert mod.main(["tail", "-n", "5", "--db", str(tmp_path / "l.db")]) == 0
    first, second = capsys.readouterr().out.splitlines()
    assert " P mains f0b=0x13 100% rt=600s load=54% vin=233.5V vbat=54.4V psu=476W gpu=151/151W" in first
    assert " C BATT f0b=0x12 100% rt=600s load=- vin=- vbat=- psu=480W gpu=-/-W" in second
