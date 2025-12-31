import asyncio
from typing import Dict
import ccxt.async_support as ccxt
import logging
import time

class InventoryEngine:
    def __init__(self, exchanges: Dict[str, ccxt.Exchange], logger: logging.Logger):
        self.exchanges = exchanges
        self.logger = logger
        # { 'binance': {'USDT': 100, 'SOL': 1.5} }
        self.state: Dict[str, Dict[str, float]] = {}
        self.is_ready = False

    async def update_balances(self):
        tasks = [ex.fetch_balance() for ex in self.exchanges.values()]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        names = list(self.exchanges.keys())
        for i, res in enumerate(results):
            if isinstance(res, Exception):
                continue
            
            # Filter for non-zero balances to keep memory clean
            clean_bal = {k: v for k, v in res['free'].items() if v > 0}
            self.state[names[i]] = clean_bal
        
        self.is_ready = True

    def get_grand_total_usd(self, prices: Dict[str, float]) -> float:
        """Calculates Total Net Worth across all exchanges using live prices."""
        total_usd = 0.0
        
        for ex_name, assets in self.state.items():
            for coin, amount in assets.items():
                if coin == 'USDT' or coin == 'USD':
                    total_usd += amount
                elif coin in prices:
                    total_usd += amount * prices[coin]
                # Note: If we don't have a price stream for the coin (e.g. Dust), it is ignored.
        
        return total_usd

    def check_liquidity(self, exchange: str, currency: str, amount_needed: float) -> bool:
        if not self.is_ready: return False
        balance = self.state.get(exchange, {}).get(currency, 0.0)
        return balance >= amount_needed

    async def run_loop(self):
        while True:
            await self.update_balances()
            await asyncio.sleep(5)