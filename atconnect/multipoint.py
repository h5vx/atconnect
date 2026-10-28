"""Multipoint helpers: tracking which devices are linked to the headphones."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass

from .client import RtkClient
from .protocol import LinkEvent, LinkProfile, Packet, parse_link_event


@dataclass
class Link:
    address: str
    profiles: LinkProfile
    is_this_computer: bool = False

    @property
    def connected(self) -> bool:
        return bool(self.profiles)


class LinkTracker:
    """Follows PROFILE_(DIS)CONNECTED events to know which hosts use the headphones.

    The headphones only report changes, so devices that were already connected
    before we started listening show up once they connect or disconnect a profile.
    """

    def __init__(self, client: RtkClient, own_address: str | None = None) -> None:
        self._lock = threading.Lock()
        self._links: dict[str, Link] = {}
        self.own_address = own_address.upper() if own_address else None
        if self.own_address:
            # We are talking to the headphones over SPP, so this computer is linked.
            self._links[self.own_address] = Link(self.own_address, LinkProfile.SPP, True)
        self.on_change: list[Callable[[LinkEvent, Link], None]] = []
        client.on_packet.append(self._on_packet)

    def _on_packet(self, pkt: Packet) -> None:
        ev = parse_link_event(pkt)
        if ev is None:
            return
        with self._lock:
            link = self._links.get(ev.address)
            if link is None:
                link = self._links[ev.address] = Link(ev.address, LinkProfile(0), ev.address == self.own_address)
            if ev.connected:
                link.profiles |= ev.profile
            else:
                link.profiles &= ~ev.profile
        for cb in self.on_change:
            cb(ev, link)

    @property
    def links(self) -> list[Link]:
        with self._lock:
            return sorted(self._links.values(), key=lambda link: (not link.is_this_computer, link.address))

    @property
    def other_devices(self) -> list[Link]:
        return [link for link in self.links if not link.is_this_computer]


def describe_profiles(profiles: LinkProfile) -> str:
    names = [p.name for p in LinkProfile if p in profiles and p.name]
    return "+".join(names) if names else "none"
