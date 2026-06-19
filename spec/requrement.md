# Requirement — Hyperliquid Independent Strategy Bot

> Phiên bản: 2.0
> Ngày cập nhật: 2026-06-19
> Scope: hệ thống giao dịch độc lập trên Hyperliquid Perp — tự sinh signal từ market data, không copy ai.
> Default: **paper-first**. Live chỉ bật thủ công với triple-arm gate.

---

## 0. Định nghĩa hệ thống

Hệ thống này **không phải copy trade**. Không watch wallet người khác, không mirror position, không dùng trader PnL làm signal.

Thay vào đó:

```
Market Data (Candles / Trades / OI / Funding)
        ↓
  data-backfill (REST lấy historical, chạy 1 lần + update mỗi giờ)
  market-ws-worker (WS streaming realtime)
        ↓
  Feature Engine (EMA, ATR, volume z-score, OI change, funding rate)
        ↓
  Regime Detector (NO_TRADE / SMALL / NORMAL / STRONG)
        ↓
  Breakout_V1 Signal Engine
        ↓
  Risk Engine (pre-trade gate: spread, funding, daily loss, max positions)
        ↓
  Paper Executor (spread + fee + slippage model)
        ↓
  Telegram Alert + Daily Report
```

**Mục tiêu MVP:** chạy ổn định 24/7 ở paper mode, sinh signal Breakout_V1, log đầy đủ lý do vào/ra, báo cáo hằng ngày.

---

## 1. Kiến trúc module (`src/`)

Giữ nguyên pattern từ `bot-copy-trade-hyperliquid` — cùng stack, cùng cấu trúc thư mục.

| Module | Vai trò |
|---|---|
| `config/settings.py` | Load `config.yaml` + `.env` (pydantic-settings). Dataclass cho từng section. |
| `monitoring/notifier.py` | Telegram + Log notifier. Copy y chang từ bot cũ, không thay đổi. |
| `monitoring/log_setup.py` | Logging setup. Copy y chang. |
| `hyperliquid/client.py` | REST client — **mở rộng** từ bot cũ: thêm `get_candles`, `get_funding_history`, `get_asset_contexts`, `get_recent_trades`. |
| `hyperliquid/ws_client.py` | WS client — **thay đổi**: thay `webData2` bằng `candle` + `trades` + `allMids`. |
| `storage/db.py` | SQLAlchemy 2.0 models: `Candle`, `FundingRate`, `AssetContext`, `Feature`, `StrategySignal`, `PaperPosition`, `PaperTrade`, `EquitySnapshot`. |
| `storage/repository.py` | CRUD cho từng model. |
| `data/backfill.py` | Fetch historical candles + funding + OI từ REST, lưu DB. Chạy 1 lần rồi update mỗi giờ. |
| `market/ws_worker.py` | Subscribe WS: `candle` (15m, 1h) + `trades` + `allMids`. Ghi vào DB + update feature cache. |
| `strategy/feature_engine.py` | Compute features từ candle/OI/funding trong DB: EMA20/50, ATR, volume_zscore, oi_change_pct, funding_percentile. |
| `strategy/regime.py` | Regime detector: tính regime_score (0–100), map sang 4 mức. |
| `strategy/breakout.py` | Breakout_V1: phát hiện break high/low 15m + volume spike + OI tăng. Output `StrategySignal`. |
| `execution/paper.py` | Paper executor: fill với spread + fee + slippage, quản lý SL/TP, track PnL. |
| `signal/daily_report.py` | Daily report: trades hôm nay, winrate, PnL, regime trung bình. Gửi Telegram. |
| `main.py` | Entry point: khởi động WS worker + strategy loop + daily report. Lock file, SIGINT/SIGTERM. |

---

## 2. Config (`config.yaml` + `.env`)

### `config.yaml`

```yaml
data:
  coins: ["BTC", "ETH", "SOL", "WIF", "HYPE"]
  candle_intervals: ["15m", "1h"]
  backfill_days: 90

regime:
  no_trade_below: 30
  small_size_below: 50
  btc_trend_weight: 0.30
  coin_trend_weight: 0.25
  volume_weight: 0.20
  funding_weight: 0.15
  oi_weight: 0.10

strategy:
  name: Breakout_V1
  timeframe: "15m"
  breakout_lookback_candles: 20      # range high/low của 20 candle trước
  volume_zscore_min: 1.5             # volume hiện tại phải > mean + 1.5 * std
  oi_change_min_pct: 0.02            # OI tăng ít nhất 2%
  funding_max_pct: 0.005             # không long khi funding > 0.5%
  spread_max_bps: 20                 # bỏ qua nếu spread > 20 bps

risk:
  max_risk_per_trade_pct: 0.25       # % paper equity per trade
  sl_atr_multiplier: 1.5             # SL = entry ± 1.5 × ATR
  tp_rr: 2.0                         # TP = entry ± SL_distance × 2
  max_daily_loss_pct: 1.0
  max_concurrent_positions: 3
  emergency_stop: false

execution:
  mode: paper
  account_balance: 10000.0
  fee_taker_bps: 2.5                 # Hyperliquid taker fee
  slippage_bps: 5.0                  # estimate slippage
```

### `.env` (secrets — gitignored)

```env
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
DATABASE_URL=postgresql://bot:bot@localhost:5432/hl_strategy
REDIS_URL=redis://localhost:6379/0

# Chỉ cần cho live (Phase 5+):
HL_ACCOUNT_ADDRESS=
HL_PRIVATE_KEY=
LIVE_TRADING_CONFIRMED=   # gõ I_UNDERSTAND để mở khoá
```

---

## 3. Database schema (`storage/db.py`)

Dùng SQLAlchemy 2.0 declarative — cùng pattern với `bot-copy-trade-hyperliquid`.

### Market data

```python
class Candle(Base):
    __tablename__ = "candles"
    id: str             # "{coin}:{interval}:{open_time_ms}"
    coin: str
    interval: str       # "15m" | "1h"
    open_time: int      # unix ms
    close_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    created_at: int

class FundingRate(Base):
    __tablename__ = "funding_rates"
    id: str             # "{coin}:{funding_time_ms}"
    coin: str
    funding_time: int   # unix ms
    rate: float         # raw rate (0.0001 = 0.01%)
    created_at: int

class AssetContext(Base):
    __tablename__ = "asset_contexts"
    id: str             # "{coin}:{timestamp_ms}"
    coin: str
    timestamp: int      # unix ms
    mark_price: float
    open_interest: float
    created_at: int
```

### Strategy

```python
class Feature(Base):
    __tablename__ = "features"
    id: str             # "{coin}:{timeframe}:{timestamp_ms}"
    coin: str
    timeframe: str
    feature_time: int
    ema_20: float
    ema_50: float
    atr: float
    volume_zscore: float
    oi_change_pct: float
    funding_rate: float
    funding_percentile: float
    regime_score: float
    created_at: int

class StrategySignal(Base):
    __tablename__ = "strategy_signals"
    id: str             # "{strategy}:{coin}:{created_at_ms}"
    strategy: str       # "Breakout_V1"
    coin: str
    side: str           # "LONG" | "SHORT"
    entry_price: float
    sl_price: float
    tp_price: float
    regime_score: float
    volume_zscore: float
    oi_change_pct: float
    reason: str         # human-readable, e.g. "break_high + volume_spike + oi_up"
    status: str         # "PENDING" | "ACTIVE" | "CLOSED" | "REJECTED"
    reject_reason: str  # nếu REJECTED: "spread_too_wide" | "funding_too_hot" | "no_trade_regime" | ...
    created_at: int
    closed_at: int | None
```

### Paper trading

```python
class PaperPosition(Base):
    __tablename__ = "paper_positions"
    id: str             # "{coin}:{side}:{opened_at_ms}"
    signal_id: str
    coin: str
    side: str           # "LONG" | "SHORT"
    entry_price: float
    size_notional: float   # USD notional
    sl_price: float
    tp_price: float
    status: str         # "OPEN" | "CLOSED"
    opened_at: int
    closed_at: int | None
    exit_price: float | None
    exit_reason: str | None   # "sl" | "tp" | "signal_expired" | "emergency_stop"
    pnl_usd: float | None
    fee_usd: float | None

class EquitySnapshot(Base):
    __tablename__ = "equity_snapshots"
    id: int             # autoincrement
    timestamp: int
    equity: float
    unrealized_pnl: float
    realized_pnl_today: float
    max_drawdown_today: float
    open_positions: int
```

---

## 4. Hyperliquid client — extensions

Giữ toàn bộ `HyperliquidClient` từ bot cũ (retry logic, Redis cache). Thêm 4 methods:

```python
async def get_candles(
    self, coin: str, interval: str, start_time_ms: int, end_time_ms: int | None = None
) -> list[dict]:
    # POST {"type": "candleSnapshot", "req": {"coin": coin, "interval": interval,
    #        "startTime": start_time_ms, "endTime": end_time_ms}}
    # Returns: [{"t", "T", "o", "h", "l", "c", "v", "n"}]

async def get_funding_history(
    self, coin: str, start_time_ms: int
) -> list[dict]:
    # POST {"type": "fundingHistory", "coin": coin, "startTime": start_time_ms}

async def get_asset_contexts(self) -> list[dict]:
    # POST {"type": "metaAndAssetCtxs"}
    # Returns: [meta, [asset_ctx_list]] — parse asset_ctx_list

async def get_recent_trades(self, coin: str) -> list[dict]:
    # POST {"type": "recentTrades", "coin": coin}
```

---

## 5. WebSocket worker — thay đổi

`HyperliquidWSClient` từ bot cũ dùng `webData2` (per-wallet). Bot mới dùng:

- `candle` — realtime candle update theo coin + interval
- `trades` — recent trades theo coin
- `allMids` — giữ nguyên từ bot cũ

Không cần `webData2`. `add_wallet` / `remove_wallet` bị loại bỏ.

```python
class MarketWSClient:
    def subscribe_candle(self, coin: str, interval: str, callback) -> None: ...
    def subscribe_trades(self, coin: str, callback) -> None: ...
    def set_price_callback(self, callback) -> None: ...  # allMids
    async def run(self, stop: asyncio.Event) -> None: ...
    # Auto-reconnect + resubscribe — cùng pattern ws_client.py hiện tại
```

---

## 6. Strategy: Breakout_V1

```
Long setup:
  - close[0] > max(high[-lookback:-1])          # break trên range high
  - volume_zscore >= config.volume_zscore_min    # volume spike
  - oi_change_pct >= config.oi_change_min_pct   # OI tăng (dòng tiền mới)
  - funding_rate <= config.funding_max_pct       # funding không quá nóng
  - spread_bps <= config.spread_max_bps          # liquidity đủ
  - regime_score >= config.regime.small_size_below  # không NO_TRADE

Short setup:
  - close[0] < min(low[-lookback:-1])            # break dưới range low
  - volume_zscore >= config.volume_zscore_min
  - oi_change_pct >= config.oi_change_min_pct
  - funding_rate >= -config.funding_max_pct      # không quá âm
  - spread_bps <= config.spread_max_bps
  - regime_score >= config.regime.small_size_below

SL: entry ± ATR * sl_atr_multiplier
TP: entry ± SL_distance * tp_rr
```

Signal chỉ được gửi lên Risk Engine nếu pass đủ điều kiện. Nếu fail bất kỳ điều kiện nào → `StrategySignal(status="REJECTED", reject_reason=...)`.

---

## 7. Regime Detector

```python
def compute_regime_score(features: Feature, btc_features: Feature) -> float:
    btc_trend = trend_score(btc_features)   # 0-100: price vs EMA50
    coin_trend = trend_score(features)
    volume_score = normalize(features.volume_zscore, 0, 3)   # 0-100
    funding_score = 100 - normalize(abs(features.funding_rate), 0, 0.005)   # cao → xấu
    oi_score = normalize(features.oi_change_pct, -0.05, 0.05)

    return (
        btc_trend * 0.30
        + coin_trend * 0.25
        + volume_score * 0.20
        + funding_score * 0.15
        + oi_score * 0.10
    )
```

| Score | Regime | Hành động |
|---:|---|---|
| 0–29 | NO_TRADE | Không trade, không mở signal mới |
| 30–49 | SMALL | size × 0.5 (nếu sau này muốn, MVP thì skip) |
| 50–84 | NORMAL | Trade bình thường |
| 85–100 | STRONG | Trade bình thường (không tăng size ở paper) |

MVP đơn giản: chỉ check `NO_TRADE`. Chưa cần điều chỉnh size theo regime.

---

## 8. Risk Engine (pre-trade gate)

Chạy trước khi paper executor nhận signal. Reject nếu:

- `regime_score < no_trade_below`
- `spread_bps > spread_max_bps`
- `funding_rate > funding_max_pct` (long) hoặc `< -funding_max_pct` (short)
- `daily_loss_pct >= max_daily_loss_pct`
- `len(open_positions) >= max_concurrent_positions`
- `emergency_stop == true`
- Đã có open position cùng coin

---

## 9. Paper Executor

Giữ cấu trúc `PaperEngine` từ bot cũ (`on_signal_open`, `on_signal_close`, `on_price_update`). Điều chỉnh:

**Fill model:**
```
fill_price (long entry) = mid_price * (1 + slippage_bps/10000) + spread/2
fill_price (short entry) = mid_price * (1 - slippage_bps/10000) - spread/2
fee_usd = size_notional * fee_taker_bps / 10000
```

**Size:**
```
risk_usd = account_balance * max_risk_per_trade_pct / 100
sl_distance_pct = abs(entry - sl) / entry
size_notional = risk_usd / sl_distance_pct
```

**Close triggers:**
- `on_price_update`: check SL hit + TP hit mỗi khi giá mới đến
- `on_signal_expired`: đóng nếu signal không còn valid sau N candle (TODO sau)

---

## 10. Telegram alerts

**Signal mở:**
```
🚀 BREAKOUT SIGNAL

BTC LONG (Breakout_V1)
Entry: $65,420.00
SL: $64,800.00 (-0.95%) | TP: $66,660.00 (+1.90%)
Volume Z-Score: 2.1 | OI Change: +3.2%
Regime: 68/100 | Funding: +0.01%
Mode: PAPER
```

**Signal bị reject:**
```
⛔ SIGNAL REJECTED

ETH LONG (Breakout_V1)
Reason: spread_too_wide (28 bps > 20 bps max)
```

**Paper open:**
```
📄 PAPER OPEN

BTC LONG
Entry: $65,428.33 (incl. slippage + spread)
Size: $1,312 notional
SL: $64,800 | TP: $66,660
Risk: $25 (0.25% account) | Fee: $0.33
```

**Paper close:**
```
📄 PAPER CLOSE — 🛑 Stop Loss

BTC LONG
Entry: $65,428 → Exit: $64,800
PnL: -$25.13 (-1.0R) | Fee: $0.33
```

**Daily report:**
```
📊 DAILY REPORT (2026-06-20)

Mode: PAPER | Account: $9,952 (+$12)
Signals: 8 | Trades: 3 | Skipped: 5
Winrate: 33% | Avg R: -0.1R
Max DD Today: -$28
Regime avg: 61/100 (NORMAL)
Strategy: Breakout_V1
```

---

## 11. Docker Compose

Cùng pattern với bot cũ. Postgres port **5434**, Redis port **6381** (để tránh conflict với 2 bot đang chạy).

```yaml
services:
  postgres:
    image: postgres:16
    ports: ["5434:5432"]
    environment:
      POSTGRES_DB: hl_strategy
      POSTGRES_USER: bot
      POSTGRES_PASSWORD: bot

  redis:
    image: redis:7-alpine
    ports: ["6381:6379"]

  hl-backfill:
    build: .
    env_file: .env
    environment:
      DATABASE_URL: postgresql://bot:bot@postgres:5432/hl_strategy
      REDIS_URL: redis://redis:6379/0
    command: ["python", "-m", "src.data.backfill", "--loop", "--hours", "1"]
    depends_on: [postgres, redis]

  hl-strategy:
    build: .
    env_file: .env
    environment:
      DATABASE_URL: postgresql://bot:bot@postgres:5432/hl_strategy
      REDIS_URL: redis://redis:6379/0
    command: ["python", "-m", "src.main"]
    depends_on: [postgres, redis, hl-backfill]

  hl-report:
    build: .
    env_file: .env
    environment:
      DATABASE_URL: postgresql://bot:bot@postgres:5432/hl_strategy
    command: ["python", "-m", "src.signal.daily_report", "--loop", "--hours", "24"]
    depends_on: [postgres]
```

---

## 12. `src/main.py` — entry point

Cùng pattern với `bot-copy-trade-hyperliquid/src/main.py`:

```python
async def run():
    cfg = load_config()
    secrets = load_secrets()

    if cfg.risk.emergency_stop:
        logger.error("EMERGENCY STOP — abort")
        return

    notifier = build_notifier(...)
    init_db(secrets.database_url)
    session_factory = make_session_factory(secrets.database_url)

    feature_engine = FeatureEngine(session_factory, cfg)
    regime = RegimeDetector(cfg.regime)
    strategy = BreakoutV1(cfg.strategy, regime, feature_engine)
    paper = PaperEngine(cfg.risk, cfg.execution, notifier, session_factory)
    ws = MarketWSClient()

    stop = asyncio.Event()
    signal.signal(SIGINT, lambda *_: stop.set())
    signal.signal(SIGTERM, lambda *_: stop.set())

    ws.set_price_callback(paper.on_price_update)
    ws.subscribe_candle_all(cfg.data.coins, cfg.data.candle_intervals, on_candle)
    # on_candle: update features → evaluate strategy → risk gate → paper.on_signal_open

    await ws.run(stop)
```

---

## 13. Cấu trúc file dự kiến

```
src/
├── config/
│   └── settings.py
├── monitoring/
│   ├── notifier.py          # copy từ bot cũ
│   └── log_setup.py         # copy từ bot cũ
├── hyperliquid/
│   ├── client.py            # mở rộng từ bot cũ
│   └── ws_client.py         # viết lại: candle/trades thay webData2
├── storage/
│   ├── db.py
│   └── repository.py
├── data/
│   └── backfill.py          # fetch historical REST → DB
├── market/
│   └── ws_worker.py         # realtime WS → feature cache + DB
├── strategy/
│   ├── feature_engine.py
│   ├── regime.py
│   └── breakout.py
├── execution/
│   └── paper.py
├── signal/
│   └── daily_report.py      # cùng pattern bot cũ
└── main.py
```

---

## 14. MVP acceptance criteria

**Đạt khi:**
- Chạy 24/7 ở paper mode, không crash khi WS reconnect.
- Mỗi signal có đủ: coin, side, entry, SL, TP, regime_score, reason.
- Mỗi paper trade có fee + slippage model (không dùng raw close price làm fill).
- Risk gate hoạt động: NO_TRADE regime + funding + spread đều block signal đúng.
- Daily report gửi Telegram đủ: trades, winrate, PnL, max DD.
- Emergency stop = true → không mở position mới.

**Chưa đạt nếu:**
- Paper PnL tính từ close price không tính fee/slippage.
- Signal không log reason.
- Không có SL.
- Không có regime check.

---

## 15. Thứ tự implement

```
1. storage/db.py + config/settings.py                → verify: init_db tạo đủ tables
2. hyperliquid/client.py (get_candles, get_funding)   → verify: fetch 90d BTC candles
3. data/backfill.py                                   → verify: DB có data, không duplicate
4. strategy/feature_engine.py                         → verify: EMA/ATR khớp tính tay
5. strategy/regime.py                                 → verify: BTC downtrend → NO_TRADE
6. strategy/breakout.py                               → verify: backtest offline 30 ngày, có signal
7. execution/paper.py                                 → verify: SL hit đóng đúng, PnL đúng
8. market/ws_worker.py (candle + allMids)             → verify: candle WS cập nhật realtime
9. main.py + docker-compose.yml                       → verify: chạy 24h không crash
10. signal/daily_report.py                            → verify: Telegram nhận report đúng format
```

Mỗi bước có thể test độc lập (unit test hoặc script thử nhanh) trước khi sang bước tiếp.
