#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Jasmine Software
"""OpenCode Go local reverse proxy.

    client --HTTP--> 127.0.0.1:8787 --HTTPS--> https://opencode.ai/inference/go

Single file, stdlib only, no config file. It keeps the Go API key in this
process, maps /v1/* onto the Go gateway, and adds the x-opencode-session header
Go requires (without it the gateway answers 400 MissingSessionID).

Environment:
  OPENCODE_GO_API_KEY   required (oc_sk_...)
  OPENCODE_GO_UPSTREAM  default https://opencode.ai/inference/go
  HOST / PORT           default 127.0.0.1 / 8787
  DEBUG=1               verbose error output
  MAX_BODY_BYTES        default 64 MiB
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from http.client import HTTPConnection, HTTPSConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

VERSION = "0.0.1"
UPSTREAM_URL = os.environ.get("OPENCODE_GO_UPSTREAM", "https://opencode.ai/inference/go").rstrip("/")
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8787"))
DEBUG = os.environ.get("DEBUG", "").strip().lower() in ("1", "true", "yes")
MAX_BODY_BYTES = int(os.environ.get("MAX_BODY_BYTES", str(64 * 1024 * 1024)))

SCRIPT = Path(__file__).resolve()
LOG_DIR = SCRIPT.parent / "logs"

HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "proxy-connection",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}

SESSION_HEADERS = (
    "x-opencode-session",
    "x-deepseek-harness-session-id",
    "x-session-affinity",
    "x-session-id",
    "session_id",
    "x-conversation-id",
    "conversation_id",
)

ROUTES = {
    "/v1/chat/completions": "/openai/v1/chat/completions",
    "/v1/responses": "/openai/v1/responses",
    "/v1/messages": "/anthropic/v1/messages",
    "/v1/models": "/v1/models",
}

KEY = ""


# --- key ---------------------------------------------------------------
def load_key() -> str | None:
    """The key comes from OPENCODE_GO_API_KEY only; this proxy keeps no config file."""
    return (os.environ.get("OPENCODE_GO_API_KEY") or "").strip() or None


def mask(key: str) -> str:
    if len(key) <= 10:
        return "***"
    return f"{key[:6]}...{key[-4:]}"


# --- session -----------------------------------------------------------
def normalize_session(value: object) -> str | None:
    text = str(value).strip()
    if not text:
        return None
    if text.startswith("ses_") and 12 <= len(text) <= 84:
        return text
    digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:32]
    return "ses_" + digest


def content_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
        return "".join(parts)
    return ""


def derive_session(body: object) -> str | None:
    if not isinstance(body, dict):
        return None

    cache_key = body.get("prompt_cache_key")
    if isinstance(cache_key, str) and cache_key.strip():
        return normalize_session(cache_key)

    metadata = body.get("metadata")
    if isinstance(metadata, dict):
        for name in ("session_id", "sessionId", "conversation_id", "conversationId"):
            value = metadata.get(name)
            if isinstance(value, str) and value.strip():
                return normalize_session(value)

    messages = body.get("messages")
    if not isinstance(messages, list):
        messages = body.get("input")
    if isinstance(messages, list):
        system = next(
            (
                item
                for item in messages
                if isinstance(item, dict) and item.get("role") in ("system", "developer")
            ),
            None,
        )
        first_user = next(
            (item for item in messages if isinstance(item, dict) and item.get("role") == "user"),
            None,
        )
        anchor = "\0".join(
            [
                str(body.get("model") or ""),
                content_text(system.get("content")) if system else "",
                content_text(first_user.get("content")) if first_user else "",
            ]
        )
        if anchor.strip():
            return normalize_session("anchor:" + anchor)
    return None


def pick_session(headers, body: object, raw_text: str) -> str:
    for name in SESSION_HEADERS:
        value = headers.get(name)
        if value and value.strip():
            session = normalize_session(value)
            if session:
                return session
    session = derive_session(body)
    if session:
        return session
    if raw_text:
        return normalize_session("raw:" + raw_text[:4096])
    return "ses_" + uuid.uuid4().hex


# --- routing -----------------------------------------------------------
def map_path(pathname: str) -> str | None:
    if pathname in ROUTES:
        return ROUTES[pathname]
    if pathname.startswith(("/openai/", "/anthropic/", "/google/")):
        return pathname
    return None


def stamp() -> str:
    return time.strftime("%H:%M:%S")


# --- proxy handler -----------------------------------------------------
class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "opencode-go-proxy/" + VERSION
    sys_version = ""

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - silence default log
        return

    # --- HTTP verbs -----------------------------------------------------
    def do_GET(self) -> None:
        self.handle_proxy()

    def do_HEAD(self) -> None:
        self.handle_proxy()

    def do_POST(self) -> None:
        self.handle_proxy()

    def do_PUT(self) -> None:
        self.handle_proxy()

    def do_PATCH(self) -> None:
        self.handle_proxy()

    def do_DELETE(self) -> None:
        self.handle_proxy()

    # --- helpers --------------------------------------------------------
    def send_json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response_only(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def read_body(self) -> bytes:
        transfer_encoding = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in transfer_encoding:
            chunks: list[bytes] = []
            total = 0
            while True:
                line = self.rfile.readline(65536)
                if not line:
                    break
                size = int(line.split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    while True:
                        trailer = self.rfile.readline(65536)
                        if trailer in (b"\r\n", b"\n", b""):
                            break
                    break
                total += size
                if total > MAX_BODY_BYTES:
                    raise ValueError(f"request body exceeds {MAX_BODY_BYTES} bytes")
                chunks.append(self.rfile.read(size))
                self.rfile.read(2)  # trailing CRLF
            return b"".join(chunks)

        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            raise ValueError(f"request body exceeds {MAX_BODY_BYTES} bytes")
        return self.rfile.read(length) if length > 0 else b""

    def build_headers(self, pathname: str, session: str, body_length: int) -> dict[str, str]:
        headers: dict[str, str] = {}
        for name, value in self.headers.items():
            lower = name.lower()
            if lower in HOP_BY_HOP:
                continue
            if lower in ("host", "authorization", "x-api-key", "x-opencode-session", "content-length"):
                continue
            headers[lower] = f"{headers[lower]}, {value}" if lower in headers else value

        headers["authorization"] = f"Bearer {KEY}"
        if pathname == "/v1/messages":
            headers["x-api-key"] = KEY
        headers["x-opencode-session"] = session
        if body_length > 0:
            headers["content-length"] = str(body_length)
        user_agent = headers.get("user-agent", "unknown")
        headers["user-agent"] = f"{user_agent} opencode-go-proxy/{VERSION}"[:256]
        return headers

    # --- main -----------------------------------------------------------
    def handle_proxy(self) -> None:
        started = time.time()
        parts = urlsplit(self.path)
        pathname = parts.path.rstrip("/") or "/"

        if pathname == "/healthz":
            self.send_json(
                200,
                {
                    "ok": True,
                    "version": VERSION,
                    "pid": os.getpid(),
                    "upstream": UPSTREAM_URL,
                },
            )
            return

        target = map_path(pathname)
        if target is None:
            self.send_json(
                404,
                {"error": {"message": f"opencode-go-proxy: no route for {pathname}", "type": "invalid_request_error"}},
            )
            return

        try:
            body = b"" if self.command in ("GET", "HEAD") else self.read_body()
        except ValueError as error:
            self.send_json(400, {"error": {"message": f"opencode-go-proxy: {error}", "type": "invalid_request_error"}})
            return
        except OSError:
            self.close_connection = True
            return

        raw_text = body.decode("utf-8", "replace").lstrip("\ufeff") if body else ""
        parsed = None
        if raw_text:
            try:
                parsed = json.loads(raw_text)
            except json.JSONDecodeError:
                parsed = None

        session = pick_session(self.headers, parsed, raw_text)
        headers = self.build_headers(pathname, session, len(body))
        model = parsed.get("model") if isinstance(parsed, dict) else None

        upstream = urlsplit(UPSTREAM_URL)
        connection_class = HTTPSConnection if upstream.scheme == "https" else HTTPConnection
        upstream_path = upstream.path.rstrip("/") + target + (f"?{parts.query}" if parts.query else "")

        connection = connection_class(upstream.hostname, upstream.port, timeout=900)
        response = None
        headers_sent = False
        status = 502
        try:
            connection.request(self.command, upstream_path, body=body or None, headers=headers)
            response = connection.getresponse()

            self.send_response_only(response.status, response.reason)
            content_length = response.getheader("Content-Length")
            for name, value in response.getheaders():
                lower = name.lower()
                if lower in HOP_BY_HOP or lower in ("content-length", "connection"):
                    continue
                self.send_header(name, value)
            if content_length is not None:
                self.send_header("Content-Length", content_length)
            else:
                # Streaming (SSE): delimit by closing the connection.
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            headers_sent = True

            if self.command != "HEAD":
                while True:
                    chunk = response.read1(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()

            status = response.status
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
            status = 499
        except Exception as error:  # noqa: BLE001 - surface any upstream failure as 502
            if DEBUG:
                print(f"[{stamp()}] upstream error: {error!r}", flush=True)
            if headers_sent:
                self.close_connection = True
            else:
                self.send_json(
                    502,
                    {"error": {"message": f"opencode-go-proxy upstream error: {error}", "type": "api_error"}},
                )
            status = 502
        finally:
            if response is not None:
                response.close()
            connection.close()

        elapsed = int((time.time() - started) * 1000)
        bits = [f"[{stamp()}]", self.command, f"{pathname} -> {status}", f"{elapsed}ms"]
        if model:
            bits.append(f"model={model}")
        bits.append(f"session={session}")
        if isinstance(parsed, dict) and parsed.get("stream"):
            bits.append("stream")
        print(" ".join(bits), flush=True)


# --- server ------------------------------------------------------------
class ProxyServer(ThreadingHTTPServer):
    """Threaded server that stays quiet when a client hangs up mid-request."""

    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address) -> None:
        error = sys.exc_info()[1]
        if isinstance(error, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
            return  # client cancelled (Ctrl+C, harness abort); not our problem
        super().handle_error(request, client_address)


# --- process helpers ---------------------------------------------------
def pid_path(port: int) -> Path:
    return LOG_DIR / f"proxy-{port}.pid"


def log_paths(port: int) -> tuple[Path, Path]:
    return LOG_DIR / f"proxy-{port}.out.log", LOG_DIR / f"proxy-{port}.err.log"


def read_pid(port: int) -> int | None:
    try:
        text = pid_path(port).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def write_pid(port: int) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    pid_path(port).write_text(str(os.getpid()), encoding="utf-8")


def clear_pid(port: int) -> None:
    try:
        pid_path(port).unlink()
    except OSError:
        pass


def _win_open(pid: int, access: int):
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(access, False, pid)
    return kernel32, handle or None


def is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        kernel32, handle = _win_open(pid, 0x1000)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def process_is_proxy(pid: int) -> bool:
    """Best-effort guard against PID reuse before we kill something."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32, handle = _win_open(pid, 0x1000)
        if not handle:
            return False
        try:
            size = wintypes.DWORD(1024)
            buffer = ctypes.create_unicode_buffer(1024)
            if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                return True  # cannot inspect; trust the pidfile
            return Path(buffer.value).name.lower().startswith("python")
        finally:
            kernel32.CloseHandle(handle)
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return True  # no procfs; trust the pidfile
    return b"proxy.py" in cmdline or b"opencode-go-proxy" in cmdline


def terminate(pid: int, timeout: float = 5.0) -> bool:
    if not is_alive(pid):
        return False
    if os.name == "nt":
        kernel32, handle = _win_open(pid, 0x0001)  # PROCESS_TERMINATE
        if not handle:
            return False
        try:
            kernel32.TerminateProcess(handle, 1)
        finally:
            kernel32.CloseHandle(handle)
    else:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return False
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not is_alive(pid):
            return True
        time.sleep(0.1)
    if os.name != "nt":
        try:
            os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
        except OSError:
            pass
        time.sleep(0.2)
    return not is_alive(pid)


def listener_pid(port: int) -> int | None:
    """Find the process listening on a port, without third-party packages."""
    if os.name == "nt":
        try:
            output = subprocess.run(
                ["netstat", "-ano", "-p", "tcp"],
                capture_output=True,
                text=True,
                timeout=15,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        for line in output.splitlines():
            fields = line.split()
            if len(fields) < 5 or fields[0].upper() != "TCP":
                continue
            if fields[3].upper() != "LISTENING":
                continue
            if not fields[1].endswith(f":{port}"):
                continue
            try:
                return int(fields[4])
            except ValueError:
                continue
        return None

    for argv in (["lsof", "-t", f"-iTCP:{port}", "-sTCP:LISTEN"], ["fuser", f"{port}/tcp"]):
        try:
            output = subprocess.run(argv, capture_output=True, text=True, timeout=15).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        for token in output.split():
            if token.isdigit():
                return int(token)
    return None


def probe_health(host: str, port: int, timeout: float = 2.0) -> dict | None:
    target = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://{target}:{port}/healthz", timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return None


# --- commands ----------------------------------------------------------
def run_foreground(key: str, host: str, port: int) -> int:
    global KEY
    KEY = key

    # SO_REUSEADDR lets two sockets share a port on Windows; check before binding.
    owner = listener_pid(port)
    if owner and owner != os.getpid():
        print(
            f"opencode-go-proxy: port {port} is already in use (pid {owner}).\n"
            f"  stop it:  {sys.executable} {SCRIPT} stop --port {port}\n"
            f"  or use:   --port {port + 1}",
            file=sys.stderr,
        )
        return 1

    try:
        server = ProxyServer((host, port), ProxyHandler)
    except OSError as error:
        print(f"opencode-go-proxy: cannot listen on {host}:{port}: {error}", file=sys.stderr)
        return 1

    write_pid(port)

    print(f"opencode-go-proxy {VERSION}")
    print(f"  listening  http://{host}:{port}/v1")
    print(f"  upstream   {UPSTREAM_URL}")
    print(f"  api key    {mask(key)} (from OPENCODE_GO_API_KEY)")
    print("  routes     /v1/chat/completions, /v1/responses, /v1/messages, /v1/models")
    print(f"  pid        {os.getpid()}")
    if DEBUG:
        print("  debug      on")
    sys.stdout.flush()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped", flush=True)
    finally:
        server.server_close()
        if read_pid(port) == os.getpid():
            clear_pid(port)
    return 0


def cmd_start(args) -> int:
    host = args.host
    port = args.port
    key = load_key()
    if not key:
        print(
            "opencode-go-proxy: OPENCODE_GO_API_KEY is not set.\n"
            '\n  PowerShell:  $env:OPENCODE_GO_API_KEY = "oc_sk_..."\n'
            '  bash/zsh:    export OPENCODE_GO_API_KEY="oc_sk_..."\n'
            "  Key: OpenCode Console -> Account -> Personal API keys",
            file=sys.stderr,
        )
        return 1

    if not args.background:
        return run_foreground(key, host, port)

    existing = probe_health(host, port)
    if existing:
        print(f"opencode-go-proxy already running on http://{host}:{port}/v1 (pid {existing.get('pid')})")
        return 0

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    out_path, err_path = log_paths(port)
    argv = [sys.executable, str(SCRIPT), "start", "--host", host, "--port", str(port)]

    # The child inherits the key from this environment, never from argv.
    env = os.environ.copy()

    kwargs: dict = {
        "cwd": str(SCRIPT.parent),
        "env": env,
        "stdin": subprocess.DEVNULL,
    }
    if os.name == "nt":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP: survives the parent console.
        kwargs["creationflags"] = 0x00000008 | 0x00000200
    else:
        kwargs["start_new_session"] = True

    with open(out_path, "ab") as out, open(err_path, "ab") as err:
        kwargs["stdout"] = out
        kwargs["stderr"] = err
        child = subprocess.Popen(argv, **kwargs)

    deadline = time.time() + 15.0
    health = None
    while time.time() < deadline:
        if child.poll() is not None:
            break
        health = probe_health(host, port)
        if health:
            break
        time.sleep(0.2)

    if not health:
        print("opencode-go-proxy: failed to start.", file=sys.stderr)
        try:
            tail = err_path.read_text(encoding="utf-8", errors="replace").splitlines()[-15:]
        except OSError:
            tail = []
        for line in tail:
            print(f"  {line}", file=sys.stderr)
        print(f"  log: {out_path}", file=sys.stderr)
        if child.poll() is None:
            terminate(child.pid)
        return 1

    pid = health.get("pid") or read_pid(port) or child.pid
    print("opencode-go-proxy started in background")
    print(f"  PID    {pid}")
    print(f"  URL    http://{host}:{port}/v1")
    print(f"  log    {out_path}")
    print(f"  stop   {sys.executable} {SCRIPT} stop --port {port}")
    return 0


def cmd_stop(args) -> int:
    port = args.port
    pid = read_pid(port)

    if pid is not None:
        if is_alive(pid) and process_is_proxy(pid):
            if terminate(pid):
                clear_pid(port)
                print(f"stopped opencode-go-proxy (pid {pid})")
            else:
                print(f"opencode-go-proxy: could not stop pid {pid}", file=sys.stderr)
                return 1
            return 0
        clear_pid(port)

    owner = listener_pid(port)
    if owner and owner != os.getpid():
        # Only kill something that answers /healthz like this proxy; a stray
        # service on the same port must be left alone.
        health = probe_health(HOST, port)
        if not health or "upstream" not in health:
            print(
                f"opencode-go-proxy: port {port} is held by pid {owner}, which is not this proxy.",
                file=sys.stderr,
            )
            return 1
        if terminate(owner):
            clear_pid(port)
            print(f"stopped process {owner} on port {port}")
            return 0
        print(f"opencode-go-proxy: could not stop pid {owner} on port {port}", file=sys.stderr)
        return 1

    print(f"nothing running on port {port}")
    return 0


def cmd_status(args) -> int:
    health = probe_health(args.host, args.port)
    if not health:
        print(f"opencode-go-proxy is not running on http://{args.host}:{args.port}")
        return 1
    print("opencode-go-proxy is running")
    print(f"  url      http://{args.host}:{args.port}/v1")
    print(f"  pid      {health.get('pid') or read_pid(args.port) or 'unknown'}")
    print(f"  version  {health.get('version', 'unknown')}")
    print(f"  upstream {health.get('upstream', UPSTREAM_URL)}")
    return 0


# --- entry point -------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="proxy.py",
        description="OpenCode Go local reverse proxy (OpenAI-compatible endpoint on 127.0.0.1).",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    subparsers = parser.add_subparsers(dest="command")

    start = subparsers.add_parser("start", help="start the proxy (foreground unless -b)")
    start.add_argument("-b", "--background", action="store_true", help="detach; logs go to ./logs/")
    start.add_argument("--host", default=HOST, help=f"bind address (default {HOST})")
    start.add_argument("--port", type=int, default=PORT, help=f"listen port (default {PORT})")

    stop = subparsers.add_parser("stop", help="stop the instance for this port")
    stop.add_argument("--port", type=int, default=PORT, help=f"listen port (default {PORT})")

    status = subparsers.add_parser("status", help="report whether the proxy is running")
    status.add_argument("--host", default=HOST, help=f"bind address (default {HOST})")
    status.add_argument("--port", type=int, default=PORT, help=f"listen port (default {PORT})")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # No subcommand keeps the original behaviour: run in the foreground.
    if args.command is None:
        args.command, args.background, args.host, args.port = "start", False, HOST, PORT

    return {"start": cmd_start, "stop": cmd_stop, "status": cmd_status}[args.command](args)


def _install_signal_handlers() -> None:
    if os.name == "nt":
        return

    def handler(_signum, _frame):
        raise KeyboardInterrupt

    for name in ("SIGTERM", "SIGHUP"):
        number = getattr(signal, name, None)
        if number is not None:
            try:
                signal.signal(number, handler)
            except (OSError, ValueError):
                pass


if __name__ == "__main__":
    _install_signal_handlers()
    sys.exit(main())
