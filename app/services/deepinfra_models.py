"""Catálogo de modelos da DeepInfra usado pelo seletor e pela validação das configurações.

O catálogo é consultado direto em GET /v1/models com a API key já configurada e mantido em
um cache curto, apenas em memória: nada disso é gravado no banco. Nenhuma mensagem de erro
inclui a chave.
"""
from __future__ import annotations

import hashlib
import threading
import time

import httpx

DEEPINFRA_MODELS_URL = "https://api.deepinfra.com/v1/models"
CACHE_TTL_SECONDS = 120.0
REQUEST_TIMEOUT_SECONDS = 15.0

_cache_lock = threading.Lock()
_cache: dict[str, tuple[float, list[str]]] = {}


class CatalogUnavailable(RuntimeError):
    """Não foi possível obter o catálogo; a mensagem é segura para exibir ao usuário."""


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _cache_key(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def fetch_catalog(token: str | None) -> list[str]:
    """Retorna os IDs de modelos existentes na DeepInfra (ordenados), com cache curto."""
    token = (token or "").strip()
    if not token:
        raise CatalogUnavailable("Salve a API key da DeepInfra para consultar os modelos disponíveis.")
    key = _cache_key(token)
    now = time.monotonic()
    with _cache_lock:
        cached = _cache.get(key)
        if cached and now - cached[0] < CACHE_TTL_SECONDS:
            return list(cached[1])
    try:
        response = httpx.get(DEEPINFRA_MODELS_URL, headers={"Authorization": f"Bearer {token}"},
                             timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
        payload = response.json()
        models = sorted({str(item["id"]) for item in payload.get("data", []) if item.get("id")})
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if status in (401, 403):
            raise CatalogUnavailable("A DeepInfra recusou a API key configurada (HTTP %d)." % status) from None
        raise CatalogUnavailable("A DeepInfra respondeu com erro HTTP %d ao listar os modelos." % status) from None
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
        raise CatalogUnavailable("Não foi possível consultar os modelos na DeepInfra agora. Tente novamente.") from None
    if not models:
        raise CatalogUnavailable("A DeepInfra devolveu uma lista de modelos vazia.")
    with _cache_lock:
        _cache[key] = (now, models)
    return list(models)


def missing_models(token: str | None, wanted: list[str]) -> list[str]:
    """Dentre os modelos pedidos, devolve os que não existem na DeepInfra (levanta CatalogUnavailable se a consulta falhar)."""
    if not wanted:
        return []
    catalog = set(fetch_catalog(token))
    return [model for model in wanted if model not in catalog]