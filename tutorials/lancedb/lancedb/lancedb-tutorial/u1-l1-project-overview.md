# 项目总览与定位

## 1. 本讲目标

本讲是整本《LanceDB 学习手册》的第一篇，面向从未接触过该项目的读者。读完本讲，你应当能够：

- 用一句话说清楚 **LanceDB 是什么**、它解决了什么样的「检索」问题。
- 看懂仓库的整体架构：**Rust 核心 + 多语言绑定（Python / Node / Java）+ 本地与远程双后端** 这套分层是怎么组织的。
- 知道 `rust/lancedb/src/lib.rs` 这个「总入口文件」对外暴露了哪些模块，并据此建立后续每一篇讲义将要深入哪里的大地图。
- 理解 LanceDB 与底层 **Lance 列式格式**、以及 **Apache Arrow** 之间的关系。

本讲只读代码、不写代码，重点是把「全局地图」画出来。后面所有的讲义都是在往这张地图里填细节。

## 2. 前置知识

本讲是真正的「从零开始」，但有几个名词最好先有个印象：

- **向量（vector）**：把一段文本、一张图片或一个用户行为，用一个固定长度的浮点数数组来表示。例如 `[0.12, -0.33, 0.88, ...]`，长度可能是 128、768 或 1536。这种数组就是「嵌入向量（embedding）」，它把语义信息编码成了几何坐标。
- **向量检索（vector search）**：给定一个查询向量，在成千上万甚至上亿条存储向量里，找出「最相似」的若干条。相似程度用**距离度量（distance metric）**衡量，比如 L2（欧氏距离）、Cosine（余弦距离）。
- **列式存储（columnar format）**：传统数据库一行一行存数据，列式存储则是一列一列存。做分析或检索时，列式存储只需读取关心的几列，效率更高。
- **Apache Arrow**：一套跨语言的列式内存标准。用 Arrow 表示数据的好处是：Rust、Python、C++ 之间传数据时可以做到**零拷贝（zero-copy）**，不用反复序列化。
- **进程内数据库（in-process / embedded database）**：像 SQLite 那样，不需要单独启动一个数据库服务进程，而是把数据库当作一个库直接嵌入到你的程序里运行。

不需要现在完全理解这些名词，本讲会在用到的地方再解释一遍。

## 3. 本讲源码地图

本讲涉及的关键文件只有三个，它们都是「门面」性质的文件，非常适合作为切入点：

| 文件 | 作用 |
|------|------|
| [README.md](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/README.md) | 项目对外的「自我介绍」，说明 LanceDB 的定位、特性和生态。 |
| [AGENTS.md](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/AGENTS.md) | 给贡献者/AI 助手的项目说明：仓库结构、构建命令、添加新方法的流程。 |
| [rust/lancedb/src/lib.rs](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/lib.rs) | Rust 核心库的**根模块文件**，顶部是项目文档，中部是所有子模块声明，底部是核心类型定义。它是整个项目的「目录页」。 |

> 提示：本讲引用的所有源码链接都指向当前 HEAD `448d5ec2`，你可以直接点击逐行对照阅读。

## 4. 核心概念与源码讲解

本讲对应的最小模块是 `lib`，也就是 Rust 核心库的根模块。为了让初学者好消化，我们把它拆成四个小节：定位、架构、模块地图、核心类型。

### 4.1 LanceDB 是什么：检索型向量数据库

#### 4.1.1 概念说明

一句话定位：**LanceDB 是一个为检索（retrieval）而生的数据库**，尤其擅长向量检索、全文检索以及两者的混合检索。

这里的关键词是「检索」而不是「存储」。很多系统能存向量，但 LanceDB 的设计重心是：**给定一个查询，如何又快又准地把最相关的少数结果捞出来**。这正是 RAG（检索增强生成）、推荐系统、语义搜索、多模态搜索这类 AI 应用的核心需求。

`AGENTS.md` 开篇第一句就点明了这个定位：

> LanceDB is a database designed for retrieval, including vector, full-text, and hybrid search.

它接着说：**LanceDB 是 Lance 的一层封装（a wrapper around Lance）**。这句话很重要——LanceDB 本身并不从零实现存储格式和算子，而是站在底层 **Lance 列式格式**的肩膀上，提供面向检索的数据库级抽象（连接、表、查询、索引、生命周期管理）。

读到这里你可能有个疑问：Lance 又是什么？可以把它理解为「专为机器学习和大规模分析设计的列式存储格式（类似 Parquet 但更适合 ML 工作负载，支持随机读写、版本管理、零拷贝）」。LanceDB 把它包装成了一个「数据库」，并加上了检索所需的索引和查询能力。

#### 4.1.2 核心流程

从用户视角，LanceDB 的典型使用链路非常短：

```
连接数据库 (connect)
   └─> 建表 / 打开表 (create_table / open_table)
          └─> 写入数据（含向量列）
          └─> 建立索引 (create_index) —— 加速后续检索
          └─> 查询 (query: 向量搜索 / 全文搜索 / SQL 过滤)
          └─> 维护 (optimize / 版本管理 / 增删改)
```

本讲只让你「看到」这条链路，每一环的源码细节会在后续讲义（u2 连接与表、u3 查询与搜索、u4 索引、u5 生命周期）里展开。

#### 4.1.3 源码精读

Rust 根模块文件顶部的文档注释，给出了官方对自身的精炼描述：

[rust/lancedb/src/lib.rs:4-5](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/lib.rs#L4-L5) —— 「LanceDB 是一个带持久化存储的开源向量检索数据库，极大简化了嵌入向量的检索、过滤与管理」。

紧接着的 `7~16` 行用要点列出了它的能力清单：

[rust/lancedb/src/lib.rs:7-16](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/lib.rs#L7-L16) —— 关键特性：生产级向量检索、多模态数据存储、向量/全文/SQL 三种检索、Rust/Python/JS 原生支持、零拷贝与自动版本管理、GPU 建索引（仅 Python）。

把这段对照 README 的措辞看，你会发现两份文档强调的是同一组能力：**快速向量检索、综合检索能力、多模态、零拷贝 + 自动版本管理**。README 的提法更营销化（"Multimodal AI Lakehouse"），lib.rs 的提法更工程化，但内核一致：

[README.md:16-23](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/README.md#L16-L23) —— README 顶部标题与定位段：「多模态 AI 湖仓」「构建在 Lance 列式格式之上」。

#### 4.1.4 代码实践

> 本讲的所有实践都是**源码阅读型实践**，不要求你立刻编译运行。

**实践目标**：用自己的话把 LanceDB 与传统数据库的区别写下来，避免背诵术语。

**操作步骤**：

1. 打开本讲的三个源码链接，重点读 `lib.rs:4-16` 的特性列表。
2. 准备一张白纸或一个文本文件，画一个两列对照表，左列写「传统关系型数据库（如 MySQL）」，右列写「LanceDB」。
3. 从以下角度各写一条：
   - 数据的核心形态（行/表 vs 向量 + 元数据 + 多模态 blob）
   - 主要查询方式（SQL 精确匹配 vs 相似度近似检索 + 全文检索）
   - 是否需要独立服务进程（MySQL 需要 vs LanceDB 可进程内嵌入）

**需要观察的现象**：你会发现最大的区别不在「能不能存」，而在「**默认的查询假设不同**」——传统数据库假设你要的是精确命中的行，LanceDB 假设你要的是「按相似度排序的 top-K 近似结果」。

**预期结果**：你能写出一段 3～5 句的话，不依赖原文措辞，说明「为什么一个做 AI 应用的团队会需要 LanceDB 这类东西」。

#### 4.1.5 小练习与答案

**练习 1**：LanceDB 官方文档说它是「a wrapper around Lance」。这里的 Lance 指的是什么？
> **参考答案**：Lance 是底层列式存储格式（一种面向 ML/分析工作负载、支持随机读写与版本管理的格式）。LanceDB 并不从零造存储，而是在 Lance 之上提供数据库级的检索抽象（连接、表、索引、查询）。

**练习 2**：README 把 LanceDB 称作「Multimodal AI Lakehouse」，而 lib.rs 把它称作「vector-search database」。这两种说法矛盾吗？
> **参考答案**：不矛盾，只是侧重点不同。README 面向用户、强调商业愿景（多模态数据湖仓）；lib.rs 面向开发者、强调技术内核（向量检索是核心能力）。多模态数据最终也要靠向量检索来「用」。

---

### 4.2 整体架构：核心 + 绑定 + 双后端

#### 4.2.1 概念说明

LanceDB 的架构可以用一句话概括：**一个 Rust 核心，套上几层薄薄的「语言绑定」外套，底下跑在本地或云端两种后端上**。

这里有两层「分叉」，理解了它们就理解了整个项目结构：

- **横向分叉（语言绑定）**：真正干活的是 Rust 核心；Python、Node/TypeScript、Java 只是「翻译层」，把各自语言的调用翻译成对 Rust 核心的调用。这样做的好处是：**核心逻辑只写一遍**，三种语言共享同一套语义，行为一致、维护成本低。
- **纵向分叉（运行后端）**：同一个核心既能**进程内本地运行**（数据存在本地文件系统或对象存储，像 SQLite 一样嵌入你的程序），也能**连接远程的 LanceDB Cloud**（数据托管在云端，通过 HTTP 访问）。两种后端共用同一套 API，用户代码几乎不用改。

#### 4.2.2 核心流程

把架构画成层次图：

```
        你的应用代码
            │
   ┌────────┼─────────┐
  Python  Node/TS   Java      ← 多语言绑定（薄翻译层）
   (PyO3) (napi-rs) (JNI)
            │
      ┌─────┴─────┐
      │  Rust 核心 │  rust/lancedb/src/*.rs   ← 真正的实现
      └─────┬─────┘
            │
   ┌────────┴────────┐
 本地后端          远程后端
 (Lance Dataset)  (LanceDB Cloud, HTTP)   ← 双后端
   本地文件/        db://
   S3/GS/Azure
```

读这张图的方式是「自上而下」：用户在任何一种语言里调用 API → 绑定层翻译 → 进入 Rust 核心 → 核心根据 URI 决定走本地 Lance Dataset 还是远程 HTTP。

> 名词解释：
> - **PyO3**：让 Rust 代码被 Python 调用的工具链。
> - **napi-rs**：让 Rust 代码被 Node.js 调用的工具链。
> - **Lance Dataset**：Lance 格式的数据集对象，本地后端实际读写的就是它。

#### 4.2.3 源码精读

`AGENTS.md` 的开篇两段正是上面这张架构图的文字版：

[AGENTS.md:1-5](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/AGENTS.md#L1-L5) —— 明确写出：LanceDB 是检索型数据库、是 Lance 的封装、有本地（进程内，类比 SQLite）和远程（对接 LanceDB Cloud）两个后端、核心用 Rust 编写、提供 Python/TypeScript/Java 绑定。

紧接着的「Project layout」列出了四个目录与它们的对应关系，这是你浏览仓库的导航：

[AGENTS.md:7-12](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/AGENTS.md#L7-L12) —— 仓库布局：`rust/lancedb`（核心实现）、`python`（PyO3 绑定）、`nodejs`（napi-rs 绑定）、`java`（Java 绑定）。

「Example plan: adding a new method on Table」一节最能体现「核心先行、绑定随后」的协作方式——添加一个新功能要先动 Rust 核心，再依次加 Python 和 TypeScript 绑定：

[AGENTS.md:69-75](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/AGENTS.md#L69-L75) —— 添加新方法的总体顺序：先加到 Rust 核心，再暴露到 Python 和 TypeScript 绑定；本地表与远程表各自实现，远程表走 HTTP、需开启 `remote` feature。

lib.rs 顶部文档也呼应了「本地 / 云端」的 URI 区分：

[rust/lancedb/src/lib.rs:47-53](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/lib.rs#L47-L53) —— 数据库路径的三种形式：本地路径 `/path/to/db`、对象存储 `s3://` / `gs://`、Lance 云 `db://`。前两者属本地后端，`db://` 属远程后端。

#### 4.2.4 代码实践

**实践目标**：画出 Rust 核心 / Python / Node / Java 之间的依赖关系图，确认你理解了「谁是核心、谁是外壳」。

**操作步骤**：

1. 在仓库根目录浏览四个子目录：`rust/`、`python/`、`nodejs/`、`java/`（你可以用编辑器或 `ls` 命令）。
2. 在 `python/`、`nodejs/` 目录下分别找一找指向 Rust 核心的线索（例如 Python 的 `python/src/*.rs` 用 PyO3、Node 的 `nodejs/src/*.rs` 用 napi-rs）。
3. 画出一张依赖图：箭头从「绑定」指向「Rust 核心」，再从「Rust 核心」分叉到「本地后端」和「远程后端」。

**需要观察的现象**：你会看到三个绑定目录里**都有各自的 `src/` 且包含 Rust 文件**——这印证了「绑定层本身也是用 Rust 写的，只不过用了不同的 FFI 框架（PyO3 / napi-rs）」。

**预期结果**：得到一张清晰的架构图，标注出「核心实现唯一、绑定层有三套、后端有两种」。

> 待本地验证：如果你本地装好了 Rust 工具链，可以试着在仓库根目录运行 `cargo metadata --no-deps --format-version=1`（只读命令），观察 workspace 里有几个 crate，验证「核心 + 绑定」的多 crate 结构。

#### 4.2.5 小练习与答案

**练习 1**：为什么 LanceDB 选择「一个 Rust 核心 + 多语言薄绑定」，而不是每种语言各写一套？
> **参考答案**：核心逻辑只实现一次，三种语言通过薄翻译层共享同一套语义；修 bug、加功能只需改一处，行为天然一致；同时 Rust 提供高性能与内存安全。

**练习 2**：本地后端和远程后端最大的区别是什么？用户代码需要因此大改吗？
> **参考答案**：本地后端进程内直接读写 Lance Dataset（数据在你自己的文件系统/对象存储里）；远程后端通过 HTTP 访问 LanceDB Cloud（数据托管在云端）。因为两者共用同一套 API，用户代码基本不用改，主要差异只是连接时用的 URI（`db://` vs 本地路径）和是否需要 `api_key`。

---

### 4.3 模块地图：lib.rs 里的子模块全景

#### 4.3.1 概念说明

`rust/lancedb/src/lib.rs` 是整个 Rust 核心的「目录页」。它通过一串 `pub mod xxx;` 声明，把核心拆成若干个子模块，每个子模块对应一个目录或文件。看懂这张模块清单，就等于拿到了后续所有讲义的「索引」。

本节的目标不是讲清每个模块的内部细节，而是让你**建立全局印象**：知道哪个主题对应哪个模块、会在第几单元深入。

#### 4.3.2 核心流程

我们可以把这些模块按「用户使用链路」归类成五组：

| 分组 | 模块 | 对应学习单元 | 一句话作用 |
|------|------|------------|-----------|
| 连接 | `connection` | u2 | 连接数据库的入口与 builder |
| 表与数据 | `table`、`data`、`arrow`、`blob` | u2 / u5 | 表抽象、数据读写、Arrow 模型、大对象 |
| 查询 | `query`、`expr`、`rerankers` | u3 | 查询构建、SQL/过滤表达式、结果重排 |
| 索引 | `index` | u4 | 标量与向量索引的创建与配置 |
| 数据库管理 | `database`、`io`、`remote` | u6 | 表集合/命名空间、存储抽象、远程后端 |
| 扩展与工程 | `embeddings`、`dataloader`、`error`、`utils`、`ipc` | u8 / 贯穿 | 嵌入函数、训练数据加载、错误类型等 |

#### 4.3.3 源码精读

lib.rs 中部的 `pub mod` 声明就是这份地图的「原始数据」：

[rust/lancedb/src/lib.rs:165-186](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/lib.rs#L165-L186) —— 全部子模块声明。注意其中两个有条件编译标记：`polars`（177 行，`polars_arrow_convertors` 仅在该 feature 开启时编译）和 `remote`（180 行，`remote` 模块仅在该 feature 开启时编译）。

这两个条件编译很关键——它们印证了「远程后端是可选 feature」的设计。`remote` 模块只在 `--features remote` 时才会被编进核心，否则本地用户完全不需要背负 HTTP 客户端等远程依赖。这一点在下一讲（u1-l2 仓库与构建）会重点展开。

紧随其后的 `use` 与 `pub use` 行，把最常用的几个类型从子模块「提升」到 crate 根，方便用户直接 `lancedb::Table` 这样用：

[rust/lancedb/src/lib.rs:188-197](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/lib.rs#L188-L197) —— re-export：`Connection`、`Table`、`Error`/`Result`、`blob` 宏等都从这里直接对外暴露。

#### 4.3.4 代码实践

**实践目标**：把 lib.rs 的模块清单变成你自己的「学习地图」。

**操作步骤**：

1. 打开 [lib.rs 模块声明](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/lib.rs#L165-L186)。
2. 复制上面的五组分类表。
3. 对每个模块，在仓库里找到它对应的文件或目录（例如 `table` → `rust/lancedb/src/table.rs` 与 `rust/lancedb/src/table/` 目录），简单瞄一眼它顶部的文档注释，猜猜它是干嘛的。

**需要观察的现象**：你会发现大多数模块既有 `xxx.rs` 又有同名的 `xxx/` 目录——这是 Rust 2018 之后的「模块文件 + 子模块目录」并存写法，`xxx.rs` 放该模块自身的代码，`xxx/` 放它的子模块。

**预期结果**：得到一张标注了「模块名 → 文件位置 → 所属学习单元」的速查表，作为后续阅读的导航。

#### 4.3.5 小练习与答案

**练习 1**：lib.rs 里 `remote` 模块前面为什么有 `#[cfg(feature = "remote")]`？
> **参考答案**：表示这个模块只有在开启 `remote` feature 时才会被编译。远程后端依赖 HTTP 客户端等额外组件，作为可选 feature 可以让纯本地用户不必引入这些依赖、保持核心精简。

**练习 2**：为什么 `Table`、`Connection` 这些类型要在 lib.rs 里再 `pub use` 一次？
> **参考答案**：为了缩短用户的导入路径。没有 re-export 的话，用户得写 `lancedb::table::Table`；re-export 之后只需写 `lancedb::Table`，API 更简洁友好。

---

### 4.4 核心类型一览：DistanceType 与 ApproxMode

#### 4.4.1 概念说明

lib.rs 除了声明模块，还在文件底部直接定义了两个贯穿全项目的「枚举（enum）」类型。即便本讲是总览，也值得先认个脸：

- **`DistanceType`（距离度量）**：决定「两个向量有多相似」。L2 是欧氏距离、Cosine 是余弦距离、Dot 是点积、Hamming 是汉明距离（用于二值向量）。这是向量检索最基础的选择。
- **`ApproxMode`（近似模式）**：决定「近似最近邻搜索」在**速度**与**精度（召回率）**之间的权衡，可选 `Fast` / `Normal` / `Accurate`。

这两个类型放在 lib.rs 根部，是因为它们是几乎所有查询都会用到的「公共配置」，放在最高层最方便各模块引用。

#### 4.4.2 核心流程

距离度量的数学含义（行内公式用 `\(...\)`）：

- **L2（欧氏距离）**：两个向量各分量差值的平方和再开方，取值范围 \([0, +\infty)\)。既看方向也看大小。
- **Cosine（余弦距离）**：由两个向量夹角的余弦换算而来，取值范围 \([0, 2]\)，对向量长度（幅度）不敏感，只看方向。
- **Dot（点积）**：两向量对应分量相乘再求和，取值范围 \((-\infty, +\infty)\)。若向量已归一化，则等价于 Cosine。
- **Hamming（汉明距离）**：两个等长二值向量在多少个位置上不同，常用于二值向量检索。

余弦距离对幅度不敏感这一点的直觉：两条方向相同、长度不同的向量，余弦距离会很接近（很相似），但 L2 距离可能很大。这就是为什么「文本语义相似」常用 Cosine——我们关心语义方向，不关心向量的绝对大小。

#### 4.4.3 源码精读

`DistanceType` 枚举定义：

[rust/lancedb/src/lib.rs:199-226](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/lib.rs#L199-L226) —— 四个变体 `L2` / `Cosine` / `Dot` / `Hamming`，每个都带文档注释说明取值范围与含义；`#[default]` 标在 `L2` 上，表示默认距离度量是 L2。

`ApproxMode` 枚举定义：

[rust/lancedb/src/lib.rs:264-279](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/lib.rs#L264-L279) —— `Fast`（低延迟、可能降低召回）/ `Normal`（默认平衡）/ `Accurate`（高召回、延迟更高）；文档注明当前仅影响 RQ 量化类索引（如 IVF_RQ）。

这两个类型都通过 `impl From` 与底层库互转——`DistanceType` 对应 `lance_linalg::distance::DistanceType`，`ApproxMode` 对应 `lance_index::vector::ApproxMode`：

[rust/lancedb/src/lib.rs:228-237](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/lib.rs#L228-L237) —— `DistanceType` 与底层 `lance_linalg` 距离类型的双向转换。这再次体现了「LanceDB 是上层封装」——真正的距离计算在底层 lance-linalg 里，LanceDB 只是定义了一套对外友好的枚举再做映射。

#### 4.4.4 代码实践

**实践目标**：通过阅读枚举定义，初步建立「选择度量 = 选择检索行为」的直觉（详细实验留到 u3-l3）。

**操作步骤**：

1. 打开 [DistanceType 定义](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/lib.rs#L199-L226)，逐条读四个变体的文档注释。
2. 思考一个具体场景：你要做「用户搜索词与商品描述的语义匹配」，你会选哪种距离？为什么？
3. 把你的选择和理由写下来（一句话即可）。

**需要观察的现象**：你会注意到 `Cosine` 的注释特别强调了「不受向量幅度影响」「全零向量未定义」这两点——这是语义检索场景的关键性质。

**预期结果**：能说出「语义相似度场景通常选 Cosine」并解释原因（关心方向而非大小）。这一结论将在 u3-l3 通过实际搜索结果验证。

#### 4.4.5 小练习与答案

**练习 1**：`DistanceType` 的默认值是哪一个？在哪里指定的？
> **参考答案**：默认是 `L2`，通过 `#[default]` 属性指定，见 lib.rs 第 207 行附近（`L2` 变体上方）。

**练习 2**：`ApproxMode` 当前只影响哪一类索引？
> **参考答案**：根据 `ApproxMode` 的文档注释，它目前只影响 RQ 量化类向量索引（如 IVF_RQ），其他索引类型会忽略该设置。

---

## 5. 综合实践

把本讲四个小节串起来，完成一份**「LanceDB 一页纸认知卡片」**：

1. **定位卡片**：用一句话写出 LanceDB 是什么、为谁解决什么问题（依据 4.1）。
2. **架构卡片**：画一张图，包含「应用 → 三种语言绑定 → Rust 核心 → 本地/远程双后端」，并标出每个绑定用的 FFF 框架（PyO3 / napi-rs）（依据 4.2）。
3. **模块卡片**：列出 lib.rs 中你最感兴趣的三个模块，标注它们对应的学习单元（依据 4.3）。
4. **概念卡片**：抄下 `DistanceType` 的四个变体和默认值，并在 Cosine 旁边写一句「为什么语义检索常用它」（依据 4.4）。

完成后，你应当能对着这张卡片，向一个没接触过 LanceDB 的同事讲清楚「这个项目大概是什么、怎么组织的、我接下来要深入哪里」。

## 6. 本讲小结

- LanceDB 是一个**为检索而生**的数据库，核心能力是向量检索、全文检索与混合检索，它建立在底层 **Lance 列式格式**之上。
- 架构是「**一个 Rust 核心 + 三层薄绑定（Python/Node/Java）+ 本地与远程双后端**」；核心逻辑只写一遍，三种语言共享同一套语义。
- `rust/lancedb/src/lib.rs` 是核心的「目录页」，`pub mod` 声明列出了 `connection` / `table` / `query` / `index` / `database` / `io` / `remote` / `embeddings` 等全部子模块，对应后续各单元。
- lib.rs 还在根部定义了两个公共枚举：**`DistanceType`**（L2/Cosine/Dot/Hamming，默认 L2）和 **`ApproxMode`**（Fast/Normal/Accurate），它们会贯穿后续所有查询相关讲义。
- 仓库布局印证了架构：`rust/lancedb`（核心）、`python`（PyO3）、`nodejs`（napi-rs）、`java`（绑定）四个顶层目录各司其职。

## 7. 下一步学习建议

本讲建立的是「全局地图」，接下来应该顺着使用链路往下走：

- **下一讲 u1-l2《仓库结构、技术栈与构建运行》**：动手认识 Cargo workspace 的组织、关键 feature flags（`remote` / `aws` / `gcs` 等），学会用 `cargo check` / `cargo test` 把项目跑起来——这是后续所有源码实践的前置条件。
- **再之后 u1-l3《第一个程序》**：以 `examples/simple.rs` 走通 connect → create_table → create_index → query 的完整链路，把本讲看到的「链路」变成可运行代码。
- 如果你急着想看「检索」本身，可以**先跳读** u3 单元，但建议至少先过完 u1-l2，否则容易在构建环境上卡住。

> 提示：本手册共 8 个单元、30 篇讲义。本讲是 u1 的第 1 篇，也是全手册的起点；每篇讲义顶部都会写明它的 `depends_on`（依赖讲义），你可以按依赖关系自由规划阅读顺序。
