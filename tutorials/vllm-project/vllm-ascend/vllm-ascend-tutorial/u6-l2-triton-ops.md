# Triton 算子集

## 1. 本讲目标

本讲是「自定义算子三层体系」的第二层——Triton 算子层。在 [u6-l1](u6-l1-python-custom-ops.md) 里我们已经看到，vllm-ascend 把 NPU 专属逻辑封装成 `torch.ops.vllm.<name>` 自定义算子，并用 `direct_register_custom_op` 注册。本讲聚焦这些算子里**用 Triton 语言实现的那一批内核**，集中在 [`vllm_ascend/ops/triton/`](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ops/triton) 目录下。

读完本讲，你应当能够：

1. 说清 vllm-ascend **为什么**要用 Triton 重写一批算子，而不是直接用 PyTorch 或 C++/AscendC。
2. 理解 Triton 在昇腾 NPU 上的**运行模型**：vector core / AI core 划分、Unified Buffer（UB）约束、以及 `triton_utils.py` 提供的公共工具。
3. 精读**融合算子** `split_qkv_rmsnorm_rope`，说清它把哪几个上游算子融成了一个 kernel、为什么这样做能省访存。
4. 了解 `fla/`（线性注意力 chunk）、`mamba/`（lightning attention）、`kda/`（kernelized delta attention）等**子算子集**各自的职责，以及它们和 C++/AscendC 内核（u6-l3）的协作方式。

## 2. 前置知识

- **Triton 是什么**：一种用 Python 写 GPU/NPU kernel 的高级语言。你写一段带 `@triton.jit` 装饰器的函数，Triton 编译器把它编译成硬件指令。它的核心抽象是「块（block/tile）」：一次处理一个 `[BLOCK_M, BLOCK_N]` 的小矩阵，让你既不用手写汇编，又能控制内存层级。在昇腾上，Triton 由 `triton-ascend` 后端编译为 CANN 指令。
- **为什么要融合（fusion）**：访存（读写显存，Global Memory, GM）往往比计算慢得多。把「归一化 → 缩放 → 旋转」等多个小算子合并成一个大算子，中间结果就可以留在片上高速缓存里，避免反复读写 GM。这是本讲反复出现的收益来源。
- **昇腾 NPU 的硬件结构**：每张卡上有两种算力核——**AI core（cube，矩阵核）**负责大规模矩阵乘，**vector core（向量核）**负责逐元素、归约等向量运算。Triton-ascend 暴露了 `num_aicore` / `num_vectorcore` 两个数量供 kernel 划分任务。片上还有一块很小的**统一缓冲（Unified Buffer, UB）**（约 192KB，代码里常用 ~85KB 作安全预算），kernel 里同时驻留的中间张量必须塞进 UB。
- **承接 u6-l1**：本讲涉及的算子大多仍走 `direct_register_custom_op` 的「impl / fake / register / wrap」四段式，且在 `NPUWorker.__init__` 触发的 `ops.__init__` import 副作用里注册；区别只在于 impl 用 Triton 实现。先复习 [u6-l1](u6-l1-python-custom-ops.md) 的算子注册四段式再读本讲会更顺。

## 3. 本讲源码地图

| 文件 / 目录 | 作用 |
| --- | --- |
| [vllm_ascend/ops/triton/triton_utils.py](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ops/triton/triton_utils.py) | Triton 公共工具：探测核数、解析昇腾专属算子（`insert_slice`/`extract_slice`/`get_element`）。 |
| [vllm_ascend/ops/triton/rms_norm.py](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ops/triton/rms_norm.py) | 单算子 RMSNorm 内核 `triton_q_rms`，用于 DSA 注意力的 Q 归一化。 |
| [vllm_ascend/ops/triton/linearnorm/split_qkv_rmsnorm_rope.py](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ops/triton/linearnorm/split_qkv_rmsnorm_rope.py) | **本讲主角**：融合 split + Q/K RMSNorm + Q/K RoPE 的大算子。 |
| [vllm_ascend/ops/triton/linearnorm/split_qkv_rmsnorm_rope_simt.py](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ops/triton/linearnorm/split_qkv_rmsnorm_rope_simt.py) | 上一算子的 SIMT（纯向量核）变体，A5 芯片专用。 |
| [vllm_ascend/ops/triton/fla/chunk.py](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ops/triton/fla/chunk.py) | 线性注意力 gated delta rule 的 chunk 实现（Triton + AscendC 混合）。 |
| [vllm_ascend/ops/triton/mamba/lightning_attn.py](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ops/triton/mamba/lightning_attn.py) | Lightning attention（BailingMoE/MiniMax 线性注意力）NPU 内核。 |
| vllm_ascend/ops/triton/kda/ | Kernelized Delta Attention（KDA）线性注意力子集，含 chunk 与 fused recurrent 两种实现。 |
| [vllm_ascend/ops/triton/rope.py](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ops/triton/rope.py) | 独立的 RoPE 内核（供未融合路径使用）。 |
| [vllm_ascend/ops/__init__.py](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ops/__init__.py) | import 副作用注册点：在 `HAS_TRITON` 保护下 import 各融合算子。 |
| [vllm_ascend/device/device_op.py](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/device/device_op.py) | `DeviceOperator` 按芯片型号在 `qkv_rmsnorm_rope` 与 `..._simt` 间分发。 |
| [vllm_ascend/compilation/passes/qknorm_rope_fusion_pass.py](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/compilation/passes/qknorm_rope_fusion_pass.py) | torch.fx 融合 pass：把「split+norm+rope」子图替换为本算子（连接 u8-l2）。 |

## 4. 核心概念与源码讲解

### 4.1 Triton 内核总览与 Ascend 运行模型

#### 4.1.1 概念说明

vllm-ascend 用 Triton 重写算子，动机集中在三点：

1. **融合省访存**：把一串小算子合并成一个 kernel，中间张量留在 UB 里，少读写 GM。这是 `split_qkv_rmsnorm_rope` 的核心收益。
2. **填补算子空白**：上游 vLLM 的某些高性能 kernel（如 flash-linear-attention 的 gated delta rule、MiniMax 的 lightning attention）是 **GPU/CUDA 专属**的，NPU 没有 CANN 等价算子。vllm-ascend 用 Triton 为它们提供 NPU 版本，否则对应模型（Gated Delta Rule 混合模型、MiniMax/BailingMoE）根本跑不起来。
3. **适配与精度**：有些上游算子在昇腾上精度不对或性能差，用 Triton 重写一份「对齐版」（例如 `fa3` 仅用于训练-推理数值对齐，见 u5-l2）。

**为什么是 Triton 而不是全用 C++/AscendC？** 因为 Triton 写起来快、可读、易迭代——一个文件几十行就能表达一个 kernel，且能在 vector / cube 两类核上自动调度。只有当 Triton 表达不了或性能不够（如大规模递归状态 `h` 的计算）时，才下沉到 u6-l3 的 AscendC C++ 内核。本讲的 `fla/chunk.py` 正是这种「Triton + AscendC 混合」的典型。

`ops/triton/` 目录按算子家族分子目录：

| 子目录 / 文件 | 家族 | 代表场景 |
| --- | --- | --- |
| `linearnorm/` | QKV + 归一化 + 位置编码融合 | 标准 Transformer 注意力层前处理 |
| `fla/` | Flash Linear Attention（gated delta rule） | 线性注意力混合模型（如 GLA） |
| `mamba/` | 状态空间 / lightning attention | MiniMax-01、BailingMoE |
| `kda/` | Kernelized Delta Attention | KDA 注意力混合模型 |
| `activation/`、`batch_invariant/` | 激活、批不变算子 | SwiGLU 量化、softmax、matmul |
| `spec_decode/`、`penalty.py`、`reject_sample.py` | 采样与投机解码 | n-gram 提议、拒绝采样（见 u4-l4、u10-l4） |

#### 4.1.2 核心流程

一个典型昇腾 Triton kernel 的运行流程：

1. **探测核数**：`init_device_properties_triton()` 缓存 `num_aicore` / `num_vectorcore`。
2. **静态切分**：与 GPU 上「按问题规模开 grid」不同，昇腾 kernel 常用 `grid = (num_vectorcore,)`——一个核一个 program，每个核负责一段 batch（静态切分，而非动态竞争）。
3. **UB 预算**：kernel 内同时存活的所有 tile 张量之和必须 ≤ UB 安全预算（代码里常取 87040 字节 ≈ 85KB）。据此反推「每个核每轮处理多少 token」。
4. **逐 tile 处理**：每个核在 `for` 循环里处理自己那段数据，用昇腾扩展算子 `insert_slice` / `extract_slice` 在 tile 内做切片重组。

这套模型的关键是：**核数决定并行度，UB 决定 tile 大小**。

#### 4.1.3 源码精读

公共工具 [triton_utils.py:46-66](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ops/triton/triton_utils.py#L46-L66) 缓存了核数并对外提供 `get_vectorcore_num()`：

```python
def get_vectorcore_num():
    global _NUM_VECTORCORE
    assert _NUM_VECTORCORE > 0, "Device properties not initialized ..."
    return _NUM_VECTORCORE
```

昇腾专属算子 [triton_utils.py:36-43](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ops/triton/triton_utils.py#L36-L43) 经 `_resolve_triton_ascend_op` 优先从 `triton.language.extra.cann.extension` 解析，回退到标准 `tl`：

```python
if HAS_TRITON:
    insert_slice = _resolve_triton_ascend_op("insert_slice")
    extract_slice = _resolve_triton_ascend_op("extract_slice")
    get_element = _resolve_triton_ascend_op("get_element")
```

这三个算子（在 tile 内切片、取标量）是后续 `split_qkv_rmsnorm_rope` 频繁使用的基础积木，因为昇腾 Triton 不能像 GPU 那样随意 reshape 大块寄存器，必须在 UB 内用切片操作重组数据布局。

最简单的单算子例子是 RMSNorm [rms_norm.py:5-33](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ops/triton/rms_norm.py#L5-L33)。它做的是每个 head_dim 向量上的均方根归一化：

\[ \text{out} = x \cdot \frac{1}{\sqrt{\frac{1}{D}\sum_{i} x_i^2 + \epsilon}} \]

关键几行（按核静态切分 batch）：

```python
core_id = tl.program_id(0)
core_num = tl.num_programs(0)
batch_per_core = tl.cdiv(total_batch, core_num)   # 每个核负责的行数
...
variance = tl.sum(x * x, axis=-1) / DIM
output = x * tl.rsqrt(variance[:, None] + variance_epsilon)
```

它的包装函数 [rms_norm.py:47-54](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ops/triton/rms_norm.py#L47-L54) 探测 vector core 数、决定 `BLOCK_M`、并以 `grid=(num_vectorcore,)` 启动。该函数被 DSA 注意力（`attention/dsa_v1.py`）用于 Q 归一化，是「Triton 填补 NPU 算子」的小型实例。

#### 4.1.4 代码实践（源码阅读型）

**目标**：建立「核数 → grid → UB tile」的直觉。

**步骤**：

1. 打开 [rms_norm.py](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ops/triton/rms_norm.py)，对照 `triton_q_rms`（L36-66）逐行标注：哪一行探测核数、哪一行算 `BLOCK_M`、哪一行启动 kernel。
2. 思考：如果把 `ROW_BLOCK_SIZE` 从 16 改成 1，`batch_per_core=1` 时会发生什么？预期：`BLOCK_M` 仍是 1，每核每轮处理 1 行，并行粒度变细但循环次数变多。
3. 在 [triton_utils.py](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ops/triton/triton_utils.py) 里找到 `init_device_properties_triton`，确认它如何拿到 `num_vectorcore`。

**预期结果**：你能用自己的话讲清「昇腾 Triton kernel 的 grid 为什么经常等于核数」。若手头无 NPU，则跳过运行，标注为**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `triton_rms_kernel` 用 `grid=(num_vectorcore,)` 而不是按 `total_batch` 开 grid？
**答**：昇腾的 vector core 数量是固定的硬件资源；按核数开 grid 让每个核静态认领一段 batch，可避免 GPU 式动态调度在昇腾上的开销，也便于控制每核的 UB 占用。

**练习 2**：`_resolve_triton_ascend_op` 先查 `cann.extension` 再回退 `tl`，为什么？
**答**：`insert_slice` / `extract_slice` / `get_element` 是昇腾 Triton 后端的扩展算子，标准 `triton.language` 不一定提供；回退到 `tl` 是为了在不同 triton-ascend 版本间保持兼容。

---

### 4.2 融合算子 split_qkv_rmsnorm_rope 精读

#### 4.2.1 概念说明

`split_qkv_rmsnorm_rope` 是本讲最重要的**融合算子**。标准 Transformer 注意力层在拿到 QKV 投影后，通常要做一连串串行小算子（以 Qwen / Llama 系的「QK-Norm + RoPE」注意力为例）：

1. 把拼接好的 QKV 按 `[q_size, kv_size, kv_size]` **split** 成 q、k、v；
2. 把 q、k **reshape** 成 `[num_heads, head_dim]`；
3. 对每个 head 的 q 做 **RMSNorm**（乘 `q_weight`）；
4. 对每个 head 的 k 做 **RMSNorm**（乘 `k_weight`）；
5. 再 **reshape** 回扁平形态；
6. 对 q、k 做 **RoPE**（旋转位置编码）。

这 6 步会产生多个中间张量（`q_norm_out`、`k_norm_out`、扁平化的 `q_flat`、`k_flat`），每个中间张量都要写回 GM 再被下一步读出。融合算子把全部 6 步压进**一个 Triton kernel**：数据从 GM 读入后，split / norm / rope 全在 UB 内完成，最后只把结果 `q, k, v` 写回 GM。RoPE 的数学是（`x1`、`x2` 为前后半维，`cos`/`sin` 由位置查表得到）：

\[
\begin{aligned}
\text{out}_1 &= x_1 \cos\theta - x_2 \sin\theta \\
\text{out}_2 &= x_2 \cos\theta + x_1 \sin\theta
\end{aligned}
\]

#### 4.2.2 核心流程

融合算子的执行流程：

1. **解析形状与开关**：`BIAS = q_bias is not None`；`IS_PARTIAL_ROPE = rope_dim != head_dim`（部分旋转：只有前 `rope_dim` 维做 RoPE，其余直通）。
2. **UB 预算反推 tile**：根据 `UB_SIZE = 87040` 和「同时驻留的张量因子」算出 `batch_size_per_iter_per_vec`（每个核每轮处理几个 token）。
3. **grid 启动**：`grid = (num_vectorcore, 1, 1)`，每个核认领一段 batch。
4. **kernel 内三段循环**：
   - 主循环：对 q+k 部分，逐 tile 读入 → RMSNorm（按 head）→ 乘 weight（±bias）→ RoPE → 写出 q、k；
   - V 段循环：v 不需要归一化和 RoPE，只是 split 后直通写出。
5. **注册**：`direct_register_custom_op(op_name="qkv_rmsnorm_rope", ...)`，对外即 `torch.ops.vllm.qkv_rmsnorm_rope`。

#### 4.2.3 源码精读

**UB tile 反推**是昇腾特有的关键技巧，[split_qkv_rmsnorm_rope.py:299-309](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ops/triton/linearnorm/split_qkv_rmsnorm_rope.py#L299-L309)：

```python
UB_SIZE = 87040  # 85K = 85 * 1024
# factor 是同时驻留各 tile 的元素总数之和
factor = 5 * q_hidden_size + 3 * kv_hidden_size + rope_dim * 2 + q_head_num * rope_dim // 2
batch_size_per_iter_per_vec = int(UB_SIZE / input.element_size()) // factor
batch_size_per_iter_per_vec = max(1, batch_size_per_iter_per_vec)
```

`factor` 本质是「这一轮里所有活着的 tile 占多少元素」，除掉 UB 字节预算后得到每轮能处理多少 token——这是融合算子能否塞进 UB 的硬约束。

**kernel 里的 RMSNorm + RoPE**（Q 分支），[split_qkv_rmsnorm_rope.py:115-159](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ops/triton/linearnorm/split_qkv_rmsnorm_rope.py#L115-L159)。先按 head 归一化（无权重）、再乘 `q_weight`：

```python
normalized_values = values_tmp1.to(tl.float32)
normalized_values = normalized_values * normalized_values
normalized_values = tl.sum(normalized_values, axis=1) / HEAD_DIM          # per-head 方差
normalized_values = 1 / tl.sqrt(normalized_values + eps).reshape(..., 1)
normalized_values = values_tmp1 * normalized_values                       # 归一化（无 weight）
normalized_values_tmp = extract_slice(...)                                 # 取 q 头
normalized_values_tmp = (normalized_values_tmp * q_weight_values).to(tl.bfloat16)  # 乘 weight
# RoPE：x1*cos - x2*sin ， x2*cos + x1*sin
values_tmp = insert_slice(values_tmp, x1 * cos - x2 * sin, ...)
values_tmp = insert_slice(values_tmp, x2 * cos + x1 * sin, ...)
```

注意：这些 `extract_slice` / `insert_slice` 全在 UB 内重组数据，**全程不落 GM**。这就是融合的收益所在。

**注册** [split_qkv_rmsnorm_rope.py:384-390](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ops/triton/linearnorm/split_qkv_rmsnorm_rope.py#L384-L390) 与 [ops/__init__.py:25-29](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ops/__init__.py#L25-L29)：

```python
direct_register_custom_op(
    op_name="qkv_rmsnorm_rope",
    op_func=split_qkv_rmsnorm_rope_impl,
    fake_impl=split_qkv_rmsnorm_rope_impl_fake,
    mutates_args=[],
    dispatch_key="PrivateUse1",
)
```

import 即注册（受 `HAS_TRITON` 保护）。`fake_impl` 只做形状推导，供 `torch.compile` / ACL Graph 追踪（见 u8）。

**按芯片型号分发**：默认路径 [device_op.py:939](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/device/device_op.py#L939) 调 `torch.ops.vllm.qkv_rmsnorm_rope`（cube+vector 混合版）；而 A5 芯片走 SIMT 变体 [device_op.py:1819](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/device/device_op.py#L1819) 调 `torch.ops.vllm.qkv_rmsnorm_rope_simt`。分发逻辑 [device_op.py:1878-1887](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/device/device_op.py#L1878-L1887)：A5 返回 `A5DeviceAdaptor`，310P 返回 `Ascend310PDeviceAdaptor`，其余返回 `BaseDeviceAdaptor`。SIMT 变体（[split_qkv_rmsnorm_rope_simt.py:8-36](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ops/triton/linearnorm/split_qkv_rmsnorm_rope_simt.py#L8-L36)）先用一个 `precompute_rope_cos_sin_kernel` 把 cos/sin 预取成连续 buffer，再在纯向量核上跑主算子，以适配 A5 的核型分布。

**与融合 pass 的衔接**：编译期，[qknorm_rope_fusion_pass.py:52-95](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/compilation/passes/qknorm_rope_fusion_pass.py#L52-L95) 用 torch.fx 模式匹配找到「split → npu_rms_norm(q) → npu_rms_norm(k) → npu_rotary_embedding」子图，把它替换成单个 `DeviceOperator.split_qkv_rmsnorm_rope` 调用。也就是说：模型代码里写的仍是分散的上游算子，编译期被自动改写为融合算子（详见 u8-l2）。

#### 4.2.4 代码实践（本讲指定任务）

**目标**：说清 `split_qkv_rmsnorm_rope` 把哪几个上游算子融合成了一个 kernel，以及带来的收益。

**步骤**：

1. 打开 [qknorm_rope_fusion_pass.py](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/compilation/passes/qknorm_rope_fusion_pass.py)，阅读 `get_pattern()`（L52-76）——这段「被替换前的子图」就是答案的权威来源。
2. 对照 `get_replacement()`（L78-95），确认替换后是单个 `split_qkv_rmsnorm_rope`。
3. 列出被融合的上游算子（见下方答案）。
4. （可选，需 NPU）运行精度测试 [test_split_qkv_rmsnorm_rope.py](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/tests/e2e/nightly/single_node/ops/singlecard_ops/triton/test_split_qkv_rmsnorm_rope.py)，对照 `custom_rope` 与 `rms_norm` 的朴素实现验证等价性。

**被融合的上游算子**（来自 `get_pattern`）：

- `qkv.split([q_size, kv_size, kv_size], dim=-1)` — 拆分 QKV；
- `q.view(..., num_heads, head_dim)` + `torch.ops.npu.npu_rms_norm(q_by_head, q_weight, eps)` — Q 的逐 head RMSNorm；
- `k.view(...)` + `torch.ops.npu.npu_rms_norm(k_by_head, k_weight, eps)` — K 的逐 head RMSNorm；
- `.view(q.shape)` / `.view(k.shape)` — 扁平化；
- `torch.ops.vllm.npu_rotary_embedding(positions, q_flat, k_flat, cos_sin_cache, ...)` — Q、K 的 RoPE。

**收益**：原本上述步骤会产生 `q_norm_out`、`k_norm_out`、`q_flat`、`k_flat` 等中间张量，每个都要写 GM 再读 GM；融合后这些中间量全部留在 UB 内，GM 读写次数从「每步一次」降到「首尾各一次」，大幅降低访存带宽压力——这对 decode 阶段（计算量小、访存占比高）尤其关键。

**预期结果**：你能写出上述 5 类算子的清单，并解释「省 GM 访存」这一收益。运行测试属于**待本地验证**（需 NPU）。

#### 4.2.5 小练习与答案

**练习 1**：`IS_PARTIAL_ROPE` 为真时 kernel 行为有何不同？
**答**：当 `rope_dim != head_dim`（部分旋转），只有前 `rope_dim` 维做 RoPE，后半维直通；kernel 在写出前用 `insert_slice` 把旋转后的前半维贴回原张量对应位置（见 L163-175 的 `IS_PARTIAL_ROPE` 分支）。

**练习 2**：为什么需要 `fake_impl`？
**答**：`torch.compile` / ACL Graph 在捕获计算图时只做形状推导、不真正执行算子；`fake_impl` 返回正确 shape 的空张量，让图捕获阶段不报错，承接 u6-l1 的「四段式」要求。

**练习 3**：A5 芯片为什么改用 `_simt` 变体？
**答**：A5 的核型分布与 A2/A3 不同，混合 cube+vector 的主版本并非最优；SIMT 变体先用独立 kernel 把 cos/sin 预取成连续 buffer，再在向量核上用纯 SIMT 风格执行，更贴合 A5 硬件。

---

### 4.3 线性注意力内核：fla chunk 与 mamba lightning_attn

#### 4.3.1 概念说明

标准注意力的复杂度随序列长度呈平方增长，长上下文代价高。**线性注意力 / 状态空间模型**（SSM）把「query × key × value」改写成可递推的隐状态 `h`，复杂度近似线性。但这类模型的 GPU 高性能 kernel（flash-linear-attention、MiniMax lightning attention）依赖 CUDA，NPU 没有现成 CANN 算子。`ops/triton/` 的 `fla/`、`mamba/`、`kda/` 三个子集就是为这些模型补的 NPU 实现：

- **`fla/`（Flash Linear Attention）**：实现 gated delta rule 的 chunk 算法（来自 flash-linear-attention 项目）。把序列切成 chunk，用 WY 表示把递推转成块矩阵运算。
- **`mamba/lightning_attn.py`**：实现 lightning attention（MiniMax-01、BailingMoE 用的线性注意力），用「对角块 + 非对角块 + KV 外积前缀和」分解。
- **`kda/`（Kernelized Delta Attention）**：与 fla 同源的 delta rule 家族，额外提供 fused recurrent 变体；代码里用 `IS_KDA=True` 区分。

这类算子的共同特点是**计算重、访存模式复杂**，单靠 Triton 在 vector 核上算不够快，所以 `fla/chunk.py` 采用了「Triton 做轻量准备 + AscendC 做重活」的混合策略（连接 u6-l3）。

#### 4.3.2 核心流程

**fla chunk gated delta rule** 的流程（`chunk_gated_delta_rule_fwd`）：

1. **轻量准备（Triton）**：`chunk_local_cumsum` 累加门控 `g`；`chunk_scaled_dot_kkt_fwd` + `solve_tril` 求 WY 表示的三角矩阵 `A`；`recompute_w_u_fwd` 得到 `w, u`。
2. **重活（AscendC）**：调 `torch.ops._C_ascend.chunk_gated_delta_rule_fwd_h` 算递归隐状态 `h` 与新 value `v_new`；调 `torch.ops._C_ascend.chunk_fwd_o` 算输出 `o`。
3. **上下文并行（PCP）**：若 PCP 组 world_size > 1，各 rank 算完本地 `final_state` 后用 `all_gather` 汇总，并按递推关系 `correct_i = Φ_i·correct_{i-1} + p_i` 修正跨 rank 的隐状态。

**mamba lightning attention** 的流程（`_attention.forward`）把注意力矩阵拆成 4 个 kernel：

1. `_fwd_diag_kernel`：算**对角块**（query 只 attend 同一块内的 key），逐 sub-block 累加；
2. `_fwd_kv_parallel`：并行算每个 block 的 **KV 外积**；
3. `_fwd_kv_reduce`：跨 block 做**前缀和**得到 KV 历史，按衰减 `exp(-s·block_size)` 折扣；
4. `_fwd_none_diag_kernel`：算**非对角块**，与对角结果相加。

#### 4.3.3 源码精读

**fla 的混合调用**——重活下沉到 AscendC，[fla/chunk.py:115-129](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ops/triton/fla/chunk.py#L115-L129) 与 [fla/chunk.py:197-209](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ops/triton/fla/chunk.py#L197-L209)：

```python
h, v_new, final_state = torch.ops._C_ascend.chunk_gated_delta_rule_fwd_h(
    k_ascendc, w_ascendc, u_ascendc, g=g_ascendc, ...)
...
o_ascendc = torch.ops._C_ascend.chunk_fwd_o(
    q_ascendc, k_ascendc, v_new, h, scale, g=g_ascendc, ...)
```

注意 `torch.ops._C_ascend.*`——这部分是 u6-l3 讲的 C++/AscendC 内核。也就是说，`fla/chunk.py` 虽在 `ops/triton/` 下，却是 Triton（准备阶段）+ AscendC（递推与输出）的混合体。这是「Triton 表达不了或不够快就下沉 AscendC」原则的实例。

**fla 的 PCP 修正**，[fla/chunk.py:138-171](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ops/triton/fla/chunk.py#L138-L171)：当序列被切到多个 rank（PCP，承接 u5-l3），每个 rank 的本地 `final_state` 必须用前一个 rank 的状态修正，递推关系为：

\[
\text{correct}_i = \Phi_i \cdot \text{correct}_{i-1} + p_i
\]

代码里 `for i in range(1, world_size)` 逐 rank 累积修正，rank 0 拿原始状态、其余 rank 用前驱修正后重算本地 `h`。

**lightning attention 的分块分解**，模块文档 [mamba/lightning_attn.py:18-23](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ops/triton/mamba/lightning_attn.py#L18-L23) 点明它替换的是 GPU 专属 kernel：

> NPU-compatible replacements for GPU-only Triton kernels used in BailingMoELinearAttention: `LightningAttentionKernelNPU` replaces `MiniMaxText01LinearKernel`.

四步分块的调度与 UB 预算注释见 [mamba/lightning_attn.py:418-538](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ops/triton/mamba/lightning_attn.py#L418-L538)，例如 `BLOCK=256`、`CBLOCK_D=32`（对角块，UB≈72KB）、`CBLOCK_KV=64`（KV 外积，UB≈112KB）。对角块 kernel 的因果衰减 [mamba/lightning_attn.py:106-112](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ops/triton/mamba/lightning_attn.py#L106-L112)：

```python
decay = tl.exp(s_index)        # s_index = -s * (q_pos - k_pos)
qk = tl.dot(q, k.trans()) * decay
qkv += tl.dot(qk, v)
```

这里有个昇腾特有的坑：[mamba/lightning_attn.py:93-100](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ops/triton/mamba/lightning_attn.py#L93-L100) 注释说明 `tl.load(..., other=0.0)` 在昇腾上不一定能可靠清零越界元素（vector→cube 加载特性），需要显式 `tl.where` 再掩一次，否则尾部脏数据会在点积里产生 NaN。这是「Triton 适配昇腾精度」的典型实例。

`AscendLightningAttentionKernel`（[mamba/lightning_attn.py:590-627](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ops/triton/mamba/lightning_attn.py#L590-L627)）是对外的 `jit_linear_forward_prefix` 入口，把 KV cache reshape 成 `[1, h, d, e]` 的 history 喂给 `lightning_attention_npu`。

#### 4.3.4 代码实践（源码阅读型）

**目标**：理解 lightning attention 的「对角 vs 非对角」分块。

**步骤**：

1. 打开 [mamba/lightning_attn.py](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ops/triton/mamba/lightning_attn.py)，在 `_attention.forward`（L388-544）里找到 4 个 kernel 的启动顺序与各自 grid。
2. 对每个 kernel 的注释（L403-417、L469-474 等）提炼出它的「UB 占用估算」。
3. 对照 `_fwd_diag_kernel`（L106-112）与 `_fwd_none_diag_kernel`（L373-384），说明为何一个要内层 `for j` 循环、另一个是单次 `tl.dot`。

**预期结果**：能口头复述「对角块要枚举块内所有 sub-block 对（双重循环 + 因果 mask），非对角块因为 attend 的是更早的整块 KV（已折叠进 `kv` 外积），所以一次 `tl.dot` 即可」。运行验证为**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`fla/chunk.py` 明明在 `ops/triton/` 下，为什么会出现 `torch.ops._C_ascend.*`？
**答**：它是 Triton + AscendC 混合实现：轻量的 WY 准备用 Triton，但递推隐状态 `h` 与输出 `o` 的矩阵运算量大、Triton 在 vector 核上不够快，故下沉到 AscendC C++ 内核（u6-l3）。

**练习 2**：lightning attention 为什么要拆成对角块和非对角块两组 kernel？
**答**：对角块（query 与 key 同属一个 block）需要逐 sub-block 的因果处理，逻辑复杂；非对角块（query 与更早的 key）因为整块 KV 已被折叠成一个外积 `kv`，可以一次矩阵乘完成。拆开能让两类各自选用最优 tile 形状与 UB 预算。

**练习 3**：`fla/chunk.py` 里 PCP 修正为何要 `for i in range(1, world_size)` 逐 rank 累积，而不是直接 all-reduce？
**答**：线性注意力的隐状态递推是「乘性 + 加性」的有序递推（`correct_i = Φ_i·correct_{i-1} + p_i`），不是可交换的求和；必须按 rank 顺序逐个修正，all-reduce 无法表达这种顺序依赖。

## 5. 综合实践

把本讲的三条主线串起来，完成一个「算子选型溯源」小任务：

1. **从模型到算子**：挑一个使用了 QK-Norm 的注意力模型（如 Qwen 系列）。说明它在前向时会经过 `split → npu_rms_norm → npu_rotary_embedding` 这串算子。
2. **追踪融合**：说明这串算子在编译期如何被 [qknorm_rope_fusion_pass.py](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/compilation/passes/qknorm_rope_fusion_pass.py) 改写成单个 `split_qkv_rmsnorm_rope`，并指出分发到哪个具体 kernel（默认 vs A5 SIMT）由 [device_op.py:1878-1887](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/device/device_op.py#L1878-L1887) 决定。
3. **回到运行模型**：解释这个 kernel 的 grid 为何是 `(num_vectorcore,)`、UB 如何决定每轮 token 数（[split_qkv_rmsnorm_rope.py:299-309](https://github.com/vllm-project/vllm-ascend/blob/646684f43ce4bdc737203a8df1149e37ea2ff824/vllm_ascend/ops/triton/linearnorm/split_qkv_rmsnorm_rope.py#L299-L309)）。
4. **对照线性注意力**：再挑一个用 lightning attention 的模型（如 BailingMoE / MiniMax-01），对比它为何不能用上面的融合算子，而要走 `mamba/lightning_attn.py` 的四 kernel 分解，并点出其中「Triton 不够快就下沉 AscendC」的位置。

**产出**：一张表格，列出「标准 Transformer 注意力层」与「线性注意力层」两条路径各自用到的 Triton 算子、是否融合、是否下沉 AscendC、关键收益。这张表能帮你把 u6-l1（注册）、u6-l2（本讲，Triton）、u6-l3（AscendC）三层串成一个完整认知。

## 6. 本讲小结

- vllm-ascend 用 Triton 重写算子的三大动机：**融合省访存**、**填补 GPU 专属算子空白**（fla/mamba/kda）、**精度适配**。
- 昇腾 Triton 的运行模型是「**核数定 grid、UB 定 tile**」：`grid=(num_vectorcore,)` 静态切分 batch，`UB_SIZE=87040` 反推每轮 token 数；`insert_slice/extract_slice/get_element` 是昇腾扩展的 tile 重组积木。
- **融合算子 `split_qkv_rmsnorm_rope`** 把 split + Q/K RMSNorm + Q/K RoPE 共 5 类上游算子压成一个 kernel，中间量全留 UB，按芯片型号在默认版与 A5 SIMT 版间分发，并由 `qknorm_rope_fusion_pass` 在编译期自动改写。
- **线性注意力子集** `fla/mamba/kda` 为 NPU 补齐了 GPU 专属的高性能 kernel；`fla/chunk.py` 是 Triton + AscendC 混合体，重活下沉 `_C_ascend.*`，并支持 PCP 跨 rank 隐状态修正。
- 算子经 `direct_register_custom_op` 注册为 `torch.ops.vllm.*`（承接 u6-l1），import 副作用注册受 `HAS_TRITON` 保护，无 Triton 时安全回退。

## 7. 下一步学习建议

- 阅读 [u6-l3](u6-l3-cpp-ascendc-kernels.md)，看本讲 `fla/chunk.py` 调用的 `torch.ops._C_ascend.chunk_gated_delta_rule_fwd_h` / `chunk_fwd_o` 在 `csrc/` 里是如何用 AscendC 的 op_host/op_kernel 双文件结构实现的。
- 阅读 [u8-l2](u8-l2-fusion-passes.md)，深入 `qknorm_rope_fusion_pass` 的 torch.fx 模式匹配机制，理解「模型写分散算子、编译期改写为融合算子」的完整闭环。
- 想了解这些 Triton 算子如何被 ACL Graph 当作原子黑盒捕获，可衔接 [u8-l3](u8-l3-aclgraph.md)；想了解 `penalty.py` / `reject_sample.py` 如何服务投机解码，可衔接 [u4-l4](u4-l4-sampler-and-rejection.md) 与 [u10-l4](u10-l4-speculative-decoding.md)。
