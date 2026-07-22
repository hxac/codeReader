# 目录结构与内核流水线总览

## 1. 本讲目标

上一讲（u1-l1）我们弄清了 sgl-kernel 是什么、怎么装。本讲要回答一个更实际的问题：

> **「我听说 sgl-kernel 里有个叫 `rmsnorm` 的算子，它的代码到底散落在哪几个文件夹里？我从 Python 调用它，中间经过了哪些环节？」**

读完本讲，你应当能够：

1. 说出 `csrc/`、`include/`、`python/`、`tests/`、`benchmark/` 五大目录各自的职责。
2. 用一句话描述「一个算子从 CUDA 代码到 Python 可调用」的完整六步流水线。
3. 拿到任意一个算子名（例如 `rmsnorm`、`silu_and_mul`），能在源码树里准确定位它在流水线每个环节对应的文件与行号。

这条六步流水线是后续所有讲义的主线索。后面每一篇讲义，本质上都是在深入这六步中的某一步。

---

## 2. 前置知识

本讲假设你已经读过 u1-l1，知道三件事：

- 源码目录叫 `sgl-kernel/`，PyPI 包名是 `sglang-kernel`，Python 里 `import sgl_kernel`。
- 这是一个用 **CMake + NVCC** 把 CUDA 代码编译成 Python 扩展（`.so`）的工程。
- 它服务于 LLM/VLM 推理，提供大量定制 GPU 算子。

你还需要几个最基础的概念（不熟悉的术语下面都会再用源码佐证）：

- **算子（operator / op）**：一个完成特定数值计算的函数，例如「对一行向量做归一化」。在 sgl-kernel 里，算子既有 CUDA 实现也有 Python 入口。
- **扩展（extension）**：编译产物是一个 `.so` 动态库，Python 用 `import` 加载它。一个扩展里可以注册成百上千个算子。
- **PyTorch Custom Op**：PyTorch 允许 C++ 注册自定义算子，注册后 Python 侧用 `torch.ops.<库名>.<算子名>` 调用。sgl-kernel 用的库名就是 `sgl_kernel`。

> 小提示：如果你完全没接触过「C++ 扩展」这个概念，可以把它类比成「用 C/CUDA 写一个性能更高的函数，再把它包装成 Python 函数」。本讲关心的是「包装链路长什么样」，不涉及 CUDA 编程细节。

---

## 3. 本讲源码地图

本讲会反复打开下面这几个文件，建议你先在编辑器里把它们打开：

| 文件 | 在流水线中的角色 | 本讲用途 |
| --- | --- | --- |
| `README.md` | 项目说明 + 贡献指南 | 抄下官方六步贡献流程作为总纲 |
| [`python/sgl_kernel/__init__.py`](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/python/sgl_kernel/__init__.py) | Python 包入口，对外导出算子 | 看算子如何被 `import` 暴露 |
| [`csrc/common_extension.cc`](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/csrc/common_extension.cc) | C++ 扩展注册文件，把算子注册进 PyTorch | 看 `m.def` / `m.impl` 怎么写 |
| [`include/sgl_kernel_ops.h`](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/include/sgl_kernel_ops.h) | C++ 头文件，声明所有算子的函数原型 | 看算子的 C++ 签名 |
| [`csrc/elementwise/fused_add_rms_norm_kernel.cu`](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/csrc/elementwise/fused_add_rms_norm_kernel.cu) | CUDA 源码，算子的真正实现 | 看算子的 GPU 计算本体 |
| [`python/sgl_kernel/elementwise.py`](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/python/sgl_kernel/elementwise.py) | Python 包装层 | 看 Python 如何转发到 `torch.ops.sgl_kernel` |
| `CMakeLists.txt` | 构建脚本 | 看 `.cu` 源文件如何被纳入编译 |

> 记住一条对应关系：**算子名相同，但它在不同文件里出现的是「同一条链路上的不同环节」**。本讲的核心就是把这串环节串起来。

---

## 4. 核心概念与源码讲解

本讲分三个最小模块：

- **4.1 目录职责划分**：每个文件夹管什么。
- **4.2 六步内核流水线总览**：算子从 CUDA 到 Python 的六个环节。
- **4.3 链路定位练习**：用 `rmsnorm` 走一遍真实链路。

### 4.1 目录职责划分

#### 4.1.1 概念说明

一个「既要高性能（CUDA）又要好用（Python）」的算子库，天然要把代码分成几个层次。sgl-kernel 用目录来划分这些层次。你可以把它想成一个工厂：

- `csrc/` 是**车间**：放 CUDA/C++ 源码，真正干活的机器都在这里。
- `include/` 是**图纸柜**：放头文件，声明「车间里有哪些机器、接口长什么样」。
- `python/` 是**销售前台**：放 Python 包装，客户（用户代码）只在这里下单。
- `tests/` 是**质检车间**：验证算子算得对不对。
- `benchmark/` 是**性能测试台**：测算子跑得快不快。

#### 4.1.2 核心流程

五个目录按「从底层硬件到顶层用户」的方向排列：

```text
[硬件 GPU]
   ▲
   │  CUDA 代码 (.cu / .cc / .cuh)
[ csrc/ ]  ──────────────────────────┐
   ▲                                  │
   │  头文件声明函数原型               │  这些是「实现层」
[ include/ ] ─────────────────────────┘
   ▲
   │  Python 包装 + __init__ 导出
[ python/sgl_kernel/ ]  ── 实现层与用户层之间的「桥」
   ▲
   │  用户调用
[ 你的训练/推理代码 ]

横向配套：
[ tests/ ]     算得对不对（正确性）
[ benchmark/ ] 跑得快不快（性能）
```

额外还有两个「基础设施」目录，本讲只做了解，后续讲义会展开：

- `cmake/`：放第三方库的 CMake 模块（如 `flashmla.cmake`）。
- 根目录的 `CMakeLists.txt`、`Makefile`、`build.sh`、`pyproject.toml`：构建系统的总开关（详见 u1-l3）。

#### 4.1.3 源码精读

我们用 `csrc/` 的内部子目录来印证「按功能族分车间」这个判断。在仓库根目录执行 `ls csrc/`，你会看到按算子功能分的子目录：

```text
csrc/
├── allreduce/          # 自定义集合通信（allreduce）
├── attention/          # 注意力算子（MLA 等）
├── elementwise/        # 逐元素与归一化算子（rmsnorm 在这里）
├── gemm/               # 矩阵乘与量化 GEMM
├── moe/                # 混合专家（MoE）算子
├── mamba/              # 状态空间模型（SSM）卷积
├── speculative/        # 投机解码
├── ...                 # 还有 cpu/ metal/ musa/ 等其它后端
├── common_extension.cc          # 「主车间」的注册总表
├── common_extension_rocm.cc     # ROCm(AMD) 后端的注册总表
└── common_extension_musa.cc     # MUSA(摩尔线程) 后端的注册总表
```

注意：**`csrc/` 里既有算子的实现（`.cu`），也有「注册总表」`*_extension.cc`**。`common_extension.cc` 就是把所有算子集中注册到 PyTorch 的那个文件——它是 4.2 流水线里「torch 扩展注册」这一步的落点。

`include/` 则是「图纸柜」，关键文件有：

```text
include/
├── sgl_kernel_ops.h         # 所有算子的 C++ 函数声明（最重要）
├── utils.h                  # 校验宏、dtype 分派宏（车间通用工具）
├── sgl_kernel_torch_shim.h  # 类型适配（int -> int64_t）
└── sgl_flash_kernel_ops.h   # flash 扩展专用声明
```

`python/sgl_kernel/` 是「销售前台」，里面也是按算子族分文件的，文件名和 `csrc/` 子目录基本对应（`elementwise.py`、`gemm.py`、`moe.py`、`attention.py`……），外加一个总入口 `__init__.py`。

#### 4.1.4 代码实践

**实践目标**：用眼睛把五个目录「对号入座」。

**操作步骤**：

1. 在仓库根目录依次执行 `ls csrc/`、`ls include/`、`ls python/sgl_kernel/`、`ls tests/`、`ls benchmark/`。
2. 数一数 `tests/` 和 `benchmark/` 里各有几个文件，观察它们的命名规律。
3. 在 `tests/` 里找 `test_norm.py`，在 `benchmark/` 里找 `bench_rmsnorm.py`——它们的命名正好对应同一个算子族（归一化）。

**需要观察的现象**：`csrc/elementwise/`、`python/sgl_kernel/elementwise.py`、`tests/test_norm.py`、`benchmark/bench_rmsnorm.py` 都围绕「归一化」这一族算子，文件名之间存在明显的呼应关系。

**预期结果**：你会感受到「同一个功能族，在五个目录里各有一个对应的文件/子目录」。

> 注意：本实践只读不写，不修改任何源码。

#### 4.1.5 小练习与答案

**练习 1**：如果我想新增一个「矩阵乘」算子，它的 CUDA 代码应该放在 `csrc/` 下的哪个子目录？

**参考答案**：`csrc/gemm/`。因为矩阵乘（GEMM）相关的算子（如 `fp8_scaled_mm`、`gptq_gemm`）都按功能族归在 `gemm/` 子目录下。

**练习 2**：`include/sgl_kernel_ops.h` 和 `csrc/common_extension.cc` 都会提到算子名（比如 `rmsnorm`），它们各自的「身份」是什么？

**参考答案**：`sgl_kernel_ops.h` 里是算子的 **C++ 函数声明（原型/图纸）**，告诉编译器「有这么个函数」；`common_extension.cc` 里则是把该函数 **注册进 PyTorch**（`m.def` 写 schema、`m.impl` 绑定实现），让 Python 能通过 `torch.ops.sgl_kernel` 找到它。

---

### 4.2 六步内核流水线总览

#### 4.2.1 概念说明

这是本讲（也是整本手册）最重要的一张图。官方 `README.md` 的「Contribution」一节其实就列出了这条流水线——贡献一个新算子，本质上就是沿着这条线把六个环节各补一刀。

这六个环节是：

1. **写 CUDA 实现**（`.cu`）：算子真正在 GPU 上干活的代码。
2. **声明头文件**（`.h`）：在 `include/sgl_kernel_ops.h` 里声明算子的 C++ 函数原型。
3. **注册 torch 扩展**（`.cc`）：在 `csrc/common_extension.cc` 里用 `m.def`（写 schema）+ `m.impl`（绑定实现）把算子注册进 PyTorch。
4. **写 Python 包装**（`.py`）：在 `python/sgl_kernel/<族>.py` 里写一个 Python 函数，转发到 `torch.ops.sgl_kernel.<算子名>`。
5. **`__init__` 导出**：在 `python/sgl_kernel/__init__.py` 里 `from ... import` 这个 Python 函数，让它对外可见。
6. **加入构建 + 补测试基准**：在 `CMakeLists.txt` 的 `SOURCES` 里加上 `.cu` 文件，并补 `tests/` 与 `benchmark/`。

#### 4.2.2 核心流程

把这六步画成一张「算子流转图」：

```text
        ┌─────────────────────────────────────────────────────────┐
        │                      用户 Python 代码                    │
        │            out = sgl_kernel.rmsnorm(x, w, eps)           │
        └───────────────────────────────┬─────────────────────────┘
                                        │ (5) __init__.py 导出
                                        ▼
        ┌─────────────────────────────────────────────────────────┐
        │  python/sgl_kernel/elementwise.py  （Python 包装层）     │
        │  torch.ops.sgl_kernel.rmsnorm.default(...)              │
        └───────────────────────────────┬─────────────────────────┘
                                        │ (3) torch 扩展注册
                                        ▼
        ┌─────────────────────────────────────────────────────────┐
        │  csrc/common_extension.cc                               │
        │  m.def("rmsnorm(...) -> ()");                           │
        │  m.impl("rmsnorm", torch::kCUDA, &rmsnorm);             │
        └───────────────────────────────┬─────────────────────────┘
                                        │ (2) 头文件声明 &rmsnorm
                                        ▼
        ┌─────────────────────────────────────────────────────────┐
        │  include/sgl_kernel_ops.h                               │
        │  void rmsnorm(at::Tensor& output, ...);                 │
        └───────────────────────────────┬─────────────────────────┘
                                        │ (1) CUDA 实现
                                        ▼
        ┌─────────────────────────────────────────────────────────┐
        │  csrc/elementwise/*.cu  （GPU 计算本体）                 │
        │  → 调用底层模板 / vendored flashinfer kernel             │
        └─────────────────────────────────────────────────────────┘

   (6) CMakeLists.txt 把 .cu 编进 common_ops 扩展；tests/ benchmark/ 验证
```

> 方向说明：**调用** 是自上而下（用户 → CUDA），而 **开发贡献** 是自下而上（先写 CUDA，再一层层包上去）。记住「开发顺序」就等于记住了 README 的贡献步骤。

为什么要有这么多层，而不是直接让 Python 调 CUDA？因为 PyTorch 的扩展机制要求：CUDA 函数必须先有一个 C++ 声明，再用 schema 注册，Python 才能安全地跨语言调用它。这套机制同时带来 dtype 分派、设备校验、`torch.compile` 兼容等好处——这些会在 u2、u4 详讲。

#### 4.2.3 源码精读

官方 `README.md` 把这条流水线写得很清楚，本讲直接引用它作为「总纲」：

- 贡献一个新算子的六步：[README.md:L49-L56](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/README.md#L49-L56) —— 对应 4.2.1 的第 1～6 步（顺序一致：csrc 实现 → include 声明 → common_extension 注册 → CMake 加源 → Python 接口 → 测试基准）。
- 关于 `m.def` / `m.impl` 的写法提示：[README.md:L60-L70](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/README.md#L60-L70) —— 解释了「先用 `m.def` 带 schema 定义、再用 `m.impl` 绑设备」的两步注册法，并附了 `bmm_fp8` 的例子。

流水线第 3 步的「注册总入口」是这样一个宏，它把整张注册表挂到 PyTorch 的 `sgl_kernel` 库名下：

```cpp
// csrc/common_extension.cc:21
TORCH_LIBRARY_FRAGMENT(sgl_kernel, m) {
  // ... 这里是成百上千对 m.def / m.impl ...
}
```

> 这一行说明：`common_extension.cc` 这个文件本身，就是流水线「第 3 步：torch 扩展注册」的容器。`TORCH_LIBRARY_FRAGMENT(sgl_kernel, m)` 里的 `sgl_kernel`，就是 Python 侧 `torch.ops.sgl_kernel` 那个名字的来源。

而流水线第 6 步（构建）的关键，是把 `.cu` 文件收进一个 `SOURCES` 列表，再编译成扩展（本讲只点一下，详见 u1-l3）：

```cmake
# CMakeLists.txt:248 起
set(SOURCES
    "csrc/allreduce/custom_all_reduce.cu"
    "csrc/attention/cutlass_mla_kernel.cu"
    ...
    "csrc/elementwise/fused_add_rms_norm_kernel.cu"   # 归一化算子的 .cu 在这里
    ...
)
```

#### 4.2.4 代码实践

**实践目标**：在脑子里把「README 的六步」和「流水线六个环节」对齐。

**操作步骤**：

1. 打开 [README.md:L49-L56](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/README.md#L49-L56)，把这六步抄到笔记里。
2. 在每一步后面，标注它对应的目录/文件：
   - 第 1 步 → `csrc/`（.cu）
   - 第 2 步 → `include/sgl_kernel_ops.h`
   - 第 3 步 → `csrc/common_extension.cc`
   - 第 4 步 → `python/sgl_kernel/<族>.py`
   - 第 5 步 → `python/sgl_kernel/__init__.py`
   - 第 6 步 → `CMakeLists.txt` + `tests/` + `benchmark/`

**需要观察的现象**：你会发现 README 的编号顺序和「开发顺序」完全一致，但和「调用顺序」相反。

**预期结果**：你能不看本讲，复述出六步与六个目录的对应关系。

#### 4.2.5 小练习与答案

**练习 1**：流水线里，为什么「头文件声明」（第 2 步）必须出现在「torch 扩展注册」（第 3 步）之前？

**参考答案**：因为第 3 步的 `m.impl("rmsnorm", torch::kCUDA, &rmsnorm)` 要取函数 `rmsnorm` 的地址 `&rmsnorm`，编译器必须先在第 2 步的头文件里看到它的声明，才知道这个符号长什么样、能否取地址。否则编译会报「未声明的标识符」。

**练习 2**：如果只做了第 1～4 步，忘了第 5 步（没在 `__init__.py` 里导出），会发生什么？

**参考答案**：算子本身能编译、能通过 `torch.ops.sgl_kernel.<name>` 调用，但用户写 `sgl_kernel.<name>` 时会 `AttributeError`（找不到这个名字）。第 5 步是让算子出现在包的公开 API 上。

**练习 3**：`TORCH_LIBRARY_FRAGMENT(sgl_kernel, m)` 里的 `sgl_kernel` 这个名字，会出现在 Python 调用链的哪个位置？

**参考答案**：会出现在 `torch.ops.sgl_kernel.<算子名>` 这个调用里——它正是 `sgl_kernel`。下一节你会看到真实代码 `torch.ops.sgl_kernel.rmsnorm.default(...)`。

---

### 4.3 链路定位练习：用 `rmsnorm` 走一遍真实链路

#### 4.3.1 概念说明

光看图不过瘾，本节我们拿一个真实算子 `rmsnorm`（RMS 归一化，LLaMA 等模型里常用），沿着六步流水线，在源码里把它的每一个环节都「点」出来。做完这个练习，你就掌握了「定位任意算子全链路」的通用方法。

> `rmsnorm` 的数学含义会在 u4-l1 详讲；本节我们只关心它的**代码位置**，不关心它算什么。

#### 4.3.2 核心流程

定位一个算子的标准动作：

1. **找注册**：在 `csrc/common_extension.cc` 里 `grep 算子名`，定位 `m.def` / `m.impl`（这是线索最集中的地方）。
2. **找声明**：在 `include/sgl_kernel_ops.h` 里 `grep 算子名`，看 C++ 签名。
3. **找实现**：根据 `m.impl` 绑定的函数名，在 `csrc/` 对应子目录找 `.cu`。
4. **找 Python 包装**：在 `python/sgl_kernel/<族>.py` 里 `grep 算子名`。
5. **找导出**：在 `python/sgl_kernel/__init__.py` 里确认它被 `import`。
6. **找构建**：在 `CMakeLists.txt` 的 `SOURCES` 里确认 `.cu` 已收录。

#### 4.3.3 源码精读

**第 3 步 · torch 扩展注册**（这是线索最密的入口）：

```cpp
// csrc/common_extension.cc:64-65
m.def("rmsnorm(Tensor! output, Tensor input, Tensor weight, float eps, bool enable_pdl) -> ()");
m.impl("rmsnorm", torch::kCUDA, &rmsnorm);
```

这一对做的事：[csrc/common_extension.cc:L64-L65](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/csrc/common_extension.cc#L64-L65)。

- `m.def(...)` 写出算子的 **schema**（类型签名）：输入 `input`、`weight`，原地写回 `output`（`Tensor!` 的感叹号表示「可变/原地修改」），返回 `()`（空）。
- `m.impl("rmsnorm", torch::kCUDA, &rmsnorm)` 把它绑定到 CUDA 设备上的 C++ 函数 `rmsnorm`。

> 想了解 schema 语法（`Tensor!`、`Tensor?`、`int`、`-> ()`），见 u2-l2。这里只需知道：`&rmsnorm` 这个函数指针，指向下一步要找的实现。

同族的 `fused_add_rmsnorm`（残差融合版）紧挨着注册：[csrc/common_extension.cc:L67-L68](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/csrc/common_extension.cc#L67-L68)，注意它绑定的实现名是 `&sgl_fused_add_rmsnorm`（带 `sgl_` 前缀）——这是个伏笔，待会儿在实现层会用到。

**第 2 步 · 头文件声明**：

```cpp
// include/sgl_kernel_ops.h:137-139
void rmsnorm(at::Tensor& output, at::Tensor& input, at::Tensor& weight, double eps, bool enable_pdl);
void sgl_fused_add_rmsnorm(
    torch::Tensor input, torch::Tensor residual, torch::Tensor weight, double eps, bool enable_pdl);
```

这一段：[include/sgl_kernel_ops.h:L134-L143](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/include/sgl_kernel_ops.h#L134-L143)。可以看到 `rmsnorm` 与 `sgl_fused_add_rmsnorm` 的 C++ 原型，正是上一步 `&rmsnorm` / `&sgl_fused_add_rmsnorm` 取地址的那个符号。

**第 1 步 · CUDA 实现**：

`rmsnorm` 家族的实现集中在 elementwise 目录。其中 `sgl_fused_add_rmsnorm` 的函数体是清晰可读的样例：[csrc/elementwise/fused_add_rms_norm_kernel.cu:L24-L59](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/csrc/elementwise/fused_add_rms_norm_kernel.cu#L24-L59)。它的关键结构是：

```cpp
// csrc/elementwise/fused_add_rms_norm_kernel.cu:24-25
void sgl_fused_add_rmsnorm(
    torch::Tensor input, torch::Tensor residual, torch::Tensor weight, double eps, bool enable_pdl) {
  CHECK_INPUT(input);            // 校验：是 CUDA 张量、连续
  // ...
  CHECK_DIM(2, input);           // 校验：维度必须是 2
  CHECK_EQ(input.size(1), weight.size(0));
  // ...
  cudaStream_t torch_current_stream = at::cuda::getCurrentCUDAStream();   // 取当前 stream
  DISPATCH_PYTORCH_DTYPE_TO_CTYPE_FLOAT_FP16(input.scalar_type(), c_type, [&] {
    norm::FusedAddRMSNorm(...);  // 调用 vendored flashinfer 的底层模板
    return true;
  });
}
```

它通过 `#include <flashinfer/norm.cuh>`（见 [fused_add_rms_norm_kernel.cu:L18](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/csrc/elementwise/fused_add_rms_norm_kernel.cu#L18)）复用了一份 vendored（内嵌进仓库的）FlashInfer 归一化模板，再用 `CHECK_*` 宏做输入校验、用 `DISPATCH_*` 宏做 dtype 分派。这两套宏定义在 `include/utils.h`，是 u2-l3 的主角。

> 说明：`rmsnorm`（非 fused 版）与 `sgl_fused_add_rmsnorm` 共享同一套 vendored flashinfer 归一化模板；前者侧重原地写 `output`，后者把残差加法融在一起原地改 `input`/`residual`。CUDA 本体都在 `csrc/elementwise/` 这一族里。

**第 4 步 · Python 包装**：

```python
# python/sgl_kernel/elementwise.py:16-28
def _rmsnorm_internal(input, weight, eps, out, enable_pdl):
    if out is None:
        out = torch.empty_like(input)
    if enable_pdl is None:
        enable_pdl = is_arch_support_pdl()
    torch.ops.sgl_kernel.rmsnorm.default(out, input, weight, eps, enable_pdl)   # ← 真正调用注册的 op
    return out
```

这一段：[python/sgl_kernel/elementwise.py:L16-L28](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/python/sgl_kernel/elementwise.py#L16-L28)。注意倒数第二行：`torch.ops.sgl_kernel.rmsnorm.default(...)` —— 这里的 `sgl_kernel` 正是第 3 步 `TORCH_LIBRARY_FRAGMENT(sgl_kernel, ...)` 的库名，`.default` 是 PyTorch 自动给 `m.impl` 绑定的「默认 overload」。

公开的 `rmsnorm` 包装还会先尝试 FlashInfer，不可用再回退到上面的内部实现：[python/sgl_kernel/elementwise.py:L76-L122](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/python/sgl_kernel/elementwise.py#L76-L122)（这条「FlashInfer 回退」策略在 u4-l1 详讲）。

**第 5 步 · `__init__` 导出**：

```python
# python/sgl_kernel/__init__.py:35-50（节选）
from sgl_kernel.elementwise import (
    ...
    rmsnorm,          # ← 第 47 行
    ...
)
```

`rmsnorm` 出现在 `from sgl_kernel.elementwise import (...)` 的导入列表里：[python/sgl_kernel/__init__.py:L47](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/python/sgl_kernel/__init__.py#L47)。正是因为这一行，用户才能写 `sgl_kernel.rmsnorm(...)`。

**第 6 步 · 构建**：

归一化族的 `.cu` 已被收进 `SOURCES`：[CMakeLists.txt:L258](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/CMakeLists.txt#L258)（即 `"csrc/elementwise/fused_add_rms_norm_kernel.cu"` 这一行）。整个 `SOURCES` 列表起于 [CMakeLists.txt:L248](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/CMakeLists.txt#L248)，被编译成扩展 `common_ops`（该扩展在文件末尾注册：[common_extension.cc:L484](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/csrc/common_extension.cc#L484) 的 `REGISTER_EXTENSION(common_ops)`）。

至此，`rmsnorm` 的六个环节全部定位完毕。

#### 4.3.4 代码实践

**实践目标**：亲手为 `rmsnorm` 画一张「算子流转图」，并标注每个文件在流水线中的环节。（这正是本讲义规格指定的实践任务。）

**操作步骤**：

1. 在仓库根目录依次执行下面 6 条 `grep`，确认本节给出的行号（如果你没有 GPU 也没关系，这些都是纯文本检索）：

   ```bash
   # 第 3 步：torch 扩展注册
   grep -n "rmsnorm" csrc/common_extension.cc | head
   # 第 2 步：头文件声明
   grep -n "void rmsnorm" include/sgl_kernel_ops.h
   # 第 1 步：CUDA 实现
   grep -n "rmsnorm" csrc/elementwise/fused_add_rms_norm_kernel.cu
   # 第 4 步：Python 包装
   grep -n "rmsnorm" python/sgl_kernel/elementwise.py | head
   # 第 5 步：__init__ 导出
   grep -n "rmsnorm" python/sgl_kernel/__init__.py
   # 第 6 步：构建
   grep -n "fused_add_rms_norm_kernel.cu" CMakeLists.txt
   ```

2. 准备一张纸（或文本文件），画一个六格表格，列头为：`环节 | 文件 | 行号 | 一句话作用`。
3. 逐格填入本节 4.3.3 给出的信息，例如：
   - `第3步 torch注册 | csrc/common_extension.cc | 64-65 | m.def schema + m.impl 绑 CUDA`
   - `第4步 Python包装 | elementwise.py | 16-28 | torch.ops.sgl_kernel.rmsnorm.default`
   - ……

4. **进阶**（可选）：换一个算子，例如 `silu_and_mul`，重跑上面的 `grep`（把 `rmsnorm` 换成 `silu_and_mul`，注意它的 CUDA 实现在 `csrc/elementwise/activation.cu`），验证「定位方法」对所有算子通用。

**需要观察的现象**：

- `rmsnorm` 这个字符串在 6 个不同文件里都能 grep 到，但每个文件里它的「身份」不同（注册/schema、声明、实现、转发、导入、构建收录）。
- `grep` 在 `common_extension.cc` 里能同时命中 `m.def` 和 `m.impl` 两行——这一对永远成对出现。

**预期结果**：得到一张完整的 `rmsnorm` 算子流转图，能一眼看出「调用方向」与「开发方向」。

> 关于「能否真正运行」：本实践是**源码阅读型实践**，不执行算子。若你想真的调用 `sgl_kernel.rmsnorm`，需要先 `make build` 出 `.so`（见 u1-l3），且需要 NVIDIA GPU；本讲不要求运行。

#### 4.3.5 小练习与答案

**练习 1**：在 `common_extension.cc` 中，`rmsnorm` 的 schema 写成 `rmsnorm(Tensor! output, ...) -> ()`，返回值是空 `()`。结合 Python 包装层 `out = torch.empty_like(input)`，解释为什么算子「返回空」却能拿到结果。

**参考答案**：因为 `output` 是 `Tensor!`（可变参数），算子在 GPU 上**原地**把结果写进传入的 `output` 张量。Python 包装先用 `torch.empty_like(input)` 分配好这块显存，调用 op 后这块显存里就是结果，最后 `return out` 把它交还用户。所以「返回空 + 原地写回」等价于「返回结果」，这种写法避免了额外的显存分配与拷贝，在推理引擎里很常见。

**练习 2**：第 4 步 Python 包装里调用的是 `torch.ops.sgl_kernel.rmsnorm.default`，多了一个 `.default`。这个 `.default` 从哪里来？

**参考答案**：PyTorch 的 custom op 用 `m.impl(name, device, fn)` 注册时，会自动创建一个名为 `default` 的 overload（重载）。所以注册名 `rmsnorm` 对应的完整调用路径是 `torch.ops.sgl_kernel.rmsnorm.default`。`.default` 是 PyTorch 的约定，不是 sgl-kernel 自己加的。

**练习 3**：如果你要在仓库里搜索「`rmsnorm` 的 CUDA 实现在哪个文件」，最可靠的检索词是什么？

**参考答案**：用第 3 步 `m.impl` 绑定的 C++ 函数名去搜。对 fused 版，绑定名是 `sgl_fused_add_rmsnorm`，用 `grep -rn "void sgl_fused_add_rmsnorm" csrc/` 可直接定位到 `csrc/elementwise/fused_add_rms_norm_kernel.cu:24`。直接按算子名搜文件名（`*rms*`）容易漏掉共享实现或 vendored 模板的情况。

---

## 5. 综合实践

把本讲三个模块串起来，完成一个「全链路定位」小任务：

**任务**：任选 sgl-kernel 中的一个算子（推荐 `silu_and_mul` 或 `rotary_embedding`，难度递增），产出一份《算子全链路档案》，包含：

1. **六步流水线表格**：参照 4.3.4 的表格格式，填齐该算子的 6 个环节（文件 + 行号 + 作用）。
2. **调用方向箭头图**：画一张从「用户 Python 调用」到「CUDA 实现」的自上而下箭头图，标注每段经过的文件。
3. **一句话定位口诀**：总结你是如何用 `grep` 在 1 分钟内找到它的注册行的。

**检查清单**（自我验证）：

- [ ] 第 3 步：在 `csrc/common_extension.cc` 里能找到成对的 `m.def(...)` + `m.impl(...)`。
- [ ] 第 2 步：在 `include/sgl_kernel_ops.h` 里能找到与 `m.impl` 绑定名一致的函数声明。
- [ ] 第 1 步：在 `csrc/<族>/` 里能找到该函数的定义（函数体）。
- [ ] 第 4 步：在 `python/sgl_kernel/<族>.py` 里能找到调用 `torch.ops.sgl_kernel.<算子名>.default` 的包装函数。
- [ ] 第 5 步：在 `python/sgl_kernel/__init__.py` 的 `from ... import (...)` 里能看到该算子名。
- [ ] 第 6 步：在 `CMakeLists.txt` 的 `SOURCES` 里能看到对应的 `.cu` 文件名。

> 以 `silu_and_mul` 为例提示：注册在 [common_extension.cc:L76-L77](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/csrc/common_extension.cc#L76-L77)，CUDA 实现在 `csrc/elementwise/activation.cu`，构建收录在 [CMakeLists.txt:L254](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/CMakeLists.txt#L254)。其余环节请你用 `grep` 自行补全。

完成这份档案后，你就真正掌握了「在 sgl-kernel 里定位任意算子全链路」的能力——这是阅读后续所有讲义的前置技能。

---

## 6. 本讲小结

- sgl-kernel 源码分五大目录：`csrc/`（CUDA/C++ 实现 + 扩展注册）、`include/`（C++ 头文件声明）、`python/`（Python 包装 + 导出）、`tests/`（正确性）、`benchmark/`（性能）。
- **核心模型是「六步内核流水线」**：CUDA 实现 → 头文件声明 → torch 扩展注册(`m.def`/`m.impl`) → Python 包装(`torch.ops.sgl_kernel.*`) → `__init__` 导出 → CMake 构建 + 测试基准。
- 这六步与 `README.md` 的「贡献新算子」官方步骤一一对应；记住「开发方向」就记住了贡献流程。
- `TORCH_LIBRARY_FRAGMENT(sgl_kernel, m)` 里的 `sgl_kernel` 就是 Python 侧 `torch.ops.sgl_kernel` 的库名来源；`m.impl` 会自动产生 `.default` overload。
- 定位任意算子的通用方法：先在 `common_extension.cc` 用算子名 grep 到 `m.impl` 绑定的 C++ 函数名，再用这个函数名去 `include/` 找声明、去 `csrc/` 找实现。
- `rmsnorm` 全链路已逐一验证：注册 [common_extension.cc:L64-L65](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/csrc/common_extension.cc#L64-L65)、声明 [sgl_kernel_ops.h:L137](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/include/sgl_kernel_ops.h#L137)、实现 [fused_add_rms_norm_kernel.cu:L24-L59](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/csrc/elementwise/fused_add_rms_norm_kernel.cu#L24-L59)、包装 [elementwise.py:L16-L28](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/python/sgl_kernel/elementwise.py#L16-L28)、导出 [__init__.py:L47](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/python/sgl_kernel/__init__.py#L47)、构建 [CMakeLists.txt:L258](https://github.com/sgl-project/sglang/blob/1b4cb6b8c14944fbe3d7e02d4f8de6e8b465d971/sgl-kernel/CMakeLists.txt#L258)。

---

## 7. 下一步学习建议

本讲建立了「算子流转」的全景图，但每一环的内部细节都还没展开。建议按以下顺序继续：

1. **u1-l3 构建系统**：搞清 `SOURCES` 是怎么被编译成 `common_ops_sm90` / `common_ops_sm100` 两个 `.so` 的，以及 `make build` 背后做了什么。
2. **u2-l1 Python 入口与架构自适应加载**：看 `__init__.py` 是如何根据 GPU 型号（sm90/sm100）决定加载哪个 `.so` 的，理解第 5 步「导出」之前的隐藏环节。
3. **u2-l2 torch op 分派**：深入第 3 步，彻底搞懂 schema 语法（`Tensor!`、`Tensor?`、`-> ()`）与 `m.def`/`m.impl` 的双步注册。
4. **u2-l3 CUDA kernel 体**：深入第 1 步，搞懂 `CHECK_INPUT`/`CHECK_DIM` 校验宏与 `DISPATCH_*` dtype 分派宏（这些宏定义在 `include/utils.h`）。

> 阅读建议：如果只想快速上手，先读 u1-l3 和 u2-l2；如果目标是「自己贡献一个新算子」，则把 u2 三篇 + u11-l3（贡献新算子端到端）作为一组通读。
