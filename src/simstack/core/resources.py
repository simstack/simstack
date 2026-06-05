import logging
from typing import List

logger = logging.getLogger("resources")


class AllowedResources:
    """
    Singleton class that holds a list of allowed resource strings.

    This class is problematic because the @node decorator is called before the config is read.
    @node may contain Parameters which set default values for resources.
    The solution is to create the AllowedResources singleton class here, and set the resources from
    context.initialize. Before context.initialize is called, any resource is allowed.

    However, resources are validated on read, so before any node is executed, the resources must be set.
    AllowedResources can be set only once.
    """

    _instance = None
    _resources: List[str] = []

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(AllowedResources, cls).__new__(cls)
        return cls._instance

    def __init__(self):
        # Only initialize once
        self._initialized = False

    def set_resources(self, resources: List[str]) -> None:
        """Set the list of allowed resources."""
        if self._initialized and resources != self._resources:
            raise RuntimeError("Resources can only be set once")
        self._initialized = True
        self._resources = resources.copy() if resources else []

    def get_resources(self) -> List[str]:
        """Get the list of allowed resources."""
        return self._resources.copy()

    def add_resource(self, resource: str) -> None:
        """Add a single resource to the list."""
        if resource not in self._resources:
            self._resources.append(resource)

    def remove_resource(self, resource: str) -> None:
        if resource == "self":
            raise ValueError("Cannot remove 'self' resource")
        """Remove a resource from the list."""
        if resource in self._resources:
            self._resources.remove(resource)

    def clear_resources(self) -> None:
        """Clear all resources from the list."""
        self._resources.clear()
        self._initialized = False

    def has_resource(self, resource: str) -> bool:
        """Check if a resource exists in the list."""
        if hasattr(self, "_initialized") and self._initialized:
            return resource in self._resources
        else:
            return True  # before this is initialized, any resource is allowed

    def __len__(self) -> int:
        """Return the number of resources."""
        return len(self._resources)

    def __iter__(self):
        """Make the class iterable."""
        return iter(self._resources)

    def __contains__(self, resource: str) -> bool:
        """Support 'in' operator."""
        return resource in self._resources

    def __str__(self) -> str:
        """String representation."""
        return f"AllowedResources({self._resources})"

    def __repr__(self) -> str:
        """String representation."""
        return f"AllowedResources({self._resources!r})"

    @property
    def initialized(self):
        return hasattr(self, "_initialized")


allowed_resources = AllowedResources()
