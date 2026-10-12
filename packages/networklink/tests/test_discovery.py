from beo.networklink.discovery import Advertiser, CAPS_VER
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
