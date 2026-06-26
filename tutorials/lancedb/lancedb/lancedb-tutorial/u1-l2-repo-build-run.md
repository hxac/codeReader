# 仓库结构、技术栈与构建运行

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 LanceDB 仓库的 Cargo workspace 由哪几个成员组成、各自承担什么职责。
- 理解 `[workspace.dependencies]` 如何统一管理依赖版本，以及绑定 crate 为什么用 `path` 依赖核心 crate。
- 认识核心 crate 的 feature flag 体系（`remote` / `aws` / `gcs` / `azure` / `oss` / `huggingface` / `dynamodb` / `openai` / `bedrock` / `sentence-transformers` / `fp16kernels` / `polars` / `s3-test`），并解释 `default = []` 的含义。
- 掌握构建与测试的常用命令（`cargo check` / `cargo test` / `cargo tree` 等），并会用 `cargo tree` 观察开启 feature 前后依赖的差异。
- 说明「远程后端为什么是可选 feature」这一设计取舍。

## 2. 前置知识

本讲会用到一些概念，先用大白话解释：

- **Cargo**：Rust 的官方构建工具和包管理器，类似 Python 的 pip + setuptools，但它同时负责编译、依赖解析和测试。
- **crate**：Rust 的一个编译单元/包，可以理解成一个库或一个可执行程序。
- **workspace（工作空间）**：把多个 crate 放在一起统一管理的方式。多个 crate 共享同一份 `Cargo.lock`、同一套依赖版本和编译配置，但各自仍是独立的包。
- **feature flag（特性开关）**：在 `Cargo.toml` 的 `[features]` 段声明，可以条件性地启用/禁用某些依赖或代码路径。比如 LanceDB 把「连云端」的能力做成 `remote` feature，不连云的用户就不必编译相关代码。
- **`default-features`**：每个 crate 可以声明一组默认开启的 feature；写 `default-features = false` 表示不要那组默认值，自己再挑要的。
- **`cdylib`**：一种动态库产物，专门用来被其他语言（Python、Node.js）通过 FFI 加载。LanceDB 的绑定 crate 就编译成 `cdylib`。
- **Lance**：LanceDB 底层的列式存储格式与实现库，是独立的仓库（`lance-format/lance`），通过 git 依赖引入。

如果你对 Rust 基本语法和 Cargo 已经熟悉，可以快速浏览本节。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `Cargo.toml`（仓库根目录） | workspace 总清单，声明成员、workspace 级共享元信息和依赖版本、CI 编译 profile。 |
| `rust/lancedb/Cargo.toml` | 核心库 crate 的清单，集中了所有 feature flag 的定义，是本讲的重点。 |
| `AGENTS.md` | 给开发者的「常用命令 + 项目布局 + 贡献流程」速查，构建命令大多出自这里。 |
| `Makefile` | 仓库根的 Make 脚本，目前只有 `licenses` 目标，用于生成第三方许可证文件。 |
| `python/Cargo.toml` | Python 绑定 crate（`lancedb-python`）清单，编译为 `_lancedb` 动态库。 |
| `nodejs/Cargo.toml` | Node.js 绑定 crate（`lancedb-nodejs`）清单，使用 napi-rs。 |

> 提示：Java 绑定（`java/`）用 Maven 而非 Cargo，**不在本 workspace 内**，本讲先不展开，留到 u7 讲绑定架构时再对比。

## 4. 核心概念与源码讲解

本讲拆成两个最小模块：**workspace**（仓库如何组织）和 **features**（特性开关体系）。

### 4.1 模块一：Cargo workspace 组织

#### 4.1.1 概念说明

LanceDB 不是一个单独的 crate，而是一个「一核多绑定」的多 crate 工程：

- **核心库** `rust/lancedb`：用 Rust 写的全部业务逻辑，对本地 Lance Dataset 和远程 HTTP 后端两套实现都在这里。
- **Python 绑定** `python`：用 PyO3 把核心库包成 Python 能 `import lancedb` 使用的动态库。
- **Node.js 绑定** `nodejs`：用 napi-rs 把核心库包成 Node 原生模块。

这三者被一个 Cargo workspace 统一管理。好处是：版本号、依赖版本、编译 profile 共享一份配置；绑定 crate 通过 `path = "../rust/lancedb"` 直接引用本地核心 crate（开发时改核心代码，绑定立即生效），而不必先发版到 crates.io。

需要注意一个反直觉的点：核心 crate `lancedb` 的版本是 `0.31.0-beta.3`，而 Python 绑定 `lancedb-python` 的版本是 `0.34.0-beta.3`——两者版本号是独立演进的，并不强制对齐。这是因为发布给用户的「Python 包版本」与「Rust 核心库版本」有不同的发布节奏（参见最近的两次 bump 提交）。

#### 4.1.2 核心流程

workspace 的组织可以概括为三件事：

1. **声明成员**：`[workspace]` 段的 `members` 列出所有参与统一编译的 crate 目录。
2. **集中版本**：`[workspace.dependencies]` 段把 Lance、Arrow、DataFusion 等公共依赖的版本只写一次，各 crate 用 `xxx.workspace = true` 引用，避免版本漂移。
3. **集中编译配置**：`[profile.*]` 段定义 CI、dev 等不同场景的编译参数（是否带调试信息、是否增量编译），全 workspace 共享。

绑定 crate（python/nodejs）额外做两件事：

- `crate-type = ["cdylib"]`：产物是动态库，供宿主语言加载。
- `lancedb = { path = "../rust/lancedb", default-features = false }`：本地路径依赖核心库，并关掉核心库的默认 feature（再按需在 `[features]` 里挑）。

#### 4.1.3 源码精读

**根 `Cargo.toml` 的 workspace 声明**：

[Cargo.toml:1-3](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/Cargo.toml#L1-L3) —— `members = ["rust/lancedb", "nodejs", "python"]` 把三个 crate 纳入同一个 workspace，`resolver = "2"` 启用新版依赖解析器（Rust 2021 起默认的特性感知解析，能更准确地处理 feature 合并，避免不同 crate 间 feature 互相污染）。

**workspace 级共享元信息**：

[Cargo.toml:5-13](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/Cargo.toml#L5-L13) —— `edition = "2024"`、`rust-version = "1.91.0"`、license、repository 等都写在 workspace 层，子 crate 用 `edition.workspace = true` 继承，避免重复。

**集中依赖版本**（节选）：

[Cargo.toml:15-66](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/Cargo.toml#L15-L66) —— 这里把 `lance = "=9.0.0-beta.8"`（用 git tag 指向 lance-format 仓库）、`arrow = "58.0.0"`、`datafusion = "53.0.0"`、`object_store = "0.13.2"` 等只声明一次。注意 `lance` 系列用的是 git 依赖（`git = "...lance.git"` + `tag`），因为 Lance 与 LanceDB 紧密协同、需要锁定到特定 tag。

**CI 编译 profile**：

[Cargo.toml:68-80](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/Cargo.toml#L68-L80) —— `[profile.ci]` 继承自 `dev` 但关闭增量编译，并只为依赖（`[profile.ci.package."*"]`）关掉调试信息，目的是让 CI 产物更小、缓存命中率更高。

**绑定 crate 如何引用核心库（以 Python 为例）**：

[python/Cargo.toml:13-15](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/python/Cargo.toml#L13-L15) —— `[lib] name = "_lancedb"` 加 `crate-type = ["cdylib"]`，决定了产物是被 Python 导入的 `_lancedb` 动态库。

[python/Cargo.toml:21](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/python/Cargo.toml#L21) —— `lancedb = { path = "../rust/lancedb", default-features = false }`，路径依赖 + 关闭核心库默认 feature。

Node.js 绑定同理：[nodejs/Cargo.toml:12-13](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/nodejs/Cargo.toml#L12-L13) 也是 `crate-type = ["cdylib"]`，[nodejs/Cargo.toml:24](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/nodejs/Cargo.toml#L24) 同样 `path` 依赖核心库，区别只在于它用 `napi`/`napi-derive` 而非 PyO3。

#### 4.1.4 代码实践

**实践目标**：用 cargo 工具亲手确认 workspace 的成员关系与依赖结构。

**操作步骤**：

1. 在仓库根目录执行 `cargo metadata --no-deps --format-version 1`，查看输出的 `packages` 与 `workspace_members` 字段，确认 workspace 里有哪几个 crate。
2. 执行 `cargo tree -p lancedb --depth 1`，观察核心 crate 的**直接**依赖里有没有 `lance`、`arrow`、`datafusion`、`object_store`。
3. 执行 `cargo tree -p lancedb-python --depth 1`，确认 Python 绑定 crate 的直接依赖里只有 `lancedb`（path 依赖）和 `pyo3` 等，**不直接依赖** `lance`——底层 Lance 是经由核心 crate 间接引入的。

**需要观察的现象**：

- 步骤 1 的输出里 `workspace_members` 应为 3 个。
- 步骤 3 里 Python/Node 绑定自己不直接写 `lance = ...`，说明所有 Lance 调用都被收敛在核心层。

**预期结果**：你能画出「Python/Node 绑定 → lancedb 核心 → lance/arrow/datafusion」的依赖箭头。

> 待本地验证：`cargo metadata`/`cargo tree` 的精确输出格式取决于本地 Cargo 版本；若命令不可用，可改用 `cargo build --workspace` 后查看 `Cargo.lock` 中的 `[[package]]` 条目。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `python/Cargo.toml` 用 `default-features = false` 引用核心库，而不是直接用默认 feature？

**参考答案**：因为核心库 `default = []`（见 4.2），默认没有任何 feature；而 Python 包希望默认就带上 `remote`/`aws`/`gcs` 等能力（见 [python/Cargo.toml:49-52](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/python/Cargo.toml#L49-L52)）。先关掉（空的）默认值、再在自己的 `[features]` 里显式列出想要的能力，可以避免核心层未来若新增默认 feature 时被意外带入。

**练习 2**：核心 crate 版本与 Python 绑定版本不同（0.31 vs 0.34），这会破坏构建吗？

**参考答案**：不会。绑定 crate 通过 `path` 依赖核心 crate，用的是源码而非 crates.io 的版本号；各自 `version` 字段只影响各自发版，不影响 `path` 依赖的解析。

### 4.2 模块二：Feature flags 机制

#### 4.2.1 概念说明

LanceDB 把「可选能力」全部做成 feature flag，核心 crate 默认不开启任何 feature（`default = []`）。这样设计的原因是：LanceDB 要同时服务「本地嵌入式」和「连云端」两种截然不同的部署，还要对接多种对象存储和多种嵌入模型。如果默认全部编译，本地用户会被迫拉入大量用不到的依赖（HTTP 客户端、各家云 SDK、Candle 神经网络库等），编译又慢又重。

feature flag 大致分五类：

| 类别 | flag | 作用 |
| --- | --- | --- |
| 存储后端 | `aws` `gcs` `azure` `oss` `huggingface` `dynamodb` | 启用对应对象存储 / manifest 存储 |
| 远程后端 | `remote` | 启用连 LanceDB Cloud 的 HTTP 客户端 |
| 嵌入集成 | `openai` `bedrock` `sentence-transformers` | 启用对应 EmbeddingFunction 实现 |
| 性能/格式 | `fp16kernels` `polars` | FP16 内核加速 / Polars 互操作 |
| 测试 | `s3-test` | 标记需要真实 S3 的测试 |

#### 4.2.2 核心流程

feature 的「传递」是理解本讲的关键。一个 feature 在 `[features]` 里的写法形如：

```toml
remote = ["dep:reqwest", "dep:http", "lance-namespace-impls/rest", "lance-namespace-impls/rest-adapter"]
```

含义是：当某处启用 `lancedb/remote`，会同时：

- 把 `reqwest`、`http` 这两个**可选依赖**（声明里带 `optional = true` 的）真正引入；
- 给依赖 `lance-namespace-impls` 启用它的 `rest` 和 `rest-adapter` feature（跨 crate 传递）。

绑定 crate 再用自己的 feature 向下透传，例如 `nodejs/Cargo.toml` 写 `remote = ["lancedb/remote"]`，于是 `pnpm build`（默认开 `remote`）最终会一路传到核心 crate。

另外，`[[example]]` 段还能用 `required-features` 限定某个示例只有在某些 feature 开启时才能编译，避免没装对应依赖时编译失败。

#### 4.2.3 源码精读

**核心 crate 的 `[features]` 全貌**：

[rust/lancedb/Cargo.toml:110-144](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/Cargo.toml#L110-L144) —— 注意第 111 行 `default = []`：默认什么都不开。随后逐个定义各类 feature，其中：

- `remote = ["dep:reqwest", "dep:http", ...]`（第 132 行）即「连云端」开关；
- `aws = ["lance/aws", "lance-io/aws", "lance-namespace-impls/dir-aws", "object_store/aws"]`（第 112-117 行）把对象存储能力向下游 crate 透传；
- `sentence-transformers`（第 138-144 行）拉入 `hf-hub`、`candle-*`、`tokenizers` 等本地推理依赖——这也是它默认不开启的原因（体积大）。

**可选依赖长什么样**：

[rust/lancedb/Cargo.toml:68-77](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/Cargo.toml#L68-L77) —— `reqwest = { ..., optional = true }` 和 `http = { ..., optional = true }`，它们只有当 `remote` feature 被启用时才会被编译。这就是 feature 控制「是否拉依赖」的底层机制。

**示例的 required-features**：

[rust/lancedb/Cargo.toml:146-169](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/Cargo.toml#L146-L169) —— 例如 `[[example]] name = "openai"` 配 `required-features = ["openai"]`，意味着只有 `cargo run --example openai --features openai` 才能编译；而 `simple` 示例没有 required-features，任何 feature 组合都能编译。

**lib.rs 中的 feature 文档**：

[rust/lancedb/src/lib.rs:26-35](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/lib.rs#L26-L35) —— 这是面向用户的 crate 文档里对 feature 的说明，可与 `Cargo.toml` 的 `[features]` 对照阅读。

**绑定 crate 默认开启的 feature**：

[python/Cargo.toml:49-52](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/python/Cargo.toml#L49-L52) 与 [nodejs/Cargo.toml:46-49](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/nodejs/Cargo.toml#L46-L49) —— 两者的 `default` 都显式列出了 `remote`、`lancedb/aws`、`lancedb/gcs` 等。也就是说，**装 Python/Node 包的用户默认就拿到了云端 + 各家云存储能力**；而纯用 Rust 核心 crate 的用户则要自己按需开。

#### 4.2.4 代码实践

**实践目标**：亲手开启 `remote` feature，观察它引入了哪些原本不存在的依赖，并理解「远程后端为何可选」。

**操作步骤**：

1. 不开任何 feature，看核心 crate 的依赖树是否含 HTTP 客户端：

   ```bash
   cargo tree -p lancedb --no-default-features | grep -i reqwest || echo "（无 reqwest）"
   ```

2. 开启 `remote`，再看一次：

   ```bash
   cargo tree -p lancedb --features remote | grep -i reqwest
   ```

3. 用差异视角对比（需要 `diff` 与进程替换 `<(...)`）：

   ```bash
   diff <(cargo tree -p lancedb --no-default-features) \
        <(cargo tree -p lancedb --features remote)
   ```

4. 顺便确认编译入口能过（该命令出自 `AGENTS.md`）：

   ```bash
   cargo check --quiet --features remote --tests --examples
   ```

**需要观察的现象**：

- 步骤 1 应当**没有** `reqwest` 输出（它被 `optional = true` 挡住了），打印「（无 reqwest）」。
- 步骤 2 应当**出现** `reqwest v0.12.x`，可能还连带 `http`、`http-body` 等。
- 步骤 3 的 diff 会清晰显示开启 `remote` 后新增的一批依赖子树。

**预期结果**：你会直观看到「开一个 feature = 多编译一整块依赖」，这正是把 `remote` 做成可选 feature 的收益——只做本地向量库的用户完全不用编译 HTTP 客户端栈。

> 待本地验证：`cargo tree` 的过滤结果依赖本地 Cargo 版本和已缓存依赖；首次执行可能触发网络下载，耗时较长。步骤 4 的 `cargo check` 全量编译可能需要数分钟。

#### 4.2.5 小练习与答案

**练习 1**：如果用户只写 `lancedb = "0.31"`（不开任何 feature）并用它连本地目录，能正常用吗？连 `db://` 云端呢？

**参考答案**：连本地目录可以——本地后端不依赖 `remote`。但连 `db://` 云端会编译失败或运行报错，因为云端逻辑（`RemoteDatabase`/`RemoteTable`、HTTP 客户端）只在 `remote` feature 下编译。

**练习 2**：为什么 `aws` feature 写成 `["lance/aws", "lance-io/aws", ...]` 这种「给下游 crate 开 feature」的形式？

**参考答案**：真正的 S3 客户端实现在依赖 `lance`/`lance-io`/`object_store` 里，LanceDB 核心只是把请求转发过去；所以「LanceDB 开 aws」本质是「让底层这些 crate 打开它们的 aws 实现」，feature 沿依赖图向下传递。

## 5. 综合实践

把 workspace 与 feature 两个模块串起来，做一次「读懂并改一行配置」的任务：

1. **读清单**：打开根 `Cargo.toml` 和 `rust/lancedb/Cargo.toml`，回答三个问题——
   - workspace 有几个成员？分别叫什么包名？
   - 核心 crate 默认开几个 feature？
   - `sentence-transformers` feature 会引入哪些以 `candle` 开头的依赖？
2. **观察 feature 传递**：执行

   ```bash
   cargo tree -e features -p lancedb-nodejs | grep -A2 "feature remote"
   ```

   试着追 `nodejs/remote → lancedb/remote → reqwest` 这条传递链，分别在 [nodejs/Cargo.toml:46-49](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/nodejs/Cargo.toml#L46-L49)、[rust/lancedb/Cargo.toml:132](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/Cargo.toml#L132) 里找到对应的那一行。
3. **跑通一个示例**：执行 `cargo run --example simple`（该示例无 required-features，应能直接跑），确认本地构建链路通畅。再尝试 `cargo run --example openai`，观察它在没开 `openai` feature 时报什么错，然后加上 `--features openai` 看错误是否变化。
4. **反思**：用一句话写下「为什么 LanceDB 把远程后端做成可选 feature，而 Python/Node 绑定默认就开」。

> 待本地验证：步骤 3 中 `simple` 示例需要 Rust 工具链与网络（首次编译依赖）；`openai` 示例即便 feature 开启，运行还需要真实 API key，本步只验证「能否编译」即可。

## 6. 本讲小结

- LanceDB 是「一核多绑定」的 Cargo workspace：核心库 `rust/lancedb` + Python/Node 两个绑定 crate，Java 用 Maven 另成体系。
- workspace 用 `[workspace.dependencies]` 集中管理 Lance/Arrow/DataFusion 等版本，绑定 crate 用 `path` + `default-features = false` 引用核心库。
- 核心 crate `default = []`：默认不开启任何 feature；feature 分存储后端、远程、嵌入集成、性能/格式、测试五类。
- `remote` feature 通过 `optional` 依赖 + 跨 crate feature 传递实现「按需编译 HTTP 客户端」，本地用户不必为其买单。
- Python/Node 绑定默认开启 `remote`/`aws`/`gcs` 等，让终端用户开箱即用；Rust 核心用户则自行按需选择。
- 常用命令：`cargo check --features remote`、`cargo test --features remote`、`cargo tree`、`cargo run --example simple`，这些大多记录在 [AGENTS.md](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/AGENTS.md#L14-L20)。

## 7. 下一步学习建议

- 下一讲 **u1-l3 第一个程序** 会真正运行 `examples/simple.rs`，把 `connect → create_table → query` 串起来，建议在本讲把 `cargo run --example simple` 跑通后再进入。
- 想深入 feature 机制可阅读 [Cargo Features 官方文档](https://doc.rust-lang.org/cargo/reference/features.html)。
- 如果对底层 Lance 感兴趣，可先记住 `Cargo.toml` 里 `lance = "=9.0.0-beta.8"` 这条 git 依赖，后续 u6 存储后端会回到这里。
