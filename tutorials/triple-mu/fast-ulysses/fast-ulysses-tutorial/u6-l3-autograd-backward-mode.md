# 自动微分：backward 是另一个 mode

## 1. 本讲目标

学完本讲，你应该能够：

1. 读懂 `A2AFunction` 这个 C++ `torch::autograd::Function`：它在 `saved_data` 里保存了什么、为什么必须保存这些东西。
2. **从数学上证明**「排列的 vjp 是逆排列」，从而理解为什么 backward 就是 `1 - mode` 的前向调用，且逐字节位精确。
3. 说清 `seq_splits` / `head_splits` 在 backward 中**原样传递而不交换**的道理，并与 `all_to_all_single` 的 backward **交换** split sizes 的语义做对比。
4. 解释 forward AD（jvp）为什么让 tangent 走**第二条集合通信**，以及 reverse-mode functorch（`grad` / `vjp` / `jacrev`）为什么不可用。
5. 写一个验证脚本，把上述结论在真实集群（或单卡退化情形）上逐条跑通，并对照 `examples/dit_attention.py` 的梯度校验。

## 2. 前置知识

本讲默认你已读过 u6-l2（`prepare` → 屏障 → 传输 → 屏障 → `copy_out` 的流水线）。在此之上，还需要几个自动微分的基础概念，用通俗语言过一遍：

- **双射与排列（permutation）**：一个函数把集合中每个元素一一对应地搬到另一个位置、且不增不减，它就是双射；有限集合上的双射就是「重排」。mode 0 的 all-to-all 不做任何计算，只把全局元素集合重排一次——它是一个排列。
- **Jacobian 与 vjp**：对函数 \( f: \mathbb{R}^N \to \mathbb{R}^M \)，Jacobian 是 \( M \times N \) 的偏导矩阵。反向传播需要的不是 Jacobian 本身，而是 **vjp**（vector-Jacobian product）：给定上游梯度 \( \bar{y} \in \mathbb{R}^M \)，算 \( \bar{x} = J^{\mathsf T} \bar{y} \in \mathbb{R}^N \)。`backward(upstream)` 做的就是这件事。
- **排列矩阵**：排列 \( \pi \) 对应的矩阵 \( P_\pi \)，每行每列恰有一个 1。它满足
  \[ P_\pi^{\mathsf T} = P_\pi^{-1} = P_{\pi^{-1}} \]
  ——转置、求逆、取逆排列，三件事是同一件事。这是本讲全部数学的支点。
- **forward AD 与 tangent**：前向模式把导数和数值一起推。输入写成「对偶数」\( x + \dot{x} \)（\( \dot{x} \) 叫 tangent），输出得到 \( y + \dot{y} \)，其中 \( \dot{y} = J\dot{x} \)。torch 里对应 `torch.autograd.forward_ad` 的 dual tensor。
- **`create_graph=True` 与双重 backward**：`torch.autograd.grad(..., create_graph=True)` 让一阶梯度本身带计算图，于是可以对梯度再求梯度（grad-of-grad）。这个开关决定了 backward 内部的调用会不会再建图——第 4.2 节会看到它如何依赖 `A2AFunction::apply` 的递归。
- **torch dispatcher 的 Autograd key**（u6-l1 已建立）：一个 op 注册了 Autograd 内核后，带梯度的调用先进入这个内核，由它负责建图，再「往下」分发到真正干活的内核。

## 3. 本讲源码地图

| 文件 | 在本讲中的角色 |
| --- | --- |
| `src/bindings.cc` | 核心战场：`A2AFunction`（forward/backward）、`dispatch_below_autograd`、`with_reverse_ad`、`all_to_all_4d_autograd`（forward AD），以及 Autograd key 的注册 |
| `python/fast_ulysses/group.py` | Python 门面：`out=` 带梯度输入时的提前拒绝（跨语言两级校验的 Python 侧） |
| `docs/api.md` | 可微性契约的权威表述：backward 是另一 mode、splits 不交换、forward AD 边界、集体契约中关于 backward 顺序的一条 |
| `docs/migration.md` | 从 DeepSpeed `_SeqAllToAll` / yunchang `SeqAllToAll4D` 的 autograd.Function 包装迁移到本库的对照 |
| `examples/dit_attention.py` | 端到端样例：无任何 autograd.Function 包装的可训练注意力块，输出 + 输入梯度双校验 |
| `test/distributed/correctness.py` | 本讲实践的直接参照：backward 位精确断言、skewed splits 断言、forward AD 断言、双重 backward 断言 |

## 4. 核心概念与源码讲解

### 4.1 A2AFunction：把 group、mode、splits 存进图

#### 4.1.1 概念说明

`A2AFunction` 是一个 C++ 的 `torch::autograd::Function<A2AFunction>`——也就是你在 Python 里写 `class MyFn(torch.autograd.Function)` 时的 C++ 对应物。它解决的问题是：`all_to_all_4d` 的正向计算由 `run()`（屏障→传输→屏障→拷出）完成，但要让 torch 的反向引擎知道「梯度怎么穿过这次集合通信」，就必须有一个节点向引擎登记两件事——**正向怎么算**（`forward`）和**梯度怎么回传**（`backward`）。

DeepSpeed 和 yunchang 的做法是在 Python 侧包一层 `torch.autograd.Function`（`_SeqAllToAll` / `SeqAllToAll4D`），backward 里重新调用前向、交换两组索引；本库把这件事做进了 C++ 算子层，**包装被删除而不是被翻译**——这也是 u6-l1 讲过的 schema 拆分的回报：functional 形式（无 `Tensor(a!)` 别名）才允许携带 autograd 公式。

forward 需要为 backward 保存四样上下文：

- **group**：`UlyssesGroup` 的 `intrusive_ptr`（torchbind 自定义类）。真正执行集合通信的有状态对象——窗口、plan 缓存、rank、world_size 都在它身上。backward 时不可能「重建」一个组：那等于再做一次集合性的对称内存分配。
- **mode**：backward 要用 `1 - mode`，必须记住正向是哪个方向。
- **seq_splits / head_splits**：组的切分几何（u2-l3），backward 原样复用。

把 group 存进 `saved_data` 有一个重要副作用：**计算图持有组的引用**。一张在 `destroy()` 之后才重放的图会在 backward 里抛错——文档认为这是正确行为：「there is nothing left to run the collective on」。

#### 4.1.2 核心流程

一次无 `out=`、输入带梯度的调用，从 Python 到设备再回来的完整路径：

```text
Python: group.all_to_all_4d(x, mode=0)              # x.requires_grad，GradMode 开
  └─ dispatcher 命中 Autograd key
       └─ all_to_all_4d_autograd()
            └─ input 无 tangent（不是 dual）？
                 └─ with_reverse_ad()
                      ├─ GradMode 关 或 x 不 requires_grad
                      │    → dispatch_below_autograd()   # 快路径：不建图，直接执行
                      └─ 否则 → A2AFunction::apply(group, x, mode, seq, head)
                           ├─ forward: saved_data 存 group/mode/seq/head
                           │          → dispatch_below_autograd() → 真正的 all_to_all_4d
                           │            （CompositeExplicitAutograd 内核 → run()）
                           └─ (反传时) backward: 读 saved_data → apply(1-mode) → 梯度
```

两个非平凡的设计决定：

1. **`forward` 不直调 `all_to_all_4d()`，而是重新进入 dispatcher**（`dispatch_below_autograd`）。直调会「离开 dispatcher 一去不回」，Autograd 之下的所有 key——Python dispatcher、FakeTensorMode、Meta——都永远不会被咨询，一个 fake tensor 会直接撞进真内核死在 `cudaMemcpy3DAsync`。`AutoDispatchBelowADInplaceOrView` guard 负责挡住 `op.call` 绕回 Autograd 内核：光靠关 GradMode 不够，GradMode 管的是建不建图，不是 dispatcher 选哪个内核。
2. **`with_reverse_ad` 的快路径**。推理（`no_grad`）或输入根本不要梯度时，`apply()` 分配的 autograd 节点是纯粹的死重——而本算子「每层每步跑两次」，所以值得在入口处分流。

#### 4.1.3 源码精读

`A2AFunction` 的 forward，把四个上下文写进 `saved_data` 后经 `dispatch_below_autograd` 执行正向（注意注释里「图持有组存活」那三行）：

- [src/bindings.cc:512-529](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L512-L529) —— `A2AFunction` 类与 `forward`：`ctx->saved_data["group"] = group` 保存 torchbind 类的 `intrusive_ptr`，`mode` 存整数，两个 splits 存成 `IValue` 向量（缺省时存 `IValue()`，即 None，backward 里据此区分「没传」和「传了空」）。

- [src/bindings.cc:478-493](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L478-L493) —— `dispatch_below_autograd`：加 guard 后用 `Dispatcher::findSchemaOrThrow(...).typed<...>()` 拿到 op 句柄再 `op.call(...)`，重新走一遍 Autograd 以下的分发。

- [src/bindings.cc:565-577](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L565-L577) —— `with_reverse_ad`：`!at::GradMode::is_enabled() || !input.requires_grad()` 时走免建图路径；注释点明动机是「节点在没人用它时是死重」。

- [src/bindings.cc:718-726](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L718-L726) —— 注册表：`all_to_all_4d` 的 schema 无别名，`CompositeExplicitAutograd` 挂执行内核、`Autograd` 挂 `all_to_all_4d_autograd`、`Meta` 显式挂形状传播（u6-l4 详解）、`refuse_batching` 双键拒绝 vmap。

#### 4.1.4 代码实践（源码阅读型，无需 GPU）

1. **实践目标**：把 4.1.2 的调用链落在具体文件与函数上，形成一张可反复对照的「autograd 路径图」。
2. **操作步骤**：
   - 打开 `src/bindings.cc`，从 [L721](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L721) 的 Autograd 注册处出发，依次找到 `all_to_all_4d_autograd` → `with_reverse_ad` → `A2AFunction::forward` → `dispatch_below_autograd` → `all_to_all_4d`（CompositeExplicitAutograd）→ `run`。
   - 再看 [python/fast_ulysses/group.py:129-149](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L129-L149)：Python 侧在 `out is None` 时直接转发到 functional op——autograd 路径由此进入。
   - 在每个节点旁标注：它运行在训练图里还是推理里、它建不建 autograd 节点。
3. **需要观察的现象**：从 Python 到 CUDA 拷贝，中间恰好只有一次「建图决策」（`with_reverse_ad` 的分支）和一次「重新分发」（`dispatch_below_autograd`）。
4. **预期结果**：一张与 4.1.2 文本图一一对应的调用链图。本实践为纯阅读，无需运行命令。

#### 4.1.5 小练习与答案

1. **问：为什么 backward 必须从 `saved_data` 里取出 group，而不能在 backward 里新建一个？**
   答：其一，组身上挂着窗口、plan 缓存、rank/world_size——重建组等于重做一次集合性的对称内存 bootstrap 分配；其二，反向传播的执行位置由 autograd 引擎的节点顺序决定而非调用方的程序决定，在 backward 里发起集合分配等于把 rendezvous 的位置交给引擎（源码注释对 `out=` 路径的拒绝正是同一个理由，见 4.2.3）。
2. **问：如果 `forward` 直接调用 `all_to_all_4d()` 函数而不是 `dispatch_below_autograd`，哪类输入会坏、坏在哪？**
   答：fake tensor / Meta 路径。直调离开 dispatcher 后，Autograd 之下的 key 不再被咨询，假张量会一路进到真内核，死在 `cudaMemcpy3DAsync` 里，报出一条与真实病因（不可 trace）无关的错误。
3. **问：`with_reverse_ad` 的快路径在什么条件下生效？为什么值得专门写一个分流函数？**
   答：`GradMode` 关闭（推理或 `no_grad`）或输入 `requires_grad` 为假时生效。值得是因为该算子「每层每步跑两次」，免掉一个永远无人使用的 autograd 节点分配是实打实的开销削减。

### 4.2 backward = 1 − mode：排列的 vjp 是逆排列

#### 4.2.1 概念说明

这是本讲的数学核心。mode 0 不做任何算术，只把全局元素集合重排一遍——它是双射。用排列矩阵写成：

\[ y = P_\pi x \]

其中 \( P_\pi \) 每行每列恰一个 1。对它求 vjp：

\[ \bar{x} = P_\pi^{\mathsf T}\,\bar{y} \]

而排列矩阵的转置就是它的逆：

\[ P_\pi^{\mathsf T} = P_\pi^{-1} = P_{\pi^{-1}} \]

「mode 1 用**同一对** splits」恰好实现 \( P_{\pi^{-1}} \)——u3-l2 已建立：mode 1 的 gather 分支正是 mode 0 的 scatter 分支「从右往左读」。于是：

\[ \bar{x} = \text{mode1}(\bar{y}),\qquad \text{backward} = \text{forward}(1 - \text{mode}) \]

两个直接推论：

- **位精确**。backward 搬的是同样的字节、不做任何重算，所以梯度对照必须用 `equal` 而不是 `allclose`——「close」反而会掩盖一个错误的 plan（测试注释原话）。
- **不产生额外内核**。没有为梯度另写传输代码，反向复用正向的全部机制（窗口、plan、屏障），只是 mode 换了个值。

#### 4.2.2 核心流程

`backward` 被引擎调用时的步骤：

1. 参数 `grads` 是对 **forward 输出**（这里只有一个张量）的梯度。若 `grads[0]` 未定义（本节点的输出没有参与最终图），直接返回 5 个未定义梯度。
2. 从 `saved_data` 恢复 `group`（`toCustomClass<UlyssesGroup>()`）、`mode`（`toInt`）、两个 splits（`isNone()` 判断缺省，再 `toIntVector()`）。
3. 调 `A2AFunction::apply(group, grads[0].contiguous(), 1 - mode, seq, head)`——注意是 **`apply()` 而非裸调用**：`apply` 会正常建图，于是「梯度的梯度」能递归地回到这里（见 4.2.4 的实践三）。
4. 返回值是 **5 个元素的列表，对应 forward 的 5 个非 ctx 参数**（group、input、mode、seq_splits、head_splits），只有 `input` 可微，其余 4 个返回未定义张量。

还有一个容易忽略的契约细节：**backward 永远走 copying 路径，绝不用 `out=` + `empty_output()` 的零拷贝**。理由写在注释里，有两条：`empty_output()` 是集合分配，放进 backward 会把 rendezvous 位置交给引擎的节点顺序；而且梯度属于引擎——它可能被累加、可能被长期持有——而窗口是单缓冲的（u4-l2），两者生命周期模型不兼容。

#### 4.2.3 源码精读

- [src/bindings.cc:495-511](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L495-L511) —— 类前的数学注释，是全段论证的权威表述：mode 0 是全局元素集上的双射，「rank r 的 (batch, s, n, k) 落到 rank owner(n) 的 (batch, seq_offset[r]+s, n−head_offset[p], k)」；mode 1 with the SAME splits 是 scatter 分支从右往左读，即其逆；「The Jacobian of a permutation is a permutation matrix, whose transpose is its inverse, so the vjp is `1 - mode`」。

- [src/bindings.cc:531-560](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L531-L560) —— `backward` 本体。关键三处：
  ```cpp
  if (!grads[0].defined()) {
      return {at::Tensor(), at::Tensor(), at::Tensor(), at::Tensor(), at::Tensor()};
  }
  ```
  输出梯度未定义时短路；随后
  ```cpp
  return {at::Tensor(),
          A2AFunction::apply(group, grads[0].contiguous(), 1 - mode, seq, head),
          at::Tensor(), at::Tensor(), at::Tensor()};
  ```
  只有 `input` 的梯度是实值，方向取 `1 - mode`，splits 原样传入；`.contiguous()` 显式保证上游梯度（可能来自转置等视图操作）是连续的。注释同时说明为何「The copying form, never `out`」。

- [test/distributed/correctness.py:296-308](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py#L296-L308) —— 正确性证据：对 mode 0 和 mode 1 各做一次 `backward(upstream)`，断言 `xg.grad` 与 `all_to_all_4d(upstream, mode=1-mode)` **位精确相等**；测试注释强调「backward moves the same bytes, it does not recompute anything」。

- [docs/api.md:54-58](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/api.md#L54-L58) —— 文档表述：「The vjp of a permutation is the inverse permutation, so the backward is the other `mode` with the **same** splits……Backward always takes the copying path, never `out=`」。

#### 4.2.4 代码实践（推导型，纸面即可完成）

1. **实践目标**：亲手完成「backward = 1 − mode」和「双重 backward」两步推导，为综合实践的脚本提供预期值。
2. **操作步骤**：
   - 设 \( y = P x \)（mode 0），证明 \( \text{sum}(y^2) = \text{sum}(x^2) \)。（提示：排列只是把元素换个位置，平方和不变。）
   - 由此推出：\( \partial\,\text{sum}(y^2)/\partial x = 2x \)（逐元素），再对 \( \text{sum}(2x) \) 求导，得二阶导数。
   - 对照 [test/distributed/correctness.py:348-358](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py#L348-L358) 的断言核对你的答案。
3. **需要观察的现象**：断言名是「double backward of sum(y**2) is 2」。
4. **预期结果**：一阶梯度 \( = 2x \)（作为 \( x \) 的函数，`create_graph=True` 下带图）；二阶梯度 \( = 2 \)（处处常数 2）。`create_graph=True` 之所以能让图延续，是因为 backward 内部走的是 `A2AFunction::apply`——`apply` 在 GradMode 开时照样建节点，于是「backward 里那次 1-mode 集合通信」本身又是一个可微节点，二阶求导再次回到 `backward`。这是递归保证，不是特判。

#### 4.2.5 小练习与答案

1. **问：为什么梯度对照必须位精确（`torch.equal`）而不能 `allclose`？**
   答：backward 复用正向的 CopyOp 重排，不重算任何数值，唯一的失败模式是「搬错了字节」——错了就是全错，没有「差一点」；容差会掩盖错误的寻址。对照：`examples/dit_attention.py` 里整块注意力模块的校验用的是 `allclose`（容差 5e-2），因为 SDPA 归约顺序不同带来的差异是真实的。
2. **问：`backward` 返回 5 个元素，分别对应什么？为什么 4 个是未定义？**
   答：对应 forward 的 5 个非 ctx 参数（group、input、mode、seq_splits、head_splits）。只有 `input` 是张量且可微；group 是自定义类、mode 和两个 splits 是整数——它们没有梯度可言，返回未定义张量占位。
3. **问：上游梯度传入时为什么显式 `.contiguous()`？`prepare` 内部不是已经会做 `input.contiguous()` 吗？**
   答：`prepare` 确实会再做一次（u6-l2 的 `call.x = input.contiguous()`），所以这是显式而非必需——它把「backward 的输入必须连续」这一契约写在调用点上，避免依赖下游实现细节。（说「冗余但无害的防御」是准确表述。）

### 4.3 splits 不交换：组几何 vs 单次收发量

#### 4.3.1 概念说明

「backward 是 `1 - mode` 且 **splits 原样传递**」这句话里，最反直觉的是后半句——因为 `torch.distributed.all_to_all_single` 的 backward **确实交换** split sizes。为什么相反？因为两个 API 的 splits 是**两种不同的对象**：

| | `all_to_all_single` 的 `split_sizes` | 本库的 `seq_splits` / `head_splits` |
| --- | --- | --- |
| 描述什么 | **这一次调用**各 rank 的发送量 / 接收量 | **组本身的几何**：rank p 拥有的序列分片与头分片 |
| 方向反转后 | 收发角色互换 → 数值必须跟着交换 | 「谁拥有什么」与数据往哪流无关 → 原样成立 |
| `mode` 的角色 | 无（方向由收发表隐含） | 只选择哪根轴到达时已分片，不改变两张表的含义 |

u2-l3 与 u3-l1 已建立：每个 rank 持有**完整的**两张表，用排他前缀和免通信地算出全组偏移。正因为它描述的是所有权而非流量，反向传输（mode 1）与梯度传输（backward）复用的是**同一对表**。

一个重要的历史注脚（[docs/migration.md:149-155](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/migration.md#L149-L155)）：DeepSpeed 的 `_SeqAllToAll` 和 yunchang 的 `SeqAllToAll4D` 用「交换 scatter/gather 两个索引」表达 backward。**偶均分时两种约定等价**——这正是旧写法长期没出错的原因；一旦分片不均匀，交换版就错了。这就是 migration 文档那句「Identical while shards are even, not once they are not」。

缺省（不传 splits）的情形也自洽：mode 0 无 splits 从形状反推出 `seq=[s_local]*ws`、`head=[n_global/ws]*ws`；mode 1 在那个输出形状上无 splits 反推出**同一对**——而且它检查整除性的那根轴是「ws 乘以某个数」，所以 backward 永远不会失败一个前向没失败过的检查（注释见 [src/bindings.cc:509-511](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L509-L511)）。

#### 4.3.2 核心流程

把「不交换」放进一次训练迭代的时间线：

```text
前向：  y  = all_to_all_4d(x,  mode=0, seq_splits=S, head_splits=H)   # 集合调用 #1
        ...  注意力（本地计算）...
        z  = all_to_all_4d(y', mode=1, seq_splits=S, head_splits=H)   # 集合调用 #2
反传：  ȳ  经节点 #2 的 backward → apply(mode=0, S, H)                # 集合调用 #3
        ...
        z̄  经节点 #1 的 backward → apply(mode=1, S, H)                # 集合调用 #4
```

四次调用用**同一对** splits；方向全部由 `mode` 一个整数表达。每个 rank 发出的形状序列自动一致——因为每个 rank 从同一对表推出同样的全局布局（u3-l2 的「免通信算出同一最大值」是同一性质在窗口尺寸上的体现）。

#### 4.3.3 源码精读

- [src/bindings.cc:502-507](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L502-L507) —— 「splits 不交换」的论证原文：`all_to_all_single` 的 backward 交换收发量是因为那些数「describe one call's send and receive counts, so reversing the direction reverses their roles」；而本库的表是「a property of the GROUP that holds whichever way the data moves」。

- [src/bindings.cc:541-547](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L541-L547) —— backward 里恢复 splits 的代码：`isNone()` 判断缺省后原样 `toIntVector()`，随后在第 556 行原样传给 `apply`——代码路径上根本**不存在**交换的机会。

- [test/distributed/correctness.py:310-325](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py#L310-L325) —— 防回归断言：用真正不均匀的 `seq_splits = [s_total-(ws-1)] + [1]*(ws-1)`、`head_splits = [4]*ws` 做 backward，断言梯度等于 `all_to_all_4d(up, mode=1, **kw)`。注释点名了这个断言的对手：「This is the check that fails if someone "fixes" the backward by swapping them the way all_to_all_single's backward does」——而且用了 skewed split 之后，「it fails on the shape, not just the values」：交换会让 mode 1 期待的输入形状整个对不上，在 `make_dims` 的交叉校验（u2-l3）处就炸，而不是悄悄算错数。

- [docs/api.md:54-58](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/api.md#L54-L58) —— 文档把两件事写在同一段：vjp 是逆排列（backward = 另一 mode）与 splits 是组几何（不交换），因为它们是同一个论证的两半。

#### 4.3.4 代码实践（预测型，纸面完成，可选手工验证）

1. **实践目标**：用一个小例子看清「交换版 backward」错得多明显。
2. **操作步骤**：
   - 取 ws=2，`seq_splits=[13, 1]`、`head_splits=[4, 4]`，`d=128`、`b=1`。写出 rank 0 的：输入形状（mode 0）、输出形状、上游梯度形状、以及正确 backward 应产生的 `x.grad` 形状。
   - 再假设有人按 `all_to_all_single` 的习惯把 backward 的 splits 交换成 `seq=[4,4]`、`head=[13,1]`：推演 mode 1 期待的输入形状，与真实梯度形状对比。
3. **需要观察的现象**：交换版期待的形状与真实梯度张量的形状完全对不上。
4. **预期结果**：rank 0 输入 `(1, 13, 8, 128)`，mode 0 输出 `(1, 14, 4, 128)`，上游梯度同输出形状，正确 backward 给出 `(1, 13, 8, 128)` 的 `x.grad`。交换版下 mode 1 期待输入 `(1, sum(seq)=8, head_splits[0]=13, 128)`——与 `(1, 14, 4, 128)` 矛盾，`make_dims` 交叉校验抛 RuntimeError。这正是 correctness.py 注释说的「fails on the shape」。（如要在真机复现，可临时改脚本传入交换后的 splits——但不要改库源码。）

#### 4.3.5 小练习与答案

1. **问：DeepSpeed/yunchang 的「交换索引」写法为什么在偶均分下碰巧正确？**
   答：偶均分时 `seq_splits=[s/w]*w`、`head_splits=[n/w]*w`，两张表数值上对称（交换后仍是各自的全 1 常数表乘不同因子），「交换两根轴的切分」与「方向取逆、表不变」落到了同一个重排上；不均分时两种约定给出不同的布局，才分出对错。
2. **问：backward 复用 sync 窗口（u4-l2 的单缓冲）为什么是安全的？**
   答：backward 永走 copying 路径、在调用者流上执行；autograd 引擎按节点顺序在同一条流上发起反向调用，同流调用被流顺序排好，满足 u4-l2「单缓冲窗口只有被同一条流排序才可共享」的条件。这也是「不用 `out=` 零拷贝」之外的第三重保障。
3. **问：不传 splits 时，mode 0 前向推出的一对表与 mode 1 backward 推出的一对一定相同吗？**
   答：一定相同。mode 0 从 `(b, s_local, n_global, d)` 推出 `seq=[s_local]*ws`、`head=[n_global/ws]*ws`；mode 1 的输入形状是 `(b, s_local*ws, n_global/ws, d)`，无 splits 时按被散射轴（序列轴）整除反推，得到的仍是同一对。且它检查整除的轴是前向已检查轴的 ws 倍，不可能新失败。

### 4.4 forward AD 双通道与 functorch 边界

#### 4.4.1 概念说明

torch 的 C++ `torch::autograd::Function` **没有 jvp 钩子**——这决定了 forward AD 不能做进 `A2AFunction`，只能做在 Autograd 内核的门口。若不做任何处理，一个带 tangent 的 dual 输入会有两种坏结局：进 `apply()` 直接 raise，或走 `with_reverse_ad` 的快路径被**静默丢掉 tangent**——后者更糟，因为没有任何报错。

出路来自同一个数学事实：排列的 jvp 还是这个排列，

\[ \dot{y} = P_\pi\,\dot{x} \]

所以 tangent 只需**再走一次同一条集合通信**。这带来一个必须写进使用契约的事实：**一个带 tangent 的调用发出两条集合通信，流量翻倍**；又因为每个 rank 跑同一张图，每个 rank 都会发出第二条——不会有人掉队。

反方向的能力边界（[docs/api.md:64-71](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/api.md#L64-L71)）：

| API | 结果 | 原因 |
| --- | --- | --- |
| `torch.autograd.forward_ad` / `torch.func.jvp` / `torch.func.linearize` | 可用，流量 ×2 | tangent 走第二条集合通信 |
| `torch.func.grad` / `torch.func.vjp` / `torch.func.jacrev` | raise | backward 是 C++ `torch::autograd::Function`，functorch 无法 lift |
| `torch.vmap` / `torch.func.jacfwd` | raise（显式拒绝） | vmap 会按 batch 逐元素循环调用集合通信，rank 间 batch 数不一致即挂起（u6-l4 详解） |
| `torch.autograd.grad(..., is_grads_batched=True)` | raise（显式拒绝） | 经 legacy `Batched` key 走进同一个逐元素循环，被同一拒绝拦下 |

对 batch 的建议是把批维 reshape 进 `b`——集合通信本来就搬运它。

#### 4.4.2 核心流程

`all_to_all_4d_autograd`（注册在 Autograd key 上）的分支逻辑：

```text
tangent = input._fw_grad(kFwLevel)          # kFwLevel = 0：torch 唯一的 forward AD level
├─ tangent 未定义（普通张量）
│    → with_reverse_ad(...)                  # 纯反向路径（4.1 的流程）
└─ tangent 已定义（dual 输入）
     → result    = with_reverse_ad(primal)   # primal 保留反向历史、不携带 tangent
     → result._set_fw_grad(with_reverse_ad(tangent), 0, /*is_inplace_op=*/false)
                                              # tangent 也走一遍同一集合通信
```

细节：primal 与 tangent 都经过 `with_reverse_ad`——如果 tangent 自身 `requires_grad`（对对偶数再求反向导数的场景），它同样会挂上 `A2AFunction` 节点；注释特别强调「The primal keeps the reverse-mode history and carries no tangent, so apply() may see it」——先把 primal 从 dual 里剥出来（`_fw_primal`），`apply()` 才不会因为见到 dual 而 raise。

#### 4.4.3 源码精读

- [src/bindings.cc:584-605](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L584-L605) —— `all_to_all_4d_autograd`：入口先取 `input._fw_grad(kFwLevel)`；注释解释 forward mode 为何在这里处理而不是 `A2AFunction`（无 C++ jvp 钩子、快路径会静默丢 tangent），以及「the jvp of a permutation is the permutation, so the tangent goes through the same call -- one more collective, which every rank issues」。

- [src/bindings.cc:579-580](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L579-L580) —— `kFwLevel = 0`，注释：「The only forward-AD level torch has」。

- [src/bindings.cc:58-67](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L58-L67) —— `refuse_vmap` 的报错文本，末尾点名 `torch.autograd.grad(is_grads_batched=True)` 也会到达这里——batched backward 就是按 batch 逐元素的循环，与本讲 backward 主题的直接交点。

- [test/distributed/correctness.py:330-346](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py#L330-L346) —— forward AD 的断言：`make_dual` 后调用，断言 jvp 等于 `all_to_all_4d(tangent, mode=0)`、primal 不受影响；若 `jvp is None` 则直接记 FAIL——「the tangent was dropped」正是没有这段内核代码时会发生的静默错误。

#### 4.4.4 代码实践（需 ≥1 张 GPU；无 GPU 环境标注：待本地验证）

1. **实践目标**：亲手看到「tangent 走第二条集合通信」与 functorch 的三类 raise。
2. **操作步骤**（接综合实践的进程环境，单卡 ws=1 即可）：

   ```python
   import torch
   import torch.autograd.forward_ad as fwd_ad

   x = torch.randn(1, 16, 4 * ws, 128, dtype=torch.bfloat16, device=dev)
   with fwd_ad.dual_level():
       t = torch.randn_like(x)
       out = group.all_to_all_4d(fwd_ad.make_dual(x, t), mode=0)
       primal, jvp = fwd_ad.unpack_dual(out)
   print("jvp defined:", jvp is not None)
   print("jvp == a2a(t):", torch.equal(jvp, group.all_to_all_4d(t, mode=0)))
   ```

   再依次尝试（每条单独捕获异常、打印消息）：

   ```python
   torch.func.jvp(lambda v: group.all_to_all_4d(v, mode=0), (x,), (t,))   # 预期：正常返回
   torch.func.grad(lambda v: group.all_to_all_4d(v, mode=0).sum())(x)     # 预期：raise
   torch.vmap(lambda v: group.all_to_all_4d(v, mode=0))(x.unsqueeze(0))   # 预期：raise
   ```
3. **需要观察的现象**：`jvp` 非 None 且与直接对 tangent 做一次集合通信位精确相等；`torch.func.grad` 与 `torch.vmap` 抛 RuntimeError，且两者消息**不同**——前者是 functorch 无法 lift C++ Function 的报错，后者是本库 `refuse_vmap` 的主动拒绝（消息里含 "does not support torch.vmap"）。
4. **预期结果**：如上。若无 GPU，本实践的每一条均为待本地验证——但三条 raise 的消息文本可先在 [src/bindings.cc:58-67](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L58-L67) 与 [docs/api.md:64-71](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/api.md#L64-L71) 里预习。

#### 4.4.5 小练习与答案

1. **问：为什么 forward AD 不能像 backward 一样写进 `A2AFunction`，而要放在 Autograd 内核里？**
   答：`torch::autograd::Function` 在 C++ 里没有 jvp 钩子：dual 输入进 `apply()` 会 raise；而如果 dual 恰好走了 `with_reverse_ad` 的快路径（no_grad 下），tangent 会被静默丢弃、没有任何报错。放在内核入口处先拆 dual，两条路径才都受控。
2. **问：`torch.func.jacrev` 与 `torch.func.jacfwd` 都求雅可比，为什么失败原因不同？**
   答：`jacrev` 是 vmap over vjp——走 reverse 路径，撞上「functorch 无法 lift C++ autograd::Function」；`jacfwd` 是 vmap over jvp——jvp 本身可用，但外层的 vmap 触发本库对 batched 输入的显式拒绝（逐元素循环集合通信会挂起）。
3. **问：一个 dual 输入的调用发出两条集合通信，为什么不会破坏「各 rank 形状序列一致」的集体契约？**
   答：因为每个 rank 跑的是同一张图、在同一处代码携带 tangent，于是每个 rank 都在同一程序点发出第二条形状相同的集合通信；契约要求的是「相同形状序列」，而非「相同条数以外的任何局部决定」。

## 5. 综合实践

把本讲四个模块串成一个可运行的验证脚本。**需要 CUDA；两张卡能看到不均匀分片的效果，单卡（`--nproc_per_node=1`）是合法的退化情形（ws=1，splits 各只有一项，逻辑全部照跑）。**

实践目标：一次性验证 (a) mode 0 + mode 1 串联还原（backward 的效果）、(b) backward == forward(1-mode) 且 splits 原样、(c) 双重 backward 经 `apply` 递归成立、(d) forward AD 的双通道。

将以下内容保存为 `practice_grad.py`（放在仓库外或临时目录均可，**不要**放进被分析的仓库源码树）：

```python
"""practice_grad.py — backward 是另一个 mode 的四重验证。

torchrun --nproc_per_node=2 practice_grad.py      # 单卡用 --nproc_per_node=1
"""
import os

import torch
import torch.autograd.forward_ad as fwd_ad
import torch.distributed as dist

from fast_ulysses import UlyssesGroup


def main() -> None:
    dist.init_process_group("nccl")
    rank, ws = dist.get_rank(), dist.get_world_size()
    torch.cuda.set_device(int(os.environ.get("LOCAL_RANK", rank)))
    dev = torch.device("cuda", torch.cuda.current_device())
    group = UlyssesGroup(require_nvlink=False)

    # ---- 1) mode 0 + mode 1 串联还原 == backward 做的事（splits 不交换的直接证据）
    s_total = 12 + ws                                  # ws=2 -> [13, 1]，真正不均匀
    seq_splits = [s_total - (ws - 1)] + [1] * (ws - 1)
    head_splits = [4] * ws
    kw = dict(seq_splits=seq_splits, head_splits=head_splits)

    x = torch.randn(1, seq_splits[rank], sum(head_splits), 128, dtype=torch.bfloat16, device=dev)
    back = group.all_to_all_4d(group.all_to_all_4d(x, mode=0, **kw), mode=1, **kw)
    assert torch.equal(back, x), "mode 0 与 mode 1 不是互逆"
    print(f"rank={rank} [1] round trip bit-exact")

    # ---- 2) backward == forward(mode=1, 同一对 splits)，位精确
    xg = x.detach().clone().requires_grad_(True)
    # 上游梯度取 mode 0 输出的形状；这次额外前向本身也是各 rank 对称发出的集合调用
    up = torch.randn_like(group.all_to_all_4d(xg.detach(), mode=0, **kw))
    group.all_to_all_4d(xg, mode=0, **kw).backward(up)
    want = group.all_to_all_4d(up, mode=1, **kw)       # up 不 requires_grad -> 快路径
    assert xg.grad is not None and torch.equal(xg.grad, want), \
        "backward 不是 1-mode，或 splits 被交换了"
    print(f"rank={rank} [2] backward == forward(mode=1, same splits), bit-exact")

    # ---- 3) 双重 backward：backward 内部走 apply()，图不断
    xd = torch.randn(1, 16, 4 * ws, 128, dtype=torch.bfloat16, device=dev, requires_grad=True)
    y = group.all_to_all_4d(xd, mode=0)                # y = P xd，偶均分
    (first,) = torch.autograd.grad((y ** 2).sum(), xd, create_graph=True)
    assert first.grad_fn is not None, "一阶梯度没有 grad_fn：backward 走了不建图的调用"
    (second,) = torch.autograd.grad(first.sum(), xd)
    # sum(y^2) = sum(x^2)（排列不改平方和）-> 一阶 = 2*xd -> 二阶 = 2
    assert torch.allclose(second.float(), torch.full_like(second.float(), 2.0))
    print(f"rank={rank} [3] double backward == 2.0 everywhere")

    # ---- 4) forward AD：tangent 走第二条集合通信
    with fwd_ad.dual_level():
        t = torch.randn_like(xd.detach())
        out = group.all_to_all_4d(fwd_ad.make_dual(xd.detach(), t), mode=0)
        primal, jvp = fwd_ad.unpack_dual(out)
    assert jvp is not None and torch.equal(jvp, group.all_to_all_4d(t, mode=0)), \
        "tangent 被丢弃，或没走同一条集合通信"
    print(f"rank={rank} [4] jvp == a2a(tangent), bit-exact")

    group.destroy()
    dist.destroy_process_group()


if __name__ == "__main__":
    main()
```

操作步骤与观察点：

1. `torchrun --nproc_per_node=2 practice_grad.py`，观察四行 `[1]`–`[4]` 依次打印——任何一条 assert 失败都精确对应本讲的一个论断被破坏。
2. 把 `kw` 里的 `seq_splits` 临时改成对称的 `[6, 6]` 之类再跑 `[2]`：它仍应通过（偶均分是特例），体会「不均分才区分两种约定」。
3. 跑示例做模块级对照：`torchrun --nproc_per_node=2 examples/dit_attention.py`（[examples/dit_attention.py:96-116](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/examples/dit_attention.py#L96-L116)）。注意两处校验的容差差异：本脚本的集合通信校验全部位精确；示例对整块注意力只有 `allclose`（5e-2），因为 SDPA 在两条路径上归约顺序不同——**交换本身位精确，注意力不是**。
4. 对照 [examples/dit_attention.py:50-63](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/examples/dit_attention.py#L50-L63) 的两次调用注释（「Differentiable; its backward is mode 1」/「Its backward is mode 0」）：示例整个模块没有一行 autograd 代码，梯度仍正确回流——那就是 `A2AFunction` 在起作用。

预期结果：四个断言全部通过；`dit_attention.py` 的 `output max |diff|` 与 `d/dinput max |diff|` 均小于 5e-2 且打印 OK，五个优化步的 loss 有变化。若在无 GPU 环境操作，以上运行结果均为**待本地验证**；脚本逻辑可先对照 [test/distributed/correctness.py:296-358](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py#L296-L358) 逐段核对——它就是这些断言的「正式版」。

## 6. 本讲小结

- `A2AFunction` 在 `saved_data` 里保存 `group`（torchbind 类的 `intrusive_ptr`，图因此持有组存活）、`mode` 和两个 splits；forward 经 `dispatch_below_autograd` 重新进入 dispatcher，保住 Meta/FakeTensor 等下层 key。
- mode 0 是全局元素集上的排列，其 Jacobian 是排列矩阵，转置即逆——所以 vjp 就是 `1 - mode` 的前向调用，位精确、零额外内核。
- splits 描述**组的几何**（rank p 拥有什么），与数据流向无关，backward 原样传递；`all_to_all_single` 的 split sizes 描述**单次收发量**，方向反转必须交换——两种对象、两种约定，偶均分时碰巧等价，不均分时分出对错。
- backward 永走 copying 路径：`empty_output()` 是集合分配，不能交给引擎的节点顺序；梯度属于引擎、可能被累加持有，与单缓冲窗口的生命周期不兼容。
- forward AD 在 Autograd 内核里手工处理：primal 与 tangent 各走一次同一集合通信（jvp of a permutation is the permutation），流量 ×2；`grad`/`vjp`/`jacrev` 因 functorch 无法 lift C++ Function 而 raise，vmap 被显式拒绝。

## 7. 下一步学习建议

- **u6-l4（Meta 内核、FakeTensor 与 vmap 拒绝）**：本讲两次路过 Meta key 与 `refuse_vmap`，下一讲正面拆解形状传播为何复用 `plan()`、哪些 op 拒绝被 trace、以及 vmap 双 key 拒绝的完整论证。
- **u7-l1（异步调用全景）**：`all_to_all_4d_async` 明确**不可微**且对带梯度输入 raise——带着本讲「梯度属于引擎、窗口单缓冲」的结论去读它的拒绝理由，会看到同一组生命周期约束的另一种表达。
- **源码延伸阅读**：`src/a2a_plan.cc` 的 `build_plan`（u3-l2）——把「mode 1 是 mode 0 从右往左读」落到 CopyOp 的发射循环上；以及 `test/distributed/correctness.py` 的其余段落，看位精确对照如何覆盖 out 三态、async 与全部 dtype。
