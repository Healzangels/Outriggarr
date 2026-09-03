"""Notifications via Apprise. The only module that imports apprise.

Events are Outriggarr's own — the *arr already announces imports. Sending never affects a
job: failures are logged and swallowed.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Protocol

log = logging.getLogger(__name__)


class Notifier(Protocol):
    def send(self, title: str, body: str) -> bool:
        """Blocking. True if at least one target accepted the message."""
        ...


def validate_apprise_urls(text: str) -> list[str]:
    """Split a settings blob (one URL per line or comma) and reject what Apprise won't take."""
    import apprise

    urls = [u.strip() for chunk in text.splitlines() for u in chunk.split(",") if u.strip()]
    bad = []
    for u in urls:
        probe = apprise.Apprise()
        if not probe.add(u):
            bad.append(u.split("://", 1)[0] + "://…" if "://" in u else u)
    if bad:
        raise ValueError(f"Apprise did not accept these URLs: {bad}")
    return urls


class AppriseNotifier:
    """Reads the URL list on every send so Settings edits apply without a restart."""

    def __init__(self, urls: Callable[[], list[str]]) -> None:
        self._urls = urls

    def send(self, title: str, body: str) -> bool:
        import apprise

        urls = self._urls()
        if not urls:
            return False
        # one Apprise per target: its notify() answers all-or-nothing, and one dead
        # Discord webhook must not read as "nothing delivered" when ntfy got it
        delivered = 0
        for u in urls:
            scheme = u.split("://", 1)[0]
            client = apprise.Apprise()
            if not client.add(u):
                log.warning("apprise rejected a configured URL (%s://…)", scheme)
                continue
            try:
                if client.notify(title=title, body=body):
                    delivered += 1
                else:
                    log.warning("notification not delivered via %s://…", scheme)
            except Exception as exc:  # apprise plugins raise all sorts; never let it out
                log.warning("notification via %s://… failed: %s", scheme, exc)
        if not delivered:
            log.warning("notification not delivered to any target (%d configured)", len(urls))
        return delivered > 0


class NullNotifier:
    def send(self, title: str, body: str) -> bool:
        return False
