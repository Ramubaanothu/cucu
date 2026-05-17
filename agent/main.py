from __future__ import annotations

import asyncio
import logging
import re as _re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import typer
import uvicorn
from rich.logging import RichHandler
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from .ai.analyst import AIAnalyst
from .api.server import build_app
from .config import Settings
from .db.database import create_tables, init_engine
from .db.models import Position, ScanRun, TradeLog, Wallet
from .executor.base import BaseExecutor
from .executor.live import LiveExecutor
from .executor.paper import PaperExecutor
from .polymarket.clob import CLOBCredentials, ClobClient
from .polymarket.data import DataClient
from .polymarket.gamma import GammaClient
from .scanner.analyzer import WalletAnalyzer
from .scanner.detector import BotDetector
from .scanner.leaderboard import LeaderboardFetcher
from .scanner.onchain_scanner import OnChainScanner, RisingStarAnalyzer
from .strategy.copier import CopyTrader
from .strategy.risk import RiskManager

cli = typer.Typer(name="cucu")


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(message)s",
        handlers=[RichHandler(rich_tracebacks=True, show_path=False)],
    )


@cli.command()
def run(
    mode: str = typer.Option("paper", help="Trading mode: paper | live"),
    scan_now: bool = typer.Option(False, help="Run leaderboard scan immediately on startup"),
    port: int = typer.Option(0, help="Override HTTP port (0 = use config)"),
    log_level: str = typer.Option("", help="Override log level"),
) -> None:
    asyncio.run(_main(mode=mode, scan_now=scan_now, port_override=port, log_level=log_level))


async def _main(
    mode: str,
    scan_now: bool,
    port_override: int,
    log_level: str,
) -> None:
    settings = Settings()
    if mode:
        settings.trading_mode = mode
    if log_level:
        settings.log_level = log_level
    if port_override:
        settings.agent_http_port = port_override

    setup_logging(settings.log_level)
    log = logging.getLogger("agent.main")
    log.info("Starting Polymarket copy-trading agent [mode=%s]", settings.trading_mode)

    db_dir = Path(settings.db_path).parent
    db_dir.mkdir(parents=True, exist_ok=True)

    engine = init_engine(settings.db_path)
    await create_tables(engine)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    clob = ClobClient()
    gamma = GammaClient()
    data = DataClient()

    analyst: AIAnalyst | None = None
    if settings.has_claude_credentials:
        import anthropic
        analyst = AIAnalyst(anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key))
        analyst.register_tool_handlers(_build_tool_handlers(clob, gamma, session_factory, settings))
        log.info("Claude AI analyst enabled")
    else:
        log.warning("ANTHROPIC_API_KEY not set — AI gating disabled, using rule-based copy")

    executor: BaseExecutor
    if settings.trading_mode == "live":
        if not settings.has_trading_credentials:
            log.error("Live mode requires POLYMARKET_API_KEY and POLYGON_PRIVATE_KEY in .env")
            raise typer.Exit(1)
        creds = CLOBCredentials(
            api_key=settings.polymarket_api_key,
            api_secret=settings.polymarket_api_secret,
            api_passphrase=settings.polymarket_api_passphrase,
            private_key=settings.polygon_private_key,
        )
        live_exec = LiveExecutor(clob, creds, session_factory)
        await live_exec.validate_credentials()
        executor = live_exec
        log.info("Live executor ready")
    else:
        executor = PaperExecutor(session_factory)
        log.info("Paper executor ready")

    active_wallets: set[str] = set()
    if settings.pinned_wallets:
        active_wallets = set(settings.pinned_wallets)
        log.info("Using %d pinned wallets", len(active_wallets))
    else:
        async with session_factory() as session:
            result = await session.execute(
                select(Wallet.address).where(Wallet.is_active == True)
            )
            active_wallets = {row[0] for row in result.all()}
        log.info("Loaded %d active wallets from DB", len(active_wallets))

    risk = RiskManager(settings, session_factory)
    copier = CopyTrader(
        settings=settings,
        risk=risk,
        executor=executor,
        analyst=analyst,
        clob=clob,
        gamma=gamma,
        session_factory=session_factory,
        active_wallets=active_wallets,
    )

    app = build_app(copier, risk, session_factory, data_client=data)
    server_config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=settings.agent_http_port,
        log_level="warning",
    )
    server = uvicorn.Server(server_config)

    log.info("HTTP API listening on port %d", settings.agent_http_port)
    log.info("TypeScript monitor: cd monitor-ts && npm start")

    on_chain_scanner = OnChainScanner(
        rpc_url=settings.polygon_rpc_url,
        data_client=data,
    )

    await asyncio.gather(
        server.serve(),
        _scan_loop(settings, data, analyst, session_factory, copier, scan_now, on_chain_scanner),
        _position_manager_loop(settings, executor, clob, session_factory),
    )


# ── Scanner loop ─────────────────────────────────────────────────────────────

async def _scan_loop(
    settings: Settings,
    data: DataClient,
    analyst: AIAnalyst | None,
    session_factory,
    copier: CopyTrader,
    run_immediately: bool,
    on_chain_scanner: OnChainScanner,
) -> None:
    log = logging.getLogger("agent.scanner")

    if not run_immediately:
        log.info(
            "Scanner will run in %d seconds. Use --scan-now to run immediately.",
            settings.scan_interval_seconds,
        )
        await asyncio.sleep(settings.scan_interval_seconds)

    while True:
        try:
            await _run_scan(settings, data, analyst, session_factory, copier, on_chain_scanner)
        except Exception as exc:
            log.error("Scan failed: %s", exc, exc_info=True)
        await asyncio.sleep(settings.scan_interval_seconds)


# Rising stars use a lower composite score floor — more risk, smaller allocation
_RISING_STAR_MIN_SCORE = 50
# At most this many rising-star slots in the final watch list
_RISING_STAR_MAX_SLOTS = 3


async def _run_scan(
    settings: Settings,
    data: DataClient,
    analyst: AIAnalyst | None,
    session_factory,
    copier: CopyTrader,
    on_chain_scanner: OnChainScanner,
) -> None:
    log = logging.getLogger("agent.scanner")
    log.info("Starting leaderboard scan…")

    fetcher = LeaderboardFetcher(data)
    analyzer = WalletAnalyzer()
    rising_analyzer = RisingStarAnalyzer()
    detector = BotDetector()

    candidates = await fetcher.fetch_candidates(top_n=200)
    log.info("Fetched %d candidates", len(candidates))

    # Parallel trade fetching — 10 concurrent requests
    fetch_sem = asyncio.Semaphore(10)

    async def score_candidate(candidate):
        async with fetch_sem:
            trades = await data.get_trader_trades(candidate.address, limit=200)
        sc = analyzer.score(candidate, trades)
        bot = detector.analyze(candidate.address, trades, candidate.profile)
        sc.raw_metrics["bot_signals"] = bot.signals
        sc.raw_metrics["bot_confidence"] = bot.bot_confidence
        sc.raw_metrics["source"] = "leaderboard"
        return sc, bot

    results = await asyncio.gather(*[score_candidate(c) for c in candidates])
    scored = [
        (sc, bot) for sc, bot in results
        if sc.composite_score >= settings.min_bot_score
    ]

    log.info("%d wallets above min_bot_score=%d", len(scored), settings.min_bot_score)

    # ── On-chain rising-star discovery ────────────────────────────────────────────
    known_addresses = {c.address for c in candidates}
    rising_count = 0
    try:
        rising_candidates = await on_chain_scanner.scan_new_traders(
            known_addresses=known_addresses,
            max_results=50,
        )
        log.info("[OnChainScanner] Scoring %d rising-star candidates…", len(rising_candidates))

        async def score_rising(candidate):
            async with fetch_sem:
                trades = await data.get_trader_trades(candidate.address, limit=200)
            sc = rising_analyzer.score(candidate, trades)
            bot = detector.analyze(candidate.address, trades, candidate.profile)
            sc.raw_metrics["bot_signals"] = bot.signals
            sc.raw_metrics["bot_confidence"] = bot.bot_confidence
            sc.raw_metrics["source"] = "onchain"
            return sc, bot

        rising_results = await asyncio.gather(*[score_rising(c) for c in rising_candidates])
        rising_scored = sorted(
            [
                (sc, bot) for sc, bot in rising_results
                if sc.composite_score >= _RISING_STAR_MIN_SCORE
            ],
            key=lambda x: x[0].composite_score,
            reverse=True,
        )[:_RISING_STAR_MAX_SLOTS]

        rising_count = len(rising_candidates)
        if rising_scored:
            log.info(
                "[OnChainScanner] %d rising stars qualify (score >= %d): %s",
                len(rising_scored),
                _RISING_STAR_MIN_SCORE,
                [s.address[:8] for s, _ in rising_scored],
            )
            scored = list(scored) + rising_scored
    except Exception as exc:
        log.warning("[OnChainScanner] Rising-star scan failed (continuing without): %s", exc)

    # ── Wallet selection ──────────────────────────────────────────────────────────
    if analyst and settings.has_claude_credentials and scored:
        top_addresses = await analyst.evaluate_wallets(
            [s for s, _ in scored],
            max_select=settings.max_target_wallets,
        )
    else:
        top_addresses = [
            s.address
            for s, _ in sorted(scored, key=lambda x: x[0].composite_score, reverse=True)
        ][: settings.max_target_wallets]

    log.info("Selected %d target wallets: %s", len(top_addresses), [a[:8] for a in top_addresses])

    async with session_factory() as session:
        all_result = await session.execute(select(Wallet))
        for w in all_result.scalars().all():
            w.is_active = False

        for ws, bot in scored:
            existing = (
                await session.execute(select(Wallet).where(Wallet.address == ws.address))
            ).scalar_one_or_none()
            w = existing or Wallet(address=ws.address)
            if not existing:
                session.add(w)
            w.composite_score = ws.composite_score
            w.win_rate = ws.win_rate
            w.roi_30d = ws.roi_30d
            w.sharpe = ws.sharpe
            w.trade_count = ws.trade_count
            w.avg_trade_size = ws.avg_trade_size
            w.trade_frequency_per_day = ws.trade_frequency_per_day
            w.is_bot_likely = bot.is_bot_likely
            w.bot_confidence = bot.bot_confidence
            w.is_active = ws.address in top_addresses
            w.last_scanned_at = datetime.utcnow()

        scan_run = ScanRun(
            completed_at=datetime.utcnow(),
            wallets_found=len(candidates) + rising_count,
            wallets_selected=len(top_addresses),
        )
        scan_run.set_addresses(top_addresses)
        session.add(scan_run)
        await session.commit()

    await copier.update_active_wallets(top_addresses)
    log.info("Scan complete. Monitoring %d wallets.", len(top_addresses))


# ── Position manager loop ─────────────────────────────────────────────────────

async def _position_manager_loop(
    settings: Settings,
    executor: BaseExecutor,
    clob: ClobClient,
    session_factory,
) -> None:
    log = logging.getLogger("agent.positions")
    while True:
        await asyncio.sleep(settings.price_update_interval)
        try:
            await _update_positions(settings, executor, clob, session_factory)
        except Exception as exc:
            log.error("Position manager error: %s", exc, exc_info=True)


async def _update_positions(
    settings: Settings,
    executor: BaseExecutor,
    clob: ClobClient,
    session_factory,
) -> None:
    log = logging.getLogger("agent.positions")

    async with session_factory() as session:
        result = await session.execute(
            select(Position).where(Position.status == "open")
        )
        positions: list[Position] = list(result.scalars().all())

    if not positions:
        return

    # Fetch prices for all unique token IDs in parallel
    unique_tokens = {p.token_id for p in positions if p.token_id}
    price_map: dict[str, float] = {}

    async def fetch_price(token_id: str) -> tuple[str, float]:
        try:
            book = await clob.get_order_book(token_id)
            return token_id, book.mid_price
        except Exception:
            return token_id, 0.0

    price_results = await asyncio.gather(*[fetch_price(t) for t in unique_tokens])
    price_map = {tid: price for tid, price in price_results if price > 0}

    now = datetime.now(timezone.utc)
    closes: list[tuple[int, str]] = []

    async with session_factory() as session:
        for pos in positions:
            current = price_map.get(pos.token_id, pos.current_price)
            if current <= 0:
                continue

            pos_obj = await session.get(Position, pos.id)
            if not pos_obj or pos_obj.status != "open":
                continue

            pos_obj.current_price = current
            pos_obj.pnl = (current - pos_obj.entry_price) * (pos_obj.size_usdc / pos_obj.entry_price)
            session.add(pos_obj)

            # Check exit conditions
            age_hours = 0.0
            if pos_obj.opened_at:
                opened = pos_obj.opened_at
                if opened.tzinfo is None:
                    opened = opened.replace(tzinfo=timezone.utc)
                age_hours = (now - opened).total_seconds() / 3600

            pct_change = (current - pos_obj.entry_price) / pos_obj.entry_price if pos_obj.entry_price > 0 else 0

            if pct_change >= settings.take_profit_pct:
                closes.append((pos_obj.id, f"Take profit: +{pct_change:.1%}"))
            elif pct_change <= -settings.stop_loss_pct:
                closes.append((pos_obj.id, f"Stop loss: {pct_change:.1%}"))
            elif age_hours >= settings.max_hold_hours:
                closes.append((pos_obj.id, f"Max hold time reached: {age_hours:.0f}h"))

        await session.commit()

    # Execute closes outside the update session to avoid nested transactions
    for pos_id, reason in closes:
        log.info("Auto-closing pos #%d: %s", pos_id, reason)
        result = await executor.close_position(pos_id, reason)
        if not result.success:
            log.warning("Auto-close failed for pos #%d: %s", pos_id, result.error)


# ── Claude tool handlers ──────────────────────────────────────────────────────

def _build_tool_handlers(
    clob: ClobClient,
    gamma: GammaClient,
    session_factory,
    settings: Settings,
) -> dict:
    _COND_RE = _re.compile(r'^(0x)?[a-fA-F0-9]{40,66}$')
    _TOKEN_RE = _re.compile(r'^\d+$|^[a-fA-F0-9]{40,66}$')

    async def get_market_details(condition_id: str) -> dict:
        if not _COND_RE.match(condition_id):
            return {"error": "Invalid condition_id format"}
        market = await gamma.get_market(condition_id)
        if not market:
            return {"error": "Market not found"}
        return {
            "question": market.question,
            "end_date": market.end_date,
            "active": market.active,
            "closed": market.closed,
            "category": market.category,
            "tokens": market.tokens,
        }

    async def get_order_book_snapshot(token_id: str) -> dict:
        if not _TOKEN_RE.match(str(token_id)):
            return {"error": "Invalid token_id format"}
        book = await clob.get_order_book(token_id)
        return {
            "token_id": token_id,
            "mid_price": book.mid_price,
            "spread": book.spread,
            "best_bid": book.bids[0] if book.bids else None,
            "best_ask": book.asks[0] if book.asks else None,
            "top_bids": book.bids[:5],
            "top_asks": book.asks[:5],
        }

    async def check_exposure() -> dict:
        rm = RiskManager(settings, session_factory)
        snap = await rm.get_exposure_snapshot()
        return {
            "total_usdc": round(snap.total_usdc, 2),
            "max_total": snap.max_total,
            "per_market": {k: round(v, 2) for k, v in snap.per_market.items()},
        }

    return {
        "get_market_details": get_market_details,
        "get_order_book_snapshot": get_order_book_snapshot,
        "check_exposure": check_exposure,
    }


if __name__ == "__main__":
    cli()
