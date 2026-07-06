# 构建系统：CMake 与构建选项

> 本讲是 `u1-l2`，承接 [u1-l1 FasterTransformer 项目总览](u1-l1-project-overview.md)。在上一讲我们已经知道：FasterTransformer（下文简称 FT）是用 CUDA/cuBLAS/cuBLASLt + C++ 写的推理加速库，支持 BERT/GPT/T5 等多种模型，并能通过 PyTorch / TensorFlow / Triton / TensorRT 四种外壳使用。本讲要回答的问题是：**这么大一坨 C++/CUDA 代码，到底怎么编译成可以跑的程序？**

## 1. 本讲目标

学完本讲，你应当能够：

1. 读懂 FT 顶层 `CMakeLists.txt` 里的 `option(...)` 体系，知道每个开关控制什么。
2. 区分「框架开关」（`BUILD_PYT` / `BUILD_TF` / `BUILD_TRT`）、「并行开关」（`BUILD_MULTI_GPU`）、「精度开关」（`ENABLE_FP8` / `SPARSITY_SUPPORT`）三组选项。
3. 理解 CUDA 版本如何自动触发 BF16 / FP8 的**条件编译**（`add_definitions`）。
4. 看 GPU 的 compute capability（SM）如何决定编译目标架构，以及为什么 `-DSM` 不能随便填一大堆。
5. 根据 `docs/gpt_guide.md` 的 Build 章节，独立写出「单 GPU PyTorch 扩展」与「多 GPU + FP8 C++ 工程」两条 `cmake` 命令，并逐个解释 `-D` 参数。

## 2. 前置知识

在进入源码前，先用大白话建立三个直觉。

### 2.1 什么是 CMake，为什么需要它

C/C++ 项目没有 Python 那种「`pip install` 就能跑」的简洁。一个 `.cu`（CUDA 源文件）要变成 GPU 上跑的机器码，需要：编译器（`nvcc`/`g++`）、头文件路径、链接库（`-lcudart -lcublas`）、目标 GPU 架构（`sm_80` 等）。**CMake 就是一个「生成编译指令」的元工具**：你在 `CMakeLists.txt` 里用接近自然语言的命令描述「我要编译什么、带哪些开关」，它就给你生成 `Makefile`，然后你用 `make` 真正编译。

典型的三步走是：

```bash
mkdir build && cd build      # 1. 建一个独立的构建目录（隔离产物）
cmake ..                     # 2. 让 CMake 读上一级的 CMakeLists.txt，生成 Makefile
make -j12                    # 3. 真正编译，-j12 表示用 12 个核并行
```

`cmake ..` 这一步里，你可以用 `-DXXX=YYY` 向 CMake 传变量。本讲的核心就是搞清楚 FT 接受哪些 `-D` 变量。

### 2.2 「条件编译」是什么意思

FT 是一个支持很多硬件和很多精度的库。比如 FP8 只有 Hopper（sm_90）+ CUDA 11.8 以上才支持；NCCL 通信只有多 GPU 才需要。如果把所有代码都编进去，会变成一个巨大无比、还编不过的可执行文件。

所以 FT 用**条件编译**：在 `CMakeLists.txt` 里用 `add_definitions("-DENABLE_FP8")` 给编译器注入一个宏 `ENABLE_FP8`，源代码里再用 `#ifdef ENABLE_FP8` 决定是否编译 FP8 那段代码。这样关掉 FP8 时，相关代码完全不参与编译，体积小、速度快。

换句话说，`-DENABLE_FP8=ON` 不是「运行时开关」，而是「**编译时**决定要不要把 FP8 代码编进来」。开关一旦定下，编出的 `.so` 就定型了。

### 2.3 GPU 架构与 SM 是什么

NVIDIA GPU 每一代都有一个「compute capability」，写成 `sm_XX`，比如：

| GPU | compute capability | 一代 |
|-----|-----|-----|
| V100 | 70 | Volta |
| T4 | 75 | Turing |
| A100 / A30 | 80 | Ampere |
| A10 | 86 | Ampere |
| H100 | 90 | Hopper |

`nvcc` 编译 `.cu` 时必须告诉它「为哪个架构生成代码」。为越多种架构编译，产物越大、编译越慢。所以 FT 允许你用 `-DSM=80` 只编你手上那张卡，节省时间。这也是后面会反复出现的概念。

> 前置术语小结：`CMake`（构建元工具）、`option`（CMake 里的编译开关）、`add_definitions`（注入编译宏）、`SM`（GPU 架构代号）、`NGC 容器`（NVIDIA 官方预装好 CUDA/PyTorch 的 Docker 镜像，如 `nvcr.io/nvidia/pytorch:22.09-py3`）。

## 3. 本讲源码地图

本讲只读三个文件，它们正好构成「配置 → 文档命令 → 产物」的完整链路：

| 文件 | 作用 | 本讲关注点 |
|------|------|-----------|
| [CMakeLists.txt](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/CMakeLists.txt) | 顶层构建脚本，定义所有 `option` 与条件编译 | option 体系、BF16/FP8 条件、SM 设置、最终产物 `transformer-shared` |
| [docs/gpt_guide.md](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/docs/gpt_guide.md) | GPT 使用手册，含 Setup / Build 章节 | Requirements、`cmake` 命令模板、FP8 实验性构建 |
| [docs/bert_guide.md](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/docs/bert_guide.md) | BERT 使用手册，含 Setup / Build 章节 | 与 GPT 对照的环境要求、Sparsity 构建参数 |

`src/`、`examples/`、`tests/` 等子目录虽然也通过 `add_subdirectory` 被纳入编译，但它们的编译内容取决于顶层这几个开关——所以本讲聚焦顶层 `CMakeLists.txt` 即可。

## 4. 核心概念与源码讲解

本讲按三个最小模块拆分：

- **4.1 option 体系总览**：FT 有哪些编译开关，怎么分类。
- **4.2 条件编译与 CUDA 版本**：BF16/FP8/Sparsity/SM 如何被自动或手动启用。
- **4.3 构建命令实战**：把 `docs` 里的 `cmake` 命令逐行翻译成「为什么这么写」。

### 4.1 CMake 选项总览：option 体系

#### 4.1.1 概念说明

FT 的构建脚本用 CMake 的 `option(NAME "描述" 默认值)` 语法定义开关。`option` 的本质是一个**布尔缓存变量**，默认值可以是 `ON`/`OFF`，但用户在命令行用 `-DNAME=ON` 可以覆盖它。脚本里再用 `if(NAME) ... endif()` 根据开关决定要不要 `add_definitions`、要不要 `find_package` 某个依赖。

我们可以把 FT 的所有 option 分成四组：

1. **框架外壳**：决定编出来的库给谁用。
   - `BUILD_PYT`（PyTorch TorchScript 扩展）、`BUILD_TF` / `BUILD_TF2`（TensorFlow）、`BUILD_TRT`（TensorRT plugin）。
2. **并行能力**：`BUILD_MULTI_GPU`（多 GPU/多节点，依赖 MPI + NCCL）。
3. **精度与特性**：`ENABLE_FP8`（FP8 推理）、`SPARSITY_SUPPORT`（2:4 稀疏，Ampere 起支持）、`BUILD_FAST_MATH`（`--use_fast_math`）。
4. **工程辅助**：`BUILD_CUTLASS_MOE` / `BUILD_CUTLASS_MIXED_GEMM`（CUTLASS 高性能 GEMM，默认 ON 但会显著拉长编译时间）、`USE_NVTX`（性能标记）、`GIT_AUTOCLONE_CUTLASS`（自动拉取 cutlass 子模块）、`USE_TRITONSERVER_DATATYPE`、`MEASURE_BUILD_TIME`。

注意一个重要事实：**所有框架/精度 option 默认都是 `OFF`**（除了 CUTLASS、FAST_MATH、NVTX 这些「纯增益」的默认 ON）。这意味着如果你只是空跑 `cmake ..`，得到的是一个**最朴素的单 GPU、C++、FP32/FP16 库**——要更多能力就得显式开。

#### 4.1.2 核心流程

一个 option 从「声明」到「生效」要经过三步，可以用下面的伪流程表示：

```
option(BUILD_XXX "..." OFF)        # ① 声明开关，默认 OFF
        │
        ▼  用户在命令行写 -DBUILD_XXX=ON
if(BUILD_XXX)                      # ② 检测开关
    add_definitions("-DBUILD_XXX") # ③a 注入编译宏（影响源码里的 #ifdef）
    find_package(MPI REQUIRED)     # ③b 或：拉取/校验依赖（MPI、NCCL、Torch…）
    list(APPEND ... 头文件/库路径)
endif()
```

要点：第 ③a 步的宏会进到每一个 `.cc/.cu` 文件里，所以源码里能用 `#ifdef BUILD_MULTI_GPU` 来条件性地编入多 GPU 代码；第 ③b 步是「带代价」的——比如开 `BUILD_MULTI_GPU` 就必须装好 MPI 和 NCCL，否则 CMake 阶段直接报错退出。

#### 4.1.3 源码精读

先看顶部的环境基线与 FP8 的 option 声明（注意 FP8 比较特殊，它的 `option` 被包在一个 CUDA 版本判断里，详见 4.2）：

[CMakeLists.txt:L14-L30](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/CMakeLists.txt#L14-L30) — 这段做两件事：①要求 CUDA ≥ 10.2；②检测 CUDA 版本，≥11 自动开 BF16 宏，≥11.8（或 ≥12）才**声明** `ENABLE_FP8` 这个 option。

再看核心的 option 集群，这是本讲最重要的代码块：

[CMakeLists.txt:L35-L57](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/CMakeLists.txt#L35-L57) — 这里集中声明了 `BUILD_CUTLASS_MOE`、`BUILD_CUTLASS_MIXED_GEMM`、`BUILD_TF`/`BUILD_TF2`/`BUILD_PYT`/`BUILD_TRT`、`GIT_AUTOCLONE_CUTLASS`、`BUILD_MULTI_GPU`、`USE_TRITONSERVER_DATATYPE` 一大批开关。注意它们**默认几乎全是 OFF**。另外两个细节：

- `BUILD_MULTI_GPU` 外面套了 `if(NOT BUILD_MULTI_GPU)`，是为了避免重复 `option` 声明报错——有时父项目会预先设这个变量。
- `BUILD_CUTLASS_MOE` / `BUILD_CUTLASS_MIXED_GEMM` 默认 `ON`，紧跟着就 `add_definitions`，所以即使你不传任何 `-D`，CUTLASS 相关 GEMM 也会被编进来（这就是 FT 首次编译比较慢的原因之一）。

接着是 Sparsity 与多 GPU 依赖处理：

[CMakeLists.txt:L75-L86](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/CMakeLists.txt#L75-L86) — `SPARSITY_SUPPORT` 描述里写明是「Ampere sparsity feature」；`BUILD_MULTI_GPU=ON` 时会 `find_package(MPI REQUIRED)` 和 `find_package(NCCL REQUIRED)`，缺一就直接失败。这正是「开开关 = 接受额外依赖」的典型例子。

> 小贴士：`SPARSITY_SUPPORT=ON` 时还必须提供 `CUSPARSELT_PATH`（见 4.2.3），否则找不到头文件。

#### 4.1.4 代码实践

**目标**：把 FT 的 option 体系整理成一张「速查表」，建立肌肉记忆。

**操作步骤**：

1. 打开 [CMakeLists.txt](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/CMakeLists.txt)，搜索所有 `option(`（IDE 里 Ctrl-F，或命令行 `grep -n "option(" CMakeLists.txt`）。
2. 对每个 option 记录四列：**名字 / 默认值 / 属于哪一组（框架/并行/精度/辅助）/ 需要的额外依赖**。

**预期产物（节选）**：

| option | 默认 | 组别 | 额外依赖 |
|--------|------|------|---------|
| `BUILD_PYT` | OFF | 框架 | PyTorch ≥ 1.5 |
| `BUILD_TF` | OFF | 框架 | TF_PATH 指向的 TensorFlow |
| `BUILD_MULTI_GPU` | OFF | 并行 | MPI + NCCL |
| `ENABLE_FP8` | OFF | 精度 | CUDA ≥ 11.8、Hopper(sm_90) |
| `SPARSITY_SUPPORT` | OFF | 精度 | cuSPARSELt、Ampere(sm_80/86) |
| `BUILD_CUTLASS_MOE` | ON | 辅助 | cutlass 子模块（编译变慢） |

**需要观察的现象**：注意「精度/框架」类默认 OFF，「纯增益」类（CUTLASS、FAST_MATH、NVTX）默认 ON——这个规律能帮你预测一条陌生 `cmake` 命令会编出什么。

#### 4.1.5 小练习与答案

**练习 1**：用户运行 `cmake ..` 时**不传任何 `-D`**，最终编出来的库具备哪些能力？
**答案**：单 GPU、纯 C++、不带 PyTorch/TF/TRT 外壳、不带 FP8/Sparsity、但带 CUTLASS MoE/Mixed GEMM、带 NVTX、带 fast math。多 GPU 与框架集成全部缺省关闭。

**练习 2**：为什么 `BUILD_CUTLASS_MOE` 默认是 `ON`，而 `BUILD_MULTI_GPU` 默认是 `OFF`？
**答案**：CUTLASS GEMM 是「纯软件、无外部依赖」的性能增益，默认开启对多数用户有利；而 `BUILD_MULTI_GPU` 需要 MPI + NCCL 这两个外部库，没装就会 `FATAL_ERROR`，所以默认关闭以免给单卡用户制造障碍。

---

### 4.2 条件编译与 CUDA 版本：BF16 / FP8 / Sparsity / SM

#### 4.2.1 概念说明

4.1 讲的是「用户主动开的开关」。本模块讲的是另一类：**CMake 根据环境自动决定要不要注入某个宏**。这通常发生在两种情况下：

1. **硬件/CUDA 版本依赖**：BF16 需要 CUDA ≥ 11；FP8 需要 CUDA ≥ 11.8 且 Hopper 架构。低版本强行开也用不了，不如让 CMake 自动判断。
2. **GPU 架构（SM）依赖**：某些指令（如 WMMA 矩阵核心、2:4 稀疏）只在特定 SM 以上才存在，必须按你指定的架构编出对应 PTX。

理解这一块的关键是区分两类宏：

- **`ENABLE_BF16`**：由 CMake 根据 CUDA 版本**自动**注入，用户一般不直接控制。
- **`ENABLE_FP8`**：是 `option`，用户要 `-DENABLE_FP8=ON` 才生效；但它的**声明本身**被 CUDA 版本守卫——CUDA 太旧时这个 option 根本不存在，你 `-DENABLE_FP8=ON` 也不会有 FP8 宏。

#### 4.2.2 核心流程

BF16 与 FP8 的自动判断逻辑（与源码一致）：

```
CUDA 主版本 ≥ 11            →  自动 add_definitions("-DENABLE_BF16")   # 无条件注入
CUDA ≥ 11.8  或  ≥ 12.0     →  声明 option(ENABLE_FP8 OFF)            # 此时才能开
                                    └─ 用户再 -DENABLE_FP8=ON 才注入 -DENABLE_FP8
```

SM（目标架构）的处理则是另一条逻辑：

```
SM_SETS = {52, 60, 61, 70, 75, 80, 86, 89, 90}    # FT 支持的全部架构
对用户 -DSM=xx 里出现的每个 xx：
   生成 -gencode=arch=compute_xx,code="sm_xx,compute_xx"
   若 xx ∈ {70,75,80,86,89,90} → 启用 WMMA（矩阵核心）宏
若用户没传 -DSM → 退回到默认 {70,75,80,86}
```

这里有一个性能权衡公式（只是直观描述，不是精确度量）：

\[
T_{\text{compile}} \;\propto\; \text{(目标架构数量)} \times \text{(开启的特性数)}
\]

也就是说，架构越多、CUTLASS/FP8 越多，编译时间越长。这就是文档反复劝你「只设你那张卡的 `-DSM`」的原因。

#### 4.2.3 源码精读

**BF16/FP8 自动注入**（本模块最关键的一段）：

[CMakeLists.txt:L19-L30](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/CMakeLists.txt#L19-L30) — 第 19–22 行：CUDA ≥ 11 时无条件 `add_definitions("-DENABLE_BF16")`。第 24–30 行：只有 CUDA ≥ 11.8（或主版本 ≥ 12）时，才 `add_definitions("-DENABLE_FP8")` 并声明 `option(ENABLE_FP8 ... OFF)`；再由 `if(ENABLE_FP8)` 决定是否打印提示。**注意**：这里的 `add_definitions("-DENABLE_FP8")` 在 L25 是无条件执行的（只要 CUDA 版本够），而 `option` 默认 OFF 只是控制日志，真正的「是否链接 FP8 目标」在文件末尾 L432 的 `if(ENABLE_FP8)`。这是一个容易看错的细节，建议结合 4.3.3 一起理解。

**Sparsity 的宏与路径**：

[CMakeLists.txt:L223-L227](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/CMakeLists.txt#L223-L227) — `SPARSITY_SUPPORT=ON` 时把 `CUSPARSELT_PATH` 的 include/lib 加入搜索路径，并注入 `-DSPARSITY_ENABLED=1`。注意这个宏是 `SPARSITY_ENABLED`，和 option 名 `SPARSITY_SUPPORT` 不完全一致，源码里 `#ifdef` 用的是前者。

**SM 架构循环**：

[CMakeLists.txt:L129-L155](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/CMakeLists.txt#L129-L155) — `SM_SETS` 列出全部支持的架构；`foreach` 把用户传入的 `-DSM`（如 `80` 或 `80;86`）逐个翻译成 `-gencode`，并在架构属于 WMMA 范围时打开矩阵核心。

[CMakeLists.txt:L164-L180](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/CMakeLists.txt#L164-L180) — 当用户**完全没传** `-DSM` 时（`FIND_SM` 仍为 False），退回到默认架构集合 `{70, 75, 80, 86}` 并启用 WMMA。这就是文档里「默认 70/75/80/86」的来源。

#### 4.2.4 代码实践

**目标**：理解「CUDA 版本不足时，开 FP8 也无效」这一容易踩的坑。

**操作步骤**：

1. 假设你在一台 CUDA 11.0 的机器上运行：
   ```bash
   cmake -DSM=80 -DCMAKE_BUILD_TYPE=Release -DENABLE_FP8=ON ..
   ```
2. 对照 [CMakeLists.txt:L24-L30](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/CMakeLists.txt#L24-L30) 走一遍逻辑：CUDA 11.0 不满足 `(≥11 且 ≥11.8) 或 ≥12`，所以 `option(ENABLE_FP8 ...)` 根本不会被声明。
3. 推断：`-DENABLE_FP8=ON` 这条变量被你传进去了，但脚本里没有任何 `if(ENABLE_FP8)` 会被触发，末尾 L432 的 FP8 链接段也不会执行。

**需要观察的现象 / 预期结果**：编出来的 `transformer-shared` 里**不含**任何 FP8 目标（`GptFP8`、`cublasFP8MMWrapper` 等）。运行带 `--data_type fp8` 的示例会找不到符号或报错。

**待本地验证**：如果你手头有 CUDA 11.0 与 CUDA 11.8+ 两台机器，可以分别构建并用 `nm lib/libtransformer-shared.so | grep FP8` 看符号差异，直观对比。

#### 4.2.5 小练习与答案

**练习 1**：CUDA 11.2 环境下，FT 是否支持 BF16？是否支持 FP8？
**答案**：BF16 支持（CUDA ≥ 11 自动注入 `ENABLE_BF16`）；FP8 不支持（CUDA 11.2 < 11.8，`ENABLE_FP8` option 未声明）。

**练习 2**：用户写 `-DSM="70;75;80;86;89;90"` 比 `-DSM=80` 慢，为什么？
**答案**：每个 SM 都要单独 `-gencode` 生成一份 PTX/cubin，6 个架构意味着同样的 `.cu` 被编译 6 遍。编译时间近似与架构数成正比。生产环境只编目标卡的架构最快。

**练习 3**：`SPARSITY_SUPPORT=ON` 后，源码里判断稀疏特性用的是哪个宏？
**答案**：`SPARSITY_ENABLED`（值为 1），不是 option 名 `SPARSITY_SUPPORT`。见 [CMakeLists.txt:L223-L227](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/CMakeLists.txt#L223-L227)。

---

### 4.3 构建命令实战：从 docs 翻译 cmake 命令

#### 4.3.1 概念说明

知道了选项体系（4.1）和条件编译（4.2），本模块把它们拼成「真实可运行的命令」。FT 的 `docs/gpt_guide.md` 与 `docs/bert_guide.md` 都有 **Setup → Build the FasterTransformer → Build the project** 三段式结构：

1. **Requirements**：列出 CMake/CUDA/Python/框架版本底线。
2. **Prepare**：拉 NGC 容器、`git clone`、`mkdir build`、`git submodule update`（拉 cutlass）。
3. **Build the project**：给出 C++ / TensorFlow / PyTorch 三种 `cmake` 命令模板。

两份 guide 的 Requirements 几乎一致（CMake ≥ 3.8 做纯 C++/TF，≥ 3.13 做 PyTorch；CUDA ≥ 11.0）。差异只在 GPT 多一个 NCCL ≥ 2.10（因为多 GPU GPT 是主推场景）。

#### 4.3.2 核心流程

一条完整的 FT 构建命令，从环境到产物，经过这几步：

```
① 进 NGC 容器（CUDA/PyTorch 都装好了）
   nvidia-docker run ... nvcr.io/nvidia/pytorch:22.09-py3
② 拉源码 + 子模块（cutlass 必需）
   git clone ... && cd FasterTransformer/build && git submodule update --init
③ cmake ..（本讲主角，用 -D 选框架/并行/精度/SM）
④ make -j12（并行编译）
⑤ 产物：build/lib/libtransformer-shared.so（核心库）
        build/bin/<model>_example（C++ 示例可执行）
        若 BUILD_PYT：还会产出 libth_transformer.so（PyTorch 扩展）
```

最终所有模型/层/kernel 的目标文件（object）都被聚合成一个巨大的共享库 `transformer-shared`，框架扩展（如 PyTorch 的 `th_transformer`）再链接它。这就是为什么改一个 `-D` 往往要重新 `cmake .. && make`。

#### 4.3.3 源码精读

**Requirements 底线**：

[docs/gpt_guide.md:L211-L224](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/docs/gpt_guide.md#L211-L224) — GPT 的环境要求：CMake ≥ 3.8（TF）或 ≥ 3.13（PyTorch，注意这正好呼应 `CMakeLists.txt` 第 14 行注释「for PyTorch extensions, version should be greater than 3.13」）、CUDA ≥ 11.0、NCCL ≥ 2.10、Python 3、PyTorch ≥ 1.5。

**Prepare（建 build 目录、拉子模块）**：

[docs/gpt_guide.md:L242-L256](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/docs/gpt_guide.md#L242-L256) — 推荐 NGC TF 镜像，`mkdir -p FasterTransformer/build`、`git submodule init && git submodule update`。注意：顶层 [CMakeLists.txt:L60-L70](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/CMakeLists.txt#L60-L70) 在 `GIT_AUTOCLONE_CUTLASS=ON`（默认）时也会自动拉 cutlass，所以即使忘手动 update，CMake 也会补一刀。

**Build the project（C++ / TF / PyTorch 三种模板）**：

[docs/gpt_guide.md:L258-L297](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/docs/gpt_guide.md#L258-L297) — 三条命令模板：
- C++：`cmake -DSM=xx -DCMAKE_BUILD_TYPE=Release -DBUILD_MULTI_GPU=ON ..`
- TF：加 `-DBUILD_TF=ON -DTF_PATH=...`
- PyTorch：`cmake -DSM=xx -DCMAKE_BUILD_TYPE=Release -DBUILD_PYT=ON -DBUILD_MULTI_GPU=ON ..`，且要求 PyTorch ≥ 1.5（呼应 [CMakeLists.txt:L246-L248](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/CMakeLists.txt#L246-L248) 的版本检查）。

> 注意：`-DSM=xx` 里的 `xx` 是占位符，要换成你 GPU 的 compute capability（见 [docs/gpt_guide.md:L262-L272](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/docs/gpt_guide.md#L262-L272) 的对照表：V100→70、T4→75、A100→80、A10→86）。文档建议只设你实际用的那张卡，编译最快。

**TF 构建的硬约束**：

[CMakeLists.txt:L111-L113](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/CMakeLists.txt#L111-L113) — `BUILD_TF`/`BUILD_TF2=ON` 时必须提供 `TF_PATH`，否则 `FATAL_ERROR`。这就是 TF 模板里一定要带 `-DTF_PATH=...` 的原因。

**PyTorch 的 ABI 自动对齐**：

[CMakeLists.txt:L242-L282](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/CMakeLists.txt#L242-L282) — `BUILD_PYT=ON` 时，CMake 会用 Python 探测 PyTorch 版本与安装路径，并自动读 `torch._C._GLIBCXX_USE_CXX11_ABI` 把 C++ 的 `_GLIBCXX_USE_CXX11_ABI` 对齐到和你装的 PyTorch 一致——否则编出的 `.so` 加载时会报符号找不到。这是「框架集成需要在编译期对齐 ABI」的典型细节，也是为什么不能随便换 PyTorch 版本而不重编。

**FP8 的实验性构建（Hopper 专用）**：

[docs/gpt_guide.md:L940-L947](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/docs/gpt_guide.md#L940-L947) — FP8 需 Hopper + CUDA 11.8，用 `nvcr.io/nvidia/pytorch:22.10-py3` 镜像，命令：
```bash
cmake -DSM=90 -DCMAKE_BUILD_TYPE=Release -DBUILD_PYT=ON -DBUILD_MULTI_GPU=ON -DENABLE_FP8=ON ..
```
这里 `-DSM=90` 是硬性要求（FP8 是 Hopper 指令）。对应到 [CMakeLists.txt:L432-L456](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/CMakeLists.txt#L432-L456)，`if(ENABLE_FP8)` 会把 `GptFP8`、`BertFP8`、`cublasFP8MMWrapper`、`layernorm_fp8_kernels` 等一大批 FP8 目标链接进 `transformer-shared`。

**Sparsity 构建模板（BERT guide）**：

[docs/bert_guide.md:L196-L227](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/docs/bert_guide.md#L196-L227) — BERT 想用稀疏 GEMM 要：
```bash
cmake -DSM=xx -DCMAKE_BUILD_TYPE=Release -DBUILD_PYT=ON -DSPARSITY_SUPPORT=ON -DCUSPARSELT_PATH=/the_extracted_cusparselt_path ..
```
注意它**没有**带 `BUILD_MULTI_GPU`（单卡也能用稀疏），但必须给 `CUSPARSELT_PATH`。

**最终产物**：

[CMakeLists.txt:L317-L317](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/CMakeLists.txt#L317-L317) — `add_library(transformer-shared SHARED ...)` 把上百个 `$<TARGET_OBJECTS:...>` 聚合成一个共享库，这是 FT 的核心交付物。多 GPU 与 NVTX 通过 [L419-L424](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/CMakeLists.txt#L419-L424)、[L426-L430](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/CMakeLists.txt#L426-L430) 的 `target_link_libraries` 追加链接；[L461](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/CMakeLists.txt#L461-L461) 统一链接 `-lcudart -lcublas -lcublasLt -lcurand`，呼应 u1-l1 里「FT 靠 cuBLAS/cuBLASLt 做矩阵乘」的说法。

#### 4.3.4 代码实践

**目标**：对照 `docs/gpt_guide.md` 的 Build 指引，亲手写出两条 `cmake` 命令，并逐个解释 `-D` 参数。

**操作步骤**：

第 1 步，准备环境（任选其一镜像）：
```bash
nvidia-docker run -ti --shm-size 5g --rm nvcr.io/nvidia/pytorch:22.09-py3 bash
git clone https://github.com/NVIDIA/FasterTransformer.git
cd FasterTransformer
mkdir -p build && cd build
git submodule init && git submodule update   # 拉 cutlass
```

第 2 步，**命令 A：仅构建单 GPU 的 PyTorch 扩展**（适合在单卡上做开发调试）：
```bash
cmake -DSM=80 -DCMAKE_BUILD_TYPE=Release -DBUILD_PYT=ON ..
make -j12
```

第 3 步，**命令 B：构建多 GPU + FP8 的 C++ 工程**（生产级 Hopper 推理）：
```bash
cmake -DSM=90 -DCMAKE_BUILD_TYPE=Release -DBUILD_MULTI_GPU=ON -DENABLE_FP8=ON ..
make -j12
```

**每个 `-D` 参数的含义表**：

| 参数 | 含义 | 在 CMakeLists 中的落点 |
|------|------|----------------------|
| `-DSM=80` / `-DSM=90` | 目标 GPU 架构（A100=80，H100=90） | L129-L155 的 foreach 循环 |
| `-DCMAKE_BUILD_TYPE=Release` | 用 Release 优化（`-O3 --use_fast_math`） | L197-L203 |
| `-DBUILD_PYT=ON` | 编 PyTorch TorchScript 扩展（产物含 `libth_transformer.so`），并对齐 CXX11 ABI | L242-L282 |
| `-DBUILD_MULTI_GPU=ON` | 开多 GPU/多节点，触发 `find_package(MPI)` + `find_package(NCCL)`，并链接 `-lmpi ${NCCL_LIBRARIES}` | L79-L86、L419-L424 |
| `-DENABLE_FP8=ON` | 编入 FP8 模型/kernel（`GptFP8`、`cublasFP8MMWrapper` 等），要求 CUDA ≥ 11.8 且 sm_90 | L24-L30、L432-L456 |

**需要观察的现象**：
- 命令 A 产物：`build/lib/libtransformer-shared.so` + `build/lib/libth_transformer.so`（后者是给 `torch.classes.load_library` 用的）。
- 命令 B 产物：`build/lib/libtransformer-shared.so` 体积明显更大（含 FP8 目标），且依赖 `libmpi.so`、`libnccl.so`。可用 `ldd build/lib/libtransformer-shared.so` 验证。

**预期结果**：
- 命令 A：`make` 成功，`nm lib/libtransformer-shared.so | grep -i fp8` 应**几乎为空**。
- 命令 B：同样 grep 能看到 `GptFP8`、`cublasFP8MMWrapper` 等 FP8 符号。

**待本地验证**：如果手头没有 H100，命令 B 在 `-DSM=90` 下仍可编译出 PTX，但运行时 FP8 kernel 只能在 Hopper 上真正执行；在非 Hopper 卡上跑 FP8 示例会失败。

#### 4.3.5 小练习与答案

**练习 1**：同事抱怨「我加了 `-DBUILD_TF=ON` 但 CMake 直接报错退出」，最可能的原因是什么？
**答案**：没设 `TF_PATH`。[CMakeLists.txt:L111-L113](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/CMakeLists.txt#L111-L113) 明确：`BUILD_TF`/`BUILD_TF2=ON` 时必须给 `TF_PATH`，否则 `FATAL_ERROR`。补上 `-DTF_PATH=/path/to/tensorflow` 即可。

**练习 2**：为什么命令 B（多 GPU + FP8）必须用 `-DSM=90`，而命令 A 可以用 `-DSM=80`？
**答案**：FP8 是 Hopper（sm_90）才有的硬件指令，CUDA 11.8 才支持。`-DSM=80`（Ampere）编不出可运行的 FP8 代码。`BUILD_MULTI_GPU` 本身不挑架构，但和 FP8 组合时受 FP8 的架构门槛约束。

**练习 3**：把 PyTorch 从 1.13 升到 2.0 后，旧的 `libth_transformer.so` 还能直接用吗？为什么？
**答案**：不一定。[CMakeLists.txt:L242-L282](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/CMakeLists.txt#L242-L282) 显示 CMake 会按 `torch._C._GLIBCXX_USE_CXX11_ABI` 对齐 ABI；若两个 PyTorch 版本的 ABI 设置或 Torch C++ API 不同，旧 `.so` 加载会失败，需要重编。

---

## 5. 综合实践

把本讲三个模块串起来，完成一个「**为指定场景定制构建配置**」的小任务。

**场景**：你在公司有一台 8 卡 A100（sm_80）服务器，CUDA 11.8，已装好 PyTorch 1.13、MPI、NCCL，打算用 FT 跑多 GPU 的 GPT 推理，但暂时不需要 FP8（A100 也不支持）。同时你发现首次编译太慢，想把不必要的 CUTLASS MoE 关掉。

**任务**：

1. **判断哪些 option 该开/该关**。依次回答：
   - `BUILD_PYT`：开还是关？为什么。
   - `BUILD_MULTI_GPU`：开还是关？开了会触发哪两个 `find_package`？
   - `ENABLE_FP8`：能不能开？为什么（结合 4.2 的 CUDA/架构门槛）。
   - `BUILD_CUTLASS_MOE`：为了加速编译该设成什么？
   - `-DSM`：设成什么最省编译时间？

2. **写出完整的 `cmake` 命令**（在 `build/` 目录下执行）。

3. **预测产物**：用一句话描述编出的 `libtransformer-shared.so` 会链接哪些外部库、不会包含哪些符号。

**参考答案**：

1. `BUILD_PYT=ON`（要用 PyTorch 外壳）；`BUILD_MULTI_GPU=ON`，会触发 `find_package(MPI)` 和 `find_package(NCCL)`（[L83-L84](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/CMakeLists.txt#L83-L84)）；`ENABLE_FP8` 虽然能声明（CUDA 11.8 ≥ 11.8），但 A100 是 sm_80 不是 Hopper，FP8 跑不起来，应保持 `OFF`；`BUILD_CUTLASS_MOE=OFF` 以缩短编译时间；`-DSM=80` 只编 A100 一种架构最快。

2. 命令：
   ```bash
   cmake -DSM=80 -DCMAKE_BUILD_TYPE=Release \
         -DBUILD_PYT=ON -DBUILD_MULTI_GPU=ON \
         -DBUILD_CUTLASS_MOE=OFF ..
   make -j12
   ```

3. 产物 `libtransformer-shared.so` 会链接 `-lmpi`、`${NCCL_LIBRARIES}`、`-lcudart -lcublas -lcublasLt -lcurand`（[L419-L424](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/CMakeLists.txt#L419-L424)、[L461](https://github.com/NVIDIA/FasterTransformer/blob/df4a7534860137e060e18d2ebf019906120ea204/CMakeLists.txt#L461-L461)），PyTorch 扩展 `libth_transformer.so` 也会被产出；但**不含**任何 FP8 目标（`GptFP8`/`cublasFP8MMWrapper` 等符号缺失），也**不含** MoE GEMM kernel。

> 待本地验证：在真实 A100 机器上执行上述命令，分别用 `ldd` 和 `nm | grep` 验证第 3 步的预测。

## 6. 本讲小结

- FT 的构建入口是顶层 `CMakeLists.txt`，核心是 `option(名字 "描述" 默认)` 开关 + `if(...) add_definitions(...)` 条件编译。框架/并行/精度类开关默认 `OFF`，纯增益类（CUTLASS、NVTX、FAST_MATH）默认 `ON`。
- CUDA 版本驱动自动条件编译：CUDA ≥ 11 自动注入 `ENABLE_BF16`；CUDA ≥ 11.8（或 ≥12）才声明 `ENABLE_FP8`，且 FP8 真正可用还需 Hopper（sm_90）。
- `-DSM=xx` 决定编译目标架构，架构越多编译越慢，建议只设实际用的那张卡；不传时退回默认 `{70,75,80,86}`。
- `BUILD_TF`/`BUILD_TF2` 必须配 `TF_PATH`；`BUILD_MULTI_GPU` 强制依赖 MPI + NCCL；`BUILD_PYT` 会自动对齐 PyTorch 的 CXX11 ABI。
- 所有模型/层/kernel 最终聚合成单一共享库 `transformer-shared`，FP8/多 GPU/NVTX 等通过 `target_link_libraries` 追加。
- 一次典型构建 = 进 NGC 容器 → `git submodule update` → `cmake -D... ..` → `make -j12`，产物落在 `build/lib/` 与 `build/bin/`。

## 7. 下一步学习建议

本讲解决了「怎么编出来」，下一讲 [u1-l3 源码目录结构与代码规范](u1-l3-directory-structure.md) 会带你走进编译产物的源头——`src/fastertransformer/` 下的 `kernels/layers/models/utils` 目录划分，理解 FT「kernel → layer → model」的三层抽象。

如果你想立刻看到代码跑起来，可以跳到 [u1-l4 第一个示例](u1-l4-first-run-examples.md)，用本讲编出来的 `bin/bert_example` 跑第一个推理；想深入某个构建开关背后的源码机制，建议后续结合：

- [u2-l3 cublasMMWrapper 与 GEMM](u2-l3-cublas-gemm.md)：理解 `-lcublasLt` 到底被谁用。
- [u7-l1 张量并行 / NCCL](u7-l1-tensor-parallel.md)：理解 `BUILD_MULTI_GPU` 拉进来的 NCCL 怎么用。
- [u9-l3 FP8 推理](u9-l3-fp8-inference.md)：理解 `ENABLE_FP8` 编进来的那些 FP8 目标是怎么工作的。
