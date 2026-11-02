from typing import List, Optional

from simstack.models.document import Model, Field, ObjectId


class Project(Model):
    field_name: str = Field(unique=True)
    description: Optional[str] = None
    tag_ids: List[ObjectId] = Field(default_factory=list)

    model_config = {
        "collection": "projects",
    }
