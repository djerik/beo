"""A music system on the bus: masterlink.MusicSystem + SourceCenter wired to the facade.

Which machine it is comes in as a profile; nothing here names one.

State flows one way only: the machine's 0x87 -> MusicSystem.state -> the facade.
Commands from the app go the other way as queued operations whose confirmation
is, again, the machine's own 0x87.
"""
from __future__ import annotations

import logging
import threading
from typing import Optional

from beo.masterlink.bus import MlBus, SerialTransport
from beo.masterlink.codec import Beo4, DEST_AUDIO, PlayState, SRC_N_MUSIC, Telegram
from beo.masterlink.music_system import MusicSystem, MusicSystemState
from beo.masterlink.profiles import MusicSystemProfile
from beo.masterlink.roles.source_center import Metadata, N_MUSIC, SourceCenter

from beo.mozart import facade

log = logging.getLogger("beo-adapter.ml")

KEYS = {"play": Beo4.PLAY, "stop": Beo4.STOP, "pause": Beo4.STOP, "skip": Beo4.STEP_UP,
        "prev": Beo4.STEP_DOWN, "cd": Beo4.CD, "standby": Beo4.STANDBY, "goto": Beo4.GOTO,
        # the manual's "next / previous CD" keys (>> / <<); sent raw
        "wind": Beo4.WIND, "rewind": Beo4.REWIND}

# facade's playback states are MozartRenderingStateValue: started | paused | stopped
STATE_NAME = {PlayState.PLAY: "started", PlayState.STOP: "paused", PlayState.WIND: "started",
              PlayState.REWIND: "started", PlayState.STANDBY: "stopped", PlayState.NO_MEDIA: "stopped",
              PlayState.UNKNOWN: "stopped"}


class MlBridge(facade.MlBackend):
    """Owns the bus, the product model and (optionally) the source-center role."""

    def __init__(self, profile: MusicSystemProfile, port: str = "/dev/ttyUSB0", *,
                 source_center: bool = True, transport=None):
        if transport is None:
            transport = SerialTransport(port)
        self.profile = profile
        self.bus = MlBus(transport)
        self.player = MusicSystem(self.bus, profile)
        self.keeper_interval_s = 4.0
        self._stop = threading.Event()
        # clock_interval_s=0: never send anything unsolicited on the bus; the machine
        # must only be woken by an explicit app or user action.
        self.source_center: Optional[SourceCenter] = (SourceCenter(self.bus, clock_interval_s=0)
                                                       if source_center else None)
        # The app's volume is a number and the machine takes steps, without
        # saying where it stands. So the number the app last set is taken as
        # where it is, and a new one is walked to from there.
        self.volume_step = 2                 # app units per press
        self.volume_form = {"src": None, "dest": DEST_AUDIO, "rel": False}
        self._volume_at: Optional[int] = None
        self._volume_target: Optional[int] = None
        self._volume_wake = threading.Event()
        # Called when the machine turns away from the network source it was
        # playing: another source was picked on it, or it was switched off.
        self.on_network_source_left = None

    def start(self) -> None:
        self.bus.start()
        self.player.on_state_changed.append(self._state_changed)
        self.player.start()
        if self.source_center:
            self.source_center.on_standby.append(self._network_source_left)
            self.source_center.on_release.append(self._network_source_left)
            self.source_center.start()
        self._stop = threading.Event()
        threading.Thread(target=self._distribution_keeper, name="ml-distribution", daemon=True).start()
        threading.Thread(target=self._volume_walker, name="ml-volume", daemon=True).start()
        log.info("MasterLink bridge up (%s, %s%s)", type(self.bus.transport).__name__, self.profile.name,
                 "" if self.profile.verified else ", not verified on the machine")

    def close(self) -> None:
        self._stop.set()
        self._volume_wake.set()
        if self.source_center:
            self.source_center.close()
        self.player.close()
        self.bus.close()

    # ---- keep the machine distributing while anyone listens ----------------
    def _listeners_present(self) -> bool:
        out = getattr(facade, "_beolink_out", None)
        try:
            return bool(out is not None and out.listeners())
        except Exception:      # noqa: BLE001
            return False

    def _distribution_keeper(self) -> None:
        """A Beo4 press selects the source LOCALLY; only the link-room request
        (0x45) makes the machine put audio on the ML pairs, and any standby or
        source change (the app's own disc selection goes through standby)
        drops it again. The machine confirms with 0x44/0x82 and the 0x08 oracle
        flips to the source within seconds. So: while listeners
        exist, ask the oracle every few seconds and re-request when needed."""
        skip = 0            # rounds to stay silent after an unanswered oracle
        backoff = 1         # doubles per unanswered round, in units of keeper_interval_s
        while not self._stop.wait(self.keeper_interval_s):
            if not self._listeners_present():
                skip, backoff = 0, 1
                continue
            if self.player.pending() or self.player.state.play_state == PlayState.STANDBY:
                continue
            if skip > 0:
                skip -= 1
                continue
            try:
                src = self.player.distributed_source(timeout=1.5)
            except Exception:      # noqa: BLE001
                continue
            if src == self._own_source():
                backoff = 1
                continue
            if src is None:
                # No answer. Asking again every round keeps the bus busy enough
                # that the machine's own statuses stop reaching us, so back off.
                skip = backoff
                backoff = min(backoff * 2, 32)
            else:
                backoff = 1
            log.warning("the machine distributes %s while listeners wait — sending the link-room request",
                        "nothing" if src == 0 else ("%02x" % src if src is not None else "? (no answer)"))
            self.player.request_distribution(self._own_source())

    # ---- volume ---------------------------------------------------------
    def _own_source(self) -> int:
        """Which of the machine's own sources it is on: the one it has said,
        and otherwise its first."""
        said = self.profile.source_coded(self.player.state.source)
        return (said or self.profile.main).code

    def _playing_source(self) -> int:
        """The source the machine is on, as far as we know: a key is addressed to it."""
        if self.source_center is not None and self.source_center.active == N_MUSIC:
            return SRC_N_MUSIC
        return self._own_source()

    def _key(self, key: int, *, src=None, dest=DEST_AUDIO, rel=False, gap_s=None) -> None:
        source = self._playing_source() if src is None else src
        self.bus.press(key, source=source, dest=dest, gap_s=gap_s)
        if rel:
            self.bus.press(Beo4.KEY_RELEASE, source=source, dest=dest, gap_s=0.05)

    @staticmethod
    def _parse_form(text: str) -> dict:
        """src=0x7a,dest=0x0f,rel -> the fields of a key telegram. src=auto follows the source."""
        form = {}
        for part in filter(None, (p.strip().lower() for p in text.split(","))):
            name, _, value = part.partition("=")
            if name == "rel":
                form["rel"] = value not in ("0", "no", "off")
            elif name in ("src", "dest"):
                form[name] = None if value == "auto" else int(value, 0)
            else:
                raise ValueError("unknown field %r: src, dest and rel are the ones there are" % name)
        return form

    def raw_key(self, spec: str) -> str:
        """Send one key with its telegram spelled out: 0x60,src=0x7a,dest=0x0f,rel"""
        key, _, rest = spec.partition(",")
        form = self._parse_form(rest)
        self._key(int(key, 0), **form)
        return "%s %s" % (key, form)

    def raw_telegram(self, body_hex: str) -> str:
        """Send one telegram given as its body in hex; the checksum is added here."""
        tg = Telegram.from_body_hex(body_hex)
        self.bus.send(tg)
        return tg.hex

    def set_volume_form(self, spec: str) -> dict:
        """How the volume keys are sent from now on."""
        self.volume_form.update(self._parse_form(spec))
        return dict(self.volume_form)

    def volume_is(self, level) -> None:
        self._volume_at = self._volume_target = int(level)

    def volume(self, level) -> None:
        if self._volume_at is None:
            self._volume_at = int(level)      # the first number is where we start counting
        self._volume_target = int(level)
        self._volume_wake.set()

    def _volume_walker(self) -> None:
        while not self._stop.is_set():
            self._volume_wake.wait()
            self._volume_wake.clear()
            while not self._stop.is_set():
                at, target = self._volume_at, self._volume_target
                if at is None or target is None or abs(target - at) < self.volume_step:
                    break
                up = target > at
                try:
                    self._key(Beo4.VOLUME_UP if up else Beo4.VOLUME_DOWN, gap_s=0.12, **self.volume_form)
                except Exception as e:      # noqa: BLE001
                    log.warning("volume key not sent: %s", e)
                    break
                self._volume_at = at + (self.volume_step if up else -self.volume_step)

    # ---- a network source into the machine ---------------------------------
    def network_source_started(self, title: str, secondary: str = "") -> bool:
        """Make the machine play what is about to arrive on the audio pairs.

        It is offered as N.MUSIC, with the title for the machine's display. True
        once the machine has asked for the source, which is when it listens to
        the pairs; until then nothing may be put on them.
        """
        if self.source_center is None:
            log.warning("no source-center role, so the machine cannot be given a network source")
            return False
        self.source_center.metadata[N_MUSIC] = Metadata(text=title, secondary=secondary)
        # Asked before it is woken: a machine that distributes nothing is off,
        # and one that comes on from off stands at the volume it starts at.
        try:
            was_off = self.player.distributed_source(timeout=1.5) == 0
        except Exception:      # noqa: BLE001
            was_off = False
        for attempt in (1, 2, 3):
            if self.source_center.wake(N_MUSIC):
                log.warning("N.MUSIC for %r: the machine asked for it", title)
                if was_off:
                    facade.machine_switched_on()
                return True
            # A machine that is on N.MUSIC already does not ask again, and one
            # that was switched off a moment ago does not hear the key yet.
            # What it says it is distributing tells the two apart.
            try:
                on = self.player.distributed_source(timeout=1.5)
            except Exception:      # noqa: BLE001
                on = None
            if on == SRC_N_MUSIC:
                log.warning("N.MUSIC for %r: the machine is on it already", title)
                self.source_center.active = N_MUSIC
                self.source_center.announce(N_MUSIC)
                return True
            log.warning("N.MUSIC for %r: no answer from the machine (attempt %d, it distributes %s)",
                        title, attempt, "?" if on is None else "%02x" % on)
            if self._stop.wait(2.0):
                break
        return False

    def network_source_playing(self, lines: list) -> None:
        """What the network source plays now, for the machine's display."""
        if self.source_center is not None:
            self.source_center.show(N_MUSIC, lines)

    def network_source_stopped(self) -> None:
        """The stream has ended. A machine still on N.MUSIC has nothing left to
        play there, so it is switched off, as a speaker that leaves a group
        goes quiet. One that has moved on to a source of its own is left alone."""
        if self._on_network_source():
            self._standby_from_network_source()

    def _on_network_source(self) -> bool:
        return self.source_center is not None and self.source_center.active == N_MUSIC

    def _standby_from_network_source(self) -> None:
        """Switch off a machine that is on N.MUSIC.

        The standby that stops it on a source of its own only makes it let go
        of N.RADIO here and leaves it on. What it obeys is the standby key
        addressed to the source it is playing, as with the volume keys; it
        answers by releasing both network sources.
        """
        self.source_center.active = None
        self._key(Beo4.STANDBY, src=SRC_N_MUSIC)
        log.info("standby key sent to the machine on N.MUSIC")

    def _network_source_left(self, source: str) -> None:
        if source == N_MUSIC and self.on_network_source_left is not None:
            log.warning("the machine left N.MUSIC")
            self.on_network_source_left()

    # ---- machine -> app ---------------------------------------------------
    def _state_changed(self, old: MusicSystemState, new: MusicSystemState) -> None:
        if new.play_state == PlayState.STANDBY and old.play_state != PlayState.STANDBY:
            # The machine went to standby (Beo4, the app, or itself): nothing is on
            # the bus any more, so drop the listeners and tell the app.
            facade.go_standby(from_app=False)
            return
        source = self.profile.source_coded(new.source)
        if source is None:
            return
        if source is not self.profile.main:
            facade.apply_ml_source(source.key)
            return
        if new.disc is None:
            return
        # With one disc, what the status carries where a changer names its
        # disc is not known, and the disc playing is the first whatever it is.
        disc = new.disc if self.profile.discs is not None else 1
        try:
            facade.apply_ml_state(disc, new.track if new.track is not None else None,
                                  STATE_NAME.get(new.play_state, "stopped"))
        except ValueError as e:
            log.warning("state not applied: %s", e)

    # ---- app -> machine (facade.MlBackend) ---------------------------------
    def press(self, key) -> None:
        if isinstance(key, int):
            self.bus.press(Beo4(key))
            return
        k = KEYS.get(key)
        if k is None:
            log.warning("unknown key %r", key)
            return
        if self._own_source() != self.profile.main.code and k != Beo4.STANDBY:
            # The queued operations wait for a status of the first source; on
            # another one a key is sent as it is, addressed to that source.
            self._key(k)
            return
        if k == Beo4.STEP_UP:
            self._log_result(self.player.next(), "next")
        elif k == Beo4.STEP_DOWN:
            self._log_result(self.player.prev(), "prev")
        elif k == Beo4.PLAY:
            self._log_result(self.player.play(), "play")
        elif k == Beo4.STOP:
            self._log_result(self.player.pause(), "pause")
        elif k == Beo4.STANDBY:
            if self._on_network_source():
                self._standby_from_network_source()
            else:
                self._log_result(self.player.standby(), "standby")
        else:
            self.bus.press(k)

    def wake(self) -> None:
        main = self.profile.main
        self.bus.press(main.select, source=main.code)

    def select_disc(self, n: int) -> None:
        if self.profile.discs is not None:
            self._log_result(self.player.select_disc(n), "select_disc(%d)" % n)
            return
        # One disc: whichever the app picks, it is the one in the machine, so
        # the machine is brought to its disc source and made to play.
        main, state = self.profile.main, self.player.state
        if state.source != main.code or state.play_state in (PlayState.STANDBY, PlayState.UNKNOWN):
            self._log_result(self.player.select_source(main), "select_disc(%d) as %s" % (n, main.key))
        elif not state.is_playing:
            self._log_result(self.player.play(), "select_disc(%d) as play" % n)
        else:
            log.info("select_disc(%d): the one disc is playing", n)

    def goto_track(self, n: int) -> None:
        self._log_result(self.player.goto_track(n), "goto_track(%d)" % n)

    def select_source(self, name: str) -> None:
        source = self.profile.source(name)
        if source is None:
            log.warning("unknown source %r", name)
            return
        self._log_result(self.player.select_source(source), "select_source(%s)" % source.key)

    def request_audio(self) -> bool:
        self.player.request_distribution(self._own_source())
        return True

    @staticmethod
    def _log_result(fut, what: str) -> None:
        def done(f):
            if f.cancelled():
                log.info("%s superseded", what)
                return
            e = f.exception()
            if e:
                log.warning("%s: %s", what, e)
            else:
                log.info("%s confirmed: %s", what, f.result())
        fut.add_done_callback(done)
