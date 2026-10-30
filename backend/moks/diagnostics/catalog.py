"""Approved diagnostic images, packages and command templates.

Sessions run prebuilt images. Extra packages come only from this catalog, pinned, installed
inside the disposable session pod (never on nodes or application containers). Python packages
install as the non-root user (pip --user); OS packages need an elevated session (root inside the
pod, still no privilege escalation, all capabilities dropped except those apk needs), which always
requires an approved action plan. Commands are fixed argv templates with validated arguments: there is no
shell, and none of this is offered to the LLM.
"""
from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from typing import Any, Callable

IMAGES: dict[str, dict[str, Any]] = {
    "netshoot": {"image": "nicolaka/netshoot:v0.16", "description": "curl, dig, nslookup, nc, openssl, jq, tcpdump-free network toolkit",
                 "tools": ["http_check", "http_get", "dns_lookup", "tcp_check", "tls_check", "jq_fetch"]},
    "curl": {"image": "curlimages/curl:8.23.0", "description": "minimal curl", "tools": ["http_check", "http_get"]},
    "python": {"image": "python:3.12-alpine", "description": "Python with pip --user for catalog packages",
               "tools": ["http_check_py"]},
}

PACKAGES: dict[str, dict[str, Any]] = {
    # name -> how to install (pinned) and whether root is needed
    "httpie": {"kind": "pip", "spec": "httpie==3.2.4", "images": ["python"], "elevated": False},
    "dnspython": {"kind": "pip", "spec": "dnspython==2.7.0", "images": ["python"], "elevated": False},
    "bind-tools": {"kind": "apk", "spec": "bind-tools", "images": ["python"], "elevated": True},
    "busybox-extras": {"kind": "apk", "spec": "busybox-extras", "images": ["python"], "elevated": True},
}

HOST = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9-]{0,62})(\.[A-Za-z0-9]([A-Za-z0-9-]{0,62}))*\.?$")
URL = re.compile(r"^https?://[A-Za-z0-9.-]{1,253}(:[0-9]{1,5})?(/[A-Za-z0-9._~%!$&'()*+,;=:@/?-]*)?$")
BLOCKED_NETS = [ipaddress.ip_network(n) for n in ("169.254.0.0/16", "fd00:ec2::254/128", "100.100.100.200/32", "0.0.0.0/8", "127.0.0.0/8")]
BLOCKED_HOSTS = {"metadata.google.internal", "metadata", "instance-data", "localhost"}


class ArgError(ValueError):
    pass


def _host(h: str) -> str:
    h = h.strip().lower().rstrip(".")
    if not HOST.match(h) and not _is_ip(h):
        raise ArgError(f"invalid host {h!r}")
    if h in BLOCKED_HOSTS:
        raise ArgError(f"{h} is blocked (metadata/local endpoints)")
    if _is_ip(h) and any(ipaddress.ip_address(h) in n for n in BLOCKED_NETS):
        raise ArgError(f"{h} is in a blocked range (link-local/metadata/loopback)")
    return h


def _is_ip(h: str) -> bool:
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        return False


def _url(u: str) -> str:
    if not URL.match(u):
        raise ArgError("URL must be http(s)://host[:port]/path without spaces or credentials")
    _host(re.sub(r"^https?://", "", u).split("/")[0].split(":")[0])
    return u


def _port(p: Any) -> str:
    try:
        n = int(p)
    except (TypeError, ValueError):
        raise ArgError("port must be a number") from None
    if not 1 <= n <= 65535:
        raise ArgError("port out of range")
    return str(n)


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    build: Callable[[dict], list[str]]
    args: tuple[str, ...]


TOOLS: dict[str, Tool] = {t.name: t for t in [
    Tool("http_check", "Status code, timings and remote address for a URL", lambda a: [
        "curl", "-sS", "-o", "/dev/null", "-m", "10", "-w",
        "status=%{http_code} remote=%{remote_ip}:%{remote_port} dns=%{time_namelookup}s connect=%{time_connect}s tls=%{time_appconnect}s total=%{time_total}s\n",
        _url(a["url"])], ("url",)),
    Tool("http_get", "First 16 KB of a response body with headers", lambda a: ["curl", "-sS", "-i", "-m", "10", "--max-filesize", "16384", _url(a["url"])], ("url",)),
    Tool("dns_lookup", "DNS records for a name", lambda a: ["dig", "+short", "+time=3", "+tries=1", _host(a["name"]),
                                                           (a.get("type") or "A").upper() if (a.get("type") or "A").upper() in ("A", "AAAA", "CNAME", "SRV", "TXT", "MX", "NS") else "A"],
         ("name", "type")),
    Tool("tcp_check", "Can a TCP connection be opened?", lambda a: ["nc", "-z", "-v", "-w", "3", _host(a["host"]), _port(a["port"])], ("host", "port")),
    Tool("tls_check", "TLS certificate subject, issuer and dates", lambda a: [
        "openssl", "s_client", "-connect", f"{_host(a['host'])}:{_port(a.get('port', 443))}", "-servername", _host(a["host"]), "-brief"],
         ("host", "port")),
    Tool("jq_fetch", "Fetch JSON and apply a jq path (identifiers and dots only)", lambda a: [
        "sh", "-c", 'curl -sS -m 10 "$1" | jq -r "$2"', "jq_fetch", _url(a["url"]), _jq(a.get("path", "."))], ("url", "path")),
    Tool("http_check_py", "HTTP status using Python (python image)", lambda a: [
        "python", "-c", "import sys,urllib.request as u; r=u.urlopen(sys.argv[1], timeout=10); print('status', r.status)", _url(a["url"])], ("url",)),
]}

JQ_PATH = re.compile(r"^\.([A-Za-z_][A-Za-z0-9_]*(\[[0-9]{1,4}\])?)?(\.[A-Za-z_][A-Za-z0-9_]*(\[[0-9]{1,4}\])?){0,10}$")


def _jq(p: str) -> str:
    if p != "." and not JQ_PATH.match(p):
        raise ArgError("jq path may only be .field.sub[0] style")
    return p


def build_argv(image_key: str, tool: str, args: dict[str, Any]) -> list[str]:
    if tool not in TOOLS:
        raise ArgError(f"unknown tool {tool!r}")
    if tool not in IMAGES[image_key]["tools"]:
        raise ArgError(f"{tool} is not available in the {image_key} image")
    extra = set(args) - set(TOOLS[tool].args)
    if extra:
        raise ArgError(f"unexpected arguments: {', '.join(sorted(extra))}")
    try:
        return TOOLS[tool].build(args)
    except KeyError as exc:
        raise ArgError(f"missing argument {exc.args[0]}") from None


def install_argv(packages: list[str], image_key: str) -> list[list[str]]:
    cmds = []
    pip = [PACKAGES[p]["spec"] for p in packages if PACKAGES[p]["kind"] == "pip"]
    apk = [PACKAGES[p]["spec"] for p in packages if PACKAGES[p]["kind"] == "apk"]
    if apk:
        cmds.append(["apk", "add", "--no-cache", *apk])
    if pip:
        cmds.append(["pip", "install", "--user", "--no-cache-dir", "--disable-pip-version-check", *pip])
    return cmds


def check_packages(packages: list[str], image_key: str) -> bool:
    """Returns whether the selection needs an elevated session; raises for unknown/unsuitable packages."""
    elevated = False
    for p in packages:
        spec = PACKAGES.get(p)
        if spec is None:
            raise ArgError(f"{p} is not in the approved package catalog")
        if image_key not in spec["images"]:
            raise ArgError(f"{p} cannot be installed in the {image_key} image")
        elevated |= spec["elevated"]
    return elevated
