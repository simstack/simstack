---
name: simstack-nodes
description: >-
  Author and call Simstack @node functions, NodeRunner results, and odmantic
  @simstack_model inputs/outputs. Use when creating or editing @node functions,
  node models, create_node_table / create_model_table, or calling nodes after
  context.initialize().
---

# Simstack Nodes

## Hard requirements (always)

1. After creating/changing `@node` functions or odmantic models, register them:
  - `uv run create_model_table --dir PATH_TO_MODEL_DIR`
  - `uv run create_node_table --dir PATH_TO_NODE_DIR`
    Paths are relative to project root. Integration tests that hit nodes/models
    need these tables built first (see `simstack/tests/with_config` conftest).
2. Before calling any `@node`, run `await context.initialize()` with **no
   arguments**.
3. Do not mock `@node` functions in tests; do not call their inner/unwrapped
   functions. External nodes in tests belong under `with_runner`.

## Node signature

- Decorate with `@node` from `simstack.core.node`.
- Prefer `async def`.
- Every input (except `**kwargs`) must be an odmantic `Model`, ideally
  `@simstack_model`. Last argument **must** be `**kwargs`.
- Scalars are models: `FloatData`, `IntData`, `BooleanData`, etc. Files:
  `FileStack` / `FileList`. Read values via `.value` (or `.real_value` where
  that API exists).
- Do **not** put raw `float` / `int` / `str` on `node_runner` result fields;
  wrap them (`FloatData(field_name="efermi", value=...)`, etc.).
- Avoid `from __future__ import annotations` on `@node` modules until the
  installed simstack version handles postponed annotations in `create_node_table`.

```python
from simstack.core.node import node
from simstack.models import FloatData, simstack_model
from odmantic import Model

@simstack_model
class MyInput(Model):
    field_name: str = "MyInput"
    data: float

@node
async def my_node(opts: MyInput, **kwargs):
    ...
```

Force rerun (bypass cache):

```python
from simstack.models import Parameters
from simstack.core.node import node

@node(parameters=Parameters(force_rerun=True))
async def my_node(...):
    ...
```

## NodeRunner

`node_runner = kwargs["node_runner"]` (or `.get("node_runner")`).

- Log: `.info()`, `.debug()`, `.error()`
- Finish: set outputs on `node_runner`, then `return node_runner.succeed()`
- Fail: `return node_runner.fail(error_message=...)`

```python
@node
async def calculate_statistics(data: IntData, **kwargs):
    node_runner = kwargs.get("node_runner")
    try:
        node_runner.info(f"input={data.value}")
        node_runner.result = FloatData(field_name="statistics", value=data.value * 2.5)
        return node_runner.succeed()
    except Exception as e:
        node_runner.error(str(e))
        return node_runner.fail(error_message=str(e))
```

Simple nodes may return a model directly:

```python
@node
def add(a: FloatData, b: FloatData, **kwargs) -> FloatData:
    return FloatData(field_name="sum", value=a.real_value + b.real_value)
```

## Docstrings for `SimstackResult`

If the return type is `SimstackResult`, document outputs for `create_node_table`:

```text
SimstackResult:
    files (List[FileStack]): Collected output files
    efermi (FloatData): Fermi energy (eV)
```

Only Models (or `List[Model]` / `Dict[str,Model]`) — never bare `float`.

## Models

- Every `@simstack_model` must declare
  `field_name: str = "ClassName"` (the class name as a string default).
- **Odmantic does not support inheritance for models.** Do not subclass one
  `Model` / `EmbeddedModel` from another to share fields. Compose instead:
  embed an `EmbeddedModel` with `Field(default_factory=...)`, or hold a nested
  `Model` via `Reference()` (required) or `Optional[Model]` (embedded when
  optional — bare `Model` fields must use `Reference()`).
- Nested required `Model` / `FileStack` / `FileListModel` fields: use
  `Reference()`. Optional file lists prefer `FileListModel` (not `FileListIO`)
  when the UI should edit a list of `FileStack`s.

```python
from typing import Optional

from odmantic import Field, Model, Reference
from pydantic import model_validator
from simstack.models import FileStack, simstack_model
from simstack.models.file_list import FileListModel
from simstack.util.cleaned_json_schema import cleaned_json_schema
from simstack.util.generate_ui_schema import generate_ui_schema

@simstack_model
class MyModel(Model):
    field_name: str = "MyModel"
    data: Any
    poscar: FileStack = Reference()  # required nested Model / FileStack
```

### Optional fields (UI + persistence)

Odmantic / RJSF do not hide `Optional[...]` fields by themselves. For optional
inputs that should appear only when needed:

1. Add a real boolean toggle on the model (persisted), e.g. `use_foo: bool = False`
   or `override_efermi: bool = False` — use `json_schema_extra={"title": "..."}`.
2. Keep the optional payload as `Optional[T] = Field(None, ...)`.
3. Add a `@model_validator(mode="before")` that:
  - infers the toggle from existing DB docs when missing
    (`use_foo = data.get("foo") is not None`);
  - clears the optional when the toggle is off (`data["foo"] = None`).
4. Override `json_schema` with `cleaned_json_schema(cls)`, pop the optional
   property schemas, and reattach them under `dependencies` / `oneOf` gated by
   the toggle (see `QMInput` in molecular_qm_models, or `VaspJobInput` /
   `Wannier90RunInput` / `VaspWannierTB2JInput` in xtalgen).
5. Override `ui_schema` with `generate_ui_schema(cls)`, set the toggle widget to
   checkbox, and **merge** `"ui:condition"` onto existing field UI entries
   (`ui_schema.setdefault(name, {})["ui:condition"] = {...}`). Never replace the
   whole entry — that drops `FileField` / `GenericFormField` from
   `generate_ui_schema` and RJSF falls back to a null/object dropdown instead of
   the upload control.

When a toggle chooses between **generated params** and a **pre-built file**
(e.g. `use_incar_file`), show the file when the flag is True and the generated
embedded params when False — both arms belong in `dependencies` / `ui:condition`.

```python
@simstack_model
class ExampleInput(Model):
    field_name: str = "ExampleInput"
    use_extra_files: bool = Field(
        False, json_schema_extra={"title": "Stage extra files"}
    )
    extra_files: Optional[FileListModel] = Field(None)

    @model_validator(mode="before")
    @classmethod
    def sync_optional_toggles(cls, data):
        if not isinstance(data, dict):
            return data
        if "use_extra_files" not in data:
            data["use_extra_files"] = data.get("extra_files") is not None
        if not data.get("use_extra_files"):
            data["extra_files"] = None
        return data

    @classmethod
    def json_schema(cls, recursive=True):
        schema = cleaned_json_schema(cls)
        schema["title"] = cls.__name__
        props = schema["properties"]
        extra = props.pop("extra_files", None)
        schema.setdefault("dependencies", {})["use_extra_files"] = {
            "oneOf": [
                {"properties": {"use_extra_files": {"const": False}}},
                {
                    "properties": {
                        "use_extra_files": {"const": True},
                        "extra_files": extra,
                    }
                },
            ]
        }
        return schema

    @classmethod
    def ui_schema(cls):
        ui = generate_ui_schema(cls)
        ui["field_name"] = {"ui:widget": "hidden"}
        ui["use_extra_files"] = {
            "ui:widget": "checkbox",
            "ui:title": "Stage extra files",
        }
        ui["extra_files"] = {"ui:condition": {"use_extra_files": True}}
        return ui
```

Executables / launchers are **not** model fields: read
`context.resource_config.get_program("name")["run_command"]` (and optionally
`context.resource_config.run(...)`) from `config.toml`, same as `vasp_run`.

## Do not

- Change `simstack.core.node` / `ObjectListMixin` / `GenericListMixin` unless
  explicitly asked.
- Call `db_find_postprocess` or use `db.engine` (use `db` directly).
- “Fix” mature `simstack/tests/**/conftest.py` by weakening production code;
  fix the tests instead.
- Subclass odmantic `Model` / `EmbeddedModel` types to share schema fields.
