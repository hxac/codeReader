# 变长注意力 ffpa_attn_varlen_func

## 1. 本讲目标

本讲是第 2 单元（公共 API）的最后一篇，承接 [u2-l1 ffpa_attn_func 签名、张量布局与返回](./u2-l1-ffpa-attn-func-signature-layout.md) 里学过的「密集注意力」入口，讲解它的「变长版兄弟」`ffpa_attn_varlen_func`。

学完本讲，你应该能够：

- 说清楚**为什么**训练里序列长度不一，直接用 `ffpa_attn_func`（密集 `[B,H,N,D]`）会浪费算力与显存，而 packed THD 布局如何解决。
- 看懂 `ffpa_attn_varlen_func` 的完整签名：packed `[T,H,D]` 张量、`cu_seqlens_q`/`cu_seqlens_k` 的「B+1 累计偏移」约定、`max_seqlen_q/k` 与 `return_lse` 的用法。
- 理解它的**实现限制**：目前**只有 CuTeDSL 后端**、仅在 **SM8x/SM90**、**大 head_dim（D≥320，且对齐）**、**fp16/bf16** 上可用，且 `dropout_p`、`window_size`、`softcap` 等几乎所有 FlashAttention 扩展选项都会被**显式拒绝**（绝不静默丢弃）。
- 能照着源码画出从公共函数到 CuTeDSL `torch op` 的整条调用链，并解释为什么 varlen 走「自己管 autograd」的路径而不是复用密集路径的 `FFPAAttnFunc`。

## 2. 前置知识

本讲默认你已经学完 u2-l1（密集 `ffpa_attn_func` 的 `[B,Nh,N,D]` 布局、`scale` 默认值、返回单个张量 `O`），也大致知道 FFPA 主攻**大 head_dim（D>256）prefill**（见 [u1-l1](./u1-l1-what-is-ffpa-split-d.md)）。下面补充三个本讲要用到的新术语。

### 2.1 序列长度不齐：padding 的浪费

真实训练里，一个 batch 里的序列长度往往差别很大（比如 1024、2048、4096 token 混在一起）。如果用密集 API `[B,H,N,D]`，就必须把所有序列**补齐（padding）到最长的那条** `N_max`，短序列后面填 0，再用 mask 把 padding 位置屏蔽掉。这样 GPU 实际算了一大堆「会被丢弃」的 padding 乘法，既浪费算力，又浪费显存（`B×N_max` 远大于真正的 token 总数）。

### 2.2 packed THD 布局

`THD` 是 FlashAttention 系列对变长输入的紧凑布局约定：

- 把整个 batch 的所有 token **首尾相接拼成一根长一维**，记总 token 数为 `T`。
- 张量形状是 `[T, H, D]`：第 0 维是「所有 token 拼起来的序列轴」，第 1 维是注意力头 `H`，第 2 维是 head_dim `D`。

这样**没有任何 padding**，显存里只存真正有用的 token。代价是：kernel 必须知道「这条长序列里哪一段属于第 0 条原始序列、哪一段属于第 1 条」——这件事由下面的 `cu_seqlens` 描述。

### 2.3 cu_seqlens：累计偏移（cumulative offsets）

`cu_seqlens`（cumulative sequence lengths）是一个长度为 `B+1` 的 `int32` 张量，记录每条原始序列在拼接长序列里的**起始下标**。规则只有两条：

1. **第一个元素必须是 0**：`cu_seqlens[0] == 0`。
2. **最后一个元素必须等于总 token 数**：`cu_seqlens[-1] == T`。

中间第 `i` 个元素就是前 `i` 条序列的长度之和。于是第 `i` 条原始序列在长序列里占据下标区间 `[cu_seqlens[i], cu_seqlens[i+1])`。

举例：3 条序列，长度分别是 `32, 17, 64`，则

```
cu_seqlens = [0, 32, 49, 113]
              │   │   │    │
              │   │   │    └─ T = 32+17+64 = 113（总 token 数）
              │   │   └────── 第 2 条从 49 开始
              │   └────────── 第 1 条从 32 开始（= 前 1 条长度之和）
              └────────────── 必须从 0 开始
```

注意力计算时，**只有同属一条原始序列的 Q 和 K 才会互相 attend**；跨序列的边界由 kernel 内部根据 `cu_seqlens` 切断，不会串味。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [src/ffpa_attn/ffpa_attn_interface.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/ffpa_attn_interface.py) | 定义公共入口 `ffpa_attn_varlen_func`：签名、docstring、把参数转交 `FFPAAttnVarlenFunc.apply`。本讲主角。 |
| [src/ffpa_attn/functional.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py) | `FFPAAttnVarlenFunc` 与被 `@torch._dynamo.disable` 守卫的 `_ffpa_varlen_apply`：制造 autograd / `torch.compile` 边界，最终落到 CuTeDSL 的 `_ffpa_attn_varlen_cute`。 |
| [src/ffpa_attn/cute/__init__.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/__init__.py) | CuTeDSL 后端的入口 shim：`_ffpa_attn_varlen_cute`（逐层校验 + 默认值 + 分发）、`_check_supported_options`（选项拒绝）、`_ffpa_attn_varlen_impl`，以及 varlen 专用的 `torch op` 注册与 `register_autograd`。 |
| [src/ffpa_attn/cute/_utils.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_utils.py) | 常量（`MIN_SUPPORTED_HEAD_DIM=320`、`SM90_SUPPORTED_HEAD_DIM=512`、`SM80_SUPPORTED_HEAD_DIM=1024`、`SM80_FWD_SPLIT_D_CHUNK=32`）与 `cu_seqlens`/head_dim 校验函数。 |
| [src/ffpa_attn/cute/README.md](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/README.md) | CuTeDSL 包的内部说明，含一段可直接抄的 varlen 调用示例。 |
| [tests/test_ffpa_cute_sm80.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_ffpa_cute_sm80.py) | varlen 正确性测试：用 `lengths=[32,17,64]` 构造 `cu_seqlens`，与「逐段 SDPA 拼接」的参考实现对比前向与反向。 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：4.1 为什么需要变长注意力（动机）；4.2 公共函数 `ffpa_attn_varlen_func`（怎么用）；4.3 入口 shim `_ffpa_attn_varlen_cute`（怎么校验与分发）；4.4 选项守门员 `_check_supported_options`（哪些选项会被拒绝）。

### 4.1 为什么需要变长注意力：dense padding 的浪费与 packed THD 的解法

#### 4.1.1 概念说明

上一讲（u2-l1）的 `ffpa_attn_func` 接收 `[B, Nh, N, D]`，要求同一个 batch 里**所有序列等长** `N`。当训练样本天然不等长时，常规做法是 padding 到 `N_max`：

- 算力浪费：注意力是关于序列长度**二次**的，padding 部分仍在做 `QK^T`，只是结果被 mask 丢掉。
- 显存浪费：张量第 2 维是 `B×N_max`，远大于真实 token 总数 `T`。

packed THD 的思路是**彻底不 padding**：把 batch 里所有 token 拼成一维 `[T,H,D]`，用 `cu_seqlens` 标出每条序列的边界，kernel 内部按边界做**分段注意力**——第 `i` 条序列的 Q 只看它自己的 K/V，互不干扰。这样算力和显存都只花在真实 token 上。

> 一句话：dense API 解决「等长 batch 的大 D 注意力」，varlen API 解决「**不等长** batch 的大 D 注意力」，且同样主打 **prefill + 大 D**。

#### 4.1.2 核心流程

把一次 batch 变长注意力拆成三步：

1. **拼接**：把 `B` 条不等长序列沿序列轴首尾相接，得到 `q:[T_q,H_q,D]`、`k/v:[T_k,H_kv,D]`，并构造累计偏移 `cu_seqlens_q`、`cu_seqlens_k`（长度均为 `B+1`）。
2. **单次 kernel**：CuTeDSL kernel 一次吃下整根拼接张量，内部按 `cu_seqlens` 把每条序列当作一个独立的注意力块处理。**没有 Python 层的 for 循环、没有逐序列 launch**，这也是它快的关键。
3. **输出仍按 THD**：输出 `out` 形状 `[T_q,H_q,D]`，与 `q` 的拼接顺序一一对应；调用方自己再按 `cu_seqlens_q` 切回各条序列即可。

#### 4.1.3 源码精读

CuTeDSL kernel 「天然吃 packed `[T,H,D]`、无需转置、无需逐序列循环」这件事，写在 `_ffpa_attn_varlen_cute` 的 docstring 里：

[src/ffpa_attn/cute/__init__.py:402-426](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/__init__.py#L402-L426) — 函数 docstring 明确说：varlen 路径**绕过**密集路径的 `FFPAAttnFunc`，且「The CuTeDSL kernel consumes packed `[T, H, D]` layout natively — no transpose, no per-sequence loop.」

对照密集路径：密集 CuTeDSL 入口 `_ffpa_attn_forward_cute` 在 [src/ffpa_attn/cute/__init__.py:292-299](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/__init__.py#L292-L299) 还要先做 `[B,H,N,D]↔[B,N,H,D]` 的转置（`_bhnd_to_bnhd`），而 varlen 路径连这步都省了——因为 `[T,H,D]` 正好就是 CuTeDSL 原生布局。

#### 4.1.4 代码实践（源码阅读型）

1. 实践目标：直观感受 padding 浪费有多大。
2. 操作步骤：读 [tests/test_ffpa_cute_sm80.py:250-256](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_ffpa_cute_sm80.py#L250-L256)，注意它用 `lengths=[32,17,64]` 构造 `cu_seqlens`。心算一下：若改用密集 `[B,H,N,D]`（`B=3`），padding 到 `N_max=64` 后第 2 维是 `3×64=192`；而 packed 是 `T=32+17+64=113`。
3. 需要观察的现象：packed 比 dense-padding 少存 `192-113=79` 个 token 位置（约 41%），序列越不齐省得越多。
4. 预期结果：理解「varlen 不是锦上添花，而是治不等长 batch 的根药」。
5. 运行结果：待本地验证（本步为阅读与心算，无需执行）。

#### 4.1.5 小练习与答案

- **练习 1**：若有 4 条序列长度为 `[1024, 2048, 512, 4096]`，写出 `cu_seqlens` 和 `T`。
  - 答案：`cu_seqlens = [0, 1024, 3072, 3584, 7680]`，`T = 7680`。
- **练习 2**：为什么 varlen 路径「没有逐序列 launch」反而更高效？
  - 答案：单次 kernel launch、GPU 持续满载，省去 `B` 次 Python→GPU 往返与启动开销；同时所有 token 共享同一套 kernel 配置，SM 利用率更稳。

---

### 4.2 ffpa_attn_varlen_func：packed THD 签名、cu_seqlens 约定与返回

#### 4.2.1 概念说明

`ffpa_attn_varlen_func` 是 varlen 的**唯一公共入口**，签名刻意对齐 Dao-AILab 的 `flash_attn_varlen_func`，方便从 FlashAttention 代码迁移。和密集 `ffpa_attn_func` 相比，它有三处根本差异：

1. **张量是 3 维 packed**：`q:[T_q,H_q,D]`、`k/v:[T_k,H_kv,D]`，没有 batch 维 `B`——batch 信息搬进了 `cu_seqlens`。
2. **没有回退到 SDPA 的 `meta.fallback()` 短路**：varlen 是 CuTeDSL 专用的，不满足条件就直接报错，不偷偷走 SDPA。
3. **可选返回 `lse`**：`return_lse=True` 时返回 `(out, lse)`，`lse` 是对数域归一化因子（log-sum-exp），形状 `[H_q, T_q]`、fp32，供自定义 loss（如 cross-entropy 与 attention 融合）或 FP32 精度的反向使用。

#### 4.2.2 核心流程

```
ffpa_attn_varlen_func(q, k, v, cu_seqlens_q, cu_seqlens_k, max_seqlen_q, max_seqlen_k,
                      *, dropout_p=0, softmax_scale=None, causal=False,
                      enable_gqa=False, return_lse=False, **kwargs)
        │
        ▼  仅做参数透传，不校验
  FFPAAttnVarlenFunc.apply(...)
        │
        ▼  @torch._dynamo.disable 守卫（制造 autograd/compile 边界）
  _ffpa_varlen_apply(...)  →  _ffpa_attn_varlen_cute(...)  （CuTeDSL）
        │
        ▼  校验 + 选项拒绝 + 选 kernel（SM90 专用 / SM80 回退）
  _ffpa_attn_varlen_impl(...)  →  torch.ops.ffpa_attn._varlen_fwd_cute
```

注意：varlen **没有 `backend=` 参数**——它永远是 CuTeDSL。想用别的后端？docstring 直说「Callers needing other shapes / backends should unpack the batch and call `ffpa_attn_func` per sequence.」，即自己拆 batch 逐条调密集 API。

#### 4.2.3 源码精读

公共函数定义与签名在 [src/ffpa_attn/ffpa_attn_interface.py:184-199](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn_interface.py#L184-L199)：

```python
def ffpa_attn_varlen_func(
  q, k, v,
  cu_seqlens_q: torch.Tensor,
  cu_seqlens_k: torch.Tensor | None,
  max_seqlen_q: int,
  max_seqlen_k: int,
  *, dropout_p=0.0, softmax_scale=None, causal=False,
  enable_gqa=False, return_lse=False, **kwargs,
) -> torch.Tensor | tuple[torch.Tensor, torch.Tensor]:
```

要点（对应 docstring [src/ffpa_attn/ffpa_attn_interface.py:200-255](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn_interface.py#L200-L255)）：

- `cu_seqlens_q`/`cu_seqlens_k` 是 `int32`、CUDA、长度 `B+1`、起始为 0 的累计偏移；`cu_seqlens_k is None` 时**默认等于 `cu_seqlens_q`**（即 self-attention，Q/K 等长同界）。
- `max_seqlen_q`/`max_seqlen_k` 是 batch 内**最大**的单序列长度，用来给 kernel 定 tile/padding 预算；不是 `T`。
- `return_lse=True` 时返回 `lse`，形状 `[H_q, T_q]`、fp32（**头在前、token 在后**，这是 CUDA/FlashAttention 的约定，不是 `[T_q,H_q]`）。
- 不支持的选项会从 `**kwargs` 进来，最终被 `_check_supported_options` 拒绝（见 4.4）。

函数体极薄，只做透传（[src/ffpa_attn/ffpa_attn_interface.py:257-271](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn_interface.py#L257-L271)），真正的活都在 CuTeDSL 侧。

`FFPAAttnVarlenFunc` 与 `_ffpa_varlen_apply` 在 [src/ffpa_attn/functional.py:990-1033](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L990-L1033)：

[src/ffpa_attn/functional.py:990-1020](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L990-L1020) — `_ffpa_varlen_apply` 被 `@torch._dynamo.disable` 装饰，意思是 `torch.compile` 追踪到这里会**主动断图**，把整段当作不透明黑盒交给 autograd，从而保住 varlen 自己注册的反向（见 4.3）。这与密集路径 `_ffpa_apply` 的设计完全一致（详见 [u3-l5 torch.compile 兼容与自定义算子](./u3-l5-torch-compile-custom-ops.md)）。

`lse` 形状在 fake 实现里被钉死为 `(num_head, total_q)`（头在前）：

[src/ffpa_attn/cute/__init__.py:798-800](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/__init__.py#L798-L800) — `out = q.new_empty((total_q, num_head, head_dim_v))`，`lse = q.new_empty((num_head, total_q), dtype=torch.float32)`。测试里也用 `assert lse.shape == (num_heads, total_q)` 锁定（[tests/test_ffpa_cute_sm80.py:246](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_ffpa_cute_sm80.py#L246)）。

#### 4.2.4 代码实践（源码阅读型）

1. 实践目标：把「docstring 承诺」与「fake 实现的形状」对上号。
2. 操作步骤：打开 docstring [src/ffpa_attn/ffpa_attn_interface.py:230-231](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn_interface.py#L230-L231)（`return_lse` 说明）与 fake 实现 [src/ffpa_attn/cute/__init__.py:798-800](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/__init__.py#L798-L800)。
3. 需要观察的现象：两处对 `lse` 形状的描述是否一致（都应是 `[H_q, T_q]`、fp32）。
4. 预期结果：确认 `out` 是 `[T_q,H_q,D]`、`lse` 是 `[H_q,T_q]`，**两者第 0、1 维顺序相反**——这是新手最常踩的坑。
5. 运行结果：待本地验证（本步为对照阅读）。

#### 4.2.5 小练习与答案

- **练习 1**：`max_seqlen_q` 应该填 `T_q`（总 token 数）吗？
  - 答案：**不应该**。它填的是 batch 内**单条**序列的最大长度（如 `max(lengths)`），用来给 kernel 定预算；`T_q` 是所有序列长度之和，语义完全不同。
- **练习 2**：`cu_seqlens_k=None` 等价于什么？
  - 答案：等价于 `cu_seqlens_k = cu_seqlens_q`，即 K/V 与 Q 共用同一套序列边界（self-attention 场景）。代码见 [src/ffpa_attn/cute/__init__.py:463-464](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/__init__.py#L463-L464)。

---

### 4.3 _ffpa_attn_varlen_cute：逐层校验、默认值与 CuTeDSL 分发

#### 4.3.1 概念说明

`_ffpa_attn_varlen_cute` 是 CuTeDSL 后端为 varlen 写的**入口 shim**（垫片函数），职责是把公共 API 传下来的「裸参数」收拾干净再交给底层 kernel：先拒绝不支持的选项，再做一套**逐项校验**（秩、形状、dtype、cu_seqlens、GQA、head_dim 范围），然后选 kernel（SM90 专用 or SM80 回退）。它和密集路径最大的架构差异是：**varlen 自己管 autograd**——通过 `torch.library.custom_op` + `register_autograd` 注册专属反向，而不是复用密集路径的 `FFPAAttnFunc`。

#### 4.3.2 核心流程

`_ffpa_attn_varlen_cute` 内部的校验顺序（顺序很重要，先拒绝「整体不支持」再查「这次传错」）：

1. **选项拒绝**：调 `_check_supported_options`（4.4），把 `window_size`/`softcap`/`attn_mask`/... 全部非默认值一次性拒掉。
2. **秩校验**：`q/k/v` 必须 3 维 `[T,H,D]`。
3. **形状一致性**：`k.shape == v.shape`（K/V 必须同形）。
4. **dtype 校验**：`q/k/v` 必须 fp16/bf16 且三者同 dtype。
5. **cu_seqlens 默认与校验**：`cu_seqlens_k` 缺省时复制 `cu_seqlens_q`；两者必须 `int32`、长度相等且 `≥2`。
6. **GQA 校验**：`enable_gqa=False` 时 `H_q` 必须 `== H_kv`；`H_q` 必须能被 `H_kv` 整除。
7. **head_dim 范围**：`MIN_SUPPORTED_HEAD_DIM(320) ≤ D ≤ cute_max_supported_head_dim()(1024)`。
8. **tensor 级硬约束**：调 `_require_cute_supported`（设备、架构 `≥8.0`、head_dim 对齐、dtype）。
9. **分发**：调 `_ffpa_attn_varlen_impl` → `torch.ops.ffpa_attn._varlen_fwd_cute`。

kernel 选择由 `_use_sm90_specialized` 决定：仅当 `major==9`（Hopper）且 **对称的** `head_dim`、`head_dim_v` 都落在 `[320,512]` 时走 SM90 专用 kernel；否则（其它架构，或 SM90 上 `D>512`，或 q/v head_dim 不对称）走 SM80 通用 Split-D 回退路径。

#### 4.3.3 源码精读

逐层校验主体在 [src/ffpa_attn/cute/__init__.py:427-493](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/__init__.py#L427-L493)。几个关键片段：

[src/ffpa_attn/cute/__init__.py:443-461](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/__init__.py#L443-L461) — 秩、`k/v` 同形、fp16/bf16、dtype一致的校验，分别抛 `ValueError`/`TypeError`。

[src/ffpa_attn/cute/__init__.py:463-472](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/__init__.py#L463-L472) — `cu_seqlens_k` 缺省复制 `cu_seqlens_q`；`int32` 校验、长度相等且 `≥2` 校验。

[src/ffpa_attn/cute/__init__.py:474-484](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/__init__.py#L474-L484) — GQA 两道关：`enable_gqa=False` 但头数不等 → `ValueError`；`H_q % H_kv != 0` → `ValueError`。

[src/ffpa_attn/cute/__init__.py:486-493](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/__init__.py#L486-L493) — head_dim 范围用 `cute_max_supported_head_dim()` 作上界（恒为 `SM80_SUPPORTED_HEAD_DIM=1024`，见 [src/ffpa_attn/cute/__init__.py:149-160](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/__init__.py#L149-L160)），下界 `MIN_SUPPORTED_HEAD_DIM=320`（[src/ffpa_attn/cute/_utils.py:21](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_utils.py#L21)）；随后调 `_require_cute_supported` 做设备/架构/对齐校验。

kernel 路由谓词 `_use_sm90_specialized` 在 [src/ffpa_attn/cute/__init__.py:188-200](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/__init__.py#L188-L200)：`major==9 and 320≤head_dim≤512 and 320≤head_dim_v≤512`。

「varlen 自己管 autograd」体现在它用的是 `@torch.library.custom_op` + `register_autograd`，而不是密集路径的 `torch.library.define/impl`：

[src/ffpa_attn/cute/__init__.py:708-745](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/__init__.py#L708-L745) — `_varlen_fwd_custom` 用 `@custom_op("ffpa_attn::_varlen_fwd_cute", mutates_args=())` 注册前向，先 `_trim_trailing_empty_varlen_segments` 去掉尾部全空段，再解码 `window_size`，最后按设备选 `_ffpa_attn_forward_sm90` 或 `_ffpa_attn_forward_sm80`。

[src/ffpa_attn/cute/__init__.py:899-944](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/__init__.py#L899-L944) — `_varlen_fwd_setup_context` 把反向需要的 `(q,k,v,out,lse,cu_seqlens_q,cu_seqlens_k)` 存进 ctx；`_varlen_fwd_backward` 调 `torch.ops.ffpa_attn._varlen_bwd_cute`；最后 `register_autograd("ffpa_attn::_varlen_fwd_cute", _varlen_fwd_backward, setup_context=...)` 把前向与反向绑成一对。

> 为什么 varlen 不复用 `FFPAAttnFunc`？因为密集路径的 `FFPAAttnFunc.backward` 要在「一前向多反向（CUDA 前向可配 Triton/SDPA 反向）」之间动态选反向（见 [u3-l4](./u3-l4-autograd-function-dispatch.md)），结构复杂；而 varlen 是 CuTeDSL 前后端捆绑的单一组合，用 `custom_op + register_autograd` 直接绑死最简单清晰，且天然 `torch.compile` 友好。

#### 4.3.4 代码实践（源码阅读型：跟踪调用链）

1. 实践目标：在源码里走通「公共函数 → CuTeDSL shim → torch op」全链路。
2. 操作步骤：依次打开并对照——
   - 公共函数透传：[ffpa_attn_interface.py:257-271](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn_interface.py#L257-L271)
   - dynamo 断图：[functional.py:990-1020](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L990-L1020)
   - 校验+分发：[cute/__init__.py:496-507](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/__init__.py#L496-L507)
   - torch op：[cute/__init__.py:708-745](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/__init__.py#L708-L745)
3. 需要观察的现象：每一层都「只多做一件事」——公共层透传、functional 层断图、cute 层校验+选 kernel、op 层真正 launch。
4. 预期结果：能口头复述这条链路上每一步的职责。
5. 运行结果：待本地验证（本步为阅读跟踪）。

#### 4.3.5 小练习与答案

- **练习 1**：在 SM90（Hopper）上跑 `head_dim=640` 的 varlen，会走哪条 kernel 路径？为什么？
  - 答案：走 **SM80 通用 Split-D 回退**路径。因为 `_use_sm90_specialized` 要求 `head_dim≤512`，`640>512` 不满足，即便设备是 SM90 也会落到 SM80 fallback（[src/ffpa_attn/cute/__init__.py:188-200](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/__init__.py#L188-L200)）。此时还要求 `D % 32 == 0`（`SM80_FWD_SPLIT_D_CHUNK`，[_utils.py:38](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/_utils.py#L38)）。
- **练习 2**：为什么 `_varlen_fwd_custom` 开头要 `_trim_trailing_empty_varlen_segments`？
  - 答案：去掉「Q、K 都为空」的尾部段，避免 kernel 对长度为 0 的段做无意义 launch（[src/ffpa_attn/cute/__init__.py:683-706](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/__init__.py#L683-L706) 与 [724-726](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/__init__.py#L724-L726)）。

---

### 4.4 _check_supported_options：选项白名单与「无静默回退」

#### 4.4.1 概念说明

FlashAttention 系列的 varlen API 有一大堆扩展选项（`window_size` 滑窗、`softcap`、`alibi_slopes`、`block_table` paged KV、`num_splits`...）。FFPA 的 CuTeDSL kernel **目前只实现了「带可选因果掩码的纯注意力」**，其余选项一律没有 kernel 实现。`_check_supported_options` 就是那个**守门员**：凡是传了非默认值的「不支持选项」，它一次性收集起来，**用一条报错把所有违规选项点名**，然后抛 `NotImplementedError`。

设计哲学是「**绝不静默丢弃**」（no silent strip-to-default）：宁可立刻报错让你改，也不偷偷把 `softcap=0.5` 当成 `0` 算出一个你不知道错了的结果。这与密集路径 `ffpa_attn_func` 的 SDPA 回退风格不同——密集路径在硬件/head_dim 不匹配时会**静默回退 SDPA**（见 [u1-l4](./u1-l4-one-line-sdpa-monkey-patch.md)），而 varlen 路径**没有任何静默回退**，不满足就直接拒绝。

#### 4.4.2 核心流程

```
检查每个「不支持选项」是否为非默认值
        │
   收集所有违规项 → unsupported 列表
        │
  unsupported 非空？
   ├─ 是 → raise NotImplementedError(点名所有违规项 + 建议 forward_backend='triton')
   └─ 否 → 正常返回，继续后续校验
```

被拒的选项清单（见下方源码）：`dropout_p`、`window_size`、`sink`、`attention_mask`（也认 `attn_mask` 别名）、`block_mask`、`softcap`、`score_mod`、`aux_tensors`、`seqused_k`、`block_table`、`num_splits`、`alibi_slopes`。

#### 4.4.3 源码精读

`_check_supported_options` 全文在 [src/ffpa_attn/cute/__init__.py:65-125](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/__init__.py#L65-L125)。核心是「先收集、再一次性报错」：

[src/ffpa_attn/cute/__init__.py:95-125](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/__init__.py#L95-L125) — 用 `unsupported` 列表累加每个非默认选项，最后若非空就拼成一条 `NotImplementedError`，信息里同时给出**修复建议**：`Use forward_backend='triton' when these options are required.`（即「这些选项请改用 Triton 后端的密集 API」）。

`_ffpa_attn_varlen_cute` 把所有可能从 `**kwargs` 溜进来的扩展选项一一取出，喂给守门员：

[src/ffpa_attn/cute/__init__.py:427-441](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/__init__.py#L427-L441) — 注意 `attention_mask=kwargs.get("attention_mask", kwargs.get("attn_mask"))` 同时认两个别名；其余选项都从 `kwargs` 取默认 `None` 再交给 `_check_supported_options` 判定。

整张「不支持选项」表也写进了 [src/ffpa_attn/cute/README.md:177](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/README.md#L177)（Varlen extras 那行），可作为速查表。

#### 4.4.4 代码实践（源码阅读型：构造一个被拒用例）

1. 实践目标：亲眼看到「无静默回退」的报错长什么样。
2. 操作步骤：读 [src/ffpa_attn/cute/__init__.py:120-125](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/__init__.py#L120-L125) 的报错模板。设想你调用 `ffpa_attn_varlen_func(..., softcap=0.5, window_size=(128,0))`。
3. 需要观察的现象：报错会把 `softcap` 和 `window_size` **同时**点出来（一条信息里两个名字），而不是先撞 `softcap` 报错、改完再撞 `window_size`。
4. 预期结果：理解「收集后一次性报错」对调试体验的好处——一次看到所有要改的选项。
5. 运行结果：待本地验证（若有 SM8x/SM90 GPU，可真跑一次确认报错文本）。

#### 4.4.5 小练习与答案

- **练习 1**：为什么 `_check_supported_options` 要「先收集全部、再一次性报错」，而不是撞到一个报一个？
  - 答案：让调用者一次看清所有违规项，避免「改一个、跑一次、再撞下一个」的低效循环（[src/ffpa_attn/cute/__init__.py:88-94](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/cute/__init__.py#L88-L94) 的 docstring 明说「one actionable error rather than ... silent semantic divergence」）。
- **练习 2**：varlen 路径为什么不像密集路径那样，遇到不支持的选项就「静默回退 SDPA」？
  - 答案：varlen 没有密集 SDPA 等价物可直接兜底（SDPA 本身没有 packed-THD varlen 入口）；而且静默把 `softcap`/`window` 丢掉会算出**语义错误**的结果，比直接报错危险得多。所以策略是「显式拒绝 + 建议改用 Triton 密集 API」。

## 5. 综合实践

把本讲四个模块串起来，亲手写一个最小可运行的 varlen 调用，并验证输出形状。**目标**：用两条长度分别为 `1024`、`2048` 的序列，构造 packed THD 输入与 `cu_seqlens_q`，在 CuTeDSL 后端（SM90 专用路径）上跑一次前向，确认 `out` 形状为 `[T_q, H_q, D]`，并尝试 `return_lse=True` 看 `lse` 形状。

```python
# 示例代码（非项目原有代码，仿照 src/ffpa_attn/cute/README.md 第 55-82 行的 varlen 示例编写）
import torch
from ffpa_attn import ffpa_attn_varlen_func

# 1) 两条不等长序列
lengths = [1024, 2048]
total_q = sum(lengths)          # = 3072
num_heads, head_dim = 32, 512   # D=512 落在 SM90 专用范围 [320,512] 内

# 2) packed THD: [T_q, H_q, D] / [T_k, H_kv, D]，self-attn 故三者同形
q = torch.randn(total_q, num_heads, head_dim,
                dtype=torch.bfloat16, device="cuda", requires_grad=True)
k = torch.randn_like(q)
v = torch.randn_like(q)

# 3) cu_seqlens_q：B+1 个累计偏移，int32，首元素 0，末元素 = T_q
cu_seqlens_q = torch.tensor([0, 1024, 3072], dtype=torch.int32, device="cuda")

# 4) 调用：varlen 恒走 CuTeDSL，无需 backend=；self-attn 故 cu_seqlens_k 可省（默认 = cu_seqlens_q）
out, lse = ffpa_attn_varlen_func(
    q, k, v,
    cu_seqlens_q=cu_seqlens_q,
    cu_seqlens_k=None,              # 默认 = cu_seqlens_q（self-attention）
    max_seqlen_q=max(lengths),      # = 2048（单条最大长度，不是 total_q）
    max_seqlen_k=max(lengths),
    softmax_scale=None,             # 默认 1/sqrt(D)
    causal=True,
    return_lse=True,
)

print(out.shape)   # 预期 torch.Size([3072, 32, 512]) == [T_q, H_q, D]
print(lse.shape)   # 预期 torch.Size([32, 3072])     == [H_q, T_q]  （头在前！）

# 5) 反向：varlen 自带 register_autograd，直接 .backward() 即可
out.sum().backward()
print(q.grad.shape)  # 预期 torch.Size([3072, 32, 512])
```

操作步骤与预期：

1. 按上面构造输入，注意 `cu_seqlens_q = [0, 1024, 3072]`（首 0、末等于 `total_q`、中间是前一条长度之和）。
2. `out.shape` 应为 `[3072, 32, 512]`；`lse.shape` 应为 `[32, 3072]`（**注意 `lse` 是头在前**，这是 4.2 强调的坑）。
3. 反向 `.backward()` 应正常返回，`q.grad.shape == q.shape`。
4. 进阶：把 `causal=True` 改掉、或把 `head_dim` 改成 `640`（SM80 回退路径，需 `640%32==0`），观察是否仍能跑通。
5. **错误实验**：试着加一个 `softcap=0.5` 或 `window_size=(128, 0)`，确认会立即抛 `NotImplementedError` 且把违规项点名（验证 4.4 的「无静默回退」）。

> 运行前提：需要一张 **SM8x（如 A100/L20）或 SM90（Hopper）** 的 NVIDIA GPU，且 head_dim 落在 `[320, 1024]`、对齐 `32`。**若本地无此类 GPU，本实践为「待本地验证」**——但仍可对照源码静态推演每一步的形状与报错。

## 6. 本讲小结

- `ffpa_attn_varlen_func` 是 FFPA 的**变长注意力**入口，对齐 `flash_attn_varlen_func`；输入是 packed THD `[T,H,D]`（无 batch 维、无 padding），batch 边界用 `cu_seqlens`（`B+1` 个累计偏移、`int32`、首 0 末 `T`）描述。
- 它**目前仅 CuTeDSL 后端**可用：SM8x/SM90、大 head_dim（`D∈[320,1024]` 且对齐 `32`）、fp16/bf16；不满足**直接报错，不回退 SDPA**。需要别的后端请自己拆 batch 逐条调密集 `ffpa_attn_func`。
- `max_seqlen_q/k` 填的是**单条最大序列长度**（不是 `T`）；`return_lse=True` 时返回 `(out, lse)`，`out` 是 `[T_q,H_q,D]`，`lse` 是 `[H_q,T_q]` fp32（**头在前**）。
- 调用链：`ffpa_attn_varlen_func`（透传）→ `FFPAAttnVarlenFunc.apply` / `_ffpa_varlen_apply`（`@torch._dynamo.disable` 断图）→ `_ffpa_attn_varlen_cute`（逐层校验 + 选 SM90/SM80 kernel）→ `torch.ops.ffpa_attn._varlen_fwd_cute`。
- varlen **自己管 autograd**：用 `@torch.library.custom_op` + `register_autograd` 绑定专属反向，不复用密集路径的 `FFPAAttnFunc`。
- `_check_supported_options` 是「无静默回退」的守门员：`dropout_p`/`window_size`/`softcap`/`attn_mask`/`block_table`/`num_splits`/`alibi_slopes` 等几乎所有 FlashAttention 扩展选项一旦非默认就**一次性点名拒绝**，并建议改用 `forward_backend='triton'`。

## 7. 下一步学习建议

- 想搞懂「CuTeDSL 后端到底怎么选 kernel、为什么 SM90 有专用路径而 SM80 是通用回退」：进入第 6 单元，先读 [u6-l1 CuTeDSL 后端总览与 SM80/SM90 分发](./u6-l1-cutedsl-overview-sm80-sm90.md)。
- 想理解 varlen 用到的 `register_autograd` / `custom_op` / `register_fake` 与 `torch.compile` 的关系：看 [u3-l5 torch.compile 兼容与 torch.library 自定义算子](./u3-l5-torch-compile-custom-ops.md)。
- 想看 varlen 的 tile 调度与 producer/consumer 流水线（CuTeDSL 怎么在 `[T,H,D]` 上分块、怎么处理变长段边界）：读 [u6-l3 Tile scheduler 与 producer/consumer pipeline](./u6-l3-tile-scheduler-pipeline.md) 与源码 `src/ffpa_attn/cute/utils/seqlen_info.py`。
- 想验证正确性：直接跑 [tests/test_ffpa_cute_sm80.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/tests/test_ffpa_cute_sm80.py) 里的 `test_sm80_cutedsl_varlen_autograd_matches_sdpa`，它用「逐段 SDPA 拼接」作参考实现，是理解 varlen 语义最好的范本。
