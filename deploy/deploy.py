#!/usr/bin/env python3
"""Deploy this repository to the Pi from a development machine.

    python deploy/deploy.py            copy the repository to the Pi (tar over ssh) and run
                                       deploy/install.sh there (packages, udev, systemd, start).
                                       From a terminal the installer asks its questions here;
                                       without one it takes its defaults and changes no setting.
    python deploy/deploy.py --copy     copy only: the service keeps running the code it started with
    python deploy/deploy.py --status   show service status + last log lines
    python deploy/deploy.py --backups  list the copies taken on the Pi

Code is edited on this machine; the Pi only ever receives what is here, and
after a copy its source directory holds exactly this: files no longer in the
repository, and trees from earlier layouts, are removed.

Every copy is preceded by a backup ON the Pi of the source tree, the config
directory and the unit file, into ~/backup/<timestamp>/. The copy is aborted if
the backup fails, because the tarball is extracted over the running install and
there is no other way back. --no-backup skips it and is for when the Pi has
nothing worth keeping.
"""
from __future__ import annotations

import io
import os
import subprocess
import sys
import tarfile

# Where to deploy. A developer's own machine, so it comes from the
# environment rather than being written into a published file:
#     set BEO_PI=pi@203.0.113.50
PI = os.environ.get("BEO_PI", "pi@raspberrypi.local")
# The repository this file sits in. It lands on the Pi as REMOTE/beo whatever
# it is called here, so the backups and the unit file keep one path. The
# paths on the Pi are relative to the home of whoever BEO_PI logs in as.
REPOS = {"beo": os.path.dirname(os.path.dirname(os.path.abspath(__file__)))}
REMOTE = "~/src"
SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".ruff_cache",
             ".mypy_cache", "node_modules"}


def bundle() -> tuple[bytes, list[str]]:
    """The tarball and the paths in it, relative to REMOTE."""
    names: list[str] = []
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, root in REPOS.items():
            if not os.path.isdir(root):
                print("  missing:", root)
                continue
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS
                               and not d.endswith(".egg-info")]
                for fn in filenames:
                    if fn.endswith((".pyc", ".log", ".bak", ".pcapng", ".wav")):
                        continue
                    full = os.path.join(dirpath, fn)
                    arc = name + "/" + os.path.relpath(full, root).replace("\\", "/")
                    tar.add(full, arcname=arc)
                    names.append(arc)
    return buf.getvalue(), names


def prune_cmd() -> str:
    """Taken on the Pi after the extraction, with the bundle's paths on stdin.

    Whatever is under REMOTE and not in the bundle goes, compiled caches
    included, so the Pi never runs a module the repository has dropped. The
    service runs as root and owns the caches it writes, hence sudo.
    """
    return (
        'cd %(remote)s && sort -u > /tmp/beo-want && '
        'sudo find . -mindepth 1 \\( -type f -o -type l \\) | sed "s|^\\./||" | sort > /tmp/beo-have && '
        'comm -23 /tmp/beo-have /tmp/beo-want > /tmp/beo-stale && '
        'tr "\\n" "\\0" < /tmp/beo-stale | sudo xargs -0 -r rm -f && '
        'sudo find . -mindepth 1 -type d -empty -delete && '
        'echo "removed $(wc -l < /tmp/beo-stale) stale file(s)" && ls'
        % {"remote": REMOTE})


BACKUP_DIR = "~/backup"
KEEP = 10


def backup_cmd() -> str:
    """Taken on the Pi, before anything is overwritten. Keeps the last KEEP copies.

    The configuration and the unit file are copied under the current name and
    the ones earlier installations used, whichever exist.
    """
    return (
        'set -e; ts=$(date +%%Y%%m%%d-%%H%%M%%S); d=%(bak)s/$ts; mkdir -p "$d"; '
        'if [ -d %(remote)s ]; then cp -a %(remote)s "$d/src"; else echo "   (no source tree yet)"; fi; '
        'for c in /etc/beo /etc/bs9000-adapter; do '
        'if [ -d "$c" ]; then sudo cp -a "$c" "$d/etc-$(basename "$c")"; fi; done; '
        'for u in beo-adapter beo-product bs9000-adapter; do '
        'if [ -f /etc/systemd/system/$u.service ]; then '
        'sudo cp -a /etc/systemd/system/$u.service "$d/"; fi; done; '
        'echo "   backup: $d  ($(du -sh "$d" | cut -f1))"; '
        'ls -1dt %(bak)s/*/ 2>/dev/null | tail -n +%(keepn)d | xargs -r sudo rm -rf'
        % {"bak": BACKUP_DIR, "remote": REMOTE, "keepn": KEEP + 1})


def ssh(cmd: str, stdin: bytes = None, timeout: int = 600, tty: bool = False) -> int:
    """tty: give the command a terminal on the Pi, so that it can ask."""
    args = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=20"]
    if tty:
        args.append("-t")
    p = subprocess.run(args + [PI, cmd], input=stdin, timeout=timeout)
    return p.returncode


def main() -> int:
    if "--status" in sys.argv:
        return ssh("systemctl --no-pager --lines=25 status beo-adapter; echo; "
                   "journalctl -u beo-adapter -n 40 --no-pager")
    if "--backups" in sys.argv:
        return ssh('ls -1dt %s/*/ 2>/dev/null || echo "no backups yet"' % BACKUP_DIR)
    data, names = bundle()
    print("bundle: %.1f kB, %d files" % (len(data) / 1024, len(names)))
    if "--no-backup" in sys.argv:
        print("backup: SKIPPED (--no-backup)")
    else:
        print("backup: copying the Pi's current state first")
        if ssh(backup_cmd(), timeout=300):
            print("backup FAILED — nothing copied. Fix it or pass --no-backup.")
            return 1
    rc = ssh("mkdir -p %s && cd %s && tar xzf -" % (REMOTE, REMOTE), stdin=data)
    if rc:
        return rc
    rc = ssh(prune_cmd(), stdin="\n".join(names).encode() + b"\n")
    if rc:
        return rc
    if "--copy" not in sys.argv:
        # With somebody at the keyboard the installer gets a terminal and
        # no time limit: a question waits for as long as its answer takes.
        asking = sys.stdin.isatty()
        return ssh("bash %s/beo/deploy/install.sh" % REMOTE,
                   timeout=None if asking else 900, tty=asking)
    print("copied. The service still runs the code it started with; "
          "run without --copy to install and restart it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
