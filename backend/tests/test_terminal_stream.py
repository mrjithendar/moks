"""The exec path for real: the actual Kubernetes client, speaking the v4 channel protocol to a local
server that runs a real Bash on a real pseudo-terminal. Only the API server itself is a stand-in."""
import asyncio
import fcntl
import json
import os
import pty
import struct
import termios

import pytest
from aiohttp import WSMsgType, web

from moks.terminal import pods as tpods
from moks.terminal.aws import TerminalError


def _status(code: int) -> bytes:
    if code == 0:
        return b'{"status":"Success"}'
    return json.dumps({"status": "Failure", "details": {"causes": [{"reason": "ExitCode", "message": str(code)}]}}).encode()


async def _exec(request):
    """What the kubelet does: one process, with channels for its stdio, its resize and its exit."""
    ws = web.WebSocketResponse(protocols=["v4.channel.k8s.io"])
    await ws.prepare(request)
    argv = request.query.getall("command")
    tty = request.query.get("tty", "").lower() == "true"  # the client sends "True"; Kubernetes accepts both
    if argv[0] == "tmux":  # tmux is not installed here; the channel behaviour under test is the same
        argv = ["bash", "--norc", "--noprofile"]
    loop = asyncio.get_running_loop()
    if tty:
        master, slave = pty.openpty()
        proc = await asyncio.create_subprocess_exec(*argv, stdin=slave, stdout=slave, stderr=slave, start_new_session=True,
                                                    preexec_fn=lambda: fcntl.ioctl(0, termios.TIOCSCTTY, 0), env={"PS1": "$ ", "PATH": os.environ["PATH"], "TERM": "xterm"})
        os.close(slave)
        os.set_blocking(master, False)
        reader, writer_fd = master, master
        ready = asyncio.Event()
        loop.add_reader(master, ready.set)
    else:
        proc = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        reader, writer_fd = proc.stdout, None

    async def output():
        while True:
            if tty:
                await ready.wait()
                ready.clear()
                try:
                    data = os.read(reader, 4096)
                except BlockingIOError:
                    continue
                except OSError:
                    data = b""
            else:
                data = await reader.read(4096)
            if not data:
                return
            await ws.send_bytes(bytes([tpods.STDOUT]) + data)

    async def incoming():
        async for msg in ws:
            if msg.type != WSMsgType.BINARY:
                continue
            channel, payload = msg.data[0], msg.data[1:]
            if channel == tpods.STDIN:
                if tty:
                    os.write(writer_fd, payload)
                else:
                    proc.stdin.write(payload)
                    await proc.stdin.drain()
            elif channel == tpods.RESIZE:
                size = json.loads(payload)
                fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", size["Height"], size["Width"], 0, 0))
        if proc.returncode is None:  # the client left: the kubelet would tear the process down
            proc.kill()

    out, inc = asyncio.create_task(output()), asyncio.create_task(incoming())
    code = await proc.wait()
    await asyncio.wait([out], timeout=1)
    if not ws.closed:
        await ws.send_bytes(bytes([tpods.ERROR]) + _status(code))
    inc.cancel()
    await ws.close()
    if tty:
        out.cancel()
        loop.remove_reader(master)
        os.close(master)
    return ws


@pytest.fixture
async def pods():
    from kubernetes_asyncio import client

    app = web.Application()
    app.router.add_get("/api/v1/namespaces/{ns}/pods/{pod}/exec", _exec)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    cfg = client.Configuration(host=f"http://127.0.0.1:{port}")

    class Conn:
        async def _client(self):
            class A:
                configuration = cfg
            return A

        async def aclose(self):
            pass

    p = tpods.Pods.__new__(tpods.Pods)
    p.conn, p.ns = Conn(), "moks-terminals"
    yield p
    await runner.cleanup()


@pytest.fixture
async def writer(pods, monkeypatch):
    """The executor's writer against the same server: diagnostic commands and database dumps use this path."""
    from moks.actions.writer import KubernetesWriter
    from moks.config import get_settings

    monkeypatch.setattr(get_settings(), "process_role", "executor")
    w = KubernetesWriter({"mode": "in_cluster"})
    monkeypatch.setattr(w.read, "_client", pods.conn._client)
    return w


async def test_a_diagnostic_command_really_runs(writer):
    assert await writer.exec("moks-diagnostics", "p", ["sh", "-c", "echo found; exit 2"]) == (2, "found\n")


async def test_a_database_dump_really_streams_to_its_sink(writer):
    chunks = []

    async def sink(data):
        chunks.append(data)
    code, _ = await writer.exec_stream("shop", "db-0", "db", ["sh", "-c", "printf PGDMP-contents"], sink, 10)
    assert code == 0 and b"".join(chunks) == b"PGDMP-contents"


async def until(stream, text: str, seconds: float = 5) -> str:
    seen = ""
    async with asyncio.timeout(seconds):
        while text not in seen:
            chunk = await stream.receive()
            assert chunk is not None, f"stream ended; saw {seen!r}"
            seen += chunk.decode("utf-8", "replace")
    return seen


async def test_typing_reaches_a_real_shell_and_its_output_comes_back(pods):
    s = await pods.attach("p")
    try:
        await s.send(b"echo reached-$((40+2))\r")
        await until(s, "reached-42")
    finally:
        await s.close()


async def test_resize_reaches_the_pseudo_terminal(pods):
    s = await pods.attach("p")
    try:
        await s.resize(132, 43)
        await s.send(b"stty size\r")
        await until(s, "43 132")
    finally:
        await s.close()


async def test_control_characters_and_utf8_pass_through(pods):
    s = await pods.attach("p")
    try:
        await s.send(b"sleep 30\r")
        await asyncio.sleep(0.3)
        await s.send(b"\x03")  # Ctrl-C
        await s.send("echo héllo-✓\r".encode())
        await until(s, "héllo-✓")
    finally:
        await s.close()


async def test_exit_ends_the_stream(pods):
    s = await pods.attach("p")
    try:
        await s.send(b"exit\r")
        async with asyncio.timeout(5):
            while await s.receive() is not None:
                pass
    finally:
        await s.close()


async def test_commands_report_output_and_exit_code(pods):
    assert await pods.run("p", ["sh", "-c", "echo hello; exit 0"]) == (0, "hello\n")
    code, out = await pods.run("p", ["sh", "-c", "echo nope; exit 3"])
    assert code == 3 and out == "nope\n"


async def test_a_file_is_written_from_stdin_with_private_permissions(pods, tmp_path):
    secret = b"[default]\naws_secret_access_key = abc/def+ghi\n"
    path = tmp_path / "aws-credentials"
    await pods.write_file("p", str(path), secret)
    assert path.read_bytes() == secret and oct(path.stat().st_mode & 0o777) == "0o600"


async def test_a_failed_write_is_an_error_not_a_silent_session_without_credentials(pods, tmp_path):
    with pytest.raises(TerminalError):
        await pods.write_file("p", str(tmp_path / "missing-dir" / "x"), b"data")


async def test_a_slow_command_times_out(pods):
    code, out = await pods.run("p", ["sleep", "30"], timeout=1)
    assert code is None and "timed out" in out


async def test_a_finished_shell_and_a_failed_check_read_differently(pods, monkeypatch):
    async def gone(*a, **k):
        return 1, "no sessions"

    async def broken(*a, **k):
        raise TerminalError("no route")
    monkeypatch.setattr(pods, "run", gone)
    assert await pods.has_shell("p") is False
    monkeypatch.setattr(pods, "run", broken)
    assert await pods.has_shell("p") is True


# ---- the real WebSocket upgrade, through uvicorn ------------------------------------------------

@pytest.fixture
async def served(database, monkeypatch):
    import uvicorn

    from moks.config import get_settings
    from moks.terminal import service
    from moks.terminal.app import create_app

    class Idle:  # nothing here reaches a pod: these connections must stop before that
        async def aclose(self):
            pass

        async def sessions(self):
            return {}

    monkeypatch.setattr(service, "pods_factory", Idle)
    monkeypatch.setattr(get_settings(), "public_url", "https://jithendar.com/moks")
    server = uvicorn.Server(uvicorn.Config(create_app(), host="127.0.0.1", port=0, log_config=None, ws_max_size=1_048_576))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    yield server.servers[0].sockets[0].getsockname()[1]
    server.should_exit = True
    await task


async def test_a_foreign_origin_is_refused_at_the_upgrade(served):
    import websockets

    url = f"ws://127.0.0.1:{served}/moks/api/terminal/sessions/00000000-0000-0000-0000-000000000000/attach"
    for origin in ("https://evil.example", None):
        with pytest.raises(websockets.exceptions.InvalidStatus) as refused:
            async with websockets.connect(url, origin=origin):
                pass
        assert refused.value.response.status_code == 403


async def test_a_right_origin_without_a_session_is_told_to_sign_in(served):
    import websockets

    url = f"ws://127.0.0.1:{served}/moks/api/terminal/sessions/00000000-0000-0000-0000-000000000000/attach"
    async with websockets.connect(url, origin="https://jithendar.com") as ws:
        with pytest.raises(websockets.exceptions.ConnectionClosed) as closed:
            await ws.recv()
    assert closed.value.rcvd.code == 4401
