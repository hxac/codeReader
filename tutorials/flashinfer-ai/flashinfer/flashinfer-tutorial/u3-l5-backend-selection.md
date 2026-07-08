# 后端选择机制（fa2/fa3/cudnn）

## 1. 本讲目标

学完本讲，你应该能够：

- 说出 FlashInfer 为什么对「同一个注意力算子」提供多个后端（FlashAttention-2、FlashAttention-3、cuDNN、trtllm-gen、cute-dsl），以及它们各自的适用场景。
- 读懂 `flashinfer/utils.py` 中的 `determine_attention_backend` 启发式，说清楚它在什么条件下返回 `"fa3"`、什么条件下退回 `"fa2"`，以及为什么它**从不**返回 cuDNN/trtllm-gen。
- 理解 `@backend_requirement` 与 `@supported_compute_capability` 这两个装饰器如何为新式 API（GEMM/MoE/norm 等）做「后端 + 算力 + 问题尺寸」的运行期校验，并能用 `is_backend_supported` / `is_compute_capability_supported` 做能力查询。
- 看懂 cuDNN 后端「不写 CUDA kernel、而是构造 cuDNN 计算图」的独特实现方式。
- 解释一个关键问题：**为什么 FA3 必须运行在 SM90a（Hopper）以上？**

本讲承接 u3-l3（decode 的 plan/run）与 u3-l4（prefill 的 plan/run），把「`backend` 这个字符串参数到底是怎么被决定的」这一直悬而未决的问题讲透。本讲**不**深入单个后端 kernel 的内部实现，只讲「选择」这一层。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**直觉一：同一算子的「多份实现」并存。** FlashInfer 不是「一个注意力 kernel 走天下」，而是对同一个数学操作（缩放点积注意力）维护了多套实现：

| 后端名 | 全称 / 来源 | 大致适用范围 |
|--------|------------|--------------|
| `fa2` | FlashAttention-2（FlashInfer 自研 CUDA kernel） | 全架构通用兜底，SM7.5 起都可用 |
| `fa3` | FlashAttention-3（基于 CUTLASS，Hopper 特化） | 仅 SM90a（Hopper），首推 prefill |
| `cudnn` | NVIDIA cuDNN 的 SDPA（FMHA）图后端 | 需安装 `cudnn` python 包，仅 NHD 布局 |
| `trtllm-gen` | TensorRT-LLM 生成式 kernel | 特定 decode / prefill 形状 |
| `cute-dsl` | CuTe DSL（CUTLASS Python DSL）kernel | Blackwell（SM100+）GQA decode 等 |

为什么要这么多份？因为不同 GPU 架构（Turing→Ampere→Hopper→Blackwell）的 tensor core 指令集差异巨大，没有一份 kernel 能在所有卡上都最优；同时不同后端的「功能覆盖面」也不同（比如 cuDNN 对 bias/pdl 支持得比 cuBLASLt 好）。FlashInfer 的策略是：**先判断硬件与参数能不能用某个后端，再在其中选优。**

**直觉二：「能力查询」与「实际选择」是两件事。** 一个后端能不能跑，取决于两道闸门：

1. **算力（compute capability）**：kernel 用到的指令是否被这块 GPU 支持。例如 FA3 用到 Hopper 的 warpgroup MMA（`wgmma`）与张量内存加速器（TMA），这些只在 SM90a 存在。
2. **参数兼容性**：dtype、head_dim、是否自定义掩码、位置编码模式等，是否落在该后端实例化过的范围内。

**直觉三：`"auto"` 只在 fa2/fa3 之间二选一。** 这是初学者最容易误解的一点：把 `backend="auto"` 传给注意力 wrapper，并不会自动落到 cuDNN 或 trtllm-gen。`"auto"` 内部只调用 `determine_attention_backend`，它的返回值只有 `"fa3"` 或 `"fa2"` 两个字面量。cuDNN、trtllm-gen、cute-dsl 都是**用户显式 opt-in** 的，必须由调用者写明 `backend="cudnn"` 才会启用。

> 补充一个术语：`compute capability`（计算能力）是 NVIDIA 给每代 GPU 的版本号，写作 `(major, minor)`，如 Hopper 是 `(9, 0)`、Blackwell B200 是 `(10, 0)`。本讲里常把它折算成整数 `major*10+minor`（如 90、100），便于在集合里比较。后缀 `a`/`f` 表示「架构特性子集」（`sm_90a` 含 wgmma/TMA，`sm_90` 不含），FA3 的 Hopper kernel 必须编成 `sm_90a`。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [flashinfer/utils.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/utils.py) | 后端选择的「大脑」：`determine_attention_backend`、`is_sm90a_supported`、`is_fa3_backend_supported`，以及 `@backend_requirement` / `@supported_compute_capability` 装饰器全在此 |
| [flashinfer/decode.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/decode.py) | decode wrapper 在 `plan` 阶段把 `"auto"` 解析成具体后端的调用点 |
| [flashinfer/prefill.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/prefill.py) | `single_prefill_with_kv_cache` 与 batch prefill wrapper 的 auto 解析、cuDNN 布局断言 |
| [flashinfer/cudnn/decode.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/cudnn/decode.py) | cuDNN decode 后端：用 cuDNN frontend 构造计算图 |
| [flashinfer/cudnn/prefill.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/cudnn/prefill.py) | cuDNN prefill 后端入口 `cudnn_batch_prefill_with_kv_cache` |
| [csrc/cudnn_sdpa_kernel_launcher.cu](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/csrc/cudnn_sdpa_kernel_launcher.cu) | cuDNN SDPA 的 C++ 启动器，从 cubin 加载并执行 |
| [flashinfer/gemm/gemm_base.py](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/gemm/gemm_base.py) | `@backend_requirement` 的最佳范例（`mm_bf16`），用于对照注意力路径 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**4.1 后端派发（`determine_attention_backend`）** 讲注意力的 fa2/fa3 自动选择；**4.2 `@backend_requirement` 装饰器** 讲新式 API 的统一校验机制；**4.3 cuDNN 后端** 讲这个「不写 kernel 只构图」的特殊后端。

### 4.1 后端派发：determine_attention_backend

#### 4.1.1 概念说明

注意力的 `backend="auto"` 需要一个**确定性的、纯函数式的**启发式：给定「设备 + 一组编译期参数」，立刻回答该用 fa2 还是 fa3。这个启发式就是 `determine_attention_backend`。

它的设计目标是「能上 FA3 就上 FA3，否则兜底 fa2」。原因有二：

- FA3 是 Hopper 特化的高性能实现，prefill 阶段（compute-bound）收益最大；
- 但 FA3 的覆盖面比 fa2 窄——某些 dtype、位置编码、掩码组合它没实例化，此时必须退回 fa2。

所以这个函数本质是一道「FA3 可用性闸门」：通过则 fa3，否则 fa2。

#### 4.1.2 核心流程

用伪代码描述 `determine_attention_backend` 的判定链：

```
function determine_attention_backend(device, pos_enc, fp16_qk_red, custom_mask, dtype_q, dtype_kv, head_dim_qk?, head_dim_vo?):
    if is_sm90a_supported(device)                          # 闸门1：硬件 = Hopper 且 CUDA≥12.3
       and is_fa3_backend_supported(pos_enc, fp16_qk_red,  # 闸门2：参数兼容
                                    custom_mask, dtype_q, dtype_kv):
        if head_dim_qk is None or head_dim_vo is None:
            return "fa3"
        if is_fa3_prefill_head_dim_supported(head_dim_qk, head_dim_vo):  # 闸门3：head_dim 实例化
            return "fa3"
    return "fa2"
```

三道闸门，层层收紧：

1. **硬件闸门** `is_sm90a_supported`：必须是 Hopper（major==9）且 CUDA ≥ 12.3。这一关过了，才说明这块卡「有可能」跑 FA3。
2. **参数闸门** `is_fa3_backend_supported`：检查 pos 编码、自定义掩码、fp16 qk reduction、FP8 KV 等 FA3 当前不支持或有限支持的特性。
3. **head_dim 闸门** `is_fa3_prefill_head_dim_supported`：FA3 的 Hopper prefill kernel 只为少数 head_dim 组合实例化过模板，不在表内的组合仍要退回 fa2。

注意一个细节：当 `head_dim_qk/head_dim_vo` 都为 `None`（即调用者没传 head_dim 信息）时，跳过第三道闸门直接返回 fa3。这通常发生在「还没拿到具体形状、只做粗判」的场合。decode 阶段因为 head_dim 固定且 q/k/v head_dim 相等，第三道闸门几乎总能过。

#### 4.1.3 源码精读

先看主函数本体（[flashinfer/utils.py:483-538](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/utils.py#L483-L538)）。注意它的返回值只有两个字面量 `"fa3"` 与 `"fa2"`，**绝不**返回 cudnn/trtllm-gen：

```python
def determine_attention_backend(
    device, pos_encoding_mode, use_fp16_qk_reductions, use_custom_mask,
    dtype_q, dtype_kv, *, head_dim_qk=None, head_dim_vo=None,
) -> str:
    if is_sm90a_supported(device) and is_fa3_backend_supported(
        pos_encoding_mode, use_fp16_qk_reductions, use_custom_mask, dtype_q, dtype_kv,
    ):
        if head_dim_qk is None or head_dim_vo is None:
            return "fa3"
        if is_fa3_prefill_head_dim_supported(head_dim_qk, head_dim_vo):
            return "fa3"
    else:
        return "fa2"
    return "fa2"
```

> 这里有个易被忽略的控制流细节：`else` 分支（返回 `"fa2"`）挂在**外层 `if`** 上；而当外层 `if` 成立、但 `head_dim` 不被支持时，函数会「穿透」到最后一行的 `return "fa2"`。两条路径都正确兜底为 fa2。

再看硬件闸门 [is_sm90a_supported](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/utils.py#L572-L574) 与它依赖的 [get_compute_capability](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/utils.py#L264-L267)、[version_at_least](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/utils.py#L541-L544)：

```python
def get_compute_capability(device):
    if device.type != "cuda":
        raise ValueError("device must be a cuda device")
    return torch.cuda.get_device_capability(device.index)   # 返回 (major, minor)

def is_sm90a_supported(device):
    major, _ = get_compute_capability(device)
    return major == 9 and version_at_least(torch.version.cuda, "12.3")
```

这段代码回答了本讲标题里的问题：**为什么 FA3 需要 SM90a？** 函数名里的 `90a` 指的是 NVIDIA 编译目标 `sm_90a`——它是 Hopper 架构里**包含 warpgroup 矩阵乘（`wgmma`）与张量内存加速器（TMA）指令**的「特性超集」。FA3 的 CUTLASS kernel 重度依赖这两类指令做 async GEMM 与 block-level 异步访存，因此必须编成 `sm_90a`；而 `sm_90`（不含这些指令）的 kernel 跑不了 FA3。代码用 `major == 9` 近似「是 Hopper 即具备 sm_90a」（市售 Hopper 计算卡均满足），并叠加 `CUDA ≥ 12.3`——因为 FA3 依赖的 CUTLASS Hopper kernel 与 TMA 的工具链支持要 12.3 才齐备。

接着是参数闸门 [is_fa3_backend_supported](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/utils.py#L387-L432)。它逐条排除 FA3 当前不支持或受限支持的特性：

```python
def is_fa3_backend_supported(pos_encoding_mode, use_fp16_qk_reductions,
                             use_custom_mask, dtype_q, dtype_kv) -> bool:
    if use_custom_mask:                          # 自定义掩码 → 不支持
        return False
    if pos_encoding_mode != PosEncodingMode.NONE.value:   # 非 NONE 位置编码 → 不支持
        return False
    if use_fp16_qk_reductions:                   # fp16 qk reduction → 不支持
        return False
    # FP8 KV 当前必须配 FP8 Q
    if dtype_kv in {torch.float8_e4m3fn, torch.float8_e5m2} and dtype_q not in {
        torch.float8_e4m3fn, torch.float8_e5m2}:
        return False
    if dtype_kv == torch.uint8:                  # NVFP4 KV（uint8 打包）→ 不支持
        return False
    return True
```

其中 `PosEncodingMode` 是个枚举（[flashinfer/utils.py:32-35](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/utils.py#L32-L35)）：`NONE=0 / ROPE_LLAMA=1 / ALIBI=2`。FA3 只接受 `NONE`——也就是说，如果你想在 FlashInfer 内部用 FA3 跑带 RoPE 的 KV，得**先在 Python 侧把 RoPE 应用到 Q/K**，再以 `pos_encoding_mode=NONE` 调用，否则会被这条闸门挡下退回 fa2。这解释了为什么 `single_prefill_with_kv_cache` 单独导出了 `rope` 参数让你自己先做 RoPE。

最后是 head_dim 闸门 [is_fa3_prefill_head_dim_supported](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/utils.py#L435-L439)：

```python
def is_fa3_prefill_head_dim_supported(head_dim_qk, head_dim_vo):
    if head_dim_qk == head_dim_vo:
        return head_dim_qk in {64, 128, 256}
    return (head_dim_qk, head_dim_vo) == (192, 128)
```

FA3 的 Hopper prefill 模板只为「等长 64/128/256」或「QK=192, VO=128」（常见于 MLA/GQA 的混合 head_dim）实例化过。其余组合（如 head_dim=96）即使硬件达标，也只能退回 fa2。

**调用点：** `single_prefill_with_kv_cache` 在 auto 时无条件调用它（[flashinfer/prefill.py:1359-1369](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/prefill.py#L1359-L1369)），把 `q.shape[-1]` 与 `out_head_dim` 作为 head_dim 传入：

```python
if backend == "auto":
    backend = determine_attention_backend(
        q.device,
        PosEncodingMode[pos_encoding_mode].value,
        use_fp16_qk_reduction,
        packed_custom_mask is not None,   # use_custom_mask
        q.dtype, k.dtype,
        head_dim_qk=q.shape[-1],
        head_dim_vo=out_head_dim,
    )
```

而 decode wrapper 的标准路径更保守：**只在涉及 FP8 dtype 时**才走 `determine_attention_backend`，否则直接定 `"fa2"`（[flashinfer/decode.py:1450-1466](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/decode.py#L1450-L1466)）。这是因为 decode 是 memory-bound，FA3 在 decode 上的相对优势不如 prefill 明显，故默认 fa2、仅在 FP8 这种 fa2 不一定覆盖的场景才让启发式介入。

#### 4.1.4 代码实践

**目标：** 亲手调用 `determine_attention_backend`，观察不同 dtype/head_dim 组合下返回的后端字符串，从而把三道闸门「跑」一遍。

**操作步骤：**

1. 写一个最小脚本，直接 import 并调用（这是纯 CPU 端逻辑，不需要真正跑 kernel）：

```python
# 示例代码：仅供学习 determine_attention_backend 的返回值
import torch
from flashinfer.utils import (
    determine_attention_backend,
    is_sm90a_supported,
    get_compute_capability,
)
from flashinfer.utils import PosEncodingMode

dev = torch.device("cuda")
print("compute_capability =", get_compute_capability(dev))
print("sm90a_supported    =", is_sm90a_supported(dev))

cases = [
    # (label, pos_enc, fp16_red, custom_mask, dtype_q, dtype_kv, hd_qk, hd_vo)
    ("bf16 / hd128",       PosEncodingMode.NONE.value,     False, False, torch.bfloat16, torch.bfloat16, 128, 128),
    ("bf16 / hd96",        PosEncodingMode.NONE.value,     False, False, torch.bfloat16, torch.bfloat16,  96,  96),
    ("bf16 / MLA 192x128", PosEncodingMode.NONE.value,     False, False, torch.bfloat16, torch.bfloat16, 192, 128),
    ("bf16 + RoPE",        PosEncodingMode.ROPE_LLAMA.value, False, False, torch.bfloat16, torch.bfloat16, 128, 128),
    ("fp8 q+kv / hd128",   PosEncodingMode.NONE.value,     False, False, torch.float8_e4m3fn, torch.float8_e4m3fn, 128, 128),
    ("fp16 q / fp8 kv",    PosEncodingMode.NONE.value,     False, False, torch.float16,   torch.float8_e4m3fn, 128, 128),
]
for label, pe, red, cm, dq, dkv, hq, hv in cases:
    b = determine_attention_backend(dev, pe, red, cm, dq, dkv, head_dim_qk=hq, head_dim_vo=hv)
    print(f"{label:24s} -> {b}")
```

2. （可选）若你的 GPU 不是 Hopper，`is_sm90a_supported` 会返回 `False`，此时**所有**用例都会返回 `"fa2"`——这正好验证了「硬件闸门优先」。

**需要观察的现象：**

- 在 Hopper 卡上，`"bf16 / hd128"`、`"bf16 / MLA 192x128"`、`"fp8 q+kv / hd128"` 应返回 `"fa3"`。
- `"bf16 / hd96"` 因 head_dim 不在 `{64,128,256}` 也不等于 (192,128)，应返回 `"fa2"`（即便硬件达标）。
- `"bf16 + RoPE"` 因 `pos_encoding_mode != NONE`，应返回 `"fa2"`。
- `"fp16 q / fp8 kv"` 触发「FP8 KV 必须配 FP8 Q」规则，应返回 `"fa2"`。

**预期结果：** 一张「参数组合 → 后端」的对照表，让你直观看到三道闸门各自挡掉了哪些用例。

**待本地验证：** 上述各用例的确切返回值取决于你的 GPU 架构与 CUDA 版本；若不在 Hopper 上，请在脚本输出里确认 `sm90a_supported=False` 后，把所有 `"fa2"` 结果解释为「硬件闸门未过」。

#### 4.1.5 小练习与答案

**练习 1：** 假设你在 Hopper 上、`head_dim=256`、bf16、无 RoPE，但启用了自定义掩码（`packed_custom_mask` 非空）。`determine_attention_backend` 会返回什么？为什么？

> **答案：** 返回 `"fa2"`。原因是参数闸门 `is_fa3_backend_supported` 第一条 `if use_custom_mask: return False` 把 FA3 挡掉了。自定义掩码会走 fa2 的 `fa2_prefill_with_kv_cache` 路径（见 u3-l4）。

**练习 2：** 为什么 `determine_attention_backend` 的签名里 `head_dim_qk/head_dim_vo` 是「关键字参数且默认 None」，而 `dtype_q/dtype_kv` 是必填位置参数？

> **答案：** dtype 是「决定 kernel 能否实例化」的硬约束，必须有；而 head_dim 在某些调用点（如尚未拿到具体形状的粗判，或 decode 这种 head_dim 固定相等的场景）可能拿不到或不需要细查，故设计成可选——为 `None` 时直接跳过第三道闸门返回 fa3，体现「能上 FA3 就上」的宽松默认。

**练习 3：** decode 路径里，非 FP8 dtype 时为什么 `auto` 直接被写成 `"fa2"`，而不调 `determine_attention_backend`？

> **答案：** decode 是 memory-bound，FA3 在 decode 上的相对优势不如 prefill；同时 fa2 decode 覆盖面最广。所以默认 fa2，仅在 FP8 这种 fa2 decode 不一定覆盖的 dtype 场景才让启发式介入（见 [decode.py:1451-1466](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/decode.py#L1451-L1466)）。

---

### 4.2 @backend_requirement 装饰器与能力查询

#### 4.2.1 概念说明

`determine_attention_backend` 是注意力的「老式」启发式——一个手写的纯函数，写死在调用点。FlashInfer 后来为 GEMM/MoE/norm/通信等**新式 API** 引入了一套更通用、声明式的机制：`@backend_requirement` 装饰器。

它的核心思想是：**把「后端清单 + 每个后端的能力 + 每个后端的问题尺寸校验」用声明式的方式挂在函数上**，由装饰器统一在调用前做校验，并对外暴露 `is_backend_supported(backend, cc)` / `is_compute_capability_supported(cc)` 两个查询方法。

> 注意区分两套机制：**注意力 wrapper（decode/prefill）目前用的是 `determine_attention_backend`，并*没有*用 `@backend_requirement`**；而 GEMM（`mm_bf16`/`mm_fp8`/`mm_fp4`）、MoE、norm 等用的是 `@backend_requirement`。两者并存，新代码倾向后者。这一点很容易混淆，记住「注意力走老路、其余走新路」即可。

#### 4.2.2 核心流程

`@backend_requirement` 的工作链：

```
装饰阶段：
  @backend_requirement(
      backend_checks = { "cudnn": _cudnn_check, "cutlass": _cutlass_check, ... },  # 每后端的 checker
      common_check   = _common_size_check,        # 所有后端共有的问题尺寸校验（可选）
      heuristic_func = _heuristic_func,           # backend=="auto" 时的排序启发式（可选）
  )
  def api(...): ...

  每个 checker 通常先被 @supported_compute_capability([80,86,90,...]) 标注它支持哪些 CC。

调用阶段（wrapper）：
  1. skip_check = kwargs.pop("skip_check", False)        # 性能热路径可跳过校验
  2. 若不跳过：绑定参数、补默认值，从第一个 torch.Tensor 参数推断 capability
  3. 若 backend == "auto"：调 suitable_auto_backends(capability, ...)
       - 逐后端：checker(args)==True 且 checker.is_compute_capability_supported(cc)
       - 再用 heuristic_func 把幸存后端排序，取首个（或交由 autotune 遍历）
     否则：校验该 backend 是否在表中、是否支持当前 cc、问题尺寸是否 OK
  4. 任一校验失败 → 抛 BackendSupportedError / ValueError
  5. 通过 → 执行原函数
```

对外暴露的能力查询：

- `api.is_backend_supported(backend, cc=None)`：该后端是否存在；给了 `cc` 还顺带查它是否支持该算力。
- `api.is_compute_capability_supported(cc)`：任意后端支持该 cc 即返回 True。

#### 4.2.3 源码精读

先看「标注能力」的轻量装饰器 [supported_compute_capability](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/utils.py#L930-L1010)。它只是给函数挂一个 `_supported_ccs` 集合属性和一个 `is_compute_capability_supported` 方法：

```python
def supported_compute_capability(supported_ccs):
    ...
    def decorator(func):
        func._supported_ccs = set(validated_ccs)
        def is_cc_supported(cc):
            return cc in func._supported_ccs
        func.is_compute_capability_supported = is_cc_supported
        return func
    return decorator
```

注意 `cc` 用整数 `major*10+minor`（如 SM9.0 = 90）。这正是后续查询时 `_get_capability` 算出来的形式（[utils.py:1209-1226](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/utils.py#L1209-L1226)）：从第一个 `torch.Tensor` 参数取 device，再 `major*10+minor`。

再看 [backend_requirement](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/utils.py#L1013-L1295) 内部两个关键闭包。**能力查询** [is_backend_supported](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/utils.py#L1124-L1141)：

```python
def is_backend_supported(backend, cc=None):
    if not has_backend_choices():
        raise ValueError(...)
    if backend not in backend_checks:
        return False
    req_checker = backend_checks[backend]
    if cc is None:
        return True                                  # 只问"有没有这个后端"
    elif hasattr(req_checker, "is_compute_capability_supported"):
        return req_checker.is_compute_capability_supported(cc)   # 连算力一起查
    return False
```

**auto 选择** [suitable_auto_backends](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/utils.py#L1187-L1207)——它把「问题尺寸」与「算力」两道闸门同时施加，再用启发式排序：

```python
def suitable_auto_backends(cc, *args, **kwargs):
    if common_check is not None and not common_check(*args, **kwargs):
        return False
    suitable_backends = []
    for backend in backend_checks:
        req_checker = backend_checks[backend]
        try:
            if req_checker(*args, **kwargs) and req_checker.is_compute_capability_supported(cc):
                suitable_backends.append(backend)
        except ValueError:
            continue
    assert heuristic_func is not None
    suitable_backends = heuristic_func(suitable_backends, *args, **kwargs)  # 排序
    ...
```

这里能清楚看到两道闸门如何叠加：`req_checker(*args)` 查问题尺寸（如 `q.shape[-1] <= 256`），`req_checker.is_compute_capability_supported(cc)` 查算力。两者都过，后端才进入候选；最后由 `heuristic_func` 排序。

**最佳范例：`mm_bf16`**（[flashinfer/gemm/gemm_base.py:517-528](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/gemm/gemm_base.py#L517-L528)）声明了 6 个后端、一个公共问题尺寸校验、一个启发式：

```python
@backend_requirement(
    {
        "cudnn":   _cudnn_mm_bf16_requirement,
        "cutlass": _cutlass_mm_bf16_requirement,
        "tgv":     _tgv_gemm_requirement,
        "cublaslt":_cublaslt_mm_bf16_requirement,
        "tinygemm":_tinygemm_mm_bf16_requirement,
        "cutile":  _cutile_mm_bf16_requirement,
    },
    common_check=_check_mm_bf16_problem_size,
    heuristic_func=_heuristic_func_mm_bf16,
)
def mm_bf16(a, b, ...): ...
```

而它的启发式 [_heuristic_func_mm_bf16](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/gemm/gemm_base.py#L482-L514) 会根据「有没有 bias/pdl」改变偏好——例如带 bias 时优先 `tgv`/`cudnn`，因为 `cutlass`/`cublaslt` 不支持 bias。这正是「同一算子、多后端、按场景选优」的工程化体现。

`@backend_requirement` 还往 wrapper 上挂了 `has_backend` / `has_backend_choices` 等查询方法（[utils.py:1289-1293](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/utils.py#L1289-L1293)），并把 `skip_check` 作为隐式关键字参数注入（[utils.py:1234](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/utils.py#L1234)），让性能敏感的热路径能 `mm_bf16(a, b, skip_check=True)` 绕过校验开销。

#### 4.2.4 代码实践

**目标：** 用 `@backend_requirement` 暴露的查询方法，**在不真正发起 GEMM 计算的前提下**，枚举 `mm_bf16` 在你机器上支持哪些后端 / 哪些算力。

**操作步骤：**

```python
# 示例代码：仅做能力查询，不触发 kernel
import flashinfer

api = flashinfer.gemm.mm_bf16   # 被 @backend_requirement 装饰过的函数对象
print("has backend choices:", api.has_backend_choices())
for cc in [75, 80, 86, 89, 90, 100, 120]:
    print(f"  cc={cc:>3} supported={api.is_compute_capability_supported(cc)}")
for b in ["cudnn", "cutlass", "tgv", "cublaslt", "tinygemm", "cutile", "nonsense"]:
    print(f"  backend={b:9s} present={api.has_backend(b)} "
          f"cc90_ok={api.is_backend_supported(b, 90)}")
```

**需要观察的现象：**

- `is_compute_capability_supported(90)` 在 `[80,86,87,89,90,100,103,110,120,121]` 这类 checker（见 [gemm_base.py:306](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/gemm/gemm_base.py#L306)）覆盖下应返回 True，而 `cc=75` 可能因没有任何后端 checker 含 75 而返回 False。
- `is_backend_supported("nonsense", 90)` 应返回 False（后端名不在表中）；`is_backend_supported("cudnn")`（不传 cc）应返回 True。

**预期结果：** 一张「后端 × 算力」的支持矩阵，让你理解装饰器如何把声明式元数据转成运行期查询。

**待本地验证：** 具体哪些 cc 返回 True 取决于当前各 `@supported_compute_capability` 标注；可用 `inspect.getsource(api)` 对照确认。

#### 4.2.5 小练习与答案

**练习 1：** 对比 `determine_attention_backend` 与 `@backend_requirement`，各自把「选后端」的逻辑放在哪里？

> **答案：** `determine_attention_backend` 是一个**显式的纯函数**，逻辑写在调用点（decode.py / prefill.py 里直接 `if backend == "auto": backend = determine_attention_backend(...)`）；`@backend_requirement` 则把后端清单、能力、问题尺寸校验、启发式**声明式地挂在被装饰函数上**，由装饰器 wrapper 统一在调用前执行，调用点本身看不到选择逻辑。

**练习 2：** `suitable_auto_backends` 里为什么对每个 `req_checker` 用 `try/except ValueError: continue`？

> **答案：** 因为 checker 在判断「问题尺寸是否支持」时，可能对某些极端形状直接抛 `ValueError`（而非返回 False）。`continue` 让一个后端的异常不至于拖垮整个 auto 选择，而是把它当作「不合适」跳过，继续评估其余后端。

**练习 3：** 如果一个 API 想完全跳过 `@backend_requirement` 的校验开销，该怎么做？为什么 `skip_check=True` 时仍可能调用 `suitable_auto_backends`？

> **答案：** 传 `skip_check=True`。但即便跳过，若 `backend=="auto"` 且提供了 `heuristic_func`，wrapper 仍会调用 `suitable_auto_backends`（[utils.py:1281-1285](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/utils.py#L1281-L1285)）——因为 auto 模式**必须**先算出候选后端列表（写入 `wrapper.suitable_auto_backends`），后续才能真正决定调用哪个后端实现，这部分无法省略。

---

### 4.3 cuDNN 后端：不写 kernel，只构图

#### 4.3.1 概念说明

前两个模块讲的都是「在已有 kernel 之间选」。cuDNN 后端则不同：FlashInfer **没有为它写 CUDA kernel**，而是把注意力计算描述成一张 **cuDNN 计算图（SDPA / FMHA graph）**，交给 NVIDIA cuDNN 的 frontend 去编译和执行。

这带来几个特点：

- **依赖外部 `cudnn` python 包**：import 失败时整个 cuDNN 后端不可用（`CUDNN_AVAILABLE=False`）。
- **必须显式 opt-in**：用户要写 `backend="cudnn"`，`"auto"` 永远不会落到它（回顾 4.1：auto 只产 fa2/fa3）。
- **布局受限**：cuDNN 后端只接受 NHD 布局。
- **执行模型不同**：用 `@cudnn.jit` + `@cudnn.graph_cache` 做「按形状缓存计算图」，而不是 FlashInfer 自己的 JIT/ninja 那一套。

#### 4.3.2 核心流程

cuDNN 后端的执行链：

```
1. import cudnn（pycuDNN frontend）；失败则 CUDNN_AVAILABLE=False，后端整体不可用
2. _create_cudnn_handle(stream)：惰性创建全局 cuDNN handle 并绑定到当前 stream
3. 用 UID（唯一标识）把 Q/K/V/O 等张量"登记"进 cuDNN 图：
     Q_UID=1, K_UID=2, V_UID=3, O_UID=1000, ...
4. _build_decode_graph / _build_prefill_graph：
     - 用 @cudnn.jit(heur_modes=[A]) 让 cuDNN 选 kernel
     - 用 @cudnn.graph_cache(key_fn=...) 按 (形状) 缓存编译好的图
5. 运行时把真实张量喂给图、执行
```

对应到 wrapper：batch prefill wrapper 在构造时检查 `backend=="cudnn"` 并断言 NHD 布局（[flashinfer/prefill.py:1682-1683](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/prefill.py#L1682-L1683)）：

```python
if backend == "cudnn":
    assert kv_layout == "NHD", "CUDNN backend only supports NHD layout"
```

#### 4.3.3 源码精读

先看 cuDNN decode 后端的依赖检测与 handle 管理（[flashinfer/cudnn/decode.py:10-27](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/cudnn/decode.py#L10-L27)）：

```python
try:
    import cudnn
    CUDNN_AVAILABLE = True
except ImportError:
    cudnn = None
    CUDNN_AVAILABLE = False

_cudnn_handle = None

def _create_cudnn_handle(stream):
    global _cudnn_handle
    if _cudnn_handle is None:
        _cudnn_handle = cudnn.create_handle()
    cudnn.set_stream(_cudnn_handle, stream.cuda_stream)
    return _cudnn_handle
```

注意它是**全局单例 handle**（注释也写明「need to make it per device in future」）——多设备场景目前是个已知简化。

接着是 UID 枚举（[flashinfer/cudnn/decode.py:31-50](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/cudnn/decode.py#L31-L50)），把每个张量角色编成稳定整数，cuDNN 图内部靠 UID 引用张量而非指针：

```python
class UIDs(Enum):
    Q_UID = 1;  K_UID = 2;  V_UID = 3
    ACTUAL_SEQ_LENS_Q_UID = 100;  ACTUAL_SEQ_LENS_KV_UID = 101
    BLOCK_TABLES_UID = 200
    O_UID = 1000;  STATS_UID = 1001
    ...
```

图的构建用 cuDNN frontend 的两个装饰器（[flashinfer/cudnn/decode.py:77-79](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/cudnn/decode.py#L77-L79)）：

```python
@cudnn.jit(heur_modes=[cudnn.heur_mode.A])
@cudnn.graph_cache(key_fn=_sdpa_decode_key_fn)
def _build_decode_graph(q, k_cache, v_cache, scale, *, max_sequence_kv, ...): ...
```

- `@cudnn.jit(heur_modes=[A])`：让 cuDNN 用启发式 A 为这张图挑底层 kernel（cuDNN 自己的 kernel 库，不是 FlashInfer 的）。
- `@cudnn.graph_cache(key_fn=_sdpa_decode_key_fn)`：按 `key_fn` 返回的键缓存编译结果，避免同形状重复编译——这与 FlashInfer 的两级缓存（u2-l5）异曲同工，只是托管在 cuDNN frontend 内。`_sdpa_decode_key_fn`（[cudnn/decode.py:53-72](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/cudnn/decode.py#L53-L72)）用 `("decode", max_sequence_kv, q.shape, k_cache.shape)` 当键。

prefill 后端的入口是 [cudnn_batch_prefill_with_kv_cache](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/cudnn/prefill.py#L563)，结构与 decode 对称（同样 `import cudnn`、全局 handle、UID 枚举、`@cudnn.jit`+`graph_cache` 的 `_build_prefill_graph`）。

最后，C++ 侧还有一个 cuDNN SDPA 启动器 [csrc/cudnn_sdpa_kernel_launcher.cu](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/csrc/cudnn_sdpa_kernel_launcher.cu)，它走的是另一条「cubin 加载」路线：通过 `CUDNN_SDPA_CUBIN_PATH` 宏拿到预编译 cubin 路径，用 FlashInfer 的 `cubin_loader`（u9-l4 会讲）加载执行（[cudnn_sdpa_kernel_launcher.cu:32-43](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/csrc/cudnn_sdpa_kernel_launcher.cu#L32-L43)）。也就是说，cuDNN 后端在 Python frontend 之外，还有一条 C++ + cubin 的实现分支，两者都是「不手写 attention kernel、复用 NVIDIA 提供的实现」。

#### 4.3.4 代码实践

**目标：** 把 cuDNN 后端「显式 opt-in + 布局受限」两件事跑通，并对照 fa2 结果验证正确性。

**操作步骤：**

```python
# 示例代码：在 Hopper/Ampere 上对照 fa2 与 cudnn
import torch, flashinfer

B, H, D, S = 4, 8, 128, 512
q = torch.randn(B, S, H, D, dtype=torch.bfloat16, device="cuda")
k = torch.randn(B, S, H, D, dtype=torch.bfloat16, device="cuda")
v = torch.randn(B, S, H, D, dtype=torch.bfloat16, device="cuda")

# 注意：cudnn 只支持 NHD 布局；fa2 对照
o_fa2 = flashinfer.single_prefill_with_kv_cache(q, k, v, backend="fa2", causal=True)

try:
    o_cudnn = flashinfer.single_prefill_with_kv_cache(q, k, v, backend="cudnn", causal=True)
    print("cudnn vs fa2 max abs diff =", (o_fa2 - o_cudnn).abs().max().item())
except Exception as e:
    print("cudnn backend 不可用或参数受限:", type(e).__name__, e)
```

**需要观察的现象：**

- 若未安装 `cudnn` 包或当前架构/dtype 不被 cuDNN 支持，会抛异常——这与 4.3.1 的「依赖外部包」一致。
- 若两者都可用，最大绝对差应在 bf16 数值精度量级（约 1e-1 ~ 1e-2 量级，取决于数值规模）。
- 尝试把 q/k/v 改成 HND 布局再传 `backend="cudnn"`，应触发 NHD 断言失败。

**预期结果：** 验证「cuDNN 后端结果与 fa2 数值一致」，并确认它对布局/dtype 的硬约束。

**待本地验证：** cuDNN 后端是否可用取决于是否安装 `cudnn` python 包及 GPU 架构；若无 cuDNN，请把异常信息当作「该后端在本机不可用」的证据来分析，而不是 kernel bug。

#### 4.3.5 小练习与答案

**练习 1：** 为什么 `backend="auto"` 永远不会选中 cuDNN？如果想用 cuDNN，调用者要怎么做？

> **答案：** 因为 `determine_attention_backend` 的返回值只有 `"fa3"`/`"fa2"`（见 4.1.3），auto 解析只调它。cuDNN 是显式 opt-in 后端，调用者必须写明 `backend="cudnn"`（batch prefill wrapper 在 [prefill.py:1682](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/prefill.py#L1682) 处据此分支处理）。

**练习 2：** cuDNN 后端没有 FlashInfer 自己的 `.cuh` kernel，它靠什么机制「缓存编译结果」以避免重复开销？

> **答案：** 靠 cuDNN frontend 的 `@cudnn.graph_cache(key_fn=...)`，按 `key_fn` 返回的（操作类型+形状）键缓存已编译的计算图（[cudnn/decode.py:78](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/cudnn/decode.py#L78)）。这与 FlashInfer 自家两级缓存（u2-l5）目标一致——摊薄重复编译——但托管在 cuDNN 内部，而非 FlashInfer 的 `~/.cache/flashinfer`。

**练习 3：** cuDNN 后端为什么强制要求 NHD 布局？

> **答案：** 因为 cuDNN SDPA 图对 K/V cache 的内存布局有固定假设（[prefill.py:1683](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/prefill.py#L1683) 的断言）。FlashInfer 自家 fa2/fa3 kernel 在 C++ 模板里同时支持 NHD/HND 两种 stride（见 u3-l2），但 cuDNN 这条「借来的」路径只接受其原生布局，故用断言把不兼容的 HND 直接挡在门外，避免运行期拿到错误结果。

## 5. 综合实践

**任务：** 为你的 GPU 绘制一张「注意力后端可用性地图」，把本讲三个模块串起来。

1. **查硬件基线**：用 `get_compute_capability` / `is_sm90a_supported` 打印你的 (major, minor) 与是否支持 FA3 所需的 sm_90a。
2. **跑启发式表**：用 4.1.4 的脚本，把「dtype × head_dim × pos_enc × custom_mask」组合的 `determine_attention_backend` 返回值列成表，标出每一行是被「硬件闸门 / 参数闸门 / head_dim 闸门」中哪一道挡下而退回 fa2 的。
3. **验证 auto 与显式等价**：挑一个返回 `"fa3"` 的组合，分别用 `single_prefill_with_kv_cache(..., backend="auto")` 与 `backend="fa3"` 各跑一次（小规模 bf16），核对输出一致——证明 wrapper 的 auto 解析确实 faithfully 调用了 `determine_attention_backend`。
4. **能力查询迁移**：对 `flashinfer.mm_bf16` 用 4.2.4 的查询脚本，对比「注意力的老式启发式」与「GEMM 的声明式 `@backend_requirement`」在 API 风格上的差异，写一段话说明你更倾向哪种、为什么。
5. **（可选）cuDNN 对照**：若机器上有 `cudnn` 包，对同一组 q/k/v 用 `backend="cudnn"` 与 `backend="fa3"` 各跑一次，记录数值差与耗时差。

**产出：** 一份 Markdown 小报告，包含硬件信息表、启发式结果表、auto/fa3 等价性核对、两套后端机制的风格对比。这份报告将直接服务于第 4 单元（MLA/Cascade 等进阶注意力变体都会复用本讲的后端选择结论）。

## 6. 本讲小结

- FlashInfer 对同一注意力算子维护 fa2/fa3/cudnn/trtllm-gen/cute-dsl 等多份后端，按硬件与参数选优；`"auto"` 仅在 fa2/fa3 间二选一，cuDNN/trtllm-gen/cute-dsl 需用户显式 opt-in。
- `determine_attention_backend` 是注意力的老式启发式，设三道闸门：硬件（`is_sm90a_supported`，要求 major==9 且 CUDA≥12.3）→ 参数（`is_fa3_backend_supported`）→ head_dim（`is_fa3_prefill_head_dim_supported`，仅 64/128/256 或 192×128）；过则 fa3，否则 fa2。
- FA3 必须 SM90a：它依赖 Hopper 的 `wgmma` 与 TMA 指令，对应编译目标 `sm_90a`，且工具链需 CUDA≥12.3。
- `@backend_requirement` + `@supported_compute_capability` 是新式 API（GEMM/MoE/norm/comm）的声明式后端校验机制，提供 `is_backend_supported` / `is_compute_capability_supported` 能力查询与 auto 启发式排序；注意力 wrapper 当前仍用老式 `determine_attention_backend`。
- cuDNN 后端不写 kernel，而是用 cuDNN frontend 构造计算图（`@cudnn.jit` + `@cudnn.graph_cache`），依赖外部 `cudnn` 包、只支持 NHD 布局；C++ 侧另有一条 cubin 加载分支（`csrc/cudnn_sdpa_kernel_launcher.cu`）。
- 能力查询与实际选择是两件事：前者回答「能不能」，后者（启发式或装饰器 wrapper）回答「用哪个」。

## 7. 下一步学习建议

- **进入第 4 单元「进阶注意力变体」**：u4-l1（MLA）会复用本讲的后端选择结论——`determine_mla_backend`（[utils.py:637-638](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/utils.py#L637-L638)）正是 `determine_attention_backend` 的 MLA 特化版，思路完全一致。
- **深入 GEMM 的多后端**：第 5 单元 u5-l1 会把本讲的 `@backend_requirement` 范例（`mm_bf16`）展开，建议先重读 [gemm_base.py:482-528](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/flashinfer/gemm/gemm_base.py#L482-L528) 的启发式函数，理解 bias/pdl 如何改变后端偏好。
- **想要新增自己的后端**：参考 u9-l1（添加新 CUDA 算子）与 u9-l3（Jinja 与分发宏），看新后端如何接入 `determine_attention_backend` 或 `@backend_requirement` 体系。
- **想看 cuDNN 后端的 C++ 侧**：直接读 [csrc/cudnn_sdpa_kernel_launcher.cu](https://github.com/flashinfer-ai/flashinfer/blob/a25af45eca5a5a7e40848d69e60609edb6f3b4ec/csrc/cudnn_sdpa_kernel_launcher.cu)，并配合 u9-l4（cubin 加载机制）理解 `CUDNN_SDPA_CUBIN_PATH` 的来源。
