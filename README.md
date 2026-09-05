# Sensai Plugin

Sensai is a local plugin that helps a person's AI assistant select and set up a useful connector or built-in tool for current work. The person chooses and approves account access in the host application's normal browser flow.

## Installation (if you are human)

1. Use ChatGPT Desktop on the Codex tab or Claude Desktop on the Code tab.
2. Start a new chat and send the matching public request.

Russian:

```text
Установи Sensai https://raw.githubusercontent.com/blackvctr/sensai-plugin/main/README.md
```

English:

```text
Install Sensai https://raw.githubusercontent.com/blackvctr/sensai-plugin/main/README.md
```

## Installation manifest

This JSON document records the public installation flow. It is data for an already-authorized host installation process; the host's own safety and permission rules remain authoritative. The manifest is an unsigned public format, not cryptographic proof of a publisher.

```json
{
  "schema": "sensai-install-v1",
  "hosts": {
    "claude_desktop": {
      "russian": {
        "visible_messages": [
          {
            "phase": "before_google_sign_in",
            "text": "Я сам установлю Sensai. Сейчас откроется обычное окно Google: выберите свой аккаунт и подтвердите доступ, чтобы Sensai мог продолжать этот рабочий разговор в следующих чатах."
          },
          {
            "phase": "after_new_chat_attempt",
            "text": "Sensai установлен. Я попытался открыть новый разговор с подготовленным сообщением. Если он появился, нажмите Enter."
          }
        ],
        "steps": [
          {
            "kind": "marketplace_add",
            "repository": "blackvctr/sensai-plugin"
          },
          {
            "kind": "plugin_install",
            "plugin": "sensai@sensai",
            "scope": "user"
          },
          {
            "kind": "sensai_login",
            "server": "plugin:sensai:sensai",
            "terminal": "linux-script"
          },
          {
            "kind": "new_chat_uri",
            "request": "Проконсультируйся с Sensai. Сначала задай мне вопросы о моей работе, обычных программах и повторяющихся задачах.",
            "uri": "claude://code/new?q=%D0%9F%D1%80%D0%BE%D0%BA%D0%BE%D0%BD%D1%81%D1%83%D0%BB%D1%8C%D1%82%D0%B8%D1%80%D1%83%D0%B9%D1%81%D1%8F%20%D1%81%20Sensai.%20%D0%A1%D0%BD%D0%B0%D1%87%D0%B0%D0%BB%D0%B0%20%D0%B7%D0%B0%D0%B4%D0%B0%D0%B9%20%D0%BC%D0%BD%D0%B5%20%D0%B2%D0%BE%D0%BF%D1%80%D0%BE%D1%81%D1%8B%20%D0%BE%20%D0%BC%D0%BE%D0%B5%D0%B9%20%D1%80%D0%B0%D0%B1%D0%BE%D1%82%D0%B5%2C%20%D0%BE%D0%B1%D1%8B%D1%87%D0%BD%D1%8B%D1%85%20%D0%BF%D1%80%D0%BE%D0%B3%D1%80%D0%B0%D0%BC%D0%BC%D0%B0%D1%85%20%D0%B8%20%D0%BF%D0%BE%D0%B2%D1%82%D0%BE%D1%80%D1%8F%D1%8E%D1%89%D0%B8%D1%85%D1%81%D1%8F%20%D0%B7%D0%B0%D0%B4%D0%B0%D1%87%D0%B0%D1%85."
          }
        ]
      }
    },
    "chatgpt_desktop": {
      "steps": [
        {
          "kind": "marketplace_add",
          "repository": "blackvctr/sensai-plugin"
        },
        {
          "kind": "plugin_install",
          "plugin": "sensai@sensai"
        },
        {
          "kind": "sensai_login",
          "server": "sensai"
        }
      ]
    }
  }
}
```

## Manual recovery

Human-readable recovery guidance is published separately from this manifest.
