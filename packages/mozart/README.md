# beo-mozart

What the Bang & Olufsen app talks to. Serve this and a product appears in the
app, with its sources, transport controls, volume and multiroom.

What the app requires, and why each answer is shaped as it is, is written up
in [docs/app-protocol.md](../../docs/app-protocol.md); the code's comments
point into it by section.

The package speaks to no other product and opens no sound card. What the
product actually *is* comes from the application, which injects a backend.
Sound between products is the application's too: it hands the facade a
factory for the streaming side, used when the app expands the product to a
listener (`--beolink`), and something to call when the app asks the product
to join or leave another one (`facade.set_beolink_in`).

A `ProductProfile` says which product this is: the type the app renders, the
sources, the menu the app walks, and any route only that product answers. The
default is a plain speaker. An application passes its own:

    from beo.mozart import facade
    facade.run(profile=MyProduct(), ml_backend=backend)

The tests here cover the profile catalogue. The suite that guards what
the app sees lives with the application that owns the only verified profile,
[`apps/beo-adapter`](../../apps/beo-adapter): a recorded answer per route, and a named test per bug that
actually happened against the real app, so a failure says what breaks rather
than only that the JSON moved.
