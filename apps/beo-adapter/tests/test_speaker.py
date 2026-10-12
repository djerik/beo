"""The product's own output: which mixer control is the level, and what the
card is told when the product's volume moves."""
import subprocess
from types import SimpleNamespace

from beo.mozart import facade
from beo_adapter.speaker import Speaker, card_of, volume_control

# A card with a two-step analogue gain listed before its real level, a
# control that is a volume without being a playback volume, and a capture.
CONTENTS = """Simple mixer control 'ADC',0
  Capabilities: cvolume
  Capture channels: Front Left - Front Right
  Limits: Capture 0 - 104
  Front Left: Capture 24 [23%] [0.00dB]
Simple mixer control 'Analogue',0
  Capabilities: pvolume
  Playback channels: Front Left - Front Right
  Limits: Playback 0 - 1
  Front Left: Playback 1 [100%] [0.00dB]
Simple mixer control 'Analogue Playback Boost',0
  Capabilities: volume
  Limits: 0 - 1
  Front Left: 0 [0%] [0.00dB]
Simple mixer control 'Digital',0
  Capabilities: pvolume pswitch
  Playback channels: Front Left - Front Right
  Limits: Playback 0 - 207
  Front Left: Playback 207 [100%] [0.00dB] [on]
"""


class Mixer:
    """amixer, as far as the speaker uses it."""

    def __init__(self, contents=CONTENTS, fails=False):
        self.contents, self.fails, self.calls = contents, fails, []

    def __call__(self, argv, **_):
        self.calls.append(argv)
        if self.fails:
            raise subprocess.CalledProcessError(1, argv)
        return SimpleNamespace(stdout=self.contents)

    def levels(self):
        return [c[-2:] for c in self.calls if "sset" in c]


def test_a_device_names_its_card_by_id_or_by_number():
    assert card_of("plughw:CARD=Headphones,DEV=0") == "Headphones"
    assert card_of("hw:1,0") == "1"
    assert card_of("hw:CARD=sndrpihifiberry") == "sndrpihifiberry"


def test_the_level_is_the_playback_volume_with_the_widest_range():
    assert volume_control(CONTENTS) == "Digital"
    assert volume_control("Simple mixer control 'ADC',0\n  Capabilities: cvolume\n") == ""


def test_the_products_scale_is_the_cards_full_scale():
    mixer = Mixer()
    s = Speaker("plughw:CARD=Headphones,DEV=0", 72, run=mixer)
    s.volume(36, False)
    s.volume(72, False)
    s.volume(90, False)                       # never past full scale
    assert mixer.calls[0] == ["amixer", "-c", "Headphones", "scontents"]
    assert mixer.calls[1] == ["amixer", "-q", "-M", "-c", "Headphones", "sset", "Digital", "50%"]
    assert mixer.levels() == [["Digital", "50%"], ["Digital", "100%"], ["Digital", "100%"]]


def test_muted_is_no_level_and_the_level_comes_back():
    mixer = Mixer()
    s = Speaker("hw:1,0", 100, "PCM", run=mixer)
    s.volume(40, True)
    s.volume(40, False)
    assert mixer.levels() == [["PCM", "0%"], ["PCM", "40%"]]


def test_a_card_that_cannot_be_set_is_said_once(caplog):
    s = Speaker("hw:1,0", 100, run=Mixer(fails=True))
    s.volume(40, False)
    s.volume(41, False)
    assert len([r for r in caplog.records if "could not be set" in r.message]) == 1


def test_the_product_says_where_its_volume_stands_now_and_at_every_change(monkeypatch):
    before = dict(facade._volume)
    monkeypatch.setattr(facade, "_volume_listener", None)
    told = []
    try:
        facade._volume.update(level=30, muted=False)
        facade.set_volume_listener(lambda level, muted: told.append((level, muted)))
        facade.set_volume(level=34)
        facade.set_volume(level=34)                    # no change, nothing said
        facade.set_volume(muted=True)
    finally:
        facade._volume.update(before)
    assert told == [(30, False), (34, False), (34, True)]
