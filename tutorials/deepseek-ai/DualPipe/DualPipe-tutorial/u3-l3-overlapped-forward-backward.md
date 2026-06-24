# 前反向重叠与 overlapped_forward_backward

## 1. 本讲目标

本讲是第 3 单元「DualPipe 引擎剖析」的核心一讲，紧接 [u3-l2 状态管理与计算原语](u3-l2-state-and-compute-primitives.md)。在 u3-l2 中，我们已经认识了两个「单职责」计算原语：`_forward_compute_chunk` 只做一次前向、`_backward_compute_chunk` 只做一次反向。但 DualPipe 的调度图里有一个关键的黑框格子——它把**一次前向（F）和一次反向（B）圈在一起**，这就是 README 中「计算/通信重叠」的精髓。

本讲要回答三个问题：

1. 引擎如何判定「要不要走重叠路径」，依据是什么？
2. `_forward_backward_compute_chunk` 如何把一次前向和一次反向**合并成同一个调度单元**交给用户？
3. 用户为什么必须写一个**自定义 `autograd.Function`**？它又是怎样和 `WeightGradStore` 握手，实现「B 立即算、W 可延后」的零气泡？

学完本讲，你应当能够：

- 说出 `overlapped_forward_backward` 这个布尔标志的**两个启用条件**。
- 读懂 `_forward_backward_compute_chunk` 的「pre-forward / pre-backward / 调用钩子 / post-forward / post-backward」五段式结构。
- 写出 `overlapped_forward_backward` 钩子的**八个参数约定**，并能解释 last stage 与中间 stage 的差异。
- 解释 `LinearFunc.backward` 中 `if WeightGradStore.enabled` 这一支路如何决定权重梯度是立即计算还是延迟入队。

## 2. 前置知识

本讲需要你已经掌握以下概念（来自前置讲义）：

- **微批次 / chunk、phase、方向翻转**（[u3-l1](u3-l1-dualpipe-init-rank-topology.md) / [u2-l1](u2-l1-bidirectional-pipeline-concepts.md)）：每个 rank 持有两个镜像模块 `self.module[0]` 和 `self.module[1]`，分别服务 forward / reverse 两个方向；引擎用 `phase ^= self.is_in_second_half` 让前后半 rank 的方向定义互换，使同一套循环对所有 rank 适用。
- **四类 chunk 缓冲与计算原语**（[u3-l2](u3-l2-state-and-compute-primitives.md)）：`input_chunks / output_chunks / output_grad_chunks / input_grad_chunks` 按 `[phase][chunk_id]` 二维组织；`_forward_compute_chunk` 取输入跑模块、`_backward_compute_chunk` 用 `loss.backward()`（终点）或 `run_backward`（中间）算反向。本讲要讲的 `_forward_backward_compute_chunk` 正是把这两个原语**融合**起来。
- **WeightGradStore 零气泡**（[u2-l4](u2-l4-weightgradstore-zero-bubble.md)）：零气泡把一次反向拆成「必须立即回传上游的输入梯度 **B**」与「可延后的权重梯度 **W**」；`WeightGradStore` 是个静态类，`put` 攒 W 函数、`flush` 整箱入队、`pop` 开箱执行、`clear` 每 step 复位；引擎用 `enabled` 开关与用户代码握手。

另外，你需要了解 PyTorch 的 **自定义 `autograd.Function`**：通过继承 `torch.autograd.Function` 并实现 `forward` / `backward` 两个静态方法，可以自定义某个算子的前向与反向行为；`backward` 的返回值个数与顺序必须和 `forward` 的输入一一对应，返回 `None` 表示对该输入不求梯度。

> 一个容易混淆的点：本讲讲的「F&B 重叠」（step 4 主步）和 u2-l4 讲的「零气泡 W 延后」（step 3/6/7）是**两套不同的优化**，但它们共享同一个底层机制——自定义 `autograd.Function`。本讲会在第 4.4 节把这个关系彻底讲清楚。

## 3. 本讲源码地图

本讲涉及两个文件：

| 文件 | 角色 | 本讲关注的内容 |
|------|------|----------------|
| `dualpipe/dualpipe.py` | DualPipe 引擎主体 | `__init__` 中的启用检测（第 23 行）；`_forward_backward_compute_chunk`（第 121–183 行） |
| `examples/example_dualpipe.py` | 端到端示例 | `LinearFunc` 自定义 autograd（第 13–34 行）；`PipelineStage.overlapped_forward_backward` 钩子（第 54–83 行） |

理解这两段代码后，你就掌握了 DualPipe「把前向和反向塞进同一个黑框格子」的全部秘密。

## 4. 核心概念与源码讲解

### 4.1 启用检测：什么时候走重叠路径

#### 4.1.1 概念说明

在进入 `_forward_backward_compute_chunk` 之前，先回答一个看似简单却很关键的问题：**引擎怎么知道用户「愿意且能够」把前向和反向重叠在一起？**

答案是一个布尔属性 `self.overlapped_forward_backward`。它不是用户显式传入的参数，而是在 `__init__` 里**根据传入的两个模块自动推断**出来的。引擎据此在两条路径之间二选一：

- **重叠路径**：把一次前向和一次反向打包，交给用户实现的 `overlapped_forward_backward` 钩子，让用户决定如何把它们重叠。
- **串行路径**（fallback）：先调 `_forward_compute_chunk` 做前向，再调 `_backward_compute_chunk` 做反向，二者顺序执行、互不重叠。

为什么需要推断而非显式开关？因为重叠路径要求用户**自己实现**重叠策略（钩子是空的，需要用户填），而且两个模块必须能用同一套重叠逻辑——所以引擎用类型检查来确认「用户确实写好了钩子」。

#### 4.1.2 核心流程

启用检测在 `DualPipe.__init__` 中执行一次，结果存为实例属性 `self.overlapped_forward_backward`，供后续每次 `step` 的 `_forward_backward_compute_chunk` 读取。判定逻辑为「两个条件取与」：

```
启用重叠  =  (两模块类型相同)  且  (该类型定义了 overlapped_forward_backward 方法)
```

伪代码：

```
self.overlapped_forward_backward =
    type(modules[0]) == type(modules[1])
    and hasattr(type(modules[0]), "overlapped_forward_backward")
```

注意两点：

1. 比较的是 `type(...)`（类对象），不是实例——因为每个 rank 的两个模块是各自独立构造的实例（见示例第 139 行 `nn.Sequential(PipelineStage(...), PipelineStage(...))`），只要它们是同一种 `PipelineStage` 类型即可。
2. 用 `hasattr(type(...), ...)` 在**类**上查找方法，这与钩子被调用时的形式 `type(module0).overlapped_forward_backward(...)` 完全对应（详见 4.3）。

#### 4.1.3 源码精读

[dualpipe/dualpipe.py:L22-L24](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L22-L24) 把传入的两个模块存进 `nn.ModuleList`，并完成启用检测——`self.overlapped_forward_backward` 由「类型相同」与「类型上存在钩子方法」两个条件共同决定。

这段代码在 `__init__` 中只执行一次，但它决定了整个 `step` 期间 `_forward_backward_compute_chunk` 走哪条分支。如果用户没有在 `PipelineStage` 上定义 `overlapped_forward_backward`，引擎会优雅降级到串行前向+反向，数值结果仍正确，只是失去了 F&B 重叠带来的气泡压缩收益。

#### 4.1.4 代码实践

阅读型实践：

1. 打开 `examples/example_dualpipe.py`，确认 `PipelineStage`（第 42 行起）上确实定义了 `@classmethod def overlapped_forward_backward(...)`（第 54 行）。
2. 思考：如果把这个 classmethod 删掉或改名，`DualPipe(local_modules)` 构造时 `self.overlapped_forward_backward` 会变成什么？`_forward_backward_compute_chunk` 会走哪条分支？

#### 4.1.5 小练习与答案

**练习 1**：`self.overlapped_forward_backward` 的判定为什么用 `type(modules[0]) == type(modules[1])`，而不是 `modules[0] == modules[1]` 或 `modules[0] is modules[1]`？

**答案**：每个 rank 的两个模块是独立构造的实例（互不相同），实例相等或同一性比较都会得到 `False`；而我们只关心「它们是不是同一种类型、能否共用同一套重叠钩子」，所以比较的是类对象 `type(...)`。

**练习 2**：判定里为什么是 `hasattr(type(modules[0]), ...)` 而不是 `hasattr(modules[0], ...)`？

**答案**：两者在大多数情况下结果一致，但用 `type(...)` 更贴合钩子的实际调用形式 `type(module0).overlapped_forward_backward(...)`——引擎始终把钩子当作类方法来调用（传入 `module0` 作为第一个实参），在类层面查找更准确地表达了这一约定。

---

### 4.2 `_forward_backward_compute_chunk`：把一次前向与一次反向合并

#### 4.2.1 概念说明

这是本讲最核心的引擎函数。它的任务是：**在同一个调度单元里，既做一次前向（phase0 方向的某个 chunk），又做一次反向（phase1 方向的某个 chunk）**，从而把 README 调度图里的「F&B 黑框格子」落地为代码。

理解它的关键是「**把数据准备和数据消费解耦**」：

- 引擎负责在调用用户钩子**之前**，把这次前向和这次反向**所有需要的数据**都从四类缓冲里取出来、打包好；
- 用户钩子负责**真正执行**前向 + 反向，并可以自由安排它们的重叠顺序；
- 引擎在钩子**返回之后**，负责把产物（前向输出、loss、输入梯度）收回缓冲。

这种「pre-prepare → 用户钩子 → post-collect」的三明治结构，正是让引擎和用户代码各司其职的设计。

#### 4.2.2 核心流程

整个函数有**三个早返回分支**，然后是「pre-forward / pre-backward / 调用钩子 / post-forward / post-backward」五段主体：

```
_forward_backward_compute_chunk(phase0, phase1):
    ① 若 forward_only（推理/无梯度）：只做 phase0 前向，返回      # 无反向可言
    ② 若未启用重叠：先做 phase0 前向，再做 phase1 反向，返回       # 串行 fallback
    ── 以下为重叠主体 ──
    pre-forward：    从缓冲取 phase0 的输入、criterion、labels
    pre-backward：   从缓冲取 phase1 的 outputs / output_grads（或 loss）
    调用钩子：       type(module0).overlapped_forward_backward(
                        module0, inputs0, criterion0, labels0,
                        module1, loss1, outputs1, output_grads1)
                    → 返回 (outputs0, loss0)
    post-forward：   把 outputs0 / loss0 收回缓冲
    post-backward：  从 inputs[phase1] 读 .grad，收回 input_grads 缓冲
```

注意 `phase0` 与 `phase1` 是**两个不同的方向**：典型调用是 `_forward_backward_chunk(0, 1)` 或 `_forward_backward_chunk(1, 0)`（见 step 4，第 384–396 行），即一次 forward 方向、一次 reverse 方向。两个方向的 chunk 各自从各自的计数器和缓冲取数据，互不干扰。

#### 4.2.3 源码精读

**早返回分支**——[dualpipe/dualpipe.py:L121-L129](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L121-L129) 先处理推理模式（`forward_only` 时没有反向）和未启用重叠的串行 fallback。这两条分支保证了：即使不写自定义钩子，DualPipe 也能正确（只是不重叠地）运行。

**pre-forward**——[dualpipe/dualpipe.py:L131-L144](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L131-L144) 先做方向翻转 `phase0 ^= self.is_in_second_half`，取下一个前向 chunk 的编号并自增游标，取出 `module0 / inputs0`，并判定 `is_last_stage0`。若该 chunk 是终点 stage 且有 criterion，则连 labels 一起准备好；否则 labels 置空、criterion 置 `None`。这段把「前向所需的一切」备齐。

**pre-backward**——[dualpipe/dualpipe.py:L146-L165](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L146-L165) 同样翻转方向、取下一个反向 chunk 编号并自增游标。这里有一个**关键分叉**：

- 若 `is_last_stage1`（终点 stage）：取出 `loss1`（标量 loss 作反向种子），`outputs1/output_grads1` 置空。
- 否则（中间 stage）：从 `output_chunks` 取出前向产物、从 `output_grad_chunks` 取出下游回传梯度，**取出后立即把这两个槽位置 `None`**（释放显存），再用列表推导过滤掉 `grad` 为 `None` 的路（`non_empty`），只对真正有梯度的张量做反向。这与 u3-l2 中 `_backward_compute_chunk` 的逻辑完全一致——重叠版本只是把「取数据」提前到了钩子调用之前。

**调用钩子**——[dualpipe/dualpipe.py:L167-L171](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L167-L171) 把上面备好的八个参数一次性传给用户钩子，拿回 `(outputs0, loss0)`。注意它以 `type(module0).overlapped_forward_backward(...)` 的形式调用，即作为类方法（`module0` 作为第一个实参传入）。

**post-forward**——[dualpipe/dualpipe.py:L173-L177](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L173-L177) 把前向产物收回：非终点 stage（或 `return_outputs=True`）时把 `outputs0` 追加进 `output_chunks[phase0]`；终点 stage 且有 criterion 时把 `loss0` 追加进 `loss_chunks`。

**post-backward**——[dualpipe/dualpipe.py:L179-L183](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L179-L183) 在钩子跑完反向后，从 `input_chunks[phase1]` 读出输入张量、立即置 `None` 释放，再通过 `[t.grad for t in inputs]` 提取每个输入的 `.grad`（autograd 在反向中已填好），追加进 `input_grad_chunks[phase1]` 等待发给上游。这与 `_backward_compute_chunk` 的收尾（第 116–119 行）逐字对应。

#### 4.2.4 代码实践

阅读型实践（跟踪调用链）：

1. 在 `dualpipe.py` 的 `step` 中找到 step 4（[dualpipe/dualpipe.py:L381-L396](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L381-L396)）。
2. 注意第 393、395–396 行对 `_forward_backward_chunk` 的调用：`_forward_backward_chunk(0, 1, recv0=False)` 与 `_forward_backward_chunk(1, 0)`，二者 phase0/phase1 互换。
3. 跟踪 `_forward_backward_chunk`（第 205–214 行）如何把 `_forward_backward_compute_chunk` 包裹在 `_recv_forward / _recv_backward / _commit_and_wait_comm / _send_forward / _send_backward` 之间，形成「收 → 等通信 → 融合计算 → 发」的完整单元。
4. **观察**：这正是 README 调度图中「同一黑框圈住前向格子与反向格子」在代码层面的对应。

#### 4.2.5 小练习与答案

**练习 1**：为什么 pre-backward 段在取出 `outputs1` 和 `output_grads1` 后要立即把缓冲槽位置 `None`？

**答案**：为了**尽早释放激活显存**。反向一旦完成，这些前向产物和下游梯度就不再需要，置 `None` 让它们可被垃圾回收。这是 DualPipe 把激活显存压到 PP+1 的关键操作之一（参见 u3-l2 的「取出 → 用完 → 置 None」生命周期）。

**练习 2**：post-backward 段用 `[t.grad for t in inputs]` 提取输入梯度。这里依赖 `inputs` 的 `.grad` 已被填好——这个 `.grad` 是在什么时候、由谁填上的？

**答案**：由用户钩子内部的反向（`loss1.backward()` 或 `run_backward(outputs1, output_grads1)`）填上。这些 `inputs` 是从上游收到的激活张量（`requires_grad=True`，见 u2-l2 的 `build_from_tensor_shapes`），autograd 反向传播时会沿计算图把梯度累积到它们的 `.grad` 上。

**练习 3**：为什么这个函数需要 `phase0` 和 `phase1` 两个参数，而不能像 `_forward_compute_chunk` 那样只有一个 `phase`？

**答案**：因为它要**同时**处理两个方向的 chunk：`phase0` 指明这次前向走哪个方向、`phase1` 指明这次反向走哪个方向，二者通常不同（如 `(0, 1)`）。单参数原语只能描述一个方向的一次计算。

---

### 4.3 `overlapped_forward_backward` 钩子：参数契约与实现

#### 4.3.1 概念说明

`overlapped_forward_backward` 是**用户必须自己实现**的类方法。引擎在第 4.2 节把「一次前向 + 一次反向」的所有数据打包好后，就调用它，把「如何重叠」的决定权完全交给用户。

它本质上是一个**协议/契约**：引擎承诺按固定顺序传八个参数、并按固定语义接收两个返回值；用户承诺在函数体内同时完成前向（产生 outputs0）和反向（产生 input_grads，通过 `.grad` 体现），并可以自由安排二者的执行顺序以实现重叠。

> 重要：示例中的实现（第 70–83 行）只是「最朴素的示范」——它先做前向、再做反向，二者在示例里其实是**顺序执行**的。真正的「重叠」收益来自把前向/反向放在不同 CUDA stream、或借助自定义 autograd 把重活儿（W）抠出去延后。钩子的存在，是让用户**有地方**去写这些进阶策略。

#### 4.3.2 核心流程

钩子接收八个位置参数，可分为「前向四件套」和「反向四件套」：

```
overlapped_forward_backward(
    # ── 前向任务（对应 phase0 的一个 chunk）──
    module0,        # 该方向的模块实例
    inputs0,        # 前向输入（一个 list，元素是张量）
    criterion0,     # 损失函数；若非终点 stage 则为 None
    labels0,        # 标签；若非终点 stage 则为 []
    # ── 反向任务（对应 phase1 的一个 chunk）──
    module1,        # 该方向的模块实例
    loss1,          # 终点 stage 时的标量 loss；中间 stage 时为 None
    outputs1,       # 中间 stage 时的前向产物；终点 stage 时为 []
    output_grads1,  # 中间 stage 时下游回传的梯度；终点 stage 时为 []
)
→ 返回 (outputs0, loss0)
```

两条互斥的「身份」分支决定了反向怎么发起：

| 该 chunk 是否终点 stage | `loss1` | `outputs1` / `output_grads1` | 反向发起方式 |
|---|---|---|---|
| 是（last stage） | 非空标量 loss | 均为空 list `[]` | `loss1.backward()` |
| 否（中间 stage） | `None` | 非空张量列表 | `run_backward(outputs1, output_grads1)` |

钩子返回 `(outputs0, loss0)`：`outputs0` 是前向产物（list）；`loss0` 仅在终点 stage 且有 criterion 时非 `None`。

#### 4.3.3 源码精读

钩子签名——[examples/example_dualpipe.py:L54-L65](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipe.py#L54-L65) 用 `@classmethod` 定义，第一个形参 `cls` 之后正是上面契约的八个参数，返回类型标注为 `Tuple[torch.Tensor, Optional[torch.Tensor]]`。docstring 直言「The code below is just an example」。

前向部分——[examples/example_dualpipe.py:L70-L75](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipe.py#L70-L75) 跑 `module0(*inputs0)` 得到 `outputs0`，把单个张量包成 list；若有 criterion 则算出 `loss0`，否则置 `None`。这与引擎非重叠路径里的 `_forward_compute_chunk` 完全等价。

反向部分——[examples/example_dualpipe.py:L77-L81](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipe.py#L77-L81) 用 `if loss1 is not None` 区分两条分支：终点 stage 调 `loss1.backward()` 并 `detach_()`（断开图，防止后续误触发二次反向）；中间 stage 调 `run_backward(outputs1, output_grads1)`。这与 `_backward_compute_chunk`（第 98–111 行）的反向发起逻辑一一对应。

返回——[examples/example_dualpipe.py:L83](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipe.py#L83) 把 `(outputs0, loss0)` 交回引擎，由引擎在 post-forward 段收回缓冲。

#### 4.3.4 代码实践

阅读型实践（对照引擎与钩子的契约）：

1. 把 4.3.3 的钩子实现与 4.2.3 的引擎调用（第 168–171 行）逐参数对齐，确认「前向四件套 / 反向四件套」完全咬合。
2. 思考：示例钩子里前向（第 70 行）和反向（第 78/81 行）是顺序写的。如果要真正让它们在 GPU 上重叠，你会考虑用什么手段？（提示：CUDA stream、或借助 4.4 节把 W 抠出去延后）。

#### 4.3.5 小练习与答案

**练习 1**：为什么钩子要返回 `loss0`，而不能像 `outputs0` 那样由引擎直接读取？

**答案**：因为前向是否计算 loss、用什么 criterion 计算，都由用户在钩子内决定；引擎无法预知。所以约定由钩子算好 `loss0` 并返回，引擎再在 post-forward 段把它收进 `loss_chunks`。

**练习 2**：终点 stage 分支里为什么反向之后要 `loss1.detach_()`？

**答案**：`detach_()` 原地把 loss 从计算图上断开，避免这个 loss 张量之后被误用作反向种子而触发意外的二次反向传播；这是一个防御性的显存与正确性保护。

**练习 3**：钩子是 `@classmethod`，但引擎用 `type(module0).overlapped_forward_backward(module0, ...)` 调用——这里 `module0` 是怎么对应到类方法的形参的？

**答案**：`@classmethod` 的第一个形参约定是类本身（示例里写作 `cls`）。但引擎调用时 `type(module0).overlapped_forward_backward(module0, ...)` 把 `module0` 作为第一个**位置实参**传进去，因此实际占据的是 `cls` 之后第一个用户形参。等价地，可以把它理解为一个接收「实例 + 八个任务参数」的普通函数。

---

### 4.4 `LinearFunc` 自定义 autograd：B 立即算、W 可延后

#### 4.4.1 概念说明

这一节回答本讲最深的问题：**用户为什么必须写一个自定义 `autograd.Function`？它和 `WeightGradStore` 是怎么握手的？**

回顾 u2-l4：一次线性层的反向产生两类梯度——

- **输入梯度 B（`grad_input`）**：必须立即算，因为它要回传给上游 stage（通过 P2P 发送）。
- **权重梯度 W（`grad_weight`）**：可以延后，因为它只在本 stage 累积到参数 `.grad`，没有跨 stage 依赖。

PyTorch 标准的 `nn.Linear.backward` 会把 B 和 W **一起、立即**算掉，无法分开。而 DualPipe 想在需要时把 W 抠出去塞进流水线气泡。所以用户必须用自定义 `autograd.Function` 接管反向，**手动**控制 W 的去向：

- 当 `WeightGradStore.enabled` 为真（零气泡开启的 step 3/6/7）：把 W 的计算包成一个函数 `put` 进队列，延后执行；`backward` 只返回 B，迅速返回。
- 当 `WeightGradStore.enabled` 为假（如重叠主步 step 4）：W 立即计算。

这个 `if WeightGradStore.enabled` 分支，就是用户代码与引擎之间「握手」的接缝：引擎只负责拨动 `enabled` 开关（在 `_backward_compute_chunk` 第 97 行），用户代码负责据此决定 W 的去向。而本讲强调的 F&B 重叠与 u2-l4 的零气泡 W 延后，**共享的正是这同一个自定义 autograd + 同一个开关**。

#### 4.4.2 核心流程

`LinearFunc` 的 `forward` / `backward` 协作如下：

```
LinearFunc.forward(ctx, input, weight):
    保存 (input, weight) 供反向
    return F.linear(input, weight)

LinearFunc.backward(ctx, grad_output):
    input, weight = 取出保存的张量
    若 weight.grad 为 None：初始化为全 0
    定义 grad_weight_fn():  weight.grad += grad_outputᵀ @ input   # 这就是 W

    if WeightGradStore.enabled:        # ← 握手开关
        WeightGradStore.put(grad_weight_fn)   # W 延后入队
    else:
        grad_weight_fn()                       # W 立即算

    grad_input = grad_output @ weight           # B 立即算（要回传上游）
    return (grad_input, None)                   # 对 weight 返回 None：W 已手动处理
```

两个关键细节：

1. `backward` 返回 `(grad_input, None)`——对 `weight` 返回 `None`，意味着「不要让 autograd 再去自动累积 weight 的梯度」，因为 W 已经被手动（立即或延后）算进 `weight.grad` 了。
2. `weight.grad` 用 `+=` 累加而非 `=` 赋值：因为梯度累加满足交换律，延后重放的顺序不影响最终结果（这是零气泡能正确工作的数学前提，见 u2-l4）。

权重梯度 W 的数学形式（对无 bias 的线性层 `y = xWᵀ`）：

\[
\frac{\partial L}{\partial W} = (\text{flatten}(x))^\top \, (\text{flatten}(\text{grad\_output}))
\]

代码里用 `flatten(0, -2)` 把除最后一维外的所有维度合并成行，等价于把 batch 与序列维拍平后做矩阵乘。输入梯度 B 的形式：

\[
\frac{\partial L}{\partial x} = \text{grad\_output} \, W
\]

#### 4.4.3 源码精读

`LinearFunc.forward`——[examples/example_dualpipe.py:L14-L18](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipe.py#L14-L18) 用 `ctx.save_for_backward(input, weight)` 保存反向所需张量，返回 `F.linear(input, weight)`。注意它只接收 `input` 和 `weight`（不含 bias），对应 `MyLinear`（第 37–39 行）用 `bias=False` 构造。

`LinearFunc.backward` 的握手核心——[examples/example_dualpipe.py:L20-L34](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipe.py#L20-L34) 是整段最关键的代码。先取出保存张量、必要时初始化 `weight.grad`（第 22–24 行）；定义 W 的计算函数（第 26–27 行）；随后第 29–32 行的 `if WeightGradStore.enabled` 正是握手接缝——延后入队或立即执行二选一；最后第 33 行立即算 B（`grad_input = grad_output @ weight`），第 34 行返回 `(grad_input, None)`。

`MyLinear` 桥接——[examples/example_dualpipe.py:L37-L39](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipe.py#L37-L39) 继承 `nn.Linear` 但重写 `forward` 调用 `LinearFunc.apply(input, self.weight)`，从而让标准线性层走自定义反向。`PipelineStage`（第 42–52 行）的两层都用 `MyLinear`，于是整段的反向都受 `WeightGradStore.enabled` 控制。

把这套机制放进 step 全局看：

- step 3/6/7 调 `_backward_chunk(..., enable_zb=True)` → `_backward_compute_chunk` 第 97 行把 `WeightGradStore.enabled` 置真 → 反向中 `LinearFunc.backward` 走 `put` 分支，W 延后；
- step 7/8 调 `_weight_chunk` → `WeightGradStore.pop()`（第 223 行）开箱执行累积的 W，填进气泡；
- step 4 重叠主步走 `_forward_backward_compute_chunk` → 用户钩子里的反向此时 `enabled` 为假（未被置真），W 立即算。

一句话：**自定义 autograd 让 W 可分离，`WeightGradStore.enabled` 让 W 可调度，二者合力既支撑 F&B 重叠，又支撑零气泡。**

#### 4.4.4 代码实践

阅读 + 推理型实践：

1. 在 `LinearFunc.backward` 中（第 29–32 行），把 `if WeightGradStore.enabled` 改成「永远立即计算」（即删掉 `put` 分支）。思考：step 4 的重叠主步结果会受影响吗？step 7/8 的零气泡会怎样？

2. **观察**（待本地验证）：step 4 不受影响（本就期望立即算）；但 step 7/8 里 `WeightGradStore.funcs_queue` 会一直是空的——因为没有任何 W 被入队，`_weight_chunk` 的 `pop()` 会触发 `assert not cls.funcs_queue.empty()`（[dualpipe/utils.py:L25](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/utils.py#L25)）而报错。这说明握手接缝一旦断裂，零气泡调度会直接崩溃。

#### 4.4.5 小练习与答案

**练习 1**：`LinearFunc.backward` 为什么对 `weight` 返回 `None`？

**答案**：因为 W（权重梯度）已经被手动算进 `weight.grad` 了（立即或延后）。若对 weight 返回非 `None` 的梯度，autograd 会再自动累积一次，导致 W 被算两遍。返回 `None` 即告诉 autograd「这个输入的梯度我自己处理过了」。

**练习 2**：为什么 `grad_weight_fn` 里用 `weight.grad += ...` 而不是 `weight.grad = ...`？

**答案**：因为零气泡会把多个 W 函数延后、乱序重放（FIFO），但梯度累加满足交换律与结合律，`+=` 保证了无论重放顺序如何，最终 `weight.grad` 都正确。用 `=` 赋值则会让后执行的覆盖先执行的，结果错误。

**练习 3**：`grad_input = grad_output @ weight` 这一步（第 33 行）为什么不能也延后入队？

**答案**：因为 B（输入梯度）是反向链路上必须**立即回传给上游 stage** 的——它要经 P2P 发给前一个 rank 继续反向传播。一旦延后，上游 stage 拿不到梯度，整条反向链就断了。所以只有没有跨 stage 依赖的 W 才能延后，B 必须立即算。

## 5. 综合实践

**目标**：亲手实现一个「两层线性层」的自定义 autograd Function，并验证它与 `WeightGradStore` 的握手——在 `enabled=False` 时 W 立即计算，在 `enabled=True` 时 W 延后入队、需 `flush` + `pop` 后才生效。这个实践不依赖多 GPU / NCCL，可在单机 CPU 上运行，直接复现 4.4 节的核心机制。

**操作步骤**：把下面的「示例代码」保存为 `lab_linearfunc.py`，放在仓库根目录下运行 `python lab_linearfunc.py`（需先 `pip install -e .` 安装 `dualpipe`，或确保 `dualpipe` 在 `PYTHONPATH` 中）。

```python
# 示例代码：自定义 autograd Function 与 WeightGradStore 的握手
import torch
import torch.nn as nn
import torch.nn.functional as F
from dualpipe.utils import WeightGradStore   # WeightGradStore 是独立可用的纯 Python 类


class LinearFunc(torch.autograd.Function):
    @staticmethod
    def forward(ctx, input, weight):
        ctx.save_for_backward(input, weight)
        return F.linear(input, weight)

    @staticmethod
    def backward(ctx, grad_output):
        input, weight = ctx.saved_tensors
        if weight.grad is None:
            weight.grad = torch.zeros_like(weight)

        def grad_weight_fn():                       # 这就是 W
            weight.grad += grad_output.flatten(0, -2).T @ input.flatten(0, -2)

        if WeightGradStore.enabled:                 # ← 握手开关
            WeightGradStore.put(grad_weight_fn)     # 延后入队
        else:
            grad_weight_fn()                        # 立即计算

        grad_input = grad_output @ weight           # B 立即算
        return grad_input, None


class MyLinear(nn.Linear):
    def forward(self, input):
        return LinearFunc.apply(input, self.weight)


torch.manual_seed(0)
lin = MyLinear(4, 8, bias=False)
x = torch.randn(3, 4, requires_grad=True)

# ---- 路径一：enabled=False，W 立即计算（对应 step 4 重叠主步的默认情形）----
WeightGradStore.clear()
WeightGradStore.enabled = False
y = lin(x)
y.sum().backward()
print("路径一 backward 后 weight.grad 是否非零：",
      bool((lin.weight.grad is not None) and lin.weight.grad.abs().sum() > 0))
print("路径一 backward 后 cache 是否为空：", WeightGradStore.cache == [])   # 期望 True

# ---- 路径二：enabled=True，W 延后入队（对应 step 3/6/7 零气泡）----
lin.weight.grad = None
WeightGradStore.clear()
WeightGradStore.enabled = True
y = lin(x)
y.sum().backward()
# backward 里只把 weight.grad 初始化为 0，真正的 W 还没执行
print("路径二 backward 后、flush 前 weight.grad 是否仍为全零：",
      bool((lin.weight.grad is not None) and lin.weight.grad.abs().sum() == 0))
WeightGradStore.flush()        # cache 整箱搬进 funcs_queue
WeightGradStore.pop()          # 开箱逐个执行 W
print("路径二 pop 后 weight.grad 是否非零：",
      bool(lin.weight.grad is not None and lin.weight.grad.abs().sum() > 0))
```

**需要观察的现象与预期结果（待本地验证）**：

- 路径一：backward 后 `weight.grad` 已有非零值，`cache` 为空。
- 路径二：backward 后 `weight.grad` 仍为全零（只有初始化的 0），`cache` 里有一个函数；`flush` + `pop` 之后 `weight.grad` 才出现非零值。
- 进一步可把「路径一的 W」与「路径二 pop 后的 W」做差，验证二者数值一致（因 `+=` 累加可交换，延后不影响结果）。

**扩展**：把上面两层 `MyLinear` 包进一个 `nn.Sequential`，套上一个 `gelu`，模拟示例里的 `PipelineStage`；再在「路径二」里连续前向+反向两个 chunk，`flush` 一次、`pop` 两次，观察 `funcs_queue` 的 FIFO 行为（与 u2-l4 的「先攒后放」对应）。

## 6. 本讲小结

- **启用检测**：`self.overlapped_forward_backward` 由「两模块类型相同」且「类型上存在 `overlapped_forward_backward` 方法」共同决定（[dualpipe.py:L23](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L23)）；不满足则降级为串行前向+反向，仍正确但失去重叠收益。
- **合并调度**：`_forward_backward_compute_chunk` 用「pre-forward / pre-backward / 调用钩子 / post-forward / post-backward」五段式，把一次前向（phase0）和一次反向（phase1）合并为同一个调度单元（[dualpipe.py:L121-L183](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L121-L183)）。
- **钩子契约**：用户实现的 `overlapped_forward_backward` 接收「前向四件套 + 反向四件套」共八个参数，返回 `(outputs0, loss0)`；反向在终点 stage 用 `loss.backward()`、中间 stage 用 `run_backward`（[example_dualpipe.py:L54-L83](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipe.py#L54-L83)）。
- **握手机制**：`LinearFunc` 自定义 autograd 在 `backward` 中用 `if WeightGradStore.enabled` 决定 W 立即算还是延后入队，B 始终立即算（[example_dualpipe.py:L20-L34](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipe.py#L20-L34)）；对 weight 返回 `None` 以避免重复累积。
- **两套优化同一机制**：F&B 重叠（step 4）与零气泡 W 延后（step 3/6/7）是两套不同优化，但都建立在「自定义 autograd + `WeightGradStore.enabled` 开关」这一共同底座上。

## 7. 下一步学习建议

本讲把「单 chunk 的前向+反向如何合并与重叠」讲透了，但还缺两块拼图才能看懂完整的 `step`：

1. **通信原语与组合操作**（[u3-l4](u3-l4-comm-primitives-and-composite-ops.md)）：`_forward_backward_compute_chunk` 是怎么被 `_recv_forward / _recv_backward / _commit_and_wait_comm / _send_forward / _send_backward` 包裹成 `_forward_backward_chunk` 的？通信与计算如何重叠？建议接着学 u3-l4，把「收 → 等通信 → 融合计算 → 发」的完整单元补齐。
2. **八步调度**（[u3-l5](u3-l5-dualpipe-eight-step-schedule.md)）：本讲多次提到 step 3/4/6/7/8，但还没讲清楚每一步循环几次、为什么 step 4 是主步、middle rank 有何特殊。学完 u3-l5，你就能把本讲的 F&B 重叠和 u2-l4 的零气泡放进完整的 8 步调度图里，彻底闭环。

此外，建议在阅读 u3-l4 / u3-l5 时，带着本讲的一个问题回去对照：**当引擎在 step 4 调 `_forward_backward_chunk(0, 1)` 时，`WeightGradStore.enabled` 当前是什么值？为什么？** 这能帮你把「调度—计算原语—握手开关」三层彻底打通。
