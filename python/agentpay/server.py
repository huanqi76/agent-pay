"""A payment-gated JSON-RPC tool server (MCP-style `tools/list` and `tools/call`).

Reading the tool list is free. Calling a tool needs an `X-Payment` header holding a signed
voucher; without one the server answers HTTP 402 and says what it wants.
"""

from __future__ import annotations

import argparse
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from web3 import Web3

from .chain import Chain
from .gate import PaymentGate, PaymentRequired, VoucherStore
from .tools import Tool, default_tools

MAX_BODY = 1_000_000


def make_server(gate: PaymentGate, tools: dict[str, Tool], host="127.0.0.1", port=8402):
    prices = {name: tool.price for name, tool in tools.items()}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # quiet by default
            pass

        def _send(self, status: int, body: dict):
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _rpc_error(self, status: int, rpc_id, code: int, message: str, **extra):
            self._send(status, {"jsonrpc": "2.0", "id": rpc_id,
                                "error": {"code": code, "message": message, **extra}})

        def do_GET(self):
            if self.path == "/.well-known/agent-pay":
                self._send(200, gate.requirements(prices))
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            if self.path != "/mcp":
                return self._send(404, {"error": "not found"})
            length = int(self.headers.get("Content-Length", 0))
            if length > MAX_BODY:
                return self._send(413, {"error": "body too large"})
            try:
                req = json.loads(self.rfile.read(length))
                method, rpc_id, params = req["method"], req.get("id"), req.get("params") or {}
            except (ValueError, KeyError, TypeError):
                return self._rpc_error(400, None, -32700, "invalid JSON-RPC request")

            if method == "tools/list":
                return self._send(200, {"jsonrpc": "2.0", "id": rpc_id, "result": {"tools": [
                    {"name": t.name, "description": t.description, "inputSchema": t.input_schema,
                     "price": str(t.price)} for t in tools.values()]}})
            if method != "tools/call":
                return self._rpc_error(404, rpc_id, -32601, f"unknown method {method}")

            tool = tools.get(params.get("name"))
            if tool is None:
                return self._rpc_error(404, rpc_id, -32602, "unknown tool")
            try:
                with gate.charge(self.headers.get("X-Payment"), tool.price):
                    text = tool.run(params.get("arguments") or {})
            except PaymentRequired as exc:
                return self._rpc_error(402, rpc_id, -32001, exc.reason,
                                       data=gate.requirements(prices) | {"need": str(exc.need)})
            except (KeyError, ValueError, TypeError) as exc:
                return self._rpc_error(400, rpc_id, -32602, f"bad arguments: {exc}")
            self._send(200, {"jsonrpc": "2.0", "id": rpc_id,
                             "result": {"content": [{"type": "text", "text": text}]}})

    return ThreadingHTTPServer((host, port), Handler)


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("command", nargs="?", choices=["serve", "settle"], default="serve")
    p.add_argument("--rpc-url", default=os.environ.get("RPC_URL", "http://127.0.0.1:8545"))
    p.add_argument("--escrow", default=os.environ.get("ESCROW_ADDRESS"), required=False)
    p.add_argument("--token", default=os.environ.get("TOKEN_ADDRESS"), required=False)
    p.add_argument("--price", type=int, default=10_000, help="base units per call (10000 = 0.01 USDC)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8402)
    p.add_argument("--vouchers", type=Path, default=Path("vouchers.json"))
    p.add_argument("--auto-settle-secs", type=int, default=0, help="redeem vouchers on this interval")
    args = p.parse_args()

    key = os.environ.get("PROVIDER_PRIVATE_KEY")
    if not (key and args.escrow and args.token):
        p.error("set PROVIDER_PRIVATE_KEY and pass --escrow and --token (or ESCROW_ADDRESS, TOKEN_ADDRESS)")

    chain = Chain(Web3(Web3.HTTPProvider(args.rpc_url)), args.escrow, args.token)
    gate = PaymentGate(chain, key, VoucherStore(args.vouchers))

    if args.command == "settle":
        for payer, amount, tx in gate.settle():
            print(f"claimed {amount} from {payer}: {tx}")
        return

    server = make_server(gate, default_tools(args.price), args.host, args.port)
    if args.auto_settle_secs:
        stop = threading.Event()

        def loop():
            while not stop.wait(args.auto_settle_secs):
                gate.settle()

        threading.Thread(target=loop, daemon=True).start()
    print(f"serving on http://{args.host}:{args.port}  provider {gate.provider}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        gate.settle()


if __name__ == "__main__":
    main()
