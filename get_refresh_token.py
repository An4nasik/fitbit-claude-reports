#!/usr/bin/env python3
"""Одноразовое получение refresh token для Google Health API.

Использование:
  python get_refresh_token.py --client-id XXX --client-secret YYY
  (или запусти без аргументов — спросит интерактивно)

Перед запуском в Google Cloud Console:
  1. Включи Google Health API.
  2. Создай OAuth client (Web application) с redirect URI: https://www.google.com
  3. Добавь свой email в Test users (страница Audience).
  4. ВАЖНО: переведи приложение в статус "In production" (кнопка Publish app),
     иначе refresh token умрёт через 7 дней.
  5. На странице Data Access добавь googlehealth.* readonly scopes.
"""
from __future__ import annotations

import argparse
import getpass
import json
import urllib.parse
import urllib.request

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REDIRECT_URI = "https://www.google.com"

SCOPES = [
    "https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly",
    "https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly",
    "https://www.googleapis.com/auth/googlehealth.sleep.readonly",
]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--client-id")
    ap.add_argument("--client-secret")
    ap.add_argument("--force-consent", action="store_true",
                    help="добавить prompt=consent (если Google не вернул refresh_token)")
    args = ap.parse_args()

    client_id = args.client_id or input("Client ID: ").strip()
    client_secret = args.client_secret or getpass.getpass("Client Secret: ").strip()

    params = {
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        # ВАЖНО: НЕ добавляем include_granted_scopes — если на этом клиенте когда-то
        # выдавались legacy fitness.* scopes, смешанный токен ломает Health API (403).
    }
    if args.force_consent:
        params["prompt"] = "consent"

    url = AUTH_URL + "?" + urllib.parse.urlencode(params)
    print("\n1. Открой в браузере:\n")
    print(url)
    print("\n2. Войди в Google-аккаунт (тот, где данные Fitbit), разреши доступ.")
    print('   Появится «Google hasn\'t verified this app» — жми Advanced -> Continue.')
    print("3. После согласия тебя перекинет на google.com — скопируй из адресной")
    print("   строки ВЕСЬ URL (или только значение параметра code) и вставь сюда.\n")

    raw = input("URL или code: ").strip()
    if "code=" in raw:
        code = urllib.parse.parse_qs(urllib.parse.urlparse(raw).query)["code"][0]
    else:
        code = raw

    body = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "client_id": client_id,
        "client_secret": client_secret,
    }).encode()
    req = urllib.request.Request(
        TOKEN_URL, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
    with urllib.request.urlopen(req) as r:
        tokens = json.loads(r.read())

    refresh = tokens.get("refresh_token")
    if not refresh:
        print("\n!!! Google не вернул refresh_token (вернул только access_token).")
        print("Отзови доступ приложения на https://myaccount.google.com/permissions")
        print("и запусти скрипт снова с флагом --force-consent")
        return

    print("\n=== УСПЕХ ===")
    print(f"refresh_token:\n{refresh}\n")
    print("Сохрани его в GitHub Secret GOOGLE_REFRESH_TOKEN (и никуда больше).")

    # Проверочный вызов API
    try:
        req = urllib.request.Request(
            "https://health.googleapis.com/v4/users/me/profile",
            headers={"Authorization": f"Bearer {tokens['access_token']}"})
        with urllib.request.urlopen(req) as r:
            profile = json.loads(r.read())
        print(f"Проверка API: OK — профиль получен ({json.dumps(profile, ensure_ascii=False)[:200]}...)")
    except Exception as e:  # noqa: BLE001
        print(f"Проверка API не удалась: {e}")
        print("Токен всё равно сохранён — проверь scopes на странице Data Access.")


if __name__ == "__main__":
    main()
