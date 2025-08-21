from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, date
from typing import Any, Dict, List, Optional, Tuple

import httpx

from .base import BaseProvider

FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "").strip()
BENCHMARK = os.getenv("BENCHMARK", "SPY").strip()
_UNIVERSE_ENV = [s.strip() for s in os.getenv("UNIVERSE_TICKERS", "").split(",") if s.strip()]
DEFAULT_UNIVERSE = _UNIVERSE_ENV or [
    "AAPL","MSFT","NVDA","AMZN","GOOGL","META","TSLA","JPM",
    "SPY","QQQ","IWM","VTI","VOO","EFA","EEM",
    "ASML","SAP","SHEL","OR","NVO"
]

HEADERS = {"X-Finnhub-Token": FINNHUB_API_KEY} if FINNHUB_API_KEY else {}

def _sma(vals: List[float], n: int) -> Optional[float]:
    if len(vals) < n:
        return None
    return sum(vals[-n:]) / n

def _ret(values: List[float], k: int) -> Optional[float]:
    if len(values) <= k or values[-k-1] == 0:
        return None
    return (values[-1] / values[-k-1]) - 1.0

def _atr14(high: List[float], low: List[float], close: List[float]) -> Optional[float]:
    n = len(close)
    if n < 15:
        return None
    trs: List[float] = []
    for i in range(1, n):
        h, l, cprev = high[i], low[i], close[i-1]
        tr = max(h - l, abs(h - cprev), abs(l - cprev))
        trs.append(tr)
    if len(trs) < 14:
        return None
    return sum(trs[-14:]) / 14.0

async def _fh_json(client: httpx.AsyncClient, url: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        r = await client.get(url, params=params, headers=HEADERS, timeout=20.0)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None

async def _candles(client: httpx.AsyncClient, symbol: str, days: int = 400) -> Optional[Dict[str, Any]]:
    end = int(datetime.utcnow().timestamp())
    start = end - days * 86400
    js = await _fh_json(client, "https://finnhub.io/api/v1/stock/candle",
                        {"symbol": symbol, "resolution": "D", "from": start, "to": end})
    if not js or js.get("s") != "ok":
        return None
    return js  # keys: c,h,l,o,t,v

async def _quote(client: httpx.AsyncClient, symbol: str) -> Optional[Dict[str, Any]]:
    return await _fh_json(client, "https://finnhub.io/api/v1/quote", {"symbol": symbol})

async def _search_symbol(client: httpx.AsyncClient, query: str) -> Optional[Tuple[str, str]]:
    """Returns (symbol, description). Accepts ticker or ISIN in query."""
    js = await _fh_json(client, "https://finnhub.io/api/v1/search", {"q": query})
    if not js or not js.get("result"):
        return None
    # Prefer exact symbol match; else first result
    for it in js["result"]:
        if it.get("symbol", "").upper() == query.upper():
            return it["symbol"], (it.get("description") or it["symbol"])
    it0 = js["result"][0]
    return it0.get("symbol"), (it0.get("description") or it0.get("symbol") or query)

@dataclass
class EvalResult:
    symbol: str
    name: str
    price: float
    score: int
    confianza: str
    riesgo_cat: str
    horizonte: str
    decision: str  # COMPRAR / MANTENER / VENDER / EVITAR
    razon: str

class TradeRepublicProvider(BaseProvider):
    """Proveedor basado en Finnhub con señales S1/S2/S3."""

    async def get_items(self, day_from: date, day_to: date, top_k: Optional[int] = None) -> List[Dict[str, Any]]:
        # No usamos este método en este proveedor para el MVP.
        return []

    # ---------- Señales ----------
    def _signals(
        self,
        close: List[float],
        ma20: Optional[float],
        ma50: Optional[float],
        ma200: Optional[float],
        be
