import json

import httpx
from sqlalchemy import select

from app.database import SessionLocal
from app.models import SystemSetting, Transaction
from app.services import llm_client
from app.services.llm_client import LLMConfig, LLMUnavailable, backoff_delay, chat_completion, load_llm_config
from app.services.telegram_agent import _simple_reply_kind, run_financial_agent
from app.services.telegram_ai import AIUnavailableError
from test_financial_agent import setup_financings

URL = llm_client.DEEPINFRA_CHAT_URL


def busy(headers=None):
    return httpx.Response(429, json={"error": {"message": "Model busy, retry later", "code": "engine_overloaded"}},
                          headers=headers or {}, request=httpx.Request("POST", URL))


def ok(content):
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]},
                          request=httpx.Request("POST", URL))


class FakeDeepInfra:
    """Responde por modelo: cada modelo tem uma fila; quando acaba, repete o ultimo item."""

    def __init__(self, script):
        self.script = {model: list(items) for model, items in script.items()}
        self.calls = []

    def __call__(self, url, headers=None, json=None, timeout=None):
        model = json["model"]
        self.calls.append(model)
        queue = self.script.get(model) or [busy()]
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return item() if callable(item) else item


def patch_http(monkeypatch, fake, sleeps=None):
    monkeypatch.setattr(llm_client.httpx, "post", fake)
    monkeypatch.setattr(llm_client, "_sleep", (sleeps.append if sleeps is not None else (lambda s: None)))


def seed_settings(db, **extra):
    db.add(SystemSetting(key="deepinfra_api_key", value="secret", is_secret=True))
    for key, value in extra.items():
        db.add(SystemSetting(key=key, value=str(value)))
    db.commit()


PRIMARY, FALLBACK = "deepseek-ai/DeepSeek-V4-Flash-0731", "backup/Model-B"


def test_defaults_when_settings_are_empty():
    with SessionLocal() as db:
        seed_settings(db)
        config = load_llm_config(db)
    assert config.models == [PRIMARY]
    assert (config.attempts, config.base_delay_ms, config.max_delay_ms, config.timeout_ms) == (3, 1000, 8000, 20000)


def test_settings_from_admin_are_respected():
    with SessionLocal() as db:
        seed_settings(db, deepinfra_model="a/x", deepinfra_fallback_models="b/y, c/z, a/x",
                      llm_retry_attempts=2, llm_retry_base_delay_ms=500, llm_retry_max_delay_ms=3000,
                      llm_request_timeout_ms=15000)
        config = load_llm_config(db)
    assert config.models == ["a/x", "b/y", "c/z"]
    assert (config.attempts, config.base_delay_ms, config.max_delay_ms, config.timeout_ms) == (2, 500, 3000, 15000)


def test_retry_then_fallback_model_on_429(monkeypatch):
    sleeps = []
    fake = FakeDeepInfra({PRIMARY: [busy()], FALLBACK: [ok("OK")]})
    patch_http(monkeypatch, fake, sleeps)
    content, model = chat_completion(LLMConfig(token="t", models=[PRIMARY, FALLBACK]), [{"role": "user", "content": "oi"}])
    assert (content, model) == ("OK", FALLBACK)
    assert fake.calls == [PRIMARY, PRIMARY, PRIMARY, FALLBACK]
    assert len(sleeps) == 2 and 1.0 <= sleeps[0] <= 1.25 and 2.0 <= sleeps[1] <= 2.5


def test_retry_after_header_and_max_delay(monkeypatch):
    sleeps = []
    fake = FakeDeepInfra({PRIMARY: [busy({"Retry-After": "3"}), busy({"Retry-After": "60"}), ok("OK")]})
    patch_http(monkeypatch, fake, sleeps)
    content, model = chat_completion(LLMConfig(token="t", models=[PRIMARY, FALLBACK]), [{"role": "user", "content": "oi"}])
    assert model == PRIMARY and sleeps == [3.0, 8.0]
    assert backoff_delay(LLMConfig(token="t", models=[PRIMARY, FALLBACK]), 3) <= 5.0


def test_timeout_is_retried_and_all_failures_raise(monkeypatch):
    fake = FakeDeepInfra({PRIMARY: [httpx.ReadTimeout("lento")], FALLBACK: [busy()]})
    patch_http(monkeypatch, fake)
    try:
        chat_completion(LLMConfig(token="t", models=[PRIMARY, FALLBACK]), [{"role": "user", "content": "oi"}])
        assert False, "deveria falhar"
    except LLMUnavailable:
        pass
    assert fake.calls == [PRIMARY] * 3 + [FALLBACK] * 3


def test_tool_selection_429_uses_fallback_model(monkeypatch):
    decision = json.dumps({"action": "tool", "tool": "preparar_despesa",
                           "arguments": {"amount": 68.43, "description": "Ifood"}})
    fake = FakeDeepInfra({PRIMARY: [busy()], FALLBACK: [ok(decision), ok("Confirma R$ 68,43 no Ifood? Posso registrar?")]})
    patch_http(monkeypatch, fake)
    with SessionLocal() as db:
        user, *_ = setup_financings(db)
        seed_settings(db, deepinfra_fallback_models=FALLBACK)
        reply = run_financial_agent(db, user, "68,43 Ifood")
    assert "Posso registrar" in reply
    assert FALLBACK in fake.calls


def test_tool_selection_429_on_every_model_raises_unavailable(monkeypatch):
    patch_http(monkeypatch, FakeDeepInfra({}))
    with SessionLocal() as db:
        user, *_ = setup_financings(db)
        seed_settings(db, deepinfra_fallback_models=FALLBACK)
        try:
            run_financial_agent(db, user, "68,43 Ifood")
            assert False
        except AIUnavailableError:
            pass
        assert db.scalar(select(Transaction)) is None


def test_synthesis_429_shows_draft_and_local_confirmation(monkeypatch):
    decision = json.dumps({"action": "tool", "tool": "preparar_despesa",
                           "arguments": {"amount": 68.43, "description": "Ifood"}})
    fake = FakeDeepInfra({PRIMARY: [ok(decision), busy()], FALLBACK: [busy()]})
    patch_http(monkeypatch, fake)
    with SessionLocal() as db:
        user, *_ = setup_financings(db)
        seed_settings(db, deepinfra_fallback_models=FALLBACK)
        reply = run_financial_agent(db, user, "68,43 Ifood")
        assert "nao consegui formatar" not in reply
        assert "Confirme este gasto" in reply and "68,43" in reply and "Ifood" in reply
        assert "Data:" in reply and "Posso registrar" in reply
        assert db.scalar(select(Transaction)) is None
        calls_before = len(fake.calls)
        # Com a API ainda ocupada, "sim" e resolvido localmente e registra o gasto.
        confirmed = run_financial_agent(db, user, "Sim!")
        assert "registrada com sucesso" in confirmed
        transaction = db.scalar(select(Transaction))
        assert transaction is not None and str(transaction.amount) == "68.43"
        # Nenhuma chamada de selecao de ferramenta foi feita para o "sim".
        selection_calls = [c for c in fake.calls[calls_before:]]
        assert all(c in (PRIMARY, FALLBACK) for c in selection_calls)


def test_local_cancel_with_pending_draft(monkeypatch):
    decision = json.dumps({"action": "tool", "tool": "preparar_despesa",
                           "arguments": {"amount": 10, "description": "Padaria"}})
    fake = FakeDeepInfra({PRIMARY: [ok(decision), busy()], FALLBACK: [busy()]})
    patch_http(monkeypatch, fake)
    with SessionLocal() as db:
        user, *_ = setup_financings(db)
        seed_settings(db, deepinfra_fallback_models=FALLBACK)
        run_financial_agent(db, user, "10 padaria")
        run_financial_agent(db, user, "cancelar")
        assert db.scalar(select(Transaction)) is None


def test_simple_reply_kind():
    for word in ("sim", "s", "ok", "Confirmo.", "pode registrar", "SIM!"):
        assert _simple_reply_kind(word) == "confirm"
    for word in ("não", "nao", "n", "cancelar", "Cancela"):
        assert _simple_reply_kind(word) == "cancel"
    for word in ("sim, mas no cartão c6", "68,43 ifood", "não foi no crédito"):
        assert _simple_reply_kind(word) is None


def test_admin_screen_saves_llm_settings(client):
    from test_web import seed_test
    seed_test(); client.post("/login", data={"username": "vh", "password": "123456"})
    page = client.get("/system/settings")
    assert page.status_code == 200 and "deepinfra_fallback_models" in page.text and PRIMARY in page.text
    client.post("/system/settings", data={
        "deepinfra_api_key": "secret", "deepinfra_model": PRIMARY, "telegram_bot_token": "",
        "deepinfra_fallback_models": FALLBACK, "llm_retry_attempts": "3", "llm_retry_base_delay_ms": "1000",
        "llm_retry_max_delay_ms": "8000", "llm_request_timeout_ms": "20000",
    })
    with SessionLocal() as db:
        config = load_llm_config(db)
    assert config.models == [PRIMARY, FALLBACK] and config.attempts == 3
    client.post("/system/settings", data={"deepinfra_api_key": "", "deepinfra_model": "", "telegram_bot_token": "",
                                          "llm_retry_attempts": "99"})
    with SessionLocal() as db:
        assert load_llm_config(db).attempts == 3