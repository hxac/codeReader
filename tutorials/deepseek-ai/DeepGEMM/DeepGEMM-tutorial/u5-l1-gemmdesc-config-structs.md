# GemmDesc 与配置数据结构

## 1. 本讲目标

在 u3-l2 里，我们看到宿主 `generate_impl` 用 `fmt::format` 把 17 个编译期常量填进一段极薄的 `.cu` 源码。这 17 个常量从何而来？答案是：它们全部来自本讲要讲的两个核心数据结构——`GemmDesc`（描述「要算什么」）和 `GemmConfig`（描述「怎么算」）。

本讲是「启发式与配置选择」单元的第一篇，只做一件事：把这两个数据结构及其依赖的三类枚举彻底讲透。学完后你应当能够：

- 看懂 `GemmDesc` 的每一个字段，并能判断它合法与否。
- 说出 `GemmConfig` 由 `Layout` / `StorageConfig` / `PipelineConfig` / `LaunchConfig` 四个子结构各管哪一摊配置维度。
- 解释 `MmaKind`、`GemmType`、`KernelType` 三类枚举的含义，以及它们如何驱动布局与内核选择。
- 给定一个具体的 FP8 GEMM 形状与累加需求，手动「填表」构造出对应的 `GemmDesc`。

本讲**不**讲解「如何在众多候选里挑出最优布局」——那是下一讲 u5-l2 的内容。本讲只讲数据结构本身，即把 `GemmDesc` 喂给 `get_best_config` 之后产出的 `GemmConfig` 长什么样。

## 2. 前置知识

阅读本讲前，请确认你已经掌握以下概念（它们在前置讲义中建立）：

- **问题描述 vs 解决方案描述**。一次 GEMM 调用要回答两个问题：「算什么」（形状、精度、布局、要不要累加）与「怎么算」（用多大的分块、几个集群、几级流水线）。DeepGEMM 用 `GemmDesc` 回答前者，用 `GemmConfig` 回答后者。
- **宿主与设备之分**（u3-l1）。本讲全部是**宿主侧 C++ 结构体**（POD），它们在 CPU 上被构造、传递、打印，最终在 `generate_impl` 里被固化成设备 kernel 的编译期常量。
- **compiled_dims 特化**（u3-l2）。`GemmDesc` 里的 `compiled_dims` 字段（默认 `"nk"`）决定了 M/N/K 哪些维度被特化为编译期常量。
- **DeviceRuntime 旋钮**（u4-l1）。`GemmDesc` 里的 `num_sms`、`tc_util` 来自进程级单例 `device_runtime`，是「用户需求」类字段。

你还需要一组术语：

- **POD（Plain Old Data）**：只有基本类型成员、没有复杂行为的「纯数据」结构体。DeepGEMM 的配置结构体都是 POD，外加一个 `operator<<` 友元函数用于打印。
- **启发式（heuristic）**：不枚举全部可能、而是用经验规则快速选出一个「足够好」配置的策略。`GemmDesc` 是启发式的**输入**，`GemmConfig` 是它的**输出**。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [deep_gemm/include/deep_gemm/common/types.cuh](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/common/types.cuh) | 定义三类枚举 `MmaKind` / `GemmType` / `KernelType`，以及元素尺寸与判定函数。这是设备/宿主共用的公共头。 |
| [csrc/jit_kernels/heuristics/config.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/config.hpp) | 本讲主角。定义 `GemmDesc`、`Layout`、`StorageConfig`、`PipelineConfig`、`LaunchConfig`、`GemmConfig`、`LayoutInfo` 全部数据结构。 |
| [csrc/jit_kernels/heuristics/common.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/common.hpp) | 模板函数 `get_best_config(desc)`：从 `GemmDesc` 推断出 `GemmConfig` 的组装总入口。 |
| [csrc/jit_kernels/heuristics/sm90.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm90.hpp) | `SM90ArchSpec`：Hopper 架构上如何由 `GemmDesc` 生成各 config。 |
| [csrc/jit_kernels/heuristics/sm100.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm100.hpp) | `SM100ArchSpec`：Blackwell 架构上的对应实现，用于对比。 |
| [csrc/jit_kernels/impls/sm90_fp8_gemm_1d1d.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/sm90_fp8_gemm_1d1d.hpp) | 一个真实的 `GemmDesc` 构造现场，演示字段如何从 API 参数填入。 |
| [csrc/jit_kernels/impls/runtime_utils.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/runtime_utils.hpp) | `get_compiled_dim` 与枚举的 `to_string`，连接 `GemmDesc` 与代码生成。 |
| [csrc/utils/math.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/utils/math.hpp) | `ceil_div` / `align` / `kPackedFP4` 等基础工具。 |

## 4. 核心概念与源码讲解

### 4.1 GemmDesc：描述「要算什么」

#### 4.1.1 概念说明

`GemmDesc` 是一次 GEMM 问题的「身份证」。它把 Python 用户传进来的零散参数（张量、形状、布局、累加标志）打包成一个结构体，作为启发式系统 `get_best_config` 的唯一输入。

理解 `GemmDesc` 的关键是抓住「它描述问题，不描述解法」：

- `GemmDesc` **不包含** `block_m`、`num_stages` 这些「怎么算」的答案——那是 `GemmConfig` 的事。
- `GemmDesc` **只包含**「算什么」的事实：形状、精度、布局、要不要累加、用户对资源使用的需求。

打个比方：`GemmDesc` 像一道考试题（「算一个 1024×4096×4096 的 FP8 矩阵乘，结果要累加到已有矩阵上」），`GemmConfig` 像解题方案（「用 128×128 的分块、8 级流水线、2-CTA 集群」）。本讲先把题目（`GemmDesc`）讲清楚。

#### 4.1.2 核心流程

`GemmDesc` 的生命周期贯穿一次 GEMM 调用的始终：

1. **构造**：API 层（如 `sm90_fp8_gemm_1d1d`）从 Python 传入的张量与参数构造 `GemmDesc`。
2. **校验**：`get_best_config` 第一步就调 `desc.check_validity()`，非法组合直接抛异常。
3. **喂入启发式**：`desc` 的字段（形状、dtype、`num_sms`、`compiled_dims`、`expected_*`）驱动候选布局枚举与评估。
4. **随 Args 传递**：`desc` 被原样塞进 `Runtime::Args`，最终在 `generate_impl` 里把 `desc.m/n/k` 等填进设备模板参数（u3-l2 讲过的 17 个编译期常量里就有 `m/n/k`、`num_groups`、`gemm_type`、`cd_dtype`）。

```
Python 参数
   │  构造
   ▼
GemmDesc ──► check_validity()
   │
   ▼
get_best_config(desc) ──► GemmConfig   （下一讲 u5-l2 详讲）
   │
   ▼
Runtime::Args { desc, config, ... }
   │  generate_impl
   ▼
.cu 源码（desc.m/n/k、config.layout.block_m ... 被填入模板）
```

#### 4.1.3 源码精读

`GemmDesc` 完整定义在 [csrc/jit_kernels/heuristics/config.hpp:12-73](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/config.hpp#L12-L73)，这段代码定义了问题的全部字段。按职责可以把字段分成七组：

**① 算法身份**

```cpp
GemmType gemm_type;        // 普通 / M轴分组 / K轴分组 / Batched / psum 变体
KernelType kernel_type;    // 1D1D / 1D2D / NoSF，见 4.3
```

`gemm_type` 区分这是稠密 GEMM、MoE 分组 GEMM 还是 K 轴分组（权重梯度）；`kernel_type` 区分内核内部的存储与线程结构。

**② 形状**

```cpp
int m, n, k, num_groups;
```

`m/n/k` 是经典 GEMM 维度；`num_groups` 是分组 GEMM 的段数（普通 GEMM 为 1）。

**③ 数据类型与布局**

```cpp
at::ScalarType a_dtype, b_dtype, cd_dtype;
cute::UMMA::Major major_a, major_b;
```

`a_dtype/b_dtype` 是输入精度（`Float8_e4m3fn` / `kPackedFP4` / `BFloat16`），`cd_dtype` 是输出精度（`Float` 或 `BFloat16`）。`major_a/major_b` 是主维（`K` 或 `MN`），承接 u2-l1 的 NT 布局约定。

**④ 累加**

```cpp
bool with_accumulation;    // D = C + A@B 还是 D = A@B
```

承接 u2-l3 的 early_return：当 `C≠D` 时 API 层会先把 C 拷到 D，让设备 kernel 始终假设 C/D 同址；这里 `with_accumulation = c.has_value()`。

**⑤ 用户需求（运行时旋钮）**

```cpp
int num_sms;
int tc_util;
std::string compiled_dims;
```

`num_sms`（SM 上限）与 `tc_util`（张量核利用率，0~100）来自 `device_runtime`（u4-l1）；`compiled_dims`（默认 `"nk"`）控制哪些维度做编译期特化（u3-l2）。

**⑥ SM100 psum 布局 padding 合约**

```cpp
bool ensure_zero_padding = true;
```

仅 SM100 的 K 轴分组 psum 布局使用，决定 padding 段是否强制清零。

**⑦ 启发式用形状提示**

```cpp
int expected_m = 0, expected_n = 0, expected_k = 0, expected_num_groups = 0;
int get_expected_m() const { return expected_m > 0 ? expected_m : m; }
// get_expected_n / get_expected_k / get_expected_num_groups 同理
```

`expected_*` 遵循全库「**0 表示默认**」约定：为 0 时取真实值，非 0 时覆盖。它的用途见 K 轴分组的真实构造例子：[csrc/jit_kernels/impls/sm90_fp8_gemm_1d1d.hpp:170-181](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/sm90_fp8_gemm_1d1d.hpp#L170-L181) 把 `k` 设为 `sum_k`（用于 TMA 描述符），却把 `expected_k` 设为 `max_k`（用于启发式选更优的布局）——同一次调用里，「搬运用的总 K」与「选配置用的典型 K」可以不同。

下面看一个普通 FP8 GEMM 的真实构造，字段一目了然：[csrc/jit_kernels/impls/sm90_fp8_gemm_1d1d.hpp:88-98](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/sm90_fp8_gemm_1d1d.hpp#L88-L98) 用 C++20 指定初始化（`.field = value`）逐字段填入，`num_sms`/`tc_util` 直接取自 `device_runtime`，`with_accumulation` 由 `c.has_value()` 推出。

最后是合法性校验 `check_validity`：[csrc/jit_kernels/heuristics/config.hpp:39-48](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/config.hpp#L39-L48)。它有三条硬约束：

1. **dtype 组合必须自洽**：BF16 路径要求 A/B 都是 BF16；MXFP8FP4 路径要求 A/B 都是 `Float8_e4m3fn` 或 `kPackedFP4`。
2. **输出只能是 BF16 或 Float**。
3. **`num_sms` 必须是偶数**（因为集群与 wave 调度依赖偶数 SM）。

不满足时 `DG_HOST_ASSERT` 抛 `DGException`，由 pybind11 翻译成 Python 异常（承接 u2-l3）。注意 `check_validity` 还会先调 `get_mma_kind()` 决定走哪条 dtype 分支——这个方法见 4.3。

#### 4.1.4 代码实践

**实践目标**：用「源码阅读 + 打印观察」掌握 `GemmDesc` 的字段构成与 `check_validity` 的约束。

**操作步骤**：

1. 打开 [csrc/jit_kernels/heuristics/config.hpp:50-72](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/config.hpp#L50-L72) 的 `operator<<`，对照它列出打印顺序——这就是你接下来要在运行时观察到的字符串。
2. 设置环境变量 `DG_PRINT_CONFIGS=1`（或 `DG_JIT_DEBUG=1`），运行任意一次 FP8 GEMM（参考 u1-l4 的最小调用）。
3. 观察控制台输出的形如 `GemmDesc(...): GemmConfig(...), LayoutInfo(...)` 的一行。这行由 [csrc/jit_kernels/heuristics/common.hpp:40-49](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/common.hpp#L40-L49) 在首次遇到某 `desc` 时打印一次（用 `static unordered_set` 去重）。
4. 逐字段把这行拆开，与 4.1.3 的七组字段一一对应。

**需要观察的现象**：同一形状多次调用只会打印**一次**（去重生效）；改变 M/N/K/dtype 后会出现**新的一行**。

**预期结果**：你能指着打印串里的 `mma_kind=`、`with_accumulation=`、`compiled_dims=` 等说出它们的来源。`c10::toString` 给出的 dtype 字符串精确拼写以本地输出为准（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `num_sms` 必须是偶数？请从「集群」与「wave」的角度解释。

> **参考答案**：`Layout` 里有 `cluster_m * cluster_n` 的集群，SM100 的 m-grouped 路径常启用 2-CTA 集群；wave 调度也以 `num_blocks / num_sms` 计算。集群大小（常为 2）要能整除 SM 数，且 `get_layout_candidates` 里有 `desc.num_sms % (cluster_m * cluster_n) != 0` 的过滤（见 [sm90.hpp:78](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm90.hpp#L78)），偶数 SM 才能让 2-CTA 集群均分。

**练习 2**：K 轴分组 GEMM 里，为什么 `desc.k = sum_k` 而 `desc.expected_k = max_k`？

> **参考答案**：`k` 用于在设备侧描述「实际要搬运的总 K 长度」（TMA 描述符、循环边界），必须是各段之和 `sum_k`；`expected_k` 只用于启发式评估「典型一段的规模」以选最优布局，用 `max_k` 更保守、更能反映最坏一段的计算量。两者职责不同，故可分离。

### 4.2 GemmConfig 组合结构：描述「怎么算」

#### 4.2.1 概念说明

`GemmConfig` 是启发式的输出，是「解题方案」。它**不是**一个大而全的结构，而是由四个小 POD 组合而成，各管一摊：

| 子结构 | 管什么 | 典型字段 |
| --- | --- | --- |
| `Layout` | 分块大小与集群拓扑 | `block_m/n/k`、`cluster_m/n`、`swap_ab` |
| `StorageConfig` | 共享内存瓦片尺寸与 swizzle | `load_block_m/n`、`store_block_m/n`、`swizzle_*_mode` |
| `PipelineConfig` | 流水线深度与共享内存总量 | `smem_size`、`num_stages` |
| `LaunchConfig` | 线程划分与 SM 分配 | `num_sms`、`num_tma_threads`、`num_math_threads` |

这是一种典型的「**组合优于继承**」设计：每个子结构都是纯数据，没有虚函数、没有状态机；`GemmConfig` 只是把四个子结构摆在一起。好处是每一步推断（先选 layout，再推 storage，再推 pipeline，最后推 launch）的输入输出清晰可测，也方便在 `operator<<` 里整体打印。

承接 u3-l2：这四个子结构里的字段（如 `block_m/n/k`、`swizzle_*`、`num_stages`、`num_sms`）正是被 `generate_impl` 填进设备模板的那 17 个编译期常量的来源。

#### 4.2.2 核心流程

`GemmConfig` 由模板函数 `get_best_config` 分四步推断组装，整体在 [csrc/jit_kernels/heuristics/common.hpp:14-52](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/common.hpp#L14-L52)：

```
get_best_config<ArchSpec>(desc)
   │
   ├─ 1. desc.check_validity()
   ├─ 2. ArchSpec::get_layout_candidates(desc)     → 候选 Layout 列表
   ├─ 3. ArchSpec::get_layout_info(desc, layout)   → 逐个评估
   │      + ArchSpec::compare(...)                  → 选最优 layout      （u5-l2 详讲）
   ├─ 4. ArchSpec::get_storage_config(desc, layout) → StorageConfig
   ├─ 5. ArchSpec::get_pipeline_config(desc, layout, storage_config) → PipelineConfig
   ├─ 6. ArchSpec::get_launch_config(desc, layout) → LaunchConfig
   │
   └─ 组装 GemmConfig { layout, storage_config, pipeline_config, launch_config }
```

注意依赖链是**单向**的：`storage_config` 依赖 `layout`，`pipeline_config` 依赖 `layout` 与 `storage_config`，`launch_config` 只依赖 `layout`。这意味着「选错 layout」会连累后面三步，所以第 2~3 步的 layout 选择是重中之重（u5-l2）。

#### 4.2.3 源码精读

**Layout**（分块与集群）：[csrc/jit_kernels/heuristics/config.hpp:76-91](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/config.hpp#L76-L91)。`block_m/n/k` 是分到共享内存/tensor core 的瓦片大小；`cluster_m/n` 是 thread block cluster 拓扑，`get_cluster_size()` 返回集群内 CTA 数（SM90 上通常 ≤2）。`swap_ab` 是 SM100 特有的 A/B 交换开关（承接 u2-l1 的转置派生思路，但这里是布局级交换而非数据转置）。

**StorageConfig**（存储瓦片与 swizzle）：[csrc/jit_kernels/heuristics/config.hpp:93-108](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/config.hpp#L93-L108)。`load_block_m/n` 是 TMA 从全局内存搬进共享内存的瓦片，`store_block_m/n` 是 epilogue 写回的瓦片；`swizzle_a/b/cd_mode` 是共享内存地址重排原子（32B/64B/128B），承接 u4-l2 的 swizzle 概念——它决定 bank conflict 行为。

**PipelineConfig**（流水线）：[csrc/jit_kernels/heuristics/config.hpp:110-120](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/config.hpp#L110-L120)。只有两个字段：`num_stages`（k-loop 软件流水线级数）与 `smem_size`（共享内存总占用字节）。

**LaunchConfig**（启动划分）：[csrc/jit_kernels/heuristics/config.hpp:122-141](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/config.hpp#L122-L141)。`num_sms` 是实际用的 SM 数，`num_sms_per_cluster` 对应集群大小，`num_tma_threads`/`num_math_threads` 是「搬运线程」与「计算线程」的分工（承接 u6-l1 的 TMA/math 线程划分），`num_non_epilogue_threads`/`num_epilogue_threads` 在 SM100 才有意义（SM90 上为 0）。

**GemmConfig**（组合体）：[csrc/jit_kernels/heuristics/config.hpp:143-157](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/config.hpp#L143-L157)，只是把上述四者包起来。

最后看四步推断如何读 `desc` 的字段。以 SM90 为例：

- `get_storage_config`（[sm90.hpp:120-146](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm90.hpp#L120-L146)）读 `desc.kernel_type`、`desc.major_a/b`、`desc.cd_dtype` 决定 store block 与 swizzle。
- `get_pipeline_config`（[sm90.hpp:148-187](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm90.hpp#L148-L187)）读 `desc.kernel_type`、`desc.gemm_type`、各 dtype 算出 `smem_per_stage`，再被 `smem_capacity`(232448) 限制出 `num_stages`。
- `get_launch_config`（[sm90.hpp:189-199](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm90.hpp#L189-L199)）读 `desc.num_sms`、`layout.block_m` 决定线程数。

可见 `desc` 的字段被「按需消费」到四个子结构里——这正是「问题描述」到「解决方案」的映射。

#### 4.2.4 代码实践

**实践目标**：跟踪 `get_best_config` 从 `desc` 到 `GemmConfig` 的四步推断，列出每步读了 `desc` 的哪些字段。

**操作步骤**：

1. 在 `DG_PRINT_CONFIGS=1` 下运行一次 FP8 GEMM，记录打印的 `GemmConfig(...)`。
2. 把它拆成 `Layout` / `StorageConfig` / `PipelineConfig` / `LaunchConfig` 四段，填进下表：

| 子结构 | 打印值 | 推断时读了 desc 的哪些字段 |
| --- | --- | --- |
| Layout | `block_m=.., block_n=.., block_k=.., cluster_m=.., cluster_n=..` | `m/n`、`get_mma_kind()`、`num_sms`、`gemm_type` |
| StorageConfig | `...` | `kernel_type`、`major_a/b`、`a/b/cd_dtype` |
| PipelineConfig | `smem_size=.., num_stages=..` | `kernel_type`、`gemm_type`、各 dtype |
| LaunchConfig | `num_sms=.., num_tma_threads=.., num_math_threads=..` | `num_sms`、`block_m` |

3. 对照本讲 4.2.3 的 SM90 实现核对每一格的来源是否吻合。

**需要观察的现象**：`block_k` 对于 FP8 总是 128（见 4.3），对 BF16 总是 64；`num_stages` 受 `smem_capacity` 上限钳制，不会超过 `kNumMaxStages`（SM90 为 16、SM100 为 32）。

**预期结果**：你能对打印串里任意一个数说出它来自 `desc` 的哪个字段、经过哪个 `get_*_config` 推出。`num_stages` 的精确值待本地验证（取决于形状）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `pipeline_config` 依赖 `storage_config`，而 `launch_config` 不依赖？

> **参考答案**：算 `num_stages` 需要 `smem_per_stage`，而每级共享内存占用（A/B/SF 瓦片）的尺寸由 `storage_config` 的 `load_block_*` 决定（见 [sm90.hpp:159-160](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm90.hpp#L159-L160)），所以 pipeline 必须在 storage 之后。`launch_config` 只需 `num_sms` 与 `block_m`（决定 math 线程数），都来自 `desc` 和 `layout`，不需要 storage 信息。

**练习 2**：SM90 与 SM100 的 `LaunchConfig` 在「线程划分」上有何不同？

> **参考答案**：SM90（[sm90.hpp:189-199](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm90.hpp#L189-L199)）只划分 `num_tma_threads`(128) 与 `num_math_threads`(128/256)，`num_epilogue_threads` 固定为 0；SM100（[sm100.hpp:219-226](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm100.hpp#L219-L226)）固定 256 线程，并进一步细分出 `num_non_epilogue_threads` 与 `num_epilogue_threads`（各 128），因为 Blackwell 的 epilogue 有独立的 warp 调度。

### 4.3 枚举类型：MmaKind / GemmType / KernelType

#### 4.3.1 概念说明

`GemmDesc` 和各 `get_*_config` 的逻辑被三类枚举驱动，它们定义在设备/宿主共用的 [deep_gemm/include/deep_gemm/common/types.cuh](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/common/types.cuh)。把这三类枚举记住，看 `get_layout_candidates` 的分支就不会迷路。

**MmaKind**（张量核乘加类型）：

```cpp
enum class MmaKind { BF16 = 0, MXFP8FP4 = 1 };
```

只有两档：要么走 BF16 的 MMA，要么走「MXFP8/FP4」一族（DeepGEMM 把 FP8 e4m3 与 FP4 统称 MXFP8FP4，因为两者共享 UE8M0 块缩放体系，承接 u2-l2）。它由输入 dtype 推出，是**纯函数**（不改设备状态）。

**GemmType**（GEMM 调度类型）：

```cpp
enum class GemmType {
    Normal = 0, MGroupedContiguous = 1, MGroupedMasked = 2,
    KGroupedContiguous = 3, Batched = 4,
    MGroupedContiguousWithPsumLayout = 5, KGroupedContiguousWithPsumLayout = 6,
};
```

它描述「M/N/K 轴怎么分组、用什么内存布局」。`Normal` 是稠密 GEMM；`MGrouped*` 是 MoE 的 M 轴分组（contiguous/masked，承接 u7）；`KGrouped*` 是权重梯度的 K 轴分组（承接 u7）；`Batched` 是批处理。两个 `*WithPsumLayout` 是 SM100 专属的「部分和布局」变体，承接 u7-l3。

**KernelType**（内核内部结构）：

```cpp
enum class KernelType { Kernel1D1D = 0, Kernel1D2D = 1, KernelNoSF = 2 };
```

它描述设备 kernel 的存储与线程拓扑：`1D1D` 指 A/B 都按 1D（沿 K）分块（SM90/SM100 主力 kernel）；`1D2D` 指 B 做 2D 分块（用于某些 SM90 FP32 输出场景）；`NoSF` 指「无缩放因子」（BF16 无需 SF）。它直接决定 SF 是否分配共享内存、`store_block_m` 取 `wgmma_m` 还是 `block_m`。

#### 4.3.2 核心流程

三类枚举各自驱动一段推断：

- **MmaKind** 由 `GemmDesc::get_mma_kind()` 从 `a_dtype` 推出，进而决定 `block_k`（`128 / element_size`）与共享内存里 SF 是否存在。
- **GemmType** 由 API 层根据调用的算子设定（`fp8_gemm_nt` → `Normal`，`m_grouped_*` → 对应 MGrouped），驱动 `get_layout_candidates` 的 `block_m` 候选（见 [sm90.hpp:19-36](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm90.hpp#L19-L36)）、multicast 启停（[sm90.hpp:63-67](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm90.hpp#L63-L67)）与调度器分支（u6-l4/u7）。
- **KernelType** 由 API 层设定，决定 `get_storage_config` 里 SF 的存储（[sm90.hpp:163-166](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm90.hpp#L163-L166)）与 `store_block_m`（[sm90.hpp:129](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm90.hpp#L129)）。

在代码生成阶段，`GemmType` 与 `cd_dtype` 还要经 `to_string` 转成 C++ 记号塞进 `.cu`（u3-l2 的 17 个模板参数），见 [csrc/jit_kernels/impls/runtime_utils.hpp:41-52](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/runtime_utils.hpp#L41-L52)。

#### 4.3.3 源码精读

**MmaKind 与元素尺寸**：[deep_gemm/include/deep_gemm/common/types.cuh:7-18](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/common/types.cuh#L7-L18)。`get_element_size` 返回 BF16=2、MXFP8FP4=1 字节。这个尺寸直接决定 `block_k`：

\[ \texttt{block\_k} = \frac{128}{\texttt{get\_element\_size(mma\_kind)}} \]

所以 BF16 时 `block_k = 64`，FP8/FP4 时 `block_k = 128`。这与 tensor core 的 MMA 粒度（K=32）对齐，也解释了 u2-l2 里 `gran_k` 常取 128 的来由。`block_k` 的计算现场在 [sm90.hpp:60](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm90.hpp#L60) 与 [sm100.hpp:29](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm100.hpp#L29)。

**GemmType 与判定函数**：[deep_gemm/include/deep_gemm/common/types.cuh:20-44](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/common/types.cuh#L20-L44)。`is_m_grouped_contiguous` / `is_k_grouped_contiguous` 把「基础型 + psum 变体」归并，避免调用处写一长串 `||`。

**KernelType**：[deep_gemm/include/deep_gemm/common/types.cuh:46-50](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/common/types.cuh#L46-L50)。

最后补一个易踩的坑：FP4 的 dtype。`kPackedFP4` 并非 PyTorch 原生类型，而是借用了 `torch::kInt8` 的别名——见 [csrc/utils/math.hpp:11](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/utils/math.hpp#L11)（`constexpr auto kPackedFP4 = torch::kInt8;`）。所以 `check_validity` 里判断 `a_dtype == kPackedFP4` 实际是在判断 `kInt8`，这是 DeepGEMM 复用现有标量类型的权宜之计。同文件还有 `ceil_div` 与 `align` 两个全库基础工具：[csrc/utils/math.hpp:14-21](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/utils/math.hpp#L14-L21)，`align(a,b) = ceil_div(a,b) * b`，你在 `get_layout_candidates` 与 `get_pipeline_config` 里会反复看到它们。

#### 4.3.4 代码实践

**实践目标**：手算 `MmaKind` 如何驱动 `block_k`，并验证 dtype → mma_kind 的映射。

**操作步骤**：

1. 对下面两种输入，套用 [config.hpp:35-37](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/config.hpp#L35-L37) 的 `get_mma_kind()` 与公式 `block_k = 128 / element_size`，填表：

| 输入 dtype | mma_kind | element_size | block_k |
| --- | --- | --- | --- |
| `Float8_e4m3fn` | MXFP8FP4 | 1 | 128 |
| `BFloat16` | BF16 | 2 | 64 |
| `kPackedFP4` (即 kInt8) | MXFP8FP4 | 1 | 128 |

2. 打开 [sm90.hpp:59-60](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm90.hpp#L59-L60) 与 [sm100.hpp:28-29](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm100.hpp#L28-L29)，确认两代架构的 `block_k` 推导式完全一致。

**需要观察的现象**：FP8 与 FP4 的 `block_k` 相同（都 128），因为它们在 MMA 层共享 MXFP8FP4 路径；BF16 的 `block_k` 减半。

**预期结果**：表内三行数值如上。这解释了为什么 FP8/FP4 kernel 的 `BLOCK_K` 断言常是 128（u6-l1 会看到 `DG_STATIC_ASSERT(BLOCK_K==128)`）。

#### 4.3.5 小练习与答案

**练习 1**：`KernelType::KernelNoSF` 时，`get_pipeline_config` 会发生什么？

> **参考答案**：`NoSF` 表示无缩放因子（BF16），故 `smem_sfa_per_stage = 0`（见 [sm90.hpp:163-164](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm90.hpp#L163-L164) 的 `kernel_type == KernelNoSF` 分支）；同时因不是 1D1D，`smem_sfb_per_stage = 0`。每级共享内存占用变小，在同样 `smem_capacity` 下能容纳更多 `num_stages`。

**练习 2**：为什么 `to_string(GemmType)` 要把枚举转成 `"GemmType::Normal"` 这样的字符串？

> **参考答案**：因为 `generate_impl` 要把 `gemm_type` 作为**编译期常量**塞进 `.cu` 源码（u3-l2），而源码里它必须是一个合法的 C++ 标识符表达式。`to_string` 产出 `"GemmType::Normal"` 这样的记号，经 `fmt::format` 填入模板实参后，恰好是设备侧可见的枚举值（设备头也 `#include` 了同一个 `types.cuh`）。

## 5. 综合实践

把本讲三个模块串起来：给定一个真实的 SM90 FP8 GEMM 调用，手动完成「问题描述」的全流程。

**场景**：在 H100（132 个 SM）上计算 \(D = C + A \times B\)，其中 \(A\) 是 \(1024 \times 4096\) 的 FP8（`Float8_e4m3fn`）矩阵（K-major），\(B\) 是 \(4096 \times 4096\) 的 FP8 矩阵（K-major），输出 \(D\) 为 FP32，需要累加到已有的 \(C\)。采用默认 `compiled_dims = "nk"`，`tc_util` 取默认（0→100）。

**任务**：

1. 按 4.1.3 的七组字段，写出完整的 `GemmDesc` 字段值（用指定初始化的伪代码）。
2. 逐一过 `check_validity` 的三条约束，说明本场景为何全部通过。
3. 推断 `get_mma_kind()`、`block_k` 的值。
4. 描述 `get_best_config` 接下来会做的四步推断（不必算出具体最优值，那是 u5-l2 的事），指出每一步读了 `desc` 的哪些字段。

**参考要点**：

1. 字段值：

   ```cpp
   GemmDesc {
     .gemm_type = GemmType::Normal,          // 稠密 GEMM
     .kernel_type = KernelType::Kernel1D1D,   // SM90 FP8 主力 kernel
     .m = 1024, .n = 4096, .k = 4096, .num_groups = 1,
     .a_dtype = torch::kFloat8_e4m3fn, .b_dtype = torch::kFloat8_e4m3fn,
     .cd_dtype = torch::kFloat,               // FP32 输出
     .major_a = cute::UMMA::Major::K, .major_b = cute::UMMA::Major::K,  // NT 布局
     .with_accumulation = true,               // C 已提供
     .num_sms = 132,                          // 来自 device_runtime
     .tc_util = 100,                          // 0 规范化为 100
     .compiled_dims = "nk"                    // 默认
     // expected_* 与 ensure_zero_padding 取默认 0 / true
   };
   ```

2. `check_validity`：① A/B 都是 `Float8_e4m3fn`，走 MXFP8FP4 分支，自洽；② `cd_dtype=Float` 合法；③ `num_sms=132` 为偶数。三条全过。
3. `get_mma_kind()` → `MXFP8FP4`（`a_dtype` 非 BF16）；`element_size=1`；`block_k = 128/1 = 128`。
4. 四步：① `get_layout_candidates` 读 `m/n`、`get_mma_kind()`（→block_k）、`num_sms`、`gemm_type`、`kernel_type`、`cd_dtype` 枚举候选；② `get_layout_info` 读 `expected_m/n/num_groups`、`num_sms`、`with_accumulation` 评估；③ `get_storage_config` 读 `kernel_type`、`major_a/b`、各 dtype；④ `get_pipeline_config` 读 `kernel_type`、`gemm_type`、各 dtype；⑤ `get_launch_config` 读 `num_sms`、`block_m`。

> 说明：上述 `tc_util=100` 与 `num_sms=132` 是基于 H100 默认的推断，实际值以 `device_runtime->get_*()` 为准（待本地验证）。

## 6. 本讲小结

- `GemmDesc` 是「问题描述」：字段分七组（算法身份、形状、dtype/布局、累加、用户旋钮、psum 合约、expected 提示），`check_validity` 用三条约束保证 dtype 自洽、输出合法、SM 数为偶数。
- `GemmConfig` 是「解决方案描述」，由 `Layout` / `StorageConfig` / `PipelineConfig` / `LaunchConfig` 四个 POD 组合，分别管分块集群、共享内存 swizzle、流水线深度、线程划分。
- `get_best_config` 按 `check_validity → 选 layout → storage → pipeline → launch` 单向依赖链推断，`desc` 字段被按需消费到四个子结构。
- 三类枚举：`MmaKind`（BF16/MXFP8FP4，由 dtype 推，决定 `block_k=128/element_size`）、`GemmType`（调度类型，驱动布局候选与 multicast）、`KernelType`（1D1D/1D2D/NoSF，决定 SF 存储与 store block）。
- `expected_*` 遵循「0 表示默认」约定，允许「搬运用的维度」与「选配置用的维度」分离（K 轴分组的 `sum_k` vs `max_k`）。
- 易踩坑：`kPackedFP4` 实为 `torch::kInt8` 别名；FP8 与 FP4 共享 MXFP8FP4 路径故 `block_k` 都为 128。

## 7. 下一步学习建议

本讲只把「数据结构」讲透，**没有**回答「候选 layout 有哪些、如何比较选出最优」。这正是下一讲 **u5-l2（布局候选与最优配置选择）** 的内容：它会展开 `SM90ArchSpec::get_layout_candidates` 的候选枚举规则（block_m/n/cluster 约束、寄存器溢出、bank conflict 过滤）与 `get_layout_info` 的评估指标（`num_waves`、`last_wave_util`、`num_cycles`）如何选出最优 `Layout`。

在进入 u5-l2 前，建议你：

- 重读 [sm90.hpp:16-118](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm90.hpp#L16-L118) 的 `get_layout_candidates`，对照本讲的 `GemmDesc` 字段理解每个 `continue` 过滤条件读了哪个字段。
- 结合 u5-l3（compiled_dims 与调优旋钮）阅读 [csrc/jit_kernels/heuristics/runtime.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/runtime.hpp) 里的 `HeuristicsRuntime`，理解 `compiled_dims`、`block_size_multiple_of` 等旋钮如何反过来影响 `desc` 进入启发式后的行为。

更下游，u6 单元会把这些 `GemmConfig` 字段（`block_m/n/k`、`num_stages`、`num_tma/math_threads`）落到设备 kernel 内部，看你本讲填的「解法」在 tensor core 上如何真正执行。
