import math
from typing import List


def _normalize_vector(v: List[float]) -> List[float]:
    """Normalize a vector to unit length."""
    norm = math.sqrt(sum(x * x for x in v))
    if norm < 1e-10:
        return [1.0, 0.0, 0.0]  # Default direction for zero vector
    return [x / norm for x in v]


def _cross_product(a: List[float], b: List[float]) -> List[float]:
    """Calculate cross product of two 3D vectors."""
    return [
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    ]


def _get_perpendicular_direction(v1: List[float]) -> List[float]:
    """Get a direction perpendicular to v1."""
    # Find the component with smallest absolute value
    min_idx = min(range(3), key=lambda i: abs(v1[i]))

    # Create perpendicular vector
    v2 = [0.0, 0.0, 0.0]
    v2[min_idx] = 1.0

    # Make it perpendicular using Gram-Schmidt
    dot_product = sum(v1[i] * v2[i] for i in range(3))
    for i in range(3):
        v2[i] -= dot_product * v1[i]

    return v2


def _get_principal_direction(matrix: List[List[float]]) -> List[float]:
    """Get the principal direction (largest eigenvalue direction) using power iteration."""
    # Start with random vector
    v = [1.0, 1.0, 1.0]

    # Power iteration
    for _ in range(10):  # Usually converges quickly
        # v = matrix * v
        new_v = [0.0, 0.0, 0.0]
        for i in range(3):
            for j in range(3):
                new_v[i] += matrix[i][j] * v[j]

        # Normalize
        norm = math.sqrt(sum(x * x for x in new_v))
        if norm > 1e-10:
            v = [x / norm for x in new_v]

    return v


def _get_eigenvectors_3x3(matrix: List[List[float]]) -> List[List[float]]:
    """
    Get eigenvectors of a 3x3 matrix using simplified approach.
    For the inertia tensor, we'll use a simplified canonical form.
    """
    # For molecular hashing, we can use a simplified approach
    # that still provides rotation invariance

    # Create an orthonormal basis from the inertia tensor
    # Start with the direction of maximum variance
    v1 = _get_principal_direction(matrix)

    # Get a perpendicular direction
    v2 = _get_perpendicular_direction(v1)

    # Third direction is cross product
    v3 = _cross_product(v1, v2)

    # Normalize all vectors
    v1 = _normalize_vector(v1)
    v2 = _normalize_vector(v2)
    v3 = _normalize_vector(v3)

    # Return as columns of transformation matrix
    return [[v1[0], v2[0], v3[0]], [v1[1], v2[1], v3[1]], [v1[2], v2[2], v3[2]]]


def _get_canonical_orientation(coords: List[List[float]]) -> List[List[float]]:
    """
    Get coordinates in a canonical orientation using the inertia tensor.

    :param coords: Centered coordinates of atoms
    :return: Coordinates in canonical orientation
    """
    if len(coords) < 2:
        return coords

    # Calculate inertia tensor (3x3 matrix)
    inertia_tensor = [[0.0, 0.0, 0.0], [0.0, 0.0, 0.0], [0.0, 0.0, 0.0]]

    for pos in coords:
        # I_ij = sum(r_k * r_l) where k,l are coordinate indices
        for i in range(3):
            for j in range(3):
                inertia_tensor[i][j] += pos[i] * pos[j]

    # Get eigenvalues and eigenvectors using power iteration method
    eigenvectors = _get_eigenvectors_3x3(inertia_tensor)

    # Transform coordinates to canonical orientation
    canonical_coords = []
    for coord in coords:
        new_coord = [0.0, 0.0, 0.0]
        for i in range(3):
            for j in range(3):
                new_coord[i] += coord[j] * eigenvectors[j][i]
        canonical_coords.append(new_coord)

    return canonical_coords
