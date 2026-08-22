from obsalt.assemble.promotion import cas_pointer


def test_cas_promotes_first_revision() -> None:
    result = cas_pointer(None, None, 1)
    assert result.promoted
    assert result.active_revision == 1


def test_cas_rebases_on_conflict() -> None:
    result = cas_pointer(2, 1, 3)
    assert not result.promoted
    assert result.rebased
