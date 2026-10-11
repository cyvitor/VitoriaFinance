"""Cliente DeepInfra com retry por modelo e fallback entre modelos.

Todas as configuracoes ficam em SystemSetting (tela Configuracoes do sistema),
nunca no .env. Valores ausentes ou invalidos usam os defaults abaixo.
"""
from dataclasses import dataclass, field
import email.utils
import logging
import random
import time
from datetime import datetime, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import SystemSetting

DEEPINFRA_CHAT_URL = "https://api.deepinfra.com/v1/openai/chat/completions"
# Qwen3.6 foi descartado: modelo de raciocinio, 40-55s por resposta na DeepInfra.
# GLM-5.3-Flash retornou 429 (engine_overloaded) no teste de 2026-10-11; reserva fica vazia
# por padrao e pode ser definida na tela Configuracoes do sistema.
DEFAULT_MODEL = "deepseek-ai/DeepSeek-V4-Flash-0731"
DEFAULT_FALLBACK_MODELS: list[str] = []
DEFAULT_RETRY_ATTEMPTS = 3
DEFAULT_RETRY_BASE_DELAY_MS = 1000
DEFAULT_RETRY_MAX_DELAY_MS = 8000
DEFAULT_REQUEST_TIMEOUT_MS = 20000

LLM_SETTING_KEYS = (
    "deepinfra_api_key", "deepinfra_model", "deepinfra_fallback_models",
    "llm_retry_attempts", "llm_retry_base_delay_ms", "llm_retry_max_delay_ms",
    "llm_request_timeout_ms",
)
RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 520, 522, 524, 529}

logger = logging.getLogger("vitoria.bot.llm")


class LLMUnavailable(RuntimeError):
    """Todos os modelos e tentativas falharam."""


@dataclass(frozen=True)
class LLMConfig:
    token: str
    models: list[str] = field(default_factory=lambda: [DEFAULT_MODEL, *DEFAULT_FALLBACK_MODELS])
    attempts: int = DEFAULT_RETRY_ATTEMPTS
    base_delay_ms: int = DEFAULT_RETRY_BASE_DELAY_MS
    max_delay_ms: int = DEFAULT_RETRY_MAX_DELAY_MS
    timeout_ms: int = DEFAULT_REQUEST_TIMEOUT_MS

    @property
    def primary_model(self) -> str:
        return self.models[0]

    def __str__(self) -> str:  # usado em logs; nunca expoe o token
        return ",".join(self.models)


def parse_model_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    items = [item.strip() for chunk in raw.splitlines() for item in chunk.split(",")]
    result: list[str] = []
    for item in items:
        if item and item not in result:
            result.append(item)
    return result


def _int_setting(value: str | None, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return min(max(number, minimum), maximum)


def load_llm_config(db: Session) -> LLMConfig | None:
    values = {item.key: item.value for item in db.scalars(select(SystemSetting).where(
        SystemSetting.key.in_(LLM_SETTING_KEYS)
    )).all()}
    token = values.get("deepinfra_api_key")
    if not token:
        return None
    primary = (values.get("deepinfra_model") or "").strip() or DEFAULT_MODEL
    if "deepinfra_fallback_models" in values:
        fallbacks = parse_model_list(values.get("deepinfra_fallback_models"))
    else:
        fallbacks = list(DEFAULT_FALLBACK_MODELS)
    models = [primary] + [item for item in fallbacks if item != primary]
    return LLMConfig(
        token=token,
        models=models,
        attempts=_int_setting(values.get("llm_retry_attempts"), DEFAULT_RETRY_ATTEMPTS, 1, 10),
        base_delay_ms=_int_setting(values.get("llm_retry_base_delay_ms"), DEFAULT_RETRY_BASE_DELAY_MS, 0, 60000),
        max_delay_ms=_int_setting(values.get("llm_retry_max_delay_ms"), DEFAULT_RETRY_MAX_DELAY_MS, 0, 120000),
        timeout_ms=_int_setting(values.get("llm_request_timeout_ms"), DEFAULT_REQUEST_TIMEOUT_MS, 1000, 120000),
    )


def _retry_after_seconds(response: httpx.Response | None) -> float | None:
    if response is None:
        return None
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        moment = email.utils.parsedate_to_datetime(raw)
        return max(0.0, (moment - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError):
        return None


def backoff_delay(config: LLMConfig, attempt: int, retry_after: float | None = None) -> float:
    """Atraso em segundos antes da proxima tentativa (attempt comeca em 1)."""
    cap = config.max_delay_ms / 1000
    if retry_after is not None:
        return min(retry_after, cap)
    base = config.base_delay_ms / 1000 * (2 ** (attempt - 1))
    jitter = random.uniform(0, base * 0.25)
    return min(base + jitter, cap)


_sleep = time.sleep  # substituivel nos testes


def chat_completion(config: LLMConfig, messages: list[dict], max_tokens: int = 500,
                    json_mode: bool = False, purpose: str = "chat") -> tuple[str, str]:
    """Executa a chamada percorrendo modelos e tentativas. Retorna (conteudo, modelo)."""
    last_error: Exception | None = None
    for model in config.models:
        payload = {"model": model, "messages": messages, "temperature": 0, "max_tokens": max_tokens}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        for attempt in range(1, config.attempts + 1):
            started = time.monotonic()
            response = None
            try:
                response = httpx.post(
                    DEEPINFRA_CHAT_URL,
                    headers={"Authorization": f"Bearer {config.token}", "Content-Type": "application/json"},
                    json=payload,
                    timeout=config.timeout_ms / 1000,
                )
                response.raise_for_status()
                content = response.json()["choices"][0]["message"]["content"]
                if content is None:
                    raise ValueError("resposta sem conteudo")
                logger.info("llm_call_ok purpose=%s model=%s status=%s attempt=%s elapsed_ms=%s",
                            purpose, model, getattr(response, "status_code", "ok"), attempt,
                            int((time.monotonic() - started) * 1000))
                return content, model
            except httpx.HTTPStatusError as exc:
                last_error = exc
                status = exc.response.status_code
                retryable = status in RETRYABLE_STATUS
                logger.warning("llm_call_failed purpose=%s model=%s status=%s attempt=%s/%s retryable=%s body=%r",
                               purpose, model, status, attempt, config.attempts, retryable,
                               exc.response.text[:200])
                if not retryable:
                    # 400/401/403/404: insistir no mesmo modelo nao adianta; tenta o proximo.
                    break
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                logger.warning("llm_call_failed purpose=%s model=%s status=%s attempt=%s/%s retryable=True",
                               purpose, model, type(exc).__name__, attempt, config.attempts)
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                last_error = exc
                logger.warning("llm_call_failed purpose=%s model=%s status=invalid_payload attempt=%s/%s error=%s",
                               purpose, model, attempt, config.attempts, exc)
                break
            if attempt < config.attempts:
                _sleep(backoff_delay(config, attempt, _retry_after_seconds(response)))
        logger.warning("llm_model_exhausted purpose=%s model=%s", purpose, model)
    logger.error("llm_all_models_failed purpose=%s models=%s error=%s", purpose, config, last_error)
    raise LLMUnavailable(str(last_error) if last_error else "nenhum modelo configurado")