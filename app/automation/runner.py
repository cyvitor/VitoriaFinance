import logging
import time

from sqlalchemy import select

from app.automation.telegram_bot import handle_message
from app.database import SessionLocal
from app.integrations.telegram import TelegramClient
from app.models import SystemSetting, TelegramProcessedUpdate

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


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
                with SessionLocal() as db:
                    update_key = str(update["update_id"])
                    if db.scalar(select(TelegramProcessedUpdate.id).where(TelegramProcessedUpdate.update_id == update_key)):
                        continue
                    response = handle_message(db, message)
                    db.add(TelegramProcessedUpdate(update_id=update_key))
                    db.commit()
                client.send_message(message["chat"]["id"], response)
        except KeyboardInterrupt:
            logger.info("Worker encerrado.")
            return
        except Exception:
            logger.exception("Erro no polling do Telegram; nova tentativa em 5 segundos.")
            time.sleep(5)


if __name__ == "__main__":
    run()
