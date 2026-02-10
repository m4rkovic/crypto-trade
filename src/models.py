# src/models.py
from dataclasses import dataclass, field
from enum import Enum
import time

class TradeStatus(Enum):
    PENDING = "PENDING"
    FILLED = "FILLED"
    FAILED = "FAILED"
    ORPHANED = "ORPHANED"
    NEUTRALIZED = "NEUTRALIZED"

@dataclass(slots=True)
class TickerData:
    """
    Sadrži podatke o ceni.
    'received_at' je vreme kad je stiglo u naš sistem (za merenje internog laga).
    """
    exchange: str
    symbol: str
    bid_price: float
    bid_vol: float
    ask_price: float
    ask_vol: float
    timestamp: float 
    received_at: float = field(default_factory=time.time)

    @property
    def age(self) -> float:
        """Koliko je star podatak u odnosu na trenutno vreme."""
        return time.time() - self.timestamp

@dataclass(slots=True)
class Opportunity:
    """
    Signal za egzekuciju.
    """
    id: str
    symbol: str
    buy_ex: str
    sell_ex: str
    buy_price: float
    sell_price: float
    quantity: float
    gross_spread_bps: float
    est_profit_usd: float
    timestamp: float