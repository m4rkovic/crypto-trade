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
        """Povlači tačno stanje sa berzi (REST API). Koristi se kao 'Sanity Check'."""
        tasks = [ex.fetch_balance() for ex in self.exchanges.values()]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        names = list(self.exchanges.keys())
        for i, res in enumerate(results):
            if isinstance(res, Exception):
                self.logger.error(f"Failed to sync balance for {names[i]}: {res}")
                continue
            
            if not isinstance(res, dict):
                continue
            
            # --- LOCAL LEDGER RECONCILIATION ---
            # Ovde osvežavamo stvarno stanje. Ako se Local Ledger malo "razdesio", ovo ga vraća u vinklu.
            if 'free' in res:
                clean_bal = {k: float(v) for k, v in res['free'].items() if float(v) > 0}
                self.confirmed_balances[names[i]] = clean_bal
            
            # Resetujemo lockove pri svakom sync-u da izbegnemo "ghost locks"
            # (Osim ako nismo u sred trejda, ali to je rizik pollinga)
            self.locked_balances[names[i]] = {}
        
        self.is_ready = True
        self.logger.info("Inventory Synchronized (REST).")

    def get_available_balance(self, exchange: str, currency: str) -> float:
        """Vraća: Confirmed - Locked"""
        confirmed = self.confirmed_balances.get(exchange, {}).get(currency, 0.0)
        locked = self.locked_balances.get(exchange, {}).get(currency, 0.0)
        return max(0.0, confirmed - locked)

    def reserve_liquidity(self, exchange: str, currency: str, amount: float) -> bool:
        """Privremeno zaključava sredstva pre slanja ordera."""
        available = self.get_available_balance(exchange, currency)
        if available >= amount:
            current_lock = self.locked_balances.setdefault(exchange, {}).get(currency, 0.0)
            self.locked_balances[exchange][currency] = current_lock + amount
            return True
        return False

    def rollback_liquidity(self, exchange: str, currency: str, amount: float):
        """Vraća sredstva u opticaj ako trejd propadne."""
        if exchange in self.locked_balances and currency in self.locked_balances[exchange]:
            self.locked_balances[exchange][currency] = max(0.0, self.locked_balances[exchange][currency] - amount)

    def confirm_trade(self, exchange: str, symbol: str, side: str, amount: float, price: float, fee_rate: float):
        """
        Ažurira lokalno stanje ODMAH nakon uspešnog trejda i SKIDA LOCK.
        Ovo je ključna komponenta 'Local Ledger' sistema.
        """
        base, quote = symbol.split('/')
        cost_usdt = amount * price
        
        if side == 'buy':
            # 1. Oslobodi Lock na USDT (jer smo ga sad stvarno potrošili)
            self.rollback_liquidity(exchange, quote, cost_usdt)
            
            # 2. Smanji USDT balans
            current_quote = self.confirmed_balances.get(exchange, {}).get(quote, 0.0)
            self.confirmed_balances[exchange][quote] = max(0.0, current_quote - cost_usdt)
            
            # 3. Povećaj Base balans (Coin koji smo kupili, umanjen za fee)
            current_base = self.confirmed_balances.get(exchange, {}).get(base, 0.0)
            recv_amount = amount * (1 - fee_rate)
            self.confirmed_balances[exchange][base] = current_base + recv_amount

        elif side == 'sell':
            # 1. Oslobodi Lock na Base (jer smo ga prodali)
            self.rollback_liquidity(exchange, base, amount)
            
            # 2. Smanji Base balans
            current_base = self.confirmed_balances.get(exchange, {}).get(base, 0.0)
            self.confirmed_balances[exchange][base] = max(0.0, current_base - amount)
            
            # 3. Povećaj USDT balans (Dobijen USDT, umanjen za fee)
            current_quote = self.confirmed_balances.get(exchange, {}).get(quote, 0.0)
            recv_usdt = cost_usdt * (1 - fee_rate)
            self.confirmed_balances[exchange][quote] = current_quote + recv_usdt
            
        self.logger.info(f"⚡ Local Ledger Updated: {exchange} {symbol} {side} (Fee: {fee_rate*100}%)")

    async def run_loop(self):
        while True:
            await self.sync_balances()
            # Pošto sada imamo Local Ledger, ne moramo da spamujemo REST API svakih 10s.
            # Možemo da povećamo interval na 60s, jer verujemo našoj lokalnoj matematici.
            await asyncio.sleep(60)