from __future__ import annotations
from abc import ABC, abstractmethod
from datetime import date
from typing import Any, Dict, List, Optional

class BaseProvider(ABC):
    @abstractmethod
    async def get_items(self, day_from: date, day_to: date, top_k: Optional[int] = None) -> List[Dict[str, Any]]:
        raise NotImplementedError
