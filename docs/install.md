# Installing

This is the reference: everything the installer does, and how to check,
update and remove what it installed. For the short path from an empty SD card
to a product in the app, read [getting-started.md](getting-started.md) first.

What you get: a Raspberry Pi that the Bang & Olufsen app sees as a product. If
a Master Link interface is attached, the classic equipment on that bus is what
the product is made of. If none is attached, you get the product on its own,
which is a supported way to run. With a sound card it is then a speaker other
products can play to.

## Before you start

- A Raspberry Pi running Debian. One Pi 3B+ on bullseye is the machine
  everything here was measured on; newer releases are expected to work and the
  installer adapts to what it finds.
- Optional: a MasterDataTool v1, which appears as a USB serial port. Without it
  the product installs and runs with nothing behind it.
- Optional: a sound card. A machine needs one with an input and an output,
  wired to the bus's audio pairs. The MasterDataTool v1 has that card on
  board, and it appears once `dtoverlay=hifiberry-dacplusadcpro,slave` is in
  the Pi's `config.txt`; the installer does not add the line. A speaker of the product's own needs any
  output, and the Pi's own jack will do: `dtparam=audio=on` in the Pi's
  `config.txt` and a restart turn it on. The installer offers to put the line
  there, on a Pi whose board has the jack, and does it only on a yes.
- The phone running the app must be on the same network. The product is found
  over mDNS, so the two have to be able to hear each other's multicast, which
  rules out most guest networks and some mesh setups.

Port 80 has to be free. The app builds its URLs without a port number, so there
is nowhere else to listen.

## Installing

Put the repository on the Pi, as a git clone if it is to be updated later,
then from inside it:

    bash deploy/install.sh

From a development machine, `python deploy/deploy.py` copies the
repository to the Pi named in `BEO_PI`, takes a backup of what is there, and
runs the installer. Run from a terminal, the installer asks its questions
there; run from a script, it takes the defaults. `--copy` copies without
installing, which leaves the service running the code it started with.

It asks four things: which product the app should draw, what is behind it,
what the product should be called in the app, and which sound card, if any,
plays to a speaker directly. All four have defaults, and `--quiet` takes the
defaults without asking. The name suggested is that of the machine chosen, or
of the product when there is none. With a machine the default is no speaker of its own.
With none it is the first card found, a card added to the Pi before the Pi's
own jack, and listening is turned on with it.

With a machine and more than one sound card added to the Pi it also asks
which of them the machine is wired to; with one, that is the one.

Where the Pi's own jack is off it asks one more: whether to turn it on. The
default is no. On a yes it keeps a copy of `config.txt` beside it, adds the
line, and offers the jack as a card to choose straight away; the jack plays
from the next restart of the Pi, which the installer leaves to you.

On a Pi that is already installed it asks about the speaker only, and only
while the configuration has none. On an answer it sets `BEO_SPEAKER_DEVICE`,
turns on listening if it was off, and, if that makes a second sound card,
names the machine's card. It keeps a copy of the configuration first and
prints every line it sets.

What it does, in order:

1. Reports the system, the Pi model and the Python version it found.
2. Installs missing packages with apt, never with pip. Debian bookworm and
   newer refuse a system-wide pip install outright, and the service runs the
   source trees straight off `PYTHONPATH`, so there is nothing to build. The
   one exception is zeroconf: older than 0.148 it announces the product so
   that other products read it as unreachable, so where apt has nothing newer
   a private copy goes into `/opt/beo/python`, which only the service uses.
3. Parses every source file under the Python that will run it, so a syntax
   error from a newer dialect is caught here and not at the first restart.
4. Looks for a USB serial port, and if it finds one, installs the udev rule
   that drops the latency timer to one millisecond. At the default of sixteen
   the USB stack batches bytes and cuts telegrams at arbitrary points.
5. Lists the sound cards by name, and offers to turn on the Pi's own jack
   if it is off.
6. Retires a previous installation if it finds one, under either of the names
   the service has had, carrying its serial and its stored clock offset across.
7. Writes the configuration, if there is not one already.
8. Installs the service, stops anything else holding the product's ports
   (80, 8080 and 9339) and says what it stopped, starts the service, and asks
   the product for its own identity before reporting success.

## The configuration

One file, `/etc/beo/product.env`. The installer writes it once and never
rewrites it, so nothing you change there is lost on an upgrade.
[configuration.md](configuration.md) says what every line does.

## Checking it

    systemctl status beo-adapter
    journalctl -u beo-adapter -f
    curl http://localhost/api/v1/beolink/self

The last one is the quickest answer to "is this thing alive": it returns the
identity the app will see.

The log names who is asking as well as what they asked for, which is usually
the fastest way to tell an app request from another product's.

## Updating

On the Pi, in the clone the service runs from:

    bash deploy/update.sh            the latest
    bash deploy/update.sh v1.2       one release, or any commit
    bash deploy/update.sh --force    even while it plays

It fetches, shows what is new, keeps a copy of the configuration, moves the
clone to the version asked for and runs that version's installer with its
defaults, which restarts the service. The configuration is not changed: a new
setting has a default in the code until you set it.

An update is never taken on its own. It restarts the product, which stops
whatever is playing, so it refuses while the product plays unless told
`--force`.

If the product does not answer after the update, the clone goes back to the
version it came from and that one is started again. A version that starts and
then misbehaves is not caught that way, so the last line of an update says how
to go back: the same command, with the version you came from.

The service says which version it is when it starts, as `version:` in the
log. A copy put on the Pi without its history says `unknown`, and cannot be
updated this way: clone the repository and install from the clone, and the
configuration in `/etc/beo` makes it the same product again.

## Removing it

    bash deploy/install.sh --uninstall

The service is stopped and removed. `/etc/beo` is left alone on purpose, so
reinstalling later gives you the same product rather than a new one.

## Finding other products

There is nothing to configure. The product browses for its neighbours
continuously, the way a real one does, and they appear as they are switched on.
There is no list to maintain and no file to edit. If the app offers nothing to
play to, the question is whether the speakers can be seen on the network, not
whether they are written down somewhere.

## When it does not work

**The app cannot find the product.** Check that port 80 is actually held by
this service and not something else, and that the phone is on the same network
rather than a guest one.

**The app finds it but shows no sources.** The product type has to be one the
app knows. Only one has been verified against a real app, so a different one
may draw nothing at all rather than drawing something wrong.

**It appears, but nothing plays.** With `BEO_MACHINE=none` or `BEO_BACKEND=none`
and no `BEO_SPEAKER_DEVICE` that is expected.
With a bus attached, the log says what was sent and what the machine answered.
