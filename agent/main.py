from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from pathlib import Path

import typer
import uvicorn
from rich.logging import RichHandler
from sqlalchemy.ext.asyncio import async_sessionmaker

from .ai.analyst import AIAnalyst
from .api.server import build_app
from .config import Settings
from .db.database import create_tables, init_engine
from .db.models import ScanRun, Wallet
from .executor.base import BaseExecutor
from .executor.live import LiveExecutor
from .executor.paper import PaperExecutor
from .polymarket.clob import CLOBCredentials, ClobClient
from .polymarket.data import DataClient
from .polymarket.gamma import GammaClient
from .scanner.analyzer import WalletAnalyzer
from .scanner.detector import BotDetector
from .scanner.leaderboard import LeaderboardFetcher
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

    # Ensure DB directory exists
    db_dir = Path(settings.db_path).parent
    db_dir.mkdir(parents=True, exist_ok=True)

    engine = init_engine(settings.db_path)
    await create_tables(engine)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    # Build API clients
    clob = ClobClient()
    gamma = GammaClient()
    data = DataClient()

    # Claude analyst (optional)
    analyst: AIAnalyst | None = None
    if settings.has_claude_credentials:
        import anthropic
        analyst = AIAnalyst(anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key))
        analyst.register_tool_handlers(_build_tool_handlers(clob, gamma, session_factory, settings))
        log.info("Claude AI analyst enabled")
    else:
        log.warning("ANTHROPIC_API_KEY not set — AI gating disabled, using rule-based copy")

    # Build executor
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
        executor = LiveExecutor(clob, creds, session_factory)
        log.info("Live executor ready")
    else:
        executor = PaperExecutor(session_factory)
        log.info("Paper executor ready")

    # Load active wallets from DB or pinned config
    active_wallets: set[str] = set()
    if settings.pinned_wallets:
        active_wallets = set(settings.pinned_wallets)
        log.info("Using %d pinned wallets", len(active_wallets))
    else:
        async with session_factory() as session:
            from sqlalchemy import select
            result = await session.execute(
                select(Wallet.address).where(Wallet.is_active == True)
            )
            active_wallets = {row[0] for row in result.all()}
        log.info("Loaded %d active wallets from DB", len(active_wallets))

    # Build strategy components
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

    # FastAPI server
    app = build_app(copier, risk, session_factory)
    server_config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=settings.agent_http_port,
        log_level="warning",
    )
    server = uvicorn.Server(server_config)

    log.info("HTTP API listening on port %d", settings.agent_http_port)
    log.info(
        "TypeScript monitor: cd monitor-ts && npm start  (polls wallets and POSTs to /copy-signal)"
    )

    await asyncio.gather(
        server.serve(),
        _scan_loop(settings, data, analyst, session_factory, copier, scan_now),
    )


async def _scan_loop(
    settings: Settings,
    data: DataClient,
    analyst: AIAnalyst | None,
    session_factory,
    copier: CopyTrader,
    run_immediately: bool,
) -> None:
    log = logging.getLogger("agent.scanner")

    if not run_immediately:
        log.info(
            "Scanner will run in %d seconds. Start with --scan-now to run immediately.",
            settings.scan_interval_seconds,
        )
        await asyncio.sleep(settings.scan_interval_seconds)

    while True:
        try:
            await _run_scan(settings, data, analyst, session_factory, copier)
        except Exception as exc:
            log.error("Scan failed: %s", exc, exc_info=True)
        await asyncio.sleep(settings.scan_interval_seconds)


async def _run_scan(
    settings: Settings,
    data: DataClient,
    analyst: AIAnalyst | None,
    session_factory,
    copier: CopyTrader,
) -> None:
    log = logging.getLogger("agent.scanner")
    log.info("Starting leaderboard scan…")

    fetcher = LeaderboardFetcher(data)
    analyzer = WalletAnalyzer()
    detector = BotDetector()

    candidates = await fetcher.fetch_candidates(top_n=200)
    log.info("Fetched %d candidates", len(candidates))

    scored = []
    for candidate in candidates:
        trades = await data.get_trader_trades(candidate.address, limit=200)
        score = analyzer.score(candidate, trades)
        bot = detector.analyze(candidate.address, trades, candidate.profile)
        score.raw_metrics["bot_signals"] = bot.signals
        score.raw_metrics["bot_confidence"] = bot.bot_confidence
        if score.composite_score >= settings.min_bot_score:
            scored.append((score, bot))

    log.info("%d wallets above min_bot_score=%d", len(scored), settings.min_bot_score)

    # Ask Claude to rank (optional)
    if analyst and settings.has_claude_credentials and scored:
        from .scanner.analyzer import WalletScore
        top_addresses = await analyst.evaluate_wallets(
            [s for s, _ in scored],
            max_select=settings.max_target_wallets,
        )
    else:
        top_addresses = [s.address for s, _ in sorted(scored, key=lambda x: x[0].composite_score, reverse=True)][: settings.max_target_wallets]

    log.info("Selected %d target wallets: %s", len(top_addresses), [a[:8] for a in top_addresses])

    # Persist to DB
    async with session_factory() as session:
        from sqlalchemy import select

        # Deactivate all
        all_wallets_result = await session.execute(select(Wallet))
        for w in all_wallets_result.scalars().all():
            w.is_active = False

        for ws, bot in scored:
            existing = (
                await session.execute(select(Wallet).where(Wallet.address == ws.address))
            ).scalar_one_or_none()

            if existing:
                w = existing
            else:
                w = Wallet(address=ws.address)
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
            wallets_found=len(candidates),
            wallets_selected=len(top_addresses),
        )
        scan_run.set_addresses(top_addresses)
        session.add(scan_run)
        await session.commit()

    # Update in-memory active set
    copier.active_wallets.clear()
    copier.active_wallets.update(top_addresses)
    log.info("Scan complete. Monitoring %d wallets.", len(top_addresses))


def _build_tool_handlers(
    clob: ClobClient,
    gamma: GammaClient,
    session_factory,
    settings: Settings,
) -> dict:
    async def get_market_details(condition_id: str) -> dict:
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
        from .strategy.risk import RiskManager
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
