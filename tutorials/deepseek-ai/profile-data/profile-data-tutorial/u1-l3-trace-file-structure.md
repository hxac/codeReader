# u1-l3 轨迹文件整体结构：PyTorch Profiler 的导出格式

## 1. 本讲目标

上一讲我们学会了用 chrome://tracing 和 Perfetto 把轨迹「看」起来。这一讲我们换一个视角，把轨迹文件当作一份**数据文件**来解剖。读完本讲，你应该能够：

1. 说出 PyTorch Profiler 导出的 Chrome Trace JSON 有哪四个核心顶层字段（`schemaVersion`、`deviceProperties`、`distributedInfo`、`traceEvents`），以及每個字段回答了什么问题。
2. 从 `deviceProperties` 读出采集机器的 GPU 硬件档案：NVIDIA H800、132 个 SM、约 79.1 GiB（标称 80 GB）显存、Hopper 架构（compute capability 9.0）。
3. 用 `distributedInfo` 区分三份轨迹：`backend` 均为 `nccl`、`rank` 均为 0、`world_size` 分别为 64 / 32 / 128，并与 README 里的 EP64 / EP32 / EP128 配置一一对应。
4. 用 Python 编写脚本加载这三份 JSON，统计 `traceEvents` 的事件数量（14240 / 85422 / 19417），并以表格形式汇报。

本讲只看「文件的骨架」，不深入单条事件的字段含义——那是下一讲（u1-l4）的内容。

## 2. 前置知识

本讲需要一点新概念，都用通俗方式解释：

- **JSON 对象与数组**：JSON 里 `{}` 包起来的是「对象」（键值对的集合），`[]` 包起来的是「数组」（有序列表）。三份轨迹文件整体上就是「一个对象，里面有一个大数组」。
- **Chrome Trace Event 格式**：Chrome 浏览器开发者工具用来记录时间线的开放格式，核心就是一个 `traceEvents` 事件数组；任何实现了这个格式的工具（chrome://tracing、Perfetto）都能打开。PyTorch Profiler 导出时在这个格式之上**附加**了几个专有的顶层键（`schemaVersion`、`deviceProperties`、`distributedInfo`、`traceName`），用来携带 PyTorch 与分布式训练的上下文。
- **顶层字段 vs 事件字段**：打开 JSON 后最外层的键叫顶层字段（本讲主题）；每个事件内部的 `ph`、`cat`、`ts` 等叫事件字段（下一讲主题）。
- **world_size / rank / backend**（u1-l1 已引入，这里精确化）：分布式训练把一个任务拆到多个进程上，`world_size` 是进程总数，`rank` 是当前进程的编号，`backend` 是进程间集合通信使用的库——这里是 `nccl`（NVIDIA Collective Communications Library，NVIDIA 为 GPU 集群提供的高性能通信库）。
- **GPU 硬件小词典**：**SM**（Streaming Multiprocessor，流多处理器）是 GPU 执行计算的基本单元，SM 越多、并行度越高；**显存**（`totalGlobalMem`）是 GPU 上高速存储的容量；**compute capability**（计算能力版本，如 9.0）标识 GPU 架构代次，9.0 对应 Hopper 架构；**warp** 是 GPU 上最小的调度单位，大小为 32 个线程；**shared memory** 是 SM 上程序员可控的高速缓存。

另外回顾 u1-l2 的一个结论：`ts` 以**微秒**为单位。本讲会进一步看到这些微秒时间戳其实是 Unix 纪元时间。

## 3. 本讲源码地图

本仓库没有传统意义的源码，三份轨迹 JSON 本身就是「源码」：

| 文件 | 大小 | 行数 | 场景 | 本讲关注点 |
|---|---|---|---|---|
| [train.json](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json) | 3.1 MB | 97654 行（末行无换行符） | 训练（EP64） | 顶层结构最完整：8 个 GPU 条目、文件头尾都可按行号引用 |
| [prefill.json](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/prefill.json) | 17.5 MB | 570650 行 | 预填充（EP32） | 与 train 同构，但 `deviceProperties` 只有 1 个条目；事件数最多 |
| [decode.json](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/decode.json) | 4.7 MB | **1 行**（单行压缩） | 解码（EP128） | 结构与上面两份完全相同，只是整份 JSON 压成一行，无法按行号定位 |
| [README.md](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/README.md) | — | 31 行 | 说明文档 | 用来核对 `distributedInfo` 与实际并行配置的对应关系 |

一个立刻值得注意的工程细节：**同样的数据，两种排版**。train.json 和 prefill.json 是带缩进的多行格式（一条事件约 6~8 行，肉眼可读），decode.json 则是单行压缩格式（4.7 MB 挤在一行里）。这也解释了为什么本讲的永久链接对 decode.json 只能指向第 1 行——分析它必须靠 Python，肉眼阅读基本不可能。

## 4. 核心概念与源码讲解

本讲的最小模块：

1. 顶层字段：一份轨迹的「封面信息」
2. `deviceProperties`：采集机器的 GPU 硬件档案
3. `distributedInfo`：并行世界的「户口本」
4. `traceEvents`：事件的海洋

### 4.1 顶层字段：一份轨迹的「封面信息」

#### 4.1.1 概念说明

把一份几 MB 的轨迹 JSON 用文本编辑器打开（或者只看前几十行），最外层是一个对象。它像一本书的封面与目录，回答四个问题：

| 顶层字段 | 回答的问题 | 类型 |
|---|---|---|
| `schemaVersion` | 这份文件是什么版本的结构？ | 整数 |
| `deviceProperties` | 数据是在什么硬件上采集的？ | 数组，每个元素描述一块 GPU |
| `distributedInfo` | 采集时进程在多大的分布式世界里、用什么通信后端？ | 对象 |
| `traceEvents` | 到底发生了什么？ | 数组，每个元素是一个「事件」 |

此外 PyTorch 还会附加第五个键 `traceName`——导出时工程师给这份轨迹起的原始文件名，往往编码了实验配置（见 4.4.3）。所以严格地说，三份文件都能观察到 **5 个顶层键**：上述四个核心字段 + `traceName`。

为什么要把硬件与分布式信息写进轨迹？因为**脱离硬件与并行配置谈耗时没有意义**：同一个 50 微秒的内核，在 132 SM 的 H800 上和在游戏卡上含义完全不同；同一个 all-to-all 通信，在 EP32 和 EP128 下代价也不同。把上下文与事件放在同一个文件里，轨迹才是自包含的。

#### 4.1.2 核心流程

阅读一份轨迹文件的推荐顺序：

```text
打开 JSON
  ├─ 1. 看 schemaVersion        → 确认格式版本（本仓库三份都是 1）
  ├─ 2. 看 deviceProperties     → 知道硬件底座（H800 × N）
  ├─ 3. 看 distributedInfo      → 知道并行规模（nccl / rank 0 / world_size）
  ├─ 4. 数一数 traceEvents      → 知道数据量级（万级事件）
  └─ 5. 才开始逐条读事件        → 下一讲的内容
```

#### 4.1.3 源码精读

train.json 的开头（注意第 1 行是空行，对象从第 2 行开始）：

[train.json:L2-L12](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L2-L12) —— 第 3 行声明 `schemaVersion: 1`；第 4 行开始是 `deviceProperties` 数组，第 5~12 行是第一块 GPU（`id: 0`）的完整属性。

[train.json:L70-L71](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L70-L71) —— 第 70 行是一行式的 `distributedInfo`；第 71 行开启 `traceEvents` 数组，从第 72 行起直到文件接近末尾都是事件。

[train.json:L97652-L97654](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L97652-L97654) —— `traceEvents` 数组在第 97652 行收口，随后是第五个顶层键 `traceName`，值为 `traces/prof_ep64_tp1_varsm_hidden7168_seqlen4096.json`，最后第 97654 行是整个 JSON 对象的闭合花括号。

对照 prefill.json 的对应位置，结构完全同构：

[prefill.json:L3-L15](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/prefill.json#L3-L15) —— 同样是 `schemaVersion` → `deviceProperties`（但只有 1 个条目，第 5~12 行）→ `distributedInfo`（第 14 行）→ `traceEvents`（第 15 行起）。

[decode.json:L1](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/decode.json#L1) —— 整份文件就一行，开头依次是 `{"schemaVersion": 1, "deviceProperties": [...], "distributedInfo": {...}, "traceEvents": [...]}`，结尾以 `"traceName": "./decode-debug-rank0.json"}` 收束。五个键与上面两份完全一致。

#### 4.1.4 代码实践

**实践目标**：用 Python 亲眼确认三份文件的顶层键集合，不靠讲义转述。

**操作步骤**（以下为示例代码，不是仓库自带脚本）：

```python
# explore_top_keys.py —— 示例代码
import json

for path in ["train.json", "prefill.json", "decode.json"]:
    with open(path, "r", encoding="utf-8") as f:
        trace = json.load(f)
    print(path, "的顶层键:", sorted(trace.keys()))
```

**需要观察的现象**：三行输出是否完全一致？有没有出乎意料的键？

**预期结果**：三份文件都输出 `['deviceProperties', 'distributedInfo', 'schemaVersion', 'traceEvents', 'traceName']`——四个核心字段加上 `traceName`。若你的输出缺少某个键，说明文件下载不完整或被截断，应重新下载。

#### 4.1.5 小练习与答案

**练习 1**：为什么 chrome://tracing 不认识 `deviceProperties` 也能正常打开这些文件？

**参考答案**：Chrome Trace Event 格式的核心只有 `traceEvents` 事件数组，查看器会忽略自己不认识的顶层键。`deviceProperties`、`distributedInfo` 等是 PyTorch Profiler 附加的扩展信息，供人和分析脚本阅读，不影响渲染。

**练习 2**：`schemaVersion: 1` 如果将来变成 2，谁需要关心？

**参考答案**：编写分析脚本的工程师需要关心——版本号提示顶层结构与事件字段的约定可能发生变化，脚本应先检查版本再解析，避免用旧假设误读新格式。对只在 UI 里看图的使用者影响不大。

**练习 3**：decode.json 为什么无法像 train.json 一样给出「第 N 行是某个字段」的引用？

**参考答案**：decode.json 是单行压缩 JSON（4.7 MB 全在第 1 行），没有换行符可以分割内容，行号定位失效；只能通过 Python 解析或文本搜索定位字段。

### 4.2 deviceProperties：采集机器的 GPU 硬件档案

#### 4.2.1 概念说明

`deviceProperties` 是采集程序所在节点上 CUDA 设备属性的序列化，内容对应 CUDA 的 `cudaGetDeviceProperties` 接口返回的设备属性。它告诉你在解读所有内核耗时之前，先看清「跑道」长什么样。

第一个条目的字段含义（三份文件中该条目内容完全相同）：

| 字段 | 值 | 含义 |
|---|---|---|
| `id` | 0 | CUDA 设备编号 |
| `name` | NVIDIA H800 | GPU 型号，DeepSeek 训练/推理集群使用的算力卡 |
| `totalGlobalMem` | 84943110144 | 显存字节数，\( \frac{84943110144}{1024^3} \approx 79.1 \) GiB，即标称 80 GB |
| `computeMajor` / `computeMinor` | 9 / 0 | 计算能力 9.0，Hopper 架构 |
| `numSms` | 132 | 132 个流多处理器（SM），决定可并行的内核规模 |
| `maxThreadsPerBlock` | 1024 | 每个线程块最多 1024 线程 |
| `maxThreadsPerMultiprocessor` | 2048 | 每个 SM 最多同时驻留 2048 线程 |
| `regsPerBlock` | 65536 | 每块可用 64K 个 32 位寄存器 |
| `warpSize` | 32 | 一个 warp 32 个线程，GPU 最小调度单位 |
| `sharedMemPerBlock` | 49152 | 每块默认共享内存 48 KiB |
| `sharedMemPerMultiprocessor` | 233472 | 每个 SM 的共享内存总量约 228 KiB |
| `sharedMemPerBlockOptin` | 232448 | 单块经 opt-in 机制可申请的最大共享内存，约 227 KiB |

这些数字在后续单元会反复变得有用：比如 decode 阶段 DeepEP 的低延迟内核会「占用一部分 SM 做通信、其余 SM 做计算」，理解这类设计的基准就是 `numSms = 132`。

一个必须诚实交代的差异：**train.json 的 `deviceProperties` 有 8 个条目（GPU 0~7），而 prefill.json 和 decode.json 只有 1 个条目（GPU 0）**。这只说明 train 导出时把节点上 8 块卡的属性都写了进去，另两份只写了 1 块；至于当初为何这样选择，仓库没有说明（**待确认**），它也不影响阅读轨迹——三份轨迹的事件都来自 rank 0 视角。

#### 4.2.2 核心流程

从 `deviceProperties` 提取硬件档案的流程：

```text
trace["deviceProperties"]          # 一个数组
  └─ 对每个条目 d：
       ├─ d["name"]                # 型号 → NVIDIA H800
       ├─ d["numSms"]              # SM 数 → 132
       ├─ d["totalGlobalMem"]      # 字节 → 换算成 GiB
       └─ d["computeMajor"], d["computeMinor"]  # 9.0 → Hopper
```

显存换算公式：

\[ \text{显存(GiB)} = \frac{\text{totalGlobalMem}}{1024^3} = \frac{84943110144}{1073741824} \approx 79.1 \]

#### 4.2.3 源码精读

[train.json:L4-L12](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L4-L12) —— `deviceProperties` 数组的第一个条目：`id: 0` 的 NVIDIA H800，`totalGlobalMem: 84943110144`、`numSms: 132`、`computeMajor: 9` 等全部关键字段。

[train.json:L13-L69](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L13-L69) —— 第 2~8 个条目，`id` 从 1 到 7，内容与第一个条目完全相同：节点上 8 块规格一致的 H800。

[train.json:L70](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L70) —— `deviceProperties` 数组在这里结束，下一行就是 `distributedInfo`，可以此确认 train 的设备数组长度为 8。

[prefill.json:L4-L13](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/prefill.json#L4-L13) —— prefill 的 `deviceProperties` 只有一个 `id: 0` 条目（内容与 train 的第一个条目逐字段相同），第 13 行就闭合了数组。

[decode.json:L1](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/decode.json#L1) —— decode 同样只有一个设备条目，单行文本开头即可看到 `{"id": 0, "name": "NVIDIA H800", ...}`。

#### 4.2.4 代码实践

**实践目标**：把 `deviceProperties` 翻译成人类可读的硬件档案表。

**操作步骤**（示例代码）：

```python
# explore_devices.py —— 示例代码
import json

with open("train.json", "r", encoding="utf-8") as f:
    trace = json.load(f)

devices = trace["deviceProperties"]
print(f"设备条目数: {len(devices)}")
for d in devices:
    gib = d["totalGlobalMem"] / 1024 ** 3
    print(f"GPU {d['id']}: {d['name']}, "
          f"SM={d['numSms']}, 显存={gib:.1f} GiB, "
          f"计算能力={d['computeMajor']}.{d['computeMinor']}")
```

**需要观察的现象**：打印出几行？各行的规格是否一致？

**预期结果**：train.json 打印 8 行，全部是 `NVIDIA H800, SM=132, 显存=79.1 GiB, 计算能力=9.0`。把脚本中的文件名换成 prefill.json 或 decode.json，则只打印 1 行，且数值相同。

#### 4.2.5 小练习与答案

**练习 1**：`totalGlobalMem` 是 84943110144 字节，为什么大家叫它「80 GB 显存」？

**参考答案**：换算成二进制单位约 79.1 GiB；厂商标称容量用十进制 GB（80 GB = 8×10¹⁰ 字节量级），且部分显存被保留，故日常称 80 GB 卡。分析脚本里应以字节原值换算 GiB 为准。

**练习 2**：知道 `numSms = 132` 对读轨迹有什么用？

**参考答案**：SM 数是 GPU 并行度的「分母」。例如一个内核只启动少量线程块时，132 个 SM 大部分闲置；DeepEP 低延迟模式（decode 轨迹）甚至会让通信「先占一部分 SM、发出 RDMA 后全部释放」。没有 SM 数就无法判断内核是否吃满了硬件。

**练习 3**：`computeMajor: 9, computeMinor: 0` 对应什么架构？为什么值得记录？

**参考答案**：计算能力 9.0，Hopper 架构。架构代次决定了可用的特性（如 WARP 特化、TMA、FP8 矩阵指令等）与性能量级，记录它才能正确解读内核名称与耗时。

### 4.3 distributedInfo：并行世界的「户口本」

#### 4.3.1 概念说明

`distributedInfo` 是一个只有三个键的小对象，却是区分三份轨迹最快的依据：

| 键 | 含义 | 三份轨迹的值 |
|---|---|---|
| `backend` | 分布式通信后端 | 均为 `nccl`（NVIDIA 的 GPU 集合通信库） |
| `rank` | 本进程在分布式世界中的编号 | 均为 `0` |
| `world_size` | 分布式世界的进程总数 | train=**64**，prefill=**32**，decode=**128** |

结合 u1-l1 学过的并行概念：三份轨迹分别采集于 EP64（训练）、EP32（预填充）、EP128（解码）的 DeepSeek-V3 规模集群。`world_size` 与 README 中「专家并行数」的数字直接对应：

- 训练：EP64、TP1、4K 序列长度，见 [README.md:L12](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/README.md#L12) —— `world_size: 64` 正是 EP64 的进程规模。
- 预填充：EP32、TP1、每 GPU 16K tokens，见 [README.md:L22](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/README.md#L22) —— 对应 `world_size: 32`。
- 解码：EP128、TP1、每 GPU 128 个请求，见 [README.md:L30](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/README.md#L30) —— 对应 `world_size: 128`。

`rank: 0` 是一个关键阅读前提：**每份轨迹只记录了 64/32/128 个进程中的 0 号进程**。所以时间线上看不到其他 rank 的活动，分析「通信等待」时要记住对端并不在这份文件里。这与 u1-l2 观察到的「内核只出现在本机 GPU 上」是同一个原因。

#### 4.3.2 核心流程

```text
读 trace["distributedInfo"]
  ├─ backend == "nccl"     → 集合通信走 NCCL（all-to-all 由 DeepEP 基于 NCCL/RDMA 实现）
  ├─ rank == 0             → 单进程视角，其他 rank 不可见
  └─ world_size ∈ {64, 32, 128}
        ├─ 64  → train.json   → 训练,   DualPipe, EP64
        ├─ 32  → prefill.json → 预填充, 双微批,   EP32
        └─ 128 → decode.json  → 解码,   双微批,   EP128
```

#### 4.3.3 源码精读

[train.json:L70](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L70) —— train 的分布式信息：`{"backend": "nccl", "rank": 0, "world_size": 64}`。

[prefill.json:L14](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/prefill.json#L14) —— prefill：`world_size` 为 32。

[decode.json:L1](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/decode.json#L1) —— decode：`world_size` 为 128（单行文件中位于 `"distributedInfo"` 之后的第一个数字即是）。

三条链接并排打开，唯一变化的就是 `world_size`——这就是「用 distributedInfo 区分三份轨迹」的全部操作。

#### 4.3.4 代码实践

**实践目标**：用脚本输出三份轨迹的分布式信息对照表，并与 README 核对。

**操作步骤**（示例代码）：

```python
# explore_dist.py —— 示例代码
import json

for path in ["train.json", "prefill.json", "decode.json"]:
    with open(path, "r", encoding="utf-8") as f:
        trace = json.load(f)
    d = trace["distributedInfo"]
    print(f"{path:14} backend={d['backend']}  rank={d['rank']}  "
          f"world_size={d['world_size']}")
```

**需要观察的现象**：三行的 `backend` 与 `rank` 是否相同？`world_size` 依次是多少？

**预期结果**：三行均为 `backend=nccl  rank=0`，`world_size` 依次为 64、32、128。随后打开 [README.md](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/README.md) 的训练、预填充、解码三节，确认这三个数字与 EP64 / EP32 / EP128 一一对应。

#### 4.3.5 小练习与答案

**练习 1**：既然 `rank: 0`，轨迹里会不会缺少其他 rank 发来的通信？

**参考答案**：会「看不到对端」。本 rank 发出的 dispatch 与收到的 combine 都会在本 rank 的 GPU 时间线上留下内核，但对端 rank 的处理过程不在这份文件里；分析通信延迟时要意识到这一点，必要时需要同时采集多个 rank 的轨迹对照。

**练习 2**：`backend: nccl` 与 README 里说的 DeepEP all-to-all 矛盾吗？

**参考答案**：不矛盾。`backend` 描述的是 PyTorch 分布式层的通信后端；DeepEP 的 all-to-all（dispatch/combine）是其上针对 MoE 优化的实现（internode/low-latency 两代），轨迹中它们以 GPU 内核形式出现，属于更上层的机制。README 在解码一节也明确指向 DeepEP 仓库（[README.md:L30](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/README.md#L30)）。

**练习 3**：为什么预填充用 EP32 而解码用 EP128？

**参考答案**：这是部署取舍：预填充每个 rank 处理 16K tokens，计算重、单卡吞吐优先，较小的 EP 已够；解码每 rank 仅 128 个请求、每步 token 少，扩大 EP 到 128 能让每个专家分到更多卡的负载、摊薄每卡通信量，配合低延迟 all-to-all 控制时延。深入对比如 u3-l3 的主题。

### 4.4 traceEvents：事件的海洋

#### 4.4.1 概念说明

`traceEvents` 是文件的主体：一个普通 JSON 数组，每个元素描述一件「发生的事」——一次 CPU 算子调用、一次 CUDA API 调用、一个 GPU 内核、一条进程命名元数据等。三份文件的事件数量分别是：

| 文件 | 事件数 | 直观理解 |
|---|---|---|
| train.json | 14240 | 约 130 ms 采集窗口内的训练活动 |
| prefill.json | 85422 | 事件最多，双微批预填充的完整内核图谱 |
| decode.json | 19417 | 若干解码步的活动 |

两个本讲就要建立的认识：

1. **时间戳是 Unix 纪元微秒**。例如 train 第一个事件的 `ts = 1740461679479712`，除以 \(10^6\) 得 Unix 秒 1740461679.48，换算后约为 2025 年 2 月 25 日（UTC）。这说明轨迹记录的是真实墙钟时间，而非从 0 开始的相对时间。换算方法：

   \[ t_{\text{Unix秒}} = \frac{ts}{10^6}, \quad \text{事件区间} = [ts,\ ts + dur]\ (\mu s) \]

2. **数组顺序 ≠ 时间顺序**。train.json 的第一个事件（[train.json:L72-L78](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L72-L78)）是一个**反向传播**算子 `autograd::engine::evaluate_function: BackwardWithAuxiliaryLossBackward`，而采集窗口约 130 ms 内既有前向也有反向——profiler 是在训练进行中途开始记录的，事件按「记录顺序」写入，做任何时间分析前都必须自己按 `ts` 排序。

另外，事件数组并非只有「活动」：它的**尾部**藏着一批 `ph: "M"` 的元数据事件（给进程、线程起名字，u1-l2 已见过）和几个收尾标记。train.json 中 `PyTorch Profiler (0)` 这个 span 的 `dur = 129749` 微秒，正是本次采集窗口的长度（约 130 ms）。

#### 4.4.2 核心流程

```text
trace["traceEvents"]                    # 数组，长度即事件总数
  ├─ [0]      首个事件（train: 反向算子；prefill/decode: aten::empty）
  ├─ [1..N-1] CPU 算子 / CUDA API / GPU 内核 / 注解 ...（下一讲逐字段精读）
  └─ 尾部     process_name / process_labels / thread_name 元数据
              + "PyTorch Profiler (0)" span（采集窗口长度）
              + "Record Window End" 收尾标记
之后顶层键 traceName 记录导出时的原始文件名
```

事件字段（`ph`、`cat`、`name`、`pid`、`tid`、`ts`、`dur`、`args`）的完整讲解是 u1-l4 的任务，本讲只需认得它们是「每条事件内部的键」。

#### 4.4.3 源码精读

[train.json:L71-L78](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L71-L78) —— `traceEvents` 数组的开头：第一条事件是 `cat: "cpu_op"`、`name` 为 `autograd::engine::evaluate_function: BackwardWithAuxiliaryLossBackward` 的完成事件，`pid: 1166, tid: 2571`，`ts: 1740461679479712, dur: 85`（微秒）。`pid 1166` 就是 CPU 进程。

[prefill.json:L15-L22](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/prefill.json#L15-L22) —— prefill 的第一条事件是最普通的 `aten::empty`（分配张量），`pid: 1097`；对比 train 的首事件，可见不同轨迹的起始记录点各不相同。

[train.json:L97401-L97405](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L97401-L97405) —— 数组尾部的元数据事件：`process_labels` 把 `pid 1166` 标记为 `CPU`。

[train.json:L97414-L97423](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L97414-L97423) —— 紧随其后，`pid 0` 被 `process_name` 命名为 `python3`、`process_labels` 标记为 `GPU 0`。GPU 1 的同款元数据块从 [train.json:L97431-L97441](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L97431-L97441) 开始，GPU 2~7 依次等距延续下去（每块约 18 行）。（prefill 与 decode 也都有 CPU + GPU 0~7 共 9 条进程轨道的元数据。）

[train.json:L97629-L97636](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L97629-L97636) —— 名为 `PyTorch Profiler (0)` 的 span 事件：`ts: 1740461679470630, dur: 129749`，即本次采集窗口约 129.7 ms——这就是 u1-l2 中「1F1B 总注解约 112 ms」得以完整落进窗口的原因。

[train.json:L97648-L97653](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L97648-L97653) —— 两条收尾事件 `Iteration Start: PyTorch Profiler` 与 `Record Window End`（`ph: "i"` 即时事件）之后，数组闭合，第五个顶层键 `traceName` 出现：`traces/prof_ep64_tp1_varsm_hidden7168_seqlen4096.json`——文件名里编码了 ep64、tp1、hidden7168（DeepSeek-V3 隐层维度 7168）、seqlen4096 等配置（其中 `varsm` 的含义仓库未说明，待确认）。

[prefill.json:L570649](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/prefill.json#L570649) —— prefill 的 `traceName` 为 `./dsv3-600B-tp1-ep32-input4096-output1-bs16384-split1-sm24.json`，与 README 的「EP32、4K prompt、16K tokens/GPU」完全互相印证。

[decode.json:L1](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/decode.json#L1) —— decode 的 `traceName` 为 `./decode-debug-rank0.json`，文件名直接点明「rank 0 视角」。

#### 4.4.4 代码实践

**实践目标**：统计三份文件的事件数量、时间范围，并亲手验证「数组顺序不等于时间顺序」。

**操作步骤**（示例代码）：

```python
# explore_events.py —— 示例代码
import json
from datetime import datetime, timezone

for path in ["train.json", "prefill.json", "decode.json"]:
    with open(path, "r", encoding="utf-8") as f:
        trace = json.load(f)
    evts = trace["traceEvents"]
    ts_list = [e["ts"] for e in evts if "ts" in e and "dur" in e]
    first, last = evts[0], evts[-1]
    print(f"{path:14} 事件数={len(evts)}")
    print(f"  首个事件: {first.get('name', '?')[:60]}  ts={first.get('ts')}")
    print(f"  末个事件: {last.get('name', '?')[:60]}  ts={last.get('ts')}")
    print(f"  最早 ts={min(ts_list)}  最晚 ts={max(ts_list)}")
    t0 = datetime.fromtimestamp(min(ts_list) / 1e6, tz=timezone.utc)
    print(f"  最早时间换算: {t0.isoformat()} (UTC)")
```

**需要观察的现象**：

1. 三份文件的事件数各是多少？
2. 「首个事件的 ts」和「全部事件里最早的 ts」是否一致？
3. 最早的时间戳换算成日期是多少？

**预期结果**：事件数分别为 14240、85422、19417（本讲义作者已用文本方式统计 `"ph":` 的出现次数核实，读者运行 `len(evts)` 应得到相同数字）；「首个事件的 ts」通常晚于「最早的 ts」（train 的首个事件是反向算子，而更早发生的事件排在数组后部），直接证明数组顺序不是时间顺序；最早时间戳约为 2025-02-25（train、prefill）与 2025-03-21（decode）前后（UTC）。若你的 Python 环境时区不同，打印的日期可能相差一天以内。

#### 4.4.5 小练习与答案

**练习 1**：`ts = 1740461679479712`，`dur = 85`。这个事件占据了哪段时间（微秒）？换算成 Unix 秒是多少？

**参考答案**：区间为 \([1740461679479712,\ 1740461679479797]\) 微秒；Unix 秒约为 1740461679.4797 至 1740461679.4799，即约 2025-02-25 05:33 UTC 之后的一瞬间，事件本身仅持续 85 微秒。

**练习 2**：为什么事件数组里第一个元素会是反向传播算子？

**参考答案**：profiler 在训练已进行到中途时才开始记录（需要 warmup 与稳定的迭代），且数组按记录/整理顺序而非严格 `ts` 排序写入；恰好这份 train 轨迹记录起点落在某次迭代的反向阶段。教训是：分析前永远先按 `ts` 排序。

**练习 3**：`traceName` 对分析有什么实际用处？

**参考答案**：它保留了导出时的原始文件名，常常编码了模型、并行配置与批量等实验参数（如 prefill 的 `tp1-ep32-input4096-bs16384`），在文件被重命名或归档后仍可追溯采集配置，相当于内置的实验备注。

## 5. 综合实践

把本讲四个模块合成一个可复用的「轨迹体检」脚本 `explore_trace.py`（示例代码）：对任意一份 Chrome Trace JSON 输出它的封面信息汇总表。

```python
#!/usr/bin/env python3
# explore_trace.py —— 示例代码：轨迹顶层结构体检
# 用法: python3 explore_trace.py train.json prefill.json decode.json
import json
import os
import sys

files = sys.argv[1:] or ["train.json", "prefill.json", "decode.json"]
rows = []
for path in files:
    with open(path, "r", encoding="utf-8") as f:
        trace = json.load(f)
    devs = trace["deviceProperties"]
    dist = trace["distributedInfo"]
    rows.append([
        path,
        round(os.path.getsize(path) / 1024 / 1024, 1),      # 大小 MB
        trace["schemaVersion"],
        len(devs),                                            # GPU 条目数
        devs[0]["name"],
        devs[0]["numSms"],
        round(devs[0]["totalGlobalMem"] / 1024 ** 3, 1),      # GiB
        f"{dist['backend']}/r{dist['rank']}/ws{dist['world_size']}",
        len(trace["traceEvents"]),
        trace.get("traceName", ""),
    ])

headers = ["文件", "大小MB", "schema", "GPU数", "型号", "SM", "显存GiB",
           "backend/rank/world_size", "事件数"]
print("| " + " | ".join(headers) + " |")
print("|" + "---|" * len(headers))
for r in rows:
    print("| " + " | ".join(str(x) for x in r) + " |")
print()
for r in rows:
    print(f"{r[0]} 的 traceName: {r[-1]}")
```

**任务**：在仓库根目录运行它，检查输出的表格，然后回答三个问题：

1. 三份轨迹中哪两个顶层字段的值完全相同、哪个字段各不相同？据此写一句「最快区分三份文件」的判据。
2. train 与 prefill/decode 的 `GPU数` 列差在哪儿？结合 4.2 说明这个差异影响什么、不影响什么。
3. 用 `traceName` 为三份文件各写一句「档案卡片」，说明它编码了哪些配置信息。

**预期结果**（数字经文本统计核实，运行后应一致）：

| 文件 | 大小MB | schema | GPU数 | 型号 | SM | 显存GiB | backend/rank/world_size | 事件数 |
|---|---|---|---|---|---|---|---|---|
| train.json | 3.0 | 1 | 8 | NVIDIA H800 | 132 | 79.1 | nccl/r0/ws64 | 14240 |
| prefill.json | 16.7 | 1 | 1 | NVIDIA H800 | 132 | 79.1 | nccl/r0/ws32 | 85422 |
| decode.json | 4.4 | 1 | 1 | NVIDIA H800 | 132 | 79.1 | nccl/r0/ws128 | 19417 |

参考结论：① `schemaVersion` 与 `deviceProperties`（首个条目）完全相同，`distributedInfo.world_size`（64/32/128）各不相同，看 `world_size` 一眼即可区分三份文件；② train 有 8 个 GPU 条目、另两份只有 1 个，这只影响「我们知道节点上有几块卡的属性」，不影响任何事件的解读；③ 例如 prefill 的 traceName `dsv3-600B-tp1-ep32-input4096-output1-bs16384-split1-sm24` 直接给出模型规模、TP/EP、输入输出长度与批量。若运行结果与此不符（例如事件数对不上），优先怀疑文件下载不完整。

## 6. 本讲小结

- 三份轨迹 JSON 的顶层都是同一套结构：四个核心字段 `schemaVersion`（=1）、`deviceProperties`（GPU 硬件档案）、`distributedInfo`（nccl / rank 0 / world_size）、`traceEvents`（事件数组），外加 PyTorch 附加的 `traceName`（原始文件名，常编码实验配置）。
- `deviceProperties` 告诉我们采集底座是 NVIDIA H800：132 个 SM、约 79.1 GiB（标称 80 GB）显存、计算能力 9.0（Hopper）；train 记录了 8 块卡的属性，prefill/decode 只记录 1 块。
- `distributedInfo` 是区分三份轨迹的最快依据：`world_size` 64 / 32 / 128 分别对应 README 中的 EP64 训练、EP32 预填充、EP128 解码；`rank: 0` 提醒我们这是单进程视角。
- `traceEvents` 是数据主体，规模为 14240 / 85422 / 19417 条；`ts` 是 Unix 纪元**微秒**，且数组按记录顺序而非时间顺序排列，分析前必须自行按 `ts` 排序。
- 事件数组尾部藏有进程/线程命名元数据与 `PyTorch Profiler (0)` span（train 的采集窗口约 129.7 ms）等收尾信息，文件最末是 `traceName`。
- decode.json 是单行压缩 JSON，肉眼按行阅读不可行——这预示了后续所有深入分析都必须以 Python 脚本为主。

## 7. 下一步学习建议

顶层骨架已经看清，下一讲 **u1-l4《读懂单条事件：ph/cat/pid/tid/ts/dur 与元数据事件》**将放大到事件内部：`ph` 的 X/M/s/f 各代表什么、`cat` 的 cpu_op / kernel / cuda_runtime / user_annotation 等类别如何区分、元数据事件如何把数字 `pid`/`tid` 变成「GPU 0 / stream 7」这样可读的名字。建议在进入下一讲前，先把本讲的 `explore_trace.py` 跑通——之后的所有脚本都建立在「能用 `json.load` 稳定加载这三份文件」之上。等到第二单元精读轨迹时，可以回头重看 `deviceProperties` 的 `numSms: 132` 与 `distributedInfo` 的 `world_size`，它们将反复出现在通信-计算重叠的分析里。
