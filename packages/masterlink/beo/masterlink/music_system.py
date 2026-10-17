"""A music system as seen from the MasterLink bus.

Two rules:

  1. State comes ONLY from the machine's own 0x87 STATUS_INFO. A key press never
     changes our state; the machine broadcasts what the key actually did ~0.1 s
     later, and that is the truth. Optimistic "track+1" arithmetic drifts away
     from reality because the track count is unknown.
  2. Every status telegram is repeated ~0.1 s later with change mask 0x00. Without
     de-duplication every event counts twice. A disc change reports track 0xff
     before the new track is known; 0xff means "unchanged", never "track 255".

Operation queue: every caller — several phones through
the app facade, Music Assistant, Home Assistant — submits into ONE queue, one
worker executes one operation at a time, and an operation ends when the machine's
0x87 confirms it or on timeout. Policies per kind:

    next / prev              FIFO
    select_disc / goto_track last wins: a pending one of the same kind is replaced
    select_source            last wins
    play / pause             replace pending of the same kind
    stop / standby           clear the queue, run now

Which machine it is comes in as a profile: its sources, and how one of its
discs is selected if it has several.
"""
from __future__ import annotations

import collections
import enum
import logging
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass, replace
from typing import Callable, Deque, List, Optional

from .bus import MlBus
from .codec import (Beo4, Clock, PlayState, StatusInfo, Telegram, decode_clock, decode_display,
                    decode_distributed_source, decode_nak, decode_status_info,
                    request, link_room_source_request, release_audio_master, PL_DISTRIBUTED_SOURCE,
                    PL_NAK, PL_STATUS_INFO, SRC_CD)
from .profiles import MusicSystemProfile, Source

log = logging.getLogger("beo.masterlink.music_system")


class OperationFailed(RuntimeError):
    pass


class OpKind(enum.Enum):
    NEXT = "next"
    PREV = "prev"
    SELECT_DISC = "select_disc"
    GOTO_TRACK = "goto_track"
    SELECT_SOURCE = "select_source"
    PLAY = "play"
    PAUSE = "pause"
    STOP = "stop"
    STANDBY = "standby"


REPLACE_SAME_KIND = {OpKind.SELECT_DISC, OpKind.GOTO_TRACK, OpKind.SELECT_SOURCE, OpKind.PLAY,
                     OpKind.PAUSE}
CLEAR_QUEUE = {OpKind.STOP, OpKind.STANDBY}


@dataclass(frozen=True)
class MusicSystemState:
    source: Optional[int] = None          # 0x8d CD, 0x6f RADIO, 0xa1 N.RADIO, 0x7a N.MUSIC
    disc: Optional[int] = None            # None on a machine with no discs to choose between
    track: Optional[int] = None
    play_state: PlayState = PlayState.UNKNOWN
    empty_discs: frozenset = frozenset()
    display: str = ""
    clock: Optional[Clock] = None

    @property
    def is_playing(self) -> bool:
        return self.play_state == PlayState.PLAY


@dataclass
class Operation:
    kind: OpKind
    arg: object = None
    future: Future = None

    def __post_init__(self):
        if self.future is None:
            self.future = Future()


StateCallback = Callable[[MusicSystemState, MusicSystemState], None]


class MusicSystem:
    """The product model. Construct, then `start()`; `close()` stops the worker.

    The queued operations send their keys to the profile's first source, and
    it is that source's status that confirms them.
    """

    def __init__(self, bus: MlBus, profile: MusicSystemProfile, *, press_timeout_s: float = 3.0,
                 source_timeout_s: float = 10.0, digit_gap_s: float = 0.25):
        self.bus = bus
        self.profile = profile
        self.press_timeout_s = press_timeout_s
        self.source_timeout_s = source_timeout_s        # a source key's wait for that source's status
        self.digit_gap_s = digit_gap_s                  # between Goto digits
        self.state = MusicSystemState()
        self.on_state_changed: List[StateCallback] = []
        self.on_nak: List[Callable[[bytes], None]] = []
        self.last_nak: Optional[bytes] = None
        self._display_buf = ""
        self._last_key = None
        self._q: Deque[Operation] = collections.deque()
        self._cv = threading.Condition()
        self._stop = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self._current: Optional[Operation] = None
        self._abort_current = threading.Event()

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        self.bus.subscribe(self._on_telegram)
        self._stop.clear()
        self._worker = threading.Thread(target=self._run, name="ml-ops", daemon=True)
        self._worker.start()

    def close(self) -> None:
        self._stop.set()
        with self._cv:
            self._cv.notify_all()
        self.bus.unsubscribe(self._on_telegram)

    # ------------------------------------------------------------ incoming
    def _on_telegram(self, tg: Telegram) -> None:
        if tg.payload_type == PL_STATUS_INFO:
            si = decode_status_info(tg)
            if si is not None:
                self._apply_status(si)
            return
        if tg.payload_type == PL_NAK:
            self.last_nak = decode_nak(tg)
            log.warning("NAK from the machine, echoed: %s", self.last_nak.hex() if self.last_nak else "?")
            for fn in self.on_nak:
                fn(self.last_nak)
            return
        txt = decode_display(tg)
        if txt is not None:
            self._display_buf = (self._display_buf + txt)[-32:]
            self._set(replace(self.state, display=self._display_buf))
            return
        clk = decode_clock(tg)
        if clk is not None:
            self._set(replace(self.state, clock=clk))

    def _apply_status(self, si: StatusInfo) -> None:
        st = self.state
        if si.source == SRC_CD:
            if si.state == PlayState.NO_MEDIA:
                # An empty tray: remember it, do not move the disc pointer (ml_to_adapter).
                if si.disc is not None and si.disc not in st.empty_discs:
                    self._set(replace(st, source=si.source, empty_discs=st.empty_discs | {si.disc},
                                      play_state=si.state))
                return
            key = (si.disc, si.track, si.state)
            if key == self._last_key and si.is_echo:
                return                                  # the 0.1 s echo
            self._last_key = key
            new = replace(st, source=si.source, disc=si.disc,
                          track=st.track if si.track is None else si.track,
                          play_state=si.state,
                          empty_discs=st.empty_discs - ({si.disc} if si.disc is not None else set()))
        else:
            new = replace(st, source=si.source, play_state=si.state)
        self._set(new)

    def _set(self, new: MusicSystemState) -> None:
        old = self.state
        if new == old:
            return
        self.state = new
        for fn in list(self.on_state_changed):
            try:
                fn(old, new)
            except Exception:
                log.exception("state callback failed")

    # ------------------------------------------------------------ queue
    def submit(self, kind: OpKind, arg: object = None) -> Future:
        op = Operation(kind, arg)
        with self._cv:
            if kind in CLEAR_QUEUE:
                for pending in list(self._q):
                    pending.future.cancel()
                self._q.clear()
                self._abort_current.set()
                self._q.appendleft(op)
            elif kind in REPLACE_SAME_KIND:
                for pending in list(self._q):
                    if pending.kind == kind:
                        pending.future.cancel()
                        self._q.remove(pending)
                if self._current is not None and self._current.kind == kind:
                    self._abort_current.set()
                self._q.append(op)
            else:
                self._q.append(op)
            self._cv.notify()
        return op.future

    def pending(self) -> List[OpKind]:
        with self._cv:
            return [o.kind for o in self._q]

    def _run(self) -> None:
        while not self._stop.is_set():
            with self._cv:
                while not self._q and not self._stop.is_set():
                    self._cv.wait(0.5)
                if self._stop.is_set():
                    return
                op = self._q.popleft()
                self._current = op
                self._abort_current.clear()
            if op.future.cancelled():
                continue
            try:
                result = self._execute(op)
                if not op.future.cancelled():
                    op.future.set_result(result)
            except Exception as e:      # noqa: BLE001
                log.warning("%s failed: %s", op.kind.value, e)
                if not op.future.cancelled():
                    op.future.set_exception(e)
            finally:
                with self._cv:
                    self._current = None

    # ------------------------------------------------------------ operations
    def next(self) -> Future: return self.submit(OpKind.NEXT)
    def prev(self) -> Future: return self.submit(OpKind.PREV)
    def play(self) -> Future: return self.submit(OpKind.PLAY)
    def pause(self) -> Future: return self.submit(OpKind.PAUSE)
    def stop(self) -> Future: return self.submit(OpKind.STOP)
    def standby(self) -> Future: return self.submit(OpKind.STANDBY)
    def select_disc(self, n: int) -> Future: return self.submit(OpKind.SELECT_DISC, int(n))
    def goto_track(self, n: int) -> Future: return self.submit(OpKind.GOTO_TRACK, int(n))
    def select_source(self, source: Source) -> Future: return self.submit(OpKind.SELECT_SOURCE, source)

    def _press(self, key: Beo4, gap_s: Optional[float] = None) -> None:
        self.bus.press(key, source=self.profile.main.code, gap_s=gap_s)

    def _status_pred(self, cond: Callable[[StatusInfo], bool]) -> Callable[[Telegram], bool]:
        def pred(tg: Telegram) -> bool:
            si = decode_status_info(tg)
            return si is not None and not si.is_echo and cond(si)
        return pred

    def _press_and_confirm(self, key: Beo4, cond: Callable[[StatusInfo], bool], timeout: float,
                           what: str, gap_s: Optional[float] = None) -> MusicSystemState:
        with self.bus.expect(self._status_pred(cond)) as w:
            self._press(key, gap_s=gap_s)
            tg = w.result(timeout)
        if tg is None:
            raise OperationFailed("%s: no 0x87 confirmation within %.1f s (state %s)" % (
                what, timeout, self.state.play_state.name))
        time.sleep(0.05)          # let _on_telegram apply it before we read state
        return self.state

    def _execute(self, op: Operation):
        k = op.kind
        if k in (OpKind.NEXT, OpKind.PREV):
            # Step Down at the start of a track restarts it (classic CD-player
            # behaviour), and no 0x87 follows the press. That is not a
            # failure, and the next press must not wait 3 s behind it, or a
            # "double press" for the previous track can never happen. So: 1 s, and
            # silence means "no change".
            key = Beo4.STEP_UP if k == OpKind.NEXT else Beo4.STEP_DOWN
            try:
                return self._press_and_confirm(key, lambda s: True, min(self.press_timeout_s, 1.0), k.value)
            except OperationFailed:
                log.info("%s: no 0x87 within 1 s — the machine did not change track", k.value)
                return self.state
        if k == OpKind.PLAY:
            return self._press_and_confirm(Beo4.PLAY, lambda s: s.state == PlayState.PLAY,
                                           self.press_timeout_s, "play")
        if k == OpKind.PAUSE or k == OpKind.STOP:
            # The machine reports stop and pause as the same state (0x01).
            return self._press_and_confirm(Beo4.STOP, lambda s: s.state == PlayState.STOP,
                                           self.press_timeout_s, k.value)
        if k == OpKind.STANDBY:
            return self._standby()
        if k == OpKind.SELECT_DISC:
            return self._select_disc(int(op.arg))
        if k == OpKind.GOTO_TRACK:
            return self._goto_track(int(op.arg))
        if k == OpKind.SELECT_SOURCE:
            return self._select_source(op.arg)
        raise OperationFailed("unknown operation %s" % k)

    def _select_source(self, source: Source) -> MusicSystemState:
        """The source's own key, addressed to that source, and a 0x87 that names
        it as the answer. Any such status counts, repeated or not: which change
        bits the status of a source without discs carries is not known."""
        def pred(tg: Telegram) -> bool:
            si = decode_status_info(tg)
            return si is not None and si.source == source.code
        with self.bus.expect(pred) as w:
            self.bus.press(source.select, source=source.code)
            tg = w.result(self.source_timeout_s)
        if tg is None:
            raise OperationFailed("select_source(%s): no 0x87 for it within %.1f s (source %s)" % (
                source.key, self.source_timeout_s,
                "?" if self.state.source is None else "%02x" % self.state.source))
        time.sleep(0.05)
        return self.state

    def _standby(self) -> MusicSystemState:
        """Switch the machine off with a RELEASE that names no source. Its answer
        is a 0x87 without the media flag and with no change bits, so it reads as
        an echo; the flag is what tells it from a status of a playing machine.
        A pause gives the same answer, so the bus cannot tell the two apart."""
        def pred(tg: Telegram) -> bool:
            si = decode_status_info(tg)
            return si is not None and not si.media_loaded
        with self.bus.expect(pred) as w:
            self.bus.send(release_audio_master())
            tg = w.result(4.0)
        if tg is None:
            log.warning("standby: no 0x87 after the RELEASE (was it already off?)")
        else:
            log.info("standby: the machine switched off (%s)", tg)
        time.sleep(0.1)
        self._set(replace(self.state, play_state=PlayState.STANDBY))
        return self.state

    def _select_disc(self, n: int) -> MusicSystemState:
        """A bare digit changes the disc while the machine is listening on the
        source, so that is tried first. When the digit goes unanswered the
        machine is taken off the source and brought back: Standby, source key,
        digit. That opens the window in which a digit means disc even when the
        machine was not listening. An empty slot is skipped, not refused: the
        machine stays where it is or goes to another disc, and that is reported
        as failure."""
        discs = self.profile.discs
        if discs is None:
            raise OperationFailed("select_disc(%d): %s has no discs to choose between"
                                  % (n, self.profile.name))
        if not 1 <= n <= discs.count:
            raise OperationFailed("disc must be 1..%d" % discs.count)
        main = self.profile.main
        if self.state.source != main.code and self.profile.source_coded(self.state.source) is not None:
            # On another source a digit is that source's own, so this one is picked first.
            self._press(main.select)
            time.sleep(discs.digit_delay_s)
        if self._digit_then_disc(n, discs.digit_timeout_s):
            return self.state
        log.info("select_disc(%d): no answer to the bare digit, taking the standby route", n)
        self._press(Beo4.STANDBY)
        if self._abort_current.wait(discs.standby_delay_s):
            raise OperationFailed("select_disc(%d) superseded" % n)
        self._press(main.select, gap_s=0)
        time.sleep(discs.digit_delay_s)
        if self._digit_then_disc(n, discs.confirm_timeout_s):
            return self.state
        raise OperationFailed("select_disc(%d): the machine did not report disc %d within %.1f s (slot empty?)"
                              % (n, n, discs.confirm_timeout_s))

    def _digit_then_disc(self, n: int, timeout: float) -> bool:
        """Press the digit and wait for a 0x87 with disc n and a known track.
        False on silence; a disc change to another disc is a failure, because
        the machine heard the digit and had nothing in slot n.

        The machine names the new disc at once and its track only when the
        mechanics are done, which can take longer than the wait. Disc n with
        the track still unknown therefore counts when the wait ends without
        the machine moving on to another disc."""
        named = False
        with self.bus.expect(self._status_pred(lambda s: s.disc is not None)) as w:
            self._press(Beo4(n), gap_s=0)
            deadline = time.monotonic() + timeout
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    return named
                tg = w.result(min(left, 0.2))
                if tg is None:
                    if self._abort_current.is_set():
                        raise OperationFailed("select_disc(%d) superseded" % n)
                    continue
                si = decode_status_info(tg)
                if si and si.disc == n and si.track is not None:
                    time.sleep(0.05)
                    return True
                if si and si.disc == n:
                    named = True
                if si and si.changed_disc and si.disc != n:
                    raise OperationFailed("select_disc(%d): the machine went to disc %d instead" % (n, si.disc))

    def _goto_track(self, n: int) -> MusicSystemState:
        """Goto (0x20) then the digits: two digits are one two-digit track number."""
        if n < 1:
            raise OperationFailed("track must be >= 1")
        digits = [int(c) for c in str(n)]
        with self.bus.expect(self._status_pred(lambda s: s.track == n)) as w:
            self._press(Beo4.GOTO)
            for d in digits:
                self._press(Beo4(d), gap_s=self.digit_gap_s)
            tg = w.result(self.press_timeout_s + 2)
        if tg is None:
            raise OperationFailed("goto_track(%d): not confirmed by 0x87" % n)
        time.sleep(0.05)
        return self.state

    # ------------------------------------------------------------ audio on the bus
    def request_distribution(self, source: Optional[int] = None) -> None:
        """Make the machine put a source's audio onto the ML pairs (link-room request).
        Needed before capturing its audio with the ADC; see codec.link_room_source_request."""
        self.bus.send(link_room_source_request(self.profile.main.code if source is None else source))

    # ------------------------------------------------------------ queries
    def distributed_source(self, timeout: float = 2.0) -> Optional[int]:
        """REQUEST_DISTRIBUTED_SOURCE (0x08): the one oracle for a source center's
        source. Returns the source byte, 0 for none, None on timeout."""
        def pred(tg: Telegram) -> bool:
            return tg.payload_type == PL_DISTRIBUTED_SOURCE and decode_distributed_source(tg) is not None
        with self.bus.expect(pred) as w:
            self.bus.send(request(PL_DISTRIBUTED_SOURCE))
            tg = w.result(timeout)
        return None if tg is None else decode_distributed_source(tg)
