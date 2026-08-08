import asyncio

from src.integrations.sbis import get_open_companies_by_date
from src.storage import NewClientStorage


async def main():
    organizations = await get_open_companies_by_date("2026-08-06")

    storage = NewClientStorage("data/clients.db")
    storage.initialize()
    saved = storage.save_sbis_list(organizations)

    print(f"Получено: {len(organizations)}")
    print(f"Сохранено: {len(saved)}")


asyncio.run(main())