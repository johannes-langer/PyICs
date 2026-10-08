"""
Compare symbols against a specification.

Specifications are plain dicts, so that they can be stored as json. Classes and callables
are accepted as well, and converted using ``to_spec_dict``.

Spec dict format
----------------
A function spec maps parameter names to parameter specs, plus a ``return`` type::

    {"is_class": False, "x": "int", "flag": None, "return": "bool"}

A parameter spec is either a type string, ``None`` (no type hint required), or a dict with
the (optional) keys ``type``, ``default`` and ``kind``::

    {"type": "int", "default": 0, "kind": "KEYWORD_ONLY"}

A missing ``kind`` means ``POSITIONAL_OR_KEYWORD``, a missing ``default`` means that no
default value is expected. ``self``/``cls`` are never listed.

A class spec maps attribute names to type strings (or ``None``), and method, property and
nested class names to nested specs::

    {
        "is_class": True,
        "name": "str",
        "__init__": {"is_class": False, "name": "str", "return": "None"},
        "size": {
            "is_class": False,
            "is_property": True,
            "return": "int",
            "setter": False,
            "deleter": False,
        },
    }

Class specs include inherited attributes, methods, properties and nested classes. A class
without an ``__init__`` is compared as if it had ``def __init__(self) -> None``.

``is_class`` and ``return`` are reserved keys, so they can not be used as parameter or
attribute names.
"""

import annotationlib
import builtins
import inspect
import re
import sys
import typing
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from rich.markup import escape

SpecDict = dict[str, Any]
SpecType = Callable | type | SpecDict

_POK = inspect.Parameter.POSITIONAL_OR_KEYWORD
_KINDS = {kind.name: kind for kind in type(_POK)}
_PARAM_KEYS = {"type", "default", "kind"}
_PROPERTY_KEYS = {"is_class", "is_property", "return", "setter", "deleter"}
# dunder methods which are part of a class spec, all others are ignored
_CHECKED_DUNDERS = ("__init__", "__call__", "__str__", "__repr__")
# module prefixes stripped from type strings, in addition to the symbol's own module
_STRIPPED_MODULES = ("typing", "collections.abc", "builtins")
_NO_DEFAULT = object()
# compared against if a class defines no ``__init__``, i.e. inherits ``object.__init__``
_IMPLICIT_INIT: SpecDict = {"is_class": False, "return": "None"}


def compare_spec(
    expected: SpecType,
    other: Any,
    *,
    by_type: bool = False,
    use_rich_markup: bool = True,
    flag_type_hints: bool = True,
    exact: bool = False,
) -> list[str]:
    """
    Compare ``other`` against an ``expected`` specification.

    Parameters
    ----------
    expected: SpecType
        The expected specification: a spec dict (see module docstring), a class, or a
        function
    other: Any
        The symbol to compare against ``expected``
    by_type: bool (optional, keyword only)
        Default False. If set, match parameters and attributes purely by their type,
        ignoring their names and order. Methods, properties and nested classes are still
        matched by name, as they define the class' public API.
    use_rich_markup: bool (optional, keyword only)
        Use ``rich`` markup in diff strings for additional highlighting.
        Default True.
    flag_type_hints: bool (optional, keyword only)
        Default True. If set to false, do not flag missing or mismatching type hints.
        Ignored if ``by_type`` is set.
    exact: bool (optional, keyword only)
        Require an exact, i.e. two-way match (no extra parameters/attributes/methods on
        the other side, no default values on the other side if none are expected)

    Returns
    -------
    list[str]
        List of differences between ``expected`` and ``other``. Empty if match.

    Raises
    ------
    TypeError
        if ``expected`` is of an invalid type
    ValueError
        if ``expected`` is a malformed spec dict
    """
    exp = to_spec_dict(expected)
    module = getattr(other, "__module__", None)
    opts = _Options(
        by_type=by_type,
        rich=use_rich_markup,
        check_types=by_type or flag_type_hints,
        exact=exact,
        module=module,
        namespace=vars(sys.modules[module]) if module in sys.modules else {},
    )

    if exp["is_class"]:
        if not inspect.isclass(other):
            return [f"Expected {opts.b('class')}, got {opts.b(type(other))}"]
    elif not (inspect.isfunction(other) or inspect.ismethod(other)):
        return [f"Expected a {opts.b('function/callable')}, got {opts.b(type(other))}"]

    try:
        oth = to_spec_dict(other)
    except ValueError:
        return ["No valid callable found."]

    return _compare(exp, oth, opts)


def to_spec_dict(obj: SpecType) -> SpecDict:
    """
    Convert a class or function into a spec dict (see module docstring). Spec dicts are
    validated and returned as-is.

    Raises
    ------
    TypeError
        if ``obj`` is neither a class, a function, nor a dict
    ValueError
        if ``obj`` is a malformed spec dict
    """
    if isinstance(obj, dict):
        _validate(obj, "spec")
        return obj
    if inspect.isclass(obj):
        return _class_to_dict(obj)
    if inspect.isfunction(obj) or inspect.ismethod(obj):
        return _func_to_dict(obj, obj.__module__)
    raise TypeError("Expected specification must be a class, function, or spec dict.")


# conversion ###########################################################################


def _format_type(annotation: Any, module: str | None) -> str | None:
    """format a type annotation as a string, without module prefixes"""
    if annotation is inspect.Parameter.empty:
        return None
    if isinstance(annotation, annotationlib.ForwardRef):
        annotation = annotation.__forward_arg__
    if isinstance(annotation, str):
        return " ".join(annotation.split())

    text = inspect.formatannotation(annotation)
    for mod in (*_STRIPPED_MODULES, module):
        if mod:
            text = re.sub(rf"(?<![\w.]){re.escape(mod)}\.", "", text)
    return text


def _func_to_dict(func: Callable, module: str | None, *, drop_first=False) -> SpecDict:
    sig = inspect.signature(func, annotation_format=annotationlib.Format.FORWARDREF)
    params = list(sig.parameters.values())[1 if drop_first else 0 :]

    spec: SpecDict = {"is_class": False}
    for p in params:
        type_str = _format_type(p.annotation, module)
        if p.kind is _POK and p.default is p.empty:
            spec[p.name] = type_str
            continue

        param: dict[str, Any] = {"type": type_str}
        if p.kind is not _POK:
            param["kind"] = p.kind.name
        if p.default is not p.empty:
            param["default"] = p.default
        spec[p.name] = param

    spec["return"] = _format_type(sig.return_annotation, module)
    return spec


def _property_to_dict(prop: property, module: str | None) -> SpecDict:
    getter = _func_to_dict(prop.fget, module, drop_first=True) if prop.fget else {}
    return {
        "is_class": False,
        "is_property": True,
        "return": getter.get("return"),
        "setter": prop.fset is not None,
        "deleter": prop.fdel is not None,
    }


def _class_to_dict(cls: type) -> SpecDict:
    spec: SpecDict = {"is_class": True}
    # the class itself and its bases, most derived first. ``object`` contributes no
    # Python functions, its dunders would only be noise
    bases = [klass for klass in cls.__mro__ if klass is not object]

    # attributes: annotated ones and those assigned in methods, incl. inherited ones
    hints = typing.get_type_hints(cls, format=annotationlib.Format.FORWARDREF)
    static = [
        a for k in reversed(bases) for a in getattr(k, "__static_attributes__", ())
    ]
    for name in dict.fromkeys([*hints, *static]):
        spec[name] = _format_type(
            hints.get(name, inspect.Parameter.empty), cls.__module__
        )

    # methods, properties and nested classes, incl. inherited ones. The most derived
    # definition of a name wins, even if it is no method (e.g. ``method = None``)
    seen: set[str] = set()
    for klass in bases:
        module = klass.__module__
        for name, member in klass.__dict__.items():
            if name in seen or (name.startswith("__") and name not in _CHECKED_DUNDERS):
                continue
            seen.add(name)

            if isinstance(member, staticmethod):
                spec[name] = _func_to_dict(member.__func__, module)
            elif isinstance(member, classmethod) or inspect.isfunction(member):
                func = getattr(member, "__func__", member)
                spec[name] = _func_to_dict(func, module, drop_first=True)
            elif isinstance(member, property):
                spec[name] = _property_to_dict(member, module)
            elif (
                inspect.isclass(member)
                and member.__qualname__ == f"{klass.__qualname__}.{name}"
            ):
                spec[name] = _class_to_dict(member)

    return spec


# validation ###########################################################################


def _param(value: Any) -> dict[str, Any]:
    """normalize a parameter spec to a dict with a ``type``, ``kind`` and optionally a
    ``default`` key"""
    if not isinstance(value, dict):
        return {"type": value, "kind": _POK.name}
    param = {"type": value.get("type"), "kind": value.get("kind", _POK.name).upper()}
    if "default" in value:
        param["default"] = value["default"]
    return param


def _validate(spec: SpecDict, path: str) -> None:
    if not isinstance(spec.get("is_class"), bool):
        raise ValueError(f"{path}: must have a boolean 'is_class' key.")  # noqa: TRY004

    if spec.get("is_property"):
        if unknown := spec.keys() - _PROPERTY_KEYS:
            raise ValueError(f"{path}: unknown property keys {sorted(unknown)}.")
        return

    for name, value in spec.items():
        if name in ("is_class", "return"):
            if name == "return" and spec["is_class"]:
                raise ValueError(f"{path}: 'return' is reserved for function specs.")
            continue

        sub_path = f"{path}.{name}"
        if spec["is_class"] and isinstance(value, dict):
            _validate(value, sub_path)
        elif isinstance(value, dict):
            if unknown := value.keys() - _PARAM_KEYS:
                raise ValueError(
                    f"{sub_path}: unknown parameter keys {sorted(unknown)}."
                )
            kind = value.get("kind", _POK.name)
            if not isinstance(kind, str) or kind.upper() not in _KINDS:
                raise ValueError(f"{sub_path}: kind must be one of {list(_KINDS)}.")
            if not isinstance(value.get("type"), str | None):
                raise ValueError(f"{sub_path}: type must be a string or None.")
        elif not isinstance(value, str | None):
            raise ValueError(f"{sub_path}: must be a type string, None, or a dict.")


# comparison ###########################################################################


@dataclass
class _Options:
    by_type: bool
    rich: bool
    check_types: bool
    exact: bool
    module: str | None
    namespace: dict[str, Any]
    _types: dict[str, str] = field(default_factory=dict)

    def b(self, text: Any) -> str:
        """bold ``text``, if rich markup is used"""
        return f"[b]{escape(str(text))}[/]" if self.rich else str(text)

    def q(self, name: str) -> str:
        """quote and bold ``name``"""
        return self.b(f"'{name}'")

    def type(self, type_str: str | None) -> str | None:
        """normalize a type string by evaluating it in the compared symbol's module, so
        e.g. ``Optional[int]`` and ``int | None`` compare equal"""
        if type_str is None:
            return None
        if type_str not in self._types:
            try:
                globals_ = {"__builtins__": builtins, **vars(typing), **self.namespace}
                normalized = _format_type(eval(type_str, globals_), self.module)
            except Exception:  # noqa: BLE001 - not a valid expression, compare as-is
                normalized = type_str
            self._types[type_str] = normalized or type_str
        return self._types[type_str]


def _describe(spec: SpecDict) -> str:
    if spec.get("is_property"):
        return "property"
    return "class" if spec["is_class"] else "method"


def _compare(exp: SpecDict, oth: SpecDict, o: _Options) -> list[str]:
    if exp.get("is_property"):
        return _compare_property(exp, oth, o)
    if exp["is_class"]:
        return _compare_class(exp, oth, o)
    return _compare_func(exp, oth, o)


def _compare_type(label: str, exp_t: str | None, oth_t: str | None, o: _Options):
    if not o.check_types or exp_t is None:
        return []
    exp_t, oth_t = o.type(exp_t), o.type(oth_t)
    if oth_t is None:
        return [f"{label} is missing its type hint."]
    if exp_t != oth_t:
        return [f"{label} type mismatch. Expected {o.b(exp_t)}, got {o.b(oth_t)}."]
    return []


def _match_by_type(
    what: str, exp_types: list[str | None], oth_types: list[str | None], o: _Options
) -> list[str]:
    """match two lists of types as multisets. ``None`` in ``exp_types`` matches any type,
    after all typed entries have been matched"""
    diffs = []
    unmatched = [o.type(t) for t in oth_types]

    for t in (o.type(t) for t in exp_types if t is not None):
        if t in unmatched:
            unmatched.remove(t)
        else:
            diffs.append(f"Missing {what} of type {o.b(t)}.")

    for _ in (t for t in exp_types if t is None):
        if unmatched:
            unmatched.pop(0)
        else:
            diffs.append(f"Missing {what}.")

    if o.exact:
        diffs.extend(
            f"Additional {what} of type {o.b(t)}." if t else f"Additional {what}."
            for t in unmatched
        )
    return diffs


def _compare_func(exp: SpecDict, oth: SpecDict, o: _Options) -> list[str]:
    exp_params = {
        n: _param(v) for n, v in exp.items() if n not in ("is_class", "return")
    }
    oth_params = {
        n: _param(v) for n, v in oth.items() if n not in ("is_class", "return")
    }

    if o.by_type:
        diffs = _match_by_type(
            "parameter",
            [p["type"] for p in exp_params.values()],
            [p["type"] for p in oth_params.values()],
            o,
        )
    else:
        diffs = _compare_params_by_name(exp_params, oth_params, o)

    diffs.extend(_compare_type("Return value", exp.get("return"), oth.get("return"), o))
    return diffs


def _compare_params_by_name(
    exp_params: dict[str, dict], oth_params: dict[str, dict], o: _Options
) -> list[str]:
    diffs = []
    for name, exp_p in exp_params.items():
        if name not in oth_params:
            diffs.append(f"Missing parameter: {o.q(name)}.")
            continue
        oth_p = oth_params[name]

        diffs.extend(
            _compare_type(f"Parameter {o.q(name)}", exp_p["type"], oth_p["type"], o)
        )

        exp_default = exp_p.get("default", _NO_DEFAULT)
        oth_default = oth_p.get("default", _NO_DEFAULT)
        if exp_default is _NO_DEFAULT and oth_default is not _NO_DEFAULT and o.exact:
            diffs.append(f"Parameter {o.q(name)} should not have a default value.")
        elif exp_default is not _NO_DEFAULT and oth_default is _NO_DEFAULT:
            diffs.append(
                f"Parameter {o.q(name)} is missing its default value "
                f"{o.b(repr(exp_default))}."
            )
        elif exp_default is not _NO_DEFAULT and exp_default != oth_default:
            diffs.append(
                f"Parameter {o.q(name)} default value mismatch. Expected "
                f"{o.b(repr(exp_default))}, got {o.b(repr(oth_default))}."
            )

        if exp_p["kind"] != oth_p["kind"]:
            diffs.append(
                f"Parameter {o.q(name)} kind mismatch. Expected "
                f"{o.b(_KINDS[exp_p['kind']].description)}, got "
                f"{o.b(_KINDS[oth_p['kind']].description)}."
            )

    if o.exact:
        diffs.extend(
            f"Additional parameter: {o.q(name)}."
            for name in oth_params
            if name not in exp_params
        )
    return diffs


def _compare_property(exp: SpecDict, oth: SpecDict, o: _Options) -> list[str]:
    diffs = _compare_type(
        "Getter return value", exp.get("return"), oth.get("return"), o
    )
    for key in ("setter", "deleter"):
        if exp.get(key) and not oth.get(key):
            diffs.append(f"Missing {key}.")
        elif oth.get(key) and not exp.get(key):
            diffs.append(f"Should not have a {key}.")
    return diffs


def _compare_class(exp: SpecDict, oth: SpecDict, o: _Options) -> list[str]:
    def split(spec: SpecDict) -> tuple[dict, dict]:
        items = [(k, v) for k, v in spec.items() if k != "is_class"]
        return (
            {k: v for k, v in items if not isinstance(v, dict)},
            {k: v for k, v in items if isinstance(v, dict)},
        )

    exp_attrs, exp_members = split(exp)
    oth_attrs, oth_members = split(oth)
    diffs = []

    # attributes
    if o.by_type:
        diffs.extend(
            _match_by_type(
                "attribute", list(exp_attrs.values()), list(oth_attrs.values()), o
            )
        )
    else:
        for name, exp_t in exp_attrs.items():
            if name in oth_members:
                diffs.append(
                    f"{o.q(name)} should be an {o.b('attribute')}, but found a "
                    f"{o.b(_describe(oth_members[name]))}."
                )
            elif name not in oth_attrs:
                diffs.append(f"Missing attribute: {o.q(name)}.")
            else:
                diffs.extend(
                    _compare_type(f"Attribute {o.q(name)}", exp_t, oth_attrs[name], o)
                )
        if o.exact:
            diffs.extend(
                f"Additional attribute: {o.q(name)}."
                for name in oth_attrs
                if name not in exp_attrs and name not in exp_members
            )

    # methods, properties and nested classes, always matched by name
    for name, exp_m in exp_members.items():
        what = _describe(exp_m)
        if name == "__init__" and name not in oth_members and name not in oth_attrs:
            oth_members = {**oth_members, name: _IMPLICIT_INIT}
        if name in oth_members and _describe(oth_members[name]) == what:
            diffs.extend(
                f"{what.capitalize()} {o.q(name)}: {d}"
                for d in _compare(exp_m, oth_members[name], o)
            )
        elif name in oth_members:
            diffs.append(
                f"{o.q(name)} should be a {o.b(what)}, but found a "
                f"{o.b(_describe(oth_members[name]))}."
            )
        elif name in oth_attrs and not o.by_type:
            diffs.append(
                f"{o.q(name)} should be a {o.b(what)}, but found an {o.b('attribute')}."
            )
        else:
            diffs.append(f"Missing {what}: {o.q(name)}.")

    if o.exact:
        diffs.extend(
            f"Additional {_describe(oth_m)}: {o.q(name)}."
            for name, oth_m in oth_members.items()
            if name not in exp_members and (o.by_type or name not in exp_attrs)
        )
    return diffs
