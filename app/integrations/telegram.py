from contextlib import contextmanager
from html import escape
import logging
import re
import threading

import httpx

logger = logging.getLogger("vitoria.bot.telegram")


class TelegramDownloadError(RuntimeError):
    """Falha ao baixar um arquivo; a mensagem nunca carrega a URL, pois ela contem o token do bot."""


def telegram_html(text: str) -> str:
    """Converte o subconjunto de Markdown usado pelo agente em HTML seguro do Telegram."""
    safe = escape(text, quote=False)
    safe = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", safe)
    safe = re.sub(r"(?<!\*)\*([^*\n]+?)\*(?!\*)", r"<i>\1</i>", safe)
    safe = re.sub(r"`([^`\n]+?)`", r"<code>\1</code>", safe)
    return safe


class TelegramClient:
    def __init__(self, token: str, timeout: float = 35.0):
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.timeout = timeout

    def get_updates(self, offset: int | None = None, polling_timeout: int = 25) -> list[dict]:
        params = {"timeout": polling_timeout, "allowed_updates": '["message","callback_query"]'}
        if offset is not None:
            params["offset"] = offset
        response = httpx.get(f"{self.base_url}/getUpdates", params=params, timeout=self.timeout)
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(payload.get("description", "Falha ao consultar o Telegram"))
        return payload.get("result", [])

    def send_message(self, chat_id: int | str, text: str, keyboard: list[list[dict]] | None = None) -> None:
        payload = {"chat_id": chat_id, "text": telegram_html(text), "parse_mode": "HTML"}
        if keyboard:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        response = httpx.post(f"{self.base_url}/sendMessage", json=payload, timeout=15.0)
        response.raise_for_status()

    def clear_keyboard(self, chat_id: int | str, message_id: int) -> None:
        payload = {"chat_id": chat_id, "message_id": message_id, "reply_markup": {"inline_keyboard": []}}
        response = httpx.post(f"{self.base_url}/editMessageReplyMarkup", json=payload, timeout=15.0)
        response.raise_for_status()

    def answer_callback_query(self, callback_query_id: str, text: str | None = None) -> None:
        payload = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text[:200]
        response = httpx.post(f"{self.base_url}/answerCallbackQuery", json=payload, timeout=15.0)
        response.raise_for_status()

    def send_chat_action(self, chat_id: int | str, action: str = "typing") -> None:
        response = httpx.post(f"{self.base_url}/sendChatAction", json={"chat_id": chat_id, "action": action}, timeout=10.0)
        response.raise_for_status()

    def download_file(self, file_id: str, max_bytes: int | None = None) -> tuple[bytes, str]:
        """Baixa um arquivo enviado ao bot (voz, audio, foto, documento). Retorna (bytes, file_path)."""
        try:
            meta = httpx.post(f"{self.base_url}/getFile", json={"file_id": file_id}, timeout=15.0)
            meta.raise_for_status()
            info = meta.json().get("result") or {}
            file_path = info.get("file_path")
            if not file_path:
                raise TelegramDownloadError("O Telegram nao informou o caminho do arquivo.")
            if max_bytes and int(info.get("file_size") or 0) > max_bytes:
                raise TelegramDownloadError("Arquivo acima do limite permitido.")
            response = httpx.get(self.base_url.replace("/bot", "/file/bot", 1) + "/" + file_path, timeout=60.0)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise TelegramDownloadError(f"Falha ao baixar arquivo do Telegram ({type(exc).__name__}).") from None
        if max_bytes and len(response.content) > max_bytes:
            raise TelegramDownloadError("Arquivo acima do limite permitido.")
        return response.content, file_path

    def send_voice(self, chat_id: int | str, audio: bytes, mime_type: str = "audio/mpeg") -> None:
        extension = {"audio/mpeg": "mp3", "audio/wav": "wav", "audio/ogg": "ogg", "audio/mp4": "m4a"}.get(mime_type, "mp3")
        response = httpx.post(f"{self.base_url}/sendVoice", data={"chat_id": str(chat_id)},
                              files={"voice": (f"resposta.{extension}", audio, mime_type)}, timeout=60.0)
        response.raise_for_status()

    def send_reply(self, chat_id: int | str, reply) -> None:
        """Aceita texto simples ou BotReply (texto + inline keyboard)."""
        if isinstance(reply, str):
            self.send_message(chat_id, reply)
        else:
            self.send_message(chat_id, reply.text, getattr(reply, "keyboard", None))


@contextmanager
def chat_action(client, chat_id: int | str, action: str = "typing", interval: float = 4.5):
    """Mantem "digitando..." / "gravando audio..." visivel enquanto o bloco roda (o Telegram expira em ~5 s)."""
    stop = threading.Event()

    def loop() -> None:
        while not stop.is_set():
            try:
                client.send_chat_action(chat_id, action)
            except Exception:  # indicador e cosmetico: nunca pode derrubar o atendimento
                logger.debug("telegram_chat_action_failed action=%s", action)
            stop.wait(interval)

    worker = threading.Thread(target=loop, daemon=True)
    worker.start()
    try:
        yield
    finally:
        stop.set()
        worker.join(timeout=2)
