# 代码生成与模板实例化

## 1. 本讲目标

本讲是「JIT 编译系统」单元的第二篇，承接 [u3-l1](u3-l1-jit-arch-overview.md) 建立的「宿主运行时 vs 设备内核」「编译期 vs 运行时」「cubin vs kernel 符号」三对概念。u3-l1 讲清了 JIT 的**总骨架**（`compiler->build` 怎么缓存、`KernelRuntime` 怎么加载），本讲则回答骨架里的一个核心问题：

> 宿主在拿到一个具体的 GEMM 形状（如 `M=4096, N=4096, K=4096`）后，**那段被 `compiler->build` 编译的 `.cu` 源码到底是怎么来的？**

学完本讲你应当能够：

1. 看懂 `SM90FP8Gemm1D1DRuntime::generate_impl` 如何用 `fmt::format` 把 17 个编译期常量填进一段 `.cu` 模板，生成可被 nvcc/nvrtc 编译的源码。
2. 理解「取函数地址强制模板实例化」这一 C++ 技巧为何是运行时 JIT 的关键。
3. 掌握 `compiled_dims`（默认 `"nk"`）如何用「`0` 表示运行时、非 `0` 表示编译期常量」的约定，在形状维度上做**选择性特化**。
4. 看懂 `LaunchRuntime` 这个 CRTP 基类如何用 `generate` / `launch_impl` 两个钩子，把所有 `*Runtime` 子类的「生成源码 → 编译 → 启动」流程统一成三行代码。

---

## 2. 前置知识

本讲默认你已经读过 u3-l1，并掌握以下概念：

- **宿主（host）/ 设备（device）**：CPU 侧调度代码（`csrc/`）与 GPU 侧 kernel 模板（`deep_gemm/include/deep_gemm/impls/*.cuh`）。
- **编译期 vs 运行时**：`BLOCK_M` 这类值若能写进模板参数，编译器就能把它当常量优化（循环展开、寄存器分配更优）；若只能在运行时传入，则是一般变量。
- **JIT 桥**：运行时代码生成把「运行时才知道的形状」固化成「编译期常量」，再交给编译器，从而兼得灵活性与性能。
- **CRTP（Curiously Recurring Template Pattern，奇异递归模板）**：`class Derived : public Base<Derived>`，基类在编译期就能知道派生类的真实类型，从而静态调用派生类的方法（无虚函数开销）。这是 `LaunchRuntime` 的核心机制。

两个 C++ 小知识（不熟的话记一句话即可）：

- **取函数地址强制实例化**：对模板函数写 `&foo<具体参数>`，会让编译器在当前编译单元里把这份模板**真正展开编译**，哪怕这个地址从未被调用。这是「我只想让编译器替我实例化某个 kernel」的标准做法。
- **`fmt::format` 的 `{}` 占位**：`fmt::format(R"(...{}...{})", a, b)` 会按顺序把 `a`、`b` 填进 `{}`。若模板字符串里要输出字面 `{` `}`（比如生成 C++ 代码的花括号），需写成 `{{` `}}`。

---

## 3. 本讲源码地图

本讲围绕以下文件展开：

| 文件 | 角色 | 本讲关注点 |
|---|---|---|
| [csrc/jit_kernels/impls/sm90_fp8_gemm_1d1d.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/sm90_fp8_gemm_1d1d.hpp) | 宿主 Runtime 类 + 宿主入口函数 | `generate_impl` 生成源码、`launch_impl` 启动、`generate/build/launch` 三步 |
| [csrc/jit_kernels/impls/runtime_utils.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/runtime_utils.hpp) | 宿主侧公共工具 | `get_compiled_dim`、各 `to_string` 把枚举/类型变成 C++ 源码片段 |
| [csrc/jit/kernel_runtime.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/kernel_runtime.hpp) | `LaunchRuntime` CRTP 基类 | `generate` 注入 include 哈希、`launch` 组装启动配置 |
| [deep_gemm/include/deep_gemm/impls/sm90_fp8_gemm_1d1d.cuh](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm90_fp8_gemm_1d1d.cuh) | 设备 kernel 模板 | 17 个模板参数签名、`SHAPE_*` 覆盖运行时值的逻辑 |
| [csrc/jit_kernels/heuristics/config.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/config.hpp) | 配置数据结构 | `GemmDesc.compiled_dims` 字段 |
| [csrc/apis/gemm.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/gemm.hpp) | Python 绑定层 | `compiled_dims` 的默认值 `"nk"` |
| [csrc/jit/compiler.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/compiler.hpp) | JIT 编译器 | `build` 把源码写入缓存目录的 `kernel.cu` |

一个贯穿全讲的**命名约定**（u3-l1 已建立）：宿主 Runtime 类文件 `csrc/jit_kernels/impls/sm90_fp8_gemm_1d1d.hpp` 与设备 kernel 模板文件 `deep_gemm/include/deep_gemm/impls/sm90_fp8_gemm_1d1d.cuh` **一一对应**，前者负责「生成 + 启动」，后者是被生成的模板本体。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **模板实例化代码生成**：`generate_impl` 如何拼出一段 `.cu` 源码。
2. **`compiled_dims` 特化机制**：如何在 M/N/K 维度上选择性特化。
3. **`LaunchRuntime` 协作**：基类如何统一 generate/build/launch 三步。

### 4.1 模板实例化代码生成

#### 4.1.1 概念说明

设备 kernel `sm90_fp8_gemm_1d1d_impl` 是一个**高度参数化模板**——它有 17 个模板参数（分块大小、swizzle、流水线级数、线程划分、GEMM 类型……）。模板本身只是「配方」，编译器不会主动编译它，除非有人用具体参数去**实例化**它。

问题来了：运行时才知道 `BLOCK_M=128, BLOCK_N=128, BLOCK_K=128, num_stages=3 ...` 这些值。如何让它们变成编译期常量？

DeepGEMM 的答案极其直接：**在宿主侧用 `fmt::format` 字符串拼接出一段极短的 `.cu` 源码**，这段源码只做一件事——用取地址的方式强制实例化一个特定参数组合的 kernel。然后把这段源码交给 `compiler->build` 编译。

换言之，JIT 的「代码生成」其实非常薄：它不生成算法逻辑（算法逻辑全在 `.cuh` 模板里），只生成一行**「请用这些常量实例化这个模板」**的代码。这是 DeepGEMM「轻量 JIT」哲学的体现。

#### 4.1.2 核心流程

一次 `sm90_fp8_gemm_1d1d` 调用里，代码生成发生在宿主入口函数的最后三步：

```
构造 GemmDesc ──► get_best_config 得到 GemmConfig ──► 构造 TMA 描述符
        │
        ▼
   组装 SM90FP8Gemm1D1DRuntime::Args（含 desc、config、tma desc）
        │
        ▼
   code   = Runtime::generate(args)    # 本讲重点：拼出 .cu 源码
   runtime= compiler->build(name, code) # 编译 + 缓存 + 加载（u3-l1 已讲）
   Runtime::launch(runtime, args)       # 启动（4.3 讲）
```

`generate_impl` 拼出的源码长这样（关键骨架）：

```cpp
#include <deep_gemm/impls/sm90_fp8_gemm_1d1d.cuh>
using namespace deep_gemm;
static void __instantiate_kernel() {
    auto ptr = reinterpret_cast<void*>(&sm90_fp8_gemm_1d1d_impl<
        <17 个具体常量逐个填入>
    >);
};
```

要点：

1. `#include` 把设备模板（`.cuh`）拉进当前编译单元。
2. `&sm90_fp8_gemm_1d1d_impl<...>` 取地址 → **强制编译器实例化**这个特定模板参数组合。
3. `reinterpret_cast<void*>` 只是为了得到一个地址（防止编译器因「未使用」而优化掉实例化），`__instantiate_kernel` 这个函数本身**永远不会被调用**——它只是实例化的「锚点」。

#### 4.1.3 源码精读

先看设备模板的 17 个参数签名，这是被填空的目标：

设备 kernel 模板签名（17 个模板参数）：[sm90_fp8_gemm_1d1d.cuh:L30-L38](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm90_fp8_gemm_1d1d.cuh#L30-L38)

```cpp
template <uint32_t SHAPE_M, uint32_t SHAPE_N, uint32_t SHAPE_K,
          uint32_t kNumGroups,
          uint32_t BLOCK_M, uint32_t BLOCK_N, uint32_t BLOCK_K,
          uint32_t kSwizzleAMode, uint32_t kSwizzleBMode,
          uint32_t kNumStages,
          uint32_t kNumTMAThreads, uint32_t kNumMathThreads,
          uint32_t kNumTMAMulticast, bool kIsTMAMulticastOnA,
          uint32_t kNumSMs,
          GemmType kGemmType, typename cd_dtype_t>
```

再看宿主侧 `generate_impl` 怎么填这 17 个空：[sm90_fp8_gemm_1d1d.hpp:L33-L64](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/sm90_fp8_gemm_1d1d.hpp#L33-L64)

```cpp
static std::string generate_impl(const Args& args) {
    return fmt::format(R"(
#include <deep_gemm/impls/sm90_fp8_gemm_1d1d.cuh>
using namespace deep_gemm;
static void __instantiate_kernel() {{
    auto ptr = reinterpret_cast<void*>(&sm90_fp8_gemm_1d1d_impl<
        {}, {}, {}, {}, {}, {}, {}, {}, {}, {}, {}, {}, {}, {}, {}, {}, {}
    >);
}};
)",
        get_compiled_dim(args.gemm_desc.m, 'm', args.gemm_desc.compiled_dims),
        get_compiled_dim(args.gemm_desc.n, 'n', args.gemm_desc.compiled_dims),
        get_compiled_dim(args.gemm_desc.k, 'k', args.gemm_desc.compiled_dims),
        args.gemm_desc.num_groups,
        args.gemm_config.layout.block_m, args.gemm_config.layout.block_n, args.gemm_config.layout.block_k,
        args.gemm_config.storage_config.swizzle_a_mode, args.gemm_config.storage_config.swizzle_b_mode,
        args.gemm_config.pipeline_config.num_stages,
        args.gemm_config.launch_config.num_tma_threads, args.gemm_config.launch_config.num_math_threads,
        args.gemm_config.layout.get_cluster_size(), args.gemm_config.layout.cluster_n > 1,
        args.gemm_config.launch_config.num_sms, to_string(args.gemm_desc.gemm_type),
        to_string(args.gemm_desc.cd_dtype));
}
```

注意几处细节：

- 模板串里的 `{{` `}}` 是 `fmt::format` 的转义，最终生成的 C++ 代码里是真正的 `{` `}`（`__instantiate_kernel()` 的函数体花括号）。而 `<>` 里的 `{}` 才是「按顺序填入实参」的占位符。
- 17 个 `{}` 与 17 个实参**严格按位置一一对应**。下表给出完整映射：

| # | 设备模板参数 | `generate_impl` 实参表达式 | 来源 / 含义 |
|---|---|---|---|
| 1 | `SHAPE_M` | `get_compiled_dim(m,'m',compiled_dims)` | M 维特化值（见 4.2，`0`=运行时） |
| 2 | `SHAPE_N` | `get_compiled_dim(n,'n',compiled_dims)` | N 维特化值 |
| 3 | `SHAPE_K` | `get_compiled_dim(k,'k',compiled_dims)` | K 维特化值 |
| 4 | `kNumGroups` | `num_groups` | 分组 GEMM 的 expert 数（普通 GEMM=1） |
| 5 | `BLOCK_M` | `layout.block_m` | M 分块大小 |
| 6 | `BLOCK_N` | `layout.block_n` | N 分块大小 |
| 7 | `BLOCK_K` | `layout.block_k` | K 分块大小（FP8 固定 128） |
| 8 | `kSwizzleAMode` | `storage_config.swizzle_a_mode` | A 的共享内存 swizzle 模式 |
| 9 | `kSwizzleBMode` | `storage_config.swizzle_b_mode` | B 的共享内存 swizzle 模式 |
| 10 | `kNumStages` | `pipeline_config.num_stages` | 软件流水线级数 |
| 11 | `kNumTMAThreads` | `launch_config.num_tma_threads` | 负责搬运的 TMA 线程数 |
| 12 | `kNumMathThreads` | `launch_config.num_math_threads` | 负责计算的线程数 |
| 13 | `kNumTMAMulticast` | `layout.get_cluster_size()` | cluster 大小（`cluster_m*cluster_n`） |
| 14 | `kIsTMAMulticastOnA` | `layout.cluster_n > 1` | 是否沿 A(M) 轴做 multicast |
| 15 | `kNumSMs` | `launch_config.num_sms` | 参与计算的 SM 数 |
| 16 | `kGemmType` | `to_string(gemm_type)` | GEMM 类型枚举（如 `GemmType::Normal`） |
| 17 | `cd_dtype_t` | `to_string(cd_dtype)` | C/D 数据类型（如 `float`） |

最后两个参数是**类型/枚举**而非整数，所以要先经 `to_string` 转成合法的 C++ 源码记号。这两个工具函数就在 [runtime_utils.hpp:L41-L63](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/runtime_utils.hpp#L41-L63)：

```cpp
static std::string to_string(const GemmType& type) {
    switch (type) {
        case GemmType::Normal:              return "GemmType::Normal";
        case GemmType::KGroupedContiguous:  return "GemmType::KGroupedContiguous";
        // ... 其余枚举值 ...
    }
}
static std::string to_string(const at::ScalarType& dtype) {
    switch (dtype) {
        case torch::kFloat:         return "float";
        case torch::kFloat8_e4m3fn: return "cutlass::float_e4m3_t";
        // ... 其余类型 ...
    }
}
```

> 注意：`cd_dtype_t` 在 SM90 FP8 kernel 里恒为 `float`（见设备侧 `DG_STATIC_ASSERT(cute::is_same_v<cd_dtype_t, float>, ...)`，[sm90_fp8_gemm_1d1d.cuh:L56](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm90_fp8_gemm_1d1d.cuh#L56)），但保留成模板参数是为了让同一套生成逻辑也能服务 SM100 等支持 BF16 输出的 kernel。

生成完成后，宿主入口函数把它们串成三步：[sm90_fp8_gemm_1d1d.hpp:L140-L143](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/sm90_fp8_gemm_1d1d.hpp#L140-L143)

```cpp
const auto code = SM90FP8Gemm1D1DRuntime::generate(args);          // ① 生成源码
const auto runtime = compiler->build("sm90_fp8_gemm_1d1d", code);  // ② 编译+缓存+加载
SM90FP8Gemm1D1DRuntime::launch(runtime, args);                     // ③ 启动
```

`compiler->build` 会把这段 `code` 写入缓存目录的 `kernel.cu`（缓存键含 `code`，见 [compiler.hpp:L100-L102](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/compiler.hpp#L100-L102)），这意味着**生成的源码会被永久留在磁盘上**，可供我们直接阅读（见 4.1.4 实践）。

#### 4.1.4 代码实践

**实践目标**：亲眼看到 DeepGEMM 为一次真实 GEMM 生成的 `.cu` 源码，并验证 17 个模板参数确实来自运行时形状与 `GemmConfig`。

**操作步骤**（需 SM90 Hopper 机器；若无机器，见末尾「无机器」分支）：

1. 编写一个最小调用脚本 `gen_demo.py`：

   ```python
   # 示例代码（非项目原有文件）
   import torch, deep_gemm
   from deep_gemm.utils.per_token_cast import per_token_cast_to_fp8

   M = N = K = 4096
   a_ref = torch.randn(M, K, device='cuda', dtype=torch.float32) * 0.05
   b_ref = torch.randn(N, K, device='cuda', dtype=torch.float32) * 0.05
   a, sfa = per_token_cast_to_fp8(a_ref, torch.float32.e4m3)        # recipe=(1,128)
   b, sfb = per_token_cast_to_fp8(b_ref, torch.float32.e4m3)
   d = torch.empty(M, N, device='cuda', dtype=torch.float32)
   deep_gemm.fp8_fp4_gemm_nt((a, sfa), (b, sfb), d)                 # 默认 compiled_dims="nk"
   ```

2. 以调试模式运行，捕获控制台输出的生成源码：

   ```bash
   DG_JIT_DEBUG=1 python gen_demo.py 2>&1 | grep -A 30 "Generated kernel code:"
   ```

3. **或**直接读磁盘缓存（推荐，更完整）。清空缓存后重跑一次：

   ```bash
   rm -rf ~/.deep_gemm
   python gen_demo.py
   cat ~/.deep_gemm/cache/kernel.sm90_fp8_gemm_1d1d.*/kernel.cu
   ```

**需要观察的现象 / 预期结果**：

- 你会看到一段以 `// Includes' hash value: ...` 开头（由 `LaunchRuntime::generate` 注入，4.3 讲）的源码，紧跟着 `#include <deep_gemm/impls/sm90_fp8_gemm_1d1d.cuh>` 与 `__instantiate_kernel`。
- 在 `<>` 里应能看到形如 `0, 4096, 4096, 1, 128, 128, 128, 128, 128, 3, 128, 128, 1, false, <SM数>, GemmType::Normal, float` 的实例化列表。
  - 第 1 个 `0` 即 `SHAPE_M=0`（M 未特化，因 `compiled_dims="nk"` 不含 `m`）。
  - 第 2、3 个 `4096` 即被特化的 `SHAPE_N` / `SHAPE_K`。
  - 中间的 `128, 128, 128` 是 `BLOCK_M/N/K`，`3` 是 `num_stages`（具体值由 `get_best_config` 决定，可能不同，但结构一致）。
- 把你看到的实例化列表与本讲 4.1.3 的映射表**逐位对齐**，标注每个值来自 `GemmDesc` 还是 `GemmConfig`。

**无机器分支（源码阅读型）**：若手边没有 SM90 GPU，可跳过运行，直接对照 [sm90_fp8_gemm_1d1d.hpp:L34-L52](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/sm90_fp8_gemm_1d1d.hpp#L34-L52) 的 `fmt::format` 串，手工「脑补」把 `compiled_dims="nk"`、`M=N=K=4096`、`block_m=n=k=128` 代入，写出 `__instantiate_kernel` 的展开结果，再与上面预期对照。

> 待本地验证：第 13/14 位（`kNumTMAMulticast`、`kIsTMAMulticastOnA`）的具体值取决于 `get_best_config` 对该形状选出的 cluster 配置，不同形状可能为 `1, false` 或 `2, ...`。

#### 4.1.5 小练习与答案

**练习 1**：`generate_impl` 里 `__instantiate_kernel` 这个函数从来没有被任何地方调用，删掉取地址那一行会怎样？

**参考答案**：删掉后编译器没有任何理由去实例化 `sm90_fp8_gemm_1d1d_impl<...>` 这个特定参数组合，模板就不会被编译进 cubin，`KernelRuntime` 在加载 cubin 时将找不到 kernel 符号（或符号数为 0），启动失败。`reinterpret_cast<void*>(&...)` 的唯一作用就是「取地址」这个动作本身强制实例化。

**练习 2**：为什么 17 个实参里，只有最后两个（`gemm_type`、`cd_dtype`）需要套 `to_string`，前面的整数不用？

**参考答案**：前面的 `BLOCK_M` 等是 `uint32_t`，`fmt::format` 会直接把它们格式化成数字字面量（合法的 C++ 整数模板实参）；而 `GemmType` 和 `at::ScalarType` 是枚举/类型，必须转成像 `GemmType::Normal`、`float` 这样的 C++ 记号字符串，才能作为模板实参出现在生成的源码里。

---

### 4.2 compiled_dims 特化机制

#### 4.2.1 概念说明

设备 kernel 的性能高度依赖编译器能否把循环边界、分块大小当作**编译期常量**来优化（展开循环、静态分配寄存器）。`BLOCK_M/N/K`、`num_stages` 等永远特化没问题——它们的候选值有限。

但 M/N/K 这三个**问题形状**维度很麻烦：

- 全特化（把具体 M/N/K 写进模板）→ 每个新形状都要重新 JIT 编译一次，编译耗时与缓存膨胀。
- 全不特化（M/N/K 全走运行时变量）→ 编译器无法做基于常量的优化，性能下降。

DeepGEMM 的折中是 **`compiled_dims`**：一个字符串（如 `"nk"`、`"mn"`），描述「哪些维度值得特化」。出现在字符串里的维度被固化为编译期常量，其余维度保留为运行时变量。

#### 4.2.2 核心流程

特化的实现极其优雅，靠一个统一的**「`0` = 运行时，非 `0` = 编译期常量」约定**：

1. `get_compiled_dim(dim, name, compiled_dims)` 决定某维度是否特化：若 `name` 出现在 `compiled_dims` 字符串里，返回真实值（特化）；否则返回 `0`（不特化）。
2. 该值作为 `SHAPE_M/N/K` 填进模板。
3. 设备 kernel 内部用一行三元判断：`SHAPE_x != 0` 就用编译期常量覆盖，否则用运行时传入的 `shape_x`。

覆盖规则可写成：

\[
\text{shape}_x \;\leftarrow\; \begin{cases} \text{SHAPE}_x & \text{若 } \text{SHAPE}_x \neq 0 \\ \text{shape}_x^{\text{runtime}} & \text{若 } \text{SHAPE}_x = 0 \end{cases}
\]

这套约定的精妙之处：**无论某维度是否特化，kernel 的函数签名都一样**（运行时 `shape_m/n/k` 始终作为参数传入，见 `launch_impl`）。特化只是让编译器在内部用常量替代，无需为「特化/非特化」维护两套签名。

#### 4.2.3 源码精读

特化判定的核心函数：[runtime_utils.hpp:L22-L31](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/runtime_utils.hpp#L22-L31)

```cpp
static int get_compiled_dim(const int& dim, const char& name, const std::string& compiled_dims) {
    if (heuristics_runtime->get_ignore_compile_dims())
        return 0;                          // 全局开关：强制所有维度都不特化
    for (const char& c: compiled_dims) {
        if (name == c)
            return dim;                    // 该维度在 compiled_dims 里 → 返回真实值（特化）
    }
    return 0;                              // 不在 → 返回 0（运行时）
}
```

它读两个输入：

- `compiled_dims`：来自 `GemmDesc.compiled_dims`（[config.hpp:L23](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/config.hpp#L23)），最终来自 Python 调用。
- `heuristics_runtime->get_ignore_compile_dims()`：一个全局「紧急关闭特化」开关（[runtime.hpp:L12,L18-L23](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/heuristics/runtime.hpp#L12)），由 Python 侧 `set_ignore_compile_dims(True)` 设置，返回 `0` 让所有维度都退化为运行时（牺牲性能换编译速度）。

Python 侧的默认值在绑定层：[gemm.hpp:L653](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/gemm.hpp#L653)

```cpp
m.def("fp8_fp4_gemm_nt", &fp8_fp4_gemm_nt, ...,
      py::arg("compiled_dims") = "nk", ...);
```

即 `fp8_fp4_gemm_nt` 默认 `compiled_dims="nk"`：N、K 特化，M 走运行时。为什么是 N、K 而非 M？因为在大模型推理里 **M（token 数）经常变化**（每次请求 token 数不同），特化 M 会触发频繁重编译；而 N、K（权重维度）通常固定。对应地，`fp8_fp4_gemm_tn/tt` 的默认是 `"mn"`（[gemm.hpp:L665,L671](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/gemm.hpp#L665)），因为转置后固定维度变成了 M、N。

设备侧的覆盖逻辑：[sm90_fp8_gemm_1d1d.cuh:L63-L66](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/deep_gemm/include/deep_gemm/impls/sm90_fp8_gemm_1d1d.cuh#L63-L66)

```cpp
// Overwrite shape constants if the compiler gives
shape_m = SHAPE_M != 0 ? SHAPE_M : shape_m;
shape_n = SHAPE_N != 0 ? SHAPE_N : shape_n;
shape_k = SHAPE_K != 0 ? SHAPE_K : shape_k;
```

注意 `shape_m/n/k` 同时也是 kernel 的**函数参数**（由 `launch_impl` 传入，见 [sm90_fp8_gemm_1d1d.hpp:L71](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/sm90_fp8_gemm_1d1d.hpp#L71)）。当 `SHAPE_x=0` 时，运行时参数原样生效；当 `SHAPE_x` 为具体值时，编译器在编译期就能确定 `shape_x`（因为 `SHAPE_x` 是模板常量），从而展开所有依赖它的循环。这正是「同一签名、按需特化」的实现。

#### 4.2.4 代码实践

**实践目标**：验证 `compiled_dims` 取值如何改变生成源码里的 `SHAPE_*`，并理解特化对编译产物数量的影响。

**操作步骤**：

1. 复用 4.1.4 的脚本，分别用两种 `compiled_dims` 跑同一个形状，对比缓存目录里生成的 `kernel.cu`：

   ```bash
   # 方式 A：默认 "nk"
   rm -rf ~/.deep_gemm && python gen_demo.py
   cp ~/.deep_gemm/cache/kernel.sm90_fp8_gemm_1d1d.*/kernel.cu /tmp/code_nk.cu

   # 方式 B：改成 ""（空串，全部运行时）
   #   在 gen_demo.py 的 fp8_fp4_gemm_nt(...) 调用里加 compiled_dims=""
   rm -rf ~/.deep_gemm && python gen_demo.py
   cp ~/.deep_gemm/cache/kernel.sm90_fp8_gemm_1d1d.*/kernel.cu /tmp/code_empty.cu
   ```

2. 用 `diff` 或肉眼对比 `/tmp/code_nk.cu` 与 `/tmp/code_empty.cu` 中 `__instantiate_kernel` 的 `<>` 列表。

**需要观察的现象 / 预期结果**：

- `code_nk.cu`：`<0, 4096, 4096, ...>`（M=0 运行时，N/K 特化）。
- `code_empty.cu`：`<0, 0, 0, ...>`（三者全运行时）。
- 再构造一个 `compiled_dims="mnk"`（全特化）跑同一形状，应得到 `<4096, 4096, 4096, ...>`。
- 把 M 从 4096 换成 8192 再各跑一次：`"nk"` 与 `""` 两种设置的缓存**目录名不变**（因为 N、K 没变，且 M 不参与特化 → 源码不变 → 摘要不变 → 命中缓存）；而 `"mnk"` 设置会**产生一个新的缓存目录**（M 进了源码 → 摘要变了）。这直观体现了「特化维度越多，缓存目录越多、编译越频繁」。

> 待本地验证：实际 `block_m/n/k`、`num_stages` 等由 `get_best_config` 依据形状选出，M 变化可能引起 config 变化从而间接改变源码；若要干净观察「仅 compiled_dims 的影响」，建议固定一个 config 或关注 `SHAPE_*` 三位即可。

#### 4.2.5 小练习与答案

**练习 1**：若 `compiled_dims="n"`，生成的源码里 `SHAPE_M`、`SHAPE_N`、`SHAPE_K` 分别是什么？

**参考答案**：`SHAPE_M=0`（m 不在 `"n"` 里）、`SHAPE_N=真实N值`（n 在里面）、`SHAPE_K=0`（k 不在里面）。

**练习 2**：`set_ignore_compile_dims(True)` 与传 `compiled_dims=""` 效果一样吗？为什么库要提供两个开关？

**参考答案**：对 `get_compiled_dim` 的返回值效果相同（都让所有 `SHAPE_*` 返回 0）。区别在于作用域与语义：`compiled_dims` 是**每次调用**的局部参数，可对不同 GEMM 用不同策略；`ignore_compile_dims` 是 `HeuristicsRuntime` 上的**全局开关**，一次设置影响之后所有 kernel，适合「我想整体关掉特化做对比实验/调试」的场景，无需逐个调用改参数。

---

### 4.3 LaunchRuntime 协作（generate / launch_impl / CRTP）

#### 4.3.1 概念说明

DeepGEMM 有十几个 `*Runtime` 类（SM90/SM100 的 GEMM、MoE、MQA、Einsum……），每个都要做「生成源码 → 编译 → 启动」这三件事。如果把这三步的逻辑在每个类里各写一遍，会有大量重复。

`LaunchRuntime` 是一个 **CRTP 基类**，用模板把「公共流程」抽到基类，「因 kernel 而异的部分」下放到两个钩子：

- `generate_impl(args)`：子类提供，负责拼出 `.cu` 源码（4.1 讲的那个）。
- `launch_impl(kernel, config, args)`：子类提供，负责把参数排成 kernel 需要的顺序并启动。

基类 `generate` / `launch` 则在钩子前后做公共工作：注入 include 哈希、组装 `LaunchConfig`。

#### 4.3.2 核心流程

```
子类 SM90FP8Gemm1D1DRuntime : public LaunchRuntime<SM90FP8Gemm1D1DRuntime>
        │  CRTP：基类在编译期就知道派生类类型，可静态调 Derived::generate_impl
        ▼
LaunchRuntime::generate(args)
   ├─ 调 Derived::generate_impl(args)   ← 子类钩子，返回源码
   ├─ 计算并注入 include 哈希（使头文件改动触发重编译）
   └─ 打印调试信息（DG_JIT_DEBUG）→ 返回最终 code
        │
        ▼  code 交给 compiler->build（u3-l1）
        ▼
LaunchRuntime::launch(kernel_runtime, args)
   ├─ 取出 kernel 句柄、当前 CUDA stream
   ├─ 允许 Python 运行时覆盖 enable_pdl
   ├─ construct_launch_config(...) 组装 grid/block/smem/cluster/PDL
   └─ 调 Derived::launch_impl(kernel, config, args)  ← 子类钩子，真正启动
```

#### 4.3.3 源码精读

子类声明（CRTP 的标准写法）：[sm90_fp8_gemm_1d1d.hpp:L15-L16](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/sm90_fp8_gemm_1d1d.hpp#L15-L16)

```cpp
class SM90FP8Gemm1D1DRuntime final: public LaunchRuntime<SM90FP8Gemm1D1DRuntime> {
```

基类 `generate`：[kernel_runtime.hpp:L122-L136](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/kernel_runtime.hpp#L122-L136)

```cpp
template <typename Args>
static std::string generate(const Args& args) {
    auto code = Derived::generate_impl(args);     // ① 调子类钩子拼源码

    // NOTES: we require that `generate_impl`'s includes never change
    static std::string include_hash;
    if (include_hash.empty())
        include_hash = include_parser->get_hash_value(code);   // ② 递归哈希 include 的头文件
    code = fmt::format("// Includes' hash value: {}\n{}", include_hash, code);  // ③ 注入到源码首行
    if (get_env<int>("DG_JIT_DEBUG"))
        printf("Generated kernel code:\n%s\n", code.c_str());
    return code;
}
```

第 ②③ 步是 u3-l1 提到的「include 哈希」落地点：把 `#include <deep_gemm/...>` 递归解析出的头文件哈希**写进生成的源码首行注释**。由于这段注释是 `code` 的一部分，而 `code` 又进入缓存键（[compiler.hpp:L101](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/compiler.hpp#L101)），所以**一旦设备头文件被改动，哈希变化 → 缓存键变化 → 自动重编译**。注意 `static std::string include_hash` 只在首次计算——注释「`generate_impl`'s includes never change」表明库假设同一 Runtime 类 include 列表是固定的，故哈希算一次即可。

基类 `launch`：[kernel_runtime.hpp:L138-L162](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/kernel_runtime.hpp#L138-L162)

```cpp
template <typename Args>
static void launch(const std::shared_ptr<KernelRuntime>& kernel_runtime, const Args& args) {
    const auto kernel = kernel_runtime->kernel;
    const auto stream = at::cuda::getCurrentCUDAStream();
    LaunchArgs launch_args = args.launch_args;
    launch_args.enable_pdl = device_runtime->get_pdl();   // 允许 Python 运行时覆盖 PDL
    const dim3 grid_dim = {...}, block_dim = {...};
    auto config = construct_launch_config(kernel, stream, launch_args.smem_size,
                                          grid_dim, block_dim, launch_args.cluster_dim, launch_args.enable_pdl);
    // ...
    Derived::launch_impl(kernel, config, args);           // 调子类钩子真正启动
}
```

子类的 `launch_impl` 只剩「按设备 kernel 的形参顺序排好参数」这一件小事：[sm90_fp8_gemm_1d1d.hpp:L66-L75](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/sm90_fp8_gemm_1d1d.hpp#L66-L75)

```cpp
static void launch_impl(const KernelHandle& kernel, const LaunchConfigHandle& config, Args args) {
    DG_CUDA_UNIFIED_CHECK(launch_kernel(kernel, config,
        args.gmem_a_ptr, args.gmem_b_ptr,
        args.grouped_layout,
        args.tensor_map_buffer,
        args.gemm_desc.m, args.gemm_desc.n, args.gemm_desc.k,   // 运行时 shape_m/n/k 始终传入
        args.tensor_map_a_base, args.tensor_map_b_base,
        args.tensor_map_sfa, args.tensor_map_sfb,
        args.tensor_map_cd));
}
```

注意 `args.gemm_desc.m/n/k` 这里被当作运行时参数传入设备 kernel——这正是 4.2 里「无论是否特化，签名都一样」的另一半：**特化值通过模板走，非特化值通过这里走**，两条路同时存在、互不冲突。

> 这也呼应了 u3-l1 的结论：`LaunchRuntime` 用 `generate`、`launch` 两钩子统一各 Runtime 类——`generate` 调子类 `generate_impl` 生成源码并注入 include 哈希，`launch` 组装 `LaunchConfig` 后调子类 `launch_impl`。

#### 4.3.4 代码实践

**实践目标**：在已有 `*Runtime` 类之外，看清 CRTP「基类调子类钩子」的调用时序，而不真正新增 kernel（避免改源码）。

**操作步骤（源码阅读型）**：

1. 在 [kernel_runtime.hpp:L124](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/kernel_runtime.hpp#L124)（`Derived::generate_impl(args)`）与 [kernel_runtime.hpp:L161](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/kernel_runtime.hpp#L161)（`Derived::launch_impl(...)`）两处，沿 `Derived = SM90FP8Gemm1D1DRuntime` 做静态展开。
2. 把整条 `generate → generate_impl → build → launch → launch_impl → launch_kernel` 链路画成调用图，标注每一步分别属于「基类公共逻辑」还是「子类钩子」。
3. 对照另一个子类（如 [csrc/jit_kernels/impls/sm90_fp8_gemm_1d2d.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/sm90_fp8_gemm_1d2d.hpp)），验证它的 `generate_impl` / `launch_impl` 结构与 1d1d 一致、仅模板名与参数顺序不同——这正是 CRTP 抽象带来的复用。

**需要观察的现象 / 预期结果**：

- 你会发现所有 `*Runtime` 子类的「外壳」完全一致：都继承 `LaunchRuntime<Self>`、都提供 `Args` 结构体、`generate_impl`、`launch_impl` 三件套；差异只在「生成哪段 `#include`」「填哪些模板参数」「launch 时参数顺序」。
- 调用图里，「公共逻辑（include 哈希、LaunchConfig 组装、PDL 覆盖）」全在基类，子类只贡献两段薄薄的特化代码。

#### 4.3.5 小练习与答案

**练习 1**：`LaunchRuntime` 为什么用 CRTP（`LaunchRuntime<Derived>`）而不是普通虚函数（`virtual std::string generate_impl() = 0`）？

**参考答案**：CRTP 是**静态多态**，基类在编译期就知道派生类类型，对 `Derived::generate_impl` 的调用会被内联，没有虚函数表查找与间接调用的运行时开销。JIT 的 generate/launch 在每次 GEMM 调用（首次）都会走，且 `generate_impl` 返回的源码会被 `fmt::format` 内联拼接，虚函数会阻碍这种优化。此外 `Args` 类型因 kernel 而异，CRTP 的模板参数能自然携带不同 `Args`，普通虚函数难以优雅表达。

**练习 2**：基类 `generate` 里 `include_hash` 是 `static` 局部变量、只算一次。若某 Runtime 类的 `generate_impl` 在不同调用里 `#include` 了不同头文件，会发生什么？

**参考答案**：会出 bug——`include_hash` 在首次调用后被固化，后续调用即使 `#include` 变了，注入的哈希也不再更新，导致头文件改动不再触发重编译（或反过来误命中缓存）。正因为如此，源码注释明确要求「`generate_impl`'s includes never change」：每个 Runtime 类的 include 列表必须是固定的，可变的只是模板实参。这是使用该基类必须遵守的不变式（invariant）。

---

## 5. 综合实践

把本讲三个模块串起来，完成一次「从形状到实例化」的完整追踪：

**任务**：给定一次 `deep_gemm.fp8_fp4_gemm_nt((a,sfa),(b,sfb), d, compiled_dims="mnk")` 调用（M=N=K=2048），**手工推导**并**运行验证**生成的 `__instantiate_kernel` 源码。

1. **推导**（纸笔）：
   - 查 [gemm.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/gemm.hpp) 确认 `fp8_fp4_gemm_nt` 派发到 `sm90_fp8_gemm_1d1d`（arch_major==9 时）。
   - 由 `compiled_dims="mnk"`，用 `get_compiled_dim`（[runtime_utils.hpp:L22-L31](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/runtime_utils.hpp#L22-L31)）推出 `SHAPE_M=N=K=2048`。
   - 假设 `get_best_config` 选出 `block_m=n=k=128, num_stages=3, cluster=(1,1)`，写出 17 位实例化列表。
2. **验证**（运行）：用 `DG_JIT_DEBUG=1` 或读 `~/.deep_gemm/cache/kernel.sm90_fp8_gemm_1d1d.*/kernel.cu`，核对推导与实际是否一致。
3. **对比**：把 `compiled_dims` 改回默认 `"nk"`，观察 `SHAPE_M` 从 `2048` 变回 `0`，并解释这对「换一个 M 值是否触发重编译」的影响（承接 4.2 的缓存键原理）。
4. **延伸**：调用 `deep_gemm.set_ignore_compile_dims(True)` 后再跑，确认所有 `SHAPE_*` 都变 `0`，并讨论此时编译产物数量与性能的此消彼长。

> 待本地验证：步骤 2 中 `get_best_config` 选出的具体 config 值取决于硬件与启发式，需在真实 SM90 机器上运行确认；推导时应聚焦 `SHAPE_*` 三位与参数顺序的正确性。

---

## 6. 本讲小结

- DeepGEMM 的「代码生成」很**薄**：`generate_impl` 用 `fmt::format` 把 17 个编译期常量填进一段只含 `#include` + `&sm90_fp8_gemm_1d1d_impl<...>` 的 `.cu` 源码，靠**取地址强制模板实例化**让编译器替它展开 kernel。
- 17 个模板实参与设备模板 17 个参数**严格按位置一一对应**；其中 `gemm_type`、`cd_dtype` 需经 `to_string` 转成 C++ 记号，其余整数直接字面量化。
- `compiled_dims`（默认 `"nk"`）用「`0`=运行时、非 `0`=编译期常量」的约定做**选择性特化**：出现在串里的维度（如 N、K）固化为常量优化性能，其余（如易变的 M）保留运行时以避免频繁重编译。
- 设备侧 `shape_x = SHAPE_x != 0 ? SHAPE_x : shape_x` 与 `launch_impl` 始终传入运行时 `m/n/k` 配合，实现「同一签名、按需特化」。
- `LaunchRuntime` 是 **CRTP 基类**，用 `generate`/`launch` 两个公共方法 + `generate_impl`/`launch_impl` 两个子类钩子，统一了十几个 `*Runtime` 类的「生成源码 → 注入 include 哈希 → 编译 → 组装 LaunchConfig → 启动」流程。
- 生成的源码会被写入缓存目录 `~/.deep_gemm/cache/kernel.<name>.<摘要>/kernel.cu`，且 `code` 本身参与缓存键——这是头文件改动能可靠触发重编译的根因。

---

## 7. 下一步学习建议

本讲讲清了「源码怎么生成」，但生成出的源码被 `compiler->build` 交给了 **NVCC 或 NVRTC** 编译——这两条编译后端如何工作、各自的取舍，是下一讲 [u3-l4 NVCC 与 NVRTC 编译器对比](u3-l4-nvcc-vs-nvrtc.md) 的主题。

在继续之前，建议：

- 若想立刻看到「编译与缓存」的全貌，可先跳读 [csrc/jit/compiler.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/compiler.hpp) 的 `build`/`compile` 方法，与 [csrc/jit/cache.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/cache.hpp) 的缓存实现（u3-l3 会深入）。
- 若对 `LaunchConfig` 里的 `cluster_dim`、`enable_pdl` 如何作用到 GPU 感兴趣，可预习 [csrc/jit/handle.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/handle.hpp) 的 `construct_launch_config`（u4-l3 会讲）。
- 想加深对 4.1 实例化技巧的理解，可对照阅读另一个结构相同的 Runtime 类 [sm100_fp8_fp4_gemm_1d1d.hpp](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/sm100_fp8_fp4_gemm_1d1d.hpp)，体会 CRTP 在不同 kernel 间的复用。
