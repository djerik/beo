"""Roles on top of a beocore session: SourceRole (we play to others) and
ListenerRole (a product plays to us).
"""
from .listener import ListenerRole, Listening
from .source import Listener, SourceRole

__all__ = ["SourceRole", "Listener", "ListenerRole", "Listening"]
