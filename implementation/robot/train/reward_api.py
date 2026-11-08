"""Eureka-style reward interface.

An external AI submits Python source that defines exactly one function

    def reward(f, xp):
        ...
        return {"component_name": scalar_expression, ...}

``f`` maps feature names (see each backend's ``feature_spec``) to arrays; ``xp``
is the array module (``jax.numpy`` inside training, ``numpy`` for validation).
The returned components are summed by the backend (each with weight 1, so the
AI owns all scaling) and logged one by one, so the per-component values can be
fed back to the AI after training.

The source is never executed before it passes an AST whitelist: no imports,
no attribute access except ``xp.<allowed function>``, no loops, no Python
branching on arrays (use ``xp.where``), no dunder names, constant feature keys
that the backend actually provides. It is then run once with numpy on zero
and random features and every component must be a finite scalar.
"""

from __future__ import annotations

import ast
import hashlib
import math
import re
from dataclasses import dataclass, field

import numpy as np

MAX_SOURCE_BYTES = 8192
MAX_COMPONENTS = 32
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")

XP_FUNCS = frozenset({
    "abs", "exp", "log", "log1p", "sqrt", "square", "sum", "mean", "max", "min", "clip",
    "where", "tanh", "cos", "sin", "maximum", "minimum", "dot", "sign", "zeros_like",
    "ones_like", "logical_and", "logical_or", "logical_not", "array", "linalg.norm",
    "any", "all", "prod", "arctan2",
})
XP_CONSTS = frozenset({"pi"})
BUILTINS = {"abs": abs, "min": min, "max": max, "float": float}

_ALLOWED_NODES = (
    ast.Module, ast.FunctionDef, ast.arguments, ast.arg, ast.Return, ast.Assign, ast.AugAssign,
    ast.Expr, ast.Dict, ast.Constant, ast.Name, ast.Load, ast.Store, ast.BinOp, ast.UnaryOp,
    ast.Compare, ast.Call, ast.Attribute, ast.Subscript, ast.Slice, ast.Tuple, ast.List,
    ast.keyword,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow, ast.USub, ast.UAdd,
    ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Eq, ast.NotEq,
    ast.BitAnd, ast.BitOr, ast.Invert,
)


class RewardRejected(ValueError):
    """The submitted reward source violates the contract; the reason is fed back to the AI."""


@dataclass
class RewardFn:
    source: str
    sha256: str
    components: list[str]
    fn: object = field(repr=False)

    def __call__(self, f, xp):
        return self.fn(f, xp)

    def describe(self) -> dict:
        return {"sha256": self.sha256, "components": self.components, "bytes": len(self.source.encode())}


def _xp_attr(node: ast.Attribute) -> str | None:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name) and node.id == "xp":
        return ".".join(reversed(parts))
    return None


def check_source(source: str, feature_spec: dict[str, tuple]) -> list[str]:
    """Validate the AST; return the component names. Raises RewardRejected."""
    if len(source.encode("utf-8")) > MAX_SOURCE_BYTES:
        raise RewardRejected(f"source longer than {MAX_SOURCE_BYTES} bytes")
    try:
        tree = ast.parse(source, mode="exec")
    except SyntaxError as e:
        raise RewardRejected(f"syntax error: {e.msg} (line {e.lineno})") from None
    body = [n for n in tree.body if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))]
    if len(body) != 1 or not isinstance(body[0], ast.FunctionDef) or body[0].name != "reward":
        raise RewardRejected("the source must define exactly one function `def reward(f, xp):` and nothing else")
    fn = body[0]
    a = fn.args
    if ([x.arg for x in a.args] != ["f", "xp"] or a.vararg or a.kwarg or a.kwonlyargs
            or a.defaults or a.posonlyargs or fn.decorator_list or fn.returns):
        raise RewardRejected("signature must be exactly `def reward(f, xp):`")
    stmts = [s for s in fn.body if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))]
    if not stmts or not isinstance(stmts[-1], ast.Return):
        raise RewardRejected("the function must end with `return {...}`")
    ret = stmts[-1].value
    if not isinstance(ret, ast.Dict) or not ret.keys:
        raise RewardRejected("return value must be a non-empty dict literal {\"name\": expression, ...}")
    names = []
    for k in ret.keys:
        if not (isinstance(k, ast.Constant) and isinstance(k.value, str) and NAME_RE.match(k.value)):
            raise RewardRejected("component names must be string literals matching [a-z][a-z0-9_]{0,39}")
        names.append(k.value)
    if len(set(names)) != len(names):
        raise RewardRejected("duplicate component names")
    if len(names) > MAX_COMPONENTS:
        raise RewardRejected(f"at most {MAX_COMPONENTS} components")

    local = {"f", "xp"}
    for s in stmts[:-1]:
        if isinstance(s, ast.Assign):
            for t in s.targets:
                if not isinstance(t, ast.Name):
                    raise RewardRejected(f"line {s.lineno}: only plain `name = expression` assignments are allowed")
                local.add(t.id)
        elif isinstance(s, ast.AugAssign):
            if not isinstance(s.target, ast.Name):
                raise RewardRejected(f"line {s.lineno}: only plain names can be augmented")
        elif not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant)):
            raise RewardRejected(f"line {s.lineno}: only assignments and the final return are allowed "
                                 f"(no loops/if/with/try/import)")

    for node in ast.walk(fn):
        if not isinstance(node, _ALLOWED_NODES):
            raise RewardRejected(f"line {getattr(node, 'lineno', '?')}: `{type(node).__name__}` is not allowed "
                                 "(use xp.where instead of if/and/or; no loops, lambdas, comprehensions)")
        if isinstance(node, ast.Name):
            if "__" in node.id:
                raise RewardRejected(f"line {node.lineno}: dunder names are not allowed")
            if isinstance(node.ctx, ast.Load) and node.id not in local and node.id not in BUILTINS:
                raise RewardRejected(f"line {node.lineno}: unknown name `{node.id}`")
            if isinstance(node.ctx, ast.Store) and node.id in ("f", "xp"):
                raise RewardRejected(f"line {node.lineno}: `{node.id}` cannot be reassigned")
        elif isinstance(node, ast.Attribute):
            path = _xp_attr(node)
            if path is None or "__" in node.attr:
                raise RewardRejected(f"line {node.lineno}: attribute access is only allowed as xp.<function>")
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                if node.func.id not in BUILTINS:
                    raise RewardRejected(f"line {node.lineno}: call to `{node.func.id}` is not allowed")
            elif isinstance(node.func, ast.Attribute):
                path = _xp_attr(node.func)
                if path not in XP_FUNCS:
                    raise RewardRejected(f"line {node.lineno}: xp.{path} is not in the allowed list "
                                         f"{sorted(XP_FUNCS)}")
            else:
                raise RewardRejected(f"line {node.lineno}: only direct calls are allowed")
        elif isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id == "f":
            key = node.slice
            if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
                raise RewardRejected(f"line {node.lineno}: features must be read as f[\"name\"] with a literal name")
            if key.value not in feature_spec:
                raise RewardRejected(f"line {node.lineno}: unknown feature f[{key.value!r}]; "
                                     f"available: {sorted(feature_spec)}")
        elif isinstance(node, ast.Constant) and isinstance(node.value, (bytes, complex)):
            raise RewardRejected(f"line {node.lineno}: constant type not allowed")
    # attributes used outside calls (xp.pi) must be constants
    for node in ast.walk(fn):
        if isinstance(node, ast.Attribute) and not any(
                isinstance(p, ast.Call) and p.func is node for p in ast.walk(fn)):
            if _xp_attr(node) not in XP_CONSTS and not _xp_attr(node).startswith("linalg"):
                raise RewardRejected(f"line {node.lineno}: xp.{_xp_attr(node)} is not an allowed constant")
    return names


def sample_features(feature_spec: dict[str, tuple], rng: np.random.Generator | None = None) -> dict:
    out = {}
    for k, (shape, dtype) in feature_spec.items():
        if rng is None:
            out[k] = np.zeros(shape, dtype=dtype)
        elif dtype == "bool":
            out[k] = rng.random(shape) > 0.5
        else:
            out[k] = rng.standard_normal(shape).astype(dtype)
    return out


def compile_reward(source: str, feature_spec: dict[str, tuple]) -> RewardFn:
    """AST-check, compile and numerically probe a reward submission."""
    names = check_source(source, feature_spec)
    code = compile(ast.parse(source), "<ai-reward>", "exec")
    ns: dict = {"__builtins__": dict(BUILTINS)}
    exec(code, ns)  # noqa: S102 - source passed the whitelist above
    fn = ns["reward"]
    for probe, feats in (("zeros", sample_features(feature_spec)),
                         ("random", sample_features(feature_spec, np.random.default_rng(0)))):
        with np.errstate(all="ignore"):
            try:
                out = fn(feats, np)
            except Exception as e:  # noqa: BLE001 - report any runtime error back to the AI
                raise RewardRejected(f"runtime error on {probe} features: {type(e).__name__}: {e}") from None
        if not isinstance(out, dict) or list(out) != names:
            raise RewardRejected("the function did not return the declared dict")
        for k, v in out.items():
            v = np.asarray(v)
            if v.ndim != 0:
                raise RewardRejected(f"component {k!r} has shape {v.shape}; every component must be a scalar "
                                     "(reduce with xp.sum / xp.mean)")
            if probe == "zeros" and not math.isfinite(float(v)):
                raise RewardRejected(f"component {k!r} is not finite on all-zero features ({float(v)})")
    return RewardFn(source=source, sha256=hashlib.sha256(source.encode()).hexdigest(), components=names, fn=fn)


def describe_features(feature_spec: dict[str, tuple], notes: dict[str, str]) -> str:
    rows = [f"- f[\"{k}\"]: shape {tuple(s)}, {d}. {notes.get(k, '')}".rstrip() for k, (s, d) in feature_spec.items()]
    return "\n".join(rows)
