import copy
from decimal import Decimal
from fractions import Fraction
from datetime import date, time, datetime, timedelta
from enum import Enum
from pathlib import Path

import json_tricks

_OPAQUE_TYPES = (
    str,
    bytes,
    int,
    float,
    bool,
    type(None),
    Decimal,
    Fraction,
    date,
    time,
    datetime,
    timedelta,
    Enum,
    Path,
    complex,
)


def _tag_tuples(obj):
    if isinstance(obj, tuple):
        return {"__tuple__": [_tag_tuples(v) for v in obj]}
    if isinstance(obj, (set, frozenset)):
        kind = "__pyics_frozenset__" if isinstance(obj, frozenset) else "__pyics_set__"
        try:
            items = sorted(obj)
        except TypeError:
            items = list(obj)
        return {kind: [_tag_tuples(v) for v in items]}
    if isinstance(obj, list):
        return [_tag_tuples(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _tag_tuples(v) for k, v in obj.items()}
    if isinstance(obj, _OPAQUE_TYPES):
        return obj
    if hasattr(obj, "__dict__"):
        new_obj = copy.copy(obj)
        object.__setattr__(
            new_obj, "__dict__", {k: _tag_tuples(v) for k, v in obj.__dict__.items()}
        )
        return new_obj
    return obj


def _untag_tuples(dct):
    if isinstance(dct, dict):
        if "__tuple__" in dct:
            return tuple(dct["__tuple__"])
        if "__pyics_set__" in dct:
            return set(dct["__pyics_set__"])
        if "__pyics_frozenset__" in dct:
            return frozenset(dct["__pyics_frozenset__"])
    return dct


def dumps(obj, **kw):
    """Wrapper around ``json_tricks.dumps`` to tag tuples early."""
    return json_tricks.dumps(_tag_tuples(obj), **kw)


def dump(obj, fp, **kw):
    """Wrapper around ``json_tricks.dump`` to tag tuples early."""
    return json_tricks.dump(_tag_tuples(obj), fp, **kw)


def loads(s, **kw):
    """Wrapper around ``json_tricks.loads`` to automatically add the _untag_tuples hook."""
    extra_hooks = (_untag_tuples,) + kw.get("extra_obj_pairs_hooks", tuple())
    return json_tricks.loads(s, extra_obj_pairs_hooks=extra_hooks, **kw)


def load(fp, **kw):
    """Wrapper around ``json_tricks.load`` to automatically add the _untag_tuples hook."""
    extra_hooks = (_untag_tuples,) + kw.get("extra_obj_pairs_hooks", tuple())
    return json_tricks.load(fp, extra_obj_pairs_hooks=extra_hooks, **kw)
