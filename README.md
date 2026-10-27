# Nano Empire Tollbooth (`nano-empire-tollbooth`)

Python decorator and protocol SDK for wrapping Python functions and HTTP endpoints in `HTTP 402 Payment Required` machine paywalls with paper-mode simulation and live `x402` Base USDC verification hooks.

![PyPI: nano-empire-tollbooth v0.2.2](https://img.shields.io/badge/pypi-nano--empire--tollbooth_v0.2.2-3775a9)
![Mode: Paper Default + Live x402 Hook](https://img.shields.io/badge/mode-paper_default_%2B_live_x402_verifier-3b82f6)
![Base Mainnet Settlement: Block 51961194](https://img.shields.io/badge/Base_Mainnet-1_self--test_settled-10b981)
![Outside Paying Customers: 0](https://img.shields.io/badge/outside_paying_customers-0-f59e0b)
![License: MIT](https://img.shields.io/badge/license-MIT-64748b)

---

## 1. Install & Run Today

```bash
pip install nano-empire-tollbooth
```

```python
from nano_empire_tollbooth import monetize, TollboothConfig, create_tollbooth

# 1. Decorator API (defaults to paper_mode=True for local dev/testing)
@monetize(price_usd=0.05, capability="recall-feed")
def get_recall_feed(limit: int = 10) -> dict:
    return {"ok": True, "count": limit}

print(get_recall_feed(5))

# 2. Explicit Tollbooth instance with custom x402 verifier hook
config = TollboothConfig(
    toll_per_message_usd=0.05,
    paper_mode=False,
    require_payment_before_route=True,
)
booth = create_tollbooth(config)
```

### Run the Test Suite Locally
```bash
git clone https://github.com/Nano-Empire-Ai/nano-empire-tollbooth.git
cd nano-empire-tollbooth
pytest -v
```

---

## 2. Machine-Readable Discovery Links

- **Canonical LLM Index (`llms.txt`):** [`https://www.nanoempireai.com/llms.txt`](https://www.nanoempireai.com/llms.txt)
- **A2A Agent Card (`v1.5.0`):** [`https://www.nanoempireai.com/.well-known/agent-card.json`](https://www.nanoempireai.com/.well-known/agent-card.json)
- **Poppy / PAP Manifest (`PAP/1.0`):** [`https://www.nanoempireai.com/.well-known/poppy.json`](https://www.nanoempireai.com/.well-known/poppy.json)
- **Commercial Offers (`offers.json`):** [`https://www.nanoempireai.com/offers.json`](https://www.nanoempireai.com/offers.json)
- **OpenAPI 3.1 Spec:** [`https://www.nanoempireai.com/.well-known/openapi.json`](https://www.nanoempireai.com/.well-known/openapi.json)

---

## 3. Pricing & Payment Rails

- **SDK Usage:** Open-source MIT package (`pip install nano-empire-tollbooth`). Paper-mode metering is free locally; live `x402` settlement connects via `booth.set_x402_verifier(...)` against your facilitator of choice (`https://x402.org/facilitator` on Base Mainnet `eip155:8453`).
- **Live Reference Endpoints Metered by Nano Empire:**
  - `GET https://recallguard-api.vercel.app/api/v1/feed` — **$0.05 USDC** (`50000` atomic units on Base `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`)
  - `POST https://recallguard-api.vercel.app/api/v1/match` — **$0.10 USDC** (`100000` atomic units on Base)

---

## 4. Settlement Proof & Honest Volume Ledger

- **On-Chain Settlement Proof (Base Mainnet Block `51961194`):**
  - [`0x0581b819b2e5b481cbbcfb8fe81e6fe57a0d0ef9c6f8fc04952164761586da64`](https://basescan.org/tx/0x0581b819b2e5b481cbbcfb8fe81e6fe57a0d0ef9c6f8fc04952164761586da64) (`0.05 USDC` settled to `0x90E23A7b6F6494A879d05DDC4278D0e55Dec1e01`).
- **Volume Disclosure:** `1` verified self-test settlement on Base Mainnet; `0` outside paying customers to date.

---

## 5. License & Contact

- **License:** MIT
- **Contact:** Robert Lambert (`rob@nanoempireai.com`) · [`https://www.nanoempireai.com`](https://www.nanoempireai.com)
