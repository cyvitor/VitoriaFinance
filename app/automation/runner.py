import time

import httpx
from sqlalchemy import select

from app.automation.telegram_bot import handle_message
from app.database import SessionLocal
from app.integrations.telegram import TelegramClient
from app.logging_config import configure_bot_logging
from app.models import SystemSetting, TelegramProcessedUpdate

logger = configure_bot_logging()


def load_telegram_token() -> str | None:
    with SessionLocal() as db:
        return db.scalar(select(SystemSetting.value).where(SystemSetting.key == "telegram_bot_token"))


def run() -> None:
    token = load_telegram_token()
    if not token:
        raise SystemExit("Configure o token do Telegram em Configuracoes globais antes de iniciar o worker.")
    client = TelegramClient(token)
    offset = None
    logger.info("Worker de automacao iniciado; aguardando mensagens do Telegram.")
    while True:
        try:
            for update in client.get_updates(offset=offset):
                offset = update["update_id"] + 1
                message = update.get("message")
                if not message:
                    continue
                logger.debug("telegram_message_received update_id=%s telegram_user_id=%s text=%r",
                             update.get("update_id"), (message.get("from") or {}).get("id"), message.get("text"))
                with SessionLocal() as db:
                    update_key = str(update["update_id"])
                    if db.scalar(select(TelegramProcessedUpdate.id).where(TelegramProcessedUpdate.update_id == update_key)):
                        continue
                    response = handle_message(db, message)
                    db.add(TelegramProcessedUpdate(update_id=update_key))
                    db.commit()
                logger.debug("telegram_message_reply update_id=%s telegram_user_id=%s reply=%r",
                             update.get("update_id"), (message.get("from") or {}).get("id"), response)
                client.send_message(message["chat"]["id"], response)
        except KeyboardInterrupt:
            logger.info("Worker encerrado.")
            return
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 409:
                logger.critical(
                    "Telegram recusou o long polling: outro worker esta usando o mesmo token. "
                    "Pare o bot duplicado ou configure tokens diferentes para producao e homologacao."
                )
                return
            logger.exception("Erro HTTP no polling do Telegram; nova tentativa em 5 segundos.")
            time.sleep(5)
        except Exception:
            logger.exception("Erro no polling do Telegram; nova tentativa em 5 segundos.")
            time.sleep(5)


if __name__ == "__main__":
    run()
