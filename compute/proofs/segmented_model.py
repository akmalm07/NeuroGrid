"""A small Granite-style transformer and a row-tiled projection template for the proof benchmark."""

from dataclasses import dataclass
import math
import random

from onnx import TensorProto as T, helper

WIDTH = 4
CONTEXT = 3
VOCAB = 4
Q = 18
MODEL = "neurogrid/tiny-granite-style-q18-v1"


class Graph:
    def __init__(self, params: dict[str, tuple[int, ...]], *, integer_inputs: tuple[str, ...] = ()):
        self.nodes = []
        self.constants = []
        self.inputs = [helper.make_tensor_value_info(key, T.INT64 if key in integer_inputs else T.FLOAT, shape)
                       for key, shape in params.items()]

    def constant(self, values: list[float] | list[int], shape: tuple[int, ...] = (), dtype: int = T.FLOAT) -> str:
        name = f"const{len(self.constants)}"
        self.constants.append(helper.make_tensor(name, dtype, shape, values))
        return name

    def op(self, op: str, *inputs: str, **attributes) -> str:
        name = f"v{len(self.nodes)}"
        self.nodes.append(helper.make_node(op, inputs, [name], **attributes))
        return name

    def axis(self, value: int) -> str:
        return self.constant([value], (1,), T.INT64)

    def finish(self, name: str, outputs: list[tuple[str, tuple[int, ...], int]]):
        # This extra public commitment makes the proof specific to this request and scheduled segment.
        context = helper.make_tensor_value_info("context", T.FLOAT, (16,))
        bound = self.op("Identity", "context")
        info = [helper.make_tensor_value_info(value, dtype, shape) for value, shape, dtype in outputs]
        info.append(helper.make_tensor_value_info(bound, T.FLOAT, (16,)))
        graph = helper.make_graph(self.nodes, name, [*self.inputs, context], info, initializer=self.constants)
        return helper.make_model(graph, opset_imports=[helper.make_opsetid("", 18)], ir_version=8)


def projection(rows: int, fan_in: int, widths: tuple[int, ...] | None = None):
    """Use a full inner dimension per tile so Q18 rounding happens once per output lane."""
    widths = widths or (fan_in,)
    if sum(widths) != fan_in:
        raise ValueError("Projection inputs do not match fan-in")
    params = {f"x{i}": (1, width) for i, width in enumerate(widths)}
    graph = Graph(params | {"weights": (fan_in, rows)})
    activation = next(iter(params)) if len(params) == 1 else graph.op("Concat", *params, axis=-1)
    result = graph.op("MatMul", activation, "weights")
    return graph.finish(f"matmul-{fan_in}-{rows}", [(result, (1, rows), T.FLOAT)])


def templates() -> dict:
    g = Graph({"index": (1,), "table": (VOCAB, WIDTH)}, integer_inputs=("index",))
    embedding = g.finish("embedding", [(g.op("Gather", "table", "index", axis=0), (1, WIDTH), T.FLOAT)])

    g = Graph({"x": (1, WIDTH), "gamma": (1, WIDTH)})
    square = g.op("Mul", "x", "x")
    mean = g.op("ReduceMean", square, g.axis(-1), keepdims=1)
    root = g.op("Sqrt", g.op("Add", mean, g.constant([1e-5])))
    normalize = g.finish("rmsnorm", [(g.op("Mul", g.op("Mul", "x", g.op("Reciprocal", root)), "gamma"),
                                      (1, WIDTH), T.FLOAT)])

    g = Graph({"q": (1, WIDTH), "kv": (1, WIDTH), "keys": (CONTEXT, 2), "values": (CONTEXT, 2),
               "write": (CONTEXT, 1), "causal": (1, CONTEXT), "cos": (1, 1), "sin": (1, 1)})
    query = g.op("Reshape", "q", g.constant([2, 2], (2,), T.INT64))
    key, value = [
        g.op("Slice", "kv", g.axis(start), g.axis(start + 2), g.axis(1)) for start in (0, 2)
    ]
    switch = g.constant([1, 0], (2,), T.INT64)
    sign = g.constant([-1.0, 1.0], (1, 2))

    def rotate(tensor: str) -> str:
        shifted = g.op("Mul", g.op("Gather", tensor, switch, axis=1), sign)
        return g.op("Add", g.op("Mul", tensor, "cos"), g.op("Mul", shifted, "sin"))

    def update(cache: str, current: str) -> str:
        return g.op("Add", cache, g.op("Mul", "write", g.op("Sub", current, cache)))

    keys, values = update("keys", rotate(key)), update("values", value)
    dots = g.op("MatMul", rotate(query), g.op("Transpose", keys, perm=[1, 0]))
    scores = g.op("Mul", dots, g.constant([1.0 / 128]))
    # Center explicitly before the Q18 lookup and set masked numerators to exactly zero.
    scores = g.op("Mul", scores, "causal")
    centered = g.op("Sub", scores, g.op("ReduceMax", scores, g.axis(-1), keepdims=1))
    numerators = g.op("Mul", g.op("Exp", centered), "causal")
    denominator = g.op("ReduceSum", numerators, g.axis(-1), keepdims=1)
    probabilities = g.op("Mul", numerators, g.op("Reciprocal", denominator))
    mixed = g.op("Reshape", g.op("MatMul", probabilities, values), g.constant([1, WIDTH], (2,), T.INT64))
    attention = g.finish("attention", [(mixed, (1, WIDTH), T.FLOAT), (keys, (CONTEXT, 2), T.FLOAT),
                                       (values, (CONTEXT, 2), T.FLOAT)])

    g = Graph({"x": (1, WIDTH), "update": (1, WIDTH)})
    residual = g.finish("residual", [(g.op("Add", "x", "update"), (1, WIDTH), T.FLOAT)])

    g = Graph({"gate": (1, WIDTH), "up": (1, WIDTH)})
    gated = g.op("Mul", g.op("Mul", "gate", g.op("Sigmoid", "gate")), "up")
    silu = g.finish("silu", [(gated, (1, WIDTH), T.FLOAT)])

    g = Graph({"logits": (1, VOCAB)})
    greedy = g.finish("greedy", [(g.op("ArgMax", "logits", axis=1, keepdims=0, select_last_index=0),
                                  (1,), T.INT64)])
    return {
        "embedding": embedding, "rmsnorm": normalize, "linear": projection(WIDTH, WIDTH),
        "attention": attention, "residual": residual, "silu": silu,
        "down": projection(WIDTH, WIDTH * 2, (WIDTH, WIDTH)), "greedy": greedy,
    }


@dataclass(frozen=True)
class Segment:
    operation: str
    position: int
    layer: int
    name: str
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]


def model_plan() -> tuple[dict[str, tuple[int, list[float]]], list[Segment], list[str]]:
    """One decoder layer, two prompt tokens, and two greedily generated tokens."""
    rng = random.Random(164)
    constants: dict[str, tuple[int, list[float]]] = {}
    for name in ("embedding", "q", "kv", "o", "gate0", "gate1", "up0", "up1", "down", "head"):
        # The fixture has deterministic initialized weights and no training or quality claim.
        extent = 0.003 if name.startswith(("gate", "up")) else 0.18
        if name in ("q", "kv"):
            extent = 0.045
        constants[name] = (Q, [rng.uniform(-extent, extent) for _ in range(32 if name == "down" else 16)])
    constants["gamma"] = (Q, [1.0] * WIDTH)
    constants["cache0"] = (Q, [0.0] * (CONTEXT * 2))

    plan: list[Segment] = []
    replies = []
    prior_keys = prior_values = "cache0"
    for position in range(CONTEXT):
        prefix = f"p{position}"
        for name, values in (
            ("write", [float(i == position) for i in range(CONTEXT)]),
            ("causal", [float(i <= position) for i in range(CONTEXT)]),
            ("cos", [math.cos(position)]), ("sin", [math.sin(position)]),
        ):
            constants[f"{prefix}-{name}"] = (Q, values)

        def add(operation: str, name: str, inputs: tuple[str, ...], outputs: tuple[str, ...] = (),
                layer: int = 0) -> str:
            full_name = f"{prefix}-{name}"
            plan.append(Segment(operation, position, layer, full_name, inputs, outputs or (full_name,)))
            return full_name

        index = f"prompt{position}" if position < 2 else replies[-1]
        hidden = add("embedding", "embed", (index, "embedding"), layer=-1)
        norm = add("rmsnorm", "norm1", (hidden, "gamma"))
        q = add("linear", "q", (norm, "q"))
        kv = add("linear", "kv", (norm, "kv"))
        keys, values = f"{prefix}-keys", f"{prefix}-values"
        mixed = add("attention", "attention", (q, kv, prior_keys, prior_values, f"{prefix}-write",
                                                f"{prefix}-causal", f"{prefix}-cos", f"{prefix}-sin"),
                    (f"{prefix}-attention", keys, values))
        prior_keys, prior_values = keys, values
        updated = add("linear", "out", (mixed, "o"))
        hidden = add("residual", "residual1", (hidden, updated))
        norm = add("rmsnorm", "norm2", (hidden, "gamma"))
        tiles = []
        for tile in range(2):
            gate = add("linear", f"gate{tile}", (norm, f"gate{tile}"))
            up = add("linear", f"up{tile}", (norm, f"up{tile}"))
            tiles.append(add("silu", f"gated{tile}", (gate, up)))
        down = add("down", "down", (*tiles, "down"))
        hidden = add("residual", "residual2", (hidden, down))
        if position >= 1:
            norm = add("rmsnorm", "final", (hidden, "gamma"), layer=1)
            logits = add("linear", "head", (norm, "head"), layer=1)
            replies.append(add("greedy", "sample", (logits,), layer=1))
    return constants, plan, replies
