# src/strategy.py
import time
import asyncio
from typing import Dict, Optional
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
        Glavna petlja logike. Poziva se svaki put kad stigne cena sa BILO KOJE berze (callback iz WS-a).
        """
        # 1. Ažuriraj keš (Ovo je logika koja je pre bila u WebSocketEngine-u)
        if ticker.symbol not in self.market_cache:
            self.market_cache[ticker.symbol] = {}
        self.market_cache[ticker.symbol][ticker.exchange] = ticker

        # 2. Proveri prilike za OVAJ coin odmah
        await self.check_arbitrage(ticker.symbol)

    async def check_arbitrage(self, symbol: str):
        exchanges_data = self.market_cache.get(symbol, {})
        # Trebaju nam bar 2 berze da bi imali šta da poredimo
        if len(exchanges_data) < 2:
            return

        # Uporedi sve parove berzi za ovaj coin
        keys = list(exchanges_data.keys())
        for i in range(len(keys)):
            for j in range(len(keys)):
                if i == j: continue
                
                ex_a_name = keys[i]
                ex_b_name = keys[j]
                
                tick_a = exchanges_data[ex_a_name] # Potencijalni BUY
                tick_b = exchanges_data[ex_b_name] # Potencijalni SELL
                
                # --- ZERO PRICE PROTECTION ---
                # Ako je neka cena 0 (nema likvidnosti na testnetu), preskoci
                if tick_a.ask_price <= 0 or tick_b.bid_price <= 0:
                    continue

                # --- RISK CHECK 1: DATA AGE ---
                if not self.risk.validate_market_data(tick_a) or not self.risk.validate_market_data(tick_b):
                    continue

                # --- CALCULATION ---
                buy_price = tick_a.ask_price  # Kupujemo po Ask (skuplje)
                sell_price = tick_b.bid_price # Prodajemo po Bid (jeftinije)
                
                # Ako je kupovna cena veća od prodajne, gubimo pare -> preskoči
                if buy_price >= sell_price:
                    continue 

                # Gross Spread (bez provizija) u baznim poenima (bps)
                spread_bps = ((sell_price - buy_price) / buy_price) * 10000
                
                # Veličina pozicije
                qty = self.target_size_usd / buy_price
                est_profit = (sell_price - buy_price) * qty
                
                # --- FEES CALCULATION ---
                # Pretpostavljamo 0.1% fee na obe strane (ukupno 0.2%)
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
                    net_profit_usd=net_profit,
                    timestamp=time.time()
                )

                # --- RISK CHECK 2: PRE-TRADE ---
                if self.risk.pre_trade_check(opp):
                    # --- EXECUTION ---
                    self.logger.info(f"✨ FOUND: {symbol} Spread: {spread_bps:.1f}bps | Est. Net: ${net_profit:.3f}")
                    await self.execute_opportunity(opp)

    async def execute_opportunity(self, opp: Opportunity):
        """Orkestrira Liquidity Check i Execution."""
        # 1. Update UI Status ODMAH
        msg = f"ATTEMPT: Buy {opp.symbol} on {opp.buy_ex.upper()} @ ${opp.buy_price:.4f} -> Sell on {opp.sell_ex.upper()}"
        self.risk.update_last_trade_status(msg)

        base_coin = opp.symbol.split('/')[0]
        quote_coin = 'USDT'
        
        # 1. OPTIMISTIC LIQUIDITY CHECK & LOCK
        # Koliko nam treba para?
        cost_usdt = opp.quantity * opp.buy_price
        cost_coin = opp.quantity
        
        has_usdt = self.inventory.reserve_liquidity(opp.buy_ex, quote_coin, cost_usdt)
        has_coin = self.inventory.reserve_liquidity(opp.sell_ex, base_coin, cost_coin)
        
        if has_usdt and has_coin:
            # 2. ATOMIC EXECUTION (Šaljemo ordere)
            success, pnl = await self.execution.execute_atomic(opp)
            
            # 3. POST-TRADE CLEANUP
            self.risk.record_execution_result(success, pnl)
            
            if not success:
                # ROLLBACK ako nije uspelo - vraćamo pare u inventory
                self.inventory.rollback_liquidity(opp.buy_ex, quote_coin, cost_usdt)
                self.inventory.rollback_liquidity(opp.sell_ex, base_coin, cost_coin)
        else:
            self.risk.update_last_trade_status(f"[yellow]SKIPPED[/yellow] | Insufficient Liquidity for {opp.symbol}")
            # Nije bilo dovoljno para, rollback ono što je uspelo da se lockuje
            if has_usdt: self.inventory.rollback_liquidity(opp.buy_ex, quote_coin, cost_usdt)
            if has_coin: self.inventory.rollback_liquidity(opp.sell_ex, base_coin, cost_coin)