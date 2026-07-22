from app.integrations.telegram import TelegramClient, telegram_html


def test_telegram_html_converts_agent_markdown_and_escapes_raw_tags():
    message = "- **Descrição:** Doce Pão\n- *Observação:* usar <conta>\n- `código`"
    assert telegram_html(message) == (
        "- <b>Descrição:</b> Doce Pão\n"
        "- <i>Observação:</i> usar &lt;conta&gt;\n"
        "- <code>código</code>"
    )


def test_send_message_uses_telegram_html_parse_mode(monkeypatch):
    captured = {}

    class Response:
        def raise_for_status(self):
            return None

    def fake_post(url, json, timeout):
        captured.update(url=url, json=json, timeout=timeout)
        return Response()

    monkeypatch.setattr("app.integrations.telegram.httpx.post", fake_post)
    TelegramClient("token").send_message(123, "**Valor:** R$ 5,00")
    assert captured["json"] == {
        "chat_id": 123, "text": "<b>Valor:</b> R$ 5,00", "parse_mode": "HTML",
    }
