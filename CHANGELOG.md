# Changelog

What changed between releases, for the people who run it. The packages and
the application share one version.

## Unreleased

## 0.1.0 — 2026-10-15

The first release. A Raspberry Pi with a Master Link interface puts a classic
Bang & Olufsen music system in the Bang & Olufsen app and plays it on network
speakers.

### What it does

- **The music system in the app.** Its discs, play, pause, skip and volume
  from the phone, and what the machine is actually playing. Verified with a
  BeoSound 9000; the BeoSound 3000, 3200 and 4 and the BeoCenter 2 can be
  chosen in the installer and have not been run against the machine.
- **Its sound in other rooms.** Expanded to network speakers from the app,
  in time with them.
- **The house's music on the classic system.** What another product plays
  comes out of the music system. Experimental, and off until turned on.
- **A network speaker of the Pi's own.** With a speaker on the Pi's audio
  output, other products can play to the Pi, with or without a music system.
  Experimental, and off until turned on.
- **In a browser.** A page of the product's own at
  `http://<product>.local/web/`, on the port the app uses: play, pause, skip,
  volume, the discs, and listening to another product. It plays the product's
  sound in the browser, the machine's or the stream the product receives from
  another, and keeps playing on a locked phone. On an iPhone it can be added
  to the home screen. While the product listens to another, the page shows
  and steers that one, its volume included. A pause on a locked phone cannot
  be resumed there; see [configuration](docs/configuration.md).

It is installed with one script, updated with another, and set in one file,
`/etc/beo/product.env`. The libraries it is built from, `beo-product`,
`beo-masterlink`, `beo-networklink` and `beo-mozart`, are versioned with it.

### Fixed before the release

- A source that pauses goes on sending its music and only asks its listeners
  to fall silent. The product did not, so a pause at the source went unheard
  on the machine and on an output of the product's own. Every output now
  plays silence while the source says it pauses.
- With only what the machine reports shown, the title read "CD 1 · Spor",
  the track's name with its number taken out. It is now "CD 1", with the
  track number where the app keeps it.
