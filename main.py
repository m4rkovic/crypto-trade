# main.py
import asyncio
import yaml
import sys
import signal
import questionary
from rich.live import Live
from rich.table import Table
from rich.layout import Layout
from rich.panel import Panel

# Import Engines
from src.logger import setup_console_logger, AsyncAuditLogger
from src.market_engine import MarketEngine
from src.websocket_engine import WebSocketEngine
from src.risk_engine import RiskEngine
from src.execution import ExecutionService
from src.inventory import InventoryEngine
from src.strategy import StrategyEngine 

# Global shutdown event
shutdown_event = asyncio.Event()

def handle_signal():
    print("\n🛑 Signal received. Shutting down...")
    shutdown_event.set()

def startup_selection(config):
    """Interactive CLI to select coins and exchanges."""
    print("\n🚀 ALPHA ARB FLEET V3 (EVENT DRIVEN) \n")
    
    # 1. Select Coins
    coins = questionary.checkbox(
        "Select Assets to Trade:", 
        choices=config['supported_coins']
    ).ask()
    
    if not coins:
        print("No coins selected. Exiting.")
        sys.exit()

    # 2. Select Exchanges
    avail_exchanges = list(config['exchanges'].keys())
    exchanges = questionary.checkbox(
        "Select Exchanges to Activate:", 
        choices=avail_exchanges
    ).ask()
    
    if len(exchanges) < 2:
        print("Need at least 2 exchanges for arbitrage. Exiting.")
        sys.exit()
        
    return coins, exchanges

async def ui_updater(risk, strategy, inventory):
    """
    Napredniji UI task koji prikazuje cene i profit u realnom vremenu.
    """
    def generate_dashboard():
        layout = Layout()
        layout.split_column(Layout(name="top"), Layout(name="bottom"))
        
        # Tabela 1: Performanse
        perf_table = Table(title="📊 Performance")
        perf_table.add_column("Metric", style="cyan")
        perf_table.add_column("Value", style="green")
        perf_table.add_row("Daily PnL", f"${risk.daily_pnl:.2f}")
        perf_table.add_row("Fails / KillSwitch", f"{risk.consecutive_fails} / {risk.kill_switch}")
        
        # Tabela 2: Live Cene (Iz Strategy Cache-a)
        price_table = Table(title="⚡ Live Market Feed (Event Driven)")
        price_table.add_column("Asset", style="bold yellow")
        price_table.add_column("Prices (Ask)", style="white")
        
        # Iteriramo kroz podatke koje Strategija vidi
        for symbol, data in strategy.market_cache.items():
            prices = []
            for ex_name, ticker in data.items():
                prices.append(f"{ex_name.upper()}: {ticker.ask_price:.2f}")
            price_table.add_row(symbol, " | ".join(prices))

        layout["top"].split_row(
            Layout(Panel(perf_table)), 
            Layout(Panel(price_table))
        )
        layout["bottom"].update(Panel(f"[bold]Active Strategy:[/bold] Monitoring {len(strategy.market_cache)} assets..."))
        
        return layout

    with Live(generate_dashboard(), refresh_per_second=2) as live:
        while not shutdown_event.is_set():
            live.update(generate_dashboard())
            await asyncio.sleep(0.5)

async def main(config):
    # Logger
    logger = setup_console_logger("AlphaArb", config['system']['log_level'])
    audit = AsyncAuditLogger(config['audit']['trade_log'])
    await audit.start()

    # 1. Initialize Engines
    market = MarketEngine(config, logger)
    risk = RiskEngine(config, logger)
    inventory = InventoryEngine(market.exchanges, logger)
    
    # 2. REST Connection & Initial Sync
    logger.info("Initializing REST API...")
    if not await market.initialize():
        logger.error("REST Initialization failed.")
        return

    logger.info("Syncing Wallet Balances...")
    await inventory.sync_balances()
    asyncio.create_task(inventory.run_loop())

    # 3. Execution Service
    execution = ExecutionService(market.exchanges, logger, config)

    # 4. Strategy Engine (The Brain)
    strategy = StrategyEngine(config, risk, inventory, execution, logger)

    # 5. WebSocket Engine (The Eyes)
    # We pass the strategy callback directly to WS Engine
    ws_engine = WebSocketEngine(
        list(config['exchanges'].keys()),
        config['supported_coins'],
        logger,
        strategy.on_ticker_update 
    )

    # Start WS
    asyncio.create_task(ws_engine.start())

    # Start UI (Non-blocking)
    ui_task = asyncio.create_task(ui_updater(risk, strategy, inventory))

    logger.info("🚀 BOT IS LIVE. Press Ctrl+C to stop.")
    
    # Wait for shutdown signal
    loop = asyncio.get_running_loop()
    try:
        loop.add_signal_handler(signal.SIGINT, handle_signal)
        loop.add_signal_handler(signal.SIGTERM, handle_signal)
    except NotImplementedError:
        pass
    
    await shutdown_event.wait()

    # Cleanup
    logger.info("Shutting down engines...")
    await ws_engine.shutdown()
    await market.shutdown()

if __name__ == "__main__":
    # 1. Load Config FIRST
    with open("config.yaml", "r") as f: 
        raw_config = yaml.safe_load(f)
    
    # 2. Interactive Selection (RUNS SYNCHRONOUSLY BEFORE ASYNC LOOP)
    selected_coins, selected_exchanges = startup_selection(raw_config)
    
    # 3. Prepare Config
    config = raw_config.copy()
    config['supported_coins'] = selected_coins
    config['exchanges'] = {k:v for k,v in raw_config['exchanges'].items() if k in selected_exchanges}

    # 4. Start Bot (Klasican asyncio run za Windows)
    asyncio.run(main(config))