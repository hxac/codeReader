# torch 对称内存与窗口分配

## 1. 本讲目标

上一单元（u3）我们把 all-to-all 完全表达成了一组「相对基址、无指针」的 `CopyOp`：源/目的偏移、步长、行数都只是字节算术。但拷贝终究要落在**真实地址**上——peer 的窗口在哪里？握手用的标志位放在哪？这些由本讲的 `UlyssesGroup::allocate()` 一次性回答。读完本讲你应该能够：

1. **解释 `empty_strided_p2p` 与 `rendezvous` 各自返回什么、为什么都是集合操作**——一个给出「每个 rank 都有一份」的显存，另一个把 ws 份分配对齐成一张全员地址表。
2. **推导 `flag_offset_slots` 的计算**——为什么本库的握手区 `flags[ws] + epoch` 必须放在 signal pad 的**末尾**而不是开头。
3. **说明 `allocate()` 结尾两行（清零自身握手区、`sym->barrier(0, 0)`）的必要性**——少掉这道 barrier，一个 rank 的首次发布会落进另一个 rank 尚未清零的 pad 里、被清零抹掉，后者就此挂死。
4. **复述「直接走分配入口点而非 `MemPool`」的顺序论证**——这是 design.md 中「触发分配的量必须全 rank 相同」这条约束的源头，也是 u4-l2 / u4-l4 两讲的地基。

本讲的源码阅读**不需要 GPU**；代码实践的主路径（手工分配并打印 peer 地址）需要 ≥2 张卡，同时给出无 GPU 的替代方案（`allocate()` 的逐行注释伪代码）。这与项目自身的分层哲学一致（u1-l4：布局在主机上测试，只有传输才需要卡）。

## 2. 前置知识

### 2.1 对称内存：每个 rank 一份、互相看得见

普通 CUDA 显存分配只知道**自己**的地址；`cudaMemcpy` 到另一张卡要走「设备间拷贝」协议，且你拿不到对端缓冲的稳定地址。**对称内存（symmetric memory）** 解决的正是这个问题：

- 组内每个 rank 各自分配一块**同样大小、同样布局**的显存；
- 通过 CUDA 虚拟内存管理（VMM）与 peer access 映射后，任一 rank 都能拿到「**从我的地址空间看，peer p 的那块在哪个地址**」，然后像访问普通显存一样直接读写对端；
- 因此一次跨卡交换不再需要通信库中转——直接发一条 `cudaMemcpy2DAsync`，目的地址就是对端窗口。

这正对应 u1-l1 的第一条特性：peer 拷贝由**拷贝引擎（CE）**执行，不占 SM。而 GPU 之间要能这样直连，前提是 NVLink（u2-l1 的构造检查）。torch 从 2.10 起在 `torch.distributed._symmetric_memory`（C++ 侧 `c10d::symmetric_memory`）提供了这套机制的完整 API，本库不自己造分配器，全部委托给它（[include/fast_ulysses/group.hpp:L5-L8](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/group.hpp#L5-L8)：窗口就是 torch 对称内存分配，「与 torch 自己的 `symm_mem.empty()` 用的是同一套机制」）。

### 2.2 torch 对称内存 API 三件套

本讲只用到三个入口，全部是 `TORCH_API`、从 torch 2.10 到 2.13 签名未变（[docs/design.md:L50-L53](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/design.md#L50-L53)）：

| API | 做什么 | 返回什么 |
| --- | --- | --- |
| `empty_strided_p2p(size, stride, dtype, device, group_name, mem_pool)` | 在当前 rank 上做一次对称分配 | 一个普通的 `at::Tensor`（本 rank 那一份） |
| `rendezvous(tensor, group_name)` | 全组交换这次分配的句柄 | `SymmetricMemory*`，提供全员地址查询 |
| `enable_symm_mem_for_group(group_name)` | 把进程组注册进对称内存 bootstrap（Python 侧） | 无返回；`rendezvous` 的前提 |

`SymmetricMemory*` 上本讲用到四个方法：`get_buffer_ptrs()`（每个 peer 的数据区地址）、`get_signal_pad_ptrs()`（每个 peer 的信号区地址）、`get_signal_pad(rank, sizes, dtype, offset)`（把某 rank 信号区的一段取成张量视图）、`barrier(chan, op)`（torch 自带的对称内存屏障）。

**signal pad（信号区）** 是关键配角：torch 的每次对称分配除了数据区，还附带一小块标志位区域，设计初衷就是给集合通信库放握手标志——天然对 peers 可见，而且不占用数据区。本库的标志位屏障（u5-l3）就住在这里。

### 2.3 记号与前置结论

设 \(ws\) 为组内 rank 数（本库限制 \(1 \le ws \le 8\)），\(r\) 为本 rank 编号。需要从 u3 带过来的只有一条：

> `allocate(numel, dtype)` 的入参 `numel` 就是 `window_numel`——**所有 rank 上相同的值**（全组各 rank 最大接收元素数，u3-l2）。窗口尺寸全组一致，是 peer 偏移能对齐的前提；这也意味着 `allocate` 在每个 rank 上以**完全相同的参数**执行。

另外回顾 u2-l1：`UlyssesGroup` 构造时 Python 侧已经调用了 `enable_symm_mem_for_group(pg.group_name)`（[python/fast_ulysses/group.py:L80-L89](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L80-L89)），注册是幂等的，且从 torch 2.13 起 `rendezvous` 能自己解析进程组、该调用已标记废弃——「仍然调用它」正是为了兼容 2.10。这一步是本讲 `rendezvous` 能工作的铺垫。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| `src/group.cc` | 有状态层（u1-l4） | `allocate()` 全文（L207-L253）；匿名命名空间里的 `flag_slots` / `flag_offset_slots`（L18-L37）；`epoch()` 里对 epoch 槽位的读取（L424-L442） |
| `include/fast_ulysses/group.hpp` | 契约与数据结构 | `Window` 结构（L23-L29）：`tensor` / `peer_ptrs` / `flag_ptrs` / `numel`；文件头注释（L5-L8） |
| `src/barrier.cu` | 设备层：标志位屏障（u5-l3 详解） | `barrier_kernel` 如何消费 `flags[ws] + epoch` 布局——下游契约 |
| `include/fast_ulysses/transfer.hpp` | 传输与屏障的契约注释 | 布局契约原文：「`uint64 flags[ws]` followed by `uint64 epoch`，调用方须提供至少 `(ws+1)*8` 字节且先清零」 |
| `include/fast_ulysses/common.hpp` | 错误检查与版本门槛 | torch 2.10 下限的 `static_assert`，原因正是 `get_signal_pad_size()` |
| `python/fast_ulysses/group.py` | Python 门面 | 构造时的 bootstrap 注册（L80-L89） |
| `docs/design.md` | 设计文档 | "Where the memory comes from" 一节：MemPool 顺序论证（L48-L59）、「触发分配的量必须全 rank 相同」（L71-L77） |

`allocate()` 是 `UlyssesGroup` 私有工具函数（[include/fast_ulysses/group.hpp:L166-L168](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/group.hpp#L166-L168)），有四个调用方，全部「分配时是集合操作」：内部窗口 `window()`（u4-l2）、lend 轮转槽 `lend_window()`（u4-l4）、调用者自持缓冲 `make_output()`（u4-l3）。本讲只讲 `allocate` 本身。

## 4. 核心概念与源码讲解

先给 `allocate()` 的全景骨架（对应 [src/group.cc:L207-L253](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L207-L253)）：

```text
allocate(numel, dtype):                      # 每个rank以相同参数执行
    TORCH_CHECK(numel > 0)                   # 4.1：全组同抛，不留人等待
    CUDAGuard(device_index_)                 # 4.1：把"当前设备"切到本组的卡
    t   = empty_strided_p2p({numel}, {1},    # 4.1：分配（集合）
                            dtype, device,
                            group_name, nullopt)   # nullopt = 不走 MemPool（4.2）
    sym = rendezvous(t, group_name)          # 4.2：会合，换全员地址（集合）
    TORCH_CHECK(sym, ...)                    # 4.2：不是对称分配/组未注册 → 报错
    win.tensor    = t
    win.peer_ptrs = get_buffer_ptrs()        # 4.2：ws 个数据区地址
    off           = flag_offset_slots(ws)    # 4.3：signal pad 末尾 ws+1 个槽
    win.flag_ptrs = get_signal_pad_ptrs()    # 4.3：每个 peer 的 pad 基址 + off*8
    TORCH_CHECK(peer_ptrs.size() == ws)      # 4.2：进程组成员与 rendezvous 一致
    get_signal_pad(rank_, {ws+1}, kLong, off).zero_()   # 4.4：只清自己的握手区
    sym->barrier(0, 0)                       # 4.4：torch 自带屏障，等全员清完（集合）
    return win
```

本讲的四个最小模块依次对应：分配（4.1）、会合与地址表（4.2）、握手区布局（4.3）、清零与屏障（4.4）。

### 4.1 empty_strided_p2p：分配一块「每个 rank 都有一份」的内存

#### 4.1.1 概念说明

`empty_strided_p2p` 是 torch 对称内存的**分配器入口点**。对它的直觉理解：

- 名字里的 **p2p** 指分配出的内存可被 peers 直接访问（peer-to-peer），而非「只在本卡可见」；
- 它返回的就是一个普通的 `at::Tensor`——本 rank 那一份，此时你还不知道 peers 的那份在哪（那是 4.2 `rendezvous` 的事）；
- 它是**集合操作**：分配过程内部要经由 bootstrap 与全组交换句柄，所以每个 rank 必须都以相同的尺寸调用它。

为什么 all-to-all 的「窗口」必须是这种内存？回看 u3-l2 的发射循环：每条 `CopyOp` 的 `dst_offset` 是**相对 peer p 窗口基址**的字节偏移。如果窗口是普通显存，拷贝引擎根本没有一个可写的对端地址。对称内存让 `peer_ptrs[p] + dst_offset` 成为一个合法的、直通对端显存的目的地址。

#### 4.1.2 核心流程

`allocate()` 的前半段（[src/group.cc:L207-L219](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L207-L219)）按序做三件事：

1. **尺寸守卫**：`TORCH_CHECK(numel > 0)`。什么时候会传 0？`numel` 是 `window_numel`，全组最大接收量；只有**所有** rank 的分片都为空时它才是 0——这正是报错消息的内容（"every rank's shard is empty"）。注释给出两层理由：其一，每个 rank 算出的窗口尺寸相同，所以这一检查**在所有 rank 上同时抛出**，不会留下任何人在集合操作里干等（这是 u2-l2「校验先于第一个屏障」纪律的又一次体现）；其二，零大小的对称分配 `data_ptr` 为 null，`rendezvous` 会拒绝 null 句柄——与其让下游报一个难懂的错误，不如在这里说清楚。
2. **设备切换**：`at::cuda::CUDAGuard guard(device_index_)`。一个 group 只服务一张卡（u2-l1 的 `device` 参数归一化），分配必须发生在正确的当前设备上。
3. **分配**：`empty_strided_p2p({numel}, {1}, dtype, device, group_name_, std::nullopt)`——一维、连续（stride 1）、指定 dtype、指定设备、挂在 `group_name_` 名下。

#### 4.1.3 源码精读

分配点与其注释（[src/group.cc:L216-L219](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L216-L219)）：

```cpp
// empty_strided_p2p rather than a MemPool: it is the allocator entry point directly, so no
// unrelated allocation can interleave with ours and desync the rendezvous order across ranks.
at::Tensor t = symm::empty_strided_p2p(
    {numel}, {1}, dtype, c10::Device(c10::DeviceType::CUDA, device_index_), group_name_, std::nullopt);
```

这段代码做了两件事：调分配入口点；第 6 个参数传 `std::nullopt`——**字面上**就是不挂到任何 `MemPool`。为什么这一点重要到值得写成注释，我们在 4.2.3 展开（一句话预告：`rendezvous` 按分配出现顺序逐个会合，任何「插队的无关分配」都可能让不同 rank 的第 N 次会合对上不同的缓冲）。

尺寸守卫的注释（[src/group.cc:L209-L214](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L209-L214)）原文强调了「全组同抛」与「null 句柄」两个理由，值得逐句读一遍——它是「本地可判定的错误提前抛、跨 rank 的失配交给契约」这条 u2-l2 原则在分配层的应用。

另外注意版本门槛：`include/fast_ulysses/common.hpp` 在每个翻译单元里检查 torch ≥ 2.10（[include/fast_ulysses/common.hpp:L8-L16](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/common.hpp#L8-L16)），理由写得很直白：`get_signal_pad_size()` 到 2.10 才成为自由 `TORCH_API` 函数，而**握手区在 pad 里的位置正是用它算的**（4.3 的主角）；`empty_strided_p2p` 与 `rendezvous` 本身反而更早就有（[docs/install.md:L10-L13](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/install.md#L10-L13)）。torch 版本下限由一个「不起眼的 pad 尺寸查询函数」决定，这是阅读依赖版本声明时少见但真实的一课。

#### 4.1.4 代码实践

**实践目标**：确认你本机 torch 的版本与对称内存 API 面，把「2.10 门槛」从文档断言变成亲眼所见。

**操作步骤**（无需 GPU，导入层面即可）：

```bash
# 示例命令
python -c "import torch; print(torch.__version__)"
python -c "from torch.distributed import _symmetric_memory as sm; print(sm.empty_strided_p2p); print(sm.rendezvous)"
python -c "from torch.distributed import _symmetric_memory as sm; import inspect; help(sm.empty_strided_p2p)"
python -c "from torch.distributed import _symmetric_memory as sm; print([n for n in dir(sm) if 'signal_pad' in n])"
```

**需要观察的现象**：

1. torch 版本号 ≥ 2.10（若否，本库无法对你的 torch 构建）。
2. `empty_strided_p2p` 与 `rendezvous` 在 Python 侧确实可导入。
3. `help()` 打印的参数列表——对照 4.1.2 中 C++ 调用位置的 6 个参数（size、stride、dtype、device、group_name、mem_pool）。
4. `dir()` 里与 signal pad 相关的名字（至少应能看到 `set_signal_pad_size`；是否存在查询当前 pad 大小的 getter 因 torch 版本而异）。

**预期结果**：前三项在 torch ≥ 2.10 的任何环境（包括无 GPU）都应成立；第 3、4 项的**具体打印内容因 torch 版本而异，待本地验证**——请把实际输出记录下来，第 5 节的综合实践会用到 pad 大小。

#### 4.1.5 小练习与答案

**练习 1**：`empty_strided_p2p` 返回的张量，和 `at::empty` 返回的张量，在「本 rank 看来」有什么区别？

<details><summary>参考答案</summary>

在「本 rank 看来」几乎没有区别——都是一个普通 `at::Tensor`，`data_ptr()` 指向本卡显存。区别在**组视角**：它附带完成了与全组的句柄交换，因此后续可以对这个分配调用 `rendezvous` 拿到每个 peer 那一份的地址；`at::empty` 的分配没有这层身份，peers 永远不知道它存在。
</details>

**练习 2**：为什么 `numel == 0` 的检查放在 `allocate()` 的第一行，而不是等到 `rendezvous` 自然失败？

<details><summary>参考答案</summary>

两个理由，都在源码注释里（[src/group.cc:L209-L214](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L209-L214)）。其一，`numel` 是全 rank 相同的量，这个检查会在所有 rank 上**同时**以同一条消息抛出，不会有人卡在集合操作里等一个已经抛异常的伙伴；其二，零大小分配的 `data_ptr` 是 null，`rendezvous` 拒绝 null 句柄时报出的错误远不如这条消息可读。本质是 u2-l2「本地可判定的事提前抛」。
</details>

### 4.2 rendezvous 与 peer 地址：把 ws 份分配对齐成一张地址表

#### 4.2.1 概念说明

rendezvous（会合）是整个对称内存机制里最「分布式」的一步。每个 rank 手里拿着自己那份分配（一个普通张量），需要回答的问题是：**「从我这个进程的地址空间看，其他 rank 的同一份分配在哪个地址？」** `rendezvous(t, group_name)` 做的就是全员交换这次分配的句柄，返回一个 `SymmetricMemory*`，它的 `get_buffer_ptrs()` 给出长度为 ws 的地址表——第 p 项就是 peer p 那份数据区**在本 rank 地址空间里**的地址。

为什么它是集合操作？因为「交换」本身就是集合：每个 rank 必须带着**同一次分配**、在各自程序的**同一点**到达会合，任何人都不能缺席。这也解释了 u2-l1 为什么说 `UlyssesGroup` 的构造是集合的——`enable_symm_mem_for_group` 的 bootstrap 注册为 `rendezvous` 铺路，而 `allocate` 内部的会合则发生在**每一次窗口分配**时（u1-l2 提过：新形状的首次调用会触发集合性的窗口分配，根源就在这里）。

地址表被装进 `Window` 结构（完整定义 [include/fast_ulysses/group.hpp:L23-L29](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/group.hpp#L23-L29)）：

```cpp
struct Window {
    at::Tensor            tensor;     ///< 持有分配存活
    std::vector<uint64_t> peer_ptrs;  ///< peer p 的那一份，从本 rank 看的地址
    std::vector<uint64_t> flag_ptrs;  ///< peer p 在其 signal pad 内的握手区地址
    int64_t               numel = 0;
};
```

这张表就是 u5-l1 传输层的一切输入：`launch_a2a_ce(src, peer_ptrs, plan, ...)` 的注释明确写着「peer p's window base, as addressed from this rank; `plan.ops` offsets are relative to it」（[include/fast_ulysses/transfer.hpp:L24-L27](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/transfer.hpp#L24-L27)）。**plan 出偏移、Window 出基址**，两者在传输层相加——这正是 u3 把 `CopyOp` 设计成「无指针」的原因。

#### 4.2.2 核心流程

```text
rank 0: t0 = empty_strided_p2p(...)  ─┐
rank 1: t1 = empty_strided_p2p(...)  ─┼─ rendezvous(t, group_name)：全员交换句柄（集合）
rank 2: t2 = empty_strided_p2p(...)  ─┘
                                        │
        ┌───────────────────────────────┴───────────────────────────────┐
        │ 每个rank得到同一张表：                                          │
        │   get_buffer_ptrs()     = [A0, A1, A2]   ← 各自地址空间里的同一组映射 │
        │   get_signal_pad_ptrs() = [P0, P1, P2]                          │
        │ 注意：rank r 视角的 A_r == t_r.data_ptr()（自己的那份）            │
        └──────────────────────────────────────────────────────────────────┘
```

`allocate()` 收表的过程（[src/group.cc:L229-L244](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L229-L244)）：`peer_ptrs` 逐个转成 `uint64_t` 存入 `Window`；`flag_ptrs` 在每个 pad 地址上叠加 4.3 要推导的 `off * 8` 偏移；最后校验 `peer_ptrs.size() == world_size_`——rendezvous 返回的成员数必须与构造时记下的组大小一致，不一致说明「进程组与这个组的成员身份对不上」，报错消息原文如此（"the process group and this group disagree about their membership"）。

#### 4.2.3 源码精读

会合点与会合失败的检查（[src/group.cc:L220-L227](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L220-L227)）：

```cpp
auto sym = symm::rendezvous(t, group_name_);
TORCH_CHECK(sym,
            "torch symmetric memory did not establish a rendezvous for a ", numel,
            "-element window on group '", group_name_, "'. The allocation is not a symmetric one, "
            "or the group is not registered with torch's symmetric-memory bootstrap.");
```

这条 `TORCH_CHECK` 防住两类 misuse：对一个**非对称**张量调 rendezvous，或者忘了 `enable_symm_mem_for_group`。两条路径都以可读的消息失败，而不是空指针解引用。

接下来是**本讲最重要的一段设计论证**——为什么不走 `MemPool`。design.md 的原文（[docs/design.md:L55-L59](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/design.md#L55-L59)）：

> `rendezvous` is collective: every rank must reach it for the same allocation at the same point in its own program. Nothing unrelated can be served from that entry point, so the sequence of allocations this group makes is the sequence every rank makes, and the SPMD call contract is the only thing it rests on.

拆开读：

1. **会合按顺序配对**。第 N 次到达 `rendezvous` 的所有 rank 会互相同意「这是第 N 次分配」。配对靠的是**到达顺序**，不靠内容。
2. **`MemPool` 会破坏顺序**。`MemPool` 是一个共享池：多个分配从同一大块里切。如果本库通过池分配，那么**任何**同样使用该池的无关分配（比如别的库调 `symm_mem`）都可能插进本库的两次分配之间——而它在不同 rank 上的插入时机可以不同。于是 rank 0 的第 3 次会合对上「窗口 X」，rank 1 的第 3 次会合对上「别人的缓冲」——地址表静默错位，后续拷贝写进错误的内存。
3. **直接走入口点 = 顺序封闭**。`empty_strided_p2p(..., std::nullopt)` 绕开任何池，本库的每次分配都独立到达会合。于是在「所有 rank 发出相同调用序列」（SPMD 契约，u1-l2）这一个前提下，**每个 rank 的会合序列完全一致**——顺序论证只需押在 SPMD 契约上，不需要任何额外同步。

这个论证还有一个推论，design.md 紧接着写明（[docs/design.md:L71-L77](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/design.md#L71-L77)）：**触发分配的量必须是全 rank 共享的量**——形状、dtype、调用计数都是；引用计数不是。`allocate()` 的四个调用方（`window()`、`lend_window()`、`make_output()`）何时触发分配，全部由这类共享量决定；u4-l2 与 u4-l4 将分别把这条约束用在「按 (role, dtype) 缓存」和「按调用计数轮转」上。本讲只需记住：**`allocate` 是集合的，所以它的每个触发点都必须是全 rank 一致的**。

#### 4.2.4 代码实践（源码阅读型）

**实践目标**：沿着源码回答「组身份错了会发生什么」，把 4.2.3 的两条检查与失败模式对上号。

**操作步骤**：

1. 读 [src/group.cc:L239-L244](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L239-L244) 的成员数检查，写下：如果某 rank 用子组 A 构造 `UlyssesGroup`、另一 rank 用更大的 WORLD 组构造，哪一行先失败？失败是抛错还是挂起？
2. 读 [python/fast_ulysses/group.py:L80-L89](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L80-L89) 与 [src/group.cc:L221-L227](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L221-L227)，写下：如果删掉构造时的 `enable_symm_mem_for_group`，torch 2.10 下首次 `allocate` 会在哪一行、以什么消息失败？torch ≥ 2.13 呢？
3. 对照 `Window` 结构（[include/fast_ulysses/group.hpp:L23-L29](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/group.hpp#L23-L29)）：`tensor` 字段除了「持有分配存活」，为什么还隐含着「窗口的内容在 `Window` 被销毁前一直可写」？提示：u4-l2 的增长路径会 `windows_.erase(it)` 先于 `allocate`。

**需要观察的现象 / 预期结果**（纸面推演即可完成）：

1. 成员数不一致是**本地可判定**的（rendezvous 返回的表长 vs 构造记下的 `world_size_`），所以是抛错，消息为 "the process group and this group disagree about their membership"——不会挂起。
2. 未注册 bootstrap 时，torch 2.10 下 `rendezvous` 建立失败 → L221 的 `TORCH_CHECK(sym, ...)` 抛出，消息含 "not registered with torch's symmetric-memory bootstrap"；torch ≥ 2.13 因 rendezvous 自行解析组，删除注册**可能不再报错**——这正是 Python 侧注释说该调用「deprecated from torch 2.13」却仍保留的原因（保持 2.10 可用）。此推断**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`peer_ptrs` 里的第 p 个地址，是「peer p 自己看到的地址」还是「本 rank 看到的地址」？为什么这个区别至关重要？

<details><summary>参考答案</summary>

是**本 rank 地址空间里**的地址（结构体注释原话 "as addressed from here"）。对称内存通过 VMM 映射让本 rank 的页表里有直达 peer 显存的映射；拷贝由**本 rank** 发出，所以必须用本 rank 视角的地址。如果存的是 peer 自己视角的地址，`cudaMemcpy2DAsync` 会把它解释成本进程的地址，写进完全无关的内存。
</details>

**练习 2**：用一句话说出「不走 MemPool」与「SPMD 契约」之间的关系。

<details><summary>参考答案</summary>

rendezvous 按到达顺序给分配配对；直接走分配入口点保证本库的分配序列不被无关分配插队，于是在「所有 rank 发出相同调用序列」这一 SPMD 前提（且仅在这一前提下）每个 rank 的会合序列自然一致——池会引入本库控制之外的插入时机，把顺序论证变得不可能。
</details>

### 4.3 signal pad 握手布局：flags[ws] + epoch 放在 pad 末尾

#### 4.3.1 概念说明

传输需要地址表（4.2 已得），**屏障也需要**——u5-l3 的标志位屏障要往每个 peer 的握手区里写标志。本库需要的握手区极小：

\[ \text{uint64 flags}[ws] \;+\; \text{uint64 epoch} \quad\Rightarrow\quad ws + 1 \text{ 个 8 字节槽位} \]

- `flags[t]`（在 rank r 的 pad 里）：**由 peer t 写**、由 rank r 读的到达标志——「t 已经到了第几个 epoch」；
- `epoch`：本 rank 的轮次计数器，由本 rank 的屏障内核用 `atomicAdd` 推进（barrier.cu L37 的 `local + ws` 正是这个槽）。

为什么不把这块放在**数据区**而放在 **signal pad**？两个原因：其一，数据区是窗口本体，尺寸由 `window_numel`（数据量的函数）决定，塞进握手区会污染「窗口 = 输出的稠密前缀」这一 u3-l2 不变量；其二，pad 本来就是为握手设计的——对 peers 可见，且 torch 自己的屏障也在用。

那为什么是 pad 的**末尾**？源码注释一锤定音（[src/group.cc:L18-L20](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L18-L20)）：

> It lives in the LAST slots of the allocation's signal pad, because torch's own `SymmetricMemory::barrier()` — which window setup calls — uses channels counted from the front, and the two must not overlap.

**torch 自己的屏障从 pad 前端按 channel 计数使用**，而 `allocate()` 的收尾恰好要调一次 `sym->barrier(0, 0)`（channel 0，见 4.4）。本库的握手区若也从前端放，两者会互相践踏。放**末尾**是唯一无冲突的位置——前段归 torch 的通道，尾段归本库。

#### 4.3.2 核心流程

三个纯函数式的算术（[src/group.cc:L21-L37](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L21-L37)）：

\[
\text{pad\_slots} = \frac{\text{get\_signal\_pad\_size}()}{8}
\qquad
\text{flag\_slots}(ws) = ws + 1
\qquad
\text{off} = \text{pad\_slots} - \text{flag\_slots}(ws)
\]

外加一条守卫：\(\text{pad\_slots} \ge \text{flag\_slots}(ws) + 8\)——pad 除了装下握手区，还得给 torch 自己的通道留至少 8 个槽的余量。布局图：

```text
rank p 的 signal pad（pad_slots 个 uint64 槽，自 pad 基址 P_p 起）
┌──────────────────────────────┬─────────────────────────────┬────────┐
│ torch 自己的通道（从前端计数）  │ flags[0] … flags[ws-1]      │ epoch  │
│  sym->barrier(0,0) 用通道 0   │ ← flag_ptrs[p] = P_p+off*8 →│        │
└──────────────────────────────┴─────────────────────────────┴────────┘
              ↑ 前端                  末尾 ws+1 个槽（本库私有）      ↑
```

`allocate()` 里 flag 地址的合成（[src/group.cc:L235-L238](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L235-L238)）：对每个 peer 的 pad 基址加 `off * 8` 字节。注意单位换算——`off` 是 **uint64 槽位数**，乘 8 变字节；后面 `get_signal_pad` 的 `offset` 参数又是**以 dtype 元素计**的槽位数（dtype 是 `kLong`，8 字节一槽，恰好一比一）。

这个布局是**三份代码共同维护的契约**，这就是为什么它写死在头文件注释里（[include/fast_ulysses/transfer.hpp:L44-L46](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/transfer.hpp#L44-L46)）：

> The layout inside each rank's flag region is `uint64 flags[ws]` followed by `uint64 epoch`, so the caller must provide at least `(ws + 1) * 8` bytes and zero them before the first call.

三份代码交叉验证（值得逐一核对）：

| 消费者 | 代码 | 用的槽 |
| --- | --- | --- |
| `allocate()`（写入方，清零） | [src/group.cc:L250](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L250) `get_signal_pad(rank_, {flag_slots(ws)}, at::kLong, off)` | 从 `off` 起的 `ws+1` 个槽 |
| `barrier_kernel`（读写方） | [src/barrier.cu:L37](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/barrier.cu#L37) `atomicAdd(local + ws, 1)`；[L43](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/barrier.cu#L43) `peers.p[t] + rank` | epoch 是 `local+ws`；发布到 peer t 的 `flags[rank]` |
| `epoch()`（测试读取方） | [src/group.cc:L437-L438](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L437-L438) `flag_ptrs[rank_] + world_size_ * 8` | epoch：基址后第 `ws` 槽 = `+ws*8` 字节 |

三个独立表达式（`off` 槽偏移、`local + ws`、`+ ws * 8`）指向同一个槽——契约一致。

#### 4.3.3 源码精读

`flag_slots` 与 `flag_offset_slots` 全文（[src/group.cc:L18-L37](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L18-L37)）：

```cpp
// The handshake needs uint64 flags[ws] followed by uint64 epoch. It lives in the LAST slots of the
// allocation's signal pad, because torch's own SymmetricMemory::barrier() -- which window setup
// calls -- uses channels counted from the front, and the two must not overlap.
int64_t flag_slots(int world_size) { return world_size + 1; }

int64_t flag_offset_slots(int world_size)
{
    const int64_t pad_slots = static_cast<int64_t>(symm::get_signal_pad_size()) / 8;
    TORCH_CHECK(pad_slots >= flag_slots(world_size) + 8,
                "the symmetric-memory signal pad holds ", pad_slots,
                " uint64 slots, too few for a ", world_size,
                "-rank handshake plus torch's own channels. Raise it with "
                "torch.distributed._symmetric_memory.set_signal_pad_size() before any allocation.");
    return pad_slots - flag_slots(world_size);
}
```

三个细节值得咀嚼：

1. **`+8` 的余量**：守卫不是「装得下就行」，而是「装下后还得剩 8 个槽给 torch 的通道」。torch 的 `barrier(chan, op)` 按通道号从前端取槽；本库 setup 用了 channel 0，将来 torch 侧别的通道使用不应与本库冲突。
2. **失败是可修复的**：报错消息直接给出药方——在任何分配**之前**调 `set_signal_pad_size()`（它是全局设置，分配之后改无效）。结合 u2-l1 的 ws ≤ 8 上限：flag_slots 最大 9，守卫要求 pad_slots ≥ 17（136 字节）；torch 的默认 pad 尺寸远大于此，所以正常情况下这条检查永不触发——它是给「用户显式改小了 pad」或未来 torch 默认变化兜底的。默认 pad 的具体字节数**待本地验证**（见综合实践）。
3. **为什么用自由函数 `get_signal_pad_size()`**：它回答「torch 给每次分配配了多大的 pad」——这个值由 torch 决定，本库只能查询、不能假设。它正是 torch 2.10 版本门槛的来源（4.1.3）。

#### 4.3.4 代码实践（纸面计算）

**实践目标**：不看源码，手算任意 `ws` 下的握手布局；并用三份代码交叉核对。

**操作步骤**：

1. 设某 torch 环境的 `get_signal_pad_size()` 返回 1024 字节（**假设值，仅为算术示例**；真实值在综合实践中打印）。计算 `ws = 8` 时的：`pad_slots`、`flag_slots`、守卫是否通过、`off`、`flag_ptrs[p]` 相对 pad 基址 `P_p` 的字节偏移、epoch 槽的字节偏移。
2. 同一 pad 尺寸下改 `ws = 5`，重复上述计算。
3. 假设用户把 pad 设成 64 字节、`ws = 8`：写出守卫的计算与结果。
4. 交叉核对：把 [src/barrier.cu:L37](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/barrier.cu#L37) 的 `local + ws` 与 [src/group.cc:L438](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L438) 的 `flag_ptrs[rank_] + world_size_ * 8` 换算成「相对本 rank flag 区基址的槽位」，验证两者相同。

**需要观察的现象 / 预期结果**：

1. `ws = 8`：`pad_slots = 128`，`flag_slots = 9`，守卫 `128 ≥ 9 + 8 = 17` ✓，`off = 119`；`flag_ptrs[p] = P_p + 952` 字节；epoch 在 `P_p + 952 + 64 = P_p + 1016` 字节处（倒数第 1 槽）。
2. `ws = 5`：`flag_slots = 6`，守卫 `128 ≥ 14` ✓，`off = 122`；epoch 在 pad 基址后 `122*8 + 5*8 = 1016` 字节——**同一位置**！epoch 永远是 pad 的最后一个槽，`flags` 紧贴其后向左排。
3. pad = 64 字节、`ws = 8`：`pad_slots = 8 < 17`，[src/group.cc:L29-L35](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L29-L35) 的 `TORCH_CHECK` 抛出，消息提示在分配前调 `set_signal_pad_size()`。
4. `local + ws`：基址后第 `ws` 槽；`flag_ptrs[rank_] + ws*8`：flag 区基址（= pad 基址 + off*8）后 `ws*8` 字节 = 第 `ws` 槽。一致 ✓。

#### 4.3.5 小练习与答案

**练习 1**：如果本库把握手区放在 pad **前端**（offset = 0），具体会发生什么坏事？

<details><summary>参考答案</summary>

`allocate()` 收尾调用的 `sym->barrier(0, 0)` 会使用 torch 的 channel 0——从 pad 前端起算的槽位。握手区若也在前端，torch 屏障写的标志会与本库的 `flags[]`/`epoch` 落在同一批槽上：轻则 torch 的屏障把 `flags` 改成意外值、本库屏障提前放行（读到未完成的传输），重则本库清零握手区时抹掉 torch 正在用的通道状态。两端各用一头，冲突在结构上不可能。
</details>

**练习 2**：`flags[t]` 这个槽由谁写、由谁读？`epoch` 呢？

<details><summary>参考答案</summary>

rank r 的 `flags[t]` 由 **peer t 写**（barrier.cu L43：rank r 发布时写的是「peer t 的 flags[r]」，即每个写者写对方pad里以自己编号命名的槽）、由 rank r 自旋等待读取（L45-L48，等待条件 `v >= epoch`）。`epoch` 由 rank r 自己的屏障内核 `atomicAdd` 推进（L37），peers 不直接读它——它们各自也推进自己的 epoch，并把它作为发布值写进对方的 `flags`。
</details>

**练习 3**：为什么 `epoch()`（测试辅助方法）读的地址是 `flag_ptrs[rank_] + world_size_ * 8`，而不是 `flag_ptrs[rank_] + flag_slots(...) * 8`？

<details><summary>参考答案</summary>

两者其实指向同一槽：`flag_ptrs[rank_]` 已经是 flag 区基址（含 `off*8`），epoch 是其后第 `ws` 槽，故 `+ ws*8`。若再加 `flag_slots = ws+1` 槽就越过 epoch、读到 pad 之外的内存。源码注释（[src/group.cc:L437](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L437)）明说「epoch 位于 ws 个 flags 之后一槽，正是 barrier_kernel 的 atomicAdd 落点」。
</details>

### 4.4 清零与 barrier：`allocate()` 最后两行为什么是集合的

#### 4.4.1 概念说明

`allocate()` 的收尾（[src/group.cc:L246-L252](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L246-L252)）：

```cpp
// This allocation may be reusing memory an earlier window freed, and the epoch has to start at
// zero. Clear only our own region -- torch's channels are its business -- then hold every rank
// until all have cleared, or one rank's first publish lands in a pad another rank has yet to clear,
// is erased, and that rank waits forever for a write that already happened.
sym->get_signal_pad(rank_, {flag_slots(world_size_)}, at::kLong, off).zero_();
sym->barrier(0, 0);
```

三个事实叠加，推出这两行的必要性：

1. **分配器会复用内存**。design.md 明确（[docs/design.md:L61-L63](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/design.md#L61-L63)）：内存不预提交、也不单调增长，「dropping a window returns it to the symmetric allocator」。u4-l2 的增长路径先 `erase` 再 `allocate`，正是为了让分配器**当场复用**刚释放的段。于是新窗口的 pad 里可能躺着**上一个窗口的 flags 与 epoch 残值**。
2. **epoch 必须从 0 开始**。屏障的等待条件是 `v >= epoch`（[src/barrier.cu:L48](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/barrier.cu#L48)），且发布是 MAX、标志只增不减（[src/barrier.cu:L10-L13](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/barrier.cu#L10-L13)）。若 `flags[t]` 里残留着一个大于等于当前 epoch 的旧值，本 rank 的第一次等待会**立即假通过**——屏障没有真正同步，rank 会读到还没写完的窗口字节。清零让「第一次等待」必须等到真实的第一次发布。
3. **清零与别人的发布竞争同一块内存**。注意方向：peers 往**我的** pad 里写 `flags[他们的编号]`（4.3 练习 2），而我清零的正是**我的** pad。如果不加约束，这两个操作可以任意交错。

第 3 点正是注释描述的竞态。看**没有** `sym->barrier(0, 0)` 时的坏时间线（ws = 2，rank A 与 B）：

| 时刻 | rank A | rank B |
| --- | --- | --- |
| t1 | `zero_()` 自己的握手区 | （还卡在别的主机工作 / 尚未到 zero） |
| t2 | `allocate` 返回；发出第一次 all_to_all；屏障内核发布 `epoch=1` → **写进 B 的 flags[A]** | — |
| t3 | — | `zero_()` 自己的握手区 → **把 A 刚写的 flags[A] 抹成 0** |
| t4 | 自己的 flags[B] ≥ 1？B 还没发布，A 等待 | `allocate` 返回；第一次屏障：等 flags[A] ≥ 1——**但那次写入已被抹掉** |
| t5 | （若 A 不再发起新调用）双方对望 | **B 永远等一个「已经发生过」的写入** |

`sym->barrier(0, 0)` 把「全员清零完成」钉在「任何 rank 离开 `allocate`」之前：barrier 本身是集合操作，一个 rank 要通过它，必须所有 rank 都已到达——而每个 rank 只有清零完自己的握手区才会到达。于是任何 rank 的首次发布（只可能发生在它离开 `allocate` 之后）必然落在**已清零**的 pad 里。

两个容易忽略的细节：

- **只清自己的 `ws + 1` 个槽**。「torch's channels are its business」——前段通道归 torch 管，我们既不能假设其内容、也不该碰它；何况我们**自己马上要用 channel 0**（就是这条 barrier）。
- **这条 barrier 是 torch 的，不是本库的**。`sym->barrier(chan, op)` 走 torch `SymmetricMemory` 自带的屏障实现（在 pad 前段通道上自旋）。本库自己的 `fast_barrier`（u5-l3）只在集合调用的路径上出现。**setup 用 torch 的屏障、稳态用自己的屏障**——而正因为 setup 用了 torch 的前端通道，4.3 才必须把本库握手区放尾部。两个设计决定互为因果。

#### 4.4.2 核心流程

```text
allocate 收尾（每个 rank）：
    zero_()  ← 只清自己的 flags[ws]+epoch（尾段 ws+1 槽）
       │
       ▼
    sym->barrier(0, 0)  ← torch 屏障，channel 0（前端）
       │  集合：全员到达 ⇔ 全员已清零
       ▼
    返回 Window —— 此后任何 rank 的首次发布
                    必然落进已清零的 pad
```

#### 4.4.3 源码精读

逐参数读清零那行（[src/group.cc:L250](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L250)）：

```cpp
sym->get_signal_pad(rank_, {flag_slots(world_size_)}, at::kLong, off).zero_();
```

- `rank_`：取**本 rank** 的 pad（也可以取任意 peer 的——地址表都在——但清零是「各清各的」）；
- `{flag_slots(world_size_)}`：视图形状，`ws + 1` 个元素；
- `at::kLong`：int64，8 字节一槽——与 pad 的 uint64 槽语义一致；
- `off`：偏移，**单位是 kLong 元素个数**，即槽位数（4.3 算出的 `pad_slots - (ws+1)`）；
- `.zero_()`：就地清零这个视图——不多不少正好是本库的握手区。

紧随其后的（[src/group.cc:L251](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L251)）`sym->barrier(0, 0)` 两个参数分别是 channel 与 op，取默认通道 0。它同时回答了一个前置问题：**为什么 `allocate` 在 CUDA graph 捕获前必须先跑一遍**——design.md 的捕获边界一节（[docs/design.md:L92-L96](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/design.md#L92-L96)）列明窗口分配的三步 `empty_strided_p2p + rendezvous + zero_()` 均不合法于捕获内，u5-l3 会展开。

顺带一提 `ws == 1`：`fast_barrier` 对单人组直接返回（[src/barrier.cu:L56-L57](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/barrier.cu#L56-L57)），但 `allocate` 里的 `sym->barrier(0, 0)` 仍然照跑——单人组上它退化为一次本地同步，不构成开销，也保持了「分配是集合操作」的统一语义（u1-l3：world_size=1 是合法一等公民）。

#### 4.4.4 代码实践（纸面推演）

**实践目标**：亲手复现 4.4.1 的竞态论证，并检查两个变体问题。

**操作步骤**：

1. 把 4.4.1 的时间线表抄下来并扩展一列「窗口数据」，标出 t4 时刻 B 若被（错误的）旧 flags 值提前放行，它会从窗口读到什么。
2. 变体一：如果把 `zero_()` 和 `sym->barrier(0, 0)` **交换顺序**（先 barrier 后清零），坏时间线还成立吗？
3. 变体二：如果 `zero_()` 清的是**整个 pad**（从 offset 0 到末尾）而不是自己的尾段，会发生什么？
4. 核对 [src/group.cc:L246-L249](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L246-L249) 的注释原文，确认你写的时间线与作者给出的一致（"one rank's first publish lands in a pad another rank has yet to clear, is erased, and that rank waits forever for a write that already happened"）。

**需要观察的现象 / 预期结果**：

1. t4 时窗口里是 peers 尚在写入（或尚未开始写）的字节——旧窗口残值或半新半旧的数据。
2. 变体一：**仍成立且更糟**。先 barrier 意味着「全员到达 barrier」不再蕴含「全员清零完」——清零完全脱离了集合点的保护，A 的发布与 B 的清零可以任意交错，竞态原样回来。
3. 变体二：会抹掉 torch 前段通道的内容——而 `sym->barrier(0,0)`（我们正要用的 channel 0）就住在那里：清掉 channel 0 的标志等于伪造「全员已到达」，barrier 假通过；torch 将来在该分配上做的任何屏障也都不可信。
4. 注释原文与手写时间线一致；差异只在「A 若继续发第二次调用会以 MAX 语义把 B 放出来、但 B 已落后整整一次调用、窗口数据可能已被覆盖」这一层——源码注释用「waits forever」概括的是不再推进的情形。

#### 4.4.5 小练习与答案

**练习 1**：`allocate()` 里清零的是谁的 pad？屏障内核发布时写的又是谁的 pad？这两件事为什么会冲突？

<details><summary>参考答案</summary>

清零的是**本 rank** 的 pad（`get_signal_pad(rank_, ...)`）；发布时 rank r 写的是**每个 peer** 的 pad（`peers.p[t] + rank`，即 peer t 的 `flags[r]`）。冲突在于：我清零的内存正是别人发布的落点——「清自己的」与「别人写我的」竞争同一块，若不同步就可能出现「先写后清」，发布被抹掉。
</details>

**练习 2**：新窗口的 pad 里为什么可能有旧值？这与本讲哪一条设计（在 u4-l2 展开）相关？

<details><summary>参考答案</summary>

因为窗口释放后内存**还给分配器**并可被后续分配复用（design.md：内存不单调增长）；u4-l2 的增长路径「先 `erase` 再 `allocate`」正是为了让分配器当场复用刚释放的段。于是同一块物理 pad 会先后属于不同窗口，残留旧窗口的 flags/epoch。这也是 `ws==1` 之外的、与 world_size 无关的清零理由。
</details>

**练习 3**：为什么 `sym->barrier(0, 0)` 用 channel 0 不会破坏本库的握手区？

<details><summary>参考答案</summary>

torch 的屏障按 channel 从 pad **前端**计数取槽；本库握手区在**末尾** `ws+1` 个槽（4.3 的 `flag_offset_slots`）。两者在布局上不相交——这正是注释「the two must not overlap」的实现方式：不是运行时协调，而是位置上天然错开。
</details>

## 5. 综合实践

**实践目标**：把 4.1–4.4 串起来——亲手用 torch 的对称内存 API 完成 `allocate()` 做的事，逐项对照两边的行为；无 GPU 时以「逐行注释伪代码 + 集合点清单」完成同一目标。

### 路线 A：≥2 GPU（或 1 GPU 的退化版）

**操作步骤**：

1. 把下面的脚本存为 `manual_symm.py`（**示例代码，非项目原有文件**；建议放在仓库外或临时目录，不要写进项目源码树）：

```python
# manual_symm.py — 示例代码：手工复刻 UlyssesGroup::allocate() 的前半段
import os
import torch
import torch.distributed as dist
import torch.distributed._symmetric_memory as symm_mem

dist.init_process_group("nccl")
rank, ws = dist.get_rank(), dist.get_world_size()
torch.cuda.set_device(int(os.environ["LOCAL_RANK"]))

# —— 对应 python/fast_ulysses/group.py L80-85：bootstrap 注册（2.13 前必须）——
name = dist.group.WORLD.group_name
symm_mem.enable_symm_mem_for_group(name)

# —— 对应 src/group.cc L218-219：分配器入口点，nullopt（不传 mem_pool）——
t = symm_mem.empty_strided_p2p((256,), (1,), torch.bfloat16, torch.device("cuda"), name)

# —— 对应 src/group.cc L220：会合，换全员地址 ——
sym = symm_mem.rendezvous(t, name)

buf_ptrs = sym.get_buffer_ptrs()            # peer_ptrs 的原料（L232-234）
pad_ptrs = sym.get_signal_pad_ptrs()        # flag_ptrs 的原料（L236-238）

print(f"[rank {rank}/{ws}] buffer ptrs: {[hex(p) for p in buf_ptrs]}")
print(f"[rank {rank}/{ws}] pad ptrs:    {[hex(p) for p in pad_ptrs]}")

# 结构验证 1：表长 == world_size（对应 group.cc L239-244 的检查）
assert len(buf_ptrs) == ws and len(pad_ptrs) == ws

# 结构验证 2：本 rank 表中自己的那一项 == 自己张量的 data_ptr
assert buf_ptrs[rank] == t.data_ptr()

# 结构验证 3：全员对「谁在哪」达成一致 —— all_gather 各自的 data_ptr 再比对
mine = [0] * ws
dist.all_gather_object(mine, t.data_ptr())
assert [int(p) for p in buf_ptrs] == mine, f"{[hex(p) for p in buf_ptrs]} != {mine}"
print(f"[rank {rank}] rendezvous 地址表与全组 data_ptr 一致 ✓")

# —— 对应 src/group.cc L250：清零自己的尾段握手区 ——
# off 的计算需要 pad 尺寸：在 dir(symm_mem) 里找 signal_pad 相关的 getter；
# 若你的 torch 未在 Python 侧暴露 getter，此步标注「待本地验证」并跳过清零后的写入测试。
```

2. 运行（2 卡；1 卡也能跑，退化为 ws=1 的单元素地址表）：

```bash
# 示例命令
torchrun --nproc_per_node=2 manual_symm.py
```

3. 在同一进程里接着调一次本库（可选）：`from fast_ulysses import UlyssesGroup`，构造后 `g.empty_output(x, mode=0)` 触发一次真实的 `allocate`，观察它正常返回——说明同一环境里两条路径共存。

**需要观察的现象**：

1. 每个 rank 打印的 `buffer ptrs` 长度等于 `ws`，且互不相同（不同卡显存地址本就不同）。
2. `buf_ptrs[rank] == t.data_ptr()` 断言通过——「从我看，我的那份就是我张量的地址」。
3. 断言 3 通过——所有 rank 的地址表是同一张表（第 p 项处处相等）。
4. `pad ptrs` 同样是 ws 项；每个 pad 基址与对应 buffer 基址不同（pad 是独立的小区域）。

**预期结果**：以上 4 条全部成立；具体地址数值**待本地验证**。若第 3 节实践中找到了 pad 尺寸 getter，可进一步按 4.3 的公式算 `off` 并打印 `flag_ptrs = [p + off*8 for p in pad_ptrs]`，再用 `sym.get_signal_pad(rank, (ws+1,), torch.int64, off)` 取出视图、写入再读回，验证「这块内存对 peers 可写」——这正是屏障发布动作的雏形（u5-l3 的 `red.release.sys` 做的同一件事的显式内存序版本）。**待本地验证**。

### 路线 B：无 GPU（伪代码 + 集合点清单）

写出 `allocate()` 的逐行注释伪代码，要求：每行注明对应源码行号；在每一处**集合点**（所有 rank 必须同时到达的操作）上打标记 `★`；在每一处**全 rank 相同的量**上打标记 `=`。完成后的清单应包含：

| 位置 | 语句 | 标记 | 说明 |
| --- | --- | --- | --- |
| 构造时（group.py L85） | `enable_symm_mem_for_group` | ★ | bootstrap 注册，幂等 |
| group.cc L218 | `empty_strided_p2p({numel}, ...)` | ★/= | `numel` 全 rank 相同（u3 的 window_numel 取全组最大） |
| group.cc L220 | `rendezvous(t, group_name_)` | ★ | 顺序配对依赖 4.2.3 的「不走 MemPool」论证 |
| group.cc L250 | `get_signal_pad(rank_, ...).zero_()` | =（参数全 rank 同构） | 各清各的；`off` 由 `get_signal_pad_size()`（torch 全局相同）与 `ws`（相同）决定 |
| group.cc L251 | `sym->barrier(0, 0)` | ★ | 「全员清完」先于「任何人离开」 |

**预期结果**：五个集合点全部标出；并能回答压轴一问——「如果两个 rank 的程序在不同位置触发了一次窗口分配（比如 rank 0 在第 3 次调用、rank 1 在第 5 次调用才需要新窗口），上表哪一行会出问题？」（答案：`rendezvous` 一行——顺序配对错位，这正是 design.md「触发分配的量必须全 rank 相同」要防的场景，u4-l2 / u4-l4 的缓存与轮转策略全部是这条约束的落实。）

## 6. 本讲小结

- **`allocate()` = 分配 + 会合 + 收址 + 布局 + 清零 + 屏障**：`empty_strided_p2p` 给出本 rank 那一份，`rendezvous` 把 ws 份分配对齐成一张全员地址表，装进 `Window{tensor, peer_ptrs, flag_ptrs, numel}`——u3 的 `CopyOp` 偏移与这里的基址在 u5-l1 的传输层相加。
- **两者都是集合操作**：分配要交换句柄、会合要全员到场；`empty_strided_p2p(..., std::nullopt)` 直连分配器入口点，使本库的会合顺序不被无关分配插队，顺序论证只押在 SPMD 契约上——由此得出全局约束「触发分配的量必须全 rank 相同」。
- **握手区 `flags[ws] + epoch` 放 signal pad 末尾**：torch 自己的 `SymmetricMemory::barrier()` 从前端按通道用 pad，而窗口 setup 恰好要调它（channel 0）；`flag_offset_slots = pad_slots - (ws+1)`，守卫还要求给 torch 通道留 8 槽余量。该布局由 `allocate`、`barrier_kernel`、`epoch()` 三份代码共同维护。
- **收尾两行缺一不可**：分配器会复用旧窗口的内存，epoch 必须从 0 开始；且「我清零我的 pad」与「peers 往我的 pad 发布」竞争同一块内存——`sym->barrier(0, 0)` 把「全员清完」钉在「任何 rank 首次发布」之前，否则一个已被抹掉的发布会让对端永远等下去。
- **`Window` 是后续三讲的共同起点**：`window()` 按 (role, dtype) 缓存它（u4-l2）、`make_output()` 把它交给调用者（u4-l3）、`lend_window()` 轮转它（u4-l4），而 `peer_ptrs`/`flag_ptrs` 分别喂给拷贝引擎（u5-l1）与标志位屏障（u5-l3）。

## 7. 下一步学习建议

下一讲 **u4-l2（窗口生命周期：按 (role, dtype) 缓存与增长）**紧接本讲：`allocate()` 的四个调用方中最核心的 `window()`——为什么内部窗口按 `(WindowRole, dtype)` 二元组缓存、增长路径为何「先 erase 再 allocate」、以及 sync/async 两流为何必须各持一个窗口。读完 u4-l2 再看 u4-l3（`empty_output` 与 `out=`）与 u4-l4（lend 轮转），就能完整回答第 5 节的压轴问题。

若想先看「地址表被谁消费」，可以跳到 u5-l1（`launch_a2a_ce` 精读，`peer_ptrs` 如何变成 `cudaMemcpy2D/3DAsync` 的目的地址）与 u5-l3（标志位屏障，`flag_ptrs` 如何变成 `red.release.sys` 的落点）；`test/distributed/cudagraph.py` 则用 `epoch()`（本讲 4.3 的第三份契约代码）验证 CUDA graph 重放下 epoch 每次 +2——那是「epoch 放设备上」这条设计的直接受益者。
