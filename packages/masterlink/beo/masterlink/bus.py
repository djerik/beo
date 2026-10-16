"""The MasterLink port and the bus on top of it.

Transport choice:
own the port. `SerialTransport` is the transport; a serial port has one owner,
so nothing else may hold it while the adapter runs.

Framing facts:
  * The FTDI FT-X on MDT v1 raises a parity error on EVERY byte whatever parity
    the receiver is set to, so the 9-bit mark/space delimiter cannot be
    recovered. 8N1 loses sync. Framing is timing + content.
  * With the FTDI latency_timer at its default 16 ms, USB batches bytes and cuts
    telegrams anywhere. At 1 ms they arrive whole, 0.5-0.6 ms per byte. A 5 ms
    silence ends a telegram; the audio master never answers sooner than 48 ms.
  * Our own bytes echo back on the half-duplex pair, possibly in two USB chunks
    (the parity switch after the first byte is a separate write + drain).
    Echo suppression must match the reassembled telegram.
  * Sending: MARK on the first byte, SPACE on the body, MARK on the trailing 00,
    each part on the wire before the parity changes. 0.35 s minimum between
    Beo4 presses.
  * tcdrain is not "on the wire" everywhere. ftdi_sio waits for the chip's
    transmitter to empty; cdc_acm (WCH CH9102 and the like) only waits for USB,
    and the parity change then overtakes bytes still in the chip's FIFO. So the
    sender also waits out the bytes' own transmission time.
"""
from __future__ import annotations

import logging
import os
import queue
import threading
import time
from typing import Callable, List, Optional

from .codec import Telegram, TelegramError, beo4_press, HEARTBEAT

log = logging.getLogger("beo.masterlink.bus")

Subscriber = Callable[[Telegram], None]


class MlTransport:
    """What a transport must provide. Threaded: `start()` spawns the reader."""

    def start(self, on_telegram: Subscriber) -> None:
        raise NotImplementedError

    def send(self, telegram: Telegram) -> None:
        raise NotImplementedError

    def close(self) -> None:
        pass


# ---------------------------------------------------------------- framing
class Framer:
    """Timing + content framing, transport-agnostic so it can be unit tested.

    feed(chunk, t) with each read; call flush(t) when a read timed out with
    nothing (i.e. silence). Complete telegrams are returned as they are found.
    """

    def __init__(self, silence_s: float = 0.005, max_buffer: int = 96, max_age_s: float = 0.5):
        self.silence_s = silence_s
        self.max_buffer = max_buffer
        self.max_age_s = max_age_s
        self._buf = bytearray()
        self._first_t: Optional[float] = None
        self._last_t: Optional[float] = None

    def feed(self, chunk: bytes, t: float) -> List[Telegram]:
        out: List[Telegram] = []
        if self._buf and self._last_t is not None and t - self._last_t > self.silence_s:
            out.extend(self._cut(t))
        if not self._buf:
            self._first_t = t
        self._buf.extend(chunk)
        self._last_t = t
        return out

    def flush(self, t: float) -> List[Telegram]:
        """Called on silence. Emits the buffer if it is a whole telegram."""
        if not self._buf or self._last_t is None:
            return []
        if t - self._last_t >= self.silence_s:
            return self._cut(t)
        return []

    def _cut(self, t: float) -> List[Telegram]:
        raw = bytes(self._buf)
        try:
            tg = Telegram.parse(raw)
            self._buf.clear()
            return [tg]
        except TelegramError:
            pass
        # Not a whole telegram. Two cases:
        #  * our own echo arrives as "c1" then the rest 20 ms later → keep waiting;
        #  * junk (a malformed telegram someone sent) followed by a good one →
        #    the good one ends exactly at the buffer end: emit it, drop the junk.
        found = self._scan(raw)
        if found and raw.endswith(found[-1].raw):
            self._buf.clear()
            log.debug("framer: resynced, dropped %d bytes", len(raw) - sum(len(f.raw) for f in found))
            return found
        stale = self._first_t is not None and (t - self._first_t) > self.max_age_s
        if len(raw) < self.max_buffer and not stale:
            return []
        self._buf.clear()
        if not found:
            log.debug("framer: dropped %d unparsable bytes: %s", len(raw), raw.hex())
        return found

    @staticmethod
    def _scan(raw: bytes) -> List[Telegram]:
        out: List[Telegram] = []
        i = 0
        while i + 12 <= len(raw):
            n = 12 + raw[i + 8]
            if i + n <= len(raw):
                try:
                    out.append(Telegram.parse(raw[i:i + n]))
                    i += n
                    continue
                except TelegramError:
                    pass
            i += 1
        return out


# ---------------------------------------------------------------- serial
def set_ftdi_latency(port: str, ms: int = 1) -> bool:
    """Best effort: /sys/bus/usb-serial/devices/<tty>/latency_timer. Needs root or a udev rule.

    Only a usb-serial adapter has the timer. A cdc_acm port (ttyACM) or the
    Pi's own UART has none, and there is nothing to set.
    """
    name = os.path.basename(port)
    if not os.path.isdir("/sys/bus/usb-serial/devices/%s" % name):
        return True
    path = "/sys/bus/usb-serial/devices/%s/latency_timer" % name
    try:
        with open(path) as f:
            cur = int(f.read().strip())
        if cur == ms:
            return True
        with open(path, "w") as f:
            f.write(str(ms))
        log.info("FTDI latency_timer %d -> %d ms", cur, ms)
        return True
    except OSError as e:
        log.warning("could not set FTDI latency_timer on %s (%s); telegrams may arrive split — "
                    "add the udev rule from deploy/", path, e)
        return False


class SerialTransport(MlTransport):
    """Owns the serial port. MDT v1: /dev/ttyUSB0 (FTDI). Carrier board: /dev/ttyAMA0."""

    def __init__(self, port: str = "/dev/ttyUSB0", baud: int = 19200, *,
                 silence_s: float = 0.005, set_latency: bool = True, echo_window_s: float = 2.0,
                 drain_margin_s: float = 0.005):
        self.port, self.baud = port, baud
        self.silence_s = silence_s
        self.drain_margin_s = drain_margin_s
        self.set_latency = set_latency
        self.echo_window_s = echo_window_s
        self._ser = None
        self._on: Optional[Subscriber] = None
        self._stop = threading.Event()
        self._reader: Optional[threading.Thread] = None
        self._wlock = threading.Lock()
        self._sent: List[tuple] = []      # (raw, t) for echo suppression

    def start(self, on_telegram: Subscriber) -> None:
        import serial
        import termios
        if self.set_latency:
            set_ftdi_latency(self.port, 1)
        ser = serial.Serial(self.port, self.baud, parity=serial.PARITY_SPACE, timeout=self.silence_s)
        fd = ser.fileno()
        a = termios.tcgetattr(fd)
        # No parity/framing marking: the FT-X flags every byte anyway.
        a[0] &= ~(termios.INPCK | termios.PARMRK | termios.ISTRIP)
        a[0] |= termios.IGNPAR
        termios.tcsetattr(fd, termios.TCSANOW, a)
        ser.reset_input_buffer()
        self._ser = ser
        self._on = on_telegram
        self._stop.clear()
        self._reader = threading.Thread(target=self._read_loop, name="ml-serial", daemon=True)
        self._reader.start()
        log.info("serial transport on %s @ %d", self.port, self.baud)

    def _read_loop(self) -> None:
        framer = Framer(self.silence_s)
        while not self._stop.is_set():
            try:
                chunk = self._ser.read(64)
            except Exception as e:      # port gone
                log.error("serial read failed: %s", e)
                time.sleep(1.0)
                continue
            now = time.monotonic()
            tgs = framer.feed(chunk, now) if chunk else framer.flush(now)
            for tg in tgs:
                if self._is_echo(tg, now):
                    continue
                self._deliver(tg)

    def _is_echo(self, tg: Telegram, now: float) -> bool:
        with self._wlock:
            self._sent = [(r, t) for r, t in self._sent if now - t < self.echo_window_s]
            for i, (r, t) in enumerate(self._sent):
                if r == tg.raw:
                    del self._sent[i]
                    return True
        return False

    def _deliver(self, tg: Telegram) -> None:
        try:
            self._on(tg)
        except Exception:
            log.exception("subscriber failed on %s", tg)

    def send(self, telegram: Telegram) -> None:
        import serial
        raw = telegram.raw
        with self._wlock:
            self._put(raw[:1], serial.PARITY_MARK)
            self._put(raw[1:-1], serial.PARITY_SPACE)
            self._put(raw[-1:], serial.PARITY_MARK)
            self._ser.parity = serial.PARITY_SPACE
            self._sent.append((raw, time.monotonic()))
        log.debug(">> %s", telegram)

    def _put(self, chunk: bytes, parity: str) -> None:
        """Send `chunk` with `parity` and return once it has left the chip."""
        self._ser.parity = parity
        self._ser.write(chunk)
        self._ser.flush()
        # Start, 8 data, parity and stop bit per byte, plus room for the chip
        # to start sending what USB has just handed it.
        time.sleep(len(chunk) * 11 / self.baud + self.drain_margin_s)

    def close(self) -> None:
        self._stop.set()
        if self._reader:
            self._reader.join(timeout=1.0)
        if self._ser:
            try:
                self._ser.close()
            except Exception:
                pass


# ---------------------------------------------------------------- bus
class MlBus:
    """One writer, paced presses, fan-out to subscribers.

    subscribe(fn) registers a callback (called on the transport's reader
    thread; keep it short).
    """

    def __init__(self, transport: MlTransport, *, press_gap_s: float = 0.35, send_gap_s: float = 0.05,
                 drop_heartbeat: bool = True):
        self.transport = transport
        self.press_gap_s = press_gap_s
        self.send_gap_s = send_gap_s
        self.drop_heartbeat = drop_heartbeat
        self._subs: List[Subscriber] = []
        self._lock = threading.Lock()
        self._last_send = 0.0
        self._last_press = 0.0

    def start(self) -> None:
        self.transport.start(self._on_telegram)

    def close(self) -> None:
        self.transport.close()

    def _on_telegram(self, tg: Telegram) -> None:
        if tg.raw == HEARTBEAT and self.drop_heartbeat:
            return
        log.debug("<< %s", tg)
        for fn in list(self._subs):
            try:
                fn(tg)
            except Exception:
                log.exception("subscriber failed")

    def subscribe(self, fn: Subscriber) -> None:
        self._subs.append(fn)

    def unsubscribe(self, fn: Subscriber) -> None:
        if fn in self._subs:
            self._subs.remove(fn)

    def send(self, telegram: Telegram) -> None:
        with self._lock:
            wait = self._last_send + self.send_gap_s - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self.transport.send(telegram)
            self._last_send = time.monotonic()

    def press(self, key: int, *, source: int = 0x8D, frm: int = 0x06, to: int = 0xC1,
              gap_s: Optional[float] = None, dest: int = 0x01) -> None:
        """A Beo4 press, never closer than press_gap_s (or gap_s) to the previous one."""
        gap = self.press_gap_s if gap_s is None else gap_s
        wait = self._last_press + gap - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self.send(beo4_press(key, source=source, frm=frm, to=to, dest=dest))
        self._last_press = time.monotonic()

    def expect(self, pred: Callable[[Telegram], bool]) -> "Waiter":
        """Register a waiter BEFORE sending, so a fast answer cannot be missed:

            with bus.expect(lambda t: t.payload_type == 0x87) as w:
                bus.press(Beo4.CD)
                tg = w.result(timeout=3)
        """
        return Waiter(self, pred)


class Waiter:
    def __init__(self, bus: MlBus, pred: Callable[[Telegram], bool]):
        self._bus, self._pred = bus, pred
        self._q: queue.Queue = queue.Queue()

    def _fn(self, tg: Telegram) -> None:
        if self._pred(tg):
            self._q.put(tg)

    def __enter__(self) -> "Waiter":
        self._bus.subscribe(self._fn)
        return self

    def __exit__(self, *exc) -> None:
        self._bus.unsubscribe(self._fn)

    def result(self, timeout: float) -> Optional[Telegram]:
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return None
