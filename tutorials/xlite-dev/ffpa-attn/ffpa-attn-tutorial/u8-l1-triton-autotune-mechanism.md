# Triton 自动调优机制：fast/max 与候选 config

## 1. 本讲目标

FFPA 的默认后端是 **Triton**。一个 Triton kernel 要跑得快，离不开「选择合适的分块大小（tile size）、warp 数、流水线级数」——这组参数就是 **config**。本讲只讲一件事：FFPA 是如何**生成候选 config**、如何在 **fast / max 两档搜索空间**之间切换，以及如何用 **seqlen 分桶**让自动调优的结果可复用、不必每次都重新搜。

读完本讲，你应该能够：

1. 说清楚 **fast 与 max 两种 autotune 模式在候选数量与字段维度上的差异**，并能手算每种组合产生多少个候选。
2. 理解 autotune 的**缓存 key** 由哪些字段组成，为什么 seqlen 要做**分桶（bucketing）**，以及 fast / max 两种分桶粒度的区别。
3. 掌握从用户旋钮 `TritonBackend(autotune=..., autotune_mode=...)` 到 kernel 实际启动的**端到端分发链路**。
4. 正确解读「full-D config 仅在特定条件下追加」这一设计意图，并能在真实源码中找到它真正的判定条件。

> 本讲依赖 [u4-l1（Triton 前向 kernel 与 online softmax 主循环）](u4-l1-triton-fwd-online-softmax.md) 与 [u5-l2（dK/dV 与 dQ kernel）](u5-l2-dkdv-dq-shared-pid.md)：你需先知道 `BLOCK_M / BLOCK_N / BLOCK_HEADDIM_QK / BLOCK_HEADDIM_V / NUM_V_GROUPS` 这些 tile 参数的语义，再来理解为什么要「搜」它们。

## 2. 前置知识

### 2.1 什么是 Triton autotune

Triton 的 `@triton.autotune(configs=..., key=...)` 是一个装饰器：被它装饰的 kernel，在**每个不同的 `key` 取值组合第一次出现时**，会把 `configs` 列表里的每一组参数都实际编译并跑一遍，挑出最快的那一组，然后把「这个 key → 最优 config」的映射**缓存**起来。之后只要 key 相同，就直接复用上次的结论，不再重搜。

- `configs`：要搜索的候选参数集合（每个 `triton.Config` 是一组 `{tile 大小}` + `num_warps/num_stages`）。
- `key`：一组**参与缓存判等的参数名**。只有当 key 的取值变了，才会触发新一轮搜索。

所以 autotune 的本质是两件事：**生成候选**（决定搜多大空间）和**定义 key**（决定什么时候重搜）。FFPA 的所有 autotune 设计都是围绕这两件事展开的。

### 2.2 为什么要分 fast / max 两档

候选越多，第一次调优越慢（每个 config 都要编译+计时）。但如果候选太少，又可能漏掉更优的配置。FFPA 给用户一个旋钮 `autotune_mode`：

- **`fast`**：小搜索空间，候选少，首次调优快，但天花板低。
- **`max`**：大搜索空间，候选多，首次调优慢，但可能搜出更优 config。

两者的差异完全体现在「候选生成函数里某些维度是 1 个取值还是 2 个取值」上——后文会逐行讲。

### 2.3 为什么要 seqlen 分桶

autotune 的 `key` 里会包含序列长度。如果直接用**精确的 seqlen** 当 key，那么 `N=4096` 调一次、`N=4100` 调一次，会被当成两个不同的 key，各自从头搜一遍——而最优 tile 在这两个长度上几乎一定相同，纯属浪费。

解决办法是**把 seqlen 映射到一个较粗的「桶上沿」**：`N=4096` 和 `N=4100` 都映射到同一个桶代表值（如 5120），于是它们复用同一次调优结果。fast 模式桶更粗（复用多、精度低），max 模式桶更细（复用少、精度高）——和「fast/max 候选数」是对偶的两套粒度。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [`src/ffpa_attn/triton/_ffpa_fwd.py`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py) | 前向 kernel 与候选生成器 `_gen_fwd_autotune_configs`、`_gen_decode_fwd_stage1_autotune_configs`，以及 autotune wrapper 工厂 `_get_fwd_autotune` / `_get_decode_fwd_stage1_autotune`、两个启动器（generic / decode）。 |
| [`src/ffpa_attn/triton/_autotune_utils.py`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_autotune_utils.py) | seqlen 分桶核心：`bucket_autotune_seqlen` / `autotune_seqlen_key` / `exact_autotune_seqlen_keys`。 |
| [`src/ffpa_attn/functional.py`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py) | `TritonBackend` 配置类（`autotune` / `autotune_mode` 旋钮）、`_FFPAAttnFunc` 把这两个旋钮透传给前向/反向。 |
| [`src/ffpa_attn/triton/__init__.py`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/__init__.py) | `torch.ops.ffpa_attn._fwd_triton` 算子的实现，负责把字符串 `autotune_mode` 在 op 边界编码为 `int` 再解码回来。 |
| [`src/ffpa_attn/triton/_ffpa_bwd.py`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd.py) | 反向的候选生成器 `_gen_bwd_autotune_configs`（结构与前向同构，供对照）。 |

## 4. 核心概念与源码讲解

### 4.1 前向 generic 候选生成：_gen_fwd_autotune_configs

#### 4.1.1 概念说明

`_gen_fwd_autotune_configs` 负责**为前向 generic kernel（即非 decode、`num_splits==1` 的主路径）生成候选 config 列表**。它是一个纯 Python 函数，输入 `headdim` 与 `autotune_mode`，输出一个 `list[triton.Config]`。

它搜索的五个维度是：

- `BLOCK_M`：Q 行块大小（注意力输出行方向的 tile）。
- `BLOCK_N`：KV 列块大小（softmax 归约方向的 tile）。
- `BLOCK_HEADDIM_QK` / `BLOCK_HEADDIM_V`：Split-D 中 D 维片段的宽度（u4-l2 讲过，二者**lockstep 联动**，始终取同一个值）。
- `num_warps`：每个 program 使用的 warp 数。
- `num_stages`：软件流水线级数（控制 SMEM 多缓冲深度）。

fast 与 max 的全部区别，就是 `BLOCK_N` 与 `num_stages` 这两个维度从「1 个取值」放宽到「2 个取值」。

#### 4.1.2 核心流程

候选总数 = 五个维度取值数的乘积。设 `H` 为 `headdim_candidates` 的长度，则：

\[
\text{configs} = |\text{BLOCK\_M}| \times |\text{BLOCK\_N}| \times H \times |\text{num\_warps}| \times |\text{num\_stages}|
\]

| 模式 | BLOCK_M | BLOCK_N | num_warps | num_stages | 单维乘积（H=2 时） |
| --- | --- | --- | --- | --- | --- |
| fast | {64,128} | {64} | {4,8} | {2} | 2·1·H·2·1 = **4H** |
| max | {64,128} | {64,128} | {4,8} | {2,3} | 2·2·H·2·2 = **16H** |

#### 4.1.3 源码精读

候选生成函数本体，注意每行注释里的候选计数与上表一致：

这是 fast/max 分歧的两个开关——`BLOCK_N` 与 `num_stages` 各自由 `autotune_mode` 决定取一值还是两值：
[\_ffpa_fwd.py:L126-L169](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L126-L169) — `_gen_fwd_autotune_configs`，注释明确写着「fast: 8 configs; max: 32 configs」（对应 H=2 的默认情形）。

关键三段：

1. **D 片段候选集合**（[\_ffpa_fwd.py:L148-L150](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L148-L150)）：默认 `[64, 128]`，仅当 `headdim == 256` 时再追加 `256`。

2. **五重嵌套循环**（[\_ffpa_fwd.py:L152-L168](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L152-L168)）：把 `BLOCK_HEADDIM_QK` 与 `BLOCK_HEADDIM_V` 设成同一个 `block_headdim`（lockstep），fast 下 `BLOCK_N` 与 `num_stages` 退化成单值。

3. **函数 docstring**（[\_ffpa_fwd.py:L130-L146](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L130-L146)）：描述了「当设备每 block 有 ≥128 KB SMEM（Ada/Hopper）时，追加一个 full-D 单片段 config」的设计意图。

> ⚠️ **务必区分「docstring 意图」与「代码实现」**：docstring 提到的「≥128 KB SMEM 才追加 full-D config」是**设计动机**（解释为何 full-D 即 `NUM_V_GROUPS==1` 的 config 在高 SMEM 设备上才值得 benchmark）；但**当前代码里真正的判定条件只有一行 `if headdim == 256`**（[\_ffpa_fwd.py:L149-L150](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L149-L150)），并没有运行期读取设备 SMEM 容量。
>
> 而且这一分支在 FFPA 的大 D 前向路径里**实际不可达**：因为 `D ≤ 256` 会回退到 SDPA（见 u3-l3 的 `fallback()`），Triton generic 前向只承接 `D ∈ [320, 1024]`，此时 `headdim` 永远不等于 256。所以在真实 FFPA 调用中，`headdim_candidates` 恒为 `[64, 128]`，候选数恒为 **fast=8 / max=32**。请把 docstring 当成「为什么这样设计」的背景，把代码当成「实际行为」的真相。

把生成好的候选交给 `triton.autotune` 包装，并指定缓存 key（注意 key 里**没有** BLOCK_M/BLOCK_N——它们是被搜的对象，不是缓存判等字段）：
[\_ffpa_fwd.py:L1291-L1316](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L1291-L1316) — `_get_fwd_autotune`，按 `(headdim, autotune_mode, dtype)` 缓存包装后的 kernel，`cache_results=True` 让 Triton 把「key→最优 config」也缓存在进程内，同一个 key 只搜一次。

#### 4.1.4 代码实践

1. **实践目标**：亲手验证 fast / max 各产生多少候选，并观察 `headdim==256` 分支的效果。
2. **操作步骤**：在装好 `torch` + `triton` 的环境里执行下面这段「示例代码」（**非项目原有代码**）：

   ```python
   # 示例代码：统计前向 generic 候选数
   from ffpa_attn.triton._ffpa_fwd import _gen_fwd_autotune_configs

   for hd in (256, 512):           # 256 触发追加分支；512 是 FFPA 真实大 D
       for mode in ("fast", "max"):
           cfgs = _gen_fwd_autotune_configs(hd, autotune_mode=mode)
           print(f"headdim={hd:>4}  {mode:<4} -> {len(cfgs):>3} configs")
   ```

3. **需要观察的现象**：`headdim=512` 时 fast/max 分别为 8/32；`headdim=256` 时因为多了一个 `BLOCK_HEADDIM=256` 候选，会变成 12/48。
4. **预期结果**：

   ```
   headdim= 256  fast ->  12 configs
   headdim= 256  max  ->  48 configs
   headdim= 512  fast ->   8 configs
   headdim= 512  max  ->  32 configs
   ```
5. 若本地未装 triton，无法 import，请标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `num_warps` 从 `[4, 8]` 改成 `[4]`，max 模式（H=2）候选数会变成多少？

**答案**：`2(BLOCK_M)·2(BLOCK_N)·2(H)·1(num_warps)·2(num_stages) = 16`。

**练习 2**：为什么 autotune 的 `key` 列表里放的是 `HEADDIM`，而候选里也有 `BLOCK_HEADDIM_*`？二者角色有何不同？

**答案**：`HEADDIM` 是**运行期已知、但不被搜索**的常量（真实 head 维度），它进 key 是为了让不同 D 的最优 config 分别缓存；而 `BLOCK_HEADDIM_*` 是**被搜索的 tile 参数**，由 autotuner 在候选间挑选。同一个 `HEADDIM=512` 下，autotuner 会在 `BLOCK_HEADDIM ∈ {64,128}` 两个候选间择优。

---

### 4.2 decode stage1 候选生成：_gen_decode_fwd_stage1_autotune_configs

#### 4.2.1 概念说明

decode 形状（`Nq` 极小、`Nkv` 长）走的是 split-KV 两阶段路径（u4-l3）。它的 stage1 kernel 与 generic kernel 形状特征不同：`Nq` 可能小到 1（走 GEMV 向量路径），因此 **BLOCK_M 的候选集合随 `use_gemv` 改变**，且 `num_stages` 固定为 2、不参与 fast/max 切换。`CHUNK_SIZE`（即 `num_splits` 决定的 KV 切片）由启动器在运行期注入，**不在 autotune 候选里**。

#### 4.2.2 核心流程

候选数仍是一组维度的乘积，但维度集合与前向不同：

\[
\text{configs} = |\text{BLOCK\_N}| \times |\text{BLOCK\_M}| \times H \times |\text{num\_warps}|
\]

（`num_stages` 恒为 2，不算自由维度。）

| use_gemv | BLOCK_N | BLOCK_M | num_warps(fast/max) | H=2 时候选数 |
| --- | --- | --- | --- | --- |
| True (Nq==1) | {64,128} | {8} | {4} / {4,8} | fast **4** / max **8** |
| False (Nq>1) | {64,128} | {16,32} | {4} / {4,8} | fast **8** / max **16** |

#### 4.2.3 源码精读

[\_ffpa_fwd.py:L172-L211](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L172-L211) — `_gen_decode_fwd_stage1_autotune_configs`，文件里直接写了四种组合的计数注释（第 189-190 行），与上表一一对应。

要点：

- **BLOCK_M 随 `use_gemv` 切换**（[\_ffpa_fwd.py:L196](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L196)）：GEMV 路径只有 1 行（`[8]`），多行 MMA 路径才搜 `[16, 32]`。
- **num_stages 写死 2**（[\_ffpa_fwd.py:L208](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L208)）：decode stage1 是带宽受限的 GEMV/小 M 场景，深流水线收益小，故不搜。
- **full-D 候选同样只在 `headdim==256` 追加**（[\_ffpa_fwd.py:L192-L194](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L192-L194)）：与前向同构，且 docstring 明确「full-D tiles 只在单行 GEMV 路径里探索」。

包装与缓存：[\_ffpa_fwd.py:L833-L854](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L833-L854) — `_get_decode_fwd_stage1_autotune`，cache_key 为 `(headdim, use_gemv, autotune_mode, dtype)`，比前向多了 `use_gemv` 这一维（因为 GEMV 与多行是两套截然不同的 tile 形状）。

#### 4.2.4 代码实践

1. **实践目标**：确认 GEMV 与多行路径的候选数差异。
2. **操作步骤**（示例代码，非项目原有）：

   ```python
   from ffpa_attn.triton._ffpa_fwd import _gen_decode_fwd_stage1_autotune_configs

   for gemv in (True, False):
       for mode in ("fast", "max"):
           cfgs = _gen_decode_fwd_stage1_autotune_configs(
               512, use_gemv=gemv, autotune_mode=mode
           )
           print(f"use_gemv={gemv!s:<5} {mode:<4} -> {len(cfgs):>2} configs")
   ```

3. **预期结果**：`gemv=True` → fast 4 / max 8；`gemv=False` → fast 8 / max 16。
4. 若未装 trinton，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习**：为什么 decode stage1 的 cache_key 要比 generic 前向多一个 `use_gemv`？

**答案**：GEMV（`Nq==1`）与多行 MMA（`Nq>1`）的 tile 形状完全不同（BLOCK_M 候选集都不一样），它们的最优 config 互不复用，因此必须作为独立 key 分别缓存，否则会拿 GEMV 的最优 config 去跑多行 kernel（或反之）。

---

### 4.3 autotune key 与 seqlen 分桶：_autotune_utils

#### 4.3.1 概念说明

前两节讲了「候选怎么来」，这一节讲「key 怎么定」。三个 autotune wrapper（前向 generic、decode stage1、反向）的 `key` 列表都是同一个四元组：

```python
key = [
  "autotune_seqlen_q_bucket",
  "autotune_seqlen_k_bucket",
  "autotune_causal_key",
  "HEADDIM",
]
```

其中 `autotune_seqlen_q_bucket` / `autotune_seqlen_k_bucket` 不是原始 seqlen，而是经 `autotune_seqlen_key()` **分桶后**的代表值。这就是 2.3 节说的「把相近 seqlen 复用同一次调优」的实现。

#### 4.3.2 核心流程

桶上沿公式（向上取整到桶大小的整数倍）：

\[
\text{bucket}(s, b) = \left\lceil \frac{s}{b} \right\rceil \cdot b = \left(\left\lfloor \frac{s-1}{b} \right\rfloor + 1\right) \cdot b
\]

两种模式的分桶策略：

| 模式 | 分桶规则 | 举例 |
| --- | --- | --- |
| **fast** | 统一桶宽 1024，>8192 一律压到 8192 | `8191→8192`，`8193→8192`（封顶） |
| **max** | 分段桶宽：≤512 用 64；(512,1024] 用 128；(1024,2048) 用 256；[2048,8192] 用 512；(8192,16384] 用 1024；>16384 压到 16384 | `513→640`，`1025→1280`，`8193→9216`，`20000→16384` |

可以看到 max 模式不仅候选更多，**分桶也更细**——两套粒度同向：max 把更多算力花在「搜得更精 + 复用更准」上。

#### 4.3.3 源码精读

桶上沿工具函数：[\_autotune\_utils.py:L18-L25](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_autotune_utils.py#L18-L25) — `_bucket_upper_edge`，即上面公式的直接实现。

fast / max 两种分桶主体：[\_autotune\_utils.py:L28-L79](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_autotune_utils.py#L28-L79) — `bucket_autotune_seqlen`，docstring 给了全部示例值，与上表一致；常量 `_AUTOTUNE_SEQLEN_BUCKET_SIZE=1024`、`_AUTOTUNE_SEQLEN_BUCKET_CAP=8192`、`_AUTOTUNE_MAX_SEQLEN_BUCKET_CAP=16384` 定义在 [\_autotune\_utils.py:L9-L11](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_autotune_utils.py#L9-L11)。

对外入口（autotune wrapper 真正调用的）：[\_autotune\_utils.py:L82-L97](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_autotune_utils.py#L82-L97) — `autotune_seqlen_key`。它有一个**开关**：当 `_EXACT_AUTOTUNE_SEQLEN_KEYS` 这个 ContextVar 为真时，直接返回**精确 seqlen** 而非分桶值。

精确模式的开关：[\_autotune\_utils.py:L100-L107](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_autotune_utils.py#L100-L107) — `exact_autotune_seqlen_keys()` 上下文管理器。**它不是给运行期用的，而是给「持久化自动调优生成器」（u8-l2 的 `python -m ffpa_attn.autotune`）用的**：生成器要为每一个目标 (headdim, seqlen) 精确 benchmark 出最优 config 写进 JSON，所以必须关掉分桶、用精确 key，否则不同 seqlen 会被合并、JSON 里就缺条目。

回到启动器，看 bucket 值是怎么塞进 kernel 参数的（前向 generic）：[\_ffpa_fwd.py:L902-L903](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L902-L903) — `autotune_seqlen_q_bucket = autotune_seqlen_key(seqlen_q, autotune_mode)`，算出的桶值随后作为普通 int 参数传进 kernel（[\_ffpa_fwd.py:L945-L947](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L945-L947)），由于它出现在 `key` 列表里，Triton 就用它做缓存判等。decode 路径同理：[\_ffpa_fwd.py:L1078-L1080](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L1078-L1080)。

#### 4.3.4 代码实践

1. **实践目标**：直观感受 fast / max 分桶粒度的差异。
2. **操作步骤**（示例代码，非项目原有，纯 Python 无需 GPU）：

   ```python
   from ffpa_attn.triton._autotune_utils import bucket_autotune_seqlen

   for s in (513, 1025, 4097, 8193, 20000):
       print(s, "fast->", bucket_autotune_seqlen(s, "fast"),
             " max->", bucket_autotune_seqlen(s, "max"))
   ```

3. **预期结果**：

   ```
   513 fast-> 1024  max-> 640
   1025 fast-> 2048  max-> 1280
   4097 fast-> 5120  max-> 4608
   8193 fast-> 8192  max-> 9216
   20000 fast-> 8192  max-> 16384
   ```
4. 此函数无 GPU 依赖，可本地直接验证。

#### 4.3.5 小练习与答案

**练习 1**：运行期 `Nq=4100` 调了 generic 前向，紧接着 `Nq=4200` 再调一次，会不会触发重新 autotune？分别用 fast / max 回答。

**答案**：fast 模式下两者都映射到桶 `5120`，**不会**重搜、直接复用；max 模式下 `4100→4608`、`4200→4608`（都在 [2048,8192] 段、桶宽 512），也映射到同一桶 `4608`，同样**不会**重搜。两种模式在此例结论相同，但 max 桶更细，意味着在更长的跨度上才会复用。

**练习 2**：为什么持久化生成器要用 `exact_autotune_seqlen_keys()` 关掉分桶？

**答案**：生成器的职责是「为每个目标 seqlen 各产出一条最优 config 写进 JSON」，若开分桶，多个目标 seqlen 会被合并成一条，JSON 里就少条目、运行期查找（u8-l3）就查不到精确形状。

---

### 4.4 TritonBackend.autotune / autotune_mode 与端到端分发链路

#### 4.4.1 概念说明

前面三节都在 triton 子包内部。这一节回答：**用户的旋钮 `autotune` / `autotune_mode` 是怎么从 `TritonBackend` 一路传到 kernel 启动的**。这条链路跨了三个文件、四次函数边界，理解它就能解释「为什么设了 `autotune_mode='max'` 第一次前向会明显变慢」。

#### 4.4.2 核心流程

```
TritonBackend(autotune=True, autotune_mode="max")     # functional.py：用户旋钮
        │  _FFPAAttnFunc.forward 透传 forward_meta.autotune / .autotune_mode
        ▼
_ffpa_attn_forward_triton(...)                        # _ffpa_fwd.py：把 mode 编码为 int
        │  int(autotune_mode == "max")  → autotune_mode_is_max
        ▼
torch.ops.ffpa_attn._fwd_triton(...)                  # __init__.py：op 边界（schema 要求 POD）
        │  "max" if autotune_mode_is_max else "fast"  → 解码回字符串
        ▼
_ffpa_attn_forward_impl(...)                          # _ffpa_fwd.py：按 num_splits 分流
        ├── num_splits==1 → _ffpa_attn_forward_generic_impl → _get_fwd_autotune
        └── 否则        → _ffpa_attn_forward_decode_impl  → _get_decode_fwd_stage1_autotune
```

#### 4.4.3 源码精读

**① 旋钮定义与校验**：[functional.py:L175-L218](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L175-L218) — `TritonBackend`，`autotune: bool = False` 与 `autotune_mode: str = "fast"`（[functional.py:L194-L195](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L194-L195)）是两个默认关闭/小搜索的字段；`__post_init__` 用 assert 把 `autotune_mode` 限制在 `("fast", "max")`（[functional.py:L206-L207](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L206-L207)），传错立即 fail-fast。

**② forward 透传**：[functional.py:L793-L812](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L793-L812) — `_FFPAAttnFunc.forward` 在 Triton 分支把 `forward_meta.autotune`、`forward_meta.autotune_mode` 作为第 5、6 个位置参数传给 `_ffpa_attn_forward_triton`（[functional.py:L804-L805](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L804-L805)）。反向同理：[functional.py:L875-L876](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L875-L876) 透传 `backward_meta.autotune / .autotune_mode`。

**③ op 边界编码/解码**：[\_ffpa_fwd.py:L1496-L1508](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L1496-L1508) — `_ffpa_attn_forward_triton` 调用注册算子时，把字符串 `autotune_mode` 编码成 `int(autotune_mode == "max")`（因为 `torch.library` 的 op schema 只接受 POD 类型，不能直接传 str）；算子实现端 [\__\_init\_\_.py:L319-L320](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/__init__.py#L319-L320) 再用 `autotune_mode="max" if autotune_mode_is_max else "fast"` 解码回来。

**④ generic vs decode 分流**：[\_ffpa_fwd.py:L916-L957](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L916-L957) — generic 启动器在 `if autotune:` 分支调 `_get_fwd_autotune(...)`；decode 启动器 [\_ffpa_fwd.py:L1107-L1123](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L1107-L1123) 在 `if autotune:` 分支调 `_get_decode_fwd_stage1_autotune(...)`。值得对照的是 **`autotune=False` 的另一条路**：[\_ffpa_fwd.py:L958-L983](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L958-L983) 先查持久化配置（u8-l3 的 `lookup_persistent_config`），查不到才用一个**写死的默认 config**（BLOCK_M=128/BLOCK_N=64/...）。也就是说，不开 `autotune` 时，config 来自持久化 JSON 或默认值，**完全不在线搜索**——这正是 fast/max 在线搜索与持久化离线搜索的分界。

> 反向的候选生成器 [\_ffpa\_bwd.py:L415-L447](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd.py#L415-L447) `_gen_bwd_autotune_configs` 与前向同构（同样五重循环、同样 `if headdim==256` 追加），差别仅是 `num_stages` 在 fast/max 都搜 `[2,3]`、字段名为单一的 `BLOCK_HEADDIM`（反向没有 QK/V 分离）。

#### 4.4.4 代码实践

1. **实践目标**：用 `TritonBackend(autotune=True, autotune_mode="max")` 触发一次真实在线调优，并观察 Triton 调优日志。
2. **操作步骤**（示例代码，非项目原有；需 GPU + 已安装 ffpa_attn）：

   ```bash
   # 打开 Triton autotune 打印，让每次「首次搜某 key」都输出最优 config
   export TRITON_PRINT_AUTOTUNING=1
   ```

   ```python
   import torch
   from ffpa_attn import ffpa_attn_func
   from ffpa_attn.functional import TritonBackend

   # 大 D、长序列：确保走 Triton generic 前向而非回退 SDPA
   B, Hq, Hkv, Nq, Nkv, D = 1, 8, 8, 4096, 4096, 512
   q = torch.randn(B, Hq, Nq, D, dtype=torch.bfloat16, device="cuda")
   k = torch.randn(B, Hkv, Nkv, D, dtype=torch.bfloat16, device="cuda")
   v = torch.randn(B, Hkv, Nkv, D, dtype=torch.bfloat16, device="cuda")

   fwd = TritonBackend(autotune=True, autotune_mode="max")
   o = ffpa_attn_func(q, k, v, forward_backend=fwd)
   ```

3. **需要观察的现象**：第一次调用会**明显卡顿**（每个候选都要编译+计时），stderr 会打印类似 `triton.autotune ... best config: ...` 的行，里面能看到挑出的 `BLOCK_M/BLOCK_N/BLOCK_HEADDIM_*/num_warps/num_stages`；改 `Nq` 到同桶内的另一值（如 4100）再调一次，应**无卡顿、无新日志**（命中桶缓存）；换成跨桶的值（如 8193）则**再次卡顿并打印新调优**。
4. **预期结果**：`max` 下首次调优日志会列出多达 32 个候选的计时（fast 则 8 个）；同桶复用、跨桶重搜的行为符合 4.3 节分桶表。
5. 由于本环境无 GPU，具体日志文本「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `autotune_mode` 在 `torch.library` op 边界要被编码成 `int`？

**答案**：`torch.library.define` 的 schema 只接受 POD/张量类型（int/float/Tensor/...），不支持任意 str。把 `"fast"/"max"` 编码成 `is_max: int` 是为了让 op schema 合法，实现端再解码回字符串。

**练习 2**：同一个 `TritonBackend(autotune=True, autotune_mode="max")` 实例，先跑 `Nq=4096` 再跑 `Nq=8193`，前者结论会被后者复用吗？

**答案**：不会。fast/max 下 `4096→5120(or 4608)`、`8193→8192(or 9216)` 分属不同桶，是两个不同 key，各自独立调优。

## 5. 综合实践

把本讲的「候选生成 + 分桶 + 端到端链路」串成一个小任务：**为 FFPA 前向画一张「shape → autotune 行为」速查表**。

1. 选定 `D=512`、`dtype=bfloat16`，分别取 `Nq ∈ {1, 4096, 8193}` 三种形状（分别对应 decode / generic 同桶 / generic 跨桶）。
2. 对每种形状，回答四个问题：
   - 走 generic 还是 decode 启动器？（提示：看 `Nq` 与占用率，decode 由 `_get_decode_num_splits` 判定，见 u4-l3）
   - `_gen_*_autotune_configs` 在 `fast` / `max` 下各产生多少候选？（用 4.1.4 / 4.2.4 的脚本验证）
   - `autotune_seqlen_q_bucket` 与 `autotune_seqlen_k_bucket` 在 fast / max 下分别是什么值？（用 4.3.4 的脚本验证）
   - 同一形状第二次调用会不会重搜？
3. 把结果填进一张表，并在源码里标注每个结论对应的行号（候选计数 → [\_ffpa\_fwd.py:L147](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L147)；分桶 → [\_autotune\_utils.py:L61](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_autotune_utils.py#L61)；透传 → [functional.py:L804](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/functional.py#L804)）。

完成后，你应当能仅凭「shape + autotune_mode」预判一次前向的调优开销与缓存命中情况——这正是 u8-l2 持久化生成器要批量做的事。

## 6. 本讲小结

- FFPA 的 Triton autotune 由两件事组成：**生成候选**（`_gen_fwd_autotune_configs` / `_gen_decode_fwd_stage1_autotune_configs`）与**定义 key**（`autotune_seqlen_*_bucket` + `autotune_causal_key` + `HEADDIM`）。
- **fast / max 是两套同向粒度**：max 在「候选更多」与「分桶更细」两方面都比 fast 重；前向 generic 在真实大 D（H=2）下为 fast=8 / max=32 个候选。
- **full-D 候选的判定**：docstring 描述了「高 SMEM 设备才追加 full-D 单片段 config」的动机，但**代码实际只判 `headdim == 256`**，且该分支在 FFPA 大 D 前向（D>256）路径里不可达，故 `BLOCK_HEADDIM_*` 恒为 64 或 128。
- **seqlen 分桶**让相近序列长度复用同一次调优；持久化生成器另用 `exact_autotune_seqlen_keys()` 关闭分桶以产出精确 JSON。
- **端到端链路**：`TritonBackend.autotune/autotune_mode` → `_FFPAAttnFunc` 透传 → op 边界把 mode 编码为 `int(is_max)` → 实现端解码 → 按 `num_splits` 分流到 generic / decode 启动器；`autotune=False` 时改走持久化配置或默认 config，不在线搜索。

## 7. 下一步学习建议

- **u8-l2（持久化调优配置生成器 CLI）**：本讲的 `exact_autotune_seqlen_keys()` 就是为它服务的；去看 `python -m ffpa_attn.autotune` 如何把每个 (headdim, seqlen) 的最优 config 离线 benchmark 成 JSON。
- **u8-l3（运行时配置查找与就近匹配回退）**：本讲 4.4.3 提到的 `autotune=False` 分支里的 `lookup_persistent_config` 就在那里实现，看它如何按 direction/kernel/causal/dtype 过滤并就近匹配 head_dim 与 seqlen。
- 想再深一层，可对比反向候选生成器 [\_ffpa\_bwd.py:L415-L447](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_bwd.py#L415-L447) 与前向的异同，并阅读 `_get_bwd_autotune` / `_get_bwd_dkdv_autotune` / `_get_bwd_dq_autotune` 三个反向 wrapper 的 key 设计。
