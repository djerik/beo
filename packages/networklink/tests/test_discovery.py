import pytest

from beo.networklink.discovery import (AVAHI_COLLISION, AVAHI_ESTABLISHED, Advertiser,
                                       AvahiAdvertiser, CAPS_VER, advertiser, service_type)
from beo.product.identity import ProductIdentity

IDENT = ProductIdentity.from_jid("2730.1200322.90000001", "Reference Source", "00:00:5E:00:53:01",
                                 product_type="CA17", host_prefix="Reference-Source")


def test_advertiser_records_four_services_single_label_instance():
    """Invariant 13: the _beocore instance name is the jid, ONE label."""
    a = Advertiser(IDENT, "203.0.113.2")
    recs = a.records()
    assert [r[0] for r in recs] == ["_beocore._tcp.local.", "_beoremote._tcp.local.",
                                    "_http._tcp.local.", "_beo_settings._tcp.local."]
    assert recs[0][1] == "2730.1200322.90000001@products.bang-olufsen.com"
    assert recs[0][2] == 5222 and recs[0][3]["ver"] == CAPS_VER and recs[0][3]["node"] == "beocore"
    assert recs[1][2] == 8080 and recs[1][3]["type"] == "2730" and recs[1][3]["item"] == "1200322"
    assert recs[1][3]["productType"] == "CA17"
    assert recs[1][3]["services"] == "BeoInput,BeoNotify,BeoZone,BeoDevice,BeoContent,BeoHome,BeoSecurity"
    assert a.host_name() == "Reference-Source-90000001.local."
    assert Advertiser(IDENT, "1.1.1.1", product_type="CA44").records()[1][3]["productType"] == "CA44"


class FakeGroup:
    """An Avahi entry group that records what it is given and answers with
    the states it is told to."""

    def __init__(self, states):
        self.states = list(states)
        self.services, self.addresses, self.commits, self.resets, self.freed = [], [], 0, 0, False

    def add_service(self, instance, type_, host, port, txt):
        self.services.append((instance, type_, host, port, dict(txt)))

    def add_address(self, host, ip):
        self.addresses.append((host, ip))

    def commit(self):
        self.commits += 1

    def state(self):
        return self.states.pop(0) if len(self.states) > 1 else self.states[0]

    def reset(self):
        self.resets += 1
        self.services, self.addresses = [], []

    def free(self):
        self.freed = True


def test_avahi_advertiser_publishes_the_four_services_and_the_address():
    group = FakeGroup([AVAHI_ESTABLISHED])
    a = AvahiAdvertiser(IDENT, "203.0.113.2", group_factory=lambda: group)
    assert a.start() is group
    assert group.addresses == [("Reference-Source-90000001.local", "203.0.113.2")]
    assert [s[0] for s in group.services] == ["2730.1200322.90000001@products.bang-olufsen.com",
                                              "Reference Source", "Reference Source", "Reference Source"]
    assert [s[1] for s in group.services] == ["_beocore._tcp.local.", "_beoremote._tcp.local.",
                                              "_http._tcp.local.", "_beo_settings._tcp.local."]
    assert all(s[2] == "Reference-Source-90000001.local" for s in group.services)
    assert [s[3] for s in group.services] == [5222, 8080, 80, 80]
    assert group.services[0][4]["jid"] == "2730.1200322.90000001@products.bang-olufsen.com"
    assert group.services[2][4] == {}
    assert group.commits == 1
    a.stop()
    assert group.freed


def test_avahi_advertiser_waits_out_a_collision_then_gives_up(monkeypatch):
    monkeypatch.setattr("beo.networklink.discovery.time.sleep", lambda s: None)
    group = FakeGroup([AVAHI_COLLISION, AVAHI_ESTABLISHED])
    a = AvahiAdvertiser(IDENT, "203.0.113.2", group_factory=lambda: group)
    a.start()
    assert group.resets == 1 and group.commits == 2 and len(group.services) == 4

    stubborn = FakeGroup([AVAHI_COLLISION])
    a = AvahiAdvertiser(IDENT, "203.0.113.2", group_factory=lambda: stubborn)
    with pytest.raises(RuntimeError):
        a.start()
    assert stubborn.resets == AvahiAdvertiser.RETRIES - 1 and stubborn.freed


def test_service_type_drops_the_domain():
    assert service_type("_beocore._tcp.local.") == "_beocore._tcp"
    assert service_type("_beo_settings._tcp.local.") == "_beo_settings._tcp"


def test_advertiser_chooser_prefers_avahi_and_falls_back_to_zeroconf():
    assert isinstance(advertiser(IDENT, "203.0.113.2", avahi_available=lambda: True), AvahiAdvertiser)
    chosen = advertiser(IDENT, "203.0.113.2", avahi_available=lambda: False)
    assert isinstance(chosen, Advertiser) and not isinstance(chosen, AvahiAdvertiser)
    assert chosen.records() == advertiser(IDENT, "203.0.113.2", avahi_available=lambda: True).records()
