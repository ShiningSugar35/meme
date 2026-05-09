# API Field Mapping Notes (Phase E+F)

## GMGN API

### Trenches (`/api/v1/trenches`)
- TODO: Confirm real GMGN response structure. Currently maps `data.tokens[]` → normalized tokens.
- Adapter: `_normalize_token_data()` in gmgn_real.py:222-228

### Token Snapshot (`/api/v1/token/price/{mint}`)
- TODO: Confirm field names for price_usd, price_sol, liquidity_usd, sol_side_liquidity
- Adapter: `fetch_latest_price()` in gmgn_real.py:342-355

### Kline (`/api/v1/token/kline/{mint}`)
- TODO: Confirm field names for open, high, low, close, buy_volume, sell_volume
- Adapter: `fetch_kline()` in gmgn_real.py:289-299

### Field alias resolutions:
| Expected | Possible GMGN field |
|----------|-------------------|
| token_mint | address, token_mint |
| pool_address | pool, pool_address |
| pool_created_at | pool_created_at, pool_created_timestamp |
| top_10_holder_rate | top_10_holder_rate, top10_holder_rate |
| price_usd | price_usd, price |
| price_sol | price_sol, sol_price |

## Jupiter API

### Quote (`/v6/quote`)
- priceImpactPct: confirmed field name (Jupiter v6)
- routePlan: confirmed array of swap steps
- outAmount: confirmed (raw lamports string)
- otherAmountThreshold: confirmed (slippage-protected min out)

### Build Swap (`/v6/swap`)
- swapTransaction: base64-encoded transaction (NOT logged in provider_requests)
- instructions: array of swap instructions

## Jito API

### Tip Floor (`/api/v1/bundles/tip_floor`)
- landed_tips_50th_percentile: confirmed
- landed_tips_75th_percentile: confirmed
- landed_tips_95th_percentile: confirmed

### Send (`/api/v1/bundles`)
- BLOCKED in ONLINE_READONLY/MOCK modes
- LIVE mode uses tip ladder: 50th → 75th → 95th (max 2 retries)
- InstructionError: no retry
- tip too low: retry with higher tip

## Ankr RPC

- URL pattern: `https://solana-mainnet.g.alchemy.com/v2/{ANKR_API_KEY}`
- Methods: getBalance, getTokenAccountsByOwner, getSignatureStatuses
- No sendTransaction/sendRawTransaction support (blocked for safety)
