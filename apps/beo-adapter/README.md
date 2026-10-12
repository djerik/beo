# beo-adapter

A Raspberry Pi as a Bang & Olufsen product.

This is the application the installer starts. The protocols live in the
[`beo` packages](../../packages); this chooses which of them to wire together,
from the configuration, and holds the little that is specific to one machine.

## What it can be

Two settings in `/etc/beo/product.env` decide it, and they are independent.

`BEO_MACHINE` is what sits behind the product:

| Value | What the app sees | Status |
|---|---|---|
| `bs9000` | a product with a Classics Adapter: six discs, transport, volume, multiroom | verified on the machine |
| `bs3000`, `bs3200`, `bs4`, `bc2` | the same product and the same six discs, in front of a machine with one | to be tried; nothing about them has been measured |
| `none` | a plain speaker with one input and nothing to browse | runs; has nothing to play |

`BEO_PRODUCT` is which Mozart product the app draws around it, one of the types
in `beo.mozart.profile.MOZART_TYPES`. Only `beolab-8` is verified.

The machine decides both how the bus is spoken to and what the app is shown.
They are chosen together because choosing them apart lets the app draw six
discs in front of a machine that has none.

The app has one disc player to draw, and it has six discs. A machine with one
disc is shown with all six so that its playback can be tried at all: whichever
is picked, the one disc plays, and the app then shows the first.

## What it is made of

| Module | What it does |
|---|---|
| `main.py` | reads the configuration, picks the machine, hands everything to the facade |
| `machines.py` | the machines this adapter can drive, and the plain speaker for when there is none |
| `ml_bridge.py` | a music system on the bus: the machine's status in, the app's commands out. Which machine it is comes in as a profile from `beo.masterlink.profiles` |
| `classics_adapter_profile.py` | the product the app sees in front of a machine, whichever it is: the Classics Adapter item number, six discs |
| `streaming.py` | this product plays to other products: the source role behind the app's expand, the capture of the machine's sound, and the clock the stream is stamped with |
| `listening.py` | another product plays to this one: the listener role, the port other products open, the enrolment, and the sound card outputs: into the machine, and the product's own |
| `speaker.py` | a sound card that plays to a speaker directly, and the mixer control that carries the product's volume to it |

With a 9000, state flows one way. The machine's own status telegram is the
only thing that changes what the app is told, so the app never shows a disc the
machine is not actually playing. Commands go the other way as virtual
remote-control presses.

Listening is off unless `BEO_LISTEN` is set, and the product adds itself to
the network's shared configuration only when `BEO_ENROL` is set;
[docs/configuration.md](../../docs/configuration.md) says what each does. When a stream
arrives the machine is asked to play a network source first, and the sound
card is opened only once it has agreed. A card set in `BEO_SPEAKER_DEVICE`
plays the stream without asking anybody, with a machine or without.

## Adding a machine

A machine is two descriptions under one name in `machines.py`. The first is
a `MusicSystemProfile` in `beo.masterlink.profiles`: its sources, and how one
of its discs is selected if it has several. The bridge is the same for every
machine and is handed that profile. The second subclasses
`beo.mozart.profile.ProductProfile` and says what the app is shown. Mark a
profile `verified` only once it has been run against the real machine.

The facade still knows two sources by name, the discs and the radio. A machine
whose first source has no discs reaches the bus through the bridge, and its
state does not reach the app until the facade takes its sources from the
machine too.

## Running it by hand

From the repository root, with the packages on the path:

    python -m beo_adapter.main --machine none
    python -m beo_adapter.main --machine bs9000 --ml none
    python -m beo_adapter.main --machine bs9000 --ml /dev/ttyUSB0 --beolink --faithful

The second form shows the 9000's product in the app with no bus attached,
which is how the app side is tried without the machine. Port 80 has to be
free, because the app builds its URLs without a port number.

## Tests

They run with the rest of the repository:

    python -m pytest

No hardware is needed. The bus is faked and the product answers in process.

`tests/test_mozart_surface.py` guards what the app sees: a recorded answer per
route, and named tests that each describe one bug that happened against the
real app. When a recorded answer changes, read the named failures first; they
say what breaks rather than only that something moved. Re-record deliberately,
and read the diff:

    python apps/beo-adapter/tests/capture_golden.py

## One rule about the hardware

Nothing here sends anything on the bus unprompted. A classic machine is woken
by its owner or by an explicit action in the app, never by a service starting.
