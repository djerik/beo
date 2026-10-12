from __future__ import annotations

import threading
from typing import Callable, List, Optional

import pytest

from beo.masterlink.bus import MlBus, MlTransport
from beo.masterlink.codec import Telegram


class FakeTransport(MlTransport):
    """Records what is sent; `responder(sent) -> [Telegram]` answers asynchronously."""

    def __init__(self, responder: Optional[Callable[[Telegram], List[Telegram]]] = None, delay: float = 0.01):
        self.sent: List[Telegram] = []
        self.responder = responder
        self.delay = delay
        self._on = None

    def start(self, on_telegram):
        self._on = on_telegram

    def inject(self, tg: Telegram, delay: Optional[float] = None) -> None:
        d = self.delay if delay is None else delay
        threading.Timer(d, self._on, args=(tg,)).start()

    def send(self, telegram: Telegram) -> None:
        self.sent.append(telegram)
        if self.responder:
            for r in self.responder(telegram) or []:
                self.inject(r)


@pytest.fixture
def fake_bus():
    tr = FakeTransport()
    bus = MlBus(tr, press_gap_s=0.0, send_gap_s=0.0)
    bus.start()
    yield bus, tr
    bus.close()

