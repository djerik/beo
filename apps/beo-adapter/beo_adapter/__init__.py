"""beo-adapter: a Raspberry Pi as a product the Bang & Olufsen app accepts.

    main             the service entry point
    machines         what can sit behind the product, and what the app sees for each
    ml_bridge        a music system on the Master Link bus, whichever it is
    classics_adapter_profile
                     the one disc player the app can draw, in front of every machine
    streaming        this product plays to other products
    listening        another product plays to this one, and into the machine
    speaker          a sound card that plays to a speaker directly

The protocols live in the beo packages. This application chooses which of them
to wire together, from the configuration.
"""
