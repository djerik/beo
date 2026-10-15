# Changelog

What changed between releases, for the people who run it. The packages and
the application share one version.

## Unreleased

### Added

- **A page of the product's own** at `http://<product>.local/web/`, on the
  port the app uses: play, pause, skip, volume, the discs, and listening to
  another product. It plays the product's sound in the browser, the machine's
  or the stream the product receives from another, and keeps playing on a
  locked phone. On an iPhone it can be added to the home screen. While the
  product listens to another, the page shows and steers that one, its volume
  included. See [configuration](docs/configuration.md) for what it can and
  cannot do on a locked phone.
- `BEO_WEB`, `BEO_WEB_FORMAT` and `BEO_WEB_BITRATE`: the page is on unless
  turned off, and its sound is AAC at 256 kbit/s unless set otherwise.
- The installer installs `ffmpeg`, which encodes the sound for the browser.
- `beo.mozart.facade.serve(prefix, handler)`, for an application that answers
  paths of its own on the facade's port.

### Fixed

- A source that pauses goes on sending its music and only asks its listeners
  to fall silent. The product did not, so a pause at the source went unheard
  on the machine and on an output of the product's own. Every output now
  plays silence while the source says it pauses.
- With only what the machine reports shown, the title read "CD 1 · Spor",
  the track's name with its number taken out. It is now "CD 1", with the
  track number where the app keeps it.
