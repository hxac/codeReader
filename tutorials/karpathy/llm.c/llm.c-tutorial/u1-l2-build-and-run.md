# 构建系统与三种运行方式

## 1. 本讲目标

学完本讲后，你应该能够：

- 看懂 llm.c 的 `Makefile` 是如何**自动探测**当前机器上的编译环境的（有没有 OpenMP、NCCL、MPI、cuDNN、nvcc、GPU）。
- 说出 `train_gpt2`、`train_gpt2fp32cu`、`train_gpt2cu` 三个构建目标分别对应哪一份源码、分别适合什么场景。
- 理解 `PRECISION`、`USE_CUDNN`、`GPU_COMPUTE_CAPABILITY` 这几个关键 make 变量如何改变最终编译出来的程序。
- 亲手跑通 **CPU 版**的训练前几步，并看懂程序打印出来的 `loss`。

本讲不涉及任何模型算法细节（那是 Unit 2 之后的事），只解决一个问题：**「这份代码怎么编译、怎么跑起来」**。

## 2. 前置知识

在动手之前，先用通俗的话把几个会反复出现的概念过一遍。承接 [u1-l1 项目总览](u1-l1-project-overview.md)，你已经知道仓库里有三套分工明确的实现：`train_gpt2.py`（PyTorch 参考与正确性标尺）、`train_gpt2.c`（纯 C/CPU 参考）、`train_gpt2.cu`（CUDA 主线，最快）。本讲的主角是连接「源码」和「可执行程序」之间的那座桥。

- **Make / Makefile**：`make` 是一个「任务执行器」，`Makefile` 里写着一组「规则（rule）」，形如 `目标: 依赖` 加一行缩进的「命令」。你在命令行输入 `make train_gpt2`，就是请它执行「生成 `train_gpt2` 这个可执行文件」的那条规则。
- **编译器 / CC**：把 C 源码翻译成机器码的程序。llm.c 默认用 `clang`（也兼容 `gcc`）。CUDA 源码（`.cu`）则必须用 NVIDIA 的 `nvcc`。
- **链接库 / `-lxxx`**：`-lgomp`、`-lnccl`、`-lmpi` 这类选项表示「链接时去系统里找 xxx 这个库」。库没装，链接就会失败。所以 Makefile 要先探测库在不在。
- **编译宏 / `-DXXX`**：`-DENABLE_BF16` 相当于在所有源码开头加了一行 `#define ENABLE_BF16`，源码里用 `#ifdef ENABLE_BF16` 来「在编译期」选择走哪一段代码。注意这是**编译期开关**，不是运行时参数。
- **OpenMP**：一套让 C 程序「一行 `#pragma omp parallel for` 就能多线程并行跑 for 循环」的标准。CPU 版训练要靠它才不至于慢到没法看。
- **starter pack**：一组预先准备好的 `.bin` 文件（模型权重、tokenizer、数据集、单元测试用的 debug state），让你不用先装 Python、不用自己训 PyTorch，就能直接体验 llm.c。

> 提醒：本讲里 `PRECISION`、`USE_CUDNN`、`GPU_COMPUTE_CAPABILITY` 都是 **make 命令行变量**（不是源码里的变量），用法是 `make train_gpt2cu PRECISION=FP32`。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [Makefile](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/Makefile) | 整个项目的构建脚本：自动探测环境、定义所有编译目标。本讲最重要的文件。 |
| [dev/download_starter_pack.sh](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/dev/download_starter_pack.sh) | 一键下载权重 / tokenizer / 数据集等 `.bin` 文件的脚本，是「跑起来」的第一步。 |
| [README.md](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/README.md) | 项目说明，其中的 *quick start* 三节给出了三条官方运行路径。 |
| [train_gpt2.c](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c) | CPU 参考实现，本讲只看它的 `main()` 训练循环（综合实践要跑的就是它）。 |

## 4. 核心概念与源码讲解

本讲按三个最小模块拆分：**编译目标与规则**、**环境自动探测**、**精度与算力架构选项**。

### 4.1 Makefile 目标与编译规则

#### 4.1.1 概念说明

一份 `Makefile` 可以看成一张「菜谱表」：左边是你要点的那道菜（**目标 target**），右边是怎么做（**命令**）。llm.c 的 Makefile 一共定义了这几个对外目标：

- `train_gpt2` —— 编译 **CPU fp32 参考实现**，源码是 `train_gpt2.c`。
- `train_gpt2fp32cu` —— 编译 **GPU fp32 legacy 版**，源码是 `train_gpt2_fp32.cu`。这是项目早期「冻结」下来的简单 CUDA 版，适合学 CUDA。
- `train_gpt2cu` —— 编译 **CUDA 主线版**，源码是 `train_gpt2.cu`（加上可选的 cuDNN 文件）。这是跑得最快、支持混合精度与多卡的版本。
- 此外还有 `test_gpt2` / `test_gpt2cu` / `test_gpt2fp32cu`（单元测试）、`profile_gpt2cu`（性能剖析）、`clean`、`all`。

需要特别区分的是：三个训练目标对应**三份不同的源码文件**，而不是同一份代码的不同优化级别。

| 构建目标 | 源码文件 | 编译器 | 典型场景 |
| --- | --- | --- | --- |
| `train_gpt2` | `train_gpt2.c` | `clang`/`gcc`（CPU） | 没有 GPU、想读懂每一层算法 |
| `train_gpt2fp32cu` | `train_gpt2_fp32.cu` | `nvcc` | 有 1 张 GPU、想学最简单的 CUDA |
| `train_gpt2cu` | `train_gpt2.cu` (+`cudnn_att.o`) | `nvcc` | 想要速度、混合精度、多卡 |

#### 4.1.2 核心流程

`make <目标>` 时发生的事，可以用下面这段伪代码概括：

```
1. 读入 Makefile，展开所有变量
2. 先把「自动探测」的 shell 片段全部跑一遍（见 4.2），
   动态决定 CFLAGS / NVCC_FLAGS / TARGETS 里到底装了哪些库
3. 找到名为 <目标> 的规则，执行其命令行
   - CPU 目标：调用 $(CC) $(CFLAGS) train_gpt2.c ... -o train_gpt2
   - CUDA 目标：调用 $(NVCC) $(NVCC_FLAGS) $(PFLAGS) train_gpt2.cu ... -o train_gpt2cu
```

关键在于：Makefile 顶部那一大段不是「规则」，而是用 `$(info ...)`、`$(shell ...)` 写的**探测脚本**。它们在解析 Makefile 时就已经执行完毕，并把结果写进变量里，供后面的规则使用。

#### 4.1.3 源码精读

先看顶部的基本编译选项，CPU 默认编译器是 `clang`，开了 `-Ofast`：

[Makefile:1-6](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/Makefile#L1-L6) —— 设定 `CC`（默认 clang）、CPU 的 `CFLAGS`（`-Ofast` 加几个告警抑制）和链接库 `LDLIBS = -lm`。

CUDA 这边则由 `NVCC_FLAGS` 控制_nvcc_ 的行为：

[Makefile:17-26](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/Makefile#L17-L26) —— `NVCC_FLAGS` 开了多线程编译、`--use_fast_math`、C++17；`USE_CUDNN ?= 0` 表示默认**不**编译 cuDNN（因为会把编译时间从几秒拖到约一分钟）。

接着是三个核心目标的实际规则：

[Makefile:264-265](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/Makefile#L264-L265) —— `train_gpt2` 目标：用 `$(CC)` 编译 `train_gpt2.c`，产出的可执行文件就叫 `train_gpt2`。注意它**没有** `$(PFLAGS)`（CPU 版不需要精度宏）。

[Makefile:276-277](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/Makefile#L276-L277) —— `train_gpt2fp32cu` 目标：用 `nvcc` 编译 `train_gpt2_fp32.cu`。注意它同样**不传** `$(PFLAGS)`，因此无论 `PRECISION` 设什么，legacy 版永远是 fp32。

[Makefile:273-274](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/Makefile#L273-L274) —— `train_gpt2cu` 目标（主线）：依赖 `train_gpt2.cu` 和 `$(NVCC_CUDNN)`，并**带上** `$(PFLAGS)`，所以它会根据 `PRECISION` 选择 FP32/FP16/BF16。

哪些目标会被 `make all` 编译，取决于 `nvcc` 是否存在：

[Makefile:250-258](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/Makefile#L250-L258) —— `TARGETS` 初始只有两个 CPU 目标；若 `which nvcc` 找到 nvcc，就把三个 CUDA 目标追加进去。这就是「没装 CUDA 也能 `make`」的原因。

#### 4.1.4 代码实践

**实践目标**：通过阅读规则，验证「`train_gpt2fp32cu` 不受 `PRECISION` 影响」这一结论。

1. 打开 `Makefile`，对比第 273-274 行（`train_gpt2cu`）和第 276-277 行（`train_gpt2fp32cu`）的命令。
2. 数一数两行命令里出现的 make 变量，确认 `$(PFLAGS)` 只出现在主线目标里。
3. 在第 233-244 行确认 `$(PFLAGS)` 的取值完全由 `PRECISION` 决定（`-DENABLE_FP32` / `-DENABLE_FP16` / `-DENABLE_BF16`）。

**预期结果**：你会发现 legacy 目标的命令里没有 `$(PFLAGS)`，因此即便运行 `make train_gpt2fp32cu PRECISION=BF16`，传进去的 `PRECISION` 对它也没有效果——它总是 fp32。这个细节解释了 README 里为什么把它叫做「fp32 (legacy)」。

> 说明：本实践是「源码阅读型」，不需要实际编译，重点是把三行规则看懂。

#### 4.1.5 小练习与答案

**练习 1**：`make train_gpt2` 默认用哪个编译器？如果想强制用 `gcc` 该怎么写命令？

**答案**：默认用 `clang`（[Makefile:1](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/Makefile#L1) 的 `CC ?= clang`，`?=` 表示「若环境未设置才用默认值」）。强制用 gcc：`make train_gpt2 CC=gcc`。

**练习 2**：执行 `make`（不带任何目标）时，实际会发生什么？

**答案**：`make` 默认执行 Makefile 里**第一个目标**。这里第一个显式目标是 `all`（[Makefile:262](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/Makefile#L262)），它依赖 `$(TARGETS)`，等价于把探测到的所有目标都编译一遍（CPU 目标必有，CUDA 目标看 nvcc 是否存在）。

### 4.2 OpenMP / NCCL / MPI / cuDNN 自动探测

#### 4.2.1 概念说明

不同机器装了不同的库：你的笔记本可能只有 OpenMP，一台服务器可能还装了 NCCL（多卡通信）和 MPI（多节点启动）。如果 Makefile 写死所有库，那么在没装某库的机器上链接就会失败。

llm.c 的做法是**逐个探测**：能找到某个库，就把对应的 `-l` 链接选项和 `-D` 编译宏追加进去；找不到就打印一条提示并跳过。这样同一份 Makefile 可以在「只有 CPU」到「多节点多卡」之间通吃。

四个被探测的对象：

- **OpenMP**：CPU 多线程并行，让 `train_gpt2` 快很多。
- **NCCL**：NVIDIA 的多 GPU 集合通信库，开启后才能多卡训练（`-DMULTI_GPU`）。
- **MPI（OpenMPI）**：多节点启动器，开启后能用 `mpirun`（`-DUSE_MPI`）。
- **cuDNN**：用它的 Flash Attention，开启后注意力走 cuDNN（`-DENABLE_CUDNN`），默认关闭。

#### 4.2.2 核心流程

每段探测都遵循同一个套路：

```
若 用户用 NO_XXX=1 显式禁用:
    打印 "已手动禁用"，跳过
否则:
    用 $(shell ...) 跑一条「测试命令」检查库是否存在
    若 存在:
        追加 -l 链接 + 追加 -D 宏，打印 "✓ ... found"
    若 不存在:
        打印 "✗ ... not found"，给出安装提示
```

这个「试探」机制由两个工具支撑：`file_exists_in_path`（查 PATH 里有没有某命令）和 `check_and_add_flag`（实际用编译器试编一个空程序，看某个 flag 是否被接受）。

#### 4.2.3 源码精读

先看两个通用工具函数：

[Makefile:39-47](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/Makefile#L39-L47) —— `file_exists_in_path`：用 `which`（或 Windows 的 `where`）判断某个可执行文件是否在 PATH 中，后面用来找 `nvcc`、`nvidia-smi`。

[Makefile:73-81](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/Makefile#L73-L81) —— `check_and_add_flag`：让 `$(CC)` 实际编译一句 `int main(){return 0;}`，看某个编译选项（如 `-march=native`）是否被支持；支持才追加。它被用来逐个试探 `CFLAGS_COND` 里的 flag。

**OpenMP 探测**（区分 macOS 与 Linux）：

[Makefile:160-195](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/Makefile#L160-L195) —— 若没设 `NO_OMP=1`，则在 macOS 上找 Homebrew 的 `libomp`（ARM 的 `/opt/homebrew/...` 或 Intel 的 `/usr/local/...`），在 Linux 上用 `$(CC) -fopenmp` 试编来判定；找到就加 `-fopenmp -DOMP` 并链接 `-lgomp`（或 `-lomp`）。`-DOMP` 这个宏让源码里的 `#ifdef OMP` 分支生效。

**NCCL 探测**：

[Makefile:198-214](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/Makefile#L198-L214) —— 用 `dpkg -l | grep -q nccl` 判断 NCCL 是否通过包管理器安装；找到就加 `-DMULTI_GPU` 和 `-lnccl`，并提示「可以多卡训练」。

**MPI 探测**：

[Makefile:217-230](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/Makefile#L217-L230) —— 在默认路径 `/usr/lib/x86_64-linux-gnu/openmpi` 下同时检查 `lib/` 与 `include/` 是否存在；都在就加 `-DUSE_MPI -lmpi` 及对应路径。

**cuDNN 探测**：

[Makefile:113-127](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/Makefile#L113-L127) —— 仅当 `USE_CUDNN=1` 时，去 `$(HOME)/cudnn-frontend/include` 或 `./cudnn-frontend/include` 找 cuDNN frontend 头文件；找到则加 `-DENABLE_CUDNN -lcudnn`，并把 `cudnn_att.o` 设为 `NVCC_CUDNN`（让 [Makefile:270-271](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/Makefile#L270-L271) 这条规则单独编译 `llmc/cudnn_att.cpp`）。找不到就 `$(error ...)` 直接报错终止。

#### 4.2.4 代码实践

**实践目标**：在脑中「预演」探测逻辑，预测不同机器上的探测结果。

1. 在 [Makefile:198-214](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/Makefile#L198-L214) 的 NCCL 探测段里，确认判定条件是 `dpkg -l | grep -q nccl`。
2. 假设你在一台**只装了 CPU、连 nvcc 都没有**的 Linux 机器上运行 `make train_gpt2`：
   - OpenMP 段：若 `clang -fopenmp` 试编成功 → 打印 `✓ OpenMP found` 并加 `-fopenmp`；否则打印 `✗ OpenMP not found`。
   - NCCL 段：会打印什么？
3. 思考：为什么这些探测结果会直接决定 `train_gpt2` 的运行速度？

**预期结果**：没有 NCCL 时会打印 `✗ NCCL is not found, disabling multi-GPU support`，但这**不影响** CPU 版编译——CPU 目标的命令里本来就没有 `-lnccl`。OpenMP 才是影响 CPU 版速度的关键：没装 OpenMP，`train_gpt2` 仍能编译运行，但 for 循环不会并行，会慢得多。

> 待本地验证：你机器上实际打印的 `✓/✗` 提示行，请在本地 `make` 后与上面的预测对照。

#### 4.2.5 小练习与答案

**练习 1**：你想在一台装了 NCCL 的机器上**临时**禁用多卡支持，只为了排除问题。该怎么做？

**答案**：运行 `make train_gpt2cu NO_MULTI_GPU=1`。这样会命中 [Makefile:198-199](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/Makefile#L198-L199) 的分支，打印「Multi-GPU (NCCL) is manually disabled」并跳过 NCCL。类似的还有 `NO_OMP=1`、`NO_USE_MPI=1`。

**练习 2**：为什么 cuDNN 默认是关闭的（`USE_CUDNN ?= 0`）？

**答案**：见 [Makefile:25-26](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/Makefile#L25-L26) 的注释：cuDNN 会把编译时间从几秒拖到大约一分钟，而且该代码路径相对较新，因此默认关闭，需要时用 `USE_CUDNN=1` 显式开启。

### 4.3 精度与算力架构选项

#### 4.3.1 概念说明

CUDA 主线版 `train_gpt2cu` 有两个「会显著改变程序行为」的编译期旋钮：

- **`PRECISION`**：决定训练用哪种浮点精度。可选 `FP32`（最准、最慢）、`FP16`（范围小、易溢出）、`BF16`（默认，范围大、精度略低，适合 LLM）。它最终变成 `-DENABLE_FP32/-DENABLE_FP16/-DENABLE_BF16`，让源码在编译期挑出对应的那套类型别名（即上一讲提到的 `floatX`）。
- **`GPU_COMPUTE_CAPABILITY`**：告诉 nvcc 「我的 GPU 是哪一代」，好生成对应的机器码（`sm_XX`）。设错了轻则性能差，重则跑不起来。

还有一个「总开关」`USE_CUDNN`，已在 4.2 讲过。这三者共同决定了你编译出来的二进制是「为哪台机器、哪种精度、走不走 Flash Attention」量身定做的。

> 概念提醒：与 Python 里改一个变量不同，这里的 `PRECISION` 一旦编译就固定了，想换精度必须重新 `make`。源码里通过 `#if defined(ENABLE_BF16)` 之类的条件编译，让同一份代码能在三种精度间切换。

#### 4.3.2 核心流程

精度变量的流转：

```
make train_gpt2cu PRECISION=FP32
        │
        ▼
PRECISION ?= BF16   (默认值，命令行覆盖为 FP32)
        │
        ▼
校验 PRECISION ∈ {FP32, FP16, BF16}，否则 $(error)
        │
        ▼
PFLAGS = -DENABLE_FP32   (第 238-239 行)
        │
        ▼
PFLAGS 被塞进 train_gpt2cu 的命令  →  nvcc 看到宏，编译 fp32 版
```

算力的流转类似：`nvidia-smi` 查出能力值 → 拼成 `--generate-code arch=compute_XX,code=[compute_XX,sm_XX]` → 加进 `NVCC_FLAGS`。

#### 4.3.3 源码精读

**`PRECISION` 的处理**：

[Makefile:233-244](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/Makefile#L233-L244) —— `PRECISION ?= BF16` 设默认值；用 `$(filter ...)` 校验取值是否合法（非法会 `$(error ...)` 终止）；再按值把 `PFLAGS` 设成三个 `-DENABLE_*` 之一。

**`GPU_COMPUTE_CAPABILITY` 的自动探测**：

[Makefile:49-63](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/Makefile#L49-L63) —— 若不在 CI 环境、且用户没手动指定，就用 `nvidia-smi --query-gpu=compute_cap` 查询所有 GPU 的算力，去掉小数点、升序排序、取**最小**值（保证生成的代码能在所有卡上跑），再拼出 `--generate-code ...`。注意 `ifeq ($(CI),true)` 这一层：CI 环境里不一定有 GPU，所以跳过查询。

**`USE_CUDNN` 的开关位置**：默认值在 [Makefile:25-26](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/Makefile#L25-L26)（`USE_CUDNN ?= 0`），开启后的探测与编译在 [Makefile:113-127](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/Makefile#L113-L127)（已在 4.2.3 介绍）。

#### 4.3.4 代码实践

**实践目标**：跟踪 `PRECISION=FP32` 从命令行一路流到 nvcc 命令的完整路径。

1. 假设命令是 `make train_gpt2cu PRECISION=FP32 USE_CUDNN=0`。
2. 在 [Makefile:233-244](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/Makefile#L233-L244) 确认 `PFLAGS` 此时 = `-DENABLE_FP32`。
3. 在 [Makefile:273-274](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/Makefile#L273-L274) 确认这条命令把 `$(PFLAGS)` 带了进去。
4. 思考：为什么 `PRECISION` 这种「全局类型选择」适合用编译宏，而不是运行时命令行参数？

**预期结果**：因为 BF16/FP16/FP32 对应不同的 C 类型（如 `__nv_bfloat16` vs `half` vs `float`），类型一旦定了，整个程序的指针、缓冲、kernel 模板都跟着定。C/CUDA 是静态类型语言，无法在运行时切换「整份代码的类型」，所以只能用编译宏在编译期分流。这正是 `floatX` 这个别名存在的意义（详见 u1-l1 术语表）。

#### 4.3.5 小练习与答案

**练习 1**：执行 `make train_gpt2cu PRECISION=TF32` 会发生什么？

**答案**：会报错终止。因为 `TF32` 不在 `VALID_PRECISIONS = FP32 FP16 BF16` 里（[Makefile:234-237](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/Makefile#L234-L237)），`$(filter)` 返回空，触发 `$(error Invalid precision TF32 ...)`。TF32 在 llm.c 里是作为 matmul 内部的一个细节开关存在的（`override_enable_tf32`），不是 `PRECISION` 的合法值。

**练习 2**：为什么 `GPU_COMPUTE_CAPABILITY` 的探测要「取所有 GPU 的最小算力」？

**答案**：见 [Makefile:53-55](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/Makefile#L53-L55)：nvcc 用 `sm_XX` 生成针对某一代的机器码，算力低的老卡跑不了为高算力卡生成的指令。取最小值能保证生成的代码在**所有**卡上都能跑（虽然会牺牲新卡的一点优化空间）。

## 5. 综合实践：跑通 CPU 版训练并记录 loss

这是本讲的主任务，对应规格里的实践要求。整体分三步：下载 starter pack → 编译 CPU 版 → 运行并记录 loss。

> 前置条件：一台装了 `clang`/`gcc` 和 `curl` 的 Linux 或 macOS 机器。本实践**不需要 GPU**，也不需要 Python。

### 步骤 1：下载 starter pack

README 的 CPU quick start（[README.md:30-39](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/README.md#L30-L39)）给出了完整命令。先给脚本加执行权限再运行：

```bash
chmod u+x ./dev/download_starter_pack.sh
./dev/download_starter_pack.sh
```

这个脚本（[dev/download_starter_pack.sh:1-80](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/dev/download_starter_pack.sh)）做的事：

- 从 HuggingFace 的 `karpathy/llmc-starter-pack` 数据集下载一组 `.bin` 文件（[dev/download_starter_pack.sh:7](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/dev/download_starter_pack.sh#L7)、[dev/download_starter_pack.sh:19-27](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/dev/download_starter_pack.sh#L19-L27)）。
- 按文件名前缀分发到不同目录：`tiny_shakespeare*` 进 `dev/data/tinyshakespeare/`、`hellaswag*` 进 `dev/data/hellaswag/`、模型/tokenizer 等 `.bin` 进仓库**根目录**（[dev/download_starter_pack.sh:36-42](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/dev/download_starter_pack.sh#L36-L42)）。
- 用 `curl` 并发下载（每批 6 个）。

下载清单含义：

| 文件 | 作用 |
| --- | --- |
| `gpt2_124M.bin` | GPT-2 124M 权重（fp32），CPU 版默认加载它 |
| `gpt2_124M_bf16.bin` | 同权重存成 bf16，给 CUDA bf16 路径用 |
| `gpt2_124M_debug_state.bin` | 单元测试用的「标准答案」（一组数据 + 目标激活/梯度） |
| `gpt2_tokenizer.bin` | GPT-2 BPE 分词器 |
| `tiny_shakespeare_train.bin` / `_val.bin` | 训练 / 验证用的 token 流（带 1024 字节头） |
| `hellaswag_val.bin` | HellaSwag 评测数据 |

**需观察**：下载完成后，仓库根目录应出现 `gpt2_124M.bin`、`gpt2_tokenizer.bin` 等文件；`dev/data/tinyshakespeare/` 下应有两个 `tiny_shakespeare_*.bin`。

### 步骤 2：编译 CPU 版

```bash
make train_gpt2
```

这会命中 [Makefile:264-265](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/Makefile#L264-L265) 的规则，用 `clang` 编译 `train_gpt2.c`。Makefile 解析时会打印一串 `✓/✗` 探测行（OpenMP 等），留意你的机器是否显示 `✓ OpenMP found`——它直接关系到下一步的速度。

**需观察**：编译成功后当前目录生成可执行文件 `train_gpt2`。

### 步骤 3：用 OpenMP 多线程运行，记录前几步 loss

```bash
OMP_NUM_THREADS=8 ./train_gpt2
```

`OMP_NUM_THREADS=8` 是给 OpenMP 的运行时环境变量，告诉它用 8 个线程（线程数按你的 CPU 核数调整；README 的提示见 [README.md:30-39](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/README.md#L30-L39)）。

程序的 `main()` 在 [train_gpt2.c:1077](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L1077) 起，关键默认值：

- [train_gpt2.c:1090-1091](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L1090-L1091)：`B = 4`（batch size）、`T = 64`（上下文长度）。
- [train_gpt2.c:1110](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L1110)：主循环 `for (step = 0; step <= 40; step++)`，即跑 41 步。
- 每 10 步算一次验证 loss（[train_gpt2.c:1113-1123](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L1113-L1123)），每 20 步生成一段文本（[train_gpt2.c:1126-1160](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L1126-L1160)）。
- 每一步的训练核心在 [train_gpt2.c:1162-1171](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L1162-L1171)：`dataloader_next_batch → gpt2_forward → gpt2_zero_grad → gpt2_backward → gpt2_update`，最后打印 `step %d: train loss %f (took %f ms)`。

**需观察 / 预期结果**：终端会先打印模型配置（`vocab_size: 50257`、`num_layers: 12` 等），接着：

```
val loss 5.25xxxx
step 0: train loss 5.3xxxxx (took xxxx.x ms)
step 1: train loss 4.xxxxxx (took xxxx.x ms)
step 2: train loss 4.xxxxxx (took xxxx.x ms)
...
```

请把你本地实际跑出来的 **step 0 / step 1 / step 2 的 train loss**，以及「每步耗时 ms」记录下来。按 README 给出的参考输出（[README.md:45-77](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/README.md#L45-L77)），loss 应从约 5.3 开始逐步下降到 4 上下。如果 loss 不降反升或为 `nan`，多半是 starter pack 没下全或文件放错了目录。

> 待本地验证：以上 loss 数值会因机器和线程数而异，请以你本地实际打印为准；README 的数值仅供参考量级。若没有合适的 CPU，可在有 CUDA 的机器上用 `make train_gpt2fp32cu && ./train_gpt2fp32cu`（[README.md:11-20](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/README.md#L11-L20)）走 GPU fp32 legacy 路径替代。

**进阶观察（可选）**：把 `OMP_NUM_THREADS` 改成 `1` 再跑一次，对比同一 step 的 `took ... ms`，直观感受 OpenMP 带来的加速——这正是 4.2 节 OpenMP 探测的意义。

## 6. 本讲小结

- llm.c 的三个训练目标对应**三份不同源码**：`train_gpt2`（`train_gpt2.c`，CPU）、`train_gpt2fp32cu`（`train_gpt2_fp32.cu`，GPU fp32 legacy）、`train_gpt2cu`（`train_gpt2.cu`，CUDA 主线）。
- Makefile 顶部用 `$(shell ...)` / `$(info ...)` 做**环境自动探测**：OpenMP、NCCL、MPI、cuDNN、nvcc、GPU 算力，找到才追加对应的 `-l` 库与 `-D` 宏，因此同一份 Makefile 适配从「纯 CPU」到「多节点多卡」的各种机器。
- `PRECISION`（默认 BF16，合法值 FP32/FP16/BF16）→ `PFLAGS`（`-DENABLE_*`）是编译期精度开关；只有主线 `train_gpt2cu` 接受它，legacy 版恒为 fp32。
- `GPU_COMPUTE_CAPABILITY` 由 `nvidia-smi` 自动查询并取所有卡的最小值；`USE_CUDNN`（默认 0）控制是否编译 Flash Attention。
- CPU 版的标准运行流程是：`./dev/download_starter_pack.sh` → `make train_gpt2` → `OMP_NUM_THREADS=N ./train_gpt2`，观察 loss 从约 5.3 逐步下降。
- 调试用可以把编译命令里的 `-O3` 换成 `-g`（见 [README.md:9](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/README.md#L9)），方便在 IDE 里单步走。

## 7. 下一步学习建议

本讲之后，你已经能让程序跑起来并看懂 loss 输出。建议接着：

1. **[u1-l3 CPU 参考实现全景与训练主循环](u1-l3-cpu-reference-overview.md)**：本讲我们把 `main()` 当黑盒用了，下一讲会打开 `train_gpt2.c`，逐段讲清 `GPT2Config`、参数/激活张量布局、以及训练主循环里 `forward → zero_grad → backward → update` 四步各做了什么。
2. 如果你想先验证「编译出来的程序在数值上是对的」，可以看 **[u3-l4 数值正确性测试](u3-l4-correctness-test.md)**，跑 `make test_gpt2 && ./test_gpt2`，它会把 C 实现和 PyTorch 参考逐元素比对。
3. 想自己生成 starter pack 里那些 `.bin` 文件（而不是下载），可以读 [README.md:24-28](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/README.md#L24-L28) 提到的 `python dev/data/tinyshakespeare.py` 与 `python train_gpt2.py`，这会顺带连接到 Unit 4 的二进制协议主题。
