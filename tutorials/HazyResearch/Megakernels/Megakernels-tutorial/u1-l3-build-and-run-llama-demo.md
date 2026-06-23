# 编译并运行 low-latency-llama demo

> 本讲对应手册单元 U1·L3，承接 [U1·L2]（仓库结构与双视角架构）。本讲把上一讲"Python 编排层 / CUDA 内核层"的两张抽象图，落到一条**可执行的命令链**上：从 `make` 产生 `.so`，到 Python 端 `import mk_llama`，再到 `generate.py` / `llama_repl.py` 跑出生成结果。

## 1. 本讲目标

学完本讲，你应当能够：

1. 在本地正确配置 `THUNDERKITTENS_ROOT` / `MEGAKERNELS_ROOT` / `PYTHON_VERSION` / `GPU` 四个环境变量，并执行 `make`。
2. 解释 `Makefile` 中 `NVCCFLAGS` 的关键开关（`-std=c++20`、`--expt-extended-lambda`、`-lineinfo`、`-Xptxas=--warn-on-spills` 等）各自的作用。
3. 说出 **H100（`sm_90a`）与 B200（`sm_100a`）** 两条编译路径的具体差异，并指出 GPU 宏不止改变 `-arch`，还会改变编译进内核的 SM 数量。
4. 看懂 [llama.cu](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cu) 里的 `PYBIND11_MODULE` 是如何把 CUDA 内核 `mk` 暴露成 Python 可调用的 `mk_llama`，以及"模块名"和"函数名"为什么**都叫 `mk_llama`**。
5. 描述 `make` 产物（一个 `.so` 文件）如何被 [mk.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/mk.py) 动态加载，从而串起 `generate.py mode=mk` 的完整调用链。

## 2. 前置知识

- **Makefile**：`make` 工具读的构建脚本。用"变量 + 规则 + 目标"描述如何把源文件变成产物。本讲你会看到 `?=（条件赋值）`、`ifndef`、`+=（追加）`、`ifeq` 等常见写法。
- **nvcc**：NVIDIA 的 CUDA 编译器，相当于 CUDA 版的 `g++`。它把 `.cu` 源文件编译成 GPU 可运行的代码，并和主机端 C++ 代码一起链接。
- **`-arch=sm_XXa`（compute capability）**：指定目标 GPU 的架构版本。`sm_90a` 对应 Hopper（H100），`sm_100a` 对应 Blackwell（B200）；结尾的 `a` 表示"启用该架构专属特性"（如 TMA、wgmma），比不带 `a` 的通用版限制更严、能开更多硬件加速。
- **pybind11**：把 C++ 函数/类编译成 Python 模块的胶水库。`PYBIND11_MODULE(模块名, 句柄)` 宏定义一个 Python 模块的入口，再用 `bind_kernel(...)` 这样的辅助函数把 CUDA kernel 注册进去。
- **Python C 扩展（`.so`）**：Python 用 `import` 加载的动态链接库，文件名形如 `mk_llama.cpython-312-x86_64-linux-gnu.so`。只有当文件名前缀（`mk_llama`）与 `import` 的名字一致、且它能被 `sys.path` 找到时，`import` 才会成功。
- **pydra**：本仓库脚本（`generate.py` / `llama_repl.py`）用的配置/命令行框架。命令行上的 `mode=mk`、`ntok=100` 这类写法，会被 pydra 解析成配置对象的字段。

如果你对 pybind11 和"指令/内核"的整体架构还不熟，建议先读 [U1·L2] 的第 4 节，再回到本讲。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| [README.md](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/README.md) | 安装与运行说明 | 环境变量与运行命令的"权威出处" |
| [demos/low-latency-llama/Makefile](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/Makefile) | 把 `llama.cu` 编译成 `mk_llama*.so` | `NVCCFLAGS`、GPU 宏、构建规则 |
| [demos/low-latency-llama/llama.cu](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cu) | pybind11 入口，暴露 `mk_llama` | `PYBIND11_MODULE` + `bind_kernel` |
| [demos/low-latency-llama/llama.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cuh) | 全局张量结构 `llama_1b_globals` | GPU 宏如何决定编译期 SM 数量 |
| [megakernels/mk.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/mk.py) | 动态加载编译后的 `.so` | `sys.path.append` + `from mk_llama import mk_llama` |
| [megakernels/dispatch.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/dispatch.py) | 按 `mode` 选择解释器 | `make_mk_interpreter(mode, mk_dir)` |
| [megakernels/scripts/generate.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/generate.py) | 基准生成脚本 | `mode=mk` 分支与计时循环 |
| [megakernels/scripts/llama_repl.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/llama_repl.py) | 交互式聊天 | 默认 `mode=mk` 与 REPL 循环 |

## 4. 核心概念与源码讲解

### 4.1 Makefile 与 GPU 宏：把 `.cu` 编译成 `.so`

#### 4.1.1 概念说明

Megakernels 的"内核层"最终要变成一个 Python 能 `import` 的动态库，这一步由 [demos/low-latency-llama/Makefile](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/Makefile) 完成。它做的事情可以概括成三句：

1. **选编译器**：用 `nvcc`。
2. **拼一长串编译/链接选项**：`NVCCFLAGS`，它包含 C++ 标准、优化级别、头文件搜索路径、pybind11 头文件、Python 链接选项，以及 `-shared -fPIC`（生成动态库）。
3. **按目标 GPU 切换架构宏**：`GPU` 变量决定 `-arch=sm_XXa` 和 `-DKITTENS_*` 预处理宏。

理解了这三点，整个构建流程就清晰了。

#### 4.1.2 核心流程

`make`（默认目标 `all`）的执行过程：

```
读取变量：NVCC、GPU（默认 B200）、PYTHON_VERSION（默认 3.13）、TARGET=mk_llama、SRC=llama.cu
        │
        ▼
拼接 NVCCFLAGS = 基础编译开关  （Makefile:16）
              + 头文件路径 + pybind11 头 + Python 链接 + -shared -fPIC  （Makefile:17）
              + GPU 相关 -D 宏 与 -arch=sm_XXa            （Makefile:20-28）
        │
        ▼
匹配规则 $(TARGET): $(SRC)  →  执行：
   nvcc llama.cu  <NVCCFLAGS>  -o  mk_llama$(python3-config --extension-suffix)
        │
        ▼
产物：demos/low-latency-llama/mk_llama.cpython-<版本>-<平台>.so
```

关键点：产物文件名由 `python3-config --extension-suffix` 决定（形如 `.cpython-312-x86_64-linux-gnu.so`），这一节后缀**必须与你实际用来跑脚本的 Python 解释器匹配**，否则 Python 端会 `import` 失败。

#### 4.1.3 源码精读

**变量与默认值**。先看顶部的几个条件赋值：

[Makefile:2-6](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/Makefile#L2-L6) —— `NVCC?=nvcc` 表示"若环境里没设 `NVCC`，就用 `nvcc`"；`ifndef GPU ... GPU=B200 ... endif` 表示**不指定 GPU 时默认按 B200 编译**。

[Makefile:8-14](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/Makefile#L8-L14) —— `TARGET=mk_llama` 是产物名前缀；`SRC=llama.cu` 是唯一源文件；`PYTHON_VERSION` 默认是 `3.13`，但 README 的示例用的是 `3.12`。这是个**关键不一致点**：你必须把 `PYTHON_VERSION` 设成你本机 Python 的主.次版本（如 `3.12`），否则下面 `-lpython${PYTHON_VERSION}` 会找不到对应的 Python 运行时库。

**基础编译开关**。

[Makefile:16](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/Makefile#L16) —— 这一行是内核优化与诊断的核心，重点开关含义如下：

| 开关 | 作用 |
| --- | --- |
| `-DNDEBUG` | 关闭断言，发布编译 |
| `--expt-extended-lambda` / `--expt-relaxed-constexpr` | 允许在 device lambda 里捕获更多、放宽 constexpr 限制（ThunderKittens 大量用到） |
| `-Xcompiler=-fPIE` / `-Xcompiler=-fno-strict-aliasing` | 把这些选项透传给主机端编译器（`g++`） |
| `--use_fast_math` / `-O3` | 开启快速数学与最高优化 |
| `-Xptxas=--warn-on-spills` | 寄存器溢出（spill）到显存时**打印警告**——对调优 megakernel 很重要 |
| `-std=c++20` | 用 C++20 标准 |
| `-lineinfo` | 嵌入行号信息，方便用 `nsys`/`ncu` 做性能剖析 |

[Makefile:17](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/Makefile#L17) —— 追加**头文件路径与 Python 胶水**：`-I${THUNDERKITTENS_ROOT}/include` 引入子模块 ThunderKittens 的头；`-I${MEGAKERNELS_ROOT}/include` 引入本项目通用虚拟机头；`$(shell python3 -m pybind11 --includes)` 在命令行展开为 pybind11 与 Python 的头文件路径；`-shared -fPIC -lpython${PYTHON_VERSION}` 决定产物是一个动态库并链接对应版本的 Python。这也是为什么 `THUNDERKITTENS_ROOT` 和 `MEGAKERNELS_ROOT` **必须事先 `export`**——少了它们，`#include "kittens.cuh"` 会找不到。

**GPU 架构分支**。

[Makefile:20-28](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/Makefile#L20-L28) —— 这一段是本讲的"GPU 宏"核心。`ifeq ($(GPU),H100)` 走 `-DKITTENS_HOPPER -arch=sm_90a`；`else`（即 B200 或任何未识别值）走 `-DKITTENS_HOPPER -DKITTENS_BLACKWELL -arch=sm_100a`。注意：H100 和 B200 都定义了 `-DKITTENS_HOPPER`，区别在于 **B200 额外定义 `-DKITTENS_BLACKWELL`，并把 `-arch` 从 `sm_90a` 换成 `sm_100a`**。

**这个 `-DKITTENS_BLACKWELL` 不只是个"开关"**，它还会改变内核里**编译期写死的 SM 数量**。看 [llama.cuh:25-26](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cuh#L25-L26)：

```c
#define H100_SM_COUNT 132
#define B200_SM_COUNT 148
```

再看类型别名 `llama_1b_globals` 的最后那个模板参数：

[llama.cuh:149-158](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cuh#L149-L158) —— 用 `#ifndef KITTENS_BLACKWELL ... H100_SM_COUNT ... #else ... B200_SM_COUNT ... #endif` 来选择 `sm_count`。也就是说：**用 `GPU=H100` 编译出的内核"以为"自己有 132 个 SM，用 `GPU=B200`（默认）编译出的"以为"有 148 个**。SM 数量决定了 launch 的 grid 大小（见 [llama.cuh:144](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cuh#L144) `dim3 grid() { return dim3(sm_count); }`），所以**编出来的 `.so` 是和具体 GPU 型号绑定的，不能跨型号通用**。

**构建与清理规则**。

[Makefile:36-37](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/Makefile#L36-L37) —— 实际的编译命令：`$(NVCC) $(SRC) $(NVCCFLAGS) -o $(TARGET)$(shell python3-config --extension-suffix)`，即 `nvcc llama.cu <所有 NVCCFLAGS> -o mk_llama<后缀>`。

[Makefile:40-41](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/Makefile#L40-L41) —— `make clean` 删掉带扩展后缀的 `.so`。

#### 4.1.4 代码实践：手写 H100 与 B200 两条编译命令的差异

1. **实践目标**：不依赖记忆，仅凭读 [Makefile](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/Makefile) 推断出 `make GPU=H100` 与 `make GPU=B200` 实际传给 `nvcc` 的命令，并指出两者的差异点。
2. **操作步骤**：
   - 打开 [Makefile:16-28](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/Makefile#L16-L28)，把第 16、17 行的所有 `NVCCFLAGS`，加上第 24-25 行（H100）或 26-27 行（B200）的 GPU 段，拼到第 37 行的命令模板里。
   - 设 `THUNDERKITTENS_ROOT=/path/TK`、`MEGAKERNELS_ROOT=/path/MK`、`PYTHON_VERSION=3.12`，写出两条完整命令（参考答案见 4.1.5）。
3. **需要观察的现象**：两条命令的**绝大多数选项完全相同**，差异只集中在一两个地方。
4. **预期结果**：差异只有两处——
   - `-arch=sm_90a`（H100）↔ `-arch=sm_100a`（B200）；
   - H100 段是 `-DKITTENS_HOPPER`，B200 段多了一个 `-DKITTENS_BLACKWELL`。
   - 其余 `-DNDEBUG`、`-std=c++20`、`-lineinfo`、头文件路径、`-shared -fPIC` 等全部一致。
5. 编译本身**不要求物理 GPU 在场**（`nvcc` 是离线编译器），但要求 CUDA Toolkit 已安装。**待本地验证**：在你本机执行 `make GPU=H100` 后，能否在 `demos/low-latency-llama/` 下看到 `mk_llama*.so` 生成。

#### 4.1.5 小练习与答案

**练习 1**：为什么 README 里要 `export PYTHON_VERSION=3.12`，而 Makefile 默认却是 `3.13`？如果两者对不上会发生什么？

> **答**：Makefile 用 `-lpython${PYTHON_VERSION}` 链接 Python 运行时库，而 `python3 -m pybind11 --includes` / `python3-config --extension-suffix` 取的是**你当前 Python 解释器**的版本。若 `PYTHON_VERSION` 与实际解释器不一致，链接器找不到 `libpython3.x`，或生成的 `.so` 后缀与 `import` 名不匹配，导致 `make` 报链接错误或运行时 `ImportError`。README 注释 `# adjust if yours is different` 正是在提醒这一点。

**练习 2**：假如你用 `make GPU=H100` 编译，却把 `.so` 拷到一台 B200 机器上运行，会出什么问题？

> **答**：`sm_90a` 的产物无法在 Blackwell 上以最优路径运行（或直接因架构不匹配加载失败）；更隐蔽的是，内核里 `sm_count` 被编译期写死成 `132`（H100），而 B200 实际有 148 个 SM，导致 launch grid 偏小、SM 利用不足。结论：`.so` 与 GPU 型号绑定，必须**在目标机型上用对应 `GPU=` 重新 `make`**。

### 4.2 llama.cu 的 PYBIND11_MODULE：把内核暴露成 `mk_llama`

#### 4.2.1 概念说明

[llama.cu](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cu) 是 CUDA 侧的"对 Python 入口"。它本身**不包含任何计算逻辑**，只做两件事：

1. 把分散在多个 `.cu` 文件里的算子（RMS、QKV、attention、downproj 等）`#include` 进来；
2. 用 pybind11 的 `PYBIND11_MODULE` 宏，把那个唯一的大内核 `mk` 注册成一个 Python 可调用对象，名字叫 `mk_llama`。

理解本节的关键是一个**命名巧合**：Python 侧 `from mk_llama import mk_llama` 里，前一个 `mk_llama` 是**模块名**，后一个 `mk_llama` 是**模块里的函数名**——两者都来自 `llama.cu`，但来自不同的位置。

#### 4.2.2 核心流程

pybind11 注册与 Python 调用的对应关系：

```
PYBIND11_MODULE(mk_llama, m)        ←  定义"模块名" = mk_llama   （llama.cu:26）
   │
   └─ kittens::py::bind_kernel<mk<...>>(m, "mk_llama", &...globals...)
                                          └─ 定义"函数名" = mk_llama  （llama.cu:28-32）
   │
   ▼  编译后
demos/low-latency-llama/mk_llama.cpython-312-x86_64-linux-gnu.so
   │  （Python 侧：mk.py 把该目录加入 sys.path）
   ▼
from mk_llama import mk_llama        ← 模块名 + 函数名 都是 mk_llama  （mk.py:7）
```

`bind_kernel` 的模板参数 `mk<default_config, llama_1b_globals, ...7 个算子类型...>` 指定了"要绑定哪个内核"；其后的一长串 `&llama_1b_globals::成员` 则是"内核要用到的所有全局张量/标量的地址"，它们按固定顺序与 Python 侧的 `globs` 对象逐字段对齐（这条数据契约是 [U1·L2] 的重点，这里只需知道它存在）。

#### 4.2.3 源码精读

**头文件聚合**。

[llama.cu:1-10](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cu#L1-L10) —— 先 `#include "llama.cuh"`（全局结构体定义），再把 7 个算子的 `.cu` 文件直接 `#include` 进来（注意是 `.cu` 而非 `.cuh`，相当于把多个翻译单元拼到一起编译），最后引入 `pyutils/pyutils.cuh`（pybind11 绑定辅助，来自 ThunderKittens 的头路径）。

**算子类型别名**。

[llama.cu:15-24](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cu#L15-L24) —— 为 7 个算子各起一个简短别名（如 `rms_qkv_rope_append_op`、`attention_partial_op` 等），方便下面在 `bind_kernel` 的模板参数列表里写清楚。

**模块入口与绑定调用**。

[llama.cu:26-27](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cu#L26-L27) —— `PYBIND11_MODULE(mk_llama, m)`：宏的第一个参数 `mk_llama` 决定**编译出的 Python 模块名**；`m` 是 pybind11 给你的模块句柄，后续往 `m` 上注册内容。`m.doc() = "";` 把模块文档字符串设为空。

[llama.cu:28-32](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cu#L28-L32) —— `kittens::py::bind_kernel<mk<...>>(m, "mk_llama", ...)`：

- **模板参数** `mk<default_config, llama_1b_globals, attention_partial_op, attention_reduction_op, rms_qkv_rope_append_op, downproj_op, o_proj_op, rms_upgate_silu_op, rms_lm_head_op>`：要绑定的就是那个唯一的大内核 `mk`，并告诉它用哪些算子类型。
- **第一个实参** `m`：注册到当前模块。
- **第二个实参** `"mk_llama"`：在 Python 侧暴露的**函数名**。所以 Python 里写 `mk_llama.mk_llama(...)`——模块点函数，两个都是 `mk_llama`。

[llama.cu:32-52](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cu#L32-L52) —— 剩下的全是 `&llama_1b_globals::成员` 地址，按"VM 状态 → 权重 → 缓存 → 激活缓冲 → 标量参数"的固定顺序列出（`Bar`、`instructions`、`timings`，然后是 `qkv_weights` 等权重，`k_cache`/`v_cache`，`hidden_states` 等缓冲，最后 `pos_id`/`attn_scale`/`rms_norm_eps`/`skip_attn_reduction` 四个标量）。这个顺序就是 [U1·L2] 讲过的 `globs` 数据契约，Python 侧用同样顺序的 `serialize()` 对接。

#### 4.2.4 代码实践：追踪"两个 `mk_llama`"的来源

1. **实践目标**：确认 Python 端 `from mk_llama import mk_llama` 的两个 `mk_llama`，分别对应 [llama.cu](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cu) 的哪一行。
2. **操作步骤**：
   - 在 [llama.cu:26](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cu#L26) 找到 `PYBIND11_MODULE(mk_llama, m)`，记下：模块名 = 第一个参数。
   - 在 [llama.cu:28-32](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cu#L28-L32) 找到 `bind_kernel<...>(m, "mk_llama", ...)`，记下：函数名 = 第二个参数。
   - 打开 [mk.py:3-7](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/mk.py#L3-L7)，把 `from mk_llama import mk_llama` 的前后两个名字与上面两处对应起来。
3. **需要观察的现象**：两处名字虽然字面相同，但来源不同——一个是 `PYBIND11_MODULE` 的模块名，一个是 `bind_kernel` 的字符串参数。
4. **预期结果**：能画出"`PYBIND11_MODULE(mk_llama) → 模块名`、`bind_kernel(m,"mk_llama") → 函数名`、二者共同支持 `from mk_llama import mk_llama`"的三步对应关系。
5. 如果想直观验证，可在 `mk.py` 的 `import` 前后各加一行 `print`（**示例代码**，非项目原有）：
   ```python
   import sys
   sys.path.append(str(mk_dir.expanduser().absolute()))
   print("looking for module mk_llama in:", [p for p in sys.path if str(mk_dir) in p])
   from mk_llama import mk_llama  # type: ignore
   print("loaded:", mk_llama)
   ```
   仅用于理解加载过程；**待本地验证**实际 `print` 输出（需要先 `make` 出 `.so`）。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `bind_kernel<...>(m, "mk_llama", ...)` 的第二个参数改成 `"run"`，Python 端要怎么改才能再次调用内核？

> **答**：函数名会变成 `run`，所以 [mk.py:7](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/mk.py#L7) 要改成 `from mk_llama import run`，并返回 `run`。模块名（`PYBIND11_MODULE(mk_llama, ...)`）不受影响。这正说明"两个 `mk_llama`"是独立配置的。

**练习 2**：[llama.cu:32-52](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cu#L32-L52) 列出了一长串 `&llama_1b_globals::成员`。为什么必须**严格按顺序**写？

> **答**：因为 `bind_kernel` 按位置把每个成员地址与内核"槽位"一一对应，Python 侧 `globs` 的 `serialize()` 也按同样顺序填充。顺序一旦错乱，内核就会把"权重指针"当成"指令表"来读，结果完全错乱。这正是 [U1·L2] 强调的 `globs` 数据契约。

### 4.3 运行命令与 make 产物：从 `.so` 到生成结果

#### 4.3.1 概念说明

`make` 的最终目的不是"得到一个 `.so`"就结束，而是让 [generate.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/generate.py) / [llama_repl.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/llama_repl.py) 能 `import` 它并调用内核。这一节把整条命令链串起来，并讲清"无 GPU 环境"下每一步会发生什么。

#### 4.3.2 核心流程：`generate.py mode=mk` 的完整命令链

```
（已编译） demos/low-latency-llama/mk_llama.cpython-312-x86_64-linux-gnu.so
        │
   ①  python megakernels/scripts/generate.py mode=mk prompt="..." ntok=100
        │   pydra 解析配置：mode=mk, setting=latency(默认), mk_dir=<repo>/demos/low-latency-llama(默认)
        ▼
   ②  加载 tokenizer + LlamaForCausalLM（从 HuggingFace 拉权重，需要联网/鉴权）
        │   build schedule → assign_to_sms → tensorize_instructions
        ▼
   ③  match mode: case "mk"  →  make_mk_interpreter("latency", mk_dir)
        │   → LatencyMK_Interpreter(mk_dir) → MK_Interpreter.__init__
        │     → get_mk_func(mk_dir): sys.path.append(mk_dir); from mk_llama import mk_llama
        ▼                                     ← 此处动态加载第①步的 .so
   ④  MK_Generator(model, interpreter, schedule, ...)
        │
   ⑤  计时循环：num_warmup 次 warmup + num_iters 次正式，每次 gen.generate(...)
        │   内部最终调用 mk_llama(...)（即内核 mk），结果写回 globs.logits
        ▼
   ⑥  打印：Average time / Output ids / Output text / Fwd per second / Tokens per second
```

要点：第③步是"两半边"接头的瞬间——[mk.py:5-7](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/mk.py#L5-L7) 把 `mk_dir` 加入 `sys.path`，再 `from mk_llama import mk_llama`。所以**`.so` 必须放在 `mk_dir`（默认 `demos/low-latency-llama`）里**，否则这一步会抛 `ModuleNotFoundError`。

#### 4.3.3 源码精读

**`.so` 的加载桥**。

[mk.py:3-7](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/mk.py#L3-L7) —— `get_mk_func` 先 `sys.path.append(str(mk_dir...))`，再 `from mk_llama import mk_llama`，返回内核可调用对象。这一行就是上一讲"跨越两半边的那一行"。

**按 mode 选解释器**。

[dispatch.py:37-38](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/dispatch.py#L37-L38) —— `make_mk_interpreter(mode, mk_dir)` 用 `mk_dir` 构造对应的 MK 解释器（latency 或 throughput）。

**generate.py 的 mk 分支与默认值**。

[generate.py:36](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/generate.py#L36) —— `mk_dir` 默认就是 `<repo>/demos/low-latency-llama`，与 `make` 的产物目录一致。

[generate.py:152-153](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/generate.py#L152-L153) —— `case "mk"` 分支：`interpreter = make_mk_interpreter(config.setting, config.mk_dir)`，随后 `MK_Generator(model, interpreter, schedule, ...)`。

> ⚠️ **一个容易踩的坑**：[generate.py:34](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/generate.py#L34) 里 `mode` 的默认值是字符串 `"model"`，而 `match` 只认 `"torch"` / `"pyvm"` / `"mk"`，其余落到 `case _` 抛 `ValueError`（见 [generate.py:146-165](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/generate.py#L146-L165)）。**所以必须显式写 `mode=mk`（或 `torch`/`pyvm`）**，否则一启动就报 `Invalid mode: model`。

[generate.py:169-185](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/generate.py#L169-L185) —— 计时循环：先 `num_warmup`（默认 5）次预热，再 `num_iters`（默认 10）次正式；用 CUDA event 计 GPU 时间、用 `time.time()` 计 CPU 时间，取正式轮的平均值打印 `Average time`。

[generate.py:204-207](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/generate.py#L204-L207) —— 打印吞吐：`Fwd per second = (ntok-1)/elapsed`，`Tokens per second = batch_size * fwd_per_second`。

**llama_repl.py 的不同点**。

[llama_repl.py:25-26](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/llama_repl.py#L25-L26) —— 与 `generate.py` 不同，REPL 的 `mode` **默认就是 `"mk"`**，`mk_dir` 同样默认指向 `demos/low-latency-llama`，所以直接 `python megakernels/scripts/llama_repl.py` 就会用 megakernel。

[llama_repl.py:61-63](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/llama_repl.py#L61-L63) —— 同样走 `case "mk"` → `make_mk_interpreter` → `MK_Generator`，然后进入交互式 `while True` 循环（[llama_repl.py:136-145](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/llama_repl.py#L136-L145)），每轮打印 `Response` 和 `Speed: ... tokens/s`。

#### 4.3.4 代码实践：在无 GPU 环境下走一遍命令链

1. **实践目标**：即使没有 GPU，也能验证"环境变量 → `make` → `.so` → `import`"这条前半段链路是否畅通，并能解释 `generate.py` 在哪一步会卡住。
2. **操作步骤**（无 GPU，仅验证编译与加载）：
   - 从仓库根目录执行：
     ```bash
     git submodule update --init --recursive
     export THUNDERKITTENS_ROOT=$(pwd)/ThunderKittens
     export MEGAKERNELS_ROOT=$(pwd)
     export PYTHON_VERSION=3.12          # 改成你本机 Python 的主.次版本
     export GPU=H100                     # 或 B200
     cd demos/low-latency-llama
     make
     ```
     这组命令来自 [README.md:16-28](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/README.md#L16-L28)。
   - `make` 完成后，确认产物存在：在 `demos/low-latency-llama/` 下应能看到 `mk_llama*.so`。
   - 回到仓库根，尝试只验证加载（**示例命令**）：
     ```bash
     python -c "import sys; sys.path.append('demos/low-latency-llama'); import mk_llama; print('ok', mk_llama.mk_llama)"
     ```
3. **需要观察的现象**：
   - `make` 过程会打印 `ptxas` / `nvlink` 的 verbose 信息（因为 `-Xptxas=--verbose -Xnvlink=--verbose`）；若有寄存器溢出，会出现 `--warn-on-spills` 的告警；正常结束会生成 `.so`。
   - 上面那条 `python -c` 若打印 `ok <绑定对象>`，说明"`.so` 能被 Python 加载"这一环成立。
4. **预期结果 / 失败点定位**：
   - 若 `make` 报 `kittens.cuh: No such file` → 说明 `THUNDERKITTENS_ROOT` 没设对，或子模块没初始化。
   - 若 `make` 报 `-lpython3.x: not found` → 说明 `PYTHON_VERSION` 与本机 Python 不一致（见 4.1.5 练习 1）。
   - 若 `python -c` 报 `ModuleNotFoundError: No module named 'mk_llama'` → 说明 `.so` 不在 `demos/low-latency-llama/`，或扩展后缀与 Python 版本不匹配。
   - 若继续执行 `python megakernels/scripts/generate.py mode=mk prompt="hi" ntok=5`，会在 [generate.py:86](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/generate.py#L86) `torch.cuda.set_device(...)` 处因**无 GPU**而抛出 PyTorch 的 CUDA 不可用错误——这是无 GPU 环境的预期终点。
5. **待本地验证**：上述每一步的具体报错文本与编译耗时随机器而定；本实践侧重"按现象定位卡在哪一环"，而非追求跑完整个生成。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `make` 之后必须把 `.so` 留在 `demos/low-latency-llama/`，而不能挪到别处再跑 `generate.py`？

> **答**：[mk.py:5-7](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/mk.py#L5-L7) 把 `mk_dir`（默认就是 `demos/low-latency-llama`）加入 `sys.path` 后才 `import mk_llama`。`.so` 一旦移走，`sys.path` 里找不到，就会 `ModuleNotFoundError`。要换位置，得同时改 `generate.py`/`llama_repl.py` 的 `mk_dir`（如 `generate.py --mk_dir <新路径>`）。

**练习 2**：同样是从 HuggingFace 拉 Llama 权重，`generate.py` 和 `llama_repl.py` 在 `mode` 上的默认行为有何不同？

> **答**：[generate.py:34](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/generate.py#L34) 默认 `mode="model"`（无效，必须显式指定），而 [llama_repl.py:26](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/llama_repl.py#L26) 默认 `mode="mk"`，开箱即用 megakernel。

**练习 3**：在无 GPU 机器上，命令链最远能走到哪一步？为什么？

> **答**：能走到第③步"加载 `.so`"（`from mk_llama import mk_llama`），因为 `import` 一个动态库不需要 GPU。但第②步加载权重 `.to(model.device)`（device=`cuda:0`）和第⑤步真正 launch 内核都需要 GPU，会在 `torch.cuda.set_device` 处报错。所以无 GPU 时，验证止步于"编译 + 能否 import"。

## 5. 综合实践：端到端跑通（或在无 GPU 时完整复盘）

**任务**：把本讲三个最小模块串成一条可复现的流程，并产出一份"环境 + 命令 + 预期产物 / 失败点"的核对清单。

**步骤**：

1. **确认环境变量**（依据 [README.md:16-28](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/README.md#L16-L28)）：
   ```bash
   export THUNDERKITTENS_ROOT=$(pwd)/ThunderKittens
   export MEGAKERNELS_ROOT=$(pwd)
   export PYTHON_VERSION=3.12   # 改成本机版本
   export GPU=H100              # 目标机型；H100=sm_90a，B200=sm_100a
   ```
2. **编译**：`cd demos/low-latency-llama && make`，确认生成 `mk_llama*.so`。把它和 4.1.5 练习 2 联系起来——想清楚为什么换 GPU 型号要重新 `make`。
3. **基准生成**（需 GPU，命令取自 [README.md:39-46](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/README.md#L39-L46)）：
   ```bash
   python megakernels/scripts/generate.py mode=mk prompt="tell me a funny joke about cookies" ntok=100
   ```
   对照 4.3.2 的命令链，把你在终端看到的 `Average time` / `Output text` / `Tokens per second` 分别对应到 [generate.py:185](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/generate.py#L185)、[generate.py:190](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/generate.py#L190)、[generate.py:207](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/generate.py#L207)。
4. **交互式聊天**（需 GPU）：`python megakernels/scripts/llama_repl.py`，注意它默认就是 `mode=mk`，会在终端打印 ASCII art 欢迎语（[llama_repl.py:133-134](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/llama_repl.py#L133-L134)），随后进入 `>>> ` 提示符。

**无 GPU 的复盘版**：跳过第 3、4 步的"运行"，改为——
- 在 4.1.4 手写两条编译命令的差异（H100 vs B200）；
- 在 4.3.4 验证到"`make` 成功且 `.so` 可被 `import`"为止；
- 写一段话说明：如果继续执行 `generate.py mode=mk`，会在哪一行因无 GPU 而失败（答：[generate.py:86](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/generate.py#L86) `torch.cuda.set_device`）。

**产出**：一张三列表格——「命令 | 作用 | 失败时的报错线索」，覆盖 `git submodule update`、4 个 `export`、`make`、`python .../generate.py mode=mk`、`python .../llama_repl.py`。

> **待本地验证**：所有实际运行结果（编译耗时、显存占用、tokens/s 数值、报错文本）依赖具体机器与 CUDA 版本，本讲只保证命令与代码定位准确。

## 6. 本讲小结

- `make` 通过 [Makefile](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/Makefile) 用 `nvcc` 把 `llama.cu` 编译成 `mk_llama*.so`；四个必设环境变量是 `THUNDERKITTENS_ROOT`、`MEGAKERNELS_ROOT`、`PYTHON_VERSION`、`GPU`。
- `NVCCFLAGS` 同时承载三类内容：优化/诊断开关（`-O3`、`--use_fast_math`、`-Xptxas=--warn-on-spills`、`-lineinfo`）、头文件与 Python 胶水（`-I...`、pybind11 头、`-shared -fPIC`）、GPU 架构（`-arch` + `-DKITTENS_*`）。
- **H100 与 B200 的编译差异只有两处**：`-arch=sm_90a` ↔ `-arch=sm_100a`，以及 B200 多一个 `-DKITTENS_BLACKWELL`；后者还会把内核里编译期 SM 数量从 132 改成 148（[llama.cuh:149-158](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cuh#L149-L158)），故 `.so` 与 GPU 型号绑定。
- [llama.cu](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cu) 的 `PYBIND11_MODULE(mk_llama, m)`（[第 26 行](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cu#L26)）定模块名、`bind_kernel(m, "mk_llama", ...)`（[第 28-32 行](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cu#L28-L32)）定函数名，二者都叫 `mk_llama`，共同支撑 `from mk_llama import mk_llama`。
- `.so` 被 [mk.py:5-7](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/mk.py#L5-L7) 动态加载：它把 `mk_dir`（默认 `demos/low-latency-llama`）加入 `sys.path` 后再 import——这是"Python↔CUDA"接头的真正落点。
- 运行入口：`generate.py mode=mk`（注意默认 mode 无效，必须显式指定）做基准计时；`llama_repl.py`（默认就是 `mode=mk`）做交互聊天。两者都在第③步经 `make_mk_interpreter` 加载 `.so`。

## 7. 下一步学习建议

- **进入内核内部**：既然已经能编译并定位到 `mk` 内核，下一步建议精读 [include/megakernel.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/megakernel.cuh) 与 [include/controller/controller.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh)，理解 megakernel 如何按 warp 分工、逐条取指执行。
- **理解 `globs` 数据契约**：把 [llama.cu:32-52](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cu#L32-L52) 的成员顺序与 [megakernels/instructions.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/instructions.py) 里的 `serialize()` 对照，看清 Python 侧如何按相同顺序填充张量。
- **三种 mode 对比**：在 [generators.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/generators.py) 中并排阅读 `PyTorchGenerator` / `PyVM_Generator` / `MK_Generator`，体会"同一份指令、三种解释器"的设计差异，并用 `mode=torch` 作为"无 megakernel 的对照基准"。
- **调优准备**：学会读 `-Xptxas=--warn-on-spills` 的告警，并结合 `nsys`/`ncu`（得益于 `-lineinfo`）剖析 `generate.py mode=mk` 的真实性能瓶颈。
