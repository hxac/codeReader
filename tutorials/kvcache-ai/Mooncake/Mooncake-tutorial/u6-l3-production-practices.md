# U6-L3: 生产环境最佳实践与案例

本讲义总结 Mooncake 在大规模生产环境中的部署最佳实践，涵盖集群规划、容量规划、故障排查、性能优化以及真实生产案例的经验总结。

## 最小模块 1：集群规划

### 概念说明

集群规划解决的是如何在物理层面组织 Mooncake 组件以获得最优性能和高可用性。Mooncake 采用 **Master-Client 架构**：Master 作为中央协调器管理集群成员、分配存储、执行驱逐策略；Client 节点贡献 DRAM/VRAM/SSD 资源形成分布式缓存池，数据直接在 Client 之间传输，Master 不在数据路径上。

**为什么需要集群规划**：
- **RDMA 网络拓扑**：RDMA 性能对网络拓扑敏感，错误的拓扑会导致带宽利用率低下
- **高可用性**：Master 单点故障会导致整个集群停止服务
- **资源均衡**：合理分配计算和存储资源可以最大化整体吞吐

### 伪代码或流程

集群规划的核心流程：

```
1. 规划网络拓扑
   输入：节点数量、GPU 数量、网络带宽
   输出：RDMA 网卡分配、Master 节点选择、Client 节点分组

2. 选择高可用方案
   if 运维复杂度 < 可靠性要求:
       使用 etcd HA 方案
   else if 运维复杂度 >= 可靠性要求:
       使用 P2P 握手（开发测试）
       或 Redis HA（简化部署）

3. 规划存储层次
   DRAM 用于热数据（KV Cache）
   SSD 用于冷数据（Offload）
   CXL 内存用于扩展容量（如可用）

4. 部署 Master 集群
   for each master_node:
       启动 mooncake_master
       配置 etcd_endpoints 或启用 P2P
       设置 rpc_thread_num 根据 CPU 核数

5. 启动 Client 节点
   for each client_node:
       配置 local_hostname（非回环地址）
       选择 metadata_server（P2P 或 HTTP/etcd）
       设置 global_segment_size（贡献内存量）
       设置 protocol（rdma 或 tcp）
```

### 原理分析

#### 1. 网络拓扑设计

Mooncake Transfer Engine 支持**拓扑感知路径选择**，根据源和目标的 NUMA 亲和性选择最优 RDMA 设备。合理的网络拓扑需要满足：

- **带宽匹配**：Prefill-Decode 分离场景下，Prefill 节点到 Decode 节点的 KV Cache 传输带宽应匹配 GPU 生成速度
- **低延迟**：Master 到 Client 的 RPC 延迟应低于 10ms（影响控制面响应速度）
- **多路径**：多 NIC 环境下应启用带宽聚合（使用 `MC_MS_FILTERS` 指定设备白名单）

#### 2. Master 高可用架构

Master 高可用通过 **etcd 分布式锁**实现：

- Leader 选举：etcd 保证同一时刻只有一个 Master 成为 Leader
- 元数据持久化：集群状态定期保存到 etcd
- 故障切换：Leader 故障后，剩余 Master 实例自动选举新 Leader

切换时间为 \[T_{elect} + T_{restore}\]，其中 \(T_{elect}\) 是 etcd 选举时间（通常 < 5s），\(T_{restore}\) 是从快照恢复状态的时间。

#### 3. 元数据服务选择

| 方案 | 适用场景 | 优点 | 缺点 |
|------|---------|------|------|
| P2P 握手 | 开发、单集群测试 | 无额外组件，最简单 | 不支持多集群，无中心化元数据 |
| 嵌入式 HTTP | 小规模生产 | 无需 etcd，Master 内置 | Master 承担元数据负载 |
| etcd | 大规模生产 HA | 成熟方案，强一致性 | 需要额外运维 etcd 集群 |
| Redis | 中等规模生产 | 部署简单 | 一致性保证弱于 etcd |

### 代码实践

#### 1. 启动高可用 Master 集群（etcd 模式）

```bash
# 节点 1
mooncake_master \
  --enable-ha=true \
  --etcd-endpoints="10.0.0.1:2379;10.0.0.2:2379;10.0.0.3:2379" \
  --rpc-address=10.0.0.1 \
  --rpc_port=50051

# 节点 2
mooncake_master \
  --enable-ha=true \
  --etcd-endpoints="10.0.0.1:2379;10.0.0.2:2379;10.0.0.3:2379" \
  --rpc-address=10.0.0.2 \
  --rpc_port=50051
```

源码位置：https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/docs/source/deployment/mooncake-store-deployment-guide.md#L247-L256

这段代码展示了如何使用 etcd 作为 HA 后端启动 Master 集群，每个实例必须指定其自己的可达 `--rpc-address`。

#### 2. 使用 P2P 握手（最简单）

```python
from distributed_object_store import DistributedObjectStore

store = DistributedObjectStore()
store.setup(
    local_hostname="192.168.1.10",  # 非 127.0.0.1
    metadata_server="P2PHANDSHAKE",  # 无需元数据服务
    global_segment_size=3200 * 1024 * 1024,
    local_buffer_size=512 * 1024 * 1024,
    protocol="rdma",
    device_name="mlx5_0",
    master_server_address="192.168.1.100:50051",
)
```

源码位置：https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/docs/source/deployment/mooncake-store-deployment-guide.md#L94-L104

这段代码展示了使用 P2P 握手的客户端配置，无需部署独立的元数据服务。

#### 3. 拓扑发现配置

```bash
# 启用 RDMA 设备自动发现
export MC_MS_AUTO_DISC=1

# 指定使用的 RDMA 网卡（白名单）
export MC_MS_FILTERS="mlx5_1,mlx5_2"

# 启动客户端
ROLE=prefill MC_MS_AUTO_DISC=1 MC_MS_FILTERS="mlx5_1,mlx5_2" \
  python3 stress_cluster_benchmark.py
```

源码位置：https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/docs/source/deployment/mooncake-store-deployment-guide.md#L147-L152

这段代码展示了如何通过环境变量配置 RDMA 拓扑自动发现和设备过滤。

### 练习题

1. **网络规划题**：你有 8 个 GPU 节点，每个节点有 2 张 200 Gbps RDMA 网卡，计划部署 Prefill-Decode 分离架构。如何分配网卡和选择 Master 节点？

2. **HA 选择题**：一个中等规模的生产集群（100+ 节点）需要 HA 支持，但运维团队熟悉 Redis 而非 etcd。应该选择哪种元数据方案？为什么？

3. **拓扑配置题**：某节点有 4 张 RDMA 网卡（mlx5_0 到 mlx5_3），其中 mlx5_0 和 mlx5_1 用于管理网络，mlx5_2 和 mlx5_3 用于数据传输。如何配置环境变量确保只使用数据网卡？

4. **P2P vs etcd 对比题**：在什么场景下应该选择 P2P 握手而不是 etcd？列出至少 3 个场景并解释原因。

### 答案

1. **网络规划题答案**：
   - 选择 2 个专用节点运行 Master（不运行 Client），保证 Master 资源独立
   - 其余 6 个节点作为 Client，Prefill 和 Decode 各 3 个
   - 每张网卡 200 Gbps，双网卡聚合后每节点 400 Gbps 带宽
   - 确保 Prefill 和 Decode 节点之间至少有一条直连路径或低延迟路由

2. **HA 选择题答案**：
   - 应该选择 **Redis HA 方案**
   - 原因：
     - Redis 是成熟的生产级方案，部署简单
     - 对于中等规模集群，Redis 的一致性保证足够
     - 运维团队熟悉 Redis，可降低运维成本
     - 性能 overhead 通常可接受（< 5%）

3. **拓扑配置题答案**：
   ```bash
   export MC_MS_AUTO_DISC=1
   export MC_MS_FILTERS="mlx5_2,mlx5_3"
   ```
   只在白名单中包含数据传输网卡，自动发现会跳过管理网卡。

4. **P2P vs etcd 对比题答案**：
   - **开发测试环境**：P2P 无需额外组件，启动最快
   - **单集群小规模部署**（< 10 节点）：P2P 足够，避免 etcd 运维复杂度
   - **快速原型验证**：P2P 让你可以快速验证功能，后续再迁移到 etcd

---

## 最小模块 2：容量规划

### 概念说明

容量规划是确定集群应该分配多少资源以满足目标负载的过程。Mooncake 的容量规划涉及三个维度：

- **内存容量**（DRAM）：由 `global_segment_size` 控制，决定 KV Cache 可用空间
- **SSD 容量**（Offload）：由 `--quota_bytes` 控制，用于扩展存储层次
- **副本数**（Replication）：由 `replica_num` 控制，影响数据可靠性和读取带宽

**为什么需要容量规划**：
- 内存不足会导致 `NO_AVAILABLE_HANDLE` 错误，对象分配失败
- SSD 容量不足会触发频繁驱逐，降低缓存命中率
- 过度配置会浪费资源，降低成本效益

### 伪代码或流程

```
1. 估算 KV Cache 大小
   输入：模型参数量、序列长度、批次大小、并发请求数
   输出：单个请求的 KV Cache 大小、峰值内存需求

2. 计算所需 DRAM
   total_dram = peak_kv_cache_size * cache_hit_ratio / memory_efficiency
   其中：
     - cache_hit_ratio: 期望的缓存命中率（如 0.7）
     - memory_efficiency: 内存利用效率（通常 0.8-0.9，考虑碎片和元数据）

3. 规划 SSD 容量
   ssd_capacity = total_dram * offload_ratio / ssd_write_amplification
   其中：
     - offload_ratio: 预期需要 offload 的数据比例
     - ssd_write_amplification: SSD 写放大（通常 1.5-2.0）

4. 设置副本策略
   for hot_objects:
       replica_num = 2  # 双副本提高可用性和读取带宽
   for warm_objects:
       replica_num = 1  # 单副本节省空间
   for cold_objects:
       enable_offload = True  # 直接 offload 到 SSD

5. 配置驱逐参数
   eviction_high_watermark_ratio = 0.95
   eviction_ratio = 0.05
   在水位线 95% 时触发驱逐，每次驱逐 5% 的对象
```

### 原理分析

#### 1. KV Cache 大小估算

对于 Transformer 模型，单个 token 的 KV Cache 大小为：

\[
\text{kv\_size\_per\_token} = \frac{2 \times n_{\text{layers}} \times d_{\text{model}} \times n_{\text{heads}} \times d_{\text{head}} \times \text{sizeof}(float16)}{\text{bytes\_per\_token}}
\]

其中：
- \(n_{\text{layers}}\)：层数
- \(d_{\text{model}}\)：隐藏层维度
- \(n_{\text{heads}}\)：注意力头数
- \(d_{\text{head}}\)：每个头的维度
- 2 倍是因为分别计算 K 和 V

#### 2. 容量规划公式

假设：
- 模型：LLaMA3-70B（80 层，8192 维）
- 序列长度：128k tokens
- 精度：FP16

单个请求的 KV Cache 大小 ≈ **80 GB**

若峰值并发 100 个请求，总需求 = 8 TB。但实际部署中：
- 缓存命中率 70%（重复前缀）
- 内存效率 85%（碎片和元数据）
- **实际所需 DRAM = 8 TB × 0.7 / 0.85 ≈ 6.6 TB**

#### 3. SSD Offload 容量

SSD 容量规划考虑：
- **写入放大**：SSD 写放大系数 1.5-2.0（日志、元数据）
- **读取放热**：热点数据会回传到 DRAM（Promotion 机制）
- **驱逐策略**：LRU 或 LFU 影响实际空间利用率

推荐 SSD 容量 = DRAM 容量的 **3-5 倍**。

### 代码实践

#### 1. 配置 Master 的驱逐和容量参数

```bash
mooncake_master \
  --eviction_high_watermark_ratio=0.95 \
  --eviction_ratio=0.05 \
  --default_kv_lease_ttl=5000 \
  --default_kv_soft_pin_ttl=1800000 \
  --enable_offload=true \
  --offload_on_evict=true \
  --promotion_on_hit=true \
  --promotion_admission_threshold=2 \
  --root_fs_dir=/mnt/ssd_cache \
  --quota_bytes=10737418240000  # 10 TB SSD
```

源码位置：https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/docs/source/deployment/mooncake-store-deployment-guide.md#L299-L308

这段代码展示了如何配置 Master 的驱逐策略和 SSD Offload 参数，控制 DRAM 和 SSD 的使用边界。

#### 2. 配置客户端的 Segment 大小

```python
# 根据 GPU 内存规划
# 假设 A100 80GB，分配 50% 给 KV Cache
import os

store = DistributedObjectStore()
store.setup(
    local_hostname=os.getenv("LOCAL_HOSTNAME"),
    metadata_server="P2PHANDSHAKE",
    global_segment_size=40 * 1024 * 1024 * 1024,  # 40 GB
    local_buffer_size=4 * 1024 * 1024 * 1024,     # 4 GB 传输缓冲
    protocol="rdma",
    device_name="mlx5_0",
    master_server_address="master:50051",
)
```

源码位置：https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/docs/source/deployment/mooncake-store-deployment-guide.md#L76-L84

这段代码展示了如何根据 GPU 内存容量规划 `global_segment_size`。

#### 3. 使用副本配置

```python
from distributed_object_store import DistributedObjectStore
from mooncake.store import ReplicateConfig

store = DistributedObjectStore()
store.setup(...)

# 配置热数据双副本
config = ReplicateConfig()
config.replica_num = 2
config.with_soft_pin = True  # 软钉住，延长存活时间
config.preferred_segment = "192.168.1.10:50052"  # 优先放置

# 存储重要 KV Cache
store.put("important_kv_cache", kv_data, config)
```

源码位置：https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/docs/source/deployment/mooncake-store-deployment-guide.md#L110-L117

这段代码展示了如何使用 `ReplicateConfig` 控制对象的副本策略。

### 练习题

1. **容量估算题**：计算 LLaMA3-70B 模型在 64k 序列长度下的单请求 KV Cache 大小（80 层，8192 维，32 头，256 维每头，FP16）。

2. **集群规划题**：一个 16 节点集群，每个节点 8×A100 80GB，分配 40% 内存给 KV Cache，总可用 DRAM 是多少？

3. **SSD 容量题**：如果规划 10 TB DRAM 用于 KV Cache，按 4 倍比例需要多少 SSD 容量？考虑 1.8 倍写放大。

4. **驱逐配置题**：某集群的 `eviction_high_watermark_ratio=0.95`，`eviction_ratio=0.1`。当前使用率 96%，触发驱逐后会降到多少？

### 答案

1. **容量估算题答案**：
   - 单层 K/V Cache = 80 层 × 8192 维 × 32 头 × 256 维/头 × 2 bytes（FP16）= **1 GB per token**
   - 64k tokens = **64 TB**（这是估算值，实际会因压缩、共享等因素减小）

2. **集群规划题答案**：
   - 单节点 A100 8 卡 = 8 × 80 GB = 640 GB
   - 40% 给 KV Cache = 640 GB × 0.4 = 256 GB per node
   - 16 节点总容量 = 256 GB × 16 = **4.1 TB DRAM**

3. **SSD 容量题答案**：
   - 理论 SSD = 10 TB × 4 = 40 TB
   - 考虑写放大 = 40 TB × 1.8 = **72 TB SSD**

4. **驱逐配置题答案**：
   - 初始使用率 96%
   - 驱逐比例 10% 的总容量（非 10% 的当前使用）
   - 假设总容量为 C，已用 0.96C
   - 驱逐 0.1C 后，使用率 = (0.96C - 0.1C) / C = **86%**

---

## 最小模块 3：故障排查

### 概念说明

故障排查是定位和解决生产环境中常见问题的技能。Mooncake 故障可分为以下几类：

- **服务启动失败**：Master/Client 无法启动
- **连接问题**：节点间无法建立 RDMA/TCP 连接
- **内存问题**：RDMA 内存注册失败、ulimit 不足
- **运行时错误**：传输失败、对象不存在、租约过期

**为什么需要系统化的故障排查**：
- 分布式系统的错误日志可能级联，首个错误才是根因
- RDMA 错误信息晦涩，需要特定的诊断工具
- 错误码（如 `NO_AVAILABLE_HANDLE`）需要正确的理解

### 伪代码或流程

```
1. 服务健康检查
   function check_service_health():
       if master_not_running:
           检查端口占用、日志错误
       if metadata_unreachable:
           检查 etcd/HTTP 服务状态、网络连通性
       if client_registration_failed:
           检查 connectable_name 是否为回环地址

2. RDMA 网络诊断
   function diagnose_rdma():
       检查 RDMA 设备状态 (ibv_devinfo)
       检查 GID 地址（是否全 0）
       检查端口状态（是否 ACTIVE）
       测试节点间连通性 (ib_write_bw)

   常见错误映射：
   - "No matched device found" → 设备名不存在
   - "Device port not active" → 端口未激活
   - "Failed to exchange handshake" → MTU/GID 不匹配

3. 内存资源检查
   function check_memory_resources():
       检查 ulimit -l（max locked memory）
       检查 RDMA max_mr_size（设备内存注册上限）
       检查 vm.max_map_count（内存映射限制）

   常见错误映射：
   - "Failed to register memory: Input/output error" → 超过 max_mr_size
   - "Failed to create QP: Cannot allocate memory" → QP 资源耗尽

4. 日志分析
   function analyze_logs():
       定位首个错误（后续错误可能级联）
       搜索特定错误模式：
       - "Error from etcd client"
       - "NO_AVAILABLE_HANDLE"
       - "LEASE_EXPIRED"
       - "Worker: Process failed for slice"
```

### 原理分析

#### 1. 错误码体系

Mooncake 使用**负整数错误码**：

**Transfer Engine 错误码**（`mooncake-transfer-engine/include/error.h`）：
- `0`：成功
- `-12` (`ERR_ADDRESS_NOT_REGISTERED`)：内存未注册
- `-14` (`ERR_DEVICE_NOT_FOUND`)：RDMA 设备不存在
- `-20` (`ERR_METADATA`)：元数据服务器不可达

**Store 错误码**（`mooncake-store/include/types.h`）：
- `-200` (`NO_AVAILABLE_HANDLE`)：内存池耗尽
- `-707` (`LEASE_EXPIRED`)：租约在传输完成前过期
- `-704` (`OBJECT_NOT_FOUND`)：对象不存在
- `-1000` (`ETCD_OPERATION_ERROR`)：etcd 操作失败

#### 2. RDMA 错误的级联效应

RDMA 驱动在首次错误后将连接设为**不可用状态**，导致：
- 提交队列中的任务全部失败，报告 `work request flushed error`
- 日志中出现大量重复错误

**诊断原则**：**定位首个错误**，忽略后续级联错误。

#### 3. 内存注册限制

RDMA 内存注册受以下限制：
- **设备限制**：`max_mr_size`（某些设备上限 64GB）
- **ulimit 限制**：`max locked memory`（默认可能很小）
- **内核限制**：`vm.max_map_count`（默认 65530 可能不足）

### 代码实践

#### 1. 诊断脚本（关键检查点）

```bash
#!/bin/bash
# Mooncake 部署快速诊断

echo "=== 1. 服务状态检查 ==="
ps aux | grep mooncake_master | grep -v grep
netstat -tuln | grep -E '(50051|8080|2379|9003)'

echo -e "\n=== 2. RDMA 设备检查 ==="
ibv_devices
ibv_devinfo | grep -A 5 "state:"
ibv_devinfo | grep -A 20 "GID:"

echo -e "\n=== 3. 内存限制检查 ==="
ulimit -a | grep "max locked memory"
sysctl vm.max_map_count

echo -e "\n=== 4. 网络连通性测试 ==="
# 在对端节点运行 ib_write_bw -d mlx5_0 -R
ib_write_bw -d mlx5_0 -R <peer_ip>

echo -e "\n=== 5. 环境变量检查 ==="
env | grep ^MC_
```

源码位置：https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/docs/source/troubleshooting/troubleshooting.md#L13-L38

这段代码展示了系统化的故障排查检查清单，涵盖了服务、RDMA、内存、网络和环境变量。

#### 2. 处理常见错误

**错误 1：`Error from etcd client`**
```bash
# 原因：etcd 绑定在 127.0.0.1，其他节点无法访问
# 修复：
etcd --listen-client-urls http://0.0.0.0:2379 \
     --advertise-client-urls http://<your_ip>:2379

# 或禁用代理
unset http_proxy https_proxy
```

源码位置：https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/docs/source/troubleshooting/troubleshooting.md#L19-L30

**错误 2：`No matched device found`**
```bash
# 原因：配置中的设备名不存在
# 诊断：
ibv_devices

# 修复：使用正确的设备名或更新配置
export MC_MS_FILTERS="mlx5_0,mlx5_2"
```

源码位置：https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/docs/source/troubleshooting/troubleshooting.md#L47-L48

**错误 3：`Failed to register memory: Input/output error`**
```bash
# 原因：内存注册超过设备 max_mr_size
# 诊断：
ibv_devinfo -v | grep max_mr_size
dmesg | grep "out of mr size"

# 修复：减小单次分配大小或分块注册
ulimit -l unlimited
```

源码位置：https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/docs/source/troubleshooting/troubleshooting.md#L53-L65

#### 3. 日志分析示例

```bash
# 搜索首个错误
grep -i "error\|failed" mooncake_master.INFO | head -20

# 分析特定错误模式
grep "NO_AVAILABLE_HANDLE" mooncake_master.INFO
grep "LEASE_EXPIRED" mooncake_master.INFO
grep "Failed to exchange handshake" mooncake_master.INFO

# 检查 RDMA 错误
grep "Worker: Process failed for slice" transfer_engine.log
grep "work request flushed error" transfer_engine.log | head -5
```

### 练习题

1. **错误诊断题**：日志中出现 `Failed to modify QP to RTR, check mtu, gid, peer lid, peer qp num`，如何排查？

2. **内存错误题**：应用崩溃重启后，出现 `Failed to register memory: Resource temporarily unavailable`，`dmesg` 显示 `CREATE_MKEY failed, status no resources(0xf)`。根因是什么？如何修复？

3. **连接问题题**：新加入的 Client 无法注册到 Master，日志显示 `connection refused`。检查清单是什么？

4. **租约问题题**：大规模传输时频繁出现 `LEASE_EXPIRED` 错误。应该如何配置？

### 答案

1. **错误诊断题答案**：
   - **检查 MTU 配置**：确保两端 MTU 一致，使用 `MC_MTU` 环境变量
   - **检查 GID**：用 `ibv_devinfo | grep GID` 查看是否全 0，设置 `MC_GID_INDEX`
   - **检查物理连接**：用 `ib_write_bw` 测试节点间连通性
   - **检查端口状态**：确认 RDMA 端口为 ACTIVE，非 `PORT_DOWN`

2. **内存错误题答案**：
   - **根因**：应用崩溃未释放 RDMA 资源，MKEY 泄漏导致 NIC 固件资源耗尽
   - **修复**：
     - **重启节点**（最可靠）：重置 NIC 固件，回收所有泄漏资源
     - **永久修复**：增加 `vm.max_map_count=16777216`
     - **预防**：避免 `kill -9`，让应用正常退出以释放资源

3. **连接问题题答案**：
   - 检查 `connectable_name` 是否为非回环地址（非 `127.0.0.1`/`localhost`）
   - 检查 `rpc_port` 是否正确且未被占用
   - 用 `telnet <master_host> <rpc_port>` 测试连通性
   - 检查防火墙规则，确保端口已加入白名单
   - 清空 etcd 数据库并重启集群（若元数据损坏）

4. **租约问题题答案**：
   - **原因**：单个对象传输时间超过租约 TTL（默认 5000ms）
   - **修复**：
     - 增加 Master 的 `--default_kv_lease_ttl`（如 `30000` 表示 30 秒）
     - 减小单个对象大小，分批传输
     - 检查网络是否存在拥塞或间歇性中断

---

## 最小模块 4：性能优化

### 概念说明

性能优化是通过配置和调优让 Mooncake 在给定硬件上达到最优吞吐和延迟。关键优化维度包括：

- **RDMA 配置**：MTU、GID、多网卡带宽聚合
- **内存管理**：HugePages、Zero-Copy、本地热缓存
- **传输策略**：连接池、拓扑感知路由、故障快速切换
- **存储分层**：DRAM-SSD 分层、数据回传、驱逐策略

**为什么需要性能优化**：
- 默认配置可能未充分利用硬件能力（如未启用多网卡聚合）
- 未优化的内存路径会引入不必要的拷贝（降低吞吐）
- 不合理的驱逐策略会降低缓存命中率

### 伪代码或流程

```
1. RDMA 性能调优
   输入：网络拓扑、网卡数量、MTU 大小
   流程：
     - 自动发现拓扑（MC_MS_AUTO_DISC=1）
     - 配置 MTU（MC_MTU=2044 或 4200）
     - 设置 GID 索引（MC_GID_INDEX=1）
     - 启用多网卡聚合（MC_MS_FILTERS="mlx5_0,mlx5_1"）

2. 内存路径优化
   启用 HugePages：
     if use_mmap_arena:
         设置 MC_MMAP_ARENA_POOL_SIZE
         配置 vm.nr_hugepages
     else:
         使用 MC_STORE_USE_HUGEPAGE=1

   启用 Zero-Copy：
     for buffer in buffers:
         register_buffer(buffer_ptr, size)
         使用 put_from/get_into 而非 put/get

3. 传输引擎调优
   if 使用 TCP:
       启用连接池（MC_TCP_ENABLE_CONNECTION_POOL=1）
       增大端口范围（net.ipv4.ip_local_port_range）

   if 使用 RDMA:
       启用目标设备亲和（MC_ENABLE_DEST_DEVICE_AFFINITY=1）
       减少不必要的 QP 创建

4. 存储分层优化
   配置 Offload：
     - offload_on_evict=True（延迟写入）
     - promotion_on_hit=True（热数据回传）
     - promotion_admission_threshold=2（访问 2 次后回传）

   配置本地热缓存：
     MC_STORE_LOCAL_HOT_CACHE_SIZE="8gb"
     MC_STORE_LOCAL_HOT_BLOCK_SIZE="2mb"
```

### 原理分析

#### 1. RDMA 多网卡带宽聚合

Transfer Engine 支持在多个 RDMA 网卡间**Striping 传输**：

\[
\text{总带宽} = \sum_{i=1}^{n} \text{带宽}_i
\]

实测数据：
- 4×200 Gbps RoCE：**87 GB/s**（2.4× 于 TCP）
- 8×400 Gbps RoCE：**190 GB/s**（4.6× 于 TCP）

**条件**：
- 网卡在同一 NUMA 节点（避免跨 NUMA 拷贝）
- 拓扑感知路由选择最优设备
- 源和目标节点网卡配置一致

#### 2. HugePages 与 TLB 缺失

HugePages（2MB 或 1GB）通过**减少页表项**降低 TLB 缺失：

- 4KB 页：1GB 内存需要 262,144 个页表项
- 2MB HugePage：1GB 内存只需 512 个页表项

TLB 缺失降低约 **50-100×**，在大量随机访问时尤其明显。

#### 3. Zero-Copy 原理

传统拷贝路径：
```
GPU → 应用缓冲区 → 内核缓冲区 → RDMA 网卡
```

Zero-Copy 路径：
```
GPU → RDMA 网卡（直接 DMA）
```

消除中间拷贝，吞吐提升 **2-3×**，延迟降低 **30-50%**。

### 代码实践

#### 1. 配置 HugePages

```bash
# 分配 2MB HugePages（96GB = 49152 页）
sudo sysctl -w vm.nr_hugepages=49152

# 验证
grep -E 'HugePages_Total|HugePages_Free|Hugepagesize' /proc/meminfo

# 持久化配置
printf 'vm.nr_hugepages=49152\n' | sudo tee /etc/sysctl.d/90-mooncake-hugepages.conf
sudo sysctl --system
```

源码位置：https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/docs/source/troubleshooting/troubleshooting.md#L184-L192

这段代码展示了如何配置 HugeTLB pool 以支持大页内存分配。

#### 2. 启用 Zero-Copy 批量传输

```python
import numpy as np
from distributed_object_store import DistributedObjectStore

store = DistributedObjectStore()
store.setup(...)

# 准备缓冲区
buffers = [np.random.randn(1024*1024).astype(np.float32) for _ in range(10)]
buffer_ptrs = [buf.ctypes.data for buf in buffers]
sizes = [buf.nbytes for buf in buffers]

# 注册缓冲区（Zero-Copy 必需）
for ptr, size in zip(buffer_ptrs, sizes):
    store.register_buffer(ptr, size)

# 批量 Zero-Copy Put
keys = [f"tensor_{i}" for i in range(10)]
results = store.batch_put_from(keys, buffer_ptrs, sizes)

# 批量 Zero-Copy Get
recv_buffers = [np.empty(1024*1024, dtype=np.float32) for _ in range(10)]
recv_ptrs = [buf.ctypes.data for buf in recv_buffers]

for ptr, size in zip(recv_ptrs, sizes):
    store.register_buffer(ptr, size)

results = store.batch_get_into(keys, recv_ptrs, sizes)

# 清理
for ptr in buffer_ptrs + recv_ptrs:
    store.unregister_buffer(ptr)
```

源码位置：https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/docs/source/deployment/mooncake-store-deployment-guide.md#L573-L582

这段代码展示了如何注册缓冲区并使用 `batch_put_from`/`batch_get_into` 实现 Zero-Copy 批量传输。

#### 3. 启用本地热缓存（Hot Cache）

```bash
# 配置本地热缓存（8GB 容量，2MB 块大小）
export MC_STORE_LOCAL_HOT_CACHE_SIZE="8gb"
export MC_STORE_LOCAL_HOT_BLOCK_SIZE="2mb"

# 可选：使用共享内存支持多进程
export MC_STORE_LOCAL_HOT_CACHE_USE_SHM=1

# 配置回传阈值（访问 3 次后回传）
export MC_STORE_LOCAL_HOT_ADMISSION_THRESHOLD=3

# 启动客户端
python -m mooncake.mooncake_store_service \
  --local_hostname=192.168.1.10 \
  --metadata_server=P2PHANDSHAKE \
  --master_server=192.168.1.100:50051
```

源码位置：https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/docs/source/deployment/mooncake-store-deployment-guide.md#L577-L583

这段代码展示了如何配置本地热缓存以加速 SSD 数据的读取。

#### 4. TCP 连接池优化

```bash
# 启用 TCP 连接池（复用连接，避免 TIME_WAIT 积累）
export MC_TCP_ENABLE_CONNECTION_POOL=1

# 若仍遇到端口耗尽，增大临时端口范围
sudo sysctl -w net.ipv4.ip_local_port_range="1024 65535"

# 可选：启用 TIME_WAIT 复用（慎用，仅用于 outbound）
sudo sysctl -w net.ipv4.tcp_tw_reuse=1
```

源码位置：https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/docs/source/troubleshooting/troubleshooting.md#L141-L153

这段代码展示了如何优化 TCP 传输以应对高并发短连接场景。

### 练习题

1. **多网卡调优题**：某节点有 4 张 mlx5 网卡（mlx5_0 到 mlx5_3），但实测带宽仅为单网卡水平。如何配置才能实现多网卡聚合？

2. **HugePages 计算题**：需要为 512 GB 的 Mooncake Segment 配置 HugePages，需要多少个 2MB 页？如何持久化配置？

3. **Zero-Copy 场景题**：在什么场景下 Zero-Copy 的优势最明显？给出至少两个场景并解释。

4. **TCP vs RDMA 选择题**：在延迟敏感的在线服务中，TCP 和 RDMA 的优缺点各是什么？如何选择？

### 答案

1. **多网卡调优题答案**：
   ```bash
   # 启用自动发现
   export MC_MS_AUTO_DISC=1

   # 设置设备白名单（假设 mlx5_2 和 mlx5_3 是数据网卡）
   export MC_MS_FILTERS="mlx5_2,mlx5_3"

   # 确保网卡在同一 NUMA 节点（用 lstopo-no-graph 检查）
   ```

2. **HugePages 计算题答案**：
   - 512 GB = 524,288 MB
   - 需要 524,288 / 2 = **262,144 个 2MB 页**
   - 持久化：
     ```bash
     printf 'vm.nr_hugepages=262144\n' | sudo tee /etc/sysctl.d/90-mooncake-hugepages.conf
     sudo sysctl --system
     ```

3. **Zero-Copy 场景题答案**：
   - **大对象传输**：如 KV Cache（几十到几百 GB），消除拷贝节省显著
   - **高频小对象**：如频繁的 hidden states 交换，累积拷贝开销大
   - **GPU 直通**：数据在 GPU 和 RDMA 网卡间直接传输，避免 CPU 参与

4. **TCP vs RDMA 选择题答案**：
   - **RDMA 优点**：低延迟（< 10μs）、高吞吐（190 GB/s）、零拷贝、CPU 占用低
   - **RDMA 缺点**：需要专门硬件、配置复杂、故障排查困难
   - **TCP 优点**：通用性强、部署简单、兼容性好
   - **TCP 缺点**：高延迟（50-100μs）、低吞吐（40 GB/s）、CPU 占用高
   - **选择**：生产环境优先 RDMA（若硬件允许），开发/测试可用 TCP

---

## 最小模块 5：生产案例

### 概念说明

生产案例是从真实大规模部署中提炼的经验和教训。Mooncake 已在多个生产系统中应用，包括：

- **Kimi K2**：Moonshot AI 的 1T 参数 MoE 模型，使用 Mooncake 实现大规模 EP（Expert Parallelism）
- **TorchSpec**：PyTorch 生态系统项目，使用 Mooncake 解耦推理和训练
- **SGLang HiCache**：多层级 KV 缓存系统，使用 Mooncake Store 作为远程存储后端

**为什么学习生产案例**：
- 真实场景暴露文档未覆盖的问题
- 大规模部署的调优经验具有普适性
- 故障恢复策略比理论更重要

### 伪代码或流程

```
1. Kimi K2 部署架构（128×H200 GPU）
   规模：128 节点，8 GPU/节点
   模型：1T 参数 MoE，32 专家
   架构：PD-disaggregation + Expert Parallelism

   流程：
     - Prefill 集群（48 节点）：处理新请求，生成 KV Cache
     - Decode 集群（64 节点）：消费 KV Cache，生成 token
     - Storage 集群（16 节点）：Mooncake Store，提供 KV Cache 池化
     - 使用 Mooncake Transfer Engine 实现 Prefill→Decode 传输
     - 使用 Mooncake EP 实现 Expert Parallelism

2. TorchSpec 训练-推理解耦
   架构：Mooncake Store 作为 hidden states 缓存
   流程：
     - 训练端：将中间 hidden states 存入 Mooncake
     - 推理端：从 Mooncake 读取 states，避免重复计算
     - 使用 Mooncake PG（Process Group）实现分布式通信

3. SGLang HiCache 多级缓存
   层次：
     L1: GPU 内存（最快，容量最小）
     L2: 主机内存（中等）
     L3: Mooncake Store（远程，容量最大）

   流程：
     - 新 KV Cache → GPU → 主机 → Mooncake Store
     - Miss 时逐层回传：Store → 主机 → GPU
```

### 原理分析

#### 1. Kimi K2 的 EP（Expert Parallelism）

K2 使用 **Mooncake EP** 实现 MoE 推理：

- **Dispatch 阶段**：Token 分发给 32 个专家，分布在多 GPU
- **Combine 阶段**：收集专家输出，聚合为最终结果
- **故障容错**：使用 `active_ranks` 标记健康专家，绕过故障节点

性能数据：
- **Prefill 吞吐**：224k tokens/sec（128 H200）
- **Decode 吞吐**：288k tokens/sec
- **KV Cache 传输**：Prefill→Decode 延迟 < 50ms（RDMA）

#### 2. TorchSpec 的训练-推理解耦

TorchSpec 使用 Mooncake 实现 **跨阶段数据共享**：

- 训练阶段保存 intermediate activations
- 推理阶段复用 activations，避免重复前向计算
- Mooncake PG 提供分布式一致性保证

#### 3. HiCache 的多级缓存策略

HiCache 使用 **RadixAttention + Mooncake Store**：

- **RadixTree 索引**：基于 token hash 的前缀共享
- **多级回传**：Miss 时自动从 Store 拉取数据
- **本地热缓存**：在 Store 客户端加速 SSD 数据读取

### 代码实践

#### 1. Kimi K2 风格的 EP 配置

```python
import torch
import torch.distributed as dist
from mooncake import pg
from mooncake.mooncake_ep_buffer import Buffer

# 初始化故障容错 PG
active_ranks = torch.ones((world_size,), dtype=torch.int32, device="cuda")
dist.init_process_group(
    backend="mooncake",
    rank=rank,
    world_size=world_size,
    pg_options=pg.MooncakeBackendOptions(active_ranks),
)

# 计算 EP Buffer 大小
num_ep_buffer_bytes = Buffer.get_ep_buffer_size_hint(
    num_max_dispatch_tokens_per_rank=1024,
    hidden=4096,
    num_ranks=8,
    num_experts=64
)

# 创建 Buffer
buffer = Buffer(
    group=dist.group.WORLD,
    num_ep_buffer_bytes=num_ep_buffer_bytes
)

# Dispatch（分发给专家）
buffer.dispatch(
    tokens=tokens,
    topk_idx=topk_idx,
    topk_weights=topk_weights,
    active_ranks=active_ranks,
    timeout_us=1000000  # 1 秒超时
)

# Combine（聚合输出）
buffer.combine(
    outputs=outputs,
    active_ranks=active_ranks,
    timeout_us=1000000
)
```

源码位置：https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/docs/source/troubleshooting/pg-ep-troubleshooting.md#L65-L71

这段代码展示了如何在 Mooncake PG 中使用故障容错的 EP Buffer。

#### 2. SGLang HiCache 集成

```python
# SGLang HiCache 配置（示意）
from sglang.srt.hicache import HiCacheConfig

config = HiCacheConfig(
    # L3 层：Mooncake Store
    remote_backend="mooncake",
    mooncake_store_config={
        "local_hostname": "192.168.1.10",
        "metadata_server": "P2PHANDSHAKE",
        "global_segment_size": 40 * 1024 * 1024 * 1024,  # 40 GB
        "protocol": "rdma",
        "master_server_address": "192.168.1.100:50051",
    },

    # L2 层：主机内存
    host_cache_size=20 * 1024 * 1024 * 1024,  # 20 GB

    # L1 层：GPU 内存
    gpu_cache_size=10 * 1024 * 1024 * 1024,  # 10 GB
)

# 启动 HiCache
hicache = HiCache(config)
```

源码位置：https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/README.md#L53-L54

这段代码展示了 SGLang 如何将 Mooncake Store 集成为 HiCache 的远程存储后端。

#### 3. vLLM PD-Disaggregation 配置

```bash
# vLLM Prefill Worker
vllm serve <model> \
  --role prefill \
  --distributed-executor-backend mp \
  --kv-connector mooncake \
  --kv-transfer-config mooncake_transfer_config.json

# mooncake_transfer_config.json
{
  "metadata_server": "P2PHANDSHAKE",
  "protocol": "rdma",
  "device_name": "mlx5_0",
  "local_buffer_size": 1073741824
}
```

源码位置：https://github.com/kvcache-ai/Mooncake/blob/8b884bcd131009f5b02a981abcb83cfdb5e21736/docs/source/getting_started/examples/vllm-integration/vllm-integration-v1.0.md

这段代码展示了 vLLM 如何使用 Mooncake Connector 实现 Prefill-Decode 分离。

### 练习题

1. **EP 超时配置题**：K2 部署中，某专家节点响应慢导致频繁超时。应该如何调整 `timeout_us` 参数？

2. **多级缓存设计题**：设计一个 3 层缓存系统（GPU → 主机 → 远程 Store），每层容量分别为 10GB、50GB、500GB。如何配置驱逐策略使得整体命中率最优？

3. **PD 分离容量题**：一个 PD 分离系统，Prefill 集群 32 节点，Decode 集群 96 节点。如何分配 KV Cache 存储节点？

4. **故障恢复题**：EP 系统中某专家节点崩溃，`active_ranks` 如何更新？其余节点如何继续服务？

### 答案

1. **EP 超时配置题答案**：
   - **诊断**：首先确定是网络慢还是计算慢（检查 GPU 利用率）
   - **调整**：
     - 若是网络抖动，增大 `timeout_us`（如从 1ms 增到 5ms）
     - 若是计算慢，考虑增加专家并行度或升级硬件
   - **权衡**：超时过大会延迟故障检测，过小会误判健康节点

2. **多级缓存设计题答案**：
   - **策略**：
     - L1（GPU）：LRU，驱逐到 L2
     - L2（主机）：LFU（访问频率），驱逐到 L3
     - L3（Store）：LRU，offload 到 SSD
   - **理由**：
     - GPU 容量小，快速周转最重要
     - 主机容量中等，频率统计更能识别热点
     - Store 容量大，简单的 LRU 足够

3. **PD 分离容量题答案**：
   - **原则**：Prefill 和 Decode 峰值不同时发生
   - **配置**：
     - 假设每个 Decode 节点需要 10 GB KV Cache
     - 总需求 = 96 × 10 GB = 960 GB
     - 存储节点 = 960 GB / 80 GB（每节点）≈ **12 节点**
   - **冗余**：考虑副本和故障，部署 **16 节点**

4. **故障恢复题答案**：
   - **更新 active_ranks**：
     ```python
     active_ranks[failed_rank] = 0
     ```
   - **绕过故障**：Dispatch/Combine 自动跳过 `active_ranks==0` 的专家
   - **恢复流程**：
     - 新节点启动，调用 `pg.join_group()`
     - 健康节点调用 `pg.recover_ranks(backend, [failed_rank])`
     - `active_ranks[failed_rank] = 1`
   - **保证**：Mooncake PG 确保恢复过程不中断服务

---

## 总结

本讲义涵盖了 Mooncake 在大规模生产环境中的五个关键方面：

1. **集群规划**：RDMA 网络拓扑、Master 高可用、元数据服务选择
2. **容量规划**：内存和 SSD 容量估算、副本策略、驱逐参数配置
3. **故障排查**：系统化诊断流程、常见错误码、RDMA 和内存问题
4. **性能优化**：RDMA 多网卡聚合、HugePages、Zero-Copy、本地热缓存
5. **生产案例**：Kimi K2 EP、TorchSpec 训练-推理解耦、SGLang HiCache

通过掌握这些实践，你可以在生产环境中成功部署、调优和维护 Mooncake 系统。
