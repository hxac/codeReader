# ZMQ 协议:PS 与 worker 的消息状态机

## 1. 本讲目标

学完本讲,你应该能够:

1. 说出 `_bind_zmq_socket` 生成的地址为什么长成 `ipc://@checkpoint-engine-<设备UUID>-<计数器>.sock`,以及「抽象 Unix domain socket」和「设备 UUID 寻址」分别解决什么问题。
2. 按顺序默写 Broadcast 更新中 PS(REQ)与 worker(REP)之间每一条消息的 payload 类型:IPC 句柄、`b""`、named tensor 列表、错误文本、`None`、`Exception`。
3. 解释 worker 侧 `update_weights_from_ipc` 的四类分支(`list` / `Exception` / 第一个 `None` / 第二个 `None`)与 `released` 标志构成的状态机。
4. 解释双方如何用两条 `None` 消息协调「释放 IPC 资源」与「执行 post_hook」两个收尾动作,以及错误发生时 ret_code 约减 + `RuntimeError` 回传的传播链。

本讲是 u3-l4(广播主流程)与 u4-l1(worker 状态机)的「合龙」:前两讲分别站在 PS 与 worker 一侧看流水线,本讲把两者放到同一条消息线上,逐条对齐。

## 2. 前置知识

### 2.1 ZeroMQ 是什么

ZeroMQ(pyzmq 的 `import zmq`)不是一个消息队列服务器,而是一套**嵌入进程内的异步 socket 库**。它比裸 socket 多给两样东西:

- **消息边界**:按「消息」收发而不是字节流,`send_pyobj(x)` 会先 pickle 再发,`recv_pyobj()` 收到后自动反 pickle。因此协议里的 payload 可以是任意可 pickle 的 Python 对象:元组、dict、`list[dict]`、`None`、甚至一个 `RuntimeError` 实例。
- **socket 类型语义**:本讲只用最简单的一对——`zmq.REQ`(请求方)与 `zmq.REP`(应答方)。

### 2.2 REQ/REP 的严格交替

REQ socket 的合法操作序列是 `send → recv → send → recv → …`,REP 是 `recv → send → recv → send → …`。**谁先谁后、一步都不能乱**:如果 REQ 连续 `send` 两次而没有中间的 `recv`,pyzmq 会立刻抛出 `zmq.error.ZMQError: Operation cannot be accomplished in current state`(常称 EFSM 错误)。

这条死板的规定恰好是本协议的骨架:PS 与 worker 的每一次交互都被强制成一问一答,任何一侧想多发或少发一条消息都会当场报错,而不是悄悄错位。后面会看到,源码里几处看似奇怪的 `socket.recv()` 正是为了在错误路径上维持交替。

### 2.3 `ipc://@` 与抽象 Unix domain socket

`ipc://` 传输在 Linux 上底层就是 Unix domain socket(UDS)。普通 UDS 地址对应文件系统里的一个路径(如 `ipc:///tmp/foo.sock`,bind 时会真的创建这个文件);而以 `@` 开头的地址使用 Linux 的**抽象命名空间**(abstract namespace):地址只是一个内核里的字符串,**不创建任何文件**,socket 关闭时内核自动回收。

好处对本场景很实际:

- 不需要在容器/共享目录里创建和清理 `.sock` 文件,不存在残留文件导致地址被占的问题;
- 不受文件系统权限、挂载传播的影响(训练与推理引擎进程的挂载命名空间可能不同);
- 每一轮更新用新计数器生成新名字,天然避开上一轮尚未关闭的同名 socket。

注意抽象名同样受内核 `sun_path`(108 字节)长度约束,`checkpoint-engine-<uuid>-<N>.sock` 这种长度是安全的。

### 2.4 设备 UUID:PS 与 worker 的共同钥匙(回顾)

u3-l1 讲过 `_get_physical_gpu_id`:PS 侧每个 rank 在初始化时取得本机的物理设备标识——CUDA/XPU 是 `GPU-<uuid>`,NPU 是 `NPU-<npu-smi 反查的 uuid>`。推理引擎侧 worker 也用完全相同的格式生成自己的 `_device_uuid`。本讲的地址寻址完全建立在这把钥匙上:**worker 拿到一份「设备 UUID → ZMQ 地址」的清单后,只取属于自己那张 GPU 的那条**。

## 3. 本讲源码地图

| 文件 | 本讲关注的片段 | 作用 |
| --- | --- | --- |
| [checkpoint_engine/ps.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py) | `_bind_zmq_socket`、`_to_named_tensor`、`_update_per_bucket` 中所有 `socket.send/recv`、`_detect_bucket_size` 中的计数器同步 | REQ 侧:地址生成、消息发送/接收、错误传播 |
| [checkpoint_engine/worker.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py) | `update_weights_from_ipc`(REP 循环)、`_extract_weights`、`_ipc_handler_for_handle`、`VllmColocateWorkerExtension._device_uuid` | REP 侧:连接、attach、四类分支状态机 |
| [checkpoint_engine/ipc_handler.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ipc_handler.py) | `TorchIPCHandler.export`、`XpuIPCHandler.export` | 第一条消息(IPC 句柄)的两种线上格式 |
| [examples/update.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py) | `req_inference` 返回的 `req_func` | 地址清单如何经 HTTP 控制面送到推理引擎 |
| [tests/test_update.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_update.py) | `checker_proc_with_error` | 错误注入测试,验证消息语义的依据 |

## 4. 核心概念与源码讲解

### 4.1 `_bind_zmq_socket`:抽象 UDS 地址与设备 UUID 寻址

#### 4.1.1 概念说明

PS 与 worker 是**同一台机器上的两个进程**(colocated 部署),之间要传两类东西:

- **控制消息**(很小的指令与元数据)→ 走本讲的 ZMQ/UDS;
- **权重数据**(几十 GB 的显存内容)→ 不经过 ZMQ,走 u4-l3/u4-l4 讲的跨进程设备 IPC(零拷贝)。

所以 ZMQ 通道上从来没有张量本体,只有「指路牌」:IPC 句柄告诉 worker 去映射哪块显存,张量清单告诉 worker 这块显存里每个参数的形状与偏移。

`_bind_zmq_socket` 解决的问题是:**worker 进程怎么找到 PS 进程?** 答案是给每张 GPU 一个确定的名字——`checkpoint-engine-<本机设备UUID>-<轮次计数器>.sock`。PS(REQ 方)bind 这个名字,worker(REP 方)按自己的设备 UUID 从清单里查到这个名字再 connect。

#### 4.1.2 核心流程

```text
__init__:  _zmq_ctx = zmq.Context(); _zmq_addr_counter = 0
gather_metas(首次):  _global_device_uuids = [rank0 的 uuid, rank1 的 uuid, ...]   # 只在第一次填充
每次 update:
  _detect_bucket_size:  用一次 all_reduce(MIN) 把所有 rank 的计数器对齐到全局最大值
  _bind_zmq_socket:
      addr(uuid) = "ipc://@checkpoint-engine-{uuid}-{counter}.sock"
      socket_paths = [(uuid_0, addr(uuid_0)), ..., (uuid_{W-1}, addr(uuid_{W-1}))]
      socket = REQ; socket.bind(addr(自己的 uuid)); counter += 1
      return socket, socket_paths
```

计数器全局对齐的公式:

\[ \text{counter}_{\text{round}} = \max_{r \in \text{group}} \text{counter}_r = -\min_r(-\text{counter}_r) \]

因为 `all_reduce` 已经要为显存探测做一次 MIN,就把计数器取负捎带在同一辆车上,一次约减同时得到「最小空闲显存」和「最大计数器」。

#### 4.1.3 源码精读

构造函数里初始化上下文与计数器([checkpoint_engine/ps.py:221-222](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L221-L222)):每个进程一个 `zmq.Context`,`_zmq_addr_counter` 从 0 起步,每 bind 一次加一。

全集群的设备 UUID 清单在 `gather_metas` 里收集,且只在第一次 gather 时填充([checkpoint_engine/ps.py:500-519](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L500-L519)):`if not self._global_device_uuids: global_device_uuids.append(...)`——设备拓扑在一轮 RL 训练里不变,收集一次即可,之后每轮复用这份名单拼地址。

地址生成与 bind([checkpoint_engine/ps.py:622-630](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L622-L630)):

```python
def _bind_zmq_socket(self) -> tuple[zmq.Socket, list[tuple[str, str]]]:
    def zmq_handle(device_uuid: str) -> str:
        return f"ipc://@checkpoint-engine-{device_uuid}-{self._zmq_addr_counter}.sock"

    socket_paths = [(uid, zmq_handle(uid)) for uid in self._global_device_uuids]
    socket = self._zmq_ctx.socket(zmq.REQ)
    socket.bind(zmq_handle(self._device_uuid))
    self._zmq_addr_counter += 1
    return socket, socket_paths
```

这段代码做了三件事:① 为**所有**设备的 UUID 各算一个地址,组成 `(uuid, 地址)` 清单 `socket_paths`(它将被整体交给 `req_func` 带出去);② 自己只 bind 本机设备对应的那一个;③ 计数器加一,保证下一轮更新用新名字。

计数器的全局同步藏在 `_detect_bucket_size`([checkpoint_engine/ps.py:638-655](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L638-L655))。张量的第二个元素放 `-self._zmq_addr_counter`,注释写明「用负数复用同一次 allreduce 的 min 操作,顺便拿到所有 rank 中最大的 zmq_addr_counter」;约减后 `self._zmq_addr_counter = -tensor[1].item()` 把本 rank 的计数器拉齐到全局最大值,**发生在** `_bind_zmq_socket` 之前(调用点在 [checkpoint_engine/ps.py:804](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L804))。

为什么必须全局对齐?关键在于 `socket_paths` 清单的「生产者」和「消费者」不是同一个进程:每个 rank 的 PS 都 bind 自己的地址(用**自己的**计数器),而发给推理引擎的清单可能由**组内某个 rank**(通常不是本机那个)拼出来。如果各 rank 计数器不一致,清单里 rank 5 设备的地址就会和 rank 5 实际 bind 的地址差一个轮次,worker connect 一个没人监听的名字而永久阻塞。取 max(而不是求和或取 min)还有一个用意:P2P 更新里没参与的 rank 会提前 return、计数器落后,下一次全员广播时 max 会把它们追平。

worker 侧的 UUID 必须与 PS 完全同格式。`VllmColocateWorkerExtension._device_uuid`([checkpoint_engine/worker.py:150-162](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L150-L162))按 cuda/npu/xpu 三种平台分别生成,其中 XPU 分支的注释直接点明契约:「Must match ps.py::_get_physical_gpu_id ("GPU-<uuid>") for the ZMQ key to resolve」——两边的字符串只要差一个字符,worker 就会在 `zmq_handles[self._device_uuid]` 处 KeyError,连接根本建立不起来。

#### 4.1.4 代码实践:亲眼看到「抽象地址不留文件」

实践目标:验证 `ipc://@name` 走抽象命名空间、不在文件系统留下 `.sock` 文件,并与普通 `ipc:///tmp/...` 对比。

操作步骤(以下为示例代码,需本地安装 pyzmq:`pip install pyzmq`):

```python
# 示例代码:观察抽象 UDS 与文件路径 UDS 的区别
import zmq

ctx = zmq.Context()
s_abstract = ctx.socket(zmq.REQ)
s_abstract.bind("ipc://@checkpoint-engine-demo-0.sock")   # 抽象地址
s_file = ctx.socket(zmq.REQ)
s_file.bind("ipc:///tmp/zmq-demo-file.sock")              # 文件路径地址
input("两个 socket 都已 bind,按回车退出…")
```

运行后在另一个终端执行:

```bash
ls -l /tmp/zmq-demo-file.sock
ls -l /tmp/checkpoint-engine-demo-0.sock 2>&1   # 预期:No such file or directory
ss -x | grep -i checkpoint-engine               # 可选:内核里能看到抽象 socket(@ 前缀)
```

需要观察的现象:`/tmp/zmq-demo-file.sock` 存在(普通 UDS 会创建文件),而 `checkpoint-engine-demo-0.sock` 在任何目录都找不到;`ss -x`(列出 UDS)中抽象地址以 `@` 前缀出现。脚本退出后文件路径的 socket 文件仍留在 `/tmp`(需要手动清理),抽象地址则随进程消失。

预期结果:以上行为待本地验证;若你的环境 `ss` 无权限看不到抽象条目,只验证「没有生成文件」即可。

#### 4.1.5 小练习与答案

**练习 1**:为什么 PS 用 REQ socket 却调用 `bind`,而 worker 用 REP 却调用 `connect`?这和「服务端 bind、客户端 connect」的直觉相反。

答案:ZMQ 中 bind/connect 与 socket 类型是正交的,谁 bind 谁取决于**谁拥有确定的名字**。PS 能在本地算出确定地址(设备 UUID + 全局同步的计数器),所以由 PS bind;worker 只有在运行时收到清单后才知道地址,所以只能 connect。

**练习 2**:如果把 `_detect_bucket_size` 里同步计数器的逻辑删掉,哪种更新方式最先出问题?

答案:P2P 与 Broadcast 混用的多轮场景最先出问题:P2P 轮次中未参与的 rank 提前 return、计数器不增长,后续某轮由组首 rank 拼出的 `socket_paths` 中,其他设备的地址轮次与它们实际 bind 的不一致,worker 会 connect 到无人监听的抽象地址而阻塞。纯单轮、全员参与且计数器天然一致的场面则暂时看不出异常。

**练习 3**:为什么每轮更新都要换一个新地址(计数器加一),而不是复用同一个地址?

答案:上一轮的 socket 可能尚未完全关闭(worker 还在 finally 清理、或异常路径残留),对同一个名字重复 bind 会冲突;换新名字让每轮通信在全新的地址上进行,旧连接的清理与新连接的建立互不干扰,也避免了抽象命名空间里的名字占用问题。

### 4.2 `req_func`:把地址清单送到 worker 手里

#### 4.2.1 概念说明

`_bind_zmq_socket` 只解决了「地址叫什么」,还差一步:worker 进程(推理引擎)怎么**拿到**这份清单?训练侧进程和推理引擎之间没有直接的初始化握手,唯一的通路是推理引擎暴露的 HTTP API。`req_func` 就是 `update()` 的调用方注入的「送信人」回调:PS 把 `socket_paths` 交给它,它负责触发推理引擎去调用 `update_weights_from_ipc`。

这解释了 u1-l2 里的架构说法:**控制面走 HTTP,数据面走 ZMQ + 设备 IPC**。HTTP 只送几十字节的地址清单,ZMQ 只送元数据,权重本体始终零拷贝。

#### 4.2.2 核心流程

```text
_update_per_bucket(每个 rank 都会执行):
  socket, socket_paths = _bind_zmq_socket()
  req_thread = Thread(target=req_func, args=(socket_paths,))   # 放到子线程,主线程继续跑广播
  req_thread.start()
  socket.send_pyobj(handle)                                    # 不等送信结果,直接开始发第一条消息

examples/update.py 的 req_func:
  src = rank // P * P            # P = inference_parallel_size,组首的全局 rank
  仅当 rank == src:
      POST {endpoint}/collective_rpc  body = dict(socket_paths[src : src+P])
      # 推理引擎收到后,对组内每个 worker 执行 collective_rpc("update_weights_from_ipc", handles)

worker 侧(VllmColocateWorkerExtension.update_weights_from_ipc):
  update_weights_from_ipc(ctx, zmq_handles[ self._device_uuid ], ...)   # 按自己的 UUID 取址并 connect
```

#### 4.2.3 源码精读

PS 侧在 `_update_per_bucket` 中把送信动作放到独立线程([checkpoint_engine/ps.py:842-849](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L842-L849)):bind 之后立刻启动 `req_thread` 并 `socket.send_pyobj(handle)` 发出第一条消息。REQ 已 bind 但对端尚未 connect 也没关系——ZMQ 会把消息暂存在发送队列里,等 worker connect 后送达。所以 PS 不必等 HTTP 送信返回,广播流水线与送信并行推进。收尾处 `req_thread.join()`([checkpoint_engine/ps.py:933-934](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L933-L934))确认送信线程结束。

`examples/update.py` 的 `req_inference` 是 `req_func` 的标准实现([examples/update.py:77-93](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L77-L93)):

```python
def req_func(socket_paths: list[tuple[str, str]]):
    if rank == src:
        request_inference_to_update(
            f"{endpoint}/collective_rpc",
            dict(socket_paths[src : src + inference_parallel_size]),
            uds=uds,
        )
```

两个要点:

1. **`if rank == src` 守卫**:每个 rank 的 PS 都会在自己的线程里调用一次 `req_func`,但只有组首(`src = rank // P * P`,即本推理实例的第一个 rank)真正发 HTTP 请求——否则同一轮更新会被请求多次,worker 的 REP 循环被重复拉起,协议直接错乱。
2. **`socket_paths[src : src+P]` 切片再转 dict**:清单里含全集群所有设备,只把属于本推理实例的那 P 条发出去。注意这些地址是组首 rank 用**自己的计数器**拼的,这正是 4.1 中「计数器必须全局对齐」的原因。

worker 侧按 UUID 取址并进入 REP 循环([checkpoint_engine/worker.py:225-231](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L225-L231)):`zmq_handles[self._device_uuid]` 一行完成了「本进程 ↔ 本 GPU ↔ 本 ZMQ 地址」的最终绑定,随后调用本讲 4.3 精读的同名模块级函数。

#### 4.2.4 代码实践:推演地址分发(源码阅读型)

实践目标:给定一组运行参数,手工推演 `req_func` 的行为,加深对「组首 + 切片 + dict 取址」三级机制的理解。

操作步骤:

1. 精读 [examples/update.py:77-93](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/examples/update.py#L77-L93),确认 `src` 的计算式与守卫的位置。
2. 假设 `torchrun --nproc_per_node 8` 启动训练侧(world_size=8),`--inference-parallel-size 4`,即 P=4;设备 UUID 依次为 `u0..u7`,当前为第 3 轮更新(所有 rank 计数器已对齐为 2,本轮 bind 后变 3)。
3. 对 rank=0、rank=3、rank=6 分别回答:是否发送 HTTP 请求?发送的 dict 内容是什么?

需要观察的现象(答案):只有 rank=0 与 rank=4 满足 `rank == src`(src 分别为 0、4)。rank=6 的 src=4,不发送;rank=0 发送 `{"u0": "ipc://@checkpoint-engine-u0-2.sock", "u1": "…-u1-2.sock", "u2": "…-u2-2.sock", "u3": "…-u3-2.sock"}`;rank=4 发送 u4..u7 的对应 dict。rank=3 虽然也 bind 了 `…-u3-2.sock`,但由 rank=0 的清单替它「广播」出去。

预期结果:8 个 PS 各 bind 一个地址,但整个集群只发出 2 个 HTTP 请求,每个请求携带 4 条地址;worker 只见属于自己 GPU 的那一条。以上为静态推演,可通过在 `req_func` 里临时加一行 `print(rank, dict(socket_paths[src:src+P]))`(本地实验,勿提交)来对照验证。

#### 4.2.5 小练习与答案

**练习 1**:为什么 `req_func` 在子线程里执行,而不是主线程直接调用?

答案:主线程要继续执行「发送 IPC 句柄 → 广播流水线」的关键路径。送信要经 HTTP 往返(等待推理引擎响应),若在主线程同步执行,所有 rank 的广播都要额外等一次网络往返;放到子线程后,送信与广播完全并行,且 `socket.send_pyobj(handle)` 依靠 ZMQ 的排队语义不依赖对端已 connect。

**练习 2**:`dict(socket_paths[...])` 中如果两个设备生成了相同的 UUID 字符串,会发生什么?

答案:dict 的键会碰撞,后一条覆盖前一条,其中一个 worker 将永远收不到自己的地址(其 PS bind 的地址无人 connect),该 worker 阻塞在 `recv` 上,整轮更新挂起。这正是 `_device_uuid` 必须是**物理设备**唯一标识(而非进程号、序号)的原因。

**练习 3**:PS 在 worker 还没 connect 时就 `send_pyobj(handle)`,消息会丢吗?

答案:不会。ZMQ 的 REQ socket 在 bind 后、对端 connect 前发出的消息会缓存在本地发送队列,连接建立后自动投递(这也是 ZMQ 与裸 socket 的重要差别)。代价是若对端永远不来,消息一直堆积,直到进程退出。

### 4.3 `update_weights_from_ipc`:worker 侧 REP 状态机

#### 4.3.1 概念说明

worker 侧的 `update_weights_from_ipc`(worker.py 的模块级函数,被 vLLM 扩展类包装)是 REP 方的完整协议实现。它可以拆成两段:

- **attach 阶段**(循环外,只执行一次):收 IPC 句柄 → 按句柄格式选 handler → attach 出共享显存 buffer → 回 `b""`。
- **状态机阶段**(循环内):反复 `recv_pyobj()`,按 payload 类型走四类分支,直到第二个 `None` 后 break。

源码里有一段珍贵的官方注释,把状态机总结成四行([checkpoint_engine/worker.py:78-82](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L78-L82)):

```text
+ receive tensor_metadata -> update_weights
+ receive Exception -> raise and stop
+ receive None first time -> release resources
+ receive None second time -> call post_hook and stop
```

#### 4.3.2 核心流程

```text
attach 阶段:
  socket = REP; socket.connect(zmq_handle)
  ipc_handle = recv_pyobj()
  ipc_handler = _ipc_handler_for_handle(ipc_handle)   # 按"线上格式"选 Torch 或 XPU handler
  buffer = ipc_handler.attach(ipc_handle, device_id)  # 映射 PS 的双缓冲显存,断言 dtype==uint8
  send(b"")                                           # 成功 ACK
  失败 → send_string(堆栈文本); recv(); raise        # recv 是为维持 REQ/REP 交替

状态机(released 初值 False):
  payload = recv_pyobj()
  ├─ released == True:  断言 payload is None → post_hook() → send(b"") → break   # 第二个 None
  ├─ payload is None:   释放:buffer=None; ipc_handler.detach(); gc; ipc_collect;
  │                     empty_cache → released=True → send(b"") → continue      # 第一个 None
  ├─ isinstance(payload, list):  weights = _extract_weights(payload, buffer)
  │                     → run(weights) → send(b"")                                # 每个桶一次
  │                     run 抛异常 → send_string(堆栈文本),不 raise,继续循环
  └─ isinstance(payload, Exception): raise payload                                # PS 强制退出信号
finally: close socket; detach; gc; empty_cache
```

#### 4.3.3 源码精读

**attach 阶段**([checkpoint_engine/worker.py:62-77](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L62-L77)):REP socket connect 到抽象地址后先收句柄。句柄选择器 `_ipc_handler_for_handle`([checkpoint_engine/worker.py:21-28](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L21-L28))按**线上格式**而非设备探测来分发:dict 且 `kind == "xpu_sycl"` 用 `XpuIPCHandler`,否则(元组)用 `TorchIPCHandler`。这两种句柄分别由 [checkpoint_engine/ipc_handler.py:62-72](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ipc_handler.py#L62-L72)(`reduce_tensor` 元组)与 [checkpoint_engine/ipc_handler.py:84-96](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ipc_handler.py#L84-L96)(带 `kind` 标签的 dict)产出——句柄自描述、自包含,所以 PS 侧一条 `send_pyobj(handle)` 就完成交接(见 [checkpoint_engine/ps.py:848-849](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L848-L849) 的注释)。

attach 失败分支([checkpoint_engine/worker.py:73-77](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L73-L77))值得逐行读:

```python
except Exception as e:
    msg = "".join(traceback.format_exception(type(e), e, e.__traceback__))
    socket.send_string(msg)
    socket.recv()  # wait for ack
    raise
```

`send_string(msg)` 把堆栈文本发给 PS;随后的 `socket.recv()` **并不是普通确认**,而是在消费 PS 收到错误后回发的下一条消息(即后文的 `RuntimeError` 实例)。如果这里不 recv 就 raise、关 socket,PS 侧 REQ 会因为对端消失而在后续收发中报错,错误信息反而丢失。这就是「用一条 recv 维持交替」的典型写法。

**状态机循环**([checkpoint_engine/worker.py:84-123](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L84-L123))。四类分支:

- **`list` 分支(还在线上更新)**([checkpoint_engine/worker.py:108-117](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L108-L117)):用 `_extract_weights` 从共享 buffer 切出权重,交给 `run`(vLLM 场景即 `model.load_weights`),成功回 `b""`。注意 except 分支里 worker **不 raise**,只 `send_string` 回传堆栈,并附注释点明动机:「Don't raise here. Because all workers should quit in the same way by receiving the exception from PS」——单点失败要升级成全体一致的退出,必须由 PS 广播裁决,worker 自己悄悄退出反而会让各进程状态分叉。
- **第一个 `None`(释放资源)**([checkpoint_engine/worker.py:94-107](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L94-L107)):`released=True`、`buffer=None` 丢弃对共享显存的引用、`ipc_handler.detach()` 释放 IPC 映射,再做一轮 gc/ipc_collect/empty_cache,回 `b""` 后 `continue`。
- **第二个 `None`(post_hook)**([checkpoint_engine/worker.py:87-93](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L87-L93)):进入下一轮循环时 `released` 已为 True,断言这次 payload 必须还是 `None`(「释放后不得再有任何数据消息」),执行 `post_hook()`(vLLM 场景是 `process_weights_after_loading`,如 FP8 重量化),回 `b""` 后 `break`。
- **`Exception` 分支(强制退出)**([checkpoint_engine/worker.py:118-121](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L118-L121)):`raise payload`,把 PS 下发的异常原样抛出。

`_extract_weights` 与 PS 侧 `_to_named_tensor` 是一对([checkpoint_engine/worker.py:31-51](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L31-L51) 与 [checkpoint_engine/ps.py:35-48](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L35-L48)):

- PS 侧把桶内每个 `ParameterMeta` 转成 `{name, dtype, shape, offset}` 四键 dict,`offset` 从传入的基址(双缓冲半区起点 `gidx % 2 * bucket_size`)起按 `aligned_size` 累加;
- worker 侧按 `size = dtype.itemsize * shape.numel()`(注意:**未对齐**的真实字节数,而不是 `aligned_size`)切 buffer,`view(dtype)` 再 `view(shape)` 还原张量;对齐槽位之间的空隙正是靠 dict 里的绝对 `offset` 跳过的;
- worker 还防御性地把 `list|tuple` 形状的 shape 重新包成 `torch.Size`([checkpoint_engine/worker.py:43-46](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L43-L46)),因为 pickle 往返后 `torch.Size` 可能退化为普通 tuple。

`finally` 块([checkpoint_engine/worker.py:125-131](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L125-L131))保证任何退出路径(包括 `raise payload`)都会 close socket、detach IPC 并清缓存——协议异常也不能泄漏 IPC 映射。

#### 4.3.4 代码实践:在 CPU 上用真实函数跑通「张量清单」一跳

实践目标:不依赖 GPU 与 vLLM,用仓库里**真实的** `_to_named_tensor`、`_extract_weights` 两个函数,配合 `ipc://@` 通道,复现协议中「PS 发清单 → worker 切张量 → 回 ACK」一跳,并验证数值无损。

说明:以下为示例代码(非项目自带脚本),仓库根目录下的 `checkpoint_engine` 可直接 import(p2p_store 的 mooncake 依赖是延迟导入,CPU 环境无需安装)。需 `pip install pyzmq torch pydantic loguru`。

操作步骤:

```python
# 示例代码:zmq_protocol_lab.py —— CPU 上的协议最小实验
import threading
import torch, zmq
from checkpoint_engine.data_types import ParameterMeta
from checkpoint_engine.ps import _to_named_tensor          # PS 侧:metas -> payload
from checkpoint_engine.worker import _extract_weights     # worker 侧:payload -> weights

ADDR = "ipc://@checkpoint-engine-lab-0.sock"
ALIGN = 256

def make_buffer_and_payload():
    torch.manual_seed(0)
    t1 = torch.randn(12)              # float32, 48 字节
    t2 = torch.ones(3, 4, dtype=torch.bfloat16)  # 24 字节
    metas = [
        ParameterMeta(name="a", dtype=t1.dtype, shape=torch.Size(t1.shape),
                      aligned_size=(t1.numel() * 4 + ALIGN - 1) // ALIGN * ALIGN),
        ParameterMeta(name="b", dtype=t2.dtype, shape=torch.Size(t2.shape),
                      aligned_size=(t2.numel() * 2 + ALIGN - 1) // ALIGN * ALIGN),
    ]
    buffer = torch.zeros(metas[0].aligned_size + metas[1].aligned_size, dtype=torch.uint8)
    buffer[:48].copy_(t1.view(torch.uint8))                     # 按 256 对齐槽位摆放
    buffer[256:256 + 24].copy_(t2.view(torch.uint8).flatten())  # 第二个槽位从 256 开始
    return buffer, metas, t1, t2

BUFFER, METAS, T1, T2 = make_buffer_and_payload()

def worker():  # 模拟 worker.py 的 REP 侧(attach 阶段用假句柄代替真实 IPC)
    ctx = zmq.Context()
    sock = ctx.socket(zmq.REP); sock.connect(ADDR)
    handle = sock.recv_pyobj(); print("W <- IPC handle:", handle); sock.send(b"")
    released = False
    while True:
        payload = sock.recv_pyobj()
        if released:
            assert payload is None
            print("W <- 第二个 None -> post_hook"); sock.send(b""); break
        if payload is None:
            released = True; print("W <- 第一个 None -> release"); sock.send(b""); continue
        if isinstance(payload, list):
            weights = _extract_weights(payload, BUFFER)     # ★ 真实生产函数
            assert torch.equal(weights[0][1], T1) and torch.equal(weights[1][1], T2)
            print("W <- list -> load_weights, 数值校验通过"); sock.send(b""); continue
        if isinstance(payload, Exception):
            raise payload

t = threading.Thread(target=worker); t.start()

ctx = zmq.Context()                                          # 模拟 PS 的 REQ 侧
sock = ctx.socket(zmq.REQ); sock.bind(ADDR)
sock.send_pyobj(("fake-ipc-handle",))                        # ① 句柄(真实为 reduce_tensor 元组)
assert sock.recv() == b""                                    # ② attach ACK
sock.send_pyobj(_to_named_tensor(METAS, offset=0))           # ③ 张量清单 ★ 真实生产函数
assert sock.recv() == b""                                    # ④ load ACK
sock.send_pyobj(None); assert sock.recv() == b""             # ⑤⑥ 释放
sock.send_pyobj(None); assert sock.recv() == b""             # ⑦⑧ post_hook
t.join(); print("协议八步全部按预期完成")
```

需要观察的现象:打印出的消息顺序恰为 ①句柄 ②ACK ③list ④ACK ⑤None ⑥ACK ⑦None ⑧ACK;`_to_named_tensor` 生成的 payload 里 `a.offset=0`、`b.offset=256`(对齐槽位起点,不是紧挨着 48);数值校验通过说明「绝对 offset + 未对齐 size」的切张量逻辑正确。

预期结果:输出如上,全流程无异常。本脚本未在本讲义编写环境中运行,具体打印待本地验证;若 `t1.view(torch.uint8)` 在旧版 torch 报错,可改为 `torch.frombuffer(t1.numpy(), dtype=torch.uint8)`。

#### 4.3.5 小练习与答案

**练习 1**:payload 里的 `offset` 为什么从 `gidx % 2 * bucket_size` 开始,而不是 0?

答案:worker attach 到的是**整个双缓冲**(大小 `bucket_size * 2`),偶数桶落在前半区、奇数桶落在后半区。`offset` 是相对整个 buffer 的绝对地址,所以第 gidx 个桶内第一个张量的 offset 必须加上半区基址 `gidx % 2 * bucket_size`(见 [checkpoint_engine/ps.py:904](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L904) 的调用方式)。

**练习 2**:`_extract_weights` 计算 size 用 `dtype.itemsize * shape.numel()`,如果改用 `aligned_size` 会怎样?

答案:会把对齐填充的空隙也切进张量,`view(shape)` 因元素数不匹配直接抛 RuntimeError。空隙必须靠 dict 中的 offset 跳过,而不是靠拉长张量吞掉。

**练习 3**:worker 的 `run()` 抛异常后为什么必须留在循环里继续 `recv`?

答案:PS 在 `resp != b""` 时会做 ret_code 约减,随后向 worker 发送 `RuntimeError("Some workers failed to update weights")` 并要求全体退出。若 worker 提前退出循环,这条消息无人接收,REQ/REP 交替被破坏;留在循环里才能在 `Exception` 分支统一 `raise payload`,让所有 worker 以完全相同的方式退出。

### 4.4 完整消息时序、两次 `None` 的资源协调与错误传播

#### 4.4.1 概念说明

把 4.1–4.3 的两侧拼起来,就得到协议全景。三个设计值得注意:

1. **交替即流水线**:REQ/REP 的一问一答恰好实现了 u3-l4 讲的「一桶深」流水线——PS 广播完第 i 桶后才去收第 i-1 桶的 ACK,worker 装载第 i-1 桶与 PS 广播第 i 桶并行。
2. **两次 `None` 分隔两段收尾**:释放 IPC 资源(显存紧张,越早越好)与执行 post_hook(可能做量化重排,须在权重全部就位后)被拆成两个独立阶段,两个 None 就是阶段分隔符,且两阶段的清理在 PS 与 worker 两侧并行进行。
3. **错误三级传播**:worker 本地错误文本 → PS ret_code 全体约减 → PS 向 worker 下发 `RuntimeError` 实例,保证「要么都成功,要么都退出」。

#### 4.4.2 核心流程:N 桶 Broadcast 更新的完整时序

| 步骤 | 方向 | payload 类型 | 语义 |
| --- | --- | --- | --- |
| 0 | PS bind / worker connect | — | `ipc://@checkpoint-engine-<uuid>-<n>.sock` |
| 1 | PS → W | 元组(CUDA/NPU)或 dict(XPU) | IPC 句柄:双缓冲显存的映射凭据 |
| 2 | W → PS | `b""` | attach 成功;失败则为堆栈文本 |
| 3 | *(每桶重复)* 广播第 i 桶后 PS 收上一条应答 | | |
| 3a | PS → W | `list[dict]`(name/dtype/shape/offset) | 第 i 桶张量清单(offset 含半区基址) |
| 3b | W → PS | `b""` / 堆栈文本 | `load_weights` 成功 / 失败 |
| 4 | PS → W | `None`(第一个) | 释放资源信号 |
| 5 | W → PS | `b""` | 已 detach IPC、清显存缓存 |
| 6 | PS → W | `None`(第二个) | 执行 post_hook 信号 |
| 7 | W → PS | `b""` | post_hook 完成,worker 退出循环 |

错误注入时插入一行:

| 步骤 | 方向 | payload 类型 | 语义 |
| --- | --- | --- | --- |
| E | PS → W | `RuntimeError` 实例 | 任一 rank 的 ret_code 非零,强制所有 worker 退出 |

注意步骤 3 的细节:PS 的循环体是「先 `dist.broadcast(buffer_b)`、再 `socket.recv()` 收**上一条**应答、再 `send` 本桶清单」。第一条收到的应答(步骤 2)对应句柄,最后一条清单的应答在循环结束后统一 `recv`([checkpoint_engine/ps.py:907](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L907))。

#### 4.4.3 源码精读

**循环体:广播、收 ACK、发清单**([checkpoint_engine/ps.py:886-905](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L886-L905)):

```python
dist.broadcast(buffer_b, src=receiver_rank, group=ranks_group)
resp = socket.recv()
if resp != b"":
    msg = resp.decode("utf-8")
    ...
    ret_code.fill_(1)
dist.all_reduce(ret_code, op=torch.distributed.ReduceOp.SUM, group=ranks_group)
...
if ret_code.item() != 0:
    socket.send_pyobj(RuntimeError("Some workers failed to update weights"))
    raise RuntimeError("Failed to update weights due to remote errors")
socket.send_pyobj(_to_named_tensor(bucket.items, gidx % 2 * bucket_size))
```

ACK 通道是纯字节:`b""` 即成功,非空字节串即 UTF-8 堆栈文本。本 rank 的 worker 出错只把 `ret_code` 置 1,是否退出要等 `all_reduce(SUM)` 后全体裁决——**任何一个** worker 失败都会让所有 rank 走进 `if ret_code.item() != 0`,向各自的 worker 发送同一个 `RuntimeError("Some workers failed to update weights")`(满足 REQ 的交替:上一条 `recv` 已完成,此时 send 合法),然后本进程 raise。worker 侧收到的正是这个实例,在 `Exception` 分支 `raise payload`。错误注入测试 [tests/test_update.py:72-86](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_update.py#L72-L86) 断言 worker 进程抛出的消息就是这句话,可作为协议语义的权威佐证。

**两次 `None` 之间的并行清理**([checkpoint_engine/ps.py:907-932](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L907-L932)):

```python
socket.recv()                       # 最后一个桶的 ACK
...
socket.send_pyobj(None)             # 第一个 None:通知 worker 释放句柄
socket.recv()
# 按"先视图后基张量"的顺序置 None,再 gc / ipc_collect / empty_cache
del buffer_b, h2d_buffer, buffer, handle
...
socket.send_pyobj(None)             # 第二个 None:通知 worker 执行 post_hook
socket.recv()
```

PS 在两次 `None` 之间做**自己这一侧**的清理(注释「Set to None in correct order (views first, then base tensors)」,即先删视图 `buffer_b` 再删基张量,避免视图持有的引用让基张量释放不掉);与此同时 worker 在第一个 `None` 后做**它那一侧**的 detach 与缓存回收(4.3.3 的 None 分支)。两侧清理不必互相等待,只需在下一个同步点(`store_based_barrier` / `dist.barrier`)之前完成即可。post_hook 放在最后一步,是因为它必须在「权重全部装载且 IPC 资源已释放」之后执行。

**收尾的固定次序**([checkpoint_engine/ps.py:933-940](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L933-L940)):`req_thread.join()`(送信线程结束)→ `dist.barrier`(全组对齐,确保没有 rank 还在用显存)→ `socket.close()` → P2P 场景注销 `__ipc_buffer__`。先 barrier 后关 socket 的顺序保证「所有人都用完」再拆通道。

#### 4.4.4 代码实践:亲手触发 EFSM 错误与错误传播链

实践目标:用两个小实验验证 REQ/REP 交替的强制约束,以及「文本 → RuntimeError 实例」的错误传播链。

实验一(违反交替,示例代码):

```python
# 示例代码:连续两次 send 观察状态机错误
import zmq
ctx = zmq.Context()
s = ctx.socket(zmq.REQ); s.bind("ipc://@efsm-demo.sock")
s.send_pyobj(None)
s.send_pyobj(None)   # 预期:zmq.error.ZMQError: Operation cannot be accomplished in current state
```

操作步骤:运行上述脚本。需要观察的现象:第二条 send 抛 `zmq.error.ZMQError`,证明 REQ 不允许连续两次发送。预期结果待本地验证。

实验二(错误传播链,改造 4.3.4 的实验):把 worker 中 `list` 分支改为模拟 `load_weights` 失败——`sock.send_string("RuntimeError: fake load failure")`;PS 侧在 `recv()` 得到非空应答后,按源码逻辑执行 `sock.send_pyobj(RuntimeError("Some workers failed to update weights"))`,worker 下一轮 `recv_pyobj()` 将收到该实例并在 `Exception` 分支抛出。

需要观察的现象:worker 的最终异常消息是 `Some workers failed to update weights`(PS 下发的统一文本),而不是它自己的 `fake load failure`——本地错误文本只向上游(PS)报告,退出指令永远由 PS 统一下发。可对照 [tests/test_update.py:84-85](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_update.py#L84-L85) 的断言。预期结果待本地验证。

#### 4.4.5 小练习与答案

**练习 1**:PS 在循环中先 `dist.broadcast` 再 `socket.recv()`,如果把两者对调,性能会发生什么变化?

答案:对调后 PS 必须等 worker 装载完第 i-1 桶并回 ACK 后才开始广播第 i 桶,「广播 i」与「装载 i-1」从并行变成串行,流水线退化,这正是 u3-l4 讲的三阶段重叠收益消失后的时序。

**练习 2**:为什么第一个 `None` 之后 worker 还要 `continue` 回到循环,而不是直接 break?

答案:协议还有第二个阶段(post_hook)。`released` 标志 + `continue` 让同一个循环处理两段收尾:第一个 None 置位后,循环还守着 socket 等第二个 None;同时 `assert payload is None` 保证释放后不允许再出现数据消息,任何违规 payload 都会立刻暴露。

**练习 3**:若 PS 在发送第一个 `None` 后崩溃(没发第二个),worker 会怎样?finally 块能补救什么?

答案:worker 阻塞在 `recv_pyobj()` 等第二个 None,直到对端 socket 关闭后 pyzmq 的 recv 抛错/永久阻塞(未设 RCVTIMEO,进程可能挂住)。finally 块([checkpoint_engine/worker.py:125-131](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L125-L131))能保证的是:**一旦**以任何方式退出循环,IPC 映射与缓存一定被清理,不会泄漏;但它救不了「等不到消息」本身——这也是为什么超时保护要靠调用方(如 vLLM 的 RPC 超时)而不是本协议实现。

## 5. 综合实践:双桶协议全息实验

把 4.3.4 的单桶实验扩展成**双桶 + 双缓冲 + 错误开关**的完整协议实验,把本讲所有知识点串起来:

1. **实践目标**:在 CPU 上完整模拟 `_update_per_bucket` 的消息序列(2 个桶、`gidx % 2` 半区偏移、两次 None),并能一键切换错误注入,输出一份可与 4.4.2 时序表逐行对照的消息日志。
2. **操作步骤**(示例代码,在 4.3.4 基础上修改):
   - 构造 6 个张量、每桶 3 个;`bucket_size` 取所有 `aligned_size` 之和的一半再向上对齐到 256;分配 `2 * bucket_size` 的 uint8 buffer 模拟双缓冲;
   - PS 侧循环 `for gidx in range(2)`:`buffer_b = buffer[gidx % 2 * bucket_size : ...]`,把第 gidx 桶的张量拷入对应半区,`send_pyobj(_to_named_tensor(bucket_items[gidx], gidx % 2 * bucket_size))`,再 `recv()` ACK;worker 侧 `list` 分支用 `_extract_weights(payload, BUFFER)` 校验数值;
   - 每条 send/recv 前后打印 `[PS→W] list(len=3, base=offset)` / `[W→PS] b""` 式日志;
   - 加命令行开关 `--error-at 1`:worker 在处理第 1 桶时 `send_string("fake failure")`,PS 检测到非空应答后 `send_pyobj(RuntimeError("Some workers failed to update weights"))`,worker 捕获并打印最终异常。
3. **需要观察的现象**:
   - 正常路径:两桶清单的 offset 基址分别为 `0` 与 `bucket_size`(双缓冲半区),第 1 桶张量的 offset 全部落在大 `bucket_size` 一侧;
   - 错误路径:第 0 桶正常 ACK,第 1 桶收到文本应答,随后 worker 打印的异常是 PS 下发的统一文本,而非本地 `fake failure`;
   - 两种路径下日志条目都能与 4.4.2 时序表一一对应。
4. **预期结果**:正常路径八类消息齐全、数值校验通过;错误路径复现「文本上行、异常下行」的传播链。本实验未在本讲义编写环境中运行,具体输出待本地验证。

## 6. 本讲小结

- 地址是算出来的,不是商量出来的:`ipc://@checkpoint-engine-<设备UUID>-<轮次计数器>.sock` 走 Linux 抽象 UDS(无文件、免清理),PS(REQ)bind 确定名字,worker(REP)按自己的设备 UUID 从清单取址 connect。
- 计数器靠 `_detect_bucket_size` 里一次「负数捎带」的 all_reduce 全局取 max 对齐,因为清单由组首 rank 拼装、却要覆盖其他 rank bind 的地址。
- 控制面与数据面彻底分离:`req_func` 只经 HTTP 送 `(uuid, 地址)` 清单,权重本体永远走设备 IPC 零拷贝;协议线上只有句柄、元数据、信号和异常。
- 消息一共五类:IPC 句柄(自包含,一条消息完成交接)、`list[dict]` 张量清单(offset 含 `gidx % 2` 半区基址)、`b""`/堆栈文本的 ACK 通道、两个 `None`(先释放资源、后执行 post_hook)、`RuntimeError` 实例(强制退出)。
- worker 是一个 `released` 标志驱动的 REP 状态机:本地失败只回文本不退出,退出指令永远由 PS 经 ret_code 全体约减后统一下发,保证全集群同生共死。

## 7. 下一步学习建议

本讲之后,PS 与 worker 之间的协议细节已经闭环。建议:

1. 学习 u5-l2(distributed 抽象层)与 u5-l3(vLLM NCCL 后端),理解 `dist.broadcast`、`all_reduce` 这些在协议两侧充当「数据面裁决」的调用在无 torch 进程组的场景下如何实现。
2. 回读 [tests/test_update.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_update.py) 的 `checker_proc`,注意它如何用多进程 + `queue.put` 充当 `req_func`、用 `device_uuid` 取址——那是本讲协议在真实 GPU 上的可执行规格书。
3. 若你关心 XPU 路径,预习 u4-l4:`XpuIPCHandler` 的 dict 句柄(`kind="xpu_sycl"`)正是本讲第一条消息的另一种线上格式,`_ipc_handler_for_handle` 的按格式分发使 REP 侧无需感知设备类型。
