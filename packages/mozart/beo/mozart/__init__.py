"""beo.mozart - be a Mozart product to the Bang & Olufsen app.

The app-facing side: api/v1 over HTTP, notifications over a WebSocket, and
the mDNS records that make the app find us. It speaks to no other product;
that is beo.networklink's job, and the application joins the two.

Modules: facade, ws, profile. What the app requires of each answer is in
docs/app-protocol.md.
"""
from __future__ import annotations

__version__ = "0.1.0"

from .facade import run
from .profile import (DEFAULT_TYPE, MOZART_TYPES, REMOTE_ROOT_ID, MozartType,
                      ProductProfile, mozart_type)

__all__ = ["__version__", "run", "DEFAULT_TYPE", "MOZART_TYPES", "REMOTE_ROOT_ID",
           "MozartType", "ProductProfile", "mozart_type"]
