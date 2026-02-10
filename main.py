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
from rich.console import Console
from rich import box

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
    shutdown_event.set()

def startup_selection(config):
    """Interactive CLI to select coins and exchanges."""
    print("\n🚀 ALPHA ARB FLEET V3 (EVENT DRIVEN) \n")
    
    coins = questionary.checkbox("Select Assets:", choices=config['supported_coins']).ask()
    if not coins: sys.exit()

    avail_exchanges = list(config['exchanges'].keys())
    exchanges = questionary.checkbox("Select Exchanges:", choices=avail_exchanges).ask()
    if len(exchanges) < 2: sys.exit()
        
    return coins, exchanges

async def ui_updater(risk, strategy, inventory, active_coins, active_exchanges):
    """
    Sporiji UI task za bolju citljivost.
    """
    def generate_dashboard():
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="body"),
            Layout(name="status", size=3),
            Layout(name="footer", size=3)
        )
        
        layout["header"].update(Panel(f"[bold blue]ALPHA ARB FLEET V3[/bold blue] | [yellow]Strategy: Event Driven[/yellow] | [green]Status: RUNNING[/green]", box=box.ROUNDED))

        # 1. Performance Table (NOVA STATISTIKA)
        perf_table = Table(title="📊 Stats", box=box.SIMPLE, expand=True)
        perf_table.add_column("Metric", style="cyan")
        perf_table.add_column("Value", style="bold white")
        
        perf_table.add_row("Daily PnL", f"[green]${risk.daily_pnl:.4f}[/green]" if risk.daily_pnl >= 0 else f"[red]${risk.daily_pnl:.4f}[/red]")
        perf_table.add_row("Attempts", str(risk.total_attempts))
        perf_table.add_row("Success", f"[green]{risk.success_count}[/green]")
        perf_table.add_row("Failed", f"[red]{risk.fail_count}[/red]")
        perf_table.add_row("KillSwitch", str(risk.kill_switch))

        # 2. Live Prices
        price_table = Table(title="⚡ Live Prices (Ask)", box=box.SIMPLE, expand=True)
        price_table.add_column("Asset", style="bold yellow")
        for ex in active_exchanges:
            price_table.add_column(ex.upper(), justify="right")
        
        for coin in active_coins:
            row_data = [coin]
            market_data = strategy.market_cache.get(coin, {})
            for ex in active_exchanges:
                ticker = market_data.get(ex)
                if ticker and ticker.ask_price > 0:
                    row_data.append(f"${ticker.ask_price:,.4f}")
                else:
                    row_data.append("[dim]-[/dim]")
            price_table.add_row(*row_data)

        # 3. Balances
        bal_table = Table(title="💰 Wallet Balances", box=box.SIMPLE, expand=True)
        bal_table.add_column("Exchange", style="magenta")
        bal_table.add_column("USDT", justify="right", style="green")
        
        base_coins = [c.split('/')[0] for c in active_coins]
        for base in base_coins:
            bal_table.add_column(f"{base}", justify="right")
            bal_table.add_column(f"{base} $", justify="right", style="dim")

        total_usdt_value = 0.0

        for ex in active_exchanges:
            confirmed = inventory.confirmed_balances.get(ex, {})
            usdt_bal = confirmed.get('USDT', 0.0)
            total_usdt_value += usdt_bal
            
            row = [ex.upper(), f"${usdt_bal:,.2f}"]
            
            for base in base_coins:
                coin_bal = confirmed.get(base, 0.0)
                price = 0.0
                market_data = strategy.market_cache.get(f"{base}/USDT", {})
                if market_data:
                    for t in market_data.values():
                        if t.ask_price > 0:
                            price = t.ask_price
                            break
                
                val = coin_bal * price
                total_usdt_value += val
                
                row.append(f"{coin_bal:.4f}")
                row.append(f"${val:,.2f}" if val > 0 else "-")
            
            bal_table.add_row(*row)

        body_layout = Layout()
        body_layout.split_row(
            Layout(Panel(price_table, box=box.ROUNDED)),
            Layout(Panel(bal_table, box=box.ROUNDED))
        )
        main_body = Layout()
        main_body.split_column(Layout(Panel(perf_table, box=box.ROUNDED), size=8), body_layout)
        
        layout["body"].update(main_body)
        
        # --- NEW STATUS PANEL ---
        # Ovo prikazuje poslednji pokusaj trejda
        layout["status"].update(Panel(f"Last Action: {risk.last_trade_info}", title="⚡ Trade Log", style="white", box=box.ROUNDED))
        
        layout["footer"].update(Panel(f"[bold]TOTAL ESTIMATED VALUE: ${total_usdt_value:,.2f}[/bold]", style="white on blue", box=box.ROUNDED))
        return layout

    console = Console()
    console.clear()
    
    # Refresh rate stavljen na 2 sekunde da mozes da stignes da procitas
    with Live(generate_dashboard(), refresh_per_second=1, screen=True) as live:
        while not shutdown_event.is_set():
            live.update(generate_dashboard())
            await asyncio.sleep(2.0) # Sacekaj 2 sekunde pre sledeceg refresha

async def main(config):
    logger = setup_console_logger("AlphaArb", config['system']['log_level'])
    audit = AsyncAuditLogger(config['audit']['trade_log'])
    await audit.start()

    is_testnet = str(config['system']['environment']).lower() == 'testnet'

    market = MarketEngine(config, logger)
    risk = RiskEngine(config, logger)
    inventory = InventoryEngine(market.exchanges, logger)
    
    logger.info(f"Initializing REST API (Testnet: {is_testnet})...")
    if not await market.initialize():
        return

    logger.info("Syncing Wallet Balances...")
    await inventory.sync_balances()
    asyncio.create_task(inventory.run_loop())

    execution = ExecutionService(market.exchanges, logger, config)
    strategy = StrategyEngine(config, risk, inventory, execution, logger)

    ws_engine = WebSocketEngine(
        list(config['exchanges'].keys()),
        config['supported_coins'],
        logger,
        strategy.on_ticker_update,
        testnet=is_testnet
    )

    asyncio.create_task(ws_engine.start())

    active_coins = config['supported_coins']
    active_exchanges = list(config['exchanges'].keys())
    
    ui_task = asyncio.create_task(ui_updater(risk, strategy, inventory, active_coins, active_exchanges))

    loop = asyncio.get_running_loop()
    try:
        loop.add_signal_handler(signal.SIGINT, handle_signal)
    except: pass
    
    await shutdown_event.wait()

    await ws_engine.shutdown()
    await market.shutdown()

if __name__ == "__main__":
    with open("config.yaml", "r") as f: raw_config = yaml.safe_load(f)
    selected_coins, selected_exchanges = startup_selection(raw_config)
    config = raw_config.copy()
    config['supported_coins'] = selected_coins
    config['exchanges'] = {k:v for k,v in raw_config['exchanges'].items() if k in selected_exchanges}
    asyncio.run(main(config))
    
# # main.py
# import asyncio
# import yaml
# import sys
# import signal
# import questionary
# from rich.live import Live
# from rich.table import Table
# from rich.layout import Layout
# from rich.panel import Panel
# from rich.console import Console
# from rich import box

# # Import Engines
# from src.logger import setup_console_logger, AsyncAuditLogger
# from src.market_engine import MarketEngine
# from src.websocket_engine import WebSocketEngine
# from src.risk_engine import RiskEngine
# from src.execution import ExecutionService
# from src.inventory import InventoryEngine
# from src.strategy import StrategyEngine 

# # Global shutdown event
# shutdown_event = asyncio.Event()

# def handle_signal():
#     print("\n🛑 Signal received. Shutting down...")
#     shutdown_event.set()

# def startup_selection(config):
#     """Interactive CLI to select coins and exchanges."""
#     print("\n🚀 ALPHA ARB FLEET V3 (EVENT DRIVEN) \n")
    
#     # 1. Select Coins
#     coins = questionary.checkbox(
#         "Select Assets to Trade:", 
#         choices=config['supported_coins']
#     ).ask()
    
#     if not coins:
#         print("No coins selected. Exiting.")
#         sys.exit()

#     # 2. Select Exchanges
#     avail_exchanges = list(config['exchanges'].keys())
#     exchanges = questionary.checkbox(
#         "Select Exchanges to Activate:", 
#         choices=avail_exchanges
#     ).ask()
    
#     if len(exchanges) < 2:
#         print("Need at least 2 exchanges for arbitrage. Exiting.")
#         sys.exit()
        
#     return coins, exchanges

# async def ui_updater(risk, strategy, inventory, active_coins, active_exchanges):
#     """
#     UI task that updates the dashboard in-place.
#     """
#     def generate_dashboard():
#         # Main Layout
#         layout = Layout()
#         layout.split_column(
#             Layout(name="header", size=3),
#             Layout(name="body"),
#             Layout(name="footer", size=3)
#         )
        
#         # Header
#         layout["header"].update(Panel(
#             f"[bold blue]ALPHA ARB FLEET V3[/bold blue] | [yellow]Strategy: Event Driven[/yellow] | [green]Status: RUNNING[/green]", 
#             box=box.ROUNDED,
#             style="white on black"
#         ))

#         # 1. Performance Table
#         perf_table = Table(title="📊 Risk & Performance", box=box.SIMPLE, expand=True)
#         perf_table.add_column("Metric", style="cyan")
#         perf_table.add_column("Value", style="bold green")
#         perf_table.add_row("Daily PnL", f"${risk.daily_pnl:.2f}")
#         perf_table.add_row("Fails / KillSwitch", f"{risk.consecutive_fails} / {risk.kill_switch}")
#         perf_table.add_row("Active Coins", str(len(active_coins)))

#         # 2. Live Prices Table
#         price_table = Table(title="⚡ Live Prices (Ask)", box=box.SIMPLE, expand=True)
#         price_table.add_column("Asset", style="bold yellow")
#         for ex in active_exchanges:
#             price_table.add_column(ex.upper(), justify="right")
        
#         for coin in active_coins:
#             row_data = [coin]
#             market_data = strategy.market_cache.get(coin, {})
#             for ex in active_exchanges:
#                 ticker = market_data.get(ex)
#                 if ticker:
#                     row_data.append(f"${ticker.ask_price:,.4f}")
#                 else:
#                     row_data.append("-")
#             price_table.add_row(*row_data)

#         # 3. Wallet Balances Table
#         bal_table = Table(title="💰 Wallet Balances", box=box.SIMPLE, expand=True)
#         bal_table.add_column("Exchange", style="magenta")
#         bal_table.add_column("USDT Free", justify="right", style="green")
        
#         # Base coins (e.g. SOL)
#         base_coins = [c.split('/')[0] for c in active_coins]
#         for base in base_coins:
#             bal_table.add_column(f"{base} Free", justify="right")
#             bal_table.add_column(f"{base} Value ($)", justify="right", style="dim")

#         total_usdt_value = 0.0

#         for ex in active_exchanges:
#             # Get confirmed balance from inventory engine
#             confirmed = inventory.confirmed_balances.get(ex, {})
            
#             usdt_bal = confirmed.get('USDT', 0.0)
#             total_usdt_value += usdt_bal
            
#             row = [ex.upper(), f"${usdt_bal:,.2f}"]
            
#             for base in base_coins:
#                 coin_bal = confirmed.get(base, 0.0)
                
#                 # Estimate value based on first available price
#                 price = 0.0
#                 market_data = strategy.market_cache.get(f"{base}/USDT", {})
#                 if market_data:
#                     first_ticker = list(market_data.values())[0]
#                     price = first_ticker.ask_price
                
#                 coin_value_usd = coin_bal * price
#                 total_usdt_value += coin_value_usd
                
#                 row.append(f"{coin_bal:.4f}")
#                 row.append(f"${coin_value_usd:,.2f}" if coin_value_usd > 0 else "-")
            
#             bal_table.add_row(*row)

#         # Body Layout (Split horizontally)
#         body_layout = Layout()
#         body_layout.split_row(
#             Layout(Panel(price_table, box=box.ROUNDED)),
#             Layout(Panel(bal_table, box=box.ROUNDED))
#         )
        
#         # Combine Performance + Body
#         main_body = Layout()
#         main_body.split_column(
#             Layout(Panel(perf_table, box=box.ROUNDED), size=6),
#             body_layout
#         )
        
#         layout["body"].update(main_body)
        
#         # Footer
#         layout["footer"].update(Panel(
#             f"[bold]GRAND TOTAL ESTIMATED VALUE: ${total_usdt_value:,.2f}[/bold]", 
#             style="white on blue", 
#             box=box.ROUNDED
#         ))
        
#         return layout

#     # Ensure screen is cleared initially
#     console = Console()
#     console.clear()
    
#     with Live(generate_dashboard(), refresh_per_second=2, screen=True) as live:
#         while not shutdown_event.is_set():
#             live.update(generate_dashboard())
#             await asyncio.sleep(0.5)

# async def main(config):
#     # Logger
#     logger = setup_console_logger("AlphaArb", config['system']['log_level'])
#     audit = AsyncAuditLogger(config['audit']['trade_log'])
#     await audit.start()

#     # Testnet check
#     is_testnet = str(config['system']['environment']).lower() == 'testnet'

#     # 1. Initialize Engines
#     market = MarketEngine(config, logger)
#     risk = RiskEngine(config, logger)
#     inventory = InventoryEngine(market.exchanges, logger)
    
#     # 2. REST Connection & Initial Sync
#     logger.info(f"Initializing REST API (Testnet: {is_testnet})...")
#     if not await market.initialize():
#         logger.error("REST Initialization failed.")
#         return

#     logger.info("Syncing Wallet Balances...")
#     await inventory.sync_balances()
#     asyncio.create_task(inventory.run_loop())

#     # 3. Execution Service
#     execution = ExecutionService(market.exchanges, logger, config)

#     # 4. Strategy Engine
#     strategy = StrategyEngine(config, risk, inventory, execution, logger)

#     # 5. WebSocket Engine
#     ws_engine = WebSocketEngine(
#         list(config['exchanges'].keys()),
#         config['supported_coins'],
#         logger,
#         strategy.on_ticker_update,
#         testnet=is_testnet
#     )

#     # Start WS
#     asyncio.create_task(ws_engine.start())

#     # Start UI (Non-blocking)
#     active_coins = config['supported_coins']
#     active_exchanges = list(config['exchanges'].keys())
    
#     ui_task = asyncio.create_task(ui_updater(risk, strategy, inventory, active_coins, active_exchanges))

#     logger.info("🚀 BOT IS LIVE. Press Ctrl+C to stop.")
    
#     # Wait for shutdown signal
#     loop = asyncio.get_running_loop()
#     try:
#         loop.add_signal_handler(signal.SIGINT, handle_signal)
#         loop.add_signal_handler(signal.SIGTERM, handle_signal)
#     except NotImplementedError:
#         pass
    
#     await shutdown_event.wait()

#     # Cleanup
#     logger.info("Shutting down engines...")
#     await ws_engine.shutdown()
#     await market.shutdown()

# if __name__ == "__main__":
#     # 1. Load Config FIRST
#     with open("config.yaml", "r") as f: 
#         raw_config = yaml.safe_load(f)
    
#     # 2. Interactive Selection
#     selected_coins, selected_exchanges = startup_selection(raw_config)
    
#     # 3. Prepare Config
#     config = raw_config.copy()
#     config['supported_coins'] = selected_coins
#     config['exchanges'] = {k:v for k,v in raw_config['exchanges'].items() if k in selected_exchanges}

#     # 4. Start Bot
#     asyncio.run(main(config))
    