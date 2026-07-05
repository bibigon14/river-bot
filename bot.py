#!/usr/bin/env python3
"""
RiverBot — Telegram-бот, присылающий уровень воды, расход и температуру
с гидропостов USGS (waterservices.usgs.gov).

Возможности:
  /now       — текущие данные по всем постам из USGS_SITES (.env)
  /river     — выбрать реку и участок кнопками, посмотреть его данные
               и сравнить с тем же днём в прошлые годы
  расписание — раз в день в заданное время бот сам присылает /now-отчёт

Настройки — в файле .env (см. .env.example).
"""

from __future__ import annotations

import io
import logging
import os
from datetime import date, datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

import matplotlib
matplotlib.use("Agg")  # без дисплея, только рендер в файл/буфер
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import requests
from dotenv import load_dotenv
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
# Посты по умолчанию: American River at Fair Oaks + Sacramento River at Freeport
# Можно указать несколько постов через запятую: "11446500,11447650"
USGS_SITES = [s.strip() for s in os.getenv("USGS_SITES", "11446500,11447650").split(",") if s.strip()]
SCHEDULE_TIME = os.getenv("SCHEDULE_TIME", "07:00")  # HH:MM, локальное время
TIMEZONE = os.getenv("TIMEZONE", "America/Los_Angeles")

# Порог температуры воды (°F), при котором комфортно/начинается миграция лосося.
# Если текущая температура <= порога, в сообщение добавляется пометка.
SALMON_TEMP_THRESHOLD_F = float(os.getenv("SALMON_TEMP_THRESHOLD_F", "65"))

# Свежие данные считаются актуальными не старше этого числа часов,
# иначе параметр показывается как "нет свежих данных" (у некоторых постов
# отдельные датчики могут не работать месяцами/годами, хотя пост в целом жив).
MAX_DATA_AGE_HOURS = 6

# На сколько лет назад сравнивать текущие данные (кнопка "Сравнить с прошлым")
HISTORY_YEARS_BACK = [1, 3, 5]

# Ширина окна для графика по годам: +/- столько дней вокруг сегодняшней даты
# (30 -> месяц вокруг текущего дня, чтобы был виден тренд, а не одна точка)
HISTORY_WINDOW_DAYS = 15

# Реестр рек и их участков (постов) для команды /river.
# Формат: "Название реки": [("Название участка", "номер поста USGS"), ...]
# Добавляйте свои посты — искать номера на https://waterdata.usgs.gov
RIVERS = {
    "American River": [
        ("Fair Oaks", "11446500"),
    ],
    "Sacramento River": [
        ("Freeport", "11447650"),
        ("Verona", "11425500"),
    ],
}

IV_URL = "https://waterservices.usgs.gov/nwis/iv/"   # текущие (instantaneous) данные
DV_URL = "https://waterservices.usgs.gov/nwis/dv/"   # исторические дневные средние

# Коды параметров USGS
PARAM_DISCHARGE = "00060"   # расход, куб. футов/с
PARAM_GAGE_HEIGHT = "00065" # уровень, футы
PARAM_TEMPERATURE = "00010" # температура воды, °C

PARAM_LABELS = {
    PARAM_DISCHARGE: ("Расход", "ft³/s"),
    PARAM_GAGE_HEIGHT: ("Уровень", "ft"),
    PARAM_TEMPERATURE: ("Температура", "°C"),
}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# USGS: текущие данные
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
    """Запрашивает у USGS текущие значения по одному посту.

    Возвращает dict вида:
      {
        "site_name": "AMERICAN R A FAIR OAKS CA",
        "values": {"00060": ("4040", "ft³/s", "2026-07-04T18:00:00.000-07:00"), ...},
        "datetime": "2026-07-04T18:00:00.000-07:00",  # самая свежая метка среди параметров
      }
    """
    params = {
        "sites": site,
        "parameterCd": ",".join(PARAM_LABELS.keys()),
        "siteStatus": "all",
        "format": "json",
    }
    resp = requests.get(IV_URL, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    series = data.get("value", {}).get("timeSeries", [])
    if not series:
        raise ValueError(f"Нет данных для поста {site}")

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
            continue  # датчик молчит дольше MAX_DATA_AGE_HOURS — считаем недоступным
        _label, unit = PARAM_LABELS.get(code, (code, ""))
        values[code] = (v["value"], unit, dt_str)
        if latest_dt is None or (dt_str and dt_str > latest_dt):
            latest_dt = dt_str

    return {"site_name": site_name, "site_no": site, "values": values, "datetime": latest_dt}


def format_message(info: dict) -> str:
    lines = [f"🌊 *{info['site_name']}* (USGS {info['site_no']})"]
    temp_f = None
    for code, (label, _unit) in PARAM_LABELS.items():
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
            lines.append(f"{PARAM_LABELS[code][0]}: нет свежих данных")
    if temp_f is not None and temp_f <= SALMON_TEMP_THRESHOLD_F:
        lines.append(f"🐟 Температура ≤ {SALMON_TEMP_THRESHOLD_F:.0f}°F — комфортно для миграции лосося")
    if info.get("datetime"):
        lines.append(f"_Обновлено: {info['datetime']}_")
    return "\n".join(lines)


def build_report() -> str:
    chunks = []
    for site in USGS_SITES:
        try:
            info = fetch_site_data(site)
            chunks.append(format_message(info))
        except Exception as e:
            logger.exception("Ошибка при получении данных по посту %s", site)
            chunks.append(f"⚠️ Пост {site}: ошибка получения данных ({e})")
    return "\n\n".join(chunks)


# --------------------------------------------------------------------------
# USGS: исторические дневные средние (nwis/dv) — для сравнения с прошлыми годами
# --------------------------------------------------------------------------

def fetch_daily_mean(site: str, day: date) -> dict:
    """Возвращает средние за сутки значения расхода и температуры для заданной даты.

    Служба dv хранит данные с момента открытия поста (часто десятки лет),
    в отличие от iv (только последние ~120 дней).
    """
    day_str = day.isoformat()
    params = {
        "sites": site,
        "parameterCd": ",".join(PARAM_LABELS.keys()),
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


def build_history_comparison(site: str, site_name: str) -> str:
    today = date.today()
    lines = [f"📊 *{site_name}* — сравнение с прошлыми годами ({today.strftime('%d.%m')}):", ""]
    any_data = False
    for years in HISTORY_YEARS_BACK:
        try:
            past_day = today.replace(year=today.year - years)
        except ValueError:
            past_day = today.replace(year=today.year - years, day=28)  # 29 февраля и т.п.
        try:
            values = fetch_daily_mean(site, past_day)
        except Exception as e:
            logger.exception("Ошибка получения истории для %s (%s лет назад)", site, years)
            lines.append(f"{past_day.strftime('%d.%m.%Y')}: ошибка получения данных ({e})")
            continue
        if not values:
            lines.append(f"{past_day.strftime('%d.%m.%Y')}: нет данных")
            continue
        any_data = True
        parts = []
        if PARAM_DISCHARGE in values:
            parts.append(f"расход {values[PARAM_DISCHARGE]} ft³/s")
        if PARAM_TEMPERATURE in values:
            try:
                c = float(values[PARAM_TEMPERATURE])
                parts.append(f"темп. {c:.1f}°C/{c*9/5+32:.1f}°F")
            except ValueError:
                pass
        if PARAM_GAGE_HEIGHT in values:
            parts.append(f"уровень {values[PARAM_GAGE_HEIGHT]} ft")
        lines.append(f"{past_day.strftime('%d.%m.%Y')}: " + (", ".join(parts) if parts else "нет данных"))
    if not any_data:
        lines.append("\nИсторические данные для этого поста недоступны через nwis/dv.")
    return "\n".join(lines)


def fetch_daily_series(site: str, code: str, start: date, end: date) -> list[tuple[date, float]]:
    """Дневные средние значения одного параметра за диапазон дат (nwis/dv)."""
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


def build_history_chart(site: str, label: str) -> tuple[bytes, str] | None:
    """Строит график расхода за окно +/- HISTORY_WINDOW_DAYS дней вокруг сегодняшней
    даты, с отдельной линией на каждый год (текущий + HISTORY_YEARS_BACK назад) —
    чтобы был виден тренд по месяцу, а не значение за один день."""
    today = date.today()
    years_list = [0] + HISTORY_YEARS_BACK  # 0 = текущий год

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
            logger.exception("Ошибка получения графика по годам для %s (%s лет назад)", site, years_back)
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
            label=f"{center.year}" + (" (сейчас)" if is_current else ""),
        )
        any_data = True

    if not any_data:
        plt.close(fig)
        return None

    ax.axvline(0, color="#888888", linewidth=1, linestyle=":")
    ax.set_xlabel(f"Дни от {today.strftime('%d.%m')}")
    ax.set_ylabel("Расход, ft³/s")
    ax.set_title(f"{label} — расход по годам ({today.strftime('%d.%m')} ± {HISTORY_WINDOW_DAYS} дн.)")
    ax.legend(fontsize=8)
    ax.grid(alpha=0.25)
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue(), label


# --------------------------------------------------------------------------
# График за последние N дней (расход + температура)
# --------------------------------------------------------------------------

CHART_PERIOD_DAYS = 7


def fetch_period_series(site: str, codes: list[str], period_days: int) -> dict:
    """Возвращает {код_параметра: [(datetime, float), ...]} за последние period_days дней.

    iv-служба USGS хранит данные примерно за последние ~120 дней.
    """
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


def build_chart(site: str, label: str) -> tuple[bytes, str] | None:
    """Строит PNG-график расхода и температуры за CHART_PERIOD_DAYS дней.

    Возвращает (png_bytes, site_name) или None, если данных нет вообще.
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
        ax1.plot(xs, ys, color="tab:blue", label="Расход (ft³/s)")
        ax1.set_ylabel("Расход, ft³/s", color="tab:blue")
        ax1.tick_params(axis="y", labelcolor="tab:blue")

    if temp:
        xs_t, ys_c = zip(*temp)
        ys_f = [c * 9 / 5 + 32 for c in ys_c]
        ax2 = ax1.twinx()
        ax2.plot(xs_t, ys_f, color="tab:red", label="Температура (°F)")
        ax2.set_ylabel("Температура, °F", color="tab:red")
        ax2.tick_params(axis="y", labelcolor="tab:red")

    ax1.set_title(f"{site_name} — последние {CHART_PERIOD_DAYS} дн.")
    ax1.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m"))
    fig.autofmt_xdate()
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue(), site_name


# --------------------------------------------------------------------------
# Telegram: команды и меню выбора реки/участка
# --------------------------------------------------------------------------

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Привет! Я слежу за уровнем, расходом и температурой воды (данные USGS).\n\n"
        "/now — текущие данные по всем постам по умолчанию\n"
        "/river — выбрать реку и участок, посмотреть график и историю\n\n"
        f"Ваш chat_id: {update.effective_chat.id}"
    )


async def now_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(build_report(), parse_mode="Markdown")


async def river_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = [
        [InlineKeyboardButton(river, callback_data=f"river:{river}")]
        for river in RIVERS
    ]
    await update.message.reply_text(
        "Выберите реку:", reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def _safe_edit(query, text: str, keyboard: InlineKeyboardMarkup, parse_mode: str | None = None) -> None:
    """edit_message_text с фолбэком: если Markdown ломается или правка невозможна,
    пробуем без разметки, а если и это не выходит — шлём новое сообщение вместо тихого падения."""
    try:
        await query.edit_message_text(text, parse_mode=parse_mode, reply_markup=keyboard)
    except Exception:
        logger.exception("edit_message_text не удался, пробуем без parse_mode")
        try:
            await query.edit_message_text(text, reply_markup=keyboard)
        except Exception:
            logger.exception("edit_message_text снова не удался, шлю новое сообщение")
            await query.message.reply_text(text, reply_markup=keyboard)


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data

    try:
        if data.startswith("river:"):
            river = data.split(":", 1)[1]
            sections = RIVERS.get(river, [])
            keyboard = [
                [InlineKeyboardButton(label, callback_data=f"site:{site_no}:{label}")]
                for label, site_no in sections
            ]
            keyboard.append([InlineKeyboardButton("« Назад", callback_data="back:rivers")])
            await _safe_edit(query, f"{river} — выберите участок:", InlineKeyboardMarkup(keyboard))

        elif data.startswith("site:"):
            _, site_no, label = data.split(":", 2)
            try:
                info = fetch_site_data(site_no)
                text = format_message(info)
            except Exception as e:
                logger.exception("Ошибка при получении данных по посту %s", site_no)
                text = f"⚠️ Ошибка получения данных для {label} ({e})"
            keyboard = [
                [InlineKeyboardButton("📊 Сравнить с прошлыми годами", callback_data=f"hist:{site_no}:{label}")],
                [InlineKeyboardButton("📉 Тренд по годам (график)", callback_data=f"histchart:{site_no}:{label}")],
                [InlineKeyboardButton("📈 График за 7 дней", callback_data=f"chart:{site_no}:{label}")],
                [InlineKeyboardButton("« К списку рек", callback_data="back:rivers")],
            ]
            await _safe_edit(query, text, InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

        elif data.startswith("hist:"):
            _, site_no, label = data.split(":", 2)
            try:
                text = build_history_comparison(site_no, label)
            except Exception as e:
                logger.exception("Ошибка сравнения с прошлыми годами для %s", site_no)
                text = f"⚠️ Не удалось получить исторические данные для {label} ({e})"
            keyboard = [[InlineKeyboardButton("« К списку рек", callback_data="back:rivers")]]
            await _safe_edit(query, text, InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

        elif data.startswith("histchart:"):
            _, site_no, label = data.split(":", 2)
            try:
                result = build_history_chart(site_no, label)
            except Exception as e:
                logger.exception("Ошибка построения графика по годам для %s", site_no)
                await query.message.reply_text(f"⚠️ Не удалось построить график по годам для {label} ({e})")
                return
            if result is None:
                await query.message.reply_text(f"Нет исторических данных для графика по посту {label}.")
                return
            png_bytes, site_name = result
            await query.message.reply_photo(
                photo=io.BytesIO(png_bytes),
                caption=f"{site_name} — расход, {date.today().strftime('%d.%m')} ± {HISTORY_WINDOW_DAYS} дн., по годам",
            )

        elif data.startswith("chart:"):
            _, site_no, label = data.split(":", 2)
            try:
                result = build_chart(site_no, label)
            except Exception as e:
                logger.exception("Ошибка построения графика для %s", site_no)
                await query.message.reply_text(f"⚠️ Не удалось построить график для {label} ({e})")
                return
            if result is None:
                await query.message.reply_text(f"Нет данных для графика по посту {label}.")
                return
            png_bytes, site_name = result
            await query.message.reply_photo(
                photo=io.BytesIO(png_bytes),
                caption=f"{site_name} — расход и температура, последние {CHART_PERIOD_DAYS} дн.",
            )

        elif data == "back:rivers":
            keyboard = [
                [InlineKeyboardButton(river, callback_data=f"river:{river}")]
                for river in RIVERS
            ]
            await _safe_edit(query, "Выберите реку:", InlineKeyboardMarkup(keyboard))

    except Exception:
        # Последний рубеж: чтобы кнопка никогда не "молчала" без ответа пользователю.
        logger.exception("Необработанная ошибка в button_callback, data=%s", data)
        try:
            await query.message.reply_text("⚠️ Произошла ошибка при обработке кнопки. Подробности — в логе бота.")
        except Exception:
            logger.exception("Не удалось даже отправить сообщение об ошибке")


async def scheduled_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    await context.bot.send_message(chat_id=CHAT_ID, text=build_report(), parse_mode="Markdown")


async def _post_init(app: Application) -> None:
    # Регистрирует команды в меню Telegram (кнопка "/" рядом с полем ввода).
    await app.bot.set_my_commands([
        BotCommand("start", "Приветствие и справка"),
        BotCommand("now", "Текущие данные по всем постам"),
        BotCommand("river", "Выбрать реку и участок"),
    ])


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN не задан. Проверьте файл .env")
    if not CHAT_ID:
        raise SystemExit("CHAT_ID не задан. Проверьте файл .env")

    app = Application.builder().token(BOT_TOKEN).post_init(_post_init).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("now", now_command))
    app.add_handler(CommandHandler("river", river_command))
    app.add_handler(CallbackQueryHandler(button_callback))

    hh, mm = (int(x) for x in SCHEDULE_TIME.split(":"))
    app.job_queue.run_daily(
        scheduled_job,
        time=dtime(hour=hh, minute=mm, tzinfo=ZoneInfo(TIMEZONE)),
        name="daily_river_report",
    )

    logger.info(
        "RiverBot запущен. Посты: %s. Ежедневная рассылка в %s (%s).",
        USGS_SITES, SCHEDULE_TIME, TIMEZONE,
    )
    app.run_polling()


if __name__ == "__main__":
    main()
