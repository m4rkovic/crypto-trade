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
import time

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
    
    # 1. Select Coins (With Pre-selection logic)
    # Pravimo listu opcija gde kazemo sta je stiklirano po defaultu
    coin_choices = []
    for coin in config['supported_coins']:
        # Ako je SOL/USDT, stavi checked=True, inace False
        is_checked = (coin == "SOL/USDT")
        coin_choices.append({"name": coin, "checked": is_checked})

    coins = questionary.checkbox("Select Assets:", choices=coin_choices).ask()
    if not coins: sys.exit()

    # 2. Select Exchanges (With Pre-selection logic)
    avail_exchanges = list(config['exchanges'].keys())
    ex_choices = []
    for ex in avail_exchanges:
        # Zelimo Binance i Bybit po defaultu
        is_checked = ex in ['binance', 'bybit']
        ex_choices.append({"name": ex, "checked": is_checked})

    exchanges = questionary.checkbox("Select Exchanges:", choices=ex_choices).ask()
    if len(exchanges) < 2: sys.exit()
    
    # 3. Manual Trade Size Input (Novo!)
    # Trazimo od korisnika da unese iznos. Default je 20.0
    size_str = questionary.text("Enter Trade Size (USD):", default="20.0").ask()
    try:
        trade_size = float(size_str)
    except ValueError:
        print("Invalid number entered. Using default $20.0")
        trade_size = 20.0
        
    return coins, exchanges, trade_size

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
        
        # --- ANIMATED HEADER ---
        dots = "." * (int(time.time()) % 4)
        status_text = f"[green]Scanning Market{dots}[/green]"
        
        layout["header"].update(Panel(f"[bold blue]ALPHA ARB FLEET V3[/bold blue] | [yellow]Strategy: Event Driven[/yellow] | {status_text}", box=box.ROUNDED))

        # 1. Performance Table
        perf_table = Table(title="📊 Live Performance", box=box.SIMPLE, expand=True)
        perf_table.add_column("Metric", style="cyan")
        perf_table.add_column("Value", style="bold white", justify="right")
        
        # PnL
        pnl_color = "green" if risk.daily_pnl >= 0 else "red"
        perf_table.add_row("💰 Daily PnL", f"[{pnl_color}]${risk.daily_pnl:.4f}[/{pnl_color}]")
        
        # Win Rate
        if risk.total_attempts > 0:
            win_rate = (risk.success_count / risk.total_attempts) * 100
            win_rate_str = f"{win_rate:.1f}%"
            wr_color = "green" if win_rate > 50 else "yellow" if win_rate > 30 else "red"
        else:
            win_rate_str = "0.0%"
            wr_color = "white"

        perf_table.add_section()
        perf_table.add_row("🎯 Win Rate", f"[{wr_color}]{win_rate_str}[/{wr_color}]")
        perf_table.add_row("TOTAL TRADES", str(risk.total_attempts))
        perf_table.add_row("✅ Successful", f"[green]{risk.success_count}[/green]")
        perf_table.add_row("❌ Failed", f"[red]{risk.fail_count}[/red]")
        
        perf_table.add_section()
        # --- NOVO: Prikazujemo velicinu trejda koju si uneo ---
        perf_table.add_row("💵 Trade Size", f"[bold yellow]${strategy.target_size_usd:.2f}[/bold yellow]")
        perf_table.add_row("📡 Mkt Scans", f"[yellow]{risk.checks_count}[/yellow]")
        perf_table.add_row("💀 KillSwitch", f"[bold red]{risk.kill_switch}[/bold red]" if risk.kill_switch else "[dim]False[/dim]")

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
        main_body.split_column(Layout(Panel(perf_table, box=box.ROUNDED), size=14), body_layout) # Malo veci panel za stats
        
        layout["body"].update(main_body)
        
        # --- STATUS PANEL ---
        layout["status"].update(Panel(f"Last Action: {risk.last_trade_info}", title="⚡ Trade Log", style="white", box=box.ROUNDED))
        
        layout["footer"].update(Panel(f"[bold]TOTAL ESTIMATED VALUE: ${total_usdt_value:,.2f}[/bold]", style="white on blue", box=box.ROUNDED))
        return layout

    console = Console()
    console.clear()
    
    with Live(generate_dashboard(), refresh_per_second=1, screen=True) as live:
        while not shutdown_event.is_set():
            live.update(generate_dashboard())
            await asyncio.sleep(1.0) 

async def main(config):
    logger = setup_console_logger("AlphaArb", config['system']['log_level'])
    
    # 1. Audit Logger Initialization
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
    
    strategy = StrategyEngine(config, risk, inventory, execution, logger, audit)

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
    
    # Sada hvatamo i 'trade_size' koji korisnik unese
    selected_coins, selected_exchanges, trade_size = startup_selection(raw_config)
    
    config = raw_config.copy()
    config['supported_coins'] = selected_coins
    config['exchanges'] = {k:v for k,v in raw_config['exchanges'].items() if k in selected_exchanges}
    
    # --- OVERRIDE CONFIG SA UNETOM VREDNOSCU ---
    # Ovo gazi ono sto pise u config.yaml samo za ovu sesiju
    config['target']['sizing_amount'] = trade_size

    asyncio.run(main(config))