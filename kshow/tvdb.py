"""Minimal TVDB v4 API client."""

import logging
import re
from dataclasses import dataclass
from datetime import date
from urllib.parse import quote, urlsplit

import httpx

log = logging.getLogger(__name__)

BASE_URL = "https://api4.thetvdb.com/v4"
MAX_EPISODE_PAGES = 20
_SLUG_URL = re.compile(r"/series/([^/?#]+)")


class TvdbError(Exception):
    """Any TVDB failure: transport error, login failure or unexpected status."""


# Hosts TVDB serves artwork from. Poster URLs are loaded by every viewer's browser, so only
# these exact hosts are accepted, whether the URL came from TVDB or from a submitted form.
IMAGE_HOSTS = frozenset({"artworks.thetvdb.com"})
MAX_IMAGE_URL_LEN = 1000


def safe_image_url(url: str | None) -> str | None:
    """url when it is a plain https:// TVDB artwork address, else None."""
    if not isinstance(url, str) or not url or len(url) > MAX_IMAGE_URL_LEN:
        return None
    if any(ch.isspace() or ord(ch) < 32 or ch == "\\" for ch in url):
        return None
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if (
        parts.scheme != "https"
        or "@" in parts.netloc
        or port is not None
        or parts.netloc.endswith(":")
        or parts.hostname not in IMAGE_HOSTS
    ):
        return None
    return url


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
    season: int = 0


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

    async def all_episodes(self, series_id: int) -> list[TvdbEpisode]:
        """Every episode of every real season (specials, season 0, are skipped)."""
        episodes: list[TvdbEpisode] = []
        for page in range(MAX_EPISODE_PAGES):
            resp = await self._get(f"/series/{series_id}/episodes/default", {"page": page})
            if resp.status_code == 404:
                raise TvdbError(f"TVDB series {series_id} not found")
            eps = self._data(resp).get("episodes") or []
            if not isinstance(eps, list):
                raise TvdbError("TVDB episodes is not a list")
            for ep in eps:
                if not isinstance(ep, dict):
                    continue
                season, number = ep.get("seasonNumber"), ep.get("number")
                if not isinstance(season, int) or not season or number is None:
                    continue
                aired = None
                if isinstance(ep.get("aired"), str):
                    try:
                        aired = date.fromisoformat(ep["aired"])
                    except ValueError:
                        log.warning("Bad aired date %r for series %s", ep["aired"], series_id)
                episodes.append(TvdbEpisode(number=number, aired=aired, season=season))
            links = resp.json().get("links")
            if not (isinstance(links, dict) and links.get("next")):
                break
        return episodes

    async def is_ended(self, series_id: int) -> bool:
        resp = await self._get(f"/series/{series_id}")
        if resp.status_code == 404:
            raise TvdbError(f"TVDB series {series_id} not found")
        status = self._data(resp).get("status")
        return isinstance(status, dict) and status.get("name") == "Ended"
