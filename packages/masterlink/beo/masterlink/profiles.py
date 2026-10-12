"""What differs between one music system and the next, as data.

A music system is driven the same way whatever it is: keys in, the machine's
own status out. What is its own is which sources it has, how many discs, and
the timings of the few sequences that had to be measured on it. A profile says
those, and says whether they were measured or assumed.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from .codec import Beo4, SRC_CD, SRC_RADIO


@dataclass(frozen=True)
class Source:
    """One source of a music system: the slot it is known by, the byte that
    names it in a telegram, and the key that selects it."""

    key: str
    code: int
    select: Beo4


@dataclass(frozen=True)
class DiscSelection:
    """How a machine with several discs is brought to one of them.

    A bare digit is tried first. When it goes unanswered the machine is taken
    off the source and brought back, standby, source key, digit, which opens
    the window in which a digit means disc. Every number is the machine's
    own, so none of them has a default.
    """

    count: int
    digit_timeout_s: float          # a bare digit's wait for the new disc
    standby_delay_s: float          # standby, then the source key
    digit_delay_s: float            # the source key, then the digit
    confirm_timeout_s: float        # the standby route's wait for the new disc


@dataclass(frozen=True)
class MusicSystemProfile:
    """One music system. `verified` says whether it has been run against the
    real machine. The first source is the one the queued operations are
    confirmed on; `discs` is None for a machine that has no discs to choose
    between."""

    key: str
    name: str
    verified: bool
    sources: Tuple[Source, ...]
    discs: Optional[DiscSelection] = None

    @property
    def main(self) -> Source:
        return self.sources[0]

    def source(self, key: str) -> Optional[Source]:
        """The source in a slot, whatever the case the slot is written in."""
        for s in self.sources:
            if s.key == key.upper():
                return s
        return None

    def source_coded(self, code: Optional[int]) -> Optional[Source]:
        """The source a telegram's source byte names, if it is one of ours."""
        for s in self.sources:
            if s.code == code:
                return s
        return None


CD_AND_RADIO = (Source("CD", SRC_CD, Beo4.CD), Source("RADIO", SRC_RADIO, Beo4.RADIO))

BS9000 = MusicSystemProfile(
    "bs9000", "BeoSound 9000", True, CD_AND_RADIO,
    DiscSelection(count=6, digit_timeout_s=3.0, standby_delay_s=3.5, digit_delay_s=0.3,
                  confirm_timeout_s=10.0))

# Written from what these machines are, and not from the bus: one disc, and
# the two sources each of them has. Any further source one of them has is left
# out until its byte has been seen in a telegram.
BS3000 = MusicSystemProfile("bs3000", "BeoSound 3000", False, CD_AND_RADIO)
BS3200 = MusicSystemProfile("bs3200", "BeoSound 3200", False, CD_AND_RADIO)
BS4 = MusicSystemProfile("bs4", "BeoSound 4", False, CD_AND_RADIO)
BC2 = MusicSystemProfile("bc2", "BeoCenter 2", False, CD_AND_RADIO)

PROFILES: Dict[str, MusicSystemProfile] = {p.key: p for p in (BS9000, BS3000, BS3200, BS4, BC2)}
