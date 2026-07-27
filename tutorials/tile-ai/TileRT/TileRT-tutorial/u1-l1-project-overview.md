# 项目定位与核心理念：tile 级运行时如何实现超低延迟

## 1. 本讲目标

本讲是整本《TileRT 学习手册》的第一篇，目标是让你在**不写一行代码、不碰 GPU** 的前提下，真正读懂 TileRT 这个项目「为什么存在」「想解决什么问题」「用什么思路解决」。

学完本讲，你应当能够：

- 用一句话向同事说清 TileRT 与 vLLM 这类「高吞吐推理系统」在优化目标上的本质区别。
- 解释「tile 级运行时（tile-level runtime engine）」的核心思想：算子分解 + 计算/IO/通信的重叠调度。
- 说出 TileRT 当前支持的模型（DeepSeek-V3.2、GLM-5/5.1）与目标硬件（8× NVIDIA B200），并知道为什么这两个模型是「各自独立的后端库」。

本讲只解决**认知**问题——建立宏观图景。具体的安装、运行、源码细节留给后续讲义（u1-l2 目录结构、u1-l3 安装与后端加载）。

---

## 2. 前置知识

本讲面向零基础读者，但有几个名词先讲清楚会更顺：

- **LLM 推理（inference）**：把一个训练好的大语言模型加载到显存里，给它一段输入文字（prompt），让它一个字一个字地「吐」出回答。
- **token**：模型处理的最小单位，约等于「一个词或半个汉字」。下文所有「生成 token」都指模型输出一个这样的单位。
- **TPOT（Time Per Output Token）**：生成**单个**输出 token 所花的时间，单位通常是毫秒（ms）。它是衡量「用户体感快慢」的核心指标——TPOT 越小，文字流得越快。
- **吞吐（throughput，tokens/s）**：单位时间总共产出多少 token。它衡量「一批请求总体能处理多少」，和 TPOT 不是一回事。
- **prefill / decode**：推理分两阶段——prefill 是「先读完整段 prompt」（一次性算，比较重），decode 是「之后逐个生成 token」（一次一个，对延迟敏感）。TileRT 重点优化的是 decode。
- **tile（分块）**：GPU 上的计算会被切成很多小块（tile）来并行执行。你可以暂时把它理解为「一块可以独立调度的小计算单元」。

> 一句话直觉：传统系统想「一次多做几个」来提高总产量；TileRT 想「让每一个都更快」来提高响应速度。两者的优化方向不同。

---

## 3. 本讲源码地图

本讲只引用三份「项目门面」文件，它们足以回答「TileRT 是什么」：

| 文件 | 作用 | 本讲怎么用 |
| --- | --- | --- |
| `README.md` | 项目对外说明书：定位、安装、用法、性能数字。 | 提取项目定位、tile 运行时定义、支持模型与硬件 |
| `pyproject.toml` | Python 包定义：包名、依赖版本锁、支持的 Python 版本。 | 看清「精确 ABI」环境约束与依赖技术栈 |
| `tilert/__init__.py` | Python 包入口：声明双后端库、提供 `load_backend`。 | 理解「一个模型族一个 .so 后端」的设计 |

后续讲义会进入 `tilert/generate.py`、`tilert/models/...` 等具体实现，本讲先把这三份「门面」读懂。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块，对应三个小节。

### 4.1 项目定位与设计目标

#### 4.1.1 概念说明

绝大多数开源推理框架（vLLM、TGI、SGLang 等）的优化主线是**吞吐**：通过连续批处理（continuous batching）、尽可能大的 batch，把一张卡、一个节点的「总产出」推到最高。这套思路非常适合离线批量任务——比如「一夜跑完 100 万条数据」。

但有一类场景，用户关心的不是「总量」，而是**单个请求的延迟**：

- **高频交易**：模型要在几毫秒内给出交易信号，晚一毫秒就亏钱。
- **交互式 AI / 实时对话**：用户盯着屏幕等字一个一个蹦出来，首字延迟和「流的速度」直接决定体验。
- **实时决策、长程 Agent（long-running agents）**：Agent 一步接一步调用模型，每一步的延迟会累乘放大。
- **AI 辅助编程**：代码补全要求「按下一个键几乎立刻出建议」。

TileRT 正是为这类场景而生。它的官方定位原话是：

> **TileRT** is a project designed to serve large language models (LLMs) in **ultra-low-latency** scenarios. Its goal is to push the latency limits of LLMs without compromising model size or quality—enabling models with hundreds of billions of parameters to achieve **millisecond-level time per output token (TPOT)**.

关键词是 ultra-low-latency（超低延迟）和 TPOT（单 token 延迟）。它**不靠缩小模型、不靠降低质量**来换速度，而是要让千亿参数的模型也能做到「毫秒级吐一个 token」。

#### 4.1.2 核心流程

理解定位的关键，是分清「吞吐」与「延迟」这两个看似相关、实则方向相反的优化目标。一个简化的关系是：

\[
\text{吞吐}(\text{tokens/s}) \;=\; \frac{\text{batch\_size}}{\text{TPOT}}
\]

- 传统高吞吐系统的做法：**增大 batch_size**。把更多请求攒在一起算，分摊掉权重读取的开销，总吞吐上去了。但 batch 越大，每一步要处理的数据越多，单个 token 的延迟（TPOT）通常也会变大。
- TileRT 的做法：**在很小的 batch（甚至 bs=1）下，把 TPOT 压到极致**。它不追求「同时服务 1000 个用户」，而是追求「服务你这一个请求时，每一个字都快」。

用一张表对比就是：

| 维度 | 高吞吐系统（如 vLLM） | TileRT |
| --- | --- | --- |
| 主优化目标 | 总吞吐 tokens/s | 单 token 延迟 TPOT |
| 偏好的 batch | 尽量大 | 小（甚至 1） |
| 典型场景 | 离线批量、高并发在线服务 | 实时交互、低延迟决策 |
| 衡量指标 | tokens/s 越高越好 | TPOT 越低越好 |

> 注意：这不是说 TileRT「吞吐低」。README 记录的成就是在「单 token 延迟极低」的前提下，仍能做到很高吞吐（见 4.3）。只是它的**优化起点和优先级**不同——先把延迟做低，再谈吞吐。

#### 4.1.3 源码精读

项目定位写在 README 的 Overview 段落，三句话层层递进：

[README.md:50](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/README.md#L50)：这句点明项目存在的理由——ultra-low-latency，并以 TPOT 为衡量标尺。

[README.md:52](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/README.md#L52)：这句划清与「传统高吞吐批处理系统」的边界，并列出五类典型应用（高频交易、交互式 AI、实时决策、长程 Agent、AI 辅助编程）。

[README.md:56](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/README.md#L56)：这句是重要声明——底层编译器技术会逐步通过 **TileLang** 和 **TileScale** 开源给社区。这告诉你 TileRT 背后是一个更大的「tile 编译」技术栈，本仓库是它在 LLM 推理上的落地。

#### 4.1.4 代码实践

> 本实践为**源码阅读型实践**（无需 GPU，本机即可完成）。

1. **实践目标**：用自己的话把 TileRT 的定位讲清楚，并真正区分「延迟优先」与「吞吐优先」。
2. **操作步骤**：
   - 打开 [README.md 的 Overview 段落](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/README.md#L50)，逐句读完第 50、52、54 行。
   - 用中文写一段约 **200 字** 的说明，回答这个问题：「为什么把算子分解成 tile 级任务、再动态重排，能降低单个 token 的延迟？」（提示：从「减少硬件空闲、让计算和通信重叠」入手。）
   - 再列出 TileRT 与 vLLM 这类高吞吐系统在**优化目标**上的**三点不同**（可参考 4.1.2 的对比表，但用自己的话重写，不要照抄）。
3. **需要观察的现象**：写完后再回头看 README，检查自己写的内容是否和官方表述一致；若有出入，修正。
4. **预期结果**：你能产出一段不依赖术语堆砌、能让外行听懂的说明，并能复述「延迟 vs 吞吐」的取舍。
5. 本步骤无需运行任何命令，属于纯阅读理解。

#### 4.1.5 小练习与答案

**练习 1**：某推理系统把 batch_size 从 1 提到 32，总吞吐提高了约 20 倍，但 TPOT 也变大了。它更像是「高吞吐系统」还是「TileRT 式的低延迟系统」？为什么？

> **参考答案**：更像是高吞吐系统。它通过增大 batch 来换总吞吐，代价是单 token 延迟上升，这与 TileRT「优先压低 TPOT、宁可 batch 小」的方向相反。

**练习 2**：README 说 TileRT「不靠缩小模型、不靠降低质量来换速度」。如果有一个项目号称「同样低延迟」，但它是把模型蒸馏到 1/10 大小做到的，它和 TileRT 的路线有何不同？

> **参考答案**：那个项目是「牺牲模型容量/质量」换延迟（小模型天然更快但能力弱）；TileRT 的路线是「保持模型大小与质量不变，靠系统/编译器层面的调度优化」来压低延迟。两者起点完全不同。

---

### 4.2 tile 级运行时引擎概念

#### 4.2.1 概念说明

定位讲清了，下一个问题是：**凭什么能做这么低延迟？** TileRT 的答案是四个字——**tile 运行时**。

先理解一个背景：GPU 上的一个大算子（比如一次大矩阵乘、一次注意力计算）在硬件上并不是「一口气算完」的，而是被切成很多个叫 **tile** 的小块，由成百上千个线程并行处理。传统框架（如 PyTorch）把每个算子当成一个「黑盒」交给 GPU 跑完再跑下一个——算子和算子之间、计算和通信之间，常常是「你等我、我等你」的串行关系，中间会有大量**硬件空闲**。

TileRT 的核心思路是：

> 与其让算子作为黑盒一个个排队，不如把算子**分解**成细粒度的 tile 级任务，再由一个**运行时（runtime）**统一**动态重排**这些任务，让「计算」「显存读写（IO）」「跨卡通信」尽可能**重叠（overlap）**在一起跑。

打个比方：传统做法像「先洗完所有菜、再切完所有菜、最后一起炒」——每一步之间厨房有空闲；tile 运行时像「一个厨师一边洗、一边切、另一个锅已经在炒」，多个环节并行，厨房不闲着，整体就快了。

README 的原话是：

> To achieve this, TileRT introduces a **tile-level runtime engine**. Leveraging a **compiler-driven approach**, LLM operators are **decomposed** into fine-grained tile-level tasks, while the runtime **dynamically reschedules** computation, I/O, and communication across multiple devices in a **highly overlapped** manner.

这里有三个关键词要记住：

- **compiler-driven（编译器驱动）**：这种分解和调度不是手写出来的，而是由编译器自动生成。README 也说底层技术会通过 TileLang / TileScale 开源。
- **decomposed（分解）**：大算子 → 细粒度 tile 任务。
- **dynamically reschedules … overlapped（动态重排 + 高度重叠）**：把三类工作（计算、IO、通信）在多卡上交错排布，**最小化空闲、提高硬件利用率**。

#### 4.2.2 核心流程

可以把 tile 运行时的工作流抽象成下面这条流水线（伪代码示意，**非项目真实代码**，仅帮助理解概念）：

```text
# 伪代码：仅用于理解概念，不是项目里的真实代码
# 传统方式
for op in layer_operators:           # 算子作为黑盒，逐个排队
    wait_for_previous(op)
    run(op)                          # 算 op 时，通信链路和 IO 多半在闲着

# tile 运行时方式
tile_tasks = decompose(layer_operators)   # 编译器把算子分解成 tile 任务
schedule = runtime.reschedule(            # 运行时动态重排
    tasks=tile_tasks,
    resources=gpus,                        # 多卡资源
    overlap=["compute", "io", "comm"],     # 让三类工作重叠
)
runtime.run(schedule)                     # 高度重叠地执行，硬件几乎不空闲
```

数学上，整体执行时间可以粗略写成三类工作「被重叠后」的耗时：

\[
T_{\text{step}} \;\approx\; \max\!\big(\,T_{\text{compute}},\; T_{\text{io}},\; T_{\text{comm}}\,\big) \;+\; T_{\text{依赖串行}}
\]

传统方式三者串行相加 \(T_{\text{compute}}+T_{\text{io}}+T_{\text{comm}}\)；tile 运行时把它们压成「取最大值」，于是每一步（每个 token）的时间被显著拉低——这就是「tile 级重排能降低单 token 延迟」的直觉解释。

> 说明：上面是用于理解的简化模型，真实运行时还要处理数据依赖、显存对齐、跨卡同步等大量细节，这些会在进阶层（u2/u3）讲义里结合源码展开。

#### 4.2.3 源码精读

tile 运行时的概念定义集中在 README 的一句话里：

[README.md:54](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/README.md#L54)：这句是整个项目的技术纲领——decomposed（分解）+ dynamically reschedules（动态重排）+ overlapped（重叠）三个动作都在这一句里。

而 `tilert/__init__.py` 的包级文档串，则从「工程实现」角度透露了 tile 运行时的落地形态——它被编译成两个**后端共享库**（`.so`），通过 `torch.ops` 自定义算子的方式注入 PyTorch：

[tilert/__init__.py:1-12](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/__init__.py#L1-L12)：这段包级文档串说明「两个后端库不在 import 时加载，而是由 `load_backend(model_type)` 按需懒加载」，且「两个库都注册同一个 `tilert` torch-op 命名空间，所以一个进程只能加载一个」。

这说明了一个重要事实：**tile 运行时的「大脑」（编译器、调度器）并不在本 Python 仓库里**，本仓库是它的「Python 驱动层 + 模型组装层」，真正的运行时被编译进了那两个 `.so` 后端库。这一点会在 4.3 和后续 u1-l3（后端加载）讲义里反复印证。

#### 4.2.4 代码实践

> 本实践为**源码阅读型实践**（无需 GPU）。

1. **实践目标**：把「计算/IO/通信重叠」这个抽象概念，落到一个你能画出来的图上。
2. **操作步骤**：
   - 画两条时间轴（用纸笔或任意画图工具即可）：
     - **时间轴 A（传统）**：把 `计算 → 通信 → IO` 画成**首尾相接**的三段，标注总长度 = 三段之和。
     - **时间轴 B（tile 运行时）**：把三段**上下错开、大部分重叠**，只露出各自不可避让的一小段，标注总长度 ≈ 三段的最大值。
   - 在时间轴 B 上，用箭头标出「计算进行的同时，通信和 IO 也在进行」的区间。
3. **需要观察的现象**：对比两条轴的「空闲段（什么都不干的间隙）」。轴 A 应该有不少空白，轴 B 几乎填满。
4. **预期结果**：你能直观看到「重叠执行 = 减少空闲 = 降低每步时间」，并能用自己的话解释这就是 README 第 54 行说的 overlapped。
5. 本步骤不涉及运行命令。

#### 4.2.5 小练习与答案

**练习 1**：在公式 \(T_{\text{step}} \approx \max(T_{\text{compute}}, T_{\text{io}}, T_{\text{comm}}) + T_{\text{依赖串行}}\) 中，要让 tile 运行时进一步提速，应该优先优化哪一项？为什么？

> **参考答案**：应优先优化「三者中的最大值」与「依赖串行」部分。因为重叠后总时间由最大值主导（木桶效应），把最慢的那一类压下去收益最大；而依赖串行是重叠不掉的「硬开销」，减少算子间不必要的依赖同样关键。

**练习 2**：为什么 tile 运行时需要「编译器驱动」？如果纯靠人工手写调度，会遇到什么问题？

> **参考答案**：tile 级任务极其细碎、且要随模型结构和硬件拓扑变化，人工手写既容易出错也难以覆盖各种情况。编译器能根据模型自动分解算子、自动排布重叠，做到「模型一变、调度自动重生成」，可维护、可移植。

---

### 4.3 支持的模型与硬件

#### 4.3.1 概念说明

知道「为什么快」之后，还要知道「TileRT 能跑在什么上、支持哪些模型」。这决定了你能不能用它。

**硬件**：TileRT v0.1.5 的官方 wheel 是**针对 8× NVIDIA B200** 编译的。这不是「最低配置」，而是**精确的 ABI 绑定**——其他 GPU、其他 CUDA/PyTorch 组合**不被保证可用**。README 用了很重的措辞强调这一点。

**模型**：当前支持两个模型族：

- **DeepSeek-V3.2**（仓库里常写作 `deepseek_v3_2` / `dsv32`）
- **GLM-5 / GLM-5.1**（仓库里写作 `glm5`）

它们各自的运行时被编译成**独立的两个后端库**：

- `libtilert_dsv32.so` —— DeepSeek-V3.2
- `libtilert_glm5.so` —— GLM-5

关键约束：**一个 Python 进程只能加载其中一个**。原因是这两个库都往 PyTorch 注册同一个算子命名空间（`torch.ops.tilert.*`），同时加载会冲突。所以如果你想分别跑 DeepSeek-V3.2 和 GLM-5，要开**两个独立进程**。

#### 4.3.2 核心流程

模型与硬件的「绑定关系」可以这样理解：

```text
        TileRT wheel（pip install tilert==0.1.5.post1）
            │
            │  精确 ABI 绑定（不可随意换版本）
            ▼
   8× NVIDIA B200  +  CUDA 13.2 + torch 2.11.0+cu130 + Python 3.12
            │
            │  内含两个后端 .so（一个进程只能选一个）
            ├──▶ libtilert_dsv32.so  ──▶  DeepSeek-V3.2
            └──▶ libtilert_glm5.so   ──▶  GLM-5 / GLM-5.1
```

从「用户视角」看，选模型 = 选后端库 = 调一次 `load_backend(...)`：

```text
load_backend("deepseek_v3_2")   →  加载 libtilert_dsv32.so  →  之后才能构造 DeepSeek 生成器
load_backend("glm5")            →  加载 libtilert_glm5.so   →  之后才能构造 GLM-5 生成器
# 同一进程里第二次调用另一个，会抛 RuntimeError
```

#### 4.3.3 源码精读

**性能实证**（证明「低延迟」不是空话）：README 的 News 区记录了两组关键数字。

[README.md:25](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/README.md#L25)：与小米 MiMo 合作，在**1 万亿参数**模型上做到 **>1000 tokens/s**，且是在**单个 8-GPU 节点**上完成——「没有定制芯片」。

[README.md:36](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/README.md#L36)：v0.1.3 起，GLM-5-FP8 达到约 **500 tokens/s**、DeepSeek-V3.2 达到约 **600 tokens/s**。

**硬件/环境绑定**：精确版本表写在安装段落。

[README.md:40](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/README.md#L40)（v0.1.1 条目里点明 **8× NVIDIA B200**）；完整的版本锁定表在安装章节（GPU=8× B200、driver 支持 CUDA 13.2、torch=2.11.0+cu130、Python=3.12 等），建议对照阅读。

**依赖技术栈**：`pyproject.toml` 把核心依赖钉死在精确版本。

[pyproject.toml:17-28](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/pyproject.toml#L17-L28)：`torch==2.11.0`、`transformers==4.46.3`、`tokenizers==0.20.3`，并带注释说明 torch 必须来自 cu130 索引——这正是「精确 ABI」的工程体现。

**双后端库**：`tilert/__init__.py` 用一个字典显式列出了两个后端。

[tilert/__init__.py:43-46](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/__init__.py#L43-L46)：`_BACKENDS = {"deepseek_v3_2": "libtilert_dsv32.so", "glm5": "libtilert_glm5.so"}`——一行代码说清「两个模型 = 两个 .so」。

[tilert/__init__.py:51-81](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/__init__.py#L51-L81)：`load_backend` 的完整实现。重点看它如何做**单后端互斥校验**——如果 `_loaded_backend` 已经是另一个库，就抛 `RuntimeError`，提示「在新进程里运行」。

#### 4.3.4 代码实践

> 本实践为**源码阅读型实践**（无需 GPU）。

1. **实践目标**：搞懂「选模型 = 选后端库」这条链路，并验证「同进程不能同时加载两个后端」是真实存在的约束。
2. **操作步骤**：
   - 打开 [tilert/__init__.py:43-81](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/__init__.py#L43-L81)，找到 `_BACKENDS` 字典和 `load_backend` 函数。
   - 阅读函数体，回答：当传入 `model_type="deepseek_v3_2"` 时，函数会去加载哪个 `.so`？如果之后**同一个进程**再调用 `load_backend("glm5")`，会走到哪一行、抛出什么异常？把对应的文件名和行号记下来。
   - 再对照 [pyproject.toml:17-28](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/pyproject.toml#L17-L28)，列出 TileRT 运行所需的三个核心 Python 依赖及其**精确**版本号。
3. **需要观察的现象**：你会注意到互斥校验发生在函数靠前位置（已经加载过别的后端就直接报错），而真正「加载库」的动作（`ctypes.CDLL` + `torch.ops.load_library`）只在第一次发生——这是「懒加载」。
4. **预期结果**：你能口述出 `load_backend` 的三段逻辑——①查字典得到 `.so` 名；②若已加载别的后端则报错；③首次加载时用 ctypes + torch.ops.load_library 注入。
5. 若想真正运行验证（需要 8× B200 环境）：在官方容器里依次执行 `load_backend("deepseek_v3_2")` 与 `load_backend("glm5")`，**预期第二次抛 `RuntimeError`**。无此环境则标注「待本地验证」即可，不要假装已运行。

#### 4.3.5 小练习与答案

**练习 1**：为什么 DeepSeek-V3.2 和 GLM-5 要做成两个独立的 `.so`，而不是合在一个库里加个开关？

> **参考答案**：两个模型的结构差异较大（注意力维度、MoE 路由方式等都不同），各自编译能针对该模型做最深度的 tile 优化、并控制库体积；同时它们复用同一个 `torch.ops.tilert` 命名空间，合并会冲突。独立 `.so` + 一进程一个，是「极致优化」与「避免命名冲突」之间的合理取舍。

**练习 2**：如果你的机器是 4× RTX 4090，能直接用官方 `tilert==0.1.5.post1` wheel 吗？

> **参考答案**：不能保证可用。官方 wheel 是针对 8× B200 + CUDA 13.2 + torch 2.11.0+cu130 精确编译的，README 明确说其他组合「untested and not guaranteed」。4090 在架构、驱动、CUDA 版本上都不匹配，大概率无法正常加载后端。

---

## 5. 综合实践

把本讲三个模块串起来，完成一份**「TileRT 一页速览（one-pager）」**。要求用你自己的话、不照抄 README，包含以下四块：

1. **一句话定位**：TileRT 是什么、为谁服务（参考 4.1）。
2. **核心机制图**：画一张「传统串行 vs tile 运行时重叠」的对比时间轴（参考 4.2.4 的实践），并在图下用一两句话点出「分解 + 重排 + 重叠」三要素。
3. **能力清单**：用表格列出支持的模型（DeepSeek-V3.2 / GLM-5）、目标硬件（8× B200）、Python/CUDA/torch 精确版本（参考 4.3.3 的源码链接），并写明「一进程一后端」的约束。
4. **一个反思**：设想你要为一个「AI 代码补全」产品选推理引擎，用户最在意「按键后立刻出建议」。你会选 TileRT 还是高吞吐系统？用本讲学到的「TPOT vs 吞吐」给出至少两点理由。

**完成标志**：把这页速览拿给一个没读过 TileRT 的同事看，Ta 能在 3 分钟内说清「TileRT 是干嘛的、凭什么快、跑在什么上」。这等于你已经把本讲的核心装进了脑子里。

> 进阶自检（可选）：速览里提到的「`.so` 后端、`load_backend`、精确 ABI」这些词，你能否不看讲义、用源码行号支撑地解释一遍？能做到，就可以放心进入下一篇 u1-l2《项目目录结构与双后端架构地图》。

---

## 6. 本讲小结

- TileRT 的定位是**超低延迟（ultra-low-latency）LLM 推理**，优化标尺是 **TPOT（单 token 延迟）**，而非传统系统的总吞吐。
- 它面向**高频交易、交互式 AI、实时决策、长程 Agent、AI 辅助编程**等「单请求延迟敏感」的场景。
- 核心技术是 **tile 级运行时**：用编译器把算子**分解**成细粒度 tile 任务，再由运行时**动态重排**，让计算/IO/通信**高度重叠**，从而压低每一步时间。
- 当前支持 **DeepSeek-V3.2** 与 **GLM-5/5.1** 两个模型，分别对应两个独立后端库 `libtilert_dsv32.so` / `libtilert_glm5.so`，**一个进程只能加载一个**。
- 硬件与依赖是**精确 ABI 绑定**：8× NVIDIA B200、CUDA 13.2、torch 2.11.0+cu130、Python 3.12，不能随意替换。
- 实证：在 1T 参数模型上做到 >1000 tokens/s（单 8-GPU 节点），证明「低延迟」与「高吞吐」可以兼得——但起点是先把延迟做低。

---

## 7. 下一步学习建议

本讲建立了宏观图景，但还没碰过任何代码细节。建议按这个顺序继续：

1. **下一篇 u1-l2《项目目录结构与双后端架构地图》**：通览 `tilert/` 包的目录划分，看清 `deepseek_v3_2` 与 `glm_5` 两套镜像模型结构，建立「看目录名就能定位功能」的能力。
2. **u1-l3《环境安装与后端动态加载机制》**：亲手跑通官方 Docker/wheel 安装，并精读 `load_backend` 如何用 `ctypes` + `torch.ops.load_library` 把 `.so` 注入进程——把本讲 4.3 的概念落到可运行的命令上。
3. **延伸阅读（非本仓库）**：README 第 56 行提到的 **TileLang** 与 **TileScale**，是 tile 编译技术的上游，有兴趣可去了解 tile 运行时「大脑」的来龙去脉。

> 学习节奏建议：本讲这类「定位与理念」的内容，重在**用自己的话复述**，不必死记术语。等进入 u1-l3 真正敲下第一条 `load_backend` 命令时，再回头看本讲，会有「原来如此」的顿悟。
