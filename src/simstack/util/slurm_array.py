"""Native Slurm array selection and stable identities shared by Server and batch jobs."""
import hashlib
import re


def index_ranges(spec):
    """Parse the index list separately from Slurm's optional %concurrency limit."""
    selection, separator, limit = spec.partition("%")
    if separator and not re.fullmatch(r"[0-9]+", limit):
        raise ValueError("Invalid Slurm array concurrency")
    ranges = []
    for item in selection.split(","):
        match = re.fullmatch(r"([0-9]+)(?:-([0-9]+)(?::([0-9]+))?)?", item)
        if not match:
            raise ValueError("Invalid Slurm array index range")
        start, end, step = match.groups()
        first, last, stride = int(start), int(end or start), int(step or 1)
        # Slurm's own MaxArraySize is at most 4,000,001.
        if first > last or last > 4_000_000 or stride == 0:
            raise ValueError("Invalid Slurm array index range")
        ranges.append(range(first, last + 1, stride))
    return ranges


def element_count(ranges):
    if len(ranges) == 1:
        return len(ranges[0])
    # Overlapping and stepped ranges are legal. A byte per possible index
    # bounds memory independently of the number of overlapping selections.
    present = bytearray(max(span.stop for span in ranges))
    for span in ranges:
        present[span.start:span.stop:span.step] = b"\x01" * len(span)
    return present.count(1)


def element_id(submission, index):
    return hashlib.sha256(f"{submission}:{int(index)}".encode()).hexdigest()[:24]


def normalize_array_option(value):
    """Turn the standalone extra option into the same typed assignment field."""
    if not isinstance(value, dict) or not isinstance(value.get("extra_options"), (list, tuple)):
        return value
    others = []
    for option in value["extra_options"]:
        match = re.fullmatch(r"--array(?:=|\s+)([^\s]+)", option) if isinstance(option, str) else None
        if match is None:
            others.append(option)
        elif value.get("array") is not None:
            raise ValueError("Slurm array was specified more than once")
        else:
            value = {**value, "array": match[1]}
    return {**value, "extra_options": others}
