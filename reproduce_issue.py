from odmantic import Model
from simstack.models.simstack_model import simstack_model

@simstack_model
class TestModel(Model):
    name: str

print(f"TestModel __collection__: {getattr(TestModel, '__collection__', 'MISSING')}")

from simstack.models import FireAndForgetResult
print(f"FireAndForgetResult __collection__: {getattr(FireAndForgetResult, '__collection__', 'MISSING')}")

from simstack.models import NodeModel
print(f"NodeModel __collection__: {getattr(NodeModel, '__collection__', 'MISSING')}")
