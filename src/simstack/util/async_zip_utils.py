async def async_zip(async_gen1, async_gen2):
    """Async version of zip for two async generators"""
    async for item1, item2 in async_zip_multiple(async_gen1, async_gen2):
        yield item1, item2


async def async_zip_multiple(*async_generators):
    """Async version of zip for multiple async generators"""
    iterators = [aiter(ag) for ag in async_generators]

    while True:
        try:
            values = []
            for it in iterators:
                values.append(await anext(it))
            yield tuple(values)
        except StopAsyncIteration:
            break
