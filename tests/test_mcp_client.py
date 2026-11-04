"""
Tests for the MCP client. A tiny stub MCP server implemented in-line
speaks JSON-RPC 2.0 over stdio, exposes two toy tools (echo + add), and
lets us exercise handshake, tools/list, tools/call, error path, and
tool-declaration normalisation without any external MCP server.
"""
from __future__ import annotations

import json
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from core.mcp_client import MCPRegistry, MCPServer, MCPError


_STUB_SERVER = textwrap.dedent('''
    import json, sys

    def write(msg):
        sys.stdout.write(json.dumps(msg) + "\\n"); sys.stdout.flush()

    TOOLS = [
        {"name": "echo",
         "description": "Echo back the input.",
         "inputSchema": {"type": "object",
                          "properties": {"text": {"type": "string"}}}},
        {"name": "add",
         "description": "Add two numbers.",
         "inputSchema": {"type": "object",
                          "properties": {"a": {"type": "number"},
                                         "b": {"type": "number"}}}},
    ]

    for line in sys.stdin:
        line = line.strip()
        if not line: continue
        msg = json.loads(line)
        method = msg.get("method")
        rid    = msg.get("id")
        params = msg.get("params") or {}
        if method == "initialize":
            write({"jsonrpc":"2.0","id":rid,"result":{
                "protocolVersion":"2024-11-05",
                "capabilities":{"tools":{"listChanged":False}},
                "serverInfo":{"name":"stub","version":"1.0"}}})
        elif method == "notifications/initialized":
            continue  # notification: no response
        elif method == "tools/list":
            write({"jsonrpc":"2.0","id":rid,"result":{"tools": TOOLS}})
        elif method == "tools/call":
            name = params.get("name"); args = params.get("arguments") or {}
            if name == "echo":
                write({"jsonrpc":"2.0","id":rid,"result":{
                    "content":[{"type":"text","text":str(args.get("text",""))}]}})
            elif name == "add":
                total = (args.get("a",0) or 0) + (args.get("b",0) or 0)
                write({"jsonrpc":"2.0","id":rid,"result":{
                    "content":[{"type":"text","text":str(total)}]}})
            elif name == "crash":
                write({"jsonrpc":"2.0","id":rid,"result":{
                    "isError": True,
                    "content":[{"type":"text","text":"synthetic failure"}]}})
            else:
                write({"jsonrpc":"2.0","id":rid,
                        "error":{"code":-32601,"message":f"no tool {name}"}})
        else:
            write({"jsonrpc":"2.0","id":rid,
                    "error":{"code":-32601,"message":method}})
''').strip()


class MCPStubTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="mcp-"))
        self.stub_path = self.tmp / "stub_server.py"
        self.stub_path.write_text(_STUB_SERVER, encoding="utf-8")

    def _server(self):
        return MCPServer(
            name="stub",
            command=[sys.executable, "-u", str(self.stub_path)],
            logger=lambda m: None,
        )

    def test_handshake_and_tool_listing(self):
        s = self._server()
        try:
            s.start()
            names = {t["name"] for t in s.tools()}
            self.assertEqual(names, {"echo", "add"})
        finally:
            s.stop()

    def test_call_returns_text(self):
        s = self._server()
        try:
            s.start()
            self.assertEqual(s.call("echo", {"text": "hi"}), "hi")
            self.assertEqual(s.call("add", {"a": 2, "b": 3}), "5")
        finally:
            s.stop()

    def test_error_result_raises(self):
        s = self._server()
        try:
            s.start()
            with self.assertRaises(MCPError):
                s.call("crash", {})
        finally:
            s.stop()

    def test_registry_flattens_and_dispatches(self):
        cfg_path = self.tmp / "mcp_servers.json"
        cfg_path.write_text(json.dumps({"servers": [
            {"name": "stub",
             "command": [sys.executable, "-u", str(self.stub_path)]},
        ]}), encoding="utf-8")
        reg = MCPRegistry(logger=lambda m: None, notify=lambda m: None)
        try:
            reg.load_from_config(cfg_path)
            names = reg.names()
            self.assertIn("mcp__stub__echo", names)
            self.assertIn("mcp__stub__add", names)
            self.assertTrue(reg.has("mcp__stub__echo"))
            self.assertEqual(reg.call("mcp__stub__echo", {"text": "yo"}), "yo")
            self.assertEqual(reg.call("mcp__stub__add", {"a": 10, "b": 7}), "17")
            # Declarations have Gemini-style uppercase types
            decls = reg.get_tool_declarations()
            self.assertTrue(any(d["name"] == "mcp__stub__add"
                                and d["parameters"]["type"] == "OBJECT"
                                for d in decls))
        finally:
            reg.shutdown()


if __name__ == "__main__":
    unittest.main(verbosity=2)
