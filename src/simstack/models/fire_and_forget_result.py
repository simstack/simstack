from typing import Dict, Any
from odmantic import Model
from simstack.models.simstack_model import simstack_model

@simstack_model
class FireAndForgetResult(Model):
    call_path: str
    models: Dict[str, Any]
    success: bool
