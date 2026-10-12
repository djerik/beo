"""The product's own output: a sound card that plays to a speaker directly,
beside the machine or with no machine at all.

The stream is written to the card at full scale and the card's mixer sets the
level, so the volume costs nothing per sample and a mute is a mixer setting
too. A machine has a volume of its own; a sound card has only this.
"""
from __future__ import annotations

import logging
import re
import subprocess

from beo.networklink.audio import AlsaPlayback

log = logging.getLogger("beo-adapter.speaker")


def card_of(device: str) -> str:
    """The card an ALSA device name points at: 'plughw:CARD=Headphones,DEV=0'
    and 'hw:1,0' each name one, by id or by number."""
    spec = device.split(":", 1)[-1].split(",")[0]
    return spec[len("CARD="):] if spec.startswith("CARD=") else spec


def volume_control(contents: str) -> str:
    """The mixer control that sets the playback level, out of what amixer
    lists for a card.

    A card has several controls with a playback volume, and some are a
    two-step analogue gain. The one with the widest range is the level.
    """
    best, widest = "", 0
    for name, body in re.findall(r"Simple mixer control '([^']+)',\d+\n((?:  .*\n?)*)", contents):
        if not re.search(r"Capabilities:.*\bpvolume\b", body):
            continue
        limits = re.search(r"Limits:.*?Playback (-?\d+) - (-?\d+)", body)
        span = int(limits.group(2)) - int(limits.group(1)) if limits else 0
        if span > widest:
            best, widest = name, span
    return best


class Speaker:
    """One sound card output and the mixer control that sets its level.

    ``top`` is the top of the product's volume scale, which is full scale on
    the card. ``control`` names the mixer control; left empty it is looked up
    on the card the first time a level is set.
    """

    def __init__(self, device: str, top: int, control: str = "", *, run=subprocess.run):
        self.device = device
        self.card = card_of(device)
        self.top = max(1, int(top))
        self.control = control
        self.run = run
        self.level, self.muted = 0, False
        self._failed = False

    def volume(self, level, muted) -> None:
        """The product's volume stands here now."""
        self.level, self.muted = int(level), bool(muted)
        self._apply()

    def open(self, now_ns=None) -> AlsaPlayback:
        """The level first and the sound after: a card keeps the level it was
        last left at, by whoever left it there."""
        self._apply()
        return AlsaPlayback(self.device, now_ns=now_ns).start()

    def _apply(self) -> None:
        percent = 0 if self.muted else round(100 * min(self.level, self.top) / self.top)
        try:
            if not self.control:
                listed = self.run(["amixer", "-c", self.card, "scontents"], capture_output=True,
                                  text=True, timeout=3, check=True).stdout
                self.control = volume_control(listed)
                if not self.control:
                    raise ValueError("card %s has no playback volume" % self.card)
                log.info("the level of %s is set on its mixer control %r", self.device, self.control)
            # Mapped, so that half way on the scale is half as loud and not
            # half way down a range of a hundred decibels.
            self.run(["amixer", "-q", "-M", "-c", self.card, "sset", self.control, "%d%%" % percent],
                     capture_output=True, timeout=3, check=True)
            self._failed = False
        except (OSError, ValueError, subprocess.SubprocessError) as e:
            # Said once: a slider being dragged sets the level many times.
            if not self._failed:
                self._failed = True
                log.error("the level of %s could not be set (%s); it plays at the level "
                          "the card stands at", self.device, e)


__all__ = ["Speaker", "card_of", "volume_control"]
