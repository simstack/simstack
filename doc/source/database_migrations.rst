.. _database_migrations:

Database Migrations
===================

Use a migration when stored MongoDB data must change: model updates, field
backfills, key renames, embedded-model rewrites, index changes, or one-off data
repairs.

Add One
-------

1. Define the target state first: model code, collection shape, and recovery
   flow.
2. Add workspace migrations under ``server/migrations/`` and central
   ``simstack`` database migrations under ``server/migrations/application/``.
   Name files ``YYYYMMDDHHMMSS_short_name.py``.
3. Set ``name`` to the file stem and ``dependencies`` to the latest migration
   in the selected directory.
4. Take a backup before running the migration on any real database.
5. Check ``CONNECTION_STRING`` before running:

   - The current runner requires a MongoDB user with ``admin.root``.
   - This applies to ``--scope ALL``, ``--scope DATABASES``, and
     ``--scope SERVER``.

6. Decide rollout before writing code:

   - **If old runners or old server code cannot read the new schema, stop
     runners and stop writes until the migration is finished.**
   - If a mixed schema must be tolerated for a short rollout window, ship
     compatibility code first, run the migration second, remove compatibility
     code last.

7. Define recovery before merge:

   - Prefer a real ``downgrade``.
   - If downgrade would drop data or cannot fully restore the old state,
     document a forward-only recovery flow:
     backup, stop writers, rerun or restore.

8. Keep the migration strict:

   - Touch only the affected collections and fields.
   - Fail fast on unsupported data.
   - Log migration-specific counts or checkpoints. The runner already logs the
     target database.
   - If the migration is started again after a partial failure, it must not
     duplicate or corrupt already migrated data.

9. Test on one target database first.

   .. code-block:: bash

      uv run python -m server.migrations.run_workspace_migrations \
        --scope DATABASES \
        --databases my_test_user_db \
        show

      uv run python -m server.migrations.run_workspace_migrations \
        --scope DATABASES \
        --databases my_test_user_db \
        migrate

      uv run python -m server.migrations.run_workspace_migrations \
        --scope ALL \
        migrate

      uv run python -m server.migrations.run_workspace_migrations \
        --scope SERVER \
        migrate

10. ``db_migrations.yml`` runs ``--scope ALL migrate`` by default. Use that
    only after one workspace DB is verified.

    .. code-block:: bash

       docker compose -f db_migrations.yml run --rm simstack-migrations

    To target one database from Docker, override the command:

    .. code-block:: bash

       docker compose -f db_migrations.yml run --rm simstack-migrations \
         uv run python -m server.migrations.run_workspace_migrations \
         --scope DATABASES \
         --databases my_test_user_db \
         migrate

    To run central ``simstack`` database migrations:

    .. code-block:: bash

       docker compose -f db_migrations.yml run --rm simstack-migrations \
         uv run python -m server.migrations.run_workspace_migrations \
         --scope SERVER \
         migrate

Example 1: Add Field - ResourceDefinition.is_enabled
----------------------------------------------------

Case: ``ResourceDefinition`` gets a new field ``is_enabled: bool = True`` and
old resource documents must receive the default.

Model change:

.. code-block:: python

   is_enabled: bool = True

Create ``server/migrations/20260527120000_resource_definition_is_enabled.py``:

.. code-block:: python

   """Backfill ResourceDefinition.is_enabled with True when missing."""
   from __future__ import annotations

   import logging

   name = "20260527120000_resource_definition_is_enabled"
   dependencies = ["20260527110000_previous_migration"]

   logger = logging.getLogger(__name__)
   MISSING_IS_ENABLED_QUERY = {"is_enabled": {"$exists": False}}


   def upgrade(db):
       resources_collection = db["resource_definition"]
       result = resources_collection.update_many(
           MISSING_IS_ENABLED_QUERY,
           {"$set": {"is_enabled": True}},
       )

       remaining_missing_count = resources_collection.count_documents(MISSING_IS_ENABLED_QUERY)
       if remaining_missing_count != 0:
           raise RuntimeError(
               f"[{name}] Expected 0 resources without is_enabled, found {remaining_missing_count}."
           )

       logger.info("[%s] Completed database=%s backfilled_resources=%s", name, db.name, result.modified_count)


   def downgrade(db):
       resources_collection = db["resource_definition"]
       result = resources_collection.update_many(
           {},
           {"$unset": {"is_enabled": ""}},
       )
       logger.info("[%s] Completed database=%s removed_is_enabled_field=%s", name, db.name, result.modified_count)

Example 2: Edit Field - QMInput.print_level Enum Rewrite
--------------------------------------------------------

Case: ``QMInput.print_level`` changes from legacy integers ``0``-``4`` to the
existing ``PrintLevel`` enum strings.

Model change:

.. code-block:: python

   print_level: PrintLevel = Field(
       PrintLevel.LOW,
       json_schema_extra={"description": "Print level for the calculation"},
   )

Create ``server/migrations/20260527121000_qm_input_print_level_enum.py``:

.. code-block:: python

   """Convert QMInput.print_level from legacy integers to PrintLevel strings."""
   from __future__ import annotations

   import logging

   name = "20260527121000_qm_input_print_level_enum"
   dependencies = ["20260527120000_resource_definition_is_enabled"]

   logger = logging.getLogger(__name__)

   PRINT_LEVEL_BY_INT = {
       0: "SILENT",
       1: "LOW",
       2: "MEDIUM",
       3: "HIGH",
       4: "EXTREME",
   }


   def upgrade(db):
       qm_inputs = db["qm_input"]
       migrated_count = 0

       for old_value, new_value in PRINT_LEVEL_BY_INT.items():
           result = qm_inputs.update_many(
               {"print_level": old_value},
               {"$set": {"print_level": new_value}},
           )
           migrated_count += result.modified_count

       remaining_legacy_count = qm_inputs.count_documents({"print_level": {"$type": "int"}})
       if remaining_legacy_count:
           raise RuntimeError(
               f"[{name}] Found {remaining_legacy_count} QMInput documents with unsupported integer print_level."
           )

       logger.info("[%s] database=%s migrated_print_levels=%s", name, db.name, migrated_count)


   def downgrade(db):
       qm_inputs = db["qm_input"]
       migrated_count = 0

       for old_value, new_value in PRINT_LEVEL_BY_INT.items():
           result = qm_inputs.update_many(
               {"print_level": new_value},
               {"$set": {"print_level": old_value}},
           )
           migrated_count += result.modified_count

       logger.info("[%s] database=%s downgraded_print_levels=%s", name, db.name, migrated_count)

Example 3: Delete Field - QMInput.name
--------------------------------------

Case: ``QMInput`` drops the legacy ``name`` field because the node title is the
single source of truth and old documents must stop storing the duplicate value.

Model change:

.. code-block:: python

   # Remove this field from QMInput
   name: Optional[str] = None

Create ``server/migrations/20260527122000_qm_input_remove_name.py``:

.. code-block:: python

   """Remove the legacy QMInput.name field."""
   from __future__ import annotations

   import logging

   name = "20260527122000_qm_input_remove_name"
   dependencies = ["20260527121000_qm_input_print_level_enum"]

   logger = logging.getLogger(__name__)
   NAME_EXISTS_QUERY = {"name": {"$exists": True}}


   def upgrade(db):
       qm_inputs = db["qm_input"]
       result = qm_inputs.update_many(NAME_EXISTS_QUERY, {"$unset": {"name": ""}})

       remaining_name_count = qm_inputs.count_documents(NAME_EXISTS_QUERY)
       if remaining_name_count != 0:
           raise RuntimeError(f"[{name}] Expected 0 QMInput documents with legacy name, found {remaining_name_count}.")

       logger.info("[%s] database=%s removed_legacy_name=%s", name, db.name, result.modified_count)


   def downgrade(db):
       raise RuntimeError(
           f"[{name}] Forward-only migration. Restore QMInput.name from backup if rollback is required."
       )

Example 4: Edit Embedded Model - FileList to References
-------------------------------------------------------

Case: a parent document stores a full ``FileList`` object inside its own data,
and each ``FileStack`` inside that list must be moved to its own collection.

Partial migration shape:

.. code-block:: python

   def _migrate_embedded_file_stack(file_stack_collection, file_stack_data: dict):
       file_stack_id = file_stack_data.setdefault("_id", ObjectId())
       existing = file_stack_collection.find_one({"_id": file_stack_id})
       if existing is None:
           file_stack_collection.insert_one(file_stack_data)

       return file_stack_id


   def _migrate_file_list_data(file_stack_collection, file_list_data: dict) -> bool:
       if "file_stacks" not in file_list_data:
           return False

       new_elements = []
       for file_stack in file_list_data["file_stacks"]:
           if isinstance(file_stack, dict):
               file_stack_id = _migrate_embedded_file_stack(file_stack_collection, file_stack)
               new_elements.append(file_stack_id)
           else:
               new_elements.append(file_stack)

       del file_list_data["file_stacks"]
       file_list_data["elements"] = new_elements
       return True


   def _migrate_parent_collection(parent_collection, parent_fields: list[str], file_stack_collection) -> int:
       migrated_count = 0

       for doc in parent_collection.find():
           changed = False

           for parent_field in parent_fields:
               parent_value = doc.get(parent_field)
               if isinstance(parent_value, dict) and _migrate_file_list_data(file_stack_collection, parent_value):
                   changed = True

           if changed:
               parent_collection.replace_one({"_id": doc["_id"]}, doc)
               migrated_count += 1

       return migrated_count


   def upgrade(db):
       file_stack_collection = db["file_stack"]
       node_registry_count = _migrate_parent_collection(db["node_registry"], ["info_files"], file_stack_collection)
       file_list_io_count = _migrate_parent_collection(db["file_list_io"], ["file_list"], file_stack_collection)
       logger.info("[%s] database=%s migrated_node_registry=%s migrated_file_list_io=%s", name, db.name, node_registry_count, file_list_io_count)
