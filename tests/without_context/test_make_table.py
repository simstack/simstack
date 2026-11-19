import subprocess
import sys

from simstack.models import IntData
from simstack.util.make_table import make_table_entries, make_table_entries_helper


def test_table_helpers_can_be_imported_before_models():
    subprocess.run([sys.executable, "-c", "from simstack.util.make_table import make_table_entries"],
                   check=True)


def test_table_name_is_preserved_with_rows():
    table = make_table_entries([IntData(value=3)], table_name="Experiments")
    assert table["tableName"] == "Experiments"


def test_custom_table_receives_requested_options():
    class CustomTable:
        def make_table_entries(self, max_recursion_level, drop_id, current_level, visited, field_prefix):
            return (max_recursion_level, drop_id, current_level, visited, field_prefix)

    model = CustomTable()
    previously_visited = {101}
    depth, drop, level, seen, prefix = make_table_entries_helper(
        model, max_recursion_level=4, drop_id=False, current_level=2,
        visited=previously_visited, field_prefix="experiment."
    )
    assert (depth, drop, level, prefix) == (4, False, 2, "experiment.")
    assert seen is previously_visited
