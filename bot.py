import asyncio
import json
import logging
import os
from datetime import datetime, timezone, timedelta
import aiohttp

# ── Настройки (переменные окружения Timeweb) ──────────────────────────────────
OMS_NUMBER = os.environ["OMS_NUMBER"]    # номер полиса
BIRTH_DATE = os.environ["BIRTH_DATE"]   # дата рождения: 1970-08-15
EI_TOKEN   = os.environ["EI_TOKEN"]     # токен из браузера (Ei-Token)
COOKIE     = os.environ["EMIAS_COOKIE"] # cookie из браузера
TG_TOKEN   = os.environ["TG_TOKEN"]     # токен Telegram бота
TG_CHAT_ID = os.environ["TG_CHAT_ID"]  # ваш Telegram chat_id

# ── Данные направления (из API ЕМИАС) ─────────────────────────────────────────
REFERRAL_ID = 172751854717   # ID направления
LPU_ID      = 10492228       # МНЦ ГКБ им. С.П. Боткина
LDP_TYPE_ID = 1267932267     # Эзофагогастродуоденоскопия, колоноилеоскопия

# ── Интервалы проверки ────────────────────────────────────────────────────────
CHECK_NORMAL = int(os.getenv("CHECK_INTERVAL_NORMAL", "300"))  # обычно: 5 мин
CHECK_ACTIVE = int(os.getenv("CHECK_INTERVAL_ACTIVE", "5"))    # активно: 5 сек
ACTIVE_START = (7, 25)
ACTIVE_END   = (7, 45)

MSK = timezone(timedelta(hours=3))
API = "https://emias.info/api-eip/v4/saOrchestrator"
TG_MIRRORS = ["https://api.telegram.org", "https://tg.i-c-a.su"]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


def get_interval() -> int:
    msk = datetime.now(MSK)
    h, m = msk.hour, msk.minute
    active = (
        (h == ACTIVE_START[0] and m >= ACTIVE_START[1]) or
        (h == ACTIVE_END[0]   and m <= ACTIVE_END[1])   or
        (ACTIVE_START[0] < h < ACTIVE_END[0])
    )
    return CHECK_ACTIVE if active else CHECK_NORMAL


def emias_headers() -> dict:
    return {
        "Content-Type": "application/json",
        "Cookie": COOKIE,
        "Ei-Token": EI_TOKEN,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
        "Referer": "https://emias.info/app/einfo/",
        "Origin": "https://emias.info",
    }


async def tg(session, text: str, buttons=None):
    payload = {"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"}
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": buttons}
    for mirror in TG_MIRRORS:
        try:
            async with session.post(
                f"{mirror}/bot{TG_TOKEN}/sendMessage",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as r:
                if r.status == 200:
                    return
        except Exception as e:
            log.warning("Telegram %s: %s", mirror, e)
    log.error("Telegram недоступен")


async def get_slots(session):
    """Проверяет доступные талоны. Возвращает список слотов | 'unauthorized' | []"""
    payload = {
        "referralId": REFERRAL_ID,
        "lpuId": LPU_ID,
        "ldpTypeId": LDP_TYPE_ID,
        "omsNumber": OMS_NUMBER,
        "birthDate": BIRTH_DATE,
    }
    endpoints = [
        f"{API}/getAvailableScheduleByReferral",
        f"{API}/getLdpSchedule",
        "https://emias.info/api-eip/v1/ldp/schedule/getAvailableSchedule",
    ]
    for url in endpoints:
        try:
            async with session.post(
                url, headers=emias_headers(), json=payload,
                timeout=aiohttp.ClientTimeout(total=15)
            ) as r:
                if r.status == 401:
                    return "unauthorized"
                if r.status in (404, 405):
                    continue
                if r.status != 200:
                    body = await r.text()
                    log.warning("%s → %d: %s", url.split("/")[-1], r.status, body[:150])
                    continue
                data = await r.json()
                log.info("Рабочий endpoint: %s", url.split("/")[-1])
                p = data.get("payload") or {}
                slots = (
                    p.get("scheduleByDays") or
                    p.get("slots") or
                    p.get("schedule") or
                    (p if isinstance(p, list) else [])
                )
                return slots
        except Exception as e:
            log.warning("%s → %s", url.split("/")[-1], e)
    return []


async def book_slot(session, slot: dict) -> bool:
    """Записывается на первый доступный слот."""
    times = slot.get("scheduleItems") or []
    if not times:
        return False
    first = times[0]
    payload = {
        "referralId": REFERRAL_ID,
        "lpuId": LPU_ID,
        "ldpTypeId": LDP_TYPE_ID,
        "omsNumber": OMS_NUMBER,
        "birthDate": BIRTH_DATE,
        "scheduleItemId": first.get("scheduleItemId"),
        "appointmentDate": slot.get("date"),
        "appointmentTime": first.get("time"),
    }
    endpoints = [
        f"{API}/createLdpAppointment",
        "https://emias.info/api-eip/v1/ldp/schedule/createLdpAppointment",
    ]
    for url in endpoints:
        try:
            async with session.post(
                url, headers=emias_headers(), json=payload,
                timeout=aiohttp.ClientTimeout(total=15)
            ) as r:
                if r.status == 200:
                    log.info("Запись успешна")
                    return True
                body = await r.text()
                log.error("%s → %d: %s", url.split("/")[-1], r.status, body[:200])
        except Exception as e:
            log.error("book_slot %s: %s", url.split("/")[-1], e)
    return False


async def run():
    async with aiohttp.ClientSession() as session:

        # Проверка авторизации при старте
        log.info("Проверяю авторизацию...")
        test = await get_slots(session)
        if test == "unauthorized":
            await tg(session,
                "❌ <b>Ошибка авторизации</b>\n\n"
                "Проверьте <code>EI_TOKEN</code> и <code>EMIAS_COOKIE</code> в Timeweb.\n"
                "Возьмите свежие значения: F12 → Network → запрос к emias.info → Headers."
            )
            return

        log.info("Авторизация OK")
        await tg(session,
            "🤖 <b>Бот запущен и авторизован</b>\n\n"
            "Мониторю: Эзофагогастродуоденоскопия, колоноилеоскопия (скрининг)\n"
            "🏥 МНЦ ГКБ им. С.П. Боткина\n\n"
            f"🔴 Активный режим 07:25–07:45 МСК — каждые {CHECK_ACTIVE} сек\n"
            f"🟢 Обычный режим — каждые {CHECK_NORMAL // 60} мин"
        )

        last_mode = None

        while True:
            try:
                interval = get_interval()
                mode = "active" if interval == CHECK_ACTIVE else "normal"
                msk_now = datetime.now(MSK).strftime("%H:%M")

                if mode != last_mode:
                    if mode == "active":
                        await tg(session, f"🔴 <b>Активный режим</b> [{msk_now} МСК] — каждые {interval} сек")
                    else:
                        await tg(session, f"🟢 Обычный режим [{msk_now} МСК] — каждые {interval // 60} мин")
                    last_mode = mode

                slots = await get_slots(session)

                if slots == "unauthorized":
                    await tg(session,
                        "🔐 <b>Токен истёк — нужно обновить</b>\n\n"
                        "1. Зайдите в ЕМИАС в браузере\n"
                        "2. F12 → Network → любой запрос к emias.info\n"
                        "3. Скопируйте <code>Ei-Token</code> и <code>Cookie</code>\n"
                        "4. Обновите в Timeweb → Variables\n"
                        "5. Перезапустите приложение"
                    )
                    await asyncio.sleep(1800)
                    continue

                if slots:
                    first = slots[0]
                    date = first.get("date", "?")
                    times = first.get("scheduleItems", [])
                    time = times[0].get("time", "?") if times else "?"
                    log.info("Талоны найдены! %s %s", date, time)
                    await tg(session, f"🎉 <b>Талоны появились!</b> Записываюсь на {date} в {time}...")

                    if await book_slot(session, first):
                        await tg(session,
                            f"✅ <b>Записано!</b>\n\n"
                            f"📋 Эзофагогастродуоденоскопия, колоноилеоскопия (скрининг)\n"
                            f"📅 {date} в {time}\n"
                            f"🏥 МНЦ ГКБ им. С.П. Боткина"
                        )
                        break
                    else:
                        await tg(session, "⚠️ Не удалось записаться, попробую снова через минуту...")
                        await asyncio.sleep(60)
                        continue
                else:
                    log.info("[%s] Талонов нет", datetime.now(MSK).strftime("%H:%M:%S"))

            except Exception as e:
                log.error("Ошибка: %s", e)
                await tg(session, f"⚠️ Ошибка: {e}")

            await asyncio.sleep(get_interval())


if __name__ == "__main__":
    asyncio.run(run())
