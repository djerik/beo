"""Be a MasterLink SourceCenter (address 0xC2): offer N.RADIO / N.MUSIC to a music system.

This is the ML counterpart of a NetworkLink listener role: audio INTO the audio
master (address 0xC1), with the master's own display and Beo4 as the user
interface. The sequence:

    us     -> master  Beo4 N.RADIO (0x93) / N.MUSIC (0x94) press, sent as 0xC2   "wake"
    master -> us      REQUEST 0x6c for the source, about half a second later
    us     -> master  0x6c reply, DISPLAY_SOURCE "N.RADIO", STATUS_INFO, TRACK_INFO_LONG,
                      what is playing as 12 characters of display text, line
                      by line, a line that is wider scrolled across once,
                      EXTENDED_SOURCE_INFORMATION (name/title, genre, country/album)
    master            reports the source as distributed to a 0x08 query, shows the text
                      on its display, and plays whatever we put on the ML audio pairs
    master -> us      STANDBY 0x10 for our source when the user picks another source
    master -> us      RELEASE 0x11 for 7a and a1 when the master powers off

The audio master never volunteers anything else about a source center's source:
no 0x87, no display echo, no NAK. It also does NOT forward Beo4 next/prev
presses made at the master for our source; the on_key callback is wired for
link-room nodes, which do forward them as 0x0d.

Telegram templates are byte-exact.
"""
from __future__ import annotations

import logging
import re
import threading
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Dict, Iterator, List, Optional, Tuple

from ..bus import MlBus
from ..codec import (Beo4, Telegram, beo4_press, encode_clock, decode_beo4, SOURCE_CENTER,
                     PL_DISTRIBUTION_REQUEST, PL_STANDBY, PL_RELEASE, PL_BEO4_KEY, PL_MLGW_BEO4,
                     REQ, SRC_N_RADIO, SRC_N_MUSIC)

log = logging.getLogger("beo.masterlink.source_center")

N_RADIO, N_MUSIC = "N.RADIO", "N.MUSIC"
SOURCES: Dict[str, int] = {N_RADIO: SRC_N_RADIO, N_MUSIC: SRC_N_MUSIC}
WAKE_KEY: Dict[str, Beo4] = {N_RADIO: Beo4.N_RADIO, N_MUSIC: Beo4.N_MUSIC}

# EXTENDED_SOURCE_INFORMATION subtypes, as the audio master's display shows them
#   radio:  2 genre, 3 country, 4 RDS / station name
#   music:  1 genre, 3 album, 4 title
SUBTYPE_TEXT = 4
SUBTYPE_SECONDARY = 3
SUBTYPE_GENRE = 2
SUBTYPE_MUSIC_GENRE = 1


@dataclass
class Metadata:
    text: str = ""           # station name (radio) / title (music)
    secondary: str = ""      # country (radio) / album (music)
    genre: str = ""
    lines: Tuple[str, ...] = ()   # what the display goes through in turn; the text alone if empty


def _ascii(s: str, n: Optional[int] = None) -> bytes:
    b = s.encode("ascii", "replace")
    if n is not None:
        b = b[:n].ljust(n, b" ")
    return b


DISPLAY_TEXT_MAX = 15
DISPLAY_WIDTH = 12
DISPLAY_LINE_MAX = 64


def _display_text(s: str, n: int = DISPLAY_TEXT_MAX) -> bytes:
    """Text as the audio master's display takes it: letters, digits, '.', '-'
    and space, at most n characters. An accented letter is sent as its base
    letter; anything else outside the set is left out."""
    folded = unicodedata.normalize("NFKD", s)
    return re.sub(r"[^A-Za-z0-9.\- ]", "", folded)[:n].encode("ascii")


class SourceCenter:
    def __init__(self, bus: MlBus, *, auto_announce: bool = True, gap_s: float = 0.25,
                 clock_interval_s: float = 1800.0, page_s: float = 4.0,
                 scroll_step_s: float = 0.2, scroll_hold_s: float = 2.0):
        self.bus = bus
        self.auto_announce = auto_announce
        self.gap_s = gap_s
        self.clock_interval_s = clock_interval_s
        # How long a line that fits stands, and how a wider one moves: still
        # at each end, one character at a time in between.
        self.page_s, self.scroll_step_s, self.scroll_hold_s = page_s, scroll_step_s, scroll_hold_s
        self._display_changed = threading.Event()
        self.metadata: Dict[str, Metadata] = {N_RADIO: Metadata(), N_MUSIC: Metadata()}
        self.active: Optional[str] = None
        self.on_request: List[Callable[[str], None]] = []
        self.on_standby: List[Callable[[str], None]] = []
        self.on_release: List[Callable[[str], None]] = []
        self.on_key: List[Callable[[int, int], None]] = []
        self._clock_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        self.bus.subscribe(self._on_telegram)
        self._stop.clear()
        threading.Thread(target=self._display_loop, name="ml-display", daemon=True).start()
        if self.clock_interval_s > 0:
            self._clock_thread = threading.Thread(target=self._clock_loop, name="ml-clock", daemon=True)
            self._clock_thread.start()

    def close(self) -> None:
        self._stop.set()
        self._display_changed.set()
        self.bus.unsubscribe(self._on_telegram)

    # ------------------------------------------------------------ bus in
    def _on_telegram(self, tg: Telegram) -> None:
        if tg.to not in (SOURCE_CENTER, 0xC3):
            return
        name = self._name(tg.dst_source)
        if tg.typ == REQ and tg.payload_type == PL_DISTRIBUTION_REQUEST and name:
            log.info("the music system asks for %s", name)
            self.active = name
            for fn in self.on_request:
                fn(name)
            if self.auto_announce:
                self.announce(name)
            return
        if tg.payload_type == PL_STANDBY and name:
            log.info("STANDBY for %s (the user switched away)", name)
            if self.active == name:
                self.active = None
            for fn in self.on_standby:
                fn(name)
            return
        if tg.payload_type == PL_RELEASE and name:
            log.info("RELEASE for %s", name)
            if self.active == name:
                self.active = None
            for fn in self.on_release:
                fn(name)
            return
        if tg.payload_type in (PL_BEO4_KEY, PL_MLGW_BEO4):
            k = decode_beo4(tg)
            if k:
                for fn in self.on_key:
                    fn(k[0], k[1])

    @staticmethod
    def _name(src: int) -> Optional[str]:
        for n, b in SOURCES.items():
            if b == src:
                return n
        return None

    # ------------------------------------------------------------ bus out
    def wake(self, source: str = N_RADIO, timeout: float = 6.0) -> bool:
        """Press the N.* key as source center and wait for the audio master's 0x6c request.

        The request arrives about half a second after the press. With
        auto_announce the announce runs from the bus thread as soon as it does.
        """
        src = SOURCES[source]

        def pred(tg: Telegram) -> bool:
            return (tg.to in (SOURCE_CENTER, 0xC3) and tg.typ == REQ
                    and tg.payload_type == PL_DISTRIBUTION_REQUEST and tg.dst_source == src)
        with self.bus.expect(pred) as w:
            self.bus.send(beo4_press(WAKE_KEY[source], source=0x6F, frm=SOURCE_CENTER))
            tg = w.result(timeout)
        if tg is None:
            log.warning("no 0x6c request for %s within %.1f s — does this unit support N.* sources?",
                        source, timeout)
            return False
        return True

    def announce(self, source: str) -> None:
        """The reply + announce sequence, byte-exact."""
        src = SOURCES[source]
        s = "%02x" % src
        md = self.metadata[source]
        seq = [
            "c1c2011400" + s + "006c010801",                                # 0x6c: distributing it
            "83c2012c00" + s + "00061100030101" + "0000" + _ascii(source, 12).hex(),   # DISPLAY_SOURCE
        ]
        if source == N_RADIO:
            seq.append("83c2011400a100871a04a10100001fbe01000000ff020100030101010300020000000001")
        else:
            seq.append("83c20114007a00871f047a0100001fbe01000000ff0201000301010103000200000000010000000000")
        seq.append("c1c20114000000820a0106" + s + "0002000000000001")        # TRACK_INFO_LONG
        for tg_hex in seq:
            self.bus.send(Telegram.from_body_hex(tg_hex))
            time.sleep(self.gap_s)
        self._send_metadata(source, md)
        self._display_changed.set()

    def show(self, source: str, lines) -> None:
        """What the display goes through from now on while the source plays."""
        md = self.metadata[source]
        if tuple(lines) != md.lines:
            md.lines = tuple(lines)
            self._display_changed.set()

    # An audio master with a 12-character display shows only the display-text
    # form and ignores the extended one; a newer one takes the extended form.
    def _display_loop(self) -> None:
        while not self._stop.is_set():
            self._display_changed.wait()
            self._display_changed.clear()
            try:
                self._run_display()
            except Exception:
                log.exception("display text failed")

    def _run_display(self) -> None:
        """Send the active source's lines in turn until they change or it stops."""
        source = self.active
        if source is None:
            return
        md = self.metadata[source]
        lines = [t for t in (_display_text(l, DISPLAY_LINE_MAX).decode() for l in md.lines or (md.text,)) if t]
        if not lines:
            return
        still = len(lines) == 1 and len(lines[0]) <= DISPLAY_WIDTH
        while True:
            for line in lines:
                for text, hold in self._frames(line):
                    if self.active != source or self._stop.is_set() or self._display_changed.is_set():
                        return
                    self.bus.send(self.display_text(SOURCES[source], text))
                    if still or self._display_changed.wait(hold):
                        return

    def _frames(self, line: str) -> Iterator[Tuple[str, float]]:
        if len(line) <= DISPLAY_WIDTH:
            yield line, self.page_s
            return
        last = len(line) - DISPLAY_WIDTH
        for i in range(last + 1):
            yield line[i:i + DISPLAY_WIDTH], self.scroll_hold_s if i in (0, last) else self.scroll_step_s

    def _send_metadata(self, source: str, md: Metadata) -> None:
        src = SOURCES[source]
        items = [(SUBTYPE_TEXT, md.text), (SUBTYPE_SECONDARY, md.secondary),
                 (SUBTYPE_GENRE if source == N_RADIO else SUBTYPE_MUSIC_GENRE, md.genre)]
        for subtype, text in items:
            if not _display_text(text):
                continue
            self.bus.send(self.ext_source_info(src, subtype, text))
            time.sleep(self.gap_s)

    @staticmethod
    def display_text(src: int, text: str) -> Telegram:
        """Free text for a 12-character display, in place of the source's name."""
        s = _display_text(text, DISPLAY_WIDTH).ljust(DISPLAY_WIDTH)
        return Telegram.from_body_hex("83c2012c00" + "%02x" % src + "000611000302010000" + s.hex())

    @staticmethod
    def ext_source_info(src: int, subtype: int, text: str) -> Telegram:
        """EXTENDED_SOURCE_INFORMATION (0x0b) as a source center sends it: payload length = 14 + len(text)."""
        s = _display_text(text)
        body = ("83c2012c00" + "%02x" % src + "000b" + "%02x" % (14 + len(s)) + "00" + "%02x" % subtype
                + "000301" + "%02x" % src + "00000003e700010001")
        return Telegram.from_body_hex(body + s.hex())

    def release(self, source: str) -> None:
        """Tell the audio master our source is gone (mirror of the master's own RELEASE form; unverified)."""
        src = SOURCES[source]
        self.bus.send(Telegram.from_body_hex("c1c2010a" + "%02x" % src + "0000110002"))
        if self.active == source:
            self.active = None

    def send_clock(self, when: Optional[datetime] = None) -> None:
        when = when or datetime.now()
        self.bus.send(encode_clock(when.hour, when.minute, when.second, when.day, when.month, when.year % 100))

    def _clock_loop(self) -> None:
        while not self._stop.wait(self.clock_interval_s):
            try:
                self.send_clock()
            except Exception:
                log.exception("clock sync failed")
