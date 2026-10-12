"""The machine decides both the bus side and what the app is shown."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest                                                        # noqa: E402

from beo.mozart.profile import ProductProfile, mozart_type           # noqa: E402
from beo_adapter import main as adapter_main                         # noqa: E402
from beo.networklink.sources import Catalog, STANDARD_SOURCES        # noqa: E402
from beo.product.identity import ProductIdentity                     # noqa: E402
from beo_adapter.machines import (MACHINES, NoMachine, machine_named,  # noqa: E402
                                  profile_for, sources_for)

JID = "3150.1454530.99000100@products.bang-olufsen.com"


def discs(profile):
    return [uri for uri, _label, _kind in profile.source_entries(JID)
            if uri.startswith("classicsAdapter://")]


def test_no_machine_is_a_plain_speaker():
    """Nothing behind the product means nothing to browse.

    A product with no machine that still offers six discs shows the app a disc
    player whose every disc does nothing.
    """
    assert machine_named("none") is None
    profile = profile_for(None, mozart_type("beolab-8"))
    assert type(profile) is ProductProfile
    assert discs(profile) == []
    assert profile.content_items() == {}
    assert "/api/v1/classics-adapter" not in profile.extra_routes("00:00:5E:00:53:01")


def test_bs9000_brings_its_own_product():
    machine = machine_named("bs9000")
    assert machine is MACHINES["bs9000"]
    profile = profile_for(machine, mozart_type("beolab-8"))
    assert len(discs(profile)) == 6
    assert "/api/v1/classics-adapter" in profile.extra_routes("00:00:5E:00:53:01")


def test_bs9000_is_spoken_to_through_the_bridge_every_machine_shares():
    from beo.masterlink.bus import MlTransport
    from beo.masterlink.profiles import BS9000
    from beo_adapter.ml_bridge import MlBridge
    bridge = machine_named("bs9000").bridge("/dev/null", transport=MlTransport(), source_center=False)
    assert type(bridge) is MlBridge and bridge.profile is BS9000


def test_every_machine_is_a_profile_and_only_the_9000_is_verified():
    from beo.masterlink.profiles import PROFILES
    assert set(MACHINES) == set(PROFILES)
    for key, machine in MACHINES.items():
        assert machine.verified == PROFILES[key].verified
        assert len(discs(profile_for(machine, mozart_type("beolab-8")))) == 6
    assert [k for k, m in MACHINES.items() if m.verified] == ["bs9000"]
    assert [k for k, p in PROFILES.items() if p.discs is not None] == ["bs9000"]


def test_the_machine_decides_what_other_products_are_offered():
    """A listener is shown the catalogue it is attached with, so a 9000 that
    lists streaming services offers what it cannot play."""
    assert [(s.key, s.source_type, s.category) for s in sources_for(machine_named("bs9000"))] == [
        ("CD", "CD", "MUSIC"), ("RADIO", "RADIO", "RADIO")]
    xml = Catalog(sources_for(machine_named("bs9000"))).contact_info_xml(
        ProductIdentity.from_jid(JID, "Synthetic 9000", "00:00:5E:00:53:01"))
    assert xml.count("<uniqueSourceId>") == 2
    assert "<identifier>RADIO</identifier>" in xml and "<identifier>CD</identifier>" in xml
    assert sources_for(None) is STANDARD_SOURCES


def test_the_sources_are_said_once_by_the_music_system(monkeypatch):
    """The catalogue and the app's menu are read from the music system's
    sources, so a machine without a radio offers none in either."""
    from dataclasses import replace
    from beo.masterlink import profiles
    from beo_adapter.classics_adapter_profile import ClassicsAdapterProfile
    machine = machine_named("bs4")
    radio_id = ClassicsAdapterProfile.PLAIN_SOURCES["RADIO"][0]

    def menu():
        return profile_for(machine, mozart_type("beolab-8")).remote_items({})
    assert [s.key for s in machine.sources] == [s.key for s in profiles.BS4.sources]
    assert menu()[ClassicsAdapterProfile.REMOTE_SOURCE_ID]["nextSiblingMenuItemId"] == radio_id

    cd_only = replace(profiles.BS4, sources=profiles.BS4.sources[:1])
    monkeypatch.setitem(profiles.PROFILES, "bs4", cd_only)
    assert [s.key for s in machine.sources] == ["CD"]
    assert radio_id not in menu()
    assert "nextSiblingMenuItemId" not in menu()[ClassicsAdapterProfile.REMOTE_SOURCE_ID]
    assert profile_for(machine, mozart_type("beolab-8")).plain_sources == ()


def test_the_machine_keeps_the_chosen_mozart_type():
    """BEO_PRODUCT and BEO_MACHINE are independent: one is what the app draws
    around the product, the other what the product contains."""
    profile = profile_for(machine_named("bs9000"), mozart_type("beolab-28"))
    assert profile.type_number == mozart_type("beolab-28").type_number
    assert len(discs(profile)) == 6


def test_unknown_machine_is_refused_by_name():
    with pytest.raises(ValueError) as e:
        machine_named("bs4000")
    assert "bs9000" in str(e.value)


@pytest.mark.parametrize("env, machine, port", [
    # a configuration from before BEO_MACHINE existed: a bus means a 9000
    ({"BEO_BACKEND": "masterlink", "BEO_ML_PORT": "/dev/ttyUSB1"}, "bs9000", "/dev/ttyUSB1"),
    ({"BEO_BACKEND": "none"}, "none", "none"),
    ({}, "bs9000", "/dev/ttyUSB0"),
    # the machine's product with no bus attached, for trying the app side alone
    ({"BEO_BACKEND": "none", "BEO_MACHINE": "bs9000"}, "bs9000", "none"),
    ({"BEO_BACKEND": "masterlink", "BEO_MACHINE": "none"}, "none", "/dev/ttyUSB0"),
])
def test_defaults_come_from_the_configuration(monkeypatch, env, machine, port):
    for key in ("BEO_BACKEND", "BEO_MACHINE", "BEO_ML_PORT"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert adapter_main._default_machine() == machine
    assert adapter_main._default_ml_port() == port


def test_log_level_reads_the_name_the_installer_writes(monkeypatch):
    monkeypatch.setenv("BEO_LOG_LEVEL", "DEBUG")
    assert adapter_main._default_log_level() == "DEBUG"


def test_no_machine_takes_every_command_and_plays_nothing():
    backend = NoMachine()
    backend.press("play")
    backend.select_disc(3)
    backend.goto_track(2)
    assert backend.request_audio() is False


def test_the_capture_card_comes_from_the_configuration_unless_said_by_hand(monkeypatch):
    for name in ("BEO_NAME", "BEO_BEOLINK", "BEO_FAITHFUL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BEO_CAPTURE_DEVICE", "hw:CARD=adc,DEV=0")
    assert adapter_main._settings_from_environment([]) == ["--beolink-device", "hw:CARD=adc,DEV=0"]
    by_hand = ["--beolink-device", "hw:9,9"]
    assert adapter_main._settings_from_environment(by_hand) == by_hand


def test_the_version_is_what_the_installer_wrote_down(monkeypatch, tmp_path):
    written = tmp_path / "version"
    monkeypatch.setenv("BEO_VERSION_FILE", str(written))
    assert adapter_main._version() == "unknown"
    written.write_text("v1.2-3-gabc1234\n", encoding="utf-8")
    assert adapter_main._version() == "v1.2-3-gabc1234"
