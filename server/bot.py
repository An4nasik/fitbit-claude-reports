#!/usr/bin/env python3
"""Telegram-бот на VPS: команды перезапуска сводок и вопросы по данным здоровья.

Диспатчит GitHub Actions workflow. Работает как systemd-сервис (healthbot.service),
long polling, stdlib only.

Команды:
  /redo      — пересобрать последнюю сводку (по времени суток: утро/вечер)
  /morning   — пересобрать утреннюю сводку (после досинхронизации сна)
  /evening   — пересобрать вечернюю сводку
  /weekly    — пересобрать недельный отчёт
  любой текст — вопрос к данным (Claude ответит по твоим метрикам)
"""
import json
import time
import urllib.parse
import urllib.request

BASE = "/opt/health-summary"
ENV = {}
for line in open(f"{BASE}/.env"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        ENV[k] = v
TG = f"https://api.telegram.org/bot{ENV['TELEGRAM_BOT_TOKEN']}"
CHAT = ENV["TELEGRAM_CHAT_ID"]
GH_TOKEN = open(f"{BASE}/gh.token").read().strip()
REPO = ENV.get("GITHUB_REPO", "user/repo")  # e.g. youruser/your-private-repo
DISPATCH_URL = (f"https://api.github.com/repos/{REPO}"
                "/actions/workflows/daily-summary.yml/dispatches")
RATE_SECONDS = 30

HELP = (
    "Привет! Что я умею:\n\n"
    "/redo — пересобрать последнюю сводку (утро/вечер по времени)\n"
    "/morning — пересобрать утреннюю сводку (если сон досинхронизировался)\n"
    "/evening — пересобрать итоги дня\n"
    "/weekly — пересобрать недельный отчёт\n\n"
    "Или просто задай вопрос по данным — например: "
    "«как мой сон за последний месяц?»"
)

# команда -> mode для workflow
REPORT_CMDS = {"/redo": "auto", "/again": "auto", "/morning": "morning",
               "/evening": "evening", "/weekly": "weekly"}


def tg(method, **params):
    req = urllib.request.Request(
        f"{TG}/{method}", data=json.dumps(params).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=70) as r:
        return json.loads(r.read())


def say(text):
    tg("sendMessage", chat_id=CHAT, text=text)


def dispatch(inputs):
    body = json.dumps({"ref": "main", "inputs": inputs}).encode()
    req = urllib.request.Request(
        DISPATCH_URL, data=body, method="POST",
        headers={"Authorization": f"token {GH_TOKEN}",
                 "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=30).read()


def main():
    offset, last = None, 0.0
    while True:
        try:
            params = {"timeout": 50, "allowed_updates": ["message"]}
            if offset:
                params["offset"] = offset
            url = f"{TG}/getUpdates?" + urllib.parse.urlencode(
                {k: json.dumps(v) if isinstance(v, list) else v
                 for k, v in params.items()})
            with urllib.request.urlopen(url, timeout=70) as r:
                updates = json.loads(r.read())
            for u in updates.get("result", []):
                offset = u["update_id"] + 1
                m = u.get("message") or {}
                if str((m.get("chat") or {}).get("id")) != CHAT:
                    continue
                text = (m.get("text") or "").strip()
                if not text:
                    continue

                if text.startswith(("/start", "/help")):
                    say(HELP)
                    continue

                if time.time() - last < RATE_SECONDS:
                    say("⏳ Предыдущий запрос ещё обрабатывается, подожди чуть-чуть.")
                    continue
                last = time.time()

                cmd = text.split()[0].lower().split("@")[0]
                if cmd in REPORT_CMDS:
                    mode = REPORT_CMDS[cmd]
                    label = {"auto": "сводку", "morning": "утреннюю сводку",
                             "evening": "итоги дня",
                             "weekly": "недельный отчёт"}[mode]
                    say(f"🔄 Пересобираю {label} со свежими данными, "
                        "придёт примерно через минуту...")
                    dispatch({"mode": mode})
                else:
                    say("🔎 Смотрю данные, ответ придёт примерно через минуту...")
                    dispatch({"question": text[:400]})
        except Exception:  # noqa: BLE001
            time.sleep(5)


if __name__ == "__main__":
    main()
