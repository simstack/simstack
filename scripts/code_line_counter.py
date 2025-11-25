import argparse
from pathlib import Path
from typing import Dict, Set, List
import fnmatch


def is_code_line(line: str) -> bool:
    """
    Check if a line contains actual code (not just whitespace or comments).
    This is a simple heuristic - you might want to make it more sophisticated.
    """
    stripped = line.strip()
    if not stripped:
        return False

    # Skip single-line comments
    if (
        stripped.startswith("#")
        or stripped.startswith("//")
        or stripped.startswith("*")
    ):
        return False

    return True


def should_exclude_path(path: Path, excluded_patterns: List[str]) -> bool:
    """
    Check if a path should be excluded based on patterns.

    Args:
        path: Path to check
        excluded_patterns: List of patterns to exclude

    Returns:
        True if path should be excluded
    """
    path_str = str(path)
    path_parts = path.parts

    for pattern in excluded_patterns:
        # Check if any part of the path matches the pattern
        for part in path_parts:
            if fnmatch.fnmatch(part, pattern):
                return True

        # Also check the full path
        if fnmatch.fnmatch(path_str, pattern):
            return True

    return False


def count_lines_in_file(file_path: Path) -> Dict[str, int]:
    """
    Count different types of lines in a file.
    Returns dict with total, code, blank, and comment lines.
    """
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
    except (IOError, OSError) as e:
        print(f"Warning: Could not read {file_path}: {e}")
        return {"total": 0, "code": 0, "blank": 0, "comments": 0}

    total_lines = len(lines)
    blank_lines = 0
    comment_lines = 0
    code_lines = 0

    in_multiline_comment = False

    for line in lines:
        stripped = line.strip()

        # Count blank lines
        if not stripped:
            blank_lines += 1
            continue

        # Handle multiline comments (/* ... */ for JS/TS, """ ... """ for Python)
        if file_path.suffix == ".py":
            if '"""' in stripped or "'''" in stripped:
                in_multiline_comment = not in_multiline_comment
                comment_lines += 1
                continue
            elif in_multiline_comment:
                comment_lines += 1
                continue
        elif file_path.suffix in [".js", ".ts", ".tsx"]:
            if "/*" in stripped:
                in_multiline_comment = True
            if in_multiline_comment:
                comment_lines += 1
                if "*/" in stripped:
                    in_multiline_comment = False
                continue

        # Check for single-line comments
        if (
            stripped.startswith("#")
            or stripped.startswith("//")
            or stripped.startswith("*")
        ):
            comment_lines += 1
        else:
            code_lines += 1

    return {
        "total": total_lines,
        "code": code_lines,
        "blank": blank_lines,
        "comments": comment_lines,
    }


def count_lines_of_code(
    directory: str, extensions: Set[str] = None, excluded_patterns: List[str] = None
) -> Dict[str, Dict[str, int]]:
    """
    Recursively count lines of code in files with specified extensions.

    Args:
        directory: Starting directory path
        extensions: Set of file extensions to include (default: .py, .js, .ts, .tsx)
        excluded_patterns: List of directory/file patterns to exclude

    Returns:
        Dictionary with file counts and totals
    """
    if extensions is None:
        extensions = {".py", ".js", ".ts", ".tsx"}

    if excluded_patterns is None:
        excluded_patterns = [
            "node_modules",
            ".git",
            ".svn",
            ".hg",
            "__pycache__",
            ".pytest_cache",
            ".venv",
            "venv",
            "env",
            ".env",
            "build",
            "dist",
            ".next",
            "coverage",
            ".nyc_output",
            "vendor",
            "target",
            "out",
            ".idea",
            ".vscode",
            "*.min.js",
            "*.min.css",
        ]

    directory_path = Path(directory)
    if not directory_path.exists():
        raise ValueError(f"Directory {directory} does not exist")

    results = {
        "files": {},
        "totals": {"total": 0, "code": 0, "blank": 0, "comments": 0},
        "by_extension": {},
        "excluded_count": 0,
    }

    # Initialize extension counters
    for ext in extensions:
        results["by_extension"][ext] = {
            "total": 0,
            "code": 0,
            "blank": 0,
            "comments": 0,
            "file_count": 0,
        }

    # Walk through directory recursively
    for file_path in directory_path.rglob("*"):
        if file_path.is_file() and file_path.suffix in extensions:
            # Check if file should be excluded
            if should_exclude_path(
                file_path.relative_to(directory_path), excluded_patterns
            ):
                results["excluded_count"] += 1
                continue

            file_stats = count_lines_in_file(file_path)

            # Store individual file results
            relative_path = file_path.relative_to(directory_path)
            results["files"][str(relative_path)] = file_stats

            # Update totals
            for key in ["total", "code", "blank", "comments"]:
                results["totals"][key] += file_stats[key]
                results["by_extension"][file_path.suffix][key] += file_stats[key]

            results["by_extension"][file_path.suffix]["file_count"] += 1

    return results


def print_results(results: Dict[str, Dict[str, int]], show_files: bool = False):
    """Print formatted results."""
    print("=" * 60)
    print("LINE COUNT SUMMARY")
    print("=" * 60)

    # Print totals
    totals = results["totals"]
    print(f"Total Lines:     {totals['total']:,}")
    print(f"Code Lines:      {totals['code']:,}")
    print(f"Blank Lines:     {totals['blank']:,}")
    print(f"Comment Lines:   {totals['comments']:,}")

    if results.get("excluded_count", 0) > 0:
        print(f"Excluded Files:  {results['excluded_count']:,}")
    print()

    # Print by extension
    print("BY FILE TYPE:")
    print("-" * 40)
    for ext, stats in results["by_extension"].items():
        if stats["file_count"] > 0:
            print(f"{ext} files ({stats['file_count']} files):")
            print(f"  Total:    {stats['total']:,}")
            print(f"  Code:     {stats['code']:,}")
            print(f"  Blank:    {stats['blank']:,}")
            print(f"  Comments: {stats['comments']:,}")
            print()

    # Print individual files if requested
    if show_files:
        print("BY FILE:")
        print("-" * 40)
        for file_path, stats in results["files"].items():
            print(f"{file_path}:")
            print(
                f"  Total: {stats['total']:,}, Code: {stats['code']:,}, "
                f"Blank: {stats['blank']:,}, Comments: {stats['comments']:,}"
            )


def main():
    parser = argparse.ArgumentParser(
        description="Count lines of code in Python, JavaScript, and TypeScript files"
    )
    parser.add_argument("directory", help="Directory to scan recursively")
    parser.add_argument(
        "--extensions",
        nargs="+",
        default=[".py", ".js", ".ts", ".tsx"],
        help="File extensions to include (default: .py .js .ts .tsx)",
    )
    parser.add_argument(
        "--exclude",
        nargs="+",
        help="Additional patterns to exclude (beyond default exclusions)",
    )
    parser.add_argument(
        "--no-default-excludes",
        action="store_true",
        help="Don't use default exclusion patterns",
    )
    parser.add_argument(
        "--show-files", action="store_true", help="Show individual file statistics"
    )
    parser.add_argument(
        "--list-excludes",
        action="store_true",
        help="List default exclusion patterns and exit",
    )

    args = parser.parse_args()

    # Default exclusion patterns
    default_excludes = [
        "node_modules",
        ".git",
        ".svn",
        ".hg",
        "__pycache__",
        ".pytest_cache",
        ".venv",
        "venv",
        "env",
        ".env",
        "build",
        "dist",
        ".next",
        "coverage",
        ".nyc_output",
        "vendor",
        "target",
        "out",
        ".idea",
        ".vscode",
        "*.min.js",
        "*.min.css",
    ]

    if args.list_excludes:
        print("Default exclusion patterns:")
        for pattern in default_excludes:
            print(f"  {pattern}")
        return 0

    try:
        # Convert extensions list to set, ensure they start with dot
        extensions = set()
        for ext in args.extensions:
            if not ext.startswith("."):
                ext = "." + ext
            extensions.add(ext)

        # Handle exclusion patterns
        excluded_patterns = []
        if not args.no_default_excludes:
            excluded_patterns.extend(default_excludes)

        if args.exclude:
            excluded_patterns.extend(args.exclude)

        print(f"Scanning directory: {args.directory}")
        print(f"File extensions: {', '.join(sorted(extensions))}")
        if excluded_patterns:
            print(
                f"Excluding patterns: {', '.join(excluded_patterns[:5])}"
                + (
                    f" (and {len(excluded_patterns)-5} more)"
                    if len(excluded_patterns) > 5
                    else ""
                )
            )
        print()

        results = count_lines_of_code(args.directory, extensions, excluded_patterns)
        print_results(results, args.show_files)

    except Exception as e:
        print(f"Error: {e}")
        return 1

    return 0


if __name__ == "__main__":
    exit(main())
