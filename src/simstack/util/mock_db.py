import asyncio
from typing import Type, TypeVar, Dict, List, Optional

from odmantic import Model, ObjectId

T = TypeVar("T", bound=Model)


class InMemoryCollection:
    def __init__(self, engine: "InMemoryAIOEngine"):
        self._engine = engine
        self._store: List[Dict] = []

    def __getitem__(self, index: int) -> Dict:
        return self._store[index]

    def __setitem__(self, index: int, value: Dict):
        self._store[index] = value

    def __iter__(self):
        return iter(self._store)


class InMemoryAIOEngine:
    def __init__(self):
        # Stores data as {collection_name: [documents]}
        self._store: Dict[str, InMemoryCollection] = {}

    async def save(self, instance: T) -> T:
        collection_name = (
            instance.__collection__
            if hasattr(instance, "__collection__")
            else instance.__class__.__name__.lower()
        )

        obj_dict = instance.model_dump()

        # Ensure the instance has an ObjectId assigned
        if not instance.id:
            instance.id = ObjectId()
            obj_dict["id"] = instance.id

        # Initialize collection if it doesn't exist
        if collection_name not in self._store:
            self._store[collection_name] = InMemoryCollection(self)

        # Update if already exists
        existing_index = next(
            (
                i
                for i, obj in enumerate(self._store[collection_name])
                if obj["id"] == instance.id
            ),
            None,
        )
        if existing_index is not None:
            self._store[collection_name][existing_index] = obj_dict
        else:
            self._store[collection_name].append(obj_dict)

        return instance

    async def find(self, model: Type[T], **query) -> List[T]:
        collection_name = (
            model.__collection__
            if hasattr(model, "__collection__")
            else model.__name__.lower()
        )
        all_objects = self._store.get(collection_name, [])
        results = []

        for obj_dict in all_objects:
            if all(obj_dict.get(k) == v for k, v in query.items()):
                results.append(model.model_validate(obj_dict))
        return results


async def find_one(self, model: Type[T], condition=None, **query) -> Optional[T]:
    # Handle both keyword arguments and condition objects
    if condition is not None:
        # For conditions like ModelMapping.name == class_name
        # This is a more robust approach to extract field name and value
        condition_str = str(condition)

        # Look for the pattern: field_name == value or field_name: {$eq: value}
        if "==" in condition_str:
            # Handle direct comparison like ModelMapping.name == 'TestClass'
            parts = condition_str.split("==")
            if len(parts) == 2:
                field_name = parts[0].strip().split(".")[-1]
                value = parts[1].strip().strip("'\"")
                query[field_name] = value
        elif "$eq" in condition_str:
            # Handle MongoDB-style query like {'mapping': {'$eq': 'tests.core.test_import_class.TestClass'}}
            import re

            # Extract field name and value using regex
            field_match = re.search(
                r"'(\w+)':\s*\{'?\$eq'?:\s*'([^']+)'", condition_str
            )
            if field_match:
                field_name = field_match.group(1)
                value = field_match.group(2)
                query[field_name] = value
            else:
                # Fallback - try to extract from the string representation
                # Look for pattern like "ModelMapping.field_name"
                field_match = re.search(r"(\w+)\.(\w+)", condition_str)
                if field_match:
                    field_name = field_match.group(2)
                    # Try to extract the value (this is still a fallback)
                    value_match = re.search(r"'([^']+)'(?:[^']*$)", condition_str)
                    if value_match:
                        value = value_match.group(1)
                        query[field_name] = value

    found = await self.find(model, **query)
    return found[0] if found else None


# Example Usage:
if __name__ == "__main__":
    from odmantic import Model

    class BinaryOperationInput(Model):
        arg1: int
        arg2: int

    async def test_engine():
        engine = InMemoryAIOEngine()
        obj = BinaryOperationInput(arg1=1, arg2=2)

        # Saving object
        await engine.save(obj)

        # Finding objects
        found_objs = await engine.find(BinaryOperationInput, arg1=1)
        print("Found objects:", found_objs)

        # Find one object
        found_one = await engine.find_one(BinaryOperationInput, arg2=2)
        print("Found single object:", found_one)

    asyncio.run(test_engine())
