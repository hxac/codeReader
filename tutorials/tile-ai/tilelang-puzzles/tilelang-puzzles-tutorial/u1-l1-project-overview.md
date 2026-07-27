# 项目总览与环境准备

## 1. 本讲目标

本讲是整个《TileLang Puzzles 学习手册》的第一篇。读完本讲，你应当能够：

- 说清楚 **TileLang 是什么**，以及 **TileLang Puzzles** 这个项目想教你什么、它的灵感来自哪里。
- 看懂仓库的目录结构，特别是 `puzzles/`（题目，含 `TODO`）和 `ans/`（参考答案）之间的对照关系。
- 完成本机环境配置，并能够用 `scripts/check_tilelang_env.py` 验证 GPU 与 TileLang 是否可用。
- 独立运行第一个参考 kernel（`ans/01-copy.py`），为后续逐个攻克 puzzle 打好基础。

本讲**不会**深入讲解 kernel 内部语法（那是后续讲义的任务），只帮你「认识项目、跑通环境」。

---

## 2. 前置知识

在开始前，建议你大致了解下面几个概念（不必精通）：

- **GPU 与 kernel**：GPU 上运行的小程序通常叫做 kernel。深度学习里大量的矩阵乘、softmax、attention 都被写成高性能 kernel。
- **DSL（领域特定语言）**：专门为某一类问题设计的语言。TileLang 就是一种「写 GPU kernel」的 DSL，它用 Python 风格的写法，最终编译成高效的 CUDA 代码。
- **Python / PyTorch**：本项目用 Python 编写 kernel 声明，用 PyTorch 张量（`torch.Tensor`）作为输入输出，并与 PyTorch 的参考结果做正确性比对。
- **JIT（即时编译）**：程序运行时才把代码编译成机器码。TileLang 用 `@tilelang.jit` 装饰器把 Python 函数即时编译成 GPU kernel。

如果你对这些概念还比较陌生也没关系，本讲会从「这是什么」开始讲起。

---

## 3. 本讲源码地图

本讲涉及的文件很少，都是项目入口与说明类文件：

| 文件 | 作用 |
|------|------|
| [README.md](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/README.md) | 项目主页说明：定位、安装、如何运行 puzzle |
| [docs/README.md](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/docs/README.md) | 文档索引：列出 10 个 puzzle、关键概念与推荐学习路线 |
| [scripts/check_tilelang_env.py](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/scripts/check_tilelang_env.py) | 环境自检脚本：打印版本、CUDA 路径，并编译运行一个简单 GEMM |
| [ruff.toml](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ruff.toml) | 代码风格配置（ruff 格式化 / lint 规则） |

另外，本讲的实践会接触到第一个 puzzle 文件 [puzzles/01-copy.py](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/01-copy.py) 与其参考答案 [ans/01-copy.py](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/01-copy.py)，但只做运行层面的体验，不深入语法。

---

## 4. 核心概念与源码讲解

### 4.1 TileLang 与 TileLang Puzzles 的定位

#### 4.1.1 概念说明

**TileLang** 是一个用于开发高性能深度学习 kernel 的领域特定语言（DSL）。它的核心理念是：让你用接近 Python 的写法描述「分块（tile）计算」，由编译器自动处理并行、向量化、Tensor Core 调用、共享内存等底层细节，最终生成接近手写 CUDA 的高效代码。

**TileLang Puzzles** 则是一个教学项目，它把「学习 TileLang」这件事拆成了 **10 个由浅入深的 puzzle（谜题/习题）**。每个 puzzle 给你一个未完成的 kernel（里面有 `TODO`），你需要补全它，使结果和 PyTorch 的参考实现一致。题目从最简单的内存拷贝（copy）开始，一路推进到 GEMM（矩阵乘）、FlashAttention、卷积、INT4 量化矩阵乘等现代 kernel。

这个项目的灵感（见 README 的致谢部分）来自三个类似的教学项目：

- [Triton Puzzles](https://github.com/srush/Triton-Puzzles)
- [Triton Puzzles Lite](https://github.com/SiriusNEO/Triton-Puzzles-Lite)
- [LeetGPU](https://leetgpu.com/)

> 术语提示：「tile（分块）」是 GPU kernel 编程的核心思想——把一个大问题切成许多小块，每块由一个 block（线程块）负责，块内数据可以放进高速的共享内存反复复用。整个手册后续几乎每一讲都在围绕「如何分块」展开。

#### 4.1.2 核心流程

TileLang Puzzles 的学习闭环是这样的：

```text
阅读 puzzle 题面（puzzles/NN-xxx.py 顶部注释 + docs/ 文档）
        │
        ▼
理解它要实现的算子（输入/输出/数学定义）
        │
        ▼
补全 kernel 中的 TODO（用 TileLang DSL 写）
        │
        ▼
运行脚本：test_puzzle 自动和 torch 参考结果比对
        │
        ▼
通过后用 bench_puzzle 测性能（可选）
        │
        ▼
对照 ans/NN-xxx.py 的参考实现学习更优写法
```

关键点：**每个 puzzle 都是「先理解算子 → 再写 kernel → 用框架验证」**，而不是凭空写代码。

#### 4.1.3 源码精读

项目定位最直接的一句话来自 README：

[README.md:5-9](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/README.md#L5-L9) — 项目标题与一句话定位：

```python
# TileLang Puzzles

TileLang Puzzles is a set of puzzles to help you learn TileLang, a domain-specific
language for developing high-performance deep learning kernels. We will start from
some trivial examples and smoothly progress to modern kernels such as GEMM and
FlashAttention, ...
```

这段话说明了三件事：① 学习对象是 TileLang 这个 DSL；② 形式是一组 puzzle；③ 难度从 trivial（拷贝）平滑推进到 GEMM、FlashAttention 等现代 kernel。

灵感来源记录在致谢里：

[README.md:36-38](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/README.md#L36-L38) — 标注了三个灵感项目（Triton Puzzles / Triton Puzzles Lite / LeetGPU），说明本项目延续了「用谜题学 GPU kernel DSL」的教学范式。

每个 puzzle 文件顶部都有一段统一的「题面头注释」，标注分类与难度。例如第一个 puzzle：

[puzzles/01-copy.py:1-9](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/01-copy.py#L1-L9) — 题面头注释，包含题目说明、分类与难度标签：

```python
"""
Puzzle 01: Copy
==============
This puzzle asks you to implement a copy operation that copies data from one
tensor to another.

Category: ["official"]
Difficulty: ["easy"]
"""
```

可以看到每个 puzzle 都带 `Category` 和 `Difficulty` 元信息，方便你快速判断题目性质。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：建立对「puzzle = 带 TODO 的未完成 kernel」这一形式的直觉。
2. **操作步骤**：
   - 打开 [puzzles/01-copy.py](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/01-copy.py)，搜索 `TODO`，记录它出现在哪几个函数里。
   - 打开 [ans/01-copy.py](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/01-copy.py)，对比同样位置：参考答案是**已经填好**的。
3. **需要观察的现象**：`puzzles/` 里多个函数体只有注释 `# TODO: Implement this function`；而 `ans/` 里对应位置有完整的 `with T.Kernel(...)` 和 `T.copy(...)` 代码。
4. **预期结果**：你应当能说出「`puzzles/` 是题目、`ans/` 是答案」这一对照关系，并在 01-copy 中找到至少两处 `TODO`（多线程版与多 block 版）。
5. 本实践为纯阅读，无需运行，结果可直接在源码中确认。

#### 4.1.5 小练习与答案

**练习 1**：TileLang 是一种通用编程语言，还是领域特定语言（DSL）？它面向哪类任务？

> **答案**：TileLang 是领域特定语言（DSL），专门用于开发高性能深度学习 GPU kernel。

**练习 2**：TileLang Puzzles 的教学形式是什么？它和直接读 TileLang 官方文档相比有什么特点？

> **答案**：形式是「一组由浅入深的 puzzle（带 TODO 的未完成 kernel）」。特点是动手驱动——你必须补全代码并用框架验证正确性，而不是被动阅读文档。

---

### 4.2 仓库目录结构与学习路线

#### 4.2.1 概念说明

理解一个项目，最快的方式是先看懂它的目录布局。TileLang Puzzles 的目录非常精简，核心目录只有五个：

| 目录/文件 | 含义 |
|-----------|------|
| `puzzles/` | **题目**：10 个 puzzle 脚本，每个含若干 `TODO` 待你补全 |
| `ans/` | **参考答案**：与 `puzzles/` 一一对应的完整实现 |
| `common/` | **公共工具**：`utils.py` 提供 `test_puzzle`（正确性验证）与 `bench_puzzle`（性能基准）|
| `docs/` | **文档**：每个 puzzle 的中（`zh/`）英（`en/`）讲解与实现指南 |
| `scripts/` | **脚本**：目前只有 `check_tilelang_env.py` 环境自检脚本 |

最关键的一对关系是 **`puzzles/NN-name.py` ↔ `ans/NN-name.py`**：编号与文件名完全对应，前者是空缺题目，后者是参考答案。

> 小提示：`docs/README.md` 第 77 行写作「Compare with reference implementation in `puzzles/ans/`」，但仓库里 `ans/` 实际位于**顶层目录**（不是 `puzzles/ans/`）。这是文档的一处小笔误，以实际目录结构为准即可。

#### 4.2.2 核心流程（推荐学习路线）

`docs/README.md` 给出了一条按难度递进的学习路线：

[docs/README.md:53-63](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/docs/README.md#L53-L63) — 推荐学习顺序图：

```text
Easy:     01 → 02 → 03 → 04 → 05
                  ↓
Medium:   06 → 07 → 08 → 09
                        ↓
Hard:                   10
```

三个难度档的含义（见 [docs/README.md:65-69](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/docs/README.md#L65-L69)）：

- **Easy（01–05）**：基础 GPU 编程概念、内存操作、简单并行。
- **Medium（06–09）**：算法优化、数值稳定、attention、矩阵乘。
- **Hard（10）**：量化技术、真实部署场景。

对应的 10 个 puzzle 主题（节选自 [docs/README.md:40-51](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/docs/README.md#L40-L51)）：

| # | Puzzle | 关键概念 | 难度 |
|---|--------|----------|------|
| 01 | Copy | 内存拷贝、并行线程、线程块 | Easy |
| 02 | Vector Add | 元素级运算、SIMD | Easy |
| 03 | Outer Vector Add | 广播、访存模式 | Easy |
| 04 | Backward Op | 梯度计算、反向传播 | Easy |
| 05 | Reduce Sum | 并行归约 | Easy |
| 06 | Softmax | 数值稳定、online softmax | Medium |
| 07 | Scalar Flash Attention | attention、内存优化 | Medium |
| 08 | Matrix | GEMM、分块、共享内存 | Medium |
| 09 | Conv | 卷积、im2col | Medium |
| 10 | Dequant MM | 量化、INT4 解量化 GEMM | Hard |

本手册的单元划分与这条官方路线一致：第一、二单元覆盖 Easy，第三单元讲归约与 softmax，第四单元讲矩阵乘与 attention，第五单元收尾卷积、量化与性能工程。

#### 4.2.3 源码精读

[docs/README.md:7-32](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/docs/README.md#L7-L32) — 文档目录结构示意：`docs/` 下分 `zh/` 与 `en/` 两套，每个 puzzle 各有一个子目录（如 `1.copy/`、`8.matrix/`），每个子目录里包含「概念讲解」与「实现指南」两篇文档。

[README.md:27-34](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/README.md#L27-L34) — 如何运行一个 puzzle（题目与答案两种方式）：

```bash
python3 puzzles/01-copy.py
python3 ans/01-copy.py
```

这说明每个脚本都是**独立可执行**的——直接 `python3` 运行即可，无需额外入口或构建步骤。

[ruff.toml:1-7](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ruff.toml#L1-L7) — 代码风格配置：行宽 100、缩进 4 空格、目标 Python 3.10、双引号字符串；lint 启用了 `E/W/F/I/B/UP` 并忽略 `E741`（允许单字母变量名，这对 kernel 里大量出现的 `i`、`j`、`k` 很重要）。

```toml
line-length = 100
indent-width = 4
target-version = "py310"

[lint]
select = ["E", "W", "F", "I", "B", "UP"]
ignore = ["E741"]
```

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：建立「题目 ↔ 答案」一一对应的目录直觉。
2. **操作步骤**：列出 `puzzles/` 与 `ans/` 两个目录下的文件（可用 `ls puzzles/ ans/`）。
3. **需要观察的现象**：两个目录下的文件名**完全相同**（`01-copy.py` … `10-dequant-mm.py`）。
4. **预期结果**：共 10 个 puzzle，编号从 01 到 10 一一对应；任选一个编号，`puzzles/NN-x.py` 是题目、`ans/NN-x.py` 是答案。
5. 本实践为目录浏览，结果可直接确认。

#### 4.2.5 小练习与答案

**练习 1**：如果你想看 Puzzle 06（Softmax）的参考实现，应该打开哪个文件？

> **答案**：`ans/06-softmax.py`（题目在 `puzzles/06-softmax.py`）。

**练习 2**：`common/utils.py` 提供了哪两个对学习最关键的工具函数？

> **答案**：`test_puzzle`（把你的 kernel 和 torch 参考结果比对）和 `bench_puzzle`（用 CUDA Event 给 kernel 计时）。它们会在下一讲详细拆解。

**练习 3**：为什么 `ruff.toml` 要特意忽略 `E741`（模糊变量名）这条规则？

> **答案**：因为 kernel 代码里大量使用 `i`、`j`、`k` 这类单字母循环/索引变量（尤其矩阵乘的 K 维），忽略 `E741` 可以避免无意义的 lint 报错。

---

### 4.3 环境检查与运行方式

#### 4.3.1 概念说明

TileLang 会把 Python 写的 kernel 编译成 CUDA 代码再运行，因此你的环境必须满足两个条件：

1. 一块可用的 **NVIDIA GPU**，以及正确配置的 **CUDA**（通过 `CUDA_HOME` 找到）。
2. 正确安装的 **TileLang** 及其依赖。

项目专门提供了一个自检脚本 `scripts/check_tilelang_env.py`，它不只打印版本号，还会**真正编译并运行一个简单 GEMM kernel**——如果这一步通过，说明你的环境可以跑后续所有 puzzle。

#### 4.3.2 核心流程

环境验证的执行流程：

```text
导入 tilelang / torch / tilelang.env
        │
        ▼
tilelang.disable_cache()         # 关闭编译缓存（教学场景便于看到每次编译）
        │
        ▼
__main__ 打印：版本号、Python 安装路径、CUDA_HOME
        │
        ▼
torch.utils.collect_env.main()   # 打印详细 torch/CUDA/GPU 环境信息
        │
        ▼
run_gemm()                        # 编译并运行一个 GEMM kernel 做端到端验证
        │
        ▼
打印 torch.allclose 比对结果与张量形状
```

如果最后一步打印 `Check GEMM result:  True`，说明环境完全可用。

#### 4.3.3 源码精读

先看脚本的主入口，它按顺序做了三件事：

[scripts/check_tilelang_env.py:53-64](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/scripts/check_tilelang_env.py#L53-L64) — 主入口：打印版本与 CUDA 路径，收集 torch 环境，最后运行一个 GEMM：

```python
if __name__ == "__main__":
    print("Installed TileLang version: ", tilelang.__version__)
    print("Installed TileLang Python path: ", tilelang.__path__)
    print("Current CUDA Path: ", env.CUDA_HOME)
    ...
    torch.utils.collect_env.main()
    ...
    run_gemm()
```

`run_gemm()` 才是真正的「端到端验证」——它用 TileLang 写了一个矩阵乘 kernel 并实际运行：

[scripts/check_tilelang_env.py:13-50](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/scripts/check_tilelang_env.py#L13-L50) — 定义并运行一个简单 GEMM，最后用 `torch.allclose` 与 `torch.matmul` 比对：

```python
def run_gemm():
    @tilelang.jit
    def gemm(A, B, block_M: int = 128, block_N: int = 128, block_K: int = 32):
        ...
        with T.Kernel(T.ceildiv(M, block_M), T.ceildiv(N, block_N), threads=128) as (bx, by):
            A_shared = T.alloc_shared((block_M, block_K), A.dtype)
            ...
            for k in T.Pipelined(T.ceildiv(K, block_K), num_stages=3):
                T.copy(A[bx * block_M, k * block_K], A_shared)
                T.copy(B[k * block_K, by * block_N], B_shared)
                T.gemm(A_shared, B_shared, C_local)
            T.copy(C_local, C[bx * block_M, by * block_N])
        return C

    A = rand_torch_tensor((2048, 4096), torch.float16)
    B = rand_torch_tensor((4096, 2048), torch.float16)
    C = gemm(A, B)
    C_torch = torch.matmul(A, B)
    print("Check GEMM result: ", torch.allclose(C, C_torch, atol=1e-3))
```

> 这段代码用到了 `@tilelang.jit`、`T.Kernel`、`T.alloc_shared`、`T.gemm`、`T.Pipelined` 等 TileLang DSL 元素。**你现在不需要看懂它们**——它们分别属于「kernel 骨架」「共享内存」「Tensor Core」「软件流水线」等主题，会在后续讲义逐一讲解。这里只需把它当作一个「能跑通就算环境 OK」的冒烟测试。

脚本第一行关闭了编译缓存：

[scripts/check_tilelang_env.py:10](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/scripts/check_tilelang_env.py#L10) — `tilelang.disable_cache()`，在教学场景下确保每次都重新编译，便于观察。

安装验证的最简方式则记录在 README：

[README.md:15-25](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/README.md#L15-L25) — 安装 TileLang 后，用一行命令或自检脚本确认：

```bash
python -c "import tilelang; print(tilelang.__version__);"
python3 scripts/check_tilelang_env.py
```

#### 4.3.4 代码实践（运行型）

这是本讲的核心实践，**需要在本机执行**。如果你当前没有 GPU 环境，可先阅读步骤，标注「待本地验证」。

1. **实践目标**：确认 TileLang 与 GPU 可用，并跑通第一个参考 kernel。
2. **操作步骤**：
   - 安装 TileLang（参考 [TileLang 仓库](https://github.com/tile-ai/tilelang) 的安装说明）。
   - 运行版本检查：
     ```bash
     python -c "import tilelang; print(tilelang.__version__)"
     ```
   - 运行环境自检脚本：
     ```bash
     python3 scripts/check_tilelang_env.py
     ```
   - 运行第一个参考答案 kernel：
     ```bash
     python3 ans/01-copy.py
     ```
3. **需要观察的现象**：
   - `check_tilelang_env.py` 应打印 TileLang 版本号、`CUDA_HOME` 路径、torch 环境摘要，并在最后打印 `Check GEMM result:  True`。
   - `ans/01-copy.py` 应打印若干 `✅ Results match: True`（来自 `test_puzzle` 的正确性比对）以及 `bench_puzzle` 的耗时。
4. **预期结果**：版本号被成功打印；GEMM 比对为 `True`；01-copy 的三组测试（serial / multi-threads / parallel）都显示匹配。
5. **待本地验证**：具体的版本号字符串、CUDA 路径与耗时数值依赖你的机器，无法在此预先确定，请以本地实际输出为准并记录下来。

> ⚠️ 注意：你**不应该**修改任何源码来完成本实践。本讲只做「运行 + 观察」。修改 puzzle、补全 TODO 是从下一讲（Puzzle 01 Copy）才开始的任务。

#### 4.3.5 小练习与答案

**练习 1**：`check_tilelang_env.py` 最后调用的 `run_gemm()` 的作用是什么？为什么它比「只打印版本号」更能说明环境可用？

> **答案**：`run_gemm()` 会真正用 TileLang 编译并运行一个 GEMM kernel，再和 `torch.matmul` 比对结果。它验证了「编译 + GPU 执行 + 数值正确」整条链路，而打印版本号只能说明包被导入成功。

**练习 2**：脚本里 `tilelang.disable_cache()` 在教学场景下有什么好处？

> **答案**：关闭编译缓存后，每次运行都会重新编译 kernel，便于你观察到「代码改动 → 重新编译 → 新的 CUDA 代码」的完整过程，避免被旧缓存误导。

**练习 3**：如果你想直接确认 TileLang 的版本号而不运行任何 kernel，用哪条命令？

> **答案**：`python -c "import tilelang; print(tilelang.__version__)"`。

---

## 5. 综合实践

把本讲的知识串起来，完成一次「从零到跑通」的环境验收：

1. **克隆并进入仓库**，确认你能看到 `puzzles/`、`ans/`、`common/`、`docs/`、`scripts/` 这五个核心目录。
2. **填写一张「环境信息表」**（请在本地运行后补全，未知项标注「待本地验证」）：

   | 项目 | 你的值 |
   |------|--------|
   | TileLang 版本 (`tilelang.__version__`) | 待本地验证 |
   | `CUDA_HOME` 路径 | 待本地验证 |
   | GPU 型号（来自 `collect_env`） | 待本地验证 |
   | `check_tilelang_env.py` 的 GEMM 比对结果 | 待本地验证（期望 `True`） |

3. **运行第一个参考 kernel**：`python3 ans/01-copy.py`，记录三组测试是否全部显示 `✅ Results match: True`，以及 `bench_puzzle` 报告的耗时（待本地验证）。
4. **对照题目与答案**：打开 `puzzles/01-copy.py` 与 `ans/01-copy.py`，确认前者的 `tl_copy_1d_multi_threads` 与 `tl_copy_1d_parallel` 是 `TODO`，而后者已填好。
5. **产出**：用一句话总结「我的环境是否就绪，下一步准备攻克哪个 puzzle」。

完成本综合实践后，你就拥有了学习后续所有讲义所需的运行环境与项目认知。

---

## 6. 本讲小结

- **TileLang** 是一种用于编写高性能深度学习 GPU kernel 的 DSL；**TileLang Puzzles** 用 10 个递进 puzzle 教你掌握它，灵感来自 Triton Puzzles 等。
- 仓库核心目录：`puzzles/`（题目，含 TODO）、`ans/`（参考答案）、`common/`（`test_puzzle` / `bench_puzzle` 工具）、`docs/`（中英文讲解）、`scripts/`（环境自检）。
- 每个 puzzle 都是独立可执行脚本，题目与答案通过编号一一对应（`puzzles/NN-x.py` ↔ `ans/NN-x.py`）。
- 推荐学习路线按难度分三档：Easy（01–05）→ Medium（06–09）→ Hard（10），本手册的单元划分与之对齐。
- 环境验证的关键是运行 `scripts/check_tilelang_env.py`——它会真正编译运行一个 GEMM，比对通过即说明环境可用。
- 代码风格由 `ruff.toml` 约定（行宽 100、Python 3.10、忽略 `E741` 以兼容 kernel 里的单字母变量）。

---

## 7. 下一步学习建议

环境跑通后，建议按下面顺序继续：

1. **下一讲 [u1-l2] TileLang Kernel 骨架与测试/基准框架**：拆解一个 TileLang kernel 的声明骨架（`@tilelang.jit`、`T.Tensor`、`T.empty`、`T.Kernel`），并深入讲解 `common/utils.py` 里的 `test_puzzle` 与 `bench_puzzle` 如何工作。这是理解所有后续 puzzle 的「公共地基」。
2. 在阅读下一讲前，可以先快速浏览 [puzzles/01-copy.py](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/01-copy.py) 顶部的长注释，它已经预告了 kernel 声明与 `T.Kernel` 的用法。
3. 如果想提前了解某个算子的背景，可以翻阅 [docs/zh/](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/docs/README.md) 下对应 puzzle 的概念讲解文档，但**实现细节请以本手册各讲为准**。
