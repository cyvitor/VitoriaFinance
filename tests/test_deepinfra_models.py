import httpx
from sqlalchemy import select

from app.database import SessionLocal
from app.models import SystemSetting
from app.services import deepinfra_models
from test_web import seed_test

CATALOG = ["deepseek-ai/DeepSeek-V4-Flash-0731", "deepseek-ai/DeepSeek-V4.1-Flash", "meta-llama/Llama-3.3-70B-Instruct"]
SECRET = "chave-secreta-de-teste"


class FakeCatalog:
    def __init__(self, models=None, status=200, error=None):
        self.models, self.status, self.error, self.calls = models if models is not None else CATALOG, status, error, 0

    def __call__(self, url, headers=None, timeout=None):
        self.calls += 1
        if self.error:
            raise self.error
        request = httpx.Request("GET", url)
        return httpx.Response(self.status, json={"object": "list", "data": [{"id": m} for m in self.models]}, request=request)


def login(client, key=SECRET, **settings):
    seed_test(); client.post("/login", data={"username": "vh", "password": "123456"})
    with SessionLocal() as db:
        if key:
            db.add(SystemSetting(key="deepinfra_api_key", value=key, is_secret=True))
        for name, value in settings.items():
            db.add(SystemSetting(key=name, value=value))
        db.commit()


def stored(name):
    with SessionLocal() as db:
        return db.scalar(select(SystemSetting.value).where(SystemSetting.key == name))


def save(client, **data):
    form = {"deepinfra_api_key": "", "deepinfra_model": "", "telegram_bot_token": ""}
    form.update(data)
    response = client.post("/system/settings", data=form, follow_redirects=True)
    assert response.status_code == 200
    return response.text


def test_catalog_endpoint_lists_models_with_short_cache_and_never_exposes_key(client, monkeypatch):
    login(client)
    fake = FakeCatalog(); monkeypatch.setattr(httpx, "get", fake)
    response = client.get("/system/deepinfra/models")
    assert response.status_code == 200 and response.json()["models"] == sorted(CATALOG)
    assert SECRET not in response.text
    assert client.get("/system/deepinfra/models").json()["models"] == sorted(CATALOG)
    assert fake.calls == 1  # segunda consulta veio do cache curto
    deepinfra_models.clear_cache()
    client.get("/system/deepinfra/models")
    assert fake.calls == 2


def test_catalog_endpoint_reports_clear_errors_without_free_text_fallback(client, monkeypatch):
    login(client, key=None)
    monkeypatch.setattr(httpx, "get", FakeCatalog())
    response = client.get("/system/deepinfra/models")
    assert response.status_code == 409 and "API key" in response.json()["error"]
    with SessionLocal() as db:
        db.add(SystemSetting(key="deepinfra_api_key", value=SECRET, is_secret=True)); db.commit()
    monkeypatch.setattr(httpx, "get", FakeCatalog(error=httpx.ConnectError(f"falha com {SECRET}")))
    response = client.get("/system/deepinfra/models")
    assert response.status_code == 502 and "models" not in response.json() and SECRET not in response.text
    monkeypatch.setattr(httpx, "get", FakeCatalog(status=401))
    response = client.get("/system/deepinfra/models")
    assert response.status_code == 502 and "recusou" in response.json()["error"] and SECRET not in response.text


def test_catalog_endpoint_requires_login(client):
    assert client.get("/system/deepinfra/models", follow_redirects=False).status_code in (303, 401, 403)


def test_settings_page_uses_picker_instead_of_free_text(client):
    login(client, deepinfra_model="deepseek-ai/DeepSeek-V4-Flash-0731")
    page = client.get("/system/settings").text
    assert "data-model-picker" in page and "data-multiple" in page
    assert 'data-initial="deepseek-ai/DeepSeek-V4-Flash-0731"' in page
    assert "<datalist" not in page and "Atualizar modelos" not in page
    assert "/static/model-picker.js" in page


def test_saving_rejects_model_that_does_not_exist_and_keeps_stored_value(client, monkeypatch):
    login(client, deepinfra_model=CATALOG[0], deepinfra_fallback_models=CATALOG[1])
    monkeypatch.setattr(httpx, "get", FakeCatalog())
    text = save(client, deepinfra_model="deepseek-ai/Modelo-Que-Nao-Existe")
    assert "Modelo inexistente na DeepInfra" in text and "Modelo-Que-Nao-Existe" in text
    assert stored("deepinfra_model") == CATALOG[0]
    text = save(client, deepinfra_fallback_models=f"{CATALOG[1]}, inventado/modelo")
    assert "Modelo inexistente na DeepInfra" in text and "inventado/modelo" in text
    assert stored("deepinfra_fallback_models") == CATALOG[1]
    assert SECRET not in text


def test_saving_accepts_existing_models_for_primary_and_fallbacks(client, monkeypatch):
    login(client, deepinfra_model=CATALOG[0])
    monkeypatch.setattr(httpx, "get", FakeCatalog())
    text = save(client, deepinfra_model=CATALOG[2], deepinfra_fallback_models=f"{CATALOG[1]}, {CATALOG[0]}")
    assert "Configurações globais atualizadas" in text
    assert stored("deepinfra_model") == CATALOG[2]
    assert stored("deepinfra_fallback_models") == f"{CATALOG[1]}, {CATALOG[0]}"
    # Esvaziar a lista de reserva continua possível e não consulta a DeepInfra.
    assert "Configurações globais atualizadas" in save(client, deepinfra_fallback_models="")
    assert stored("deepinfra_fallback_models") == ""


def test_unchanged_stored_models_do_not_block_other_settings_or_call_deepinfra(client, monkeypatch):
    login(client, deepinfra_model="deepseek-ai/Antigo-Removido", deepinfra_fallback_models="outro/removido")
    fake = FakeCatalog(); monkeypatch.setattr(httpx, "get", fake)
    text = save(client, deepinfra_model="deepseek-ai/Antigo-Removido", deepinfra_fallback_models="outro/removido",
                llm_retry_attempts="5")
    assert "Configurações globais atualizadas" in text and stored("llm_retry_attempts") == "5"
    assert fake.calls == 0
    assert stored("deepinfra_model") == "deepseek-ai/Antigo-Removido"


def test_changing_model_is_refused_when_deepinfra_is_unreachable(client, monkeypatch):
    login(client, deepinfra_model=CATALOG[0])
    monkeypatch.setattr(httpx, "get", FakeCatalog(error=httpx.ConnectError("sem rede")))
    text = save(client, deepinfra_model=CATALOG[2], llm_retry_attempts="7")
    assert "Modelos não alterados" in text and "Não foi possível consultar" in text
    assert stored("deepinfra_model") == CATALOG[0] and stored("llm_retry_attempts") is None


def test_new_api_key_is_used_to_validate_models_in_the_same_save(client, monkeypatch):
    login(client, key=None)
    monkeypatch.setattr(httpx, "get", FakeCatalog())
    assert "Configurações globais atualizadas" in save(client, deepinfra_api_key="nova-chave-de-teste", deepinfra_model=CATALOG[1])
    assert stored("deepinfra_model") == CATALOG[1]