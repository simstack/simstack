"""Explicit registry resolution; local image references remain local."""

_DOCKER_HUB_LIBRARY_PREFIX = "docker.io/library/"

def _image_has_registry_host(image: str) -> bool:
    """True if the first path component looks like a registry (host:port or host.tld)."""
    if "/" not in image:
        return False
    first = image.split("/", 1)[0]
    return ":" in first or "." in first


def resolve_docker_pull_ref(image: str, docker_registry: str | None) -> str | None:
    """Registry ref to ``docker pull``, or None to skip (local / Hub library alias)."""
    if not image or image.endswith(".sif") or image.startswith("docker://"):
        return None

    name = image
    if name.startswith(_DOCKER_HUB_LIBRARY_PREFIX):
        name = name[len(_DOCKER_HUB_LIBRARY_PREFIX) :]
    elif name.startswith("docker.io/"):
        name = name[len("docker.io/") :]

    if _image_has_registry_host(name):
        return name

    if docker_registry:
        return f"{docker_registry.rstrip('/')}/{name}"
    return None
