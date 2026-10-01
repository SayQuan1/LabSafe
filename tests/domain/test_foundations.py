"""Pure foundation structure and capacity guards."""

import pytest

from packages.domain.foundations import (
    create_inspection,
    location_parent,
    require_active,
    template_items,
)
from packages.domain.security import Principal, ServiceError

ACTOR = Principal("user", "tenant", "inspector", (("inspector", "laboratory", "lab"),))


@pytest.mark.parametrize("kind", ["room", "area", "shelf", "cabinet", "invalid"])
@pytest.mark.parametrize("parent", [None, "room", "area", "shelf", "cabinet"])
def test_location_parent_matrix(kind, parent):
    allowed = {
        "room": {None},
        "area": {"room"},
        "shelf": {"room", "area"},
        "cabinet": {"room", "area"},
    }
    if parent in allowed.get(kind, set()):
        location_parent(kind, parent)
    else:
        with pytest.raises(ServiceError, match="parent") as error:
            location_parent(kind, parent)
        assert error.value.status == 422


@pytest.mark.parametrize("status", ["active", "archived", "unknown"])
def test_active_resource_guard(status):
    if status == "active":
        require_active(status)
    else:
        with pytest.raises(ServiceError) as error:
            require_active(status)
        assert error.value.status == 409


@pytest.mark.parametrize("count", [0, 1, 100, 101])
def test_template_capacity(count):
    items = [{"id": i, "code": str(i), "sort_order": i} for i in range(count)]
    if 1 <= count <= 100:
        template_items(items)
    else:
        with pytest.raises(ServiceError):
            template_items(items)


@pytest.mark.parametrize("key", ["id", "code", "sort_order"])
def test_template_unique_fields(key):
    items = [{"id": i, "code": str(i), "sort_order": i} for i in range(2)]
    items[1][key] = items[0][key]
    with pytest.raises(ServiceError, match="Duplicate"):
        template_items(items)


@pytest.mark.parametrize(
    "count,locations,accepted",
    [
        (1, ["a"], True),
        (10, list(range(10)), True),
        (100, ["a"], True),
        (1, list(range(100)), True),
        (101, ["a"], False),
        (2, list(range(51)), False),
        (0, ["a"], False),
        (1, [], False),
        (1, ["a", "a"], False),
    ],
)
def test_inspection_cartesian_capacity(count, locations, accepted):
    if accepted:
        create_inspection(ACTOR, "lab", "published", count, locations)
    else:
        with pytest.raises(ServiceError) as error:
            create_inspection(ACTOR, "lab", "published", count, locations)
        assert error.value.status == 422


@pytest.mark.parametrize("status", ["draft", "retired"])
def test_inspection_requires_published(status):
    with pytest.raises(ServiceError) as error:
        create_inspection(ACTOR, "lab", status, 1, ["a"])
    assert error.value.status == 409


def test_inspection_capture_scope():
    with pytest.raises(ServiceError) as error:
        create_inspection(ACTOR, "other", "published", 1, ["a"])
    assert error.value.status == 404
    viewer = Principal("u", "t", "v", (("viewer", "laboratory", "lab"),))
    with pytest.raises(ServiceError) as error:
        create_inspection(viewer, "lab", "published", 1, ["a"])
    assert error.value.status == 403
