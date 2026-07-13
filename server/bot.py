#!/usr/bin/env python3
"""Telegram-бот на VPS: вопрос по данным здоровья -> GitHub Actions -> ответ Claude.

Работает как systemd-сервис (healthbot.service), long polling, stdlib only.
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


def tg(method, **params):
    req = urllib.request.Request(
        f"{TG}/{method}", data=json.dumps(params).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=70) as r:
        return json.loads(r.read())


def say(text):
    tg("sendMessage", chat_id=CHAT, text=text)


def dispatch(question):
    body = json.dumps({"ref": "main", "inputs": {"question": question[:400]}}).encode()
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
                if text.startswith("/start"):
                    say("Привет! Задай вопрос по своим данным — например: "
                        "«как мой сон за последний месяц?»")
                    continue
                if time.time() - last < RATE_SECONDS:
                    say("⏳ Предыдущий вопрос ещё обрабатывается, подожди чуть-чуть.")
                    continue
                last = time.time()
                say("🔎 Смотрю данные, ответ придёт примерно через минуту...")
                dispatch(text)
        except Exception:  # noqa: BLE001
            time.sleep(5)


if __name__ == "__main__":
    main()
