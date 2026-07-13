#!/usr/bin/env python3
"""Ежедневная сводка здоровья: Google Health API -> Telegram.

Режимы:
  morning — сон прошлой ночи + метрики восстановления (пульс покоя, HRV, SpO2, дыхание)
  evening — итоги дня: шаги, дистанция, этажи, калории, активные зоны, пульс, тренировки
  auto    — выбирается по локальному времени (04:00–13:59 -> morning, иначе evening)

Переменные окружения (обязательные):
  GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN,
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
Опциональные:
  TZ_NAME (default: Europe/Moscow)

Примеры:
  python summary.py --mode evening --dry-run
  python summary.py --mode morning --date 2026-07-09 --debug
  python summary.py --test-telegram
"""
from __future__ import annotations

import argparse
import html
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

API = "https://health.googleapis.com/v4"
TOKEN_URL = "https://oauth2.googleapis.com/token"

DEBUG = False

MONTHS_RU = ["", "января", "февраля", "марта", "апреля", "мая", "июня",
             "июля", "августа", "сентября", "октября", "ноября", "декабря"]
WEEKDAYS_RU = ["понедельник", "вторник", "среда", "четверг",
               "пятница", "суббота", "воскресенье"]

STAGES_RU = {"DEEP": "глубокий", "LIGHT": "лёгкий", "REM": "REM", "AWAKE": "бодрств."}

ACTIVITY_RU = {
    "RUN": "Бег", "RUNNING": "Бег", "TREADMILL": "Дорожка", "WALK": "Ходьба",
    "WALKING": "Ходьба", "HIKING": "Хайкинг", "BIKE": "Велосипед",
    "BIKING": "Велосипед", "SWIM": "Плавание", "SWIMMING": "Плавание",
    "WEIGHTS": "Силовая", "STRENGTH_TRAINING": "Силовая", "WORKOUT": "Тренировка",
    "YOGA": "Йога", "ELLIPTICAL": "Эллипс", "SPORT": "Спорт",
    "AEROBIC_WORKOUT": "Аэробика", "INTERVAL_WORKOUT": "Интервальная",
}


class ApiError(Exception):
    def __init__(self, code: int, body: str, url: str = ""):
        self.code, self.body, self.url = code, body, url
        super().__init__(f"HTTP {code} for {url}: {body[:500]}")


def log(*args):
    print(*args, file=sys.stderr, flush=True)


def http_json(method: str, url: str, *, headers=None, body: bytes | None = None,
              retries: int = 3) -> dict:
    """Единая точка сетевых вызовов (удобно мокать в тестах)."""
    last_err = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=body, method=method,
                                     headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            err_body = e.read().decode(errors="replace")
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(2 ** attempt)
                last_err = ApiError(e.code, err_body, url)
                continue
            raise ApiError(e.code, err_body, url) from None
        except urllib.error.URLError as e:
            if attempt < retries:
                time.sleep(2 ** attempt)
                last_err = e
                continue
            raise
    raise last_err  # pragma: no cover


def env(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        log(f"ОШИБКА: не задана переменная окружения {name}")
        sys.exit(2)
    return val


# ---------------------------------------------------------------- Google Health

def get_access_token() -> str:
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "client_id": env("GOOGLE_CLIENT_ID"),
        "client_secret": env("GOOGLE_CLIENT_SECRET"),
        "refresh_token": env("GOOGLE_REFRESH_TOKEN"),
    }).encode()
    resp = http_json("POST", TOKEN_URL, body=body,
                     headers={"Content-Type": "application/x-www-form-urlencoded"})
    return resp["access_token"]


class Health:
    def __init__(self, token: str):
        self.h = {"Authorization": f"Bearer {token}",
                  "Content-Type": "application/json"}

    def list_points(self, dtype: str, flt: str) -> list[dict]:
        base = f"{API}/users/me/dataTypes/{dtype}/dataPoints"
        points, page_token = [], None
        while True:
            qs = {"filter": flt, "pageSize": "1000"}
            if page_token:
                qs["pageToken"] = page_token
            raw = http_json("GET", base + "?" + urllib.parse.urlencode(qs),
                            headers=self.h)
            if DEBUG:
                log(f"--- list {dtype} ---\n{json.dumps(raw, ensure_ascii=False)[:3000]}")
            points.extend(raw.get("dataPoints", []))
            page_token = raw.get("nextPageToken")
            if not page_token:
                return points

    def daily_rollup(self, dtype: str, day: date) -> dict | None:
        """Роллап за один календарный день (civil time часового пояса устройства)."""
        nxt = day + timedelta(days=1)
        body = json.dumps({
            "range": {
                "start": {"date": {"year": day.year, "month": day.month, "day": day.day}},
                "end": {"date": {"year": nxt.year, "month": nxt.month, "day": nxt.day}},
            },
            "windowSizeDays": 1,
        }).encode()
        raw = http_json("POST", f"{API}/users/me/dataTypes/{dtype}/dataPoints:dailyRollUp",
                        headers=self.h, body=body)
        if DEBUG:
            log(f"--- rollup {dtype} {day} ---\n{json.dumps(raw, ensure_ascii=False)[:3000]}")
        pts = raw.get("rollupDataPoints") or []
        return pts[0] if pts else None

    def daily_rollups_range(self, dtype: str, d0: date, d1: date) -> list:
        """Роллапы по одному на день за диапазон [d0, d1). Максимум 14 дней для heart-rate."""
        body = json.dumps({
            "range": {
                "start": {"date": {"year": d0.year, "month": d0.month, "day": d0.day}},
                "end": {"date": {"year": d1.year, "month": d1.month, "day": d1.day}},
            },
            # pageSize НЕ передавать: API отвечает INVALID_ROLLUP_QUERY_DURATION (баг)
            "windowSizeDays": 1,
        }).encode()
        raw = http_json("POST", f"{API}/users/me/dataTypes/{dtype}/dataPoints:dailyRollUp",
                        headers=self.h, body=body)
        if DEBUG:
            log(f"--- rollup-range {dtype} {d0}..{d1} ---\n"
                f"{json.dumps(raw, ensure_ascii=False)[:2000]}")
        return raw.get("rollupDataPoints") or []


# ------------------------------------------------------------------- extractors

def _walk(obj, path=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk(v, path + "." + k.lower())
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v, path)
    else:
        yield path, obj


def _as_num(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v)  # int64 приходит строкой
        except ValueError:
            return None
    return None


def find_num(obj, *patterns) -> float | None:
    """Ищет числовой лист, путь которого содержит все подстроки одного из паттернов.

    Паттерны проверяются по порядку — первый успешный побеждает.
    """
    if obj is None:
        return None
    leaves = []
    for p, v in _walk(obj):
        n = _as_num(v)
        if n is None:
            continue
        if p.endswith(".year") or p.endswith(".month") or p.endswith(".day"):
            continue
        leaves.append((p, n))
    for pat in patterns:
        for p, n in leaves:
            if all(s in p for s in pat):
                return n
    return None


def find_str(obj, *patterns) -> str | None:
    if obj is None:
        return None
    leaves = [(p, v) for p, v in _walk(obj) if isinstance(v, str) and v]
    for pat in patterns:
        for p, v in leaves:
            if all(s in p for s in pat):
                return v
    return None


def hr_stats(obj) -> dict:
    """Мин/средн/макс пульса из HeartRateRollupValue (по суффиксам ключей)."""
    out = {}
    if obj is None:
        return out
    for p, v in _walk(obj):
        n = _as_num(v)
        if n is None or "resting" in p or "confidence" in p or "zone" in p:
            continue
        if p.endswith("avg") or p.endswith("mean"):
            out.setdefault("avg", n)
        elif p.endswith("max"):
            out.setdefault("max", n)
        elif p.endswith("min") and not p.endswith("perminute"):
            out.setdefault("min", n)
    return out


def parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def point_date(p: dict) -> date | None:
    d = (p.get("civilStartTime") or {}).get("date") or {}
    try:
        return date(int(d["year"]), int(d["month"]), int(d["day"]))
    except (KeyError, TypeError, ValueError):
        return None


def trend(series: dict, day: date) -> str:
    """Сравнение значения за day со средним за предыдущие 7 дней."""
    today = series.get(day.isoformat())
    lo = (day - timedelta(days=7)).isoformat()
    prev = [v for k, v in series.items() if lo <= k < day.isoformat()]
    if today is None or len(prev) < 3:
        return ""
    avg = sum(prev) / len(prev)
    if avg <= 0:
        return ""
    delta = (today - avg) / avg * 100
    if abs(delta) < 3:
        return " (≈ ср. нед.)"
    arrow = "↑" if delta > 0 else "↓"
    return f" ({arrow}{abs(delta):.0f}% к ср. нед.)"


HISTORY_DAYS = 14


# ---------------------------------------------------------------------- fetching

def utc_range(tz: ZoneInfo, d0: date, d1: date) -> tuple:
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    a = datetime(d0.year, d0.month, d0.day, tzinfo=tz).astimezone(timezone.utc)
    b = datetime(d1.year, d1.month, d1.day, tzinfo=tz).astimezone(timezone.utc)
    return a.strftime(fmt), b.strftime(fmt)


def safe(fn, misses: list, name: str):
    try:
        return fn()
    except ApiError as e:
        log(f"[warn] {name}: HTTP {e.code} {e.body[:300]}")
        misses.append(name)
        return None
    except Exception as e:  # noqa: BLE001
        log(f"[warn] {name}: {e}")
        misses.append(name)
        return None


def fetch_daily_series(hc: Health, dtype: str, d0: date, d1: date, misses: list,
                       name: str, *patterns) -> dict:
    """Серия значений daily-типа по датам за [d0, d1)."""
    snake = dtype.replace("-", "_")
    parts = dtype.split("-")
    camel = parts[0] + "".join(w.capitalize() for w in parts[1:])

    def go():
        flt = f'{snake}.date >= "{d0.isoformat()}" AND {snake}.date < "{d1.isoformat()}"'
        series = {}
        for p in hc.list_points(dtype, flt):
            payload = p.get(camel) or {}
            dd = payload.get("date") or {}
            try:
                k = date(int(dd["year"]), int(dd["month"]), int(dd["day"])).isoformat()
            except (KeyError, TypeError, ValueError):
                continue
            v = find_num(payload, *patterns)
            if v is not None:
                series[k] = v
        return series

    return safe(go, misses, name) or {}


def fetch_rollup_series(hc: Health, dtype: str, d0: date, d1: date, misses: list,
                        name: str, extractor) -> dict:
    """Серия значений rollup-типа по датам за [d0, d1)."""
    pts = safe(lambda: hc.daily_rollups_range(dtype, d0, d1), misses, name) or []
    series = {}
    for p in pts:
        d = point_date(p)
        v = extractor(p)
        if d and v is not None:
            series[d.isoformat()] = v
    return series


def fetch_sleep_range(hc: Health, tz: ZoneInfo, d0: date, d1: date,
                      misses: list) -> list:
    """Все сессии сна, закончившиеся в [d0, d1), с локальной датой конца."""
    a, b = utc_range(tz, d0, d1)
    flt = f'sleep.interval.end_time >= "{a}" AND sleep.interval.end_time < "{b}"'
    pts = safe(lambda: hc.list_points("sleep", flt), misses, "сон") or []
    sessions = []
    for p in pts:
        s = p.get("sleep") or {}
        summary = s.get("summary") or {}
        interval = s.get("interval") or {}
        if not interval.get("endTime"):
            continue
        stages = {}
        for st in (summary.get("stagesSummary") or []):
            t = st.get("type", "")
            m = _as_num(st.get("minutes"))
            if t and m is not None:
                stages[t] = stages.get(t, 0) + m
        sessions.append({
            "end_date": parse_iso(interval["endTime"]).astimezone(tz).date().isoformat(),
            "asleep": find_num(summary, ("minutesasleep",)) or 0,
            "awake": find_num(summary, ("minutesawake",)),
            "stages": stages,
            "start": interval.get("startTime"),
            "end": interval.get("endTime"),
        })
    sessions.sort(key=lambda x: x["asleep"], reverse=True)
    return sessions


def fetch_exercises(hc: Health, tz: ZoneInfo, day: date, misses: list) -> list:
    # Session-типы (кроме сна/ЭКГ) фильтруются по civil-времени устройства
    nxt = day + timedelta(days=1)
    flt = (f'exercise.interval.civil_start_time >= "{day.isoformat()}" '
           f'AND exercise.interval.civil_start_time < "{nxt.isoformat()}"')
    pts = safe(lambda: hc.list_points("exercise", flt), misses, "тренировки") or []
    out = []
    for p in pts:
        ex = p.get("exercise") or {}
        interval = ex.get("interval") or {}
        minutes = None
        if interval.get("startTime") and interval.get("endTime"):
            minutes = (parse_iso(interval["endTime"]) -
                       parse_iso(interval["startTime"])).total_seconds() / 60
        name = (find_str(ex, ("activitytype",), ("exercisetype",), ("activityname",),
                         ("name",), ("type",)) or "Активность")
        out.append({
            "name": ACTIVITY_RU.get(name.upper(), name.replace("_", " ").capitalize()),
            "minutes": minutes,
            "kcal": find_num(ex, ("calor",), ("energy",), ("kcal",)),
        })
    return out


# ------------------------------------------------------------------- formatting

def fmt_int(n) -> str:
    return "н/д" if n is None else f"{int(round(n)):,}".replace(",", " ")


def fmt_hm(minutes) -> str:
    if minutes is None:
        return "н/д"
    m = int(round(minutes))
    return f"{m // 60} ч {m % 60:02d} мин"


def date_ru(d: date) -> str:
    return f"{WEEKDAYS_RU[d.weekday()]}, {d.day} {MONTHS_RU[d.month]}"


def extract_distance_m(obj) -> float | None:
    """Дистанция в метрах с учётом единиц (API отдаёт millimetersSum)."""
    for unit, k in ((0.001, ("millimeter",)), (0.01, ("centimeter",)),
                    (1000.0, ("kilometer",)), (1.0, ("meter",)),
                    (1.0, ("distance",))):
        v = find_num(obj, k)
        if v is not None:
            return v * unit
    return None


def build_evening(hc: Health, tz: ZoneInfo, day: date) -> tuple:
    misses = []
    start = day - timedelta(days=HISTORY_DAYS - 1)
    nxt = day + timedelta(days=1)

    steps_s = fetch_rollup_series(hc, "steps", start, nxt, misses, "шаги",
                                  lambda v: find_num(v, ("count",), ("sum",)))
    kcal_s = fetch_rollup_series(hc, "total-calories", start, nxt, misses, "калории",
                                 lambda v: find_num(v, ("calor",), ("energy",),
                                                    ("kcal",), ("sum",)))
    azm_s = fetch_rollup_series(hc, "active-zone-minutes", start, nxt, misses,
                                "активные зоны",
                                lambda v: find_num(v, ("total",), ("minute",), ("sum",)))
    hr_pts = safe(lambda: hc.daily_rollups_range("heart-rate", start, nxt),
                  misses, "пульс") or []
    hr = {}
    for p in hr_pts:
        if point_date(p) == day:
            hr = hr_stats(p)

    def r(t, name):
        return safe(lambda: hc.daily_rollup(t, day), misses, name)

    steps = steps_s.get(day.isoformat())
    kcal = kcal_s.get(day.isoformat())
    azm = azm_s.get(day.isoformat())
    dist_m = extract_distance_m(r("distance", "дистанция"))
    floors = find_num(r("floors", "этажи"), ("floor",), ("count",), ("sum",))
    exercises = fetch_exercises(hc, tz, day, misses)

    rhr_s = fetch_daily_series(hc, "daily-resting-heart-rate", start, nxt, misses,
                               "пульс покоя", ("beat",), ("restingheartrate",), ("bpm",))
    hrv_s = fetch_daily_series(hc, "daily-heart-rate-variability", start, nxt, misses,
                               "HRV", ("rmssd",), ("milli",), ("variability",), ("hrv",))
    sleep_s = {}
    for s in fetch_sleep_range(hc, tz, start, nxt, misses):
        sleep_s[s["end_date"]] = max(sleep_s.get(s["end_date"], 0), s["asleep"])

    history = {"report_day": day.isoformat(), "steps": steps_s, "kcal": kcal_s,
               "active_zone_minutes": azm_s, "exercises_today": exercises,
               "sleep_minutes": sleep_s, "resting_heart_rate": rhr_s, "hrv_ms": hrv_s}

    lines = [f"📊 <b>Итоги дня — {date_ru(day)}</b>", ""]
    line = f"🚶 Шаги: <b>{fmt_int(steps)}</b>"
    if dist_m:
        km = dist_m / 1000
        line += f" ({km:.2f} км)" if km < 10 else f" ({km:.1f} км)"
    lines.append(line + trend(steps_s, day))
    if kcal is not None:
        lines.append(f"🔥 Калории: <b>{fmt_int(kcal)}</b> ккал" + trend(kcal_s, day))
    if azm is not None:
        lines.append(f"⚡ Активные зоны: <b>{fmt_int(azm)}</b> мин" + trend(azm_s, day))
    if floors:
        lines.append(f"🪜 Этажи: <b>{fmt_int(floors)}</b>")
    if hr:
        parts = []
        if "avg" in hr:
            parts.append(f"средн. {fmt_int(hr['avg'])}")
        if "min" in hr:
            parts.append(f"мин. {fmt_int(hr['min'])}")
        if "max" in hr:
            parts.append(f"макс. {fmt_int(hr['max'])}")
        lines.append("❤️ Пульс: " + " / ".join(parts))
    if exercises:
        lines.append("")
        lines.append("🏋️ Тренировки:")
        for ex in exercises:
            item = f"  • {html.escape(ex['name'])}"
            if ex["minutes"]:
                item += f" — {int(round(ex['minutes']))} мин"
            if ex["kcal"]:
                item += f", {fmt_int(ex['kcal'])} ккал"
            lines.append(item)
    if misses:
        lines += ["", f"⚠️ Нет данных: {', '.join(dict.fromkeys(misses))}"]
    return "\n".join(lines), history


def build_morning(hc: Health, tz: ZoneInfo, day: date) -> tuple:
    misses = []
    start = day - timedelta(days=HISTORY_DAYS - 1)
    nxt = day + timedelta(days=1)
    all_sleep = fetch_sleep_range(hc, tz, start, nxt, misses)
    sessions = [s for s in all_sleep if s["end_date"] == day.isoformat()]

    rhr_s = fetch_daily_series(hc, "daily-resting-heart-rate", start, nxt, misses,
                               "пульс покоя", ("beat",), ("restingheartrate",), ("bpm",))
    hrv_s = fetch_daily_series(hc, "daily-heart-rate-variability", start, nxt, misses,
                               "HRV", ("rmssd",), ("milli",), ("variability",), ("hrv",))
    spo2_s = fetch_daily_series(hc, "daily-oxygen-saturation", start, nxt, misses,
                                "SpO2", ("percent", "avg"), ("percent",),
                                ("saturation",), ("avg",))
    breath_s = fetch_daily_series(hc, "daily-respiratory-rate", start, nxt, misses,
                                  "дыхание", ("breath",), ("rate",))

    def latest(series):
        for d in (day, day - timedelta(days=1)):
            if d.isoformat() in series:
                return series[d.isoformat()], d
        return None, day

    rhr, rhr_d = latest(rhr_s)
    hrv, hrv_d = latest(hrv_s)
    spo2, _ = latest(spo2_s)
    breath, _ = latest(breath_s)

    sleep_s = {}
    for s in all_sleep:
        sleep_s[s["end_date"]] = max(sleep_s.get(s["end_date"], 0), s["asleep"])

    steps_s = fetch_rollup_series(hc, "steps", start, nxt, misses, "шаги",
                                  lambda v: find_num(v, ("count",), ("sum",)))
    azm_s = fetch_rollup_series(hc, "active-zone-minutes", start, nxt, misses,
                                "активные зоны",
                                lambda v: find_num(v, ("total",), ("minute",), ("sum",)))

    history = {"report_day": day.isoformat(), "sleep_minutes": sleep_s,
               "resting_heart_rate": rhr_s, "hrv_ms": hrv_s,
               "spo2_pct": spo2_s, "respiratory_rate": breath_s,
               "steps": steps_s, "active_zone_minutes": azm_s}

    lines = [f"🌅 <b>Утренняя сводка — {date_ru(day)}</b>", ""]
    if sessions:
        main = sessions[0]
        lines.append(f"😴 Сон: <b>{fmt_hm(main['asleep'])}</b>"
                     + trend(sleep_s, day))
        if main["stages"]:
            order = ["DEEP", "REM", "LIGHT", "AWAKE"]
            st = [f"{STAGES_RU.get(k, k.lower())} {fmt_hm(main['stages'][k])}"
                  for k in order if k in main["stages"]]
            lines.append("   " + " · ".join(st))
        if main["start"] and main["end"]:
            s = parse_iso(main["start"]).astimezone(tz).strftime("%H:%M")
            e = parse_iso(main["end"]).astimezone(tz).strftime("%H:%M")
            lines.append(f"   🛏 {s} → {e}")
        for nap in sessions[1:]:
            lines.append(f"   + дневной сон {fmt_hm(nap['asleep'])}")
    else:
        lines.append("😴 Сон: данных пока нет")

    lines.append("")
    if rhr is not None:
        lines.append(f"❤️ Пульс покоя: <b>{fmt_int(rhr)}</b> уд/мин"
                     + trend(rhr_s, rhr_d))
    if hrv is not None:
        lines.append(f"📈 HRV: <b>{fmt_int(hrv)}</b> мс" + trend(hrv_s, hrv_d))
    if spo2 is not None:
        lines.append(f"🫁 SpO2: <b>{spo2:.0f}%</b>")
    if breath is not None:
        lines.append(f"🌬 Дыхание: <b>{breath:.1f}</b>/мин")
    if misses:
        lines += ["", f"⚠️ Нет данных: {', '.join(dict.fromkeys(misses))}"]
    return "\n".join(lines), history


def build_analysis(history: dict, base_text: str, weekly: bool = False) -> str | None:
    """Структурированный анализ через Claude Code CLI. None, если недоступен."""
    if weekly:
        fmt_block = (
            "Ответ по-русски, СТРОГО в этом формате, обычный текст без markdown:\n"
            "📅 Итоги: <2-3 предложения — главное о неделе>\n"
            "📊 Сравнение: <неделя против прошлой — что лучше, что хуже>\n"
            "📈 Тренды: <длинные тенденции по всей глубине данных>\n"
            "🔗 Связи: <зависимости между метриками>\n"
            "⚠️ Внимание: <медленные ухудшения/ранние сигналы; иначе: всё в норме>\n"
            "🎯 На неделю: <2-3 конкретных пункта>"
        )
    else:
        fmt_block = (
            "Ответ по-русски, СТРОГО в этом формате, каждая секция с новой строки, "
            "1-2 предложения на секцию, обычный текст без markdown:\n"
            "📊 Динамика: <сегодня на фоне двух недель>\n"
            "🔗 Связи: <зависимости между метриками, которые видны в данных>\n"
            "⚠️ Внимание: <медленные ухудшения или ранние сигналы, если есть; "
            "иначе напиши: всё в норме>\n"
            "💡 Совет: <один конкретный совет на завтра>"
        )
    prompt = (
        "Ты ассистент по фитнес-данным. Ниже метрики Fitbit за последние 14 дней "
        "(JSON, ключи — даты) и сегодняшняя сводка.\n\n"
        f"ДАННЫЕ:\n{json.dumps(history, ensure_ascii=False)}\n\n"
        f"СВОДКА:\n{base_text}\n\n"
        "Задача: найти то, что человек сам не замечает из-за плавных изменений.\n"
        "Методика: сравни среднее за последние 7 дней со средним за предыдущие 7; "
        "ищи монотонные тренды от 3 дней; ищи СВЯЗКИ между метриками "
        "(сон ↔ HRV ↔ пульс покоя ↔ дыхание ↔ нагрузка) — например, одновременные "
        "рост пульса покоя, падение HRV и учащение дыхания могут быть ранним "
        "сигналом болезни или перегрузки.\n\n"
        f"{fmt_block}\n\n"
        "Это наблюдения по данным трекера, не медицинские диагнозы — "
        "формулируй как наблюдения, без слова «диагноз» и без дисклеймеров."
    )
    cmd = os.environ.get("CLAUDE_BIN", "claude")
    model = os.environ.get("CLAUDE_MODEL", "")
    attempts = [["--model", model]] if model else []
    attempts.append([])  # fallback: дефолтная модель, если явная недоступна/лимит
    for extra in attempts:
        try:
            r = subprocess.run([cmd, "-p", *extra], input=prompt,
                               capture_output=True, text=True, timeout=240)
        except (OSError, subprocess.TimeoutExpired) as e:
            log(f"[warn] анализ ({extra or 'default'}): {e}")
            continue
        out = (r.stdout or "").strip()
        if r.returncode == 0 and out:
            return out
        log(f"[warn] claude {extra or 'default'} exit={r.returncode} "
            f"stdout={out[:300]!r} stderr={(r.stderr or '')[:300]!r}")
    return None


def collect_history(hc: Health, tz: ZoneInfo, day: date, days: int = 28,
                    store_dir: str = "data") -> tuple:
    """Полный набор серий за `days` дней + архив из хранилища (до 90 дней)."""
    misses = []
    start = day - timedelta(days=days - 1)
    nxt = day + timedelta(days=1)

    h = {"report_day": day.isoformat()}
    h["steps"] = fetch_rollup_series(hc, "steps", start, nxt, misses, "шаги",
                                     lambda v: find_num(v, ("count",), ("sum",)))
    # total-calories: максимум 14 дней на запрос — бьём диапазон на куски
    h["kcal"] = {}
    chunk_start = start
    while chunk_start < nxt:
        chunk_end = min(chunk_start + timedelta(days=14), nxt)
        h["kcal"].update(fetch_rollup_series(
            hc, "total-calories", chunk_start, chunk_end, misses, "калории",
            lambda v: find_num(v, ("calor",), ("energy",), ("kcal",), ("sum",))))
        chunk_start = chunk_end
    h["active_zone_minutes"] = fetch_rollup_series(
        hc, "active-zone-minutes", start, nxt, misses, "активные зоны",
        lambda v: find_num(v, ("total",), ("minute",), ("sum",)))
    h["resting_heart_rate"] = fetch_daily_series(
        hc, "daily-resting-heart-rate", start, nxt, misses, "пульс покоя",
        ("beat",), ("restingheartrate",), ("bpm",))
    h["hrv_ms"] = fetch_daily_series(
        hc, "daily-heart-rate-variability", start, nxt, misses, "HRV",
        ("rmssd",), ("milli",), ("variability",), ("hrv",))
    h["spo2_pct"] = fetch_daily_series(
        hc, "daily-oxygen-saturation", start, nxt, misses, "SpO2",
        ("percent", "avg"), ("percent",), ("saturation",), ("avg",))
    h["respiratory_rate"] = fetch_daily_series(
        hc, "daily-respiratory-rate", start, nxt, misses, "дыхание",
        ("breath",), ("rate",))
    sleep_s = {}
    for s in fetch_sleep_range(hc, tz, start, nxt, misses):
        sleep_s[s["end_date"]] = max(sleep_s.get(s["end_date"], 0), s["asleep"])
    h["sleep_minutes"] = sleep_s
    # тренировки: одним запросом за весь период
    a = start.isoformat()
    b = nxt.isoformat()
    flt = (f'exercise.interval.civil_start_time >= "{a}" '
           f'AND exercise.interval.civil_start_time < "{b}"')
    ex_list = []
    for p in (safe(lambda: hc.list_points("exercise", flt), misses, "тренировки") or []):
        ex = p.get("exercise") or {}
        interval = ex.get("interval") or {}
        minutes = None
        if interval.get("startTime") and interval.get("endTime"):
            minutes = round((parse_iso(interval["endTime"]) -
                             parse_iso(interval["startTime"])).total_seconds() / 60)
        name = (find_str(ex, ("activitytype",), ("exercisetype",), ("name",),
                         ("type",)) or "Активность")
        d_local = (parse_iso(interval["startTime"]).astimezone(tz).date().isoformat()
                   if interval.get("startTime") else None)
        ex_list.append({"date": d_local,
                        "name": ACTIVITY_RU.get(name.upper(),
                                                name.replace("_", " ").capitalize()),
                        "minutes": minutes,
                        "kcal": find_num(ex, ("calor",), ("energy",), ("kcal",))})
    h["exercises_28d"] = ex_list

    # архив из репо: расширяем серии вглубь до 90 дней
    archive = load_history_store(store_dir)
    for metric, series in archive.items():
        if metric in h and isinstance(h[metric], dict):
            for k, v in series.items():
                h[metric].setdefault(k, v)
    return h, misses


def _avg(vals):
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


def week_stats(series: dict, end: date, offset_weeks: int = 0):
    """Среднее за неделю, заканчивающуюся end - 7*offset_weeks."""
    e = end - timedelta(days=7 * offset_weeks)
    vals = [series.get((e - timedelta(days=i)).isoformat()) for i in range(7)]
    return _avg(vals)


def wk_line(emoji, label, series, end, fmt, better="up"):
    cur, prev = week_stats(series, end), week_stats(series, end, 1)
    if cur is None:
        return None
    line = f"{emoji} {label}: <b>{fmt(cur)}</b>"
    if prev:
        d = (cur - prev) / prev * 100
        if abs(d) >= 2:
            line += f" ({'↑' if d > 0 else '↓'}{abs(d):.0f}%)"
        else:
            line += " (≈)"
    return line


def build_weekly(hc: Health, tz: ZoneInfo, day: date) -> tuple:
    history, misses = collect_history(hc, tz, day)
    end = day
    start_w = day - timedelta(days=6)
    lines = [f"📅 <b>Недельный отчёт — {start_w.day} {MONTHS_RU[start_w.month]} — "
             f"{day.day} {MONTHS_RU[day.month]}</b>",
             "", "Средние за неделю (в скобках — к прошлой неделе):"]
    for args in [
        ("😴", "Сон", history["sleep_minutes"], fmt_hm),
        ("🚶", "Шаги/день", history["steps"], fmt_int),
        ("🔥", "Калории/день", history["kcal"], lambda v: fmt_int(v) + " ккал"),
        ("⚡", "Активные зоны/день", history["active_zone_minutes"],
         lambda v: fmt_int(v) + " мин"),
        ("❤️", "Пульс покоя", history["resting_heart_rate"], fmt_int),
        ("📈", "HRV", history["hrv_ms"], lambda v: fmt_int(v) + " мс"),
    ]:
        line = wk_line(args[0], args[1], args[2], end, args[3])
        if line:
            lines.append(line)

    week_ex = [e for e in history["exercises_28d"]
               if e["date"] and start_w.isoformat() <= e["date"] <= day.isoformat()]
    if week_ex:
        total_min = sum(e["minutes"] or 0 for e in week_ex)
        lines += ["", f"🏋️ Тренировок: <b>{len(week_ex)}</b> ({fmt_int(total_min)} мин)"]
        for e in week_ex:
            item = f"  • {e['date'][8:10]}.{e['date'][5:7]} {html.escape(e['name'])}"
            if e["minutes"]:
                item += f" — {e['minutes']} мин"
            lines.append(item)

    if misses:
        lines += ["", f"⚠️ Нет данных: {', '.join(dict.fromkeys(misses))}"]
    return "\n".join(lines), history


def build_ask(hc: Health, tz: ZoneInfo, question: str) -> str:
    history, _ = collect_history(hc, tz, datetime.now(tz).date())
    prompt = (
        "Ты ассистент по фитнес-данным пользователя (Fitbit). Ниже его данные "
        "(JSON, серии по датам, глубина до 90 дней) и вопрос.\n\n"
        f"ДАННЫЕ:\n{json.dumps(history, ensure_ascii=False)}\n\n"
        f"ВОПРОС: {question}\n\n"
        "Ответь по-русски, по делу, компактно (до 6 предложений или короткий "
        "список строк с эмодзи). Обычный текст без markdown. Если в данных нет "
        "ответа — так и скажи. Наблюдения, не медицинские диагнозы."
    )
    cmd = os.environ.get("CLAUDE_BIN", "claude")
    model = os.environ.get("CLAUDE_MODEL", "")
    attempts = [["--model", model]] if model else []
    attempts.append([])
    for extra in attempts:
        try:
            r = subprocess.run([cmd, "-p", *extra], input=prompt,
                               capture_output=True, text=True, timeout=240)
        except (OSError, subprocess.TimeoutExpired) as e:
            log(f"[warn] ask ({extra or 'default'}): {e}")
            continue
        out = (r.stdout or "").strip()
        if r.returncode == 0 and out:
            return out
        log(f"[warn] ask claude exit={r.returncode} stdout={out[:200]!r}")
    return "Не смог получить ответ от модели, попробуй позже."


def update_history_store(store_dir: str, history: dict):
    """Дописывает серии метрик в data/YYYY-MM.json (идемпотентно, с бэкфиллом)."""
    os.makedirs(store_dir, exist_ok=True)
    per_month = {}

    def put(day_key: str, metric: str, value):
        month = day_key[:7]
        if month not in per_month:
            path = os.path.join(store_dir, f"{month}.json")
            try:
                per_month[month] = json.load(open(path, encoding="utf-8"))
            except (OSError, ValueError):
                per_month[month] = {}
        per_month[month].setdefault(day_key, {})[metric] = value

    for metric, series in history.items():
        if isinstance(series, dict):
            for k, v in series.items():
                if isinstance(k, str) and len(k) == 10 and k[4] == "-":
                    put(k, metric, v)
    ex = history.get("exercises_today")
    if ex:
        put(history["report_day"], "exercises", ex)
    for month, data in per_month.items():
        path = os.path.join(store_dir, f"{month}.json")
        json.dump(data, open(path, "w", encoding="utf-8"),
                  ensure_ascii=False, sort_keys=True, indent=0)
    log(f"история сохранена: {', '.join(sorted(per_month))}")


def load_history_store(store_dir: str, max_days: int = 90) -> dict:
    """Слитые серии из data/*.json не старше max_days: {metric: {date: value}}."""
    merged = {}
    if not os.path.isdir(store_dir):
        return merged
    cutoff = (date.today() - timedelta(days=max_days)).isoformat()
    for name in sorted(os.listdir(store_dir)):
        if not name.endswith(".json"):
            continue
        try:
            data = json.load(open(os.path.join(store_dir, name), encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for day_key, metrics in data.items():
            if day_key < cutoff or not isinstance(metrics, dict):
                continue
            for m, v in metrics.items():
                if m != "exercises":
                    merged.setdefault(m, {})[day_key] = v
    return merged


CHART_PANELS = [
    ("sleep_minutes", "Сон, ч", 1 / 60),
    ("resting_heart_rate", "Пульс покоя, уд/мин", 1),
    ("hrv_ms", "HRV, мс", 1),
    ("steps", "Шаги, тыс.", 1 / 1000),
]


def render_chart(history: dict, path: str, days: int = 14) -> bool:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
    except ImportError:
        log("[warn] matplotlib не установлен — график пропущен")
        return False
    panels = [(k, t, f) for k, t, f in CHART_PANELS
              if isinstance(history.get(k), dict) and history[k]]
    if not panels:
        return False
    end = date.fromisoformat(history.get("report_day", date.today().isoformat()))
    xs = [end - timedelta(days=i) for i in range(days - 1, -1, -1)]
    fig, axes = plt.subplots(len(panels), 1, figsize=(7.2, 1.75 * len(panels)),
                             sharex=True)
    if len(panels) == 1:
        axes = [axes]
    for ax, (key, title, factor) in zip(axes, panels):
        s = history[key]
        ys = [(s.get(d.isoformat()) or float("nan")) * factor
              if s.get(d.isoformat()) is not None else float("nan") for d in xs]
        ax.plot(xs, ys, marker="o", markersize=3.5, linewidth=1.6, color="#2563eb")
        vals = [y for y in ys if y == y]
        if vals:
            avg = sum(vals) / len(vals)
            ax.axhline(avg, linestyle="--", linewidth=1, color="#94a3b8")
        ax.set_title(title, fontsize=9, loc="left")
        ax.grid(alpha=0.25)
        ax.tick_params(labelsize=8)
    axes[-1].xaxis.set_major_formatter(mdates.DateFormatter("%d.%m"))
    fig.autofmt_xdate(rotation=0, ha="center")
    fig.tight_layout()
    fig.savefig(path, dpi=140)
    plt.close(fig)
    return True


# --------------------------------------------------------------------- telegram

def send_telegram(text: str):
    token, chat_id = env("TELEGRAM_BOT_TOKEN"), env("TELEGRAM_CHAT_ID")
    body = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                       "disable_web_page_preview": True}).encode()
    http_json("POST", f"https://api.telegram.org/bot{token}/sendMessage",
              headers={"Content-Type": "application/json"}, body=body)


def send_telegram_photo(path: str, caption: str = ""):
    token, chat_id = env("TELEGRAM_BOT_TOKEN"), env("TELEGRAM_CHAT_ID")
    boundary = "----hb" + os.urandom(12).hex()
    parts = []
    for name, value in (("chat_id", chat_id), ("caption", caption)):
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; "
                     f"name=\"{name}\"\r\n\r\n{value}\r\n".encode())
    with open(path, "rb") as f:
        img = f.read()
    parts.append(f"--{boundary}\r\nContent-Disposition: form-data; "
                 f"name=\"photo\"; filename=\"chart.png\"\r\n"
                 f"Content-Type: image/png\r\n\r\n".encode() + img + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    http_json("POST", f"https://api.telegram.org/bot{token}/sendPhoto",
              headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
              body=b"".join(parts))


# ------------------------------------------------------------------------- main

def main() -> int:
    global DEBUG
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mode", choices=["morning", "evening", "weekly", "auto"],
                    default="auto")
    ap.add_argument("--date", help="YYYY-MM-DD: отчётный день (override)")
    ap.add_argument("--dry-run", action="store_true", help="напечатать, не отправлять")
    ap.add_argument("--debug", action="store_true", help="дампить сырые ответы API в stderr")
    ap.add_argument("--test-telegram", action="store_true",
                    help="только проверить отправку в Telegram")
    ap.add_argument("--analyze", action="store_true",
                    help="добавить анализ от Claude (нужен claude CLI и токен)")
    ap.add_argument("--ask", metavar="ВОПРОС",
                    help="ответить на вопрос по данным и отправить в Telegram")
    ap.add_argument("--chart", metavar="PNG",
                    help="приложить график за 14 (daily) / 28 (weekly) дней")
    ap.add_argument("--save-history", metavar="DIR",
                    help="дописать метрики в хранилище (data/YYYY-MM.json)")
    args = ap.parse_args()
    DEBUG = args.debug

    # Windows-консоль может быть в cp1251 — эмодзи в сводке её роняют
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass

    if args.test_telegram:
        send_telegram("✅ Тест: бот сводок здоровья подключён.")
        print("Отправлено.")
        return 0

    tz = ZoneInfo(os.environ.get("TZ_NAME", "Europe/Moscow"))
    now = datetime.now(tz)

    mode = args.mode
    if mode == "auto":
        mode = "morning" if 4 <= now.hour < 14 else "evening"

    if args.date:
        day = date.fromisoformat(args.date)
    elif mode == "weekly":
        # запуск в ~00:14 пн -> неделя, закончившаяся воскресеньем
        day = now.date() if now.hour >= 14 else now.date() - timedelta(days=1)
    elif mode == "morning":
        day = now.date()  # сон, закончившийся сегодня утром
    else:
        # запуск в ~00:00 -> отчёт за только что закончившийся день
        day = now.date() if now.hour >= 14 else now.date() - timedelta(days=1)

    hc = Health(get_access_token())

    if args.ask:
        answer = build_ask(hc, tz, args.ask)
        text = "❓ <i>" + html.escape(args.ask) + "</i>\n\n" + html.escape(answer)
        if args.dry_run:
            print(text)
        else:
            send_telegram(text)
            print("Ответ отправлен.")
        return 0

    if mode == "weekly":
        text, history = build_weekly(hc, tz, day)
    elif mode == "morning":
        text, history = build_morning(hc, tz, day)
    else:
        text, history = build_evening(hc, tz, day)

    if args.save_history:
        try:
            update_history_store(args.save_history, history)
        except Exception as e:  # noqa: BLE001
            log(f"[warn] история не сохранена: {e}")

    if args.analyze:
        analysis = build_analysis(history, text, weekly=(mode == "weekly"))
        if analysis:
            text += "\n\n🧠 <b>Анализ</b>\n" + html.escape(analysis)

    chart_ok = False
    if args.chart:
        try:
            chart_ok = render_chart(history, args.chart,
                                    days=28 if mode == "weekly" else 14)
        except Exception as e:  # noqa: BLE001
            log(f"[warn] график не построен: {e}")

    if args.dry_run:
        print(text)
        if chart_ok:
            print(f"[график: {args.chart}]")
    else:
        send_telegram(text)
        if chart_ok:
            send_telegram_photo(args.chart,
                                "Динамика за " + ("28" if mode == "weekly" else "14")
                                + " дней")
        print(f"Сводка ({mode}, {day}) отправлена.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
