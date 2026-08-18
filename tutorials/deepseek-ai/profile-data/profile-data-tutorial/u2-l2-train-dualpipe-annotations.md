# train.json 与 DualPipe：1F1B 调度注解

> 前置讲义：u1-l4（读懂单条事件：ph/cat/pid/tid/ts/dur 与元数据事件）、u2-l1（CPU 与 GPU 的关联：cuda_runtime、External id 与 ac2g）。
> 本讲假设你已经会用 `cat`/`ts`/`dur`/`args` 描述一条事件，并且知道 CPU 侧记录与 GPU 侧记录是异步的两份账。

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释 `ProfilerStep#1` 与 `1F1B` 两个「容器注解」的来源与含义。
2. 列出 train.json 中真实存在的全部 12 种 `user_annotation` 名称：2 个容器注解 + 10 类 stage 注解（attn/mlp × F/B/W，dispatch/combine × F/B），并说明 F/B/W 分别对应前向、反向数据梯度、反向权重梯度。
3. 从 `user_annotation` 的时间区间还原一个 chunk 内「先反后前」的调度顺序，并验证每个 chunk 恰好包含 4 个 MoE 层的重复模式。
4. 读懂 8 个 GPU 进程时间线与 CPU 注解的对应关系，理解 README 中「PP 通信未计入轨迹」「模拟绝对均衡 MoE 路由」这两个解读前提。

## 2. 前置知识

### 2.1 流水线并行与 1F1B 调度

大模型训练常把模型按层切成若干段，分布到多张卡上串行执行，这就是**流水线并行（Pipeline Parallelism, PP）**：第 0 张卡算前几层，把中间结果发给第 1 张卡……不同 micro-batch 像流水线上的工件一样依次流过各卡。

最朴素的调度是「先把所有 micro-batch 的前向算完，再一起算反向」（GPipe 式），反向要等全部前向结束，中间夹着一大段显存与时间上的浪费。**1F1B（one-forward-one-backward，一次前向一次反向）**是更紧凑的经典调度：热身阶段先做几个前向，之后每个 micro-batch 前向一完成就立刻做上一个 micro-batch 的反向，前后交替，让流水线两端都忙起来。

**DualPipe（双向流水线）**是 DeepSeek 在此之上的改进：把每个 chunk（一小段连续层）的前向和反向再各自拆成更细的阶段（stage）——注意力计算、MoE 计算、专家分发/聚合通信等——然后把「一个 forward chunk」和「一个 backward chunk」成对编排，让其中一个的**通信阶段**与另一个的**计算阶段**在时间上重叠，从而压缩流水线气泡。本讲要读的 train.json，正是这对 forward/backward chunk 交错执行时的 CPU 侧调度记录。

### 2.2 F / B / W 三个后缀

在本仓库和 DualPipe/DeepEP 生态的惯用记号里：

| 后缀 | 含义 | 说明 |
|------|------|------|
| `F` | forward | 前向计算 |
| `B` | backward（data gradients） | 反向传播中「对输入的梯度」部分 |
| `W` | backward for weights（weight gradients） | 反向传播中「对权重的梯度」部分 |

把 `B` 和 `W` 拆开是性能优化的重要手段：`B` 的输出要立刻被下游层用，时间上紧耦合；`W` 只在优化器更新时才需要，可以被延迟、甚至攒到通信空闲时再算。理解了这一点，你就能解释为什么轨迹里会出现 `mlp(B)` 与 `mlp(W)` 两个独立的注解。

### 2.3 两个解读前提（来自 README）

读这份数据前必须记住 README 交代的两件事：

- **采集时模拟了绝对均衡的 MoE 路由**（见 [README.md:L3](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/README.md#L3)）：真实 MoE 路由是不均衡的，而这里人为令每个专家收到的 token 数完全相同。所以你看到的时长是「理想负载」下的调度行为，不能直接外推到路由倾斜的真实场景。
- **训练轨迹只记录一对 forward/backward chunk 的重叠**，每个 chunk 含 4 个 MoE 层；并行配置为 EP64、TP1、4K 序列长度（与 DeepSeek-V3 预训练一致），且 **PP 的点对点通信未被计入轨迹**（见 [README.md:L11-L12](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/README.md#L11-L12)）。也就是说，轨迹里看不到「chunk 在流水线级间传送」的那部分通信，你能看到的 all-to-all 通信是 MoE 专家并行带来的 `dispatch`/`combine`。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [README.md](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/README.md) | 仓库说明：三份轨迹的场景、并行配置、重叠策略与均衡路由前提 |
| [train.json](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json) | 训练轨迹（EP64），本讲主角：42 条 `user_annotation` 事件 + 8 个 GPU 进程时间线 |
| [assets/train.jpg](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/assets/train.jpg) | README 内嵌的训练截图：CPU 注解轨道 + GPU 0~7 时间线的整体版式 |

本讲引用的 train.json 关键位置一览（行号均对应当前 HEAD `4496024`）：

| 内容 | 行号 |
|------|------|
| `ProfilerStep#1` 事件 | L14417-L14421 |
| `1F1B` 事件 | L14424-L14428 |
| 第一个 `combine(B)` | L14431-L14435 |
| 第一个 `attn(F)` | L14676-L14680 |
| 第二个 `dispatch(F)`（20ms 长注解） | L21375-L21379 |
| GPU 侧 `cached_notify` 内核（stream 27） | L31292-L31305 |
| GPU 侧 `internode::dispatch` 内核（stream 27） | L31324-L31331 |
| GPU 侧 `ln_fwd_kernel`（stream 7，计算流） | L31392-L31405 |
| 步末 `cudaDeviceSynchronize` | L97384-L97389 |
| `process_labels`：CPU / GPU 0 / GPU 7 | L97402-L97405 / L97420-L97423 / L97546-L97549 |
| `thread_name`：stream 7 / 23 / 27 | L97558-L97561 / L97570-L97573 / L97582-L97585 |
| 采集窗口 `Trace` 事件 | L97630 附近 |

## 4. 核心概念与源码讲解

### 4.1 DualPipe 与 1F1B 调度：两个容器注解

#### 4.1.1 概念说明

打开 train.json 的 `user_annotation` 事件（u1-l4 讲过：`cat="user_annotation"` 是 PyTorch 的 `record_function` 机制留下的用户自定义区间），你会发现它们不是散乱的，而是套在一个两层「容器」里：

```
ProfilerStep#1                      ← 外层：profiler 的一个调度步
└── 1F1B                            ← 内层：一次 1F1B 调度（DualPipe 编排的一对 chunk）
    ├── combine(B), attn(F), ...    ← 具体的 stage 注解（下一节）
```

- `ProfilerStep#1` 是 **PyTorch Profiler 自动生成**的注解：当使用 `schedule()` 采集时，每一步训练迭代会被包成一个 `ProfilerStep#N`。这里的 `#1` 说明本次采集窗口只覆盖了一步。
- `1F1B` 是**训练框架代码主动打的**注解（框架在进入 1F1B 调度循环处调用了 `record_function("1F1B")`），它框住了「这一步里实际执行 DualPipe 前反向交错调度的部分」。

两个容器的时长几乎相同：`ProfilerStep#1` 为 112202µs（约 112.2ms），`1F1B` 为 112168µs，只差 34µs 的进入/退出开销。这也印证了 u1-l2 里你在截图上看到的现象：CPU 轨道上最显眼的那条贯穿全程的注解就是 `1F1B`。

#### 4.1.2 核心流程

从容器注解还原本轨迹覆盖的内容：

1. Profiler 开始记录一步训练（`ProfilerStep#1` 起点 ts=1740461679470813）。
2. 框架进入 1F1B 调度，对一对 forward/backward chunk 做细粒度交错编排（`1F1B` 起点 ts=1740461679470833）。
3. 调度器依次进入各 stage 的 `record_function` 区间（下一节详解）。
4. 交错执行完毕，`1F1B` 结束（约 112.2ms 处）。
5. 步末做一次 `cudaDeviceSynchronize`（GPU 真正收工），随后 `ProfilerStep#1` 关闭。

#### 4.1.3 源码精读

`ProfilerStep#1` 事件（[train.json:L14417-L14421](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L14417-L14421)）：

```json
"ph": "X", "cat": "user_annotation", "name": "ProfilerStep#1", "pid": 1166, "tid": 1166,
"ts": 1740461679470813, "dur": 112202,
"args": {
  "External id": 4609,"Ev Idx": 2040
}
```

这条事件说明：CPU 主线程（pid=1166，即 u1-l4 认过的那个 `process_labels` 标为 "CPU" 的进程，主线程 TID=PID）从 1740461679470813µs 起花费 112202µs 完成了一步调度。注意 `args` 里只有 `External id` 和 `Ev Idx` 两个编号，没有任何张量信息——注解是纯时间标记。

紧随其后的 `1F1B` 事件（[train.json:L14424-L14428](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L14424-L14428)）：

```json
"ph": "X", "cat": "user_annotation", "name": "1F1B", "pid": 1166, "tid": 1166,
"ts": 1740461679470833, "dur": 112168,
```

步末的收尾同步（[train.json:L97384-L97389](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L97384-L97389)）：

```json
"ph": "X", "cat": "cuda_runtime", "name": "cudaDeviceSynchronize", "pid": 1166, "tid": 1166,
"ts": 1740461679583058, "dur": 17308,
"args": { "External id": 66619, "cbid": 165, "correlation": 66619 }
```

`1F1B` 注解结束于 1740461679583001，随后 CPU 花了 17308µs 做 `cudaDeviceSynchronize`——这段时间 CPU 什么都不做，纯粹等 GPU 把队列里剩余内核全部跑完。它是「CPU 账本」与「GPU 账本」在每步末尾的对账点（回顾 u2-l1 的异步模型）。

顺带一个有用的事实：整个 train.json 中时长 ≥ 10ms 的事件只有 7 个——`ProfilerStep#1`、`1F1B`、3 条后文的 `dispatch(F)`、上面这条 `cudaDeviceSynchronize`，以及末尾框住整个采集窗口的 `Trace` 事件（dur=129749µs，[train.json:L97630](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L97630)）。「全文件只有 7 个长事件」这个结论后面马上要用到。

#### 4.1.4 代码实践

**实践目标**：用脚本确认两个容器注解的嵌套关系与时长。

**操作步骤**（示例代码）：

```python
# extract_annotations.py —— 提取 train.json 全部 user_annotation 并按 ts 排序
import json

with open("train.json") as f:          # 文件约 3MB，可直接整体加载
    trace = json.load(f)

anns = [e for e in trace["traceEvents"] if e.get("cat") == "user_annotation"]
anns.sort(key=lambda e: e["ts"])        # u1-l3 讲过：事件按记录顺序存放，必须按 ts 重排

t0 = anns[0]["ts"]                      # 以 ProfilerStep#1 起点为时间零点
print(f"{'名称':<16}{'开始(ms)':>10}{'时长(µs)':>10}")
for a in anns[:6]:                      # 先看前 6 条
    print(f"{a['name']:<16}{(a['ts']-t0)/1e3:>10.3f}{a['dur']:>10}")
```

**需要观察的现象**：第一行是 `ProfilerStep#1`（0.000, 112202），第二行是 `1F1B`（0.020, 112168）——两个区间几乎重合，`1F1B` 完整落在 `ProfilerStep#1` 内部。

**预期结果**：基于当前 HEAD 的 train.json 实测，`user_annotation` 共 42 条；前两条如上，总时长约 112ms。若你的输出条数不是 42，请检查是否漏排序或误把其他 `cat` 计入。

#### 4.1.5 小练习与答案

**练习 1**：`ProfilerStep#1` 和 `1F1B` 都长约 112ms，但 `cudaDeviceSynchronize` 发生在 `1F1B` 结束之后。这说明「一步训练完成」的判据是 CPU 侧调度循环退出，还是 GPU 侧工作队列清空？

**答案**：是 GPU 侧工作队列清空。CPU 侧 `1F1B` 注解关闭只代表调度器把所有内核都提交完了（异步），CPU 还要再等 17.3ms 才确认 GPU 收工。这正是 u2-l1 讲的「提交 ≠ 执行」在整步尺度上的体现。

**练习 2**：为什么本轨迹里只有 `ProfilerStep#1`，没有 `#2`、`#3`？

**答案**：采集窗口只覆盖一步迭代（trace 尾部的 `Trace` 事件 dur=129749µs，略大于一步的 112ms，其余是启动/收尾开销）。PyTorch Profiler 按步编号生成 `ProfilerStep#N`，只录一步自然只有 `#1`。

### 4.2 attn/mlp/dispatch/combine × F/B/W：stage 注解体系

#### 4.2.1 概念说明

容器之下是 10 类 stage 注解。DualPipe 把 chunk 的前向/反向拆成 4 种 stage：

| stage | 对应的计算/通信 | 出现的后缀 |
|-------|----------------|-----------|
| `attn` | 注意力（MLA）计算 | F / B / W |
| `mlp` | MoE 专家计算（含路由、专家 GEMM） | F / B / W |
| `dispatch` | DeepEP all-to-all「去程」：把 token 发往各专家所在 GPU | F / B |
| `combine` | DeepEP all-to-all「回程」：把专家结果聚合回来 | F / B |

注意两点：

1. **`dispatch` 和 `combine` 没有 `W` 版本**——它们是纯通信原语，自身没有可训练参数，反向传播不需要为它们计算权重梯度。10 类 = 3×2 + 2×2。
2. 加上 `ProfilerStep#1` 和 `1F1B`，train.json 全部 12 种注解名称、42 条事件，构成：

\[
42 = \underbrace{1}_{\text{ProfilerStep\#1}} + \underbrace{1}_{\text{1F1B}} + \underbrace{4}_{\text{attn(F)}} + \underbrace{4}_{\text{attn(B)}} + \underbrace{4}_{\text{attn(W)}} + \underbrace{4}_{\text{mlp(F)}} + \cdots + \underbrace{4}_{\text{combine(B)}}
\]

每种 stage 注解恰好出现 **4 次**——这正对应 README 说的「每个 chunk 含 4 个 MoE 层」：调度器把同一套 stage 序列对着 4 个层重复执行了 4 遍。

#### 4.2.2 核心流程

把 42 条注解按 `ts` 排序后（t0 取 `ProfilerStep#1` 起点 1740461679470813），会看到一个清晰的、重复 4 遍的模式。第 1 遍（第一「层」）的完整序列：

| 序 | 注解 | 开始(ms) | 时长(µs) | 方向 |
|----|------|---------|---------|------|
| 1 | `combine(B)` | 0.100 | 626 | 反向·聚合 |
| 2 | `attn(F)` | 0.803 | 3282 | 前向·注意力 |
| 3 | `mlp(B)` | 4.149 | 826 | 反向·MoE |
| 4 | `dispatch(F)` | 5.004 | 2426 | 前向·分发 |
| 5 | `dispatch(B)` | 7.454 | 147 | 反向·分发 |
| 6 | `mlp(W)` | 7.612 | 302 | 反向·MoE 权重梯度 |
| 7 | `mlp(F)` | 7.942 | 700 | 前向·MoE |
| 8 | `attn(B)` | 8.683 | 4837 | 反向·注意力 |
| 9 | `attn(W)` | 13.536 | 111 | 反向·注意力权重梯度 |
| 10 | `combine(F)` | 13.673 | 180 | 前向·聚合 |

之后第 2 遍从 14.007ms 的 `combine(B)` 重新开始，第 3 遍从 45.463ms、第 4 遍从 78.075ms，直到 111.857ms 处最后一个 `combine(F)` 收尾。4 遍 × 10 条 = 40 条 stage 注解。

观察这个序列的「方向」列：B、F、B、F、B、B、F、B、B、F——**反向 stage 与前向 stage 交错出现，且以反向的 `combine(B)` 打头**。这就是「先反后前」：DualPipe 在这一对 chunk 里，让 backward chunk 的 stage 优先进入调度，forward chunk 的 stage 插在反向各阶段的缝隙里。直觉上，这是把「反向 chunk 刚产出的通信需求」和「前向 chunk 的计算」错开：当调度器在 `attn(F)` 里提交前向注意力计算时，GPU 通信流上正在飞的是此前提交的反向 dispatch/combine 报文（下一节用 GPU 侧数据证实这一点）。

另外注意各 stage 的时长特征（4 次采样的范围）：

| 注解 | 时长范围(µs) | 特征 |
|------|-------------|------|
| `attn(F)` | 3115 ~ 3282 | 前向注意力，约 3.1ms |
| `attn(B)` | 4697 ~ 4837 | 反向注意力约 4.7ms，比前向长约 50% |
| `attn(W)` | 104 ~ 117 | 很短 |
| `mlp(F)` | 700 ~ 761 | |
| `mlp(B)` | 823 ~ 845 | |
| `mlp(W)` | 302 ~ 322 | |
| `dispatch(F)` | **2426 / 20604 / 21678 / 23100** | 第 1 次与后 3 次相差近 10 倍 |
| `dispatch(B)` | 147 ~ 162 | |
| `combine(F)` | 178 ~ 193 | |
| `combine(B)` | 270 ~ 626 | |

有一个必须警惕的解读陷阱：**`user_annotation` 的 `dur` 是 CPU 侧 `record_function` 区间的墙钟时间，不等于 GPU 上该 stage 的计算/通信时长**。最典型的证据是 `dispatch(F)` 的第 2~4 次（20ms 上下）：我们检索过整个文件，这两万微秒的区间内**没有任何一条时长 ≥ 100µs 的子事件**——里面只有零星的微秒级 `aten::empty`、`aten::record_stream` 等小操作，其余大块时间 CPU 处于 PyTorch/aten 层记录不到的状态（DeepEP 的 C++ 扩展在内部轮询、等待跨节点数据）。真正占住 GPU 的时间要去 kernel 事件里看，那是下一讲 u2-l3 的内容。

#### 4.2.3 源码精读

第一个 `combine(B)`——整个 1F1B 里第一条 stage 注解（[train.json:L14431-L14435](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L14431-L14435)）：

```json
"ph": "X", "cat": "user_annotation", "name": "combine(B)", "pid": 1166, "tid": 1166,
"ts": 1740461679470913, "dur": 626,
"args": { "External id": 4611,"Ev Idx": 2042 }
```

它紧贴着 `1F1B` 起点（83µs 之后）出现，说明调度循环一进来就先做反向的 combine 收尾——「先反后前」的第一笔。

第一个 `attn(F)`（[train.json:L14676-L14680](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L14676-L14680)）：

```json
"ph": "X", "cat": "user_annotation", "name": "attn(F)", "pid": 1166, "tid": 1166,
"ts": 1740461679471616, "dur": 3282,
```

第 2 个 `dispatch(F)`，20ms 长注解的代表（[train.json:L21375-L21379](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L21375-L21379)）：

```json
"ph": "X", "cat": "user_annotation", "name": "dispatch(F)", "pid": 1166, "tid": 1166,
"ts": 1740461679489166, "dur": 20604,
```

它内部开头是这样的（[train.json:L21381-L21415](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L21381-L21415)，节选）：

```json
{ "ph": "X", "cat": "cpu_op", "name": "aten::empty", "ts": 1740461679489219, "dur": 8, ... },
{ "ph": "X", "cat": "cpu_op", "name": "aten::record_stream", "ts": 1740461679489256, "dur": 3, ... },
```

以及结尾处成串的 1µs 级 `aten::record_stream`（[train.json:L21780-L21860](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L21780-L21860)）。**20ms 里没有一个 ≥ 100µs 的子事件**——结合前文「全文件仅 7 个 ≥10ms 事件」的检索结论，可以确信这段墙钟时间的大头不在 PyTorch 能记录到的任何操作上，而是在 DeepEP 扩展内部的等待中。

#### 4.2.4 代码实践

**实践目标**：完整提取 42 条注解，按 ts 排序输出「名称/开始时间/时长」表格，数出 `dispatch(F)` 与 `combine(B)` 的次数，验证 4 层重复模式。

**操作步骤**（示例代码，接 4.1.4 的脚本继续写）：

```python
# count_annotations.py
import json
from collections import Counter

with open("train.json") as f:
    trace = json.load(f)

anns = [e for e in trace["traceEvents"] if e.get("cat") == "user_annotation"]
anns.sort(key=lambda e: e["ts"])
t0 = anns[0]["ts"]

# 1) 名称计数
counter = Counter(a["name"] for a in anns)
for name, cnt in sorted(counter.items()):
    print(f"{name:<16}{cnt}")

# 2) 找出「一轮」的边界：每轮以 combine(B) 开始、combine(F) 结束
rounds, cur = [], []
for a in anns:
    if a["name"] == "combine(B)" and cur:      # 新一轮开始，上一轮封存
        rounds.append(cur); cur = []
    cur.append(a)
rounds.append(cur)

print(f"\n共 {len(rounds)} 轮")
for i, r in enumerate(rounds, 1):
    seq = " -> ".join(a["name"] for a in r)
    span = (r[-1]["ts"] + r[-1]["dur"] - r[0]["ts"]) / 1e3
    print(f"第{i}轮 ({span:.3f} ms): {seq}")
```

**需要观察的现象**：

1. `Counter` 输出中，10 类 stage 注解每类恰好 4 次，加上 `ProfilerStep#1`、`1F1B` 各 1 次，总计 42。
2. 每一轮的序列都是同样的 10 个名字、同样以 `combine(B)` 开头 `combine(F)` 结尾。
3. `dispatch(F)` 的开始时间在第 1 轮约 5.0ms，第 2~4 轮分别在 18.4ms、49.8ms、82.4ms 附近，且时长跳到 20ms 以上。

**预期结果**：4 轮 × 10 条 stage 注解；`dispatch(F)` 计数为 4，`combine(B)` 计数为 4（其中第 1 条在 0.100ms、其余在每轮开头）。这组数字直接印证 README「Each chunk contains 4 MoE layers」。以上数值均为当前 HEAD 的 train.json 实测结果。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `attn(B)`（约 4.7ms）比 `attn(F)`（约 3.1ms）长，而 `attn(W)` 只有约 0.1ms？

**答案**：反向注意力除了重放前向的存取，还要计算对 Q/K/V 多个输入的梯度以及 softmax 的反向传播，工作量天然大于前向（实测比值约 4.78/3.21 ≈ 1.49）。`attn(W)` 短，是因为注意力的可训练参数很少（MLA 的投影矩阵部分在 `attn` stage 内的权重梯度只占少量 kernel 提交时间），且 W 梯度可以异步延迟执行，CPU 侧只需很短的提交时间。再次强调：注解时长是 CPU 墙钟，只宜做同层级的相对比较。

**练习 2**：第 1 个 `dispatch(F)` 只有 2426µs，后 3 个都在 20ms 以上。给出至少一个合理解释，并设计一个验证办法。

**答案**：合理解释（假设，待本地验证）：第 1 轮 dispatch 所需的数据/布局在本地已就绪，CPU 提交后很快返回；后 3 轮的 dispatch 需要等待跨节点 all-to-all 的对端数据或 notify 握手完成，DeepEP 在 CPU 侧阻塞轮询，把等待时间计入了注解墙钟。验证办法：用 u2-l1 的 correlation 方法把这 4 次 dispatch 关联到 GPU stream 27 上的 `internode::dispatch` 内核，比较 CPU 注解时长与 GPU 内核时长的差值——如果 GPU 内核只有几毫秒而 CPU 注解 20ms，差值即 CPU 等待；再对比 4 次的 `cached_notify`/`notify_dispatch` 内核出现时间即可定位等待点（这也是 u2-l3 的预习内容）。

**练习 3**：如果框架没有拆分 B 与 W，轨迹会发生什么变化？

**答案**：`mlp(B)` 与 `mlp(W)` 会合并成一个更长的 `mlp(B)` 注解，调度器失去「把 W 梯度挪到通信空隙执行」的自由度——这正是拆分 B/W 的意义：B 与下游紧耦合必须先算，W 可以错峰。在注解层面你就失去了观察这种错峰的窗口。

### 4.3 8 个 GPU 进程时间线的阅读：注解之下的真实执行

#### 4.3.1 概念说明

u1-l4 讲过，`ph:"M"` 的元数据事件把数字 pid/tid 翻译成可读轨道名。train.json 的 GPU 侧版式是：

- pid=1166 → 进程标签 "CPU"（[train.json:L97402-L97405](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L97402-L97405)）
- pid=0~7 → 进程标签 "GPU 0" ~ "GPU 7"（如 [train.json:L97420-L97423](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L97420-L97423) 的 "GPU 0" 与 [train.json:L97546-L97549](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L97546-L97549) 的 "GPU 7"）
- GPU 0 内部的 tid=7/23/27 → 线程名 "stream 7 "、"stream 23 "、"stream 27 "（[train.json:L97558-L97585](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L97558-L97585)，注意名字带尾随空格，脚本匹配要 `strip()`）

这就是你在 [assets/train.jpg](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/assets/train.jpg) 截图里看到的版式：最上面是 CPU 1166 的注解轨道，下面排着 GPU 0~7 八条进程轨道，GPU 0 展开后能看到 stream 7（密集的计算内核）与 stream 23/27（稀疏的短条）。

为什么是 8 个 GPU 进程？因为训练用了 8 卡节点（train.json 的 `deviceProperties` 记录了 8 块 H800，见 u1-l3），profiler 为每块卡建了一条轨道。但结合 u1-l2 的结论——**963 个 kernel 事件全部落在 GPU 0**，GPU 1~7 的轨道是空的。原因：这是 world_size=64 中 rank 0 的单进程视角（`distributedInfo` 的 rank=0），profiler 只记录本进程提交的内核；同时轨迹是 CPU 侧账本的镜像，其他 7 块卡上由别的 rank 驱动的工作不会出现在这里。所以「8 条 GPU 轨道」是**采集环境**的形状，不是「本 rank 用了 8 块卡」。

真正要看的是 GPU 0 内部的**流分工**：

- **stream 7（计算流）**：907 个内核，注意力、GEMM、layer_norm 等所有占 SM 的计算。
- **stream 27（通信流）**：DeepEP 的 dispatch/combine 通信内核，包括每层一次的 `internode::dispatch`——全文件恰好 4 个，与 `dispatch(F)` 注解 ×4 一一对应。

「计算内核在 stream 7、通信内核在 stream 27」就是通信-计算重叠在数据上的物理形态：**两条流可以同时占用 GPU 的不同资源并行推进**。

#### 4.3.2 核心流程

把第 1 轮的 CPU 注解与 GPU 两条流放到同一条时间轴上（t0 = `ProfilerStep#1` 起点，单位 ms）：

```
CPU 主线程（注解，串行）:
  combine(B)   attn(F)      mlp(B)  dispatch(F)  ...
  [0.10─0.73] [0.80─4.08] [4.15─4.98] [5.00─7.45] ...

GPU stream 27（通信流）:
  cached_notify        dispatch(内核)
  [0.63────1.64]       [1.64──────4.96]          ← 合计约 4.3ms

GPU stream 7（计算流）:
  ... ln_fwd_kernel [0.82─0.93] ... FlashAttn/GEMM 持续提交执行 ...
```

三个关键对齐关系：

1. **通信先行**：`cached_notify` 内核在 GPU 上 0.63ms 就开跑（ts=9471440），早于 CPU 进入 `attn(F)` 注解（0.80ms）。调度器先把反向 dispatch 的报文发出去，再回头做前向计算的提交。
2. **重叠区间**：GPU 通信区间 [0.63ms, 4.96ms] 与 `attn(F)` 注解区间 [0.80ms, 4.08ms] 几乎完全交叠。用区间交集可以量化（这也是 u3-l2 重叠率指标的雏形）：对两组区间集合 \( A=\{[a_i^{s},a_i^{e})\} \)、\( B=\{[b_j^{s},b_j^{e})\} \)，重叠总量为

\[
\text{overlap}(A,B)=\sum_{i}\sum_{j}\left|[a_i^{s},a_i^{e})\cap[b_j^{s},b_j^{e})\right|
\]

代入第 1 轮：`attn(F)` 区间整体落入通信区间内部，交集约等于 `attn(F)` 全长 3282µs——即前向注意力计算期间，all-to-all 通信一直在 GPU 上并行推进。
3. **注解是收尾**：CPU 侧 `dispatch(F)` 注解（5.00ms 起）反而是**在 GPU dispatch 内核（1.64~4.96ms）即将结束后**才进入的——里面装的是等待与结果处理，不是通信本体。

#### 4.3.3 源码精读

GPU 通信流上的第一个内核 `cached_notify`（[train.json:L31292-L31305](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L31292-L31305)）：

```json
"ph": "X", "cat": "kernel", "name": "void dpsk::ep::internode::cached_notify<false>(int, int, int, ...)",
"ts": 1740461679471440, "dur": 1008,
"args": {
  "queued": 0, "device": 0, "context": 1,
  "stream": 27, "correlation": 44493,
  "grid": [20, 1, 1], "block": [320, 1, 1],
  "est. achieved occupancy %": 2
}
```

注意 `pid: 0`（GPU 0）、`tid: 27`（stream 27），以及 `est. achieved occupancy %: 2`——通信内核只用了极少的 SM（grid 仅 20 个块），它主要是搬运数据、发 RDMA 指令，不追求算力占用。

紧接其后的 `internode::dispatch` 主内核（[train.json:L31324-L31331](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L31324-L31331)）：

```json
"ph": "X", "cat": "kernel", "name": "void dpsk::ep::internode::dispatch<false, 8, true, 8>(int4*, float*, ...)",
"ts": 1740461679472450, "dur": 3320,
"args": { ..., "stream": 27, "correlation": 44508, ... }
```

ts=9472450、dur=3320µs：与 `cached_notify`（结束于 9472448）几乎无缝衔接，两段合计覆盖 [9471440, 9475770]，恰好在 CPU 进入 `dispatch(F)` 注解（9475817）之前完成。

同一时间窗内计算流上的内核，例如 layer_norm 前向（[train.json:L31392-L31405](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/train.json#L31392-L31405)）：

```json
"ph": "X", "cat": "kernel", "name": "void layer_norm::ln_fwd_kernel<...7168u...>(layer_norm::FwdParams)",
"ts": 1740461679471821, "dur": 112,
"args": { ..., "stream": 7, "correlation": 44633, "est. achieved occupancy %": 19 }
```

`tid: 7` / `stream: 7`，ts=9471821 落在 `attn(F)` 注解区间 [9471616, 9474898] 内、也落在通信区间 [9471440, 9475770] 内——**同一毫秒里，stream 7 在算 layer_norm，stream 27 在跑 all-to-all**。模板参数里的 `7168u` 正是 DeepSeek-V3 的隐藏维度，可作为辨认这套内核的指纹之一。

#### 4.3.4 代码实践

**实践目标**：验证「4 个 `dispatch(F)` 注解 ↔ stream 27 上 4 个 `internode::dispatch` 内核」，并亲手算一次第 1 轮的通信-计算重叠。

**操作步骤**（示例代码）：

```python
# verify_overlap.py
import json

with open("train.json") as f:
    trace = json.load(f)
evs = trace["traceEvents"]

# 1) CPU 侧：dispatch(F) 注解
disp_ann = sorted((e for e in evs
                   if e.get("cat") == "user_annotation" and e["name"] == "dispatch(F)"),
                  key=lambda e: e["ts"])

# 2) GPU 侧：stream 27 上的 internode::dispatch 内核
disp_ker = sorted((e for e in evs
                   if e.get("cat") == "kernel"
                   and "dpsk::ep::internode::dispatch<" in e["name"]),
                  key=lambda e: e["ts"])

print(f"dispatch(F) 注解数 = {len(disp_ann)}, internode::dispatch 内核数 = {len(disp_ker)}")
for a, k in zip(disp_ann, disp_ker):
    print(f"注解 [{(a['ts']-disp_ann[0]['ts'])/1e3:8.3f}ms, dur={a['dur']:6d}µs]  "
          f"内核 [{(k['ts']-disp_ann[0]['ts'])/1e3:8.3f}ms, dur={k['dur']:5d}µs, stream={k['args']['stream']}]")

# 3) 第 1 轮重叠：attn(F) 注解区间 ∩ (cached_notify + dispatch 两个内核的并集)
attn = next(e for e in evs if e.get("cat") == "user_annotation" and e["name"] == "attn(F)")
comm = [e for e in evs if e.get("cat") == "kernel"
        and e["args"].get("stream") == 27
        and 1740461679470813 <= e["ts"] < 1740461679475817]   # 第 1 轮窗口
a0, a1 = attn["ts"], attn["ts"] + attn["dur"]
overlap = sum(max(0, min(a1, k["ts"] + k["dur"]) - max(a0, k["ts"])) for k in comm)
print(f"\nattn(F) 时长 {attn['dur']}µs，其中与通信流重叠 {overlap}µs "
      f"({overlap/attn['dur']:.1%})")
```

**需要观察的现象**：

1. 注解数与内核数都是 4，一一配对；但注意配对后**内核的 ts 普遍早于对应注解的 ts**（第 1 对：内核 1.64ms vs 注解 5.00ms）——提交在前、注解收尾在后。
2. 最后一行输出的重叠百分比应接近 100%。

**预期结果**：基于当前 HEAD 实测，4 个 `internode::dispatch<false, 8, true, 8>` 内核全部在 stream 27；第 1 轮 `attn(F)`（3282µs）与通信流内核（`cached_notify` 1008µs + `dispatch` 3320µs）的重叠约 3280µs，即约 100%。若你的窗口过滤条件不同（把 `per_token_cast_to_fp8` 等 dispatch 前置量化内核也算进通信），重叠数值不变但并集时长会变长。待本地验证：不同 Python/系统下的输出格式（如百分比位数）可能有细微差异，核心数值以你运行结果为准。

#### 4.3.5 小练习与答案

**练习 1**：GPU 1~7 的轨道为什么是空的？如果改为在 rank 5 上采集，轨迹会变成什么样？

**答案**：本轨迹是 world_size=64 中 rank 0 的单进程视角，profiler 只记录本进程经 CUDA 提交的活动，GPU 1~7 上由其他进程驱动的工作不可见；8 条 GPU 轨道只是本节点 8 块卡的「轨道模板」。若在 rank 5 采集，内容结构相同（同样的注解体系与流分工），但 kernel 事件会落在 rank 5 所在节点它可见的那块 GPU 上，时间戳整体平移。

**练习 2**：`cached_notify` 的 `est. achieved occupancy %` 只有 2，这说明什么？它是问题吗？

**答案**：占用率低说明该内核只用 20 个 block（grid [20,1,1]）、约 1.5 warp/SM，即几乎不占 SM 算力。这不是问题而是设计意图：通信内核的本职是搬运数据与驱动 RDMA，不是算数；把 SM 留给计算流正是「通信不抢算力」的体现（decode 阶段的低延迟实现更极端，RDMA 发出后 SM 全部释放，见 u2-l5）。

**练习 3**：截图 [assets/train.jpg](https://github.com/deepseek-ai/profile-data/blob/449602428a1b023acb8a505d4f34fef536535db6/assets/train.jpg) 里 GPU 0 展开的是 stream 7 和 stream 23，本讲却说通信在 stream 27。矛盾吗？

**答案**：不矛盾。截图展示的是用户当前缩放/折叠状态下的轨道（stream 27 可能被折叠或截取区间不同），而元数据事件（L97558-L97585）明确登记了 stream 7、23、27 三条线程轨道；`internode::dispatch` 等通信内核的 `args.stream` 实测为 27。以数据为准，截图只是观察入口。stream 23 上的活动可在 chrome://tracing 中展开自行查看。

## 5. 综合实践

**任务：绘制第 2 轮（第二个 MoE 层）的「CPU 注解 × GPU 双流」对照时间轴，并解释 20ms 的去向。**

步骤建议：

1. 用 4.2.4 的脚本定位第 2 轮的 10 条注解（`combine(B)` 起点 ts=1740461679484820 附近的那一轮，即相对时间 14.007ms 起）。
2. 筛选出时间窗 [第 2 轮开始, 第 2 轮结束] 内、`pid=0` 的全部 kernel 事件，按 `args.stream` 分成 stream 7 组与 stream 27 组。
3. 用文本各画一条时间轴（每格 2ms），分别标注：CPU 注解条、stream 27 通信内核条、stream 7 计算内核条。
4. 回答：`dispatch(F)` 注解的 20604µs 里，GPU 通信流真正活跃的时间大约是多少？其余时间 CPU 在做什么？把你的结论写成 3~5 句话。

评判标准（自查）：

- 时间轴上能看到 stream 7 与 stream 27 的条在时间上交叠；
- 你能区分「CPU 墙钟时间（注解 dur）」与「GPU 活动时间（kernel dur 之和）」两个口径；
- 对 20ms 缺口的解释落在「DeepEP 扩展内部等待/轮询不被 profiler 记录」这一机制上，而不是猜测为「GPU 在算 20ms」。

本任务完成后，你就把本讲的三个模块——容器注解、stage 体系、GPU 时间线阅读——串成了一条完整的证据链。

## 6. 本讲小结

- train.json 共 42 条 `user_annotation`：外层 `ProfilerStep#1`（112202µs）套住 `1F1B`（112168µs），之下是 10 类 stage 注解（attn/mlp × F/B/W + dispatch/combine × F/B），每种恰好 4 次，对应「每个 chunk 含 4 个 MoE 层」。
- 每轮 10 条注解的固定顺序 `combine(B) → attn(F) → mlp(B) → dispatch(F) → dispatch(B) → mlp(W) → mlp(F) → attn(B) → attn(W) → combine(F)`，反向与前向交错、以反向 combine 打头，是 DualPipe「先反后前」成对编排的直接记录。
- 注解的 `dur` 是 CPU 侧墙钟：`dispatch(F)` 的 20ms 区间内没有任何 ≥100µs 的子事件，大头是 DeepEP 扩展内部不被 profiler 记录的等待；GPU 真实活动要看 kernel 事件。
- 8 条 GPU 进程轨道是 8 卡节点的采集版式，实际内核全在 GPU 0；计算在 stream 7（907 个内核）、通信在 stream 27（4 个 `internode::dispatch` 与 `dispatch(F)` 注解一一对应）。
- 第 1 轮数据实证了重叠：通信区间 [9471440, 9475770] 与 `attn(F)` 区间几乎完全交叠，`cached_notify` 占用率仅 2%——通信先行发出、计算并行推进、注解只是 CPU 收尾。
- 两条解读前提贯穿始终：模拟绝对均衡 MoE 路由；PP 通信未计入轨迹。

## 7. 下一步学习建议

本讲只回答了「调度器在 CPU 上做了什么、GPU 双流的形状长什么样」，但刻意没展开 GPU 通信内核本身。下一讲 **u2-l3（训练/预填充轨迹中的 DeepEP 通信内核）** 将精读 `dpsk::ep::internode::` 家族：`dispatch` / `combine` / `notify_dispatch` / `cached_notify` / `get_dispatch_layout` 各自的签名特征、职责分工与耗时占比，并统计通信类内核占全部 GPU kernel 时间的比例——那才是量化「通信-计算重叠收益」的地方。建议先做完本讲综合实践再进入 u2-l3，因为你将需要复用「按注解区间筛选内核」的脚本技巧。后续 u3-l2 会把本讲 4.3.2 中的区间交集公式推广为系统性的重叠率与气泡指标。
