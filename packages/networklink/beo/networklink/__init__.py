"""beo.networklink - the Bang & Olufsen NetworkLink protocol family.

Discovery over mDNS, the BeoCore session and its stanzas, FPS audio
streaming with PTP timing, the source role that plays to other products,
the listener role that is played to, the configuration the products share,
and the NSDK client that reads a product's logs. The protocol is
undocumented; the behaviour here is what the shipping products do on the
wire.
"""
from __future__ import annotations

from beo.product.identity import DOMAIN, ProductIdentity
from .sources import (Catalog, Experience, IrMapping, NowPlaying, SourceDescriptor,
                      StatusBuilder, Volume, SOURCES, STANDARD, STANDARD_SOURCES)
from .beocore import IdCounter, Session, Stanza
from .fps import (AudioFormat, FpsReceiver, FpsSender, MonotonicClock, OffsetPtpClock,
                  build_header, build_marker, FORMAT_44100, RATE_HZ)
from .audio import AlsaCapture, Fanout, Silence, ToneSource
from .discovery import Advertiser, AvahiAdvertiser, Browser, Peer, advertiser
from .roles import SourceRole

__all__ = [
    "DOMAIN", "ProductIdentity",
    "Catalog", "Experience", "IrMapping", "NowPlaying", "SourceDescriptor",
    "StatusBuilder", "Volume", "SOURCES", "STANDARD", "STANDARD_SOURCES",
    "IdCounter", "Session", "Stanza",
    "AudioFormat", "FpsReceiver", "FpsSender", "MonotonicClock", "OffsetPtpClock",
    "build_header", "build_marker", "FORMAT_44100", "RATE_HZ",
    "AlsaCapture", "Fanout", "Silence", "ToneSource",
    "Advertiser", "AvahiAdvertiser", "advertiser", "Browser", "Peer",
    "SourceRole",
]
