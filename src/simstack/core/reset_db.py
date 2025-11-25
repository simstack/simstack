from simstack.core.context import context


async def main():
    context.initialize()
    await context.db.reset_database()


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
