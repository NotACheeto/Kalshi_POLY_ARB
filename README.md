# Polymarket ↔ Kalshi Cross-Exchange Arbitrage Engine

Production-grade, low-latency, security-hardened quantitative arbitrage engine designed to trade between **Polymarket** and **Kalshi**, with a primary focus on ultra-short duration contracts (**<= 24 hours to resolution**).

Built on the foundational trading rule:
> **NEVER knowingly place a trade unless the conservative estimated expected profit is strictly positive after exchange fees, spreads, slippage buffers, capital costs, and adverse execution allowances.**

---

## Key System Architecture

```
Kalshi_POLY_ARB/
├── config/
│   ├── config.yaml               # Validated production configuration (Safe DRY_RUN default)
│   └── .env.example              # Template for API credentials (zero secrets)
├── src/
│   ├── models.py                 # Normalized domain types (OrderBook, Market, Opportunity)
│   ├── config.py                 # Pydantic configuration loader & validation
│   ├── bot.py                    # Orchestrator: discovery, real-time scanning, execution
│   ├── math/
│   │   ├── fee_calculator.py     # Kalshi CFTC quadratic taker formula & Polymarket fee model
│   │   └── ev_calculator.py      # Hard Positive-EV gate, sizing, and friction modeling
│   ├── matcher/
│   │   └── market_matcher.py     # Deterministic market matcher (strike, date, outcome)
│   ├── clients/
│   │   ├── kalshi_client.py      # Kalshi v2 client: RSA-SHA256 (PSS), connection pool, orderbook_fp
│   │   └── polymarket_client.py  # Polymarket Gamma & CLOB client: EIP-712 orders, connection pool
│   └── execution/
│       ├── risk_manager.py       # Concurrency pair locking, exposure limits, kill switch
│       ├── reconciler.py         # Append-only Write-Ahead Log (WAL) & startup crash recovery
│       └── leg_risk_fsm.py       # 7-state atomic two-leg FSM with partial fill hedging
├── tests/                        # 18 unit, integration, simulation, and security tests
├── main.py                       # CLI entry point (--dry-run, --live, --scan-once)
└── requirements.txt              # Lean async stack (no bloated web dashboards)
```

---

## Mathematical Formulation

### Synthetic Complementary Arbitrage

Because Polymarket (Polygon ERC-1155) and Kalshi (CFTC-cleared USD ledger) do not share a clearinghouse, tokens cannot cross exchanges to deliver against short positions. The engine instead constructs **synthetic complementary bundles** held to settlement:

$$\text{Strategy 1: Buy YES on Polymarket} + \text{Buy NO on Kalshi}$$
$$\text{Strategy 2: Buy NO on Polymarket} + \text{Buy YES on Kalshi}$$

In both outcomes ($\text{YES}$ or $\text{NO}$), exactly one contract pays $\$1.00$ while the other pays $\$0.00$. The guaranteed gross payout is **strictly $\$1.00$ per contract unit**.

### Conservative Net Expected Value (EV)

$$\text{Gross Cost per Unit } C_{\text{gross}} = P_1^{\text{ask}} + P_2^{\text{ask}}$$
$$\text{Gross Profit} = Q \times (\$1.00 - C_{\text{gross}})$$
$$\text{Total Friction} = \text{Fee}_{\text{poly}} + \text{Fee}_{\text{kalshi}} + \text{Cost}_{\text{slippage}} + \text{Cost}_{\text{adverse}} + \text{Cost}_{\text{capital}}$$
$$\text{Conservative Net EV} = \text{Gross Profit} - \text{Total Friction}$$

### Hard Safety Gate

A trade is executed if and only if all conditions are met:
1. $\text{Conservative Net EV} \ge \text{MinRequiredProfit}$ ($\ge \$0.25$ default)
2. $\frac{\text{Conservative Net EV}}{Q \times C_{\text{gross}}} \ge \text{MinNetEdgePct}$ ($\ge 1.50\%$ default)
3. $\text{Quote Age} \le 2.0\text{ seconds}$ (rejection of stale data)
4. $5\text{ minutes} \le T_{\text{resolution}} \le 24\text{ hours}$ (short-duration window)

---

## Two-Leg Execution State Machine

```
DETECTED
   ↓ (Acquire Pair Lock)
VALIDATING
   ↓ (Pre-Flight Recheck)
LEG1_SUBMITTED ──────────────→ ABORTED (Zero Fill / Timeout -> 0 Exposure)
   ↓ (Fill Confirmation)
LEG1_PARTIAL / LEG1_FILLED (Cancel Unfilled Remainder)
   ↓ (Submit Exact Filled Quantity)
LEG2_SUBMITTED
   ↓ (Fill Confirmation)
COMPLETED (Hedge Locked) OR HEDGING (Auto-Unwind if Unhedged)
```

- **Zero Exposure on Leg 1 Timeout**: If Leg 1 does not fill within timeout, it is cancelled immediately. Exposure = 0.
- **Partial Fill Coordination**: If Leg 1 fills 8 out of 20 contracts, the remaining 12 are immediately cancelled. Leg 2 is submitted for **precisely 8 contracts**.
- **Automated Emergency Unwind**: If Leg 2 fails to fill within timeout, Leg 1 is unwound on the market within a strict loss tolerance to eliminate unhedged overnight risk.

---

## Installation & Setup

### 1. Install Dependencies

```powershell
pip install -r requirements.txt
```

### 2. Configure Credentials

Copy the template:
```powershell
cp config/.env.example .env
```

Populate your `.env` (automatically ignored by Git):
```bash
# Polymarket
POLYMARKET_API_KEY="your_api_key"
POLYMARKET_SECRET="your_secret"
POLYMARKET_PASSPHRASE="your_passphrase"
POLYMARKET_PRIVATE_KEY="your_polygon_wallet_private_key"

# Kalshi
KALSHI_API_KEY_ID="your_kalshi_key_id"
KALSHI_PRIVATE_KEY_PATH="config/kalshi_key.pem"
```

---

## Running the Engine

### Paper Trading / Dry Run Mode (Default)

Runs against live real-time market data without placing real capital at risk:

```powershell
# Continuous live market scanning:
python main.py

# Single-cycle market scan and report:
python main.py --scan-once
```

### Live Trading Mode

Requires explicit two-key authorization:

1. Enable live mode in `config/config.yaml`:
   ```yaml
   execution:
     dry_run: false
     live_trading_confirmed: true
   ```
2. Run with the mandatory confirmation flag:
   ```powershell
   python main.py --live --confirm-live-risk
   ```

---

## Running Tests

Execute the comprehensive test suite:

```powershell
python -m pytest tests/ -v
```

18/18 tests pass across EV math, fee calculations, deterministic matching, concurrency locks, state machine transitions, crash recovery journaling, and credential sanitization.
