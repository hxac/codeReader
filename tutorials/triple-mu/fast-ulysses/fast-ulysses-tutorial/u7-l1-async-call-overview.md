# 异步调用全景：comm 流与 AsyncCollectiveTensor

## 1. 本讲目标

学完本讲，你应该能够：

- 说清 `all_to_all_4d_async` 与同步版 `all_to_all_4d` 在执行位置上的唯一区别：同一条「prepare → 屏障 → CE 传输 → 屏障 → copy_out」流水线（u6-l2）被搬到一条**组级高优先级 comm 流**上，使传输可与 caller 流上的计算重叠。
- 解释 caller 流为什么必须在 Python 侧捕获、以两个整数（`caller_stream_id` / `caller_device_index`）跨语言传入 C++，而不能在 C++ 里取「当前流」。
- 说明输出张量为什么要做 `record_stream` 双流记账，以及 `register_stream_completion` 如何把 comm 流上的完成事件绑进 torch 的 work registry。
- 描述 `AsyncCollectiveTensor` 的等待语义：`.wait()` 与首个 aten op 都只在 caller 流上插入 GPU 侧等待、主机不阻塞；view 操作不等待；带梯度的输入被显式拒绝；丢弃结果的注册表残留后果；无 registry 构建退化成 `CompletedHandle`。

## 2. 前置知识

**CUDA 流（stream）与事件（event）**。CUDA 把工作按流排队，同一条流内的操作严格按序执行，不同流之间没有顺序保证。跨流的先后关系要用事件表达：`cudaEventRecord(ev, s)` 在流 `s` 上打一个标记，`cudaStreamWaitEvent(s2, ev)` 让流 `s2` 从此处开始等待该标记兑现。等待是 GPU 侧的——主机线程发出这两条指令后立即返回。

**流优先级**。每个流可带优先级，影响同设备上多条流争用执行资源时的调度倾向。torch 的约定是数值越小优先级越高，`torch.cuda.Stream.priority_range()` 返回 `(least, greatest)` 两个端点，其中 `greatest` 是优先级最高（数值最小）的那端。

**缓存分配器与 `record_stream`**。torch 的 CUDA 缓存分配器按「分配时所在的那条流」记账：张量被释放后，内存块只要在分配流上已无待完成工作就可被回收复用。若另一条流还在读写这块内存，分配器并不知道，回收就会踩到在飞的读写。`tensor.record_stream(s)` 就是把「流 `s` 也会碰这块内存」登记给分配器。

**重叠（overlap）为什么可能**。承接 u5-l2：跨 NVLink 的 peer 拷贝由拷贝引擎（CE）执行，不占 SM，因此可与 caller 流上的 GEMM 完全并行；本库的异步调用就是把整个交换放进另一条流，让 caller 流继续跑计算。

**torch 的函数式集合通信包装**。`torch.distributed._functional_collectives.AsyncCollectiveTensor` 是 torch 自带的一个张量包装子类：包装一个「完成事件已注册进 work registry」的张量，使首次真正消费它时自动插入等待。本讲会讲清本库如何生产、又如何约束这个包装。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `python/fast_ulysses/group.py` | Python 门面：comm 流的创建、`all_to_all_4d_async` 的全部 Python 侧逻辑（参数拒绝、caller 流捕获、staged 调用、完成注册、record_stream、返回包装） |
| `src/bindings.cc` | torch 算子层：`all_to_all_4d_staged` / `all_to_all_4d_staged_out` 两个内部算子及其共享实现 `run_staged`（caller 流重建、暂存入口、复用 u6-l2 流水线） |
| `src/work.cc` | `register_stream_completion`：在 comm 流上记录完成事件并注册成 `StreamEventWork`；无 registry 构建的退化路径 |
| `include/fast_ulysses/work.hpp` | 上述函数的契约注释：registry 按 **storage** 记账，条目属于产生它的那次调用 |
| `src/group.cc` | `stage()`：输入在 **caller 流**上拷进按 `(shape, dtype)` 池化的暂存缓冲（池的细节是下一讲 u7-l2 的主题，本讲只取「输入不跨流」这一面） |
| `docs/api.md` | 异步调用的文档化契约：等待语义、暂存池大小、lend、退化行为 |
| `benchmark/bench_a2a.py` | `run_overlap`：serial vs concurrent 的隐藏率测量，本讲综合实践的参照 |
| `test/distributed/correctness.py` | 异步行为的权威断言：非连续输入、5 个同形状并发、非默认流上发起 |

## 4. 核心概念与源码讲解

### 4.1 comm 流：高优先级传输流

#### 4.1.1 概念说明

同步调用 `all_to_all_4d` 全程在 **caller 的当前流**上执行：屏障、CE 传输、copy_out 与调用者前后的计算严格串行，交换时间一分不少地出现在关键路径上。异步调用的思路很直接：把同一条流水线整段搬到另一条流（comm 流）上，caller 流只负责「投喂输入」和「取走结果」，中间的计算与交换并行。

comm 流是**组级**资源，构造 `UlyssesGroup` 时创建一次、按最高优先级。两个设计决定值得注意：

1. **sync 调用刻意不走 comm 流**。若同步调用也路由进 comm 流，进出各需一次跨流事件等待（「two event hops」），换来的是零收益的纯开销——同步语义本来就要求 caller 流等它完成。
2. **comm 流与 caller 流互不排序**。两条无序的流可能同时对同一个窗口读写，所以 C++ 侧为 sync 与 async 准备了不同的窗口（`kSyncWindow` / `kAsyncWindow`，承接 u4-l2 的 `(role, dtype)` 缓存键）。这是「混用 sync 与 async 是安全的」这一文档承诺的来源。

#### 4.1.2 核心流程

```text
UlyssesGroup.__init__
  ├─ torch.cuda.Stream.priority_range() → 取 greatest（最高优先级）
  ├─ self._comm_stream = torch.cuda.Stream(device, priority=greatest)
  └─ 之后每次 async 调用:
       with torch.cuda.stream(self._comm_stream):   # 把 comm 流设为当前流
           torch.ops.fast_ulysses.all_to_all_4d_staged(...)   # C++ 里 getCurrentCUDAStream() 即 comm 流
```

时间线上，异步调用的目标形态是：

```text
caller 流:  [暂存输入 x]  [------------- 计算链 (GEMM) -------------]  [wait / 消费 y]
comm 流:                  [屏障][ CE 传输 ][屏障][ copy_out ]                 ← 与计算并行
```

#### 4.1.3 源码精读

[python/fast_ulysses/group.py:L91-L96](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L91-L96) 构造函数末尾创建 comm 流：注释说明它「只服务异步调用」——同步调用留在 caller 流上，因为路由进来要付两次事件跳的开销；并点明两条流互不排序，这正是 C++ 侧给它们分开配窗口的原因。

[python/fast_ulysses/group.py:L203-L214](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L203-L214) 异步调用的核心三行：进入 `torch.cuda.stream(self._comm_stream)` 上下文后调用 staged 算子。此刻起直到退出上下文，「当前流」就是 comm 流——这是 4.2 要展开的关键。

[src/bindings.cc:L383-L384](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L383-L384) C++ 侧用 `at::cuda::getCurrentCUDAStream()` 拿到的就是 Python 刚设为当前的 comm 流——屏障、传输、copy_out 全部排在它上面。

[python/fast_ulysses/group.py:L267-L277](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L267-L277) `destroy()` 先 `self._comm_stream.synchronize()` 再 `dist.barrier()`：NCCL 的 barrier 只同步 caller 的当前流，一个从未等待的异步调用可能仍在写一个即将被释放的窗口。`__del__`（[L279-L286](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L279-L286)）同样先排干 comm 流——这是 comm 流的生命周期义务。

#### 4.1.4 代码实践

1. **实践目标**：确认 comm 流的优先级确实取到最高档，且同步调用路径完全不经过它。
2. **操作步骤**：
   - 运行下述片段（示例代码，需 GPU）：

     ```python
     import torch, torch.distributed as dist
     from fast_ulysses import UlyssesGroup
     dist.init_process_group("nccl")
     group = UlyssesGroup()
     least, greatest = torch.cuda.Stream.priority_range()
     print("range:", least, greatest)
     print("comm:", group._comm_stream.priority)
     print("default:", torch.cuda.default_stream().priority)
     ```

   - 无 GPU 时改做源码检索：在 `python/fast_ulysses/group.py` 里搜索 `_comm_stream` 的全部出现位置，逐个归类它属于哪条路径。
3. **需要观察的现象**：`comm` 的优先级数值应等于 `greatest`（多数实现上不大于 `default` 的数值；具体取值**待本地验证**）。`_comm_stream` 的出现位置应只落在三处：构造、`all_to_all_4d_async`、`destroy`/`__del__` 的排干。
4. **预期结果**：同步方法 `all_to_all_4d` 的函数体中没有任何一处引用 `_comm_stream`——「sync 留在 caller 流」不是口号，而是代码事实。

#### 4.1.5 小练习与答案

1. **问**：把同步调用也路由进 comm 流会怎样？
   **答**：进入前 caller 流要等 comm 流、退出后 caller 流再等一次——两次跨流事件等待的固定开销，而同步语义的收益本来就是零。源码注释的原话是「costs two event hops」。
2. **问**：如果 sync 与 async 共用一个窗口，会发生什么？
   **答**：两条互不排序的流对一个单缓冲窗口并发读写——上一讲的读写竞态（u4-l2）。所以窗口缓存键里有 `WindowRole`（`kSyncWindow`/`kAsyncWindow`）。
3. **问**：`destroy()` 里为什么必须先 `synchronize` comm 流再 `dist.barrier()`？
   **答**：`dist.barrier` 只同步 caller 的当前流，看不见 comm 流上未完成的异步写；不排干就可能释放一个正被写的窗口。

### 4.2 staged 算子调用：caller 流的捕获与跨语言传递

#### 4.2.1 概念说明

这是本讲最核心的一个设计问题：**C++ 算子怎么知道「caller 原来在哪条流上」？**

答案是不可能自己知道。`run_staged` 执行时，Python 已经用 `with torch.cuda.stream(self._comm_stream)` 把 comm 流设成了当前流，此时 C++ 里任何 `getCurrentCUDAStream()` 拿到的都是 comm 流。而流水线恰恰需要 caller 的流做两件 comm 流做不了的事：

1. **输入暂存必须发生在 caller 流上**（详见下文）：这样输入张量 `x` 只被它自己所属的流读取，comm 流只见暂存副本；
2. **完成等待必须落在 caller 流上**：`wait()` 或首个 aten op 消费结果时，等待要插进 caller 当时的流。

所以 caller 流必须在 Python 侧、**进入 comm 流上下文之前**捕获，再作为参数传入。它以两个整数过 schema——torch 的 schema 装不下流对象——C++ 侧用 `c10::Stream::unpack3(stream_id, device_index, CUDA)` 重建。重建而非包一个裸句柄，为的是让缓存分配器认识到「和 caller 当时用的是同一个流对象」（`record_stream` 的记账才对得上号）。

`device_index` 为什么也要传：`torch.cuda.current_stream(self.device)` 显式按**组的设备**取流；无参形式会取「当前设备」的流——若当时别的设备是当前设备，取到的流与本组无关，也对本组的张量做不了 `record_stream`。

#### 4.2.2 核心流程

```text
Python:  caller = torch.cuda.current_stream(self.device)     # 先捕获（含设备号）
         with torch.cuda.stream(comm_stream):
             staged(group, x, mode, splits…, caller.stream_id, caller.device_index, lend)
                     │
C++:     run_staged:
           ├─ check_alive / require_cuda / require_group_device   # 全部先于 stage()，理由见下
           ├─ CUDAGuard(input.device)
           ├─ comm   = getCurrentCUDAStream()          # = comm 流（Python 设的）
           ├─ caller = Stream::unpack3(stream_id, device_index, CUDA)   # 重建 caller 流
           ├─ slot   = group->stage(x, caller, comm)   # 暂存拷贝在 caller 流上执行
           │            └─ 完成后: record(ready, caller); comm wait ready
           └─ run(…, slot->tensor, kAsyncWindow, comm, lend)   # u6-l2 流水线，排在 comm 流
```

各动作落在哪条流：

| 动作 | 流 |
| --- | --- |
| 输入暂存拷贝 `slot->tensor.copy_(x)`（吸收跨步输入） | caller 流 |
| `ready` 事件记录 → comm 流等待它 | caller → comm |
| 开路屏障、CE 传输、收尾屏障 | comm 流 |
| copy_out（默认路径） | comm 流 |
| `wait()` / 首个 aten op 的等待 | caller 流（消费时刻） |

#### 4.2.3 源码精读

[python/fast_ulysses/group.py:L198-L202](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L198-L202) 捕获 caller 流：显式传 `self.device`，注释解释了无参形式在「别的设备是当前设备」时取到一条与本组无关、也无法对本组张量做 `record_stream` 的流。

[python/fast_ulysses/group.py:L203-L226](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L203-L226) 在 comm 流上下文里调用两个 staged 算子：无 `out` 走 `all_to_all_4d_staged`（把 `caller.stream_id`、`caller.device_index` 连同 `lend` 一起传入）；有 `out` 走 `all_to_all_4d_staged_out`，结果就是调用者的 `out`。

[src/bindings.cc:L739-L751](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L739-L751) 两个 staged 算子的 schema：`int caller_stream_id, int caller_device_index` 是普通整数参数——schema 装不下流对象，只能这样跨语言传递。

[src/bindings.cc:L364-L396](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L364-L396) `run_staged` 全文，三个关键点：

- [L374-L383](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L374-L383) 三项检查先于 `stage()`：`check_alive` 必须最先（`destroy()` 第二次调用会提前返回，destroy 之后再建的暂存条目将永远无人释放）；设备检查防止暂存池（按 `(shape, dtype)` 缓存）留下一块错误设备的缓冲。
- [L383-L388](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L383-L388) `comm = getCurrentCUDAStream()` 取到的是 Python 设为当前的 comm 流；caller 流用 `Stream::unpack3` 从两个整数重建，注释点明「重建而非包装裸句柄，缓存分配器看到的才是 caller 当时用的同一个流对象」。
- [L389-L395](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L389-L395) 暂存发生在 caller 流上，「caller 的张量永远不被 comm 流读取」——替代方案 `record_stream` 会把每个被释放的输入都钉住直到 comm 流追上；暂存拷贝顺带吸收跨步输入，所以这里刻意**不**调用 `contiguous()`（那会把拷贝发到 comm 流上，恰是要避免的事）。随后复用 u6-l2 的 `run`，窗口角色 `kAsyncWindow`、流为 `comm`。

[src/group.cc:L340-L342](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L340-L342) `stage()` 里显式 `CUDAStreamGuard(caller)`——「下面一切发生在 caller 的流上，caller 的张量只在那里被读，comm 流只看到暂存副本」。

[src/group.cc:L395-L401](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/group.cc#L395-L401) 暂存拷贝之后记录 `ready` 事件并让 comm 流等待它：comm 流不许在拷贝落地前读暂存缓冲。（槽的选择、池的上限与满池等待是 u7-l2 的主题。）

[test/distributed/correctness.py:L360-L374](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py#L360-L374) 「非默认流上发起异步调用」的对抗性测试：在一条侧流上先跑 24 轮 GEMM「压舱」再造出输入，然后发起异步调用。若 C++ 读的是默认流而非被告知的 caller 流，暂存读就会乱序读到错误字节——这个测试就是 caller 流必须显式传参的实证。

顺带一提（u6-l4 已详述）：[src/bindings.cc:L425-L451](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L425-L451) staged 一对算子拒绝被 trace，理由之一正是这两个参数「指名一条活的 CUDA 流」，被固化进图后重放到哪里都是错的。

#### 4.2.4 代码实践

1. **实践目标**：亲手复现「caller 流传错了会怎样」的可观察版本，并写出两个整数参数的必要性论证。
2. **操作步骤**（示例代码，需单卡即可，`ws=1` 合法）：

   ```python
   # side_stream_async.py —— 精简自 test/distributed/correctness.py 的 ballast 测试
   import os, torch, torch.distributed as dist
   from fast_ulysses import UlyssesGroup
   dist.init_process_group("nccl")
   torch.cuda.set_device(int(os.environ.get("LOCAL_RANK", 0)))
   group = UlyssesGroup()
   dev = group.device

   x = torch.randn(1, 64, 40, 384, dtype=torch.bfloat16, device=dev)
   want = group.all_to_all_4d(x, mode=0).clone()          # 参照

   side = torch.cuda.Stream(device=dev)
   side.wait_stream(torch.cuda.current_stream())
   with torch.cuda.stream(side):                          # 输入的生产者换到侧流
       ballast = torch.randn(2048, 2048, dtype=torch.bfloat16, device=dev)
       for _ in range(24):
           ballast = ballast @ ballast.T * 0.001          # 压舱：让输入“晚到”
       produced = torch.empty_like(x).copy_(x)
       got = group.all_to_all_4d_async(produced, mode=0).wait()
   torch.cuda.current_stream().wait_stream(side)
   print("equal:", torch.equal(got, want))
   group.destroy(); dist.destroy_process_group()
   ```

   然后回答书面问题：`caller.stream_id` / `caller_device_index` 为什么必须由 Python 传入？（参考答案见 4.2.5。）
3. **需要观察的现象**：输出 `equal: True`。若无 caller 流参数、C++ 误读默认流，`produced` 尚未在侧流上算完，暂存拷贝会读到未初始化字节（**待本地验证**；正确性由源码与 correctness.py 的断言保证）。
4. **预期结果**：体会到「谁在哪条流上」是异步正确性的全部——库里把它显式化的方式就是把两个整数写进 schema。

#### 4.2.5 小练习与答案

1. **问**：`run_staged` 里调用 `getCurrentCUDAStream()` 得到的是哪条流？为什么？
   **答**：comm 流。因为 Python 侧已经用 `with torch.cuda.stream(self._comm_stream)` 把它设为当前——这正是 caller 流必须作为参数显式传入的根源。
2. **问**：只传 `caller_stream_id`、不传 `caller_device_index` 行不行？
   **答**：不行。`c10::Stream::unpack3` 需要三要素（流 id、设备号、设备类型）才能重建出与 caller 所用**同一个**流对象；缺设备号无法定位流所属的设备，缓存分配器的 `record_stream` 记账也会对不上。
3. **问**：`stage()` 里为什么不顺手 `input.contiguous()`？
   **答**：`contiguous()` 的拷贝会发到当前流（此刻是 comm 流），等于让 comm 流直接读 caller 的张量——正是暂存要避免的跨流读取；暂存拷贝本身在 caller 流上执行，天然吸收跨步输入。

### 4.3 record_stream 双流记账与完成注册

#### 4.3.1 概念说明

异步调用的输出 `y` 天生是「双流张量」：一条流生产它，另一条流消费它。缓存分配器只认识分配时的那条流，不知道另一条流也在碰它——不登记，内存块可能在仍在被读写时就被回收复用，而且是静默的。所以 Python 侧对 `y` 调了**两次** `record_stream`：

- 默认路径：`y` 由 `at::empty` 在 **comm 流为当前**时分配（分配流 = comm），消费发生在 **caller 流**——`record_stream(caller)` 必需；
- `out=` 路径：`y` 就是调用者早已在 caller 流上持有的缓冲，写它的是 **comm 流**（含 peers 直写与 copy_out）——`record_stream(comm)` 必需。

两行一起写，把两种路径都盖住。

「消费时的等待」则由**完成注册**解决：C++ 侧在 comm 流上记录一个完成事件，绑到 `y` 的 storage 上（torch 的 work registry 按 **storage** 记账，所以条目属于产生它的那次调用，而不是恰好占用的那块缓冲）。之后 `.wait()` 或首个 aten op 只需在当时的 caller 流上 `cudaStreamWaitEvent` 一下——主机全程不参与握手。若本构建的 libtorch 没有 `c10d::register_work`，注册退化为「立刻让 caller 流等待、销毁事件」，返回 `false`，Python 侧据此外露 `CompletedHandle` 类型。

#### 4.3.2 核心流程

```text
Python:  y = staged(...)                        # 传输已在 comm 流上排队
         registered = _C.register_stream_completion(y, comm_stream.cuda_stream)
         y.record_stream(comm_stream); y.record_stream(caller)
         return AsyncCollectiveTensor(y) if registered else CompletedHandle(y)

C++:     register_stream_completion:
           event = create(DisableTiming); record(event, comm_stream)
           ├─ 有 registry:  c10d::register_work(y, StreamEventWork(event)) → true
           └─ 无 registry:  caller 当前流 wait(event)；destroy(event)     → false
```

#### 4.3.3 源码精读

[python/fast_ulysses/group.py:L227-L232](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L227-L232) 三步收尾：注册完成事件（传 comm 流的裸句柄）；注释「两条流碰这个输出：一条分配它、另一条写它，缓存分配器必须先知道跨流使用才允许回收」；两次 `record_stream`；按注册成败返回包装类型。

[src/bindings.cc:L776-L783](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L776-L783) `register_stream_completion` 只能是 pybind 函数而非 torch 算子：它接收裸流句柄、改动进程级全局 registry，两样都不属于 schema。

[src/work.cc:L62-L80](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/work.cc#L62-L80) 实现本体：`cudaEventCreateWithFlags(…, cudaEventDisableTiming)`（只做同步用，计时位关掉更便宜）→ 在 comm 流上记录 → 有 registry 时 `c10d::register_work(tensor, StreamEventWork)`，事件所有权交给 registry（「registry 拥有 Work，Work 拥有事件」）；无 registry 时让 caller 当前流等待后立刻销毁事件（等待已捕获事件的记录状态，销毁是安全的），返回 `false`。

[src/work.cc:L22-L56](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/work.cc#L22-L56) `StreamEventWork`：`wait()` 只执行 `cudaStreamWaitEvent(当前流, event)` 即返回 `true`——主机不阻塞；`synchronize()` 才是给收尾场景的主机阻塞；`isCompleted()` 用 `cudaEventQuery`。

[include/fast_ulysses/work.hpp:L1-L18](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/work.hpp#L1-L18) 头文件注释点出按 storage 记账的语义：`wait_tensor` 会等待注册在某张量 storage 上的**每一个** `c10d::Work`——「条目属于产生结果的那次调用，而不是它恰好占用的缓冲」。

[python/fast_ulysses/group.py:L20-L32](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L20-L32) `CompletedHandle`：无 registry 构建的返回类型，`wait()` 只做解包——C++ 已在调用现场让 caller 流等待过了，所以正确但**无重叠**；做成独立类型就是为了让这个事实可见。

[docs/api.md:L118-L120](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/api.md#L118-L120) 丢弃结果的后果清单：被丢的结果在 torch 的 work registry 里留下条目和 CUDA 事件，torch 退出时打印幸存条目数；`out=` 是盲区——读自己的 `out` 永远不经过 registry，所以**必须**读返回的包装对象。

#### 4.3.4 代码实践

1. **实践目标**：判断当前构建有无 work registry，并观察注册表残留。
2. **操作步骤**（示例代码，需单卡）：

   ```python
   h = group.all_to_all_4d_async(x, mode=0)
   print(type(h).__name__)      # AsyncCollectiveTensor = 有 registry；CompletedHandle = 无
   y = h.wait()
   ```

   再写一个「故意丢弃」的循环（示例代码）：连发 8 次 `group.all_to_all_4d_async(t, mode=0)` 不保存返回值，进程退出时留意 torch 打印的 registry 幸存条目信息。
3. **需要观察的现象**：`type(h)` 直接告诉你构建走了哪条分支；丢弃循环退出时的幸存条目打印（**待本地验证**，具体文案随 torch 版本）。
4. **预期结果**：官方 wheel（较新 torch）应返回 `AsyncCollectiveTensor`；`out=` 版本即使不读返回值也不会泄漏条目到「读结果」这条路径——但按文档仍应读返回的包装。

#### 4.3.5 小练习与答案

1. **问**：两次 `record_stream` 各自防的是什么？
   **答**：默认路径 `y` 在 comm 流上分配、caller 流上消费，防分配器在 caller 仍读时回收；`out=` 路径 `y` 是 caller 早先分配的缓冲、由 comm 流写，防的是反方向。两次调用把两种路径一次盖住。
2. **问**：`FAST_ULYSSES_HAS_WORK_REGISTRY=0` 的构建上异步调用还剩多少「异步」？
   **答**：几乎没有——caller 流在调用现场就被排到完成事件之后（正确、无重叠），返回类型也换成 `CompletedHandle` 让退化可见。
3. **问**：为什么 registry 按 storage 而不是按缓冲地址记账是正确的粒度？
   **答**：等待属于「产生这次结果的那次调用」；同一块缓冲被后续调用复用时，旧条目不应被新调用触发，新调用的条目也不该丢（头文件注释的原意）。

### 4.4 AsyncCollectiveTensor：等待语义与视图陷阱

#### 4.4.1 概念说明

`AsyncCollectiveTensor` 是 torch 自带的张量包装子类，本库只做两件事：生产它（`registered` 为真时）、以及对它的两个坑设防。

**等待语义**（torch侧行为，本库文档转述）：

- `.wait()` 返回裸张量；
- **首个 aten op** 消费它时自动等待（包装子类在 dispatch 时对每个张量先做 `wait_tensor`——torch 的实现细节，不在本仓库）；
- 两种等待都只是在 caller 当前流上插入 `cudaStreamWaitEvent`，主机不阻塞；
- **view 操作不等待**：视图不消费数据，包装类把视图再包装一层。陷阱在于：拿着视图以为「已经拿到数据」是错觉，真正读字节时才等待——通常无害，但若你把视图交给非 aten 的消费者（例如 `.data_ptr()` 后自己拷走），就没有任何等待发生。

**两个坑的设防**：

1. **不可微**：torch 以 `_make_wrapper_subclass(..., requires_grad=elem.requires_grad)` 构造包装，包装类成了 autograd 的**叶子**——autograd 在子类之上运行，永远看不到被包装张量的历史。于是 `backward()` 会正常跑完、梯度落在包装上、`x.grad` 是 `None`：静默丢梯度。本库选择对带梯度的输入直接 `raise`。
2. **丢弃残留**：见 4.3——每个结果都必须被 `wait()` 或消费。

#### 4.4.2 核心流程

```text
y (裸张量, comm 流生产, 完成事件已绑 storage)
  └─ AsyncCollectiveTensor(y)
       ├─ h.wait()          → wait_tensor → caller 流 wait 事件 → 返回 y
       ├─ h + 1  (aten op)  → dispatch 前自动 wait → 返回普通结果
       └─ h.view(...)       → 不 wait，重新包装成 AsyncCollectiveTensor(view)
```

#### 4.4.3 源码精读

[python/fast_ulysses/group.py:L161-L179](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L161-L179) 方法 docstring 就是完整契约：`.wait()` 与首个 aten op 都让 caller 流 GPU 侧等待、主机不阻塞；view 再包装不等待；「等待或消费每一个结果」，丢弃即注册表残留；`out=` 是盲区；无 registry 时返回 `CompletedHandle`。

[python/fast_ulysses/group.py:L188-L197](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L188-L197) 梯度输入的拒绝：注释逐句给出「包装类是叶子 → backward 跑完但 `x.grad` 为 None」的论证——「拒绝而非静默出错」是本库的一贯纪律（与 `out=` 的拒绝同型，u6-l2）。

[python/fast_ulysses/group.py:L182-L187](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L182-L187) `out=` 与 `lend=True` 互斥的拒绝：两者都是零拷贝入口，替调用者悄悄选一个会让另一个静默失效。

[python/fast_ulysses/group.py:L232](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L232) 按注册成败分流返回类型。

[docs/api.md:L109-L116](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/api.md#L109-L116) 文档版同一契约，含「叶子」论证与 view 例外。

[test/distributed/correctness.py:L185-L218](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py#L185-L218) 5 个同形状异步调用并发在飞：`staging_slots_debug` 断言池按 `[1, 2, 3, 4, 4]` 增长——前 4 个真正重叠，第 5 个等一个槽退休。这是「等待语义 + 暂存池」联合行为的权威测试。

#### 4.4.4 代码实践

1. **实践目标**：用可观察实验区分「等待的两种触发」与「不等待的视图」。
2. **操作步骤**（示例代码，需单卡）：

   ```python
   import time, torch
   h = group.all_to_all_4d_async(x, mode=0)
   t0 = time.perf_counter(); y = h.wait(); t1 = time.perf_counter()
   print(f"wait() host cost: {(t1 - t0) * 1e6:.1f} us")   # 预期：微秒级，主机不阻塞

   h2 = group.all_to_all_4d_async(x, mode=0)
   v = h2.view(-1)          # 或 h2[0]
   print(type(v).__name__)  # 预期：仍是 AsyncCollectiveTensor —— 视图没有触发等待
   z = v + 1                # 首个 aten op：这里才等待
   ```

3. **需要观察的现象**：`wait()` 的主机耗时是微秒量级（它只入队一个 `cudaStreamWaitEvent`）；`view` 后类型不变（**待本地验证**——这是 torch 侧行为）。
4. **预期结果**：建立直觉——`wait()` 是「把等待排进流」而非「等它完成」；视图只是延后了这个动作。

#### 4.4.5 小练习与答案

1. **问**：`h.wait()` 会阻塞主机吗？
   **答**：不会。`StreamEventWork::wait` 只在当前流上入队 `cudaStreamWaitEvent` 后立即返回；真正阻塞主机的是 `synchronize()`（收尾用）。
2. **问**：为什么视图操作可以不等待？
   **答**：视图不读数据字节，只是元数据变换；torch 的包装子类把它再包装，等待推迟到真正消费。风险在于绕过 aten 直接取 `.data_ptr()` 的消费者不会有任何等待。
3. **问**：带梯度的输入为什么被拒而不是「尽力支持」？
   **答**：包装类是 autograd 叶子，梯度会静默落在包装上而 `x.grad` 为 `None`——错误能跑通但结果无声地错，所以显式 `raise`，让用户改用可微的 `all_to_all_4d()`。

## 5. 综合实践

**任务**：写一个推理风格样例，量化异步调用的「隐藏率」，并完成 caller 流参数的书面论证。参照 [benchmark/bench_a2a.py:L217-L259](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L217-L259) 的 `run_overlap`（其方法论文档见 [L19-L22](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py#L19-L22)）。

```python
# overlap_probe.py（示例代码，参照 bench_a2a.run_overlap 简化）
# 运行: torchrun --nproc_per_node=N overlap_probe.py    （重叠结论需 N>=2；N=1 可跑通流程）
import os, statistics
import torch
import torch.distributed as dist
from fast_ulysses import UlyssesGroup

dist.init_process_group("nccl")
torch.cuda.set_device(int(os.environ.get("LOCAL_RANK", 0)))
rank, ws = dist.get_rank(), dist.get_world_size()
group = UlyssesGroup()

heads, d = 40, 384                      # heads 必须被 ws 整除，否则先向上取整到 ws 的倍数
s_total = 75600 + 227
s_total -= s_total % ws
dev = torch.device("cuda", torch.cuda.current_device())
x = torch.randn(1, s_total // ws, heads, d, dtype=torch.bfloat16, device=dev)
a = torch.randn(s_total // ws, 5120, dtype=torch.bfloat16, device=dev)
w = torch.randn(5120, 5120, dtype=torch.bfloat16, device=dev)

def gemms():                            # 形似 to_q/k/v 的三条 GEMM
    for _ in range(3):
        _ = a @ w

def timed(fn, iters=16, warmup=4):
    for _ in range(warmup):
        fn()
    torch.cuda.synchronize(); dist.barrier()
    pairs = [(torch.cuda.Event(enable_timing=True), torch.cuda.Event(enable_timing=True))
             for _ in range(iters)]
    for s, e in pairs:
        s.record(); fn(); e.record()
    torch.cuda.synchronize()
    return statistics.median(s.elapsed_time(e) for s, e in pairs)

def serial():                           # 同步调用：交换与计算首尾相接
    group.all_to_all_4d(x, mode=0)
    gemms()

def concurrent():                       # 异步调用：交换藏进 GEMM 之下
    h = group.all_to_all_4d_async(x, mode=0)   # 立刻返回（结果在 comm 流上生产）
    gemms()                                    # caller 流继续算
    h.wait()                                   # 只把等待排进 caller 流，主机不阻塞

t_gemm = timed(gemms)
t_a2a  = timed(lambda: group.all_to_all_4d(x, mode=0))
t_ser  = timed(serial)
t_con  = timed(concurrent)
if rank == 0:
    print(f"gemm_alone {t_gemm:.3f} ms  a2a_alone {t_a2a:.3f} ms")
    print(f"serial {t_ser:.3f} ms   concurrent {t_con:.3f} ms")
    print(f"hidden {(t_ser - t_con) / t_a2a * 100:.0f}%")
group.destroy()
dist.destroy_process_group()
```

**要观察什么**：

- 理想形态下 \( t_{concurrent} \approx \max(t_{a2a}, t_{gemm}) \)，而 \( t_{serial} \approx t_{a2a} + t_{gemm} \)。隐藏率定义为

  \[ \text{hidden\%} = \frac{t_{serial} - t_{concurrent}}{t_{a2a}} \times 100\% \]

  即交换时间有多大比例消失在计算之下。
- `ws=1` 时没有 peer 传输（交换只剩自份额经窗口的搬运），隐藏率不具推广意义——流程能跑通、结论要 `N>=2` 才算数。
- 更严谨的版本应像 `run_overlap` 那样**交替**测量 serial 与 concurrent 并各取中位数：GEMM 时长的漂移与 a2a 本身同量级，交替能对消漂移。独占执行（`tools/exclusive.sh`）是数字有效的前提（u9-l4）。
- 全部数字**待本地验证**。

**书面论证（参考答案）**：`caller.stream_id` / `caller_device_index` 为什么必须由 Python 传入、不能在 C++ 里取当前流？

1. **时序上不可能**：staged 算子执行时，Python 已把 comm 流设为当前流，C++ 里 `getCurrentCUDAStream()` 只能得到 comm 流——caller 的流在这一刻对 C++ 不可见，必须在进入上下文**之前**捕获并作为参数带进来。
2. **正确性上不够**：即使能取到某条「当前流」，也无覆盖侧流场景——输入可能是在一条自定义侧流上生产的（correctness.py 的 ballast 测试），暂存拷贝必须排在那条流上才读得到正确的字节。
3. **设备维度**：无参的 `current_stream` 按「当前设备」取名，当前设备可能不是组的设备；取到的流与本组无关，也无法对本组张量做 `record_stream`。所以按 `self.device` 显式取名，设备号随流 id 一起传。
4. **传递形态**：schema 装不下流对象，只能以两个整数过界；C++ 侧 `Stream::unpack3` 重建出与 caller 所用**同一个**流对象，缓存分配器与 `record_stream` 的记账才对得上。这两个整数也正是 staged 算子拒绝被 trace 的理由之一——一条活的流被固化进图后，重放到哪里都是错的。

## 6. 本讲小结

- 异步调用与同步调用共享 u6-l2 的整条流水线，区别只在于它被搬到组级**高优先级 comm 流**上（sync 刻意不路由进去：两次事件跳的纯开销）；两条流互不排序，故窗口按 `kAsyncWindow`/`kSyncWindow` 分开。
- caller 流必须在 Python 侧捕获、以 `caller_stream_id` + `caller_device_index` 两个整数传入 C++——算子执行时「当前流」已是 comm 流，且输入可能在任意侧流上生产；C++ 用 `Stream::unpack3` 重建出同一个流对象。
- 输入在 **caller 流**上拷进按 `(shape, dtype)` 池化的暂存缓冲（comm 流只读暂存副本），输出做**两次** `record_stream` 盖住「分配流 × 消费流」两种组合。
- 完成事件记录在 comm 流上并经 `c10d::register_work` 绑到结果的 storage；`.wait()` 与首个 aten op 都只在 caller 流插入 GPU 侧等待、主机不阻塞；view 不等待只再包装。
- 纪律：等待或消费每一个结果（丢弃即注册表残留）；带梯度输入、`out=`+`lend` 组合都被显式拒绝而非静默出错；无 registry 构建退化成 `CompletedHandle`（正确、无重叠、类型可见）。

## 7. 下一步学习建议

- **u7-l2（暂存池）**：本讲只用了 `stage()` 的「输入不跨流」这一面；下一讲深入池的选择逻辑（`cudaEventQuery` 复用、每形状 4 槽、16 形状上限、满池等待）与 `StagingRelease` 在异常路径的释放保证。
- **u7-l3（工作注册表）**：`StreamEventWork` 与 `c10d::register_work` 的构建期探测（CMake 用 `nm` 探测符号）、无 registry 分支的完整行为差异清单。
- 动手参照：[benchmark/bench_a2a.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/benchmark/bench_a2a.py) 的 `overlap` 与 `zerosm` 两模式（隐藏率与零 SM 的测法联动，u5-l2），以及 [test/distributed/correctness.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py) 中全部 async 断言（非连续输入、5 并发在飞、侧流发起）。
