import asyncio
import logging
from contextvars import ContextVar
from typing import Optional, Any, Iterable
from odmantic import AIOEngine

logger = logging.getLogger(__name__)


class AIOEngineProxy(AIOEngine):
    """
    A proxy engine that inherits all behavior from AIOEngine but overrides `save`.

    The overridden `save` will:
    - If the model has a `save` member function, call it with the engine instance.
    - Else, if any direct part/attribute of the model has a `save` member function, call those with the engine instance.
    - Otherwise, fall back to the original AIOEngine.save implementation.

    Notes:
    - Supports single model instances or iterables of model instances (list/tuple/set).
    - Avoids recursion by delegating to super().save for the fallback.
    """

    async def save(self, obj: Any, *args, **kwargs) -> Any:  # type: ignore[override]
        # Handle collections of models
        if isinstance(obj, (list, tuple, set)):
            results = []
            for item in obj:
                results.append(await self._save_one(item, *args, **kwargs))
            return results
        # Single model
        return await self._save_one(obj, *args, **kwargs)

    async def save_unchecked(self, obj: Any, *args, **kwargs) -> Any:
        """
        Save without checking for custom save methods.
        This is a direct call to the original AIOEngine.save.
        """
        return await super().save(obj, *args, **kwargs)

    async def save_all(self, instances: Iterable[Any], *args, **kwargs) -> list[Any]:
        """Save multiple model instances, preserving input order."""
        results = []
        for instance in instances:
            results.append(await self._save_one(instance, *args, **kwargs))
        return results

    async def _save_one(self, model: Any, *args, **kwargs) -> Any:
        # 1) Try model's own `save`
        if await self._maybe_call_custom_save(model):
            return None

        # 2) Try parts' `save` (shallow scan of attributes and common containers)
        parts_saved = await self._call_parts_saves(model)

        # 3) If neither the model nor any part handled saving, fallback to AIOEngine
        if not parts_saved:
            try:
                # Motor/Odmantic engine is bound to a specific client, which is bound to a loop.
                # If the loop changed, we need to ensure we use the current loop's client.
                # However, super().save uses self.client which might be from a different loop.
                return await super().save(model, *args, **kwargs)
            except RuntimeError as e:
                if "attached to a different loop" in str(e):
                    # Log the mismatch for debugging
                    engine_loop = self.client.get_io_loop()
                    try:
                        current_loop = asyncio.get_running_loop()
                    except RuntimeError:
                        current_loop = None
                    
                    logger.error(
                        f"Loop mismatch detected in AIOEngineProxy.save for {model}. "
                        f"Engine loop: {id(engine_loop)}, "
                        f"Current loop: {id(current_loop) if current_loop else 'None'}"
                    )
                    
                    # Try a desperate measure: if we are in a different loop, we might need to 
                    # use a different engine/client that is bound to the current loop.
                    # This is tricky because AIOEngine doesn't easily allow changing the client.
                    raise RuntimeError(
                        f"Loop mismatch detected in AIOEngineProxy.save for {model}. "
                        f"Engine loop: {id(engine_loop)}, "
                        f"Current loop: {id(current_loop) if current_loop else 'None'}"
                    ) from e
                raise e
        return None

    async def _maybe_call_custom_save(self, target: Any) -> bool:
        """
        If `target` has a callable `save`, invoke it with this engine.
        Tries positional engine and keyword engine forms, awaits if coroutine.
        Returns True if a save was invoked, False otherwise.
        """
        if not hasattr(target, "save"):
            return False
        save_attr = getattr(target, "save")
        if not callable(save_attr):
            raise AttributeError(f"Model {target} has no callable `save` method")
        return await target.save(self)

    async def _call_parts_saves(self, model: Any) -> bool:
        """
        Inspect immediate parts/attributes of the model and call their `save` if present.
        Handles:
          - Direct attributes from vars(model)
          - Items inside lists/tuples/sets
          - Values of dicts
        Returns True if at least one part save was called.
        """
        any_saved = False

        def iter_parts(root: Any) -> Iterable[Any]:
            # Direct attributes
            try:
                for v in vars(root).values():
                    yield v
            except TypeError:
                # Objects without __dict__
                pass

            # For convenience, also scan common container-typed attributes one level deep
            try:
                for v in vars(root).values():
                    if isinstance(v, (list, tuple, set)):
                        for item in v:
                            yield item
                    elif isinstance(v, dict):
                        for item in v.values():
                            yield item
            except TypeError:
                pass

        seen_ids = set()
        for part in iter_parts(model):
            pid = id(part)
            if pid in seen_ids:
                continue
            seen_ids.add(pid)

            if await self._maybe_call_custom_save(part):
                any_saved = True

        return any_saved


current_engine_context: ContextVar[Optional[AIOEngineProxy]] = ContextVar(
    "current_engine", default=None
)


def get_current_engine_from_context() -> Optional[AIOEngineProxy]:
    """Get the current engine from context"""
    return current_engine_context.get()
