#!/usr/bin/env python3
"""
What the Bang & Olufsen app talks to: a Mozart product over REST, WebSocket
and mDNS.

What the product is comes from the ProductProfile the application passes to
run(): a plain speaker by default, or for instance a Beolab 8 with a Classics
Adapter attached, for which the app runs its adapter check to completion and
exposes CD 1-6 as sources. What the app requires of each answer is in
docs/app-protocol.md; comments below point at it by section.

Every request is logged so unhandled endpoints show up as UNHANDLED.

Only run this on your own LAN against your own app.
Requires: zeroconf
"""
import argparse
import json
import logging
import os
import re
import socket
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from zeroconf import Zeroconf, ServiceInfo

from beo.product.identity import SERIAL_FILE, load_serial

from .profile import ProductProfile

from .ws import NotificationServer

LOG_PATH = os.environ.get("BEO_LOG", os.path.join(os.path.expanduser("~"), "beo-mozart.log"))
log = logging.getLogger("beo.mozart.facade")
CRLF = b'\r\n'

# ---- Bonjour ----------------------------------------------------------------
# The app browses four fixed service types; this is the Mozart one, and
# _mozart._tcp. is not among them (docs/app-protocol.md §1).
SERVICE_TYPE = "_bangolufsen._tcp.local."

# The app's liveness ping; it marks the product offline without an answer
# here (docs/app-protocol.md §1).
PING_PORT = 8080

# Publicly trusted HTTPS, as the app requires; it refuses self-signed hosts.
# None of it shows for this source: the app draws its own disc graphic
# (docs/app-protocol.md §7).
ART_URLS = {
    1: "https://coverartarchive.org/release-group/8e8a594f-2175-38c7-a871-abb68ec363e7/front-500",
    2: "https://coverartarchive.org/release-group/f5093c06-23e3-404f-aeaa-40f72885ee3a/front-500",
    3: "https://coverartarchive.org/release-group/c63baaaa-10c0-4b86-ad7e-1f39d7e2eccc/front-500",
    4: "https://coverartarchive.org/release-group/416bb5e5-c7d1-3977-8fd7-7c9daf6c2be6/front-500",
    5: "https://coverartarchive.org/release-group/dcff25f1-702d-3b5e-b0da-d48172e6e62a/front-500",
    6: "https://coverartarchive.org/release-group/49081a6f-8f76-3159-aba2-8527126d9384/front-500",
}

# State changes to the app (docs/app-protocol.md §8).
WS_PORT = 9339

notifier = NotificationServer(WS_PORT)

# ---- Emulated identity ------------------------------------------------------
# The type number decides which product the app renders; the item number gates
# the Classics Adapter check. Why a Beolab 8 carrying the adapter's item
# number: docs/app-protocol.md §2.
#
# What this product is, in one object. The default is a plain speaker; an
# application replaces it wholesale by passing profile= to run(). Everything
# derived from it is set in _apply_profile below, so a replacement takes effect
# everywhere at once.
PROFILE: ProductProfile = ProductProfile()

TYPE_NUMBER = PROFILE.type_number
ITEM_NUMBER = PROFILE.item_number

# The serial is the third field of the jid, and the jid is the app's primary key
# for this product. It is derived once from the machine and persisted, never
# randomised: see beo.product.identity for why, and for the band it sits in.
SERIAL = load_serial()
# Must be a value the peers recognise, and must agree with /BeoDevice. Making
# it internally consistent instead killed the connection.
PRODUCT_TYPE = PROFILE.product_type
FRIENDLY = PROFILE.friendly
# Must parse as a .NET System.Version, or the app crashes outright
# (docs/app-protocol.md §1).
FIRMWARE = PROFILE.firmware
def _real_mac():
    """The machine's own MAC.

    An invented address meant we presented one hardware id over mDNS and a
    different one on the BeoLink side: two devices, seen from outside.
    """
    n = uuid.getnode()
    return ":".join(f"{(n >> b) & 0xFF:02X}" for b in range(40, -8, -8))


MAC = _real_mac()
# Set from the product name and the serial. It must be the SAME name the
# BeoLink side announces, or we appear on the network as two hosts.
HOSTNAME = PROFILE.host_prefix + "-" + SERIAL + ".local."

# Still read in a few places that are about playing a disc rather than about
# describing the product. Those belong with the backend and move next.
CD_COUNT = getattr(PROFILE, "DISC_COUNT", 0)
CLASSICS_PREFIX = getattr(PROFILE, "PREFIX", "")

# Which disc and track is playing. A REST snapshot that never changes is
# indistinguishable from a product that ignores you, so activation updates this
# and announces it over the WebSocket.
#
# "source" is the slot of the plain source the machine is on, or None while it
# is on its discs.
_active = {"cd": 1, "track": 1, "progress": 0, "state": "idle", "source": None}

TRACK_SECONDS = 240

# Who else is on the network: asked of the application, never configured and
# never cached (docs/app-protocol.md §9).
def _no_peers():
    return []


_peers_provider = _no_peers


def set_peers_provider(fn):
    """Give the facade a way to ask who is out there. fn() -> [dict].

    Each dict carries jid, friendlyName, ipAddress, productType and
    audioTransport. An empty list is a valid answer and means nobody has been
    heard from yet.
    """
    global _peers_provider
    _peers_provider = fn or _no_peers


def peers():
    """Everyone currently known, asked fresh every time.

    Never cached here: a product that was switched off a minute ago must stop
    being offered, and a product switched on must appear without a restart.
    """
    try:
        return list(_peers_provider() or [])
    except Exception:                                   # noqa: BLE001
        log.exception("the peer directory failed; reporting nobody")
        return []


def peer_by_jid(jid):
    for p in peers():
        if p.get("jid") == jid:
            return p
    return {}

# ---- the way out to the real BeoSound 9000 -------------------------------
# What the app asks for goes out on Master Link as a virtual Beo4 key press,
# through the MasterLink backend. The telegram forms live in beo.masterlink.
_ml = None                      # an MlBackend, or None without a machine


class MlBackend:
    """What the facade needs from the MasterLink side. An application
    implements it on top of beo.masterlink and passes it to run()."""

    def press(self, key):
        """key: "play" "stop" "skip" "prev" "cd" "standby" "goto" or a digit."""

    def wake(self):
        """Bring the machine to its first source, the one it is switched on to."""

    def select_disc(self, n):
        pass

    def goto_track(self, n):
        pass

    def select_source(self, slot):
        """Switch the machine to the source in this slot."""

    def request_audio(self):
        return False

    def volume(self, level):
        """The app set the volume to this level (0..maximum)."""

    def volume_is(self, level):
        """The machine stands at this level now; nothing is to be sent."""


def set_ml_backend(backend):
    global _ml
    _ml = backend


def ml_send(*keys, gap=0.35):
    """Send one or more Beo4 keys to the 9000. Silent no-op without a machine."""
    if _ml is None:
        return
    for key in keys:
        try:
            _ml.press(key if isinstance(key, str) else int(key))
            log.info("     -> ML: Beo4 %s", key)
        except Exception as e:
            log.warning("     -> ML: could not send %s (%s)", key, e)
            return


def ml_wake():
    """Bring the machine to its first source. Silent no-op without a machine."""
    if _ml is None:
        return
    try:
        _ml.wake()
        log.info("     -> ML: woken on its first source")
    except Exception as e:
        log.warning("     -> ML: could not wake the machine (%s)", e)


def ml_select_source(slot):
    """Bring the machine to this source, unless it plays it already: asked
    again, a machine starts the source over. True when the machine was
    brought to it here, so that whoever asked knows the machine is theirs to
    switch off. Silent no-op without a machine."""
    if _ml is None:
        return False
    if _active["source"] == slot and _active["state"] == "play":
        log.info("     -> ML: already playing %s", slot)
        return False
    try:
        _ml.select_source(slot)
        log.info("     -> ML: brought to %s", slot)
        return True
    except Exception as e:
        log.warning("     -> ML: could not bring the machine to %s (%s)", slot, e)
        return False


def plain_source(slot):
    """The product's plain source in a slot, or None: for the discs too."""
    for source in PROFILE.plain_sources:
        if source.slot == slot:
            return source
    return None


def ml_select_disc(n):
    """Change to disc n. A bare digit does it while the 9000 listens on CD; the
    standby, CD, digit route is the fallback. Both live in masterlink.MusicSystem."""
    log.info("     -> ML: changing to disc %d", n)
    if _ml is not None:
        _ml.select_disc(int(n))


# Whoever the app has told us to expand to. The app reads the result out of
# our own listener list, so it has to be remembered (docs/app-protocol.md §9).
_beolink_listeners: list[str] = []

# Starts in standby: the bus does not say whether a silent 9000 is on, and a
# product shown as "on" gets a standby press from the app's power button. The
# first status telegram with a playing/stopped state flips it
# (docs/app-protocol.md §5).
_power = {"value": "standby"}

# The volume a machine starts at when it is switched on is stored in the
# machine, and the machine does not say what it is. So the app's "default
# volume" is where the owner states it, and it is kept across restarts.
VOLUME_FILE = os.environ.get("BEO_VOLUME_FILE", "/etc/beo/volume-default")


def _stored_default(fallback=30):
    try:
        with open(VOLUME_FILE, encoding="utf-8") as f:
            return int(f.read().split()[0])
    except (OSError, ValueError, IndexError):
        return fallback


# level/maximum/default/muted, and no "range": followers are scaled against
# maximum.
_volume = {"level": 30, "maximum": 100, "default": _stored_default(), "muted": False}


def volume_state():
    return {"level": {"level": _volume["level"]}, "maximum": {"level": _volume["maximum"]},
            "default": {"level": _volume["default"]}, "muted": {"muted": _volume["muted"]}}


# Whoever turns the level into sound on an output of the product's own. A
# machine is told through its backend instead, in the steps it takes.
_volume_listener = None


def set_volume_listener(listener):
    """listener(level, muted) is told where the volume stands, now and at
    every change."""
    global _volume_listener
    _volume_listener = listener
    listener(_volume["level"], _volume["muted"])


def _volume_changed():
    notifier.send("WebSocketEventVolume", volume_state())
    if _volume_listener is not None:
        _volume_listener(_volume["level"], _volume["muted"])


def set_volume(level=None, muted=None):
    changed = False
    if level is not None and int(level) != _volume["level"]:
        _volume["level"] = max(0, min(int(level), _volume["maximum"]))
        changed = True
        if _ml is not None:
            _ml.volume(_volume["level"])
    if muted is not None and bool(muted) != _volume["muted"]:
        _volume["muted"] = bool(muted)
        changed = True
    if changed:
        _volume_changed()
    return volume_state()


def set_default_volume(level):
    """The level the machine starts at, as the owner states it in the app."""
    _volume["default"] = max(0, min(int(level), _volume["maximum"]))
    try:
        os.makedirs(os.path.dirname(VOLUME_FILE), exist_ok=True)
        with open(VOLUME_FILE, "w", encoding="utf-8") as f:
            f.write("%d\n" % _volume["default"])
    except OSError as e:
        log.warning("     -> the default volume is not kept across a restart (%s)", e)
    log.info("     -> default volume %d", _volume["default"])


# The products that play along whenever this one is switched on, by jid.
# The owner picks them on the product's page, and the choice outlives a
# restart, like the default volume.
PLAY_ON_FILE = os.environ.get("BEO_PLAY_ON_FILE", "/etc/beo/play-on")
# How long a chosen product is waited for after the switch-on: the network
# may not have named it yet when the service has just started.
PLAY_ON_WAIT_S = 20.0


def _stored_play_on():
    try:
        with open(PLAY_ON_FILE, encoding="utf-8") as f:
            return [line.strip() for line in f if line.strip()]
    except OSError:
        return []


_play_on: list[str] = _stored_play_on()


def play_on():
    """The jids of the products that play along when this one comes on."""
    return list(_play_on)


def set_play_on(jids):
    """Which products play along from now on, kept on disk."""
    wanted = []
    for jid in jids:
        jid = str(jid).strip()
        if jid and jid not in wanted:
            wanted.append(jid)
    _play_on[:] = wanted
    try:
        with open(PLAY_ON_FILE, "w", encoding="utf-8") as f:
            f.write("".join(jid + "\n" for jid in wanted))
    except OSError as e:
        log.warning("     -> the products that play along are not kept across a restart (%s)", e)
    log.info("     -> plays along when switched on: %s", ", ".join(wanted) or "nobody")
    return wanted


def _bring_in_on_switch_on():
    """The product has come on: the products chosen to play along are brought
    in the way the app's expand brings one in, each in a thread of its own.

    Not while this product listens to another: the sound is that one's then,
    and there is nothing of ours to share. A product the network has not
    named yet is asked for again until PLAY_ON_WAIT_S has passed, or the
    product has gone back to standby.
    """
    if not _play_on or _beolink_out is None:
        return
    heard = getattr(_beolink_in, "listening_to", None)
    if heard:
        log.info("     -> listening to %s, so nobody is brought in", heard)
        return

    def bring_in():
        request_ml_audio()
        waiting = list(_play_on)
        deadline = time.monotonic() + PLAY_ON_WAIT_S
        while True:
            waiting = [jid for jid in waiting if _beolink_out.add(jid) is False]
            if not waiting or time.monotonic() >= deadline or _power["value"] != "on":
                break
            time.sleep(2.0)
        for jid in waiting:
            log.warning("     -> %s should play along but cannot be reached", jid)

    threading.Thread(target=bring_in, name="play-on", daemon=True).start()


def machine_switched_on():
    """The machine has just come on, so it stands at the volume it starts at.

    This is the one moment its volume is known without it saying so; from
    here the steps sent to it are counted.
    """
    _volume["level"] = _volume["default"]
    if _ml is not None:
        _ml.volume_is(_volume["level"])
    _volume_changed()


def set_power(value):
    if _power["value"] == value:
        return
    if value == "on":
        machine_switched_on()
    _power["value"] = value
    log.info("     -> power %s", value)
    notifier.send("WebSocketEventPowerState", {"value": value})
    # Power decides whether there is a source to show (current_source), so
    # the app hears about the source right after the power state.
    source = current_source()
    notifier.send("WebSocketEventSourceChange", source)
    notifier.send("WebSocketEventPlaybackSource", source)
    # After the source, so the app has one to draw the group around.
    if value == "on":
        _bring_in_on_switch_on()


def go_standby(from_app=True):
    """Standby: stop every BeoLink listener, put the 9000 in standby, leave
    whatever we were listening to, tell the app."""
    if _beolink_out is not None:
        try:
            _beolink_out.clear()
        except Exception as e:      # noqa: BLE001
            log.warning("     -> could not clear listeners: %s", e)
    _beolink_listeners.clear()
    if from_app:
        ml_send("standby")
    if _beolink_in is not None:
        try:
            _beolink_in.leave()
        except Exception as e:      # noqa: BLE001
            log.warning("     -> could not leave the group: %s", e)
    _active["state"] = "stopped"
    _active["progress"] = 0
    notifier.send("WebSocketEventPlaybackState", {"value": "stopped"})
    set_power("standby")


def websocket_snapshot():
    """Every state the app renders from, for a client that has just connected.

    Same payloads as the change events, in the order a product sends them.
    The listeners are replayed as expand results: the app adds a follower
    per `expand` and would otherwise show the group as empty.
    """
    import uuid as _uuid
    source = current_source()
    events = [
        ("WebSocketEventPowerState", dict(_power)),
        ("WebSocketEventVolume", volume_state()),
        ("WebSocketEventSourceChange", source),
        ("WebSocketEventPlaybackSource", source),
        ("WebSocketEventPlaybackState", {"value": _active["state"]}),
        ("WebSocketEventPlaybackMetadata", now_metadata()),
        ("WebSocketEventPlaybackProgress", {"id": str(_active["track"]),
                                            "progress": _active["progress"],
                                            "totalDuration": TRACK_SECONDS}),
    ]
    for jid in list(_beolink_listeners):
        events.append(("WebSocketEventBeolinkJoinResult",
                       {"jid": jid, "status": "joined", "type": "expand",
                        "error": "ok", "requestID": str(_uuid.uuid4())}))
    return events


notifier.on_connect = websocket_snapshot


def announce_track():
    """Tell the app what is playing now: state, metadata and progress."""
    notifier.send("WebSocketEventPlaybackState", {"value": _active["state"]})
    notifier.send("WebSocketEventPlaybackMetadata", now_metadata())
    notifier.send("WebSocketEventPlaybackProgress",
                  {"id": str(_active["track"]), "progress": _active["progress"],
                   "totalDuration": TRACK_SECONDS})


def apply_ml_source(source, state="started", from_machine=True):
    """The machine is on the plain source in this slot.

    Said by the machine, or by the app's own choice until the machine answers.
    Only the machine's word says it is on. A slot the product has no source
    for is left alone: there is nothing to show the app for it.
    """
    if plain_source(source) is None:
        log.warning("     -> ML: on %s, which this product does not show", source)
        return
    changed = source != _active["source"] or state != _active["state"]
    _active.update(source=source, state=state, progress=0)
    global _pending_cd
    _pending_cd = None
    if changed:
        log.info("     -> %s: %s, %s", "ML" if from_machine else "app", source, state)
        shown = current_source()
        notifier.send("WebSocketEventSourceChange", shown)
        notifier.send("WebSocketEventPlaybackSource", shown)
        announce_track()
    if from_machine and state in ("started", "paused"):
        set_power("on")


def apply_ml_state(cd=None, track=None, state=None):
    """The 9000's real state, from the MasterLink side (masterlink.MusicSystem via
    ml_bridge, or the private POST /api/v1/_ml/state). Never guessed.
    state: "started" | "paused" | "stopped"
    """
    cd = int(cd if cd is not None else _active["cd"])
    track = int(track if track is not None else _active["track"])
    state = state or _active["state"]
    if not 1 <= cd <= CD_COUNT:
        raise ValueError("cd %d udenfor 1..%d" % (cd, CD_COUNT))
    changed = (cd != _active["cd"] or track != _active["track"] or state != _active["state"])
    if track != _active["track"] or cd != _active["cd"]:
        _active["progress"] = 0
    source_changed = cd != _active["cd"] or _active["source"] is not None
    changed = changed or source_changed
    _active.update(cd=cd, track=max(1, track), state=state, source=None)
    global _pending_cd
    if source_changed:
        _pending_cd = None          # the machine has answered; its disc is the disc
    if source_changed:
        notifier.send("WebSocketEventSourceChange",
                      mozart_source(active_content_uri(), "CD %d" % cd, "classicsAdapter"))
    if changed:
        log.info("     -> ML: CD %d, track %d, %s", cd, track, state)
        announce_track()
    if state in ("started", "paused"):
        set_power("on")
    if changed:
        _listeners_follow_state()
    return {"ok": True, "cd": cd, "track": track, "state": state}


def _listeners_follow_state():
    """The products that play our sound hear whether it plays or pauses, so
    a pause stays a pause with them, whoever asked for it."""
    if _beolink_out is None:
        return
    try:
        _beolink_out.set_state("play" if _active["state"] == "started" else "pause")
    except Exception as e:                              # noqa: BLE001
        log.warning("     -> the listeners were not told the state: %s", e)


# The transport keys a listener sends through the tunnel, by their code.
# Beo4 has no pause key: on a CD player STOP is what pauses it.
LISTENER_KEYS = {0x35: "play", 0x36: "pause", 0x1E: "skip", 0x1F: "prev"}
LISTENER_GO = 0x20
# How long the digits of a track number may be apart, after GO. A machine
# takes them at a remote's pace, and a second digit makes a two-digit track.
TRACK_DIGITS_S = 2.0

_track_entry = {"digits": "", "at": 0.0, "timer": None}
_track_entry_lock = threading.Lock()


def listener_key(code):
    """A key pressed on a product that plays our sound, handed to us as the
    machine's own keys are. It goes the way the app's command goes.

    The digits are the machine's own: a digit alone is a disc, and GO
    followed by one or two digits is a track on it.
    """
    if code == LISTENER_GO:
        with _track_entry_lock:
            _track_entry["digits"], _track_entry["at"] = "", time.monotonic()
        log.info("     -> GO, from a listener: the digits that follow are a track")
        return
    if 0 <= code <= 9 and _track_entry_digit(code):
        return
    command = LISTENER_KEYS.get(code)
    if command is None:
        # The digit keys name a disc, as they do on the machine's own remote.
        if 1 <= code <= CD_COUNT:
            log.info("     -> CD %d, from a listener", code)
            choose_disc(code)
            return
        log.info("     -> key 0x%02x from a listener is not a transport key; nothing done", code)
        return
    log.info("     -> %s, from a listener", command)
    playback_command(command)


def _track_entry_digit(digit):
    """A digit while a track number is being entered: True when it was taken
    as one. The number goes to the machine after the second digit, or when
    no digit follows within the pause a remote's own digits allow."""
    with _track_entry_lock:
        if time.monotonic() - _track_entry["at"] > TRACK_DIGITS_S:
            return False
        _track_entry["digits"] += str(digit)
        _track_entry["at"] = time.monotonic()
        timer = _track_entry["timer"]
        if timer is not None:
            timer.cancel()
        if len(_track_entry["digits"]) >= 2:
            _track_entry["timer"] = None
            number = _track_entry["digits"]
        else:
            _track_entry["timer"] = threading.Timer(TRACK_DIGITS_S, _track_entry_done)
            _track_entry["timer"].daemon = True
            _track_entry["timer"].start()
            return True
    _go_to_track(int(number))
    return True


def _track_entry_done():
    with _track_entry_lock:
        number, _track_entry["digits"], _track_entry["timer"] = _track_entry["digits"], "", None
        _track_entry["at"] = 0.0
    if number:
        _go_to_track(int(number))


def _go_to_track(n):
    with _track_entry_lock:
        _track_entry["digits"], _track_entry["at"] = "", 0.0
    if n < 1:
        log.info("     -> track 0 from a listener: nothing done")
        return
    log.info("     -> track %d, from a listener", n)
    if _ml is None:
        _active["track"] = n
        _active["progress"] = 0
        _active["state"] = "started"
        announce_track()
        return
    try:
        _ml.goto_track(n)
    except Exception as e:                              # noqa: BLE001
        log.warning("     -> ML: could not go to track %d (%s)", n, e)


def choose_disc(wanted, uri=None):
    """Make the machine play this disc, as the app's pick of it does; ``uri``
    is the content uri the app named it by, when it did."""
    # With a machine attached the disc is whatever it reports. Claiming it
    # changed here reads as flicker when the status corrects it.
    _active["state"] = "started"
    _active["source"] = None
    if _ml is not None:
        set_pending_cd(wanted)
        log.info("     -> CD %d chosen (the machine is on CD %d)", wanted, _active["cd"])
        threading.Thread(target=ml_select_disc, args=(wanted,), daemon=True).start()
    else:
        # No machine attached: behave as before so the emulator is still
        # useful on its own.
        _active["cd"] = wanted
        _active["track"] = 1
        _active["progress"] = 0
        log.info("     -> now playing CD %d, track 1 of %d", _active["cd"],
                 track_count(_active["cd"]))
    notifier.send("WebSocketEventSourceChange",
                  mozart_source(uri or active_content_uri(), f"CD {_active['cd']}", "classicsAdapter"))
    announce_track()
    _listeners_follow_state()


def playback_command(command):
    """play | pause | stop | skip | prev, from the app or from a listener.
    The HTTP status the app is answered with.

    Those five are all the app knows; anything else is 404, never a made-up
    state (docs/app-protocol.md §5).
    """
    cd = _active["cd"]
    n = track_count(cd)

    if command in ("play", "pause", "stop"):
        _active["state"] = {"play": "started", "pause": "paused",
                            "stop": "stopped"}[command]
        log.info("     -> %s (CD %d, track %d)", command, cd, _active["track"])
        notifier.send("WebSocketEventPlaybackState",
                      {"value": _active["state"]})
        # Beo4 has no Pause: on a CD player STOP is what pauses it, which is
        # why both map to the same key here rather than to an invented one.
        ml_send({"play": "play", "pause": "stop", "stop": "stop"}[command])
        _listeners_follow_state()
        return 200

    if command in ("skip", "prev"):
        if _ml is not None:
            # Do not guess the new track: the 9000 never says how many a
            # disc has. Send the key and let the status telegram answer
            # (docs/app-protocol.md §6).
            ml_send("skip" if command == "skip" else "prev")
            _active["progress"] = 0
            _active["state"] = "started"
            log.info("     -> %s sent to the 9000; the track number comes "
                     "back from the machine", command)
            _listeners_follow_state()
            return 200

        # No machine attached: keep the emulator usable on its own.
        step = 1 if command == "skip" else -1
        _active["track"] = (_active["track"] - 1 + step) % n + 1
        _active["progress"] = 0
        _active["state"] = "started"
        log.info("     -> %s -> CD %d, track %d/%d", command, cd,
                 _active["track"], n)
        announce_track()
        _listeners_follow_state()
        return 200

    log.warning("     -> unknown command %r (the app knows only "
                "play/pause/stop/skip/prev)", command)
    return 404

# Set when the BeoLink audio side is enabled. Without it expand and unexpand
# behave as they always did: the app gets its 200, and no sound goes anywhere.
_beolink_out = None

# Set when the product can itself listen to another. Without it join and leave
# are acknowledged and nothing follows.
_beolink_in = None


def set_beolink_in(listening):
    """Give the facade the listening side: an object with join(jid, on_result),
    leave() and listening_to, the jid it follows or None. on_result(joined) is
    called once the outcome is known."""
    global _beolink_in
    _beolink_in = listening


# Paths the application answers itself, by prefix. The app never asks for
# them, so they cannot shadow a route it relies on.
_served = {}


def serve(prefix, handler):
    """handler(request, path) answers every request at or under ``prefix``
    in full, through the BaseHTTPRequestHandler it is given; request.command
    is the verb, and a write's body is request.body. A response that never
    ends, such as a stream, holds only its own connection."""
    _served[prefix.rstrip("/")] = handler


# "wired" or "wireless", the only two values the app knows. Decided from which
# interface carries our LAN address.
OUR_TRANSPORT = "wired"

# Demo disc contents: (album, artist, [track titles]). A real Beosound 9000
# supplies none of this; --faithful emulates that (docs/app-protocol.md §6).
DISCS = {
    1: ("Kind of Blue", "Miles Davis",
        ["So What", "Freddie Freeloader", "Blue in Green",
         "All Blues", "Flamenco Sketches"]),
    2: ("The Dark Side of the Moon", "Pink Floyd",
        ["Speak to Me", "Breathe", "On the Run", "Time",
         "The Great Gig in the Sky", "Money", "Us and Them",
         "Any Colour You Like", "Brain Damage", "Eclipse"]),
    3: ("Blue Train", "John Coltrane",
        ["Blue Train", "Moment's Notice", "Locomotion",
         "I'm Old Fashioned", "Lazy Bird"]),
    4: ("Rumours", "Fleetwood Mac",
        ["Second Hand News", "Dreams", "Never Going Back Again",
         "Don't Stop", "Go Your Own Way", "Songbird", "The Chain",
         "You Make Loving Fun", "I Don't Want to Know",
         "Oh Daddy", "Gold Dust Woman"]),
    # Roman numerals on purpose: digits in a title are read as the disc number.
    5: ("Goldberg Variations", "Glenn Gould",
        ["Aria"] + [f"Variatio {r}" for r in
                    ["I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X",
                     "XI", "XII", "XIII", "XIV", "XV", "XVI", "XVII", "XVIII",
                     "XIX", "XX", "XXI", "XXII", "XXIII", "XXIV", "XXV", "XXVI",
                     "XXVII", "XXVIII", "XXIX", "XXX"]]),
    6: ("Café Bleu", "The Style Council",
        ["Mick's Blessings", "The Whole Point of No Return",
         "Me Ship Came In!", "Blue Café", "The Paris Match",
         "My Ever Changing Moods", "Dropping Bombs on the Whitehouse",
         "A Gospel", "Strength of Your Nature", "You're the Best Thing",
         "Here's One That Got Away", "Headstart for Happiness",
         "Council Meetin'"]),
}


def disc_info(cd):
    """(album, artist, tracks) for a disc — or bare numbers in faithful mode,
    where a track has no name to show, only its place on the disc."""
    if not DISCS:
        return f"CD {cd}", "", [""] * 12
    return DISCS.get(cd, (f"CD {cd}", "", [""] * 12))


def track_count(cd):
    return len(disc_info(cd)[2])


def cd_metadata(cd, track):
    """MozartPlaybackContentMetadata for a disc/track.

    `id` must be unique per track, not per disc, and the only digits in
    `title` must be the disc number (docs/app-protocol.md §6).
    """
    album, artist, tracks = disc_info(cd)
    track_title = tracks[track - 1] if 1 <= track <= len(tracks) else ""

    # The app reads every digit in the title as the disc number; a digit in the
    # track name crashes the player.
    safe = "".join(c for c in track_title if not c.isdigit()).strip(" -·")
    title = f"CD {cd} · {safe}" if safe else f"CD {cd}"
    uid = cd * 100 + track                      # unique across discs
    return {
        "albumName": album, "artistName": artist, "title": title,
        "track": track, "trackCount": len(tracks),
        "totalDuration": TRACK_SECONDS, "totalDurationSeconds": TRACK_SECONDS,
        # The first entry is the one the app shows.
        "art": ([{"key": "512x512", "size": "large", "type": "image",
                  "hasLocalImage": False, "url": art_url(cd)},
                 {"key": "256x256", "size": "medium", "type": "image",
                  "hasLocalImage": False, "url": art_url(cd)}] if DISCS else []),
        "bitdepth": 16, "bitrate": 1411, "samplerate": 44100,
        "containerName": "", "encoding": "pcm", "genre": "", "id": uid,
        "inputChannels": "2", "outputChannels": "2", "isExplicit": False,
        "organization": "", "queueId": "00000000-0000-0000-0000-000000000000",
        "remoteLeader": {"audioTransport": "", "friendlyName": "", "jid": ""},
        "remoteSource": "", "source": "classicsAdapter",
        "sourceInternalId": f"cd{cd}-{track}",
        "tags": [], "uri": f"{CLASSICS_PREFIX}{cd}",
    }


def now_metadata():
    """What is playing, for the app: the disc and track, or the plain source.

    A plain source carries the same fields with nothing in them: the machine
    says which source it is on and not what plays there.
    """
    meta = cd_metadata(_active["cd"], _active["track"])
    plain = plain_source(_active["source"])
    if plain is None:
        return meta
    meta.update(albumName="", artistName="", title=plain.name, trackCount=0, art=[],
                id=0, source="lineIn", sourceInternalId=plain.slot.lower(), uri="")
    return meta


def progress_ticker():
    """Advance playback one second at a time and announce it.

    The app shows --:-- until progress is non-zero, whatever the duration
    says (docs/app-protocol.md §6).
    """
    beat = 0
    while True:
        time.sleep(1)
        # Listeners have to see a living source. Without a steady progress
        # notification nothing confirms to the speaker that it is still playing.
        beat += 1
        if _beolink_out is not None and beat % 2 == 0:
            _beolink_out.progress()
        if _active.get("state") != "started" or _active["source"] is not None:
            continue
        _active["progress"] += 1
        if _active["progress"] >= TRACK_SECONDS:
            # End of track: roll onto the next one, wrapping within the disc.
            n = track_count(_active["cd"])
            _active["track"] = _active["track"] % n + 1
            _active["progress"] = 0
            log.info("     -> track ended, continuing with track %d/%d",
                     _active["track"], n)
            notifier.send("WebSocketEventPlaybackMetadata",
                          cd_metadata(_active["cd"], _active["track"]))
        notifier.send("WebSocketEventPlaybackProgress",
                      {"id": str(_active["track"]),
                       "progress": _active["progress"],
                       "totalDuration": TRACK_SECONDS})


def art_url(cd):
    """Cover art URL for a disc from the Cover Art Archive, or None.

    The app only loads art from a publicly trusted host (docs/app-protocol.md §7).
    """
    return ART_URLS.get(cd)


def active_content_uri():
    return f"{CLASSICS_PREFIX}{_active['cd']}"


# The disc the app asked for while the 9000 is still changing to it. A change
# takes up to a few seconds by a bare digit and ~7 s by the standby route, and
# the app reads content/active 2 s after asking, once; answering with the old
# disc there leaves two discs marked as playing.
_pending_cd = None          # (disc, time.monotonic() deadline)
PENDING_DISC_S = 15.0


def set_pending_cd(disc):
    global _pending_cd
    _pending_cd = (int(disc), time.monotonic() + PENDING_DISC_S)


def pending_cd():
    """The pending disc, or None once the machine has answered or time is up."""
    global _pending_cd
    if _pending_cd is not None and time.monotonic() < _pending_cd[1]:
        return _pending_cd[0]
    _pending_cd = None
    return None


def active_or_pending_content_uri():
    cd = pending_cd()
    return f"{CLASSICS_PREFIX}{cd if cd is not None else _active['cd']}"

# Remote-menu item ids are GUIDs, lowercase and hyphenated, never numbers
# (docs/app-protocol.md §5).
REMOTE_CA_ID = getattr(PROFILE, "REMOTE_SOURCE_ID", "")


# ---- Mozart API payloads ----------------------------------------------------
def product_info():
    """GET api/v1/product/info  -> MozartProductInformation

    Every field is dereferenced unconditionally by the app, so a thinner
    response throws inside it (docs/app-protocol.md §5).
    """
    return {
        "apple": {"homeApp": False},
        "bootloader": {"version": FIRMWARE},
        "componentVersions": {"items": [{"component": "SW", "version": FIRMWARE}]},
        "encryptionInfo": {"algorithm": "", "keyLength": 0, "publicKey": ""},
        "fep": {"version": FIRMWARE},
        "name": {"friendlyName": FRIENDLY},
        "pcbRevisions": {"items": []},
        "powerDelivery": {"version": "1.0"},
        "production": {
            "hardwareRevision": "1",
            "itemNumber": ITEM_NUMBER,
            "macAddresses": {"macAddresses": [
                {"name": "ethernet", "mac": MAC},
                {"name": "wlansta", "mac": MAC},
            ]},
            "productDefaultName": PROFILE.product_default_name,
            "productionSite": "emulator",
            "productionTimestamp": "2024-01-01T00:00:00Z",
            "serialNumber": SERIAL,
            "typeNumber": TYPE_NUMBER,
            "wifiCountryCode": "DK",
        },
        "touch": {"version": FIRMWARE},
        # Present but empty: a speaker, not a TV, and nothing throws walking in.
        "tv": {"edid": {"name": "", "year": 0},
               "lge": {"country": "", "countryGroup": "", "deviceId": "",
                       "firmwareVersion": "", "language": "", "screenSize": 0,
                       "webOsDialect": ""}},
    }


def content_items(start_with=""):
    """GET api/v1/content?startWith=... -> dict<string, MozartContentItem>

    What there is to browse is the product's business, not the protocol's.
    """
    return PROFILE.content_items(start_with)


# Labels the app has renamed via PUT api/v1/remote/{id}; a real product
# persists them (docs/app-protocol.md §3).
_remote_labels: dict[str, str] = {}


def remote_menu():
    """GET api/v1/remote  -> dict<string, MozartRemoteMenuItem>

    Where the player's source list comes from, not playback/sources. The root
    must be labelled MUSIC and the tree must be walkable from a root
    (docs/app-protocol.md §5).
    """
    return PROFILE.remote_items(_remote_labels)


def playback_sources():
    """GET api/v1/playback/sources -> MozartSourceArray {items: [MozartSource]}

    The field names are the DTO's: id and name, not sourceId and friendlyName
    (docs/app-protocol.md §5).
    """
    jid = f"{TYPE_NUMBER}.{ITEM_NUMBER}.{SERIAL}@products.bang-olufsen.com"
    return {"items": [mozart_source(sid, name, kind)
                      for sid, name, kind in PROFILE.source_entries(jid)]}


# Sentinel: answer 404 rather than an empty 200. A 404 is a feature the product
# does not have; an empty 200 is a crash in the app (docs/app-protocol.md §4).
NOT_FOUND = object()

# Sentinel: answer 204. The app's client accepts one success code per endpoint,
# and for audioleader that is 200 or 204, never 404 (docs/app-protocol.md §4).
NO_CONTENT = object()


def beozone_source(cd=None):
    """Our one source, in the form /BeoZone/Zone/Sources uses.

    One entry per source type, not per disc; the shape is a product's own
    (docs/app-protocol.md §10).
    """
    jid = f"{TYPE_NUMBER}.{ITEM_NUMBER}.{SERIAL}@products.bang-olufsen.com"
    sid = f"CD:{jid}"
    return [sid, {
        "id": sid,
        "friendlyName": "Beosound 9000",
        "sourceType": {"type": "CD"},
        "category": "MUSIC",
        "inUse": True,
        "profile": "",
        "borrowed": False,
        "linkable": True,
        "recommendedIrMapping": [
            {"format": 0, "unit": 0, "command": 146},
            {"format": 11, "unit": 0, "command": 146}],
        "contentProtection": {"schemeList": ["NONE"]},
        "embeddedBinary": {"schemeList": []},
        "product": {"jid": jid, "friendlyName": FRIENDLY},
        "jid": jid,
        "_capabilities": {"editable": ["friendlyName"]},
        "_links": {"self": {"href": "./" + urllib.parse.quote(sid, safe="")}},
    }]


def beozone_active_source():
    jid = f"{TYPE_NUMBER}.{ITEM_NUMBER}.{SERIAL}@products.bang-olufsen.com"
    return {"primary": f"CD{_active['cd']}:{jid}", "primaryJid": jid}


def _notify_listeners_changed():
    """Make the app fetch /api/v1/beolink/listeners again.

    The join event can only add listeners; this notification is the only way
    to remove one, and without it unexpand hangs (docs/app-protocol.md §8).
    """
    notifier.send("WebSocketEventNotification",
                  {"value": "beolinkListeners", "code": ""})


def _expand_joined(jid):
    """A listener is playing our source: remember it and tell the app.

    The join result's fields are enums, and `type` must be "expand": the
    default, join, makes the app clear its listener list instead
    (docs/app-protocol.md §8).
    """
    if jid not in _beolink_listeners:
        _beolink_listeners.append(jid)
    notifier.send("WebSocketEventBeolinkJoinResult",
                  {"jid": jid, "status": "joined", "type": "expand",
                   "error": "ok", "requestID": str(uuid.uuid4())})
    # To expand is to play: a group whose leader is paused is not drawn.
    if _active["state"] != "started":
        _active["state"] = "started"
        notifier.send("WebSocketEventPlaybackState",
                      {"value": _active["state"]})
        notifier.send("WebSocketEventPlaybackSource", current_source())


def listener_joined(jid):
    """A product began playing our sound by itself, as one does for a source
    it has borrowed or joined: the app draws it as it draws an expand."""
    _expand_joined(jid)


def listener_left(jid):
    """A product stopped playing our sound by itself: it leaves the list the
    app draws, and the app is made to fetch the list again."""
    if jid in _beolink_listeners:
        _beolink_listeners.remove(jid)
        _notify_listeners_changed()


def _expand_result(jid, joined):
    """What became of an expand the BeoLink side accepted, told from its thread.

    A failure is answered with silence: the app's own 25 s timer then reports
    it, and a `failed` event would disturb the join flow's bookkeeping instead
    (docs/app-protocol.md §8).
    """
    if joined:
        _expand_joined(jid)
    else:
        log.warning("     -> expanding to %s failed; the app reports the error "
                    "itself when its wait times out", jid)


def _join_result(jid, request_id, joined):
    """What became of a join the app asked for, told from the listening side.

    `type` is "join" and `jid` is the product we now follow: that is what
    makes the app draw us as its follower. A failure is answered with
    silence, as for expand: the app's own timer reports it.
    """
    if not joined:
        log.warning("     -> joining %s failed; the app reports the error "
                    "itself when its wait times out", jid)
        return
    notifier.send("WebSocketEventBeolinkJoinResult",
                  {"jid": jid, "status": "joined", "type": "join",
                   "error": "ok", "requestID": request_id})


def playback_state():
    """GET api/v1/playback/state -> MozartPlaybackState

    Nothing is playing, but every sub-object the app may walk into is present.
    """
    return {
        "state": {"value": _active.get("state", "idle")},
        "source": current_source(),
        "progress": {"id": str(_active["track"]), "progress": _active["progress"],
                     "totalDuration": TRACK_SECONDS},
        "metadata": now_metadata(),
    }


def mozart_source(sid, name, type_value):
    """MozartSource — `type` is an object with a `value`, not a bare string."""
    return {"id": sid, "name": name, "type": {"value": type_value},
            "isEnabled": True, "isPlayable": True,
            "isMultiroomAvailable": True, "isSeekable": False}


def source_is_active():
    """Is there a source to show, or is the machine off?

    Standby is not "CD". A selection the
    app has just made counts as active until the machine answers, so the
    player does not flicker to "no source" meanwhile.
    """
    return (_power["value"] == "on" or _active["state"] in ("started", "paused")
            or pending_cd() is not None)


def current_source():
    """The active source as the app should see it right now.

    With nothing active the type is `unknown`, which the player renders as
    "No source". It is still a complete MozartSource, because the app
    dereferences id and name.
    """
    if not source_is_active():
        return mozart_source("", "", "unknown")
    plain = plain_source(_active["source"])
    if plain is not None:
        # Line-in is the app's one plain source: a name and nothing to browse.
        return mozart_source("lineIn", plain.name, "lineIn")
    return mozart_source(active_content_uri(), f"CD {_active['cd']}", "classicsAdapter")


def source_choices():
    """The machine's sources as the product's page offers them: the discs as
    one, under the name the app knows it by, then the plain ones; and which
    of them plays now. The ids are the ones the app selects by, so the page
    picks a source the way the app does."""
    active = source_is_active()
    plain = plain_source(_active["source"]) if active else None
    items = []
    if REMOTE_CA_ID:
        items.append({"id": REMOTE_CA_ID, "name": _remote_labels.get(REMOTE_CA_ID, "CD"),
                      "active": active and plain is None})
    for s in PROFILE.plain_sources:
        items.append({"id": s.item_id, "name": _remote_labels.get(s.item_id, s.name),
                      "active": plain is not None and plain.slot == s.slot})
    return items


# Static GET responses. Field types follow the app's DTOs exactly; a wrong type
# is as fatal as a missing field (docs/app-protocol.md §4).
GET_ROUTES = {
    "/api/v1/product/info": product_info,
    "/api/v1/remote": remote_menu,
    "/api/v1/playback/sources": playback_sources,
    "/api/v1/state": lambda: {"power": {"value": _power["value"]}, "volume": volume_state()},
    "/api/v1/state/power": lambda: {"value": _power["value"]},
    "/api/v1/sound/volume": lambda: volume_state(),
    "/api/v1/sound/volume/level": lambda: {"level": _volume["level"]},
    "/api/v1/sound/volume/mute": lambda: {"muted": _volume["muted"]},
    "/api/v1/sound/volume/settings": lambda: {"default": {"level": _volume["default"]},
                                             "maximum": {"level": _volume["maximum"]}},
    "/api/v1/content-triggers": lambda: {"items": []},
    "/api/v1/playback/queue/settings": lambda: {
        "consume": False, "gapless": True, "repeat": "none", "shuffle": False},
    # A leader as soon as anyone listens, 204 otherwise. An empty leader is an
    # empty group in the app (docs/app-protocol.md §9).
    "/api/v1/beolink/audioleader": lambda: (
        {"jid": f"{TYPE_NUMBER}.{ITEM_NUMBER}.{SERIAL}@products.bang-olufsen.com",
         "friendlyName": FRIENDLY, "audioTransport": OUR_TRANSPORT}
        if _beolink_listeners else NO_CONTENT),

    "/api/v1/content/active": lambda: content_items()[active_or_pending_content_uri()],
    "/api/v1/beolink/enabled": lambda: {"value": True},
    "/api/v1/beolink/self": lambda: {"jid": f"{TYPE_NUMBER}.{ITEM_NUMBER}.{SERIAL}"
                                            "@products.bang-olufsen.com",
                                     "friendlyName": FRIENDLY},
    # Objects with jid and audioTransport, not bare jid strings
    # (docs/app-protocol.md §9).
    "/api/v1/beolink/listeners": lambda: [
        {"jid": j,
         "audioTransport": peer_by_jid(j).get("audioTransport", "wireless")}
        for j in _beolink_listeners],
    # Every peer, including those already listening, and only jid. Filtering or
    # adding fields broke it.
    "/api/v1/beolink/available-listeners": lambda: [
        {"jid": p["jid"]} for p in peers()],
    "/api/v1/beolink/peers": lambda: [dict(p) for p in peers()],
    "/api/v1/network": lambda: {"interfaces": [{"type": "wlan", "macAddress": MAC}]},
    "/api/v1/softwareupdate": lambda: {"state": "idle", "version": FIRMWARE},
    "/api/v1/setup/linein": lambda: {"enabled": True},
    "/api/v1/hdmi": lambda: {"items": []},
    "/api/v1/scenes": lambda: {},
    "/api/v1/alarms": lambda: {},
    "/api/v1/timers": lambda: {},
    "/api/v1/sound/features": lambda: {},

    # --- the endpoints the app asked for and previously got an empty {} ------
    "/api/v1/cast/tos-accepted": lambda: {"accepted": True},
    "/api/v1/cast/version": lambda: {"version": "1.56.500000"},
    "/api/v1/playback/state": playback_state,
    "/api/v1/playback/queue": lambda: {"items": []},
    "/api/v1/playback/sources/active": current_source,
    "/api/v1/proximity/settings": lambda: {"disabled": True},
    "/api/v1/setup/enableusagereport": lambda: {"enabled": False},
    "/api/v1/setup/presets": lambda: {},          # dict<string, MozartPreset>
    "/api/v1/softwareupdate/settings": lambda: {"automaticUpdate": False},
    "/api/v1/sound/settings/adjustments": lambda: {
        "advancedBeamforming": 0, "ambience": 0.0, "bass": 0, "directivity": "omni",
        "ecoMode": False, "fadein": False, "loudness": False, "treble": 0},
    "/api/v1/sound/settings/toneTouch": lambda: {"x": 0.0, "y": 0.0, "z": 0.0},

    # SpeakerLink: answer truthfully that we are a lone speaker. Saying
    # "standalone" is safer than 404 here — it stops the app offering to pair us.
    "/api/v1/speakerlink/role": lambda: {"role": "none", "desired": "none",
                                         "channel": "all", "primary": ""},
    "/api/v1/speakerlink/association": lambda: {
        "name": FRIENDLY, "type": "standalone", "supported": ["standalone"],
        "secondaries": [],
        # Structurally present, semantically empty — `type: standalone` is what
        # says we are not paired; these exist only so nothing NPEs reading them.
        "primary": {"name": "", "channel": "left"},
        "secondary": {"name": "", "channel": "left", "ipAddress": "", "serialNumber": ""}},

    # The BeoZone surfaces a product serves and other products read
    # (docs/app-protocol.md §10). Sources is where a peer fetches our catalogue.
    "/BeoZone/Zone/Sources": lambda: {
        "sources": [beozone_source()]},
    # The service index a peer starts at; a product's own list.
    "/": lambda: {"services": [
        {"name": "BeoNotify", "path": "BeoNotify/"},
        {"name": "BeoZone", "path": "BeoZone/"},
        {"name": "BeoDevice", "path": "BeoDevice/"},
        {"name": "BeoContent", "path": "BeoContent/"},
        {"name": "BeoHome", "path": "BeoHome/"},
        {"name": "BeoSecurity", "path": "BeoSecurity/"},
        {"name": "Ping", "path": "Ping"}]},
    "/BeoNotify": lambda: {"beoNotify": {"notifications": {
        "_links": {"self": {"href": "./Notifications"}}}}},
    "/BeoSecurity": lambda: {"Sessions": {
        "_links": {"self": {"href": "./Sessions"}}}},
    "/BeoHome": lambda: {"beoHome": {"profiles": [
        {"name": "triggerProfile", "version": 1,
         "_links": {"self": {"href": "./trigger/"}}}]}},
    "/BeoZone/System": lambda: {"features": ["PRODUCTS"]},
    # A product answers 200 with an empty body.
    "/Ping": lambda: {},

    # The shapes of a product's replies, with only the capabilities
    # a BeoSound 9000 has; peers read the features lists before trying anything.
    "/BeoZone/Zone": lambda: {"features": []},
    "/BeoZone/Zone/Sound": lambda: {"features": ["VOLUME", "VOLUME_LEVEL"]},
    "/BeoZone/Zone/Stream": lambda: {"features": ["PAUSE", "PLAY", "SKIP",
                                                  "STOP"]},
    "/BeoZone/Zone/List": lambda: {"features": ["STEP"]},
    "/BeoZone/Zone/Sound/Volume/Speaker/Level": lambda: {
        "level": 40,
        "_capabilities": {"editable": ["level"]},
        "_links": {"/relation/modify": {"href": "./"}}},
    "/BeoZone/Zone/Sound/Mode": lambda: {"mode": {"list": [
        {"id": 1, "friendlyName": "default", "digit": 1,
         "_links": {"/relation/modify": {"href": "./1"},
                    "self": {"href": "./1"}}}], "active": 1}},
    "/BeoZone/Zone/Sound/SpeakerGroup": lambda: {"speakerGroup": {"list": [
        {"id": 1, "friendlyName": "SpeakerGroup HH1", "digit": 1,
         "_capabilities": {"editable": ["friendlyName"]},
         "_links": {"/relation/modify": {"href": "./1"},
                    "self": {"href": "./1"}}}], "active": 1}},
    "/BeoContent": lambda: {"sources": []},
    "/BeoSecurity/Sessions": lambda: {"sessionList": {"session": []}},
    "/BeoDevice/powerManagement": lambda: {"profile": {
        "name": "powerManagementProfile", "version": 1,
        "_links": {"self": {"href": "./"}},
        "powerManagement": {
            "standby": {
                "powerState": ("standby" if _active["state"] != "started"
                               else "on"),
                "_capabilities": {"editable": ["powerState"],
                                  "value": {"powerState": ["on", "standby"]}},
                "_links": {"/relation/modify": {"href": "./standby"},
                           "self": {"href": "./standby"}}}}}},

    # A disc player has no queue, but the surface must exist and answer empty.
    "/BeoZone/Zone/PlayQueue": lambda: {
        "playQueue": {"playQueueItem": [],
                      "_capabilities": {"editable": []}}},

    "/BeoZone/System/Products": lambda: {"products": [{
        "jid": f"{TYPE_NUMBER}.{ITEM_NUMBER}.{SERIAL}"
               "@products.bang-olufsen.com",
        "friendlyName": FRIENDLY,
        "online": True,
        "source": [beozone_source()[1]],
        "integrate": False,
        "borrowSource": False,
        "irReroute": "none",
    }]},

    # Hardware a speaker lacks answers 404, as a real product does.
    "/api/v1/sound/listening-modes/active": lambda: NOT_FOUND,
    "/api/v1/stand/info": lambda: NOT_FOUND,
    "/api/v1/stand/endstops": lambda: NOT_FOUND,
    "/api/v1/stand/status": lambda: NOT_FOUND,
}

# A product may answer routes no other product has. The Classics Adapter check
# lives here rather than in the facade, because a speaker has no adapter.
GET_ROUTES.update(PROFILE.extra_routes(MAC))


def _apply_profile(profile):
    """Point every derived name at a new product.

    The facade keeps these as module-level names because every route reads them
    directly; this is the one place that sets them, so they cannot drift apart.
    """
    global PROFILE, TYPE_NUMBER, ITEM_NUMBER, PRODUCT_TYPE, FRIENDLY, FIRMWARE
    global HOSTNAME, CD_COUNT, CLASSICS_PREFIX, REMOTE_CA_ID
    PROFILE = profile
    TYPE_NUMBER = profile.type_number
    ITEM_NUMBER = profile.item_number
    PRODUCT_TYPE = profile.product_type
    FRIENDLY = profile.friendly
    FIRMWARE = profile.firmware
    HOSTNAME = profile.host_prefix + "-" + SERIAL + ".local."
    _volume["maximum"] = profile.volume_maximum
    for name in ("level", "default"):
        _volume[name] = min(_volume[name], _volume["maximum"])
    CD_COUNT = getattr(profile, "DISC_COUNT", 0)
    CLASSICS_PREFIX = getattr(profile, "PREFIX", "")
    REMOTE_CA_ID = getattr(profile, "REMOTE_SOURCE_ID", "")
    for path in [k for k in GET_ROUTES if k == "/api/v1/classics-adapter"]:
        del GET_ROUTES[path]
    GET_ROUTES.update(profile.extra_routes(MAC))


# ---- Legacy BeoZone/BeoDevice (kept so older discovery paths still answer) ---
def beodevice():
    return {"beoDevice": {
        # Must agree with productType in the _beoremote announcement.
        "productId": {"productType": PRODUCT_TYPE, "typeNumber": TYPE_NUMBER,
                      "serialNumber": SERIAL, "itemNumber": ITEM_NUMBER},
        "productFamily": "mozart",
        "productFriendlyName": {"productFriendlyName": FRIENDLY},
        "software": {"version": FIRMWARE},
        "hardware": {"version": "S53", "type": TYPE_NUMBER, "item": ITEM_NUMBER,
                     "serial": SERIAL, "mac": MAC},
        "profiles": [{"name": "lineInSettingsProfile", "version": 1}],
    }}


LEGACY_ROUTES = {
    "/BeoDevice": beodevice,
    "/BeoZone/Zone/Sound/Volume": lambda: {"volume": {"speaker": {
        "level": 30, "muted": False, "range": {"minimum": 0, "maximum": 90}}}},
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # http.server sends headers and body separately; with Nagle on, every
    # response waited ~15 ms for the client's ACK.
    disable_nagle_algorithm = True

    def _send(self, obj, code=200):
        # HTTP forbids a body on a 204, and a client that reads for a
        # Content-Length of 2 on one hangs on the next request.
        if code == 204:
            self.send_response(204)
            self._flush_with_body(b"")
            return
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._flush_with_body(body)

    def _flush_with_body(self, body: bytes):
        """end_headers() + body as a single send."""
        self._headers_buffer.append(CRLF)
        self.wfile.write(b"".join(self._headers_buffer) + body)
        self._headers_buffer = []

    def _read_body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        return self.rfile.read(n) if n else b""

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        qs = urllib.parse.parse_qs(parsed.query)
        # Who is asking matters as much as what they ask for: it decides
        # whether a 404 hits the app, another product, or nobody.
        log.info("GET  %-46s <- %s", self.path, self.client_address[0])

        for prefix, handler in _served.items():
            if path == prefix or path.startswith(prefix + "/"):
                return handler(self, path)

        # The long notification stream: line-delimited JSON, connection held
        # open, every line beginning with '{' (docs/app-protocol.md §10).
        if path == "/BeoNotify/Notifications":
            log.info("     -> notification stream opened")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Connection", "keep-alive")
            self.send_header("Cache-Control", "no-cache")
            self._headers_buffer.append(CRLF)
            self.wfile.write(b"".join(self._headers_buffer))
            self._headers_buffer = []
            try:
                first = {"notification": {
                    "type": "SOURCE", "kind": "source",
                    "data": beozone_active_source()}}
                self.wfile.write((json.dumps(first) + "\r\n").encode())
                self.wfile.flush()
                while True:
                    time.sleep(5)
                    ping = {"notification": {"type": "PROGRESS_INFORMATION",
                                             "kind": "playing",
                                             "data": {"state": _active["state"],
                                                      "position":
                                                          _active["progress"]}}}
                    self.wfile.write((json.dumps(ping) + "\r\n").encode())
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                log.info("     -> notification stream closed")
            return

        if path == "/api/v1/content":
            start = qs.get("startWith", [""])[0]
            items = content_items(start)
            log.info("     -> %d content items for startWith=%r", len(items), start)
            return self._send(items)

        for table in (GET_ROUTES, LEGACY_ROUTES):
            if path in table:
                payload = table[path]()
                if payload is NOT_FOUND:
                    log.info("     -> 404 (feature not present on this product)")
                    return self._send({}, 404)
                if payload is NO_CONTENT:
                    log.info("     -> 204 (no group yet)")
                    return self._send({}, 204)
                return self._send(payload)

        # The app reads the group out of this BeoZone surface: who can be
        # added, and who listens now. The source id is the BeoZone shape,
        # CD:jid, not a Mozart content path (docs/app-protocol.md §10).
        if path == "/BeoZone/Zone/ActiveSources":
            jid = f"{TYPE_NUMBER}.{ITEM_NUMBER}.{SERIAL}@products.bang-olufsen.com"
            cds = [f"CD:{jid}"]
            free = [p["jid"] for p in peers()]
            return self._send({"primaryExperience": {
                "source": {
                    "id": f"CD:{jid}",
                    "friendlyName": "Beosound 9000",
                    "sourceType": {"type": "CD"},
                    "category": "MUSIC",
                    "inUse": True,
                    "linkable": True,
                    "product": {"jid": jid, "friendlyName": FRIENDLY},
                    "_capabilities": {"editable": ["id"], "value": {"id": cds}},
                },
                "listenerList": {
                    "listener": [{"jid": j} for j in _beolink_listeners],
                    "_capabilities": {"editable": ["listener.jid"],
                                      "value": {"listener.jid": free}},
                    "_links": {"/relation/create":
                               {"href": "./primaryExperience/"}},
                },
            }})

        # 404, not {}: see NOT_FOUND.
        log.warning("     UNHANDLED GET %s  -> 404 (add a route if the app needs it)", path)
        self._send({}, 404)

    def _write_like(self, verb):
        body = self._read_body()
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/")
        log.info("%s %s  body=%s", verb, self.path, body[:400].decode("utf-8", "replace"))

        for prefix, handler in _served.items():
            if path == prefix or path.startswith(prefix + "/"):
                self.body = body
                return handler(self, path)

        if path.startswith("/api/v1/sound/volume"):
            try:
                data = json.loads(body or b"{}")
            except ValueError:
                return self._send({"error": "bad json"}, 400)
            # The app takes 201 as done on these three, and anything else as
            # a failure: a 200 moves the volume and still shows an error.
            if path.endswith("/level"):
                return self._send(set_volume(level=data.get("level")), 201)
            if path.endswith("/mute"):
                return self._send(set_volume(muted=data.get("muted")), 201)
            if path.endswith("/settings"):
                mx = data.get("maximum", {}).get("level") if isinstance(data.get("maximum"), dict) else None
                if mx:
                    # The owner may lower the top, not raise it past the scale.
                    _volume["maximum"] = min(int(mx), PROFILE.volume_maximum)
                df = data.get("default", {}).get("level") if isinstance(data.get("default"), dict) else None
                if df is not None:
                    set_default_volume(df)
                return self._send(volume_state(), 201)
            return self._send(set_volume(level=(data.get("level") or {}).get("level")
                                         if isinstance(data.get("level"), dict) else data.get("level"),
                                         muted=(data.get("muted") or {}).get("muted")
                                         if isinstance(data.get("muted"), dict) else data.get("muted")), 200)

        # PUT state/standby is the app's power button; state/power {"value":
        # "on"} wakes the product (docs/app-protocol.md §5).
        if path == "/api/v1/state/standby":
            log.info("     -> the app switches off (standby)")
            go_standby(from_app=True)
            return self._send({}, 200)
        if path == "/api/v1/state/power":
            try:
                want = json.loads(body or b"{}").get("value", "on")
            except ValueError:
                return self._send({"error": "bad json"}, 400)
            if want == "on":
                ml_wake()
                _active["source"] = None
                set_power("on")
            else:
                go_standby(from_app=True)
            return self._send({}, 200)

        if path.startswith("/api/v1/content/") and path.endswith("/trigger"):
            uri = urllib.parse.unquote(
                path[len("/api/v1/content/"):-len("/trigger")])
            return self._trigger_content(uri)

        if path.startswith("/api/v1/content-trigger/"):
            trigger_id = urllib.parse.unquote(path[len("/api/v1/content-trigger/"):])
            if verb.strip() == "POST":
                # POST activates the trigger; PUT only renames it.
                return self._activate(trigger_id)
            log.info("     -> content trigger modified: %s", trigger_id)
            return self._send({}, 200)

        if path.startswith("/api/v1/playback/sources/active/"):
            return self._activate(urllib.parse.unquote(
                path[len("/api/v1/playback/sources/active/"):]))

        if path.startswith("/api/v1/playback/command/"):
            return self._command(path[len("/api/v1/playback/command/"):])

        # Private control endpoints for the Master Link side, outside the Mozart
        # API; the app never calls them (docs/app-protocol.md §5).
        if path.startswith("/api/v1/_ml/key/"):
            key = path[len("/api/v1/_ml/key/"):].strip().lower()
            if _ml is None:
                return self._send({"error": "no machine attached"}, 409)
            if key == "linkroom":
                log.info("     -> _ml/key: link-room request 0x45 (measurement)")
                _ml.player.request_distribution()
                return self._send({"sent": "linkroom"}, 200)
            if key == "oracle":
                try:
                    src = _ml.player.distributed_source(timeout=1.5)
                except Exception as e:      # noqa: BLE001
                    return self._send({"error": str(e)}, 500)
                log.info("     -> _ml/key: oracle 0x08 -> %r", src)
                return self._send({"distributed": src}, 200)
            # A key with its telegram spelled out, for finding the form the
            # machine answers to: 0x60,src=0x7a,dest=0x0f,rel
            if "," in key and hasattr(_ml, "raw_key"):
                try:
                    sent = _ml.raw_key(key)
                except ValueError as e:
                    return self._send({"error": str(e)}, 400)
                log.info("     -> _ml/key: %s (measurement, not the app)", sent)
                return self._send({"sent": sent}, 200)
            try:
                k = int(key, 0) if key[:1].isdigit() else key
            except ValueError:
                return self._send({"error": "key must be a name or a number"}, 400)
            log.info("     -> _ml/key: %r (measurement, not the app)", k)
            ml_send(k)
            return self._send({"sent": key}, 200)

        if path.startswith("/api/v1/_ml/telegram/") and hasattr(_ml, "raw_telegram"):
            try:
                sent = _ml.raw_telegram(path[len("/api/v1/_ml/telegram/"):].strip())
            except ValueError as e:
                return self._send({"error": str(e)}, 400)
            log.info("     -> _ml/telegram: %s (measurement, not the app)", sent)
            return self._send({"sent": sent}, 200)

        if path.startswith("/api/v1/_ml/volume-form/") and hasattr(_ml, "set_volume_form"):
            try:
                form = _ml.set_volume_form(path[len("/api/v1/_ml/volume-form/"):])
            except ValueError as e:
                return self._send({"error": str(e)}, 400)
            log.info("     -> _ml/volume-form: %s", form)
            return self._send({"form": form}, 200)

        if path == "/api/v1/_ml/state":
            try:
                data = json.loads(body or b"{}")
            except ValueError:
                return self._send({"error": "bad json"}, 400)
            try:
                res = apply_ml_state(data.get("cd"), data.get("track"), data.get("state"))
            except ValueError as e:
                return self._send({"error": str(e)}, 400)
            return self._send(res, 200)

        # BeoLink multiroom, the app-facing flow. The audio is the BeoLink plane,
        # plugged in as _beolink_out. Everything the app sends here is logged,
        # because nothing documents it (docs/app-protocol.md §9).
        if path.startswith("/api/v1/beolink/expand/"):
            jid = urllib.parse.unquote(path[len("/api/v1/beolink/expand/"):])
            log.warning("     -> APPEN BEDER OS UDVIDE TIL %s  (body=%r)",
                        jid, (body or b"")[:200])
            # The machine must put audio on the bus first, or we stream
            # silence.
            request_ml_audio()
            # The app is told "joined" only when it is true: at once without a
            # BeoLink side, 404 if the jid cannot be reached, otherwise when
            # the stream runs (docs/app-protocol.md §9).
            if _beolink_out is None:
                _expand_joined(jid)
            elif _beolink_out.add(jid) is False:
                log.warning("     -> %s cannot be reached; replying 404", jid)
                return self._send({"error": "Remote device not found"}, 404)
            elif not hasattr(_beolink_out, "on_expand_result"):
                _expand_joined(jid)
            return self._send({}, 202)

        if path.startswith("/api/v1/beolink/unexpand/"):
            jid = urllib.parse.unquote(path[len("/api/v1/beolink/unexpand/"):])
            log.warning("     -> the app removes listener %s", jid)
            if jid in _beolink_listeners:
                _beolink_listeners.remove(jid)
            if _beolink_out is not None:
                _beolink_out.remove(jid)
            _notify_listeners_changed()
            return self._send({}, 202)

        if path.startswith("/api/v1/beolink/join/"):
            jid = urllib.parse.unquote(path[len("/api/v1/beolink/join/"):])
            log.warning("     -> the app asks US to join %s  (body=%r)",
                        jid, (body or b"")[:200])
            # The app keeps the request id and waits for a join result that
            # carries it; without one its own timer reports a failure, even
            # while the audio is arriving.
            request_id = str(uuid.uuid4())
            if _beolink_in is not None:
                _beolink_in.join(jid, lambda joined: _join_result(jid, request_id, joined))
            return self._send({"jid": jid, "requestID": request_id,
                               "result": "/api/v1/beolink/join/result/" + request_id}, 202)

        if path == "/api/v1/beolink/leave":
            log.warning("     -> the app asks us to leave the group")
            if _beolink_in is not None:
                _beolink_in.leave()
            _beolink_listeners.clear()
            if _beolink_out is not None:
                _beolink_out.clear()
            _notify_listeners_changed()
            return self._send({}, 202)
        if path.startswith("/api/v1/remote/"):
            item_id = path[len("/api/v1/remote/"):]
            try:
                label = json.loads(body or b"{}").get("label")
            except ValueError:
                label = None
            if label:
                _remote_labels[item_id] = label
                log.info("     -> remote menu item %s renamed to %r (persisted)",
                         item_id, label)
            else:
                log.info("     -> remote menu item %s updated", item_id)
            return self._send({}, 200)

        log.warning("     UNHANDLED %s %s  -> returning {}", verb, path)
        self._send({})

    def _command(self, command):
        """POST api/v1/playback/command/{play|pause|stop|skip|prev}"""
        return self._send({}, playback_command(command))

    def _announce_track(self):
        announce_track()

    def _trigger_content(self, uri):
        """POST api/v1/content/{uri}/trigger: how the app selects a disc.

        Not playback/sources/active/{id}; see docs/app-protocol.md §5.
        """
        disc = uri.rsplit("/", 1)[-1]
        if not disc.isdigit() or not 1 <= int(disc) <= CD_COUNT:
            log.warning("     -> unknown content uri %r", uri)
            return self._send({}, 404)
        choose_disc(int(disc), uri)
        return self._send({}, 200)

    def _activate(self, source_id):
        """POST api/v1/playback/sources/active/{id}: two different ids arrive.

        A content URI ending in the disc number, or the remote menu item's
        GUID when "CD" itself is picked (docs/app-protocol.md §5).
        """
        disc = source_id.rsplit("/", 1)[-1]
        if disc.isdigit():
            return self._trigger_content(source_id)

        plain = next((s for s in PROFILE.plain_sources if s.item_id == source_id), None)
        if plain is not None:
            log.info("     -> source %s selected", plain.name)
            if _ml is not None:
                threading.Thread(target=_ml.select_source, args=(plain.slot,), daemon=True).start()
            apply_ml_source(plain.slot, from_machine=False)
            return self._send({}, 200)

        if source_id == REMOTE_CA_ID:
            # The Classics Adapter source itself: resume whatever disc is loaded.
            # The machine may be off, on another source of its own or on a
            # network source, so it is told; on a disc that is playing the
            # key changes nothing.
            _active["source"] = None
            ml_wake()
            _active["state"] = "started"
            log.info("     -> source CD selected, continuing on CD %d track %d",
                     _active["cd"], _active["track"])
            notifier.send("WebSocketEventSourceChange",
                          mozart_source(active_content_uri(),
                                        f"CD {_active['cd']}", "classicsAdapter"))
            self._announce_track()
            return self._send({}, 200)

        log.warning("     -> unknown source id %s", source_id)
        return self._send({}, 200)

    def do_PUT(self):
        self._write_like("PUT ")

    def do_POST(self):
        self._write_like("POST")

    def do_PATCH(self):
        self._write_like("PATC")

    def do_DELETE(self):
        self._write_like("DEL ")

    def log_message(self, *a):   # silence BaseHTTPRequestHandler's own logging
        pass


def get_lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()


def connect_ml():
    """Report which MasterLink backend is in use.

    Optional on purpose: the facade has to keep working on a laptop with no
    BeoSound 9000 attached.
    """
    if _ml is None:
        log.warning("ML:   no MasterLink backend - the app's commands never reach the 9000")
    else:
        log.info("ML:   backend %s", type(_ml).__name__)


def request_ml_audio():
    """Ask the machine to put audio on the bus. Without it there is nothing to send."""
    if _ml is None:
        log.warning("ML:   no MasterLink backend - cannot request audio on the bus")
        return False
    try:
        _ml.request_audio()
        log.warning("ML:   link-room request sent (%s)", _active["source"] or "the discs")
        return True
    except Exception as e:
        log.warning("ML:   the link-room request failed (%s)", e)
        return False


_mdns = None


def shutdown_mdns():
    """The mDNS goodbye (TTL 0) for our record, once. Called from the SIGTERM
    handler BEFORE the process exits: without it the app keeps the record,
    never sees the product go away, and never reconnects after a restart."""
    global _mdns
    if _mdns is None:
        return
    zc, info = _mdns
    _mdns = None
    try:
        zc.unregister_service(info)
        zc.close()
        log.info("stopped, mDNS goodbye sent")
    except Exception as e:      # noqa: BLE001
        log.warning("mDNS goodbye failed: %s", e)


def run(argv=None, ml_backend=None, beolink_out_factory=None, profile=None):
    """Start the Mozart facade.

    ml_backend: an MlBackend. beolink_out_factory: callable(jid, name, peers,
    device=..., tone=...) -> object with add/remove/clear/progress/listeners.
    The facade has none of its own: without one, --beolink has no sound.
    profile: a ProductProfile saying what product this is. The default is a
    plain speaker, so an application with real hardware behind it passes its
    own. It is applied before the arguments are parsed, so the defaults the
    command line shows are the profile's.
    """
    global TYPE_NUMBER, ITEM_NUMBER, SERIAL, _beolink_out
    if profile is not None:
        _apply_profile(profile)
    ap = argparse.ArgumentParser()
    ap.add_argument("--ip", default=None, help="IP to advertise (default: auto LAN IP)")
    ap.add_argument("--port", type=int, default=80,
                    help="REST API port. The app requests http://{ip} with no "
                         "port, so this must be 80 for the app to reach it")
    ap.add_argument("--name", default=None,
                    help="what the product is called in the app and on the network. "
                         "Left out, it is the profile's own name")
    ap.add_argument("--faithful", action="store_true",
                    help="report only what a real Beosound 9000 can: a disc name "
                         "and a track number. No titles, artists or cover art — "
                         "it has no CD-Text, only names typed in on a Beo4")
    ap.add_argument("--typenumber", default=TYPE_NUMBER,
                    help="mDNS TXT 'tn' — must be a type compiled into the app. "
                         "4600/116 (the real Classics Adapter) are NOT and get "
                         "discarded as 'Unknown product'")
    ap.add_argument("--itemnumber", default=ITEM_NUMBER,
                    help="mDNS TXT 'in' — 1454530 is what gates the Classics Adapter check")
    ap.add_argument("--serial", default=SERIAL, metavar="NNNNNNNN",
                    help="mDNS TXT 'sn' and the jid's third field. Default: %s, from %s "
                         "or derived from the machine id. Changing it makes the app see a "
                         "different product." % (SERIAL, SERIAL_FILE))
    ap.add_argument("--beolink", action="store_true",
                    help="let the app's expand start a REAL BeoLink stream to "
                         "the speaker. Without it expand answers 200 and no "
                         "sound goes anywhere")
    ap.add_argument("--beolink-jid", default=None,
                    help="the identity we present on the BeoLink plane. The "
                         "default is the product's own jid, so the app side and "
                         "the audio side are ONE product; a speaker accepts "
                         "that without objection. Set it "
                         "only to recreate the old split arrangement")
    ap.add_argument("--beolink-device", default="hw:0,0")
    ap.add_argument("--beolink-tone", action="store_true",
                    help="send a 440 Hz tone to the listeners instead of the "
                         "sound card's audio: proves the BeoLink plane without "
                         "a machine on the bus")
    ap.add_argument("--airplay", action="store_true",
                    help="send the audio over AirPlay instead of BeoLink. "
                         "The BeoLink audio plane expects membership in the "
                         "speakers' cloud-managed device list, which cannot be "
                         "written locally; AirPlay is open, and the speaker "
                         "reports playback itself")
    a = ap.parse_args(argv)

    TYPE_NUMBER, ITEM_NUMBER, SERIAL = a.typenumber, a.itemnumber, a.serial
    # One product, one name, in every record (docs/app-protocol.md §2).
    global FRIENDLY, HOSTNAME
    if a.name:
        FRIENDLY = a.name
    # A host name takes letters, digits and hyphens only; a name that leaves
    # nothing of those falls back to the profile's.
    host = re.sub(r"[^A-Za-z0-9]+", "-", a.name or "").strip("-") or PROFILE.host_prefix
    HOSTNAME = host + "-" + SERIAL + ".local."
    ip = a.ip or get_lan_ip()
    global DISCS
    if a.faithful:
        DISCS = None
    global OUR_TRANSPORT
    try:
        with open("/proc/net/route") as f:
            ifaces = {l.split()[0] for l in f.read().splitlines()[1:] if l.split()}
        OUR_TRANSPORT = "wireless" if any(i.startswith("wl") for i in ifaces) else "wired"
    except OSError:
        pass
    if ml_backend is not None:
        set_ml_backend(ml_backend)
    connect_ml()

    if a.airplay:
        raise SystemExit('--airplay is not supported')
    elif a.beolink:
        BeolinkOut = beolink_out_factory or _no_beolink_out
        # One product, one jid, or the app cannot assemble the group
        # (docs/app-protocol.md §2).
        beolink_jid = (a.beolink_jid
                       or f"{TYPE_NUMBER}.{ITEM_NUMBER}.{SERIAL}"
                          "@products.bang-olufsen.com")
        if a.beolink_jid:
            log.warning("BeoLink jid %s differs from the Mozart jid — the app will "
                        "not be able to show the group", a.beolink_jid)
        _beolink_out = BeolinkOut(beolink_jid, FRIENDLY, peers(),
                                  device=a.beolink_device, tone=a.beolink_tone,
                                  product_type=PRODUCT_TYPE)
        if hasattr(_beolink_out, "on_expand_result"):
            _beolink_out.on_expand_result = _expand_result

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname).1s %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(LOG_PATH, encoding="utf-8")],
    )

    zc = Zeroconf()
    props = {b"tn": TYPE_NUMBER, b"in": ITEM_NUMBER, b"sn": SERIAL,
             b"fn": FRIENDLY, b"fv": FIRMWARE, b"wa": MAC}
    info = ServiceInfo(
        SERVICE_TYPE,
        f"{a.name}.{SERVICE_TYPE}",
        addresses=[socket.inet_aton(ip)],
        port=a.port,
        properties=props,
        # One host name, shaped like a real product's (docs/app-protocol.md §1).
        server=HOSTNAME,
    )
    # Announce like a product that has just rebooted, and wait out our own
    # stale record instead of dying (docs/app-protocol.md §1).
    from zeroconf import NonUniqueNameException
    for attempt in range(1, 16):
        try:
            # Skip the conflict probe: the name is ours by construction.
            zc.register_service(info, cooperating_responders=True)
            break
        except NonUniqueNameException:
            if attempt == 15:
                raise
            log.warning("mDNS: '%s' is still announced by someone (a previous instance?); "
                        "retrying in 2 s (%d/15)", a.name, attempt)
            time.sleep(2.0)
    global _mdns
    _mdns = (zc, info)
    log.info("mDNS: %s '%s' at %s:%d", SERVICE_TYPE, a.name, ip, a.port)
    log.info("  TXT: tn=%s in=%s sn=%s fn='%s'", TYPE_NUMBER, ITEM_NUMBER, SERIAL, FRIENDLY)

    # Port 80 for the REST API and 8080 for the liveness ping, both required
    # (docs/app-protocol.md §1).
    ports = [a.port] + ([PING_PORT] if a.port != PING_PORT else [])
    servers = []
    for p in ports:
        srv = ThreadingHTTPServer(("0.0.0.0", p), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)

    notifier.start()
    threading.Thread(target=progress_ticker, daemon=True).start()
    log.info("HTTP: Mozart api/v1 + legacy BeoZone on ports %s",
             ", ".join(str(p) for p in ports))
    log.info("Logging to %s. Watch for 'content trigger modified' x6 = success.", LOG_PATH)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        for srv in servers:
            srv.shutdown()
        shutdown_mdns()


def _no_beolink_out(*args, **kwargs):
    """Stands in when the application hands in no BeoLink audio side. The
    facade still runs: the app sees the product and the machine is controlled,
    but "expand to" produces no audio."""
    log.error("no BeoLink audio side was handed in: expand and unexpand answer without sound")
    return None


def main():
    run()


if __name__ == "__main__":
    main()
