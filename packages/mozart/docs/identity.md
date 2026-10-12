# Mozart identity: type, item number and serial

Every Mozart product identifies itself by one jid:

    <type>.<item>.<serial>@products.bang-olufsen.com

The adapter announces this over mDNS (`_bangolufsen._tcp`) and returns it from
`GET /api/v1/beolink/self`. The B&O app decides from it what the product is,
which UI it draws, and whether a Classics Adapter is attached. The installer
therefore has to settle on a triple that the app accepts, that stays the same
for the life of the installation, and that cannot collide with a real product on
the same network.

- **type** — the product model. The app has a fixed table; an unknown type is
  not rendered.
- **item** — the sales item number. `1454530` is the Classics Adapter item;
  the app shows the Classics Adapter UI (disc selection, CD source) only for
  this combination of a known type and this item number.
- **serial** — eight digits, apparently one counter shared by every model
  rather than a series per type (see below).

Only one type and item pair is verified against a real app: **3150.1454530**
(Beolab 8 with Classics Adapter), with a serial in the band 99 000 000 to
99 999 999. The band is deliberately outside the real range so it can never
clash with a shipped unit.

## Serial numbers look like one global counter

Serials observed in public debug logs and REST examples, where the person
had no reason to curate the number. They are rounded here because each one
identifies somebody's product.

| Serial | Product | Seen in | Observed |
|---|---|---|---|
| ~26.0 million | Beoplay A9 2nd gen | jid | June 2023 |
| ~27.9 million | Beoplay M5 | jid | February 2022 |
| ~33.1 million | unidentified, type 4541 | jid | January 2024 |
| ~35.9 million | Beolab 28 | `X-Mozart-Serial` response header | December 2022 |
| ~38.2 million | Beosound Level | `X-Mozart-Serial` header, firmware 5.1.0.249 | March 2026 |

The observation date is an upper bound on manufacture, never the other way
round.

B&O appears not to keep a separate number series per model. Two products of very
different age and price sit a few hundred thousand apart, while two products of
the same generation sit millions apart. A per-model series cannot produce that.
Older BeoWorld tables for MasterLink-era products show the same pattern, with
the leading digits tracking the production year, 09 in 1994 and 24 in 2013.

The counter is not linear, so no year formula survives contact with the data.
Roughly 1.1 million units a year fits the 2013 to 2020 stretch, but a Beolab 28
already carried 35.9 million at the end of 2022, while a Level built no later
than early 2026 carried 38.2 million.

### What is not evidence

BeoTool's App Store listing shows a product details screen with a BeoPlay A6 at
serial 00093323 and a BeoSound Essence at 25023788. If the first were a real
reading it would disprove everything above, because no single counter produces
both. It cannot be used. Store screenshots are marketing material and
identifiers in them are routinely sanitised or invented, and the same screen
supplies the type and item numbers, so those are no more trustworthy.

Two ways to settle it, neither done yet: read the eight digits off a product's
own label, or read the third field of the jid a product announces over mDNS.
The second is free for any product on the network and needs no screenshot.

## How the serial is chosen

Not randomly. The jid is the app's primary key for this product, so a serial
that changes between installs makes the app see a brand-new product, leaves the
old one behind as an unreachable ghost, and leaves listeners holding a stale jid
in their `ActiveSources`. Realism was never the goal either: the app accepted
a serial in the 99 band, and nothing in any measurement so far suggests it
validates the range.

So the serial is **derived once from the machine itself and persisted**, always
in the band 99 000 000 to 99 999 999. That band sits above every serial anyone
has observed, so it cannot collide with a shipped unit, and it is recognisable
as ours in a real product's own log, which is how the adapter's `WakeUp` lines
are picked out of a listener's log.

Precedence, highest first:

1. `--serial NNNNNNNN` on the command line.
2. `BEO_SERIAL` in the environment. The installer writes it into
   `/etc/beo/product.env`, which is where an installed product gets it.
3. The serial file `/etc/beo/serial`, if one exists.
4. Derived from `/etc/machine-id`, as a last resort from the MAC address.

```python
def derive_serial(seed=None):
    """99 + six digits out of the machine's own id. Same machine, same serial."""
    ...
    h = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return "99" + "%06d" % (int(h[:12], 16) % 1000000)
```

The installer derives a serial only when there is no configuration yet. If an
earlier installation left a serial file, that value is carried across instead,
so the product keeps the identity it has already published, and an existing
configuration without a serial stops the install rather than getting a new
one. The same derivation is implemented twice, in
`beo.product.identity.derive_serial` and inline in `deploy/install.sh`, and
the two must agree; both were checked against the same machine id.

The hostname is derived from the serial in the same place, because mDNS and the
BeoLink side announcing different names makes one product look like two.

### Why not a realistic serial

Three reasons, in order of weight. Nothing has ever been observed to validate
the range, and the app accepted a serial in the 99 band, so realism buys nothing measurable.
The ranges rest on five incidental sightings, so a number picked to look real
would be a guess dressed up as a fact. And consumers that key off the bare
serial, such as the Home Assistant integration which builds its entity ids from
it, can be shadowed by a fake that lands on a real device's number, which the 99
band makes impossible.

The collision risk itself is small either way, because the identity is the whole
jid: a clash needs another Beolab 8 carrying the Classics Adapter item number
*and* the same serial. That weakens the realism argument on its own terms rather
than supporting it.

## What was not found

No published list of serial numbers exists, and no published statement of how
the series are allocated. B&O's own support pages describe where to read the
eight digits off the product, never what the ranges mean. Searching turns up
only numbers people pasted by accident. The remaining sources are owner
photographs of the label, the app's About screen and tools like BeoTool, none of
which is searchable as text, and none of which has been read off a real device
here yet.

So the table is a collection of sightings, not a specification, and it is used
for one purpose only: to establish a ceiling, so the fake band can be placed
safely above everything anyone has seen. That purpose survives even if the
global-counter reading turns out to be wrong.
