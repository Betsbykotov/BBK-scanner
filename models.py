from dataclasses import dataclass, field
import time


@dataclass
class OddsUpdate:
    match_id: str
    match_name: str
    sport: str
    bookmaker: str
    market: str
    selection: str
    odds: float
    timestamp: float = field(default_factory=time.time)
