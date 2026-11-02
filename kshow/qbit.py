"""Minimal qBittorrent Web API v2 client."""

import logging
import re
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

_HASH = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$", re.IGNORECASE)


class QbitError(Exception):
    """Any qBittorrent failure: transport, auth, non-2xx or malformed response."""


class QbitRejected(QbitError):
    """qBittorrent answered but refused to add the torrent: it is up and running."""


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
        # qBittorrent 4.x replies 200 "Ok." (or "Fails."); 5.x replies 204 with no body (401 on
        # bad credentials). Either way a real login hands out a SID cookie, checked below.
        if not resp.is_success or resp.text.strip() not in ("Ok.", ""):
            raise QbitError(f"qBittorrent login failed (HTTP {resp.status_code})")
        sid = resp.cookies.get("SID")
        if not sid:
            raise QbitError("qBittorrent login returned no SID cookie")
        self._sid = sid
        # httpx stores Set-Cookie in the shared jar; drop it so it stays private.
        for cookie in list(self._http.cookies.jar):
            if cookie.name == "SID":
                self._http.cookies.jar.clear(cookie.domain, cookie.path, cookie.name)

    async def _request(
        self, method: str, path: str, allow: tuple[int, ...] = (), **kwargs
    ) -> httpx.Response:
        """Send with login; raise on a non-2xx status unless it is in allow."""
        if self._sid is None:
            await self._login()
        resp = await self._send(method, path, **kwargs)
        if resp.status_code == 403:
            await self._login()
            resp = await self._send(method, path, **kwargs)
        if not resp.is_success and resp.status_code not in allow:
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
            allow=(409,),
        )
        # Web API < 2.14 answers "Ok." / "Fails."; qBittorrent 5.2+ (API 2.14) answers
        # JSON counts, or HTTP 409 when nothing was added.
        text = resp.text.strip()
        if resp.status_code == 409:
            raise QbitRejected(f"qBittorrent rejected torrent (HTTP 409): {text[:200]!r}")
        if text == "Ok.":
            return
        if text == "Fails.":
            raise QbitRejected("qBittorrent rejected torrent: 'Fails.'")
        try:
            body = resp.json()
            success = int(body["success_count"]) + int(body["pending_count"])
            failures = int(body["failure_count"])
        except (ValueError, KeyError, TypeError):
            raise QbitError(f"qBittorrent add returned unexpected reply: {text[:200]!r}") from None
        if failures > 0 or success < 1:
            raise QbitRejected(f"qBittorrent rejected torrent: {text[:200]!r}")

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
