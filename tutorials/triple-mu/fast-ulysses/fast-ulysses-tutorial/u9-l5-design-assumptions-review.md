# 设计反思：未文档化的假设与被删掉的方案

## 1. 本讲目标

这是学习手册的收官之讲。前面八讲我们把 fast-ulysses 的每一层都拆开看过——plan、窗口、传输、屏障、算子注册、异步路径、构建与测试。本讲换一个视角：**不再问「它是怎么工作的」，而问「它站在什么之上、它放弃了什么」**。

学完后你应该能够：

1. 一字不差地陈述本项目赖以生存的核心假设——「一次已完成的拷贝引擎（CE）写入，会在随后 announce 它的内核 release store 到达之前，对目的地可见」——并说出支持「没有任何厂商文档保证它」这一判断的四条具体理由。
2. 复述 NCCL 如何用「换形状」规避这条假设，以及 design.md 论证过的、为什么本库不照搬的两条实测代价。
3. 复述拒绝 PCIe 的实测依据：两个节点 0.62× 与 0.14×、相差 5 倍且不做平均；瓶颈不在 fabric 而在跨步拷贝模式对 fabric 的抽取率。
4. 对 design.md「Removed, and why」清单里的每个被删特性，各用一句话说出删除理由，并把它们与 migration.md 的「本项目没有的东西」对上号。
5. 写出一篇有理有据的设计评审笔记：如果要把屏障换成 NCCL 形状，要动哪些文件、付出什么代价、留下什么没回答的问题。

## 2. 前置知识

本讲几乎不引入新机制，但要求你带着前几讲的结论来读：

- **CE（copy engine，拷贝引擎）**：GPU 上独立于流式多核（SM）的 DMA 拷贝单元。远端 peer 拷贝走 CE、不占 SM（u5-l1、u5-l2）。
- **release/acquire 内存序**：`red.release.sys` 与 `ld.acquire.sys` 这对 PTX 指令构成发布-等待协议——release 之前的写对拿到对应 acquire 读的线程可见（u5-l3）。
- **两道屏障**：每次调用是「开路屏障 → CE 传输 → 收尾屏障」三段式。开路屏障让写者等读者（单缓冲窗口保护），收尾屏障向全组宣布「我写的字节已落地」（u6-l2）。
- **设备侧 epoch**：轮次计数由 `atomicAdd` 在内核里推进，而不是主机算好传进去——为了让 CUDA graph 重放不满足于陈旧状态（u5-l3）。
- **负控制（negative control）**：故意注入故障、确认测试还能失败，否则测试「变盲」——证明不了任何东西（u9-l3）。
- **SPMD 契约**：所有 rank 发出相同的调用序列，违反的后果是挂起而非报错（u1-l2、u4-l2）。

一个值得先建立的直觉：**「正确」有两层**。一层是「在我测过的机器上每次都对」；另一层是「有文档或规范背书，换一台机器、换一个驱动版本仍然对」。fast-ulysses 的作者在 design.md 里极其诚实地区分了这两层，并把第一层里最危险的一条单独拎出来写成一章——这就是本讲 4.1 的主角。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [docs/design.md](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/design.md) | 本讲的主文本：「What this rests on that is not documented」「Why NVLink only」「Removed, and why」三章 |
| [docs/migration.md](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/migration.md) | 从 DeepSpeed / sglang / yunchang 迁移的对照表，以及「What this does not have」清单 |
| [src/barrier.cu](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/barrier.cu) | 自旋屏障内核：release/acquire PTX、设备侧 epoch、`BarPeers::p[8]` |
| [src/transfer.cu](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/transfer.cu) | CE 传输路径，以及测试专用故障注入 `set_ce_fault` |
| [include/fast_ulysses/transfer.hpp](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/transfer.hpp) | 屏障与故障注入的契约注释（握手区布局写在这里） |
| [src/group.cc](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc) | 握手区在 signal pad 末尾的布局计算（评审「换形状」时要动的第四处） |
| [src/nvlink.cc](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/nvlink.cc) | `check_nvlink`：fail-open 语义与拒绝始末 |
| [python/fast_ulysses/group.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py) | `_require_nvlink`：设备号 all_gather 后调用 C++ 检查 |
| [test/distributed/ce_ordering.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/ce_ordering.py) | 假设的实证测试 + 每次运行都武装的负控制 |
| [docs/benchmark.md](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/benchmark.md) | "Why not PCIe" 一章的实测数字；被否决替代方案表 |

## 4. 核心概念与源码讲解

### 4.1 未文档化的假设：CE 写入何时对目的地可见

#### 4.1.1 概念说明

整个 fast-ulysses 只依赖一条没有任何厂商文档保证的性质。design.md 把它写成一句加粗的话：

> **A completed copy-engine write is visible at the destination by the time a later kernel's release store announcing it arrives.**
> （一次已完成的拷贝引擎写入，在随后某个内核为它发布的那条 release store 到达目的地之前，就已经对目的地可见。）

它为什么是命门？看一次调用的时序：rank $r$ 用 `cudaMemcpy2D/3DAsync` 把字节写进各 peer 的窗口（CE 干的），然后启动收尾屏障内核，内核里的 `red.release.sys` 向每个 peer 的 `flags[r]` 发布本轮 epoch；peer $t$ 的内核用 `ld.acquire.sys` 自旋等自己的 `flags[r]$ 达到该 epoch，等到了就去读窗口。release/acquire 这一对（PTX 有定义）保证了「release 之前的写」对「拿到同步的 acquire 读」可见。**问题是：CE 写入是不是「release 之前的写」？** PTX 对 `.release` 的作用域定义是「当前线程的先前操作」——而 CE 传输不是任何线程的操作，它是主机通过流提交的 DMA。于是链条里有一环是空的，而整个设计踩在这一环上。

注意边界：这条假设**只关乎收尾屏障**。开路屏障（写者等读者）保护的是单缓冲窗口，不涉及「CE 写是否落地」；出问题的方向只有一个——发布者宣布了、字节还在路上。也注意它是**可见性**假设而不是序假设：流序保证内核在拷贝之后启动（这是主机侧语义），悬而未决的只是「内核启动时，目的地能不能读到那些字节」。

#### 4.1.2 核心流程

把一次收尾握手里各事件的依赖画出来，标注每一环的依据：

\[
\underbrace{W_{r\to t}}_{\text{CE 写入}} \;\prec_{\text{stream}}\; \underbrace{K_r}_{\text{收尾内核}} \;\prec_{\text{release.sys}}\; \underbrace{F_t[r]=e}_{\text{flag 发布}} \;\prec_{\text{acquire.sys}}\; \underbrace{K_t\ \text{自旋退出}}_{\text{读到 } e} \;\prec\; \underbrace{R_{t}}_{\text{读窗口}}
\]

- $\prec_{\text{stream}}$：流序说内核在拷贝「完成」之后才启动——但 CUDA API 对 memcpy「完成」的定义是**主机侧性质**（何时向主机报告完成），不是「对目的设备可见」。
- $\prec_{\text{release.sys}} / \prec_{\text{acquire.sys}}$：PTX 有定义，但 release 只序「当前线程的先前操作」，CE 传输不在内。
- 于是 $W_{r\to t} \Rightarrow R_t$ 这条端到端可见性，四份权威文档全部缺席：

| # | 文档缺位 | 原文依据（design.md「What this rests on that is not documented」） |
| --- | --- | --- |
| 1 | CUDA API Reference | memcpy 的「完成」被定义为主机侧性质，不含目的地可见性 |
| 2 | CUDA Programming Guide | 跨设备序保证只覆盖 NULL 流，且对非默认流上的异步拷贝明确撤回 |
| 3 | PTX | `.release` 的作用域是「当前线程的先前操作」，CE 传输不是线程的操作 |
| 4 | 全体厂商文档 | 没有任何一方对这条序给出任一方向的文档 |

作者同时给出了业界对照，用来校准「该多担心」：

| 实现者 | 形状 | 序强度 |
| --- | --- | --- |
| **NVSHMEM** | on-stream `signal_op`：CE 送 payload，然后由 `<<<1,1>>>` 内核裸 `atomicAdd_system` 发 flag，**无任何 fence** | 比本库**更弱** |
| **TransformerEngine** | userbuffers 的 `ring_exchange`（出货默认）同样形状 | 比本库**更弱** |
| **本库** | 收尾屏障用 `red.release.sys` 发布 | 至少带了 release |
| **NCCL** | kernel-less CE 集合通信（2.28+）：flag 作为 8 字节 D2D 拷贝与 payload 同流同引擎，`cuStreamWaitValue32` 等待 | **换形状规避**（见 4.2） |

最后是证据与保证的区分：「It holds in testing, which is evidence and not a guarantee」——`ce_ordering.py` 每次运行都武装负控制，让这条实证不会悄悄变成不测试任何东西的空转。

#### 4.1.3 源码精读

假设的正式陈述与四条缺位，在 design.md 的专章里：

- [docs/design.md:L130-L141](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/design.md#L130-L141) — 章节标题即为「What this rests on that is not documented」（本项目所依赖而无人文档化的东西），加粗句是假设本体，下面四条 bullet 逐条列出 API Reference、Programming Guide、PTX、厂商文档的缺位。
- [docs/design.md:L142-L148](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/design.md#L142-L148) — 业界对照：NVSHMEM 的 `signal_op_kernel` 与 TransformerEngine 的 `ring_exchange` 都以更弱的序发布，并以「Ours at least publishes with `red.release.sys`」收束。
- [docs/design.md:L156-L158](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/design.md#L156-L158) — 「测试成立是证据而非保证」，并指向负控制。

屏障内核里承担「发布」这一环的两条 PTX（它们的正确性**有** PTX 背书；没有背书的是它们与 CE 写的关系）：

- [src/barrier.cu:L9-L24](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/barrier.cu#L9-L24) — `red_release_sys_max_u64`（系统域 release 的 MAX 发布）与 `ld_acquire_sys_u64`（系统域 acquire 自旋读）。注释说明 MAX 是防御：调用序被破坏时 flag 不会倒退，但「窗口调用有序」时 MAX 并不承担正确性。
- [src/barrier.cu:L31-L49](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/barrier.cu#L31-L49) — `barrier_kernel` 全体：线程 0 `atomicAdd` 推进设备侧 epoch，`t < ws` 的线程先向 peer `t` 的 `flags[rank]` 发布、再自旋等自己的 `flags[t]`。这就是 4.1.2 流程图里的 $K_r$ 与 $K_t$。

契约层把假设写进了头文件——这是「假设显式化」的工程手段：

- [include/fast_ulysses/transfer.hpp:L53-L59](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/transfer.hpp#L53-L59) — `set_ce_fault` 的文档注释直陈：「算子假设已完成的 CE 写在随后内核的 release store 到达前对目的地可见，无厂商文档保证；武装它使测试获得负控制」。
- [src/transfer.cu:L56-L66](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/transfer.cu#L56-L66) — 实现侧同一论断：负控制的价值「等于」它测的假设本身的价值。
- [src/transfer.cu:L98-L124](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/transfer.cu#L98-L124) — 故障的具体注入点：`cudaLaunchHostFunc` 让 payload 在 xfer 流上睡 `delay_us`，并且**跳过把 caller 流 join 回 xfer 完成事件的那一行**——于是收尾屏障在字节还在飞的时候就发布了。除此之外调用的一切都不变，这是一个外科手术式的负控制。

实证测试与它的判读（u9-l3 已精读过对抗性设计，这里只看它与本假设的对应）：

- [test/distributed/ce_ordering.py:L1-L18](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/ce_ordering.py#L1-L18) — docstring 开篇即问「flag 到达时 CE payload 可见吗」，随后把四条文档缺位复述成自己的话，并给出业界对照。
- [test/distributed/ce_ordering.py:L111-L117](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/ce_ordering.py#L111-L117) — 两阶段执行：先武装 `_C._set_ce_fault(2000µs)` 跑一遍（读者**必须**读到陈旧字节），再解除武装跑干净遍（必须一个陈旧字节都没有）。
- [test/distributed/ce_ordering.py:L123-L137](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/ce_ordering.py#L123-L137) — 三种判决：`BLIND`（负控制没撕裂，仪器失明）、`FAIL`（干净遍读到陈旧字节）、`PASS`。`armed > 0 and torn == 0` 才算过——把「测试还能失败」写进断言。

#### 4.1.4 代码实践

**实践目标**：不借助 design.md 的措辞，从源码里独立重建这条假设的边界——训练「从代码里读出隐式契约」的能力。

**操作步骤**：

1. 在仓库根目录执行下面的检索，找出所有承载这条假设的注释点（预期命中 `src/transfer.cu`、`include/fast_ulysses/transfer.hpp`、`docs/design.md`、`test/distributed/ce_ordering.py`）：
   ```bash
   grep -rn "release store" src/ include/ test/ | grep -v Binary
   grep -rn "undocumented\|no vendor" src/ include/
   ```
2. 打开 [src/transfer.cu:L68-L126](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/transfer.cu#L68-L126) 的 `launch_a2a_ce`，在纸上回答：正常路径（`g_fault_delay_us == 0`）里，是谁、在哪一行、以什么方式保证「caller 流上的收尾屏障内核启动时，xfer 流上的 peer 拷贝已完成」？（提示：`cudaStreamWaitEvent(stream, done, 0)`。）
3. 追问并写下答案：这一行给出的「完成」，是 CUDA API 意义上的完成（主机侧语义），还是「对目的 GPU 可读」？去 [docs/design.md:L135-L140](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/design.md#L135-L140) 核对你写的答案与作者的 四条缺位 是否逐条对得上。
4. 用你自己的话写一段不超过 80 字的假设陈述，放在笔记里——综合实践（第 5 节）会用到它。

**需要观察的现象**：检索结果集中在四个文件、`launch_a2a_ce` 的 join 行与故障行只差一个布尔分支。

**预期结果**：你能指出「正常路径的 join 事件 + 收尾内核的 release」两者拼起来仍然缺一环——那一环就是 4.1.1 的假设。本实践为纯源码阅读，无需 GPU，**待本地验证**的只有检索命中的具体行集。

#### 4.1.5 小练习与答案

**练习 1**：假设把收尾屏障的发布指令从 `red.release.sys` 降级为普通 `st.global`（无内存序），这条假设的表述会怎么变？

**答案**：业界对照表里的 NVSHMEM / TransformerEngine 一列就是答案——它们正是这么做的（裸 `atomicAdd_system`、无 fence）。假设本身（CE 写 vs 发布的序）不变，但**第二条防线**没了：原本 release/acquire 这一对是 PTX 保证的，降级后连「SM 内核自己的普通写」与读者之间的序也只剩侥幸。本库「至少带了 release」的全部含义就在这一格。

**练习 2**：为什么负控制要选择「扣住 payload + 跳过 join」，而不是「让收尾内核提早上」？

**答案**：见 [src/transfer.cu:L120-L124](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/transfer.cu#L120-L124) 的注释——跳过 join 后「caller 流在等到远端拷贝之前就到达收尾屏障并发布，调用的其余一切都不变」。若改成提早上内核，测试测的就变成「流调度谁先谁低」而不是「发布与 CE 落地的序」；负控制必须精确打击被测性质，否则武装了也只是制造噪声。

**练习 3**：`ce_ordering.py` 的判决为什么要求 `armed > 0`（武装遍必须撕裂）而不是「武装遍撕裂越多越好」？

**答案**：[test/distributed/ce_ordering.py:L123-L137](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/ce_ordering.py#L123-L137)：`armed > 0` 只证明「故障到达了被测路径」，是**仪器有效性**检查，不是性能指标；干净遍 `torn == 0` 才是性质本身。撕裂次数由 skew 与时序决定，量纲上不承载信息。

### 4.2 NCCL 的替代形状：让 flag 与数据同乘一台引擎

#### 4.2.1 概念说明

面对同一条假设，业界有两条路：**接受它并加强发布**（本库、NVSHMEM、TransformerEngine——只是加强的程度不同），或者**换一个形状让它不再被需要**。NCCL 2.28+ 的 kernel-less CE 集合通信（`ce_coll.cc`、`NCCL_CTA_POLICY_ZERO`）选了后者：flag 不是由 SM 内核发布的，而是**一次 8 字节的 device-to-device `cudaMemcpyAsync`，与 payload 同流、同引擎**，随后用 `cuStreamWaitValue32` 在流上等待。于是「CE 写 → 内核 release」这条无人担保的边被替换成「同一台 CE 上同流的两次拷贝」——flag 与数据走同一条路，先后到达由引擎内部的流序兜住，假设从「跨引擎跨执行单元的可见性」退化为「一台引擎对自己的队列的序」，后者即便同样没有正式文档，风险面也小得多。

design.md 的态度很克制：**「That is the shape to move to if this assumption ever has to go」**——这是假设破产时的逃生方向，不是现在的选择。为什么不现在就搬？因为作者实测过这条形状的两个构件，两个都更慢。

#### 4.2.2 核心流程

把「NCCL 形状的收尾握手」与现状并排：

```text
现状（barrier kernel）：
  xfer 流:  [CE payload 拷贝...] → (join 事件) → caller 流: [K_close: red.release.sys × ws] → [K_open/spin]
  发布者 = SM 内核（1 次启动，4–6 µs）
  等待者 = SM 内核（自旋）

NCCL 形状：
  同一条流: [CE payload 拷贝...] → [CE 8B flag D2D 拷贝 × ws] → cuStreamWaitValue32(每个 peer 的 flag)
  发布者 = CE（7 次 8B peer 拷贝 ≈ 16 µs，ws=8）
  等待者 = 流 memop（对调度器不可见）
```

两条实测代价（design.md「The barrier」章 + benchmark.md 被否决方案表）：

1. **发布侧更慢**：把收尾 flag 改成传输流上的 CE 写（NCCL 的 kernel-less 形状），实测比它要替换的屏障内核**慢 3×**——ws=8 时 7 次 8 字节 peer 拷贝约 16 µs，对内核的 4–6 µs；而且它只挪走了「发布」，等待仍在内核里。
2. **等待侧在并发计算下更差**：`cuStreamWriteValue64/WaitValue64` 实测把并发 GEMM 下的重叠从 **+34% 拉到 −28%**——恰是本算子存在的理由的反面。CUDA memop 文档给出了机制：经这些 API 建立的序「对 CUDA 不可见」，被阻塞的 memop 在工作调度器眼里不存在，并发内核会被排在它后面。

另有一段**自我修正**值得细读：早期版本的 design.md 曾把回归归咎于 `CU_DEVICE_ATTRIBUTE_CAN_FLUSH_REMOTE_WRITES`，后来查实该属性只门控 `_FLUSH` 变体（那是给 NIC 经 PCIe 到达的写准备的）；NVSHMEM 的 on-stream 等待确实需要它并使用 `FLUSH`，而 **NCCL 的 CE 等待两者都不用**——所以该属性本身并不构成「等待形式在 NVLink 上不可用」的理由，**实测回归才是**。

#### 4.2.3 源码精读

- [docs/design.md:L150-L154](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/design.md#L150-L154) — NCCL 形状的完整描述与定位：「这是假设不得不放弃时的去处；其代价、以及它为何并非显然更好，见下文屏障一节」。
- [docs/design.md:L103-L108](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/design.md#L103-L108) — memop 的实测回归（+34% → −28%）与机制解释（「序对 CUDA 不可见」→ 被阻塞的 memop 对调度器隐形）。
- [docs/design.md:L110-L113](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/design.md#L110-L113) — 对 `CAN_FLUSH_REMOTE_WRITES` 的自我修正段：属性只门控 `_FLUSH` 变体，NCCL 的 CE 等待既不需要属性也不用 `FLUSH`，故属性不是等待形式不可用的理由。
- [docs/benchmark.md:L319-L320](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/benchmark.md#L319-L320) — 被否决方案表的两行：memop 一行（回归数字）与「收尾 flag 走传输流 CE 写（NCCL 的 kernel-less 形状）」一行（3× 慢，7×8B ≈ 16 µs vs 4–6 µs，且只挪发布）。
- [src/barrier.cu:L60-L65](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/barrier.cu#L60-L65) — 源码侧留存的选择理由注释（自旋内核而非 memop：等待 memop 需要的属性并非处处存在，且并发计算下实测更差；本内核 PTX 只要 sm_70）。对照上条可读出 design.md 的修正比注释走得更远——**决定性理由是实测**，属性论已被收窄。
- [src/barrier.cu:L115 注释对应的实现](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/barrier.cu#L53-L67) — `fast_barrier`：一次 `<<<1, 32>>>` 启动同时服务发布与等待，这是 4–6 µs 数字的来源；换形状后这一次启动要拆成 7 次引擎拷贝加一串 memop。

#### 4.2.4 代码实践

**实践目标**：把「换形状」从一句话变成一份可执行的改动清单——为第 5 节的综合评审笔记备料。

**操作步骤**：

1. 通读 [src/barrier.cu](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/barrier.cu)（全文 70 行）与 [include/fast_ulysses/transfer.hpp:L37-L51](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/transfer.hpp#L37-L51) 的契约注释，列出 `fast_barrier` 当前同时承担的**四个**职责：推进 epoch、发布 flag、自旋等待、（隐式地）在调用流上占一个排序点。
2. 对每个职责写一行「NCCL 形状下由谁接手」：epoch 推进→？（注意 `cuStreamWriteValue32` 的写入值是**主机参数**，图捕获时会固化为常量——这与 [src/barrier.cu:L33-L37](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/barrier.cu#L33-L37) 的设备侧 `atomicAdd`、以及 design.md 的 CUDA graph 论证如何相容？）；发布→xfer 流上的 8B D2D 拷贝；等待→`cuStreamWaitValue32`；流排序点→memop 本身。
3. 把「不确定能否接手」的职责单独标出，注明理由。这一步**不要求得出结论**，要求得出问题。

**需要观察的现象**：四个职责里至少有一个（epoch 推进）在换形状后没有明显的接手者。

**预期结果**：一张 4 行两列的职责交接表，其中 epoch 行标着「未解决：写值/比较值固化进图 vs 设备侧推进」。纯纸面推演，无需 GPU，结论**待本地验证**仅限于你对其余三行交接者的判断。

#### 4.2.5 小练习与答案

**练习 1**：NCCL 形状为什么「规避」而不是「加强」了假设？

**答案**：现状的问题是 CE 写与 SM 内核 release 之间没有文档化的序（跨执行单元、跨引擎）。NCCL 把发布也变成 CE 拷贝且与 payload 同流，需要担保的序变成「同一引擎同流上先后两次拷贝的完成顺序」——不再跨执行单元，不再需要 release 语义牵线。假设没有被证明，而是被**从依赖图里删掉了**。

**练习 2**：design.md 说 NCCL 形状「not obviously better」，两条实测依据分别是什么？各自测的是什么构件？

**答案**：其一（[docs/benchmark.md:L320](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/benchmark.md#L320)）测**发布侧**：收尾 flag 走传输流 CE 写，3× 慢于屏障内核（16 µs vs 4–6 µs），且只挪走发布、等待仍在内核；其二（[docs/design.md:L103-L108](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/design.md#L103-L108)）测**等待侧**：`cuStreamWaitValue` 一族在并发 GEMM 下把重叠从 +34% 拖到 −28%，机制是 memop 的序「对 CUDA 不可见」、阻塞的 memop 对调度器隐形。

**练习 3**：如果 `CAN_FLUSH_REMOTE_WRITES` 在某机器上为假，本库的自旋内核还能用吗？NCCL 的 CE 等待呢？

**答案**：本库能——`red.release.sys`/`ld.acquire.sys` 不经过该属性（[src/barrier.cu:L9-L24](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/barrier.cu#L9-L24) 只要求 sm_70）。NCCL 的 CE 等待按 design.md 的修正「属性与 `FLUSH` 都不用」，所以也不受它门控；真正需要该属性的是 NVSHMEM 那种用 `_FLUSH` 变体的 on-stream 等待。这道题检验你是否读懂了那段自我修正：**属性不再是理由，实测才是**。

### 4.3 为何拒绝 PCIe：不稳定比慢更致命

#### 4.3.1 概念说明

「为什么只有 NVLink」不是一个偏好问题，而是一个实测结论加一个工程决策：

- **实测结论**：在 PCIe 上本算子**正确但更慢**，且慢的倍数不稳定——跨 CPU socket 时，一台 PCIe 节点上跑出基线的 0.62×，另一台 0.14×，两台相差 5 倍，因此**不做平均**。
- **工程决策**：要在这拓扑上追平 NCCL，需要一条「共享主机内存传输 + 第二次握手」——那是一个**新传输层**，不是调度改动，且在 NVLink 上一文不值。既然不建，就**让构造函数拒绝这个拓扑，而不是假装支持**。

关键的分析转折是「fabric 不是差异所在」：两台节点上平坦的 peer 拷贝都能到同样的带宽（单流 54.6 / 55.9 GB/s，八路并发每流 52.1 GB/s），差异在于**带步长（pitched）的拷贝模式能从 fabric 里抽取多少**——一台 11.2 GB/s、一台 2.1 GB/s，即同一上限的 20% 与 4%，而 NVLink 机器上是 90–99%。也就是说，本库「重排即步长」的核心换法（u1-l1、u3-l2）在 PCIe 交换机的布线方式面前失效了，且失效程度取决于 socket 之下 PCIe switch 的具体布局（快的那台 2+2 成对，慢的那台 3+1）。传输段本身成了慢的那一段——与 NVLink 上的情形正好相反，「步长换重排」的交易在那里不再划算。

#### 4.3.2 核心流程

拒绝链路（谁在哪儿拒绝）：

```text
UlyssesGroup(require_nvlink=True)
  └─ python/fast_ulysses/group.py: _require_nvlink
       ├─ all_gather 各 rank 的设备号          ← 保证全组结论一致（拒绝必须集体发生）
       ├─ 两 rank 共占一 GPU → 跳过检查         ← 不是本检查关心的拓扑
       └─ _C.check_nvlink(devices)
            └─ src/nvlink.cc: check_nvlink
                 ├─ nvlink_matrix 返回 nullopt → 返回空串（fail-open：说不出就放行）
                 └─ 矩阵确有 false 对 → 返回拒绝消息 → Python raise RuntimeError
```

benchmark.md 还记录了这条拒绝链的实机验证：在一台全无 NVLink 的机器上，默认构造抛出 `cuda:0 and cuda:1 are not joined by NVLink`，`doctor` 打印全 `N` 矩阵，而同一台机器 `pytest test/` 51/51 通过（测试显式关掉检查构造组）——**拒绝生效，且不影响其余正确性**。

#### 4.3.3 源码精读

- [docs/design.md:L160-L171](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/design.md#L160-L171) — 「Why NVLink only」：0.62×/0.14×、两节点 5× 分歧不做平均；「fabric 不是差异所在」——平坦 peer 拷贝两节点同带宽，差异在跨步模式抽取率（20% 与 4% vs NVLink 的 90–99%），且随 socket 下的 switch 布局退化。
- [docs/design.md:L173-L175](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/design.md#L173-L175) — 不追平的理由：追平 NCCL 需要共享主机内存传输加第二握手，是新传输而非调度改动，在 NVLink 上一文不值；「不建，所以构造函数拒绝拓扑而不是假装」。
- [docs/benchmark.md:L264-L283](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/benchmark.md#L264-L283) — "Why not PCIe" 一章：拒绝生效的实机记录（raise + doctor 全 N + pytest 51/51）与两节点成本表（A 节点 0.62×，B 节点 0.14×）。
- [docs/benchmark.md:L285-L298](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/benchmark.md#L285-L298) — 平坦拷贝带宽两节点一致（54.6/55.9/52.1 GB/s）、跨步抽取 11.2 vs 2.1 GB/s、switch 布局差异（2+2 vs 3+1），以及对 v0.1 结论的修订：「两种模式都是真的」，v0.1 的「跨步从不慢于平坦」在这台机器上被 25× 的差距推翻。
- [src/nvlink.cc:L201-L220](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/nvlink.cc#L201-L220) — `check_nvlink`：`nullopt` 返回空串（fail-open，L204-L206）；确有 false 对时拼出拒绝消息——消息本身就在教育用户：「本库直接写 peer 内存，走 PCIe（尤其跨 socket）比 torch.distributed 慢……请对这个组用 torch.distributed，或传 `require_nvlink=False` 以测量；`fast-ulysses doctor` 打印完整矩阵」。
- [python/fast_ulysses/group.py:L101-L109](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L101-L109) — Python 侧半段：all_gather 设备号、重复设备即跳过（一 GPU 多 rank 不是本检查的对象）、非空 `problem` 即 raise。

#### 4.3.4 代码实践

**实践目标**：把「不稳定的慢」从一句话变成你自己核对过的三组数字。

**操作步骤**：

1. 只读 [docs/benchmark.md:L277-L298](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/benchmark.md#L277-L298)，抄下三组数：① 两节点的 `vs BASE`（0.62× / 0.14×）；② 平坦拷贝单流/并发带宽（54.6 / 55.9 / 52.1 GB/s）；③ 两节点跨步抽取（11.2 / 2.1 GB/s）。
2. 用 ③÷② 估算每台的抽取率（约 20% 与 4%），再与文中 NVLink 的 90–99% 并列成一张三行小表。
3. 写两句话回答：为什么「两节点相差 5×」本身就足以拒绝，即使较快的那台（0.62×）看起来勉强可用？（提示：想想一个会在任意两台 PCIe 机器之间漂移 5× 的数，对一个声称 1.60–2.17× 加速的库意味着什么——你无法向任何用户承诺方向。）

**需要观察的现象**：数字之间的关系（fabric 同、抽取异）指向「交易失效」而非「带宽不足」。

**预期结果**：三行小表加两句话结论。纯阅读型实践，无需 GPU；若有条件在 PCIe 机器上复跑，属**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么「正确但慢」还要拒绝，而不是加一个性能警告？

**答案**：[docs/design.md:L173-L175](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/design.md#L173-L175)：追平需要新传输层（共享主机内存 + 第二握手），在 NVLink 上一文不值，所以不建；不建就拒绝——「refuses the topology instead of pretending」。警告会把一个实测不稳定（5× 漂移）的路径留给用户，而拒绝把用户导向 torch.distributed 这个已知更快的选项。

**练习 2**：`check_nvlink` 为什么对 `nullopt` 放行而不是拒绝？

**答案**：[src/nvlink.cc:L204-L206](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/nvlink.cc#L204-L206) 返回空串即放行。NVML 经 dlopen 加载，机器上没有或太老时探测「说不出」——fail-open 的取向是**不因探测工具的缺席误拒用户**（u8-l1 详述）；拒绝只发生在矩阵确实回答且含 false 对时。两害相权：误拒把可用拓扑挡在门外，放行的代价只是用户可能踩到慢路径，且文档与拒绝消息都指了路。

**练习 3**：两台 PCIe 节点平坦拷贝带宽几乎相同，为什么跨步拷贝差 5×？

**答案**：[docs/benchmark.md:L290-L293](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/benchmark.md#L290-L293)：差异在 PCIe switch 的布线——快节点 socket-0 的 GPU 两两成对（2+2），慢节点三块挤一个 switch、一块孤悬（3+1）。带步长拷贝在 switch 上产生的访问模式对这种布局敏感，于是「同一 fabric、同一平坦带宽、不同的抽取率」。

### 4.4 被移除的表面：每条一句话的理由

#### 4.4.1 概念说明

design.md 的最后一章「Removed, and why」开篇一句是全文的座右铭：**「Earlier versions carried more surface than they could defend」**——早期版本背着超过自己能辩护能力的 API 表面。删除不是做减法的审美，而是把每条表面要么换成**有更强不变量支撑的等价物**，要么确认**它要防的问题已经不存在**。这张清单是理解当前 API 形状（u2、u4 学过的 `out=`、`empty_output`、`lend`）为什么长成这样的反面教材：你知道了它们取代了什么，才算真正知道它们是什么。

与它互补的是 migration.md 的「What this does not have」——那不是「删过」的历史，而是**现存的边界**，迁移前先读：「Check this list before you start, not after」。

#### 4.4.2 核心流程

五条被删特性 → 一句话理由 → 接替者：

| 被删掉的 | 一句话理由 | 接替者 |
| --- | --- | --- |
| **NVSHMEM 后端** | 它给的正是 torch 对称内存后来给的（带 peer 地址的 VMM 分配），代价却是一整套 bootstrap、按步长切 team、独立池分配器、两个为躲开 torch 自带 NVSHMEM 符号冲突而挑的未文档化 `hostlib_` 入口、外加一条无法执行的「活组必须瓜分作业」规则——换到 torch 后**无一存活** | `empty_strided_p2p` + `rendezvous`（u4-l1） |
| **借出窗口的入口点**（结果即窗口本身） | 生命周期契约没有任何机制强制，谁也说不清缓冲何时被回收 | `out=` 接 `empty_output()`：同样的节省，缓冲寿命握在调用者手里（u4-l3） |
| **`tag` 参数** | 它为了「同时存活的结果分居不同窗口」而存在；拷贝路径每次调用各自新张量、零拷贝路径用调用者自己的缓冲，**已无歧义可消** | 无（问题消失） |
| **`reserve()` / `seal()`** | 它们防的是旧池「分配是集合的且流同步」导致的调用中途死锁；那死锁的食物——一次故意留在飞行中的自旋屏障——随延迟收尾握手一起被删，死锁饿死了 | 无（死锁源头已除） |
| **`torch.distributed` 回退组** | 对不该支持的拓扑提供回退等于假装支持；正确的答案是「直接用 torch.distributed」 | 构造期 NVLink 拒绝（4.3） |

「What this does not have」清单里值得单独点名的三条（它们是**结构事实**而非删除历史）：

- **跨节点**：没有任何字节走 NIC。
- **`world_size > 8`**：结构性的——`BarPeers::p[8]`（[src/barrier.cu:L27-L29](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/barrier.cu#L27-L29)），peer 地址按值传进内核的定长数组。
- **ring attention 及 ulysses×ring 混合、注意力内核本身**：本库只交付集合通信，q/k/v 管道与核选择是调用者的事（u2-l4 的三明治结构）。

#### 4.4.3 源码精读

- [docs/design.md:L190-L210](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/design.md#L190-L210) — 「Removed, and why」全章：开篇的「carried more surface than they could defend」，随后 NVSHMEM 一段（L194-L198，五项代价逐一列出）、借出入口（L199-L201）、`tag`（L202-L204）、`reserve()/seal()`（L205-L208）、回退组（L209-L210）。
- [docs/migration.md:L214-L231](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/migration.md#L214-L231) — 「What this does not have」：跨节点、PCIe（复述 0.62×/0.14×）、`world_size > 8`（并点名 `BarPeers::p[8]` 在 `src/barrier.cu`）、ring 混合、注意力内核、`torch.compile` 追踪、async/`out=` 的 autograd、3D/5D 入口、dtype 白名单与 16 字节对齐。
- [src/barrier.cu:L26-L29](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/barrier.cu#L26-L29) — `BarPeers` 的定义与注释：「world_size 受算子层执行的 [1,8] 单节点上限约束」——migration.md 那条「结构性」清单项的实物。
- [docs/benchmark.md:L314-L322](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/benchmark.md#L314-L322) — 被否决方案表（与本模块相邻但不同类：那是**实现方案**的取舍——每 peer 一条流、顺序 peer 枚举、`cudaMemcpy3DBatchAsync`、单行拷贝折叠、连续段写、memop、NCCL 形状发布、双缓冲、跨 socket 主机暂存——每行一个实测数字，是「删」的另一种形态：**试过、量过、放弃过**）。
- [docs/migration.md:L233-L243](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/migration.md#L233-L243) — 「After the switch」：换库后的纪律清单（构造/`empty_output`/`destroy` 是集合的、形状与 dtype 序列全 rank 一致、组只建一次、`out=` 省 copy-out 值 1.14–1.28× 但不进训练图）——被删表面留下的「形状」最终由这些纪律来守。

#### 4.4.4 代码实践

**实践目标**：验证「删除是等价替换」——每条被删特性在当前代码里都能指认出接替者或消失的原因。

**操作步骤**：

1. 对表中五行，各打开一个「接替者」锚点核对：NVSHMEM 行看 [src/group.cc:L216-L220](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L216-L220)（`empty_strided_p2p` 直连分配入口的注释）；借出行看 `empty_output` 的 owned_ 记账（u4-l3 已精读）；`tag` 行在 [docs/api.md](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/api.md) 里确认今日签名确无此参数；`reserve/seal` 行在公开 API 里 grep 确认不存在：
   ```bash
   grep -rn "reserve\|seal\|\btag\b" python/fast_ulysses/ src/bindings.cc | grep -v staging
   ```
2. 对每行写一句「删除后不变量反而更强了」或「问题源头已消失」的判词（示例：`out=` 的不变量是「缓冲寿命 = 调用者手里那份引用的寿命」，可被 storage 引用计数观测；而旧借出入口的寿命契约无人观测）。
3. 把 [docs/benchmark.md:L314-L322](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/benchmark.md#L314-L322) 的九行方案与「Removed」五项并排放一张 14 行总表，加一列「删除依据类型」：实测数字 / 等价替换 / 问题消失 / 结构事实。

**需要观察的现象**：grep `reserve|seal|tag` 在公开 API 命中为零（staging 相关的 `release_staging` 是另一回事，注意区分）。

**预期结果**：14 行总表。每行的判词不超过一句。纯阅读型实践，无需 GPU。

#### 4.4.5 小练习与答案

**练习 1**：`tag` 被删是因为「没什么可消歧的了」。用 u4 的知识解释：哪两条路径让「同时存活的结果分居不同窗口」不再必要？

**答案**：拷贝路径（默认）每次调用都产出自己的新张量，窗口只是中转站，结果不驻留；零拷贝路径（`out=` / `empty_output`）结果落在**调用者自己的缓冲**里，天然与别的调用隔离。`tag` 防的那类冲突在这两条路径上都无法构造。（`lend=True` 的轮转是后来为 async 加的第四入口，槽选择由调用计数决定——它面对的同一问题用的是 SPMD 一致量而非 `tag`，见 u4-l4。）

**练习 2**：`reserve()/seal()` 防的死锁「食物被删掉了」是什么意思？

**答案**：[docs/design.md:L205-L208](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/design.md#L205-L208)：旧池的分配是集合的且会做流同步，若发生在一次调用的中途，会与那次调用**故意留在飞行中的自旋屏障**互相等待——屏障等对端、对端等分配、分配等屏障。后来的设计把收尾握手延迟到不再与分配交叠的位置，自旋屏障不再滞留空中，这个死锁失去了食物；分配依旧集合（u4-l1 的 rendezvous 论证），但不再有死锁可喂。

**练习 3**：`world_size > 8` 的上限为什么 migration.md 称它「structural」？放宽它最少要动什么？

**答案**：因为 peer 的 flag 地址按值装在 `BarPeers::p[8]`（[src/barrier.cu:L27-L29](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/barrier.cu#L27-L29)）这个定长数组里随内核参数传入。放宽至少要改成指针+长度（或 `cudaMemcpyToSymbol` 到常量/全局内存），并同步放宽算子层 `[1,8]` 校验；但单节点 8 卡的物理上限让它不值得——这正是「结构事实」类边界与「待办」的区别。

## 5. 综合实践

**任务：写一篇 300 字左右的设计评审笔记——《如果把屏障换成 NCCL 形状》**。这是全手册最后一个实践，也是规格指定的本讲实践任务，它把 4.1 的假设、4.2 的形状、4.4 的评审文体串成一份真实可用的工程文档。

**要求**：

1. **改动清单**：列出四个必改文件及各自改动——
   - [src/barrier.cu](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/barrier.cu)：`fast_barrier` 拆分——发布改为在 xfer 流上向每个 peer 的 `flags[rank]` 发 8 字节 D2D 拷贝；等待改为 `cuStreamWaitValue32`；自旋内核退役（`BarPeers` 可留作地址载体）。
   - [src/transfer.cu](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/transfer.cu)：flag 拷贝必须排在 `launch_a2a_ce` 已发射的 peer 拷贝**同一条 xfer 流**之后（「同引擎」是规避假设的全部意义）；故障注入的 join 跳过分支语义随之复核。
   - [include/fast_ulysses/transfer.hpp](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/transfer.hpp)：契约注释改写——握手区布局从「`uint64 flags[ws]` + `uint64 epoch`」改为 32 位 flag 的约定（`cuStreamWaitValue32` 是 32 位等待），`fast_barrier` 的签名与文档同步。
   - [src/group.cc](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc)：[L18-L37](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L18-L37) 的 `flag_slots`/`flag_offset_slots` 与 [L250](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L250) 的清零范围按新布局重算，signal pad 末尾的偏移不变、宽度变。
2. **代价**（必须引 design.md 论证过的两条）：发布侧 3× 慢（7×8B ≈ 16 µs vs 内核 4–6 µs，且只挪发布）；等待侧 memop 在并发 GEMM 下把重叠 +34% 拖到 −28%（「序对 CUDA 不可见」）。
3. **开放问题至少一条**：`cuStreamWriteValue32` 的写值与 `cuStreamWaitValue32` 的比较值都是主机参数、图捕获时固化——与「epoch 必须由设备侧 `atomicAdd` 推进，否则重放被陈旧状态满足」（[src/barrier.cu:L33-L37](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/barrier.cu#L33-L37)、[docs/design.md:L85-L89](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/design.md#L85-L89)）如何相容？design.md 未回答——评审笔记的价值就在于把它写出来。
4. **迁移对策三条**：从 [docs/migration.md:L214-L231](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/migration.md#L214-L231)「What this does not have」任挑三条，各写一句对策。参考示例（三选三可换成你自己的）：
   - 跨节点 → 「在节点边界拆分通信：组内序列并行、组间换 ring/DP，不让任何字节等 NIC。」
   - 无注意力内核 → 「qkv 打包进 `3*head_dim` 末维、两次调用之间放你自己的 SDPA，照 [examples/dit_attention.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/examples/dit_attention.py) 的三明治组装。」
   - 3D/5D 入口不存在 → 「上游按 `(b, s, n, d)` 投影：5D 折 qkv 轴成 `(b, s, n, 3d)`（警惕 4.4 之外的混头陷阱），`(s, n, d)` 补一个 view。」

**预期结果**：一篇你自己的、可放进团队 wiki 的评审短文——有改动清单、有引证代价、有开放问题、有迁移对策。**参考答案不提供全文**（抄结论就失去了评审训练），只提供自检三条：① 四个文件是否都出现且改动与 4.2.3 的源码事实一致；② 两条代价是否带数字与出处；③ 开放问题是否触及「固化进图」这个 design.md 未覆盖的角落。全部纯纸面作业，无需 GPU。

## 6. 本讲小结

- **一条假设撑起整个库**：「已完成的 CE 写在随后内核的 release store 到达前对目的地可见」——四份权威（API Reference 的主机侧完成语义、Programming Guide 的 NULL 流限定、PTX 的线程作用域、全体厂商的沉默）全部不给背书；业界三家接受它（NVSHMEM、TE 更弱，本库至少带 release），NCCL 换形状规避它。
- **实证不等于证明**：`ce_ordering.py` 每次运行武装负控制，`armed > 0 and torn == 0` 才算数——证据的效力以「测试还能失败」为前提。
- **NCCL 形状是逃生口不是现答案**：flag 走 D2D 拷贝同流同引擎 + `cuStreamWaitValue32` 等待；实测发布侧 3× 慢、等待侧把并发重叠从 +34% 拖到 −28%（memop 的序对 CUDA 不可见）；且 `CU_DEVICE_ATTRIBUTE_CAN_FLUSH_REMOTE_WRITES` 已被作者自我修正为「不是理由」。
- **拒绝 PCIe 靠的是不稳定的实测**：0.62×/0.14×、两节点 5× 分歧不做平均；fabric 相同（平坦拷贝带宽一致），垮掉的是跨步模式的抽取率（20%/4% vs 90–99%），随 switch 布局漂移；追平需要新传输层而不建，于是构造函数拒绝而非假装。
- **「删过的表面」各有判词**：NVSHMEM（torch 对称内存全盘接替）、借出入口（`out=`+`empty_output` 把寿命还给调用者）、`tag`（无歧义可消）、`reserve/seal`（死锁的食物没了）、回退组（用 torch.distributed 才是答案）——外加结构性的 `BarPeers::p[8]`、单节点、无注意力内核。
- **design.md 本身会修订自己**：CAN_FLUSH 一段与 v0.1 的「跨步从不慢于平坦」被后来的测量推翻——设计文档是活文本，这是它可信的原因。

## 7. 下一步学习建议

本讲到此，九个单元全部完成。三条收束路线：

1. **把手册读到代码里去**：挑一个你真正关心的改动（比如综合实践的 NCCL 形状，或双缓冲窗口——benchmark.md 记录它只值 ~0.7% 且会摧毁 window_race 的前提），在仓库里追一遍它会触碰的每个文件，用本手册各讲的调用链图核对；给别人讲一遍 4.1 的四条缺位，讲不清的地方就是你要重读的地方。
2. **去读被对照的实现**：NCCL 的 `ce_coll.cc`（kernel-less CE 路径）、NVSHMEM 的 `signal_op` 回退内核、TransformerEngine 的 `ring_exchange`——design.md 对它们只各用了一句，值得带着本讲的框架去看原仓库里那句话的实物。
3. **带着这套方法论看下一个项目**：每当代码「能跑」时问三个问题——它依赖哪条没人写下来的性质（4.1）？它拒绝的边界在哪、依据是实测还是偏好（4.3）？它删掉了什么、接替者的不变量是否更强（4.4）？这三个问题就是这本手册想留给你的东西。
