# src/risk_engine.py
from .models import TickerData, Opportunity
import logging
import time

class RiskEngine:
    """
    Enforces risk limits, validates market data freshness, and acts as a circuit breaker.
    Also tracks trade statistics for the UI.
    """
    def __init__(self, config: dict, logger: logging.Logger):
        self.cfg = config['risk_compliance']
        self.logger = logger
        self.daily_pnl = 0.0
        self.consecutive_fails = 0
        self.kill_switch = False
        
        # --- UI STATS ---
        self.total_attempts = 0
        self.success_count = 0
        self.fail_count = 0
        self.last_trade_info = "No trades yet" # Tekst za UI
        
        # --- NOVO: SCANNER HEARTBEAT ---
        self.checks_count = 0 # Brojimo koliko smo puta proverili market

    def increment_check_count(self):
        """Poziva se svaki put kad strategy proveri market, da bi UI bio ziv."""
        self.checks_count += 1

    def validate_market_data(self, data: TickerData) -> bool:
        """
        Filter out stale or anomalous market data.
        In HFT, old data is 'toxic' and leads to bad fills.
        """
        # 1. Latency Check
        if data.age > self.cfg['max_data_age_seconds']:
            # We don't log every single stale tick to avoid spamming logs, 
            # but in debug mode, you might want to see this.
            return False
            
        # 2. Anomaly Check (Zero or Negative Prices)
        if data.bid_price <= 0 or data.ask_price <= 0:
            return False
            
        return True

    def pre_trade_check(self, opp: Opportunity) -> bool:
        """
        The Final Gatekeeper: Can we execute this specific trade opportunity?
        """
        if self.kill_switch:
            # System is locked down due to previous failures or drawdown
            return False

        # 1. Profitability Check
        if opp.gross_spread_bps < self.cfg['min_spread_bps']:
            return False 

        # 2. Daily Drawdown Limit
        if self.daily_pnl < -self.cfg['max_daily_drawdown_usd']:
            self.logger.critical(f"⛔ REJECTED: Max Daily Drawdown Hit (${self.daily_pnl:.2f})")
            self.kill_switch = True
            return False
        
        # 3. Max Exposure Check (Per Trade)
        # Note: 'quantity' in Opportunity is in base asset (e.g. SOL).
        # We approximate USD value: quantity * price
        trade_val_usd = opp.quantity * opp.buy_price
        if trade_val_usd > self.cfg['max_exposure_per_trade_usd']:
            # self.logger.warning(f"⛔ REJECTED: Size too big")
            return False

        return True

    def update_last_trade_status(self, msg: str):
        """Update the status message shown in the UI immediately."""
        self.last_trade_info = f"[{time.strftime('%H:%M:%S')}] {msg}"

    def record_execution_result(self, success: bool, pnl_impact: float = 0.0):
        """
        Updates the internal state based on the result of an attempted trade.
        """
        self.daily_pnl += pnl_impact
        self.total_attempts += 1
        
        if success:
            self.success_count += 1
            self.consecutive_fails = 0
            self.update_last_trade_status(f"[bold green]SUCCESS[/bold green] | PnL: ${pnl_impact:.4f}")
        else:
            self.fail_count += 1
            self.consecutive_fails += 1
            self.update_last_trade_status(f"[bold red]FAILED[/bold red] | PnL Impact: ${pnl_impact:.4f}")

            if self.consecutive_fails >= self.cfg['max_consecutive_failures']:
                self.logger.critical(f"⛔ KILL SWITCH ACTIVATED: {self.consecutive_fails} consecutive execution failures.")
                self.kill_switch = True