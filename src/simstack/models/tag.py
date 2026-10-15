from typing import Optional

from simstack.models.document import Field, Model


class Tag(Model):
    name: str
    description: Optional[str] = None

    model_config = {
        "collection": "tags",
    }
