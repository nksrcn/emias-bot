"""Запускает auth_server и bot параллельно."""
import asyncio
import logging
from auth_server import start_server
from bot import run

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


async def main():
    # Стартуем веб-сервер для авторизации
    runner = await start_server(port=8080)
    # Стартуем бота
    try:
        await run()
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
