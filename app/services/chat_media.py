"""Private app-chat media processing against models configured by an administrator."""
import base64
import os
import re
import uuid
from pathlib import Path

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import SystemSetting

MAX_MEDIA_BYTES = 12 * 1024 * 1024
MODEL_NAME = re.compile(r"^[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+){1,4}$")
MIME_EXTENSIONS = {"audio/mp4": ".m4a", "audio/mpeg": ".mp3", "audio/wav": ".wav",
                   "audio/x-wav": ".wav", "audio/aac": ".aac", "audio/ogg": ".ogg",
                   "audio/webm": ".webm", "image/jpeg": ".jpg", "image/png": ".png",
                   "image/webp": ".webp", "application/pdf": ".pdf"}


IMAGE_TRANSCRIPT_PREFIX = "Dados lidos da imagem (podem conter erros):\n"


class MediaProcessingError(RuntimeError):
    pass


class MediaNotConfigured(MediaProcessingError):
    """O administrador ainda nao configurou a chave ou o modelo do recurso."""


def setting(db: Session, key: str) -> str | None:
    return db.scalar(select(SystemSetting.value).where(SystemSetting.key == key))


def storage_root() -> Path:
    configured = os.getenv("CHAT_MEDIA_STORAGE_DIR")
    root = Path(configured).expanduser() if configured else Path(__file__).resolve().parents[2] / "private_media"
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def store_bytes(data: bytes, mime_type: str) -> str:
    if len(data) > MAX_MEDIA_BYTES:
        raise MediaProcessingError("O arquivo excede o limite de 12 MB.")
    extension = MIME_EXTENSIONS.get(mime_type)
    if not extension:
        raise MediaProcessingError("Formato de mídia não suportado.")
    key = f"{uuid.uuid4().hex}{extension}"
    root = storage_root()
    (root / key).write_bytes(data)
    return key


def read_bytes(key: str) -> bytes:
    if not re.fullmatch(r"[a-f0-9]{32}\.(?:m4a|mp3|wav|aac|ogg|webm|jpg|png|webp|pdf)", key):
        raise FileNotFoundError
    root = storage_root()
    path = (root / key).resolve()
    if path.parent != root:
        raise FileNotFoundError
    return path.read_bytes()


def _api_key(db: Session) -> str:
    key = setting(db, "deepinfra_api_key")
    if not key:
        raise MediaNotConfigured("A chave de IA ainda não foi configurada no sistema.")
    return key


def _model(db: Session, key: str) -> str:
    model = (setting(db, key) or "").strip()
    if not model:
        raise MediaNotConfigured("Configure o modelo de mídia nas configurações globais.")
    if not MODEL_NAME.fullmatch(model) or ".." in model:
        raise MediaProcessingError("O identificador do modelo configurado é inválido.")
    return model


def transcribe_audio(db: Session, data: bytes, filename: str, mime_type: str) -> str:
    model = _model(db, "audio_transcription_model")
    try:
        response = httpx.post(
            f"https://api.deepinfra.com/v1/inference/{model}",
            headers={"Authorization": f"Bearer {_api_key(db)}"},
            files={"audio": (filename, data, mime_type)},
            data={"language": "pt"}, timeout=120,
        )
        response.raise_for_status()
        result = response.json()
        text = str(result.get("text") or "").strip()
        if not text and isinstance(result.get("segments"), list):
            text = " ".join(str(segment.get("text") or "").strip()
                            for segment in result["segments"] if isinstance(segment, dict)).strip()
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        raise MediaProcessingError("Não foi possível transcrever o áudio. Verifique o modelo e tente novamente.") from exc
    if not text:
        raise MediaProcessingError("Não consegui identificar fala nesse áudio. Tente gravar novamente.")
    return text[:8000]


_NOISE_CREDITS = ("amara org", "legendas pela comunidade", "legenda adriana zanotto", "transcricao e legendagem",
                  "inscreva se no canal", "obrigado por assistir", "thanks for watching", "subtitles by",
                  "sous titres", "www ")
_NOISE_SHORT = {"obrigado", "obrigada", "tchau", "tchau tchau", "ate a proxima", "e ai", "hum", "hmm", "tchau obrigado",
                "musica", "aplausos", "risos", "silencio", "thank you", "bye"}


def looks_like_noise(text: str, duration: float | None = None) -> bool:
    """True quando a "transcricao" parece alucinacao do Whisper sobre silencio ou ruido.

    Creditos de legenda e laços de repeticao sao sempre rejeitados. Frases curtas genericas
    ("Obrigado.", "Tchau.") so quando o audio dura 3 s ou mais: falar so isso leva ~1 s, entao
    uma palavra solta num audio longo e o padrao tipico do Whisper sobre silencio.
    """
    import unicodedata
    stripped = (text or "").strip()
    if re.fullmatch(r"[\[\(\*_ ].*[\]\)\*_ ]", stripped) and len(stripped.split()) <= 3:
        return True  # "[Musica]", "(aplausos)"
    norm = unicodedata.normalize("NFKD", stripped.casefold())
    norm = "".join(ch for ch in norm if not unicodedata.combining(ch))
    norm = " ".join(re.sub(r"[^a-z0-9$ ]+", " ", norm).split())
    if not norm or not any(ch.isalnum() for ch in norm):
        return True
    if any(credit in norm for credit in _NOISE_CREDITS):
        return True
    words = norm.split()
    if len(words) >= 6 and len(set(words)) / len(words) < 0.25:
        return True
    return norm in _NOISE_SHORT and duration is not None and duration >= 3


def describe_receipt(db: Session, data: bytes, mime_type: str) -> str:
    model = _model(db, "image_recognition_model")
    prompt = (
        "Leia este recibo, comprovante ou boleto para ajudar a registrar uma despesa pessoal. "
        "Extraia somente os dados legíveis: estabelecimento/descrição, valor total, data, "
        "forma de pagamento e itens relevantes. Não invente dados ausentes; marque como 'não identificado'. "
        "Responda em português, em texto curto, e avise quando houver dúvida de leitura."
    )
    payload = {"model": model, "messages": [{"role": "user", "content": [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{base64.b64encode(data).decode('ascii')}"}},
    ]}], "max_tokens": 700, "temperature": 0}
    try:
        response = httpx.post("https://api.deepinfra.com/v1/openai/chat/completions",
            headers={"Authorization": f"Bearer {_api_key(db)}"}, json=payload, timeout=120)
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        if isinstance(content, list):
            content = " ".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
        text = str(content or "").strip()
    except (httpx.HTTPError, ValueError, TypeError, KeyError, IndexError) as exc:
        raise MediaProcessingError("Não foi possível ler a imagem. Verifique o modelo e tente outra foto.") from exc
    if not text:
        raise MediaProcessingError("Não consegui identificar informações legíveis nesta imagem.")
    return text[:8000]


# MPEG-1 and MPEG-2/2.5 Layer III bitrate tables (kbit/s) and sample rates, indexed by header fields.
_MP3_BITRATES = {True: [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320],
                 False: [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160]}
_MP3_SAMPLE_RATES = {3: [44100, 48000, 32000], 2: [22050, 24000, 16000], 0: [11025, 12000, 8000]}


def fix_concatenated_mp3(data: bytes) -> bytes:
    """Make an MP3 built from concatenated TTS chunks playable on Android.

    Some TTS models return one MP3 per sentence glued together, each with its own Xing/Info
    frame. ExoPlayer trusts the first one: the reply stopped after the first sentence (about a
    second) and the player never reported the end of the track, so play could not be repeated.
    Keep only the first Info frame and rewrite it with the real frame and byte totals, without
    the seek table. On any parse doubt the original bytes are returned.
    """
    pos = 0
    if data[:3] == b"ID3" and len(data) >= 10:
        pos = 10 + ((data[6] & 0x7F) << 21 | (data[7] & 0x7F) << 14 | (data[8] & 0x7F) << 7 | (data[9] & 0x7F))
    out = bytearray(data[:pos])
    info_at = -1  # offset in ``out`` of the kept Xing/Info tag
    audio_frames = 0
    changed = False
    while pos + 4 <= len(data):
        b0, b1, b2, b3 = data[pos:pos + 4]
        version, layer = (b1 >> 3) & 3, (b1 >> 1) & 3
        bitrate_index, rate_index = b2 >> 4, (b2 >> 2) & 3
        if (b0 != 0xFF or (b1 & 0xE0) != 0xE0 or version == 1 or layer != 1
                or bitrate_index in (0, 15) or rate_index == 3):
            return data  # not a clean Layer III frame stream: leave it alone
        mpeg1 = version == 3
        length = (144 if mpeg1 else 72) * _MP3_BITRATES[mpeg1][bitrate_index] * 1000 \
            // _MP3_SAMPLE_RATES[version][rate_index] + ((b2 >> 1) & 1)
        if length < 4 or pos + length > len(data):
            return data
        mono = (b3 >> 6) == 3
        side_info = (17 if mono else 32) if mpeg1 else (9 if mono else 17)
        tag_at = pos + 4 + side_info + (0 if b1 & 1 else 2)
        if data[tag_at:tag_at + 4] in (b"Xing", b"Info"):
            if info_at < 0 and audio_frames == 0:
                info_at = len(out) + (tag_at - pos)
                out += data[pos:pos + length]
            else:
                changed = True  # later chunk header: drop it
        else:
            out += data[pos:pos + length]
            audio_frames += 1
        pos += length
    out += data[pos:]
    if info_at < 0 or not changed or info_at + 16 > len(out):
        return data if info_at < 0 or not changed else bytes(out)
    out[info_at + 4:info_at + 8] = (3).to_bytes(4, "big")  # frames + bytes fields only
    out[info_at + 8:info_at + 12] = audio_frames.to_bytes(4, "big")
    out[info_at + 12:info_at + 16] = len(out).to_bytes(4, "big")
    return bytes(out)


def synthesize_speech(db: Session, text: str) -> tuple[bytes, str] | None:
    model = (setting(db, "audio_generation_model") or "").strip()
    if not model:
        return None
    if not MODEL_NAME.fullmatch(model) or ".." in model:
        raise MediaProcessingError("O identificador do modelo de voz configurado é inválido.")
    # Kokoro uses its own voice IDs; ``pf_dora`` is its Brazilian Portuguese voice.
    # Other configured TTS models use the multilingual Serena voice by default.
    voice = "pf_dora" if "kokoro" in model.lower() else "Serena"
    payload = {"model": model, "input": text[:4000], "voice": voice, "response_format": "mp3"}
    try:
        response = httpx.post("https://api.deepinfra.com/v1/audio/speech",
            headers={"Authorization": f"Bearer {_api_key(db)}"}, json=payload, timeout=120)
        response.raise_for_status()
        mime = response.headers.get("content-type", "").split(";", 1)[0]
        if mime.startswith("audio/"):
            return (fix_concatenated_mp3(response.content) if mime == "audio/mpeg" else response.content), mime
        body = response.json()
        encoded = body.get("audio") or body.get("audio_base64")
        if isinstance(encoded, dict):
            encoded = encoded.get("data") or encoded.get("b64_json")
        if not encoded and isinstance(body.get("data"), list) and body["data"]:
            encoded = body["data"][0].get("b64_json")
        if encoded:
            if isinstance(encoded, str) and encoded.startswith("data:"):
                encoded = encoded.split(",", 1)[-1]
            return base64.b64decode(encoded), "audio/mpeg"
        audio_url = body.get("audio_url") or (body.get("audio", {}).get("url") if isinstance(body.get("audio"), dict) else None)
        if audio_url and str(audio_url).startswith("https://"):
            audio_response = httpx.get(audio_url, timeout=60)
            audio_response.raise_for_status()
            return audio_response.content, audio_response.headers.get("content-type", "audio/mpeg").split(";", 1)[0]
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        raise MediaProcessingError("A resposta em áudio não pôde ser gerada; a resposta em texto foi mantida.") from exc
    raise MediaProcessingError("O modelo de voz não retornou um arquivo de áudio reconhecido.")
