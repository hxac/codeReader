# compiled_dims 与运行时调优旋钮

## 1. 本讲目标

本讲聚焦 DeepGEMM 启发式系统的「可调旋钮层」。在 u5-l1 我们认识了描述「算什么」的 `GemmDesc`，在 u5-l2 我们跟完了「选最优布局」的 `get_best_config`；本讲要回答的是：**当默认启发式不够用，或者你想在「编译成本」和「运行性能」之间手动权衡时，库提供了哪些开关？**

学完本讲你应当能够：

- 说清 `compiled_dims`（每调用的局部参数）与 `set_ignore_compile_dims`（全局开关）如何决定哪些维度被烤成编译期常量、哪些留在运行时，并能解释这条「特化—编译次数—性能」的三角权衡。
- 掌握 `set_block_size_multiple_of` 如何通过 `std::lcm` 约束候选 block 尺寸，以及它在 SM90 / SM100 两代架构候选枚举里的不同作用点。
- 看懂 SM100 上 `get_theoretical_mk_alignment_for_contiguous_layout` 的数学推导，并能据此为分组连续（m-grouped contiguous）布局算出最小可用对齐。

## 2. 前置知识

本讲默认你已经建立以下认知（来自前置讲义，这里只做最简回顾）：

- **宿主/设备分层与 JIT 桥**（u1-l3、u3-l1）：宿主 C++ 负责派发与配置，设备 kernel 是 `.cuh` 模板；JIT 在运行时把形状固化为编译期常量后编译出 cubin。
- **`compiled_dims` 与 `get_compiled_dim`**（u3-l2）：一段如 `"nk"` 的字符串，约定「`0` 表示该维度留运行时、非 `0` 表示烤成编译期常量」；`get_compiled_dim(dim, name, compiled_dims)` 是这个约定的实现。
- **`GemmConfig` / `get_best_config`**（u5-l1、u5-l2）：配置由 `Layout`、`StorageConfig`、`PipelineConfig`、`LaunchConfig` 组合而成；`get_best_config` 先枚举 block 候选再评估选优。
- **`DeviceRuntime` 单例与 `get_arch_major()`**（u4-l1）：`get_arch_major()` 返回 `9`(Hopper/SM90) 或 `10`(Blackwell/SM100)，是全库派发的核心开关；`HeuristicsRuntime` 是与之并列的、专门承载「启发式旋钮」的进程级单例。

一个贯穿全讲的关键直觉：**DeepGEMM 把形状特化当作一种「投资」——投入一次 JIT 编译，换取一个对该形状高度优化的 cubin；特化的维度越多，单 kernel 越快，但需要编译的形状组合也越多。** 本讲三个旋钮本质上都在帮你管理这笔投资。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|---|---|---|
| [csrc/jit_kernels/heuristics/runtime.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/runtime.hpp) | `HeuristicsRuntime` 单例，承载全部启发式旋钮 | 三个旋钮的字段、getter/setter、`get_theoretical_mk_alignment_for_contiguous_layout` |
| [csrc/jit_kernels/impls/runtime_utils.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/runtime_utils.hpp) | 宿主侧公共工具 | `get_compiled_dim` 如何同时受 `ignore_compile_dims` 与 `compiled_dims` 双重控制 |
| [csrc/apis/runtime.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/runtime.hpp) | 运行时旋钮的 pybind11 注册 | `set_ignore_compile_dims`、`set_block_size_multiple_of` |
| [csrc/apis/layout.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/layout.hpp) | 布局工具的 pybind11 注册 | `set/get_mk_alignment_for_contiguous_layout`、`get_theoretical_mk_alignment_for_contiguous_layout` |
| [csrc/jit_kernels/heuristics/sm90.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm90.hpp) / [sm100.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm100.hpp) | 两代架构的 `get_layout_candidates` | 旋钮如何参与 block 候选枚举 |
| [csrc/apis/gemm.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/gemm.hpp) | GEMM API 层与 pybind 注册 | 各算子 `compiled_dims` 的默认值、k_grouped 对齐校验 |

## 4. 核心概念与源码讲解

### 4.1 形状特化与 ignore_compile_dims

#### 4.1.1 概念说明

回顾 u3-l2：设备 kernel 是高度参数化的模板，一次调用最终会被实例化成

```cpp
sm90_fp8_gemm_1d1d_impl<SHAPE_M, SHAPE_N, SHAPE_K, ...>
```

其中 `SHAPE_M/N/K` 三个模板参数要么是真实数值（编译期特化），要么是 `0`（表示「这个维度我不特化，运行时再传入」）。决定它们是哪一个的，就是本节的主角。

DeepGEMM 提供了**两层**控制，作用域不同：

- **`compiled_dims`（局部、每调用）**：作为参数传给 `fp8_gemm_nt(..., compiled_dims="nk")` 这样的 API，只影响这一次调用。默认值因算子而异（见 4.1.2）。
- **`set_ignore_compile_dims(True)`（全局、进程级）**：一个「紧急总开关」，开启后让**之后所有** kernel 的所有维度都退化为运行时（`0`），等价于把 `compiled_dims` 一键清空。

为什么要提供两层？因为它们服务于不同场景：`compiled_dims` 让你对不同 GEMM 用不同策略（比如权重形状固定就特化 N/K，token 数 M 频繁变就不特化 M）；`ignore_compile_dims` 则适合「我想整体关掉特化、做一次干净的对比实验或快速试跑」——不用逐个调用去改参数。

#### 4.1.2 核心流程

`compiled_dims` 是一个字符串，每个字符代表一个要特化的维度：`'m'`/`'n'`/`'k'`。判定逻辑在 `get_compiled_dim`：

```
对维度 name、运行时值 dim、策略串 compiled_dims：
    若 ignore_compile_dims 为真：直接返回 0（强制运行时）
    否则若 name 出现在 compiled_dims 中：返回 dim（特化）
    否则：返回 0（运行时）
```

注意第一行的优先级：**全局开关 `ignore_compile_dims` 压过一切**，即使你在某次调用里显式传了 `compiled_dims="nk"`，只要全局开关开着，所有维度照样返回 `0`。

各算子的**默认** `compiled_dims` 并不统一，而是按「哪个维度在该算子里最可能频繁变化」来设定。从 pybind 注册可以读出规律：

| 算子类别 | 默认 `compiled_dims` | 含义 |
|---|---|---|
| 稠密 `fp8_gemm_nt/nn`、`bf16_gemm_nt/nn` | `"nk"` | M 留运行时，N/K 特化（权重形状固定，token 数 M 变） |
| 稠密 `fp8_gemm_tn/tt`、`bf16_gemm_tn/tt` | `"mn"` | K 留运行时（TN/TT 下 K 成为易变维） |
| `m_grouped_*_contiguous` / `*_masked` | `"nk"` | 同上，分组数与 N/K 固定 |
| `k_grouped_*_contiguous` | `"mn"` | K 每组不同、最易变，故 K 留运行时 |

> 这张表的依据是 [csrc/apis/gemm.hpp:649-757](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/gemm.hpp#L649-L757) 中每个 `m.def` 的 `py::arg("compiled_dims") = ...`。

特化值最终如何作用到设备 kernel？以 SM90 1D1D 为例，宿主 `generate_impl` 把三个 `get_compiled_dim(...)` 的返回值填进模板实例化代码，而 `launch_impl` 又**始终**把运行时的 `m/n/k` 一起传进 kernel。设备侧再做一个覆盖：

```
shape_m = SHAPE_M != 0 ? SHAPE_M : shape_m;   // 特化值优先，否则用运行时值
shape_n = SHAPE_N != 0 ? SHAPE_N : shape_n;
shape_k = SHAPE_K != 0 ? SHAPE_K : shape_k;
```

这就是「同一份 kernel 源码、按需特化」的实现根基：特化维度走编译期常量（编译器可据此做循环展开、寄存器分配优化），未特化维度走运行时参数（一个 kernel 二进制能服务所有该维度的取值）。

#### 4.1.3 源码精读

先看 `HeuristicsRuntime` 上 `ignore_compile_dims` 的定义（与本讲另外两个旋钮并列）：

[csrc/jit_kernels/heuristics/runtime.hpp:12-24](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/runtime.hpp#L12-L24) —— 三个旋钮字段与 `ignore_compile_dims` 的 getter/setter，默认 `false`。

再看双开关的实际裁决点 `get_compiled_dim`：

[csrc/jit_kernels/impls/runtime_utils.hpp:22-31](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/runtime_utils.hpp#L22-L31) —— 第 23 行先查全局 `ignore_compile_dims`（命中即返回 `0`），第 26-29 行再按字符匹配 `compiled_dims`。

宿主把裁决结果填进模板实例化、同时把运行时形状传进启动：

[csrc/jit_kernels/impls/sm90_fp8_gemm_1d1d.hpp:53-55](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/sm90_fp8_gemm_1d1d.hpp#L53-L55) —— `generate_impl` 用三次 `get_compiled_dim` 产出 `SHAPE_M/N/K`。

[csrc/jit_kernels/impls/sm90_fp8_gemm_1d1d.hpp:66-74](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/sm90_fp8_gemm_1d1d.hpp#L66-L74) —— `launch_impl` 第 71 行始终传入运行时 `m/n/k`，与特化值配合。

设备侧的覆盖（前面伪代码的真实出处）：

[deep_gemm/include/deep_gemm/impls/sm90_fp8_gemm_1d1d.cuh:64-66](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm90_fp8_gemm_1d1d.cuh#L64-L66) —— `SHAPE_*` 为 `0` 时回退到运行时 `shape_*`。

最后是 Python 侧的注册（全局开关挂在这里）：

[csrc/apis/runtime.hpp:30-32](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/runtime.hpp#L30-L32) —— `deep_gemm.set_ignore_compile_dims` 直接改 `heuristics_runtime` 单例。

#### 4.1.4 代码实践

**实践目标**：直观看到「特化」与「不特化」对**编译产物数量**与**首次调用耗时**的影响。

**操作步骤**：

1. 清空 JIT 缓存：`rm -rf ~/.deep_gemm`（默认缓存目录，见 u3-l1）。
2. 准备一段最小 FP8 GEMM 调用（参考 u1-l4 / `tests/test_fp8_fp4.py` 的 `generate_normal` 构造输入），形状取 `M=1024, N=4096, K=4096`。
3. 开启 JIT 调试观察编译：`DG_JIT_DEBUG=1 python your_script.py`，记录从首次调用到「kernel ready」的耗时，以及 `~/.deep_gemm` 下新生成的 `kernel.*` 目录数量。
4. 再次清空缓存，在脚本最前面加一行 `deep_gemm.set_ignore_compile_dims(True)`，重复第 3 步。
5. 对两个形状略有不同的调用（如 `M=1024` 与 `M=1056`）在两种设置下各跑一次，对比是否触发重新编译。

**需要观察的现象**：

- `compiled_dims="nk"`（默认）下，改变 `M`（未特化维度）**不应**触发重新编译，因为 `SHAPE_M=0` 对所有 `M` 共用一个 cubin；改变 `N` 或 `K`（特化维度）**会**触发重新编译。
- `set_ignore_compile_dims(True)` 下，改变任意维度都**不应**触发重新编译——所有维度都是 `0`，一个 cubin 通吃。

**预期结果**：特化版本单形状性能更优（编译器能依据固定 N/K 优化），但形状空间变大时编译次数按特化维度的取值数增长；`ignore_compile_dims=True` 版本编译次数最少、首调最快，但单形状性能略低。具体加速比与编译耗时**待本地验证**（取决于硬件与编译后端，见 u3-l4 的 NVCC/NVRTC 选择）。

#### 4.1.5 小练习与答案

**练习 1**：对稠密 `fp8_gemm_nt`，默认 `compiled_dims="nk"`。若你的负载里 `N` 也会动态变化（比如动态宽度推理），你会怎么调整？

**参考答案**：把 `compiled_dims` 收窄为 `"k"`（只特化 K），让 `N` 也留运行时。这样 `N` 变化不再触发重编译，代价是损失一点对 `N` 的特化优化。不建议用 `set_ignore_compile_dims(True)`，因为那会连 `K` 一起去掉、影响所有其他算子；局部参数 `compiled_dims` 更精准。

**练习 2**：`set_ignore_compile_dims(True)` 与每次调用都传 `compiled_dims=""` 效果一样吗？为什么库要同时提供两者？

**参考答案**：对 `get_compiled_dim` 的返回值效果相同（都让所有 `SHAPE_*` 为 `0`）。区别在作用域：`compiled_dims` 是局部参数、可逐调用定制；`ignore_compile_dims` 是 `HeuristicsRuntime` 上的全局开关、一次设置影响之后所有 kernel。后者适合「整体关掉特化做对比/调试」的批量场景，免去逐个调用改参的麻烦。

---

### 4.2 block 对齐约束：set_block_size_multiple_of

#### 4.2.1 概念说明

`get_best_config` 选优的第一步是枚举 block 候选（`block_m`、`block_n`，`block_k` 固定为 `128/element_size`，见 u5-l2）。候选默认按一个「基础步长」生成（SM90 的 `block_n` 步长 16，SM100 非 swap 分支的 `block_n` 步长 32）。

`set_block_size_multiple_of(x)`（或传二元组 `(mx, nx)`）的作用是：**强制候选 block 尺寸是给定值的倍数**。它通过把基础步长与给定值取最小公倍数来实现：

\[
\text{step} = \mathrm{lcm}(\text{base\_step},\ x)
\]

步长变大 → 候选变稀疏 → 最终选出的 block 尺寸一定整除你的数据布局粒度。

什么时候需要它？典型场景是用户的张量在 M 或 N 方向有固定的对齐粒度（例如分组连续布局里每段必须落在某个边界上），此时若 block 尺寸不是该粒度的倍数，就会出现一个 block 横跨两段、带来不必要的边界处理或 padding。预先约束 block 尺寸可避免这类问题。

#### 4.2.2 核心流程

旋钮本身只存两个整数（`block_m_multiple_of`、`block_n_multiple_of`，默认都是 `1`）。真正起作用的地方在两代架构的 `get_layout_candidates` 里，且作用点不同：

- **SM90**：`block_m` 候选是固定的几个值（`{64,128}`，小 M 时追加 `16/32`，BF16 追加 `256`，或分组时取 `mk_alignment`），不受此旋钮影响；只有 `block_n` 候选用 `step = lcm(16, block_n_multiple_of)` 来生成。
- **SM100**：
  - **swap_ab 分支**（m-grouped 不走此分支，见 4.3）：`block_m` 候选用 `step = lcm(16, block_m_multiple_of)`，`block_n` 固定 `{128}`。
  - **非 swap 分支**：`block_m` 由 `desc.m` 大小定（`{32/64/128}`）；`block_n` 在 `16 % block_n_multiple_of == 0` 时追加 `16`，主候选用 `step = lcm(32, block_n_multiple_of)`。

可见「哪个 block 维度被约束」是架构与分支相关的，但机制统一：`std::lcm`。默认值 `1` 时 `lcm(base, 1) = base`，行为退化为不加约束，因此这个旋钮对默认负载是「零干扰」的。

> 为什么 SM90 的 `block_n` 基础步长是 16、SM100 非 swap 是 32？这与各自的张量核指令粒度及共享内存 bank 结构有关（u6-l2、u6-l1 详述）。本讲只需记住：基础步长是架构常量，旋钮只能在其上「放大」、不能缩小。

#### 4.2.3 源码精读

旋钮定义与默认值：

[csrc/jit_kernels/heuristics/runtime.hpp:26-37](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/runtime.hpp#L26-L37) —— `block_m_multiple_of`/`block_n_multiple_of` 字段与 getter/setter，默认 `1`。

SM90 的 `block_n` 候选步长（u5-l2 已点到，这里给全上下文）：

[csrc/jit_kernels/heuristics/sm90.hpp:38-57](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm90.hpp#L38-L57) —— 第 40 行 `step = std::lcm(16, get_block_n_multiple_of())`，随后从 `step` 枚举到 `end`（受寄存器溢出约束，1D1D 上限 160）。

SM100 的两个作用点：

[csrc/jit_kernels/heuristics/sm100.hpp:51-74](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm100.hpp#L51-L74) —— swap 分支第 52 行约束 `block_m`；非 swap 分支第 67-69 行按能否整除决定是否追加 `16`、并以 `lcm(32, ...)` 约束 `block_n`。

Python 侧注册支持一元或二元组：

[csrc/apis/runtime.hpp:33-41](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/runtime.hpp#L33-L41) —— 传 `int` 时 `mx=nx=x`，传 `(mx, nx)` 时分别设置，对应 Python 里 `deep_gemm.set_block_size_multiple_of(x)` 或 `set_block_size_multiple_of((mx, nx))`。

#### 4.2.4 代码实践

**实践目标**：观察约束 block 尺寸倍数后，选出的 `block_n`（或 `block_m`）候选集合如何变化。

**操作步骤**：

1. 写一段稠密 `fp8_gemm_nt` 调用，形状 `M=512, N=4096, K=4096`。
2. 开启配置打印：`DG_PRINT_CONFIGS=1 python your_script.py`（该环境变量会让库打印每个形状选中的 config，见 README / u5-l2）。
3. 记录默认下选中的 `Layout`（其中的 `block_n`）。
4. 在调用前加 `deep_gemm.set_block_size_multiple_of(64)`，再次运行并对比打印的 `block_n`。
5. 再试 `set_block_size_multiple_of(128)`，观察候选是否进一步收窄。

**需要观察的现象**：默认下 `block_n` 可能落在 16 的任意倍数（如 48、80…）；设为 64 的倍数后，`block_n` 只可能是 64、128、192…；设为 128 后只可能是 128、256…。

**预期结果**：约束值越大，可选 `block_n` 越少，若该值与硬件友好的 block 尺寸不重合，可能选不到最优 tile、性能下降；若恰好匹配你的数据对齐粒度，则避免跨段 tile、性能可能提升。具体数值**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：默认 `block_n_multiple_of = 1` 时，SM90 的 `block_n` 候选步长是多少？设为 `32` 后呢？

**参考答案**：默认 `lcm(16, 1) = 16`，步长 16；设为 32 后 `lcm(16, 32) = 32`，步长变为 32，候选数大约减半。

**练习 2**：为什么这个旋钮用 `std::lcm`（最小公倍数）而不是简单地把步长替换为给定值？

**参考答案**：因为存在一个架构强制的「基础步长」（SM90 的 16、SM100 的 32），它来自张量核粒度与 bank 对齐，不能被违反。`lcm(base, x)` 保证新步长既是用户要求 `x` 的倍数，又是基础步长的倍数——即同时满足「用户对齐」与「硬件对齐」两个约束的最宽松取值。

---

### 4.3 contiguous 对齐：mk_alignment 与理论值推导

#### 4.3.1 概念说明

「分组连续布局」（m-grouped contiguous，见 u7-l1）把多个 expert 的 token 拼成**一个连续张量**，用一个 `grouped_layout` 数组标记每段归属。为了让张量核 tile 不横跨 expert 边界、并使 TMA 描述符的 stride 可计算，**每段的 M（以及 K 方向）长度必须对齐到某个粒度**，这个粒度就是 `mk_alignment_for_contiguous_layout`。

这个旋钮直接决定了两个地方：

1. **block 候选**：m-grouped contiguous 的 `block_m` 直接被设成对齐值（因为每段要对齐，block 尺寸最好就是对齐值本身或其约数，库选择直接用对齐值）。
2. **输入校验**：k_grouped 等 API 会断言「对齐值能被 32 整除」并用它校验每段 K 的对齐。

对齐值越大 → 每段 padding 越多 → 显存与计算浪费越多；对齐值越小 → padding 越少，但受硬件/TMA 限制不能无限小。`get_theoretical_mk_alignment_for_contiguous_layout` 就是帮你算「在不违反硬件约束的前提下，最小能用多少」。

#### 4.3.2 核心流程

旋钮 `mk_alignment_for_contiguous_layout` 默认是 `kLegacyMKAlignmentForContiguousLayout = 128`（一个保守的历史值）。理论值函数则按架构分化：

- **非 SM100**（`get_arch_major() != 10`，即 SM90）：直接返回 legacy 的 `128`——SM90 上没有更细的推导。
- **SM100**（`arch_major == 10`）：从一个上限 `block_m = 224` 出发，按 `mma_step = 32` 向下缩减。若调用者提供了 `expected_m`（预期的单段 M），则在「缩减后仍能覆盖 `expected_m`」的前提下尽量缩小：

\[
B = \{64,\,96,\,128,\,160,\,192,\,224\}
\]

\[
\text{result} =
\begin{cases}
128 & \text{arch\_major} \ne 10 \\
224 & \text{arch\_major} = 10 \text{ 且未给 } \textit{expected\_m} \\
\min\{\,b \in B \mid b \geq \textit{expected\_m}\,\} \text{（上限 224）} & \text{arch\_major} = 10 \text{ 且给了 } \textit{expected\_m}
\end{cases}
\]

直觉解读：候选集合 \(B\) 是「224 起步、步长 32 向下」的六个值。给定 `expected_m` 后，选**最小的、仍不小于** `expected_m` 的候选；若 `expected_m` 超过 224，就钳在 224。这保证 block 尺寸足够覆盖最大单段、又尽量小以减少 padding。

**怎么用**：典型流程是先 `get_theoretical_mk_alignment_for_contiguous_layout(expected_m)` 算出理论最小值，再 `set_mk_alignment_for_contiguous_layout(该值)` 应用到全局，之后构造对齐到该值的输入并调用 m-grouped API。`tests/test_fp8_fp4.py` 与 `tests/test_bf16.py` 正是这个套路（见 4.3.4）。

#### 4.3.3 源码精读

旋钮字段与默认值（含 legacy 常量）：

[csrc/jit_kernels/heuristics/runtime.hpp:10-15](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/runtime.hpp#L10-L15) —— `kLegacyMKAlignmentForContiguousLayout = 128`，`mk_alignment_for_contiguous_layout` 默认取它。

理论值推导全貌：

[csrc/jit_kernels/heuristics/runtime.hpp:47-57](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/runtime.hpp#L47-L57) —— 第 48-49 行架构分流；第 51-55 行 SM100 的 224→32 步进缩减循环；注意循环条件 `block_m - mma_step >= expected_m` 正是「缩减后仍覆盖 expected_m」的数学表达。

旋钮如何决定 block 候选：

[csrc/jit_kernels/heuristics/sm90.hpp:31-36](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm90.hpp#L31-L36) —— SM90 上 m-grouped contiguous 的 `block_m_candidates` 直接取对齐值。

[csrc/jit_kernels/heuristics/sm100.hpp:32-43](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/sm100.hpp#L32-L43) —— SM100 上 m-grouped 三类（contiguous / psum / masked）统一 `block_m = get_mk_alignment_for_contiguous_layout()`，`block_n=128`，并按 N 与 SM 数奇偶决定 `cluster_n`。

输入校验中对齐值的消费：

[csrc/apis/gemm.hpp:309-314](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/gemm.hpp#L309-L314) —— k_grouped 校验：`k_alignment = get_mk_alignment_for_contiguous_layout()`，并断言 `k_alignment % 32 == 0`（解释了为何理论值集合都是 32 的倍数）。

psum 布局打包 kernel 也读它：

[csrc/jit_kernels/impls/smxx_layout.hpp:195](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/smxx_layout.hpp#L195) —— 仅在 `use_psum_layout` 时取对齐值，用于跳过 padding 行。

Python 侧注册：

[csrc/apis/layout.hpp:142-150](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/layout.hpp#L142-L150) —— `set/get_mk_alignment_for_contiguous_layout` 与 `get_theoretical_mk_alignment_for_contiguous_layout`（`expected_m` 默认 `nullopt`）。

#### 4.3.4 代码实践

**实践目标**：亲手算出理论对齐值，并用库验证；再体验「设小对齐值减少 padding」的效果。

**操作步骤**：

1. 假设你在 SM100 上跑 m-grouped，预估每段 `expected_m ≈ 160`。手算：候选集 \(\{64,96,128,160,192,224\}\) 中不小于 160 的最小值是 `160`。
2. 在 Python 里验证：`print(deep_gemm.get_theoretical_mk_alignment_for_contiguous_layout(160))`，应输出 `160`；再试 `161`，应输出 `192`；不传参应输出 `224`；传 `300`（超过 224）应输出 `224`。
3. 对照 `tests/test_fp8_fp4.py` 的写法（[tests/test_fp8_fp4.py:83-84](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/tests/test_fp8_fp4.py#L83-L84)）：先 `get_theoretical_mk_alignment_for_contiguous_layout(int(expected_m_per_group * 1.2))`（留 20% 余量），再 `set_mk_alignment_for_contiguous_layout(alignment)`，然后据此构造每段对齐的输入。
4. 对比默认 `128` 与设为理论最小值两种情况下，构造出的连续张量总 M（= 各段对齐后之和），体会 padding 差异。

**需要观察的现象**：`expected_m` 越接近某个候选上沿，理论值跳变越明显（如 128→129 直接从 128 跳到 160）；设小对齐值后，总 M 变小、padding 段变少。

**预期结果**：函数返回值严格落在 \(\{64,96,128,160,192,224\}\)（SM100）或固定 128（SM90），且为 `expected_m` 的「向上取整到候选集」。本实践可在任意 SM100 机器上验证；若仅 SM90 机器，则始终返回 128。

#### 4.3.5 小练习与答案

**练习 1**：为什么 k_grouped 校验里要断言 `k_alignment % 32 == 0`？理论值函数的候选步长恰好是 32，这之间有联系吗？

**参考答案**：有直接联系。SM100 的理论值候选集 \(B\) 步长正是 `mma_step = 32`，所有候选都是 32 的倍数；SM90 返回 128 也是 32 的倍数。而 k_grouped 用 1D1D kernel、recipe=(1,1,gran_k)（gran_k 为 32 或 128），其 TMA 与 tile 对齐都依赖 32 粒度，故库用断言把「对齐值必须是 32 倍数」作为不变量保护起来。

**练习 2**：若你把 `mk_alignment_for_contiguous_layout` 设成 `16`（小于理论最小值）会发生什么？

**参考答案**：`set_` 本身不做下界校验，会接受 `16`，但后续很可能在 TMA 描述符构造或 kernel 内触发断言失败（如 bank 对齐、UTCCP 对齐不满足）。正因如此，库提供 `get_theoretical_*` 让你**先**算出安全下界再用，测试代码也总是「先 theoretical、后 set」。

## 5. 综合实践

把三个旋钮串起来，完成 spec 要求的核心对比：**在同一个形状上，比较 `compiled_dims="nk"`（默认特化）与 `set_ignore_compile_dims(True)`（全运行时）两种设置下的编译产物数量与性能，并解释特化带来的「编译—性能」权衡。**

**任务步骤**：

1. **准备**：`rm -rf ~/.deep_gemm` 清缓存；写一个函数 `run_gemm(M, N, K)`，内部构造 FP8 输入与 SF（参考 `tests/test_fp8_fp4.py` 的 `generate_normal` / `per_token_cast_to_fp8`），调用 `deep_gemm.fp8_gemm_nt`，并用 `deep_gemm.testing.calc_diff` 校验数值。
2. **基线（特化）**：在 `DG_JIT_DEBUG=1 DG_PRINT_CONFIGS=1` 下，依次跑三个**仅 M 不同**的形状：`(1024,4096,4096)`、`(1056,4096,4096)`、`(2048,4096,4096)`。记录：
   - 触发了几次 JIT 编译？（应只 1 次：N/K 固定、M 未特化。）
   - 每个 shape 选中的 `Layout`（`DG_PRINT_CONFIGS` 打印）。
   - 用 `deep_gemm.testing.bench` 测稳态耗时与 TFLOPS。
3. **对照（全运行时）**：清缓存，脚本开头加 `deep_gemm.set_ignore_compile_dims(True)`，重复第 2 步。
4. **再对照（过特化）**：清缓存，去掉 `ignore`，改成每次调用传 `compiled_dims="mnk"`（三个维度全特化），重复第 2 步——这次 `(1024,...)` 与 `(1056,...)` 因 M 不同应**各编译一次**。
5. **分析**：填一张表（列：设置、编译次数、首调耗时、稳态 TFLOPS），回答：
   - 为什么默认 `"nk"` 在「仅 M 变化」时只编译一次？
   - `"mnk"` 为何编译次数随 M 取值数线性增长？这值得吗？
   - `ignore_compile_dims=True` 牺牲了什么换来了什么？

**需要观察的现象与预期结果**：

| 设置 | 编译次数（3 个 M） | 稳态性能相对高低 |
|---|---|---|
| `compiled_dims="nk"`（默认） | 1 | 最优（N/K 特化） |
| `compiled_dims="mnk"` | 3（每个 M 一次） | 略优于或持平 `"nk"`，但编译开销大 |
| `set_ignore_compile_dims(True)` | 1 | 略低（无任何特化） |

**结论方向**：特化是「用编译时间换运行时间」的投资。固定维度（权重 N/K）值得特化；高频变化维度（token M）不值得特化，否则编译次数爆炸。`ignore_compile_dims` 是「全部不投资」的极端档，适合快速试跑或冷启动敏感场景。具体数字**待本地验证**。

> 进阶：把 4.2 的 `set_block_size_multiple_of` 与 4.3 的 `set_mk_alignment_for_contiguous_layout` 也接入——改用 `m_grouped_fp8_gemm_nt_contiguous`，先用 `get_theoretical_mk_alignment_for_contiguous_layout` 算对齐、`set_mk_alignment_for_contiguous_layout` 应用，再用 `set_block_size_multiple_of` 约束 block，观察 `DG_PRINT_CONFIGS` 选中的 `block_m` 是否等于你设的对齐值。

## 6. 本讲小结

- DeepGEMM 把「形状特化」视为投资：特化维度越多，单 kernel 越快，但需编译的形状组合越多。本讲三个旋钮都在帮你管理这笔投资。
- **形状特化**受双重控制：局部参数 `compiled_dims`（默认因算子而异：稠密 nt/nn 与 m-grouped 为 `"nk"`，tn/tt 与 k_grouped 为 `"mn"`）与全局开关 `set_ignore_compile_dims`（优先级最高，一键清空所有特化）；二者都经由 `get_compiled_dim` 裁决，设备侧以 `SHAPE_* != 0 ? SHAPE_* : shape_*` 覆盖。
- **block 对齐约束** `set_block_size_multiple_of` 通过 `std::lcm(base, x)` 放大候选步长，同时满足「用户对齐」与「硬件基础步长」（SM90 的 16 / SM100 的 32）；默认值 `1` 时零干扰。
- **contiguous 对齐** `mk_alignment_for_contiguous_layout` 决定 m-grouped 每段的 M/K 对齐粒度（默认保守的 128），并直接成为 `block_m` 候选；其理论最小值由 `get_theoretical_mk_alignment_for_contiguous_layout` 推导——SM90 固定 128，SM100 在候选集 \(\{64,...,224\}\)（步长 32）中取不小于 `expected_m` 的最小值。
- 三类旋钮虽注册位置不同（`runtime.hpp` vs `layout.hpp`），但都挂在同一个 `HeuristicsRuntime` 进程级单例上，统一遵守「`0`/`1` 表示默认」与「先算理论值再 set」的约定。

## 7. 下一步学习建议

- **u6-1 内核入口：SM90 FP8 GEMM 1D1D**：下钻到设备 kernel 内部，看本讲的 `SHAPE_*` 特化值与 `BLOCK_M/N/K`（来自 u5-l2 的 `Layout`）如何共同决定共享内存划分与流水线。
- **u7-1 连续布局的 M 轴分组 GEMM**：本讲的 `mk_alignment` 旋钮在那里是刚需，结合 `grouped_layout` 索引理解「为什么每段必须对齐」。
- **u7-3 K 轴分组 GEMM 与 psum 布局**：看 k_grouped 如何消费 `k_alignment % 32 == 0` 不变量，以及 psum 布局（`smxx_layout.hpp:195`）如何利用对齐值跳过 padding 行。
- **延伸阅读**：对照 `tests/test_fp8_fp4.py`、`tests/test_bf16.py`、`tests/test_mega_moe.py` 中「先 `get_theoretical_*`、再 `set_*`」的真实用法，巩固三个旋钮的实战组合。
