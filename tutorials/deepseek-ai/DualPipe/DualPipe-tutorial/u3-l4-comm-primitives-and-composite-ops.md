# 通信原语与组合操作

## 1. 本讲目标

上一讲（u3-l2）我们讲完了 DualPipe 的「状态管理」与「计算原语」——引擎知道怎么取一个微批次、怎么跑前向 `module[phase]`、怎么用 `run_backward` 跑反向。但计算原语只负责「本地算」，它不负责「把数据搬给邻居、再从邻居拿回数据」。

本讲就把这块拼图补上：**相邻 GPU 之间的 P2P 通信**，以及把「通信 + 计算」拼装成可调度单元的**组合操作**。学完本讲你应该能：

1. 说清 DualPipe 通信的「两段式骨架」——先**累积**收发请求，再**一次性批量提交**，以及为什么这样做能压缩气泡。
2. 读懂 `phase ^= is_in_second_half` 这一行「方向翻转」黑魔法，并能在一张表里说清四个收发原语各自向哪个邻居收发。
3. 说出 `_commit_and_wait_comm` 何时真正提交通信、`_free_tensors` 何时回收显存，以及 `to_free` 里的张量为什么会被**推迟一个提交周期**才释放。
4. 看懂 `_forward_chunk / _backward_chunk / _forward_backward_chunk / _weight_chunk` 这四个组合操作的「recv → commit_wait → compute → send」固定节拍，并能手动展开一次 `_forward_backward_chunk(0, 1)` 的完整调用序列。

本讲是下一讲 u3-l5「八步调度 `step()`」的零件库——八步调度的每一行都是这四个组合操作的排列组合。

## 2. 前置知识

本讲默认你已掌握以下内容（来自前置讲义）：

- **微批次（chunk）与双向流水线**（u2-l1）：一个 batch 被切成多个 chunk 依次流过流水线；数据从两端相向喂入，形成 forward、reverse 两条方向。
- **comm.py 的 P2P 工具**（u2-l2）：`append_irecv` / `append_isend` **不立即收发**，而是把 `dist.P2POp` 追加进一个 `ops` 列表；真正的收发要靠 `dist.batch_isend_irecv` 统一提交。`TENSOR_SHAPES` / `TENSOR_DTYPE` 是全局约定的张量形状与 dtype。
- **rank 拓扑**（u3-l1）：`self.rank` 是逻辑流水线 rank；`self.first_rank / prev_rank / next_rank / last_rank` 都是**进程组内 rank**，直接喂给底层通信；四个布尔标志 `is_first_rank / is_last_rank / is_middle_rank / is_in_second_half` 驱动后续分支。
- **状态与计算原语**（u3-l2）：`input_chunks / output_chunks / output_grad_chunks / input_grad_chunks` 四类缓冲按 `[phase][chunk_id]` 二维组织；`current_*_chunk_id` 是「先读后自增」的游标；`_forward_compute_chunk` 取输入算前向、`_backward_compute_chunk` 取输出与梯度算反向。

补充一个 PyTorch 分布式常识：`dist.batch_isend_irecv(ops)` 会把一组 `P2POp`（含 `isend` 和 `irecv`）打包成**一次集合调用**返回一组 `Work` 句柄，调用方再逐个 `req.wait()`。相比一条一条地 `isend`/`irecv`，批量提交能让多个收发**彼此重叠**、并与计算重叠——这正是 DualPipe 通信层的设计起点。

## 3. 本讲源码地图

本讲只涉及两个文件，全部集中在 `dualpipe/` 包内：

| 文件 | 本讲涉及内容 | 作用 |
| --- | --- | --- |
| `dualpipe/comm.py` | `append_irecv` / `append_isend`（L25-38） | 通信「累积」半边：把 P2POp 追加进 ops 列表 |
| `dualpipe/dualpipe.py` | `_recv_forward` / `_send_forward` / `_recv_backward` / `_send_backward`（L231-283） | 四个收发原语：决定向哪个邻居、攒什么请求 |
| `dualpipe/dualpipe.py` | `_commit_and_wait_comm` / `_free_tensors`（L225-229, L285-292） | 通信「提交」半边与显存回收 |
| `dualpipe/dualpipe.py` | `_forward_chunk` / `_backward_chunk` / `_forward_backward_chunk` / `_weight_chunk`（L185-223） | 四个组合操作：把收发与计算拼成调度单元 |

两个全局状态容器也在本讲频繁出现，它们在 `_reset_states` 里初始化（u3-l2 已讲过，这里只复用）：

- `self.comm_ops: List[dist.P2POp]`：待提交的通信请求队列，每次 `step` 开始时清空（[dualpipe/dualpipe.py:64](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L64)）。
- `self.to_free: List[torch.Tensor]`：等通信完成后要释放显存的张量列表（[dualpipe/dualpipe.py:65](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L65)）。

## 4. 核心概念与源码讲解

### 4.1 通信的两段式骨架与方向翻转技巧

#### 4.1.1 概念说明

如果把 DualPipe 的通信比作「发快递」，那么它故意拆成两个岗位：

- **打包员（累积）**：`_recv_*` / `_send_*` 只负责「把要收发的包裹贴单子、堆到传送带上」，即往 `self.comm_ops` 里追加 `P2POp`，**绝不自己发货**。底层工具 `comm.append_irecv` / `comm.append_isend` 就是贴单子的动作。
- **调度员（提交）**：只有 `_commit_and_wait_comm` 有权「按下发货按钮」——它调用 `dist.batch_isend_irecv` 把传送带上**所有**单子一次性发出去，再 `wait` 等它们全部送达。

为什么要这么麻烦地拆成两段？因为**批量提交是重叠的前提**。设想一个中间 rank 在某个调度单元里要「收下一个前向输入 + 收下一个反向梯度」。如果每个收发都立即执行，两次通信只能串行；而如果先把两个 `irecv` 都攒到 `comm_ops` 里，再一次 `batch_isend_irecv` 提交，NCCL 就能让它们**并行飞**，甚至和 GPU 上的计算重叠。这就是「累积 → 批量提交」两段式设计的全部动机。

> 回顾 u2-l2：`append_irecv` 接收时会现造一个空缓冲区（`build_from_tensor_shapes`），`append_isend` 发送时则复用调用方传入的张量。本讲我们站在引擎层，看这些张量从哪个缓冲来、回到哪个缓冲去。

#### 4.1.2 核心流程

四个收发原语都遵循同一个**三步骨架**（以「收」为例，「发」是对称的）：

```text
收发原语(phase):
  1. phase ^= self.is_in_second_half        # 方向翻转：把 phase 归一成「0=正向 / 1=反向」
  2. 边界判定：是端点吗？是 → 直接 return（短路）
  3. 游标自增 + 选邻居(prev/next) + append_irecv/isend 攒进 comm_ops
     （收：把新缓冲 append 进对应 chunk 缓冲；发：把待发张量登记进 to_free）
```

**方向翻转**是本讲最需要吃透的一行：

```python
phase ^= self.is_in_second_half
```

`step()` 开头的注释（[dualpipe/dualpipe.py:355-356](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L355-L356)）说得很清楚：

- 前半段 rank：`phase 0` = forward 方向，`phase 1` = reverse 方向；
- 后半段 rank：`phase 0` = reverse 方向，`phase 1` = forward 方向。

这是因为后半段 rank 持有的两个镜像模块，方向定义和前半段相反（见 u3-l1）。为了让收发代码「对前半段和后半段写同一套逻辑」，引擎在进入收发时用 XOR 把 phase **归一化**：归一后 `phase == 0` **永远**表示 forward（数据从 rank 0 流向 last rank），`phase == 1` **永远**表示 reverse（数据从 last rank 流向 rank 0）。后续选邻居、判端点全都基于这个归一后的 phase，再也不用关心本 rank 在哪一半。

XOR 的效果可以列成一张小表：

| 本 rank 位置 | `is_in_second_half` | 原 phase 0 → 归一后 | 原 phase 1 → 归一后 |
| --- | --- | --- | --- |
| 前半段 | 0 | 0 = forward | 1 = reverse |
| 后半段 | 1 | 1 = reverse | 0 = forward |

#### 4.1.3 源码精读

「累积」半边的工具在 comm.py。`append_irecv` 先造缓冲区、再用 `get_global_rank` 把组内 rank 翻译成全局 rank，然后逐个张量贴 `P2POp(irecv)` 单子；`append_isend` 同理，只是用调用方传入的张量、贴 `P2POp(isend)` 单子。两者都只往传入的 `ops` 列表里 `append`，**不发车**：

[dualpipe/comm.py:25-38](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/comm.py#L25-L38) — `append_irecv` 与 `append_isend`：通信的「贴单子」动作，只累积、不提交。

「方向翻转」则出现在四个收发原语的第一行，例如 `_recv_forward`：

[dualpipe/dualpipe.py:231-232](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L231-L232) — `phase ^= self.is_in_second_half` 把 phase 归一成「0=forward / 1=reverse」。

两个全局容器的初始化在 `_reset_states` 中：

[dualpipe/dualpipe.py:64-65](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L64-L65) — 每次 `step` 开始时把 `comm_ops` 和 `to_free` 清空。

#### 4.1.4 代码实践（源码阅读型）

**目标**：亲眼确认「累积」与「提交」是分开的两步。

1. 打开 `dualpipe/comm.py`，确认 `append_irecv` / `append_isend` 的函数体里**没有任何** `dist.batch_isend_irecv` 或 `req.wait()`——它们只 `ops.append(...)`。
2. 打开 `dualpipe/dualpipe.py`，用搜索确认 `_commit_and_wait_comm` 是**全文件唯一**调用 `dist.batch_isend_irecv` 的地方（L288）。
3. 观察：四个收发原语里都没有 `batch_isend_irecv`，它们只在「攒单子」。

**预期现象**：「提交」权力被严格收归到 `_commit_and_wait_comm` 一处，其余所有地方都只能累积。这是理解后续组合操作节拍的关键。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `append_irecv` 要 `requires_grad=True`（见 comm.py L22）？
**答**：收下来的张量要作为下游 stage 前向的输入参与反向求导，必须带梯度图节点，所以 `build_from_tensor_shapes` 造缓冲时就开 `requires_grad=True`。

**练习 2**：如果把 `phase ^= self.is_in_second_half` 这一行删掉，前后半段 rank 的通信方向会怎样？
**答**：后半段 rank 的 phase 不再被归一，`phase 0` 会被误当成 forward 方向去 `prev_rank` 收数据，但后半段 rank 持有的 `module[0]` 其实服务 reverse 方向——收发对象和邻居全部错位，数据流会乱。

---

### 4.2 四个收发原语：_recv_forward / _send_forward / _recv_backward / _send_backward

#### 4.2.1 概念说明

四个原语分别对应一个 chunk 张量生命周期的四个「搬运时刻」：

| 原语 | 搬的是什么 | 从哪来 / 到哪去 | 写入 / 取出的缓冲 |
| --- | --- | --- | --- |
| `_recv_forward` | 上游送来的**前向输入** | 收：phase0←prev_rank，phase1←next_rank | append 进 `input_chunks` |
| `_send_forward` | 本地算出的**前向输出** | 发：phase0→next_rank，phase1→prev_rank | 取自 `output_chunks`，登记 `to_free` |
| `_recv_backward` | 下游回传的**输出梯度** | 收：phase0←next_rank，phase1←prev_rank | append 进 `output_grad_chunks` |
| `_send_backward` | 本地算出的**输入梯度** | 发：phase0→prev_rank，phase1→next_rank | 取自 `input_grad_chunks`，置 None |

注意 forward 与 backward 的邻居方向**正好相反**：前向数据沿 rank 0 → last_rank 流，所以前向「收自上游(prev)、发往下游(next)」；梯度沿反方向回流，所以反向「收自下游(next)、发往上游(prev)」。reverse 方向（phase 1）则整体翻转。

每个原语都有一处**端点短路**：流水线的端点 rank 没有邻居可收发，直接 `return`。这正是「双向流水线首末两端各喂一半数据」（u2-l1）在通信层的体现。

#### 4.2.2 核心流程

四个原语共用一套判定逻辑，只是「哪个端点短路」「向哪个邻居」不同。下面用归一后的 phase（0=forward / 1=reverse）统一描述：

**端点短路表**（归一 phase）：

| 原语 | 短路条件 | 直觉 |
| --- | --- | --- |
| `_recv_forward` | `is_first_stage`：phase0 是 first_rank，phase1 是 last_rank | forward 的**起点**自己有外部输入，不收 |
| `_send_forward` | `is_last_stage`：phase0 是 last_rank，phase1 是 first_rank | forward 的**终点**算完即结束，不发 |
| `_recv_backward` | `is_last_stage`（同 send_forward） | 梯度的**起点**（算 loss 处）自己有梯度种子，不收 |
| `_send_backward` | `is_first_stage`（同 recv_forward） | 梯度的**终点**（模型输入处）没有上游，不发 |

可以发现对称之美：`_recv_forward` 与 `_send_backward` 共用 `is_first_stage`；`_send_forward` 与 `_recv_backward` 共用 `is_last_stage`。因为前向的起点正是反向的终点、前向的终点正是反向的起点。

**邻居选择表**（归一 phase）：

| 原语 | phase 0 (forward) | phase 1 (reverse) |
| --- | --- | --- |
| `_recv_forward` | prev_rank | next_rank |
| `_send_forward` | next_rank | prev_rank |
| `_recv_backward` | next_rank | prev_rank |
| `_send_backward` | prev_rank | next_rank |

#### 4.2.3 源码精读

**`_recv_forward`**：翻方向 → 起点短路 → 收自上游，新缓冲 append 进 `input_chunks`。

[dualpipe/dualpipe.py:231-239](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L231-L239) — `_recv_forward`：起点 rank 短路；否则 `current_recv_f_chunk_id` 自增，向 `prev_rank`(phase0) / `next_rank`(phase1) 攒 irecv，结果 append 进 `input_chunks[phase]`。

**`_send_forward`**：翻方向 → 终点短路 → 取出待发输出，攒 isend，并把张量登记进 `to_free`。

[dualpipe/dualpipe.py:241-254](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L241-L254) — `_send_forward`：终点 rank 短路；否则用 `current_send_f_chunk_id` 从 `output_chunks` 取出张量，向 `next_rank`(phase0) / `prev_rank`(phase1) 攒 isend；若不需要返回输出，把张量加进 `to_free` 等回收。

**`_recv_backward`**：推理模式(`forward_only`)直接 return；否则翻方向 → 终点（梯度起点）短路 → 收自下游，append 进 `output_grad_chunks`。

[dualpipe/dualpipe.py:256-267](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L256-L267) — `_recv_backward`：推理模式短路；终点 rank 短路；否则向 `next_rank`(phase0) / `prev_rank`(phase1) 攒 irecv，结果 append 进 `output_grad_chunks[phase]`。

**`_send_backward`**：推理模式短路；否则翻方向 → 起点（梯度终点）短路 → 取出输入梯度，攒 isend，并把该槽置 None。

[dualpipe/dualpipe.py:269-283](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L269-L283) — `_send_backward`：推理模式短路；起点 rank 短路；否则用 `current_send_b_chunk_id` 从 `input_grad_chunks` 取出张量、槽位置 None，向 `prev_rank`(phase0) / `next_rank`(phase1) 攒 isend。

#### 4.2.4 代码实践（源码阅读型）

**目标**：验证「forward 与 backward 邻居方向相反」。

1. 在 `_send_forward`(L251) 与 `_send_backward`(L283) 之间对照，确认 phase 0 时一个发 `next_rank`、一个发 `prev_rank`。
2. 在 `_recv_forward`(L238) 与 `_recv_backward`(L266) 之间对照，确认 phase 0 时一个收 `prev_rank`、一个收 `next_rank`。
3. 对照 4.2.2 的两张表，逐行核对源码。

**预期结果**：四个原语的邻居选择与表格完全一致，且 forward/badkward 方向相反、phase0/phase1 方向相反。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `_recv_backward` 和 `_send_backward` 开头都有 `if self.forward_only: return`，而 `_recv_forward` / `_send_forward` 没有？
**答**：推理（`forward_only`，由 `not torch.is_grad_enabled()` 决定）时不跑反向，既不需要收输出梯度、也不需要发输入梯度，所以两个 backward 原语整体短路；前向在推理时仍需收发，故不短路。

**练习 2**：`_send_forward` 把张量加进 `to_free`，`_send_backward` 却把槽位置 `None`，为什么处理方式不同？
**答**：`_send_forward` 发的是 `output_chunks` 里的输出张量，它们还要在**下一次反向**被 `run_backward` 取用（若 `return_outputs`），不能立刻销毁，只能先登记 `to_free` 等通信完成后由 `_free_tensors` 软释放；而 `input_grad_chunks` 里的输入梯度发出去后就再无用处，直接置 None 即可立即让 Python 释放引用。

---

### 4.3 批量提交与显存回收：_commit_and_wait_comm / _free_tensors

#### 4.3.1 概念说明

`_commit_and_wait_comm` 是整个通信层的**唯一发货口**。它做三件事：

1. 若 `comm_ops` 为空就直接返回（什么都没攒，不白调一次 `batch_isend_irecv`）。
2. 否则 `dist.batch_isend_irecv(self.comm_ops)` 一次性提交全部请求，再逐个 `req.wait()` 等它们完成。
3. 提交并等待完成后，清空 `comm_ops`，并调用 `_free_tensors()` 回收 `to_free` 里的张量显存。

`_free_tensors` 则是「软释放」：它不 `del` 张量对象，而是把张量的底层存储换成空 `torch.Tensor()`——`tensor.data = torch.Tensor()`。这样 Python 侧的引用还在（缓冲槽位仍指向这个对象），但 GPU 显存被立刻还给分配器。它还断言 `tensor._base is None`，拒绝回收「视图张量」（view），因为视图的显存归母张量管，单独清视图释放不了显存还会留下悬空引用。

#### 4.3.2 核心流程：to_free 张量何时被释放（重点）

这是本讲最微妙的一点，也是综合实践要回答的问题。关键在于：**发送是「延后提交」的，所以释放也必须「延后一个提交周期」。**

设一个中间 rank 连续跑两次 `_forward_chunk(0)`，记为 chunk A、chunk B：

```text
chunk A:
  recv_forward   → comm_ops = [R1]
  commit_wait    → 提交 [R1] 并 wait；to_free 此时为空，不释放；comm_ops=[]
  forward_compute→ 算出输出 O1，存进 output_chunks
  send_forward   → comm_ops = [S1]；to_free = [O1 的张量]   ← 发送只「贴单」，未提交！

chunk B:
  recv_forward   → comm_ops = [S1, R2]   ← 上一块的 S1 还在传送带上！
  commit_wait    → 一次性提交 [S1, R2]（发送与接收重叠！）并 wait
                   → 然后 _free_tensors() 释放 to_free=[O1 的张量]  ← 现在才安全
                   → comm_ops=[]; to_free=[]
  forward_compute→ 算出 O2
  send_forward   → comm_ops = [S2]；to_free = [O2 的张量]
```

结论很清晰：

- 一个张量在 `_send_forward` 里被**登记进 `to_free`** 的时刻，它的 `isend` **尚未提交**，绝不能释放。
- 它要等到**下一次 `_commit_and_wait_comm`**——那一次才会真正提交并 `wait` 完它的 `isend`，随后 `_free_tensors` 才安全回收。
- 因此 `to_free` 里的张量恒定「**晚一个提交周期**」被释放。这个延迟是有意为之：既保证了「发送实际完成前不释放」，又让「上一块的发送」能与「下一块的接收」在同一次 `batch_isend_irecv` 里**重叠**。

> `step()` 结束前还有一次兜底提交 [dualpipe/dualpipe.py:427](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L427)，保证最后一批「只贴单未提交」的发送也被发出去、最后一批 `to_free` 也被回收。

#### 4.3.3 源码精读

**`_commit_and_wait_comm`**：空则跳过；否则批量提交、等待、清空、回收。

[dualpipe/dualpipe.py:285-292](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L285-L292) — `_commit_and_wait_comm`：`batch_isend_irecv` 提交 `comm_ops` 全部请求，逐个 `wait`，清空 `comm_ops`，再 `_free_tensors()` 回收 `to_free`。

**`_free_tensors`**：拒绝视图张量，软释放底层存储。

[dualpipe/dualpipe.py:225-229](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L225-L229) — `_free_tensors`：对每个 `to_free` 张量断言非视图，再把 `tensor.data` 换成空张量释放显存，最后清空列表。

#### 4.3.4 代码实践（源码阅读型）

**目标**：确认「提交即回收」绑定在一起。

1. 读 `_commit_and_wait_comm`，确认 `_free_tensors()` 是它在 `wait` 之后**唯一**的副作用调用。
2. 全文搜索 `to_free`，确认张量**只**在 `_send_forward`(L254) 被加入、**只**在 `_free_tensors` 被清空，中间没有任何地方提前 `del`。
3. 思考：若把 `_free_tensors()` 从 `_commit_and_wait_comm` 里挪到 `_send_forward` 末尾会怎样？

**预期结果/现象**：若提前释放，`isend` 还没提交就要发一个已被清空的张量，通信内容会变成垃圾（甚至非法内存）。因此释放必须严格排在「提交 + wait」之后。**待本地验证**：在有 GPU 的环境里给 `_free_tensors` 加一行日志，可观察到它在每次 `_commit_and_wait_comm` 后触发，频率与提交次数一致。

#### 4.3.5 小练习与答案

**练习 1**：`_free_tensors` 为什么要 `assert tensor._base is None`？
**答**：`_base is not None` 表示该张量是某个更大张量的视图。视图不独占显存，清它的 `data` 既释放不了母张量显存，又会让母张量失去这块视图的引用关系，容易引发隐蔽 bug。所以引擎要求 stage 输出必须是「非视图」的独立张量。

**练习 2**：`_commit_and_wait_comm` 开头的 `if not self.comm_ops: return` 有什么用？
**答**：组合操作里 `_weight_chunk` 等会在「没有新收发」时仍调用 `_commit_and_wait_comm`（见 4.4）。这个早退避免对空列表白白发起一次 `batch_isend_irecv`，既是性能优化，也避免无意义的同步点。

---

### 4.4 四个组合操作：_forward_chunk / _backward_chunk / _forward_backward_chunk / _weight_chunk

#### 4.4.1 概念说明

收发原语和计算原语都是「零件」，八步调度需要的是「能直接调用的积木」。四个组合操作就是把零件按固定节拍拼好的积木：

| 组合操作 | 节拍 | 对应调度的什么动作 |
| --- | --- | --- |
| `_forward_chunk(phase)` | recv → commit_wait → forward_compute → send | 推进一个前向 |
| `_backward_chunk(phase)` | recv → commit_wait → backward_compute → send | 推进一个反向 |
| `_forward_backward_chunk(phase0, phase1)` | recv_f + recv_b → commit_wait → fb_compute → send_f + send_b | 一次前向 + 一次反向**重叠**（F&B） |
| `_weight_chunk()` | commit_wait → WeightGradStore.pop() | 在气泡里塞一个**延后的权重梯度**（零气泡） |

它们都暴露了 `recv` / `send` 开关（`_forward_chunk` / `_backward_chunk`）或 `recv0` 开关（`_forward_backward_chunk`），让八步调度能精确控制「这次要不要收/要不要发」——因为端点 rank 和 middle rank 在某些步骤需要跳过收发，这正是 u3-l5 里 `send=self.is_middle_rank` 这类参数的由来。

#### 4.4.2 核心流程

**`_forward_chunk` / `_backward_chunk`** 共用同一个「四拍」骨架，差别只在中间调的是 forward 还是 backward 计算原语：

```text
_forward_chunk(phase, recv=True, send=True):
  if recv:  _recv_forward(phase)       # 攒收
  _commit_and_wait_comm()              # 提交并等待（上一块的 send 也在此提交）
  _forward_compute_chunk(phase)        # 本地算
  if send:  _send_forward(phase)       # 攒发（登记 to_free，但不提交）
```

**`_forward_backward_chunk`** 把一次前向与一次反向**捆绑成一个调度单元**：两个 recv 都在 `commit_wait` 之前攒好（于是它们在同一次 `batch_isend_irecv` 里重叠），算完 `_forward_backward_compute_chunk` 后两个 send 一起攒好（留给下一次提交重叠）。这就是 README 调度图里「同一个黑框圈住的前向格 + 反向格」的代码化身（F&B 重叠，u1-l1、u3-l3）：

```text
_forward_backward_chunk(phase0, phase1, recv0=True):
  if recv0: _recv_forward(phase0)      # ┐ 两个 recv 一起攒
            _recv_backward(phase1)     # ┘
  _commit_and_wait_comm()              # 一次提交 → 两个收发重叠
  _forward_backward_compute_chunk(phase0, phase1)   # 前向 + 反向重叠计算
  _send_forward(phase0)                # ┐ 两个 send 一起攒，留给下一次提交
  _send_backward(phase1)               # ┘
```

**`_weight_chunk`** 最特殊：它没有计算原语，只做 `_commit_and_wait_comm` + `WeightGradStore.pop()`——即「把当前攒着的通信提交掉，然后从零气泡队列里弹出一个延后的权重梯度函数执行」。它的存在意义就是**填气泡**：当某段时间 GPU 计算资源有空档（通信在飞），就趁机把原本要算的 W（权重梯度）补上。注意 `WeightGradStore.pop()` 注释写着 `# Assume FIFO`（L222）——按入队顺序弹出。

#### 4.4.3 源码精读

**`_forward_chunk`**：四拍节拍的范本。

[dualpipe/dualpipe.py:185-193](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L185-L193) — `_forward_chunk`：`recv` 开关控制是否收，`commit_wait` 提交，`_forward_compute_chunk` 计算，`send` 开关控制是否发。

**`_backward_chunk`**：与 `_forward_chunk` 同构，中间换成反向计算，并多传一个 `enable_zb`（u3-l2 讲过，控制权重梯度是否延后入队）。

[dualpipe/dualpipe.py:195-203](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L195-L203) — `_backward_chunk`：`recv`/`send` 开关 + `commit_wait` + `_backward_compute_chunk(phase, enable_zb)`。

**`_forward_backward_chunk`**：两个 recv 一起攒、两个 send 一起攒，中间一次重叠计算。

[dualpipe/dualpipe.py:205-214](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L205-L214) — `_forward_backward_chunk`：`recv0` 控制前向是否收；反向 recv 无条件（受 `_recv_backward` 内部 forward_only / 端点短路保护）；一次 `commit_wait`；`_forward_backward_compute_chunk` 前反向重叠；最后无条件 `send_forward` + `send_backward`（同样受端点短路保护）。

**`_weight_chunk`**：推理模式短路；否则提交通信 + 弹一个权重梯度。

[dualpipe/dualpipe.py:216-223](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L216-L223) — `_weight_chunk`：`forward_only` 短路；`_commit_and_wait_comm` 后 `WeightGradStore.pop()`（FIFO）执行一个延后的权重梯度，用来填气泡。

#### 4.4.4 代码实践（手动展开调用序列）

**目标**：手动展开一次 `_forward_backward_chunk(0, 1, recv0=True)`，列出 recv → commit_wait → compute → send 的顺序。

设调用者是一个**非端点、非 middle** 的前半段 rank（`is_in_second_half=False`，所以 phase 归一后 0=forward、1=reverse；且它有完整的 prev/next 邻居）。展开如下：

```text
_forward_backward_chunk(phase0=0, phase1=1, recv0=True):
  1. _recv_forward(0)
       phase ^= 0 → 0；非起点 → 攒 irecv(prev_rank) 进 comm_ops
       → comm_ops = [irecv(prev, 输入)]
  2. _recv_backward(1)
       非 forward_only；phase ^= 0 → 1；非终点 → 攒 irecv(next_rank) 进 comm_ops
       → comm_ops = [irecv(prev, 输入), irecv(next, 输出梯度)]
  3. _commit_and_wait_comm()
       batch_isend_irecv([两条 irecv]) → 两个接收重叠；wait
       → _free_tensors()（此时 to_free 来自「上一次组合操作遗留的 send」）
       → comm_ops = []
  4. _forward_backward_compute_chunk(0, 1)
       前向取 input_chunks[0] 算 module[0]，反向取 output_chunks[1]+output_grad_chunks[1] 跑 run_backward
       → 产出前向输出（存 output_chunks[0]）与反向输入梯度（存 input_grad_chunks[1]）
  5. _send_forward(0)
       phase ^= 0 → 0；非终点 → 取 output_chunks[0] 的输出，攒 isend(next_rank)
       → comm_ops = [isend(next, 输出)]；to_free += [刚发的输出张量]
  6. _send_backward(1)
       非 forward_only；phase ^= 0 → 1；非起点 → 取 input_grad_chunks[1] 的输入梯度（槽位置 None），攒 isend(prev_rank)
       → comm_ops = [isend(next, 输出), isend(prev, 输入梯度)]
```

**需要观察的现象**：

- 第 1、2 步的两个 `irecv` 都先攒进 `comm_ops`，在第 3 步**一次性**提交——这就是「接收与接收重叠」。
- 第 5、6 步的两个 `isend` 也只攒不提交，它们要等到**下一个组合操作**开头的 `_commit_and_wait_comm`（或 `step` 末尾 L427 的兜底提交）才真正发出——这就是「发送与下一次接收重叠」。
- 第 3 步的 `_free_tensors` 释放的是 `to_free` 里**上一轮** send 登记的张量（因为它们的 isend 在这一次提交里才完成），**不是**第 5 步刚登记的输出张量——后者要等到再下一次提交。这与 4.3.2 的结论一致：`to_free` 张量恒定「晚一个提交周期」释放。

**预期结果**：一次 `_forward_backward_chunk` 内部只发生**一次** `batch_isend_irecv`（第 3 步），它负责提交「本块的接收」+「上一块的发送」；本块的发送被推迟到下一次提交。若无法实际运行，以上为「待本地验证」的静态推导。

#### 4.4.5 小练习与答案

**练习 1**：`_forward_backward_chunk` 里反向的 `_recv_backward(phase1)` 没有 recv 开关，会不会在端点 rank 上出错？
**答**：不会。`_recv_backward` 内部已对 `forward_only` 和终点 rank 做了短路（4.2.3），端点 rank 调用时直接 return，不攒任何请求，所以不需要外层开关。

**练习 2**：`_weight_chunk` 为什么放在步骤 3/6/7（u3-l5 将讲）而不是步骤 4？
**答**：步骤 4 是主步 `nF0B1F1B0`，前向与反向已经充分重叠、GPU 几乎没有空档；而步骤 3/6/7 处于灌水/排水附近，存在只能做单一方向计算的气泡，正好用 `_weight_chunk` 把延后的 W 塞进去填满，实现零气泡。

**练习 3**：四个组合操作里，哪一个**一定不会**调用任何 `_recv_*`？哪一个**一定不会**调用任何计算原语？
**答**：`_weight_chunk` 既不收也不发数据、也不调用 `_forward/backward_compute_chunk`，它只提交通信 + `WeightGradStore.pop()`；所以「不调用计算原语」的是 `_weight_chunk`。其余三个都会收发且都会调用计算原语（`_weight_chunk` 是唯一不调用计算原语的）。

---

## 5. 综合实践

**任务**：把本讲四个组合操作串起来，画出「一个中间 rank 跑 step 4 主步两次迭代」的 `comm_ops` 与 `to_free` 内容演化图，并据此解释显存回收时机。

设该 rank 非端点、非 middle、在前半段。step 4 主步的迭代体（[dualpipe/dualpipe.py:383-396](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L383-L396)）简化为：

```text
_forward_backward_chunk(0, 1)   # 迭代 i>0 时
_forward_backward_chunk(1, 0)
```

操作步骤：

1. 取两次连续迭代，按 4.4.4 的展开方式，逐行记录每次 `_commit_and_wait_comm` 触发前 `comm_ops` 里的内容、以及触发后 `to_free` 里被释放的张量来源。
2. 重点标注：第 N 次提交释放的张量，其 `isend` 是在第 N-1 次组合操作的 send 步登记的。
3. 回答两个问题：
   - 在整个 step 4 期间，一个前向输出张量从「被算出」到「被 `_free_tensors` 软释放」，中间经历了哪几次提交？
   - 如果把 `return_outputs=True`（u3-l2 讲过的输出返回开关），`_send_forward` 里 `to_free.extend(tensors)` 这一行（L253-254）会被跳过——这对显存有何影响？

预期结果：

- 一个前向输出张量在「本次 send 登记 → 下次 commit_wait 提交并 wait → 下次 `_free_tensors` 释放」之间，恰好隔着**一次** `_commit_and_wait_comm`。
- `return_outputs=True` 时输出张量不被登记进 `to_free`，于是不会被软释放——它们要保留到最后 `gather` 聚合输出（[dualpipe/dualpipe.py:434](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L434)），代价是这些输出的显存会占用更久。这正是 README 说 DualPipe 激活显存约为 PP+1 的一个具体来源。

若无可运行的多 GPU 环境，本实践为「源码阅读 + 纸面推演」型，明确标注「待本地验证」。

## 6. 本讲小结

- DualPipe 通信是**两段式**的：`_recv_*` / `_send_*` 只往 `self.comm_ops` 攒 `P2POp`（累积），只有 `_commit_and_wait_comm` 用 `dist.batch_isend_irecv` 一次性提交（提交）。批量提交是收发彼此重叠、与计算重叠的前提。
- `phase ^= self.is_in_second_half` 在每个收发原语开头把 phase **归一**为「0=forward / 1=reverse」，让前/后半段 rank 共用同一套收发逻辑；归一后 forward 与 backward 的邻居方向相反、phase0 与 phase1 的邻居方向相反。
- 四个收发原语各有**端点短路**：`_recv_forward`/`_send_backward` 在 first stage 短路，`_send_forward`/`_recv_backward` 在 last stage 短路；两个 backward 原语还在推理模式整体短路。
- `_free_tensors` 用 `tensor.data = torch.Tensor()` **软释放**显存并拒绝视图张量；`to_free` 里的张量恒定**晚一个提交周期**被释放——因为发送是延后提交的，必须等下一次 `_commit_and_wait_comm` 提交并 wait 完它的 isend 之后才能回收。
- 四个组合操作 `_forward_chunk` / `_backward_chunk` / `_forward_backward_chunk` / `_weight_chunk` 都遵循「recv → commit_wait → compute → send」节拍（`_weight_chunk` 例外，无计算原语，用 `WeightGradStore.pop()` 填气泡），并通过 `recv`/`send`/`recv0` 开关让八步调度精确控制收发。

## 7. 下一步学习建议

本讲把通信层和组合操作的全部零件备齐了，下一讲 **u3-l5「DualPipe 八步调度 `step()`」** 就是把这些零件按 8 段公式编排起来的总装车间。建议：

1. 先回头重读 `step()` 里 step 1～step 8 的循环（[dualpipe/dualpipe.py:358-425](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L358-L425)），把每一行映射到本讲的某个组合操作，你会发现 8 步调度其实就是这四个积木在不同 `recv`/`send`/`enable_zb` 参数下的排列。
2. 关注 step 4 主步里 `is_middle_rank` 的特殊分支（L385-391）——它故意**不**用 `_forward_backward_chunk` 重叠，与本讲 4.4 的「正常重叠节拍」形成对比，体会 middle rank 为何要进一步压缩气泡。
3. 想理解零气泡在调度层面的落点，重点对照 step 3/6/7 里的 `_weight_chunk` 与 `_backward_chunk(..., enable_zb=True)` 调用——它们正是本讲 `_weight_chunk` 与 `enable_zb` 开关在真实调度中的使用现场（结合 u2-l4 的 WeightGradStore）。
