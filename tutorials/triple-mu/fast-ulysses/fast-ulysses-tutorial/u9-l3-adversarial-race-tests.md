# 对抗性测试：CE 顺序、窗口竞态与重叠屏障

## 1. 本讲目标

fast-ulysses 的正确性依赖几条「结果本身证明不了」的性质：CE 写入的可见时序、单缓冲窗口不被下一调用覆盖、sync/async 双流各用各的窗口、CUDA graph 重放拿到新鲜的 epoch。这些性质全部是时序性质——不咬合的运行次序下它们照样「通过」。本讲解读四个专门与自己的实现作对的测试 worker，读完你应当能：

1. 解释什么是**负控制**（negative control）：为什么「武装起来的故障必须真的让测试读到陈旧字节」，否则这个测试就是瞎的（BLIND）。
2. 说明**时序偏斜（skew）必须做进传输长度而不是到达时间**才能被观察到，并说出四个 worker 各自把 skew 放在哪里、为什么放在那里。
3. 复述**单缓冲窗口的读写竞态窗口**：为什么防护必须放在第 i+1 次调用的开头（开路屏障）而不是第 i 次调用的结尾，以及 window_race 如何同时验证「有防护则不撕」与「拆掉防护必撕」。
4. 复述 **sync/async 双流双窗口隔离**的三重检查（位精确数据、epoch 等式、重叠活性），以及 cudagraph 用「每次重放 epoch 恰好 +2」钉死设备侧 epoch 的必要性。

## 2. 前置知识

- **负控制（negative control）**：借自医学实验的词。证明「仪器能读出病」的方式是故意注入一个已知的病灶：如果连它都读不出来，说明仪器坏了，之后所有「没病」的读数都不可信。在本项目里，「病灶」是一个故意注入的故障（延迟 CE 载荷），「读出病」= 测试读到陈旧字节。
- **撕裂（tear）与陈旧字节（stale bytes）**：读到的不是本次调用应得的值，而是上一次调用残留的、或别的调用混入的字节。本讲的 worker 用「每次迭代填一个可区分的常量」来检测撕裂。
- **BLIND（变盲）**：一个时序测试在实现已被破坏的前提下仍然通过，说明它根本没有测量到目标性质。BLIND 在这些 worker 里是**失败**（退出码非 0），不是跳过。
- **bf16 的精度边界**：bfloat16 只有 8 个有效位，整数到 256 逐个精确，往上相邻整数会撞在同一表示上。这决定了测试值要「在 1..128 循环」而不是一路递增。
- 需要承接的前置认知（来自 u4/u5/u7/u9 前几讲）：一次调用 = 「开路屏障 → CE 传输 → 收尾屏障 →（可选）copy_out」（u6-l2）；窗口单缓冲、按 `(role, dtype)` 缓存，sync 走调用者流、async 走 comm 流、两流互不排序（u4-l2、u7-l1）；epoch 由 barrier_kernel 在**设备侧** atomicAdd 推进（u5-l3）；`empty_output()` 缓冲就是窗口本体、peers 直写（u4-l3）；位精确与探针断言的双轨思路（u9-l2）。
- 这四个 worker 都要求 world_size ≥ 2（`fast_barrier` 在 ws=1 时直接返回、什么都不发射，一切检查都会空洞通过），所以本讲的实践以**源码阅读型**为主，多卡运行为可选步骤。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [test/distributed/ce_ordering.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/ce_ordering.py) | 验证「CE 载荷在宣布它的 flag 到达时已在目的地可见」这一未文档化假设；带武装式负控制 |
| [test/distributed/window_race.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/window_race.py) | 验证 peer 的下一次调用不会覆盖本 rank 还在读的窗口；负控制按 rank 归约 |
| [test/distributed/overlapping_barriers.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/overlapping_barriers.py) | 验证 sync 与 async 两把自旋屏障内核同时驻留时互不干扰（双窗口隔离） |
| [test/distributed/cudagraph.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/cudagraph.py) | 验证捕获的稳态调用在重放下仍正确；断言 epoch 每次重放恰好 +2 |
| [src/transfer.cu](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/transfer.cu) | CE 传输本体 + `_set_ce_fault` 故障注入点（延迟载荷、跳过汇合） |
| [src/bindings.cc](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc) | `transfer_on_stream` 的两道屏障；`epoch_debug` 与 `_set_ce_fault` 绑定 |
| [src/barrier.cu](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/barrier.cu) | 自旋屏障内核；epoch 的设备侧 atomicAdd；ws=1 早退 |
| [src/group.cc](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc) | `epoch()` 探针：先查 `owned_`，再查 `windows_[(role, dtype)]`，否则 -1 |
| [test/test_distributed.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_distributed.py) | 用 torchrun 包装这些 worker 的测试编排、超时清理与 BLIND 处理 |

运行方式（需 ≥2 GPU，待本地验证）：`pytest test/test_distributed.py -m multigpu -k "ce_ordering or window_race or overlapping or cudagraph"`，或直接 `torchrun --nproc_per_node=<N> test/distributed/<worker>.py`。这四个文件在编排里被列入 `_NEEDS_PEERS`，在 `FAST_ULYSSES_TEST_NPROC=1` 时跳过而非假通过（[test/test_distributed.py:34-36](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_distributed.py#L34-L36)、[test/test_distributed.py:68-69](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_distributed.py#L68-L69)）；而 ce_ordering 与 window_race 的 BLIND **必须继续让套件失败**，只有机器原因导致的 NVLINK BLIND 才转为 skip（[test/test_distributed.py:71-76](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_distributed.py#L71-L76)）。

## 4. 核心概念与源码讲解

### 4.1 CE 可见性假设与负控制

#### 4.1.1 概念说明

整个设计押在一条没有任何文档背书的假设上：**一次已完成（completed）的拷贝引擎写，在随后同一条流上屏障内核的 release store 到达目的地时，已经对目的地可见**。

为什么它是假设而不是保证？ce_ordering 的 docstring 给出三层理由（[test/distributed/ce_ordering.py:5-12](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/ce_ordering.py#L5-L12)）：

- 流序只说拷贝在屏障内核**发射前已完成**，而 CUDA 的「completed」是**主机侧属性**（host-visible property）；
- Programming Guide 的跨设备排序保证只覆盖 NULL 流，对其他流上的异步拷贝明确收回；
- PTX 的 `.release` 语义只对**当前线程**的先前操作排序，而 CE 传输不是任何一个线程的操作。

形式化地：记 \( C \) 为 CE 拷贝、\( S \) 为其后同流的 release store，流序给你的是 \( \text{completed}_{\text{host}}(C) \prec \text{issue}(S) \)，设计需要的是 \( \text{visible}_{\text{peer}}(C) \prec \text{observed}_{\text{peer}}(S) \)——中间这一步没有任何规范补上。所以这个 worker 的通过只是「这台机器、这个形状上的证据」，不是证明。NVIDIA 自家的 NVSHMEM（on-stream SIGNAL_ADD）和 TransformerEngine（userbuffers ring_exchange）以更弱的顺序发布，NCCL 则用「flag 本身作为 D2D 拷贝走载荷同一条流」换了形状来回避这个赌注（[test/distributed/ce_ordering.py:14-18](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/ce_ordering.py#L14-L18)）——这条对照线在 u9-l5 的设计反思里会再次出现。

既然主检查（CLEAN 阶段）测的是「没病」，就必须有一个负控制证明「仪器能读出病」：**ARMED 阶段注入故障，读者必须看到陈旧字节**。

#### 4.1.2 核心流程

```
准备：不均匀 seq_splits（rank 0 占大头）→ empty_output（零拷贝）→ 循环外完成分配与热身

ARMED（负控制）：
  _set_ce_fault(2000us)          # 武装
  循环 120 次：fill(v) → all_to_all_4d(out=窗口) → 立刻数 (y != v)
  要求：armed_torn > 0（全组求和后至少有一次撕裂）
  _set_ce_fault(0)               # 解除

CLEAN（主检查）：
  同一循环再来一遍
  要求：clean_torn == 0

判定：ok = (armed > 0) and (torn == 0)
  armed == 0 → 打印 BLIND，退出 1：故障没打到被测路径，CLEAN 什么也没证明
  torn > 0  → 打印 FAIL：真机上假设不成立
```

故障注入的实现要点（见 4.1.3）：**在 xfer 流上垫一个睡 2ms 的 host func 把载荷压住，同时跳过「caller 流等 xfer 完成」的汇合**——两件事合起来保证收尾屏障在字节还在飞行时就发布，这正是假设所担心的场景被人为变成现实。

#### 4.1.3 源码精读

负控制的开关是一个仅测试可见的 C++ 全局量，武装时 launch_a2a_ce 会做两处手脚：

[src/transfer.cu:16-22](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/transfer.cu#L16-L22) —— 故障延迟的全局量与「睡指定微秒数」的 host func（debug-only，0 = 关，正常构建唯一可见的状态）。

[src/transfer.cu:98-101](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/transfer.cu#L98-L101) —— 注入点一：xfer 流在等完 ready 事件后先执行 `cudaLaunchHostFunc(delay_payload)`，把后面所有 peer 拷贝压住 2000µs：

```cpp
// Fault injection (see set_ce_fault): hold the payload back so the flag cannot be behind it.
if (g_fault_delay_us > 0) {
    ULYSSES_CUDA_CHECK(cudaLaunchHostFunc(xfer, delay_payload, reinterpret_cast<void*>(g_fault_delay_us)));
}
```

[src/transfer.cu:118-124](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/transfer.cu#L118-L124) —— 注入点二：**跳过 caller 流对 done 事件的等待**。没有这一步，caller 流会先等远端拷贝全部落地才走到收尾屏障，故障就被自己抵消了：

```cpp
// Skipping this join is the fault: the caller's stream then reaches the closing barrier, and
// publishes, without waiting for the remote copies. Nothing else about the call changes.
if (g_fault_delay_us == 0) {
    ULYSSES_CUDA_CHECK(cudaStreamWaitEvent(stream, done, 0));
}
```

Python 侧的绑定刻意加了下划线、且不是 torch op，因为「武装它等于故意弄坏算子」（[src/bindings.cc:801-803](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L801-L803)）。

worker 的两阶段与判定（[test/distributed/ce_ordering.py:20-25](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/ce_ordering.py#L20-L25)）明确写下：ARMED 是负控制、**每次调用都执行**而不是躺在注释里等谁来想起；Phase 1 失败不代表算子没事，代表这个 worker 变瞎了。执行处：

[test/distributed/ce_ordering.py:111-117](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/ce_ordering.py#L111-L117) —— 武装 → 跑 ARMED → 解除 → 跑 CLEAN。

[test/distributed/ce_ordering.py:65-79](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/ce_ordering.py#L65-L79) —— 测量循环本体：填常量、调用、**直接排在收尾屏障后面读窗口本身**。注释专门说明读之前不能有任何 copy/clone：屏障和首次读之间的任何一次 D2D 传递都是给写留出的排水时间，恰好掩蔽被测的量；`.item()` 的主机同步发生在比较已经读走数据之后，也掩不住。

[test/distributed/ce_ordering.py:119-137](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/ce_ordering.py#L119-L137) —— 全组 `all_reduce` 求和后判定 `ok = armed > 0 and torn == 0`；armed 为 0 打 BLIND、clean 撕裂打 FAIL、否则 PASS（带控制组撕裂计数）。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：把 ARMED 状态下两条流上的操作序列画成时间线，确认「flag 早于载荷」是被两处注入**共同**保证的。
2. **操作步骤**：
   - 精读 [src/transfer.cu:86-124](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/transfer.cu#L86-L124)，列出 caller 流与 xfer 流各自的操作（ready 事件、host func、peer 拷贝、done 事件、条件汇合）。
   - 回到 [src/bindings.cc:194-211](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L194-L211)，把「开路屏障 → launch_a2a_ce → 收尾屏障」叠到 caller 流时间线上。
   - 分别推演两种**残缺**注入：只延迟不跳汇合、只跳汇合不延迟。
3. **需要观察的现象**：时间线图上，完整注入时收尾屏障的 release store 明显早于 peer 拷贝落地。
4. **预期结果**：只延迟不跳汇合——caller 流在 done 事件处等住，屏障仍排在拷贝之后，ARMED 撕不了 → BLIND；只跳汇合不延迟——拷贝没有被压住，屏障发布时字节大概率已就位，ARMED 不稳定撕裂 → 也是 BLIND。结论：**两处注入缺一不可**。
5. 多卡实际运行（`torchrun --nproc_per_node=4 test/distributed/ce_ordering.py`）与 ARMED 撕裂计数：待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么负控制要「每次运行都执行」，而不是作为注释里的一个手动步骤？

**答案**：注释里的控制依赖人去记得执行，久而久之没人做，测试就退化成只有 CLEAN 一半——它仍然能在假设被破坏时通过（比如某代驱动悄悄改了 CE 顺序），却没有任何机制发现自己已经测不到。把 ARMED 放进每次运行，且 `armed == 0` 直接判负，让「仪器失灵」这件事本身成为机器可检测的失败（[test/distributed/ce_ordering.py:22-24](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/ce_ordering.py#L22-L24)）。

**练习 2**：`_set_ce_fault` 为什么通过 pybind 暴露为 `_set_ce_fault` 而不是注册成一个 torch 算子？

**答案**：注册成算子它会进入 dispatcher 的 schema 体系（可能被 trace / autograd 捕获），而且一个「故意弄坏算子」的入口没有任何正当业务语义。pybind 模块函数 + 下划线前缀把它钉死在「TESTS ONLY」的语义层（[src/bindings.cc:801-803](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L801-L803)）。

**练习 3**：ce_ordering 把 ARMED 结果**全组求和**（`armed > 0`），window_race 却按 rank 归约——ce_ordering 为什么不得不接受这个 weaker 的形式？

**答案**：因为 skew 的构造把 rank 0 设成慢**写者**，rank 0 自己的窗口由快 rank 填、它的读最不容易撕——若要求每个 rank 的 ARMED 都撕裂，在一个正常工作的运行上也会对 skew 围绕的那个 rank 报 BLIND。代价被写明而不是藏起来：只打到部分 rank 的故障也会全组 PASS，所以 `armed` 只证明「故障打到了被测路径」，不证明「处处都打到」（[test/distributed/ce_ordering.py:42-49](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/ce_ordering.py#L42-L49)）。

### 4.2 传输内 skew 设计

#### 4.2.1 概念说明

时序测试的第二条命脉：**你得让竞争真的有机会发生**。四个 worker 各自需要某种「偏斜」（skew）把系统推离整齐划一的稳态，否则屏障们都在微秒级内对齐通过，死代码也测不出来。关键洞察是：**skew 的落点必须匹配被测性质**——

- ce_ordering 测的是「载荷还在飞时 flag 先到」。这要求**传输长度不均**：让 rank 0 的出向拷贝长几倍，快 rank 才会在它还在拷时就到达收尾屏障并读它的窗口。
- **到达偏斜在这里无效**，而且不是理论推断：docstring 记载这个 worker 的早期版本就是把偏斜做在到达时间上，结果「通过了但什么都没测」——因为**每次调用以一个握手（开路屏障）开场，它在任何数据移动之前把所有 rank 重新对齐**（[test/distributed/ce_ordering.py:33-37](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/ce_ordering.py#L33-L37)）。你让某个 rank 晚睡 2ms 醒来，它只是晚一点进入开路屏障，之后大家仍然同一起跑线出发，传输长度照旧相等，谁也读不到撕裂。
- window_race / overlapping_barriers / cudagraph 测的是别的性质，所以 skew 落点各不相同（见下表）。

另一个贯穿的设计细节：**每次迭代的填充值必须在 bf16 下可区分**。bf16 有 8 个有效位，整数到 256 精确、往上相邻值撞车——所以值要在 1..128 循环而不是 `1..ITERS` 递增，否则第 257 次迭代的陈旧字节和新字节表示相同，撕裂静默不可见（[test/distributed/ce_ordering.py:38-40](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/ce_ordering.py#L38-L40)；overlapping_barriers 的失明清单第 9 条也钉了同一件事）。

#### 4.2.2 核心流程

四个 worker 的 skew 落点对比：

| worker | 被测性质 | skew 落点 | 为什么必须在这里 |
| --- | --- | --- | --- |
| ce_ordering | flag 先于载荷可见 | **传输长度**：不均匀 seq_splits，rank 0 占大头 | 到达偏斜被开路握手抹平；只有拷贝本身不均等，快 rank 才会在 rank 0 还在拷时读 |
| window_race | peer 下一调用的写 vs 本 rank 的读 | **读者侧**：每轮恰好一个 rank（`rank == i % ws`）跑 96 个 GEMM 压舱 | 需要有人领先发起下一调用去覆盖慢读者的窗口；人人压舱则谁也不领先，CLEAN 空转 |
| overlapping_barriers | 两把自旋内核同时驻留 | **提交时刻**：迟到 rank `torch.cuda._sleep(200k cycles)` | 这里要的恰是「对端停在内核里等」：迟到 rank 的对端会同时坐进两把 spin kernel，共驻留成立 |
| cudagraph | 重放下握手仍活着 | **rank 0 重放前跑 GEMM 链** | 一齐到达的重放互相重新对齐，死屏障也能通过；让其他 rank 领先才能咬到 |

注意 overlapping_barriers 用「到达偏斜」是**正确的**而非犯 ce_ordering 的旧错：它测的不是传输时序，而是两个屏障内核的共驻留——迟到 rank 的对端必须在 spin kernel 里等它，睡眠恰好制造这个等待。同一工具，不同性质，不同落点。

#### 4.2.3 源码精读

ce_ordering 的形状构造——skew 的来源是 `seq_splits`，mode 0 下每个 rank 出向发送 \( s_{\text{me}} \times n_{\text{total}} \times d \) 个元素，所以序列轴正是让传输不等的轴：

[test/distributed/ce_ordering.py:92-100](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/ce_ordering.py#L92-L100) —— rank 0 拿走剩余全部序列，其余 rank 各拿 \( s_{\text{total}}/(2ws) \)。可以算出 rank 0 的序列恒为其他 rank 的 \( ws+1 \) 倍（ws=4 时 5 倍，ws=8 时 9 倍），它的拷贝相应长好几倍，快 rank 便在收尾屏障处读到它的 flag 落地的瞬间：

```python
base = s_total // (2 * ws)
seq_splits = [base] * ws
seq_splits[0] = s_total - base * (ws - 1)
```

[test/distributed/ce_ordering.py:103-109](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/ce_ordering.py#L103-L109) —— 分配与 rendezvous **必须在循环外**完成：放进循环里会每轮把 rank 串行化，恰好毁掉这个 worker 依赖的偏斜。

对照三个「同类不同落点」的例子：

[test/distributed/window_race.py:230-232](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/window_race.py#L230-232) —— CLEAN 相位的旋转压舱：`if rank == i % ws` 才跑 GEMM 链，且 `skewed` 计数器在分支**内部**递增、事后校验全组总和恰为 ITERS——这是给「对称压舱」这一失明模式装的机械探测器（删掉 `if` 守卫让所有 rank 都跑，CLEAN 变空转但计数器立刻暴露）。

[test/distributed/overlapping_barriers.py:251-252](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/overlapping_barriers.py#L251-252) —— 提交时刻偏斜：`torch.cuda._sleep(SKEW_CYCLES)`，注释说明迟到的量必须超过主机提交下一调用的时间，否则偏斜在到达设备前就被吸收。

[test/distributed/cudagraph.py:33-39](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/cudagraph.py#L33-L39) —— skew 的必要性陈述直白：「一齐到达的重放会重新对齐，死屏障照样通过——这正是紧凑 benchmark 循环制造的那种失明。所以 rank 0 在每次重放前跑 GEMM 链，让其他 rank 领先」，并且点名删除 `if rank == 0` 压舱就是这个 worker 的负控制。

#### 4.2.4 代码实践

1. **实践目标**：用具体数字确认「传输长度偏斜」的量级，并验证 bf16 值域选择的必要性。
2. **操作步骤**：
   - 取 ws=4，按 [test/distributed/ce_ordering.py:94-99](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/ce_ordering.py#L94-L99) 计算：`s_total = 8192*4 = 32768`，`base = 4096`，`seq_splits = [20480, 4096, 4096, 4096]`；写出 rank 0 与 rank 1 的出向元素数（mode 0：\( s_{\text{me}} \times 32 \times 384 \)）。
   - 换成均匀 `seq_splits = [8192]*4` 重算，比较两种情况下最快 rank 与最慢 rank 完成出向拷贝的时间比（假设带宽相同）。
   - 在任意有 torch 的环境跑一行：`torch.tensor([300.0, 301.0], dtype=torch.bfloat16)`，观察两个值是否仍可区分。
3. **需要观察的现象**：不均匀时 rank 0 的发送量是其他 rank 的 5 倍；均匀时全部相等。
4. **预期结果**：不均匀 splits 下快 rank 完成 ≈ 1/5 于 rank 0 的拷贝后即进入收尾屏障——撕裂窗口打开。bf16 中 300 与 301 落到同一表示（间隔为 2），即超过 256 后相邻迭代值不可区分——这解释了 `v = 1 + i % 128` 的循环。
5. 有 ≥2 GPU 时运行 ce_ordering 并记录 PASS 行里的 `MB/call` 与控制组撕裂计数：待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：把 ce_ordering 的 skew 改成「让 rank 0 每轮 `torch.cuda._sleep(2ms)` 再调用」，其余不变。ARMED 与 CLEAN 各会怎样？

**答案**：两者都测不到东西。开路屏障是集体握手，rank 0 睡醒后大家仍在握手处对齐，之后各 rank 传输长度相同、同时完成，快 rank 读 rank 0 窗口时它的拷贝早已落地——ARMED 可能仍撕裂（因为注入也压了载荷），但 CLEAN 阶段的「性质本身」从没被置于危险中；更关键的是这个版本复刻了 docstring 记载的历史错误：早版就是这么写的，「通过了但什么都没测」（[test/distributed/ce_ordering.py:33-37](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/ce_ordering.py#L33-L37)）。区分两者：负控制管的是「仪器」，skew 管的是「CLEAN 有没有暴露在风险里」，两者独立失效。

**练习 2**：window_race 的 CLEAN 相位为什么压舱要**轮换**（`rank == i % ws`）而不是固定让 rank 0 慢？

**答案**：轮换让「没有 rank 沉淀出稳定的领先/落后关系」，并且每个 rank 的窗口都轮到被覆盖的机会（[test/distributed/window_race.py:226-229](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/window_race.py#L226-L229)）。overlapping_barriers 还从采样角度补了理由：采样步长 97 是素数，若与 ws 有公因子就只采到轮换的固定相位（[test/distributed/overlapping_barriers.py:147-150](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/overlapping_barriers.py#L147-L150)）。

**练习 3**：ITERS=120 的循环里 `v = float(1 + i % 128)`，那 v 会取到重复值吗？为什么这无妨？

**答案**：会（i=1 与 i=129 若存在则同值；120 轮内 1..120 不重复，但跨 ARMED/CLEAN 两相位同值会重复）。无妨的前提是撕裂检测比较的是**本次**填充值与窗口读出值：只要同一迭代内「刚 fill 的 v」与「窗口里的旧 v'」可区分即可；而相邻迭代若落在 bf16 不可区分区（>256）才是致命的——循环值域保证这一点（[test/distributed/ce_ordering.py:38-40](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/ce_ordering.py#L38-L40)）。

### 4.3 窗口读写竞态

#### 4.3.1 概念说明

窗口是**单缓冲**的：下一次用同一窗口的调用会直接覆盖上一次的结果。收尾屏障证明的是「每个 peer 的**写**已落地」，它对任何人的**读**不做任何证明。而在零拷贝路径（`out=` 来自 `empty_output()`）上，调用方读取结果的时刻是算子完全看不见的——于是防护只能放在**第 i+1 次调用的开头**：开路屏障让「写者们等读者们」，peer 的拷贝引擎被链在它自己的开路屏障之后。

竞态窗口可以写成：本 rank 对第 \( i \) 次结果的读 \( R^r_i \) 与 peer \( p \) 的第 \( i+1 \) 次写 \( W^p_{i+1} \) 之间没有任何流序；库承诺的只是**当 \( R^r_i \) 排在调用者流上时**，第 \( i+1 \) 次调用的开路屏障在流序上位于 \( R^r_i \) 之后，从而 \( W^p_{i+1} \) 不可能越过屏障先落。读若放在别的流上（与调用者流无序），承诺不成立——这正是 armed 相位故意做的事。

window_race 的两相位结构：**ARMED** 把同样的延迟和同样的读放到一条旁路流上（与后续调用无序），并让调用者流恰好把**一个**后续调用排到未决读之前——它**必须**在每个 rank 上撕裂，否则说明压舱已经短于 peer 的下一次调用，CLEAN 相位在毫无屏障时也能通过（BLIND）。**CLEAN** 把同样的读放回调用者流——它必须不撕，这是性质本身，也是库唯一承诺过的读者。

#### 4.3.2 核心流程

```
ARMED（每迭代 i）：
  fill(v) → all_to_all_4d(out=窗口)          # 调用者流
  current_stream().wait_stream(side)          # 只等上一迭代的读 → lag 恰好一个调用
  side.wait_stream(current_stream())          # 本迭代的读排在本调用屏障之后
  side 流: 压舱 GEMM 链 + 读 peer_lo/peer_hi ≠ v 计数
  # 调用者流刻意不等本迭代的读 → 下一调用的拷贝会落到还在排队的读上

CLEAN（每迭代 i）：
  fill(v) → all_to_all_4d(out=窗口)
  if rank == i % ws: 压舱 GEMM 链（旋转的慢读者）
  在调用者流上读 peer_lo/peer_hi ≠ v         # 流序上排在第 i+1 调用的开路屏障之前

带外检查：
  epoch 差: e1-e0 == 2*ITERS 且 e2-e1 == 2*ITERS   # 每调用恰好两道屏障
  普通张量探针 epoch_debug(...) == -1              # 证明全程走了零拷贝路径
  skewed 全组总和 == ITERS                          # 旋转压舱未被对称化
归约：按 rank 统计 armed_torn==0 的个数（blind_ranks 必须为 0）
```

#### 4.3.3 源码精读

被测的防护本身在 [src/bindings.cc:198-205](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L198-L205)——注释直接标题式写明「WRITERS WAIT FOR READERS」，并解释为什么守在下一调用开头而非上一调用结尾（零拷贝结果的读取时刻算子看不见）：

```cpp
// WRITERS WAIT FOR READERS, before writing anything. The window is single-buffered, so this
// call is about to overwrite what the previous one produced, which a peer may still be
// reading; ... It guards the START of a call rather than the end of the previous one because on
// the zero-copy path the result is read by the caller at a time the operator never sees.
fast_barrier(stream, call.win->flag_ptrs, rank);
launch_a2a_ce(...);
```

worker 侧的关键片段：

[test/distributed/window_race.py:5-14](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/window_race.py#L5-L14) —— 性质陈述：保证位于 `transfer_on_stream` 的第一道 `fast_barrier`（并指出它之所以有用，还依赖 peer 的拷贝被链在 peer 自己的开路屏障之后这一事实）。

[test/distributed/window_race.py:162-163](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/window_race.py#L162-L163) —— 只读 **peer 分片**：mode 0 下聚合序列轴按源 rank 排列，本 rank 自己那份由**本 rank 在调用者流上**的同设备拷贝写入，同流读天然有序、永远不会撕；把它算进比较会让 armed 在纯本地写上撕裂、而真正被开路屏障守护的 peer 路径反而没被测量。

[test/distributed/window_race.py:194-210](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/window_race.py#L194-L210) —— ARMED 循环。两个 `wait_stream` 精确界定 lag：第 204 行等的是**上一迭代**的读（把落后量钳在一个调用），第 205 行让本迭代的读排在本调用之后；调用者流随后刻意不等本迭代的读——这就是「武装」。

[test/distributed/window_race.py:221-237](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/window_race.py#L221-L237) —— CLEAN 循环：旋转压舱 + 在调用者流上读，注释点明「调用 i+1 的开路屏障在流序上位于它之后——这就是整个性质」。

[test/distributed/window_race.py:245-252](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/window_race.py#L245-L252) —— epoch 等式：每调用两道屏障，两个相位各推进 `2*ITERS`。它是**必要**检查但**不是**那个检查——docstring 花了一整段说明 epoch 对**位置**失明（把开路屏障挪到 launch 之后，epoch 照样 +2 而性质已死），所以它不能取代数据检查、也不能被当作冗余删掉（[test/distributed/window_race.py:85-92](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/window_race.py#L85-L92)）。

[test/distributed/window_race.py:257-263](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/window_race.py#L257-L263) —— 零拷贝路径探测器：拿一个普通张量探测 `epoch_debug` 必须返回 -1。探针解析逻辑在 [src/group.cc:424-442](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L424-L442)：先查 `owned_`（empty_output 缓冲的表），查不到再查 `windows_[(role, dtype)]`，仍查不到返回 -1。内部 sync 窗口只在某次调用**没走**零拷贝路径时才会被分配——所以 -1 恰好证明「peers 写的就是我们读的这块缓冲」。

[test/distributed/window_race.py:266-276](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/window_race.py#L266-L276) —— 归约方式：把 `armed_torn == 0` 作为**每 rank 一票**计数（blind_ranks），不是对 armed 求和。「一次控制只对跑它的那个 rank 有发言权」——每个 rank 的 CLEAN 只被它自己的 ARMED 担保。

docstring 的失明目录（[test/distributed/window_race.py:33-83](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/window_race.py#L33-L83)）逐条列出会让这个 worker 在「开路屏障被删」的构建上仍然通过的做法——对称压舱、删压舱、lag 累积、算上自己的分片、求和归约、读副本、不用窗口、循环内主机读、循环内任何集合通信、常量填充——以及各自的机械探测器（`skewed` 计数、统一 `ballast_chain`、epoch 对、-1 探针）。它还给出**进程外**的负控制：删掉 `transfer_on_stream` 两道 `fast_barrier` 里的**第一道**并重编译，CLEAN 必须撕、epoch 差必须减半；且提醒 `all_to_all_4d_timed` 内联着同一对屏障（[src/bindings.cc:639-643](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L639-L643)），bisect 的构建不能只补一处。

#### 4.3.4 代码实践（源码阅读型）

1. **实践目标**：写出 ARMED 控制为什么必须**在每个 rank 上**撕裂，并画出单迭代时间线。
2. **操作步骤**：
   - 对 rank r 画时间线：调用者流上 `call_i → (不等 read_i) → call_{i+1}`；旁路流上 `read_i`。标注 [test/distributed/window_race.py:204-205](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/window_race.py#L204-L205) 两个 `wait_stream` 各自界定了什么。
   - 写一段注释回答：若某 rank 的 ARMED 从不撕裂（armed_torn==0），这个 rank 的 CLEAN 通过还能信吗？归约时如何区分「rank r 瞎了」与「全组瞎了」？
   - 对照 ce_ordering 的求和归约（4.1.5 练习 3），写两三句说明两者差异的根源。
3. **需要观察的现象**：时间线上 `call_{i+1}` 的 peer 拷贝与旁路流的 `read_i` 无序重叠——竞态窗口打开。
4. **预期结果**：要点——每 rank 的时序条件不同（它在旋转中的位置、它窗口被谁写），一个 rank 的撕裂不能为另一个 rank 的时序担保；所以 blind_ranks 必须==0。ce_ordering 的求和是因为 skew 围绕的 rank 0 结构性地不容易撕，per-rank 会在好构建上误报 BLIND；window_race 的旋转使每 rank 地位对称，per-rank 是可达的更严格形式。
5. 多卡运行 `torchrun --nproc_per_node=4 test/distributed/window_race.py` 观察 PASS 行（`control tore N/256 unordered reads, clean 0 ... epoch +2/call`）：待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：ARMED 相位为什么必须在旁路流上读，而不是「在调用者流上读但多加几个 GEMM」？

**答案**：调用者流上的任何操作都被流序排在 `call_{i+1}` 的开路屏障之前——那正是被测的保证，读永远安全，armed 永远不撕，控制失效。武装的本质是**破坏读与下一调用之间的流序**（把读放到无序的旁路流），而不是单纯拉长时间（[test/distributed/window_race.py:22-27](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/window_race.py#L22-L27)）。

**练习 2**：把 [test/distributed/window_race.py:204](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/window_race.py#L204) 的 `current_stream().wait_stream(side)` 删掉会怎样？

**答案**：旁路流每迭代再落后一整条压舱，到第 3 轮读已被几十个调用甩开——armed 在「没有任何真实读者会经历的落后量」上撕裂、保持绿码，而压舱对 CLEAN 相位来说可能短了一个数量级。这等于造出了一个**不会失败的控制**，与没有控制同义（失明目录第三条，[test/distributed/window_race.py:45-51](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/window_race.py#L45-L51)）。

**练习 3**：为什么这个 worker 打破 correctness.py 的「检查、然后 dist.barrier()」节奏，把所有归约推迟到两个循环之后？

**答案**：循环内任何集合调用（`dist.barrier`、对计数器的 all_reduce、用 dist 算参照）要么在读之前直接阻塞 peers、要么在读之后把大家重新对齐——两种都毁掉「恰好一个调用领先于未决读」的武装几何，也会让 CLEAN 的旋转压舱失效。每 rank 无论看到什么都跑满全部迭代、每轮结果留在设备上循环后一次读走，是让这个偏离安全的纪律（[test/distributed/window_race.py:75-79](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/window_race.py#L75-L79)、[test/distributed/window_race.py:106-108](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/window_race.py#L106-L108)）。

### 4.4 双流窗口隔离与 CUDA graph 的 epoch 断言

#### 4.4.1 概念说明

docs/api.md 说「sync 与 async 调用不共享窗口，所以混用是安全的」。overlapping_barriers 就是这句话的证据，检验的对象是唯一可能让它为假的条件：async 调用还在高优先级 comm 流上飞行时，sync 调用在无序的调用者流上发出——两把 `barrier_kernel` 同时驻留，各自在**自己**分配的 signal pad 里自旋。窗口按 `(role, dtype)` 分键（u4-l2），这正是 v0.2 把两个角色拆成两个窗口的原因；docstring 记载参考实现在完全相同的形状上 reproduced 两个真 bug：非原子的 epoch 读写让两内核认领同一 epoch、握手变 no-op、**挂起**；改成原子后 epoch 唯一但两集合间**排序不一致**（rank X 给 async 3/sync 4，rank Y 相反），X 等 Y 为另一次调用发布的 flag，结果撕裂。所以握手状态必须 per-window，而 worker 用三重检查证明隔离。

cudagraph 则守住 u5-l3 讲过的隐患：若 epoch 是**主机**算出来的，捕获时它被固化成图里的常量，每次重放宣布同一个 E，peer flags 里还留着上一轮的 E，`while (v < epoch)` 自旋被陈旧状态直接满足——握手静默变 no-op，peers 可以在我们读窗口时覆盖它。`barrier_kernel` 在**设备侧** atomicAdd 推进 epoch，重放天然拿到新值；worker 的断言是必要检查：**每次重放 epoch 恰好 +2**（每调用两道屏障），等式而非「>0」——删掉开路屏障的构建每次只 +1，任何放宽形式都放过它。

#### 4.4.2 核心流程

overlapping_barriers 的主循环（每轮）：

```
if rank == i % ws: _sleep(200k cycles)        # 迟到 rank：其 peers 将同时坐进两把自旋内核
[采样轮: mark0 于 caller 流]                   # 本轮提交一切之前
handle = all_to_all_4d_async(q, mode=0)        # 必须最先发（staging 在 caller 流上拷输入并令
                                               # comm 流等它 → 后发的 async 会被整个 sync 调用排序）
[采样轮: mark1 于 comm 流、mark2 于 caller 流]  # mark2 紧贴 sync 调用，中间不得插入任何排序
k1_out = all_to_all_4d(k1, mode=0)             # sync：调用者流
k2_out = all_to_all_4d(k2, mode=0)             # sync 窗口在 async 还活着时被复用
if handle.completed: early_wait += 1           # 活性检查的主机半边：隐式等待的探测器
q_out = handle.wait()                          # 最后，GPU 侧等待
三组 (out != want) 计数 + min/max 留在设备上   # 循环内零主机读、零集合调用
```

三重检查（缺一即盲）：

| 检查 | 断言 | 性质 | 负控制（合并 kAsyncWindow→kSyncWindow）下的预期读数 |
| --- | --- | --- | --- |
| 数据 | 2000 轮中每个结果逐位等于本调用的常量（`!=` 计数，无容差） | 充分不必要，需要时序咬合 | 撕裂/串值，或挂起 |
| epoch | sync 探针恰 `+4N`、async 探针恰 `+2N`（每轮 2 sync + 1 async，各两道屏障） | 必要、免疫时序 | 键未分配：sync 读 6N、async 读 -1；同分配双键：两边 6N |
| 重叠活性 | 采样轮 mark2 落在 async 设备寿命**前半**（中点判据）且 `handle.completed` 从未提前 | 证明确实测了并发 | 不变；序列化运行自己报 BLIND |

cudagraph 的循环（每次重放）：`x.normal_()`（新输入，陈旧窗口=错答案而非同答案）→ **先**算集体参照（它会重新对齐各 rank，所以必须在压舱之前）→ rank 0 跑 GEMM 链 → `graph.replay()` → `torch.equal` 位精确比较 → 采样 epoch。收尾断言相邻采样差全为 2。

#### 4.4.3 源码精读

[test/distributed/overlapping_barriers.py:45-66](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/overlapping_barriers.py#L45-L66) —— 三重检查的完整陈述。epoch 检查点名「等式是指纹」：合并角色的构建在 sync 探针上读 6N，任何 `>=`、`>0`、「有推进」形式都放行（失明清单第 4 条）；数据检查说明共享窗口的表现是「一次调用的载荷出现在另一次的结果里」；活性检查说明为什么中点而非 `span > issued`——async 不可能早于 staging 拷贝开始，而 mark2 紧随其后，所以「sync 在 async 结束前发出」对任何**序列化**运行都成立，作为判据是空洞的。

[test/distributed/overlapping_barriers.py:30-43](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/overlapping_barriers.py#L30-L43) —— 两个顺序决定：**async 必须先发**（stage 在 caller 流上拷输入、comm 流等该拷贝，后发的 async 被前面的 sync 调用整体排序，永无共驻留，文件变空洞）；**两个调用都走 copying 路径**（`empty_output()` 缓冲是 `owned_` 里的独立窗口，调用拿到它就不再碰角色窗口，且 epoch 探针经 `owned_` 解析会读错计数器——[src/group.cc:429](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L429) 先查 owned_）。

[test/distributed/overlapping_barriers.py:257-272](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/overlapping_barriers.py#L257-L272) —— 主循环的发射次序与两个活性半检查：`sample[1].record(group._comm_stream)` 在 async 自己的流上、`sample[2].record()` 紧贴 sync 调用（注释警告 mark 与调用之间不得插入任何把两流排序起来的东西，否则对间隙检查不可见）；`handle.completed` 在本轮 wait **之前**读——aten 算子会隐式等待，已经 completed 说明 caller 流在 sync 发出前就被排到 comm 流后面了。

[test/distributed/overlapping_barriers.py:284-295](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/overlapping_barriers.py#L284-L295) —— epoch 等式断言与失败信息（直接把「合并窗口报 6N」写进 FAIL 文案）。

[test/distributed/overlapping_barriers.py:327-340](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/overlapping_barriers.py#L327-L340) —— 活性判据的计算：两个采样都从 caller 流上同一基线**向前**量（不依赖 `cudaEventElapsedTime` 跨流的符号语义）；`span` 是 async 的整段设备寿命，`issued` 是 caller 流在提交 sync 前深入其中的距离，中点判据 `span > 2.0 * issued`。

[test/distributed/overlapping_barriers.py:158-172](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/overlapping_barriers.py#L158-L172) —— 看门狗：自旋内核无超时、`cudaStreamSynchronize` 不返回错误、栈中无任何东西 raise（「违反契约就挂起，没有 raise 没有超时」），所以 worker 自己给墙钟设界，用 daemon 线程 + `os._exit`（主线程卡在永不返回的 CUDA 调用里、torch 在那里释放 GIL；正常解释器 teardown 会在 NCCL 析构里二次卡死）。**TIMEOUT 是失败**，不是基础设施抖动。

[test/distributed/cudagraph.py:5-14](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/cudagraph.py#L5-L14) —— 「捕获成功证明不了任何事」的开篇与主机常量 epoch 的危险机理；点名设备侧 atomicAdd 正是为了让重放拿到新鲜 epoch。

[test/distributed/cudagraph.py:97-113](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/cudagraph.py#L97-L113) —— 重放循环：`x.normal_()` 让陈旧窗口成为**错**答案；参照是集体操作会重新对齐所有人，所以排在压舱**之前**；`torch.equal` 零容差。

[test/distributed/cudagraph.py:115-130](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/cudagraph.py#L115-L130) —— epoch 等式：相邻采样差必须全为 2，并写明「屏障数若变，改常量；不要放宽形式」。

设备侧的配合实现在 [src/barrier.cu:31-49](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/barrier.cu#L31-L49)：线程 0 对 `local + ws`（flags 数组后面那个槽）做 atomicAdd 取得本轮 epoch，随后各线程向 peer 的 `flags[rank]` 做 `red.release.sys.max` 发布、再 `ld.acquire.sys` 自旋等自己的 `flags[t]`。窗口分配时握手区被清零、epoch 从 0 起步（[src/group.cc:246-251](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L246-L251)），而 `epoch()` 探针正是从 `flag_ptrs[rank] + ws*8` 这个槽同步读回（[src/group.cc:437-441](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L437-L441)）——绑定暴露为 `epoch_debug`，注释同样写明「撕裂数据是握手死掉的**充分**信号而非必要信号，只有 epoch 说得清屏障是否活着」（[src/bindings.cc:683-691](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L683-L691)）。

#### 4.4.4 代码实践（源码阅读型）

1. **实践目标**：推导「合并窗口」负控制的三种可能结局，并解释 cudagraph 的参照为何必须排在压舱之前。
2. **操作步骤**：
   - 读 [test/distributed/overlapping_barriers.py:117-121](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/overlapping_barriers.py#L117-L121)：把 `src/bindings.cc:302` 处的 `kAsyncWindow` 改成 `kSyncWindow` 重编译（**实验后务必还原**——这是你自己的工作副本，不要提交）。
   - 对照 4.4.2 的表格写出三种结局各自触发哪条打印（FAIL 撕裂 / FAIL epoch / TIMEOUT）。
   - 读 [test/distributed/cudagraph.py:100-107](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/cudagraph.py#L100-L107)，写出把 `reference_even` 挪到压舱**之后**会发生什么。
3. **需要观察的现象**：纸面推导即可；若重编译并运行（≥2 GPU），观察 worker 打印的是三种结局中的哪一种。
4. **预期结果**：合并窗口后——两个探针里 async 侧读到 -1（`(kAsyncWindow, bf16)` 键从未分配）或两侧同读 6N，数据撕裂或看门狗超时（epoch 撞车正是参考实现**挂死**的方式），三种之一必须出现，否则该循环失明、PASS 无意义。参照后置：集体参照把所有 rank 重新对齐，压舱制造的「其他 rank 领先」被抹掉，重放又在整齐到达下进行——死屏障也能通过，数据检查空转。
5. 实际运行与重编译实验：待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：overlapping_barriers 的失明清单第 13 条说「按 rank 奇偶交替谁先发」会在健康构建上死锁——为什么？

**答案**：两次调用的远端拷贝都串行在**一条**组级 xfer 流上。若 rank A 先发 sync、rank B 先发 async：A 的 async 屏障排在自己那次 sync 调用后面，而 A 的 sync 屏障在等 B 的 async 发布、B 的 async 屏障在等 A 的 sync 发布——环状等待。所以**主机提交次序必须在所有 rank 上一致**（[test/distributed/overlapping_barriers.py:112-115](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/overlapping_barriers.py#L112-L115)）。这也是 SPMD 契约在测试代码里的体现。

**练习 2**：为什么 cudagraph 的 epoch 断言必须是「恰好 +2」的等式，`>= 2` 或 `> 0` 为什么不行？

**答案**：删掉（或弄死）**一道**屏障的构建每次重放只 +1，「有推进」形式全放行；而「恰好 +2」同时验证了每调用两道屏障都在图里活着。同理 overlapping_barriers 用 4N/2N 的等式抓「合并成 6N」。等式是指纹，放宽即失明（[test/distributed/cudagraph.py:115-120](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/cudagraph.py#L115-L120)）。

**练习 3**：为什么 overlapping_barriers 的两个调用必须**同 dtype 同形状**？

**答案**：窗口按 `(role, dtype)` 分键。换成不同 dtype，即使角色被合并，两次调用也自然落在不同窗口上，负控制不再失败、隔离检查测不到合并；形状则不同——窗口按容量匹配，同 dtype 下不同形状仍可能是同一分配，但同形状还有更廉价的理由（同一个 plan、同一个 staging 缓冲、循环中不重分配窗口）（[test/distributed/overlapping_barriers.py:88-95](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/overlapping_barriers.py#L88-L95)、[test/distributed/overlapping_barriers.py:188-190](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/overlapping_barriers.py#L188-L190)）。

**练习 4**：在没有 `c10d::register_work` 的 libtorch 上（u7-l3 的 `CompletedHandle` 路径），overlapping_barriers 会怎样自处？

**答案**：热身阶段的第一次 async 调用返回 `CompletedHandle`，worker 立刻记下 blind_reason——`register_stream_completion` 在调用时刻就让 caller 流等待 comm 事件（[src/work.cc:73-79](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/work.cc#L73-L79)），「async」被完整排序在 sync 之前，不可能有共驻留；后续检查全部通过但什么都没测，所以直接判 BLIND 而非假装通过（[test/distributed/overlapping_barriers.py:211-219](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/overlapping_barriers.py#L211-L219)、失明清单第 10 条）。

## 5. 综合实践

本讲的综合实践是规格指定的**精读 + 论证**任务（源码阅读型，无需 GPU；有多卡时可加跑验证）。逐字精读 [test/distributed/ce_ordering.py:1-50](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/ce_ordering.py#L1-L50) 的 docstring 与 [test/distributed/window_race.py:1-109](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/window_race.py#L1-L109) 的失明目录，然后书面回答三个问题（每题答案应能在源码里指出依据行）：

**问题 1：负控制每次运行都执行，防止的是什么？**

参考答案要点：防止「测试变盲而不自知」。时序检查（CLEAN）通过有两种解释——性质成立，或仪器根本测不到该性质；不每次武装故障并要求它真的撕（`armed > 0`，[test/distributed/ce_ordering.py:123](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/ce_ordering.py#L123)），这两种解释无法区分。躺在注释里的控制会随时间被遗忘，等驱动/硬件/重构让测量链路失效时（比如某天 `_set_ce_fault` 的注入点因重构不再覆盖新路径），CLEAN 依旧绿灯，负控制恰恰在那一天缺席。BLIND 是退出码 1 的失败（[test/test_distributed.py:74-76](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/test_distributed.py#L74-L76) 也刻意保持它失败）。

**问题 2：若去掉不均匀分片带来的传输长度偏斜、改为让某个 rank 晚到（例如调用前 `torch.cuda._sleep`），测试为何会「通过但什么都没测」？**

参考答案要点：调用以开路屏障这一集体握手开场，它在任何数据移动前把所有 rank 重新对齐（[test/distributed/ce_ordering.py:33-37](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/ce_ordering.py#L33-L37)）。晚到的 rank 只是晚一点进握手，之后各 rank 传输长度相等、几乎同时完成——快 rank 到达收尾屏障、读 rank 0 窗口时，rank 0 的载荷早已落地，「flag 先于载荷」的场景从未在 CLEAN 相位出现。ARMED 相位也许仍会撕（注入压住了载荷），于是表面一切绿；但主检查测的性质从没被置于危险中——这正是该 worker 早期版本的真实历史。教训：**负控制保仪器，skew 保暴露**，两者独立失效、都必须独立成立。

**问题 3：window_race 的 armed 控制为什么必须**在每个 rank**上都撕裂？**

参考答案要点：一次控制只是「跑它的那个 rank」的时序证据——各 rank 在旋转压舱中的位置、其窗口的对端组合、设备时机都不同，rank A 的读被覆盖不能证明 rank B 的读也处于可被覆盖的时序（[test/distributed/window_race.py:59-62](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/window_race.py#L59-L62)、[test/distributed/window_race.py:266-276](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/window_race.py#L266-L276)）。所以归约的是「armed 保持沉默的 rank 数」（blind_ranks），且必须为 0：任何一个 rank 的 armed 没撕，说明压舱在该 rank 的时序下短于 peer 的下一次调用，该 rank 的 CLEAN 通过在毫无屏障时也会发生——它那一票是盲的。对照：ce_ordering 因 skew 围绕 rank 0 构造而不得不求和（4.1.5 练习 3），两份 docstring 把各自的取舍都写在了明面上。

**可选加练（≥2 GPU，待本地验证）**：
1. `torchrun --nproc_per_node=<N> test/distributed/ce_ordering.py` 与 `... window_race.py`，把 PASS 行里的控制组撕裂计数（`tore X/ITERS*ws`）记下来，确认负控制真的撕了。
2. 进程外负控制：注释掉 [src/bindings.cc:203](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L203) 的第一道 `fast_barrier`（连带 [src/bindings.cc:639](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L639) 的同位屏障，二者必须同删）重新构建，重跑 window_race——预期 FAIL（CLEAN 撕裂且 epoch 差减半）。**做完必须还原全部改动。**

## 6. 本讲小结

- **CE 可见性是假设不是保证**：流序的「completed」是主机侧属性、跨设备排序保证只覆盖 NULL 流、PTX `.release` 只对当前线程先前的操作排序。ce_ordering 的 CLEAN 只证明「本机本形状」，它的可信度全部来自每次运行都武装的负控制：`_set_ce_fault` 同时压住载荷并跳过流汇合，读者必须读到陈旧字节，否则整场判 BLIND。
- **skew 的落点必须匹配被测性质**：到达偏斜会被开路握手抹平（ce_ordering 的历史教训），传输长度偏斜才能制造「flag 先于载荷」；读者侧旋转压舱制造「下一调用覆盖未决读」；提交时刻的睡眠制造屏障内核共驻留；rank 0 的 GEMM 链让重放错峰。bf16 只有 8 个有效位，测试值必须在 1..128 循环。
- **读写竞态的防护在下一调用的开头**：收尾屏障只证明写不证明读，零拷贝结果的读取时刻算子看不见，所以「写者等读者」的开路屏障是唯一正确的位置。window_race 用旁路流的 armed 读（必须每 rank 撕）+ 调用者流的 clean 读（必须不撕）+ epoch 恰 +2/调用 + 普通探针 -1 + skewed 计数器，把性质、仪器、路径三件事分别钉住。
- **双流隔离靠三重检查**：位精确数据（充分）、epoch 等式 4N/2N（必要、免疫时序、专抓窗口合并的 6N/-1 指纹）、重叠活性（中点判据 + completed 探针，防「结构正确但全程序列化」）。epoch 等式不可放宽为 `>=`。
- **CUDA graph 的 epoch 必须在设备侧推进**：主机算出的 epoch 会被捕获固化成常量、重放被陈旧状态满足；`barrier_kernel` 的 atomicAdd 让每次重放拿到新鲜值，cudagraph 以「相邻采样差恰为 2」的等式钉死它。
- **BLIND 是失败不是跳过**：这些 worker 对「自己测不到」的处理是退出码 1（区别于机器原因的 NVLINK BLIND 转 skip）；挂起是另一种失败形态——自旋内核无超时、栈中无 raise，所以 worker 自带看门狗，且测试编排用 SIGTERM→killpg 兜底回收孤儿 rank。

## 7. 下一步学习建议

- 下一讲 **u9-l4（基准方法论）**：这些对抗性 worker 教会你的「时序必须被制造、数字必须可判读」会直接延续到 bench_a2a.py 的 stages/overlap/zerosm 模式与 exclusive.sh 的独占执行纪律——尤其 zerosm 模式的两列联读法与本讲「单一检查不足为凭」一脉相承。
- 之后 **u9-l5（设计反思）**：把 ce_ordering docstring 里「NVIDIA 自家以更弱顺序发布 / NCCL 换形状回避」的对照线接回 docs/design.md 的完整论证，复盘未文档化假设的边界与被移除的方案。
- 源码重读建议：带着本讲的框架回头读 [src/bindings.cc:194-211](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L194-L211)（两道屏障的注释即是三个 worker 的被测物）与 [src/barrier.cu:31-49](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/barrier.cu#L31-L49)（每个线程「发布的格子别人等、等待的格子别人写」的角色分工），再快速浏览 [test/distributed/correctness.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py) 的 `reference_even`——cudagraph 的位精确参照来自它（u9-l2 已精读，值得带着 skew 的视角再看一遍它为什么被排在压舱之前）。
