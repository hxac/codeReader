# C++ 绑定与 API 派发层

## 1. 本讲目标

前几讲我们已经知道：用户调用 `deep_gemm.fp8_gemm_nt(...)` 后，请求会进入一个叫 `_C` 的 C++ 扩展模块，再继续往下走到 GPU kernel。但「进入 `_C`」和「真正执行 tensor core 计算」之间，还有一段非常重要的**宿主（CPU）侧胶水代码**——它负责把 Python 对象翻译成 C++ 类型、校验输入合法性、处理各种平凡情况、最后根据当前 GPU 架构把请求派发到正确的实现。

本讲专门拆解这一段「桥梁」。学完后你应该能够：

1. 说清楚 `_C` 这个 Python 扩展模块是怎么用 pybind11 注册出来的，以及 7 个 `register_apis` 各自管什么。
2. 看懂 API 层（`apis/gemm.hpp` 等）统一的「校验 → early_return → 变换 SF → 派发」四步范式，并能解释 `early_return` 在哪些情况下会让计算直接短路。
3. 理解 `device_runtime->get_arch_major()` 返回 `9`（Hopper/SM90）还是 `10`（Blackwell/SM100）是全库派发的核心开关，并掌握 `fp8_fp4_gemm_nt` 如何据此调用 `sm90_fp8_gemm_1d1d` 或 `sm100_fp8_fp4_gemm_1d1d`。

本讲不涉及设备 kernel 内部（那是 u6 的事），也不涉及 JIT 编译细节（那是 u3 的事）——我们只聚焦「Python ↔ C++ 边界」与「按架构派发」这两件事。

## 2. 前置知识

### 2.1 什么是 pybind11

Python 本身是解释执行的，而 DeepGEMM 的核心是 C++/CUDA 代码。要在 Python 里调用 C++ 函数，需要一个「绑定层」。**pybind11** 就是这样一个库：你用宏 `PYBIND11_MODULE(name, m)` 声明一个模块，然后用 `m.def("python名", &c++函数指针, 参数说明...)` 把 C++ 函数注册成 Python 可调用的对象。`m` 是模块对象的引用，类似于一个「注册表」。DeepGEMM 的 `_C` 模块就是用这种方式构建的。

### 2.2 缩放因子（SF）随架构变化（承接 u2-l2）

本讲的派发逻辑会用到上一讲（u2-l2）的关键结论：

- **SM90（arch_major==9）**：缩放因子 SFA/SFB 是 `float32`（`torch::kFloat`）。
- **SM100（arch_major==10）**：缩放因子是打包的 UE8M0（4 个指数打包进一个 `int32`，即 `torch::kInt`）。

因此 API 层不仅用 `arch_major` 派发，还会**用变换后 SF 的 dtype 作为第二道校验**——这等于把架构与数据格式绑死，防止配置错配。

### 2.3 术语速查

| 术语 | 含义 |
|------|------|
| 宿主（host） | CPU 侧，负责调度、校验、启动 |
| 设备（device） | GPU 侧，负责真正的 tensor core 计算 |
| major | 张量的「主维」，即 stride 为 1 的那一维；K-major 表示 K 维连续 |
| arch_major | GPU 计算能力的 major 号，9=Hopper，10=Blackwell |
| early_return | 在平凡情况下（如 M=0 或 K=0）提前返回，跳过 kernel |
| SF | scaling factor，缩放因子 |

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
|------|------|-----------|
| `csrc/python_api.cpp` | 整个 `_C` 扩展的**唯一编译入口** | pybind11 模块注册，聚合 7 个 `register_apis` |
| `csrc/apis/gemm.hpp` | GEMM 系列 API 的**校验 + 派发逻辑** | `fp8_fp4_gemm_nt`、`early_return`、arch 派发 |
| `csrc/apis/runtime.hpp` | 运行时配置 API（`set_num_sms`、`init` 等） | `init` 如何把路径交给 JIT 子系统 |
| `csrc/jit/device_runtime.hpp` | `DeviceRuntime` 单例 | `get_arch_major()` 的实现 |
| `csrc/utils/layout.hpp` | 校验用的小工具函数 | `get_major_type_ab`、`fp8_requires_k_major`、`check_ab_fp8_fp4` |
| `csrc/utils/exception.hpp` | 断言宏 | `DG_HOST_ASSERT` 抛异常而非 abort |
| `csrc/jit_kernels/impls/sm90_fp8_gemm_1d1d.hpp` | SM90 侧宿主 Runtime | 派发的终点之一：`sm90_fp8_gemm_1d1d` |

> 提示：本讲会反复出现「宿主 Runtime 类」与「设备 kernel 模板」的区分。前者在 `csrc/jit_kernels/impls/*.hpp`，后者在 `deep_gemm/include/deep_gemm/impls/*.cuh`。这是 u1-l3 已经建立的命名约定。

## 4. 核心概念与源码讲解

### 4.1 pybind11 注册：`_C` 模块是怎么来的

#### 4.1.1 概念说明

还记得 u1-l2 讲过，`setup.py` 只编译**一个** C++ 文件 `csrc/python_api.cpp` 生成 `deep_gemm._C`。这个文件非常短，它唯一的职责就是：**用 pybind11 声明模块，然后把所有子模块的注册函数挨个调用一遍**。

这是一种典型的「聚合入口 + 分散实现」设计：每个功能域（gemm、attention、mega、einsum、hyperconnection、layout、runtime）各自在一个 `apis/*.hpp` 里实现自己的 `static void register_apis(pybind11::module_& m)`，`python_api.cpp` 只负责把它们串起来。这样添加新功能域时，只需新增一个 `apis/xxx.hpp` 并在 `python_api.cpp` 里加一行调用，互不干扰。

#### 4.1.2 核心流程

1. 编译期宏 `TORCH_EXTENSION_NAME` 被定义为 `_C`（如果没有外部定义）。
2. `PYBIND11_MODULE(_C, m)` 宏展开后，Python `import deep_gemm._C` 时会执行这个宏的函数体。
3. 函数体依次调用 7 个命名空间下的 `register_apis(m)`，把各自的 C++ 函数注册进模块 `m`。
4. 注册完成后，Python 侧就能通过 `deep_gemm._C.fp8_fp4_gemm_nt(...)` 等方式调用。

#### 4.1.3 源码精读

整个 `_C` 模块的入口，干净到只有 7 行实质代码：

[csrc/python_api.cpp:12-28](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/python_api.cpp#L12-L28) —— 定义 `TORCH_EXTENSION_NAME` 为 `_C`，然后用 `PYBIND11_MODULE` 宏声明模块并聚合 7 个 `register_apis`。

```cpp
#ifndef TORCH_EXTENSION_NAME
#define TORCH_EXTENSION_NAME _C
#endif

PYBIND11_MODULE(TORCH_EXTENSION_NAME, m) {
    m.doc() = "DeepGEMM C++ library";
    deep_gemm::attention::register_apis(m);
    deep_gemm::einsum::register_apis(m);
    deep_gemm::hyperconnection::register_apis(m);
    deep_gemm::gemm::register_apis(m);
    deep_gemm::layout::register_apis(m);
    deep_gemm::mega::register_apis(m);
    deep_gemm::runtime::register_apis(m);
}
```

而每个功能域的 `register_apis` 内部，就是一长串 `m.def(...)`。以 GEMM 域为例：

[csrc/apis/gemm.hpp:645-769](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/gemm.hpp#L645-L769) —— `register_apis` 把每个 C++ 静态函数绑定成 Python 函数，并用 `py::arg(...)` 声明参数名与默认值。

其中 `fp8_fp4_gemm_nt` 的注册如下（注意默认值 `compiled_dims="nk"`、`disable_ue8m0_cast=false`）：

[csrc/apis/gemm.hpp:649-654](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/gemm.hpp#L649-L654) —— 把 C++ 函数 `fp8_fp4_gemm_nt` 暴露为同名 Python 函数。

```cpp
m.def("fp8_fp4_gemm_nt", &fp8_fp4_gemm_nt,
      py::arg("a"), py::arg("b"), py::arg("d"),
      py::arg("c") = std::nullopt, py::arg("recipe") = std::nullopt,
      py::arg("recipe_a") = std::nullopt, py::arg("recipe_b") = std::nullopt,
      py::arg("compiled_dims") = "nk",
      py::arg("disable_ue8m0_cast") = false);
```

此外，DeepGEMM 还用 `m.attr(...)` 给同一函数起**别名**——`fp8_gemm_nt` 其实就是 `fp8_fp4_gemm_nt` 的别名（FP8 是 FP8xFP4 的特例）：

[csrc/apis/gemm.hpp:711-714](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/gemm.hpp#L711-L714) —— 用 `m.attr` 建立旧名字到新名字的别名，保持向后兼容。

```cpp
m.attr("fp8_gemm_nt") = m.attr("fp8_fp4_gemm_nt");
m.attr("fp8_gemm_nn") = m.attr("fp8_fp4_gemm_nn");
// ...
```

最后，`runtime::register_apis` 里还注册了一个特殊的 `init` 函数——它在 u1-l4 里被 `import deep_gemm` 时自动调用，把库根目录与 CUDA 目录交给 C++ 侧的 JIT 编译器、运行时加载器和头文件解析器：

[csrc/apis/runtime.hpp:42-48](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/runtime.hpp#L42-L48) —— `_C.init(library_root, cuda_home)` 完成 JIT 子系统的路径初始化。

```cpp
m.def("init", [&](const std::string& library_root_path, const std::string& cuda_home_path_by_python) {
#if DG_TENSORMAP_COMPATIBLE
    Compiler::prepare_init(library_root_path, cuda_home_path_by_python);
    KernelRuntime::prepare_init(cuda_home_path_by_python);
    IncludeParser::prepare_init(library_root_path);
#endif
});
```

> 注意：`init` 受 `DG_TENSORMAP_COMPATIBLE`（CUDA ≥ 12.1）保护。如果 CUDA 版本太老，整个 FP8/TMA 能力都会在编译期被裁掉——这也是为什么 `gemm.hpp` 里大量代码包在 `#if DG_FP8_COMPATIBLE and DG_TENSORMAP_COMPATIBLE` 里。

#### 4.1.4 代码实践

**实践目标**：从 Python 侧直接观察 `_C` 模块注册出来的内容，验证 7 个功能域确实挂载成功。

**操作步骤**：

1. 在已安装 DeepGEMM 的环境里（或阅读源码无法运行时，做「源码阅读型实践」）执行：
   ```python
   import deep_gemm
   names = [n for n in dir(deep_gemm._C) if not n.startswith('__')]
   print(names)
   ```
2. 用关键字过滤观察各功能域：`fp8`、`bf16`、`cublaslt`、`m_grouped`、`k_grouped`、`mega`、`mqa`、`einsum`、`set_`。
3. 验证别名：`deep_gemm._C.fp8_gemm_nt is deep_gemm._C.fp8_fp4_gemm_nt` 应为 `True`。

**需要观察的现象**：`_C` 模块下会出现一长串函数名，正好对应 `gemm.hpp`、`attention.hpp`、`mega.hpp`、`einsum.hpp`、`hyperconnection.hpp`、`layout.hpp`、`runtime.hpp` 里所有 `m.def` / `m.attr` 注册的条目。

**预期结果**：`fp8_gemm_nt is fp8_fp4_gemm_nt` 返回 `True`，证明它们指向同一个底层 C++ 函数指针。若无法在 GPU 环境运行，请改为阅读 `csrc/apis/gemm.hpp:645-718` 自行清点 `m.def`/`m.attr` 条目，并标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 DeepGEMM 把所有 API 散到 7 个 `apis/*.hpp`，而不是全写在 `python_api.cpp` 里？

**参考答案**：为了功能域解耦与可维护性。每个域（gemm、attention、mega…）独立管理自己的函数与校验逻辑，新增域只需新增一个头文件并在 `python_api.cpp` 加一行 `register_apis(m)`；同时 `python_api.cpp` 保持极短，编译更快、职责更清晰。

**练习 2**：`m.attr("fp8_gemm_nt") = m.attr("fp8_fp4_gemm_nt")` 和重新 `m.def("fp8_gemm_nt", &fp8_fp4_gemm_nt, ...)` 有什么区别？

**参考答案**：`m.attr` 直接让两个名字指向**同一个已注册的 Python 函数对象**（`is` 判定为 True），不重复构造、也不需要再写一遍参数签名；而 `m.def` 会再注册一次、需要重复列出所有 `py::arg` 默认值。前者更省事且保证别名与正名行为完全一致。

---

### 4.2 参数校验与 early_return：API 层的统一范式

#### 4.2.1 概念说明

一旦请求进入 C++ 函数（比如 `fp8_fp4_gemm_nt`），API 层不会立刻去启动 kernel，而是先做一套**统一的预处理**。原因有二：第一，GPU kernel 一旦启动开销大且难调试，必须在宿主侧把非法输入挡住；第二，很多调用其实是「平凡情况」（比如某维度为 0），根本不需要算。

DeepGEMM 的每个 GEMM API 几乎都遵循同一个四步范式：

1. **布局校验**：检查 A/B 的 major、C/D 是否行主序。
2. **类型与形状校验**：从张量里抽出 M/N/K，断言三者自洽、dtype 合法。
3. **early_return**：处理空问题或 K=0 等平凡情况，必要时把 C 拷到 D。
4. **变换 SF + 派发**：把缩放因子变换成 kernel 所需布局，再按架构派发。

这里有一个关键设计：**断言失败不是 `abort`，而是抛 C++ 异常**。pybind11 会把异常翻译成 Python 异常，于是用户在 Python 侧能拿到完整的 traceback，而不是进程崩溃。

#### 4.2.2 核心流程

以 `fp8_fp4_gemm_nt` 为例，它的预处理流程（伪代码）：

```
读 major_a, major_b
若 fp8_requires_k_major(): 断言 major_a == K 且 major_b == K
check_major_type_cd(d)              # D 必须行主序
(m,k)   = check_ab_fp8_fp4(a)       # 抽 M,K（FP4 时自动解包）
(n,k')  = check_ab_fp8_fp4(b)       # 抽 N,K
断言 m==m_, n==n_, k==k_           # 与 D 的形状对齐
若 early_return(m,n,k,d,c): return  # 平凡情况短路
(sfa,sfb,...) = transform_sf(...)   # 变换 SF 到 TMA 布局
按 arch_major + sfa.dtype 派发      # → sm90 / sm100 实现
```

`early_return` 的判定逻辑用真值表更直观：

| 条件 | 行为 | 返回值 |
|------|------|--------|
| `m==0` 或 `n==0` | 空问题，什么都不做 | `true`（短路） |
| `k==0` 且 C、D 不同址 | 把 C 拷到 D（或清零） | `true`（短路） |
| `k==0` 且 C、D 同址 | 无需拷贝 | `true`（短路） |
| `k>0` 且有 C 且 C≠D | **先把 C 拷到 D**，再继续算 | `false`（继续） |
| `k>0` 其它情况 | 直接继续算 | `false`（继续） |

注意第 4 行：当带累加（C 非空）且 C 和 D 不是同一块内存时，kernel 假设 C/D 同址，所以**必须在启动 kernel 前先把 C 复制进 D**。这样 kernel 内部就只需做 `D = A@B + D`，不必处理「C/D 不同址」的复杂情况。

#### 4.2.3 源码精读

先看一组校验用的小工具函数，它们位于 `csrc/utils/layout.hpp`，是所有 API 共享的：

[csrc/utils/layout.hpp:21-34](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/utils/layout.hpp#L21-L34) —— `get_major_type_ab` 用「最后一维 stride 是否为 1」判定 K-major 还是 MN-major；`check_major_type_cd` 强制输出必须行主序；`fp8_requires_k_major()` 直接返回 `arch_major == 9`。

```cpp
static cute::UMMA::Major get_major_type_ab(const torch::Tensor& t) {
    major_check(t);
    return t.stride(-1) == 1 ? cute::UMMA::Major::K : cute::UMMA::Major::MN;
}

static void check_major_type_cd(const torch::Tensor& t) {
    major_check(t);
    DG_HOST_ASSERT(t.stride(-1) == 1);   // the library only supports row-major output layouts
}

static bool fp8_requires_k_major() {
    return device_runtime->get_arch_major() == 9;
}
```

> 这一行 `fp8_requires_k_major()` 把 u2-l1 的结论固化成代码：**SM90 的 WGMMA 强制 K-major，所以 FP8 输入必须 K-major；SM100 放宽了，所以不强制**。

再看 FP4 的形状解包。FP4 是「两个元素打包成一个字节」的 e2m1，所以张量的某一维是被压缩了一半的，需要解包：

[csrc/utils/layout.hpp:45-52](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/utils/layout.hpp#L45-L52) —— `check_ab_fp8_fp4` 对 FP8 直接返回形状；对打包 FP4 则按 major 把 K 或 MN 维乘 2 还原真实逻辑大小。

```cpp
static std::tuple<int, int> check_ab_fp8_fp4(const torch::Tensor& ab, ...) {
    auto [mn, k] = get_shape<2>(ab);
    if (ab.scalar_type() != torch::kFloat8_e4m3fn) {
        DG_HOST_ASSERT(ab.scalar_type() == kPackedFP4 and arch_major == 10);
        major == cute::UMMA::Major::K ? (k *= 2) : (mn *= 2);
    }
    return std::make_tuple(mn, k);
}
```

然后是断言宏——它**抛异常**而非终止进程：

[csrc/utils/exception.hpp:29-40](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/utils/exception.hpp#L29-L40) —— `DG_HOST_ASSERT` 失败时抛出 `DGException`，其中 `DG_HOST_UNREACHABLE` 用于表达「理论上不该到达」的分支（如不支持的架构组合）。

```cpp
#define DG_HOST_ASSERT(cond) \
do { if (not (cond)) { throw DGException("Assertion", __FILE__, __LINE__, #cond); } } while (0)

#define DG_HOST_UNREACHABLE(reason) (throw DGException("Assertion", __FILE__, __LINE__, reason))
```

现在看主角 `early_return`：

[csrc/apis/gemm.hpp:19-46](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/gemm.hpp#L19-L46) —— 平凡问题短路：M/N 为 0 直接返回；K 为 0 时按需把 C 拷到 D；带累加且 C≠D 时提前把 C 复制进 D。

```cpp
static bool early_return(const int& m, const int &n, const int& k,
                         const torch::Tensor& d, const std::optional<torch::Tensor>& c) {
    if (m == 0 or n == 0) return true;          // 空问题

    const bool is_cd_same = c.has_value() and c->data_ptr() == d.data_ptr();
    if (is_cd_same)
        DG_HOST_ASSERT(c->sizes() == d.sizes() and c->strides() == d.strides());
    DG_HOST_ASSERT(d.scalar_type() == torch::kBFloat16 or d.scalar_type() == torch::kFloat);
    if (c.has_value()) { check_major_type_cd(c.value()); DG_HOST_ASSERT(d.scalar_type() == c.value().scalar_type()); }

    if (k == 0) {                                 // 无累加维度
        if (not is_cd_same) c.has_value() ? d.copy_(c.value()) : d.zero_();
        return true;
    }
    if (c.has_value() and not is_cd_same)          // kernel 假设 C/D 同址
        d.copy_(c.value());
    return false;
}
```

最后看 `fp8_fp4_gemm_nt` 是怎么把四步范式串起来的（先看校验 + early_return 部分，派发部分留到 4.3）：

[csrc/apis/gemm.hpp:82-107](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/gemm.hpp#L82-L107) —— 典型的「布局校验 → 类型形状校验 → early_return → 变换 SF」四步。

```cpp
static void fp8_fp4_gemm_nt(...) {
    const auto major_a = get_major_type_ab(a.first);
    const auto major_b = get_major_type_ab(b.first);
    if (fp8_requires_k_major()) {                       // SM90 强制 K-major
        DG_HOST_ASSERT(major_a == cute::UMMA::Major::K);
        DG_HOST_ASSERT(major_b == cute::UMMA::Major::K);
    }
    check_major_type_cd(d);                             // D 行主序

    const auto arch_major = device_runtime->get_arch_major();
    const auto [m , k ] = check_ab_fp8_fp4(a.first, major_a, arch_major);
    const auto [n , k_] = check_ab_fp8_fp4(b.first, major_b, arch_major);
    const auto [m_, n_] = get_shape<2>(d);
    DG_HOST_ASSERT(m == m_ and n == n_ and k == k_);
    DG_HOST_ASSERT(d.scalar_type() == torch::kBFloat16 or d.scalar_type() == torch::kFloat);

    if (early_return(m, n, k, d, c)) return;            // 平凡情况短路

    const auto [sfa, sfb, gran_k_a, gran_k_b] =
        layout::transform_sf_pair_into_required_layout(...);   // 变换 SF
    // ... 派发（见 4.3）
}
```

#### 4.2.4 代码实践

**实践目标**：亲手触发 `early_return` 的两条短路路径，并观察断言失败如何变成 Python 异常。

**操作步骤**（需要 GPU 环境；无环境请做源码阅读型实践）：

1. **触发 K=0 短路**：构造一个 `k=0` 的合法形状，传入一个非空 `c`，观察 `d` 是否被 `c` 覆盖。
   ```python
   import torch, deep_gemm
   # M=16, N=32, K=0 —— 注意 SF 形状需合法，这里仅示意
   d = torch.zeros(16, 32, dtype=torch.bfloat16, device='cuda')
   c = torch.full((16, 32), 7., dtype=torch.bfloat16, device='cuda')
   # 用合法的 (a, sfa), (b, sfb) 元组调用，k=0 时应触发 early_return
   # 预期：调用后 d 被 c 覆盖为全 7
   ```
2. **触发断言异常**：故意传一个列主序（非行主序）的 `d`，观察报错。
   ```python
   d_bad = torch.empty(16, 32, dtype=torch.bfloat16, device='cuda').t().contiguous().t()
   # 预期：抛出 DGException -> Python RuntimeError，信息含 "Assertion error (csrc/apis/gemm.hpp:NN)"
   ```

**需要观察的现象**：第 1 步调用后 `d` 全为 7（来自 `d.copy_(c)`），且**没有任何 kernel 启动**；第 2 步直接抛异常而非 segfault，且异常信息里带有源码文件名和行号。

**预期结果**：`early_return` 让平凡调用零开销返回；非法布局被 `DG_HOST_ASSERT` 挡在宿主侧。无 GPU 时请阅读 [csrc/apis/gemm.hpp:19-46](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/gemm.hpp#L19-L46) 并手动推演上述两种输入会命中哪一行，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `early_return` 在 `k>0` 且 `c≠d` 时要提前 `d.copy_(c)`，而不是让 kernel 内部处理？

**参考答案**：DeepGEMM 的设备 kernel 统一假设 C 和 D 是同一块内存（即 `D = A@B + D`）。如果让 kernel 处理「C/D 不同址」会显著增加设备侧复杂度与寄存器/带宽开销。因此在宿主侧用一次 `copy_` 把 C 搬进 D，让 kernel 逻辑保持最简。

**练习 2**：`DG_HOST_ASSERT(d.scalar_type() == torch::kBFloat16 or d.scalar_type() == torch::kFloat)` 说明输出 D 只能是哪两种类型？为什么不是 FP8？

**参考答案**：D 只能是 BF16 或 FP32。因为 GEMM 的累加是在高精度下进行的，输出需要足够动态范围；FP8 范围太窄无法直接承载累加结果，所以输出必须是 BF16/FP32，再由上层按需量化。

---

### 4.3 架构派发：arch_major 9 vs 10

#### 4.3.1 概念说明

走到派发这一步，输入已经合法、平凡情况已短路、SF 也已变换好。最后的问题是：**该调用哪个具体实现？** 答案取决于当前 GPU 是 Hopper（SM90）还是 Blackwell（SM100）。

全库的派发开关就一个函数：`device_runtime->get_arch_major()`，它返回 `cudaDeviceProp.major`——`9` 表示 SM90，`10` 表示 SM100。这个值在进程首次查询设备属性时被缓存，之后所有 API 都读这个缓存值。

派发时还有**第二道保险**：变换后 SF 的 dtype。如前所述，SM90 的 SF 是 `float32`、SM100 的 SF 是打包 `int32`。所以 `fp8_fp4_gemm_nt` 用 `arch_major` **和** `sfa.scalar_type()` 两个条件共同判定，任一不匹配就 `DG_HOST_UNREACHABLE`。这等于把「架构 + 数据格式」绑定校验，杜绝错配。

#### 4.3.2 核心流程

派发的判定树（以 `fp8_fp4_gemm_nt` 为例）：

```
arch_major == 9 且 sfa.dtype == Float ?
   是 → 再看 gran_n：
           gran_n == 1 → sm90_fp8_gemm_1d1d   (1D 缩放布局)
           gran_n != 1 → sm90_fp8_gemm_1d2d  (2D 缩放布局)
arch_major == 10 且 sfa.dtype == Int ?
   是 → sm100_fp8_fp4_gemm_1d1d
都不是 → DG_HOST_UNREACHABLE("Unsupported architecture or scaling factor types")
```

> 这里 `gran_n` 是缩放因子在 N 维的粒度（recipe 的第二维）。`gran_n==1` 表示每行一个 SF（1D 布局），否则是 2D 块状缩放——SM90 据此再细分到 `1d1d` 或 `1d2d` 两套 kernel。SM100 统一走 `1d1d`。

派发的**终点**是宿主 Runtime 函数，比如 `sm90_fp8_gemm_1d1d`（定义在 `csrc/jit_kernels/impls/sm90_fp8_gemm_1d1d.hpp`）。它本身还不在 GPU 上——它负责构造 `GemmDesc`、调启发式选配置、建 TMA 描述符，最后通过 `SM90FP8Gemm1D1DRuntime::launch(...)` 触发 JIT 编译与 kernel 启动（这部分是 u3/u4 的内容）。

顺带一提，nn/tn/tt 三种布局并不是三套独立 kernel，而是**先转置再转发给 nt**（承接 u2-l1）：

```
fp8_fp4_gemm_nn(a,b,...)  = fp8_fp4_gemm_nt(a, {b.T, sf_b.T}, ...)
fp8_fp4_gemm_tn(a,b,...)  = fp8_fp4_gemm_nt({a.T,sf_a.T}, {b.T,sf_b.T}, ...)
fp8_fp4_gemm_tt(a,b,...)  = fp8_fp4_gemm_nt({a.T,sf_a.T}, b, ...)
```

注意转置时**数据张量与其 SF 必须一起转置**，保持逐块对应。

#### 4.3.3 源码精读

先看派发开关本身。`get_arch_major` 只是 `cudaDeviceProp.major` 的直读：

[csrc/jit/device_runtime.hpp:83-101](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/device_runtime.hpp#L83-L101) —— `get_arch_pair` 返回缓存的 `(major, minor)`；`get_arch_major` 返回 `major`（9 或 10）；`get_arch` 还会拼出 `90a`/`100a`/`100f` 这样的 arch 字符串供 NVCC/NVRTC 使用。

```cpp
std::pair<int, int> get_arch_pair() {
    const auto prop = get_prop();        // 首次访问时 cudaGetDeviceProperties 并缓存
    return {prop->major, prop->minor};
}
int get_arch_major() {
    return get_arch_pair().first;        // 9 = Hopper, 10 = Blackwell
}
```

而 `device_runtime` 是一个**惰性初始化的全局单例**：

[csrc/jit/device_runtime.hpp:136](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/device_runtime.hpp#L136) —— `device_runtime` 是 `LazyInit<DeviceRuntime>` 全局对象，首次解引用时才构造 `DeviceRuntime`（创建 cuBLASLt handle、分配 workspace）。

```cpp
static auto device_runtime = LazyInit<DeviceRuntime>([](){ return std::make_shared<DeviceRuntime>(); });
```

现在看 `fp8_fp4_gemm_nt` 的派发块——双条件判定 + 三分支：

[csrc/apis/gemm.hpp:109-124](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/gemm.hpp#L109-L124) —— 用 `arch_major` + `sfa.scalar_type()` 双重判定派发到 SM90（再按 gran_n 选 1d1d/1d2d）或 SM100。

```cpp
// Dispatch into different implements
if (arch_major == 9 and sfa.scalar_type() == torch::kFloat) {
    const int gran_n = recipe.has_value() ? std::get<1>(recipe.value()) : std::get<0>(recipe_b.value());
    if (gran_n == 1) {
        sm90_fp8_gemm_1d1d(a.first, sfa, b.first, sfb, c, d, m, n, k, major_a, major_b, compiled_dims);
    } else {
        const auto major_sfb = get_major_type_ab(sfb);
        sm90_fp8_gemm_1d2d(a.first, sfa, b.first, sfb, c, d, m, n, k, major_a, major_b, major_sfb, compiled_dims);
    }
} else if (arch_major == 10 and sfa.scalar_type() == torch::kInt) {
    sm100_fp8_fp4_gemm_1d1d(a.first, sfa, b.first, sfb, c, d, m, n, k, gran_k_a, gran_k_b,
                            major_a, major_b, compiled_dims);
} else {
    DG_HOST_UNREACHABLE("Unsupported architecture or scaling factor types");
}
```

派发的终点之一是 `sm90_fp8_gemm_1d1d`，它是个宿主包装函数——构造描述符、选配置、最后启动：

[csrc/jit_kernels/impls/sm90_fp8_gemm_1d1d.hpp:78-99](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/sm90_fp8_gemm_1d1d.hpp#L78-L99) —— 构造 `GemmDesc`，调 `get_best_config<SM90ArchSpec>(desc)` 选最优配置，再建 TMA 描述符并启动（后续行省略）。

```cpp
static void sm90_fp8_gemm_1d1d(const torch::Tensor& a, const torch::Tensor& sfa, ...) {
    DG_HOST_ASSERT(c.has_value() and d.scalar_type() == torch::kFloat);
    DG_HOST_ASSERT(major_a == cute::UMMA::Major::K and major_b == cute::UMMA::Major::K);

    const auto desc = GemmDesc { .gemm_type = GemmType::Normal, .kernel_type = KernelType::Kernel1D1D,
        .m = m, .n = n, .k = k, .num_groups = 1, ...,
        .num_sms = device_runtime->get_num_sms(),
        .tc_util = device_runtime->get_tc_util(), .compiled_dims = compiled_dims };
    const auto config = get_best_config<SM90ArchSpec>(desc);   // 启发式选配置（u5）
    // ... 构造 TMA 描述符，最后 SM90FP8Gemm1D1DRuntime::launch(...)
}
```

而这个 Runtime 类继承了 CRTP 基类 `LaunchRuntime`，把「生成源码 → 编译 → 启动」的共性逻辑抽到基类，子类只提供 `generate_impl`（用 `fmt::format` 把模板参数填进设备 `.cuh`）和 `launch_impl`（真正发起 launch）：

[csrc/jit_kernels/impls/sm90_fp8_gemm_1d1d.hpp:33-64](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/sm90_fp8_gemm_1d1d.hpp#L33-L64) —— `generate_impl` 用 `fmt::format` 把 BLOCK_M/N/K、num_stages、num_sms 等编译期常量填入设备模板，生成一段 `#include <deep_gemm/impls/sm90_fp8_gemm_1d1d.cuh>` 的 `.cu` 源码。

```cpp
static std::string generate_impl(const Args& args) {
    return fmt::format(R"(
#include <deep_gemm/impls/sm90_fp8_gemm_1d1d.cuh>
using namespace deep_gemm;
static void __instantiate_kernel() {{
    auto ptr = reinterpret_cast<void*>(&sm90_fp8_gemm_1d1d_impl<
        {}, {}, {},     // m, n, k（由 get_compiled_dim 决定是否特化）
        ...>);
}};
})", get_compiled_dim(args.gemm_desc.m, 'm', args.gemm_desc.compiled_dims), ...);
}
```

最后，nn/tn/tt 的转置转发——注意 SF 一起转置：

[csrc/apis/gemm.hpp:126-164](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/gemm.hpp#L126-L164) —— `nn/tn/tt` 都是先对 A 和/或 B（连同其 SF）做零拷贝 `.transpose()`，再调用 `fp8_fp4_gemm_nt`。

```cpp
static void fp8_fp4_gemm_nn(...) {
    fp8_fp4_gemm_nt(a, {b.first.transpose(0, 1), b.second.transpose(0, 1)}, ...);
}
static void fp8_fp4_gemm_tn(...) {
    fp8_fp4_gemm_nt({a.first.transpose(0, 1), a.second.transpose(0, 1)},
                    {b.first.transpose(0, 1), b.second.transpose(0, 1)}, ...);
}
```

> 这种「转置即转发」的设计意味着：**nt 是唯一真正的原生 kernel，nn/tn/tt 只是薄包装**（u2-l1 的结论在此得到代码印证）。`.transpose()` 在 PyTorch 里是零拷贝的元数据操作，只改 stride 不搬数据。

#### 4.3.4 代码实践

**实践目标**：完整跟踪一次 `fp8_fp4_gemm_nt` 从 pybind11 注册到架构派发的调用链，并画出调用图。这是本讲的核心实践任务。

**操作步骤**（源码阅读型实践，可在任意环境完成）：

1. 从 [csrc/python_api.cpp:24](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/python_api.cpp#L24) 的 `deep_gemm::gemm::register_apis(m)` 出发。
2. 跳到 [csrc/apis/gemm.hpp:649](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/gemm.hpp#L649) 的 `m.def("fp8_fp4_gemm_nt", &fp8_fp4_gemm_nt, ...)`，确认它绑定到同文件的 `static void fp8_fp4_gemm_nt(...)`（第 73 行）。
3. 沿 `fp8_fp4_gemm_nt` 函数体读：校验（83-99 行）→ `early_return`（102 行）→ `transform_sf_pair_into_required_layout`（106 行）→ 派发块（110-123 行）。
4. 在派发块里，假设 `arch_major==9`：进入 [csrc/apis/gemm.hpp:113](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/gemm.hpp#L113) 的 `sm90_fp8_gemm_1d1d(...)`。
5. 跳到 [csrc/jit_kernels/impls/sm90_fp8_gemm_1d1d.hpp:78](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit_kernels/impls/sm90_fp8_gemm_1d1d.hpp#L78)，观察它构造 `GemmDesc`、调 `get_best_config`、最后 `SM90FP8Gemm1D1DRuntime::launch`。
6. 再假设 `arch_major==10`：进入 [csrc/apis/gemm.hpp:119](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/gemm.hpp#L119) 的 `sm100_fp8_fp4_gemm_1d1d(...)`（在 `csrc/jit_kernels/impls/sm100_fp8_fp4_gemm_1d1d.hpp`）。

**需要观察的现象**：调用链在 `apis/gemm.hpp` 处分叉——SM90 与 SM100 走向两个不同的宿主 Runtime 文件，但二者都最终汇聚到 `LaunchRuntime::launch`（[csrc/jit/kernel_runtime.hpp:139](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/jit/kernel_runtime.hpp#L139)）这个统一启动入口。

**预期结果**：画出如下调用图（Mermaid 风格文字描述）：

```
deep_gemm.fp8_fp4_gemm_nt  (Python)
        │  pybind11
        ▼
gemm::register_apis  →  m.def("fp8_fp4_gemm_nt", &fp8_fp4_gemm_nt)        [gemm.hpp:649]
        │
        ▼
fp8_fp4_gemm_nt(a,b,d,c,...)                                             [gemm.hpp:73]
   ├─ get_major_type_ab / check_major_type_cd / check_ab_fp8_fp4          [utils/layout.hpp]
   ├─ early_return(...)                                                  [gemm.hpp:19]
   ├─ layout::transform_sf_pair_into_required_layout(...)                [apis/layout.hpp]
   └─ arch_major==9 ? sm90_fp8_gemm_1d1d(...)   [sm90_fp8_gemm_1d1d.hpp:78]
        arch_major==10? sm100_fp8_fp4_gemm_1d1d(...) [sm100_fp8_fp4_gemm_1d1d.hpp]
              │
              ▼
        *Runtime::launch  →  LaunchRuntime::launch                       [kernel_runtime.hpp:139]
              ├─ Derived::generate_impl  (fmt::format 生成 .cu)          [sm90_fp8_gemm_1d1d.hpp:33]
              └─ Derived::launch_impl    (cuLaunchKernel)                [sm90_fp8_gemm_1d1d.hpp:66]
```

如果手边有 GPU，可加设 `DG_JIT_DEBUG=1` 运行一次 `fp8_fp4_gemm_nt`，控制台会打印 `Generated kernel code:` 和 `Launch kernel with {...} x ...`，可直接对照上图验证（「待本地验证」其精确的 grid/block 取值）。

#### 4.3.5 小练习与答案

**练习 1**：派发块为什么用 `arch_major == 9 and sfa.scalar_type() == torch::kFloat` 两个条件，而不是只看 `arch_major`？

**参考答案**：双重条件把「架构」与「数据格式」绑死校验。理论上 SM90 必然产生 float SF、SM100 必然产生 int（打包 UE8M0）SF，但用 `and` 显式断言二者一致，能在 SF 变换出错或 `disable_ue8m0_cast` 误用时立刻 `DG_HOST_UNREACHABLE`，而不是把错误数据喂给 kernel 导致难以诊断的数值错误。

**练习 2**：若在一台 SM90（Hopper）机器上调用 `fp8_fp4_gemm_nt` 并设置 `gran_n=4`（非 1），会走哪个 kernel？为什么 SM90 需要区分 1d1d 与 1d2d？

**参考答案**：会走 `sm90_fp8_gemm_1d2d`（[gemm.hpp:114-117](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/gemm.hpp#L114-L117)）。`gran_n==1` 表示缩放因子在 N 维是逐行的「1D」布局，可直接用 `1d1d` kernel；`gran_n>1` 表示 SF 是 N×K 的「2D」块状布局，需要 `1d2d` kernel 用不同的 TMA 描述符与加载方式处理 SFB，所以 SM90 据此再细分两套 kernel。SM100 则统一走 `1d1d`。

**练习 3**：`nn`、`tn`、`tt` 三个函数各自对 A 和 B 做了什么转置？

**参考答案**：`nn` 只转置 B（连同 SFB）；`tn` 同时转置 A（含 SFA）和 B（含 SFB）；`tt` 只转置 A（含 SFA）。三者最终都转发给 `fp8_fp4_gemm_nt`，且每次转置都让数据张量与其 SF 同步转置以保持逐块对应。

## 5. 综合实践

**任务**：为 DeepGEMM 的「API 派发层」编写一份一页纸的**派发规约文档**，要求覆盖三种入口（FP8/Fp4、BF16、cuBLASLt）在 NT 布局下的完整派发路径，并能据此预测任意 `(arch_major, dtype)` 组合会落到哪个实现。

**具体步骤**：

1. 阅读三个 NT 函数：[fp8_fp4_gemm_nt](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/gemm.hpp#L73-L124)、[bf16_gemm_nt](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/gemm.hpp#L404-L438)、[cublaslt_gemm_nt](https://github.com/deepseek-ai/DeepGEMM/blob/54e22612409371d6364144b69086735beb54e98b/csrc/apis/gemm.hpp#L611-L628)。
2. 制作一张派发表：

   | 入口 | arch_major==9 | arch_major==10 | 是否经 early_return | 是否变换 SF |
   |------|---------------|----------------|---------------------|-------------|
   | fp8_fp4_gemm_nt | sm90_fp8_gemm_1d1d/1d2d | sm100_fp8_fp4_gemm_1d1d | 是 | 是 |
   | bf16_gemm_nt | sm90_bf16_gemm | sm100_bf16_gemm | 是 | 否 |
   | cublaslt_gemm_nt | cublaslt_gemm | cublaslt_gemm | 是 | 否 |

3. 思考并回答：为什么 cuBLASLt 路径**不分架构**派发到不同函数？（提示：cuBLASLt 是 NVIDIA 官方库，内部自带架构适配。）
4. 验证你的派发表：对照源码确认每一格的函数名与「是否变换 SF」两列正确。

**预期产出**：一张能直接贴进团队 Wiki 的派发速查表，外加一段对「为什么 FP8 路径最复杂（要变换 SF + 按 gran_n 再分叉）、而 cuBLASLt 路径最简单」的解释。

## 6. 本讲小结

- **`_C` 模块由 pybind11 注册**：`csrc/python_api.cpp` 是唯一编译入口，用 `PYBIND11_MODULE` 聚合 7 个功能域的 `register_apis(m)`；每个域用 `m.def` 绑定函数、用 `m.attr` 建立别名（如 `fp8_gemm_nt` ⇄ `fp8_fp4_gemm_nt`）。
- **API 层遵循统一的四步范式**：布局校验 → 类型形状校验 → `early_return`（处理 M/N=0、K=0、C/D 不同址拷贝）→ 变换 SF → 派发。
- **断言抛异常而非 abort**：`DG_HOST_ASSERT` / `DG_HOST_UNREACHABLE` 抛 `DGException`，pybind11 翻译成 Python 异常，用户能拿到带文件名行号的 traceback。
- **派发开关是 `device_runtime->get_arch_major()`**：返回 9（SM90）或 10（SM100），源自缓存的 `cudaDeviceProp.major`；`device_runtime` 是惰性初始化的全局单例。
- **派发用「架构 + SF dtype」双条件**：SM90 配 float SF 走 `sm90_fp8_gemm_1d1d/1d2d`，SM100 配 int(UE8M0) SF 走 `sm100_fp8_fp4_gemm_1d1d`，错配即 `DG_HOST_UNREACHABLE`。
- **nt 是唯一原生 kernel，nn/tn/tt 仅转置转发**：转置时数据张量与 SF 必须同步转置，`.transpose()` 是零拷贝元数据操作。

## 7. 下一步学习建议

本讲停在了「派发到宿主 Runtime 函数（如 `sm90_fp8_gemm_1d1d`）」这一层。接下来有三个自然的深入方向：

1. **向 JIT 编译深入（u3）**：本讲末尾看到 `generate_impl` 用 `fmt::format` 生成 `.cu` 源码、`LaunchRuntime::launch` 触发编译。u3-l1/u3-l2 会完整拆解「签名 → 缓存查找 → 编译 → 原子重命名 → 加载 cubin」的全流程，建议先读 `csrc/jit/compiler.hpp` 与 `csrc/jit/kernel_runtime.hpp`。
2. **向启发式选配置深入（u5）**：派发终点里的 `get_best_config<SM90ArchSpec>(desc)` 决定了用多大的 BLOCK_M/N/K、几级流水线。u5 会讲解 `GemmDesc`/`GemmConfig` 数据结构与布局候选评估。
3. **向设备 kernel 深入（u6）**：被 `#include` 的设备模板 `deep_gemm/include/deep_gemm/impls/sm90_fp8_gemm_1d1d.cuh` 才是真正跑在 tensor core 上的代码，u6-l1 会从共享内存划分与 TMA/math 线程分工讲起。

如果只想快速巩固本讲，建议立刻动手完成第 5 节的派发表实践——它能把本讲三个最小模块的知识一次性串起来。
