# Hyperliquid Independent Strategy Bot

Bot chiến lược độc lập trên Hyperliquid Perp. Không copy ai — tự đọc thị trường, tự sinh signal breakout, tự kiểm soát rủi ro.

## Kiến trúc

```
Market Data (Candles / OI / Funding / Trades)
        │
        ▼
  hl-backfill (mỗi 1h)          market-ws-worker (realtime)
  └─ REST historical → DB        └─ WS candle + allMids → cache
        │                                    │
        └──────────────────┬─────────────────┘
                           ▼
              Feature Engine
              (EMA20/50, ATR, volume_zscore, oi_change, funding_percentile)
                           │
                           ▼
              Regime Detector (score 0-100)
              NO_TRADE / SMALL / NORMAL / STRONG
                           │
                           ▼
              Breakout_V1 Signal Engine
              (break high/low + volume spike + OI up)
                           │
                           ▼
              Risk Engine (pre-trade gate)
                           │
                           ▼
              Paper Executor (spread + fee + slippage)
                           │
                           ▼
              Telegram Alert + Daily Report
```

| Module | Vai trò |
|---|---|
| `src/data/backfill.py` | Fetch historical candles/funding → DB, chạy mỗi 1h |
| `src/data/coin_selector.py` | Chọn top coins theo volume (dynamic universe) |
| `src/strategy/feature_engine.py` | Tính EMA20/50, ATR, volume_zscore, oi_change_pct, funding_percentile |
| `src/strategy/regime.py` | Regime score 0-100 → NO_TRADE / SMALL / NORMAL / STRONG |
| `src/strategy/breakout.py` | Breakout_V1: break high/low + volume spike + OI up → StrategySignal |
| `src/execution/paper.py` | Paper trade với fee + slippage model, SL/TP, PnL tracking |
| `src/signal/daily_report.py` | Daily Telegram report |
| `src/storage/db.py` | SQLAlchemy 2.0: Candle, FundingRate, Feature, StrategySignal, PaperPosition |

---

## Cài đặt local

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # điền token Telegram (nếu có)
.venv/bin/pytest -q
```

## Cấu hình `.env`

```env
TELEGRAM_BOT_TOKEN=<token>
TELEGRAM_CHAT_ID=<chat_id>

# DB + Redis (mặc định dùng port tránh conflict với 2 bot kia)
DATABASE_URL=postgresql://bot:bot@localhost:5434/hl_strategy
REDIS_URL=redis://localhost:6381/0

# Chỉ cần cho live trading:
HL_ACCOUNT_ADDRESS=
HL_PRIVATE_KEY=
LIVE_TRADING_CONFIRMED=   # gõ I_UNDERSTAND để mở khoá
```

## Chạy local

```bash
# Backfill dữ liệu lịch sử (cần chạy 1 lần đầu)
.venv/bin/python -m src.data.backfill

# Chạy bot (WS realtime + signal engine)
.venv/bin/python -m src.main
```

---

## Deploy lên server (GCP VM)

**Server:** `toannx@35.239.129.132`, thư mục `~/bot-trade-hyperliquid`

> Bot chạy song song với 2 bot kia.
> Postgres dùng port **5434**, Redis dùng port **6381** để tránh conflict.

### Deploy lần đầu

```bash
# 1. Copy code (không copy .venv, .env, data)
rsync -av --exclude='.venv' --exclude='__pycache__' --exclude='*.pyc' \
  --exclude='.env' --exclude='data/' --exclude='logs/' \
  /Users/macos/Documents/code/personal/bot-pro-trade/bot-trade-hyperliquid/ \
  toannx@35.239.129.132:~/bot-trade-hyperliquid/

# 2. Copy secrets
scp .env toannx@35.239.129.132:~/bot-trade-hyperliquid/.env

# 3. Build & start
ssh toannx@35.239.129.132 \
  "cd ~/bot-trade-hyperliquid && sudo docker compose build && sudo docker compose up -d"
```

### Update code (workflow chuẩn)

```bash
rsync -av --exclude='.venv' --exclude='__pycache__' --exclude='*.pyc' \
  --exclude='.env' --exclude='data/' --exclude='logs/' \
  /Users/macos/Documents/code/personal/bot-pro-trade/bot-trade-hyperliquid/ \
  toannx@35.239.129.132:~/bot-trade-hyperliquid/

ssh toannx@35.239.129.132 \
  "cd ~/bot-trade-hyperliquid && sudo docker compose build && sudo docker compose up -d"
```

### Chỉ đổi `.env` hoặc `config/config.yaml` (không rebuild)

```bash
scp .env toannx@35.239.129.132:~/bot-trade-hyperliquid/.env
# hoặc
scp config/config.yaml toannx@35.239.129.132:~/bot-trade-hyperliquid/config/config.yaml

ssh toannx@35.239.129.132 \
  "cd ~/bot-trade-hyperliquid && sudo docker compose up -d"
```

### Kiểm tra logs

```bash
# Tất cả services
ssh toannx@35.239.129.132 \
  "cd ~/bot-trade-hyperliquid && sudo docker compose logs -f"

# Strategy bot (signals, PnL)
ssh toannx@35.239.129.132 \
  "cd ~/bot-trade-hyperliquid && sudo docker compose logs -f hl-strategy"

# Backfill (data pipeline)
ssh toannx@35.239.129.132 \
  "cd ~/bot-trade-hyperliquid && sudo docker compose logs -f hl-backfill"

# Status containers
ssh toannx@35.239.129.132 \
  "cd ~/bot-trade-hyperliquid && sudo docker compose ps"
```

### Xem dữ liệu trong DB (qua SSH tunnel)

```bash
# Mở tunnel
ssh -f -N -L 15434:localhost:5434 toannx@35.239.129.132

# Kết nối
psql postgresql://bot:bot@localhost:15434/hl_strategy

# Signals gần nhất
psql postgresql://bot:bot@localhost:15434/hl_strategy \
  -c "SELECT coin, side, status, entry, sl, tp, regime_score, created_at
      FROM strategy_signals ORDER BY created_at DESC LIMIT 20;"

# Paper positions đang mở
psql postgresql://bot:bot@localhost:15434/hl_strategy \
  -c "SELECT coin, side, entry_price, sl_price, tp_price, size_usd, opened_at
      FROM paper_positions WHERE status='OPEN' ORDER BY opened_at;"

# Equity snapshots (daily PnL)
psql postgresql://bot:bot@localhost:15434/hl_strategy \
  -c "SELECT ts, equity, daily_pnl FROM equity_snapshots ORDER BY ts DESC LIMIT 10;"
```

---

## Services (docker-compose)

| Service | Command | Restart |
|---------|---------|---------|
| `postgres` | — | unless-stopped |
| `redis` | — | unless-stopped |
| `hl-backfill` | `backfill --loop --hours 1` | unless-stopped |
| `hl-strategy` | `src.main --report-hours 24` | unless-stopped |

---

## Tuning nhanh (`config.yaml`)

| Muốn | Sửa |
|---|---|
| Thêm coin theo dõi | `data.coins: ["BTC","ETH","SOL","WIF","HYPE","ARB"]` |
| Ít trade hơn (regime chặt hơn) | `regime.no_trade_below: 40` |
| Breakout nhạy hơn | `strategy.volume_zscore_min: 1.2` |
| Rủi ro nhỏ hơn mỗi lệnh | `risk.max_risk_per_trade_pct: 0.1` |
| TP/SL ratio cao hơn | `risk.tp_rr: 3.0` |
| Dừng khẩn cấp | `risk.emergency_stop: true` (không cần rebuild) |

---

## Chế độ live

**3 lớp bảo vệ** — thiếu bất kỳ 1 cái → paper mode:
1. `execution.mode: live` trong `config.yaml`
2. `HL_PRIVATE_KEY` được set trong `.env`
3. `LIVE_TRADING_CONFIRMED=I_UNDERSTAND` trong `.env`

---

## Backtest & Kết quả tốt nhất

### Chạy backtest

```bash
# Backtest đơn lẻ
.venv/bin/python -m src.backtest.runner --days 180 --timeframe 1h

# Sweep nhiều params, walk-forward, slippage stress
.venv/bin/python -m src.backtest.scenarios \
  --names S07-1h-filter --htf match \
  --grid-vol-z 2.2 --grid-tp-rr 2.5 \
  --grid-ema-spread 1.2 \
  --grid-min-vol 10e6 \
  --rolling-universe 10 --days 180 --wf 3 --slippage 5 20 50 \
  --coins BTC HYPE ETH SOL ZEC WLD NEAR XPL EIGEN AVAX XRP LIT JTO UNI XMR TAO AAVE MET AERO SUI VVV CRV PUMP ONDO ASTER ENA FARTCOIN XLM kPEPE BNB
```

Kết quả lưu tự động tại `data/sweep_<timestamp>.json`.

### Scenario tốt nhất: `S07-1h-filter-z2.2-rr2.50-sp1.20-v10m` (cập nhật 2026-06-20)

**Dataset:** 180 ngày (Dec 22 2025 → Jun 20 2026), rolling top-10 coins theo 24h volume từ pool-30, 4h HTF trend filter.

| Metric | Giá trị |
|---|---|
| Trades | 200 |
| Win Rate | **42.0%** |
| PnL | +$2,045 (+20.4%) |
| Profit Factor | **1.692** |
| Max Drawdown | 3.1% |
| Avg Win / Avg Loss | +$59.5 / -$25.5 |
| Avg Hold | 12.4h |

**Walk-forward 3 windows (mỗi ~59 ngày):**

| Window | Trades | WR | PF | MaxDD |
|---|---|---|---|---|
| W1 Dec22→Feb20 | 72 | 43.1% | 1.766 | 1.4% |
| W2 Feb20→Apr21 | 64 | 39.1% | 1.499 | 2.8% |
| W3 Apr21→Jun20 | 64 | 43.8% | 1.817 | 2.0% |
| **Tổng** | **200** | **42.0%** | **avg 1.694** | **3.1%** |

3/3 windows profitable, worst_PF = 1.499.

**Params (đã update vào `config.yaml`):**

```yaml
strategy:
  timeframe: "1h"
  breakout_lookback_candles: 30
  volume_zscore_min: 2.2
  ema_spread_min_pct: 1.2    # trending market filter
  min_vol_24h_usd: 10000000  # thin-volume coin filter ($10M/day)

risk:
  tp_rr: 2.5
  sl_atr_multiplier: 1.5
  max_concurrent_positions: 2
```

**Filters đã thử nghiệm và kết quả:**

| Filter | Tác động | Quyết định |
|---|---|---|
| EMA spread ≥ 1.2% | WR 37.8% → 42%, PF 1.35 → 1.69 | ✅ **Bật** |
| Min 24h vol ≥ $10M | Loại thin-volume coins, PF nhẹ tăng, DD giảm | ✅ **Bật** |
| ATR expansion ≥ 1.1× | WR giảm, PF giảm | ❌ Không dùng |
| 2-bar confirmation | WR giảm 37% → 42%, WF kém hơn | ❌ Không dùng |

**WR ceiling:** ~42% với signal structure hiện tại (1-candle breakout). Để đạt 50% cần thay đổi loại signal (mean-reversion, pullback entry).
