# main.py
import asyncio
import yaml
import time
import sys
import questionary
from datetime import datetime
from rich.live import Live
from rich.table import Table
from rich.layout import Layout
from rich.console import Console
from rich.panel import Panel

# Import Engines
from src.logger import setup_console_logger, AsyncAuditLogger
from src.market_engine import MarketEngine
from src.websocket_engine import WebSocketEngine
from src.risk_engine import RiskEngine
from src.execution import ExecutionService
from src.inventory import InventoryEngine
from src.models import Opportunity

# --- UI HELPER FUNCTIONS ---

def startup_selection(config):
    """Interactive CLI to select coins and exchanges."""
    print("\n🚀 ALPHA ARB FLEET COMMAND \n")
    coins = questionary.checkbox("Select Assets to Trade:", choices=config['supported_coins']).ask()
    if not coins:
        print("No coins selected. Exiting.")
        sys.exit()

    avail_exchanges = list(config['exchanges'].keys())
    exchanges = questionary.checkbox("Select Exchanges to Activate:", choices=avail_exchanges).ask()
    if len(exchanges) < 2:
        print("Need at least 2 exchanges for arbitrage. Exiting.")
        sys.exit()
    return coins, exchanges

def generate_dashboard(inventory, ws_engine, active_coins, active_exchanges):
    """
    Creates the Rich Console Dashboard layout.
    Shows Live Prices, Inventory, and Total Net Worth.
    """
    
    # 1. Price Table
    price_table = Table(title="📡 Live Market Feed")
    price_table.add_column("Asset", style="cyan")
    price_table.add_column("Price (USD)", justify="right", style="green")
    
    prices = ws_engine.get_all_prices()
    for coin in active_coins:
        base = coin.split('/')[0]
        p = prices.get(base, 0.0)
        price_table.add_row(base, f"${p:,.2f}")

    # 2. Inventory Table
    inv_table = Table(title="💰 Portfolio Composition")
    inv_table.add_column("Exchange", style="magenta")
    inv_table.add_column("USDT", justify="right", style="green")
    inv_table.add_column("Coins Held", justify="right")
    
    total_worth = inventory.get_grand_total_usd(prices)
    
    for ex in active_exchanges:
        data = inventory.state.get(ex, {})
        usdt = data.get('USDT', 0.0)
        
        # --- FIX: Truncate the list of coins to prevent UI breaking ---
        # Filter out USDT and empty balances
        non_usdt = {k:v for k,v in data.items() if k != 'USDT' and v > 0}
        
        # Take only the first 5 coins
        display_items = list(non_usdt.items())[:5]
        others_count = len(non_usdt) - 5
        
        # Format the string
        others_str = ", ".join([f"{k}:{v:.2f}" for k,v in display_items])
        
        if others_count > 0:
            others_str += f", [dim]+{others_count} others[/dim]"
            
        inv_table.add_row(ex.upper(), f"${usdt:,.2f}", others_str if others_str else "-")
        # -------------------------------------------------------------

    # Layout Construction
    layout = Layout()
    layout.split_column(
        Layout(name="top"),
        Layout(name="bottom")
    )
    
    layout["top"].split_row(
        Layout(Panel(price_table)),
        Layout(Panel(inv_table))
    )
    
    footer = Panel(f"[bold gold1]GRAND TOTAL NET WORTH: ${total_worth:,.2f}[/bold gold1]", style="white on blue")
    layout["bottom"].update(footer)
    layout["bottom"].size = 3
    
    return layout

# --- MAIN CONTROLLER ---

class HedgeFundBot:
    def __init__(self, selected_coins, selected_exchanges):
        self.coins = selected_coins
        self.ex_names = selected_exchanges
        self.config = self._load_config()
        self.config['exchanges'] = {k:v for k,v in self.config['exchanges'].items() if k in self.ex_names}
        
        self.audit_log = AsyncAuditLogger(self.config['audit']['trade_log'])
        self.logger = setup_console_logger("AlphaArb", "ERROR") 

        self.ws_engine = WebSocketEngine(self.ex_names, self.coins, self.logger)
        self.rest_engine = MarketEngine(self.config, self.logger)
        self.risk = RiskEngine(self.config, self.logger)
        self.inventory = None
        self.executor = None
        self.target_amt = self.config['target']['sizing_amount']

    def _load_config(self):
        with open("config.yaml", "r") as f: return yaml.safe_load(f)

    def calculate_size(self, price: float) -> float:
        return self.target_amt / price

    async def run(self):
        try:
            print("Initializing Diagnostic Checks...")
            await self.audit_log.start()
            is_healthy = await self.rest_engine.initialize()
            if not is_healthy:
                print("❌ Diagnostic Failed. Check API Keys.")
                return

            await self.ws_engine.start()
            self.inventory = InventoryEngine(self.rest_engine.exchanges, self.logger)
            asyncio.create_task(self.inventory.run_loop())
            
            print("Synchronizing Inventory...")
            while not self.inventory.is_ready: await asyncio.sleep(0.1)

            self.executor = ExecutionService(self.rest_engine.exchanges, self.logger, self.config)

            console = Console()
            with Live(console=console, refresh_per_second=4) as live:
                while not self.risk.kill_switch:
                    start_tick = time.time()
                    live.update(generate_dashboard(self.inventory, self.ws_engine, self.coins, self.ex_names))

                    for coin in self.coins:
                        snapshots = self.ws_engine.get_snapshot(coin)
                        if len(snapshots) < 2: continue
                        
                        for buy_snap in snapshots:
                            for sell_snap in snapshots:
                                if buy_snap.exchange == sell_snap.exchange: continue
                                
                                if not self.risk.validate_market_data(buy_snap) or \
                                   not self.risk.validate_market_data(sell_snap):
                                    continue

                                buy_price = buy_snap.ask_price
                                sell_price = sell_snap.bid_price
                                if sell_price <= buy_price: continue

                                size = self.calculate_size(buy_price)
                                spread_bps = ((sell_price - buy_price) / buy_price) * 10000
                                
                                # --- FIX: Pass symbol=coin into Opportunity ---
                                opp = Opportunity(
                                    id=f"{int(time.time()*1000)}",
                                    symbol=coin, # <--- NEW: Ensures execution.py knows what to trade
                                    buy_ex=buy_snap.exchange, sell_ex=sell_snap.exchange,
                                    buy_price=buy_price, sell_price=sell_price,
                                    quantity=size, gross_spread_bps=spread_bps,
                                    net_profit_usd=(sell_price - buy_price) * size,
                                    timestamp=time.time()
                                )

                                if self.risk.pre_trade_check(opp):
                                    base_coin = coin.split('/')[0]
                                    has_quote = self.inventory.check_liquidity(opp.buy_ex, 'USDT', opp.quantity * opp.buy_price)
                                    has_base = self.inventory.check_liquidity(opp.sell_ex, base_coin, opp.quantity)
                                    
                                    if has_quote and has_base:
                                        success, pnl = await self.executor.execute_atomic(opp)
                                        self.risk.record_execution_result(success, pnl)
                                        await self.audit_log.log_trade([datetime.utcnow().isoformat(), opp.id, success, pnl])

                    elapsed = time.time() - start_tick
                    await asyncio.sleep(max(0, 0.1 - elapsed))
        finally:
            print("Shutting down resources...")
            await self.ws_engine.shutdown()
            await self.rest_engine.shutdown()

if __name__ == "__main__":
    with open("config.yaml", "r") as f:
        raw_conf = yaml.safe_load(f)
    try:
        sel_coins, sel_exs = startup_selection(raw_conf)
        bot = HedgeFundBot(sel_coins, sel_exs)
        try:
            import uvloop 
            asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
        except ImportError:
            pass
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        print("\n🛑 Bot Stopped by User.")
        sys.exit()