"""mDNS: find the products (Browser) and be one (Advertiser, AvahiAdvertiser).

``advertiser()`` picks the publisher: the machine's Avahi daemon where one
runs, since it writes the jid as the one name label every peer asks for, and
zeroconf elsewhere.
"""
from __future__ import annotations

import logging
import socket
import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

from beo.product.identity import ProductIdentity

log = logging.getLogger("beo.networklink.discovery")

# The capability hash. It describes the CAPABILITIES, not the device — every
# product shares exactly this one. A ver nobody recognises cannot be looked
# up.
CAPS_VER = "dSsQBJenapvOryzvtU90IM7SI+o="

SERVICES = ("BeoInput,BeoNotify,BeoZone,BeoDevice,"
            "BeoContent,BeoHome,BeoSecurity")

@dataclass(frozen=True)
class Peer:
    jid: str
    ip: str
    name: str = ""
    host: str = ""
    product_type: str = ""


class Browser:
    """Find ASE products: ``_beocore`` gives jid -> ip, ``_beoremote`` gives
    name, productType and host."""

    def peers(self, seconds: float = 6) -> List[Peer]:
        from zeroconf import Zeroconf, ServiceBrowser, ServiceListener
        found: Dict[str, dict] = {}

        class L(ServiceListener):
            def _s(self, z, t, n):
                try:
                    i = z.get_service_info(t, n, timeout=2500)
                except Exception:
                    return
                if not (i and i.parsed_addresses()):
                    return
                props = {k.decode() if isinstance(k, bytes) else k:
                         (v.decode() if isinstance(v, bytes) else v)
                         for k, v in (i.properties or {}).items()}
                jid = props.get("jid") or n
                d = found.setdefault(jid, {"jid": jid})
                d["ip"] = i.parsed_addresses()[0]
                if t.startswith("_beoremote"):
                    d["name"] = props.get("name") or ""
                    d["product_type"] = props.get("productType") or ""
                    d["mac"] = props.get("mac") or ""
                d.setdefault("name", props.get("name") or "")
                d["host"] = (i.server or "").rstrip(".")

            add_service = update_service = _s

            def remove_service(self, *a):
                pass

        z = Zeroconf()
        ServiceBrowser(z, "_beocore._tcp.local.", L())
        ServiceBrowser(z, "_beoremote._tcp.local.", L())
        time.sleep(seconds)
        z.close()
        return [Peer(d["jid"], d["ip"], d.get("name", ""), d.get("host", ""),
                     d.get("product_type", "")) for d in found.values()]


class PeerDirectory:
    """Who else is on the network, kept current for as long as it runs.

    A real product does not ship with a list of its neighbours. It listens, and
    products appear and disappear as they are switched on and off. This holds a
    pair of mDNS browsers open and keeps a dictionary up to date behind them, so
    a caller can ask at any moment without waiting for a scan.

    Start it, ask ``peers()`` whenever you need the answer, and close it when the
    process ends. The first answer may be empty: mDNS takes a moment, and an
    empty list is the truthful answer to "who have I heard from so far".
    """

    # Not in any record, and not knowable from one. Products report themselves
    # as wireless, and the field only takes "wired" or "wireless", so that is
    # the honest default rather than a guess at the cabling in somebody's
    # house.
    DEFAULT_TRANSPORT = "wireless"

    def __init__(self, exclude_jid: str = ""):
        self.exclude_jid = exclude_jid.split("@")[0]
        self._found: Dict[str, dict] = {}
        self._lock = threading.Lock()
        self._zc = None
        self._browsers: List[object] = []

    # ------------------------------------------------------------- lifecycle
    def start(self) -> "PeerDirectory":
        from zeroconf import Zeroconf, ServiceBrowser, ServiceListener

        directory = self

        class L(ServiceListener):
            def _seen(self, z, type_, name):
                try:
                    info = z.get_service_info(type_, name, timeout=2500)
                except Exception:                       # noqa: BLE001
                    return
                if not (info and info.parsed_addresses()):
                    return
                props = {k.decode() if isinstance(k, bytes) else k:
                         (v.decode() if isinstance(v, bytes) else v)
                         for k, v in (info.properties or {}).items()}
                # _beocore and _beoremote carry the jid outright. The Mozart
                # record does not: it carries the three parts separately, and
                # falling back to the instance name there produced entries
                # whose "jid" was the product's friendly name.
                jid = props.get("jid") or ""
                if not jid and all(props.get(k) for k in ("tn", "in", "sn")):
                    jid = "%s.%s.%s@products.bang-olufsen.com" % (
                        props["tn"], props["in"], props["sn"])
                bare = jid.split("@")[0]
                if len(bare.split(".")) != 3:
                    return                              # not an identity we can use
                if bare == directory.exclude_jid:
                    return                              # ourselves
                with directory._lock:
                    d = directory._found.setdefault(jid, {
                        "jid": jid, "friendlyName": "", "ipAddress": "",
                        "productType": "",
                        "audioTransport": directory.DEFAULT_TRANSPORT})
                    d["ipAddress"] = info.parsed_addresses()[0]
                    if props.get("name"):
                        d["friendlyName"] = props["name"]
                    if props.get("productType"):
                        d["productType"] = props["productType"]

            add_service = update_service = _seen

            def remove_service(self, z, type_, name):
                """A product that has gone quiet is no longer somewhere to send
                audio, so it leaves the list rather than lingering as an entry
                that fails when the app offers it."""
                with directory._lock:
                    for jid, d in list(directory._found.items()):
                        if name.startswith(jid):
                            directory._found.pop(jid, None)

        self._zc = Zeroconf()
        for service in ("_beocore._tcp.local.", "_beoremote._tcp.local.",
                        "_bangolufsen._tcp.local."):
            self._browsers.append(ServiceBrowser(self._zc, service, L()))
        log.info("mDNS: watching for products, continuously")
        return self

    def close(self) -> None:
        if self._zc is not None:
            try:
                self._zc.close()
            except Exception:                           # noqa: BLE001
                pass
            self._zc = None
        self._browsers = []

    # ----------------------------------------------------------------- reads
    def peers(self) -> List[dict]:
        """Everyone heard from so far, as plain dictionaries.

        The keys are the ones the Mozart API uses, so an application can hand
        this straight to the facade without translating.
        """
        with self._lock:
            return [dict(d) for d in self._found.values() if d.get("ipAddress")]


class Announcement:
    """The four services a product has, with our own identity.

    A peer very likely decides what we ARE from _beoremote — that is where
    the type number, item number and productType live — and only then decides
    whether a _beocore entry is worth dialling. The _beocore instance name is
    the jid, ONE DNS label (invariant 13): a peer asks for the SRV and TXT of
    that one label, and a record whose name is split at the jid's dots does
    not answer its question. TTLs are a product's own: A/SRV 120, TXT/NSEC
    4500 — zeroconf's and avahi's defaults.
    """

    RETRIES = 6                  # retry when registration sees a conflict (our own leftovers)

    def __init__(self, ident: ProductIdentity, ip: str, *, caps_ver: str = CAPS_VER,
                 product_type: Optional[str] = None):
        self.ident, self.ip, self.caps_ver = ident, ip, caps_ver
        # What a product sends in _beoremote is its hardware code (CA17, say);
        # the TypeName the app knows it by is another string.
        self.product_type = product_type or ident.product_type

    def records(self) -> list:
        """(type, instance, port, txt) for the four services, in order."""
        jid = self.ident.full_jid()
        name = self.ident.name
        return [
            ("_beocore._tcp.local.", jid, 5222,
             {"hash": "sha-1", "jid": jid, "name": name, "node": "beocore",
              "txtvers": "1", "ver": self.caps_ver}),
            ("_beoremote._tcp.local.", name, 8080,
             {"assoc": "", "item": str(self.ident.item_no), "jid": jid,
              "mac": self.ident.mac, "name": name, "productType": self.product_type,
              "services": SERVICES, "type": str(self.ident.type_no)}),
            ("_http._tcp.local.", name, 80, {}),
            ("_beo_settings._tcp.local.", name, 80, {}),
        ]

    def host_name(self) -> str:
        raw = self.ident.host_name()
        host = raw if raw.endswith(".local") else raw + ".local"
        if not host.endswith("."):
            host += "."
        return host

    def start(self):
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError


class Advertiser(Announcement):
    """The announcement through python-zeroconf.

    zeroconf writes every name label by label, split at each dot, so the jid
    goes out as eight labels. A peer whose responder keeps the instance name
    as one label asks for that one label, gets a record under another name
    and never resolves us: it lists us by name but cannot find our address
    and never dials. Where Avahi runs, ``AvahiAdvertiser`` is the one to use;
    this one serves a machine without it, and the peers that split names
    the same way.
    """

    MIN_ZEROCONF = (0, 148, 0)   # below this zeroconf writes NSEC of the MISSING types

    def __init__(self, ident: ProductIdentity, ip: str, *, caps_ver: str = CAPS_VER,
                 product_type: Optional[str] = None):
        super().__init__(ident, ip, caps_ver=caps_ver, product_type=product_type)
        self._zc = None
        self._infos: list = []

    def start(self):
        import zeroconf as _zc
        from zeroconf import ServiceInfo, Zeroconf
        try:
            u = tuple(int(x) for x in _zc.__version__.split(".")[:3])
        except Exception:
            u = (0, 0, 0)
        if u < self.MIN_ZEROCONF:
            raise RuntimeError(
                "zeroconf %s writes NSEC of the MISSING types and makes us "
                "unresolvable. Requires at least %s."
                % (".".join(map(str, u)), ".".join(map(str, self.MIN_ZEROCONF))))
        addrs = [socket.inet_aton(self.ip)]
        host = self.host_name()
        self._zc = Zeroconf()
        self._infos = []
        for typ, instance, port, txt in self.records():
            info = ServiceInfo(
                typ,
                "%s.%s" % (instance, typ),
                addresses=addrs,
                port=port,
                properties=dict(txt),
                server=host,
                # A product's own TTLs: A/SRV 120, TXT/NSEC 4500 — zeroconf's
                # and avahi's defaults. With these a record appears in a
                # peer's Bonjour cache at once.
                host_ttl=120,
                other_ttl=4500,
            )
            # strict=False: B&O uses _beo_settings._tcp, and RFC 6763 does not
            # allow an underscore inside a service name. Avahi accepts it;
            # zeroconf rejects it unless the check is off. We imitate a
            # product that exists, not one that ought to.
            #
            # allow_name_change=False: better fail than come up as «<name>-2».
            # Starting shortly after ourselves, our own records still sit in
            # the others' caches and zeroconf reads them as a conflict. It is
            # not a stranger that took the name, it is us. We wait them out,
            # exactly as a real product does when it restarts.
            last_error = None
            for _ in range(self.RETRIES):
                try:
                    self._zc.register_service(info, allow_name_change=False, strict=False)
                    last_error = None
                    break
                except Exception as e:
                    last_error = e
                    time.sleep(3)
            if last_error is not None:
                self.stop()
                raise RuntimeError("%s could not be registered: %s: %s"
                                   % (info.name, type(last_error).__name__, last_error))
            self._infos.append(info)
        log.info("mDNS: %d records advertised for %s on %s", len(self._infos),
                 self.ident.full_jid(), host)
        return self._zc

    def stop(self) -> None:
        if self._zc is not None:
            for info in self._infos:
                try:
                    self._zc.unregister_service(info)
                except Exception:
                    pass
            try:
                self._zc.close()
            except Exception:
                pass
            self._zc = None
            self._infos = []


# Avahi's entry-group states and publish flags, as its D-Bus API numbers them.
AVAHI_ESTABLISHED, AVAHI_COLLISION, AVAHI_FAILURE = 2, 3, 4
AVAHI_IF_UNSPEC, AVAHI_PROTO_INET = -1, 0
AVAHI_PUBLISH_NO_REVERSE = 16


def service_type(full: str) -> str:
    """``_beocore._tcp`` out of ``_beocore._tcp.local.``: Avahi takes the type
    and the domain apart."""
    full = full.rstrip(".")
    return full[:-len(".local")] if full.endswith(".local") else full


class _DbusEntryGroup:
    """One Avahi entry group over the system bus: the records of one
    announcement, committed together and withdrawn together."""

    def __init__(self):
        import dbus
        self._dbus = dbus
        bus = dbus.SystemBus()
        server = dbus.Interface(bus.get_object("org.freedesktop.Avahi", "/"),
                                "org.freedesktop.Avahi.Server")
        self._group = dbus.Interface(bus.get_object("org.freedesktop.Avahi", server.EntryGroupNew()),
                                     "org.freedesktop.Avahi.EntryGroup")

    @staticmethod
    def available() -> bool:
        """Whether an Avahi daemon answers on the system bus."""
        try:
            import dbus
            bus = dbus.SystemBus()
            dbus.Interface(bus.get_object("org.freedesktop.Avahi", "/"),
                           "org.freedesktop.Avahi.Server").GetVersionString()
            return True
        except Exception:                               # noqa: BLE001
            return False

    def add_service(self, instance: str, type_: str, host: str, port: int, txt: dict) -> None:
        d = self._dbus
        # Avahi writes an empty list as a TXT of one empty string, as a
        # product does for the services that carry nothing.
        entries = d.Array([d.ByteArray(("%s=%s" % (k, v)).encode()) for k, v in txt.items()],
                          signature="ay")
        self._group.AddService(d.Int32(AVAHI_IF_UNSPEC), d.Int32(AVAHI_PROTO_INET), d.UInt32(0),
                               d.String(instance), d.String(service_type(type_)),
                               d.String(""), d.String(host), d.UInt16(port), entries)

    def add_address(self, host: str, ip: str) -> None:
        d = self._dbus
        # No reverse record: the daemon already publishes one for the
        # machine's own name, and a second answer for the same address would
        # collide with it.
        self._group.AddAddress(d.Int32(AVAHI_IF_UNSPEC), d.Int32(AVAHI_PROTO_INET),
                               d.UInt32(AVAHI_PUBLISH_NO_REVERSE), d.String(host), d.String(ip))

    def commit(self) -> None:
        self._group.Commit()

    def state(self) -> int:
        return int(self._group.GetState())

    def reset(self) -> None:
        self._group.Reset()

    def free(self) -> None:
        self._group.Free()


class AvahiAdvertiser(Announcement):
    """The announcement through the machine's Avahi daemon.

    Avahi keeps an instance name as one label whatever it contains, so the
    jid goes out as the peers expect it, and the daemon answers their
    questions for as long as the group lives. The address record for our
    host name goes in the same group: the daemon publishes addresses only
    for its own name, and ours is the product's.
    """

    SETTLE = 8.0                 # seconds to wait for the daemon's verdict on the names

    def __init__(self, ident: ProductIdentity, ip: str, *, caps_ver: str = CAPS_VER,
                 product_type: Optional[str] = None, group_factory=_DbusEntryGroup):
        super().__init__(ident, ip, caps_ver=caps_ver, product_type=product_type)
        self._group_factory = group_factory
        self._group = None

    def _fill(self, group) -> None:
        host = self.host_name().rstrip(".")
        group.add_address(host, self.ip)
        for typ, instance, port, txt in self.records():
            group.add_service(instance, typ, host, port, txt)
        group.commit()

    def _settle(self, group) -> int:
        deadline = time.monotonic() + self.SETTLE
        while True:
            state = group.state()
            if state in (AVAHI_ESTABLISHED, AVAHI_COLLISION, AVAHI_FAILURE) or time.monotonic() > deadline:
                return state
            time.sleep(0.2)

    def start(self):
        group = self._group_factory()
        self._group = group
        # A collision right after our own restart is our own records, still
        # in the others' caches. We wait them out, as a product does.
        for attempt in range(self.RETRIES):
            self._fill(group)
            state = self._settle(group)
            if state == AVAHI_ESTABLISHED:
                log.info("mDNS: %d records advertised through Avahi for %s on %s",
                         len(self.records()), self.ident.full_jid(), self.host_name())
                return group
            if state == AVAHI_COLLISION and attempt + 1 < self.RETRIES:
                group.reset()
                time.sleep(3)
                continue
            self.stop()
            raise RuntimeError("Avahi did not establish the announcement (state %d)" % state)

    def stop(self) -> None:
        if self._group is not None:
            try:
                self._group.free()
            except Exception:                           # noqa: BLE001
                pass
            self._group = None


def advertiser(ident: ProductIdentity, ip: str, *, caps_ver: str = CAPS_VER,
               product_type: Optional[str] = None,
               avahi_available=_DbusEntryGroup.available) -> Announcement:
    """The announcement for this machine: Avahi where a daemon answers, since
    every peer resolves what it publishes; zeroconf otherwise."""
    if avahi_available():
        return AvahiAdvertiser(ident, ip, caps_ver=caps_ver, product_type=product_type)
    log.warning("no Avahi daemon on the bus: announcing through zeroconf, which peers "
                "that keep the jid as one name label cannot resolve")
    return Advertiser(ident, ip, caps_ver=caps_ver, product_type=product_type)


def own_ip(toward: str = "203.0.113.1") -> str:
    """Our address on the network, not 127.0.0.1. Nothing is sent; a connected
    UDP socket just reveals which address the kernel would use for that target."""
    p = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        p.connect((toward, 1))
        return p.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        p.close()


__all__ = ["Peer", "Browser", "Announcement", "Advertiser", "AvahiAdvertiser",
           "advertiser", "CAPS_VER", "own_ip"]
