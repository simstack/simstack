import asyncio
import functools

import nest_asyncio

nest_asyncio.apply()


def async_helper(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        return asyncio.run(func(*args, **kwargs))

    return wrapper
