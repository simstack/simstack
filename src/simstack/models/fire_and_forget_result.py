from typing import Dict, Any
from odmantic import Model, Field
from simstack.models.simstack_model import simstack_model

@simstack_model
class FireAndForgetResult(Model):
    """
    Model representing the result of a fire-and-forget task.

    Attributes:
        call_path: The full call path of the node.
        models: A dictionary mapping argument/result names to their values.
        success: Whether the task was successful.
        next_step: Whether this result triggers a next step.
    """
    call_path: str = Field(description="The full call path of the node")
    models: Dict[str, Any] = Field(description="A dictionary mapping argument/result names to their values")
    success: bool = Field(description="Whether the task was successful")
    next_step: bool = Field(default=False, description="Whether this result triggers a next step")