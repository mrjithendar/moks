"""Public web pages for the knowledge base: fetching, and HTML to Markdown-like text.

MOKS fetches these URLs from inside the cluster, so a URL must never become a way to reach the
cluster, the VPC or the metadata service. Every hop is resolved, every address must be public, and
the request connects to the checked address, so a second DNS answer cannot change where it goes.
Redirects are followed by hand for the same reason. Links on pages are data: only a crawl follows
them, on the same host and under the path of a listed URL, and it honours robots.txt.
"""
from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from collections.abc import Awaitable, Callable
from html.parser import HTMLParser
from urllib.parse import urldefrag, urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from ..connectors.base import ConnectorError

USER_AGENT = "MOKS-knowledge/1.0"
MAX_BYTES = 2_000_000
MAX_REDIRECTS = 5
TIMEOUT_SECONDS = 15.0
PORTS = {None, 80, 443}
PAGE_TYPES = ("text/html", "application/xhtml+xml", "text/plain", "text/markdown")
NOT_PAGES = re.compile(r"\.(pdf|zip|gz|tar|tgz|png|jpe?g|gif|svg|webp|ico|mp4|mp3|css|js|woff2?|ttf|exe|dmg|deb|rpm|iso)$", re.I)

Resolver = Callable[[str], Awaitable[list[str]]]


class PageGone(Exception):
    """404 or 410: the one answer that proves a page no longer exists."""


def check_url(url: str) -> str:
    """Rejects what can be refused without touching the network, and drops the fragment."""
    clean = urldefrag(url.strip())[0]
    if len(clean) > 1000:
        raise ConnectorError("bad_request", "URL is longer than 1000 characters")
    parts = urlsplit(clean)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ConnectorError("bad_request", f"not an http(s) URL: {clean[:200]}")
    if parts.username or parts.password:
        raise ConnectorError("bad_request", "URLs cannot carry a username or password")
    if parts.port not in PORTS:
        raise ConnectorError("bad_request", "only the standard ports 80 and 443 are allowed")
    return clean


async def _system_resolver(host: str) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return [i[4][0] for i in infos]


async def public_address(host: str, resolver: Resolver | None = None) -> str:
    """Every answer must be public: one private address among public ones is how rebinding starts."""
    try:
        answers = await (resolver or _system_resolver)(host)
    except OSError:
        raise ConnectorError("unavailable", f"could not resolve {host}") from None
    addresses = [ipaddress.ip_address(a.split("%")[0]) for a in answers]
    if not addresses:
        raise ConnectorError("unavailable", f"could not resolve {host}")
    for a in addresses:
        mapped = getattr(a, "ipv4_mapped", None) or a
        if not mapped.is_global:
            raise ConnectorError("forbidden", f"{host} is not a public address")
    return str(addresses[0])


async def fetch(url: str, *, transport: httpx.AsyncBaseTransport | None = None, resolver: Resolver | None = None) -> tuple[str, str, str]:
    """Returns (final URL, content type, text), revalidating every redirect."""
    async with httpx.AsyncClient(transport=transport, timeout=TIMEOUT_SECONDS, follow_redirects=False, trust_env=False) as client:
        for _ in range(MAX_REDIRECTS + 1):
            url = check_url(url)
            parts = httpx.URL(url)
            address = await public_address(parts.host, resolver)
            request = client.build_request("GET", parts.copy_with(host=address), headers={"Host": parts.netloc.decode(), "User-Agent": USER_AGENT, "Accept": "text/html, text/plain;q=0.9"},
                                           extensions={"sni_hostname": parts.host})
            try:
                response = await client.send(request, stream=True)
            except httpx.TimeoutException:
                raise ConnectorError("timeout", f"timed out reading {url[:200]}") from None
            except httpx.HTTPError as exc:
                raise ConnectorError("unavailable", f"could not read {url[:200]}: {type(exc).__name__}") from None
            try:
                if response.status_code in (301, 302, 303, 307, 308) and response.headers.get("location"):
                    url = urljoin(url, response.headers["location"])
                    continue
                if response.status_code in (404, 410):
                    raise PageGone(url)
                if response.status_code != 200:
                    raise ConnectorError("unavailable", f"HTTP {response.status_code} from {url[:200]}")
                kind = response.headers.get("content-type", "").split(";")[0].strip().lower()
                if kind not in PAGE_TYPES:
                    raise ConnectorError("bad_request", f"{kind or 'unknown content'} is not a web page")
                body = b""
                async for block in response.aiter_bytes():
                    body += block
                    if len(body) > MAX_BYTES:
                        raise ConnectorError("too_large", f"page is larger than {MAX_BYTES // 1_000_000} MB")
                return url, kind, body.decode(response.encoding or "utf-8", errors="replace")
            finally:
                await response.aclose()
    raise ConnectorError("unavailable", f"too many redirects from {url[:200]}")


SKIPPED = {"script", "style", "noscript", "template", "svg", "canvas", "iframe", "object", "nav", "footer", "header", "aside", "form"}
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
BLOCKS = {"p", "div", "section", "article", "main", "br", "tr", "table", "ul", "ol", "dl", "dt", "dd", "blockquote", "figure", "figcaption"}
INVISIBLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.I)


class _Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.title = ""
        self.links: list[str] = []
        self._skip: tuple[str, int] | None = None
        self._in_title = self._pre = False

    def _hidden(self, tag: str, attrs: dict) -> bool:
        return tag in SKIPPED or "hidden" in attrs or attrs.get("aria-hidden") == "true" or bool(INVISIBLE.search(attrs.get("style") or ""))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if self._skip:
            if tag == self._skip[0]:
                self._skip = (tag, self._skip[1] + 1)
            return
        if tag not in VOID and self._hidden(tag, a):  # text a reader cannot see is not knowledge, and is where instructions to a model hide
            self._skip = (tag, 1)
            return
        if tag == "title":
            self._in_title = True
        elif tag == "a" and a.get("href"):
            self.links.append(a["href"])
        elif tag == "pre":
            self._pre = True
            self.out.append("\n```\n")
        elif re.fullmatch(r"h[1-6]", tag):
            self.out.append("\n\n" + "#" * int(tag[1]) + " ")
        elif tag == "li":
            self.out.append("\n- ")
        elif tag in BLOCKS:
            self.out.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if self._skip:
            if tag == self._skip[0]:
                self._skip = (tag, self._skip[1] - 1) if self._skip[1] > 1 else None
            return
        if tag == "title":
            self._in_title = False
        elif tag == "pre":
            self._pre = False
            self.out.append("\n```\n")
        elif re.fullmatch(r"h[1-6]", tag) or tag in BLOCKS:
            self.out.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        if self._in_title:
            self.title += data
        elif self._pre:
            self.out.append(data)
        else:
            self.out.append(re.sub(r"\s+", " ", data))


def read_html(html: str) -> tuple[str, str, list[str]]:
    """(title, Markdown-like text, raw hrefs)."""
    page = _Page()
    page.feed(html)
    page.close()
    text = re.sub(r"\n[ \t]+", "\n", "".join(page.out))
    return page.title.strip()[:500], re.sub(r"\n{3,}", "\n\n", text).strip(), page.links


def in_crawl(link: str, base: str, seeds: list[str]) -> str | None:
    """The page's link as a crawl target, or None: same host, under a listed URL's directory, a page."""
    try:
        url = check_url(urljoin(base, link))
    except (ConnectorError, ValueError):
        return None
    parts = urlsplit(url)
    if NOT_PAGES.search(parts.path):
        return None
    for seed in seeds:
        s = urlsplit(seed)
        if parts.hostname == s.hostname and parts.path.startswith(s.path.rsplit("/", 1)[0] + "/"):
            return url
    return None


async def robots_for(url: str, *, transport=None, resolver=None) -> RobotFileParser:
    """A site with no readable robots.txt has stated no rules."""
    parts = urlsplit(url)
    rules = RobotFileParser()
    try:
        _, _, text = await fetch(f"{parts.scheme}://{parts.netloc}/robots.txt", transport=transport, resolver=resolver)
    except (ConnectorError, PageGone):
        rules.parse([])
    else:
        rules.parse(text.splitlines())
    return rules
