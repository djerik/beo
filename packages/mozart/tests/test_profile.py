"""The Mozart type catalogue and how a profile takes its identity from it."""
import pytest

from beo.mozart.profile import DEFAULT_TYPE, MOZART_TYPES, ProductProfile, mozart_type


def test_default_is_the_verified_beolab_8():
    t = mozart_type(None)
    assert t.key == DEFAULT_TYPE == "beolab-8"
    assert t.type_number == "3150"
    assert t.verified
    assert [k for k, t in MOZART_TYPES.items() if t.verified] == ["beolab-8"]


def test_type_numbers_are_distinct_and_numeric():
    numbers = [t.type_number for t in MOZART_TYPES.values()]
    assert len(set(numbers)) == len(numbers)
    assert all(n.isdigit() for n in numbers)


def test_lookup_forgives_case_and_spacing():
    assert mozart_type("Beosound A5") is MOZART_TYPES["beosound-a5"]
    assert mozart_type("  beolab_28 ") is MOZART_TYPES["beolab-28"]


def test_unknown_key_names_every_valid_one():
    with pytest.raises(ValueError) as e:
        mozart_type("beosound-9000")
    for key in MOZART_TYPES:
        assert key in str(e.value)


def test_profile_takes_identity_from_the_type():
    p = ProductProfile(MOZART_TYPES["beosound-level"])
    assert p.type_number == "6476"
    assert p.product_default_name == "Beosound Level"
    assert p.friendly == "Beosound Level (emulated)"
    assert p.host_prefix == "Beosound-Level"


def test_subclass_keeps_its_own_name_and_host():
    class Mine(ProductProfile):
        friendly = "Mine"
        host_prefix = "Mine-host"

    p = Mine(MOZART_TYPES["beosound-a5"])
    assert (p.type_number, p.friendly, p.host_prefix) == ("2541", "Mine", "Mine-host")


def test_product_type_defaults_to_the_apps_type_name():
    assert ProductProfile().product_type == "SEOUL"
    assert ProductProfile(MOZART_TYPES["beolab-28"]).product_type == "s58"

    class Adapter(ProductProfile):
        product_type = "CA17"

    assert Adapter(MOZART_TYPES["beolab-28"]).product_type == "CA17"
