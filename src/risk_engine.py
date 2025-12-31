# src/risk_engine.py
from .models import TickerData, Opportunity
import logging

class RiskEngine:
    """
    Enforces risk limits, validates market data freshness, and acts as a circuit breaker.
    Separates the decision 'Can we trade?' from the logic of finding the trade.
    """
    def __init__(self, config: dict, logger: logging.Logger):
        self.cfg = config['risk_compliance']
        self.logger = logger
        self.daily_pnl = 0.0
        self.consecutive_fails = 0
        self.kill_switch = False

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
            self.logger.warning(f"⛔ REJECTED: Trade size ${trade_val_usd:.2f} exceeds limit ${self.cfg['max_exposure_per_trade_usd']}")
            return False

        return True

    def record_execution_result(self, success: bool, pnl_impact: float = 0.0):
        """
        Updates the internal state based on the result of an attempted trade.
        """
        self.daily_pnl += pnl_impact
        
        if success:
            self.consecutive_fails = 0
        else:
            self.consecutive_fails += 1
            if self.consecutive_fails >= self.cfg['max_consecutive_failures']:
                self.logger.critical(f"⛔ KILL SWITCH ACTIVATED: {self.consecutive_fails} consecutive execution failures.")
                self.kill_switch = True