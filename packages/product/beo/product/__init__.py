"""beo.product - what a Bang & Olufsen product is, independent of protocol.

Types only: no sockets, no serial port, no sound card. Master Link, NetworkLink
and the Mozart API all describe the same handful of things with different
spelling, and each of them maps to and from these types inside the package that
owns its wire format.
"""
from .identity import DOMAIN, ProductIdentity, derive_serial, load_serial
from .playback import PlayState, Volume

__all__ = ["DOMAIN", "ProductIdentity", "derive_serial", "load_serial",
           "PlayState", "Volume"]
