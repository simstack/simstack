from typing import Optional

from odmantic import Field, Model


class Tag(Model):
    name: str = Field(unique=True)
    description: Optional[str] = None

    model_config = {
        "collection": "tags",
    }
