# 项目总览：什么是 megakernel 虚拟机

> 本讲是 Megakernels 学习手册的第一讲（u1-l1，beginner 阶段）。不需要你写过任何 CUDA 代码，我们会从「这个项目到底想解决什么问题」讲起，再带你读懂 README、依赖清单和它最重要的子模块。所有源码引用都附带永久链接，你可以点进去对照阅读。

---

## 1. 本讲目标

学完本讲，你应该能够：

1. 用一句话说清 **Megakernels** 项目要解决的问题——把整个 LLM 推理塞进一个常驻 GPU 的「megakernel 虚拟机」，以追求极低延迟。
2. 理解三个关键概念：**megakernel（超大内核）**、**持久化内核（persistent kernel）**、**片上虚拟机（on-chip VM）**，以及它们为什么能带来低延迟。
3. 读懂 README 里的**安装**和**运行**步骤，知道每一步在做什么。
4. 读懂 `pyproject.toml`，说出包名、Python 版本要求以及全部依赖的用途。
5. 理解 **ThunderKittens 子模块**是什么、为什么它是整个项目的基石，以及 `.gitmodules` 如何把它钉在一个特定分支上。
6. 说清楚为什么编译时需要 `THUNDERKITTENS_ROOT`、`GPU`、`PYTHON_VERSION` 这几个环境变量。

---

## 2. 前置知识

本讲对读者几乎没有硬性前置要求，但下面几个概念会帮助你更快理解。

| 概念 | 通俗解释 |
| --- | --- |
| **GPU / 内核（kernel）** | GPU 上运行的一段并行程序。通常一次「推理」会启动成百上千个 kernel，每个做完就把 GPU 交还。 |
| **kernel launch 开销** | 每次从 CPU 端启动一个 kernel，都要付出「排命令、同步、走 PCIe」的固定代价。kernel 又小又多时，这个开销会主导总时间。 |
| **延迟（latency）** | 从「输入一个 prompt」到「拿到第一个/下一个 token」的时间。延迟敏感的场景（实时对话）比吞吐（批量离线）更在意它。 |
| **LLM 推理** | 给定已生成的 token，预测下一个 token。对自回归模型来说，生成 N 个 token 就是跑 N 次模型前向。 |
| **git submodule（子模块）** | 在一个 git 仓库里嵌入另一个仓库。父仓库只记录子仓库的「某个提交 + 远程地址」，需要单独 `init/update` 才会真正下载代码。 |

> 不熟悉 CUDA 术语没关系，本讲只用到最浅的一层。后续讲义会深入到 warp、shared memory、TMA 等细节。

---

## 3. 本讲源码地图

本讲涉及的关键文件如下（仓库根目录为 `/`）：

| 文件 | 行数 | 作用 |
| --- | --- | --- |
| [README.md](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/README.md) | 47 | 项目入口文档：安装步骤、低延迟 Llama demo 的编译与运行命令。 |
| [pyproject.toml](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/pyproject.toml) | 26 | Python 包定义：包名 `megakernels`、Python 版本要求、依赖清单。 |
| [.gitmodules](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/.gitmodules) | 4 | 声明 ThunderKittens 子模块：路径、远程地址、钉死的分支。 |
| [demos/low-latency-llama/Makefile](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/Makefile) | 42 | 编译 Llama megakernel 的 Makefile，揭示了 `THUNDERKITTENS_ROOT`/`GPU`/`PYTHON_VERSION` 的真实用途。 |
| [include/megakernel.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/megakernel.cuh) | 173 | 「虚拟机」本体：一个持久化 CUDA kernel，内部按 warp 分工扮演不同功能单元。本讲只读它来建立直觉。 |

> 前三个文件是本讲**必须覆盖**的最小模块；后两个用来佐证「虚拟机」概念、解释环境变量，作为延伸阅读。

---

## 4. 核心概念与源码讲解

### 4.1 什么是 megakernel 虚拟机：持久化内核与低延迟动机

#### 4.1.1 概念说明

先建立一个直觉。

传统做法：把 LLM 的一层拆成很多个小 kernel（attention 一个、matmul 一个、RMSNorm 一个……），每生成一个 token，CPU 就要依次启动几百个 kernel。每个 kernel 启动都要付 **kernel launch 开销**，还要在 CPU 和 GPU 之间来回「对话」。token 数一多，光是这些开销就占了相当大比例的总时间——对实时聊天这种**延迟敏感**场景非常不划算。

Megakernels 的核心想法是反过来的：

> **与其启动一千个小 kernel，不如启动一个「超大 kernel」（megakernel），让它一直常驻在 GPU 上，自己负责跑完整个模型。**

这就是三个概念的名字来源：

- **megakernel（超大内核）**：一个巨大的、自己干完所有活的 CUDA kernel。
- **持久化内核（persistent kernel）**：kernel 启动后**不退出**，一直占着 SM（流多处理器）等活干，直到显式结束。
- **片上虚拟机（on-chip VM）**：既然这个大 kernel 要自己干所有活，它内部就长得很像一台**迷你计算机**——有不同的「功能单元」各司其职，模型的一层层算子被当成「指令」喂给它执行。所以项目里直接管它叫 **VM（virtual machine）**。

低延迟的来源：**一次 kernel 启动 + 几乎没有 CPU↔GPU 往返**，省掉了成百上千次 launch 开销和同步代价；同时数据尽量留在片上（shared memory / 寄存器），减少显存搬运。

> 关键洞察：这里的「虚拟机」不是 Java/Python 那种跑字节码的 VM，而是**「在 GPU 片上用 warp 硬件搭出来的一台专用小机器」**。它的「指令」是模型算子，它的「CPU」是 GPU 上的线程束（warp）。

#### 4.1.2 核心流程

把 megakernel VM 类比成一台单核 CPU，执行过程大致如下：

```
启动一个持久化 kernel（mk）  ── 只 launch 一次
        │
        ▼
┌─────────────────────────────────────────────┐
│  megakernel 内部，warps 按角色分工：           │
│  controller  ← 取指/译码：决定下一条「算子指令」  │
│  launcher    ← 派发：把算子交给执行单元          │
│  loader      ← 内存载入：从显存搬数据进片上       │
│  storer      ← 内存写回：把结果搬回显存           │
│  consumer    ← ALU：真正做 attention / matvec 计算│
└─────────────────────────────────────────────┘
        │  循环执行「算子指令」，直到生成完所有 token
        ▼
   kernel 退出，返回结果
```

要点：

1. **只 launch 一次**：整个推理过程理论上只需一次 kernel 启动。
2. **片上自驱**：`controller` 在 GPU 上自己取下一条「指令」，不需要 CPU 干预。
3. **算子即指令**：模型里的 attention、矩阵向量乘等被当成 VM 的「操作码」。

#### 4.1.3 源码精读

上面的类比不是空想，而是直接体现在源码里。看 [include/megakernel.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/megakernel.cuh) 这一段——它就是 VM 的「主循环」，按 warp 号把不同 warp 派到不同功能单元：

[include/megakernel.cuh:L118-L140](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/megakernel.cuh#L118-L140) —— 这段是 VM 的角色分工：`consumer` warp 跑计算主循环（`consumer::main_loop`），其余 warp 按 `switch(warpid)` 分别进入 `loader`、`storer`、`launcher`、`controller`，正好对应 4.1.2 里画出的「内存载入 / 内存写回 / 派发 / 取指译码」四个角色。

代码骨架（只看结构，省略模板参数）：

```cpp
if (kittens::warpid() < config::NUM_CONSUMER_WARPS) {
    // consumer：真正的计算单元（attention / matvec 等）
    ::megakernel::consumer::main_loop<...>(g, mks);
} else {
    switch (kittens::warpgroup::warpid()) {
        case 0: ::megakernel::loader::main_loop<...>(g, mks); break;     // 载入
        case 1: ::megakernel::storer::main_loop<...>(g, mks); break;     // 写回
        case 2: ::megakernel::launcher::main_loop<...>(g, mks); break;   // 派发
        case 3: ::megakernel::controller::main_loop<...>(g, mks); break; // 取指/译码
    }
}
```

而且源码注释里直接用了 **VM** 这个词来描述它：

[include/megakernel.cuh:L152-L155](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/megakernel.cuh#L152-L155) —— 调试分支里打印 `"Overall VM execution time"`，可见作者就是把这一整段当作「虚拟机执行」来看待的。

> 注意：本讲只是用这个文件**建立直觉**，不需要看懂每行。后续讲义会逐个解剖 controller / launcher / loader / storer / consumer。另外，「算子即指令」的具体编码（每条指令长什么样、怎么调度）来自 `megakernels/instructions.py`、`megakernels/scheduler.py` 等 Python 侧文件，也不在本讲范围内。

#### 4.1.4 代码实践（源码阅读型）

**实践目标**：用肉眼确认「megakernel = 一个自驱动的片上虚拟机」不是营销话术，而是写在 C++ 里的真实结构。

**操作步骤**：

1. 打开永久链接 [include/megakernel.cuh:L118-L140](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/megakernel.cuh#L118-L140)。
2. 数一数：`switch` 里一共出现了几个角色名？分别对应 4.1.2 流程图里的哪个框？
3. 思考：为什么 `consumer` 是用 `if (warpid() < NUM_CONSUMER_WARPS)` 判定，而其余四个是 `switch(warpid())` 各取一个？（提示：计算量大、需要很多 warp；控制/搬运只需要少量。）

**需要观察的现象**：你会看到 5 个 `main_loop` 调用，分别属于 `consumer / loader / storer / launcher / controller`。

**预期结果**：与 4.1.2 的流程图一一对应，验证「VM 由若干功能单元组成」的说法。

> 本实践不运行任何命令，属于「源码阅读型实践」。

#### 4.1.5 小练习与答案

**练习 1**：用一句话解释「persistent kernel」和「megakernel」的关系。
**答案**：megakernel 是一种 persistent kernel——它启动后不退出、常驻 SM，自己跑完整个模型；「mega」强调它把原本上千个小 kernel 的活全揽下来了。

**练习 2**：为什么这种设计对**延迟**友好，却不一定对所有场景都最优？
**答案**：它消除了成百上千次 kernel launch 开销和 CPU↔GPU 往返，所以单条请求的延迟很低；但它长期占用 SM、控制逻辑复杂，对追求**吞吐**的大批量离线推理未必是最优解。

---

### 4.2 README：从零安装与运行

#### 4.2.1 概念说明

README 是你接触项目的第一站。它分成两块：

- **Installation**：把 Python 包 `megakernels` 装好（包括把 ThunderKittens 子模块拉下来）。
- **Low-Latency Llama Demo**：编译那个 C++/CUDA megakernel，然后跑交互对话或基准测试。

理解的关键：这个项目**既有 Python 侧（调度、指令生成），又有 C++/CUDA 侧（真正的 VM 内核）**。所以「安装」分两步——先装 Python 包，再 `make` 编译出 GPU 内核（它会被编译成一个 Python 可直接 import 的扩展模块）。

#### 4.2.2 核心流程

```bash
# —— 安装阶段 ——
git submodule update --init --recursive   # ① 拉取 ThunderKittens 子模块
pip install uv                             # ② 安装 uv（更快的 pip 替代）
uv pip install torch ... --index-url cu128 # ③ 装匹配 CUDA 12.8 的 torch
uv pip install -e .                        # ④ 以「可编辑」模式装 megakernels 本包

# —— 编译阶段（在仓库根目录）——
export THUNDERKITTENS_ROOT=$(pwd)/ThunderKittens   # 给 nvcc 指向子模块头文件
export MEGAKERNELS_ROOT=$(pwd)                     # 给 nvcc 指向本仓库 include/
export PYTHON_VERSION=3.12                          # 匹配你的解释器版本
export GPU=H100                                     # 选目标 GPU 架构
cd demos/low-latency-llama && make                  # 编译出 megakernel 扩展

# —— 运行阶段 ——
python megakernels/scripts/llama_repl.py            # 交互对话
python megakernels/scripts/generate.py mode=mk prompt="..." ntok=100  # 基准
```

#### 4.2.3 源码精读

**安装四连**在 [README.md:L8-L12](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/README.md#L8-L12)：

```bash
git submodule update --init --recursive
pip install uv
uv pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu128
uv pip install -e .
```

- 第 1 行必须最先做：`--init --recursive` 把 ThunderKittens 子模块（4.4 节详讲）真正下载下来，否则后面编译会找不到头文件。
- 第 3 行用 `--index-url .../cu128` 指定 **CUDA 12.8** 版的 torch——因为 megakernel 用到了 Hopper/Blackwell 的新特性，需要较新的 CUDA。
- 第 4 行 `-e .` 是「可编辑安装」：你改了 `megakernels/` 里的 Python 代码，不用重装就生效。

**编译四连**在 [README.md:L21-L27](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/README.md#L21-L27)：

```bash
export THUNDERKITTENS_ROOT=$(pwd)/ThunderKittens
export MEGAKERNELS_ROOT=$(pwd)
export PYTHON_VERSION=3.12 # adjust if yours is different
export GPU=H100 # options are {H100, B200}, else defaults to B200
cd demos/low-latency-llama
make
```

注意 README 这里只列了 `{H100, B200}` 两个选项，默认 `B200`。但实际 Makefile 还支持更多——见 4.4.3 和综合实践。

**运行命令**在 [README.md:L35](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/README.md#L35)（交互对话）和 [README.md:L44](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/README.md#L44)（基准测试）：

```bash
python megakernels/scripts/llama_repl.py                                              # 对话
python megakernels/scripts/generate.py mode=mk prompt="tell me a funny joke about cookies" ntok=100  # 跑 100 token
```

> `mode=mk` 表示用 megakernel 引擎跑（`mk` = megakernel）。`prompt=...` 和 `ntok=...` 是用 `pydra` 风格的命令行参数（见 4.3）。

#### 4.2.4 代码实践

**实践目标**：把 README 的安装步骤「翻译」成「每一步为什么」，确认你能独立完成新机器上的部署。

**操作步骤**：

1. 在一台有 NVIDIA GPU（H100 或 B200 佳）的 Linux 机器上克隆仓库。
2. 严格按 [README.md:L8-L12](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/README.md#L8-L12) 执行四条安装命令。
3. 设置 [README.md:L21-L24](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/README.md#L21-L24) 的四个环境变量（按你的实际 Python 版本和 GPU 修改）。
4. `cd demos/low-latency-llama && make`。

**需要观察的现象**：第 2 步会下载 ThunderKittens 仓库到 `ThunderKittens/`；第 4 步 `nvcc` 会输出 PTX/SASS 汇编信息（因为 Makefile 开了 `-Xptxas=--verbose`），最后生成一个 `mk_llama*.so` 文件。

**预期结果**：得到一个可被 Python import 的编译产物，随后 `python megakernels/scripts/llama_repl.py` 能进入对话。

> 如果没有 H100/B200，编译很可能失败或行为不符——这是 GPU 架构强绑定的项目。若无法在本地复现，请把本实践视为「待本地验证」，重点放在理解每一步。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `git submodule update --init --recursive` 必须在 `uv pip install -e .` 之前？
**答案**：因为编译 megakernel 时需要 ThunderKittens 的头文件（`kittens.cuh` 等）。子模块不拉下来，`include/` 里 `#include "kittens.cuh"` 就找不到，整个 C++ 侧无法编译。

**练习 2**：`uv pip install -e .` 里的 `-e` 是什么意思，对开发者有什么好处？
**答案**：`-e` = editable（可编辑）安装，相当于建一个指向源码的链接。改了 `megakernels/*.py` 立即生效，不用反复重装，方便调试。

---

### 4.3 pyproject.toml：包名、Python 版本与依赖清单

#### 4.3.1 概念说明

`pyproject.toml` 是现代 Python 项目的「身份证」。本项目的它回答三件事：

1. **包叫什么**：`name = "megakernels"`（这就是你能 `import megakernels` 的原因）。
2. **要什么环境**：`requires-python = ">=3.12"`，最低 Python 3.12。
3. **依赖哪些库**：一份依赖列表，决定了 `uv pip install -e .` 会自动装什么。

#### 4.3.2 核心流程

依赖安装的流程其实就是 setuptools 读取 `pyproject.toml` → 解析 `dependencies` → 逐个装到环境里：

```
uv pip install -e .
   │
   ▼
读取 [project].dependencies  →  transformers / einops / pybind11 / ...
   │
   ▼
[tool.setuptools] packages = {find={}}  →  自动发现包目录 megakernels/
   │
   ▼
注册一个名为 megakernels、可 import 的 Python 包
```

注意 `[tool.setuptools] packages = {find = {}}`：它让 setuptools **自动扫描**出 `megakernels/` 这个包目录（`pyproject.toml` 在根目录，旁边正好有个 `megakernels/` 文件夹）。

#### 4.3.3 源码精读

完整文件见 [pyproject.toml:L5-L26](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/pyproject.toml#L5-L26)。逐段看：

**包元信息**（[pyproject.toml:L5-L9](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/pyproject.toml#L5-L9)）：

```toml
[project]
name = "megakernels"
version = "0.0.1"
readme = "README.md"
requires-python = ">=3.12"
```

- `name = "megakernels"` → `import megakernels` 的来源。
- `requires-python = ">=3.12"` → 必须用 3.12+（这也是 README 里 `PYTHON_VERSION=3.12` 的由来，Makefile 默认甚至假设 3.13）。

**依赖清单**（[pyproject.toml:L10-L23](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/pyproject.toml#L10-L23)），下表给出每个依赖的用途：

| 依赖 | 用途（在本项目里） |
| --- | --- |
| `transformers==4.48.3` | 加载 Llama 模型权重/分词器（注意版本被**钉死**在 4.48.3）。 |
| `pydra-config>=0.0.13` | 命令行/配置解析，解释 `mode=mk prompt="..."` 这种写法。 |
| `accelerate` | 辅助模型加载与设备分配。 |
| `tabulate` | 把 benchmark 结果画成表格。 |
| `tqdm` | 进度条。 |
| `matplotlib` | 画延迟/性能图。 |
| `einops` | 张量 reshape 的可读写法，模型代码常用。 |
| `pyright` | Python 静态类型检查。 |
| `openai` | （可能用于对比基线或 API 调用。） |
| `psutil` | 读取系统/进程信息（如内存、CPU 占用）。 |
| `art` | ASCII 艺术字（很可能是 repl 启动时的横幅 banner）。 |
| `pybind11` | **把 C++/CUDA 编译出的 megakernel 暴露成 Python 可调用模块**——这是 Python 侧和 GPU 内核之间的桥。 |

其中 `pybind11` 是连接两个世界的关键：[demos/low-latency-llama/Makefile:L16-L17](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/Makefile#L16-L17) 的编译参数里就调用了 `python3 -m pybind11 --includes` 并链接 `-lpython${PYTHON_VERSION}`，把 C++ 内核编进一个 `.so` 让 Python import。

**包发现**（[pyproject.toml:L25-L26](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/pyproject.toml#L25-L26)）：

```toml
[tool.setuptools]
packages = {find = {}}
```

→ 自动找到 `megakernels/` 目录作为包。

#### 4.3.4 代码实践

**实践目标**：亲手列出 `megakernels` 包的全部依赖，并标注每个的用途。

**操作步骤**：

1. 打开 [pyproject.toml:L10-L23](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/pyproject.toml#L10-L23)。
2. 把 12 个依赖抄进一个表格（可参考 4.3.3 的用途表）。
3. （可选，待本地验证）装好后运行 `pip show megakernels` 或 `uv pip freeze | grep -i -E 'transformers|einops|pybind11'`，对比实际安装版本与 `pyproject.toml` 声明是否一致。

**需要观察的现象**：声明里 `transformers==4.48.3` 是精确版本，其余大多是宽松版本。

**预期结果**：你能复述 12 个依赖里至少 8 个的用途，特别是 `pybind11` 和 `transformers` 的角色。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `transformers` 被钉成 `==4.48.3`，而 `tabulate` 没有版本限制？
**答案**：模型加载/权重格式与 transformers 版本强相关，升级可能改变张量布局或接口，所以钉死以保证可复现；`tabulate` 只是画表格、接口稳定，放开版本限制更省心。

**练习 2**：如果删掉 `pybind11` 依赖，项目哪一部分会先坏掉？
**答案**：C++/CUDA megakernel 的编译会坏。`pybind11` 负责把编译出的内核包成 Python 扩展，没有它 Python 侧就无法 import 到 GPU 内核，整个「Python 调度 + GPU 执行」的桥梁就断了。

---

### 4.4 ThunderKittens 子模块：整个体系的基石

#### 4.4.1 概念说明

ThunderKittens（TK）是同属 HazyResearch 的另一个开源项目，专门提供**在 GPU 上写高性能 kernel 的抽象**（它把 warp、shared memory、TMA 等硬件细节封装成好用的原语，`kittens.cuh`）。

Megakernels 的 megakernel VM 是**建在 ThunderKittens 之上**的：`include/megakernel.cuh` 第一行就 `#include "kittens.cuh"`。换句话说：

> **没有 ThunderKittens，就没有 Megakernels 的 C++ 内核侧。**

所以 TK 必须作为 **git submodule** 嵌进来，并且编译时要用 `THUNDERKITTENS_ROOT` 告诉 nvcc 去哪找它的头文件。

#### 4.4.2 核心流程

子模块的生命周期：

```
.gitmodules 声明子模块
   │  path = ThunderKittens
   │  url  = https://github.com/HazyResearch/ThunderKittens.git
   │  branch = bvm-single-ctrl-pre-new-warps   ← 钉在一个特定开发分支
   ▼
git submodule update --init --recursive
   │
   ▼
ThunderKittens/ 目录被真正填充代码
   │
   ▼
编译时 -I${THUNDERKITTENS_ROOT}/include  ← nvcc 据此找到 kittens.cuh
```

#### 4.4.3 源码精读

**子模块声明**——整个 [`.gitmodules`](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/.gitmodules) 只有一个子模块（[.gitmodules:L1-L4](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/.gitmodules#L1-L4)）：

```ini
[submodule "ThunderKittens"]
    path = ThunderKittens
    url = https://github.com/HazyResearch/ThunderKittens.git
    branch = bvm-single-ctrl-pre-new-warps
```

注意 `branch = bvm-single-ctrl-pre-new-warps`：Megakernels 用的不是 TK 的主干，而是**一个特定的开发分支**（名字里的 `bvm` 很可能是 block-VM 相关的工作分支，`single-ctrl` 暗示单控制器设计）。这意味着 TK 在跟着 Megakernels 的需求一起演进，**不能随便换成 TK 主干版本**——否则 VM 可能编译不过或行为变化。

**编译时如何使用子模块**——[demos/low-latency-llama/Makefile:L16-L17](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/Makefile#L16-L17)：

```makefile
NVCCFLAGS += ... -I${THUNDERKITTENS_ROOT}/include -I${MEGAKERNELS_ROOT}/include \
             $(shell python3 -m pybind11 --includes) ... -lpython${PYTHON_VERSION}
```

这就是 `THUNDERKITTENS_ROOT` 和 `MEGAKERNELS_ROOT` 的真身：它们只是两个 `-I` 头文件搜索路径。`THUNDERKITTENS_ROOT` 指向子模块，`MEGAKERNELS_ROOT` 指向本仓库自己的 `include/`。

**GPU 与 Python 版本如何在 Makefile 里生效**——[demos/low-latency-llama/Makefile:L4-L6](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/Makefile#L4-L6) 设默认 `GPU=B200`；[Makefile:L12-L14](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/Makefile#L12-L14) 设默认 `PYTHON_VERSION=3.13`；[Makefile:L20-L28](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/Makefile#L20-L28) 根据 `GPU` 选架构：

```makefile
ifeq ($(GPU),4090)   NVCCFLAGS += -DKITTENS_4090 -arch=sm_89
else ifeq ($(GPU),A100)  NVCCFLAGS += -DKITTENS_A100 -arch=sm_80
else ifeq ($(GPU),H100)  NVCCFLAGS += -DKITTENS_HOPPER -arch=sm_90a
else                     NVCCFLAGS += -DKITTENS_HOPPER -DKITTENS_BLACKWELL -arch=sm_100a   # B200 等
endif
```

可见 `GPU` 实际支持 `{4090, A100, H100, B200}` 四档（README 只提了 H100/B200），每档对应不同的 `-arch=sm_*` 和 `-DKITTENS_*` 宏——因为 ThunderKittens 对不同 GPU 代际有不同的代码路径。

> 这也回答了本讲目标里的环境变量问题（综合实践会再串一遍）：
> - **`THUNDERKITTENS_ROOT`** → 提供 `-I.../include`，让 nvcc 找到 `kittens.cuh`。
> - **`MEGAKERNELS_ROOT`** → 提供 `-I.../include`，让 nvcc 找到本仓库的 VM 头文件。
> - **`GPU`** → 选目标架构（`-arch`）和 ThunderKittens 的代际宏（`KITTENS_*`）。
> - **`PYTHON_VERSION`** → 编译出的 `.so` 要链接 `libpython${PYTHON_VERSION}`，必须与你的解释器版本一致，否则 import 扩展时会崩。

#### 4.4.4 代码实践

**实践目标**：验证「子模块 + 环境变量」这套机制真的在编译链路里起作用。

**操作步骤**：

1. 看 [.gitmodules:L1-L4](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/.gitmodules#L1-L4)，记下子模块的 `path` 和 `branch`。
2. 在编译前（未 `git submodule update`）尝试 `cd demos/low-latency-llama && make`（**待本地验证**）。
3. 执行 `git submodule update --init --recursive` 后，确认 `ThunderKittens/include/` 下存在 `kittens.cuh`（可 `ls ThunderKittens/include/`，**待本地验证**）。
4. 对照 [Makefile:L16-L17](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/Makefile#L16-L17) 确认 `${THUNDERKITTENS_ROOT}/include` 正好指向步骤 3 的目录。

**需要观察的现象**：步骤 2 大概率报「找不到 `kittens.cuh`」之类的编译错误；步骤 3 之后该文件存在；步骤 4 的路径对得上。

**预期结果**：直观体会到「子模块没拉 = 编译失败」，以及 `THUNDERKITTENS_ROOT` 为何必不可少。

#### 4.4.5 小练习与答案

**练习 1**：为什么 Megakernels 要把 ThunderKittens 钉在 `bvm-single-ctrl-pre-new-warps` 分支，而不是用 TK 的主干 main？
**答案**：Megakernels 的 VM 设计（如「single controller」单控制器）依赖 TK 上尚未合入主干的特性/改动。用主干可能接口对不上、编译失败或行为变化；钉分支保证可复现、可控。

**练习 2**：如果有人把 `.gitmodules` 里的 `url` 改掉、但没重新 `git submodule sync`，会发生什么？
**答案**：本地 git 可能仍记得旧 url，`update` 时连错地方或连不上。正确做法是改完 url 后 `git submodule sync --recursive` 再 `update --init`。

---

## 5. 综合实践

把本讲三个最小模块串成一个完整任务：**在一台新机器上从零部署并理解 Megakernels**。

**任务**：

1. **克隆并初始化子模块**（覆盖 4.2 / 4.4）：
   ```bash
   git clone <repo>
   cd Megakernels
   git submodule update --init --recursive
   ```
   观察并记录：`ThunderKittens/` 目录从空变为有代码；记下它跟踪的分支（对照 [.gitmodules:L4](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/.gitmodules#L4)）。

2. **列出全部依赖**（覆盖 4.3）：打开 [pyproject.toml:L10-L23](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/pyproject.toml#L10-L23)，写一份「依赖 → 用途」表，至少标注 `transformers`、`pybind11`、`pydra-config`、`einops` 四个关键项。

3. **回答环境变量三连问**（覆盖 4.4.3）：用你自己的话解释为什么 `make` 之前必须 `export` 这三个变量——
   - `THUNDERKITTENS_ROOT` 是什么？编译链路里哪一行用到它？（答：给 nvcc 的 `-I` 头文件路径，指向 TK 子模块；见 [Makefile:L17](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/Makefile#L17)。）
   - `GPU` 改成不同值会改变什么？（答：`-arch=sm_*` 和 `-DKITTENS_*` 宏；见 [Makefile:L20-L28](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/Makefile#L20-L28)。）
   - `PYTHON_VERSION` 设错了会怎样？（答：`.so` 链接的 `libpython` 版本与解释器不匹配，import 扩展时崩溃；见 [Makefile:L17](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/Makefile#L17) 的 `-lpython${PYTHON_VERSION}`。）

4. **（可选，需 GPU）真跑一遍**：按 [README.md:L8-L27](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/README.md#L8-L27) 安装并编译，再 `python megakernels/scripts/generate.py mode=mk prompt="hi" ntok=5` 跑 5 个 token。若无法本地运行，明确标注「待本地验证」，把重心放在步骤 1–3 的理解。

**交付物**：一份 markdown 笔记，包含依赖表、环境变量三问的答案，以及你对「megakernel VM 为什么低延迟」的一句话总结。

---

## 6. 本讲小结

- **Megakernels 的定位**：把整个 LLM 推理塞进一个常驻 GPU 的 **megakernel 虚拟机**，靠「只 launch 一次 + 片上自驱 + 数据留片上」来追求极低延迟。
- **三个核心概念**：megakernel（一个干完所有活的大内核）、persistent kernel（启动后不退出、常驻 SM）、on-chip VM（用 warp 搭出的、把算子当指令执行的片上小机器）。
- **VM 结构有源码佐证**：[include/megakernel.cuh:L118-L140](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/megakernel.cuh#L118-L140) 按 warp 把 `consumer / loader / storer / launcher / controller` 派到不同功能单元。
- **安装两阶段**：先 `git submodule update --init --recursive` + `uv pip install -e .` 装 Python 包（[README.md:L8-L12](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/README.md#L8-L12)），再 `make` 编译 GPU 内核（[README.md:L21-L27](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/README.md#L21-L27)）。
- **包与依赖**：包名 `megakernels`，要求 Python ≥3.12；关键依赖 `transformers==4.48.3`（模型）、`pybind11`（C++↔Python 桥）、`pydra-config`（命令行）等（[pyproject.toml:L5-L26](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/pyproject.toml#L5-L26)）。
- **ThunderKittens 是基石**：作为子模块嵌入，钉在 `bvm-single-ctrl-pre-new-warps` 分支（[.gitmodules:L1-L4](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/.gitmodules#L1-L4)）；`THUNDERKITTENS_ROOT / GPU / PYTHON_VERSION` 三个环境变量分别控制头文件路径、目标架构、Python 链接版本（[Makefile:L16-L28](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/Makefile#L16-L28)）。

---

## 7. 下一步学习建议

本讲只建立了「VM 是什么、怎么装」的宏观印象。建议接下来：

1. **先读 Python 侧入口**：`megakernels/scripts/llama_repl.py` 和 `generate.py`，看一条 prompt 是怎么进入系统的。
2. **再读调度与指令**：`megakernels/scheduler.py`（VM 的「指令」怎么排）、`megakernels/instructions.py`（每条「指令」长什么样）、`megakernels/llama.py`（Llama 的每一层如何映射成算子）。
3. **最后进入 C++ 内核**：从 `include/launcher.cuh`、`include/controller/controller.cuh` 开始，对照本讲的 [megakernel.cuh:L118-L140](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/megakernel.cuh#L118-L140)，逐个解剖 controller / launcher / loader / storer / consumer 五大功能单元。
4. **延伸阅读 ThunderKittens**：理解 `kittens.cuh` 提供的原语，能帮你更快看懂 megakernel 的 C++ 实现。

> 配套地，可以把 `demos/low-latency-llama/` 下的 `llama.cu`、`attention_*.cu`、`matvec_*.cu` 当作「算子即指令」的具体例子来读——它们正是被 VM consumer 执行的「操作码」。
