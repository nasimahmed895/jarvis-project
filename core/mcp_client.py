"""
Minimal Model Context Protocol client.

Lets JARVIS launch MCP servers as stdio subprocesses, discover the tools
they expose, and call them. No third-party dependency: this is plain
JSON-RPC 2.0 framed as line-delimited JSON on stdin/stdout, which is how
MCP stdio transport works.

Each server is described in `config/mcp_servers.json` as:

    {
      "servers": [
        {
          "name": "notion",
          "command": ["npx", "-y", "@modelcontextprotocol/server-notion"],
          "env": {"NOTION_API_KEY": "secret_..."},
          "disabled": false
        },
        ...
      ]
    }

At startup the client spawns each enabled server, calls `initialize` +
`tools/list`, and publishes the tools so JARVIS's Gemini tool declaration
list includes them under names like `mcp__notion__search`. When the model
calls one of those names, the client routes it to the right server's
`tools/call`.

Scope:
  - stdio transport only (streamable HTTP can come later).
  - Tools only (no resources, prompts, sampling callbacks).
  - One thread per server reading stdout; one lock per server for the
    request/response correlation. The Gemini turn loop dispatches
    `call_tool` on a background thread so the live loop never blocks.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable


_DEFAULT_TIMEOUT = 30.0


class MCPError(RuntimeError):
    pass


class MCPServer:
    """One stdio-attached MCP server. Owns a subprocess + reader thread."""

    def __init__(self, name: str, command: list[str],
                 env: dict[str, str] | None = None,
                 cwd: str | None = None,
                 logger: Callable[[str], None] | None = None):
        self.name = name
        self.command = list(command)
        self.env = dict(env or {})
        self.cwd = cwd
        self._log = logger or (lambda m: None)
        self._proc: subprocess.Popen | None = None
        self._next_id = 1
        self._id_lock = threading.Lock()
        self._pending: dict[int, dict] = {}
        self._pending_cv = threading.Condition()
        self._reader: threading.Thread | None = None
        self._stop = threading.Event()
        self._tools: list[dict] = []
        self.started = False

    # ── Process lifecycle ────────────────────────────────────────────────

    def start(self) -> None:
        if self.started:
            return
        env = os.environ.copy()
        env.update(self.env)
        self._log(f"[MCP:{self.name}] launching: {' '.join(self.command)}")
        self._proc = subprocess.Popen(
            self.command,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env, cwd=self.cwd,
            bufsize=1, text=True, encoding="utf-8",
        )
        self._reader = threading.Thread(
            target=self._read_loop, daemon=True,
            name=f"mcp-{self.name}-read",
        )
        self._reader.start()
        # Capture stderr onto our log without blocking.
        threading.Thread(
            target=self._stderr_loop, daemon=True,
            name=f"mcp-{self.name}-err",
        ).start()
        # Handshake. Any failure here propagates up so caller can skip the server.
        self._request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "jarvis", "version": "1.0"},
        }, timeout=20.0)
        self._notify("notifications/initialized", {})
        self._refresh_tools()
        self.started = True
        self._log(f"[MCP:{self.name}] online — {len(self._tools)} tool(s)")

    def stop(self) -> None:
        self._stop.set()
        if self._proc is None:
            return
        try:
            self._proc.terminate()
            self._proc.wait(timeout=5)
        except Exception:
            try:
                self._proc.kill()
            except Exception:
                pass

    def _stderr_loop(self) -> None:
        proc = self._proc
        if not proc or not proc.stderr:
            return
        for line in proc.stderr:
            if self._stop.is_set():
                break
            line = line.rstrip()
            if line:
                self._log(f"[MCP:{self.name}:stderr] {line[:300]}")

    def _read_loop(self) -> None:
        proc = self._proc
        if not proc or not proc.stdout:
            return
        for line in proc.stdout:
            if self._stop.is_set():
                break
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception as e:
                self._log(f"[MCP:{self.name}] bad line: {e} ({line[:120]!r})")
                continue
            rid = msg.get("id")
            if rid is None:
                # notification from server — ignore for now (progress, log)
                continue
            with self._pending_cv:
                self._pending[rid] = msg
                self._pending_cv.notify_all()
        # On EOF, mark remaining pending as aborted.
        with self._pending_cv:
            for rid in list(self._pending):
                self._pending[rid] = {"id": rid,
                                      "error": {"code": -32000,
                                                "message": "server closed"}}
            self._pending_cv.notify_all()

    # ── JSON-RPC primitives ──────────────────────────────────────────────

    def _next(self) -> int:
        with self._id_lock:
            v = self._next_id
            self._next_id += 1
            return v

    def _send(self, payload: dict) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise MCPError(f"{self.name}: server not running")
        self._proc.stdin.write(json.dumps(payload) + "\n")
        self._proc.stdin.flush()

    def _notify(self, method: str, params: dict | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method,
                    "params": params or {}})

    def _request(self, method: str, params: dict | None = None,
                 timeout: float = _DEFAULT_TIMEOUT) -> Any:
        rid = self._next()
        self._send({"jsonrpc": "2.0", "id": rid, "method": method,
                    "params": params or {}})
        deadline = time.monotonic() + timeout
        with self._pending_cv:
            while rid not in self._pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise MCPError(f"{self.name}: {method} timed out")
                self._pending_cv.wait(timeout=remaining)
            resp = self._pending.pop(rid)
        if "error" in resp and resp["error"]:
            err = resp["error"]
            raise MCPError(f"{self.name}: {method} → {err.get('message')}")
        return resp.get("result")

    # ── Tools surface ────────────────────────────────────────────────────

    def _refresh_tools(self) -> None:
        res = self._request("tools/list", {}, timeout=15.0)
        self._tools = res.get("tools", []) if isinstance(res, dict) else []

    def tools(self) -> list[dict]:
        """Return the raw MCP tool list for this server."""
        return list(self._tools)

    def call(self, tool_name: str, arguments: dict | None = None,
             timeout: float = _DEFAULT_TIMEOUT) -> str:
        """Call a tool; returns a flat string result (concatenated text
        content). Non-text content (images, resource refs) is summarised."""
        res = self._request(
            "tools/call",
            {"name": tool_name, "arguments": arguments or {}},
            timeout=timeout,
        )
        if not isinstance(res, dict):
            return str(res)
        if res.get("isError"):
            msg = _extract_content(res.get("content"))
            raise MCPError(f"{self.name}:{tool_name} error: {msg[:400]}")
        return _extract_content(res.get("content"))


def _extract_content(content: Any) -> str:
    if not content:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text" and item.get("text"):
                    parts.append(str(item["text"]))
                elif item.get("type") == "image":
                    parts.append("[image]")
                elif item.get("type") == "resource":
                    parts.append("[resource]")
                elif item.get("text"):
                    parts.append(str(item["text"]))
        return "\n".join(parts).strip()
    return str(content)


# ── Registry ────────────────────────────────────────────────────────────

class MCPRegistry:
    """Owns every launched MCP server and exposes their tools to JARVIS."""

    def __init__(self, logger: Callable[[str], None] | None = None,
                 notify: Callable[[str], None] | None = None):
        self._servers: dict[str, MCPServer] = {}
        self._log = logger or (lambda m: None)
        self._notify = notify or (lambda m: None)

    def load_from_config(self, config_path: Path | str) -> None:
        p = Path(config_path)
        if not p.exists():
            self._log(f"[MCP] no config at {p} — skipping")
            return
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            self._log(f"[MCP] bad config {p}: {e}")
            return
        for entry in data.get("servers", []):
            if entry.get("disabled"):
                continue
            name = entry.get("name") or "unnamed"
            if name in self._servers:
                self._log(f"[MCP] duplicate server name {name!r}, skipping")
                continue
            server = MCPServer(
                name=name,
                command=entry.get("command") or [],
                env=entry.get("env"),
                cwd=entry.get("cwd"),
                logger=self._log,
            )
            try:
                server.start()
                self._servers[name] = server
                self._notify(f"SYS: MCP '{name}' online "
                             f"({len(server.tools())} tools).")
            except Exception as e:
                self._log(f"[MCP] {name} failed to start: {e}")

    def shutdown(self) -> None:
        for s in self._servers.values():
            try:
                s.stop()
            except Exception:
                pass
        self._servers.clear()

    # Flattened tool list in the style JARVIS's Gemini declarations expect.
    # Each MCP tool becomes a declaration with name `mcp__<server>__<tool>`
    # so the Gemini tool namespace cannot collide with native actions.
    def get_tool_declarations(self) -> list[dict]:
        out: list[dict] = []
        for server in self._servers.values():
            for t in server.tools():
                tool_name = str(t.get("name", "")).strip()
                if not tool_name:
                    continue
                flat = f"mcp__{server.name}__{tool_name}"
                schema = t.get("inputSchema") or {"type": "OBJECT",
                                                  "properties": {}}
                out.append({
                    "name": flat,
                    "description": (t.get("description") or
                                    f"MCP tool {tool_name} on {server.name}"),
                    "parameters": _normalise_schema(schema),
                })
        return out

    def names(self) -> set[str]:
        return {d["name"] for d in self.get_tool_declarations()}

    def has(self, name: str) -> bool:
        return name in self.names()

    def call(self, flat_name: str, args: dict | None = None,
             timeout: float = _DEFAULT_TIMEOUT) -> str:
        if not flat_name.startswith("mcp__"):
            raise MCPError(f"not an MCP tool: {flat_name}")
        parts = flat_name.split("__", 2)
        if len(parts) < 3:
            raise MCPError(f"malformed MCP tool name: {flat_name}")
        _, server_name, tool_name = parts
        server = self._servers.get(server_name)
        if server is None:
            raise MCPError(f"MCP server {server_name!r} is not running")
        return server.call(tool_name, args or {}, timeout=timeout)


def _normalise_schema(schema: dict) -> dict:
    """Gemini wants upper-case type names ("STRING", "OBJECT", ...). MCP
    tool schemas use JSON Schema lowercase. Convert in-place."""
    if not isinstance(schema, dict):
        return {"type": "OBJECT", "properties": {}}
    out = dict(schema)
    t = out.get("type")
    if isinstance(t, str):
        out["type"] = t.upper()
    props = out.get("properties")
    if isinstance(props, dict):
        out["properties"] = {k: _normalise_schema(v) for k, v in props.items()}
    items = out.get("items")
    if isinstance(items, dict):
        out["items"] = _normalise_schema(items)
    return out
