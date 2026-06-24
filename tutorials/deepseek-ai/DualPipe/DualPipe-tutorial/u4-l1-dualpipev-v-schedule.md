# DualPipeV 的 V 型调度

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 DualPipeV 与 DualPipe 在**拓扑**和**数据喂入方式**上的根本差异。
- 解释 last rank 处 `detach().requires_grad_()` 如何把前向（phase 0）输出「折」成反向（phase 1）输入，从而形成 V 型转折。
- 解释反向阶段如何用「梯度桥」把 phase 1 的输入梯度回灌成 phase 0 的输出梯度，保证数学上等价于一张连通的自动微分图。
- 读懂 `DualPipeV.step` 的八步调度，并能对照 DualPipe 写出每步循环次数公式的差异与替换规则。

## 2. 前置知识

本讲是专家层第 1 讲，默认你已经学完：

- **u3-l5（DualPipe 八步调度）**：DualPipe 的入口校验、scatter 切半、八步调度、loss/outputs 聚合的完整骨架。
- **u3-l1（rank 拓扑）**：`rank_mapping` / `rank_inverse_mapping` 互逆映射、`is_in_second_half`、`is_middle_rank` 的含义。
- **u3-l2 / u3-l3 / u3-l4**：状态缓冲 `[phase][chunk_id]`、`_forward_compute_chunk` / `_backward_compute_chunk`、`phase ^= is_in_second_half` 方向翻转、`detach().requires_grad_()`、`run_backward`。

两个关键术语回顾：

- **phase（阶段）**：引擎里「方向」的编号。`phase=0` 与 `phase=1` 分别对应一个镜像模块 `self.module[phase]`，并对应两套独立的缓冲（`input_chunks[phase]`、`output_chunks[phase]` 等）。
- **detach（截断）**：`t.detach()` 造出一个与 `t` 共享数据但**不在自动微分图上**的新叶子张量；`.requires_grad_()` 再让它能反向求导。本讲的核心就是用这一对操作在 V 型顶点处「手动接线」。

一个最关键的对比直觉（先记住，后文展开）：

| | DualPipe（双向） | DualPipeV（V 型） |
|---|---|---|
| 数据喂入点 | 首末两端各喂一半 | **只在 first rank 喂数据** |
| 每设备持有阶段 | 2 个（互为镜像） | 2 个（前半 + 后半） |
| 折叠点（fold point） | middle rank（流水线对折点） | **last rank（V 型顶点）** |
| phase 是否与 rank 相关 | 是（用 `phase ^= is_in_second_half` 翻转） | **否（phase 绝对，全局统一）** |
| 建模深度 PP 所需设备 | PP | **PP/2** |

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [dualpipe/dualpipev.py](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipev.py) | 本讲主角，`DualPipeV` 类的全部实现 |
| [dualpipe/dualpipe.py](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py) | 对照基准，`DualPipe` 类，用于逐行对比差异 |
| [examples/example_dualpipev.py](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipev.py) | DualPipeV 的可运行示例，展示模型切分与校验 |

阅读建议：本讲全程把 `dualpipev.py` 和 `dualpipe.py` **并排对照**看，差异往往只有几行，但每行都对应一个 V 型调度的设计取舍。

---

## 4. 核心概念与源码讲解

### 4.1 DualPipeV 初始化：单向拓扑与简化标志

#### 4.1.1 概念说明

`DualPipeV.__init__` 做的事和 `DualPipe.__init__` 几乎一样：把两个模块收进 `nn.ModuleList`、解析 `rank_mapping` / `rank_inverse_mapping`、算出邻居 rank 和首末标志。**关键差异在于它「少算」了两个标志位**：`is_in_second_half` 和 `is_middle_rank`。

为什么能省？回到两张图：

- DualPipe 是**双向**流水线——数据从首末两端相向喂入，两股数据流在中点（middle rank）相遇。为了让前后半段 rank 共用同一套调度公式，DualPipe 用 `phase ^= is_in_second_half` 把 phase 的定义**按 rank 所处半段翻转**（前半段：phase 0=前向；后半段：phase 0=反向）。
- DualPipeV 是**单向灌水**——数据只从 first rank 的 phase 0 进入，一路前向跑到 last rank，在 V 型顶点「折」回来，再以 phase 1 反向跑回 first rank 算 loss。**phase 的含义对所有 rank 都一致**（phase 0 永远是前向、phase 1 永远是反向），所以不需要半段翻转，自然也不需要 `is_in_second_half`。折叠点从「中间 rank」变成了「末尾 rank」，于是用现成的 `is_last_rank` 就够了，不再需要 `is_middle_rank`。

这一省，让后续所有通信原语和计算原语开头的 `phase ^= self.is_in_second_half` 全部消失，代码更直白。

#### 4.1.2 核心流程

`DualPipeV.__init__` 的拓扑计算流程：

```text
1. 把两个模块存进 self.module（nn.ModuleList）
2. 推断 overlapped_forward_backward 是否可用
3. 取进程组、num_ranks
4. 由 rank_mapping 求 rank_inverse_mapping（含 +1 的 None 哨兵）
5. self.rank = 逻辑 pp rank
6. prev_rank / next_rank = 组内 rank（供 P2P 通信）
7. is_first_rank = (rank == 0)
   is_last_rank = (rank == num_ranks - 1)
   ——到此结束，没有 is_in_second_half / is_middle_rank
```

模型切分（见示例）决定了每个 rank 持有的两个阶段：

```text
完整模型有 num_ranks*2 个 stage（编号 0 .. 2N-1）
rank r 持有：
  module[0] = stage r              → 服务 phase 0（前向）
  module[1] = stage (2N-1-r)       → 服务 phase 1（反向）
```

于是前向跑 stage 0,1,…,N-1（rank 0→N-1），反向跑 stage N,N+1,…,2N-1（rank N-1→0），同一条设备链被复用两次，呈 V/U 字形。

#### 4.1.3 源码精读

`DualPipeV.__init__` 的签名与 DualPipe 完全一致，接收 `(modules, batch_dim, process_group, rank_mapping)`：

[dualpipe/dualpipev.py:12-26](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipev.py#L12-L26)：存模块、推断 `overlapped_forward_backward`、取进程组与 `num_ranks`。

[dualpipe/dualpipev.py:30-38](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipev.py#L30-L38)：用一行原地翻转 `rank_inverse_mapping[rank_mapping[i]] = i` 求出逆映射，再据此算 `self.rank`（逻辑 pp rank）与 `prev_rank` / `next_rank`（组内 rank，直接喂给底层 P2P）。注意 DualPipeV **没有**计算 `first_rank` / `last_rank` 这两个组内 rank 字段——因为它不需要在通信里用到首末的组内编号（V 型顶点处的衔接是本地操作，见 4.2）。

[dualpipe/dualpipev.py:40-41](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipev.py#L40-L41)：只设两个布尔标志：

```python
self.is_first_rank = self.rank == 0
self.is_last_rank = self.rank == self.num_ranks - 1
```

对照 DualPipe 在同一位置多算的两行：

[dualpipe/dualpipe.py:44-45](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L44-L45)：DualPipe 额外设了 `is_in_second_half`（驱动 `phase ^= ...` 翻转）与 `is_middle_rank`（折叠点判定）。DualPipeV 把这两者都删掉。

示例里的模型切分（每个 rank 持 stage r 与 stage 2N-1-r）：

[examples/example_dualpipev.py:127](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipev.py#L127)：完整模型有 `pp_size * 2` 个 stage。

[examples/example_dualpipev.py:137-140](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipev.py#L137-L140)：`local_full_modules = (full_modules[rank], full_modules[pp_size*2 - 1 - rank])`，正是「stage r + stage 2N-1-r」的镜像对，再 `load_state_dict` 进两个独立的 `PipelineStage`。

#### 4.1.4 代码实践

**实践目标**：直观感受「单向灌水」与「双向灌水」在数据喂入上的差异。

**操作步骤**：

1. 打开 `examples/example_dualpipev.py`，找到 [main 函数 144-149 行](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipev.py#L144-L149)，注意只有 `is_first_rank` 持有真实的 `x`/`l`，其余 rank 都置 `None`。
2. 对照 `examples/example_dualpipe.py` 的 [145-153 行](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipe.py#L145-L153)，DualPipe 里 `is_first_rank` 与 `is_last_rank` **都**持有数据（且各拿 `full_x.chunk(2)` 的一半）。
3. 在纸上画出 `pp_size=4` 时两种调度各自的数据入口：DualPipeV 只有 rank 0 一个入口；DualPipe 有 rank 0、rank 3 两个入口。

**需要观察的现象**：DualPipeV 中 loss 只在 rank 0 聚合返回（见 4.3），其余 rank 的 loss 为 `None`；DualPipe 中 rank 0 与 rank 3 都会返回各自那半的 loss。

**预期结果**：你能用一句话概括——「DualPipeV 砍掉了一半的数据入口，代价是要在末 rank 把数据折回来」。

**运行结果**：待本地验证（需要多 GPU 与 NCCL 环境）。

#### 4.1.5 小练习与答案

**练习 1**：DualPipeV 为什么不需要 `is_in_second_half`？

> 参考答案：因为 DualPipeV 单向灌水，phase 0 对所有 rank 都是前向、phase 1 都是反向，phase 含义是全局绝对的，不随 rank 所处半段而变，所以不需要翻转。

**练习 2**：DualPipeV 把 `is_middle_rank` 替换成了哪个标志？为什么？

> 参考答案：替换成了 `is_last_rank`。因为折叠点从「流水线对折的中点」移到了「V 型顶点」即末 rank。

---

### 4.2 V 型转折：detach 衔接与梯度桥

这是 DualPipeV 与 DualPipe **最核心的几行差异**，也是「V 型」这个名字的来源。

#### 4.2.1 概念说明

V 型转折要解决一个问题：前向（phase 0）跑到 last rank 后，它的输出怎么变成反向（phase 1）的输入，让数据「折」回来？而且折回来之后，反向的梯度还要能正确地回流到前向，保证整条链的梯度数学正确。

DualPipeV 用一对「对称」的操作在 last rank 本地完成这件事，**完全不经过通信**：

- **前向折返（detach 衔接）**：last rank 的 phase 0 输出做 `detach().requires_grad_()`，得到的「新叶子张量」追加进 `input_chunks[1]`，成为 phase 1 的输入。
- **反向桥接（梯度桥）**：last rank 的 phase 1 反向算出的输入梯度，不是发给上游，而是追加进 `output_grad_chunks[0]`，成为 phase 0 的输出梯度。

为什么必须 `detach`？

1. **调度自由**：detach 把原本连通的自动微分图切成两张独立的子图（phase 0 一张、phase 1 一张），它们才能被独立调度、与通信重叠——这正是流水线并行的前提。若不 detach，整张图连通，反向必须严格按全局顺序执行，无法重叠。
2. **梯度等价**：`detach()` 只是断开图、不改变数值；`.requires_grad_()` 让新叶子能反传。配合反向阶段的「梯度桥」手动把 phase 1 的输入梯度回灌给 phase 0 的输出，**最终各参数的梯度与一张连通图完全一致**——示例里的 `cal_diff < 1e-13` 校验就是在验证这一点。

用一个文字图说明（N=num_ranks，前向 →，反向 ←，⤓ 表示 detach 折返）：

```text
前向 phase 0 : rank0[stage0] → rank1[stage1] → … → rank(N-1)[stage N-1]
                                                       ⤓ detach().requires_grad_()
反向 phase 1 : rank0[stage 2N-1] ← … ← rank1[stage N+1] ← rank(N-1)[stage N]
                                                       ⤒ 梯度桥（input_grad → output_grad）
反向 phase 0 : ←———————————————————————————————————————— rank(N-1) 用桥来的梯度做 run_backward
```

#### 4.2.2 核心流程

**前向折返**（`_forward_compute_chunk` 与 `_forward_backward_compute_chunk` 的 post-forward 段）：

```text
若 (is_last_rank 且 phase==0):
    把 outputs 每个张量 detach().requires_grad_()
    追加到 input_chunks[1]   ← 本地折返，成为 phase 1 的下一个输入
```

**反向桥接**（`_backward_compute_chunk` 与 `_forward_backward_compute_chunk` 的 post-backward 段）：

```text
算出 phase 的 input_grads = [t.grad for t in inputs]
若 (is_last_rank 且 phase==1):
    把 input_grads 追加到 output_grad_chunks[0]   ← 本地桥接，成为 phase 0 的输出梯度
否则:
    把 input_grads 追加到 input_grad_chunks[phase]  ← 正常情况，准备发给上游
```

此外，`is_last_stage`（算 loss 的终点）定义也变了：DualPipeV 只有 `is_first_rank 且 phase==1` 才算 loss，因为反向 phase 1 的终点在 first rank。

#### 4.2.3 源码精读

**前向计算里的 detach 折返**。这是 DualPipe 素有而 DualPipeV 新增的两行：

[dualpipe/dualpipev.py:70](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipev.py#L70)：`is_last_stage = (self.is_first_rank and phase == 1)`——只有 first rank 的 phase 1 算 loss（对照 DualPipe 的 [dualpipe.py:75](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L75) 多了 `or (self.is_last_rank and phase == 0)`）。

[dualpipe/dualpipev.py:79-80](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipev.py#L79-L80)：**V 型折返的核心两行**：

```python
if self.is_last_rank and phase == 0:
    self.input_chunks[1].append([output.detach().requires_grad_() for output in outputs])
```

这段在 DualPipe 的 `_forward_compute_chunk` 里**完全不存在**——DualPipe 两端都喂数据，不存在「折返」。`output.detach()` 截断自动微分图，`.requires_grad_()` 让新张量可反传，然后塞进 `input_chunks[1]`，成为同一 rank 上 phase 1 的输入。

[dualpipe/dualpipev.py:81-82](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipev.py#L81-L82)：把输出追加进 `output_chunks[phase]`（与 DualPipe 一致，只是这里的 `phase` 不再翻转）。

**反向计算里的梯度桥**：

[dualpipe/dualpipev.py:112-118](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipev.py#L112-L118)：算出 `input_grads` 后按 rank/phase 分流：

```python
inputs = self.input_chunks[phase][chunk_id]
self.input_chunks[phase][chunk_id] = None
input_grads = [t.grad for t in inputs]
if self.is_last_rank and phase == 1:
    self.output_grad_chunks[0].append(input_grads)   # 梯度桥
else:
    self.input_grad_chunks[phase].append(input_grads)  # 正常发上游
```

对照 DualPipe 的 [dualpipe.py:116-119](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L116-L119)，DualPipe 永远走 `else` 分支（直接进 `input_grad_chunks[phase]`），没有桥。原因：phase 1 的输入梯度就是 phase 1 模块对其输入的梯度，正常发给上游即可；而 DualPipeV 在 last rank 的 phase 1 输入正是 detach 来的张量，它的 `.grad` 必须桥到 phase 0 的输出梯度位，phase 0 反向才能用 `run_backward(outputs, output_grads)` 把梯度继续往前传。

**重叠版（`_forward_backward_compute_chunk`）里的同一对操作**：

[dualpipe/dualpipev.py:171-172](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipev.py#L171-L172)：post-forward 段的折返（`is_last_rank and phase0 == 0` 时把 `outputs0` 折进 `input_chunks[1]`）。对照 DualPipe 的 [dualpipe.py:173-175](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L173-L175) 没有折返。

[dualpipe/dualpipev.py:182-185](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipev.py#L182-L185)：post-backward 段的梯度桥（`is_last_rank and phase1 == 1` 时桥到 `output_grad_chunks[0]`）。对照 DualPipe 的 [dualpipe.py:179-183](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L179-L183) 永远走 `input_grad_chunks`。

**为什么 V 型衔接不需要通信？** 看 last rank 的通信短路：

[dualpipe/dualpipev.py:233-235](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipev.py#L233-L235)：`_recv_forward` 在 `is_last_rank and phase == 1` 时直接 `return`——last rank 的 phase 1 输入不靠收信，靠的就是上面 detach 折返本地塞进来的。

[dualpipe/dualpipev.py:258-260](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipev.py#L258-L260) 与 [265-270](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipev.py#L265-L270)：`_recv_backward` 在 `is_last_rank and phase == 0` 短路、`_send_backward` 在 `is_last_rank and phase == 1` 短路——梯度桥同样在本地完成，不收不发。这正是 V 型顶点「就地折返、零通信」的优雅之处。

> 对照要点：DualPipe 的四个通信原语开头都有 `phase ^= self.is_in_second_half`（如 [dualpipe.py:232](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L232)），DualPipeV 全部删去；且 DualPipe 用 `is_middle_rank` 相关判定，DualPipeV 用 `is_first_rank`/`is_last_rank` 直接判定首末短路。

#### 4.2.4 代码实践（本讲指定实践任务）

**实践目标**：并排对比 DualPipeV 与 DualPipe 的 `_forward_compute_chunk` / `_backward_compute_chunk`，亲手标出 last rank 处 phase0↔phase1 衔接的差异代码。

**操作步骤**：

1. 打开两个文件并排：
   - [dualpipe/dualpipev.py:63-82](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipev.py#L63-L82)（`_forward_compute_chunk`）
   - [dualpipe/dualpipe.py:67-85](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L67-L85)（`_forward_compute_chunk`）
2. 在 DualPipeV 版本里，用记号标出 4 处 V 型差异：
   - **(A) 折返**：第 79-80 行 `if self.is_last_rank and phase == 0: input_chunks[1].append([output.detach().requires_grad_() ...])`。
   - **(B) 终点定义**：第 70 行 `is_last_stage = (self.is_first_rank and phase == 1)`（少了 DualPipe 的 last rank 那一支）。
3. 再并排对比反向：
   - [dualpipe/dualpipev.py:112-118](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipev.py#L112-L118)
   - [dualpipe/dualpipe.py:116-119](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L116-L119)
4. 标出反向的 **(C) 梯度桥**：第 115-116 行 `if self.is_last_rank and phase == 1: self.output_grad_chunks[0].append(input_grads)`。
5. 为每个标记写一句话解释其作用（参考 4.2.1）。

**需要观察的现象**：DualPipeV 比 DualPipe **多出**两块逻辑（折返、梯度桥），而 DualPipe 这两处都是「无」——因为 DualPipe 两个方向各自有独立的数据入口，不需要在某个 rank 把一个方向的产物折成另一个方向的输入。

**预期结果**：你能填出下表——

| 衔接环节 | DualPipe | DualPipeV（last rank） |
|---|---|---|
| phase0 输出 → phase1 输入 | 无（两端独立喂数据） | `detach().requires_grad_()` 折返进 `input_chunks[1]` |
| phase1 输入梯度 → phase0 输出梯度 | 无 | 桥接进 `output_grad_chunks[0]` |
| 是否需要通信 | — | 否（全部本地） |

**运行结果**：本实践为源码阅读型，无需运行；如需数值验证，可运行 `examples/example_dualpipev.py`，确认 `cal_diff < 1e-13` 通过（即 detach+桥接后的梯度与连通图一致）。该运行结果待本地验证（需多 GPU）。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `output.detach().requires_grad_()` 改成直接 `output`（不 detach），会发生什么？

> 参考答案：phase 0 与 phase 1 会并入同一张连通的自动微分图，phase 1 反向会自动继续往 phase 0 传梯度。这样虽然梯度仍正确，但破坏了两段子图的独立性，引擎的 F&B 重叠调度将无法生效，且 `input_chunks[1]` 会持有可能被提前释放的图节点，行为不可控。

**练习 2**：梯度桥为什么放在 `is_last_rank and phase == 1`，而不是别的位置？

> 参考答案：phase 1 的输入正是在 last rank 由 detach 折返而来的张量，它的 `.grad` 是「相对 detach 新叶子的梯度」。要把它变成 phase 0 的输出梯度供 `run_backward` 使用，必须在 last rank、phase==1 反向完成的那一刻桥接，时序与位置都唯一。

**练习 3**：DualPipeV 的 loss 只在哪一个 rank 的哪一个 phase 计算？为什么？

> 参考答案：只在 `is_first_rank and phase == 1`。因为 phase 1 反向链的终点在 first rank，数据从 first rank 进入、绕 V 一圈后回到 first rank，loss 自然在此计算。

---

### 4.3 DualPipeV.step：八步调度与循环公式差异

#### 4.3.1 概念说明

`DualPipeV.step` 的骨架与 `DualPipe.step` 完全同构：入口校验 → scatter 切分 → 八步调度 → loss/outputs 聚合 → 复位返回。八步的名字也一一对应（nF0 / nF0F1 / nB1W1F1 / nF0B1F1B0 / nB1F1B0 / nB1B0 / nWB0 / nW），零气泡机制（step 3/6/7 启用、step 8 排空）也照搬。

差异集中在三处：

1. **scatter 范围**：DualPipe 在 first/last 两个 rank 各 scatter **一半**（`half_num_chunks`）；DualPipeV 只在 first rank scatter **全部**（`num_chunks`）。
2. **循环次数公式**：DualPipe 用「半量」量（`num_half_ranks`、`half_rank`、`half_num_chunks`）驱动；DualPipeV 直接用全量量（`num_ranks`、`rank`、`num_chunks`）驱动。
3. **step 4 的折叠点特判**：DualPipe 用 `is_middle_rank`，DualPipeV 用 `is_last_rank`。

直觉解释：DualPipe 的调度是「两条对称半链」，所以公式都按半链长度算；DualPipeV 是「一条整链折成 V」，半链长度就等于整链长度，于是「半量」自然替换成「全量」。

#### 4.3.2 核心流程

DualPipeV.step 主流程：

```text
1. 校验 TENSOR_SHAPES / TENSOR_DTYPE、forward_only、num_chunks >= num_ranks*2
   （注意：不要求 num_ranks 为偶数，也不要求 num_chunks 为偶数）
2. _reset_states()
3. 若 is_first_rank：scatter(inputs, num_chunks) → input_chunks[0]；scatter(labels) → labels
4. 八步调度（公式见下表）
5. _commit_and_wait_comm()
6. 若 is_first_rank：聚合 loss = torch.stack(loss_chunks)；按需 gather outputs
7. _reset_states() 后返回 (loss, outputs)
```

八步循环次数公式对照（替换规则：`num_half_ranks→num_ranks`、`half_rank→rank`、`half_num_chunks→num_chunks`、`is_middle_rank→is_last_rank`）：

| 步骤 | DualPipe 公式 | DualPipeV 公式 |
|---|---|---|
| step_1（nF0） | \((\text{num\_half\_ranks} - \text{half\_rank} - 1)\times 2\) | \((\text{num\_ranks} - \text{rank} - 1)\times 2\) |
| step_2（nF0F1） | \(\text{half\_rank} + 1\) | \(\text{rank} + 1\) |
| step_3（nB1W1F1） | \(\text{num\_half\_ranks} - \text{half\_rank} - 1\) | \(\text{num\_ranks} - \text{rank} - 1\) |
| step_4（nF0B1F1B0） | \(\text{half\_num\_chunks} - \text{num\_ranks} + \text{half\_rank} + 1\) | \(\text{num\_chunks} - \text{num\_ranks}\times 2 + \text{rank} + 1\) |
| step_5（nB1F1B0） | \(\text{num\_half\_ranks} - \text{half\_rank} - 1\) | \(\text{num\_ranks} - \text{rank} - 1\) |
| step_6（nB1B0） | \(\text{half\_rank} + 1\) | \(\text{rank} + 1\) |
| step_7（nWB0） | \(\text{num\_half\_ranks} - \text{half\_rank} - 1\) | \(\text{num\_ranks} - \text{rank} - 1\) |
| step_8（nW） | \(\text{half\_rank} + 1\) | \(\text{rank} + 1\) |

负载均衡恒等式也对应升级：

- DualPipe：\(\text{step\_1} + \text{step\_2} + \text{step\_4} = \text{half\_num\_chunks}\)（phase 0 前向恰好算完一半 chunk）。
- DualPipeV：\(\text{step\_1} + \text{step\_2} + \text{step\_4} = \text{num\_chunks}\)（phase 0 前向恰好算完**全部** chunk）。

零气泡依旧由 `WeightGradStore` 实现：step 3/6/7 把权重梯度 `put` 延后、用 `_weight_chunk` 的 `pop` 填进气泡，step 8 全部排空并以 `assert funcs_queue.empty()` 收尾——这套机制 DualPipeV 与 DualPipe 完全一致。

#### 4.3.3 源码精读

**入口与校验**：

[dualpipe/dualpipev.py:311-312](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipev.py#L311-L312)：断言 `TENSOR_SHAPES` / `TENSOR_DTYPE` 已设置。

[dualpipe/dualpipev.py:318](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipev.py#L318)：`assert num_chunks > 0 and num_chunks >= num_ranks * 2`。对照 DualPipe 的 [dualpipe.py:332-333](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L332-L333) 多了 `assert num_ranks % 2 == 0` 和 `num_chunks % 2 == 0`——DualPipeV **不要求**偶数设备数/批次数，所以示例入口可「步长 −1」遍历任意 GPU 数。

**scatter（只 first rank，切全部）**：

[dualpipe/dualpipev.py:325-328](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipev.py#L325-L328)：

```python
if self.is_first_rank:
    self.input_chunks = (scatter(inputs, num_chunks, self.batch_dim), [])
    self.labels = scatter(labels, num_chunks, self.batch_dim)
    self.criterion = criterion
```

注意 `labels` 在 DualPipeV 里是**单层列表**（按 chunk_id 索引），而 DualPipe 的 `labels` 是 `[phase0, phase1]` 二元组（见 [dualpipe.py:345-353](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L345-L353)，两端各喂一半）。相应地，`_reset_states` 里 `self.labels` 的类型注解也不同：

[dualpipe/dualpipev.py:50](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipev.py#L50)：`self.labels: List[List[torch.Tensor]] = None`（单层）。对照 [dualpipe.py:54](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L54) 是 `Tuple[...][...]`（phase 二元组）。`_forward_compute_chunk` 里取标签也由 `self.labels[phase][chunk_id]` 简化为 `self.labels[chunk_id]`。

**八步调度**：

[dualpipe/dualpipev.py:330-396](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipev.py#L330-L396)：八步循环。逐段看关键差异——

step_1（[331-333](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipev.py#L331-L333)）：`step_1 = (num_ranks - rank - 1) * 2`，灌水阶段，只做 `_forward_chunk(0)`。

step_4 主步（[353-367](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipev.py#L353-L367)）：i==0 时对折叠点特判：

```python
if i == 0:
    if self.is_last_rank:                    # ← DualPipe 用 is_middle_rank
        # NOTE: We don't overlap these two chunks to further reduce bubble size.
        self._forward_chunk(0, recv=False, send=False)
        self._send_forward(1)
        self._backward_chunk(1, send=False)
        self._send_forward(0)
        self._send_backward(1)
    else:
        self._forward_backward_chunk(0, 1, recv0=False)
else:
    self._forward_backward_chunk(0, 1)
self._forward_backward_chunk(1, 0)
```

特判的**函数体与 DualPipe 逐字相同**（对照 [dualpipe.py:384-396](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L384-L396)），唯一差别是守卫从 `is_middle_rank` 换成 `is_last_rank`——因为「不重叠这两个 chunk 以进一步缩气泡」的折叠点，在 V 型里就是 last rank。

step_6 零气泡奇偶切换（[376-384](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipev.py#L376-L384)）：`rank % 2` 替换 DualPipe 的 `half_rank % 2`，逻辑相同（错峰启用零气泡以平衡队列）。

step_8 收尾（[393-396](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipev.py#L393-L396)）：排空权重梯度并 `assert WeightGradStore.funcs_queue.empty()`。

**loss/outputs 聚合（只 first rank）**：

[dualpipe/dualpipev.py:400-407](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipev.py#L400-L407)：

```python
loss, outputs = None, None
if self.is_first_rank:
    if criterion is not None:
        loss = torch.stack(self.loss_chunks)
    if return_outputs:
        outputs = gather(self.output_chunks[1], self.batch_dim)
        if len(outputs) == 1:
            outputs = outputs[0]
```

对照 DualPipe 的 [dualpipe.py:429-436](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L429-L436)：DualPipe 在 `is_first_rank or is_last_rank` 都可能返回 loss，且用 `output_chunks[self.is_first_rank]`（first 取 phase 1、last 取 phase 0）；DualPipeV 恒为 `output_chunks[1]`，因为终点永远是 first rank 的 phase 1。这与示例校验一致——[example_dualpipev.py:152-155](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipev.py#L152-L155) 只有 `is_first_rank` 断言 loss，其余断言 `loss is None`。

#### 4.3.4 代码实践

**实践目标**：取 README 示例规模（4 PP ranks、10 micro-batches），手算每个 rank 的八步循环次数，验证负载均衡恒等式，并对照调度图理解每步。

**操作步骤**：

1. 设 `num_ranks=4, num_chunks=10`（即 README「4 PP ranks (8 PP stages) and 10 micro-batches」）。
2. 用 DualPipeV 公式算 rank 0..3 的各步次数：
   - `step_1 = (num_ranks-rank-1)*2`，`step_2 = rank+1`，`step_4 = num_chunks - num_ranks*2 + rank + 1 = 3 + rank`，其余奇偶步同理。
3. 逐行填表（见预期结果）。
4. 对每个 rank 验证 `step_1 + step_2 + step_4 == num_chunks == 10`。
5. 打开 README 的 [DualPipeV 调度图说明 22 行](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/README.md#L22)（`images/dualpipev.png`），对照调度图找出 step_1（灌水）、step_4（稳态全重叠）、step_8（收尾排空）对应图中的区域。

**需要观察的现象**：rank 越靠后，step_1（纯灌水）越短、step_4（稳态主步）越长；首 rank step_1 最长（要先把前向灌满），末 rank step_1=0（立刻进入稳态并承担 V 型折返）。

**预期结果**：

| rank | step_1 | step_2 | step_3 | step_4 | step_5 | step_6 | step_7 | step_8 | step_1+2+4 |
|---|---|---|---|---|---|---|---|---|---|
| 0（首） | 6 | 1 | 3 | 3 | 3 | 1 | 3 | 1 | **10** ✓ |
| 1 | 4 | 2 | 2 | 4 | 2 | 2 | 2 | 2 | **10** ✓ |
| 2 | 2 | 3 | 1 | 5 | 1 | 3 | 1 | 3 | **10** ✓ |
| 3（末） | 0 | 4 | 0 | 6 | 0 | 4 | 0 | 4 | **10** ✓ |

末 rank（rank 3）的 step_1=0 正印证了「V 型顶点不灌水、直接折返」——它的 phase 0 输出立刻 detach 成 phase 1 输入。

**运行结果**：本实践为公式手算 + 读图，无需运行；若想用代码核对，可在 `DualPipeV.step` 的每个 `for` 循环前后加一行 `print(rank, "step_k", i)` 观察实际迭代次数。运行结果待本地验证（需多 GPU）。

#### 4.3.5 小练习与答案

**练习 1**：把 DualPipeV 的 step_4 公式 `num_chunks - num_ranks*2 + rank + 1` 代入 DualPipe 的替换规则反推，应得到 DualPipe 的什么表达式？

> 参考答案：按「`num_chunks→half_num_chunks`、`num_ranks→num_ranks`（注意 num_ranks 在 DualPipe 公式里不变，但其一半是 num_half_ranks）、`rank→half_rank`」，DualPipeV 的 `num_chunks - num_ranks*2 + rank + 1` 对应 DualPipe 的 `half_num_chunks - num_ranks + half_rank + 1`（因为 `num_ranks*2` 对应 DualPipe 里 `num_ranks = 2*num_half_ranks`）。这正是 [dualpipe.py:382](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L382)。

**练习 2**：DualPipeV 为何取消 `num_ranks % 2 == 0` 的断言？

> 参考答案：DualPipe 需要偶数 rank 才有明确的 middle rank 对折点；DualPipeV 的折叠点是 last rank，对任意 rank 数都存在，且调度公式只依赖 `rank` 与 `num_ranks`，无需对折对称，故不要求偶数。

**练习 3**：相同模型深度（PP 个 stage）下，DualPipeV 比 DualPipe 少用多少设备？代价是什么？

> 参考答案：DualPipeV 只需 PP/2 个设备（DualPipe 需 PP 个）。气泡公式与激活显存（PP+1）相同，代价是每设备仍持 2 个阶段（2× 参数），且数据要绕 V 一圈——端到端单步延迟更长，但单位设备吞吐更高。（详见下一讲 u4-l2。）

---

## 5. 综合实践

**任务**：用一张大表，把 DualPipeV 与 DualPipe 从「拓扑 → 数据喂入 → V 型衔接 → 八步公式 → 聚合返回」做一次端到端的差异梳理，并标注每条差异对应的源码行号。

**操作步骤**：

1. 建一张 6 列表：差异主题 ｜ DualPipe 做法 ｜ DualPipeV 做法 ｜ DualPipeV 源码行 ｜ DualPipe 源码行（对照）｜ 设计动机。
2. 至少覆盖以下主题：
   - 拓扑标志（`is_in_second_half` / `is_middle_rank` 的有无）
   - 数据喂入（两端各半 ｜ 首端全部）
   - V 型折返（无 ｜ `detach().requires_grad_()`）
   - 梯度桥（无 ｜ `output_grad_chunks[0].append`）
   - 通信原语的 `phase ^=` 翻转（有 ｜ 无）
   - step_4 折叠点守卫（`is_middle_rank` ｜ `is_last_rank`）
   - 循环公式的量（半量 ｜ 全量）
   - loss/outputs 返回（首或末 ｜ 仅首）
3. 逐行核对源码行号（用本讲给出的永久链接）。
4. 在表末写一段话：如果你只有一半的 GPU，应该选哪个？如果模型很深、想要最小气泡，又该怎么选？

**预期结果**：一张可直接作为「DualPipe vs DualPipeV 速查表」的文档，每条差异都能跳转到源码定位。这张表也将是下一讲 u4-l2（双向 vs V 型取舍）的直接输入。

**运行结果**：本实践为文档梳理型，无需运行。

## 6. 本讲小结

- DualPipeV 是 DualPipe 的「切半」V 型变体：**只在 first rank 喂数据**，数据前向跑到 last rank 后折返、反向跑回 first rank 算 loss。
- 拓扑更简：去掉了 `is_in_second_half` 与 `is_middle_rank`，phase 含义全局绝对，所有通信/计算原语开头的 `phase ^= is_in_second_half` 全部删除。
- V 型顶点（last rank）靠两对本地产出衔接：前向 `detach().requires_grad_()` 把 phase 0 输出折成 phase 1 输入；反向把 phase 1 输入梯度桥成 phase 0 输出梯度——**全程零通信**，且梯度与连通图等价。
- 八步调度骨架与零气泡机制照搬 DualPipe，循环公式把「半量」(`num_half_ranks`/`half_rank`/`half_num_chunks`) 替换为「全量」(`num_ranks`/`rank`/`num_chunks`)，折叠点守卫由 `is_middle_rank` 换成 `is_last_rank`。
- 相同模型深度下 DualPipeV 只需 PP/2 个设备（DualPipe 需 PP 个），气泡与激活显存相同——这是 V 型的核心收益。

## 7. 下一步学习建议

- **下一讲 u4-l2（双向 vs V 型：气泡、显存与设备取舍）**：基于本讲的差异表与 README 对比表，量化分析两者在气泡大小、激活显存、设备数上的工程取舍，给出选型建议。
- **u4-l3（实战：自定义流水线模块与正确性验证）**：动手实现一个带 `overlapped_forward_backward` 的 `PipelineStage`，用 `ref_step` 与 `cal_diff` 验证 DualPipeV 的梯度正确性（即验证本讲讲的 detach+梯度桥确实数学等价）。
- 继续精读：把 `dualpipe/dualpipev.py` 的 `_forward_backward_compute_chunk`（[120-185 行](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipev.py#L120-L185)）与 DualPipe 的同名函数（[dualpipe.py:121-183](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L121-L183)）并排读完，确认重叠版里的折返与梯度桥和非重叠版完全一致。
