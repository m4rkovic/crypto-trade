# src/strategy.py
import time
import asyncio
from typing import Dict, Optional, Set
from .models import TickerData, Opportunity
from .risk_engine import RiskEngine
from .inventory import InventoryEngine
from .execution import ExecutionService

class StrategyEngine:
    """
    Event-Driven Strategy: "The Reality Check" Edition.
    Fokus na brutalno realnu kalkulaciju profita i eliminaciju Testnet anomalija.
    Zadržava sve Testnet bypass-ove i detaljne logove.
    """
    def __init__(self, config, risk: RiskEngine, inventory: InventoryEngine, execution: ExecutionService, logger, audit_logger):
        self.config = config
        self.risk = risk
        self.inventory = inventory
        self.execution = execution
        self.logger = logger
        self.audit_logger = audit_logger
        
        self.market_cache: Dict[str, Dict[str, TickerData]] = {}
        self.target_size_usd = config['target']['sizing_amount']
        
        # --- ZAŠTITA OD SPAMOVANJA & COOLDOWN ---
        self.active_trades: Set[str] = set()
        self.last_trade_time: Dict[str, float] = {}
        # Cooldown period (2s je minimum da bi market 'udisao')
        self.cooldown_seconds = config['performance'].get('cooldown_seconds', 2.0)
        
        # --- SIMULACIJA REALNOSTI ---
        self.simulated_slippage_bps = 5.0  # 0.05% slippage po strani
        self.sanity_check_max_spread = 1000.0 # 10% - Sve preko ovoga je verovatno glitch

    async def on_ticker_update(self, ticker: TickerData):
        if ticker.symbol not in self.market_cache:
            self.market_cache[ticker.symbol] = {}
        self.market_cache[ticker.symbol][ticker.exchange] = ticker
        await self.check_arbitrage(ticker.symbol)

    async def check_arbitrage(self, symbol: str):
        self.risk.increment_check_count()

        if symbol in self.active_trades: 
            return

        now = time.time()
        if now - self.last_trade_time.get(symbol, 0) < self.cooldown_seconds:
            return

        exchanges_data = self.market_cache.get(symbol, {})
        if len(exchanges_data) < 2: return

        keys = list(exchanges_data.keys())
        for i in range(len(keys)):
            for j in range(len(keys)):
                if i == j: continue
                
                ex_a_name = keys[i]
                ex_b_name = keys[j]
                
                tick_a = exchanges_data[ex_a_name] # Buy side
                tick_b = exchanges_data[ex_b_name] # Sell side
                
                if tick_a.ask_price <= 0 or tick_b.bid_price <= 0: continue

                buy_price_theory = tick_a.ask_price
                sell_price_theory = tick_b.bid_price
                if buy_price_theory >= sell_price_theory: continue 

                # 1. BRUTAL SPREAD CALCULATION
                gross_spread_bps = ((sell_price_theory - buy_price_theory) / buy_price_theory) * 10000
                
                if gross_spread_bps > self.sanity_check_max_spread:
                    continue

                if gross_spread_bps > self.config['risk_compliance']['min_spread_bps']:
                    
                    # -----------------------------------------------------------
                    # 1. LATENCY CHECK (Modifikovan za Testnet realnost)
                    # -----------------------------------------------------------
                    # PAŽNJA ZA LIVE MODE:
                    # Na Testnetu ignorišemo starost podataka jer nema likvidnosti.
                    # KADA PREBACIS NA 'LIVE' (PRAVI NOVAC): 'environment: live' u configu.
                    # -----------------------------------------------------------
                    is_testnet = self.config['system'].get('environment') == 'testnet'
                    data_valid = self.risk.validate_market_data(tick_a) and self.risk.validate_market_data(tick_b)

                    if not data_valid:
                        age_a = now - tick_a.timestamp
                        age_b = now - tick_b.timestamp
                        if not is_testnet:
                            self.risk.update_last_trade_status(f"[yellow]Stale {symbol}: ({age_a:.2f}s / {age_b:.2f}s) SKIPPED[/yellow]")
                            continue

                    # -----------------------------------------------------------
                    # 2. SMART SIZING & WALLET CHECK
                    # -----------------------------------------------------------
                    target_qty = self.target_size_usd / buy_price_theory
                    market_vol = min(tick_a.ask_vol, tick_b.bid_vol)
                    if is_testnet and market_vol <= 0: market_vol = target_qty 
                    
                    balance_usdt = self.inventory.get_available_balance(ex_a_name, 'USDT')
                    max_buy_qty = (balance_usdt * 0.99) / buy_price_theory 
                    
                    base_coin = symbol.split('/')[0]
                    balance_coin = self.inventory.get_available_balance(ex_b_name, base_coin)
                    max_sell_qty = balance_coin 
                    
                    qty = min(target_qty, market_vol, max_buy_qty, max_sell_qty)
                    
                    if (qty * buy_price_theory) < 10.0: continue

                    # --- 3. THEORETICAL NET PROFIT (Fees + Simulated Slippage) ---
                    gross_profit = (sell_price_theory - buy_price_theory) * qty
                    fee_rate_a = self.config['exchanges'][ex_a_name].get('fee_rate', 0.001)
                    fee_rate_b = self.config['exchanges'][ex_b_name].get('fee_rate', 0.001)
                    
                    total_fees_est = (qty * buy_price_theory * fee_rate_a) + (qty * sell_price_theory * fee_rate_b)
                    slippage_cost = (qty * buy_price_theory) * (self.simulated_slippage_bps / 10000) * 2 
                    
                    net_profit_est = gross_profit - total_fees_est - slippage_cost

                    if net_profit_est <= 0:
                        self.risk.update_last_trade_status(f"[dim]Borderline {symbol}: Net ${net_profit_est:.4f}[/dim]")
                        continue

                    opp = Opportunity(
                        id=f"{symbol}-{int(now*1000)}",
                        symbol=symbol,
                        buy_ex=ex_a_name,
                        sell_ex=ex_b_name,
                        buy_price=buy_price_theory,
                        sell_price=sell_price_theory,
                        quantity=qty,
                        gross_spread_bps=gross_spread_bps,
                        net_profit_usd=net_profit_est,
                        timestamp=now
                    )

                    if self.risk.pre_trade_check(opp):
                        self.active_trades.add(symbol)
                        try:
                            self.logger.info(f"✨ SIGNAL: {symbol} | Gross: {gross_spread_bps:.1f}bps | Est. Net: ${net_profit_est:.4f}")
                            await self.execute_opportunity(opp, buy_price_theory, sell_price_theory)
                            self.last_trade_time[symbol] = time.time()
                        finally:
                            self.active_trades.remove(symbol)
                        return

    async def execute_opportunity(self, opp: Opportunity, theory_buy: float, theory_sell: float):
        msg = f"ATTEMPT: Buy {opp.symbol} on {opp.buy_ex.upper()} -> Sell on {opp.sell_ex.upper()}"
        self.risk.update_last_trade_status(msg)

        base_coin = opp.symbol.split('/')[0]
        quote_coin = 'USDT'
        
        cost_usdt = opp.quantity * opp.buy_price
        cost_coin = opp.quantity
        
        has_usdt = self.inventory.reserve_liquidity(opp.buy_ex, quote_coin, cost_usdt)
        has_coin = self.inventory.reserve_liquidity(opp.sell_ex, base_coin, cost_coin)
        
        if has_usdt and has_coin:
            # --- START EXECUTION METRICS ---
            exec_start = time.time()
            success, gross_pnl, real_buy, real_sell = await self.execution.execute_atomic(opp)
            exec_time_ms = int((time.time() - exec_start) * 1000)
            
            if success:
                # --- FINAL ACCOUNTING AFTER FILL ---
                fee_rate_a = self.config['exchanges'][opp.buy_ex].get('fee_rate', 0.001)
                fee_rate_b = self.config['exchanges'][opp.sell_ex].get('fee_rate', 0.001)
                
                actual_fees = (opp.quantity * real_buy * fee_rate_a) + (opp.quantity * real_sell * fee_rate_b)
                net_realized_pnl = gross_pnl - actual_fees

                # --- SLIPPAGE CALCULATION ---
                buy_slip = ((real_buy - theory_buy) / theory_buy) * 10000
                sell_slip = ((theory_sell - real_sell) / theory_sell) * 10000
                total_slip_bps = buy_slip + sell_slip

                self.risk.record_execution_result(success, net_realized_pnl)
                
                self.inventory.confirm_trade(opp.buy_ex, opp.symbol, 'buy', opp.quantity, real_buy, fee_rate_a)
                self.inventory.confirm_trade(opp.sell_ex, opp.symbol, 'sell', opp.quantity, real_sell, fee_rate_b)

                # AUDIT LOG: Snimamo istinu o latenciji i slippage-u
                trade_record = [
                    time.strftime('%Y-%m-%d %H:%M:%S'),
                    opp.symbol,
                    f"{opp.buy_ex}->{opp.sell_ex}",
                    f"{opp.quantity:.6f}",
                    f"{real_buy:.4f}",
                    f"{real_sell:.4f}",
                    f"{net_realized_pnl:.4f}",
                    f"{exec_time_ms}ms",
                    f"{total_slip_bps:.1f}bps",
                    "SUCCESS"
                ]
                await self.audit_logger.log_trade(trade_record)
                self.logger.info(f"✅ REALIZED: ${net_realized_pnl:.4f} in {exec_time_ms}ms | Slip: {total_slip_bps:.1f}bps")
                
            else:
                self.risk.record_execution_result(success, gross_pnl)
                self.inventory.rollback_liquidity(opp.buy_ex, quote_coin, cost_usdt)
                self.inventory.rollback_liquidity(opp.sell_ex, base_coin, cost_coin)
        else:
            missing = []
            if not has_usdt: missing.append(f"{opp.buy_ex}: No USDT")
            if not has_coin: missing.append(f"{opp.sell_ex}: No {base_coin}")
            self.risk.update_last_trade_status(f"[red]NO FUNDS: {' | '.join(missing)}[/red]")
            
            if has_usdt: self.inventory.rollback_liquidity(opp.buy_ex, quote_coin, cost_usdt)
            if has_coin: self.inventory.rollback_liquidity(opp.sell_ex, base_coin, cost_coin)
            
# # src/strategy.py
# import time
# import asyncio
# from typing import Dict, Optional, Set
# from .models import TickerData, Opportunity
# from .risk_engine import RiskEngine
# from .inventory import InventoryEngine
# from .execution import ExecutionService

# class StrategyEngine:
#     """
#     Event-Driven Strategy.
#     Sluša 'on_ticker' evente i odmah reaguje.
#     Čuva lokalni keš cena (OrderBook Lite).
#     """
#     def __init__(self, config, risk: RiskEngine, inventory: InventoryEngine, execution: ExecutionService, logger, audit_logger):
#         self.config = config
#         self.risk = risk
#         self.inventory = inventory
#         self.execution = execution
#         self.logger = logger
#         self.audit_logger = audit_logger
        
#         self.market_cache: Dict[str, Dict[str, TickerData]] = {}
#         self.target_size_usd = config['target']['sizing_amount']
#         self.active_trades: Set[str] = set()

#     async def on_ticker_update(self, ticker: TickerData):
#         if ticker.symbol not in self.market_cache:
#             self.market_cache[ticker.symbol] = {}
#         self.market_cache[ticker.symbol][ticker.exchange] = ticker
#         await self.check_arbitrage(ticker.symbol)

#     async def check_arbitrage(self, symbol: str):
#         self.risk.increment_check_count()

#         if symbol in self.active_trades: return

#         exchanges_data = self.market_cache.get(symbol, {})
#         if len(exchanges_data) < 2: return

#         keys = list(exchanges_data.keys())
#         for i in range(len(keys)):
#             for j in range(len(keys)):
#                 if i == j: continue
                
#                 ex_a_name = keys[i]
#                 ex_b_name = keys[j]
                
#                 tick_a = exchanges_data[ex_a_name] # Buy
#                 tick_b = exchanges_data[ex_b_name] # Sell
                
#                 # --- ZERO PRICE PROTECTION ---
#                 if tick_a.ask_price <= 0 or tick_b.bid_price <= 0: continue

#                 # --- CALCULATION PRE-CHECK ---
#                 # Prvo izracunamo spread da vidimo ima li smisla uopste gledati dalje
#                 buy_price = tick_a.ask_price
#                 sell_price = tick_b.bid_price
#                 if buy_price >= sell_price: continue 

#                 spread_bps = ((sell_price - buy_price) / buy_price) * 10000
                
#                 # Ako je spread dobar, tek onda radimo teske provere
#                 if spread_bps > self.config['risk_compliance']['min_spread_bps']:
                    
#                     # -----------------------------------------------------------
#                     # 1. LATENCY CHECK (Modifikovan za Testnet realnost)
#                     # -----------------------------------------------------------
#                     # PAŽNJA ZA LIVE MODE:
#                     # Na Testnetu ignorišemo starost podataka jer nema likvidnosti (podaci kasne 10s+).
#                     #
#                     # KADA PREBACIS NA 'LIVE' (PRAVI NOVAC):
#                     # 1. U 'config.yaml' promeni: environment: "live"
#                     # 2. U 'config.yaml' promeni: max_data_age_seconds: 0.5 (ili 0.2 za VPS)
#                     #
#                     # Ovaj kod ispod automatski postaje strog cim detektuje da nije 'testnet'.
#                     # -----------------------------------------------------------
#                     is_testnet = self.config['system'].get('environment') == 'testnet'
                    
#                     # Proveravamo da li je podatak svez
#                     data_valid = self.risk.validate_market_data(tick_a) and self.risk.validate_market_data(tick_b)

#                     if not data_valid:
#                         age_a = time.time() - tick_a.timestamp
#                         age_b = time.time() - tick_b.timestamp
                        
#                         msg = f"[yellow]Stale {symbol}: ({age_a:.2f}s / {age_b:.2f}s)[/yellow]"
                        
#                         if is_testnet:
#                             # --- TESTNET BYPASS ---
#                             self.risk.update_last_trade_status(msg + " -> ALLOWED (Testnet)")
#                         else:
#                             # --- LIVE MODE PROTECTION ---
#                             self.risk.update_last_trade_status(msg + " -> SKIPPED")
#                             continue

#                     # -----------------------------------------------------------
#                     # 2. LIQUIDITY CHECK (Sa FIX-om za Testnet Volume=0)
#                     # -----------------------------------------------------------
#                     desired_qty = self.target_size_usd / buy_price
                    
#                     # Provera sta berza kaze da ima na stanju
#                     market_vol = min(tick_a.ask_vol, tick_b.bid_vol)
                    
#                     # --- TESTNET VOLUME HACK ---
#                     if is_testnet and market_vol <= 0:
#                         # self.logger.warning(f"⚠️ Testnet Zero Volume detected for {symbol}. Forcing execution.")
#                         available_qty = desired_qty
#                     else:
#                         available_qty = market_vol

#                     qty = min(desired_qty, available_qty)
                    
#                     # 3. MIN NOTIONAL CHECK
#                     trade_val = qty * buy_price
#                     if trade_val < 10.0:
#                         vol_a_usd = tick_a.ask_vol * tick_a.ask_price
#                         vol_b_usd = tick_b.bid_vol * tick_b.bid_price
                        
#                         n1 = ex_a_name[:3].capitalize()
#                         n2 = ex_b_name[:3].capitalize()
                        
#                         self.risk.update_last_trade_status(f"[yellow]Skipped {symbol}: Low Vol ({n1}:${vol_a_usd:.1f} / {n2}:${vol_b_usd:.1f})[/yellow]")
#                         continue

#                     est_profit = (sell_price - buy_price) * qty
                    
#                     # 4. FEES
#                     fee_rate_a = self.config['exchanges'][ex_a_name].get('fee_rate', 0.001)
#                     fee_rate_b = self.config['exchanges'][ex_b_name].get('fee_rate', 0.001)
#                     total_fees = ((qty * buy_price) * fee_rate_a) + ((qty * sell_price) * fee_rate_b)
                    
#                     net_profit = est_profit - total_fees

#                     if net_profit <= 0:
#                         self.risk.update_last_trade_status(f"[yellow]Skipped {symbol}: Fees ate profit[/yellow]")
#                         continue

#                     opp = Opportunity(
#                         id=f"{symbol}-{int(time.time()*1000)}",
#                         symbol=symbol,
#                         buy_ex=ex_a_name,
#                         sell_ex=ex_b_name,
#                         buy_price=buy_price,
#                         sell_price=sell_price,
#                         quantity=qty,
#                         gross_spread_bps=spread_bps,
#                         net_profit_usd=net_profit,
#                         timestamp=time.time()
#                     )

#                     if self.risk.pre_trade_check(opp):
#                         self.active_trades.add(symbol)
#                         try:
#                             self.logger.info(f"✨ FOUND: {symbol} Spread: {spread_bps:.1f}bps | Est. Net: ${net_profit:.3f} | Vol: {qty:.4f}")
#                             await self.execute_opportunity(opp)
#                         finally:
#                             self.active_trades.remove(symbol)
#                         return

#     async def execute_opportunity(self, opp: Opportunity):
#         msg = f"ATTEMPT: Buy {opp.symbol} on {opp.buy_ex.upper()} @ ${opp.buy_price:.4f} -> Sell on {opp.sell_ex.upper()}"
#         self.risk.update_last_trade_status(msg)

#         base_coin = opp.symbol.split('/')[0]
#         quote_coin = 'USDT'
        
#         cost_usdt = opp.quantity * opp.buy_price
#         cost_coin = opp.quantity
        
#         has_usdt = self.inventory.reserve_liquidity(opp.buy_ex, quote_coin, cost_usdt)
#         has_coin = self.inventory.reserve_liquidity(opp.sell_ex, base_coin, cost_coin)
        
#         if has_usdt and has_coin:
#             success, pnl, real_buy_price, real_sell_price = await self.execution.execute_atomic(opp)
            
#             if success:
#                 self.risk.record_execution_result(success, pnl)
                
#                 fee_a = self.config['exchanges'][opp.buy_ex].get('fee_rate', 0.001)
#                 fee_b = self.config['exchanges'][opp.sell_ex].get('fee_rate', 0.001)
                
#                 self.inventory.confirm_trade(opp.buy_ex, opp.symbol, 'buy', opp.quantity, real_buy_price, fee_a)
#                 self.inventory.confirm_trade(opp.sell_ex, opp.symbol, 'sell', opp.quantity, real_sell_price, fee_b)

#                 # AUDIT LOG
#                 trade_record = [
#                     time.strftime('%Y-%m-%d %H:%M:%S'),
#                     opp.symbol,
#                     opp.buy_ex,
#                     opp.sell_ex,
#                     f"{opp.quantity:.6f}",
#                     f"{real_buy_price:.4f}",
#                     f"{real_sell_price:.4f}",
#                     f"{pnl:.4f}",
#                     "SUCCESS"
#                 ]
#                 await self.audit_logger.log_trade(trade_record)
                
#             else:
#                 self.risk.record_execution_result(success, pnl)
#                 self.inventory.rollback_liquidity(opp.buy_ex, quote_coin, cost_usdt)
#                 self.inventory.rollback_liquidity(opp.sell_ex, base_coin, cost_coin)
#         else:
#             # --- DETALJAN ISPIS NEDOSTATKA SREDSTAVA ---
#             missing = []
#             if not has_usdt:
#                 missing.append(f"{opp.buy_ex}: No USDT")
#             if not has_coin:
#                 missing.append(f"{opp.sell_ex}: No {base_coin}")
            
#             missing_str = " | ".join(missing)
#             self.risk.update_last_trade_status(f"[red]NO FUNDS: {missing_str}[/red]")
            
#             if has_usdt: self.inventory.rollback_liquidity(opp.buy_ex, quote_coin, cost_usdt)
#             if has_coin: self.inventory.rollback_liquidity(opp.sell_ex, base_coin, cost_coin)