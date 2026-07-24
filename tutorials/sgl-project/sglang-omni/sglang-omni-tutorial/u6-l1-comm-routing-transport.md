# 通信路由与传输选择

## 1. 本讲目标

学完本讲，你应该能够：

- 说出 SGLang-Omni 在两个 stage 之间搬运数据时，**共有哪几种物理传输**，以及它们各自的适用场景。
- 看懂 `CommRouter` 如何仅凭「局部性」（同进程 / 同节点跨 GPU / 跨节点）这一信息，把一条 stage 边映射到一种传输。
- 理解 `LOCAL_OBJECT` 直传为什么是「引用传递 + 只读」契约，以及它为何不建 relay。
- 读懂 `CommEngine` 如何把「路由结果」落成真正的字节搬运，特别是它「**先发控制消息、再等传输完成**」的排序对 NIXL / Mooncake 这类基于信用（credit）的后端为何是必需的。
- 读懂 `StageIO` 如何把 stage 对象打包成 `DataRef`、在接收侧还原，以及「直接 CUDA IPC」旁路何时启用。

## 2. 前置知识

本讲是 **u3-l3（Relay 数据平面与传输后端）** 的承接与上收。请先确认你已经掌握：

- **控制平面 vs 数据平面**：控制平面用 ZMQ + msgpack 传小而快的命令状态（见 u3-l2），数据平面用 relay 传大张量。
- **`DataRef` 是「指针」**：控制消息里只带一个指向数据本体的 `DataRef`，张量本体走 relay（见 u3-l3）。
- **stage 之间构成有向图**：一条边 `A → B` 表示 stage A 把产物交给 stage B（`next` / `stream_to` 等，见 u2-l5、u3-l1）。
- **进程拓扑**：哪些 stage 共享同一个 OS 进程（见 u3-l4）。

一个关键认知：u3-l3 讲的是「relay 后端（cuda_ipc/shm/nixl/mooncake）各自的实现」，而本讲讲的是更上一层的问题——**给定一条边，到底该用哪一个后端？** 这个决策由 `CommRouter` 做，它就是本讲的主角。

> 术语速查：
> - **局部性（locality）**：两个 stage 在物理上挨得多近——同一进程？同一节点同一 GPU？同一节点不同 GPU？不同节点？
> - **信用（credit）**：发送方持有的「发送配额」。基于池的后端（CUDA IPC / NIXL / Mooncake）发送前要先占用一个 slot/credit，接收方确认消费后才能归还。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [sglang_omni/comm/data_ref.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/comm/data_ref.py) | 定义 `TransportKind` 枚举（四种传输）与 `DataRef`「指针」结构。 |
| [sglang_omni/comm/router.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/comm/router.py) | **本讲核心**。`CommRouter` 负责局部性分类、选择传输、按需懒构造 relay。 |
| [sglang_omni/comm/engine.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/comm/engine.py) | `CommEngine`：stage 拥有的通信引擎，负责字节搬运（入队、写、发控制消息、等 ACK）。 |
| [sglang_omni/comm/stage_io.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/comm/stage_io.py) | `StageIO`：在「stage 对象」与「数据平面 `DataRef`」之间做打包/解包，并实现「直接 CUDA IPC」旁路。 |
| [sglang_omni/relay/base.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/relay/base.py) | `Relay` / `RelayOperation` 抽象与 `CreditAllocator`（信用分配器）。 |
| [sglang_omni/pipeline/stage/runtime.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/stage/runtime.py) | `Stage` 在发送侧调用 router / engine / stage_io，并决定走「本地直传 / 直接 CUDA IPC / relay」哪条路径。 |
| [tests/unit_test/pipeline/test_comm_router.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/tests/unit_test/pipeline/test_comm_router.py) | 路由规则的权威测试用例，是理解规则的最好 cheatsheet。 |

## 4. 核心概念与源码讲解

### 4.1 传输种类与选择规则表（transport 选择）

#### 4.1.1 概念说明

SGLang-Omni 把所有跨 stage 搬运抽象成 **四种传输**，定义在 `TransportKind` 枚举里：

```python
class TransportKind(str, Enum):
    LOCAL_OBJECT = "local_object"   # 同进程：直接传 Python 对象引用
    CUDA_IPC = "cuda_ipc"           # 同节点 GPU↔GPU：走 relay 的 CUDA IPC 池
    SHM = "shm"                     # CPU 张量：走 relay 的共享内存
    MOONCAKE = "mooncake"           # 跨节点：走 Mooncake（RDMA / 网络）
```

来源：[sglang_omni/comm/data_ref.py:11-15](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/comm/data_ref.py#L11-L15)

关键直觉是 **「挨得越近，传输越便宜」**：

- 两个 stage 在**同一进程**里 → 根本不用拷贝，直接传对象引用（`LOCAL_OBJECT`）。
- 两个 stage 在**同一节点**、且都住在 **GPU** 上 → 用 CUDA IPC，GPU 显存直接共享，省一次「GPU→CPU→GPU」的中转（`CUDA_IPC`）。
- 数据在 **CPU**（或目标不在 GPU） → 用共享内存（`SHM`）。
- 两个 stage 在**不同节点** → 只能走网络，用 Mooncake（`MOONCAKE`）。

> 注意：`TransportKind` 里**没有**单独的「直接 CUDA IPC」枚举值。所谓「直接 CUDA IPC」是一条**代码旁路**（同 GPU 时的零拷贝优化），它复用了 `torch` 自带的 IPC handle，绕过 relay 池，在控制消息里直接内嵌 handle。我们在 4.4 节单独讲它。

#### 4.1.2 核心流程：选择规则表

`CommRouter.outbound(target)` 用一张固定的优先级表做决策。用伪代码表示：

```
outbound(target):
    if target in same_process_targets:   return LOCAL_OBJECT   # ① 同进程，最高优先
    return _physical_outbound(target)

_physical_outbound(target):
    if target in remote_stage_names:     return MOONCAKE       # ② 跨节点
    if self_is_gpu and target in gpu_stage_names:
                                         return CUDA_IPC       # ③ 同节点 + 双方都是 GPU
    return SHM                                                  # ④ 兜底（CPU / 单边非 GPU）
```

浓缩成一张表：

| 边的局部性 | 条件 | 选择 |
|---|---|---|
| 同进程 | `target ∈ same_process_targets` | `LOCAL_OBJECT` |
| 跨节点 | `target ∈ remote_stage_names` | `MOONCAKE` |
| 同节点 + 自己是 GPU + 目标是 GPU 阶段 | `self_is_gpu and target ∈ gpu_stage_names` | `CUDA_IPC` |
| 其它（同节点 CPU 等） | 兜底 | `SHM` |

除了 `outbound`（按「边的局部性」选），router 还提供两个**载荷感知**的变体：

- `outbound_payload(target, payload)`：会真正检查 payload 里张量的 `device`。全 CPU 张量 → `SHM`；含 CUDA 张量且双方都是 GPU → `CUDA_IPC`；张量设备混用 → 直接报错。来源：[sglang_omni/comm/router.py:135-145](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/comm/router.py#L135-L145)。
- `outbound_stream(target, data)`：流式 chunk 要求 `data` 必须是 `torch.Tensor`，且**不能**把 CUDA 张量发给非 GPU 目标（会抛 `ValueError`）。来源：[sglang_omni/comm/router.py:83-98](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/comm/router.py#L83-L98)。

为什么需要载荷感知？因为「边的局部性」只告诉你目标 stage 是否**可能**住在 GPU，但具体这条 payload 里装的是 CPU 张量还是 GPU 张量，要等运行时才知道。例如 `thinker → decode` 这条边双方都是 GPU 阶段，但若某次只传 CPU 上的 token ids，`outbound_payload` 会务实地下放到 `SHM`，而不是强行用 CUDA IPC。

#### 4.1.3 源码精读

先看最核心的 `outbound` 与 `_physical_outbound`：

```python
def outbound(self, target: str) -> TransportKind:
    if target in self.same_process_targets:
        return TransportKind.LOCAL_OBJECT
    return self._physical_outbound(target)

def _physical_outbound(self, target: str) -> TransportKind:
    if target in self.remote_stage_names:
        return TransportKind.MOONCAKE
    if self.self_is_gpu and target in self.gpu_stage_names:
        return TransportKind.CUDA_IPC
    return TransportKind.SHM
```

来源：[sglang_omni/comm/router.py:66-81](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/comm/router.py#L66-L81)

这里有一个**重要的安全提醒**写在注释里（值得逐字理解）：目前「跨节点」是靠 `remote_stage_names` 这个白名单显式声明的；凡是不在 `remote_stage_names` 里的目标，都被默认当作「同节点」。源码注释明确警告——如果未来的放置规划（placement）忘记把某条跨节点边填进 `remote_stage_names`，它会被**静默**降级成 `cuda_ipc/shm`，而不会报错（因为还缺一个节点级配置阶段来做硬断言）。这是阅读本文件时最容易踩的认知坑。

`self_is_gpu` 是一个属性，只看 `gpu_id is not None`：

```python
@property
def self_is_gpu(self) -> bool:
    return self.gpu_id is not None
```

来源：[sglang_omni/comm/router.py:56-58](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/comm/router.py#L56-L58)

注意：它**不**检查当前进程是否真的能看到那块 GPU——纯粹是一个局部性标记。这也意味着「路由决策本身不依赖 GPU」，我们后面会用它设计可在纯 CPU 机器上跑的实践。

#### 4.1.4 代码实践

**实践目标**：把本节的选择规则表「跑」一遍，验证你的直觉。

**操作步骤**（无需 GPU，纯 CPU 即可，因为 `outbound()` 只做集合成员判断）：

```python
# 示例代码：在项目根目录的 python REPL 里运行
from sglang_omni.comm.router import CommRouter
from sglang_omni.comm.data_ref import TransportKind

r = CommRouter(
    stage_name="thinker",
    gpu_id=0,                                   # self_is_gpu = True
    same_process_targets={"local_helper"},      # 同进程目标
    gpu_stage_names={"decode"},                 # 同节点 GPU 阶段
    remote_stage_names={"remote_decode"},       # 跨节点目标
    comm_config={},
)

print(r.outbound("local_helper"))   # 预期 LOCAL_OBJECT
print(r.outbound("decode"))         # 预期 CUDA_IPC
print(r.outbound("remote_decode"))  # 预期 MOONCAKE
print(r.outbound("some_cpu_stage")) # 预期 SHM
```

**需要观察的现象**：四条 `print` 的输出应分别是 `TransportKind.LOCAL_OBJECT / CUDA_IPC / MOONCAKE / SHM`。

**预期结果**：与选择规则表完全一致。这正是官方测试 `test_comm_router_uses_mooncake_only_for_remote_edges` 断言的内容，见 [tests/unit_test/pipeline/test_comm_router.py:25-40](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/tests/unit_test/pipeline/test_comm_router.py#L25-L40)。

> 关于 `outbound_payload` / `outbound_stream` 涉及真实 CUDA 张量的分支（如 `test_comm_router_uses_cuda_ipc_for_mixed_gpu_payloads`），需要真实 GPU 才能验证，**待本地验证（需 GPU）**。

#### 4.1.5 小练习与答案

**练习 1**：若一个 stage 的 `gpu_id=None`（纯 CPU 阶段，如 preprocessing），它向一个 GPU 阶段发 payload，`outbound()` 会返回什么？为什么？

> **答案**：返回 `SHM`。因为 `self_is_gpu` 为 `False`，`_physical_outbound` 里 `self.self_is_gpu and target in gpu_stage_names` 整个条件为假，落到兜底的 `SHM`。这也符合直觉：发送方自己没有 GPU，无从发起 CUDA IPC，只能走 CPU 共享内存，由接收方 GPU 阶段自行把数据搬上卡。

**练习 2**：`remote_stage_names` 没有包含某条真实的跨节点边，会发生什么？

> **答案**：会被静默当成同节点，落到 `CUDA_IPC` 或 `SHM`，而不会报错（见 4.1.3 的源码注释警告）。这是一个需要在放置规划阶段主动避免的陷阱。

---

### 4.2 `LOCAL_OBJECT` 直传：引用传递与只读契约

#### 4.2.1 概念说明

`LOCAL_OBJECT` 是四种传输里最特殊的一个：**它不走 relay、不拷贝、不发 ACK**。当两个 stage 共享同一个 OS 进程（见 u3-l4 进程拓扑）时，发送方直接把 Python 对象**引用**递交给接收方，就像函数调用传参一样。

这带来两个必须遵守的契约：

1. **只读契约**：接收方必须把收到的 payload / 流数据 / metadata 当作**只读**，除非这条边显式给了它一个「隔离的投影对象」（isolated projected object）。
2. **生命周期契约**：因为没有拷贝，发送方必须保证这个对象在接收方用完之前**不会被释放或被原地改写**。

这两个契约不是「建议」，而是写在 `LocalStageDispatcher` 的类文档里的硬约束：

> "Process-local dispatch passes Python object references directly. Receivers must treat payloads, stream data, and metadata as read-only unless the edge explicitly gives them an isolated projected object."

来源：[sglang_omni/pipeline/local_dispatch.py:9-15](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/local_dispatch.py#L9-L15)

#### 4.2.2 核心流程：谁有资格成为 `same_process_targets`？

一个目标 stage 能否走 `LOCAL_OBJECT`，由进程拓扑求解阶段决定。规则（见 `_resolve_same_process_targets`）：

```
对当前 stage 的每个下游目标（来自 next + stream_to）：
    if 当前 stage 是 TP 阶段 (tp_size > 1):   跳过（TP 阶段不参与同进程直传）
    if 目标 stage 是 TP 阶段:                  跳过
    if 目标与当前 stage 在进程拓扑里属同一 OS 进程:
        把目标加入 same_process_targets
```

来源：[sglang_omni/pipeline/mp_runner.py:185-212](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/mp_runner.py#L185-L212)

关键点：**只有「同进程」且「双方都不是 TP 阶段」** 才能走本地直传。TP 阶段（张量并行）一律被排除——因为 TP 阶段必须独占进程、有自己独立的 NCCL / 控制平面（见 u3-l4、u6-l6）。

#### 4.2.3 源码精读

在 `CommRouter` 里，`LOCAL_OBJECT` 是一个「**没有 relay**」的传输。任何试图为它取 relay 的调用都会被显式拒绝：

```python
def relay(self, kind: TransportKind) -> Relay:
    if kind is TransportKind.LOCAL_OBJECT:
        raise ValueError("local_object has no relay")
    ...
```

来源：[sglang_omni/comm/router.py:107-116](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/comm/router.py#L107-L116)

`relay_for(target)` 同样会在目标是同进程时抛错，并提示「请走 local-object 派发」：

```python
def relay_for(self, target: str) -> tuple[TransportKind, Relay]:
    kind = self.outbound(target)
    if kind is TransportKind.LOCAL_OBJECT:
        raise ValueError(
            f"same-process target {target!r} has no relay transport; "
            "use local-object dispatch"
        )
    return kind, self.relay(kind)
```

来源：[sglang_omni/comm/router.py:118-125](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/comm/router.py#L118-L125)

那么真正走 `LOCAL_OBJECT` 的代码在哪？在 `Stage` 的发送路径里——它先判断目标是否同进程，若是，就直接交给 `_local_dispatcher`（即 `LocalStageDispatcher`），完全不碰 relay / engine：

```python
await self._local_dispatcher.send_payload(
    from_stage=self.name,
    to_stage=target,
    request_id=request_id,
    payload=projected_payload,
)
return
```

来源：[sglang_omni/pipeline/stage/runtime.py:1124-1130](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/stage/runtime.py#L1124-L1130)

注意它传的是 `projected_payload`——如果这条边配了 `project_payload` 投影函数，就会给接收方一个「隔离对象」，此时接收方可以安全改写；否则就是同一个引用，必须只读。这正是只读契约的落点。

#### 4.2.4 代码实践

**实践目标**：理解「引用直传」与「relay 拷贝」在行为上的差异。

**操作步骤**（源码阅读型）：

1. 打开 [sglang_omni/pipeline/local_dispatch.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/local_dispatch.py)，阅读 `LocalStageDispatcher.send_payload`，确认它只是把对象塞进目标 stage 的接收队列、**没有任何 `.clone()` / `.copy()`**。
2. 对比 [sglang_omni/comm/stage_io.py:290-324](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/comm/stage_io.py#L290-L324) 的 `write_payload`——relay 路径会把张量 `.contiguous().view(torch.uint8)` 后 `torch.cat` 进发送池，是一次实打实的拷贝。

**需要观察的现象**：本地直传路径里找不到任何张量拷贝调用；relay 路径里有显式的 `torch.cat` 与 `put_async`。

**预期结果**：本地直传 = 零拷贝引用传递；relay = 拷贝进有界池。这也解释了为什么 `LOCAL_OBJECT` 不需要 ACK——没有发送池资源要回收。

#### 4.2.5 小练习与答案

**练习**：为什么 TP 阶段之间的边**不能**走 `LOCAL_OBJECT`，哪怕它们恰好被放在同一进程？

> **答案**：因为进程拓扑硬性规定 TP 阶段必须**独占** OS 进程（见 u3-l4、stage_workers.py 的 `_get_worker_process_env` 注释），所以两个 TP 阶段根本不可能在同一进程里；`_resolve_same_process_targets` 也对 `tp_size > 1` 的 stage 直接返回空集（mp_runner.py:191-192），从源头排除。这是「TP 独占进程」不变量在通信层的体现。

---

### 4.3 `CommEngine`：把路由结果落成字节搬运

#### 4.3.1 概念说明

`CommRouter` 只负责「选哪种传输」这一**路由决策**；真正把字节搬过去的是 `CommEngine`。它的定位在类文档里说得很清楚：

> "Stages keep routing semantics; the engine owns byte movement mechanics."
> （Stage 保留路由语义；engine 负责字节搬运机制。）

来源：[sglang_omni/comm/engine.py:59-64](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/comm/engine.py#L59-L64)

`CommEngine` 是每个 `Stage` 私有的，内部持有一个 `CommRouter`。它的对外能力是「发送 payload / 发送流 chunk / 读取 payload / 读取流 chunk / 处理 ACK / 清理」。它把 router 选出的 `TransportKind` 翻译成具体的「写 relay → 发控制消息 → 等 ACK」三段式动作。

#### 4.3.2 核心流程：发送一次 payload 的完整时序

发送一个 payload 要跨**两个平面**协作（数据平面写张量、控制平面通知接收方）。时序如下：

```
[发送方 CommEngine.send_payload]
  1. 把 job 放入「按目标 stage 分」的发送队列 (send_queue)
  2. 该队列的 worker 取出 job：
     a. write_payload(relay, payload)        # 数据平面：relay.put_async 写入池，拿到 op + metadata
     b. register_pending(object_id, [op])    # 登记一个「待 ACK」条目
     c. control_plane.send_to_stage(         # 控制平面：发 DataReadyMessage，data_ref 携带「取货单」
            DataReadyMessage(data_ref=...))
     d. arm_pending(object_id)               # 启动一个 watcher 等待 ACK
  3. job.ready.set_result(data_ref)          # 通知 send_payload 的调用方「指针已发出」

[接收方]（收到 DataReadyMessage）
  4. read_payload(relay, data_ref)           # 数据平面：relay.get_async 读出张量
  5. control_plane 回送 DataAckMessage        # 控制平面：告诉发送方「我读完了」

[发送方 watcher (_watch_pending)]
  6. 收到 ACK → op.mark_receiver_done()       # 标记接收方已消费
  7. op.wait_for_completion()                 # 等「释放 slot」真正完成（如 CUDA Event 同步、信用归还）
```

这张时序图里最关键的一步是 **(c) 在 (g) 之前**——也就是本节的学习重点：**控制消息必须在「等待传输完成」之前发出**（control-before-wait）。

为什么这个顺序对 NIXL / Mooncake 这类基于信用的后端是性命攸关的？

- 这些后端的 `put_async` 只是把数据写进**本地**发送缓冲区，并立刻返回一个 `op`。所谓「传输完成」并不是「写完本地」，而是「**接收方已经把数据消费掉、发送方可以安全释放这个 slot/credit**」。
- 而接收方只有在**收到 `DataReadyMessage`（控制消息）**之后，才知道「有数据可取、取货单在哪」，才会去 `get_async` 消费。
- 于是形成依赖链：`完成 = 接收方消费 = 接收方收到控制消息`。如果发送方在 (c) **之前**就阻塞在 `wait_for_completion` 上，接收方永远收不到通知、永远不会消费、完成事件永远不会触发 → **死锁**。

所以正确顺序必须是：先把控制消息（指针）发出去通知接收方，再去等完成。源码里 (c) `send_to_stage` 出现在 (d) `arm_pending`（内部才 `wait_for_completion`）之前，正是这个不变量。

> 对于无信用的 `SHM` 后端，`wait_for_completion` 基本是本地拷贝同步、很快返回，但 control-before-wait 依然是统一的最小代价正确顺序——引擎对所有后端一视同仁，不因后端而分支。

#### 4.3.3 源码精读

先看 `send_payload` 如何入队（按目标分队列）：

```python
def _send_queue_for(self, queue_key: str):
    ...
    queue = asyncio.Queue(maxsize=self._send_queue_size)   # 默认 1024
    self._send_queues[queue_key] = queue
    task = asyncio.create_task(self._run_send_worker(queue_key, queue))
    ...
```

来源：[sglang_omni/comm/engine.py:265-279](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/comm/engine.py#L265-L279)

要点：**每个目标 stage 一条独立队列 + 一个独立 worker 任务**。这意味着发往不同目标的发送互不阻塞；发往同一目标的发送被串行化（保证顺序）。队列满（默认 1024 个在途 job）时 `queue.put` 会自然背压。

接着看 worker 内部 `_run_payload_send` 的「写 → 发控制 → arm」三段（注意顺序）：

```python
# a. 数据平面：写入 relay，拿到 data_ref 与 op
data_ref, op = await stage_io.write_payload(
    job.relay, job.request_id, job.payload,
    transport=job.transport, from_stage=job.from_stage, to_stage=job.to_stage,
)
object_id = data_ref.object_id
self._register_pending(object_id, [op])

# c. 控制平面：先发 DataReadyMessage（control BEFORE wait）
await job.control_plane.send_to_stage(
    job.to_stage, job.target_endpoint,
    DataReadyMessage(request_id=..., from_stage=..., to_stage=..., data_ref=data_ref.to_dict()),
)
# d. 发完控制消息，才 arm 一个 watcher 去等完成
self._arm_pending(object_id)
```

来源：[sglang_omni/comm/engine.py:297-340](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/comm/engine.py#L297-L340)

再看 watcher `_watch_pending`——ACK 到达后的处理顺序：

```python
async def _watch_pending(self, object_id, pending):
    try:
        await asyncio.wait_for(pending.ack, timeout=self._ack_timeout_s)  # 默认 30s
        for op in pending.ops:
            op.mark_receiver_done()          # 接收方已消费
        for op in pending.ops:
            await op.wait_for_completion(timeout=self._ack_timeout_s)  # 再等 slot 释放
    ...
```

来源：[sglang_omni/comm/engine.py:415-431](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/comm/engine.py#L415-L431)

ACK 如何唤醒 watcher？接收方回送的 `DataAckMessage` 经控制平面到达发送方，调用 `ack_transfer`，把 `pending.ack` 这个 future 完成：

```python
def ack_transfer(self, ack: DataAckMessage) -> None:
    ...
    pending = self._pending.get(ack.object_id)
    ...
    if ack.success:
        if not pending.ack.done():
            pending.ack.set_result(None)     # 唤醒 _watch_pending
        return
    ...
```

来源：[sglang_omni/comm/engine.py:241-263](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/comm/engine.py#L241-L263)

补充：ACK 超时（默认 30s）不会错误地释放 slot——`_watch_pending` 的 `except` 分支会走 `mark_receiver_failed` 并 raise，由更上层决定如何处理，避免「数据其实没被消费完就归还 slot」造成的数据损坏（承接 u3-l3 的 ACK 纪律）。

#### 4.3.4 代码实践

**实践目标**：用一张时序图固化「control-before-wait」的记忆。

**操作步骤**（源码阅读型）：

1. 打开 [sglang_omni/comm/engine.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/comm/engine.py)，在 `_run_payload_send`（L297）里标出三行：`write_payload`、`send_to_stage`、`_arm_pending`。
2. 在 `_watch_pending`（L415）里标出：`wait_for(pending.ack)`、`mark_receiver_done`、`wait_for_completion`。
3. 画两条泳道（发送方 / 接收方），把上面 7 个步骤连成时序图。

**需要观察的现象**：`send_to_stage`（控制消息）出现在 `_arm_pending` 之前；`_arm_pending` 内部才出现 `wait_for_completion`。

**预期结果**：你能在图上明确指出「控制消息先于 wait_for_completion」这一顺序，并能用一句话解释「若颠倒会死锁」的原因。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `CommEngine` 要「按目标 stage 分队列」，而不是所有目标共用一条队列？

> **答案**：为了让发往不同目标的发送**互不阻塞**（一个慢目标不应拖累发往快目标的 job），同时保证发往**同一**目标的发送**按序**到达（worker 串行消费同一队列）。这也天然提供了背压：单目标在途 job 过多时，该队列 `put` 会阻塞。

**练习 2**：若 ACK 在超时窗口（默认 30s）内始终未到，engine 会怎么处理 slot？

> **答案**：不会归还 slot。`_watch_pending` 的 `except` 分支会 `mark_receiver_failed` 并向上 raise，把故障交给上层；这样即使超时，也不会出现「接收方其实还没读完、发送方却已释放 slot」的数据损坏。承接 u3-l3 所述的 ACK 严格一一对应纪律。

---

### 4.4 `StageIO`：打包/解包与「直接 CUDA IPC」旁路

#### 4.4.1 概念说明

`StageIO` 是「**stage 对象**」与「**数据平面 `DataRef`**」之间的适配层。stage 手里拿的是 Python 对象（嵌套 dict / 列表 / `torch.Tensor`），而数据平面只认一段连续缓冲区 + 一份「取货单」metadata。`StageIO` 负责：

- **发送侧**：把对象里的所有 `torch.Tensor` 抽出来、拼接成一段连续 buffer、调用 `relay.put_async`，并生成一个 `DataRef`（含 tensor 的 shape/dtype/offset 等元信息，让接收方能还原）。
- **接收侧**：读出 buffer，按 `DataRef` 里的元信息把每个 tensor 切回来，重新组装成原来的对象结构。

此外，它还实现了一条**旁路**：当两个 stage 恰好住在**同一块 GPU** 上时，可以走「直接 CUDA IPC」——把 tensor 的 IPC handle 直接内嵌在控制消息里，**完全绕过 relay 池**。

#### 4.4.2 核心流程

**普通打包（write_payload）**：

```
payload.data (含 tensor)
  → extract_tensors: 把每个 tensor 替换成 placeholder，收集 {path: tensor}
  → _pack_tensors: 按 dtype 对齐 padding，torch.cat 拼成一段连续 buffer（在 relay 设备上）
  → relay.put_async(buffer) → 得到 op.metadata（取货单）
  → 组装 DataRef: header=pickle(无 tensor 的 payload), tensors=[TensorMeta(path,shape,dtype,offset,size)], buffer=BackendRef(metadata)
```

**普通解包（read_payload）**：

```
DataRef
  → 读 header: pickle.loads 还原无 tensor 的 payload 骨架
  → relay.get_async 读出整段 buffer
  → 按 tensors 里每个 TensorMeta 的 offset/size/shape/dtype 切出 tensor
  → restore_tensors: 把 tensor 填回骨架 → 还原成原对象
  → relay.cleanup(request_id)
```

**直接 CUDA IPC 旁路（仅同 GPU）**：

```
serialize_direct_cuda_ipc_payload(payload):
  → 只抽 CUDA tensor（extract_cuda_tensors）
  → 对每个 tensor 用 ForkingPickler 生成 IPC handle 字节（torch 跨进程共享显存的标准机制）
  → 组装成 {_type: "TorchCudaIpcPayload", header: pickle(骨架), tensors: [{path, tensor_bytes}]}
  → 整个结构作为 data_ref 内嵌进 DataReadyMessage，走控制平面
  （不调 relay，不需要 ACK，没有 slot 占用）
```

关键区别：普通路径要写池、要 ACK、要清理 slot；直接 IPC 路径把 handle 直接塞进控制消息，接收方 `pickle.loads` 即可拿到同一块显存的本地视图——零拷贝、零池占用。代价是它只能在**同进程或同 GPU**时用（IPC handle 不能跨节点）。

#### 4.4.3 源码精读

先看普通发送侧的打包 `_pack_tensors`——注意它做了 **dtype 对齐填充**，保证每个 tensor 起始地址满足其 dtype 的对齐要求：

```python
def _pack_tensors(tensors, *, device):
    target_device = torch.device(device)
    entries, chunks, offset = [], [], 0
    for path, tensor in tensors.items():
        flat = tensor.contiguous().view(torch.uint8).reshape(-1)
        if flat.device != target_device:
            flat = flat.to(device=target_device)
        padding = _pad_offset(offset, _dtype_alignment(tensor.dtype))
        if padding:
            chunks.append(torch.zeros(padding, dtype=torch.uint8, device=target_device))
            offset += padding
        chunks.append(flat)
        entries.append(TensorMeta(path=path, shape=..., dtype=..., device=..., offset=offset, size=...))
        offset += int(flat.numel())
    return torch.cat(chunks), entries
```

来源：[sglang_omni/comm/stage_io.py:535-564](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/comm/stage_io.py#L535-L564)

接收侧 `read_payload` 按元信息切片还原：

```python
tensors = {
    entry.path: _restore_tensor_device(
        transfer_buf[entry.offset : entry.offset + entry.size]
        .view(_torch_dtype(entry.dtype)).reshape(entry.shape),
        entry.device,
    )
    for entry in data_ref.tensors
}
relay.cleanup(request_id)
```

来源：[sglang_omni/comm/stage_io.py:337-347](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/comm/stage_io.py#L337-L347)

再看直接 CUDA IPC 的资格判定 `can_use_direct_cuda_ipc`——它在 router 构造时就预算好一个 frozenset：

```python
self._direct_cuda_ipc_targets = frozenset(
    name
    for name, gpu_ids in self.stage_gpu_ids.items()
    if self.gpu_id == self.placement_gpu_id            # 我在自己的放置 GPU 上
    and gpu_ids == (self.placement_gpu_id,)            # 目标也独占同一块 GPU
    and name not in self.same_process_targets          # 不是同进程（同进程走 LOCAL_OBJECT）
    and name not in self.remote_stage_names            # 不是跨节点
)
```

来源：[sglang_omni/comm/router.py:44-51](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/comm/router.py#L44-L51)

也就是说，「直接 CUDA IPC」只有在**两边住在同一块 GPU**（但不共进程）时才启用。它的序列化用 `ForkingPickler`（`multiprocessing` 的 IPC pickle，能正确序列化 CUDA tensor 的跨进程 handle）：

```python
def _ipc_pickle(obj: Any) -> bytes:
    if not _contains_cuda_tensor(obj):
        return pickle.dumps(obj)
    buf = io.BytesIO()
    ForkingPickler(buf, 2).dump(obj)
    return buf.getvalue()
```

来源：[sglang_omni/comm/stage_io.py:679-684](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/comm/stage_io.py#L679-L684)

最后，在 `Stage` 的发送路径里能看到三选一的完整决策：**同进程 → 本地直传；同 GPU 且含 CUDA tensor → 直接 IPC；否则 → relay**：

```python
can_use_direct_cuda_ipc = self._comm.router.can_use_direct_cuda_ipc(target)
if (
    not self._disable_direct_cuda_ipc_payload
    and can_use_direct_cuda_ipc
    and stage_io.payload_has_cuda_tensor(projected_payload)
):
    direct_ref = stage_io.serialize_direct_cuda_ipc_payload(projected_payload)
    await self.control_plane.send_to_stage(target, endpoint,
        DataReadyMessage(request_id=..., from_stage=..., to_stage=..., data_ref=direct_ref))
    return

# 否则走 relay
transport_kind, relay = self._comm.router.relay_for_payload(target, projected_payload)
await self._comm.send_payload(relay=relay, ...)
```

来源：[sglang_omni/pipeline/stage/runtime.py:1132-1176](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/pipeline/stage/runtime.py#L1132-L1176)

注意 `disable_direct_cuda_ipc_payload` 这个开关——某些模型（如 Qwen3-Omni 的 `mm_aggregate → thinker` 提前提交）会显式关掉直接 IPC，强制走 relay，因为那条边需要 relay 的 ACK/生命周期语义来配合「partial-start」重叠（见 u5-l2）。这是一个「旁路虽快，但不是所有边都该用」的有意设计点。

#### 4.4.4 代码实践

**实践目标**：用官方测试验证「直接 CUDA IPC」的资格判定边界。

**操作步骤**（源码阅读 + 部分可运行）：

1. 阅读 [tests/unit_test/pipeline/test_comm_router.py:74-107](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/tests/unit_test/pipeline/test_comm_router.py#L74-L107)，里面有四个目标：`same_code2wav`（同 GPU，应通过）、`cross_code2wav`（目标在另一块 GPU，应拒绝）、`tp_decode`（目标是 TP 阶段、跨多 GPU，应拒绝）、`local_code2wav`（同进程，应拒绝——它走 LOCAL_OBJECT）。
2. 把该测试的 `CommRouter(...)` 构造参数抄到一个 REPL 里，**去掉**需要 GPU 的断言，仅打印四个 `can_use_direct_cuda_ipc(...)` 的结果：

```python
# 示例代码：无需 GPU，can_use_direct_cuda_ipc 是纯局部性判断
from sglang_omni.comm.router import CommRouter
r = CommRouter(
    stage_name="talker_ar", gpu_id=1, placement_gpu_id=1,
    same_process_targets={"local_code2wav"},
    gpu_stage_names={"same_code2wav", "cross_code2wav", "tp_decode"},
    stage_gpu_ids={"same_code2wav": (1,), "cross_code2wav": (0,),
                   "tp_decode": (0, 1), "local_code2wav": (1,)},
    comm_config={},
)
for t in ["same_code2wav", "cross_code2wav", "tp_decode", "local_code2wav"]:
    print(t, r.can_use_direct_cuda_ipc(t))
# 预期：same_code2wav True；其余三个 False
```

**需要观察的现象**：只有 `same_code2wav` 返回 `True`。

**预期结果**：与测试断言一致——直接 IPC 仅对「两边同住一块 GPU 且不共进程、不跨节点」的目标开放。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `_pack_tensors` 要做 dtype 对齐填充？

> **答案**：因为多个 tensor 被拼进同一段连续 buffer，接收侧要按 `offset` 把每个 tensor 切出来再 `.view(dtype)`。如果前一个 tensor 的结尾地址不满足后一个 tensor 的 dtype 对齐要求，`.view` 出来的 tensor 会因未对齐访问而出错或性能崩塌。`_pad_offset(offset, alignment)` 用 `(-offset) % alignment` 计算需要补的零字节数。

**练习 2**：直接 CUDA IPC 旁路比普通 `CUDA_IPC` relay 路径省了什么？

> **答案**：省了「写池 + 占 slot + 等 ACK + 清理 slot」这一整套。直接 IPC 把 tensor 的 IPC handle 直接内嵌进控制消息，接收方 `pickle.loads` 即拿到同一块显存的本地视图；没有发送池资源要管理，所以不需要 ACK 与 slot 释放。代价是只能在同 GPU（IPC handle 不能跨节点）且不共进程时用。

---

## 5. 综合实践

**任务**：给定三种 stage 边，分别写出 `CommRouter` 会选择的传输，并说明理由。然后用一段可运行的 Python 把你的答案验证一遍（路由决策部分无需 GPU）。

设定一个 `thinker` stage（`gpu_id=0`），它有三条出边：

| 边 | 目标 stage 的物理位置 |
|---|---|
| A：`thinker → local_aggregate` | 与 `thinker` 共享同一 OS 进程 |
| B：`thinker → talker_ar` | 同节点，但住在另一块 GPU（`gpu_id=1`） |
| C：`thinker → remote_code2wav` | 跨节点（另一台机器） |

**请先作答**（不看下面）：

- A 走什么传输？
- B 走什么传输？（追问：B 上的 payload 全是 CPU 张量时呢？）
- C 走什么传输？

**参考答案与验证脚本**：

```python
# 示例代码：验证综合实践（路由决策无需 GPU）
from sglang_omni.comm.router import CommRouter
from sglang_omni.comm.data_ref import TransportKind

r = CommRouter(
    stage_name="thinker", gpu_id=0,
    same_process_targets={"local_aggregate"},      # A 边
    gpu_stage_names={"talker_ar"},                 # B 边：同节点 GPU 阶段
    remote_stage_names={"remote_code2wav"},        # C 边：跨节点
    comm_config={},
)

print("A:", r.outbound("local_aggregate"))   # LOCAL_OBJECT（同进程，引用直传，无 relay）
print("B:", r.outbound("talker_ar"))         # CUDA_IPC（同节点 + 双方 GPU）
print("B(cpu payload):", r.outbound_payload("talker_ar", {"ids": __import__("torch").empty(1)}))  # SHM（payload 全 CPU → 务实降级）
print("C:", r.outbound("remote_code2wav"))   # MOONCAKE（跨节点）
```

**要点解释**：

- **A**：`target ∈ same_process_targets` → `LOCAL_OBJECT`。零拷贝引用传递，接收方须只读。理由：同进程，没必要拷贝。
- **B**：`self_is_gpu and target ∈ gpu_stage_names` → `CUDA_IPC`。但若该次 payload 全是 CPU 张量，`outbound_payload` 会务实降级到 `SHM`——**边的局部性只决定「可能」，具体载荷才决定「现实」**。这是本讲最易被忽略的细节。
- **C**：`target ∈ remote_stage_names` → `MOONCAKE`。理由：跨节点只能走网络（RDMA）。

> 若你想把 B 的 `CUDA_IPC` 真正「发送」一次（走完 engine + relay），需要真实 GPU 与启动好的控制平面，**待本地验证（需 GPU）**；但仅验证「选哪种传输」的本任务，上面这段纯 CPU 脚本即可跑通。

## 6. 本讲小结

- SGLang-Omni 把跨 stage 搬运抽象成 **四种传输**：`LOCAL_OBJECT`（同进程引用直传）、`CUDA_IPC`（同节点 GPU↔GPU，走 relay 池）、`SHM`（CPU 共享内存）、`MOONCAKE`（跨节点网络）。
- **`CommRouter` 只凭局部性做路由决策**：同进程 → LOCAL_OBJECT；跨节点 → MOONCAKE；同节点且双方都是 GPU → CUDA_IPC；兜底 → SHM。它**不**定义 stage 协议、**不**向 stage 暴露 Mooncake 专属 handle。
- `outbound` 按「边」选，`outbound_payload` / `outbound_stream` **按实际载荷**选——全 CPU 张量会被务实降级到 SHM，CUDA 张量发给非 GPU 目标会直接报错。
- **`LOCAL_OBJECT` 是引用传递**：零拷贝、无 relay、无 ACK；接收方必须**只读**，发送方须保证对象存活；只有「同进程且双方都非 TP」的边才够资格。
- **`CommEngine` 负责字节搬运**：按目标分队列串行发送；执行「写 relay → 发控制消息 → arm 等 ACK」三段式。其中**控制消息必须在 `wait_for_completion` 之前发出**（control-before-wait），否则基于信用的后端（NIXL/Mooncake）会死锁。
- **`StageIO` 负责对象↔`DataRef` 的打包解包**（含 dtype 对齐填充），并提供「**直接 CUDA IPC**」旁路：同 GPU 时把 IPC handle 内嵌进控制消息、绕过 relay 池，但可被 `disable_direct_cuda_ipc_payload` 关闭。

## 7. 下一步学习建议

- **回到 relay 后端实现**：本讲的传输是「选」出来的，具体每种后端怎么 `put_async` / `get_async`、怎么用 `CreditAllocator` 管 slot，请重读 u3-l3（Relay 数据平面）的 CUDA IPC 池部分，你会把「选择」和「执行」两侧彻底打通。
- **看真实模型的传输配置**：阅读 [sglang_omni/models/qwen3_omni/config.py](https://github.com/sgl-project/sglang-omni/blob/cf61f234e5a2cc7f63a24bdef84b446bd9a42f74/sglang_omni/models/qwen3_omni/config.py) 里 `disable_direct_cuda_ipc_payload` 的使用场景（结合 u5-l2 的 thinker→talker 流式），理解「为什么有时要主动关掉最快的旁路」。
- **下一讲 u6-l2（量化、权值加载与校验）**：从「数据怎么搬」转向「权重怎么加载与校验」，进入另一个专家层主题。
- **若对跨节点感兴趣**：可以跳读 `sglang_omni/relay/mooncake.py` 与 `nixl.py`，对照本讲的 control-before-wait 结论，验证「信用归还依赖接收方消费」这一不变量在后端里是如何落地的。
