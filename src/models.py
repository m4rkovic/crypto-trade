# src/models.py
from dataclasses import dataclass, field
from enum import Enum
import time

class TradeStatus(Enum):
    """
    Enum representing the lifecycle states of a trade.
    """
    PENDING = "PENDING"
    FILLED = "FILLED"
    FAILED = "FAILED"
    ORPHANED = "ORPHANED"
    NEUTRALIZED = "NEUTRALIZED"

@dataclass(slots=True)
class TickerData:
    """
    Immutable data structure for market snapshots.
    Using __slots__ for memory efficiency in high-frequency loops.
    """
    exchange: str
    symbol: str
    bid_price: float
    bid_vol: float
    ask_price: float
    ask_vol: float
    timestamp: float 

    @property
    def age(self) -> float:
        """Returns the age of the data in seconds."""
        return time.time() - self.timestamp

@dataclass(slots=True)
class Opportunity:
    """
    Represents a qualified arbitrage signal passed from Strategy to Execution.
    """
    id: str
    symbol: str  # <--- NEW FIELD: Tracks which coin (e.g., SOL/USDT)
    buy_ex: str
    sell_ex: str
    buy_price: float
    sell_price: float
    quantity: float
    gross_spread_bps: float
    net_profit_usd: float
    timestamp: float