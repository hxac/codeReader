# 目录结构与 kernel 模块约定

> 所属单元：U1 项目入门：定位、运行与目录结构
> 依赖讲义：[u1-l1 LeetCUDA 是什么](./u1-l1-project-overview.md)

## 1. 本讲目标

学完本讲后，你应该能够：

- 说出 LeetCUDA 仓库**顶层有哪些目录**，以及它们各自承担什么职责（`kernels/`、`third-party/`、`others/`、`slides/`、`docs/`、几个 git 子模块）。
- 画出 `kernels/` 下各算子目录的**标准三件套约定**：`README.md` + `<kernel>.cu` + `<kernel>.py`。
- 看懂 **`.cu` 文件（CUDA kernel + pybind 绑定）** 与 **`.py` 文件（PyTorch 包装 + 验证 + 基准）** 之间的分工，并能把「C++ 里的某个 kernel 函数」对应到「Python 里的某个调用入口」。
- 区分**简单算子目录**（如 `relu/`，单文件 + 即时编译）与**大型算子目录**（如 `hgemm/`、`flash-attn/`，多子目录 + `setup.py`）两种组织形态。

承接 u1-l1：你已经知道 LeetCUDA 是一个面向初学者的 CUDA 学习仓库，核心是 200+ 个 kernel。本讲回答下一个自然的问题——**「这些 kernel 在仓库里到底是怎么摆放的？我打开一个目录，应该先看哪个文件？」**

## 2. 前置知识

本讲几乎不涉及 CUDA 语法细节，只需要你具备以下常识：

- **什么是源码目录树**：一个项目就像一棵树，根目录下挂着若干文件夹，每个文件夹再挂文件或子文件夹。读源码的第一步永远是「先认路」。
- **文件后缀的含义**：
  - `.cu`：CUDA 源文件，里面既能写 C++ 代码，也能写运行在 GPU 上的 kernel（用 `__global__` 标记）。
  - `.py`：Python 脚本，这里主要用来**调用** kernel、**验证**结果、**计时**做基准。
  - `.cc`/`.cpp`：纯 C++ 源文件，本仓库里常用来集中存放「pybind 绑定」代码（把 C++ 函数暴露给 Python）。
  - `.md`：Markdown 文档，仓库里每个算子目录都配一个 `README.md` 当说明书。
- **PyTorch 扩展的两条路**（本讲只要求「听说过」，细节留给 [u3-l1](./u3-l1-pytorch-cuda-extension.md)）：
  1. **即时编译（JIT）**：用 `torch.utils.cpp_extension.load(...)` 在运行时把 `.cu` 编译成可调用模块。
  2. **预编译安装**：用 `setup.py` + `CUDAExtension` 提前编译成 pip 包。

## 3. 本讲源码地图

本讲涉及的「源码」其实大多是**目录与文件本身**，而不是某段算法逻辑。关键文件如下：

| 文件 / 目录 | 作用 |
|:---|:---|
| `README.md` | 项目总入口，含特性清单、Quick Start、200+ kernel 分类表、博客索引。 |
| `.gitmodules` | 记录 3 个 git 子模块（cutlass、HGEMM、ffpa-attn）的来源。 |
| `kernels/` | **核心目录**，30 个算子子目录 + `interview/`（notes-v2）等。 |
| `kernels/relu/README.md` | ReLU 算子的说明书：列出实现版本 + 测试命令 + 输出示例。 |
| `kernels/relu/relu.cu` | ReLU 的 CUDA kernel 实现 **和** pybind 绑定（简单算子范本）。 |
| `kernels/relu/relu.py` | ReLU 的 PyTorch 包装：JIT 加载 + 基准测试脚手架。 |
| `kernels/hgemm/setup.py` | 大型算子范本：用 `CUDAExtension` 预编译 `toy_hgemm`。 |
| `kernels/hgemm/pybind/hgemm.cc` | 大型算子的 pybind 集中注册文件。 |

## 4. 核心概念与源码讲解

### 4.1 仓库顶层目录地图

#### 4.1.1 概念说明

LeetCUDA 不是一个用构建系统（如 CMake）组织的「应用程序」，而更像一本**用源码写成的教科书**。因此它的顶层目录是按「学习内容」而非「软件分层」来划分的：真正要学的 kernel 都在 `kernels/`，外围目录（`third-party/`、`others/`、`slides/`）是依赖、扩展和讲义材料。

理解顶层目录的意义在于：当你想学某个算子时，**直接进 `kernels/`**；当你想知道仓库依赖了什么外部库，看 `third-party/`；当你想做进阶的分布式/TRT 实验，看 `others/`。

#### 4.1.2 核心流程：从顶层目录到一篇讲义

读者拿到仓库后的「认路」流程可以这样走：

1. 读 `README.md` → 了解项目有哪些算子、按难度怎么分类。
2. 进入 `kernels/` → 选一个感兴趣的算子目录（如 `relu/`）。
3. 读该目录的 `README.md` → 看它实现了哪些版本。
4. 打开 `<kernel>.cu` 读 kernel，打开 `<kernel>.py` 跑测试。
5. 需要底层库支撑（如 CUTLASS）时，去 `third-party/cutlass` 找头文件。

#### 4.1.3 源码精读：真实的顶层结构

下面是仓库根目录实际包含的内容（在 Linux 上用 `ls` 即可看到）：

```
LeetCUDA/
├── README.md            # 项目总说明（约 760 行，特性+kernel表+博客）
├── LICENSE              # GPLv3
├── CONTRIBUTE.md        # 如何贡献 kernel
├── .gitmodules          # 3 个子模块声明（见下）
├── .gitignore / .pre-commit-config.yaml / .clang-format-ignore / .isort.cfg
├── kernels/             # ⭐ 核心目录：30 个算子子目录（见 4.2）
├── third-party/         # 外部依赖
│   └── cutlass/         #   NVIDIA CUTLASS 子模块（CuTe/CUTLASS kernel 依赖它）
├── others/              # CUDA kernel 之外的扩展实验
│   ├── pytorch/         #   PyTorch 分布式集合通信测试 + custom_ops + slides
│   └── tensorrt/        #   TensorRT plugin / fmha 导出示例
├── slides/              # 配套讲义幻灯片
│   ├── cuda-slides/
│   └── vllm-slides/
├── docs/                # 文档目录（当前为空，预留）
├── HGEMM/               # 子模块 → github.com/xlite-dev/HGEMM（独立 HGEMM 仓库）
└── ffpa-attn/           # 子模块 → github.com/xlite-dev/ffpa-attn（FA2 长序列扩展）
```

注意三个「看似空、其实是子模块」的目录：`third-party/cutlass`、`HGEMM`、`ffpa-attn`。它们在 `git clone` 后是**空的**，必须执行 `git submodule update --init --recursive` 才会拉取内容。这一点 README 的 Quick Start 第一行就强调了：

```bash
git submodule update --init --recursive --force && cd kernels/interview
```

参见 [README.md:41](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/README.md#L41)（Quick Start 首行即初始化子模块）。

而 `.gitmodules` 记录了这三个子模块的来源，CUTLASS 来自 NVIDIA 官方：

参见 [.gitmodules:1-3](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/.gitmodules#L1-L3)（cutlass 子模块指向 NVIDIA 官方仓库）。

> **术语解释：git 子模块（submodule）**
> 一个 git 仓库里「嵌入」了另一个 git 仓库。父仓库只记录子仓库的 commit 号，不复制其文件内容。所以 `git clone` 父仓库后，子模块目录是空的，需要额外命令拉取。LeetCUDA 把庞大的 CUTLASS 和两个衍生项目作为子模块，避免把它们的海量代码塞进主仓库。

#### 4.1.4 代码实践

**实践目标**：亲手确认顶层目录结构，识别哪些是子模块。

**操作步骤**：

1. 在仓库根目录运行 `ls -la`，对照上面的目录树逐一核对。
2. 运行 `cat .gitmodules`，数一数有几个 `[submodule "..."]` 段落。
3. 运行 `ls third-party/cutlass`（若未执行 `submodule update`，应为空或仅有 `.git`）。

**需要观察的现象**：

- 顶层有 `kernels/`、`third-party/`、`others/`、`slides/`、`docs/`、`HGEMM/`、`ffpa-attn/` 七个目录。
- `.gitmodules` 里恰好 3 段：`third-party/cutlass`、`HGEMM`、`ffpa-attn`。

**预期结果**：与上方目录树一致。若 `third-party/cutlass` 为空，说明子模块未初始化，运行 Quick Start 第一行命令即可。

> 待本地验证：如果你在无 GPU / 无网络的容器里，`submodule update` 可能失败，此时只能看到空目录，属正常现象。

#### 4.1.5 小练习与答案

**练习 1**：仓库里有一个 `interview/` 目录（在 `kernels/` 下，见 4.2），它和顶层目录有什么关系？为什么它不在顶层？

> **答案**：`interview/` 收录的是「面试向」的单文件学习骨架 `notes-v2.cu`，本质仍是 kernel 学习材料，所以归在 `kernels/` 下而非顶层。顶层只放「项目级」资源（说明、依赖、扩展），`kernels/` 专门放学习用 kernel。

**练习 2**：`others/` 目录里为什么放的是「PyTorch 分布式测试」和「TensorRT」？它们和 CUDA kernel 是什么关系？

> **答案**：LeetCUDA 主题是手写 CUDA kernel，但 kernel 最终要嵌入 PyTorch 或 TRT 才能用。`others/` 收录的是「kernel 之外的生态集成」实验，属于扩展阅读，不是核心 kernel 练习。

---

### 4.2 kernels/ 算子目录与「README + .cu + .py」三件套约定

#### 4.2.1 概念说明

`kernels/` 是仓库的心脏，里面有 **30 个子目录**（见下方完整清单）。绝大多数算子目录都遵循一个高度统一的约定——**三件套**：

```
kernels/<算子名>/
├── README.md        # 说明书：实现了哪些版本、怎么测试、输出长什么样
├── <算子名>.cu      # CUDA kernel 实现（+ 简单算子的 pybind 绑定）
└── <算子名>.py      # PyTorch 包装：加载 .cu、验证正确性、跑基准
```

这套约定是 LeetCUDA 的「模块单元」。你在 u1-l1 已经知道 README 描述的工作流是「custom CUDA kernel → PyTorch Python bindings → Run tests」，这三件套正好一一对应：

参见 [README.md:247](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/README.md#L247)（README 明确给出每个主题的工作流：kernel 实现 → Python 绑定 → 运行测试）。

这个约定的好处是：**学任何一个新算子，你的阅读顺序都一样**——先 README，再 .cu，最后 .py。这极大降低了「换一个目录就重新摸索」的认知成本。

#### 4.2.2 核心流程：三件套如何协作

```
        README.md                <kernel>.cu                  <kernel>.py
   ┌─────────────────┐      ┌────────────────────┐      ┌────────────────────┐
   │ 列出实现版本     │  ←→  │ __global__ kernel  │ ←加载│ load("x.cu") → lib │
   │ 测试命令         │      │ + host 启动函数    │      │ 构造 torch.Tensor  │
   │ 输出示例         │      │ + PYBIND11_MODULE  │      │ 调 lib.xxx(...)    │
   └─────────────────┘      └────────────────────┘      │ 对比 PyTorch 参考  │
                                                        │ warmup + 计时      │
                                                        └────────────────────┘
```

- **README.md** 是「目录的入口文档」，告诉你这个算子实现了哪些精度/向量化版本。
- **`.cu`** 是「实现层」，既写 GPU kernel，又（对简单算子而言）写好 pybind 暴露。
- **`.py`** 是「使用层」，负责把 `.cu` 加载进来、喂数据、对答案、测时间。

#### 4.2.3 源码精读：以 relu 为标准范本

`kernels/relu/` 是最干净的三件套样本。先用 `ls kernels/relu` 看到三个文件：`README.md`、`relu.cu`、`relu.py`（外加一个 `.gitignore`）。

**① README.md：说明书**

ReLU 的 README 顶部用清单列出了所有实现版本，并用一段 bash 给出测试命令：

```markdown
## 0x00 说明
- [X] relu_f32_kernel
- [X] relu_f32x4_kernel(float4向量化版本)
- [X] relu_f16_kernel(fp16版本)
...
- [X] PyTorch bindings

## 测试
export TORCH_CUDA_ARCH_LIST=Ada
python3 relu.py
```

参见 [kernels/relu/README.md:3-13](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/relu/README.md#L3-L13)（版本清单）与 [kernels/relu/README.md:16-22](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/relu/README.md#L16-L22)（测试命令）。这份 README 还贴出了完整输出表（不同 `S,K` 规模下各版本的耗时），让你不跑也能看到预期结果。

**② relu.cu：kernel + 绑定都在一个文件**

`relu.cu` 文件顶部是一组**全仓库通用的类型转换宏**（把指针重解释为 `float4`/`half2` 等，用于向量化访存）：

```cpp
#define FLOAT4(value) (reinterpret_cast<float4 *>(&(value))[0])
#define HALF2(value)  (reinterpret_cast<half2 *>(&(value))[0])
#define LDST128BITS(value) (reinterpret_cast<float4 *>(&(value))[0])
```

参见 [kernels/relu/relu.cu:12-16](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/relu/relu.cu#L12-L16)（仓库通用的向量化访存宏）。

接着是真正的 GPU kernel，例如最朴素的 FP32 ReLU：

```cpp
// grid(N/256), block(K=256)
__global__ void relu_f32_kernel(float *x, float *y, int N) {
  int idx = blockIdx.x * blockDim.x + threadIdx.x;
  if (idx < N) y[idx] = fmaxf(0.0f, x[idx]);
}
```

参见 [kernels/relu/relu.cu:21-25](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/relu/relu.cu#L21-L25)（naive f32 ReLU kernel）。

> **小提示**：永久链接里的 `/blob/<commit>/` 是一串很长的 commit 号（本讲统一用 `7d9ce2a...`）。点击链接跳转后，GitHub 会精确锁定到这次提交，即使仓库以后改动，你看到的仍是本讲编写时的代码——这正是「永久」二字的含义，也是我们逐行核对行号的依据。

文件后半部分是 **pybind 绑定**：用一个 `TORCH_BINDING_RELU` 宏批量生成「启动函数」，再用 `PYBIND11_MODULE` 把它们注册成 Python 可调用名字：

```cpp
TORCH_BINDING_RELU(f32, torch::kFloat32, float, 1)
TORCH_BINDING_RELU(f32x4, torch::kFloat32, float, 4)
...
PYBIND11_MODULE(TORCH_EXTENSION_NAME, m) {
  TORCH_BINDING_COMMON_EXTENSION(relu_f32)
  TORCH_BINDING_COMMON_EXTENSION(relu_f32x4)
  ...
}
```

参见 [kernels/relu/relu.cu:118-162](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/relu/relu.cu#L118-L162)（宏生成启动函数 + 实例化）与 [kernels/relu/relu.cu:164-171](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/relu/relu.cu#L164-L171)（PYBIND11_MODULE 注册入口）。

这里的 `TORCH_BINDING_COMMON_EXTENSION(relu_f32)` 展开后等价于 `m.def("relu_f32", &relu_f32, "relu_f32");`——把 C++ 函数 `relu_f32` 以同名暴露给 Python。绑定宏本身只有两行：

参见 [kernels/relu/relu.cu:108-110](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/relu/relu.cu#L108-L110)（`TORCH_BINDING_COMMON_EXTENSION` 宏定义）。

**③ relu.py：JIT 加载 + 基准脚手架**

`relu.py` 用 `torch.utils.cpp_extension.load` 把 `relu.cu` 即时编译成一个 Python 模块 `lib`，后续直接 `lib.relu_f32(x, y)` 调用：

```python
from torch.utils.cpp_extension import load
lib = load(
    name="relu_lib",
    sources=["relu.cu"],
    extra_cuda_cflags=["-O3", "--use_fast_math", ...],
    extra_cflags=["-std=c++17"],
)
```

参见 [kernels/relu/relu.py:10-24](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/relu/relu.py#L10-L24)（JIT 加载 `.cu`）。随后是一段标准基准逻辑：构造不同 `S,K` 的张量，对每个版本 warmup + 1000 次迭代计时：

参见 [kernels/relu/relu.py:27-66](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/relu/relu.py#L27-L66)（`run_benchmark`：warmup + iters + 同步计时）与 [kernels/relu/relu.py:69-90](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/relu/relu.py#L69-L90)（主循环：遍历规模并调用 `lib.relu_f32` 等）。

#### 4.2.4 代码实践

**实践目标**：验证三件套约定在多个算子目录中成立。

**操作步骤**：

1. 依次 `ls kernels/relu kernels/elementwise kernels/softmax kernels/rms-norm kernels/sgemv kernels/dot-product`。
2. 对比每个目录的文件构成，找出共同点。

**需要观察的现象**：每个目录都至少有 `README.md`、一个 `.cu`、一个 `.py`，且 `.cu`/`.py` 的主文件名与目录名对应（如 `softmax/softmax.cu`、`softmax/softmax.py`）。

**预期结果**：三件套约定在 Easy/Medium 算子中高度一致。少数目录会有差异（见下方对比表），这是有意义的特例，不是约定被破坏。

#### 4.2.5 小练习与答案

**练习 1**：`kernels/nms/` 里除了 `nms.cu` 和 `nms.py`，还多了一个 `nms.cc`。为什么 ReLU 不需要 `.cc` 而 NMS 需要？

> **答案**：是否拆出独立的 `.cc` 取决于绑定代码的规模。ReLU 的绑定只有几行宏，直接写在 `relu.cu` 末尾即可；NMS 可能把 C++ 侧的辅助逻辑或绑定单独放进 `nms.cc`，让 `nms.cu` 更聚焦于 kernel。两者本质都是「把 C++ 暴露给 Python」，只是组织粒度不同。

**练习 2**：`kernels/sgemm/` 里有 4 个 `.cu` 文件（`sgemm.cu`、`sgemm_async.cu`、`sgemm_wmma_tf32_stage.cu`、`sgemm_cublas.cu`），但只有一个 `sgemm.py`。这说明什么？

> **答案**：一个算子可以有**多个递进优化的 `.cu` 实现**（naive → async → Tensor Core → cuBLAS 对照），但只需**一个 `.py` 入口**统一加载和基准对比它们。这正是 LeetCUDA「同一算子多版本对照学习」的体现。

---

### 4.3 `.cu` 与 `.py` 的分工：从 kernel 到 PyTorch 调用入口

#### 4.3.1 概念说明

理解了三件套，还要理解它内部最重要的「接力关系」：**一个 GPU kernel 是怎么变成 Python 里一行 `lib.relu_f32(x, y)` 的？** 这条链路上有四个角色：

1. **`__global__` kernel 函数**（如 `relu_f32_kernel`）：真正跑在 GPU 上、每个线程执行一段计算。
2. **host 启动函数**（如 `relu_f32`）：跑在 CPU 上，负责设置 grid/block 维度并用 `<<<grid, block>>>` 语法**启动**上面的 kernel。
3. **`PYBIND11_MODULE` 注册**：给启动函数起一个 Python 名字。
4. **Python 侧 `lib.xxx(...)`**：实际调用入口。

很多人初学时会把「kernel 函数」和「Python 调用入口」当成一个东西，其实中间隔了启动函数和绑定两层。搞清这层对应关系，是后续阅读任何 kernel 的基本功。

此外，仓库里存在**两种不同的「绑定 + 加载」范式**，对应简单算子和大型算子：

| 维度 | 简单算子（如 `relu/`） | 大型算子（如 `hgemm/`、`flash-attn/`） |
|:---|:---|:---|
| 绑定代码位置 | 直接写在 `<kernel>.cu` 末尾 | 独立放 `pybind/<name>.cc` |
| Python 加载方式 | `load()` 即时编译 | `setup.py` + `CUDAExtension` 预编译安装 |
| `.cu` 文件数 | 通常 1 个 | 多个，按优化路线分子目录（`naive/`、`wmma/`、`mma/`、`wgmma/`…） |
| 包名 | 运行时模块名（如 `relu_lib`） | pip 包名（如 `toy-hgemm`） |

#### 4.3.2 核心流程：四层接力与两种范式

**简单算子的接力链（relu）**：

```
__global__ relu_f32_kernel        // GPU kernel（relu.cu:21）
        ↑ 由它启动
relu_f32(...)                     // host 启动函数（宏生成，relu.cu:118）
        ↑ 注册
PYBIND11_MODULE → m.def("relu_f32", &relu_f32)   // relu.cu:164
        ↑ JIT 加载整文件
lib = load(sources=["relu.cu"])   // relu.py:10
        ↓ 调用
lib.relu_f32(x, y)                // relu.py:78（Python 入口）
```

**大型算子的接力链（hgemm）**：kernel 散落在多个子目录的 `.cu` 里，每个 `.cu` 只写 kernel 和启动函数的**声明/定义**；`pybind/hgemm.cc` 集中 `#include` 并用 `m.def` 注册全部入口；最后由 `setup.py` 一次性编译安装为 `toy_hgemm` 包。

#### 4.3.3 源码精读：两种范式对照

**范式 A — 简单算子：绑定内联在 `.cu`（relu，已见 4.2.3）**

复习关键点：`relu.cu` 一个文件同时包含 kernel、启动函数、`PYBIND11_MODULE`；`relu.py` 用 `load()` JIT 编译后直接 `lib.relu_f32(...)`。链路完全自洽在一个目录的两个文件里。

**范式 B — 大型算子：绑定独立 + setup.py 预编译（hgemm）**

`kernels/hgemm/` 是一个「小项目」，结构比 relu 复杂得多：

```
kernels/hgemm/
├── README.md  hgemm.py  makefile  setup.py     # 文档 + Python 基准 + 两种构建入口
├── naive/        # 手写 CUDA Cores 路线：hgemm.cu, hgemm_async.cu
├── wmma/         # WMMA Tensor Core 路线：hgemm_wmma.cu, hgemm_wmma_stage.cu
├── mma/          # MMA PTX 路线：basic/, others/, swizzle/
├── wgmma/        # Hopper WGMMA 路线：*_fp16acc_*, *_fp32acc_*
├── cutlass/      # CuTe DSL 路线：hgemm_mma_stage_tn_cute.cu
├── cublas/       # cuBLAS 对照基线：hgemm_cublas.cu
├── pybind/       # ⭐ 集中绑定：hgemm.cc
├── bench/  utils/  tools/                      # 基准、工具、构建辅助
```

> 注意：每个子目录代表一种**优化路线/精度/架构**（naive → WMMA → MMA → WGMMA → CuTe），这就是 README 里 HGEMM 那张大特性表「实体化」成目录的结果。这是 u1-l1 提到的「同一算子多版本对照」在大型算子里的升级形态。

在大型范式里，绑定不再写在某个 `.cu` 末尾，而是集中到 `pybind/hgemm.cc`。该文件先 `#include <torch/extension.h>`，然后**声明**来自各 `.cu` 的启动函数（仅函数签名，不带实现），最后统一注册。摘录开头：

```cpp
#define STRINGFY(str) #str
#define TORCH_BINDING_COMMON_EXTENSION(func)  m.def(STRINGFY(func), &func, STRINGFY(func));

// from hgemm.cu
void hgemm_naive_f16(torch::Tensor a, torch::Tensor b, torch::Tensor c);
void hgemm_sliced_k_f16(torch::Tensor a, torch::Tensor b, torch::Tensor c);
...
```

参见 [kernels/hgemm/pybind/hgemm.cc:5-6](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/hgemm/pybind/hgemm.cc#L5-L6)（绑定宏）与 [kernels/hgemm/pybind/hgemm.cc:9-14](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/hgemm/pybind/hgemm.cc#L9-L14)（来自 `hgemm.cu` 的启动函数声明）。注释 `// from hgemm.cu`、`// from hgemm_wmma.cu` 等清楚地标注了每个函数来自哪个子目录的 `.cu`——这就是「`.cc` 如何把分散的 `.cu` 串起来」的明证。

编译则由 `setup.py` 用 `CUDAExtension` + `BuildExtension` 完成，并通过 `include_dirs` 把各子目录加入头文件搜索路径：

```python
ext_modules.append(
    CUDAExtension(
        name="toy_hgemm",
        sources=get_build_sources(),
        extra_compile_args={"cxx": [...], "nvcc": [...]},
        include_dirs=[
            Path(this_dir) / "naive", Path(this_dir) / "utils",
            Path(this_dir) / "wmma",  Path(this_dir) / "mma",
            Path(this_dir) / "cutlass", Path(this_dir) / "cublas",
            Path(this_dir) / "pybind",
        ],
    )
)
setup(name="toy-hgemm", ..., cmdclass={"build_ext": BuildExtension})
```

参见 [kernels/hgemm/setup.py:45-68](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/hgemm/setup.py#L45-L68)（`CUDAExtension` + `include_dirs`）与 [kernels/hgemm/setup.py:70-96](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/hgemm/setup.py#L70-L96)（`setup(...)` 用 `BuildExtension` 安装为 `toy-hgemm`）。

> **直觉理解**：为什么大型算子要换范式？因为它的 `.cu` 多到几十个、跨越多个子目录，还依赖 CUTLASS 头文件，用 JIT `load()` 一行命令很难配齐所有 `include_dirs` 和宏。`setup.py` 能精细控制编译参数，预编译成包后还能被 `pip uninstall` 干净移除（见 setup.py 注释 `# package name managed by pip`）。简单算子只有一个 `.cu`、没有外部依赖，JIT 最省事。**选哪种范式，取决于复杂度。**

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：把 `relu.cu` 里的 kernel 函数与 `relu.py` 里的调用入口一一对应画出来。

**操作步骤**：

1. 打开 [kernels/relu/relu.cu](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/relu/relu.cu) 与 [kernels/relu/relu.py](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/relu/relu.py)。
2. 在 `relu.cu` 中找到：`relu_f32_kernel`（L21）、由 `TORCH_BINDING_RELU(f32,...)` 生成的启动函数 `relu_f32`、`PYBIND11_MODULE` 里的注册行（L165）。
3. 在 `relu.py` 中找到：`load(...)`（L10）、调用 `lib.relu_f32(x, y)`（L78）。
4. 画一张四层对应表（参考答案见下）。

**需要观察的现象**：四个名字之间存在「同名传递」——`relu_f32` 这个名字从 C++ 启动函数，经 `m.def("relu_f32", ...)`，原样出现在 Python 的 `lib.relu_f32`。kernel 本身（`relu_f32_kernel` 带 `_kernel` 后缀）**不会**直接出现在 Python，它被启动函数包了一层。

**预期结果（参考答案表）**：

| 层 | 名字 | 位置 | 作用 |
|:---|:---|:---|:---|
| GPU kernel | `relu_f32_kernel` | relu.cu:21 | 每个线程算 `y[idx]=max(0,x[idx])` |
| host 启动函数 | `relu_f32` | relu.cu:118(宏生成) | 配置 grid/block，启动上面的 kernel |
| pybind 注册 | `m.def("relu_f32", &relu_f32)` | relu.cu:165 | 把 `relu_f32` 暴露为 Python 名 |
| Python 入口 | `lib.relu_f32(x, y)` | relu.py:78 | 实际调用点 |

> 待本地验证：`lib.relu_f32` 是否真能调用，取决于 `load()` 是否成功编译（需要本机有 nvcc 与 GPU）。无 GPU 环境下，可只完成「静态对应关系」部分。

#### 4.3.5 小练习与答案

**练习 1**：为什么 Python 里调用的是 `lib.relu_f32` 而不是 `lib.relu_f32_kernel`？

> **答案**：`__global__` kernel 不能被 Python 直接调用，它必须由一个 host 函数用 `<<<grid, block>>>` 启动。暴露给 Python 的是这个 host 启动函数 `relu_f32`，而 `relu_f32_kernel` 只是它在 GPU 上的「内核」。所以 Python 调用的是「启动函数」，不是 kernel 本身。

**练习 2**：对比 `relu/` 和 `hgemm/`，如果一个新算子只有 1 个 `.cu` 且不依赖 CUTLASS，应该选哪种范式？

> **答案**：选简单范式（`load()` JIT）。理由：单文件无外部依赖时，JIT 一行 `load(sources=["x.cu"])` 就能编译调用，无需写 `setup.py`，也无需把绑定拆到单独 `.cc`。只有当 `.cu` 数量多、需要复杂 `include_dirs` 或要发布为 pip 包时，才升级到 `setup.py` 范式。

**练习 3**：`hgemm/pybind/hgemm.cc` 里写的是 `void hgemm_naive_f16(torch::Tensor a, ...);` 这样的**声明**，而不是实现。实现去哪了？

> **答案**：实现（kernel + 启动函数体）在 `naive/hgemm.cu` 里。`.cc` 只收集声明并注册，真正的定义由 `setup.py` 把 `naive/hgemm.cu` 和 `pybind/hgemm.cc` 一起编译、链接成一个模块。注释 `// from hgemm.cu` 就是声明与实现的对应线索。

## 5. 综合实践

**任务**：亲手绘制两张图，把本讲知识串起来。

**图 A：`.cu` kernel 与 `.py` 调用入口对应图（必做）**

以 `kernels/relu/` 为例，画出从 `relu_f32_kernel`（GPU）到 `lib.relu_f32`（Python）的完整四层接力图，并在每条箭头上标注「谁调用谁」「发生在 CPU 还是 GPU」。要求至少包含 `relu_f32` 和 `relu_f16x8_pack` 两条链路。可参考 4.3.4 的答案表扩展为流程图。

**图 B：`kernels/` 目录结构标注图（必做）**

新建一份目录结构图，标注 `kernels/` 下**至少 10 个**算子目录及其作用。下面给出实际存在的目录清单供你挑选（共 30 个，均已确认存在）：

```
kernels/
├── interview/      notes-v2.cu 单文件学习骨架（8 Phase）
├── elementwise/    逐元素加（f32/f16 + 向量化）
├── relu/  sigmoid/  elu/  gelu/  swish/  hardswish/  hardshrink/   各类激活函数
├── histogram/      直方图（atomicAdd 练习）
├── embedding/      Embedding 查表
├── reduce/         warp/block 归约原语
├── dot-product/    点积（归约 + 向量化）
├── softmax/        naive/safe/online softmax
├── rms-norm/  layer-norm/   归一化层
├── rope/           旋转位置编码
├── mat-transpose/  矩阵转置（含 CuTe 版）
├── sgemv/  hgemv/  矩阵向量乘（FP32 / FP16）
├── sgemm/  hgemm/  矩阵乘（FP32 / FP16，含 Tensor Core）
├── flash-attn/     FlashAttention（纯 MMA PTX）
├── swizzle/        SMEM swizzle 专项演示
├── ws-hgemm/       Warp Specialization HGEMM
├── nvidia-nsight/  ncu 性能分析教程（bank_conflicts 等）
├── openai-triton/  Triton kernel（vector-add/softmax/layer-norm/...）
└── cutlass/  transformer/  nms/   其它（CUTLASS 示例、Transformer 算子、NMS）
```

要求：从上面挑 ≥10 个目录，用一句话写出它的作用（可参考上方注释或 README 的 kernel 表），并标注它属于 u1-l1 里提到的哪个难度档（Easy/Medium/Hard…）。提示：难度分级可查 [README.md:259-260](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/README.md#L259-L260) 的 Easy/Medium 与 Hard 段落说明，以及 [README.md:257](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/README.md#L257)（Easy/Medium 涵盖 element-wise/reduce/softmax/norm 等，Hard 段聚焦 sgemv/sgemm/hgemm/flash-attention）。

**验收标准**：

- 图 A 能说清「kernel 不等于 Python 入口，中间有启动函数 + pybind 两层」。
- 图 B 至少 10 个目录、每个一句话作用、标注难度档，且作用描述与 README 分类吻合。

## 6. 本讲小结

- LeetCUDA 顶层按「学习内容」分目录：核心是 `kernels/`，外围 `third-party/`（CUTLASS 依赖）、`others/`（PyTorch 分布式/TRT 扩展）、`slides/`、`docs/`，另有 `HGEMM/`、`ffpa-attn/` 两个子模块。
- `git submodule update --init --recursive` 是拿到 CUTLASS 等子模块内容的前提，否则对应目录为空。
- `kernels/` 下绝大多数算子遵循 **`README.md` + `<kernel>.cu` + `<kernel>.py` 三件套**，阅读顺序固定为 README → .cu → .py。
- 一个 GPU kernel 到 Python 入口要经过**四层接力**：`__global__` kernel → host 启动函数 → `PYBIND11_MODULE` 注册 → `lib.xxx(...)`；Python 调用的是启动函数，不是 kernel 本身。
- 仓库有**两种绑定范式**：简单算子用 `.cu` 内联绑定 + `load()` JIT（如 relu）；大型算子用独立 `pybind/*.cc` + `setup.py` 预编译（如 hgemm/flash-attn，按优化路线再分 `naive/`、`wmma/`、`mma/`、`wgmma/` 等子目录）。
- 大型算子的每个子目录代表一条优化路线，是 README 特性表「实体化」的结果——同一算子多版本对照学习是本仓库的核心方法论。

## 7. 下一步学习建议

- **横向熟悉更多三件套**：按图 B 的清单，挑 2~3 个 Easy 算子（如 `elementwise/`、`sigmoid/`）走一遍 README → .cu → .py，巩固约定。
- **进入 CUDA 编程模型**：下一讲 [u2-l1 线程层次：grid/block/warp](./u2-l1-thread-hierarchy.md) 将以 `relu.cu` 的 `relu_f32_kernel` 为例，正式讲解 `blockIdx`/`threadIdx` 如何映射到数据索引——本讲的目录认知是那里的前提。
- **绑定机制留到后面**：若想深入 pybind 与 `setup.py` 的细节（如 `CUDAExtension` 参数、`TORCH_BINDING` 宏展开），可在 U3 单元的 [u3-l1 PyTorch CUDAExtension 与 pybind 绑定机制](./u3-l1-pytorch-cuda-extension.md) 系统学习。
- **大型算子先睹为快**：学完基础后，可对照本讲的 hgemm 子目录结构，直接浏览 [kernels/hgemm/README.md](https://github.com/xlite-dev/LeetCUDA/blob/7d9ce2adc2c532390525fd91d0feff57567737ab/kernels/hgemm/README.md) 的特性表，建立对 HGEMM 学习路线的整体印象（具体 kernel 留待 U9–U13）。
