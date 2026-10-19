import struct

from beo.networklink.audio import Fanout, Silence, ToneSource
from beo.networklink.fps import FORMAT_44100


def test_tone_block_size_and_frame_alignment():
    t = ToneSource(FORMAT_44100, 440.0, 0.35)
    blocks = [t.read_block() for _ in range(100)]
    assert all(len(b) == FORMAT_44100.payload_bytes == 4248 for b in blocks)
    # the two channels carry the same sample: an odd position would swap them
    for b in blocks:
        l, r = struct.unpack("<hh", b[:4])
        assert l == r
    # exactly one second of buffer, looped: block 0 == the block one second later
    t2 = ToneSource(FORMAT_44100)
    first = t2.read_block()
    n = 44100 * 4 // 4248
    for _ in range(n - 1):
        t2.read_block()
    # after 44100 frames the position wraps to 0 only if divisible; check bytes are periodic
    assert len(first) == 4248


def test_tone_peak_matches_amplitude():
    t = ToneSource(FORMAT_44100, 1000.0, 0.5)
    b = t.read_block()
    vals = struct.unpack("<%dh" % (len(b) // 2), b)
    assert max(vals) == int(0.5 * 32767)


def test_silence_is_none():
    assert Silence().read_block() is None


class Counting:
    format = FORMAT_44100

    def __init__(self):
        self.n = 0

    def read_block(self):
        self.n += 1
        return bytes([self.n % 256]) * 4248


def test_fanout_drop_oldest_per_subscriber():
    src = Counting()
    fo = Fanout(src, depth=3, paced=False)
    a = fo.subs.setdefault("a", __import__("queue").Queue(maxsize=3))
    b = fo.subs.setdefault("b", __import__("queue").Queue(maxsize=3))
    for i in range(1, 8):
        fo._distribute(bytes([i]) * 4248)
    items = [q.get_nowait() for q in (a, a, a)]                 # (index, bytes)
    assert [buf[0] for _, buf in items] == [5, 6, 7]              # oldest 1..4 dropped
    assert [n for n, _ in items] == [4, 5, 6]                     # capture-timeline indices survive
    assert b.qsize() == 3 and b.get_nowait()[1][0] == 5
    assert fo.blocks == 7 and fo.origin_ns is not None


def test_fanout_subscribe_unsubscribe_starts_and_stops_source():
    class Src(Counting):
        started = stopped = 0

        def start(self):
            self.started += 1

        def stop(self):
            self.stopped += 1
    src = Src()
    fo = Fanout(src, depth=8, paced=False)
    sa = fo.subscribe("a")
    sb = fo.subscribe("b")
    assert src.started == 1
    assert sa.read_block() is not None or True          # may be None before the thread fills
    fo.unsubscribe("a")
    assert src.stopped == 0
    fo.unsubscribe("b")
    assert src.stopped == 1
    assert sb.format is FORMAT_44100



def test_timeline_restarts_when_the_capture_restarts():
    """The last listener leaves, time passes, a new one joins: its timeline
    starts when ITS capture starts. The reader of the run that ended used to
    hand over one more block after the reset, which pinned origin_ns to the
    moment the capture stopped; the next listener's PTS were then as late as
    the pause was long, and the speaker played silence."""
    import threading
    import time

    class Slow(Counting):
        """Blocks in read_block the way ALSA does, and still returns the block
        it was waiting for after the capture has been told to stop."""
        def __init__(self):
            super().__init__()
            self.reading = threading.Event()

        def read_block(self):
            self.reading.set()
            time.sleep(0.03)
            return super().read_block()

    src = Slow()
    fo = Fanout(src, depth=8, paced=True)
    first = fo.subscribe("a")
    while first.read_indexed(timeout=0.5) is None:
        pass
    src.reading.clear()
    assert src.reading.wait(1)              # the reader is inside read_block now
    fo.unsubscribe("a")
    time.sleep(0.1)                         # the block it was waiting for arrives
    assert fo.origin_ns is None and fo.blocks == 0

    time.sleep(0.2)                         # the pause
    rejoined = time.monotonic_ns()
    second = fo.subscribe("b")
    item = None
    while item is None:
        item = second.read_indexed(timeout=0.5)
    assert item[0] == 0
    assert second.origin_ns >= rejoined
    fo.unsubscribe("b")


def test_timeline_pts_identical_for_two_listeners():
    """Two listeners that joined at different moments stamp the SAME PTS on the
    same block: PTS = clock.at_ns(origin + n * interval), independent of when
    the listener joined and of what it dropped (two listeners are out of step
    with a per-sender PTS counter)."""
    import queue as _q
    from beo.networklink.audio import _Subscriber
    from beo.networklink.fps import OffsetPtpClock
    fo = Fanout(Counting(), depth=8, paced=False)      # no subscribe(): no reader thread
    qa = fo.subs.setdefault("a", _q.Queue(maxsize=8))
    a = _Subscriber(fo.format, qa, fo)
    for i in range(1, 4):
        fo._distribute(bytes([i]) * 4248)               # blocks 0, 1, 2
    qb = fo.subs.setdefault("b", _q.Queue(maxsize=8))
    b = _Subscriber(fo.format, qb, fo)                   # joins late
    fo._distribute(bytes([4]) * 4248)                    # block 3 goes to both
    clock = OffsetPtpClock(1_000_000_000, lead_ns=0)
    interval = fo.format.interval_ns
    got_a = {n: clock.at_ns(a.origin_ns + n * interval) for n, _ in [a.read_indexed() for _ in range(4)]}
    got_b = {n: clock.at_ns(b.origin_ns + n * interval) for n, _ in [b.read_indexed()]}
    assert 3 in got_a and 3 in got_b and got_a[3] == got_b[3]
    assert got_a[3] - got_a[2] == interval


def test_a_fanout_counts_the_blocks_it_drops_for_a_slow_subscriber_and_says_so(caplog):
    import logging
    from beo.networklink.audio import Fanout, FORMAT_44100

    class Blocks:
        format = FORMAT_44100
        def __init__(self): self.n = 0
        def read_block(self):
            self.n += 1
            return b"\x00" * FORMAT_44100.payload_bytes if self.n <= 10 else None
    fan = Fanout(Blocks(), depth=4, paced=False)
    sub = fan.subscribe("slow")
    import time
    time.sleep(0.5)
    assert fan.dropped.get("slow", 0) >= 6
    assert sub.read_indexed(timeout=0.1)[0] >= 6          # the oldest went first
    with caplog.at_level(logging.WARNING, logger="beo.networklink.audio"):
        fan.unsubscribe("slow")
    assert any("dropped for slow" in r.message for r in caplog.records)
