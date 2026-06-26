# 距离度量 DistanceType

## 1. 本讲目标

本讲承接 u3-l2（向量相似度搜索），专门回答一个问题：**向量搜索时，「最近」到底是按什么标准算出来的？**

学完本讲你应该能够：

- 区分 LanceDB 内置的四种距离度量 `L2` / `Cosine` / `Dot` / `Hamming` 的数学含义、取值范围和适用场景；
- 理解为什么「查询距离类型必须与索引训练时的距离类型一致」；
- 解释 `ApproxMode`（`Fast` / `Normal` / `Accurate`）这个速度与精度的权衡开关，以及它只对 RQ 量化索引生效的限制；
- 看懂 `DistanceType` 如何与底层 `lance-linalg` / `lance-index` 做无损互转，从而让上层 API 与底层计算内核解耦。

## 2. 前置知识

本讲默认你已经掌握：

- **向量（vector）**：一组浮点数构成的数组，例如 `[1.0, 0.0]`，常用来表示文本/图像的语义嵌入。
- **向量相似度搜索**：给定一个查询向量，在表中找出与之「最接近」的若干条向量。u3-l2 已讲过 `nearest_to()` 的用法和 `_distance` 结果列。
- **范数（norm）**：向量的「长度」。最常用的是 L2 范数 \(\lVert \vec{x} \rVert_2 = \sqrt{\sum_i x_i^2}\)，它就是向量自身的欧几里得长度。
- **归一化（normalization）**：把向量除以它的范数，使其 L2 范数变为 1，方向不变。

> 关键直觉：**「最近」需要一个度量函数来定义**。度量不同，排出的「最近邻」顺序就可能完全不同。本讲的主角 `DistanceType` 就是这个度量函数的枚举开关。

## 3. 本讲源码地图

本讲只涉及两个最小模块，对应两个文件：

| 文件 | 作用 |
| --- | --- |
| `rust/lancedb/src/lib.rs` | 在 crate 根部定义 `DistanceType` 与 `ApproxMode` 两个公共枚举，并实现它们与底层 `lance-linalg` / `lance-index` 同名类型的双向转换、字符串解析与显示。 |
| `rust/lancedb/src/query.rs` | `VectorQuery` 构建器上的 `distance_type()` 与 `approx_mode()` 方法，把上述枚举写入 `VectorQueryRequest`，最终交给本地/远程后端执行。 |

一句话概括数据流：

```
用户代码
  └─ query().nearest_to(v).distance_type(Cosine)   // query.rs
        └─ VectorQueryRequest.distance_type = Some(Cosine)
              └─ 执行时经 From<DistanceType> 转成 lance-linalg 的 DistanceType  // lib.rs
                    └─ 底层距离计算内核（KNN flat search / ANN 索引）
```

## 4. 核心概念与源码讲解

### 4.1 DistanceType：四种距离度量

#### 4.1.1 概念说明

向量搜索要回答「哪条向量离查询向量最近」，必须先约定一个**距离函数**（越小越近）。LanceDB 把可选的距离函数集中在一个枚举 `DistanceType` 里，提供四种取值：

- **L2（欧几里得距离）**：最经典的「直线距离」，同时受向量的**大小（幅度）**和**方向**影响。
- **Cosine（余弦距离）**：只看两个向量的**夹角**，对幅度不敏感，是文本嵌入检索的常用默认。
- **Dot（点积）**：两个向量的内积，受幅度影响；当向量归一化后，点积等价于余弦相似度。
- **Hamming（汉明距离）**：统计两个向量在多少个位置上取值不同，用于二值/布尔向量。

为什么要把度量做成一个枚举而不是写死？因为不同业务对「相似」的定义不同：图像检索可能更关心绝对差异（L2），而文本语义检索更关心方向是否一致（Cosine）。把它们收敛到一个枚举，既能统一查询接口，又方便序列化传递给远程后端。

#### 4.1.2 核心流程

设两个 \(n\) 维向量 \(\vec{x}\)（库里某一行）与 \(\vec{q}\)（查询向量），各度量定义如下。

**L2（欧几里得距离）**：

\[
d_{L2}(\vec{x}, \vec{q}) = \sqrt{\sum_{i=1}^{n}(x_i - q_i)^2} \in [0, +\infty)
\]

它对幅度敏感：一个很长但方向相同的向量，仍可能因为「远」而排在后面。

**Cosine（余弦距离）**：先算余弦相似度（取值 \([-1,1]\)），再用「1 减相似度」转成距离（取值 \([0,2]\)）：

\[
\cos(\vec{x}, \vec{q}) = \frac{\vec{x}\cdot\vec{q}}{\lVert\vec{x}\rVert_2\,\lVert\vec{q}\rVert_2} \in [-1,1]
\]

\[
d_{\cosine} = 1 - \cos(\vec{x}, \vec{q}) \in [0,2]
\]

分子分母都有范数，所以幅度被「约掉」了，结果只取决于方向。源码注释也特别提醒：**全零向量没有方向，余弦距离未定义**，这类向量不应作为查询或库内向量。

**Dot（点积距离）**：

\[
d_{dot}(\vec{x}, \vec{q}) = \vec{x}\cdot\vec{q} = \sum_{i=1}^{n} x_i q_i \in (-\infty, +\infty)
\]

点积越大越相似。它对幅度敏感；当 \(\vec{x},\vec{q}\) 都归一化时（\(\lVert\cdot\rVert_2=1\)），点积就等于余弦相似度，此时 Dot 与 Cosine 等价。

**Hamming（汉明距离）**：

\[
d_{hamming}(\vec{x}, \vec{q}) = \sum_{i=1}^{n} \mathbb{1}[\,x_i \neq q_i\,]
\]

即「不同位置的个数」，适合二值向量（如哈希指纹）。

> 度量选择速查表：

| 度量 | 取值范围 | 是否受幅度影响 | 典型场景 |
| --- | --- | --- | --- |
| L2 | \([0, +\infty)\) | 是 | 图像/绝对差异、未归一化向量 |
| Cosine | \([0, 2]\) | 否 | 文本语义检索（默认推荐之一） |
| Dot | \((-\infty, +\infty)\) | 是 | 已归一化向量、最大内积搜索（MIPS） |
| Hamming | 整数个数 | 否 | 二值/布尔向量、指纹去重 |

#### 4.1.3 源码精读

`DistanceType` 定义在 crate 根部，默认值是 `L2`：

[rust/lancedb/src/lib.rs:199-226](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/lib.rs#L199-L226) —— `DistanceType` 枚举，标注了 `#[default] L2`，每个变体的文档注释里写明了取值范围与是否受幅度影响（这正是上表的来源）。

关键摘录：

```rust
#[derive(Debug, Copy, Clone, PartialEq, Serialize, Deserialize, Default)]
#[non_exhaustive]
#[serde(rename_all = "lowercase")]
pub enum DistanceType {
    #[default]
    L2,
    Cosine,
    Dot,
    Hamming,
}
```

几点值得注意：

- `#[default] L2`：不调用 `distance_type()` 时，查询默认按 L2 度量。这与 `query.rs` 中 `distance_type()` 的文档「By default `DistanceType::L2` is used」一致。
- `#[serde(rename_all = "lowercase")]`：序列化成小写字符串（`"l2"`、`"cosine"`、`"dot"`、`"hamming"`），这是把查询参数发给远程后端时的线上格式。
- `#[non_exhaustive]`：未来可能新增度量变体，调用方 `match` 时需保留兜底分支。
- `Option<DistanceType>`：在 `VectorQueryRequest` 中是 `Option`，`None` 表示「未显式指定，沿用默认/索引度量」。

`distance_type()` 这个查询构建器方法位于 `VectorQuery`，它会校验「必须与索引训练度量一致」：

[rust/lancedb/src/query.rs:1181-1196](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L1181-L1196) —— `distance_type()` 文档明确写出约束：**若有向量索引，查询度量必须与训练索引时用的度量一致，否则结果无效**；并把值写入 `request.distance_type`。

```rust
/// Note: if there is a vector index then the distance type used MUST match the distance
/// type used to train the vector index.  If this is not done then the results will be invalid.
///
/// By default [`DistanceType::L2`] is used.
pub fn distance_type(mut self, distance_type: DistanceType) -> Self {
    self.request.distance_type = Some(distance_type);
    self
}
```

这两个字段是 `VectorQueryRequest` 的成员：

[rust/lancedb/src/query.rs:936-939](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L936-L939) —— `distance_type` 与 `approx_mode` 在请求结构体中均为 `Option`，`Default` 实现里设为 `None`（见 `query.rs:956-957`）。

源码里有一处现成的用法可参考，它在测试中把一条 L2 默认查询改成了 Cosine + Accurate：

[rust/lancedb/src/query.rs:1552-1582](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L1552-L1582) —— 链式调用 `.distance_type(DistanceType::Cosine).approx_mode(ApproxMode::Accurate)`，随后断言 `request.distance_type == Some(Cosine)`。这正是「构建器只改 request、不执行 IO」模式的标准范例。

#### 4.1.4 代码实践

本实践对应规格中的核心任务：**用同一组向量分别以 L2 和 Cosine 搜索，比较返回顺序差异，并解释为何 Cosine 对幅度不敏感。**

我们构造一个能直观暴露两者差异的小数据集：

- 查询向量 `q = [1.0, 0.0]`
- 行 A `vector = [3.0, 0.0]`：方向与 q 相同，但幅度大很多
- 行 B `vector = [1.0, 1.0]`：与 q 夹角 45°，幅度接近

预期：

- L2 下 \(d(q,A)=2\)、\(d(q,B)=1\)，**B 更近**，顺序 B、A；
- Cosine 下 \(d(q,A)=0\)、\(d(q,B)\approx 0.293\)，**A 更近**，顺序 A、B。

1. **实践目标**：亲手验证「同一组数据，换度量得到完全相反的最近邻顺序」。
2. **操作步骤**：在 `rust/lancedb/examples/` 下新建一个临时示例（或直接在 `simple.rs` 基础上改），运行下方示例代码。
3. **需要观察的现象**：两次搜索打印出的 `id` 顺序不同；`_distance` 列的数值在不同度量下量纲完全不同（L2 是欧氏距离，Cosine 落在 \([0,2]\)）。
4. **预期结果**：L2 打印 `B, A`；Cosine 打印 `A, B`。
5. 运行命令（项目根目录，需开启 `remote` 之外的常规 feature 即可，本例不依赖 remote）：

```shell
cargo run --example <你的示例名>
```

示例代码（非项目原有文件，标注为「示例代码」）：

```rust
// 示例代码：对比 L2 与 Cosine 的最近邻顺序
use std::sync::Arc;
use arrow_array::{
    types::Float32Type, FixedSizeListArray, Float32Array, Int32Array, RecordBatch,
};
use arrow_schema::{DataType, Field, Schema};
use futures::TryStreamExt;
use lancedb::{connect, query::ExecutableQuery, DistanceType};

#[tokio::main]
async fn main() -> lancedb::Result<()> {
    let tmp = tempfile::tempdir().unwrap();
    let db = connect(tmp.path().to_str().unwrap()).execute().await?;

    let dim = 2;
    let schema = Arc::new(Schema::new(vec![
        Field::new("id", DataType::Int32, false),
        Field::new(
            "vector",
            DataType::FixedSizeList(Arc::new(Field::new("item", DataType::Float32, true)), dim),
            true,
        ),
    ]));
    let batch = RecordBatch::try_new(
        schema,
        vec![
            Arc::new(Int32Array::from(vec![0, 1])),                       // id: A=0, B=1
            Arc::new(FixedSizeListArray::from_iter_primitive::<Float32Type, _, _>(
                vec![
                    Some(vec![Some(3.0), Some(0.0)]), // A = [3,0]
                    Some(vec![Some(1.0), Some(1.0)]), // B = [1,1]
                ],
                dim,
            )),
        ],
    )?;
    let table = db.create_table("t", batch).execute().await?;

    let q: &[f32] = &[1.0, 0.0]; // 查询向量

    // 1) 默认 L2
    let l2 = table
        .query()
        .nearest_to(q)?
        .execute()
        .await?
        .try_collect::<Vec<_>>()
        .await?;
    println!("L2 顺序: {:?}", order(&l2));

    // 2) 切换为 Cosine
    let cos = table
        .query()
        .nearest_to(q)?
        .distance_type(DistanceType::Cosine)
        .execute()
        .await?
        .try_collect::<Vec<_>>()
        .await?;
    println!("Cosine 顺序: {:?}", order(&cos));

    Ok(())
}

// 从结果批次里抽出 id 与 _distance，打印「(id, distance)」列表
fn order(batches: &[RecordBatch]) -> Vec<(i32, f32)> {
    batches
        .iter()
        .flat_map(|b| {
            let ids = b.column_by_name("id").unwrap().as_primitive::<arrow_array::types::Int32Type>();
            let dist = b.column_by_name("_distance").unwrap().as_primitive::<arrow_array::types::Float32Type>();
            ids.values().iter().zip(dist.values().iter()).map(|(&i, &d)| (i, d)).collect::<Vec<_>>()
        })
        .collect()
}
```

> 注：本表数据量很小、无向量索引，因此两次都走 flat search（暴力比较），结果是精确的，正好用来对照两种度量。`_distance` 的确切数值**待本地验证**（不同度量量纲不同，不要跨度量直接比较大小）。

#### 4.1.5 小练习与答案

**练习 1**：如果把查询向量 `q` 换成 `[2.0, 0.0]`（方向不变、幅度翻倍），L2 与 Cosine 的顺序会怎么变？

> **答案**：Cosine 顺序不变（方向没变，A 仍排第一）；L2 顺序也不变（A 仍是幅度方向都接近的）。但若把 A 的幅度再放大到很大，L2 会把 A 推得很远——这正是 L2 受幅度影响、Cosine 不受影响的体现。

**练习 2**：为什么文档强调「查询度量必须与索引训练度量一致」？

> **答案**：向量索引（如 IVF-PQ）在训练时按某一种度量把向量划分进空间/聚类，并用该度量做近似比较。若查询用了另一种度量，索引空间划分与查询度量不匹配，返回的「近似最近邻」在数学上就不成立，结果是无效的（而不是慢，而是错）。

**练习 3**：何时应选 `Dot` 而非 `Cosine`？

> **答案**：当你的向量已经预先归一化（L2 范数为 1）时，点积等于余弦相似度，用 Dot 可省掉 Cosine 内部的归一化计算、更快，这类场景常叫「最大内积搜索（MIPS）」；若向量未归一化且你只关心方向，仍应选 Cosine。

---

### 4.2 ApproxMode：速度与精度的权衡

#### 4.2.1 概念说明

向量搜索分两种：

- **Flat search（暴力搜索）**：把查询向量与库中每条向量逐一比较，精确但慢。
- **ANN（近似最近邻）**：用索引（IVF / HNSW / 量化）大幅提速，结果是近似的，可能漏掉少量真正最近邻。

`ApproxMode` 是 ANN 搜索内部的一个「旋钮」，用来在**查询延迟**与**召回率（recall，是否能把真正的最近邻找回来）**之间做权衡：

- `Fast`：优先低延迟，可能降低召回；
- `Normal`（默认）：折中；
- `Accurate`：优先高召回，可能增加延迟。

需要特别注意源码注释里的限制：**当前 `ApproxMode` 只对 RQ 量化索引（如 `IVF_RQ`）生效，其它索引类型会忽略该设置**。所以它不是所有查询的通用开关，而是一个针对特定量化索引的精细控制。

#### 4.2.2 核心流程

近似搜索的质量通常用召回率衡量：

\[
\text{recall@k} = \frac{|\,\text{ANN 返回的 top-}k\, \cap\, \text{真实 top-}k\,|}{k}
\]

`Fast` 倾向更快返回但 recall 偏低，`Accurate` 倾向更高 recall 但更慢。三档对应不同参数组合（RQ 量化精度/搜索宽度等），由底层 `lance-index` 的 `ApproxMode` 解释。

#### 4.2.3 源码精读

`ApproxMode` 同样定义在 crate 根部：

[rust/lancedb/src/lib.rs:264-279](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/lib.rs#L264-L279) —— `ApproxMode` 枚举，`#[default] Normal`，文档注释写明「只影响 RQ 量化索引，其它索引类型忽略此设置」。

```rust
/// Controls the speed / accuracy tradeoff for approximate vector search.
///
/// This currently only affects RQ-quantized vector indexes, such as IVF_RQ.
/// Other index types ignore this setting.
#[derive(Debug, Copy, Clone, PartialEq, Eq, Serialize, Deserialize, Default)]
#[non_exhaustive]
#[serde(rename_all = "lowercase")]
pub enum ApproxMode {
    Fast,
    #[default]
    Normal,
    Accurate,
}
```

查询构建器方法：

[rust/lancedb/src/query.rs:1198-1205](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L1198-L1205) —— `approx_mode()` 把值写入 `request.approx_mode`，同样只是配置、不执行 IO。

`ApproxMode` 的字符串解析支持大小写不敏感（`FAST` 也能解析）：

[rust/lancedb/src/lib.rs:309-325](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/lib.rs#L309-L325) —— `FromStr` 实现先 `to_ascii_lowercase` 再匹配，非法值返回 `Error::InvalidInput`。对应的序列化/解析断言见 [rust/lancedb/src/query.rs:1584-1599](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L1584-L1599)（`"fast"`/`"accurate"` 序列化、`"FAST"` 解析、非法值报错）。

#### 4.2.4 代码实践

`ApproxMode` 的真实效果只有在 RQ 量化索引上才显现，普通小表上调用它不会改变结果。因此这里给一个**源码阅读型实践**：

1. **实践目标**：确认 `approx_mode()` 只修改请求、不触发执行；并理解它在不同索引下是否生效。
2. **操作步骤**：
   - 阅读测试 [rust/lancedb/src/query.rs:1601-1621](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L1601-L1621)：它建表后调用 `.approx_mode(ApproxMode::Fast)`，断言 `query.request.approx_mode == Some(Fast)`，全程没有 `.execute()`——印证「构建器只配置」。
   - 想观察真实效果，需要建一个 `IVF_RQ` 量化索引（详见 u4 单元「索引体系」），分别用 `Fast`/`Accurate` 跑同一查询，记录延迟与召回。
3. **需要观察的现象**：在非 RQ 索引上，无论设 `Fast` 还是 `Accurate`，返回结果应一致（被忽略）；在 `IVF_RQ` 上，`Accurate` 通常 recall 更高、延迟更大。
4. **预期结果**：非 RQ 索引下结果不变；RQ 索引下 recall/延迟出现差异（具体数值**待本地验证**）。

#### 4.2.5 小练习与答案

**练习 1**：在小表上调用 `.approx_mode(ApproxMode::Fast)` 后搜索，结果会变快吗？

> **答案**：不会。该表无 RQ 量化索引（甚至无索引），`ApproxMode` 被忽略，查询仍走 flat search，设置不产生任何效果。

**练习 2**：为什么把 `ApproxMode` 设计成三档枚举而不是一个连续的浮点权重？

> **答案**：底层 RQ 索引只有若干离散的参数组合可用，离散枚举能 1:1 对应到这些组合，避免「连续权重却落到不存在的配置」的歧义；同时 `non_exhaustive` 也保留未来加档的可能。

---

### 4.3 DistanceType 与 lance-linalg 的互转

#### 4.3.1 概念说明

`DistanceType` 是 LanceDB **面向用户**的枚举，但真正做距离计算的内核在另一个 crate：`lance-linalg`（距离）和 `lance-index`（近似模式）。LanceDB 在两者之间放了一层**双向转换**，让上层 API 保持稳定，底层内核可以独立演进。

这种「同名枚举 + From/Into 互转」是 Rust 中常见的解耦手法：LanceDB 不直接依赖 `lance-linalg` 的计算实现，只依赖它的类型；计算逻辑由 `lance`/`lance-index` 负责。

#### 4.3.2 核心流程

```
lancedb::DistanceType  ──From──▶  lance_linalg::distance::DistanceType   (交给距离计算内核)
                       ◀─From──
lancedb::ApproxMode    ──From──▶  lance_index::vector::ApproxMode          (交给 ANN 索引)
                       ◀─From──
```

字符串解析也复用底层：`DistanceType::try_from("l2")` 先交给 `lance-linalg` 解析，再转回上层类型；`Display` 同样委托底层格式化。这样字符串的合法集合（`"l2"`/`"cosine"`/`"dot"`/`"hamming"`）由底层唯一决定，避免两处定义不一致。

#### 4.3.3 源码精读

引入底层类型的 `use`：

[rust/lancedb/src/lib.rs:195-196](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/lib.rs#L195-L196) —— 把底层 `LanceApproxMode`、`LanceDistanceType` 引入作用域并重命名，准备互转。

两个方向的 `From` 实现：

[rust/lancedb/src/lib.rs:228-248](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/lib.rs#L228-L248) —— `From<DistanceType> for LanceDistanceType` 与反向 `From`，逐变体一一映射，是「穷尽 match」，编译器保证新增变体时必须更新此处。

字符串解析与显示委托底层：

[rust/lancedb/src/lib.rs:250-262](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/lib.rs#L250-L262) —— `TryFrom<&str>` 调用 `LanceDistanceType::try_from(value).map(Self::from)`；`Display` 调用 `LanceDistanceType::from(*self).fmt(f)`。可见「哪些字符串合法」「怎么打印」完全由 `lance-linalg` 决定。

`ApproxMode` 同理，两个方向的转换：

[rust/lancedb/src/lib.rs:281-299](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/lib.rs#L281-L299) —— `From<ApproxMode> for LanceApproxMode` 及反向实现，`Fast/Normal/Accurate` 一一映射。

> 对比一个细节：`DistanceType` 的字符串解析委托给 `lance-linalg`（[lib.rs:250-256](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/lib.rs#L250-L256)），而 `ApproxMode` 的字符串解析在 LanceDB 自己实现（[lib.rs:309-325](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/lib.rs#L309-L325)）。这是因为 `lance-linalg` 提供了字符串解析能力，而 `lance-index::ApproxMode` 没有暴露，所以 LanceDB 自己补上——这是「能用底层就用底层」的体现。

#### 4.3.4 代码实践

源码阅读型实践，理解转换的边界：

1. **实践目标**：验证字符串解析的合法集合与大小写行为，加深「字符串合法性由底层决定」的理解。
2. **操作步骤**：
   - 阅读上面 `TryFrom<&str>`（DistanceType）与 `FromStr`（ApproxMode）两段实现。
   - 写一小段测试：分别对 `"l2"`、`"L2"`、`"cosine"`、`"dot"`、`"hamming"` 调用 `DistanceType::try_from`，对 `"fast"`、`"FAST"`、`"accurate"`、`"invalid"` 调用 `ApproxMode::try_from`。
3. **需要观察的现象**：`DistanceType` 的解析是否大小写敏感（取决于 `lance-linalg`）；`ApproxMode` 是大小写不敏感的（因为代码里 `to_ascii_lowercase`）。
4. **预期结果**：`ApproxMode` 的 `"FAST"` 能解析成 `Fast`、`"invalid"` 报 `InvalidInput`（已在 [query.rs:1584-1599](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L1584-L1599) 的测试中验证）；`DistanceType` 的具体大小写行为**待本地验证**（取决于底层实现）。

#### 4.3.5 小练习与答案

**练习 1**：如果 `lance-linalg` 未来新增了一种 `Manhattan` 距离，LanceDB 这边要改哪些地方？

> **答案**：因为 `From`/反向 `From` 是穷尽 match，编译器会强制 LanceDB 在两处 `match` 里加上 `Manhattan => Self::Manhattan` 分支；同时要在 `DistanceType` 枚举里新增 `Manhattan` 变体（注意 `non_exhaustive`，下游可能也需要更新）。

**练习 2**：为什么 `Display` 要委托给底层，而不是自己写 `match`？

> **答案**：让字符串表示只有一处真相源（`lance-linalg`）。若两边都写，一旦不一致就会出现「解析接受、打印输出」对不上的 bug。委托底层等于复用同一套格式化逻辑，天然一致。

---

## 5. 综合实践

把本讲三个最小模块串起来：

1. 建一张包含若干向量的表，**刻意让某些向量方向相同但幅度差异大**（复用 4.1.4 的数据集）。
2. 用 `.bypass_vector_index()`（[query.rs:1207-1217](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L1207-L1217)）保证走精确 flat search。
3. 分别用 `L2`、`Cosine`、`Dot` 三种度量各跑一次 `nearest_to(q)`，打印每条的 `(id, _distance)`。
4. 把所有库向量先做归一化（除以自身范数）重新写入，再用 `Dot` 跑一次，确认此时 `Dot` 的顺序与 `Cosine` 完全一致——从而亲手验证「归一化后 Dot ≡ Cosine」。
5.（选做）尝试调用 `.approx_mode(ApproxMode::Accurate)`，观察在无 RQ 索引时它是否被忽略（结果应不变），印证 4.2 的限制。

完成后，你应该能用一句话向别人解释清楚：「为什么换一个 `DistanceType`，最近邻的顺序就可能颠倒。」

## 6. 本讲小结

- `DistanceType` 枚举（`L2`/`Cosine`/`Dot`/`Hamming`）定义在 crate 根部 [lib.rs:199-226](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/lib.rs#L199-L226)，默认 `L2`，决定「最近」的数学定义；取值范围和是否受幅度影响各不相同。
- `Cosine` 对幅度不敏感（只看方向），`L2`/`Dot` 受幅度影响；归一化后 `Dot` 与 `Cosine` 等价。
- 查询度量通过 `VectorQuery::distance_type()` 设置（[query.rs:1181-1196](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/query.rs#L1181-L1196)），**必须与索引训练度量一致**，否则结果无效。
- `ApproxMode`（`Fast`/`Normal`/`Accurate`，默认 `Normal`）只对 RQ 量化索引（如 `IVF_RQ`）生效，是速度/召回的权衡旋钮，其它索引忽略它。
- 上层枚举与底层 `lance-linalg` / `lance-index` 同名类型通过双向 `From` 互转（[lib.rs:228-299](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/lib.rs#L228-L299)），字符串解析/显示尽可能委托底层，保证单一真相源。

## 7. 下一步学习建议

- 距离类型真正影响性能是在**索引**上。建议进入 **u4 索引体系**：先看 u4-l1（索引总览），再看 u4-l3（IVF 家族）与 u4-l4（量化技术），理解 `IVF_RQ` 量化索引与 `ApproxMode` 的关系。
- 若想了解 `_distance` 之外结果如何与全文检索融合，可继续 **u3-l6 混合搜索与 RRF 重排**。
- 进阶阅读：直接看 `lance-linalg` 与 `lance-index` 中 `DistanceType` / `ApproxMode` 的定义，体会 LanceDB「上层枚举 + 底层内核」的分层设计。
