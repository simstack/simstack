"""Filesystem-only dependency checks must precede any uploaded Python import."""

import pytest

from simstack.workflows.dependencies import bootstrap_digests, interpreter_digest, validate_dependency_environment


TRUSTED_SITES = ["/operator/core-site", "/operator/extra-site"]
OPERATOR_PTH = "simstack-core-runtime.pth"


@pytest.fixture
def private_site(tmp_path):
    environment = tmp_path / "environment"
    site = environment / "lib" / "python3.12" / "site-packages"
    site.mkdir(parents=True)
    (site / OPERATOR_PTH).write_text("\n".join(TRUSTED_SITES) + "\n")
    return environment, site


def _validate(environment):
    validate_dependency_environment(
        environment, trusted_sites=TRUSTED_SITES, operator_pth=OPERATOR_PTH
    )


@pytest.mark.parametrize("name", ["SimStack", "ODMantic", "Motor", "PyMongo", "BSON"])
def test_protected_transitive_distribution_is_rejected_even_if_directory_is_renamed(
    private_site, name
):
    environment, site = private_site
    metadata = site / "innocent_dependency-1.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: 1\n")
    with pytest.raises(ValueError, match="protected distribution"):
        _validate(environment)


@pytest.mark.parametrize(
    "module", ["simstack", "simstack.py", "simstack.cpython-312.so", "odmantic", "motor.py", "pymongo", "bson", "sitecustomize.py", "usercustomize.py"]
)
def test_renamed_distribution_cannot_supply_protected_import_namespace(
    private_site, module
):
    environment, site = private_site
    (site / module).write_text("raise AssertionError('uploaded code must not execute')")
    with pytest.raises(ValueError, match="protected module"):
        _validate(environment)


@pytest.mark.parametrize("line", ["import malicious", "import\tmalicious", "import os; os.system('must-not-run')"])
def test_executable_pth_startup_hooks_are_rejected_without_execution(private_site, line):
    environment, site = private_site
    (site / "hook.pth").write_text(line + "\n")
    with pytest.raises(ValueError, match="executable startup hooks"):
        _validate(environment)


def test_extra_static_pth_cannot_escape_private_environment(private_site, tmp_path):
    environment, site = private_site
    outside = tmp_path / "outside"
    outside.mkdir()
    (site / "extra.pth").write_text(str(outside) + "\n")
    with pytest.raises(ValueError, match="escapes"):
        _validate(environment)


def test_inside_static_pth_directory_is_inspected_for_protected_modules(private_site):
    environment, site = private_site
    extra = environment / "extra"
    extra.mkdir()
    (extra / "simstack.py").write_text("raise AssertionError('must not execute')")
    (site / "extra.pth").write_text(str(extra) + "\n")
    with pytest.raises(ValueError, match="protected module"):
        _validate(environment)


def test_static_pth_symlink_target_cannot_escape(private_site, tmp_path):
    environment, site = private_site
    outside = tmp_path / "outside"
    outside.mkdir()
    (site / "extra").symlink_to(outside, target_is_directory=True)
    (site / "extra.pth").write_text("extra\n")
    with pytest.raises(ValueError, match="escapes"):
        _validate(environment)


def test_static_pth_trailing_whitespace_checks_the_same_directory_as_python(private_site):
    environment, site = private_site
    extra = environment / "extra"
    extra.mkdir()
    (extra / "simstack.py").write_text("raise AssertionError('must not import')")
    (site / "extra.pth").write_text(str(extra) + " \t\r\n")
    with pytest.raises(ValueError, match="protected module"):
        _validate(environment)


@pytest.mark.parametrize("separator", ["\u2028", "\x85"])
@pytest.mark.parametrize("protected", [False, True])
def test_unicode_line_separator_inside_pth_path_is_not_a_file_line_break(
    private_site, separator, protected
):
    environment, site = private_site
    extra = environment / f"one{separator}two"
    extra.mkdir()
    name = "simstack.py" if protected else "ordinary_dependency.py"
    (extra / name).write_text("raise AssertionError('must not import')")
    (site / "extra.pth").write_text(str(extra) + "\n")
    if protected:
        with pytest.raises(ValueError, match="protected module"):
            _validate(environment)
    else:
        _validate(environment)


@pytest.mark.parametrize("kind", ["replaced", "symlink", "missing", "oversized"])
def test_operator_core_path_declaration_is_required_exact_and_non_executable(
    private_site, tmp_path, kind
):
    environment, site = private_site
    declaration = site / OPERATOR_PTH
    if kind == "replaced":
        declaration.write_text("/some/other/core\n")
    elif kind == "symlink":
        declaration.unlink()
        external = tmp_path / "operator.pth"
        external.write_text("\n".join(TRUSTED_SITES) + "\n")
        declaration.symlink_to(external)
    elif kind == "missing":
        declaration.unlink()
    else:
        declaration.write_text("#" + "x" * (64 * 1024))
    with pytest.raises(ValueError, match="Core paths|startup paths"):
        _validate(environment)


def test_legitimate_pydantic_overlay_and_internal_static_paths_are_allowed(private_site):
    environment, site = private_site
    (site / "pydantic").mkdir()
    metadata = site / "pydantic-2.12.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text("Metadata-Version: 2.1\nName: Pydantic\nVersion: 2.12\n")
    (site / "pydantic_core.cpython-312.so").write_bytes(b"test-native-library")
    extra = environment / "extra"
    extra.mkdir()
    (extra / "another_library.py").write_text("raise AssertionError('never import for validation')")
    (site / "extra.pth").write_text(f"# safe private path\n{extra}\n")
    _validate(environment)


def test_missing_private_site_packages_fails_closed(tmp_path):
    environment = tmp_path / "empty"
    environment.mkdir()
    with pytest.raises(ValueError, match="no private site-packages"):
        _validate(environment)


def test_dependency_environment_checks_trusted_interpreter_bytes_without_running_it(private_site):
    environment, _ = private_site
    python = environment / "bin" / "python"
    python.parent.mkdir()
    python.write_bytes(b"operator-created interpreter")
    expected = interpreter_digest(python)
    validate_dependency_environment(
        environment, trusted_sites=TRUSTED_SITES, operator_pth=OPERATOR_PTH,
        expected_python_sha256=expected,
    )
    python.write_bytes(b"replacement from wheel data scripts")
    with pytest.raises(ValueError, match="trusted Python executable"):
        validate_dependency_environment(
            environment, trusted_sites=TRUSTED_SITES, operator_pth=OPERATOR_PTH,
            expected_python_sha256=expected,
        )


@pytest.mark.parametrize("replacement", ["pyvenv.cfg", "bin/python3.12"])
def test_venv_configuration_and_python_aliases_cannot_be_replaced(private_site, replacement):
    environment, _ = private_site
    binaries = environment / "bin"
    binaries.mkdir()
    for name in ("python", "python3.12"):
        (binaries / name).write_bytes(b"operator-created interpreter")
    (environment / "pyvenv.cfg").write_text("home = /operator\n")
    expected = bootstrap_digests(environment)
    validate_dependency_environment(
        environment, trusted_sites=TRUSTED_SITES, operator_pth=OPERATOR_PTH,
        expected_bootstrap=expected,
    )
    (environment / replacement).write_bytes(b"untrusted bootstrap replacement")
    with pytest.raises(ValueError, match="trusted Python bootstrap"):
        validate_dependency_environment(
            environment, trusted_sites=TRUSTED_SITES, operator_pth=OPERATOR_PTH,
            expected_bootstrap=expected,
        )


@pytest.mark.parametrize("override", ["python._pth", "bin/python._pth", "lib/python3.12._pth", "bin/pyvenv.cfg", "bin/pybuilddir.txt"])
def test_new_startup_override_files_cannot_bypass_unchanged_bootstrap_hashes(private_site, override):
    environment, _ = private_site
    binaries = environment / "bin"
    binaries.mkdir()
    (binaries / "python").write_bytes(b"operator-created interpreter")
    (environment / "pyvenv.cfg").write_text("home = /operator\n")
    expected = bootstrap_digests(environment)
    path = environment / override
    path.write_text("/attacker/core\n")
    with pytest.raises(ValueError, match="startup paths|startup configuration"):
        validate_dependency_environment(
            environment, trusted_sites=TRUSTED_SITES, operator_pth=OPERATOR_PTH,
            expected_bootstrap=expected,
        )
