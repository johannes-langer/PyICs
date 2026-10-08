import copy
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from datetime import date, time, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any

import json_tricks
from json_tricks.decoders import pathlib_hook

from pyics.util.paths import StrPath

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
    if isinstance(obj, Path):
        # json_tricks would store str(obj), i.e. backslashes on Windows. Forward slashes
        # are read correctly on all platforms.
        return {"__pathlib__": obj.as_posix()}
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


def dumps(obj: Any, **kw: Any) -> str:
    """Wrapper around ``json_tricks.dumps`` to tag tuples early. Compression isn't
    supported, since its result is bytes."""
    txt = json_tricks.dumps(_tag_tuples(obj), **kw)
    if not isinstance(txt, str):
        raise TypeError("compressed output isn't supported, use json_tricks directly")
    return txt


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


def _plain_hook(dct: Any) -> Any:
    """Decode the tags of paths, tuples and sets, if they are all a dict holds, so that
    nothing that is decoded hides other data from ``scan_tags``."""
    if not isinstance(dct, dict) or len(dct) != 1:
        return dct
    key, value = next(iter(dct.items()))
    if key == "__pathlib__" and isinstance(value, str):
        return pathlib_hook(dct)
    if key in ("__tuple__", "__pyics_set__", "__pyics_frozenset__"):
        if isinstance(value, list):
            try:
                return _untag_tuples(dct)
            except TypeError:  # unhashable items of a set
                pass
    return dct


def loads_plain(s: str) -> Any:
    """
    Decode ``s`` as plain data: dicts, lists, strings and numbers, paths, and tuples and
    sets as tagged by ``dumps``. Nothing else is decoded, e.g. class instances stay the
    dicts json_tricks tagged them as, so that decoding never imports a module or
    creates an object. For files of plain data, like course manifests, and to inspect a
    file before it is decoded (see ``scan_tags``).
    """
    return json_tricks.loads(s, obj_pairs_hooks=(_plain_hook,))


def load_plain(fp: StrPath) -> Any:
    """``loads_plain`` the file at ``fp``."""
    with open(fp, encoding="utf-8") as f:
        return loads_plain(f.read())


@dataclass(frozen=True)
class Tags:
    """What decoding data tagged by json_tricks needs (see ``scan_tags``)."""

    # the modules that json_tricks imports to decode the classes and enums named by the
    # tags, None for one defined in a script (i.e. in ``__main__``)
    modules: frozenset[str | None]
    # the packages that json_tricks needs for its own tags: numpy for arrays, pandas
    # for data frames and series, pytz for (date)times with a time zone
    packages: frozenset[str]


def scan_tags(data: Any) -> Tags:
    """
    Find what decoding ``data``, as read by ``loads_plain``, needs besides the standard
    library: the modules of the classes and enums whose instances it holds, and the
    packages json_tricks needs for some of its tags.
    """
    modules: set[str | None] = set()
    packages: set[str] = set()

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            if "__instance_type__" in obj:
                modules.add(_tagged_module(obj["__instance_type__"]))
            enum = obj.get("__enum__")
            if isinstance(enum, dict) and "__enum_instance_type__" in enum:
                modules.add(_tagged_module(enum["__enum_instance_type__"]))
            if "__ndarray__" in obj:
                packages.add("numpy")
            if "__pandas_dataframe__" in obj or "__pandas_series__" in obj:
                packages.update(("pandas", "numpy"))
            if ("__datetime__" in obj or "__time__" in obj) and "tzinfo" in obj:
                packages.add("pytz")
            for value in obj.values():
                walk(value)
        elif isinstance(obj, (list, tuple, set, frozenset)):
            for value in obj:
                walk(value)

    walk(data)
    return Tags(frozenset(modules), frozenset(packages))


def _tagged_module(instance_type: Any) -> str | None:
    """The module of a class tagged as ``[module, name]``."""
    if isinstance(instance_type, (list, tuple)) and instance_type:
        module = instance_type[0]
        return module if isinstance(module, str) and module else None
    return None
