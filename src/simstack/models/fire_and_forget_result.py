from typing import Dict, AsyncIterator, Tuple
from odmantic import Model
from simstack.models.simstack_model import simstack_model
from simstack.models.named_data_reference import NamedDataReference

@simstack_model
class FireAndForgetResult(Model):
    call_path: str
    input_models: Dict[str, NamedDataReference]
    result_models: Dict[str, NamedDataReference]
    success: bool
    next_step_started: bool = False

    async def iter_input_models(self) -> AsyncIterator[Tuple[str, Model]]:
        from simstack.core.context import context
        db = context.db
        for name, ref in self.input_models.items():
            model = await db.find_one_by_model_name(ref.variable_mapping, ref.reference)
            yield name, model

    async def iter_result_models(self) -> AsyncIterator[Tuple[str, Model]]:
        from simstack.core.context import context
        db = context.db
        for name, ref in self.result_models.items():
            model = await db.find_one_by_model_name(ref.variable_mapping, ref.reference)
            yield name, model
    
