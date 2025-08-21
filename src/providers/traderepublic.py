from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, date
from typing import Any, Dict, List, Optional, Tuple

import httpx

from .base import BaseProvider

# === Config ===
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "").strip()
BENCHMARK = os.getenv("BENCHMARK", "SPY").strip()

# Universo para /buyideas (puedes sobreescribir con UNIVERSE_TICKERS en ENV)
_UNIVERSE_ENV = [s.strip() for s in os.getenv("UNIVERSE_TICKERS", "").split(",") if s.strip()]
DEFAULT_UNIVERSE = _UNIVERSE_ENV or [
    "AAPL","MSFT","NVDA","AMZN","GOOGL","META","TSLA","AVGO","AMD","NFLX","ADBE","COST","PEP","ORCL",
    "SPY","QQQ","IWM","VTI","VOO","EFA","EEM",
    "ASML","SAP","NVO"
]

HEADERS = {"X-Finnhub-Token": FINNHUB_API_KEY} if FINNHUB_API_KEY else {}

# === Utils ===
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

async def _fh_json(client: httpx.AsyncClient, url: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """GET a Finnhub endpoint ensuring token is in query and raising useful errors."""
    if not FINNHUB_API_KEY:
        raise RuntimeError("FINNHUB_API_KEY vacío")
    q = dict(params or {})
    q["token"] = FINNHUB_API_KEY
    try:
        r = await client.get(url, params=q, headers=HEADERS, timeout=20.0)
        r.raise_for_status()
        return r.json()
    except httpx.HTTPStatusError as e:
        # Propaga info útil (401/429/etc.)
        body = e.response.text[:200] if e.response is not None else str(e)
        raise RuntimeError(f"finnhub {e.response.status_code}: {body}")
    except Exception as e:
        raise RuntimeError(f"finnhub error: {e}")

async def _candles(client: httpx.AsyncClient, symbol: str, days: int = 420) -> Dict[str, Any]:
    """
    Intenta Finnhub; si no hay acceso/datos, cae a Yahoo Finance.
    """
    try:
        end = int(datetime.utcnow().timestamp())
        start = end - days * 86400
        js = await _fh_json(
            client,
            "https://finnhub.io/api/v1/stock/candle",
            {"symbol": symbol, "resolution": "D", "from": start, "to": end},
        )
        if js.get("s") == "ok":
            return js
        # estados típicos: 'no_data'
        raise RuntimeError(f"candles finnhub estado={js.get('s')}")
    except Exception:
        # Fallback a Yahoo
        return await _candles_yahoo(client, symbol, days=days)

async def _quote(client: httpx.AsyncClient, symbol: str) -> Dict[str, Any]:
    return await _fh_json(client, "https://finnhub.io/api/v1/quote", {"symbol": symbol})

async def _candles_yahoo(client: httpx.AsyncClient, symbol: str, days: int = 420) -> Dict[str, Any]:
    """
    Fallback a Yahoo Finance para velas diarias cuando Finnhub devuelve 403/no_data.
    No requiere API key. Devuelve dict con keys c,h,l (listas) y s="ok".
    """
    # Rango aproximado en función de días solicitados
    if days <= 365:
        rng = "1y"
    elif days <= 730:
        rng = "2y"
    elif days <= 1825:
        rng = "5y"
    else:
        rng = "10y"

    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {"interval": "1d", "range": rng}
    r = await client.get(url, params=params, timeout=20.0)
    r.raise_for_status()
    js = r.json()
    res = (js.get("chart", {}).get("result") or [None])[0]
    if not res:
        raise RuntimeError("yahoo: sin resultado")
    q = (res.get("indicators", {}).get("quote") or [None])[0]
    if not q:
        raise RuntimeError("yahoo: sin 'quote'")
    c_raw = q.get("close") or []
    h_raw = q.get("high") or []
    l_raw = q.get("low") or []
    # Filtra None manteniendo alineación
    c, h, l = [], [], []
    for ci, hi, li in zip(c_raw, h_raw, l_raw):
        if ci is None or hi is None or li is None:
            continue
        c.append(float(ci))
        h.append(float(hi))
        l.append(float(li))
    if len(c) < 30:
        raise RuntimeError("yahoo: serie demasiado corta")
    return {"s": "ok", "c": c, "h": h, "l": l}

async def _search_symbol(client: httpx.AsyncClient, query: str) -> Optional[Tuple[str, str]]:
    """Devuelve (symbol, description). Acepta ticker o ISIN."""
    js = await _fh_json(client, "https://finnhub.io/api/v1/search", {"q": query})
    res = js.get("result") or []
    if not res:
        return None
    # Prioriza coincidencia exacta de symbol
    for it in res:
        sym = (it.get("symbol") or "").upper()
        if sym == query.upper():
            return it["symbol"], (it.get("description") or it["symbol"])
    it0 = res[0]
    return it0.get("symbol"), (it0.get("description") or it0.get("symbol") or query)

# === Modelos de salida ===
@dataclass
class EvalResult:
    symbol: str
    name: str
    price: float
    score: int
    confianza: str
    riesgo_cat: str
    horizonte: str
    decision: str   # COMPRAR / MANTENER / VENDER / EVITAR
    razon: str

# === Proveedor principal ===
class TradeRepublicProvider(BaseProvider):
    """Proveedor (basado en Finnhub) con señales S1/S2/S3."""

    async def get_items(self, day_from: date, day_to: date, top_k: Optional[int] = None) -> List[Dict[str, Any]]:
        # No se usa en este MVP (se usa /buyideas y /check)
        return []

    # -------- Señales ----------
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
        # Pendiente MA50 positiva (aprox)
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

    def _risk_and_score(
        self,
        high: List[float],
        low: List[float],
        close: List[float],
        sig: Dict[str, Any],
    ) -> Tuple[str, int, str, str]:
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

        # Buscar símbolo (acepta ticker o ISIN)
        sym_desc = await _search_symbol(client, query)
        if not sym_desc:
            raise RuntimeError("search: sin resultados para el ticker/ISIN")
        symbol, name = sym_desc

        # Velas del símbolo
        cd = await _candles(client, symbol, days=420)
        c, h, l = cd["c"], cd["h"], cd["l"]
        if not c or len(c) < 30:
            raise RuntimeError(f"candles: serie demasiado corta len={len(c) if c else 0}")

        ma20 = _sma(c, 20)
        ma50 = _sma(c, 50)
        ma200 = _sma(c, 200)
        sym_ret63 = _ret(c, 63)

        # Velas del benchmark (para fuerza relativa)
        bench_ret63: Optional[float] = None
        try:
            bench_cd = await _candles(client, BENCHMARK, days=420)
            bench_ret63 = _ret(bench_cd["c"], 63) if bench_cd and bench_cd.get("c") else None
        except Exception:
            bench_ret63 = None  # si falla benchmark, seguimos sin RS

        # Señales y métricas
        sig = self._signals(c, ma20, ma50, ma200, bench_ret63, sym_ret63)
        riesgo, score, confianza, horizonte = self._risk_and_score(h, l, c, sig)

        # Decisión
        if sig["s3"]:
            decision = "VENDER"
        elif sig["s1"] and not sig["s3"]:
            decision = "COMPRAR"
        elif sig["s2"]:
            decision = "MANTENER"
        else:
            decision = "EVITAR"

        # Precio actual
        qt = await _quote(client, symbol)
        price = float(qt.get("c") or c[-1]) if qt else float(c[-1])

        # Razón breve
        razones: List[str] = []
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
                # Ignora símbolos con no_data/límites/etc.
                continue
        out.sort(key=lambda x: x.score, reverse=True)
        return out[:max(1, top_k)]
