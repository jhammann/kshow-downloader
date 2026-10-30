"""Apprise API notifier. Never raises."""

import logging

import httpx

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, apprise_url: str | None, tag: str | None, http: httpx.AsyncClient):
        self._url = apprise_url
        self._tag = tag
        self._http = http

    async def send(self, title: str, body: str) -> None:
        if not self._url:
            return
        payload = {"title": title, "body": body}
        if self._tag:
            payload["tag"] = self._tag
        try:
            resp = await self._http.post(self._url, json=payload)
            if resp.status_code >= 300:
                log.warning("Apprise notification failed: HTTP %s", resp.status_code)
        except Exception as exc:  # notifications must never break callers
            log.warning("Apprise notification failed: %r", exc)
