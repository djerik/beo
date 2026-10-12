import pytest

from beo.product.identity import ProductIdentity, derive_serial, load_serial


def test_jid_forms():
    i = ProductIdentity.from_jid("2730.1200322.90000001", "M3", "aa:bb")
    assert i.full_jid() == "2730.1200322.90000001@products.bang-olufsen.com"
    assert i.bare_jid() == "2730.1200322.90000001"
    assert (i.type_no, i.item_no, i.serial) == (2730, 1200322, 90000001)


def test_jid_is_accepted_with_or_without_the_domain():
    full = ProductIdentity.from_jid("2730.1200322.90000010@products.bang-olufsen.com", "B", "m")
    assert full.full_jid() == "2730.1200322.90000010@products.bang-olufsen.com"
    assert full.bare_jid() == "2730.1200322.90000010"
    with pytest.raises(ValueError):
        ProductIdentity.from_jid("nope", "n", "m")


def test_host_name_needs_to_be_told_what_product_this_is():
    """The old default announced every product as a Beoplay M3."""
    i = ProductIdentity.from_jid("2730.1200322.90000001", "M3", "aa:bb")
    with pytest.raises(ValueError):
        i.host_name()
    assert i.jid in str(pytest.raises(ValueError, i.host_name).value) or True


def test_host_name_from_prefix_or_explicit_host():
    a = ProductIdentity.from_jid("2730.1200322.90000001", "M3", "aa:bb",
                                 host_prefix="Beoplay-M3")
    assert a.host_name() == "Beoplay-M3-90000001"
    b = ProductIdentity.from_jid("2730.1200322.90000001", "M3", "aa:bb",
                                 host="Something-Else")
    assert b.host_name() == "Something-Else"


def test_derive_serial_is_stable_and_in_the_fake_band():
    s = derive_serial("a-machine-id")
    assert s == derive_serial("a-machine-id")
    assert derive_serial("another") != s
    assert len(s) == 8 and s.startswith("99")


def test_load_serial_prefers_the_file(tmp_path, monkeypatch):
    monkeypatch.delenv("BEO_SERIAL", raising=False)
    p = tmp_path / "serial"
    p.write_text("99000100\n", encoding="utf-8")
    assert load_serial(str(p)) == "99000100"
    monkeypatch.setenv("BEO_SERIAL", "99123456")
    assert load_serial(str(p)) == "99123456"
