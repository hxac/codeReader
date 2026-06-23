# tensorize_instructions：序列化为指令张量

## 1. 本讲目标

学完本讲后，你应该能够：

1. 说清楚 `INTS_PER_INSTRUCTION = 32` 这个常量的来源，以及 `serialize_and_pad` 如何把任意一条指令补齐到恰好 32 个 int。
2. 解释为什么要把不同 SM（流多处理器）的指令队列用 `NoOp` 补到等长。
3. 画出 `globs.instructions` 张量的形状 `[num_sms, queue_len, 32]` 和 `globs.timings` 张量的形状 `[num_sms, queue_len, 128]`，并说明每一维分别对应什么。
4. 能够手算「两条指令 → 一维 int 列表」的 flatten 过程，并解释 `timings` 为何是 128 个槽。

本讲是把「调度好的指令对象」变成「GPU 内核能直接读取的张量」的最后一道工序。

---

## 2. 前置知识

在进入本讲前，你需要大致了解以下内容（不熟悉也没关系，下面会用通俗语言再讲一遍）：

- **指令对象（`Instruction`）**：Megakernels 用 Python 的 `@dataclass` 描述每一条算子指令，比如「对第 0 层做 layernorm + qkv」。每条指令都有一组字段（`layer_idx` 等），并能通过 `serialize()` 方法把自己拍平成一个 int 列表。这部分在 **u3-l1（BaseGlobals 与 Instruction）** 里有完整讲解。
- **SM 队列（`list[list[Instruction]]`）**：上一讲 **u4-l1（assign_to_sms）** 把一个 DAG 调度结果分配到各个 SM 上，每个 SM 得到一个指令队列。因为分配方式（`rr` / `zz` / `wave` / `dag` / `pool`）不同，**各 SM 队列的长度往往不相等**。这正是本讲要解决的麻烦。
- **张量（Tensor）**：这里就是「多维数组」。`torch.tensor` 把一维列表变成数组，`.view(...)` 再把它重新解释成多维形状。
- **GPU 线程与 lane**：GPU 上一个 warp 由 32 个线程（lane 0–31）组成。后面会看到，**「32 个 int 对应 32 个 lane，每个 lane 读一个 int」** 正是 `INTS_PER_INSTRUCTION = 32` 的物理来源。

> 一句话回顾 u4-l1 的产物：`assign_to_sms(...)` 返回 `list[list[Instruction]]`——外层长度是 `num_sms`，每个内层 list 是某一条 SM 要执行的指令序列。本讲 `tensorize_instructions` 的输入就是它。

---

## 3. 本讲源码地图

本讲涉及的文件如下：

| 文件 | 在本讲中的作用 |
|---|---|
| [megakernels/scheduler.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py) | **主角**。包含 `INTS_PER_INSTRUCTION`、`TIMING_SLOTS`、`serialize_and_pad`、`tensorize_instructions` 全部核心逻辑。 |
| [megakernels/instructions.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/instructions.py) | 提供 `Instruction.serialize()`（拍平逻辑）和 `NoOp`（空指令，用于补齐）。 |
| [include/config.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/config.cuh) | GPU 侧常量 `INSTRUCTION_WIDTH = 32`、`TIMING_WIDTH = 128`，解释「为什么是 32 和 128」。 |
| [include/controller/instruction_fetch.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/instruction_fetch.cuh) | GPU 控制器如何按 `[sm_id, instruction_index, 0..31]` 读取 32 个 int，印证张量形状。 |
| [include/controller/timings_store.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/timings_store.cuh) | GPU 控制器如何把 128 个 timing 槽写回显存，印证 `timings` 张量形状。 |

> Python 侧（调度器）负责「造」张量，C++ 侧（内核）负责「读 / 写」张量。本讲会两边对照着讲，这样 32 和 128 就不再是魔法数字。

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

- **4.1 `serialize_and_pad`**：把一条指令拍平并补齐到 32 个 int。
- **4.2 NoOp 补齐**：让所有 SM 队列等长。
- **4.3 `instructions` 张量**：形状 `[num_sms, queue_len, 32]`。
- **4.4 `timings` 张量**：形状 `[num_sms, queue_len, 128]`。

### 4.1 serialize_and_pad：一条指令 = 32 个 int

#### 4.1.1 概念说明

调度阶段产生的指令是「对象」，例如 `LayerNorm_QKV_MatVecRopeAppend(layer_idx=0, ...)`。对象里有字段名、有类型，对 Python 很友好，但 GPU 内核只认「一串连续的 int」。所以需要两步：

1. **`serialize()`**：把对象的字段按固定顺序拍平成一维 int 列表。不同的指令字段数不同，所以拍平后的长度也不同（可能 4 个、5 个、十几个）。
2. **`serialize_and_pad()`**：在拍平结果后面补 `0`，**强行让每条指令都占满 32 个 int**。

为什么是 32？因为 GPU 上一个 warp 正好 32 个 lane。后面（4.1.3 的 C++ 侧）会看到，控制器 warp 用「lane `i` 读第 `i` 个 int」的方式一次性把整条指令搬进共享内存，32 个 lane 各搬一个字，恰好搬完一条指令。

#### 4.1.2 核心流程

`serialize()` 的拍平规则（字段逐个处理，跳过 `global_idx`）：

- 起手放一个 `opcode()`（操作码，区分指令种类）。
- 字段是 `int` → 直接追加 1 个 int。
- 字段是 `list` / `tuple` → 先追加「长度」，再追加每个元素。
- 字段是 `None` → 追加一个 `0`（占位）。

`serialize_and_pad()` 的补齐规则：

\[
\text{num\_padding} = 32 - \text{len}(\text{serialized})
\]

只要 `num_padding >= 0`，就在末尾追加 `num_padding` 个 `0`，使总长度恒为 32。

> 这是一个**硬约束**：任何指令 `serialize()` 出来的长度都不能超过 32，否则 `assert num_padding >= 0` 直接报错。换句话说，单条指令的「字段 + opcode + 列表元素总数」不能超过 31。

伪代码：

```
serialize_and_pad(instruction):
    words = instruction.serialize()      # 长度不固定，1 ~ 32
    pad   = 32 - len(words)
    assert pad >= 0
    return words + [0] * pad             # 长度恒为 32
```

#### 4.1.3 源码精读

常量定义在文件顶部：

[megakernels/scheduler.py:13-14](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py#L13-L14) —— `INTS_PER_INSTRUCTION = 32`（每条指令占 32 个 int）和 `TIMING_SLOTS = 128`（每条指令的 timing 槽数）。

补齐函数本体：

[megakernels/scheduler.py:274-278](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py#L274-L278) —— 先 `serialize()`，再算补几个 0，断言不能超长，最后拼上 0。

拍平规则在基类里：

[megakernels/instructions.py:97-119](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/instructions.py#L97-L119) —— `words = [self.opcode()]` 开头放操作码；随后遍历字段，`int` 直接加、`list/tuple` 加长度再加元素、`None` 加 0；遇到不支持类型抛 `ValueError`。

`NoOp` 的拍平结果恒为 `[0]`：

[megakernels/instructions.py:122-126](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/instructions.py#L122-L126) —— `NoOp` 没有任何字段，`opcode()` 返回 0，所以 `serialize()` 返回 `[0]`，补齐后就是 32 个 0。

**为什么是 32（GPU 侧证据）**：

[include/config.cuh:14-15](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/config.cuh#L14-L15) —— `INSTRUCTION_WIDTH = 32`，注释写明「128 bytes per instruction」（32 个 int × 4 字节 = 128 字节）。

[include/controller/instruction_fetch.cuh:24-26](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/instruction_fetch.cuh#L24-L26) —— 控制器 warp 里 `if (laneid < INSTRUCTION_WIDTH) instruction[laneid] = src_ptr[laneid];`，也就是 **32 个 lane 各搬一个 int**。这就是 Python 侧必须补齐到 32 的根本原因。

#### 4.1.4 代码实践

**实践目标**：亲手验证 `serialize_and_pad` 把不同长度的拍平结果都补成 32。

**操作步骤**（在仓库根目录，需要已安装 `torch`）：

1. 想象两条指令：
   - `NoOp()`：`serialize()` = `[0]`（1 个 int）。
   - `LayerNorm_QKV_MatVecRopeAppend(layer_idx=0, start_output_block_idx=0, end_output_block_idx=4)`：`opcode()` = 1，3 个 int 字段，`serialize()` = `[1, 0, 0, 4]`（4 个 int）。
2. 分别套用 `serialize_and_pad`：
   - `NoOp` → `[0]` + 31 个 0 = 32 个 0。
   - `LayerNorm...` → `[1, 0, 0, 4]` + 28 个 0 = 32 个 int。
3. （可选）在仓库里跑一小段确认：

```python
# 示例代码：在仓库根目录 python 中运行
from megakernels.scheduler import serialize_and_pad, INTS_PER_INSTRUCTION
from megakernels.instructions import NoOp
from megakernels.demos.latency.instructions import LayerNorm_QKV_MatVecRopeAppend

print(INTS_PER_INSTRUCTION)                       # 32
print(serialize_and_pad(NoOp()))                  # [0]*32
print(len(serialize_and_pad(NoOp())))             # 32
ins = LayerNorm_QKV_MatVecRopeAppend(0, 0, 4)
print(serialize_and_pad(ins)[:6])                 # [1, 0, 0, 4, 0, 0]
print(len(serialize_and_pad(ins)))                # 32
```

**需要观察的现象**：无论原始 `serialize()` 是 1 个还是 4 个 int，`serialize_and_pad` 的输出长度都恰好是 32，前几个是有效字段、后面全是 0。

**预期结果**：`len(...)` 恒为 32。若你构造的字段太多导致 `serialize()` 超过 32，会触发 `assert num_padding >= 0` 报错。（这段示例依赖仓库环境，实际输出待本地验证。）

#### 4.1.5 小练习与答案

**练习 1**：`AttentionReduction` 有一个 `reduction_list: list[int]` 字段。假设 `reduction_list = [2, 3, 5]`，它会让 `serialize()` 多出几个 int？

**答案**：多出 `1（长度）+ 3（元素）= 4` 个 int。`serialize()` 对 list 的处理是「先写长度，再写元素」（见 [instructions.py:110-112](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/instructions.py#L110-L112)）。所以 list 越长，越容易逼近 32 的上限。

**练习 2**：如果某条指令 `serialize()` 出来是 33 个 int，`serialize_and_pad` 会怎样？

**答案**：`num_padding = 32 - 33 = -1`，`assert num_padding >= 0` 失败，抛 `AssertionError`。这告诉你单条指令的编码不能超过 32 个 int，是设计上的硬约束。

---

### 4.2 NoOp 补齐：让所有 SM 队列等长

#### 4.2.1 概念说明

`assign_to_sms`（u4-l1）返回的是 `list[list[Instruction]]`，每个内层 list 是一条 SM 的队列。**问题**：不同分配模式下，各 SM 队列长度几乎不可能完全相等。比如 `round_robin` 在 `num_sms=4`、共 10 条指令时，队列长度分别是 3、3、2、2。

可张量必须是「规整的矩形」——你不能把「长度不一的几个 list」直接塞进一个三维张量。解决办法很朴素：**找到最长的那个队列，把所有比它短的队列用 `NoOp`（空操作）补到一样长**。

`NoOp` 是天然的填充剂：它的 `opcode()` 是 0，GPU 控制器读到操作码 0 就什么都不干（[include/noop.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/noop.cuh) 里就是空实现），而且它 `serialize()` 恒为 `[0]`，补齐后是全 0，不会污染任何字段。

#### 4.2.2 核心流程

```
max_queue_len = max(各队列长度)
for queue in instruction_queues:
    queue.extend([NoOp()] * (max_queue_len - len(queue)))   # 末尾补 NoOp
```

补齐后：

- 所有 SM 队列长度都等于 `max_queue_len`。
- 原 DAG 指令在前，`NoOp` 在后。
- 控制器在每个 SM 上都跑满 `max_queue_len` 步，多出来的步是空转（NoOp），不影响正确性。

> 注意：补齐是「就地修改」输入的 `instruction_queues`（用 `queue.extend`）。所以调用 `tensorize_instructions` 之后，传入的队列会被改写。这是一个值得注意的副作用。

#### 4.2.3 源码精读

[megakernels/scheduler.py:287-289](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py#L287-L289) —— 先算 `max_queue_len`（最长队列），再对每条队列 `extend` 上 `(max_queue_len - len(queue))` 个 `NoOp()`，把短队列拉齐。

为什么 `NoOp` 适合补齐？看它的定义：

[megakernels/instructions.py:122-126](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/instructions.py#L122-L126) —— `NoOp` 不声明任何字段，`opcode()` 返回 0，`serialize()` 返回 `[0]`。所以补齐出来的内容是「操作码 0 + 全 0 字段」，对内核而言就是「读到了一条什么也不做的指令」。

#### 4.2.4 代码实践

**实践目标**：观察补齐前后队列长度的变化。

**操作步骤**：

1. 假设 `num_sms = 3`，三条队列长度分别是 4、4、1（模拟某次 `dag` 分配的不均衡）。
2. 手算：`max_queue_len = 4`，三条队列补齐后长度都变成 4。第三条队列末尾被追加 `4 - 1 = 3` 个 `NoOp()`。
3. （可选）在仓库里跑：

```python
# 示例代码
from megakernels.scheduler import INTS_PER_INSTRUCTION
from megakernels.instructions import NoOp, Instruction

queues = [[object(), object(), object(), object()],
          [object(), object(), object(), object()],
          [object()]]                  # 长度 4, 4, 1
max_len = max(len(q) for q in queues)  # 4
for q in queues:
    q.extend([NoOp()] * (max_len - len(q)))
print([len(q) for q in queues])        # [4, 4, 4]
```

**需要观察的现象**：补齐前长度不齐，补齐后全部等于最大值；被补的是末尾。

**预期结果**：`[len(q) for q in queues]` 变成 `[4, 4, 4]`。

#### 4.2.5 小练习与答案

**练习 1**：为什么补齐时追加的是 `NoOp()`，而不是把多余指令平均分给别的 SM？

**答案**：因为各 SM 队列在分配阶段就已经确定，不能移动指令（移动会破坏依赖与负载均衡）。补齐只是为了让张量成为矩形——`NoOp` 不做任何计算，既能让形状对齐，又不改变语义，是最廉价的「填充剂」。

**练习 2**：补齐会改变 `max_queue_len` 的值吗？

**答案**：不会。`max_queue_len` 是补齐前最长队列的长度，补齐只是把短队列拉到这个值；最长队列补 0 个 `NoOp`，`max_queue_len` 不变。

---

### 4.3 instructions 张量：形状 [num_sms, queue_len, 32]

#### 4.3.1 概念说明

补齐 + 逐条 `serialize_and_pad` 之后，所有指令都变成 32 个 int。接下来把它们拼成一个大张量，挂到 `globs.instructions` 上，交给 GPU 内核。

最终形状是：

\[
\text{instructions.shape} = [\,\text{num\_sms},\ \text{queue\_len},\ 32\,]
\]

每一维的含义：

| 维度 | 大小 | 含义 |
|---|---|---|
| 第 0 维 | `num_sms` | 每条 SM 一个独立的指令区，互不干扰。 |
| 第 1 维 | `queue_len`（= `max_queue_len`） | 每个 SM 要执行的指令条数（含 NoOp）。 |
| 第 2 维 | `32` | 每条指令的 32 个 int 字（操作码 + 字段 + 填充）。 |

#### 4.3.2 核心流程

```
flattened = []
for queue in instruction_queues:                       # 逐 SM
    for instruction in queue:                          # 逐指令
        flattened.extend(serialize_and_pad(instruction))  # 追加 32 个 int
# flattened 总长度 = num_sms * queue_len * 32
serialized = torch.tensor(flattened, dtype=int32).view(num_sms, -1, 32)
```

关键点：

- 先把**所有 SM、所有指令**拍平成**一维** int 列表，顺序是「SM0 的指令依次排完 → SM1 的指令依次排完 → …」。
- 每条指令贡献 32 个 int，所以总长度一定是 32 的倍数。
- `.view(num_sms, -1, 32)` 把一维列表重新解释成三维：第 0 维是 `num_sms`，第 2 维是 32，中间的 `-1` 由 PyTorch 自动推断为 `queue_len`。
- `dtype=torch.int32` 与 GPU 侧 `int`（4 字节）对应。

#### 4.3.3 源码精读

拼接与 reshape：

[megakernels/scheduler.py:291-299](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py#L291-L299) —— 外层遍历队列、内层 `serialize_and_pad` 每条指令并 `extend` 进 `flattened`；然后 `torch.tensor(..., dtype=torch.int32, device=device).view(num_sms, -1, INTS_PER_INSTRUCTION)` 得到 `[num_sms, queue_len, 32]`。

写回 globals：

[megakernels/scheduler.py:307](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py#L307) —— `globs.instructions = serialized`，把这个张量挂到全局对象上，后续内核启动时直接读取。

**GPU 侧如何对上这个形状**：控制器读取指令时按 `(SM id, 指令序号, 字内偏移)` 三元组定位——

[include/controller/instruction_fetch.cuh:16-17](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/instruction_fetch.cuh#L16-L17) —— `src_ptr = &g.instructions[coord{get_worker_id(), instruction_index, 0}]`，`get_worker_id()` 就是 SM id，选第 0 维；`instruction_index` 选第 1 维（即 `queue_len` 那一维）；最内层 0..31 选第 2 维的 32 个字。三者正好对应 `[num_sms, queue_len, 32]`。

[include/controller/instruction_fetch.cuh:34](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/instruction_fetch.cuh#L34) —— `int num_iters = g.instructions.rows();`，控制器在这个 SM 上要跑 `queue_len` 步。

#### 4.3.4 代码实践

**实践目标**：验证 `flattened` 的总长度和 `view` 后的形状。

**操作步骤**：

1. 设 `num_sms = 2`，补齐后两条队列长度都是 `queue_len = 3`（每条 3 个指令，含 NoOp）。
2. 每条指令 32 个 int，所以 `flattened` 总长度 = `2 * 3 * 32 = 192`。
3. `.view(2, -1, 32)` → 中间维自动推断为 `192 / (2 * 32) = 3` → 形状 `[2, 3, 32]`。
4. （可选）用纯 PyTorch 复现这个 reshape（不依赖项目对象）：

```python
# 示例代码
import torch
num_sms, queue_len, ints = 2, 3, 32
flattened = list(range(num_sms * queue_len * ints))   # 192 个 int
t = torch.tensor(flattened, dtype=torch.int32).view(num_sms, -1, ints)
print(t.shape)        # torch.Size([2, 3, 32])
```

**需要观察的现象**：`-1` 维被推断成 3；形状正好是 `[num_sms, queue_len, 32]`。

**预期结果**：`torch.Size([2, 3, 32])`。

#### 4.3.5 小练习与答案

**练习 1**：如果 `view(num_sms, -1, INTS_PER_INSTRUCTION)` 里把 `-1` 写成具体的 `queue_len`，效果一样吗？

**答案**：一样。`-1` 只是让 PyTorch 自动推断；只要 `flattened` 的总长度 = `num_sms * queue_len * 32`，写成 `view(num_sms, queue_len, 32)` 结果完全相同。用 `-1` 的好处是不必显式传 `max_queue_len`，减少出错。

**练习 2**：为什么 `flattened` 的总长度一定是 32 的倍数？

**答案**：因为每条指令经 `serialize_and_pad` 后都恰好 32 个 int，`extend` 时整条整条地加。所以 `len(flattened) = 指令总数 × 32`，必为 32 的倍数，`view` 不会报错。

---

### 4.4 timings 张量：形状 [num_sms, queue_len, 128]

#### 4.4.1 概念说明

除了指令本身，调度器还会分配一个 **timing 张量**，形状是：

\[
\text{timings.shape} = [\,\text{num\_sms},\ \text{queue\_len},\ 128\,]
\]

前两维和 `instructions` 完全一致（每个 SM、每条指令一个位置），第三维是 **128 个 timing 槽**。它的作用是：GPU 内核在执行每条指令时，可以把若干个「时间事件」的耗时（用 `clock64()` 量出的周期差）写进这 128 个槽里，运行结束后回传给 CPU 做性能分析。

所以第三维的 **128 = `TIMING_WIDTH` = `TIMING_SLOTS`**，对应一条指令上最多能记录 128 个时间事件。

#### 4.4.2 核心流程

```
timings = torch.zeros([num_sms, max_queue_len, 128], dtype=int32, device=device)
globs.timings = timings
```

要点：

- 用 `torch.zeros` 初始化为全 0（不像 `instructions` 那样有实际数据）。
- 第三维写死成 `TIMING_SLOTS = 128`（[scheduler.py:14](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py#L14)），和 GPU 侧 `TIMING_WIDTH = 128` 对齐。
- 前两维必须和 `instructions` 一一对应：`(sm_id, instruction_index)` 在两个张量里指向「同一条指令」。

#### 4.4.3 源码精读

分配 timing 张量：

[megakernels/scheduler.py:301-308](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py#L301-L308) —— `torch.zeros([num_sms, max_queue_len, TIMING_SLOTS], dtype=torch.int32, device=device)`，然后 `globs.timings = timings`。

**为什么是 128（GPU 侧证据）**：

[include/config.cuh:18-19](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/config.cuh#L18-L19) —— `TIMING_WIDTH = 128`，`using timing_t = int[TIMING_WIDTH]`。每条指令在共享内存里配套一个 `int[128]` 的 timing 区（见 [include/util.cuh:11-19](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L11-L19) 的 `instruction_state_t`，里面同时持有 `instructions` 和 `timings`）。

事件编号占用情况（说明 128 够用且有冗余）：

[include/util.cuh:215-246](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L215-L246) —— 定义了一组 `TEVENT_*` 常量（控制器 / loader / launcher / storer / consumer 的起止事件、`FREE_SLOTS_START = 55`、triples 相关事件到 125），全部落在 `[0, 128)` 内。内核用 `record(event_id)` 把周期差写进 `timing()[event_id]`（[include/util.cuh:190-196](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L190-L196)）。

内核把 timing 区写回显存时，也是按 `(sm_id, instruction_index)` 定位、整块 128 个 int 一次拷回：

[include/controller/timings_store.cuh:11-23](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/timings_store.cuh#L11-L23) —— `dst_ptr = &g.timings[coord{get_worker_id(), instruction_index, 0}]`，拷贝 `TIMING_WIDTH * sizeof(int)` 字节（= 128 个 int）。这与 Python 侧 `[num_sms, queue_len, 128]` 完全对应。

> 小细节：`record()` 受 `if constexpr (config::TIMING_RECORD_ENABLED)` 保护，而 `TIMING_RECORD_ENABLED` 默认是 `false`（[config.cuh:46](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/config.cuh#L46)）。也就是说默认不开计时，`timings` 会一直保持全 0——但张量**照样要分配**，因为 `store_timings_and_reset` 会无条件执行那块 128-int 的 DMA（[controller/timings_store.cuh:26-46](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/timings_store.cuh#L26-L46)），需要一块合法的目标内存。

#### 4.4.4 代码实践

**实践目标**：解释 `timings` 为什么是 `[num_sms, queue_len, 128]`，并验证三处的「128」一致。

**操作步骤**：

1. 对照三处常量：
   - Python：`TIMING_SLOTS = 128`（[scheduler.py:14](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py#L14)）。
   - C++：`TIMING_WIDTH = 128`（[config.cuh:18](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/config.cuh#L18)）。
   - DMA 字节数：`TIMING_WIDTH * sizeof(int) = 128 * 4`（[timings_store.cuh:13](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/controller/timings_store.cuh#L13)）。
2. 解释三句话：
   - **第 0 维 = `num_sms`**：每条 SM 独立计时，`get_worker_id()`（SM id）选这一维。
   - **第 1 维 = `queue_len`**：每条指令单独计时，`instruction_index` 选这一维。
   - **第 2 维 = `128`**：一条指令上最多 128 个时间事件槽，`event_id` 选这一维。
3. （可选）在仓库里确认 Python 端形状：

```python
# 示例代码：模拟 timings 分配（不依赖真实 globals）
import torch
num_sms, queue_len, slots = 4, 10, 128
timings = torch.zeros([num_sms, queue_len, slots], dtype=torch.int32)
print(timings.shape)   # torch.Size([4, 10, 128])
print(timings.dtype)   # torch.int32
```

**需要观察的现象**：三处「128」数值一致；前两维与 `instructions` 同形；`timings` 初值全 0。

**预期结果**：形状 `torch.Size([4, 10, 128])`、`dtype=torch.int32`、元素全 0。

#### 4.4.5 小练习与答案

**练习 1**：`timings` 为什么不也走 `serialize_and_pad` 那种「不定长 + 补 0」的方式，而是固定 128？

**答案**：因为 GPU 侧要用 `record(event_id)` 直接按下标写、再用一次定长 bulk DMA（`cp.async.bulk`，固定 `TIMING_WIDTH*4` 字节）整块拷回。定长 128 让这两步都能用静态已知的字节量，避免运行时长度协商；而指令走补齐是因为指令字段本就因种类而异，补齐后才好做「32 lane 各搬一字」的并行加载。两者需求不同。

**练习 2**：默认配置（`TIMING_RECORD_ENABLED = false`）下，`timings` 张量最终会是什么值？还需要分配它吗？

**答案**：值会是全 0（因为 `record()` 被编译期关闭，没人往里写周期数；而 `store_timings_and_reset` 每步又会把它清零）。但仍然必须分配——因为内核里 `store_timings_and_reset` 会无条件对这块 128-int 内存做 DMA 写回，缺了它就是非法显存访问。所以 `timings` 是「即使不计时也要存在」的对齐用缓冲区。

---

## 5. 综合实践

把四个模块串起来，完成一个小任务：**给定两条指令和两个 SM，手算并验证 `tensorize_instructions` 的全部产物**。

### 任务设定

- `num_sms = 2`。
- 两条队列（已补齐前）：
  - SM0：`[LayerNorm_QKV_MatVecRopeAppend(layer_idx=2, start_output_block_idx=0, end_output_block_idx=8)]`（1 条真指令）
  - SM1：`[NoOp()]`（1 条，纯占位）
- 约定 `LayerNorm_QKV_MatVecRopeAppend` 的 `opcode() = 1`。

### 第 1 步：补齐（模块 4.2）

- `max_queue_len = max(1, 1) = 1`。
- 两条队列本来就等长，无需补 `NoOp`。补齐后仍各 1 条。

> （如果你想体会补齐，可把 SM1 改成空队列 `[]`，则 `max_queue_len = 1`，SM1 会被补 1 个 `NoOp()`。）

### 第 2 步：逐条 serialize_and_pad（模块 4.1）

- SM0 那条：`serialize()` = `[1, 2, 0, 8]`（opcode 1 + 三个 int 字段）→ 补 28 个 0 → 32 个 int：
  `[1, 2, 0, 8, 0, 0, …(共 28 个 0)…]`。
- SM1 那条 `NoOp`：`serialize()` = `[0]` → 补 31 个 0 → 32 个 0：
  `[0, 0, …(共 32 个 0)…]`。

### 第 3 步：flatten 与 view（模块 4.3）

按「SM0 全部指令 → SM1 全部指令」顺序拼接：

\[
\text{flattened} = \underbrace{[1,2,0,8,0,\dots]}_{32\text{ 个}} \;+\; \underbrace{[0,0,\dots]}_{32\text{ 个}} \quad\Rightarrow\quad \text{总长 } = 64
\]

验证：**每条指令恰好占 32 个 int**（32 + 32 = 64，没有零头）。

`view(num_sms=2, -1, 32)`：`64 / (2 × 32) = 1` → 形状 `[2, 1, 32]`，即 `num_sms=2, queue_len=1, ints=32`。

### 第 4 步：分配 timings（模块 4.4）

`timings = torch.zeros([num_sms=2, max_queue_len=1, 128])` → 形状 `[2, 1, 128]`，全 0。

### 第 5 步：自检问题

1. 如果 SM0 有 2 条真指令、SM1 有 0 条，`max_queue_len` 是多少？`timings` 形状是什么？
   - 答：`max_queue_len = 2`；`timings` 形状 `[2, 2, 128]`；SM1 会被补 2 个 `NoOp`；`instructions` 形状 `[2, 2, 32]`。
2. `flattened` 总长 = `num_sms × queue_len × 32`，本任务里 = `2 × 1 × 32 = 64`，与第 3 步一致 ✓。
3. （可选）在仓库根目录跑下面脚本，把你手算的 `[2,1,32]` / `[2,1,128]` 和真实输出对照：

```python
# 示例代码：用项目对象真实跑一遍（依赖 torch 与仓库环境）
from megakernels.scheduler import tensorize_instructions, INTS_PER_INSTRUCTION, TIMING_SLOTS
from megakernels.instructions import NoOp
from megakernels.demos.latency.instructions import LayerNorm_QKV_MatVecRopeAppend

# 注意：真实调用需要一个 BaseGlobals 实例（globs）。这里只演示「形状」部分，
# 完整调用请参考 megakernels/scripts/generate.py 中 schedule_builder.build + assign_to_sms + tensorize_instructions 的用法。
print("INTS_PER_INSTRUCTION =", INTS_PER_INSTRUCTION)  # 32
print("TIMING_SLOTS         =", TIMING_SLOTS)          # 128
```

> 说明：`tensorize_instructions` 的第一个参数是 `globs: BaseGlobals`，单跑需要先 `build` 出 schedule（见 [megakernels/scripts/generate.py:139-144](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/generate.py#L139-L144)）。本任务的手算部分是确定性的，可直接验证；脚本输出待本地验证。

---

## 6. 本讲小结

- `serialize_and_pad` 把任意一条指令 `serialize()` 后补 0，**强制每条指令占满 32 个 int**；超过 32 会 `assert` 失败。
- `32` 不是随便选的：GPU 控制器 warp 的 32 个 lane 各搬一个 int（`instruction_fetch.cuh`），Python 侧 `INTS_PER_INSTRUCTION = 32` 与 C++ 侧 `INSTRUCTION_WIDTH = 32` 严格对齐。
- 各 SM 队列长度不一，用 `NoOp`（`opcode = 0`、`serialize = [0]`）把短队列补到 `max_queue_len`，让张量成为规整矩形；补齐会**就地修改**输入队列。
- `globs.instructions` 形状为 `[num_sms, queue_len, 32]`：先 flatten 成一维 int 列表（SM0→SM1→…），再 `.view(num_sms, -1, 32)`，`-1` 自动推断成 `queue_len`。
- `globs.timings` 形状为 `[num_sms, queue_len, 128]`：前两维与 `instructions` 一一对应，第三维的 `128 = TIMING_SLOTS = TIMING_WIDTH`，对应一条指令上 128 个时间事件槽。
- `timings` 即便默认不计时（`TIMING_RECORD_ENABLED = false`）也要分配，因为内核会无条件对它做定长 DMA 写回。

---

## 7. 下一步学习建议

- **跟踪整条流水线**：回到 [megakernels/scripts/generate.py:139-144](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/generate.py#L139-L144)，看 `build → assign_to_sms → tensorize_instructions` 三步如何串起来，本讲是其中的最后一步。
- **进入内核侧**：下一步可以读 `include/controller/instruction_fetch.cuh`（控制器怎么逐条取指、怎么用 `g.instructions.rows()` 控制循环）和 `include/controller/timings_store.cuh`（timing 怎么写回），把「Python 造张量 ↔ C++ 读 / 写张量」的对应关系彻底打通。
- **理解指令内存布局**：结合 [include/util.cuh:11-19](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/include/util.cuh#L11-L19) 的 `instruction_state_t`，看 `instructions`（32 int）和 `timings`（128 int）在共享内存里如何成对存放、如何随指令流水线（`INSTRUCTION_PIPELINE_STAGES = 2`）轮转。
- **补齐策略的延伸**：思考如果想让 `max_queue_len` 尽量小（减少 NoOp 空转），不同的 `assign_to_sms` 模式（`rr` / `wave` / `dag`，见 u4-l1）对均衡性的影响，这是一个值得动手实验的优化方向。
