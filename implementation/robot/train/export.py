"""Export a brax PPO policy to ONNX with the same interface as the official Open Duck policy.

Semantics follow Open_Duck_Playground/playground/common/export_onnx.py (which needs
tensorflow + tf2onnx): obs -> (obs - mean) / std -> Dense+swish ... Dense ->
take the first half (loc) -> tanh.  Input ``obs`` [1, n_obs] float32, output
``continuous_actions`` [1, n_act].  Built directly with the onnx helper API.
"""

from __future__ import annotations

import pickle
from pathlib import Path

import numpy as np

ACTIVATIONS = ("swish", "relu", "tanh", "elu")


def _np(x):
    return np.asarray(x, dtype=np.float32)


def extract(params, obs_key: str = "state") -> dict:
    """brax ppo.train params = (normalizer, policy, value) -> plain numpy dict."""
    norm, policy = params[0], params[1]
    mean, std = norm.mean, norm.std
    if isinstance(mean, dict):
        mean, std = mean[obs_key], std[obs_key]
    p = policy.get("params", policy) if isinstance(policy, dict) else policy["params"]
    layers = []
    i = 0
    while f"hidden_{i}" in p:
        layers.append((_np(p[f"hidden_{i}"]["kernel"]), _np(p[f"hidden_{i}"]["bias"])))
        i += 1
    if not layers:
        raise ValueError(f"no hidden_<i> layers in policy params: {list(p)}")
    return {"mean": _np(mean), "std": _np(std), "layers": layers}


def forward(net: dict, obs: np.ndarray, activation: str = "swish") -> np.ndarray:
    x = (np.asarray(obs, np.float32) - net["mean"]) / net["std"]
    for k, (w, b) in enumerate(net["layers"]):
        x = x @ w + b
        if k < len(net["layers"]) - 1:
            x = _act_np(x, activation)
    loc = x[..., : x.shape[-1] // 2]
    return np.tanh(loc)


def _act_np(x, a):
    if a == "swish":
        return x / (1.0 + np.exp(-x))
    if a == "relu":
        return np.maximum(x, 0)
    if a == "tanh":
        return np.tanh(x)
    if a == "elu":
        return np.where(x > 0, x, np.exp(x) - 1)
    raise ValueError(a)


def to_onnx(net: dict, path: Path, activation: str = "swish") -> Path:
    import onnx
    from onnx import TensorProto, helper, numpy_helper

    if activation not in ACTIVATIONS:
        raise ValueError(activation)
    n_obs = int(net["mean"].shape[-1])
    n_out = int(net["layers"][-1][0].shape[1])
    n_act = n_out // 2
    inits = [numpy_helper.from_array(net["mean"].reshape(1, -1), "obs_mean"),
             numpy_helper.from_array(net["std"].reshape(1, -1), "obs_std"),
             numpy_helper.from_array(np.array([0], np.int64), "sl_start"),
             numpy_helper.from_array(np.array([n_act], np.int64), "sl_end"),
             numpy_helper.from_array(np.array([1], np.int64), "sl_axis")]
    nodes = [helper.make_node("Sub", ["obs", "obs_mean"], ["x_c"]),
             helper.make_node("Div", ["x_c", "obs_std"], ["x0"])]
    cur = "x0"
    for k, (w, b) in enumerate(net["layers"]):
        inits += [numpy_helper.from_array(w, f"w{k}"), numpy_helper.from_array(b.reshape(1, -1), f"b{k}")]
        nodes += [helper.make_node("MatMul", [cur, f"w{k}"], [f"m{k}"]),
                  helper.make_node("Add", [f"m{k}", f"b{k}"], [f"z{k}"])]
        cur = f"z{k}"
        if k < len(net["layers"]) - 1:
            if activation == "swish":
                nodes += [helper.make_node("Sigmoid", [cur], [f"s{k}"]),
                          helper.make_node("Mul", [cur, f"s{k}"], [f"a{k}"])]
            else:
                op = {"relu": "Relu", "tanh": "Tanh", "elu": "Elu"}[activation]
                nodes.append(helper.make_node(op, [cur], [f"a{k}"]))
            cur = f"a{k}"
    nodes += [helper.make_node("Slice", [cur, "sl_start", "sl_end", "sl_axis"], ["loc"]),
              helper.make_node("Tanh", ["loc"], ["continuous_actions"])]
    graph = helper.make_graph(
        nodes, "rlsok_policy",
        [helper.make_tensor_value_info("obs", TensorProto.FLOAT, [1, n_obs])],
        [helper.make_tensor_value_info("continuous_actions", TensorProto.FLOAT, [1, n_act])],
        inits)
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 11)],
                              producer_name="rlsok.robot.train.export")
    model.ir_version = 7
    onnx.checker.check_model(model)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    onnx.save(model, str(path))
    return path


def check_onnx(net: dict, path: Path, activation: str = "swish", n: int = 16, seed: int = 0) -> float:
    """Max |onnx - numpy| over random observations; used as an export self-check."""
    import onnxruntime as ort

    s = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
    rng = np.random.default_rng(seed)
    worst = 0.0
    for _ in range(n):
        o = (net["mean"] + net["std"] * rng.standard_normal(net["mean"].shape)).astype(np.float32)[None]
        a = s.run(None, {"obs": o})[0]
        worst = max(worst, float(np.abs(a - forward(net, o, activation)).max()))
    return worst


def save_net(net: dict, path: Path) -> None:
    Path(path).write_bytes(pickle.dumps({"mean": net["mean"], "std": net["std"],
                                         "layers": [(w, b) for w, b in net["layers"]]}))


def load_net(path: Path) -> dict:
    return pickle.loads(Path(path).read_bytes())
