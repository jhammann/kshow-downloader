import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from urllib.parse import urlencode, urljoin

import httpx
from selectolax.lexbor import LexborHTMLParser as HTMLParser, LexborNode as Node
from sqlalchemy import select
from sqlalchemy.orm import Session

from kshow.config import Settings
from kshow.db import SourceRow
from kshow.releases import Release, parse_release_name
from kshow.sources.http import SiteClient, SourceUnreachable

log = logging.getLogger(__name__)

__all__ = ["Candidate", "SourceUnreachable", "TorrenttipSource", "build_sources", "is_torrent"]


@dataclass(frozen=True)
class Candidate:
    release: Release
    post_url: str
    posted: date | None
    source: str


def is_torrent(data: bytes) -> bool:
    return data.startswith(b"d") and b"4:info" in data


def _posted_date(link: Node) -> date | None:
    """Date from the `YY/MM/DD` cell next to the link in the main results list."""
    li = link.parent
    while li is not None and li.tag != "li":
        li = li.parent
    if li is None:
        return None
    cell = li.css_first("div.text-right")
    if cell is None:
        return None
    try:
        yy, mm, dd = (int(p) for p in cell.text(strip=True).split("/"))
        return date(2000 + yy, mm, dd)
    except ValueError:
        return None


class TorrenttipSource:
    def __init__(self, client: SiteClient, name: str = "torrenttip"):
        self._client = client
        self.name = name

    async def search(self, query: str, page: int = 1) -> list[Candidate]:
        params: dict[str, str | int] = {"q": query}
        if page > 1:
            params["page"] = page
        final_url, body, _ = await self._client.get(f"/search?{urlencode(params)}")
        found: dict[str, Candidate] = {}
        for link in HTMLParser(body.decode("utf-8", errors="replace")).css('a[href*="/t/"][title]'):
            release = parse_release_name(link.attributes["title"] or "")
            if release is None:
                continue
            post_url = urljoin(final_url, link.attributes["href"] or "")
            posted = _posted_date(link)
            existing = found.get(post_url)
            if existing is None or (existing.posted is None and posted is not None):
                found[post_url] = Candidate(release, post_url, posted, self.name)
        return list(found.values())

    async def fetch_torrent(self, post_url: str) -> bytes:
        if not self._client.is_site_url(post_url):
            log.warning("refusing post URL outside the site domain: %s", post_url)
            raise SourceUnreachable(f"post URL {post_url!r} is not on the source domain")
        final_url, body, _ = await self._client.get(post_url)
        link = HTMLParser(body.decode("utf-8", errors="replace")).css_first('a[href*="/download/"]')
        if link is None:
            raise ValueError(f"no download link on {post_url}")
        download_url = urljoin(final_url, link.attributes["href"] or "")
        data = await self._client.get_download(download_url, referer=post_url)
        if not is_torrent(data):
            raise ValueError(f"{download_url} did not return a torrent")
        return data


def build_sources(
    session: Session, http: httpx.AsyncClient, settings: Settings
) -> list[TorrenttipSource]:
    rows = session.scalars(
        select(SourceRow).where(SourceRow.enabled.is_(True)).order_by(SourceRow.priority)
    ).all()
    sources = []
    for row in rows:
        if row.kind != "torrenttip":
            log.warning("skipping source %r: unsupported kind %r", row.name, row.kind)
            continue

        def persist(new_url: str, row: SourceRow = row) -> None:
            row.current_url = new_url
            session.commit()

        client = SiteClient(row, http, settings.flaresolverr_url, persist)
        sources.append(TorrenttipSource(client, row.name))
    return sources
