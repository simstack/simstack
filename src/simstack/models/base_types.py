from odmantic import Model, Reference
from pydantic import model_validator

from simstack.models import simstack_model


@simstack_model
class IntData(Model):
    field_name: str = "IntData"
    value: int

    @model_validator(mode="before")
    @classmethod
    def ensure_fieldname(cls, data):
        """Ensure fieldname is set for existing documents"""
        if isinstance(data, dict) and "field_name" not in data:
            data["field_name"] = cls.__name__
        return data

    def make_table_entries(
        self,
        max_recursion_level=1,
        drop_id=True,
        current_level=0,
        visited=None,
        field_prefix="",
    ):
        return {self.field_name: self.value}

    def make_column_defs_instance(
        self,
        table_name=None,
        max_recursion_level=1,
        drop_id=True,
        current_level=0,
        visited=None,
        field_prefix="",
    ):
        return [
            {
                "field": self.field_name,
                "headerName": self.field_name,
                "type": "numericColumn",
            }
        ]

    @classmethod
    def ui_schema(cls, **kwargs) -> dict:
        return {
            "ui:field": "IntDataField"
        }  # This tells RJSF to use the custom component


@simstack_model
class FloatData(Model):
    field_name: str = "FloatData"
    value: float

    @model_validator(mode="before")
    @classmethod
    def ensure_fieldname(cls, data):
        """Ensure fieldname is set for existing documents"""
        if isinstance(data, dict) and "field_name" not in data:
            data["field_name"] = cls.__name__
        return data

    def make_table_entries(
        self,
        max_recursion_level=1,
        drop_id=True,
        current_level=0,
        visited=None,
        field_prefix="",
    ):
        return {self.field_name: self.value}

    def make_column_defs_instance(
        self,
        table_name=None,
        max_recursion_level=1,
        drop_id=True,
        current_level=0,
        visited=None,
        field_prefix="",
    ):
        return [
            {
                "field": self.field_name,
                "headerName": self.field_name,
                "type": "numericColumn",
            }
        ]

    @classmethod
    def ui_schema(cls, **kwargs) -> dict:
        return {
            "ui:field": "FloatDataField"
        }  # This tells RJSF to use the custom component


@simstack_model
class StringData(Model):
    field_name: str = "StringData"
    value: str

    @model_validator(mode="before")
    @classmethod
    def ensure_fieldname(cls, data):
        """Ensure fieldname is set for existing documents"""
        if isinstance(data, dict) and "field_name" not in data:
            data["field_name"] = cls.__name__
        return data

    def make_table_entries(
        self,
        max_recursion_level=1,
        drop_id=True,
        current_level=0,
        visited=None,
        field_prefix="",
    ):
        return {self.field_name: self.value}

    def make_column_defs_instance(
        self,
        table_name=None,
        max_recursion_level=1,
        drop_id=True,
        current_level=0,
        visited=None,
        field_prefix="",
    ):
        return [
            {
                "field": self.field_name,
                "headerName": self.field_name,
                "type": "textColumn",
            }
        ]

    @classmethod
    def ui_schema(cls, **kwargs) -> dict:
        return {
            "ui:field": "StringDataField"
        }  # This tells RJSF to use the custom component


@simstack_model
class BooleanData(Model):
    field_name: str = "BooleanData"
    value: bool

    @model_validator(mode="before")
    @classmethod
    def ensure_fieldname(cls, data):
        """Ensure fieldname is set for existing documents"""
        if isinstance(data, dict) and "field_name" not in data:
            data["field_name"] = cls.__name__
        return data

    def make_table_entries(
        self,
        max_recursion_level=1,
        drop_id=True,
        current_level=0,
        visited=None,
        field_prefix="",
    ):
        return {self.field_name: self.value}

    def make_column_defs_instance(
        self,
        table_name=None,
        max_recursion_level=1,
        drop_id=True,
        current_level=0,
        visited=None,
        field_prefix="",
    ):
        return [
            {
                "field": self.field_name,
                "headerName": self.field_name,
                "type": "booleanColumn",
            }
        ]

    @classmethod
    def ui_schema(cls, **kwargs) -> dict:
        return {
            "ui:field": "BooleanDataField"
        }  # This tells RJSF to use the custom component


@simstack_model
class BinaryOperationInput(Model):
    field_name: str = "BinaryOperationInput"
    arg1: FloatData = Reference()
    arg2: FloatData = Reference()

    @model_validator(mode="before")
    @classmethod
    def ensure_fieldname(cls, data):
        """Ensure fieldname is set for existing documents"""
        if isinstance(data, dict) and "field_name" not in data:
            data["field_name"] = cls.__name__
        return data

    def __str__(self):
        return f"BinaryOperationInput(arg1={self.arg1}, arg2={self.arg2})"


@simstack_model
class IteratorInput(Model):
    field_name: str = "IteratorInput"
    start: int
    stop: int
    generator: str = "range"

    @model_validator(mode="before")
    @classmethod
    def ensure_fieldname(cls, data):
        """Ensure fieldname is set for existing documents"""
        if isinstance(data, dict) and "field_name" not in data:
            data["field_name"] = cls.__name__
        return data
