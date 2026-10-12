"""Who a product says it is.

The serial recurs in the jid, in the mDNS records, in the host name and in every
beocore stanza, and all of them must read it from the SAME object. Extracted
from the NetworkLink library and from the adapter, which had each built their
own and then disagreed about the host name.
"""
from __future__ import annotations

import hashlib
import io
import os
import uuid
from dataclasses import dataclass
from typing import Optional

# Shared by every product on the network; the jid is "<type>.<item>.<serial>@DOMAIN".
DOMAIN = "products.bang-olufsen.com"

# 99xxxxxx sits deliberately above the real counter: a Beosound Level built no
# later than early 2026 carried 38.2 million. A serial in this band can never
# collide with a shipped unit, and it is recognisable as ours in a real
# product's own log. See docs on identity for the sightings behind that number.
FAKE_BAND = (99_000_000, 99_999_999)
SERIAL_FILE = os.environ.get("BEO_SERIAL_FILE", "/etc/beo/serial")


def derive_serial(seed: Optional[str] = None) -> str:
    """99 plus six digits out of the machine's own id. Same machine, same serial.

    Never random. The jid is the app's primary key for a product: a serial that
    changes between installs makes the app see a brand-new product, leaves the
    old one as an unreachable ghost, and leaves listeners holding a stale jid.
    """
    if seed is None:
        for p in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
            try:
                seed = io.open(p, encoding="utf-8").read().strip()
            except OSError:
                continue
            if seed:
                break
    if not seed:
        seed = "%012x" % uuid.getnode()
    h = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    lo, hi = FAKE_BAND
    return str(lo + int(h[:12], 16) % (hi - lo + 1))


def load_serial(path: Optional[str] = None) -> str:
    """Explicit beats persisted beats derived."""
    env = os.environ.get("BEO_SERIAL", "").strip()
    if env:
        return env
    try:
        held = io.open(path or SERIAL_FILE, encoding="utf-8").read().strip()
    except OSError:
        held = ""
    return held or derive_serial()


@dataclass(frozen=True)
class ProductIdentity:
    """The identity a product presents, on every plane at once.

    ``jid`` may be given bare (``2730.1200322.90000001``) or full; ``full_jid``
    always returns the full form, which is what every stanza carries.

    ``product_type`` is the TypeName the app knows the product by, not the
    hardware code (``CA17``). It is empty unless the caller says what the
    product is; there is no neutral name to fall back on.

    ``host`` is the mDNS host name without ``.local``. If it is empty it is
    built from ``host_prefix`` and the serial. There is deliberately no
    fallback beyond that: a product that announces another product's host name
    appears on the network as that other product.
    """

    jid: str
    name: str
    mac: str
    type_no: int
    item_no: int
    serial: int
    product_type: str = ""
    host: str = ""
    host_prefix: str = ""

    def full_jid(self) -> str:
        """``<type>.<item>.<serial>@products.bang-olufsen.com``."""
        return self.jid if "@" in self.jid else "%s@%s" % (self.jid, DOMAIN)

    def bare_jid(self) -> str:
        """The jid without the domain."""
        return self.jid.split("@", 1)[0]

    def host_name(self) -> str:
        """mDNS host name without ``.local``."""
        if self.host:
            return self.host
        if self.host_prefix:
            return "%s-%s" % (self.host_prefix, self.serial)
        raise ValueError(
            "identity %s has neither host nor host_prefix. Guessing one means "
            "announcing some other product's host name." % self.bare_jid())

    @classmethod
    def from_jid(cls, jid: str, name: str, mac: str, *,
                 product_type: str = "",
                 host: Optional[str] = None,
                 host_prefix: str = "") -> "ProductIdentity":
        """Build an identity from a jid, parsing type/item/serial out of it.

        The type and item numbers MUST agree with the jid: advertising
        2730/1200322 under a jid that says 3150/1454530 produced a product
        whose own fields contradicted each other.
        """
        parts = jid.split("@")[0].split(".")
        if len(parts) != 3:
            raise ValueError("jid must be <type>.<item>.<serial>[@domain]: %r" % jid)
        type_no, item_no, serial = (int(p) for p in parts)
        return cls(jid=jid, name=name, mac=mac, type_no=type_no, item_no=item_no,
                   serial=serial, product_type=product_type,
                   host=host or "", host_prefix=host_prefix)
