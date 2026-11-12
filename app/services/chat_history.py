"""Janela de historico compartilhada pelo modelo e pelo app.

O modelo recebe apenas as ultimas N mensagens concluidas, limitadas tambem por um orcamento total
de caracteres. N e o orcamento ficam em SystemSetting (tela Configuracoes globais), nunca no .env.
O app abre o chat com essa mesma janela e carrega paginas mais antigas somente quando o usuario pede.
"""
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import SystemSetting, TelegramConversationMessage

DEFAULT_HISTORY_MESSAGES = 20
DEFAULT_HISTORY_CHARS = 8000
HISTORY_MESSAGES_RANGE = (2, 200)
HISTORY_CHARS_RANGE = (500, 60000)
SETTING_MESSAGES = "chat_history_max_messages"
SETTING_CHARS = "chat_history_max_chars"


def _bounded(raw: str | None, default: int, low_high: tuple[int, int]) -> int:
    try:
        number = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return min(max(number, low_high[0]), low_high[1])


def history_limits(db: Session) -> tuple[int, int]:
    """(maximo de mensagens, orcamento de caracteres) vigentes; valores ausentes/invalidos usam o padrao."""
    values = {item.key: item.value for item in db.scalars(select(SystemSetting).where(
        SystemSetting.key.in_([SETTING_MESSAGES, SETTING_CHARS]))).all()}
    return (_bounded(values.get(SETTING_MESSAGES), DEFAULT_HISTORY_MESSAGES, HISTORY_MESSAGES_RANGE),
            _bounded(values.get(SETTING_CHARS), DEFAULT_HISTORY_CHARS, HISTORY_CHARS_RANGE))


def model_window(db: Session, user_id: int) -> list[TelegramConversationMessage]:
    """Mensagens concluidas que o modelo enxerga, em ordem cronologica.

    Pega as ultimas N e, andando do mais novo para o mais antigo, para ao estourar o orcamento de
    caracteres. A mensagem mais recente sempre entra, mesmo sozinha acima do orcamento.
    """
    max_messages, max_chars = history_limits(db)
    newest_first = db.scalars(select(TelegramConversationMessage).where(
        TelegramConversationMessage.user_id == user_id,
        TelegramConversationMessage.status == "done",  # audio/imagem em processamento ou com erro ainda nao e contexto
    ).order_by(TelegramConversationMessage.id.desc()).limit(max_messages)).all()
    kept: list[TelegramConversationMessage] = []
    used = 0
    for item in newest_first:
        size = len(item.content or "")
        if kept and used + size > max_chars:
            break
        kept.append(item)
        used += size
    kept.reverse()
    return kept
