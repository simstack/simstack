# TESTPLAN compute_arg_hash
SOURCE: src/simstack/core/node.py
METHOD: compute_arg_hash
TEST_FILE: tests/with_context/core/test_node_argument_hashing.py
SCOPE: unit+integration
STATUS: implemented

## CASE_INDEX
TOTAL_CASES: 5
### AREA A01 Argument Hash Contract [4]
- [A01.C01] [U] [P1] Should hash ordinary model instances deterministically by value :: inputs=two equal FloatData instances | internals=complex_hash_function path | collaborators=none | callContract=none | outputs=same hash | valueFlow=model values to hash | sideEffects=none
- [A01.C02] [U] [P0] Should preserve top-level custom complex_hash for node arguments :: inputs=top-level Model defining complex_hash | internals=compute_arg_hash top-level branch | collaborators=model complex_hash | callContract=called once per top-level argument | outputs=same custom value hashes together, different value hashes apart | valueFlow=custom hash return into aggregate hash | sideEffects=custom call counter increments
- [A01.C03] [U] [P0] Should serialize nested BaseModel values without invoking nested custom complex_hash :: inputs=ParentModel with nested EmbeddedModel whose complex_hash raises | internals=hashable_inputs recursive serialization | collaborators=nested model complex_hash not called | callContract=zero nested custom hash calls | outputs=hash returned | valueFlow=nested fields to hashable dict | sideEffects=none
- [A01.C04] [U] [P0] Should let nested model values affect the node argument hash :: inputs=two ParentModel instances differing only in nested value | internals=hashable_inputs recursive serialization | collaborators=none | callContract=none | outputs=different hashes | valueFlow=nested scalar value to aggregate hash | sideEffects=none
### AREA A02 Fanout Registry Regression [1]
- [A02.C01] [I] [P0] Should create all child NodeRegistry entries when an async parent fans out 50 Slurm children with heavy nested model inputs :: inputs=IntData count=50 and child inputs containing nested hash traps | internals=async @node wrapper, compute_arg_hash, make_registry_entry, nested Slurm inline submission | collaborators=submit_node monkeypatched to complete entries | callContract=submit_node called once per child | outputs=parent returns FloatData count=50 | valueFlow=count input to child fanout count and registry parent_ids | sideEffects=50 child registry entries with parent call_path and completed status

## PLAN
ID_RULES: AREA=A01...; GROUP=<AREA>.G01...; CASE=<AREA>.C01...
EQUIVALENCE_CLASSES:
- Top-level argument shape
  - ordinary ODMantic model without custom complex_hash
  - top-level ODMantic model with custom complex_hash
- Nested value shape
  - nested BaseModel without behavior-changing custom hash
  - nested BaseModel with custom complex_hash that must not be called from node argument hashing
  - nested scalar value changed
- Execution shape
  - direct compute_arg_hash unit call
  - async @node parent fanout through registry creation
- Queue shape
  - local default parent
  - Slurm child on current resource with inline submission
- Fanout size
  - one direct hash input
  - many child node inputs
INTERACTIONS:
- I01
  - dimensions: Nested value shape, Execution shape
  - classes: nested custom complex_hash that must not be called, async @node parent fanout through registry creation
  - kind: required_pair
  - behavior_change: this is the production regression path where child registry creation happens only after argument hashing
  - covered_by: A02.C01
- I02
  - dimensions: Top-level argument shape, Nested value shape
  - classes: top-level model with custom complex_hash, nested custom complex_hash that must not be called
  - kind: required_pair
  - behavior_change: top-level custom hashes remain allowed while nested custom hashes are bypassed for node argument serialization
  - covered_by: A01.C02,A01.C03
- I03
  - dimensions: Queue shape, Fanout size
  - classes: Slurm child on current resource with inline submission, many child node inputs
  - kind: required_pair
  - behavior_change: verifies the nested Slurm fix still works when many children are created from one parent
  - covered_by: A02.C01
### AREA A01 Argument Hash Contract
INTENT: Protect the intended node argument hash semantics after the expensive nested model hashing fix.
AXES: top_level=ordinary,custom_complex_hash; nested=plain,custom_hash_trap,value_changed; execution=direct_hash
GROUPS:
- A01.G01
  - intent: preserve deterministic value hashing for simple models
  - varies: top_level=ordinary
  - fixed: execution=direct_hash
  - why_meaningful: keeps baseline hash behavior visible
  - generates: A01.C01
  - omitted_neighbors: covered by existing simple hash test, no separate fanout behavior
- A01.G02
  - intent: preserve top-level custom complex_hash contract
  - varies: top_level=custom_complex_hash
  - fixed: execution=direct_hash
  - why_meaningful: prevents over-correcting by disabling all model custom hashes
  - generates: A01.C02
  - omitted_neighbors: nested custom hash behavior is separate and covered in A01.G03
- A01.G03
  - intent: bypass nested custom complex_hash while preserving nested value provenance
  - varies: nested=custom_hash_trap,value_changed
  - fixed: execution=direct_hash
  - why_meaningful: covers the exact bug class behind slow QMInput hashing
  - generates: A01.C03,A01.C04
  - omitted_neighbors: same arrange-act-assert shape proves both zero nested calls and value sensitivity
### AREA A02 Fanout Registry Regression
INTENT: Reproduce the master-node failure shape with core-only models so child registry creation cannot silently regress.
AXES: execution=async_parent_fanout; queue=slurm_inline_child; fanout_size=50; nested=custom_hash_trap
GROUPS:
- A02.G01
  - intent: verify many child nodes cross the hashing barrier and create registry entries
  - varies: fanout_size=50
  - fixed: execution=async_parent_fanout; queue=slurm_inline_child; nested=custom_hash_trap
  - why_meaningful: this is the smallest core-only version of master -> many_orca -> ORCA grandchildren
  - generates: A02.C01
  - omitted_neighbors: direct single-child behavior is already covered by nested Slurm unit tests

## EXISTING_TESTS
COVERED: tests/with_context/core/test_hash.py::test_basic_hashing :: A01.C01 baseline deterministic hashing
COVERED: tests/with_context/core/test_hash.py::test_compute_arg_hash_serializes_nested_models_by_value :: A01.C03 and A01.C04 direct nested value regression
COVERED: tests/with_context/core/test_nested_slurm_submission.py::test_nested_slurm_child_is_submitted_inline_on_current_resource :: single child inline Slurm submission
MISSING: top-level custom complex_hash preservation :: no test proved the fix did not disable top-level custom hashes
MISSING: async parent fanout with many nested hash-trap child inputs :: no test reproduced the real "children never appear after input construction" symptom
DECISION_POINTS: preserve_and_extend :: existing tests are valid but need integration coverage for the full registry-creation path

## CODEX_DETAILS
NOTE: Sections below are Codex generation and maintenance details. Review `CASE_INDEX`, `EQUIVALENCE_CLASSES`, `INTERACTIONS`, `AREA` groups, and `EXISTING_TESTS` first.

DERIVATION:
- RAW_TOTAL_CASES: 5
- step1_dimensions_and_classes_complete=yes, includes top-level hash contract, nested hash trap, fanout size, and Slurm child routing
- step2_raw_cases_derived_from_dimensions_branches_outputs=yes, each behavior-changing branch/output has one semantic case
- step3_reuse_structure_applied_without_silent_case_loss=yes, only A01.C03 and A01.C04 share helper setup while retaining separate assertions
- merge_decisions:
  - M01 :: raw_cases=A01.C03,A01.C04 | final_case=none merged, shared helper only | justification=same setup, distinct semantic assertions retained

## COVERAGE_MAP
BRANCHES:
- B01 :: code=compute_arg_hash uses arg.complex_hash for top-level Model with custom hash | covered_by=A01.C02 | merge_justification=n/a
- B02 :: code=compute_arg_hash falls back to complex_hash_function(hashable_inputs(arg)) | covered_by=A01.C01,A01.C03,A01.C04,A02.C01 | merge_justification=n/a
- B03 :: code=hashable_value recurses into BaseModel values | covered_by=A01.C03,A01.C04,A02.C01 | merge_justification=n/a
- B04 :: code=Node.run_somewhere submits Slurm child inline on same resource | covered_by=A02.C01 | merge_justification=n/a
OUTPUTS:
- O01 :: shape=stable hash string for equal inputs | covered_by=A01.C01,A01.C02,A01.C03 | merge_justification=n/a
- O02 :: shape=different hash for changed nested value | covered_by=A01.C04 | merge_justification=n/a
- O03 :: shape=parent node result reports 50 completed children | covered_by=A02.C01 | merge_justification=n/a
AXIS_CLASSES:
- top_level: ordinary ODMantic model | covered_by=A01.C01 | merge_justification=n/a
- top_level: custom complex_hash | covered_by=A01.C02 | merge_justification=n/a
- nested: custom hash trap | covered_by=A01.C03,A02.C01 | merge_justification=n/a
- nested: value changed | covered_by=A01.C04 | merge_justification=n/a
- execution: async parent fanout | covered_by=A02.C01 | merge_justification=n/a
- queue: Slurm child inline submission | covered_by=A02.C01 | merge_justification=n/a
CALL_CONTRACTS:
- CALL01 :: collaborator=top-level model complex_hash | when=top-level arg has custom complex_hash | args=no args | count=once per top-level argument hash | covered_by=A01.C02
- CALL02 :: collaborator=nested model complex_hash | when=nested BaseModel has custom complex_hash | args=n/a | count=zero | covered_by=A01.C03,A02.C01
- CALL03 :: collaborator=submit_node | when=50 Slurm children are created on current resource | args=each child NodeRegistry | count=50 | covered_by=A02.C01
VALUE_PROVENANCE:
- V01 :: field=arg_hash | source=input | path=top-level custom complex_hash return -> aggregate list hash | covered_by=A01.C02
- V02 :: field=arg_hash | source=input | path=nested BaseModel __dict__ scalar values -> hashable dict -> aggregate hash | covered_by=A01.C03,A01.C04
- V03 :: field=NodeRegistry.parent_ids | source=parent execution node id | path=parent kwargs parent_id -> child NodeRegistry.parent_ids | covered_by=A02.C01
- V04 :: field=NodeRegistry.call_path | source=parent call_path plus child name | path=decorator update_kwargs call_path concatenation | covered_by=A02.C01

## IMPLEMENTATION
HELPERS: top-level test-only Model and EmbeddedModel classes with hash-trap behavior; async parent/child nodes
FACTORIES: build_large_hash_input(index, payload_count)
MOCK_BOUNDARY: monkeypatch simstack.core.submit_node.submit_node to mark Slurm child entries completed without sbatch
DO_NOT_CARE: real ORCA, real Slurm, real MongoDB, chemical model details
CHUNKING: keep fanout regression in one integration test because the bug requires the combined parent fanout plus child registry path

## SANITY
METHOD_COMPLEXITY: compute_arg_hash is small, but it gates all node registry creation and delegates recursive hashing
MEANINGFUL_BRANCH_COUNT: 4
MEANINGFUL_CLASS_COUNT: 10
RAW_TOTAL_CASES: 5
HEURISTIC_CASE_RANGE: hard_floor=5; target_range=5-7
PLANNED_CASE_COUNT: 5
SELF_REVIEW: The plan covers the direct hash contract and the real workflow-shaped regression without depending on simstack-model or a Slurm cluster.
