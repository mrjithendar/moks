"""A small, bounded RESP2 client for the Redis connector.

MOKS needs a handful of read commands (INFO, SLOWLOG, LATENCY, CLIENT LIST, SCAN, MEMORY USAGE,
CLUSTER INFO/NODES, ROLE, SENTINEL get-master-addr-by-name), so it speaks the protocol itself instead of
pulling in a client library. Every reply is size-capped and every read has a deadline, and the client never
pipelines or subscribes: one request, one reply.

Works against Redis and Valkey (both speak RESP2 after AUTH; HELLO is never sent).
"""
from __future__ import annotations

import asyncio
import ssl
from typing import Any

from .base import ConnectorError

MAX_REPLY_BYTES = 8_000_000
MAX_ELEMENTS = 100_000


class RespError(Exception):
    """An error reply (-ERR …) from the server."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message
        self.code = message.split(" ", 1)[0] if message else ""


def encode(*parts: str | bytes | int) -> bytes:
    out = [b"*%d\r\n" % len(parts)]
    for p in parts:
        b = p if isinstance(p, bytes) else str(p).encode()
        out.append(b"$%d\r\n%s\r\n" % (len(b), b))
    return b"".join(out)


class _Reader:
    def __init__(self, stream: asyncio.StreamReader, limit: int):
        self.stream = stream
        self.left = limit
        self.elements = 0

    async def _line(self) -> bytes:
        line = await self.stream.readuntil(b"\r\n")
        self.left -= len(line)
        if self.left < 0:
            raise ConnectorError("too_large", f"redis reply exceeded {MAX_REPLY_BYTES} bytes; narrow the request")
        return line[:-2]

    async def read(self) -> Any:
        line = await self._line()
        if not line:
            raise ConnectorError("unavailable", "redis sent an empty reply line", retryable=True)
        kind, rest = line[:1], line[1:]
        if kind == b"+":
            return rest.decode("utf-8", "replace")
        if kind == b"-":
            return RespError(rest.decode("utf-8", "replace"))
        if kind == b":":
            return int(rest)
        if kind == b"$":
            n = int(rest)
            if n < 0:
                return None
            self.left -= n + 2
            if self.left < 0:
                raise ConnectorError("too_large", f"redis reply exceeded {MAX_REPLY_BYTES} bytes; narrow the request")
            data = await self.stream.readexactly(n + 2)
            return data[:-2].decode("utf-8", "replace")
        if kind == b"*":
            n = int(rest)
            if n < 0:
                return None
            self.elements += n
            if self.elements > MAX_ELEMENTS:
                raise ConnectorError("too_large", "redis reply has too many elements; narrow the request")
            return [await self.read() for _ in range(n)]
        raise ConnectorError("bad_request", f"unexpected redis reply type {kind!r}")


class RespConnection:
    """One TCP (or TLS) connection. Not safe for concurrent use: the connector serialises calls."""

    def __init__(self, host: str, port: int, *, timeout: float = 10.0, tls: bool = False, verify_tls: bool = True,
                 ca_file: str | None = None, server_hostname: str | None = None):
        self.host, self.port, self.timeout = host, port, timeout
        self.tls, self.verify_tls, self.ca_file = tls, verify_tls, ca_file
        self.server_hostname = server_hostname
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None

    def _ssl(self) -> ssl.SSLContext | None:
        if not self.tls:
            return None
        ctx = ssl.create_default_context(cafile=self.ca_file) if self.ca_file else ssl.create_default_context()
        if not self.verify_tls:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx

    async def connect(self) -> None:
        try:
            self.reader, self.writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port, ssl=self._ssl(), server_hostname=(self.server_hostname or self.host) if self.tls else None,
                                        limit=1 << 20),
                timeout=min(5.0, self.timeout))
        except asyncio.TimeoutError:
            raise ConnectorError("timeout", f"redis at {self.host}:{self.port} did not accept a connection in time", retryable=True) from None
        except ssl.SSLError as exc:
            raise ConnectorError("config", f"TLS handshake with redis at {self.host}:{self.port} failed ({exc.reason or type(exc).__name__})") from None
        except OSError as exc:
            raise ConnectorError("unavailable", f"cannot reach redis at {self.host}:{self.port} ({type(exc).__name__})", retryable=True) from None

    @property
    def connected(self) -> bool:
        return self.writer is not None and not self.writer.is_closing()

    async def close(self) -> None:
        if self.writer is not None:
            self.writer.close()
            try:
                await self.writer.wait_closed()
            except Exception:
                pass
        self.reader = self.writer = None

    async def call(self, *parts: str | bytes | int) -> Any:
        """Send one command and return its reply. Error replies come back as RespError, not raised."""
        if not self.connected:
            await self.connect()
        assert self.reader is not None and self.writer is not None
        try:
            self.writer.write(encode(*parts))
            await asyncio.wait_for(self.writer.drain(), timeout=self.timeout)
            return await asyncio.wait_for(_Reader(self.reader, MAX_REPLY_BYTES).read(), timeout=self.timeout)
        except asyncio.TimeoutError:
            await self.close()  # the reply may still arrive; never read it as the answer to the next command
            raise ConnectorError("timeout", f"redis did not answer {str(parts[0]).upper()} within {self.timeout:.0f}s", retryable=True) from None
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.LimitOverrunError, ValueError) as exc:
            await self.close()
            raise ConnectorError("unavailable", f"redis connection to {self.host}:{self.port} failed ({type(exc).__name__})", retryable=True) from None
        except ConnectorError:
            await self.close()
            raise


def raise_for(reply: Any, command: str) -> Any:
    """Turn an error reply into a ConnectorError with a kind the UI can act on."""
    if not isinstance(reply, RespError):
        return reply
    msg = reply.message
    if reply.code in ("NOAUTH", "WRONGPASS") or "AUTH" in reply.code:
        raise ConnectorError("auth", f"redis rejected the credentials ({reply.code})")
    if reply.code == "NOPERM":
        raise ConnectorError("forbidden", f"the redis user may not run {command}: grant it in the ACL (docs/DATASTORES.md)")
    if reply.code == "LOADING":
        raise ConnectorError("unavailable", "redis is loading its dataset into memory", retryable=True)
    if "unknown command" in msg.lower():
        raise ConnectorError("bad_request", f"this server does not support {command}")
    raise ConnectorError("bad_request", f"redis {command}: {msg[:300]}")


def parse_info(text: str) -> dict[str, dict[str, Any]]:
    """INFO output -> {section: {key: value}}; numbers become numbers, `a=1,b=2` values become dicts."""
    out: dict[str, dict[str, Any]] = {}
    section = "default"
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            section = line[1:].strip().lower()
            out.setdefault(section, {})
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        out.setdefault(section, {})[key] = _value(value)
    return out


def _value(v: str) -> Any:
    if "=" in v and "," in v or (v.count("=") == 1 and not v.startswith("=")):
        parts = {}
        for item in v.split(","):
            if "=" in item:
                k, val = item.split("=", 1)
                parts[k] = _scalar(val)
        if parts:
            return parts
    return _scalar(v)


def _scalar(v: str) -> Any:
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        return v


def parse_client_list(text: str) -> list[dict[str, Any]]:
    rows = []
    for line in (text or "").splitlines():
        row: dict[str, Any] = {}
        for item in line.split(" "):
            if "=" in item:
                k, v = item.split("=", 1)
                row[k] = _scalar(v) if k in ("id", "age", "idle", "db", "sub", "psub", "multi", "qbuf", "omem", "tot-mem") else v
        if row:
            rows.append(row)
    return rows


def parse_cluster_nodes(text: str) -> list[dict[str, Any]]:
    """CLUSTER NODES: id addr flags master ping pong epoch link slots..."""
    rows = []
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) < 8:
            continue
        addr = parts[1].split("@", 1)[0]
        host, _, port = addr.rpartition(":")
        flags = parts[2].split(",")
        rows.append({"id": parts[0][:12], "host": host.split(",")[0], "port": int(port) if port.isdigit() else None, "flags": flags,
                     "role": "master" if "master" in flags else "replica" if "slave" in flags else "unknown",
                     "master_id": None if parts[3] == "-" else parts[3][:12], "link": parts[7],
                     "failed": "fail" in flags or "fail?" in flags, "slots": parts[8:][:20]})
    return rows
