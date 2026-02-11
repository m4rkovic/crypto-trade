# src/execution.py
import asyncio
from typing import Tuple, Dict, Any, Optional, cast
from .models import Opportunity

class ExecutionService:
    """
    Handles the high-stakes logic of placing orders.
    Enforces 'Atomic' execution (All-or-Nothing attempts) and handles
    partial fill failures (Orphans) to preserve capital.
    """
    def __init__(self, exchanges: Dict[str, Any], logger, config: dict):
        self.exchanges = exchanges
        self.logger = logger
        self.dry_run = config['system'].get('dry_run', False)

    async def execute_atomic(self, opp: Opportunity) -> Tuple[bool, float, float, float]:
        """
        Attempts to execute both legs of the arbitrage simultaneously.
        
        Returns:
            Tuple(Success: bool, Realized_PnL: float, Buy_Fill_Price: float, Sell_Fill_Price: float)
        """
        # 1. DRY RUN CHECK
        if self.dry_run:
            self.logger.info(f"🔵 DRY RUN: Trade Simulated | Est. Profit: ${opp.net_profit_usd:.4f}")
            # U dry run-u pretpostavljamo da smo dobili cenu koju smo hteli
            return True, opp.net_profit_usd, opp.buy_price, opp.sell_price

        self.logger.info(f"⚡ EXECUTION TRIGGERED: {opp.symbol} | Buy {opp.buy_ex} -> Sell {opp.sell_ex} | Amt: {opp.quantity}")

        buy_client = self.exchanges[opp.buy_ex]
        sell_client = self.exchanges[opp.sell_ex]

        # 2. FIRE ORDERS ASYNCHRONOUSLY
        # Market orderi za sigurnost izvršenja
        futures = [
            buy_client.create_order(opp.symbol, 'market', 'buy', opp.quantity),
            sell_client.create_order(opp.symbol, 'market', 'sell', opp.quantity)
        ]

        # return_exceptions=True da ne bi srušili ceo bot ako jedna berza pukne
        results = await asyncio.gather(*futures, return_exceptions=True)
        buy_res, sell_res = results[0], results[1]

        # Check for Exceptions
        buy_filled = not isinstance(buy_res, Exception)
        sell_filled = not isinstance(sell_res, Exception)

        # 3. OUTCOME ANALYSIS
        if buy_filled and sell_filled:
            # BEST CASE: Both executed perfectly
            
            # --- PYLANCE FIX & SAFETY CHECK ---
            # Eksplicitno proveravamo da li su rezultati rečnici (dict) pre nego što zovemo .get()
            # Ovo rešava "Cannot access attribute get" grešku.
            if isinstance(buy_res, dict) and isinstance(sell_res, dict):
                real_buy_price = buy_res.get('average') or buy_res.get('price') or opp.buy_price
                real_sell_price = sell_res.get('average') or sell_res.get('price') or opp.sell_price
            else:
                # Fallback ako CCXT vrati nešto čudno što nije dict, a nije ni Exception
                real_buy_price = opp.buy_price
                real_sell_price = opp.sell_price
            
            # Ako je average 0 (može se desiti na testnetu nekad), vrati se na planiranu
            if real_buy_price <= 0: real_buy_price = opp.buy_price
            if real_sell_price <= 0: real_sell_price = opp.sell_price

            self.logger.info(f"✅ SUCCESS: Atomic Fill. Buy: ${real_buy_price:.4f} | Sell: ${real_sell_price:.4f}")
            
            # Računamo STVARNI PnL
            realized_pnl = (real_sell_price - real_buy_price) * opp.quantity
            
            return True, realized_pnl, real_buy_price, real_sell_price
        
        elif not buy_filled and not sell_filled:
            # SAFE FAIL: Oba su pala.
            self.logger.warning(f"⚠️ FAILED: Both legs rejected. BuyErr: {buy_res} | SellErr: {sell_res}")
            return False, 0.0, 0.0, 0.0
        
        else:
            # WORST CASE: ORPHAN DETECTED.
            self.logger.error(f"🚨 ORPHAN DETECTED. Buy: {buy_filled}, Sell: {sell_filled}")
            pnl_impact = await self._neutralize_orphan(buy_filled, sell_filled, opp)
            return False, pnl_impact, 0.0, 0.0

    async def _neutralize_orphan(self, buy_ok: bool, sell_ok: bool, opp: Opportunity) -> float:
        """
        Panic logic to unwind a stuck position.
        Prioritizes exiting the market over profit.
        """
        unwind_pnl = 0.0

        if buy_ok:
            self.logger.warning(f"Orphan Type: LONG {opp.buy_ex}. DUMPING...")
            try:
                ex = self.exchanges[opp.buy_ex]
                await ex.create_order(opp.symbol, 'market', 'sell', opp.quantity)
                self.logger.info(f"🏳️ NEUTRALIZED: Position closed on {opp.buy_ex}.")
                unwind_pnl = -(opp.quantity * opp.buy_price * 0.05) # Pretpostavljamo 5% gubitak (spread + fee + panic slippage)
            except Exception as e:
                self.logger.critical(f"💀 CATASTROPHIC FAILURE: Could not neutralize LONG position: {e}")
        
        elif sell_ok:
            self.logger.warning(f"Orphan Type: SHORT {opp.sell_ex}. BUYING BACK...")
            try:
                ex = self.exchanges[opp.sell_ex]
                await ex.create_order(opp.symbol, 'market', 'buy', opp.quantity)
                self.logger.info(f"🏳️ NEUTRALIZED: Position closed on {opp.sell_ex}.")
                unwind_pnl = -(opp.quantity * opp.sell_price * 0.05)
            except Exception as e:
                self.logger.critical(f"💀 CATASTROPHIC FAILURE: Could not neutralize SHORT position: {e}")

        return unwind_pnl
    
# # src/execution.py
# import asyncio
# from typing import Tuple, Dict, Any
# from .models import Opportunity

# class ExecutionService:
#     """
#     Handles the high-stakes logic of placing orders.
#     Enforces 'Atomic' execution (All-or-Nothing attempts) and handles
#     partial fill failures (Orphans) to preserve capital.
#     """
#     def __init__(self, exchanges: Dict[str, Any], logger, config: dict):
#         self.exchanges = exchanges
#         self.logger = logger
        
#         # --- FIX 1: Safe Config Retrieval ---
#         # Defaults to False (Live Trading) if 'dry_run' is missing from config
#         self.dry_run = config['system'].get('dry_run', False)
        
#         # --- FIX 2: Multi-Asset Support ---
#         # We REMOVED self.symbol because the bot now trades multiple coins.
#         # The symbol is passed dynamically in the 'execute_atomic' method via the 'opp' object.

#     async def execute_atomic(self, opp: Opportunity) -> Tuple[bool, float]:
#         """
#         Attempts to execute both legs of the arbitrage simultaneously.
        
#         Returns:
#             Tuple(Success: bool, Realized_PnL: float)
#         """
#         # 1. DRY RUN CHECK
#         if self.dry_run:
#             self.logger.info(f"🔵 DRY RUN: Trade Simulated | Est. Profit: ${opp.net_profit_usd:.4f}")
#             # In dry run, we assume perfect execution for stats
#             return True, opp.net_profit_usd

#         self.logger.info(f"⚡ EXECUTION TRIGGERED: {opp.symbol} | Buy {opp.buy_ex} -> Sell {opp.sell_ex} | Amt: {opp.quantity}")

#         buy_client = self.exchanges[opp.buy_ex]
#         sell_client = self.exchanges[opp.sell_ex]

#         # 2. FIRE ORDERS ASYNCHRONOUSLY
#         # We use asyncio.gather to ensure network requests go out at the exact same millisecond.
#         # 'market' orders are used for certainty of execution (Taker Strategy).
#         futures = [
#             buy_client.create_order(opp.symbol, 'market', 'buy', opp.quantity),
#             sell_client.create_order(opp.symbol, 'market', 'sell', opp.quantity)
#         ]

#         # return_exceptions=True prevents one failure from crashing the code
#         results = await asyncio.gather(*futures, return_exceptions=True)
#         buy_res, sell_res = results[0], results[1]

#         # Check for Exceptions (Network errors, Insufficient Funds, API Errors)
#         buy_filled = not isinstance(buy_res, Exception)
#         sell_filled = not isinstance(sell_res, Exception)

#         # 3. OUTCOME ANALYSIS
#         if buy_filled and sell_filled:
#             # BEST CASE: Both executed perfectly
#             self.logger.info(f"✅ SUCCESS: Atomic Fill. BuyID: {buy_res['id']} | SellID: {sell_res['id']}")
#             # In a live env, we would parse the fill price here to get exact PnL. 
#             # For speed, we return the estimated PnL.
#             return True, opp.net_profit_usd
        
#         elif not buy_filled and not sell_filled:
#             # SAFE FAIL: Both failed. No money lost (except maybe time).
#             self.logger.warning(f"⚠️ FAILED: Both legs rejected. No exposure. BuyErr: {buy_res} | SellErr: {sell_res}")
#             return False, 0.0
        
#         else:
#             # WORST CASE: ORPHAN DETECTED (One filled, one failed).
#             # We must immediately neutralize to prevent holding a bag.
#             pnl_impact = await self._neutralize_orphan(buy_filled, sell_filled, opp)
#             return False, pnl_impact

#     async def _neutralize_orphan(self, buy_ok: bool, sell_ok: bool, opp: Opportunity) -> float:
#         """
#         Panic logic to unwind a stuck position.
#         Prioritizes exiting the market over profit.
#         """
#         self.logger.error("🚨 CRITICAL: ORPHAN TRADE DETECTED. INITIATING NEUTRALIZATION.")
        
#         unwind_pnl = 0.0

#         if buy_ok:
#             # Scenario: We bought the coin, but failed to sell it.
#             # Risk: Price drops while we hold it.
#             # Action: Sell it back immediately on the same exchange (usually fastest).
#             self.logger.warning(f"Orphan Type: LONG {opp.buy_ex} (Buy Filled, Sell Failed). Action: DUMP.")
#             try:
#                 ex = self.exchanges[opp.buy_ex]
#                 # Sell back the exact quantity we just bought
#                 await ex.create_order(opp.symbol, 'market', 'sell', opp.quantity)
                
#                 self.logger.info(f"🏳️ NEUTRALIZED: Position closed on {opp.buy_ex}.")
#                 # We assume we lost the spread + 2x fees.
#                 unwind_pnl = -(opp.quantity * opp.buy_price * 0.02) 
                
#             except Exception as e:
#                 # If this fails, we are in serious trouble (Internet down? Exchange down?).
#                 self.logger.critical(f"💀 CATASTROPHIC FAILURE: Could not neutralize LONG position: {e}")
#                 # Real Hedge Funds would trigger an SMS alert to a human here.
        
#         elif sell_ok:
#             # Scenario: We sold the coin (Short), but failed to buy it back.
#             # Risk: Price goes up, we owe the exchange the coin.
#             # Action: Buy it back immediately.
#             self.logger.warning(f"Orphan Type: SHORT {opp.sell_ex} (Sell Filled, Buy Failed). Action: COVER.")
#             try:
#                 ex = self.exchanges[opp.sell_ex]
#                 # Buy back the exact quantity
#                 await ex.create_order(opp.symbol, 'market', 'buy', opp.quantity)
                
#                 self.logger.info(f"🏳️ NEUTRALIZED: Position closed on {opp.sell_ex}.")
#                 unwind_pnl = -(opp.quantity * opp.sell_price * 0.02)
                
#             except Exception as e:
#                 self.logger.critical(f"💀 CATASTROPHIC FAILURE: Could not neutralize SHORT position: {e}")

#         return unwind_pnl