"""beo-adapter service entry point.

    python -m beo_adapter.main                                  everything from the configuration
    python -m beo_adapter.main --machine bs9000 --ml /dev/ttyUSB0 --beolink --faithful
    python -m beo_adapter.main --machine none                   a plain speaker, nothing behind it
    python -m beo_adapter.main --machine bs9000 --ml none       the 9000's product, with no bus attached
    python -m beo_adapter.main --machine none --beolink --listen on --speaker-device plughw:CARD=Headphones,DEV=0
                                                                a speaker other products play to

Everything after the adapter's own options is passed to the facade
(beo.mozart.facade.run), so --name, --ip, --port, --beolink-jid, --beolink-device, --faithful all work.
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys

_OFF = ("none", "no", "off")


def _configure_logging(level: str) -> None:
    from beo.mozart import facade
    handlers = [logging.StreamHandler()]
    try:
        handlers.append(logging.FileHandler(facade.LOG_PATH, encoding="utf-8"))
    except OSError as e:
        print("no file log at %s: %s" % (facade.LOG_PATH, e), file=sys.stderr)
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname).1s %(name)s %(message)s", handlers=handlers)


def _backend_off() -> bool:
    return os.environ.get("BEO_BACKEND", "").strip().lower() in _OFF


def _default_machine() -> str:
    """Which classic product is behind this one, according to the configuration.

    BEO_MACHINE names it. A configuration written before that setting existed
    has only BEO_BACKEND, and the only machine there was then is a BeoSound
    9000, so that is what a missing BEO_MACHINE means on a bus.
    """
    explicit = os.environ.get("BEO_MACHINE", "").strip().lower()
    if explicit:
        return explicit
    return "none" if _backend_off() else "bs9000"


def _default_ml_interface() -> str:
    """Which MasterLink interface is on the Pi: mdt1 unless the configuration
    says otherwise, as every configuration from before the setting existed
    was written for one."""
    return os.environ.get("BEO_ML_INTERFACE", "").strip().lower() or "mdt1"


def _default_ml_port() -> str:
    """Where the bus is, according to the installed configuration. A v1 is a
    USB serial port; a v2 sits on the Pi's own UART."""
    if _backend_off():
        return "none"
    return os.environ.get("BEO_ML_PORT") or ("/dev/serial0" if _default_ml_interface() == "mdt2" else "/dev/ttyUSB0")


def _default_log_level() -> str:
    return os.environ.get("BEO_LOG_LEVEL") or "INFO"


def _version() -> str:
    """What the installer found this to be: a release, a commit, or unknown
    for a copy that came without its history."""
    try:
        with open(os.environ.get("BEO_VERSION_FILE", "/etc/beo/version"), encoding="utf-8") as f:
            return f.read().strip() or "unknown"
    except OSError:
        return "unknown"


def _settings_from_environment(rest):
    """Fill in what the configuration says, without overriding the command line.

    The service is started with no arguments at all: everything comes from
    /etc/beo/product.env, so the unit file is the same on every installation.
    A human running it by hand can still say anything and be obeyed.
    """
    out = list(rest)
    name = os.environ.get("BEO_NAME", "").strip()
    if name and not any(a == "--name" or a.startswith("--name=") for a in out):
        out += ["--name", name]
    capture = os.environ.get("BEO_CAPTURE_DEVICE", "").strip()
    if capture and not any(a == "--beolink-device" or a.startswith("--beolink-device=") for a in out):
        out += ["--beolink-device", capture]
    if os.environ.get("BEO_BEOLINK", "").strip() in ("1", "true", "yes", "on"):
        if "--beolink" not in out:
            out.append("--beolink")
    if os.environ.get("BEO_FAITHFUL", "").strip() in ("1", "true", "yes", "on"):
        if "--faithful" not in out:
            out.append("--faithful")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--machine", default=_default_machine(),
                    help="the classic product behind this one (BEO_MACHINE in the "
                         "configuration): bs9000, another of beo_adapter.machines.MACHINES, "
                         "or none for a plain speaker. It decides both how the bus is "
                         "spoken to and what the app draws")
    ap.add_argument("--ml", default=_default_ml_port(),
                    help="MasterLink serial port (BEO_ML_PORT in the configuration; MDT v1: "
                         "/dev/ttyUSB0, v2: /dev/serial0). 'none' = no bus attached: the "
                         "machine's product is shown, and commands go nowhere")
    ap.add_argument("--ml-interface", default=_default_ml_interface(),
                    help="which MasterLink interface is on the port (BEO_ML_INTERFACE in the "
                         "configuration): mdt1, a MasterDataTool v1 on USB, or mdt2, a "
                         "MasterDataTool v2 on the Pi's own UART. The v2 has not been tried")
    ap.add_argument("--no-source-center", action="store_true",
                    help="do not act as ML SourceCenter (N.RADIO / N.MUSIC into the machine)")
    ap.add_argument("--no-discovery", action="store_true",
                    help="do not look for other products on the network. Without "
                         "discovery there is nobody to expand to, so this is for "
                         "testing the product itself in isolation")
    ap.add_argument("--listen", default=os.environ.get("BEO_LISTEN", ""),
                    help="let other products play to this one (BEO_LISTEN in the "
                         "configuration): on, or the addresses of products to hold a "
                         "session open to, comma separated. Needs --beolink. With a "
                         "machine behind the product the stream is played into it, and "
                         "with an output of the product's own it is played there")
    ap.add_argument("--enrol", action="store_true",
                    default=os.environ.get("BEO_ENROL", "").strip() in ("1", "true", "yes", "on"),
                    help="add this product to the configuration the network's products "
                         "share, if it is not in it (BEO_ENROL in the configuration). "
                         "Sources play only to products that are. It changes a document "
                         "every product holds, so it is not done unasked")
    ap.add_argument("--playback-device", default=os.environ.get("BEO_PLAYBACK_DEVICE", "hw:0,0"),
                    help="the sound card output that feeds the machine "
                         "(BEO_PLAYBACK_DEVICE in the configuration)")
    ap.add_argument("--speaker-device", default=os.environ.get("BEO_SPEAKER_DEVICE", "").strip(),
                    help="a sound card output of the product's own, played to a speaker "
                         "directly (BEO_SPEAKER_DEVICE in the configuration), such as "
                         "plughw:CARD=Headphones,DEV=0. What other products play to this "
                         "one comes out there, beside the machine or with none. Needs --listen")
    ap.add_argument("--speaker-mixer", default=os.environ.get("BEO_SPEAKER_MIXER", "").strip(),
                    help="the mixer control that sets the level of that output "
                         "(BEO_SPEAKER_MIXER in the configuration). Left out, it is the "
                         "card's playback volume with the widest range")
    ap.add_argument("--web", default=os.environ.get("BEO_WEB", "on"),
                    help="the product's page at /web/, with its sound for a browser "
                         "(BEO_WEB in the configuration): on or off")
    ap.add_argument("--web-format", default=os.environ.get("BEO_WEB_FORMAT", "aac").strip().lower(),
                    help="what the sound is sent to a browser as (BEO_WEB_FORMAT): aac or mp3")
    ap.add_argument("--web-bitrate", default=os.environ.get("BEO_WEB_BITRATE", "256k").strip(),
                    help="its bitrate (BEO_WEB_BITRATE), as ffmpeg writes it: 256k")
    ap.add_argument("--log-level", default=_default_log_level())
    ap.add_argument("--product", default=os.environ.get("BEO_PRODUCT", ""),
                    help="which Mozart product the app should see (BEO_PRODUCT in the "
                         "configuration). beolab-8 is the verified one; see "
                         "beo.mozart.profile.MOZART_TYPES for the rest")
    a, rest = ap.parse_known_args(argv)

    _configure_logging(a.log_level)
    log = logging.getLogger("beo-adapter")

    from beo.mozart import facade
    from beo.mozart.profile import mozart_type
    from .machines import NoMachine, machine_named, profile_for, sources_for
    from .ml_bridge import INTERFACES

    try:
        machine = machine_named(a.machine)
        mozart = mozart_type(a.product)
        if a.ml_interface not in INTERFACES:
            raise ValueError("no MasterLink interface called %r; one of %s" % (a.ml_interface, ", ".join(INTERFACES)))
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2

    if machine is None or a.ml.strip().lower() in _OFF:
        backend = NoMachine()
    elif not os.path.exists(a.ml):
        # Left to the serial library, this is a traceback that names neither
        # the setting nor the way out.
        log.error("there is no MasterLink interface at %s. Name the right port in "
                  "BEO_ML_PORT in /etc/beo/product.env, or set BEO_BACKEND=none to run "
                  "with no bus, then restart the service%s", a.ml,
                  ". A MasterDataTool v2 is on /dev/serial0 once the Pi's UART is turned on "
                  "in its start-up file" if a.ml_interface == "mdt2" else "")
        return 1
    else:
        backend = machine.bridge(a.ml, interface=a.ml_interface, source_center=not a.no_source_center)
        backend.start()

    def stop(*_):
        try:
            facade.shutdown_mdns()          # the goodbye the app waits for
            close = getattr(backend, "close", None)
            if close is not None:
                close()
        finally:
            os._exit(0)
    signal.signal(signal.SIGTERM, stop)

    # The machine decides the profile. A product with nothing behind it is a
    # plain speaker, because a disc player in the app with no discs to play is
    # a promise nothing can keep.
    profile = profile_for(machine, mozart)
    log.info("version: %s", _version())
    log.info("product: %s (type %s%s)", mozart.display_name, mozart.type_number,
             "" if mozart.verified else ", not verified against the app")
    log.info("behind it: %s%s%s", machine.description if machine else "nothing, a plain speaker",
             "" if machine is None or machine.verified else ", not verified on the machine",
             "" if machine is None or not isinstance(backend, NoMachine) else ", no bus attached")

    # Who else is on the network, found the way a real product finds it: by
    # listening, from now until the process ends. There is no list to maintain
    # and no file to edit, so a speaker switched on this evening is offered
    # without anyone touching the install.
    directory = None
    if not a.no_discovery:
        from beo.networklink.discovery import PeerDirectory
        own = "%s.%s.%s" % (profile.type_number, profile.item_number, facade.SERIAL)
        directory = PeerDirectory(exclude_jid=own).start()
        facade.set_peers_provider(directory.peers)

    def stop_discovery():
        if directory is not None:
            directory.close()

    # Sound between products is handed in here, not built by the facade.
    # Without the factory every expand answers and no sound goes anywhere.
    from .streaming import BeolinkOut
    from .listening import BeolinkIn, from_configuration

    # The listening side presents the same identity as the streaming side, so
    # it is built from it, at the moment the facade has settled who we are.
    dial = from_configuration(a.listen)

    speaker = None
    if a.speaker_device and dial is None:
        log.warning("an output of the product's own is configured (%s) and listening is "
                    "not, so nothing will be played on it", a.speaker_device)
    elif a.speaker_device:
        from .speaker import Speaker
        speaker = Speaker(a.speaker_device, profile.volume_maximum, a.speaker_mixer)
        facade.set_volume_listener(speaker.volume)
        log.info("own output: %s", a.speaker_device)

    # The page in a browser, and the product's sound for it. The page is
    # there from the start; the sound only once the BeoLink side has built
    # the capture, and silence until then.
    web = None
    if a.web.strip().lower() not in _OFF + ("0", "false"):
        from .web import WebStream
        try:
            web = WebStream(codec=a.web_format, bitrate=a.web_bitrate)
        except ValueError as e:
            print(e, file=sys.stderr)
            return 2
        if machine is not None:
            web.on_machine = facade.request_ml_audio
        facade.serve("/web", web.serve)
        log.info("web page at /web/, sound sent as %s %s", a.web_format, a.web_bitrate)

    def beolink_out(*args, **kw):
        out = BeolinkOut(*args, sources=sources_for(machine), **kw)
        if web is not None:
            web.machine = out.capture
            if out.tone:
                from beo.networklink.audio import Fanout, ToneSource
                web.machine = Fanout(ToneSource(out.fmt, 440.0, 0.2), paced=False)
        if dial is not None:
            host = facade.HOSTNAME.rstrip(".")
            # The network's own time, when the clock follows it: the stamp the
            # stream clock adds for sending is taken off again.
            clock = out.role.clock
            lead = getattr(clock, "lead_ns", None)
            now_ns = (lambda: clock.now_ns() - lead) if lead is not None else None
            listening = BeolinkIn(out.ident, out.role.catalog, dial=dial,
                                  host=host[:-len(".local")], machine=backend,
                                  device=a.playback_device, speaker=speaker,
                                  web=web, now_ns=now_ns,
                                  enrol=a.enrol,
                                  wireless=facade.OUR_TRANSPORT == "wireless").start()
            facade.set_beolink_in(listening)
            if web is not None:
                web.listening = listening
        return out

    try:
        facade.run(_settings_from_environment(rest), ml_backend=backend, profile=profile,
                   beolink_out_factory=beolink_out)
    finally:
        stop_discovery()
    return 0


if __name__ == "__main__":
    sys.exit(main())
