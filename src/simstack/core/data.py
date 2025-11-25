import inspect
import uuid
from typing import get_origin, get_args, Any, Type, Optional

from pydantic import BaseModel
from sqlmodel import SQLModel, Field


def get_recursive(data: Any, key: str):
    elements = key.split(".")
    if len(elements) == 1:
        if key == "$$":
            return data
        else:
            return data.__dict__[key]
    else:
        return get_recursive(data.__dict__[elements[0]], ".".join(elements[1:]))


class Assignment(BaseModel):
    """
    Represents a mapping container class inherited from BaseModel.

    This class stores a default dictionary mapping and provides a callable
    method that enables operations using a source data and a target class.
    It will be used to generate instances of workflow nodes
    or workflow results from the workflow data

    In the mapping dict the keys specify the target attribute names of the target class.

    If the target class is None, the special key "$$" is used to specify that value.
    The value specifies the source attribute name of the source data. The special value "$$" means
    that the value is the source data itself will be used.

    This mechanism allows to map non-class types to each other and to create instances of classes
    where needed.

    For BaseModel classes the assignment is driven by the model_fields of the target class. For fields that
    are BaseModel classes, the assignment is recursive.

    :ivar mapping: Stores the dictionary for mapping values between the
        source and the target class.
    :type mapping: dict
    """

    # TODO doe SQLModel for dict
    id: int = Field(default=None, primary_key=True)
    # mapping: dict = Field(default_factory=dict)

    model_config = {
        "frozen": True,
    }

    def __init__(self, mapping: dict):
        super().__init__(mapping=mapping)

    def __call__(self, target_class: Type[SQLModel] | None, source: Any):
        """
        Create a new instance of the class from the source data

        :param source:
        :return:
        """
        if target_class is None:
            return get_recursive(source, self.mapping["$$"])
        else:
            return target_class.from_data(source, self)


def is_data_type(annotation):
    """Check if a type annotation is or contains Data"""
    # Direct subclass check
    if inspect.isclass(annotation) and issubclass(annotation, Data):
        return True

    # Handle Union types (like Optional[Type])
    origin = get_origin(annotation)
    if origin is not None:
        # For union types (X | Y or Union[X, Y])
        args = get_args(annotation)
        return any(is_data_type(arg) for arg in args)

    return False


class Data(SQLModel, table=False):
    id: Optional[uuid.UUID] = Field(default=uuid.uuid4(), primary_key=True)
    # item_type: str = Field(sa_column_kwargs={"nullable": False})
    #
    # __mapper_args__ = {
    #     "polymorphic_on": "item_type",
    #     "polymorphic_identity": "base_item",
    # }

    # TODO automatically set the type to the name of the class
    # @validator(mode='before')
    # def set_type_to_class_name(cls, data):
    #     if isinstance(data, dict):
    #         # Don't override if explicitly provided
    #         if 'type' not in data:
    #             data['type'] = cls.__name__
    #     return data

    @classmethod
    def from_data(cls, data: SQLModel, assignment: Assignment):
        value_dict = {}
        mapping = assignment.mapping
        for key, field_info in cls.model_fields.items():
            if key == "type":
                value_dict["type"] = cls.__name__
            else:
                annotation = field_info.annotation
                # Check if this field is a Data subclass
                if is_data_type(annotation) and key in mapping:
                    # If mapping is a dict, it's a nested mapping for a Data subclass
                    if isinstance(mapping[key], dict):
                        # Get the concrete subclass to use
                        if inspect.isclass(annotation) and issubclass(annotation, Data):
                            nested_class = annotation
                        else:
                            # TODO Handle Union types - this is simplified, might need enhancement
                            for arg in get_args(annotation):
                                if inspect.isclass(arg) and issubclass(arg, Data):
                                    nested_class = arg
                                    break

                        # Create an instance using the nested mapping
                        # TODO handle error if nested_class is not defined
                        value_dict[key] = nested_class.from_data(data, mapping[key])
                    else:
                        # It's a direct reference, get from source data
                        value_dict[key] = get_recursive(data, mapping[key])
                else:
                    # Not a Data subclass, handle normally
                    value_dict[key] = get_recursive(data, mapping[key])
        return cls(**value_dict)


# Number = float | int
