"""End-to-end smoke test: REAL processes, REAL HTTP, REAL state machine [D10].

Orchestrates: uvicorn API on :8000 (background loops live), the compiled Go
watcher polling the mock chain, and a webhook receiver verifying HMACs.

Flow: merchant+invoice -> Transfer appears on mock chain -> watcher ingests ->
processor credits -> sweep confirms at depth -> webhook delivered (after one
simulated 503) -> SETTLED -> replay dedupe check. Writes smoke_log.txt.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mocks import (  # noqa: E402
    HOT, PAYER, TX_HASH, USDC, HOOKS, chain_state, start_servers,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG: list[str] = []
API_KEY = "dev-internal-key"


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    LOG.append(line)
    print(line, flush=True)


def api(method: str, path: str, body: dict | None = None, headers: dict | None = None):
    req = urllib.request.Request(
        f"http://127.0.0.1:8000{path}",
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", **(headers or {})},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:  # surface the API's error detail
        raise RuntimeError(f"{method} {path} -> {e.code}: {e.read().decode()}") from e


def main() -> int:
    for f in ("smoke.db", "smoke_state.json", "smoke_log.txt"):
        p = os.path.join(ROOT, f)
        if os.path.exists(p):
            os.remove(p)
    import mocks
    mocks.HookReceiver.fail_next = True
    chain_state.update({"height": 113, "transfers": [], "getlogs_calls": 0, "secret": ""})
    HOOKS.clear()

    rpc_srv, hook_srv = start_servers()

    env = {**os.environ, "DATABASE_URL": f"sqlite:///{ROOT}/smoke.db",
           # Critical: the API must share the SAME hot wallet + contract as the
           # watcher, or invoices get a receiving address no payment will ever
           # match. RPC_URL lets the sweep read chain height for confirmations.
           # INTERNAL_API_KEY is pinned: pydantic-settings also reads .env, and
           # the developer's real key there would 401 the watcher's ingest.
           "RECEIVING_ADDRESS": HOT, "USDC_CONTRACT": USDC,
           "RPC_URL": "http://127.0.0.1:8555", "INTERNAL_API_KEY": API_KEY,
           # The mock webhook receiver runs on loopback — the SSRF guard [D22]
           # must be relaxed for this rig only.
           "WEBHOOK_ALLOW_PRIVATE_HOSTS": "true"}
    api_err = open(os.path.join(ROOT, "api_stderr.txt"), "w")
    api_proc = subprocess.Popen(
        [os.path.join(ROOT, ".venv", "Scripts", "python.exe"), "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", "8000", "--log-level", "info"],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=api_err,
    )
    time.sleep(4)  # lifespan starts the background loops

    # The smoke db was deleted above; create the schema before traffic arrives.
    # IMPORTANT: app.models must be imported first, otherwise Base.metadata is
    # empty and create_all is a silent no-op (this bit me once already).
    subprocess.run(
        [os.path.join(ROOT, ".venv", "Scripts", "python.exe"), "-c",
         "import app.models, app.db as d; d.Base.metadata.create_all(d.get_engine())"],
        cwd=ROOT, env=env, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    log("API up on :8000 (processor/sweep/dispatcher loops live)")

    try:
        ok = _scenario()
    finally:
        api_proc.terminate()
        rpc_srv.shutdown()
        hook_srv.shutdown()
        with open(os.path.join(ROOT, "smoke_log.txt"), "w") as f:
            f.write("\n".join(LOG))
    return 0 if ok else 1


def _scenario() -> bool:
    m = api("POST", "/merchants", {"name": "Smoke", "webhook_url": "http://127.0.0.1:9801/hook"})
    chain_state["secret"] = m["webhook_secret"]
    auth = {"X-API-Key": m["api_key"]}  # [D20] merchant-scoped endpoints need a key
    inv = api("POST", f"/merchants/{m['id']}/invoices",
              {"amount": "25.50", "description": "smoke order", "idempotency_key": "smoke-1"},
              headers=auth)["invoice"]
    payable = int(Decimal(inv["payable_amount"]) * 1_000_000)
    log(f"invoice {inv['public_id'][:8]}… payable={inv['payable_amount']} ({payable} base) status={inv['status']}")
    if inv["status"] != "AWAITING_PAYMENT":
        log(f"FAIL: expected AWAITING_PAYMENT, got {inv['status']}")
        return False

    # Payment appears at a block the watcher will scan AFTER its first-run
    # checkpoint (first run starts "now" and never rescans history), so we
    # queue it at head+2 and let the mock chain advance into it.
    chain_state["transfers"].append(
        {"tx": TX_HASH, "block": chain_state["height"] + 2, "contract": USDC, "amount": payable})
    watcher_log = open(os.path.join(ROOT, "watcher_stderr.txt"), "w")
    watcher = subprocess.Popen(
        [os.path.join(ROOT, "watcher", "mpay-watcher.exe")], cwd=ROOT,
        env={**os.environ, "RPC_URL": "http://127.0.0.1:8555", "API_URL": "http://127.0.0.1:8000",
             "INTERNAL_API_KEY": API_KEY, "RECEIVING_ADDRESS": HOT, "USDC_CONTRACT": USDC,
             "CONFIRMATIONS": "12", "POLL_SECONDS": "1", "STATE_FILE": os.path.join(ROOT, "smoke_state.json")},
        stdout=watcher_log, stderr=subprocess.STDOUT,
    )
    log("watcher started (poll=1s, confirmations=12)")

    deadline, status = time.time() + 22, inv["status"]
    while time.time() < deadline:
        time.sleep(1.0)
        cur = api("GET", f"/invoices/{inv['public_id']}")["invoice"]
        if cur["status"] != status:
            log(f"state: {status} -> {cur['status']}  paid={cur['paid_amount']}")
            status = cur["status"]
        if status == "SETTLED":
            break
    watcher.terminate()

    ok = True
    if status != "SETTLED":
        log(f"FAIL: final status {status} (expected SETTLED)")
        ok = False
    if not HOOKS:
        log("FAIL: no webhook received")
        ok = False
    else:
        if any(not h["sig_ok"] for h in HOOKS):
            log("FAIL: webhook with invalid HMAC signature")
            ok = False
        if HOOKS[0]["type"] != "invoice.confirmed" or (len(HOOKS) > 1 and HOOKS[1]["type"] != "invoice.settled"):
            log(f"FAIL: unexpected webhook sequence: {[h['type'] for h in HOOKS]}")
            ok = False

    # Acceptance: replaying the same event must not double-credit.
    dup = api("POST", "/internal/events", {"events": [{
        "tx_hash": TX_HASH, "log_index": 0, "block_number": 101,
        "contract_address": USDC, "from_address": PAYER, "to_address": HOT,
        "amount_base_units": payable}]}, {"X-Internal-Key": API_KEY})
    final = api("GET", f"/invoices/{inv['public_id']}")["invoice"]
    paid_after = int(Decimal(final["paid_amount"]) * 1_000_000)
    log(f"replay: duplicates={dup['duplicates']} paid_still={final['paid_amount']}")
    if dup["duplicates"] != 1 or paid_after != payable:
        log("FAIL: replay changed state!")
        ok = False

    log(f"webhooks: {[h['type'] for h in HOOKS]} (first 503'd -> retried ok)")
    log("SMOKE " + ("PASSED" if ok else "FAILED"))
    return ok


if __name__ == "__main__":
    sys.exit(main())
