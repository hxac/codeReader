# 工作注册表：StreamEventWork 与完成事件

## 1. 本讲目标

前两讲（u7-l1、u7-l2）追完了异步调用的输入侧：caller 流捕获、暂存池、按形状复用的槽位。本讲补上**输出侧的最后一环**——`all_to_all_4d_async` 返回之后，调用者凭什么「不等地」拿到一个能安全使用的张量？

答案不在本库自己的代码里，而在 torch 的**工作注册表（work registry）**中：本库把 comm 流上的一个 CUDA 完成事件**注册到结果张量的 storage 上**，之后无论是 `.wait()` 还是任何 aten 算子第一次碰到这个结果，torch 都会替你插入跨流依赖——GPU 侧等待，主机不阻塞。

本讲回答三个问题：

1. `StreamEventWork` 如何把一个裸的 `cudaEvent_t` 包装成 torch 认识的 `c10d::Work`？`.wait()` 到底做了什么、没做什么？
2. 「按 storage 键绑定」意味着什么？为什么被丢弃的异步结果会在注册表里留下残留？没有注册表的构建为什么会**退化**成 `CompletedHandle`，退化后行为差在哪？
3. CMake 为什么用 `nm` 探测 libtorch_cpu 的**导出符号**、而不是读头文件里的函数声明，来决定编译哪条分支？

## 2. 前置知识

- **c10d::Work**：torch 分布式里「一次异步集合通信的句柄」的 C++ 接口——NCCL 的 allreduce、Gloo 的 broadcast 返回的都是它的子类。核心虚方法：`wait(timeout)`（等待完成）、`isCompleted()`（查询）、`synchronize()`（让**主机**阻塞到完成）。本库要借用的正是这套接口。
- **c10d::register_work 与 wait_tensor**：torch 的 `_functional_collectives` 机制提供一对配套设施——`register_work(tensor, work)` 把一个 `Work` 绑到某个张量身上；之后 `wait_tensor(tensor)` 会等**所有**绑在这个张量身上的 `Work`。`AsyncCollectiveTensor` 的 `.wait()` 与「第一个 aten 算子触发隐式等待」正是经由这条通道实现的（仓库内的证据见 [test/distributed/overlapping_barriers.py:74-76](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/overlapping_barriers.py#L74-L76) 的注释：显式 `.wait()` 直接调 `wait_tensor`，只有 `trigger_wait` 设置 completed 标志）。
- **storage 与张量的区别**：torch 张量由「storage（一块内存）+ 视图信息（偏移、步长、形状）」构成。注册表按 **storage** 记账而不是按张量对象——两个共享同一 storage 的视图命中同一个注册表条目。这一细节是本讲 4.2 的主角。
- **CUDA 事件三操作**（u7-l2 已建立）：`cudaEventRecord(ev, s)` 在流 s 的当前位置打完成标记；`cudaStreamWaitEvent(s, ev, 0)` 让流 s 停在 ev 触发前——**GPU 侧等待，主机立刻返回**；`cudaEventSynchronize(ev)` 才是**主机**阻塞。`cudaEventDisableTiming` 标志表示该事件只作同步标记、不计时，更便宜。
- **动态符号表与 `nm -D`**：一个 `.so` 的「导出符号」（别的库可以链接到的函数名）记录在动态符号表里，`nm -D --defined-only <lib>` 把它们列出来。C++ 函数名会经 Itanium ABI 规则**修饰（mangling）**成带类型信息的字符串。
- **承接 u7-l1**：comm 流是 group 构造时创建的最高优先级流，与 caller 流互不排序；异步调用整段（stage 的 comm 侧等待 → 屏障 → CE 传输 → 屏障 → copy_out）都排在 comm 流上。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [include/fast_ulysses/work.hpp](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/work.hpp#L1-L19) | 整个机制的契约注释（为什么存在、storage 键的含义）与 `register_stream_completion` 声明，全文 18 行 |
| [src/work.cc](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/work.cc#L1-L83) | `StreamEventWork`（L22-L56）与 `register_stream_completion`（L62-L80）的全部实现 |
| [CMakeLists.txt](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/CMakeLists.txt#L90-L109) | `c10d::register_work` 的 nm 探测段；L147-L154 据此定义 `FAST_ULYSSES_HAS_WORK_REGISTRY` 并在缺失时告警 |
| [python/fast_ulysses/group.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L151-L232) | 调用侧：`CompletedHandle`（L20-L32）与 `all_to_all_4d_async` 尾部的注册、`record_stream`、包装（L227-L232） |
| [src/bindings.cc](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L775-L783) | `_C.register_stream_completion` 的 pybind 暴露，及其「为什么不是 torch op」的注释 |
| [setup.py](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/setup.py#L109-L141) | 把探测结果从 CMake 的 configure 输出里捞出来，写进 `_build_meta.py` |
| [tools/preflight_gpu.sh](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/tools/preflight_gpu.sh#L62-L66) | 上架前自检打印 `work_registry=...`，让「这个 wheel 走哪条路径」无需 dlopen 即可见 |
| [docs/design.md](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/design.md#L177-L185) | 设计文档「The async result」一节：storage 键、丢弃残留、退化路径 |

一个分层事实：本讲的 C++ 部分极小（一个 83 行的 `.cc` + 一个 18 行的 `.hpp`），而且**只被 Python 调用一次**——`group.py` 第 227 行。它存在的全部理由，是把本库的 CUDA 事件翻译进 torch 已有的 functional-collectives 机制，让「等待」这件事对调用者自动发生。

## 4. 核心概念与源码讲解

### 4.1 StreamEventWork：把 CUDA 事件包装成 c10d::Work

#### 4.1.1 概念说明

torch 的 `wait_tensor` 只认 `c10d::Work`。本库的「完成信号」却只是一个 `cudaEvent_t`——comm 流上一个普通的完成标记。`StreamEventWork` 就是这两者之间的适配器：**一个完成状态就是 CUDA 事件的 Work**。

它的设计要点在类注释里一句话说尽（[src/work.cc:19-21](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/work.cc#L19-L21)）：

> wait() enqueues a dependency on whatever stream is current when the wait happens and returns immediately -- the host is not part of the handshake.

翻译过来：`.wait()` 把依赖插入**等待发生时刻的当前流**，然后立刻返回——主机全程不参与握手。这与 NCCL Work 常见的「主机等通信器」截然不同，也正因如此，异步交换才能与 caller 流的计算真正重叠。

#### 4.1.2 核心流程

`StreamEventWork` 四个虚方法的语义对照：

| 方法 | CUDA 调用 | 主机是否阻塞 | 谁来调 |
| --- | --- | --- | --- |
| `wait(timeout)` | `cudaStreamWaitEvent(当前流, event)` | 否 | torch 的 `wait_tensor`（由 `.wait()` 或首个 aten op 触发） |
| `synchronize()` | `cudaEventSynchronize(event)` | **是** | 需要主机真等的调用方，如收尾 |
| `isCompleted()` | `cudaEventQuery(event)` | 否 | torch 查询进度 |
| `isSuccess()` | 恒 `true` | 否 | torch 查询成败（事件只有「已触发 / 未触发」两种状态） |

生命周期：构造时**接过事件的所有权**（不复制、不新建）；析构时销毁事件——这是注释里说的「registry owns the Work, and the Work owns the event」链条的末端。

#### 4.1.3 源码精读

类的完整定义只有 30 多行：

[src/work.cc:22-36](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/work.cc#L22-L36)——`StreamEventWork` 继承 `c10d::Work`，构造函数只存指针；析构函数调用 `cudaEventDestroy(event_)`，注意注释「Unchecked: this runs from the registry's teardown, where a throw has nowhere to go」——析构从注册表销毁路径调用，异常无处可去，所以不做错误检查（与 [include/fast_ulysses/common.hpp:43](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/common.hpp#L43) 里 RAII `Event` 包装的同一纪律一致；这里没用 RAII 包装，因为所有权要**移交**给注册表，create/destroy 分居两处，正是 common.hpp 注释所说的 hand-paired 形态）。

```cpp
bool wait(std::chrono::milliseconds /*timeout*/) override
{
    ULYSSES_CUDA_CHECK(cudaStreamWaitEvent(at::cuda::getCurrentCUDAStream(), event_, 0));
    return true;
}
```

这是全类的灵魂：**在「现在」的流上插一个 GPU 侧等待**。timeout 参数被无视——CUDA 事件没有超时等待的原语，返回 `true` 表示「已安排」。对照 [src/work.cc:39-47](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/work.cc#L39-L47)：`synchronize()` 才调用真正阻塞主机的 `cudaEventSynchronize`，注释写明它是给「要主机阻塞的调用方，例如收尾」准备的；`isCompleted()` 用 `cudaEventQuery` 与 `cudaSuccess` 比较。

注意整段被 `#if FAST_ULYSSES_HAS_WORK_REGISTRY`（[src/work.cc:15](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/work.cc#L15)）包住：没有注册表的构建里这个类根本不存在，避免链接一个不存在的 `c10d::register_work` 符号（4.4 详述）。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：把 `StreamEventWork` 的四个虚方法与 CUDA 事件三操作（u7-l2 前置知识）一一对上，并找出「谁最终调用了析构函数」。
2. **操作步骤**：通读 [src/work.cc:22-56](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/work.cc#L22-L56)；然后追 `event_` 的完整一生——在 `register_stream_completion` 里 `cudaEventCreateWithFlags` 创建（L66）、被 `c10d::register_work` 交给注册表（L71）、在析构里销毁（L29）。
3. **需要观察的现象**：事件的生命周期与 `StreamEventWork` 对象完全重合；本库没有任何代码直接调用 `~StreamEventWork`——它由注册表持有的 `intrusive_ptr` 引用计数归零时触发。
4. **预期结果**：能回答「如果把 `wait()` 里的 `getCurrentCUDAStream()` 换成构造时保存的 comm 流会发生什么」——等待会插在 comm 流自己身上，等于自己等自己已经排完的作业，caller 流完全得不到保护。

#### 4.1.5 小练习与答案

**练习 1**：`wait()` 返回 `true` 代表「传输完成了吗」？

答案：不代表。它只代表「等待命令已成功排入当前流」。返回后主机继续跑，GPU 上当前流的后续工作会停在事件触发之前——这正是「主机不参与握手」的含义。真正查询完成状态的是 `isCompleted()`。

**练习 2**：为什么 `isSuccess()` 不做任何检查、恒返回 `true`？

答案：CUDA 事件只有「未触发 / 已触发」两态，不是可查询错误的对象；传输本身的 CUDA 错误会在流上以别的方式暴露（后续操作返回错误、或被 `ULYSSES_CUDA_CHECK` 捕获）。对这个 Work 而言「已触发」即「成功」，不存在「完成了但失败」的中间态可报告。

**练习 3**：`synchronize()` 的预期调用者是谁？（提示：看 `destroy()`。）

答案：需要主机真正阻塞的场合，注释点名「e.g. at teardown」。本库自己的收尾在 Python 侧：[python/fast_ulysses/group.py:272-275](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L272-L275) 的 `destroy()` 先 `self._comm_stream.synchronize()` 排干 comm 流再销毁窗口——同一个「收尾前主机必须真等」的需求。

### 4.2 register_stream_completion：按 storage 绑定完成事件

#### 4.2.1 概念说明

适配器有了，还差「登记」这一步。`register_stream_completion(tensor, comm_stream)` 做三件事：

1. 在 comm 流的**当前位置**记录一个不计时事件——此刻 comm 流上已排入的正是本次异步调用的全部工作（stage 的 comm 侧等待、两道屏障、CE 传输、copy_out），所以事件触发即「整次交换已落地」；
2. 把事件包进 `StreamEventWork`，用 `c10d::register_work(tensor, ...)` 绑到结果张量上；
3. 返回 `true`，告诉 Python 侧「事件已是注册表的，你可以包 `AsyncCollectiveTensor` 了」。

**为什么按 storage 键是对的**：头文件注释（[include/fast_ulysses/work.hpp:5-6](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/work.hpp#L5-L6)）给出准确表述——`wait_tensor` 等**每一个**绑在该张量 storage 上的 Work，「所以条目属于产出这个结果的那次调用，而不是它恰好占用的那块缓冲」。这一点对本库尤其要紧：async 窗口按 (role, dtype) 复用（u4-l2）、lend 槽按调用计数轮转（u4-l4），**同一个缓冲会被多次调用轮流占用**。若注册表按缓冲记账，第二次调用就会撞上第一次的条目；按 storage（即这一次调用产出的那个张量对象背后的内存 + 引用）记账，条目天然与调用一一对应。

#### 4.2.2 核心流程

调用发生时（Python 侧，[python/fast_ulysses/group.py:227](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L227)）的完整时序：

```text
comm 流:   [stage读就绪等待][屏障][CE 传输][屏障][copy_out → y]  ← 全部已排入
caller 流: （当前流 = caller；with 块已退出）
           register_stream_completion(y, comm):
             cudaEventCreateWithFlags(DisableTiming)
             cudaEventRecord(ev, comm)        ← 排在 copy_out 之后
             c10d::register_work(y, StreamEventWork(ev))
             return true
           y.record_stream(comm); y.record_stream(caller)
           return AsyncCollectiveTensor(y)

之后某刻（用户代码）:
           y.wait()  或  首个 aten op 碰 y
             → torch 调 Work::wait()
             → cudaStreamWaitEvent(当时的当前流, ev)   ← GPU 侧，主机即刻返回
```

两个容易忽略的细节：

- **调用时的当前流必须是 caller 流**。头文件契约写明「call it with the CALLER's stream current」（[include/fast_ulysses/work.hpp:12-13](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/work.hpp#L12-L13)）。`group.py` 里这一行在 `with torch.cuda.stream(self._comm_stream)` 块**之外**（L227，with 块是 L203-L226），所以当前流恰好是 caller 流——这不仅是风格，4.3 会看到退化路径直接依赖它。
- **事件必须不计时**（`cudaEventDisableTiming`）：它只当同步标记用，计时事件有额外开销。

#### 4.2.3 源码精读

[include/fast_ulysses/work.hpp:1-6](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/work.hpp#L1-L6)——头文件开篇注释讲清了整个机制的存在理由：把 comm 流上完成的结果绑进 torch 的 functional-collective 机器，使 async 调用能返回一个「`.wait()` 或任何 aten 算子首次使用时自动插入跨流依赖」的 `AsyncCollectiveTensor`，而不是一个**还在被写入就被人静默读走**的缓冲。

[include/fast_ulysses/work.hpp:12-16](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/include/fast_ulysses/work.hpp#L12-L16)——函数契约：在 `comm_stream` 上记录完成事件并绑到 `tensor`；返回 `true` 表示事件已归注册表所有，返回 `false` 表示本构建无注册表（见 4.3）。

[src/work.cc:62-72](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/work.cc#L62-L72)——实现主体：

```cpp
bool register_stream_completion(const at::Tensor& tensor, cudaStream_t comm_stream)
{
    const at::cuda::CUDAGuard guard(tensor.device());
    cudaEvent_t               event = nullptr;
    ULYSSES_CUDA_CHECK(cudaEventCreateWithFlags(&event, cudaEventDisableTiming));
    ULYSSES_CUDA_CHECK(cudaEventRecord(event, comm_stream));

#if FAST_ULYSSES_HAS_WORK_REGISTRY
    // The registry owns the Work, and the Work owns the event.
    c10d::register_work(tensor, c10::make_intrusive<StreamEventWork>(event));
    return true;
```

`CUDAGuard` 先切到张量所在设备（CUDA 的流 / 事件句柄是 per-device 的）；创建、记录、注册三步，所有权链「注册表 → Work → 事件」在注释里一行写明。

Python 侧的调用与包装在 [python/fast_ulysses/group.py:227-232](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L227-L232)：

```python
registered = _C.register_stream_completion(y, self._comm_stream.cuda_stream)
# Two streams touch the output: one allocated it, the other wrote it, and the caching
# allocator must know about the cross-stream use before it may recycle the block.
y.record_stream(self._comm_stream)
y.record_stream(caller)
return AsyncCollectiveTensor(y) if registered else CompletedHandle(y)
```

返回值 `registered` 直接决定包装类型；两次 `record_stream` 是缓存分配器侧的记账（注释：一个流分配了它、另一个流写了它，回收前必须知道跨流使用）——这是**内存安全**，与注册表的**同步语义**互相独立、缺一不可。

pybind 暴露在 [src/bindings.cc:781-783](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/bindings.cc#L781-L783)，其上方注释（L778-L780）解释了它为什么是 pybind 函数而不是 torch op：**它接收裸流句柄、并改写进程级注册表，两者都进不了 schema**。这与 u6-l1 的分层原则一致——进得了 schema 的都注册成 op，进不去的留在 pybind。

最后是文档化的**残留语义**：[docs/api.md:118-120](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/api.md#L118-L120)——「每个结果都要 wait 或使用；被丢弃的结果在 torch 的 work registry 里留下条目和它的 CUDA 事件，torch 在进程退出时打印幸存者数量。`out=` 是漏洞，因为读你自己的 `out` 从不经过注册表——要读返回的包装」。`out=` 形态下 Python 侧拿到的是你传入的缓冲（[python/fast_ulysses/group.py:216-226](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L216-L226)），等它的人不存在，唯一的保护是调用瞬间已排好的流依赖。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：验证「等待确实经由注册表、且有两个不同入口」这一说法在仓库内有据可查。
2. **操作步骤**：
   - 读 [test/distributed/correctness.py:203-208](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/correctness.py#L203-L208)：5 个同形状异步调用先全部发出、句柄存进 `handles`，**之后**才逐个 `h.wait()` 并比对——若 wait 会阻塞主机等传输完成，这个测试就测不到任何并发。
   - 读 [test/distributed/overlapping_barriers.py:70-76](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/test/distributed/overlapping_barriers.py#L70-L76)：「touching the wrapper with any aten op, which waits implicitly」——隐式等待即首个 aten op 触发；且注释明确「显式 `.wait()` 不设置 completed 标志（它直接调 `wait_tensor`），只有 `trigger_wait` 设置」。
3. **需要观察的现象**：两处证据分别覆盖显式与隐式两个入口，且都依赖「wait 只插 GPU 侧依赖」这一性质。
4. **预期结果**：能写出这句话——`.wait()` 和「首个 aten op」最终都汇到 `wait_tensor`，区别只是谁触发、是否标记 wrapper 状态；view op 两边都不沾（只再包装，见 [docs/api.md:116](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/api.md#L116)）。

#### 4.2.5 小练习与答案

**练习 1**：async 窗口按 (role, dtype) 复用（u4-l2）。第二次异步调用复用同一块窗口内存时，第一次调用留下的注册表条目会不会让第二次调用的 `.wait()` 多等？

答案：不会。条目绑在**第一次调用产出的张量**的 storage 上；第二次调用产出的是新张量（对窗口的新视图，storage 层面是这次调用登记的那个对象），`wait_tensor` 只等「绑在这个张量上的」Work。窗口内存的复用安全由另一套机制保证：单缓冲窗口的读写竞态由两道屏障与「只有被同一条流排序的调用才能共享窗口」的规则管（u4-l2、u9-l3）。

**练习 2**：如果把结果 `y` 切一片 `v = y[0]` 后丢弃 `y`、只等 `v`，注册表条目还能被触发吗？

答案：能。`v` 与 `y` 共享同一 storage，而注册表按 storage 记账——这正是「条目属于调用而非缓冲」设计的直接红利。（注意 `AsyncCollectiveTensor` 的 view 会再包装而不等待，最终对视图的首次 aten 使用仍汇到同一 storage 的条目。）

**练习 3**：为什么 `record_stream` 与 `register_work` 必须都做，只做其中一个会怎样？

答案：只注册不等（漏 `record_stream`）：同步对了，但 caller 流释放、comm 流还在写的那块内存可能被分配器回收复用——数据竞争。只 `record_stream` 不注册：内存安全了，但没有任何机制让后续读者插入跨流依赖——读到半成品数据，且无报错。两者一个管「内存何时可复用」、一个管「读取何时可开始」。

### 4.3 CompletedHandle 退化：没有注册表时怎么办

#### 4.3.1 概念说明

`c10d::register_work` 是 torch 较新版本才导出的符号（它是 `_functional_collectives` 机制的一部分）。装着旧版 torch 的机器上链接不到它——本库的选择不是拒绝安装，而是**编译期切到退化路径**，并把退化显式地暴露给用户：

- C++ 侧：不注册任何东西，改为**在调用瞬间**就让 caller 流等上完成事件（「eager ordering」），随后立刻销毁事件，返回 `false`；
- Python 侧：返回 `CompletedHandle` 而不是 `AsyncCollectiveTensor`——**一个不同类型**，让「这次调用没有重叠」从 `type(y)` 就看得见（[python/fast_ulysses/group.py:20-23](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L20-L23) 的类注释）。

语义总结（[docs/api.md:145-147](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/docs/api.md#L145-L147)）：同样的 `.wait()`、正确的结果、**没有重叠**；用不同类型让它可见。

#### 4.3.2 核心流程

退化路径的时序（对照 4.2.2）：

```text
comm 流:   [stage][屏障][CE 传输][屏障][copy_out → y]
caller 流: register_stream_completion(y, comm):     ← 当前流仍是 caller
             create + record(ev, comm)              ← 与注册路径相同
             cudaStreamWaitEvent(caller, ev)        ← 退化的关键：立刻插等待
             cudaEventDestroy(ev)                   ← 立刻销毁
             return false
           return CompletedHandle(y)

之后:      handle.wait() → 直接返回 self._tensor（解包而已，无任何 GPU 操作）
```

正确性论证藏在注释里（[src/work.cc:74-75](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/work.cc#L74-L75)）：「Nothing can reach this event through torch（没有注册表，torch 够不到这个事件），所以在这里就把它对齐到 caller 流。立刻销毁是安全的：wait 已经捕获了事件被记录时的状态，销毁会推迟（destroy defers）。」——CUDA 语义允许销毁一个仍被流等待引用的事件，运行时会推迟真正的回收。

#### 4.3.3 源码精读

退化分支整个是 `#else` 三行（[src/work.cc:73-79](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/src/work.cc#L73-L79)）：

```cpp
#else
    // Nothing can reach this event through torch, so order it against the caller here. Destroying it
    // immediately is safe: the wait captured the event's recorded state, and destroy defers.
    ULYSSES_CUDA_CHECK(cudaStreamWaitEvent(at::cuda::getCurrentCUDAStream(), event, 0));
    ULYSSES_CUDA_CHECK(cudaEventDestroy(event));
    return false;
#endif
```

注意 `getCurrentCUDAStream()` 在这里取到的是 caller 流——因为 `group.py` 的调用点在 comm 流上下文之外。这就是头文件契约「call it with the CALLER's stream current」的另一半原因：注册路径只是「恰好无害」，退化路径**直接依赖**它把等待插对地方。

Python 侧的退化类型（[python/fast_ulysses/group.py:20-32](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/python/fast_ulysses/group.py#L20-L32)）：

```python
class CompletedHandle:
    """What the async call returns on a build whose libtorch has no ``c10d::register_work``. ..."""
    def __init__(self, tensor: torch.Tensor) -> None:
        self._tensor = tensor

    def wait(self) -> torch.Tensor:
        return self._tensor
```

`wait()` 只是解包。CMake 侧对应的人类可读告警（[CMakeLists.txt:150-153](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/CMakeLists.txt#L150-L153)）把后果说全：「async 调用将返回 CompletedHandle 而非 AsyncCollectiveTensor，caller 流在 launch 时就被排定等待——正确，但没有重叠」。

#### 4.3.4 代码实践（清单编写，综合实践的第 2 问在此准备）

1. **实践目标**：写出 `FAST_ULYSSES_HAS_WORK_REGISTRY=0` 构建下 async 调用的**完整**行为差异清单（这是本讲综合实践任务的后半部分，此处先列提纲）。
2. **操作步骤**：对照 4.2.2 与 4.3.2 的两条时序图，沿 `group.py` 的 `all_to_all_4d_async` 逐行走一遍退化路径：L182-L197 的两道拒绝检查、L202 caller 流捕获、L203-L226 comm 流上下文与 staged 算子、L227 注册（退化）、L229-L231 `record_stream`、L232 包装。
3. **需要观察的现象**：哪些步骤**完全不变**（拒绝检查、staging、staged 算子、record_stream、lend 轮转），哪些**改变**（L227 的 C++ 分支、L232 的返回类型、等待插入的时机、事件生命周期、丢弃结果的后果）。
4. **预期结果**：得到一份不少于 6 条的差异清单（参考答案见综合实践；亦可在 [tools/preflight_gpu.sh:62-66](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/tools/preflight_gpu.sh#L62-L66) 打印的 `work_registry=` 字段处核对该机器实际走哪条路径）。

#### 4.3.5 小练习与答案

**练习 1**：退化路径「正确但无重叠」——到底什么被串行化了？主机也被阻塞了吗？

答案：被串行化的是 **caller 流上的后续 GPU 工作**：launch 瞬间插的 `cudaStreamWaitEvent` 让 caller 流排在整次交换之后，所以「交换与 caller 计算重叠」这一收益消失。主机仍然不阻塞——`all_to_all_4d_async` 照样立刻返回，用户若在其他流上有工作也仍可重叠。

**练习 2**：为什么退化路径敢立刻 `cudaEventDestroy`，而注册路径的事件要活到 Work 析构？

答案：退化路径里事件唯一的消费者是那一条已排入 caller 流的等待；CUDA 语义下销毁一个仍被等待引用的事件是安全的（运行时推迟回收）。注册路径的事件要被**未来的、不确定时刻的** `wait()` 反复使用——`cudaStreamWaitEvent`、`cudaEventQuery` 都还会碰它——所以必须活到注册表销毁 Work 为止。

**练习 3**：`CompletedHandle` 为什么必须是**不同类型**，而不是同一个 `AsyncCollectiveTensor` 加个标志位？

答案：不同类型让「没有重叠」变成调用方 `isinstance` 一查便知的事实，而不是藏在文档里的运行时差异——用户可以据此决定要不要调整用法（比如接受无重叠、或升级 torch）。这正是其类注释「a distinct type so that is visible」的设计意图；同一策略也出现在「`out=` 不可微直接 raise 而非静默丢梯度」（u4-l3）——**把降级显式化**是本库的一贯纪律。

### 4.4 nm 符号探测：CMake 怎么知道有没有注册表

#### 4.4.1 概念说明

`FAST_ULYSSES_HAS_WORK_REGISTRY` 的取值不是写死的版本号对照表，而是构建时对**即将链接的那份 libtorch_cpu** 做的一次实测。注释（[CMakeLists.txt:90-98](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/CMakeLists.txt#L90-L98)）给出了核心论证：

> Probe the LIBRARY, not the header: a declaration says nothing about what libtorch_cpu exports.

**为什么探测库而不是头文件**：扩展 `_C` 是动态链接 `libtorch_cpu.so` 的，符号能不能解析取决于**库导没导出**它。torch 的安装包里始终带着完整的 `torch/include` 头文件——声明可能存在而库不导出（函数被删、改为内联、或这个构建没编进去）。若只查头文件：编译通过、链接失败或 `import` 时 undefined symbol——错误被推迟到离原因最远的地方。`nm -D --defined-only` 列出的是库的**动态符号表（导出表）**，一步问到动态链接器关心的那个问题。

**为什么探测 mangled 前缀**：C++ 的 Itanium ABI 会把 `c10d::register_work(const at::Tensor&, ...)` 修饰成一条带类型信息的字符串。CMake 搜的子串是：

```text
_ZN4c10d13register_workERKN2at6Tensor
```

拆开读：

| 片段 | 含义 |
| --- | --- |
| `_ZN` | 嵌套名（namespace 限定的函数）开始 |
| `4c10d` | 4 个字符的名字：`c10d` |
| `13register_work` | 13 个字符的名字：`register_work` |
| `E` | 嵌套名部分结束，后面是参数列表 |
| `RKN2at6Tensor` | `R`=引用，`K`=const，`N2at6Tensor...`=嵌套类型 `at::Tensor` |

这是一次**前缀匹配**：完整的修饰名还会继续拼上第二个参数（`c10::intrusive_ptr<c10d::Work>` 的模板修饰），但注释说明该子串已唯一钉住目标重载——`c10d::register_work` 里以 `(const at::Tensor&, ...)` 开头的就这一个，于是第二个模板参数怎么修饰都无所谓。这是用「截断到模板参数之前」换来的、对模板细节不敏感的探测。

探测失败**不是错误**：注释写明「Missing it is not fatal——src/work.cc then orders the result eagerly and group.py hands back a CompletedHandle instead」，随后只是 WARNING（[CMakeLists.txt:150-153](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/CMakeLists.txt#L150-L153)）。这正是 4.3 退化路径的入口。

#### 4.4.2 核心流程

```text
find_package(Torch) 之后:
1. find_library(TORCH_CPU_LIBRARY torch_cpu PATHS ${TORCH_INSTALL_PREFIX}/lib)   # 找到那份 .so
2. execute_process(${CMAKE_NM} -D --defined-only libtorch_cpu.so)                # 列导出符号
   ↳ NM_STATUS 非 0（比如 nm 不存在）⇒ 当作没有，不报错
3. string(FIND 输出, "_ZN4c10d13register_workERKN2at6Tensor")                     # 前缀匹配
4. 有 ⇒ HAVE_C10D_REGISTER_WORK=ON；configure 输出一行状态
5. 据此给 _C 定义 FAST_ULYSSES_HAS_WORK_REGISTRY=1/0
6. setup.py 从 configure 的 stdout 里捞这行状态，写进 _build_meta.py
   （has_work_registry="c10d::register_work available: ON" in configure.stdout）
```

两个工程细节值得注意：

- **「Unquoted ${CMAKE_NM}」**（[CMakeLists.txt:96-97](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/CMakeLists.txt#L96-L97)）：如果 `CMAKE_NM` 未定义，加引号会传一个空字符串参数、CMake 直接报错；不加引号则命令失败、`RESULT_VARIABLE` 报告失败——退化而非崩溃。
- **构建事实快照**：这行状态只出现在 configure 输出和编译出的扩展里；`setup.py` 把它捞出来写进 `_build_meta.py`（[setup.py:109-111](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/setup.py#L109-L111) 的注释解释：dlopen 本身失败时 `build_info()` 无法回答，而那恰恰是最需要答案的时刻），于是 `tools/preflight_gpu.sh` 无需 import 扩展就能打印 `work_registry=...`。

#### 4.4.3 源码精读

[CMakeLists.txt:99-109](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/CMakeLists.txt#L99-L109)——探测段本体：

```cmake
find_library(TORCH_CPU_LIBRARY torch_cpu PATHS "${TORCH_INSTALL_PREFIX}/lib" NO_DEFAULT_PATH REQUIRED)
execute_process(COMMAND ${CMAKE_NM} -D --defined-only "${TORCH_CPU_LIBRARY}"
        OUTPUT_VARIABLE TORCH_CPU_SYMBOLS RESULT_VARIABLE NM_STATUS ERROR_QUIET)
set(HAVE_C10D_REGISTER_WORK OFF)
if (NM_STATUS EQUAL 0)
    string(FIND "${TORCH_CPU_SYMBOLS}" "_ZN4c10d13register_workERKN2at6Tensor" REGISTER_WORK_AT)
    if (NOT REGISTER_WORK_AT EQUAL -1)
        set(HAVE_C10D_REGISTER_WORK ON)
    endif ()
endif ()
message(STATUS "c10d::register_work available: ${HAVE_C10D_REGISTER_WORK}")
```

`NO_DEFAULT_PATH` 确保查的是**本次链接的那份** torch 的 lib 目录，不会被系统里另一份 libtorch 抢走；默认值 `OFF`、只有 nm 成功且找到符号才翻成 `ON`——一切失败模式都倒向「退化但正确」。

[CMakeLists.txt:147-154](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/CMakeLists.txt#L147-L154)——探测结果变成编译宏：有则 `FAST_ULYSSES_HAS_WORK_REGISTRY=1`；无则 WARNING + `=0`。`src/work.cc` 开头的 `#if`（L6-L9、L15）据此决定 `<torch/csrc/distributed/c10d/...>` 头文件、`StreamEventWork` 类、`register_work` 调用是否参与编译——**没探测到就连声明都不出现**，链接自然安全。

[setup.py:137-141](https://github.com/triple-mu/fast-ulysses/blob/ad1a4a06a42d8129a278f010b1d67a6e206d38e6/setup.py#L137-L141)——`has_work_registry` 的快照写入：用 `"c10d::register_work available: ON" in configure.stdout` 这个字符串判断从 CMake 的 STATUS 行里捞事实，与 `_diagnose`、`doctor`、preflight 共享同一份真相。

#### 4.4.4 代码实践（本机可做，无 GPU 要求）

1. **实践目标**：亲手复现 CMake 的探测，确认你环境里的 torch 走哪条路径。
2. **操作步骤**：
   ```bash
   python -c "import torch, os; print(os.path.join(os.path.dirname(torch.__file__), 'lib', 'libtorch_cpu.so'))"
   # 把上面打印的路径代入下一行（macOS 是 libtorch_cpu.dylib，torch 官方 wheel 主要是 linux）
   nm -D --defined-only <那个路径>/libtorch_cpu.so | grep -c _ZN4c10d13register_workERKN2at6Tensor
   python -c "import torch; print(torch.__version__)"
   ```
   再查本仓库构建事实：`python -c "from fast_ulysses import _build_meta as m; print(m.HAS_WORK_REGISTRY)"`（若已安装），或在开发构建的 configure 输出里找 `c10d::register_work available:` 一行。
3. **需要观察的现象**：grep 计数为 ≥1 时注册路径可用、`AsyncCollectiveTensor` 生效；为 0 时是 `CompletedHandle` 路径。`_build_meta` 的值应与 nm 结果一致。
4. **预期结果**：较新的 torch（含 functional-collectives 的 work registry）计数 ≥1；如果本机 torch 较旧且计数为 0，`pip install -e . --no-build-isolation` 的 configure 输出里应出现那条 WARNING。具体数值**待本地验证**（本教程编写环境未装 torch，未代跑）。

#### 4.4.5 小练习与答案

**练习 1**：把探测改成「在头文件里 grep `register_work`」会在什么情况下给出错误答案？

答案：头文件声明存在、但那份 `libtorch_cpu.so` 没有导出该符号时（例如函数在那个构建里被移除或未编入，而 include 目录随包完整分发）。结果是编译通过、运行时 `_C` 的 dlopen/动态解析失败——`undefined symbol`，一个比「configure 时一行 WARNING + 退化路径」晚得多、也难懂得多的失败。

**练习 2**：探测为什么用**前缀** `_ZN4c10d13register_workERKN2at6Tensor`，而不是把完整修饰名（含第二个参数的 `intrusive_ptr` 模板）写全？

答案：第二个参数是模板类型 `c10::intrusive_ptr<c10d::Work>`，其修饰串长且依赖模板实现细节（默认 Deleter 等）；截断在第一个参数之后已唯一锁定重载（`c10d::register_work` 没有别的以 `(const at::Tensor&, ...)` 开头的重载），既避免拼写超长模板串出错，也对模板参数的无关变化不敏感。

**练习 3**：`CMAKE_NM` 为空时这段代码的行为是什么？为什么这样设计？

答案：`execute_process` 的 COMMAND 以一个不存在的程序名运行而失败，`NM_STATUS != 0`，`HAVE_C10D_REGISTER_WORK` 保持 `OFF`，构建继续走退化路径。设计意图（注释 L96-L97）：**任何探测失败都倒向「能用但无重叠」**，而不是让安装失败——与 NVLink 检查的 fail-open（u8-l1）同一种取舍：探测是优化，不是正确性的前提。

## 5. 综合实践

**任务：证明「两个等待入口都不阻塞主机」，并写出退化路径的行为差异清单。** 需要 ≥2 GPU 才能真正交换数据（world_size=1 时交换退化为本地拷贝，但时序机制完全相同，单卡也可观察主机行为）。

第 1 部分——测量脚本（示例代码，保存为 `u7l3_practice.py`，用 `torchrun --nproc_per_node=<N> u7l3_practice.py` 运行）：

```python
# 示例代码：验证 async 结果的两个等待入口都只插 GPU 侧依赖、主机即刻返回。
import time
import torch
import torch.distributed as dist
from fast_ulysses import UlyssesGroup

dist.init_process_group("nccl")
rank, ws = dist.get_rank(), dist.get_world_size()
dev = torch.device("cuda", torch.cuda.local_rank())
group = UlyssesGroup(device=dev)

b, s, n, d = 1, 8192, 8 * ws, 128          # d*dtype 达 16B 对齐；总量够大才有可测的传输时长
x = torch.randn(b, s, n // ws, d, dtype=torch.bfloat16, device=dev)
torch.cuda.synchronize()

# 入口一：显式 .wait()
h1 = group.all_to_all_4d_async(x, mode=0)
t0 = time.perf_counter(); y1 = h1.wait(); host_wait_us = (time.perf_counter() - t0) * 1e6

# 入口二：首个 aten op 隐式等待（另一次调用、另一个句柄）
h2 = group.all_to_all_4d_async(x, mode=0)
t0 = time.perf_counter(); z = h2 + 1; host_aten_us = (time.perf_counter() - t0) * 1e6

# 参照：真正等到传输完成的耗时（主机阻塞）
t0 = time.perf_counter(); torch.cuda.synchronize(); drain_us = (time.perf_counter() - t0) * 1e6

if rank == 0:
    print(f"wait() host {host_wait_us:.1f}us | aten op host {host_aten_us:.1f}us | "
          f"drain {drain_us:.1f}us | type(h1)={type(h1).__name__}")
group.destroy(); dist.destroy_process_group()
```

- **观察与预期**：前两个数应是**微秒量级**（各只是一次 `cudaStreamWaitEvent` / 一次子类分发 + 等待入队），`drain` 则包含真正的交换时长、显著更大——差距本身就是「主机不参与握手」的证据。若两个入口的耗时而接近 `drain`，说明等待被悄悄做成了主机阻塞，属异常。精度更高的做法是用 `nsys profile torchrun ...` 看 `cudaStreamWaitEvent` 是否只出现在 GPU 时间线。具体数值**待本地验证**。
- 顺带核对 `type(h1).__name__`：注册路径是 `AsyncCollectiveTensor`，退化路径是 `CompletedHandle`——脚本因此同时充当路径指示器。

第 2 部分——退化路径行为差异清单（对照 4.3 的时序图作答，参考答案）：

| # | 维度 | `FAST_ULYSSES_HAS_WORK_REGISTRY=1` | `=0`（CompletedHandle 路径） |
| --- | --- | --- | --- |
| 1 | 返回类型 | `AsyncCollectiveTensor` | `CompletedHandle`（类型即信号） |
| 2 | 等待插入时机 | **使用时刻**（`.wait()` 或首个 aten op） | **launch 时刻**（C++ 里即刻 `cudaStreamWaitEvent`） |
| 3 | 等待插在哪条流 | 使用时刻的当前流 | 固定 caller 流（依赖「以 caller 流为当前流调用」的契约） |
| 4 | CUDA 事件生命周期 | 活到注册表销毁 Work（析构里 destroy） | record 后立刻 destroy（destroy 推迟回收） |
| 5 | 与 caller 计算的重叠 | 有——依赖在使用时才插入 | 无——caller 流已被排到整次交换之后 |
| 6 | 丢弃结果的后果 | 注册表残留条目 + 事件，退出时 torch 打印幸存计数 | 无残留（注册表从未参与） |
| 7 | `out=` 形态 | 返回的 `out` 不经注册表，需自行保证读取时序（文档点名的「洞」） | 同样无注册表可依，且 launch 等待已覆盖 caller 流 |
| 8 | 不变的部分 | —— | 拒绝检查、staging 暂存拷贝、staged 算子、两次 `record_stream`、lend 轮转、主机不阻塞 |

## 6. 本讲小结

- `StreamEventWork` 是裸 `cudaEvent_t` 到 `c10d::Work` 的适配器：`wait()` 只在**当时的当前流**插一条 `cudaStreamWaitEvent` 就返回——主机不参与握手，重叠由此而来；`synchronize()` 才是主机阻塞，留给收尾。
- `register_stream_completion` 在 comm 流当前位置记录一个不计时事件，经 `c10d::register_work` 绑到结果张量的 **storage** 上——条目属于「产出该结果的那次调用」而非「它恰好占用的缓冲」，这与本库窗口复用、lend 轮转的形态严格相容；`record_stream` 管内存复用安全，与注册表的同步语义互补、缺一不可。
- 两个等待入口——显式 `.wait()` 与首个 aten op——最终都汇到 torch 的 `wait_tensor`；view 操作两边都不触发等待。被丢弃的结果留下注册表残留，torch 在退出时打印幸存计数；`out=` 是洞。
- 无 `c10d::register_work` 的构建退化成 `CompletedHandle`：launch 时刻即刻对 caller 流插等待并销毁事件——正确、无重叠、类型可见；staging、record_stream、lend 等机制全部照旧。
- CMake 用 `nm -D --defined-only` 探测 libtorch_cpu 的**导出符号**（mangled 前缀 `_ZN4c10d13register_workERKN2at6Tensor`），而不是读头文件声明——因为声明不能证明库导出；探测失败不致命，一切失败模式都倒向退化路径。结果被 setup.py 快照进 `_build_meta.py`，preflight/doctor 无需 dlopen 即可报告。

## 7. 下一步学习建议

本讲讲完，u7「异步路径」三部曲（comm 流与 AsyncCollectiveTensor → 暂存池 → 工作注册表）已经闭环。建议下一步：

- **u9-l1（测试架构）**：看 `torchrun` 包装器如何用超时 + `killpg` 清理孤儿 rank——理解为什么一条挂在自旋屏障里的 rank 会拖住整组 GPU，以及测试基础设施如何防它。
- **u9-l3（对抗性测试）**：`overlapping_barriers.py` 把本讲的 `wait_tensor` / `trigger_wait` 区别用进了「让测试变盲」清单——那是注册表语义在测试设计里的实战应用。
- **u8-l3（wheel 矩阵与 ABI 匹配）**：本讲的探测决定「同一份源码在哪些 torch 上能拿到重叠」，u8-l3 则从分发角度回答「哪个 wheel 配哪个 torch」——`_diagnose` 对 undefined symbol 的归因正是本讲探测失败在用户侧的呈现。
- 源码延伸阅读：`torch/distributed/_functional_collectives.py` 中 `AsyncCollectiveTensor`、`wait_tensor`、`trigger_wait` 的实现（本库依赖的对岸），对照本讲 4.2 引用的仓库内证据阅读。
