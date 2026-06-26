# 向量相似度搜索：VectorQuery

> 单元 u3·讲义 l2 ｜ 依赖：u3-l1（查询基础：QueryBase 与 Select）、u1-l4（Arrow 数据模型与向量列表示）

## 1. 本讲目标

上一讲（u3-l1）我们学会了用 `QueryBase` 做一次「不带相似度」的普通扫描——过滤、投影、分页。本讲把查询推进到 LanceDB 最核心的能力：**向量相似度搜索**。

学完本讲，你应该能够：

1. 用 `nearest_to` 把一个普通 `Query` 升级为 `VectorQuery`，并说清楚升级过程里源码做了哪三件事。
2. 看懂 `ExecutableQuery::execute` 为什么返回的是一个「流」，以及怎么消费这个流拿到结果。
3. 解释结果里自动多出来的 `_distance` 列代表什么、为什么结果是按它升序排列的。

本讲只覆盖一个最小模块：**query**（查询模块）。距离度量的细节、IVF/HNSW 索引原理分别留给 u3-l3 与 u4，本讲聚焦「构造 → 执行 → 读结果」这条主线。

## 2. 前置知识

在进入源码前，先用大白话对齐三个概念：

- **向量（vector）**：一段定长浮点数组，比如 `[0.1, 0.2, ..., 0.9]`，用来表示一段文本/一张图片的「语义坐标」。LanceDB 里它存成 Arrow 的 `FixedSizeList<Float32>`（见 u1-l4）。
- **相似度搜索**：给定一个查询向量 \(q\)，在表里找出「离 \(q\) 最近」的若干条向量，并按远近排序。远近用「距离」衡量，**距离越小 = 越相似**。
- **暴力搜索 vs 近似搜索**：
  - 没有索引时，LanceDB 把 \(q\) 和表里每一条向量都算一次距离再排序，称为 **flat search（暴力搜索）**。准确但慢。
  - 有向量索引时（u4 会讲），LanceDB 走 **ANN（Approximate Nearest Neighbor，近似最近邻）**，快但结果近似。

理解一条贯穿全局的设计原则：**LanceDB 的查询对象只配置、不执行**。`nearest_to(...)` 只是改配置，真正读盘发生在 `execute().await`。这套「构建器 + execute」风格从 connect 一路贯穿到查询（见 u2-l1、u1-l3）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `rust/lancedb/src/query.rs` | 查询模块主体：`Query`、`VectorQuery`、`IntoQueryVector`、`ExecutableQuery` 全在这里。本讲主线。 |
| `rust/lancedb/src/table.rs` | `Table::vector_search` 便捷方法，是 `query().nearest_to(...)` 的语法糖。 |
| `rust/lancedb/src/table/query.rs` | `AnyQuery` 枚举，把普通查询和向量查询统一打包交给后端。 |
| `rust/lancedb/examples/simple.rs` | 官方最小示例，含一段完整可运行的向量搜索代码。 |

## 4. 核心概念与源码讲解

### 4.1 从 Query 到 VectorQuery：`nearest_to` 的升级流程

#### 4.1.1 概念说明

回忆 u3-l1：`table.query()` 返回一个 `Query` 对象，它只描述「扫描 + 过滤 + 投影」，**不知道**任何查询向量。要让查询带上「相似度」语义，需要把它「升级」成 `VectorQuery`。

这个升级动作的入口就是 `Query::nearest_to`。它的名字很形象：**寻找离给定向量最近（nearest）的若干条**。升级之后，`VectorQuery` 既能继承 `QueryBase` 的全部方法（`limit`、`select`、`only_if`……），又多了一组向量专属调参方法（`distance_type`、`nprobes`、`refine_factor` 等）。

#### 4.1.2 核心流程

`nearest_to(vector)` 内部做了三件事：

1. **类型转换**：把用户传入的 `vector`（可能是 `Vec<f32>`、`&[f32; 128]` 等）转换成 LanceDB 内部期望的 Arrow 数组（见 4.2）。
2. **升级查询对象**：把普通 `Query` 包装成 `VectorQuery`，把向量塞进它的 `query_vector` 列表。
3. **补默认 limit**：向量搜索「永远有上限」，如果用户没设 `limit`，默认填 `DEFAULT_TOP_K = 10`。

伪代码：

```
fn nearest_to(self, vector):
    vq = self.into_vector()                      // Query → VectorQuery
    qv  = vector.to_query_vector(Float32, ...)   // 用户向量 → Arrow 数组
    vq.request.query_vector.push(qv)             // 记下查询向量
    if vq.request.base.limit is None:
        vq.request.base.limit = 10               // 默认 top-10
    return vq
```

为什么向量搜索一定要有 `limit`？因为「找最近的 N 条」本身就是 top-N 语义，没有上限的「全部按距离排序」既无意义又昂贵。而普通扫描默认无 limit（u3-l1）。

#### 4.1.3 源码精读

先看默认 top-K 常量：

[rust/lancedb/src/query.rs:36-36](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L36-L36) —— 定义 `DEFAULT_TOP_K = 10`，向量搜索未指定 limit 时用它兜底。

核心升级方法：

[rust/lancedb/src/query.rs:858-868](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L858-L868) —— `Query::nearest_to`，完整对应上面三步：`into_vector()` 升级、`to_query_vector` 转换、`push` 进 `query_vector`、补默认 limit。

升级用的辅助方法：

[rust/lancedb/src/query.rs:820-822](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L820-L822) —— `Query::into_vector`，把 `Query` 转成 `VectorQuery`（查询向量为空）。

[rust/lancedb/src/query.rs:987-993](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L987-L993) —— `VectorQuery::new`，把原 `Query` 的 `parent`（表句柄）和 `request` 搬进 `VectorQueryRequest::from_plain_query`。

`VectorQuery` 持有的请求结构比普通查询「大一圈」，在普通 `QueryRequest` 之外多了向量专属字段：

[rust/lancedb/src/query.rs:913-942](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L913-L942) —— `VectorQueryRequest`，注意它把普通查询 `base: QueryRequest` 嵌进自己（组合而非继承），并新增 `query_vector`、`distance_type`、`minimum_nprobes`、`refine_factor`、`use_index` 等向量专属字段。

[rust/lancedb/src/query.rs:944-961](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L944-L961) —— 默认值：`minimum_nprobes = maximum_nprobes = 20`、`use_index = true`（有索引就用 ANN，没索引自动退化为 flat search）。

最后看一个「语法糖」入口，效果完全等价于 `query().nearest_to(...)`：

[rust/lancedb/src/table.rs:1431-1433](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table.rs#L1431-L1433) —— `Table::vector_search`，一行转发到 `query().nearest_to()`，文档明确说二者是同一件事。

#### 4.1.4 代码实践

**目标**：验证 `nearest_to` 会自动补 limit=10。

**步骤**：

1. 打开 `rust/lancedb/src/query.rs`，定位 `nearest_to`（L858）。
2. 阅读它末尾的 `if vector_query.request.base.limit.is_none()` 分支。
3. 在测试 `test_setters_getters`（L1521 起）附近读懂断言：`limit(100).nearest_to(...)` 之后 `query.request.base.limit == 100`。

**预期结果**：确认调用 `limit(100)` **再**调用 `nearest_to` 时，由于 limit 已被设为 100，默认 10 不会覆盖它；只有完全不调 `limit` 时才会填 10。

**待本地验证**：可自行写一个临时断言——`table.query().nearest_to(&[0.1, 0.2])`（不调 limit）后打印 `query.current_request().base.limit`，应看到 `Some(10)`。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `nearest_to` 在 limit 为 `None` 时要主动填 10，而普通 `query()` 不需要？

**参考答案**：普通扫描的语义是「返回所有行」，无上限是合理的；而向量搜索是 top-N 语义，必须有一个 N，否则「找最近的全部并排序」既没有用户意义、在大表上又极其昂贵，所以用 `DEFAULT_TOP_K` 兜底。

**练习 2**：`Table::vector_search(q)` 和 `Table::query().nearest_to(q)` 有什么区别？

**参考答案**：没有区别。`vector_search` 源码就是一行 `self.query().nearest_to(query)`，纯属便捷写法。

---

### 4.2 查询向量的统一输入：`IntoQueryVector`

#### 4.2.1 概念说明

`nearest_to` 的参数类型是 `impl IntoQueryVector`——一个泛型约束，而不是写死成 `Vec<f32>`。这是为了让 Rust 用户不必先手搓 Arrow 数组就能搜索：你可以直接传 `&[1.0; 128]`、`vec![0.1, 0.2]`、甚至一个已有的 `Arc<dyn Array>`。

`IntoQueryVector` trait 只要求实现一个方法 `to_query_vector`：把输入转成 `Arc<dyn Array>`（一个 Arrow 数组）。转换时还会被告知目标 `data_type`（向量列的元素类型，如 Float32）和 `embedding_model_label`（嵌入模型名，用于报错提示）。

> 注意：这里转换出的「数组」通常长度为 1——即「一个查询向量被包成单元素的 FixedSizeList」。这和 u1-l4 讲的「向量列 = FixedSizeList<Float32>」是一致的。

#### 4.2.2 核心流程

```
用户传入 vector（任意 IntoQueryVector 类型）
        │
        ▼
to_query_vector(data_type=Float32, label="default")
        │
        ├── 类型已匹配？ → 直接克隆
        └── 需要转换？   → 用 arrow_cast 转换 或 逐元素收集
        │
        ▼
Arc<dyn Array>（一个 Arrow 数组，通常长度 1）
        │
        ▼
push 进 VectorQueryRequest.query_vector
```

LanceDB 为常见类型提供了 blanket（覆盖式）实现：

| 输入类型 | 说明 |
| --- | --- |
| `&[f32]` / `Vec<f32>` | 最常用：原生 f32 切片/向量 |
| `&[f16]` / `Vec<f16>` | 半精度（启用 fp16 时） |
| `&[f64]` / `Vec<f64>` | 双精度 |
| `&[f32; N]` 等定长数组 | 编译期已知维度，常用 `&[1.0; 128]` |
| `Arc<dyn Array>` / `&dyn Array` | 已是 Arrow 数组 |

#### 4.2.3 源码精读

trait 定义与方法签名：

[rust/lancedb/src/query.rs:131-163](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L131-L163) —— `IntoQueryVector` trait。文档解释了它存在的意义：让用户用原生类型（如 `Vec<f32>`）而不用 Arrow 数组，并为未来「注册嵌入模型后接受字符串输入」留口子。

以最常用的 `&[f32]` 实现为例：

[rust/lancedb/src/query.rs:251-278](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L251-L278) —— `impl IntoQueryVector for &[f32]`，按目标类型分支：要 Float32 就直接收集、要 Float16/Float64 就逐元素转换，其它类型报 `Error::InvalidInput`。

定长数组通过切片转发，避免重复实现：

[rust/lancedb/src/query.rs:320-329](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L320-L329) —— `impl IntoQueryVector for &[f32; N]`，调 `self.as_slice()` 复用 `&[f32]` 的实现。所以示例里 `&[1.0; 128]` 能直接用。

如果想一次提交多个查询向量，用 `add_query_vector`：

[rust/lancedb/src/query.rs:1032-1036](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L1032-L1036) —— `VectorQuery::add_query_vector`，往 `query_vector` 列表里追加第二个、第三个向量。文档说明这「不会比并发发多个查询更快」，结果会多一列 `query_index` 标识某条结果来自第几个查询向量。

#### 4.2.4 代码实践

**目标**：体会 `IntoQueryVector` 的便利。

**步骤**：

1. 打开 `rust/lancedb/examples/simple.rs` 的 `search` 函数。
2. 观察 `nearest_to(&[1.0; 128])`——直接传了一个 128 维的定长数组引用，完全不用碰 Arrow。
3. 思考：如果改成 `nearest_to(vec![1.0f32; 128])` 是否也合法？

**预期结果**：合法。因为 `Vec<f32>` 同样实现了 `IntoQueryVector`（[rust/lancedb/src/query.rs:353-362](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L353-L362)）。

#### 4.2.5 小练习与答案

**练习 1**：如果向量列是 `FixedSizeList<Float16>`，而用户传了 `&[f32]`，会发生什么？

**参考答案**：不会报错。`&[f32]` 的实现会按目标 `data_type = Float16` 分支，逐元素用 `f16::from_f32` 转换，返回一个 `Float16Array`（见 L258-260）。

**练习 2**：为什么 trait 方法里要带一个 `embedding_model_label` 参数？

**参考答案**：纯粹为了报错信息更友好。当类型不匹配时，错误信息能写出「嵌入模型 X 期望类型 Y，但输入是 Z」，方便定位。它不影响正常转换逻辑。

---

### 4.3 执行向量查询：`ExecutableQuery::execute` 与流式结果

#### 4.3.1 概念说明

到目前为止，`nearest_to` 只是在内存里「填表」。要真正读盘、算距离、排序，必须调用 `execute()`。

`execute()` 返回的不是 `Vec<RecordBatch>`，而是一个 **流（stream）**：`SendableRecordBatchStream`。也就是说结果是一段一段吐出来的，而不是一次性全装进内存。这是 LanceDB 面向「可能很大的结果集」的关键设计。

`VectorQuery` 和普通 `Query` 一样实现了 `ExecutableQuery` trait，所以执行接口是统一的——这正体现了 u3-l1 提到的「三种查询对象最终走同一套执行契约」。

#### 4.3.2 核心流程

```
VectorQuery.execute()
        │
        ▼
execute_with_options(QueryExecutionOptions::default())   // L646
        │
        ├── 若 full_text_search.is_some() → 走混合搜索 execute_hybrid（u3-l6 详讲）
        └── 否则 → inner_execute_with_options()           // L1293
                │
                ▼
        create_plan(...)  →  DataFusion ExecutionPlan      // 生成执行计划
                │
                ▼
        execute_plan(plan) → 流                            // 真正跑起来
                │
                ▼
        MaxBatchLengthStream 包装  →  控制每批最多多少行（默认 1024）
                │
        （可选）TimeoutStream 包装  →  超时控制
                │
                ▼
        SendableRecordBatchStream（一段段 RecordBatch）
```

关于「流」的一个关键认知：**每个 `RecordBatch` 的大小和行的顺序不是确定的**（见下方文档原文）。LanceDB 会用多线程并行计算，并对慢消费者施加反压（backpressure）以控制单次查询内存。

消费这个流有两种常见姿势：
- 异步逐批：`while let Some(batch) = stream.next().await { ... }`
- 一次性收集：`.try_collect::<Vec<_>>().await`（来自 `futures::TryStreamExt`）

#### 4.3.3 源码精读

`ExecutableQuery` trait 定义了执行契约：

[rust/lancedb/src/query.rs:633-706](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L633-L706) —— `ExecutableQuery` trait。重点读 `execute` / `execute_with_options` 的文档：明确说结果是 `SendableRecordBatchStream`（一个 `RecordBatch` 流），且**批次大小与行序不确定**，会有 readahead 并对慢消费施加反压。

`execute` 是 `execute_with_options` 的默认参数糖：

[rust/lancedb/src/query.rs:646-648](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L646-L648) —— `execute()` 默认调 `execute_with_options(QueryExecutionOptions::default())`。

`QueryExecutionOptions` 控制执行细节：

[rust/lancedb/src/query.rs:592-627](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L592-L627) —— `QueryExecutionOptions`：`max_batch_length`（每批最大行数，默认 1024）和 `timeout`（超时，默认无）。文档强调 `max_batch_length` 是「上限」，中间也可能吐更小的批以避免内存拷贝。

`VectorQuery` 的执行实现，注意它对「混合搜索」做了特判：

[rust/lancedb/src/query.rs:1334-1346](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L1334-L1346) —— `VectorQuery::execute_with_options`：若同时带 `full_text_search`，转交 `execute_hybrid`（u3-l6）；否则走 `inner_execute_with_options`。

真正干活的地方：

[rust/lancedb/src/query.rs:1293-1306](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L1293-L1306) —— `inner_execute_with_options`：`create_plan` 生成 DataFusion 计划 → `execute_plan` 跑起来 → `MaxBatchLengthStream` 限批 → 可选 `TimeoutStream` 限时。

`create_plan` 会把请求打包成 `AnyQuery` 再交给表后端：

[rust/lancedb/src/query.rs:1329-1332](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L1329-L1332) —— `VectorQuery::create_plan`，把 `VectorQueryRequest` 包成 `AnyQuery::VectorQuery` 交给 `parent.create_plan`。

[rust/lancedb/src/table/query.rs:32-36](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/table/query.rs#L32-L36) —— `AnyQuery` 枚举，两个变体 `Query` / `VectorQuery`，让本地与远程后端用同一套分发逻辑处理两类查询。

官方示例里的消费姿势（`try_collect` 收集成 `Vec<RecordBatch>`）：

[rust/lancedb/examples/simple.rs:125-136](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/examples/simple.rs#L125-L136) —— `search` 函数：`query().limit(2).nearest_to(&[1.0; 128])?.execute().await?.try_collect::<Vec<_>>().await`，一条链走完「查询 → 向量 → 执行 → 收集」。

#### 4.3.4 代码实践

**目标**：用两种姿势消费同一次向量搜索的结果流。

**步骤**：

1. 阅读 simple.rs 的 `search`（L125-136），这是「`try_collect` 一次收齐」姿势。
2. 再看 query.rs 测试 `query_base_methods_on_vector_query`（L2047-2062），它用的是「`while let Some(batch) = stream.next().await`」逐批姿势，并断言第一个 batch 行数为 1、之后流结束。

**预期结果**：两种姿势拿到的是同一份结果，区别只是「要不要把整个结果集物化进内存」。结果集很大时推荐逐批处理。

**待本地验证**：把 simple.rs 的 `search` 改成逐批打印每个 batch 的行数（`batch.num_rows()`），运行 `cargo run --example simple`，观察打印了几批、每批多少行。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `execute()` 不直接返回 `Vec<RecordBatch>`？

**参考答案**：因为结果集可能很大，一次性装进内存会撑爆。返回流可以让调用方边读边处理、并享受内部的反压机制（控制单查询内存）。`try_collect` 只是「流的一个便捷终点」，本质还是流。

**练习 2**：`max_batch_length` 设成 100，结果集有 10000 行，会怎样？

**参考答案**：流会被切成约 100 个 batch、每个不超过 100 行。测试 `test_vector_query_execute_with_options_respects_max_batch_length`（[rust/lancedb/src/query.rs:1920-1937](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L1920-L1937)）正是断言这一点。

---

### 4.4 结果中的 `_distance` 列与排序行为

#### 4.4.1 概念说明

执行一次向量搜索后，返回的 `RecordBatch` 会比原表 schema **多出一列**：`_distance`。它是一个 `Float32` 列，记录「这一行向量与查询向量的距离」。

关键约定：

- **`_distance` 越小 = 越相似**，结果默认按 `_distance` **升序**排列（最近的在最前）。
- 默认距离度量是 **L2（欧氏距离）**；可换 Cosine/Dot/Hamming，但那是 u3-l3 的主题。本讲只需理解「距离越小越近」。
- `_distance` 是「打分列」，和全文检索的 `_score`、混合检索的融合分数是同一类机制。

为什么要把距离暴露成普通列？因为它是 Arrow 的一列，你可以像处理任何数值列一样筛选、投影、排序它。比如用 `distance_range` 只保留某个距离区间的结果。

#### 4.4.2 核心流程

不带索引时（flat search），距离计算的本质是：对表里每一条向量 \(x\)，计算它与查询向量 \(q\) 的距离 \(d(q, x)\)，再按 \(d\) 升序取前 \(K\) 条。以 L2 为例：

\[
d(q, x) = \sqrt{\sum_{i=1}^{n}(q_i - x_i)^2}
\]

（LanceDB 实际返回的距离值与具体度量实现一致；这里只示意 L2 的几何含义。）

```
查询向量 q  ──┐
              ├── 对每行 x 算 d(q,x) ─── 全部候选带 _distance
表里所有行 ──┘
              │
              ▼
        按 _distance 升序排序，取前 K=limit 条
              │
              ▼
        输出 RecordBatch（含原列 + _distance 列）
```

> 行序提醒：**整体**是按 `_distance` 升序的，但「跨 batch 的行序」和「batch 内部的切片粒度」并不保证确定（见 4.3 文档）。所以做断言时通常先 `try_collect` 再整体看顺序。

#### 4.4.3 源码精读

`_distance` 列名来自底层 `lance_index::vector::DIST_COL` 常量（值为 `"_distance"`）：

[rust/lancedb/src/query.rs:21-21](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L21-L21) —— 引入 `DIST_COL`（以及 FTS 的 `SCORE_COL`），二者是两类搜索各自的打分列名。

混合搜索代码里能看到这个列的类型定义（佐证它是 `Float32`、不可空）：

[rust/lancedb/src/query/hybrid.rs:97-97](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query/hybrid.rs#L97-L97) —— `Field::new(DIST_COL, DataType::Float32, false)`，`_distance` 列的 schema 定义。

无索引时走的是 flat search 计划，测试给出了计划名 `KNNFlatSearch`：

[rust/lancedb/src/query.rs:2031-2044](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L2031-L2044) —— `test_create_execute_plan`：对未建索引的表 `nearest_to`，生成的执行计划里包含 `KNNFlatSearch` 节点（暴力搜索）和 `ProjectionExec`（投影出含 `_distance` 的列）。

`distance_range` 可以按 `_distance` 区间过滤结果（左闭右开 `[lower, upper)`）：

[rust/lancedb/src/query.rs:1131-1135](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L1131-L1135) —— `distance_range`，设置 `lower_bound` / `upper_bound`。

测试 `test_distance_range` 直接以 `batch["_distance"]` 取列并断言所有距离落在 `[0.0, 1.0)`：

[rust/lancedb/src/query.rs:2116-2138](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L2116-L2138) —— 取 `_distance` 列、转为 `Float32` 数组，断言每个值都在 `[0.0, 1.0)` 区间。这正好演示了如何读这一列。

#### 4.4.4 代码实践

**目标**：执行 top-5 向量搜索，打印每条的 `_distance`，验证升序。

**步骤**（基于 simple.rs 改造，示例代码）：

```rust
// 示例代码：在 simple.rs 的 search 函数基础上改造
use arrow_array::cast::AsArray;
use arrow_array::types::Float32Type;

let mut stream = table
    .query()
    .limit(5)                       // top-5
    .nearest_to(&[1.0; 128])?       // 128 维查询向量
    .execute()
    .await?;

// 逐批取出 _distance 列并打印
while let Some(batch) = stream.next().await {
    let batch = batch?;
    let dist = batch["_distance"].as_primitive::<Float32Type>();
    for v in dist.values() {
        println!("distance = {}", v);
    }
}
```

**需要观察的现象**：

1. 输出的 `distance` 数值个数应为 5（top-5）。
2. 这些数值应**单调不增**……不，是**单调非降**（升序）：从最小开始递增。因为最近的在前。
3. 由于 simple.rs 里所有向量都是 `[1.0; 128]`、查询向量也是 `[1.0; 128]`，理论上第一条 `_distance` 应为 `0.0`（与自身完全一致）。

**预期结果**：打印 5 个数，第一个约为 `0.0`，后续 ≥ 0 且递增。

**待本地验证**：`cd rust && cargo run --example simple`（或把上述片段放进一个临时 example 运行），核对实际数值。simple.rs 的数据全部相同，所以排序主要受平局处理影响，但第一条 `0.0` 是确定的。

#### 4.4.5 小练习与答案

**练习 1**：结果里的 `_distance` 列，是「越小越相似」还是「越大越相似」？

**参考答案**：越小越相似。`_distance` 是「距离」，距离为 0 表示完全相同；结果按升序返回，最近的在最前。

**练习 2**：不建索引也能向量搜索吗？和建了索引有什么区别？

**参考答案**：能。`nearest_to` 文档（[rust/lancedb/src/query.rs:840-849](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L840-L849)）说明：无索引时做 flat search（暴力、准确、慢）；有索引时做 ANN（近似、快）。`VectorQueryRequest::use_index` 默认 `true`，可用 `bypass_vector_index()` 强制走暴力搜索以获得 ground truth。

**练习 3**：为什么说「整体升序」但不保证「batch 之间顺序确定」？

**参考答案**：因为 LanceDB 多线程并行计算，结果的物化批次边界与并行度相关（见 `execute_with_options` 文档）。要做严格顺序断言，应先 `try_collect` 成单个 `RecordBatch` 再整体检查 `_distance` 列。

---

## 5. 综合实践

把本讲四个模块串起来，完成一个「**对比 flat search 与带 refine 的搜索**」的小任务：

1. **建表**：参考 simple.rs 的 `create_some_records`（[rust/lancedb/examples/simple.rs:60-89](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/examples/simple.rs#L60-L89)），构造一张 1000 行、128 维向量列的表（不建索引）。
2. **flat 搜索**：执行 `table.query().limit(5).nearest_to(&[1.0; 128])?.execute()`，`try_collect` 后记录 5 条 `_distance`，作为「准确答案」。
3. **强制走索引路径**：因为没建索引，`use_index=true` 实际仍退化成 flat（见 4.4）。思考：要让 ANN 真正生效需要先建向量索引（u4-l3），本步可仅用 `explain_plan(true)` 观察计划里是否出现 `KNNFlatSearch` 节点。
4. **读列**：用 `batch["_distance"].as_primitive::<Float32Type>()` 取出距离，验证 flat 结果升序、首条约为 `0.0`。
5. **多查询向量**：再用 `add_query_vector`（[rust/lancedb/src/query.rs:1032-1036](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L1032-L1036)）追加第二个查询向量，`limit(1)`，观察结果多出一列 `query_index`（可参考测试 `test_multiple_query_vectors`，[rust/lancedb/src/query.rs:2140-2168](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L2140-L2168)）。

通过这个任务，你会同时用到：`nearest_to` 升级（4.1）、`IntoQueryVector` 输入（4.2）、流式执行与收集（4.3）、`_distance` 列读取（4.4）。

> 待本地验证：本任务需要可运行的 Rust 环境。可先 `cd rust && cargo test -p lancedb --features remote query::tests::test_create_execute_plan` 确认环境，再改造示例。

## 6. 本讲小结

- 一切向量搜索从 `Query::nearest_to(vector)` 开始：它把普通 `Query` 升级成 `VectorQuery`，做三件事——转换查询向量、塞进 `query_vector`、未设 limit 时补默认 `10`。
- `nearest_to` 接收 `impl IntoQueryVector`，因此可以直接传 `&[f32; N]`、`Vec<f32>` 等原生类型，不必手搓 Arrow 数组；`Table::vector_search` 是等价语法糖。
- 查询对象只配置不执行；真正读盘在 `ExecutableQuery::execute()`，返回的是 `SendableRecordBatchStream`（一段段 `RecordBatch` 的流），批次大小与行序不保证确定，可用 `try_collect` 收齐或逐批消费。
- `VectorQuery::execute_with_options` 对「带全文检索的混合搜索」做了特判，普通向量搜索走 `inner_execute_with_options` → 生成 DataFusion 计划 → 执行 → 限批/限时包装。
- 结果会自动多出 `_distance`（`Float32`）打分列，**越小越相似**、默认按升序返回；默认度量是 L2；无索引时走 flat search（计划含 `KNNFlatSearch`），有索引时走 ANN。
- 请求最终经 `AnyQuery::VectorQuery` 打包交给本地或远程后端，三类查询共享同一套执行契约。

## 7. 下一步学习建议

- **u3-l3 距离度量 DistanceType**：本讲默认用了 L2。L2/Cosine/Dot/Hamming 的语义差异、`ApproxMode` 的精度/速度权衡、以及与 `lance-linalg` 的互转都在下一讲展开。
- **u3-l4 过滤表达式与 SQL**：把 `only_if`（标量预过滤）叠加到向量搜索上，体会 prefilter / postfilter 对结果数量的影响。
- **u4-l3 向量索引 IVF 家族**：本讲的 flat search 只是基线；想理解 ANN 如何用 `nprobes`、`refine_factor` 在召回率与延迟间权衡，进入索引单元。
- **延伸阅读源码**：`rust/lancedb/src/query.rs` 的 `execute_hybrid`（L1219）通向 u3-l6 混合搜索与 RRF 重排，可提前扫一眼。
