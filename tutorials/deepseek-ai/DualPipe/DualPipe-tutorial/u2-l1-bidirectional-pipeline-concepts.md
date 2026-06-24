# 双向流水线与微批次核心概念

## 1. 本讲目标

第 1 单元让你建立了 DualPipe 的全局认知（它是什么、怎么跑、目录与导出长什么样）。从本讲开始，我们进入**第 2 单元（公共基础设施）**，先把贯穿整个项目的几个核心概念讲透，再去看具体的通信层、切分工具和零气泡机制。本讲是这一单元的「概念地基」，不深入任何算法实现细节，只解决三件事：

1. 理解**微批次（micro-batch / chunk）切分**为什么能提高设备利用率，并能在源码里找到切分发生的位置。
2. 理解 DualPipe **双向流水线**的核心设计——为什么每个进程（rank）要持有**两个**对称的模块，以及这两个模块分别对应完整模型的哪一部分。
3. 结合 README 的调度图，看懂数据是如何从流水线**两端相向**流动的（forward 方向与 reverse 方向）。

读完本讲后，你应该能在不看答案的情况下，指着 `example_dualpipe.py` 里 `local_modules = nn.Sequential(stage, stage)` 这一构造，说清楚这两个 `stage` 分别映射到完整模型的第几号阶段，以及为什么要成对出现。

> 本讲**不**讲解 8 步调度的具体循环公式（那是第 3 单元 u3-l5 的内容），也**不**展开 `comm.py` / `WeightGradStore` 的实现（那是 u2-l2 / u2-l4 的内容）。本讲只建立「为什么这么设计」的直觉。

## 2. 前置知识

本讲假设你已读过 u1-l1～u1-l3，具备以下基础概念（已在前置讲义中建立）：

- **流水线并行（Pipeline Parallelism, PP）**：把一个深层模型沿「深度」切成若干**阶段（stage）**，每个 stage 放在一张 GPU 上；数据依次流过 stage 0 → stage 1 → … 完成一次前向，反向则倒着流回。
- **气泡（bubble）**：流水线在「灌水（fill）」与「排水（drain）」阶段存在设备空闲，这部分空闲就是气泡。u1-l1 给出了气泡公式：1F1B 为 \((PP-1)(F+B)\)，DualPipe 为 \((PP/2-1)(F\&B+B-3W)\)。
- **rank**：分布式进程的编号。在 DualPipe 示例里，进程数 = 流水线阶段数 `pp_size` = GPU 数，三者相等（见 u1-l2）。
- **F、B、W、F&B**：F = 一个前向 chunk 的执行时间，B = 一个完整反向 chunk，W = 「只算权重的反向」chunk，F&B = 一次前向与一次反向**重叠**后的执行时间。
- **包结构**：`dualpipe/dualpipe.py` 定义 `DualPipe` 引擎，`dualpipe/utils.py` 提供 `scatter`/`gather`/`WeightGradStore` 等工具（见 u1-l3 的依赖图）。

补充两个本讲会用到、但前面没专门强调的小概念：

- **张量切分（tensor_split）**：把一个大张量沿某个维度均分成若干小张量，是微批次切分的底层操作。PyTorch 的 `torch.tensor_split` 即使整除不净也能均分。
- **「镜像」对称**：若一个序列是 \(a_0, a_1, \dots, a_{n-1}\)，那么位置 \(i\) 与位置 \(n-1-i\) 就互为镜像。DualPipe 的两模块设计正是建立在这种镜像关系上。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
|------|------|-----------|
| `README.md` | 项目说明 | 调度图说明（微批次数、双向、重叠格子的含义） |
| `dualpipe/dualpipe.py` | DualPipe 引擎 | `module[phase]` 索引、`phase ^= is_in_second_half`、phase↔方向注释、首末 rank 的输入方向 |
| `dualpipe/utils.py` | 通用工具层 | `scatter` / `chunk_tensor` 如何把整批输入切成微批次列表 |
| `examples/example_dualpipe.py` | 示例脚本 | `full_modules` 构造、`local_modules = nn.Sequential(stage, stage)`、首末 rank 喂入 |

> 说明：本讲引用的源码行号基于当前 HEAD `030ce43`。`utils.py` 虽然不在本讲规格的 `source_files` 里列出，但「微批次 scatter 概念」必须借助其中的 `scatter` / `chunk_tensor` 才能讲清楚，故一并引用；这两个函数的**深入实现**留给 u2-l3。

## 4. 核心概念与源码讲解

### 4.1 微批次切分（micro-batch / chunk）与 `scatter`

#### 4.1.1 概念说明

如果每次只把**一整个 batch** 灌进流水线，会发生什么？stage 0 算完整个 batch 才能把它交给 stage 1，此时 stage 0 空闲、stage 1 才开始忙；等 stage 1 算完才轮到 stage 2……于是在「灌水」阶段，后面的 stage 全在干等。整批串行流过 P 个 stage，气泡占比很高。

解决办法是把一个 batch 切成若干**微批次（micro-batch）**，在 DualPipe 源码里也叫 **chunk**。切成微批次后，stage 0 算完第 1 个微批次就立刻把它交给 stage 1，自己马上开始算第 2 个微批次；于是 stage 0 和 stage 1 可以**同时**处理不同的微批次，流水线被「灌满」，设备利用率大幅提升。

直观地，微批次越多、流水线越满、相对气泡越小。设阶段数为 \(P\)、微批次数为 \(M\)、每个 chunk 在一个 stage 上耗时 \(t\)，则理想情况下总耗时约为 \((P-1+M)\,t\)（灌水 \(P-1\) 段 + 有效 \(M\) 段），气泡占比近似为：

\[
\text{bubble fraction} \;\approx\; \frac{P-1}{P-1+M}
\]

\(M\) 越大，分母越大，气泡越小。这正是为什么 DualPipe 示例里用了 20 个微批次（`num_chunks=20`），而不是 1 个。

#### 4.1.2 核心流程

把整批输入切成微批次，在 DualPipe 里由 `utils.py` 的 `scatter` 完成，它在 `DualPipe.step` 一开始被调用。流程是：

1. `step` 收到整批 `inputs`（首/末 rank 才有，中间 rank 为 `None`）和切分数 `num_chunks`。
2. 调用 `scatter(inputs, half_num_chunks, batch_dim)`，把整批沿 `batch_dim`（默认 0）切成 `half_num_chunks` 个微批次。
3. 返回值是一个「微批次列表」，每个元素是一个 tuple，装着该微批次在各输入张量上的切片。

> 注意切分数是 `half_num_chunks = num_chunks // 2` 而不是 `num_chunks`。原因正是「双向」：一半微批次走 forward 方向、另一半走 reverse 方向，每方向各 `num_chunks/2` 个。这部分在 4.3 再展开。

#### 4.1.3 源码精读

`scatter` 本体只做两件事：把每个输入张量沿指定维度切成 `chunks` 份，再用 `zip(*inputs)` 重组为「按微批次组织」的列表：

[dualpipe/utils.py:62-71](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/utils.py#L62-L71) —— `scatter` 先把单个张量包成单元素 tuple，再对每个输入调用 `chunk_tensor` 切片，最后 `zip(*inputs)` 把「每个张量的第 k 片」聚成第 k 个微批次。若输入全是 `None`，则返回 `chunks` 个空 tuple，保证下游循环次数正确。

底层切片由 `chunk_tensor` 完成，它处理了 `None` 的边界情况：

[dualpipe/utils.py:46-49](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/utils.py#L46-L49) —— `chunk_tensor` 用 `x.tensor_split(chunks, dim=dim)` 做均分；若 `x is None` 则返回 `chunks` 个 `None`，这就是为什么中间 rank（无输入）也能安全调用 `scatter`。

真正调用切分的地方在 `DualPipe.step` 的开头：

[dualpipe/dualpipe.py:345-346](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L345-L346) —— `inputs` 与 `labels` 都被 `scatter` 切成 `half_num_chunks` 个微批次。注意这里切的是**一半**，因为另一半微批次属于反方向（见 4.3）。

示例脚本里整批输入的规模也印证了微批次思想：

[examples/example_dualpipe.py:118-119](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipe.py#L118-L119) 与 [examples/example_dualpipe.py:131](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipe.py#L131) —— `num_chunks=20`、`micro_batch_size=3`，于是 `full_x` 的第 0 维是 `num_chunks * micro_batch_size = 60`，正好能切成 20 个微批次、每批 3 条样本。

#### 4.1.4 代码实践

**实践目标**：亲手验证 `scatter` 把整批输入切成「微批次列表」的过程与形状。

**操作步骤**（仅需 CPU 与 PyTorch，不需要 GPU/分布式）：

```python
# 示例代码（非项目原有，仅用于演示 scatter 行为）
import torch
from dualpipe.utils import scatter, gather

x = torch.arange(24).reshape(6, 4)          # 想象成 batch=6 的输入
micro = scatter(x, chunks=3, dim=0)         # 切成 3 个微批次
print(len(micro), micro[0].shape)           # 3, (2, 4)
print(torch.cat([m for (m,) in micro], 0))  # 拼回原样，应与 x 相同
```

**需要观察的现象**：

- `len(micro) == 3`，即得到 3 个微批次。
- 每个微批次第 0 维为 `6/3 = 2`。
- 把 3 个微批次沿 `dim=0` 拼回，结果与原始 `x` 完全一致（`torch.equal` 为真）。

**预期结果**：你看到 `3`、`(2, 4)`，且拼回的张量逐元素等于 `x`。这说明 `scatter` 是「无损」的按维均分。

**待本地验证**：上述命令需已安装 PyTorch 与 `dualpipe`（`pip install -e .`）。若未安装，可改为纯阅读 `utils.py:62-71` 理解逻辑。`gather` 的深入实现与 `None` 边界细节留给 u2-l3，本处不展开。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `scatter` 要把单个张量先包成 `(inputs,)` 再处理？

**参考答案**：为了让「单张量输入」和「多张量输入（tuple/list）」走同一条代码路径。统一成序列后，就能用 `zip(*inputs)` 把每个输入的第 k 片聚成第 k 个微批次，逻辑只写一遍。

**练习 2**：若把 `num_chunks` 从 20 调大到 40（总样本数不变、每批更小），对气泡有什么影响？

**参考答案**：根据 \( (P-1)/(P-1+M) \)，\(M\) 增大→气泡占比下降，设备更忙、利用率更高。代价是每个微批次更小、kernel 启动与通信的相对开销上升，存在一个收益递减的平衡点。DualPipe 选 20 是一个工程上合理的折中。

---

### 4.2 双向流水线：每个 rank 持有两个镜像模块

> 这是本讲的核心模块，也是整篇 DualPipe 最关键的「为什么」。

#### 4.2.1 概念说明

普通的流水线只有**一个方向**：数据从 stage 0 一路流到最后一个 stage。DualPipe 的核心创新叫**双向流水线（bidirectional pipeline）**：数据从流水线的**两端**相向喂入，于是有两条数据流：

- **forward 方向**：数据从 stage 0 → stage 1 → … → 最后一个 stage，方向「正」。
- **reverse 方向**：数据从最后一个 stage → … → stage 0，方向「反」。

两条流在同一个设备上**同时**进行：每个设备既要处理 forward 流的一个微批次，又要处理 reverse 流的一个微批次，并让这两次计算**重叠**（这就是 README 里 \(F\&B\) 的来源）。要支持这件事，每个设备上就得有**两个模块**：一个负责 forward 流所经过的那个 stage，另一个负责 reverse 流所经过的那个 stage。

那么这两个 stage 具体是完整模型的哪两段呢？答案是**镜像对称**的一对：

- 设备 `rank = r` 持有完整模型的 **stage `r`** 与 **stage `pp_size - 1 - r`**。

也就是说，rank 0 持有 stage 0 与最后一个 stage；rank 1 持有 stage 1 与倒数第二个 stage；以此类推。这样安排的直接效果是：

- **forward 流**在设备序列上从 rank 0 走到最后一个 rank，依次经过 stage 0, 1, 2, …；
- **reverse 流**也从 rank 0 走到最后一个 rank（同一物理拓扑），但依次经过的是最后一个 stage, …, stage 0（镜像）。

两条流复用同一套设备间的 P2P 通信链路，只是在「逻辑方向」上相反。这就是为什么 README 调度图里 forward 与 reverse 两个方向的微批次是**对称**的。

代价（u1-l1 已给出）是每个设备要存两个 stage 的参数，所以 **Parameter Per Device = 2×**；相应地，激活显存为 \(PP+1\)。这是用「显存」换「气泡」的工程取舍。

#### 4.2.2 核心流程

把上述设计落到代码上，分三步理解：

1. **构造完整模型**：示例先建一个有 `pp_size` 个 stage 的完整模型 `full_modules`。
2. **每个 rank 取镜像两段**：rank `r` 从完整模型里取出 stage `r` 与 stage `pp_size-1-r`，组装成本地的**两个**模块，传给 `DualPipe`。
3. **用 phase 索引两个模块**：引擎内部用 `self.module[phase]` 在两个模块间切换，`phase=0` 与 `phase=1` 分别对应两个方向；通过 `phase ^= is_in_second_half` 实现「前半 rank 与后半 rank 的方向定义互换」。

用伪代码表示第 2、3 步：

```
# 每个 rank 的本地组装（示例代码）
local_modules = nn.Sequential( stage_r, stage_(pp_size-1-r) )   # 两个镜像 stage
DualPipe(local_modules)                                          # 交给引擎

# 引擎内部：用 phase 选模块
phase ^= is_in_second_half          # 后半 rank 翻转 phase
outputs = self.module[phase](inputs)  # phase 0 / 1 分别走两个 stage
```

关于 phase 与方向的对应关系，引擎里有一句关键注释（下一节引用），概括成一张表：

| rank 所处位置 | phase 0 含义 | phase 1 含义 |
|--------------|-------------|-------------|
| 前半（rank < num_ranks/2） | forward 方向 | reverse 方向 |
| 后半（rank ≥ num_ranks/2） | reverse 方向 | forward 方向 |

也就是说，「同一个 phase 编号」在不同 rank 上可能代表相反的方向；这种统一编号是为了让 8 步调度能对所有 rank 写同一套循环。

#### 4.2.3 源码精读

先看示例脚本如何构造完整模型，再给每个 rank 取镜像两段——这是本模块最关键的两行：

[examples/example_dualpipe.py:128](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipe.py#L128) —— 构造完整模型：`nn.Sequential(*[PipelineStage(hidden_size) for _ in range(pp_size)])`，共 `pp_size` 个 stage，编号 0 ~ `pp_size-1`。这是「尚未切分到各设备」的完整模型。

[examples/example_dualpipe.py:138](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipe.py#L138) —— **本讲最重要的一行**：`local_full_modules = nn.Sequential(full_modules[rank], full_modules[pp_size - 1 - rank])`。rank `r` 取出 stage `r` 和 stage `pp_size-1-r`，正是一对**镜像** stage。

[examples/example_dualpipe.py:139-142](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipe.py#L139-L142) —— 新建两个独立的 `PipelineStage`，分别 `load_state_dict` 载入上面取出的两段权重，再 `DualPipe(local_modules)` 交给引擎。注意是「两份同结构的 stage」，这也是后面 `overlapped_forward_backward` 能启用的前提。

再看引擎如何接收并索引这两个模块：

[dualpipe/dualpipe.py:11-23](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L11-L23) —— `DualPipe.__init__` 的签名要求 `modules: Tuple[nn.Module, nn.Module]`（恰好两个模块），随后 `self.module = nn.ModuleList(modules)` 把它们存为可索引的列表；第 23 行还顺带检测「两个模块是否同类型、且类型上定义了 `overlapped_forward_backward`」，若是则启用前反向重叠（u3-l3 详述）。

真正「用 phase 选模块」发生在前向计算里：

[dualpipe/dualpipe.py:67-77](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L67-L77) —— `_forward_compute_chunk` 的核心：第 68 行 `phase ^= self.is_in_second_half` 做方向翻转，第 77 行 `outputs = self.module[phase](*inputs)` 用翻转后的 phase 在两个镜像 stage 间二选一。这正是「每个 rank 两个模块」在运行时的体现。

phase 与 forward/reverse 方向的正式定义，写在 `step` 里的一段注释中：

[dualpipe/dualpipe.py:355-356](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L355-L356) —— 明确说明：前半 rank 的 phase 0 = forward、phase 1 = reverse；后半 rank 反过来。结合上面的 `phase ^= is_in_second_half`，可知所有 rank 上「forward 方向」最终都落到 `self.module` 里负责 forward 的那个 stage 上。

最后，判定「前半 / 后半」的拓扑标志在 `__init__` 里一次性算好：

[dualpipe/dualpipe.py:42-45](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L42-L45) —— `is_in_second_half = self.rank >= self.num_ranks // 2` 决定该 rank 属于前半还是后半，直接驱动 phase 翻转；`is_middle_rank` 标记正好处于流水线中间、两个方向交汇处的两个 rank，它们在 8 步调度里有特殊处理（u3 详述）。

#### 4.2.4 代码实践（本讲主实践）

**实践目标**：用 `pp_size=8` 手算每个 rank 持有的两个 stage 编号，找出镜像对，并解释「为什么每个进程要持有两个 stage」。

**操作步骤**：

1. 阅读 [examples/example_dualpipe.py:128](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipe.py#L128) 与 [examples/example_dualpipe.py:138](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipe.py#L138)。
2. 取 `pp_size=8`，对 `rank = 0,1,…,7`，分别计算 `module[0] = full_modules[rank]` 与 `module[1] = full_modules[7-rank]` 对应的 stage 编号。
3. 圈出「持有完全相同一对 stage」的 rank 组合。

**参考答案（每个 rank 的两个 stage）**：

| rank | module[0] = stage `rank` | module[1] = stage `7-rank` | 镜像伙伴 |
|------|--------------------------|----------------------------|---------|
| 0 | stage 0 | stage 7 | rank 7 |
| 1 | stage 1 | stage 6 | rank 6 |
| 2 | stage 2 | stage 5 | rank 5 |
| 3 | stage 3 | stage 4 | rank 4 |
| 4 | stage 4 | stage 3 | rank 3 |
| 5 | stage 5 | stage 2 | rank 2 |
| 6 | stage 6 | stage 1 | rank 1 |
| 7 | stage 7 | stage 0 | rank 0 |

**需要观察的现象**：

- rank `r` 与 rank `7-r` 持有**完全相同**的两个 stage，只是 `module[0]` / `module[1]` 顺序互换。例如 rank 0 是 `(stage0, stage7)`，rank 7 是 `(stage7, stage0)`。
- 每个 stage 恰好被两个 rank 持有（如 stage 0 同时在 rank 0 与 rank 7 上）。

**预期结果 / 解释**：每个进程之所以要持有两个 stage，是因为它要同时参与**两条方向相反的数据流**：forward 流经过 stage `r`（在 rank `r` 上用 `module[0]`），reverse 流经过 stage `pp_size-1-r`（在同一 rank 上用 `module[1]`）。两个 stage 互为镜像，于是同一对物理设备（rank `r` 与 rank `pp_size-1-r`）就能既支撑正向计算、又支撑反向计算，且让二者重叠，从而把气泡压到 \((PP/2-1)(F\&B+B-3W)\)。

**待本地验证**：若你有 8 张 GPU，可运行 `python examples/example_dualpipe.py` 并在 [example_dualpipe.py:168-176](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipe.py#L168-L176) 的梯度校验里观察 `p1all[pp_size - 1 - rank]` 的累加——它正是把镜像伙伴的梯度补回来的证据。无 GPU 时本实践为纯阅读型，上表即结论。

#### 4.2.5 小练习与答案

**练习 1**：rank 3 与 rank 4 为什么叫「middle rank」？它们持有的 stage 有什么特别之处？

**参考答案**：`num_ranks=8` 时，`num_ranks//2 = 4`，所以 rank 3（`4-1`）与 rank 4 正好位于流水线正中间（[dualpipe.py:45](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L45)）。它们持有的分别是 `(stage3, stage4)` 与 `(stage4, stage3)`——恰好是流水线「前半最后一段」与「后半第一段」的交界，两条方向的数据流在此交汇，所以 8 步调度里对它们有特殊处理。

**练习 2**：如果某个 rank 的两个模块类型不同（比如一个是 `PipelineStage`、另一个是别的类），会怎样？

**参考答案**：[dualpipe.py:23](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L23) 会把 `overlapped_forward_backward` 检测为 `False`，于是引擎退化为「前向、反向分开算、不重叠」的保守模式（见 u3-l3）。要启用重叠，两个模块必须同类型且该类型定义了 `overlapped_forward_backward` 钩子。

---

### 4.3 看懂 README 调度图与双向数据流

#### 4.3.1 概念说明

光有「两个镜像模块」还不够，还得理解数据怎么从两端喂入、又怎么在图上体现为「对称的两条流」。README 给出的调度图就是这两条流的「时间表」：

- 横轴是时间，纵轴是 8 个 PP rank（设备）。
- 图里画了 **8 PP ranks、20 个微批次、两个方向**。
- **forward 方向**的微批次从一端喂入，**reverse 方向**的微批次从另一端喂入，两者关于图中线对称（README 因此省略了 reverse 方向的批次号）。
- 被**同一条黑色边框**圈住的两个格子，表示「一次前向与一次反向的执行被刻意重叠」，即 \(F\&B\)。

理解这张图的关键，是把「设备的物理排列（rank 0..7）」与「两条逻辑方向（forward / reverse）」分开看：同一行（同一设备）上往往同时有一个 forward 格子和一个 reverse 格子，它们就是 4.2 讲的「两个镜像模块」在同一时刻各算各的。

#### 4.3.2 核心流程

数据从两端喂入，在示例里是这样安排的：

1. **首 rank（rank 0）**：拿到**前半**输入 `full_x.chunk(2)[0]`（喂给 forward 方向），同时拿到**后半**标签 `full_l.chunk(2)[1]`（喂给 reverse 方向的损失）。
2. **末 rank（rank 7）**：拿到**后半**输入 `full_x.chunk(2)[1]`（喂给 reverse 方向），同时拿到**前半**标签 `full_l.chunk(2)[0]`（喂给 forward 方向的损失）。
3. **中间 rank**：不直接拿到输入/标签，只通过 P2P 接收上下游传来的微批次。
4. forward 流在末 rank 处算出 loss 并开始反向；reverse 流在首 rank 处算出 loss 并开始反向。两条反向流又各自回流，最终在首/末 rank 把 input_grad 算完。

#### 4.3.3 源码精读

README 对调度图的文字说明就在「Schedules」一节：

[README.md:5-13](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/README.md#L5-L13) —— 明确写了「8 PP ranks、20 micro-batches、two directions」，并指出 reverse 方向与 forward 对称（故省略批次号），以及「同一黑色边框圈住的两个格子 = 互相重叠的一次前向与一次反向」。这段文字是读懂配图 `images/dualpipe.png` 的钥匙。

示例脚本里「两端相向喂入」的实现：

[examples/example_dualpipe.py:145-153](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipe.py#L145-L153) —— 首 rank 取 `x = full_x.chunk(2)[0]`、`l = full_l.chunk(2)[1]`；末 rank 取 `x = full_x.chunk(2)[1]`、`l = full_l.chunk(2)[0]`；其余 rank 为 `None`。注意 `x` 与 `l` 取的是**不同**的一半——输入和标签分别属于相反的方向。

引擎把这两端输入分别导向两条流：

[dualpipe/dualpipe.py:347-353](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L347-L353) —— 首 rank 把输入放进 `input_chunks` 的 phase 0（forward）、标签放进 phase 1（reverse）；末 rank 则把输入放进 phase 1、标签放进 phase 0。配合 [dualpipe/dualpipe.py:355-356](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L355-L356) 的方向定义，就形成了「输入从两端进、loss 在对端算」的 V 字形双向数据流。

「重叠」在代码里的落点是 `_forward_backward_compute_chunk`：

[dualpipe/dualpipe.py:121](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L121) 与 [dualpipe/dualpipe.py:168-171](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L168-L171) —— 把一次 forward（`module0`）与一次 backward（`module1`）打包交给用户的 `overlapped_forward_backward` 钩子同时处理，这正是调度图里「同一边框两个格子」所对应的 \(F\&B\) 重叠操作。详细机制留给 u3-l3。

#### 4.3.4 代码实践

**实践目标**：把调度图上的「两个方向」与示例代码的输入分配对应起来。

**操作步骤**：

1. 打开 README，找到 `images/dualpipe.png` 配图（或直接看 [README.md:5-13](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/README.md#L5-L13) 的文字）。
2. 在图上找到首 rank（最上面一行）与末 rank（最下面一行），观察微批次是从这两端分别「进入」的。
3. 对照 [examples/example_dualpipe.py:145-153](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipe.py#L145-L153)，确认「首 rank 拿前半输入、末 rank 拿后半输入」。
4. 在图上找一对被同一条黑色边框圈住的格子，理解它们就是 [dualpipe.py:168-171](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L168-L171) 里同时处理的前向+反向。

**需要观察的现象**：

- 图的上边与下边都有微批次「进入」，呈对称形态。
- 中间几行（rank）上，同一时刻既有 forward 格子又有 reverse 格子。

**预期结果**：你能指着图说出「这一行是 rank r，它同时在算 forward 流的某个微批次（走 stage r）和 reverse 流的某个微批次（走 stage 7-r）」，把 4.2 的两模块设计与 4.3 的数据流图对应起来。

**待本地验证**：配图为静态资源，无需运行即可阅读；若想动态验证数据走向，需多 GPU 环境运行示例并加日志，本实践以图码对照为主。

#### 4.3.5 小练习与答案

**练习 1**：为什么首 rank 拿的 `x`（输入）与 `l`（标签）是 `full_x` / `full_l` 的**不同**一半？

**参考答案**：因为首 rank 的输入喂给 **forward 方向**，而它手里的标签配的是 **reverse 方向**的损失——reverse 方向的输入其实是从末 rank 进来的（`full_x.chunk(2)[1]`）。所以首 rank 的 `x` 与 `l` 属于两条不同的流，自然取不同的一半。这正对应 [dualpipe.py:347-353](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L347-L353) 里 input 与 labels 被放进不同 phase。

**练习 2**：调度图里 forward 与 reverse 为什么是「对称」的？省略 reverse 的批次号会丢失信息吗？

**参考答案**：因为每个 rank 持有的是镜像 stage 对（stage r 与 stage 7-r），两条方向的数据流走的是同一套设备、同样的微批次切分，只是方向相反，所以图上必然对称。reverse 的批次号只是 forward 批次号的镜像，省略它不丢任何调度信息——README 因此为了简洁而省略（[README.md:10-12](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/README.md#L10-L12)）。

## 5. 综合实践

**任务**：把本讲三个模块串起来，画出一张「`pp_size=8` 时 DualPipe 的设备—stage—方向」对应表，并配上数据流说明。

请整理出以下内容（文本表格或示意图均可）：

1. **设备 ↔ stage 映射**：列出 rank 0..7 各自持有的 `module[0]` 与 `module[1]` 对应的 stage 编号（直接用 4.2.4 的结论）。
2. **方向标注**：对每个 rank，标出 `module[0]` / `module[1]` 中哪个走 forward、哪个走 reverse（提示：前半 rank 的 `module[0]` 走 forward；后半 rank 因 `phase ^= is_in_second_half` 而 `module[0]` 走 reverse）。
3. **数据流**：用箭头画出 forward 流（从首 rank 的输入到末 rank 的 loss）与 reverse 流（从末 rank 的输入到首 rank 的 loss），并指出二者在哪里被重叠成 \(F\&B\)。

**验收标准**：

- 你能回答：「rank 2 上的 `module[1]` 对应完整模型的第几号 stage？属于哪个方向？」（答：stage `7-2=5`；rank 2 在前半，`module[1]` 对应 phase 1 = reverse 方向。）
- 你能回答：「为什么每个设备参数量是普通流水线的 2 倍？」（答：因为每个设备持有两个镜像 stage 的参数，见 4.2.1。）
- 你能回答：「首 rank 与末 rank 的输入分别喂给了哪个方向？」（答：首 rank 输入→forward，末 rank 输入→reverse。）

> 待本地验证：若已装好多 GPU 环境，可运行 `python examples/example_dualpipe.py`，对照其打印的 `pp_size=, num_chunks=, ...` 与上表自查；否则本实践为源码阅读型，结论即上述表格。

## 6. 本讲小结

- **微批次（chunk）切分**通过把一个 batch 拆成多片依次灌入流水线，显著降低「灌水/排水」阶段的相对气泡，气泡占比近似为 \((P-1)/(P-1+M)\)；DualPipe 用 `utils.py` 的 `scatter` 完成切分，并在 `step` 开头把整批切成 `num_chunks/2` 份。
- DualPipe 的核心是**双向流水线**：数据从两端相向喂入，形成 forward 与 reverse 两条对称的数据流。
- 每个进程持有**两个镜像 stage**：rank `r` 持有 stage `r` 与 stage `pp_size-1-r`，分别服务于 forward 与 reverse 两条流；代价是参数量 2×、激活显存 \(PP+1\)。
- 引擎用 `self.module[phase]` 在两个 stage 间切换，并以 `phase ^= is_in_second_half` 实现「前半 rank 与后半 rank 方向定义互换」，从而让 8 步调度对所有 rank 写同一套循环。
- 首 rank 拿前半输入（forward）、末 rank 拿后半输入（reverse），输入与标签分属不同方向，loss 在对端计算，形成 V 字形双向数据流。
- README 调度图中「同一黑色边框圈住的两个格子」即一次前向与一次反向的 \(F\&B\) 重叠，对应代码里的 `_forward_backward_compute_chunk`。

## 7. 下一步学习建议

本讲建立了「双向 + 微批次 + 两模块」的概念地基。接下来请按第 2 单元的顺序，进入这些概念对应的**具体实现**：

1. **u2-l2 通信层 comm.py**：双向数据流靠设备间 P2P 收发实现，去读 `append_irecv` / `append_isend` 与 `set_p2p_tensor_shapes`，理解微批次是怎么在 rank 之间传递的。
2. **u2-l3 scatter/gather**：回到 `utils.py`，深入本讲只用了一半的 `scatter` / `gather` / `chunk_tensor` / `cat_tensor`，理解微批次切分与聚合的完整细节（含 `None` 边界）。
3. **u2-l4 WeightGradStore**：本讲多次提到「重叠」与 \(F\&B\)，而零气泡 \(W\) 的延迟计算正是靠 `WeightGradStore`，去理解它如何把权重梯度缓存并重放以填充气泡。
4. 读完第 2 单元后，再进入**第 3 单元**逐层剖析 `dualpipe.py` 引擎，把本讲的概念落到 8 步调度的每一行。

一句话：本讲讲清了「为什么每个 rank 要两个 stage、数据为何双向流动」，下一讲开始看「这些数据具体是怎么在卡间传过去的」。
