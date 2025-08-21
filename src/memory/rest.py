from __future__ import annotations
import httpx

def make_fingerprint(namespace: str, user_id: str, payload: str) -> str:
    raw = f"{namespace}:{user_id}:{payload}".encode("utf-8")
    import hashlib as _h
    return _h.sha256(raw).hexdigest()

class RestMemory:
    def __init__(self, base_url: str, token: str, client: httpx.AsyncClient | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._client = client
    @property
    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}
    async def exists(self, key: str) -> bool:
        client = self._client or httpx.AsyncClient()
        close = self._client is None
        try:
            r = await client.post(f"{self.base_url}/exists/{key}", headers=self.headers)
            r.raise_for_status()
            return bool(r.json().get("result"))
        finally:
            if close:
                await client.aclose()
    async def setex(self, key: str, ttl_seconds: int, value: str) -> bool:
        client = self._client or httpx.AsyncClient()
        close = self._client is None
        try:
            r = await client.post(f"{self.base_url}/setex/{key}/{ttl_seconds}/{value}", headers=self.headers)
            r.raise_for_status()
            return r.json().get("result") == "OK"
        finally:
            if close:
                await client.aclose()
