"""Mock chain RPC + merchant webhook receiver for the smoke test [D9-style].

- MockRPC: JSON-RPC on :8555 — eth_blockNumber (climbs with each getLogs poll,
  simulating chain progress) and eth_getLogs returning queued USDC Transfers.
- HookReceiver: merchant webhook on :9801 — verifies the HMAC signature like a
  real merchant would, fails the FIRST delivery with 503 to exercise the retry
  path, then accepts.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOT = "0x0000000000000000000000000000000000000aa9"
USDC = "0x0000000000000000000000000000000000000bb8"
PAYER = "0x0000000000000000000000000000000000000cc7"
TX_HASH = "0xsmoketx0000000000000000000000000000000000000000000000000000000001"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

chain_state = {"height": 113, "transfers": [], "getlogs_calls": 0, "secret": ""}
HOOKS: list[dict] = []  # captured webhook deliveries


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


class MockRPC(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        method = body.get("method")
        if method == "eth_blockNumber":
            # The chain advances on every height poll (watcher + sweep both
            # poll); getLogs does NOT advance it — otherwise height only moves
            # when the watcher already has room to scan (deadlock).
            chain_state["height"] += 2
            result = hex(chain_state["height"])
        elif method == "eth_getLogs":
            chain_state["getlogs_calls"] += 1
            result = [
                {
                    "blockNumber": hex(t["block"]),
                    "blockHash": "0xblockhash1",
                    "transactionHash": t["tx"],
                    "logIndex": 0,
                    "address": t["contract"],
                    "topics": [
                        TRANSFER_TOPIC,
                        "0x" + "0" * 24 + PAYER[2:],
                        "0x" + "0" * 24 + HOT[2:],
                    ],
                    "data": "0x" + t["amount"].to_bytes(32, "big").hex(),
                }
                for t in chain_state["transfers"]
            ]
        else:
            result = None
        resp = json.dumps({"jsonrpc": "2.0", "id": body.get("id", 1), "result": result}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)

    def log_message(self, *a):  # silence per-request noise
        pass


class HookReceiver(BaseHTTPRequestHandler):
    fail_next = True

    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers["Content-Length"]))
        if HookReceiver.fail_next:
            HookReceiver.fail_next = False
            log("hook receiver: 503 (simulated outage) — testing retry path")
            self.send_response(503)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        expected = hmac.new(chain_state["secret"].encode(), body, hashlib.sha256).hexdigest()
        got = self.headers.get("X-Mpay-Signature", "")
        payload = json.loads(body)
        HOOKS.append({"type": payload["type"], "sig_ok": hmac.compare_digest(expected, got)})
        log(f"hook receiver: {payload['type']} sig_ok={hmac.compare_digest(expected, got)}")
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):  # silence
        pass


def start_servers():
    rpc_srv = ThreadingHTTPServer(("127.0.0.1", 8555), MockRPC)
    hook_srv = ThreadingHTTPServer(("127.0.0.1", 9801), HookReceiver)
    threading.Thread(target=rpc_srv.serve_forever, daemon=True).start()
    threading.Thread(target=hook_srv.serve_forever, daemon=True).start()
    log("mock chain :8555 and webhook receiver :9801 up")
    return rpc_srv, hook_srv
