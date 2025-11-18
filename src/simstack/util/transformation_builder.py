from typing import Dict, Callable
import json
from simstack.models.model_transformer import FieldTransform


class TransformationBuilder:
    """Helper class for application developers to build transformations"""

    def __init__(self, model_name: str):
        self.model_name = model_name
        self.transforms: Dict[str, FieldTransform] = {}
        self.description: str = ""

    def set_description(self, description: str) -> 'TransformationBuilder':
        """Set description for the transformation"""
        self.description = description
        return self

    def drop_field(self, field_path: str) -> 'TransformationBuilder':
        """Drop a field from the output"""
        self.transforms[field_path] = FieldTransform(
            model_name=self.model_name,
            field_path=field_path,
            action="drop"
        )
        return self

    def rename_field(self, field_path: str, new_name: str) -> 'TransformationBuilder':
        """Rename a field"""
        self.transforms[field_path] = FieldTransform(
            model_name=self.model_name,
            field_path=field_path,
            action="rename",
            target_field=new_name
        )
        return self

    def apply_function(self, field_path: str, function_code: str) -> 'TransformationBuilder':
        """Apply a transformation function to a field"""
        self.transforms[field_path] = FieldTransform(
            model_name=self.model_name,
            field_path=field_path,
            action="function",
            function_code=function_code
        )
        return self

    def apply_compiled_function(self, field_path: str, func: Callable) -> 'TransformationBuilder':
        """Apply a compiled function to a field by storing its bytecode"""
        if not callable(func):
            raise ValueError("func must be callable")

        # Extract function metadata
        func_code = func.__code__
        bytecode_hex = func_code.co_code.hex()

        code_metadata = {
            "co_argcount": func_code.co_argcount,
            "co_posonlyargcount": func_code.co_posonlyargcount,
            "co_kwonlyargcount": func_code.co_kwonlyargcount,
            "co_nlocals": func_code.co_nlocals,
            "co_stacksize": func_code.co_stacksize,
            "co_flags": func_code.co_flags,
            "co_code": bytecode_hex,
            "co_consts": func_code.co_consts,
            "co_names": func_code.co_names,
            "co_varnames": func_code.co_varnames,
            "co_filename": func_code.co_filename,
            "co_name": func_code.co_name,
            "co_firstlineno": func_code.co_firstlineno,
            "co_lnotab": func_code.co_lnotab.hex() if func_code.co_lnotab else "",
            "co_freevars": func_code.co_freevars,
            "co_cellvars": func_code.co_cellvars,
        }

        wrapper_code = f"""
import types
import marshal

code_metadata = {repr(code_metadata)}
bytecode = bytes.fromhex(code_metadata['co_code'])
lnotab = bytes.fromhex(code_metadata['co_lnotab']) if code_metadata['co_lnotab'] else b''

reconstructed_code = types.CodeType(
    code_metadata['co_argcount'],
    code_metadata['co_posonlyargcount'],
    code_metadata['co_kwonlyargcount'],
    code_metadata['co_nlocals'],
    code_metadata['co_stacksize'],
    code_metadata['co_flags'],
    bytecode,
    code_metadata['co_consts'],
    code_metadata['co_names'],
    code_metadata['co_varnames'],
    code_metadata['co_filename'],
    code_metadata['co_name'],
    code_metadata['co_firstlineno'],
    lnotab,
    code_metadata['co_freevars'],
    code_metadata['co_cellvars']
)

transform_func = types.FunctionType(reconstructed_code, globals())

try:
    result = transform_func(value)
except Exception as e:
    result = value
"""

        return self.apply_function(field_path, wrapper_code)

    def apply_function_by_name(self, field_path: str, func_name: str) -> 'TransformationBuilder':
        """Apply a function by its name"""
        function_code = f"""
if '{func_name}' in globals():
    transform_func = globals()['{func_name}']
    try:
        result = transform_func(value)
    except Exception as e:
        result = value
else:
    result = value
"""
        return self.apply_function(field_path, function_code)

    def extract_enum_value(self, field_path: str, value_key: str = "value") -> 'TransformationBuilder':
        """Helper to extract value from enum-like objects"""
        function_code = f"""
if isinstance(value, dict) and "{value_key}" in value:
    result = value["{value_key}"]
else:
    result = value
"""
        return self.apply_function(field_path, function_code)

    def flatten_object(self, field_path: str, prefix: str = "") -> 'TransformationBuilder':
        """Helper to flatten nested objects"""
        function_code = f"""
if isinstance(value, dict):
    flattened = {{}}
    prefix = "{prefix}"
    for k, v in value.items():
        new_key = f"{{prefix}}_{{k}}" if prefix else k
        flattened[new_key] = v
    result = flattened
else:
    result = value
"""
        return self.apply_function(field_path, function_code)

    def pandas_to_react(self, field_path: str = "content_") -> 'TransformationBuilder':
        """Helper for common pandas transformation"""
        function_code = """
import io
import pandas as pd
import json

if value:
    try:
        buffer = io.BytesIO(value)
        df = pd.read_pickle(buffer)
        
        for col in df.select_dtypes(include=['datetime64']):
            df[col] = df[col].dt.strftime('%Y-%m-%dT%H:%M:%S')
        
        data = df.to_dict(orient='records')
        result = json.loads(json.dumps(data, default=str))
    except Exception as e:
        result = []
else:
    result = []
"""
        return self.apply_function(field_path, function_code)

    def to_json(self) -> str:
        """Export as JSON string"""
        export_data = {
            "model_name": self.model_name,
            "transforms": [transform.model_dump(exclude={"id"}) for transform in self.transforms.values()]
        }

        return json.dumps(export_data, indent=2)