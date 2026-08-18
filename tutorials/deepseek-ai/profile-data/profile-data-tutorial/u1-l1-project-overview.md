# 项目概览：profile-data 是什么

## 1. 本讲目标

学完本讲，你应该能够：

1. 一句话说清 `deepseek-ai/profile-data` 仓库的定位：它不是传统代码项目，而是 DeepSeek 公开的训练/推理框架性能分析（profiling）数据集。
2. 列举三份轨迹各自的阶段与并行配置：`train.json`（训练，EP64）、`prefill.json`（预填充，EP32，每 GPU 16K tokens）、`decode.json`（解码，EP128，每 GPU 128 个请求）。
3. 说清 README 中三张截图与三份 JSON 的一一对应关系。
4. 理解「采集时模拟绝对均衡的 MoE 路由」这一重要前提，以及它对解读数据的影响。

本讲是整本手册的第一讲，不要求你已经读过任何轨迹文件内部结构——那是后续讲义的任务。本讲只解决一个问题：**这个仓库里有什么、为什么值得学**。

## 2. 前置知识

本讲需要的背景概念都不深，用通俗语言逐个过一遍。

### 2.1 什么是 profiling（性能分析）

profiling 指的是：在程序运行时记录「谁、在什么时刻、花了多长时间」的过程。对深度学习框架来说，就是记录每一个 CPU 算子调用、每一次 CUDA API 调用、每一个 GPU 内核的起止时间。**PyTorch Profiler** 是 PyTorch 官方自带的这类工具，它可以把结果导出为 Chrome Trace 格式的 JSON 文件——这种格式原本为 Chrome 浏览器的性能追踪设计，因此可以直接在浏览器里可视化。本仓库的三份 JSON 正是 PyTorch Profiler 导出的这种文件。

### 2.2 大模型服务的两个阶段：预填充与解码

现代大模型推理分为两个阶段，本仓库各给了一份轨迹：

| 阶段 | 英文 | 输入 | 输出 | 特点 |
|---|---|---|---|---|
| 预填充 | prefilling | 一整段 prompt（如 4K token） | 每个 token 位置的 KV 缓存 | 一次处理大量 token，是**计算密集型** |
| 解码 | decoding | 上一步生成的 1 个 token | 下一个 token | 每步只算 1 个 token，访存/通信占比高，对**延迟敏感** |

训练（training）则是前向 + 反向都在跑，本仓库的训练轨迹展示了 DeepSeek DualPipe 方案中一对前向/反向分块的重叠执行。

### 2.3 并行记号速览：EP / TP / PP / world_size

读 README 会遇到这些缩写，先建一张速查表：

| 记号 | 全称 | 一句话解释 |
|---|---|---|
| EP | Expert Parallelism（专家并行） | 把 MoE 的不同专家分布到不同 GPU 上，token 需要「寄」到专家所在的 GPU |
| TP | Tensor Parallelism（张量并行） | 把单个大矩阵乘法切开分到多卡，本仓库三份轨迹均为 TP1（即不用 TP） |
| PP | Pipeline Parallelism（流水线并行） | 把模型按层切成多段，每段放不同 GPU，段间传激活值 |
| world_size | — | 分布式训练/推理的总进程（GPU）数，如 EP64 表示 64 个进程各自持有部分专家 |

### 2.4 MoE 与路由、all-to-all 通信

MoE（Mixture of Experts，混合专家）层的每个 token 会被**路由**（routing）到少数几个专家（如 DeepSeek-V3 的 top-2）去计算。当专家分布在多卡上（EP）时，token 要先发往专家所在卡，算完再收回来——这两次方向相反的全员互发，就是 **all-to-all 通信**，在 DeepEP 库里分别叫 `dispatch`（发出去）和 `combine`（收回来）。MoE 模型的性能瓶颈往往就在这里，所以「**通信与计算如何重叠**」正是本仓库想展示的核心主题。

### 2.5 两个硬件词：RDMA 与 SM

- **RDMA**：远程直接内存访问，一种「网卡直接搬运内存数据、几乎不劳烦 CPU」的高速网络技术，MoE 的 all-to-all 通常走 RDMA。
- **SM**：Streaming Multiprocessor，GPU 内真正执行计算的流式多处理器，可以理解为 GPU 的「CPU 核」。解码阶段的一个亮点是：通信内核发出 RDMA 消息后即释放全部 SM，让计算独占 GPU。

## 3. 本讲源码地图

本仓库非常小，全部家当如下（文件大小为本讲义编写时实际测得）：

| 文件 | 作用 | 大小 |
|---|---|---|
| `README.md` | 唯一的"文档源码"，说明仓库定位与三份轨迹的采集配置 | 2.3 KB |
| `train.json` | 训练阶段轨迹（EP64，DualPipe 一对前/反向 chunk） | 约 3.0 MB |
| `prefill.json` | 预填充阶段轨迹（EP32，双微批） | 约 16.6 MB |
| `decode.json` | 解码阶段轨迹（EP128，低延迟 all-to-all） | 约 4.4 MB |
| `assets/train.jpg` | 训练轨迹的可视化截图，对应 `train.json` | 约 490 KB |
| `assets/prefill.jpg` | 预填充轨迹的可视化截图，对应 `prefill.json` | 约 400 KB |
| `assets/decode.jpg` | 解码轨迹的可视化截图，对应 `decode.json` | 约 480 KB |

记忆要点：**每个场景 = 1 份 JSON 数据 + 1 张截图**，README 是它们的索引。

## 4. 核心概念与源码讲解

### 4.1 项目背景与定位

#### 4.1.1 概念说明

大多数开源仓库放的是「代码」，这个仓库放的是「**运行数据**」。DeepSeek 把自家训练与推理框架在真实集群上跑出来的 profiler 轨迹直接公开，目的写在 README 第一段：帮助社区理解他们的**通信-计算重叠策略**（communication-computation overlap）和**底层实现细节**。

为什么这很有价值？因为论文里只能用文字和示意图描述「通信与计算重叠」，而轨迹文件里是**每一次内核调用的真实时间戳**。你可以亲自量化：通信到底占了多少时间？重叠率是多少？气泡在哪里？这是文字资料给不了的。

同时要认识到它的边界：这是**数据集**不是工具库——没有可安装的包、没有入口函数，学习方式是「读数据 + 写脚本分析数据」。

#### 4.1.2 核心流程

这份数据是如何产生、又如何被使用的？整个链路如下：

```text
DeepSeek 训练/推理框架（真实集群，多 GPU）
        │  运行时挂载
        ▼
PyTorch Profiler 采集 CPU 算子 / CUDA API / GPU 内核事件
        │  导出
        ▼
Chrome Trace 格式 JSON（本仓库的 train/prefill/decode.json）
        │  下载后用浏览器打开
        ▼
chrome://tracing（或 edge://tracing、Perfetto）可视化浏览
        │  进一步
        ▼
用 Python 脚本做定量分析（本手册第三单元）
```

注意其中一步特殊处理：采集时**模拟了绝对均衡的 MoE 路由**（见 4.1.3），即人为让每个专家收到的 token 数完全相同。

#### 4.1.3 源码精读

仓库定位的全部信息浓缩在 README 开头三行：

> [README.md:L1-L3](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/README.md#L1-L3)

```text
# Profiling Data in DeepSeek Infra

Here, we publicly share profiling data from our training and inference framework
to help the community better understand the communication-computation overlap
strategies and low-level implementation details. The profiling data was captured
using the PyTorch Profiler. ... Notice that we simulate an absolutely balanced
MoE routing strategy for profiling.
```

逐句拆解这段话的信息量：

1. **"training and inference framework"**——数据同时覆盖训练与推理（预填充、解码）两类、共三个场景。
2. **"communication-computation overlap strategies"**——明确主题是通信-计算重叠，这是贯穿整本手册的主线。
3. **"captured using the PyTorch Profiler"**——工具是 PyTorch 官方 profiler，所以导出格式是标准 Chrome Trace JSON，任何人都能用通用的脚本和可视化工具处理它，不需要 DeepSeek 私有工具。
4. **"simulate an absolutely balanced MoE routing strategy"**——最重要的前提：真实 MoE 路由是不均衡的（不同专家热门程度不同），采集时被替换成**人为绝对均衡**的路由。解读数据时要记住：这份数据展示的是「理想负载下的重叠策略」，不能直接拿来推算真实业务的专家负载倾斜。

#### 4.1.4 代码实践

**实践一：盘点仓库文件（阅读型实践）**

1. **实践目标**：亲手确认仓库里只有「1 份 README + 3 份 JSON + 3 张截图」，建立"这是数据集"的直观感受。
2. **操作步骤**：
   - 克隆或下载仓库后，在仓库根目录执行 `ls -la`（或用文件管理器查看）。
   - 进入 `assets/` 目录再执行一次 `ls -la`。
   - 用 `wc -c README.md train.json prefill.json decode.json`（或查看文件属性）确认各文件大小。
3. **需要观察的现象**：三份 JSON 的大小差异明显。
4. **预期结果**：`prefill.json`（约 16.6 MB）明显大于 `decode.json`（约 4.4 MB）和 `train.json`（约 3.0 MB）——预填充每 GPU 要处理 16K tokens、采集的内核事件最多，文件也最大。粗略地，JSON 越大 ≈ 事件越多 ≈ 计算越密集。
5. 以上大小数字已在仓库当前 HEAD（`4496024`）实测核验；你的克隆结果应一致。

#### 4.1.5 小练习与答案

**练习 1**：为什么说这个仓库是「数据集」而不是「代码库」？
**答案**：仓库中没有任何可编译/可运行的源码或包管理文件（没有 `setup.py`、`package.json` 等），主体是三份 PyTorch Profiler 导出的轨迹 JSON 与三张截图；学习方式是「读数据 + 写脚本分析数据」。

**练习 2**：这份数据是用什么工具采集的？为什么这一点让你不需要任何私有工具就能分析它？
**答案**：PyTorch Profiler。它导出的是标准 Chrome Trace 格式 JSON，可以用 chrome://tracing、Perfetto 等通用工具可视化，用任意 JSON 解析库（如 Python `json`）分析。

**练习 3**：「模拟绝对均衡 MoE 路由」意味着什么？它限制了我们能用这份数据回答哪类问题？
**答案**：意味着每个专家被人为分配到相同数量的 token。它适合研究通信-计算重叠调度本身，但不适合研究真实业务下专家负载不均衡（路由倾斜）带来的性能影响。

### 4.2 三份轨迹的并行配置

#### 4.2.1 概念说明

三份轨迹分别对应三个场景，配置各不相同。先给总表（全部来自 README 原文，见 4.2.3 的逐段精读）：

| 维度 | train.json | prefill.json | decode.json |
|---|---|---|---|
| 阶段 | 训练 | 推理·预填充 | 推理·解码 |
| 专家并行 | EP64 | EP32 | EP128 |
| 张量并行 | TP1 | TP1 | TP1 |
| 序列长度 | 4K | prompt 4K | prompt 4K |
| 批量设置 | —（DualPipe 分块） | 每 GPU 16K tokens | 每 GPU 128 个请求 |
| 重叠策略 | DualPipe：一对前/反向 chunk 重叠 | 双微批重叠计算与 all-to-all | 双微批重叠，且 all-to-all 不占 SM |
| 对齐的真实配置 | DeepSeek-V3 预训练 | V3/R1 线上部署 | 接近线上部署 |

三个场景的重叠策略值得分别用一句话讲透：

- **训练（DualPipe）**：把一对独立的前向与反向 chunk（每个 chunk 含 4 个 MoE 层）放在一起交错执行，前向的计算去填反向的通信空隙，反之亦然。README 特别注明：为简化采集，**流水线（PP）通信未计入轨迹**。
- **预填充（双微批）**：把一批请求拆成两个微批，微批 A 做计算时微批 B 发 all-to-all，轮换往复。有个讲究：两个微批的**注意力计算负载要均衡**——同一条 prompt 甚至可能被拆到两个微批里。
- **解码（双微批 + 低延迟通信）**：同样用双微批，但通信机制升级：all-to-all 期间**不占用 GPU SM**——RDMA 消息发出后所有 SM 立即释放去算数，等计算结束后再等通信收尾。这就是 DeepEP 的 low-latency 实现。

#### 4.2.2 核心流程

两个推理场景的批量口径不同，做个换算有助于建立数量级直觉（以下均为估算，README 未给出微批切分细则）：

预填充阶段，每 GPU 处理 16K tokens、prompt 长 4K，则每 GPU 一次大约处理

\[ \frac{16384 \text{ tokens}}{4096 \text{ tokens/prompt}} = 4 \text{ 条 prompt} \]

若均分成两个微批，每个微批约 2 条 prompt（即约 8K tokens）。解码阶段每 GPU 128 个请求，均分两微批则每微批 64 个请求，而每个请求每步只前进 1 个 token——所以解码每步的计算量远小于预填充，通信开销的相对占比反而更高，这正是解码必须用「不占 SM 的通信」的原因。

#### 4.2.3 源码精读

README 按场景分三段给出配置，逐段引用。

**训练段**：

> [README.md:L5-L12](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/README.md#L5-L12)

```text
The training profile data demonstrates our overlapping strategy for a pair of
individual forward and backward chunks in DualPipe. Each chunk contains 4 MoE
layers. The parallel configuration aligns with DeepSeek-V3 pretraining settings:
EP64, TP1 with 4K sequence length. And the PP communication is not included
during profiling for simplicity.
```

这段说明：轨迹展示的是 **DualPipe** 中一对前/反向 chunk 的重叠；每个 chunk 含 **4 个 MoE 层**；配置对齐 DeepSeek-V3 预训练（**EP64、TP1、4K 序列**）；PP 通信**未采集**。后续第二单元精读 `train.json` 里的 `attn(F/B/W)`、`dispatch(F/B)` 等注解时，会反复用到「4 层一个 chunk」这个数字。

**预填充段**：

> [README.md:L14-L22](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/README.md#L14-L22)

```text
For prefilling, the profile employs EP32 and TP1 (in line with DeepSeek V3/R1's
actual online deployment), with a prompt length set to 4K and a batch size of
16K tokens per GPU. In our prefilling stage, we utilize two micro-batches to
overlap computation and all-to-all communication, while ensuring that the
attention computation load is balanced across the two micro-batches — meaning
that the same prompt may be split between them.
```

这段给出四个关键事实：**EP32、TP1**（对齐 V3/R1 线上部署）、**prompt 4K**、**每 GPU 16K tokens**；重叠手段是**两个微批**，且刻意让两微批的**注意力负载均衡**（允许把同一条 prompt 拆开）。为什么盯住注意力？因为预填充是计算密集型，注意力是最重的算子，两微批负载不均就会一快一慢、重叠失效。

**解码段**：

> [README.md:L24-L30](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/README.md#L24-L30)

```text
For decoding, the profile employs EP128, TP1, and a prompt length of 4K (closely
matching the actual online deployment configuration), with a batch size of 128
requests per GPU. ... the all-to-all communication during decoding does not
occupy GPU SMs: after RDMA messages are issued, all GPU SMs are freed, and the
system waits for the all-to-all communication to complete after the computation
has finished. For more information about the all-to-all implementation, please
refer to DeepEP.
```

这段的配置是 **EP128、TP1、prompt 4K、每 GPU 128 个请求**；最独特的一点是：**all-to-all 不占 GPU SM**——RDMA 消息发出后 SM 全部让给计算，计算结束后才等待通信完成。all-to-all 的实现来自开源库 **DeepEP**（第二、三单元会在轨迹里找到它的内核）。

#### 4.2.4 代码实践

**实践二：从 README 抽取配置填表（阅读型实践）**

1. **实践目标**：不看任何二手资料，只读 README 原文，独立整理三份轨迹的配置表。
2. **操作步骤**：
   - 打开 [README.md](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/README.md)，逐段阅读 Training / Prefilling / Decoding 三节。
   - 建一张五行表格，行是：阶段、专家并行（EP）、张量并行（TP）、序列长度、批量设置；列是三份 JSON。
   - 填完后与 4.2.1 的总表核对。
3. **需要观察的现象**：三段文字的叙述结构其实完全平行（并行配置 → 批量设置 → 重叠策略），抓住结构后信息不易漏。
4. **预期结果**：EP 一栏依次为 64 / 32 / 128；序列长度均为 4K；批量依次为「DualPipe 分块 / 16K tokens 每_gpu / 128 请求每_gpu」。
5. **交叉验证（可选）**：在本地用编辑器打开 `train.json`，搜索字符串 `"world_size"`，应看到 `64`；对 `prefill.json`、`decode.json` 同样操作应分别得到 `32`、`128`。本讲义编写时已在仓库当前 HEAD 上核验过这三个值（三份文件的 `distributedInfo` 中均为 `world_size: 64/32/128`，`backend: "nccl"`）。此步「待本地验证」——取决于你本地的编辑器能否流畅打开数 MB 的 JSON。

#### 4.2.5 小练习与答案

**练习 1**：EP64 / EP32 / EP128 分别是什么意思？为什么训练用 64 而解码用 128？
**答案**：分别表示 64 / 32 / 128 个进程参与专家并行（每进程持有一部分专家）。数值差异反映真实部署形态：训练对齐 DeepSeek-V3 预训练配置，预填充对齐 V3/R1 线上服务，解码集群规模更大（EP128，接近线上部署）。

**练习 2**：解码阶段的 all-to-all 为什么能「不占用 GPU SM」？预填充阶段也是这样吗？
**答案**：解码用的是 DeepEP 的低延迟实现：RDMA 消息一旦发出（发出动作本身极短），GPU SM 即被全部释放去做计算，通信在网卡/链路上自行传输，计算结束后才回收结果。预填充不是——它的 all-to-all 内核会占用 SM，所以靠双微批让「一个微批计算、另一个微批通信」来重叠。

**练习 3**：预填充的双微批为什么要求「注意力负载均衡」，甚至不惜把同一条 prompt 拆到两个微批？
**答案**：两个微批是交替执行的，总时长受较慢一方制约；预填充里注意力是计算大头，若两微批注意力负载不均，重的一方会拖住整体，重叠就失效了。把同一 prompt 拆开是实现负载均衡的手段之一。

### 4.3 仓库文件布局

#### 4.3.1 概念说明

仓库的组织极其简单，就是一个「索引 + 三组数据」的结构：`README.md` 当索引，每个场景配一对文件——原始轨迹 JSON（数据本体）和 `assets/` 下的可视化截图（让你不打开浏览器也能在 GitHub 页面上预览效果）。截图和 JSON 是**同一份数据的两种视图**：JSON 是机器可读的全部事件，截图是人类一眼能看懂的那一小段画面。

#### 4.3.2 核心流程

文件之间的对应关系：

```text
README.md（索引：定位说明 + 三组链接）
 ├── Training       ──→ train.json    ←──→ assets/train.jpg
 ├── Prefilling     ──→ prefill.json  ←──→ assets/prefill.jpg
 └── Decoding       ──→ decode.json   ←──→ assets/decode.jpg
```

使用路径有两条：

- **快速看**：直接看 `assets/` 截图，适合建立直觉。
- **深入看**：下载 JSON，用 chrome://tracing 打开，自由缩放到任意微秒级区间（下一讲 u1-l2 的内容）。

#### 4.3.3 源码精读

README 中三组链接的落点：

- 训练：数据 [README.md:L7](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/README.md#L7)（`[[profile_data]](train.json)`），截图 [README.md:L9](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/README.md#L9)（`![train](assets/train.jpg)`）。
- 预填充：数据 [README.md:L18](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/README.md#L18)，截图 [README.md:L20](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/README.md#L20)。
- 解码：数据 [README.md:L26](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/README.md#L26)，截图 [README.md:L28](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/README.md#L28)。

三个数据文件本体（二进制级的 JSON 大文件，无行号概念，链接指向文件本身）：

- [train.json](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json)
- [prefill.json](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/prefill.json)
- [decode.json](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/decode.json)

三张截图的共同样貌：深色背景的 trace 查看器界面，上方是 CPU 进程/线程轨道（其上叠有较长的用户注解区间，训练截图中被高亮选中的那条长区间就是 DualPipe 一对前/反向 chunk 的调度范围），下方是一排 GPU 进程轨道，每条轨道上密布着彩色内核条——彩色块越密，说明该时段 GPU 越忙；轨道之间的空隙就是潜在的性能气泡。截图中的具体注解名称（如 `1F1B`、`attn`、`dispatch` 等）将在第二单元精读时逐一辨认，本讲只需认识「CPU 轨道在上、GPU 轨道在下、彩条是内核」这一版式。

#### 4.3.4 代码实践

**实践三：为三张截图各写一句说明（观察型实践）**

1. **实践目标**：把三张截图分别与三份 JSON、三种重叠策略对上号。
2. **操作步骤**：
   - 在 GitHub 页面直接点开 [assets/train.jpg](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/assets/train.jpg)、[assets/prefill.jpg](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/assets/prefill.jpg)、[assets/decode.jpg](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/assets/decode.jpg)（或用本地看图软件打开）。
   - 对每张截图回答两个问题：CPU 轨道上的注解区间大致是什么形状（连续一大段，还是两段交替）？GPU 轨道上的内核条是密集连续还是有明显空隙？
   - 为每张写一句说明：「它对应哪份轨迹、展示了什么现象」。
3. **需要观察的现象**：三张图版式相同但节奏不同——训练图呈现一对前/反向 chunk 内部的交错调度；预填充与解码图呈现双微批的交替结构，其中解码图的通信段在独立轨道上、计算段间隙更规律。
4. **预期结果**：参考说明见 5. 综合实践中的答案表；你的句子不必与参考一致，抓住「对应哪份 JSON + 一个可观察现象」即可。
5. 截图观察结果依赖本地显示效果，细节描述「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`assets/prefill.jpg` 展示的是哪份文件的内容？如果要看它的全部事件，应该打开哪个文件？
**答案**：展示 `prefill.json` 的可视化结果；要看全部事件需打开 `prefill.json`（截图只是其中一小段画面的预览）。

**练习 2**：按文件从大到小排序三份轨迹 JSON，并说说这个顺序与各场景批量设置的关系。
**答案**：`prefill.json`（约 16.6 MB）> `decode.json`（约 4.4 MB）> `train.json`（约 3.0 MB）。预填充每 GPU 16K tokens、计算最密集、内核事件最多，文件最大；文件大小粗略正比于事件数量。

**练习 3**：如果你只想花 30 秒了解「通信-计算重叠长什么样」，用仓库里哪个文件？如果要做定量分析呢？
**答案**：30 秒看 `assets/` 下任意一张截图；定量分析必须用对应的 JSON 原始文件写脚本处理。

## 5. 综合实践

**任务：制作「三轨迹配置速查卡」**

把本讲全部内容串成一张可保存的速查卡（建议存为 `profile-data-tutorial/notes-u1-l1.md` 之外的私人笔记，或任何你习惯的形式）：

1. **配置表**：从 README 原文（不是从本讲义）独立整理下表并核对：

| 轨迹文件 | 阶段 | world_size（EP） | 序列长度 | 批量设置 |
|---|---|---|---|---|
| train.json | 训练 | 64 | 4K | —（DualPipe 一对前/反向 chunk，每 chunk 4 个 MoE 层） |
| prefill.json | 推理·预填充 | 32 | prompt 4K | 每 GPU 16K tokens（双微批） |
| decode.json | 推理·解码 | 128 | prompt 4K | 每 GPU 128 个请求（双微批） |

2. **截图说明**：为三张截图各写一句话。参考答案：
   - `assets/train.jpg`：对应 `train.json`，展示 DualPipe 中一对前向/反向 chunk 的重叠执行——CPU 轨道上的长注解区间覆盖了下方多个 GPU 进程上交错排列的计算内核。
   - `assets/prefill.jpg`：对应 `prefill.json`，展示预填充阶段两个微批如何交替地「一个算、一个通信」，让 all-to-all 与计算在时间上错开重叠。
   - `assets/decode.jpg`：对应 `decode.json`，展示解码阶段低延迟 all-to-all 的效果——通信在独立通道上进行且不占用 GPU SM，GPU 轨道上的计算内核得以连续排布。
3. **交叉验证**：在本地对三份 JSON 分别搜索 `"world_size"`，确认 64 / 32 / 128 与表格一致（本讲义编写时已核验，本地结果「待本地验证」）。

完成这张卡，本讲的四个目标就全部落地了。

## 6. 本讲小结

- `profile-data` 是 DeepSeek 公开的**性能分析数据集**：三份由 PyTorch Profiler 导出的 Chrome Trace JSON，覆盖训练（EP64）、预填充（EP32）、解码（EP128）三个场景，无任何可运行代码。
- 仓库主线主题是**通信-计算重叠**：训练用 DualPipe 的一对前/反向 chunk，预填充/解码用双微批，解码更进一步让 all-to-all 完全不占 GPU SM（RDMA 发出后释放 SM）。
- 每个场景 = 1 份 JSON + 1 张截图，README 是索引；截图看直觉，JSON 做定量。
- 采集前提是**模拟绝对均衡的 MoE 路由**：数据反映理想负载下的调度行为，不能直接外推真实路由倾斜场景。
- 预填充按 token 计批量（16K tokens/GPU）、解码按请求计批量（128 requests/GPU），口径不同源于两阶段计算密度差异巨大。

## 7. 下一步学习建议

下一讲（u1-l2《把轨迹文件看起来的两种方式》）将动手把 `train.json` 真正打开：用 Chrome 的 `chrome://tracing`（或 Perfetto UI，ui.perfetto.dev）加载这几 MB 的 JSON，学习缩放、轨道折叠与事件点选，把本讲「截图里的版式」变成自己可以任意探索的交互界面。

在进入下一讲之前，建议你先做一件事：用文本编辑器打开 `train.json` 的开头几十个字符，扫一眼 JSON 的顶层字段名（如 `traceEvents`、`deviceProperties`、`distributedInfo`）——这会为 u1-l3《轨迹文件整体结构》做好铺垫。
