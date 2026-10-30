import asyncio
import ipaddress
import logging
import re
import socket
from collections.abc import Callable
from urllib.parse import urljoin, urlsplit

import httpx

from kshow.db import SourceRow

log = logging.getLogger(__name__)

PROBE_RANGE = 10
DEFAULT_UA = "Mozilla/5.0"


MAX_DOWNLOAD_HOPS = 5


class SourceUnreachable(Exception):
    pass


class _OffPattern(SourceUnreachable):
    """A site request ended on a host that does not match the domain template."""


async def _resolve_host(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


async def _ensure_public_url(url: str) -> None:
    """Raise SourceUnreachable unless url is http(s) and its host is only public addresses."""
    parts = urlsplit(url)
    host = parts.hostname
    if parts.scheme not in ("http", "https") or not host:
        raise SourceUnreachable(f"refusing to fetch {url!r}: not an http(s) URL")
    try:
        addrs = [host]
        ipaddress.ip_address(host)
    except ValueError:
        try:
            addrs = await _resolve_host(host, parts.port or (443 if parts.scheme == "https" else 80))
        except OSError as e:
            raise SourceUnreachable(f"cannot resolve {host}: {e!r}") from e
    for addr in addrs:
        ip = ipaddress.ip_address(addr.split("%")[0])
        if not ip.is_global:
            log.warning("refusing to fetch %s: %s is not a public address", url, ip)
            raise SourceUnreachable(f"refusing to fetch {url!r}: non-public address")


def _base_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _is_challenge(resp: httpx.Response) -> bool:
    if resp.status_code not in (403, 503):
        return False
    if resp.headers.get("cf-mitigated") == "challenge":
        return True
    return "Just a moment..." in resp.text


class SiteClient:
    """HTTP access to one source site: follows domain changes, solves Cloudflare via FlareSolverr."""

    def __init__(
        self,
        row: SourceRow,
        http: httpx.AsyncClient,
        flaresolverr_url: str | None,
        on_url_change: Callable[[str], None],
    ):
        self._template = row.domain_template
        self._pattern = re.compile(
            "^" + re.escape(row.domain_template.rstrip("/")).replace(re.escape("{N}"), r"\d+") + "$"
        )
        self._base = row.current_url.rstrip("/")
        self._http = http
        self._flaresolverr_url = flaresolverr_url.rstrip("/") if flaresolverr_url else None
        self._on_url_change = on_url_change
        self._user_agent = DEFAULT_UA
        self._cookies: dict[str, dict[str, str]] = {}  # host -> {name: value}

    async def get(
        self, path_or_url: str, referer: str | None = None
    ) -> tuple[str, bytes, str]:
        """Return (final_url, body, content_type). Relative paths use the live base URL."""
        try:
            if path_or_url.startswith(("http://", "https://")):
                return await self._fetch(path_or_url, referer)
            try:
                return await self._fetch(urljoin(self._base + "/", path_or_url), referer)
            except (httpx.TransportError, _OffPattern):
                await self._probe()
                return await self._fetch(urljoin(self._base + "/", path_or_url), referer)
        except httpx.TransportError as e:
            raise SourceUnreachable(f"{path_or_url} unreachable: {e!r}") from e

    async def resolve(self) -> str:
        """Return the live base URL, probing neighbouring domain numbers if the current one is dead."""
        try:
            await self._fetch(self._base + "/")
        except (httpx.TransportError, _OffPattern) as e:
            log.warning("%s unreachable (%r), probing", self._base, e)
            await self._probe()
        return self._base

    async def _probe(self) -> None:
        match = re.search(r"\d+", urlsplit(self._base).hostname or "")
        if "{N}" not in self._template or not match:
            raise SourceUnreachable(f"{self._base} unreachable and no domain number to probe")
        current = int(match.group())
        for n in range(current + 1, current + 1 + PROBE_RANGE):
            candidate = self._template.replace("{N}", str(n)).rstrip("/")
            try:
                await self._fetch(candidate + "/")
            except (httpx.TransportError, SourceUnreachable):
                continue
            log.info("domain moved to %s", self._base)
            return
        raise SourceUnreachable(
            f"{self._base} and next {PROBE_RANGE} domains unreachable"
        )

    async def _fetch(
        self, url: str, referer: str | None = None
    ) -> tuple[str, bytes, str]:
        host = urlsplit(url).netloc
        is_site = self.is_site_url(url)
        headers = self._headers(url, referer)
        resp = await self._http.get(url, headers=headers, follow_redirects=True)
        if _is_challenge(resp):
            final_url, body, ctype = await self._solve(url)
        elif resp.status_code >= 500:
            raise SourceUnreachable(f"{url} returned {resp.status_code}")
        else:
            final_url = str(resp.url)
            body = resp.content
            ctype = resp.headers.get("content-type", "")
        if is_site:
            final_base = _base_of(final_url)
            if not self.is_site_url(final_base):
                raise _OffPattern(f"{url} redirected off-site to {final_base}")
            self._note_base(final_base)
        return final_url, body, ctype

    def is_site_url(self, url: str) -> bool:
        return bool(self._pattern.match(_base_of(url)))

    def _headers(self, url: str, referer: str | None) -> dict[str, str]:
        headers = {"User-Agent": self._user_agent}
        if referer:
            headers["Referer"] = referer
        cookies = self._cookies.get(urlsplit(url).netloc)
        if cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
        return headers

    async def get_download(self, url: str, referer: str) -> bytes:
        """Fetch a scraped (untrusted) URL: public hosts only, redirects followed manually and re-checked."""
        try:
            for _ in range(MAX_DOWNLOAD_HOPS + 1):
                await _ensure_public_url(url)
                resp = await self._http.get(
                    url, headers=self._headers(url, referer), follow_redirects=False
                )
                location = resp.headers.get("location")
                if resp.is_redirect and location:
                    url = urljoin(url, location)
                    continue
                return resp.content
        except httpx.TransportError as e:
            raise SourceUnreachable(f"{url} unreachable: {e!r}") from e
        raise SourceUnreachable(f"too many redirects fetching {url}")

    def _note_base(self, new_base: str) -> None:
        if new_base != self._base:
            self._base = new_base
            self._on_url_change(new_base)

    async def _solve(self, url: str) -> tuple[str, bytes, str]:
        if not self._flaresolverr_url:
            raise SourceUnreachable(f"{url} is behind a Cloudflare challenge and FLARESOLVERR_URL is not set")
        log.info("Cloudflare challenge on %s, using FlareSolverr", url)
        try:
            resp = await self._http.post(
                f"{self._flaresolverr_url}/v1",
                json={"cmd": "request.get", "url": url, "maxTimeout": 60000},
                timeout=90,
            )
            data = resp.json()
            solution = data["solution"]
            html = solution["response"]
        except (httpx.HTTPError, ValueError, KeyError) as e:
            raise SourceUnreachable(f"FlareSolverr failed for {url}: {e!r}") from e
        self._user_agent = solution.get("userAgent") or self._user_agent
        for c in solution.get("cookies", []):
            domain = (c.get("domain") or urlsplit(url).netloc).lstrip(".")
            self._cookies.setdefault(domain, {})[c["name"]] = c["value"]
        # cookies are issued for the registrable domain; make them usable for the exact host
        host = urlsplit(url).netloc
        for domain, jar in list(self._cookies.items()):
            if host == domain or host.endswith("." + domain):
                self._cookies.setdefault(host, {}).update(jar)
        return solution.get("url") or url, html.encode(), "text/html"
