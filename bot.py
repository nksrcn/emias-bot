import asyncio
import json
import logging
import os
from datetime import datetime, timezone, timedelta
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

# ── Настройки ─────────────────────────────────────────────────────────────────
EMIAS_LOGIN    = os.environ["EMIAS_LOGIN"]
EMIAS_PASSWORD = os.environ["EMIAS_PASSWORD"]
TG_TOKEN       = os.environ["TG_TOKEN"]
TG_CHAT_ID     = os.environ["TG_CHAT_ID"]
RAILWAY_URL    = os.environ["RAILWAY_PUBLIC_DOMAIN"]  # автоматически задаётся Railway

CHECK_INTERVAL_NORMAL = int(os.getenv("CHECK_INTERVAL_NORMAL", "300"))  # 5 минут
CHECK_INTERVAL_ACTIVE = int(os.getenv("CHECK_INTERVAL_ACTIVE", "5"))    # 5 секунд
ACTIVE_START = (7, 25)
ACTIVE_END   = (7, 45)

EMIAS_URL  = "https://emias.info/app/einfo/#/referrals"
COOKIES_FILE = "/tmp/emias_cookies.json"
MSK = timezone(timedelta(hours=3))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ── Интервал проверки ─────────────────────────────────────────────────────────
def get_interval() -> int:
    msk = datetime.now(MSK)
    h, m = msk.hour, msk.minute
    active = (
        (h == ACTIVE_START[0] and m >= ACTIVE_START[1]) or
        (h == ACTIVE_END[0]   and m <= ACTIVE_END[1])   or
        (ACTIVE_START[0] < h < ACTIVE_END[0])
    )
    return CHECK_INTERVAL_ACTIVE if active else CHECK_INTERVAL_NORMAL


# ── Telegram ──────────────────────────────────────────────────────────────────
async def tg(session, text: str, buttons=None):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"}
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": buttons}
    try:
        async with session.post(url, json=payload) as r:
            if r.status != 200:
                log.error("Telegram error: %s", await r.text())
    except Exception as e:
        log.error("Telegram failed: %s", e)


async def tg_auth_request(session):
    """Шлёт в Telegram кнопку для авторизации."""
    login_url = f"https://{RAILWAY_URL}/auth/login"
    await tg(session,
        "🔐 <b>Нужна авторизация в ЕМИАС</b>\n\n"
        "Нажмите кнопку ниже, войдите в ЕМИАС.\n"
        "Бот автоматически продолжит работу после входа.",
        buttons=[[{"text": "🔑 Войти в ЕМИАС", "url": login_url}]]
    )


# ── Cookies ───────────────────────────────────────────────────────────────────
def save_cookies(cookies: list):
    with open(COOKIES_FILE, "w") as f:
        json.dump(cookies, f)
    log.info("Cookies сохранены (%d шт.)", len(cookies))


def load_cookies() -> list | None:
    try:
        with open(COOKIES_FILE) as f:
            return json.load(f)
    except Exception:
        return None


# ── Авторизация ───────────────────────────────────────────────────────────────
async def try_login_with_password(page) -> bool:
    """Пробует войти логин+пароль автоматически."""
    try:
        await page.goto("https://emias.info/app/einfo/", wait_until="networkidle", timeout=30000)
        await asyncio.sleep(2)

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

        login_input = page.locator("input[type='text'], input[name='login'], input[id*='login']").first
        await login_input.wait_for(state="visible", timeout=10000)
        await login_input.fill(EMIAS_LOGIN)

        pass_input = page.locator("input[type='password']").first
        await pass_input.fill(EMIAS_PASSWORD)

        submit = page.locator("button[type='submit'], button:has-text('Войти')").first
        await submit.click()

        await page.wait_for_url("**/einfo/**", timeout=15000)
        await asyncio.sleep(2)

        # Сохраняем свежие cookies
        cookies = await page.context.cookies()
        save_cookies(cookies)
        log.info("Автологин успешен")
        return True
    except Exception as e:
        log.warning("Автологин не удался: %s", e)
        return False


async def restore_session(context, page) -> bool:
    """Пробует восстановить сессию из сохранённых cookies."""
    cookies = load_cookies()
    if not cookies:
        return False
    try:
        await context.add_cookies(cookies)
        await page.goto(EMIAS_URL, wait_until="networkidle", timeout=20000)
        await asyncio.sleep(2)
        # Проверяем что мы залогинены (нет редиректа на login)
        if "login" in page.url or "auth" in page.url:
            return False
        # Проверяем наличие элементов личного кабинета
        cabinet = page.locator("text=Направления, text=Записи, text=Новая запись").first
        if await cabinet.is_visible(timeout=5000):
            log.info("Сессия восстановлена из cookies")
            return True
    except Exception as e:
        log.warning("Восстановление сессии не удалось: %s", e)
    return False


async def wait_for_manual_login(http_session, context) -> list | None:
    """
    Ждёт пока пользователь нажмёт 'Я вошёл' на странице авторизации.
    После этого перехватывает cookies из Playwright context.
    """
    log.info("Жду подтверждения ручного логина...")
    for _ in range(720):  # ждём максимум 1 час
        await asyncio.sleep(5)
        try:
            async with http_session.get("http://localhost:8080/auth/status") as r:
                data = await r.json()
                if data.get("ready"):
                    log.info("Пользователь подтвердил логин, перехватываю cookies...")
                    # Открываем ЕМИАС в Playwright и забираем cookies
                    tmp_page = await context.new_page()
                    await tmp_page.goto("https://emias.info/app/einfo/", wait_until="networkidle", timeout=30000)
                    await asyncio.sleep(2)
                    cookies = await context.cookies()
                    await tmp_page.close()
                    if cookies:
                        save_cookies(cookies)
                        log.info("Перехвачено %d cookies", len(cookies))
                        return cookies
        except Exception as e:
            log.warning("Ошибка при ожидании логина: %s", e)
    return None


# ── Проверка и запись ─────────────────────────────────────────────────────────
async def check_and_book(page) -> str:
    if "login" in page.url or "auth" in page.url:
        return "need_relogin"

    await page.goto(EMIAS_URL, wait_until="networkidle", timeout=30000)
    await asyncio.sleep(2)

    # Ищем карточку направления
    card = page.locator("text=Эзофагогастродуоденоскопия").first
    try:
        await card.wait_for(state="visible", timeout=10000)
    except PlaywrightTimeout:
        log.warning("Карточка направления не найдена")
        return "no_slots"

    # Кнопка «Записаться»
    book_btn = page.locator(
        "//div[contains(., 'Эзофагогастродуоденоскопия')]//button[contains(text(), 'Записаться')] | "
        "//li[contains(., 'Эзофагогастродуоденоскопия')]//button[contains(text(), 'Записаться')]"
    ).first
    if not await book_btn.is_visible(timeout=2000):
        book_btn = page.locator("button:has-text('Записаться')").first

    try:
        await book_btn.wait_for(state="visible", timeout=5000)
        await book_btn.click()
        log.info("Нажал 'Записаться'")
    except PlaywrightTimeout:
        return "no_slots"

    await asyncio.sleep(3)

    # Нет талонов?
    no_slots = page.locator("text=нет времени для самостоятельной записи, text=Нет доступных талонов, text=Попробуйте позже").first
    if await no_slots.is_visible(timeout=4000):
        return "no_slots"

    log.info("🎉 Талоны появились! Выбираю слот...")

    # Выбираем первый доступный слот
    slot = page.locator(
        "button.slot, .time-slot:not(.disabled), [class*='slot']:not([class*='disabled']), "
        "td.available, .calendar-day:not(.disabled)"
    ).first
    try:
        await slot.wait_for(state="visible", timeout=8000)
        await slot.click()
        await asyncio.sleep(2)
    except PlaywrightTimeout:
        log.warning("Слоты не найдены")
        return "no_slots"

    # Подтверждаем запись
    confirm = page.locator(
        "button:has-text('Подтвердить'), button:has-text('Записаться'), button:has-text('Готово'), button[type='submit']"
    ).last
    try:
        await confirm.wait_for(state="visible", timeout=8000)
        await confirm.click()
        await asyncio.sleep(3)
    except PlaywrightTimeout:
        pass

    # Проверяем успех
    success = page.locator("text=Вы записаны, text=Запись подтверждена, text=Успешно").first
    if await success.is_visible(timeout=5000):
        return "booked"
    if any(x in page.url for x in ["success", "confirm", "appointment"]):
        return "booked"

    return "no_slots"


# ── Главный цикл ──────────────────────────────────────────────────────────────
async def run():
    import aiohttp
    async with aiohttp.ClientSession() as http:
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

            # ── Авторизация при старте ────────────────────────────────────────
            log.info("Попытка восстановить сессию из cookies...")
            logged_in = await restore_session(context, page)

            if not logged_in:
                log.info("Cookies устарели, пробую автологин...")
                logged_in = await try_login_with_password(page)

            if not logged_in:
                log.warning("Автологин не удался, запрашиваю ручной логин...")
                await tg(http, "🤖 Бот запущен, но требуется авторизация.")
                await tg_auth_request(http)
                new_cookies = await wait_for_manual_login(http, context)
                if new_cookies:
                    save_cookies(new_cookies)
                    await context.add_cookies(new_cookies)
                    logged_in = True
                else:
                    await tg(http, "❌ Авторизация не получена за 1 час. Перезапустите бота.")
                    return

            await tg(http, (
                "🤖 <b>Бот запущен и авторизован</b>\n\n"
                f"🔴 Активный режим 07:25–07:45 МСК — каждые {CHECK_INTERVAL_ACTIVE} сек\n"
                f"🟢 Обычный режим — каждые {CHECK_INTERVAL_NORMAL // 60} мин"
            ))

            last_mode = None

            while True:
                try:
                    interval = get_interval()
                    mode = "active" if interval == CHECK_INTERVAL_ACTIVE else "normal"

                    if mode != last_mode:
                        msk = datetime.now(MSK).strftime("%H:%M")
                        if mode == "active":
                            await tg(http, f"🔴 <b>Активный режим</b> [{msk} МСК]\nПроверяю каждые {interval} сек")
                        else:
                            await tg(http, f"🟢 Обычный режим [{msk} МСК] — каждые {interval // 60} мин")
                        last_mode = mode

                    result = await check_and_book(page)

                    if result == "booked":
                        msk_time = datetime.now(MSK).strftime("%d.%m.%Y %H:%M")
                        await tg(http,
                            f"✅ <b>Записано!</b>\n\n"
                            f"Талон на Эзофагогастродуоденоскопию, колоноилеоскопию (скрининг) занят.\n"
                            f"⏰ Время записи: {msk_time} МСК"
                        )
                        log.info("Запись успешна! Останавливаю бота.")
                        break

                    elif result == "need_relogin":
                        log.warning("Сессия истекла")
                        # Пробуем автологин
                        if await try_login_with_password(page):
                            await tg(http, "🔄 Сессия обновлена автоматически")
                        else:
                            # Просим пользователя залогиниться
                            await tg_auth_request(http)
                            new_cookies = await wait_for_manual_login(http, context)
                            if new_cookies:
                                save_cookies(new_cookies)
                                await context.add_cookies(new_cookies)
                                await tg(http, "✅ Авторизация обновлена, продолжаю мониторинг")
                            else:
                                await tg(http, "❌ Авторизация не получена. Проверьте бота.")

                    elif result == "no_slots":
                        log.info("[%s] Талонов нет", datetime.now(MSK).strftime("%H:%M:%S"))

                except Exception as e:
                    log.error("Ошибка: %s", e)
                    await tg(http, f"⚠️ Ошибка: {e}")

                await asyncio.sleep(get_interval())

            await browser.close()
