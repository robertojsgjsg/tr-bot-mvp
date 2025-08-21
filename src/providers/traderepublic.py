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
        bench_ret63: Optional[float],
        sym_ret63: Optional[float],
    ) -> Dict[str, Any]:
        ret1d = _ret(close, 1) or 0.0
        ret5d = _ret(close, 5) or 0.0
        s1 = ((ret1d >= 0.01) or (ret5d >= 0.03)) and (ma20 is not None and close[-1] > 1.005 * ma20)

        s2_parts = 0
        if ma50 is not None and ma200 is not None and close[-1] > ma50 > ma200:
            s2_parts += 1
        # pendiente MA50 positiva (aproximada)
        s2_slope = False
        if len(close) >= 260:
            recent = sum(close[-50:]) / 50.0
            old = sum(close[-60:-10]) / 50.0
            s2_slope = recent > old
        if s2_slope:
            s2_parts += 1
        rs_ok = False
        if bench_ret63 is not None and sym_ret63 is not None:
            rs_ok = (sym_ret63 - bench_ret63) > 0.0
        if rs_ok:
            s2_parts += 1
        s2 = s2_parts >= 2

        # S3: caída y cruce bajo MA20 (ayer >= MA20 y hoy < MA20 con ret1d <= -1%)
        s3 = False
        if ma20 is not None and len(close) >= 21:
            yesterday_above = close[-2] >= ma20
            today_below = close[-1] < ma20 and (ret1d <= -0.01)
            s3 = yesterday_above and today_below

        return {"ret1d": ret1d, "ret5d": ret5d, "s1": s1, "s2": s2, "s2_parts": s2_parts, "s3": s3}

    def _risk_and_score(self, high: List[float], low: List[float], close: List[float], sig: Dict[str, Any]) -> Tuple[str, int, str, str]:
        atr = _atr14(high, low, close)
        price = close[-1]
        riesgo = "Medio"
        penalty = 0.5
        if atr is not None and price:
            vol = atr / price
            if vol < 0.015:
                riesgo, penalty = "Bajo", 0.0
            elif vol > 0.03:
                riesgo, penalty = "Alto", 1.0

        score_raw = (40 if sig["s1"] else 0) + (50 if sig["s2"] else 0) - int(30 * penalty)
        score = max(0, min(100, score_raw))

        if sig["s2"]:
            horizonte = "Largo" if sig.get("s2_parts", 0) >= 3 else "Medio"
        elif sig["s1"]:
            horizonte = "Corto"
        else:
            horizonte = "Observación"

        confianza = "Alta" if sig["s2"] and riesgo != "Alto" else ("Media" if (sig["s1"] or sig["s2"]) else "Baja")
        return riesgo, score, confianza, horizonte

    async def evaluate(self, client: httpx.AsyncClient, query: str) -> Optional[EvalResult]:
        if not FINNHUB_API_KEY:
            raise RuntimeError("Falta FINNHUB_API_KEY")

        sym_desc = await _search_symbol(client, query)
        if not sym_desc:
            return None
        symbol, name = sym_desc

        cd = await _candles(client, symbol, days=420)
        if not cd:
            return None
        c, h, l = cd["c"], cd["h"], cd["l"]
        if not c or len(c) < 60:
            return None

        ma20 = _sma(c, 20)
        ma50 = _sma(c, 50)
        ma200 = _sma(c, 200)
        sym_ret63 = _ret(c, 63)

        bench_cd = await _candles(client, BENCHMARK, days=420)
        bench_ret63 = _ret(bench_cd["c"], 63) if bench_cd and bench_cd.get("c") else None

        sig = self._signals(c, ma20, ma50, ma200, bench_ret63, sym_ret63)
        riesgo, score, confianza, horizonte = self._risk_and_score(h, l, c, sig)

        decision = "EVITAR"
        if sig["s3"]:
            decision = "VENDER"
        elif sig["s1"] and not sig["s3"]:
            decision = "COMPRAR"
        elif sig["s2"]:
            decision = "MANTENER"

        qt = await _quote(client, symbol)
        price = float(qt.get("c") or c[-1]) if qt else float(c[-1])

        razones = []
        if sig["s1"]:
            razones.append("S1 activo (mom. 1D/5D y sobre MA20)")
        if sig["s2"]:
            razones.append("S2 activo (tendencia y fuerza relativa)")
        if sig["s3"]:
            razones.append("S3 activo (caída y cruce bajo MA20)")
        if not razones:
            razones.append("Sin señales fuertes (observación)")

        return EvalResult(
            symbol=symbol,
            name=name,
            price=price,
            score=score,
            confianza=confianza,
            riesgo_cat=riesgo,
            horizonte=horizonte,
            decision=decision,
            razon="; ".join(razones),
        )

    async def buyideas(self, client: httpx.AsyncClient, top_k: int = 5) -> List[EvalResult]:
        if not FINNHUB_API_KEY:
            raise RuntimeError("Falta FINNHUB_API_KEY")
        out: List[EvalResult] = []
        for sym in DEFAULT_UNIVERSE:
            try:
                r = await self.evaluate(client, sym)
                if r and (r.decision in ("COMPRAR", "MANTENER")):
                    out.append(r)
            except Exception:
                continue
        out.sort(key=lambda x: x.score, reverse=True)
        return out[:max(1, top_k)]
