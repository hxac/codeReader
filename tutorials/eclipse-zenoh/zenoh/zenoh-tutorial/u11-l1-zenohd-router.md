# zenohd 路由器：启动与配置

## 1. 本讲目标

本讲进入「路由器与插件系统」单元的第一讲。前面十几讲我们一直用 `zenoh::open(config)` 在自己的进程里创建 Session，写的是「应用」。本讲反过来，看 Zenoh 官方提供的那个开箱即用的路由器二进制 **`zenohd`** 是怎么把 `zenoh::open` 包装成一个可配置、可加载插件、可常驻后台运行的「基础设施服务」的。

学完本讲你应该能够：

1. 说清 `zenohd` 与 `zenoh::open` 的关系——它本质就是「Zenoh runtime + 插件管理器」的一层薄壳。
2. 读懂 `zenohd/src/main.rs` 的启动主流程：日志初始化 → CLI 解析 → 配置合并 → `zenoh::open` → 主线程挂起。
3. 把每一个命令行参数（`--config` / `--listen` / `--connect` / `--cfg` / `--plugin` / `--rest-http-port` 等）对应到 Config 的具体键上，并理解它们之间的**优先级**。
4. 描述插件是如何在 `RuntimeBuilder::build` 内部被「先加载、再启动」的，以及静态插件与动态插件的区别。

本讲只讲**启动与配置**这一条链路；`Plugin` trait 的接口细节、存储后端、REST 插件的内部实现分别留给 u11-l2 / u11-l3 / u11-l4。

## 2. 前置知识

本讲假设你已经掌握以下内容（否则先看对应讲义）：

- **Config 与 WhatAmI（u2-l3）**：知道 `zenoh::Config` 是一棵以斜杠分层的弱类型键值树，只能用 `from_file` / `from_json5` / `insert_json5` / `get_json` 读写；知道 Router / Peer / Client 三种角色。
- **Session 与 Runtime（u7-l1）**：知道 `zenoh::open(config)` 的执行链是 `RuntimeBuilder::build → Session::init → Runtime::start`，Runtime 承载节点的全部连接状态。
- **builder 与 Resolvable/Wait（u1-l4）**：知道 builder 必须 `.await` 或 `.wait()` 才真正 resolve。
- **clap 的 derive 用法**：知道 `#[derive(Parser)]` 配合 `#[arg(...)]` 可以从结构体字段自动生成命令行解析器。

补充一个新术语：

- **daemon（守护进程）**：指设计为常驻后台运行、提供持续服务而非「跑完就退出」的程序。`zenohd` 名字里的 `d` 就是 daemon 的意思。它的 `main` 函数最后用 `std::thread::park()` 把主线程永久挂起，正是 daemon 的典型写法。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [zenohd/src/main.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenohd/src/main.rs) | `zenohd` 的全部入口：CLI 定义（`Args`）、`main` 主流程、`config_from_args` 配置合并逻辑 |
| [zenohd/README.md](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenohd/README.md) | 命令行参数的官方文档与插件清单 |
| [zenohd/Cargo.toml](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenohd/Cargo.toml) | 声明 `zenohd` 对 `zenoh` 开启 `internal` + `plugins` + `runtime_plugins` + `unstable` 四个内部 feature |
| [zenoh/src/api/loader.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/loader.rs) | 插件加载与启动的三个函数：`load_plugin` / `load_plugins` / `start_plugins` |
| [zenoh/src/net/runtime/mod.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/runtime/mod.rs) | `RuntimeBuilder::build`，在 `zenoh::open` 内部完成「建 runtime → 加载插件 → 启动插件」 |
| [zenoh/src/api/config.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/config.rs) | `zenoh::Config` 的定义，以及 `unstable` feature 门控的 `Deref` 到内部 `zenoh_config::Config` |

## 4. 核心概念与源码讲解

### 4.1 Args/CLI 解析

#### 4.1.1 概念说明

`zenohd` 是一个命令行程序，它需要从命令行接收大量配置（监听哪个端口、连接哪个对端、加载哪些插件……）。Zenoh 用 Rust 生态里最流行的 CLI 库 **clap** 来做这件事，并且用的是它的 **derive 模式**：你只要定义一个普通的结构体 `Args`，用 `#[arg(...)]` 标注每个字段，clap 就自动帮你生成解析器、帮助文本（`--help`）和版本信息（`--version`）。

clap derive 的核心思想是「**字段即参数**」：

- 结构体的每个字段 ↔ 一个命令行选项。
- 字段类型决定参数形态：`bool` 是开关（`--flag`，不跟值），`Option<String>` 是可选带值参数，`Vec<String>` 是可重复参数（同一个选项可以出现多次，值被收集成列表）。
- `#[arg(short, long)]` 决定选项的长短写（`-c` / `--config`），`short = 'e'` 可以重命名短选项（注意 `-e` 是 connect，沿用历史）。

#### 4.1.2 核心流程

`zenohd` 的 CLI 解析流程极简：

```
main() 调用 Args::parse()
   ↓
clap 读取 std::env::args()
   ↓
按 Args 结构体定义匹配选项、收集值
   ↓
解析失败 → clap 自动打印错误/帮助并退出进程
解析成功 → 返回填充好的 Args 实例
```

#### 4.1.3 源码精读

`Args` 结构体就是 `zenohd` 全部命令行参数的「权威清单」，用 `#[derive(Parser)]` 标注：

```rust
#[derive(Debug, Parser)]
#[command(version=GIT_VERSION, long_version=LONG_VERSION.as_str(), about="The zenoh router")]
struct Args {
    #[arg(short, long, value_name = "PATH")]
    config: Option<String>,
    #[arg(short, long, value_name = "ENDPOINT")]
    listen: Vec<String>,
    ...
}
```

见 [zenohd/src/main.rs:27-35](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenohd/src/main.rs#L27-L35)——这里定义了 `config`（`Option`，可有可无）与 `listen`（`Vec`，可重复）两个字段的形态。

整个 `Args` 一共有 12 个字段，逐个对应 README 里列出的命令行参数。下表把它们整理成一张「字段 → CLI → 说明」对照表（行号指向 main.rs 中各字段的定义处）：

| 字段（main.rs） | CLI | 类型 | 说明 |
| --- | --- | --- | --- |
| [config](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenohd/src/main.rs#L31-L32) | `-c/--config <PATH>` | `Option<String>` | 配置文件路径（JSON5/YAML） |
| [listen](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenohd/src/main.rs#L34-L35) | `-l/--listen <ENDPOINT>` | `Vec<String>` | 监听端点，可重复 |
| [connect](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenohd/src/main.rs#L38-L39) | `-e/--connect <ENDPOINT>` | `Vec<String>` | 主动连接的对端，可重复 |
| [id](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenohd/src/main.rs#L42-L43) | `-i/--id` | `Option<String>` | 指定 ZID（否则随机） |
| [plugin](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenohd/src/main.rs#L46-L47) | `-P/--plugin` | `Vec<String>` | 必须加载的插件，可重复 |
| [plugin_search_dir](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenohd/src/main.rs#L50-L51) | `--plugin-search-dir <PATH>` | `Vec<String>` | 插件库搜索目录 |
| [no_timestamp](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenohd/src/main.rs#L53-L54) | `--no-timestamp` | `bool` | 关闭路由时自动盖时间戳 |
| [no_multicast_scouting](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenohd/src/main.rs#L56-L57) | `--no-multicast-scouting` | `bool` | 关闭多播发现应答 |
| [rest_http_port](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenohd/src/main.rs#L61-L62) | `--rest-http-port <SOCKET>` | `Option<String>` | 启用 REST 插件并设端口 |
| [cfg](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenohd/src/main.rs#L72-L73) | `--cfg <CFG>` | `Vec<String>` | 任意 `KEY:VALUE` 配置覆盖，可重复 |
| [adminspace_permissions](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenohd/src/main.rs#L75-L76) | `--adminspace-permissions` | `Option<String>` | 管理空间读写权限 |

注意一个容易踩的坑：`-e` 是 **connect**（连接对端）而不是 edit；这是为了与历史版本兼容，代码里用 `short = 'e'` 显式重命名（见 [zenohd/src/main.rs:38](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenohd/src/main.rs#L38)）。事实上 `--connect` 对应的错误提示至今仍写着 `--peer`（[zenohd/src/main.rs:173](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenohd/src/main.rs#L173)），就是这个历史遗留的痕迹。

真正触发解析的就一行：

```rust
let args = Args::parse();
```

见 [zenohd/src/main.rs:87](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenohd/src/main.rs#L87)。`Args::parse()` 内部会读 `std::env::args()`，匹配失败时 clap 直接打印帮助并 `exit`，不会返回错误——所以 `main` 里不需要 `match`。

> 关于版本信息：`#[command(version=GIT_VERSION, ...)]` 里的 `GIT_VERSION` 由 `git_version` crate 在编译期从 git 提交号生成，见 [zenohd/src/main.rs:21](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenohd/src/main.rs#L21)。所以 `zenohd --version` 能打印出当前二进制对应的精确提交。

#### 4.1.4 代码实践

**实践目标**：用 clap 自动生成的 `--help` 当作「活文档档」，对照源码确认每个参数。

**操作步骤**：

1. 进入仓库根目录，编译并查看帮助（注意 `cargo run` 必须用 `--` 把后续参数透传给 `zenohd` 而不是 cargo 自己）：

   ```bash
   cargo run -p zenohd -- --help
   ```

2. 也可以先编译出二进制再运行（推荐 `--release`，debug 编译的 zenohd 性能很差）：

   ```bash
   cargo build -p zenohd --release
   ./target/release/zenohd --help
   ```

3. 查看长版本信息：

   ```bash
   ./target/release/zenohd --version
   ./target/release/zenohd -V
   ```

**需要观察的现象**：

- `--help` 输出里应能看到本讲表格里列出的全部选项，且 `-c/-l/-e/-i/-P` 等短选项与说明一一对应。
- 选项被分了组（Core Options / Plugin Management / Behavioral Options / Advanced Configuration / Help & Version），这正是 README 的章节划分。
- `-V` 会打印带 git 提交号和 rustc 版本的字符串，对应源码里的 `LONG_VERSION`（[zenohd/src/main.rs:23-25](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenohd/src/main.rs#L23-L25)）。

**预期结果**：你能从 `--help` 输出里找到每个字段对应的行，反过来也能从源码字段预测 `--help` 里会出现什么。如果某个选项在 `--help` 里没有出现，多半是你拼错了长短写（clap 不认会直接报错）。

> 若本地尚未安装 Rust 工具链或编译资源不足，上述命令的精确输出标注「待本地验证」，但 `--help` / `--version` 的存在性与分组结构可由源码确定。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `listen` 字段类型是 `Vec<String>` 而不是 `Option<String>`？这对用户使用有什么直接影响？

> **答案**：因为一个路由器可以同时监听多个端点（比如同时监听 TCP 和 WS）。`Vec` 表示同一个选项可以在命令行出现多次，clap 会把每次的值都收集进列表；而 `Option` 只能出现一次（`Some` 或 `None`）。所以你能写 `-l tcp/0.0.0.0:7447 -l ws/0.0.0.0:8080/my` 来开两个监听器。

**练习 2**：如果用户既不传 `--config` 也不传任何参数，`Args::parse()` 会失败吗？

> **答案**：不会失败。所有字段要么是 `Option`、要么是 `Vec`（空时为空 Vec）、要么是 `bool`（默认 `false`），没有任何字段被标记为必需。clap 只在「必需参数缺失」时报错，这里没有必需参数，所以零参数也能解析成功，`config` 为 `None`、各 `Vec` 为空——此时 zenohd 会走 `Config::default()` 默认配置（见 4.2.3）。

---

### 4.2 config 合并

#### 4.2.1 概念说明

CLI 解析只是第一步。`zenohd` 真正的复杂度在于：**同一个配置项可以被好几个来源指定**——配置文件、`--listen`、`--cfg`、隐含默认值……当它们冲突时谁说了算？

`config_from_args` 函数就是这个「**配置合并器**」：它以某个**基础配置**为底，然后把 CLI 参数按固定顺序一层层覆盖上去，最终产出一份完整的 `zenoh::Config`。

理解这一节的关键是抓住两个概念：

- **基础配置的三选一**：优先级是「`--cfg ':{...}'` 内联整份配置 > `--config FILE` 配置文件 > `Config::default()` 默认值」。
- **`--cfg KEY:VALUE` 是最终逃生舱**：所有带结构化语义的 CLI 选项（`--listen`、`--no-multicast-scouting` 等）先被应用，而所有**非空 key** 的 `--cfg` 在最后才应用，因此 `--cfg` 能覆盖一切——它是最高优先级的逐项覆盖手段。

#### 4.2.2 核心流程

`config_from_args` 的合并顺序（自上而下，**越往下优先级越高**）：

```
1. 确定基础配置:
     - 若有 --cfg ':{...}'（空 key） → Config::from_json5(整份)
     - 否若有 --config FILE          → Config::from_file(FILE)
     - 否                            → Config::default()
2. 若 mode 未设 → 强制设为 Router
3. 若有 --id                  → set_id
4. 若有 --rest-http-port      → plugins/rest/http_port + __required__
5. 强制 adminspace.enabled = true
6. 强制 plugins_loading.enabled = true
7. 若有 --plugin-search-dir   → plugins_loading/search_dirs
8. 若有 -P/--plugin           → plugins/{name}/__required__(+__path__)
9. 若有 --connect             → connect/endpoints（整体替换）
10. 若有 --listen             → listen/endpoints（整体替换）
11. 若有 --no-timestamp       → timestamping/enabled = false
12. 若有 --no-multicast-scouting → scouting/multicast/enabled = false
13. 若有 --adminspace-permissions → adminspace/permissions
14. 对每个非空 key 的 --cfg KEY:VALUE → insert(KEY, VALUE)  ← 最后执行，优先级最高
```

#### 4.2.3 源码精读

**(1) 基础配置三选一**——注意第一个循环专门挑出空 key 的 `--cfg`：

```rust
let mut inline_config = None;
for json in &args.cfg {
    if let Some(("", cfg)) = json.split_once(':') {
        inline_config = Some(cfg);
    }
}
let mut config = if let Some(cfg) = inline_config {
    Config::from_json5(cfg).expect("Invalid Zenoh config")
} else if let Some(fname) = args.config.as_ref() {
    Config::from_file(fname).expect("Failed to load config file")
} else {
    Config::default()
};
```

见 [zenohd/src/main.rs:103-116](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenohd/src/main.rs#L103-L116)。`split_once(':')` 把 `KEY:VALUE` 切成两段；当 `KEY` 是空串时，表示「VALUE 是一整份配置」，用它替代基础配置。这里用 `.expect(...)` 而非 `?`，说明配置错误会直接 panic 退出。

**(2) 强制 Router 角色**——这是 zenohd 与普通应用最本质的区别：

```rust
if config.mode().is_none() {
    config.set_mode(Some(WhatAmI::Router)).unwrap();
}
```

见 [zenohd/src/main.rs:118-120](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenohd/src/main.rs#L118-L120)。回忆 u2-l3：`WhatAmI` 默认是 Peer。zenohd 作为「路由器」二进制，**只要用户没显式指定 mode，就强制改成 Router**。这正是「daemon router」语义的落点——你也可以用 `--cfg mode='"peer"'` 强行把它当 peer 跑，但默认行为是成为 Router。

**(3) 强制开启 adminspace 与 plugins_loading**：

```rust
config.adminspace.set_enabled(true).unwrap();
config.plugins_loading.set_enabled(true).unwrap();
```

见 [zenohd/src/main.rs:136-137](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenohd/src/main.rs#L136-L137)。这两行说明：无论配置文件怎么写，`zenohd` 永远会启用管理空间（让你能用 Zenoh 的 key expression 查询路由器内部状态，详见 u12-l3）和插件加载机制。这是路由器作为「可观测、可扩展」基础设施的硬性要求。

> **关键细节：为什么 main.rs 能直接写 `config.connect.endpoints.set(...)`？** 回忆 u2-l3 我们说过 `zenoh::Config` 字段私有、只能用 `insert_json5`。但这里 main.rs 直接访问了 `.connect.endpoints`、`.adminspace`、`.scouting` 等字段——这是因为 `zenoh::Config` 有一个 `Deref`/`DerefMut` 实现指向内部 `zenoh_config::Config`，而它被 `#[zenoh_macros::unstable]` 门控（见 [zenoh/src/api/config.rs:122-136](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/config.rs#L122-L136)）。`zenohd` 在 Cargo.toml 里开启了 `unstable`（见 [zenohd/Cargo.toml:39-44](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenohd/Cargo.toml#L39-L44)），所以能用结构化字段访问；普通应用不开 `unstable`，就只能用 `insert_json5`。换句话说，`main.rs` 用的是「内部特权 API」，应用层不应模仿。

**(4) `--listen` / `--connect` 整体替换列表**：

```rust
if !args.listen.is_empty() {
    config.listen.endpoints.set(
        args.listen.iter().map(|v| match v.parse::<EndPoint>() {
            Ok(v) => v,
            Err(e) => panic!("Couldn't parse option --listen={v} into Locator: {e}"),
        }).collect(),
    ).unwrap();
}
```

见 [zenohd/src/main.rs:180-196](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenohd/src/main.rs#L180-L196)。这里有个重要语义：`.set(...)` 是**整体替换**而非追加。也就是说，命令行一旦出现 `--listen`，就会**覆盖**配置文件里的 `listen/endpoints`，而不是在它的基础上增加。`--connect` 同理（[zenohd/src/main.rs:163-179](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenohd/src/main.rs#L163-L179)，每个值用 `EndPoints` 解析，因此单个 `--connect` 里还可以逗号分隔多个端点）。

**(5) `--cfg KEY:VALUE` 在最后应用，优先级最高**：

```rust
for json in &args.cfg {
    if let Some((key, value)) = json.split_once(':') {
        if !key.is_empty() {
            match json5::Deserializer::from_str(value) {
                Ok(mut deserializer) => {
                    if let Err(e) = config.insert(key.strip_prefix('/').unwrap_or(key), &mut deserializer) {
                        tracing::warn!("Couldn't perform configuration {}: {}", json, e);
                    }
                }
                ...
            }
        }
    } else {
        panic!("--cfg accepts KEY:VALUE pairs. ...")
    }
}
```

见 [zenohd/src/main.rs:250-267](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenohd/src/main.rs#L250-L267)。注意它跳过了空 key（空 key 已在第一步用作基础配置）。这里把 VALUE 当作 JSON5 流式反序列化后 `config.insert(KEY, ...)`，KEY 开头的 `/` 会被剥掉。**因为这段循环在整个函数最后执行**，所以 `--cfg` 能覆盖前面任何结构化 CLI（包括 `--listen`、`--no-multicast-scouting`）的设置——这就是「最高优先级逐项覆盖」的含义。它失败时只 `warn` 不 panic，比基础配置的 `.expect()` 宽容。

最后，`config_from_args` 把这份合并好的 Config 返回，并由 `main` 透传日志：

```rust
let config = config_from_args(&args);
tracing::info!("Initial conf: {}", &config);
```

见 [zenohd/src/main.rs:88-89](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenohd/src/main.rs#L88-L89)。

#### 4.2.4 代码实践

**实践目标**：亲手验证「`--cfg` 覆盖一切」与「`--listen` 整体替换」两条规则。

**操作步骤**：

1. 用默认配置启动一个 zenohd（开多播发现以便观察）：

   ```bash
   ./target/release/zenohd
   ```

   观察日志，应能看到默认监听了 `tcp/0.0.0.0:7447` 并启用了 multicast scouting。

2. 用 `--listen` 替换监听端口为 8447：

   ```bash
   ./target/release/zenohd --listen tcp/127.0.0.1:8447
   ```

   预期日志里只剩 8447 这一个监听端点，原来的 7447 **消失**（验证整体替换）。

3. 用 `--cfg` 关闭多播发现（这正是任务里要求的命令）：

   ```bash
   ./target/release/zenohd --cfg scouting/multicast/enabled=false
   ```

   预期日志中不再出现 multicast scouting 相关的「listening for multicast」行。

4. 验证 `--cfg` 的优先级高于 `--no-multicast-scouting` 的反面——先用开关关闭，再用 `--cfg` 强制打开：

   ```bash
   ./target/release/zenohd --no-multicast-scouting --cfg scouting/multicast/enabled=true
   ```

   预期 multicast scouting 又被打开（因为 `--cfg` 在最后应用）。

**需要观察的现象**：第 2 步监听端口被替换；第 3 步多播发现被关闭；第 4 步两个相反指令同时出现时，`--cfg` 一方获胜。

**预期结果**：四条命令的行为差异都能在启动日志里直接看到（搜索 `listen` 与 `scout`/`multicast` 关键字）。

> 日志的精确文本随版本变化，标注「待本地验证」；但「`--listen` 替换」「`--cfg` 最高优先级」这两条规则由源码顺序确定，不会变。

#### 4.2.5 小练习与答案

**练习 1**：假设配置文件 `my.json5` 里写了 `listen/endpoints: ["tcp/0.0.0.0:7447"]`，启动命令是 `zenohd -c my.json5 -l tcp/0.0.0.0:8447`。最终路由器监听哪些端口？

> **答案**：只监听 `tcp/0.0.0.0:8447`，7447 不会监听。因为 `--listen` 走的是 `config.listen.endpoints.set(...)` 整体替换语义（[main.rs:180-196](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenohd/src/main.rs#L180-L196)），配置文件里的值被整个覆盖。若想同时保留两个端口，应写 `-l tcp/0.0.0.0:7447 -l tcp/0.0.0.0:8447`。

**练习 2**：命令 `zenohd --cfg ':{mode:"peer"}' --cfg mode='"router"'` 最终角色是什么？

> **答案**：Router。第一个 `--cfg ':{mode:"peer"}'` 是空 key，作为基础配置整体载入（mode=peer）；但随后 [main.rs:118-120](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenohd/src/main.rs#L118-L120) 检查到……注意这里有个微妙点：基础配置里 mode 已经是 `peer`（非空），所以第 2 步「mode 未设则强制 Router」**不会**触发；但最后的非空 `--cfg mode='"router"'` 会再次把 mode 改成 router（因为 `--cfg` 在最后执行、优先级最高）。所以最终是 Router。

**练习 3**：为什么第 14 步的 `--cfg` 失败时只打 `warn`，而第 1 步 `from_json5` 失败时用 `.expect()` 直接 panic？这样设计合理吗？

> **答案**：合理。基础配置是 zenohd 能否运行的根基，解析失败意味着用户给的整份配置就是坏的，没法继续，应当立即失败暴露问题；而单个 `--cfg KEY:VALUE` 只是局部覆盖，某一项失败不应让整个路由器起不来，所以降级为告警、跳过该项继续。这体现了「致命错误硬失败、局部错误软告警」的工程取舍。

---

### 4.3 runtime + 插件启动

#### 4.3.1 概念说明

配置合并好之后，`main` 调用 `zenoh::open(config).wait()` 拿到一个 `Session`。这看似和普通应用没区别，但有一个关键差异：**zenohd 开启了 `plugins` feature**，于是 `zenoh::open` 内部的 `RuntimeBuilder::build` 会额外完成「加载插件、启动插件」两件事。

README 对 `zenohd` 的一句话定义是：「**`zenohd` is the Zenoh runtime with a plugin manager**」——见 [zenohd/README.md:23](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenohd/README.md#L23)。本节就是拆解这句话的后半段「plugin manager」。

先区分两个概念：

- **静态插件（statically linked）**：在编译期就链接进 `zenohd` 二进制、和 zenohd 同生共死的插件。zenohd 默认带两个：`zenoh-plugin-rest` 和 `zenoh-plugin-storage-manager`（见 [zenohd/README.md:92-97](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenohd/README.md#L92-L97)）。
- **动态插件（dynamically loaded）**：运行时从 `.so` / `.dll` / `.dylib` 文件加载的插件，由 `-P/--plugin` 或配置里的 `plugins/<name>/__path__` 指定。

⚠️ **重要约束**：因为 Rust 没有稳定 ABI，动态插件必须用**和 zenohd 完全相同的 Rust 版本**、**相同版本的 zenoh 依赖**、**相同的 feature 集合**编译，否则会在加载时被拒绝，强行使用会导致 SIGSEGV 崩溃——见 [zenohd/README.md:89-90](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenohd/README.md#L89-L90) 的 WARNING。

#### 4.3.2 核心流程

`zenoh::open` 内部 `RuntimeBuilder::build` 的执行顺序（只画与本讲相关部分）：

```
zenoh::open(config).wait()
   └─ RuntimeBuilder::build()              [mod.rs:675]
        ├─ 解析 zid / whatami / hlc
        ├─ 构建 Gateway（路由器）+ TransportManager（传输管理器）
        ├─ load_plugins(&config)           [mod.rs:747-749]  ← 读配置、加载插件库
        │     └─ 遍历 config.plugins().load_requests()
        │          ├─ declare + load 每个插件
        │          └─ required 加载失败 → panic
        ├─ （若启用）AdminSpace::start      [mod.rs:785-787]
        └─ start_plugins(&runtime)         [mod.rs:791]      ← 用 DynamicRuntime 启动每个插件
              └─ 对每个 loaded plugin 调 plugin.start(dynamic_runtime)
                   └─ required 启动失败 → panic
```

注意「**加载（load）**」与「**启动（start）**」是两个分离的阶段：load 是把插件代码读进进程、校验版本，start 才是调用插件的 `start` 方法、把 `DynamicRuntime` 交给它去注册自己的功能。分离的好处是：可以先把所有插件都加载完、确认 ABI 兼容，再统一启动。

#### 4.3.3 源码精读

**(1) `zenohd` 的 main 把控制权交给 `zenoh::open`**：

```rust
let _session = match zenoh::open(config).wait() {
    Ok(runtime) => runtime,
    Err(e) => {
        eprintln!("{e}. Exiting...");
        std::process::exit(-1);
    }
};
std::thread::park();
```

见 [zenohd/src/main.rs:91-100](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenohd/src/main.rs#L91-L100)。注意返回值被命名为 `_session`（其实它就是 `Session`），它在 `main` 结束前必须一直被持有——一旦 drop，Session 关闭、runtime 停止、路由器也就退出了。所以最后一句 `std::thread::park()` 把主线程永久挂起，让 daemon 常驻；你只能用 Ctrl-C / kill 来终止它。`-1` 是 zenohd 启动失败时的退出码。

**(2) 插件加载发生在 `RuntimeBuilder::build` 内部**。先看加载：

```rust
// Plugins manager
#[cfg(feature = "plugins")]
let plugins_manager = plugins_manager
    .take()
    .unwrap_or_else(|| load_plugins(&config));
```

见 [zenoh/src/net/runtime/mod.rs:745-749](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/runtime/mod.rs#L745-L749)。这段被 `#[cfg(feature = "plugins")]` 门控——普通应用不开 `plugins` feature，这里整段会被编译掉，所以 `zenoh::open` 在普通应用里**根本不会**加载插件；只有 zenohd（和显式开启 `plugins` 的程序）才会。`plugins_manager.take().unwrap_or_else(...)` 表示：如果调用方（如测试）已经预先塞了一个 manager 就用它，否则用 `load_plugins(&config)` 从配置现读。zenohd 走的是后者。

紧接着是管理空间与插件启动：

```rust
// Admin space
if start_admin_space {
    AdminSpace::start(&runtime).await;
}

// Start plugins
#[cfg(feature = "plugins")]
start_plugins(&runtime);
```

见 [zenoh/src/net/runtime/mod.rs:784-791](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/runtime/mod.rs#L784-L791)。注意顺序：**先起 AdminSpace，再起插件**——这样插件启动后就能立刻通过管理空间暴露自己的状态（插件状态查询机制见 u12-l3）。

**(3) `load_plugins`：把配置里的插件清单变成已加载的代码**：

```rust
pub(crate) fn load_plugins(config: &Config) -> PluginsManager {
    let mut manager = PluginsManager::dynamic(config.libloader(), PLUGIN_PREFIX.to_string());
    for plugin_load in config.plugins().load_requests() {
        let PluginLoad { id, name, paths, required } = plugin_load;
        ...
        if let Err(e) = load_plugin(&mut manager, &name, &id, &paths, required) {
            if required { panic!("Plugin load failure: {e}") }
            else { tracing::error!("Plugin load failure: {e}") }
        }
    }
    manager
}
```

见 [zenoh/src/api/loader.rs:50-73](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/loader.rs#L50-L73)。关键点：

- `config.plugins().load_requests()` 把配置里所有 `plugins/<name>/__required__`（和可选的 `__path__`）翻译成一列 `PluginLoad` 请求。回忆 4.2.3：`-P/--plugin` 就是往这些键里写值（[main.rs:148-162](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenohd/src/main.rs#L148-L162)），所以 CLI 的 `-P` 与配置文件的 `plugins/` 节最终汇入同一条加载流水线。
- `PLUGIN_PREFIX` 是 `"zenoh_plugin_"`（见 [zenoh/src/api/plugins.rs:26-28](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/plugins.rs#L26-L28)），即只给名字时（如 `-P rest`），loader 会去找 `libzenoh_plugin_rest.so` 这样的库文件。
- `required` 区分硬失败与软失败：`__required__: true` 的插件加载失败直接 `panic`（让 zenohd 退出），否则只记 error 日志继续。

**(4) `start_plugins`：把 runtime 交给每个插件**：

```rust
pub(crate) fn start_plugins(runtime: &Runtime) {
    let mut manager = runtime.plugins_manager();
    let dynamic_runtime = runtime.clone().into();
    for plugin in manager.loaded_plugins_iter_mut() {
        let required = plugin.required();
        ...
        match plugin.start(&dynamic_runtime) {
            Ok(_) => { tracing::info!(...) }
            Err(e) => { /* required → panic; 否则 error */ }
        }
    }
}
```

见 [zenoh/src/api/loader.rs:75-123](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/loader.rs#L75-L123)。`dynamic_runtime = runtime.clone().into()` 把 `Runtime` 转成 `DynamicRuntime`——这就是传给插件 `start` 方法的参数类型（`Plugin::StartArgs = DynamicRuntime`，见 [zenoh/src/api/plugins.rs:33](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/plugins.rs#L33)）。插件拿到它就能访问 Session、注册自己的 queryable/subscriber、暴露 admin space。这里有个防御性细节：错误格式化时用了 `catch_unwind`（[loader.rs:94-97](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/loader.rs#L94-L97)），因为插件来自动态库，一旦它返回的错误对象因 ABI 不兼容而无法 `to_string`，会触发 panic——这时捕获并提示「请用相同 cargo 版本重编插件」。

> **`-P/--plugin` 的两种写法**：见 [main.rs:148-162](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenohd/src/main.rs#L148-L162)。`-P rest`（只给名字）会写 `plugins/rest/__required__=true`，由 loader 按 `PLUGIN_PREFIX` 搜库；`-P rest:/path/to/lib.so`（`name:path`）会额外写 `plugins/rest/__path__`，loader 直接按路径加载。两者最终都被 `load_plugins` 统一消费。

#### 4.3.4 代码实践

**实践目标**：观察「内置静态插件」的加载与启动日志，并理解 `-P` 如何变成一次插件加载请求。

**操作步骤**：

1. 把日志级别调到 `info` 启动 zenohd（默认就是 `z=info`，见 [main.rs:273](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenohd/src/main.rs#L273)）：

   ```bash
   RUST_LOG=z=info ./target/release/zenohd
   ```

2. 在日志里查找形如 `Starting plugin` / `Successfully started plugin` 的行。注意：rest 与 storage_manager 是**静态插件**，是否出现在日志取决于配置里是否 `__required__: true`；它们默认不一定启用。

3. 用 `--rest-http-port` 显式启用 REST 插件，再观察日志：

   ```bash
   ./target/release/zenohd --rest-http-port 8000
   ```

   预期看到 REST 插件被加载并在 8000 端口监听 HTTP。回忆 4.2.3，这条命令在配置里写了 `plugins/rest/http_port` 与 `plugins/rest/__required__=true`（[main.rs:124-135](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenohd/src/main.rs#L124-L135)），于是 `load_plugins` 会把它纳入加载清单。

4. 故意用一个不存在的插件名测试 `required` 硬失败：

   ```bash
   ./target/release/zenohd -P no_such_plugin
   ```

   预期 zenohd 打印 `Plugin load failure: ...` 后**直接 panic 退出**（因为 `-P` 写入的是 `__required__=true`，见 [main.rs:158-161](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenohd/src/main.rs#L158-L161) 与 [loader.rs:64-66](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/loader.rs#L64-L66)）。

**需要观察的现象**：第 3 步 REST 插件成功起在 8000 端口；第 4 步进程因找不到插件而 panic 退出（退出码非 0）。

**预期结果**：你能从日志里分辨出「加载（load）」与「启动（start）」两个阶段各自的提示行，并验证 `required` 插件失败会让整个 zenohd 退出。

> 实际日志措辞与退出码请「待本地验证」；「`-P` → `__required__` → 失败 panic」这条链路由源码确定。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `load_plugins` 和 `start_plugins` 要分成两步，而不是在每个插件加载后立刻启动？

> **答案**：分离有两个好处。其一，先全部 load 完可以尽早暴露 ABI 不兼容（动态库读进来时就能发现符号/版本不匹配），避免「启动到一半才发现某个插件崩了」的半成品状态。其二，`start_plugins` 需要一个**已经构建完成**的 `Runtime`（要把 `DynamicRuntime` 交给插件），而 `load_plugins` 是在 `RuntimeBuilder::build` 中段、runtime 尚未组装好时调用的——所以必须先 load（早期、只要 config）后 start（晚期、需要 runtime）。见 [mod.rs:747-749](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/runtime/mod.rs#L747-L749) 与 [mod.rs:791](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/runtime/mod.rs#L791) 的位置差异。

**练习 2**：普通应用调用 `zenoh::open(config)` 会不会触发 `load_plugins`？为什么？

> **答案**：不会。`load_plugins` 与 `start_plugins` 的调用点都被 `#[cfg(feature = "plugins")]` 门控（[mod.rs:746](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/runtime/mod.rs#L746) 与 [mod.rs:790](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/runtime/mod.rs#L790)）。普通应用默认不开 `plugins` feature，相关代码在编译期就被剔除；只有 zenohd（Cargo.toml 里显式开了 `plugins` + `runtime_plugins` + `internal` + `unstable`，见 [zenohd/Cargo.toml:39-44](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenohd/Cargo.toml#L39-L44)）才具备插件能力。

**练习 3**：`plugin.start(&dynamic_runtime)` 传给插件的 `dynamic_runtime` 是什么？插件拿它能干什么？

> **答案**：`dynamic_runtime` 由 `runtime.clone().into()` 得来（[loader.rs:77](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/loader.rs#L77)），是 `DynamicRuntime` 类型，对应 `Plugin` trait 的 `StartArgs`（[plugins.rs:33](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/plugins.rs#L33)）。它本质是 runtime 的一个共享句柄，插件拿它可以访问/创建 Session、声明自己的 queryable/subscriber、注册 config_checker、向 admin space 暴露状态。具体接口细节是 u11-l2 的主题。

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「**带 REST 接口、关闭多播发现、固定 ZID 的路由器**」部署任务。

**要求**：用**一条命令**启动 zenohd，满足以下全部条件，并验证每一项生效：

1. 监听 `tcp/0.0.0.0:7447`（用 `--listen` 显式指定）。
2. 关闭多播发现（用 `--no-multicast-scouting`）。
3. 固定 ZID 为 `a0b1c2d3e4f5061728394a5b6c7d8e9f`（用 `--id`）。
4. 启用 REST 插件，HTTP 端口 8000（用 `--rest-http-port`）。
5. 通过 `--cfg` 把 `metadata/name` 设为 `"my-router"`。

**参考命令**：

```bash
./target/release/zenohd \
  --listen tcp/0.0.0.0:7447 \
  --no-multicast-scouting \
  --id a0b1c2d3e4f5061728394a5b6c7d8e9f \
  --rest-http-port 8000 \
  --cfg 'metadata/name:"my-router"'
```

**验证步骤**（开另一个终端）：

1. 看启动日志，确认 `Using ZID: a0b1c2...` 与监听 7447、缺少 multicast scouting 行。
2. 用 REST 接口验证路由器活着：

   ```bash
   # 查询 admin space 里这个路由器的元数据（把 ZID 换成上面的值）
   curl 'http://localhost:8000/@/a0b1c2d3e4f5061728394a5b6c7d8e9f/router/__metadata__'
   ```

   预期能看到 `name: "my-router"`，证明 `--cfg` 的元数据写进去了、且 REST 插件与 admin space 都在正常工作（admin space 由 4.2.3 的 `adminspace.set_enabled(true)` 保证开启）。

3. 思考题：如果在上面的命令末尾再加一个 `--cfg scouting/multicast/enabled=true`，多播发现会被重新打开吗？为什么？（答：会，因为非空 key 的 `--cfg` 在 `config_from_args` 最后执行，优先级最高，会覆盖 `--no-multicast-scouting` 写入的 `false`。）

**预期结果**：一条命令、五个要求全部满足，REST 验证返回你设置的名字。

> REST 查询的确切 key 与返回 JSON 结构请「待本地验证」（admin space 的路径约定细节在 u12-l3 展开）；本实践的重点是验证「CLI → Config → runtime → 插件」整条链路是否按你预期工作。

## 6. 本讲小结

- `zenohd` 不是黑盒，它就是「Zenoh runtime + 插件管理器」的一层薄壳，README 原文：*zenohd is the Zenoh runtime with a plugin manager*。
- `main` 的四步流程：`init_logging` → `Args::parse()` → `config_from_args()` → `zenoh::open(config).wait()`，最后 `std::thread::park()` 常驻。
- `Args` 用 clap derive 定义，12 个字段即 12 类命令行参数；`Vec` 字段（`listen`/`connect`/`plugin`/`cfg`）可重复，`bool` 字段是开关。
- 配置合并遵循固定优先级：基础配置（`--cfg ':{...}'` > `--config FILE` > 默认）→ 结构化 CLI 覆盖 → 非空 key 的 `--cfg` 最后应用、优先级最高；`--listen`/`--connect` 是**整体替换**而非追加。
- zenohd 只要 mode 未设就强制 `Router`，并强制开启 `adminspace` 与 `plugins_loading`——这是它作为「可观测、可扩展基础设施」的硬性设定。
- `main.rs` 能用 `config.connect.endpoints.set(...)` 这种结构化访问，靠的是 `unstable` feature 门控的 `Deref`；普通应用没这个特权，只能 `insert_json5`。
- 插件加载分两阶段：`load_plugins`（在 runtime 组装中段、读配置加载库）→ `start_plugins`（runtime 就绪后、用 `DynamicRuntime` 启动）；`required` 插件失败会 panic 让 zenohd 退出。这套逻辑只在 `plugins` feature 开启时编译进 `zenoh::open`。

## 7. 下一步学习建议

本讲把 zenohd 的「启动与配置」讲完了，但插件**内部**长什么样还没展开。建议下一步：

1. **u11-l2 插件系统：Plugin trait 与 PluginsManager**：精读 `plugins/zenoh-plugin-trait`，看 `Plugin` trait、`PluginsManager` 的声明/加载/启动生命周期，以及动态插件为何受 ABI 限制——它直接承接本讲 4.3 的 `load_plugins`/`start_plugins`，把「为什么这么调用」讲透。
2. **u11-l3 存储与后端**：看内置静态插件 `zenoh-plugin-storage-manager` 如何把 `Volume`/`Storage` trait 接进路由器，是本讲「内置插件」一个完整实例。
3. **u11-l4 REST 插件**：看另一个内置静态插件 `zenoh-plugin-rest` 如何把 HTTP 映射到 Zenoh，对应本讲综合实践里那个 8000 端口背后的实现。
4. 若想验证本讲行为，推荐先按 u1-l2 用 `--release` 编译整个 workspace，再按本讲第 4 节的命令逐条实验；并用 `RUST_LOG=z=debug` 观察更细的加载/启动日志。
