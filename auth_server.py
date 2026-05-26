import asyncio
import json
import logging
import os
from aiohttp import web

log = logging.getLogger(__name__)

_state = {
    "cookies_ready": None,
    "new_cookies": None,
}


async def handle_login_page(request):
    """
    Страница авторизации.
    Открывает ЕМИАС в новой вкладке, затем просит пользователя
    вернуться и нажать кнопку подтверждения.
    Бот перехватывает cookies через Playwright после подтверждения.
    """
    html = """<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>ЕМИАС — Авторизация бота</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            background: #f0f2f5; min-height: 100vh;
            display: flex; flex-direction: column; align-items: center;
            justify-content: center; padding: 20px;
        }
        .card {
            background: white; border-radius: 16px;
            padding: 32px 28px; max-width: 420px; width: 100%;
            box-shadow: 0 4px 24px rgba(0,0,0,0.08);
        }
        .logo {
            font-size: 48px; text-align: center; margin-bottom: 16px;
        }
        h1 {
            font-size: 20px; font-weight: 700; color: #1e293b;
            text-align: center; margin-bottom: 8px;
        }
        .subtitle {
            font-size: 14px; color: #64748b;
            text-align: center; margin-bottom: 28px;
        }
        .steps {
            display: flex; flex-direction: column; gap: 16px;
            margin-bottom: 28px;
        }
        .step {
            display: flex; align-items: flex-start; gap: 14px;
        }
        .step-num {
            width: 32px; height: 32px; border-radius: 50%;
            background: #2563eb; color: white;
            display: flex; align-items: center; justify-content: center;
            font-size: 14px; font-weight: 700; flex-shrink: 0;
        }
        .step-num.done { background: #16a34a; }
        .step-text { padding-top: 6px; font-size: 15px; color: #374151; line-height: 1.4; }
        .step-text b { color: #1e293b; }
        .btn {
            display: block; width: 100%; padding: 14px;
            border-radius: 10px; border: none; cursor: pointer;
            font-size: 16px; font-weight: 600; text-align: center;
            text-decoration: none; transition: opacity 0.2s;
        }
        .btn:hover { opacity: 0.9; }
        .btn-primary { background: #2563eb; color: white; margin-bottom: 12px; }
        .btn-success { background: #16a34a; color: white; display: none; }
        .btn-success.show { display: block; }
        .status {
            margin-top: 16px; padding: 12px 16px;
            border-radius: 8px; font-size: 14px;
            text-align: center; display: none;
        }
        .status.show { display: block; }
        .status.success { background: #dcfce7; color: #16a34a; }
        .status.waiting { background: #fef9c3; color: #854d0e; }
        .divider {
            border: none; border-top: 1px solid #e5e7eb;
            margin: 20px 0;
        }
    </style>
</head>
<body>
    <div class="card">
        <div class="logo">🤖</div>
        <h1>Авторизация ЕМИАС-бота</h1>
        <p class="subtitle">Выполните 3 простых шага</p>

        <div class="steps">
            <div class="step">
                <div class="step-num" id="n1">1</div>
                <div class="step-text">Нажмите кнопку ниже — откроется <b>ЕМИАС</b> в новой вкладке</div>
            </div>
            <div class="step">
                <div class="step-num" id="n2">2</div>
                <div class="step-text">Введите логин и пароль, нажмите <b>«Войти»</b></div>
            </div>
            <div class="step">
                <div class="step-num" id="n3">3</div>
                <div class="step-text">Вернитесь сюда и нажмите <b>«Я вошёл»</b></div>
            </div>
        </div>

        <a href="https://emias.info/app/einfo/" target="_blank" class="btn btn-primary" id="openBtn"
           onclick="onOpen()">
            🔑 Открыть ЕМИАС
        </a>

        <button class="btn btn-success" id="doneBtn" onclick="onDone()">
            ✅ Я вошёл в ЕМИАС
        </button>

        <div class="status waiting" id="status"></div>
    </div>

    <script>
        function onOpen() {
            setTimeout(() => {
                document.getElementById('doneBtn').classList.add('show');
                document.getElementById('n1').classList.add('done');
                document.getElementById('n1').textContent = '✓';
            }, 1000);
        }

        async function onDone() {
            document.getElementById('doneBtn').disabled = true;
            document.getElementById('doneBtn').textContent = 'Проверяю...';
            document.getElementById('n2').classList.add('done');
            document.getElementById('n2').textContent = '✓';
            document.getElementById('n3').classList.add('done');
            document.getElementById('n3').textContent = '✓';

            const status = document.getElementById('status');

            try {
                const r = await fetch('/auth/notify', { method: 'POST' });
                const d = await r.json();
                if (d.ok) {
                    status.textContent = '✅ Отлично! Бот получил авторизацию и продолжает работу. Можете закрыть эту страницу.';
                    status.className = 'status show success';
                    document.getElementById('doneBtn').textContent = '✅ Готово!';
                } else {
                    throw new Error('not ok');
                }
            } catch(e) {
                status.textContent = '⚠️ Что-то пошло не так. Попробуйте ещё раз.';
                status.className = 'status show waiting';
                document.getElementById('doneBtn').disabled = false;
                document.getElementById('doneBtn').textContent = '✅ Я вошёл в ЕМИАС';
            }
        }
    </script>
</body>
</html>"""
    return web.Response(text=html, content_type="text/html")


async def handle_notify(request):
    """Пользователь подтвердил что залогинился — сигналим боту."""
    if _state["cookies_ready"]:
        _state["cookies_ready"].set()
    return web.json_response({"ok": True})


async def handle_status(request):
    ready = _state["cookies_ready"] and _state["cookies_ready"].is_set()
    return web.json_response({"ready": ready})


async def handle_cookies(request):
    cookies = _state.get("new_cookies")
    if cookies:
        _state["cookies_ready"].clear()
        _state["new_cookies"] = None
        return web.json_response({"cookies": cookies})
    return web.json_response({"cookies": None})


async def handle_set_cookies(request):
    data = await request.json()
    _state["new_cookies"] = data.get("cookies")
    if _state["cookies_ready"]:
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


async def start_server(port: int = int(os.getenv("PORT", "8080"))):
    _state["cookies_ready"] = asyncio.Event()
    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info("Auth server started on port %d", port)
    return runner
