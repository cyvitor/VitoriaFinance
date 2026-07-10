import httpx


class TelegramClient:
    def __init__(self, token: str, timeout: float = 35.0):
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.timeout = timeout

    def get_updates(self, offset: int | None = None, polling_timeout: int = 25) -> list[dict]:
        params = {"timeout": polling_timeout, "allowed_updates": '["message"]'}
        if offset is not None:
            params["offset"] = offset
        response = httpx.get(f"{self.base_url}/getUpdates", params=params, timeout=self.timeout)
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(payload.get("description", "Falha ao consultar o Telegram"))
        return payload.get("result", [])

    def send_message(self, chat_id: int | str, text: str) -> None:
        response = httpx.post(f"{self.base_url}/sendMessage", json={"chat_id": chat_id, "text": text}, timeout=15.0)
        response.raise_for_status()
