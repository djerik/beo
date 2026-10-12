# Configuration

Every setting the product has, and what each one does. Nothing here is needed
to get started: the installer writes a working file, and
[getting-started.md](getting-started.md) never opens it.

One file, `/etc/beo/product.env`, read by systemd:

    BEO_SERIAL=99123456
    BEO_NAME=Beosound 9000
    BEO_PRODUCT=beolab-8
    BEO_MACHINE=bs9000
    BEO_BACKEND=masterlink
    BEO_ML_PORT=/dev/ttyUSB0
    BEO_BEOLINK=1
    BEO_FAITHFUL=1
    BEO_LISTEN=off
    BEO_ENROL=0
    BEO_PLAYBACK_DEVICE=hw:0,0
    BEO_CAPTURE_DEVICE=hw:0,0
    BEO_SPEAKER_DEVICE=
    BEO_LOG_LEVEL=INFO

Every setting is written out, with its default where nothing was chosen, so
the file shows what there is to change. Edit it and restart the service. **The
installer never rewrites it**, so nothing you change here is lost on an
upgrade. It checks that the file makes
sense, refuses to continue if the serial is missing rather than inventing a
new one, and changes a line only on a yes to the question about a speaker.

`BEO_SERIAL` is the one line to leave alone. The identity it forms is the app's
primary key for this product: change it and the app sees an entirely new
product, forgets the old one along with any group it was part of, and other
speakers keep a stale identity they can no longer reach. It is derived from the
machine itself at install, so reinstalling on the same hardware gives the same
product back.

`BEO_PRODUCT` is which Mozart product the app draws. The app can only render
the product types compiled into it, and only `beolab-8` has been verified
against it; the installer lists the others, which are there to be tried. The
log says what the app asks a given product for that it does not answer. The
type number is the first field of the jid, so changing the product changes the
jid: the app sees a new product and forgets the old one, exactly as with the
serial. Change it, restart the service, and set the product up again in the app.
[product-matrix.md](product-matrix.md) is the list of types still to be tried.

`BEO_MACHINE` is the classic product behind this one: `bs9000`, or `none`. It
decides both how the bus is spoken to and what the app draws. With a 9000 the
app sees a product with a Classics Adapter and six discs. With `none` it sees
a plain speaker that answers everything and has nothing to play. `bs3000`,
`bs3200`, `bs4` and `bc2` are there to be tried and have not been run against
a machine: each is taken to have one disc and a radio, the app draws the same
six discs, and whichever is picked the one disc plays. A
configuration written before this setting existed has no such line, and a bus
is then taken to have a BeoSound 9000 on it.

`BEO_BACKEND` is `masterlink` when the machine is on the bus, and `none` when
no bus is attached. `BEO_MACHINE=bs9000` with `BEO_BACKEND=none` shows the
9000's product in the app with nothing on the other side, which is how the app
side is tried without the machine.

`BEO_BEOLINK` turns on streaming this product's sound to other speakers, which
needs a capture device. Listening is built on it too, so it is also on for a
product that only listens; expanding from that one finds nothing to capture.

`BEO_FAITHFUL` shows only what the machine itself reports, a disc and a track
number, instead of filling in titles and artists it cannot know.

The rest are written as off. They need `BEO_BEOLINK`.

`BEO_LISTEN=on` lets other products play to this one. The product then also
listens on ports 5222 and 1234 and announces itself to the other products.
With a machine behind it the stream is played into the machine, which is
offered it as N.MUSIC. With `BEO_SPEAKER_DEVICE` it is played on that card.
With neither it is received and not played. A list of addresses instead of
`on` also holds a session open to each of them.

`BEO_ENROL=1` lets the product add itself to the configuration the products
on the network share. A source plays only to products it finds there, so
without an entry nothing will offer this product as a listener. It is a
setting of its own because it changes a document every product holds: it
adds this product's entry if there is none, and never edits or removes
anything.

`BEO_PLAYBACK_DEVICE` is the sound card output that feeds the machine, and
`BEO_CAPTURE_DEVICE` the input its sound is captured from. `hw:0,0` is the
first card to come up. With more than one card that is not the same card on
every start, so name it: `hw:CARD=<name>,DEV=0`, with
the name `aplay -l` prints after the card's number. Turning on the Pi's own
jack makes a second card, so name the machine's before you do.

`BEO_SPEAKER_DEVICE` is a sound card that plays to a speaker directly, such as
`plughw:CARD=Headphones,DEV=0` for the Pi's own jack. Empty means none. What
other products play to this one comes out there. It needs `BEO_LISTEN`, and it does not need a
machine: with one, both play, each block at the same time on the network's
clock. The two cards do not have the same delay, so in one room the pair can
sound coloured; in two rooms it cannot be heard.

The product's volume and mute are set on that card's mixer, with the top of
the app's scale as the card's full level. The control is the card's playback
volume with the widest range; `BEO_SPEAKER_MIXER` names another. A machine is
not affected: it has a volume of its own.

Three settings are not in the file, because nobody should need them. Add a
line to use one. `BEO_PTP=0` stops the product following the other products'
clock and makes it use a stored offset alone, read from the file
`BEO_PTP_OFFSET` names (`/etc/beo/ptp_offset.ns`). `BEO_LOG` is where the
service keeps a copy of its log, beside the journal.

The service keeps two files of its own in `/etc/beo`: `version`, written by
the installer, and `volume-default`, the default volume set in the app.
