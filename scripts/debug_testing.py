import sys
import os
import platform

# Windows: platform.machine() and platform.processor() can hang indefinitely when
# called via WMI (common with numpy/scipy). This is especially problematic during debugging.
if sys.platform == "win32":
    # On some Windows systems, platform.uname() and related functions can hang
    # indefinitely because they try to use WMI. We provide static fallbacks.

    # Use environment variables or common defaults if possible to avoid calling hanging functions
    _system = os.environ.get("OS", "Windows")
    _node = os.environ.get("COMPUTERNAME", "localhost")
    _release = "10"  # Default to 10
    _version = "10.0.19041"  # Default version

    platform.machine = lambda: "AMD64"
    platform.processor = lambda: "Intel64 Family 6 Model 158 Stepping 10, GenuineIntel"
    platform.uname = lambda: platform.uname_result(
        _system,
        _node,
        _release,
        _version,
        "AMD64",
        "Intel64 Family 6 Model 158 Stepping 10, GenuineIntel",
    )

# Add the project root and simstack/src to sys.path to ensure simstack can be
# imported when running/debugging this script directly from the IDE.
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

src_path = os.path.join(project_root, "simstack", "src")
if os.path.exists(src_path) and src_path not in sys.path:
    sys.path.insert(0, src_path)

if __name__ == "__main__":
    print("Hello, World!")
