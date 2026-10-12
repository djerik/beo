#!/bin/bash
# Update this product. Run it ON the Pi, in the clone the service runs from.
#
#     bash deploy/update.sh            the latest
#     bash deploy/update.sh v1.2       one release, or any commit: also the way back
#     bash deploy/update.sh --force    even while it plays
#
# It fetches, moves the clone to the version asked for, and runs that
# version's installer with its defaults, which restarts the service. If the
# product does not answer afterwards, it goes back to the version it came from.
#
# What it will never do: change the configuration, or interrupt music without
# being told to. An update is asked for by the owner, not taken.
set -eu

SRC="$(cd "$(dirname "$0")/.." && pwd)"
CONF_DIR=/etc/beo
BACKUP_DIR="$HOME/backup"

name_of() {   # a version as a person would say it: the release, or the commit
  git -C "$SRC" describe --tags --always "$1" 2>/dev/null || echo "$1"
}

playing() {
  # A player or a recorder of the service's is sound in or out right now; the
  # product's own state covers a machine playing with nobody listening.
  pgrep -x aplay >/dev/null 2>&1 && return 0
  pgrep -x arecord >/dev/null 2>&1 && return 0
  curl -s -m 3 http://127.0.0.1/api/v1/playback/state 2>/dev/null | python3 -c '
import json, sys
sys.exit(0 if json.load(sys.stdin)["state"]["value"] == "started" else 1)' 2>/dev/null
}

install() {   # the installer of whatever version the clone is at
  bash "$SRC/deploy/install.sh" --quiet
}

# Everything runs inside one function, called on the last line: the update
# replaces this file while it runs, and a shell reads a script as it goes.
main() {
  local force=0 want="" from to
  for arg in "$@"; do
    case "$arg" in
      --force) force=1 ;;
      -*) echo "unknown option: $arg" >&2; exit 2 ;;
      *) want="$arg" ;;
    esac
  done

  echo "== where this is"
  if ! git -C "$SRC" rev-parse --git-dir >/dev/null 2>&1; then
    echo "   $SRC is a copy without its history, so there is nothing to fetch into." >&2
    echo "   Clone the repository on the Pi and install from the clone; the" >&2
    echo "   configuration in $CONF_DIR is kept, so it comes back as this product." >&2
    exit 1
  fi
  from="$(git -C "$SRC" rev-parse HEAD)"
  echo "   $(name_of "$from")"
  if [ -n "$(git -C "$SRC" status --porcelain --untracked-files=no)" ]; then
    echo "   files here have been edited, and an update would overwrite them:" >&2
    git -C "$SRC" status --short --untracked-files=no >&2
    echo "   Put them back (git checkout -- <file>) or keep them elsewhere first." >&2
    exit 1
  fi

  echo "== what there is"
  git -C "$SRC" fetch --quiet --tags origin
  if [ -n "$want" ]; then
    to="$(git -C "$SRC" rev-parse --verify --quiet "$want^{commit}" \
          || git -C "$SRC" rev-parse --verify --quiet "origin/$want^{commit}" || true)"
    if [ -z "$to" ]; then
      echo "   no version called $want. Releases:" >&2
      git -C "$SRC" tag --sort=-creatordate | head -10 | sed 's/^/     /' >&2
      exit 1
    fi
  else
    # The branch the repository itself calls its main one, whatever its name.
    git -C "$SRC" remote set-head origin --auto >/dev/null 2>&1 || true
    to="$(git -C "$SRC" rev-parse --verify --quiet "origin/HEAD^{commit}" || true)"
    if [ -z "$to" ]; then
      echo "   the repository does not say which branch is its latest" >&2
      exit 1
    fi
  fi
  if [ "$to" = "$from" ]; then
    echo "   already at $(name_of "$from"); nothing to do"
    exit 0
  fi
  echo "   $(name_of "$from") -> $(name_of "$to")"
  git -C "$SRC" log --oneline --no-decorate "$from..$to" | head -20 | sed 's/^/     /'

  # Checked last, so that there is something to interrupt for.
  if [ "$force" = "0" ] && playing; then
    echo "   it is playing, and the update restarts it. Run this again when it" >&2
    echo "   is quiet, or with --force." >&2
    exit 1
  fi

  echo "== keeping what is here"
  if [ -d "$CONF_DIR" ]; then
    local kept="$BACKUP_DIR/$(date +%Y%m%d-%H%M%S)"
    mkdir -p "$kept"
    sudo cp -a "$CONF_DIR" "$kept/etc-beo"
    echo "$from" > "$kept/version"
    echo "   the configuration, and the version it ran, in $kept"
  fi

  echo "== updating"
  # The clone sits at a commit and on no branch: which branch it was on, and
  # what state that branch was left in, then never matters.
  git -C "$SRC" checkout --quiet --detach "$to"
  if install; then
    echo
    echo "   Now at $(name_of "$to"). To go back:  bash deploy/update.sh $(name_of "$from")"
    exit 0
  fi

  echo
  echo "== going back to $(name_of "$from")"
  git -C "$SRC" checkout --quiet --detach "$from"
  if install; then
    echo
    echo "   $(name_of "$to") did not come up, and $(name_of "$from") runs again." >&2
  else
    echo
    echo "   $(name_of "$to") did not come up, and neither did $(name_of "$from")." >&2
    echo "   Start here:  journalctl -u beo-adapter -n 50" >&2
  fi
  exit 1
}

main "$@"; exit
