from classmate.worker.wait_for_schema import schema_is_current


def test_schema_is_current_when_all_heads_are_applied() -> None:
    assert schema_is_current({"0001_initial"}, {"0001_initial"})


def test_schema_is_not_current_when_a_head_is_missing() -> None:
    assert not schema_is_current(set(), {"0001_initial"})


def test_schema_is_not_current_without_repository_heads() -> None:
    assert not schema_is_current({"0001_initial"}, set())


def test_schema_is_current_with_multiple_heads() -> None:
    assert schema_is_current({"head_a", "head_b"}, {"head_a", "head_b"})
    assert not schema_is_current({"head_a"}, {"head_a", "head_b"})
