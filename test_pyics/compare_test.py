"""
Test how ``SimpleTest`` compares results with expected values (``pyics.tests.compare``).
"""

from types import ModuleType
from typing import Any

import pytest

from pyics.tests.compare import IncomparableError, equal, explain


@pytest.fixture
def np() -> ModuleType:
    return pytest.importorskip("numpy")


@pytest.fixture
def pd() -> ModuleType:
    return pytest.importorskip("pandas")


def _explanation(res: Any, expected: Any) -> list[str]:
    assert not equal(res, expected)
    return explain(res, expected)


# --- plain values ---


@pytest.mark.parametrize(
    ("res", "expected", "matches"),
    [
        (3, 3, True),
        (3, 3.0, True),
        (3, 4, False),
        ("ab", "ab", True),
        ([1, [2, 3]], [1, [2, 3]], True),
        ([1, [2, 3]], [1, [2, 4]], False),
        ([1, 2], [1, 2, 3], False),
        (("A", "B"), ["A", "B"], False),
        ({"a": 1, "b": 2}, {"b": 2, "a": 1}, True),
        ({"a": 1}, {"a": 1, "b": 2}, False),
        ({1, 2}, {2, 1}, True),
        (None, None, True),
    ],
)
def test_plain_values_are_compared_by_eq(
    res: Any, expected: Any, matches: bool
) -> None:
    assert equal(res, expected) is matches


def test_plain_values_are_explained_by_pytest() -> None:
    assert _explanation(3, 4) == ["3 == 4"]
    lines = _explanation(["B", "C"], ["B", "D"])
    assert lines[0] == "['B', 'C'] == ['B', 'D']"
    assert "At index 1 diff: 'C' != 'D'" in lines


def test_the_same_object_matches_itself_in_a_list() -> None:
    nan = float("nan")
    assert [nan] == [nan]  # as by Python, since the items are identical
    assert equal([nan], [nan])
    assert not equal([nan], [float("nan")])


def test_an_ambiguous_eq_fails_clearly() -> None:
    class Ambiguous:
        def __bool__(self) -> bool:
            raise ValueError("ambiguous")

        def __repr__(self) -> str:
            return "Ambiguous()"

    class Vector:
        def __eq__(self, other: object) -> Any:
            return Ambiguous()

        def __repr__(self) -> str:
            return "Vector()"

    with pytest.raises(IncomparableError) as excinfo:
        equal(Vector(), Vector())
    assert isinstance(excinfo.value, AssertionError)  # i.e. the test fails
    assert str(excinfo.value).splitlines() == [
        "Vector() == Vector()",
        "",
        "Can't tell whether they are equal: == gave Ambiguous(), not True or False",
    ]


# --- numpy arrays ---


def test_equal_arrays_match(np: ModuleType) -> None:
    assert equal(np.array([2, 4]), np.array([2, 4]))
    assert equal(np.array([[1, 2], [3, 4]]), np.array([[1, 2], [3, 4]]))
    assert equal(np.array([], dtype=int), np.array([], dtype=int))
    assert equal(np.array(5), np.array(5))
    assert equal(np.array(["a", "b"]), np.array(["a", "b"]))


def test_integers_of_any_size_match(np: ModuleType) -> None:
    """e.g. numpy's default integers, which are 32 bits wide on Windows with numpy 1"""
    assert equal(np.array([1, 2], dtype=np.int32), np.array([1, 2], dtype=np.int64))


def test_nans_match(np: ModuleType) -> None:
    assert equal(np.array([1.0, np.nan]), np.array([1.0, np.nan]))
    assert equal(np.array([1j, np.nan]), np.array([1j, np.nan]))
    assert not equal(np.array([1.0, np.nan]), np.array([np.nan, 1.0]))


def test_differing_values_are_listed_by_position(np: ModuleType) -> None:
    assert _explanation(np.array([2, 4, 6]), np.array([2, 0, 0])) == [
        "array([2, 4, 6]) == array([2, 0, 0])",
        "",
        "Values differ at 2 of 3 positions:",
        "At index 1 diff: 4 != 0",
        "At index 2 diff: 6 != 0",
    ]


def test_positions_in_several_dimensions(np: ModuleType) -> None:
    lines = _explanation(np.array([[1, 2], [3, 4]]), np.array([[1, 2], [3, 5]]))
    assert lines[2:] == [
        "Values differ at 1 of 4 positions:",
        "At index (1, 1) diff: 4 != 5",
    ]


def test_only_the_first_positions_are_listed(np: ModuleType) -> None:
    lines = _explanation(np.arange(30), np.arange(30) + 1)
    assert (
        lines[0] == "array([ 0,  1..., 27, 28, 29]) == array([ 1,  2..., 28, 29, 30])"
    )
    assert lines[2] == "Values differ at 30 of 30 positions:"
    assert lines[3] == "At index 0 diff: 0 != 1"
    assert lines[-2:] == ["At index 9 diff: 9 != 10", "... and 20 more"]


def test_differing_zero_dimensional_arrays(np: ModuleType) -> None:
    assert _explanation(np.array(1.5), np.array(2.5))[2:] == [
        "Values differ: 1.5 != 2.5"
    ]


def test_shapes_must_match(np: ModuleType) -> None:
    """rather than one array being broadcast to the other's shape"""
    assert _explanation(np.array([[2, 4]]), np.array([2, 4]))[2:] == [
        "Shapes differ: (1, 2) != (2,)"
    ]
    assert _explanation(np.array([2, 2]), np.array([2]))[2:] == [
        "Shapes differ: (2,) != (1,)"
    ]


def test_kinds_of_values_must_match(np: ModuleType) -> None:
    assert _explanation(np.array([2.0, 4.0]), np.array([2, 4]))[2:] == [
        "Types of values differ: float64 != int64"
    ]


@pytest.mark.parametrize("res", [[2, 4], (2, 4), 2])
def test_a_result_that_isnt_an_array_fails(np: ModuleType, res: Any) -> None:
    lines = _explanation(res, np.array([2, 4]))
    assert lines[-1] == f"Types differ: {type(res).__name__} != numpy.ndarray"


def test_an_array_result_doesnt_match_a_list(np: ModuleType) -> None:
    assert _explanation(np.array([2, 4]), [2, 4])[2:] == [
        "Types differ: numpy.ndarray != list"
    ]


def test_numpy_scalars_are_compared_by_eq(np: ModuleType) -> None:
    assert equal(np.float64(4.0), 4.0)
    assert equal(np.int64(4), np.float64(4.0))
    assert not equal(np.float64(4.0), 4.5)


def test_arrays_of_objects_are_compared_item_by_item(np: ModuleType) -> None:
    ragged = np.empty(2, dtype=object)
    ragged[:] = [np.array([1, 2]), np.array([3])]
    other = np.empty(2, dtype=object)
    other[:] = [np.array([1, 2]), np.array([4])]
    assert equal(ragged, ragged.copy())
    assert _explanation(ragged, other)[2:] == [
        "Values differ at 1 of 2 positions:",
        "At index 1 diff: array([3]) != array([4])",
    ]


def test_arrays_in_lists_tuples_and_dicts(np: ModuleType) -> None:
    assert equal((np.array([1, 2]), 3), (np.array([1, 2]), 3))
    assert equal({"x": [np.array([1, 2])]}, {"x": [np.array([1, 2])]})
    assert not equal([np.array([1, 2])], [np.array([1, 3])])
    assert not equal([np.array([1, 2])], [np.array([1, 2]), 3])

    lines = _explanation(
        (np.array([1, 2]), np.array([3])), (np.array([1, 2]), np.array([4]))
    )
    assert lines[0] == "(array([1, 2]), array([3])) == (array([1, 2]), array([4]))"
    assert "At index 1 diff: array([3]) != array([4])" in lines
    lines = _explanation(
        {"x": np.array([1, 2]), "y": 1}, {"x": np.array([1, 2]), "y": 2}
    )
    assert "Differing items:" in lines
    assert "{'y': 1} != {'y': 2}" in lines


# --- pandas objects ---


def test_equal_data_frames_and_series_match(pd: ModuleType) -> None:
    df = pd.DataFrame({"a": [1, 2], "b": [1.5, float("nan")]})
    assert equal(df, df.copy())
    assert equal(df["a"], df["a"].copy())
    assert equal(df.index, pd.Index([0, 1]))


def test_differing_data_frames_are_explained(pd: ModuleType) -> None:
    lines = _explanation(pd.DataFrame({"a": [1, 2]}), pd.DataFrame({"a": [1, 3]}))
    assert lines[2:] == [
        'DataFrame.iloc[:, 0] (column name="a") are different',
        "",
        'DataFrame.iloc[:, 0] (column name="a") values are different (50.0 %)',
        "[index]: [0, 1]",
        "[left]:  [1, 2]",
        "[right]: [1, 3]",
    ]


def test_types_of_columns_must_match(pd: ModuleType) -> None:
    lines = _explanation(pd.DataFrame({"a": [1, 2]}), pd.DataFrame({"a": [1.0, 2.0]}))
    assert 'Attribute "dtype" are different' in lines


def test_differing_series_are_explained(pd: ModuleType) -> None:
    lines = _explanation(pd.Series([1, 2]), pd.Series([1, 3]))
    assert "Series values are different (50.0 %)" in lines


def test_a_series_doesnt_match_a_data_frame(pd: ModuleType) -> None:
    lines = _explanation(pd.Series([1, 2]), pd.DataFrame({"a": [1, 2]}))
    assert lines[-1].startswith("Types differ: pandas.")
    assert _explanation([1, 2], pd.Series([1, 2]))[-1].startswith("Types differ: list")
