"""Check permissions on private files produced in the current user's temp directory."""

import os
import stat
import subprocess


def assert_private_file(path):
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        return

    # Windows inherits the user's DACL. Only the user, SYSTEM and admins may have access.
    script = """
    $ErrorActionPreference = 'Stop'
    $acl = [System.IO.File]::GetAccessControl($env:SIMSTACK_TEST_FILE)
    $allowed = @([Security.Principal.WindowsIdentity]::GetCurrent().User.Value,
                 'S-1-5-18', 'S-1-5-32-544')
    $owner = $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
    if ($owner -notin $allowed) { throw "Unexpected file owner: $owner" }
    $allowed += 'S-1-3-4'  # OWNER RIGHTS
    $rules = $acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])
    $grants = @($rules | Where-Object { $_.AccessControlType -eq 'Allow' })
    if ($grants.Count -eq 0) { throw 'No access rules on the private file' }
    foreach ($rule in $grants) {
        if ($rule.IdentityReference.Value -notin $allowed) { throw $rule.IdentityReference }
    }
    """
    subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        env={**os.environ, "SIMSTACK_TEST_FILE": str(path)}, check=True,
    )
