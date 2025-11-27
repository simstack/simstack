import pytest
import pytest_asyncio
from odmantic import Model

from simstack.core.context import context
from simstack.models.models import ModelMapping
from simstack.models.pickle_models import ClassPickle
from simstack.util.importer import import_class


# Define a class that will be used for testing import_class (avoid pytest collection)
class SampleClass(Model):
    """A simple class for testing import_class."""

    value: str = "default"

    def get_value(self):
        return self.value


# Define a test class in a different module
class AnotherSampleClass(Model):
    """Another sample class for testing import_class."""

    name: str = "default"

    def get_name(self):
        return self.name


@pytest_asyncio.fixture
async def setup_model_mapping():
    """Set up a ModelMapping entry for SampleClass."""
    # Create a ModelMapping entry for SampleClass
    model_mapping = ModelMapping(
        name="SampleClass",
        mapping="tests.core.test_import_class.SampleClass",
        collection_name="test_collection"
    )
    await context.db.save(model_mapping)

    yield context.db.engine

    # Clean up - remove the test data
    try:
        await context.db.delete(model_mapping)
    except Exception:
        pass  # Ignore cleanup errors

@pytest.mark.asyncio
async def test_import_class_regular():
    """Test importing a class using regular Python import."""
    # Import the SampleClass using import_class
    cls = await import_class("tests.core.test_import_class.SampleClass")

    # Verify that the class was imported correctly
    assert cls is SampleClass

    # Create an instance and verify it works
    instance = cls(value="test")
    assert instance.get_value() == "test"


@pytest.mark.asyncio
async def test_import_class_from_model_mapping(setup_model_mapping):
    """Test importing a class using ModelMapping."""
    # Import the SampleClass using import_class

    cls = await import_class("tests.core.test_import_class.SampleClass")

    # Verify that the class was imported correctly
    assert cls is SampleClass

    # Create an instance and verify it works
    instance = cls(value="test")
    assert instance.get_value() == "test"


@pytest.mark.asyncio
async def test_import_class_nonexistent():
    """Test importing a non-existent class."""
    # Try to import a non-existent class
    with pytest.raises(
        LookupError, match="Error finding ModelMapping for NonExistentClass"
    ):
        await import_class("tests.core.test_import_class.NonExistentClass")


if __name__ == "__main__":
    pytest.main(["-xvs", __file__])
