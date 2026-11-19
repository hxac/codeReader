# lend=True：由调用计数驱动的窗口轮转

## 1. 本讲目标

`out=` 买到了「零拷出」（没有 copy_out 这一步），但代价是调用者要自己管理缓冲：一个并发调用一个 `empty_output()` 缓冲，还得记住「重新发起调用前别再读旧结果」。`lend=True` 把这笔账收回去：缓冲归 group，调用者只管用。本讲讲清这层便利背后的机制与代价。

学完本讲，你应该能够：

1. **复述规则**：「槽由调用计数选择、引用计数只做检查」——每个 dtype 4 个借出窗口按调用序号轮转（\(\text{idx} = \text{calls} \bmod 4\)），而 storage 引用计数只用来在选中后校验「没有存活结果还指着它」。
2. **解释**：引用计数为什么不能反过来当「选择依据」——它是 rank 本地量，两个 rank 可以在什么执行顺序下选中不同窗口、进而挂起；以及为什么「调用计数」「window_numel」这类全 rank 共享量可以。
3. **写出使用边界与违反方式**：至多 4 个 lend 结果同时存活（第 5 个 raise）、各 rank 必须在程序同一点丢弃结果（错位则挂起而非报错）、与 `out=` 互斥（ValueError）、不可微（RuntimeError）——以及每条边界违反后分别发生什么。

本讲是 u4-l2「触发分配的量必须全 rank 相同」这条设计约束最锋利的一次应用：**共享量做选择，局部量做检查**。整个 `lend_window()` 不过 30 行，但每一行都在执行这条纪律。

## 2. 前置知识

### 2.1 零拷贝路径与 out= 三态（u4-l3）

`all_to_all_4d` 的传输永远落在一个对称内存窗口里；`out=` 决定之后发生什么：

- **不传 `out`**：窗口是 group 内部的，传输结束后一次平坦的设备内拷贝（copy_out）把数据搬进调用者的普通张量。
- **传普通张量**：同上，copy_out 目标换成你的张量。
- **传 `empty_output()` 缓冲**：该缓冲本身就是窗口，peers 直写，`copy_out` 整段消失。

`lend=True` 是**第四种入口**：不传 `out`、也不自己分配，group 把自己的窗口「借」给你——peers 直写它，返回结果就是它的视图，同样没有 copy_out。它与 `out=` 的分工（[docs/api.md:L142-L143](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/api.md#L142-L143)）：结果在本步内消费并释放 → `lend`；要跨步保留 → `empty_output()` + `out=`。

### 2.2 单缓冲窗口（u4-l2）

一个窗口同一时刻只装一次调用的结果：下一次使用它的调用会整段覆写。所以两个并发调用共享一个窗口的前提是「有同一条流给它们排序」。sync 调用走调用者流、async 调用走 comm 流，两流互不排序——这是内部窗口按 `(role, dtype)` 分键的原因。

### 2.3 storage 引用计数 use_count

torch 里每个张量通过其 storage 持有底层数据；`tensor.storage().use_count()` 报告当前有几个持有者。u4-l3 的 `prune_owned()` 已经用过它：`use_count() == 1` 意味着「只剩记账的那一份自己」，缓冲可以归还分配器。

```python
# 示例代码：use_count 的直觉（任何有 torch 的机器上可试）
import torch

t = torch.empty(4)
print(t.storage().use_count())   # 1：只有 t 自己
v = t[1:3]                       # 视图，与 t 共享同一个 storage
print(t.storage().use_count())   # 2：t 和 v 各持一份
del v
print(t.storage().use_count())   # 1：视图消失，计数回落
```

本讲的 use_count 检查用的是完全相同的判据：池里的 `Window::tensor` 是一个持有者，调用者手里的结果视图是另一个；`== 1` 即「调用者已放 手」。

### 2.4 SPMD 契约与「共享量 vs 局部量」（u4-l1 / u4-l2 / design.md）

SPMD（单程序多数据）：每个 rank 执行同一份程序，因此**发出的调用序列完全相同**。由此推出一条铁律（[docs/design.md:L71-L77](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/design.md#L71-L77)）：

- **全 rank 共享的量**——形状、dtype、`window_numel`、调用计数——可以用来做**选择**（包括触发集合分配）。
- **rank 本地的量**——引用计数、空闲内存、时间——只能用来做**检查**，绝不能做选择。

原因：对称内存分配（`empty_strided_p2p` + `rendezvous`）是集合操作，每个 rank 必须在各自程序的同一时刻为同一次分配走到 rendezvous；若选择依据是本地量，rank 间一旦分歧，轻则挂起在 rendezvous，重则彼此通过对端地址写错内存。

### 2.5 AsyncCollectiveTensor（会用即可，u7-l1 详讲）

`all_to_all_4d_async` 返回一个包装对象：`.wait()` 返回普通张量，任何 aten 操作也会隐式等待——GPU 侧等 comm 流完成，主机不阻塞。本讲只需要知道 `.wait()` 拿到的就是借出窗口的视图张量。

## 3. 本讲源码地图

| 文件 | 本讲关注点 |
| --- | --- |
| [include/fast_ulysses/group.hpp](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/group.hpp#L179-L189) | `LentPool` 结构、`kLentSlots = 4`、`lent_` 表；`lend_window()` 的契约注释（本讲论证的原文） |
| [src/group.cc](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L269-L302) | `lend_window()` 实现：轮转、use_count 检查、槽内增长；`destroy()` 对 `lent_` 的收尾 |
| [src/bindings.cc](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L140-L149) | `prepare()` 的 lend 分支（结果 = 窗口视图、`out_is_window`）；sync 路径把 lend 固定为 false |
| [python/fast_ulysses/group.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L151-L232) | `all_to_all_4d_async` 的 `lend` 形参、与 `out=` 的互斥检查、结果包装 |
| [docs/design.md](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/design.md#L67-L77) | 「触发分配的量必须是全 rank 共享的」整段论证 |
| [docs/api.md](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/api.md#L126-L149) | `lend=True` 的用户契约文本 |
| [test/distributed/correctness.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py#L221-L249) | 轮转的位级观察：5 次调用 4 个不同地址、第 5 个回到第 1 个 |
| [test/distributed/validation.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/validation.py#L325-L348) | 上限的拒绝测试：4 个存活 + 第 5 次 raise、丢弃后恢复、out= 互斥 |

调用链回顾（u1-l4）：`group.py` → `src/bindings.cc`（prepare → 屏障 → 传输 → 屏障 → copy_out）→ `src/group.cc`（窗口）→ `src/transfer.cu` / `src/barrier.cu`。lend 改写的正是这条链上「输出从哪来」的那一段。

## 4. 核心概念与源码讲解

### 4.1 lend=True：零拷贝路径的第四种入口

#### 4.1.1 概念说明

`lend=True` 是 `all_to_all_4d_async` 的关键字参数：调用者不给 `out`，group 从自己的借出窗口池里挑一个窗口当作本次输出的落点。peers 通过拷贝引擎直写这个窗口，返回结果就是窗口密集前缀的视图，`copy_out` 步骤不存在。

它买到的东西与 `out= empty_output()` 缓冲完全相同（零拷出、peers 直写），差别只在**谁拥有缓冲、谁管理生命周期**：

| | `out=` + `empty_output()` | `lend=True` |
| --- | --- | --- |
| 缓冲归属 | 调用者 | group |
| 并发调用的缓冲数 | 调用者自己保证一调用一缓冲 | group 轮转 4 个窗口，自动错开 |
| 生命周期规则 | 「重新发起用同一缓冲的调用前别读旧结果」 | 「至多 4 个结果存活，且各 rank 同点丢弃」 |
| 适合 | 结果跨步保留 | 结果在发出它的那一步内消费（拆分的 q/k/v 交换） |

注意一个源码事实：**lend 只存在于 async 调用**。sync 的 `all_to_all_4d`、其 out 变体、`_timed` 在绑定层把 lend 写死为 false，只有 staged 算子（async 的内部入口）透传它，Python 门面也只在 `all_to_all_4d_async` 上暴露。动机可从池大小注释读出：lend 服务的场景——一次打包交换拆成 q/k/v 多个**同时在飞**的结果——本身就是异步场景；sync 调用同步完成，单一 `(kSyncWindow, dtype)` 窗口由流排序安全复用（u4-l2），不需要轮转池。

#### 4.1.2 核心流程

一次 `group.all_to_all_4d_async(x, mode=0, lend=True)` 的完整路径：

```text
Python  all_to_all_4d_async(x, lend=True)
  ├─ out= 与 lend 互斥检查（ValueError）          ← 纯本地，最先发生
  ├─ requires_grad 输入拒绝（RuntimeError）
  ├─ 取 caller 流；切到 comm 流
  └─ torch.ops...all_to_all_4d_staged(..., lend)   ← 透传 lend
C++     run_staged
  ├─ check_alive / 设备检查
  ├─ stage(x)：输入拷进暂存缓冲                    ← u7-l2 的主题
  └─ run → prepare
       ├─ plan(...)：寻址计划，得 window_numel      ← u3 的主题
       └─ lend 分支：
            win = lend_window(dtype, window_numel)  ← 本讲核心
            output = win.tensor 的密集前缀视图
            out_is_window = true
     transfer_on_stream
       ├─ fast_barrier（flag_ptrs）
       ├─ launch_a2a_ce（peer_ptrs，peers 直写各 rank 选中的窗口）
       └─ fast_barrier
     run 收尾：out_is_window ⇒ 跳过 copy_out        ← 零拷出的由来
Python  注册完成事件 / record_stream / 包成 AsyncCollectiveTensor
```

窗口在 `ws == 1` 时整个屏障退化为空操作（[src/barrier.cu:L53-L57](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/barrier.cu#L53-L57)），但 lend 的轮转与上限检查都在主机侧的 `prepare` 里，与 world_size 无关——单卡也能完整复现本讲的所有行为。

#### 4.1.3 源码精读

**prepare 的 lend 分支**（[src/bindings.cc:L140-L149](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L140-L149)）——没有 `out` 但调用者接受借出窗口时，向 group 要一个窗口，并让结果直接成为它的视图：

```cpp
else if (lend) {
    // No `out`, but the caller accepts a lent window: the peers write it directly and the
    // result IS that window, so there is no copy-out and the caller owns no buffer. Which
    // window is a rotation the group drives, not a choice this call makes -- see lend_window().
    const Window& lent = group->lend_window(call.x.scalar_type(), call.plan->window_numel);
    call.win           = &lent;
    call.out_is_window = true;
    call.output =
        lent.tensor.narrow(0, 0, c10::multiply_integers(call.plan->output_shape)).view(call.plan->output_shape);
}
```

三个要点：

1. `lend_window` 只拿到 **dtype 和 window_numel** 两个量——都是全 rank 共享的；选择所需的信息里没有任何本地状态。
2. `call.output` 是窗口的 `narrow + view`（与 `make_output()` 的返回完全同构，见 u4-l3），共享同一 storage——这正是后面 use_count 检查能感知「调用者还持有结果」的原因。
3. `out_is_window = true` 使 `run()` 跳过 copy_out（[src/bindings.cc:L225-L241](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L225-L241)）：

```cpp
const Call call = prepare(group, input, mode, seq_splits, head_splits, out, role, lend);
transfer_on_stream(group, call, stream);
if (!call.out_is_window) {
    copy_out(call, stream, static_cast<int>(group->rank()));   // lend 路径永远不进来
}
return call.output;
```

**Python 侧的互斥检查**（[python/fast_ulysses/group.py:L180-L187](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L180-L187)）——`out=` 与 `lend=True` 都是零拷贝入口，静默偏袒其中一个会让另一个悄悄失效，所以显式拒绝：

```python
if out is not None and lend:
    raise ValueError(
        "all_to_all_4d_async takes out= or lend=True, not both: each is a way of getting "
        "the zero-copy path, one with a buffer you own and one with a window the group "
        "lends. Drop whichever you did not mean."
    )
```

**sync 路径进不来**：三个同步入口在绑定层写死 `/*lend=*/false`——`all_to_all_4d`（[src/bindings.cc:L269](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L269)）、`all_to_all_4d_out`（[src/bindings.cc:L329](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L329)）、`all_to_all_4d_timed`（[src/bindings.cc:L632](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L632)）；staged 算子的 schema 才有 lend 形参且默认 false（[src/bindings.cc:L739-L744](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L739-L744)），由 `all_to_all_4d_async` 传入（[python/fast_ulysses/group.py:L205-L214](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L205-L214)）。

#### 4.1.4 代码实践（源码阅读型）

**目标**：把 lend=True 的一次调用在四个文件里的路径亲手标一遍，并回答两个「为什么」。

**步骤**：

1. 打开 [python/fast_ulysses/group.py:L151-L232](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L151-L232)，从签名读到 `AsyncCollectiveTensor(y)`，抄下每一步做了什么。
2. 打开 [src/bindings.cc:L140-L149](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L140-L149) 与 [L364-L396](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L364-L396)，确认 `lend` 如何从 Python 形参流到 `prepare`。
3. 回答：(a) 为什么互斥检查放在函数第一行、抛 ValueError 而不是替调用者选一个？(b) 借出窗口池为什么**不需要** `WindowRole` 进键（对照 u4-l2 的 `(role, dtype)` 二元组键）？

**需要观察的现象 / 预期结果**：(a) 两者都通向零拷贝，选谁都是替调用者做静默决定——错误信息里明说了「Drop whichever you did not mean」；(b) lend 只被 async 一条调用路径（comm 流）使用，不存在第二条流与之争用，单流内部由流顺序排序，dtype 一个键即可。sync/async 的内部窗口才需要 role 分键。

**待本地验证**：无需运行；答案全部可在源码注释中找到。

#### 4.1.5 小练习与答案

**练习 1**：`lend=True` 返回的 `y.wait()` 与窗口是什么关系？

**答案**：`y.wait()` 返回的是借出窗口 tensor 的 `narrow(0, 0, output_numel).view(output_shape)` 视图，与窗口共享 storage；从 peers 落笔到调用者读取，数据一次都没有被搬运过（这正是零拷出）。

**练习 2**：把 `lend=True` 用在 sync 的 `all_to_all_4d` 上会发生什么？

**答案**：什么也不会发生——`all_to_all_4d` 根本没有 `lend` 参数，Python 会以 TypeError 拒绝未知关键字；绑定层三个同步入口也都写死 `/*lend=*/false`。lend 是 async 家族专属。

**练习 3**：一个「步内消费」的典型场景是什么？

**答案**：把一次 q/k/v 打包交换拆成三个张量级 async 调用（u2-l4 讲过等价的打包原理）：每个结果在本步内被注意力计算消费并释放。这正是 `kLentSlots = 4`（3 个 + 1 个在飞）注释所描述的负载。

### 4.2 LentPool 轮转：槽由调用计数选择，kLentSlots = 4

#### 4.2.1 概念说明

group 为**每个 dtype** 维护一个 `LentPool`：一个最多 4 个元素的窗口数组 `slots`，加一个调用计数器 `calls`。每次 lend 调用：

\[
\text{idx} \;=\; \text{calls}{+}{+} \;\bmod\; 4
\]

- **前 4 次**（池未满）：`idx == slots.size()`，`push_back(allocate(numel, dtype))`——边填充边分配。这是个集合分配点，而触发它的 `idx` 是调用计数，全 rank 一致。
- **第 5 次起**：纯轮转，`idx < slots.size()`，复用既有槽。

为什么固定 4 个？头文件的注释原文（[include/fast_ulysses/group.hpp:L179-L183](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/group.hpp#L179-L183)）：「Four is a packed exchange split into q/k/v with one still in flight; a caller holding more lent results than this at once is **refused rather than slowed down**, because every way of slowing it down would be a decision this rank makes alone.」——拆成 q/k/v 的交换 3 个结果再加 1 个在飞的余量；超过它的调用者得到**拒绝**而不是**变慢**，因为一切「变慢」的方案（等槽、拷出、再分配）都是本 rank 独自做的决定。

计数器是 **per-dtype** 的（`lent_` 按 `at::ScalarType` 分键），所以准确说是「该 dtype 的第几次 lend 调用」。SPMD 契约保证全体 rank 的调用序列相同，因此按 dtype 过滤后的子序列也相同——每个 rank 的计数器读数永远一致。

#### 4.2.2 核心流程

`lend_window` 的骨架（去掉检查与增长，见 4.2.3 的完整源码）：

```text
lend_window(dtype, numel):
    pool = lent_[dtype]                 # 每 dtype 一池
    pool.slots.reserve(4)               # 引用稳定性：一次到位，之后 push_back 不再搬移
    idx = pool.calls++ % 4              # 选择：全 rank 相同的量
    if idx == len(pool.slots):          # 池未满（恰在前 4 次发生）
        return slots.append(allocate(numel, dtype))   # 集合分配，触发量 idx 全 rank 一致
    slot = slots[idx]
    检查 slot 的 storage 引用计数 == 1   # 检查：rank 本地的量（4.3 的主题）
    if slot.numel < numel:              # 增长：触发量 numel 全 rank 一致
        slot = allocate(numel, dtype)
    return slot
```

对同一 dtype 的前 8 次 lend 调用，池的行为完全确定：

| 调用序号（第 n 次） | idx = (n−1) mod 4 | 分配前池大小 | 行为 |
| :---: | :---: | :---: | --- |
| 1 | 0 | 0 | 分配窗口 A，入池 |
| 2 | 1 | 1 | 分配窗口 B，入池 |
| 3 | 2 | 2 | 分配窗口 C，入池 |
| 4 | 3 | 3 | 分配窗口 D，入池 |
| 5 | 0 | 4 | 轮转回 A（须无存活结果指向 A） |
| 6 | 1 | 4 | 轮转回 B |
| 7 | 2 | 4 | 轮转回 C |
| 8 | 3 | 4 | 轮转回 D |

这张表就是测试的预言：5 次「用完即弃」的调用应观察到 4 个不同的 `data_ptr`，且第 5 个等于第 1 个。

#### 4.2.3 源码精读

**数据结构**（[include/fast_ulysses/group.hpp:L179-L189](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/group.hpp#L179-L189)）——`kLentSlots` 的动机与「拒绝而非放慢」的理由都写在这段注释里：

```cpp
// Lent output windows, per dtype: a fixed rotation, so the same call picks the same slot on
// every rank. Four is a packed exchange split into q/k/v with one still in flight; a caller
// holding more lent results than this at once is refused rather than slowed down, because
// every way of slowing it down would be a decision this rank makes alone.
static constexpr size_t kLentSlots = 4;

struct LentPool {
    std::vector<Window> slots;  // grown to kLentSlots, then rotated through
    size_t              calls = 0;
};
std::map<at::ScalarType, LentPool> lent_;
```

**实现**（[src/group.cc:L269-L302](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L269-L302)）。逐段看：

```cpp
const Window& UlyssesGroup::lend_window(at::ScalarType dtype, int64_t numel)
{
    check_alive();
    LentPool& pool = lent_[dtype];
    // The reference this returns outlives the call's own frame, so growth must not move the slots.
    // A no-op once the capacity is there, which is after the first kLentSlots calls.
    pool.slots.reserve(kLentSlots);
```

`reserve(4)` 的原因：返回的 `const Window&` 会被存进 `Call.win`，在 `prepare` 返回后仍被 `transfer_on_stream` 使用（读它的 `peer_ptrs` / `flag_ptrs`）；`std::vector` 扩容会搬移元素、使旧引用失效。第一次调用就 reserve 到 4，之后的 `push_back` 永不搬移。

```cpp
    // The counter, never a search for a free slot: see the header for why the choice has to be
    // a quantity every rank shares. The pool fills before it rotates, so this branch runs exactly
    // kLentSlots times per dtype, at the same calls on every rank.
    const size_t idx = pool.calls++ % kLentSlots;
    if (idx == pool.slots.size()) {
        pool.slots.push_back(allocate(numel, dtype));
        return pool.slots.back();
    }
```

核心一行：**`idx = pool.calls++ % kLentSlots`，用计数器，绝不搜索空闲槽**。填充分支恰在每个 dtype 的前 4 次、也就是全体 rank 的同序号调用上触发——`allocate()` 里的 rendezvous 因此永不错位。

```cpp
    Window& slot = pool.slots[idx];
    // ...use_count 检查（4.3 精读）...
    if (slot.numel < numel) {
        slot = Window{};                // free before allocating, so the allocator can reuse the
        slot = allocate(numel, dtype);  // segment -- the order window() takes, for the same reason
    }
    return slot;
}
```

槽内增长与 u4-l2 的 `window()` 同款「先释放再分配」，让对称分配器能复用旧段；触发量是 `numel`（即 plan 的 `window_numel`，全 rank 取最大、处处相同），依旧集合安全。正确性测试特意覆盖了它（[test/distributed/correctness.py:L247-L249](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py#L247-L249)）：同一形状上 mode 1 的输出更大、`window_numel` 不同，是对借出槽触发增长的那次调用。

**轮转的观察式证明**（[test/distributed/correctness.py:L221-L245](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py#L221-L245)）——5 次「用完即弃」的 lend 调用，记录 `data_ptr`：

```python
lent_ptrs = []
for i in range(5):
    y = group.all_to_all_4d_async(x, mode=0, lend=True).wait()
    check.equal(f"lend=True #{i}", y, want)
    lent_ptrs.append(y.data_ptr())
    del y
if len(set(lent_ptrs)) != 4 or lent_ptrs[4] != lent_ptrs[0]:
    ...  # FAIL
```

测试注释点破了一个巧妙之处：**普通分配无法产生这个模式**——caching allocator 会把刚释放的块立刻还给你，一个「从没进窗口池的 lend」会给出 5 个相同地址；只有真正的轮转给出「4 个不同、再回到第 1 个」。断言 `len(set(lent_ptrs)) == 4 and lent_ptrs[4] == lent_ptrs[0]` 因此同时证明了「有 4 个窗口」和「在轮转」两件事。

#### 4.2.4 代码实践

**目标**：亲眼看到（或纸面推出）轮转模式 `[A, B, C, D, A]`。

**步骤**：

1. 有 GPU（≥1 卡）：跑正确性负载并盯住 lend 段输出——
   ```bash
   torchrun --nproc_per_node=<N> test/distributed/correctness.py
   ```
   预期打印 `OK ws=<N> lend=True rotates through 4 windows`（[test/distributed/correctness.py:L244](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py#L244)）。
2. 无 GPU：把 4.2.2 的表格延长到第 12 次调用，写出每次的 idx、命中槽与是否分配；再回答：若第 3 次调用用了**另一个 dtype**，两个池的计数器各是多少？

**需要观察的现象**：步骤 1 中每个 rank 的 `lent_ptrs` 都是 4 个不同地址循环；步骤 2 中第 9–12 次与第 1–4 次一一对应命中同名槽、不再分配；换 dtype 后新池计数器从 0 开始独立计数（旧池计数器不动）。

**预期结果**：轮转周期恒为 4；分配总共只发生 4 次（每 dtype）。**待本地验证**（多卡行为）。

#### 4.2.5 小练习与答案

**练习 1**：第 5 次调用会再进 `push_back` 分支吗？第 100 次呢？

**答案**：都不会。`idx = (n−1) mod 4`，池满后 `idx < slots.size()` 恒成立，填充分支每个 dtype 恰好执行 4 次（第 1–4 次），之后永远走轮转分支。

**练习 2**：为什么「池先填满再轮转」能保证分配点是集合一致的？

**答案**：填充条件 `idx == slots.size()` 只依赖 idx 和池大小，而这两者都由调用计数决定；SPMD 契约保证每个 rank 的 lend 调用子序列（按 dtype）相同 ⇒ 同一序号的调用在所有 rank 上同时判定「需要分配」，`allocate()` 的 rendezvous 不会错位。

**练习 3**：`lent_` 为什么按 dtype、而不按形状分池？

**答案**：与 u4-l2 的 `window()` 同理——形状被**容量匹配**取代：槽按 `window_numel` 增长，装得下就复用。按形状分键会让「同 dtype 不同形状」的调用各占一串窗口，内存停在多份高水位上；容量复用让每 dtype 至多 4 个窗口停在一份高水位。

### 4.3 use_count 检查：引用计数是检查，不是选择

#### 4.3.1 概念说明

轮转回答「这次用**哪个**槽」；引用计数回答「这个槽**能不能**用」：

- 调用者手里的结果是窗口 storage 的视图（4.1.3），持有它 ⇒ `use_count() ≥ 2`（池的 `Window::tensor` 一个 + 调用者的视图一个）；
- 只剩池自己 ⇒ `use_count() == 1`。

于是「至多 4 个 lend 结果同时存活」这条边界的执行方式是：轮转落回一个仍被存活结果指向的槽时，`TORCH_CHECK` 抛 `RuntimeError`——在任何屏障、任何传输之前（u2-l2 的「全部校验先于第一个屏障」纪律）。

**为什么是拒绝而不是软化**？[test/distributed/validation.py:L326-L330](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/validation.py#L326-L330) 的注释列出了所有「软」选项并一一否决：等它空闲、退回拷出路径、再分配一个窗口——**每一种都是从本 rank 的引用计数读出来的决定**，rank 间一旦不一致，就会一边零拷贝一边拷出、一边填窗口 A 一边填窗口 B。抛异常反而是唯一 rank 无关的行为，因为抛的条件（轮转到被占槽）本身由全 rank 一致的轮转决定。

还有一条**不对称性**必须记住：这个检查只能拦住「还持有结果的一侧」，拦不住「先丢弃的一侧」。若 rank 0 早已 `del` 而rank 1 还持着同一轮次的旧结果，下一次调用轮转到该槽时：rank 0 的 `use_count == 1` 顺利通过、走进屏障；rank 1 的 `== 2` 抛异常、不进屏障——rank 0 在标志位上永远自旋。这正是 api.md 开篇「违反契约 = 挂起而非报错」的又一实例，也是「**每个 rank 必须在自己程序的同一点丢弃结果**」这条纪律为什么必须写给调用方、而不能完全由库兜底。

把本讲的边界与违反方式整理成一张表（学习目标三的落点）：

| # | 边界 | 违反方式 | 后果 | 检查位置 |
| :---: | --- | --- | --- | --- |
| 1 | 至多 4 个 lend 结果同时存活 | 存活第 4 个时发第 5 次 | `RuntimeError`（"at most 4 lent results may be alive at once"），先于第一个屏障 | [src/group.cc:L289-L296](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L289-L296) |
| 2 | 各 rank 在程序同一点丢弃结果 | rank 间丢弃时机错位 | **挂起而非报错**：先丢弃侧进屏障空等 | 无法由库单侧检查——契约（[docs/api.md:L137-L140](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/api.md#L137-L140)） |
| 3 | 与 `out=` 互斥 | 同时传 `out=` 与 `lend=True` | `ValueError`（"not both"） | [python/fast_ulysses/group.py:L182-L187](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L182-L187) |
| 4 | 不可微（async 家族共有） | grad 开启下传 `requires_grad` 输入 | `RuntimeError`（梯度会被静默丢弃，故拒绝） | [python/fast_ulysses/group.py:L192-L197](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L192-L197) |
| 5 | 结果在发步内消费（设计意图） | 跨步保留 lend 结果 | 不 raise；但你在用错工具——正确工具是 `empty_output()` + `out=` | 建议而非强制（[docs/api.md:L142-L143](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/api.md#L142-L143)） |

#### 4.3.2 核心流程

```text
时刻 T：rank 上 4 个存活结果（各槽 use_count = 2，池计数 calls = c）
   ↓ 第 5 次 lend 调用（idx = c mod 4 命中被占槽）
   ↓ TORCH_CHECK(use_count == 1) 失败
   ↓ 抛 RuntimeError —— 此时：没进过任何屏障、没发过任何拷贝
   ↓ group 状态未受污染：del 旧结果后，下一次调用照常轮转
```

#### 4.3.3 源码精读

**检查本身**（[src/group.cc:L286-L296](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L286-L296)）——判据与 `prune_owned()`（[src/group.cc:L198-L205](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L198-L205)）完全同款，`use_count() == 1` 即「只剩记账者自己」：

```cpp
Window& slot = pool.slots[idx];
// The rotation says WHICH slot; the reference count says whether the caller is done with it.
// One holder means this pool is the only one left, the same test prune_owned() uses on owned_.
TORCH_CHECK(slot.tensor.storage().use_count() == 1,
            "all_to_all_4d_async(lend=True) has rotated back to a window a live result still "
            "refers to: at most ",
            kLentSlots,
            " lent results may be alive at once. Drop the older ones before issuing more "
            "calls, or pass out= and own the buffer. Every rank has to drop them at the same "
            "point in its own program -- a lent window is symmetric memory, and the rotation "
            "is what keeps every rank on the same one.");
```

这条报错消息本身就是一份浓缩的设计文档：给出上限（`at most 4`）、两条出路（`Drop the older ones` / `pass out=`，分别对应步内消费与跨步保留两种形态）、以及最关键的契约句（`Every rank has to drop them at the same point in its own program`）。注意检查发生在**增长判断之前**——需要换更大的窗口时，先确认没有旧结果还指着旧窗口，旧段才安全释放。

**测试**（[test/distributed/validation.py:L325-L348](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/validation.py#L325-L348)）——held 列表是关键：必须让 4 个结果**同时**存活，检查的才是「存活数上限」而不是「曾经分配数」：

```python
held = [group.all_to_all_4d_async(good, lend=True).wait() for _ in range(4)]
check.raises(
    "a fifth live lent result",
    "lent results may be alive at once",
    lambda: group.all_to_all_4d_async(good, lend=True).wait(),
)
del held
check.accepts(
    "lend=True again once the earlier results are dropped",
    lambda: group.all_to_all_4d_async(good, lend=True).wait(),
)
check.raises(
    "out= together with lend=True",
    "not both",
    lambda: group.all_to_all_4d_async(good, out=group.empty_output(good), lend=True),
)
```

三段各证一事：第 5 个存活结果被拒且消息命中契约句；`del held` 后同一点恢复可用（raise 没有留下脏状态）；out= 与 lend 同传被 Python 层的 ValueError 拒绝。

**收尾**（[src/group.cc:L476-L478](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L476-L478)）——`destroy()` 里借出窗口与 `owned_` 同等待遇：清池只释放没人再引用的窗口，调用者仍持有的结果靠自己的 storage 引用活到底：

```cpp
// Lent windows behave like owned ones here, and like them a result the caller still holds stays
// alive through its own storage reference; dropping the pools releases only the rest.
lent_.clear();
```

#### 4.3.4 代码实践

**目标**：亲手触发一次上限拒绝，并核对消息文本。

**步骤**：

1. 单卡即可复现（检查在纯主机路径、与 world_size 无关）：
   ```bash
   torchrun --nproc_per_node=1 test/distributed/validation.py
   ```
2. 在输出中找到 `OK ws=1 rejects a fifth live lent result`、`OK ws=1 lend=True again once the earlier results are dropped`、`OK ws=1 rejects out= together with lend=True` 三行。
3. 对照 [src/group.cc:L289-L296](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L289-L296) 的 `TORCH_CHECK`，把消息里的 `at most 4` 与 `kLentSlots`、`lent results may be alive at once` 与测试断言片段一一对应。

**需要观察的现象**：三行 OK 依次出现；第 5 次调用的异常在**任何集合操作之前**抛出（validation.py 的 `Rejects.raises` 在每次检查后放了一个 `dist.barrier()`，正是为了暴露「迟到或单侧的拒绝」——见其文件头注释）。

**预期结果**：全部通过；若把 `held` 改成 3 个再发第 5 次，调用会**成功**（轮转命中的是空闲槽）。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：把 `held` 换成只存活 2 个结果，第 3、4、5 次调用各自命运如何？

**答案**：第 3、4 次进入池填充分支（分配新窗口），永远到不了轮转；第 5 次 idx = 0，若那 2 个存活结果恰好占用槽 0 或 1 则 raise，占用槽 2、3（例如先发了 4 次再挑着持有）则成功。**上限拒绝精确等价于「轮转落回被占槽」**，而不是「计数超过 4」。

**练习 2**：如果库改成「第 5 次调用静默退回拷出路径」，会在什么情况下坏掉？

**答案**：退化决策由引用计数驱动：rank A 的 4 个结果恰已释放而 rank B 还持着时，A 拷出、B 零拷贝——两人对「本次落点窗口」的共识被破坏，A 经内部窗口寻址、B 经借出窗口寻址，写错对象、等错标志，挂起或数据错。这正是现行实现「拒绝而非软化」的理由。

**练习 3**：为什么这条 `TORCH_CHECK` 没有像 `make_dims` 的检查那样进 plan 缓存键？

**答案**：它检查的是**张量无关的运行时状态**（storage 引用计数），同一 (shape, mode, dtype, splits) 的两次调用可以一过一不过；而缓存键里的检查「同键即同值」，命中缓存不可能跳过一个本会失败的检查（见 [src/group.cc:L39-L44](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L39-L44) 的注释）。每调用一次就得查一次。

### 4.4 SPMD 一致性论证：引用计数为什么不能当选择依据

#### 4.4.1 概念说明

把 4.1–4.3 串起来的是一条两句话的规则，原文在 [include/fast_ulysses/group.hpp:L96-L106](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/group.hpp#L96-L106) 的函数契约注释里：

> **THE SLOT IS CHOSEN BY A CALL COUNTER, not by looking for one that is free.** Every rank has to land on the same symmetric allocation -- the transfer addresses the peers through the chosen window's `peer_ptrs` and the barrier through its `flag_ptrs`, so ranks that disagree write past each other and wait on flags nobody sets. A rank's own reference counts are not a quantity its peers share; the number of calls it has made is, because every rank issues the same sequence. [...] **Reference counts are the CHECK rather than the choice.**

论证链条：

1. **窗口身份必须全组一致**。一次调用的传输经所选窗口的 `peer_ptrs` 寻址对端内存（u5-l1），两道屏障经它的 `flag_ptrs` 握手（u5-l3；`flag_ptrs` 是该窗口自己 signal pad 的末尾偏移，u4-l1）。rank 间选了不同的窗口 ⇒ 数据写向对端不同的地址、各自在不同的标志位上等待 ⇒ **互写不重叠、互等无人置位** ⇒ 挂起。
2. **选择依据必须全 rank 相同**。可用量：形状、dtype、`window_numel`、（per-dtype 的）调用计数——它们相同的理由都是 SPMD 契约「调用序列相同」。不可用量：引用计数、空闲内存、时间——这些是每个进程自己的状态，**连「两个 rank 的 Python 对象在同一时刻各持几个引用」都不保证相同**（对象生命周期由各自程序、乃至各自的 GC 时机决定）。
3. **结论**：共享量做选择（轮转 idx、增长 numel），局部量做检查（use_count）——检查失败的行为（抛异常）不依赖本地状态的具体值是否与他人一致，因为「轮转落回被占槽」这个条件本身由共享的轮转决定，抛与不抛在每个 rank 上是同步的。

design.md 把这条规则放进更大的图景（[docs/design.md:L71-L77](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/design.md#L71-L77)）：它约束的**每一个按需分配的路径**——`window()` 的容量增长、`lend_window()` 的填充与增长——触发条件全是共享量。lend 只是「这条约束咬得最疼的地方」（原文：`lend=True is where this bites`）。

**一个澄清边界的对照**（为下一单元埋线）：u7-l2 的暂存池 `stage()` **确实**用本地状态选槽——`cudaEventQuery` 扫描找第一个已完成的槽（[src/group.cc:L347-L361](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L347-L361)）。这不违反规则，因为暂存缓冲**不是对称内存**：它没有 `peer_ptrs`，永远只被本 rank 的两条流（caller 与 comm）触碰，选哪个槽对其他 rank 不可见、也无影响。规则从来不是「不许看本地状态」，而是「**不许让本地状态影响任何别人看得见的行为**」——窗口身份别人看得见，暂存槽身份别人看不见。

#### 4.4.2 核心流程（反例推演）

假设把实现改成「扫描 slots，选第一个 `use_count() == 1` 的空闲槽」。构造一个最小分歧场景，两个 rank、4 个槽都分配过、程序几乎相同——只差一行：

```text
初始：两 rank 都完成了 4 次 lend 调用（槽 0..3 = 窗口 A..D），
      且各自持有若干旧结果——SPMD 不约束对象生命周期：

  rank 0 的程序                       rank 1 的程序
  ---------------                     ---------------
  y0 = 结果(槽0)   # 仍存活           （y0 已被某个中间变量覆盖释放）
  （y1 已释放）                        y1 = 结果(槽1)   # 仍存活
  ...                                 ...
  ── 两边同时发出第 5 次 lend=True 调用 ──
  扫描：槽0 被占(use_count=2)          扫描：槽0 空闲(use_count=1)
       → 选中槽 1（窗口B）                  → 选中槽 0（窗口A）

  rank 0 经窗口B的peer_ptrs 把数据写向 rank1 的窗口B 内存，
           在窗口B的flag_ptrs 上等 rank 1 发布；
  rank 1 经窗口A的peer_ptrs 把数据写向 rank 0 的窗口A 内存，
           在窗口A的flag_ptrs 上等 rank 0 发布。
  → 两人等待的标志位谁也不会置位；自旋内核无超时 → 永久挂起。
  （且两边的"结果"分别是窗口A/B的视图——即使字节碰巧一致，读的也是不同内存。）
```

要点拆解：

- **分歧源**：两 rank 的引用计数布局不同（谁还持着哪个旧结果），这是纯本地状态，SPMD 契约管不到。
- **分歧后果有两层**：数据层（`peer_ptrs` 不同 ⇒ 写错对象）与同步层（`flag_ptrs` 不同 ⇒ 等错标志）。**同步层单独就足以挂起**——就算窗口同尺寸使字节布局完全一致，两个 rank 也在不同的 signal pad 上自旋。
- **现行设计为什么免疫**：选择只依赖 `calls`，两边计数器读数永远相同 ⇒ 永远选中同一个窗口分配。use_count 只在选择**之后**被读，且读出的结果只影响「抛不抛」，不影响「选哪个」。

再补上 4.3.1 那个「现行设计也无法自动兜底」的场景，作为纪律存在的理由：

```text
rank 0: del y_old（第 4 步）           rank 1: y_old 仍存活（第 4 步之后才 del）
  ↓ 第 5 次 lend：idx 一致命中槽 k
  ↓ rank 0: use_count==1 → 通过 → 进入 fast_barrier 自旋等待
  ↓ rank 1: use_count==2 → TORCH_CHECK 抛异常 → 永远不进屏障
  → rank 0 挂起。检查拦得住持有者，拦不住先丢弃者。
```

两个反例共享同一个根因：**把 rank 本地的生命周期信息当成了可协同的信号**。

#### 4.4.3 源码精读

**契约注释**（[include/fast_ulysses/group.hpp:L89-L107](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/group.hpp#L89-L107)）——整段论证的原文，也是本模块的「权威表述」：

```cpp
/// @brief A window to LEND for this call's output: the peers write it, the result IS it, and
/// nothing is copied out. COLLECTIVE when it allocates.
///
/// `out=` buys the same saving and hands the caller the lifetime with it -- one buffer per
/// concurrent call, and never read a result after reissuing that buffer's call. Here the group
/// keeps the buffers and rotates through a fixed set of them.
///
/// THE SLOT IS CHOSEN BY A CALL COUNTER, not by looking for one that is free. Every rank has to
/// land on the same symmetric allocation -- the transfer addresses the peers through the chosen
/// window's peer_ptrs and the barrier through its flag_ptrs, so ranks that disagree write past
/// each other and wait on flags nobody sets. A rank's own reference counts are not a quantity
/// its peers share; the number of calls it has made is, because every rank issues the same
/// sequence. Both conditions below that allocate are keyed on that counter or on `numel`, which
/// is likewise the same everywhere.
///
/// Reference counts are the CHECK rather than the choice: the slot the rotation lands on must
/// be one no live result still refers to, and this throws when it is not. Throwing rather than
/// falling back to a copy, since the fallback would be a local decision too.
```

注意最后一句补全了 4.3 的论证：连「退回拷出」这种看似无害的兜底也是本地决策，所以拒绝是唯一选择。

**design.md 的总纲**（[docs/design.md:L71-L77](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/design.md#L71-L77)）：

```markdown
That ordering argument constrains every later path that allocates on demand: **what triggers the
allocation has to be a quantity every rank shares.** A shape, a dtype, a count of calls — all of
those are the same everywhere, because the SPMD contract says the sequence of calls is. A reference
count is not, and neither is anything derived from one. `lend=True` is where this bites: the group
lends one of four windows per dtype, and it picks by counting calls rather than by looking for a
window nobody holds, precisely because the second reads local state. Reference counts still appear
there, as the check that the rotation landed on a free window, never as the choice of which one.
```

**用户侧契约文本**（[docs/api.md:L132-L140](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/api.md#L132-L140)）——同一条规则面向调用者的措辞：为什么必须轮转（传输经窗口寻址、屏障经 signal pad）、为什么上限是规则不是提示（"a rule, not a hint"）、以及先丢弃者不会 raise 而会挂起的警告。

#### 4.4.4 代码实践（论证型）

**目标**：把 4.4.2 的反例写成一段能放进代码评审的注释——这是理解本讲的「毕业证」。

**步骤**：

1. 在你的笔记（或第 5 节脚本的头部）写下这段注释，必须包含四个要素：两 rank 的存活结果布局、扫描顺序、各自选中的窗口、挂起发生在数据层还是同步层（答：两层都有，同步层单独充分）。
2. 再回答一个变体：如果两个 rank 的丢弃只差一行代码（rank 1 晚一行 `del`），现行实现**什么时候不挂起**？
3. 对照 [src/group.cc:L347-L361](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L347-L361) 的暂存池：说出它用 `cudaEventQuery`（本地状态）选槽为什么合法。

**需要观察的现象 / 预期结果**：(2) 的答案——只有当后续 lend 调用的轮转恰好**没有**落回被单侧持有的那个槽时才不挂起（比如错位只差一行，下一次调用的 idx 落在别的槽上，那次调用侥幸成功；但被持有的槽总会在 4 次内被轮转回来，挂起只是被推迟）。(3) 的答案——暂存缓冲不是对称内存、无 `peer_ptrs`，选哪个槽对其他 rank 不可见；「本地状态不得影响别人看得见的行为」未被违反。

**待本地验证**：无需运行；反例本身不可安全实机运行（挂起无超时，需手工清理进程——这正是 u9-l1 讲 torchrun 包装器超时处理的原因）。

#### 4.4.5 小练习与答案

**练习 1**：分别列出三个「可以做选择」的量和三个「只能做检查」的量（本库范围内）。

**答案**：可选择——形状/dtype（plan 缓存键、窗口键）、`window_numel`（窗口与槽的增长）、per-dtype lend 调用计数（轮转 idx）；只可检查——storage 引用计数（`prune_owned`、use_count 检查）、`cudaEventQuery` 的完成状态（暂存池复用）、主机空闲内存。判据：别人看不看得见这个量影响的行为。

**练习 2**：为什么「等一个槽空闲」（wait-for-free）与「找一个空闲槽」一样非法？

**答案**：等哪个槽、等多久都由本地引用计数/事件状态决定；rank A 等槽 3 释放、rank B 等槽 0，即使最终都等到，它们在此期间发起的调用已用不同窗口寻址与握手——分歧形式不同，挂起结局相同。此外「等待」还会把一个通信库的失败模式变成不可复现的死锁。

**练习 3**：把 `lend_window` 的分配触发条件从「idx == slots.size()」改成「idx == slots.size() && 空闲内存充足」，会破坏什么？

**答案**：空闲内存是 rank 本地量：内存充裕的 rank 走分配、紧张的 rank 跳过，同一次调用一边进 rendezvous 一边不进——`allocate()` 的集合 rendezvous 直接挂起。这就是「每一次按需分配的触发量都必须全 rank 共同可计算」的含义。

## 5. 综合实践

把本讲全部内容装进一个可运行的脚本：轮转观察 + 上限拒绝 + 恢复 + 互斥检查，最后把 4.4 的论证写成文件头注释。

**示例代码**（保存为 `fast-ulysses-tutorial/lend_bound.py`，非项目原有文件）：

```python
# lend_bound.py -- u4-l4 综合实践：lend=True 的轮转与边界
#
# 运行（单卡即可，上限检查在纯主机路径、与 world_size 无关）：
#   torchrun --nproc_per_node=1 fast-ulysses-tutorial/lend_bound.py
#
# ─────────────────────────────────────────────────────────────────────────────
# [论证注释] 如果 lend_window 改成「扫描找第一个 use_count()==1 的空闲槽」：
#
#   前提：4 个槽都已分配（窗口 A..D）。两个 rank 的程序相同，但对象生命周期不受
#   SPMD 约束——rank 0 的 y0（槽 0 的结果）仍存活、y1 已释放；rank 1 恰好相反。
#   两边同时发出下一次 lend=True：
#     rank 0 扫描：槽 0 被占(use_count=2) → 选中槽 1（窗口 B）；
#     rank 1 扫描：槽 0 空闲(use_count=1) → 选中槽 0（窗口 A）。
#   后果有两层：(a) 数据层——rank 0 经窗口 B 的 peer_ptrs 把数据写向 rank 1 的窗口 B
#   内存，rank 1 却把数据写向 rank 0 的窗口 A 内存，互写不重叠；(b) 同步层——两人
#   各自在窗口 B / 窗口 A 的 flag_ptrs（各自的 signal pad 末尾）上等对方发布，等的是
#   谁也不会置位的标志，自旋内核无超时 → 永久挂起。同尺寸窗口使 (a) 可能侥幸对齐，
#   但 (b) 单独就足以挂起。所以选择必须用全 rank 共享的量（调用计数），use_count 只
#   能在选择之后做检查——「共享量做选择，局部量做检查」。
# ─────────────────────────────────────────────────────────────────────────────
from __future__ import annotations

import os

import torch
import torch.distributed as dist

from fast_ulysses import UlyssesGroup


def main() -> None:
    dist.init_process_group("nccl")
    rank, ws = dist.get_rank(), dist.get_world_size()
    torch.cuda.set_device(int(os.environ.get("LOCAL_RANK", rank)))
    dev = torch.device("cuda", torch.cuda.current_device())

    group = UlyssesGroup(require_nvlink=False)
    # n_global = 4*ws 保证 mode 0 的偶均分整除；d=128 的 bf16 满足 16B 对齐。
    x = torch.randn(2, 16, 4 * ws, 128, dtype=torch.bfloat16, device=dev)

    # --- 1. 轮转：5 次「用完即弃」应走 [A, B, C, D, A] ------------------------------
    ptrs = []
    for _ in range(5):
        y = group.all_to_all_4d_async(x, mode=0, lend=True).wait()
        ptrs.append(y.data_ptr())
        del y  # 在下一次调用前丢弃：调用方纪律的一半
    assert len(set(ptrs)) == 4 and ptrs[4] == ptrs[0], f"rotation broken: {ptrs}"
    print(f"rank={rank} lend rotation: {[hex(p) for p in ptrs]}")

    # --- 2. 边界：4 个存活结果 + 第 5 次必须 raise ---------------------------------
    # held 用列表保持 4 个结果同时存活；.wait() 返回的视图持有窗口 storage 的引用，
    # 使每个槽的 use_count == 2。
    held = [group.all_to_all_4d_async(x, mode=0, lend=True).wait() for _ in range(4)]
    try:
        group.all_to_all_4d_async(x, mode=0, lend=True).wait()
    except RuntimeError as exc:
        # 消息来自 src/group.cc 的 TORCH_CHECK：at most 4 lent results may be alive at once
        assert "lent results may be alive at once" in str(exc), str(exc)
        print(f"rank={rank} 5th live result refused OK")
    else:
        raise AssertionError("the 5th live lent result was accepted")
    del held  # 所有 rank 在程序的同一行丢弃：调用方纪律的另一半
    group.all_to_all_4d_async(x, mode=0, lend=True).wait()  # 恢复：raise 没留脏状态
    dist.barrier()

    # --- 3. 互斥：out= 与 lend=True 同传 -------------------------------------------
    try:
        group.all_to_all_4d_async(x, out=group.empty_output(x, mode=0), lend=True)
    except ValueError as exc:
        assert "not both" in str(exc), str(exc)
        print(f"rank={rank} out= + lend=True refused OK")
    else:
        raise AssertionError("out= together with lend=True was accepted")

    group.destroy()
    dist.destroy_process_group()


if __name__ == "__main__":
    main()
```

**操作步骤**：

1. 保存脚本，在仓库根目录运行 `torchrun --nproc_per_node=1 fast-ulysses-tutorial/lend_bound.py`；有多卡可换 `--nproc_per_node=N`。
2. 观察第 1 段打印的 5 个地址；把 `for _ in range(5)` 里的循环体改成不 `del y`（全部存活），重新运行。
3. 对照第 5 次调用的异常消息与 [src/group.cc:L289-L296](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L289-L296) 的 `TORCH_CHECK` 逐句核对。
4. 头部的论证注释对照 [include/fast_ulysses/group.hpp:L96-L106](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/group.hpp#L96-L106) 检查四个要素是否齐全（存活布局 / 扫描顺序 / 各自选中 / 挂起层次）。

**需要观察的现象与预期结果**：

- 第 1 段：5 个地址呈 `[A, B, C, D, A]`（4 个不同值循环）——与 [test/distributed/correctness.py:L236](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py#L236) 的断言一致。
- 第 2 段：第 5 次调用抛 `RuntimeError`，消息含 `lent results may be alive at once`；`del held` 后的下一次调用成功。
- 步骤 2 的变体：不丢弃时**第 5 次循环调用**（原第 1 段的第 5 次）就会 raise——因为槽 0 已被第 1 个仍存活的结果占住。
- 本脚本未在本环境运行过（无 GPU），以上均为依据源码与既有测试的预期，**待本地验证**。

**注意**：断言失败的 rank 会先停下、其他 rank 可能在 `dist.barrier()` 等待，但 torchrun 检测到工作进程退出后会终止整组——这与项目自身测试「打印 FAIL 并继续」的策略不同，教程脚本以快速失败为先。

## 6. 本讲小结

- **lend=True 是零拷贝路径的第四种入口**：不传 `out`、不自备缓冲，peers 直写 group 出借的窗口，结果就是窗口密集前缀的视图，`copy_out` 整段消失；与 `out=` 互斥，async 专属。
- **槽由调用计数选择**：每个 dtype 一个 `LentPool`，\( \text{idx} = \text{calls} \bmod 4 \)；前 4 次边填充边集合分配，之后纯轮转——`kLentSlots = 4` 对应拆成 q/k/v 的交换再加一个在飞。
- **引用计数只做检查**：轮转落回仍被存活结果指向的槽（`storage().use_count() > 1`）时抛 `RuntimeError`，且发生在第一个屏障之前；「至多 4 个存活」因此是规则而非提示，一切软化（等待/拷出/再分配）都是 rank 本地决策。
- **SPMD 一致性是根因**：传输经所选窗口的 `peer_ptrs`、屏障经其 `flag_ptrs`，选错窗口 = 写错地址 + 等错标志 = 挂起；共享量（形状、dtype、window_numel、调用计数）可做选择，局部量（引用计数、事件状态、空闲内存）只能做检查。
- **丢弃纪律归调用方**：use_count 检查拦得住「还持有的一侧」、拦不住「先丢弃的一侧」——各 rank 必须在自己程序的同一行丢弃结果，否则先丢弃侧挂起在屏障上；这是 api.md 集体契约「挂起而非报错」的又一实例。
- **收尾与对照**：`destroy()` 清池时调用者仍持有的结果靠自身 storage 引用活到底；u7-l2 的暂存池用 `cudaEventQuery`（本地状态）选槽并不违反规则——暂存缓冲不是对称内存，选哪个槽别人看不见。

## 7. 下一步学习建议

本讲反复说「选错窗口 ⇒ 写错地址、等错标志」，两半威胁分别在下一单元兑现：

- **u5-l1（拷贝引擎传输）**：看 `launch_a2a_ce` 如何把 plan 的 CopyOp 映射成 `cudaMemcpy2D/3DAsync`，并真正使用 `win->peer_ptrs`——「写错地址」的那一半。
- **u5-l3（标志位屏障）**：看自旋内核如何用 `flag_ptrs` 上的 release/acquire 发布与等待——「等错标志」的那一半，以及为什么握手状态按窗口隔离使「每个窗口一套 flags」成为构造性事实。

之后两条延伸线：

- **u7-l2（暂存池）**：一个刻意用本地状态选槽的设计，与 lend 池互为镜像——对照读完，「共享量做选择、局部量做检查」的边界就立体了。
- **u9-l3（对抗性测试）**：`window_race.py` 等测试如何专门证明「写者不会覆盖读者」——正是本讲 use_count 检查所守护的那条性质。
