"""Minimal TVDB v4 API client."""

import logging
import re
from dataclasses import dataclass
from datetime import date
from urllib.parse import quote

import httpx

log = logging.getLogger(__name__)

BASE_URL = "https://api4.thetvdb.com/v4"
_SLUG_URL = re.compile(r"/series/([^/?#]+)")


class TvdbError(Exception):
    """Any TVDB failure: transport error, login failure or unexpected status."""


def safe_image_url(url: str | None) -> str | None:
    """url when it is an https:// address safe to put in an <img src>, else None."""
    return url if isinstance(url, str) and url.startswith("https://") else None


def _eng(translations) -> str | None:
    return translations.get("eng") if isinstance(translations, dict) else None


@dataclass
class TvdbMatch:
    id: int
    name: str
    english_name: str | None
    year: str | None
    network: str | None
    image_url: str | None


@dataclass
class TvdbEpisode:
    number: int
    aired: date | None


class TvdbClient:
    def __init__(self, api_key: str, pin: str | None, http: httpx.AsyncClient):
        self._api_key = api_key
        self._pin = pin
        self._http = http
        self._token: str | None = None

    async def _login(self) -> str:
        body = {"apikey": self._api_key}
        if self._pin:
            body["pin"] = self._pin
        try:
            resp = await self._http.post(f"{BASE_URL}/login", json=body)
        except httpx.HTTPError as exc:
            raise TvdbError(f"TVDB login failed: {exc!r}") from exc
        if resp.status_code != 200:
            raise TvdbError(f"TVDB login failed: HTTP {resp.status_code}")
        try:
            self._token = resp.json()["data"]["token"]
        except (ValueError, KeyError, TypeError) as exc:
            raise TvdbError("TVDB login returned no token") from exc
        log.info("TVDB login ok")
        return self._token

    async def _get(self, path: str, params: dict | None = None) -> httpx.Response:
        """GET with bearer auth; one re-login on 401. Returns 2xx or 404 responses."""
        for attempt in (1, 2):
            token = self._token or await self._login()
            try:
                resp = await self._http.get(
                    f"{BASE_URL}{path}",
                    params=params,
                    headers={"Authorization": f"Bearer {token}"},
                )
            except httpx.HTTPError as exc:
                raise TvdbError(f"TVDB request {path} failed: {exc!r}") from exc
            if resp.status_code == 401 and attempt == 1:
                log.info("TVDB token rejected, logging in again")
                self._token = None
                continue
            if resp.status_code == 404:
                return resp
            if not resp.is_success:
                raise TvdbError(f"TVDB {path} returned HTTP {resp.status_code}")
            return resp
        raise TvdbError(f"TVDB {path} returned HTTP 401")  # pragma: no cover

    def _data(self, resp: httpx.Response, expect: type = dict):
        try:
            data = resp.json()["data"]
        except (ValueError, KeyError, TypeError) as exc:
            raise TvdbError("TVDB returned an unexpected response body") from exc
        if not isinstance(data, expect):
            raise TvdbError(f"TVDB returned unexpected data type {type(data).__name__}")
        return data

    async def search(self, query: str) -> list[TvdbMatch]:
        resp = await self._get("/search", {"query": query, "type": "series"})
        results = self._data(resp, list)
        matches = []
        for item in results[:8]:
            if not isinstance(item, dict):
                continue
            try:
                tvdb_id = int(item["tvdb_id"])
            except (KeyError, ValueError, TypeError):
                continue
            matches.append(
                TvdbMatch(
                    id=tvdb_id,
                    name=item.get("name") or "",
                    english_name=_eng(item.get("translations")),
                    year=item.get("year"),
                    network=item.get("network"),
                    image_url=item.get("image_url"),
                )
            )
        return matches

    async def lookup(self, ref: str) -> TvdbMatch | None:
        ref = ref.strip()
        if ref.isdigit():
            path = f"/series/{int(ref)}"
        else:
            m = _SLUG_URL.search(ref)
            if not m:
                return None
            seg = m.group(1)
            path = f"/series/{int(seg)}" if seg.isdigit() else f"/series/slug/{quote(seg)}"
        resp = await self._get(path)
        if resp.status_code == 404:
            return None
        d = self._data(resp)
        try:
            series_id = int(d["id"])
        except (KeyError, ValueError, TypeError) as exc:
            raise TvdbError("TVDB series has no valid id") from exc
        return TvdbMatch(
            id=series_id,
            name=d.get("name") or "",
            english_name=None,
            year=d.get("year"),
            network=None,
            image_url=d.get("image"),
        )

    async def english_name(self, series_id: int) -> str | None:
        resp = await self._get(f"/series/{series_id}/translations/eng")
        if resp.status_code == 404:
            return None
        name = self._data(resp).get("name")
        return name if isinstance(name, str) and name else None

    async def season_episodes(self, series_id: int, season: int) -> list[TvdbEpisode]:
        resp = await self._get(
            f"/series/{series_id}/episodes/default", {"season": season, "page": 0}
        )
        if resp.status_code == 404:
            raise TvdbError(f"TVDB series {series_id} not found")
        episodes = []
        eps = self._data(resp).get("episodes") or []
        if not isinstance(eps, list):
            raise TvdbError("TVDB episodes is not a list")
        for ep in eps:
            if not isinstance(ep, dict):
                continue
            if ep.get("seasonNumber") != season or ep.get("number") is None:
                continue
            aired = None
            if isinstance(ep.get("aired"), str):
                try:
                    aired = date.fromisoformat(ep["aired"])
                except ValueError:
                    log.warning("Bad aired date %r for series %s", ep["aired"], series_id)
            episodes.append(TvdbEpisode(number=ep["number"], aired=aired))
        return episodes

    async def is_ended(self, series_id: int) -> bool:
        resp = await self._get(f"/series/{series_id}")
        if resp.status_code == 404:
            raise TvdbError(f"TVDB series {series_id} not found")
        status = self._data(resp).get("status")
        return isinstance(status, dict) and status.get("name") == "Ended"
