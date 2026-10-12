"""What can sit behind the product.

A machine is two descriptions that have to agree: the one the bridge speaks
to it on the bus by, and the profile that tells the app what it is. They are
chosen together, by one name, because choosing them apart lets the app draw
six discs in front of a machine that has none.

A machine with one disc is shown with the same six: the app has no other disc
player to draw, and whichever of them is picked, the one disc plays.

With no machine the product is a plain speaker: it appears in the app, answers
everything, and has nothing to play.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Tuple

from beo.mozart import facade
from beo.mozart.profile import MozartType, ProductProfile
from beo.networklink.sources import SOURCES, STANDARD_SOURCES, SourceDescriptor

log = logging.getLogger("beo-adapter.machine")

NONE = "none"


@dataclass(frozen=True)
class Machine:
    """One classic product this adapter can drive.

    `verified` says whether it has been run against the real machine. Which
    sources it has is said once, by its music system; the list other products
    are given and the app's menu are both read from there. The imports are
    deferred so that a product with nothing behind it never loads a bus
    library it has no use for.
    """

    key: str
    description: str
    verified: bool
    _load: Callable[[], Tuple[object, type]]

    @property
    def slots(self) -> Tuple[str, ...]:
        """The slots of the machine's sources, the main one first."""
        return tuple(s.key for s in self._load()[0].sources)

    @property
    def sources(self) -> Tuple[SourceDescriptor, ...]:
        """What the product lists to other products: the machine's sources."""
        return tuple(SOURCES[slot] for slot in self.slots)

    def bridge(self, port: str, **options) -> facade.MlBackend:
        """The bus side, not yet started."""
        from .ml_bridge import MlBridge
        return MlBridge(self._load()[0], port, **options)

    def profile(self, mozart: Optional[MozartType] = None) -> ProductProfile:
        """What the app sees in front of this machine."""
        return self._load()[1](mozart, sources=self.slots)


def _disc_player(key: str) -> Callable[[], Tuple[object, type]]:
    """A music system by its key, with the one disc player the app can draw."""
    def load() -> Tuple[object, type]:
        from beo.masterlink.profiles import PROFILES
        from .classics_adapter_profile import ClassicsAdapterProfile
        return PROFILES[key], ClassicsAdapterProfile
    return load


MACHINES: Dict[str, Machine] = {m.key: m for m in [
    Machine("bs9000", "BeoSound 9000 on Master Link", True, _disc_player("bs9000")),
    Machine("bs3000", "BeoSound 3000 on Master Link", False, _disc_player("bs3000")),
    Machine("bs3200", "BeoSound 3200 on Master Link", False, _disc_player("bs3200")),
    Machine("bs4", "BeoSound 4 on Master Link", False, _disc_player("bs4")),
    Machine("bc2", "BeoCenter 2 on Master Link", False, _disc_player("bc2")),
]}


def machine_named(key: Optional[str]) -> Optional[Machine]:
    """The machine for a configuration value, or None for a plain speaker."""
    k = (key or NONE).strip().lower()
    if k in (NONE, "no", "off", ""):
        return None
    try:
        return MACHINES[k]
    except KeyError:
        raise ValueError("unknown machine %r; known: %s, or %s"
                         % (key, ", ".join(sorted(MACHINES)), NONE)) from None


def profile_for(machine: Optional[Machine], mozart: Optional[MozartType] = None) -> ProductProfile:
    """The machine's own profile, or a plain speaker when there is none."""
    return machine.profile(mozart) if machine is not None else ProductProfile(mozart)


def sources_for(machine: Optional[Machine]) -> Tuple[SourceDescriptor, ...]:
    """What the product lists to other products: the machine's sources, or a
    networked product's own when there is none."""
    return machine.sources if machine is not None else STANDARD_SOURCES


class NoMachine(facade.MlBackend):
    """Nothing on the other side: every command is logged and goes nowhere."""

    def press(self, key):
        log.info("(no machine) press %s", key)

    def wake(self):
        log.info("(no machine) wake")

    def select_disc(self, n):
        log.info("(no machine) select_disc %s", n)

    def goto_track(self, n):
        log.info("(no machine) goto_track %s", n)

    def select_source(self, name):
        log.info("(no machine) select_source %s", name)

    def request_audio(self):
        return False


__all__ = ["MACHINES", "Machine", "NONE", "NoMachine", "machine_named", "profile_for",
           "sources_for"]
