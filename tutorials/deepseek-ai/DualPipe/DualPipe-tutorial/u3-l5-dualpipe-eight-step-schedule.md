# DualPipe 八步调度引擎 step()

## 1. 本讲目标

本讲是「DualPipe 引擎剖析」单元的收尾篇，把前四讲拆解的零件（rank 拓扑、状态缓冲、计算原语、通信原语与组合操作）装配成最终的调度引擎 `DualPipe.step()`。

学完后你应该能够：

1. 说清 `step()` 从入口校验、scatter 切分到八步调用的整体骨架。
2. 掌握驱动八步循环次数的三个核心量 `num_half_ranks`、`half_rank`、`half_num_chunks`，并能手算任意 rank 在每一步的循环次数。
3. 读懂主步 `nF0B1F1B0` 的全重叠执行，以及 middle rank（流水线对折点）为什么在这里被特殊处理。
4. 说清零气泡（zero bubble）在哪几步被启用、为什么 `step 8` 要在最后排空 `WeightGradStore`。
5. 解释最终 `loss` / `outputs` 如何在 first / last rank 上聚合并返回。

---

## 2. 前置知识

本讲是第 3 单元的综合，直接依赖以下已建立的认知（若不熟悉请先回看对应讲义）：

- **u3-l1 rank 拓扑**：每个进程持有两个镜像模块；`self.rank` 是逻辑 pp rank；`is_first_rank` / `is_last_rank` / `is_middle_rank`（流水线对折点）/ `is_in_second_half` 四个标志驱动调度分支。
- **u3-l2 状态与计算原语**：`_reset_states` 建立 `[phase][chunk_id]` 二维缓冲与进度计数器；`_forward_compute_chunk` / `_backward_compute_chunk` 是「取数 → 计算 → 置 None」的计算原语。
- **u3-l3 前反向重叠**：`_forward_backward_compute_chunk` 把一次前向和一次反向合并为同一调度单元；零气泡靠用户自定义 `autograd.Function` 与 `WeightGradStore.enabled` 开关握手实现。
- **u3-l4 通信原语与组合操作**：`_forward_chunk` / `_backward_chunk` / `_forward_backward_chunk` / `_weight_chunk` 四个组合操作都遵循「recv → commit_wait → compute → send」节拍；`_weight_chunk` 不做计算，只用 `WeightGradStore.pop()` 把延后的权重梯度填进气泡。
- **u2-l3 scatter/gather**：`scatter` 在 `step` 开头把整批输入切成微批次列表，`gather` 在结尾把各微批次输出聚合回整批。
- **u2-l4 WeightGradStore**：全静态类，`cache` 攒权重梯度函数、`flush` 入队、`pop` 开箱执行（FIFO）、`clear` 复位。

补充一个贯穿全讲的方向约定（源码注释 [dualpipe/dualpipe.py:355-356](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L355-L356)）：

- 前半段 rank：`phase 0` 表示 forward 方向，`phase 1` 表示 reverse 方向。
- 后半段 rank：方向定义互换，`phase 0` 表示 reverse，`phase 1` 表示 forward。

这一翻转由各原语开头的 `phase ^= self.is_in_second_half` 完成（见 u3-l4），让八步调度对所有 rank 都能写同一套循环。

---

## 3. 本讲源码地图

本讲全部源码集中在一个文件：

| 文件 | 本讲关注的内容 |
|------|--------------|
| `dualpipe/dualpipe.py` | `DualPipe.step()` 方法（294–440 行），包含入口校验、scatter、八步调度、loss/outputs 聚合与返回 |

涉及的概念性配图与示例：

| 文件 | 作用 |
|------|------|
| `README.md` | 八步调度对应的调度图 `images/dualpipe.png`（8 PP ranks、20 micro-batches）与气泡公式 |
| `examples/example_dualpipe.py` | 真实调用 `dualpipe_model.step(...)` 的入口，以及 loss / 梯度的校验逻辑 |

---

## 4. 核心概念与源码讲解

### 4.1 step 入口：校验、scatter 与输入分发

#### 4.1.1 概念说明

`step()` 是 DualPipe 对外的训练 / 推理入口。它的职责是：

1. 校验运行前提（张量形状已设置、rank 数与 chunk 数合法、首末 rank 提供了 criterion）。
2. 推断本次是「训练」还是「纯前向推理」（由 `torch.is_grad_enabled()` 决定）。
3. 把整批输入 / 标签 `scatter` 成微批次列表，并按 rank 身份分发到对应方向的缓冲。
4. 依次执行八步调度。
5. 在首末 rank 上聚合 `loss` 与（可选的）`outputs`，复位状态后返回。

#### 4.1.2 核心流程

```text
step(*inputs, num_chunks, criterion, labels, return_outputs)
  │
  ├─ 0. 校验 TENSOR_SHAPES / TENSOR_DTYPE 已设置
  ├─ 1. forward_only = not torch.is_grad_enabled()   # 推理 vs 训练
  ├─ 2. 校验 num_ranks 偶数、num_chunks 偶数且 ≥ num_ranks*2
  ├─ 3. 计算 num_half_ranks / half_rank / half_num_chunks
  ├─ 4. _reset_states()                              # 清 WeightGradStore、建缓冲
  ├─ 5. scatter(inputs/labels, half_num_chunks)      # 只切一半，另一半走反方向
  ├─ 6. 按 first/last rank 把 inputs/labels 放进对应 phase 缓冲
  │
  ├─ 7. 八步调度 step_1 … step_8
  │
  ├─ 8. _commit_and_wait_comm()                      # 提交残留通信
  ├─ 9. first/last rank：聚合 loss / outputs
  ├─ 10. _reset_states()                             # 清理本次状态
  └─ return loss, outputs
```

#### 4.1.3 源码精读

`step` 的签名与参数含义在文档字符串里写得很清楚：`inputs` 与 `criterion`、`labels` 只在 first / last rank 上需要，中间 rank 传 `None` 即可；返回的 `(loss, outputs)` 也只在首末 rank 有意义。

入口断言确保运行前提成立——必须先调用过 `set_p2p_tensor_shapes` / `set_p2p_tensor_dtype`：

[dualpipe/dualpipe.py:325-333](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L325-L333) — 校验张量形状已设置、`num_ranks` 为偶数、`num_chunks` 为偶数且不小于 `num_ranks * 2`。`forward_only` 由是否启用梯度推断，决定后续反向 / 权重梯度相关步骤是否短路。

接着是 scatter 与按 rank 分发。注意 `scatter` 只切 `half_num_chunks`（即 `num_chunks // 2`）份——因为另一半微批次走反方向，由对端 rank 喂入：

[dualpipe/dualpipe.py:340-353](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L340-L353) — first rank 把输入放进 `input_chunks` 的 phase 0（forward 方向）、标签放进 phase 1；last rank 反过来，输入进 phase 1、标签进 phase 0。这正对应「输入与标签分属不同方向、loss 在对端计算」的双向数据流（见 u2-l1）。

#### 4.1.4 代码实践

**实践目标**：确认中间 rank 也能安全调用 `step`，因为它不持有真实输入。

**操作步骤**：

1. 打开 `examples/example_dualpipe.py:144-156`，观察 `main` 里只有 `is_first_rank` / `is_last_rank` 构造了真实的 `x`、`l`，其余 rank 都是 `x = None; l = None`。
2. 对比 `dualpipe_model.step(x, num_chunks=num_chunks, criterion=criterion, labels=(l,), return_outputs=False)` 这一行对所有 rank 一视同仁地调用。

**需要观察的现象 / 预期结果**：中间 rank 传入 `None`，`scatter` 会把裸 `None` 包成 `(None,)`（见 u2-l3），`step` 内部不会因为中间 rank 没有输入而报错；首末 rank 之外的进程返回的 `loss` 恒为 `None`。这一点由 `example_dualpipe.py:159-165` 的 `assert loss is None`（对中间 rank）佐证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `num_chunks` 必须满足 `num_chunks % 2 == 0 and num_chunks >= num_ranks * 2`？

**参考答案**：`num_chunks` 必须能被 2 整除，是因为调度把微批次对半分给 forward / reverse 两个方向（`half_num_chunks = num_chunks // 2`）；不小于 `num_ranks * 2`，是因为主步 step 4 的循环次数公式 `step_4 = half_num_chunks - num_ranks + half_rank + 1` 对最小的 `half_rank = 0` 必须为正，即 `half_num_chunks >= num_ranks`，等价于 `num_chunks >= num_ranks * 2`，否则主步根本无法执行。

**练习 2**：`forward_only` 是怎么判定的？它会影响哪些步骤？

**参考答案**：`forward_only = not torch.is_grad_enabled()`，即只要外层处于 `torch.no_grad()` 上下文就判定为纯前向推理。它会让 `_backward_compute_chunk`、`_recv_backward`、`_send_backward`、`_weight_chunk` 提前 `return` 短路，所以推理模式下八步里所有反向 / 权重相关操作都被跳过。

---

### 4.2 驱动八步的三个核心量

#### 4.2.1 概念说明

八步调度的循环次数不是写死的常数，而是由 rank 在流水线中的「位置」决定。源码用三个量刻画这个位置：

- `num_half_ranks = num_ranks // 2`：流水线对折后的「半边」rank 数。
- `half_rank = min(rank, num_ranks - 1 - rank)`：当前 rank 距离最近一端有几步，是一个关于流水线中点对称的量。
- `half_num_chunks = num_chunks // 2`：每个方向上的微批次数。

关键直觉：`half_rank` 越靠近两端（越小），「灌水 / 排水」相关的步骤越多；越靠近中点（middle rank），主步 step 4 越长。但**每个 rank 做的总功是相同的**（都是 `half_num_chunks` 个前向 + 反向），只是这些功被分配到不同步骤里——这正是让所有设备保持忙碌、把气泡压到最小的负载均衡设计。

#### 4.2.2 核心流程

设 `H = num_half_ranks`、`h = half_rank`、`C = half_num_chunks`、`P = num_ranks = 2H`。八步循环次数公式为：

| 步骤 | 名称 | 循环次数公式 |
|------|------|------------|
| step 1 | nF0 | \( (H - h - 1) \times 2 \) |
| step 2 | nF0F1 | \( h + 1 \) |
| step 3 | nB1W1F1 | \( H - h - 1 \) |
| step 4 | nF0B1F1B0（主步） | \( C - P + h + 1 \) |
| step 5 | nB1F1B0 | \( H - h - 1 \) |
| step 6 | nB1B0 | \( h + 1 \) |
| step 7 | nWB0 | \( H - h - 1 \) |
| step 8 | nW | \( h + 1 \) |

观察到一个优美的对称结构：

- step 3 = step 5 = step 7 = \( H - h - 1 \)；
- step 2 = step 6 = step 8 = \( h + 1 \)；
- step 1 = \( 2 \times (H - h - 1) \)，恰是 step 3 的两倍；
- 主步 step 4 随 `h` 线性增长，中间 rank 最长。

**一致性校验**：每个 rank 在 forward 方向（首半段 rank 的 phase 0）总共要处理 `C` 个微批次。把所有「含 phase 0 前向」的步骤次数相加：

\[
\text{step}_1 + \text{step}_2 + \text{step}_4
= 2(H-h-1) + (h+1) + (C - 2H + h + 1)
= C
\]

代入 \(P = 2H\) 后化简正好等于 `half_num_chunks` \(C\)。这证明三个公式自洽——无论 rank 位置如何，前向微批次总数恒定。

#### 4.2.3 源码精读

三个量在 `step` 开头一次性算出，并缓存到实例上供后续步骤与组合操作使用：

[dualpipe/dualpipe.py:334-338](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L334-L338) — `half_rank = min(rank, num_ranks - 1 - rank)` 是关于流水线中点对称的量：rank 0 和 rank P-1 都是 0，rank 1 和 rank P-2 都是 1，依此类推。`self.num_half_ranks` / `self.half_rank` 被缓存，供组合操作（如 `_weight_chunk` 的零气泡填充）读取。

#### 4.2.4 代码实践

**实践目标**：用 Python 把八步循环次数公式抄出来，打印一张「rank × step」表，直观感受对称结构。

**操作步骤**（示例代码，非项目原有代码）：

```python
def step_counts(num_ranks, num_chunks, rank):
    num_half_ranks = num_ranks // 2
    half_rank = min(rank, num_ranks - 1 - rank)
    half_num_chunks = num_chunks // 2
    return {
        "step_1": (num_half_ranks - half_rank - 1) * 2,
        "step_2": half_rank + 1,
        "step_3": num_half_ranks - half_rank - 1,
        "step_4": half_num_chunks - num_ranks + half_rank + 1,
        "step_5": num_half_ranks - half_rank - 1,
        "step_6": half_rank + 1,
        "step_7": num_half_ranks - half_rank - 1,
        "step_8": half_rank + 1,
    }

# num_ranks=8, num_chunks=20
for rank in range(8):
    print(rank, step_counts(8, 20, rank))
```

**需要观察的现象 / 预期结果**：输出应如下表（按 `half_rank` 分组，rank 0↔7、1↔6、2↔5、3↔4 完全对称）：

| rank | half_rank | step_1 | step_2 | step_3 | step_4 | step_5 | step_6 | step_7 | step_8 |
|------|-----------|--------|--------|--------|--------|--------|--------|--------|--------|
| 0, 7 | 0 | 6 | 1 | 3 | 3 | 3 | 1 | 3 | 1 |
| 1, 6 | 1 | 4 | 2 | 2 | 4 | 2 | 2 | 2 | 2 |
| 2, 5 | 2 | 2 | 3 | 1 | 5 | 1 | 3 | 1 | 3 |
| 3, 4 | 3 | 0 | 4 | 0 | 6 | 0 | 4 | 0 | 4 |

注意 middle rank（rank 3、4，`half_rank = 3`）的 step 1/3/5/7 全为 0，主步 step 4 达到最大值 6；而端点 rank（rank 0、7）正好相反。这是「待本地验证」的计算结果，读者可自行运行上面脚本核对。

#### 4.2.5 小练习与答案

**练习 1**：对 `num_ranks=8, num_chunks=20`，验证任意 rank 的 `step_1 + step_2 + step_4` 都等于 10。

**参考答案**：以 `half_rank=2`（rank 2 或 5）为例：\(2 + 3 + 5 = 10 = \text{half\_num\_chunks}\)。其余 half_rank 同理：0→6+1+3=10；1→4+2+4=10；3→0+4+6=10。这说明前向方向上每个 rank 恰好处理 10 个微批次。

**练习 2**：如果把 `num_chunks` 从 20 调到 24（仍满足约束），哪些 step 的次数会变？

**参考答案**：只有 **step 4（主步）** 会变。因为 `step_4 = half_num_chunks - num_ranks + half_rank + 1`，`half_num_chunks` 从 10 变成 12，每一步 step 4 都增加 2；其余七步只依赖 `num_half_ranks` 和 `half_rank`，与 `num_chunks` 无关，故不变。这也说明微批次越多，主要是在主步的「稳态全重叠」阶段摊薄气泡，符合 README「微批次越多气泡占比越小」的结论。

---

### 4.3 八步调度逐段精读

#### 4.3.1 概念说明

八步调度对应 README 调度图 `images/dualpipe.png`（8 PP ranks、20 micro-batches）从左到右的几何区域。该图的总体形状是经典流水线调度：**左侧灌水三角形 → 中部稳态矩形 → 右侧排水三角形**，两端各有三角形气泡（设备在灌水 / 排水期的空闲）。八步与图区域的对应关系如下（精确的逐格映射请打开配图对照）：

| 步骤 | 在调度图中的区域 | 主要做的事 |
|------|----------------|-----------|
| step 1 nF0 | 左侧灌水三角形的主体 | 只做 forward，把流水线逐步灌满 |
| step 2 nF0F1 | 灌水三角形末段，开始喂入反方向 | forward 双方向同时启动 |
| step 3 nB1W1F1 | 灌水与稳态的过渡带 | 反向 + 零气泡权重梯度 + 前向 三者重叠 |
| step 4 nF0B1F1B0 | 中部稳态矩形（面积最大） | 前向与反向完全重叠（F&B） |
| step 5 nB1F1B0 | 稳态与排水的过渡带 | 反向与反向前向重叠 |
| step 6 nB1B0 | 右侧排水三角形主体 | 两个方向的反向，后半段启用零气泡 |
| step 7 nWB0 | 排水三角形末段 | 零气泡权重梯度 + 反向重叠 |
| step 8 nW | 最右侧收尾小三角 | 排空剩余权重梯度（纯 W） |

> 说明：本表描述的是调度图「左侧三角形 → 中部矩形 → 右侧三角形」的宏观分区与各步的职责，而非逐格像素映射。读者应打开 `images/dualpipe.png` 结合 F（黄）/ B（绿）/ W（浅蓝）/ F&B（橙）图例对照阅读。

#### 4.3.2 核心流程

八步的总览（伪代码，省略循环体细节）：

```text
# step 1: nF0 —— 纯前向 warm-up
for i in range((H-h-1)*2):     _forward_chunk(0)

# step 2: nF0F1 —— 喂入反方向前向
_recv_forward(0)
for i in range(h+1):
    _forward_chunk(0, recv=False, send=is_middle_rank)
    _recv_forward(0)
    _forward_chunk(1, send=(not is_middle_rank) or (i < h))
    if not is_middle_rank: _send_forward(0)

# step 3: nB1W1F1 —— 反向+零气泡W+前向 重叠
for i in range(H-h-1):
    _backward_chunk(1, enable_zb=True)
    _recv_forward(1)
    _weight_chunk()
    _forward_chunk(1, recv=False)

# step 4: nF0B1F1B0 —— 主步，全重叠
for i in range(C-P+h+1):
    if i==0 and is_middle_rank:  # 中点特判：暂不重叠，进一步缩气泡
        _forward_chunk(0, recv=False, send=False); _send_forward(1)
        _backward_chunk(1, send=False); _send_forward(0); _send_backward(1)
    elif i==0:                  _forward_backward_chunk(0, 1, recv0=False)
    else:                       _forward_backward_chunk(0, 1)
    _forward_backward_chunk(1, 0)

# step 5: nB1F1B0 —— 过渡
for i in range(H-h-1):
    _backward_chunk(1)
    _forward_backward_chunk(1, 0)

# step 6: nB1B0 —— 双向反向，后半段开零气泡
enable_zb = False
for i in range(h+1):
    if i==(h+1)//2 and h%2==1: enable_zb=True
    _backward_chunk(1, enable_zb=enable_zb)
    if i==(h+1)//2 and h%2==0: enable_zb=True
    _backward_chunk(0, enable_zb=enable_zb)

# step 7: nWB0 —— 零气泡W + 反向
for i in range(H-h-1):
    _weight_chunk()
    _backward_chunk(0, enable_zb=True)

# step 8: nW —— 排空权重梯度
for i in range(h+1): _weight_chunk()
assert WeightGradStore.funcs_queue.empty()
```

#### 4.3.3 源码精读

**Step 1（nF0）** —— 纯前向灌水：

[dualpipe/dualpipe.py:358-361](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L358-L361) — 反复调用 `_forward_chunk(0)` 在 forward 方向灌水。端点 rank（`half_rank=0`）灌水最多（6 次），middle rank（`half_rank=H-1`）不灌水（0 次），因为数据从中点附近就能最快到达。

**Step 2（nF0F1）** —— 在继续 forward 方向前向的同时，开始喂入反方向（phase 1）的前向，并把数据「拐弯」交给反方向邻居：

[dualpipe/dualpipe.py:363-372](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L363-L372) — middle rank 在这里被特殊对待：它的 phase 0 前向 `send=is_middle_rank`（要发出去），而普通 rank 的 phase 0 前向 `send=False`（不发），改由循环末尾显式 `_send_forward(0)` 发送。这是双向数据流在中点「转向」的衔接逻辑。

**Step 3（nB1W1F1）** —— 三重叠过渡带，**首次启用零气泡**：

[dualpipe/dualpipe.py:373-379](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L373-L379) — 每轮先 `_backward_chunk(1, enable_zb=True)`（反向时把权重梯度 `put` 进 `WeightGradStore` 延后），再 `_recv_forward(1)`，再 `_weight_chunk()`（`pop` 出一个延后的权重梯度填进气泡），最后 `_forward_chunk(1, recv=False)`。反向、权重梯度、前向三者在时间上重叠。

**Step 4（nF0B1F1B0，主步）** —— 稳态全重叠，是面积最大的区域：

[dualpipe/dualpipe.py:381-396](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L381-L396) — 主步核心是 `_forward_backward_chunk(0, 1)` 和 `_forward_backward_chunk(1, 0)` 两次调用，把 phase 0 / phase 1 的前向与反向两两重叠（即调度图里的 F&B 橙色格）。注意 `i == 0` 时 **middle rank 走一条不重叠的分支**，源码注释明确写道：

> NOTE: We don't overlap these two chunks to further reduce bubble size.

也就是说，middle rank 在主步最开始刻意**放弃**这两次重叠，换取更小的整体气泡——这是双向流水线在对折点的一个精细优化。

**Step 5（nB1F1B0）** —— 稳态到排水的过渡：

[dualpipe/dualpipe.py:398-402](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L398-L402) — 每轮先 `_backward_chunk(1)` 做一次反向，再 `_forward_backward_chunk(1, 0)` 把反方向前向与 forward 方向反向重叠。

**Step 6（nB1B0）** —— 排水主体，两个方向的反向，**在循环中点启用零气泡**：

[dualpipe/dualpipe.py:404-413](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L404-L413) — `enable_zb` 初始为 `False`，在 `i == step_6 // 2` 时翻为 `True`。翻动的精确时机取决于 `half_rank` 的奇偶性：奇数 `half_rank` 在 phase 1 反向之前开启，偶数 `half_rank` 在 phase 1 反向之后（phase 0 反向之前）开启。这种交错是为了把权重梯度计算在两个 phase 间均衡摊开。

**Step 7（nWB0）** —— 零气泡 W 与反向重叠：

[dualpipe/dualpipe.py:415-419](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L415-L419) — 每轮 `_weight_chunk()`（`pop` 一个延后权重梯度）接 `_backward_chunk(0, enable_zb=True)`，把排水期的气泡继续用权重梯度填满。

**Step 8（nW）** —— 收尾，**排空所有剩余权重梯度**：

[dualpipe/dualpipe.py:421-425](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L421-L425) — 只调用 `_weight_chunk()`，把 `WeightGradStore` 队列里残留的权重梯度全部 `pop` 执行。循环结束后的 `assert WeightGradStore.funcs_queue.empty()` 是一道安全闸：保证本步所有被延后的权重梯度都已算完，没有遗漏。

#### 4.3.4 代码实践

**实践目标**：跟踪 `step` 内一次完整调用的「操作类型」序列，理解每步实际发出哪些 F/B/W。

**操作步骤**（源码阅读型实践）：

1. 在 `dualpipe/dualpipe.py` 的 `_forward_compute_chunk`、`_backward_compute_chunk`、`_weight_chunk` 三个函数入口处（分别约 67、87、216 行），用注释提示的方式在脑中标记：前向 = F、反向 = B、`WeightGradStore.pop()` = W。
2. 取 `num_ranks=8, num_chunks=20`，针对端点 rank（`half_rank=0`）逐展开八步，只统计主类型：
   - step 1：6×F
   - step 2：1×(F0+F1)
   - step 3：3×(B1+W+F1)
   - step 4：3×(F&B 主步)
   - step 5：3×(B1+F&B)
   - step 6：1×(B1+B0)
   - step 7：3×(W+B0)
   - step 8：1×W

**需要观察的现象 / 预期结果**：把每个方向的前向次数加起来应等于 `half_num_chunks = 10`（与 4.2.5 练习 1 互证）。W 的总次数应与 B 的总次数相等（每个反向 chunk 对应一个权重梯度 chunk），这也解释了为什么 step 8 结束时 `funcs_queue` 必然为空。

**预期结果 / 待本地验证**：上述计数为依据源码公式推导的结果；若要观察真实运行时序，可在多 GPU 环境运行 `python examples/example_dualpipe.py`，并在三个原语入口加打印日志（见 4.3.5 练习 2）。

#### 4.3.5 小练习与答案

**练习 1**：step 4 的 `i == 0` 分支里，middle rank 为什么不调用 `_forward_backward_chunk` 而是拆成一串单独的 send/recv/compute？

**参考答案**：源码注释 `We don't overlap these two chunks to further reduce bubble size` 说明，middle rank 处于双向数据流的对折点，第一个 chunk 的前向输出需要立刻「拐弯」喂给反方向、反向梯度也需要立刻回传。如果套用通用的 `_forward_backward_chunk` 重叠模板，会引入额外等待；拆成显式的 send/recv 序列反而能压缩整体气泡。这是针对中点拓扑的精细优化，不影响数值正确性。

**练习 2**：如何在运行时验证 W 的总数等于 B 的总数？

**参考答案**：在 `_backward_compute_chunk` 与 `_weight_chunk` 入口分别加一行 `print`（或计数器），运行 `python examples/example_dualpipe.py` 后统计某个 rank 的两类调用次数。因为每个反向 chunk 在零气泡模式下把权重梯度 `put` 进 `WeightGradStore`，每个 `_weight_chunk` 用 `pop` 消费一个，故二者一一对应、总数相等；这也正是 step 8 末尾 `assert WeightGradStore.funcs_queue.empty()` 能成立的原因。修改源码仅为观测，观测后应还原，不要提交。

---

### 4.4 loss / outputs 的聚合与返回

#### 4.4.1 概念说明

八步跑完后，`self.loss_chunks` 里累积了首末 rank 在各自「终点 stage」上算出的每个微批次的 loss；`self.output_chunks` 里（若 `return_outputs=True`）累积了终点 stage 的输出。`step` 的收尾工作就是把它们聚合回整批，并在首末 rank 上返回，中间 rank 返回 `(None, None)`。

#### 4.4.2 核心流程

```text
eight steps done
  │
  ├─ _commit_and_wait_comm()            # 提交并等待残留通信、回收显存
  │
  ├─ loss, outputs = None, None
  ├─ if is_first_rank or is_last_rank:
  │     if criterion is not None:
  │         loss = torch.stack(self.loss_chunks)          # 堆叠所有微批次 loss
  │     if return_outputs:
  │         outputs = gather(output_chunks[is_first_rank])  # 聚合终点输出
  │         if only one: outputs = outputs[0]               # 单路解包
  │
  ├─ _reset_states()                   # 清空本次缓冲
  └─ return loss, outputs
```

这里有一个精妙的索引：`output_chunks[self.is_first_rank]`。对 first rank，`is_first_rank=True=1`，取 `output_chunks[1]`；对 last rank，`is_first_rank=False=0`，取 `output_chunks[0]`。它取的恰好是该 rank 作为「终点 stage」产出最终输出的那个 phase。

#### 4.4.3 源码精读

聚合逻辑：

[dualpipe/dualpipe.py:429-436](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L429-L436) — `loss = torch.stack(self.loss_chunks)` 把各微批次 loss 堆成一个张量；`outputs = gather(self.output_chunks[self.is_first_rank], self.batch_dim)` 把终点 stage 的各微批次输出 `gather` 回整批，单路时显式解包。

为什么 first rank 取 phase 1、last rank 取 phase 0？因为「终点 stage」判定 `is_last_stage = (is_first_rank and phase == 1) or (is_last_rank and phase == 0)`（见 `_forward_compute_chunk` [dualpipe/dualpipe.py:75](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L75)）：first rank 的反方向（phase 1）是终点、last rank 的正方向（phase 0）是终点。因此 first rank 的 loss 对应「last rank 喂入的输入」，last rank 的 loss 对应「first rank 喂入的输入」，与文档字符串 [dualpipe/dualpipe.py:315-317](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L315-L317) 一致。

最终复位并返回：

[dualpipe/dualpipe.py:438-440](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L438-L440) — 再次 `_reset_states()` 释放本次所有缓冲，返回 `(loss, outputs)`。

#### 4.4.4 代码实践

**实践目标**：用 `example_dualpipe.py` 的校验逻辑，确认聚合后的 loss / outputs 与单进程参考结果一致。

**操作步骤**：

1. 阅读 `examples/example_dualpipe.py:134-135` 的 `ref_step`：它用单进程、不分片的方式把整批切成 `num_chunks` 份，依次前向 + `loss.backward()`，得到参考 `loss_ref` 与 `output_ref`。
2. 阅读 `examples/example_dualpipe.py:159-165` 的 loss 校验：first rank 断言 `torch.equal(loss, loss_ref.chunk(2)[1])`，last rank 断言 `torch.equal(loss, loss_ref.chunk(2)[0])`，中间 rank 断言 `loss is None`。
3. 阅读 `examples/example_dualpipe.py:180-192` 的推理校验（`return_outputs=True`）：first rank 断言 outputs 等于参考输出的后半段、last rank 断言等于前半段。

**需要观察的现象 / 预期结果**：在多 GPU 环境运行 `python examples/example_dualpipe.py`，所有断言通过即说明聚合正确——first rank 拿到的是反方向（对应 last rank 输入）的 loss/outputs，last rank 拿到的是正方向（对应 first rank 输入）的 loss/outputs，二者合起来正好覆盖整批。

**待本地验证**：若无 GPU，可只做源码阅读，确认 `loss_chunks` 的追加时机（`_forward_compute_chunk` 的 [dualpipe/dualpipe.py:79-82](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L79-L82)）与 `torch.stack` 聚合的顺序一致。

#### 4.4.5 小练习与答案

**练习 1**：`torch.stack(self.loss_chunks)` 与 `torch.mean` 有什么区别？为什么不在这里取平均？

**参考答案**：`torch.stack` 把多个微批次 loss 张量沿新维度堆叠，得到形状 `(num_microbatches, ...)` 的张量，**不改变数值**；`mean` 会求平均。`step` 只负责把各微批次 loss 原样聚合返回，是否平均、如何加权是调用方的职责（用户可能想自己控制 loss 缩放），所以引擎不做隐式平均。

**练习 2**：如果 `return_outputs=False`（默认），`self.output_chunks` 里的张量会在哪里被释放？

**参考答案**：在 `_send_forward` 里，当 `not self.return_outputs` 时，已发送的输出张量被加入 `self.to_free`（[dualpipe/dualpipe.py:253-254](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L253-L254)），随后由 `_commit_and_wait_comm` → `_free_tensors` 在「晚一个提交周期」后软释放（见 u3-l4）。`return_outputs=True` 时则保留，最终由 `gather` 取出并返回。这正是 DualPipe 控制激活显存（README 表中 `PP+1`）的关键机制。

---

## 5. 综合实践

把本讲的知识串起来，完成一份「DualPipe 八步调度手算报告」。

**任务**：取 `num_ranks=8, num_chunks=20`，针对 **middle rank（rank 3 或 4）** 和 **某个普通 rank（rank 1）**，完成以下三件事：

1. **计算循环次数表**：分别列出这两个 rank 的 step_1 … step_8 循环次数（参考 4.2.4 的脚本，或手算）。
2. **标注调度图区域**：打开 `images/dualpipe.png`，用 4.3.1 的对应表，说明这两个 rank 的每一步落在调度图的哪个宏观区域（左三角 / 过渡带 / 中部矩形 / 右三角 / 收尾小三角）。
3. **一致性自检**：对这两个 rank，分别验证：
   - 前向方向微批次总数 = `step_1 + step_2 + step_4` 是否等于 `half_num_chunks = 10`；
   - 零气泡启用的步骤（step 3、6、7）与 step 8 排空是否配平——即被 `put` 进 `WeightGradStore` 的权重梯度数等于被 `pop` 出的数量。

**预期产出**（参考答案）：

- rank 1（`half_rank=1`）：step_1..step_8 = `4, 2, 2, 4, 2, 2, 2, 2`；前向总数 = 4+2+4 = 10 ✓；零气泡在 step 3（2 次 `enable_zb`）、step 6（后半段开启）、step 7（2 次 `enable_zb`）启用，step 8 用 2 次 `_weight_chunk` 排空。
- rank 3（`half_rank=3`，middle）：step_1..step_8 = `0, 4, 0, 6, 0, 4, 0, 4`；前向总数 = 0+4+6 = 10 ✓；middle rank 的 step 1/3/5/7 全为 0（不灌水、不参与过渡反向），主步 step 4 最长（6 次全重叠），并在 `i==0` 走「不重叠」特判分支。
- 区域映射：rank 1 在 step 1–2 主要落在左灌水三角，step 4 落在中部矩形，step 6–8 落在右排水三角；middle rank 几乎不经历左三角，主要集中在中部矩形和右排水段。

**待本地验证**：若有多 GPU，可运行 `python examples/example_dualpipe.py`，在 `_forward_compute_chunk` / `_backward_compute_chunk` / `_weight_chunk` 入口加临时打印，对照手算的调用次数。观测后请还原源码。

---

## 6. 本讲小结

- `step()` 是 DualPipe 的训练 / 推理总入口，骨架是「校验 → scatter 切半 → 八步调度 → 聚合 loss/outputs → 复位返回」。
- 八步循环次数由三个量驱动：`num_half_ranks`、`half_rank`（关于流水线中点对称的位置量）、`half_num_chunks`；它们满足 `step_3=step_5=step_7`、`step_2=step_6=step_8`、`step_1=2×step_3` 的对称结构。
- 主步 step 4（nF0B1F1B0）是面积最大的稳态全重叠区，靠 `_forward_backward_chunk` 实现前向与反向的 F&B 重叠；middle rank 在 `i==0` 刻意不重叠以进一步缩气泡。
- 零气泡在 step 3、6（后半段）、7 启用（把权重梯度 `put` 进 `WeightGradStore` 延后），step 8 用一连串 `_weight_chunk` 全部 `pop` 排空，并以 `assert funcs_queue.empty()` 收尾。
- 一致性恒等式 `step_1 + step_2 + step_4 = half_num_chunks` 保证每个 rank 前向微批次总数恒定，是负载均衡的数学体现。
- loss / outputs 只在 first / last rank 聚合返回，索引 `output_chunks[is_first_rank]` 精准取到「该 rank 作为终点 stage」所在 phase 的输出。

---

## 7. 下一步学习建议

本讲完成了 DualPipe 主引擎的完整剖析。接下来进入第 4 单元（专家层）：

- **u4-l1 DualPipeV 的 V 型调度**：DualPipeV 复用了本讲的八步骨架，但拓扑与喂入方式不同——只在 first rank 喂数据、在 last rank 处用 `detach().requires_grad_()` 做 V 型转折。建议对照本讲的 step 公式，重点看 DualPipeV 在哪些步骤上与 DualPipe 不同。
- **u4-l2 双向 vs V 型：取舍**：基于 README 对比表与两套调度代码，理解为什么 DualPipe 需要 PP 个设备而 DualPipeV 只需 PP/2 个，以及二者气泡公式相同但设备效率不同的原因。
- **u4-l3 自定义模块实战**：以 `example_dualpipev.py` 为模板，亲手实现一个带 `overlapped_forward_backward` 的 `PipelineStage`，用 `ref_step` 与 `cal_diff < 1e-13` 做端到端数值校验，把本讲理解的调度与 u3-l3 的自定义 autograd 串成闭环。

继续阅读建议：重读 `dualpipe/dualpipe.py:294-440` 的 `step` 全文，结合本讲八步表，逐行确认每个组合操作调用的时机与方向，做到「看懂调度图每一格对应哪一行代码」。
