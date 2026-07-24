# Relay 数据平面与传输后端

## 1. 本讲目标

学完本讲，你应该能够：

- 说清楚 SGLang-Omni 的「数据平面（relay）」是什么，它和 [u3-l2] 讲的控制平面为什么必须分工：控制平面传「小命令」，relay 传「大张量」。
- 读懂 `relay/base.py` 里 `Relay` 与 `RelayOperation` 两个抽象，并讲清 `RelayOperation.metadata` 这个「契约」：发送方返回一段元数据，接收方靠它取回数据。
- 讲清 CUDA IPC 后端（`relay/cuda_ipc.py`）的**发送侧有界 GPU 槽池**：池子多大、一个张量占几个 slot、怎么分配、怎么回收。
- 完整复述一次跨阶段张量搬运的**资源生命周期**：发送方写池→发 `DataReadyMessage`→等 ACK；接收方读→发 `DataAckMessage`；发送方收到一次 ACK 就释放**整段** slot 区间。
- 解释「slot 粒度」与「一次 `DataAckMessage` 释放整段区间」为什么是兼容的、且不矛盾。

## 2. 前置知识

本讲承接 [u3-l2 控制平面与 ZMQ 消息]。你已经知道：

- 控制平面用 ZMQ + msgpack 传**小而快**的命令与状态。
- `DataReadyMessage.data_ref` 有「双重身份」：它要么是直接 CUDA IPC 信封，要么是一个指向 relay 后端的 `DataRef` 字典。本讲的主角就是后者背后真正搬运数据的「数据平面」。
- 控制消息里只放「指针/信封」，不放张量本体。

读源码前，先建立三个直觉：

1. **GPU 之间传张量，本质是「让对端进程能看见我这块显存」。** CUDA IPC（Inter-Process Communication）就是 PyTorch 提供的、把一块 GPU 显存「跨进程共享」的机制：发送方导出一个 handle（句柄），接收方凭这个 handle 在自己的进程里重建出指向**同一块物理显存**的张量，再发起一次 peer-to-peer 拷贝（同卡是内存拷贝，跨卡走 NVLink/P2P）。relay 的 CUDA IPC 后端就是在这个原语之上做的工程封装。
2. **显存是稀缺资源，必须有界、可回收。** 如果每来一个张量就 `torch.empty` 一块新显存再导出，显存很快被撑爆、且碎片化。SGLang-Omni 的做法是预分配一个**有界池子（bounded pool）**，把池子切成等大的「槽（slot）」，每个张量占一段连续的槽，用完归还。这和操作系统的分页/内存池思想一致。
3. **「发送方什么时候可以安全复用这块显存」是个关键问题。** 发送方把数据拷进池子后，不能立刻复用这段槽——因为接收方可能还没把数据拷走，提前复用会导致数据损坏。所以发送方必须**等接收方确认「我读完了」**才能释放槽。这个「确认」就是控制平面里的 `DataAckMessage`。

> 名词速查：**relay** = 数据平面抽象；**backend** = relay 的某一种实现（cuda_ipc / shm / nixl / mooncake / nccl）；**slot** = 池子里的等大存储单元；**object_id** = 一次搬运的逻辑编号，一个 ACK 对应一个 object_id。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [sglang_omni/relay/base.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/relay/base.py) | 定义 `Relay`、`RelayOperation` 两个抽象基类，以及 `register_relay`/`create_relay` 工厂和 `CreditAllocator`。是所有后端共享的接口契约。 |
| [sglang_omni/relay/cuda_ipc.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/relay/cuda_ipc.py) | CUDA IPC 后端：发送侧有界 GPU 槽池、`put_async`/`get_async`、事件同步、IPC handle 导出与导入。本讲重点精读。 |
| [sglang_omni/relay/shm.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/relay/shm.py) | 主机共享内存后端，搬运 CPU 张量。结构比 cuda_ipc 简单，适合对照理解 relay 接口。 |
| [sglang_omni/proto/messages.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/proto/messages.py) | `DataAckMessage` 定义——接收方读完数据后回传的「确认」，携带 `object_id`。 |
| [sglang_omni/comm/stage_io.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/comm/stage_io.py) | 在 relay 之上再封装一层：`write_payload` 调 `put_async`、`read_payload` 调 `get_async` + `cleanup`，把 relay 的 `metadata` 装进 `DataRef.buffer.info`。 |
| [sglang_omni/comm/engine.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/comm/engine.py) | `CommEngine`：发送侧维护 `_pending` 表，收 `DataAckMessage` 后调 `op.mark_receiver_done()` 再 `wait_for_completion()`，从而触发 slot 释放。 |
| [sglang_omni/pipeline/stage/runtime.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/stage/runtime.py) | 接收侧 Stage：`read_payload` 成功后发 `success=True` 的 `DataAckMessage`，失败发 `success=False`。 |

> 提醒：传输后端（cuda_ipc / shm / nixl / mooncake）的**选择**由 `comm/router.py` 按阶段局部性派生，[u6-l1] 会专门讲。本讲只聚焦「选定了 cuda_ipc 之后，数据平面内部怎么搬、怎么管资源」。

## 4. 核心概念与源码讲解

### 4.1 Relay 抽象接口与 Operation 契约

#### 4.1.1 概念说明

数据平面面对一个矛盾：传输介质多种多样（同卡显存、跨卡 NVLink、跨节点 RDMA、主机共享内存……），每种介质的 API 完全不同；但上层 Stage 只关心一件事——「把这个张量交给你，你等会儿告诉接收方怎么取」。SGLang-Omni 的解法是抽出一个统一接口 `Relay`，把「用什么介质搬」的差异藏在各后端实现里，对上层只暴露四个方法：

- `put_async(tensor, ...)`：发送一个张量，返回一个操作句柄。
- `get_async(metadata, dest_tensor, ...)`：接收方凭发送方给的 `metadata` 把数据读进 `dest_tensor`。
- `cleanup(request_id)`：按请求清理残留资源（abort 时用）。
- `close()`：关闭整个 relay，释放全局资源。

每个异步操作统一抽象成 `RelayOperation`，它的核心是：

- `metadata`：**发送方写给接收方的「取货单」**——接收方靠它才能找到数据（比如 cuda_ipc 的 handle、shm 的块名）。
- `wait_for_completion()`：等这次传输真正完成（对发送方 = 接收方已读走，对接收方 = 拷贝已结束）。
- `mark_receiver_done()` / `mark_receiver_failed()`：**接收方回传确认**的入口——发送方靠它知道「这块显存可以安全复用了」。

#### 4.1.2 核心流程

一次完整的 relay 搬运（发送方 S、接收方 R、控制平面 C）：

```
S: op = await relay.put_async(tensor)          # 写入后端介质，拿到 op + op.metadata
S: 把 op.metadata 装进 DataRef，经 C 发 DataReadyMessage 给 R
S: await op.wait_for_completion()              # 阻塞，等 R 的 ACK
                                               （此刻 S 持有那段资源，不能复用）

R: 收到 DataReadyMessage，取出 data_ref.buffer.info（= op.metadata）
R: op2 = await relay.get_async(metadata, dest) # 读数据进 dest_tensor
R: await op2.wait_for_completion()             # 等拷贝结束
R: 经 C 给 S 回 DataAckMessage(object_id=..., success=True)

S: CommEngine 收到 ack -> op.mark_receiver_done() -> wait_for_completion() 解除阻塞
S: 触发 release_cb -> 归还那段 slot（或 SHM 块）
```

注意中间的**两次等待**语义不同：

- 接收方的 `wait_for_completion` 等的是「拷贝物理完成」（GPU 事件）。
- 发送方的 `wait_for_completion` 等的是「接收方读完了」（由 `DataAckMessage` 驱动的 `mark_receiver_done`）。发送方**不能**只靠自己的 GPU 事件，因为「我拷进池子」完成 ≠ 「对端已拷走」。

#### 4.1.3 源码精读

`Relay` 抽象基类定义了四个方法，全部 `@abstractmethod`：

[sglang_omni/relay/base.py:L103-L136](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/relay/base.py#L103-L136) —— `Relay` 接口。注意 `put_async` 的注释点明 `receiver_id`「names the destination import owner when a backend resource carries one-consumer ownership」（当后端资源是「单消费者」时，用来指名归属）。cuda_ipc 正是单消费者模型，因此 `receiver_id` 必填。

`RelayOperation` 抽象基类，`metadata` 是抽象 property：

[sglang_omni/relay/base.py:L80-L100](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/relay/base.py#L80-L100) —— `RelayOperation` 接口。`mark_receiver_done`/`mark_receiver_failed` 默认抛 `NotImplementedError`，只有**需要 ACK 来管理资源生命周期**的后端（cuda_ipc、shm）才覆写它们；nixl/mooncake 用自己的通知机制，不需要这套。

工厂与注册表让后端可插拔：

[sglang_omni/relay/base.py:L35-L77](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/relay/base.py#L35-L77) —— `create_relay`。它先按名字去 `RELAY_REGISTRY` 查；查不到就**动态 import 对应子模块**（`from .cuda_ipc import CudaIpcRelay`），触发该模块用 `@register_relay("cuda_ipc")` 装饰器完成注册；再用 `inspect.signature` **过滤 kwargs**，只把目标类 `__init__` 认识的参数传进去。这样不同后端的构造参数互不干扰。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：验证「relay 后端是按需懒加载的」，并看清 `metadata` 契约的两端。
2. **操作步骤**：
   - 打开 [sglang_omni/relay/cuda_ipc.py:L491-L492](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/relay/cuda_ipc.py#L491-L492)，确认 `@register_relay("cuda_ipc")` 装饰器把名字 `cuda_ipc` 注册进 `RELAY_REGISTRY`。
   - 打开 [sglang_omni/comm/stage_io.py:L306-L323](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/comm/stage_io.py#L306-L323)，看 `write_payload` 如何调 `relay.put_async(...)` 并把返回的 `op.metadata` 装进 `DataRef.buffer.info`（这就是「取货单」打包点）。
   - 打开 [sglang_omni/comm/stage_io.py:L327-L352](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/comm/stage_io.py#L327-L352)，看 `read_payload` 如何用 `data_ref.buffer.info` 作为 `metadata` 调 `relay.get_async`。
3. **需要观察的现象**：`put_async` 的输出（`op.metadata`）正是 `get_async` 的输入（`metadata`），两端只靠这一个字典耦合，没有任何共享变量。
4. **预期结果**：你能用一句话说出「metadata 是发送方写给接收方的取货单，relay 接口不规定它的内容，由各后端自定义」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `RelayOperation.mark_receiver_done` 的默认实现是抛 `NotImplementedError`，而不是空操作？

> **答案**：因为不是所有后端都需要「接收方确认」来管理发送方资源。cuda_ipc 和 shm 把数据放在有界池/命名块里，必须等接收方读完才能回收，所以要覆写这套；而 nixl/mooncake 靠 RDMA 自带的通知机制管理生命周期，用不到它。默认抛异常能在「后端声称支持 ACK 但忘了实现」时尽早暴露问题，比静默忽略安全。

**练习 2**：`create_relay` 为什么要用 `inspect.signature` 过滤 kwargs？

> **答案**：不同后端构造参数差异很大（cuda_ipc 有 `slot_size_kb`/`pool_size_mb`，shm 有 `slot_size_mb`/`credits`）。上层 `CommRouter` 用一套通用 kwargs 去构造任意后端，过滤能保证「只传目标类认识的那几个」，避免 `unexpected keyword argument`。注意 cuda_ipc 自身在 [L503-L506](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/relay/cuda_ipc.py#L503-L506) 显式拒绝多余 kwargs，所以漏过滤会被它兜住。

---

### 4.2 CUDA IPC 发送侧有界 GPU 槽池

#### 4.2.1 概念说明

CUDA IPC 后端（`CudaIpcRelay`）是同节点 GPU 之间传张量的主力路径。它的核心设计是**发送侧有界池**：

- 进程启动时**不**立即分配显存（懒分配，首次 `put_async` 才建池，见 `_ensure_local_pool`）。
- 建池时一次性 `torch.empty` 一大块连续显存，按固定 `slot_size`（默认 64KB）切成 `slot_count` 个等大 slot。
- 每次要发的张量，按字节大小向上取整占用 `num_slots` 个**连续** slot。
- slot 是可复用资源：接收方读完、ACK 回来后，这段 slot 被归还，下一个张量可复用。

这个设计把「无界的张量搬运」约束在「有界的显存预算」内，避免显存随请求数线性增长。

#### 4.2.2 核心流程

槽位数量计算（向上取整）：

\[
\text{num\_slots} = \max\!\left(1,\; \left\lceil \frac{\text{size}}{\text{slot\_size}} \right\rceil\right)
\]

池子大小与槽数：

\[
\text{slot\_count} = \left\lfloor \frac{\text{pool\_size\_mb} \times 2^{20}}{\text{slot\_size}} \right\rfloor, \qquad \text{pool\_size} = \text{slot\_count} \times \text{slot\_size}
\]

分配与回收的状态机：

```
acquire(num_slots):
  在 _free 布尔数组里找一段长度 >= num_slots 的连续 True 段  # _find_contiguous
  找到 -> 把这段置 False，返回 offset = slot_index * slot_size
  没找到 -> await self._changed.wait()  # 让出，等别人 release 唤醒
  循环

release(offset, num_slots):
  校验 [offset, offset+num_slots) 全是 False（防重复释放）
  把这段置 True，_changed.set()  # 唤醒所有等待者
```

关键不变量：**分配以「连续 slot 段」为单位，释放也以同一段为单位**。一段连续 slot 对应一次 `put_async`，对应一个 `object_id`，对应一次 ACK。

#### 4.2.3 源码精读

构造函数里算出池子规格：

[sglang_omni/relay/cuda_ipc.py:L493-L542](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/relay/cuda_ipc.py#L493-L542) —— `CudaIpcRelay.__init__`。要点：
- `self.slot_size = int(slot_size_kb) * 1024`（默认 64KB）。
- `self.slot_count = requested_pool_size // self.slot_size`，`self.pool_size = self.slot_count * self.slot_size`（向下对齐到 slot 整数倍）。
- `self.credits = self.slot_count`：信用数等于总 slot 数。
- 兼容老参数：若没传 `pool_size_mb`，则用旧的 `slot_size_mb * credits`（默认 512MB × 2 = 1GB）推出来。

懒分配池子：

[sglang_omni/relay/cuda_ipc.py:L550-L582](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/relay/cuda_ipc.py#L550-L582) —— `_ensure_local_pool`。首次调用时 `torch.empty(total_pool_bytes, dtype=torch.uint8, device=device)` 一次分配整池，生成唯一 `pool_id`，并建 `_ContiguousSlotAllocator`。日志会打印池子大小、slot 数。

槽数计算与连续分配器：

[sglang_omni/relay/cuda_ipc.py:L141-L144](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/relay/cuda_ipc.py#L141-L144) —— `_slots_for_size`，即上面的向上取整公式。

[sglang_omni/relay/cuda_ipc.py:L386-L428](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/relay/cuda_ipc.py#L386-L428) —— `_ContiguousSlotAllocator.acquire_async`。持 `asyncio.Lock` 调 `_find_contiguous` 找连续段；找不到就 `_changed.clear()` 后 `await self._changed.wait()` 让出，被 `release` 的 `_changed.set()` 唤醒后重试。

[sglang_omni/relay/cuda_ipc.py:L430-L444](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/relay/cuda_ipc.py#L430-L444) —— `release`。先断言目标段「当前全是已占用」（`if self._free[index]: raise "cuda_ipc slot released twice"`，防重复释放），再把整段置回 `True`，`_changed.set()` 唤醒等待者。**这就是「释放整段区间」的实现**：一次调用归还 `[slot_index, slot_index+num_slots)` 全部 slot。

`_find_contiguous` 在布尔数组里线性扫描找第一个足够长的连续 `True` 段：

[sglang_omni/relay/cuda_ipc.py:L446-L458](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/relay/cuda_ipc.py#L446-L458) —— `_find_contiguous`。

#### 4.2.4 代码实践（源码阅读 + 推演型）

1. **实践目标**：用具体数字感受「slot 粒度」，并验证连续分配/整段释放的正确性。
2. **操作步骤**：
   - 假设 `CudaIpcRelay(engine_id="t", device="cuda:0", slot_size_kb=64, pool_size_mb=1)`。计算：`slot_size = 64*1024 = 65536` 字节；`slot_count = 1*1024*1024 // 65536 = 16` 个 slot。
   - 现在要发一个 `size = 150_000` 字节的张量：`num_slots = ceil(150000/65536) = 3`。推演 `_find_contiguous(3)` 会返回 `slot_index=0`，占用 slot 0/1/2，`offset=0`。
   - 再发第二个同样大小的张量：返回 `slot_index=3`，占用 3/4/5。
   - 假设第一个张量的 ACK 先回来，`release(offset=0, num_slots=3)`：把 0/1/2 置回 True。此时 `_free = [T,T,T, F,F,F, T,T,T,T,T,T,T,T,T,T]`。
3. **需要观察的现象**：释放时 `release` 会逐个检查 0/1/2 当前都是 `False`（已占用），再把它们一起置 `True`；若误传 `num_slots=2`，会留下 slot 2 仍为 `False` 成为「泄漏的 slot」，且后续 release 该 slot 会触发 `"released twice"` 报错。
4. **预期结果**：你能说清「一次 put_async 占用连续 N 个 slot，一次 release 必须归还同样这 N 个」，两者**同一个 num_slots、同一段 offset**。
5. 待本地验证：在有 GPU 的环境跑 `pytest tests/unit_test/relay/test_cuda_ipc_relay.py`（该测试需启动两个真实进程，见测试文件头部注释）。

#### 4.2.5 小练习与答案

**练习 1**：如果某个张量大小恰好是 `slot_size` 的整数倍（比如 `size = 2 * slot_size`），它会占几个 slot？如果张量只有 1 字节呢？

> **答案**：整数倍时占 `size/slot_size` 个（这里是 2 个）；1 字节时，`_slots_for_size` 的 `max(1, ...)` 保证至少占 1 个 slot——一个 slot 是最小分配单位，不能拆零。

**练习 2**：为什么分配器用「连续 slot 段」而不是「任意散落的 slot 集合」？

> **答案**：因为最终要把池子里的数据通过 `pool_slice = pool_tensor[offset:offset+size]` 一次性 `copy_` 进去，接收方也按 `[offset:offset+size]` 一次性读出。连续区间让拷贝是一次连续的显存访问；散落 slot 会要求多次拷贝或额外索引，既慢又复杂。所以分配器坚持「连续」约束，宁可等待也不拿散落 slot。

---

### 4.3 数据搬运的 put/get 双向路径

#### 4.3.1 概念说明

有了池子，`put_async` 和 `get_async` 就是「写池子 + 导出 IPC 信封」与「导入信封 + 从池子读」这两件事。这里有两个 GPU 编程的关键技巧：

- **CUDA Event 做完成同步。** 显存拷贝是异步的（`non_blocking=True`），发出 `copy_` 调用后 CPU 立刻返回，但 GPU 还在搬。用一个 `torch.cuda.Event` 记录在拷贝所在的 stream 上，等 event 触发就代表拷贝真正完成。`interprocess=True` 的 event 还能跨进程传递。
- **同卡用 stream.wait_event 跨进程等。** 发送方把 `ready_event` 记在「写池子」的 stream 上并导出 IPC handle；接收方导入这个 handle 后，在自己读数据的 stream 上 `stream.wait_event(ready_event)`——这保证接收方的读不会跑在发送方的写之前，避免读到半成品数据。

#### 4.3.2 核心流程

发送方 `put_async`：

```
flat = tensor.contiguous().view(uint8)            # 拍平成字节流
num_slots = _slots_for_size(flat.numel(), slot_size)
allocation = await _acquire_slots(allocator, num_slots)   # 拿到 offset
pool_slice = pool_tensor[offset : offset + size]
ready_event = cuda.Event(interprocess=True)
在独立 stream 上: pool_slice.copy_(flat, non_blocking=True); ready_event.record(stream)
ready_handle = ready_event.ipc_handle()           # 导出可跨进程的 event handle
pool_storage_handle = _dump_cuda_storage_handle(pool_tensor)  # 导出池子显存的 IPC 信封
metadata = {transfer_info:{size,offset,slot_index,slot_size,num_slots,...},
            cuda_ipc:{pool_id, pool_storage, src_device_id, ready_event}}
return CudaIpcPutOperation(metadata, release_cb=lambda: allocator.release(offset, num_slots), ...)
```

接收方 `get_async`：

```
pool_tensor = self._get_remote_pool(metadata)     # 首次按 pool_id 导入对端池子并缓存
ready_event = cuda.Event.from_ipc_handle(dst_device, ipc_meta["ready_event"])
src = pool_tensor[offset : offset + size]
dst = dest_tensor.view(uint8)
stream.wait_event(ready_event)                     # 等发送方写完
dst[:size].copy_(src[:size], non_blocking=True)    # peer-to-peer 拷贝
event = cuda.Event(); event.record(stream)
return CudaIpcGetOperation(event, ...)             # 等 event 即等拷贝完成
```

#### 4.3.3 源码精读

发送侧 `put_async`（精简关键段）：

[sglang_omni/relay/cuda_ipc.py:L700-L734](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/relay/cuda_ipc.py#L700-L734) —— 申请 slot、在独立 stream 上 `copy_`、记录 `ready_event`、导出 IPC handle。注意 `pool_slice.copy_(flat, non_blocking=True)` 是异步拷贝，`ready_event.record(stream)` 标记「写完」。

[sglang_omni/relay/cuda_ipc.py:L760-L789](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/relay/cuda_ipc.py#L760-L789) —— 构造 `metadata` 并返回 `CudaIpcPutOperation`。`metadata` 分两块：`transfer_info`（数据在池子里的位置与槽位信息，接收方按它切片）和 `cuda_ipc`（pool_id、池子显存 IPC 信封、源设备号、ready_event handle，接收方按它导入）。`release_cb=lambda: allocator.release(offset, num_slots)` 被绑进操作句柄——这就是「整段释放」的回调。

接收侧 `get_async`（精简关键段）：

[sglang_omni/relay/cuda_ipc.py:L802-L850](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/relay/cuda_ipc.py#L802-L850) —— 导入对端池子、校验偏移与槽位、用 `from_ipc_handle` 在**目标设备**上重建 ready_event。注释「Import on the waiting device; source-device imports can hang cross-GPU」点出一个坑：跨 GPU 时 event 必须在接收方设备上导入，否则可能挂死。

[sglang_omni/relay/cuda_ipc.py:L852-L903](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/relay/cuda_ipc.py#L852-L903) —— `stream.wait_event(ready_event)` + `dst.copy_(src, non_blocking=True)`，再 `event.record(stream)`，返回 `CudaIpcGetOperation`。`_ensure_peer_access`（[L75-L89](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/relay/cuda_ipc.py#L75-L89)）会在跨卡时尽量开启 P2P 直连，开不了就告警「会绕 host 内存、没有 NVLink 快路径」。

接收方等待拷贝完成：

[sglang_omni/relay/cuda_ipc.py:L290-L340](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/relay/cuda_ipc.py#L290-L340) —— `CudaIpcGetOperation.wait_for_completion`。先 `event.query()` 快速试探（已完成就直接返回，省一次线程切换）；没完成就把 `event.synchronize()` 丢进 `ThreadPoolExecutor` 里跑，避免阻塞 asyncio 事件循环。线程数由 `SGLANG_OMNI_CUDA_IPC_WAIT_THREADS`（默认 8）控制。

#### 4.3.4 代码实践（源码阅读型）

1. **实践目标**：看清发送方与接收方如何靠 `metadata` 里的两个字段协同。
2. **操作步骤**：
   - 在 [cuda_ipc.py:L760-L776](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/relay/cuda_ipc.py#L760-L776) 找到发送方写入的 `metadata`，列出 `transfer_info` 和 `cuda_ipc` 各有哪些 key。
   - 在 [cuda_ipc.py:L829-L849](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/relay/cuda_ipc.py#L829-L849) 找到接收方读取这些 key 的语句，一一对应。
3. **需要观察的现象**：接收方读的每一个 key（`offset`/`slot_index`/`num_slots`/`pool_id`/`pool_storage`/`src_device_id`/`ready_event`）都在发送方写出的 metadata 里有对应来源；没有任何一方去读一个对方没写的字段。
4. **预期结果**：你能总结「metadata 是 put/get 之间唯一的耦合点，cuda_ipc 用 `transfer_info` 描述『数据在哪』、用 `cuda_ipc` 描述『怎么访问那块显存』」。

#### 4.3.5 小练习与答案

**练习 1**：为什么接收方要先 `stream.wait_event(ready_event)` 再 `copy_`，而不是直接 `copy_`？

> **答案**：发送方的 `copy_`（把张量写进池子）是异步的。`ready_event` 记录在发送方写 stream 上，代表「写完」。接收方 `wait_event` 确保自己的读不会跑到发送方的写前面，否则会读到未初始化或半写入的数据。这是跨进程 GPU 数据依赖的标准同步手段。

**练习 2**：`CudaIpcGetOperation.wait_for_completion` 为什么先用 `event.query()` 再用线程池 `synchronize`？

> **答案**：`query()` 是非阻塞的瞬时查询，拷贝往往已经完成（发送与接收之间有控制平面往返延迟），命中就能零开销返回、不必动用线程池；只有真没完成时，才把会阻塞的 `synchronize()` 交给 `ThreadPoolExecutor`，避免把整个 asyncio 事件循环卡住。

---

### 4.4 资源生命周期与单次逻辑 ACK 释放

#### 4.4.1 概念说明

把 4.1～4.3 串起来，就到了本讲最关键的问题（也是 `practice_task`）：**slot 是细粒度的（一个张量可能占好几个 slot），但释放却是「一次 ACK 释放整段区间」——这两者怎么对得上？**

答案是：**「一次 `put_async` = 一次连续 slot 分配 = 一个 `object_id` = 一条 `DataAckMessage` = 一次整段 `release`」是严格的一一对应。** slot 粒度只用在池子内部的「连续段查找」上；对外的资源记账单位永远是「一整段」。一条 `DataAckMessage` 携带一个 `object_id`，发送方据此找到对应的 `release_cb`，一次 `allocator.release(offset, num_slots)` 把整段 `num_slots` 个 slot 全部归还。

这条链路上还有两个必须讲清的工程细节：

- **失败也不能泄漏资源，但更不能错误释放。** 当 ACK 超时（接收方没在 `_ack_timeout_s` 内回确认），发送方的 `wait_for_completion` 走的是 `fail_cb`（标记整个 relay 失败）而**不是** `release_cb`——因为此时数据可能还在被对端读，贸然释放 slot 会引发数据损坏。失败后整个 relay 进入 failed 态，唤醒所有在等 slot 的协程，让它们快速失败退出。
- **abort 时的清理。** `cleanup(request_id)` 在 cuda_ipc 里是空操作（slot 的生命周期由 ACK/release_cb 管理，不按 request_id 另算）；真正的 abort 由上层广播 `AbortMessage` 触发，最终让 pending 的 future 取消。

#### 4.4.2 核心流程

发送侧 ACK 驱动的释放状态机（`CommEngine` 侧）：

```
put_async 完成 -> 注册 _pending[object_id] = (ops=[op], ack=Future)
                 -> 发 DataReadyMessage 给接收方
                 -> 起 _watch_pending(object_id) 任务 await ack

接收方 read_payload 成功 -> _send_data_ack(success=True)
                         -> 回 DataAckMessage(object_id) 给发送方

发送方 CommEngine.ack_transfer(ack):
  pending.ack.set_result(None)            # 解除 _watch_pending 的 await

_watch_pending 唤醒后:
  for op in ops: op.mark_receiver_done()  # 点亮 op 的 _receiver_done future
  for op in ops: await op.wait_for_completion()
      -> PutOperation.wait_for_completion: await _receiver_done -> self._release_cb()
      -> release_cb = allocator.release(offset, num_slots)   # 整段归还
```

失败路径：

```
_watch_pending 超时/异常:
  for op in ops: op.mark_receiver_failed(exc)   # 让 _receiver_done 抛异常
  for op in ops: await op.wait_for_completion()  # 走 fail_cb 分支，调 _mark_failed
                                                # 注意：不调 release_cb
```

#### 4.4.3 源码精读

发送方 `CudaIpcPutOperation`：完成 = 「接收方已确认」，完成后才 `release_cb`：

[sglang_omni/relay/cuda_ipc.py:L202-L246](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/relay/cuda_ipc.py#L202-L246) —— `CudaIpcPutOperation.wait_for_completion`。它 `await self._receiver_done`（一个 future），正常完成后 `self._release_cb()`（归还整段 slot）；超时或异常时调 `self._fail_cb(exc)`（标记 relay 失败），**不**调 release_cb，并清理对 source_tensor/event 的引用。

[sglang_omni/relay/cuda_ipc.py:L248-L255](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/relay/cuda_ipc.py#L248-L255) —— `mark_receiver_done` / `mark_receiver_failed`：分别给 `_receiver_done` 设结果或异常，这就是点亮发送方等待的开关。

ACK 如何变成 `mark_receiver_done`——`CommEngine` 是关键粘合层：

[sglang_omni/comm/engine.py:L401-L431](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/comm/engine.py#L401-L431) —— `_register_pending`/`_arm_pending`/`_watch_pending`。`_watch_pending` 先 `await pending.ack`（等控制平面把 `DataAckMessage` 翻译成 `pending.ack.set_result`），成功后 `op.mark_receiver_done()` 再 `await op.wait_for_completion()`——后者内部触发 `release_cb`，归还整段 slot。失败分支则 `mark_receiver_failed` 后仍要 `wait_for_completion`（走 fail_cb，确保不泄漏对 op 的引用）。

[sglang_omni/comm/engine.py:L241-L263](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/comm/engine.py#L241-L263) —— `ack_transfer`：收到的 `DataAckMessage` 带 `object_id`，据此定位 `_pending[object_id]`，`success=True` 就 `pending.ack.set_result(None)`；`success=False` 就 `set_exception`。注意它对**陈旧 ACK**（object_id 已不在 pending）是 debug 日志 + 忽略，幂等。

`DataAckMessage` 的结构：

[sglang_omni/proto/messages.py:L98-L107](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/proto/messages.py#L98-L107) —— `DataAckMessage` 字段：`request_id` / `from_stage` / `to_stage` / `object_id` / `success` / `error`。**一条消息只带一个 `object_id`**——这就是「一次 ACK 对应一次 put_async 对应一段 slot」的协议层体现。

接收方何时发 ACK：

[sglang_omni/pipeline/stage/runtime.py:L412-L431](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/stage/runtime.py#L412-L431) —— 接收侧 `_receive_remote_payload`（节选）：`read_payload` 成功后调 `_send_data_ack(msg, data_ref, success=True)`；失败则 `success=False, error=...` 并 `relay.cleanup(request_id)`。

[sglang_omni/pipeline/stage/runtime.py:L655-L676](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/stage/runtime.py#L655-L676) —— `_send_data_ack` 构造并发 `DataAckMessage`，其中 `object_id=data_ref.object_id`，与发送方 `write_payload` 生成的 object_id 严格一致。

失败兜底：relay 标记失败后唤醒所有等 slot 的协程：

[sglang_omni/relay/cuda_ipc.py:L623-L652](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/relay/cuda_ipc.py#L623-L652) —— `_acquire_slots` 同时 `await` 「分配成功」和「relay 失败」两个事件（`asyncio.wait(..., FIRST_COMPLETED)`）。一旦 `_mark_failed` 点亮 `_failed_event`，阻塞在 `acquire_async` 里的协程立刻被唤醒并抛错，不会死等一个永远不会空闲的 slot。

`cleanup` 与 `close`：

[sglang_omni/relay/cuda_ipc.py:L905-L913](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/relay/cuda_ipc.py#L905-L913) —— `cleanup(request_id)` 是 **`pass`**（slot 由 ACK/release_cb 管，不按 request_id 单独清）；`close` 清空远程池缓存、storage handle 缓存、池子引用，并关闭等待线程池。

#### 4.4.4 代码实践（对应 practice_task）

1. **实践目标**：阅读 cuda_ipc.py 的发送池分配与单次逻辑 ACK 释放逻辑，**解释 slot 粒度与「一次 `DataAckMessage` 释放整段区间」的关系**。
2. **操作步骤**：
   - 在 [cuda_ipc.py:L689-L697](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/relay/cuda_ipc.py#L689-L697) 看 `put_async` 如何用 `_slots_for_size` 算出 `num_slots`，再 `_acquire_slots` 拿到**一个** `offset`。确认：无论 `num_slots` 是 1 还是 N，返回的 allocation 只有一个 `offset`。
   - 在 [cuda_ipc.py:L778-L789](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/relay/cuda_ipc.py#L778-L789) 看 `release_cb=lambda: allocator.release(offset, num_slots)`——这一个闭包同时捕获 `offset` 和 `num_slots`，即「整段」。
   - 在 [stage_io.py:L311-L314](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/comm/stage_io.py#L311-L314) 看 `object_id = f"{request_id}:payload:{from_stage}:{to_stage}"`——一次 put 生成一个 object_id。
   - 在 [messages.py:L98-L107](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/proto/messages.py#L98-L107) 确认 `DataAckMessage` 只带**一个** `object_id`。
   - 在 [engine.py:L415-L429](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/comm/engine.py#L415-L429) 看 `_watch_pending` 收到这一个 ACK 后，对 `pending.ops` 里的**每一个** op 调 `mark_receiver_done` + `wait_for_completion`，后者触发**那一个** `release_cb`，进而 `allocator.release(offset, num_slots)` 一次归还整段。
3. **需要观察的现象 / 预期结论（即本题答案）**：
   - **slot 粒度**只存在于池子内部——它决定「一个张量占几个连续 slot」，是空间碎片管理的需要。
   - **释放粒度**是「整段」——`release(offset, num_slots)` 一次归还这次 `put_async` 申请的全部 `num_slots` 个连续 slot。
   - 两者之所以兼容，是因为协议保证 **「一次 put_async ⇄ 一个 object_id ⇄ 一条 DataAckMessage ⇄ 一次整段 release」严格一一对应**：一条 ACK 永远只针对一个 object_id，而该 object_id 背后记录了完整的 `(offset, num_slots)`，所以一次 ACK 就能、且只能释放那整段。
   - cuda_ipc 的 `cleanup(request_id)` 是空操作正说明了这一点：slot 生命周期完全由「ACK → release_cb」这条链管理，不需要再按 request_id 做额外回收。
4. 待本地验证：跑 `pytest tests/unit_test/pipeline/test_comm_engine_ack.py -q`，观察 ACK 成功/失败/陈旧三条路径的行为；以及 `pytest tests/unit_test/relay/test_cuda_ipc_relay.py::test_cuda_ipc_put_timeout_fails_relay_without_releasing_slot`，验证**超时不会触发 release_cb**（防止数据损坏）。

#### 4.4.5 小练习与答案

**练习 1**：如果一次 `put_async` 占了 5 个 slot，接收方回的 `DataAckMessage` 会释放几个 slot？是发 5 条 ACK 还是 1 条？

> **答案**：1 条 ACK，释放 5 个 slot。`DataAckMessage` 只携带一个 `object_id`，对应这一次 `put_async`；发送方收到后调 `release_cb` → `allocator.release(offset, num_slots=5)`，一次性把这段 5 个连续 slot 全部归还。slot 数量只影响 `release` 内部循环的次数，不改变「一条 ACK 对应一次释放」的语义。

**练习 2**：为什么 ACK 超时时，`wait_for_completion` 调 `fail_cb` 而不是 `release_cb`？

> **答案**：超时意味着接收方可能仍在读这段显存，或者 ACK 只是丢了但拷贝还在进行。此时释放 slot 给下一个张量复用，会让两个张量写到/读自同一块显存，造成数据损坏。所以选择「不释放 + 标记整个 relay 失败」：牺牲这一个 relay 实例（之后所有 put 都会快速失败），换取不破坏数据完整性。这体现了「宁可整体报废，不可局部损坏」的资源安全原则。

**练习 3**：对照 [shm.py:L48-L98](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/relay/shm.py#L48-L98)，shm 后端的 `ShmPutOperation` 也用了 `mark_receiver_done` + `release_cb`。它释放的「整段区间」是什么？

> **答案**：shm 没有槽池，它的「整段」就是**一整个命名的 SharedMemory 块**。`release_cb` 是 `self._sem.release()`——归还一个信号量信用（`credits` 控制并发块数）。语义和 cuda_ipc 完全一致：一次 put 对应一个 SHM 块、一个 object_id、一条 ACK、一次释放；区别只是「释放的是显存 slot 段」还是「一个 SHM 块的信用」。

## 5. 综合实践

把本讲四个模块串起来，做一次「端到端张量搬运的纸面推演 + 行号取证」：

**任务**：假设 thinker stage 要把一个 `[8, 4096]` 的 bfloat16 hidden state（`size = 8*4096*2 = 65536` 字节，恰好 1 个 64KB slot）经 cuda_ipc 传给同节点的 talker stage。请按顺序回答，并为每一步给出**源码行号证据**：

1. **写**：talker 侧的 `CudaIpcRelay`（假设 `slot_size_kb=64`）这次占几个 slot、offset 可能是多少？依据 [cuda_ipc.py:L141-L144](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/relay/cuda_ipc.py#L141-L144) 与 [L700-L703](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/relay/cuda_ipc.py#L700-L703)。
2. **信封**：`put_async` 返回的 `metadata` 里，`transfer_info.num_slots` 和 `cuda_ipc.ready_event` 各是什么？依据 [L760-L776](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/relay/cuda_ipc.py#L760-L776)。
3. **通知**：这段 metadata 如何被装进 `DataRef` 并经 `DataReadyMessage` 送到 talker？依据 [stage_io.py:L306-L324](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/comm/stage_io.py#L306-L324) 与 [engine.py:L316-L325](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/comm/engine.py#L316-L325)。
4. **读**：talker 侧 `get_async` 如何用 `ready_event` 保证读到完整数据？依据 [cuda_ipc.py:L862-L874](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/relay/cuda_ipc.py#L862-L874)。
5. **确认与释放**：talker 读完后发的 `DataAckMessage` 携带的 `object_id` 是什么？thinker 收到后释放几个 slot？依据 [runtime.py:L431](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/stage/runtime.py#L431)、[engine.py:L415-L429](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/comm/engine.py#L415-L429)、[cuda_ipc.py:L224-L246](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/relay/cuda_ipc.py#L224-L246)。

**参考结论**：

1. `num_slots = ceil(65536 / 65536) = 1`；若池空闲，offset 一般是 `0`（首个连续段）。
2. `num_slots=1`；`ready_event` 是一个 `interprocess=True` 的 CUDA Event 的 IPC handle（跨进程句柄），记录在 thinker 写池子的 stream 上。
3. `op.metadata` 被装进 `DataRef.buffer.info`（`BackendRef.from_relay_info`），`DataReadyMessage.data_ref` 携带该 `DataRef` 的 dict，经控制平面 PUSH 给 talker。
4. talker 导入 `ready_event`，在自身读 stream 上 `stream.wait_event(ready_event)` 确保 thinker 已写完，再 `dst.copy_(src, non_blocking=True)`。
5. `object_id` 形如 `<request_id>:payload:thinker:talker`；thinker 的 `_watch_pending` 收到这一条 ACK → `mark_receiver_done` → `wait_for_completion` → `release_cb` → `allocator.release(offset, num_slots=1)`，释放这 **1** 个 slot。一次 put、一个 object_id、一条 ACK、一次整段释放——链路闭合。

> 若本地有双 GPU，可用 `tests/unit_test/relay/test_cuda_ipc_relay.py` 跑真实的双进程往返（该测试在文件头注明需两个真实进程，因为一个进程打不开自己的 CUDA IPC handle）。

## 6. 本讲小结

- **数据平面（relay）与控制平面分工**：控制平面传小命令，relay 传大张量；二者只靠 `DataReadyMessage.data_ref`（携带 `op.metadata`）与回程的 `DataAckMessage` 协作。
- **`Relay` / `RelayOperation` 是统一契约**：`put_async`/`get_async`/`cleanup`/`close` 四方法 + `metadata`/`wait_for_completion`/`mark_receiver_done`；`metadata` 是发送方写给接收方的「取货单」，内容由各后端自定义。
- **CUDA IPC 后端用有界 GPU 槽池**：池子按 `slot_size`（默认 64KB）切成 `slot_count` 个等大 slot，每个张量按 `ceil(size/slot_size)` 占用一段**连续** slot，懒分配、可复用。
- **put/get 靠 CUDA Event 同步**：发送方在写 stream 上记录 `interprocess` event 并导出 IPC handle，接收方 `wait_event` 后再 `copy_`，跨进程数据依赖靠 event 保证。
- **资源生命周期严格一一对应**：一次 `put_async` ⇄ 一个 `object_id` ⇄ 一条 `DataAckMessage` ⇄ 一次整段 `release(offset, num_slots)`。slot 粒度只是池内碎片管理，对外释放单位永远是整段。
- **失败时不释放、而是标记 relay 失败**：ACK 超时走 `fail_cb` 不走 `release_cb`，避免数据损坏；失败事件会唤醒所有阻塞在 `acquire_async` 的协程，防止死等。

## 7. 下一步学习建议

- 本讲只讲了「选定了 cuda_ipc 之后内部怎么搬」。至于**某条 stage 边到底选 cuda_ipc 还是 shm / nixl / mooncake / local_object**，由 `CommRouter` 按阶段局部性派生——这正是 [u6-l1 通信路由与传输选择] 的主题，建议接着读。
- 想看 relay 在真实 Stage 里如何被 `CommEngine`/`StageIO` 调用，可回顾 [u3-l1 Stage 抽象与 IO 外壳] 的 scheduler 桥接部分，并把本讲的 ACK 链路套进去。
- 对跨节点 RDMA 感兴趣的读者，可对照阅读 [relay/nixl.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/relay/nixl.py) 与 [relay/mooncake.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/relay/mooncake.py)，体会它们如何用各自的通知机制替代本讲的 `DataAckMessage`+`mark_receiver_done`。
- 性能调优时，关注 cuda_ipc 的池子规格（`pool_size_mb`/`slot_size_kb`）与等待线程数（`SGLANG_OMNI_CUDA_IPC_WAIT_THREADS`），并结合 [u6-l3 请求级 Profiler] 的 comm trace 事件（`cuda_ipc_put_async`/`cuda_ipc_put_wait_ack`/`cuda_ipc_get_wait_copy`）定位传输瓶颈。
