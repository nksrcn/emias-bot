import asyncio
import logging
import os
from datetime import datetime, timezone, timedelta
import aiohttp

# ── Настройки (переменные окружения Timeweb) ──────────────────────────────────
OMS_NUMBER     = os.environ["OMS_NUMBER"]
BIRTH_DATE     = os.environ["BIRTH_DATE"]
EI_TOKEN       = os.environ["EI_TOKEN"]
COOKIE         = os.environ["EMIAS_COOKIE"]
TG_TOKEN       = os.environ["TG_TOKEN"]
TG_CHAT_ID     = os.environ["TG_CHAT_ID"]
REFRESH_TOKEN  = os.environ["REFRESH_TOKEN"]   # refreshToken из localStorage
ACCESS_TOKEN   = os.environ["ACCESS_TOKEN"]    # accessToken из localStorage

# Мутабельное состояние токенов (обновляются автоматически)
_tokens = {
    "ei_token":      EI_TOKEN,
    "cookie":        COOKIE,
    "access_token":  ACCESS_TOKEN,
    "refresh_token": REFRESH_TOKEN,
}

# ── Данные направления (МНЦ ГКБ им. С.П. Боткина) ────────────────────────────
# Эти значения нужно обновить когда появятся талоны и мы увидим реальный
# getAvailableResourceScheduleInfo для нашего направления
REFERRAL_ID = int(os.getenv("REFERRAL_ID", "172751854717"))

# ── Интервалы проверки ────────────────────────────────────────────────────────
CHECK_NORMAL = int(os.getenv("CHECK_INTERVAL_NORMAL", "300"))  # 5 мин
CHECK_ACTIVE = int(os.getenv("CHECK_INTERVAL_ACTIVE", "1"))    # 1 сек
ACTIVE_START = (7, 28)
ACTIVE_END   = (7, 35)

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
        "Cookie": _tokens["cookie"],
        "Ei-Token": _tokens["ei_token"],
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
        "Referer": "https://emias.info/app/einfo/",
        "Origin": "https://emias.info",
    }


async def refresh_session(session) -> bool:
    """Обновляет сессию через whoAmI."""
    log.info("Обновляю сессию через whoAmI...")
    try:
        async with session.post(
            "https://emias.info/web-api/whoAmI/",
            json={"accessToken": _tokens["access_token"]},
            headers={
                "Content-Type": "application/json",
                "Cookie": _tokens["cookie"],
                "Origin": "https://emias.info",
                "Referer": "https://emias.info/app/einfo/",
            },
            timeout=aiohttp.ClientTimeout(total=15)
        ) as r:
            if r.status == 200:
                # Обновляем cookie из Set-Cookie заголовков
                new_cookies = r.cookies
                if new_cookies:
                    cookie_str = "; ".join(f"{k}={v.value}" for k, v in new_cookies.items())
                    if cookie_str:
                        _tokens["cookie"] = cookie_str + "; " + _tokens["cookie"]
                log.info("whoAmI OK — сессия обновлена")
                return True
            body = await r.text()
            log.warning("whoAmI → %d: %s", r.status, body[:150])
            return False
    except Exception as e:
        log.error("whoAmI ошибка: %s", e)
        return False


async def tg(session, text: str):
    payload = {"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"}
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


async def get_referral_info(session) -> dict | str:
    """
    Шаг 1: Получаем список направлений.
    Возвращает данные нашего направления или 'unauthorized'.
    """
    try:
        async with session.post(
            f"{API}/getAssignmentsReferralsInfo",
            headers=emias_headers(),
            json={"omsNumber": OMS_NUMBER, "birthDate": BIRTH_DATE},
            timeout=aiohttp.ClientTimeout(total=15)
        ) as r:
            if r.status == 401:
                return "unauthorized"
            if r.status != 200:
                log.warning("getAssignmentsReferralsInfo → %d", r.status)
                return {}
            data = await r.json()
            referrals = (
                data.get("payload", {})
                .get("arInfo", {})
                .get("referrals", {})
                .get("items", [])
            )
            # Ищем наше направление по ID
            for ref in referrals:
                if ref.get("id") == REFERRAL_ID:
                    return ref
            return {}
    except Exception as e:
        log.error("get_referral_info: %s", e)
        return {}


async def get_slots(session, referral: dict) -> list | str:
    """
    Шаг 2: Получаем доступные слоты для направления.
    Возвращает список слотов или 'unauthorized'.
    """
    try:
        async with session.post(
            f"{API}/getAvailableResourceScheduleInfo",
            headers=emias_headers(),
            json={
                "omsNumber": OMS_NUMBER,
                "birthDate": BIRTH_DATE,
                "referralId": REFERRAL_ID,
                # availableResourceId и complexResourceId приходят
                # в ответе getAssignmentsReferralsInfo когда есть талоны
                "availableResourceId": referral.get("availableResourceId"),
                "complexResourceId": referral.get("complexResourceId"),
            },
            timeout=aiohttp.ClientTimeout(total=15)
        ) as r:
            if r.status == 401:
                return "unauthorized"
            if r.status != 200:
                body = await r.text()
                log.warning("getAvailableResourceScheduleInfo → %d: %s", r.status, body[:150])
                return []
            data = await r.json()
            p = data.get("payload") or {}
            # Ищем слоты в ответе
            slots = (
                p.get("scheduleByDays") or
                p.get("slots") or
                p.get("schedule") or
                p.get("availableDays") or
                []
            )
            log.info("Слотов найдено: %d", len(slots))
            return slots
    except Exception as e:
        log.error("get_slots: %s", e)
        return []


async def check_available(session) -> list | str:
    """
    Проверяет наличие талонов через getAssignmentsReferralsInfo.
    Талоны есть если в направлении появились availableResourceId/complexResourceId.
    """
    try:
        async with session.post(
            f"{API}/getAssignmentsReferralsInfo",
            headers=emias_headers(),
            json={"omsNumber": OMS_NUMBER, "birthDate": BIRTH_DATE},
            timeout=aiohttp.ClientTimeout(total=15)
        ) as r:
            if r.status == 401:
                return "unauthorized"
            if r.status != 200:
                log.warning("check_available → %d", r.status)
                return []
            data = await r.json()
            referrals = (
                data.get("payload", {})
                .get("arInfo", {})
                .get("referrals", {})
                .get("items", [])
            )
            for ref in referrals:
                if ref.get("id") == REFERRAL_ID:
                    # Талоны есть если появились эти поля
                    if ref.get("availableResourceId") or ref.get("countActiveAppointment", 0) > 0:
                        log.info("Найдены данные для записи: %s", ref)
                        return ref
                    # Логируем текущее состояние для диагностики
                    log.info("Направление найдено, талонов нет. countActive=%d",
                             ref.get("countActiveAppointment", 0))
                    return []
            log.warning("Направление %d не найдено в списке", REFERRAL_ID)
            return []
    except Exception as e:
        log.error("check_available: %s", e)
        return []


async def book_slot(session, referral: dict, slot: dict) -> bool:
    """
    Шаг 3: Записываемся на первый доступный слот.
    """
    # Берём первое время из слота
    times = (
        slot.get("scheduleItems") or
        slot.get("slots") or
        slot.get("times") or
        []
    )
    if not times:
        log.error("Нет времён в слоте: %s", slot)
        return False

    first = times[0]
    start_time = first.get("startTime") or first.get("time") or first.get("start")
    end_time   = first.get("endTime")   or first.get("end")

    log.info("Записываюсь: startTime=%s endTime=%s", start_time, end_time)

    try:
        async with session.post(
            f"{API}/createAppointment",
            headers=emias_headers(),
            json={
                "omsNumber": OMS_NUMBER,
                "birthDate": BIRTH_DATE,
                "referralId": REFERRAL_ID,
                "availableResourceId": referral.get("availableResourceId"),
                "complexResourceId": referral.get("complexResourceId"),
                "startTime": start_time,
                "endTime": end_time,
            },
            timeout=aiohttp.ClientTimeout(total=15)
        ) as r:
            body = await r.text()
            if r.status == 200:
                log.info("Запись успешна: %s", body[:200])
                return True
            log.error("createAppointment → %d: %s", r.status, body[:200])
            return False
    except Exception as e:
        log.error("book_slot: %s", e)
        return False


async def run():
    async with aiohttp.ClientSession() as session:

        # Проверка авторизации при старте
        log.info("Проверяю авторизацию...")
        test = await check_available(session)
        if test == "unauthorized":
            await tg(session,
                "❌ <b>Ошибка авторизации</b>\n\n"
                "Проверьте <code>EI_TOKEN</code> и <code>EMIAS_COOKIE</code> в Timeweb.\n"
                "F12 → Network → запрос к emias.info → Headers."
            )
            return

        log.info("Авторизация OK")
        await tg(session,
            "🤖 <b>Бот запущен и авторизован</b>\n\n"
            "Мониторю: Эзофагогастродуоденоскопия, колоноилеоскопия (скрининг)\n"
            "🏥 МНЦ ГКБ им. С.П. Боткина\n\n"
            f"🔴 Активный режим 07:28–07:35 МСК — каждую секунду\n"
            f"🟢 Обычный режим — каждые {CHECK_NORMAL // 60} мин"
        )

        last_mode = None
        last_refresh = datetime.now(MSK)  # время последнего обновления токена

        while True:
            try:
                interval = get_interval()
                mode = "active" if interval == CHECK_ACTIVE else "normal"
                msk_now = datetime.now(MSK).strftime("%H:%M")

                if mode != last_mode:
                    if mode == "active":
                        await tg(session, f"🔴 <b>Активный режим</b> [{msk_now} МСК] — каждую секунду")
                    else:
                        await tg(session, f"🟢 Обычный режим [{msk_now} МСК] — каждые {interval // 60} мин")
                    last_mode = mode

                # Проактивно обновляем сессию каждые 30 минут
                now = datetime.now(MSK)
                if (now - last_refresh).seconds > 1800:
                    log.info("Проактивное обновление сессии...")
                    await refresh_session(session)
                    last_refresh = now

                result = await check_available(session)

                if result == "unauthorized":
                    log.warning("Токен истёк, пробую автообновление...")
                    refreshed = await refresh_session(session)
                    if refreshed:
                        log.info("Сессия обновлена автоматически")
                        await tg(session, "🔄 Сессия обновлена автоматически, продолжаю мониторинг")
                    else:
                        log.warning("Автообновление не удалось, прошу пользователя обновить вручную")
                        await tg(session,
                            "🔐 <b>Токен истёк — нужно обновить вручную</b>\n\n"
                            "1. Зайдите в ЕМИАС в браузере\n"
                            "2. F12 → Application → Local Storage → emias.info\n"
                            "3. Скопируйте <code>persist:sessionAuth</code>\n"
                            "4. Обновите <code>REFRESH_TOKEN</code>, <code>ACCESS_TOKEN</code>, "
                            "<code>EI_TOKEN</code>, <code>EMIAS_COOKIE</code> в Timeweb\n"
                            "5. Перезапустите приложение"
                        )
                        await asyncio.sleep(1800)
                    continue

                if result and isinstance(result, dict):
                    # Талоны появились — получаем расписание
                    log.info("Талоны появились! Получаю расписание...")
                    slots = await get_slots(session, result)

                    if slots and isinstance(slots, list) and len(slots) > 0:
                        first_slot = slots[0]
                        date = first_slot.get("date", "?")
                        times = (first_slot.get("scheduleItems") or
                                 first_slot.get("slots") or [])
                        time = times[0].get("startTime", "?") if times else "?"

                        log.info("Записываюсь на %s %s", date, time)
                        await tg(session, f"🎉 <b>Талоны появились!</b> Записываюсь на {date} в {time}...")

                        if await book_slot(session, result, first_slot):
                            await tg(session,
                                f"✅ <b>Записано!</b>\n\n"
                                f"📋 Эзофагогастродуоденоскопия, колоноилеоскопия (скрининг)\n"
                                f"📅 {date} в {time}\n"
                                f"🏥 МНЦ ГКБ им. С.П. Боткина"
                            )
                            log.info("Запись успешна! Останавливаю бота.")
                            break
                        else:
                            await tg(session, "⚠️ Не удалось записаться, попробую снова...")
                    else:
                        log.warning("Слоты не получены, данные: %s", result)

                else:
                    log.info("[%s] Талонов нет", datetime.now(MSK).strftime("%H:%M:%S"))

            except Exception as e:
                log.error("Ошибка: %s", e)
                await tg(session, f"⚠️ Ошибка: {e}")

            await asyncio.sleep(get_interval())


if __name__ == "__main__":
    asyncio.run(run())
