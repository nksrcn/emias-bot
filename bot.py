import asyncio
import logging
import os
from datetime import datetime, timezone, timedelta
import aiohttp

# ── Настройки ─────────────────────────────────────────────────────────────────
OMS_NUMBER    = os.environ["OMS_NUMBER"]
BIRTH_DATE    = os.environ["BIRTH_DATE"]
TG_TOKEN      = os.environ["TG_TOKEN"]
TG_CHAT_ID    = os.environ["TG_CHAT_ID"]
REFERRAL_ID   = int(os.getenv("REFERRAL_ID", "172751854717"))

CHECK_NORMAL  = int(os.getenv("CHECK_INTERVAL_NORMAL", "300"))  # 5 мин
CHECK_ACTIVE  = int(os.getenv("CHECK_INTERVAL_ACTIVE", "1"))    # 1 сек
ACTIVE_START  = (7, 28)
ACTIVE_END    = (7, 35)

# Токены — читаем при старте, храним в словаре для обновления
_tokens = {
    "ei_token":     os.environ["EI_TOKEN"],
    "cookie":       os.environ["EMIAS_COOKIE"],
    "access_token": os.environ["ACCESS_TOKEN"],
}

MSK = timezone(timedelta(hours=3))
API = "https://emias.info/api-eip/v4/saOrchestrator"
TG_MIRRORS = ["https://api.telegram.org", "https://tg.i-c-a.su"]

# Интервал обновления сессии — каждые 20 минут (с запасом)
SESSION_REFRESH_INTERVAL = 20 * 60

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ── Время ─────────────────────────────────────────────────────────────────────
def get_interval() -> int:
    msk = datetime.now(MSK)
    h, m = msk.hour, msk.minute
    active = (
        (h == ACTIVE_START[0] and m >= ACTIVE_START[1]) or
        (h == ACTIVE_END[0]   and m <= ACTIVE_END[1])   or
        (ACTIVE_START[0] < h < ACTIVE_END[0])
    )
    return CHECK_ACTIVE if active else CHECK_NORMAL


# ── Заголовки ─────────────────────────────────────────────────────────────────
def emias_headers() -> dict:
    return {
        "Content-Type": "application/json",
        "Cookie":       _tokens["cookie"],
        "Ei-Token":     _tokens["ei_token"],
        "User-Agent":   "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
        "Referer":      "https://emias.info/app/einfo/",
        "Origin":       "https://emias.info",
    }


# ── Telegram ──────────────────────────────────────────────────────────────────
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


# ── Обновление сессии ─────────────────────────────────────────────────────────
async def refresh_session(session) -> bool:
    """
    Обновляет сессию через whoAmI.
    accessToken == Ei-Token — одно и то же значение.
    whoAmI возвращает новый accessToken который используем как новый Ei-Token.
    """
    import re
    log.info("Обновляю сессию через whoAmI (accessToken=%s...)", _tokens["access_token"][:20])
    try:
        async with session.post(
            "https://emias.info/web-api/whoAmI/",
            json={"accessToken": _tokens["access_token"]},
            headers={
                "Content-Type": "application/json",
                "Cookie":       _tokens["cookie"],
                "Origin":       "https://emias.info",
                "Referer":      "https://emias.info/app/einfo/",
                "User-Agent":   "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
            },
            timeout=aiohttp.ClientTimeout(total=15)
        ) as r:
            if r.status == 200:
                # Обновляем session-cookie
                new_cookies = r.cookies
                if new_cookies:
                    existing = _tokens["cookie"]
                    for k, v in new_cookies.items():
                        part = f"{k}={v.value}"
                        if k in existing:
                            existing = re.sub(rf"{k}=[^;]*", part, existing)
                        else:
                            existing = part + "; " + existing
                    _tokens["cookie"] = existing

                # Самое важное: whoAmI возвращает новый accessToken
                # который одновременно является новым Ei-Token
                data = await r.json()
                new_token = (
                    data.get("accessToken") or
                    data.get("access_token") or
                    data.get("eiToken") or
                    data.get("token")
                )
                if new_token:
                    _tokens["access_token"] = new_token
                    _tokens["ei_token"] = new_token
                    log.info("Токен обновлён: %s...", new_token[:20])
                else:
                    log.info("whoAmI не вернул новый токен, используем текущий")

                log.info("whoAmI OK — сессия обновлена")
                return True

            body = await r.text()
            log.warning("whoAmI → %d: %s", r.status, body[:200])
            return False
    except Exception as e:
        log.error("whoAmI ошибка: %s", e)
        return False


# ── Проверка талонов ──────────────────────────────────────────────────────────
async def check_available(session):
    """
    Возвращает dict с данными направления если талоны есть,
    [] если талонов нет, 'unauthorized' если токен истёк.
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
                    if ref.get("availableResourceId"):
                        log.info("Талоны появились! %s", ref)
                        return ref
                    log.info("[%s] Талонов нет (countActive=%d)",
                             datetime.now(MSK).strftime("%H:%M:%S"),
                             ref.get("countActiveAppointment", 0))
                    return []
            log.warning("Направление %d не найдено", REFERRAL_ID)
            return []
    except Exception as e:
        log.error("check_available: %s", e)
        return []


# ── Получение слотов ──────────────────────────────────────────────────────────
async def get_slots(session, referral: dict) -> list:
    try:
        async with session.post(
            f"{API}/getAvailableResourceScheduleInfo",
            headers=emias_headers(),
            json={
                "omsNumber":         OMS_NUMBER,
                "birthDate":         BIRTH_DATE,
                "referralId":        REFERRAL_ID,
                "availableResourceId": referral.get("availableResourceId"),
                "complexResourceId":   referral.get("complexResourceId"),
            },
            timeout=aiohttp.ClientTimeout(total=15)
        ) as r:
            if r.status != 200:
                body = await r.text()
                log.warning("getAvailableResourceScheduleInfo → %d: %s", r.status, body[:150])
                return []
            data = await r.json()
            p = data.get("payload") or {}
            return (
                p.get("scheduleByDays") or
                p.get("slots") or
                p.get("schedule") or
                p.get("availableDays") or
                []
            )
    except Exception as e:
        log.error("get_slots: %s", e)
        return []


# ── Запись ────────────────────────────────────────────────────────────────────
async def book_slot(session, referral: dict, slot: dict) -> bool:
    times = (
        slot.get("scheduleItems") or
        slot.get("slots") or
        slot.get("times") or []
    )
    if not times:
        log.error("Нет времён в слоте: %s", slot)
        return False

    first = times[0]
    start_time = first.get("startTime") or first.get("time") or first.get("start")
    end_time   = first.get("endTime")   or first.get("end")
    log.info("Записываюсь: %s — %s", start_time, end_time)

    try:
        async with session.post(
            f"{API}/createAppointment",
            headers=emias_headers(),
            json={
                "omsNumber":           OMS_NUMBER,
                "birthDate":           BIRTH_DATE,
                "referralId":          REFERRAL_ID,
                "availableResourceId": referral.get("availableResourceId"),
                "complexResourceId":   referral.get("complexResourceId"),
                "startTime":           start_time,
                "endTime":             end_time,
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


# ── Главный цикл ──────────────────────────────────────────────────────────────
async def run():
    async with aiohttp.ClientSession() as session:

        # Проверка авторизации при старте
        log.info("Проверяю авторизацию...")
        test = await check_available(session)
        if test == "unauthorized":
            await tg(session,
                "❌ <b>Ошибка авторизации при запуске</b>\n\n"
                "Обновите токены в Timeweb:\n"
                "<code>EI_TOKEN</code>, <code>EMIAS_COOKIE</code>, <code>ACCESS_TOKEN</code>\n\n"
                "F12 → Network → Headers (для EI_TOKEN и COOKIE)\n"
                "F12 → Application → Local Storage → persist:sessionAuth (для ACCESS_TOKEN)"
            )
            return

        log.info("Авторизация OK")
        await tg(session,
            "🤖 <b>Бот запущен и авторизован</b>\n\n"
            "Мониторю: Эзофагогастродуоденоскопия, колоноилеоскопия (скрининг)\n"
            "🏥 МНЦ ГКБ им. С.П. Боткина\n\n"
            f"🔴 Активный режим 07:28–07:35 МСК — каждую секунду\n"
            f"🟢 Обычный режим — каждые {CHECK_NORMAL // 60} мин\n"
            f"🔄 Автообновление сессии — каждые {SESSION_REFRESH_INTERVAL // 60} мин"
        )

        last_mode = None
        last_refresh = datetime.now(MSK)

        while True:
            try:
                now = datetime.now(MSK)
                interval = get_interval()
                mode = "active" if interval == CHECK_ACTIVE else "normal"

                # Уведомляем о смене режима
                if mode != last_mode:
                    msk_now = now.strftime("%H:%M")
                    if mode == "active":
                        await tg(session, f"🔴 <b>Активный режим</b> [{msk_now} МСК] — каждую секунду")
                    else:
                        await tg(session, f"🟢 Обычный режим [{msk_now} МСК] — каждые {interval // 60} мин")
                    last_mode = mode

                # Проактивное обновление сессии каждые 20 минут
                if (now - last_refresh).total_seconds() > SESSION_REFRESH_INTERVAL:
                    log.info("Проактивное обновление сессии...")
                    await refresh_session(session)
                    last_refresh = now

                # Проверяем талоны
                result = await check_available(session)

                if result == "unauthorized":
                    # Пробуем обновить через whoAmI
                    log.warning("401 — пробую обновить сессию...")
                    if await refresh_session(session):
                        log.info("Сессия обновлена, повторяю проверку")
                        await tg(session, "🔄 Сессия обновлена автоматически")
                        last_refresh = datetime.now(MSK)
                    else:
                        # Только тогда просим пользователя
                        await tg(session,
                            "🔐 <b>Требуется обновление токенов</b>\n\n"
                            "1. Откройте emias.info в браузере\n"
                            "2. F12 → Network → Headers → скопируйте <code>Ei-Token</code> и <code>Cookie</code>\n"
                            "3. F12 → Application → Local Storage → <code>persist:sessionAuth</code> → скопируйте <code>accessToken</code>\n"
                            "4. Обновите в Timeweb: <code>EI_TOKEN</code>, <code>EMIAS_COOKIE</code>, <code>ACCESS_TOKEN</code>\n"
                            "5. Перезапустите приложение"
                        )
                        await asyncio.sleep(1800)
                    continue

                if result and isinstance(result, dict):
                    log.info("Талоны найдены! Получаю расписание...")
                    slots = await get_slots(session, result)

                    if slots:
                        first_slot = slots[0]
                        date  = first_slot.get("date", "?")
                        times = first_slot.get("scheduleItems") or first_slot.get("slots") or []
                        time  = times[0].get("startTime", "?") if times else "?"

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

            except Exception as e:
                log.error("Ошибка: %s", e)
                await tg(session, f"⚠️ Ошибка: {e}")

            await asyncio.sleep(get_interval())


if __name__ == "__main__":
    asyncio.run(run())
