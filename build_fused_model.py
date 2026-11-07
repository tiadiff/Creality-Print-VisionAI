"""
Builds models/realesr_animevideo_v3_fused.onnx from the original Real-ESRGAN model.

The fused model takes a uint8 NHWC RGB image and returns a uint8 NHWC RGB image,
so normalisation, clipping, quantisation and layout conversion run on the GPU
instead of in numpy (which cost as much as the inference itself).
"""

import os
import onnx
from onnx import TensorProto, helper, numpy_helper
import numpy as np

SRC = os.path.join(os.path.dirname(__file__), "models", "realesr_animevideo_v3.onnx")
DST = os.path.join(os.path.dirname(__file__), "models", "realesr_animevideo_v3_fused.onnx")

model = onnx.load(SRC)
g = model.graph

orig_in = g.input[0].name
orig_out = g.output[0].name
print(f"original input={orig_in} output={orig_out} opset={[o.version for o in model.opset_import]}")

# Rename the original graph output so we can append post-processing nodes.
raw_out = orig_out + "_raw"
for node in g.node:
    for i, name in enumerate(node.output):
        if name == orig_out:
            node.output[i] = raw_out

def const(name, arr):
    return numpy_helper.from_array(np.asarray(arr), name=name)

g.initializer.extend([
    const("fx_inv255", np.float32(1.0 / 255.0)),
    const("fx_255", np.float32(255.0)),
    const("fx_half", np.float32(0.5)),
    const("fx_zero", np.float32(0.0)),
    const("fx_one", np.float32(1.0)),
])

pre = [
    helper.make_node("Cast", ["input_u8"], ["fx_f32"], to=TensorProto.FLOAT),
    helper.make_node("Mul", ["fx_f32", "fx_inv255"], ["fx_norm"]),
    helper.make_node("Transpose", ["fx_norm"], [orig_in], perm=[0, 3, 1, 2]),
]
post = [
    helper.make_node("Clip", [raw_out, "fx_zero", "fx_one"], ["fx_clip"]),
    helper.make_node("Mul", ["fx_clip", "fx_255"], ["fx_scaled"]),
    helper.make_node("Add", ["fx_scaled", "fx_half"], ["fx_round"]),
    helper.make_node("Transpose", ["fx_round"], ["fx_nhwc"], perm=[0, 2, 3, 1]),
    helper.make_node("Cast", ["fx_nhwc"], ["output_u8"], to=TensorProto.UINT8),
]

# New graph input / output, original input becomes an internal tensor.
del g.input[:]
g.input.append(helper.make_tensor_value_info("input_u8", TensorProto.UINT8, [1, "h", "w", 3]))
del g.output[:]
g.output.append(helper.make_tensor_value_info("output_u8", TensorProto.UINT8, [1, "oh", "ow", 3]))

nodes = list(pre) + list(g.node) + list(post)
del g.node[:]
g.node.extend(nodes)

onnx.checker.check_model(model)
onnx.save(model, DST)
print(f"saved {DST}")

