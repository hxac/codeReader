# Megakernels 项目概览与环境搭建

## 最小模块 1：项目背景与目标

### 1. 概念说明

Megakernels 项目旨在探索**超大规模 GPU 内核（megakernels）**的设计与实现，用于加速大语言模型（LLM）的推理。传统 LLM 推理将模型分解为数百个小的 CUDA 内核，每个内核负责单一操作（如矩阵乘法、激活函数等），这种方式存在以下问题：

- **内核启动开销**：频繁的 CPU-GPU 同步和内核启动延迟累积
- **内存带宽浪费**：中间结果在 GPU 全局内存中反复读写
- **寄存器压力**：小内核难以充分利用 GPU 的寄存器文件

Megakernels 通过**将整个 Transformer 层融合为单个巨型内核**来消除这些瓶颈，实现：
- 单次内核启动完成前向传播
- 中间结果保留在片上存储（寄存器/共享内存）
- 最大化计算密度和内存带宽利用率

### 2. 伪代码或流程

传统推理流程 vs Megakernel 流程：

```
# 传统多内核推理（简化）
for layer in layers:
    for component in [rms, qkv_proj, attention, o_proj, mlp]:
        gpu_kernel(component)  # 每个组件一次内核启动
        sync()                  # CPU-GPU 同步

# Megakernel 推理
for layer in layers:
    megakernel(layer)  # 单次内核启动完成整层
    sync()              # 仅层间同步
```

### 3. 原理分析

Megakernels 的核心原理是**算子融合（operator fusion）**与**寄存器/共享内存复用**。通过编译时静态分析，将Transformer层中的所有算子（RMS归一化、QKV投影、RoPE位置编码、注意力计算、MLP等）融合为单个CUDA内核。

数据流分析：对于第 \(i\) 层Transformer，输入隐藏状态 \(h_i \in \mathbb{R}^{B \times S \times D}\)（B=batch size, S=sequence length, D=hidden dim）经历以下变换：

\[
\begin{aligned}
h_i' &= \text{RMSNorm}(h_i) \\
Q, K, V &= h_i' W_Q, h_i' W_K, h_i' W_V \\
Q', K' &= \text{RoPE}(Q, K, \text{pos}) \\
\text{Attn} &= \text{Softmax}((Q'K'^T)/\sqrt{d_k}) V \\
h_{attn} &= \text{Attn} W_O \\
h_{mlp} &= \text{SwiGLU}(\text{RMSNorm}(h_{attn})) \\
h_{i+1} &= h_{attn} + h_{mlp}
\end{aligned}
\]

在传统实现中，每个中间张量（\(h_i', Q, K, V, \text{Attn}\) 等）都需要写入全局内存。Megakernels通过**寄存器阻塞（register tiling）**和**共享内存缓存**，使得这些中间结果仅在片上流转，大幅减少全局内存访问。

### 4. 代码实践

项目入口为 `README.md`，其核心功能是低延迟 LLaMA 推理演示。项目结构体现了模块化设计：

- [demos/low-latency-llama/](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/) 包含 LLaMA 模型的 megakernel 实现
- 每个组件（注意力、MLP等）拆分为独立 `.cu` 文件，便于开发和调试
- [llama.cu](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cu#L15-L24) 定义完整的操作流水线，将各算子组合为完整内核

关键代码片段（`llama.cu:15-24`）定义了操作类型别名：

```cpp
using rms_qkv_rope_append_op =
    rms_qkv_rope_append<default_config, llama_1b_globals>;
using attention_partial_op =
    attention_partial<default_config, llama_1b_globals>;
// ... 其他操作类型
```

这些类型最终通过 PYBIND11_MODULE 暴露为 Python 可调用模块。

### 5. 练习题

1. 传统 LLM 推理中，一个 32 层的 LLaMA 模型需要启动多少次 CUDA 内核？如果使用 megakernel，可以减少到多少次？（假设每层有 8 个算子）

2. 为什么 megakernels 特别适合 LLM 的自回归生成场景，而不是预训练场景？

3. 在上述数据流公式中，哪些中间张量可以完全保留在寄存器中？哪些必须写入共享内存或全局内存？

4. Megakernels 的主要劣势是什么？在什么场景下传统多内核方案更优？

### 6. 答案

**答案 1**：传统方案需要 \(32 \times 8 = 256\) 次内核启动。使用 megakernel 后，每层仅需 1 次，共 32 次，减少为原来的 1/8。

**答案 2**：LLM 生成是**自回归**的，每次仅生成一个 token，batch size 和 sequence length 都很小（通常 batch=1, seq≤128）。这种小规模场景下，内核启动开销占比显著，megakernel 的融合优势明显。预训练则处理大批量数据（batch≥512），计算密度高，内核启动开销相对可忽略。

**答案 3**：
- **可保留在寄存器**：\(h_i'\)（RMS 输出）、\(Q, K, V\) 的部分分块、注意力分数矩阵（对于小 seq_len）
- **需要共享内存**：\(K, V\) 缓存（跨 token 复用）、权重矩阵的缓存块
- **需要全局内存**：模型权重（只读，从 HBM 加载）、最终输出 \(h_{i+1}\)

**答案 4**：Megakernels 的劣势包括：
- **开发复杂度高**：需要手动管理寄存器分配、共享内存同步
- **灵活性差**：难支持动态形状、条件分支
- **编译时间长**：大型内核编译可达数分钟
传统方案更适合**快速原型开发、支持多变架构、需要细粒度优化控制**的场景。

---

## 最小模块 2：依赖关系与子模块

### 1. 概念说明

Megakernels 项目依赖两个核心组件：
- **ThunderKittens**：底层 GPU 抽象库，提供寄存器级张量操作原语
- **PyTorch 与 Transformers**：模型权重加载和高层次接口

ThunderKittens 是一个**嵌入式 DSL**，用 C++ 模板元编程实现，提供：
- 类型安全的寄存器/共享内存张量抽象
- 经过手工调优的矩阵乘法、GEMM 原语
- Warp 级同步和线程块调度工具

Megakernels 在 ThunderKittens 之上构建 LLM 特定的算子融合框架。

### 2. 伪代码或流程

依赖层次结构：

```
Megakernels (LLM 算子融合)
    ↓
ThunderKittens (GPU 原语库)
    ↓
CUDA PTX (GPU 指令集)
    ↓
NVIDIA GPU 硬件
```

项目初始化流程：

```
clone Megakernels
    ↓
git submodule init  # 注册 ThunderKittens 子模块
    ↓
git submodule update # 下载 ThunderKittens 特定分支
    ↓
pip install dependencies # PyTorch, transformers 等
```

### 3. 原理分析

子模块机制（`.gitmodules`）允许 Megakernels **固定 ThunderKittens 的特定提交**，确保兼容性。ThunderKittens 的分支 `bvm-single-ctrl-pre-new-warps` 包含针对 megakernels 优化的控制器实现。

Python 依赖（`pyproject.toml:10-23`）分为三类：
- **模型相关**：`transformers`（权重加载）、`accelerate`（分布式）
- **数值计算**：`einops`（张量操作）、`torch`（后端）
- **工具**：`pydra-config`（配置管理）、`tqdm`（进度条）、`openai`（API 兼容）

ThunderKittens 与 CUDA 的关系：ThunderKittens 代码编译为 **CUDA C++**，进一步通过 NVCC 编译为 PTX（Parallel Thread Execution）指令在 GPU 上执行。

### 4. 代码实践

子模块配置在 [`.gitmodules`](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/.gitmodules#L1-L4) 中定义：

```ini
[submodule "ThunderKittens"]
    path = ThunderKittens
    url = https://github.com/HazyResearch/ThunderKittens.git
    branch = bvm-single-ctrl-pre-new-warps
```

Python 依赖在 [`pyproject.toml`](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/pyproject.toml#L10-L23) 中声明：

```toml
dependencies = [
    "transformers==4.48.3",
    "pydra-config>=0.0.13",
    "accelerate",
    # ... 其他依赖
]
```

安装脚本（[`README.md:8-12`](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/README.md#L8-L12)）：
```bash
git submodule update --init --recursive
pip install uv
uv pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu128
uv pip install -e .
```

### 5. 练习题

1. 如果 ThunderKittens 子模块未正确初始化，编译时会出现什么错误？

2. 为什么要固定 ThunderKittens 的特定分支，而不是总是使用最新版本？

3. `transformers==4.48.3` 中的 `==` 精确版本约束有什么利弊？

4. 如何查看已安装的 ThunderKittens 子模块的当前提交哈希？

### 6. 答案

**答案 1**：编译时会出现 `fatal error: kittens/...: No such file or directory`，因为 ThunderKittens 头文件在 `THUNDERKITTENS_ROOT/include` 目录中未被找到。

**答案 2**：固定分支确保 **API 兼容性**。ThunderKittens 是活跃开发的研究项目，接口可能频繁变化。Megakernels 依赖于特定的控制器实现（`bvm-single-ctrl-pre-new-warps`），使用其他版本可能导致编译失败或运行时错误。

**答案 3**：
- **利**：确保可复现性，避免依赖更新引入的破坏性变更
- **弊**：无法获得依赖的安全补丁和新特性，需要手动测试和升级

**答案 4**：
```bash
cd ThunderKittens
git log -1  # 查看最新提交
git rev-parse HEAD  # 输出完整哈希
```

或在父仓库中：`git submodule status`

---

## 最小模块 3：GPU 架构配置

### 1. 概念说明

Megakernels 支持多种 NVIDIA GPU 架构，每种架构有不同的**计算能力（compute capability）**和硬件特性：
- **H100 (Hopper)**：sm_90a，支持 FP8，Transformer 引擎
- **B200 (Blackwell)**：sm_100a，新一代架构，增强张量核心
- **A100 (Ampere)**：sm_80，成熟的数据中心 GPU
- **4090 (Ada Lovelace)**：sm_89，消费级旗舰

GPU 架构影响代码生成的关键参数：
- **SM 版本**：决定 PTX 指令集（如 tensor core 操作）
- **寄存器文件大小**：影响寄存器分块的可行性
- **共享内存容量**：限制可缓存的中间数据量
- **Warp scheduler 数量**：影响指令级并行

### 2. 伪代码或流程

Makefile 中的 GPU 配置逻辑（伪代码）：

```makefile
GPU = env("GPU") || "B200"

if GPU == "4090":
    FLAGS += "-DKITTENS_4090 -arch=sm_89"
else if GPU == "A100":
    FLAGS += "-DKITTENS_A100 -arch=sm_80"
else if GPU == "H100":
    FLAGS += "-DKITTENS_HOPPER -arch=sm_90a"
else:  # B200 or default
    FLAGS += "-DKITTENS_HOPPER -DKITTENS_BLACKWELL -arch=sm_100a"
```

编译驱动：

```
GPU=H100 make
    ↓
NVCC 接收 -DKITTENS_HOPPER -arch=sm_90a
    ↓
编译器选择 H100 特定代码路径
    ↓
生成适配 H100 的 PTX 和 Cubin
```

### 3. 原理分析

NVCC 的 `-arch` 参数指定**虚拟架构**，如 `sm_90a` 表示：
- `sm_90`：Hopper 架构的基础计算能力
- `a`：变体标识（表示可选架构特性）

预处理器宏（`-DKITTENS_HOPPER`）在 ThunderKittens 代码中触发条件编译，选择架构特定的优化：
- 寄存器分配策略（Hopper 有更多寄存器）
- Tensor core 操作（Hopper 支持 FP8 GEMM）
- Warp 分组策略（Hopper 的 scheduler 改进）

不同架构的**理论性能对比**（以 TFLOPS 计，FP16/BF16）：
- H100: ~67 TFLOPS (FP16 Tensor Core)
- B200: ~100+ TFLOPS (FP16 Tensor Core, 估计)
- A100: ~19.5 TFLOPS (FP16 Tensor Core)
- 4090: ~83 TFLOPS (FP16 Tensor Core)

实际性能受限于**内存带宽**和**内核实现效率**。

### 4. 代码实践

GPU 配置在 [`Makefile:4-28`](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/Makefile#L4-L28) 中实现：

```makefile
ifndef GPU
GPU=B200
endif

# ... NVCCFLAGS 定义 ...

ifeq ($(GPU),4090)
NVCCFLAGS+= -DKITTENS_4090 -arch=sm_89
else ifeq ($(GPU),A100)
NVCCFLAGS+= -DKITTENS_A100 -arch=sm_80
else ifeq ($(GPU),H100)
NVCCFLAGS+= -DKITTENS_HOPPER -arch=sm_90a
else
NVCCFLAGS+= -DKITTENS_HOPPER -DKITTENS_BLACKWELL -arch=sm_100a
endif
```

环境变量设置（[`README.md:24`](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/README.md#L24)）：
```bash
export GPU=H100  # 或 B200, A100, 4090
```

### 5. 练习题

1. 如果在 A100 GPU 上编译时错误地设置了 `GPU=H100`，会发生什么？

2. 为什么 B200 的默认配置同时定义 `KITTENS_HOPPER` 和 `KITTENS_BLACKWELL` 宏？

3. 如何查看当前 GPU 的实际计算能力？

4. 假设你在开发一款新 GPU "X300"，其计算能力为 sm_110，需要在 Makefile 中添加什么配置？

### 6. 答案

**答案 1**：编译会成功，但**运行时会失败**，因为生成的 PTX 代码包含 Hopper 特定指令（如 FP8 tensor core 操作），A100 硬件不支持，导致驱动程序返回 "invalid instruction" 错误。

**答案 2**：Blackwell 是 Hopper 的**继承者**，大部分基础组件共享。`KITTENS_HOPPER` 启用 Hopper 引入的基础优化（如改进的 warp scheduler），`KITTENS_BLACKWELL` 启用 Blackwell 特有的新特性（如更大的共享内存、新的 tensor core 操作）。

**答案 3**：
```bash
nvidia-smi --query-gpu=compute_cap --format=csv
# 或在 CUDA 代码中：
cudaDeviceProp prop;
cudaGetDeviceProperties(&prop, 0);
printf("sm_%d%d\n", prop.major, prop.minor);
```

**答案 4**：
```makefile
else ifeq ($(GPU),X300)
NVCCFLAGS+= -DKITTENS_X300 -arch=sm_110
```
然后在 ThunderKittens 代码中添加 `#ifdef KITTENS_X300` 代码路径，实现 X300 特定优化。

---

## 最小模块 4：编译与运行流程

### 1. 概念说明

Megakernels 的编译流程将 CUDA 源码编译为 **Python 可扩展模块**，通过 pybind11 暴露 C++ 函数给 Python。整个流程分为：
1. **预处理**：NVCC 处理 CUDA 特定语法（`__global__`、`<<<...>>>`）
2. **编译**：将 `.cu` 文件编译为 PTX（虚拟汇编）和 Cubin（二进制）
3. **链接**：与 Python 运行时链接，生成共享库（`.so`/`.pyd`）
4. **加载**：Python import 时动态加载共享库

运行流程：
1. **Python 初始化**：加载模型权重、配置 GPU
2. **Megakernel 调用**：Python 调用编译好的内核函数
3. **GPU 执行**：内核在 GPU 上运行，完成前向传播
4. **结果回传**：将输出张量从 GPU 复制回 CPU（如需）

### 2. 伪代码或流程

完整编译和运行流程：

```
# ========== 编译阶段 ==========
cd demos/low-latency-llama
make
    ↓
NVCC 编译 llama.cu
    ↓
生成 mk_llama.cpython-312-x86_64-linux-gnu.so
    ↓
Python 可通过 import mk_llama 加载

# ========== 运行阶段 ==========
python megakernels/scripts/llama_repl.py
    ↓
加载 LLaMA 权重 (HuggingFace)
    ↓
编译 megakernel（首次运行）
    ↓
进入交互式 REPL，用户输入 prompt
    ↓
调用 mk_llama.mk(...) 生成 tokens
    ↓
打印结果
```

### 3. 原理分析

NVCC 编译管道（简化）：

```
llama.cu (CUDA C++)
    ↓ [CUDA Frontend]
LLVM IR (带 CUDA 内建函数)
    ↓ [PTX Backend]
PTX Assembly (sm_90a 虚拟指令)
    ↓ [Opt]
优化后的 PTX
    ↓ [PTX Assembler]
Cubin (GPU 二进制机器码)
```

NVCCFLAGS 中的关键标志（[`Makefile:16`](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/Makefile#L16)）：
- `-O3`：最高优化级别
- `--use_fast_math`：允许浮点近似（更快的 sin/cos/sqrt）
- `-Xptxas=-v`：显示寄存器使用和 spill 统计
- `-lineinfo`：生成调试行号信息（用于性能分析）
- `--expt-extended-lambda`：允许在设备代码中使用 lambda（ThunderKittens 需要）

pybind11 绑定机制（[`llama.cu:26-50`](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cu#L26-L50)）：
- `PYBIND11_MODULE(mk_llama, m)` 定义模块入口
- `bind_kernel` 模板函数将 C++ 函数绑定为 Python 可调用对象
- 通过指针暴露全局状态（权重、缓存、配置）

### 4. 代码实践

编译命令（[`README.md:20-28`](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/README.md#L20-L28)）：
```bash
export THUNDERKITTENS_ROOT=$(pwd)/ThunderKittens
export MEGAKERNELS_ROOT=$(pwd)
export PYTHON_VERSION=3.12
export GPU=H100
cd demos/low-latency-llama
make
```

编译目标定义（[`Makefile:36-37`](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/Makefile#L36-L37)）：
```makefile
$(TARGET): $(SRC)
	$(NVCC) $(SRC) $(NVCCFLAGS) -o $(TARGET)$(shell python3-config --extension-suffix)
```

`python3-config --extension-suffix` 输出平台特定的共享库后缀（如 `.cpython-312-x86_64-linux-gnu.so`）。

运行 REPL（[`README.md:35`](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/README.md#L35)）：
```bash
python megakernels/scripts/llama_repl.py
```

基准测试（[`README.md:44`](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/README.md#L44)）：
```bash
python megakernels/scripts/generate.py mode=mk prompt="..." ntok=100
```

### 5. 练习题

1. 编译时如果 `THUNDERKITTENS_ROOT` 环境变量未设置，会出现什么错误？

2. `python3-config --extension-suffix` 的输出在不同平台上有什么不同？

3. 为什么 `make clean` 需要 `python3-config --extension-suffix`？

4. 如何使用 `nvprof` 或 `nsys` 分析 megakernel 的性能？

### 6. 答案

**答案 1**：NVCC 会报错 `fatal error: kittens/...: No such file or directory`，因为 `-I${THUNDERKITTENS_ROOT}/include` 包含路径解析为空字符串或错误路径。

**答案 2**：
- Linux：`.cpython-312-x86_64-linux-gnu.so`
- macOS：`.cpython-312-darwin.so`
- Windows：`.cp312-win_amd64.pyd`

后缀编码了 Python 版本（312）、架构（x86_64）、系统和 ABI。

**答案 3**：因为共享库的完整文件名包含平台特定后缀。硬编码 `rm -f mk_llama.so` 只能在 Linux 上工作，而使用 `python3-config --extension-suffix` 使 Makefile 跨平台兼容。

**答案 4**：
```bash
# nsight systems (推荐)
nsys profile --stats=true python megakernels/scripts/generate.py mode=mk prompt="test" ntok=10

# nvprof (已弃用，但仍可用)
nvprof --print-gpu-trace python megakernels/scripts/generate.py mode=mk prompt="test" ntok=10
```

关键指标：GPU 时间占比、内存带宽利用率、warp 执行效率。

---

## 总结

本讲义覆盖了 Megakernels 项目的四个核心维度：
1. **项目背景与目标**：理解 megakernels 在 LLM 推理中的动机和优势
2. **依赖关系与子模块**：掌握 ThunderKittens 和 Python 依赖的作用
3. **GPU 架构配置**：学会为不同 GPU 编译和优化代码
4. **编译与运行流程**：理解从源码到执行的完整管道

掌握这些内容后，你将能够：
- 正确设置开发环境
- 为特定 GPU 架构编译 megakernels
- 运行和调试 LLM 推理
- 理解性能优化的原理和限制

下一步建议：阅读 ThunderKittens 文档，深入学习寄存器级编程技巧。
