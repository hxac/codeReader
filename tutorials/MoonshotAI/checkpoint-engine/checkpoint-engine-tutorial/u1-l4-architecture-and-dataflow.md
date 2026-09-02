# 整体架构与三阶段数据流总览

## 1. 本讲目标

学完本讲,你应该能够:

1. **画出一次广播(Broadcast)权重更新的完整时序图**,包括 ParameterServer(下称 PS)与推理引擎 worker 之间的每一条 ZMQ 消息。
2. **在源码中精确指出三阶段流水线的位置**:H2D(锁页内存 → 显存)、broadcast(PS 各 rank 间广播)、reload(推理引擎装载权重)分别对应 `ps.py` 和 `worker.py` 的哪些行。
3. **说明显存不足时流水线退化为串行执行的条件**,以及为什么流水线模式需要 `3 × bucket_size` 的显存而串行模式只需要 `2 × bucket_size`。
4. 从宏观上理解 **P2P 更新方式为什么服务于"动态扩容"场景**,它与 Broadcast 共用哪一套代码骨架。

本讲是第一单元(入门)的最后一讲,目标是把前几讲认识的"文件地图"升级为"动态数据流图"。本讲只看宏观流程,函数内部的分布式细节(进程组、NCCL、RDMA)留给第三、五单元。

## 2. 前置知识

### 2.1 流水线与双缓冲:一个生活类比

想象一个洗衣房:洗衣机(阶段 1)、烘干机(阶段 2)、叠衣桌(阶段 3)。如果你洗完第一批才启动烘干机、烘干完才叠衣服,三个设备大部分时间都在闲置。**流水线(pipeline)** 的做法是:第一批衣服进入烘干机的同时,洗衣机开始洗第二批——三个阶段各自满负荷,总时间接近最慢那个阶段的耗时,而不是三个阶段耗时之和。

流水线需要一个前提:**每个阶段之间有独立的"暂存区"**。如果只有一个篮子,烘干机没法在叠衣员还没拿走上一批衣服时放入新一批。**双缓冲(double buffering)** 就是两个轮流使用的篮子:写入方写第 A 半区时,读取方还在读第 B 半区。checkpoint-engine 用 `2 × bucket_size` 的一块显存实现它。

### 2.2 H2D、锁页内存与异步拷贝

- **H2D(Host to Device)**:数据从主机内存(CPU 侧)复制到显存(GPU 侧),走 PCIe 总线,由 GPU 上的拷贝引擎(copy engine)执行,可以与计算内核并行。
- **锁页内存(pinned memory / page-locked memory)**:普通主机内存可能被操作系统换页,不能作为 DMA 传输源;锁页内存被固定在物理地址上,才能发起**异步** H2D 拷贝(对应 PyTorch 的 `copy_(..., non_blocking=True)`)。第 1 讲已经提到,注册 checkpoint 时权重会被放进锁页内存池。
- **`synchronize()`**:异步操作只是"提交"给硬件;调用同步函数才会真正等待硬件完成。理解"哪些调用只是提交、哪些调用会阻塞"是读懂本讲代码的关键。

### 2.3 集合通信中的 broadcast

`dist.broadcast(tensor, src=k)` 是 PyTorch 分布式的集合通信原语:rank `k` 把自己的一份 `tensor` 发给进程组内**所有** rank,所有人都得到相同副本。它由 NCCL/CCL 等后端实现,走 NVLink 或网卡,与 PCIe 上的 H2D 拷贝**使用不同的硬件通道**——这正是三阶段可以重叠的物理基础。

### 2.4 ZMQ 的 REQ/REP 模式

ZeroMQ 的 REQ(请求方)/REP(应答方)socket 遵循严格的一问一答:REQ 方 `send` 之后**必须**先 `recv` 才能再次 `send`,反之亦然。所以 PS 与 worker 之间的消息序列是一张可以逐条数出来的清单——本讲的时序图就是这样画出来的。

### 2.5 跨进程共享显存(IPC)

同一台机器上,PS 进程和推理引擎进程各自有独立的地址空间,但可以通过 CUDA IPC(Intel XPU 上是 SYCL `ipc_memory`)让一个进程把自己显存里的一块 buffer "导出"为可序列化句柄,另一个进程 `attach` 后**直接映射同一块显存**,读写零拷贝。第 1 讔提过:控制面走 HTTP/ZMQ,数据面走 IPC + 集合通信。细节在第 4 单元精讲。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注的段落 |
| --- | --- | --- |
| `README.md` | 架构的文字权威 | Architecture 与 Optimized Weight Broadcast 两节 |
| [checkpoint_engine/ps.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py) | 服务端:编排三阶段流水线 | `update`、`_update_per_bucket`、`_detect_bucket_size`、`_copy_to_buffer`、`_bind_zmq_socket`、`_to_named_tensor` |
| [checkpoint_engine/worker.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py) | 消费端:reload 状态机 | `update_weights_from_ipc`、`_extract_weights`、`VllmColocateWorkerExtension` |
| `checkpoint_engine/ipc_handler.py` | IPC 契约(本讲只做背景) | `IPCHandler.export/attach/detach` |
| `tests/test_update.py` | 端到端测试(worker 的"替身") | `checker_proc`、`checker_proc_with_error`、`run` |

一句话回顾第 3 讲的分工:`ps.py` 是总装车间,`worker.py` 是推理引擎侧的镜像,两者只通过 `data_types.py` 的元数据 + ZMQ 消息 + IPC 句柄对话。本讲就把这条对话完整走一遍。

## 4. 核心概念与源码讲解

### 4.1 三阶段流水线:H2D → broadcast → reload

#### 4.1.1 概念说明

问题:训练侧产生了新权重(在 CPU 锁页内存里),推理集群有几百张 GPU,每张 GPU 上的模型**切分方式**还可能与训练侧不同。如何把几 TB 数据在最短时间内铺满所有 GPU?

README 给出的答案是把它组织成三个阶段(对应 [README.md:L20-L28](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/README.md#L20-L28)):

1. **H2D**:把权重从锁页内存搬进 GPU 显存(这些权重可能来自磁盘,也可能直接来自训练引擎)。
2. **broadcast**:在 checkpoint-engine 的各 rank 之间广播,结果落在**一块与推理引擎共享的 IPC buffer** 里。
3. **reload**:推理引擎自己决定从广播数据里**取哪个子集**写入模型权重。

三个阶段用三块不同的硬件(H2D 用 PCIe 拷贝引擎、broadcast 用 NVLink/网卡、reload 用 GPU 核心与显存带宽),所以可以让它们像洗衣房一样同时开工——对不同桶(bucket)交错执行。README 用一张图描述了这个流水线(见 [README.md:L31-L35](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/README.md#L31-L35) 的 `figures/pipeline.png`),并特别说明:流水线天然需要更多显存,**显存不够时会回退到串行执行**([README.md:L37](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/README.md#L37))——这句话的源码依据在 4.2 节展开。

还有一个容易被忽略的架构要点:**广播的是"整桶数据",各 worker 只取自己需要的子集**。正因为 reload 阶段由推理引擎自己挑权重(README 第 25 行:"inference engine decides what subset of weights to copy from the broadcasted data"),训练侧与推理侧的切分方式可以完全不同——这就是"在不同 sharding pattern 之间搬运权重"的实现基础。

#### 4.1.2 核心流程

一次 Broadcast 更新(`ParameterServer.update` 且不传 `ranks`)的完整时序:

```text
PS 进程(每个 GPU 一个)                worker 进程(vLLM,同 GPU)
──────────────────────                ──────────────────────────
(update 之前:register_checkpoint 已把权重放进 CPU 锁页内存池;
 gather_metas 已收集全局元数据并算好桶大小。)

(1) 分配 buffer(2×bucket),export IPC 句柄
    ── ZMQ: send(ipc_handle) ─────────►  (2) attach → 映射同一块显存
(3) recv b"" ◄────────────────────────── (attach 成功应答)

对每个桶 gidx = 0, 1, 2, ...:
(4) [H2D] 本 rank 的下一桶: 锁页池 ──► h2d_buffer
         (non_blocking=True,异步提交)
(5) [D2D] 若本 rank 是该桶的接收者:
         h2d_buffer ──► buffer[gidx%2 半区]
(6) [broadcast] dist.broadcast(buffer 半区)
         ← 注意:这一步不等上一桶 reload 完成就提交
(7) recv b"" ◄────────────────────────── (对上一条元数据的应答,
                                          = 上一桶 reload 已完成)
(8) ── ZMQ: send(桶 gidx 的张量元数据) ─►  (9) [reload] _extract_weights
                                            + model.load_weights
                                            (从 buffer[gidx%2] 零拷贝切出)

全部桶完成后:
(10) recv b"" ◄────────────────────────── (最后一桶 reload 完成应答)
(11) ── ZMQ: send(None) ───────────────►  (12) 释放:buffer 置空、ipc detach、
                                             gc + empty_cache
(13) recv b"" ◄──────────────────────────
(14) ── ZMQ: send(None) ───────────────►  (15) post_hook: 权重后处理
(16) recv b"" ◄──────────────────────────
```

对应的主循环伪代码(只保留数据流):

```text
for 第 i 轮:
    异步 H2D: 我的第 i 桶 → h2d_buffer          # 阶段 1(流水线模式)
    for 每个 rank 的第 i 桶 bucket:
        半区 = buffer[(gidx % 2) * bucket_size : ...]
        若我是接收者: 半区 ← h2d_buffer          # 显存内 D2D 拷贝
        dist.broadcast(半区, src=接收者)         # 阶段 2
        等待 worker 对上一桶的应答                # ← 与阶段 3 重叠的关键
        发送本桶张量元数据 → worker 执行 reload   # 阶段 3
```

流水线的重叠来自两处:

- **broadcast 与 reload 重叠**:第 (6) 步在第 (7) 步之前提交——广播桶 `gidx+1` 写入半区 `(gidx+1)%2` 时,worker 还在从半区 `gidx%2` 里装载桶 `gidx`。双缓冲保证两者不踩同一块显存。
- **H2D 与 broadcast 重叠**:第 (4) 步是异步提交的,本 rank 为自己下一桶做 H2D 的同时,其他 rank 的桶正在广播。这也是 `h2d_buffer` 存在的意义:把"我的 H2D"提前到上一轮发起。

显存代价:流水线模式同时持有 `h2d_buffer`(1 份桶大小)+ IPC 双缓冲(2 份桶大小)= `3 × bucket_size`。

#### 4.1.3 源码精读

**入口:`update()` 方法负责进程组生命周期,真正的流水线在 `_update_per_bucket`。**

[checkpoint_engine/ps.py:L569-L620](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L569-L620) 是 `update()` 全文。它做四件事:必要时自动初始化进程组(L596-L597);若指定了 `ranks` 则创建子进程组(L599);在 `with build_ipc_handler(...)` 上下文中调用 `_update_per_bucket`(L602-L603),`with` 保证即使中途失败也会释放导出的 IPC 句柄;`finally` 里销毁进程组并清空显存缓存(L610-L615)。`update()` 的 docstring(L577-L592)明确写着:**本函数必须在 `gather_metas` 之后调用**,`ranks` 不传则走全量广播、传了则走 P2P。

**阶段 1(H2D)的源码位置:**

- [checkpoint_engine/ps.py:L856-L862](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L856-L862):主循环每轮开头,把自己的第 `i` 桶拷进 `h2d_buffer`(仅流水线模式,即 `not disable_h2d_buffer`)。
- [checkpoint_engine/ps.py:L704-L709](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L704-L709):`_copy_to_buffer` 的本地分支,从锁页内存池 `pool.buffer` 拷到目标 buffer,注意 **`non_blocking=True`**——这就是异步 H2D,锁页内存是它的前提。
- [checkpoint_engine/ps.py:L879-L887](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L879-L887):串行回退模式(见 4.2)没有 `h2d_buffer`,接收者把锁页池**直接** H2D 拷进 IPC buffer 的半区。

**阶段 2(broadcast)的源码位置:**

- [checkpoint_engine/ps.py:L876-L890](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L876-L890):先用 `start = gidx % 2 * bucket_size` 算出本桶落在双缓冲的哪一半,切出视图 `buffer_b`(L876-L877);接收者把自己 `h2d_buffer` 的内容 D2D 拷进该半区(L889);随后 [L890](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L890) 的 `dist.broadcast(buffer_b, src=receiver_rank, group=ranks_group)` 把数据铺到进程组内所有 rank 的同名半区。
- 广播模式下"谁负责哪桶"由 [checkpoint_engine/ps.py:L101-L103](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L101-L103) 决定:`ranks` 为空时,**每个 rank 既是自己那份数据的 owner,也是 receiver**(`(owner_rank, owner_rank, bucket)`)。所以每个 PS rank 都会广播自己分片、也接收别人的分片。

**阶段 3(reload)的源码位置:**

- PS 侧:广播完成后,PS 等到 worker 对**上一桶**的应答(L891),再把本桶的张量清单发过去——[checkpoint_engine/ps.py:L904](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L904) 的 `socket.send_pyobj(_to_named_tensor(bucket.items, gidx % 2 * bucket_size))`。`_to_named_tensor`([checkpoint_engine/ps.py:L35-L48](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L35-L48))把每个参数打包成 `{name, dtype, shape, offset}`,其中 `offset` 从 `gidx % 2 * bucket_size` 起累加**对齐后**的字节数——这就是 worker 切张量的"图纸"。
- worker 侧:收到的 `payload` 是 list 时(仍在更新),调用 [checkpoint_engine/worker.py:L108-L117](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L108-L117) 中的 `run(_extract_weights(payload, buffer))`。`_extract_weights`([checkpoint_engine/worker.py:L39-L51](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L39-L51))按 `offset` 在扁平 buffer 上 `buffer[offset:offset+size].view(dtype=dtype).view(shape)`——**零拷贝视图**,不搬数据。对 vLLM 来说,`run` 最终是 `self.model_runner.model.load_weights(weights)`([checkpoint_engine/worker.py:L204-L212](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L204-L212)),由模型自己挑需要的名字。

**一个小的阅读细节**:PS 在发送元数据之前调用了 `self.device_manager.device_module.synchronize()` 和 `ret_code.item()`(L898-L900),后者会隐式同步——保证广播数据真正落进共享显存后,worker 才会开始读它。

#### 4.1.4 代码实践

**实践目标**:不看本讲正文,独立完成"三阶段源码定位表",验证自己能把 README 的文字描述落到具体代码行。

**操作步骤**:

1. 打开 [README.md:L20-L28](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/README.md#L20-L28),读三遍三阶段描述。
2. 打开 `checkpoint_engine/ps.py`,只读 `_update_per_bucket`(L751 起)的主循环 L855-L905。
3. 在纸上画一张三列表格:阶段 / 关键代码行 / 关键调用,自己填写。
4. 再打开 `checkpoint_engine/worker.py` 的 L86-L117,补全 reload 阶段的 worker 侧行号。

**需要观察的现象**(填表时的自检问题):

- H2D 拷贝里哪个参数让它变成异步?(`non_blocking=True`,ps.py L708)
- broadcast 的目标张量是谁切出来的?(`buffer_b = buffer[start : start + bucket.size]`,ps.py L877)
- 元数据里的 `offset` 从几开始?(`gidx % 2 * bucket_size`,ps.py L904)

**预期结果**:与下表一致(行号基于当前 HEAD `d1de07b`):

| 阶段 | PS 侧 | worker 侧 |
| --- | --- | --- |
| H2D | ps.py L856-L862(流水线预取)/ L879-L887(串行直拷),底层 L704-L709 | — |
| broadcast | ps.py L876-L890(选半区 + D2D + `dist.broadcast`) | — |
| reload | ps.py L904(发送元数据,`_to_named_tensor` L35-L48) | worker.py L108-L117(收元数据调 `run`)+ L39-L51(`_extract_weights`)+ L204-L212(`load_weights`) |

本实践纯阅读,无需 GPU,无需安装任何东西(可直接在 GitHub 永久链接上完成)。

#### 4.1.5 小练习与答案

**练习 1**:为什么 IPC 共享的 `buffer` 要开 `2 × bucket_size`,而 `h2d_buffer` 只要 `1 × bucket_size`?

**答案**:`buffer` 是 PS(写入方)与 worker(读取方)共用的交接区,广播桶 `gidx+1` 写入半区 `(gidx+1)%2` 时,worker 可能还在从半区 `gidx%2` 装载桶 `gidx`,必须双缓冲才能让 broadcast 与 reload 重叠(见 ps.py L876 的 `gidx % 2` 交替、L890 的广播与 L904 的元数据发送顺序)。`h2d_buffer` 是 PS 进程内部的私有中转站,只有 PS 自己按轮次写入和读出,单份即可。

**练习 2**:worker 怎么知道某个权重张量在共享 buffer 的哪个位置、什么形状?

**答案**:PS 每广播完一个桶,就发送 `_to_named_tensor(bucket.items, gidx % 2 * bucket_size)` 生成的元数据列表(ps.py L904、L35-L48),每项含 `{name, dtype, shape, offset}`;worker 的 `_extract_weights`(worker.py L39-L51)按 `offset` 切片并用 `.view(dtype).view(shape)` 还原。注意 `offset` 是**相对整个 2 倍 buffer 的绝对偏移**,起点是本桶所在半区的起始地址。

**练习 3**:README 的 Limitations 里说"论文中提到的完美三阶段流水线目前尚未实现"([README.md:L227-L230](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/README.md#L227-L230)),结合本讲源码,你认为"已实现的部分"做到了什么?

**答案**:当前实现已经做到两组重叠——各 rank 的 H2D 异步预取与其他 rank 的广播重叠(ps.py L856-L862 的 `non_blocking` 拷贝 + L890 广播),以及桶 `gidx+1` 的广播与桶 `gidx` 的 reload 重叠(L890 广播在 L891 等待上一桶应答之前提交,双缓冲保证不冲突)。README 指出"完美"流水线适用于 H2D 与 broadcast 在 PCIe 上不冲突的架构,即三个阶段对**连续的桶**完全同时推进;这属于未来工作,本讲读者只需识别出当前代码中真实存在的两处重叠即可。

### 4.2 ParameterServer._update_per_bucket:流水线的编排者

#### 4.2.1 概念说明

`_update_per_bucket` 是整个项目的**心脏**:它决定桶多大、把全局元数据切成桶、分配三块角色不同的显存、驱动"H2D→广播→应答"主循环、聚合错误、并按正确顺序释放资源。Broadcast 和 P2P 两种更新方式共用这一个函数,靠 `ranks` 参数分流。

把它当成一个"施工队长"来理解:开工前先看材料清单(元数据)和仓库容量(显存),决定每车运多少(桶大小);然后安排三条传送带(h2d_buffer、buffer 双缓冲、ZMQ 控制信道)同时开工;任何工人(worker)报错就全队停工;收工时按"先视图、后底板"的顺序清场。

#### 4.2.2 核心流程

函数骨架([checkpoint_engine/ps.py:L751-L940](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L751-L940))按顺序做十件事:

1. **前置断言与能力守卫**(L759-L772):元数据非空、进程组已初始化、设备必须支持跨进程显存 IPC,否则直接报错(避免在更深处出现难懂的 `_share_fd_` 错误)。
2. **Broadcast / P2P 分流**(L774-L802):`ranks` 为空走广播;非空则要求设备支持 P2P、本 rank 必须在 `ranks` 里(`need_update`),不在的直接 `return`(L799-L800),在的先 `barrier` 再继续(L802)。
3. **探测桶大小**(L804,详见下文 `_detect_bucket_size`)。
4. **生成桶**(L805-L811):把全局参数元数据按 `bucket_size` 切成 `H2DBucket` 序列。
5. **分配 `h2d_buffer`**(L813-L817):串行模式下为 `None`。
6. **分配 `buffer`(2 倍桶大小)**(L824-L826):这就是与 worker 共享的 IPC 双缓冲。
7. **导出 IPC 句柄**(L827-L833):P2P 模式先把目标 buffer 注册进 p2p store;然后 `ipc_handler.export(buffer)` 得到可序列化句柄。
8. **建立 ZMQ 信道**(L842-L849):`_bind_zmq_socket` 绑定一个抽象 Unix domain socket 地址,把所有 `(设备UUID, 地址)` 列表交给后台线程执行 `req_func`(由调用方传入,通常是"通知推理引擎开始更新"的 HTTP 请求);PS 随即把 IPC 句柄发出去(L849)。
9. **主循环**(L855-L905):即 4.1 的三阶段流水线,外加错误聚合(见下)。
10. **收尾**(L907-L940):最后一次应答 → 发第一个 `None`(worker 释放资源)→ 按顺序 `del buffer_b, h2d_buffer, buffer, handle` 并 `gc.collect()` / `ipc_collect()` / `empty_cache()` → 发第二个 `None`(worker 执行 `post_hook`);`finally` 里 join 线程、`barrier`、关 socket、注销 p2p buffer。

**显存账本**(理解退化的钥匙):

| 显存块 | 大小 | 作用 | 何时存在 |
| --- | --- | --- | --- |
| `h2d_buffer` | 1 × bucket_size | H2D 预取中转站,让各 rank 的 H2D 并行提前 | 仅流水线模式 |
| `buffer` | 2 × bucket_size | 与 worker 共享的 IPC 双缓冲 | 恒有 |
| `ret_code` | 标量 | 跨 rank 聚合错误码 | 主循环期间 |

于是两种模式的显存下限是:

\[ \text{流水线}: 3 \times \text{bucket\_size} \qquad \text{串行}: 2 \times \text{bucket\_size} \]

**桶大小与退化条件**(`_detect_bucket_size`,[checkpoint_engine/ps.py:L632-L682](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L632-L682)):

1. 构造一个二元张量:各 rank 当前**空闲显存 × mem_fraction**(默认 0.9,可用 `PS_MEM_FRACTION` 改)与 `-zmq_addr_counter`,用一次 `all_reduce(MIN)`(L640-L655)同时取到"全集群最小空闲显存"和"各 rank ZMQ 计数器的最大值"(负数复用 MIN 约减,见 L647-L648 注释)。
2. 扫描全局元数据求**最大单张量**的对齐字节数 `max_tensor_bytes`(L656-L660)——桶必须装得下最大的单个张量。
3. 判定(L661-L678):
   - 若 \( \text{max\_tensor\_bytes} \le \lfloor \text{free}/3 \rfloor \):**流水线模式**,可用桶上限 = free/3,日志打印 `use h2d buffer`(L662-L666)。
   - 否则:**串行回退**,桶上限 = free/2,并要求 `max_tensor_bytes ≤ free/2`,不满足则断言失败(L667-L678);rank 0 日志打印 `disable h2d buffer ...`。
4. 最终桶大小(L679-L681):

\[ \text{bucket\_size} = \min\left(\max\left(\text{PS\_MAX\_BUCKET\_SIZE\_GB}\ (默认\ 8\text{GiB}),\ \text{max\_tensor\_bytes}\right),\ \text{free\_bytes}\right) \]

即"至少要装下最大张量,至多不超过(模式对应的)空闲显存份额,平时封顶 8 GiB(可用环境变量 `PS_MAX_BUCKET_SIZE_GB` 调整,见 [README.md:L179-L182](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/README.md#L179-L182))"。上式中的 free 都按 256 字节(`_ALIGN_SIZE`,定义于 [checkpoint_engine/pin_memory.py:L23](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/pin_memory.py#L23))向下对齐。

**串行回退后到底"串行"在哪**:主循环里 `h2d_buffer` 不存在,接收者在轮到自己广播之前,把锁页池**直接** H2D 拷进 IPC buffer 半区(L879-L887)。于是 H2D 不再提前预取、也无法与其他 rank 的广播重叠,只能贴着广播的临界路径执行——这就是 README 说的"回退到串行执行"。代码注释(L668-L670)也点明了代价:带宽受限于单机的 H2D,但省下一份桶大小的显存。

**错误传播**(任何一个 worker 失败,全体一致退出):

- worker 装载失败时不抛异常,而是把异常文本回传给 PS([checkpoint_engine/worker.py:L113-L117](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L113-L117),注释写明:所有 worker 必须以相同方式退出,由 PS 统一下发异常)。
- PS 收到非空应答就把 `ret_code` 置 1(L892-L897),经 `all_reduce(SUM)` 广而告之(L898);任何一个 rank 发现 `ret_code != 0`,就向自己的 worker 发送 `RuntimeError("Some workers failed to update weights")` 并抛出 `RuntimeError("Failed to update weights due to remote errors")`(L900-L903)。
- worker 收到 Exception 类型 payload 时才真正 `raise`([checkpoint_engine/worker.py:L118-L121](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L118-L121))。

**REQ/REP 消息序列表**(PS 视角;`B` 为桶总数):

| 序 | PS 动作 | worker 对应动作 | 代码 |
| --- | --- | --- | --- |
| 0 | `send(ipc_handle)` | `attach` 共享显存,回 `b""` | ps.py L849 / worker.py L68-L72 |
| gidx=0..B-1 | `dist.broadcast` 后 `recv`(收到的是**上一条**消息的应答),再 `send(张量元数据)` | 收元数据 → reload → 回 `b""` | ps.py L890-L904 / worker.py L108-L117 |
| B | `recv`(最后一桶应答) | reload 完成回 `b""` | ps.py L907 |
| B+1 | `send(None)` 第一次 | 释放 IPC 资源后回 `b""` | ps.py L913-L914 / worker.py L94-L107 |
| B+2 | `send(None)` 第二次 | 执行 `post_hook` 后回 `b""` | ps.py L931-L932 / worker.py L87-L93 |

PS 共发送 \(B+3\) 条、接收 \(B+3\) 条消息——REQ/REP 严格交替,一条不多一条不少。协议细节在第 3 单元第 6 讲还会专门展开。

#### 4.2.3 源码精读

**分流与守卫**——[checkpoint_engine/ps.py:L774-L802](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L774-L802):`ranks` 为空打日志走广播;非空先做 P2P 能力守卫(XPU 上 Mooncake 无法注册显存,直接拒绝并提示改用广播,L782-L788),再断言 p2p store 已初始化;`need_update = self._rank in ranks`(L793),不在名单的 rank 直接返回,**但注意它们仍参与了 `update()` 开头的 `dist.new_group` 等全局集合操作**——这就是 `update()` docstring(L578-L580)警告"所有 rank 都必须调用 update"的原因。

**三种 buffer 的诞生**——[checkpoint_engine/ps.py:L813-L833](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L813-L833):

```python
h2d_buffer: torch.Tensor | None = (
    None
    if disable_h2d_buffer
    else torch.empty(bucket_size, dtype=torch.uint8, device=self.device_manager.device_type)
)   # 串行回退时不分配,直接省下一份桶大小的显存
...
buffer = torch.empty(
    bucket_size * 2, dtype=torch.uint8, device=self.device_manager.device_type
)   # IPC 双缓冲:两个半区轮流承接桶
...
handle = ipc_handler.export(buffer)   # 导出给 worker 的跨进程句柄
```

**ZMQ 信道与触发线程**——[checkpoint_engine/ps.py:L842-L849](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L842-L849):`_bind_zmq_socket`(定义在 [checkpoint_engine/ps.py:L622-L630](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L622-L630))为每个设备 UUID 生成形如 `ipc://@checkpoint-engine-{uuid}-{counter}.sock` 的抽象 UDS 地址;`req_func` 在独立线程里运行,职责是"把地址清单交给推理引擎并让它调用 worker 扩展"(vLLM 场景即 `/collective_rpc`);PS 主线程随即发出 IPC 句柄(L849)。

**主循环**——[checkpoint_engine/ps.py:L855-L905](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L855-L905) 已在 4.1.3 逐行对应三阶段,这里补充三个读代码时的观察点:

- `ret_code` 初始化为标量张量(L852),`buffer_b` 在循环前先声明为 `None`(L853)——否则当循环体一次都没执行时,收尾的 `del buffer_b`(L916)会抛 `NameError`。
- 每个桶开始时 rank 0 会打印进度日志(L871-L875),含当前已分配/保留显存,是排查显存问题的一手材料。
- 收尾释放顺序是刻意的:**先删视图(`buffer_b`)再删底板(`buffer`)**(L916 注释 "Set to None in correct order (views first, then base tensors)"),随后 `synchronize → gc.collect → ipc_collect → empty_cache → synchronize`(L917-L921)。

**`finally` 块**——[checkpoint_engine/ps.py:L933-L940](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L933-L940):无论成败都要 join 触发线程、`dist.barrier`(保证所有 rank 一起离开)、关 socket、注销 p2p 模式下注册的 IPC buffer、清空缓存。

#### 4.2.4 代码实践

**实践目标**:不动一行代码,手工推演 `_detect_bucket_size` 在三种场景下的输出,把"流水线 vs 串行"的判定条件变成肌肉记忆。

**操作步骤**:

1. 精读 [checkpoint_engine/ps.py:L632-L682](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L632-L682),确认三个输入:全集群最小空闲显存 `free`(已乘 `mem_fraction`,默认 0.9)、最大单张量对齐字节数 `max_tensor_bytes`、环境变量 `PS_MAX_BUCKET_SIZE_GB`(默认 8)。
2. 对下面三个场景,分别计算:走哪个分支、`disable_h2d_buffer` 是否为 True、最终 `bucket_size`:
   - **场景 A**:集群最小空闲显存 50 GiB,最大张量 1 GiB,未设环境变量。
   - **场景 B**:集群最小空闲显存 50 GiB,最大张量 20 GiB,未设环境变量。
   - **场景 C**:集群最小空闲显存 50 GiB,最大张量 30 GiB,未设环境变量。
3. 写出每个场景下三块显存(`h2d_buffer`、`buffer`、合计)各占多少。

**需要观察的现象**(如果有一台 GPU 机器并运行 `examples/update.py`):rank 0 的日志会依次出现 `use h2d buffer` 或 `disable h2d buffer when max_tensor_bytes ... is larger than free_bytes ... // 3`(ps.py L663 / L671-L673),以及 `auto detect bucket size X GiB`(L681)。对照你算的结果。**该日志观察需要 GPU 环境,待本地验证**;纯 CPU 环境只完成纸面推演即可。

**预期结果**(纸面推演,忽略 256 字节对齐的零头;`free = 50 × 0.9 = 45 GiB`):

| 场景 | 判定 | 模式 | bucket_size | 显存占用 |
| --- | --- | --- | --- | --- |
| A | 1 ≤ 45/3 = 15 | 流水线 | min(max(8, 1), 15) = **8 GiB** | h2d 8 + buffer 16 = 24 GiB |
| B | 20 > 15 → 回退;20 ≤ 45/2 = 22.5 | 串行 | min(max(8, 20), 22.5) = **20 GiB** | h2d 0 + buffer 40 = 40 GiB |
| C | 30 > 15 → 回退;30 > 22.5 | **断言失败报错**(ps.py L675-L677) | — | — |

场景 B 说明了回退的合理性:宁可牺牲流水线(省一份桶显存、桶开到 20 GiB),也要装下超大张量;场景 C 说明单张量连 free/2 都放不下时无解,直接失败比静默截断更好。

#### 4.2.5 小练习与答案

**练习 1**:为什么桶大小要用 `all_reduce(MIN)` 取**全集群最小**空闲显存,而不是各 rank 用自己的?

**答案**:所有 rank 必须使用**同一个** `bucket_size`,因为 `dist.broadcast` 的收发双方要切出同样大的半区(`gidx % 2 * bucket_size` 对齐);如果某个 rank 的桶比别人大,小显存 rank 会 OOM。木桶效应决定了只能按最紧的那张卡算(ps.py L640-L655)。同一次 `all_reduce` 还顺带用"负数复用 MIN"的技巧拿到了各 rank `_zmq_addr_counter` 的最大值,保证 ZMQ 地址计数全局一致。

**练习 2**:`PS_MEM_FRACTION` 和 `PS_MAX_BUCKET_SIZE_GB` 分别作用在公式 `bucket_size = min(max(cap, max_tensor), free)` 的哪一端?各自调大会有什么后果?

**答案**:`PS_MEM_FRACTION`(默认 0.9)乘在空闲显存上,决定 `free`,即公式右端的**上限**;调大它,桶上限提高,但更逼近真实空闲显存,有与推理引擎争抢显存的风险。`PS_MAX_BUCKET_SIZE_GB`(默认 8)是日常**封顶**,决定 `cap`;调大它,单桶更大、循环轮数更少、流水线更饱满,但三倍桶大小的显存占用随之上升,且更容易触发回退甚至失败。

**练习 3**:主循环里半区索引用的是 `gidx % 2` 而不是外层轮次 `i % 2`,为什么?

**答案**:`i` 是"第 i 轮",而一轮内要为**每个接收 rank** 各广播一个桶,桶的全局编号 `gidx` 在一轮内连续递增(ps.py L905)。若按 `i` 取半区,同一轮内多个桶会落在同一半区:广播桶 `gidx+1` 时 worker 可能还在读半区 0 里的桶 `gidx`(第 (7) 步的应答还没回来),数据会被覆盖。按 `gidx` 交替才能保证相邻两个桶永远在不同半区,这是双缓冲正确性的前提。

### 4.3 update_weights_from_ipc:worker 侧的 reload 状态机

#### 4.3.1 概念说明

worker 侧的全部逻辑浓缩在一个函数里:`update_weights_from_ipc`([checkpoint_engine/worker.py:L54-L131](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L54-L131))。它运行在**推理引擎进程**里(vLLM 通过 `/collective_rpc` 调用 `VllmColocateWorkerExtension.update_weights_from_ipc`,后者再委托给它),扮演 ZMQ 的 REP 方。它不认识任何分布式原语,只做四件事:

1. 收 IPC 句柄,`attach` 出共享显存 buffer;
2. 收张量元数据列表,从 buffer 零拷贝切出权重,调用 `run`(= `load_weights`);
3. 收到第一个 `None` 时释放 IPC 资源;
4. 收到第二个 `None` 时执行 `post_hook`(权重后处理)后退出。

源码注释把这套规则总结为四类消息的状态机([checkpoint_engine/worker.py:L78-L82](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L78-L82)):收 list → 更新权重;收 Exception → 抛出并停止;第一次收 None → 释放资源;第二次收 None → 调 `post_hook` 并停止。

#### 4.3.2 核心流程

```text
                 recv(ipc_handle)
                       │ attach 成功
                       ▼
              send(b"")            ── 若 attach 失败:回传异常文本并 raise
                       │
        ┌───────► recv(payload)
        │              │
        │              ├── list(仍在更新)
        │              │      run(_extract_weights(payload, buffer))
        │              │      synchronize → send(b"") ──┐
        │              │      (run 抛异常:send_string(异常文本),不 raise)
        │              ├── Exception(PS 强制退出信号)──► raise payload
        │              └── None 且未释放:
        │                     buffer=None;ipc detach;gc/ipc_collect/empty_cache
        │                     released = True → send(b"") ──┐
        │              └── None 且已释放:
        │                     post_hook() → synchronize → send(b"") → break ◄─┘
        └──────────────────────────────────────────────────────(循环)
                       │
                 finally:关 socket、再 detach 一次、清缓存
```

设计上有两个值得咀嚼的点:

- **worker 报错不自杀**。`run` 抛异常时,worker 把 traceback 文本回传给 PS(L113-L117),自己继续循环。因为 PS 各 rank 还卡在集合通信里,必须由 PS 通过 `ret_code` 约减统一决定"全队一起退",再把异常对象下发给所有 worker(见 4.2.2)。如果 worker 当场 raise,其他进程可能永远等不到集合通信的对端。
- **释放是两次 `None` 而不是一次**。第一次 `None` 只释放 IPC 显存资源(此时权重已装载完,PS 也已确认),第二次 `None` 才触发 `post_hook`(如 FP8/量化相关的 `process_weights_after_loading`)。把"释放"与"后处理"分成两个栅栏,让 PS 能在两者之间完成自己的清理(对照 ps.py L913-L932 的顺序)。

#### 4.3.3 源码精读

**连接与 attach**——[checkpoint_engine/worker.py:L62-L72](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L62-L72):创建 REP socket 并 `connect` 到 PS 绑定的抽象 UDS 地址;收到句柄后由 `_ipc_handler_for_handle`(L21-L28)根据句柄的线上格式选择 `TorchIPCHandler`(CUDA/NPU)或 `XpuIPCHandler`(XPU),`attach` 出 `uint8` 类型的共享 buffer。IPC 契约的定义见 [checkpoint_engine/ipc_handler.py:L39-L59](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ipc_handler.py#L39-L59)(`export / attach / detach` 三个抽象方法),模块 docstring([checkpoint_engine/ipc_handler.py:L1-L11](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ipc_handler.py#L1-L11))强调:句柄是自包含的可序列化值,所以 PS 一次 `send_pyobj` 就完成交接。

**reload 分支**——[checkpoint_engine/worker.py:L108-L117](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L108-L117):`payload` 是 list 时执行 `run(_extract_weights(payload, buffer))`,随后 `synchronize` 保证装载真正完成才回 `b""`——PS 的下一次 `recv` 因此有了"上一桶已装载完"的语义,这正是 4.1 时序图第 (7) 步的含义。

**零拷贝切张量**——[checkpoint_engine/worker.py:L39-L51](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L39-L51):`size = dtype.itemsize * shape.numel()`,`buffer[offset : offset + size].view(dtype=dtype).view(shape)`,全程不复制字节;权重张量直接"住在"共享 buffer 上,直到 `load_weights` 把它们写进模型参数。

**第一次 None(释放)**——[checkpoint_engine/worker.py:L94-L107](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L94-L107):`buffer = None` 丢掉视图引用、`ipc_handler.detach()` 归还 IPC 映射,再 `gc.collect()` / `ipc_collect()` / `empty_cache()`,与 PS 侧的收尾清理(L916-L921)一一呼应。

**第二次 None(post_hook)**——[checkpoint_engine/worker.py:L87-L93](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L87-L93):`released` 为 True 时,断言这一定是 None(协议不允许释放后再收到数据),执行 `post_hook()`、同步、应答、`break`。

**vLLM 集成层**——[checkpoint_engine/worker.py:L168-L231](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/worker.py#L168-L231):`VllmColocateWorkerExtension.update_weights_from_ipc` 被 vLLM 的 `collective_rpc` 调用(L191-L192 注释),它用 `self._device_uuid` 在 `zmq_handles` 字典里挑出属于本 GPU 的 ZMQ 地址(L225-L231),再委托给本节的状态机;`_load_weights`(L204-L212)装载主模型与可选的 MTP/drafter 模型,`_post_hook`(L214-L223)调用 vLLM 的 `process_weights_after_loading`。设备 UUID 如何与 PS 侧对齐,留到第 4 单元第 2 讲。

#### 4.3.4 代码实践

**实践目标**:通过端到端测试 `tests/test_update.py` 理解 worker 的"替身"如何验证三阶段,尤其是错误传播的两个异常消息为何不同。

**操作步骤**:

1. 阅读 [tests/test_update.py:L88-L132](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_update.py#L88-L132) 的 `checker_proc`:它在一个独立进程里调用真实的 `update_weights_from_ipc`(L107-L113),`run` 回调是 `check`(L98-L103)——逐个比较收到的权重与参考张量是否逐元素相等,`post_hook` 只做一次 `synchronize`。
2. 阅读驱动函数 [tests/test_update.py:L135-L177](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_update.py#L135-L177):注意 L167 `ps.update(checkpoint_name, queue.put, ranks=ranks)` ——**`req_func` 就是往进程队列里放 ZMQ 地址清单**,checker 子进程从队列取地址,扮演"被通知的推理引擎"。没有 vLLM,协议照样跑通,这就是控制面与数据面解耦的直接证据。
3. 阅读 [tests/test_update.py:L52-L86](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_update.py#L52-L86) 的 `checker_proc_with_error`:rank 0 的 `run` 会故意抛 `RuntimeError`(L72-L76)。
4. 回答两个问题(答案见下):① `checker_proc` 里哪几行分别对应真实 vLLM 场景的 attach、reload、post_hook?② 为什么 L85 断言 worker 进程收到的异常消息必须是 `"Some workers failed to update weights"`,而测试主进程(PS 侧)期望的却是 `"Failed to update weights due to remote errors"`(L343)?

**需要观察的现象**:如果想真正运行,需要至少 2 张 GPU 并执行 `pytest tests/test_update.py`(该测试带 `@pytest.mark.gpu` 标记,纯 CPU 环境会被跳过,见 L239-L264);本实践以阅读为主,**运行结果待本地验证**。

**预期结果**(纸面答案):

- ① attach = L107-L113 里 `update_weights_from_ipc` 内部的 `socket.recv_pyobj` + `ipc_handler.attach`(即 worker.py L68-L72);reload = `run=...` 回调执行时的 `check`(L98-L103,逐张量 `assert (weight == named_tensors[name]).all()`);post_hook = L112 的 `post_hook=lambda: synchronize`。
- ② 两个消息来自 ps.py 的两行:L902 `socket.send_pyobj(RuntimeError("Some workers failed to update weights"))` 是 PS **发给 worker** 的强制退出信号,worker(替身)收到后 raise,所以替身进程断言它;L903 `raise RuntimeError("Failed to update weights due to remote errors")` 是 PS **自己向上抛**的异常,所以测试主进程(L343)断言它。一个数字之差,方向相反——这正是 4.2.2 错误传播链的两端。

#### 4.3.5 小练习与答案

**练习 1**:元数据里的 `offset` 为什么是"相对整个 2 倍 buffer"的绝对偏移(起点 `gidx % 2 * bucket_size`),而不是相对桶起点从 0 开始?

**答案**:因为 worker 的 `_extract_weights` 拿到的是**整块 2 倍 buffer** 的视图(worker.py L70 attach 出的就是完整的 `buffer`),它不关心"这是第几桶",只按 `offset` 直接切片(worker.py L49)。PS 侧 `_to_named_tensor(metas, offset)` 在起始偏移上累加对齐字节数(ps.py L35-L48),把"第几个半区"的信息折叠进了绝对偏移里,worker 侧因此完全无状态。

**练习 2**:`run` 回调抛异常后,worker 进程会立刻退出吗?为什么这样设计?

**答案**:不会。worker 把异常格式化成文本回传给 PS(worker.py L113-L117),自己继续消息循环;只有收到 PS 下发的 Exception 对象(worker.py L118-L121)才 raise。注释(L114-L115)给出了理由:所有 worker 必须以相同方式退出——此时 PS 各 rank 还在 `dist.broadcast` / `dist.all_reduce` 等集合通信里,若某个 worker 进程单方面退出,与之配对的 PS rank 会失联,其他 rank 将永远阻塞在集合通信上。

**练习 3**:`released` 标志防住的是什么非法序列?

**答案**:防止"释放之后还收到数据"。第一次 `None` 之后 worker 已把 `buffer` 置 None、归还 IPC 映射,此时若再收到张量元数据,`_extract_weights` 会在 `assert buffer is not None` 上失败、甚至访问已释放显存。所以状态机在 L87-L88 用 `assert payload is None, "Should not receive any payload after released"` 显式拦截,把协议错误变成清晰的断言错误。

### 4.4 P2P 更新:动态扩容场景的另一条通道

#### 4.4.1 概念说明

Broadcast 解决"一大批实例**同步**换权重",但生产上还有另一类需求:**新实例动态加入**(实例重启、弹性扩容),而存量实例正在服务请求。此时不能为了新实例把存量实例拉进一次全量广播——那会打断服务。README 对 P2P 的定位见 [README.md:L18](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/README.md#L18):"为了避免影响存量实例上的工作负载,我们用 mooncake-transfer-engine 把权重从**存量实例的 CPU** 点对点发到**新实例的 GPU**"。

理解 P2P 的最省力方式:**它复用了 4.1-4.3 的整套骨架,只替换了"H2D 这一格"的数据来源**。

| 环节 | Broadcast | P2P |
| --- | --- | --- |
| 数据来源 | 本地 CPU 锁页内存池 | **远端 owner 的 CPU 锁页内存**(经 RDMA 读) |
| 进入显存的方式 | PCIe H2D 拷贝 | Mooncake transfer engine 直接写入本机显存 buffer |
| 集合通信 | `dist.broadcast` 到所有 rank | 仅 `ranks` 子组内,且主要是同步/错误约减 |
| ZMQ + IPC + reload | 使用 | **完全相同** |
| 典型调用 | `ps.update(name, req_func)` | `ps.update(name, req_func, ranks=[...])`(如 README L61 的 `ranks=range(0, 16)`) |

存量实例为什么不受打扰:它们只是把自己锁页内存池**注册**进了 transfer engine(注册发生在 `register_checkpoint` 时,见 [checkpoint_engine/ps.py:L721-L735](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L721-L735)),GPU 不参与任何集合通信,数据被 RDMA 读走时 CPU 内存也只是在被读取而已。

另一个宏观事实:发送方与接收方是**不同批次**的实例。新实例的 PS 通过 `gather_metas`(或跨进程导入 metas,第 6 单元第 3 讲)拿到旧实例的内存布局与 p2p store 地址,于是"owner rank"属于旧实例、"receiver rank"属于新实例,桶的分配要同时吃满收发双方的网卡带宽——这就是 README 的 "Optimized P2P Bucket Assignment"([README.md:L39-L43](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/README.md#L39-L43)),分配算法在第 5 单元第 6 讲精讲。

#### 4.4.2 核心流程

P2P 模式下 `_update_per_bucket` 的差异点(对照 4.2.2 的十步骨架):

1. **分流与守卫**(L774-L802):`ranks` 非空;设备必须支持 P2P(XPU 直接拒绝,L782-L788);`need_update = self._rank in ranks`,名单外的 rank 在做完 `update()` 的全局准备工作后直接返回;名单内的先 `dist.barrier`(L802,注释:避免后续设备 OOM——等所有参与方都就绪再开始占显存)。
2. **桶的 receiver 分配换算法**(L805-L811):Broadcast 时 `_gen_h2d_buckets` 直接令 `receiver = owner`(L101-L103);P2P 时走 `_assign_receiver_ranks`(L108-L163),按 RDMA 拓扑把桶分给接收端,目标是让每对收发的网卡都被打满。
3. **目标 buffer 注册进 p2p store**(L827-L832):本机的 IPC buffer(或 `h2d_buffer`)要先注册,远端 RDMA 引擎才能把它当写入目标。
4. **H2D 换成 RDMA 读**:`_copy_to_buffer` 带 `owner_rank` 参数时进入远端分支——[checkpoint_engine/ps.py:L692-L703](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L692-L703) 收集每段(本机 buffer 指针、远端锁页池指针、长度),再由 [L712-L713](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L712-L713) 的 `self._p2p_store.batch_transfer_sync_read(...)` 批量同步读回。远端地址从元数据里的 `p2p_store_addr` 查得(`_get_addr_ptrs`,L716-L719)。
5. **之后的广播、ZMQ、reload、收尾与 Broadcast 完全一致**:每个桶仍是"取数 → 写入本机 IPC buffer 半区 → 通知 worker reload"(P2P 子组内若只有本 rank 需要数据,`dist.broadcast` 退化为组内同步)。

#### 4.4.3 源码精读

**分流守卫**——[checkpoint_engine/ps.py:L774-L802](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L774-L802):注意 L782-L788 的报错信息直接给出修复建议("Use the broadcast update (leave ranks unset) instead"),这是"把失败提前到入口"的又一次实践(与 L766-L772 的 IPC 守卫呼应)。README 也声明了这条限制:XPU 不支持 P2P,Mooncake 没有 Level Zero 后端([README.md:L80](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/README.md#L80))。

**P2P 的"取数"分支**——[checkpoint_engine/ps.py:L684-L714](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L684-L714):`_copy_to_buffer` 用 `owner_rank is not None` 一个参数就把"H2D 本地拷贝"和"RDMA 远端读"合进了同一个函数——Broadcast 与 P2P 在这里汇合成同一段后续代码,这是"两种更新方式共用 `_update_per_bucket`"在源码上的具体形态。

**接收端挑选(只看结论)**——[checkpoint_engine/ps.py:L135-L137](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ps.py#L135-L137):`num_receivers = min(len(local_topo), len(buckets_by_rdma_device))`,每个本地 RDMA 设备组里选最小 rank 当接收端;后续列优先展平、每轮占用不重复网卡的贪心分配在 L139-L163。本讲只需记住"桶 → 接收端"的映射不是顺序轮流,而是按网卡拓扑来的。

#### 4.4.4 代码实践

**实践目标**:用两次文本搜索画出"P2P 能力守卫链",验证课堂上那句"XPU 上 P2P 不可用"不是口头断言而是代码约束。

**操作步骤**:

1. 在仓库根目录执行 `grep -rn "supports_device_p2p" checkpoint_engine/`(或用编辑器的全局搜索)。
2. 对每个命中位置,读上下文 10 行,标注它发生在什么时机(初始化 / 更新入口)、失败时做什么(跳过 / 报错)。
3. 再执行 `grep -n "p2p" README.md`,找出与代码对应的文档句子。

**需要观察的现象**:命中应当有三处语义:`ParameterServer.__init__` 里初始化 P2PStore 前的能力判断(不支持则整个 store 置 None,ps.py L237-L248)、`_update_per_bucket` 入口的硬拒绝(ps.py L782-L788)、以及 README 对 XPU 限制的说明(README.md L80)。`supports_device_p2p` 的定义本体在 `checkpoint_engine/device_utils.py` 中(第 5 单元第 1 讲精读,此处不必展开)。

**预期结果**:你能写出这样一条链——XPU 设备 → `supports_device_p2p()` 返回 False → 初始化时 `_p2p_store = None`(并打印 "p2p store disabled: not supported on device type 'xpu'")→ 一旦调用方传了 `ranks`,更新入口直接 `RuntimeError` 并建议改用 Broadcast。纯文本搜索,CPU 环境即可完成,无需运行。

#### 4.4.5 小练习与答案

**练习 1**:P2P 模式下一次 `update(ranks=[...])` 调用中,哪些 PS rank 真正搬运数据?名单外的 rank 为什么要参与这次调用?

**答案**:只有 `self._rank in ranks` 的 rank 真正搬运(`need_update`,ps.py L793),名单外的在 L799-L800 直接返回。但**所有** rank 都必须调用 `update()`,因为开头 `dist.new_group(ranks)`(L599)是对全世界的集合操作,结尾 `store_based_barrier`(L604)也以 `world_size` 为 rendezvous 数;这正是 `update()` docstring(L578-L580)警告的挂起风险。

**练习 2**:为什么 P2P 模式在真正传输前先执行一次 `dist.barrier`(ps.py L802)?

**答案**:源码注释给出的动机是 "first execute a barrier to avoid subsequent device oom"——参与方刚走到这里时各自的显存状态可能差异很大(例如新实例刚完成初始化、缓存未清),而接下来要连续分配 `h2d_buffer` 与 2 倍 IPC buffer。先在子组内对齐一步,把成员间的准备差异消除掉,再统一探测桶大小、统一分配,降低某一方在分配阶段 OOM 的概率。

**练习 3**:对比表格里说 P2P 与 Broadcast "ZMQ + IPC + reload 完全相同",源码上有什么证据?

**答案**:两条路径进入同一个 `_update_per_bucket`(README L17-L18 明确写了两种方式都指到它),主循环、`_bind_zmq_socket`、`ipc_handler.export`、两次 `None` 收尾(ps.py L842-L932)没有任何 `if p2p_update` 分支;P2P 特有的代码只有:入口分流与守卫(L774-L802)、目标 buffer 注册(L827-L832)、以及 `_copy_to_buffer` 里 `owner_rank is not None` 的取数分支(L692-L713)。差异被收敛在"数据怎么进 buffer"这一格,交接协议保持不变。

## 5. 综合实践

**任务:为一个 3 桶的小例子"人肉执行"一遍协议,产出带源码行号标注的时序图。**

这是把本讲三个模块串起来的纸面作业,不需要任何硬件。

1. **设定**:某次 Broadcast 更新共 3 个桶(`gidx = 0, 1, 2`),你的机器是 rank 1,参与了全部桶的接收与广播。请写出:
   - 每个桶使用的 `buffer` 半区(用 `gidx % 2` 计算);
   - PS 与 worker 之间**每一条** ZMQ 消息的方向与内容(按 4.2.2 的消息序列表编号),数出 PS 总共发送、接收各多少条;
   - `dist.broadcast` 被调用多少次。
2. **对照源码自检**:每条消息旁边标注 `ps.py` / `worker.py` 的行号(发送在 ps.py L849/L904/L913/L931,接收在 L891/L907/L914/L932;worker 侧对应 worker.py L68/L86/L106/L92)。
3. **扩展到退化条件**:假设你的卡空闲显存 45 GiB、`mem_fraction` 取默认 0.9、最大张量 18 GiB,回答:这次更新会以哪种模式执行?桶多大?`h2d_buffer` 是否存在?画出该模式下时序图里"第 (4) 步"的变化。
4. **再扩展到 P2P**:如果把同一批权重改为 `ranks=[0, 1]` 的 P2P 更新,时序图中哪几条箭头不变、哪一格被替换成什么?

**预期结果**(可自查的关键数字):

- 3 个桶 → `dist.broadcast` 3 次;PS 发送 = 1(句柄)+ 3(元数据)+ 2(None)= 6 条,接收 = 3(逐桶应答)+ 1(末桶应答)+ 2(None 应答)= 6 条,REQ/REP 严格交替,总共 12 条消息。
- 半区序列:桶 0 → 半区 0,桶 1 → 半区 1,桶 2 → 半区 0(与桶 0 复用,但此时桶 0 的 reload 早已被应答)。
- 第 3 问:free = 45 × 0.9 = 40.5 GiB,free/3 = 13.5 GiB < 18 GiB → 串行回退;free/2 = 20.25 GiB ≥ 18 GiB,故 bucket = min(max(8, 18), 20.25) = 18 GiB;`h2d_buffer` 不存在,时序图第 (4) 步消失,改为第 (5) 步内"接收者把锁页池直接 H2D 拷进半区"(ps.py L879-L887)。
- 第 4 问:所有 ZMQ 箭头与 reload 不变;被替换的是数据来源——"H2D:锁页池 → buffer"变成"RDMA:`batch_transfer_sync_read` 从远端 owner 锁页池 → 本机 buffer"(ps.py L692-L713),且 `ranks` 外的 rank 不出现在图中(它们已在 L799-L800 返回)。

如果你所在的环境有 GPU,可以把这张图与 `pytest tests/test_update.py` 运行时的日志互相印证(rank 0 会逐桶打印 `begin to update bucket i/N ... bucket_size: ...MiB`,ps.py L871-L875);没有 GPU,本作业的纸面推演本身即是完整交付。

## 6. 本讲小结

- **一次 Broadcast 更新 = 三阶段流水线**:H2D(锁页池 → `h2d_buffer`,ps.py L856-L862/L704-L709)→ broadcast(D2D 进 IPC 双缓冲半区后 `dist.broadcast`,ps.py L876-L890)→ reload(worker 按元数据从共享 buffer 零拷贝切张量并 `load_weights`,ps.py L904 + worker.py L108-L117/L39-L51)。
- **重叠靠两件事**:`h2d_buffer` 让各 rank 的 H2D 提前异步预取;`gidx % 2` 双缓冲让桶 `gidx+1` 的广播与桶 `gidx` 的 reload 同时进行。代价是 `3 × bucket_size` 显存。
- **显存不够就退化**:`max_tensor_bytes > free/3` 时放弃 `h2d_buffer`(省一份桶显存、桶上限放宽到 free/2),H2D 贴着广播临界路径串行执行;连 free/2 都装不下最大张量则直接报错(ps.py L632-L682)。
- **worker 是一个四类消息的 REP 状态机**:list → 装载;Exception → 退出;第一个 None → 释放 IPC;第二个 None → `post_hook`(worker.py L78-L123)。worker 报错只回传不 raise,由 PS 用 `ret_code` 约减统一下发退出信号。
- **P2P 是同一骨架的换源版本**:只把"H2D 取数"换成 Mooncake RDMA 远端读(ps.py L692-L713),控制面与 reload 协议原封不动,服务的是"新实例动态加入、不打扰存量实例"的场景。

## 7. 下一步学习建议

按学习路线,下一单元(第 2 单元)回到**数据与内存**:先读 `data_types.py` 里的 `ParameterMeta`、`H2DBucket`、`MemoryBufferMetas`——本讲反复出现的 `aligned_size`、桶、元数据列表都在那里定义;再读 `pin_memory.py` 理解锁页内存池是怎么从 safetensors 建起来的。带着本讲的问题去读会非常顺:比如"`_to_named_tensor` 累加的 `aligned_size` 是谁算的?"(答案在 `data_types.py` 的对齐逻辑里)。

如果急于看动态行为,可以先读 [tests/test_update.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/tests/test_update.py) 全文,把它当作"最小可运行的架构演示";想提前深入 IPC 句柄的导出与重建,可读 [checkpoint_engine/ipc_handler.py](https://github.com/MoonshotAI/checkpoint-engine/blob/d1de07b3aacff34050d09c3efa093f9a2fcdcf73/checkpoint_engine/ipc_handler.py)(第 4 单元第 3、4 讲的主题)。第 3 单元将逐段精读本讲一笔带过的 `gather_metas`、`_gen_h2d_buckets` 与 ZMQ 协议细节。
