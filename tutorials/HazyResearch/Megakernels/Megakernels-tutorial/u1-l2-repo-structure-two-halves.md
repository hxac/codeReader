# 仓库结构与双视角架构

> 本讲对应手册单元 U1·L2，承接 [U1·L1]（项目总览）。建议先读完 L1 再进入本讲。

## 1. 本讲目标

学完本讲，你应当能够：

1. 画出 Megakernels 仓库的**顶层目录树**，并说出每个目录的职责。
2. 区分两个"半边"：**Python 编排层**（`megakernels/`，负责"生成指令"）与 **CUDA 内核层**（`include/` + `demos/`，负责"执行指令"）。
3. 定位四个关键入口文件：`dispatch.py`、`mk.py`、`llama.cu`、`megakernel.cuh`，并说清它们各自被谁调用。
4. 理解 `util/mk_init/` 这个脚手架工具"像 `npm init` 一样新建一个 megakernel 项目"的角色。
5. 独立统计各目录的文件数量，并画出一张 Python→CUDA 的模块依赖草图。

## 2. 前置知识

- **Python 包（package）**：一个含 `__init__.py` 的目录，可以被 `import`。Megakernels 把所有 Python 代码放进名为 `megakernels` 的包里。
- **CUDA / `.cu` / `.cuh`**：NVIDIA GPU 的 C++ 方言。`.cu` 是可编译的源文件，`.cuh` 是头文件（header-only），通过 `#include` 拼接在一起。
- **kernel（核函数）**：用 `__global__` 标记、在 GPU 上并发执行的函数。本项目的核心是一个叫 `mk`（megakernel）的大 kernel。
- **warp（线程束）**：GPU 上 32 个线程组成的基本执行单元。本项目里不同 warp 分工不同（取指令、搬数据、算数）。
- **pybind11**：把 C++ 函数/类编译成 Python 可调用模块的胶水库。本项目的 `mk_llama` 就是这么暴露给 Python 的。
- **指令（instruction）**：你可以把它类比成 CPU 的机器码——一条整数数组，描述"做一次矩阵×向量、或一次注意力计算"。本项目的核心思想是：**Python 把模型翻译成一串指令，GPU 上的一个 megakernel 像虚拟机一样逐条执行这些指令**。

如果上面某些词还陌生，先记住最后一句话，本讲会逐步展开。

## 3. 本讲源码地图

| 文件 / 目录 | 角色 | 属于哪一半 |
| --- | --- | --- |
| [README.md](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/README.md) | 安装与运行说明 | 文档 |
| [pyproject.toml](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/pyproject.toml) | 定义 Python 包 `megakernels` | Python 侧 |
| [megakernels/dispatch.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/dispatch.py) | 按 `mode` 选择调度器与解释器 | Python 侧（枢纽） |
| [megakernels/mk.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/mk.py) | 动态加载并调用编译后的 CUDA megakernel | Python↔CUDA 桥 |
| [megakernels/instructions.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/instructions.py) | "指令"与"全局张量"的数据类定义 | Python 侧 |
| [megakernels/generators.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/generators.py) | 三种生成器：`torch` / `pyvm` / `mk` | Python 侧 |
| [demos/low-latency-llama/llama.cu](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cu) | 用 pybind11 把 GPU kernel 暴露成 `mk_llama` | CUDA 侧（入口） |
| [include/megakernel.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/megakernel.cuh) | megakernel 入口模板，划分 warp 角色 | CUDA 侧（VM 内核） |
| [include/config.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/config.cuh) | 硬件/流水线常量（页数、warp 数等） | CUDA 侧（配置） |
| [include/controller/controller.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh) | 控制器 warp：从显存逐条"取指令" | CUDA 侧（执行） |
| [demos/low-latency-llama/Makefile](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/Makefile) | 用 `nvcc` 把 `llama.cu` 编译成 `mk_llama.so` | CUDA 侧（构建） |
| [util/mk_init/main.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/util/mk_init/main.py) | 脚手架：交互式新建一个 megakernel 项目 | 工具 |

## 4. 核心概念与源码讲解

### 4.1 目录结构总览

#### 4.1.1 概念说明

Megakernels 不是一个"单一语言"的项目，而是 **Python + CUDA 两套代码紧密协作**的混合仓库。要读懂它，第一步就是建立"目录地图"：知道哪部分跑在 CPU 上、哪部分跑在 GPU 上，以及它们在哪里接上头。

仓库根目录除了 `README.md`、`pyproject.toml`、`LICENSE` 这些常规文件外，有 5 个关键目录：

```
Megakernels/
├── README.md            # 安装与运行说明
├── pyproject.toml       # 声明 Python 包 megakernels 的依赖
├── .gitmodules          # 声明 ThunderKittens 为 git 子模块
├── ThunderKittens/      # 【子模块】底层 GPU 原语库 kittens（不在本讲深入）
├── megakernels/         # 【Python 编排层】—— 生成"指令"
├── include/             # 【CUDA 内核层·通用虚拟机】—— 执行"指令"
├── demos/               # 【CUDA 内核层·具体应用】
│   └── low-latency-llama/
├── util/
│   └── mk_init/         # 【脚手架】像 npm init 一样新建 megakernel 项目
└── Megakernels-tutorial/  # 本手册（讲义）所在目录
```

#### 4.1.2 核心流程：两半边如何分工

理解整个仓库只需抓住一条主线：

> **Python 侧负责"生成指令"，CUDA 侧负责"执行指令"。**

具体来说：

1. Python 把一个 Llama 模型的一次前向计算，编译（schedule）成一串**指令**（整数数组）。
2. 这些指令连同所有权重、激活张量，被放进一个叫 `globs` 的"全局状态"对象里。
3. Python 调用编译好的 CUDA 模块 `mk_llama`，把 `globs` 整个交给 GPU。
4. GPU 上的一个 megakernel（`mk`）像虚拟机一样，**逐条取出并执行**这些指令，把结果写回 `globs` 里的输出张量。
5. Python 读回输出张量，得到下一个 token。

这张草图会在 4.2 和 4.3 中用源码逐段坐实。

#### 4.1.3 源码精读：README 给出的两条命令

仓库顶层 [README.md](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/README.md) 用两段命令勾勒了"两半边"：

[README.md:14-28](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/README.md#L14-L28) —— 这段先 `cd demos/low-latency-llama && make`，对应 **CUDA 侧**：把 `llama.cu` 编译成 Python 可导入的 `mk_llama` 扩展（见 4.3）。

[README.md:39-46](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/README.md#L39-L46) —— 这段运行 `python megakernels/scripts/generate.py mode=mk ...`，对应 **Python 侧**：编排并最终调用刚编译出的 megakernel。

> 注意先后顺序：**必须先编译 CUDA 侧（`make`），Python 侧才能 `mode=mk` 运行**。这正是两半边耦合的体现。

#### 4.1.4 代码实践：统计每个目录的文件数量

这是一个纯文件系统操作，无需 GPU，用于帮你"用手摸一遍"目录结构。

1. **实践目标**：用命令统计各目录下源码文件数量，建立对仓库规模与分布的直觉。
2. **操作步骤**（在仓库根目录执行）：

   ```bash
   # Python 侧：megakernels/ 下所有 .py
   find megakernels -name '*.py' | wc -l

   # CUDA 通用虚拟机：include/ 下所有 .cuh
   find include -name '*.cuh' | wc -l

   # CUDA 具体应用：demos/ 下所有 .cu / .cuh
   find demos -name '*.cu' -o -name '*.cuh' | wc -l

   # 脚手架：util/mk_init/ 下所有文件
   find util/mk_init -type f | wc -l
   ```

3. **需要观察的现象**：四条命令各输出一个数字。
4. **预期结果**（基于当前 HEAD `7309cec`）：

   | 目录 | 文件数 | 说明 |
   | --- | --- | --- |
   | `megakernels/` | 23 个 `.py` | Python 编排层，规模最大 |
   | `include/` | 13 个 `.cuh` | CUDA 通用虚拟机（全是头文件） |
   | `demos/` | 10 个 `.cu`/`.cuh` | Llama 的具体算子实现 |
   | `util/mk_init/` | 7 个文件 | 脚手架模板 |

   如果你看到的数字与上表一致，说明你的工作目录与本讲所基于的 HEAD 一致。若不一致，多半是 `ThunderKittens` 子模块未初始化，或目录被改动过——可先忽略，不影响理解架构。

5. **说明**：以上计数由本讲作者在生成时实际执行 `Glob` 检索得到，并非凭空估计。

#### 4.1.5 小练习与答案

- **练习 1**：仓库里有一个 `ThunderKittens/` 目录，它是本项目的源码吗？
  - **答案**：不是。它是 git 子模块（见 `.gitmodules`），是底层 GPU 原语库 kittens，`include/megakernel.cuh` 顶部 `#include "kittens.cuh"` 就来自它。本讲把它当作"外部依赖"，不深入。
- **练习 2**：`include/` 里几乎没有 `.cu`、全是 `.cuh`，这说明什么？
  - **答案**：说明 `include/` 是一套 header-only 的通用虚拟机模板。它本身不直接编译，而是被 `demos/low-latency-llama/llama.cu` 等"具体应用"`#include` 之后，随应用一起编译。

---

### 4.2 Python 编排层：`megakernels/`

#### 4.2.1 概念说明

`megakernels/` 是一个标准 Python 包（目录里有 [__init__.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/__init__.py)，并被 [pyproject.toml](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/pyproject.toml) 注册）。它承担三件事：

1. **建模**：用 PyTorch 定义 Llama 模型（`llama.py`），并加载预训练权重。
2. **编排（schedule）**：把模型翻译成一串"指令"（`scheduler.py` + `instructions.py`）。
3. **驱动**：选择用哪种方式执行这串指令（`dispatch.py` + `generators.py`）。

其中"驱动"提供了三种执行路径，形成一条由慢到快、互为参照的梯度：

- `torch`：纯 PyTorch 参考实现（正确性基准）。
- `pyvm`：在 **Python 里**解释同一串指令（`python_vm.py`），用于验证"指令"的语义对不对。
- `mk`：把同一串指令交给 **CUDA megakernel** 执行（最终高性能路径）。

> 关键点：三种路径执行的是**同一套指令语义**。`pyvm` 和 `mk` 共享同一份指令，只是解释器不同——这正是"指令作为 Python 与 CUDA 之间契约"的体现。

#### 4.2.2 核心流程：从命令行到指令

以 `mode=mk` 运行为例，Python 侧的调用链如下（伪代码）：

```
scripts/generate.py        # 命令行入口，读 mode=mk
  └─ dispatch.make_schedule_builder("latency")   # 选调度器
        scheduler.build(model)                    # 把模型编译成指令 schedule
  └─ dispatch.make_mk_interpreter("latency", mk_dir)  # 选解释器
        mk.get_mk_func(mk_dir)                    # 动态 import 编译好的 mk_llama
  └─ MK_Generator(model, interpreter, schedule)
        gen.run(...)                              # 每生成一个 token 调一次
          └─ interpreter.interpret(globs)         # 调用 CUDA megakernel
```

`globs` 里装着两样东西：① 一张形状为 `[num_instructions, 32]` 的整数张量 `instructions`（即"指令"）；② 所有权重与激活张量。

#### 4.2.3 源码精读

**(a) 指令长什么样**——[instructions.py:83-119](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/instructions.py#L83-L119)

每个指令是一个 dataclass，`opcode()` 给出操作码（如 `NoOp` 是 `0`），`serialize()` 把它压平成一串整数。一条指令在 GPU 上占用的宽度由 CUDA 侧常量决定（见 4.3）：

\[ \text{每条指令大小} = \text{INSTRUCTION\_WIDTH} \times 4\text{ 字节} = 32 \times 4 = 128\text{ 字节} \]

对应 CUDA 侧 [config.cuh:14-15](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/config.cuh#L14-L15) 的 `INSTRUCTION_WIDTH = 32`（注释明确写 "128 bytes per instruction"）。Python 的 `serialize()` 与 CUDA 的 `int[32]` 必须逐字段对齐——这就是两半边的"二进制契约"。

**(b) 全局张量 `globs`**——[instructions.py:10-69](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/instructions.py#L10-L69)

`BaseGlobals` 把模型权重、KV cache、激活值、`instructions`、`barriers`、`timings` 等打包成一个对象。注意 `__post_init__` 里 `instructions` 与 `timings` 初始为 `None`，要等调度器填好才赋值。这个 `globs` 之后会**整个**传给 GPU。

**(c) 枢纽：按 mode 选调度器/解释器**——[dispatch.py:17-42](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/dispatch.py#L17-L42)

三个字典 `BUILDER_MAP` / `MK_INTERPRETER_MAP` / `INSTRUCTION_TO_SOLVER_MAP` 都按 `"latency"` 或 `"throughput"` 两套 demo 来分流；下面的三个工厂函数 `make_schedule_builder` / `make_mk_interpreter` / `make_pyvm_interpreter` 把选择结果返回给调用方。所以 `dispatch.py` 是 Python 侧的"调度枢纽"，所有入口（`generate.py`、`llama_repl.py`）都先经过它。

**(d) 加载 CUDA 扩展的桥**——[mk.py:5-17](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/mk.py#L5-L17)

`get_mk_func(mk_dir)` 把 `mk_dir`（默认指向 `demos/low-latency-llama`）加入 `sys.path`，然后 `from mk_llama import mk_llama`——这里的 `mk_llama` 正是 [llama.cu](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cu) 编译出的 `.so`。`MK_Interpreter` 把它包成一个统一的 `interpret(globs)` 接口。

**(e) 真正发起 GPU 调用**——[generators.py:121-143](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/generators.py#L121-L143)

`MK_Generator.run()` 在每个 token 步里：填好 `hidden_states`、设置 `pos_id`，然后调用 `self.interpreter.interpret(self.schedule.globs)`——这一句就是把整包 `globs`（含指令）送进 GPU megakernel。`PyVM_Generator`（[generators.py:166-201](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/generators.py#L166-L201)）走的是同一接口，但 `interpret` 换成了纯 Python 解释器 [python_vm.py:90-95](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/python_vm.py#L90-L95)。

#### 4.2.4 代码实践：追踪 `mode=mk` 的调用链

1. **实践目标**：不运行代码，仅靠阅读，把 Python 侧"从命令行到 GPU 调用"的链条串起来。
2. **操作步骤**：
   1. 打开 [scripts/generate.py:146-165](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/generate.py#L146-L165)，找到 `case "mk":` 分支，记下它构造的是哪个 Generator。
   2. 注意 [generate.py:36](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/generate.py#L36) 里 `mk_dir` 默认指向 `demos/low-latency-llama`——这就是 Python 与 CUDA 的"接头地点"。
   3. 跳到 [generators.py:121-143](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/generators.py#L121-L143)，找到调用 `self.interpreter.interpret(...)` 的那一行。
   4. 再看 [demos/latency/mk.py:8-49](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/mk.py#L8-L49) 的 `interpret_with_mk`，确认它把 `globs` 里的所有张量按固定顺序传给 `mk_func(...)`。
3. **需要观察的现象**：你应该能看到 `globs.instructions`（指令）、`globs.barriers`（同步屏障）、各权重与激活张量，作为参数被原样传给 `mk_func`。
4. **预期结果**：画出一条链 `generate.py → MK_Generator.run → MK_Interpreter.interpret → interpret_with_mk → mk_func(=mk_llama)`。`mk_func` 的实现在 CUDA 侧（4.3）。
5. **说明**：本实践为"源码阅读型实践"，无需 GPU 即可完成。

#### 4.2.5 小练习与答案

- **练习 1**：`dispatch.py` 里为什么需要 `latency` 和 `throughput` 两套并列的实现？
  - **答案**：因为本项目同时提供"低延迟"和"高吞吐"两种目标场景，二者的指令集、调度策略不同。`megakernels/demos/latency/` 与 `megakernels/demos/throughput/` 各有一份 `scheduler.py` / `mk.py` / `python_vm.py` / `instructions.py` 四件套，由 `dispatch.py` 按 mode 选择。
- **练习 2**：`pyvm` 模式存在的意义是什么？
  - **答案**：它用纯 Python（[python_vm.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/python_vm.py)）解释**与 `mk` 相同的指令**，用于在没有 GPU 或调试时验证指令语义是否正确，相当于"指令集的参考实现"。

---

### 4.3 CUDA 内核层：`include/` 与 `demos/`

#### 4.3.1 概念说明

CUDA 侧也分两层：

- **`include/`：通用虚拟机（VM）**。它不知道"Llama"是什么，只提供"如何启动一个 megakernel、如何按 warp 分工、如何逐条取指令并调度执行"的通用框架。它是 header-only 的模板库。
- **`demos/low-latency-llama/`：具体应用**。它把 Llama 的每种算子（attention、matvec、rmsnorm…）实现成可被 VM 调度的 "op"，定义好所有全局张量（`llama.cuh`），并写出入口 `llama.cu`（用 pybind11 暴露成 `mk_llama`）。

打个比方：`include/` 是一台"CPU 的取指—译码—执行流水线"，`demos/` 是为这台机器写的一套"专用程序 + 接线说明"。

#### 4.3.2 核心流程：一个 megakernel 启动后发生什么

当 Python 调用 `mk_func(globs...)` 时，GPU 上启动一个名为 `mk` 的 kernel。该 kernel 内部按 warp 分成 5 类角色（见 [megakernel.cuh:118-140](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/megakernel.cuh#L118-L140)）：

| warp 角色 | 职责 | 对应头文件 |
| --- | --- | --- |
| controller | 从显存**取指令**、分配页、构造信号量 | [controller/controller.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh) |
| loader | 把所需数据从显存搬到共享内存 | `loader.cuh` |
| storer | 把结果从共享内存搬回显存 | `storer.cuh` |
| launcher | 启动具体算子（op） | `launcher.cuh` |
| consumer（多个） | 真正做矩阵/注意力计算的主力 warp | `consumer.cuh` |

简化后的执行循环（伪代码）：

```
启动 mk<<<grid, block>>>(g)          # g 即 globs，通过 __grid_constant__ 传入
  每个 warp 根据自身 id 进入各自 main_loop：
    controller: for 每条指令 i:
                   从 g.instructions 取第 i 条 → 放入流水线槽
                   构造信号量、分配页 → 通知其它 warp "就绪"
    loader/storer/launcher/consumer: 等待信号量 → 处理当前指令的数据 → 完成
```

控制器的那条主循环尤其重要，因为它直接体现了"GPU 执行 Python 生成的指令"。

#### 4.3.3 源码精读

**(a) VM 配置常量**——[config.cuh:7-52](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/config.cuh#L7-L52)

`default_config` 定义了流水线级数、指令宽度、warp 数、共享内存分页等关键常量。例如 `NUM_CONSUMER_WARPS = 16`、`NUM_WARPS = 4 + 16 = 20`（4 个管理 warp + 16 个计算 warp），`INSTRUCTION_PIPELINE_STAGES = 2`（双缓冲取指）。注意 [config.cuh:42-44](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/config.cuh#L42-L44) 用 `static_assert(NUM_PAGES == 13)` 把分页数硬钉成 13——改配置会直接编译失败，这是一种保护。

**(b) megakernel 入口与 warp 分工**——[megakernel.cuh:166-171](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/megakernel.cuh#L166-L171)

真正的 `__global__` kernel 是 `mk`，它接收一个 `__grid_constant__ globals g`（即 Python 传来的 globs），委托给 [megakernel.cuh:118-140](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/megakernel.cuh#L118-L140) 的分支：计算 warp 数多的进 `consumer::main_loop`，其余 4 个管理 warp 按 `warpid()` 分别进 loader/storer/launcher/controller。

**(c) 控制器取指循环**——[controller.cuh:24-132](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L24-L132)

这段是"GPU 执行指令"最直接的证据。`num_iters = g.instructions.rows()` 读出指令总数（即 Python 那张 `[N, 32]` 张量的行数），然后 `for (instruction_index = 0; instruction_index < num_iters; ...)` 逐条处理：取指（`load_instructions`）→ 分配页 → 构造信号量 → `arrive(instruction_arrived)` 通知其它 warp。可以看到 [controller.cuh:106-107](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L106-L107) 还会读 `opcode = instruction()[0]`——这与 Python 侧 [instructions.py:98](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/instructions.py#L98) `serialize()` 把 `opcode()` 放在第一个字完全对应。

**(d) 应用入口：暴露 `mk_llama`**——[llama.cu:26-53](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cu#L26-L53)

`PYBIND11_MODULE(mk_llama, m)` 用 `kittens::py::bind_kernel<mk<...>>(m, "mk_llama", &Bar, &instructions, &timings, &各权重..., &各激活...)` 把 kernel `mk` 及其全部全局张量绑定成一个 Python 可调用对象。注意它绑定的指针（`&llama_1b_globals::instructions` 等）与 Python 侧 [demos/latency/mk.py:8-49](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/mk.py#L8-L49) 传参的顺序是一一对应的——这是两半边接头的"插针脚定义"。`llama.cu` 顶部 [llama.cu:1-10](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cu#L1-L10) 还 `#include` 了各个算子 `.cu` 文件（attention/matvec/rms…），把"具体应用"拼装出来。

**(e) 构建过程**——[Makefile:1-41](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/Makefile#L1-L41)

`make` 用 `nvcc` 编译 `llama.cu`，`-I${MEGAKERNELS_ROOT}/include` 把通用 VM 头文件纳入搜索路径，`-I${THUNDERKITTENS_ROOT}/include` 引入 kittens 原语，最终输出 `mk_llama$(python3-config --extension-suffix)`（即 `.so`）。`GPU` 变量（H100/B200 等）决定目标架构宏（[Makefile:20-28](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/Makefile#L20-L28)）。

#### 4.3.4 代码实践：把"指令"在两端对上号

1. **实践目标**：验证 Python 写出的指令格式与 CUDA 读取的格式确实一致。
2. **操作步骤**：
   1. 在 [instructions.py:97-119](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/instructions.py#L97-L119) 确认 `serialize()` 的第一个字是 `opcode()`。
   2. 在 [config.cuh:14-15](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/config.cuh#L14-L15) 确认每条指令宽度是 32 个 int。
   3. 在 [controller.cuh:66-68](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L66-L68) 看 `load_instructions` 如何按 `instruction_index` 取第 i 条，再在 [controller.cuh:106-107](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh#L106-L107) 确认它用 `instruction()[0]` 当 opcode。
3. **需要观察的现象**：两端对"一条指令 = `[opcode, 其余字段...]`、宽度 32"的约定完全吻合。
4. **预期结果**：你能用自己的话讲清"Python `serialize()` 的输出 → `globs.instructions[N,32]` 张量 → controller 按行取指 → 读首字作 opcode"这条数据通路。
5. **说明**：本实践为源码阅读型，无需 GPU。若想真正运行，需先按 [README.md:14-28](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/README.md#L14-L28) 完成 `make`（待本地验证）。

#### 4.3.5 小练习与答案

- **练习 1**：为什么 `include/` 是 header-only，而 `demos/` 里有真正的 `.cu`？
  - **答案**：`include/` 是参数化的通用模板（`template <typename config, typename globals, typename... ops>`），必须由具体应用实例化后才能编译。`demos/low-latency-llama/llama.cu` 就是那个"实例化点"——它填入具体的 `llama_1b_globals` 与各算子类型，触发模板实例化并编译。
- **练习 2**：`mk` kernel 收到的 `g` 是什么？
  - **答案**：就是 Python 侧的 `globs`（[instructions.py:10-69](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/instructions.py#L10-L69)），经 pybind11 的 `bind_kernel` 以 `__grid_constant__` 形式传入，包含指令表、权重、激活、barriers、timings 等全部状态。

---

### 4.4 脚手架：`util/mk_init/`

#### 4.4.1 概念说明

`util/mk_init/` 是一个独立的命令行小工具，定位类似 `npm init`：**交互式地新建一个全新的 megakernel 项目骨架**。当你想用这套"Python 编排 + CUDA 虚拟机"框架去做一个新模型（不一定是 Llama）时，用它生成起点代码，而不是从 `demos/low-latency-llama/` 手动复制。

#### 4.4.2 核心流程

1. 用户运行 `python -m mk_init --name MyModel`（或交互输入项目名）。
2. 工具在 `sources/` 模板目录里读取文件（[main.py:122-128](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/util/mk_init/main.py#L122-L128) 列出了模板清单）。
3. 把模板里的占位符 `{{PROJECT_NAME}}` / `{{PROJECT_NAME_LOWER}}` 替换成真实项目名（[main.py:23-28](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/util/mk_init/main.py#L23-L28)），例如把 `{{PROJECT_NAME_LOWER}}.cu` 改名成 `mymodel.cu`。
4. 在目标目录创建 `src/`、`tests/` 子目录，写入 `config.cuh`、`.cu`、`setup.py`、`test_example.py` 等，并生成 `.gitignore`。

#### 4.4.3 源码精读

[mk_init/main.py:66-131](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/util/mk_init/main.py#L66-L131) 是 `main()`：解析 `--name`/`--target` 参数、校验项目名合法性、创建目录、复制并替换模板。模板本体放在 [util/mk_init/sources/](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/util/mk_init/sources/)，可以看到它生成了 `src/config.cuh`（一份精简版 VM 配置）和 `src/{{PROJECT_NAME_LOWER}}.cu`（一个最小入口，类似 `llama.cu` 的角色）。这意味着脚手架生成的项目天然符合 4.3 描述的"通用 VM + 具体应用"两层结构。

#### 4.4.4 代码实践：读懂脚手架生成什么

1. **实践目标**：在不运行的前提下，预测 `mk_init` 会生成哪些文件。
2. **操作步骤**：阅读 [main.py:122-128](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/util/mk_init/main.py#L122-L128) 的 `template_files` 列表，并对照 [util/mk_init/sources/](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/util/mk_init/sources/) 目录下的实际模板。
3. **需要观察的现象**：模板里有一个 `{{PROJECT_NAME_LOWER}}.cu`，文件名含占位符。
4. **预期结果**：若项目名为 `Foo`，会生成 `src/foo.cu`、`src/config.cuh`、`setup.py`、`tests/test_example.py`、`README.md`，以及 `.gitignore`。
5. **说明**：本实践为源码阅读型。若要真正运行，需先安装 `util/` 这个独立包（其有自己的 [pyproject.toml](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/util/pyproject.toml)）；运行结果待本地验证。

#### 4.4.5 小练习与答案

- **练习 1**：脚手架生成的 `.cu` 入口与 `demos/low-latency-llama/llama.cu` 是什么关系？
  - **答案**：同一类角色——都是"具体应用的实例化与 pybind11 入口"。脚手架给你一个最小空壳，`llama.cu` 则是这个空壳被填满 Llama 算子后的成品。

## 5. 综合实践：绘制 Python→CUDA 模块依赖草图

把本讲内容串起来，完成规格要求的核心任务：**绘制一张 Python→CUDA 的模块依赖草图，并标注哪一侧负责"生成指令"、哪一侧负责"执行指令"**。

**任务**：

1. 在一张纸或文本文件里画出下面这条依赖链，并为每个节点填上对应文件路径。
2. 用两种颜色（或标记）区分"生成指令"与"执行指令"。
3. 在两半边的交界处，写明它们交换的"数据契约"是什么。

**参考答案草图**（ASCII）：

```
【Python 编排层 —— 生成指令】
  scripts/generate.py (mode=mk)
        │
        ▼
  dispatch.py ── 选 scheduler/interpreter
        │
        ├── scheduler.build(model) ──► instructions.py: Instruction.serialize()
        │                                   │  产出 globs.instructions[N,32] 整数张量
        │                                   │  + globs.{weights, activations, barriers...}
        │                                   ▼
        └── mk.get_mk_func(demos/low-latency-llama) ──► import mk_llama (.so)
                    │
                    ▼
              generators.MK_Generator.run()
                    │  调 interpreter.interpret(globs)
                    ▼
   ══════════ 数据契约：globs（指令表 + 全部张量） ══════════
                    │
                    ▼
【CUDA 内核层 —— 执行指令】
  demos/low-latency-llama/llama.cu  ── pybind11: bind_kernel<mk>(...,"mk_llama")
        │
        ▼
  include/megakernel.cuh :: mk<<<>>>(g)   __global__ kernel
        │  按 warp 分工
        ├── controller/controller.cuh : for 每条指令 → 取指/分页/信号量
        ├── loader.cuh / storer.cuh   : 搬运数据
        ├── launcher.cuh              : 启动 op
        └── consumer.cuh              : 计算 (attention/matvec/...)
        │
        ▼
  结果写回 globs.{logits, hidden_states, k_cache, v_cache...}
        │
        ▼
【回到 Python】读取 globs.logits → argmax → 下一个 token
```

**关键标注**：

- **生成指令的一侧**：`megakernels/`（尤其 `scheduler.py` + `instructions.py`）。
- **执行指令的一侧**：`include/`（虚拟机框架）+ `demos/low-latency-llama/`（算子与入口）。
- **数据契约**：`globs` 对象——其中 `instructions` 是一张 `[N, 32]` 的 int 张量（每行一条指令，首字为 opcode，共 128 字节）；`barriers` 用于 warp 间同步；其余为权重与激活张量。两半边靠 pybind11 的 `bind_kernel` 按固定顺序对接这些张量。

**自检**：如果你的草图里能指出 [mk.py:7](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/mk.py#L7) 的 `from mk_llama import mk_llama` 是跨越两半边的那一行，且 [llama.cu:26](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cu#L26) 的 `PYBIND11_MODULE(mk_llama, ...)` 是它的另一端，那么你就真正理解了这套架构的"接头"。

## 6. 本讲小结

- Megakernels 是一个 **Python + CUDA 双语言**仓库，主线是"Python 生成指令、CUDA 执行指令"。
- `megakernels/`（23 个 `.py`）是 **Python 编排层**：建模、调度成指令、按 `torch`/`pyvm`/`mk` 三种方式驱动。
- `include/`（13 个 `.cuh`）是 **CUDA 通用虚拟机**：header-only 模板，提供 megakernel 入口与 controller/loader/storer/launcher/consumer 的 warp 分工。
- `demos/low-latency-llama/`（10 个 `.cu`/`.cuh`）是 **具体应用**：实现 Llama 各算子，用 pybind11 暴露 `mk_llama`。
- 两半边的**数据契约**是 `globs`，核心是一张 `[N,32]` 的整数指令表；Python 的 `serialize()` 与 CUDA 的 `int[32]` + `opcode = instruction[0]` 逐字段对齐。
- `util/mk_init/` 是脚手架，用于按 `demos/` 的结构新建一个空项目。
- 四个关键入口：`dispatch.py`（调度枢纽）、`mk.py`（加载 CUDA 扩展的桥）、`llama.cu`（CUDA 侧 pybind11 入口）、`megakernel.cuh`（VM kernel 本体）。

## 7. 下一步学习建议

- **下一步讲义**：进入"指令与调度"专题——精读 [scheduler.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py) 与 [demos/latency/scheduler.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/scheduler.py)，搞清一个 Llama 层是如何被翻译成指令序列的。
- **CUDA 侧深入**：阅读 [controller/controller.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/controller.cuh) 全文，理解取指流水线、页分配与信号量构造。
- **对照阅读**：把 [generators.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/generators.py) 的 `PyVM_Generator` 与 `MK_Generator` 并排看，体会"同一份指令、两种解释器"的设计。
- **动手准备**：按 [README.md](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/README.md) 在本地初始化子模块并 `make`，为后续能在 GPU 上跑 `mode=mk` 做准备。
