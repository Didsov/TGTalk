import asyncio

from src.integrations.sbis import get_open_companies_by_date


async def main():
    organizations = await get_open_companies_by_date('2026-08-07')

    print(f"Получено: {len(organizations)}")
    print(f"Сохранено: {len(organizations)}")


asyncio.run(main())
