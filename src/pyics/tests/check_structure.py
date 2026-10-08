"""
Check the structure of source code against rules, e.g. whether it uses recursion, avoids
loops, or calls a certain function.

The source is parsed with ``ast``, but never executed. Rules are plain dicts, so that they
can be stored as json.

Rule format
-----------
A rule has exactly one of the following keys:

``concept``
    A name from ``CONCEPTS``, e.g. ``"loop"``, ``"recursion"`` or ``"match"``. Some
    concepts take parameters, which are given as additional keys::

        {"concept": "call", "name": "sorted"}

``node``
    The name of an ``ast`` node class, or a list of them. Abstract classes such as
    ``"stmt"`` match all of their subclasses::

        {"node": ["ListComp", "GeneratorExp"]}

``pattern``
    A single Python statement or expression, which is matched structurally. ``_`` matches
    any expression, statement or identifier, and ``...`` any number of arguments,
    elements or statements. Optional parts which the pattern leaves out (``else``
    branches, decorators, keyword arguments, annotations, parameters, ...) match
    anything::

        {"pattern": "for _ in range(len(_)): ..."}

A rule finds a number of nodes in the source, and holds if that number is at least
``min`` and at most ``max``. By default, a rule requires at least one node, or at most
``max`` nodes if only ``max`` is given, so ``"max": 0`` forbids a construct. ``scope`` restricts the search to the function or class with that dotted name
(e.g. ``"Graph.search"``), including everything nested in it. ``message`` replaces the
generated failure message::

    {"concept": "loop", "scope": "fib", "max": 0, "message": "Use recursion instead."}

Rules can be combined with ``{"all": [...]}``, ``{"any": [...]}`` and ``{"not": rule}``.
These accept a ``message`` as well, and a ``scope``, which applies to all contained rules
without a ``scope`` of their own.

Recursion is detected from the calls between the functions and methods defined in the
source. Method calls are resolved by name within the class, so ``child.size()`` inside
``Tree.size`` counts as recursion, just like ``self.size()``. Aliases (``f = fib``),
functions passed as arguments and recursive calls which are never executed are not taken
into account.
"""

import ast
import textwrap
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from functools import cache, cached_property
from typing import Any

from rich.markup import escape

Rule = dict[str, Any]

_LEAF_KEYS = ("concept", "node", "pattern")
_COMBINATOR_KEYS = ("all", "any", "not")
_MODIFIER_KEYS = {"scope", "min", "max", "message"}

_DEF_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
_LOOP_TYPES = (ast.For, ast.AsyncFor, ast.While)

# fields which are not compared when matching patterns
_IGNORED_FIELDS = {"ctx", "type_comment"}
# fields which match anything if they are left out (None or empty) in a pattern
_OPTIONAL_FIELDS = {
    "annotation",
    "asname",
    "bases",
    "cause",
    "decorator_list",
    "defaults",
    "finalbody",
    "format_spec",
    "guard",
    "handlers",
    "keywords",
    "kw_defaults",
    "kwarg",
    "kwonlyargs",
    "msg",
    "name",
    "optional_vars",
    "orelse",
    "posonlyargs",
    "returns",
    "type_params",
    "vararg",
}


class _MissingScope(Exception):
    def __init__(self, scope: str):
        self.scope = scope


def check_structure(
    rules: list[Rule] | Rule,
    source: str,
    *,
    filename: str = "",
    use_rich_markup: bool = True,
) -> list[str]:
    """
    Check ``source`` against ``rules``.

    Parameters
    ----------
    rules: list[Rule] | Rule
        A rule dict or a list of rule dicts (see module docstring), which must all hold
    source: str
        The source code to check
    filename: str (optional, keyword only)
        Name of the source file, used in the message if it can not be parsed
    use_rich_markup: bool (optional, keyword only)
        Use ``rich`` markup in messages for additional highlighting.
        Default True.

    Returns
    -------
    list[str]
        Messages for all rules which do not hold. Empty if all rules hold.

    Raises
    ------
    TypeError, ValueError
        if ``rules`` are malformed
    """
    rules = validate_rules(rules)
    o = _Markup(use_rich_markup)
    try:
        tree = ast.parse(source, filename or "<unknown>")
    except SyntaxError as err:
        what = o.q(filename) if filename else "the source"
        return [f"Could not parse {what}: {err.msg} (line {err.lineno})."]

    ctx = _Context(tree, o)
    messages = []
    for rule in rules:
        try:
            messages.extend(_evaluate(rule, ctx, None))
        except _MissingScope as err:
            messages.append(f"No function or class {o.q(err.scope)} found.")
    return list(dict.fromkeys(messages))


def validate_rules(rules: list[Rule] | Rule) -> list[Rule]:
    """
    Validate a rule dict or a list of rule dicts (see module docstring), and return them
    as a list.

    Raises
    ------
    TypeError
        if ``rules`` is neither a dict nor a list
    ValueError
        if a rule is malformed
    """
    if isinstance(rules, dict):
        rules = [rules]
    if not isinstance(rules, list | tuple):
        raise TypeError("Rules must be a rule dict or a list of rule dicts.")
    for i, rule in enumerate(rules):
        _validate(rule, f"rules[{i}]")
    return list(rules)


# concepts #############################################################################


@dataclass(frozen=True)
class Concept:
    """
    A named construct, found by ``find(nodes, ctx, **params)`` among the ``nodes`` in
    scope. ``detailed`` describes the concept if parameters are given, with their values
    inserted, e.g. ``"call to {name}"``.
    """

    description: str
    find: Callable[..., list[ast.AST]]
    params: tuple[str, ...] = ()
    required: tuple[str, ...] = ()
    detailed: str | None = None

    def describe(self, params: dict[str, Any], o: _Markup) -> str:
        if params and self.detailed:
            return self.detailed.format(**{k: o.q(v) for k, v in params.items()})
        return self.description


def _of_type(*types: type[ast.AST]) -> Callable[..., list[ast.AST]]:
    return lambda nodes, ctx: [n for n in nodes if isinstance(n, types)]


def _calls(nodes: list[ast.AST], ctx: _Context, *, name: str) -> list[ast.AST]:
    """calls of ``name``, as a function (``name(...)``), a method (``_.name(...)``), or
    by its dotted name (``math.sqrt(...)``)"""

    def matches(func: ast.expr) -> bool:
        if isinstance(func, ast.Name):
            return func.id == name
        if isinstance(func, ast.Attribute):
            return func.attr == name or ast.unparse(func) == name
        return False

    return [n for n in nodes if isinstance(n, ast.Call) and matches(n.func)]


def _imports(
    nodes: list[ast.AST], ctx: _Context, *, name: str | None = None
) -> list[ast.AST]:
    """imports of module ``name`` or its submodules, or of any module"""

    def matches(module: str | None) -> bool:
        return name is None or module == name or (module or "").startswith(f"{name}.")

    return [
        n
        for n in nodes
        if (isinstance(n, ast.Import) and any(matches(a.name) for a in n.names))
        or (isinstance(n, ast.ImportFrom) and matches(n.module))
    ]


CONCEPTS: dict[str, Concept] = {
    "loop": Concept("loop", _of_type(*_LOOP_TYPES)),
    "for_loop": Concept("for loop", _of_type(ast.For, ast.AsyncFor)),
    "while_loop": Concept("while loop", _of_type(ast.While)),
    "nested_loop": Concept(
        "nested loop", lambda nodes, ctx: [n for n in nodes if n in ctx.nested_loops]
    ),
    "comprehension": Concept(
        "comprehension",
        _of_type(ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp),
    ),
    "conditional": Concept("conditional", _of_type(ast.If, ast.IfExp, ast.Match)),
    "if": Concept("if statement", _of_type(ast.If)),
    "ternary": Concept("conditional expression", _of_type(ast.IfExp)),
    "match": Concept("match statement", _of_type(ast.Match)),
    "recursion": Concept(
        "recursive call",
        lambda nodes, ctx: [n for n in nodes if n in ctx.recursive_calls],
    ),
    "function": Concept(
        "function definition", _of_type(ast.FunctionDef, ast.AsyncFunctionDef)
    ),
    "lambda": Concept("lambda", _of_type(ast.Lambda)),
    "class": Concept("class definition", _of_type(ast.ClassDef)),
    "generator": Concept("yield", _of_type(ast.Yield, ast.YieldFrom)),
    "exception_handling": Concept("try statement", _of_type(ast.Try, ast.TryStar)),
    "raise": Concept("raise statement", _of_type(ast.Raise)),
    "with": Concept("with statement", _of_type(ast.With, ast.AsyncWith)),
    "global": Concept(
        "global or nonlocal statement", _of_type(ast.Global, ast.Nonlocal)
    ),
    "call": Concept(
        "function call",
        _calls,
        params=("name",),
        required=("name",),
        detailed="call to {name}",
    ),
    "import": Concept(
        "import", _imports, params=("name",), detailed="import of {name}"
    ),
}


# validation ###########################################################################


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _bounds(rule: Rule) -> tuple[Any, Any]:
    """``min`` and ``max`` of a leaf rule, with defaults"""
    hi = rule.get("max")
    return rule.get("min", 1 if hi is None else 0), hi


def _is_node_type(name: Any) -> bool:
    cls = getattr(ast, name, None) if isinstance(name, str) else None
    return isinstance(cls, type) and issubclass(cls, ast.AST)


def _validate(rule: Any, path: str) -> None:
    if not isinstance(rule, dict):
        raise ValueError(f"{path}: must be a rule dict.")  # noqa: TRY004

    keys = [k for k in (*_LEAF_KEYS, *_COMBINATOR_KEYS) if k in rule]
    if len(keys) != 1:
        raise ValueError(
            f"{path}: must have exactly one of the keys "
            f"{[*_LEAF_KEYS, *_COMBINATOR_KEYS]}."
        )
    key = keys[0]

    scope = rule.get("scope")
    if scope is not None and not (
        isinstance(scope, str) and all(p.isidentifier() for p in scope.split("."))
    ):
        raise ValueError(f"{path}.scope: must be a dotted name, e.g. 'Graph.search'.")
    if not isinstance(rule.get("message", ""), str):
        raise ValueError(f"{path}.message: must be a string.")  # noqa: TRY004

    if key in _COMBINATOR_KEYS:
        if unknown := rule.keys() - {key, "scope", "message"}:
            raise ValueError(f"{path}: unknown keys {sorted(unknown)}.")
        if key == "not":
            _validate(rule[key], f"{path}.not")
            return
        if not isinstance(rule[key], list | tuple) or not rule[key]:
            raise ValueError(f"{path}.{key}: must be a non-empty list of rules.")
        for i, child in enumerate(rule[key]):
            _validate(child, f"{path}.{key}[{i}]")
        return

    allowed = {key, *_MODIFIER_KEYS}
    if key == "concept":
        concept = CONCEPTS.get(rule[key]) if isinstance(rule[key], str) else None
        if concept is None:
            raise ValueError(
                f"{path}.concept: unknown concept {rule[key]!r}, expected one of "
                f"{list(CONCEPTS)}."
            )
        allowed.update(concept.params)
        if missing := set(concept.required) - rule.keys():
            raise ValueError(f"{path}: missing keys {sorted(missing)}.")
        for param in concept.params:
            if not isinstance(rule.get(param, ""), str):
                raise ValueError(f"{path}.{param}: must be a string.")  # noqa: TRY004
    elif key == "node":
        names = [rule[key]] if isinstance(rule[key], str) else rule[key]
        if not isinstance(names, list | tuple) or not names:
            raise ValueError(
                f"{path}.node: must be a name or a non-empty list of names."
            )
        for name in names:
            if not _is_node_type(name):
                raise ValueError(f"{path}.node: unknown ast node type {name!r}.")
    else:
        if not isinstance(rule[key], str):
            raise ValueError(f"{path}.pattern: must be a string.")
        try:
            _compile_pattern(rule[key])
        except SyntaxError as err:
            raise ValueError(f"{path}.pattern: {err.msg}") from None

    if unknown := rule.keys() - allowed:
        raise ValueError(f"{path}: unknown keys {sorted(unknown)}.")

    lo, hi = _bounds(rule)
    if not _is_count(lo):
        raise ValueError(f"{path}.min: must be a non-negative integer.")
    if hi is not None and not _is_count(hi):
        raise ValueError(f"{path}.max: must be a non-negative integer or None.")
    if hi is not None and lo > hi:
        raise ValueError(f"{path}: min ({lo}) is greater than max ({hi}).")


# patterns #############################################################################


@cache
def _compile_pattern(pattern: str) -> ast.AST:
    """
    Parse ``pattern`` into the node it matches: an expression, or a statement.

    Raises
    ------
    SyntaxError
        if ``pattern`` is not a single statement or expression
    """
    try:
        body = ast.parse(textwrap.dedent(pattern)).body
    except SyntaxError as err:
        raise SyntaxError(f"invalid syntax ({err.msg}).") from None
    if len(body) != 1:
        raise SyntaxError("must be a single statement or expression.")
    return body[0].value if isinstance(body[0], ast.Expr) else body[0]


def _is_wildcard(p: ast.AST) -> bool:
    """``_``, as an expression or a statement"""
    if isinstance(p, ast.Expr):
        p = p.value
    return isinstance(p, ast.Name) and p.id == "_"


def _is_ellipsis(p: ast.AST) -> bool:
    """``...``, as an expression or a statement"""
    if isinstance(p, ast.Expr):
        p = p.value
    return isinstance(p, ast.Constant) and p.value is Ellipsis


def _has_params(args: ast.arguments) -> bool:
    return bool(
        args.posonlyargs or args.args or args.vararg or args.kwonlyargs or args.kwarg
    )


def _match(p: Any, n: Any) -> bool:
    """whether the node (or field value) ``n`` matches the pattern ``p``"""
    if isinstance(p, list):
        return isinstance(n, list) and _match_list(p, n)
    if not isinstance(p, ast.AST):
        return p == "_" or p == n
    if _is_wildcard(p):
        return isinstance(n, ast.AST)
    if type(p) is not type(n):
        return False
    if isinstance(p, ast.Constant):
        # no identifier wildcard in string constants, and 1 must not match True
        return type(p.value) is type(n.value) and p.value == n.value
    if isinstance(p, ast.arguments) and not _has_params(p):
        return True

    for name in p._fields:
        value = getattr(p, name, None)
        if name in _IGNORED_FIELDS or (
            name in _OPTIONAL_FIELDS and value in (None, [])
        ):
            continue
        if not _match(value, getattr(n, name, None)):
            return False
    return True


def _match_list(ps: list, ns: list) -> bool:
    if not ps:
        return not ns
    if _is_ellipsis(ps[0]):
        return any(_match_list(ps[1:], ns[i:]) for i in range(len(ns) + 1))
    return bool(ns) and _match(ps[0], ns[0]) and _match_list(ps[1:], ns[1:])


# analysis #############################################################################


def _child_defs(
    node: ast.AST,
) -> Iterator[ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef]:
    """the defs in ``node``, which are not nested in other defs"""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, _DEF_TYPES):
            yield child
        else:
            yield from _child_defs(child)


def _descendants(node: ast.AST) -> Iterator[ast.AST]:
    for child in ast.iter_child_nodes(node):
        yield from ast.walk(child)


def _own_nodes(node: ast.AST) -> Iterator[ast.AST]:
    """the nodes in the body of a function, excluding those in nested defs"""
    is_function = isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    stack: list[ast.AST] = list(node.body) if is_function else []
    while stack:
        n = stack.pop()
        yield n
        if not isinstance(n, _DEF_TYPES):
            stack.extend(ast.iter_child_nodes(n))


@dataclass(eq=False)
class _Def:
    """a module, function or class, with the functions and classes defined in it"""

    node: ast.AST
    parent: _Def | None = None
    children: dict[str, _Def] = field(default_factory=dict)

    def __post_init__(self):
        for child in _child_defs(self.node):
            self.children[child.name] = _Def(child, self)

    @property
    def is_class(self) -> bool:
        return isinstance(self.node, ast.ClassDef)

    @property
    def is_function(self) -> bool:
        return isinstance(self.node, ast.FunctionDef | ast.AsyncFunctionDef)

    def walk(self) -> Iterator[_Def]:
        yield self
        for child in self.children.values():
            yield from child.walk()

    def lookup(self, name: str) -> _Def | None:
        """resolve ``name`` as Python would inside this def: in the enclosing functions and
        the module, skipping class bodies"""
        d: _Def | None = self
        while d is not None:
            if not d.is_class and name in d.children:
                return d.children[name]
            d = d.parent
        return None

    def member(self, name: str, seen: frozenset = frozenset()) -> _Def | None:
        """the member ``name`` of this class, including those of base classes defined in
        the same module"""
        if name in self.children:
            return self.children[name]
        for base in getattr(self.node, "bases", ()):
            cls = (
                self.parent.lookup(base.id)
                if self.parent and isinstance(base, ast.Name)
                else None
            )
            if (
                cls
                and cls.is_class
                and cls not in seen
                and (found := cls.member(name, seen | {self}))
            ):
                return found
        return None

    def enclosing_class(self) -> _Def | None:
        d = self.parent
        while d is not None and not d.is_class:
            d = d.parent
        return d

    def resolve(self, func: ast.expr) -> _Def | None:
        """the function called by ``func(...)`` inside this def, if it is defined in the
        module. Methods are resolved by name: ``C.m(...)`` calls the method ``m`` of the
        class ``C``, any other ``_.m(...)`` the method ``m`` of the enclosing class."""
        target = None
        if isinstance(func, ast.Name):
            target = self.lookup(func.id)
        elif isinstance(func, ast.Attribute):
            owner = (
                self.lookup(func.value.id) if isinstance(func.value, ast.Name) else None
            )
            cls = owner if owner and owner.is_class else self.enclosing_class()
            target = cls.member(func.attr) if cls else None
        if target and target.is_class:
            target = target.member("__init__")
        return target if target and target.is_function else None


def _recursive_calls(tree: ast.Module) -> set[ast.AST]:
    """calls from a function to itself, directly or via other functions"""
    functions = [d for d in _Def(tree).walk() if d.is_function]
    edges = {
        f: [
            (call, callee)
            for call in _own_nodes(f.node)
            if isinstance(call, ast.Call) and (callee := f.resolve(call.func))
        ]
        for f in functions
    }

    def reaches(start: _Def, goal: _Def) -> bool:
        seen, stack = set(), [start]
        while stack:
            d = stack.pop()
            if d is goal:
                return True
            if d not in seen:
                seen.add(d)
                stack.extend(callee for _, callee in edges.get(d, ()))
        return False

    return {call for f in functions for call, callee in edges[f] if reaches(callee, f)}


def _nested_loops(tree: ast.Module) -> set[ast.AST]:
    """loops inside other loops of the same function"""
    nested = set()

    def visit(node: ast.AST, in_loop: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, _LOOP_TYPES):
                if in_loop:
                    nested.add(child)
                visit(child, True)
            else:
                visit(
                    child, in_loop and not isinstance(child, (*_DEF_TYPES, ast.Lambda))
                )

    visit(tree, False)
    return nested


def _find_scope(tree: ast.Module, scope: str) -> ast.AST | None:
    node: ast.AST | None = tree
    for name in scope.split("."):
        matches = [d for d in _child_defs(node) if d.name == name]
        node = matches[-1] if matches else None
        if node is None:
            return None
    return node


# evaluation ###########################################################################


@dataclass(frozen=True)
class _Markup:
    rich: bool

    def text(self, text: str) -> str:
        """``text`` as it is, escaped if rich markup is used"""
        return escape(text) if self.rich else text

    def b(self, text: Any) -> str:
        """bold ``text``, if rich markup is used"""
        return f"[b]{escape(str(text))}[/]" if self.rich else str(text)

    def q(self, name: str) -> str:
        """quote and bold ``name``"""
        return self.b(f"'{name}'")


class _Context:
    def __init__(self, tree: ast.Module, o: _Markup):
        self.tree = tree
        self.o = o
        self._nodes: dict[str | None, list[ast.AST]] = {}

    @cached_property
    def recursive_calls(self) -> set[ast.AST]:
        return _recursive_calls(self.tree)

    @cached_property
    def nested_loops(self) -> set[ast.AST]:
        return _nested_loops(self.tree)

    def nodes_in(self, scope: str | None) -> list[ast.AST]:
        if scope not in self._nodes:
            node: ast.AST | None = self.tree
            if scope is not None and (node := _find_scope(self.tree, scope)) is None:
                raise _MissingScope(scope)
            self._nodes[scope] = list(_descendants(node))
        return self._nodes[scope]


def _find(rule: Rule, ctx: _Context, scope: str | None) -> list[ast.AST]:
    nodes = ctx.nodes_in(scope)
    if "concept" in rule:
        concept = CONCEPTS[rule["concept"]]
        params = {p: rule[p] for p in concept.params if p in rule}
        return concept.find(nodes, ctx, **params)
    if "node" in rule:
        names = [rule["node"]] if isinstance(rule["node"], str) else rule["node"]
        types = tuple(getattr(ast, name) for name in names)
        return [n for n in nodes if isinstance(n, types)]
    pattern = _compile_pattern(rule["pattern"])
    return [n for n in nodes if _match(pattern, n)]


def _noun(rule: Rule, o: _Markup) -> str:
    """what a leaf rule looks for, e.g. "loop" or "call to 'sorted'" """
    if "concept" in rule:
        concept = CONCEPTS[rule["concept"]]
        return concept.describe({p: rule[p] for p in concept.params if p in rule}, o)
    if "node" in rule:
        names = [rule["node"]] if isinstance(rule["node"], str) else rule["node"]
        return f"{' or '.join(o.b(n) for n in names)} node"
    return o.b(f"`{' '.join(rule['pattern'].split())}`")


def _requirement(rule: Rule, o: _Markup, scope: str | None) -> str:
    """what a leaf rule requires, e.g. "no loop in 'fib'" """
    noun, (lo, hi) = _noun(rule, o), _bounds(rule)
    if hi == 0:
        text = f"no {noun}"
    elif lo == 1 and hi is None:
        text = noun
    elif lo == hi:
        text = f"exactly {lo} × {noun}"
    elif hi is None:
        text = f"at least {lo} × {noun}"
    elif lo == 0:
        text = f"at most {hi} × {noun}"
    else:
        text = f"between {lo} and {hi} × {noun}"
    return f"{text} in {o.q(scope)}" if scope else text


def _describe(rule: Rule, o: _Markup, scope: str | None) -> list[str]:
    """lines describing what ``rule`` requires"""
    scope = rule.get("scope", scope)
    if "not" in rule:
        return ["not:", *_bullets([_describe(rule["not"], o, scope)])]
    for key in ("all", "any"):
        if key in rule:
            return [f"{key} of:", *_bullets(_describe(c, o, scope) for c in rule[key])]
    return [_requirement(rule, o, scope)]


def _bullets(items) -> list[str]:
    """indent a list of line lists as bullet points"""
    return [
        f"  - {line}" if i == 0 else f"    {line}"
        for lines in items
        for i, line in enumerate(lines)
    ]


def _found(nodes: list[ast.AST], o: _Markup) -> str:
    if not nodes:
        return "none"
    lines = sorted(
        {ln for n in nodes if (ln := getattr(n, "lineno", None)) is not None}
    )
    if not lines:
        return o.b(len(nodes))
    label = "line" if len(lines) == 1 else "lines"
    return f"{o.b(len(nodes))} ({label} {', '.join(map(str, lines))})"


def _evaluate(rule: Rule, ctx: _Context, scope: str | None) -> list[str]:
    """messages for ``rule`` not holding, empty if it holds"""
    o = ctx.o
    scope = rule.get("scope", scope)

    if "all" in rule:
        messages = [m for child in rule["all"] for m in _evaluate(child, ctx, scope)]
    elif "any" in rule:
        # all children are evaluated, so that missing scopes are always reported
        failures = [_evaluate(child, ctx, scope) for child in rule["any"]]
        if not all(failures):
            return []
        lines = [[line for m in msgs for line in m.splitlines()] for msgs in failures]
        messages = ["\n".join(["None of the following holds:", *_bullets(lines)])]
    elif "not" in rule:
        if _evaluate(rule["not"], ctx, scope):
            return []
        description = _bullets([_describe(rule["not"], o, scope)])
        messages = ["\n".join(["Expected the following not to hold:", *description])]
    else:
        found = _find(rule, ctx, scope)
        lo, hi = _bounds(rule)
        if lo <= len(found) and (hi is None or len(found) <= hi):
            return []
        messages = [
            f"Expected {_requirement(rule, o, scope)}, found {_found(found, o)}."
        ]

    if messages and "message" in rule:
        return [o.text(rule["message"])]
    return messages
