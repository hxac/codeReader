# 状态管理与计算原语

> 讲义 id：`u3-l2`　|　所属单元：u3 DualPipe 引擎剖析　|　依赖：`u3-l1`

## 1. 本讲目标

上一篇 [u3-l1](u3-l1-dualpipe-init-rank-topology.md) 讲清楚了 `DualPipe.__init__` 如何确定「我是谁、我的邻居是谁、我在流水线的哪一半」。本讲顺着往下走，进入 `step()` 真正开始执行前的最后一道准备——**状态初始化**，以及引擎跑起来后反复调用的**两个计算原语**。

学完本讲你应该能够：

- 画出 `_reset_states` 中 `input_chunks / output_chunks / output_grad_chunks / input_grad_chunks` 这四类缓冲的二维结构 `[phase][chunk_id]`，并说出每一类装的是什么。
- 说清 `current_f_chunk_id`、`current_b_chunk_id` 等 6 个计数器「先读后自增」的取用方式，以及为什么计算 / 发送 / 接收要各用一套游标。
- 区分 `_forward_compute_chunk` 与 `_backward_compute_chunk` 的执行逻辑，尤其是 **last stage 用 `loss.backward()`、中间 stage 用 `run_backward(outputs, output_grads)`** 这条关键分叉，并理解一个 chunk 的张量如何被「取出 → 用完 → 置 `None`」地走完一生。

本讲只盯住「状态 + 两个原语」这三块，**不**展开 8 步调度（u3-l5）、通信原语（u3-l4）、前反向重叠钩子（u3-l3）。

## 2. 前置知识

本讲默认你已经掌握下列概念（来自前置讲义，这里只做一句话提醒，不重复展开）：

- **两个镜像模块**：每个进程通过 `self.module = nn.ModuleList(modules)` 持有两个 stage，`self.module[phase]` 按 phase 二选一（u3-l1）。
- **方向翻转技巧**：`phase ^= self.is_in_second_half`，让前后半 rank 的方向定义互换，使同一套调度代码对所有人成立（u3-l1 / u2-l1）。
- **拓扑标志位**：`is_first_rank / is_last_rank / is_middle_rank / is_in_second_half`，全部在 `__init__` 里算好（u3-l1）。
- **微批次（chunk）**：一个 batch 被切成 `num_chunks` 份，每个 rank 在每个方向上只处理其中一半（`half_num_chunks` 份），用 `chunk_id` 索引（u2-l1 / u2-l3）。
- **WeightGradStore 零气泡开关**：全静态类，`enabled` 开关控制权重梯度（W）是立即算还是攒进队列稍后算（u2-l4）。
- **scatter/gather**：`step()` 开头用 `scatter` 把首/末 rank 的输入切成微批次列表（u2-l3）。

还有两个在 `step()` 入口刚设置、本讲会反复用到的标志：

[dualpipe/dualpipe.py:327-328](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L327-L328) 设置 `self.forward_only = not torch.is_grad_enabled()`（是否处于推理/`no_grad` 模式）与 `self.return_outputs = return_outputs`（首末 rank 是否要把最终输出返回给调用方）。这两个标志决定了下面原语里很多「是否释放」「是否保存输出」的分支。

---

## 3. 本讲源码地图

本讲几乎全部围绕一个文件：

| 文件 | 作用 | 本讲用到的方法 |
|---|---|---|
| `dualpipe/dualpipe.py` | DualPipe 引擎主体 | `_reset_states`、`_forward_compute_chunk`、`_backward_compute_chunk`（外加 `step()` 中调用它们的上下文） |
| `dualpipe/utils.py` | 工具与零气泡存储 | `run_backward`（被反向原语调用）、`WeightGradStore`（u2-l4 已讲，本讲只复用其 `enabled/flush/clear` 接口） |

`run_backward` 和 `WeightGradStore` 的内部实现已在 [u2-l4](u2-l4-weightgradstore-zero-bubble.md) 讲过，本讲把它们当作「现成的工具」使用，不再重复。

---

## 4. 核心概念与源码讲解

### 4.1 `_reset_states`：八步调度的状态地基

#### 4.1.1 概念说明

DualPipe 的 `step()` 是一个**有状态的长流程**：它要在 8 个步骤里反复前向、反向、收发数据，且**前后步骤共享同一批中间张量**——比如第 1 步前向算出的输出，要到第 4 步才被反向消费。

引擎需要一个地方统一登记这些中间张量与进度。`_reset_states` 就是这个「状态登记表」的初始化函数。它做两件事：

1. **清空上一轮残留**：调用 `WeightGradStore.clear()` 把零气泡队列清干净，保证每一步训练从干净状态开始。
2. **建立四类张量缓冲 + 一组进度计数器**：四类缓冲按 `[phase][chunk_id]` 二维组织，计数器记录「下一个该处理第几号 chunk」。

它在 `step()` 里被调用**两次**：开头一次（开始本步训练）和结尾一次（收尾清理）。

[dualpipe/dualpipe.py:343](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L343) 是开头的调用；[dualpipe/dualpipe.py:438](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L438) 是结尾的调用。

#### 4.1.2 核心流程

`_reset_states` 的流程可以拆成三段：

```
① 清零气泡队列
   WeightGradStore.clear()

② 建四类「二维」张量缓冲（外层按 phase 分两格，内层是按 chunk_id 增长的列表）
   input_chunks       = ([], [])   # 前向：喂给 module 的输入
   output_chunks      = ([], [])   # 前向：module 吐出的输出
   output_grad_chunks = ([], [])   # 反向：下游回传的「对输出的梯度」
   input_grad_chunks  = ([], [])   # 反向：算出的「对输入的梯度」，要发给上游
   labels = None                    # 首末 rank 的标签（按 phase 组织）
   loss_chunks = []                 # 各 chunk 的 loss（一维列表）
   criterion = None                 # 损失函数

③ 建 6 组进度计数器（每组都是 [phase0, phase1] 两格，初值 0）
   current_f_chunk_id       # 前向「计算」游标
   current_b_chunk_id       # 反向「计算」游标
   current_send_f_chunk_id  # 前向「发送」游标
   current_send_b_chunk_id  # 反向「发送」游标
   current_recv_f_chunk_id  # 前向「接收」游标
   current_recv_b_chunk_id  # 反向「接收」游标
   comm_ops = []            # 攒 P2P 通信请求的累积列表
   to_free  = []            # 通信完成后要回收显存的张量
```

这四类缓冲的关键直觉是：**一个 chunk 的张量有四个生命阶段，正好对应四类缓冲**：

```
input_chunks[p][c]         前向的原料 ─┐
                                      ├─ _forward_compute_chunk 消费
output_chunks[p][c]        前向的产物 ─┘
output_grad_chunks[p][c]   下游回传的梯度 ─┐
                                          ├─ _backward_compute_chunk 消费
input_grad_chunks[p][c]    算出的输入梯度 ─┘
```

每一类都是「外层 phase（长度 2）+ 内层 chunk_id（长度随处理进度增长）」的二维结构。所谓「二维」就是：**第一维选方向，第二维选微批次**。

#### 4.1.3 源码精读

先看清零与四类缓冲的声明：

[dualpipe/dualpipe.py:47-56](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L47-L56) —— 先 `WeightGradStore.clear()` 清队列，再声明四类缓冲。注意每个缓冲都是 `([], [])` 这个两元素 tuple：外层两格对应 phase 0 / phase 1，内层 `[]` 会在后续 `append` 中按 chunk_id 顺序增长。`labels` 与 `criterion` 暂置 `None`，由 `step()` 在首末 rank 上另行填入。

再看 6 组计数器与两个列表：

[dualpipe/dualpipe.py:58-65](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L58-L65) —— 每个计数器都是 `[0, 0]`（phase0 从 0、phase1 从 0）。`comm_ops` 和 `to_free` 分别是「攒通信请求」和「待回收张量」的累积列表（具体用法见 u3-l4，本讲只需知道它们在此初始化）。

补充一点 `step()` 如何在 `_reset_states()` 之后**填入真实数据**：

[dualpipe/dualpipe.py:343-353](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L343-L353) —— 先 `scatter` 把整批输入/标签切成 `half_num_chunks` 个微批次；首 rank 把输入放进 `input_chunks` 的 phase0 槽（`inputs` 是一个按 chunk_id 排好的列表）、标签放进 phase1 槽；末 rank 反过来。中间 rank 不在这里填 `input_chunks`，它们的输入由后面的 `_recv_forward` 一边接收一边 `append`。所以**无论首末还是中间 rank，最终 `input_chunks[phase]` 都是一个按 chunk_id 索引的列表**，只是填充方式不同。

#### 4.1.4 代码实践

**实践目标**：用纯 Python 复刻 `_reset_states` 建立的二维缓冲结构，亲手感受「外层 phase、内层 chunk_id」的形状。

下面是**示例代码（非项目代码，无需 GPU 即可运行）**，用一个普通类模拟引擎的状态：

```python
# 示例代码：仅用于理解二维缓冲结构，不是 DualPipe 源码
class MockEngine:
    def _reset_states(self):
        self.input_chunks       = ([], [])   # [phase][chunk_id] -> [tensor, ...]
        self.output_chunks      = ([], [])
        self.output_grad_chunks = ([], [])
        self.input_grad_chunks  = ([], [])
        self.current_f_chunk_id = [0, 0]
        self.current_b_chunk_id = [0, 0]

eng = MockEngine()
eng._reset_states()
# 模拟「phase0 的第 0 号 chunk 输入已就位」
eng.input_chunks[0].append(["in0_chunk0_a", "in0_chunk0_b"])
eng.input_chunks[0].append(["in0_chunk1_a", "in0_chunk1_b"])
print(eng.input_chunks[0][1])   # 取 phase0 的第 1 号 chunk -> ['in0_chunk1_a', 'in0_chunk1_b']
print(eng.current_f_chunk_id)   # [0, 0]
```

**操作步骤**：

1. 把上面的代码存成 `mock_state.py`，用 `python mock_state.py` 运行。
2. 在最后再 `append` 一项，观察 `eng.input_chunks[0]` 的长度如何随 chunk 增长。
3. 试着用 `eng.input_chunks[1]`（phase1）也 `append` 一项，体会两个方向相互独立。

**需要观察的现象**：`input_chunks[0]` 是一个**列表**，下标就是 `chunk_id`；外层 tuple 的下标 `0/1` 才是 phase。

**预期结果**：`print(eng.input_chunks[0][1])` 打印 `['in0_chunk1_a', 'in0_chunk1_b']`，说明 `[phase][chunk_id]` 两层下标先选方向、再选微批次。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `input/output/input_grad/output_grad` 四类缓冲都设计成两元素 tuple，而不是一个一维列表？
**答**：外层两格对应两个 phase（方向）：phase0 与 phase1。前向数据与反向数据、以及两个方向各自的处理进度必须分开存放，否则会互相覆盖。一层列表无法同时区分「方向」和「微批次」两个维度。

**练习 2**：`current_f_chunk_id`（前向计算游标）和 `current_send_f_chunk_id`（前向发送游标）为什么不合并成一个计数器？
**答**：因为「计算」「发送」「接收」在 8 步调度里发生在不同时刻——同一个 chunk 可能先被算出来、过几步才发出去；也可能先收到、过几步才算。三者进度不同步，必须各用一套独立游标，否则会取错 chunk。

**练习 3**：`_reset_states` 在一次 `step()` 里被调用两次（开头和结尾），结尾那次为什么必要？
**答**：结尾那次用于收尾清理——释放本步残留的张量引用、把状态恢复成干净初值，避免下一步训练读到上一步的脏数据或泄漏显存。同时 `loss` 与 `outputs` 在调用结尾 `_reset_states()` 之前已被取出（[dualpipe/dualpipe.py:429-440](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L429-L440)），所以清理不会丢结果。

---

### 4.2 `_forward_compute_chunk`：前向计算原语

#### 4.2.1 概念说明

调度器每次「想让某个方向前进一步」，就调用一次 `_forward_compute_chunk(phase)`。它的工作非常聚焦：**取出该方向下一个待算 chunk 的输入 → 跑一遍 `self.module[phase]` → 把输出（或 loss）放回缓冲**。

它要处理三种特殊情况：

- **推理模式**（`forward_only`）：输入用完即释放，因为不反传。
- **终点 stage**（`is_last_stage`）：这里要算 loss 而不是普通输出。
- **需要返回输出**（`return_outputs`）：即使不是终点，也要把输出留下来交给调用方。

#### 4.2.2 核心流程

```
_forward_compute_chunk(phase):
    phase ^= is_in_second_half            # 后半 rank 翻转方向定义
    chunk_id = current_f_chunk_id[phase]  # 取当前要算的 chunk 号
    current_f_chunk_id[phase] += 1        # 游标前进（先读后自增）

    inputs = input_chunks[phase][chunk_id]
    if forward_only:
        input_chunks[phase][chunk_id] = None   # 推理：立即释放输入

    is_last_stage = (is_first_rank and phase==1) or (is_last_rank and phase==0)

    outputs = module[phase](*inputs)           # 真正的前向
    outputs = [outputs] if 是单个 Tensor else outputs   # 统一成列表

    if is_last_stage and criterion is not None:
        labels = labels[phase][chunk_id]
        loss = criterion(*outputs, *labels)
        loss_chunks.append(loss)               # 终点：存 loss

    if (not is_last_stage) or return_outputs:
        output_chunks[phase].append(outputs)   # 非终点或需返回：存输出
```

注意两点：① 「先读后自增」的游标用法——先取出当前值，再 `+1`，保证下次调用自然落到下一个 chunk；② 输出用 `append` 进 `output_chunks`，所以 `output_chunks[phase]` 的第 `c` 个元素正好对应 `chunk_id == c`，与反向取用对齐。

`is_last_stage` 的判定值得专门记一下：

\[ \text{is\_last\_stage} = (\text{is\_first\_rank} \wedge \text{phase}=1) \vee (\text{is\_last\_rank} \wedge \text{phase}=0) \]

双向流水线有**两个**终点：首 rank 的 phase1（反向方向的数据一路从末 rank 走回首 rank）和末 rank 的 phase0（前向方向的数据一路从首 rank 走至末 rank）。只有走到这两个终点，才会算 loss。这正是 u2-l1 所说「输入与标签分属不同方向、loss 在对端计算」的代码体现。

#### 4.2.3 源码精读

逐段看 [dualpipe/dualpipe.py:67-85](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L67-L85)：

[dualpipe/dualpipe.py:68-73](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L68-L73) —— 方向翻转、取出 `chunk_id` 并自增游标、取出输入；推理模式下把输入槽置 `None` 释放显存。

[dualpipe/dualpipe.py:75](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L75) —— 计算 `is_last_stage` 标志（公式见上）。

[dualpipe/dualpipe.py:77-82](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L77-L82) —— 跑前向 `self.module[phase](*inputs)`；把单个 `Tensor` 包成列表；若是终点且有 `criterion`，则 `criterion(*outputs, *labels)` 算 loss 并 `append` 进 `loss_chunks`。

[dualpipe/dualpipe.py:84-85](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L84-L85) —— 非终点（输出要留给反向消费）或需要返回输出时，才把输出 `append` 进 `output_chunks`。这意味着**终点 stage 且不要求返回输出时，普通输出不会被保存**——终点只关心 loss。

#### 4.2.4 代码实践

**实践目标**：阅读 [dualpipe/dualpipe.py:67-85](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L67-L85)，在纸上推演一次 `_forward_compute_chunk` 调用。

**操作步骤**：

1. 假设当前是「末 rank、前向（外层传入 `phase=0`，且末 rank 不在 second half，故翻转后仍 `phase=0`）」，`current_f_chunk_id[0]` 当前为 `2`。
2. 逐行走代码：`chunk_id` 取到几？游标变成几？`is_last_stage` 是 True 还是 False？
3. 接着看：会走 `loss_chunks.append` 分支，还是 `output_chunks[phase].append` 分支，还是都走？

**需要观察的现象**：末 rank 的 phase0 是终点，`is_last_stage=True`；若提供了 `criterion`，会算 loss。

**预期结果**：`chunk_id=2`，游标变为 `3`，`is_last_stage=True`。若 `criterion is not None`，执行 `loss = criterion(*outputs, *labels)` 并 `loss_chunks.append(loss)`；并且因为 `is_last_stage` 为真，第 84 行的 `output_chunks` 分支**只有**在 `return_outputs=True` 时才会执行。这是源码阅读可确定的逻辑，实际在多卡上跑需待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`forward_only` 为真时，为什么把 `input_chunks[phase][chunk_id]` 置 `None`？
**答**：推理模式下不会反传，输入张量用完即无用，置 `None` 可立即释放显存，避免在 8 步调度中越攒越多。

**练习 2**：一个 chunk 的输出，在什么条件下会真正进入 `output_chunks`？
**答**：当「不是终点 stage」（输出要留给后续反向消费）**或** `return_outputs=True`（调用方要拿到最终输出）时。终点 stage 且不需要返回输出时，只存 loss、不存普通输出。

---

### 4.3 `_backward_compute_chunk`：反向计算原语（last stage vs 中间 stage）

#### 4.3.1 概念说明

反向原语是本讲的难点。它和前向原语对称：**取出该方向下一个待反算的 chunk → 跑反向 → 把对输入的梯度放回缓冲（供发给上游）**。

难点在于：**反向的「种子」从哪来？** 这取决于当前是不是终点 stage：

- **终点 stage（last stage）**：手里有一个标量 `loss`，直接 `loss.backward()`，PyTorch 自动算整条链路。
- **中间 stage**：没有标量 loss，手里只有「前向输出」和「下游回传的、对这些输出的梯度」。必须用这批梯度作种子，调底层引擎 `run_backward(outputs, output_grads)` 显式驱动反向。

两条路最终都把反向结果写进每个输入张量的 `.grad`，再收集成 `input_grads` 存进 `input_grad_chunks`，等待发送给上游。

`enable_zb` 参数在本原语里只负责一件事：**控制权重梯度（W）是立即算还是延后**（详见 u2-l4）。

#### 4.3.2 核心流程

```
_backward_compute_chunk(phase, enable_zb=False):
    if forward_only: return                 # 推理不反传

    phase ^= is_in_second_half
    chunk_id = current_b_chunk_id[phase]
    current_b_chunk_id[phase] += 1          # 反向计算游标先读后自增

    is_last_stage = (is_first_rank and phase==1) or (is_last_rank and phase==0)

    WeightGradStore.enabled = enable_zb     # 开/关零气泡

    if is_last_stage:
        loss = loss_chunks[chunk_id]
        loss.backward()                     # 路径 A：标量 loss 作种子
        loss.detach_()                      # 就地 detach，释放计算图
    else:
        outputs      = output_chunks[phase][chunk_id]      # 取前向输出（种子张量）
        if not return_outputs:
            output_chunks[phase][chunk_id] = None          # 取出即释放
        output_grads = output_grad_chunks[phase][chunk_id] # 取下游回传梯度（种子梯度）
        output_grad_chunks[phase][chunk_id] = None         # 取出即释放
        non_empty = [(t,g) for t,g in zip(outputs, output_grads) if g is not None]
        outputs, output_grads = list(zip(*non_empty))      # 过滤掉无梯度的输出
        if len(outputs) > 0:
            run_backward(outputs, output_grads)            # 路径 B：显式种子驱动反向

    WeightGradStore.enabled = False
    if enable_zb:
        WeightGradStore.flush()             # 把缓存的 W 函数整箱入队，稍后 pop

    inputs = input_chunks[phase][chunk_id]
    input_chunks[phase][chunk_id] = None    # 输入也释放
    input_grads = [t.grad for t in inputs]  # 读出对每个输入的梯度
    input_grad_chunks[phase].append(input_grads)   # 存起来，等会发给上游
```

一个 chunk 的张量在反向里遵循严格的「**取出 → 用完 → 置 `None`**」生命周期：

```
output_chunks[p][c]       (前向存入) ──取出──> 反向用 ──置 None
output_grad_chunks[p][c]  (recv存入) ──取出──> 反向用 ──置 None
input_chunks[p][c]        (前向存入) ──取出──> 读 .grad ──置 None
                                        │
                                        └──> input_grad_chunks[p].append(...)
```

这种「用完即清」是 DualPipe 控制**激活显存**的关键：任意时刻，缓冲里只保留尚未被消费的 chunk，已消费的立即释放。这呼应了 u1-l1 中「DualPipe 激活显存约 PP+1」的代价分析。

#### 4.3.3 源码精读

入口与游标，[dualpipe/dualpipe.py:87-95](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L87-L95)：推理直接 `return`；否则方向翻转、取 `chunk_id`、自增反向游标、算 `is_last_stage`（与前向原语用同一公式）。

零气泡开关与终点分支，[dualpipe/dualpipe.py:97-101](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L97-L101)：置 `WeightGradStore.enabled = enable_zb`；终点 stage 取出对应 `loss`，`loss.backward()` 跑全链路反向，再 `loss.detach_()` 就地剥离计算图。

中间 stage 分支，[dualpipe/dualpipe.py:102-111](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L102-L111)：取出前向输出 `outputs` 和下游梯度 `output_grads`，各自取出后立即把缓冲槽置 `None`；用列表推导 `non_empty` 过滤掉梯度为 `None` 的（那些输出不回传梯度）的 `(t, g)` 对；`zip(*non_empty)` 再拆回平行的 `outputs` 与 `output_grads`；若仍有需要反算的，调 `run_backward(outputs, output_grads)`（即 [dualpipe/utils.py:36-43](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/utils.py#L36-L43) 的底层反向引擎薄封装，以 `accumulate_grad=True` 累加梯度）。

收尾的零气泡 flush 与输入梯度收集，[dualpipe/dualpipe.py:112-119](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L112-L119)：关掉 `enabled`；若启用了零气泡则 `WeightGradStore.flush()`（把本 chunk 攒的 W 函数整箱塞进 FIFO，留给后续 `_weight_chunk` 的 `pop` 执行，见 u2-l4）；最后取出输入张量、置 `None`、用列表推导 `[t.grad for t in inputs]` 读出每个输入的梯度，`append` 进 `input_grad_chunks`。

> **注意一个隐含不变量**：[dualpipe/dualpipe.py:108-110](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L108-L110) 的 `if len(outputs) > 0` 守卫实际上几乎不会走到「假」分支——因为若 `non_empty` 为空，`list(zip(*[]))` 会得到 `[]`，`outputs, output_grads = []` 这一步解包就会先抛 `ValueError`。也就是说，引擎默认「下游至少回传一个非空梯度」。这条不变量由通信层（下游总会 `_send_backward` 至少一路梯度）保证。

#### 4.3.4 代码实践

**实践目标**：跟踪一次中间 stage 的 `_backward_compute_chunk`，亲眼看 `outputs` 与 `output_grads` 如何被「取出 → 用完 → 置 `None`」。

下面是**示例代码（非项目代码，无需 GPU）**，用普通对象模拟张量与缓冲，复刻反向原语中段的生命周期：

```python
# 示例代码：模拟中间 stage 反向原语里 outputs/output_grads 的取出与释放
class T:
    def __init__(self, name): self.name = name; self.grad = "grad_of_" + name

# 假设前向已存好第 0 号 chunk 的输出，下游已回传对应梯度
output_chunks      = [[ [T("out0"), T("out1")] ]]   # [phase0][chunk0]
output_grad_chunks = [[ [T("g0"),   None]      ]]   # out1 没有回传梯度

outputs      = output_chunks[0][0]                 # 取出前向输出
output_chunks[0][0] = None                         # 取出即释放
output_grads = output_grad_chunks[0][0]            # 取出下游梯度
output_grad_chunks[0][0] = None                    # 取出即释放

non_empty = [(t, g) for t, g in zip(outputs, output_grads) if g is not None]
outputs, output_grads = list(zip(*non_empty))      # 过滤掉 None 梯度对
print("要反算的输出:", [t.name for t in outputs])    # 只剩 out0
print("output_chunks 槽:", output_chunks[0][0])     # None
print("output_grad_chunks 槽:", output_grad_chunks[0][0])  # None
```

**操作步骤**：

1. 把代码存为 `mock_backward.py` 运行。
2. 对照 [dualpipe/dualpipe.py:102-111](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L102-L111) 逐行核对，确认每一步都和源码一致。
3. 把 `output_grad_chunks` 里的 `None` 改成一个真梯度对象，再跑，观察「要反算的输出」从 1 个变成 2 个。

**需要观察的现象**：`out1` 因为对应梯度是 `None` 被过滤掉；两个缓冲槽在取出后都变成 `None`。

**预期结果**：`要反算的输出: ['out0']`；两个槽打印 `None`。这准确复刻了源码「过滤无梯度输出 + 取出即释放」的行为。

#### 4.3.5 小练习与答案

**练习 1**：终点 stage 用 `loss.backward()`，中间 stage 用 `run_backward(outputs, output_grads)`，根本差别是什么？
**答**：反向需要一个「种子」。终点 stage 手里有标量 `loss`，可直接 `loss.backward()` 让 autograd 从 loss 自动算整条链路；中间 stage 没有标量 loss，只能拿下游回传的「对输出的梯度」`output_grads` 作种子，通过 `run_backward` 显式驱动反向。两者最终都会把结果写进输入张量的 `.grad`。

**练习 2**：`loss.detach_()`（[dualpipe/dualpipe.py:101](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L101)）为什么用带下划线的就地版本？
**答**：`detach_()` 是就地操作，直接在原 loss 张量上切断计算图引用，从而释放反向图占用的显存；不改变量绑定。因为后续 `step()` 末尾会用 `torch.stack(self.loss_chunks)` 聚合 loss 返回（[dualpipe/dualpipe.py:432](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L432)），只需保留数值、不需保留计算图。

**练习 3（进阶）**：若一个中间 stage chunk 的所有 `output_grads` 都是 `None`（即下游一路梯度全空），代码会怎样？
**答**：`non_empty` 为空，`list(zip(*[]))` 得到 `[]`，随后 `outputs, output_grads = []` 解包会抛 `ValueError`——`if len(outputs) > 0` 守卫来不及救。因此引擎隐含假设「下游至少回传一个非空梯度」，这条假设由通信层保证（下游总会发送至少一路梯度）。

---

## 5. 综合实践

把本讲三块知识串起来，做一个**端到端的纸上推演**：跟踪某一个 phase 上「一个 chunk 从被前向消费、到被反向消费」的完整张量流转。

**任务**：取一个中间 stage（既非首也非末 rank），假设外层传入 `phase=0`，且该 rank 不在 second half。

1. **画二维缓冲表**：在纸上画出 `input_chunks / output_chunks / output_grad_chunks / input_grad_chunks` 四行，每行画两格（phase0、phase1），每格是一个按 `chunk_id` 排列的列表。标出某号 chunk（如 `chunk_id=3`）在四行里的位置。
2. **前向阶段**：当 `_forward_compute_chunk` 处理该 chunk 时，标出哪一行被「读取」（`input_chunks`）、哪一行被「写入」（`output_chunks`），并标出游标 `current_f_chunk_id[0]` 的变化。
3. **反向阶段**：当 `_backward_compute_chunk` 处理同一 chunk 时，标出哪两行被「读取并置 `None`」（`output_chunks`、`output_grad_chunks`）、哪一行被「写入」（`input_grad_chunks`），以及 `input_chunks` 槽被置 `None`、`current_b_chunk_id[0]` 的变化。
4. **核对生命周期**：确认该 chunk 在四类缓冲中「填入 → 消费 → 清空」的先后顺序符合 4.3.2 的流程图。

**验收标准**：你能不看本讲义，口述出「前向读 `input_chunks` 写 `output_chunks`；反向读 `output_chunks` 与 `output_grad_chunks`、写 `input_grad_chunks`，并把三者对应的 `input_chunks` 槽清空」。这相当于说清楚了 DualPipe 控制激活显存的核心机制。

> 真正在多卡上跑通需要 NCCL 与多 GPU 环境，本综合实践以源码阅读 + 纸上推演为主；若需观察真实张量，可参考 [u1-l2](u1-l2-setup-and-run-examples.md) 运行 `examples/example_dualpipe.py`，相关分布式执行结果待本地验证。

---

## 6. 本讲小结

- `_reset_states` 是八步调度的状态地基：它清空 `WeightGradStore`，并建立 `input/output/input_grad/output_grad` 四类「`[phase][chunk_id]`」二维缓冲，外加 6 组「先读后自增」的进度计数器和 `comm_ops / to_free` 两个累积列表。
- 四类缓冲对应一个 chunk 张量的四个生命阶段：前向原料、前向产物、下游回传梯度、算出的输入梯度；每类都用「外层选方向、内层选微批次」的二维结构。
- `_forward_compute_chunk` 取出下一个 chunk 的输入、跑 `self.module[phase]`，终点 stage 算 loss 存 `loss_chunks`，否则把输出存进 `output_chunks`；推理模式下输入用完即释放。
- `_backward_compute_chunk` 的核心分叉是：**终点 stage 用 `loss.backward()`**（标量 loss 作种子），**中间 stage 用 `run_backward(outputs, output_grads)`**（下游梯度作种子）。
- 反向原语严格遵循「取出 → 用完 → 置 `None`」的生命周期，把 `output_chunks`、`output_grad_chunks`、`input_chunks` 的对应槽清空，并把输入梯度写进 `input_grad_chunks` 等待发给上游——这是 DualPipe 控制激活显存的关键。
- `enable_zb` 在本原语里只负责开关权重梯度的「立即算 / 延后入队」，开启时反向结束后调 `WeightGradStore.flush()` 把 W 函数整箱塞进 FIFO（细节见 u2-l4）。

---

## 7. 下一步学习建议

本讲只讲了「计算原语」本身——它们假设输入张量已经躺在缓冲里、输出的梯度也会有人送来。但**谁负责收发这些张量、谁负责把它们排进缓冲、谁负责批量提交 P2P 通信**？这些是下一讲 [u3-l4 通信原语与组合操作](u3-l4-comm-primitives-and-composite-ops.md) 的内容：`_recv/_send_forward/_backward`、`_commit_and_wait_comm`、`_free_tensors`，以及把收发 + 计算打包在一起的 `_forward_chunk / _backward_chunk / _forward_backward_chunk / _weight_chunk`。

读完 u3-l4 后，再进入 [u3-l5 DualPipe 八步调度 step()](u3-l5-dualpipe-eight-step-schedule.md)，你会看到本讲的两个计算原语和那些组合操作如何被拼成完整的 8 步调度。建议同时回头重读本讲的 4.3.2 流程图——它是理解八步调度中「张量何时被生产、何时被消费」的钥匙。
