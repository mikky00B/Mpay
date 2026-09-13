# Quickstart

From zero to a live, payable invoice in about ten minutes.

## 0. Requirements

- Python 3.11+
- Go 1.21+ (for the chain watcher)
- An EVM RPC endpoint — [Alchemy](https://www.alchemy.com) and [Infura](https://www.infura.io) both have free tiers (Sepolia testnet is fine to start; Alchemy's free tier caps `eth_getLogs` at 10 blocks, which Mpay handles automatically)
- A wallet with some test ETH for gas ([Sepolia faucet](https://sepoliafaucet.com)) and test USDC ([Circle faucet](https://faucet.circle.com))

## 1. Install and run the API

```bash
git clone https://github.com/mikky00B/Mpay && cd Mpay
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt      # Windows; Linux/macOS: .venv/bin/pip

# Create the database schema (SQLite by default):
.venv/Scripts/python -c "import app.models, app.db as d; d.Base.metadata.create_all(d.get_engine())"

.venv/Scripts/python -m uvicorn app.main:app --port 8000
```

Verify: `curl http://127.0.0.1:8000/health` → `{"ok":true}`

## 2. Configure

Create a `.env` file (or export env vars). Minimum for a Sepolia test run:

```ini
RECEIVING_ADDRESS=0xYourHotWalletAddress     # where payments land
USDC_CONTRACT=0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238   # Sepolia USDC
CHAIN_ID=11155111                            # encoded into payment QRs — MUST match your network
RPC_URL=https://eth-sepolia.g.alchemy.com/v2/YOUR_KEY
INTERNAL_API_KEY=some-long-random-string     # shared with the watcher only
CONFIRMATION_THRESHOLD=3                     # blocks before an invoice is CONFIRMED
```

Restart the API after creating `.env`. All settings are listed in [Operations](operations.md).

## 3. Build and run the watcher

```bash
cd watcher
go build -o mpay-watcher.exe .

RPC_URL=https://eth-sepolia.g.alchemy.com/v2/YOUR_KEY \
API_URL=http://127.0.0.1:8000 \
RECEIVING_ADDRESS=0xYourHotWalletAddress \
USDC_CONTRACT=0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238 \
INTERNAL_API_KEY=some-long-random-string \
CONFIRMATIONS=3 POLL_SECONDS=10 MAX_BLOCKS=10 \
STATE_FILE=state.json ./mpay-watcher.exe
```

You should see `first run: starting from head NNNNN`. The watcher only sees blocks mined **after** it starts — start it before you expect payments.

## 4. Create a merchant and an invoice

```bash
curl -X POST http://127.0.0.1:8000/merchants -H "Content-Type: application/json" \
  -d '{"name":"My Shop","webhook_url":"https://your-app.example/hooks/mpay"}'
```

Response (store both secrets — they are shown **once**):

```json
{"id":1, "webhook_secret":"whsec_…", "api_key":"mpay_sk_…"}
```

```bash
curl -X POST http://127.0.0.1:8000/merchants/1/invoices \
  -H "Content-Type: application/json" -H "X-API-Key: mpay_sk_…" \
  -d '{"amount":"25.50","description":"Order #42"}'
```

## 5. Get paid

The invoice response contains `payable_amount` (e.g. `25.500042` — the unique matching amount) and `public_id`. Send your customer the payment link:

```
http://localhost:8000/pay/{public_id}
```

The checkout page shows a scannable QR (wallet apps pre-fill token, network, and exact amount) and live status. Once the transfer is `CONFIRMATION_THRESHOLD` blocks deep, your `webhook_url` receives a signed `invoice.confirmed` — see [Webhooks](webhooks.md) for verification. The invoice then flips to `SETTLED`.

## 6. Run the test suite

```bash
pytest -q                    # 48 behavior tests against a real (temp-file) DB
python scripts/smoke_e2e.py  # full-system E2E: real API + watcher vs a mock chain
```

---

Next: the [API reference](api.md) for full endpoint details, or [Operations](operations.md) before taking real payments.
