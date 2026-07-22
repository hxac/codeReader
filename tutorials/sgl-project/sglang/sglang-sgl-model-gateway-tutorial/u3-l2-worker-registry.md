# WorkerRegistry 与 HashRing

> 承接：本讲是控制面「Worker 生命周期」单元的第二篇。上一篇（u3-l1）建立了 `Worker` trait、`WorkerType`、`BasicWorkerBuilder` 这些「单个 Worker」的表示。本讲回答下一个问题：**一群 Worker 由谁来管？路由器每次请求要如何在微秒级找到一个合适的 Worker？**

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `WorkerRegistry` 用了哪些并发数据结构、维护了哪几张索引表，以及为什么读路径要做到「无锁」。
- 写出注册 / 移除一个 Worker 时，registry 需要同步更新的所有内部结构。
- 解释一致性哈希环（`HashRing`）解决什么问题：为什么不是简单 `hash % N`，虚拟节点为什么是 150 个，blake3 为什么被选中。
- 读懂 `find_healthy_url` 的「二分定位 + 顺时针遍历 + 去重」查找算法。
- 理解 `get_hash_ring` 的预计算缓存、`set_mesh_sync` 的多实例状态同步，以及一致性哈希策略（`consistent_hashing` / `prefix_hash`）如何消费这个环。
- 自己编写一个测试，验证一致性哈希在 Worker 增减时的迁移比例接近 \(1/N\)。

## 2. 前置知识

在进入源码前，先用三段直觉建立认知。

### 2.1 为什么需要「注册表」而不是一个 `Vec<Worker>`

网关（sgl-model-gateway）每秒要处理成千上万个推理请求，每次请求都要从「能服务该模型的健康 Worker」里挑一个。如果每次都遍历全部 Worker 做过滤，代价太高。于是需要一个**按多个维度预先建好索引**的数据结构：

- 按 **模型** 查（`/v1/chat/completions` 带了 `model` 字段，要找能服务它的 Worker）；
- 按 **Worker 类型** 查（PD 分离部署时，要分别找 Prefill / Decode Worker）；
- 按 **连接模式** 查（HTTP 路由器只关心 HTTP Worker，gRPC 路由器只关心 gRPC Worker）；
- 按 **URL / ID** 查（健康检查、熔断器要定位到具体那个 Worker）。

这就是 `WorkerRegistry` 的职责：一个并发安全的、多维度索引的 Worker 容器。

### 2.2 一致性哈希要解决的痛点

最朴素的负载均衡是 `hash(key) % N`（N 是 Worker 数）。但它有个致命问题：**N 一变，几乎所有 key 都会被重新映射**。例如 N 从 3 变到 4，`hash(key) % N` 的结果对约 3/4 的 key 都会改变，导致 KV cache 全部失效、会话亲和全部断裂。

一致性哈希（consistent hashing）的思路是：把 Worker 和 key 都映射到同一个「环」上，key 顺时针走到的**第一个 Worker** 就是它的归属。这样增删一个 Worker 时，只有落在「新 Worker 占据的那段弧」上的 key 会迁移，理论上迁移比例约为：

\[
\Pr(\text{key 迁移}) \approx \frac{1}{N}
\]

其中 \(N\) 是变化后的 Worker 总数。这是 `HashRing` 的核心价值。

### 2.3 并发读 vs 并发写的不对称

网关的访问模式是**读极多、写极少**：每次请求都要查 registry（读），但 Worker 的增删（写）只在注册 / 下线 / 服务发现对账时发生。因此 registry 的设计哲学是：

> 读路径走「无锁的不可变快照」，写路径才付出「拷贝重建」的代价。

记住这个不对称，后面看到 `Arc<[Arc<dyn Worker>]>` 这种写法就不会觉得奇怪了。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 | 本讲角色 |
|------|------|----------|
| [src/core/worker_registry.rs](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/worker_registry.rs) | `WorkerRegistry`、`HashRing`、`WorkerId` 的全部实现 | **本讲唯一主角** |
| [src/core/worker.rs](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/worker.rs) | `Worker` trait、`WorkerType`、`ConnectionMode` | registry 索引的维度来源 |
| [src/policies/consistent_hashing.rs](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/policies/consistent_hashing.rs) | 一致性哈希负载均衡策略 | 环的**消费方**示例 |
| [src/routers/grpc/common/stages/worker_selection.rs](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/routers/grpc/common/stages/worker_selection.rs) | gRPC 路由的 Worker 选择阶段 | 取环并传给策略的调用点 |
| [src/server.rs](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/server.rs) | 启动编排 | `set_mesh_sync` 的注入点 |

`WorkerRegistry` 和 `HashRing` 都在 [src/core/mod.rs:42](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/mod.rs#L42) 处统一再导出，所以业务代码都通过 `crate::core::{WorkerRegistry, HashRing}` 来引用。

---

## 4. 核心概念与源码讲解

### 4.1 WorkerId：Worker 的稳定身份标识

#### 4.1.1 概念说明

一个 Worker 有 URL（如 `http://10.0.0.1:8000`），但 URL 不适合当唯一标识：

- DP（数据并行）Worker 的 URL 会被改写为 `http://10.0.0.1:8000@2` 这种带 rank 后缀的形式，同一个物理服务会对应多个 URL；
- Worker 可能被移除后重新注册，我们希望「同 URL 的 Worker 复用同一个 ID」；
- 外部系统（如 Mesh 状态同步）需要一个稳定、不暴露内部地址的标识。

`WorkerId` 是一个**newtype**，内部就是一个字符串（默认是 UUID v4），提供了生成、复用、保留（reserve）的能力。

#### 4.1.2 核心流程

- `WorkerId::new()`：调用 `Uuid::new_v4()` 生成一个全新的随机 UUID。
- `WorkerId::from_string(s)`：把任意字符串包成 ID（用于从外部配置恢复稳定 ID）。
- `reserve_id_for_url(url)`：注册表层面的「占位」操作——用 DashMap 的 `entry().or_default()` **原子地**「查不到就建一个、查到就返回现有的」，避免「先查再插」的竞态。

#### 4.1.3 源码精读

`WorkerId` 本身极简，是一个带 `Hash/Eq/Clone` 的 newtype：

```rust
// src/core/worker_registry.rs
#[derive(Debug, Clone, Hash, Eq, PartialEq)]
pub struct WorkerId(String);
```

> 这是 [worker_registry.rs:147-148](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/worker_registry.rs#L147-L148) 处的定义。派生了 `Hash + Eq`，所以它可以作为 `DashMap` 的 key。

`new()` 直接生成 UUID v4（[worker_registry.rs:150-154](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/worker_registry.rs#L150-L154)）：

```rust
pub fn new() -> Self {
    Self(Uuid::new_v4().to_string())
}
```

真正值得注意的是「按 URL 复用 ID」和「原子占位」两个机制。先看注册时的复用逻辑（[worker_registry.rs:242-248](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/worker_registry.rs#L242-L248)）：

```rust
pub fn register(&self, worker: Arc<dyn Worker>) -> WorkerId {
    let worker_id = if let Some(existing_id) = self.url_to_id.get(worker.url()) {
        existing_id.clone()   // 同 URL 已存在，复用旧 ID
    } else {
        WorkerId::new()        // 否则生成新 ID
    };
    ...
```

这保证了「同 URL 的多次注册拿到同一个 ID」。

而 `reserve_id_for_url` 则更进一步，用 DashMap 的 entry API 一步完成「占位」，规避竞态（[worker_registry.rs:301-303](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/worker_registry.rs#L301-L303)）：

```rust
pub fn reserve_id_for_url(&self, url: &str) -> WorkerId {
    self.url_to_id.entry(url.to_string()).or_default().clone()
}
```

> `entry().or_default()`：key 不存在就插入一个 `Default`（即新生成的 UUID）的 `WorkerId`，存在就直接返回引用。整个判断 + 插入在 DashMap 分片锁内完成，是原子的。`WorkerService` 在异步注册流程开始时就先调它锁定 ID，避免两个并发注册给同一个 URL 分配两个不同 ID。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：理解 ID 的「复用」与「占位」两条路径区别。
2. **操作步骤**：
   - 打开 `src/core/worker_service.rs`，定位到调用 `reserve_id_for_url` 的地方（约第 241 行）。
   - 思考：为什么注册流程要「先 reserve、再 register」两步，而不是直接 `register`？
3. **需要观察的现象**：你会看到 `reserve_id_for_url` 在工作流**开始**时被调用，拿到一个 ID 串起后续步骤；而 `register` 在工作流**末尾**才把真正的 Worker 对象塞进 registry。
4. **预期结果**：理解这是为了在「发现元数据 → 建连 → 激活」这段耗时窗口里，URL→ID 的映射已经稳定存在，避免重复注册。
5. **结论**：待本地确认 `worker_service.rs:241` 的调用上下文与你的推断一致。

#### 4.1.5 小练习与答案

**练习 1**：`WorkerId` 为什么要派生 `Hash` 和 `Eq`？

**参考答案**：因为它要作为 `DashMap<WorkerId, Arc<dyn Worker>>` 的 key，而 `DashMap`（和标准 `HashMap` 一样）要求 key 类型实现 `Hash + Eq` 才能计算分片与判等。

**练习 2**：如果两个线程同时 `register` 同一个 URL 的 Worker，最终 registry 里会有几个条目？

**参考答案**：只有一个。因为 `register` 内部用 `url_to_id.get(url)` 判断 URL 是否已存在；即便存在竞态，`url_to_id.insert` 也会用相同 key 覆盖，`workers` 表里的 key（`WorkerId`）也会因复用逻辑而相同。若要完全杜绝竞态，应先 `reserve_id_for_url`。

---

### 4.2 WorkerRegistry：并发安全的多维度注册表

#### 4.2.1 概念说明

`WorkerRegistry` 是控制面的「真相之源」（source of truth）：**当前有哪些 Worker、它们各自是什么类型、服务什么模型、健康与否**。它把同一个 Worker 集合，按五种不同的 key 预先建好索引，让路由器按任意维度都能 O(1) 或 O(log n) 地取到候选列表。

为了支持高并发读，它大量使用 `dashmap::DashMap`（分片锁的并发 map），并把最热的「按模型」索引设计成**不可变快照**，实现真正的无锁读。

#### 4.2.2 核心流程

registry 内部维护 **6 张并发表**：

| 字段 | key → value | 用途 |
|------|-------------|------|
| `workers` | `WorkerId → Arc<dyn Worker>` | 主表，存全部 Worker |
| `model_index` | `模型 → Arc<[Arc<dyn Worker>]>` | 按模型 O(1) 取候选（不可变快照） |
| `hash_rings` | `模型 → Arc<HashRing>` | 按模型取预计算的一致性哈希环 |
| `type_workers` | `WorkerType → Vec<WorkerId>` | 按 Regular/Prefill/Decode 分类 |
| `connection_workers` | `ConnectionMode → Vec<WorkerId>` | 按 HTTP/gRPC 分类 |
| `url_to_id` | `URL → WorkerId` | URL 反查 ID（去重 + 复用） |

**注册（`register`）流程**：

```
register(worker)
  ├─ 查 url_to_id：同 URL 复用旧 ID，否则新建 UUID
  ├─ workers.insert(id, worker)
  ├─ url_to_id.insert(url, id)
  ├─ model_index：对该模型的快照做 copy-on-write（克隆 + 追加）
  ├─ rebuild_hash_ring(model)          ← 重建该模型的环
  ├─ type_workers[type].push(id)
  ├─ connection_workers[conn].push(id)
  └─ 若启用 mesh：sync_worker_state(...) 同步到集群
```

**移除（`remove`）流程**与之对称：从主表删 → 从 URL 映射删 → model_index 快照过滤重建 → 重建环 → 从 type/connection 索引 `retain` 过滤 → 标记 unhealthy + 清理指标 → 同步 mesh。

**读（`get_by_model`）流程**：直接从 `model_index` 取出 `Arc<[...]>` 再 `Arc::clone`，**一次原子引用计数自增**，零锁。

#### 4.2.3 源码精读

先看 6 张表的定义（[worker_registry.rs:180-204](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/worker_registry.rs#L180-L204)）：

```rust
pub struct WorkerRegistry {
    workers: Arc<DashMap<WorkerId, Arc<dyn Worker>>>,
    model_index: ModelIndex,                                       // Arc<DashMap<String, Arc<[Arc<dyn Worker>]>>>
    hash_rings: Arc<DashMap<String, Arc<HashRing>>>,
    type_workers: Arc<DashMap<WorkerType, Vec<WorkerId>>>,
    connection_workers: Arc<DashMap<ConnectionMode, Vec<WorkerId>>>,
    url_to_id: Arc<DashMap<String, WorkerId>>,
    mesh_sync: Arc<RwLock<OptionalMeshSyncManager>>,
}
```

> 注意 `model_index` 的 value 类型 `Arc<[Arc<dyn Worker>]>`：这是一个**不可变的、引用计数的切片**。读它不需要任何锁，只是原子地 bump 一次引用计数。这就是文件头注释里强调的「lock-free reads」。

`model_index` 的类型别名（[worker_registry.rs:176](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/worker_registry.rs#L176)）：

```rust
type ModelIndex = Arc<DashMap<String, Arc<[Arc<dyn Worker>]>>>;
```

注册时对 `model_index` 的 copy-on-write 更新是关键（[worker_registry.rs:260-268](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/worker_registry.rs#L260-L268)）：

```rust
let model_id = worker.model_id().to_string();
self.model_index
    .entry(model_id.clone())
    .and_modify(|existing| {
        // 把旧快照克隆出来，追加新 worker，再整体替换为一个新 Arc<[...]>
        let mut new_workers: Vec<Arc<dyn Worker>> = existing.iter().cloned().collect();
        new_workers.push(worker.clone());
        *existing = Arc::from(new_workers.into_boxed_slice());
    })
    .or_insert_with(|| Arc::from(vec![worker.clone()].into_boxed_slice()));
```

> 这里不直接 `push` 进现有 Vec，而是**新建一个不可变快照**替换旧的。正在读旧快照的请求仍持有旧 `Arc`，不受影响；新请求会读到新快照。这就是无锁读的代价：每次写都重建一次切片。

紧接着重建该模型的哈希环（[worker_registry.rs:270-271](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/worker_registry.rs#L270-L271) 调用 [worker_registry.rs:221-229](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/worker_registry.rs#L221-L229)）：

```rust
fn rebuild_hash_ring(&self, model_id: &str) {
    if let Some(workers) = self.model_index.get(model_id) {
        let ring = HashRing::new(&workers);
        self.hash_rings.insert(model_id.to_string(), Arc::new(ring));
    } else {
        self.hash_rings.remove(model_id);   // 该模型已无 Worker，删环
    }
}
```

读路径则极致轻量（[worker_registry.rs:388-393](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/worker_registry.rs#L388-L393)）：

```rust
pub fn get_by_model(&self, model_id: &str) -> Arc<[Arc<dyn Worker>]> {
    self.model_index
        .get(model_id)
        .map(|workers| Arc::clone(&workers))
        .unwrap_or_else(|| Arc::from(Self::EMPTY_WORKERS))
}
```

按维度查询还有 `get_by_type`、`get_by_connection`（[worker_registry.rs:396-401](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/worker_registry.rs#L396-L401) 与 [worker_registry.rs:442-447](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/worker_registry.rs#L442-L447)）：它们先从对应索引拿到 `Vec<WorkerId>`，再用 `self.get(id)` 逐个取回 Worker 对象。

PD 模式专用的 `get_prefill_workers`（[worker_registry.rs:423-434](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/worker_registry.rs#L423-L434)）直接遍历主表，用 `match worker_type` 过滤出 `Prefill{..}`（注意它**忽略 bootstrap_port**，任何 prefill 都算）；`get_decode_workers` 则复用 `get_by_type(&WorkerType::Decode)`。

还有两个「组合查询」入口值得记住：

- `get_workers_filtered`（[worker_registry.rs:512-561](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/worker_registry.rs#L512-L561)）：接受 model/type/connection/runtime/healthy 任意组合的过滤条件，优先用 `model_index` 走 O(1) 起点，再在结果上叠 filter。gRPC 的 PD 选择阶段就用它按 `Grpc{port:None}` 通配匹配所有 gRPC Worker。
- `stats`（[worker_registry.rs:564-623](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/worker_registry.rs#L564-L623)）：一次遍历主表，统计总数、健康数、各类型数、熔断器各状态数，填进 `WorkerRegistryStats`（[worker_registry.rs:703-728](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/worker_registry.rs#L703-L728)）。这是 `/workers` 这类管理端点展示状态的来源。

#### 4.2.4 代码实践（源码阅读型 + 测试）

1. **实践目标**：验证「同 URL 复用 ID」「按模型查询」「PD 分类查询」三条行为。
2. **操作步骤**：阅读并运行仓库自带的测试 `test_model_index_fast_lookup`（[worker_registry.rs:774-835](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/worker_registry.rs#L774-L835)）。
3. **需要观察的现象**：注册 3 个 Worker（两个 `llama-3`、一个 `gpt-4`）后，`get_by_model("llama-3")` 返回 2 个、`get_by_model("gpt-4")` 返回 1 个、`get_by_model("unknown-model")` 返回 0 个；`remove_by_url` 后数量正确减少。
4. **预期结果**：测试通过。运行命令 `cargo test -p smg test_model_index_fast_lookup`。
5. **若运行失败**：待本地验证依赖与编译环境。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `model_index` 用 `Arc<[Arc<dyn Worker>]>` 而不是 `Arc<RwLock<Vec<Arc<dyn Worker>>>>`？

**参考答案**：前者是**不可变快照**，读取只需一次原子引用计数自增，完全无锁、无竞争；后者读取要抢读锁，高并发下读读之间也有 cache-line 争用。网关读极多写极少，用 copy-on-write 的不可变快照把读代价降到最低，付出的代价只是写时重建一次切片。

**练习 2**：`get_prefill_workers` 为什么不直接用 `type_workers` 索引，而要遍历主表？

**参考答案**：因为 `WorkerType::Prefill { bootstrap_port }` 携带端口字段，`type_workers` 的 key 是**完整**的 `WorkerType`，不同端口的 prefill 会落到不同 key。`get_prefill_workers` 的语义是「要所有 prefill、不管端口」，所以它用 `match WorkerType::Prefill { .. }` 通配，只能遍历主表。

---

### 4.3 HashRing：一致性哈希环

#### 4.3.1 概念说明

`HashRing` 把「按 key 选 Worker」从 O(n) 遍历降到 O(log n)。它解决两个问题：

1. **均匀分布**：同一个物理 Worker 在环上放多个「虚拟节点」，避免少量 Worker 时分布严重倾斜。
2. **最小迁移**：增删 Worker 时，只有约 \(1/N\) 的 key 改变归属，保护 KV cache 与会话亲和。

环用一个按位置排序的数组 `(ring_position, worker_url)` 表示，查找用二分。

#### 4.3.2 核心流程

**建环**（`HashRing::new`）：

```
对每个 worker：
    url = worker.url()
    对 vnode in 0..150：
        pos = blake3(url || "#" || vnode.to_le_bytes()) 取前 8 字节
        entries.push((pos, Arc<clone>(url)))   ← 150 个虚拟节点共享同一个 Arc<str>
按 pos 排序（便于二分）
```

**查环**（`find_healthy_url(key, is_healthy)`）：

```
key_pos = blake3(key) 取前 8 字节
start = 二分找到第一个 pos >= key_pos 的下标（partition_point）
从 start 开始顺时针走（到末尾绕回头部）：
    用 HashSet 去重，避免对同一 Worker 的多个虚拟节点重复判定
    第一个满足 is_healthy(url) 的 url 即返回
全部走完都不健康 → None
```

**为什么是 150 个虚拟节点？** 这是工程上常见的折中：虚拟节点越多，分布越均匀，但内存与建环开销越大。150 在「均匀性」和「内存」之间取得平衡。注释（[worker_registry.rs:29-31](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/worker_registry.rs#L29-L31)）专门说明了这个选择。

**为什么用 blake3？** 它快、稳定，且哈希值**不随 Rust 版本变化**（不像 `std::hash::DefaultHasher` 的结果在不同版本/平台可能不同）。一致性哈希要求「同一个 key 永远落到同一个位置」，因此必须用确定性哈希。

#### 4.3.3 源码精读

常量与结构（[worker_registry.rs:31](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/worker_registry.rs#L31) 与 [worker_registry.rs:42-48](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/worker_registry.rs#L42-L48)）：

```rust
const VIRTUAL_NODES_PER_WORKER: usize = 150;

pub struct HashRing {
    /// 按 ring_position 排序的 (位置, worker_url) 数组。
    /// 每个 worker 占 150 个虚拟节点，用 Arc<str> 共享 URL，避免 150 份拷贝。
    entries: Arc<[(u64, Arc<str>)]>,
}
```

建环（[worker_registry.rs:53-80](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/worker_registry.rs#L53-L80)），关键片段：

```rust
for worker in workers {
    let url: Arc<str> = Arc::from(worker.url());   // 每个 worker 只建一份 Arc<str>
    let url_bytes = url.as_bytes();
    for vnode in 0..VIRTUAL_NODES_PER_WORKER {
        let mut hasher = blake3::Hasher::new();
        hasher.update(url_bytes);
        hasher.update(b"#");
        hasher.update(&(vnode as u64).to_le_bytes());
        let pos = u64::from_le_bytes(hasher.finalize().as_bytes()[..8].try_into().unwrap());
        entries.push((pos, Arc::clone(&url)));      // 150 个虚拟节点共享同一 Arc<str>
    }
}
entries.sort_unstable_by_key(|(pos, _)| *pos);      // 排序以支持二分
```

> `url + "#" + vnode` 的拼接保证同一 Worker 的不同虚拟节点落在环上不同位置；用 `Arc::clone(&url)` 而非 `url.to_string()`，让 150 个条目共享同一个字符串分配，省内存。

查找算法（[worker_registry.rs:95-128](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/worker_registry.rs#L95-L128)）：

```rust
pub fn find_healthy_url<F>(&self, key: &str, is_healthy: F) -> Option<&str>
where F: Fn(&str) -> bool,
{
    if self.entries.is_empty() { return None; }
    let key_pos = Self::hash_position(key);
    let start = self.entries.partition_point(|(pos, _)| *pos < key_pos);  // 二分定位
    let mut checked_urls = HashSet::with_capacity(self.worker_count().min(16));
    for i in 0..self.entries.len() {
        let (_, url) = &self.entries[(start + i) % self.entries.len()];   // 顺时针，绕回
        if !checked_urls.insert(url) { continue; }   // 跳过同 Worker 的其它虚拟节点
        if is_healthy(url) { return Some(url); }
    }
    None
}
```

> 三个细节：(1) `partition_point` 是标准库的二分，O(log n) 定位起点；(2) `(start + i) % len` 实现「走到末尾绕回头部」的环形遍历；(3) `checked_urls` 去重——因为同一个 Worker 在环上有 150 个点，顺时针走时可能连续撞到同一个 Worker 的多个虚拟节点，去重避免对它重复调用 `is_healthy`，也保证最多只判定 `worker_count()` 次。

注意 key 的位置和 worker 的虚拟节点位置用了**不同**的哈希入参（key 直接 `blake3(key)`，虚拟节点是 `url#vnode`），但**同一个** blake3 算法，所以它们落在同一个 \([0, 2^{64})\) 空间里，顺时针规则才有意义。

#### 4.3.4 代码实践（见本讲第 5 节综合实践）

`HashRing` 的实践与「迁移比例」紧密相关，统一放在第 5 节给出可运行测试。

#### 4.3.5 小练习与答案

**练习 1**：`find_healthy_url` 最坏情况下要调用多少次 `is_healthy`？

**参考答案**：最多 `worker_count()` 次（即环上不重复的 Worker 数）。因为有 `checked_urls` 去重，每个 Worker 最多被判定一次；最坏情况是所有 Worker 都不健康，遍历完所有不重复 URL 后返回 `None`。

**练习 2**：把虚拟节点数从 150 改成 1，会发生什么？

**参考答案**：Worker 在环上只有一个点，少量 Worker（比如 3 个）时分布会严重不均——某些 Worker 可能分到远多于 1/3 的 key，迁移比例也会偏离 \(1/N\)。虚拟节点的意义就是把「一个 Worker」摊薄成环上多个点，逼近均匀分布。

---

### 4.4 get_hash_ring 缓存、set_mesh_sync 与消费侧

#### 4.4.1 概念说明

`HashRing` 的构建要遍历所有 Worker × 150 个虚拟节点并排序，是 O(W·150·log) 的开销。如果每次请求都重建，代价不可接受。因此 registry 把每个模型的环**预计算并缓存**在 `hash_rings` 表里，只在 Worker 增删时重建（见 4.2.3 的 `rebuild_hash_ring`）。请求路径只调用 `get_hash_ring(model)`，拿到一个 `Arc<HashRing>` 直接用。

另外，当网关以 Mesh 模式多实例部署时，registry 还负责把 Worker 状态同步给集群（`set_mesh_sync` 注入一个可选的同步管理器）。这个机制让 registry 在「单机」和「多机」两种部署下用同一套代码。

#### 4.4.2 核心流程

**取环**：

```
get_hash_ring(model) → hash_rings.get(model).map(Arc::clone) → Option<Arc<HashRing>>
```

**Mesh 注入**（启动期，`server.rs`）：

```
若 mesh_sync_manager 存在：
    worker_registry.set_mesh_sync(Some(manager))
    policy_registry.set_mesh_sync(Some(manager))
此后 register/remove/update_worker_health 都会顺带 sync_worker_state / remove_worker_state
```

**消费侧**（gRPC Worker 选择阶段）：

```
ring = worker_registry.get_hash_ring(model)
policy.select_worker(workers, SelectWorkerInfo { hash_ring: ring, ... })
  └─ 策略内部：ring.find_healthy_url(key, |url| 健康集合.contains(url))
```

#### 4.4.3 源码精读

`get_hash_ring` 极简，一次 DashMap 读 + Arc 克隆（[worker_registry.rs:232-234](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/worker_registry.rs#L232-L234)）：

```rust
pub fn get_hash_ring(&self, model_id: &str) -> Option<Arc<HashRing>> {
    self.hash_rings.get(model_id).map(|r| Arc::clone(&r))
}
```

`set_mesh_sync` 用 `RwLock` 包住可选的同步管理器，允许**初始化之后**再线程安全地设置（[worker_registry.rs:237-239](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/worker_registry.rs#L237-L239)）：

```rust
pub fn set_mesh_sync(&self, mesh_sync: OptionalMeshSyncManager) {
    *self.mesh_sync.write().unwrap() = mesh_sync;
}
```

> 为什么用 `RwLock<Option<...>>` 而不是 `OnceLock`？因为 `OptionalMeshSyncManager` 是 `Option<...>`，且「Mesh 关闭」时也可能是 `None`、之后又可能被设置——`set_mesh_sync` 需要可写。读多写少，`RwLock` 合适。注释（[worker_registry.rs:200-203](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/worker_registry.rs#L200-L203)）指出：`None` 时 registry 完全独立工作，不依赖 Mesh。

注册时的同步调用（[worker_registry.rs:286-294](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/worker_registry.rs#L286-L294)）：

```rust
if let Some(ref mesh_sync) = *self.mesh_sync.read().unwrap() {
    mesh_sync.sync_worker_state(
        worker_id.as_str().to_string(),
        worker.model_id().to_string(),
        worker.url().to_string(),
        worker.is_healthy(),
        0.0, // TODO: Get actual load
    );
}
```

> 注意 `if let Some(ref mesh_sync) = *...read().unwrap()`——读到 `None` 就什么都不做，这就是「Mesh 关闭时 no-op」的实现。

启动期的注入点在 `server.rs`（[server.rs:960-974](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/server.rs#L960-L974)）：Mesh 启用后，把同一个 `sync_manager` 同时挂到 `worker_registry` 和 `policy_registry` 上。

最后看消费侧——gRPC Worker 选择阶段如何取环并传给策略（[worker_selection.rs:164-180](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/routers/grpc/common/stages/worker_selection.rs#L164-L180)）：

```rust
let hash_ring = self.worker_registry
    .get_hash_ring(model_id.unwrap_or(UNKNOWN_MODEL_ID));
let idx = policy.select_worker(
    &available,
    &SelectWorkerInfo { request_text: text, tokens, headers, hash_ring },
).await?;
```

而策略内部（如 `ConsistentHashingPolicy`）会用 `ring.find_healthy_url` 把 key 映射到健康 Worker（[consistent_hashing.rs:86-91](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/policies/consistent_hashing.rs#L86-L91)）：

```rust
if let Some(ref ring) = info.hash_ring {
    let url = ring.find_healthy_url(key, |url| healthy_url_to_idx.contains_key(url))?;
    return healthy_url_to_idx.get(url).copied();
}
```

> 环返回的是 worker **URL**，策略再把它映射回 workers 数组里的**下标**。这样即便 workers 数组经过了过滤（比如只留健康的），也能通过 URL 正确匹配。`SelectWorkerInfo.hash_ring` 字段在 [policies/mod.rs:171-173](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/policies/mod.rs#L171-L173) 定义，注释明确说它是「由 WorkerRegistry 构建并缓存、透传给策略以避免每次请求重建」。

#### 4.4.4 代码实践（源码阅读型）

1. **实践目标**：串起「registry 缓存环 → 透传给策略 → 策略调用 find_healthy_url」整条调用链。
2. **操作步骤**：
   - 在 `src/routers/grpc/common/stages/worker_selection.rs:167` 处看到 `get_hash_ring`。
   - 在 `src/policies/consistent_hashing.rs:89` 处看到 `find_healthy_url`。
   - 在 `src/policies/prefix_hash.rs:165` 处看到另一个消费者（前缀哈希策略也复用同一个环）。
3. **需要观察的现象**：两个不同策略（一致性哈希、前缀哈希）都**不自己建环**，而是复用 registry 预计算好的同一个 `Arc<HashRing>`。
4. **预期结果**：理解「环属于 registry，策略只是消费者」的分工——这正是 `SelectWorkerInfo` 把 `hash_ring` 作为字段透传的原因。
5. **结论**：待本地确认 `prefix_hash.rs:165` 的调用与上述一致。

#### 4.4.5 小练习与答案

**练习 1**：为什么把环放在 registry 里缓存，而不是让每个策略自己建？

**参考答案**：建环要遍历 Worker × 150 虚拟节点并排序，开销大；而同一个模型的 Worker 集合在两次增删之间是不变的，所有策略（一致性哈希、前缀哈希等）看到的环应当一致。集中缓存既省去每请求重建的开销，又保证不同策略看到同一份「真相」。

**练习 2**：`set_mesh_sync` 传入 `None` 时，registry 的行为会变成什么样？

**参考答案**：`register`/`remove`/`update_worker_health` 里那段 `if let Some(ref mesh_sync) = ...` 不进分支，完全不调用任何同步方法——registry 退化为纯单机模式，Mesh 集群功能关闭。

---

## 5. 综合实践

**任务**：编写一个测试，注册若干 Worker 到 registry，分别调用 `get_by_model` / `get_prefill_workers` / `get_hash_ring`，并量化验证「一致性哈希在 Worker 增减时的迁移比例接近 \(1/N\)」。

下面是一段**示例代码**（非项目原有代码），你可以把它加到 `src/core/worker_registry.rs` 的 `#[cfg(test)] mod tests` 内，或放进自己的临时测试文件运行：

```rust
// 示例代码：验证一致性哈希迁移比例
#[test]
fn test_hash_ring_migration_ratio() {
    use crate::core::{BasicWorkerBuilder, WorkerType, WorkerRegistry, Worker};
    use std::collections::{HashMap, HashSet};
    use std::sync::Arc;

    let registry = WorkerRegistry::new();
    let model = "llama-3";

    // 1. 注册 3 个 Regular、同模型的 Worker
    let urls: Vec<String> = (0..3).map(|i| format!("http://w{}:8000", i)).collect();
    for url in &urls {
        let mut labels = HashMap::new();
        labels.insert("model_id".to_string(), model.to_string());
        let worker: Arc<dyn Worker> = Arc::new(
            BasicWorkerBuilder::new(url.as_str())
                .worker_type(WorkerType::Regular)
                .labels(labels)
                .build(),
        );
        registry.register(worker);
    }

    // 2. 验证按模型查询与 PD 查询
    assert_eq!(registry.get_by_model(model).len(), 3);
    assert_eq!(registry.get_prefill_workers().len(), 0); // 没有 prefill

    // 3. 取缓存好的环
    let ring = registry.get_hash_ring(model).expect("模型应有环");
    assert_eq!(ring.worker_count(), 3);
    assert_eq!(ring.len(), 3 * 150);

    // 4. 对一批 key 选 Worker（全部健康）
    let healthy: HashSet<String> = urls.iter().cloned().collect();
    let keys: Vec<String> = (0..3000).map(|i| format!("prompt-{i}")).collect();
    let before: Vec<String> = keys
        .iter()
        .map(|k| ring.find_healthy_url(k, |u| healthy.contains(u)).unwrap().to_string())
        .collect();

    // 5. 新增第 4 个 Worker，环自动重建
    let new_url = "http://w3:8000".to_string();
    let mut labels = HashMap::new();
    labels.insert("model_id".to_string(), model.to_string());
    let new_worker: Arc<dyn Worker> = Arc::new(
        BasicWorkerBuilder::new(new_url.as_str())
            .worker_type(WorkerType::Regular)
            .labels(labels)
            .build(),
    );
    registry.register(new_worker);

    let ring2 = registry.get_hash_ring(model).expect("重建后仍应有环");
    assert_eq!(ring2.worker_count(), 4);

    // 6. 重新选并统计迁移数
    let healthy2: HashSet<String> = urls.iter().chain(std::iter::once(&new_url)).cloned().collect();
    let moved = keys
        .iter()
        .zip(before.iter())
        .filter(|(k, old)| {
            let now = ring2.find_healthy_url(k, |u| healthy2.contains(u)).unwrap();
            now != old.as_str()
        })
        .count();
    let ratio = moved as f64 / keys.len() as f64;

    // 7. 期望迁移比例 ≈ 1/(N+1) = 1/4 = 0.25，留出统计容差
    println!("3→4 迁移比例 = {:.3}（理论 ~0.25）", ratio);
    assert!(ratio < 0.32, "迁移比例异常偏高: {ratio}");
    assert!(ratio > 0.18, "迁移比例异常偏低: {ratio}");
}
```

**操作步骤与观察要点**：

1. 把上面代码放入测试模块，运行 `cargo test -p smg test_hash_ring_migration_ratio -- --nocapture`。
2. 观察打印的迁移比例，应在 0.25 附近波动（3000 个样本下波动很小）。
3. 把样本数从 3→4 改成 9→10，重跑，观察迁移比例是否趋近 \(1/10 = 0.10\)——这验证了「Worker 越多，单次扩容迁移越少」的一致性哈希特性。
4. 作为对照，思考：若改用 `blake3(key) % N` 模取模，3→4 时迁移比例会接近多少？（答案：接近 0.75，几乎全部迁移。）

**预期结果**：测试通过，迁移比例落在理论 \(1/N\) 附近。**若本地环境无法编译/运行，明确标注「待本地验证」。**

> 对照基准：仓库已有 `test_model_index_fast_lookup`（[worker_registry.rs:774](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/worker_registry.rs#L774)）覆盖了注册/按模型查询/移除的正向路径，可先确保它在你的环境通过，再跑上面的迁移测试。

## 6. 本讲小结

- `WorkerId` 是 UUID v4 的 newtype，registry 通过 `url_to_id` 实现「同 URL 复用 ID」，用 `reserve_id_for_url` 的 entry API 原子占位以规避竞态。
- `WorkerRegistry` 维护 6 张并发表（workers / model_index / hash_rings / type_workers / connection_workers / url_to_id），把同一批 Worker 按五种维度预先索引。
- 读路径用「不可变快照 + Arc 引用计数」实现无锁读；写路径用 copy-on-write 重建快照，并同步重建该模型的哈希环、刷新各维度索引、可选同步 Mesh。
- `HashRing` 用 blake3 把每个 Worker 在 \([0,2^{64})\) 环上放 150 个虚拟节点（共享一个 `Arc<str>`），按位置排序；`find_healthy_url` 用二分定位 + 顺时针遍历 + HashSet 去重，O(log n) 选出第一个健康 Worker。
- 一致性哈希使增删一个 Worker 时迁移比例约为 \(1/N\)，远优于 `hash % N` 的几乎全迁移，保护了 KV cache 与会话亲和。
- 环由 registry 预计算缓存（`get_hash_ring`），只在 Worker 增删时重建；策略层（`consistent_hashing` / `prefix_hash`）通过 `SelectWorkerInfo.hash_ring` 透传复用，不自己建环。
- `set_mesh_sync` 让同一份 registry 代码在单机（`None`，所有同步 no-op）和多机 Mesh 两种部署下都能工作。

## 7. 下一步学习建议

本讲建立了「Worker 集合的存储与索引」。接下来按依赖顺序建议：

1. **u3-l3 WorkerManager 与 LoadMonitor**：registry 只存静态信息，Worker 负载（`load()`）是动态的。`WorkerManager` / `LoadMonitor` 负责周期采样负载，驱动 `power_of_two` 等策略。
2. **u3-l4 JobQueue 异步作业队列**：本讲的 `register`/`remove` 是同步的「底层原语」，真实注册走的是 `JobQueue` 提交的 `AddWorker`/`RemoveWorker` 作业——理解这层异步封装。
3. **u5-l3 cache_aware 策略** 与 **u5-l4 一致性哈希策略族**：本讲的 `HashRing` 是这些策略的「底层数据结构」，到那时你会看到 `find_healthy_url` 的返回值如何与负载均衡阈值、routing key 结合做最终选路。
4. **u9-l4 Mesh / 高可用 CRDT 状态同步**：本讲只点了 `set_mesh_sync` 的接口，Mesh 多实例如何用 CRDT 同步 Worker 与前缀树状态，留到那里展开。
