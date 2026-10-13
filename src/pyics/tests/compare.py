"""
Comparing the result of a function with the expected one, for ``SimpleTest``.

Plain values are compared with ``==``. numpy arrays and pandas objects are compared as a
whole instead, since their ``==`` compares element by element. numpy and pandas aren't
dependencies of pyics: their objects are only looked for when they are imported, as
they are when a result or an expected value is one of them.
"""

import sys
from typing import Any

from _pytest._io.saferepr import saferepr
from _pytest.assertion.util import assertrepr_compare, dummy_highlighter

# length of each side of a summary, as in pytest's: (80 - 15 - len("==") - 2) // 2
_SUMMARY_SIZE = 30
# positions listed when arrays differ
_MAX_POSITIONS = 10


class IncomparableError(AssertionError):
    """A result can't be compared with the expected value, e.g. since its ``==`` gives
    neither ``True`` nor ``False``."""


def _is_array(value: Any) -> bool:
    np = sys.modules.get("numpy")
    return np is not None and isinstance(value, np.ndarray)


def _pandas_types() -> tuple[type, ...]:
    pd = sys.modules.get("pandas")
    return () if pd is None else (pd.DataFrame, pd.Series, pd.Index)


def summary(left: Any, right: Any, *, op: str = "==") -> str:
    """The comparison of ``left`` and ``right`` by ``op`` in a line, with both
    shortened, e.g. ``'3' in '9'``."""
    left = saferepr(left, maxsize=_SUMMARY_SIZE)
    return f"{left} {op} {saferepr(right, maxsize=_SUMMARY_SIZE)}"


def _type_name(value: Any) -> str:
    cls = type(value)
    if cls.__module__ == "builtins":
        return cls.__qualname__
    return f"{cls.__module__}.{cls.__qualname__}"


def equal(res: Any, expected: Any) -> bool:
    """
    Whether ``res`` matches ``expected``. numpy arrays match if their shapes, the kinds of
    their values (e.g. integers, see ``numpy.dtype.kind``) and their values do, with NaNs
    matching NaNs. pandas objects match by their ``equals``. Lists, tuples and dicts are
    compared item by item, so that they can hold arrays, too. Anything else matches if
    ``res == expected``.

    Raises ``IncomparableError`` if ``==`` gives neither ``True`` nor ``False``.
    """
    if _is_array(res) or _is_array(expected):
        return not _array_differences(res, expected)
    pandas_types = _pandas_types()
    if isinstance(res, pandas_types) or isinstance(expected, pandas_types):
        return isinstance(expected, pandas_types) and bool(expected.equals(res))
    if type(res) is type(expected) and type(res) in (list, tuple):
        return len(res) == len(expected) and all(
            r is e or equal(r, e) for r, e in zip(res, expected)
        )
    if type(res) is type(expected) is dict:
        return res.keys() == expected.keys() and all(
            res[k] is expected[k] or equal(res[k], expected[k]) for k in res
        )

    eq = res == expected
    try:
        return bool(eq)
    except Exception as error:  # e.g. element by element, by an unknown array type
        raise IncomparableError(
            f"{summary(res, expected)}\n\nCan't tell whether they are equal: == gave "
            f"{saferepr(eq, maxsize=_SUMMARY_SIZE)}, not True or False"
        ) from error


def _array_differences(res: Any, expected: Any) -> list[str]:
    """How ``res`` differs from ``expected``, one of which is a numpy array, as by
    ``equal``. Empty if they match."""
    np = sys.modules["numpy"]
    if not (isinstance(res, np.ndarray) and isinstance(expected, np.ndarray)):
        return [f"Types differ: {_type_name(res)} != {_type_name(expected)}"]
    if res.shape != expected.shape:
        return [f"Shapes differ: {res.shape} != {expected.shape}"]
    if res.dtype.kind != expected.dtype.kind:
        return [f"Types of values differ: {res.dtype} != {expected.dtype}"]

    if res.dtype.kind == "O":  # any objects, e.g. arrays themselves
        same = np.frompyfunc(equal, 2, 1)(res, expected)
    else:
        same = res == expected
        if res.dtype.kind in "fc":
            same |= np.isnan(res) & np.isnan(expected)
    # all positions differ if the values weren't compared one by one
    same = np.broadcast_to(np.asarray(same, dtype=bool), res.shape)
    if same.all():
        return []
    if res.ndim == 0:
        return [f"Values differ: {res.item()!r} != {expected.item()!r}"]

    count = int(np.count_nonzero(~same))
    lines = [f"Values differ at {count} of {same.size} positions:"]
    for position in np.argwhere(~same)[:_MAX_POSITIONS]:
        p = tuple(int(i) for i in position)
        index = p[0] if len(p) == 1 else p
        lines.append(f"At index {index} diff: {res.item(p)!r} != {expected.item(p)!r}")
    if count > _MAX_POSITIONS:
        lines.append(f"... and {count - _MAX_POSITIONS} more")
    return lines


def _pandas_differences(res: Any, expected: Any) -> list[str]:
    """How ``res`` differs from ``expected``, one of which is a pandas object, as told by
    ``pandas.testing``."""
    pd = sys.modules["pandas"]
    checks = {
        pd.DataFrame: pd.testing.assert_frame_equal,
        pd.Series: pd.testing.assert_series_equal,
        pd.Index: pd.testing.assert_index_equal,
    }
    for kind, check in checks.items():
        if isinstance(res, kind) and isinstance(expected, kind):
            break
    else:
        return [f"Types differ: {_type_name(res)} != {_type_name(expected)}"]
    try:
        check(res, expected, check_exact=True)
    except AssertionError as error:
        return str(error).strip().splitlines()
    return []


class _Whole:
    """An array or pandas object in a list, tuple or dict, which pytest then compares
    as a whole, by ``equal``, rather than element by element."""

    __hash__ = None  # type: ignore[assignment]

    def __init__(self, value: Any):
        self.value = value

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _Whole) and equal(self.value, other.value)

    def __repr__(self) -> str:
        return repr(self.value)


def _wrap(value: Any) -> Any:
    if _is_array(value) or isinstance(value, _pandas_types()):
        return _Whole(value)
    if type(value) in (list, tuple):
        return type(value)(_wrap(v) for v in value)
    if type(value) is dict:
        return {k: _wrap(v) for k, v in value.items()}
    return value


def explain(res: Any, expected: Any) -> list[str]:
    """Lines explaining how ``res`` differs from ``expected``: a summary, followed by
    the details, e.g. the positions at which arrays differ."""
    if _is_array(res) or _is_array(expected):
        details = _array_differences(res, expected)
    elif isinstance(res, _pandas_types()) or isinstance(expected, _pandas_types()):
        details = _pandas_differences(res, expected)
    else:
        lines = assertrepr_compare(
            "==",
            _wrap(res),
            _wrap(expected),
            verbose=1,
            highlighter=dummy_highlighter,
            assertion_text_diff_style="ndiff",
        )
        return list(lines) or [f"{res!r} == {expected!r}"]
    return [summary(res, expected), *(["", *details] if details else [])]
