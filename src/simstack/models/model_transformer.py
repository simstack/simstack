import json
import marshal
import re
import types
from enum import Enum
from typing import Dict, Any, List, Optional, Callable

from odmantic import Model, Field


class TransformationAction(str, Enum):
    DROP = "drop"
    RENAME = "rename"
    FUNCTION = "function"
    COMPILED_FUNCTION = "compiled_function"


class FieldTransform(Model):
    """Serializable field transformation configuration"""

    model_name: str = Field(index=True)  # Model this transform applies to
    field_path: str = Field(index=True)  # Field path for this transform
    action: TransformationAction
    target_field: Optional[str] = None  # For rename action
    function_code: Optional[str] = None  # For function action
    function_name: Optional[str] = None  # For function by name
    bytecode_metadata: Optional[Dict[str, Any]] = None  # For compiled function


class TransformerEngine:
    def __init__(self, db_engine=None):
        self.field_transforms_cache: Dict[
            str, Dict[str, FieldTransform]
        ] = {}  # model_name -> {field_path -> FieldTransform}
        self.db_engine = db_engine
        self.registered_functions: Dict[str, Callable] = {}
        self.safe_globals = {
            "__builtins__": {
                "len": len,
                "str": str,
                "int": int,
                "float": float,
                "bool": bool,
                "list": list,
                "dict": dict,
                "tuple": tuple,
                "set": set,
                "min": min,
                "max": max,
                "sum": sum,
                "abs": abs,
                "round": round,
                "isinstance": isinstance,
                "hasattr": hasattr,
                "getattr": getattr,
            },
            "json": json,
            "re": re,
            "types": types,
            "marshal": marshal,
        }

    def register_function(self, name: str, func: Callable):
        """Register a function that can be called by name in transformations"""
        self.registered_functions[name] = func
        self.safe_globals[name] = func

    async def save_field_transform(self, transform: FieldTransform) -> FieldTransform:
        """Save a single field transformation to database"""
        if self.db_engine:
            # Check if transform already exists
            existing = await self.db_engine.find_one(
                FieldTransform,
                FieldTransform.model_name == transform.model_name,
                FieldTransform.field_path == transform.field_path,
            )

            if existing:
                # Update existing
                existing.action = transform.action
                existing.target_field = transform.target_field
                existing.function_code = transform.function_code
                existing.function_name = transform.function_name
                existing.bytecode_metadata = transform.bytecode_metadata
                saved_transform = await self.db_engine.save(existing)
            else:
                # Create new
                saved_transform = await self.db_engine.save(transform)

            # Update local cache
            if transform.model_name not in self.field_transforms_cache:
                self.field_transforms_cache[transform.model_name] = {}
            self.field_transforms_cache[transform.model_name][
                transform.field_path
            ] = saved_transform
            return saved_transform
        else:
            # No database, just store in memory
            if transform.model_name not in self.field_transforms_cache:
                self.field_transforms_cache[transform.model_name] = {}
            self.field_transforms_cache[transform.model_name][
                transform.field_path
            ] = transform
            return transform

    async def save_model_transforms(
        self, model_name: str, transforms: Dict[str, FieldTransform]
    ) -> List[FieldTransform]:
        """Save all field transformations for a model"""
        saved_transforms = []
        for field_path, transform in transforms.items():
            # Ensure model_name and field_path are set correctly
            transform.model_name = model_name
            transform.field_path = field_path
            saved_transform = await self.save_field_transform(transform)
            saved_transforms.append(saved_transform)
        return saved_transforms

    async def load_model_transforms(self, model_name: str) -> Dict[str, FieldTransform]:
        """Load all field transformations for a model"""
        # Check cache first
        if model_name in self.field_transforms_cache:
            return self.field_transforms_cache[model_name]

        # Load from database
        if self.db_engine:
            transforms = await self.db_engine.find(
                FieldTransform, FieldTransform.model_name == model_name
            )

            # Convert to dict and cache
            transforms_dict = {t.field_path: t for t in transforms}
            self.field_transforms_cache[model_name] = transforms_dict
            return transforms_dict

        return {}

    async def delete_field_transform(self, model_name: str, field_path: str) -> bool:
        """Delete a specific field transformation"""
        if self.db_engine:
            transform = await self.db_engine.find_one(
                FieldTransform,
                FieldTransform.model_name == model_name,
                FieldTransform.field_path == field_path,
            )
            if transform:
                await self.db_engine.delete(transform)
                # Remove from cache
                if model_name in self.field_transforms_cache:
                    self.field_transforms_cache[model_name].pop(field_path, None)
                return True
        else:
            # Remove from memory cache
            if (
                model_name in self.field_transforms_cache
                and field_path in self.field_transforms_cache[model_name]
            ):
                del self.field_transforms_cache[model_name][field_path]
                return True

        return False

    async def delete_model_transforms(self, model_name: str) -> bool:
        """Delete all transformations for a model"""
        if self.db_engine:
            transforms = await self.db_engine.find(
                FieldTransform, FieldTransform.model_name == model_name
            )
            for transform in transforms:
                await self.db_engine.delete(transform)
            # Clear cache
            self.field_transforms_cache.pop(model_name, None)
            return len(transforms) > 0
        else:
            # Remove from memory cache
            if model_name in self.field_transforms_cache:
                del self.field_transforms_cache[model_name]
                return True

        return False

    async def get_all_model_names(self) -> List[str]:
        """Get all model names that have transformations"""
        if self.db_engine:
            transforms = await self.db_engine.find(FieldTransform)
            model_names = list(set(t.model_name for t in transforms))
            return model_names
        else:
            return list(self.field_transforms_cache.keys())

    async def export_model_transforms(self, model_name: str) -> Optional[str]:
        """Export all transformations for a model as JSON"""
        transforms = await self.load_model_transforms(model_name)
        if transforms:
            # Convert to list of dictionaries for JSON export
            export_data = {
                "model_name": model_name,
                "transforms": [transform.dict() for transform in transforms.values()],
            }
            return json.dumps(export_data, indent=2)
        return None

    async def import_model_transforms(self, json_str: str) -> List[FieldTransform]:
        """Import transformations from JSON"""
        data = json.loads(json_str)
        model_name = data["model_name"]
        transforms_data = data["transforms"]

        transforms = {}
        for transform_data in transforms_data:
            transform = FieldTransform(**transform_data)
            transforms[transform.field_path] = transform

        return await self.save_model_transforms(model_name, transforms)

    async def transform_data(
        self, data: Dict[str, Any], model_name: str, json_schema: Dict[str, Any] = None
    ) -> Dict[str, Any]:
        """Transform data using loaded transformations, handling nested models based on JSON schema"""
        # First, discover all model names that might be needed for transformation
        all_model_names = self._discover_all_model_names(data, model_name, json_schema)

        # Load all transforms for all discovered models
        all_transforms = {}
        for discovered_model_name in all_model_names:
            transforms = await self.load_model_transforms(discovered_model_name)
            all_transforms[discovered_model_name] = transforms

        return self._apply_hierarchical_transformations(
            data, all_transforms, model_name, json_schema
        )

    def _discover_all_model_names(
        self, data: Dict[str, Any], model_name: str, json_schema: Dict[str, Any] = None
    ) -> set:
        """Discover all model names that will be needed during transformation"""
        discovered_models = {model_name}

        if not json_schema:
            return discovered_models

        # Recursively discover nested models from the schema
        self._discover_nested_models_from_schema(
            model_name, json_schema, discovered_models
        )

        return discovered_models

    def _discover_nested_models_from_schema(
        self, model_name: str, json_schema: Dict[str, Any], discovered_models: set
    ):
        """Recursively discover all nested model names from JSON schema"""
        if model_name in discovered_models:
            # Avoid infinite recursion for circular references
            return

        # Get schema properties for current model
        schema_properties = {}
        if "definitions" in json_schema:
            model_def = json_schema["definitions"].get(None, model_name)
            schema_properties = model_def.get(None, "properties")
        elif model_name == "root" and "properties" in json_schema:
            schema_properties = json_schema["properties"]

        # Look for nested model references in each property
        for field_name, field_schema in schema_properties.items():
            nested_model_name = self._extract_model_name_from_schema(field_schema)
            if nested_model_name and nested_model_name not in discovered_models:
                discovered_models.add(nested_model_name)
                # Recursively discover models within this nested model
                self._discover_nested_models_from_schema(
                    nested_model_name, json_schema, discovered_models
                )

    def _extract_model_name_from_schema(
        self, field_schema: Dict[str, Any]
    ) -> Optional[str]:
        """Extract model name from a field schema definition"""
        # Check for direct $ref to a model
        if "$ref" in field_schema:
            ref = field_schema["$ref"]
            if ref.startswith("#/definitions/"):
                return ref.replace("#/definitions/", "")

        # Check for array items that reference models
        if field_schema.get("type") == "array" and "items" in field_schema:
            items_schema = field_schema["items"]
            if "$ref" in items_schema:
                ref = items_schema["$ref"]
                if ref.startswith("#/definitions/"):
                    return ref.replace("#/definitions/", "")

        # Check for object with properties that might indicate a model
        if field_schema.get("type") == "object":
            # Look for allOf, anyOf, oneOf patterns that might reference models
            for key in ["allOf", "anyOf", "oneOf"]:
                if key in field_schema:
                    for item in field_schema[key]:
                        if "$ref" in item:
                            ref = item["$ref"]
                            if ref.startswith("#/definitions/"):
                                return ref.replace("#/definitions/", "")

        return None

    def _apply_hierarchical_transformations(
        self,
        data: Dict[str, Any],
        all_transforms: Dict[str, Dict[str, FieldTransform]],
        current_model_name: str,
        json_schema: Dict[str, Any] = None,
    ) -> Dict[str, Any]:
        """Apply transformations at all levels of nested data based on JSON schema"""
        if not isinstance(data, dict):
            return data

        result = {}

        # Get transforms for current model
        transforms = all_transforms.get(current_model_name, {})

        # Get schema properties for current model if available
        schema_properties = {}
        if json_schema and "definitions" in json_schema:
            model_def = json_schema["definitions"].get(current_model_name, {})
            schema_properties = model_def.get("properties", {})
        elif json_schema and "properties" in json_schema:
            schema_properties = json_schema["properties"]

        # Process each field in the data
        for field_name, field_value in data.items():
            field_path = field_name
            transform = transforms.get(field_path)

            # Check if this field should be dropped
            if transform and transform.action == TransformationAction.DROP:
                continue

            # Determine target field name (for renames)
            target_field_name = field_name
            if (
                transform
                and transform.action == TransformationAction.RENAME
                and transform.target_field
            ):
                target_field_name = transform.target_field

            # Process the field value
            processed_value = field_value

            # Check if this field represents a nested model according to schema
            nested_model_name = self._get_nested_model_name(
                field_name, schema_properties, json_schema
            )

            if nested_model_name and nested_model_name in all_transforms:
                # This field contains a nested model - apply transformations recursively
                if isinstance(field_value, dict):
                    # Single nested model
                    processed_value = self._apply_hierarchical_transformations(
                        field_value, all_transforms, nested_model_name, json_schema
                    )
                elif isinstance(field_value, list):
                    # Array of nested models
                    processed_list = []
                    for item in field_value:
                        if isinstance(item, dict):
                            processed_item = self._apply_hierarchical_transformations(
                                item, all_transforms, nested_model_name, json_schema
                            )
                            processed_list.append(processed_item)
                        else:
                            processed_list.append(item)
                    processed_value = processed_list
            elif isinstance(field_value, dict):
                # Regular nested object (not a model) - process recursively but keep same model context
                processed_value = self._apply_hierarchical_transformations(
                    field_value, all_transforms, current_model_name, json_schema
                )
            elif isinstance(field_value, list):
                # Array that might contain nested objects
                processed_list = []
                for item in field_value:
                    if isinstance(item, dict):
                        processed_item = self._apply_hierarchical_transformations(
                            item, all_transforms, current_model_name, json_schema
                        )
                        processed_list.append(processed_item)
                    else:
                        processed_list.append(item)
                processed_value = processed_list

            # Apply field-level transformations (function transformations)
            if transform and transform.action == TransformationAction.FUNCTION:
                try:
                    processed_value = self._execute_function(
                        processed_value, transform.function_code
                    )
                except Exception as e:
                    print(f"Error applying function to {field_path}: {e}")
                    # Keep the processed value (which might be recursively transformed)
            elif (
                transform and transform.action == TransformationAction.COMPILED_FUNCTION
            ):
                try:
                    processed_value = self._execute_compiled_function(
                        processed_value, transform
                    )
                except Exception as e:
                    print(f"Error applying compiled function to {field_path}: {e}")
                    # Keep the processed value

            result[target_field_name] = processed_value

        return result

    def _get_nested_model_name(
        self,
        field_name: str,
        schema_properties: Dict[str, Any],
        json_schema: Dict[str, Any],
    ) -> Optional[str]:
        """Determine if a field represents a nested model and return the model name"""
        if not schema_properties or field_name not in schema_properties:
            return None

        field_schema = schema_properties[field_name]

        # Check for direct $ref to a model
        if "$ref" in field_schema:
            ref = field_schema["$ref"]
            if ref.startswith("#/definitions/"):
                return ref.replace("#/definitions/", "")

        # Check for array items that reference models
        if field_schema.get("type") == "array" and "items" in field_schema:
            items_schema = field_schema["items"]
            if "$ref" in items_schema:
                ref = items_schema["$ref"]
                if ref.startswith("#/definitions/"):
                    return ref.replace("#/definitions/", "")

        # Check for object with properties that might indicate a model
        if field_schema.get("type") == "object":
            # Look for allOf, anyOf, oneOf patterns that might reference models
            for key in ["allOf", "anyOf", "oneOf"]:
                if key in field_schema:
                    for item in field_schema[key]:
                        if "$ref" in item:
                            ref = item["$ref"]
                            if ref.startswith("#/definitions/"):
                                return ref.replace("#/definitions/", "")

        return None

    def _execute_compiled_function(
        self, field_value: Any, transform: FieldTransform
    ) -> Any:
        """Execute a compiled function transformation"""
        if not transform.bytecode_metadata:
            return field_value

        try:
            # Reconstruct function from bytecode metadata
            bytecode = marshal.loads(
                bytes.fromhex(transform.bytecode_metadata["bytecode"])
            )
            func = types.FunctionType(
                bytecode,
                {**self.safe_globals, **self.registered_functions},
                transform.bytecode_metadata.get("name", "transform_func"),
            )

            # Execute the function
            return func(field_value)
        except Exception as e:
            raise RuntimeError(f"Compiled function execution failed: {e}")

    def _apply_transformations(
        self, data: Dict[str, Any], transforms: Dict[str, FieldTransform]
    ) -> Dict[str, Any]:
        """Apply field transformations to data"""
        result = {}

        # Process each field in the original data
        for field_path, field_value in self._flatten_dict(data).items():
            transform = transforms.get(field_path)

            if transform and transform.action == "drop":
                # Skip this field
                continue
            elif transform and transform.action == "rename":
                # Rename the field
                new_path = transform.target_field or field_path
                result[new_path] = field_value
            elif transform and transform.action == "function":
                # Apply function transformation
                try:
                    transformed_value = self._execute_function(
                        field_value, transform.function_code
                    )
                    result[field_path] = transformed_value
                except Exception as e:
                    # On error, keep original value and optionally log
                    print(f"Error applying function to {field_path}: {e}")
                    result[field_path] = field_value
            else:
                # No transformation, keep original
                result[field_path] = field_value

        # Reconstruct nested dictionary from flattened paths
        return self._unflatten_dict(result)

    def _flatten_dict(
        self, data: Dict[str, Any], parent_key: str = "", sep: str = "."
    ) -> Dict[str, Any]:
        """Flatten nested dictionary to dot-notation paths"""
        items = []
        for key, value in data.items():
            new_key = f"{parent_key}{sep}{key}" if parent_key else key
            if isinstance(value, dict) and value:
                items.extend(self._flatten_dict(value, new_key, sep=sep).items())
            else:
                items.append((new_key, value))
        return dict(items)

    def _unflatten_dict(
        self, flattened: Dict[str, Any], sep: str = "."
    ) -> Dict[str, Any]:
        """Reconstruct nested dictionary from flattened dot-notation"""
        result = {}
        for key, value in flattened.items():
            keys = key.split(sep)
            current = result
            for k in keys[:-1]:
                if k not in current:
                    current[k] = {}
                current = current[k]
            current[keys[-1]] = value
        return result

    def _execute_function(self, field_value: Any, function_code: str) -> Any:
        """Safely execute transformation function"""
        if not function_code:
            return field_value

        # Create safe execution environment with registered functions
        local_vars = {"value": field_value}
        safe_globals_with_funcs = {**self.safe_globals, **self.registered_functions}

        try:
            # Execute the function code
            exec(function_code, safe_globals_with_funcs, local_vars)
            # The function should set 'result' variable
            return local_vars.get("result", field_value)
        except Exception as e:
            raise RuntimeError(f"Function execution failed: {e}")


# Database management functions
class TransformationManager:
    def __init__(self, db_engine):
        self.db_engine = db_engine
        self.transformer_engine = TransformerEngine(db_engine)

    async def export_model_config(self, model_name: str) -> Optional[str]:
        """Export all transformations for a model as JSON string"""
        return await self.transformer_engine.export_model_transforms(model_name)

    async def import_model_config(self, json_str: str) -> List[FieldTransform]:
        """Import transformations from JSON string"""
        return await self.transformer_engine.import_model_transforms(json_str)

    async def copy_model_transforms(
        self, source_model: str, target_model: str
    ) -> List[FieldTransform]:
        """Copy transformations from one model to another"""
        source_transforms = await self.transformer_engine.load_model_transforms(
            source_model
        )
        if not source_transforms:
            return []

        # Create new transforms for target model
        target_transforms = {}
        for field_path, transform in source_transforms.items():
            new_transform = FieldTransform(
                model_name=target_model,
                field_path=field_path,
                action=transform.action,
                target_field=transform.target_field,
                function_code=transform.function_code,
                function_name=transform.function_name,
                bytecode_metadata=transform.bytecode_metadata,
            )
            target_transforms[field_path] = new_transform

        return await self.transformer_engine.save_model_transforms(
            target_model, target_transforms
        )

    async def get_all_models_with_transforms(self) -> List[str]:
        """Get list of all models that have transformations"""
        return await self.transformer_engine.get_all_model_names()

    async def delete_all_transforms_for_model(self, model_name: str) -> bool:
        """Delete all transformations for a specific model"""
        return await self.transformer_engine.delete_model_transforms(model_name)
