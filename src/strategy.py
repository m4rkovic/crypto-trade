# src/strategy.py
import time
import asyncio
from typing import Dict
from .models import TickerData, Opportunity
from .risk_engine import RiskEngine
from .inventory import InventoryEngine
from .execution import ExecutionService

class StrategyEngine:
    """
    Event-Driven Strategy.
    Sluša 'on_ticker' evente i odmah reaguje.
    Čuva lokalni keš cena (OrderBook Lite).
    """
    def __init__(self, config, risk: RiskEngine, inventory: InventoryEngine, execution: ExecutionService, logger):
        self.config = config
        self.risk = risk
        self.inventory = inventory
        self.execution = execution
        self.logger = logger
        
        # Lokalni keš cena: { 'BTC/USDT': { 'binance': TickerData, 'okx': TickerData } }
        self.market_cache: Dict[str, Dict[str, TickerData]] = {}
        
        self.target_size_usd = config['target']['sizing_amount']

    async def on_ticker_update(self, ticker: TickerData):
        """
        Glavna petlja logike. Poziva se svaki put kad stigne cena sa BILO KOJE berze.
        """
        # 1. Ažuriraj keš
        if ticker.symbol not in self.market_cache:
            self.market_cache[ticker.symbol] = {}
        self.market_cache[ticker.symbol][ticker.exchange] = ticker

        # 2. Proveri prilike za OVAJ coin (ne iteriraj kroz sve coinove, presporo)
        await self.check_arbitrage(ticker.symbol)

    async def check_arbitrage(self, symbol: str):
        exchanges_data = self.market_cache.get(symbol, {})
        if len(exchanges_data) < 2:
            return

        # Uporedi sve parove berzi za ovaj coin
        # O(N^2) ali N je malo (2-3 berze), tako da je zanemarljivo
        keys = list(exchanges_data.keys())
        for i in range(len(keys)):
            for j in range(len(keys)):
                if i == j: continue
                
                ex_a_name = keys[i]
                ex_b_name = keys[j]
                
                tick_a = exchanges_data[ex_a_name] # Potencijalni BUY
                tick_b = exchanges_data[ex_b_name] # Potencijalni SELL
                
                # --- RISK CHECK 1: DATA AGE ---
                if not self.risk.validate_market_data(tick_a) or not self.risk.validate_market_data(tick_b):
                    continue

                # --- CALCULATION ---
                buy_price = tick_a.ask_price  # Kupujemo po Ask (skuplje)
                sell_price = tick_b.bid_price # Prodajemo po Bid (jeftinije)
                
                if buy_price >= sell_price:
                    continue # Nema profita ni u teoriji

                # Gross Spread (bez provizija)
                spread_bps = ((sell_price - buy_price) / buy_price) * 10000
                
                # Veličina pozicije
                qty = self.target_size_usd / buy_price
                est_profit = (sell_price - buy_price) * qty
                
                # --- FEES CALCULATION ---
                # Pretpostavljamo 0.1% fee na obe strane (ukupno 0.2%) = 20 bps
                fees_usd = (qty * buy_price * 0.001) + (qty * sell_price * 0.001)
                net_profit = est_profit - fees_usd

                # Ako je NETO profit negativan, preskoči
                if net_profit <= 0:
                    continue

                opp = Opportunity(
                    id=f"{symbol}-{int(time.time()*1000)}",
                    symbol=symbol,
                    buy_ex=ex_a_name,
                    sell_ex=ex_b_name,
                    buy_price=buy_price,
                    sell_price=sell_price,
                    quantity=qty,
                    gross_spread_bps=spread_bps,
                    est_profit_usd=net_profit,
                    timestamp=time.time()
                )

                # --- RISK CHECK 2: PRE-TRADE ---
                if self.risk.pre_trade_check(opp):
                    # --- EXECUTION ---
                    self.logger.info(f"✨ FOUND: {symbol} Spread: {spread_bps:.1f}bps | Est. Net: ${net_profit:.3f}")
                    await self.execute_opportunity(opp)

    async def execute_opportunity(self, opp: Opportunity):
        """
        Orkestrira Liquidity Check i Execution.
        """
        base_coin = opp.symbol.split('/')[0]
        quote_coin = 'USDT'
        
        # 1. OPTIMISTIC LIQUIDITY CHECK & LOCK
        # Treba nam USDT na Buy exchange
        cost_usdt = opp.quantity * opp.buy_price
        
        # Treba nam COIN na Sell exchange
        cost_coin = opp.quantity
        
        has_usdt = self.inventory.reserve_liquidity(opp.buy_ex, quote_coin, cost_usdt)
        has_coin = self.inventory.reserve_liquidity(opp.sell_ex, base_coin, cost_coin)
        
        if has_usdt and has_coin:
            # 2. ATOMIC EXECUTION
            success, pnl = await self.execution.execute_atomic(opp)
            
            # 3. POST-TRADE CLEANUP
            self.risk.record_execution_result(success, pnl)
            
            if not success:
                # ROLLBACK ako nije uspelo
                self.inventory.rollback_liquidity(opp.buy_ex, quote_coin, cost_usdt)
                self.inventory.rollback_liquidity(opp.sell_ex, base_coin, cost_coin)
                self.logger.info("Liquidity Rolled Back.")
            else:
                # Ako je uspelo, Inventory Engine će se sam osvežiti pri sledećem sync-u (za 10s)
                # ili možemo implementirati "commit" logiku, ali sync je sigurniji.
                pass
        else:
            # Nije bilo para, rollback ono što je uspelo da se lockuje
            if has_usdt: self.inventory.rollback_liquidity(opp.buy_ex, quote_coin, cost_usdt)
            if has_coin: self.inventory.rollback_liquidity(opp.sell_ex, base_coin, cost_coin)
            # Logujemo throttle da ne spama
            # self.logger.debug(f"Insufficient funds for {opp.symbol}")