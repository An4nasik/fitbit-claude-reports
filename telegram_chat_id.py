#!/usr/bin/env python3
"""Узнать свой Telegram chat_id.

1. Создай бота у @BotFather (/newbot), получи токен.
2. Напиши своему боту любое сообщение (например /start).
3. Запусти: python telegram_chat_id.py <BOT_TOKEN>
"""
import json
import sys
import urllib.request

if len(sys.argv) != 2:
    print(__doc__)
    sys.exit(1)

with urllib.request.urlopen(
        f"https://api.telegram.org/bot{sys.argv[1]}/getUpdates") as r:
    updates = json.loads(r.read())

chats = {}
for u in updates.get("result", []):
    msg = u.get("message") or u.get("edited_message") or {}
    chat = msg.get("chat") or {}
    if chat.get("id"):
        name = chat.get("username") or chat.get("first_name") or "?"
        chats[chat["id"]] = name

if not chats:
    print("Обновлений нет. Сначала напиши боту сообщение и запусти снова.")
else:
    for cid, name in chats.items():
        print(f"chat_id: {cid}  ({name})")
