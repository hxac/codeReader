# 标志位屏障：自旋内核、设备侧 epoch 与 CUDA graph

## 1. 本讲目标

学完本讲，你应该能够：

1. 逐行解释 `barrier_kernel` 的发布-等待（publish-wait）协议：谁向哪里写什么、谁在哪里自旋等什么、`red.release.sys` 与 `ld.acquire.sys` 各自承诺了什么。
2. 论证 epoch 计数器为什么必须放在设备上、由内核里的 `atomicAdd` 推进——而不是由主机算好当参数传进去——以及这如何决定 CUDA graph 重放的正确性。
3. 说出 sync / async 两条路径各自的 CUDA graph 捕获边界，以及捕获前「预热所有形状 + 传 `out=` 固定地址」这条纪律背后的原因。
4. 复述「为什么不用 `cuStreamWriteValue64` / `cuStreamWaitValue64`」的实测依据与文档化机制，以及一段被修正过的错误归因。

本讲是 u5 单元的第三讲，承接 u5-l1（拷贝引擎传输 `launch_a2a_ce`）与 u4-l1（对称内存窗口分配、signal pad 握手区布局）。读完本讲，u9-l3 的对抗性测试（CE 顺序、窗口竞态）和 u9-l5 的设计反思才有完整的落点。

## 2. 前置知识

- **屏障（barrier）**：集合通信中的「全员到齐才放行」。本库不用 NCCL 的屏障，而是自己在 P2P 可访问的内存上实现一个，因为传输走的是拷贝引擎（CE），配一个 NCCL 屏障会把整个设计拖回 SM 内核。
- **发布-等待协议**：最朴素的分布式屏障。每个参与者把自己的「轮次号」写进所有同伴都能看到的格子（发布），然后自旋读自己那排格子，直到所有同伴都公布了同一轮次（等待）。本讲的 `flags[ws]` 数组就是这排格子。
- **release / acquire 内存序**：CPU 与 GPU 都有的内存排序原语。release 写保证「我之前的所有写入，对随后以 acquire 读到我这个值的线程可见」；acquire 读是它的另一半。没有它们，写入可能被重排或滞留在缓存里，等待方读到旧值。
- **PTX 与 `.sys` 作用域**：PTX 是 NVIDIA 的虚拟指令集，CUDA C++ 可以用 `asm volatile` 内联。内存序指令带作用域后缀：`.gpu` 只保证同一设备内有序，`.sys` 覆盖整个系统——包括经 NVLink 连接的其他 GPU。跨 GPU 握手必须用 `.sys`。这些指令自 `sm_70`（Volta）起可用。
- **CUDA graph（图捕获与重放）**：把一段流上的内核启动与拷贝记录成一张图，之后可以整图重放。关键语义：**捕获时不执行，只记录；内核参数在捕获那一刻被固化**。这句朴素的话正是本讲第三节的主角。
- **epoch（纪元/轮次）**：一个单调递增的计数器，每次屏障推进 1。它让「peer 写过没有」变成「peer 写到了第几轮」，使同一块 flag 内存可以被无限次复用而互不混淆。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/barrier.cu](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/barrier.cu) | 屏障的全部实现：两条内联 PTX、自旋内核 `barrier_kernel`、入口 `fast_barrier`。本讲的核心文件。 |
| [include/fast_ulysses/transfer.hpp](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/transfer.hpp) | 屏障与传输的**契约文档**：内存布局要求、epoch 在设备上的理由、`set_ce_fault` 故障注入。 |
| [src/bindings.cc](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc) | `transfer_on_stream`：两次屏障夹住传输的调用点；`epoch_debug` 测试方法的注册处。 |
| [src/group.cc](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc) | 握手区在 signal pad 里的偏移计算（u4-l1 已讲布局，这里只取坐标）；`epoch()` 的读法。 |
| [include/fast_ulysses/group.hpp](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/group.hpp) | `Window` 结构（`flag_ptrs` 字段）与 `epoch()` 的契约注释。 |
| [test/distributed/cudagraph.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/cudagraph.py) | 「捕获后的稳态调用在重放时还正确吗」的测试工作负载，本讲第四节的主要精读对象。 |
| [docs/design.md](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/design.md) | 设计论证：epoch 上设备的动机、捕获边界、memop 弃用的实测数据与归因更正。 |

## 4. 核心概念与源码讲解

### 4.1 屏障的位置与握手区布局

#### 4.1.1 概念说明

u5-l1 讲了传输：`launch_a2a_ce` 把 `CopyOp` 变成 CE 拷贝，直接写进 peer 的窗口。但传输本身不解决两个时序问题：

1. **开写之前**：窗口是单缓冲的（u4-l2），本次调用要覆写窗口，而某个 peer 可能还在**读**上一次的结果——写者必须等读者。
2. **宣布完成之后**：对端怎么知道我写完了？它需要一个所有 rank 都能观察到的「写完了」信号。

这两件事都由屏障完成。每次调用付两道屏障——这是 u2-l4 成本模型里「按次数计费的延迟项」的组成部分，也是基准里 barrier 阶段的实体。

#### 4.1.2 核心流程

一次 sync 调用的设备侧序列（全部排序在调用者流上）：

```text
prepare()（纯主机校验，先于一切屏障 —— u6-l2）
   │
   ├─ ① fast_barrier   开路屏障：写者等读者
   ├─ ② launch_a2a_ce  CE 拷贝写 peer 窗口 + 自份额走本流（u5-l1）
   ├─ ③ fast_barrier   收尾屏障：证明所有人的写都已落地
   └─ ④ copy_out       窗口 → 调用者张量（零拷贝路径没有这一步）
```

握手区的内存布局（每个 rank 一份，在自己的 signal pad 末尾）：

```text
本 rank 的 flag 区域（共 (ws + 1) 个 uint64 槽位）：
┌─────────────────────────────┬─────────┐
│ flags[0]  flags[1] … flags[ws-1] │ epoch   │
└─────────────────────────────┴─────────┘
   ↑ peer t 写进 flags[rank_t]        ↑ 本 rank 的 barrier_kernel 用
     （每个同伴占一格）                  atomicAdd 推进
```

rank \( r \) 的发布动作写的是「peer \( t \) 的 `flags[r]`」；它的等待动作自旋读「自己的 `flags[t]`」。也就是说：**我写的格子别人等，我等的格子别人写**。

#### 4.1.3 源码精读

调用点——两次屏障夹住传输，以及开路屏障守护的是「读」而非上一调用的「写」：

- [src/bindings.cc:198-211](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L198-L211)：`transfer_on_stream` 先发开路屏障（注释明确写着 "WRITERS WAIT FOR READERS"——单缓冲窗口即将被覆写，peer 可能还在读上一个结果；守调用开始而非上一调用结束，是因为零拷贝路径的结果由调用方在算子看不见的时刻读取），然后 `launch_a2a_ce`，再发收尾屏障（注释指向 CE 可见性假设，u9-l3 详述）。

握手区坐标——在 signal pad 的**最后** `(ws+1)` 个槽位，偏移由下式给出：

- [src/group.cc:21-37](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L21-L37)：`flag_slots(ws) = ws + 1`（ws 个 flag 加一个 epoch）；`flag_offset_slots(ws) = pad_slots - (ws+1)`，即从 pad 末尾倒着数。注释给出放在末尾的原因：torch 自带的 `SymmetricMemory::barrier()`（窗口建立时会被调用）**从 pad 前端计数**使用通道，两者绝不能重叠。`TORCH_CHECK(pad_slots >= flag_slots + 8, ...)` 在两端之间强制至少 8 个槽位的余量。

窗口携带的地址表——屏障与传输共用同一张 `peer_ptrs` / `flag_ptrs` 结构：

- [include/fast_ulysses/group.hpp:24-29](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/group.hpp#L24-L29)：`Window` 的 `flag_ptrs[p]` 是「peer p 的握手区，从本 rank 视角寻址」。分配时由 [src/group.cc:235-238](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L235-L238) 从 `get_signal_pad_ptrs()` 加偏移算出。

分配时的清零——epoch 必须从 0 开始：

- [src/group.cc:246-252](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L246-L252)：只清零**自己的**握手区（torch 的通道是 torch 的事），然后 `sym->barrier(0, 0)` 等全员清完——防止某 rank 的首次发布落进另一个 rank 尚未清零的 pad、随后被清零抹掉，导致永久等待。这段的完整论证在 u4-l1，这里只需要记住结论：**epoch 的初值是 0，且这个 0 是集体性建立起来的**。

契约原文——调用方必须提供至少 `(ws + 1) * 8` 字节并在首次调用前清零：

- [include/fast_ulysses/transfer.hpp:37-51](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/transfer.hpp#L37-L51)：`fast_barrier` 的文档注释，一句话概括整个协议：「rank r 用 release 写把它的 epoch 发布进每个 peer 的 `flags[r]`，然后等到自己的 `flags[0..ws-1]` 全部达到该 epoch」。

#### 4.1.4 代码实践

1. **实践目标**：不看讲义，独立推导握手区的字节坐标，并核对 `fast_barrier` 的 `ws == 1` 短路。
2. **操作步骤**：
   - 读 [src/group.cc:21-37](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L21-L37)，设 signal pad 共 \( P \) 个 uint64 槽位、`ws = 8`，写出本 rank flag 区域的字节区间（相对 pad 起点）。
   - 读 [src/barrier.cu:53-57](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/barrier.cu#L53-L57)，回答 `ws == 1` 时屏障做了什么、epoch 会是多少。
   - 解释 `TORCH_CHECK` 里 `+ 8` 这个余量的含义。
3. **需要观察的现象**：三个问题都可在源码里直接核对，不需要 GPU。
4. **预期结果**：
   - flag 区域占最后 \( (8+1) = 9 \) 个槽位共 \( 9 \times 8 = 72 \) 字节，字节区间为 \( [\,8(P-9),\ 8P\,) \)，epoch 在最后一个槽位 \( 8(P-1) \) 处；`TORCH_CHECK` 要求 \( P \ge 17 \)，即 torch 前端通道与我们的末尾区域之间至少隔 8 个槽位——两侧任何一侧的增长都不至于立刻压到另一侧。
   - `ws == 1` 时 `fast_barrier` 直接 `return`，内核根本不启动，epoch 恒为分配时清出的 0（见 [src/barrier.cu:56-57](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/barrier.cu#L56-L57)）。这也呼应 u1-l3 的结论：单卡 world_size=1 是合法一等公民，屏障被整体跳过。

#### 4.1.5 小练习与答案

1. **问：握手区为什么放在 signal pad 的末尾，而不是开头？**
   答：torch 自带的 `SymmetricMemory::barrier()` 从 pad **前端**计数使用通道，而窗口建立（`allocate()` 收尾的 `sym->barrier(0,0)`）必然会调用它；我们的 `flags[ws] + epoch` 若也从前端数，两者会互相覆盖。从末端倒数（`offset = pad_slots - (ws+1)`）让两个使用者各占一端。
2. **问：开路屏障为什么守在「本次调用的开始」而不是「上一次调用的结束」？**
   答：收尾屏障证明的是所有人的**写**已落地，对上一结果的**读**一无所知；而零拷贝路径（`out=` / lend）的结果由调用方在算子完全看不见的时刻读取。守调用开始，意味着「我要覆写窗口了，此刻还在读旧结果的人必须先到齐」。
3. **问：`empty_output()` 会推进 epoch 吗？**
   答：不会。`empty_output` → `make_output` → `allocate` 只做分配、清零自己的握手区和 torch 自己的 `sym->barrier`（走 pad 前端通道）——没有任何一次 `fast_barrier`。epoch 只由 `all_to_all_4d(_out/_staged)` 的两道屏障推进。

### 4.2 release/acquire PTX：自旋内核逐行精读

#### 4.2.1 概念说明

`barrier_kernel` 是一个 **单块、32 线程**的自旋内核。它把发布-等待协议压进一个内核：一部分线程负责向 peer 发布，同一批线程各自自旋等待一个 peer。内存序由两条内联 PTX 承担：

- `red.release.sys.global.max.u64`：**red**uction（只写不返回值）——对目标地址做原子 MAX，带 release 内存序、系统作用域。release 保证：这条指令之前的、本线程的写入，对任何随后以 acquire 读到该值的线程可见。MAX 而非普通 store，使 flag **永不回退**。
- `ld.acquire.sys.global.u64`：带 acquire 内存序、系统作用域的读。acquire 保证：一旦读到发布方写下的值，之后本线程的读都能看到发布方在发布前做的一切。

`.sys` 是关键：屏障跨 GPU，`.gpu` 作用域的保证不覆盖经 NVLink 的远端线程。

#### 4.2.2 核心流程

线程分工（`ws ≥ 2`，因为 `ws == 1` 时内核根本不启动）：

| 线程 | 阶段 1（推进轮次） | 阶段 2（同步） | 阶段 3（发布） | 阶段 4（等待） |
| --- | --- | --- | --- | --- |
| `threadIdx.x == 0` | `atomicAdd` 推进 epoch，写入 `__shared__` | `__syncthreads()` | 发布给 peer 0 并自旋等 peer 0 | （与阶段 3 合并：发布后自旋） |
| `0 < t < ws` | — | `__syncthreads()` | 发布给 peer t 并自旋等 peer t | 同上 |
| `t ≥ ws` | — | `__syncthreads()` | `return`（退出内核） | — |

协议时序（以 rank \( r \)、第 \( e \) 次屏障为例）：

1. 线程 0 读自己的 epoch 槽，原子加一得到新轮次 \( e \)，广播到 shared memory。
2. 线程 \( t < ws \) 把 \( e \) 用 `red.release.sys.max` 写进 **peer \( t \) 的 `flags[r]`**。
3. 线程 \( t \) 自旋 `ld.acquire.sys` 读**自己的 `flags[t]`**，直到 \( v \ge e \)。
4. 所有线程退出，内核结束——屏障完成，流上的后续工作（CE 拷贝或 copy_out）开始。

等待条件写成不等式 \( v \ge e \) 而非 \( v = e \)：配合 MAX 发布的「只增不减」，即使某个 peer 已经公布了**更高**的轮次（契约下不会发生），等待也能安全通过。

#### 4.2.3 源码精读

两条内存序原语——注释同时解释了为什么用 MAX：

- [src/barrier.cu:9-24](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/barrier.cu#L9-L24)：`red_release_sys_max_u64` 与 `ld_acquire_sys_u64`。注释原话：发布是 MAX 而不是 store，所以 flag 永不后退；在窗口调用有序的前提下这不会发生、MAX 不承担正确性——它买到的是：**一个打破调用顺序的调用者，无法把 peer 的 flag 压到该 peer 正在等待的 epoch 之下**。同时注明需要 `sm_70` 或更新。

内核本体——四行关键代码：

- [src/barrier.cu:31-49](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/barrier.cu#L31-L49)：
  - L35-37：`__shared__ uint64_t epoch;` 线程 0 执行 `atomicAdd(local + ws, 1) + 1`——`atomicAdd` 返回**旧值**，加一得到新轮次，写进 shared。`local` 就是本 rank 的 flag 区域基址，`local + ws` 正是 4.1 布局图里的 epoch 槽。
  - L38：`__syncthreads()`，注释 "must precede the early-out: every launched thread has to reach it"——CUDA 要求块内所有线程一致到达 `__syncthreads()`，若让 `t ≥ ws` 的线程先 `return`，其余线程会在一个并非全员到达的屏障上行为未定义。
  - L41-42：`if (t >= ws) return;` 多余的线程在此退出。
  - L43：发布——`peers.p[t]` 是 peer t 的 flag 基址，`+ rank`（uint64 指针运算，单位是 8 字节）落到 peer t 的 `flags[rank]`，写的值是本轮 `epoch`。
  - L44-48：等待——自旋 `ld_acquire_sys_u64(local + t)`（自己的 `flags[t]`，由 peer t 写入），循环条件 `v < epoch`。

入口与启动配置——单块 32 线程，按值传参：

- [src/barrier.cu:53-67](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/barrier.cu#L53-L67)：`fast_barrier` 先做 `ws == 1` 短路；把 `flag_ptrs` 拷进 `BarPeers`（[src/barrier.cu:27-29](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/barrier.cu#L27-L29)——定长 8 个槽，对应算子层强制的 `ws ∈ [1,8]`）；`barrier_kernel<<<1, 32, 0, stream>>>` 在调用方传入的流上启动。注意这个内核**本身要占一个 SM 上的 block**——「零 SM」是远端 CE 拷贝的性质（u5-l1、u5-l2 已建立），屏障不是零 SM 的；但它是一个普通内核，占用只发生在内核边界内（启动 → 自旋 → 退出），调度器对它完全可见——这一点在 4.5 与 memop 对比时再回来。

顺带一条诚实边界：收尾屏障的可见性链条是「peer 的 CE 拷贝 → peer 的 release 发布 → 我的 acquire 读到 → 我读窗口」。PTX 只把 release 的保证范围限定为「**当前线程**之前的操作」，而 CE 拷贝不是线程的操作——这正是 [docs/design.md:130-158](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/design.md#L130-L158) 承认的**未文档化假设**，u9-l3 的 `ce_ordering.py` 是它的负控制测试。本讲不展开。

#### 4.2.4 代码实践

1. **实践目标**：给 `barrier_kernel` 写逐行注释，标出每个线程（`threadIdx.x == 0`、`t < ws`、`t >= ws`）各自执行的阶段。
2. **操作步骤**：打开 [src/barrier.cu:31-49](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/barrier.cu#L31-L49)，在纸上（或自己的笔记里，**不要改源码**）为每一行写出注释，然后与下面的参考版对照。
3. **需要观察的现象**：你能不看任何材料说出「我写的格子别人等、我等的格子别人写」对应的两行代码吗？
4. **预期结果**（参考注释，`ws=4`、`rank=2`、第 7 次屏障为例）：

```cuda
// 以下为「示例注释」，基于 src/barrier.cu:31-49 原文，非项目原有代码
__global__ void barrier_kernel(uint64_t* local, BarPeers peers, int ws, int rank)
{
    __shared__ uint64_t epoch;
    if (threadIdx.x == 0)                    // 仅线程 0：
        epoch = atomicAdd(...(local + ws), 1ULL) + 1;  // 原子推进 epoch：6 -> 7（旧值 6 + 1）
    __syncthreads();                         // 全体 32 线程到齐，epoch 已进 shared

    int t = threadIdx.x;
    if (t >= ws) return;                     // 线程 4..31：只参与 syncthreads，到此退出

    // 线程 0..3（t < ws），各自负责一个 peer：
    red_release_sys_max_u64(...(peers.p[t]) + rank, epoch);  // 我(rank=2)的轮次 7 release 发布
                                                             // 进 peer t 的 flags[2] —— 别人等它
    uint64_t v;
    const uint64_t* mine = local + t;        // 我自己的 flags[t] —— peer t 写它
    do {
        v = ld_acquire_sys_u64(mine);        // acquire 读；读到 7 说明 peer t 也到了第 7 轮
    } while (v < epoch);
}
```

#### 4.2.5 小练习与答案

1. **问：把 `.sys` 改成 `.gpu` 会发生什么？**
   答：`.gpu` 作用域的 release/acquire 只对同一设备上的线程有效。屏障的发布方与等待方分属不同 GPU，靠 NVLink 互见；`.gpu` 下远端更新不在保证范围内，等待方可能永远读不到新值（或读不到有序的值），屏障失去正确性。所以必须 `.sys`。
2. **问：为什么发布用 MAX 而不是普通 store？在契约遵守时它有什么用？**
   答：契约下毫无作用（注释原话 "not load-bearing"）——同一窗口的调用被流排序，flag 天然单调。它是防御：若某调用者打破了每窗口的调用顺序，一个迟到的旧轮次用普通 store 会把 flag 拉回去，可能把 peer 的 flag 压到该 peer 正在等待的 epoch 之下；MAX 让 flag 只增不减。
3. **问：`__syncthreads()` 为什么必须放在 `if (t >= ws) return;` 之前？**
   答：CUDA 要求块内所有线程都到达 `__syncthreads()`。若多余线程先退出，剩余线程在一个「非全员到达」的屏障上是未定义行为（典型表现是挂起）。源码注释明确写了这一点。

### 4.3 设备侧 epoch：由 atomicAdd 推进的单调计数器

#### 4.3.1 概念说明

「epoch 放在哪、由谁推进」看起来是个无关紧要的实现细节，实际是整个屏障对 CUDA graph 正确性的支点。两种方案对比：

| 方案 | 推进者 | 图捕获下的行为 |
| --- | --- | --- |
| 主机侧 epoch：每次调用主机算 `++epoch` 当内核参数传入 | 主机 | **参数在捕获时被烤成常量** \( E \)；每次重放宣布的都是 \( E \)，而 peer 的 flag 已经 \( \ge E \)，自旋立即满足——握手静默变成空操作 |
| 设备侧 epoch：内核体内 `atomicAdd(local + ws, 1)` | 内核 | `atomicAdd` 是图里的一条指令，**每次重放都真实执行**，轮次天然刷新 |

这不是假想的风险。[test/distributed/cudagraph.py:7-14](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/cudagraph.py#L7-L14) 的 docstring 写明：主机侧 epoch 正是「被替换掉的那个版本，它的重放结果损坏」（"it is the version this replaced, and its replays came back corrupt"）。

另一处细节同样是防御性的：为什么用 `atomicAdd` 而不是「读出来、加一、写回去」？注释（[src/barrier.cu:33-34](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/barrier.cu#L33-L34)）说：这样打破每窗口顺序契约的调用者不会** outright 丢掉一个 epoch**。读-改-写方案下，两个并发的旧式内核会读到同一个旧值、写回同一个新值——轮次重复了，两个不同的屏障宣布了同一个 \( e \)，等待方无法区分。`atomicAdd` 保证每次执行恰好加一，轮次永不重复。

#### 4.3.2 核心流程

epoch 的生命周期与不变量：

```text
分配窗口（u4-l1）
  └─ 清零自身握手区 + sym->barrier        ⇒ epoch = 0（集体性地建立）
每次调用（sync 或 async）
  ├─ 开路屏障  ⇒ atomicAdd：epoch ← epoch + 1，发布并等待该值
  ├─ 传输
  └─ 收尾屏障  ⇒ atomicAdd：epoch ← epoch + 1，发布并等待该值
```

- 每次调用推进 2：\[ \Delta\,\text{epoch} = 2 \quad \text{每次调用} \]
- epoch 是 uint64，每调用 +2，实践中永不回绕。
- 等待条件 \( v \ge e \) 与「flag 只增不减」共同保证：等待只能被**本轮或更新轮次**的发布满足，永远不会被陈旧值骗过——前提是轮次本身不重复，这正是 `atomicAdd` 买到的。

#### 4.3.3 源码精读

- [src/barrier.cu:33-38](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/barrier.cu#L33-L38)：`atomicAdd` 推进 epoch 的两行及其防御性注释（"so a caller who breaks the per-window ordering contract cannot lose an epoch outright"）。
- [include/fast_ulysses/transfer.hpp:41-44](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/transfer.hpp#L41-L44)：契约原文——"The epoch lives on the device and is advanced by the kernel, so a captured graph advances it on replay too -- a host-computed epoch would bake a constant into the graph and every replay would be satisfied by stale state."
- [docs/design.md:85-89](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/design.md#L85-L89)：设计论证落笔处，并指向 `test/distributed/cudagraph.py` 作为这条主张的守护测试（八次重放、每次 +2、每次位精确）。

测试如何读它——`epoch_debug`：

- [src/bindings.cc:688-691](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L688-L691)：`epoch_debug` 注册在 torchbind 类上（TESTS ONLY）。注册处的注释说明它存在的原因：数据撕裂只是屏障死掉的**充分**信号，只有 epoch 是**必要**信号——重放恰好对齐时，死屏障的数据检查照样干净通过。
- [src/group.cc:424-442](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L424-L442)：`epoch()` 的读法。地址是 `flag_ptrs[rank] + ws * 8`（epoch 槽在 ws 个 flag 之后），用一次同步的 `cudaMemcpy` D2H 读回。注意 probe 的解析顺序：先查 `owned_`（`empty_output()` 的缓冲**自身就是一个窗口**，零拷贝路径不经过 role 窗口），再按 `(role, dtype)` 查 role 窗口，都不存在返回 -1。

#### 4.3.4 代码实践

1. **实践目标**：用 `epoch_debug` 验证「每次调用 +2」的记账，并预测 `world_size = 1` 的行为。
2. **操作步骤**（示例代码，需已按 u1-l3 装好本库）：

```python
# 示例代码：epoch 记账验证
# ≥2 GPU 时用 torchrun --nproc_per_node=N 启动；单卡用 --nproc_per_node=1 亦可（观察另一半问题）
import torch
from fast_ulysses import UlyssesGroup

group = UlyssesGroup()                       # world_size=1 时屏障整体跳过
x = torch.randn(2, 16, 4 * group.world_size, 128, dtype=torch.bfloat16, device="cuda")
out = group.empty_output(x, mode=0)          # 只分配、清零，不推进 epoch
for i in range(3):
    group.all_to_all_4d(x, mode=0, out=out)
    torch.cuda.synchronize()
    print(i, group._handle.epoch_debug(out, 0))   # role 0 = kSyncWindow
```

3. **需要观察的现象**：打印序列。
4. **预期结果**：`ws ≥ 2` 时打印 `0 2`、`1 4`、`2 6`——每次调用恰好 +2（每道屏障各 +1）。`ws = 1` 时恒打印 0：`fast_barrier` 在 `ws == 1` 处短路，内核从未启动，epoch 停在分配时清出的 0。多卡部分**待本地验证**（本讲义写作环境无 GPU）；单卡部分同样待本地验证，但预测可直接从 [src/barrier.cu:56-57](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/barrier.cu#L56-L57) 推出。

#### 4.3.5 小练习与答案

1. **问：三次 sync 调用后 epoch 是多少？`empty_output` 算不算？**
   答：6。`empty_output` 不算（它不做 `fast_barrier`）；每次 `all_to_all_4d` 推进 2（开路 + 收尾）。`ws = 1` 时是 0。
2. **问：`atomicAdd` 与「读-加-写」在什么场景下有行为差异？为什么本库在契约下仍然选 `atomicAdd`？**
   答：同一窗口两个屏障内核并发时（即调用者打破每窗口顺序契约），读-加-写会让两者读到同一旧值、写回同一新值，丢一次推进、轮次重复；`atomicAdd` 保证每次执行恰好 +1。契约下单窗口的屏障被流排序、不会并发，所以这是纯防御——与 4.2 的 MAX 发布同一思路：违约时的失败模式从「静默损坏」降级为「可被发现的乱序」。
3. **问：`epoch_debug(probe, role)` 为什么要先查 `owned_`？**
   答：`empty_output()` 的缓冲自身就是对称窗口，记在 `owned_` 里；带 `out=` 的零拷贝调用根本不经过 `(role, dtype)` 的 role 窗口。不先查 `owned_`，把这种缓冲当 probe 会错误地返回 role 窗口的 epoch（甚至 -1），答非所问。

### 4.4 CUDA graph 捕获边界与 cudagraph.py

#### 4.4.1 概念说明

CUDA graph 的语义回顾：**捕获时不执行、只记录；内核参数在捕获那一刻固化**。4.3 已论证内核体内的 `atomicAdd` 让图重放天然拿到新轮次——但这只解决了「屏障正确」这一个问题。一次完整的 `all_to_all_4d` 调用里还有别的东西，捕获边界由它们决定：

- **sync 调用，且窗口已存在**：可捕获。捕获区里只有「屏障 → 拷贝 → 屏障」。
- **sync 调用，窗口尚不存在**（某形状的第一次调用）：捕获失败。窗口分配 = `empty_strided_p2p` + `rendezvous` + `zero_()`，三者没有一个是捕获中合法的操作——`rendezvous` 是集合操作，捕获中的流只能记录设备侧工作。
- **async 调用**：完全不可捕获。`stage()` 要让 comm 流等待一个由**上一次、未捕获的**调用记录的事件（staging 池的 ready/release 事件，u7-l2 详述），跨图的事件依赖使捕获无效。

由此得出两条使用纪律（[docs/design.md:100-101](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/design.md#L100-L101)）：**捕获前把每个形状都预热一遍；给图传 `out=`（来自 `empty_output()`）固定输出地址**。

为什么必须 `out=`：捕获区内调用 `at::empty` 会从图的**私有内存池**分配，重放之间该池被复用，逃逸出捕获区的张量可能被后续重放静默覆写。`out=` 让图每次都写同一个由调用者持有、生命周期由调用者控制的地址。输入侧同理：测试里 `x` 在捕获前分配、之后只做原地 `normal_()` 改写（[test/distributed/cudagraph.py:71](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/cudagraph.py#L71) 与 L101）——图读的地址也是固定的。

#### 4.4.2 核心流程

`cudagraph.py` 的结构（它自称 worker，是一个多进程测试工作负载）：

```text
torchrun 启动 ws 个 rank
  ├─ 构造 group、输入 x
  ├─ 预热（捕获区外）：empty_output（分配窗口 + 固定输出地址）→ 一次调用 → synchronize
  ├─ 按 torch 要求在 side stream 上再预热一次
  ├─ 捕获 group.all_to_all_4d(x, mode=0, out=out)   ← 失败则整场测试判为失败退出
  └─ 8 次重放，每次：
       ① x.normal_()                    新数据：陈旧窗口给出「错的答案」而非「重复的正确答案」
       ② want = reference_even(...)     NCCL 参照（集合性，会把各 rank 重新对齐）
       ③ if rank == 0: 24 个 GEMM       压舱物：让 rank 0 慢、其他人跑 ahead —— 制造 skew
       ④ graph.replay(); synchronize
       ⑤ 检查一（充分信号）：torch.equal(out, want) 位精确
       ⑥ 检查二（必要信号）：epoch_debug 采样
  └─ 终局：所有相邻 epoch 采样之差必须恰好等于 2
```

两个检查互补，缺一不可（docstring L25-32）：

- **数据检查是充分非必要**：撕裂需要 peer 真的迟到才咬人；一次 skew 恰好没咬到的重放会干净通过。
- **epoch 检查是必要信号，且必须是相等（equality）**：每次调用两道屏障，重放一次 epoch 恰好 +2；若开路屏障被删掉，每次只 +1——任何「至少推进过」（> 0）形式的检查都会放行，而窗口在整个调用期间无人守卫。

skew（压舱物）是数据检查有意义的前提（docstring L33-39）：所有 rank 齐到的重放会互相重新对齐，死屏障照样通过——「紧基准循环产生的盲区」正是这样来的。所以 rank 0 每次重放前跑 24 个 GEMM，其他人先行；而参照 `reference_even` 本身是集合操作、会把大家重新对齐，所以必须放在压舱物**之前**。删掉 `if rank == 0` 的压舱物后，这个 worker 在屏障被移除的情况下也会通过——这就是它内置的负控制。

#### 4.4.3 源码精读

测试的动机与危害模型：

- [test/distributed/cudagraph.py:1-14](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/cudagraph.py#L1-L14)：「捕获成功证明不了任何事，这正是这个 worker 存在的意义」。危害：主机侧 epoch 是烤进图的常量，重放宣布同一 \( E \)、peer flag 已持有 \( E \)、自旋被陈旧状态满足、握手静默变 no-op——「peer 随后可以在我们读窗口时覆写它」。
- [test/distributed/cudagraph.py:16-23](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/cudagraph.py#L16-L23)：边界的自我声明——「这不是被藏起来的 bug，它们就是边界，docs/design.md 明说了」。

捕获纪律的落地：

- [test/distributed/cudagraph.py:73-77](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/cudagraph.py#L73-L77)：先在捕获区外预热——注释点名这就是「分配窗口（集合）+ 建 plan」，因此捕获区里只剩屏障/拷贝/屏障；`out` 是「每次重放都写的静态地址」。
- [test/distributed/cudagraph.py:81-94](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/cudagraph.py#L81-L94)：torch 要求的 side stream 预热 + 捕获本体；捕获抛异常不是跳过而是记录原因。

重放循环与两类检查：

- [test/distributed/cudagraph.py:99-113](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/cudagraph.py#L99-L113)：新输入 → 参照（在压舱物之前，L102 注释）→ rank 0 的 24 个 GEMM（L104-106）→ 重放 → `torch.equal` 位精确（L109，注意是 `torch.equal` 而非 `allclose`——u2-l4 的 DiT 示例用 allclose 是因为 SDPA 归约顺序，这里没有归约，位精确是合法且更强的断言）→ epoch 采样。
- [test/distributed/cudagraph.py:115-130](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/cudagraph.py#L115-L130)：相等检查及注释——「若每调用的屏障数有变，更新常数；不要放松形式」。
- [test/distributed/cudagraph.py:132-146](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/cudagraph.py#L132-L146)：`all_reduce` 汇总裁决；「捕获失败 = 什么都没检查 = 退出码 1」而非静默通过。

设计文档的对应主张：

- [docs/design.md:91-101](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/design.md#L91-L101)：可捕获面「比该论证本身暗示的更窄」，且边界是**分配**的属性而非握手的属性；最后一句就是两条纪律。

#### 4.4.4 代码实践

1. **实践目标**：吃透「8 次重放 epoch 恰好 +16 且每次位精确」到底证明了什么；写出捕获纪律的两条理由。
2. **操作步骤**：
   - 精读 [test/distributed/cudagraph.py:60-130](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/cudagraph.py#L60-L130)。
   - 有 ≥2 GPU 时运行：`torchrun --nproc_per_node=<N> test/distributed/cudagraph.py`，记录 rank 0 的 `OK ws=... epoch advanced [...]` 行（**待本地验证**；本讲义写作环境无 GPU）。无 GPU 时做纸面推演（下一步）。
   - 纸面推演：预热共发生几次 sync 调用？`epochs[0]` 是多少？8 次重放后最后一个采样值是多少？
   - 回答两个问题（答案见下）。
3. **需要观察的现象**：成功输出的 steps 序列；以及若删掉压舱物，测试对「被移除的屏障」是否仍然敏感。
4. **预期结果**：
   - 预热共 **2 次** sync 调用（L76 一次、L88 side stream 上一次；`empty_output` 与捕获本身不产生屏障），每次 +2，故 `epochs[0] = 4`；捕获不执行；8 次重放各 +2，采样序列为 4, 6, 8, 10, 12, 14, 16, 18, 20，总移动量 \( 2 \times 8 = 16 \)，打印 `epoch advanced [2, 2, 2, 2, 2, 2, 2, 2]`。
   - **问题一（为什么 +16 且位精确能证明重放没用过陈旧状态）**：+2/重放证明每次重放都真实执行了内核体内的 `atomicAdd`——图没有带着烤死的常量跑，两道屏障各自宣布了一个**全新**的轮次并被等待方以该轮次满足（若开路屏障死了，每次只 +1，相等检查立刻抓住）。位精确则是充分信号：在 rank 0 被 24 个 GEMM 拖慢、其他 rank 遥遥领先的局面下，领先的 rank 若不受开路屏障约束，会在 rank 0 仍读取上一轮结果时覆写它的窗口，`torch.equal` 失败。必要信号（epoch 相等）+ 充分信号（skew 下的位精确）合起来，才覆盖「握手活着」的全部方向。
   - **问题二（为什么必须预热所有形状、传 `out=`）**：未预热的形状在捕获区内触发 `empty_strided_p2p + rendezvous + zero_()` 的集合分配——捕获中不合法，捕获直接失败（这失败还被测试判定为「什么都没检查」）。不传 `out=` 则输出从图的私有内存池分配，池在重放间复用，调用者手中的结果张量可能被后续重放静默覆写；`out=` 来自 `empty_output()`，地址固定、由调用者持有生命周期，每次重放写同一块缓冲。

#### 4.4.5 小练习与答案

1. **问：为什么 epoch 检查必须写成「恰好等于 2」，而不是「大于 0」？**
   答：每次调用两道屏障。若开路屏障被删（正是最危险的一侧——它守着「写者等读者」），每次重放 +1，任何「至少推进过」的形式都通过，而窗口在整个调用期间无人守卫。相等形式同时锚定了「两道都在」和「没有多余的屏障」两个事实。
2. **问：`reference_even` 为什么必须放在压舱物之前？**
   答：它是 NCCL 集合操作，会把所有 rank 重新对齐。放在压舱物之后的话，rank 0 的 GEMM 结束前其他 rank 已在参照处等待，skew 被参照本身吃掉，数据检查退化回「齐到的重放」。先参照、后压舱、再重放，才能让其他 rank 在重放处真正跑在 rank 0 前面。
3. **问：捕获失败时测试为什么以退出码 1 结束，而不是算作「跳过」？**
   答：docstring 的原则：「一次没能捕获的运行什么都没检查」("a run that could not capture checked NOTHING")。静默通过会让边界变化（比如某天 sync 路径也变得不可捕获）被当成 PASS 报告出来。

### 4.5 memop 弃用论证：为什么不用 cuStreamWriteValue/WaitValue

#### 4.5.1 概念说明

driver API 提供一组**流内存操作（memop）**：`cuStreamWriteValue64`（往流里插一个「写一个值」的动作）和 `cuStreamWaitValue64`（往流里插一个「等到某地址达到某值」的动作）。直觉上它正是屏障想要的形状：不用启动内核，发布 = WriteValue，等待 = WaitValue，看起来比自旋内核更轻。

本库试过，然后放弃了。理由分三层：

1. **实测回归**：并发计算下，overlap 从 **+34% 掉到 −28%**——这正是本算子存在意义上要避免的回归。
2. **文档化的机制**：CUDA memop 文档说，经这些 API 建立的顺序「对 CUDA 不可见」——一个被阻塞的 WaitValue 对工作调度器不可见，并发内核可以被排在它后面。相比之下自旋内核是普通内核，调度器完全看得见它的占用（且占用只发生在内核边界内）。
3. **硬件属性门槛**（barrier.cu 注释里的次要理由）：等待型 memop 需要一个 remote-write-flush 设备属性，不是所有机器都有。

另有一段**被修正的归因**值得学：早期版本的设计笔记曾把责任归给 `CU_DEVICE_ATTRIBUTE_CAN_FLUSH_REMOTE_WRITES`。后来查实：该属性只 gate `_FLUSH` 变体（为经 PCIe 从 NIC 到达的写存在）；NVSHMEM 的 on-stream wait 确实需要它并用 `FLUSH`，而 NCCL 的 CE wait 两者都不用——所以该属性本身并不构成「NVLink 上不能用等待形态」的理由。**实测回归才是保留至今的理由**，属性论被从笔记里更正掉了。这是一次很好的示范：性能结论要用测量钉住，归因要能被证据推翻。

#### 4.5.2 核心流程

两条候选路径的对比：

| | 自旋内核（现方案） | memop（被否方案） |
| --- | --- | --- |
| 发布 | 内核线程执行 `red.release.sys.max.u64` | `cuStreamWriteValue64` |
| 等待 | 内核线程自旋 `ld.acquire.sys.u64` | `cuStreamWaitValue64` |
| SM 占用 | 一个 32 线程 block，只在内核边界内占用，调度器可见 | 不占内核，但被阻塞的等待**对调度器不可见** |
| 硬件要求 | `sm_70`（PTX 指令） | 等待形态依赖 remote-write-flush 设备属性（不一定存在） |
| 并发计算下实测 | overlap +34% | overlap −28% |
| CUDA graph | 内核体内的 atomicAdd 随重放执行（4.3 的支点） | memop 也可捕获，但见实测 |

顺带一个前瞻：[docs/design.md:150-154](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/design.md#L150-L154) 指出，NCCL 的免内核 CE 集合通信（`ce_coll.cc`、`NCCL_CTA_POLICY_ZERO`）选择了另一形状：**flag 作为 8 字节 D2D `cudaMemcpyAsync` 与载荷同流同引擎**，等待用 `cuStreamWaitValue32`——flag 与数据走同一条引擎路径，绕开了本库依赖的可见性假设。若那个假设哪天必须放弃，这就是迁移方向；代价与为什么不显然更好，在 u9-l5 的设计反思里展开。

#### 4.5.3 源码精读

- [src/barrier.cu:61-64](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/barrier.cu#L61-L64)：弃用理由的第一手注释——"A spin kernel, not stream memops (cuStreamWriteValue64/WaitValue64): the waiting memop needs a remote-write-flush device attribute that is not always present, and measured worse under concurrent compute."；随后两句给出自旋内核的门槛：PTX 只需 `sm_70`，SM 占用只发生在内核边界。
- [docs/design.md:103-108](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/design.md#L103-L108)：实测数字（+34% → −28%）与文档化机制（"is not visible to CUDA" → 被阻塞的 memop 对调度器不可见、并发内核可被排在它后面），并明确「这仍是不用它们的理由」。
- [docs/design.md:110-115](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/design.md#L110-L115)：归因更正段——`CAN_FLUSH_REMOTE_WRITES` 只 gate `_FLUSH` 变体；NVSHMEM 需要它、NCCL 不需要；因此该属性不是 NVLink 上不能用等待形态的理由。结尾一句 "The spin kernel's inline PTX needs only `sm_70`"。

#### 4.5.4 代码实践

1. **实践目标**：把「为什么不用 memop」整理成一段你能向别人复述的论证，并区分其中哪些是测量、哪些是机制、哪些曾被归因错。
2. **操作步骤**：
   - 精读上面三段源码引用。
   - 用不超过五句话写出论证，必须包含：① 实测数字；② 文档化机制；③ 被更正的属性归因。
   - 对照检查：你的论证里，「+34% → −28%」是在什么负载形态下测的？（答案在 [docs/design.md:18-31](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/design.md#L18-L31) 描述的 zerosm 式「并发 GEMM 链」语境里——即 u5-l2 的双臂 A/B 方法。）
3. **需要观察的现象**：无运行成分，纯源码阅读型实践。
4. **预期结果**：一段类似「memop 免内核但实测在并发计算下 overlap 从 +34% 跌到 −28%；CUDA 文档说经 memop 的顺序对 CUDA 不可见，被阻塞的等待对调度器不可见、并发内核可被排在其后；早期归因于 CAN_FLUSH_REMOTE_WRITES 属性是错的——它只 gate `_FLUSH` 变体（NIC 经 PCIe 场景），NVSHMEM 用它而 NCCL 不用；保留至今的理由是实测回归本身。自旋内核的 PTX 只需 sm_70，占用发生在内核边界内且调度器可见。」的论证。

#### 4.5.5 小练习与答案

1. **问：memop 明明省掉了两次内核启动，为什么实测反而更差？**
   答：两个层面。测量层面：并发 GEMM 链下 overlap +34% → −28%。机制层面：CUDA 文档说明经 memop 建立的顺序「对 CUDA 不可见」，被阻塞的 WaitValue 对工作调度器不可见，并发内核可以被排在它后面——传输想守护的计算重叠反而被破坏。自旋内核是普通内核，占用与阻塞对调度器完全可见。
2. **问：为什么设计笔记要撤回对 `CAN_FLUSH_REMOTE_WRITES` 的归因？**
   答：查实该属性只 gate `_FLUSH` 变体，那是为经 PCIe 从 NIC 到达的写准备的；NVSHMEM 的 on-stream wait 需要它并使用 `FLUSH`，NCCL 的 CE wait 两者皆不用。既然 NCCL 能在不用该属性的情况下等待，属性本身就不能解释「等待形态在 NVLink 上不可行」——真正的理由是实测回归。
3. **问：如果未来要迁往 NCCL 式「flag 走同一 CE 引擎 + `cuStreamWaitValue32` 等待」，本讲的哪些结论会受影响？**
   答：`barrier.cu` 的自旋内核与它的 PTX 内存序会被替换；`flag_ptrs` 的握手区布局要为 D2D 拷贝的 flag 重新设计；epoch 的推进位置也要重新论证（写入动作从内核变成 CE 拷贝）。4.4 的捕获边界基本不变（分配属性决定），4.3 的「设备侧推进」原则反而被强化——flag 也在图内由设备侧动作更新。完整清单是 u9-l5 综合实践的任务。

## 5. 综合实践

**任务：给 `cudagraph.py` 做 epoch 全程记账，并写一段判读。**

1. **实践目标**：把本讲四个模块串起来——布局（epoch 槽在哪）、协议（谁推进它）、图语义（捕获固化什么、重放执行什么）、测试判读（必要信号 vs 充分信号）。
2. **操作步骤**：
   - 通读 [test/distributed/cudagraph.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/cudagraph.py) 全文，列出到 `epochs[0]` 采样为止、发生在 `out` 这个窗口上的每一次 `fast_barrier`，给出 `epochs[0]` 的精确值。
   - 推导 8 次重放后的完整采样序列与成功时打印的 steps 列表。
   - 用不超过 200 字回答：「8 次重放 epoch 恰好 +16 且每次位精确」为什么能证明重放没有用过陈旧状态？把答案组织成「必要信号说了什么 + 充分信号说了什么」两句。
   - 写出捕获前必须预热所有形状、必须传 `out=` 固定地址的两条理由，各配一处源码引用（提示：前者看 [test/distributed/cudagraph.py:73-77](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/cudagraph.py#L73-L77) 的注释与 [docs/design.md:94-101](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/design.md#L94-L101)；后者看 [docs/design.md:100-101](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/design.md#L100-L101)）。
   - 有 ≥2 GPU 时实际运行并核对你的记账（**待本地验证**）；无 GPU 时保留纸面推演。
3. **需要观察的现象**：纸面序列与实际打印是否一致；若不一致，回溯是哪一次调用少算了屏障。
4. **预期结果**：`epochs[0] = 4`（L76 与 L88 两次预热调用 × 每调用 2 道屏障；`empty_output` 与捕获本身不产生屏障），采样序列 4, 6, …, 20，steps 为 `[2] * 8`，总移动 +16。判读要点：必要信号（epoch 相等）证明两道屏障每次重放都真实执行了设备侧 atomicAdd 并以新轮次等待——图没有用烤死的常量；充分信号（skew 下的位精确）证明开路屏障真的拦住了跑在前面的 rank——两者合起来才是「重放没有用过陈旧状态」的完整证明。两条纪律：未预热的形状会把集合分配（`empty_strided_p2p + rendezvous + zero_()`）带进捕获区，捕获直接失败；不传 `out=` 则输出落在图的私有内存池，重放间被复用，调用者手中的结果可能被静默覆写。

## 6. 本讲小结

- 屏障是**单块 32 线程的自旋内核**：线程 0 用 `atomicAdd` 推进设备上的 epoch，线程 \( t < ws \) 用 `red.release.sys.max.u64` 把轮次发布进 peer t 的 `flags[rank]`、再用 `ld.acquire.sys.u64` 自旋等自己的 `flags[t]` 达到该轮次——「我写的格子别人等，我等的格子别人写」。
- MAX 发布与 atomicAdd 推进在契约下都不承担正确性，它们是**防御**：违约调用者最多造成可被发现的乱序，而不是 flag 回退或轮次重复导致的静默损坏。
- epoch 放在**设备上**是 CUDA graph 正确性的支点：内核体内的 `atomicAdd` 随每次重放真实执行；主机侧 epoch 会被捕获烤成常量、重放被陈旧状态满足——那曾是真实发生过损坏的旧版本。
- 捕获边界由**分配**属性决定：sync 调用且窗口已存在才可捕获；async 因 staging 的跨图事件依赖完全出局。纪律是预热所有形状 + `out=` 固定地址。
- `cudagraph.py` 用两个互补检查守住主张：epoch **恰好** +2/重放是必要信号（相等形式连「删掉开路屏障只剩 +1」都抓得住），skew 下的位精确是充分信号；压舱物是数据检查有意义的前提，也是内置的负控制。
- memop（`cuStreamWriteValue/WaitValue`）被弃用的最终理由是**实测回归**（并发计算下 overlap +34% → −28%）与「被阻塞的 memop 对调度器不可见」的文档化机制；早期的 `CAN_FLUSH_REMOTE_WRITES` 归因已被笔记自己更正。

## 7. 下一步学习建议

- **u6 单元（torch 算子集成）**：本讲的屏障是 `transfer_on_stream` 的中段；u6-l2 会把 `prepare() → 屏障 → 传输 → 屏障 → copy_out` 整条流水线与「全部校验先于第一个屏障」的原则完整串起来。
- **u7-l2（暂存池）**：本讲说 async 不可捕获的原因是「stage() 等待前一次未捕获调用记录的事件」——u7-l2 精读 staging 池后这句边界就完全落地。
- **u9-l3（对抗性测试）**：本讲反复出现的「负控制」「必要/充分信号」方法论在 `ce_ordering.py`（CE 可见性假设）与 `window_race.py`（窗口读写竞态）中还有两次更深的展开。
- **u9-l5（设计反思）**：若想认真评估「迁往 NCCL 形状（flag 走同一 CE 引擎 + `cuStreamWaitValue32`）」，需要综合本讲的 memop 实测、[docs/design.md](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/design.md) 的假设清单与 u4-l1 的握手区布局，写一份改动面清单。
- 直接阅读顺序建议：[src/barrier.cu](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/barrier.cu)（69 行，一小时可逐行读完）→ [test/distributed/cudagraph.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/cudagraph.py) 的 docstring → [docs/design.md](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/design.md) 的 "The barrier" 一节。
