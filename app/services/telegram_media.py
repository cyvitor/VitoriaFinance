"""Audio, fotos e documentos recebidos pelo Telegram.

Usa a mesma transcricao (Whisper), a mesma leitura de comprovantes e o mesmo agente do app, sempre na
area padrao do usuario. O audio/imagem original entra no historico compartilhado (ChatMedia) para
aparecer tambem no app. Os botoes de confirmar/cancelar sao os mesmos do fluxo de texto.
"""
from dataclasses import dataclass
from datetime import datetime
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.integrations.telegram import TelegramDownloadError, chat_action
from app.logging_config import get_bot_logger
from app.models import ChatMedia, TelegramConversationMessage, User
from app.services import chat_media
from app.services.chat_media import (IMAGE_TRANSCRIPT_PREFIX, MAX_MEDIA_BYTES, MediaNotConfigured,
                                     MediaProcessingError, describe_receipt, looks_like_noise, store_bytes,
                                     synthesize_speech, transcribe_audio)
from app.services.fixed_bills import BotReply

logger = get_bot_logger()

MAX_AUDIO_SECONDS = 300
MIN_AUDIO_SECONDS = 1
MIN_AUDIO_BYTES = 800
VOICE_REPLY_MAX_CHARS = 700
ECHO_MAX_CHARS = 280

MSG_TOO_BIG = "Esse arquivo \u00e9 grande demais (limite de 12 MB). Pode enviar um menor?"
MSG_TOO_LONG = "Esse \u00e1udio passa de 5 minutos. Pode dividir em partes menores ou me escrever o essencial?"
MSG_NOISE = ("N\u00e3o consegui ouvir fala clara nesse \u00e1udio \U0001f642 Pode gravar de novo, um pouco mais perto do "
             "microfone, ou me escrever o que precisa?")
MSG_DOWNLOAD = "N\u00e3o consegui baixar esse arquivo do Telegram agora. Pode enviar de novo?"
MSG_AUDIO_FORMAT = "N\u00e3o reconheci o formato desse \u00e1udio. Envie uma mensagem de voz ou um arquivo MP3, M4A, OGG ou WAV."
MSG_AUDIO_OFF = "O reconhecimento de \u00e1udio ainda n\u00e3o est\u00e1 configurado no sistema. Por enquanto, me escreva a mensagem."
MSG_AUDIO_FAIL = "N\u00e3o consegui transcrever esse \u00e1udio agora. Tente de novo em instantes ou me escreva a mensagem."
MSG_IMAGE_OFF = "A leitura de imagens ainda n\u00e3o est\u00e1 configurada no sistema. Por enquanto, me escreva os dados do gasto."
MSG_IMAGE_FAIL = ("N\u00e3o consegui ler essa imagem. Tente uma foto mais n\u00edtida, com o comprovante inteiro e boa luz, "
                  "ou me escreva os dados.")
MSG_IMAGE_FORMAT = "Esse formato de imagem n\u00e3o \u00e9 suportado. Envie como foto ou como JPG, PNG ou WebP."
MSG_PDF = ("Ainda n\u00e3o leio PDF por aqui. Envie uma foto ou um print da conta/comprovante, "
           "ou me escreva o valor e a descri\u00e7\u00e3o.")
MSG_FILE = "N\u00e3o sei ler esse tipo de arquivo. Envie um \u00e1udio, uma foto de comprovante ou escreva sua mensagem."

AUDIO_MIME_ALIASES = {
    "audio/mp3": "audio/mpeg", "audio/x-mp3": "audio/mpeg", "audio/mpeg3": "audio/mpeg",
    "audio/x-m4a": "audio/mp4", "audio/m4a": "audio/mp4", "audio/x-opus+ogg": "audio/ogg", "audio/opus": "audio/ogg",
    "audio/vnd.wave": "audio/wav", "audio/wave": "audio/wav", "audio/x-wav": "audio/wav", "audio/vorbis": "audio/ogg",
}
AUDIO_SUPPORTED = {"audio/mp4", "audio/mpeg", "audio/wav", "audio/aac", "audio/ogg", "audio/webm"}
AUDIO_EXTENSIONS = {"ogg": "audio/ogg", "oga": "audio/ogg", "opus": "audio/ogg", "mp3": "audio/mpeg", "m4a": "audio/mp4",
                    "mp4": "audio/mp4", "wav": "audio/wav", "aac": "audio/aac", "webm": "audio/webm"}
IMAGE_SUPPORTED = {"image/jpeg", "image/png", "image/webp"}


@dataclass
class VoiceBotReply(BotReply):
    """Resposta em texto (com botoes) que tambem ganhara uma mensagem de voz depois de enviada."""
    voice_text: str | None = None
    user_id: int | None = None
    message_id: int | None = None  # mensagem do assistente a que o audio pertence


def _audio_mime(mime: str | None, name: str | None) -> str | None:
    mime = (mime or "").lower().split(";", 1)[0].strip()
    mime = AUDIO_MIME_ALIASES.get(mime, mime)
    if mime in AUDIO_SUPPORTED:
        return mime
    extension = (name or "").rsplit(".", 1)[-1].lower() if "." in (name or "") else ""
    return AUDIO_EXTENSIONS.get(extension)


def _image_mime(mime: str | None) -> str | None:
    mime = (mime or "").lower().split(";", 1)[0].strip()
    mime = "image/jpeg" if mime == "image/jpg" else mime
    return mime if mime in IMAGE_SUPPORTED else None


def incoming_media(message: dict) -> dict | None:
    """Descreve o anexo da mensagem: kind audio/image/pdf/file, ou None se nao houver anexo conhecido."""
    voice = message.get("voice")
    if voice and voice.get("file_id"):
        return {"kind": "audio", "voice": True, "file_id": voice["file_id"], "size": voice.get("file_size"),
                "duration": voice.get("duration"), "mime": voice.get("mime_type") or "audio/ogg", "name": "voz.ogg"}
    audio = message.get("audio")
    if audio and audio.get("file_id"):
        return {"kind": "audio", "voice": False, "file_id": audio["file_id"], "size": audio.get("file_size"),
                "duration": audio.get("duration"), "mime": audio.get("mime_type"), "name": audio.get("file_name") or "audio"}
    photos = message.get("photo")
    if photos:
        best = max(photos, key=lambda item: (item.get("file_size") or 0, (item.get("width") or 0) * (item.get("height") or 0)))
        return {"kind": "image", "voice": False, "file_id": best["file_id"], "size": best.get("file_size"),
                "mime": "image/jpeg", "name": "foto.jpg"}
    document = message.get("document")
    if document and document.get("file_id"):
        mime = (document.get("mime_type") or "").lower()
        base = {"voice": False, "file_id": document["file_id"], "size": document.get("file_size"), "mime": mime,
                "name": document.get("file_name") or "arquivo"}
        if mime.startswith("audio/") or _audio_mime(None, base["name"]):
            return {**base, "kind": "audio", "duration": None}
        if mime.startswith("image/"):
            return {**base, "kind": "image"}
        if mime == "application/pdf":
            return {**base, "kind": "pdf"}
        return {**base, "kind": "file"}
    return None


def _last_message_id(db: Session, user_id: int) -> int:
    return db.scalar(select(TelegramConversationMessage.id).where(
        TelegramConversationMessage.user_id == user_id).order_by(TelegramConversationMessage.id.desc())) or 0


def _persist(db: Session, user: User, before_id: int, stored: str, reply_text: str, kind: str, data: bytes,
             filename: str, mime: str, transcript: str) -> int | None:
    """Anexa o original ao historico compartilhado; devolve o id da resposta do assistente."""
    try:
        user_row = db.scalar(select(TelegramConversationMessage).where(
            TelegramConversationMessage.user_id == user.id, TelegramConversationMessage.role == "user",
            TelegramConversationMessage.id > before_id).order_by(TelegramConversationMessage.id.asc()))
        if user_row is None:  # fluxos locais (contas fixas) nao gravam historico sozinhos
            user_row = TelegramConversationMessage(user_id=user.id, role="user", content=stored[:4000], channel="telegram")
            db.add(user_row)
            db.flush()
            db.add(TelegramConversationMessage(user_id=user.id, role="assistant", content=reply_text[:4000], channel="telegram"))
            db.flush()
        db.add(ChatMedia(user_id=user.id, message_id=user_row.id, kind=kind, storage_key=store_bytes(data, mime),
                         filename=filename[:180], mime_type=mime, transcript=transcript))
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("telegram_media_persist_failed user_id=%s kind=%s", user.id, kind)
        return None
    return db.scalar(select(TelegramConversationMessage.id).where(
        TelegramConversationMessage.user_id == user.id, TelegramConversationMessage.role == "assistant",
        TelegramConversationMessage.id > before_id).order_by(TelegramConversationMessage.id.desc()))


def _split(reply) -> tuple[str, list | None]:
    return (reply, None) if isinstance(reply, str) else (reply.text, reply.keyboard)


def _snippet(text: str) -> str:
    clean = re.sub(r"[*_`]+", "", " ".join(text.split()))
    return clean if len(clean) <= ECHO_MAX_CHARS else clean[:ECHO_MAX_CHARS].rstrip() + "\u2026"


def handle_media(db: Session, user: User, message: dict, media: dict, client, message_date) -> str | BotReply:
    kind = media["kind"]
    if kind == "pdf":
        return MSG_PDF
    if kind == "file":
        return MSG_FILE
    if client is None:
        return MSG_DOWNLOAD
    if media.get("size") and media["size"] > MAX_MEDIA_BYTES:
        return MSG_TOO_BIG
    try:
        return _handle_audio(db, user, media, client, message_date) if kind == "audio" \
            else _handle_image(db, user, message, media, client, message_date)
    except Exception as exc:
        db.rollback()
        from app.automation.telegram_bot import agent_error_message
        return agent_error_message(user.id, exc)


def _handle_audio(db: Session, user: User, media: dict, client, message_date) -> str | BotReply:
    from app.automation import telegram_bot
    duration = media.get("duration") or 0
    if duration and duration > MAX_AUDIO_SECONDS:
        return MSG_TOO_LONG
    if duration and duration < MIN_AUDIO_SECONDS:
        return MSG_NOISE
    mime = _audio_mime(media.get("mime"), media.get("name"))
    if not mime:
        return MSG_AUDIO_FORMAT
    try:
        data, _path = client.download_file(media["file_id"], MAX_MEDIA_BYTES)
    except TelegramDownloadError:
        return MSG_DOWNLOAD
    if len(data) < MIN_AUDIO_BYTES:
        return MSG_NOISE
    try:
        transcript = transcribe_audio(db, data, media.get("name") or "audio", mime)
    except MediaNotConfigured:
        return MSG_AUDIO_OFF
    except MediaProcessingError as exc:
        return MSG_NOISE if "identificar fala" in str(exc) else MSG_AUDIO_FAIL
    if looks_like_noise(transcript, duration or None):
        logger.info("telegram_audio_rejected_as_noise user_id=%s chars=%s", user.id, len(transcript))
        return MSG_NOISE
    echo = f"\U0001f399\ufe0f Entendi: \u201c{_snippet(transcript)}\u201d"
    stored = f"\u00c1udio enviado. Transcri\u00e7\u00e3o: {transcript}"
    before_id = _last_message_id(db, user.id)
    try:
        reply = telegram_bot.text_turn(db, user, transcript, message_date, stored_user_content=stored)
    except Exception as exc:
        db.rollback()
        return f"{echo}\n\n{telegram_bot.agent_error_message(user.id, exc)}"
    reply_text, keyboard = _split(reply)
    assistant_id = _persist(db, user, before_id, stored, reply_text, "audio_user", data,
                            media.get("name") or "voz.ogg", mime, transcript)
    full = f"{echo}\n\n{reply_text}"
    if media.get("voice") and len(reply_text) <= VOICE_REPLY_MAX_CHARS:
        return VoiceBotReply(full, keyboard, voice_text=reply_text, user_id=user.id, message_id=assistant_id)
    return BotReply(full, keyboard)


def _handle_image(db: Session, user: User, message: dict, media: dict, client, message_date) -> str | BotReply:
    from app.automation import telegram_bot
    mime = _image_mime(media.get("mime"))
    if not mime:
        return MSG_IMAGE_FORMAT
    try:
        data, _path = client.download_file(media["file_id"], MAX_MEDIA_BYTES)
    except TelegramDownloadError:
        return MSG_DOWNLOAD
    try:
        extracted = describe_receipt(db, data, mime)
    except MediaNotConfigured:
        return MSG_IMAGE_OFF
    except MediaProcessingError:
        return MSG_IMAGE_FAIL
    caption = " ".join((message.get("caption") or "").split())[:500]
    transcript = f"{IMAGE_TRANSCRIPT_PREFIX}{extracted}"
    prompt = ("O usu\u00e1rio enviou uma imagem de comprovante para ajudar a registrar uma despesa. "
              f"Dados extra\u00eddos, que devem ser confirmados pelo usu\u00e1rio: {extracted}. "
              + (f"O usu\u00e1rio escreveu junto da imagem: {caption}. " if caption else "")
              + "Apresente os dados de forma clara, pergunte o que estiver ausente e nunca registre sem mostrar "
                "o resumo e receber confirma\u00e7\u00e3o expl\u00edcita.")
    stored = f"Comprovante enviado.\n{transcript}" + (f"\nLegenda: {caption}" if caption else "")
    before_id = _last_message_id(db, user.id)
    reply = telegram_bot.agent_turn(db, user, prompt[:4000], message_date, stored_user_content=stored[:4000])
    reply_text, keyboard = _split(reply)
    _persist(db, user, before_id, stored, reply_text, "receipt_image", data, media.get("name") or "foto.jpg", mime, transcript)
    return BotReply(reply_text, keyboard)


def speakable(text: str) -> str:
    """Texto do chat -> texto para fala: sem markdown/emoji e com valores em reais por extenso."""
    def money(match: re.Match) -> str:
        whole, cents = match.group(1).replace(".", ""), match.group(2)
        reais = f"{whole} {'real' if whole == '1' else 'reais'}"
        return reais if cents == "00" else f"{reais} e {int(cents)} {'centavo' if int(cents) == 1 else 'centavos'}"
    out = re.sub(r"R\$\s*(\d{1,3}(?:\.\d{3})+|\d+),(\d{2})", money, text)
    out = re.sub(r"[*_`#>]+", "", out)
    out = re.sub("[\U0001f000-\U0001faff\u2600-\u27bf\ufe0f]", "", out)
    return " ".join(out.split())


def deliver_voice(client, chat_id, reply, session_factory) -> bool:
    """Depois do texto: gera e envia a resposta falada de uma mensagem de voz. Nunca levanta erro."""
    text = getattr(reply, "voice_text", None)
    if not text:
        return False
    try:
        with session_factory() as db:
            if not (chat_media.setting(db, "audio_generation_model") or "").strip():
                return False
            with chat_action(client, chat_id, "record_voice"):
                generated = synthesize_speech(db, speakable(text))
            if not generated:
                return False
            audio, mime = generated
            client.send_voice(chat_id, audio, mime)
            if reply.message_id and reply.user_id:
                try:
                    db.add(ChatMedia(user_id=reply.user_id, message_id=reply.message_id, kind="audio_response",
                                     storage_key=store_bytes(audio, mime), filename="resposta.mp3", mime_type=mime,
                                     transcript=text, played_at=datetime.utcnow()))  # ja ouviu no Telegram: o app nao toca sozinho
                    db.commit()
                except Exception:
                    db.rollback()
                    logger.exception("telegram_voice_persist_failed user_id=%s", reply.user_id)
            return True
    except Exception as exc:  # so o tipo: mensagens de erro do httpx trazem a URL (e o token do bot)
        logger.warning("telegram_voice_reply_failed user_id=%s error=%s", getattr(reply, "user_id", None), type(exc).__name__)
        return False
