from typing import Optional

from odmantic import EmbeddedModel, ObjectId, Model

import logging

logger = logging.getLogger(__name__)

class NamedDataReference(EmbeddedModel):
    variable_name: str
    variable_mapping: str
    reference: ObjectId
    
    def __repr__(self) -> str:
        return f"NamedDataReference(variable_name='{self.variable_name}', variable_mapping='{self.variable_mapping}', reference='{self.reference}')"
    
    @classmethod
    def from_variable(cls, variable: Model, variable_name: Optional[str] = None,  task_id: Optional[str] = None):
        from simstack.core.context import context
        if not isinstance(variable, Model):
            logger.error(f"task_id: {task_id} Processing Variable {variable} must be an instance of Model")
            raise ValueError(f"Variable must be an instance of Model")

        variable_class_name = variable.__class__.__name__ 
        table_name = context.model_mappings.get_by_name(variable_class_name)
        if table_name is None:
            logger.error(f"Could not find table name for {variable_class_name}")
            raise ValueError(f"Could not find table name for {variable_class_name}")
        if variable_name is None:
            # Check if variable already has a field_name we can use
            if hasattr(variable, "field_name") and variable.field_name:
                variable_name = variable.field_name
            else:
                variable_name = "variable"

        return cls(variable_name=variable_name, variable_mapping=table_name.mapping, reference=variable.id)
