# Trying every product type before release

`beo.mozart.profile.MOZART_TYPES` lists eleven Mozart product types the app
can draw, and exactly one of them has been checked against a real app: a
Beolab 8 carrying the Classics Adapter item number. The other ten are
extrapolations. Before the project goes public, each of them should have been
tried once, so that the installer's "there to be tried" is a statement about
what we saw and not a hope.

## How one Pi becomes another product

A product's identity is its jid, `<type>.<item>.<serial>@products.bang-olufsen.com`.
The type number is the first field, so changing `BEO_PRODUCT` changes the jid,
and the app sees a different product. Nothing else has to change: the serial
stays, the IP stays, the name may stay.

Switching is one config line and one restart, on the Pi:

    sudo nano /etc/beo/product.env                   set BEO_PRODUCT=beosound-a5
    sudo systemctl restart beo-adapter
    curl -s http://localhost/api/v1/beolink/self     answers with the new type number

Do not restart while something is playing: a restart mid-playback leaves the
9000 running with nobody listening.

What happens on the network, in order:

1. systemd sends SIGTERM. The facade's handler sends the mDNS goodbye (TTL 0)
   for its `_bangolufsen._tcp` record before the process exits. That is the
   signal the app waits for; without it the old product lingers in its list
   until the record's own TTL runs out (two minutes for the address record,
   75 minutes for the TXT) and the app may show two products for a while.
2. The new process registers a record with the same instance name
   (`BEO_NAME`), the new type number in the TXT and a host name built from the
   new product's name and the serial, `Beosound-A5-99123456.local`. If the
   goodbye was lost, the registration sees its own stale record as a conflict
   and waits it out, up to thirty seconds, exactly as after a crash.
3. The app resolves the record, reads the type number, and draws the product
   it names.

**The same IP is not a problem.** mDNS keys records by instance name and host
name, not by address, and the app keys products by jid. Several products
sharing one address is what every multi-product emulator does. What one Pi
cannot do is be two products at once: the service owns ports 80 and 8080, and
the product's MAC is its hardware id in the app (app-protocol, "One MAC"), so
two products on one Pi would be two products with one hardware id. One at a
time, in sequence.

**The app must be closed fully between switches.** It fetches nothing on its
own after the first WebSocket snapshot, and a backgrounded app keeps the old
product's connection. Swipe it away, open it again, and the new product is
there to be set up as if unboxed. The old one is gone from the list if the
goodbye arrived.

## What to look at for each type

The columns are what differs between types, from the app's side. Fill the
table in as you go; a type that draws, lists sources, plays and expands is as
verified as the Beolab 8.

| product | type | appears | setup completes | sources listed | plays | expands to listener | the app asks for, unanswered |
|---|---|---|---|---|---|---|---|
| beolab-8 | 3150 | yes | yes | yes | yes | yes | nothing (2026-10-06) |
| beosound-a5 | 2541 | | | | | | |
| beosound-2 | 3091 | | | | | | |
| beosound-a9 | 3090 | | | | | | |
| beosound-balance | 6471 | | | | | | |
| beosound-level | 6476 | | | | | | |
| beosound-emerge | 2738 | | | | | | |
| beolab-28 | 4545 | | | | | | |
| beoconnect-core | 3058 | | | | | | |
| beosound-theatre | 3100 | | | | | | |
| beosound-premiere | 3120 | | | | | | |

The last column is the useful one. The log names every request the app makes
and every one the facade answers with 404, so after a type's run:

    journalctl -u beo-adapter --since '-10min' | grep ' 404 '

What each type's capability set makes the app call beyond the common
endpoints is written next to it in `MOZART_TYPES` (`asks_for`). A battery product will ask for a battery; a TV product will ask
for HDMI inputs and speaker groups. An unanswered request is not necessarily a
failure, the app may just leave that page blank, but it is the list of what a
faithful profile for that type would have to add.

Expect the TV types (Theatre and Premiere) to be the odd ones: their setup
flow in the app assumes a screen, and the sources they list are inputs, not a
music menu. They are in the list because the app can draw them, not
because a Beosound 9000 behind a television makes sense.

## Results

Dated, one line per run, newest first. What was tried, what the app did, what
the log said.

- 2026-10-06, beolab-8: the reference. Appears, sets up, lists CD and radio,
  plays, and expands to a speaker. A television as listener accepts the
  request but reports no valid provider for our source. Nothing unanswered in
  the log.
