from typing import List, Optional

from simstack.models.document import Field, Model, ObjectId


class Project(Model):
    field_name: str
    description: Optional[str] = None
    tag_ids: List[ObjectId] = Field(default_factory=list)

    model_config = {
        "collection": "projects",
    }
