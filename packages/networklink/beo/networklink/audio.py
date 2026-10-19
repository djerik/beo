"""Audio sources for the FPS sender, and a sink for a received stream.

The core never opens a sound card by itself; ``AlsaCapture`` is a thin
``arecord`` subprocess kept here for the Pi, and ``AlsaPlayback`` is its
``aplay`` counterpart. ``Fanout`` distributes one capture to N listeners, one
queue per listener, oldest dropped on overflow.
"""
from __future__ import annotations

import logging
import math
import queue
import struct
import subprocess
import threading
import time
from typing import Dict, Optional

try:
    from typing import Protocol
except ImportError:                                    # pragma: no cover
    Protocol = object                                  # type: ignore

from .fps import AudioFormat, FORMAT_44100, _wait_until

log = logging.getLogger("beo.networklink.audio")


class AudioSource(Protocol):
    """What the FPS sender pulls from: exactly ``format.payload_bytes`` per
    call, or None for silence."""

    format: AudioFormat

    def read_block(self) -> Optional[bytes]: ...


class Silence:
    """Always None — the sender substitutes zeros."""

    def __init__(self, fmt: AudioFormat = FORMAT_44100):
        self.format = fmt

    def read_block(self) -> Optional[bytes]:
        return None


class ToneSource:
    """A pure tone, precomputed once and looped.

    The buffer holds one second (``rate_hz`` frames), so a whole-hertz
    frequency wraps from end to start without a click. Looping it is far
    cheaper than packing every datagram from scratch.

    The default amplitude 0.35 is about -21 dBFS, a few dB below typical
    music, so a test tone is never louder than it.
    """

    def __init__(self, fmt: AudioFormat = FORMAT_44100, freq: float = 440.0,
                 amplitude: float = 0.35):
        self.format = fmt
        self.freq, self.amplitude = freq, amplitude
        n = fmt.rate_hz
        top = amplitude * 32767
        buf = bytearray()
        for i in range(n):
            v = int(top * math.sin(2 * math.pi * freq * i / fmt.rate_hz))
            buf += struct.pack("<hh", v, v)
        self._buf = bytes(buf)
        self._frame = fmt.bytes_per_frame
        self._pos = 0

    def read_block(self) -> bytes:
        payload = self.format.payload_bytes
        buf = self._buf
        i = self._pos
        out = buf[i:i + payload]
        if len(out) < payload:
            out += buf[:payload - len(out)]
        self._pos = (i + payload) % len(buf)
        # The buffer must begin on a whole frame, otherwise the channels swap.
        if self._pos % self._frame:
            self._pos -= self._pos % self._frame
        return out


class AlsaCapture:
    """arecord in raw mode, chopped into exactly one FPS payload per read.

    A queue sits between arecord and the sender because the two are paced by
    different clocks, the ADC's and ours. They agree on paper but not to the
    microsecond, and without some slack every scheduling hiccup would lose a
    datagram.

    arecord rather than a Python binding: it is on the image already, and a
    raw pipe cannot hide a format error. The card supports 44100 S16_LE
    natively, so nothing is resampled.
    """

    def __init__(self, device: str = "hw:0,0", fmt: AudioFormat = FORMAT_44100, depth: int = 64):
        self.device = device
        self.format = fmt
        # Deep enough to ride out a reader held up for more than a second:
        # a block dropped here is a hole in every stream, and the depth costs
        # nothing while the reader keeps up.
        self.q: "queue.Queue[bytes]" = queue.Queue(maxsize=depth)
        self.proc = None
        self.blocks = 0
        self.dropped = 0
        self._stop = threading.Event()

    def start(self) -> "AlsaCapture":
        if self.proc is not None:
            return self
        self._stop.clear()
        self.proc = subprocess.Popen(
            ["arecord", "-D", self.device, "-f", self.format.sample,
             "-r", str(self.format.rate_hz), "-c", str(self.format.channels),
             # One datagram per period, four periods of buffer. Left to itself
             # ALSA picks a period several datagrams long; the reader then wakes
             # to a burst of blocks and the stream goes out in clumps instead of
             # one datagram per interval.
             "--period-size", str(self.format.frames_per_datagram),
             "--buffer-size", str(self.format.frames_per_datagram * 4),
             "-t", "raw", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        threading.Thread(target=self._read, daemon=True, name="alsa-capture").start()
        log.info("ALSA: capturing from %s (%d Hz %s %d ch)", self.device,
                 self.format.rate_hz, self.format.sample, self.format.channels)
        return self

    def _read(self) -> None:
        payload = self.format.payload_bytes
        proc = self.proc
        while not self._stop.is_set():
            buf = proc.stdout.read(payload)
            if not buf or len(buf) < payload:
                log.warning("ALSA: the stream ended")
                return
            self.blocks += 1
            try:
                self.q.put_nowait(buf)
            except queue.Full:
                # Dropping the oldest keeps latency bounded. Dropping the newest
                # would make the stream drift further behind for every hiccup.
                try:
                    self.q.get_nowait()
                    self.dropped += 1
                    self.q.put_nowait(buf)
                except queue.Empty:
                    pass

    def read_block(self) -> Optional[bytes]:
        try:
            return self.q.get(timeout=0.05)
        except queue.Empty:
            return None            # FpsSender substitutes silence

    def stop(self) -> None:
        self._stop.set()
        proc = self.proc
        self.proc = None
        if proc is None:
            return
        # Wait for arecord to be gone before returning. The card is clocked by
        # the Master Link interface, so with no audio on the bus arecord sits
        # in a blocking read and ignores SIGTERM; if it still holds the device
        # when the next capture starts, the new arecord reads nothing.
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            log.warning("ALSA: the recorder did not react to terminate — killing it")
            proc.kill()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                log.error("ALSA: the recorder could not be killed, pid %d "
                          "still holds the sound card", proc.pid)
        # The drops are holes in every stream that was fed from here.
        log.info("ALSA: capture stopped after %d blocks, %d dropped", self.blocks, self.dropped)


class _Subscriber:
    """One listener's view of the Fanout. ``read_indexed`` gives the block's
    index on the capture timeline, so every listener stamps the same PTS on
    the same block (see Fanout.origin_ns)."""

    def __init__(self, fmt: AudioFormat, q: "queue.Queue", fanout: "Fanout"):
        self.format = fmt
        self.q = q
        self.fanout = fanout

    @property
    def origin_ns(self) -> Optional[int]:
        return self.fanout.origin_ns

    def read_indexed(self, timeout: float = 0.05):
        try:
            return self.q.get(timeout=timeout)     # (index, bytes)
        except queue.Empty:
            return None

    def read_block(self) -> Optional[bytes]:
        item = self.read_indexed()
        return None if item is None else item[1]   # FpsSender substitutes silence


class Fanout:
    """One capture, read once, distributed to a queue per subscriber.

    The ADC can only be opened in one place. A queue can only be read in one
    place. So the capture is read once and distributed to a queue per
    listener — otherwise two speakers would steal each other's datagrams and
    both get half the music.

    The queue per listener drops the oldest on overflow. A slow receiver must
    fall behind alone; it must not drag the others with it.

    The underlying source is started on the first subscriber and stopped on
    the last unsubscribe when it has ``start()``/``stop()`` (AlsaCapture).
    ``paced=True`` means the source blocks at real-time rate (ALSA); with
    ``paced=False`` (tone, file) the reader thread paces itself at
    ``format.interval_s``.
    """

    def __init__(self, source, *, depth: int = 64, paced: bool = True):
        self.source = source
        self.format: AudioFormat = source.format
        self.depth = depth
        self.paced = paced
        self.dropped: Dict[str, int] = {}
        self.subs: Dict[str, "queue.Queue"] = {}
        self.blocks = 0
        # The capture timeline: block i was captured at origin_ns + i * interval_ns
        # (time.monotonic_ns). Every listener stamps the same PTS on the same
        # block whenever it joined, which is what keeps them in sync.
        self.origin_ns: Optional[int] = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    def _ensure(self) -> None:
        # Each run of the capture gets its own stop flag and a fresh timeline.
        # The flag travels with the reader thread, so a block that an ended run
        # was still waiting on cannot land on the new timeline and set
        # origin_ns to the wrong moment.
        with self._lock:
            if self._thread is not None:
                return
            stop = self._stop = threading.Event()
            self.origin_ns = None
            self.blocks = 0
            thread = self._thread = threading.Thread(target=self._read, args=(stop,),
                                                     daemon=True, name="fanout")
        start = getattr(self.source, "start", None)
        if callable(start):
            start()
        thread.start()
        log.info("Fanout: reading %s", type(self.source).__name__)

    def _distribute(self, buf: bytes, run: Optional[threading.Event] = None) -> None:
        """``run`` is the stop flag of the capture run the block came from. A
        block from a run that has ended is dropped: it has no place on the
        timeline of the next one."""
        with self._lock:
            if run is not None and (run.is_set() or run is not self._stop):
                return
            if self.origin_ns is None:
                self.origin_ns = time.monotonic_ns()
            item = (self.blocks, buf)
            self.blocks += 1
            qs = list(self.subs.items())
        for key, q in qs:
            try:
                q.put_nowait(item)
            except queue.Full:
                # The oldest goes, and the stream to this subscriber has a hole
                # the width of it: counted, so a run can say how many.
                try:
                    q.get_nowait()
                    self.dropped[key] = self.dropped.get(key, 0) + 1
                    q.put_nowait(item)
                except queue.Empty:
                    pass

    def _read(self, stop: threading.Event) -> None:
        next_at = time.perf_counter()
        while not stop.is_set():
            buf = self.source.read_block()
            if buf is None:
                if not self.paced:
                    time.sleep(0.005)
                continue
            self._distribute(buf, stop)
            if not self.paced:
                next_at += self.format.interval_s
                _wait_until(next_at)

    def subscribe(self, key: str) -> _Subscriber:
        with self._lock:
            self.subs[key] = queue.Queue(maxsize=self.depth)
            q = self.subs[key]
        self._ensure()
        return _Subscriber(self.format, q, self)

    def unsubscribe(self, key: str) -> None:
        with self._lock:
            self.subs.pop(key, None)
            dropped = self.dropped.pop(key, 0)
            thread = self._thread if not self.subs else None
            if thread is not None:
                self._stop.set()        # under the lock: nothing is distributed after this
                self._thread = None
                self.origin_ns = None
                self.blocks = 0
        if dropped:
            log.warning("Fanout: %d block(s) were dropped for %s: holes in its stream, one "
                        "datagram wide each", dropped, key)
        if thread is not None:
            stop = getattr(self.source, "stop", None)
            if callable(stop):
                stop()
            # Let the reader finish before a new run can start, so it cannot
            # take the first block of the next capture with it.
            if thread is not threading.current_thread():
                thread.join(timeout=1.0)
            log.info("Fanout: no listeners left — capture stopped")


class AlsaPlayback:
    """aplay in raw mode, fed the blocks of a received stream, each at its time.

    Every block carries the network time at which it is to be heard, about
    two seconds after it arrives. Holding a block until then is what keeps
    this output in step with the other listeners of the same source; without
    a network clock the blocks are played as they come, which is audible as
    an echo between rooms but still plays.

    The sound card and the network clock do not run at the same rate. A card
    that is slow lets blocks pile up until they are too old and are dropped; a
    card that is fast runs dry for a moment. Both are a click now and then,
    not a drift that grows.

    Blocks are queued and written by a thread of their own: aplay blocks while
    the card is not clocked, and the receiver must never wait for it.
    """

    LATE_NS = 150_000_000          # older than this when its turn comes: dropped
    SANE_NS = 5_000_000_000        # further ahead than this is another clock, not a delay

    def __init__(self, device: str = "hw:0,0", fmt: AudioFormat = FORMAT_44100, *,
                 now_ns=None, latency_ms: int = 60, depth: int = 256):
        self.device = device
        self.format = fmt
        self.now_ns = now_ns
        self.latency_ns = latency_ms * 1_000_000
        self.q: "queue.Queue" = queue.Queue(maxsize=depth)
        self.proc = None
        self.played = 0
        self.dropped = 0
        self._stop = threading.Event()
        self._untimed_logged = False

    def start(self) -> "AlsaPlayback":
        if self.proc is not None:
            return self
        self._stop.clear()
        self.proc = subprocess.Popen(
            ["aplay", "-D", self.device, "-f", self.format.sample,
             "-r", str(self.format.rate_hz), "-c", str(self.format.channels),
             "--period-size", str(self.format.frames_per_datagram),
             "--buffer-size", str(self.format.frames_per_datagram * 4),
             "-t", "raw", "-"],
            stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
        threading.Thread(target=self._write, daemon=True, name="alsa-playback").start()
        log.info("ALSA: playing to %s (%d Hz %s %d ch), %s", self.device, self.format.rate_hz,
                 self.format.sample, self.format.channels,
                 "timed by the network clock" if self.now_ns else "as it arrives")
        return self

    def write(self, block) -> None:
        """Queue one received block: ``block.device_ns`` is when, ``block.pcm`` what."""
        try:
            self.q.put_nowait((block.device_ns, block.pcm))
        except queue.Full:
            # The oldest goes: it is the one whose time has most likely passed.
            try:
                self.q.get_nowait()
                self.dropped += 1
                self.q.put_nowait((block.device_ns, block.pcm))
            except (queue.Empty, queue.Full):
                pass

    def _due(self, pts_ns: int) -> bool:
        """Wait for the block's time. False = it is too old to play."""
        if self.now_ns is None:
            return True
        ahead = pts_ns - self.latency_ns - self.now_ns()
        if abs(ahead) > self.SANE_NS:
            if not self._untimed_logged:
                self._untimed_logged = True
                log.warning("ALSA: the stream's time is %.1f s from the network clock; "
                            "playing it as it arrives", ahead / 1e9)
            return True
        if ahead < -self.LATE_NS:
            return False
        if ahead > 0:
            self._stop.wait(ahead / 1e9)
        return True

    def _write(self) -> None:
        proc = self.proc
        while not self._stop.is_set():
            try:
                pts_ns, pcm = self.q.get(timeout=0.2)
            except queue.Empty:
                continue
            if not self._due(pts_ns):
                self.dropped += 1
                continue
            try:
                proc.stdin.write(pcm)
                proc.stdin.flush()
            except (OSError, ValueError):
                log.warning("ALSA: the player ended")
                return
            self.played += 1

    def stop(self) -> None:
        self._stop.set()
        proc = self.proc
        self.proc = None
        if proc is None:
            return
        # Killed, not drained: what is still queued belongs to a stream that
        # has ended, and aplay blocked on an unclocked card ignores terminate.
        proc.kill()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            log.error("ALSA: the player could not be killed, pid %d still holds "
                      "the sound card", proc.pid)
        log.info("ALSA: playback stopped after %d blocks, %d dropped", self.played, self.dropped)


__all__ = ["AudioSource", "Silence", "ToneSource", "AlsaCapture", "AlsaPlayback", "Fanout"]
