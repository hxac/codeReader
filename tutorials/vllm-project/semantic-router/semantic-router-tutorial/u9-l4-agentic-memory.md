# 智能体记忆（Agentic Memory）

## 1. 本讲目标

本讲讲解 vLLM Semantic Router（SR）的**智能体记忆子系统** `pkg/memory`：它让路由器能跨会话地记住"关于用户的事实"，并在后续请求里把这些记忆检索出来、注入到发给模型的上下文里。读完后你应该能够：

- 说清 `Store` 接口承担了哪些职责，以及为什么记忆存储被抽象成可替换的后端；
- 区分 **semantic / procedural / episodic** 三种记忆类型，以及 `Memory` 结构里每个字段的用途；
- 描述一次 `Retrieve` 如何把**向量相似度检索**与 **BM25 + n-gram 混合重排**、**自适应阈值**、**记忆过滤器（MemoryFilter）** 串成一条完整的检索流水线。

本讲是「向量检索、语义缓存与记忆」单元（u9）的收尾，承接 u9-l2（向量存储与多后端）建立的 `VectorStoreBackend` 抽象，把检索能力专门用到"记忆"这一业务对象上。

## 2. 前置知识

阅读本讲前，建议你已经了解：

- **嵌入（embedding）**：把一段文本映射成一个固定维度的浮点向量，语义相近的文本向量也相近；相似度通常用余弦相似度（cosine similarity）度量。参见 u9-l1 / u8-l4。
- **向量检索 / HNSW**：在海量向量里快速找 top-k 近邻的近似算法。SR 用 Milvus 的服务端 HNSW 或进程内 HNSW，参见 u9-l1 / u9-l2。
- **余弦相似度**：两个向量夹角的余弦，范围 \([-1,1]\)，归一化向量下与点积等价，常被截到 \([0,1]\) 当"相关度"用。
- **BM25**：经典的关键词检索打分函数，用词频（TF）饱和与文档长度归一化衡量"这篇文档对这个词有多重要"。
- **多租户隔离**：记忆天然是用户私有的，必须保证 A 用户永远检索不到 B 用户的记忆。u9-l3 讲过缓存里的 HMAC 命名空间隔离，本讲讲记忆里的另一种隔离手段。

一句话定位：缓存（u9-l3）缓存的是"一次完整的请求-响应对"，目标是**省一次推理**；记忆（本讲）存的是"从对话里提炼出的事实条目"，目标是**让模型在未来的对话里"记得"这个用户**。二者都靠嵌入检索，但生命周期与语义不同。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/semantic-router/pkg/memory/store.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/memory/store.go) | 定义 `Store` 接口（增删改查 + 检索）与全局单例 `globalMemoryStore`。 |
| [src/semantic-router/pkg/memory/types.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/memory/types.go) | 核心数据类型：`MemoryType`（三种记忆类型）、`Memory`、`RetrieveResult`、`RetrieveOptions`、`MemoryFilter`、`MemoryScope`。 |
| [src/semantic-router/pkg/memory/milvus_store.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/memory/milvus_store.go) | `MilvusStore` 结构体、构造函数、连接/重试基础设施。 |
| [src/semantic-router/pkg/memory/milvus_store_retrieve.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/memory/milvus_store_retrieve.go) | `Retrieve` 的核心流水线：归一化选项 → 嵌入 → 向量召回 → 混合重排 → 自适应阈值 → 截断。 |
| [src/semantic-router/pkg/memory/inmemory_store.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/memory/inmemory_store.go) | 进程内实现，暴力余弦检索，用于测试与 POC，是理解接口的最小样例。 |
| [src/semantic-router/pkg/memory/reflection.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/memory/reflection.go) | `ReflectionGate`——检索后的启发式过滤器：时效衰减、去重、token 预算。 |
| [src/semantic-router/pkg/memory/filter_registry.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/memory/filter_registry.go) | `MemoryFilter` 的注册表与工厂 `NewMemoryFilter`，支持 `heuristic` / `noop` 两种算法。 |
| [src/semantic-router/pkg/memory/milvus_filter.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/memory/milvus_filter.go) | 把"用户隔离"编译成 Milvus 过滤表达式，含注入防护。 |
| [src/semantic-router/pkg/vectorstore/hybrid.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/vectorstore/hybrid.go) | 混合检索的公共算子：`BM25Index`、`NgramIndex`、`FuseScores`（被记忆的 `hybridRerank` 复用）。 |
| [src/semantic-router/pkg/extproc/router_memory.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/router_memory.go) | 后端工厂：按 `config.Memory.Backend` 选择 Milvus / Valkey / Qdrant 并拼装 `Store`。 |

---

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：**① Store 接口与存储抽象**、**② Memory 与记忆类型**、**③ 混合检索与重排**。

### 4.1 Store 接口：记忆存储的统一抽象

#### 4.1.1 概念说明

记忆子系统面临和向量库（u9-l2）、语义缓存（u9-l3）同样的"多后端"问题：测试时想用进程内 map，生产时想用 Milvus / Qdrant / Valkey。SR 的做法是定义一个**业务接口 `Store`**，把"记忆的增删改查与语义检索"约定下来，让不同后端各自实现。这与 `VectorStoreBackend`（管文档向量）、`CacheBackend`（管缓存条目）是平行的三套抽象，各自服务于一种业务对象。

`Store` 之上还挂了一个**全局单例** `globalMemoryStore`：ExtProc 路由器在启动时构造好 `Store` 并 `SetGlobalMemoryStore` 写入，API Server 等其他包用 `GetGlobalMemoryStore()` 现读。这种"显式传递 + 全局快捷入口"的双轨和 u4-l2 讲的 `routerruntime.Registry` 思路一致。

#### 4.1.2 核心流程

`Store` 接口的十个方法可分成四组：

1. **写入与单个读取**：`Store`（新建，ID 重复报错）、`Get`（按 ID 取）、`Update`（改，不存在报错）。
2. **语义检索**：`Retrieve`（按查询文本做向量检索，是本讲重点）。
3. **过滤列举与批量删除**：`List`（按 user/type 过滤、分页、按 `created_at` 倒序）、`Forget`（按 ID 删）、`ForgetByScope`（按 user/project/type 批量删）。
4. **生命周期**：`IsEnabled`、`CheckConnection`、`Close`。

接口注释明确要求**实现必须是线程安全的**，因为检索（读）与记忆写入（响应阶段）会并发发生。

#### 4.1.3 源码精读

接口本体在 [src/semantic-router/pkg/memory/store.go:31-70](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/memory/store.go#L31-L70)——这是整个子系统的契约：

```go
type Store interface {
    Store(ctx context.Context, memory *Memory) error
    Retrieve(ctx context.Context, opts RetrieveOptions) ([]*RetrieveResult, error)
    Get(ctx context.Context, id string) (*Memory, error)
    Update(ctx context.Context, id string, memory *Memory) error
    List(ctx context.Context, opts ListOptions) (*ListResult, error)
    Forget(ctx context.Context, id string) error
    ForgetByScope(ctx context.Context, scope MemoryScope) error
    IsEnabled() bool
    CheckConnection(ctx context.Context) error
    Close() error
}
```

几点关键约定（都写在注释里）：

- `Retrieve` 用的是"**基于嵌入的相似度检索**"（[store.go:38-40](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/memory/store.go#L38-L40)），即调用方只传文本查询，由实现负责把查询嵌入成向量。
- `List` 强制 `UserID` 必填、结果按 `created_at` 倒序（[store.go:50-52](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/memory/store.go#L50-L52)）——这是"列举"与"语义检索"的分界：列举不做相似度，只按属性过滤。
- `ForgetByScope` 的 `MemoryScope` 里 `UserID` 必填，`ProjectID`、`Types` 选填（[store.go:58-60](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/memory/store.go#L58-L60)）。

全局单例用一把 `sync.RWMutex` 保护，写用写锁、读用读锁（[store.go:8-29](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/memory/store.go#L8-L29)）：

```go
func SetGlobalMemoryStore(store Store) { /* 写锁 */ }
func GetGlobalMemoryStore() Store      { /* 读锁，未启用返回 nil */ }
```

后端选择不在 `pkg/memory` 内部，而在 extproc 的工厂里。`createMilvusMemoryStore` 会先连 Milvus、做健康检查，再调用 `memory.NewMilvusStore` 拼出实现（[src/semantic-router/pkg/extproc/router_memory.go:121-173](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/router_memory.go#L121-L173)）；Valkey、Qdrant 各有平行的 `createValkeyMemoryStore` / `createQdrantMemoryStore`。这就是"接口在 memory 包、装配在 extproc 包"的分工。

#### 4.1.4 代码实践

**实践目标**：建立"接口 ↔ 多实现"的直觉。

1. 打开 [store.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/memory/store.go) 与 [inmemory_store.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/memory/inmemory_store.go)。
2. 对照接口逐个核对 `InMemoryStore` 实现了哪十个方法（注意 `CheckConnection` 对内存后端"永远健康"）。
3. 在 [router_memory.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/router_memory.go) 里搜索 `createMilvusMemoryStore` / `createValkeyMemoryStore` / `createQdrantMemoryStore`，确认它们返回的都是 `memory.Store` 接口类型。

**需要观察的现象**：三个 `create*` 函数签名都以 `(memory.Store, error)` 返回，这就是"调用方只认接口、不认具体后端"的体现。

**预期结果**：你能用一句话回答"为什么切换记忆后端不需要改检索代码"——因为 `Retrieve` 的调用方只持有 `Store` 接口。运行命令需要 CGO 与外部依赖，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：`Store.Store` 与 `Store.Update` 的"报错条件"分别是什么？为什么不设计成 upsert（有则更新、无则新建）？

**参考答案**：`Store` 在 ID 已存在时报错，`Update` 在 ID 不存在时报错（见 [store.go:34-36](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/memory/store.go#L34-L36) 与 [store.go:46-48](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/memory/store.go#L46-L48)）。不做成 upsert 是为了让"新建一条记忆"和"修改已有记忆"在调用层是两个显式动作，避免后台合并（consolidation）等流程意外覆盖既有记忆。

**练习 2**：`List` 与 `Retrieve` 都能"找记忆"，二者最本质的区别是什么？

**参考答案**：`List` 是**非语义的属性过滤**（按 user/type 过滤、按时间倒序、分页），不计算相似度；`Retrieve` 是**语义检索**（把 query 嵌入后按向量相似度排序）。前者用于"管理面列出用户的所有记忆"，后者用于"请求时挑出与当前问题最相关的几条"。

---

### 4.2 Memory 与三种记忆类型

#### 4.2.1 概念说明

记忆子系统的核心业务对象是 `Memory`，它借鉴了认知科学对人类记忆的三分法：

- **semantic（语义记忆）**：事实、偏好、知识。例：*"用户去夏威夷的预算是 10000 美元"*。
- **procedural（程序记忆）**：操作步骤、how-to。例：*"部署 payment-service：先 npm build，再 docker push"*。
- **episodic（情景记忆）**：过去事件、会话摘要。例：*"2024-12-29，用户规划了 1 万美元预算的夏威夷假期"*。

把记忆分类型有两个实际好处：检索时可以按 `Types` 过滤（"只查操作步骤"），以及让模型在注入时知道每条记忆的"性质"。`MemoryType` 只是一个字符串类型别名，三类的区分主要靠约定与示例注释。

#### 4.2.2 核心流程

一条 `Memory` 的生命周期：

1. **产生**：响应阶段，记忆提取器（`extractor.go`）从对话里抽取出一条事实，填好 `Content` / `Type` / `UserID` / 溯源字段（`CreatedByUserID`、`ConversationID`、`CreatedVia`）。
2. **嵌入**：写入前由 `GenerateEmbedding` 把 `Content` 变成向量，存进 `Embedding` 字段（不序列化到 JSON）。
3. **存储**：`Store.Store` 落库；Milvus 后端把 `Embedding` 存进向量字段、其余元数据存进标量字段。
4. **检索**：`Retrieve` 时按相似度命中；命中后 `AccessCount` 自增、`LastAccessed` 刷新（用于留存评分与强化）。
5. **合并/遗忘**：后台 `ConsolidateUser` 把相似记忆合并，`ForgetByScope` 支持用户"删除我所有记忆"。

#### 4.2.3 源码精读

三种记忆类型定义在 [src/semantic-router/pkg/memory/types.go:12-24](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/memory/types.go#L12-L24)，每条注释都给了一个贴切的例子：

```go
MemoryTypeSemantic    MemoryType = "semantic"     // "User's budget for Hawaii is $10,000"
MemoryTypeProcedural  MemoryType = "procedural"   // "To deploy payment-service: run npm build..."
MemoryTypeEpisodic    MemoryType = "episodic"     // "On Dec 29 2024, user planned Hawaii vacation..."
```

`Memory` 结构体（[types.go:27-75](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/memory/types.go#L27-L75)）字段可分四组：

| 分组 | 字段 | 说明 |
| --- | --- | --- |
| 标识与内容 | `ID` / `Type` / `Content` | `Content` 注释强调"必须自包含上下文"——写 *"预算是 1 万"* 不如写 *"夏威夷之行的预算是 1 万"*，因为检索时没有上下文（[types.go:35-36](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/memory/types.go#L35-L36)）。 |
| 向量 | `Embedding` | 带 `json:"-"`，**不进 JSON**，只在后端内部流转（[types.go:38-39](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/memory/types.go#L38-L39)）。 |
| 作用域 | `UserID` / `ProjectID` / `Source` | `UserID` 是**用户隔离**的主键；`Source` 标来源（如 `consolidation`）。 |
| 时效与留存 | `CreatedAt` / `UpdatedAt` / `AccessCount` / `LastAccessed` / `Importance` | `AccessCount` 与 `LastAccessed` 用于留存评分（注释点明 \(S = S_0 + \text{AccessCount}\)，[types.go:56-60](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/memory/types.go#L56-L60)）；`Importance` 是 0~1 的优先级。 |
| 溯源（Provenance） | `CreatedByUserID` / `ConversationID` / `CreatedVia` | 记录"这条记忆是谁、在哪次会话、用什么方式产生的"（[types.go:65-74](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/memory/types.go#L65-L74)）。 |

检索相关三个类型是本讲后半段的主角：

- `RetrieveResult`（[types.go:77-84](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/memory/types.go#L77-L84)）= `Memory` + `Score`（0~1 相似度）。
- `RetrieveOptions`（[types.go:86-117](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/memory/types.go#L86-L117)）是检索的全部旋钮，重点几个：`Threshold` 默认 0.70（[types.go:103-104](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/memory/types.go#L103-L104)）、`Limit` 默认 5、`HybridSearch`/`HybridMode` 控制混合重排（[types.go:106-110](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/memory/types.go#L106-L110)）、`AdaptiveThreshold` 控制自适应阈值（[types.go:112-116](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/memory/types.go#L112-L116)）。
- `DefaultMemoryConfig`（[types.go:119-129](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/memory/types.go#L119-L129)）给出"兜底画像"：维度 384、limit 5、阈值 0.70。

> 提示：`MemoryConfig` 的完整字段在 [src/semantic-router/pkg/config/runtime_config.go:237-256](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/runtime_config.go#L237-L256)，包含 `Backend`（选 milvus/valkey/qdrant）、`HybridSearch`、`AdaptiveThreshold`、`Reflection` 等开关。

#### 4.2.4 代码实践

**实践目标**：用源码注释反推记忆抽取的设计意图。

1. 阅读 [types.go:26-75](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/memory/types.go#L26-L75) 的 `Memory` 注释。
2. 回答：为什么 `Content` 字段注释要求"自包含上下文"？联系"检索时不带历史"这一事实解释。
3. 看 `AccessCount` / `LastAccessed` 的注释，思考：如果两条记忆相似度一样，这两字段能怎么用来"优先保留更常用"的记忆？

**预期结果**：你能写出一段话，解释"自包含 Content"与"嵌入检索无上下文"之间的因果关系——因为检索时只对 `Content` 单独求嵌入，脱离原对话，所以 Content 必须自带主语与对象。

#### 4.2.5 小练习与答案

**练习**：请各举一个属于 semantic、procedural、episodic 的记忆例子（不要用源码注释里的夏威夷例子）。

**参考答案**（示例）：

- semantic：*"用户是素食者"*（关于用户的事实/偏好）。
- procedural：*"重启路由器要先 `make vllm-sr-dev` 再 `vllm-sr serve`"*（操作步骤）。
- episodic：*"上周三用户反馈登录接口 500，已定位为 Redis 连接池打满"*（过去事件）。

---

### 4.3 混合检索与重排：嵌入 + BM25 + 自适应阈值 + 过滤器

这是本讲的重头戏，回答实践任务里的问题：**一次 `Retrieve` 如何结合嵌入相似度与 BM25 重排选出最终记忆？**

#### 4.3.1 概念说明

纯向量检索有个老毛病：**词面不匹配**。查询 *"deploy payment-service"* 与一条记忆 *"部署支付服务先 build 再 push"* 语义相近但词面几乎不重叠，嵌入可能把它们排得很近，但也可能漏掉；反之，向量检索偶尔会把"形似而义远"的条目排到前面。

SR 的对策是**混合检索（hybrid search）**：先让向量检索拉一个**扩大版候选集**（topK × 8），再用两类文本信号重排——

- **BM25**：经典关键词检索，对查询里的每个词按 TF 饱和 + 文档长度归一化打分，擅长精确词面命中；
- **n-gram Jaccard**：把文本切成字符 n-gram 算 Jaccard 相似度，对拼写变形、部分匹配更鲁棒。

三路分数再用 **weighted** 或 **RRF** 两种方式融合成最终分。

但光有重排还不够，还有两个"收口"机制：

- **自适应阈值（adaptive threshold）**：固定阈值 0.70 太死板——有时所有候选都弱相关（最高分才 0.72），有时候选间有明显断层。自适应阈值在候选分数里找"最大间隔（elbow）"，只在断层之上保留，但用基础阈值做地板，避免注入弱相关记忆。
- **记忆过滤器（MemoryFilter）**：检索回来的记忆还要过一道"反思门（reflection gate）"，做时效衰减、去重、token 预算控制，**这一步在请求路径上、在 Store 之外**，是"检索后、注入前"的后处理。

#### 4.3.2 核心流程

`MilvusStore.Retrieve` 的完整流水线（[milvus_store_retrieve.go:16-63](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/memory/milvus_store_retrieve.go#L16-L63)）：

```text
Retrieve(opts)
  │
  ├─ ① normalizeRetrieveOpts：补默认 limit(=config) / threshold(=config)，校验 query/userID 非空
  │
  ├─ ② GenerateEmbedding(opts.Query)：文本 → 查询向量
  │
  ├─ ③ searchMemoryVectors：Milvus HNSW 余弦检索，取扩大版 topK
  │       filter = user_id == "<uid>" && (type 过滤)
  │       searchTopK = hybrid ? limit*8 : limit*4   (下限 20)
  │
  ├─ ④ finalizeRetrieveResults(sr, opts, limit, threshold)
  │      ├─ parseCandidates：把 Milvus 行解析成 []*RetrieveResult（带原始余弦分）
  │      ├─ if HybridSearch && len>1：hybridRerank（BM25 + n-gram 融合，重写 Score）
  │      ├─ if AdaptiveThreshold && len>1：adaptiveThresholdElbow 抬高 threshold
  │      └─ applyRetrieveThreshold：按 Score 降序取 ≥ threshold 的前 limit 条
  │           （命中则异步 recordRetrievalBatch 累加 AccessCount）
  │
  └─ 返回 []*RetrieveResult
```

注意：**`MemoryFilter`（反思门）不在这条流水线里**，它在调用方 `filterRetrievedMemories` 里紧接 `Retrieve` 之后执行（见 4.3.3 末尾）。这是有意的分层——`Store` 只管"召回 + 重排 + 截断"，"要不要注入、注入多少"留给请求路径。

#### 4.3.3 源码精读

**(a) 归一化与扩大召回**

[retrieveSearchTopK](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/memory/milvus_store_retrieve.go#L94-L103) 决定向量检索拉多少候选：

```go
searchTopK := limit * 4
if hybridSearch { searchTopK = limit * 8 }
if searchTopK < 20 { searchTopK = 20 }
```

混合重排要"先宽后严"——先拉一个比最终 `limit` 大得多的候选池（默认 8 倍），给 BM25 / n-gram 留出翻盘空间。

**(b) 向量检索本身**

[searchMemoryVectors](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/memory/milvus_store_retrieve.go#L105-L134) 用 Milvus 的 HNSW 索引（`NewIndexHNSWSearchParam(64)`，ef=64）做**余弦**检索，取 `id/content/memory_type/metadata` 四个字段，并套上重试退避。用户隔离在这一步就生效：过滤表达式由 [retrieveFilterExpr](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/memory/milvus_store_retrieve.go#L86-L92) 拼出，核心是 `milvusUserScopeFilter(userID)`。

[milvusUserScopeFilter](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/memory/milvus_filter.go#L18-L20) 只有一行，但注释花了十几行解释安全：

```go
func milvusUserScopeFilter(userID string) string {
    return fmt.Sprintf("user_id == %q", userID)
}
```

`%q` 把 userID 渲染成带转义的 Milvus 字符串字面量，**防止过滤表达式注入（CWE-943）**——否则一个构造出的 `x" || user_id != "y` 就能挣脱引号、泄露或删除别人的记忆（[milvus_filter.go:5-17](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/memory/milvus_filter.go#L5-L17)）。这是记忆子系统用户隔离的**硬防线**（区别于 u9-l3 缓存里的 HMAC 软隔离）。

**(c) 混合重排 hybridRerank**

[finalizeRetrieveResults](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/memory/milvus_store_retrieve.go#L136-L154) 是调度中心：解析候选 → 可选重排 → 可选自适应阈值 → 截断。

[hybridRerank](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/memory/milvus_store_retrieve.go#L303-L345) 把每条候选包成一个"伪 chunk"（复用 `vectorstore.EmbeddedChunk` 类型），交给公共算子打分：

```go
bm25Idx  := vectorstore.NewBM25Index(pseudoChunks)
bm25Scores  := bm25Idx.Score(opts.Query, bm25K1, bm25B)
ngramIdx := vectorstore.NewNgramIndex(pseudoChunks, ngramSize)
ngramScores := ngramIdx.Score(opts.Query)
fused    := vectorstore.FuseScores(vectorScores, bm25Scores, ngramScores, hybridCfg)
```

这里 SR 的设计很巧：**记忆子系统不自己实现 BM25**，而是把候选"伪装"成文档块，直接复用 `pkg/vectorstore` 的混合检索算子（与 u9-l2 文档检索同源）。融合后的 `FinalScore` 回写到候选的 `Score` 上，后续截断逻辑无需感知是否做过重排。

**(d) BM25 的数学**

`BM25Index.Score` 在 [src/semantic-router/pkg/vectorstore/hybrid.go:148-194](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/vectorstore/hybrid.go#L148-L194)。对查询词 \(q\) 在文档 \(d\) 上的得分：

\[
\text{BM25}(d,q)=\sum_{t\in q}\text{IDF}(t)\cdot \frac{\text{tf}(t,d)\cdot(k_1+1)}{\text{tf}(t,d)+k_1\cdot\left(1-b+b\cdot\frac{|d|}{\text{avgDL}}\right)}
\]

其中 \(\text{tf}\) 是词频，\(|d|\) 是文档长度，\(\text{avgDL}\) 是平均文档长度，\(k_1\)（默认 1.2）控制词频饱和，\(b\)（默认 0.75）控制长度归一化强度。IDF 用的是平滑形式（[hybrid.go:169](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/vectorstore/hybrid.go#L169)）：

\[
\text{IDF}(t)=\ln\!\left(\frac{N-n(t)+0.5}{n(t)+0.5}+1\right)
\]

\(N\) 是候选总数，\(n(t)\) 是含词 \(t\) 的候选数。直观理解：**越稀有的词命中越值钱**（IDF 大），**词频有边际递减**（饱和项），**长文档要打折**（长度归一化）。

> n-gram 那一路用的是字符级 3-gram（默认）的 [Jaccard 相似度](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/vectorstore/hybrid.go#L294-L309) \(|A\cap B|/|A\cup B|\)，对拼写变形与子串匹配更友好（[hybrid.go:219-237](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/vectorstore/hybrid.go#L219-L237)）。

**(e) 分数融合 FuseScores**

[FuseScores](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/vectorstore/hybrid.go#L327-L366) 支持两种模式（由 `HybridMode` 选择，默认 `weighted`）：

- **weighted（加权）**：先对**无界**的 BM25 分做 min-max 归一化到 \([0,1]\)（向量余弦与 n-gram Jaccard 本就在 \([0,1]\)），再加权求和（[fuseWeighted, hybrid.go:370-403](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/vectorstore/hybrid.go#L370-L403)）。默认权重向量 0.7 / BM25 0.2 / n-gram 0.1（[hybrid.go:46-48](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/vectorstore/hybrid.go#L46-L48)），即"以向量为主、文本为辅"：

  \[
  \text{final}=w_V\cdot s_{\text{vec}}+w_B\cdot\tilde{s}_{\text{bm25}}+w_N\cdot s_{\text{ngram}}
  \]

- **RRF（倒数排名融合）**：不看绝对分数、只看每路的排名 \(\text{rank}_r\)（[fuseRRF, hybrid.go:406-438](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/vectorstore/hybrid.go#L406-L438)），常数 \(k\) 默认 60：

  \[
  \text{final}(d)=\sum_{r}\frac{1}{k+\text{rank}_r(d)}
  \]

  RRF 的好处是**无须归一化**、对每路分数量纲不敏感，坏处是丢了分数的绝对大小信息。

融合后按 `FinalScore` 降序回写到候选（[milvus_store_retrieve.go:335-343](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/memory/milvus_store_retrieve.go#L335-L343)）。

**(f) 自适应阈值（elbow）**

[adaptiveThresholdElbow](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/memory/milvus_store_retrieve.go#L170-L194) 在按分降序排好的候选里找**相邻最大间隔**：

\[
\text{gap}_i = \text{score}_{i-1}-\text{score}_i,\quad i=1,\dots,n-1
\]

找到最大 \(\text{gap}\)（须 ≥ 0.05 才算"真断层"），把阈值抬到断层中点 \(\text{score}_{i-1}-\text{gap}/2\)；若该值不大于地板 `floor`（基础阈值），就维持地板。效果：候选明显分两拨时，自动只留上面那一拨；候选分数连续平滑时，退化为固定阈值。

**(g) 截断与访问计数**

[applyRetrieveThreshold](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/memory/milvus_store_retrieve.go#L156-L168) 按分降序、跳过 < threshold、最多取 limit 条；命中后 `go m.recordRetrievalBatch(ids)` **异步**累加这些记忆的 `AccessCount`——异步是为了不阻塞请求路径（[finalizeRetrieveResults 末尾, retrieve.go:146-152](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/memory/milvus_store_retrieve.go#L146-L152)）。

**(h) 检索后的反思门 MemoryFilter**

回到请求路径：[filterRetrievedMemories](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/processor_req_body_memory.go#L209-L230) 拿到 `Retrieve` 结果后，用 `memory.NewMemoryFilter(...)` 构造一个过滤器并 `.Filter()`。

[NewMemoryFilter](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/memory/filter_registry.go#L43-L71) 是注册表工厂：按 `config.Memory.Reflection.Algorithm` 选算法，`init()` 注册了 `heuristic`（默认）与 `noop`（原样放行）两种（[filter_registry.go:15-18](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/memory/filter_registry.go#L15-L18)）；未知算法回退 `heuristic` 并告警。`MemoryFilter` 接口本身极简（[types.go:155-163](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/memory/types.go#L155-L163)）：输入 `[]*RetrieveResult`、输出筛过并重排后的子集，返回空表示"一条都不注入"。

默认的 [ReflectionGate](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/memory/reflection.go#L28-L74)（启发式、**不调 LLM、亚毫秒开销**）对召回结果做五步处理（[Filter, reflection.go:79-110](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/memory/reflection.go#L79-L110)）：

1. **拦截**：按正则 `block_patterns` 删恶意内容（默认空，对抗内容主要由上游越狱分类器在写入侧拦截）。
2. **时效衰减**：按记忆年龄做指数衰减（[applyRecencyDecay, reflection.go:137-150](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/memory/reflection.go#L137-L150)）：

   \[
   \text{factor}=\exp\!\left(-\ln 2\cdot\frac{\text{age\_days}}{\text{halfLife}}\right)
   \]

   半衰期默认 30 天——30 天前的记忆分数打对折。注意这会**改写 Score**。
3. **重排**：按衰减后的分数降序。
4. **去重**：用词级 Jaccard 相似度（默认阈值 0.90）剔除近似重复（[dedup, reflection.go:155-176](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/memory/reflection.go#L155-L176)）。
5. **token 预算**：按分降序累加 token 估算（`words × 1.3`），超额即截断（[enforceTokenBudget, reflection.go:180-191](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/memory/reflection.go#L180-L191)），默认预算 2048 token。

多个过滤器可用 [ChainFilter](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/memory/filter_registry.go#L92-L116) 串行组合（任一步清空即短路返回）。

#### 4.3.4 代码实践

**实践目标**：手工走完一次混合检索流水线，把"嵌入 + BM25 重排"具象化。

1. **观察扩大召回**：阅读 [retrieveSearchTopK](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/memory/milvus_store_retrieve.go#L94-L103)，记录 `limit=5`、`HybridSearch=true` 时实际向 Milvus 请求的 `searchTopK` 是多少（答：`max(5*8, 20)=40`）。
2. **跟踪重排数据流**：在 [hybridRerank](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/memory/milvus_store_retrieve.go#L303-L345) 里确认三件事：候选被包成 `pseudoChunks`、原始余弦分进 `vectorScores`、融合后的 `FinalScore` 被回写到 `c.Score`。
3. **对比两种融合**：打开 [fuseWeighted](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/vectorstore/hybrid.go#L370-L403) 与 [fuseRRF](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/vectorstore/hybrid.go#L406-L438)，回答：为什么 weighted 模式必须对 BM25 做 min-max 归一化，而 RRF 不用？（提示：BM25 分数无上界，而 RRF 只用排名。）
4. **定位过滤器**：在 [processor_req_body_memory.go:209-230](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/processor_req_body_memory.go#L209-L230) 确认 `MemoryFilter` 在 `Retrieve` 之后、注入之前执行。

**需要观察的现象 / 预期结果**：

- 混合重排只作用于"向量召回之后"的候选池，**不会再去后端拉新数据**；它只是对已有候选重新打分排序。
- `AdaptiveThreshold` 与 `HybridSearch` 是两个**独立**开关（见 [RetrieveOptions:106-116](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/memory/types.go#L106-L116)），可单独开闭。
- 反思门会**改写** `Score`（时效衰减），所以"Store 返回的分数"与"最终注入决策依据的分数"可能不同。

实际运行 `Retrieve` 需要 CGO 嵌入后端与一个 Milvus 实例，**待本地验证**；若只想验证重排逻辑，可直接构造 `[]*RetrieveResult` 调用 `ReflectionGate.Filter`（纯 Go、无外部依赖）。

#### 4.3.5 小练习与答案

**练习 1**：假设 `limit=5`、候选的融合分依次是 `[0.91, 0.89, 0.88, 0.55, 0.54, 0.53]`，基础 `threshold=0.70`，开启 `AdaptiveThreshold`。最终会返回几条？阈值被抬到多少？

**参考答案**：相邻间隔依次为 `0.02, 0.01, 0.33, 0.01, 0.01`，最大间隔 0.33（≥ 0.05）出现在 index=3 处，自适应阈值 = `0.88 - 0.33/2 = 0.715`，高于地板 0.70，故阈值取 0.715。最终保留分数 ≥ 0.715 的三条：`0.91, 0.89, 0.88`。

**练习 2**：为什么 `hybridRerank` 要把记忆候选"伪装"成 `vectorstore.EmbeddedChunk`，而不是在 `pkg/memory` 里单独写一套 BM25？

**参考答案**：复用。文档检索（u9-l2）与记忆重排需要完全相同的 BM25 / n-gram / 融合算子，把它们放在 `pkg/vectorstore` 作为公共算子，记忆侧只需把候选适配成 `EmbeddedChunk` 即可，避免重复实现与分叉。这也体现了 SR"抽取优先"的约定（见 u1-l2 的 Known Hotspots）。

**练习 3**：`ReflectionGate` 的时效衰减会改写 `Score`。如果同一批记忆在"注入模型"和"写访问计数"两个地方都用 `Score`，会不会互相干扰？

**参考答案**：不会。访问计数在 `Retrieve` 内部、反思门**之前**就已经依据向量（或融合）分数完成截断并异步记录（[finalizeRetrieveResults](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/memory/milvus_store_retrieve.go#L136-L154) 在 Store 内、`Filter` 在 Store 外）。反思门改写的是返回给调用方的 `RetrieveResult.Score`，用于注入时的排序与预算控制，不回写后端，因此两条路径互不影响。

---

## 5. 综合实践

**任务**：为一条假想的用户查询，画出记忆从"检索"到"注入"的完整时序，并标注每一步分数如何变化。

场景：用户问 *"how do I deploy payment-service again?"*（用户隔离 `user_id="u123"`），系统里存着 6 条 procedural 记忆，混合检索 + 自适应阈值 + 反思门都开启。

请完成：

1. **召回**：写出 [retrieveFilterExpr](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/memory/milvus_store_retrieve.go#L86-L92) 生成的 Milvus 过滤表达式（答案应形如 `user_id == "u123" && ...`）。说明为什么这个表达式能阻止别的用户的记忆被召回。
2. **重排**：解释查询里的 *"deploy"* 与 *"payment-service"* 两个词如何通过 [BM25Index.Score](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/vectorstore/hybrid.go#L148-L194) 让"原原本本提到这两个词"的记忆分数被抬高，即使某条记忆的向量余弦分不是最高。
3. **收口**：用一组你自己编造的 6 个候选分数，推演 [adaptiveThresholdElbow](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/memory/milvus_store_retrieve.go#L170-L194) 会把阈值抬到多少、留下几条。
4. **反思门**：假设留下的一条记忆创建于 60 天前（半衰期 30 天），用 [applyRecencyDecay](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/memory/reflection.go#L137-L150) 的公式算出衰减因子（答：\(e^{-\ln 2 \cdot 60/30}=e^{-\ln 4}=0.25\)），说明它最终分数会缩水到原来的 1/4。
5. **产出**：把上述四步画成一张流程图，每步标注"分数来自哪里、被谁改写"。

> 这个练习不需要运行环境，纯源码阅读 + 手工推演即可完成，目的是把"嵌入召回 → 文本重排 → 自适应截断 → 时效衰减"四层串成一条可解释的链路。

## 6. 本讲小结

- `Store` 是记忆子系统的统一接口（增删改查 + `Retrieve` 语义检索 + 生命周期），实现可替换：进程内（测试/POC）、Milvus、Valkey、Qdrant；后端装配在 extproc 的工厂里，调用方只认接口。
- 记忆分三类：**semantic**（事实/偏好）、**procedural**（步骤）、**episodic**（事件）；`Memory` 结构除内容与向量外，还带作用域（`UserID` 为主键）、时效/留存（`AccessCount`/`LastAccessed`/`Importance`）与溯源字段；`Content` 必须自包含上下文。
- 一次 `Retrieve` 是"**向量召回（扩大 topK）→ 可选混合重排（BM25 + n-gram，复用 vectorstore 公共算子，weighted/RRF 融合）→ 可选自适应阈值（找最大间隔 elbow，基础阈值做地板）→ 按阈值与 limit 截断 → 异步累加访问计数**"的流水线。
- 用户隔离有两道：`milvusUserScopeFilter` 用 `%q` 转义把 `user_id` 编进 Milvus 过滤表达式，防 CWE-943 注入（硬防线）；这区别于缓存里的 HMAC 软隔离。
- 检索后的 **MemoryFilter（默认 `ReflectionGate`）** 在请求路径上做"拦截 + 时效衰减 + 去重 + token 预算"，**不调 LLM、亚毫秒**，会改写最终用于注入的分数；算法通过注册表工厂 `NewMemoryFilter` 选择，可链式组合。
- 记忆与缓存的关键区别：记忆存"提炼出的事实条目"、跨会话长期生效；缓存存"完整请求-响应对"、命中即省一次推理。

## 7. 下一步学习建议

- **回到请求主链路看记忆如何被注入**：阅读 `src/semantic-router/pkg/extproc/processor_req_body_memory.go` 全文，看 `Retrieve` 结果如何经 `filterRetrievedMemories` → `injectRetrievedMemories` → `FormatMemoriesAsContext` 写进发给模型的 system 上下文（承接 u5-l1 / u5-l3）。
- **看记忆如何被"写"出来**：阅读 `src/semantic-router/pkg/memory/extractor.go` 与 `consolidation.go`，理解响应阶段的记忆抽取与后台合并（`ConsolidateUser` 的贪心单链聚类）。
- **横向对比三个检索子系统**：把本讲的 `Store`、u9-l2 的 `VectorStoreBackend`、u9-l3 的 `CacheBackend` 放在一起，体会 SR 如何为"文档 / 缓存 / 记忆"三种业务对象各定一套接口、却共用同一批底层检索算子（HNSW、BM25、混合融合）。
- **配置信趣点**：在 [config/runtime_config.go](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/runtime_config.go) 的 `MemoryConfig` 里，尝试设计一组配置（`hybrid_search`、`adaptive_threshold`、`reflection.recency_decay_days`），并预测它们对检索结果的影响。
