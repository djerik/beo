"""FPS — B&O's NetworkLink audio transport. Sender and test receiver.

The 32-byte header:

  offset  0..1   0x1234           magic (same value as the port)
  offset  2..3   sequence         uint16, +1 per datagram, wraps at 65536
  offset  4..7   see below        NOT a constant
  offset  8..15  uint64 ns        DEVICE CLOCK / PTS. +24_081_632 ns per
                                  datagram at 44.1 kHz (22_125_000 at 48 kHz).
  offset 16..23  0xffff…ffff      constant; meaning unknown
  offset 24..31  int64 ns         STREAM CLOCK, same cadence. Time since THIS
                                  stream began, not since the source started,
                                  and not zero: packet #1 carries ~0.3 s.
  offset 32..    PCM              1062 frames x 2 channels x 2 bytes = 4248

Offset 4..7 is a session field. A source sends different values in different
sessions — 00016100, 00014100, 0001b100, 0001e900, 00010900 — constant within
one session. The receiver plays the same whatever value it carries.

The device clock (offset 8..15) is a PTS in the network's PTP time: the
listener's pipeline runs on a PTP clock and rejects a PTS given in our own
uptime as «Not valid PTS». So: our clock + the PTP offset, plus the lead a
source gives its packets (1.117 s; we use 1.08 s with margin for the ~10 ms
offset uncertainty).

The audio is 16-bit signed little-endian stereo, uncompressed and unencrypted,
at 44100 Hz: 1062 frames per 24 081 633 ns IS 44100/s. The high word of
offset 4..7 names the format — fpsd logs «FPS audio format 1 (S16LE, 44100
Hz)» and «format 2 (S16LE, 48000 Hz)». We send ONLY format 1, to every
product: every listener plays 44.1 kHz, so the rate is a constant of this
library, not a property of the listener.

Beside the UDP stream the source opens a TCP connection to the same port on
the receiver and sends one 0x0a byte per second. The receiver uses it to
notice the source vanishing without waiting for the audio to dry up. The
receiver does not answer it, and the source port is not shared with UDP.
"""
from __future__ import annotations

import logging
import socket
import struct
import sys
import threading
import time
from dataclasses import dataclass
from typing import Iterator, List, Optional, Set, Tuple

try:                                                   # Python 3.8+
    from typing import Protocol
except ImportError:                                    # pragma: no cover
    Protocol = object                                  # type: ignore

log = logging.getLogger("beo.networklink.fps")

PORT = 1234
MAGIC = 0x1234
HEADER_BYTES = 32
CHANNELS = 2
BYTES_PER_SAMPLE = 2
FRAMES_PER_DATAGRAM = 1062
RATE_HZ = 44100

# Offset 4..7. High word = format code (0001 = 44.1 kHz, the only one we send;
# 0002 = 48 kHz). The low word is not a constant, and the receiver does not
# decide on it.
SESSION_FIELD = bytes.fromhex("00016100")
CONST_16_23 = b"\xff" * 8

# A source sends one 16-byte datagram as number two in the stream. Same magic
# and same sequence counter as the audio, but another constant and eight zero
# bytes:
#
#     12340001 00020200 00000000 00000000
#
# The marker is a CONTROL DATAGRAM, not only an opening: the same datagram
# goes out at pause and at play, together with the PROGRESS notification.
# Offset 4..5 is a counter that starts at 2 and goes up one per event; 6..7 is
# fixed 0200. The audio datagrams continue unchanged with music during the
# pause, and nothing goes on 1235/1236 or TCP — so it is this datagram that
# asks the listener to mute/unmute (MuteAllSinks / UnmuteAllSinks).
MARKER_4_7 = bytes.fromhex("00020200")

# All FPS traffic carries ToS 0x88 — DSCP 34, AF41 — without exception.
TOS_AF41 = 0x88

# How far the PTS lies AHEAD of PTP-now when the packet is sent. The listener's
# pipeline delay is 1.117 s (ptsToPlayMs 1110), and a PTS further ahead than
# that is reported «Not valid PTS» — the limit is apparently the pipeline
# delay. Slightly under, with margin for the ~10 ms offset uncertainty.
PTP_LEAD_NS = 1_080_000_000

# What offset 24..31 begins at. Packet #1 carries about 0.3 s even when the
# source has played for seconds BEFORE the listener joined — so it is time
# since THIS stream began, with a small lead, not playback time since the
# source started.
STREAM_START_NS = 300_000_000


@dataclass(frozen=True)
class AudioFormat:
    """PCM format of one FPS stream. 44 100 Hz for every product (see above)."""

    rate_hz: int = RATE_HZ
    channels: int = CHANNELS
    sample: str = "S16_LE"
    frames_per_datagram: int = FRAMES_PER_DATAGRAM

    @property
    def bytes_per_sample(self) -> int:
        if self.sample != "S16_LE":
            raise ValueError("only S16_LE is supported, not %r" % self.sample)
        return BYTES_PER_SAMPLE

    @property
    def bytes_per_frame(self) -> int:
        return self.channels * self.bytes_per_sample

    @property
    def payload_bytes(self) -> int:
        return self.frames_per_datagram * self.bytes_per_frame

    @property
    def interval_ns(self) -> int:
        """The cadence follows from the two: one datagram per frames/rate seconds."""
        return round(self.frames_per_datagram * 1_000_000_000 / self.rate_hz)

    @property
    def interval_s(self) -> float:
        return self.interval_ns / 1_000_000_000


FORMAT_44100 = AudioFormat(RATE_HZ)


# ------------------------------------------------------------------ clocks


class FpsClock(Protocol):
    """Where the PTS (offset 8..15) comes from: PTP nanoseconds, incl. lead."""

    def now_ns(self) -> int: ...


class MonotonicClock:
    """time.monotonic_ns() — our uptime. For loopback tests and the "oppetid"
    mode, where no PTP listener reads the PTS."""

    def at_ns(self, monotonic_ns: int) -> int:
        return monotonic_ns

    def now_ns(self) -> int:
        return time.monotonic_ns()


class OffsetPtpClock:
    """PTP time = our monotonic clock + a stored offset, plus the lead.

    The offset depends on this machine's own clock and holds only until it
    reboots; ``nsdk.FpsdLog.estimate_offset_ns`` derives it from the listener's
    fpsd log. Without it the PTSs are invalid and the listener silent.
    """

    def __init__(self, offset_ns: int, lead_ns: int = PTP_LEAD_NS):
        self.offset_ns = int(offset_ns)
        self.lead_ns = int(lead_ns)

    def now_ns(self) -> int:
        return time.monotonic_ns() + self.offset_ns + self.lead_ns

    def at_ns(self, monotonic_ns: int) -> int:
        """PTP time of a moment given in time.monotonic_ns(), plus the lead."""
        return monotonic_ns + self.offset_ns + self.lead_ns


# ------------------------------------------------------------------ packets


def build_header(sequence: int, device_ns: int, stream_ns: int,
                 session_field: bytes = SESSION_FIELD) -> bytes:
    """The 32-byte FPS header. Both clocks are 64-bit nanoseconds, big-endian;
    the bytes at offset 8..11 are the device clock's high word, not a constant.
    """
    return (struct.pack(">HH", MAGIC, sequence & 0xFFFF)
            + session_field
            + struct.pack(">Q", device_ns & 0xFFFFFFFFFFFFFFFF)
            + CONST_16_23
            + struct.pack(">q", stream_ns))


def build_marker(sequence: int, counter: int = 2) -> bytes:
    """The short datagram, without audio. counter: 2 at start, then 3, 4, ..."""
    return (struct.pack(">HHH", MAGIC, sequence & 0xFFFF, counter & 0xFFFF)
            + MARKER_4_7[2:] + b"\x00" * 8)


assert len(build_marker(1)) == 16, "the marker must be 16 bytes"
assert len(build_header(0, 0, 0)) == 32, "the header must be 32 bytes"


def _set_priority(sok) -> None:
    """Mark the socket AF41, and ask the kernel to send it in the matching queue.

    IP_TOS sets the mark on the wire. SO_PRIORITY drives Linux' own queue and
    thereby which WMM category the wireless card picks; without it the mark on
    the wire can be right while the packet is still sent as Best Effort. Both
    are best effort — if they fail, audio is still sent.
    """
    try:
        sok.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, TOS_AF41)
    except OSError as e:
        log.warning("FPS: could not set IP_TOS (%s)", e)
    try:
        # SO_PRIORITY 5 corresponds to AC_VI in Linux' traffic classes.
        sok.setsockopt(socket.SOL_SOCKET, socket.SO_PRIORITY, 5)
    except (OSError, AttributeError):
        pass


def _wait_until(target: float) -> None:
    """Sleep until a point in time, precisely — also on Windows.

    time.sleep(delay) alone is too coarse for a 24 ms cadence: Windows' timer
    is 15.6 ms coarse by default, and timeBeginPeriod(1) below brings it to
    1 ms. The coarse sleep stops 1 ms early and a spin covers the rest.

    The spin part is deliberately SHORT. A longer spin holds the interpreter
    lock and lets the product's other threads — HTTP, XMPP, notifications —
    wait and then take the lock in long stretches, which makes the send
    cadence far more uneven than the sleep it was meant to improve.
    """
    rest = target - time.perf_counter()
    if rest > 0.0015:
        time.sleep(rest - 0.001)
    while time.perf_counter() < target:
        pass


# On Windows the timer resolution can be raised from 15.6 ms to 1 ms for the
# whole process. That makes the coarse sleep above far more precise, so the
# spin part stays short.
if sys.platform == "win32":
    try:
        import ctypes
        ctypes.windll.winmm.timeBeginPeriod(1)
    except Exception:
        pass


@dataclass
class FpsStats:
    datagrams_sent: int
    markers_sent: int
    resets: int
    sequence: int
    device_ns: int
    stream_ns: int


class FpsSender:
    """Streams PCM to one receiver, with the TCP heartbeat alongside. Own thread.

    ``clock.now_ns()`` is sampled ONCE at :meth:`start` for the PTS; from then
    on the PTS advances exactly ``fmt.interval_ns`` per datagram (invariant 9).
    ``stream_start_ns`` is what offset 24..31 begins at; ``session_field`` is
    offset 4..7.
    """

    def __init__(self, host: str, fmt: AudioFormat, clock: FpsClock, *,
                 port: int = PORT, stream_start_ns: int = STREAM_START_NS,
                 session_field: bytes = SESSION_FIELD):
        self.host, self.port = host, port
        self.fmt = fmt
        self.clock = clock
        self.session_field = bytes(session_field)
        if len(self.session_field) != 4:
            raise ValueError("session_field (offset 4..7) must be 4 bytes")
        self.sequence = 0
        self.device_ns = 0             # sampled from the clock at start()
        self.stream_ns = stream_start_ns
        self.stream_start_ns = stream_start_ns
        self._udp = None
        self._tcp = None
        self._running = False
        self._source = None            # callable -> PCM bytes, or None
        self._last_send = None        # send cadence, see _pump
        self._cadence: List[float] = []
        self.datagrams_sent = 0
        self._marker_sent = False
        self._marker_n = 2             # 2 at start, +1 per event
        self._markers_sent = 0
        self._resets = 0
        # Injection points for tests (FakeClock pacing): perf_counter and the
        # precise wait. Production uses the real ones.
        self._now = time.perf_counter
        self._wait_until = _wait_until

    # ---- lifecycle ---------------------------------------------------------
    def start(self, source) -> None:
        """source: an AudioSource (``read_block()``) or a callable returning
        exactly ``fmt.payload_bytes`` of PCM, or None for silence."""
        self._indexed = None
        if source is None:
            self._source = None
        elif callable(source):
            self._source = source
        elif hasattr(source, "read_indexed") and hasattr(self.clock, "at_ns"):
            # Timeline mode (Fanout subscriber): the PTS comes from the block's
            # place on the capture timeline, identical for every listener.
            self._indexed = source
            self._source = source.read_block
        else:
            self._source = source.read_block
        # The PTS: our clock + PTP offset + lead, sampled once. From here it
        # counts exactly 1062 frames per datagram.
        self.device_ns = self.clock.now_ns()
        self._udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # The audio must carry the AF41 priority, or it lands in Best Effort
        # on the wireless network — and one lost fragment loses the whole
        # datagram.
        _set_priority(self._udp)
        try:
            self._tcp = socket.create_connection((self.host, self.port), timeout=5)
            self._tcp.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            _set_priority(self._tcp)
            log.info("FPS: TCP heartbeat opened to %s:%d", self.host, self.port)
        except Exception as e:
            # A real source opens TCP first; if the receiver is not listening
            # there, it is almost certainly not expecting us at all.
            log.warning("FPS: could not open TCP to %s:%d (%s) — the receiver "
                        "is probably not listening", self.host, self.port, e)
            self._tcp = None
        # Bind the UDP socket now so its local port can be read — and logged
        # with the TCP one. A source does not share the port between the two,
        # so the receiver cannot tie audio and heartbeat together on it; ours
        # have separate random ports as well.
        try:
            self._udp.bind(("", 0))
        except OSError:
            pass
        try:
            up = self._udp.getsockname()[1]
            tp = self._tcp.getsockname()[1] if self._tcp else None
            log.warning("FPS: our ports — UDP from %s, TCP from %s%s", up, tp,
                        "  (SAME)" if up == tp else "  (different)")
        except OSError:
            pass
        self._running = True
        threading.Thread(target=self._pump_timeline if self._indexed is not None else self._pump,
                         daemon=True, name="fps-pump").start()
        threading.Thread(target=self._heartbeat, daemon=True, name="fps-heartbeat").start()
        threading.Thread(target=self._read_heartbeat, daemon=True, name="fps-heartbeat-rx").start()
        log.info("FPS: streaming to %s:%d", self.host, self.port)

    def _read_heartbeat(self) -> None:
        """Drain what the receiver sends back on the heartbeat socket.

        Our own receiver answers 0x0a to every byte; reading keeps the socket
        flowing for any receiver that answers, and logs the first replies.
        """
        s = self._tcp
        if not s:
            return
        try:
            s.settimeout(1.0)
        except OSError:
            return
        shown = 0
        while self._running and self._tcp is s:
            try:
                b = s.recv(256)
            except socket.timeout:
                continue
            except OSError:
                return
            if not b:
                log.warning("FPS: the receiver closed the heartbeat")
                return
            if shown < 5:
                shown += 1
                log.warning("FPS: the receiver wrote on the heartbeat: %s", b[:32].hex())

    def stop(self) -> None:
        self._running = False
        for s in (self._tcp, self._udp):
            try:
                if s:
                    s.close()
            except OSError:
                pass
        self._tcp = self._udp = None
        log.info("FPS: stopped after %d datagrams", self.datagrams_sent)

    def send_marker(self) -> None:
        """The control datagram at pause/play — see MARKER_4_7. Uses a
        sequence number of the audio series and counts one up."""
        if not self._udp:
            return
        self._marker_n += 1
        try:
            self._udp.sendto(build_marker(self.sequence, self._marker_n),
                             (self.host, self.port))
            self.sequence = (self.sequence + 1) & 0xFFFF
            self._markers_sent += 1
            log.warning("FPS: marker #%d sent (pause/play)", self._marker_n)
        except Exception as e:
            log.warning("FPS: the marker could not be sent (%s)", e)

    def mute(self) -> None:
        """Marker n+1: the listener mutes itself (MuteAllSinks). PCM continues."""
        self.send_marker()

    def unmute(self) -> None:
        """Marker n+2: UnmuteAllSinks. Same datagram, next counter value."""
        self.send_marker()

    def stats(self) -> FpsStats:
        return FpsStats(self.datagrams_sent, self._markers_sent, self._resets,
                        self.sequence, self.device_ns, self.stream_ns)

    @property
    def running(self) -> bool:
        return self._running

    # ---- the two loops -----------------------------------------------------
    def _heartbeat(self) -> None:
        while self._running and self._tcp:
            try:
                self._tcp.sendall(b"\x0a")
            except Exception as e:
                log.info("FPS: heartbeat interrupted (%s)", e)
                return
            time.sleep(1.0)

    def _send_datagram(self, pcm: bytes) -> bool:
        payload_bytes = self.fmt.payload_bytes
        if len(pcm) < payload_bytes:
            pcm = pcm + bytes(payload_bytes - len(pcm))
        elif len(pcm) > payload_bytes:
            pcm = pcm[:payload_bytes]
        datagram = build_header(self.sequence, self.device_ns, self.stream_ns, self.session_field) + pcm
        try:
            self._udp.sendto(datagram, (self.host, self.port))
            self.datagrams_sent += 1
        except Exception as e:
            log.warning("FPS: send failed (%s)", e)
            return False
        self.sequence = (self.sequence + 1) & 0xFFFF
        if self.datagrams_sent == 1 and not self._marker_sent:
            self._marker_sent = True
            try:
                self._udp.sendto(build_marker(self.sequence, self._marker_n), (self.host, self.port))
                self._markers_sent += 1
            except Exception as e:
                log.warning("FPS: the marker could not be sent (%s)", e)
            self.sequence = (self.sequence + 1) & 0xFFFF
        return True

    def _pump_timeline(self) -> None:
        """Send each captured block as it arrives, stamped with ITS time on the
        capture timeline: PTS = clock.at_ns(origin + n * interval). All
        listeners of the same Fanout therefore carry identical PTS for the same
        block, whenever they joined and whatever they dropped. The stream clock
        starts at stream_start_ns for the first block this listener gets."""
        interval_ns = self.fmt.interval_ns
        first_n = None
        log.info("FPS: timeline mode (PTS from the capture timeline)")
        while self._running:
            item = self._indexed.read_indexed(timeout=0.05)
            if item is None:
                continue
            n, pcm = item
            origin = self._indexed.origin_ns
            if origin is None:
                continue
            if first_n is None:
                first_n = n
            self.device_ns = self.clock.at_ns(origin + n * interval_ns)
            self.stream_ns = self.stream_start_ns + (n - first_n) * interval_ns
            if not self._send_datagram(pcm):
                return
            now = self._now()
            if self._last_send is not None:
                self._cadence.append((now - self._last_send) * 1000.0)
                if len(self._cadence) >= 400:
                    import statistics as _st
                    _d = self._cadence
                    log.warning("FPS: SEND CADENCE over %d: mean %.2f ms  spread %.2f ms  min %.2f  max %.2f  "
                                ">5ms off: %d", len(_d), _st.mean(_d), _st.pstdev(_d), min(_d), max(_d),
                                sum(1 for x in _d if abs(x - self.fmt.interval_s * 1000) > 5))
                    self._cadence = []
            self._last_send = now

    def _pump(self) -> None:
        payload_bytes = self.fmt.payload_bytes
        interval_ns = self.fmt.interval_ns
        interval_s = self.fmt.interval_s
        silence = b"\x00" * payload_bytes
        next_at = self._now()
        while self._running:
            pcm = None
            if self._source:
                try:
                    pcm = self._source()
                except Exception as e:
                    log.warning("FPS: the audio source failed (%s)", e)
            if pcm is None:
                pcm = silence
            elif len(pcm) < payload_bytes:
                pcm = pcm + silence[len(pcm):]
            elif len(pcm) > payload_bytes:
                pcm = pcm[:payload_bytes]

            datagram = build_header(self.sequence, self.device_ns,
                                    self.stream_ns, self.session_field) + pcm
            try:
                self._udp.sendto(datagram, (self.host, self.port))
                self.datagrams_sent += 1
            except Exception as e:
                log.warning("FPS: send failed (%s)", e)
                return
            # Send cadence, measured HERE — not at arrival after WiFi. It is
            # the only way to separate our own unevenness from the network's.
            now = self._now()
            if self._last_send is not None:
                self._cadence.append((now - self._last_send) * 1000.0)
                if len(self._cadence) >= 400:
                    import statistics as _st
                    _d = self._cadence
                    log.warning("FPS: SEND CADENCE over %d: mean %.2f ms  "
                                "spread %.2f ms  min %.2f  max %.2f  "
                                ">5ms off: %d", len(_d), _st.mean(_d),
                                _st.pstdev(_d), min(_d), max(_d),
                                sum(1 for x in _d if abs(x - interval_s * 1000)
                                    > 5))
                    self._cadence = []
            self._last_send = now

            self.sequence = (self.sequence + 1) & 0xFFFF

            # The marker, once, right after the first audio datagram — that is
            # its place in the stream. It uses a sequence number of the same
            # series, so the counter must move on again afterwards.
            if self.datagrams_sent == 1 and not self._marker_sent:
                self._marker_sent = True
                try:
                    self._udp.sendto(build_marker(self.sequence, self._marker_n),
                                     (self.host, self.port))
                    self._markers_sent += 1
                except Exception as e:
                    log.warning("FPS: the marker could not be sent (%s)", e)
                self.sequence = (self.sequence + 1) & 0xFFFF

            self.device_ns += interval_ns
            self.stream_ns += interval_ns

            # Absolute schedule, not sleep(interval): the receiver derives
            # timing from the timestamps, so drift would show up as skew.
            next_at += interval_s
            now = self._now()
            if next_at > now:
                self._wait_until(next_at)
            elif now - next_at < 0.100:
                # Slightly behind: keep the plan, so the next datagram catches
                # up what was lost. Resetting the plan at every delay loses a
                # little time each reset, and the PTS (which counts exactly
                # 1062 frames per datagram) then falls steadily behind the
                # listener's PTP clock.
                pass
            else:
                # Far behind (GC, swap, NIC): catching up would send everything
                # in one clump — worse than a gap. Reset the plan AND move the
                # clocks just as far, so the PTS is still true in PTP time;
                # the receiver fills the gap itself.
                behind = now - next_at
                hop = int(behind / interval_s) * interval_ns
                self.device_ns += hop
                self.stream_ns += hop
                self._resets += 1
                log.warning("FPS: %.0f ms behind — plan reset, clocks "
                            "moved %d ms", behind * 1000, hop // 1_000_000)
                next_at = now


# ---------------------------------------------------------------- receiver


@dataclass
class FpsBlock:
    """One received audio datagram, header decoded."""
    sequence: int
    session_field: bytes
    device_ns: int
    stream_ns: int
    pcm: bytes
    arrived: float                 # perf_counter at arrival
    peer: str
    gap: int = 0                   # sequence step - 1 (0 = contiguous)


class FpsReceiver:
    """Receive an FPS stream. A test receiver, not a product.

    It exists so the packetiser can be proved right against a machine that
    tells us what it saw, before it is ever pointed at a B&O speaker. It
    checks the three things a real receiver would notice: the 32-byte header
    (magic, the two constants, both nanosecond clocks), sequence continuity,
    and cadence — the clocks must advance ``fmt.interval_ns`` per datagram.
    """

    def __init__(self, fmt: AudioFormat, *, port: int = PORT, bind: str = "0.0.0.0",
                 heartbeat: bool = True):
        self.fmt, self.port, self.bind = fmt, port, bind
        self.heartbeat = heartbeat
        self._udp = None
        self._stop = threading.Event()
        self.state = {"beats": 0, "tcp_peer": None}
        self.n = 0
        self.gaps = 0
        self.bad_magic = 0
        self.short = 0                     # datagrams shorter than the header, not markers
        self.markers = 0                   # 16-byte mute/unmute markers
        self.marker_counters: List[int] = []
        self.prev_seq = None
        self.prev_dev = None
        self.prev_str = None
        self.dev_deltas: List[int] = []
        self.str_deltas: List[int] = []
        self.consts: Set[Tuple[str, str]] = set()
        self.payload_sizes: Set[int] = set()
        self.first_arrival = None
        self.last_arrival = None
        self.peer = None

    def _tcp_listener(self) -> None:
        """A real source opens TCP 1234 and sends 0x0a once a second."""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((self.bind, self.port))
        except OSError as e:
            log.warning("FPS receiver: could not listen on TCP %d: %s", self.port, e)
            return
        s.listen(4)
        s.settimeout(0.5)
        stop, state = self._stop, self.state
        while not stop.is_set():
            try:
                conn, addr = s.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            state["tcp_peer"] = addr[0]
            conn.settimeout(0.5)
            while not stop.is_set():
                try:
                    b = conn.recv(64)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not b:
                    break
                state["beats"] += len(b)
            conn.close()
        s.close()

    def open(self) -> "FpsReceiver":
        self._stop.clear()
        if self.heartbeat:
            threading.Thread(target=self._tcp_listener, daemon=True,
                             name="fps-rx-heartbeat").start()
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        udp.bind((self.bind, self.port))
        udp.settimeout(1.0)
        self._udp = udp
        return self

    def close(self) -> None:
        self._stop.set()
        if self._udp:
            try:
                self._udp.close()
            except OSError:
                pass
            self._udp = None

    def decode(self, data: bytes, addr, now: float) -> Optional[FpsBlock]:
        """Decode one datagram and update the statistics. None = not audio."""
        self.peer = addr[0]
        if len(data) < 4:
            self.short += 1
            return None
        magic, seq = struct.unpack(">HH", data[0:4])
        if magic != MAGIC:
            self.bad_magic += 1
            return None
        if len(data) == 16 and data[6:8] == MARKER_4_7[2:]:
            # The 16-byte marker (mute/unmute control datagram). It uses a
            # sequence number of the audio series, so it keeps continuity.
            self.markers += 1
            self.marker_counters.append(struct.unpack(">H", data[4:6])[0])
            if self.prev_seq is not None and (seq - self.prev_seq) & 0xFFFF != 1:
                self.gaps += 1
            self.prev_seq = seq
            return None
        if len(data) < HEADER_BYTES:
            self.short += 1
            return None
        c47 = data[4:8]
        dev = struct.unpack(">Q", data[8:16])[0]
        c1623 = data[16:24].hex()
        stm = struct.unpack(">q", data[24:32])[0]
        self.consts.add((c47.hex(), c1623))
        self.payload_sizes.add(len(data) - HEADER_BYTES)

        gap = 0
        if self.prev_seq is not None:
            step = (seq - self.prev_seq) & 0xFFFF
            if step != 1:
                self.gaps += 1
                gap = step - 1
        if self.prev_dev is not None:
            self.dev_deltas.append(dev - self.prev_dev)
            self.str_deltas.append(stm - self.prev_str)
        self.prev_seq, self.prev_dev, self.prev_str = seq, dev, stm

        if self.first_arrival is None:
            self.first_arrival = now
        self.last_arrival = now
        self.n += 1
        return FpsBlock(seq, c47, dev, stm, data[HEADER_BYTES:], now, addr[0], gap)

    def blocks(self, seconds: Optional[float] = None) -> Iterator[FpsBlock]:
        """Yield audio blocks as they arrive, for ``seconds`` (None = until close)."""
        if self._udp is None:
            self.open()
        deadline = None if seconds is None else time.time() + seconds
        while not self._stop.is_set() and (deadline is None or time.time() < deadline):
            try:
                data, addr = self._udp.recvfrom(8192)
            except socket.timeout:
                continue
            except OSError:
                return
            blk = self.decode(data, addr, time.perf_counter())
            if blk is not None:
                yield blk

    def stats(self) -> dict:
        out = {
            "peer": self.peer, "datagrams": self.n, "bad_magic": self.bad_magic,
            "short": self.short, "gaps": self.gaps,
            "markers": self.markers, "marker_counters": list(self.marker_counters),
            "payload_sizes": sorted(self.payload_sizes),
            "header_constants": sorted(self.consts),
            "heartbeat_bytes": self.state["beats"], "tcp_peer": self.state["tcp_peer"],
        }
        if self.dev_deltas:
            iv = self.fmt.interval_ns
            out["device_clock_exact"] = sum(1 for d in self.dev_deltas if d == iv)
            out["stream_clock_exact"] = sum(1 for d in self.str_deltas if d == iv)
            out["deltas"] = len(self.dev_deltas)
        if self.n > 1 and self.last_arrival > self.first_arrival:
            span = self.last_arrival - self.first_arrival
            out["arrival_rate"] = (self.n - 1) / span
            out["expected_rate"] = 1 / self.fmt.interval_s
            out["audio_seconds"] = self.n * self.fmt.frames_per_datagram / self.fmt.rate_hz
        return out


__all__ = [
    "AudioFormat", "FORMAT_44100", "RATE_HZ", "FpsClock", "MonotonicClock",
    "OffsetPtpClock", "build_header", "build_marker", "FpsSender", "FpsStats",
    "FpsReceiver", "FpsBlock", "PORT", "MAGIC", "HEADER_BYTES", "SESSION_FIELD",
    "PTP_LEAD_NS", "STREAM_START_NS", "TOS_AF41",
]
