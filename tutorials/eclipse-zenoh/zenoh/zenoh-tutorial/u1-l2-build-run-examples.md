# 构建、运行与第一个示例

> 承接上一篇《u1-l1 Zenoh 是什么》。上一篇我们已经建立了全局地图：Zenoh 用一套 API 把 Pub/Sub、Store/Query、Compute 统一起来，本仓库是它的 Rust 参考实现，并区分了 `zenoh` / `zenoh-ext`（稳定 API）和 `commons` / `io`（内部实现）。本篇不再重复这些定位，而是动手把项目跑起来：**编译、运行官方示例 `z_pub` / `z_sub`，并读懂它们的主流程**。

## 1. 本讲目标

学完本讲，你应当能够：

1. 用 `cargo` 编译整个 Zenoh workspace，并知道有哪些编译选项（release、features）。
2. 用 `cargo run --example <名字>` 运行官方示例，并能区分「直接用 cargo 跑」和「跑编译好的二进制」两种方式。
3. 读懂 `z_pub.rs` 与 `z_sub.rs` 的主流程，能说清「打开会话 → 声明实体 → 收发数据」这三步。
4. 理解 `examples` 这个 crate 是如何组织的：为什么它对 `zenoh` 依赖了 `default-features = false`、它的 features（`shared-memory` / `unstable`）门控了哪些示例。
5. 通过修改 key expression，亲手验证一次「发布端与订阅端如何匹配」。

## 2. 前置知识

在动手前，先理解三个最基础的概念（后续单元会深入）：

- **cargo workspace**：Zenoh 把几十个 crate 放在同一个 workspace 里统一编译。`examples` 是其中一个成员 crate（见根 `Cargo.toml` 的 `members`）。只要你在仓库根目录运行 cargo 命令，cargo 就能在整个 workspace 里找到它，所以可以直接 `cargo run --example z_pub`。
- **example 与 binary 的区别**：Rust 里 example 是放在 `examples/` 目录下、用 `[[example]]` 声明的小程序，主要用于演示；它和正式发布的 binary 不是一个东西。Zenoh 的 example 既是教学示例，也是测试 Zenoh 功能的工具集。
- **key expression（键表达式）**：可以暂时理解成 Zenoh 的「地址」，例如 `demo/example/zenoh-rs-pub`。它支持通配符：`*` 匹配单层，`**` 匹配多层。发布端往某个 key 写数据，订阅端用一个（可能带通配符的）key 订阅，只要两边的 key **相交（intersect）**，数据就会被送达。这是本篇动手环节的核心机制。

> 名词速查：`Session`（会话，所有 Zenoh 操作的入口）、`Publisher`（发布者）、`Subscriber`（订阅者）、`Sample`（一条数据样本，含 key、负载、kind）。本篇只要建立直觉即可，细节在后续单元展开。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/README.md) | 项目的「Build and run」「Examples」章节，给出编译与运行示例的权威命令。 |
| [examples/README.md](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/examples/README.md) | 示例集的说明文档，逐个介绍每个示例的用途与典型用法。 |
| [examples/Cargo.toml](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/examples/Cargo.toml) | `zenoh-examples` crate 的清单：依赖、features、每个 `[[example]]` 的名字与路径。 |
| [examples/src/lib.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/examples/src/lib.rs) | 定义所有示例共享的 `CommonArgs`，即 `-e`/`--mode`/`--listen` 等公共命令行参数，并把它转成 `Config`。 |
| [examples/examples/z_pub.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/examples/examples/z_pub.rs) | 发布者示例：周期性地往一个 key 写数据。 |
| [examples/examples/z_sub.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/examples/examples/z_sub.rs) | 订阅者示例：监听匹配的 key，打印收到的每一条数据。 |

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**构建说明**、**examples crate 的组织方式**、**z_pub / z_sub 示例主流程**。

### 4.1 构建说明：用 cargo 编译 Zenoh

#### 4.1.1 概念说明

Zenoh 是一个标准的 Rust workspace 项目，所以编译它和编译任何 cargo 项目一样：装好 Rust 工具链，运行 `cargo build`。但有两点需要特别注意：

- Zenoh 体积大、依赖多，**首次编译会比较慢**，并且对 Rust 工具链版本有最低要求。
- Zenoh 通过 **feature 开关**把可选能力（如共享内存 shared-memory）隔离开，默认不一定全部启用。

#### 4.1.2 核心流程

编译与运行的典型步骤：

1. 安装 Cargo + Rust，并保持工具链最新：`rustup update`。
2. 在仓库根目录编译：`cargo build --release --all-targets`。
   - `--release`：开优化，运行性能才有意义（Zenoh 是高性能网络栈，debug 模式性能会差很多）。
   - `--all-targets`：连 example、测试一起编译，这样 `z_pub` / `z_sub` 才会被编出来。
3. 运行示例有两种方式：
   - 用 cargo 直接跑：`cargo run --example z_pub`（在仓库根目录即可，因为 `examples` 是 workspace 成员）。
   - 跑编译好的二进制：`./target/release/examples/z_pub`。
4. 给示例传参数时要加 `--` 分隔，例如 `cargo run --example z_pub -- -h`。

#### 4.1.3 源码精读

README 的「Build and run」章节给出工具链要求与编译命令：

> Zenoh 可以用 Rust stable（>= 1.75.0）编译，但部分依赖可能需要更新的版本。
>
> [README.md:69-78](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/README.md#L69-L78) —— 这段说明了最低 Rust 版本，以及编译命令 `cargo build --release --all-targets`。

README 同时说明 feature 的用法，并以 shared-memory 为例：

> [README.md:80-85](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/README.md#L80-L85) —— 这段说明：要用共享内存，必须在依赖里显式启用 `features = ["shared-memory"]`。本篇用不到共享内存，保持默认即可。

「Examples」章节给出运行示例的权威命令，并提示用 `--` 传参：

> [README.md:89-99](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/README.md#L89-L99) —— 这段说明示例既可用 Cargo 运行，也可从 `target/release/examples` 直接运行，并列出 Pub/Sub 的两条命令 `cargo run --example z_sub` 与 `cargo run --example z_pub`。

> 备注：`examples/README.md` 里也写了「运行预编译产物」的方式，路径写的是 `./target/release/example/<example_name>`（单数），这是该文档的一处笔误；cargo 实际产出的目录是 `target/release/examples/`（复数，见上方 README）。本讲统一采用正确的 `target/release/examples/`。

#### 4.1.4 代码实践

1. **实践目标**：把整个 Zenoh workspace 编译出来，确认示例可被生成。
2. **操作步骤**：
   ```bash
   # 在仓库根目录
   rustup update
   cargo build --release --all-targets
   ```
   编译完成后，查看产物目录里是否有 z_pub：
   ```bash
   ls target/release/examples/ | grep -E 'z_pub|z_sub'
   ```
3. **需要观察的现象**：编译过程会拉取大量依赖；最后 `ls` 能看到 `z_pub` 与 `z_sub` 两个可执行文件。
4. **预期结果**：`target/release/examples/` 下出现名为 `z_pub`、`z_sub` 的可执行文件。
5. **说明**：如果你只想快速看一眼示例行为，也可以跳过手动 `build`，直接用 `cargo run --example z_sub`，cargo 会按需编译。

#### 4.1.5 小练习与答案

- **练习 1**：为什么 README 推荐加 `--release`，而不直接用默认的 debug 构建？
  - **参考答案**：Zenoh 是高性能网络栈，debug 构建不开优化，运行得很慢、吞吐很低；发布/订阅示例在这种性能下体验很差，所以推荐 `--release`。
- **练习 2**：如果只想编译 example 而不跑测试，`--all-targets` 这个参数有什么用？
  - **参考答案**：`--all-targets` 让 cargo 把 example、bench、test 等所有目标都编译出来；不加它的话，example 不一定会被编译，`target/release/examples/` 里可能找不到 `z_pub`。

---

### 4.2 examples crate 的组织方式

#### 4.2.1 概念说明

`examples` 不是一个普通示例文件夹，而是一个正式的 crate，名字叫 **`zenoh-examples`**，并且被纳入了 workspace（在根 `Cargo.toml` 的 `members` 里能看到 `"examples"`）。它有两个职责：

1. **教学**：演示如何用 Rust 写 Zenoh 应用。
2. **工具**：作为实验和测试 Zenoh 功能的工具集（如吞吐测试 `z_pub_thr` / `z_sub_thr`）。

因为它是一个 crate，所以有自己的 `Cargo.toml`、自己的 features，并且用一个 `CommonArgs` 把所有示例都需要的公共命令行参数抽出来复用。

#### 4.2.2 核心流程

示例 crate 的组织逻辑：

1. **清单声明**：在 `examples/Cargo.toml` 里，每个示例都用一段 `[[example]]` 声明 `name` 和 `path`，把 `examples/examples/` 下的某个 `.rs` 文件注册成一个可运行的 example。
2. **公共参数复用**：`examples/src/lib.rs` 定义了 `CommonArgs` 结构体，所有示例都 `use zenoh_examples::CommonArgs;` 把它嵌进自己的参数结构里，从而免费获得 `-e`、`--mode`、`--listen` 等参数。
3. **features 门控**：部分示例（如 `z_pub_shm`）需要 `shared-memory` + `unstable` 两个 feature，用 `required-features` 标注；不开这些 feature，cargo 不会编出对应示例。

#### 4.2.3 源码精读

先看 crate 的基本属性与 features：

> [examples/Cargo.toml:21-32](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/examples/Cargo.toml#L21-L32) —— 这段说明：crate 名是 `zenoh-examples`，`publish = false`（仅内部用，不发布到 crates.io），并定义了三个 feature：`default`（同时启用 zenoh/zenoh-ext 的 default）、`shared-memory`（转发给 `zenoh/shared-memory`）、`unstable`（转发给 `zenoh/unstable`）。

依赖部分值得注意——它对 `zenoh` 用了 `default-features = false`：

> [examples/Cargo.toml:34-43](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/examples/Cargo.toml#L34-L43) —— 这段说明示例依赖 `zenoh` 与 `zenoh-ext`（都关掉了默认 feature），再由 examples 自己的 `default` feature 决定开哪些能力。这是一种「把 feature 开关收拢到自己手里」的常见写法。

`[[example]]` 声明长这样：

> [examples/Cargo.toml:76-83](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/examples/Cargo.toml#L76-L83) —— 这里把 `examples/z_pub.rs` 注册为 example `z_pub`（普通示例），并把 `examples/z_pub_shm.rs` 注册为 `z_pub_shm`，后者用 `required-features = ["shared-memory", "unstable"]` 限定只有开了对应 feature 才编译。这就是为什么不开 `shared-memory` feature 时你跑不了 `z_pub_shm`。

公共参数 `CommonArgs` 定义了所有示例共享的 CLI 选项：

> [examples/src/lib.rs:9-37](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/examples/src/lib.rs#L9-L37) —— 这段定义了 `--config`、`--cfg`、`-m/--mode`（默认 peer）、`-e/--connect`、`-l/--listen`、`--no-multicast-scouting`、`--enable-shm` 等参数。注意 `-e`/`--connect` 就是用来连接到一个 Zenoh 路由器（zenohd）或对端的端点。

`CommonArgs` 还实现了 `From<CommonArgs> for Config`，把这些 CLI 参数翻译成 Zenoh 的 `Config`：

> [examples/src/lib.rs:45-96](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/examples/src/lib.rs#L45-L96) —— 这段把 `--connect` 写进 `connect/endpoints`、`--listen` 写进 `listen/endpoints`、`--no-multicast-scouting` 关闭 `scouting/multicast/enabled` 等等。理解这段后你就明白：示例里的 `-e tcp/localhost:7447` 最终是变成 Config 里的连接端点。

> 与文档呼应：`examples/README.md` 提到，如果在 Docker 里跑 Zenoh 路由器，需要给示例加 `-e tcp/localhost:7447`，因为 Docker 不支持 UDP 多播、scouting 发现机制用不了（见 [examples/README.md:13-15](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/examples/README.md#L13-L15)）。这就是 `CommonArgs::connect` 的典型用途。

#### 4.2.4 代码实践

1. **实践目标**：不运行任何示例，只通过 `--help` 看清示例到底接受哪些参数，并理解它们从哪来。
2. **操作步骤**：
   ```bash
   cargo run --example z_pub -- -h
   cargo run --example z_sub -- -h
   ```
3. **需要观察的现象**：帮助信息里既有 z_pub 自己的参数（`-k/--key`、`-v/--payload` 等），也有来自 `CommonArgs` 的公共参数（`-e`、`--mode`、`--listen`、`--no-multicast-scouting`……）。
4. **预期结果**：你能指出「哪些参数是 z_pub 独有的、哪些是所有示例共享的」，并说出 `CommonArgs` 这个名字。
5. **说明**：clap 的 `#[command(flatten)]` 就是把 `CommonArgs` 展开合并进当前示例的参数表，这就是共享的原理（详见 `z_sub.rs` 里的 `common: CommonArgs` 字段）。

#### 4.2.5 小练习与答案

- **练习 1**：为什么 `examples` 对 `zenoh` 的依赖写成 `default-features = false`，再在自己的 `default` feature 里重新启用？
  - **参考答案**：这样示例 crate 能完全掌控启用了哪些 zenoh 能力（例如只在需要时才开 `shared-memory`/`unstable`），不会被 zenoh 的默认 feature 牵着走，方便精确组合 feature。
- **练习 2**：`z_pub_shm` 为什么不能直接 `cargo run --example z_pub_shm` 跑起来？
  - **参考答案**：它的 `[[example]]` 声明了 `required-features = ["shared-memory", "unstable"]`（见 `examples/Cargo.toml`），不开这两个 feature 时 cargo 根本不会编译它。需要 `cargo run --example z_pub_shm --features shared-memory,unstable`（且 z_pub_shm 属于较高级的示例，本篇不展开）。

---

### 4.3 第一个示例：z_pub 与 z_sub 主流程

#### 4.3.1 概念说明

`z_pub` 和 `z_sub` 是 Zenoh 最经典的 Pub/Sub 示例对。它们演示了一个 Zenoh 应用的标准「三步走」：

1. **打开会话**：`zenoh::open(config).await` 得到一个 `Session`。
2. **声明实体**：发布端 `session.declare_publisher(&key)`，订阅端 `session.declare_subscriber(&key)`。
3. **收发数据**：发布端在循环里 `publisher.put(payload)`；订阅端在循环里 `subscriber.recv_async().await` 拿到 `Sample`。

这两个示例默认是能直接配对的：`z_pub` 默认往 `demo/example/zenoh-rs-pub` 发布，而 `z_sub` 默认订阅 `demo/example/**`（`**` 匹配多层），所以默认配置下两者天然相交、能收到数据。

#### 4.3.2 核心流程

`z_pub` 主流程（伪代码）：

```text
init_log()                      # 初始化日志
(config, key, payload, ...) = parse_args()
session  = zenoh::open(config).await          # 1. 打开会话
publisher = session.declare_publisher(&key).await   # 2. 声明发布者
loop {
    sleep(1s)
    buf = format!("[{idx}] {payload}")
    publisher.put(buf)                          # 3. 发数据
        .encoding(TEXT_PLAIN)
        .await
}
```

`z_sub` 主流程（伪代码）：

```text
init_log()
(config, key) = parse_args()
session    = zenoh::open(config).await          # 1. 打开会话
subscriber = session.declare_subscriber(&key).await   # 2. 声明订阅者
loop {
    sample = subscriber.recv_async().await       # 3. 收数据（阻塞等待）
    print(sample.kind(), sample.key_expr(), sample.payload())
}
```

两边的对称结构非常清晰：都是「open → declare → loop 收发」。

#### 4.3.3 源码精读

**z_pub 的入口与三步走：**

> [examples/examples/z_pub.rs:14-18](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/examples/examples/z_pub.rs#L14-L18) —— 导入：`clap::Parser`（命令行解析）、`zenoh` 的 `Encoding` / `KeyExpr` / `Config`，以及共享的 `CommonArgs`。

> [examples/examples/z_pub.rs:20-31](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/examples/examples/z_pub.rs#L20-L31) —— 这是「三步走」的前两步：`#[tokio::main]` 把 `main` 变成异步入口；`zenoh::init_log_from_env_or("error")` 初始化日志（默认 error 级别）；`zenoh::open(config).await` 打开会话；`session.declare_publisher(&key_expr).await` 声明发布者。

> [examples/examples/z_pub.rs:48-60](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/examples/examples/z_pub.rs#L48-L60) —— 这是「第三步」：一个 `for idx in 0..u32::MAX` 的无限循环，每秒构造一条形如 `[  0] Pub from Rust!` 的字符串，用 `publisher.put(buf).encoding(Encoding::TEXT_PLAIN)...await` 发出。注释还提示：要看如何序列化不同类型的消息，请参考 `z_bytes.rs`。

**z_pub 的命令行参数（默认 key 在这里）：**

> [examples/examples/z_pub.rs:63-79](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/examples/examples/z_pub.rs#L63-L79) —— 用 clap derive 定义 `Args`：`-k/--key` 默认 `demo/example/zenoh-rs-pub`，`-v/--payload` 默认 `Pub from Rust!`，还有 `--attach`（附件）、`--add_matching_listener`（匹配监听），并通过 `#[command(flatten)] common: CommonArgs` 嵌入公共参数。

**z_sub 的入口与收数循环：**

> [examples/examples/z_sub.rs:18-29](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/examples/examples/z_sub.rs#L18-L29) —— 同样是「open → declare」：打开会话，再用 `session.declare_subscriber(&key_expr).await` 声明订阅者。

> [examples/examples/z_sub.rs:31-50](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/examples/examples/z_sub.rs#L31-L50) —— 这是订阅端的核心：`while let Ok(sample) = subscriber.recv_async().await` 持续阻塞等待下一条样本；拿到后用 `sample.payload().try_to_string()` 把负载还原成字符串，再打印 `sample.kind()`（Put/Delete）、`sample.key_expr()` 和负载内容，并可选地打印附件（attachment）。

**z_sub 的默认 key 与公共参数：**

> [examples/examples/z_sub.rs:53-60](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/examples/examples/z_sub.rs#L53-L60) —— `SubArgs` 的 `-k/--key` 默认值是 `demo/example/**`。正因为这个 `**` 通配符，它才能匹配到 `z_pub` 默认发布的 `demo/example/zenoh-rs-pub`。

> 小结一句：两边都没有写任何「找对方」的代码，匹配完全由 key expression 的相交关系决定，Zenoh 自动把发布的数据路由给匹配的订阅者。这正是 Zenoh「声明式」用法的特点。

#### 4.3.4 代码实践

这是本讲的**主实践任务**，请务必在两个终端里完成。

1. **实践目标**：亲手跑通一次 Pub/Sub，然后通过修改 key 验证「匹配」机制。
2. **操作步骤**：
   - 终端 A（订阅端）：
     ```bash
     cargo run --example z_sub
     ```
   - 终端 B（发布端）：
     ```bash
     cargo run --example z_pub
     ```
   - 观察 z_sub 收到数据后，**停止 z_pub（Ctrl-C），修改它的 key**：
     ```bash
     cargo run --example z_pub -- -k demo/hello
     ```
   - 此时再观察 z_sub 是否还能收到数据；如果不能，**给 z_sub 也指定一个能匹配 `demo/hello` 的 key**，例如：
     ```bash
     cargo run --example z_sub -- -k 'demo/**'
     ```
3. **需要观察的现象**：
   - 第一次两端用默认 key 时，z_sub 持续打印形如 `>> [Subscriber] Received Put ('demo/example/zenoh-rs-pub': '[  0] Pub from Rust!')` 的行。
   - 把 z_pub 的 key 改成 `demo/hello` 后，默认订阅 `demo/example/**` 的 z_sub **收不到**（因为 `demo/hello` 不在 `demo/example/` 下）。
   - 把 z_sub 改成 `demo/**` 后，又能收到了（因为 `demo/**` 覆盖 `demo/hello`）。
4. **预期结果**：你通过改 key 直观地验证了「只有 key 相交，数据才会被送达」，并对 `**` 的多层匹配有了体感。
5. **说明**：本机首次运行依赖 Zenoh 的默认 scouting/发现机制（默认是 peer 模式、多播发现）。如果你的网络环境禁用多播（如某些虚拟机/Docker），两端可能互相发现不了——那时需要用 `-e` 显式连接，或加 `--no-multicast-scouting` 配合 `--listen/--connect`。这部分属于进阶用法，本篇了解即可。

#### 4.3.5 小练习与答案

- **练习 1**：为什么 `z_pub` 默认 key 是 `demo/example/zenoh-rs-pub`，而 `z_sub` 默认 key 是 `demo/example/**` 时两者能匹配？如果把 z_sub 改成 `demo/example/*`（单个 `*`）还匹配吗？
  - **参考答案**：`**` 匹配多层路径，所以 `demo/example/zenoh-rs-pub` 被 `demo/example/**` 覆盖。单个 `*` 只匹配一层，`demo/example/*` 能匹配 `demo/example/任意单层`，因此也能匹配 `demo/example/zenoh-rs-pub`（它正好是 example 下面一层）。但如果发布 key 是 `demo/example/a/b` 这种两层，单个 `*` 就匹配不上了。`*` 与 `**` 的精确差异会在《u2-l2 Key Expression》详讲。
- **练习 2**：`z_pub` 的循环里 `publisher.put(buf)` 后面链式调用了 `.encoding(Encoding::TEXT_PLAIN)`，这个 encoding 是给谁看的？
  - **参考答案**：它是负载的「编码元数据」，告诉接收方这份数据该按什么格式理解（这里是纯文本）。订阅端 `z_sub` 目前只是把它当字符串打印，但更复杂的应用可以用它来决定如何反序列化。编码体系会在《u5-l1 ZBytes 与 Encoding》展开。
- **练习 3**：`z_sub` 用 `subscriber.recv_async().await` 取数据，如果一直没有匹配的发布者，这行代码会怎样？
  - **参考答案**：它会一直 `await`（阻塞等待），直到有匹配的 `Sample` 到来或订阅者被关闭（此时循环退出）。这就是「回调 / 通道」里「通道」这种取数方式的特点——异步等待。

---

## 5. 综合实践

把本讲三块内容串起来，完成下面这个端到端小任务：

**任务**：从零编译 Zenoh，用两个终端跑通 Pub/Sub，再借助「公共参数」做一组对比实验。

1. **编译**（模块 4.1）：
   ```bash
   cargo build --release --all-targets
   ```
2. **读懂参数**（模块 4.2）：
   ```bash
   cargo run --example z_pub -- -h
   ```
   在帮助里找出三类参数：z_pub 独有参数（`-k`/`-v`/`--attach`/`--add_matching_listener`）、来自 `CommonArgs` 的公共参数（`-e`/`--mode`/`--listen`/`--no-multicast-scouting`/`--enable-shm`）、以及它们各自的默认值。
3. **跑通 Pub/Sub 并验证匹配**（模块 4.3）：
   - 终端 A：`cargo run --example z_sub`
   - 终端 B：`cargo run --example z_pub`
   - 把 z_pub 的 key 改成 `demo/hello`，确认默认 z_sub 收不到；再把 z_sub 改成 `-k 'demo/**'`，确认又能收到。
4. **进阶对比（可选）**：用 `--mode` 切换会话角色观察行为差异，例如：
   ```bash
   # 终端 A（订阅端，peer 模式）
   cargo run --example z_sub -- -m peer
   # 终端 B（发布端，client 模式连 A 的监听端口）
   cargo run --example z_pub -- -m client -e tcp/127.0.0.1:7447
   ```
   > 说明：上面的 `-m`/`-e` 来自 `CommonArgs`（见 [examples/src/lib.rs:23-27](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/examples/src/lib.rs#L23-L27)）。client 模式需要明确连到一个对端（`-e`）；具体端口能否监听成功取决于你的网络与是否有其他进程占用，若连不上请把 `-e` 指向你实际能用的端点。三种角色（Router/Peer/Client）的含义在《u2-l3 配置系统与 WhatAmI 三种角色》详讲。

**交付物**：把你观察到的「改 key 前后能否收到数据」的结论，以及从 `-h` 里抄下来的 3 个参数及其默认值，整理成几行中文笔记即可。

---

## 6. 本讲小结

- Zenoh 是标准 cargo workspace，编译用 `cargo build --release --all-targets`，最低 Rust 1.75；运行示例用 `cargo run --example <名字>` 或直接跑 `target/release/examples/<名字>`。
- `examples` 是一个名为 `zenoh-examples` 的正式 crate（workspace 成员），既是教学示例也是测试工具；每个示例由 `examples/Cargo.toml` 里的 `[[example]]` 声明。
- 示例用 `CommonArgs`（`examples/src/lib.rs`）复用 `-e`/`--mode`/`--listen` 等公共参数，并用 `From<CommonArgs> for Config` 把它们翻译成 Zenoh 的 `Config`。
- `z_pub` / `z_sub` 体现 Zenoh 应用的「三步走」：`zenoh::open` 打开会话 → `declare_publisher/declare_subscriber` 声明实体 → 循环 `put` / `recv_async` 收发。
- 默认配置下两端天然匹配：`z_pub` 发 `demo/example/zenoh-rs-pub`，`z_sub` 订 `demo/example/**`；改 key 会改变匹配关系——**只有 key 相交数据才会送达**。
- 共享内存等高级能力用 feature 开关（`shared-memory` / `unstable`）控制，相关示例（如 `z_pub_shm`）靠 `required-features` 限定，本篇保持默认即可。

## 7. 下一步学习建议

本篇你已经能把 Zenoh 跑起来并读懂最简单的 Pub/Sub。建议接下来：

1. **进入《u2-l1 打开一个 Session》**：深入理解 `zenoh::open`、`Config`、`Session` 的克隆语义与生命周期，把本篇里「黑盒使用」的会话变成「心里有数」。
2. **接着读《u2-l2 Key Expression》**：把本篇用到的 `*` / `**` 通配符与 `includes` / `intersects` 匹配规则彻底搞清楚——这是理解「为什么改 key 就收不到数据」的关键。
3. 想提前看更多示例，可浏览 [examples/README.md](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/examples/README.md) 里列出的 `z_put` / `z_get` / `z_queryable` / `z_storage` 等，它们分别对应后续 Query/Reply、存储等单元。
