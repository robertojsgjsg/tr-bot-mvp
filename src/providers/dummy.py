from __future__ import annotations
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from .base import BaseProvider

class DummyProvider(BaseProvider):
    async def get_items(self, day_from: date, day_to: date, top_k: Optional[int] = None) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        d = day_from
        while d <= day_to:
            for idx in range(1, 6):
                items.append({
                    "date": d.strftime("%Y-%m-%d"),
                    "league": "DUMMY",
                    "name": f"Producto/Evento {idx}",
                    "market": "Tipo",
                    "selection": f"Opción {idx}",
                    "price": f"{1.00 + idx/10:.2f}",
                    "source": "dummy",
                    "value": (idx - 3) * 0.01,
                })
            d += timedelta(days=1)
        if top_k and top_k > 0:
            items = items[:top_k]
        return items
