"""The MasterLink port on a MasterDataTool v2.

The v2 interface puts a microcontroller between the Pi and the bus. The
controller does the 9-bit framing, the checksum and the end byte, and drops
the echo of what it sent, so none of the serial tricks the v1 needs apply.
The Pi talks to it on its own UART at 115200 baud, 8N1, in frames:

    55 AA  <channel>  <length>  <payload ...>  <crc lo> <crc hi>

The CRC is CRC-16/CCITT-FALSE (polynomial 0x1021, start 0xFFFF) over the
channel, the length and the payload. A payload is at most 128 bytes.

Channels:
    00  MasterLink. To the controller: a telegram without its checksum and
        end byte, which the controller adds. From it: the whole telegram.
    20  the board's pins, one state byte: bit 0 power out on the bus, bit 1
        power seen on the bus, bit 2 bus master, bit 3 the DAC playing.
    FE  a line of text from the controller's own log.
    FF  ping; the answer starts with "PONG" and carries the firmware version.

Nothing here has been run against a v2 board.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import List, Optional, Tuple

from .bus import MlTransport, Subscriber
from .codec import Telegram, TelegramError

log = logging.getLogger("beo.masterlink.mdt2")

SYNC = b"\x55\xaa"
CHAN_ML = 0x00
CHAN_GPIO = 0x20
CHAN_LOG = 0xFE
CHAN_PING = 0xFF
MAX_PAYLOAD = 128
FRAME_OVERHEAD = len(SYNC) + 2 + 2     # sync, channel, length, crc

GPIO_BITS = ((0x01, "power out"), (0x02, "power on bus"), (0x04, "master"), (0x08, "DAC playing"))


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def frame(chan: int, payload: bytes = b"") -> bytes:
    if len(payload) > MAX_PAYLOAD:
        raise ValueError("payload of %d bytes is over the %d the controller takes" % (len(payload), MAX_PAYLOAD))
    inner = bytes([chan & 0xFF, len(payload)]) + bytes(payload)
    crc = crc16(inner)
    return SYNC + inner + bytes([crc & 0xFF, crc >> 8])


class FrameParser:
    """Frames out of the byte stream, whatever chunks it arrives in.

    Bytes before a sync are dropped. A frame whose CRC does not match is
    skipped from its sync on, so a sync inside a corrupt frame is found.
    """

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, chunk: bytes) -> List[Tuple[int, bytes]]:
        self._buf.extend(chunk)
        out: List[Tuple[int, bytes]] = []
        while True:
            i = self._buf.find(SYNC)
            if i < 0:
                keep = 1 if self._buf.endswith(SYNC[:1]) else 0   # its AA may be in the next chunk
                del self._buf[:len(self._buf) - keep]
                return out
            if i:
                log.debug("dropped %d bytes before a frame: %s", i, bytes(self._buf[:i]).hex())
                del self._buf[:i]
            if len(self._buf) < 4:
                return out
            n = self._buf[3]
            if n > MAX_PAYLOAD:
                del self._buf[:len(SYNC)]
                continue
            end = 4 + n + 2
            if len(self._buf) < end:
                return out
            inner = bytes(self._buf[2:4 + n])
            crc = self._buf[end - 2] | (self._buf[end - 1] << 8)
            if crc != crc16(inner):
                log.debug("frame with a bad crc on channel %02x, %d bytes", inner[0], n)
                del self._buf[:len(SYNC)]
                continue
            out.append((inner[0], inner[2:]))
            del self._buf[:end]


def describe_pong(payload: bytes) -> str:
    """The firmware's answer to a ping, in words: version, what it does, and
    whether it is bus master. Older firmware answers with "PONG" alone."""
    if not payload.startswith(b"PONG"):
        return "unexpected answer %s" % payload.hex()
    if len(payload) < 8:
        return "firmware without a version"
    major, minor, patch, caps = payload[4:8]
    text = "firmware %d.%d.%d" % (major, minor, patch)
    links = [name for bit, name in ((0x01, "ML"), (0x02, "DL86"), (0x04, "DL80"), (0x08, "GPIO")) if caps & bit]
    if links:
        text += ", " + "/".join(links)
    rest = payload[8:]
    if (major, minor) >= (1, 4) and rest:
        text += ", " + ("master" if rest[0] else "slave")
        rest = rest[1:]
    build = rest.decode("ascii", "replace").strip()
    return text + (", build " + build if build else "")


class Mdt2Transport(MlTransport):
    """Owns the UART to the v2 controller. /dev/serial0 once the Pi's UART is
    turned on and Bluetooth is moved off it."""

    def __init__(self, port: str = "/dev/serial0", baud: int = 115200, *, echo_window_s: float = 2.0):
        self.port, self.baud = port, baud
        self.echo_window_s = echo_window_s
        self._ser = None
        self._on: Optional[Subscriber] = None
        self._stop = threading.Event()
        self._reader: Optional[threading.Thread] = None
        self._wlock = threading.Lock()
        self._sent: List[tuple] = []      # (raw, t): the controller drops echoes, this is in case one slips through

    def start(self, on_telegram: Subscriber) -> None:
        import serial
        ser = serial.Serial(self.port, self.baud, timeout=0.05)
        ser.reset_input_buffer()
        self._ser = ser
        self._on = on_telegram
        self._stop.clear()
        self._reader = threading.Thread(target=self._read_loop, name="ml-mdt2", daemon=True)
        self._reader.start()
        self._write(frame(CHAN_PING))
        log.info("MasterDataTool v2 on %s @ %d", self.port, self.baud)

    def _read_loop(self) -> None:
        parser = FrameParser()
        while not self._stop.is_set():
            try:
                chunk = self._ser.read(256)
            except Exception as e:      # port gone
                log.error("serial read failed: %s", e)
                time.sleep(1.0)
                continue
            if chunk:
                for chan, payload in parser.feed(chunk):
                    self._on_frame(chan, payload)

    def _on_frame(self, chan: int, payload: bytes) -> None:
        if chan == CHAN_ML:
            try:
                tg = Telegram.parse(payload)
            except TelegramError as e:
                log.debug("controller passed a telegram that does not parse (%s): %s", e, payload.hex())
                return
            if self._is_echo(tg, time.monotonic()):
                return
            try:
                self._on(tg)
            except Exception:
                log.exception("subscriber failed on %s", tg)
        elif chan == CHAN_PING:
            log.info("controller: %s", describe_pong(payload))
        elif chan == CHAN_LOG:
            log.info("controller: %s", payload.decode("ascii", "replace").rstrip())
        elif chan == CHAN_GPIO and payload:
            log.debug("board pins: %s", ", ".join(n for bit, n in GPIO_BITS if payload[0] & bit) or "none set")

    def _is_echo(self, tg: Telegram, now: float) -> bool:
        with self._wlock:
            self._sent = [(r, t) for r, t in self._sent if now - t < self.echo_window_s]
            for i, (r, t) in enumerate(self._sent):
                if r == tg.raw:
                    del self._sent[i]
                    return True
        return False

    def send(self, telegram: Telegram) -> None:
        data = frame(CHAN_ML, telegram.body)
        with self._wlock:
            self._write(data)
            self._sent.append((telegram.raw, time.monotonic()))
        log.debug(">> %s", telegram)

    def _write(self, data: bytes) -> None:
        self._ser.write(data)
        self._ser.flush()

    def close(self) -> None:
        self._stop.set()
        if self._reader:
            self._reader.join(timeout=1.0)
        if self._ser:
            try:
                self._ser.close()
            except Exception:
                pass
