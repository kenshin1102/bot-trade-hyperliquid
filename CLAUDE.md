# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
```

## 5. Project: Hyperliquid Independent Strategy Bot

**Philosophy:** Không copy ai. Hệ thống tự đọc thị trường, tự sinh signal, tự kiểm soát rủi ro.

Pipeline:
```
Market Data (Candles / Trades / OI / Funding)
        ↓
  data-backfill (REST historical) + market-ws-worker (realtime)
        ↓
  Feature Engine (EMA, ATR, volume_zscore, oi_change, funding_percentile)
        ↓
  Regime Detector (NO_TRADE / SMALL / NORMAL / STRONG)
        ↓
  Breakout_V1 Signal Engine
        ↓
  Risk Engine (pre-trade gate)
        ↓
  Paper Executor (spread + fee + slippage model)
        ↓
  Telegram Alert + Daily Report
```

### Architecture (`src/`)

| Module | Responsibility |
|---|---|
| `config/settings.py` | Load `config.yaml` + `.env` (pydantic-settings) |
| `monitoring/notifier.py` | Telegram + Log notifier, never crashes bot |
| `storage/db.py` | SQLAlchemy 2.0 models: Candle, FundingRate, AssetContext, Feature, StrategySignal, PaperPosition, EquitySnapshot |
| `storage/repository.py` | CRUD per model |
| `hyperliquid/client.py` | REST: candles, funding_history, asset_contexts, recent_trades, allMids |
| `hyperliquid/ws_client.py` | WebSocket: candle + trades + allMids (NOT webData2) |
| `data/backfill.py` | Fetch historical data → DB, runs every hour |
| `market/ws_worker.py` | Realtime WS → feature cache + DB |
| `strategy/feature_engine.py` | Compute EMA20/50, ATR, volume_zscore, oi_change_pct, funding_percentile |
| `strategy/regime.py` | Regime score 0-100 → NO_TRADE / SMALL / NORMAL / STRONG |
| `strategy/breakout.py` | Breakout_V1: break high/low + volume spike + OI up → StrategySignal |
| `execution/paper.py` | Paper trade: fill with spread+fee+slippage, SL/TP, PnL tracking |
| `signal/daily_report.py` | Daily Telegram report |

### Key Rules
- **Everything defaults to PAPER.** Live needs: mode=live + HL_PRIVATE_KEY + LIVE_TRADING_CONFIRMED.
- Hyperliquid API: REST `https://api.hyperliquid.xyz/info` + WS `wss://api.hyperliquid.xyz/ws`.
- NOT copy trade — no webData2, no wallet monitoring, no trader scoring.
- Every signal must have: coin, side, entry, SL, TP, regime_score, reason string.
- Paper fill model must include fee + slippage — not raw close price.
- Risk gate runs before paper executor. Reject conditions saved as StrategySignal(status=REJECTED).
- Docker ports: Postgres **5434**, Redis **6381** (avoid conflict with other bots on server).
- Deploy server: `toannx@35.239.129.132`, dir `~/bot-trade-hyperliquid`.
