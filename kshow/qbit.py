"""Minimal qBittorrent Web API v2 client."""

import logging
import re
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

_HASH = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$", re.IGNORECASE)


class QbitError(Exception):
    """Any qBittorrent failure: transport, auth, non-2xx or malformed response."""


@dataclass
class QbitTorrent:
    hash: str
    tags: list[str]
    progress: float
    state: str
    save_path: str


@dataclass
class QbitFile:
    name: str
    size: int


class QbitClient:
    def __init__(self, url: str, username: str, password: str, http: httpx.AsyncClient):
        self._url = url.rstrip("/")
        self._username = username
        self._password = password
        self._http = http
        # Kept privately and sent per request so it never lives in the shared jar.
        self._sid: str | None = None

    async def _send(self, method: str, path: str, **kwargs) -> httpx.Response:
        headers = {"Referer": self._url}
        if self._sid:
            headers["Cookie"] = f"SID={self._sid}"
        try:
            return await self._http.request(
                method, f"{self._url}/api/v2{path}", headers=headers, **kwargs
            )
        except httpx.HTTPError as exc:
            raise QbitError(f"qBittorrent request {path} failed: {exc!r}") from exc

    async def _login(self) -> None:
        self._sid = None
        resp = await self._send(
            "POST",
            "/auth/login",
            data={"username": self._username, "password": self._password},
        )
        if resp.status_code != 200 or resp.text.strip() != "Ok.":
            raise QbitError("qBittorrent login failed")
        sid = resp.cookies.get("SID")
        if not sid:
            raise QbitError("qBittorrent login returned no SID cookie")
        self._sid = sid
        # httpx stores Set-Cookie in the shared jar; drop it so it stays private.
        for cookie in list(self._http.cookies.jar):
            if cookie.name == "SID":
                self._http.cookies.jar.clear(cookie.domain, cookie.path, cookie.name)

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        if self._sid is None:
            await self._login()
        resp = await self._send(method, path, **kwargs)
        if resp.status_code == 403:
            await self._login()
            resp = await self._send(method, path, **kwargs)
        if not resp.is_success:
            raise QbitError(f"qBittorrent {path} returned HTTP {resp.status_code}")
        return resp

    @staticmethod
    def _check_hash(hash: str) -> None:
        # qBittorrent treats "all" and "a|b" specially; only accept a single real hash.
        if not isinstance(hash, str) or not _HASH.fullmatch(hash):
            raise QbitError(f"invalid torrent hash: {hash!r}")

    @staticmethod
    def _check_category(category: str) -> None:
        # An empty category means "uncategorised" in qBittorrent, which the app does not own.
        if not category or not category.strip():
            raise QbitError("category must not be empty")

    @staticmethod
    def _json(resp: httpx.Response, path: str):
        try:
            return resp.json()
        except ValueError as exc:
            raise QbitError(f"qBittorrent {path} returned malformed JSON") from exc

    async def add_torrent(
        self, data: bytes, filename: str, category: str, save_path: str, tag: str
    ) -> None:
        self._check_category(category)
        resp = await self._request(
            "POST",
            "/torrents/add",
            files={"torrents": (filename, data, "application/x-bittorrent")},
            data={"category": category, "savepath": save_path, "tags": tag},
        )
        if resp.text.strip() != "Ok.":
            raise QbitError(f"qBittorrent rejected torrent: {resp.text.strip()[:200]!r}")

    async def torrents(self, category: str) -> list[QbitTorrent]:
        self._check_category(category)
        resp = await self._request("GET", "/torrents/info", params={"category": category})
        try:
            return [
                QbitTorrent(
                    hash=item["hash"],
                    tags=[t.strip() for t in item["tags"].split(",") if t.strip()],
                    progress=float(item["progress"]),
                    state=item["state"],
                    save_path=item["save_path"],
                )
                for item in self._json(resp, "/torrents/info")
            ]
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise QbitError("qBittorrent /torrents/info returned unexpected data") from exc

    async def files(self, hash: str) -> list[QbitFile]:
        self._check_hash(hash)
        resp = await self._request("GET", "/torrents/files", params={"hash": hash})
        try:
            return [
                QbitFile(name=item["name"], size=int(item["size"]))
                for item in self._json(resp, "/torrents/files")
            ]
        except (KeyError, TypeError, ValueError) as exc:
            raise QbitError("qBittorrent /torrents/files returned unexpected data") from exc

    async def delete(self, hash: str, delete_files: bool) -> None:
        self._check_hash(hash)
        await self._request(
            "POST",
            "/torrents/delete",
            data={"hashes": hash, "deleteFiles": "true" if delete_files else "false"},
        )
