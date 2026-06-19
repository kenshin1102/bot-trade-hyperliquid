"""Daily report: sends summary of paper trades to Telegram."""
from __future__ import annotations

import argparse
import asyncio
import time
from datetime import datetime, timedelta, timezone

from src.config.settings import ExecutionConfig, load_config, load_secrets
from src.execution.paper import PaperEngine, PaperPosition
from src.monitoring.notifier import Notifier, build_notifier
from src.storage.db import PaperPositionRow, init_db, make_session_factory
from src.storage.repository import PaperPositionRepo, StrategySignalRepo

_VN_TZ = timezone(timedelta(hours=7))
_TABLE_LIMIT = 50
_EXIT_ABBREV = {"sl": "SL", "tp": "TP", "emergency_stop": "STOP"}


def _hold_str(seconds: int) -> str:
    if seconds >= 86400:
        return f"{seconds // 86400}d{(seconds % 86400) // 3600}h"
    if seconds >= 3600:
        return f"{seconds // 3600}h{(seconds % 3600) // 60}m"
    return f"{seconds // 60}m"


def _trade_table(positions: list[PaperPositionRow]) -> str:
    if not positions:
        return ""
    rows_data = positions[-_TABLE_LIMIT:]
    truncated = len(positions) > _TABLE_LIMIT

    hdr = f"{'#':>3} {'Coin':<7}{'Side':>6} {'PnL($)':>9} {'Ret%':>7} {'Hold':>7} Exit"
    sep = "-" * len(hdr)
    rows = [hdr, sep]
    for i, p in enumerate(rows_data, start=1):
        pnl = p.pnl_usd or 0.0
        pnl_s = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
        ret = (pnl / p.size_notional * 100) if p.size_notional else 0.0
        hold = _hold_str(max(0, (p.closed_at or p.opened_at) - p.opened_at))
        reason = _EXIT_ABBREV.get(p.exit_reason or "", p.exit_reason or "exit")
        rows.append(f"{i:>3} {p.coin:<7}{p.side:>6} {pnl_s:>9} {ret:+6.1f}% {hold:>7} {reason}")

    note = f"\n(hiển thị {_TABLE_LIMIT}/{len(positions)} lệnh)" if truncated else ""
    return "<pre>" + "\n".join(rows) + note + "</pre>"


def _summary_line(positions: list[PaperPositionRow]) -> str:
    if not positions:
        return "Không có lệnh nào trong 24h qua."
    wins = sum(1 for p in positions if (p.pnl_usd or 0.0) > 0)
    total_pnl = sum(p.pnl_usd or 0.0 for p in positions)
    avg_ret = sum(
        ((p.pnl_usd or 0.0) / p.size_notional * 100) if p.size_notional else 0.0
        for p in positions
    ) / len(positions)
    pnl_s = f"+${total_pnl:.2f}" if total_pnl >= 0 else f"-${abs(total_pnl):.2f}"
    wr = wins / len(positions) * 100
    return (
        f"Tổng: {len(positions)} lệnh | W:{wins} L:{len(positions)-wins} "
        f"| PnL: {pnl_s} | WR: {wr:.0f}% | Avg: {avg_ret:+.1f}%"
    )


def _open_positions_lines(positions: list[PaperPosition], now_ts: int) -> list[str]:
    if not positions:
        return ["Không có position nào đang mở."]
    lines = []
    for p in positions:
        hold = _hold_str(max(0, now_ts - p.opened_at))
        lines.append(f"{p.coin} {p.side} | ${p.size_notional:,.0f} | entry ${p.entry_price:,.4f} | {hold}")
    return lines


async def send_daily_report(
    notifier: Notifier,
    paper: PaperEngine,
    session_factory,
    cfg_execution: ExecutionConfig,
) -> None:
    since_ts = int(time.time()) - 86400
    now_ts = int(time.time())

    s = session_factory()
    try:
        closed = PaperPositionRepo(s).list_closed_today(since_ts)
        signals = StrategySignalRepo(s).list_today(since_ts)
    finally:
        s.close()

    open_pos = paper.get_open_positions()
    equity = cfg_execution.account_balance + paper.get_daily_pnl()

    rejected_signals = [s for s in signals if s.status == "REJECTED"]
    executed_signals = [s for s in signals if s.status in ("ACTIVE", "CLOSED")]
    total_signals = len(signals)
    total_trades = len(closed)

    date_str = datetime.fromtimestamp(now_ts, tz=_VN_TZ).strftime("%a %d %b %Y")

    lines = [
        f"📊 <b>DAILY REPORT — {date_str}</b>",
        f"Mode: PAPER | Equity: ${equity:,.0f}",
        f"Signals: {total_signals} | Executed: {len(executed_signals)} | Rejected: {len(rejected_signals)}",
        "",
        "📈 <b>24h qua</b>",
    ]

    table = _trade_table(closed)
    if table:
        lines.append(table)
    lines.append(_summary_line(closed))
    lines.append("")
    lines.append(f"📂 <b>Đang giữ ({len(open_pos)})</b>")
    lines.extend(_open_positions_lines(open_pos, now_ts))

    await notifier.send("info", "\n".join(lines))


if __name__ == "__main__":
    async def _run(loop_hours: float) -> None:
        cfg = load_config()
        secrets = load_secrets()
        notifier = build_notifier(secrets.telegram_bot_token, secrets.telegram_chat_id)
        init_db(secrets.database_url)
        sf = make_session_factory(secrets.database_url)
        paper = PaperEngine(cfg.risk, cfg.execution, notifier, sf)

        if loop_hours > 0:
            while True:
                await send_daily_report(notifier, paper, sf, cfg.execution)
                await asyncio.sleep(loop_hours * 3600)
        else:
            await send_daily_report(notifier, paper, sf, cfg.execution)

    p = argparse.ArgumentParser(prog="python -m src.signal.daily_report")
    p.add_argument("--loop", action="store_true")
    p.add_argument("--hours", type=float, default=24.0)
    args = p.parse_args()
    asyncio.run(_run(args.hours if args.loop else 0))
