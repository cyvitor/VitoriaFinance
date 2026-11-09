import httpx
import pytest
from sqlalchemy import select

from app.automation import runner
from app.automation.telegram_bot import UNSUPPORTED_MESSAGE, handle_message
from app.database import SessionLocal
from app.integrations.telegram import TelegramClient, TelegramDownloadError, chat_action
from app.models import ChatMedia, SystemSetting, TelegramConversationMessage, TelegramProcessedUpdate
from app.services import telegram_media
from app.services.chat_media import MediaNotConfigured, MediaProcessingError, looks_like_noise
from app.services.fixed_bills import BotReply
from app.services.telegram_media import VoiceBotReply, deliver_voice, incoming_media, speakable
from test_telegram_expenses import setup_linked_user

AUDIO = b"OggS" + b"x" * 3000


class FakeTelegram:
    """Substitui o TelegramClient: nada sai para a rede."""

    def __init__(self, data=AUDIO, fail=None):
        self.data, self.fail = data, fail
        self.downloads, self.actions, self.voices, self.replies = [], [], [], []

    def download_file(self, file_id, max_bytes=None):
        self.downloads.append(file_id)
        if self.fail:
            raise self.fail
        return self.data, "voice/file_1.oga"

    def send_chat_action(self, chat_id, action="typing"):
        self.actions.append(action)

    def send_voice(self, chat_id, audio, mime_type="audio/mpeg"):
        self.voices.append((chat_id, audio, mime_type))

    def send_reply(self, chat_id, reply):
        self.replies.append((chat_id, reply))


def voice_message(duration=6, size=3000, **extra):
    return {"voice": {"file_id": "V1", "duration": duration, "mime_type": "audio/ogg", "file_size": size},
            "from": {"id": 777}, "chat": {"id": 777, "type": "private"}, **extra}


def photo_message(caption=None):
    message = {"photo": [{"file_id": "P-small", "file_size": 900, "width": 90, "height": 90},
                         {"file_id": "P-big", "file_size": 90000, "width": 1280, "height": 960}],
               "from": {"id": 777}, "chat": {"id": 777, "type": "private"}}
    if caption:
        message["caption"] = caption
    return message


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("CHAT_MEDIA_STORAGE_DIR", str(tmp_path))
    state = {"agent_calls": [], "keyboard": None, "reply": "Voc\u00ea gastou R$ 120,50 no mercado. *Quer ver os detalhes?*"}

    def fake_agent(db, user, text, reference_date=None, stored_user_content=None, **kwargs):
        state["agent_calls"].append(text)
        db.add_all([
            TelegramConversationMessage(user_id=user.id, role="user", content=stored_user_content or text, channel="telegram"),
            TelegramConversationMessage(user_id=user.id, role="assistant", content=state["reply"], channel="telegram"),
        ])
        db.commit()
        if state["keyboard"]:
            db.info["bot_keyboard"] = state["keyboard"]
        return state["reply"]

    monkeypatch.setattr("app.automation.telegram_bot.run_financial_agent", fake_agent)
    monkeypatch.setattr("app.services.telegram_media.transcribe_audio", lambda *args: "quanto gastei no mercado esse m\u00eas")
    monkeypatch.setattr("app.services.telegram_media.describe_receipt", lambda *args: "Mercado Bom, R$ 54,90, hoje, cart\u00e3o")
    monkeypatch.setattr("app.services.telegram_media.synthesize_speech", lambda db, text: (b"ID3fake-voice", "audio/mpeg"))
    with SessionLocal() as db:
        setup_linked_user(db)
    return state


def rows(db):
    return db.scalars(select(TelegramConversationMessage).order_by(TelegramConversationMessage.id)).all()


def test_looks_like_noise_rejects_whisper_hallucinations_but_keeps_real_speech():
    assert looks_like_noise("Legendas pela comunidade Amara.org")
    assert looks_like_noise("[M\u00fasica]") and looks_like_noise("...") and looks_like_noise("")
    assert looks_like_noise("tchau tchau tchau tchau tchau tchau tchau")
    assert looks_like_noise("Obrigado.", duration=8) and not looks_like_noise("Obrigado.", duration=1)
    assert not looks_like_noise("Paguei cinquenta reais de luz hoje")
    assert not looks_like_noise("Obrigado, mas registra 30 reais de padaria", duration=9)


def test_incoming_media_describes_voice_audio_photo_and_documents():
    assert incoming_media({"text": "oi"}) is None
    voice = incoming_media(voice_message())
    assert voice["kind"] == "audio" and voice["voice"] is True and voice["file_id"] == "V1" and voice["duration"] == 6
    audio = incoming_media({"audio": {"file_id": "A1", "mime_type": "audio/mpeg", "file_name": "nota.mp3", "duration": 40}})
    assert audio["kind"] == "audio" and audio["voice"] is False and audio["name"] == "nota.mp3"
    photo = incoming_media(photo_message())
    assert photo["kind"] == "image" and photo["file_id"] == "P-big"  # a maior resolucao
    assert incoming_media({"document": {"file_id": "D1", "mime_type": "application/pdf", "file_name": "fatura.pdf"}})["kind"] == "pdf"
    assert incoming_media({"document": {"file_id": "D2", "mime_type": "image/png", "file_name": "p.png"}})["kind"] == "image"
    assert incoming_media({"document": {"file_id": "D3", "mime_type": "application/octet-stream", "file_name": "gravacao.m4a"}})["kind"] == "audio"
    assert incoming_media({"document": {"file_id": "D4", "mime_type": "application/zip", "file_name": "a.zip"}})["kind"] == "file"


def test_voice_note_is_transcribed_echoed_answered_and_shared_with_the_app_history(env, client):
    env["keyboard"] = [[{"text": "Confirmar", "callback_data": "ok:c:1"}]]
    telegram = FakeTelegram()
    with SessionLocal() as db:
        reply = handle_message(db, voice_message(), media_client=telegram)
    # Gasto aguardando confirmacao (botoes): so texto, sem TTS.
    assert not isinstance(reply, VoiceBotReply) and telegram.downloads == ["V1"]
    assert env["agent_calls"] == ["quanto gastei no mercado esse m\u00eas"]  # o agente recebe a transcricao, como se fosse texto
    assert reply.text.startswith("\U0001f399\ufe0f Entendi: \u201cquanto gastei no mercado esse m\u00eas\u201d")
    assert "Voc\u00ea gastou R$ 120,50" in reply.text and reply.keyboard == env["keyboard"]
    assert getattr(reply, "voice_text", None) is None
    with SessionLocal() as db:
        user_row, assistant_row = rows(db)
        assert user_row.content == "\u00c1udio enviado. Transcri\u00e7\u00e3o: quanto gastei no mercado esse m\u00eas"
        media = db.scalars(select(ChatMedia)).all()
        assert [(m.kind, m.message_id, m.mime_type) for m in media] == [("audio_user", user_row.id, "audio/ogg")]
        assert media[0].transcript == "quanto gastei no mercado esse m\u00eas"
    token = client.post("/api/v1/auth/login", json={"username": "expense-user", "password": "password123"}).json()["access_token"]
    history = client.get("/api/v1/assistant/history", headers={"Authorization": f"Bearer {token}"}).json()["items"]
    assert [(row["role"], row["channel"]) for row in history] == [("user", "telegram"), ("assistant", "telegram")]
    assert history[0]["content"] == "\u00c1udio enviado." and history[0]["media"][0]["kind"] == "audio_user"


def test_spoken_reply_is_sent_after_the_text_and_marked_played_for_the_app(env, monkeypatch):
    telegram = FakeTelegram()
    spoken = []
    with SessionLocal() as db:
        db.add(SystemSetting(key="audio_generation_model", value="test/tts")); db.commit()
        reply = handle_message(db, voice_message(), media_client=telegram)
    monkeypatch.setattr("app.services.telegram_media.synthesize_speech",
                        lambda db, text: (spoken.append(text), (b"ID3fake-voice", "audio/mpeg"))[1])
    assert deliver_voice(telegram, 777, reply, SessionLocal) is True
    assert spoken == ["Voc\u00ea gastou 120 reais e 50 centavos no mercado. Quer ver os detalhes?"]  # sem markdown, valor por extenso
    assert telegram.voices == [(777, b"ID3fake-voice", "audio/mpeg")] and "record_voice" in telegram.actions
    with SessionLocal() as db:
        response = db.scalar(select(ChatMedia).where(ChatMedia.kind == "audio_response"))
        assert response.message_id == reply.message_id and response.played_at is not None  # o app nao toca de novo


def test_text_only_replies_when_audio_is_a_file_or_the_answer_is_long_or_tts_is_off(env):
    telegram = FakeTelegram()
    audio_file = {"audio": {"file_id": "A1", "duration": 30, "mime_type": "audio/mpeg", "file_name": "nota.mp3"},
                  "from": {"id": 777}, "chat": {"id": 777, "type": "private"}}
    with SessionLocal() as db:
        reply = handle_message(db, audio_file, media_client=telegram)
        assert not isinstance(reply, VoiceBotReply) and "Entendi" in reply.text
        env["reply"] = "linha longa " * 80
        long_reply = handle_message(db, voice_message(), media_client=telegram)
        assert not isinstance(long_reply, VoiceBotReply)
        env["reply"] = "Curta."
        short = handle_message(db, voice_message(), media_client=telegram)
    assert isinstance(short, VoiceBotReply)
    assert deliver_voice(telegram, 777, "texto simples", SessionLocal) is False
    assert deliver_voice(telegram, 777, short, SessionLocal) is False  # audio_generation_model nao configurado
    assert telegram.voices == []


def test_voice_failure_never_raises_and_never_leaks_the_bot_token(env, caplog):
    class Broken(FakeTelegram):
        def send_voice(self, *args):
            raise httpx.HTTPStatusError("403 for url https://api.telegram.org/bot123:SECRET/sendVoice",
                                        request=httpx.Request("POST", "https://x"), response=httpx.Response(403))

    with SessionLocal() as db:
        db.add(SystemSetting(key="audio_generation_model", value="test/tts")); db.commit()
        reply = handle_message(db, voice_message(), media_client=Broken())
    caplog.set_level("DEBUG")
    assert deliver_voice(Broken(), 777, reply, SessionLocal) is False
    assert "SECRET" not in caplog.text


@pytest.mark.parametrize("scenario, expected", [
    ("noise", telegram_media.MSG_NOISE), ("whisper_empty", telegram_media.MSG_NOISE),
    ("tiny", telegram_media.MSG_NOISE), ("short", telegram_media.MSG_NOISE),
    ("long", telegram_media.MSG_TOO_LONG), ("big", telegram_media.MSG_TOO_BIG),
    ("download", telegram_media.MSG_DOWNLOAD), ("off", telegram_media.MSG_AUDIO_OFF),
    ("fail", telegram_media.MSG_AUDIO_FAIL), ("format", telegram_media.MSG_AUDIO_FORMAT),
])
def test_unusable_audio_gets_a_friendly_message_and_never_reaches_the_agent(env, monkeypatch, scenario, expected):
    telegram, message = FakeTelegram(), voice_message()
    if scenario == "noise":
        monkeypatch.setattr("app.services.telegram_media.transcribe_audio", lambda *args: "Legendas pela comunidade Amara.org")
    elif scenario == "whisper_empty":
        def empty(*args):
            raise MediaProcessingError("N\u00e3o foi poss\u00edvel identificar fala no \u00e1udio.")
        monkeypatch.setattr("app.services.telegram_media.transcribe_audio", empty)
    elif scenario == "tiny":
        telegram = FakeTelegram(data=b"x" * 100)
    elif scenario == "short":
        message = voice_message(duration=0.4)
    elif scenario == "long":
        message = voice_message(duration=900)
    elif scenario == "big":
        message = voice_message(size=50 * 1024 * 1024)
    elif scenario == "download":
        telegram = FakeTelegram(fail=TelegramDownloadError("falhou"))
    elif scenario in ("off", "fail"):
        def broken(*args):
            raise MediaNotConfigured("sem modelo") if scenario == "off" else MediaProcessingError("deepinfra 500")
        monkeypatch.setattr("app.services.telegram_media.transcribe_audio", broken)
    elif scenario == "format":
        message = {"audio": {"file_id": "A9", "mime_type": "application/x-unknown", "file_name": "gravacao"},
                   "from": {"id": 777}, "chat": {"id": 777, "type": "private"}}
    with SessionLocal() as db:
        reply = handle_message(db, message, media_client=telegram)
        assert reply == expected
        assert rows(db) == [] and db.scalars(select(ChatMedia)).all() == []  # nada vai para o historico nem para o modelo
    assert env["agent_calls"] == []


def test_photo_goes_through_receipt_analysis_with_caption_buttons_and_shared_media(env):
    env["reply"] = "Mercado Bom, R$ 54,90. Confirma o registro?"
    env["keyboard"] = [[{"text": "Confirmar", "callback_data": "ok:c:9"}, {"text": "Cancelar", "callback_data": "ok:x:9"}]]
    telegram = FakeTelegram(data=b"\xff\xd8\xff" + b"j" * 500)
    with SessionLocal() as db:
        reply = handle_message(db, photo_message("foi no cr\u00e9dito"), media_client=telegram)
    assert isinstance(reply, BotReply) and not isinstance(reply, VoiceBotReply)
    assert reply.text == env["reply"] and reply.keyboard == env["keyboard"]  # os mesmos botoes do fluxo de texto
    assert telegram.downloads == ["P-big"]
    prompt = env["agent_calls"][0]
    assert "Mercado Bom, R$ 54,90" in prompt and "foi no cr\u00e9dito" in prompt and "confirma\u00e7\u00e3o expl\u00edcita" in prompt
    with SessionLocal() as db:
        user_row, _ = rows(db)
        assert user_row.content.startswith("Comprovante enviado.") and "Legenda: foi no cr\u00e9dito" in user_row.content
        media = db.scalar(select(ChatMedia))
        assert (media.kind, media.mime_type, media.message_id) == ("receipt_image", "image/jpeg", user_row.id)
        assert "Mercado Bom" in media.transcript


@pytest.mark.parametrize("scenario, expected", [
    ("off", telegram_media.MSG_IMAGE_OFF), ("fail", telegram_media.MSG_IMAGE_FAIL),
    ("download", telegram_media.MSG_DOWNLOAD), ("format", telegram_media.MSG_IMAGE_FORMAT),
])
def test_unreadable_photo_gets_a_friendly_message(env, monkeypatch, scenario, expected):
    telegram, message = FakeTelegram(data=b"img"), photo_message()
    if scenario in ("off", "fail"):
        def broken(*args):
            raise MediaNotConfigured("sem modelo") if scenario == "off" else MediaProcessingError("500")
        monkeypatch.setattr("app.services.telegram_media.describe_receipt", broken)
    elif scenario == "download":
        telegram = FakeTelegram(fail=TelegramDownloadError("x"))
    else:
        message = {"document": {"file_id": "D1", "mime_type": "image/gif", "file_name": "a.gif"},
                   "from": {"id": 777}, "chat": {"id": 777, "type": "private"}}
    with SessionLocal() as db:
        assert handle_message(db, message, media_client=telegram) == expected
        assert rows(db) == []
    assert env["agent_calls"] == []


def test_pdf_other_files_and_unsupported_messages_are_explained(env):
    base = {"from": {"id": 777}, "chat": {"id": 777, "type": "private"}}
    with SessionLocal() as db:
        assert handle_message(db, {**base, "document": {"file_id": "D", "mime_type": "application/pdf"}},
                              media_client=FakeTelegram()) == telegram_media.MSG_PDF
        assert handle_message(db, {**base, "document": {"file_id": "D", "mime_type": "application/zip"}},
                              media_client=FakeTelegram()) == telegram_media.MSG_FILE
        assert handle_message(db, {**base, "sticker": {"file_id": "S"}}) == UNSUPPORTED_MESSAGE
        assert handle_message(db, voice_message()) == telegram_media.MSG_DOWNLOAD  # sem cliente para baixar
    assert env["agent_calls"] == []


def test_voice_message_is_ignored_from_strangers_and_groups(env):
    with SessionLocal() as db:
        stranger = voice_message(); stranger["from"] = {"id": 999}; stranger["chat"] = {"id": 999, "type": "private"}
        assert "ainda nao esta associado" in handle_message(db, stranger, media_client=FakeTelegram())
        group = voice_message(); group["chat"] = {"id": -5, "type": "group"}
        assert "somente em uma conversa privada" in handle_message(db, group, media_client=FakeTelegram())
    assert env["agent_calls"] == []


def test_voice_that_hits_a_local_fixed_bill_answer_is_still_stored_in_the_shared_history(env, monkeypatch):
    monkeypatch.setattr("app.automation.telegram_bot.handle_bill_text", lambda db, user, text, day: BotReply("Claro vence dia 10."))
    with SessionLocal() as db:
        reply = handle_message(db, voice_message(), media_client=FakeTelegram())
        assert isinstance(reply, VoiceBotReply) and "Claro vence dia 10." in reply.text
        user_row, assistant_row = rows(db)
        assert user_row.role == "user" and assistant_row.content == "Claro vence dia 10."
        assert db.scalar(select(ChatMedia)).message_id == user_row.id
    assert env["agent_calls"] == []


def test_speakable_removes_markup_and_spells_out_money():
    assert speakable("**Total:** R$ 1.234,00 \U0001f389 e R$ 1,01") == "Total: 1234 reais e 1 real e 1 centavo"


def test_chat_action_keeps_indicator_alive_and_survives_failures():
    telegram = FakeTelegram()
    with chat_action(telegram, 1, "typing", interval=0.01):
        import time
        time.sleep(0.08)
    assert telegram.actions.count("typing") >= 2
    seen = len(telegram.actions)
    import time
    time.sleep(0.05)
    assert len(telegram.actions) == seen  # parou ao sair do bloco

    class Broken:
        def send_chat_action(self, *args):
            raise RuntimeError("rede")

    with chat_action(Broken(), 1, "record_voice", interval=0.01):
        result = "ok"
    assert result == "ok"


def test_runner_processes_a_voice_note_once_sending_text_then_voice(env):
    telegram = FakeTelegram()
    with SessionLocal() as db:
        db.add(SystemSetting(key="audio_generation_model", value="test/tts")); db.commit()
    update = {"update_id": 5001, "message": voice_message()}
    assert runner.process_message(telegram, update, update["message"]) is True
    assert runner.process_message(telegram, update, update["message"]) is False  # o mesmo update nao repete
    assert len(telegram.replies) == 1 and telegram.replies[0][0] == 777
    assert telegram.replies[0][1].text.startswith("\U0001f399\ufe0f Entendi")
    assert len(telegram.voices) == 1 and "typing" in telegram.actions and "record_voice" in telegram.actions
    assert env["agent_calls"] == ["quanto gastei no mercado esse m\u00eas"]
    with SessionLocal() as db:
        assert db.scalar(select(TelegramProcessedUpdate).where(TelegramProcessedUpdate.update_id == "5001"))


def test_runner_text_message_has_no_voice_and_a_failed_download_still_answers(env):
    telegram = FakeTelegram(fail=TelegramDownloadError("x"))
    update = {"update_id": 5002, "message": {"text": "oi", "from": {"id": 777}, "chat": {"id": 777, "type": "private"}}}
    assert runner.process_message(telegram, update, update["message"]) is True
    voice_update = {"update_id": 5003, "message": voice_message()}
    assert runner.process_message(telegram, voice_update, voice_update["message"]) is True
    assert telegram.replies[1][1] == telegram_media.MSG_DOWNLOAD and telegram.voices == []


def test_telegram_client_download_and_voice_upload_use_the_bot_api(monkeypatch):
    calls = []

    def fake_post(url, **kwargs):
        calls.append(("post", url, kwargs))
        if url.endswith("/getFile"):
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "voice/f.oga", "file_size": 4}},
                                  request=httpx.Request("POST", url))
        return httpx.Response(200, json={"ok": True}, request=httpx.Request("POST", url))

    def fake_get(url, **kwargs):
        calls.append(("get", url, kwargs))
        return httpx.Response(200, content=b"abcd", request=httpx.Request("GET", url))

    monkeypatch.setattr("app.integrations.telegram.httpx.post", fake_post)
    monkeypatch.setattr("app.integrations.telegram.httpx.get", fake_get)
    client = TelegramClient("111:TOKEN")
    assert client.download_file("F1", 1000) == (b"abcd", "voice/f.oga")
    assert calls[1][1] == "https://api.telegram.org/file/bot111:TOKEN/voice/f.oga"
    client.send_voice(7, b"audio", "audio/mpeg")
    client.send_chat_action(7, "record_voice")
    voice_call, action_call = calls[2], calls[3]
    assert voice_call[1].endswith("/sendVoice") and voice_call[2]["files"]["voice"][0] == "resposta.mp3"
    assert action_call[2]["json"] == {"chat_id": 7, "action": "record_voice"}
    with pytest.raises(TelegramDownloadError):
        client.download_file("F1", 2)  # acima do limite


def test_telegram_download_error_never_contains_the_token_url(monkeypatch):
    def boom(url, **kwargs):
        raise httpx.ConnectError(f"falha em {url}")

    monkeypatch.setattr("app.integrations.telegram.httpx.post", boom)
    with pytest.raises(TelegramDownloadError) as caught:
        TelegramClient("111:TOKEN").download_file("F1")
    assert "TOKEN" not in str(caught.value) and caught.value.__cause__ is None


def test_advisory_voice_answer_is_spoken_but_an_expense_confirmation_is_not(env):
    telegram = FakeTelegram()
    with SessionLocal() as db:
        spoken = handle_message(db, voice_message(), media_client=telegram)
        assert isinstance(spoken, VoiceBotReply)  # conversa/consultoria: responde tambem por voz
        env["reply"] = "Confirme este gasto:\n\nValor: R$ 10,00\n\nPosso registrar?"
        env["keyboard"] = [[{"text": "Confirmar", "callback_data": "ok:c:d:1"}]]
        confirm = handle_message(db, voice_message(), media_client=telegram)
    assert not isinstance(confirm, VoiceBotReply) and confirm.keyboard == env["keyboard"]
    assert confirm.text.startswith("\U0001f399\ufe0f Entendi:") and "Confirme este gasto" in confirm.text
