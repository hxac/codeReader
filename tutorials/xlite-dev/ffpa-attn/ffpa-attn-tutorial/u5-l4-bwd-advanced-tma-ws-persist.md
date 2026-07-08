# 反向高级开关：TMA / warp-specialize / persist / split-launch

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 `enable_tma`、`enable_ws`、`persist_dkdv`、`split_launch` 这四个 Triton 反向开关各自解决什么问题，以及它们之间的依赖关系（谁必须配谁）。
- 解释为什么 `persist_dkdv` 能提升精度：它把 `dK`/`dV` 的跨块累加从「全局 load/add/store 往返」改成「fp32 寄存器内累加、最后一次性 store」。
- 看懂 `_ffpa_bwd_dkdv_persist_sm90` 里 `dk_acc`/`dv_acc` 两个寄存器累加器的循环结构，并理解它为什么对 `is_causal=True` 的 bf16 场景尤其重要。
- 理解 TMA `TensorDescriptor` 加载与 `warp_specialize` 如何让「全局→共享内存」的加载与 MMA 计算重叠。
- 用 `grad_kv_storage_dtype`/`grad_q_storage_dtype` 控制梯度缓冲区的存储 dtype，并结合 `_triton_bwd_grad_tensor_like` 的注释算出 `fp32` 的显存代价。

## 2. 前置知识

本讲是 Triton 反向系列的最后一篇，建立在 u5-l1（delta 预处理）、u5-l2（shared-pid 主路径）、u5-l3（decode 反向）之上。默认你已经知道：

- FFPA 反向先跑一个 `_ffpa_bwd_pre_impl` 算 `delta = rowsum(dO * O)`，再让主 kernel 用它做 rescale。
- shared-pid 主路径里 `dK`/`dV` 由 K 列块独占（普通 store），`dQ` 由 Q 行块独占（普通 store），三者都用「首块 store、其余 load+add+store」的非原子模式累加。
- FFPA 主攻大 head_dim（D>256），split-D 把 D 维切成 `BLOCK_HEADDIM` 宽的片段。

本讲补充四个基础术语：

- **TMA（Tensor Memory Accelerator，张量内存加速器）**：Hopper（SM90）引入的硬件单元，能用一条指令把一块连续的 HBM 数据异步搬进共享内存（SRAM）。Triton 里通过 `TensorDescriptor`（张量描述符）使用：宿主机先建好描述符，kernel 里 `desc.load([y, d])` 就发起一次 TMA 拷贝。
- **warp-specialize（线程束特化）**：把一个 CTA 内的 warp 分成「生产者」（专门做 g2s 加载）和「消费者」（专门做 MMA 计算）两组，用 barrier 协调，让加载和计算在时间上重叠。Triton 里通过 `tl.range(..., warp_specialize=True)` 开启。
- **跨块累加（cross-tile accumulation）**：反向里一个输出 tile（如某块 `dK`）由多个输入 tile 贡献，需要把各贡献累加起来。FFPA 的非持久路径用「全局 load 旧值 → 加新贡献 → store 回去」实现，每个贡献都要一次 HBM 往返。
- **fp32 寄存器累加器**：把累加变量放在寄存器里（而非 HBM），全程用 fp32，直到循环结束才一次性转存（cast+store）。这是 FlashAttention 类 kernel 高精度的关键。

> 直觉：u5-l2 讲过，非融合主路径对 `dK`/`dV`/`dQ` 用的是「load 旧值 → add → store」的全局往返模式。这个模式有个代价——每次往返都要按**存储 dtype**（bf16 或 fp16）做一次舍入，累加多了精度就掉得厉害，尤其是 `is_causal=True` 时因果掩码让有效行变少、舍入误差占比更大。本讲的四个开关都是围绕「怎么少做往返、怎么让往返更精确、怎么让加载更高效」展开的旋钮。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| `src/ffpa_attn/functional.py` | `TritonBackend` 配置类定义了四个开关的字段与 `__post_init__` 里的依赖断言；`_FFPAAttnFunc.backward` 把这些开关透传给 Triton 反向入口。 |
| `src/ffpa_attn/triton/_ffpa_bwd.py` | 通用反向实现。`_ffpa_attn_backward_triton_impl` 的签名带四个 `enable_*` 形参，并在顶部用 `if enable_tma and seqlen_q >= 8` 决定是否进入 SM90 分支。模块开头的 docstring 解释了「全局往返累加」为何是精度与性能瓶颈。 |
| `src/ffpa_attn/triton/_ffpa_bwd_sm90.py` | SM90 专用 TMA 反向实现。包含 `is_sm90_tma_backward_supported` 能力探测、`_ffpa_bwd_sm90_make_descs` 描述符构造、`_ffpa_bwd_dkdv_persist_sm90` 持久累加 kernel、以及 `_ffpa_attn_backward_sm90_impl` 分发逻辑。 |
| `src/ffpa_attn/triton/__init__.py` | `torch.library` 注册的 `_bwd_triton` 算子实现。`_triton_bwd_grad_tensor_like` 分配 `dQ`/`dK`/`dV` 缓冲区（决定跨块累加的存储 dtype），`_grad_kv_storage_dtype_from_code` 把 int 编码解码成 dtype。 |
| `tests/test_ffpa_bwd.py` | SM90 TMA + persist/split_launch 的正确性测试，本讲实践直接参考它的调用写法。 |

## 4. 核心概念与源码讲解

### 4.1 四个反向开关：定义、依赖与 fail-fast 断言

#### 4.1.1 概念说明

FFPA 的 Triton 后端在 `TritonBackend` 配置类里暴露了四个「反向高级开关」。它们都不是默认开启的（默认全 `False`，走最通用的 shared-pid 主路径），需要用户显式通过 `backward_backend=TritonBackend(...)` 传入：

- `enable_tma`：启用 SM90+ 的 TMA 硬件加速。它是「大门」——SM90 专用反向分支只有它在 `True` 时才会进入。
- `enable_ws`：强制 warp-specialized 配置（语义上要求 `enable_tma=True`）。
- `persist_dkdv`：把 `dK`/`dV` 的累加器以 fp32 形式驻留在寄存器里跨块累加（要求 `enable_tma=True` 且 `backward=True`）。
- `split_launch`：把原本一次 launch 算完的 `dKdV` 与 `dQ` 拆成两次独立 launch。

这四个开关有严格的依赖关系，违反会在构造 `TritonBackend` 时立即 `assert` 失败（fail-fast），而不是静默忽略。

#### 4.1.2 核心流程

开关从用户到 kernel 的传递链路：

```text
backward_backend=TritonBackend(enable_tma=..., persist_dkdv=...)
        │  __post_init__ 校验依赖（assert）
        ▼
FFPAAttnMeta.from_kwargs → backward_meta（一个 TritonBackend 实例）
        │  _FFPAAttnFunc.backward 透传
        ▼
_ffpa_attn_backward_triton(enable_tma=..., enable_persist_dkdv=..., ...)
        │  torch op _bwd_triton（编码成 int）
        ▼
_ffpa_attn_backward_triton_impl(enable_tma, enable_ws, enable_persist_dkdv, enable_split_launch)
        │  if enable_tma and seqlen_q >= 8:
        ▼
_ffpa_attn_backward_sm90_impl(...)   ← SM90 专用 TMA 分支
```

#### 4.1.3 源码精读

`TritonBackend` 把四个开关声明为 dataclass 字段，docstring 说明了各自的依赖：

[src/ffpa_attn/functional.py:174-202](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L174-L202) — `TritonBackend` 类定义。注意 docstring 明确写了 `persist_dkdv` 要求 `enable_tma` 和 `backward=True`，`enable_ws` 要求 `enable_tma`。

依赖断言集中在 `__post_init__`：

[src/ffpa_attn/functional.py:204-218](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L204-L218) — 三条硬约束：`persist_dkdv` 必须同时满足 `backward=True` 与 `enable_tma=True`；`split_launch`/`preprocess_d_chunk`/`grad_kv_storage_dtype`/`grad_q_storage_dtype` 都要求 `backward=True`。这意味着这些开关只能挂在「反向后端」上，传成 `forward_backend` 会在构造时直接报错。

`_FFPAAttnFunc.backward` 把这些开关原样透传给 Triton 反向入口（注意 `persist_dkdv` 在这里改名成 `enable_persist_dkdv`、`split_launch` 改名成 `enable_split_launch`）：

[src/ffpa_attn/functional.py:885-888](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L885-L888) — 四个 `enable_*` 形参从 `backward_meta` 取值传给 `_ffpa_attn_backward_triton`。

#### 4.1.4 代码实践

实践目标：亲手触发 `TritonBackend` 的 fail-fast 断言，确认依赖关系。

1. 操作步骤：阅读 `tests/test_triton_autotune_mode.py` 里 `test_persist_dkdv_requires_backward_tma` 的写法，仿照它构造两个 `TritonBackend`——一个违反依赖、一个满足依赖。
2. 需要观察的现象：违反依赖的构造应抛出 `AssertionError`，消息包含 `persist_dkdv requires enable_tma`；满足依赖的正常返回，且 `meta.backward_meta.persist_dkdv is True`。
3. 预期结果：参考 `tests/test_triton_autotune_mode.py:81-98` 的断言。

```python
# 示例代码：演示依赖断言（不是项目原有测试，仅作说明）
import pytest
from ffpa_attn.functional import TritonBackend, FFPAAttnMeta

# 违反依赖：persist_dkdv=True 但没开 enable_tma → 应抛 AssertionError
with pytest.raises(AssertionError, match="persist_dkdv requires enable_tma"):
    FFPAAttnMeta.from_kwargs(
        backward_backend=TritonBackend(backward=True, persist_dkdv=True)
    )

# 满足依赖：正常构造
meta = FFPAAttnMeta.from_kwargs(
    backward_backend=TritonBackend(
        backward=True, enable_tma=True, persist_dkdv=True
    )
)
assert meta.backward_meta.enable_tma is True
assert meta.backward_meta.persist_dkdv is True
```

> 注意：`enable_ws` 没有硬 `assert` 要求 `enable_tma`，但它的语义（docstring 里写了 `requires enable_tma`）只在 SM90 TMA 分支里生效——非 TMA 路径的通用 kernel 根本没有 `warp_specialize` 形参，所以 `enable_ws=True` 但 `enable_tma=False` 时它会被**静默忽略**。这是「软依赖」与「硬断言」的区别。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `persist_dkdv` 必须要求 `backward=True`，而不能挂在 `forward_backend` 上？

**答案**：`persist_dkdv` 优化的是反向里 `dK`/`dV` 的跨块累加，前向根本不存在「梯度累加」这一步。`Backend` 基类里 `forward`/`backward` 是互斥声明的（一个 `TritonBackend` 实例要么管前向、要么管反向），挂在 `forward_backend` 上语义上无意义，所以用 `assert` 直接拦下。

**练习 2**：如果同时传 `split_launch=True` 和 `preprocess_d_chunk=True` 但忘了写 `backward=True`，会发生什么？

**答案**：`__post_init__` 里的第二条断言会失败，抛出 `AssertionError: backward-only Triton options require backward=True`。这两个开关都是反向专用的。

---

### 4.2 enable_tma + warp_specialize：SM90 专用 TMA 反向分支

#### 4.2.1 概念说明

`enable_tma=True` 会把反向从「通用 shared-pid 路径」切到「SM90 专用 TMA 路径」。两者最大的区别在于**加载方式**：

- 通用路径：用裸指针 `tl.load(Q + off_b*stride_qb + ...)`，每次加载要算一长串地址。
- TMA 路径：用 `TensorDescriptor`，kernel 里 `desc_q.load([y_offset, d_offset])` 一行发起异步批量拷贝，硬件自己处理地址与边界。

`warp_specialize` 进一步在这些 TMA 加载上叠加「生产者/消费者」特化：编译器把加载循环编译成「一组 warp 生产数据（g2s）、另一组 warp 消费数据（MMA）」，二者通过 barrier 交错，让下一次加载与当前计算重叠。

#### 4.2.2 核心流程

SM90 TMA 分支的进入判定在通用反向入口的顶部：

```text
_ffpa_attn_backward_triton_impl:
  if enable_tma and seqlen_q >= 8:
      if is_sm90_tma_backward_supported(q, k, v, do, dq, dk, dv, seqlen_q):
          _ffpa_attn_backward_sm90_impl(...)   # 进入 SM90 TMA 路径，return
  # 否则继续走通用 shared-pid 路径
```

`is_sm90_tma_backward_supported` 做四项检查：`seqlen_q >= 8`（decode 形状不走这条路）、张量在 CUDA 上、设备算力 major>=9（Hopper+）、dtype 是 fp16/bf16、且所有张量最后一维 stride==1（列连续，TMA 对布局有要求）。

#### 4.2.3 源码精读

进入 SM90 分支的判定与委托：

[src/ffpa_attn/triton/_ffpa_bwd.py:2109-2140](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd.py#L2109-L2140) — `if enable_tma and seqlen_q >= 8` 进入，先调 `is_sm90_tma_backward_supported` 探测，通过则把 `enable_ws`/`enable_persist_dkdv`/`enable_split_launch`/`use_dkdvdq_fusion` 全部传给 `_ffpa_attn_backward_sm90_impl` 并 `return`。注意它把通用路径算出的 `split_launch`（已经 `and seqlen_q >= 8`）和 `use_dkdvdq_fusion` 一起带过去，保证两个分支的行为一致。

能力探测函数：

[src/ffpa_attn/triton/_ffpa_bwd_sm90.py:1482-1502](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd_sm90.py#L1482-L1502) — 四项检查。最后一行 `all(tensor.stride(-1) == 1 for tensor in ...)` 要求所有输入输出张量最后一维（head_dim 维）连续，这是 TMA 描述符的布局前提。

描述符构造——注意它把 `[B, H, N, D]` 拍扁成二维 `[B*H*N, D]`：

[src/ffpa_attn/triton/_ffpa_bwd_sm90.py:1505-1529](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd_sm90.py#L1505-L1529) — `_ffpa_bwd_sm90_make_descs` 为 q/k/v/do 各建一个 `TensorDescriptor`，`shape=[B*H*N, D]`、`strides=[D, 1]`、`block_shape=[1,1]`（占位，真正块形状由 `pre_hook` 在 launch 时按 `BLOCK_M`/`BLOCK_N`/`BLOCK_HEADDIM` 填）。

warp_specialize 在 kernel 里的体现——Phase 1 的 D 片段循环：

[src/ffpa_attn/triton/_ffpa_bwd_sm90.py:150-174](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd_sm90.py#L150-L174) — `if warp_specialize:` 分支用 `tl.range(0, num_d_chunks, 1, disallow_acc_multi_buffer=True, flatten=True, warp_specialize=True)`，里面是四个 `desc_*.load(...)` 加两个 `tl.dot`；`else` 分支是普通 `for d_chunk in range(...)`。两个分支计算完全等价，区别只在编译器是否插入生产者/消费者流水线。

autotune 候选配置如何反映 `enable_ws`：

[src/ffpa_attn/triton/_ffpa_bwd_sm90.py:1297-1337](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd_sm90.py#L1297-L1337) — `_gen_bwd_sm90_autotune_configs`。关键两行：`for num_warps in [8, 16] if enable_ws else [4, 8]`（ws 模式只搜 8/16 warp，因为要分组），以及 `"warp_specialize": enable_ws`（把开关烘焙进每个 config）。注意它还带 `pre_hook=_sm90_bwd_host_descriptor_pre_hook`，在 launch 前把描述符的 `block_shape` 设成 `[BLOCK_M, BLOCK_HEADDIM]` 等。

#### 4.2.4 代码实践

实践目标：理解 TMA 分支的进入条件，验证在不支持的硬件上会优雅回退。

1. 操作步骤：阅读 `is_sm90_tma_backward_supported` 的四个返回条件，在脑中（或本地）枚举「能进入」与「不能进入」的典型情形。
2. 需要观察的现象：
   - Ampere（SM80，major=8）+ `enable_tma=True`：能力探测返回 `False`，回退到通用路径，`enable_tma` 实际不生效。
   - Hopper（SM90）+ `enable_tma=True` + `seqlen_q=1`：因 `seqlen_q < 8` 返回 `False`，走 decode 反向路径。
   - Hopper + `seqlen_q=128` + 所有张量列连续：返回 `True`，进入 SM90 TMA 路径。
3. 预期结果：待本地验证（需要相应 GPU）。可在 `tests/test_ffpa_bwd.py` 里搜索 `_skip_if_no_sm90_tma`，确认 SM90 测试在没有 Hopper 的机器上会被跳过。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `_ffpa_bwd_sm90_make_descs` 要把四维 `[B, H, N, D]` 拍扁成二维 `[B*H*N, D]`？

**答案**：TMA 描述符描述的是一块二维「行 × 列」的内存区域，kernel 用 `[y_offset, d_offset]` 寻址。把 batch 和 head 维合并进 y 维后，`off_b`/`off_h` 的地址计算退化成 `q_base_y = (off_b*nheads + off_h)*seqlen_q` 这种线性偏移（见 kernel 里 `q_base_y`/`kv_base_y` 的定义），描述符本身不需要知道 batch/head 的语义，只在 y 维上做线性平移即可。

**练习 2**：`enable_ws=True` 时，autotune 为什么只搜 `num_warps in [8, 16]` 而不是 `[4, 8]`？

**答案**：warp-specialize 需要把 warp 分成生产者组和消费者组，至少要足够多的 warp 才能两组都有合理的并行度（4 个 warp 无法有效分组）。所以候选只保留 8 和 16。

---

### 4.3 persist_dkdv 与 _ffpa_bwd_dkdv_persist_sm90：fp32 寄存器跨块累加

#### 4.3.1 概念说明

这是四个开关里**最需要理解原理**的一个。要搞懂它，先得明白 u5-l2 主路径里 `dK`/`dV` 的累加是怎么做的，以及它为什么会有精度问题。

通用路径（`_ffpa_bwd_dkdv`）里，一个 K 列块要对所有 Q 行块累加 `dK`/`dV`。它没有把整块 `dK`/`dV` 留在寄存器里，而是每处理完一个 Q 块就做一次「全局往返」：

```text
# 伪代码：每个 Q 块贡献一次
if 是第一个 Q 块:
    dk_d = dS^T @ Q          # 算贡献
    store(DK, dk_d)          # 直接写
else:
    dk_val = load(DK)        # 读回旧值（按 DK 的存储 dtype 舍入过）
    dk_d = dS^T @ Q
    store(DK, dk_val + dk_d) # 加完再写（又一次舍入）
```

问题在于 `load`/`store` 会按 `DK` 的**存储 dtype**（bf16 有 7 位尾数、fp16 有 10 位）做舍入。每个 Q 块贡献都要「读 → fp32 加 → 舍入回存储 dtype → 写」一次，往返次数等于 Q 块数。模块开头的 docstring 把这一点讲得很直白。

`persist_dkdv` 的解法：把累加变量 `dk_acc`/`dv_acc` 放在 **fp32 寄存器**里，跨所有 Q 块累加，**直到这个 N 块处理完才一次性 store**。这样整个 N 块只发生一次舍入（最终 store），中间全是 fp32 寄存器加法，精度大幅提升。

#### 4.3.2 核心流程

`_ffpa_bwd_dkdv_persist_sm90` 的循环嵌套（与非持久版本对比）：

```text
非持久 _ffpa_bwd_dkdv:
  for start_m in 所有 Q 块:           # 外层遍历 Q 块
      算 score / dP / dS
      for d_chunk in D 片段:
          load 旧 DK → dk_d = dS^T @ Q → store(DK, dk_val + dk_d)   # 每 Q 块往返

持久 _ffpa_bwd_dkdv_persist_sm90:
  for out_d_chunk in D 片段:          # 外层换成 D 片段（每个片段一组累加器）
      dk_acc = zeros([BLOCK_N, BLOCK_HEADDIM], fp32)   # 寄存器累加器
      dv_acc = zeros([BLOCK_N, BLOCK_HEADDIM], fp32)
      for start_m in 所有 Q 块:
          算 score / dP / dS
          dk_acc += dS^T @ Q           # 寄存器内 fp32 累加，不碰 HBM
          dv_acc += P_drop^T @ DO
      store(DK, dk_acc)               # 整个 N 块算完，一次性 store
      store(DV, dv_acc)
```

注意循环顺序的颠倒：非持久版本外层是 Q 块、内层是 D 片段；持久版本外层是 D 片段、内层是 Q 块。这是因为持久累加器 `dk_acc` 的大小是 `[BLOCK_N, BLOCK_HEADDIM]`（一个 D 片段宽），必须为每个 D 片段单独维护一组、并在该片段的所有 Q 块上累加完才 store。

#### 4.3.3 源码精读

模块开头那段很长的 docstring 解释了「全局往返累加」为何是瓶颈和精度问题的根源：

[src/ffpa_attn/triton/_ffpa_bwd.py:34-62](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd.py#L34-L62) — 「Performance note」。第 44–48 行明确指出：累加 dtype 跟随输出存储 dtype，如果 wrapper 分配 fp32 缓冲区则跨块累加保持 fp32，否则每次往返都在低精度上舍入。这正是 `grad_kv_storage_dtype` 与 `persist_dkdv` 两条精度路线的来源。

持久累加器的声明与循环结构：

[src/ffpa_attn/triton/_ffpa_bwd_sm90.py:626-636](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd_sm90.py#L626-L636) — 外层 `for out_d_chunk in range(num_d_chunks)`，内层在循环开始处声明 `dk_acc = tl.zeros([BLOCK_N, BLOCK_HEADDIM], dtype=tl.float32)` 和 `dv_acc`。注意 `dtype=tl.float32`——这是精度收益的关键。紧跟着的注释（629–634 行）解释了动机。

[src/ffpa_attn/triton/_ffpa_bwd_sm90.py:629-634](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd_sm90.py#L629-L634) — 注释原文：避免 `fp32→bf16/fp16→fp32` 往返直到最终 store；并特别指出 `is_causal=True` 时这种往返会带来显著精度损失，且此方案只适合 SM90+ 这种算力充沛的设备，SM<90（Ada/Ampere）应该改用 fp32 HBM 存储（即 `grad_kv_storage_dtype=fp32`）。

寄存器内累加与一次性 store：

[src/ffpa_attn/triton/_ffpa_bwd_sm90.py:748-759](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd_sm90.py#L748-L759) — 内层 Q 块循环体末尾：`dk_acc += tl.trans(tl.dot(tl.trans(q), dS, out_dtype=tl.float32))`（寄存器加，不 store）。外层 D 片段循环结束后才 `tl.store(dk_ptrs, dk_acc, ...)` 一次。对比非持久版本同位置是 `load → add → store`，差异一目了然。

`persist_dkdv` 在分发层的体现——kernel 用 `PERSIST_DKDV_ACC` 这个 constexpr 在两个内部 kernel 间二选一：

[src/ffpa_attn/triton/_ffpa_bwd_sm90.py:1191-1201](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd_sm90.py#L1191-L1201) — `_ffpa_bwd_sm90_kernel_impl` 里 `if PERSIST_DKDV_ACC:` 调 `_ffpa_bwd_dkdv_persist_sm90`，否则调非持久的 `_ffpa_bwd_dkdv_sm90`。`PERSIST_DKDV_ACC` 在 launch meta 里由 `enable_persist_dkdv` 填入（见 [src/ffpa_attn/triton/_ffpa_bwd_sm90.py:1897](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd_sm90.py#L1897) 的 `PERSIST_DKDV_ACC=enable_persist_dkdv`）。

#### 4.3.4 代码实践

实践目标：亲手跑一次 `enable_tma=True, persist_dkdv=True` 的因果反向，验证它与 SDPA 数值一致（精度没有因为 bf16 往返而劣化）。

1. 实践目标：复现 `tests/test_ffpa_bwd.py::test_sm90_tma_persist_dkdv_causal_matches_sdpa`。
2. 操作步骤：参考 [tests/test_ffpa_bwd.py:170-195](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_ffpa_bwd.py#L170-L195)，构造 `B=1, H=2, N=128, D=512` 的 bf16 `q/k/v`，`is_causal=True`，用 `backward_backend=TritonBackend(backward=True, enable_tma=True, persist_dkdv=True)` 调 `ffpa_attn_func`，`out.sum().backward()`，再与 SDPA 参考梯度比 `torch.allclose`。
3. 需要观察的现象：在 Hopper（SM90）上，`q.grad`/`k.grad`/`v.grad` 与 SDPA 参考在容差内一致；尤其 `k.grad`/`v.grad`（正是 persist 优化的对象）不出现因 bf16 往返导致的精度掉点。
4. 预期结果：测试断言通过。无 Hopper 硬件时待本地验证（测试会被 `_skip_if_no_sm90_tma` 跳过）。

#### 4.3.5 小练习与答案

**练习 1**：持久版本为什么把 D 片段循环放到最外层，而不是像非持久版本那样把 Q 块循环放最外层？

**答案**：持久累加器 `dk_acc`/`dv_acc` 的形状是 `[BLOCK_N, BLOCK_HEADDIM]`，即「一个 D 片段宽」。要让它跨所有 Q 块累加完毕再 store，就必须让「D 片段」成为最外层循环：进入一个 D 片段时分配累加器，遍历完所有 Q 块后 store，再进入下一个 D 片段。如果把 Q 块放最外层，每个 Q 块都要为所有 D 片段各维护一组累加器并 store 一次，就退化成「每 Q 块一次 store」的非持久模式了。

**练习 2**：注释说持久方案「只适合 SM90+」。在 SM80（Ampere）上想要类似的 `dK`/`dV` 精度，应该用哪个开关？

**答案**：用 `grad_kv_storage_dtype=torch.float32`（见 4.4 节）。它让 wrapper 把 `DK`/`DV` 缓冲区分配成 fp32，这样即使仍走「load/add/store」往返，每次往返也在 fp32 上进行，舍入误差大幅降低。代价是显存翻倍。

---

### 4.4 split_launch、dkdvdq 融合与 grad_*_storage_dtype：精度与显存的另一组旋钮

#### 4.4.1 概念说明

`persist_dkdv` 是「在寄存器里解决精度问题」，但只适用于 SM90。本节讲另外两条路线：

1. **`split_launch`**：调度层面的开关。通用主路径用一个 shared-pid kernel 同时算 `dKdV` 和 `dQ`（grid 第 0 维取 `max(cdiv(Nk,BN), cdiv(Nq,BM))`）。`split_launch=True` 改成两次独立 launch——一次只算 `dKdV`（grid 第 0 维 `cdiv(Nk,BN)`），一次只算 `dQ`（grid 第 0 维 `cdiv(Nq,BM)`）。好处是两个 kernel 可以各自 autotune 最优的 `BLOCK_M`/`BLOCK_N`，互不牵制。

2. **`grad_kv_storage_dtype` / `grad_q_storage_dtype`**：精度层面的开关。它改变的不是 kernel 内部逻辑，而是 wrapper 分配 `DK`/`DV`/`DQ` 缓冲区的**存储 dtype**。由于跨块累加的「load/add/store」往返按存储 dtype 舍入，把缓冲区设成 fp32 就等于让所有往返在 fp32 上进行。

这两个开关（连同 `preprocess_d_chunk`）只要求 `backward=True`，**不**要求 `enable_tma`，所以在非 Hopper 硬件上也能用。

#### 4.4.2 核心流程

存储 dtype 如何影响 kernel 行为（关键链路）：

```text
backward_backend=TritonBackend(grad_kv_storage_dtype=torch.float32)
        ▼  _bwd_triton torch op（编码成 int：None=0, fp32=1, fp16=2）
_grad_kv_storage_dtype_from_code(1) → torch.float32
        ▼
dk = _triton_bwd_grad_tensor_like(k, torch.float32)   # 分配 fp32 缓冲区
        ▼  kernel 里 tl.load(dk_ptrs)/tl.store(dk_ptrs, ...) 按 fp32 往返
```

存储 dtype 的 int 编码（跨 torch op 边界传递时必须序列化成 int）：

| code | grad_kv/grad_q_storage_dtype |
| --- | --- |
| `0` | `None`（保持激活值的 fp16/bf16） |
| `1` | `torch.float32` |
| `2` | `torch.float16` |

#### 4.4.3 源码精读

`split_launch` 与 `use_dkdvdq_fusion` 的解析（注意它们都带 `seqlen_q >= 8` 守卫）：

[src/ffpa_attn/triton/_ffpa_bwd.py:2099-2107](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd.py#L2099-L2107) — `split_launch = enable_split_launch and seqlen_q >= 8`；`use_dkdvdq_fusion` 由环境变量 `FFPA_TRITON_BWD_FUSE_DKDVDQ` 控制，且要求「未 split_launch」。注释（2099–2103 行）特别澄清：`split_launch` 只改变 dKdV 与 dQ 是否分两次 launch，**不**消除重复的 score 重算——单 launch 的 wrapper 仍顺序调用 dKdV 和 dQ 两个角色，重复重算是 kernel 结构本身的问题。

存储 dtype 从 `dk.dtype` 反推（用于持久化配置查找）：

[src/ffpa_attn/triton/_ffpa_bwd.py:2172-2179](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd.py#L2172-L2179) — wrapper 已经按 `grad_kv_storage_dtype` 分配了 `dk`/`dv`，这里通过检查 `dk.dtype` 反推出字符串 `"fp32"`/`"fp16"`/`None`，作为持久化 autotune 配置查找的 key 之一。这说明存储 dtype 会影响选中的 config（不同 dtype 对应不同的最优 tile）。

`_triton_bwd_grad_tensor_like`——决定缓冲区 dtype 与显存代价的核心函数：

[src/ffpa_attn/triton/__init__.py:209-250](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/__init__.py#L209-L250) — `grad_storage_dtype=None` 时 `torch.empty_like(tensor)`（保持激活 dtype），否则 `torch.empty_like(tensor, dtype=grad_storage_dtype)`。注释（223–236 行）给出了显存代价的精确计算：一个 fp32 缓冲区花 `tensor.numel() * 4` 字节，典型大 D 自注意力 `B=1, Hq=32, Nq=Nkv=8192, D=512` 是 `1*32*8192*512*4 = 536870912` 字节 = **512 MiB**，且 GQA/MQA 在头扩展后同样按扩展后的体积计费。

int 编码解码：

[src/ffpa_attn/triton/__init__.py:253-263](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/__init__.py#L253-L263) — `_grad_kv_storage_dtype_from_code`：`0→None, 1→fp32, 2→fp16`，其它抛 `ValueError`。`_bwd_triton` 的 schema 里 `grad_kv_storage_dtype_code` 和 `grad_q_storage_dtype_code` 都是 int（见 [src/ffpa_attn/triton/__init__.py:354-363](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/__init__.py#L354-L363)），因为 torch op schema 不能直接传 dtype 对象。

三个梯度缓冲区的分配（注意 `dq.zero_()`）：

[src/ffpa_attn/triton/__init__.py:400-403](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/__init__.py#L400-L403) — `dq = _triton_bwd_grad_tensor_like(q, grad_q_storage_dtype)` 后紧跟 `dq.zero_()`，注释「atomic_add accumulates from the initial value; must be zero」。这是因为融合路径（`use_dkdvdq_fusion`）下 `dQ` 用 `tl.atomic_add` 累加，初始值必须为零；`dk`/`dv` 不需要 zero（它们是 store 覆盖式写入）。

`dkdvdq` 融合路径里 `dQ` 用 atomic_add 的原因（以及 bf16 在 SM<90 的退化）：

[src/ffpa_attn/triton/_ffpa_bwd.py:1128-1144](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd.py#L1128-L1144) — 注释解释：融合后 pid 只索引 K 块，`dQ` 失去唯一拥有者（多个 K 块程序贡献同一 Q tile），必须用 `tl.atomic_add`；bf16 硬件 atomic 只在 SM90+ 有，SM<90 退化成 CAS 循环，在 L20 上 128 个 K 块争用同一 dq 元素时性能严重下降（比 fp16 慢 2.4 倍）。缓解手段正是 `grad_q_storage_dtype=fp32`（用 fp32 硬件原子，比 bf16 CAS 快 1.9 倍，但 dq 显存翻倍）。

#### 4.4.4 代码实践

实践目标：算出 `grad_kv_storage_dtype=fp32` 在一个具体形状下的显存代价，并对照 `_triton_bwd_grad_tensor_like` 的注释验证。

1. 实践目标：手算 fp32 `DK`/`DV` 缓冲区的显存，理解为何注释称之为「expensive」。
2. 操作步骤：
   - 取形状 `B=1, Hq=32, Nq=Nkv=8192, D=512`，dtype 原本是 bf16。
   - bf16 下单个缓冲区字节数 = `1*32*8192*512*2`（bf16 每元素 2 字节）。
   - fp32 下单个缓冲区字节数 = `1*32*8192*512*4`。
   - `DK` 和 `DV` 各一个，所以 `grad_kv_storage_dtype=fp32` 多用的显存 = `2 * (fp32字节 - bf16字节)`。
3. 需要观察的现象：fp32 单缓冲 536870912 字节 = 512 MiB，bf16 单缓冲 256 MiB；`DK`+`DV` 两个缓冲从 512 MiB 涨到 1024 MiB，**多用 512 MiB**。这与注释里的 512 MiB 数字一致。
4. 预期结果：手算 `1*32*8192*512*4 = 536870912`，除以 \(1024^2\) 得 512 MiB。可写一段小程序核对：

```python
# 示例代码：核对显存代价（不调用 GPU）
B, Hq, N, D = 1, 32, 8192, 512
elems = B * Hq * N * D
bf16_bytes = elems * 2
fp32_bytes = elems * 4
print(fp32_bytes)                       # 536870912
print(fp32_bytes / (1024**2), "MiB")    # 512.0 MiB
# DK + DV 两个缓冲：bf16→fp32 多用
print(2 * (fp32_bytes - bf16_bytes) / (1024**2), "MiB")  # 512.0 MiB
```

#### 4.4.5 小练习与答案

**练习 1**：`split_launch=True` 能消除「score 被算两遍」的问题吗？

**答案**：不能。模块 docstring（`_ffpa_bwd.py:50-57`）和 `split_launch` 处的注释（2099–2103 行）都强调：重复重算 score/dP/dS 是 kernel 结构本身的问题，dKdV 角色和 dQ 角色各自重建一次。`split_launch` 只是把这两个角色从「一次 launch 内顺序调用」拆成「两次独立 launch」，score 仍算两遍。要真正只算一次 score，需要开启 `FFPA_TRITON_BWD_FUSE_DKDVDQ=1` 的融合路径（代价是 `dQ` 退化为 atomic_add）。

**练习 2**：为什么 `dq` 要 `zero_()` 而 `dk`/`dv` 不用？

**答案**：`dQ` 在融合路径下用 `tl.atomic_add` 累加（多个 K 块程序贡献同一 Q tile），atomic_add 是「读旧值 → 加 → 写」，初始值必须为零否则结果偏大。`dK`/`dV` 是覆盖式 store（第一个 Q 块直接 `store`，后续才 load/add/store），不需要预置零。即使非融合路径下 `dQ` 是普通 store，wrapper 也统一 `zero_()` 以兼容融合路径，代价可忽略。

---

## 5. 综合实践

把四个开关串起来，设计一次「在 Hopper 上榨取最高 `dK`/`dV` 精度」的反向调用，并对照说明每个开关的角色与代价。

**任务**：针对 `B=1, H=2, N=128, D=512` 的 bf16 因果自注意力，写一段脚本，依次尝试三种反向配置，对比 `k.grad` 与 SDPA 参考的 `max_abs_err`：

1. **基线**：默认 `backward_backend="triton"`（不开任何高级开关）。
2. **SM90 寄存器精度**：`TritonBackend(backward=True, enable_tma=True, persist_dkdv=True)`。
3. **非 Hopper 友好的 HBM 精度**：`TritonBackend(backward=True, grad_kv_storage_dtype=torch.float32)`（这条在任何 Triton 支持的 GPU 上都能用）。

**步骤**：

1. 参考 `tests/test_ffpa_bwd.py:170-195` 与 `tests/test_ffpa_bwd.py:522-526` 的写法，分别用上面三种 `backward_backend` 调 `ffpa_attn_func(..., is_causal=True)`，`out.sum().backward()`。
2. 用 `_sdpa_ref_grads`（或自己用 `F.scaled_dot_product_attention` 算）得到参考 `dk_ref`。
3. 对每种配置算 `(k.grad - dk_ref).abs().max()`。
4. 对照 `_triton_bwd_grad_tensor_like` 的注释（`__init__.py:223-236`）说明：配置 3 因为 `DK`/`DV` 改成 fp32，额外占用 `2 * 512 MiB = 1 GiB`（针对该形状）；配置 2 不增加 HBM 占用（累加在寄存器），但只在 SM90 上生效，且因外层循环换成 D 片段、寄存器压力上升。

**预期观察**：

- 在 Hopper 上，配置 2 的 `k.grad` 误差应最小（fp32 寄存器累加，只最终 store 一次）。
- 配置 3 在任意 Triton GPU 上都能改善 `k.grad` 精度，但显存代价最大。
- 配置 1 在 bf16 + causal 下 `k.grad` 误差最大（多次 bf16 往返舍入）。

> 待本地验证：上述误差排序依赖具体硬件与 Triton 版本；若无 Hopper，配置 2 会被 `is_sm90_tma_backward_supported` 挡掉、自动退化为通用路径，此时只剩配置 1 与配置 3 可比。

## 6. 本讲小结

- 四个反向高级开关（`enable_tma`/`enable_ws`/`persist_dkdv`/`split_launch`）都挂在 `TritonBackend` 上，默认全 `False`；`persist_dkdv` 硬依赖 `enable_tma` 且 `backward=True`，其余反向专用开关只要求 `backward=True`，违反会在 `__post_init__` 立即 assert 失败。
- `enable_tma=True` 且 `seqlen_q >= 8` 且通过 `is_sm90_tma_backward_supported` 探测（SM90+、fp16/bf16、列连续）时，反向切到 SM90 专用 TMA 分支，用 `TensorDescriptor` 加载代替裸指针加载；`warp_specialize` 在此分支里让加载与 MMA 重叠。
- `persist_dkdv` 用 `_ffpa_bwd_dkdv_persist_sm90` 把 `dk_acc`/`dv_acc` 放在 fp32 寄存器里跨所有 Q 块累加，循环顺序从「外层 Q 块」换成「外层 D 片段」，最终只 store 一次，消除了 bf16/fp16 往返舍入，对 `is_causal=True` 尤其有效。
- `split_launch` 把 dKdV 与 dQ 拆成两次独立 launch（各自 autotune），但不消除 score 重算；真正消除重算的是 `FFPA_TRITON_BWD_FUSE_DKDVDQ=1` 融合路径，代价是 `dQ` 退化为 atomic_add。
- `grad_kv_storage_dtype`/`grad_q_storage_dtype` 通过 `_triton_bwd_grad_tensor_like` 改变 `DK`/`DV`/`DQ` 缓冲区的存储 dtype，是「非 Hopper 硬件也能用」的精度路线；fp32 单缓冲对一个典型大 D 形状要花 512 MiB，DK+DV 两个缓冲显存翻倍。
- 两条精度路线（persist 寄存器 vs fp32 HBM 存储）互补：SM90 上优先 persist（不增显存），SM<90 用 fp32 存储（增显存但保精度）。

## 7. 下一步学习建议

- **进入自动调优**：本讲的 SM90 路径大量依赖持久化 autotune 配置查找（`lookup_bwd_sm90_persistent_config`）。建议接着学 u8-l1（Triton 自动调优机制）和 u8-l3（运行时配置查找与就近匹配），搞懂 `grad_kv_storage_dtype` 如何作为 autotune key 影响选中的 config。
- **对比 CuTeDSL 反向**：CuTeDSL 后端（u6 系列）在 SM90 上也走 producer/consumer 流水线与 tile scheduler，与本讲的 `warp_specialize` 思路相通但实现路径不同，对照阅读能加深对「Hopper 反向如何榨取性能」的理解。
- **阅读 SM90 专用 kernel**：如果想进一步看 `_ffpa_bwd_dkdv_persist_sm90` 与非持久版本的字节级差异，建议直接 diff `_ffpa_bwd_dkdv_sm90`（[src/ffpa_attn/triton/_ffpa_bwd_sm90.py:58-286](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd_sm90.py#L58-L286)）与 `_ffpa_bwd_dkdv_persist_sm90`（547–759 行），重点看循环顺序与 store 位置的差别。
