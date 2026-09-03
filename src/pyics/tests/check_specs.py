import inspect
import typing
from typing import Any, Callable, Type, Union

SpecType = Union[Callable, Type]


def compare_spec(
    expected: SpecType,
    other: Any,
    *,
    use_rich_markup: bool = True,
    flag_type_hints: bool = True,
    exact: bool = False,
) -> list[str]:
    """
    Compare ``other`` against an ``expected`` specification.

    Parameters
    ----------
    expected: SpecType
        The expected specification, which is either a Type, or a Signature
    other: Any
        The symbol to compare against ``expected``
    use_rich_markup: bool (optional, keyword only)
        Use ``rich`` markup in diff strings for additional highlighting.
        Default True.
    flag_type_hints: bool (optional, keyword only)
        Default True. If set to false, do not flag missing type hints.
    exact: bool (optional, keyword only)
        Require an exact, i.e. two-way match (no extra attributes/methods on the other
        side)

    Returns
    -------
    list[str]
        List of differences between ``expected`` and ``other``. Empty if match.

    Raises
    ------
    TypeError
        if ``expected`` is of an invalid type
    """
    if inspect.isclass(expected):
        if not inspect.isclass(other):
            ret_str = (
                f"Expected [b]class[/], got [b]{type(other)}[/]"
                if use_rich_markup
                else f"Expected class, got {type(other)}"
            )
            return [ret_str]
        return compare_class_spec(
            expected,
            other,
            use_rich_markup=use_rich_markup,
            flag_type_hints=flag_type_hints,
            exact=exact,
        )

    elif inspect.isfunction(expected) or inspect.ismethod(expected):
        if not (inspect.isfunction(other) or inspect.ismethod(other)):
            ret_str = (
                f"Expected a [b]function/callable[/], got [b]{type(other)}[/]"
                if use_rich_markup
                else f"Expected a function/callable, got {type(other)}"
            )
            return [ret_str]
        return compare_func_spec(
            expected,
            other,
            use_rich_markup=use_rich_markup,
            flag_type_hints=flag_type_hints,
            exact=exact,
        )

    else:
        raise TypeError("Expected specification must be a class, Protocol, or function.")


def compare_class_spec(
    expected: Type,
    other: Type,
    *,
    use_rich_markup: bool = True,
    flag_type_hints: bool = True,
    exact: bool = False,
) -> list[str]:
    """
    Compares an ``expected`` class against an ``other`` class.

    Parameters
    ----------
    expected: Type
        The expected specification, which is either a class or a Protocol
    other: Type
        The symbol to compare against ``expected``, which should be a class
    use_rich_markup: bool (optional, keyword only)
        Use ``rich`` markup in diff strings for additional highlighting.
        Default True.
    flag_type_hints: bool (optional, keyword only)
        Default True. If set to false, do not flag missing type hints.
    exact: bool (optional, keyword only)
        Require an exact, i.e. two-way match (no extra attributes/methods on the other
        side)

    Returns
    -------
    list[str]
        List of differences between ``expected`` and ``other``. Empty if match.
    """
    diffs = []

    # non-callable attributes
    try:
        # attributes only
        diffs.extend([
            (
                f"Missing attribute: [b]'{attr}'[/]."
                if use_rich_markup
                else f"Missing attribute: '{attr}'."
            )
            for attr in expected.__static_attributes__
            if attr not in other.__static_attributes__
        ])
        if exact:
            diffs.extend([
                (
                    f"Additional attribute: [b]'{attr}'[/]."
                    if use_rich_markup
                    else f"Additional attribute: '{attr}'."
                )
                for attr in other.__static_attributes__
                if attr not in expected.__static_attributes__
            ])

        # type hints (intentionally does not make use of exact, additional type hints
        # should not be punished)
        if flag_type_hints:
            exp_hints = typing.get_type_hints(expected)
            other_hints = typing.get_type_hints(other)

            for name, hint in exp_hints.items():
                if (
                    name not in other_hints and name in other.__static_attributes__
                ):  # TODO: Might cause issues with name-mangled attributes
                    diffs.append(
                        f"Missing type hint for attribute [b]'{name}'[/]."
                        if use_rich_markup
                        else f"Missing type hint for attribute '{name}'."
                    )
                elif other_hints[name] != hint:
                    diffs.append(
                        f"Attribute [b]'{name}'[/] type mismatch. Expected: [b]{hint}[/], Got: [b]{other_hints[name]}[/]."
                        if use_rich_markup
                        else f"Attribute '{name}' type mismatch. Expected: {hint}, Got: {other_hints[name]}."
                    )
    except Exception as e:
        # Should only be casue by typing.get_type_hints, which are only called if
        # flag_type_hints is true
        diffs.append(f"Error resolving type hints: {e}")

    # check __dict__
    for name, expected_attr in expected.__dict__.items():
        # Ignore standard dunder methods unless explicitly overridden in the spec
        if name.startswith("__") and name not in (
            "__init__",
            "__call__",
            "__str__",
            "__repr__",
        ):
            continue

        # check functions/methods
        if inspect.isfunction(expected_attr) or inspect.ismethod(expected_attr):
            if name not in other.__dict__:
                diffs.append(
                    f"Missing method: [b]'{name}'[/]."
                    if use_rich_markup
                    else f"Missing method: '{name}'."
                )
            else:
                student_attr = other.__dict__[name]
                if not (inspect.isfunction(student_attr) or inspect.ismethod(student_attr)):
                    diffs.append(
                        f"Attribute [b]'{name}'[/] should be a [b]method[/], but found [b]{type(student_attr)}[/]."
                        if use_rich_markup
                        else f"Attribute '{name}' should be a method, but found {type(student_attr)}."
                    )
                    continue

                # Recursively check the method signature
                func_diffs = compare_func_spec(
                    expected_attr,
                    student_attr,
                    use_rich_markup=use_rich_markup,
                    flag_type_hints=flag_type_hints,
                    exact=exact,
                )
                for d in func_diffs:
                    diffs.append(
                        f"Method [b]'{name}'[/]: {d}"
                        if use_rich_markup
                        else f"Method '{name}': {d}"
                    )

        # check properties
        if isinstance(expected_attr, property):
            if not isinstance(getattr(other, name), property):
                diffs.append(
                    f"[b]'{name}'[/] should be a property, but got [b]{type(getattr(other, name))}[/]."
                    if use_rich_markup
                    else f"'{name}' should be a property, but got {type(getattr(other, name))}."
                )

            for func, clql in zip(
                ["fget", "fset", "fdel"], ["getter", "setter", "deleter"]
            ):
                if (
                    getattr(expected_attr, func) is None
                    and getattr(getattr(other, name), func) is not None
                ):
                    diffs.append(
                        f"[b]'{name}'[/] should not have a [b]{clql}[/]."
                        if use_rich_markup
                        else f"'{name}' should not have a {clql}."
                    )
                elif getattr(getattr(other, name), func) is None:
                    diffs.append(
                        f"[b]'{name}'[/] should have a [b]{clql}[/]."
                        if use_rich_markup
                        else f"'{name}' should have a {clql}."
                    )
                else:
                    func_diffs = compare_func_spec(
                        getattr(expected_attr, func),
                        getattr(getattr(other, name), func),
                        use_rich_markup=use_rich_markup,
                        flag_type_hints=flag_type_hints,
                        exact=exact,
                    )
                    for d in func_diffs:
                        diffs.append(
                            f"Method [b]'{name}.{clql}'[/]: {d}"
                            if use_rich_markup
                            else f"Method '{name}.{clql}': {d}"
                        )

    return diffs


def compare_func_spec(
    expected: Callable,
    other: Callable,
    *,
    use_rich_markup: bool = True,
    flag_type_hints: bool = True,
    exact: bool = False,
) -> list[str]:
    """
    Compares an ``expected`` callable against an ``other`` callable.

    Parameters
    ----------
    expected: Callable
        The expected specification, which is a Callable. ``expected`` does not need to
        be implemented
    other: Callable
        The symbol to compare against ``expected``, which should be a Callable
    use_rich_markup: bool (optional, keyword only)
        Use ``rich`` markup in diff strings for additional highlighting.
        Default True.
    flag_type_hints: bool (optional, keyword only)
        Default True. If set to false, do not flag missing type hints.
    exact: bool (optional, keyword only)
        Require an exact, i.e. two-way match (no extra attributes/methods on the other
        side)

    Returns
    -------
    list[str]
        List of differences between ``expected`` and ``other``. Empty if match.
    """
    diffs = []

    exp_sig = inspect.signature(expected)  # we want this to crash
    try:
        other_sig = inspect.signature(other)
    except ValueError:
        return ["No valid callable found."]

    # exp_params = list(exp_sig.parameters.values())
    # other_params = list(other_sig.parameters.values())

    if exact:
        for name, other_param in other_sig.parameters.items():
            if name not in exp_sig.parameters:
                diffs.append(
                    f"Additional parameter: [b]'{name}'[/]."
                    if use_rich_markup
                    else f"Additional parameter: '{name}'."
                )

    for name, exp_param in exp_sig.parameters.items():
        if name not in other_sig.parameters:
            diffs.append(
                f"Missing parameter: [b]'{name}'[/]."
                if use_rich_markup
                else f"Missing parameter: '{name}'."
            )
            continue

        other_param = other_sig.parameters[name]

        if flag_type_hints and exp_param.annotation != inspect.Parameter.empty:
            if other_param.annotation == inspect.Parameter.empty:
                diffs.append(
                    f"Parameter [b]'{name}'[/] is missing its type hint."
                    if use_rich_markup
                    else f"Parameter '{name}' is missing its type hint."
                )
            elif exp_param.annotation != other_param.annotation:
                diffs.append(
                    f"Parameter [b]'{name}'[/] type mistmatch. Expected [b]{exp_param.annotation}[/], got [b]{other_param.annotation}[/]."
                    if use_rich_markup
                    else f"Parameter '{name}' type mistmatch. Expected {exp_param.annotation}, got {other_param.annotation}."
                )

        # default values
        if (exp_param.default != inspect.Parameter.empty) or exact:
            if exp_param.default != other_param.default:
                diffs.append(
                    f"Parameter [b]'{name}'[/] default value mismatch. Expected [b]'{name}'[/]."
                    if use_rich_markup
                    else f"Parameter '{name}' default value mismatch. Expected '{name}'."
                )

        # param kind
        if exp_param.kind != other_param.kind:
            diffs.append(
                f"Parameter [b]'{name}'[/] kind mismatch. Expected [b]{exp_param.kind}[/]'."
                if use_rich_markup
                else f"Parameter '{name}' kind mismatch. Expected {exp_param.kind}'."
            )

    # return type
    if flag_type_hints and exp_sig.return_annotation != inspect.Signature.empty:
        if other_sig.return_annotation == inspect.Signature.empty:
            diffs.append("Missing return type hint.")
        elif exp_sig.return_annotation != other_sig.return_annotation:
            diffs.append(
                f"Return type mismatch. Expected [b]{exp_sig.return_annotation}[/], got [b]{other_sig.return_annotation}[/]."
                if use_rich_markup
                else f"Return type mismatch. Expected {exp_sig.return_annotation}, got {other_sig.return_annotation}."
            )

    return diffs
