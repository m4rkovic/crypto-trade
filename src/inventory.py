# src/inventory.py
import asyncio
from typing import Dict, Optional
import ccxt.async_support as ccxt
import logging

class InventoryEngine:
    def __init__(self, exchanges: Dict[str, ccxt.Exchange], logger: logging.Logger):
        self.exchanges = exchanges
        self.logger = logger
        self.confirmed_balances: Dict[str, Dict[str, float]] = {}
        self.locked_balances: Dict[str, Dict[str, float]] = {}
        self.is_ready = False

    async def sync_balances(self):
        """Povlači tačno stanje sa berzi (REST API)."""
        tasks = [ex.fetch_balance() for ex in self.exchanges.values()]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        names = list(self.exchanges.keys())
        for i, res in enumerate(results):
            if isinstance(res, Exception):
                self.logger.error(f"Failed to sync balance for {names[i]}: {res}")
                continue
            
            # --- PYLANCE FIX ---
            # Eksplicitno proveravamo da li je dict pre nego sto pristupimo kljucevima
            if not isinstance(res, dict):
                continue
            
            if 'free' in res:
                clean_bal = {k: float(v) for k, v in res['free'].items() if float(v) > 0}
                self.confirmed_balances[names[i]] = clean_bal
            
            self.locked_balances[names[i]] = {}
        
        self.is_ready = True
        self.logger.info("Inventory Synchronized.")

    def get_available_balance(self, exchange: str, currency: str) -> float:
        """Vraća: Confirmed - Locked"""
        confirmed = self.confirmed_balances.get(exchange, {}).get(currency, 0.0)
        locked = self.locked_balances.get(exchange, {}).get(currency, 0.0)
        return max(0.0, confirmed - locked)

    def reserve_liquidity(self, exchange: str, currency: str, amount: float) -> bool:
        available = self.get_available_balance(exchange, currency)
        if available >= amount:
            current_lock = self.locked_balances.setdefault(exchange, {}).get(currency, 0.0)
            self.locked_balances[exchange][currency] = current_lock + amount
            return True
        return False

    def rollback_liquidity(self, exchange: str, currency: str, amount: float):
        if exchange in self.locked_balances and currency in self.locked_balances[exchange]:
            self.locked_balances[exchange][currency] = max(0.0, self.locked_balances[exchange][currency] - amount)

    async def run_loop(self):
        while True:
            await self.sync_balances()
            await asyncio.sleep(10)