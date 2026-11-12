# build_plan 精读：两种模式的拷贝发射

## 1. 本讲目标

上一讲（u3-l1）我们认识了三个数据结构：`A2ADims` 描述交换几何、`CopyOp` 描述一条带步长的字节拷贝、`A2APlan` 汇总输出形状、窗口尺寸与发送清单。本讲钻进唯一的构建函数 `build_plan`，读完你应该能够：

1. **推导 mode 0 下任意 (rank, peer) 对的 src/dst offset 公式**——不看源码也能手算出一条 `CopyOp` 的全部字段。
2. **解释 mode 1 为什么必须写跨步**——核心是「窗口即返回张量」这一设计决定：接收方的对称内存窗口直接按输出布局存放，没有事后的本地重排 pass。
3. **说明 window_numel 为什么取所有 rank 中的最大接收量**，以及为什么每个 rank 能不通信地算出同一个值。
4. **说明本 rank 自己的份额为什么也走窗口**，这个看似多余的自拷贝如何让 `copy_out` 退化成一次平坦拷贝、并让零拷贝路径成为可能。

本讲依旧**不需要 GPU**：`build_plan` 是纯主机算术（u1-l4 讲过它无 CUDA、无 torch、无通信库），全部实践用 `a2a_plan_debug` 算子加 numpy 字节缓冲即可完成。

## 2. 前置知识

### 2.1 带步长拷贝（pitched copy）的直觉

把一次 2D 拷贝想象成「铺瓷砖」：每块瓷砖 `width` 字节，一行 `rows` 块瓷砖，相邻瓷砖的起点间距是 `pitch`。当 `pitch == width` 时瓷砖首尾相连（连续内存）；当 `pitch > width` 时瓷砖之间有缝隙（跨步内存）。`cudaMemcpy2DAsync` 就是按这套参数搬字节，`CopyOp` 的字段与之逐一对应（u3-l1 已建立映射，u5-l1 将在 CUDA 侧兑现）。

### 2.2 记号约定（本讲全程使用）

设 `e = elem_size`（每个元素的字节数），记：

\[ D = d \cdot e \qquad \text{（一个「头 × 深度」单元的字节数）} \]

\[ N = \text{head\_total} = \sum_p n_p \qquad S_p = \text{seq\_offsets}[p] \qquad H_p = \text{head\_offsets}[p] \]

其中 \(n_p\)、\(s_p\) 分别是 `head_splits[p]`、`seq_splits[p]`，\(S\)、\(H\) 是两张表的排他前缀和（u3-l1 讲过：免通信地给出全组偏移）。回顾 u2-l3 的关键语义：**splits 是「组的所有权表」**——\(s_p\) 是 rank \(p\) 拥有的全局序列分片长度，\(n_p\) 是它拥有的全局头列数，它们描述的是切分几何，而不是某次调用各自的收发量。

### 2.3 两个模式的形状契约

来自 [include/fast_ulysses/a2a_plan.hpp:L12-L17](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/a2a_plan.hpp#L12-L17) 的枚举注释：

| 模式 | 枚举名 | 输入（本 rank 持有） | 输出（本 rank 收到） |
| --- | --- | --- | --- |
| mode 0 | `kScatterHead` | `[b, s_me, N, d]`（我的序列分片 × 全部头） | `[b, seq_total, n_me, d]`（完整序列 × 我的头列） |
| mode 1 | `kGatherHead` | `[b, seq_total, n_me, d]` | `[b, s_me, N, d]` |

mode 1 是 mode 0 的严格逆变换（u1-l2）。

### 2.4 CopyOp 字段速查

来自 [include/fast_ulysses/a2a_plan.hpp:L40-L51](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/a2a_plan.hpp#L40-L51)：

| 字段 | 含义 |
| --- | --- |
| `peer` | 这条拷贝发往哪个 rank 的窗口 |
| `src_offset` / `dst_offset` | 源/目的起点，**相对各自缓冲基址的字节偏移**（不含指针） |
| `src_pitch` / `dst_pitch` | 源/目的相邻行的字节间距 |
| `width` | 每行的字节数 |
| `rows` | 行数 |
| `depth` / `src_slice` / `dst_slice` | 批维折叠：`depth` 个批次副本按各自 slice 步长重复 |

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| `src/a2a_plan.cc` | 纯主机 plan 构建器（无 CUDA/无 torch/无通信库） | `build_plan` 的两个分支、窗口循环、`push_batched` |
| `include/fast_ulysses/a2a_plan.hpp` | 数据结构与设计契约注释 | `CopyOp`/`A2APlan` 字段语义、「自份额走窗口」的设计注释 |
| `src/plan_bindings.cc` | torch 与 plan 的唯一相遇点（u1-l4） | `a2a_plan_debug` 的 10 字段输出顺序，实践的取数依据 |
| `test/test_plan.py` | 无 GPU 的布局测试 | `apply_ops`/`run_plan` 重放、窗口断言、往返测试，实践的参照 |
| `src/bindings.cc` | 算子层（本讲只引用） | `copy_out`：自份额走窗口换来的「一次平坦拷贝」 |
| `src/group.cc` | 有状态层（本讲只引用） | `plan()` 的缓存调用点，`build_plan` 的真实客户 |

真实调用点在 [src/group.cc:L191-L195](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L191-L195)：`UlyssesGroup::plan()` 用 `make_dims` 把张量尺寸翻译成 `A2ADims`，再按 dtype 的 `elementSize` 调 `build_plan`，结果按形状键缓存。本讲只关心 `build_plan` 内部。

## 4. 核心概念与源码讲解

先给出 `build_plan` 的骨架（[src/a2a_plan.cc:L143-L216](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc#L143-L216)）：

```text
build_plan(dims, mode, elem_size):
    validate()                       # u3-l1 讲过：六组检查 + 溢出防护，先于一切分配
    计算 seq_total / head_total / 两张前缀和表 / s_me / n_me / d_bytes
    plan.output_shape = 按 mode 取 [b, seq_total, n_me, d] 或 [b, s_me, N, d]
    窗口循环：对每个 p 算「p 将收到的元素数」，window_numel 取最大
    发射循环：对每个 p 生成一条（或一批）CopyOp —— mode 0 与 mode 1 两个互为镜像的分支
    return plan
```

本讲的四个最小模块分别对应：mode 0 分支（4.1）、mode 1 分支（4.2）、窗口循环（4.3）、发射循环里「不特判 p == rank」这一决定（4.4）。

为了能手算验证，本讲用一个贯穿全文的小例子：

> **贯穿例子**：\(b=1,\ d=1,\ e=1\)（设想 1 字节 dtype；plan 层只做字节算术，16 字节对齐是上层 `bindings`/`group` 的契约，u2-l2 讲过），`seq_splits = [2,1,0]`（rank 2 没有序列分片），`head_splits = [2,1,1]`。
> 于是 \(N=4\)，`seq_offsets = [0,2,3]`，`head_offsets = [0,2,3]`，`seq_total = 3`。

### 4.1 mode 0 scatter 分支：读跨步、写连续

#### 4.1.1 概念说明

mode 0 之前，每个 rank 持有「我的序列分片 × **全部**头」；之后要变成「**完整**序列 × 我的头列」。所以 rank \(r\) 必须把它的每一行序列中**属于 peer \(p\) 的那几列头**送出去，同时从所有 peer 那里收回自己的头列。源码注释一语道破（[src/a2a_plan.cc:L177-L179](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc#L177-L179)）：

> Send peer p the head columns it owns, for every local sequence position; they land in the slice of p's window that this rank's sequence shard occupies. The window rows are contiguous, so this reads strided and writes contiguous.

「读跨步」：我的输入一行是 \(N \cdot D\) 字节（跨全部头），但每次只取其中 \(n_p \cdot D\) 字节的一列，所以源 pitch 是满行宽。「写连续」：peer \(p\) 的窗口按它的输出 `[b, seq_total, n_p, d]` 行主序布局，我的序列分片在全局序列里占据连续的 \(s_r\) 行，写入处每行恰好就是 `width`，所以目的 pitch 等于 width——瓷砖无缝铺满。

#### 4.1.2 核心流程

sender \(r\) → receiver \(p\) 的字段推导（记 \(D = d\cdot e\)）：

\[
\begin{aligned}
\text{width} &= n_p \cdot D &&\text{（p 拥有的头列 × d，一行瓷砖的字节数）}\\
\text{src\_offset} &= H_p \cdot D &&\text{（从我的输入中 p 的头列开始读）}\\
\text{src\_pitch} &= N \cdot D &&\text{（我的输入一行跨全部头 → 跨步读）}\\
\text{dst\_offset} &= S_r \cdot \text{width} &&\text{（p 的窗口里，我的序列分片从全局行 } S_r \text{ 开始）}\\
\text{dst\_pitch} &= \text{width} &&\text{（连续写：pitch == width）}\\
\text{rows} &= s_r &&\text{（我的全部本地序列行）}
\end{aligned}
\]

注意两个对称的角色：**源侧按「头」寻址**（\(H_p\) 决定从哪读），**目的侧按「序列」寻址**（\(S_r\) 决定写到哪）。批维经 `push_batched` 折叠：`src_stride = s_r · N · D`（我输入的一个批次元素），`dst_stride = seq_total · width`（p 窗口的一个批次元素）。

**手算贯穿例子（rank 1，\(s_1 = 1\)，输入是 4 个头的一行）**，对每个 peer \(p\)：

| p | width \(=n_p D\) | src_offset \(=H_p D\) | src_pitch \(=ND\) | dst_offset \(=S_1\cdot width\) | dst_pitch | rows \(=s_1\) |
| --- | --- | --- | --- | --- | --- | --- |
| 0 | 2 | 0 | 4 | 2·2 = 4 | 2 | 1 |
| 1 | 1 | 2 | 4 | 2·1 = 2 | 1 | 1 |
| 2 | 1 | 3 | 4 | 2·1 = 2 | 1 | 1 |

解读 p=0 那行：rank 1 把自己那一行序列的前 2 个头（字节 0..2）拷到 rank 0 窗口偏移 4 处——rank 0 的窗口是 `[1, 3, 2, 1]`，全局行 2（即 rank 1 的分片）起始于字节 \(2 \times 2 = 4\)。

#### 4.1.3 源码精读

mode 0 分支完整位于 [src/a2a_plan.cc:L176-L193](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc#L176-L193)：

```cpp
const int64_t row = dims.head_splits[p] * d_bytes;
if (row == 0 || s_me == 0) {
    continue;
}
CopyOp op;
op.peer       = p;
op.src_offset = head_offset[p] * d_bytes;
op.src_pitch  = head_total * d_bytes;
op.dst_offset = seq_offset[dims.rank] * row;
op.dst_pitch  = row;
op.width      = row;
op.rows       = s_me;
push_batched(plan.ops, op, dims.b, s_me * head_total * d_bytes, seq_total * row);
```

逐点说明：

- [src/a2a_plan.cc:L180-L183](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc#L180-L183)：守卫 `row == 0 || s_me == 0`。`row == 0` 表示 peer \(p\) 一个头都不拥有（发它没意义）；`s_me == 0` 表示我自己没有序列分片（无货可发）。**零长度分片是合法输入**（validate 只拒绝负数，[src/a2a_plan.cc:L132-L140](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc#L132-L140)），守卫保证它不发射任何拷贝——贯穿例子里 rank 2 的 mode 0 ops 是**空表**，但它照样接收 rank 0/1 写入的数据。
- `dst_offset = seq_offset[dims.rank] * row`：注意乘数用的是**发送者自己的**序列偏移 \(S_r\) 和**接收者的**行宽 `row`——一个偏移同时吃到两张表的条目，这正是「重排即步长」在偏移层面的样子。
- `push_batched`（[src/a2a_plan.cc:L70-L89](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc#L70-L89)，u3-l1 已精读）只在多行且步长可整除 pitch 时把 \(b\) 个批次折叠成一条 3D 拷贝；\(b=1\) 时 `depth=1`，直接落一条 2D 拷贝。贯穿例子 \(b=1\)，每个 peer 恰一条 op。

#### 4.1.4 代码实践

1. **实践目标**：用调试算子打印 mode 0 的发射清单，与 4.1.2 的手算表逐格核对。
2. **操作步骤**（无需 GPU、无需进程组；前提是按 u1-l3 构建过扩展）：

```python
# inspect_mode0.py
import torch  # import 即注册 torch.ops.fast_ulysses
plan = torch.ops.fast_ulysses.a2a_plan_debug(
    b=1, d=1, rank=1, world_size=3,
    seq_splits=[2, 1, 0], head_splits=[2, 1, 1],
    mode=0, elem_size=1,
)
output_shape, window_numel, ops = plan
print("output_shape =", list(output_shape))   # 预期 [1, 3, 1, 1]
print("window_numel =", int(window_numel))    # 预期 6（见 4.3）
print(ops.numpy())  # 每行 10 个字段，顺序见下
```

字段顺序来自 [src/plan_bindings.cc:L14-L15](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/plan_bindings.cc#L14-L15)：`peer, src_off, dst_off, src_pitch, dst_pitch, width, rows, depth, src_slice, dst_slice`。

3. **需要观察的现象**：`ops` 应有 3 行（p=0,1,2 各一条），与 rank 1 那行手算表完全一致，例如 p=0 行应为 `(0, 0, 4, 4, 2, 2, 1, 1, 0, 0)`——前 7 个字段即 4.1.2 的手算值；`depth=1` 且 `src_slice=dst_slice=0`，因为 \(b=1\) 时 `push_batched` 走未融合分支，只调偏移、不填 slice 字段（保留 `CopyOp` 的默认值 0，见 [include/fast_ulysses/a2a_plan.hpp:L48-L50](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/a2a_plan.hpp#L48-L50)；slice 只在融合分支被赋值，而 `depth=1` 时重放也用不到它们）。
4. **预期结果**：再对 `rank=2` 调一次，`ops` 应为空张量（形状 `[0, 10]`），但 `window_numel` 仍是 6、`output_shape` 仍是 `[1, 3, 1, 1]`——「不发货」与「照常收货」互不影响。**待本地验证**（以上数值由 4.1.2 公式手算得出，确定性强，但请运行确认）。

#### 4.1.5 小练习与答案

**练习 1**：贯穿例子中 rank 0（\(s_0=2\)）向 p=1 发送的 `dst_offset` 是多少？
答：`dst_offset = S_0 · width = 0 · (n_1·D) = 0`。rank 0 的序列分片从全局行 0 开始，所以它写进任何窗口都从字节 0 起；它的两条行分别落在窗口行 0 和行 1。

**练习 2**：若把本例改成 \(b=4\)，rank 1 → p=0 的 op 会变成什么？
答：`push_batched(op, depth=4, src_stride = 1·4·1 = 4, dst_stride = 3·2 = 6)`。因 `rows = 1`，不满足「只融合多行拷贝」的规则（[src/a2a_plan.cc:L72](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc#L72) `worth_fusing = depth > 1 && op.rows > 1`），退化为 4 条 `depth=1` 的 2D 拷贝，偏移各加一份 stride。这与 [test/test_plan.py:L196-L203](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_plan.py#L196-L203) 的断言一致。

**练习 3**：mode 0 里 `src_pitch` 由谁决定、`dst_pitch` 由谁决定？
答：`src_pitch = N·D` 只依赖**我自己的输入布局**（一行跨全部头）；`dst_pitch = n_p·D` 依赖**接收方 p 的头列数**。源步长描述我、目的步长描述对方——mode 1 将看到完全镜像的分工。

### 4.2 mode 1 gather 分支：读连续、写跨步（窗口即返回张量）

#### 4.2.1 概念说明

mode 1 是逆方向：我现在持有「完整序列 × 我的头列」，要把**属于 peer \(p\) 的那几段序列行**（只带我的头列）送回去。源码注释（[src/a2a_plan.cc:L195-L198](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc#L195-L198)）专门解释了为什么这次目的侧必须跨步：

> The inverse: send peer p the sequence rows it owns, carrying only this rank's head columns, into p's window at this rank's head offset -- a strided write. **Landing them contiguously in a per-sender segment is not available here: the window IS the returned tensor, so there is no local pass to interleave them afterwards.**

直觉对比两种排列的「形状」：

- mode 0 中被散射的轴（序列）在输出的**外层**（行方向）：rank \(r\) 送出的 \(s_r\) 行在接收方窗口里天然连续（全局行 \(S_r..S_r+s_r\)），所以能连续写。
- mode 1 中被散射的轴（头）在输出的**内层**（每行内部）：接收方 \(p\) 的输出一行是 `[head_total, d]`，由**所有发送者的头列拼接**而成。若 rank \(r\) 把自己的字节连续写到某处，它们只是「每行的一小段」，行与行之间必须留出其他 sender 的位置——这就是跨步写。

替代方案本可以是：各 sender 连续写到窗口里的「按发送者划分的段」，再由接收方跑一个本地重排内核把段交织成输出。但这会引入一个 SM 内核（违背零 SM 目标），并且窗口不再直接等于输出，零拷贝路径随之消失。设计者选择了让**拷贝引擎直接按最终布局跨步落位**——用 pitch 表达重排，一行代码都不加。

#### 4.2.2 核心流程

mode 1 的字段推导，与 4.1.2 逐条镜像（头 ⇄ 序列、读 ⇄ 写互换角色）：

\[
\begin{aligned}
\text{width} &= n_r \cdot D &&\text{（我的头列 × d）}\\
\text{src\_offset} &= S_p \cdot \text{width} &&\text{（p 的序列分片在我的输入里从全局行 } S_p \text{ 开始）}\\
\text{src\_pitch} &= \text{width} &&\text{（连续读：pitch == width）}\\
\text{dst\_offset} &= H_r \cdot D &&\text{（p 的窗口里，我的头列从 } H_r \text{ 开始）}\\
\text{dst\_pitch} &= N \cdot D &&\text{（p 的窗口一行跨全部头 → 跨步写）}\\
\text{rows} &= s_p &&\text{（p 拥有的序列行数）}
\end{aligned}
\]

对称性一目了然：mode 0 的源公式 ⇄ mode 1 的目的公式，`src_pitch = N·D` ⇄ `dst_pitch = N·D`，`rows` 的提供者从 \(s_r\)（我的货）换成 \(s_p\)（对方的货）。批维 stride 同样镜像：`src_stride = seq_total · width`，`dst_stride = s_p · N · D`。

**手算贯穿例子（rank 0，\(n_0 = 2\)，输入 `[1, 3, 2, 1]`）**，`row = n_0·D = 2`：

| p | \(s_p\) | width | src_offset \(=S_p\cdot width\) | src_pitch | dst_offset \(=H_0 D\) | dst_pitch \(=ND\) | rows \(=s_p\) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 0 | 2 | 2 | 0 | 2 | 0 | 4 | 2 |
| 1 | 1 | 2 | 2·2 = 4 | 2 | 0 | 4 | 1 |
| 2 | 0 | — | — | — | — | — | 跳过（守卫） |

解读 p=1 那行：把我的输入中全局行 2（rank 1 的序列分片，偏移 4..6 字节）那一行，拷进 rank 1 窗口偏移 0、行距 4 处——rank 1 的输出是 `[1, 1, 4, 1]`，第一行的字节 0..2 正是「rank 0 贡献的头列」。

#### 4.2.3 源码精读

mode 1 分支完整位于 [src/a2a_plan.cc:L194-L213](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc#L194-L213)：

```cpp
const int64_t row = n_me * d_bytes;
const int64_t s_p = dims.seq_splits[p];
if (row == 0 || s_p == 0) {
    continue;
}
CopyOp op;
op.peer       = p;
op.src_offset = seq_offset[p] * row;
op.src_pitch  = row;
op.dst_offset = head_offset[dims.rank] * d_bytes;
op.dst_pitch  = head_total * d_bytes;
op.width      = row;
op.rows       = s_p;
push_batched(plan.ops, op, dims.b, seq_total * row, s_p * head_total * d_bytes);
```

- [src/a2a_plan.cc:L201-L203](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc#L201-L203)：守卫换成 `row == 0 || s_p == 0`。`row == 0` 是我自己一个头都不拥有；`s_p == 0` 是对方没有序列分片、不接收。贯穿例子里**所有 rank** 发往 p=2 的 op 都被跳过——rank 2 的 mode 1 输出形状是 `[1, 0, 4, 1]`，元素数为 0，一个什么都不收的 rank。
- `dst_offset = head_offset[dims.rank] * d_bytes`：目的偏移由**我自己的**头偏移决定（在别人的窗口里，我的头列从 \(H_r\) 开始）——与 mode 0 的 `dst_offset` 用 \(S_r\) 形成精确对照。
- 头部注释 [include/fast_ulysses/a2a_plan.hpp:L62-L65](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/a2a_plan.hpp#L62-L65) 点明这份清单同时服务两个入口，并预告了 4.4 的内容：

> One plan serves both entry points: `ops` covers all world_size destinations, so this rank's own share travels through the window like every peer's -- which is what the zero-copy path needs, the window BEING the caller's output, and what makes the copying path's copy-out one flat copy.

#### 4.2.4 代码实践

1. **实践目标**：从真实 plan 中「看见」写跨步——用数据证实 mode 1 的 `dst_pitch > width`，并数出守卫跳过的 op。
2. **操作步骤**：

```python
# inspect_mode1.py
import torch
for rank in range(3):
    shape, window, ops = torch.ops.fast_ulysses.a2a_plan_debug(
        1, 1, rank, 3, [2, 1, 0], [2, 1, 1], mode=1, elem_size=1)
    print(f"rank {rank}: shape={list(shape)} window={int(window)} n_ops={ops.shape[0]}")
    for op in ops.numpy():
        peer, s_off, d_off, s_pitch, d_pitch, width, rows = op[:7]
        print(f"  peer={peer} src=({s_off},{s_pitch}) dst=({d_off},{d_pitch}) "
              f"width={width} rows={rows} strided_write={d_pitch > width}")
```

3. **需要观察的现象**：每个 rank 恰有 2 条 op（发往 p=0 与 p=1；p=2 被 `s_p == 0` 跳过）；所有 op 满足 `src_pitch == width`（连续读）且 `d_pitch == 4 > width`（跨步写）；rank 1 的 `dst_offset` 全部是 2（\(H_1 = 2\)），rank 2 的全部是 3（\(H_2 = 3\)）。
4. **预期结果**：rank 0 的 p=1 行应为 `(1, 4, 0, 2, 4, 2, 1, 1, 0, 0)`——前 7 个字段与 4.2.2 手算表一致，slice 字段同 4.1.4 的说明（b=1 未融合，保持默认 0）；三个 rank 的 `window` 都打印 8（见 4.3）。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：如果强行把 mode 1 的 `dst_pitch` 改成 `width`（连续写），会发生什么？
答：各 sender 的字节会在窗口里按到达顺序首尾相接，互相覆盖或堆叠成「按发送者分段」的布局；此时窗口不再等于输出张量的布局，必须再跑一个本地交织内核才能得到 `[b, s_p, N, d]`——零拷贝路径消失，copy_out 也从一次平坦拷贝变成一次带重排的拷贝。这正是注释里「no local pass to interleave them」所排除的方案。

**练习 2**：mode 1 下 rank 2（\(n_2 = 1\)，\(s_2 = 0\)）的 ops 列表和输出形状各是什么？
答：ops 是 2 条（向 p=0 发 2 行、向 p=1 发 1 行，各携 1 个头列，`dst_offset = H_2·D = 3`）；输出形状 `[1, 0, 4, 1]`——它自己拥有的序列长度为 0，所以收不到任何东西。注意「\(s_p = 0\) 时对方不收」与「\(s_r = 0\) 时我不发」是同一张表在两个 mode 里的两次使用。

**练习 3**：mode 0 的 `src` 公式与 mode 1 的 `dst` 公式是什么关系？
答：完全同构——把 4.1.2 的源侧三条（`width = n_p·D`、`src_offset = H_p·D`、`src_pitch = N·D`）中的 \(p\) 换成「发送者视角的我」即得 4.2.2 的目的侧三条。整对分支构成「读跨步写连续 ⇄ 读连续写跨步」的镜像，这就是「mode 1 是 mode 0 的逆变换」在拷贝发射层面的实现方式。

### 4.3 窗口尺寸取最大：所有 rank 同尺寸的偏移对齐论证

#### 4.3.1 概念说明

`window_numel` 是本 rank 要申请的对称内存窗口**容量（元素数）**。头文件注释（[include/fast_ulysses/a2a_plan.hpp:L53-L58](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/a2a_plan.hpp#L53-L58)）给出了三条理由：

> Elements the symmetric window must hold: the LARGEST rank's output, not this rank's. The peer offsets only line up while every rank allocates the same size, and each rank can compute the max without communicating, because the splits describe the whole group.

拆开读：

1. **容量 = 全组最大输出，不是我的输出**。4.1/4.2 的 dst 偏移都是按「接收方 p 的窗口就是 p 的输出布局」雕刻的，发送方一厢情愿地往里写；窗口必须至少装下每个 rank 的输出，取最大是全组统一的安全尺寸。
2. **同尺寸是偏移对齐的前提，也是集体分配的前提**。窗口的分配是集合操作（对称内存 rendezvous，u4-l1 将精读）：若各 rank 按各自输出大小申请，同一时刻不同 rank 报出的尺寸不同，rendezvous 失配——按 u2-l2 的 SPMD 契约，后果是**挂起而非报错**。
3. **免通信**。因为两张 splits 表描述的是全组（每 rank 各持一份全表，u2-l3），每个 rank 对「最大」算出确定性相同的结果，无需一次 all_reduce。

#### 4.3.2 核心流程

窗口循环（[src/a2a_plan.cc:L166-L173](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc#L166-L173)）对每个 p 计算「p 将收到的元素数」再取最大：

\[ \text{window\_numel} = \max_p \; b \cdot \text{rows}_p \cdot \text{heads}_p \cdot d \]

其中两个因子按 mode 取值：

| mode | \(\text{rows}_p\) | \(\text{heads}_p\) | p 的接收量 | 由谁最大化 |
| --- | --- | --- | --- | --- |
| 0 | `seq_total`（人人收完整序列） | \(n_p\) | \(b \cdot \text{seq\_total} \cdot n_p \cdot d\) | **头最多**的 rank |
| 1 | \(s_p\) | `head_total`（人人收全部头） | \(b \cdot s_p \cdot N \cdot d\) | **序列最长**的 rank |

两种 mode 下这个量都恰好等于「p 的输出元素数」，所以一句话概括：**window_numel = 全组最大输出的元素数**。不均匀分片时它通常**大于**本 rank 的输出——多出来的部分是浪费的显存，换来的是全组统一的偏移假设。

**手算贯穿例子**：

- mode 0：三个 rank 的输出分别是 `[1,3,2,1]`=6、`[1,3,1,1]`=3、`[1,3,1,1]`=3 → `window_numel = 6`。注意 rank 2 没有序列分片，照样收一份完整序列（3 行 × 1 头）。
- mode 1：输出分别是 `[1,2,4,1]`=8、`[1,1,4,1]`=4、`[1,0,4,1]`=0 → `window_numel = 8`。**rank 2 一个元素都收不到，却要申请 8 个元素的窗口**——这是「全组同尺寸」的代价最极端的展示。

#### 4.3.3 源码精读

```cpp
// Window size: the largest receive over all ranks (see A2APlan::window_numel).
for (int p = 0; p < dims.world_size; ++p) {
    const int64_t rows       = (mode == kScatterHead) ? seq_total : dims.seq_splits[p];
    const int64_t heads      = (mode == kScatterHead) ? dims.head_splits[p] : head_total;
    const int64_t peer_numel = checked_mul(
        checked_mul(checked_mul(dims.b, rows, "window size"), heads, "window size"), dims.d, "window size");
    plan.window_numel = std::max(plan.window_numel, peer_numel);
}
```

位于 [src/a2a_plan.cc:L166-L173](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc#L166-L173)。三个要点：

- 循环覆盖**所有** p（包括自己），与发射循环的口径一致；`checked_mul` 三连乘是 u3-l1 讲过的溢出防护——注释 [src/a2a_plan.cc:L17-L18](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc#L17-L18) 解释过：peer 的表项没有上层交叉校验兜底，乘法回绕会产生**负的** window_numel，而下游所有「尺寸检查」会把负数读成「足够小」。
- 这段循环在 `output_shape` 之后、发射循环之前——容量先于内容确定，因为 `A2APlan` 的读者（group 层的窗口缓存，u4-l2）先看容量再取 ops。
- 测试侧的对应断言在 [test/test_plan.py:L145-L147](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_plan.py#L145-L147)：所有 rank 的 `window_numel` 必须相同（`len(windows) == 1`），且等于各 rank 输出元素数的最大值。偶均分时甚至形状都相同（[test/test_plan.py:L154-L158](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_plan.py#L154-L158) 的 `test_even` 额外断言）。

#### 4.3.4 代码实践

1. **实践目标**：验证「全组同尺寸 = 最大输出」在两种 mode 下都成立，并亲手算对两个数字。
2. **操作步骤**：在 4.1.4 / 4.2.4 的脚本外面包一层循环，对 `mode in (0, 1)`、`rank in range(3)` 收集 `window_numel` 与输出元素数：

```python
for mode in (0, 1):
    windows, outs = set(), []
    for rank in range(3):
        shape, window, _ = torch.ops.fast_ulysses.a2a_plan_debug(
            1, 1, rank, 3, [2, 1, 0], [2, 1, 1], mode, 1)
        windows.add(int(window)); outs.append(int(torch.tensor(shape).prod()))
    assert len(windows) == 1 and windows.pop() == max(outs)
    print(f"mode {mode}: window == max output == {max(outs)}")
```

3. **需要观察的现象**：mode 0 打印 6，mode 1 打印 8；mode 1 下 rank 2 的输出元素数是 0，但 `window_numel` 仍参与全组统一值。
4. **预期结果**：两组断言都通过；与 4.3.2 手算一致。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：偶均分（所有 \(s_p\) 相等、所有 \(n_p\) 相等）时，window_numel 与本 rank 输出元素数是什么关系？
答：相等——每个 rank 的输出都一样大，最大值等于任何一份。此时「浪费」为零，这也是 `test_even` 能额外断言所有 rank 输出**形状**相同的原因。

**练习 2**：为什么每个 rank 不需要一次 all_reduce 就能拿到同一个最大值？
答：`build_plan` 的输入 `A2ADims` 含全组两张 splits 表（每 rank 一份完整拷贝），最大值是这张共享数据的确定性函数；相同的输入在所有 rank 上算出相同结果。这也是 u2-l3「每 rank 持有整张表」设计的又一收益。

**练习 3**：假设把窗口循环改成 `plan.window_numel = 本 rank 的输出元素数`，运行时最先在哪一步暴露问题？
答：不在 `build_plan`（它不分配内存），而在 group 层的集合窗口分配：不同 rank 报出不同尺寸，对称内存 rendezvous 失配，表现为部分 rank 无限等待（挂起而非报错）。若尺寸碰巧都够大且一致，问题会推迟到某个 op 写越界为止。这正是窗口循环注释「peer offsets only line up while every rank allocates the same size」防住的场景。

### 4.4 自份额走窗口：让 copy_out 成为一次平坦拷贝

#### 4.4.1 概念说明

回看发射循环（[src/a2a_plan.cc:L175-L176](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc#L175-L176)）：`for (int p = 0; p < dims.world_size; ++p)` **没有任何 `p == rank` 的特判**。当 \(p = r\) 时，mode 0 分支生成的是「把我的输入分片（我的序列行 × 我自己的头列）搬到我自己窗口里它在输出中的最终位置」的同设备拷贝。也就是说：**我自己那份产出也和所有 peer 的产出一样，先落进窗口**。

为什么这么做？因为窗口的稠密前缀（从基址起、按输出布局排好的 `output_shape` 个元素）必须**完整**，两条下游路径才都成立：

- **零拷贝路径**：`out=` 来自 `empty_output()` 时，返回给调用者的张量就是窗口本身的视图（u4-l3 将精读）——peers 直写窗口，没有任何 copy-out。若自己的份额不在窗口里，这个视图就缺一块。
- **copying 路径**：普通调用要窗口 → 输出张量搬一次。因为「每个 rank 的结果从窗口基址起稠密排列」，这次搬运是**一条** `cudaMemcpyAsync`，见 [src/bindings.cc:L213-L223](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L213-L223)：

```cpp
// Window -> the caller's tensor, ordered after the closing barrier on the same stream. Every
// rank's result is dense from the window base, so this is one flat device-to-device copy -- this
// rank's own share included, since it travels through the window like every peer's.
void copy_out(const Call& call, cudaStream_t stream, int rank)
{
    ULYSSES_CUDA_CHECK(cudaMemcpyAsync(call.output.data_ptr(),
                                       reinterpret_cast<const void*>(call.win->peer_ptrs[rank]),
                                       static_cast<size_t>(call.output.nbytes()),
                                       cudaMemcpyDeviceToDevice,
                                       stream));
}
```

这段代码只有一个起点（`win->peer_ptrs[rank]`，即**自己的**窗口基址）、一个长度（`output.nbytes()`）、无任何 pitch——平坦拷贝能成立，前提正是「我的份额也在窗口里、且与 peers 的份额拼成稠密前缀」。

#### 4.4.2 核心流程

把一次 mode 0 调用的数据流画出来（mode 1 同理）：

```text
输入 x [b, s_me, N, d]                 我的窗口 [b, seq_total, n_me, d]        输出 y
┌──────────────────────┐    发往每个 p（含 p == me！）   ┌─────────────────────┐
│ 我的序列行 × 全部头   │ ───────────────────────────▶  │ 全局行 S_me 起的      │ ──copy_out──▶ y
└──────────────────────┘   头列 n_p 发往 p 的窗口       │ s_me 行 × 我的头列    │   （一条平坦 D2D 拷贝，
                             p == me 的那条是同设备拷贝    │                     │    或零拷贝路径直接返回视图）
                                                         │ 其余行来自别的 rank   │
                                                         └─────────────────────┘
```

自份额 op 的字段与任何 peer 无异，例如贯穿例子 mode 0 rank 1 的 p=1 行：`src_offset = H_1·D = 2`（我的头列）、`dst_offset = S_1·row = 2`（我的窗口里我的全局行 2）、width=1、rows=1——同一块数据「从我布局搬到我输出的最终布局」。GPU 上这条 `peer == rank` 的拷贝走调用者流（同设备拷贝不占拷贝引擎的跨设备路径），流与事件的汇合细节由 u5-l1 精读，此处不展开。

设计取舍的一句话总结：**多付一次同设备小拷贝，买下「窗口前缀 ≡ 完整输出」这条不变量**；它同时供养零拷贝与平坦 copy_out 两条路径，也让本讲的重放测试无需为「自己的份额」开特例（[test/test_plan.py:L9-L11](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_plan.py#L9-L11) 的模块注释原话：'every rank sends its own share through it like any other peer's'）。

#### 4.4.3 源码精读

- 发射循环无特判：[src/a2a_plan.cc:L175-L176](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/a2a_plan.cc#L175-L176)。p 取到 `dims.rank` 时两个分支的公式都自然退化成「自搬运」——不需要新代码路径，这是该设计在实现上的优雅之处。
- 设计契约注释：[include/fast_ulysses/a2a_plan.hpp:L62-L65](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/a2a_plan.hpp#L62-L65)（4.2.3 已全文引用），`this rank's own share travels through the window like every peer's` 即本模块的标题出处。
- 收益兑现处：`copy_out` 的单条平坦 `cudaMemcpyAsync`，[src/bindings.cc:L216-L223](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L216-L223)，及其上方注释 [src/bindings.cc:L213-L215](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L213-L215)。
- 测试语义对齐：`run_plan` 给每个模拟 rank 一个「自己输出大小」的窗口并按 `op[0]` 写入（[test/test_plan.py:L62-L69](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_plan.py#L62-L69)），p == rank 的 op 在重放里就是「写自己的缓冲」，与设备行为同构。

#### 4.4.4 代码实践

1. **实践目标**：在 numpy 重放中「删掉」自份额 op，亲眼看到输出缺了哪一块——证明它不是可有可无的冗余。
2. **操作步骤**：先做综合实践（第 5 节）拿到可运行的重放脚本，然后在 `run_plan` 里加一个过滤：

```python
def run_plan_skip_self(inputs, plans):
    windows = [np.zeros(p["output_bytes"], dtype=np.uint8) for p in plans]
    for r, plan in enumerate(plans):
        src = inputs[r].reshape(-1).view(np.uint8)
        for op in plan["ops"]:
            if op[0] == r:          # 跳过 p == rank 的自份额拷贝
                continue
            apply_ops([op], src, windows[op[0]])
    return windows
```

用 mode 0 的 plans 跑 `run_plan_skip_self`，把每个 rank 的窗口 view 成输出形状。

3. **需要观察的现象**：每个 rank 的结果中，「本应来自自己的那段」是零（numpy `zeros` 初始化的原始值），其余段落完好。贯穿例子里 rank 0 的输出 `[1, 3, 2, 1]` 的全局行 0..2 中来自 rank 0 自己的那两行（前 2×2 字节）应为 0，行 2（来自 rank 1）正确。
4. **预期结果**：与参照（不过滤时的结果）恰好差「自己的份额」那一块；这演示了零拷贝路径若没有自份额 op，返回的窗口视图将带着一块未初始化的洞。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：mode 0 的自份额 op 把数据从什么布局搬成什么布局？
答：从我的输入布局（我的序列行 × 我的头列 \(n_r\)，行距 \(N \cdot D\)）搬到我窗口中它在输出布局里的最终位置（全局行 \(S_r\) 起、我的头列 \(H_r\) 起，行距 \(n_r \cdot D\)）。字节数相同，排列不同——一条拷贝完成一次局部重排。

**练习 2**：自份额 op 在 GPU 上由谁执行、走哪条流？为什么它不与 peer 拷贝抢拷贝引擎？
答：它是一条同设备（device-to-device）拷贝，走**调用者流**；peer 拷贝才走独立的传输流（xfer 流）。流的选择与事件汇合由 `launch_a2a_ce` 决定，u5-l1 精读；本讲只需记住「`peer == rank` 在 plan 层不特判，在传输层才分流」。

**练习 3**：如果没有自份额 op，copy_out 需要变成什么样？
答：至少两次拷贝：窗口里别人的部分（在输出布局中是「挖了洞」的）加上输入里自己的部分，或者一次带重排的拷贝；零拷贝路径则直接不可用。「一条平坦 `cudaMemcpyAsync`」这一性质完全建立在自份额走窗口之上。

## 5. 综合实践：不均匀分片的 mode 0 → mode 1 往返重放

本实践把四个模块串起来：用**同一组真正不均匀的 splits**（含一个零分片）分别生成 mode 0 与 mode 1 的 plan，按 `CopyOp` 在 numpy 字节缓冲上重放（完全参照 [test/test_plan.py:L40-L59](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_plan.py#L40-L59) 的 `apply_ops` 与 [L62-L69](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_plan.py#L62-L69) 的 `run_plan`），验证往返后数据逐字节还原——这正是 [test_round_trip](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_plan.py#L236-L257) 的语义，也是两次 all-to-all 包夹注意力（u1-l2）在布局层面的正确性根基。

**实践目标**：一个脚本同时证明 (a) 两个 mode 的发射公式互为逆排列；(b) 窗口取最大在两种 mode 下都成立；(c) 两个 plan 用同一对 splits 而不交换。

**操作步骤**：将下面的脚本保存为独立文件（例如 `roundtrip_plan.py`，放在仓库外的任意目录均可），运行 `python roundtrip_plan.py`。前提：已按 u1-l3 构建扩展；无需 GPU、无需进程组。

```python
# roundtrip_plan.py —— 无 GPU 验证 build_plan 两种模式的发射逻辑（示例代码，供学习用）
import numpy as np
import torch  # import 即注册 torch.ops.fast_ulysses

SCATTER, GATHER = 0, 1

def build_plan(b, d, rank, seq_splits, head_splits, mode, elem_size):
    shape, window_numel, ops = torch.ops.fast_ulysses.a2a_plan_debug(
        b, d, rank, len(seq_splits), list(seq_splits), list(head_splits),
        mode, elem_size)
    shape = list(shape)
    return {"shape": shape,
            "output_bytes": int(np.prod(shape)) * elem_size,
            "window_numel": int(window_numel),
            "ops": ops.numpy()}

def apply_ops(ops, src, dst):          # 与 test/test_plan.py 的 apply_ops 相同
    for (_peer, s_off, d_off, s_pitch, d_pitch,
         width, rows, depth, s_slice, d_slice) in ops:
        for z in range(depth):
            for row in range(rows):
                s = s_off + z * s_slice + row * s_pitch
                t = d_off + z * d_slice + row * d_pitch
                dst[t : t + width] = src[s : s + width]

def run_plan(inputs, plans):           # rank r 的 op 写进 op[0] 号 rank 的窗口
    windows = [np.zeros(p["output_bytes"], dtype=np.uint8) for p in plans]
    for r, plan in enumerate(plans):
        src = inputs[r].reshape(-1).view(np.uint8)
        for op in plan["ops"]:
            apply_ops([op], src, windows[op[0]])
    return windows

def make_inputs(b, d, seq_splits, head_splits, mode, dtype=np.int8):
    seq_total, head_total = sum(seq_splits), sum(head_splits)
    inputs, base = [], 0
    for r in range(len(seq_splits)):
        shape = ((b, seq_splits[r], head_total, d) if mode == SCATTER
                 else (b, seq_total, head_splits[r], d))
        n = int(np.prod(shape))
        inputs.append((base + np.arange(n, dtype=np.int64)).astype(dtype).reshape(shape))
        base += n          # 各处取值不同，任何错位字节都会暴露
    return inputs

# ---- 1. 真正不均匀的 splits：序列差 2:1、头列差 2:1、rank 2 没有序列 ----
seq_splits, head_splits = [2, 1, 0], [2, 1, 1]
b, d, elem, dtype, ws = 1, 1, 1, np.int8, 3

# ---- 2. mode 0 重放：结果应等于 reference_scatter_head（u3-l3 精读）----
inputs = make_inputs(b, d, seq_splits, head_splits, SCATTER, dtype)
plans0 = [build_plan(b, d, r, seq_splits, head_splits, SCATTER, elem) for r in range(ws)]
mid_raw = run_plan(inputs, plans0)
mid = [mid_raw[r].view(dtype).reshape(plans0[r]["shape"]) for r in range(ws)]
head_off = np.cumsum([0] + head_splits)                      # 参照：test_plan.py L72-L86
ref = [np.concatenate([x[:, :, head_off[p]:head_off[p+1], :] for x in inputs], axis=1)
       for p in range(ws)]
for r in range(ws):
    np.testing.assert_array_equal(mid[r], ref[r], err_msg=f"mode0 rank {r}")

# ---- 3. 窗口断言：全组同尺寸，且等于最大输出（模块 4.3）----
for plans, tag in ((plans0, "mode0"),):
    assert len({p["window_numel"] for p in plans}) == 1
    assert plans[0]["window_numel"] == max(int(np.prod(p["shape"])) for p in plans)
    print(tag, "window_numel =", plans[0]["window_numel"])   # 预期 6

# ---- 4. mode 1 重放：同一对 splits，不交换 ----
plans1 = [build_plan(b, d, r, seq_splits, head_splits, GATHER, elem) for r in range(ws)]
back_raw = run_plan(mid, plans1)
back = [back_raw[r].view(dtype).reshape(plans1[r]["shape"]) for r in range(ws)]
for r in range(ws):
    np.testing.assert_array_equal(back[r], inputs[r], err_msg=f"round trip rank {r}")
assert len({p["window_numel"] for p in plans1}) == 1
assert plans1[0]["window_numel"] == max(int(np.prod(p["shape"])) for p in plans1)
print("mode1 window_numel =", plans1[0]["window_numel"])     # 预期 8
print("round trip OK: gather(scatter(x)) == x for every rank")
```

**需要观察的现象与预期结果**：

1. mode 0 重放与 `reference_scatter_head` 参照逐元素相等——4.1 的发射公式正确。
2. `mode0 window_numel = 6`、`mode1 window_numel = 8`——4.3 的手算，两种 mode 的窗口分别由「头最多的 rank」和「序列最长的 rank」决定，且 mode 1 下 rank 2 输出为 0 仍占 8 元素窗口。
3. 往返断言全部通过：`gather(scatter(x)) == x` 对每个 rank 逐字节成立——两个分支共用同一对 splits、互为逆排列。
4. 以上数值由本讲公式手算得出，**待本地验证**（脚本不依赖 GPU，任何装好扩展的环境可直接运行）。

**最后回答实践的核心问题：为什么两个 plan 用的是同一对 splits 而不交换？**

`torch.distributed.nn.all_to_all_single` 的 `split_sizes` 描述的是**本次调用的收发量**（我发多少给谁、从谁收多少）；做逆交换时收发角色互换，所以参数必须 swap（u2-l3 对比过）。而本库的 `seq_splits`/`head_splits` 是**组的所有权表**：\(s_p\) 永远表示「全局序列的第 \(S_p..S_p+s_p\) 行归 rank \(p\) 所有」，\(n_p\) 永远表示「全部头中的第 \(H_p..H_p+n_p\) 列归 rank \(p\) 所有」。mode 0 是「把我的行按列的主人分发」，mode 1 是「把我的列按行的主人收回」——两次寻址用的是**同一张所有权分配**，mode 1 只是同一排列的逆，因此两张前缀和表原样复用。这个脚本就是证明：两次 `build_plan` 传入完全相同的 splits，往返精确还原。同一个论证也预告了 u6-l3 的结论——backward（mode 0 的 vjp）就是 mode 1，splits 原样传递、不交换。

## 6. 本讲小结

- **mode 0（scatter head）读跨步、写连续**：`src_pitch = N·D`（我的输入一行跨全部头），`dst_pitch = width = n_p·D`（peer 的窗口里我的序列分片天然连续）；源侧按头寻址（\(H_p\)），目的侧按序列寻址（\(S_r\)）。
- **mode 1（gather head）读连续、写跨步**：完美镜像；写跨步不是缺陷而是选择——「窗口即返回张量」，被散射的头轴在输出每行**内部**，让拷贝引擎直接按最终布局落位，免去任何本地交织内核。
- **window_numel 取全组最大输出**：发送方按「接收方窗口 = 接收方输出布局」一厢情愿地雕刻偏移，全组必须统一尺寸，集合分配（rendezvous）才不挂起；每 rank 持全表所以免通信算出同一个最大值。mode 0 由头最多的 rank 定尺寸，mode 1 由序列最长的 rank 定。
- **自份额也走窗口**：发射循环不特判 `p == rank`，多付一次同设备拷贝，买下「窗口稠密前缀 ≡ 完整输出」这条不变量——零拷贝路径直接返回窗口视图，copying 路径的 `copy_out` 退化为一条平坦 `cudaMemcpyAsync`。
- **零长度分片在两个分支里各有一道守卫**：mode 0 的 `s_me == 0`（我不发）与 mode 1 的 `s_p == 0`（对方不收）；不发照收、不收也照样分配全组统一尺寸的窗口。
- **splits 是所有权表而非收发量**，所以 mode 0 与 mode 1（以及将来的 backward）复用同一对表、不交换——往返重放逐字节还原是这一语义的直接验证。

## 7. 下一步学习建议

- **u3-l3（无 GPU 的布局测试）**：本讲的 `apply_ops`/`run_plan`/参照函数都出自 `test/test_plan.py`，下一讲系统看这张测试网如何覆盖偶均分、不均匀、空分片、全 dtype、批融合与非 2 幂 world_size，以及 `test_fused_ops_survive_cuda_memcpy_3d` 为融合 op 设的「tripwire」。
- **u5-l1（拷贝引擎传输）**：本讲的 `CopyOp` 十字段将在 `launch_a2a_ce` 里逐条变成 `cudaMemcpy2D/3DAsync` 调用，并看到 `peer == rank` 的自份额拷贝如何在流层面与 peer 拷贝分流、由事件汇合。
- **u4-l1 / u4-l3（对称内存窗口）**：`window_numel` 的真实消费者——`allocate()` 如何把它变成对称内存申请，`empty_output()` 如何把窗口视图借给调用者实现零拷贝。
- **u6-l3（自动微分）**：本讲「两个 mode 互为逆排列、splits 不交换」的代数性质，正是 `backward = 1 - mode` 且 saved splits 原样传递的证明基础。
