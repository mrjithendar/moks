"""The terminal namespace, and nothing else: this client is refused anywhere outside it by the
gateway's Role, and by construction here because no method takes a namespace."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
from pathlib import Path
from typing import Any, Protocol

import yaml

from ..config import get_settings
from ..connectors.base import ConnectorError
from ..connectors.kubernetes import KubernetesConnector, open_exec
from ..models import TerminalSession
from .aws import TerminalError

log = logging.getLogger("moks.terminal")

SHELL_USER = 10002
SECRETS_DIR = "/run/moks"
AWS_FILE = f"{SECRETS_DIR}/aws-credentials"
KUBECONFIG = f"{SECRETS_DIR}/kubeconfig"
CONTAINER = "shell"
CA_FILE = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
# -A joins the session a dropped connection left behind, -D takes it from any other browser.
TMUX = ["tmux", "-u", "new-session", "-A", "-D", "-s", "moks"]
STDIN, STDOUT, STDERR, ERROR, RESIZE = 0, 1, 2, 3, 4


def pod_manifest(s: TerminalSession) -> dict:
    """Restricted Pod Security, with every default that matters written out."""
    cfg = get_settings()
    seconds = s.lifetime_minutes * 60
    pull = [{"name": n} for n in cfg.terminal_image_pull_secrets.split(",") if n]
    env = {"HOME": "/tmp/home", "TERM": "xterm-256color", "AWS_SHARED_CREDENTIALS_FILE": AWS_FILE, "AWS_CONFIG_FILE": f"{SECRETS_DIR}/aws-config",
           "AWS_REGION": s.region, "AWS_DEFAULT_REGION": s.region, "AWS_EC2_METADATA_DISABLED": "true", "AWS_PAGER": "", "KUBECONFIG": KUBECONFIG,
           "MOKS_ACCOUNT": s.aws_account, "MOKS_ENVIRONMENT": s.environment, "TMUX_TMPDIR": "/tmp"}
    return {
        "apiVersion": "v1", "kind": "Pod",
        "metadata": {"name": s.pod_name, "namespace": s.pod_namespace,
                     "labels": {"app": "moks-terminal", "moks.io/session": str(s.id)},
                     "annotations": {"moks.io/owner": str(s.owner_id), "moks.io/expires-at": s.expires_at.isoformat()}},
        "spec": {
            "serviceAccountName": cfg.terminal_service_account, "automountServiceAccountToken": False, "enableServiceLinks": False,
            "hostNetwork": False, "hostPID": False, "hostIPC": False, "shareProcessNamespace": False,
            "restartPolicy": "Never", "activeDeadlineSeconds": seconds + 120, "terminationGracePeriodSeconds": 1,
            "securityContext": {"runAsNonRoot": True, "runAsUser": SHELL_USER, "runAsGroup": SHELL_USER, "seccompProfile": {"type": "RuntimeDefault"}},
            **({"imagePullSecrets": pull} if pull else {}),
            "containers": [{
                "name": CONTAINER, "image": s.image, "imagePullPolicy": "IfNotPresent", "command": ["sleep", str(seconds + 60)],
                "env": [{"name": k, "value": v} for k, v in env.items()],
                "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "privileged": False, "capabilities": {"drop": ["ALL"]}},
                "resources": {"requests": {"cpu": "50m", "memory": "128Mi", "ephemeral-storage": "64Mi"},
                              "limits": {"cpu": cfg.terminal_cpu_limit, "memory": cfg.terminal_memory_limit, "ephemeral-storage": "512Mi"}},
                "volumeMounts": [{"name": "tmp", "mountPath": "/tmp"}, {"name": "secrets", "mountPath": SECRETS_DIR}],
            }],
            "volumes": [{"name": "tmp", "emptyDir": {"sizeLimit": "256Mi"}},
                        # Memory only: credentials never reach the node's disk, and go when the pod goes.
                        {"name": "secrets", "emptyDir": {"medium": "Memory", "sizeLimit": "1Mi"}}],
        },
    }


def kubeconfig(server: str, ca_pem: bytes, token: str, namespace: str) -> bytes:
    return yaml.safe_dump({
        "apiVersion": "v1", "kind": "Config", "current-context": "moks",
        "clusters": [{"name": "cluster", "cluster": {"server": server, "certificate-authority-data": base64.b64encode(ca_pem).decode()}}],
        "users": [{"name": "session", "user": {"token": token}}],
        "contexts": [{"name": "moks", "context": {"cluster": "cluster", "user": "session", "namespace": namespace}}],
    }).encode()


class Stream(Protocol):
    async def send(self, data: bytes) -> None: ...
    async def resize(self, cols: int, rows: int) -> None: ...
    async def receive(self) -> bytes | None: ...
    async def close(self) -> None: ...


class ExecStream:
    """An interactive exec, over the v4 channel protocol."""

    def __init__(self, owner, resp) -> None:
        self.owner, self.resp = owner, resp

    async def send(self, data: bytes) -> None:
        await self.resp.send_bytes(bytes([STDIN]) + data)

    async def resize(self, cols: int, rows: int) -> None:
        await self.resp.send_bytes(bytes([RESIZE]) + json.dumps({"Width": cols, "Height": rows}).encode())

    async def receive(self) -> bytes | None:
        """The next output, or None once the shell's connection has ended."""
        while True:
            msg = await self.resp.receive()
            if msg.type not in (1, 2):  # aiohttp WSMsgType TEXT/BINARY
                return None
            data = msg.data if isinstance(msg.data, (bytes, bytearray)) else msg.data.encode()
            if data and data[0] in (STDOUT, STDERR) and len(data) > 1:
                return bytes(data[1:])
            if data and data[0] == ERROR:
                return None

    async def close(self) -> None:
        try:
            await self.resp.close()
        finally:
            await self.owner.close()


class Pods:
    def __init__(self) -> None:
        self.conn = KubernetesConnector({"mode": "in_cluster"})
        self.ns = get_settings().terminal_namespace

    async def aclose(self) -> None:
        await self.conn.aclose()

    async def create(self, manifest: dict) -> None:
        core = await self.conn.core()
        try:
            await asyncio.wait_for(core.create_namespaced_pod(self.ns, manifest), 30)
        except Exception as exc:
            raise TerminalError(f"the pod could not be created: {_status(exc)}") from None

    async def wait_running(self, name: str, seconds: int) -> None:
        core = await self.conn.core()
        for _ in range(seconds // 2):
            pod = await asyncio.wait_for(core.read_namespaced_pod(name, self.ns), 15)
            if pod.status.phase == "Running":
                return
            if pod.status.phase in ("Failed", "Succeeded"):
                raise TerminalError(f"the pod stopped before it was ready ({pod.status.phase})")
            waiting = [c.state.waiting.reason for c in pod.status.container_statuses or [] if c.state and c.state.waiting and c.state.waiting.reason]
            if any(r in ("ErrImagePull", "ImagePullBackOff", "InvalidImageName") for r in waiting):
                raise TerminalError(f"the terminal image could not be pulled ({waiting[0]})")
            await asyncio.sleep(2)
        raise TerminalError(f"the pod was not running after {seconds} seconds")

    async def delete(self, name: str) -> None:
        core = await self.conn.core()
        try:
            await asyncio.wait_for(core.delete_namespaced_pod(name, self.ns, grace_period_seconds=0), 30)
        except Exception as exc:
            if getattr(exc, "status", None) != 404:
                raise

    async def sessions(self) -> dict[str, str]:
        """Pod name to session id, for every terminal pod that exists."""
        core = await self.conn.core()
        out = await asyncio.wait_for(core.list_namespaced_pod(self.ns, label_selector="app=moks-terminal"), 30)
        return {p.metadata.name: p.metadata.labels.get("moks.io/session", "") for p in out.items}

    async def service_account_token(self, pod: str, seconds: int) -> str:
        """Bound to this pod, so it stops working when the pod is deleted, whatever its expiry says."""
        from kubernetes_asyncio import client

        core = await self.conn.core()
        current = await asyncio.wait_for(core.read_namespaced_pod(pod, self.ns), 15)
        request = client.AuthenticationV1TokenRequest(spec=client.V1TokenRequestSpec(
            audiences=[], expiration_seconds=seconds, bound_object_ref=client.V1BoundObjectReference(kind="Pod", api_version="v1", name=pod, uid=current.metadata.uid)))
        try:
            out = await asyncio.wait_for(core.create_namespaced_service_account_token(get_settings().terminal_service_account, self.ns, request), 15)
        except Exception as exc:
            raise TerminalError(f"could not issue a Kubernetes token: {_status(exc)}") from None
        return out.status.token

    async def server(self) -> tuple[str, bytes]:
        api = await self.conn._client()
        return api.configuration.host, Path(CA_FILE).read_bytes()

    async def _exec(self, ws, pod: str, argv: list[str], stdin: bool, tty: bool):
        return await asyncio.wait_for(open_exec(ws, pod, self.ns, command=argv, container=CONTAINER, stderr=True, stdout=True, stdin=stdin, tty=tty), 15)

    async def run(self, pod: str, argv: list[str], stdin: bytes = b"", timeout: int = 30) -> tuple[int | None, str]:
        """A fixed command with no shell. stdin stays out of argv, so credentials never appear in a process list."""
        from kubernetes_asyncio.stream import WsApiClient

        cfg = (await self.conn._client()).configuration
        out, code = bytearray(), None
        async with WsApiClient(configuration=cfg) as ws:
            try:
                resp = await self._exec(ws, pod, argv, bool(stdin), False)
            except Exception as exc:
                raise TerminalError(f"could not run a command in the pod: {_status(exc)}") from None
            try:
                async with asyncio.timeout(timeout):
                    if stdin:
                        await resp.send_bytes(bytes([STDIN]) + stdin)
                    while True:
                        msg = await resp.receive()
                        if msg.type not in (1, 2):
                            break
                        data = msg.data if isinstance(msg.data, (bytes, bytearray)) else msg.data.encode()
                        if data[0] in (STDOUT, STDERR):
                            out.extend(data[1:])
                            del out[65536:]
                        elif data[0] == ERROR:
                            code = exit_code(bytes(data[1:]))
            except TimeoutError:
                return None, f"timed out after {timeout}s"
            finally:
                await resp.close()
        return code, out.decode("utf-8", "replace")

    async def write_file(self, pod: str, path: str, content: bytes) -> None:
        # head stops after exactly this many bytes, because this protocol cannot signal end of input.
        code, out = await self.run(pod, ["sh", "-c", f"umask 077; head -c {len(content)} > \"$1\"", "sh", path], stdin=content)
        if code != 0:
            raise TerminalError(f"could not place credentials in the pod (exit {code})")

    async def has_shell(self, pod: str) -> bool:
        """False only when tmux itself says the shell is gone. A failed check must not end a session."""
        try:
            code, _ = await self.run(pod, ["tmux", "has-session", "-t", "moks"], timeout=10)
        except TerminalError:
            return True
        return code != 1

    async def attach(self, pod: str) -> ExecStream:
        """Joins the shell. Owns its own client because a terminal outlives any one request."""
        from kubernetes_asyncio.stream import WsApiClient

        cfg = (await self.conn._client()).configuration
        ws = WsApiClient(configuration=cfg, heartbeat=30)  # a quiet shell must not look like a dead connection
        try:
            return ExecStream(ws, await self._exec(ws, pod, TMUX, True, True))
        except Exception as exc:
            await ws.close()
            raise TerminalError(f"could not attach to the pod: {_status(exc)}") from None


def exit_code(payload: bytes) -> int:
    status = json.loads(payload.decode() or "{}")
    if status.get("status") == "Success":
        return 0
    return int(next((c.get("message") for c in (status.get("details") or {}).get("causes", []) if c.get("reason") == "ExitCode"), 1))


def _status(exc: Exception) -> str:
    if isinstance(exc, ConnectorError):
        return exc.message
    return str(getattr(exc, "reason", None) or type(exc).__name__)[:200]
