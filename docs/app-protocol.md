# What the Bang & Olufsen app requires of a Mozart product

This is what the Bang & Olufsen app expects of a product that wants to appear
in it, seen from the product's side of the network: what the app asks for,
what it accepts, and what it shows when an answer is not the one it wanted.
The code that implements it is `beo.mozart.facade`, which points here by
section.

Endpoint, field and event names are the ones that go over the network.

## 1. Discovery

### Service type

The app browses exactly four Bonjour service types: `_beoremote._tcp.`,
`_beo_settings._tcp.`, `_bangolufsen._tcp.` and `_zenith._tcp.`. A Mozart
product is announced as `_bangolufsen._tcp.`. The app never asks for
`_mozart._tcp.`: the device running it queries these four and nothing else.

### TXT record

The record carries `tn` (type number), `in` (item number), `sn` (serial), `fn`
(friendly name), `fv` (firmware version) and `wa` (MAC).

Two of these do different jobs and are easy to confuse:

- **`tn`** decides which product the app thinks this is: image, name,
  capabilities, setup flow. It must be a type the app knows.
- **`in`** is what the Classics Adapter check (§3) compares against `1454530`.
  It has no effect on identity, only on the adapter check.

The jid the app keys the product on is `{tn}.{in}.{sn}@products.bang-olufsen.com`.
The serial is derived once from the machine and persisted, never randomised;
`beo.product.identity` explains the band it sits in.

### Firmware version

`fv` must be two to four numeric parts separated by dots, and nothing else.
The app reads it as a version number with no error handling, so a build-hash
suffix such as `1.9.0.30523.1cf1cea` takes the whole app down. It did, the
first time the emulator got that far.

### Host name

One host name for the whole product, shaped like a real one: the model with
hyphens, then the serial. The BeoLink side must announce the same host name,
or the product appears on the network as two hosts. The emulator once had two,
one of which contained a space.

### Two ports

- **Port 80**, the REST API. The app requests `http://{ip}/...` with no port,
  so the API cannot be moved.
- **Port 8080**, the liveness ping. The app requests `http://{ip}:8080/?` and
  marks the product offline without an answer. Pinging is only skipped when
  the firmware version is at least 5.1 **and** a setting in the app that the
  product does not control is on, so it cannot be relied on.

### Restart and goodbye

The app only (re)starts its WebSocket on a Bonjour "found", and a found needs a
lost before it. After a crash or a kill no goodbye was sent, and the app keeps
the old record. A product that starts therefore sends a goodbye for its own
name first, then the announcement, and sends a goodbye (TTL 0) from its
SIGTERM handler.

Right after a restart the product's own previous record can still sit in the
cache the new mDNS responder builds while probing. With zeroconf this shows as
a name conflict for 30 s or more; `cooperating_responders=True` skips the
probe, which is safe because the name is ours by construction (the serial is
in the host name).

## 2. Identity: why the emulator is a Beolab 8

The app cannot represent a Classics Adapter as a product at all. A discovered
device is resolved by its type number; a type number the app does not know is
discarded, with no fallback, and the Classics Adapter's own type numbers are
not among those it knows.

That is by design. In this app a Classics Adapter is an *accessory*, never a
device: the app asks an existing Mozart product whether one is attached (§3)
and, if so, adds a "CD" group to that product's page.

So the emulator borrows a real Mozart type number (Beolab 8 = 3150) while
carrying the item number the adapter check keys on.

### The other Mozart product types

A product is drawn only when it announces a type number the app knows;
anything else is an unknown product and discarded. These are the eleven the
facade's catalogue offers, the products Bang & Olufsen list as Mozart
products. Where a type has more than one number, the facade uses the first.

| Product | Type numbers | What the app asks for beyond the common set |
|---|---|---|
| Beolab 8 | 3150 | listening modes, tone touch, proximity, stereo pair, Powerlink, WiSA, logo light from firmware 5.1 |
| Beosound Balance | 6471-6474, 1000 | directivity, room compensation, tone touch, proximity, stereo pair; listening modes from firmware 3.4 |
| Beosound Level | 6476, 6478, 6479 | placement, battery, proximity, stereo pair; listening modes from 3.4 |
| Beosound Emerge | 2738 | stereo pair; listening modes from 3.4 |
| Beolab 28 | 4545 | curtain control, listening modes, tone touch, proximity, Powerlink, WiSA, stereo pair |
| Beosound A5 | 2541 | listening modes, tone touch, battery, stereo pair |
| Beosound 2 3rd gen | 3091 | proximity, stereo pair; listening modes from 3.4 |
| Beosound A9 5th gen | 3090 | stereo pair; listening modes from 3.4 |
| Beoconnect Core | 3058 | listening modes, Powerlink and speaker outputs, Beosound Shape, TV source configuration |
| Beosound Theatre | 3100 | TV product: HDMI, speaker groups, Powerlink, external speakers, listening positions |
| Beosound Premiere | 3120 | TV product: HDMI, speaker groups, Powerlink, LED bar |

What the type changes is which pages the app shows for the product, and so
which endpoints it calls. The common set (product info, remote menu, sources,
playback, volume, power, BeoLink, software update, network, cast, presets,
proximity) is asked of every type. The rest follow the product's capabilities:

| Capability | Endpoints the app then uses |
|---|---|
| listening modes | `sound/listening-modes`, `sound/listening-modes/active`. Always for Beolab 8, Beolab 28, A5, Beoconnect Core and the TV products; from firmware 3.4 for the rest, so at the facade's 1.9.0 they never ask |
| tone touch | `sound/settings/toneTouch` |
| proximity sensor, LED bar | `proximity/settings`, `setup/led` |
| logo light (firmware 5.1+) | `logolight/settings` |
| placement | `product/placement` |
| battery | `battery`, `diagnostics/battery` |
| directivity, room compensation | `sound/settings/adjustments/directivity`, `sound/settings/roomCompensation` |
| stereo pair | `speakerlink/*` |
| speaker groups | `sound/speakergroup`, `sound/speakergroup/active` |
| Powerlink | `powerlink/settings`, `output/speakers`, `output/supported-speakers` |
| WiSA | `speakers/wireless/wisa/*` |
| curtain control | `product/curtain/position` |
| Beosound Shape | `output/beosound-shape/*` |
| TV product | `hdmi`, `hdmi/{id}`, longer waits on pending operations, TV source pages |

The facade answers 404 to all of these that it does not model, which the app
treats as "feature not present" (§4). Changing the type therefore changes the
picture, the name and the settings pages the app offers; it does not by itself
break the product. The Classics Adapter check (§3) keys on the item number
alone and runs for any type.

The product type name is not in the Mozart TXT record: only `tn`, `in`, `sn`,
`fn`, `fv` and `wa` are, so the name never matters to the app. It matters to
the other products (below).

### Why not the BeoZone stack

The app looks for a Classics Adapter only on a Mozart product:
`api/v1/classics-adapter` is never requested of an older product that speaks
BeoZone alone.

### The product type field

The product type appears in `/BeoDevice` and in `_beoremote`'s `productType`,
and the two must agree. The value has to be one the peer knows: `CA17`, a
Beoplay M3's hardware code, makes a listening M3 open the connection itself;
the name of our own type makes it ignore us entirely. Consistency gives way
to recognition.

### One product, one everything

The app and the other products assemble their picture of us from several
records, and every one must say the same thing:

- **One MAC.** An invented address meant one hardware id over mDNS and another
  on the BeoLink side: two devices, seen from outside.
- **One name.** The emulator once called itself one thing in `fn` and
  `productFriendlyName` and another in the mDNS service and on the BeoLink
  side, so another product's record of us carried one name and our own
  announcement the other.
- **One jid.** With a separate BeoLink jid the speaker joins perfectly well,
  but the app cannot assemble the group: it asks the speaker who it is
  listening to and gets one jid, and asks us who we are and gets the other.

## 3. The Classics Adapter check

For a Mozart product the app runs this sequence:

1. The product's item number must be `1454530`. It comes from the TXT `in`
   and from `/api/v1/product/info`.
2. `GET api/v1/classics-adapter` must return 2xx with a body.
3. `GET api/v1/remote` returns a dictionary of remote menu items. The app
   looks for an item whose `content.contentUri` is `classicsAdapter` and
   whose label is not `CD`, then
4. `PUT api/v1/remote/{id}` renames it to `CD`. A real product persists the
   new label; without that the app renames on every run.
5. `GET api/v1/content?startWith=classicsAdapter%3A%2F%2FCD%2F`. The result
   must contain **exactly six items**, or the app gives up and shows no
   adapter.
6. For each of the six: `PUT api/v1/content-trigger/{id}`.
7. The adapter is then shown as attached.

## 4. REST conventions

### 404, never an empty 200

The app treats every Mozart endpoint the same way: a successful response is
used, anything else is skipped and it carries on. A non-2xx is a *designed*
path meaning "this product has no such feature". A 200 carrying `{}` is not:
it is read as an object whose fields are all missing, and the app then uses
them without checking. That is exactly how an
early `product/info` stub took the whole app down. When in doubt, 404 is the
safe answer and an empty 200 the dangerous one. The same goes for an
unmodelled endpoint.

### One success code per endpoint

The app decides success on one status code per endpoint and treats anything
else as a failure:

| Endpoint | Success |
|---|---|
| `beolink/expand`, `unexpand`, `join`, `leave` | 202 Accepted |
| `beolink/listeners`, `available-listeners` | 200 |
| `beolink/audioleader` | 200, or 204 when there is no group |

`audioleader` handles 200 and 204 but not 404. Answering 404 when nobody had
been expanded to became an exception instead of "no group yet".

HTTP forbids a body on a 204, and a client that reads a Content-Length of 2 on
one hangs on the next request.

### Field types are as fatal as fields

Values must have exactly the types the API defines. A wrong type is as fatal
as a missing field (see the firmware version above).

## 5. Endpoints

### `GET api/v1/product/info`

The app uses every field without checking. A null anywhere in the tree fails
inside the app, which then reports the useless "could not find the product"
error, which is exactly what a thinner response produced. In particular:

- `production.productionTimestamp` must be a date and time the app can parse.
- `production.macAddresses.macAddresses` needs entries whose `name` contains
  `ethernet` and `wlansta` (matched case-insensitively).
- `apple.homeApp`, `fep.version` and `touch.version` are read directly.
- The `tv` subtree must be present even for a speaker, so nothing throws
  walking into it; empty values assert no TV capability.

### `GET api/v1/remote` (dictionary of remote menu items)

This is where the player's source list comes from, not `playback/sources`.
The app builds its "MUSIC" list from the remote items. Each candidate must
pass all three of:

- its **parent** item's label is `MUSIC`. Calling the root "Sources" silently
  filters every child out of the source list.
- it is not `disabled`.
- the content URI starts with an entry on the app's whitelist, which gains
  `classicsAdapter` once the adapter is detected.

Item ids are GUIDs, not numbers, and so are `parentMenuItemId`,
`firstChildMenuItemId` and `nextSiblingMenuItemId`, each of which may be
null. Sending "1" and "2" made the whole dictionary unreadable, so
`api/v1/remote` yielded nothing and the source list went empty; the app
reported "200 with a body it could not read". The dictionary key must equal
the item's id, lowercase and hyphenated, because the app finds children by
comparing the key with `firstChildMenuItemId`.

Only the tree the app walks is seen: root items (no `parentMenuItemId`), then
`firstChildMenuItemId`, then the `nextSiblingMenuItemId` chain.

### `GET api/v1/playback/sources`

The field names matter: `sourceId` and `friendlyName` are not fields of a
source. Using them produced sources with no `type`, which crashed the app's
source page. `type` is an object with a `value`, not a bare string.

### Selecting a disc

Selecting a disc in the source list sends `POST api/v1/content/{uri}/trigger`,
not `playback/sources/active/{id}`. Found by watching the app. Answering 200
without changing state or emitting events is why picking CD 2 once appeared
to do nothing.

`POST api/v1/playback/sources/active/{id}` receives two different ids. Picking
a disc in the CD list sends a content URI ending in the disc number. Picking
"CD" in the player's source list sends the remote menu item's GUID. Answering
200 to the GUID without changing anything is why selecting the CD source
stopped doing anything.

`POST api/v1/content-trigger/{id}` activates the trigger; `PUT` only renames it.

### `POST api/v1/playback/command/{command}`

The command is one of exactly five values: play, pause, stop, skip, prev.
Mapping anything else to a made-up state is worse than ignoring it: a
playback state the app does not recognise leaves the transport controls
stuck, which is what "skip" did while it fell through to "unknown".

### Power

The power state is one of `networkStandby`, `on`, `standby`, `shutdown`,
`storage`. The app's power button is `PUT api/v1/state/standby` with an empty
body; `api/v1/state/power` with
`{"value": "on"}` wakes the product. It reads `GET api/v1/state/power`, and
changes are pushed as `WebSocketEventPowerState {value}`.

A product shown as "on" gets a standby press from the power button, so a
product that cannot tell whether its machine is on starts in standby.

Standby is not a source. A machine in standby reported with CD as its active
source made the app show CD for a machine that was off.

### Volume

The volume state has `level`, `maximum` and `default` (each `{level}`) and
`muted` (`{muted}`). The scale is 0..100 and the app scales a group's
followers against the leader's **maximum**. A `range` field that is not part
of it left `maximum` missing, and followers ran at twice the level.

### `api/v1/_ml/...`

Private control endpoints, not part of the Mozart API, through which the Master
Link side feeds the product the disc and track that are really in the machine.
The app never calls them, and a name outside the documented `api/v1` surface
cannot collide with one it might call later.

## 6. Playback metadata

### The metadata object

- **`id` must be unique per track, not per disc.** The app decides whether
  the metadata changed on `id` first, then title, art and uri.
  With id = track and a generic title every disc looked identical and
  switching from CD 1 to CD 2 changed nothing on screen.
- **`title` is not free text.** The app collects every digit in the track
  name, treats the result as the disc number, and looks it up among the CD
  items; when nothing matches, the player crashes. So a title may carry a
  track name, but the only digits in
  it must be the disc number: "CD 5 · Variatio 12" collapses to "512", matches
  no disc, and crashes the player. The demo discs therefore avoid digits
  (Roman numerals on the Goldberg Variations), and the facade strips digits
  from track names as a safety net.
- **The first `art[]` entry is the one that shows.** The app takes its `url`
  for any source that is not netRadio or dlna.

### Progress

Without a progress counter the app shows 00:00 and --:-- forever. It shows
the placeholder whenever the current progress is 0, whatever duration the
metadata carries. The duration was always arriving; nothing was counting.

### What a real Beosound 9000 can supply

Nothing of the above. It does not read CD-Text; it identifies a disc by its
lead-in TOC and looks up a name the owner typed in on the Beo4, up to 200
discs, disc names only. On real hardware the app would get the owner's text as
`albumName` and a track number. The facade's `--faithful` flag emulates that.

The 9000 never says how many tracks a disc has. Guessing the next track on
skip with a made-up count drifted further from the machine with every press:
six quick taps left the app on track 1 while the disc played track 4. The
facade sends the key and lets the status telegram say what happened.

## 7. Cover art

The app fetches cover art over plain HTTPS, honouring Android's system trust
store, and rewrites any `http://` art URL to `https://` first. Art can only
load from a host with a publicly trusted certificate: a self-signed listener
is refused (curl on the same device exits 60, "unable to get local issuer
certificate"), a `data:` URI fails too, and user-installed CAs are not
trusted by apps targeting modern Android. The facade therefore runs no HTTPS
listener and serves no art of its own; `art[]` only ever points at the Cover
Art Archive.

None of it shows for the Classics Adapter source, and no emulator can change
that. For a source containing `classicsAdapter` the app discards the `art[]`
array and draws its own grey disc graphic without making a request, which is
why no art fetch ever appeared in the log. The facade keeps real, publicly trusted HTTPS
URLs in `art[]` because the array is part of a faithful metadata object and
would be used should the source type ever change.

## 8. WebSocket notifications

The app opens `ws://{ip}:9339` for state changes. Without it the app never
learns that a source was selected or playback started. It closes the socket
unless the product answers pings.

### Snapshot on connect

A real Mozart product pushes every state once when a client connects, and the
app relies on it: it fetches nothing on connect and renders from the last
event of each type, a cache it clears on disconnect. The
snapshot is the same payloads as the change events, in the order a product
sends them: power, volume, source change, playback source, playback state,
metadata, progress, then one `WebSocketEventBeolinkJoinResult` per listener,
because the app adds a follower per `expand` event and would otherwise show
the group as empty.

Power decides whether there is a source to show, so the source events follow
the power event immediately.

### Event names

`WebSocketEventBeolinkJoinResult` is one of the 24 event names the app
dispatches on. An invented event type is not an error the app reports; it is
simply never delivered.

### `WebSocketEventBeolinkJoinResult`

All four fields are fixed values or a GUID, not free text:

- `status`: idle | joined | failed
- `error`: ok | busy | leaderless | alone | silent | missing | rejected | timeout
- `type`: join | joinRequested | expand | expandRequested
- `requestID`: a GUID

`type` is the one that matters. The app branches on it: `join`
clears the listener list, sets the leader to the jid and marks us a follower
(we have become a follower of someone else); `expand` adds the jid to the
listener list (we have expanded to someone else). Without the field the app
takes it to be join, so it believed we had become a follower of
the speaker and cleared the listener list. The tap was accepted, and the list
was empty again when reopened.

To expand is to play. With the state still `paused` the app had a group with a
leader that was not playing, and drew nothing, however correct every call was.

### Listeners changed

The join event can only add: `expand` appends to the listener list, and no
value of it removes anybody. The only way back is
`WebSocketEventNotification {value: "beolinkListeners"}`, which the app
handles by fetching `GET api/v1/beolink/listeners` again. Without it, leaving
a group hangs: unexpand starts a pending operation, nothing confirms it, the
timer runs out and the app reports it could not remove the listener.

### Expand timing

The app starts a 25 s timer when the expand POST answers 202 and stops it on
the joined/expand event. If the event never comes, the app itself reports that
the expand failed, so a failure is answered with silence. A `failed` event is
not the opposite of `joined`: it stops the timer without clearing the
speaker's pending state and resets the leader, which is the join flow's
bookkeeping, not the expand's. Until 2026-10-06 the event was sent the moment
the POST arrived, and the app showed a listener that was getting no sound.

## 9. Multiroom surfaces

### Who is on the network

A real product ships with no idea who its neighbours are: it listens, and they
appear as they are switched on. A hard-coded list is wrong the moment somebody
buys a speaker or a router hands out a new address. The facade does not browse
mDNS itself, because that would tie the app-facing protocol to the
product-to-product one; the application passes in a callable
(`beo.networklink.discovery.PeerDirectory` on a real install), and it is asked
fresh every time, never cached.

### `GET api/v1/beolink/listeners`

A list of objects with `jid` and `audioTransport`, not bare jid strings. The
app cannot read a list of strings, and a listener it cannot read is never
shown as joined, however well the audio plays.

`audioTransport` is not a free field; the only two values the app knows are
`wired` and `wireless`.

The product records whoever the app told it to expand to and reports it back,
because the app reads the result out of the product's own listener list rather
than waiting for a confirmation. An expand the product does not remember looks
like a failure even after a 200.

### `GET api/v1/beolink/available-listeners`

Every peer, including those already listening, and only `jid` per entry: the
available-listener type has exactly that one field. The app resolves the
display name from its own account data, so a jid it does not recognise simply
does not appear. An empty array is why the app once never offered to expand
the product to anything: it is the list the "play everywhere" sheet is built
from.

Filtering out active listeners (on the assumption that "available" meant
"free") made the speaker vanish from the app entirely, neither joined nor
joinable. Adding `master`, `state` and `enabled` to the entry did nothing:
the app does not read those fields here. Membership lives in neither list; it comes from the
join event with `type=expand`.

### `GET api/v1/beolink/audioleader`

`{jid, friendlyName, audioTransport}` as soon as anyone has been expanded to,
204 otherwise. By all appearances this is where the app gets the group it
draws: an empty leader is an empty group, however well the audio is playing.

### Expand flow

`api/v1/beolink/{expand,unexpand,join}/{jid}` and `/leave`.
The app-facing flow
answers 202; making a real speaker receive audio is the BeoLink plane, which
the application plugs in. Every byte the app sends here is logged, because
what the app asks a Mozart product to do when it expands to an ASE speaker is
not documented anywhere.

The app is told "joined" only when that is true:

- No BeoLink side at all: the app-facing flow is all there is; joined at once.
- The BeoLink side cannot reach the jid: 404, which the app knows as
  "Remote device not found" and reports at once.
- Accepted: 202 now, and the joined event when the stream runs. A BeoLink side
  without an expand-result hook cannot say when, so it is believed at once.

Before the stream, the machine has to put audio on the bus, or the product
streams silence however well the rest of the chain works. On expand a speaker
answers connected, sends Connect, comes out of standby and sets CD with our
jid as its primary experience.

## 10. Legacy BeoZone surfaces

The app fetches some BeoZone surfaces even on a Mozart product, and other
products read them to learn what we are. A product that promises a service in
`_beoremote` and does not deliver it is not a usable peer.

- **Service index `/`.** A peer starts at the root to find out what we offer.
  Answering `{}` while announcing seven services in `_beoremote` said
  "nothing". The list is a Beoplay M3's own, minus BeoInput which it does
  not serve either.
- **`/Ping`.** A Beoplay M3 answers 200 with an empty body. The cheapest
  liveness check a product has, and it was on our service list without
  existing.
- **`/BeoNotify/Notifications`.** The long notification stream, line-delimited
  JSON with the connection held open. The parser reads a line at a time and
  requires each to begin with `{`. We announced it and answered 404, and were
  polled several times a second by a product trying to subscribe.
- **`/BeoZone/Zone/ActiveSources`.** The app reads the group out of it: the
  capabilities list holds those who can be added, the listener list those
  listening now. Without the listener list the app has nothing to draw, and
  without a source with a non-null `sourceType` it builds no Source at all.
  The source id is the BeoZone shape (`CD:jid`), not a Mozart content path;
  a listening M3 itself registered our source as `CD:jid`.
- **`/BeoZone/Zone/Sources`.** Where a product fetches another product's
  catalogue; a 404 here meant nobody could find out what we had to play. One
  product has one entry per source type, not one per disc: discs are content
  inside the source. The fields and their order are from a Beoplay M3's
  own reply; IR code 146 (0x92) is the Beo4 CD key.
- **`/BeoDevice`.** `productType` must agree with the `_beoremote`
  announcement, or a peer that reads both sees two answers to what we are.
- **The rest.** Measured 2026-08-12, the emulator was missing 11 of the 17
  endpoints a real M3 answers on while announcing the same services. The
  shapes are copied from its replies, and only the capabilities a BeoSound
  9000 has are included. The `features` lists are the product's own
  declaration of what it can do, and a peer reads them before trying anything.
  A disc player has no play queue, but the surface has to exist and answer
  empty rather than be missing.
- **Features a speaker does not have** (listening modes, motorised stand,
  HDMI) answer 404, which is how a real product without the hardware answers.
  SpeakerLink is the exception: `type: standalone` is safer than 404, because
  it stops the app offering to pair us.
