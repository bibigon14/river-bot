#!/usr/bin/env python3
"""
RiverBot — a Telegram bot that reports water level, flow, and temperature
from USGS gauges (waterservices.usgs.gov). Supports English and Russian.

Features:
  /now       — current data for all sites in USGS_SITES (.env)
  /river     — pick a river and reach with buttons, view charts and history
  /language  — switch between English and Russian
  schedule   — sends a /now-style report automatically once a day

Configuration lives in .env (see .env.example).
"""

from __future__ import annotations

import io
import json
import logging
import os
import time
from datetime import date, datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

import matplotlib
matplotlib.use("Agg")  # no display, render straight to a buffer/file
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import requests
from dotenv import load_dotenv
from telegram.error import NetworkError
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
# Default sites: American River at Fair Oaks + Sacramento River at Freeport.
# Comma-separated list of USGS site numbers: "11446500,11447650"
USGS_SITES = [s.strip() for s in os.getenv("USGS_SITES", "11446500,11447650").split(",") if s.strip()]
SCHEDULE_TIME = os.getenv("SCHEDULE_TIME", "07:00")  # HH:MM, local time
TIMEZONE = os.getenv("TIMEZONE", "America/Los_Angeles")

# Default UI language for chats that haven't picked one yet ("en" or "ru").
DEFAULT_LANGUAGE = os.getenv("DEFAULT_LANGUAGE", "en").lower()
if DEFAULT_LANGUAGE not in ("en", "ru"):
    DEFAULT_LANGUAGE = "en"

# Water temperature threshold (°F) below which we flag conditions as
# comfortable for salmon migration.
SALMON_TEMP_THRESHOLD_F = float(os.getenv("SALMON_TEMP_THRESHOLD_F", "65"))

# Data older than this many hours is treated as unavailable (a sensor can
# stay silent for months/years even if the site as a whole is still active).
MAX_DATA_AGE_HOURS = 6

# How many years back to compare against (button "Compare with past years").
HISTORY_YEARS_BACK = [1, 3, 5]

# Window width for the year-over-year chart: +/- this many days around today
# (so a trend is visible instead of a single data point).
HISTORY_WINDOW_DAYS = 15

# Registry of rivers and their reaches (gauges) for the /river command.
# Format: "River name": [("Reach label", "USGS site number"), ...]
# Add your own sites — look up numbers at https://waterdata.usgs.gov
RIVERS = {
    "American River": [
        ("Fair Oaks", "11446500"),
    ],
    "Sacramento River": [
        ("Freeport", "11447650"),
        ("Verona", "11425500"),
    ],
}

IV_URL = "https://waterservices.usgs.gov/nwis/iv/"   # instantaneous values
DV_URL = "https://waterservices.usgs.gov/nwis/dv/"   # historical daily means

# USGS parameter codes
PARAM_DISCHARGE = "00060"    # flow, cubic feet per second
PARAM_GAGE_HEIGHT = "00065"  # gage height, feet
PARAM_TEMPERATURE = "00010"  # water temperature, °C

PARAM_UNITS = {
    PARAM_DISCHARGE: "ft³/s",
    PARAM_GAGE_HEIGHT: "ft",
    PARAM_TEMPERATURE: "°C",
}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# i18n
# --------------------------------------------------------------------------

LANG_STORE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lang_store.json")


def _load_lang_store() -> dict:
    try:
        with open(LANG_STORE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


_lang_store: dict = _load_lang_store()


def _save_lang_store() -> None:
    try:
        with open(LANG_STORE_PATH, "w", encoding="utf-8") as f:
            json.dump(_lang_store, f)
    except OSError:
        logger.exception("Failed to save language preferences")


def get_lang(chat_id) -> str:
    return _lang_store.get(str(chat_id), DEFAULT_LANGUAGE)


def set_lang(chat_id, lang: str) -> None:
    _lang_store[str(chat_id)] = lang
    _save_lang_store()


PARAM_LABELS = {
    "en": {
        PARAM_DISCHARGE: "Flow",
        PARAM_GAGE_HEIGHT: "Level",
        PARAM_TEMPERATURE: "Temperature",
    },
    "ru": {
        PARAM_DISCHARGE: "Расход",
        PARAM_GAGE_HEIGHT: "Уровень",
        PARAM_TEMPERATURE: "Температура",
    },
}

T = {
    "en": {
        "greeting": (
            "Hi! I track water level, flow, and temperature (USGS data).\n\n"
            "/now — current data for all default sites\n"
            "/river — pick a river and reach, view charts and history\n"
            "/language — switch between English and Russian\n\n"
            "Your chat_id: {chat_id}"
        ),
        "choose_river": "Choose a river:",
        "choose_reach": "{river} — choose a reach:",
        "back_rivers": "« Back to rivers",
        "btn_compare": "📊 Compare with past years",
        "btn_trend": "📉 Year-over-year trend (chart)",
        "btn_chart7": "📈 7-day chart",
        "no_fresh_data": "no fresh data",
        "salmon_note": "🐟 Temperature ≤ {threshold:.0f}°F — comfortable for salmon migration",
        "updated": "Updated: {dt}",
        "err_data": "⚠️ Error fetching data for {label} ({err})",
        "err_history": "⚠️ Couldn't get historical data for {label} ({err})",
        "err_chart": "⚠️ Couldn't build the chart for {label} ({err})",
        "err_trend_chart": "⚠️ Couldn't build the year-over-year chart for {label} ({err})",
        "no_data_chart": "No data available for a chart for site {label}.",
        "no_data_trend": "No historical data available for a chart for site {label}.",
        "generic_error": "⚠️ Something went wrong handling that button. Check the bot log for details.",
        "history_title": "📊 *{site_name}* — compared to past years ({date}):",
        "history_no_data": "{date}: no data",
        "no_data_word": "no data",
        "history_error": "{date}: error fetching data ({err})",
        "history_unavailable": "\nHistorical data isn't available for this site via nwis/dv.",
        "history_flow": "flow {v} ft³/s",
        "history_temp": "temp {c:.1f}°C/{f:.1f}°F",
        "history_level": "level {v} ft",
        "chart7_caption": "{site_name} — flow and temperature, last {days} days",
        "chart7_title": "{site_name} — last {days} days",
        "chart7_ylabel_flow": "Flow, ft³/s",
        "chart7_ylabel_temp": "Temperature, °F",
        "chart7_legend_flow": "Flow (ft³/s)",
        "chart7_legend_temp": "Temperature (°F)",
        "trend_title": "{label} — flow by year ({date} ± {window} d.)",
        "trend_xlabel": "Days from {date}",
        "trend_ylabel": "Flow, ft³/s",
        "trend_now_suffix": " (now)",
        "trend_caption": "{label} — flow, {date} ± {window} d., by year",
        "lang_prompt": "Choose your language:",
        "lang_set": "Language set to English.",
        "cmd_start": "Greeting and help",
        "cmd_now": "Current data for all sites",
        "cmd_river": "Choose a river and reach",
        "cmd_language": "Change language",
    },
    "ru": {
        "greeting": (
            "Привет! Я слежу за уровнем, расходом и температурой воды (данные USGS).\n\n"
            "/now — текущие данные по всем постам по умолчанию\n"
            "/river — выбрать реку и участок, посмотреть график и историю\n"
            "/language — сменить язык (английский/русский)\n\n"
            "Ваш chat_id: {chat_id}"
        ),
        "choose_river": "Выберите реку:",
        "choose_reach": "{river} — выберите участок:",
        "back_rivers": "« К списку рек",
        "btn_compare": "📊 Сравнить с прошлыми годами",
        "btn_trend": "📉 Тренд по годам (график)",
        "btn_chart7": "📈 График за 7 дней",
        "no_fresh_data": "нет свежих данных",
        "salmon_note": "🐟 Температура ≤ {threshold:.0f}°F — комфортно для миграции лосося",
        "updated": "Обновлено: {dt}",
        "err_data": "⚠️ Ошибка получения данных для {label} ({err})",
        "err_history": "⚠️ Не удалось получить исторические данные для {label} ({err})",
        "err_chart": "⚠️ Не удалось построить график для {label} ({err})",
        "err_trend_chart": "⚠️ Не удалось построить график по годам для {label} ({err})",
        "no_data_chart": "Нет данных для графика по посту {label}.",
        "no_data_trend": "Нет исторических данных для графика по посту {label}.",
        "generic_error": "⚠️ Произошла ошибка при обработке кнопки. Подробности — в логе бота.",
        "history_title": "📊 *{site_name}* — сравнение с прошлыми годами ({date}):",
        "history_no_data": "{date}: нет данных",
        "no_data_word": "нет данных",
        "history_error": "{date}: ошибка получения данных ({err})",
        "history_unavailable": "\nИсторические данные для этого поста недоступны через nwis/dv.",
        "history_flow": "расход {v} ft³/s",
        "history_temp": "темп. {c:.1f}°C/{f:.1f}°F",
        "history_level": "уровень {v} ft",
        "chart7_caption": "{site_name} — расход и температура, последние {days} дн.",
        "chart7_title": "{site_name} — последние {days} дн.",
        "chart7_ylabel_flow": "Расход, ft³/s",
        "chart7_ylabel_temp": "Температура, °F",
        "chart7_legend_flow": "Расход (ft³/s)",
        "chart7_legend_temp": "Температура (°F)",
        "trend_title": "{label} — расход по годам ({date} ± {window} дн.)",
        "trend_xlabel": "Дни от {date}",
        "trend_ylabel": "Расход, ft³/s",
        "trend_now_suffix": " (сейчас)",
        "trend_caption": "{label} — расход, {date} ± {window} дн., по годам",
        "lang_prompt": "Выберите язык:",
        "lang_set": "Язык переключён на русский.",
        "cmd_start": "Приветствие и справка",
        "cmd_now": "Текущие данные по всем постам",
        "cmd_river": "Выбрать реку и участок",
        "cmd_language": "Сменить язык",
    },
}


def tr(lang: str, key: str, **kwargs) -> str:
    text = T.get(lang, T["en"]).get(key) or T["en"][key]
    return text.format(**kwargs) if kwargs else text


# --------------------------------------------------------------------------
# USGS: current data
# --------------------------------------------------------------------------

def _is_fresh(dt_str: str | None) -> bool:
    if not dt_str:
        return False
    try:
        dt = datetime.fromisoformat(dt_str)
    except ValueError:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    age_hours = (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds() / 3600
    return age_hours <= MAX_DATA_AGE_HOURS


def fetch_site_data(site: str) -> dict:
    """Fetches current values from USGS for one site.

    Returns a dict like:
      {
        "site_name": "AMERICAN R A FAIR OAKS CA",
        "values": {"00060": ("4040", "ft³/s", "2026-07-04T18:00:00.000-07:00"), ...},
        "datetime": "2026-07-04T18:00:00.000-07:00",  # freshest timestamp among params
      }
    """
    params = {
        "sites": site,
        "parameterCd": ",".join(PARAM_UNITS.keys()),
        "siteStatus": "all",
        "format": "json",
    }
    resp = requests.get(IV_URL, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    series = data.get("value", {}).get("timeSeries", [])
    if not series:
        raise ValueError(f"No data for site {site}")

    site_name = series[0]["sourceInfo"]["siteName"]
    values = {}
    latest_dt = None

    for ts in series:
        code = ts["variable"]["variableCode"][0]["value"]
        vals = ts.get("values", [{}])[0].get("value", [])
        if not vals:
            continue
        v = vals[0]
        dt_str = v.get("dateTime")
        if not _is_fresh(dt_str):
            continue  # sensor has been silent longer than MAX_DATA_AGE_HOURS
        unit = PARAM_UNITS.get(code, "")
        values[code] = (v["value"], unit, dt_str)
        if latest_dt is None or (dt_str and dt_str > latest_dt):
            latest_dt = dt_str

    return {"site_name": site_name, "site_no": site, "values": values, "datetime": latest_dt}


def format_message(info: dict, lang: str) -> str:
    labels = PARAM_LABELS.get(lang, PARAM_LABELS["en"])
    lines = [f"🌊 *{info['site_name']}* (USGS {info['site_no']})"]
    temp_f = None
    for code in (PARAM_DISCHARGE, PARAM_GAGE_HEIGHT, PARAM_TEMPERATURE):
        label = labels[code]
        if code in info["values"]:
            value, unit, _dt = info["values"][code]
            if code == PARAM_TEMPERATURE:
                try:
                    temp_c = float(value)
                    temp_f = temp_c * 9 / 5 + 32
                    lines.append(f"{label}: {temp_c:.1f}°C / {temp_f:.1f}°F")
                except ValueError:
                    lines.append(f"{label}: {value} {unit}")
            else:
                lines.append(f"{label}: {value} {unit}")
        else:
            lines.append(f"{label}: {tr(lang, 'no_fresh_data')}")
    if temp_f is not None and temp_f <= SALMON_TEMP_THRESHOLD_F:
        lines.append(tr(lang, "salmon_note", threshold=SALMON_TEMP_THRESHOLD_F))
    if info.get("datetime"):
        lines.append(f"_{tr(lang, 'updated', dt=info['datetime'])}_")
    return "\n".join(lines)


def build_report(lang: str) -> str:
    chunks = []
    for site in USGS_SITES:
        try:
            info = fetch_site_data(site)
            chunks.append(format_message(info, lang))
        except Exception as e:
            logger.exception("Error fetching data for site %s", site)
            chunks.append(tr(lang, "err_data", label=site, err=e))
    return "\n\n".join(chunks)


# --------------------------------------------------------------------------
# USGS: historical daily means (nwis/dv) — for comparing with past years
# --------------------------------------------------------------------------

def fetch_daily_mean(site: str, day: date) -> dict:
    """Returns daily-mean flow/temperature/level values for a given date.

    The dv service keeps data back to when the site was established (often
    decades), unlike iv (only the last ~120 days).
    """
    day_str = day.isoformat()
    params = {
        "sites": site,
        "parameterCd": ",".join(PARAM_UNITS.keys()),
        "startDT": day_str,
        "endDT": day_str,
        "statCd": "00003",  # Mean
        "siteStatus": "all",
        "format": "json",
    }
    resp = requests.get(DV_URL, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    series = data.get("value", {}).get("timeSeries", [])
    values = {}
    for ts in series:
        code = ts["variable"]["variableCode"][0]["value"]
        vals = ts.get("values", [{}])[0].get("value", [])
        if not vals:
            continue
        values[code] = vals[0]["value"]
    return values


def build_history_comparison(site: str, site_name: str, lang: str) -> str:
    today = date.today()
    lines = [tr(lang, "history_title", site_name=site_name, date=today.strftime("%d.%m")), ""]
    any_data = False
    for years in HISTORY_YEARS_BACK:
        try:
            past_day = today.replace(year=today.year - years)
        except ValueError:
            past_day = today.replace(year=today.year - years, day=28)  # Feb 29 etc.
        date_str = past_day.strftime("%d.%m.%Y")
        try:
            values = fetch_daily_mean(site, past_day)
        except Exception as e:
            logger.exception("Error fetching history for %s (%s years back)", site, years)
            lines.append(tr(lang, "history_error", date=date_str, err=e))
            continue
        if not values:
            lines.append(tr(lang, "history_no_data", date=date_str))
            continue
        any_data = True
        parts = []
        if PARAM_DISCHARGE in values:
            parts.append(tr(lang, "history_flow", v=values[PARAM_DISCHARGE]))
        if PARAM_TEMPERATURE in values:
            try:
                c = float(values[PARAM_TEMPERATURE])
                parts.append(tr(lang, "history_temp", c=c, f=c * 9 / 5 + 32))
            except ValueError:
                pass
        if PARAM_GAGE_HEIGHT in values:
            parts.append(tr(lang, "history_level", v=values[PARAM_GAGE_HEIGHT]))
        lines.append(f"{date_str}: " + (", ".join(parts) if parts else tr(lang, "no_data_word")))
    if not any_data:
        lines.append(tr(lang, "history_unavailable"))
    return "\n".join(lines)


def fetch_daily_series(site: str, code: str, start: date, end: date) -> list[tuple[date, float]]:
    """Daily-mean values of one parameter over a date range (nwis/dv)."""
    params = {
        "sites": site,
        "parameterCd": code,
        "startDT": start.isoformat(),
        "endDT": end.isoformat(),
        "statCd": "00003",  # Mean
        "siteStatus": "all",
        "format": "json",
    }
    resp = requests.get(DV_URL, params=params, timeout=20)
    resp.raise_for_status()
    data = resp.json()

    points: list[tuple[date, float]] = []
    for ts in data.get("value", {}).get("timeSeries", []):
        for v in ts.get("values", [{}])[0].get("value", []):
            try:
                d = datetime.fromisoformat(v["dateTime"]).date()
                points.append((d, float(v["value"])))
            except (ValueError, KeyError):
                continue
    return points


def build_history_chart(site: str, label: str, lang: str) -> tuple[bytes, str] | None:
    """Builds a flow chart over a +/- HISTORY_WINDOW_DAYS window around today,
    with one line per year (current + HISTORY_YEARS_BACK) — so a trend is
    visible over roughly a month, not just a single day's value."""
    today = date.today()
    years_list = [0] + HISTORY_YEARS_BACK  # 0 = current year

    fig, ax = plt.subplots(figsize=(8, 4.5), dpi=150)
    color_cycle = ["#1e88b8", "#e2624f", "#8a9a3a", "#9467bd", "#c9432f"]
    any_data = False

    for i, years_back in enumerate(years_list):
        try:
            center = today.replace(year=today.year - years_back)
        except ValueError:
            center = today.replace(year=today.year - years_back, day=28)
        start = center - timedelta(days=HISTORY_WINDOW_DAYS)
        end = center + timedelta(days=HISTORY_WINDOW_DAYS)
        try:
            points = fetch_daily_series(site, PARAM_DISCHARGE, start, end)
        except Exception:
            logger.exception("Error fetching year-over-year chart for %s (%s years back)", site, years_back)
            continue
        if not points:
            continue
        points.sort(key=lambda p: p[0])
        offsets = [(d - center).days for d, _ in points]
        values = [v for _, v in points]
        is_current = years_back == 0
        ax.plot(
            offsets, values,
            color=color_cycle[i % len(color_cycle)],
            linewidth=2.6 if is_current else 1.6,
            linestyle="-" if is_current else "--",
            label=f"{center.year}" + (tr(lang, "trend_now_suffix") if is_current else ""),
        )
        any_data = True

    if not any_data:
        plt.close(fig)
        return None

    today_str = today.strftime("%d.%m")
    ax.axvline(0, color="#888888", linewidth=1, linestyle=":")
    ax.set_xlabel(tr(lang, "trend_xlabel", date=today_str))
    ax.set_ylabel(tr(lang, "trend_ylabel"))
    ax.set_title(tr(lang, "trend_title", label=label, date=today_str, window=HISTORY_WINDOW_DAYS))
    ax.legend(fontsize=8)
    ax.grid(alpha=0.25)
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue(), label


# --------------------------------------------------------------------------
# Chart for the last N days (flow + temperature)
# --------------------------------------------------------------------------

CHART_PERIOD_DAYS = 7


def fetch_period_series(site: str, codes: list[str], period_days: int) -> dict:
    """Returns {param_code: [(datetime, float), ...]} for the last period_days
    days. The USGS iv service keeps roughly the last ~120 days."""
    params = {
        "sites": site,
        "parameterCd": ",".join(codes),
        "period": f"P{period_days}D",
        "siteStatus": "all",
        "format": "json",
    }
    resp = requests.get(IV_URL, params=params, timeout=20)
    resp.raise_for_status()
    data = resp.json()

    series = data.get("value", {}).get("timeSeries", [])
    result: dict[str, list[tuple[datetime, float]]] = {}
    site_name = series[0]["sourceInfo"]["siteName"] if series else None
    for ts in series:
        code = ts["variable"]["variableCode"][0]["value"]
        points = []
        for v in ts.get("values", [{}])[0].get("value", []):
            try:
                dt = datetime.fromisoformat(v["dateTime"])
                points.append((dt, float(v["value"])))
            except (ValueError, KeyError):
                continue
        result[code] = points
    return {"site_name": site_name, "series": result}


def build_chart(site: str, label: str, lang: str) -> tuple[bytes, str] | None:
    """Builds a PNG chart of flow and temperature over CHART_PERIOD_DAYS days.

    Returns (png_bytes, site_name), or None if there's no data at all.
    """
    data = fetch_period_series(site, [PARAM_DISCHARGE, PARAM_TEMPERATURE], CHART_PERIOD_DAYS)
    series = data["series"]
    flow = series.get(PARAM_DISCHARGE, [])
    temp = series.get(PARAM_TEMPERATURE, [])
    if not flow and not temp:
        return None

    site_name = data["site_name"] or label

    fig, ax1 = plt.subplots(figsize=(8, 4.5), dpi=150)

    if flow:
        xs, ys = zip(*flow)
        ax1.plot(xs, ys, color="tab:blue", label=tr(lang, "chart7_legend_flow"))
        ax1.set_ylabel(tr(lang, "chart7_ylabel_flow"), color="tab:blue")
        ax1.tick_params(axis="y", labelcolor="tab:blue")

    if temp:
        xs_t, ys_c = zip(*temp)
        ys_f = [c * 9 / 5 + 32 for c in ys_c]
        ax2 = ax1.twinx()
        ax2.plot(xs_t, ys_f, color="tab:red", label=tr(lang, "chart7_legend_temp"))
        ax2.set_ylabel(tr(lang, "chart7_ylabel_temp"), color="tab:red")
        ax2.tick_params(axis="y", labelcolor="tab:red")

    ax1.set_title(tr(lang, "chart7_title", site_name=site_name, days=CHART_PERIOD_DAYS))
    ax1.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m"))
    fig.autofmt_xdate()
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue(), site_name


# --------------------------------------------------------------------------
# Telegram: commands and the river/reach selection menu
# --------------------------------------------------------------------------

def _river_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(river, callback_data=f"river:{river}")]
        for river in RIVERS
    ])


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lang = get_lang(update.effective_chat.id)
    await update.message.reply_text(tr(lang, "greeting", chat_id=update.effective_chat.id))


async def now_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lang = get_lang(update.effective_chat.id)
    await update.message.reply_text(build_report(lang), parse_mode="Markdown")


async def river_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lang = get_lang(update.effective_chat.id)
    await update.message.reply_text(tr(lang, "choose_river"), reply_markup=_river_keyboard(lang))


async def language_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🇬🇧 English", callback_data="lang:en")],
        [InlineKeyboardButton("🇷🇺 Русский", callback_data="lang:ru")],
    ])
    lang = get_lang(update.effective_chat.id)
    await update.message.reply_text(tr(lang, "lang_prompt"), reply_markup=keyboard)


async def _safe_edit(query, text: str, keyboard: InlineKeyboardMarkup, parse_mode: str | None = None) -> None:
    """edit_message_text with a fallback: if Markdown breaks or the edit isn't
    possible, retry without formatting, and if that also fails, send a new
    message instead of failing silently."""
    try:
        await query.edit_message_text(text, parse_mode=parse_mode, reply_markup=keyboard)
    except Exception:
        logger.exception("edit_message_text failed, retrying without parse_mode")
        try:
            await query.edit_message_text(text, reply_markup=keyboard)
        except Exception:
            logger.exception("edit_message_text failed again, sending a new message")
            await query.message.reply_text(text, reply_markup=keyboard)


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data
    lang = get_lang(update.effective_chat.id)

    try:
        if data.startswith("lang:"):
            new_lang = data.split(":", 1)[1]
            if new_lang not in T:
                new_lang = "en"
            set_lang(update.effective_chat.id, new_lang)
            await _safe_edit(query, tr(new_lang, "lang_set"), InlineKeyboardMarkup([]))

        elif data.startswith("river:"):
            river = data.split(":", 1)[1]
            sections = RIVERS.get(river, [])
            keyboard = [
                [InlineKeyboardButton(label, callback_data=f"site:{site_no}:{label}")]
                for label, site_no in sections
            ]
            keyboard.append([InlineKeyboardButton(tr(lang, "back_rivers"), callback_data="back:rivers")])
            await _safe_edit(query, tr(lang, "choose_reach", river=river), InlineKeyboardMarkup(keyboard))

        elif data.startswith("site:"):
            _, site_no, label = data.split(":", 2)
            try:
                info = fetch_site_data(site_no)
                text = format_message(info, lang)
            except Exception as e:
                logger.exception("Error fetching data for site %s", site_no)
                text = tr(lang, "err_data", label=label, err=e)
            keyboard = [
                [InlineKeyboardButton(tr(lang, "btn_compare"), callback_data=f"hist:{site_no}:{label}")],
                [InlineKeyboardButton(tr(lang, "btn_trend"), callback_data=f"histchart:{site_no}:{label}")],
                [InlineKeyboardButton(tr(lang, "btn_chart7"), callback_data=f"chart:{site_no}:{label}")],
                [InlineKeyboardButton(tr(lang, "back_rivers"), callback_data="back:rivers")],
            ]
            await _safe_edit(query, text, InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

        elif data.startswith("hist:"):
            _, site_no, label = data.split(":", 2)
            try:
                text = build_history_comparison(site_no, label, lang)
            except Exception as e:
                logger.exception("Error comparing with past years for %s", site_no)
                text = tr(lang, "err_history", label=label, err=e)
            keyboard = [[InlineKeyboardButton(tr(lang, "back_rivers"), callback_data="back:rivers")]]
            await _safe_edit(query, text, InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

        elif data.startswith("histchart:"):
            _, site_no, label = data.split(":", 2)
            try:
                result = build_history_chart(site_no, label, lang)
            except Exception as e:
                logger.exception("Error building year-over-year chart for %s", site_no)
                await query.message.reply_text(tr(lang, "err_trend_chart", label=label, err=e))
                return
            if result is None:
                await query.message.reply_text(tr(lang, "no_data_trend", label=label))
                return
            png_bytes, site_name = result
            await query.message.reply_photo(
                photo=io.BytesIO(png_bytes),
                caption=tr(lang, "trend_caption", label=site_name, date=date.today().strftime("%d.%m"), window=HISTORY_WINDOW_DAYS),
            )

        elif data.startswith("chart:"):
            _, site_no, label = data.split(":", 2)
            try:
                result = build_chart(site_no, label, lang)
            except Exception as e:
                logger.exception("Error building chart for %s", site_no)
                await query.message.reply_text(tr(lang, "err_chart", label=label, err=e))
                return
            if result is None:
                await query.message.reply_text(tr(lang, "no_data_chart", label=label))
                return
            png_bytes, site_name = result
            await query.message.reply_photo(
                photo=io.BytesIO(png_bytes),
                caption=tr(lang, "chart7_caption", site_name=site_name, days=CHART_PERIOD_DAYS),
            )

        elif data == "back:rivers":
            await _safe_edit(query, tr(lang, "choose_river"), _river_keyboard(lang))

    except Exception:
        # Last line of defense: a button should never fail silently.
        logger.exception("Unhandled error in button_callback, data=%s", data)
        try:
            await query.message.reply_text(tr(lang, "generic_error"))
        except Exception:
            logger.exception("Couldn't even send the error message")


async def scheduled_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    lang = get_lang(CHAT_ID)
    await context.bot.send_message(chat_id=CHAT_ID, text=build_report(lang), parse_mode="Markdown")


async def _post_init(app: Application) -> None:
    # Registers commands in Telegram's "/" menu next to the input field.
    # Default (shown for any language not explicitly overridden below):
    await app.bot.set_my_commands([
        BotCommand("start", T["en"]["cmd_start"]),
        BotCommand("now", T["en"]["cmd_now"]),
        BotCommand("river", T["en"]["cmd_river"]),
        BotCommand("language", T["en"]["cmd_language"]),
    ])
    # Russian command descriptions, shown to users whose Telegram client
    # language is set to Russian:
    await app.bot.set_my_commands(
        [
            BotCommand("start", T["ru"]["cmd_start"]),
            BotCommand("now", T["ru"]["cmd_now"]),
            BotCommand("river", T["ru"]["cmd_river"]),
            BotCommand("language", T["ru"]["cmd_language"]),
        ],
        language_code="ru",
    )


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN is not set. Check your .env file")
    if not CHAT_ID:
        raise SystemExit("CHAT_ID is not set. Check your .env file")

    app = Application.builder().token(BOT_TOKEN).post_init(_post_init).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("now", now_command))
    app.add_handler(CommandHandler("river", river_command))
    app.add_handler(CommandHandler("language", language_command))
    app.add_handler(CallbackQueryHandler(button_callback))

    hh, mm = (int(x) for x in SCHEDULE_TIME.split(":"))
    app.job_queue.run_daily(
        scheduled_job,
        time=dtime(hour=hh, minute=mm, tzinfo=ZoneInfo(TIMEZONE)),
        name="daily_river_report",
    )

    logger.info(
        "RiverBot started. Sites: %s. Daily report at %s (%s). Default language: %s.",
        USGS_SITES, SCHEDULE_TIME, TIMEZONE, DEFAULT_LANGUAGE,
    )
    while True:
        try:
            app.run_polling()
            break
        except NetworkError as e:
            logger.warning("Network error, retrying in 15s: %s", e)
            time.sleep(15)


if __name__ == "__main__":
    main()
