"""The configuration a network of products shares, and a product's entry in it.

One document, held by a leader the products elect among themselves and
announce as ``_beoconfig._tcp``. Under ``Devices#1`` it has one entry per
product, named by the jid. A source streams only to a listener it finds
there, and offers only those as listeners, so the entry is what makes a
product part of the network.

A product enrols by sending its own entry to the leader in an ``Insert``. The
leader stamps the revision, stores it and passes it on to every subscriber;
an Insert sent to a product that is not the leader changes nothing.

The document is a tree of ``<item name=...>`` with ``<data>`` at the leaves.
A list repeats its name: ``Scheme#1``, ``Scheme#1_1``, ``Scheme#1_2``. An
empty value is an item named ``^emptyValue`` holding ``true``. Dots in an
item's name are written ``%2E``.
"""
from __future__ import annotations

import logging
import socket
import time
from typing import Callable, Iterable, Optional, Sequence, Tuple
from xml.sax.saxutils import escape

from beo.product.identity import ProductIdentity

from .beocore import Session
from .sources import SourceDescriptor

log = logging.getLogger("beo.networklink.configuration")

NS = "beocore.configuration.1"
SERVICE = "_beoconfig._tcp.local."

# What a listener says it can receive: the three protection schemes, and the
# one version of the stream there is.
RENDER_SCHEMES = ("NONE", "DECIMATE", "PROPRIETARY")
EXPERIENCES = ("join", "expand")
# How long the product takes from stream to sound, as the products in the
# network state it for themselves.
RENDER_DELAY_MS = 86


def key(name: str) -> str:
    """An item name as the document writes it."""
    return name.replace(".", "%2E")


def item(name: str, *children: str, data=None) -> str:
    body = "".join(children)
    if data is not None:
        body = "<data>%s</data>" % escape(str(data)) + body
    return '<item name="%s">%s</item>' % (key(name), body)


def empty() -> str:
    return item("^emptyValue", data="true")


def many(name: str, values: Sequence[str]) -> str:
    """A list: the same name, numbered from the second on."""
    return "".join(item(name if i == 0 else "%s_%d" % (name, i), data=v)
                   for i, v in enumerate(values))


def _schemes(values: Sequence[str]) -> str:
    return item("Schemes#1", many("Scheme#1", values)) if values else empty()


def source_entry(src: SourceDescriptor, ident: ProductIdentity, index: int) -> str:
    """One source, under the slot a listener knows it by."""
    codes = "".join(
        item("IrCode#1" if i == 0 else "IrCode#1_%d" % i,
             item("Format#1", data=m.format), item("Unit#1", data=m.unit),
             item("Command#1", data=m.command))
        for i, m in enumerate(src.ir_mapping))
    return item(
        src.beocore_identifier,
        item("Name#1", data=src.name),
        item("SourceType#1", item("Type#1", data=src.source_type)),
        item("Category#1", data=src.category),
        item("InUse#1", data="true" if src.in_use else "false"),
        item("Profile#1", empty()),
        item("Linkable#1", data="true" if src.linkable else "false"),
        item("ContentProtection#1", _schemes((src.protection,))),
        item("EmbeddedBinary#1", _schemes(src.embedded_binary)),
        item("UniqueSourceId#1", item("Source#1", data=src.key),
             item("Jid#1", data=ident.full_jid())),
        item("RecommendedIrMappings#1", codes or empty()),
        item("Index#1", data=index),
        item("ProductName#1", data=ident.name))


def device_entry(ident: ProductIdentity, sources: Iterable[SourceDescriptor], *,
                 wireless: bool = True, render_delay_ms: int = RENDER_DELAY_MS) -> str:
    """A product's whole entry, in the order a product writes its own."""
    return item(
        ident.full_jid(),
        item("Name#1", data=ident.name),
        item("Macs#1", item("Mac#1", data=ident.mac.upper())),
        item("Sources#1", *(source_entry(s, ident, i) for i, s in enumerate(sources))),
        item("ConnectionType#1", data="WIRELESS" if wireless else "WIRED"),
        item("RenderDelayMs#1", data=render_delay_ms),
        item("Render#1",
             item("Fps#1", item("ContentProtection#1", _schemes(RENDER_SCHEMES)),
                  item("StreamingVersions#1", item("Ver#1", data=1))),
             item("Multiroom#1", item("SupportedExperiences#1",
                                      many("SupportedExperience#1", EXPERIENCES)))),
        item("EmbeddedBinary#1", empty()),
        item("Fps#1", item("StreamingVersions#1", item("Ver#1", data=1))),
        item("RoleList#1", item("Roles#1", item("Role#1", data="integratedMaster"))),
        item("Integrate#1", item("Role#1", data="none"), item("Jid#1", empty())))


def insert_request(entry_xml: str) -> str:
    """The iq body that enrols an entry. No revision: the leader stamps it."""
    return ("<Insert xmlns='%s'><config>%s</config><version>1</version></Insert>"
            % (NS, item("Devices#1", entry_xml)))


def subscribe_request() -> str:
    """Ask for the whole document, and for every change after it."""
    return ("<_notification-subscribe xmlns='%s'><configuration><revision>0</revision>"
            "</configuration></_notification-subscribe>" % NS)


def enrolled(document: str, jid: str) -> bool:
    """Whether the document has an entry for this jid (a deletion mark is not one)."""
    devices = document.split("<deleted>", 1)[0]
    return '<item name="%s">' % key(jid) in devices


def find_leader(seconds: float = 8.0) -> Optional[Tuple[str, str]]:
    """(ip, jid) of whoever holds the configuration now, or None.

    It changes several times a day, so it is looked up each time it is needed.
    """
    from zeroconf import ServiceBrowser, Zeroconf
    found = []

    class Listener:
        def add_service(self, zc, typ, name):
            info = zc.get_service_info(typ, name, 3000)
            if not info or not info.addresses:
                return
            jid = (info.properties.get(b"jid") or b"").decode()
            if jid:
                found.append((socket.inet_ntoa(info.addresses[0]), jid))
        update_service = add_service

        def remove_service(self, *a):
            pass

    zc = Zeroconf()
    try:
        ServiceBrowser(zc, SERVICE, Listener())
        deadline = time.time() + seconds
        while not found and time.time() < deadline:
            time.sleep(0.2)
    finally:
        zc.close()
    return found[0] if found else None


def ask(ident: ProductIdentity, ip: str, jid: str, body: str, *, timeout: float = 30,
        session_factory: Callable[..., Session] = Session) -> Optional[str]:
    """One request to the leader on a session of its own: it stops answering
    a session that has written to it."""
    session = session_factory(ident, ip, peer_jid=jid, name="networklink-configuration")
    session.open()
    try:
        # The leader learns what we are from its own question to us, which
        # follows the stream header; a request that overtakes it may go
        # unanswered.
        time.sleep(2.0)
        return session.request(body, timeout=timeout)
    finally:
        session.close()


PRESENT, ENROLLED, NO_LEADER, NO_ANSWER, REFUSED = "present", "enrolled", "no leader", "no answer", "refused"


def ensure_enrolled(ident: ProductIdentity, entry_xml: str, *,
                    leader: Callable[[], Optional[Tuple[str, str]]] = find_leader,
                    session_factory: Callable[..., Session] = Session) -> str:
    """See that the product has an entry, writing it only if it has none.

    The document is read first. An entry that is already there is left as it
    is, whatever it says: this adds a product, it does not edit one, and it
    never removes anything.
    """
    who = leader()
    if who is None:
        return NO_LEADER
    ip, leader_jid = who
    jid = ident.full_jid()
    document = ask(ident, ip, leader_jid, subscribe_request(), timeout=40,
                   session_factory=session_factory)
    if document is None:
        return NO_ANSWER
    if enrolled(document, jid):
        return PRESENT
    log.warning("%s is not in the configuration held by %s; sending its entry", jid, leader_jid)
    reply = ask(ident, ip, leader_jid, insert_request(entry_xml), session_factory=session_factory)
    if reply is None:
        return NO_ANSWER
    if "<accepted>1</accepted>" not in reply:
        log.error("the leader did not accept the entry: %s", reply[:400])
        return REFUSED
    return ENROLLED


__all__ = ["NS", "SERVICE", "device_entry", "source_entry", "insert_request",
           "subscribe_request", "enrolled", "item", "key", "find_leader", "ask",
           "ensure_enrolled", "PRESENT", "ENROLLED", "NO_LEADER", "NO_ANSWER", "REFUSED"]
