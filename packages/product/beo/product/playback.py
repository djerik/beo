"""What a product is doing, and how loud.

These say nothing about any wire. Master Link reports play state as a byte in a
status telegram, the Mozart API as a string in a JSON body, NetworkLink as an
element in a progress notification. Each maps to and from these members inside
its own package, once.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass


class PlayState(enum.Enum):
    UNKNOWN = "unknown"
    STOP = "stop"
    PLAY = "play"
    WIND = "wind"
    REWIND = "rewind"
    STANDBY = "standby"
    NO_MEDIA = "no_media"   # a tray with no disc in it


@dataclass(frozen=True)
class Volume:
    """A level inside a range, plus mute.

    The range travels with the level: a linked speaker scales the level
    against the range it is given.
    """

    level: int = 35
    muted: bool = False
    minimum: int = 0
    maximum: int = 90
