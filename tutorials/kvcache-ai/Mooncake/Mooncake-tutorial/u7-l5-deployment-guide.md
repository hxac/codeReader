# 部署指南：从单机到集群

## 1. 本讲目标

本讲是「运维与部署」主题的一讲。前面我们已经把 Mooncake Store 的**架构**（[u5-l1](u5-l1-store-architecture.md)）、**Master / Real Client**（u5-l2 / u5-l3）、**多级存储与 offload 内部机制**（u6-l2 / u6-l4）都讲透了。本讲不再深入内部实现，而是退回到「**怎么把它真正部署起来**」这一视角：照着官方部署文档，把一个 Store 从「单机最小可用」一路扩到「本地 SSD offload」再到「NVMe-oF 远程 SSD 池」，并讲清每一步该改哪个配置、为什么这么配。

学完本讲你应该能够：

1. **单机部署**：用 P2P handshake 元数据模式，在单机上跑通 `store_client_e2e.py` 端到端示例，说清所需的最小进程集合与配置项，理解为什么这是官方推荐的入门方式。
2. **SSD offload 部署**：理解「DRAM 内存池 + 本地 SSD 挡板」的容量规划逻辑，说清三种存储后端（bucket / file-per-key / offset-allocator）在**容量配额与淘汰语义**上的关键差异，并能据此配置出可控的 SSD 用量。
3. **NVMe-oF（NoF）部署**：理解「远程 SSD 池」的拓扑（Mooncake 服务节点 + SPDK SSD 池节点），说清 SPDK target 的创建、namespace 向 master 的**注册**，以及 master 对 NoF 段的**心跳探活**机制与相关参数。

> 本讲以三份官方部署文档为主要依据，所有配置项都尽量回溯到源码或文档行号，便于你在版本升级后核对。

## 2. 前置知识

本讲默认你已经具备以下背景：

- **Store 架构与控制面/数据面分离（依赖 [u5-l1](u5-l1-store-architecture.md)）**：你需要知道一个完整 Store 集群里通常有「Master Service（控制面）」「TE 元数据服务」「若干 Client（数据面，贡献内存）」这三类进程，以及数据是 Client↔Client 直传、绕过 Master 的。本讲的部署就是在把这三类进程按不同拓扑组合起来。
- **元数据后端与 P2P handshake（依赖 [u7-l3](u7-l3-metadata-server-backends.md)）**：你需要知道 TE 的元数据可以用 P2P handshake（零外部依赖、无中心）/ 内嵌 HTTP / etcd / Redis 四种后端。本讲的「单机部署」会重点用到 **P2P handshake**，而 NoF 部署示例则用了独立 HTTP 元数据服务器。
- **SSD offload 的内部机制（参考 [u6-l4](u6-l4-offload-promotion.md) 与 [u6-l2](u6-l2-multi-tier-storage.md)）**：offload 的「何时把数据从 DRAM 搬到 SSD、何时提升回来」的内部策略已经在那两讲讲过。本讲只关心「**部署时怎么把它开起来、容量怎么规划**」，不重复内部机制。

### 几个部署相关的基础概念

- **Real Client（真实客户端）**：Mooncake Store 里真正持有内存段、能贡献资源给集群的客户端进程。一个推理进程可以直接内嵌一个 Real Client，也可以单独起一个 `mooncake_client` 进程、让推理进程以 DummyClient 方式连过去（见 [u5-l3](u5-l3-real-client.md)）。**SSD offload 一定发生在 Real Client 里**，这是部署时决定用哪种模式的关键分叉点。
- **SPDK / NVMe-oF（NoF）**：SPDK（Storage Performance Development Kit）是用户态存储栈，能绕过内核直接驱动 NVMe SSD，延迟极低。**NVMe-oF（NVMe over Fabrics）** 则把 NVMe 协议跑在 RDMA/TCP 网络上，让远端机器像访问本地盘一样访问另一台机器上的 SSD。Mooncake 用「NoF 远程 SSD 池」把一批跨节点的 SSD 当成一个可被 master 调度的存储层。
- **hugepages（大页内存）**：SPDK 需要把内存固定在物理页上（pin），默认 4KB 分页会撑爆页表，因此要预留 2MB 大页。NoF 部署里会出现「配置 hugepages」这一步，原因就在这。

## 3. 本讲源码地图

本讲主要依据三份官方部署文档，辅以端到端测试脚本与几个 Python 部署工具。关键文件如下：

| 文件 | 作用 | 本讲怎么用 |
|---|---|---|
| [docs/source/deployment/mooncake-store-deployment-guide.md](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/docs/source/deployment/mooncake-store-deployment-guide.md) | Store 部署与调优总指南 | 单机部署（Quick Start / P2P handshake）、各部署场景、master 启动参数全集 |
| [docs/source/deployment/ssd-offload.md](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/docs/source/deployment/ssd-offload.md) | SSD offload 专属部署指南 | offload 两种部署模式、Real Client 参数、存储后端与容量配额、淘汰策略 |
| [docs/source/deployment/nvmf-ssd-deployment-guide.md](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/docs/source/deployment/nvmf-ssd-deployment-guide.md) | NVMe-oF（NoF）远程 SSD 池部署指南 | NoF 拓扑、SPDK target 创建、namespace 注册、性能测试与 vLLM 接入 |
| [mooncake-store/tests/e2e/store_client_e2e.py](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/tests/e2e/store_client_e2e.py) | 基于 `MooncakeDistributedStore` 的持续 put/get 工作负载生成器 | 单机部署实践的「客户端」锚点 |
| [mooncake-store/tests/e2e/run_nof_heartbeat_tcp_e2e.sh](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/tests/e2e/run_nof_heartbeat_tcp_e2e.sh) | NoF 心跳端到端测试脚本（TCP SPDK target） | NoF 部署四组件拓扑与心跳参数的「可运行范例」 |
| [mooncake-wheel/mooncake/mooncake_store_service.py](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-wheel/mooncake/mooncake_store_service.py) | 带 REST API 的独立 Store 服务 | 内嵌 Real Client 调用 `store.setup(...)` 的真实参数序列（含 offload 开关） |
| [mooncake-wheel/mooncake/spdk_tgt_create.py](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-wheel/mooncake/spdk_tgt_create.py) | 通过 SSH 远程创建 SPDK target 的工具 | NoF 部署中「创建 SSD 池」那一步的默认传输参数来源 |
| [mooncake-wheel/mooncake/mooncake_ssd_register.py](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-wheel/mooncake/mooncake_ssd_register.py) | 把 SPDK target 的 namespace 注册到 master 的工具 | NoF 部署中「注册」那一步、`MooncakeDistributedNoFRegister` 的调用方式 |
| `mooncake-store/src/master.cpp` / `mooncake-store/include/types.h` | master 主程序与默认常量 | NoF 心跳三个 gflag 的定义与默认值 |
| `mooncake-integration/transfer_engine/transfer_engine_py.cpp` | Python 集成层（`store` 模块的 C++ 后端） | `P2PHANDSHAKE` 字面量如何被解析 |

> 小提示：`store_client_e2e.py` 里 `import store` 的 `store`，是编译产物 `mooncake-integration/store*.so` 暴露的模块（见 e2e 文档里的 `PYTHONPATH=/path/to/build/mooncake-integration`），它和 wheel 包里的 `from mooncake.store import ...` 是同一套底层绑定。这会影响你「怎么跑通」这个示例。

## 4. 核心概念与源码讲解

### 4.1 单机部署：P2P handshake 最小配置

#### 4.1.1 概念说明

「单机部署」不是贬义——它是**官方推荐的入门起点**。部署指南在 Architecture Overview 一节就明确：

> We also provide a P2P handshake mechanism (`P2PHANDSHAKE`) that enables decentralized metadata management by storing metadata locally on each node, eliminating the need for a centralized service — this is the simplest metadata handshake method and the recommended starting point.
> —— [mooncake-store-deployment-guide.md:14](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/docs/source/deployment/mooncake-store-deployment-guide.md#L14)

为什么 P2P handshake 是最简的？回顾 [u7-l3](u7-l3-metadata-server-backends.md)：TE 元数据本来要靠一个外部服务（etcd/Redis/HTTP）来登记各节点段的网络地址。P2P handshake 把这一层**完全去掉**——节点之间在建立连接时直接用 TCP socket 互发 JSON 交换握手信息，每个节点把元数据存在本地。于是部署时「元数据服务」这一整个进程都不需要起了。

它的代价是：元数据没有中心化、不持久、跨网段发现能力弱，所以**只适合开发、测试、单机或小型临时集群**；生产大规模集群仍建议用 etcd。部署指南的 Tip 也写明了这条选型建议：

> P2P handshake is the easiest way to get started — it is decentralized and requires no etcd/Redis/HTTP metadata service. Prefer it for development and simple deployments; use an external etcd/Redis for large, long-lived clusters.
> —— [mooncake-store-deployment-guide.md:42-L44](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/docs/source/deployment/mooncake-store-deployment-guide.md#L42-L44)

#### 4.1.2 核心流程

单机 P2P handshake 部署只有两个进程（**元数据服务被省掉了**）：

```text
进程1: mooncake_master            (控制面，RPC 端口 50051)
进程2: 一个 Real Client (Python)   (数据面 + store server，贡献内存段)
       └─ metadata_server = "P2PHANDSHAKE"   ← 关键魔法值
```

启动顺序与配置：

```text
1) 启动 master
   mooncake_master --enable_http_metadata_server=true \
                   --http_metadata_server_host=0.0.0.0 \
                   --http_metadata_server_port=8080
   （注：P2P handshake 模式下 master 的内嵌 HTTP 元数据服务其实用不到，
        但指南 Quick Start 默认开着它，方便切换到 HTTP 元数据模式。）

2) 启动 Real Client，把 metadata_server 设成字面量 "P2PHANDSHAKE"
   store.setup(local_hostname=...,
               metadata_server="P2PHANDSHAKE",   ← 唯一与「HTTP 模式」不同的地方
               global_segment_size=...,          # 贡献给集群的 DRAM
               local_buffer_size=...,            # TE 暂存缓冲
               protocol="tcp",
               master_server_address="127.0.0.1:50051")
```

注意一个反直觉点：**即便走 P2P handshake，Client 仍要连 master**（走 `master_server_address`，默认 50051）。P2P handshake 省掉的是 **TE 的元数据服务**，而不是 Store 的 **Master Service**——这两个是不同层次的东西（见 [u5-l1 的「容易踩的坑」](u5-l1-store-architecture.md)）。Master 负责「分配副本空间、记录对象元数据、淘汰调度」，这些控制面职责无可替代。

#### 4.1.3 源码精读

**魔法值 `P2PHANDSHAKE` 是怎么被识别的？** 在 Python 集成层里，连接串解析函数对没有 `://` 的输入做了特殊处理：如果整个字符串就等于 `P2PHANDSHAKE`，则协议留空、domain 设为 `P2PHANDSHAKE`：

[transfer_engine_py.cpp:138-149](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/transfer_engine/transfer_engine_py.cpp#L138-L149) —— `conn_string` 既不含 `://`、又等于 `P2PHANDSHAKE` 时，走专门的 P2P 分支。随后的 `buildConnString` 进一步确认：只要 `metadata_server == P2PHANDSHAKE`，连接串原样返回 `P2PHANDSHAKE`，不再拼 `协议://地址`：

[transfer_engine_py.cpp:156-160](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-integration/transfer_engine/transfer_engine_py.cpp#L156-L160) —— 这就是「把 `metadata_server` 设成字面量 `P2PHANDSHAKE` 即可启用 P2P 握手」的代码落点。这条字符串最终会把 TE 的元数据存储插件切换到 `SocketHandShakePlugin`（无中心，见 [u7-l3 的 4.2 节](u7-l3-metadata-server-backends.md)）。

**部署指南给出的最小 client 调用**——把 `metadata_server` 直接写成 `P2PHANDSHAKE`，其余参数和 HTTP 模式完全一致：

[mooncake-store-deployment-guide.md:91-L104](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/docs/source/deployment/mooncake-store-deployment-guide.md#L91-L104) —— 文档同时给了 `MooncakeDistributedStore().setup(...)`（内嵌 Real Client）和 `python -m mooncake.mooncake_store_service`（独立 store 服务）两种起法，两者都接受 `metadata_server=P2PHANDSHAKE`。

**`store_client_e2e.py` 里的实际调用**——本讲实践的客户端就是它。它用位置参数调用 `mc.setup(...)`，第 2 个位置参数就是 `--metadata-server`：

```python
mc = store.MooncakeDistributedStore()
setup_ret = mc.setup(
    args.local_hostname,
    args.metadata_server,      # ← 传 "P2PHANDSHAKE" 即可切到 P2P 模式
    args.global_segment_size,
    args.local_buffer_size,
    args.protocol,
    args.device_name,
    args.master_server,
)
```

—— [store_client_e2e.py:39-L48](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/tests/e2e/store_client_e2e.py#L39-L48)

注意它的 `--metadata-server` 默认值是 `http://127.0.0.1:8080/metadata`（[store_client_e2e.py:19](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/tests/e2e/store_client_e2e.py#L19)），即默认走 HTTP 元数据；要切到 P2P handshake，运行时显式传 `--metadata-server P2PHANDSHAKE` 即可，无需改代码。

#### 4.1.4 代码实践

**实践目标**：在源码层面确认「P2P handshake 单机部署 = 2 个进程 + 1 个魔法值」，并定位每个进程的启动参数。

**操作步骤**：

1. 打开部署指南的 Single-Node 场景描述，确认它就是 Quick Start 的复述，并标注其局限：

   [mooncake-store-deployment-guide.md:228-L239](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/docs/source/deployment/mooncake-store-deployment-guide.md#L228-L239) —— 「单 master 是单点故障；master 挂了，集群操作暂停直到恢复」。这就是单机部署的边界。

2. 打开 `store_client_e2e.py`，把它的 11 个命令行参数与文档对照，建一张「参数 → 含义 → P2P 模式取值」表（见下方综合实践里会用到）。

3. 思考一个边界问题：P2P handshake 模式下，master 的 `--enable_http_metadata_server` 还有用吗？（提示：P2P 模式下 Client 不会去访问 8080 的 HTTP 元数据接口，所以可以不开；指南 Quick Start 默认开是为了方便你随时切回 HTTP 模式。）

**需要观察的现象**：`metadata_server` 是**唯一**决定「TE 用哪种元数据后端」的开关；`master_server_address` 则**始终**指向 Store master，与元数据后端无关。两者职责正交。

**预期结果**：你能用一句话区分——「`metadata_server=P2PHANDSHAKE` 省掉的是 TE 元数据进程，master 进程一个都不能省」。

#### 4.1.5 小练习与答案

**练习 1**：单机 P2P handshake 部署里，如果只起 master、不起任何 Client，会发生什么？能 `Put`/`Get` 吗？

> **参考答案**：master 启动正常，但没有 Client 贡献内存段，集群的内存池为空。此时任何 `PutStart` 都会因为「无可分配空间」返回 `NO_AVAILABLE_HANDLE`（回顾 [u5-l1](u5-l1-store-architecture.md) 的 `PutStart` 流程）。`Get` 也读不到任何对象。所以要跑通端到端，**至少要起一个贡献 `global_segment_size > 0` 的 Client**。

**练习 2**：把 `metadata_server` 从 `http://127.0.0.1:8080/metadata` 改成 `P2PHANDSHAKE`，master 进程需要重启或改配置吗？

> **参考答案**：不需要。`metadata_server` 只影响 **Client 侧 TE 的元数据插件选择**，master 不关心 Client 用哪种元数据后端。master 该开的功能（RPC、可选的内嵌 HTTP）照旧。这也印证了「TE 元数据」与「Store master」是两个独立层次。

---

### 4.2 SSD offload 部署：容量规划与淘汰配置

#### 4.2.1 概念说明

单机部署只用了 DRAM。当 KV cache 的量超过集群 DRAM 总量时，Mooncake 允许把**冷数据**从 DRAM 卸载（offload）到**本地 SSD**，读未命中时再回源。SSD 比 DRAM 便宜一个数量级，这是「花更少的钱装更多 KV cache」的关键能力。

部署层面有两个要点必须先理清：

**(1) offload 一定发生在 Real Client 里**。SSD offload 文档开篇就强调它依赖 Real Client，并提供两种部署模式：

> SSD offload requires the **Real Client** and supports two deployment modes:
> - **Mode A: Embedded Real Client** — the Python process embeds the Real Client, and SSD offload runs inside the Python process.
> - **Mode B: Standalone Real Client + DummyClient** — a standalone `mooncake_client` process runs SSD offload, and the Python process connects via a DummyClient.
>
> In both modes, all SSD reads and writes happen within the Real Client.
> —— [ssd-offload.md:9-L14](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/docs/source/deployment/ssd-offload.md#L9-L14)

Mode A（推理进程内嵌 Real Client）最简单；Mode B（独立 `mooncake_client` 进程 + DummyClient）适合「推理进程不想承担 SSD I/O 与内存管理」的场景，把重活交给一个专门的常驻进程。

**(2) 容量配额是「双开关」，且语义随存储后端而变**。这是新手最容易踩坑的地方：你看到的「容量」其实由两个环境变量共同决定，而且**后端不同，含义不同**——这是本模块的核心。

#### 4.2.2 核心流程

开启本地 SSD offload 的部署流程：

```text
1) 建目录:  mkdir -p /nvme/mooncake_offload     (必须是已存在的绝对目录)

2) 起 master，打开 offload 总开关:
   mooncake_master --enable_offload=true
   （可选: --offload_on_evict=true     把卸载推迟到淘汰路径, 而非 Put 完成立刻卸载
         --promotion_on_hit=true      允许 SSD-only 的热数据提升回 DRAM
         --promotion_admission_threshold=2   提升准入门槛）

3) 起 Real Client (Mode A 内嵌 或 Mode B 独立 mooncake_client), 同样要 --enable_offload=true,
   并设两个关键环境变量:
   export MOONCAKE_OFFLOAD_FILE_STORAGE_PATH=/nvme/mooncake_offload      # 存储目录
   export MOONCAKE_OFFLOAD_STORAGE_BACKEND_DESCRIPTOR=bucket_storage_backend  # 后端类型
```

容量规划的核心直觉（**这是部署最容易出错的地方**）：

- DRAM 内存池（`--global_segment_size`）是「热层」，容量小、访问快；
- SSD 是「温/冷层」，容量大、访问稍慢；
- **只有当写入总量 > DRAM 池容量、触发淘汰时，offload 才会被激活**。所以文档的示例特意注明「Memory pool size: 4 GB (smaller than the total data written, to trigger offload)」（[ssd-offload.md:207](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/docs/source/deployment/ssd-offload.md#L207)）。如果你把 DRAM 池开得比写入数据还大，offload 永远不会触发——这是 Troubleshooting 里列的头号问题。

#### 4.2.3 源码精读

**Real Client 参数表**——Mode B 独立 `mooncake_client` 的全部相关 flag，注意 `--enable_offload`「**必须在 master 和 client 两边都设为 true**」：

[ssd-offload.md:89-L102](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/docs/source/deployment/ssd-offload.md#L89-L102) —— 参数含 `metadata_server`（这里示例也用了 `P2PHANDSHAKE`）、`master_server_address`、`global_segment_size`、`protocol`/`device_names`、`--enable_offload`。Troubleshooting 进一步明确「两边都要开」：

[ssd-offload.md:280-L284](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/docs/source/deployment/ssd-offload.md#L280-L284) —— `Confirm --enable_offload=true is passed to both mooncake_client and mooncake_master`。

**容量双开关的核心设置表**：

[ssd-offload.md:107-L121](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/docs/source/deployment/ssd-offload.md#L107-L121) —— 这里有两个看起来都在管「容量」的变量，务必分清：

| 环境变量 | 默认值 | 真实含义 |
|---|---|---|
| `MOONCAKE_OFFLOAD_TOTAL_SIZE_LIMIT_BYTES` | `2199023255552`（2 TB） | 磁盘用量**上限**（quota），全局生效 |
| `MOONCAKE_OFFLOAD_BUCKET_MAX_TOTAL_SIZE`（仅 bucket 后端） | `0` | bucket 后端的**淘汰阈值**；`0` **不是「无限制」**，而是「取物理盘容量 × 90%」 |

**最关键的陷阱**——`MOONCAKE_OFFLOAD_BUCKET_MAX_TOTAL_SIZE=0` 的含义：

[ssd-offload.md:130](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/docs/source/deployment/ssd-offload.md#L130) —— 原文 `When set to 0, the backend uses **90% of the physical disk capacity** as the quota — it does not mean unlimited. Set an explicit value to control disk usage precisely.`。也就是说，你以为「0 = 不限制」，实际是「0 = 默认吃掉 90% 的盘」。生产环境一定要显式设一个值来精确控制用量。

**三种存储后端的容量语义差异**（部署选型时必须知道）：

[ssd-offload.md:172-L180](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/docs/source/deployment/ssd-offload.md#L172-L180) —— `offset_allocator_storage_backend` 把 `MOONCAKE_OFFLOAD_TOTAL_SIZE_LIMIT_BYTES` **直接当作预分配文件大小（100%，无安全余量）**，且**没有**单独的 quota 变量；更狠的是它在初始化时**截断数据文件、不支持重启恢复**。这与 bucket 后端「`0` 取 90% 物理盘」的语义完全不同。可用一张表概括：

| 后端 | 容量如何决定 | 是否支持重启恢复 | 适用场景 |
|---|---|---|---|
| `bucket_storage_backend`（推荐） | `BUCKET_MAX_TOTAL_SIZE`（0→90% 物理盘）为淘汰阈值，多个 KV 装进 bucket 文件 | 是（启动扫描元数据上报 master） | 通用、大规模 |
| `file_per_key_storage_backend` | `TOTAL_SIZE_LIMIT_BYTES` 为上限，一 key 一文件 | 是 | 调试、小规模 |
| `offset_allocator_storage_backend` | `TOTAL_SIZE_LIMIT_BYTES` **直接=预分配文件大小（100%）** | **否（重启截断，数据丢失）** | 高并发小对象、不需持久 |

**bucket 后端的淘汰策略**——当磁盘用量逼近阈值时，按策略淘汰整个 bucket：

[ssd-offload.md:184-L194](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/docs/source/deployment/ssd-offload.md#L184-L194) —— `none`（不淘汰，盘满即写失败）/ `fifo`（先淘汰最老 bucket）/ `lru`（淘汰最久未读 bucket），且是**两阶段淘汰**：先从元数据移除并通知 master，再排空在途读，最后删文件。

**Mode A 内嵌 Real Client 的真实调用**——带 REST API 的 store 服务（`mooncake.mooncake_store_service`）在 `store.setup(...)` 里把 offload 开关作为最后两个参数传入，这是「内嵌模式」部署的代码落点：

[mooncake_store_service.py:123-L135](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-wheel/mooncake/mooncake_store_service.py#L123-L135) —— 注意它比 `store_client_e2e.py` 的 7 参数版多了 3 个尾部参数（`None` 占位、`enable_ssd_offload`、`ssd_offload_path`）。所以「内嵌 offload」在 API 层就是多传 `enable_ssd_offload=True` 与 `ssd_offload_path=...` 这两个值。

#### 4.2.4 代码实践

**实践目标**：为一台「200 GB SSD + 4 GB DRAM 池」的机器，规划出一组可控容量的 offload 配置，并解释每个数字的依据。

**操作步骤**：

1. 假设物理 SSD 挂载在 `/nvme`，可用空间约 200 GB。
2. 选择 `bucket_storage_backend`，并**显式**设淘汰阈值（避免「0 = 90% 物理盘」的隐式行为）：

   ```bash
   export MOONCAKE_OFFLOAD_FILE_STORAGE_PATH=/nvme/mooncake_offload
   export MOONCAKE_OFFLOAD_STORAGE_BACKEND_DESCRIPTOR=bucket_storage_backend
   export MOONCAKE_OFFLOAD_BUCKET_MAX_TOTAL_SIZE=$((200 * 1024 * 1024 * 1024))  # 200 GB
   export MOONCAKE_OFFLOAD_BUCKET_EVICTION_POLICY=lru
   ```

   这与文档 Example 里的真实配置一字不差，见 [ssd-offload.md:218-L233](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/docs/source/deployment/ssd-offload.md#L218-L233)。

3. 把有效容量写成公式（直觉版）：

   \[
   C_{\text{有效}} \approx \min\bigl(\,C_{\text{物理SSD}},\; Q_{\text{配额}}\,\bigr),\quad
   Q_{\text{配额}} =
   \begin{cases}
   0.9 \times C_{\text{物理SSD}}, & \text{若 } \texttt{BUCKET\_MAX\_TOTAL\_SIZE}=0 \\
   \text{显式设定值}, & \text{否则}
   \end{cases}
   \]

   即「SSD 能用的量 = 物理盘与配额两者取小」。DRAM 池（`global_segment_size=4GB`）只决定「热层大小」，不进入这个公式。

**需要观察的现象**：DRAM 池（4 GB）远小于 SSD 配额（200 GB）。当你持续 `Put` 超过 4 GB 时，master 会触发内存淘汰（高水位 0.95），被选中的对象经 offload 路径写入 SSD；SSD 累计到 200 GB 后，按 `lru` 淘汰最久未读的 bucket。

**预期结果**：你能回答「为什么必须把 DRAM 池设得比写入量小」——否则淘汰不触发，offload 永远不激活（[ssd-offload.md:284](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/docs/source/deployment/ssd-offload.md#L284)）。**待本地验证**：实际跑一轮写入后，用 `du -sh /nvme/mooncake_offload` 观察 SSD 用量增长，再 `curl` master 的 `/metrics/summary` 看是否出现 offload 相关计数。

#### 4.2.5 小练习与答案

**练习 1**：`MOONCAKE_OFFLOAD_BUCKET_MAX_TOTAL_SIZE` 不设（即用默认 0），会发生什么？这是好事还是坏事？

> **参考答案**：默认 0 会让 bucket 后端把「物理盘容量 × 90%」当作配额。在专用 SSD 上也许可接受，但在**共享盘**（SSD 还跑着别的业务）上很危险——Mooncake 会毫无顾忌地吃到 90%，挤掉其他业务。生产环境应**显式设值**，把它压到你能让出的额度内（参考 [ssd-offload.md:130](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/docs/source/deployment/ssd-offload.md#L130)）。

**练习 2**：为什么 `offset_allocator_storage_backend` 不适合需要「重启后还能读到旧 offload 数据」的场景？

> **参考答案**：它在初始化时直接截断（truncate）数据文件、清空内存元数据，**不支持重启恢复**（[ssd-offload.md:172-L180](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/docs/source/deployment/ssd-offload.md#L172-L180)）。而 `bucket_storage_backend` / `file_per_key_storage_backend` 在启动时会扫描既有 SSD 元数据并上报 master，旧数据继续可读（[ssd-offload.md:239-L240](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/docs/source/deployment/ssd-offload.md#L239-L240)）。所以要持久就得避开 offset-allocator。

**练习 3**：开启 offload 后，`--enable_offload` 只在 master 上设 true 够不够？

> **参考答案**：不够。必须在 **master 和 Real Client 两边都设 true**（[ssd-offload.md:282](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/docs/source/deployment/ssd-offload.md#L282)）。因为 master 负责「调度卸载任务」，Real Client 负责「真正执行 SSD 读写」，两者缺一不可。

---

### 4.3 NVMe-oF（NoF）部署：远程 SSD 段与心跳配置

#### 4.3.1 概念说明

4.2 讲的是「**本地** SSD」——SSD 直接连在 Real Client 所在机器上。但生产里 SSD 往往是**集中部署**在一批专用节点上的（存储节点），计算节点通过网络访问它们。NVMe-oF（NoF）就是干这个的：把一批跨节点的 SSD 通过 RDMA/TCP 暴露成「远程 SSD 池」，让 master 把它们当成一个可调度的存储层，客户端可以把数据副本（`nof_replica`）写到这个池里。

NoF 部署的**两个阶段**，文档开篇就讲清了：

> - Start Mooncake services built with NoF support enabled.
> - Create SPDK NVMe-oF targets on SSD pool nodes and register their namespaces with the Mooncake master.
>
> After registration, the master reports the registered NVMe-oF namespaces as a remote SSD pool in its metrics, and clients can place NoF replicas through Mooncake Store.
> —— [nvmf-ssd-deployment-guide.md:4-L14](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/docs/source/deployment/nvmf-ssd-deployment-guide.md#L4-L14)

这里有两个关键词：

- **SPDK NVMe-oF target**：跑在 SSD 池节点上的 SPDK 进程（`nvmf_tgt`），它把本地 NVMe SSD 封装成 NVMe-oF subsystem，对外提供 NQN（subsystem 名字）、namespace（命名空间，类似分区）、listener（监听地址:端口）。
- **注册（register）**：把这些 namespace 的「连接三元组」（NQN + namespace ID + 地址:端口）登记到 master，master 才知道集群里多了这些远程 SSD 段，才能在分配副本时把数据放到上面。

**与本地 offload 的本质区别**：本地 offload 是「**单机内部的 DRAM→SSD 搬运**」，数据流不出这台机器；NoF 是「**跨节点的远程 SSD 副本**」，数据要经过网络写到另一台机器的 SSD 上。这也决定了 NoF 多了一个必须解决的运维问题——**远程段可能整体失联**，于是 master 需要**心跳探活**。

#### 4.3.2 核心流程

NoF 部署的完整拓扑（以文档示例的三节点为例）：

```text
节点角色:
  192.168.65.81  Mooncake 服务节点  (master + metadata + store service)
  192.168.65.56  SSD 池节点 1       (跑 SPDK nvmf_tgt, 暴露 NVMe SSD)
  192.168.65.57  SSD 池节点 2       (跑 SPDK nvmf_tgt, 暴露 NVMe SSD)

步骤:
  0) 编译: cmake -DUSE_NOF=ON ...   (NoF 支持是编译期开关)
  1) 部署 Mooncake 服务:
       2.2 起 master        : mooncake_master --rpc_address=192.168.65.81
       2.3 起元数据(独立HTTP): python3 -m mooncake.http_metadata_server --host=... --port=8080
       2.4 起 store service  : python3 -m mooncake.mooncake_store_service --config=... --port=8081
            (需先 echo 512 > /proc/sys/vm/nr_hugepages 配 hugepages)
  2) 部署 SSD 池 (在 .56/.57 上创建 SPDK target):
       python3 -m mooncake.spdk_tgt_create --spdk_target_info="ip:... path:... [pci:...]" ...
  3) 注册 SSD 池到 master:
       python3 -m mooncake.mooncake_ssd_register --master_server_address=... --spdk_target_info="ip:... path:..."
  4) (可选) 性能压测 / 接入 vLLM+LMCache
```

注意拓扑上的分工：**Mooncake 服务节点不直接持有 SSD**，它只是「指挥」；真正拥有 SSD 的是两个 SSD 池节点。注册之后，这些远程 SSD 段就像本地内存段一样进入 master 的全局资源池，可以被分配为 `nof_replica`。

**心跳探活机制**（NoF 相比本地 offload 多出来的关键运维能力）：master 周期性地对每个已挂载的 NoF 段发起探活，连续失败达到阈值就**卸载（unmount）该段**，避免把副本分配到一个已经失联的远程 SSD 上。文档的 e2e 脚本正是用「杀掉 SPDK target → 等 master 心跳发现并 unmount → 验证客户端行为」来验证这条路径。

#### 4.3.3 源码精读

**节点拓扑与各组件部署命令**——文档示例的三节点分工：

[nvmf-ssd-deployment-guide.md:24-L27](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/docs/source/deployment/nvmf-ssd-deployment-guide.md#L24-L27) —— Mooncake 服务节点 `.81` 跑 master/metadata/store；`.56`/`.57` 是 SSD 池节点。

master、独立 HTTP 元数据、store service 的启动命令分别是：

[nvmf-ssd-deployment-guide.md:29-L45](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/docs/source/deployment/nvmf-ssd-deployment-guide.md#L29-L45) —— 注意这里**用独立的 `python3 -m mooncake.http_metadata_server`**（非 master 内嵌），原因是 NoF 场景常需要元数据服务稳定可独立重启；若启动报 aiohttp 错误，`pip3 install aiohttp`。

**store_service.json 配置**——store service 节点要配 hugepages（SPDK 需要）：

[nvmf-ssd-deployment-guide.md:49-L77](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/docs/source/deployment/nvmf-ssd-deployment-guide.md#L49-L77) —— 配置里 `device_name` 要用 `ibv_devices` 查到的真实 RDMA 设备名（如 `mlx5_0`）；`echo 512 > /proc/sys/vm/nr_hugepages` 给 SPDK 预留大页，512 通常够启动。

**创建 SSD 池——`spdk_tgt_create` 工具的默认传输参数**：

[nvmf-ssd-deployment-guide.md:102-L120](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/docs/source/deployment/nvmf-ssd-deployment-guide.md#L102-L120) —— 命令行 `--spdk_target_info` 的格式是 `ip:<ip> path:<spdk路径> pci:<pci1>,<pci2>`；`pci` 可省略，省略时工具会**自动发现**目标节点上「SPDK-ready 或未挂载」的 NVMe 设备。这些默认值的代码来源是：

[spdk_tgt_create.py:19-L29](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-wheel/mooncake/spdk_tgt_create.py#L19-L29) —— `DEFAULT_TRANSPORT_OPTIONS` 把 `trtype=RDMA`、`max_queue_depth=128`、`max_io_size=4096` 等写死为默认。工具内部通过 SSH 连到每个 SSD 池节点，依次跑：`setup.sh`（绑驱动）→ 启动 `nvmf_tgt` → `nvmf_create_transport` → `bdev_nvme_attach_controller`（建块设备）→ `nvmf_create_subsystem`（建 subsystem，固定 NQN `nqn.2016-06.io.spdk:cnode1`）→ `nvmf_subsystem_add_ns`（加 namespace）→ `nvmf_subsystem_add_listener`（监听 `4420` 端口）。这一长串封装在 `deploy_target()` 里（[spdk_tgt_create.py:381-L430](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-wheel/mooncake/spdk_tgt_create.py#L381-L430)）。

**注册——`mooncake_ssd_register` 工具**：它先 SSH 到每个 target 跑 `nvmf_get_subsystems` + `bdev_get_bdevs` 拿到 NQN/nsid/traddr/trsvcid/size，再调底层 `MooncakeDistributedNoFRegister().real_register(...)` 把这些段登记到 master：

[mooncake_ssd_register.py:241-L250](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-wheel/mooncake/mooncake_ssd_register.py#L241-L250) —— `real_register(nqn, nsid, traddr, trsvcid, base, size, master_server_address)` 七个参数正是 SPDK subsystem 暴露出来的「远程段」描述。注意它对「segment already exists」错误做了幂等处理（[mooncake_ssd_register.py:260-L263](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-wheel/mooncake/mooncake_ssd_register.py#L260-L263)），所以重复注册是安全的。

**心跳三个 gflag 与默认值**——这是 NoF 部署「段与心跳配置」的源码核心。master 侧定义了三个控制探活行为的参数：

```cpp
DEFINE_int64(nof_heartbeat_interval_sec,
             mooncake::DEFAULT_NOF_HEARTBEAT_INTERVAL_SEC,
             "How often master probes each mounted NoF segment");
DEFINE_uint32(nof_heartbeat_probe_timeout_ms,
              mooncake::DEFAULT_NOF_HEARTBEAT_PROBE_TIMEOUT_MS,
              "Timeout in milliseconds for a single NoF heartbeat probe");
DEFINE_uint32(
    nof_heartbeat_failures_threshold,
    mooncake::DEFAULT_NOF_HEARTBEAT_FAILURES_THRESHOLD,
    "Consecutive NoF heartbeat failures required before unmounting a NoF "
    "segment");
```

—— [master.cpp:159-L169](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master.cpp#L159-L169)。三者默认值定义在：

[types.h:96-L98](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/types.h#L96-L98) —— 间隔 **10 秒**、单次探活超时 **1000 毫秒**、连续 **3 次**失败才卸载段。把它们翻译成「故障检测窗口」：

\[
T_{\text{detect}} \approx N_{\text{threshold}} \times T_{\text{interval}} = 3 \times 10\text{s} = 30\text{s}
\]

即默认配置下，一个远程 SSD 段失联后，master 大约 **30 秒**后才会判定它死亡并卸载。这就是为什么 e2e 脚本里 `wait_for_pattern` 的超时设成 `HEARTBEAT_INTERVAL * HEARTBEAT_FAILURES + 20`（留 20 秒余量）。

**e2e 心跳脚本的四组件拓扑**——这是 NoF 部署「能跑通的最小真实范例」，把 master + 独立 HTTP 元数据 + SPDK target（TCP）+ Python client 四个进程都显式拉起：

[run_nof_heartbeat_tcp_e2e.sh:90-L101](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/tests/e2e/run_nof_heartbeat_tcp_e2e.sh#L90-L101) —— 这里 master 显式传了三个心跳参数：`--nof_heartbeat_interval_sec=$HEARTBEAT_INTERVAL`(默认 2)、`--nof_heartbeat_probe_timeout_ms=$HEARTBEAT_TIMEOUT_MS`(默认 500)、`--nof_heartbeat_failures_threshold=$HEARTBEAT_FAILURES`(默认 3)。注意 e2e 为了测试快，把间隔从默认 10s 压到了 2s。

注册那一步直接内联调用了底层绑定（等价于 `mooncake_ssd_register` 但走 TCP、用 Malloc 盘）：

[run_nof_heartbeat_tcp_e2e.sh:105-L118](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/tests/e2e/run_nof_heartbeat_tcp_e2e.sh#L105-L118) —— `store.MooncakeDistributedNoFRegister().real_register(NQN, nsid=1, host, port, base=0, size, master_rpc)`。注意环境变量 `MC_NOF_TRTYPE=TCP`——它告诉客户端用 TCP 而非 RDMA 连 SPDK target，这正是脚本名里「tcp」的由来。

随后启动 `store_client_e2e.py`，关键参数是 `--memory-replica-num` 与 `--nof-replica-num`：

[run_nof_heartbeat_tcp_e2e.sh:120-L131](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/tests/e2e/run_nof_heartbeat_tcp_e2e.sh#L120-L131) —— 这两个数组装进 `ReplicateConfig`（[store_client_e2e.py:53-L55](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/tests/e2e/store_client_e2e.py#L53-L55)），分别决定「写几份内存副本 / 几份 NoF 远程 SSD 副本」。设 `--global-segment-size 0` 即进入「**NoF-only**」模式（[readme.md:148](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/tests/e2e/readme.md#L148)）——此时不贡献本地内存段，所有副本都落在远程 NoF 池。

#### 4.3.4 代码实践

**实践目标**：通过阅读 e2e 脚本，理解「NoF 心跳故障检测」的可观察行为，而不必真的搭一套 SPDK 硬件。

**操作步骤**：

1. 打开 [run_nof_heartbeat_tcp_e2e.sh](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/tests/e2e/run_nof_heartbeat_tcp_e2e.sh)，定位三个关键阶段：
   - **阶段 A（注入故障前）**：[:L134-L149](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/tests/e2e/run_nof_heartbeat_tcp_e2e.sh#L134-L149) 等客户端达到稳态成功数（`put_ok`/`get_ok` ≥ `PRE_FAULT_SUCCESS_TARGET`，默认 3）。
   - **阶段 B（注入故障）**：[:L153-L159](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/tests/e2e/run_nof_heartbeat_tcp_e2e.sh#L153-L159) `kill` 掉 SPDK target，然后等待 master 日志出现 `action=unmount_nof_segment_by_heartbeat`。
   - **阶段 C（验证）**：[:L206-L216](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/tests/e2e/run_nof_heartbeat_tcp_e2e.sh#L206-L216) 根据 `CLIENT_GLOBAL_SEGMENT_SIZE` 是否为 0 判定期望：非 0（有内存副本）→ 卸载 NoF 段后 I/O 仍应成功（服务连续性）；为 0（NoF-only）→ 卸载后 I/O 应开始失败（NoF 段是唯一副本来源）。
2. 把脚本顶部的心跳默认值（[:L21-L23](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/tests/e2e/run_nof_heartbeat_tcp_e2e.sh#L21-L23)：间隔 2s、超时 500ms、阈值 3）与 master 源码默认值（间隔 10s、超时 1000ms、阈值 3，见 [types.h:96-L98](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/include/types.h#L96-L98)）对比，理解「测试用更激进的心跳是为了缩短用例耗时」。

**需要观察的现象**：脚本里 `wait_for_pattern ... action=unmount_nof_segment_by_heartbeat` 的超时上限是 `HEARTBEAT_INTERVAL * HEARTBEAT_FAILURES + 20`。这正对应「连续 N 次失败才卸载」的语义——心跳间隔越短、阈值越小，故障检测越快，但误判风险也越高。

**预期结果**：你能解释「为什么生产环境不会把心跳间隔设成 1 秒」——太短会在网络抖动时把健康段误判为死亡并卸载，造成不必要的副本迁移与短暂失败；太长则故障检测窗口拉大。默认 10s/3 次（≈30s 检测窗口）是权衡后的取值。**待本地验证**：若有 build 目录与 SPDK，按 `BUILD_DIR=/path/to/build ./run_nof_heartbeat_tcp_e2e.sh` 跑一遍（需 passwordless sudo 用于 hugepages）。

#### 4.3.5 小练习与答案

**练习 1**：NoF 部署里，「创建 SSD 池」和「注册 SSD 池」是两步，能不能合并？为什么分成两步？

> **参考答案**：逻辑上可以串起来，但分成两步有运维意义。「创建」（`spdk_tgt_create`）是在 SSD 池节点上动手——绑驱动、起 `nvmf_tgt`、建 subsystem/namespace/listener，属于**变更存储侧基础设施**；「注册」（`mooncake_ssd_register`）只是把这些已存在的段登记到 master，属于**只读发现 + 登记**，且对「已存在」做了幂等（[mooncake_ssd_register.py:260-L263](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-wheel/mooncake/mooncake_ssd_register.py#L260-L263)）。分开后，SPDK target 重启不需要重新「创建」，只需重新「注册」即可恢复。

**练习 2**：master 心跳判定一个 NoF 段死亡并卸载后，如果该段后来恢复了，会自动重新挂载吗？

> **参考答案**：不会自动重新挂载。卸载（unmount）意味着 master 把该段从可用资源池移除；恢复后需要**重新执行注册**（`mooncake_ssd_register`）把它重新登记进来。这也呼应了练习 1——「注册」是可重复执行的、幂等的恢复手段。

**练习 3**：`--global-segment-size 0`（NoF-only 模式）下，杀掉 SPDK target 后客户端为什么开始失败，而不是像默认模式那样继续成功？

> **参考答案**：默认模式下客户端贡献了本地内存段（`global_segment_size > 0`），副本有「内存副本」兜底，NoF 段卸载后内存副本仍可读，故 I/O 仍成功（验证**服务连续性**）。NoF-only 模式下 `global_segment_size=0`，**所有副本都依赖远程 NoF 段**，NoF 段一旦卸载就无副本可读，I/O 必然失败（验证**NoF-only 失败行为**）。这正是脚本 [:L206-L216](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/tests/e2e/run_nof_heartbeat_tcp_e2e.sh#L206-L216) 两种分支的判定依据。

---

## 5. 综合实践

**任务**（即本讲规格里的代码实践任务）：按照部署指南，在单机上用 **P2P handshake 模式**跑通 `store_client_e2e.py` 端到端示例，记录所需的环境变量与配置，再**叠加开启本地 SSD offload**，观察容量变化。

### 阶段一：跑通单机 P2P handshake 端到端

**前置**：已按 [u1-l3 从源码构建](u1-l3-build-from-source.md) 编译出 `build/mooncake-store/src/mooncake_master` 与 `build/mooncake-integration/store*.so`。

**步骤**：

1. **起 master**（终端 1）：

   ```bash
   ./build/mooncake-store/src/mooncake_master \
     --enable_http_metadata_server=false \
     --logtostderr=true
   # P2P 模式用不到内嵌 HTTP 元数据, 这里关掉以保持拓扑最小。
   # master 监听 0.0.0.0:50051。
   ```

2. **起客户端工作负载**（终端 2）——这就是「跑通 store_client_e2e.py」：

   ```bash
   PYTHONPATH=./build/mooncake-integration \
   python3 mooncake-store/tests/e2e/store_client_e2e.py \
     --local-hostname 127.0.0.1:50071 \
     --metadata-server P2PHANDSHAKE \
     --master-server 127.0.0.1:50051 \
     --global-segment-size 67108864 \
     --local-buffer-size 33554432 \
     --payload-size 4096 \
     --duration-sec 20 \
     --sleep-ms 200 \
     --key-prefix p2p-demo
   ```

   关键点：`--metadata-server P2PHANDSHAKE` 是与本讲 4.1 的唯一呼应；`PYTHONPATH` 指向 `mooncake-integration` 才能 `import store`（见 [readme.md:133-L143](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/tests/e2e/readme.md#L133-L143)）。

3. **观察**：终端 2 应持续打印 `put_ok ...` / `get_ok ...`，最后输出一行 `summary put_ok=... put_fail=0 get_ok=... get_fail=0 mismatch=0`。`put_fail=0 get_fail=0 mismatch=0` 即代表端到端跑通。

**需要记录的配置清单**（填表）：

| 进程 | 关键参数/环境变量 | 取值 | 依据 |
|---|---|---|---|
| master | RPC 端口 | 50051（默认） | [master.cpp](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/src/master.cpp) `--rpc_port` |
| client | `--metadata-server` | `P2PHANDSHAKE` | 本讲 4.1.3 |
| client | `--global-segment-size` | 67108864（64 MB） | 贡献的 DRAM |
| client | `PYTHONPATH` | `./build/mooncake-integration` | `import store` 所在 |
| client | `--master-server` | `127.0.0.1:50051` | 指向 master |

### 阶段二：叠加本地 SSD offload，观察容量变化

在阶段一「能跑通」的基础上，让写入量超过 DRAM 池、触发 offload。

**步骤**：

4. **建 SSD 目录**：

   ```bash
   mkdir -p /tmp/mooncake_offload_demo   # 用一块够大的盘, 这里演示用 /tmp
   ```

5. **重启 master，开 offload 总开关**（终端 1，先 Ctrl-C 旧 master）：

   ```bash
   ./build/mooncake-store/src/mooncake_master \
     --enable_offload=true \
     --offload_on_evict=true \
     --logtostderr=true
   ```

6. **重启客户端，导出 offload 环境变量**（终端 2）：

   ```bash
   export MOONCAKE_OFFLOAD_FILE_STORAGE_PATH=/tmp/mooncake_offload_demo
   export MOONCAKE_OFFLOAD_STORAGE_BACKEND_DESCRIPTOR=bucket_storage_backend
   # 演示用, 显式设一个小的淘汰阈值, 避免 "0=90%物理盘" 的隐式行为
   export MOONCAKE_OFFLOAD_BUCKET_MAX_TOTAL_SIZE=$((512 * 1024 * 1024))  # 512 MB
   export MOONCAKE_OFFLOAD_BUCKET_EVICTION_POLICY=lru

   PYTHONPATH=./build/mooncake-integration \
   python3 mooncake-store/tests/e2e/store_client_e2e.py \
     --metadata-server P2PHANDSHAKE \
     --master-server 127.0.0.1:50051 \
     --global-segment-size 67108864 \
     --local-buffer-size 33554432 \
     --payload-size 4096 \
     --duration-sec 60 \
     --sleep-ms 20 \
     --key-prefix offload-demo
   ```

   > 注意：`store_client_e2e.py` 走的是 `store.setup(...)` 的 7 参数版（[store_client_e2e.py:39-L48](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-store/tests/e2e/store_client_e2e.py#L39-L48)），**不直接接受 `enable_ssd_offload` 形参**；本地 SSD offload 是否生效取决于编译产物是否暴露了 offload 开关以及底层 Real Client 是否读取上述环境变量。若该脚本未触发 offload，可改用 `python -m mooncake.mooncake_store_service`（其 `store.setup` 带 `enable_ssd_offload`/`ssd_offload_path` 尾参，见 [mooncake_store_service.py:123-L135](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-wheel/mooncake/mooncake_store_service.py#L123-L135)）作为承载 offload 的 Real Client，再用 `store_client_e2e.py` 之外的写入方式灌数据。

7. **观察容量变化**（终端 3，并行运行）：

   ```bash
   watch -n 2 'du -sh /tmp/mooncake_offload_demo; ls -1 /tmp/mooncake_offload_demo | head'
   # 同时 curl master 指标(若开了 metrics):
   # curl -s http://localhost:9003/metrics/summary
   ```

**预期现象**：
- 因 `global_segment_size`（64 MB）远小于持续写入量，DRAM 池很快到达高水位（0.95），触发淘汰 → 被淘汰对象经 offload 写入 SSD → `/tmp/mooncake_offload_demo` 出现 `*.bucket`/`*.meta` 文件，`du` 显示用量增长。
- 用量逼近 512 MB（`BUCKET_MAX_TOTAL_SIZE`）后，`lru` 策略开始淘汰最久未读的 bucket，用量在 512 MB 附近企稳而非无限增长——这就是「容量配额」生效的可观察证据。
- 对照阶段一：阶段一没有 offload，被淘汰对象直接丢弃，SSD 目录始终为空；阶段二 SSD 目录开始有数据。**这一对比正是「开启 SSD offload 带来容量扩展」的直接体现**。

**若无法本地运行**：明确标注「待本地验证」，但仍需完成「配置清单」表格与「阶段一 vs 阶段二容量行为对比」的文字推断，引用本讲源码行号作为依据。

## 6. 本讲小结

- **单机部署**用 **P2P handshake**（`metadata_server="P2PHANDSHAKE"`）即可，**省掉 TE 元数据进程**，但 master 进程不可省；它是官方推荐的入门方式，适合开发/测试/小集群，生产大规模仍建议 etcd。
- **SSD offload 一定发生在 Real Client 里**，有内嵌（Mode A）与独立 `mooncake_client`+DummyClient（Mode B）两种部署模式；`--enable_offload=true` 必须**在 master 与 Real Client 两边都设**。
- offload 的**容量是双开关**：`MOONCAKE_OFFLOAD_TOTAL_SIZE_LIMIT_BYTES`（磁盘用量上限）与（bucket 后端独有的）`MOONCAKE_OFFLOAD_BUCKET_MAX_TOTAL_SIZE`（淘汰阈值，**`0` 表示取物理盘 90%，不是无限制**）；且 `offset_allocator` 后端把前者**直接当预分配文件大小（100%）且不支持重启恢复**——后端不同语义不同。
- offload **只在写入量 > DRAM 池、触发淘汰时才激活**；DRAM 池（`global_segment_size`）是热层，SSD 是温/冷层，有效容量 ≈ min(物理盘, 配额)。
- **NVMe-oF（NoF）** 是跨节点的**远程 SSD 池**：Mooncake 服务节点（master/metadata/store）+ 若干 SSD 池节点（SPDK `nvmf_tgt`）；部署分「建池（`spdk_tgt_create`）」与「注册（`mooncake_ssd_register`，幂等）」两步。
- NoF 相比本地 offload 多了**心跳探活**：master 默认每 **10s** 探一次、单次超时 **1000ms**、连续 **3 次**失败才卸载远程段（≈30s 检测窗口）；e2e 脚本用更激进的心跳验证「杀 target → unmount → 服务连续性/NoF-only 失败」。

## 7. 下一步学习建议

- **offload 内部机制**：本讲只讲「怎么部署」，没讲「何时卸载、何时提升」。深入阅读 [u6-l4 offload 与 promotion](u6-l4-offload-promotion.md) 与 [u6-l2 多级存储](u6-l2-multi-tier-storage.md)，理解 `offload_on_evict`、`promotion_on_hit`、`promotion_admission_threshold` 背后的 CountMinSketch 准入与淘汰链路。
- **HA 与元数据生产部署**：本讲的单机/小集群拓扑都是单 master。要上生产，阅读 [u7-l1 HA 主备](u7-l1-ha-leader-standby.md)（master 多副本 + etcd/Redis/K8s 选主）与 [u7-l3 元数据后端](u7-l3-metadata-server-backends.md)（P2P/HTTP/etcd/Redis 选型与 Go runtime 互斥约束），把本讲的拓扑升级为高可用集群。
- **NoF 性能与上层接入**：阅读 [nvmf-ssd-deployment-guide.md 的 §6 性能测试](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/docs/source/deployment/nvmf-ssd-deployment-guide.md)（`nof_worker_pool_bench` 与 vLLM+LMCache 的 `MC_NOF_WORKERS`/`MC_NOF_SUBMIT_CHUNK_BYTES`/`MC_NOF_INFLIGHT_BYTES_LIMIT` 三个 QoS 参数），理解 NoF I/O 的并发与限流调优。
- **动手验证**：按综合实践阶段一，先把 P2P handshake 单机端到端跑通（最值得先拿下的里程碑），再叠加 offload 观察容量曲线，最后（若有 SPDK 硬件）尝试 NoF 心跳 e2e 脚本，把三种部署拓扑都亲手过一遍。
