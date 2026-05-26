"""
Мини веб-сервер на Railway.
Когда сессия истекает — бот шлёт в Telegram кнопку со ссылкой.
Вы открываете ссылку, логинитесь в ЕМИАС прямо в браузере через noVNC,
бот перехватывает cookies и продолжает работу.
"""
import asyncio
import json
import logging
import os
from aiohttp import web

log = logging.getLogger(__name__)

# Глобальное хранилище: сюда бот кладёт browser context, сюда же пишет новые cookies
_state = {
    "page": None,           # Playwright page для авторизации
    "cookies_ready": asyncio.Event() if False else None,  # инициализируется в setup()
    "new_cookies": None,
}


def setup(loop):
    _state["cookies_ready"] = asyncio.Event()


async def handle_login_page(request):
    """Отдаёт HTML-страницу с iframe на ЕМИАС для ручного логина."""
    html = """<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>ЕМИАС — Авторизация бота</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, sans-serif; background: #f0f2f5; }
        .header {
            background: #2563eb; color: white;
            padding: 16px 24px;
            display: flex; align-items: center; gap: 12px;
        }
        .header h1 { font-size: 18px; font-weight: 600; }
        .header p { font-size: 13px; opacity: 0.85; margin-top: 2px; }
        .steps {
            background: white; border-bottom: 1px solid #e5e7eb;
            padding: 12px 24px;
            display: flex; gap: 24px; align-items: center;
            font-size: 13px; color: #374151;
        }
        .step { display: flex; align-items: center; gap: 6px; }
        .step-num {
            width: 22px; height: 22px; border-radius: 50%;
            background: #2563eb; color: white;
            display: flex; align-items: center; justify-content: center;
            font-size: 11px; font-weight: 700; flex-shrink: 0;
        }
        iframe {
            width: 100%; border: none;
            height: calc(100vh - 100px);
            display: block;
        }
        #status {
            position: fixed; bottom: 20px; right: 20px;
            background: #1e293b; color: white;
            padding: 10px 18px; border-radius: 8px;
            font-size: 13px; display: none;
        }
        #status.show { display: block; }
        #status.success { background: #16a34a; }
    </style>
</head>
<body>
    <div class="header">
        <div>
            <h1>🤖 Авторизация ЕМИАС-бота</h1>
            <p>Войдите в ЕМИАС ниже — бот автоматически продолжит работу</p>
        </div>
    </div>
    <div class="steps">
        <div class="step"><div class="step-num">1</div> Введите логин и пароль ЕМИАС</div>
        <div class="step"><div class="step-num">2</div> Нажмите «Войти»</div>
        <div class="step"><div class="step-num">3</div> Закройте эту страницу</div>
    </div>
    <iframe src="https://emias.info/app/einfo/" id="frame"></iframe>
    <div id="status"></div>

    <script>
        // Проверяем каждые 3 сек готовность cookies на сервере
        async function checkReady() {
            try {
                const r = await fetch('/auth/status');
                const d = await r.json();
                if (d.ready) {
                    const s = document.getElementById('status');
                    s.textContent = '✅ Авторизация принята! Бот продолжает работу.';
                    s.className = 'show success';
                    setTimeout(() => window.close(), 3000);
                    return;
                }
            } catch(e) {}
            setTimeout(checkReady, 3000);
        }

        // Слушаем сообщения от iframe (postMessage после логина)
        window.addEventListener('message', async (e) => {
            if (e.data && e.data.type === 'emias_logged_in') {
                await fetch('/auth/notify', { method: 'POST' });
            }
        });

        checkReady();
    </script>
</body>
</html>"""
    return web.Response(text=html, content_type="text/html")


async def handle_notify(request):
    """iframe сообщает что логин прошёл."""
    _state["cookies_ready"].set()
    return web.json_response({"ok": True})


async def handle_status(request):
    """Бот проверяет — готовы ли cookies."""
    ready = _state["cookies_ready"].is_set()
    return web.json_response({"ready": ready})


async def handle_cookies(request):
    """Бот забирает новые cookies после логина."""
    cookies = _state.get("new_cookies")
    if cookies:
        _state["cookies_ready"].clear()
        _state["new_cookies"] = None
        return web.json_response({"cookies": cookies})
    return web.json_response({"cookies": None})


async def handle_set_cookies(request):
    """Бот кладёт свежие cookies (вызывается после перехвата из браузера)."""
    data = await request.json()
    _state["new_cookies"] = data.get("cookies")
    _state["cookies_ready"].set()
    return web.json_response({"ok": True})


def create_app():
    app = web.Application()
    app.router.add_get("/auth/login", handle_login_page)
    app.router.add_get("/auth/status", handle_status)
    app.router.add_post("/auth/notify", handle_notify)
    app.router.add_get("/auth/cookies", handle_cookies)
    app.router.add_post("/auth/set_cookies", handle_set_cookies)
    app.router.add_get("/", lambda r: web.Response(text="EMIAS Bot is running ✅"))
    return app


async def start_server(port: int = 8080):
    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info("Auth server started on port %d", port)
    return runner
