# PD 分离部署 disagg

## 1. 本讲目标

本讲讲解 LMDeploy 的 **Prefill/Decode 分离部署（PD Disaggregation，代码中称 DistServe）** 框架。

Prefill（首字计算）和 Decode（逐字生成）是 LLM 推理的两个阶段，二者对算力与显存带宽的消耗模式截然不同：Prefill 是计算密集型、可大幅并行；Decode 是访存密集型、单步只产一个 token。把它们塞进同一个引擎（Hybrid）会互相干扰——长 prefill 会阻塞短 decode。PD 分离的核心思想是**把两类负载拆到两组不同的引擎上**，并在 prefill 完成后把已算好的 KV cache 直接搬运到 decode 引擎，避免重复计算。

学完本讲，你应当能够：

- 说清 PD 分离部署的动机与整体架构，区分 `Hybrid` 与 `DistServe` 两种服务策略。
- 读懂 `lmdeploy/pytorch/disagg/` 三个子模块的职责分工：**config（配置/枚举）**、**backend（KV 传输数据面）**、**conn（连接管理控制面）**。
- 画出「prefill 节点 → KV 传输 → decode 节点」的完整数据流，并指出 conn 模块负责哪一段通信。
- 认识 DLSlime 与 Mooncake 两套传输后端，以及 RDMA / NVLink 两种迁移协议。

> 重要前提：**目前仅 PyTorch 后端支持 PD 分离**，TurboMind 不支持（见 [README.md:41](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/disagg/README.md#L41)）。

## 2. 前置知识

本讲是专家层内容，需要你先掌握以下基础（对应前置讲义）：

- **持续批处理与 Paged Attention**：理解 KV cache 是「分块（block）」组织的，每个 block 存若干 token 的 K、V 张量（见 u4-4、u4-5）。
- **PyTorch 引擎的异步循环**：Engine 内部有 `preprocess_loop` / `main_loop` / `send_response_loop` / `migration_loop` 四条协程（见 u4-2）。本讲的迁移正是 `migration_loop` 干的活。
- **引擎配置数据类**：`PytorchEngineConfig` 有一个 `role` 字段，本讲会用到它的 `Hybrid / Prefill / Decode` 取值（见 u3-2）。
- **proxy 多机代理**：理解 `serve/proxy/proxy.py` 是多个 `api_server` 前的反向代理，负责节点路由（见 u8-4）。

几个本讲会用到的术语先解释清楚：

- **PD Peer**：一对 prefill 引擎与 decode 引擎之间的连接关系。
- **KV Migration（迁移）**：把 prefill 引擎 GPU 上算好的 KV cache，通过网络搬运到 decode 引擎 GPU 的过程。
- **GPUDirect RDMA（GDR）**：让网卡直接读写 GPU 显存、不经过主机内存拷贝的硬件能力，是高速 KV 迁移的基础。
- **MR（Memory Region）**：在 RDMA 编程中，必须先「注册」一段显存/内存区域，网卡才能远程读写它。
- **控制面 vs 数据面**：控制面负责「握手、协调、通知」（少量小消息），数据面负责「搬运 KV 字节」（大块吞吐）。本讲你会看到 LMDeploy 用 zmq + HTTP 做控制面、用 RDMA/NVLink 做数据面。

## 3. 本讲源码地图

PD 分离的全部代码集中在 `lmdeploy/pytorch/disagg/`，外加引擎与代理里的接入点。下表是本讲涉及的文件：

| 文件 | 作用 |
| --- | --- |
| [disagg/README.md](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/disagg/README.md) | 部署文档与最小启动示例（router + prefill + decode 三进程） |
| [disagg/config.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/disagg/config.py) | 枚举与配置数据类：服务策略、引擎角色、迁移后端、传输协议、引擎拓扑 |
| [disagg/messages.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/disagg/messages.py) | 迁移执行的消息载体：迁移批次、搬运指令、连接握手消息 |
| [disagg/backend/base.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/disagg/backend/base.py) | 传输后端抽象基类 `MigrationBackendImpl` |
| [disagg/backend/backend.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/disagg/backend/backend.py) | `MIGRATION_BACKENDS` 注册表（mmengine Registry） |
| [disagg/backend/dlslime.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/disagg/backend/dlslime.py) | DLSlime 传输后端实现（RDMA / NVLink） |
| [disagg/backend/mooncake.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/disagg/backend/mooncake.py) | Mooncake 传输后端实现 |
| [disagg/conn/protocol.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/disagg/conn/protocol.py) | 连接协议的数据模型：初始化请求/响应、迁移请求等 |
| [disagg/conn/engine_conn.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/disagg/conn/engine_conn.py) | 引擎侧 P2P 连接 `EngineP2PConnection`（zmq 控制面 + 委托数据面握手） |
| [disagg/conn/proxy_conn.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/disagg/conn/proxy_conn.py) | 代理侧连接池 `PDConnectionPool`（编排 prefill↔decode 握手） |

接入点（非 disagg 目录，但完成数据流闭环必须了解）：

| 文件 | 作用 |
| --- | --- |
| [pytorch/engine/engine.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine.py) | Engine 持有 `engine_conn`，按 `role` 截断 max_new_tokens、唤醒 `migration_event` |
| [pytorch/engine/engine_loop.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine_loop.py) | `migration_loop` 协程：调度迁移、构造搬运批次、回送首 token |
| [pytorch/engine/cache_engine.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/cache_engine.py) | 把整块 KV cache 注册为 MR、按层/块步长计算搬运指令 |
| [serve/openai/api_server.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/openai/api_server.py) | 暴露 `/distserve/*` 路由供握手与缓存释放 |
| [serve/proxy/proxy.py](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/proxy/proxy.py) | DistServe 策略：先打 prefill、再带 `migration_request` 打 decode |

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：**config（配置层）** → **backend（数据面：KV 字节传输）** → **conn（控制面：连接编排）**。三者是分层关系，config 提供词汇表，backend 和 conn 共同消费它。

### 4.1 配置层 disagg config

#### 4.1.1 概念说明

PD 分离要回答一组「选择题」：服务用哪种策略？每个引擎扮演什么角色？KV 用什么后端搬、走什么协议？两台引擎各自的并行与缓存拓扑如何？`disagg/config.py` 用 **`enum.Enum` + pydantic `BaseModel`** 把这些选择固化成强类型词汇表，整个 disagg 子包的其余文件都围绕它展开。

#### 4.1.2 核心流程

config 层本身不执行流程，它定义五个枚举与若干配置模型，关系如下：

```text
ServingStrategy ──┐  决定整体部署形态（Hybrid 共置 / DistServe 分离）
EngineRole ───────┤  决定单个引擎身份（影响请求分发与 ModelInputs 构造）
MigrationBackend ─┤  决定用 DLSlime 还是 Mooncake 搬 KV
MigrationProtocol ┤  决定走 RDMA 还是 NVLink（在 conn/protocol.py 中定义）
RDMALinkType ─────┘  RDMA 子类型（IB / RoCE）

DistServeEngineConfig: 描述「我这台引擎」的并行度 + 缓存布局，握手时交换给对端
```

握手时两台引擎交换 `DistServeEngineConfig` 有两个目的，源码注释讲得很清楚（见 4.1.3）：一是算出 KV cache 的 **块步长（stride）** 以正确定位偏移；二是当 prefill 与 decode 用不同并行策略时，**算出哪些 worker 之间需要互连**。

#### 4.1.3 源码精读

**① 服务策略与引擎角色**——两个最顶层的枚举：

[config.py:7-36](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/disagg/config.py#L7-L36)：`ServingStrategy` 区分 `Hybrid`（prefill/decode 共置一个引擎）与 `DistServe`（分到不同引擎、prefill 后迁移 KV）。`EngineRole` 有一个反直觉但重要的注释——**技术上所有引擎都是 hybrid 引擎**，角色取决于「收到什么请求」，但仍需在启动时标记角色，原因有二：让 proxy 能正确发现引擎；DP 引擎（DeepSeek-V3 的 DP+EP）在构造 ModelInputs 时，hybrid/prefill/decode 三种角色的逻辑不同。

**② 迁移后端与传输链路**——决定「用什么搬、走哪条线」：

[config.py:39-50](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/disagg/config.py#L39-L50)：`MigrationBackend`（`DLSlime`/`Mooncake`）选传输引擎；`RDMALinkType`（`IB`/`RoCE`）选 RDMA 子类型。

[config.py:53-68](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/disagg/config.py#L53-L68)：`DistServeRDMAConfig` 默认 `with_gdr=True`（启用 GPUDirect RDMA）、`link_type=RoCE`，并明确警告「目前仅支持 GDR；IB 因缺乏测试环境未验证」。

**③ 引擎拓扑配置**——握手交换的核心信息：

[config.py:79-108](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/disagg/config.py#L79-L108)：`DistServeEngineConfig` 携带并行配置（`tp_size/ep_size/dp_size/pp_size/dp_rank`）与缓存配置（`block_size/num_cpu_blocks/num_gpu_blocks`）。注释给出一个具体例子：prefill 用 `pp4`、decode 用 `tp2pp2` 时，worker 连接对是 `(0,0)(0,1)(1,0)(1,1)(2,2)(2,3)(3,2)(3,3)`；而 `(tp4,tp4)` 时则是对角线 `(0,0)(1,1)(2,2)(3,3)`。这段注释揭示了「**并行度不对称时连接拓扑会变复杂**」，是 PD 分离比普通 TP 更难的地方。

> 注意：`DistServeTCPConfig` 与 `DistServeNVLinkConfig`（[config.py:71-77](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/disagg/config.py#L71-L77)）目前都是 `TODO` 占位，真正的协议枚举 `MigrationProtocol(TCP/RDMA/NVLINK)` 定义在 `conn/protocol.py`（4.3 节）。

#### 4.1.4 代码实践

**实践目标**：在源码层面把 config 层的「词汇表」摸清楚。

**操作步骤**：

1. 打开 `lmdeploy/pytorch/disagg/config.py`，依次列出五个 `enum.Enum` 类与它们的取值。
2. 找到 `DistServeEngineConfig`，数一数它有几个字段，分成「并行」与「缓存」两组。
3. 阅读其 docstring 中 `pp4` vs `tp2pp2` 的连接对例子。

**需要观察的现象**：你会注意到「真正实现的协议只有 RDMA（带 GDR）」，TCP/NVLink 多为占位或部分实现。

**预期结果**：能用一句话回答「为什么握手时要交换 `DistServeEngineConfig`」——为了算 KV 块偏移步长、并按并行度推导 worker 连接对。**待本地验证**：若本地装了 lmdeploy，可 `python -c "from lmdeploy.pytorch.disagg.config import * ; import lmdeploy.pytorch.disagg.config as c; print([x for x in dir(c) if x[0].isupper()])"` 打印全部符号。

#### 4.1.5 小练习与答案

**练习 1**：`EngineRole` 既然注释说「技术上所有引擎都是 hybrid」，为什么还要在启动时区分 Prefill/Decode？
**答案**：为了让 proxy 能按角色发现并路由引擎（见 README 的 `--role Prefill/Decode`），以及让 DP 引擎在构造 `ModelInputs` 时针对不同角色走不同分支。

**练习 2**：`DistServeRDMAConfig` 默认 `with_gdr=True`，关掉它会怎样？
**答案**：会退回到「网卡经主机内存再拷进 GPU」的传统路径，KV 迁移延迟与 CPU 拷贝开销显著上升；而当前实现「仅支持 GDR」（见 config.py 注释），关掉后可能直接不可用。

---

### 4.2 传输后端 disagg backend

#### 4.2.1 概念说明

config 决定「搬什么、怎么标识」，backend 决定「**真正把 KV 字节从 prefill GPU 搬到 decode GPU 的那一段数据面**」。LMDeploy 提供两套可插拔传输后端：

- **DLSlime**：OpenMMLab 自研的轻量 RDMA 传输库（`pip install dlslime`），支持 RDMA 与 NVLink，是 README 默认推荐路径。
- **Mooncake**：月之暗面开源的 KV cache 传输引擎，通过 `mooncake.engine.TransferEngine` 提供更高层的存储抽象。

两套后端实现同一套抽象接口 `MigrationBackendImpl`，靠 mmengine 注册表 `MIGRATION_BACKENDS` 按名字切换，互不耦合。可选依赖用 `try/except ImportError` 保护——没装就自动禁用该后端。

#### 4.2.2 核心流程

每个传输后端对外暴露一条 **「初始化 → 注册显存 → 握手 → 搬运」** 的流水线，由 `MigrationBackendImpl` 抽象基类规定：

```text
p2p_initialize(init_request)            # 创建 endpoint（RDMA 网卡 / NVLink）
   ↓
register_memory_region(mr_request)      # 把本端 KV cache 显存注册为可被远端读写的 MR
   ↓
endpoint_info(remote_engine_id, proto)  # 导出本端寻址信息交给对端
   ↓
p2p_connect(remote_engine_id, conn_req) # 用对端的寻址信息建立连接
   ↓
p2p_migrate(assignment, async_op)       # 按指令批次搬运 KV（数据面主操作）
```

关键点：`p2p_migrate` 收到的不是「整个 session」，而是一张**搬运指令表 `MigrationAssignment`**——每条 `AssignmentInstruct` 说明「从本端某 MR 的 source_offset、搬到对端某 MR 的 target_offset、长度 length」。这样就能精确到「逐层、逐块」地搬，避免整块显存大拷贝。

搬运指令的数据模型在 [messages.py:9-28](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/disagg/messages.py#L9-L28)：`MigrationExecutionBatch` 是 migrate 的输入（一组 `(remote_engine_id, [(prefill_block_id, decode_block_id)...])`），`AssignmentInstruct` 是单条搬运（`mr_key/target_offset/source_offset/length`），`MigrationAssignment` 是它们组装后的批次。

#### 4.2.3 源码精读

**① 抽象基类与注册表**——定义「一套接口、多个实现」的契约：

[backend/base.py:12-40](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/disagg/backend/base.py#L12-L40)：`MigrationBackendImpl` 用 `@abstractmethod` 规定七个方法，注意 `store`/`load` 目前是抽象占位（两个实现都 `raise NotImplementedError`），为未来的「KV cache 落盘/读盘」预留。

[backend/backend.py:4](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/disagg/backend/backend.py#L4)：`MIGRATION_BACKENDS = Registry('migration_backend', ...)`，配合实现类上的 `@MIGRATION_BACKENDS.register_module(MigrationBackend.DLSlime.name)` 装饰器实现按名注册。

[backend/__init__.py:6-18](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/disagg/backend/__init__.py#L6-L18)：两个 `try/except ImportError` 分别尝试导入 DLSlime、Mooncake，缺依赖则记录日志后跳过——典型的「可选后端按环境启用」模式。

**② DLSlime 后端**——基于 RDMA `read` 的点对点搬运：

[backend/dlslime.py:24-46](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/disagg/backend/dlslime.py#L24-L46)：`DLSlimeMigrationManagement` 管理单条连接，构造时按 `rank % len(nics)` 选网卡创建 `RDMAEndpoint`；若协议是 NVLink，则尝试 `NVLinkEndpoint`（未编译则回退 `RDMAEndpoint`）。

[backend/dlslime.py:59-73](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/disagg/backend/dlslime.py#L59-L73)：核心搬运方法 `p2p_migrate` 把每条指令展开成 5 元组，调 `endpoint.read(batch)` 一次性发起整批 RDMA 读请求，再用 `future.wait()` 等完成；环境变量 `LMDEPLOY_USE_ASYNC_MIGRATION` 控制是否丢进线程池异步等待。注意是 **`read`**——即 decode 端主动去 prefill 端的显存里「拉」KV。

[backend/dlslime.py:76-102](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/disagg/backend/dlslime.py#L76-L102)：`DLSlimeBackend` 用 `links: dict[remote_engine_id, DLSlimeMigrationManagement]` 管理到多个对端的连接，方法都转发给对应 link。

**③ Mooncake 后端**——更高层的 transfer engine 抽象：

[backend/mooncake.py:24-47](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/disagg/backend/mooncake.py#L24-L47)：`get_rdma_nics()` 通过 `ibv_devices` 命令枚举本机 RDMA 网卡——这是与 DLSlime（用 `dlslime.available_nic()`）不同的本机探测方式。

[backend/mooncake.py:197-234](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/disagg/backend/mooncake.py#L197-L234)：`_migrate` 逐条指令计算「本端地址 = 本端 buffer 基址 + source_offset」「对端地址 = 对端 buffer 基址 + target_offset」，调 `engine.transfer_sync_read(remote_url, local_addr, remote_addr, length)` 完成同步搬运。两套后端最终都落到「远端读」语义，只是上层抽象粒度不同。

#### 4.2.4 代码实践

**实践目标**：对比两套传输后端，理解「同一接口、不同实现」。

**操作步骤**：

1. 打开 `backend/base.py`，列出 `MigrationBackendImpl` 的全部抽象方法名。
2. 并排打开 `dlslime.py` 与 `mooncake.py`，对比它们如何实现 `register_memory_region` 与 `p2p_migrate`。
3. 在 `dlslime.py` 中找到 `@MIGRATION_BACKENDS.register_module(...)` 装饰器，确认注册名取自 `MigrationBackend` 枚举。

**需要观察的现象**：DLSlime 用 `dlslime.RDMAEndpoint.read(batch)` 批量发起；Mooncake 逐条调 `transfer_sync_read`。两者的「地址计算」都在 offset 层面，不碰业务语义。

**预期结果**：能用一句话指出「backend 层只负责按 `AssignmentInstruct` 搬字节，不知道也不关心搬的是哪层、哪个 token 的 KV」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 backend 用「注册表 + try/except 导入」而不是直接 `import`？
**答案**：因为 dlslime 与 mooncake 都是**可选依赖**（需要特定硬件/驱动），用注册表让运行时按名字切换，用 try/except 让没装该库的环境自动禁用对应后端而不报错。

**练习 2**：`p2p_migrate` 用的是 RDMA `read`（拉）还是 `write`（推）？由谁发起？
**答案**：用 `read`，由 **decode 端**主动从 prefill 端显存「拉」KV 过来（见 dlslime `endpoint.read`、mooncake `transfer_sync_read`）。这种「接收方主动拉」的设计便于 decode 端按自己的 block 布局写入正确位置。

---

### 4.3 连接管理 disagg conn

#### 4.3.1 概念说明

backend 负责「搬字节」的数据面，但它不会自己建立连接——谁来选网卡、谁来注册显存、谁来交换寻址信息、谁来通知对端「我搬完了你可以释放缓存」？这些都是 **conn 模块（控制面）** 的职责。

conn 模块分两层：

- **`engine_conn.py`（引擎侧）**：每台引擎持有一个 `EngineP2PConnection`，负责本端的 zmq 控制信道 + 委托 backend 做数据面握手 + 处理对端的缓存释放通知。
- **`proxy_conn.py`（代理侧）**：proxy 持有一个 `PDConnectionPool`，作为「总导演」编排 prefill 与 decode 两台引擎之间的三步握手。

一句话区分：**conn 负责建立和维护 P2P 连接（控制面），backend 负责在已建立的连接上搬运 KV（数据面）**。conn 在握手阶段会调用 backend 的 `p2p_initialize/register_memory_region/p2p_connect`，握手完成后才轮到 `p2p_migrate` 上场。

#### 4.3.2 核心流程

**控制面握手的三步舞**（由 proxy 的 `PDConnectionPool.connect()` 编排，见 [proxy_conn.py:156-208](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/disagg/conn/proxy_conn.py#L156-L208)）：

```text
proxy 向 prefill、decode 各发 HTTP：
  Step 0  GET /distserve/engine_info          → 取回双方的 DistServeEngineConfig（校验 tp_size 一致）
  Step 1  POST /distserve/p2p_initialize      → 各方建 zmq socket + backend.p2p_initialize
                                            + 注册本端 KV 显存为 MR
                                            → 返回 zmq_address 与 kvtransfer_endpoint_info
  Step 2  POST /distserve/p2p_connect         → 各方 zmq PULL connect + backend.p2p_connect
                                            → 建立数据面连接，启动 zmq 接收任务
连接进入 Connected 态，后续请求可携带 migration_request 触发数据面搬运
```

握手建立后，**数据面搬运**才在 decode 引擎的 `migration_loop` 中发生：decode 端按 `migration_request` 里的 `remote_block_ids`（prefill 端块号）与本端 `decode_block_ids` 配对，生成 `MigrationExecutionBatch`，经 `executor.migrate` → `cache_engine.migrate` → `backend.p2p_migrate` 完成搬运；搬运完通过 **zmq 控制信道**给 prefill 端发 `DistServeCacheFreeRequest`，通知它释放这部分缓存（见 [engine_conn.py:73-87](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/disagg/conn/engine_conn.py#L73-L87)）。

连接是**惰性建立**的（lazy）：不在引擎启动时建好，而是在第一个真实请求到达时才由 proxy 按需握手（见 `PDConnectionPool` docstring，[proxy_conn.py:51-64](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/disagg/conn/proxy_conn.py#L51-L64)）。

#### 4.3.3 源码精读

**① 协议数据模型 protocol.py**——控制面消息的「契约」：

[conn/protocol.py:14-27](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/disagg/conn/protocol.py#L14-L27)：`MigrationProtocol` 枚举（TCP/RDMA/NVLINK），注释明确「目前仅支持 GPU Directed RDMA」。这是真正生效的传输协议枚举（与 config 里的占位配置互补）。

[conn/protocol.py:36-69](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/disagg/conn/protocol.py#L36-L69)：`DistServeInitRequest`（握手入参：双方引擎 id/config、协议、rank、各类 config）与 `DistServeInitResponse`（握手回包：`engine_endpoint_info` 即控制面 zmq 地址、`kvtransfer_endpoint_info` 即数据面寻址信息）。响应里的 `kvtransfer_endpoint_info` 用 `str` 存放——注释说明这是为了**通用性**（RDMA、NVLink 等不同介质的寻址信息格式不同，统一用字符串承载，由 backend 自行解析）。

[conn/protocol.py:83-91](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/disagg/conn/protocol.py#L83-L91)：`MigrationRequest` 是**业务请求里夹带的迁移指令**：`remote_engine_id/remote_session_id/remote_token_id/remote_block_ids`（prefill 端的 KV 定位信息），`is_dummy_prefill` 标记是否跳过真实 prefill（用于压测 decode）。这条消息由 proxy 构造、塞进 decode 请求里，是「数据面搬运的触发器」。

**② 引擎侧连接 engine_conn.py**——控制面 zmq 信道 + 委托数据面：

[conn/engine_conn.py:30-58](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/disagg/conn/engine_conn.py#L30-L58)：`EngineP2PConnection` 持有按 `remote_engine_id` 索引的 zmq context/sender/receiver。`p2p_initialize` 建一个 `zmq.PUSH` socket 并 `bind` 到本机端口（控制面发送信道），再调 `engine.executor.p2p_initialize(init_request)`——这一步会下钻到 `cache_engine.p2p_initialize`，真正创建 backend 并把整块 KV 显存注册为 MR，返回数据面寻址信息。

[conn/engine_conn.py:60-66](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/disagg/conn/engine_conn.py#L60-L66)：`p2p_connect` 用对端返回的 zmq 地址 `connect` 一个 `zmq.PULL`（控制面接收信道），并创建常驻任务 `handle_zmq_recv` 监听对端消息。

[conn/engine_conn.py:73-87](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/disagg/conn/engine_conn.py#L73-L87)：`zmq_send` 发送 `DistServeCacheFreeRequest`；`handle_zmq_recv` 收到后调用 `engine.end_session`——这就是「decode 搬完 KV 后通知 prefill 释放缓存」的控制面回路。

**③ 代理侧连接池 proxy_conn.py**——三步握手总导演：

[conn/proxy_conn.py:51-97](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/disagg/conn/proxy_conn.py#L51-L97)：`PDConnectionPool` 维护 `prefill_endpoints`/`decode_endpoints` 两个集合、`pool: dict[(p_url,d_url), PDConnectionState]` 连接表，以及 `migration_session_shelf`（迁移中会话的容错登记簿，decode 实例崩溃时据此 gc prefill 端缓存）。`CONN_SEMAPHORE_SIZE = 2048` 限制最大并发连接数。

[conn/proxy_conn.py:125-263](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/disagg/conn/proxy_conn.py#L125-L263)：`connect()` 是握手核心，内部 `conn_worker` 协程执行前面流程图的三步：取双方 engine_config（`assert tp_size 相等`）→ 双向 `p2p_initialize` → 双向 `p2p_connect`。外层用 `max_retry_cnt = 8` 重试，连接状态机在 Disconnected/Connecting/Connected 间流转，重复请求通过 `PDConnectionState.event` 合并等待。

[conn/proxy_conn.py:271-305](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/disagg/conn/proxy_conn.py#L271-L305)：`drop()` 在 decode 实例掉线时触发——先对登记在册的会话发 `distserve/free_cache` 做 gc，再发 `distserve/p2p_drop_connect` 断开 P2P，最后从 pool 移除。这是 PD 分离的**容错与回收**逻辑。

#### 4.3.4 代码实践

**实践目标**：理清控制面握手的三步，并定位「谁调谁」。

**操作步骤**：

1. 打开 `conn/proxy_conn.py` 的 `connect` 方法，找到三个内部函数 `get_engine_config`、`p2p_initialize`、`p2p_connect`，看它们各自 `POST`/`GET` 的 `/distserve/*` 路径。
2. 打开 `serve/openai/api_server.py` 的 [distserve 路由（约 1300-1335 行）](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/openai/api_server.py#L1300-L1335)，确认这些路由都转交给 `VariableInterface.async_engine.p2p_initialize/p2p_connect/...`。
3. 在 `engine.py` 的 [p2p 代理方法（486-493 行）](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine.py#L486-L493)看到它们又转交给 `self.engine_conn`，从而闭合「proxy → HTTP → api_server → Engine → engine_conn → executor → cache_engine → backend」调用链。

**需要观察的现象**：控制面消息走 HTTP（握手）+ zmq（运行时小通知），数据面 KV 字节走 backend 的 RDMA read——两条信道完全分离。

**预期结果**：能指出「conn 模块负责建立与维护 P2P 连接（控制面），并在握手时调用 backend 注册显存、交换寻址信息；握手完成后由 backend 负责实际搬运（数据面）」。

#### 4.3.5 小练习与答案

**练习 1**：`DistServeInitResponse` 里为什么用 `str` 类型存 `endpoint_info`，而不是结构化字段？
**答案**：为了通用性——不同介质（RDMA 的 qp 信息、NVLink 的句柄等）寻址格式不同，统一用字符串承载、交由对应 backend 自行 `json.loads` 解析（见 protocol.py 第 66-69 行注释与 dlslime.py `connect` 的 `json.loads`）。

**练习 2**：为什么连接是「惰性建立」而非引擎启动时就建好？
**答案**：因为引擎启动时还不知道会有哪些 prefill/decode peer、也不必为没有的连接白白占资源；按需在首个请求时建连接，能简化部署（见 proxy_conn.py docstring 第 56-58 行）。代价是首请求会有握手延迟，可用 `/distserve/connection_warmup` 预热。

---

### 4.4 全链路串联（衔接三模块）

> 本节不是独立的「最小模块」，而是把 config/backend/conn 与引擎、代理接起来，帮你完成实践任务里的「画数据流」。它依赖前述三节。

把三模块拼成端到端流程，关键接入点如下：

1. **请求分流**：proxy 收到请求后按 `ServingStrategy` 分支（[proxy.py:669](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/proxy/proxy.py#L669)）。DistServe 分支先把 prefill 请求的 `max_tokens=1, stream=False, with_cache=True, preserve_cache=True` 打到 prefill 引擎（[proxy.py:672-690](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/proxy/proxy.py#L672-L690)），拿到 `cache_block_ids`/`remote_token_ids` 等定位信息。

2. **惰性握手**：若 `pd_connection_pool.is_connected(p_url, d_url)` 为假，调 `connect(PDConnectionMessage(...))` 走 4.3 节的三步握手（[proxy.py:698-706](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/proxy/proxy.py#L698-L706)）。

3. **构造迁移指令并打 decode**：把 prefill 的定位信息包成 `MigrationRequest` 塞进 decode 请求（[proxy.py:712-718](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/serve/proxy/proxy.py#L712-L718)）。注意 prefill 引擎被 `EngineRole.Prefill` 强制截断 `max_new_tokens=1`（[engine.py:436-437](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine.py#L436-L437)）。

4. **decode 端触发迁移循环**：`_add_message` 收到带 `migration_request` 的请求后 `self.migration_event.set()`（[engine.py:454-466](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine.py#L454-L466)），唤醒 `migration_loop`。该循环**仅在 `role != Hybrid` 时启动**（[engine_loop.py:626-628](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine_loop.py#L626-L628)）。

5. **生成搬运批次**：`_migration_loop_migrate` 把 prefill 块号与 decode 块号一一配对成 `MigrationExecutionBatch`，交 `executor.migrate`（[engine_loop.py:536-562](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine_loop.py#L536-L562)）。

6. **数据面搬运**：`cache_engine.migrate` 按「层步长 × 块步长」把每对块展开成 `AssignmentInstruct` 列表，调 `migration_backend_impl.p2p_migrate`（[cache_engine.py:478-511](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/cache_engine.py#L478-L511)）。其中 backend 实例是在首次 `p2p_initialize` 时按 `cache_config.migration_backend.name` 从注册表取的（[cache_engine.py:454-456](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/cache_engine.py#L454-L456)）。

7. **控制面回收**：搬运完 `zmq_send` 通知 prefill 释放缓存（[engine_loop.py:561-562](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine_loop.py#L561-L562)），`_migration_loop_get_outputs` 产出 decode 的首 token（[engine_loop.py:564-583](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/engine_loop.py#L564-L583)）。

> 数学补充：单块 KV 字节数与 u4-5 一致——若模型有 \(L\) 层、block 含 \(B\) 个 token、每层 KV 头数为 \(h_v\)、每头维度 \(d\)、dtype 占 \(s\) 字节，则单 block 字节约为 \(2 \cdot L \cdot B \cdot h_v \cdot d \cdot s\)（K 与 V 各一份）。`cache_engine.migrate` 里的 `assignment_len` 与 `layer_stride` 正是这个估算的代码化：`assignment_len = element_size() * size(-1)`（单层单块字节数）、`layer_stride = num_gpu_blocks * assignment_len`（跨一层的步长），用于把 `(layer, block)` 二维坐标压平成一维偏移。

## 5. 综合实践

**实践任务**：阅读 `disagg/README.md` 与 `config.py`，画出「prefill 节点 → KV 传输 → decode 节点」的完整数据流，并说明 conn 模块负责哪一段通信。

**步骤**：

1. **读部署文档**：打开 [disagg/README.md](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/disagg/README.md)，记录三个进程的角色与端口：router(proxy) `:8000`、prefill `:23333`（`--role Prefill`）、decode `:23334`（`--role Decode`）。注意 prefill/decode 都带 `--proxy-url` 注册到 proxy。

2. **画三张图**（建议手绘或用文本框）：
   - **部署图**：用户 → proxy(8000) → {prefill(23333, GPU0), decode(23334, GPU1)}。
   - **握手时序图**：proxy 同时向 prefill、decode 发 `GET engine_info` → 双向 `POST p2p_initialize` → 双向 `POST p2p_connect`，标注每一步在 conn 的哪个函数（`get_engine_config`/`p2p_initialize`/`p2p_connect`）。
   - **单请求 KV 迁移图**：proxy 打 prefill（max_tokens=1，保留缓存）→ 拿 cache_block_ids →（必要时握手）→ 带 `MigrationRequest` 打 decode → decode 的 `migration_loop` 调 `backend.p2p_migrate`（RDMA read）→ zmq 通知 prefill free cache → decode 流式吐 token。

3. **回答 conn 的职责**：在你的图上用两种颜色区分——**控制面（conn 负责）**：HTTP 握手（engine_info/p2p_initialize/p2p_connect）+ zmq 运行时通知（cache free）+ 连接池状态机与容错回收；**数据面（backend 负责）**：RDMA/NVLink 上的 KV 字节搬运。明确写下「conn 不搬 KV 字节，它只建立/维护连接并在握手时调用 backend 注册显存、交换寻址信息」。

4. **核对约束**：在 README 找到「only Pytorch backend supports PD Disaggregation」与「NVLink/RDMA 二选一、RDMA 默认」两句话，标注到图上。

**预期结果**：得到一张能向同事讲清「为什么有 conn 和 backend 两层、它们各干什么」的图。**待本地验证**：若有双卡 + RDMA 环境，可按 README 跑通 internlm2_5-7b-chat 的 DistServe 示例，用 `LMDEPLOY_LOG_LEVEL=DEBUG` 观察握手与迁移日志；无硬件环境则本任务为「源码阅读型实践」，以上画图即完成。

## 6. 本讲小结

- PD 分离（DistServe）把计算密集的 prefill 与访存密集的 decode 拆到两组引擎，prefill 后把 KV cache 迁移到 decode，避免重复 prefill、互不干扰；**仅 PyTorch 后端支持**。
- **config 层**用枚举固化词汇表：`ServingStrategy`（Hybrid/DistServe）、`EngineRole`（Hybrid/Prefill/Decode，技术上都是 hybrid 引擎但需标记角色）、`MigrationBackend`（DLSlime/Mooncake）、`MigrationProtocol`（仅 GDR RDMA 真正实现），并用 `DistServeEngineConfig` 在握手时交换并行度与缓存布局以算偏移、推 worker 连接对。
- **backend 层**是数据面：实现 `MigrationBackendImpl` 七方法，按 `MigrationAssignment`（逐层逐块的 `AssignmentInstruct`）搬运 KV 字节；两套后端都用「decode 端 RDMA read 主动拉」语义，靠 mmengine 注册表 + try/except 可选导入切换。
- **conn 层**是控制面：`protocol.py` 定义握手/迁移消息契约，`engine_conn.py` 提供每引擎的 zmq 信道 + 委托 backend 握手 + 处理 cache-free 通知，`proxy_conn.py` 的 `PDConnectionPool` 编排三步握手（engine_info → p2p_initialize → p2p_connect）、惰性建连、带容错回收。
- 控制面（HTTP + zmq）与数据面（RDMA/NVLink）完全分离：conn 建立/维护连接并在握手时调 backend 注册显存，握手完成后 backend 负责实际搬运。
- 端到端闭环：proxy 分流 → 惰性握手 → prefill 产 KV 定位信息 → decode 的 `migration_loop`（仅 `role != Hybrid` 启动）触发 `cache_engine.migrate` → backend 搬运 → zmq 通知释放 → decode 吐首 token。

## 7. 下一步学习建议

- **回看迁移循环细节**：结合 u4-2 的 EngineLoop 四协程模型，重读 `engine_loop.py` 的 `migration_loop` 与 `_schedule_migration`，理解迁移如何与 prefill/decode 调度互不阻塞。
- **深入 KV 偏移数学**：对照 u4-5 的 Paged Attention 物理块布局，手算 `cache_engine.migrate` 里 `assignment_len`/`layer_stride`/`remote_layer_stride` 的几何含义，理解为什么必须交换双方的 `num_gpu_blocks`。
- **扩展阅读 proxy 容错**：u8-4 讲了 proxy 的节点管理与路由策略，本讲的 `PDConnectionPool.drop()` 与 `migration_session_shelf` 正是其 PD 专属容错补充，可对照阅读。
- **关注演进**：`store`/`load` 抽象方法、TCP/NVLink 配置、`/distserve/gc` 路由目前都是 TODO/NotImplemented，是后续版本可能的扩展方向，建议跟进 `lmdeploy/pytorch/disagg/` 的 commit。
