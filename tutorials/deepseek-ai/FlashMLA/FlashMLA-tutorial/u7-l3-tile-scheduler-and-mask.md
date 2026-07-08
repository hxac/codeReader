# Causal tile scheduler 与 mask

> 本讲属于「CUTLASS Dense MHA Prefill/Backward（SM100）」单元（Unit 7），承接 u7-l2《Mainloop collective 与 MLA fusion》。在 u7-l2 里我们看到了 warp-specialized mainloop 如何把 Copy/MMA/Softmax 流水化；本讲回答两个紧随其后的问题：(1) 一个 CTA 到底该算哪一块、按什么顺序算？(2) 因果掩码（causal mask）这种「上三角不算」的需求，是在哪一层、用什么机制落实的？

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 `MaskMode`（运行时枚举）是如何在 fwd 入口被映射成编译期的 `CausalMask` / `ResidualMask` 类型，并理解「运行时值编译期化」为何对 GPU kernel 至关重要。
- 描述通用 tile scheduler（`IndividualTileScheduler` / `PersistentTileScheduler`）与 causal 专用 scheduler（`CausalIndividualTileScheduler` / `CausalPersistentTileScheduler`）的职责差异。
- 解释 causal 注意力里「跳过 tile」的真正判定逻辑：scheduler 负责**重排序**（让最长主循环先跑、提升 L2 命中），而真正的**上三角裁剪**由 `CausalMask::get_trip_count` 在 mainloop 内部限制 K-tile 迭代次数来完成。
- 理解 `fmha_options.hpp` 的 Option/Tag 机制如何把 `kIsPersistent` 这类调优开关编译期化，以及 `pipeline_mla.hpp` 为 MLA mainloop 提供的「按字节数获取流水槽」的 TMA pipeline。

## 2. 前置知识

本讲默认你已经具备 u7-l1（CUTLASS 分层：device→kernel→collective→common）与 u7-l2（warp-specialized mainloop、online softmax、TMEM/UMMA）的认知。补充三个本讲会用到的术语：

- **tile（分块）**：attention 矩阵 $Q K^\top$ 太大装不下，按行列切成小块（本讲的 SM100 dense prefill 用 `TileShape = <256(Q), 128(K), ...>`），一个 CTA 负责一块。把所有块派给所有 CTA 的策略就叫 **tile scheduler**。
- **causal / 因果掩码**：自回归语言模型里，第 $i$ 个 query 只能看见前 $i$ 个 key，等价于把 $QK^\top$ 的上三角置 $-\infty$。下三角才是有效计算区。
- **persistent kernel**：普通 kernel 一个 CTA 只算一块、算完即退；persistent kernel 启动固定数量（≈SM 数）的 CTA，每个 CTA 在 `for` 循环里连续啃多块，省去反复启动开销、也更利于 L2 复用。
- **online softmax**：边累加边做 softmax 归一，配合 rescale 把分块结果合并（u4-l2、u7-l2 已讲）。

如果你对「枚举值如何变成模板参数」还不熟，可以先回头看 u2-l3 的 `DISPATCH_*` 宏，本讲的 MaskMode 派发是同一思想的 CUTLASS 变体。

## 3. 本讲源码地图

本讲涉及的关键文件（均在 `csrc/sm100/prefill/dense/` 下）：

| 文件 | 作用 |
| --- | --- |
| [common/mask.cuh](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/common/mask.cuh) | 定义运行时枚举 `MaskMode`（None/Causal/Custom），是 Python 端传进来的「整数」。 |
| [collective/fmha_fusion.hpp](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/collective/fmha_fusion.hpp) | 定义编译期 mask **策略类型**：`NoMask` / `ResidualMask` / `CausalMask<kIsQBegin>`，以及 varlen 辅助类型 `VariableLength`。 |
| [kernel/fmha_tile_scheduler.hpp](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/kernel/fmha_tile_scheduler.hpp) | 通用 scheduler：`IndividualTileScheduler`（一 CTA 一块）与 `PersistentTileScheduler`（持久化）。 |
| [kernel/fmha_causal_tile_scheduler.hpp](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/kernel/fmha_causal_tile_scheduler.hpp) | causal 专用 scheduler：`CausalIndividualTileScheduler`（重排序 + swizzle）与 `CausalPersistentTileScheduler`。 |
| [kernel/fmha_options.hpp](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/kernel/fmha_options.hpp) | Option/Tag 机制：在模板参数包里按 tag 查找编译期开关。 |
| [common/pipeline_mla.hpp](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/common/pipeline_mla.hpp) | MLA 专用 TMA→UMMA 流水 `PipelineTmaAsyncMla`，核心是 `producer_acquire_bytes`。 |
| [fmha_cutlass_fwd_sm100.cu](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/fmha_cutlass_fwd_sm100.cu) / [.cuh](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/fmha_cutlass_fwd_sm100.cuh) | fwd 入口：把 `MaskMode` 派发到具体 mask 类型，再据 mask 类型选 scheduler。 |

> 说明：`fmha_fusion.hpp`、`fmha_options.hpp`、`pipeline_mla.hpp` 虽然带 NVIDIA CUTLASS 版权头，但都被 FlashMLA 直接 include 使用，是本讲不可绕过的真实代码。

## 4. 核心概念与源码讲解

本讲按四个最小模块展开：先讲 **mask 类型与派发**（4.1，回答「掩码在哪定」），再讲 **通用 scheduler**（4.2）与 **causal scheduler**（4.3，回答「CTA 算哪块、怎么跳」），最后讲 **options / pipeline**（4.4，回答「调优开关与流水如何编译期化」）。

### 4.1 Mask 类型与 MaskMode 映射

#### 4.1.1 概念说明

注意力掩码的本质是：在算完 $S = QK^\top$ 之后、做 softmax 之前，把不该参与的位置（未来 token、超出序列长度的 padding）置成 $-\infty$。FlashMLA 把「需要哪种掩码」抽象成两类表达：

- **运行时枚举 `MaskMode`**：Python / C++ 接口层用一个整数告诉 kernel「我要 causal 还是不 要」。运行时才知道，**便宜、灵活**。
- **编译期策略类型 `CausalMask` / `ResidualMask` / `NoMask`**：每种掩码的具体行为（要不要裁剪上三角、要不要处理参差边界）以模板类型编码。编译期才知道，**零分支、可被内联进 softmax 主循环**。

GPU kernel 性能极度依赖分支消除：如果 `if (is_causal)` 写在 softmax 的内层循环里，每个元素都要判断一次。所以工程上的标准做法是——入口处把运行时枚举**派发**成编译期类型，让编译器为每种掩码单独生成一份无分支 kernel。这正是 u2-l3 `DISPATCH_*` 宏的思想，本讲是它的 CUTLASS 具体应用。

#### 4.1.2 核心流程

fwd 入口 `FMHACutlassSM100FwdRun` 的掩码派发流程：

1. 从参数读出整数 `mask_mode_code`，`static_cast` 成 `MaskMode`。
2. 按 `mask_mode == kCausal` 二分：causal → `CausalMask<false>`；否则 → `ResidualMask`。
3. 这套类型作为模板参数 `ActiveMask` 一路传到 mainloop，mainloop 在 softmax 步骤里调用 `ActiveMask::apply_mask` 与 `get_trip_count`。

派发表：

| `MaskMode`（运行时） | `ActiveMask`（编译期） | 行为 |
| --- | --- | --- |
| `kCausal` | `CausalMask<false>` | 下三角 + 参差 K 边界裁剪 |
| `kNone` / `kCustom` | `ResidualMask` | 仅处理参差 K 边界（无 causal） |

> 注意 `kNone` 与 `kCustom` 都落到 `ResidualMask`——在当前实现里二者等价，因为唯一的「非因果」掩码需求就是 K 序列长度不能被 tile 整除时的边界处理。

#### 4.1.3 源码精读

**运行时枚举**只有三行，极简：[mask.cuh:3-7](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/common/mask.cuh#L3-L7) 定义 `enum class MaskMode { kNone, kCausal, kCustom }`。

**派发发生在 fwd 入口**。先把整数转成枚举：[fmha_cutlass_fwd_sm100.cu:42](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/fmha_cutlass_fwd_sm100.cu#L42)

```cpp
MaskMode mask_mode = static_cast<MaskMode>(mask_mode_code);
```

然后用一个 `apply_config` lambda 做「枚举→类型」的二分：[fmha_cutlass_fwd_sm100.cu:49-63](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/fmha_cutlass_fwd_sm100.cu#L49-L63)

```cpp
auto apply_config = [&](auto fn) {
  if (mask_mode == MaskMode::kCausal) {
    if (is_varlen) fn(CausalMask<false>{}, cute::true_type{}, ...);
    else           fn(CausalMask<false>{}, cute::false_type{}, ...);
  } else {
    if (is_varlen) fn(ResidualMask{}, cute::true_type{}, ...);
    else           fn(ResidualMask{}, cute::false_type{}, ...);
  }
};
```

`fn` 接到的第一参数是 mask **值**（空类型对象），但 C++ 会按值类型推导出模板参数 `Mask`，从而让 `call_run_fmha_fwd<Mask, ...>` 拿到编译期类型——这就是 CUTLASS 风格的「用实例化做编译期派发」。`CausalMask<false>` 的 `<false>` 指 `kIsQBegin=false`，即约定「Q 在矩阵末尾」（见 4.3 详述）。

**三种 mask 类型的具体行为**都在 `fmha_fusion.hpp`。基类 `NoMask` 的 `apply_mask` 是空操作：[fmha_fusion.hpp:72-80](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/collective/fmha_fusion.hpp#L72-L80)。`ResidualMask` 继承 `NoMask`，只额外把「K 列下标 ≥ 实际 K 长度」的位置置 $-\infty$，处理参差边界：[fmha_fusion.hpp:113-132](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/collective/fmha_fusion.hpp#L113-L132)

```cpp
for (int i = 0; i < size(acc_qk); i++) {
  auto pos = index_qk(i);
  if (get<1>(pos) >= get<1>(problem_size)) {  // k_col >= SK
    acc_qk(i) = -INFINITY;
  }
}
```

`CausalMask<kIsQBegin>` 的 `apply_mask` 同时做「上三角」和「参差边界」两件事，[fmha_fusion.hpp:258-275](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/collective/fmha_fusion.hpp#L258-L275)（`kIsQBegin=false` 分支）：

```cpp
const auto offset_q = get<1>(problem_size) - get<0>(problem_size);  // SK - SQ
for (int i = 0; i < size(acc_qk); i++) {
  auto pos = index_qk(i);                       // pos = (q_row, k_col)
  if ((get<0>(pos) + offset_q < get<1>(pos))    // 上三角：q+offset < k
      || (get<1>(pos) >= get<1>(problem_size))) { // 参差边界：k >= SK
    acc_qk(i) = -INFINITY;
  }
}
```

当 $S_Q = S_K$（方阵因果，prefill 常见）时 $\text{offset\_q}=0$，条件退化为 `q_row < k_col`，即标准下三角。$\text{offset\_q}\ne 0$ 的情形（$S_Q\ne S_K$，如推理续写）把 Q 整体平移到 K 序列末尾。

#### 4.1.4 代码实践

**实践目标**：亲手把 `MaskMode → ActiveMask` 的映射在脑中跑一遍，并验证你对参差边界的理解。

**操作步骤**（源码阅读型）：

1. 打开 [fmha_cutlass_fwd_sm100.cu:42-83](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/fmha_cutlass_fwd_sm100.cu#L42-L83)，对照派发表，回答：若 Python 传 `mask_mode=0`（`kNone`）且非 varlen，最终 `ActiveMask` 是哪个类型？传 `mask_mode=1`（`kCausal`）呢？
2. 打开 [fmha_fusion.hpp:113-132](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/collective/fmha_fusion.hpp#L113-L132) 与 [258-275](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/collective/fmha_fusion.hpp#L258-L275)，对比 `ResidualMask::apply_mask` 与 `CausalMask::apply_mask` 的差异。

**需要观察的现象 / 预期结果**：

- `mask_mode=0`（非 varlen）→ `ResidualMask`；`mask_mode=1`（非 varlen）→ `CausalMask<false>`。
- 两者**唯一**的实质差别：`CausalMask` 多了上三角判断 `q_row + offset_q < k_col`；参差边界判断 `k_col >= SK` 两者都有。所以 `ResidualMask` = 「不因果，但仍处理 K 参差」。

> 本实践为纯源码阅读，无需 GPU；若想跑数值验证可参考 4.1.5 第 2 题。

#### 4.1.5 小练习与答案

1. **问**：为什么 `kNone` 和 `kCustom` 都映射到 `ResidualMask` 而不是 `NoMask`？
   **答**：即便没有因果约束，当 $S_K$ 不能被 K-tile 大小（128）整除时，最后一个 K-tile 会包含越界的 padding 位置，必须把这些位置置 $-\infty$ 否则 softmax 会算错。`ResidualMask` 正是「无因果 + 处理参差边界」；纯 `NoMask` 只在 $S_K$ 恰好整除时才安全，故入口不直接用它。

2. **问**：设 $S_Q = S_K = 200$，K-tile=128。写出 `CausalMask<false>` 在 m_block=0（Q 行 0..127）时，哪些 K 列会被 `apply_mask` 置 $-\infty$。
   **答**：offset_q = 0。对 query 行 r（0≤r≤127），mask 掉满足 `r < k_col` 或 `k_col ≥ 200` 的列。即每个 query 行 r 只保留 `k_col ∈ [0, r]` 且 `k_col < 200`；上三角（k_col > r）与 K≥200 的 padding 全置 $-\infty$。

---

### 4.2 通用 tile scheduler（Individual / Persistent）

#### 4.2.1 概念说明

`fmha_tile_scheduler.hpp` 提供两种「非因果」调度策略，回答「一个 CTA 算哪块」：

- **`IndividualTileScheduler`（一 CTA 一块）**：grid 维度直接编 problem size，`(grid.x = num_m_blocks, grid.y = H, grid.z = B)`，CTA 的逻辑坐标就是 `blockIdx`。算完一块就退出，简单。
- **`PersistentTileScheduler`（持久化）**：grid 只开 `min(总块数, SM 数)` 个 CTA，每个 CTA 在 `for` 循环里用 `block_idx += gridDim.x` 轮流啃多块，直到 `block_idx >= num_blocks`。好处是 CTA 数恒定、启动与尾部队列开销小、L2 复用更好。

两者都提供一个统一接口：`to_underlying_arguments`（host 侧算 grid/Params）、`get_grid_shape`（给 device 层启动）、`is_valid()` / `get_block_coord()` / `operator++()`（device 侧循环迭代）。

#### 4.2.2 核心流程

device 层（`fmha.hpp`）启动 kernel 时调用 `TileScheduler::get_grid_shape`，kernel 内每个 warp 角色跑一个统一循环（[sm100_fmha_fwd_kernel_tma_warpspecialized.hpp:438-440](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/kernel/sm100_fmha_fwd_kernel_tma_warpspecialized.hpp#L438-L440)）：

```
TileScheduler sched{params.tile_scheduler};        // 构造，读 block_idx
for (; sched.is_valid(); ++sched) {                // 持久化才真正循环多次
  auto blk_coord = sched.get_block_coord();        // (m_block, _, (bidh, bidb))
  ... mainloop.softmax / mma ...                   // 算这一块
}
```

对 `Individual` 而言 `operator++` 直接把 `valid_ = false`（[fmha_tile_scheduler.hpp:79-83](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/kernel/fmha_tile_scheduler.hpp#L79-L83)），即每个 CTA 只跑一次；对 `Persistent` 而言 `block_idx += gridDim.x`，循环到 `block_idx >= num_blocks`（[fmha_tile_scheduler.hpp:152-156](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/kernel/fmha_tile_scheduler.hpp#L152-L156)）。

#### 4.2.3 源码精读

**Individual 的 grid 与坐标**：[fmha_tile_scheduler.hpp:55-62](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/kernel/fmha_tile_scheduler.hpp#L55-L62) 算出 `grid = (num_m_blocks, H, B)`（其中 num_m_blocks 按 cluster 形状 round_up），坐标直接取 `blockIdx`：[fmha_tile_scheduler.hpp:73-77](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/kernel/fmha_tile_scheduler.hpp#L73-L77)

```cpp
return make_coord(blockIdx.x, _0{}, make_coord(blockIdx.y, blockIdx.z));
//                        ↑ m_block          ↑ bidh   ↑ bidb
```

**Persistent 的 grid 与解码**：[fmha_tile_scheduler.hpp:131-134](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/kernel/fmha_tile_scheduler.hpp#L131-L134) 把 grid 钳到 SM 数：

```cpp
dim3 grid(std::min(params.num_blocks, params.hw_info.sm_count), 1, 1);
```

总块数 `num_blocks = num_m_blocks * H * B`（[fmha_tile_scheduler.hpp:121-122](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/kernel/fmha_tile_scheduler.hpp#L121-L122)）。每个 CTA 用三个 `FastDivmod`（除法取模的快速整数实现）把一维 `block_idx` 解码成 `(m_block, bidb, bidh)`：[fmha_tile_scheduler.hpp:142-149](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/kernel/fmha_tile_scheduler.hpp#L142-L149)

```cpp
int block_decode = block_idx;
int m_block, bidb, bidh;
params.divmod_m_block(block_decode, m_block, block_decode);  // 先剥 m_block
params.divmod_b     (block_decode, bidb, block_decode);      // 再剥 bidb
params.divmod_h     (block_decode, bidh, block_decode);      // 剩下 bidh
```

> `FastDivmod` 是 CUTLASS 的「预计算倒数、用乘法替代除法」工具，比 GPU 原生整数除法快得多，在热路径解码坐标时很关键。

#### 4.2.4 代码实践

**实践目标**：理解 persistent 的「CTA 数钳到 SM 数」如何影响启动规模。

**操作步骤**：

1. 读 [fmha_tile_scheduler.hpp:105-134](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/kernel/fmha_tile_scheduler.hpp#L105-L134)，找到 `num_blocks` 与 `get_grid_shape`。
2. 读 [device/fmha.hpp:205-214](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/device/fmha.hpp#L205-L214)，确认 device 层直接用 `TileScheduler::get_grid_shape(params.tile_scheduler)` 作为 grid。

**需要观察的现象 / 预期结果**：设 B200 有 148 个 SM，某问题 `num_m_blocks=10, H=64, B=4` → `num_blocks = 2560`。Individual 会启动 2560 个 CTA；Persistent 只启动 `min(2560, 148)=148` 个 CTA，每个 CTA 平均处理 `ceil(2560/148)≈18` 块。

> 待本地验证：可在 host 端打印 `Operation::get_grid_shape` 返回的 dim3，对比 Individual 与 Persistent 的实际 grid。

#### 4.2.5 小练习与答案

1. **问**：`IndividualTileScheduler::operator++` 为什么把 `valid_` 直接置 false？
   **答**：Individual 模式下一个 CTA 只负责一块（`blockIdx` 即坐标），没有「下一块」可言，自增后立即失效，使 `for` 循环只跑一轮——与 Persistent 的多轮循环共用同一份 `for` 模板代码。

2. **问**：persistent 的 `block_idx += gridDim.x`（而非 `+= 1`）意味着什么？
   **答**：让 grid 内所有 CTA 以「跨步」方式瓜分剩余块：CTA 0 取 block 0, gridDim.x, 2·gridDim.x, …；CTA 1 取 1, gridDim.x+1, …。这样总块数被均匀分配，且每个 CTA 处理的块在 `block_idx` 维度上离散分布，利于负载均衡。

---

### 4.3 Causal tile scheduler 与「跳过 tile」的判定逻辑

#### 4.3.1 概念说明

这是本讲最容易被误解的一点，先澄清：

> **causal scheduler 本身并不「删掉」上三角 tile**——它仍然为每个 query tile 都启动一个 CTA。它的真正职责是**重排序**：swizzle Q/H 维度提升 L2 命中，并把「主循环最长」（即有效 K-tile 最多、计算量最大）的 query tile **优先启动**，让多数 SM 尽早进入满负荷。真正「跳过上三角」的工作发生在 mainloop 内部，由 `CausalMask::get_trip_count` 限制每个 CTA 只迭代到它的因果边界为止。

换句话说，「causal 跳过 tile」是**两层**协作：

1. **scheduler 层（重排 + swizzle）**：决定 CTA 启动顺序与坐标映射，目标是 L2 与占用率，见 [fmha_causal_tile_scheduler.hpp:42-43](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/kernel/fmha_causal_tile_scheduler.hpp#L42-L43) 的注释。
2. **mask 层（裁剪 K-tile 迭代）**：`CausalMask::get_trip_count` 给出该 query tile 只需迭代多少个 K-tile，超出部分根本不加载、不计算；`apply_mask` 只处理那一个「对角线上」的边界 K-tile。

此外还有一道「显式跳过」：因为 grid 的 m_block 维度被 round_up 到 cluster 大小，可能产生超出实际 $S_Q$ 的尾巴 tile，kernel 在循环里用一行 `continue` 直接跳过：[sm100_fmha_fwd_kernel_tma_warpspecialized.hpp:445-447](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/kernel/sm100_fmha_fwd_kernel_tma_warpspecialized.hpp#L445-L447)

```cpp
if (get<0>(blk_coord) * get<0>(TileShape{}) >= get<0>(logical_problem_shape)) {
  continue;   // m_block 超出实际 Q 长度，跳过
}
```

#### 4.3.2 核心流程

先看 mask 层的裁剪数学。对第 $m$ 个 query tile（`m_block`，从 0 起编号），tile 行宽 $\text{TileM}=256$、K-tile 列宽 $\text{TileN}=128$。该 tile 内最高 query 行的绝对位置（含 Q 平移）为：

\[
\text{row}_{\max} = (m+1)\cdot \text{TileM} + \text{offset}_q,\qquad \text{offset}_q = S_K - S_Q
\]

因果约束要求只保留 $k_\text{col} \le \text{row}_{\max}$ 的 key，故需要的 K-tile 数（`get_trip_count` 的 `max_blocks_q`）为：

\[
\text{max\_blocks\_q} = \left\lceil \frac{(m+1)\cdot \text{TileM} + \text{offset}_q}{\text{TileN}} \right\rceil
\]

而物理上 K 一共只有 $\text{max\_blocks\_k}=\lceil S_K/\text{TileN}\rceil$ 个 tile，取两者最小值即为该 CTA 实际要迭代的 K-tile 数：

\[
\text{trip\_count}(m) = \min\!\big(\text{max\_blocks\_k},\ \text{max\_blocks\_q}\big)
\]

效果：$m$ 越大（越靠后的 query tile），$\text{trip\_count}$ 越大，直至等于 $\text{max\_blocks\_k}$（满 K）。$m=0$ 时只有 1~2 个 K-tile——上三角被「跳过」了，根本不进入 MMA。其中 `get_masked_trip_count`（通常 ≤1）标记需要对角线边界 tile 调 `apply_mask`，`get_unmasked_trip_count = trip - masked` 是无需掩码的「全有效」快路径 trip。

再看 scheduler 层。`CausalIndividualTileScheduler` 把 grid 设为 `(H, num_m_blocks, B)` 但**反转 m_block 顺序**——让最后一个（计算最重的）m_block 先启动：

```
// last q tile launch first
if (blockIdx.y >= params.tile_max_q) {            // 尾巴块
  return make_coord(gridDim.y - 1 - blockIdx.y, _0{}, (blockIdx.x, blockIdx.z));
}
return make_coord(gridDim.y - 1 - row_idx, _0{}, (col_idx, blockIdx.z));  // 反转
```

同时把 Q（行）与 H（头）两个维度 swizzle 成 `TileQ=16 × TileH=8` 的小格，让相邻 CTA 访问的 K 数据尽量重叠，提升 L2 命中。

#### 4.3.3 源码精读

**mask 层裁剪**——`CausalMask::get_trip_count`：[fmha_fusion.hpp:197-215](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/collective/fmha_fusion.hpp#L197-L215)

```cpp
int max_blocks_k = Base::get_trip_count(...);          // ceil_div(SK, TileN)
if constexpr (IsQBegin) {
  int max_blocks_q = ceil_div((get<0>(blk_coord)+1)*get<0>(tile_shape), get<1>(tile_shape));
  return std::min(max_blocks_k, max_blocks_q);
} else {  // 本仓库用 <false>
  const int offset_q = get<1>(problem_size) - get<0>(problem_size);  // SK - SQ
  int max_blocks_q = ceil_div((get<0>(blk_coord)+1)*get<0>(tile_shape) + offset_q, get<1>(tile_shape));
  return std::min(max_blocks_k, max_blocks_q);
}
```

`get<0>(blk_coord)` 是 m_block，`get<0>(tile_shape)`=TileM，`get<1>(tile_shape)`=TileN。`get_masked_trip_count`（[fmha_fusion.hpp:217-231](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/collective/fmha_fusion.hpp#L217-L231)）把 trip 限制到约 $\lceil \text{TileM}/\text{TileN}\rceil$（对角带宽度），剩下的 `get_unmasked_trip_count`（[fmha_fusion.hpp:233-241](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/collective/fmha_fusion.hpp#L233-L241)）是纯下三角、无需 `apply_mask` 的快路径。

**scheduler 层重排**——`CausalIndividualTileScheduler`：[fmha_causal_tile_scheduler.hpp:65-76](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/kernel/fmha_causal_tile_scheduler.hpp#L65-L76) 算 grid（注意 grid.x=H 必须是 TileH=8 的倍数，这正是 fwd 入口 `h % TileH == 0` 校验的由来，见 4.4）；[fmha_causal_tile_scheduler.hpp:87-110](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/kernel/fmha_causal_tile_scheduler.hpp#L87-L110) 是坐标映射与「反转 + 尾巴处理」逻辑。

**scheduler 选择**——何时用 causal 版、何时用普通版，由 mask 类型驱动：[fmha_cutlass_fwd_sm100.cuh:82-90](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/fmha_cutlass_fwd_sm100.cuh#L82-L90)

```cpp
using TileScheduler = std::conditional_t<
    kIsPersistent,
    std::conditional_t<std::is_same_v<ActiveMask, CausalMask<false>> ||
                           std::is_same_v<ActiveMask, CausalMask<true>>,
                       CausalPersistentTileScheduler,
                       PersistentTileScheduler>,
    std::conditional_t<kIsMaskTileSchedulerValid,
                       CausalIndividualTileScheduler,
                       IndividualTileScheduler>>;
```

即：mask 是 `CausalMask` 才选 causal scheduler，否则退回通用 scheduler。

#### 4.3.4 代码实践

**实践目标**：把「scheduler 重排」与「mask 裁剪」两层分开，亲手算一个 causal 的 trip_count。

**操作步骤**：

1. 读 [fmha_fusion.hpp:197-241](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/collective/fmha_fusion.hpp#L197-L241)，确认 `get_trip_count / get_masked_trip_count / get_unmasked_trip_count` 三者的关系。
2. 读 [fmha_causal_tile_scheduler.hpp:87-110](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/kernel/fmha_causal_tile_scheduler.hpp#L87-L110)，找到「last q tile launch first」的分支。

**需要观察的现象 / 预期结果**：取 $S_Q=S_K=4096$，TileM=256，TileN=128，则 num_m_blocks=16、max_blocks_k=32。对各 m_block 用 `CausalMask<false>`（offset_q=0）算 trip_count：

| m_block | (m+1)·256 | max_blocks_q=⌈…/128⌉ | trip=min(32, q) |
| --- | --- | --- | --- |
| 0 | 256 | 2 | 2 |
| 7 | 2048 | 16 | 16 |
| 15 | 4096 | 32 | 32（满） |

可见前面的 query tile 只算 2 个 K-tile（上三角被裁掉），最后一块才算满 32 个——这正是「跳过上三角」的体现，而 scheduler 同时把这些重块优先启动。

> 待本地验证：可在 mainloop 加日志打印 `m_block` 与 `get_trip_count` 返回值，对照上表。

#### 4.3.5 小练习与答案

1. **问**：既然 causal scheduler 不删 tile，为什么还要单独搞一个 `CausalIndividualTileScheduler`，而不是复用 `IndividualTileScheduler`？
   **答**：因为 causal 下各 query tile 计算量差异巨大（前轻后重）。普通 Individual 按自然顺序启动，会让大量 SM 先去算轻块、算完空闲等重块，占用率差。causal 版把重块（大 m_block）优先启动，让 SM 尽早进入满负荷，并通过 Q/H swizzle 提升 K 的 L2 命中——这是为 causal 量身定制的负载与缓存优化。

2. **问**：`get_unmasked_trip_count` 存在的意义是什么？
   **答**：在 trip_count 内，只有最末一个（对角线）K-tile 需要逐元素 `apply_mask`，其余 K-tile 完全落在有效区内、无需任何判断。mainloop 对这部分「快路径」trip 可以跳过 `apply_mask` 调用，省掉逐元素分支——把掩码开销压缩到仅 1 个 tile。

---

### 4.4 fmha_options 与 pipeline_mla

#### 4.4.1 概念说明

剩下两个文件分别解决「调优开关如何编译期化」与「MLA 流水如何按字节同步」。

**`fmha_options.hpp`**：CUTLASS 风格的 Option/Tag 机制。kernel 有很多「可以调但不一定每次都传」的编译期开关（是否 persistent、stage 数、cluster 形状…）。与其把这些都做成必填模板参数，不如用一个可变参数包 `KernelOptions...`，调用方按需塞入 `Option<Tag::kIsPersistent, true_type>`，kernel 内部用 `find_option_t<Tag::kIsPersistent, 默认值, KernelOptions...>` 查找——传了就用传的，没传就用默认值。

**`pipeline_mla.hpp`**：u7-l2 讲过 SM100 的 warp-specialized 流水靠 producer/consumer barrier 同步。MLA 因为 K 与 V 同源、head_dim 不对称（latent 128 + rope 64），TMA 每次搬的字节数随阶段变化，需要一种「producer 在获取流水槽时一并声明本次要写多少字节」的同步。`PipelineTmaAsyncMla` 在 CUTLASS 通用 `PipelineTmaUmmaAsync` 之上加了 `producer_acquire_bytes`，用 `arrive_and_expect_tx(bytes)` 把「到达计数 + 字节计数」捆绑，让 consumer（UMMA）精确等到数据落盘。

#### 4.4.2 核心流程

**Option 机制**：`find_option<Tag, Default, Options...>` 是一个递归的编译期查找——逐个比较 `Option::tag == kTag`，命中则取该 Option，否则递归剩下一个，全不命中取 `Default`。`Tag` 枚举列出了所有可用开关位（[fmha_options.hpp:60-77](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/kernel/fmha_options.hpp#L60-L77)）。

**`kIsPersistent` 如何决定 scheduler**：fwd 入口 `call_run_fmha_fwd` 根据是否 causal/varlen 构造一个 Option：[fmha_cutlass_fwd_sm100.cu:21-24](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/fmha_cutlass_fwd_sm100.cu#L21-L24)

```cpp
static constexpr bool IsCausalMask = std::is_same_v<Mask, CausalMask<false>>;
using Option = std::conditional_t<IsCausalMask || (IsVarlen),
    Option<Tag::kIsPersistent, false_type>,    // causal/varlen → 非持久化
    Option<Tag::kIsPersistent, true_type>>;    // 普通非varlen → 持久化
```

`FwdRunner` 再读出来：[fmha_cutlass_fwd_sm100.cuh:79-80](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/fmha_cutlass_fwd_sm100.cuh#L79-L80)

```cpp
static constexpr bool kIsPersistent =
    find_option_t<Tag::kIsPersistent, true_type, KernelOptions...>::value;
```

即：**非 causal 且非 varlen** 才走 persistent；causal 或 varlen 一律用 individual（因为 causal 的重排逻辑与持久化的均匀跨步分配不兼容，varlen 的每 batch 长度不同也不宜持久化跨步）。

此外，入口还有一道「causal individual 是否可用」的运行时校验——要求头数能被 TileH=8 整除：[fmha_cutlass_fwd_sm100.cuh:325-334](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/fmha_cutlass_fwd_sm100.cuh#L325-L334)。满足则 `kIsMaskTileSchedulerValid=true`（用 `CausalIndividualTileScheduler`），否则退回普通 `IndividualTileScheduler`。

#### 4.4.3 源码精读

**Option 查找的递归模板**：[fmha_options.hpp:40-58](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/kernel/fmha_options.hpp#L40-L58)

```cpp
template<auto kTag, typename Default, typename Option, typename... Options>
struct find_option<kTag, Default, Option, Options...> :
  std::conditional_t<Option::tag == kTag, Option, find_option<kTag, Default, Options...>> {};
```

`Option` 包装：[fmha_options.hpp:79-83](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/kernel/fmha_options.hpp#L79-L83) 每个 `Option<kTag, Value>` 带 `static constexpr auto tag`。整个机制纯编译期，运行时零开销。

**MLA pipeline 的核心方法**：[pipeline_mla.hpp:176-196](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/common/pipeline_mla.hpp#L176-L196)

```cpp
void producer_acquire_bytes(uint32_t stage, uint32_t bytes, uint32_t phase, ProducerToken token) {
  if (token != BarrierStatus::WaitDone) {
    empty_barrier_ptr_[stage].wait(phase);     // 等消费者释放该 stage
  }
  if (params_.is_leader) {
    full_barrier_ptr_[stage].arrive_and_expect_tx(bytes);  // 声明「我将写 bytes 字节」
  }
}
```

`arrive_and_expect_tx(bytes)` 是 SM100 mbarrier 的「transactional barrier」语义：producer 抵达并预告字节数，consumer 端的 `wait` 要等到「抵达计数 + 实际写入字节数 ≥ 预告」才放行。这样无论 TMA 这一轮搬的是 K（576 维）还是 V（512 维）的不同字节数，同步都能正确。`consumer_release` 还针对 2-SM cluster MMA 用 `umma_arrive_multicast_2x1SM`（[pipeline_mla.hpp:228-247](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/common/pipeline_mla.hpp#L228-L247)）。

#### 4.4.4 代码实践

**实践目标**：追踪 `kIsPersistent` 从入口到 scheduler 选择的完整链路。

**操作步骤**：

1. 在 [fmha_cutlass_fwd_sm100.cu:21-24](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/fmha_cutlass_fwd_sm100.cu#L21-L24) 确认四种 (mask, varlen) 组合各产出哪个 `Option`。
2. 顺着 [fmha_cutlass_fwd_sm100.cuh:79-90](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/fmha_cutlass_fwd_sm100.cuh#L79-L90) 看 `kIsPersistent` 与 `ActiveMask` 如何共同决定 `TileScheduler`。
3. 在 [fmha_options.hpp:40-58](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/kernel/fmha_options.hpp#L40-L58) 验证：若 `KernelOptions...` 为空，`find_option_t<Tag::kIsPersistent, true_type>` 返回默认值 `true_type`（即默认 persistent）。

**需要观察的现象 / 预期结果**：四种组合的 scheduler 落点（假设 `h % 8 == 0`）：

| mask | varlen | kIsPersistent | scheduler |
| --- | --- | --- | --- |
| CausalMask | 否 | false | CausalIndividualTileScheduler |
| CausalMask | 是 | false | CausalIndividualTileScheduler |
| ResidualMask | 否 | true | PersistentTileScheduler |
| ResidualMask | 是 | false | IndividualTileScheduler |

> 待本地验证：可临时在 host 端用 `std::is_same_v<TileScheduler, ...>` 打印实际类型名核对。

#### 4.4.5 小练习与答案

1. **问**：为什么 causal 一定不走 persistent？
   **答**：persistent 靠 `block_idx += gridDim.x` 跨步均匀瓜分 tile，但 causal 下各 query tile 计算量差异极大（前轻后重），均匀跨步会造成严重负载不均；而 causal individual 的「重块优先」重排正是为对抗这种不均。两者设计目标冲突，故 causal 强制走 individual。

2. **问**：`producer_acquire_bytes` 里的 `arrive_and_expect_tx(bytes)` 为什么对 MLA 特别重要？
   **答**：MLA 的 K（latent+rope=576 维）与 V（latent=512 维）宽度不同，TMA 每次实际搬运字节数随对象而变。transactional barrier 让 producer 把「本次字节数」动态告诉 barrier，consumer 据此精确等待，无需为不同 head_dim 写不同的同步逻辑——这是支撑 u7-l2 「K/V 不同宽、独立 stage」的同步基础。

---

## 5. 综合实践

**任务**：把本讲四个模块串起来，画出一条「Python 传入 mask_mode → 最终每个 CTA 算哪些 K-tile」的完整决策链，并用 PyTorch 写一个参考实现验证 causal 裁剪的正确性。

**步骤**：

1. **追链路（源码阅读）**。从 [fmha_cutlass_fwd_sm100.cu:42](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/fmha_cutlass_fwd_sm100.cu#L42) 出发，依次填写下表每一格的依据（文件:行号）：

   | 决策点 | 依据代码 | 结果（以 causal、非 varlen、h=64 为例） |
   | --- | --- | --- |
   | MaskMode → ActiveMask | `fmha_cutlass_fwd_sm100.cu:50-54` | `CausalMask<false>` |
   | ActiveMask → kIsPersistent | `fmha_cutlass_fwd_sm100.cu:21-24` | false |
   | h%TileH==0 ? kIsMaskTileSchedulerValid | `fmha_cutlass_fwd_sm100.cuh:325-327` | true（64%8==0） |
   | TileScheduler 最终类型 | `fmha_cutlass_fwd_sm100.cuh:88-90` | `CausalIndividualTileScheduler` |
   | 每个 CTA 的 K-tile 数 | `fmha_fusion.hpp:197-215` | `min(⌈SK/128⌉, ⌈(m+1)·256/128⌉)` |

2. **PyTorch 参考验证（无 GPU 也可跑）**。下面的示例代码（非项目原有）用 PyTorch 复现 causal + 参差边界的掩码语义，对照 `CausalMask::apply_mask`：

   ```python
   # 示例代码：验证 causal 下三角 + 参差边界的掩码（对照 fmha_fusion.hpp:258-275）
   import torch
   def causal_residual_mask(sq, sk, tile_m=256, tile_n=128):
       # 模拟每个 m_block 实际需要迭代的 K-tile 数（对照 get_trip_count, kIsQBegin=false）
       offset_q = sk - sq
       num_m_blocks = (sq + tile_m - 1) // tile_m
       max_blocks_k = (sk + tile_n - 1) // tile_n
       for m in range(num_m_blocks):
           max_blocks_q = ((m + 1) * tile_m + offset_q + tile_n - 1) // tile_n
           trip = min(max_blocks_k, max_blocks_q)
           print(f"m_block={m}: trip_count={trip}/{max_blocks_k}  (跳过 {max_blocks_k-trip} 个上三角 K-tile)")
   causal_residual_mask(sq=4096, sk=4096)
   ```

   预期输出与 4.3.4 的表格一致（m=0→2, m=7→16, m=15→32）。

3. **思考题**：把 `sq=sk=4096` 改成推理续写场景 `sq=1, sk=4096`（offset_q=4095），重跑上式，观察 trip_count 的变化，并解释为何此时几乎所有 K-tile 都是「有效」的（这正是 decode 阶段几乎无上三角浪费的原因）。

> 本综合实践无需 GPU，全部可在纯 Python / 源码阅读完成；带 GPU 时可进一步调用 `flash_attn_varlen_func` 对比输出。

## 6. 本讲小结

- **两层掩码表达**：运行时 `MaskMode`（`mask.cuh`）在 fwd 入口被派发成编译期策略类型 `CausalMask<false>` / `ResidualMask`（`fmha_fusion.hpp`），从而把掩码判断编译期化、消除 softmax 内层分支。
- **跳过 tile 的真相**：causal scheduler 本身**不删 tile**，只做「重块优先」重排与 Q/H swizzle（提升 L2 与占用率）；真正的上三角裁剪由 `CausalMask::get_trip_count` 在 mainloop 内限制每个 CTA 的 K-tile 迭代数完成，`apply_mask` 仅处理对角线边界那一个 tile。
- **四选一的 scheduler**：由 `kIsPersistent`（option 机制）与 `ActiveMask`（是否 causal）共同决定——causal/普通 × individual/persistent 四个类，causal 与 varlen 都强制走 individual。
- **Option 机制**：`find_option_t<Tag, Default, Options...>` 在可变参数包里编译期查找调优开关，传了用传的、没传用默认，运行时零开销；`kIsPersistent` 即由此读出。
- **MLA pipeline**：`PipelineTmaAsyncMla::producer_acquire_bytes` 用 mbarrier 的 transactional 语义（`arrive_and_expect_tx(bytes)`）让 TMA producer 动态声明每轮字节数，支撑 K/V 不同宽度的同步。
- **入口护栏**：`h % TileH(=8) == 0` 是 causal individual scheduler 可用的硬条件，不满足则退回普通 individual。

## 7. 下一步学习建议

- 本讲聚焦 **fwd** 的 tile scheduler 与 mask。下一篇 **u7-l4《Autograd fwd/bwd 与 dense 接口》** 会进入反向传播：看 `MaskMode` 如何一路传到 bwd kernel、bwd 的 workspace 如何分配，以及 `FlashAttnVarlenFunc` 的 `save_for_backward` 流程。
- 若你对 causal mask 的 `kIsQBegin` 两种朝向（Q 在首 / Q 在尾）感兴趣，可对照 [fmha_fusion.hpp:187-277](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/collective/fmha_fusion.hpp#L187-L277) 的两个分支，思考推理 decode（Q 在尾）与训练 prefill（方阵）的差异。
- 想深入 SM100 流水同步，可读 [pipeline_mla.hpp](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm100/prefill/dense/common/pipeline_mla.hpp) 与 u7-l2 提到的各 `PipelineQ/KV/S/C/O`，把「按字节同步」与「warp 角色分工」对照起来理解。
- 回到 u2-l3 的 `DISPATCH_*` 宏与本讲的 Option/Tag 机制做一次对比：二者都是「运行时值编译期化」，但前者用 IIFE lambda、后者用模板特化查找，体会 CUTLASS 与 FlashMLA 自家代码在风格上的异同。
