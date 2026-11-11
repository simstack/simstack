"""Read local process identity without requiring utilities in science images."""
from pathlib import Path
import os
import subprocess


def process_rows():
    """Return PID, parent PID, group ID and state; never treat query failure as empty."""
    if os.name == "posix" and Path("/proc/self/stat").is_file():
        rows = []
        for directory in Path("/proc").iterdir():
            if not directory.name.isdigit():
                continue
            try:
                fields = (directory / "stat").read_text().rpartition(")")[2].split()
            except FileNotFoundError:
                continue
            except PermissionError:
                if directory.stat().st_uid != os.getuid():
                    continue
                raise
            if len(fields) < 3:
                raise RuntimeError("Malformed process identity")
            rows.append((int(directory.name), int(fields[1]), int(fields[2]), fields[0]))
        return rows
    result = subprocess.run(["ps", "-axo", "pid=,ppid=,pgid=,stat="],
        capture_output=True, text=True, check=False, timeout=5, start_new_session=True)
    if result.returncode:
        raise RuntimeError("Process identity query failed")
    rows = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        fields = line.split()
        if len(fields) != 4 or not all(field.isdigit() for field in fields[:3]):
            raise RuntimeError("Malformed process identity")
        rows.append((*map(int, fields[:3]), fields[3]))
    if not rows:
        raise RuntimeError("Empty process identity query")
    return rows
