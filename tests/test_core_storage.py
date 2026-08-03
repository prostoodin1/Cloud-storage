import pytest

from cloud_storage.core.storage import InvalidLogicalPath, normalize_logical_path


@pytest.mark.parametrize(
    "value",
    ["../secret", "/absolute", "folder\\file", "a//b", "folder/../secret", "\x00bad"],
)
def test_logical_path_rejects_traversal_and_ambiguous_names(value: str) -> None:
    with pytest.raises(InvalidLogicalPath):
        normalize_logical_path(value)


def test_logical_path_keeps_unicode_and_nested_folders() -> None:
    assert normalize_logical_path("Фото/Отпуск/море.jpg") == "Фото/Отпуск/море.jpg"
