# Changelog

What changed between releases, for the people who run it. The packages and
the application share one version.

## Unreleased

### Fixed

- **Other products can now find this one's address.** The product's
  announcement to other products carries its jid as the service's name,
  and the products ask for that name as one label, dots and all. zeroconf
  split it at every dot, so they listed the product by name but could not
  resolve it, showed no IP address under *Discover devices*, answered
  "Unknown peer" and never dialled it. The announcement now goes through
  the Pi's Avahi daemon, which keeps the name whole; zeroconf remains the
  fallback where no daemon runs. The installer adds `python3-dbus`.
- **The product's source can be borrowed.** In the entry the product writes
  into the network's shared configuration, the jid inside its source's id
  was written with literal dots, where every product writes it with the dots
  written out, as the entry's own name is. A product set up with this one
  as its primary found no source to borrow. The entry now has the written-out
  form, and a product that finds its own entry saying something else than
  it writes now sends it again at start. The source is offered under its
  own names, CD and RADIO on a music system, with the product's name
  beside them, as the borrower lists the two.
- **A product that has borrowed the source can play it.** Such a product
  opens a session to this one and asks for the source with a Connect of its
  own, with no request from our side first. That question was declined as
  not implemented. The source side now takes the caller on as a listener,
  starts the stream to it and brings the machine to the source, unless it
  plays it already. When the last such listener goes, a machine switched
  on for it is switched off again. A listener that then asks for another of the
  product's sources gets it: the machine switches, and the one stream
  follows, where before a second stream started beside the first.
- **The keys on a product that plays this one's sound work.** Play,
  pause, next and previous pressed there arrive as the machine's own key
  codes through the product-to-product tunnel, where they went
  unanswered. They now go the way the app's commands go, and every
  product that plays the sound is told whether it plays or pauses, so a
  pause stays a pause with them instead of resuming after a second.
- **A source switch no longer switches the machine off.** Between two
  sources the machine reports standby for a moment, and that was taken
  for it switching off: every listener was dropped. While a selection of
  ours is under way it is the switch.
- **A speaker can join the machine's own playing.** Asked what there is
  to join while the machine played and nobody listened, the product
  answered with nothing; now it answers with the source the machine is
  on, and the speaker's own Connect is served.
- **The app no longer keeps a speaker that left.** A product that stops
  playing this one's sound by itself now leaves the app's list, and one
  that starts by itself is drawn in it.
- **Deeper queues on the way out, and dropped blocks counted.** A block
  dropped between the sound card and a stream is a hole the listener
  fills with a resync, which is a glitch. The queues are deep enough to
  ride out a reader held up for over a second, and a run says at its end
  how many blocks it dropped for whom.
- **Expand is quick again.** A product that already knows this one's
  capabilities says nothing after its stream header when this one dials
  it, and the expand then waited six seconds for a first stanza before
  going on. The peer's jid is now read from its header, so the request
  follows as soon as the peer has spoken.
- **A television can be expanded to.** It wakes its panel before it
  answers, which takes ten seconds and more, and the expand gave up at
  ten. It now waits twenty, as long as the app allows.
- **A lost datagram is sent again.** A product that misses a datagram
  of the stream asks the source for it again, on the stream port plus
  one, and asks until it comes. Nobody listened there, so the product's
  player ran a datagram short of the timestamps and started itself over
  every few seconds: a small gap in the sound each time. The last six
  seconds of every stream are kept, and a request is answered with the
  datagram as it went out.
- **The digit keys on a product that plays this one's sound are the
  machine's own**: a digit alone chooses the disc, and GO, or the centre
  button on a Beoremote One, followed by one or two digits a track on it,
  as on the machine's own remote.
- **`beo-listening`**, installed on the Pi: a speaker's own log as one
  command, the counts of its last minutes of listening, or with
  `--find` the lines that say what it did with a join or a link, or with
  `--sources` its source list, which the older products answer too;
  `--device`, `--active` and `--join` ask the same API what the product
  is, what it plays, and make it join what plays elsewhere; `--peers` is
  its own view of the network and whom it can reach.

### Added

- **The source, on the product's page.** The page shows which of the
  machine's sources plays, CD or Radio on a music system, and a tap on the
  other switches the machine to it, the way the app's own pick does. The
  choice is hidden while the product listens to another product.
- **Speakers that play along by themselves.** On the product's page, under
  *Play on other products*, each product has a button that makes it play
  this product's sound now, and a tick, **Auto**, that makes it play along
  every time this product is switched on, from the app, the remote or the
  machine itself. The ticks are kept in `/etc/beo/play-on`. The page's two
  link blocks, listening to another product and playing on other products,
  fold so that the open one is the direction the product is in.
- **The MasterDataTool v2** as a Master Link interface, beside the v1. It
  sits on the Pi's own UART and has a controller that does the bus framing,
  so the product talks to it in frames of its own rather than in the bus's
  bytes. `BEO_ML_INTERFACE=mdt2` in the configuration selects it, and the
  installer asks when it finds the UART turned on and no USB interface. It
  has not been run against a machine. See
  [getting started](docs/getting-started.md#setup-a-a-classic-music-system).

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
