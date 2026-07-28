# RDMA 传输层抽象：Mooncake 与 NIXL

## 1. 本讲目标

本讲聚焦 PD（prefill-decode）分离架构的**数据平面**：KV 状态究竟用什么机制、走什么协议从 vLLM prefill 节点搬到 TileRT decode 节点。承接 u4-l3 讲清的「控制平面走 TCP 传小 JSON、数据平面走 RDMA 传 KV 字节」的双轨设计，本讲专门拆开数据平面这一侧的 `transport.py`。

学完后你应该能够：

1. 说清 `Transport` 抽象基类为什么存在、它定义了哪四个方法、为什么框架代码可以对此完全无感知。
2. 区分两种实现——`MooncakeTransport`（TransferEngine + 同步批量写）与 `NixlTransport`（UCX 后端 + VRAM 描述符 + 轮询）——在注册、握手、写完成语义上的不同。
3. 跟踪 `register` / `local_meta` / `write` 三个方法在 prefill 侧与 decode 侧的配合，理解「接收端先注册地址、握手交换元数据、发送端按元数据写」这一通用 RDMA 协作模式。
4. 解释为什么 `hello` 中的 `transport` 字段必须两端严格一致，以及多 NIC 主机上为什么要用 `UCX_NET_DEVICES` 绑定 NIXL 到 RDMA 网卡。

---

## 2. 前置知识

本讲默认你已经掌握 u4-l1 到 u4-l4 的内容，这里只补充三个本讲特有的概念。

### 2.1 什么是 RDMA

RDMA（Remote Direct Memory Access，远程直接内存访问）允许一台机器的网卡**绕过操作系统内核与 CPU**，直接读写另一台机器显存/内存里某段字节。关键好处是低延迟、低 CPU 占用：发送端只需告诉网卡「从地址 A 起、写 N 字节、到对端地址 B」，网卡硬件（如 NVIDIA ConnectX 的 mlx5）自己完成搬运。本讲的两种 transport 都是建立在 RDMA 之上的封装。

### 2.2 GPU 显存指针与 VRAM 区域

PyTorch 张量分配在 GPU 显存（VRAM）上时，有一块连续的物理区间。`tensor.data_ptr()` 返回这块区间的起始地址（一个整数）。RDMA 引擎要搬运 GPU 显存，必须拿到这个整数地址加上字节长度，把它注册给网卡，网卡才知道「允许直接读写哪段显存」。NIXL 文档里把 GPU 显存区域叫 **VRAM**（区别于主存 DRAM），本讲沿用此术语。

### 2.3 控制平面 vs 数据平面（承接 u4-l3）

- **控制平面**：传小消息（握手、请求元数据、完成通知），走 **TCP + 长度前缀 JSON 帧**（`send_msg`/`recv_msg`），可靠但慢。
- **数据平面**：传 KV 状态这种大块字节，走 **RDMA**，快但不自己传语义。

`Transport` 类只管数据平面，它对控制平面一无所知。两者在 `_send` 里被串成一次完整的发送（先 TCP 握手拿地址，再 RDMA 写字节）。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `tilert/pd_vllm/transport.py` | 本讲主角：`Transport` 抽象基类 + `MooncakeTransport` + `NixlTransport` + `make_transport` 工厂。 |
| `tilert/pd_vllm/wire.py` | 控制平面协议：`hello_msg` 信封把 transport 名字与元数据塞进握手消息。 |
| `tilert/pd_vllm/prefill_connector.py` | 发送侧：`_ensure_worker_ready` 初始化 transport、`_send` 用 transport 发数据。 |
| `tilert/pd_vllm/receive_server.py` | 接收侧：`__init__` 初始化 transport、把 `local_meta()` 放进 hello。 |
| `tilert/pd_vllm/profiles/mla_nsa.py` | `rdma_plan` / `hello_layout` / `buffer_bytes`：把模型相关计算交给 profile，transport 本身保持模型无关。 |
| `README.md` | PD 部署命令、`UCX_NET_DEVICES` 绑定实践。 |

> 说明：本讲引用的 `transport.py` 一共只有 109 行，是整个 PD 模块里最小、最自洽的一个文件，非常适合作为理解「抽象 + 多后端 + 工厂」设计的范本。

---

## 4. 核心概念与源码讲解

### 4.1 Transport 抽象基类与工厂：数据平面的统一接口

#### 4.1.1 概念说明

PD 分离要在两节点间搬大块 KV 字节。理论上可以用很多种 RDMA 引擎（Mooncake、NIXL、未来还可能加别的）。如果接收服务、发送连接器里到处直接调用某个引擎的 API，换引擎就得改一堆地方。

`Transport` 基类就是为了把这个变化点**收口到一处**：它定义一个最小的、与引擎无关的四个方法契约，框架（`prefill_connector` / `receive_server`）只对着基类编程，具体引擎藏在两个子类里，由 `make_transport` 工厂按名字实例化。这正是 u4-l1 讲过的「模型差异收口于 `ModelProfile`」思路在传输层的镜像——**引擎差异收口于 `Transport`**。

基类定义了哪四个方法（即一个 RDMA 引擎被框架使用的完整生命周期）：

| 方法 | 调用时机 | 作用 |
| --- | --- | --- |
| `init(host)` | 进程启动，建连接引擎 | 创建引擎实例、完成本地监听/握手准备 |
| `register(ptr, nbytes, dev_id)` | 显存缓冲分配后 | 把一段本地显存地址注册给引擎（允许被 RDMA 读写） |
| `local_meta()` | 握手前 | 返回一段「能让对端找到我」的连接元数据 |
| `write(remote_meta, srcs, dsts, lens)` | 真正发数据时 | 按对端的元数据，把本地若干段字节写到对端指定地址 |

注意这四个方法刻意**不出现任何模型概念**（不知道 MLA、不知道 KI/PE 平面），也**不出现任何控制平面概念**（不建 TCP、不发 JSON）。`srcs/dsts/lens` 是纯地址三元组，谁算出来的？是 `ModelProfile.rdma_plan`（见 4.4）。这种「接口越窄、复用越广」的分层是本讲最重要的设计直觉。

#### 4.1.2 核心流程

一个 transport 实例在进程内的生命周期（以接收侧 decode 节点为例）：

```
ReceiveServer.__init__
   ├─ make_transport("nixl" 或 "mooncake")   # 工厂选实现
   ├─ transport.init(hostname)                # ① 建引擎
   ├─ transport.register(base_ptr, total, 0)  # ② 注册接收缓冲整块显存
   ├─ transport.local_meta()                  # ③ 取连接元数据
   │      └─ 塞进 hello_msg，等发送端连进来
   └─ （后续每个请求）发送端 transport.write(hello, srcs, dsts, lens)  # ④ 对端写进来
```

发送侧 prefill 节点对称地做 ①②③，只是它注册的是「暂存缓冲 staging」而非接收缓冲，且在 ④ 里它是主动写的一方。

#### 4.1.3 源码精读

基类本身就是四个桩方法，体量极小却定义了整个数据平面的契约：

[transport.py:9-15](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/transport.py#L9-L15) —— `Transport` 抽象基类：定义 `init/register/local_meta/write` 四方法，`name = "?"` 是后端标识，子类覆盖。

注意这里没有用 `abc.ABC` + `@abstractmethod`，而是用 `...` 桩方法。这意味着：基类本身可实例化（不会抛错），但调用桩方法什么都不做。框架靠的是「工厂只会造出 `MooncakeTransport` / `NixlTransport` 两个具体子类」来保证方法被正确实现，而不是靠运行时抽象校验。这是一种**鸭子类型**而非严格抽象基类的选择，与同模块 `ModelProfile` 用 `typing.Protocol` 的风格略有不同。

工厂是这个收口模式的收口点：

[transport.py:102-109](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/transport.py#L102-L109) —— `_BACKENDS` 字典注册两实现，`make_transport` 按名查表；缺省 `mooncake`，未知名字抛 `ValueError`。

`make_transport` 的两个细节值得记住：

1. **缺省值 `mooncake`**：传 `None` 或空字符串时落到 `mooncake`。这也是为什么 README 的命令里 prefill 侧 `"tilert_transport"` 与 decode 侧 `--transport` 两个开关都不写时不会报错——会默认用 mooncake。
2. **大小写归一 `.lower()`**：`"NIXL"`、`"Nixl"`、`"nixl"` 都能匹配，容错命令行输入。

#### 4.1.4 代码实践

实践目标：在不依赖 RDMA 硬件的前提下，验证 `make_transport` 的工厂行为与未知后端的报错路径。

操作步骤：

1. 在已安装 `tilert` 的容器里（无需 GPU），打开 Python REPL。
2. 执行 `from tilert.pd_vllm.transport import make_transport, _BACKENDS`。
3. `print(sorted(_BACKENDS))` 观察注册表内容；预期得到 `['mooncake', 'nixl']`。
4. `t = make_transport("nixl"); print(t.name)` 预期输出 `nixl`。
5. `make_transport("rdma")` 预期抛 `ValueError: unknown transport 'rdma'; choices: ['mooncake', 'nixl']`。

需要观察的现象：步骤 4 不需要任何 RDMA 硬件——`make_transport` 只 `return _BACKENDS[key]()` 实例化对象，**不调用 `init`**，因此不触发 `from nixl._api import ...` 这种重依赖。只有显式调 `t.init(...)` 才会去 import 真正的 RDMA 库。

预期结果：你能在普通容器里完成工厂层面的全部验证，而 RDMA 相关的 import 是延迟到 `init` 才发生的——这是「懒加载」思想在 transport 层的体现，与 u1-l3 讲的后端懒加载一脉相承。

#### 4.1.5 小练习与答案

**练习 1**：如果团队要新增第三种 RDMA 后端 `ibex`，需要改 `transport.py` 里的哪些地方？

**答案**：只需新增一个 `class IbexTransport(Transport)` 实现四方法，再在 `_BACKENDS` 字典加一项 `"ibex": IbexTransport`。框架代码（`prefill_connector` / `receive_server`）与 `wire.py` 都不用改——这正是抽象收口的价值。

**练习 2**：为什么 `make_transport` 用 `.lower()` 归一化大小写，而 `wire.py` 的 `hello_msg` 里 `transport` 字段不做同样处理？

**答案**：`make_transport` 接的是人写的命令行/配置，容错优先；`hello_msg` 里的 `transport` 字段是程序自己填的 `self._transport.name`（固定小写 `"mooncake"`/`"nixl"`），不需要再容错，且两端都来自同一份常量，自然相等。

---

### 4.2 MooncakeTransport：TransferEngine + 同步批量写

#### 4.2.1 概念说明

Mooncake（月饼）是 Moonshot AI（Kimi）开源的高性能 KVCache 与传输引擎，因 SGLang 等推理框架率先用于 PD 分离传输而成为业界事实标准之一。`MooncakeTransport` 的 docstring 明确写「serve_sglang precedent」——**借鉴 SGLang 的先例**，用 Mooncake 的 `TransferEngine` 做单引擎 + P2P 握手 + 同步写。

它是 TileRT 的**默认后端**（`make_transport(None)` 落到这里），也是设计上最直白的一个：把「建引擎、注册显存、换 session_id、批量同步写」四步一对一映射到 Mooncake 的 C++/Python API。

#### 4.2.2 核心流程

```
init(host):
    engine = TransferEngine()
    engine.initialize(host, "P2PHANDSHAKE", "rdma", "")   # P2P 握手模式 + rdma 传输
    session_id = f"{host}:{engine.get_rpc_port()}"         # 本端唯一标识

register(ptr, nbytes, dev_id):
    engine.batch_register_memory([ptr], [nbytes])          # 把这段显存交给引擎托管

local_meta():
    return {"session_id": self.session_id}                 # 只交换一个字符串

write(remote_meta, srcs, dsts, lens):
    engine.batch_transfer_sync_write(
        remote_meta["session_id"], srcs, dsts, lens)       # 一次性批量、同步写完
```

关键特征：

1. **P2P 握手**：`initialize` 的第二个参数 `"P2PHANDSHAKE"` 表示两端用点对点握手建立连接，不依赖中心化的 session 服务。
2. **session_id 极简**：连接元数据只有一个字符串 `{host}:{rpc_port}`，对端拿到它就能找到这个引擎。
3. **同步批量写**：`batch_transfer_sync_write` 一次性提交 `(srcs, dsts, lens)` 三组并行列表，**调用返回即表示全部写完**——引擎内部已经阻塞到完成。这是它和 NIXL 轮询模型最大的区别（详见 4.3 与综合实践）。

#### 4.2.3 源码精读

[transport.py:18-43](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/transport.py#L18-L43) —— `MooncakeTransport` 全貌：四方法各 2–4 行，全部委托给 `engine`。

逐行要点：

- 第 26–30 行 `init`：`TransferEngine()` 是 Mooncake 的核心对象；`initialize` 返回 `0` 表示成功，非 0 抛 `RuntimeError`。`session_id` 把 `host` 与引擎自报的 RPC 端口拼起来作为全局唯一标识。
- 第 32–35 行 `register`：注意它**忽略 `dev_id` 参数**（只把 `ptr`/`nbytes` 传下去）。Mooncake 在 `initialize` 时已经绑定了设备，注册阶段不再关心是哪块卡。
- 第 40–43 行 `write`：`batch_transfer_sync_write(session_id, srcs, dsts, lens)` 是一个**自带阻塞**的调用——返回后字节已落到对端显存。`srcs` 是本地暂存缓冲里的若干源地址，`dsts` 是对端接收缓冲里的若干目标地址，`lens` 是每段长度，三者等长。

> 重要：这里 `srcs/dsts/lens` 是「三组并行列表」，对一段连续区域而言 `srcs=[a]`, `dsts=[b]`, `lens=[N]`；对多段（如每层 KV 一段）则是多元素列表。Mooncake 支持一次性批量提交多段，减少提交次数。

#### 4.2.4 代码实践

实践目标：阅读 `rdma_plan`，理解 `srcs/dsts/lens` 这三组列表是怎么算出来的，从而明白 `write` 收到的实参含义。

操作步骤：

1. 打开 `tilert/pd_vllm/profiles/mla_nsa.py`，定位 `rdma_plan`（[mla_nsa.py:323-342](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/profiles/mla_nsa.py#L323-L342)）。
2. 观察它对 `num_layers`（如 GLM-5 是 121 层）每一层都 `append` 三段：KV、PE、KI，分别对应本地的源地址（`base + ...`）、对端的目标地址（`hello["kv_base"] + ...` 等）、长度。
3. 数一下：对一个 121 层模型，单次 `write` 的 `srcs/dsts/lens` 各会有 \( 3 \times 121 = 363 \) 个元素。

需要观察的现象：`srcs` 完全由本地暂存缓冲基址 `base` 与 profile 的布局常量算出（不依赖网络）；`dsts` 则**依赖握手收到的 hello**里的 `kv_base/pe_base/ki_base`——这正是「接收端先在 hello 里公告自己的地址，发送端据此写」的协作模式（4.4 详述）。

预期结果：你能口头复述「一次 `write` 把 363 段、每段几百 KB 到几 MB 的字节，从 prefill 暂存缓冲批量同步写到 decode 接收缓冲的对应平面」。

#### 4.2.5 小练习与答案

**练习 1**：`MooncakeTransport.local_meta()` 为什么只返回一个 `session_id` 字符串，而不像 NIXL 那样返回一大段 base64 元数据？

**答案**：Mooncake 的连接建立走 `P2PHANDSHAKE`，两端用 `session_id`（host + rpc_port）互找，引擎内部维护注册过的内存与对端映射，调用方只需告诉它「写到哪个 session」即可，无需把内存布局序列化传出。

**练习 2**：`register` 忽略了 `dev_id`，会不会是 bug？

**答案**：不是。Mooncake 在 `initialize(host, ..., "rdma", "")` 阶段已绑定设备上下文；且当前 PD 部署里接收侧只在 `cuda:0` 注册一次（见 4.4），暂存侧也按 `torch.cuda.current_device()` 选卡，`dev_id` 在 mooncake 实现里确实不参与逻辑，但作为基类契约参数保留，供 NIXL 使用。

---

### 4.3 NixlTransport：UCX 后端 + VRAM 描述符 + 轮询

#### 4.3.1 概念说明

NIXL（NVIDIA Inference Transfer Library）是 NVIDIA 官方的推理传输库，底层默认用 **UCX**（Unified Communication X）框架，再往下走 GPUDirect RDMA。它和 Mooncake 解决同一个问题，但 API 风格迥异：

- Mooncake 把「注册、连接、写」都封进一个 `TransferEngine`，对外只暴露 `session_id` 与同步写。
- NIXL 暴露更底层的「**agent + 描述符 + 传输句柄**」三元组，调用方要自己组装本地描述符、远端描述符，提交后**轮询**直到完成。

`NixlTransport` 选择 NIXL 的现实原因见 README：vLLM 自家的原生 disaggregation 也用 NIXL（`NixlConnector`）。在 Topology B（共享 prefill 同时服务 TileRT decode 与原生 vLLM decode）里，两端都用 NIXL 能「共用一套传输库」，避免同时加载两套 RDMA 运行时。

#### 4.3.2 核心流程

```
init(host):
    agent = nixl_agent(f"{host}:{os.getpid()}", nixl_agent_config(backends=["UCX"]))
    #           ↑ 全局唯一的 agent 名，强制 UCX 后端
    _remotes = {}    # 远端元数据 bytes -> 远端 agent 名 的缓存

register(ptr, nbytes, dev_id):
    agent.register_memory([(ptr, nbytes, dev_id, "")], "VRAM")   # 四元组描述符注册显存

local_meta():
    return {
      "nixl_meta": base64(agent.get_agent_metadata()),   # 序列化本端全部连接信息
      "nixl_dev":  self._dev,
    }

write(remote_meta, srcs, dsts, lens):
    # ① 懒加载远端 agent（同一对端只 add 一次）
    rname = _remotes.get(meta) or agent.add_remote_agent(meta)
    # ② 把 (src,len) 本地地址列表 → NIXL 本地描述符
    ld = agent.get_xfer_descs([(s, n, self._dev) for ...], "VRAM")
    # ③ 把 (dst,len) 远端地址列表 → NIXL 远端描述符
    rd = agent.get_xfer_descs([(d, n, rdev) for ...],    "VRAM")
    # ④ 建传输句柄、提交 WRITE
    h = agent.initialize_xfer("WRITE", ld, rd, rname)
    st = agent.transfer(h)
    # ⑤ 忙轮询直到 DONE 或 ERR
    while st not in ("DONE", "ERR"):
        st = agent.check_xfer_state(h)
        if 超过 _MAX_POLL: raise
    finally: agent.release_xfer_handle(h)
```

五个 NIXL 特有概念：

| 概念 | 含义 |
| --- | --- |
| **agent** | 一个进程的传输端点，名字必须全局唯一（`host:pid`） |
| **VRAM 描述符** | `(ptr, nbytes, dev_id, "")` 四元组，标记一段 GPU 显存 |
| **agent metadata** | agent 自描述的字节流，base64 后塞进 hello，对端据此 `add_remote_agent` |
| **xfer 句柄 (h)** | 一次传输任务的抽象，提交后用它查状态 |
| **轮询完成** | `transfer` 只是提交，真正完成要反复 `check_xfer_state` |

注意 ⑤ 的轮询与 Mooncake 的「返回即完成」是**本质区别**，也是本讲综合实践要对比的核心。

#### 4.3.3 源码精读

[transport.py:46-99](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/transport.py#L46-L99) —— `NixlTransport` 全貌。

几个关键代码点：

- 第 56–62 行 `init`：`nixl_agent(f"{host}:{os.getpid()}", nixl_agent_config(backends=["UCX"]))`——agent 名用 `host:pid` 保证两端不重名（重名会握手失败）；`backends=["UCX"]` 显式钉死走 UCX，这也是为什么多 NIC 绑定要用 `UCX_NET_DEVICES` 这个 **UCX 自己的环境变量**（见 4.5）。
- 第 61 行 `self._remotes: dict[bytes, str]`：缓存「远端 metadata → 远端 agent 名」，避免每次 `write` 都重复 `add_remote_agent`（见 `write` 第 76–79 行的懒加载）。
- 第 64–66 行 `register`：`register_memory([(ptr, nbytes, dev_id, "")], "VRAM")`，注意这里**用了 `dev_id`**（与 Mooncake 不同），因为 NIXL 需要知道这是哪块卡的显存才能走 GPUDirect。
- 第 68–72 行 `local_meta`：`get_agent_metadata()` 返回字节流，`base64.b64encode(...).decode()` 转成可放进 JSON 的字符串。这就是 NIXL 握手比 Mooncake「重」的原因——元数据可能是几 KB 的二进制 blob。
- 第 74–99 行 `write`：核心是 ④⑤ 的「提交 + 轮询」。

重点看轮询段（第 88–99 行）：

[transport.py:88-99](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/transport.py#L88-L99) —— 提交后忙轮询 `check_xfer_state`，状态 ∈ {`DONE`, `ERR`, 其它（进行中）}；超过 `_MAX_POLL=2_000_000` 次判超时；`finally` 释放句柄避免泄漏。

`_MAX_POLL = 2_000_000`（第 54 行）是个保险：理论上大块 KV 传输应在远小于这个轮询次数内完成，超过即认定链路异常。这是忙等（busy-wait）模型的典型缺陷——CPU 会被轮询占满，所以 `write` 应放在后台线程（prefill 侧的 `_sender_loop` 正是如此，见 u4-l2）。

#### 4.3.4 代码实践

实践目标：对比两种 `write` 的同步模型，这是本讲指定的核心实践任务的第一部分。

操作步骤：

1. 并排打开 `MooncakeTransport.write`（[transport.py:40-43](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/transport.py#L40-L43)）与 `NixlTransport.write`（[transport.py:74-99](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/transport.py#L74-L99)）。
2. 在笔记本上用伪代码分别画出两者的「调用 → 返回」时序：
   - Mooncake：`ret = batch_transfer_sync_write(...)` 之后下一行就是 `if ret != 0`——**返回即完成**，引擎内部阻塞。
   - NIXL：`transfer(h)` 之后还要进 `while` 循环反复 `check_xfer_state(h)`——**提交与完成分离**，调用方负责确认。
3. 思考：为什么 NIXL 不把轮询封进 `transfer` 做成同步？提示——这给了调用方「提交后先干别的、稍后再回来 poll」的异步空间；只是当前 `NixlTransport.write` 选择立刻轮询到底。

需要观察的现象：两个 `write` 的**函数签名完全一致**（都收 `remote_meta, srcs, dsts, lens`），这是基类契约的功劳；但内部完成模型完全不同。框架代码 `self._transport.write(hello, srcs, dsts, lens)`（[prefill_connector.py:329](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/prefill_connector.py#L329)）对两种后端是同一行代码。

预期结果：你能用自己的话总结——**Mooncake = 阻塞同步写（一次调用含等待），NIXL = 提交 + 忙轮询（调用方自管完成确认）**。两者对调用方都表现为「函数返回即写完」，但实现路径与 CPU 占用模式不同。

> 待本地验证：若你有 RDMA 硬件，可在两种后端下各跑一次 PD 传输，观察发送线程的 CPU 占用——NIXL 轮询期 CPU 会明显升高。

#### 4.3.5 小练习与答案

**练习 1**：`NixlTransport.write` 里 `self._remotes` 缓存为什么 key 用 `base64decode(remote_meta["nixl_meta"])` 的**字节**，而不是直接用 `remote_meta` 字典？

**答案**：远端 metadata 的字符串形式在 JSON 序列化/反序列化后可能产生不同的字符串对象（但解码出的字节内容相同）；用解码后的字节做 key 能稳定去重，避免同一对端被重复 `add_remote_agent`。

**练习 2**：把 `_MAX_POLL` 调小（比如 100）会怎样？

**答案**：大块 KV（百 MB 级）很可能在 100 次轮询内传不完，于是抛 `RuntimeError("nixl xfer timed out")`——这是误报超时。`_MAX_POLL` 应设得足够覆盖最坏传输时长；当前 `2_000_000` 是个偏保守的上界。

---

### 4.4 两端配合：register / local_meta / write 与 hello 握手

#### 4.4.1 概念说明

这是本讲第二块核心。`Transport` 的四个方法不是孤立存在的，它们在 prefill 与 decode 两端严格配对，靠**控制平面的 hello 握手**把「接收端的连接元数据 + 缓冲地址」交给发送端。整个协作遵循 RDMA 的经典模式：

> **接收端先注册显存、把地址公告出去；发送端拿到地址后，直接往那个地址写。**

这一节回答三个问题：(a) 接收端在 `ReceiveServer.__init__` 里怎么准备好 transport；(b) 发送端在 `_ensure_worker_ready` + `_send` 里怎么用 transport；(c) 中间的 hello 消息承载了什么。

#### 4.4.2 核心流程

```
┌─────────────── decode 节点 (接收端) ───────────────┐
│ ReceiveServer.__init__:                            │
│   transport = make_transport("nixl")               │
│   transport.init(hostname)            ── ① 建引擎 │
│   transport.register(base_ptr, total, 0) ─ ② 注册 │
│   meta = transport.local_meta()       ── ③ 取元数据│
│   hello = hello_msg(transport.name, meta,          │
│                      max_seq_len, layout_version,  │
│                      hello_layout, busy)           │
│   （阻塞等发送端连进来，把 hello 发回去）          │
└────────────────────────────────────────────────────┘
                       │ TCP 控制平面（hello / req / done 三帧）
                       ▼
┌─────────────── prefill 节点 (发送端) ──────────────┐
│ _ensure_worker_ready (一次性):                     │
│   transport = make_transport("mooncake"/"nixl")    │
│   transport.init(hostname); register(staging,...)  │
│                                                    │
│ _send (每个请求):                                  │
│   hello = recv_msg(conn)          ── 收到对端元数据│
│   校验 magic / layout_version / transport / max_seq│
│   srcs,dsts,lens = profile.rdma_plan(hello, ...)   │
│   transport.write(hello, srcs, dsts, lens) ── ④ 写 │
└────────────────────────────────────────────────────┘
```

三个要点：

1. **接收端 `register` 的是整块接收缓冲**（按 `max_seq_len` 预留，见 u4-l3），一次性注册、反复复用；发送端 `register` 的是暂存缓冲 `staging`。
2. **`local_meta()` 只在初始化时调一次**，结果塞进 hello。hello 每次连接都会重发，但内容不变（缓冲地址在进程生命期固定）。
3. **`write` 的 `remote_meta` 参数直接就是收到的 `hello` 字典**——因为 hello 里 `**transport_meta` 已经把 `session_id`（mooncake）或 `nixl_meta`/`nixl_dev`（nixl）展开进去了。这是为什么 `write` 第一个参数命名 `remote_meta` 而非 `hello`：它只需要元数据那部分。

#### 4.4.3 源码精读

**接收侧**（decode）——`ReceiveServer.__init__` 准备 transport 并把它编进 hello：

[receive_server.py:59-67](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/receive_server.py#L59-L67) —— `make_transport` → `init` → `register` → `local_meta` 四连，把 transport 名与元数据备好；`transport` 名来自构造参数（默认 `mooncake`）。

随后 `_handle` 把这些塞进 hello 回给发送端：

[receive_server.py:122-132](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/receive_server.py#L122-L132) —— `hello_msg(self._transport.name, self._transport_meta, ...)` 把 transport 名与元数据打包进握手信封。

hello 信封的结构在 `wire.hello_msg`：

[wire.py:69-92](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/wire.py#L69-L92) —— `transport` 字段放引擎名、`**transport_meta` 展开 mooncake 的 `session_id` 或 nixl 的 `nixl_meta`/`nixl_dev`、`**layout` 放接收缓冲各平面的基地址（`kv_base`/`pe_base`/`ki_base`）。

**发送侧**（prefill）——先一次性初始化 transport，再每个请求用它写：

[prefill_connector.py:225-227](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/prefill_connector.py#L225-L227) —— `_ensure_worker_ready` 里 `make_transport` → `init` → `register(staging)`，与接收侧对称。

注意第 81 行 `self._transport_name = extra.get("tilert_transport", "mooncake")`：发送端引擎名来自 `--kv-transfer-config` 里的 `tilert_transport` 字段，缺省 `mooncake`。

发送侧 `_send` 收到 hello 后，**先做四项校验**，再算 rdma_plan，最后 write：

[prefill_connector.py:307-329](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/prefill_connector.py#L307-L329) —— 校验 `transport` 字段两端一致（307-310）、`seq` 不超对端 `max_seq_len`（311-312）；`rdma_plan` 用 hello 里的基地址算 `(srcs,dsts,lens)`（326-328）；`transport.write(hello, ...)` 把 hello 当 `remote_meta` 直接传（329）。

第 307–310 行正是本讲指定实践任务的第二问的答案所在：

[prefill_connector.py:307-310](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/prefill_connector.py#L307-L310) —— 断言 `hello["transport"] == self._transport.name`，两端引擎不一致直接 `AssertionError`。

为什么必须一致？因为 `write(remote_meta, ...)` 第一步就是 `remote_meta["session_id"]`（mooncake）或 `remote_meta["nixl_meta"]`（nixl）——若发送端是 mooncake 而接收端是 nixl，hello 里只有 `nixl_meta` 没有 `session_id`，`write` 会 `KeyError`；反之同理。更隐蔽的是：即便字段名巧合相同，两套引擎的连接元数据语义完全不兼容，强行用会导致 RDMA 写到错误地址、KV 静默损坏。所以必须**在握手期就拦下**，而不是等到数据损坏。这与 u4-l3 讲的 `layout_version` 拦截 dtype 错配是同一防御思路——**把不兼容挡在握手期，不放进数据平面**。

#### 4.4.4 代码实践

实践目标：跟踪一次请求从握手到 RDMA 写的完整路径，确认 transport 三方法在两端如何配对。

操作步骤：

1. 打开 [prefill_connector.py:286-341](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/prefill_connector.py#L286-L341) 的 `_send`。
2. 在源码边标注每个变量来自哪一端：
   - `hello` ← 接收端 `local_meta()` + `hello_layout()`（经 TCP 传来）
   - `srcs` ← 本地 `staging.data_ptr()` + profile 布局
   - `dsts` ← `hello["kv_base"]/["pe_base"]/["ki_base"]`（接收端公告的地址）
3. 解释为什么 `srcs` 不需要握手就能算出来，而 `dsts` 必须等握手。

需要观察的现象：`_send` 里出现三次 `wire.send_msg`/`recv_msg`（控制平面）夹着一次 `transport.write`（数据平面）——这正是 u4-l3 的「双轨设计」在一次发送里的具体形态。

预期结果：你能画出「TCP 收 hello → 算 plan → RDMA 写 → TCP 发 done」的四段时序，并指出 transport 只参与了第三段。

#### 4.4.5 小练习与答案

**练习 1**：假设 prefill 配了 `"tilert_transport": "nixl"`，decode 漏写 `--transport`（落到默认 mooncake），第一次请求会在哪里失败？

**答案**：在 [prefill_connector.py:307-310](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/prefill_connector.py#L307-L310) 的 `assert hello.get("transport") == self._transport.name` 处 `AssertionError`，提示 `transport mismatch: decode=mooncake vs prefill=nixl`。不会进入 `write`，不会损坏数据。

**练习 2**：为什么 `local_meta()` 在两端都调，而 `write` 只在发送端调？

**答案**：`local_meta()` 产出的是「如何找到我」的名片，两端都需要把自己的名片交给对方（放进 hello）；接收端不主动写数据，只被动接收，所以没有 `write` 调用——它注册的显存由发送端的 `write` 直接写入。

---

### 4.5 多 NIC 主机绑定 UCX_NET_DEVICES

#### 4.5.1 概念说明

高端 GPU 服务器通常配多块 RDMA 网卡（如多张 ConnectX，设备名 `mlx5_0`、`mlx5_1`、`mlx5_2` …）。NIXL 走 UCX 后端，UCX 启动时会**自动探测可用网卡并选一条路径**。问题在于：UCX 的自动选择可能挑中一块走以太网交换机的「慢」网卡，而非直连 InfiniBand/RoCE 的「快」网卡，导致 RDMA 退化成普通网络传输、延迟飙升。

解决方法是 `UCX_NET_DEVICES` 这个 **UCX 自己的环境变量**：显式列出允许使用的网卡，把 UCX 的选择空间限定在 RDMA NIC 上。注意它管的是 UCX，所以**只对 NIXL 后端有效，对 Mooncake 无作用**——Mooncake 用自己的 `rdma` provider 选路。

#### 4.5.2 核心流程

```
# 在跑 vLLM prefill（以及任何 NIXL 进程）前导出：
export UCX_NET_DEVICES=mlx5_1:1,mlx5_2:1,...   # 逗号分隔，:1 是端口
vllm serve ...  --kv-transfer-config '{... "tilert_transport": "nixl"}'
```

`mlx5_X:1` 的含义：`mlx5_X` 是网卡设备名（`ibv_devices` 可查），`:1` 是该网卡的端口 1。多块网卡逗号分隔，UCX 会在其中选路。

#### 4.5.3 源码精读

README 的 Topology A 与 B 都强调了这条实践：

[README.md:320](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/README.md#L320) —— 说明 NIXL 为默认示例引擎，多 NIC 主机必须 `UCX_NET_DEVICES` 绑定，Mooncake 也支持但用 `--transport mooncake` 切换。

[README.md:340](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/README.md#L340) —— Topology A 里 prefill 节点的 `export UCX_NET_DEVICES=mlx5_1:1,mlx5_2:1,...`。

回到代码层面，`UCX_NET_DEVICES` 之所以生效，正是因为 `NixlTransport.init` 显式钉死了 UCX 后端：

[transport.py:60](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/transport.py#L60) —— `nixl_agent_config(backends=["UCX"])`，NIXL 走 UCX，于是 UCX 的环境变量（含 `UCX_NET_DEVICES`）对其生效。

对比两套命令更清楚：

| 后端 | decode 侧开关 | prefill 侧配置 | 多 NIC 绑定 |
| --- | --- | --- | --- |
| mooncake（默认） | `--transport mooncake` 或省略 | `"tilert_transport": "mooncake"` 或省略 | Mooncake 自管，**不读 `UCX_NET_DEVICES`** |
| nixl | `--transport nixl` | `"tilert_transport": "nixl"` | **必须** `export UCX_NET_DEVICES=...` |

[README.md:334](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/README.md#L334) 与 [README.md:354](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/README.md#L354) —— Topology A 里 decode 用 `--transport nixl`、prefill 用 `"tilert_transport": "nixl"`，两端一致。

#### 4.5.4 代码实践

实践目标：在没有 RDMA 硬件的情况下，理解 `UCX_NET_DEVICES` 的作用范围与验证方法。

操作步骤：

1. 在 NIXL 节点上用 `ibv_devices`（或 `lspci | grep -i mellanox`）列出本机 RDMA 网卡，记下设备名。
2. 阅读 [transport.py:56-62](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/transport.py#L56-L62)，确认 `init` 里没有出现任何网卡选择代码——选路完全委托给 UCX。
3. 推理：如果不设 `UCX_NET_DEVICES`，UCX 会怎么选？提示——它遍历所有探测到的传输（TCP、IB、RoCE、shared memory），按内置评分选「最优」，但「最优」可能不是你期望的 RDMA 直连路径。

需要观察的现象：TileRT 的 `transport.py` 对网卡选择**零代码**——这是刻意的，把网络拓扑决策留给运维通过 UCX 环境变量配置，代码保持通用。

预期结果：你能向团队解释「为什么同样的 NIXL 代码、换个机房延迟就翻倍」——很可能是新机房多 NIC 但没设 `UCX_NET_DEVICES`，UCX 选错了网卡。

> 待本地验证：若有 RDMA 硬件，用 `ucx_info -d` 观察 UCX 探测到的设备与选中路径，对比设/不设 `UCX_NET_DEVICES` 的差异。

#### 4.5.5 小练习与答案

**练习 1**：Mooncake 后端下，设 `UCX_NET_DEVICES` 有用吗？

**答案**：没用。Mooncake 不走 UCX（它在 `initialize` 时直接指定 `"rdma"` provider），不读 UCX 的环境变量。Mooncake 的网卡选择由其自身配置决定。

**练习 2**：Topology B 里原生 vLLM decode（`NixlConnector`）与 TileRT decode 都用 NIXL，`UCX_NET_DEVICES` 要在哪些进程设？

**答案**：所有走 NIXL 的进程都要设——共享 prefill、原生 vLLM decode、TileRT decode 节点的接收侧（TileRT decode 的 `ReceiveServer` 也会 `init` NIXL agent）。任一进程漏设都可能被 UCX 自动选路带偏。README 在 Topology B 的三个进程前都列了这条 export（[README.md:378](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/README.md#L378)、[README.md:386](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/README.md#L386)）。

---

## 5. 综合实践

把本讲四个最小模块串起来，完成一次「源码阅读 + 流程推演」的综合任务。

**任务背景**：你被要求把一个 TileRT PD 集群从 Mooncake 后端迁移到 NIXL 后端。请基于源码写出迁移检查清单。

**操作步骤**：

1. **确定改哪些配置**（对应 4.1 的工厂与 4.4 的两端配合）：
   - decode 侧命令行：`--transport mooncake` → `--transport nixl`（参 [decode_server.py:282-286](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/decode_server.py#L282-L286)）。
   - prefill 侧 JSON：`"tilert_transport": "mooncake"` → `"tilert_transport": "nixl"`（参 [prefill_connector.py:81](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/prefill_connector.py#L81)）。
   - 两端**必须同时改**，否则被 [prefill_connector.py:307-310](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/prefill_connector.py#L307-L310) 的 transport 一致性断言拦下。

2. **补网络绑定**（对应 4.5）：在所有 NIXL 进程前 `export UCX_NET_DEVICES=mlx5_1:1,mlx5_2:1,...`。说明为什么 Mooncake 时不需要这条、NIXL 时必须加（提示：UCX 自动选路风险）。

3. **核对依赖**（对应 4.2/4.3）：确认两端环境都装了 `nixl`（`from nixl._api import nixl_agent` 能 import），否则 `init` 会在 [transport.py:57](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/transport.py#L57) 抛 `ImportError`。Mooncake 时这里 import 的是 `mooncake.engine`。

4. **预测行为差异**（对应 4.3 的同步模型对比）：迁移后，prefill 发送线程（`_sender_loop`）的 CPU 占用预计会升高——因为 NIXL `write` 的轮询比 Mooncake 的同步阻塞更耗 CPU。若延迟敏感，考虑调大发送线程优先级或评估是否能切回 Mooncake。

**预期产出**：一份 4–6 条的迁移检查清单，每条标注对应的源码行号与本讲解释的模块。

> 待本地验证：迁移后的实际延迟与 CPU 占用变化需要在真实集群上 benchmark 才能确认，本实践只覆盖源码层面的推理。

---

## 6. 本讲小结

- `Transport` 是数据平面的统一抽象，用 `init/register/local_meta/write` 四方法把 RDMA 引擎差异收口到一处，框架代码对此完全无感知；`make_transport` 工厂按名字（缺省 `mooncake`）实例化。
- `MooncakeTransport` 借鉴 SGLang 先例，用单个 `TransferEngine` + P2P 握手 + `batch_transfer_sync_write`，连接元数据只有一个 `session_id` 字符串，`write` 返回即表示写完（引擎内部阻塞）。
- `NixlTransport` 走 UCX 后端，用 `(ptr,nbytes,dev,"")` 四元组 VRAM 描述符注册显存，握手交换 base64 编码的 agent metadata，`write` 是「提交 + 忙轮询 `check_xfer_state`」两段式，`_MAX_POLL` 兜底超时。
- 两端配合遵循 RDMA 经典模式：接收端 `ReceiveServer.__init__` 注册接收缓冲、`local_meta()` 进 hello 公告地址；发送端 `_ensure_worker_ready` 注册暂存缓冲、`_send` 收 hello 后 `rdma_plan` 算地址三元组、`write` 直接写。
- `hello["transport"]` 必须两端一致，在 [prefill_connector.py:307-310](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/prefill_connector.py#L307-L310) 拦截，否则两套引擎的连接元数据语义不兼容会导致 RDMA 写错地址或 `KeyError`。
- 多 NIC 主机用 NIXL 时必须 `export UCX_NET_DEVICES` 绑定 RDMA 网卡（UCX 自动选路可能选错），Mooncake 不读此变量。

---

## 7. 下一步学习建议

- **u4-l6 引擎接口与缓存注入**：本讲到 `transport.write` 把 KV 字节写到接收缓冲为止；这些字节随后如何被 `profile.convert` 反量化、被 `engine.inject_cache` 逐层写入 `caches`，是下一讲的主题。建议重点读 [mla_nsa.py:149-184](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/profiles/mla_nsa.py#L149-L184) 的 `convert`，看接收缓冲字节如何变回 `(ki, kv, pe)` 张量。
- **深入 Mooncake / NIXL 上游**：若对传输引擎本身感兴趣，可阅读 Mooncake 的 `TransferEngine` 文档与 NVIDIA NIXL 仓库的 UCX backend 示例，理解 P2P 握手与 GPUDirect RDMA 的底层机制。
- **回看 u2-l5 三层张量契约**：接收缓冲里 `kv/pe/ki` 三平面的布局正是 u2-l5 讲的 `caches` 在 PD 场景下的「远端来源」；对照 `buffer_bytes`/`hello_layout`（[mla_nsa.py:138-147](https://github.com/tile-ai/TileRT/blob/a8368a681342d0686e76bbd2225b320f2fead8a2/tilert/pd_vllm/profiles/mla_nsa.py#L138-L147)）与 u2-l5 的 `Idx` 缓存槽，理解「同一个 KV 在运行时、转换器、PD 传输三种视角下的布局一致性」。
