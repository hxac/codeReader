# 通信-计算重叠：EventOverlap 与双流控制

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 DeepEP 中**通信流（comm stream）**与**计算流（compute stream）**为什么要分离，以及它们各自指向哪条 CUDA stream。
- 看懂 `dispatch`/`combine` 的四个流控开关（`previous_event`、`previous_event_before_epilogue`、`async_with_compute_stream`、`allocate_on_comm_stream`）如何编织出“等待 / 重叠 / 同步”的同步图。
- 写出 README 推荐的“dispatch → 独立计算 → `event.current_stream_wait()`”标准重叠模式，并能用计时证明通信与计算确实重叠了。
- 区分 C++ 侧的 `EventHandle`（裸 CUDA event 封装）与 Python 侧的 `EventOverlap`（带 `with` 语法、hook、release_handle 的便利封装），并理解 `record_stream` 在多流内存安全里的角色。

本讲只到“**Python 接口层 ↔ C++ host 层**”的同步机制，不进入 dispatch/combine 的 GPU kernel 内部（那是 U5/U6 的内容）。

## 2. 前置知识

### 2.1 CUDA stream 与并发

一张 GPU 上可以同时存在多条 **stream（流）**。同一条 stream 内的算子按提交顺序串行执行；**不同 stream 之间没有隐式顺序**，因此可以并发执行（前提是硬件还有空闲的 SM / copy engine）。这是 GPU 上做“通信-计算重叠（communication–computation overlap）”的物理基础。

在 PyTorch 里：

- `torch.cuda.current_stream()` 返回“当前流”，默认是 0 号流（也叫 default / compute stream）。绝大多数用户算子都跑在这条流上。
- `torch.cuda.Stream(...)` 可以新建一条流；`torch.cuda.set_stream(s)` 可以把当前流切换到 `s`。
- `s0.wait_event(e)` / `s0.wait_stream(s1)` 用来在两条流之间建立显式依赖。

### 2.2 CUDA event 与跨流同步

**event（事件）**本质是插在某条 stream 上的一个“路标”。它有两个基本动作：

- **record**：在某条 stream 上打点，表示“执行到这里时记一下”。event 本身不阻塞。
- **wait**：让另一条 stream 等到这个 event 被 record 完成后，才继续往后执行。

于是 event 成为**跨流传递依赖**的标准工具：A 流 record 一个 event，B 流 wait 这个 event，就建立了“B 必须等 A”的依赖，而无需让 A、B 真正串行。DeepEP 的所有跨流同步都建立在这个原语上。

### 2.3 为什么要让通信和计算重叠

MoE 的 dispatch/combine 是 all-to-all 通信，本身要占几十到几百微秒。如果这段通信期间 GPU 的计算单元（SM）完全空转，就是纯粹的浪费。DeepEP 的做法是：把通信 kernel 放到一条**独立的 comm stream** 上，让它和用户在 **compute stream** 上的计算算子并行跑。只要计算算子不依赖通信结果，两者就能重叠，总耗时从“通信 + 计算”降到接近“max(通信, 计算)”：

\[
T_{\text{overlap}} \approx \max(T_{\text{comm}},\ T_{\text{compute}}), \qquad
T_{\text{serial}} = T_{\text{comm}} + T_{\text{compute}}
\]

这正是 `prefer_overlap_with_compute` 这个构造开关存在的意义（见上一讲 u2-l2）：开启时 DeepEP 倾向于**少占 SM**，把更多 SM 让给计算流，从而让重叠成为可能。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| `csrc/elastic/utils.hpp` | 定义全局唯一的 comm stream（`get_global_comm_stream`）。 |
| `csrc/utils/event.hpp` | C++ 侧的 `EventHandle`：对 `torch::Event` 的薄封装，提供 `current_stream_wait` 与 `stream_wait` 工具。 |
| `csrc/elastic/buffer.hpp` | `ElasticBuffer` 的 C++ 实现：成员 `comm_stream`、`get_comm_stream`，以及三段式流控制 `stream_control_prologue` / `stream_control_before_epilogue` / `stream_control_epilogue`。 |
| `deep_ep/utils/event.py` | Python 侧的 `EventOverlap`：在 `EventHandle` 之上提供 `current_stream_wait`、`register_hook_after_wait`、`with` 语法。 |
| `deep_ep/buffers/elastic.py` | `ElasticBuffer.dispatch/combine` 的 Python 包装：把四个流控开关下传给 C++，并把返回的 event 包成 `EventOverlap`；`capture()` 与 `get_comm_stream()` 也在这里。 |
| `tests/elastic/test_ep.py` | 用 `enumerate_ep_modes` 枚举四个开关的组合，`launch` 辅助函数演示标准调用姿势。 |

调用方向回顾（承接 u1-l2）：用户调 `buffer.dispatch(...)` → `self.runtime.dispatch`（pybind11）→ C++ `ElasticBuffer::dispatch`（host，做流控制）→ `launch_dispatch`（启动器）→ JIT 编译的 GPU kernel。本讲聚焦第二步的 **host 层流控制**。

## 4. 核心概念与源码讲解

### 4.1 双流模型：compute stream 与 comm stream

#### 4.1.1 概念说明

DeepEP 给每个 `ElasticBuffer` 实例绑定了一条**专属的通信流 `comm_stream`**，所有 dispatch/combine 的 GPU kernel 都在这条流上启动。而用户的算子（GEMM、归一化、激活…）依然跑在 PyTorch 的**当前流（compute stream）**上。

这样做有两个直接收益：

1. **隔离**：通信 kernel 即使占满了它自己的 SM，也不会在调度上“插队”打断 compute stream 上已经排好队的算子。
2. **可重叠**：两条流独立，靠 event 建立最小必要的依赖，其余时间窗口天然可以并行。

#### 4.1.2 核心流程

- comm_stream 是**进程级单例**：第一次被请求时从 PyTorch 的 stream pool 里取一条**高优先级**流，之后整个进程里所有 `ElasticBuffer` 共用同一条（避免每个 buffer 都开一条流，造成调度混乱）。
- 它在 `ElasticBuffer` 构造时被存为成员 `comm_stream`，并在 Python 侧通过 `get_comm_stream()` 暴露出来，供需要时与用户自建流做同步。
- 所有 dispatch/combine 的 kernel launch、所有跨 rank 的对称内存信号写入，都以 `comm_stream` 为参数提交。

#### 4.1.3 源码精读

comm stream 的真正出处是 `csrc/elastic/utils.hpp` 里的全局工厂：它用一个函数内 `static` 变量保证只创建一次，并从 PyTorch 的高优先级流池里取流。

[csrc/elastic/utils.hpp:L8-L13](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/utils.hpp#L8-L13) — 全局唯一的 comm stream：`static` 变量只在首次调用时 `getStreamFromPool(true)`（`true` 表示高优先级）取一条流，之后恒定返回同一条。

构造 `ElasticBuffer` 时，把这个全局流存为成员，整个对象的生命周期里都用它：

[csrc/elastic/buffer.hpp:L37-L38](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L37-L38) — 声明成员 `comm_stream`。

[csrc/elastic/buffer.hpp:L93](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L93) — 在构造初始化列表里 `comm_stream(get_global_comm_stream())` 绑定全局流。

[csrc/elastic/buffer.hpp:L168-L170](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L168-L170) — `get_comm_stream()` 把 `comm_stream` 返回给 Python。

Python 侧把这条 C++ stream 包回成一个 `torch.cuda.Stream` 对象，方便用户拿去做手动同步：

[deep_ep/buffers/elastic.py:L539-L547](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L539-L547) — `get_comm_stream()` 用 C++ 返回的 `stream_id` 重建 `torch.cuda.Stream`，这样用户可以 `torch.cuda.current_stream().wait_stream(buffer.get_comm_stream())` 等。

> 注意：compute stream 并不是某个固定成员，而是**每次调用 dispatch/combine 时现场取**的 `at::cuda::getCurrentCUDAStream()`。也就是说，“compute stream”就是用户当前所在的那条 PyTorch 流。这一点在下一节的三段式控制里会反复出现。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：确认 comm stream 是进程级单例，且与 default compute stream 不是同一条。
2. **步骤**：
   - 在构造好 `buffer` 后，调用 `buffer.get_comm_stream()` 拿到 comm stream 对象，打印它的 `stream_id`。
   - 打印 `torch.cuda.current_stream().stream_id`（默认计算流）。
   - 构造第二个 `ElasticBuffer`，再次打印它的 `get_comm_stream().stream_id`。
3. **观察**：comm stream 的 id 与 default stream 的 id **不同**；两个 buffer 返回的 comm stream id **相同**（单例）。
4. **预期结果**：两组 id 满足上述关系。具体数值与 PyTorch 版本和设备有关，**待本地验证**。

#### 4.1.5 小练习与答案

**Q1**：为什么 comm stream 要从“高优先级流池”里取（`getStreamFromPool(true)`），而不是用一条普通优先级的流？

**答**：通信 kernel 通常是延迟敏感的关键路径，给它高优先级可以让调度器在 SM 资源紧张时优先推进通信，减少 dispatch/combine 的尾延迟，从而让重叠窗口更稳定。

**Q2**：如果用户在同一段代码里手动 `torch.cuda.set_stream(buffer.get_comm_stream())`，把当前流切到 comm stream，再调 `buffer.dispatch(async_with_compute_stream=True)`，会发生什么不妥？

**答**：此时 `getCurrentCUDAStream()` 返回的就是 comm stream，于是“compute stream”和“comm stream”退化成同一条流，重叠彻底失效；同时 `stream_control_epilogue` 里 `stream_wait(compute_stream, comm_stream)` 这种“不同流”的前提也不再成立。DeepEP 假定用户调用 dispatch/combine 时**仍处在自己的 compute stream 上**。

### 4.2 dispatch/combine 的三段式流控制：四个开关

#### 4.2.1 概念说明

`dispatch` 和 `combine` 各有四个与流/事件相关的开关（签名见 [deep_ep/buffers/elastic.py:L863-L867](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L863-L867)）：

| 开关 | 默认 | 作用 |
| --- | --- | --- |
| `async_with_compute_stream` | `False` | 为 `True` 时，**计算流不等通信完成**就返回，并带回一个 event；为 `False` 时，返回前计算流会**阻塞**等通信完成。 |
| `allocate_on_comm_stream` | `False` | 为 `True` 时，dispatch/combine 内部新分配的张量（如 `recv_x`）都登记在 comm stream 名下（并把当前流临时切到 comm stream）。 |
| `previous_event` | `None` | 一个由用户提前 `buffer.capture()` 录制的 event；表示“在这之前的某段计算先跑，通信必须等它”。**传它时必须同时开 `allocate_on_comm_stream`。** |
| `previous_event_before_epilogue` | `None` | 类似 `previous_event`，但只在 **copy/reduce epilogue 之前**生效（epilogue 是 dispatch/combine 主 kernel 之后的那段收尾拷贝）。 |

这四个开关被 C++ 的三个函数消费：**prologue（开场）→ before_epilogue（收尾前）→ epilogue（收尾）**。它们共同决定“谁等谁、张量归谁、要不要回吐 event”。

#### 4.2.2 核心流程

一次 dispatch/combine 的 host 侧流控制可以画成下面这张时序图（`async=True` 的情况）：

```
compute stream:  ──[ 用户计算 A ]──·                              ·──[ 用户计算 B ]──►
                                     \                            /
                                      \ (previous_event 或        \ (current_stream_wait
                                       \  默认 wait compute)       \  才能安全用 recv_x)
                                        \                          \
comm stream:    ──────wait────►[ dispatch/combine 主 kernel ]──►[ epilogue ]──record event──►
```

具体步骤：

1. **prologue**：
   - 记下 `compute_stream = getCurrentCUDAStream()`。
   - 若 `allocate_on_comm_stream`，把当前流**临时切到 comm stream**（这样后续 `torch::empty` 分配的显存归 comm stream）。
   - **硬约束**：若给了 `previous_event`，则必须 `allocate_on_comm_stream`（断言）。
   - 决定 comm stream 的等待对象：给了 `previous_event` 就 `stream_wait(comm, previous_event)`（等用户那段先跑完的计算）；否则 `stream_wait(comm, compute_stream)`（保守地等计算流之前的所有工作，保证 `x`/`topk_idx` 等输入已就绪）。
2. **主 kernel**：在 comm stream 上启动 dispatch/combine 的 GPU kernel。
3. **before_epilogue**：若给了 `previous_event_before_epilogue`，comm stream 在跑 epilogue 前再等一次这个 event。
4. **epilogue**：
   - 若 `async_with_compute_stream`：在 comm stream 上 **record 一个 event**（表示“通信全部完成”），把它作为返回值带回 Python；同时为输出的张量做 `record_stream`（保证显存安全）。**不切换**回 compute stream 之前的阻塞等待——计算流可以继续干别的。
   - 否则（同步）：`stream_wait(compute_stream, comm_stream)`——计算流在返回前**必须**等通信完成。
   - 若 `allocate_on_comm_stream`：把当前流**切回** compute stream。

#### 4.2.3 源码精读

**prologue** —— 处理“分配流切换”和“comm 等谁”：

[csrc/elastic/buffer.hpp:L526-L549](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L526-L549) — `stream_control_prologue`：
- `compute_stream = getCurrentCUDAStream()`（每次现场取计算流）。
- `allocate_on_comm_stream` 为真时 `setCurrentCUDAStream(comm_stream)`。
- 第 539–540 行断言 `previous_event` 隐含 `allocate_on_comm_stream`：因为 `previous_event` 表示“计算先跑、产生输入”，输入张量与 comm stream 上的分配若不统一管理会有跨流内存序问题，所以强制在 comm stream 上分配。
- 第 543–547 行：有 `previous_event` 就等它，否则保守地等整个 compute stream。

**before_epilogue** —— 一个独立的、可选的等待点：

[csrc/elastic/buffer.hpp:L551-L554](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L551-L554) — `stream_control_before_epilogue`：仅当传了 `previous_event_before_epilogue` 才让 comm stream 在 epilogue 前等它，用于“主 kernel 可以先发出去，但 epilogue 必须等某段计算”的精细重叠场景。

**epilogue** —— 决定“返回 event 还是阻塞等待”，并处理 `record_stream`：

[csrc/elastic/buffer.hpp:L556-L584](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L556-L584) — `stream_control_epilogue`：
- `async_with_compute_stream` 为真时（第 562–573 行）：`event = EventHandle(comm_stream)`（在 comm stream 上 record 完成事件，作为返回值）；然后默认对每个输出张量同时 `record_stream(compute_stream)` 和 `record_stream(comm_stream)`，让 PyTorch 的 caching allocator 知道这块显存同时被两条流使用，**不要过早回收**。设了环境变量 `EP_AVOID_RECORD_STREAM` 时改用 `event->tensors_to_record` 自行托管（主要为了兼容 CUDA graph，见 `event.py` 里的注释）。
- 否则（第 574–576 行）：`stream_wait(compute_stream, comm_stream)`，即计算流阻塞等通信完成。
- 第 579–580 行：若之前切到了 comm stream，这里切回 compute stream。

> 关键直觉：**`async_with_compute_stream` 把“等待”的决定权交还给了用户**——C++ 不再在返回前阻塞计算流，而是回吐一个 event，用户可以选择立刻等（等同同步），也可以先插一段独立计算再等（实现重叠）。这就是下一节 `EventOverlap` 存在的全部理由。

Python 侧只是把 C++ 的返回值（`None` 或一个 `EventHandle`）包成 `EventOverlap`：

[deep_ep/buffers/elastic.py:L1016-L1017](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L1016-L1017) — `event_overlap = EventOverlap(event)`（同步模式下 `event` 为 `None`）。

[deep_ep/buffers/elastic.py:L1107](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L1107) — `combine` 同样以 `EventOverlap(event)` 收尾。

#### 4.2.4 代码实践（阅读 + 推理）

1. **目标**：理解 `enumerate_ep_modes` 为什么把 `allocate_on_comm_stream` 和 `with_previous_event` 绑定。
2. **步骤**：阅读 [tests/elastic/test_ep.py:L22-L31](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/tests/elastic/test_ep.py#L22-L31) 的枚举，注意 `allocate_on_comm_stream in ((1,) if with_previous_event else (0, 1))`。
3. **观察**：当 `with_previous_event=1` 时，`allocate_on_comm_stream` 只取 `1`；其余情况下 `0/1` 都测。
4. **预期结果 / 解释**：这正好对应 4.2.3 里 prologue 的断言“`previous_event` 隐含 `allocate_on_comm_stream`”。测试在枚举层就排除了会被 C++ 断言拒绝的组合。
5. 额外阅读 [tests/elastic/test_ep.py:L34-L41](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/tests/elastic/test_ep.py#L34-L41) 的 `launch`：`with_previous_event` 时 `params.update(previous_event=buffer.capture())`，且异步模式下每次调用末尾都 `values[-1].current_stream_wait()`——测试为了逐项比对正确性，主动放弃了重叠收益，把每次调用都串行化。

#### 4.2.5 小练习与答案

**Q1**：README 的训练示例（[README.md:L190-L199](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/README.md#L190-L199)）只设了 `async_with_compute_stream=True`，没设 `allocate_on_comm_stream`。这时 `recv_x` 是分配在哪条流上的？为什么这样也安全？

**答**：`allocate_on_comm_stream` 默认 `False`，所以 prologue 不切换当前流，`recv_x` 分配在 compute stream 上。安全性靠 epilogue 里的 `record_stream`：异步模式下 DeepEP 会对 `recv_x` 同时 `record_stream(compute_stream)` 和 `record_stream(comm_stream)`，caching allocator 因此知道它正被 comm stream 写、被 compute stream 读，不会在任一条流结束前回收这块显存。

**Q2**：什么场景下才需要显式用 `previous_event`（而不是依赖默认的 `stream_wait(comm, compute)`）？

**答**：当你想让“产生 dispatch 输入的计算”与“**前一次** dispatch/combine 的通信”重叠时。默认的 `stream_wait(comm, compute_stream)` 会让 comm 等待 compute stream 上**所有**已提交工作，包括那些其实不影响这次 dispatch 输入的算子，过严。用 `previous_event` 时，你只 record 真正产生输入的那一小段计算，comm 只等这一段，从而把无关算子解耦出去，获得更长的重叠窗口。这就是它要求 `allocate_on_comm_stream=True`、把分配挪到 comm stream 上的原因（避免被 compute stream 上后续无关算子的显存生命周期干扰）。

### 4.3 EventOverlap 与 EventHandle：等待与重叠的同步原语

#### 4.3.1 概念说明

`EventOverlap` 是 DeepEP 给用户的**唯一同步句柄**：`dispatch`/`combine` 的返回值最后一项就是它。它本质上是对 C++ `EventHandle`（一个 `torch::Event`）的 Python 包装，并额外提供了三个便利：

- `current_stream_wait()`：让当前（计算）流等到通信完成。
- `register_hook_after_wait(hook)`：注册一个“等待之后立即执行”的回调，用于确定性排序等场景。
- `with event_overlap: ...` 语法：在退出 `with` 块时自动等待，写起来更紧凑。

C++ 侧的 `EventHandle` 则是更底层、更“裸”的存在：它只管 record + wait。

#### 4.3.2 核心流程

**C++ `EventHandle` 的两副面孔**：

- 默认构造 `EventHandle()`：在**当前流**上 record 一个 event。这正是 Python `buffer.capture()` 用来产生 `previous_event` 的方式。
- `EventHandle(stream)`：在**指定流**上 record。epilogue 里 `EventHandle(comm_stream)` 用它来标记通信完成。
- `current_stream_wait()`：让**当前流**等这个 event。

**Python `EventOverlap` 的标准重叠用法**（README 推荐，[README.md:L255-L267](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/README.md#L255-L267)）：

```python
recv_x, recv_topk_idx, recv_topk_weights, handle, event = dispatch_forward(...)  # async=True
# ... 这里插一段与 recv_x 无关的独立 GPU 计算 ...
event.current_stream_wait()    # 等通信完成
# 之后才能安全使用 recv_x
```

也可用 `with` 写成：

```python
with event:
    do_independent_compute()   # 退出 with 时自动 current_stream_wait
```

#### 4.3.3 源码精读

**C++ `EventHandle`** —— 裸 event 封装：

[csrc/utils/event.hpp:L10-L27](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/utils/event.hpp#L10-L27) — `EventHandle` 持有 `shared_ptr<torch::Event>` 和一个 `tensors_to_record` 列表；默认构造（[L14-L17](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/utils/event.hpp#L14-L17)）在当前流 record；`current_stream_wait()`（[L26](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/utils/event.hpp#L26)）让当前流 wait 这个 event。

[csrc/utils/event.hpp:L35-L42](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/utils/event.hpp#L35-L42) — 两个 `stream_wait` 重载：第一个让 `s_0` 等 `s_1`（**断言两条流不同**，否则等待无意义），通过新建一个 event 在 `s_1` 上 record、`s_0` 上 wait 实现；第二个让 `s` 等 `EventHandle`。prologue/epilogue 里的“comm 等 compute / compute 等 comm”都走这两个工具。

它被 pybind11 暴露成 Python 的 `deep_ep._C.EventHandle`（注意：绑定写在 legacy 模块里，但因为是公共类型，V2 同样可用）：

[csrc/legacy/buffer.hpp:L1763-L1765](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/legacy/buffer.hpp#L1763-L1765) — 暴露默认构造（对应 `capture()`）与 `current_stream_wait`。

**Python `buffer.capture()`** —— 产生 `previous_event` 的入口：

[deep_ep/buffers/elastic.py:L529-L537](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L529-L537) — `capture()` 直接 `return EventHandle()`，于是在**当前 compute 流**上 record 了一个 event。把它作为下一次 dispatch 的 `previous_event` 传入，等价于“comm stream 等到 compute 流执行到这里为止”。

**Python `EventOverlap`** —— 在 `EventHandle` 之上加 hook 与 `with`：

[deep_ep/utils/event.py:L8-L30](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/utils/event.py#L8-L30) — 类定义与构造：持有 `event`、`extra_tensors`（注释说明它是为了模拟 `record_stream`，主要服务于 CUDA graph，且 V2 里已基本不再需要）、以及内部标志 `_release_handle_by_call` 和钩子 `hook_after_wait`。

[deep_ep/utils/event.py:L39-L54](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/utils/event.py#L39-L54) — `current_stream_wait(release_handle=False)`：先让当前流等 event；若有 `hook_after_wait` 就执行并清空（只触发一次）；若 `release_handle=True` 则把 `self.event` 置 `None`，从而释放 event 里登记的张量（V2 的 event 自身可能持有 `tensors_to_record`，置空可提前释放）。

[deep_ep/utils/event.py:L56-L61](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/utils/event.py#L56-L61) — `register_hook_after_wait`：断言同一时刻只能挂一个 hook。它的主要用户是确定性排序。

[deep_ep/utils/event.py:L63-L96](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/utils/event.py#L63-L96) — `__call__`/`__enter__`/`__exit__`：`event_overlap(release_handle=True)` 配置“退出 with 时是否释放 handle”，`__enter__` 直接返回 self，`__exit__` 在 event 非空时调用 `current_stream_wait`。注意 `__call__` 返回 `self` 而非新对象，避免改变底层 event 的引用计数。

**hook 与确定性排序的接线** —— 把 `deterministic_sort` 挂到“等待之后”：

[deep_ep/buffers/elastic.py:L1021-L1027](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L1021-L1027) — 当 `self.deterministic` 时，把 `handle.deterministic_sort` 包成 `epilogue`：异步模式下 `register_hook_after_wait(epilogue)`（等通信完成、在计算流上排序）；同步模式下直接 `epilogue()` 立即执行。这就是 `EventOverlap.register_hook_after_wait` 的真实用武之地——确定性排序必须在“通信已就绪、且仍在当前计算流”这个时机执行。（确定性排序本身详见 u6-l3。）

#### 4.3.4 代码实践（完整代码实践：验证通信-计算重叠）

这是本讲的主实践。目标是：用 `async_with_compute_stream=True` 调 dispatch，在 `event.current_stream_wait()` **之前**插入一段独立的矩阵乘，再等待；用计时对比“重叠版”和“串行版”，证明通信与计算确实重叠。

下面的脚本以 `tests/elastic/test_ep.py` 的分布式拉起方式为模板（用 `deep_ep.utils.envs.init_dist` 复现 u1-l4 学过的多进程启动），保持与真实测试一致的 API 用法。

```python
# overlap_demo.py
# 示例代码：验证 DeepEP 通信与计算的重叠
# 运行（单机多卡，至少 2 张 Hopper GPU）：
#   python overlap_demo.py --num-tokens 4096 --hidden 7168 --num-experts 256 --num-topk 6
import argparse
import time
import torch
import torch.distributed as dist
import deep_ep
from deep_ep.utils.envs import init_dist   # 复用测试里的分布式工具（见 u1-l4）

def run(local_rank: int, num_local_ranks: int, args):
    rank_idx, num_ranks, group = init_dist(local_rank, num_local_ranks, seed=0)

    num_experts, num_topk, hidden, num_tokens = args.num_experts, args.num_topk, args.hidden, args.num_tokens
    buffer = deep_ep.ElasticBuffer(
        group, num_max_tokens_per_rank=num_tokens, hidden=hidden, num_topk=num_topk,
        allow_hybrid_mode=True)

    # 构造 dispatch 输入
    x = torch.randn(num_tokens, hidden, dtype=torch.bfloat16, device='cuda')
    scores = torch.randn(num_tokens, num_experts, device='cuda')
    _, topk_idx = torch.topk(scores, num_topk, dim=-1, sorted=False)
    topk_idx = topk_idx.to(deep_ep.topk_idx_t)
    topk_weights = torch.rand(num_tokens, num_topk, dtype=torch.float, device='cuda')

    # 一段“与 dispatch 结果完全无关”的独立计算（compute stream 上跑）
    a = torch.randn(args.matmul_n, args.matmul_n, dtype=torch.bfloat16, device='cuda')
    b = torch.randn(args.matmul_n, args.matmul_n, dtype=torch.bfloat16, device='cuda')

    def overlapped():
        # async=True：计算流不会在返回前等通信完成
        _, _, _, _, event = buffer.dispatch(
            x, topk_idx=topk_idx, topk_weights=topk_weights,
            num_experts=num_experts, num_max_tokens_per_rank=num_tokens,
            async_with_compute_stream=True)
        c = a @ b                       # 独立计算，与通信并行
        event.current_stream_wait()     # 用到 recv_x 前才等通信
        return c

    def serial():
        _, _, _, _, event = buffer.dispatch(
            x, topk_idx=topk_idx, topk_weights=topk_weights,
            num_experts=num_experts, num_max_tokens_per_rank=num_tokens,
            async_with_compute_stream=True)
        event.current_stream_wait()     # 先等通信完成
        c = a @ b                       # 再做计算：串行
        return c

    # 预热（首次 dispatch 会触发 JIT 编译，必须排除在计时外）
    for _ in range(3):
        overlapped(); serial()
    torch.cuda.synchronize()

    N = 20
    torch.cuda.synchronize(); t0 = time.perf_counter()
    for _ in range(N): overlapped()
    torch.cuda.synchronize(); t_overlap = (time.perf_counter() - t0) / N

    torch.cuda.synchronize(); t0 = time.perf_counter()
    for _ in range(N): serial()
    torch.cuda.synchronize(); t_serial = (time.perf_counter() - t0) / N

    if rank_idx == 0:
        print(f'[overlap] overlapped={t_overlap*1e3:.3f} ms, serial={t_serial*1e3:.3f} ms, '
              f'saved={ (t_serial - t_overlap)*1e3:.3f} ms')

    buffer.runtime.destroy()
    dist.barrier(); dist.destroy_process_group()

if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--num-tokens', type=int, default=4096)
    p.add_argument('--hidden', type=int, default=7168)
    p.add_argument('--num-experts', type=int, default=256)
    p.add_argument('--num-topk', type=int, default=6)
    p.add_argument('--matmul-n', type=int, default=4096)   # 独立矩阵乘的规模
    args = p.parse_args()
    torch.multiprocessing.spawn(run, args=(torch.cuda.device_count(), args),
                                nprocs=torch.cuda.device_count())
```

1. **实践目标**：亲眼看到 `t_overlap < t_serial`，即重叠版比串行版快。
2. **操作步骤**：保存为 `overlap_demo.py`，在单机多卡（≥2 张 Hopper）上 `python overlap_demo.py` 运行。注意第 4.1.2 节强调的“首次调用会 JIT 编译”，所以预热循环必不可少。
3. **需要观察的现象**：
   - `overlapped` 的耗时**小于** `serial`。
   - 调整 `--matmul-n` 让矩阵乘的耗时与单次 dispatch 的通信耗时**接近**时，重叠收益最明显（接近“max(comm, compute)”）；矩阵乘过大或过小都会让收益变小。
4. **预期结果**：`saved` 为正。具体毫秒数与机器、GPU 数、`num_tokens`、`hidden` 强相关，**待本地验证**。若 `saved ≤ 0`，最常见原因是预热不充分（JIT 编译被计入计时）或矩阵乘规模与通信耗时差距过大。
5. **进阶验证（可选）**：把 `a @ b` 换成**依赖 `recv_x`** 的计算（例如 `recv_x @ a`），放到 `current_stream_wait()` **之前**——此时计算必须等通信结果，等价于串行，`saved` 应回落到接近 0。这反向证明：只有“独立计算”才能真正重叠。

#### 4.3.5 小练习与答案

**Q1**：`event.current_stream_wait()` 是在 CPU 上阻塞，还是在 GPU 上建立依赖？调用它之后 CPU 会立刻返回吗？

**答**：它是在 **GPU 上**建立依赖——让当前（计算）流 wait 那个 event，CPU 端只是提交了一条 wait 指令，**立刻返回**，不会阻塞 CPU。这正是异步流水线能工作的前提：CPU 不断往两条流提交工作，GPU 自己按 event 依赖关系调度。

**Q2**：`with event_overlap:` 在并发安全上依赖了什么隐含约定？如果在 `with` 块里访问 `recv_x` 会怎样？

**答**：`with` 语法假定你在块内做的是**不依赖通信结果**的独立计算——因为 `current_stream_wait` 只在 `__exit__` 时才提交。如果在块内就访问 `recv_x`，由于计算流尚未等待通信完成，GPU 调度上没有保证 `recv_x` 已被写完，会读到未定义数据。所以“在 `with` 块内用通信结果”是错误用法；要用结果，必须先退出 `with`（或先显式 `current_stream_wait()`）。

## 5. 综合实践

把 4.1～4.3 串起来，完成下面这个“最小重叠 MoE 前向”任务（仍是示例代码，结果待本地验证）：

```python
# minimal_overlap_moe.py（示例代码）
# 目标：dispatch(async) -> 一段与结果无关的 GEMM -> wait -> 模拟 expert GEMM -> combine(async) -> wait
import torch, deep_ep
from deep_ep.utils.envs import init_dist

def run(local_rank, num_local_ranks, args):
    rank, num_ranks, group = init_dist(local_rank, num_local_ranks, seed=0)
    buf = deep_ep.ElasticBuffer(group, num_max_tokens_per_rank=args.num_tokens,
                                hidden=args.hidden, num_topk=args.num_topk)
    num_local_experts = args.num_experts // num_ranks

    x = torch.randn(args.num_tokens, args.hidden, dtype=torch.bfloat16, device='cuda')
    scores = torch.randn(args.num_tokens, args.num_experts, device='cuda')
    _, topk_idx = torch.topk(scores, args.num_topk, dim=-1, sorted=False)
    topk_idx = topk_idx.to(deep_ep.topk_idx_t)
    topk_weights = torch.rand(args.num_tokens, args.num_topk, device='cuda')

    # 一段独立计算（模拟 dispatch 期间可并行的非 MoE 算子）
    indep_a = torch.randn(2048, 2048, dtype=torch.bfloat16, device='cuda')
    indep_b = torch.randn(2048, 2048, dtype=torch.bfloat16, device='cuda')

    # 1) dispatch，异步
    recv_x, _, _, handle, event = buf.dispatch(
        x, topk_idx=topk_idx, topk_weights=topk_weights,
        num_experts=args.num_experts, num_max_tokens_per_rank=args.num_tokens,
        async_with_compute_stream=True)
    # 2) 在通信进行的同时，跑独立 GEMM（重叠窗口）
    _ = indep_a @ indep_b
    # 3) 等通信完成，之后才能用 recv_x 做 expert GEMM
    event.current_stream_wait()
    expert_out = recv_x[: handle.num_recv_tokens] @ torch.randn(
        args.hidden, args.hidden, dtype=torch.bfloat16, device='cuda')
    # 4) combine，异步；返回的 event 用 current_stream_wait 兜底
    combined_x, _, combine_event = buf.combine(
        expert_out, handle=handle, topk_weights=handle.topk_idx.new_zeros(
            (expert_out.shape[0],)), async_with_compute_stream=True)
    combine_event.current_stream_wait()

    if rank == 0:
        print('overlap MoE forward done, combined_x:', tuple(combined_x.shape))
    buf.runtime.destroy()

if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument('--num-tokens', type=int, default=2048)
    p.add_argument('--hidden', type=int, default=7168)
    p.add_argument('--num-experts', type=int, default=256)
    p.add_argument('--num-topk', type=int, default=6)
    a = p.parse_args()
    torch.multiprocessing.spawn(run, args=(torch.cuda.device_count(), a),
                                nprocs=torch.cuda.device_count())
```

要求：

1. 解释清楚为什么第 2 步的 GEMM 必须放在 `event.current_stream_wait()` **之前**才算“重叠”，而 expert GEMM（第 3 步）必须放在**之后**。
2. 用 4.3.4 的计时方法，对比“保留第 2 步 GEMM”与“把第 2 步 GEMM 挪到 `current_stream_wait()` 之后”两种版本的总耗时，验证前者更短。
3. 把第 1 步改成 `async_with_compute_stream=False`（同步），再观察总时长的变化，解释为什么同步模式下根本谈不上“重叠”。

预期：异步 + 第 2 步前置时总时长最短；同步模式下 dispatch 会阻塞计算流，第 2 步 GEMM 只能在通信完成后才开始，总时长 ≈ 通信 + 计算。具体数字**待本地验证**。

## 6. 本讲小结

- DeepEP 给每个 `ElasticBuffer` 绑定一条**进程级单例的高优先级 comm stream**（`get_global_comm_stream`），用户的算子仍在 **compute stream**（即 `getCurrentCUDAStream()`）上，两者靠 event 建立最小依赖，其余窗口可并行——这是通信-计算重叠的物理基础。
- `dispatch`/`combine` 的 host 侧流控制是**三段式**：`stream_control_prologue`（切分配流 + 决定 comm 等谁）→ 主 kernel → `stream_control_epilogue`（异步则 record event 并 `record_stream`，同步则让 compute 阻塞等 comm）；中间还可插一个 `stream_control_before_epilogue`。
- 四个开关里，`async_with_compute_stream` 决定“回吐 event 还是阻塞等待”，是重叠的总开关；`previous_event` 表示“计算先跑、通信等它”，且**强制要求** `allocate_on_comm_stream`（对应一个 host 断言和测试枚举里的绑定）；`allocate_on_comm_stream` 把张量分配/归属切到 comm stream。
- `EventHandle`（C++）是裸 `torch::Event` 封装，`buffer.capture()` = `EventHandle()` 在当前计算流上 record，用于产生 `previous_event`；epilogue 里 `EventHandle(comm_stream)` 标记通信完成。
- `EventOverlap`（Python）是返回给用户的同步句柄：`current_stream_wait()` 建立依赖、`register_hook_after_wait` 支撑确定性排序、`with` 语法让重叠写法更紧凑；`record_stream` 负责多流显存安全。
- 标准重叠模式（README）：`dispatch(async=True)` → 独立计算 → `event.current_stream_wait()` → 才用 `recv_x`。

## 7. 下一步学习建议

- **进入 dispatch 内核**：本讲只到 host 层的“何时启动、何时等待”。真正的 dispatch GPU kernel 怎么把 token 经 NVLink/RDMA 写到对端 buffer，见 **u5-l1（直接模式 Dispatch）** 与 **u5-l2（Hybrid Dispatch）**。
- **combine 的反向路由**：理解 `combine` 如何复用 dispatch 返回的 `EPHandle` 元数据，见 **u6-l1（Combine 主流程）**。
- **确定性排序**：本讲多次提到 `register_hook_after_wait` 服务于确定性排序，其完整实现（多 channel/多 rank 到达顺序导致的非确定性、expand/非 expand 排序键构造）见 **u6-l3（确定性路由）**。
- **CUDA graph 兼容性**：`stream_control_epilogue` 里 `EP_AVOID_RECORD_STREAM` 与 `event->tensors_to_record`、`EventOverlap.extra_tensors` 都是为 CUDA graph 留的口子，建议在学完 U5/U6 后回头结合 CUDA graph 文档再读一遍这两处。
