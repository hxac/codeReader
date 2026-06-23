# Topology：硬件拓扑发现与设备选择

## 1. 本讲目标

学完本讲后，你应当能够：

1. 说清楚 **拓扑矩阵（Topology Matrix）** 到底记录了什么——为什么要把 NIC 分成「首选（preferred）」和「可用（avail）」两类。
2. 理解 `Topology` 类是如何通过 `/sys` 文件系统、`libibverbs`、CUDA Runtime 三种途径，自动发现 NUMA 节点、RDMA NIC（HCA）和 GPU 设备的。
3. 掌握 `selectDevice` 的两个重载——为什么首选 NIC 优先、重试时如何回退到全部 NIC、`hint` 又是用来做什么的。
4. 看懂 `scripts/generate_cluster_topology.py` 跨机器实测带宽/延迟、并用匈牙利算法匹配 NIC 的思路。
5. 能够对照 `topology.cpp` 解释「系统如何为一段内存选择最优 NIC」的完整链路。
6. 说清楚 GPU 的 PCI BDF（Bus:Device.Function）为什么要做安全小写化——即 `discoverCudaTopology` 为何改用 `char_util::to_lower` 而非直接 `tolower`，背后的「带符号 char 触发未定义行为（UB）」是什么。

---

## 2. 前置知识

本讲假设你已经读过 [u2-l1 Transfer Engine 总览](u2-l1-overview.md)，知道 Segment、Buffer、Transport、Batch Transfer 是什么。下面补充几个本讲必须的硬件概念。

### 2.1 NUMA 节点

多路服务器有多个 CPU 插槽，每个插座挂着自己「最近」的那块内存和 PCIe 设备，这就是一个 **NUMA 节点（NUMA node）**。访问本 NUMA 节点的内存很快，跨 NUMA 访问要走 UPI/QPI 互联，会变慢、还会挤占互联带宽。Linux 把 NUMA 信息暴露在 `/sys/devices/system/node/nodeN/` 下，CPU 核、内存页、PCIe 设备都各自带一个 `numa_node` 属性。

### 2.2 RDMA、NIC、HCA、InfiniBand、RoCE

- **NIC**：网卡，这里特指能做 RDMA 的高性能网卡。
- **HCA（Host Channel Adapter）**：RDMA 语境下对「主机侧 RDMA 适配器」的称呼，本讲里 NIC ≈ HCA，代码里的 `hca_list_` 就是「RDMA 网卡列表」。
- **InfiniBand / RoCE**：两种承载 RDMA 的网络协议。Mooncake 用 `libibverbs`（`ibv_*` 系列 API）统一管理它们，所以代码里看到的 `ibv_get_device_list` 对两者都适用。
- **`ibv_devices`**：libibverbs 自带的命令行工具，列出本机所有 RDMA 设备（如 `mlx5_0`、`mlx5_1`）。

### 2.3 为什么需要「拓扑感知」

> 引用项目文档原话：现代推理服务器通常由多个 CPU 插槽、DRAM、GPU 和 RDMA NIC 设备组成……传输可能受到 UPI 或 PCIe 交换机带宽限制。Transfer Engine 实现了**拓扑感知路径选择**：把 NIC 按「内存类型」分类成首选/次要列表，正常情况下用首选 NIC，实现本地 NUMA 内传输或仅跨本地 PCIe 交换机的 GPUDirect RDMA。

一句话总结：**把数据走「最近的网卡」发出去**，避免跨 NUMA、跨 PCIe 交换机造成的带宽塌陷。

### 2.4 一段真实的拓扑矩阵

这是文档 `cpp-api.md` 给出的实际格式，每个内存位置（`cpu:0`、`cuda:0`）对应两个数组：首选 NIC 和可用 NIC：

```json
"priority_matrix": {
    "cpu:0":  [["mlx5_2"], ["mlx5_3"]],
    "cpu:1":  [["mlx5_3"], ["mlx5_2"]],
    "cuda:0": [["mlx5_2"], ["mlx5_3"]]
}
```

含义：给 `cpu:0` 的内存发数据，首选 `mlx5_2`，实在不行才用 `mlx5_3`。这正是 `TopologyEntry` 要表达的东西。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [topology.h](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/include/topology.h) | `TopologyEntry` / `TopologyMatrix` / `Topology` 类的声明，定义了「名称字符串 → 首选/可用 HCA」的数据结构。 |
| [topology.cpp](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/src/topology.cpp) | 拓扑发现、JSON 解析、`resolve()` 编号化、`selectDevice()` 选路逻辑的全部实现。 |
| [char_util.h](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/include/char_util.h) | 单字节安全小写化工具 `char_util::to_lower`，专治「把带符号 `char` 直接喂给 `std::tolower` 触发 UB」的隐患（PCI BDF 小写化时用到）。 |
| [scripts/generate_cluster_topology.py](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/scripts/generate_cluster_topology.py) | 跨节点 SSH 工具：实测两机各 NIC 对之间的 RDMA 带宽/延迟，用匈牙利算法做最优 NIC 配对，产出集群级拓扑文件。 |
| [memory_location.h](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/include/memory_location.h) | `kWildcardLocation = "*"`、`segments:` 分段内存编码（用于把一段交错分布的 buffer 解析到具体 `cpu:N`）。 |
| [common.h](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/include/common.h) | `SimpleRandom`（设备选择里的轻量随机数）与 `transfer_engine_impl.cpp` 调用拓扑的入口。 |

---

## 4. 核心概念与源码讲解

### 4.1 拓扑矩阵与 `Topology` 类

#### 4.1.1 概念说明

`Topology` 是 Transfer Engine 用来回答一个核心问题的组件：

> 「给定一段内存（它的位置是 `cpu:0` / `cuda:2` / `*`），我应该用本机的哪块 RDMA 网卡去收发它？」

它内部维护一张 **拓扑矩阵 `matrix_`**，结构是 `内存位置字符串 → TopologyEntry`。每个 `TopologyEntry` 把本机所有 NIC 拆成两类：

- `preferred_hca`：首选 NIC——物理上「离这块内存近」的网卡（同 NUMA，或 PCIe 距离最短）。
- `avail_hca`：可用 NIC——其余网卡，正常不走，故障/重试时兜底。

#### 4.1.2 核心流程

`Topology` 的生命周期分三步：

```
        ┌─────────────┐   自动扫描硬件      ┌─────────────┐
        │ discover()  │ ─────────────────▶  │  matrix_    │  名称字符串 → {preferred, avail}
        │ 或 parse()  │   或读 JSON         │ (人类可读)   │
        └─────────────┘                     └──────┬──────┘
                                                   │ resolve()
                                                   ▼
                                          ┌─────────────────┐
        selectDevice("cpu:0", retry) ───▶ │ resolved_matrix_ │  名称 → 整数 HCA 下标
                                          │   (查表 O(1))    │
                                          └─────────────────┘
```

1. **填充阶段**：`discover()` 自动发现，或 `parse(json)` 直接吃一份手写拓扑。两者都只写 `matrix_`。
2. **编号化阶段**：`resolve()` 把 NIC 字符串（`mlx5_0`）统一编号成全局整数下标，存进 `resolved_matrix_`，供热路径 `selectDevice` 做 O(1) 查表。
3. **选路阶段**：每次传输调用 `selectDevice(location, retry_count)`，在 resolved 矩阵里挑一个下标返回。

#### 4.1.3 源码精读

先看数据结构。`TopologyEntry` 用两个 `vector<string>` 分别装首选/可用 NIC，并提供 `toJson()` 序列化成「两个数组的数组」：

[topology.h:37-56](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/include/topology.h#L37-L56) —— `TopologyEntry` 定义，`preferred_hca`/`avail_hca` 两个字段、以及把它们打包成 `[[preferred...],[avail...]]` 的 `toJson()`。

[topology.h:58-59](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/include/topology.h#L58-L59) —— `TopologyMatrix = unordered_map<string, TopologyEntry>`，key 就是 `cpu:0`、`cuda:1`、`*` 这类位置字符串。

接着是 `Topology` 类的对外接口，注意有两个 `selectDevice` 重载、`discover` 有带/不带 filter 两个版本：

[topology.h:61-95](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/include/topology.h#L61-L95) —— `Topology` 的公开 API：`discover`/`parse`/`disableDevice`/`selectDevice`(×2)/`getHcaList`，私有 `resolve()`。

`resolve()` 产出的不是字符串而是**带索引的**结构 `ResolvedTopologyEntry`，还顺带维护一张「名字 → 下标」的哈希表，用于 `hint` 查询：

[topology.h:102-123](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/include/topology.h#L102-L123) —— `ResolvedTopologyEntry` 持有 `vector<int>` 的首选/可用下标，以及 `getHcaIndex(name)` 把 NIC 名字翻译成下标（先查 preferred，再查 avail，找不到返回 -1）。

#### 4.1.4 代码实践：打印一张拓扑矩阵

**目标**：用最小的调用链，看到 `discover()` 之后 `matrix_` 长什么样。

**操作步骤**（源码阅读型，无需 RDMA 硬件也能做）：

1. 打开 [topology.cpp:526-546](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/src/topology.cpp#L526-L546) 阅读 `discover()`，确认它做了三件事：`listInfiniBandDevices` 拿 NIC、`discoverCpuTopology` 生成 `cpu:N` 条目、（若编译了 CUDA）`discoverCudaTopology` 生成 `cuda:N` 条目。
2. 再看 [topology.cpp:591-597](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/src/topology.cpp#L591-L597) 的 `toString()`——它遍历 `matrix_` 调每个 entry 的 `toJson()`，正是你想看的人类可读形式。

**预期结果**：在有多 NIC、多 NUMA 的机器上，`toString()` 会输出类似第 2.4 节那种 `{"cpu:0":[[...],[...]], "cpu:1":[[...],[...]]}` 的矩阵；在单 NIC 机器上，所有 `cpu:N` 的 `preferred_hca` 都会指向那唯一一块网卡。

**待本地验证**：单机无 RDMA 设备时 `discover()` 会返回空列表，`toString()` 为 `{}`，需要真实 RoCE/IB 网卡才能看到非平凡结果。

#### 4.1.5 小练习与答案

**练习 1**：`matrix_` 和 `resolved_matrix_` 为什么不能合并成一个？为什么要先填字符串版再做一次编号化？

> **参考答案**：`matrix_` 存人类可读的 NIC 名字，用于序列化（`toString`/`parse` 与 JSON 互通）和 `disableDevice` 这类按名字操作的接口；`resolved_matrix_` 存全局整数下标，因为热路径 `selectDevice` 每次传输都要调用，必须 O(1) 且避免重复字符串查找。`resolve()` 还顺带构造 `hca_list_`（全局 NIC 顺序），让上层 `RdmaTransport` 能用下标直接索引 `context_list_[device_id]`。

**练习 2**：`selectDevice` 返回的整数下标含义是什么？它的取值范围由谁决定？

> **参考答案**：返回的是 NIC 在全局 `hca_list_` 中的下标（由 `resolve()` 里的 `hca_id_map` 统一分配，`0..N-1`）。上层用它去取对应的 `RdmaContext`。所以「设备选择」本质是「在 N 块网卡里挑一块」。

---

### 4.2 硬件发现：NUMA、NIC、GPU

#### 4.2.1 概念说明

`discover()` 必须把三类硬件信息凑齐：

1. **RDMA NIC（HCA）**：用 `libibverbs` 枚举，再从 `/sys/class/infiniband/<name>/../..` 解析出它的 **PCI 总线号** 和 **NUMA 节点**。
2. **CPU NUMA 节点**：扫 `/sys/devices/system/node/nodeN/`，每个节点生成一条 `cpu:N`。
3. **GPU**（若编译开启）：用 CUDA Runtime 拿到每张卡的 PCI 总线号，再和 NIC 算「PCIe 距离」。

发现的关键判据是 **NUMA 亲和**：同 NUMA 的 NIC 优先；GPU 还要进一步看 **PCIe 距离**（是不是挂在同一个 PCIe 交换机下）。

#### 4.2.2 核心流程

NIC 枚举会做三层「可用性体检」，任何一层不过就跳过该 NIC：

```
ibv_get_device_list          # 拿到全部 RDMA 设备
   │  对每个设备：
   ├─ getIbvDeviceWhitelist  # MC_TE_FILTERS 环境变量做白名单过滤
   ├─ isIbDeviceAvailable
   │     ├─ isIbDeviceAccessible  # /dev/infiniband/<name> 可读写？
   │     └─ checkIbDevicePort     # 端口有 GID、且处于 IBV_PORT_ACTIVE？
   └─ 从 /sys 读 PCI 总线号 + numa_node
```

CPU 拓扑的判据很直接——**NIC 的 NUMA == CPU 节点号，就进 preferred，否则进 avail**：

```
for 每个 /sys/devices/system/node/nodeN:
    for 每块 HCA:
        if hca.numa_node == N: preferred  否则: avail
```

GPU 拓扑更精细，分两步：先按 NUMA 同/不同筛出候选集，再在候选集里按 **PCIe 距离最小** 选 preferred：

```
for 每张 GPU i (cudaDeviceGetPCIBusId):
    same_numa = [同 NUMA 的 HCA]              # 第一优先级：NUMA 亲和
    candidates = same_numa 非空 ? same_numa : all_hca
    for candidates 里每块 HCA:
        d = getPciDistance(gpu.pci, hca.pci)  # 解析 /sys/bus/pci/devices/ 真实路径
        记录 min_distance 的 HCA 们 → preferred，其余 → avail
```

#### 4.2.3 源码精读

**NIC 可用性体检**——三层过滤，确保选出来的 NIC 真能用：

[topology.cpp:44-71](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/src/topology.cpp#L44-L71) —— `isIbDeviceAccessible`：检查 `/dev/infiniband/<name>` 存在、是字符设备、且当前用户可读写。

[topology.cpp:73-98](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/src/topology.cpp#L73-L98) —— `checkIbDevicePort`：用 `ibv_query_port` 确认端口有 GID 表项且 `state == IBV_PORT_ACTIVE`，避免选到没插线/Down 的口。

[topology.cpp:100-138](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/src/topology.cpp#L100-L138) —— `isIbDeviceAvailable`：遍历物理端口，只要有一个 active 就算可用。

**白名单 + PCI/NUMA 解析**：

[topology.cpp:159-188](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/src/topology.cpp#L159-L188) —— `getIbvDeviceWhitelist`：读取 `MC_TE_FILTERS`（逗号分隔的 NIC 名），非空则只放行名单内设备。

[topology.cpp:225-245](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/src/topology.cpp#L225-L245) —— `listInfiniBandDevices` 里解析 NIC 的 PCI 总线号（`realpath` 跟随 `/sys/class/infiniband/<name>/../..` 符号链接）和 `numa_node`（直接读 `/sys/.../numa_node` 文件）。

**CPU 拓扑生成**——NUMA 亲和判据的核心：

[topology.cpp:355-390](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/src/topology.cpp#L355-L390) —— `discoverCpuTopology`：遍历每个 NUMA 节点，`hca.numa_node == node_id` 的 NIC 进 `preferred_hca`，其余进 `avail_hca`，生成 `cpu:N` 条目。

**GPU 拓扑生成**——PCIe 距离判据：

[topology.cpp:396-424](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/src/topology.cpp#L396-L424) —— `getPciDistance`：把两个 PCI 设备的 `/sys/bus/pci/devices/<bus>` 用 `realpath` 解析成真实路径，再数「公共前缀之后还剩多少个 `/`」。共享前缀越长（挂在同一 PCIe 交换机）距离越小。

[topology.cpp:437-496](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/src/topology.cpp#L437-L496) —— `discoverCudaTopology`：`cudaDeviceGetPCIBusId` 拿 GPU 的 PCI 总线号，紧接着 [topology.cpp:450](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/src/topology.cpp#L450) 用 `char_util::to_lower` 把它整体小写化（理由见 4.5 节）；先按 `isSameNumaNode` 筛同 NUMA 候选（[topology.cpp:426-435](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/src/topology.cpp#L426-L435)），再用 `getPciDistance` 取距离最小的 HCA 作 preferred。

#### 4.2.4 代码实践：手动复现 NUMA 亲和判定

**目标**：不依赖编译，直接用 shell 模拟 `discoverCpuTopology` 的判据，理解「为什么这块 NIC 被分给 `cpu:0`」。

**操作步骤**：

```bash
# 1. 列出本机 NUMA 节点（对应 /sys/devices/system/node/nodeN）
ls -d /sys/devices/system/node/node*

# 2. 列出本机 RDMA 网卡
ibv_devices 2>/dev/null || ls /sys/class/infiniband/

# 3. 查每块 NIC 的 NUMA 亲和（对应 topology.cpp:239-241 的读 numa_node）
for dev in $(ls /sys/class/infiniband/ 2>/dev/null); do
  node=$(cat /sys/class/infiniband/$dev/device/numa_node)
  echo "$dev -> NUMA $node"
done
```

**需要观察的现象**：理想情况下，每块 NIC 的 `numa_node` 会和某个 CPU NUMA 节点对上。按 `discoverCpuTopology` 的判据，`mlx5_0` 若 `numa_node==0`，就会被放进 `cpu:0` 的 `preferred_hca`。

**预期结果**：在双路 + 多 NIC 的服务器上（例如 8×H800 + 8×mlx5 + 2 NUMA，见 `sglang-hicache-benchmark-results-v1.md` 的典型配置），你会看到 NIC 在 NUMA 0 和 NUMA 1 上大致各占一半，正好对应 `cpu:0` 和 `cpu:1` 各有 4 块 preferred NIC。

**待本地验证**：若 `numa_node` 为 `-1`（内核未暴露或虚拟机），该 NIC 不会匹配任何 `cpu:N`，只能落到通配符 `*` 或被归为 avail。

#### 4.2.5 小练习与答案

**练习 1**：假设机器有 2 个 NUMA 节点、4 块 NIC（`mlx5_0/1` 在 NUMA 0，`mlx5_2/3` 在 NUMA 1）。写出 `discoverCpuTopology` 生成的矩阵。

> **参考答案**：
> ```
> cpu:0 -> preferred:[mlx5_0, mlx5_1], avail:[mlx5_2, mlx5_3]
> cpu:1 -> preferred:[mlx5_2, mlx5_3], avail:[mlx5_0, mlx5_1]
> ```

**练习 2**：`getPciDistance` 为什么用「数 `/` 个数」而不是比较总线号的字符串？

> **参考答案**：PCI 总线号本身是平的（如 `0000:81:00.0`），无法体现「是否挂在同一个 PCIe 交换机下」。而 `realpath` 把 `/sys/bus/pci/devices/<bus>` 解析成真实路径后，**共享路径前缀的长度**就反映了拓扑层级——挂在同一个 switch 下的设备路径前缀更长、剩下的 `/` 更少，所以「数公共前缀之后的 `/`」恰好近似 PCIe 拓扑跳数。

---

### 4.3 设备选择：`selectDevice`

#### 4.3.1 概念说明

发现 + 编号只是准备，真正的热路径是 `selectDevice`。它有两条规则：

1. **首选优先**：优先在 `preferred_hca` 里挑；只在 preferred 为空时才退到 `avail_hca`。
2. **重试回退**：第一次（`retry_count == 0`）随机选一个 preferred；如果那条路失败要重试（`retry_count > 0`），就按 `(retry_count-1) % total` 轮转，把 preferred 和 avail **全部** NIC 都纳入候选——这是故障兜底机制。

还有一个带 `hint` 的重载：调用方明确指定「我想用 `mlx5_2`」，拓扑就用 `getHcaIndex` 把名字翻成下标返回（找不到才走正常选路）。

> 关于随机性：默认用 `SimpleRandom`（一个线性同余 PRNG）在 preferred 里随机选，目的是**把流量分散到多块首选 NIC 上做多 NIC 带宽聚合**；设置环境变量 `MC_PATH_ROUNDROBIN` 后改成线程内轮转计数器。

#### 4.3.2 核心流程

`selectDevice(location, retry_count)` 的决策树：

```
resolved_matrix_ 里查 location        # 找不到 → ERR_DEVICE_NOT_FOUND
retry_count == 0 ?
├─ 是：rand = SimpleRandom.next() 或 round_robin 计数器
│       preferred 非空 → return preferred[rand % |preferred|]
│       否则           → return avail[rand % |avail|]
└─ 否：index = (retry_count-1) % (|preferred|+|avail|)
       index < |preferred| ? preferred[index] : avail[index-|preferred|]
```

带 hint 的重载先走捷径：

```
hint 非空 → getHcaIndex(hint) 命中则直接返回该下标
未命中（或 hint 空）→ 退化为上面的无 hint 选路
```

#### 4.3.3 源码精读

**带 hint 的重载**——先查名字，查不到回退：

[topology.cpp:607-624](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/src/topology.cpp#L607-L624) —— `selectDevice(storage_type, hint, retry_count)`：`hint` 非空时用 `entry.getHcaIndex(hint)`（定义在 [topology.h:110-120](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/include/topology.h#L110-L120)）直接定位下标；找不到或 hint 空就委托给无 hint 版本。

**无 hint 的核心选路**：

[topology.cpp:626-653](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/src/topology.cpp#L626-L653) —— 关键两分支：`retry_count==0` 时随机/轮转选 preferred；`retry_count>0` 时按 `(retry_count-1) % total` 轮转，preferred 和 avail 一起参与。

**随机源**：

[common.h:672-696](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/include/common.h#L672-L696) —— `SimpleRandom`：线性同余（`a=1664525, c=1013904223`），`Get()` 返回 thread_local 实例，种子用纳秒时间，保证不同线程选不同 NIC。

**轮转开关**：

[topology.cpp:499-506](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/src/topology.cpp#L499-L506) —— 构造函数读 `MC_PATH_ROUNDROBIN`，决定 `use_round_robin_`，进而决定 `retry_count==0` 分支用随机还是轮转。

**调用方**——看上层怎么用这个返回值：

[rdma_transport.cpp:741-777](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/src/transport/rdma_transport/rdma_transport.cpp#L741-L777) —— `RdmaTransport::selectDevice`：先按 offset 落到具体 buffer，再把 buffer 名（`cpu:0` 或 `segments:` 编码）作为 `location` 传给 `desc->topology.selectDevice`；如果该 location 选不到，就用 `kWildcardLocation = "*"` 兜底。

#### 4.3.4 代码实践：跟踪一次 `selectDevice` 决策

**目标**：在内存里推演 `selectDevice("cpu:0", retry_count)` 的返回值，验证「首选优先 + 重试回退」。

**操作步骤**（源码阅读 + 手动推演）：

设 `cpu:0` 的 `preferred=[mlx5_0, mlx5_1]`（下标 0、1），`avail=[mlx5_2, mlx5_3]`（下标 2、3），`total=4`。

1. 读 [topology.cpp:626-653](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/src/topology.cpp#L626-L653)。
2. 推演三个调用：
   - `selectDevice("cpu:0", 0)` → `retry_count==0`，随机/轮转落在 preferred，返回 `0` 或 `1`。
   - `selectDevice("cpu:0", 1)` → `index=(1-1)%4=0` → preferred[0] = `mlx5_0`。
   - `selectDevice("cpu:0", 3)` → `index=(3-1)%4=2` → 因为 `2 >= |preferred|=2`，退到 `avail[2-2]=avail[0]=mlx5_2`。
3. 结合 [worker_pool.cpp](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/src/transport/rdma_transport/worker_pool.cpp) 里对 `selectDevice` 的多次调用（行 117、130、424），观察上层如何通过递增 `retry_count` 来「试遍所有 NIC」。

**预期结果**：当某条 NIC 路径传输失败时，上层会以递增的 `retry_count` 反复调用 `selectDevice`，`% total` 的轮转特性保证了每一次重试都换一块**还没试过的** NIC，直到 avail 池也耗尽。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `retry_count>0` 分支要把 `avail_hca` 也纳入，而 `retry_count==0` 不纳入？

> **参考答案**：`retry_count==0` 是首次尝试，只走「最优」的 preferred NIC，保证正常情况下带宽最大、延迟最低。一旦失败说明首选路有问题（比如对端该 NIC 没起来、或链路故障），重试时就必须把 avail（次优但可用）也纳入候选，提高传输成功率。这是「正常最优、异常兜底」的典型设计。

**练习 2**：`hint` 参数有什么实际用途？谁会传一个非空 hint？

> **参考答案**：`hint` 让上层强制指定某块 NIC。典型场景是已经通过握手知道对端用的是某条 NIC（`peer_nic_path`），希望本地用「配对」的那块 NIC 发起连接，避免再随机选一个导致跨路。`rdma_transport.cpp` 的 `selectDevice` 重载里，`hint` 来自 buffer 维度的绑定信息。

---

### 4.4 集群拓扑脚本 `generate_cluster_topology.py`

#### 4.4.1 概念说明

`Topology` 类解决的是**单机**问题（本机内存 → 本机哪块 NIC）。但跨节点传输还涉及「本机 NIC A 到对机 NIC B 的实测带宽/延迟」——这是 `generate_cluster_topology.py` 做的事：

> 通过 SSH 到两台机器，遍历所有 NIC 对（笛卡尔积），用 `ib_write_bw` / `ib_read_lat` 实测每对 NIC 的带宽和延迟，再用**匈牙利算法（linear_sum_assignment）**做最优配对，最终把结果存成 `cluster-topology.json`。

它和 `Topology` 类的关系：脚本产出的是**跨机实测**的优先级数据，可作为人工调优输入；而 `Topology` 类做的是**单机自动发现**。两者互补。

#### 4.4.2 核心流程

```
main()
  ├─ SSH 取两机 machine-id（用作去重 key）
  ├─ list_rdma_devices(src) / list_rdma_devices(dst)   # 各自 ibv_devices + 读 numa_node
  ├─ for (dev1, dev2) in 笛卡尔积(devices_src, devices_dst):
  │      run_rdmatest()  # numactl 绑定 + ib_write_bw 测带宽 + ib_read_lat 测延迟
  ├─ process_host_pair(record):
  │      build_partition_map()         # 按 "src_numa-dst_numa" 分组
  │      solve_partition_group()       # 每组内用匈牙利算法最小化总延迟
  │          └─ scipy linear_sum_assignment
  └─ save_results → cluster-topology.json
```

关键设计点：

- **`numactl` 绑定**：测试时把进程绑到 NIC 所在的 NUMA 节点（`--cpunodebind --membind`），这样测出的才是「最优路径」的真实带宽，而不是被跨 NUMA 拖累的值。
- **按 NUMA 分区匹配**：先按 `src_numa-dst_numa` 把 NIC 对分组，再在每组内用匈牙利算法做一一配对（保证每块 src NIC 尽量配到延迟最低的 dst NIC）。

#### 4.4.3 源码精读

**列设备**——shell 拼出 `ibv_devices`，再读 sysfs 的 numa：

[generate_cluster_topology.py:51-67](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/scripts/generate_cluster_topology.py#L51-L67) —— `list_rdma_devices`：远程 `ibv_devices` 取设备名，再 `cat /sys/class/infiniband/<dev>/device/numa_node`，返回 `[{name, numa_node}]`。注意它的路径和 C++ 侧 `topology.cpp:240` 读的是同一个 sysfs 属性，逻辑一致。

**实测带宽/延迟**：

[generate_cluster_topology.py:92-128](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/scripts/generate_cluster_topology.py#L92-L128) —— `run_rdmatest`：对端起 `ib_write_bw` server，本机用 `numactl` 绑定后跑 client 测带宽；再用 `ib_read_lat` 测延迟。

[generate_cluster_topology.py:95-98](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/scripts/generate_cluster_topology.py#L95-L98) —— `numactl_prefix`：NUMA 非负时拼出 `--cpunodebind=N --membind=N`，否则空串（不绑定）。

**匈牙利算法配对**：

[generate_cluster_topology.py:154-193](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/scripts/generate_cluster_topology.py#L154-L193) —— `solve_partition_group`：把 src×dst NIC 的延迟组成代价矩阵，归一化后调 `scipy.optimize.linear_sum_assignment`（匈牙利算法）求最小代价的一一匹配。

[generate_cluster_topology.py:196-213](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/scripts/generate_cluster_topology.py#L196-L213) —— `process_host_pair`：按 NUMA 对分组、求最优匹配，剩下的额外 NIC 用 `allow_partial=True` 再匹配一次，分别存进 `partition_matchings`。

**主流程**：

[generate_cluster_topology.py:217-270](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/scripts/generate_cluster_topology.py#L217-L270) —— `main`：取 machine-id、笛卡尔积遍历测试、覆盖已有条目时交互确认、最后写 JSON。

#### 4.4.4 代码实践：运行脚本并解读输出

**目标**：在两台有 RDMA 网卡的机器上跑脚本，得到 `cluster-topology.json`，并理解其中的 `partition_matchings`。

**操作步骤**：

1. 确认两台机器都装了 `ibverbs-utils`/`perftest`（提供 `ib_write_bw`、`ib_read_lat`）和 Python 依赖：

   ```bash
   pip install paramiko tqdm numpy scipy
   ```

2. 在源机执行（`dst-host` 为对端主机名/IP）：

   ```bash
   cd /path/to/Mooncake
   python3 scripts/generate_cluster_topology.py \
       --src-host localhost --dst-host node071 --file cluster-topology.json
   ```

   多 NIC 时加 `--sudo` 让远端命令有权限操作 RDMA 设备。

3. 查看产物：

   ```bash
   python3 -c "import json;print(json.dumps(json.load(open('cluster-topology.json')),indent=2))"
   ```

**需要观察的现象**：

- `endpoints` 数组里每条记录含 `src_dev/dst_dev/src_numa/dst_numa/bandwidth/latency`。
- `partition_matchings` 按 `"src_numa-dst_numa"` 分组，每组是被匈牙利算法选中的「最优 NIC 对」。

**预期结果**：同 NUMA 对的 NIC 配对（例如 `0-0`）通常延迟最低、带宽最高；跨 NUMA 对（`0-1`）性能较差。这从实测角度印证了 C++ 侧「同 NUMA preferred」的设计直觉。

**待本地验证**：本实践依赖真实双机 RDMA 环境（含 SSH 免密或 `--sudo`）。若仅有单机多 NIC，可把 `--src-host` 和 `--dst-host` 都设为 `localhost`，脚本会走 `is_local_host` 分支用本地 subprocess 测试本机 NIC 对——但本机 NIC 对间测的是 loopback/同交换机带宽，仅作功能验证。

#### 4.4.5 小练习与答案

**练习 1**：脚本为什么要先用 `build_partition_map` 按 `src_numa-dst_numa` 分组，再分别做匈牙利匹配，而不是把所有 NIC 对扔进一个大矩阵？

> **参考答案**：NUMA 亲和是强约束——属于不同 NUMA 对的 NIC 不应互相争夺配对资格。分组后，每块 src NIC 只在自己「同 NUMA 对」的候选 dst NIC 里找最优，避免匈牙利算法把跨 NUMA 的次优配对和同 NUMA 的最优配对混在一起算总代价，保证配对结果符合物理拓扑。

**练习 2**：`run_rdmatest` 里为什么要用 `numactl --cpunodebind --membind`？如果去掉会怎样？

> **参考答案**：绑定 CPU 和内存到 NIC 所在 NUMA 节点，是为了测出「走最优路径」的带宽上限——这正是 C++ 侧 `selectDevice` 选 preferred NIC 想达到的效果。去掉绑定后，测试进程的内存可能落在另一个 NUMA，测出的带宽会被跨 NUMA 访问拖低，得到的就不是 NIC 本身的能力，失去了调优参考价值。

---

### 4.5 深入：PCI BDF 安全小写化与 `char_util::to_lower`

#### 4.5.1 概念说明

在 4.2 节的 GPU 拓扑发现里，有一步容易被忽略却很关键：拿到 GPU 的 PCI 总线号后，要把它整体**小写化**，再去和 NIC 的 `pci_bus_id` 比较。这一步就是 [topology.cpp:450](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/src/topology.cpp#L450) 那行：

```cpp
for (char *ch = pci_bus_id; (*ch = to_lower(*ch)); ch++);
```

先说三个背景概念：

1. **PCI BDF（Bus:Device.Function）**：PCI 设备在总线上的地址，形如 `0000:01:00.0`。`cudaDeviceGetPCIBusId` 返回的串里十六进制字母 `A–F` 可能是大写；而 NIC 那侧从 `/sys` 拿到的 `pci_bus_id`（`basename(realpath(...))`）是小写。**两边大小写不一致就无法正确比较 NUMA 亲和和 PCIe 距离**，所以 GPU 这一侧必须先统一转小写。

2. **`char` 的符号性**：C/C++ 标准并没有规定 `char` 是有符号还是无符号。但在 x86-64 / ARM 的 Linux 上，gcc/clang 默认把 `char` 当**有符号**（`signed char`）。于是任意字节值 `b` 满足 \(0 \le b \le 255\)，当 \(b > 127\) 时存进 `char` 会变成负数（\(b=128\) 即 `0x80` → `-128`）。

3. **`std::tolower` 的契约**：`<cctype>` 里 `int std::tolower(int c)` 规定——实参**必须**能表示为 `unsigned char`，或等于 `EOF`（通常是 `-1`）。**除此之外的任何值都是未定义行为（UB）**。把一个负的 `char`（非 EOF）直接传进去，正好踩中这条 UB。

把 2 和 3 拼起来就是隐患所在：`for (... (*ch = tolower(*ch)) ...)` 这种写法，一旦 `*ch` 是个 \(>0x7F\) 的字节，`char` 被隐式提升为负的 `int` 传给 `tolower`，标准意义上是 UB。提交 `b92cf05f`（"[TE] Fix signed-char tolower UB in PCI BDF lowercasing loops"）正是为修掉它而引入了 `char_util.h`。

> 直觉总结：`tolower` 看起来人畜无害，但它的参数有「必须是 `unsigned char` 范围」的隐藏前提；只要源头是 `char`，就该先 `static_cast<unsigned char>` 再传。这是 C/C++ 里一个经典的「字符处理坑」。

#### 4.5.2 核心流程

`char_util::to_lower` 的实现只有一行，但这一行正是修 UB 的关键：

```cpp
// char_util.h
static inline char to_lower(char c) {
    return static_cast<char>(
        std::tolower(static_cast<unsigned char>(c)));  // 关键：先转 unsigned char
}
```

数据流是这样的：

```
   char c                 (可能为负，如 0x80 → -128)
      │  static_cast<unsigned char>
      ▼
   unsigned char          (一定是 0..255，例如 0x80)
      │  隐式提升为 int
      ▼
   std::tolower(int)      ← 此时实参满足契约，无 UB
      │
      ▼
   static_cast<char>      (还原回 char 写回缓冲区)
```

调用点 [topology.cpp:450](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/src/topology.cpp#L450) 的循环语义：从 `pci_bus_id` 首字节开始，逐字节 `to_lower` 后**写回原位**，赋值表达式的值作为循环条件——遇到 `\0`（值为 0）时条件为假、循环结束。所以这一行同时完成「小写化」和「遍历到结尾」两件事。

#### 4.5.3 源码精读

**工具函数本体**——注意注释把「为何要转 `unsigned char`」直接写进了源码：

[char_util.h:22-27](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/include/char_util.h#L22-L27) —— `to_lower(char c)`：先 `static_cast<unsigned char>(c)` 消除「负值传给 `tolower`」的 UB，再把结果转回 `char` 返回。注释明确指出「直接把（可能带符号的）`char` 传给 `std::tolower` 在字节 \(>0x7F\) 时是 UB」。

**唯一调用点**——GPU 拓扑发现里 PCI BDF 的逐字节小写化：

[topology.cpp:450](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/src/topology.cpp#L450) —— `cudaDeviceGetPCIBusId` 取到的 `pci_bus_id` 紧接着逐字节过一遍 `to_lower`，之后才能拿去和 NIC 的 `pci_bus_id`（小写）做 `isSameNumaNode` / `getPciDistance` 比较。

**这次更新的提交背景**：`b92cf05f` 把 `#include <ctype.h>` 换成 `#include "char_util.h"`，并把 [topology.cpp:450](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/src/topology.cpp#L450) 原来的裸 `tolower` 改成 `char_util::to_lower`。修改前后**只动了符号化（to_lower）这一处逻辑**，行号净变化为 0（删一个 `ctype.h`、加一个 `char_util.h`），所以本讲其它行号引用依然有效。

#### 4.5.4 代码实践：说清「为何要用 `char_util::to_lower` 而非直接 `tolower`」

**目标**：通过一个最小实验确认「`char` 是有符号 + 字节 \(>0x7F\) 会变成负数」，从而理解为什么直接 `tolower(*ch)` 是 UB、为什么 `char_util::to_lower` 是正确写法。这是本讲规格明确要求的实践。

**操作步骤**：

1. 阅读本次更新的 diff（已贴在 4.5.3），确认改动就是「裸 `tolower` → `char_util::to_lower`」。

2. 写一个最小验证程序（**示例代码，非项目源码**），观察 `char` 的符号性以及 \(>0x7F\) 字节的负值：

   ```cpp
   // demo_signed_char.cpp —— 示例代码，仅用于理解 UB，不是 Mooncake 的文件
   #include <cctype>
   #include <iostream>
   #include <limits>

   int main() {
       std::cout << "char is_signed? "
                 << std::numeric_limits<char>::is_signed << "\n";  // 多数平台打印 1
       unsigned char u = 0x80;            // 128
       char c = static_cast<char>(u);
       std::cout << "char(0x80) as int = " << (int)c << "\n";     // signed char 下打印 -128
       // 标准契约：传给 tolower 的实参必须是 unsigned char 或 EOF。
       // 直接传 *负的* c（不是 EOF）—— 标准意义上是 UB：
       int r_bad = std::tolower(c);
       // 正确写法：先转 unsigned char：
       int r_good = std::tolower(static_cast<unsigned char>(c));
       std::cout << "tolower(负) = " << r_bad
                 << ", tolower(unsigned char) = " << r_good << "\n";
       return 0;
   }
   ```

   编译运行：

   ```bash
   g++ -O2 -o demo_signed_char demo_signed_char.cpp && ./demo_signed_char
   ```

3. 对照 [char_util.h:25-27](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/include/char_util.h#L25-L27)，确认它的 `to_lower` 走的正是第 2 步里的「正确写法」。

**需要观察的现象**：

- `char is_signed?` 在 x86-64 / ARM Linux + gcc/clang 下通常打印 `1`（有符号）。
- `char(0x80) as int` 打印 `-128`——证明一个 \(>0x7F\) 的字节存进 `char` 后是负数。
- `tolower(负)` 与 `tolower(unsigned char)` 在你的平台/glibc 上**结果可能碰巧一样**（见下方说明），但这并不能否定「直接传负值是 UB」。

**预期结果**：你能用自己的话回答：「`char` 默认有符号，PCI BDF 串里的字节若为 \(>0x7F\) 会被解释成负数；`std::tolower` 要求实参落在 `unsigned char` 范围或等于 `EOF`，传负值是 UB。`char_util::to_lower` 在调用 `tolower` 前先 `static_cast<unsigned char>`，从根上消除了这个 UB。」

**待本地验证 / 重要说明**：glibc 的 `tolower` 内部用的是「下标可取负值」的查表实现（表指针已预留了 `[-128, ...]` 的偏移），所以**在 glibc 上即便传负值也常常『看起来正常』**，常规测试甚至常规 sanitizers 都未必报错。但这只是**实现层面的巧合**，不改变标准意义上的 UB——换 libc、换优化级别或启用更严格的检查时随时可能爆雷。正因为「不会立即出错」，这类 UB 才特别隐蔽，值得用一个专门的 `char_util::to_lower` 把它彻底关掉。

#### 4.5.5 小练习与答案

**练习 1**：PCI BDF 串（如 `0000:01:00.0`）全是 ASCII，理论上没有 \(>0x7F\) 的字节。那为什么还要把 `tolower` 换成 `char_util::to_lower`？

> **参考答案**：当前 PCI BDF 确实都是 ASCII，所以「今天不触发」不代表「代码正确」。`topology.cpp:450` 的循环对缓冲区里**每个字节**调用 `tolower`，只要 API 未来返回的串、或缓冲区内容里出现一个 \(>0x7F\) 的字节，UB 就会成立。UB 的可怕之处在于「看似能跑」——glibc 的查表实现恰好容忍负下标，常规测试测不出问题。用 `char_util::to_lower` 把它改成「按定义正确」，是消除这类**潜伏型 UB**的标准做法，也让代码对未来的输入变化天然免疫。

**练习 2**：为什么不直接把 `pci_bus_id` 声明成 `unsigned char[]` 来绕开问题？为什么选择做一个 `char_util::to_lower` 工具？

> **参考答案**：`pci_bus_id` 的类型由 `cudaDeviceGetPCIBusId(char*, ...)` 这个 CUDA API 的签名决定（参数就是 `char*`），调用方改不了；后续 `isSameNumaNode`、`getPciDistance` 也都按 `const char*` / C 字符串处理。把缓冲区改 `unsigned char` 会牵动整条调用链的类型，得不偿失。正确而轻量的做法是在「真正调用 `tolower` 的那一处」做边界转换——即 `char_util::to_lower` 只在工具内部 `static_cast<unsigned char>`，对外仍保持 `char` 接口，既修了 UB，又不污染上层类型。

---

## 5. 综合实践

把本讲的三个最小模块串起来，完成一个「从硬件发现到设备选择」的端到端推演。

**任务**：假设一台双路服务器，2 个 NUMA 节点、4 块 RDMA NIC、2 张 GPU，配置如下：

| 设备 | PCI/NUMA |
| --- | --- |
| `mlx5_0` | NUMA 0 |
| `mlx5_1` | NUMA 0 |
| `mlx5_2` | NUMA 1 |
| `mlx5_3` | NUMA 1 |
| `cuda:0` | NUMA 0，挂在 mlx5_0 同一 PCIe 交换机 |
| `cuda:1` | NUMA 1，挂在 mlx5_2 同一 PCIe 交换机 |

**步骤**：

1. **画出 `matrix_`**。参照 [topology.cpp:355-390](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/src/topology.cpp#L355-L390) 写出 `cpu:0`、`cpu:1` 两个条目（同 NUMA 进 preferred）；参照 [topology.cpp:437-496](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/src/topology.cpp#L437-L496) 推演 `cuda:0`、`cuda:1`（先按 NUMA 缩小候选，再按 `getPciDistance` 取最近）。

2. **推演 `selectDevice`**。对一段注册为 `cpu:0` 的内存，分别算 `selectDevice("cpu:0", 0)` 和 `selectDevice("cpu:0", 2)`（`retry_count>0`）的候选集合。

3. **对照调用方**。阅读 [rdma_transport.cpp:757-774](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/src/transport/rdma_transport/rdma_transport.cpp#L757-L774)，确认当 `cpu:0` 选不到时，上层用 `kWildcardLocation="*"` 兜底，而 `resolve()` 把所有 NIC 都注册进了 `"*"` 的 preferred（见 [topology.cpp:666-669](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/src/topology.cpp#L666-L669)）。

**参考答案**：

```
matrix_:
  cpu:0  -> preferred:[mlx5_0, mlx5_1], avail:[mlx5_2, mlx5_3]
  cpu:1  -> preferred:[mlx5_2, mlx5_3], avail:[mlx5_0, mlx5_1]
  cuda:0 -> preferred:[mlx5_0],         avail:[mlx5_1, mlx5_2, mlx5_3]   # NUMA0 候选里 mlx5_0 距离最近
  cuda:1 -> preferred:[mlx5_2],         avail:[mlx5_0, mlx5_1, mlx5_3]

selectDevice("cpu:0", 0): 在 [mlx5_0, mlx5_1] 随机/轮转
selectDevice("cpu:0", 2): index=(2-1)%4=1 → preferred[1]=mlx5_1
"*" 兜底: preferred 含全部 4 块 NIC，保证任何 location 都至少能选出设备
```

**待本地验证**：`cuda:N` 的 preferred 严格取决于 `getPciDistance` 的实测路径前缀，上面的单选结果是「假设同交换机距离为 1、异交换机距离更大」的推演；真实机器需按 `/sys/bus/pci/devices/` 实际拓扑核对。

---

## 6. 本讲小结

- **拓扑矩阵**把每块 NIC 按内存位置分成 `preferred_hca`（同 NUMA / PCIe 最近）和 `avail_hca`（次优兜底），是「拓扑感知路径选择」的数据基础。
- `Topology` 类三段式生命周期：`discover()/parse()` 填充人类可读的 `matrix_` → `resolve()` 编号成整数下标的 `resolved_matrix_` → `selectDevice()` 在热路径 O(1) 选路。
- **硬件发现**走三条路：`libibverbs` 拿 NIC（过三层可用性体检 + `MC_TE_FILTERS` 白名单）、`/sys/devices/system/node` 拿 NUMA、CUDA Runtime 拿 GPU 并用 `getPciDistance` 算 PCIe 距离。
- `selectDevice` 遵循**首选优先、重试回退**：首次随机/轮转选 preferred，`retry_count>0` 时按 `% total` 把 preferred+avail 全纳入，保证故障时试遍所有 NIC；`hint` 重载允许强制指定 NIC。
- `MC_PATH_ROUNDROBIN` 把首次选择从随机改成线程内轮转，适合大块传输做多 NIC 带宽聚合。
- `generate_cluster_topology.py` 是**跨机实测**工具：SSH + `ib_write_bw`/`ib_read_lat` 测每对 NIC 的带宽延迟，按 NUMA 对分组后用匈牙利算法求最优配对，产出集群级 `cluster-topology.json`，与单机自动发现互补。
- **PCI BDF 安全小写化**：GPU 的 PCI 总线号要先逐字节转小写才能和 NIC 比较（[topology.cpp:450](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/src/topology.cpp#L450)）。提交 `b92cf05f` 把裸 `tolower` 换成 `char_util::to_lower`，根除了「带符号 `char` 直传 `std::tolower`、字节 \(>0x7F\) 时是 UB」的潜伏隐患——此类 UB 在 glibc 上常「看似正常」，尤需用专门工具封堵。

---

## 7. 下一步学习建议

1. **`resolve()` 的编号化细节**：重读 [topology.cpp:655-692](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/src/topology.cpp#L655-L692)，弄清 `hca_list_`、`kWildcardLocation` 的 preferred 是怎么累积的，这是 `*` 兜底能工作的关键。
2. **选路在传输链路里的位置**：进入 `RdmaTransport`——读 [worker_pool.cpp](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/src/transport/rdma_transport/worker_pool.cpp) 看 `selectDevice` 返回的下标如何被 `context_list_[device_id]` 使用、失败如何递增 `retry_count`。这将对应「RdmaTransport / Worker」相关讲义。
3. **分段内存编码**：阅读 [memory_location.h:44-72](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/include/memory_location.h#L44-L72) 的 `segments:` 格式，理解一段交错分布在多 NUMA 的 buffer 如何被解析到具体 `cpu:N`，再被 `selectDevice` 用上。
4. **自定义拓扑**：看 [transfer_engine_impl.cpp:236-253](https://github.com/kvcache-ai/Mooncake/blob/1f7f71a18a9dc48e9901d8293c5c3625ba166939/mooncake-transfer-engine/src/transfer_engine_impl.cpp#L236-L253) 里 `MC_CUSTOM_TOPO_JSON` 如何让 `parse()` 直接吃一份手写拓扑，跳过自动发现。
5. **举一反三查 ctype UB**：`char_util::to_lower` 修的是 `<ctype.h>` 家族的通用坑（`toupper`/`isdigit`/`isspace` 等同理要求实参为 `unsigned char` 或 `EOF`）。可以用 `grep` 在仓库里搜还有没有「直接把 `char` 喂给 ctype 函数」的写法，体会这类潜伏型 UB 为何要靠专门的小工具集中封堵。
