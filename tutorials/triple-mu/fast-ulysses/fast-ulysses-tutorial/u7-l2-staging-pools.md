# 暂存池：stage 与 release_staged

## 1. 本讲目标

上一讲（u7-l1）我们看到 `all_to_all_4d_async` 把整段「屏障 → 传输 → 屏障 → copy_out」流水线搬上组级 comm 流，与 caller 流上的计算重叠。但那里有一个被一笔带过的关键步骤：**输入在进入 comm 流之前，先被拷进了一块由 group 持有的暂存（staging）缓冲**。本讲回答三个问题：

1. 为什么输入必须先拷一份？直接让 comm 流读调用者的张量行不行？（答案：行，但代价是跨流生命周期问题——要么内存不安全，要么用 `record_stream` 把每个已释放的输入都钉到 comm 流追上为止。）
2. 暂存缓冲为什么是**按 (shape, dtype) 键的池**，每形状至多 4 槽、全局至多 16 个形状？第 5 个同形状并发调用会发生什么？
3. 释放事件（release event）为什么由一个 `noexcept` 函数记录，而且**在传输抛异常时也必须记录**？

学完本讲，你应该能独立读通 `stage()` 与 `release_staging()` 的每一个分支，并设计出验证池行为的测试。

## 2. 前置知识

- **caller 流与 comm 流**：u7-l1 已建立的两条互不排序的 CUDA 流。caller 流是用户代码当前所在的流（输入张量在这里产生、也在这里被读）；comm 流是 group 在构造时创建的最高优先级流（[python/fast_ulysses/group.py:94-95](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L94-L95)），异步交换全在这里执行。
- **CUDA 缓存分配器与 `record_stream`**：torch 的 GPU 内存不是每个 `free` 都真正还给驱动，而是回到 per-stream 的缓存池，**在同一条流上**复用。若一个张量在 A 流分配、又被 B 流使用，就必须调 `Tensor.record_stream(B)`，告诉分配器「B 还在用这块内存，A 流上释放后别急着复用」。代价：这块内存会被一直钉住，直到 B 流推进到登记点之后。本讲的核心权衡正是「对每个输入张量做 `record_stream`」与「拷进一小池 group 自己的缓冲」之间的选择。
- **CUDA 事件的三个操作**：`cudaEventRecord(ev, stream)` 在流的当前位置插一个完成标记；`cudaEventQuery(ev)` 立即返回 `cudaSuccess`（已触发）或 `cudaErrorNotReady`（尚未触发），不阻塞；`cudaStreamWaitEvent(s, ev, 0)` 让流 s 在 ev 触发前不再前进，也是 GPU 侧的等待、不阻塞主机。一个从未被 record 过的事件，query 会返回 `cudaSuccess`——这在后文的扫描逻辑里正好是「从未被读过」的正确答案。
- **InferenceMode（推理模式）**：`torch.inference_mode()` 下创建的张量叫**推理张量**，它不记录版本计数器，且这个身份是**永久的**——之后在推理模式之外向它做 `copy_` 会直接 raise。这是 C++ 侧一个容易踩的坑，本讲会看到 `stage()` 如何显式绕开。
- **scope guard（作用域守卫）**：一个析构函数里做清理工作的栈对象。C++ 的栈对象在离开作用域时析构，**即使是因为异常展开（exception unwinding）离开**——这是「异常路径也要释放」的标准手段。
- 承接 u6-l2 的纪律：**一切可能抛错的检查先于第一个屏障**。本讲会看到它在异步路径上的又一处应用（检查先于 `stage()`）。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [include/fast_ulysses/group.hpp](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/group.hpp#L112-L199) | `Staging` 结构、`stage()`/`release_staging()`/`staging_slots()` 的契约注释、`kMaxStagingPerShape = 4` 与 `staging_` 池的声明 |
| [src/group.cc](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L320-L422) | `stage()`（拷贝 + 两个事件 + 三档槽选择）、`release_staging()`（noexcept 记录释放事件）、`staging_slots()`（测试探针） |
| [src/bindings.cc](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L176-L191) | `StagingRelease` scope guard 与 `run_staged()`（stage 的唯一调用方） |
| [python/fast_ulysses/group.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L151-L232) | `all_to_all_4d_async`：捕获 caller 流、把流 id 传进 C++、输出侧的 `record_stream` |
| [test/distributed/correctness.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py#L185-L219) | 「5 个同形状异步调用并发」实测：池增长序列断言 `[1, 2, 3, 4, 4]` |
| [include/fast_ulysses/common.hpp](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/common.hpp#L28-L57) | `Event` RAII 包装：`stage()` 每次调用新建的 ready 事件由它托管 |

注意一个分层事实：**staging 缓冲不是对称内存**。对称内存（u4 系列讲的窗口）是 peers 直写的输出通道；staging 只是普通 `at::empty` 分配的**输入副本**，从不上跨进程——它是纯粹的流间隔离装置。

## 4. 核心概念与源码讲解

### 4.1 stage 拷贝与事件：为什么输入必须先拷一份

#### 4.1.1 概念说明

设想不拷贝：`all_to_all_4d_async(x)` 直接把 `x.data_ptr()` 交给 comm 流上的 CE 拷贝。有两个问题。

**问题一（内存安全）**：`x` 是 caller 流上的张量。Python 侧 `all_to_all_4d_async` 返回后，用户可能立刻让 `x` 离开作用域——分配器看到 caller 流上已无引用，就把这块内存放回缓存池，**在同一条 caller 流上**复用。而 comm 流上的交换可能还没开始读它。两条流互不排序，复用写入与交换读取就会竞争。

**问题二（`record_stream` 的代价）**：正统修法是对 `x` 调 `record_stream(comm)`——分配器从此不敢复用这块内存，直到 comm 流推进过登记点。安全了，但**每个**异步输入都被钉住一段不可控的时间；对推理循环里每步一批新输入的形态，钉住的内存量随发行节奏累积。源码注释原话：「record_stream would instead pin every freed input until the comm stream caught up」（[src/bindings.cc:389-392](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L389-L392)）。

`stage()` 选第三条路：**在 caller 流上**把 `x` 拷进一块 group 自己持有、永不提前释放的缓冲；comm 流从头到尾只看这块副本。caller 的张量只被 caller 流读，comm 的读取对象生命周期由 group 管理。一次拷贝换来两条流之间唯一的耦合点——两个事件。

附带一个红利：这个拷贝**顺便吸收了非连续输入**。`run_staged` 刻意不调 `contiguous()`——那会把重排拷贝发射到 comm 流上，恰恰变成一次「没人排序过的跨流读取」；`stage()` 里的 `copy_` 在 caller 流上做同样的事，且时序正确。

#### 4.1.2 核心流程

一次异步调用中，staging 相关的时序（`ready` 是每次调用新建的事件；`release` 是随槽位存活的事件）：

```text
caller 流:  ... 生产 x ...
            slot->tensor.copy_(x)          ← stage() 发起的暂存拷贝
            cudaEventRecord(ready, caller)
comm  流:   cudaStreamWaitEvent(comm, ready)   ← 副本就绪前不开读
            开路屏障 → CE 传输（读 slot->tensor）→ 收尾屏障 → copy_out
            cudaEventRecord(slot->release, comm)  ← release_staging() 记录：
                                                    comm 已读完，槽可复用
（之后某次同形状调用）
caller 流:  cudaEventQuery(slot->release) == cudaSuccess ?
            → 是：直接复用该槽          ← 零成本路径
```

两个事件各管一个方向：`ready` 让 **comm 等 caller**（副本写完才能读）；`release` 让 **caller 等 comm**（comm 读完才能覆写）。二者合起来把「一条流上的写入」安全地移交给「另一条流上的读取」，再把缓冲安全地拿回来。

#### 4.1.3 源码精读

先看唯一调用方 `run_staged`，确认 stage 落在哪一步之前：

[src/bindings.cc:374-395](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L374-L395)：`run_staged` 的主体。按顺序：`check_alive` / `require_cuda` / `require_group_device` 三个检查**先于** `stage()`——注释给出两个理由：destroy 之后再建的 staging 条目永远不会被清理、事件随 group 泄漏；而且暂存缓冲按 (shape, dtype) 缓存却取**输入的设备**，一次错设备的调用会在错误设备上留下缓冲。随后 `comm = getCurrentCUDAStream()`（Python 已把 comm 流设为当前流），再从两个整数重建 caller 流（u7-l1 讲过的 `Stream::unpack3`），最后 `stage(input, caller, comm)`，返回的 `slot->tensor` 作为传输输入传给 `run()`。

stage 的拷贝与 ready 事件本体在 `stage()` 尾部：

[src/group.cc:340-401](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L340-L401)：`const c10::cuda::CUDAStreamGuard guard(caller);` 之后的一切都发生在 **caller 的流**上——注释明确写着「caller 的张量只在这里被读，comm 流只看到暂存副本」。尾部三行是本模块的核心：

```cpp
slot->tensor.copy_(x);                        // 暂存拷贝，caller 流
const Event ready(cudaEventDisableTiming);    // 每次调用新建，RAII 托管
ULYSSES_CUDA_CHECK(cudaEventRecord(ready, caller));
ULYSSES_CUDA_CHECK(cudaStreamWaitEvent(comm, ready, 0));
```

注意 ready 事件「trailing everything already submitted on the caller's stream—including any earlier consumer of this same buffer」：它排在 caller 流当前尾部，所以连**同槽上一位读者的残留工作**也被一并覆盖进等待里。

`Event` 是 [include/fast_ulysses/common.hpp:33-57](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/common.hpp#L33-L57) 的 RAII 包装，在 `stage()` 返回时析构。此时它已被 record 且 comm 已挂上等待；CUDA 对「已完成记录、尚有等待挂着」的事件的销毁是延迟到引用全部完成的，所以提前销毁句柄是安全的——这也是作者用手写 `cudaEventCreateWithFlags`/`cudaEventDestroy` 配对换成 RAII 的原因之一（见该类注释：手工配对在抛异常的调用上每次泄漏一个事件）。

最后看非连续输入的实测：[test/distributed/correctness.py:167-177](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py#L167-L177)：构造一个 `wide[..., :128]` 的跨步张量，断言它确实非连续（「built a contiguous tensor; it tests nothing」——测试自己的负控制），然后验证 async 结果与 sync 参照位精确一致。这个用例存在的原因就是 4.1.1 说的「暂存拷贝吸收跨步读取」。

#### 4.1.4 代码实践

**实践目标**：亲手验证「不暂存」与「暂存」两种方案在内存安全上的差别，并跟踪 ready/release 两个事件的去向。

**操作步骤**（源码阅读型，无需 GPU）：

1. 读 [src/bindings.cc:389-392](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L389-L392) 的注释，列出「直接把 x 交给 comm 流」需要补的两个动作（对 x 记录版本、对 x `record_stream`），并写出各自的代价。
2. 在纸上画两条流的时间轴，标注：`copy_`、`ready` 的 record 与 wait、开路屏障、CE 传输、收尾屏障、`release` 的 record。标出「comm 流读 slot->tensor」的区间被哪两个事件夹住。
3. 对照 [python/fast_ulysses/group.py:227-231](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L227-L231)：输出侧 `y.record_stream(comm)` 与 `y.record_stream(caller)`——注意**输出**用的是 record_stream 方案而**输入**用的是暂存方案。思考为什么不对称（提示：输出是调用者要长期持有的结果，钉住它是语义本身；输入是每步都换的临时量，钉住它是纯开销）。

**需要观察的现象**：两份注释对 `record_stream` 的表述一正一反，能互相印证。

**预期结果**：能说出「输入暂存、输出 record_stream」这个不对称设计的理由。运行时验证（步骤 2 的时间轴与 nsys trace 对照）待本地验证（需 GPU）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `cudaStreamWaitEvent(comm, ready, 0)` 这一行删掉，最坏会发生什么？

**答案**：comm 流不再等暂存拷贝完成。CE 拷贝可能在 `copy_` 写入之前或进行中读取 `slot->tensor`，读到旧数据或半新半旧的数据——静默的数据错误，不报错。特别是复用槽位时（4.2），旧一轮数据还在，错误结果看起来完全合法。

**练习 2**：ready 事件为什么每次调用新建（RAII），而 release 事件随槽位常驻？

**答案**：ready 的语义是「caller 流**当前尾部**已到」——每次调用尾部不同，事件必须新鲜；用完即弃，交给 RAII 最省事。release 的语义是「comm 流已读完**这个槽**」——它描述的是槽位状态而非某次调用，要与槽同生命周期，供后续所有同形状调用 query。

### 4.2 按 (shape, dtype) 的池：结构、扫描复用与 16 形状上限

#### 4.2.1 概念说明

有了单个暂存缓冲，最朴素的实现是「一个 shape 一个槽」。但头文件注释点出要害（[include/fast_ulysses/group.hpp:121-126](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/group.hpp#L121-L126)）：**共享一个槽仍然是正确的，只是不并发**——第二个调用的暂存拷贝会在 caller 流上等第一个交换退休，把「提前发行以重叠的计算」重新串行化。池存在的理由是**并发**而非正确性：把一次打包交换拆成 q/k/v 三次同形状异步调用时（这正是 lend 轮转选 4 的同一动机），三次应当同时在天上。

于是结构是：`staging_` 是一个 map，键 `(shape 向量, dtype)`，值是**槽位池**（`unique_ptr<Staging>` 的向量）。选择逻辑三档（详见 4.3 的分支图）：扫到一个 comm 流已读完的槽就复用；没有且未到上限就新建；没有且到上限就等待。

两个结构性细节值得先记：

- **`unique_ptr` 而非值存储**：调用方（`run_staged`）在整段传输期间持有裸 `Staging*`；若向量按值存 `Staging`，`push_back` 扩容会搬移对象、使已发出去的指针悬空。`unique_ptr` 保证指向的对象永不动。
- **16 形状上限**：与 `plans_` 的 512 条上限同一动机（防数据依赖形状无限增长），但这里每个条目钉着**真金白银的显存**（每形状最多 4 个整输入大小的缓冲），所以界更紧。超界时的动作是「全清」：先 `cudaEventSynchronize` 每个槽的 release 事件（**主机阻塞**，等 comm 流确实读完），销毁事件，然后 `staging_.clear()`。注释解释了为什么此时同步是安全的：`release_staging()` 在 `run_staged` 的 scope guard 里已经对**每个**已发行调用执行过，同步等到的必然是已记录的事件。

#### 4.2.2 核心流程

```text
stage(x, caller, comm):
  key = (x.shape, x.dtype)
  若 staging_ 已有 16 个形状且 key 不在其中:
      对每个槽: cudaEventSynchronize(release); cudaEventDestroy(release)   # 主机阻塞
      staging_.clear()                                # 全清，下次调用重建
  pool = staging_[key]

  CUDAStreamGuard(caller)                              # 以下全在 caller 流

  slot = null
  for candidate in pool:                               # 第一档：扫描复用
      if cudaEventQuery(candidate->release) == cudaSuccess:
          slot = candidate; break                      # comm 已读完，白拿
      （NotReady 落进 sticky last error，需 cudaGetLastError() 清掉）

  if slot == null and pool.size() < 4:                 # 第二档：新建（见 4.3）
      ...
  elif slot == null:                                   # 第三档：池满等待（见 4.3）
      ...

  slot->tensor.copy_(x)                                # caller 流
  record(ready, caller); wait(comm, ready)             # comm 等副本
  return slot
```

#### 4.2.3 源码精读

池的声明与注释：[include/fast_ulysses/group.hpp:197-199](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/group.hpp#L197-L199)——`std::map<std::pair<std::vector<int64_t>, at::ScalarType>, std::vector<std::unique_ptr<Staging>>> staging_;`，注释即「unique_ptr 因为调用方跨传输持有 `Staging*`，池增长不得搬移已发出去的槽」。

键与 16 形状上限：[src/group.cc:322-338](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L322-L338)。这段的注释把与 `plans_` 的差别讲得很清楚：「Unlike plans_, every entry here pins up to kMaxStagingPerShape inputs' worth of device memory, so the bound matters more」——plan 缓存超界清空的代价是每形状几微秒的重算，这里超界清空的代价是每个存活形状多付一次拷贝。

扫描循环：[src/group.cc:347-361](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L347-L361)：

```cpp
for (auto& candidate : pool) {
    const cudaError_t status = cudaEventQuery(candidate->release);
    if (status == cudaSuccess) { slot = candidate.get(); break; }
    if (status != cudaErrorNotReady) { ULYSSES_CUDA_CHECK(status); }
    (void)cudaGetLastError();
}
```

三处细节：`cudaSuccess` 即「comm 已读完（或从未记录过——从未被读，同样是空闲的正确答案）」；真正的错误码仍走检查宏抛出；而 `cudaErrorNotReady` 虽是本次调用的正常答案，却会落进 CUDA 的 sticky last error，被下一个不相关的 `cudaGetLastError()` 当成自己的错报出来——所以当场清掉，注释说这与 `at::cuda::CUDAEvent::query()` 的做法同源。

#### 4.2.4 代码实践

**实践目标**：确认池的键、上限与扫描语义，理解「复用是常态、新建是冷启动」。

**操作步骤**：

1. 读 [src/group.cc:327-337](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L327-L337)，回答：第 17 个新形状到达时，`cudaEventSynchronize` 阻塞的是主机还是 GPU？为什么此处敢阻塞（对照 4.2.1 提到的 scope guard 保证）？
2. 设想同一 (shape, dtype) 的调用以「issue 五个 → 全部 wait → 再 issue 五个」的节奏循环。写出第二轮五个调用各自走哪一档。
3. 若两个形状仅差一个维度（如 `(1,16,40,128)` 与 `(1,16,40,64)`），它们共用池吗？用键的构造（[src/group.cc:322](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L322)）说明理由。

**需要观察的现象 / 预期结果**（源码推理即可完成）：1. 阻塞主机；因为每个已发行调用的 release 事件都已记录，同步必有终点。2. 第二轮全部走第一档（第一轮 `wait()` 已让 caller 流等到 comm 完成，release 均已触发）。3. 不共用——键含完整 shape 向量。运行验证待本地验证。

#### 4.2.5 小练习与答案

**练习**：为什么 `staging_` 的值是 `vector<unique_ptr<Staging>>` 而不是 `vector<Staging>`？如果换成后者，哪个具体代码位置会首先出问题？

**答案**：`run_staged` 在 `stage()` 返回后一直持有 `Staging* slot`（[src/bindings.cc:393-395](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L393-L395)），并在 scope guard 里再次使用它。若按值存储，`pool.push_back(...)`（[src/group.cc:382](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L382)）引起向量扩容时对象被搬移，前一次调用还握着的指针指向已作废的内存——`release_staging(slot, comm)` 会把释放事件记到一块随机内存上。

### 4.3 kMaxStagingPerShape：每形状 4 槽上限、池满等待与新建槽的三个细节

#### 4.3.1 概念说明

常量在 [include/fast_ulysses/group.hpp:191-195](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/group.hpp#L191-L195)：`static constexpr size_t kMaxStagingPerShape = 4;`。选 4 的理由写在注释里：一次打包交换拆成 q/k/v 后还有余量；而**每个槽都钉着一份完整输入的显存**，过了界就让调用等待而不是无限增长——所以 16 形状的总账是 16 × 4 = 64 个输入大小的缓冲，显存开销有硬上界。

第 5 个并发调用走「池满等待」档：挑 `pool.front()`，在 **caller 流**上 `cudaStreamWaitEvent(caller, slot->release, 0)`。两个微妙点：

- 等待插在 caller 流上，意味着这次调用的暂存拷贝 `copy_` 被推迟到 comm 流读完 front 槽——阻塞是 GPU 侧的、且阻塞的是**发起调用这条流**，不是主机。这与头文件注释「the second call's copy waits, ON THE CALLER'S STREAM」精确对应：等待的位置本身就是损失（caller 流被拖住），但方向正确（不能让 comm 流等 caller 的拷贝，那会反噬所有在途交换）。
- 挑 `front()` 而非「最先退休的槽」：注释承认 front 是「第一个**建成**的槽，不是第一个会退休的」——扫描从它开始，所以它反而最常被复用。任何在途槽都是正确答案；要等得更短就得维护退休顺序，而这条路径只有超界的调用才会到达，不值得为它记账。

新建槽（第二档）则藏着三个容易忽略的工程细节，作者各写了一段注释：

1. **一起提交**：`tensor` 与 `release` 事件先建进局部，最后一起 `push_back`。若半成品进了池（张量有了、事件还是 null），这个槽会被发出、然后在 null 事件上炸掉，**永久**报废。
2. **`at::empty` 而非 `empty_like`**：`empty_like` 会复制输入的布局——一个跨步输入会得到一个跨步的暂存缓冲，而传输层把暂存缓冲当**密集**的读。
3. **`InferenceMode(false)`**：暂存缓冲寿命跨调用。若某次调用恰好在用户的 `torch.inference_mode()` 里发生，缓冲就成了永远的推理张量；此后所有在推理模式之外的 `copy_` 都 raise——**一次**这样的调用就永久报废这个 (shape, dtype)。显式包一层 `InferenceMode(false)` 保证缓冲永远是普通张量。

#### 4.3.2 核心流程

三档分支的完整判定（`slot` 为扫描结果）：

```text
if slot == nil 且 pool.size() < 4:        # 第二档：新建
    InferenceMode no_inference(false)     # 永远普通张量
    fresh = make_unique<Staging>()
    staged  = at::empty(x.sizes(), x.options())      # 密集，不用 empty_like
    cudaEventCreateWithFlags(&release, DisableTiming)
    fresh->tensor = staged; fresh->release = release # 局部建好
    pool.push_back(move(fresh))                      # 一起提交
    slot = pool.back().get()
elif slot == nil:                         # 第三档：池满等待
    slot = pool.front()
    cudaStreamWaitEvent(caller, slot->release, 0)    # GPU 侧，caller 流
# else: 第一档，扫描已复用
```

池的增长序列因此完全确定：同形状的第 1–4 个**互不等待的**调用依次新建 4 个槽；第 5 个等待。这正是 correctness.py 断言 `[1, 2, 3, 4, 4]` 的来源（`staging_slots_debug` 读的是池的**大小**，第 5 次不再增长）。

#### 4.3.3 源码精读

常量与总账注释：[include/fast_ulysses/group.hpp:191-199](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/group.hpp#L191-L199)。

新建分支：[src/group.cc:363-384](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L363-L384)，三段注释分别对应上面三个细节，关键三行：

```cpp
const c10::InferenceMode no_inference(false);
auto        fresh   = std::make_unique<Staging>();
at::Tensor  staged  = at::empty(x.sizes(), x.options());
ULYSSES_CUDA_CHECK(cudaEventCreateWithFlags(&release, cudaEventDisableTiming));
```

事件带 `cudaEventDisableTiming`：release 事件只用于「到了没有」的询问与等待，不需要时间戳，关掉计时让运行时可以选更便宜的实现（对比 u6-l2 里 `_timed` 算子的事件**必须**保留计时）。

池满分支：[src/group.cc:385-393](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L385-L393)。

实测：[test/distributed/correctness.py:191-218](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py#L191-L218)。这个测试是三档逻辑的完整端到端验证，三处设计值得精读：

- 用**专属形状**（`packed = [torch.randn(B, 8, 4*ws, 64, ...)]`），让计数从零开始而不是接续前面的调用；
- `torch.cuda.synchronize()` 后 `torch.cuda._sleep(100_000_000)`：睡眠内核**占住 caller 流**，comm 流又等 caller 流的 ready 事件，于是五个交换发行期间没有任何一个能退休——这是逼出第二、三档的前提。注释警告：若增长序列早早出现重复（如 `[1, 1, ...]`），说明调用根本没有并发，是一次「盲跑」（blind run）而非通过；
- 断言 `growth == [1, 2, 3, 4, 4]`：前四个依次新建，第五个停在 4——正是「等待而非增长」的那一档。

另外，[docs/benchmark.md:7-11](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/benchmark.md#L7-L11) 明确：0.2.1 加入的暂存池只有正确性测试、**没有性能数字**（方法学要求整台空闲节点，当时不可得）。读任何「池带来 X% 提升」的说法都要记得这一点。

#### 4.3.4 代码实践

**实践目标**：回答规格中的核心问题——同一 (shape, dtype) 的第 5 个并发异步调用走哪条分支、阻塞发生在哪条流；并设计一个用 `staging_slots_debug` 的测试。

**操作步骤**：

1. **走查第 5 个调用**。前提与 correctness.py 相同：caller 流被占住，前 4 个交换都在途。逐条对照 [src/group.cc:347-393](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L347-L393)：扫描对 4 个槽全部得到 `cudaErrorNotReady`（第一档落空）；`pool.size() < kMaxStagingPerShape` 为假（4 不小于 4，第二档落空）；进入 `else if`（第三档）。
2. **设计你自己的池增长测试**。可以最小化 correctness.py 的骨架（示例代码，需 ≥1 GPU；`torchrun --nproc_per_node=1` 时集体退化为本地交换，但 staging 路径原样执行——待本地验证）：

   ```python
   # 示例代码：验证暂存池按 [1, 2, 3, 4, 4] 增长（需先按 docs/quickstart.md 初始化进程组与 group）
   shape = (1, 8, 4 * group.world_size, 64)          # 专属形状，计数从零开始
   xs = [torch.randn(*shape, dtype=torch.bfloat16, device=group.device) for _ in range(5)]
   torch.cuda.synchronize()
   torch.cuda._sleep(100_000_000)                    # 占住 caller 流，交换无法退休
   handles, growth = [], []
   for x in xs:
       handles.append(group.all_to_all_4d_async(x, mode=0))
       growth.append(group._handle.staging_slots_debug(x))
   for h in handles:
       h.wait()
   assert growth == [1, 2, 3, 4, 4], growth
   ```

3. **加一个负对照**：把 `_sleep` 那行注释掉再跑一次，记录 `growth` 变成什么样（多半出现重复的 1——交换退休太快，扫描一直命中第一档）。这正好复现源码注释里「盲跑」的警告。
4. **解释**为什么第五次调用 `staging_slots_debug` 返回 4 而不是 5（它读 `pool.size()`，即 [src/group.cc:418-422](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L418-L422) 的 `it->second.size()`，池满后不再 push）。

**需要观察的现象**：有睡眠时 `[1, 2, 3, 4, 4]`；无睡眠时序列含重复且总槽数可能停在 1。

**预期结果**：第 5 个调用走第三档——`slot = pool.front()`，`cudaStreamWaitEvent(caller, slot->release, 0)`，**等待插在 caller 流上**（GPU 侧），随后的 `copy_` 被推迟到 comm 流读完 front 槽；主机不阻塞。多卡下的完整验证待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么池满的等待要插在 caller 流而不是 comm 流？

**答案**：插在 comm 流会拖住**所有**在途交换（包括别的形状的），并倒置依赖方向——comm 流本该只等「数据就绪」。插在 caller 流，代价只是这一次调用的暂存拷贝晚点发生，影响局限在发起者自己身上；这也和同步语义一致：caller 流本来就是被 `copy_` 占用的那条流。

**练习 2**：把 `kMaxStagingPerShape` 从 4 改成 1，功能上还正确吗？性能上会发生什么？

**答案**：正确——头文件注释明说「Sharing a slot is still correct, just not concurrent」。性能上，同形状的第二个并发调用的 `copy_` 会立刻落到等待档，caller 流被上一个交换拖住，异步形式「提前发行以重叠计算」的目的落空；q/k/v 拆分场景退化成串行。

**练习 3**：若删掉 `const c10::InferenceMode no_inference(false);`，怎样一个用户脚本会触发故障？

**答案**：用户在 `with torch.inference_mode():` 里发出某形状的第一次异步调用——暂存缓冲被建成推理张量。此后任何一次该形状的异步调用（不在推理模式中）执行 `slot->tensor.copy_(x)` 时 raise：「Inplace update to inference tensor outside InferenceMode is not allowed」。这个 (shape, dtype) 的池条目永久不可用。

### 4.4 noexcept 释放：release_staging 与 StagingRelease scope guard

#### 4.4.1 概念说明

释放只有一行语义：**在 comm 流上记录 release 事件**，声明「comm 对这个槽的读取到此为止，caller 流可以覆写了」。难点不在动作，在**时机与异常安全**：

- `run_staged` 里，`stage()` 之后的一切（prepare 校验、屏障、CE 传输）都可能抛异常。若释放只在「正常返回」路径上做，一次抛异常的调用就留下一个**从未记录** release 事件的槽。
- 从未记录的事件，`cudaEventQuery` 恒返回 `cudaSuccess`——下一次同形状调用会**毫不犹豫地复用这个槽**，在 caller 流上 `copy_` 覆写它，而 comm 流可能还在读。注释把这叫「silently, because waiting on an unrecorded event succeeds and does nothing」（[src/bindings.cc:176-179](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L176-L179)）：静默的数据竞争，不报错。
- 修法是 scope guard：`StagingRelease` 的析构函数调用 `release_staging`。C++ 栈对象在异常展开时同样析构，所以**无论 run() 正常返回还是抛出**，释放事件都会被记录。记录在 comm 流**当前尾部**——若异常发生在传输已发行之后，事件恰好排在读完之后；若异常发生得更早（比如 prepare 就拒了），事件排在 comm 已有工作之后，只是让槽「晚点被认为空闲」，保守而正确。

`release_staging` 被标 `noexcept`、且那一行 `cudaEventRecord` 刻意**不加检查宏**，两条理由都在头文件注释里（[include/fast_ulysses/group.hpp:130-135](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/group.hpp#L130-L135)）：它运行在异常传播途中，析构路径上再抛（`noexcept` 下即 `std::terminate`）会把原异常吞掉换成一个更糟的崩溃；且「检查失败所能报告的状态，与不检查留下的状态完全相同」——record 失败时槽同样处于「未记录」，与什么都不做无异，检查没有收益。

顺带一提收尾路径：`destroy()`（[src/group.cc:466-473](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L466-L473)）销毁每个槽的 release 事件（同样不加检查——teardown 不抛），然后清空整张 `staging_`；Python 侧 `destroy()` 在此之前已 `comm_stream.synchronize()`（u7-l1），所以销毁事件时 comm 流已无在途读取。`run_staged` 把 `check_alive` 放在 `stage()` 之前（[src/bindings.cc:374-380](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L374-L380)），堵的正是「destroy 之后再建池条目、事件随 group 泄漏」这个缝。

#### 4.4.2 核心流程

```text
run_staged:
    检查（check_alive / require_cuda / require_group_device）
    comm = 当前流（Python 已设为 comm 流）；caller = unpack3(两整数)
    slot = group->stage(x, caller, comm)
    StagingRelease release{group, slot, comm}      # 构造 guard
    run(...)                                       # 屏障 → 传输 → 屏障 → copy_out
        │
        ├─ 正常返回 ──► release 析构 ──► cudaEventRecord(slot->release, comm)
        └─ 中途抛出 ──► 栈展开 ──────► release 析构 ──► 同样记录 ◄── 关键
    （异常继续向上传播；槽状态一致，不会变成「永不空闲」的假空闲）
```

#### 4.4.3 源码精读

scope guard 全文：[src/bindings.cc:176-191](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L176-L191)。注意注释里的另一句：「The slot this call used, not "the last one staged"」——guard 携带的是**本次调用实际用到的**槽指针；同形状多个调用在途时，各自的 guard 释放各自的槽，绝不能引用「最近一次 stage 的那个」。

释放函数本体：[src/group.cc:406-416](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L406-L416)：

```cpp
void UlyssesGroup::release_staging(Staging* slot, cudaStream_t comm) noexcept
{
    if (slot != nullptr) {
        cudaEventRecord(slot->release, comm);   // 刻意不加 ULYSSES_CUDA_CHECK
    }
}
```

guard 在 `run_staged` 中的落点：[src/bindings.cc:393-395](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L393-L395)——`stage()` 返回后**立即**构造，先于 `run()` 的任何屏障。这保证保护的覆盖面是「传输全段」，而不是「传输的尾部」。

测试探针的注册与存在理由：[src/bindings.cc:692-698](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L692-L698)——`staging_slots_debug` 的注释说得直白：多个同形状异步调用并发「正是暂存池存在的理由，而结果本身看不见它——共享一个槽也是**正确的**，只是在 caller 流上串行化。槽的数量把两者分开」。这是继 `epoch_debug` 之后第二个「结果证明不了、只能靠观测内部状态」的测试方法（u6-l1 的主题在此复现）。

#### 4.4.4 代码实践

**实践目标**：把「异常路径也必须记录释放事件」的论证写成自己的话，并找出代码里每一处为此服务的细节。

**操作步骤**：

1. 读 [src/bindings.cc:176-191](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L176-L191) 的注释，然后**不借助注释**复述：若 guard 不存在，一次在 CE 拷贝已发行后抛出的调用，会让下一次同形状调用发生什么？分三步写：① 该槽的 release 事件状态；② 下次调用扫描该槽的返回值；③ `copy_` 与 comm 读取的关系。
2. 在 [src/group.cc:406-416](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L406-L416) 与 [include/fast_ulysses/common.hpp:41-44](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/common.hpp#L41-L44) 找出两处「刻意不检查的 CUDA 调用」，比较它们的理由（一个在异常传播中，一个在析构函数中——共同点是不能抛）。
3. 思考题：`release_staging` 为什么需要 `slot` 作参数，而不是记住「最近 stage 的槽」？（答案在注释里，也见 4.4.3。）

**需要观察的现象 / 预期结果**：纯源码推理即可完成；① 从未记录；② `cudaSuccess`（未记录事件查询即成功）；③ caller 流覆写与 comm 流读取竞争，静默出错。这与注释的描述逐字吻合。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `release_staging` 的 `cudaEventRecord` 不加 `ULYSSES_CUDA_CHECK`？

**答案**：两个理由（头文件注释）：它可能在异常传播中运行，此时再抛会把原异常换成 terminate；而且检查没有收益——record 失败留下的状态（事件未记录）与不加检查时完全一样，报告它并不能改善什么。

**练习 2**：`StagingRelease` 的构造位置为什么必须在 `run()` **之前**、`stage()` **之后**，而不是包住 `stage()` 本身？

**答案**：`stage()` 自己新建槽时已经用「局部建好、一起提交」保证了失败不留半成品（4.3）；guard 要保护的是**传输段**（屏障与 CE 拷贝——那里才会出现「拷贝已发行、随后抛出」的窗口）。若 guard 在 `stage()` 之前构造，slot 还不存在；若在 `run()` 之后，传输抛出的瞬间已经来不及了。

## 5. 综合实践

**任务：给「第 5 个并发调用」写一份完整的判决书，并落成一个可运行的测试。**

分三步，综合本讲全部四个模块：

1. **判决（4.1 + 4.3）**：写一段 200 字以内的分析，回答：同一 (shape, dtype) 的第 5 个并发异步调用，在扫描、新建、等待三档中走哪档？等待插在哪条流、由谁触发、阻塞是否涉及主机？它随后的暂存拷贝何时才真正执行？（参考答案：走等待档；`cudaStreamWaitEvent(caller, pool.front()->release, 0)` 插在 caller 流；触发条件是 comm 流读完 front 槽并记录了 release 事件；GPU 侧等待、主机不阻塞；`copy_` 排在该等待之后。）
2. **测试（4.3.4）**：以 [test/distributed/correctness.py:185-219](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py#L185-L219) 为蓝本，写出你自己的池增长测试（专属形状 + `torch.cuda._sleep` + 逐次 `staging_slots_debug`），断言 `[1, 2, 3, 4, 4]`，并附一个去掉睡眠的负对照。多卡跑法参照 docs/develop.md 的 torchrun 包装；单卡以 `--nproc_per_node=1` 运行能否复现同一序列，待本地验证。
3. **论证（4.4）**：假设你 review 一份把 `StagingRelease` 换成「run() 正常返回后手动调 release_staging」的改动。写出驳回意见，必须包含：未记录事件被 `cudaEventQuery` 当作空闲的语义、覆写与 comm 读取的竞争、以及为什么静默错误比崩溃更难排查。（依据：[src/bindings.cc:176-179](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L176-L179)。）

## 6. 本讲小结

- 异步输入必须先在 **caller 流**上拷进 group 持有的暂存缓冲：直接跨流读要么内存不安全，要么用 `record_stream` 把每个已释放输入钉到 comm 流追上为止；暂存把钉住的对象换成一小池 group 自己的缓冲，还顺带吸收非连续输入。
- 两个事件夹住一次交接：**ready**（每次调用新建）让 comm 等副本写完；**release**（随槽常驻）在 comm 流上声明读完。复用槽 = 查询 release 已触发，零成本。
- 池按 **(shape, dtype)** 键组织，选择三档：扫描复用 → 未达 `kMaxStagingPerShape = 4` 新建 → 池满在 **caller 流**上等 `pool.front()` 的 release（GPU 侧、不阻塞主机）；`staging_` 另有 16 形状上限，超界同步并全清。共享一个槽依然正确，池买的是**并发**。
- 新建槽的三个防御：局部建好一起提交（不留半成品）、`at::empty` 而非 `empty_like`（暂存必须密集）、`InferenceMode(false)`（防缓冲永久变成推理张量）。
- 释放由 **`StagingRelease` scope guard** 驱动：异常展开时析构同样执行，`release_staging` 是 `noexcept` 且不加检查——否则一次传输异常会留下「从未记录 release」的槽，被下次调用当空闲复用，覆写与读取静默竞争。
- 池行为**结果不可见**，`staging_slots_debug` 是唯一探针；官方正确性测试断言增长序列 `[1, 2, 3, 4, 4]`，且性能数字尚无公开测量。

## 7. 下一步学习建议

- 下一讲 **u7-l3 工作注册表：StreamEventWork 与完成事件**：离开输入侧，看输出如何把 comm 流的完成事件绑到张量的 storage（`c10d::register_work`）、`wait()` 如何只在当前流插入依赖，以及无注册表时的 `CompletedHandle` 退化——与 `stage()` 的 ready/release 事件对照，你会看到同一套事件原语的另一种用法。
- 想巩固「结果证明不了、需要探针」的方法论，回头读 u6-l1 的 `epoch_debug` 一节，与本讲 `staging_slots_debug` 对比。
- 想看异步全链路的对抗性测试，读 [test/distributed/window_race.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/window_race.py) 与 u9-l3 的「负控制」设计——本讲的「去掉睡眠即盲跑」是同一思想的入门版。
