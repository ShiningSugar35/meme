# Solana Meme Trading Bot v1 (Prototype)

This repository is a production-oriented prototype for a Solana meme token trading system. It includes a FastAPI backend and a React + Vite frontend. The system is designed to support both simulation and live trading (with live trading disabled by default and DRY_RUN enabled by default).

**Important:** 
- Do NOT commit real private keys or API secrets to this repository. Use the `.env` file (not committed) for secrets.
- Live trading is disabled by default: `LIVE_TRADING_ENABLED=false`.
- All providers run in DRY_RUN mode by default: `DRY_RUN=true`. No real transactions will be broadcast.

## Setup

### 1. Create and Activate Project Virtual Environment

Windows (using project .venv):

```powershell
python -m venv .venv
.\.venv\Scripts\Activate
# Verify: python -c "import sys; print(sys.executable)"
# Should output: D:\meme\.venv\Scripts\python.exe
```

Linux/macOS:

```bash
python3 -m venv .venv
source .venv/bin/activate
# Verify: python -c "import sys; print(sys.executable)"
```

### 2. Install Dependencies

```bash
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

### 3. Configure Environment

Copy `.env.example` to `.env` and customize:

```bash
cp .env.example .env
```

Edit `.env` with your settings:

- `LIVE_TRADING_ENABLED=false` (default, required)
- `DRY_RUN=true` (default, required)
- `GMGN_API_KEY=` (optional for testing)
- `JITO_BASE_URL=https://mainnet.block-engine.jito.wtf` (optional)
- `WALLET_PUBLIC_KEY=` (your public key, for reference only)
- `WALLET_PRIVATE_KEY=` (NEVER use a real key in .env for testing)

**WARNING:** Never set `WALLET_PRIVATE_KEY` to a real private key in `.env`. Even with `DRY_RUN=true`, the system will refuse to broadcast if this is detected.

### 4. Start Backend

```bash
python -m uvicorn backend.app.main:app --reload
```

Health check: `GET /health`

## Testing

Run all tests (using project .venv):

```bash
python -m pytest -q
```

Run specific test suite:

```bash
python -m pytest -q backend/app/tests/test_trading_pipeline.py -vv -s --tb=long
```

Run business invariant tests:

```bash
python -m pytest -q backend/app/tests/test_business_invariants.py -vv -s --tb=long
```

Run provider dry-run tests:

```bash
python -m pytest -q backend/app/tests/test_provider_dry_run.py -vv -s --tb=long
```

## Safety

- All providers default to `DRY_RUN=true`. No real transactions will be broadcast.
- `LIVE_TRADING_ENABLED=false` by default. Override only if you understand the risks.
- All API keys and secrets are read from `.env` and masked in logs.
- `provider_requests` table never stores complete API keys or private keys.
