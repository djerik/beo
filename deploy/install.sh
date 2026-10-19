#!/bin/bash
# Install a product the Bang & Olufsen app accepts on this machine. Run it ON the Pi.
#
#     bash deploy/install.sh              ask, then install
#     bash deploy/install.sh --quiet      take the defaults, ask nothing
#     bash deploy/install.sh --uninstall  stop and remove the service
#
# What it will never do: change an identity that already exists, change a
# line of a configuration you have edited without asking, or send anything on
# the MasterLink bus. A BeoSound 9000 is switched on by its owner, not by an
# installer.
set -eu

SRC="$(cd "$(dirname "$0")/.." && pwd)"         # the repository, whatever it was cloned as
CONF_DIR=/etc/beo
CONF="$CONF_DIR/product.env"
UNIT=/etc/systemd/system/beo-adapter.service
QUIET=0

for arg in "$@"; do
  case "$arg" in
    --quiet) QUIET=1 ;;
    --uninstall)
      echo "== removing the service"
      sudo systemctl disable --now beo-adapter 2>/dev/null || true
      sudo rm -f "$UNIT"
      sudo systemctl daemon-reload
      echo "   the configuration in $CONF_DIR is left alone, so a reinstall keeps this product's identity"
      exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

ask() {   # ask <prompt> <default> -> answer on stdout
  local prompt="$1" default="$2" reply=""
  if [ "$QUIET" = "1" ] || [ ! -t 0 ]; then echo "$default"; return; fi
  read -r -p "$prompt [$default]: " reply </dev/tty || reply=""
  echo "${reply:-$default}"
}

echo "== what this machine is"
. /etc/os-release 2>/dev/null || true
echo "   ${PRETTY_NAME:-unknown system}, python $(python3 -V 2>&1 | cut -d' ' -f2)"
if [ -r /proc/device-tree/model ]; then
  echo "   $(tr -d '\0' < /proc/device-tree/model)"
fi

# Packages come from apt, never from a system-wide pip. Debian bookworm and
# newer refuse that outright (PEP 668), and the service runs the source trees
# straight off PYTHONPATH, so there is nothing to build or install.
echo "== packages"
missing=""
python3 -c "import serial" 2>/dev/null || missing="$missing python3-serial"
python3 -c "import zeroconf" 2>/dev/null || missing="$missing python3-zeroconf"
# Other products are told of this one through the Avahi daemon, which
# writes the product's jid as the one name label they ask for.
python3 -c "import dbus" 2>/dev/null || missing="$missing python3-dbus"
# The product's page sends its sound to a browser through ffmpeg.
command -v ffmpeg >/dev/null 2>&1 || missing="$missing ffmpeg"
if [ -n "$missing" ]; then
  echo "   installing:$missing"
  sudo apt-get install -y -q $missing
else
  echo "   all present"
fi

# One exception, kept out of the system's own packages. Other products find
# this one by its announcement on the network, and a zeroconf older than
# 0.148 writes that announcement so that they read it as "cannot be reached".
# Where apt has nothing newer, a private copy goes into a directory only the
# service's PYTHONPATH names.
# Asked as root, because that is who the service runs as: whoever installs
# may have a newer copy of their own that the service never sees.
PRIVATE=/opt/beo/python
if ! sudo env PYTHONPATH=$PRIVATE python3 -c "
import sys, zeroconf
sys.exit(0 if tuple(int(x) for x in zeroconf.__version__.split('.')[:2]) >= (0, 148) else 1)" 2>/dev/null; then
  echo "   zeroconf here is too old to announce the product to other products;"
  echo "   putting a newer one in $PRIVATE"
  sudo mkdir -p "$PRIVATE"
  # A system written straight from the image need not have pip.
  python3 -m pip --version >/dev/null 2>&1 || sudo apt-get install -y -q python3-pip || true
  sudo python3 -m pip install -q --upgrade --target "$PRIVATE" "zeroconf>=0.148,<1"     || echo "   could not fetch it: the product runs, but no source will offer it as a listener"
fi

echo "== the code compiles under this python"
python3 - "$SRC" <<'PY'
import ast, pathlib, sys
root, bad = pathlib.Path(sys.argv[1]), 0
trees = sorted(root.glob("packages/*/beo")) + sorted(
    d for d in root.glob("apps/*/*") if (d / "__init__.py").is_file())
for tree in trees:
    for f in tree.rglob("*.py"):
        try:
            ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
        except SyntaxError as e:
            print("   SYNTAX %s: %s" % (f, e)); bad += 1
print("   %d source trees checked" % len(trees))
sys.exit(1 if bad else 0)
PY

BOOTCFG=/boot/firmware/config.txt
[ -f "$BOOTCFG" ] || BOOTCFG=/boot/config.txt

echo "== MasterLink interface"
# Only USB serial ports are looked for. The Pi's own UART is there on every
# Pi, wired to something or not, so its being there says nothing: a
# MasterDataTool v2 on it is asked about, never assumed.
ML_INTERFACE=mdt1
ML_PORTS="$(ls /dev/ttyUSB* /dev/ttyACM* 2>/dev/null | tr '\n' ' ')"
ML_PORT="${ML_PORTS%% *}"
if [ -n "$ML_PORT" ]; then
  echo "   found a USB serial port at $ML_PORT"
  [ "$ML_PORT " = "$ML_PORTS" ] || echo "   (and more: $ML_PORTS; the first is taken unless named below)"
  sudo install -m 644 "$SRC/deploy/99-ftdi-latency.rules" /etc/udev/rules.d/ 2>/dev/null || true
  sudo udevadm control --reload-rules 2>/dev/null || true
  # At the default 16 ms the USB stack batches bytes and cuts telegrams at
  # arbitrary points. At 1 ms they arrive whole.
  LATENCY="/sys/bus/usb-serial/devices/${ML_PORT#/dev/}/latency_timer"
  if [ -e "$LATENCY" ]; then
    echo 1 | sudo tee "$LATENCY" >/dev/null
    echo "   latency timer set to 1 ms"
  fi
  # The interface with a WCH chip holds the bus's data lines up only as the
  # bus master, and it is master only while GPIO25 is driven low; a pull-up on
  # the board leaves it slave. As slave it hears nothing on a bus no other
  # product holds up. Set in the boot configuration, the pin is low from power
  # on; set here as well, it is low without a restart.
  if udevadm info -q property -n "$ML_PORT" 2>/dev/null | grep -q '^ID_VENDOR_ID=1a86$'; then
    if [ -f "$BOOTCFG" ] && grep -q '^gpio=25=op,dl' "$BOOTCFG"; then
      echo "   the interface is MasterLink master (GPIO25 low in $BOOTCFG)"
    elif [ -f "$BOOTCFG" ]; then
      echo "   the interface (a WCH chip) is MasterLink master only with GPIO25 held low,"
      echo "   which is one line in $BOOTCFG"
      if [ "$(ask '   make it master (yes / no)' yes)" = "yes" ]; then
        sudo cp -n "$BOOTCFG" "$BOOTCFG.before-ml-master"
        printf '\n[all]\ngpio=25=op,dl\n' | sudo tee -a "$BOOTCFG" >/dev/null
        sudo pinctrl set 25 op dl 2>/dev/null || sudo raspi-gpio set 25 op dl 2>/dev/null || true
        echo "   master from now on; the file as it was is $BOOTCFG.before-ml-master"
      fi
    fi
  fi
elif [ -e /dev/serial0 ]; then
  # The v2 has no USB lead: its controller is on the Pi's UART, which the
  # start-up file turns on. The v2 has not been run against a machine.
  echo "   no USB serial port, but the Pi's own UART is on at /dev/serial0"
  if [ "$(ask '   is a MasterDataTool v2 on it (yes / no)' no)" = "yes" ]; then
    ML_INTERFACE=mdt2
    ML_PORT=/dev/serial0
    echo "   taken as a MasterDataTool v2, which has not been tried against a machine"
  else
    echo "   then the product is installed with no bus behind it unless one is named"
    echo "   below, which is a supported way to run: the app sees the product, there"
    echo "   is simply nothing on the other side of it yet."
  fi
else
  echo "   none found. Unless one is named below, the product is installed with"
  echo "   no bus behind it, which is a supported way to run: the app sees the"
  echo "   product, there is simply nothing on the other side of it yet."
fi

choose_ml_port() {   # -> ML_PORT: the bus's serial port, or nothing for no bus
  local p
  p="$(ask "   the serial port the MasterLink interface is on (a path, or none)" "${ML_PORT:-none}")"
  while [ "$p" != "none" ] && [ ! -e "$p" ]; do
    echo "   there is nothing at $p"
    p="$(ask "   the serial port the MasterLink interface is on (a path, or none)" "none")"
  done
  if [ "$p" = "none" ]; then ML_PORT=""; else ML_PORT="$p"; fi
}

# Cards are named and never numbered: with more than one, the order they come
# up in is not the same on every start. The sound of a display is left out:
# it is neither wired to a machine nor a speaker, and its card has a name of
# its own on every system.
echo "== sound cards"
CARDS="$(aplay -l 2>/dev/null | grep -vi hdmi | sed -n 's/^card [0-9]*: \([^ ]*\) .*/\1/p' | sort -u | tr '\n' ' ')"
BOARD=""      # the first card that was added to the Pi
OWN=""        # the Pi's own analogue output
for c in $CARDS; do
  case "$c" in
    Headphones|ALSA) OWN="$c" ;;
    *) [ -n "$BOARD" ] || BOARD="$c" ;;
  esac
done
if [ -n "$CARDS" ]; then echo "   found: $CARDS"; else echo "   none found"; fi

# The Pi's own jack is a line in the boot configuration, and it comes up as a
# card only at the next start. It is offered where the board has one, turned
# on only on a yes, and can be chosen below before the Pi has been restarted.
JACK_TURNED_ON=0     # by this run, so on the owner's yes
JACK_PENDING=0       # on in the boot configuration, not yet a card
MODEL="$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || true)"
case "$MODEL" in
  *Zero*|*"Compute Module"*|*"Pi 400"*|*"Pi 500"*|"Raspberry Pi 5"*) HAS_JACK=0 ;;
  "Raspberry Pi"*) HAS_JACK=1 ;;
  *) HAS_JACK=0 ;;
esac
if [ -z "$OWN" ] && [ "$HAS_JACK" = "1" ] && [ -f "$BOOTCFG" ]; then
  if grep -q '^dtparam=audio=on' "$BOOTCFG"; then
    JACK_PENDING=1
  else
    echo "   the Pi's own jack is off. Turning it on is one line in $BOOTCFG"
    echo "   and a restart of the Pi."
    if [ "$(ask '   turn the jack on (yes / no)' no)" = "yes" ]; then
      sudo cp -n "$BOOTCFG" "$BOOTCFG.before-audio"
      # Added at the end, where it wins over a line that turns it off, and
      # under [all], because a line at the end of the file otherwise belongs
      # to whichever section happens to be the last one.
      printf '\n[all]\ndtparam=audio=on\n' | sudo tee -a "$BOOTCFG" >/dev/null
      echo "   turned on; the file as it was is $BOOTCFG.before-audio"
      JACK_TURNED_ON=1
      JACK_PENDING=1
    fi
  fi
  if [ "$JACK_PENDING" = "1" ]; then
    OWN=Headphones
    CARDS="$CARDS$OWN "
    echo "   the jack becomes the card $OWN when the Pi is restarted"
  fi
fi

free_cards() {   # free_cards <the machine's card, or nothing> -> FREE
  FREE="$(for c in $CARDS; do [ "$c" = "$1" ] || printf '%s ' "$c"; done)"
}

choose_machine_card() {   # -> WIRED: the card the machine is wired to, or nothing
  # The Pi's own output has no input, so it is never the machine's. With one
  # added card there is nothing to ask; with more, only the owner knows.
  local added
  added="$(for c in $CARDS; do [ "$c" = "$OWN" ] || printf '%s ' "$c"; done)"
  WIRED="$BOARD"
  [ "$(echo $added | wc -w)" -gt 1 ] || return 0
  WIRED="$(ask "   the sound card the machine is wired to (${added% })" "$BOARD")"
  while ! printf ' %s ' "$added" | grep -q " $WIRED "; do
    echo "   not a sound card that was added to this Pi: $WIRED"
    WIRED="$(ask "   the sound card the machine is wired to (${added% })" "$BOARD")"
  done
}

choose_speaker() {   # choose_speaker <default> -> SPEAKER, one of FREE or none
  SPEAKER="$1"
  [ -n "$FREE" ] || return 0
  SPEAKER="$(ask "   a sound card that plays to a speaker directly (${FREE}/ none)" "$SPEAKER")"
  while [ "$SPEAKER" != "none" ] && ! printf ' %s ' "$FREE" | grep -q " $SPEAKER "; do
    echo "   not a sound card that is free here: $SPEAKER"
    SPEAKER="$(ask "   a sound card that plays to a speaker directly (${FREE}/ none)" "none")"
  done
}

# Earlier installations ran under other service names and owned the same
# ports. Two cannot run side by side, and an old one would win the race on
# reboot, so they are retired here and what they held is carried across.
for old in bs9000-adapter beo-product; do
  if [ -f "/etc/systemd/system/$old.service" ]; then
    echo "== retiring the previous installation ($old)"
    sudo systemctl disable --now "$old" 2>/dev/null || true
    sudo rm -f "/etc/systemd/system/$old.service"
    sudo systemctl daemon-reload
  fi
done
OLD_CONF_DIR=/etc/bs9000-adapter
if [ -d "$OLD_CONF_DIR" ]; then
  sudo mkdir -p "$CONF_DIR"
  if [ -f "$OLD_CONF_DIR/serial" ] && [ ! -f "$CONF" ]; then
    sudo cp "$OLD_CONF_DIR/serial" "$CONF_DIR/serial.carried-over"
    echo "   the previous installation's serial has been kept, and will be reused below"
  fi
  if [ -f "$OLD_CONF_DIR/ptp_offset.ns" ] && [ ! -f "$CONF_DIR/ptp_offset.ns" ]; then
    sudo cp "$OLD_CONF_DIR/ptp_offset.ns" "$CONF_DIR/ptp_offset.ns"
    echo "   the previous installation's stored clock offset has been kept"
  fi
  sudo rm -f "$OLD_CONF_DIR/peers.json"
fi

echo "== configuration"
# Importing beo.mozart pulls in the facade and its sibling packages, so the
# whole tree goes on the path, exactly as the service unit has it.
CODE="$SRC/packages/product:$SRC/packages/masterlink:$SRC/packages/networklink:$SRC/packages/mozart:$SRC/apps/beo-adapter"
PRODUCTS="$(PYTHONPATH="$CODE" python3 -c 'from beo.mozart.profile import MOZART_TYPES; print(" ".join(MOZART_TYPES))')"
MACHINES="$(PYTHONPATH="$CODE" python3 -c 'from beo_adapter.machines import MACHINES; print(" ".join(MACHINES))')"
MACHINES_VERIFIED="$(PYTHONPATH="$CODE" python3 -c 'from beo_adapter.machines import MACHINES; print(" ".join(k for k, m in MACHINES.items() if m.verified))')"
if [ -f "$CONF" ]; then
  # An existing identity is never touched. The jid is the app's primary key for
  # this product: mint a new one and the app sees a different product, the old
  # one becomes an unreachable ghost, and listeners keep a stale address.
  echo "   $CONF exists, keeping it"
  conf_get() {   # the value the service will see: the last line wins
    sed -n "s/^$1=//p" "$CONF" | tail -1
  }
  CONF_COPIED=0
  conf_set() {   # conf_set <name> <value>: one line, changed or added
    if [ "$CONF_COPIED" = "0" ]; then
      sudo cp "$CONF" "$CONF.before-install"
      CONF_COPIED=1
      echo "   the file as it was is $CONF.before-install"
    fi
    if grep -q "^$1=" "$CONF"; then
      sudo sed -i "s|^$1=.*|$1=$2|" "$CONF"
    else
      echo "$1=$2" | sudo tee -a "$CONF" >/dev/null
    fi
    echo "   $1=$2"
  }
  SERIAL="$(sed -n 's/^BEO_SERIAL=//p' "$CONF" | head -1)"
  if [ -z "$SERIAL" ]; then
    echo "   ERROR: $CONF has no BEO_SERIAL." >&2
    echo "   Refusing to invent one: that would publish a different product." >&2
    echo "   Put the old serial back, or delete the file to start over." >&2
    exit 1
  fi
  echo "   serial $SERIAL"
  if ! grep -q '^BEO_PRODUCT=' "$CONF"; then
    echo "   no BEO_PRODUCT: the app sees a Beolab 8. To change the product, add"
    echo "   BEO_PRODUCT=<name> to $CONF and restart; names: $PRODUCTS"
    echo "   (the type number is part of the jid, so the app will see a new product)"
  fi
  if ! grep -q '^BEO_MACHINE=' "$CONF"; then
    echo "   no BEO_MACHINE: with a bus attached, the machine behind it is taken"
    echo "   to be a BeoSound 9000. Machines: $MACHINES none"
  fi

  # A speaker of the product's own is the one thing asked of a configuration
  # that exists, and only while it has none: the default is to leave it so,
  # and nothing is written without an answer.
  MACHINE="$(conf_get BEO_MACHINE)"
  if [ -z "$MACHINE" ]; then
    case "$(conf_get BEO_BACKEND)" in none|no|off) MACHINE=none ;; *) MACHINE=bs9000 ;; esac
  fi
  # A bus port that is not there stops the service from starting at all, so
  # it is asked about again rather than left for the log to say.
  case "$(conf_get BEO_BACKEND)" in none|no|off) ;; *)
    if [ "$MACHINE" != "none" ]; then
      CONF_PORT="$(conf_get BEO_ML_PORT)"
      CONF_PORT="${CONF_PORT:-/dev/ttyUSB0}"
      if [ "$CONF_PORT" != "none" ] && [ ! -e "$CONF_PORT" ]; then
        echo "   BEO_ML_PORT is $CONF_PORT, and there is nothing there"
        choose_ml_port
        if [ -n "$ML_PORT" ]; then
          conf_set BEO_ML_PORT "$ML_PORT"
        else
          conf_set BEO_BACKEND none
          echo "   no bus: the app shows the machine, and nothing reaches it"
        fi
      fi
    fi ;;
  esac
  MACHINE_CARD=""
  if [ "$MACHINE" != "none" ]; then
    MACHINE_CARD="$(conf_get BEO_PLAYBACK_DEVICE | sed -n 's/.*CARD=\([^,]*\).*/\1/p')"
    WIRED="$MACHINE_CARD"
    [ -n "$WIRED" ] || choose_machine_card
    free_cards "$WIRED"
  else
    free_cards ""
  fi
  SPEAKER=none
  if [ -n "$FREE" ] && [ -z "$(conf_get BEO_SPEAKER_DEVICE)" ]; then
    echo "   no BEO_SPEAKER_DEVICE: what other products play to this one does not"
    echo "   come out on a speaker of its own"
    choose_speaker none
    if [ "$SPEAKER" != "none" ]; then
      conf_set BEO_SPEAKER_DEVICE "plughw:CARD=$SPEAKER,DEV=0"
      # A speaker with nobody playing to it is silent, so listening goes on
      # with it, and listening is built on the streaming side.
      case "$(conf_get BEO_BEOLINK)" in 1|true|yes|on) ;; *) conf_set BEO_BEOLINK 1 ;; esac
      case "$(conf_get BEO_LISTEN)" in ""|0|no|off|false|none) conf_set BEO_LISTEN on ;; esac
    fi
  fi
  if [ "$MACHINE" != "none" ] && [ -z "$MACHINE_CARD" ] && [ "$(echo $CARDS | wc -w)" -gt 1 ]; then
    echo "   more than one sound card, and the machine's is not named: hw:0,0 is"
    echo "   whichever comes up first"
    if [ -n "$WIRED" ] && { [ "$SPEAKER" != "none" ] || [ "$JACK_TURNED_ON" = "1" ]; }; then
      # What was just agreed to is what made the second card, and without
      # this the machine would lose its own at the next start.
      conf_set BEO_PLAYBACK_DEVICE "hw:CARD=$WIRED,DEV=0"
      conf_set BEO_CAPTURE_DEVICE "hw:CARD=$WIRED,DEV=0"
    else
      echo "   Name it in BEO_PLAYBACK_DEVICE and BEO_CAPTURE_DEVICE, as hw:CARD=<card>,DEV=0"
    fi
  fi
else
  sudo mkdir -p "$CONF_DIR"
  # Derived from the machine, never random, so a reinstall on the same hardware
  # produces the same product. 99xxxxxx sits above B&O's real range.
  if [ -f "$CONF_DIR/serial.carried-over" ]; then
    SERIAL="$(cat "$CONF_DIR/serial.carried-over")"
    echo "   reusing the serial from the previous installation: $SERIAL"
  else
  SERIAL="$(python3 -c "
import hashlib, uuid
seed = ''
for p in ('/etc/machine-id', '/var/lib/dbus/machine-id'):
    try:
        seed = open(p).read().strip()
    except OSError:
        continue
    if seed:
        break
seed = seed or '%012x' % uuid.getnode()
print(99000000 + int(hashlib.sha256(seed.encode()).hexdigest()[:12], 16) % 1000000)
")"
  fi
  # The app can only draw products compiled into it, and only one of them has
  # been verified. The list comes from the code so the two cannot drift apart.
  echo "   products the app can draw: $PRODUCTS"
  echo "   (beolab-8 is the verified one; any other is an experiment, and the"
  echo "   log shows what the app asks for that this product does not answer)"
  PRODUCT="$(ask '   which product should the app see' 'beolab-8')"
  while ! printf ' %s ' "$PRODUCTS" | grep -q " $PRODUCT "; do
    echo "   not a product the app knows: $PRODUCT"
    PRODUCT="$(ask '   which product should the app see' 'beolab-8')"
  done
  # The machine decides both how the bus is spoken to and what the app draws:
  # with none, the product is a plain speaker with nothing to browse.
  # The list comes from the code, as the products do. Only what has been run
  # against the real machine is called verified.
  echo "   machines this adapter can drive: $MACHINES"
  echo "   ($MACHINES_VERIFIED is verified; any other is an experiment, taken to have one"
  echo "   disc, which plays whichever of the app's six is picked)"
  if [ -n "$ML_PORT" ]; then MACHINE=bs9000; else MACHINE=none; fi
  MACHINE="$(ask '   what is behind it (a machine, or none)' "$MACHINE")"
  while [ "$MACHINE" != "none" ] && ! printf ' %s ' "$MACHINES" | grep -q " $MACHINE "; do
    echo "   not a machine this adapter can drive: $MACHINE"
    MACHINE="$(ask '   what is behind it (a machine, or none)' "none")"
  done
  # Asked last, so that the name suggested is the name of what was chosen.
  if [ "$MACHINE" = "none" ]; then
    SUGGESTED="$(PYTHONPATH="$CODE" python3 -c 'import sys; from beo.mozart.profile import MOZART_TYPES; print(MOZART_TYPES[sys.argv[1]].display_name)' "$PRODUCT")"
  else
    SUGGESTED="$(PYTHONPATH="$CODE" python3 -c 'import sys; from beo.masterlink.profiles import PROFILES; print(PROFILES[sys.argv[1]].name)' "$MACHINE")"
  fi
  NAME="$(ask '   what should this product be called in the app' "$SUGGESTED")"
  # A machine with no bus to it is still drawn as that machine; commands to
  # it go nowhere until a port is named in the configuration.
  if [ "$MACHINE" != "none" ]; then
    choose_ml_port
    [ -n "$ML_PORT" ] || echo "   no bus: the app shows the machine, and nothing reaches it"
  fi
  if [ "$MACHINE" = "none" ]; then BEOLINK=0; else BEOLINK=1; fi
  if [ "$MACHINE" != "none" ] && [ -n "$ML_PORT" ]; then BACKEND=masterlink; else BACKEND=none; fi

  # The card a machine is wired to is the machine's. Any other can play what
  # other products send to this one to a speaker directly, and with no
  # machine that is the only way out there is.
  PLAYBACK_DEVICE=hw:0,0
  CAPTURE_DEVICE=hw:0,0
  if [ "$MACHINE" = "none" ]; then
    free_cards ""
    choose_speaker "${BOARD:-${OWN:-none}}"
  else
    choose_machine_card
    free_cards "$WIRED"
    if [ -n "$WIRED" ]; then
      PLAYBACK_DEVICE="hw:CARD=$WIRED,DEV=0"
      CAPTURE_DEVICE="hw:CARD=$WIRED,DEV=0"
    fi
    choose_speaker none
  fi
  LISTEN=off
  SPEAKER_DEVICE=""
  if [ "$SPEAKER" != "none" ]; then
    # A speaker with nobody playing to it is silent, so listening goes on
    # with it, and listening is built on the streaming side.
    BEOLINK=1
    LISTEN=on
    SPEAKER_DEVICE="plughw:CARD=$SPEAKER,DEV=0"
  fi

  sudo tee "$CONF" >/dev/null <<EOF
# This product, as the Bang & Olufsen app sees it.
#
# Written once, at install. Edit it freely and restart the service, with one
# warning: change BEO_SERIAL and the app sees an entirely new product and
# forgets the old one, along with any groups it was part of.
#
# The installer never rewrites this file. It checks that it is sane, and
# changes a line only on a yes: it asks about a speaker while there is none.

BEO_SERIAL=$SERIAL
BEO_NAME=$NAME

# Which Mozart product the app draws. beolab-8 is the verified one; the others
# are the types compiled into the app, there to be tried. The type number is
# part of the jid, so changing this makes the app see a new product too.
BEO_PRODUCT=$PRODUCT

# The classic product behind this one, or none for a plain speaker. It decides
# both how the bus is spoken to and what the app draws.
BEO_MACHINE=$MACHINE

# masterlink = the machine is on the bus. none = no bus attached.
BEO_BACKEND=$BACKEND
BEO_ML_PORT=${ML_PORT:-none}

# Which interface is on that port: mdt1, a MasterDataTool v1 on USB, or mdt2,
# a MasterDataTool v2 on the Pi's own UART (not yet tried against a machine).
BEO_ML_INTERFACE=$ML_INTERFACE

# Stream this product's sound to other speakers, which needs a capture
# device. Listening to other products is built on it too.
BEO_BEOLINK=$BEOLINK

# Show only what the machine actually reports, rather than filling gaps.
BEO_FAITHFUL=1

# Let other products play to this one: on, off, or the addresses of products
# to hold a session open to, comma separated. Needs BEO_BEOLINK.
BEO_LISTEN=$LISTEN

# 1 = add this product to the configuration the network's products share, if
# it is not in it. Sources play only to products that are. It changes a
# document every product holds, so it is not done unasked.
BEO_ENROL=0

# The sound card the machine is wired to: the output that feeds it and the
# input its sound is captured from. hw:0,0 is the first card to come up; with
# more than one card, name it, as hw:CARD=<card>,DEV=0.
BEO_PLAYBACK_DEVICE=$PLAYBACK_DEVICE
BEO_CAPTURE_DEVICE=$CAPTURE_DEVICE

# A sound card that plays to a speaker directly, as plughw:CARD=<card>,DEV=0,
# or empty for none. What other products play to this one comes out there,
# beside the machine or with none. Needs BEO_LISTEN. The product's volume is
# set on the card's mixer; BEO_SPEAKER_MIXER names the control when the one
# with the widest range is not the right one.
BEO_SPEAKER_DEVICE=$SPEAKER_DEVICE

BEO_LOG_LEVEL=INFO
EOF
  echo "   wrote $CONF (serial $SERIAL)"
fi

echo "== service"
# Which version this is, for the service to say in its log. Asked here, by
# whoever owns the clone: the service runs as root, and git answers root
# nothing about a repository that is somebody else's. A copy from a
# development machine is no clone, so whoever made it says which version it
# is, in BEO_VERSION.
VERSION="${BEO_VERSION:-$(git -C "$SRC" describe --tags --always --dirty 2>/dev/null || echo unknown)}"
echo "$VERSION" | sudo tee "$CONF_DIR/version" >/dev/null
echo "   version $VERSION"
sed "s#@SRC@#$SRC#g" "$SRC/deploy/beo-adapter.service" | sudo tee "$UNIT" >/dev/null
sudo systemctl daemon-reload

# The product answers on these, and anything else holding them has to go. Say
# what is being stopped rather than killing silently.
for p in 80 8080 9339; do
  holder="$(sudo fuser "$p"/tcp 2>/dev/null | tr -d ' ' || true)"
  if [ -n "$holder" ] && ! systemctl is-active --quiet beo-adapter; then
    echo "   port $p is held by pid $holder, stopping it"
    sudo fuser -k "$p"/tcp 2>/dev/null || true
  fi
done

sudo systemctl enable beo-adapter >/dev/null 2>&1
sudo systemctl restart beo-adapter

echo "== does it work"
ok=1
for i in 1 2 3 4 5 6 7 8 9 10; do
  sleep 1
  systemctl is-active --quiet beo-adapter || continue
  self="$(curl -s -m 3 http://127.0.0.1/api/v1/beolink/self || true)"
  case "$self" in *products.bang-olufsen.com*) break ;; esac
done

systemctl is-active --quiet beo-adapter || { echo "   the service is not running"; ok=0; }
self="$(curl -s -m 3 http://127.0.0.1/api/v1/beolink/self || true)"
case "$self" in
  *products.bang-olufsen.com*) echo "   it answers as $self" ;;
  *) echo "   it does not answer on port 80 yet"; ok=0 ;;
esac

if [ "$ok" = "1" ]; then
  echo
  echo "   Done. Open the Bang & Olufsen app on a phone on this network."
  echo "   Logs:  journalctl -u beo-adapter -f"
  if [ "$JACK_PENDING" = "1" ]; then
    echo
    echo "   The Pi's own jack is on from the next start, and plays nothing until"
    echo "   then. Restart the Pi:  sudo reboot"
  fi
else
  echo
  echo "   Something is wrong. Start here:  journalctl -u beo-adapter -n 50"
  exit 1
fi
