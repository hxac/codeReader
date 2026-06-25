# 布局候选与最优配置选择

## 1. 本讲目标

上一讲 u5-l1 把两个数据结构讲透了：`GemmDesc` 描述「要算什么」，`GemmConfig` 描述「怎么算」。但二者之间的「黑盒」——模板函数 `get_best_config(desc)`——上一讲只用一句「沿 `check_validity → 选 layout → storage → pipeline → launch` 的单向依赖链推断」带过了。本讲就来拆开这个黑盒。

本讲要回答三个层层递进的问题：

1. **候选从哪来？** `GemmConfig.layout` 里的 `block_m`、`block_n`、`block_k`、`cluster_m`、`cluster_n` 不是拍脑袋定的，而是 `ArchSpec::get_layout_candidates(desc)` 按一套规则枚举出来的，再用一串约束（寄存器压力、bank conflict、swizzle、流水线深度）过滤。
2. **哪个最优？** 候选可能有几十个，`ArchSpec::get_layout_info(desc, layout)` 给每个候选算一组指标（`num_waves`、`last_wave_util`、`num_cycles`），`ArchSpec::compare` 决定谁赢。
3. **两代架构有何不同？** 同一个 `get_best_config` 骨架，套上 `SM90ArchSpec` 与 `SM100ArchSpec` 后，候选规则、代价模型、比较策略差别很大。

学完后你应当能够：

- 说清 `get_best_config` 的「枚举候选 → 逐个评估 → 选最优 → 推断 storage/pipeline/launch」三段式流程。
- 解释 `block_m` / `block_n` / `cluster` 候选的生成规则与过滤约束（寄存器溢出、bank conflict、swizzle、流水线深度、multicast 合法性）。
- 用 `num_waves`、`last_wave_util`、`num_cycles` 三个指标，手算给定形状的 wave 数与尾波利用率。
- 对比 `SM90ArchSpec` 与 `SM100ArchSpec` 的真实差异：multicast 维度、`swap_ab`、代价模型成熟度、比较策略。

本讲把上承的「数据结构」与下接的「设备内核启动」连接起来：选出的 `Layout` 决定了 `BLOCK_M/N/K` 模板参数（u3-l2）与 `cluster_dim` 启动属性（u4-l3）。

## 2. 前置知识

阅读本讲前，请确认你已经掌握以下概念（它们在前置讲义中建立）：

- **`GemmDesc` / `GemmConfig` 四子结构**（u5-l1）。本讲里，`Layout`（`block_m/n/k`、`cluster_m/n`）、`StorageConfig`（`swizzle`）、`PipelineConfig`（`num_stages`）、`LaunchConfig` 会反复出现；三类枚举 `MmaKind`（决定 `block_k`）、`GemmType`、`KernelType` 驱动候选分支。
- **swizzle 模式**（u4-l2）。共享内存用 32B/64B/128B 的 XOR 重排消除 bank conflict；候选过滤里要求「swizzle 原子足够大」。
- **`num_sms` / `tc_util` 旋钮**（u4-l1）。`num_sms` 直接进入 wave 数计算，且必须为偶数。
- **宿主 Runtime 类与 `get_best_config` 调用点**（u2-l3 / u3-l2）。各 `impls/sm*_*.hpp` 在构造 `GemmDesc` 后第一件事就是 `const auto config = get_best_config<SMxxArchSpec>(desc);`。

你还需要一组术语：

- **tile / block（分块）**：`block_m × block_n` 是一个线程块（CTA）负责计算的输出瓦片大小；`block_k` 是 K 方向一次累加的厚度。
- **wave（波）**：把全部输出 block 平铺到 SM 上，SM 一次最多并行处理 `num_sms` 个 block，多出来的 block 要分成若干「波」依次处理。
- **multicast / cluster（多播 / 集群）**：相邻 CTA 组成 cluster，可共享同一份 TMA 加载以节省显存带宽。`cluster_m * cluster_n` 是集群大小，DeepGEMM 只支持集群大小 ≤ 2。
- **寄存器溢出（register spill）**：分块太大时寄存器装不下，溢出到本地内存，性能骤降。
- **bank conflict（存储体冲突）**：共享内存分 32 个 bank，若多个线程的访问落到同一 bank，访问被串行化。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [csrc/jit_kernels/heuristics/common.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/common.hpp) | 模板函数 `get_best_config<ArchSpec>(desc)`：本讲总入口，编排「枚举 → 评估 → 选优 → 推断」三段式。 |
| [csrc/jit_kernels/heuristics/sm90.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm90.hpp) | `SM90ArchSpec`：Hopper 上候选枚举、完整 L1/L2 代价模型、纯 `num_cycles` 比较。 |
| [csrc/jit_kernels/heuristics/sm100.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm100.hpp) | `SM100ArchSpec`：Blackwell 上的对应实现，含 `swap_ab`、`tmem` 容量检查、字典序多准则比较。 |
| [csrc/jit_kernels/heuristics/config.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/config.hpp) | `Layout`、`LayoutInfo`（评估指标）等数据结构定义（u5-l1 已讲结构，本讲用其语义）。 |
| [csrc/jit_kernels/heuristics/utils.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/utils.hpp) | `get_swizzle_mode`：贪心选最大可整除的 swizzle 原子。 |
| [csrc/jit_kernels/heuristics/runtime.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/runtime.hpp) | `HeuristicsRuntime`：提供 `block_n_multiple_of` 等调优旋钮，影响候选步长。 |
| [csrc/jit_kernels/impls/sm90_fp8_gemm_1d1d.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/sm90_fp8_gemm_1d1d.hpp) | 真实调用点 `get_best_config<SM90ArchSpec>(desc)`，演示 `desc` 如何喂入。 |
| [deep_gemm/include/deep_gemm/common/types.cuh](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/common/types.cuh) | `MmaKind` / `GemmType` / `KernelType` 三类枚举（u5-l1 已讲，本讲引用其取值）。 |

## 4. 核心概念与源码讲解

### 4.1 布局候选枚举

#### 4.1.1 概念说明

配置选择的第一步不是「评估」，而是「先生成一份候选清单」。`get_best_config` 把这件事委托给 `ArchSpec::get_layout_candidates(desc)`，它返回一个 `std::vector<Layout>`。

这套枚举的核心思想是 **「先宽生成，再用约束过滤」**：

- 先按经验给出「可能合理」的 `block_m` / `block_n` 取值集合与 `cluster` 组合。
- 再把这些取值做笛卡尔积，逐个用一串硬约束筛掉会出问题（寄存器溢出、bank conflict、swizzle 太小、流水线太浅、multicast 不整除）的组合。
- 活下来的才进候选清单。

为什么要「枚举 + 过滤」而不是「直接推导最优」？因为分块大小、集群、流水线深度之间存在大量耦合（块大寄存器溢出但流水线深；块小无溢出但 wave 多），没有闭式解，只能枚举候选再用代价模型打分（见 4.2）。

#### 4.1.2 核心流程

`get_best_config` 的骨架在 [csrc/jit_kernels/heuristics/common.hpp:14-52](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/common.hpp#L14-L52)，先校验、再枚举候选：

```
get_best_config<ArchSpec>(desc):
  desc.check_validity()                      # 合法性校验
  candidates = ArchSpec::get_layout_candidates(desc)
  assert(candidates 非空)
  # 选最优（见 4.2）
  best = candidates[0]
  for 其余 candidate:
    若 ArchSpec::compare(info(candidate), info(best)) 为真: 更新 best
  # 推断其余配置
  storage  = ArchSpec::get_storage_config(desc, best)
  pipeline = ArchSpec::get_pipeline_config(desc, best, storage)
  launch   = ArchSpec::get_launch_config(desc, best)
  return GemmConfig{ best, storage, pipeline, launch }
```

SM90 上候选枚举的骨架（去掉细节）如下，是一个四层嵌套循环 + 一串 `continue` 过滤：

```
# SM90 get_layout_candidates 骨架
block_k = 128 / element_size                 # 固定
for cluster_m in {1,2}:
  for cluster_n in {1,2}:
    若 cluster_m*cluster_n > 2: skip          # 只支持集群大小 ≤ 2
    若 num_sms % (cluster_m*cluster_n) != 0: skip
    for block_m in block_m_candidates:
      for block_n in block_n_candidates:
        若 不满足约束: skip                   # 寄存器/swizzle/流水线/multicast/1D2D 展开
        candidates.append(Layout{block_m,block_n,block_k,cluster_m,cluster_n})
```

#### 4.1.3 源码精读

**(1) `block_m` 候选：形状驱动的小块优化**

`block_m` 候选在 [csrc/jit_kernels/heuristics/sm90.hpp:16-36](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm90.hpp#L16-L36) 生成，按 `gemm_type` 分支。对最常见的 Normal/Batched/K-Grouped：

```cpp
block_m_candidates = {64, 128};
// NOTES: smaller block M can avoid TMA L2 OOB bound
if (desc.m <= 16) block_m_candidates.push_back(16);
if (desc.m <= 32) block_m_candidates.push_back(32);

// BF16 output GEMM supports 256
if (desc.cd_dtype != torch::kFloat)
    block_m_candidates.push_back(256);
```

这段做了三件事，对应三条经验规则：

- **默认 64/128**：这两个值是 SM90 WGMMA（M 固定为 64，见 u6-l2）友好、寄存器压力可控的常用分块。
- **小 M 时追加更小的块**：当 `m` 很小（解码 batch 场景），`block_m=128` 会让一个 block 算 128 行但只有 `m` 行有效，大量线程空转、TMA 还要加载大片 padding（注释说的「TMA L2 OOB」风险）。追加 16/32 让分块贴合真实 `m`。
- **BF16 输出时追加 256**：BF16 占 2 字节，输出寄存器/共享内存占用比 FP32 小，允许更大的 `block_m`；FP32 输出则不行。

`MGroupedMasked` 用 `{64, 128}`；`MGroupedContiguous` 的 `block_m` 直接锁定为分组对齐量 `mk_alignment_for_contiguous_layout`（u5-l3 详讲），因为连续分组要求每段对齐。

**(2) `block_n` 候选：步长枚举 + FP32 输出的 bank-conflict 避让**

`block_n` 候选在 [csrc/jit_kernels/heuristics/sm90.hpp:38-57](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm90.hpp#L38-L57)：

```cpp
int step = std::lcm(16, heuristics_runtime->get_block_n_multiple_of());
int start = step;
// Avoid bank conflicts for 1D1D kernel FP32 output
if (desc.kernel_type == KernelType::Kernel1D1D and desc.cd_dtype == torch::kFloat) {
    DG_HOST_ASSERT(desc.major_a == cute::UMMA::Major::K);
    DG_HOST_ASSERT(desc.major_b == cute::UMMA::Major::K);
    start = 24;
    block_n_candidates.push_back(16);
}
// Register spills
int end = 256;
if (desc.kernel_type == KernelType::Kernel1D2D) end = 192;
if (desc.kernel_type == KernelType::Kernel1D1D) end = 160;
for (int i = start; i <= end; i += step)
    block_n_candidates.push_back(i);
```

三个要点：

- **步长与起点**：默认 `step = lcm(16, block_n_multiple_of)`（旋钮默认 1，故 step=16），从 `step` 起按 step 递增。`block_n_multiple_of` 是用户约束 `block_n` 对齐值的旋钮（见 4.1.4）。
- **1D1D + FP32 输出从 24 起步**：1D1D kernel 用单个 warp-group 写回输出（`store_block_m = wgmma_m = 64`，见下文 `get_storage_config`）。FP32 是 4 字节，若 `block_n` 取到与共享内存 bank 周期对齐的「整数倍」值，多个线程的写会撞到同一 bank（bank conflict）。所以这条分支把起点抬到 `24`（非整数倍、偏离对齐），并显式补一个 `16`（足够小、冲突可接受）。该分支同时断言 A、B 都是 K-major，即仅 NT 布局才走这里。
- **上界随 kernel 类型收紧**：默认 `end=256`；1D2D 收到 192、1D1D 收到 160——`block_n` 越大寄存器占用越高，越容易溢出，故更受限的 kernel 类型给更小的上界。

**(3) `block_k`：固定不变量**

`block_k` 不枚举，固定为 [csrc/jit_kernels/heuristics/sm90.hpp:59-60](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm90.hpp#L59-L60)：

```cpp
const int block_k = 128 / get_element_size(desc.get_mma_kind());
```

BF16（2 字节）→ `block_k=64`；FP8/FP4（1 字节）→ `block_k=128`。它对齐张量核 MMA 的粒度。注意：**SM90 与 SM100 的 `block_k` 公式完全相同**——这是两代架构的共同不变量，不是差异点（差异见 4.3）。

**(4) multicast 与集群约束**

是否启用 multicast 在 [csrc/jit_kernels/heuristics/sm90.hpp:62-67](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm90.hpp#L62-L67) 判定：

```cpp
const bool disable_multicast =
    (desc.gemm_type == GemmType::KGroupedContiguous and desc.num_groups > 4) or
    (desc.gemm_type == GemmType::Batched);
```

K-Grouped 且组数多时（>4）关闭 multicast（启发式认为收益不抵开销），Batched 直接不支持。集群循环 [sm90.hpp:70-114](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm90.hpp#L70-L114) 里逐层过滤：`cluster_m*cluster_n > 2` 跳过、`num_sms` 不能整除集群大小则跳过。

**(5) 组合级过滤约束**

每个 `(block_m, block_n, cluster)` 组合还要过 [sm90.hpp:83-108](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm90.hpp#L83-L108) 的五道关：

```cpp
// 1D2D kernel 的展开要求
if (desc.kernel_type == KernelType::Kernel1D2D and block_n > block_k and
    (block_n % (block_n - block_k) != 0 and block_k % (block_n - block_k) != 0))
    continue;
// masked/psum 布局下 multicast 必须能整除 N 分块数
if ((desc.gemm_type == GemmType::MGroupedMasked or
     desc.gemm_type == GemmType::MGroupedContiguousWithPsumLayout) and
    ceil_div(desc.n, block_n) % (cluster_m * cluster_n) != 0)
    continue;
// 至少有一维 ≤ 128，避免寄存器溢出
if (block_m > 128 and block_n > 128) continue;
// swizzle 原子要足够大（32B 性能差）
if (storage_config.swizzle_a_mode % 64 != 0 or storage_config.swizzle_b_mode % 64 != 0)
    continue;
// 流水线至少 3 级（小块至少 4 级）以掩盖 TMA 延迟
int num_stages = get_pipeline_config(desc, layout, storage_config).num_stages;
if (num_stages < 3 or (block_m * block_n < 128 * 192 and num_stages < 4))
    continue;
```

逐条解读：

- **1D2D 展开**：1D2D kernel 把 `block_n` 拆成多段循环展开，要求 `block_n - block_k` 能整除 `block_n` 或 `block_k` 之一，否则展开不齐。
- **multicast 整除**：masked/psum 布局里，N 方向分块数必须能被集群大小整除，否则 multicast 无法均匀分配。
- **寄存器压力**：`block_m > 128 且 block_n > 128` 一律拒绝——两维都太大必然溢出，至少留一维 ≤ 128。
- **swizzle 原子**：调用 `get_swizzle_mode`（[utils.hpp:12-21](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/utils.hpp#L12-L21)，贪心取 128/64/32/16 中最大可整除者），要求 A、B 的 swizzle 至少 64B（32B 性能差，直接淘汰）。
- **流水线深度**：用 `get_pipeline_config` 算出该布局能塞下几级 stage，至少要 3 级才能掩盖 TMA 延迟；小块（`block_m*block_n < 128*192`）计算量小，更需要 4 级。

> 注意：过滤时会现场调用 `get_storage_config` / `get_pipeline_config`，所以「枚举」与「推断其余配置」在实现上是交织的——但这只是用来过滤候选，最终 `GemmConfig` 里的 storage/pipeline 仍在选出最优 layout 后重新推断一遍（common.hpp:29-31）。

#### 4.1.4 代码实践

**实践目标**：用 `DG_PRINT_CONFIGS=1` 观察启发式为不同形状选出的 `GemmConfig` 与 `LayoutInfo`，验证「小 M 倾向更小 block_m」「1D1D/FP32 输出 block_n 从 24 起步」两条规则。

**操作步骤**：

1. 确认已按 u1-l2 构建 DeepGEMM，`import deep_gemm` 成功（需 SM90 或 SM100 GPU；本环境无 GPU，具体输出**待本地验证**）。
2. 写一个最小脚本（示例代码，非项目原有文件），分别用「大 M」和「小 M（如 m=16）」两种形状调用 SM90 的 FP8 GEMM，并切换 FP32 / BF16 输出：

   ```python
   # 示例代码：观察启发式选配置
   import os
   os.environ['DG_PRINT_CONFIGS'] = '1'
   import torch, deep_gemm
   from deep_gemm.utils.fp8_utils import per_token_cast_to_fp8, per_block_cast_to_fp8

   def run(m, n, k, cd_dtype):
       a, a_sf = per_token_cast_to_fp8(torch.randn(m, k, device='cuda'), torch.tensor([k], device='cuda'))
       b, b_sf = per_block_cast_to_fp8(torch.randn(n, k, device='cuda'), torch.tensor([k], device='cuda'))
       d = torch.empty(m, n, device='cuda', dtype=getattr(torch, cd_dtype))
       deep_gemm.fp8_fp4_gemm_nt((a, a_sf), (b, b_sf), d)   # cd_dtype 控制输出精度
       print(f'--- m={m} n={n} k={k} cd={cd_dtype} done ---')

   run(4096, 4096, 4096, 'bfloat16')   # 大 M，BF16 输出
   run(16,   4096, 4096, 'bfloat16')   # 小 M
   run(4096, 4096, 4096, 'float32')    # FP32 输出（1D1D 路径）
   ```

   > 上述 `per_token_cast_to_fp8` / `per_block_cast_to_fp8` 等 API 名与签名以 `deep_gemm/utils/` 与 `tests/test_fp8_fp4.py` 的真实实现为准；运行前请对照真实函数签名调整，**待本地验证**。

3. 观察控制台：`get_best_config` 在 [common.hpp:40-50](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/common.hpp#L40-L50) 会按 `GemmDesc(...): GemmConfig(...), LayoutInfo(...)` 格式打印（同一 `desc` 只打印一次）。

**需要观察的现象与预期结果**：

- **小 M → 更小 block_m**：`m=16` 的行里，`Layout` 的 `block_m` 应出现 `16` 或 `32`（小 M 才追加的候选）；`m=4096` 的行 `block_m` 多为 `64`/`128`。原理：小 M 时大 block_m 会算大片 padding、TMA 加载浪费且可能 L2 OOB。
- **1D1D/FP32 输出 → block_n 从 24 起**：FP32 输出（`cd_dtype=float32`）那行的 `block_n` 取值集合应是 `{16, 24, 40, 56, …}`（起点 24、步长 16，外加 16），而不会出现 32/64 这类与 bank 周期对齐的值。原理：1D1D kernel 单 warp-group 写 FP32 输出，避开 bank conflict。
- **BF16 输出 → block_m 可达 256**：BF16 行的候选/选中 `block_m` 可能含 256，FP32 行不会。

> 若手头没有 GPU，可改为「源码阅读型实践」：对 `m=16`、`cd_dtype=kFloat`、`kernel_type=Kernel1D1D` 手写一份 `GemmDesc`，人工走一遍 [sm90.hpp:16-57](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm90.hpp#L16-L57)，写出 `block_m_candidates` 与 `block_n_candidates` 两个集合，验证上述预期。

#### 4.1.5 小练习与答案

**练习 1**：在 SM90 上，`kernel_type=Kernel1D1D`、`cd_dtype=kFloat`、`block_n_multiple_of=1` 时，`block_n_candidates` 的前 5 个值是哪些？

**答**：`step = lcm(16,1) = 16`，FP32 输出分支设 `start=24` 并先 push `16`，循环 `i=24,40,…,160`。故前 5 个为 `16, 24, 40, 56, 72`。

**练习 2**：为什么 `block_m > 128 且 block_n > 128` 的组合会被 [sm90.hpp:94-95](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm90.hpp#L94-L95) 直接淘汰？

**答**：两维都超过 128 时，单个 block 的累加器与中间数据寄存器占用过高，必然溢出到本地内存（register spill），性能骤降。约束「至少一维 ≤ 128」是从寄存器预算出发的硬上限。

**练习 3**：`get_swizzle_mode(128, 1)`（block_k=128、FP8 即 1 字节）返回什么？为什么候选过滤要求它 `% 64 == 0`？

**答**：`get_swizzle_mode` 在 `{128,64,32,16}` 中取首个能被 `128*1=128` 整除者，即 `128`。`128 % 64 == 0` 通过。要求 swizzle ≥ 64B 是因为 32B swizzle 性能差（消除 bank conflict 的能力弱），源码注释明确「32B's performance is low」。

---

### 4.2 布局评估与比较

#### 4.2.1 概念说明

有了候选清单，第二步是「打分排序」。`ArchSpec::get_layout_info(desc, layout)` 给每个候选算出一组评估指标，打包成 `LayoutInfo`：

```cpp
struct LayoutInfo {
    int num_waves;          // 需要几「波」才能铺满全部 block
    int last_wave_util;     // 最后一波里有多少个 block 在干活
    int64_t num_cycles;     // 代价模型估算的周期数（越小越快）
    Layout layout;
};
```

（定义见 [csrc/jit_kernels/heuristics/config.hpp:160-173](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/config.hpp#L160-L173)。）

三个指标的直觉：

- **`num_waves`**：总 block 数除以 SM 数向上取整。波数越多，尾波越可能只有零星 block 在跑、其余 SM 空闲——浪费。
- **`last_wave_util`**：最后一波的 block 数。它越接近 `num_sms`，尾波利用率越高。
- **`num_cycles`**：把数据搬运与计算的周期数建模成一个数，越小越快。它是 SM90 上比较的唯一依据。

#### 4.2.2 核心流程

`get_best_config` 选最优的循环在 [common.hpp:18-26](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/common.hpp#L18-L26)：

```cpp
auto layout = layout_candidates[0];
auto layout_info = ArchSpec::get_layout_info(desc, layout);
for (int i = 1; i < ...; ++ i) {
    const auto candidate_info = ArchSpec::get_layout_info(desc, layout_candidates[i]);
    if (ArchSpec::compare(candidate_info, layout_info))
        layout = layout_candidates[i], layout_info = candidate_info;
}
```

注意 `compare(a, b)` 的语义：**返回 `true` 表示 `a` 优于当前最优 `b`**（即 `a` 应当取代 `b`）。这是一个「严格更优」比较器，等价时不替换，保证结果稳定（首个出现的最优胜出）。

整段是线性扫描选最小（最优），不是排序——\(O(n)\) 即可，因为只要全局最优。

#### 4.2.3 源码精读

**(1) SM90 代价模型：建模 L1/L2 周期**

SM90 的 `get_layout_info` 在 [sm90.hpp:201-238](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm90.hpp#L201-L238)。核心是三步：

第一步，算总 block 数与 wave 结构（[sm90.hpp:202-208](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm90.hpp#L202-L208)）：

```cpp
const auto num_blocks =
    ceil_div(desc.get_expected_m(), layout.block_m) *
    ceil_div(desc.get_expected_n(), layout.block_n) *
    desc.get_expected_num_groups();
const auto num_waves = ceil_div(num_blocks, desc.num_sms);
const auto num_last_blocks = num_blocks % desc.num_sms;
const auto last_wave_util = num_last_blocks == 0 ? desc.num_sms : num_last_blocks;
```

其中 `num_blocks` 是 M、N、组数三个方向分块数的乘积。注意用的是 `expected_*`（`expected_m/n/num_groups`），即用「预期形状」而非瞬时形状评估——这让解码等形状会变化的场景也能选到稳态最优（`expected_*` 为 0 时回退到真实 `m/n`）。

第二步，建模每个 block 的数据搬运量与周期（[sm90.hpp:210-231](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm90.hpp#L210-L231)）。关键公式：

\[
\text{wave\_efficiency} = \frac{\text{num\_blocks}}{\text{num\_waves} \times \text{num\_sms}}
\]

`wave_efficiency` 是「平均每波的占用率」：理想情况下每波都跑满 `num_sms` 个 block，实际受尾波拖累。再把 L1、L2 两条带宽瓶颈各自算出周期，取较大者后除以占用率，得到总周期：

\[
\text{num\_cycles} = \frac{\max(\text{num\_l1\_cycles},\ \text{num\_l2\_cycles})}{\text{wave\_efficiency}}
\]

直觉：HBM 带宽与总算力对同一问题恒定、不随配置变化，所以模型只盯 **L1/L2 这两个会随配置变化的瓶颈**——`block` 越大单 block 搬运量越大但 block 数越少，存在权衡，代价模型正是来量化这个权衡。`cluster_m/cluster_n` 通过 multicast 让相邻 block 共享 A/B 加载，故 `num_bytes_l2_ab` 里 `block_m/cluster_n + block_n/cluster_m` 体现了 multicast 对 L2 带宽的节省。

第三步，单波 multicast 惩罚（[sm90.hpp:234-235](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm90.hpp#L234-L235)）：

```cpp
if (layout.cluster_n * layout.cluster_m > 1 and num_waves <= 1)
    num_cycles = std::numeric_limits<int64_t>::max();
```

只有一波时（`num_waves <= 1`）启用 multicast 反而有害：multicast 把两个 CTA 绑成一个集群，单波场景下相当于把可用并行度砍半，没有后续波来掩盖。故直接置为周期无穷大，让比较器淘汰它。

**(2) SM90 比较：纯周期**

[sm90.hpp:241-243](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm90.hpp#L241-L243) 极简：

```cpp
static bool compare(const LayoutInfo& a, const LayoutInfo& b) {
    return a.num_cycles < b.num_cycles;
}
```

SM90 上一切由代价模型说了算：`num_cycles` 越小越优。`num_waves` / `last_wave_util` 已被吸收进 `num_cycles`（通过 `wave_efficiency`），所以比较时无需单列。

#### 4.2.4 代码实践

**实践目标**：手算一个形状的 wave 结构，与 `DG_PRINT_CONFIGS=1` 打印的 `LayoutInfo` 对照。

**操作步骤**：

1. 取一个 Normal GEMM，`m=1024, n=4096, k=4096`，SM90（`num_sms=132`，H100），假设选出 `block_m=128, block_n=128, cluster=(1,1)`。
2. 手算：

   \[
   \text{num\_blocks} = \lceil 1024/128\rceil \times \lceil 4096/128\rceil = 8 \times 32 = 256
   \]

   \[
   \text{num\_waves} = \lceil 256/132\rceil = 2,\quad
   \text{last\_wave\_util} = 256 \bmod 132 = 124
   \]

   \[
   \text{wave\_efficiency} = 256 / (2 \times 132) \approx 0.970
   \]

3. 在 `DG_PRINT_CONFIGS=1` 下运行同一形状（**待本地验证**具体 `num_cycles`），核对打印的 `num_waves=2, last_wave_util=124`。

**需要观察的现象与预期结果**：`num_waves` 与 `last_wave_util` 应与手算一致；`num_cycles` 因涉及带宽常数（`l2_bandwidth_per_cycle` 等，[sm90.hpp:211-212](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm90.hpp#L211-L212)）需在真实机器上读出。若把 `num_sms` 调小（`deep_gemm.set_num_sms(66)`），`num_waves` 会升至 4、`wave_efficiency` 下降，`num_cycles` 增大——可借此验证旋钮对选配置的影响。

#### 4.2.5 小练习与答案

**练习 1**：`m=256, n=256, block_m=128, block_n=128, num_sms=132`，求 `num_waves` 与 `last_wave_util`。

**答**：`num_blocks = 2×2 = 4`；`num_waves = ceil(4/132) = 1`；`num_last_blocks = 4 % 132 = 4`，`last_wave_util = 4`（不满一波，利用率极低）。

**练习 2**：练习 1 中若某候选 `cluster=(2,1)`（multicast），`get_layout_info` 会把它判成什么样？

**答**：因 `num_waves <= 1` 且集群大小 > 1，触发 [sm90.hpp:234-235](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm90.hpp#L234-L235) 的惩罚，`num_cycles` 被置为 `int64_t` 最大值，`compare` 中必败。即单波问题下 multicast 候选被淘汰。

**练习 3**：为何 SM90 的 `compare` 只比 `num_cycles`，而不显式比 `num_waves`？

**答**：因为 `num_cycles` 已通过 `wave_efficiency = num_blocks / (num_waves * num_sms)` 把 wave 结构（含尾波浪费）吸收进去。再单比 `num_waves` 会重复计数。`num_waves` / `last_wave_util` 主要供打印诊断与人脑理解，不直接参与 SM90 的比较决策。

---

### 4.3 架构特化 ArchSpec：SM90 vs SM100

#### 4.3.1 概念说明

`get_best_config` 是模板函数 [common.hpp:13-14](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/common.hpp#L13-L14)，行为完全由模板参数 `ArchSpec` 决定。所有 SM90 的 `impls/sm90_*.hpp` 调 `get_best_config<SM90ArchSpec>(desc)`，所有 SM100 的 `impls/sm100_*.hpp` 调 `get_best_config<SM100ArchSpec>(desc)`（例如 [sm90_fp8_gemm_1d1d.hpp:99](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/sm90_fp8_gemm_1d1d.hpp#L99) 与 [sm100_fp8_fp4_gemm_1d1d.hpp:114](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/sm100_fp8_fp4_gemm_1d1d.hpp#L114)）。

二者共享 `get_best_config` 的「枚举 → 评估 → 选优 → 推断」骨架，但在**候选规则、代价模型、比较策略**上差别显著。这一节就来对比这些真实差异。

> 准确性提示：两代架构的 `block_k` 公式完全相同（均为 `128 / element_size`），是共同不变量。manifest 里提到的「block_k」并非差异点；真正的差异在下表所列各项。

#### 4.3.2 核心流程

调用方按 `device_runtime->get_arch_major()`（u4-l1）决定走哪条路径：`9` → SM90 impl → `SM90ArchSpec`；`10` → SM100 impl → `SM100ArchSpec`。`ArchSpec` 同时承担两职：既是候选枚举/评估的规则集，又通过 `smem_capacity` 常量（两代均为 232448 字节）被其它 impl（如 mqa_logits）借用作共享内存上限校验。

#### 4.3.3 源码精读

两代 `ArchSpec` 的真实差异汇总如下表，逐项对应源码：

| 维度 | SM90（[sm90.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm90.hpp)） | SM100（[sm100.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm100.hpp)） |
| --- | --- | --- |
| **`swap_ab`（交换 A/B）** | 断言 `swap_ab==0`，不支持（[sm90.hpp:125](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm90.hpp#L125)） | 枚举 `swap_ab∈{0,1}`，m-grouped 强制开启（[sm100.hpp:47](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm100.hpp#L47), [L32-35](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm100.hpp#L32-L35)） |
| **multicast 维度** | `cluster_m`、`cluster_n` 各自可到 2，即 M 或 N 维均可 multicast（[sm90.hpp:70-75](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm90.hpp#L70-L75)） | 受 `swap_ab` 约束只走一维：`swap_ab=0` 只允许 `cluster_m` 到 2，`swap_ab=1` 只允许 `cluster_n` 到 2（[sm100.hpp:76-88](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm100.hpp#L76-L88)） |
| **`block_m` 候选** | 多候选 `{64,128}`，小 M 追加 16/32，BF16 追加 256（[sm90.hpp:17-30](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm90.hpp#L17-L30)） | 按 `m` 分档单选：`m≤32→{32}`、`m≤64→{64}`、否则 `{128}`（[sm100.hpp:62-64](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm100.hpp#L62-L64)） |
| **代价模型** | 完整 L1/L2 周期模型，`num_cycles` 有意义（[sm90.hpp:210-231](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm90.hpp#L210-L231)） | `num_cycles` 恒为 0（`TODO: calculate expected cycles`，未实现，[sm100.hpp:236-237](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm100.hpp#L236-L237)） |
| **`compare` 策略** | 纯 `num_cycles`（[sm90.hpp:241-243](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm90.hpp#L241-L243)） | 字典序多准则：单波优先 → 集群大优先 → 波少优先 → 尾波利用率高优先 → 块和小优先（[sm100.hpp:241-266](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm100.hpp#L241-L266)） |
| **额外硬约束** | swizzle 要求 ≥ 64B（[sm90.hpp:102](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm90.hpp#L102)） | 多两项：① tensor memory 容量 `2*umma_n + tmem_sf_cols ≤ 512`（[sm100.hpp:124-128](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm100.hpp#L124-L128)）；② A/B 有 K-major 时强制 128B swizzle（[sm100.hpp:133-137](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm100.hpp#L133-L137)） |

下面挑三处最关键的展开。

**(1) SM100 的 `swap_ab`：交换 A/B 以适配 UMMA**

SM90 的 `get_storage_config` 一开始就 `DG_HOST_ASSERT(layout.swap_ab == 0)`（[sm90.hpp:125](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm90.hpp#L125)），从不交换。SM100 则把 `swap_ab` 当作枚举维度（[sm100.hpp:47](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm100.hpp#L47)），且 m-grouped 场景强制 `swap_ab=true`（[sm100.hpp:32-43](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm100.hpp#L32-L43)）。根因是 SM100 的 UMMA 指令对 operand 布局有特定要求（见 u6-l2），某些场景把 A/B 交换后更易落到硬件友好的 layout A/D。

**(2) SM100 multicast 的不对称**

[sm100.hpp:76-88](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm100.hpp#L76-L88)：

```cpp
for (int cluster_m = 1; cluster_m <= 2; ++ cluster_m) {
    if (swap_ab == 1 and cluster_m > 1) continue;   // swap 后只能在 cluster_n
    for (int cluster_n = 1; cluster_n <= 2; ++ cluster_n) {
        if (cluster_m * cluster_n > 2) continue;
        if (swap_ab == 0 and cluster_n > 1) continue;  // 只支持 layout A/D
        ...
```

对比 SM90 的「M、N 两维对称」，SM100 的 multicast 被 `swap_ab` 钉死在一维：不交换时只能沿 M 维 multicast（`cluster_n` 恒为 1），交换时只能沿 N 维。这是 SM100 UMMA 的 layout A/D 约束的直接体现。

**(3) SM100 的字典序多准则比较**

因 `num_cycles` 未实现，SM100 不能像 SM90 那样只比周期，转而用一组工程经验准则做字典序比较（[sm100.hpp:241-266](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm100.hpp#L241-L266)）：

```cpp
static bool compare(const LayoutInfo& a, const LayoutInfo& b) {
    // 1) 单波永远更好
    if ((a.num_waves == 1 or b.num_waves == 1) and a.num_waves != b.num_waves)
        return a.num_waves < b.num_waves;
    // 2) multicast（集群大）更好
    if (a.layout.get_cluster_size() != b.layout.get_cluster_size())
        return a.layout.get_cluster_size() > b.layout.get_cluster_size();
    // 3) 波数少更好
    if (a.num_waves != b.num_waves) return a.num_waves < b.num_waves;
    // 4) 尾波利用率高更好
    if (a.last_wave_util != b.last_wave_util) return a.last_wave_util > b.last_wave_util;
    // 5) block_m + block_n 小更好；再退到 block_m * block_n 小更好
    if (a.layout.block_m + a.layout.block_n != b.layout.block_m + b.layout.block_n)
        return a.layout.block_m + a.layout.block_n < b.layout.block_m + b.layout.block_n;
    return a.layout.block_m * a.layout.block_n < b.layout.block_m * b.layout.block_n;
}
```

直觉解读：

- **单波优先**：能一波跑完就别分多波（避免尾波浪费），这是最高优先级。
- **集群大优先**：同等条件下 multicast 能省 L2 带宽，故集群 2 优于集群 1。
- **波少 / 尾波利用率高**：把 SM90 里由 `wave_efficiency` 吸收的逻辑显式拆成两条准则。
- **块和小优先**：前几条打平时，更小的分块寄存器压力更小、流水线更灵活，作为兜底偏好。

这套字典序是 SM100 在「缺周期模型」下的务实替代：用几条单调的经验规则逼近最优，而非精确建模。

#### 4.3.4 代码实践

**实践目标**：对比同一组形状在 SM90 与 SM100（若有两种硬件）下选出的配置，体会 `swap_ab` 与比较策略差异。

**操作步骤**：

1. 在 SM90 机器上 `DG_PRINT_CONFIGS=1` 跑 `m=4096,n=4096,k=4096` 的 BF16 GEMM，记录选中 `Layout`（`swap_ab` 必为 0）。
2. 在 SM100 机器上跑同一形状，记录选中 `Layout`：观察 `swap_ab` 是否为 1、`cluster` 是否只沿一维、`block_m` 是否为单一值（128）。
3. 选一个 `num_waves==1` 的小形状（如 `m=128,n=128`）在 SM100 上跑，对比两个候选（集群 1 vs 集群 2）：按 [sm100.hpp:243-244](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm100.hpp#L243-L244)「单波优先」第一准则，集群 2 会被淘汰吗？

**需要观察的现象与预期结果**：

- SM90 行的 `swap_ab=0`、`cluster` 可能出现 `(2,1)` 或 `(1,2)`。
- SM100 行 `swap_ab` 可能为 1，`cluster` 只在某一维为 2。
- 第 3 步：单波时第一准则只比 `num_waves`（两者都是 1，不触发），接着第二准则「集群大优先」会让集群 2 胜出——**与 SM90 单波惩罚淘汰 multicast 的行为相反**。这是两代比较策略最直观的差异（**待本地验证**具体输出）。

> 无双硬件时可做「源码阅读型实践」：对比 [sm90.hpp:241-243](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm90.hpp#L241-L243) 与 [sm100.hpp:241-266](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm100.hpp#L241-L266) 两段 `compare`，说明为什么「单波 + 集群2」在 SM90 被淘汰、在 SM100 被选中。

#### 4.3.5 小练习与答案

**练习 1**：SM100 的 `get_layout_info` 把 `num_cycles` 设为 0（[sm100.hpp:237](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm100.hpp#L237)）。这是否会让所有候选的 `num_cycles` 相同、从而使比较退化为只看后续准则？

**答**：是的。SM100 上所有候选 `num_cycles` 都是 0，`compare` 的字典序实际从「单波优先」开始生效，`num_cycles` 形同虚设（占位待实现）。这正是 SM100 必须用多准则、而 SM90 可以只比周期的根因。

**练习 2**：为何 m-grouped GEMM 在 SM100 上强制 `swap_ab=true` 且只给单一候选（[sm100.hpp:32-43](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm100.hpp#L32-L43)）？

**答**：m-grouped（MoE）把多个 expert 的 token 拼在 M 轴，`block_m` 被锁定为分组对齐量。这类场景对布局要求很刚性（`block_n=128`、`block_m=alignment`），可调空间小，故不枚举多候选、直接给一个满足 UMMA layout A/D 的 `swap_ab=true` 配置。

**练习 3**：SM100 比较「集群大优先」([sm100.hpp:247-248](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm100.hpp#L247-L248)) 与 SM90「单波 multicast 惩罚」([sm90.hpp:234-235](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm90.hpp#L234-L235)) 看似矛盾，如何理解？

**答**：两者针对的场景不同。SM90 的惩罚只在「`num_waves<=1`（单波）」时触发——单波下 multicast 砍并行度无利可图；多波时 SM90 的代价模型会通过 L2 带宽节省体现 multicast 收益。SM100 的「集群大优先」是字典序里的次级准则（在单波判定之后），且因无周期模型而作为 multicast 收益的代理。本质上两代都认可「多波时 multicast 通常更优」，只是 SM90 用精确模型、SM100 用经验准则。

---

## 5. 综合实践

把本讲三块知识串起来，完成一次「人工启发式」推演。

**任务**：给定 SM90、Normal GEMM、FP8 输入、FP32 输出、`m=16, n=512, k=4096, num_sms=132, block_n_multiple_of=1`，请你：

1. **枚举候选**：写出 `block_m_candidates`、`block_n_candidates`、`block_k`，并标出 `cluster` 的可能取值。
2. **过滤**：指出哪些 `(block_m, block_n)` 组合会被「寄存器压力」「swizzle 太小」「流水线太浅」淘汰（可估算，不必精确到每个组合）。
3. **评估**：对存活的候选里 `block_m=16, block_n=24, cluster=(1,1)` 与 `block_m=128, block_n=128, cluster=(1,1)` 两个，手算 `num_blocks / num_waves / last_wave_util / wave_efficiency`，判断哪个 `num_cycles` 更可能胜出。
4. **验证**：在 `DG_PRINT_CONFIGS=1` 下运行该形状，核对启发式实际选中的 `Layout` 与 `LayoutInfo` 是否与你的推演一致（**待本地验证**）。

**参考推演要点**：

- `block_m_candidates = {64,128,16}`（默认 64/128，`m≤16` 追加 16；FP32 输出不追加 256）；`block_n_candidates = {16,24,40,…,160}`（1D1D+FP32 起点 24 + 16）；`block_k=128`。
- `cluster` 取值：`{(1,1),(2,1),(1,2)}`（集群 ≤2 且 132 能整除）。
- 过滤：`block_m>128 且 block_n>128` 本例不触发（无 128×128 以上）；但小 `block_n`（如 16）的 swizzle 可能落到 32B 被淘汰；`block_m=16,block_n=24` 的乘积 384 远小于 `128*192`，要求 `num_stages≥4`，若共享内存装不下 4 级则被淘汰。
- 评估：`block_m=16,block_n=24` → `num_blocks=ceil(16/16)*ceil(512/24)=1*22=22`，`num_waves=1`，`last_wave_util=22`（满地浪费）；`block_m=128,block_n=128` → `num_blocks=1*4=4`，`num_waves=1`，`last_wave_util=4`。两者都单波，`wave_efficiency` 都低，最终哪个 `num_cycles` 小取决于单 block 搬运量与 block 数的权衡——`block_m=16` 方案 block 多（22）但单 block 搬运量小，`block_m=128` 方案 block 少（4）但单 block 搬运量大。这是代价模型存在的意义：它把这组权衡量化成一个数。真实胜出者**待本地验证**。

> 这个练习的核心不是算出「正确答案」，而是体会：**枚举给可能性，过滤去非法，代价模型在合法候选里做权衡**——这正是 `get_best_config` 三段式的全部价值。

## 6. 本讲小结

- `get_best_config<ArchSpec>(desc)` 是「枚举候选 → 逐个评估 → 选最优 → 推断 storage/pipeline/launch」的三段式模板函数，骨架在 [common.hpp:14-52](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/common.hpp#L14-L52)。
- **候选枚举**走「先宽生成、再用约束过滤」：`block_m` 形状驱动（小 M 追加小块、BF16 追加 256）、`block_n` 步长枚举（1D1D+FP32 从 24 起避 bank conflict）、`block_k` 固定 `128/element_size`，组合再过寄存器/swizzle/流水线/multicast/1D2D 展开五道关。
- **布局评估**产出 `LayoutInfo{num_waves, last_wave_util, num_cycles, layout}`；`num_waves` 与 `wave_efficiency` 量化尾波浪费，`compare(a,b)` 返回「a 严格优于 b」。
- **SM90 用完整 L1/L2 周期模型**，比较只看 `num_cycles`，并对「单波 + multicast」施以无穷周期惩罚。
- **SM100 与 SM90 的真实差异**在 `swap_ab`（SM100 枚举、SM90 断言 0）、multicast 维度（SM90 对称两维、SM100 受 swap_ab 钉死一维）、代价模型（SM100 `num_cycles=0` 未实现）、比较策略（SM100 字典序多准则）以及额外的 tensor memory 容量与 128B swizzle 约束；`block_k` 公式两代相同，是共同不变量。
- 选出的 `Layout` 直接决定设备 kernel 的 `BLOCK_M/N/K` 模板参数（u3-l2）与 `cluster_dim` 启动属性（u4-l3），是宿主侧「怎么算」的最终落点。

## 7. 下一步学习建议

- **向调优旋钮深入（u5-l3）**：本讲的候选步长受 `block_n_multiple_of`、分组对齐受 `mk_alignment_for_contiguous_layout`、形状特化受 `compiled_dims` 影响——下一讲 u5-l3「compiled_dims 与运行时调优旋钮」系统讲解这些旋钮如何改写候选清单与编译产物。
- **向设备内核深入（u6）**：本讲选出的 `Layout` 会变成 kernel 模板参数。u6-l1 以 `sm90_fp8_gemm_1d1d` 为例讲一个完整 kernel 如何消费 `BLOCK_M/N/K`；u6-l2 对比 SM90 WGMMA 与 SM100 UMMA，解释本讲里反复出现的「layout A/D」「K-major 约束」「swap_ab」在硬件层面的根因；u6-l4 讲 `Scheduler` 如何把本讲算出的 `num_blocks` 真正分配到各 SM。
- **源码延伸阅读**：对照 [sm100.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm100.hpp) 的 `get_storage_config`/`get_pipeline_config`（SM100 版本引入了 `tmem` 指针与 32 级 stage 上限），体会两代架构在共享内存与流水线建模上的差异。
