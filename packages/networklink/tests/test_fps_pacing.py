"""Invariant 9: PTS = clock.now_ns() at start, then exactly frames*1e9/rate
per datagram over 10 000 datagrams — no drift, no re-sampling of the clock.
Invariant 10: marker counter 2 at start, +1 per mute/unmute, one sequence
number of the audio series consumed per marker."""
import struct

from beo.networklink.fps import FORMAT_44100, FpsSender, MAGIC


class FakeClock:
    def __init__(self, t):
        self.t = t
        self.calls = 0

    def now_ns(self):
        self.calls += 1
        return self.t


class FakeTime:
    """perf_counter that only moves when the sender waits."""

    def __init__(self):
        self.t = 1000.0

    def now(self):
        return self.t

    def wait_until(self, target):
        if target > self.t:
            self.t = target


class FakeUdp:
    def __init__(self, sender, limit):
        self.sender, self.limit = sender, limit
        self.sent = []

    def sendto(self, data, addr):
        self.sent.append(data)
        if len([d for d in self.sent if len(d) > 16]) >= self.limit:
            self.sender._running = False


def _run(n, clock_t=5_000_000_000_000):
    clock = FakeClock(clock_t)
    s = FpsSender("127.0.0.1", FORMAT_44100, clock, stream_start_ns=300_000_000)
    ft = FakeTime()
    s._now, s._wait_until = ft.now, ft.wait_until
    s.device_ns = clock.now_ns()            # what start() does before opening sockets
    udp = FakeUdp(s, n)
    s._udp = udp
    s._running = True
    s._source = lambda: b"\x01" * FORMAT_44100.payload_bytes
    s._pump()
    return s, udp.sent, clock


def test_pts_advances_exactly_one_interval_per_datagram():
    n = 10_000
    s, sent, clock = _run(n)
    audio = [d for d in sent if len(d) == 32 + FORMAT_44100.payload_bytes]
    assert len(audio) == n
    iv = FORMAT_44100.interval_ns
    for i, d in enumerate(audio):
        magic, seq = struct.unpack(">HH", d[:4])
        assert magic == MAGIC
        dev = struct.unpack(">Q", d[8:16])[0]
        stm = struct.unpack(">q", d[24:32])[0]
        assert dev == 5_000_000_000_000 + i * iv, i
        assert stm == 300_000_000 + i * iv, i
    # the clock is sampled once, never re-read while pacing
    assert clock.calls == 1
    assert s.stats().resets == 0


def test_sequence_and_marker_after_first_datagram():
    s, sent, _ = _run(5)
    assert len(sent[1]) == 16                    # the marker is datagram #2
    assert sent[1].hex() == "12340001000202000000000000000000"
    seqs = [struct.unpack(">H", d[2:4])[0] for d in sent]
    assert seqs == [0, 1, 2, 3, 4, 5]            # marker consumed sequence 1
    assert s.stats().markers_sent == 1


def test_mute_unmute_marker_counter():
    s, sent, _ = _run(3)
    s._udp = FakeUdp(s, 999)
    s.mute()
    s.unmute()
    m = s._udp.sent
    assert [len(d) for d in m] == [16, 16]
    assert struct.unpack(">H", m[0][4:6])[0] == 3   # pause
    assert struct.unpack(">H", m[1][4:6])[0] == 4   # play
    assert m[0][6:8] == b"\x02\x00" and m[1][8:] == b"\x00" * 8
    assert s.stats().markers_sent == 3


def test_short_payload_is_padded_and_none_is_silence():
    clock = FakeClock(0)
    s = FpsSender("127.0.0.1", FORMAT_44100, clock)
    ft = FakeTime()
    s._now, s._wait_until = ft.now, ft.wait_until
    blocks = iter([b"\x07" * 10, None])
    s._source = lambda: next(blocks)
    udp = FakeUdp(s, 2)
    s._udp, s._running = udp, True
    s._pump()
    audio = [d for d in udp.sent if len(d) > 16]
    assert audio[0][32:42] == b"\x07" * 10 and audio[0][42:] == b"\x00" * (4248 - 10)
    assert audio[1][32:] == b"\x00" * 4248
