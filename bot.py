import asyncio
import logging
import os
import re
from datetime import datetime, timezone, timedelta
import aiohttp

# ── Настройки ─────────────────────────────────────────────────────────────────
OMS_NUMBER     = os.environ["OMS_NUMBER"]    # 7700007070150870
BIRTH_DATE     = os.environ["BIRTH_DATE"]   # 1970-08-15
EMIAS_LOGIN    = os.environ["EMIAS_LOGIN"]  # логин ЕМИАС
EMIAS_PASSWORD = os.environ["EMIAS_PASSWORD"]  # пароль ЕМИАС
TG_TOKEN       = os.environ["TG_TOKEN"]
TG_CHAT_ID     = os.environ["TG_CHAT_ID"]
REFERRAL_ID    = int(os.getenv("REFERRAL_ID", "172751854717"))

CHECK_NORMAL   = int(os.getenv("CHECK_INTERVAL_NORMAL", "300"))  # 5 мин
CHECK_ACTIVE   = int(os.getenv("CHECK_INTERVAL_ACTIVE", "1"))    # 1 сек
ACTIVE_START   = (7, 28)
ACTIVE_END     = (7, 35)
LOGIN_INTERVAL = 2.5 * 60 * 60  # обновляем куку каждые 2.5 часа

MSK = timezone(timedelta(hours=3))
API = "https://emias.info/api-eip/v4/saOrchestrator"
TG_MIRRORS = ["https://api.telegram.org", "https://tg.i-c-a.su"]

# Токены — обновляются при каждом автологине
_tokens = {
    "ei_token": "",
    "cookie":   "",
}

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


# ── Автологин через Playwright ────────────────────────────────────────────────
async def do_login() -> bool:
    """
    Открывает браузер, логинится в ЕМИАС, забирает session-cookie и Ei-Token.
    Браузер закрывается сразу после получения токенов.
    """
    from playwright.async_api import async_playwright, TimeoutError as PwTimeout
    log.info("Запускаю браузер для обновления сессии...")

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
                locale="ru-RU",
            )
            page = await context.new_page()

            # Перехватываем все запросы чтобы поймать Ei-Token
            ei_token_found = []

            async def on_request(request):
                ei = request.headers.get("ei-token") or request.headers.get("Ei-Token")
                if ei and ei not in ei_token_found:
                    ei_token_found.append(ei)

            page.on("request", on_request)

            # Открываем ЕМИАС
            await page.goto("https://emias.info/app/einfo/", wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(2)

            # Ищем кнопку входа
            for selector in ["text=Войти", "a:has-text('Войти')", "button:has-text('Войти')"]:
                try:
                    btn = page.locator(selector).first
                    if await btn.is_visible(timeout=2000):
                        await btn.click()
                        await asyncio.sleep(1)
                        break
                except Exception:
                    pass

            # Переключаемся на логин/пароль если нужно
            for selector in ["text=Войти с логином и паролем", "text=Логин и пароль", "text=Другой способ"]:
                try:
                    btn = page.locator(selector).first
                    if await btn.is_visible(timeout=2000):
                        await btn.click()
                        await asyncio.sleep(1)
                        break
                except Exception:
                    pass

            # Вводим логин
            login_input = page.locator("input[type='text'], input[name='login'], input[id*='login']").first
            await login_input.wait_for(state="visible", timeout=15000)
            await login_input.fill(EMIAS_LOGIN)

            # Вводим пароль
            pass_input = page.locator("input[type='password']").first
            await pass_input.fill(EMIAS_PASSWORD)

            # Нажимаем войти
            submit = page.locator("button[type='submit'], button:has-text('Войти')").first
            await submit.click()

            # Ждём загрузки личного кабинета
            try:
                await page.wait_for_url("**/einfo/**", timeout=20000)
            except PwTimeout:
                log.error("Логин не прошёл — не попали в личный кабинет")
                await browser.close()
                return False

            await asyncio.sleep(3)

            # Ждём пока появится Ei-Token в запросах
            for _ in range(10):
                if ei_token_found:
                    break
                await asyncio.sleep(1)

            # Собираем cookies
            cookies = await context.cookies()
            cookie_str = "; ".join(
                f"{c['name']}={c['value']}"
                for c in cookies
                if "emias.info" in c.get("domain", "")
            )

            await browser.close()

            if not cookie_str:
                log.error("Cookies не получены после логина")
                return False

            _tokens["cookie"] = cookie_str
            if ei_token_found:
                _tokens["ei_token"] = ei_token_found[-1]
                log.info("Ei-Token получен: %s...", ei_token_found[-1][:20])
            else:
                log.warning("Ei-Token не перехвачен, используем cookie")

            log.info("Логин успешен, cookie обновлены")
            return True

    except Exception as e:
        log.error("Ошибка логина: %s", e)
        return False


# ── Проверка талонов ──────────────────────────────────────────────────────────
async def check_available(session):
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
                "omsNumber":           OMS_NUMBER,
                "birthDate":           BIRTH_DATE,
                "referralId":          REFERRAL_ID,
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

        # Первый логин при старте
        log.info("Первый логин...")
        if not await do_login():
            await tg(session,
                "❌ <b>Не удалось залогиниться при запуске</b>\n\n"
                "Проверьте <code>EMIAS_LOGIN</code> и <code>EMIAS_PASSWORD</code> в Timeweb."
            )
            return

        # Проверяем что API работает
        test = await check_available(session)
        if test == "unauthorized":
            await tg(session, "❌ <b>Авторизация не прошла после логина</b>")
            return

        log.info("Авторизация OK")
        await tg(session,
            "🤖 <b>Бот запущен и авторизован</b>\n\n"
            "Мониторю: Эзофагогастродуоденоскопия, колоноилеоскопия (скрининг)\n"
            "🏥 МНЦ ГКБ им. С.П. Боткина\n\n"
            f"🔴 Активный режим 07:28–07:35 МСК — каждую секунду\n"
            f"🟢 Обычный режим — каждые {CHECK_NORMAL // 60} мин\n"
            f"🔄 Автообновление сессии — каждые 2.5 часа"
        )

        last_mode    = None
        last_login   = datetime.now(MSK)

        while True:
            try:
                now      = datetime.now(MSK)
                interval = get_interval()
                mode     = "active" if interval == CHECK_ACTIVE else "normal"

                # Уведомляем о смене режима
                if mode != last_mode:
                    msk_now = now.strftime("%H:%M")
                    if mode == "active":
                        await tg(session, f"🔴 <b>Активный режим</b> [{msk_now} МСК] — каждую секунду")
                    else:
                        await tg(session, f"🟢 Обычный режим [{msk_now} МСК] — каждые {interval // 60} мин")
                    last_mode = mode

                # Обновляем сессию каждые 2.5 часа
                if (now - last_login).total_seconds() > LOGIN_INTERVAL:
                    log.info("Обновляю сессию через логин...")
                    if await do_login():
                        last_login = datetime.now(MSK)
                        log.info("Сессия обновлена успешно")
                    else:
                        log.error("Не удалось обновить сессию, продолжаю со старой")

                # Проверяем талоны
                result = await check_available(session)

                if result == "unauthorized":
                    log.warning("401 — принудительно обновляю сессию...")
                    if await do_login():
                        last_login = datetime.now(MSK)
                        await tg(session, "🔄 Сессия обновлена автоматически")
                    else:
                        await tg(session,
                            "❌ <b>Не удалось обновить сессию</b>\n\n"
                            "Проверьте <code>EMIAS_LOGIN</code> и <code>EMIAS_PASSWORD</code> в Timeweb."
                        )
                        await asyncio.sleep(300)
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
