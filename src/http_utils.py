from typing import Dict, List
import httpx
from . import config

async def backend_get(path: str, params: dict | None = None):
    url = f"{config.BACKEND_BASE}{path}"
    async with httpx.AsyncClient(timeout=30) as cx:
        return await cx.get(url, params=params)

async def probe_headers(target_url: str, headers: Dict[str, str]):
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as cx:
        return await cx.head(target_url, headers=headers)

def headers_kv(headers: dict) -> List[str]:
    kv: List[str] = []
    for k, v in (headers or {}).items():
        kv += ["-headers", f"{k}: {v}\r\n"]
    return kv
