import sglang.kernels as K
from sglang.kernels import registry, select_kernel, PlatformInfo

pl = PlatformInfo.detect()
print("platform:", pl.device_type, pl.cuda_arch_major)
print("op count:", len(registry.ops()))

s = select_kernel("quantization.sgl_per_token_quant_fp8")
print("fp8 single-backend ->", s.backend.value, s.target)

try:
    select_kernel("layernorm.rmsnorm")
    print("rmsnorm multi-backend -> resolved")
except ValueError:
    print("rmsnorm multi-backend -> ValueError (device gate)")

from sglang.kernels.ops.layernorm import rmsnorm
import torch

out = rmsnorm(torch.randn(4, 8, dtype=torch.bfloat16), torch.ones(8, dtype=torch.bfloat16))
print("rmsnorm CPU fallback out.shape =", tuple(out.shape))
