# 配置系统与 WhatAmI 三种角色

## 1. 本讲目标

本讲是「核心概念：Session 与 Key Expression」单元的第三讲。上一讲你已经学会打开 `Session`，并知道 `zenoh::open(config)` 需要一个 `Config` 参数。本讲专门把这个 `Config` 拆开讲透。

学完本讲，你应当能够：

1. 说清 `zenoh::Config` 的内部结构：它是一棵「带校验、字段私有、只能用字符串键读写」的配置树。
2. 用 `from_file` / `from_json5` 从文件或字符串加载配置，用 `insert_json5` / `get_json` / `remove` 增删查配置项。
3. 区分 Zenoh 的三种节点角色（Router / Peer / Client），理解 `WhatAmI` 枚举与它的字符串表示，以及「同一个值可随角色变化」的 `ModeDependentValue` 机制。
4. 看懂仓库根目录的 `DEFAULT_CONFIG.json5` 是做什么的，以及代码里的默认值（`defaults.rs`）与配置文件之间的分工。

## 2. 前置知识

本讲依赖《u2-l1 打开一个 Session》。在继续前，请确认你已理解以下内容：

- **Config 是 open 的入参**：`zenoh::open(config)` 返回一个 `Session`，`config` 决定了这个会话「以什么角色运行、监听哪些端口、连接哪些节点、是否启用组播发现」。
- **Config 字段是私有的**：上一讲已经提过，`zenoh::Config` 不直接暴露字段，只能用字符串键（如 `"mode"`、`"listen/endpoints"`）来读写。本讲会解释**为什么**这样设计、以及它内部到底长什么样。
- **ZenohId 与 WhatAmI 角色**：上一讲讲过 `zid()` 读取本节点唯一标识；本讲补齐「角色」这一维度——每个节点除了有唯一 ID，还有一个角色 `WhatAmI`（router / peer / client），由配置里的顶层 `mode` 决定。

补充一个术语：**serde**。Zenoh 的配置用 Rust 生态里最常用的序列化库 `serde` 来做「JSON5 / YAML 字符串 ↔ Rust 结构体」的互转。你不需要会写 serde 派生，只需知道：配置文件本质上是把一棵 JSON 树反序列化成内部的 `Config` 结构体。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| `zenoh/src/api/config.rs` | 公开 API 层的 `zenoh::Config`，是一个薄封装（newtype），把内部 `zenoh_config::Config` 藏起来，只暴露 `from_file` / `from_json5` / `insert_json5` / `get_json` 等方法。 |
| `commons/zenoh-config/src/lib.rs` | 内部配置 crate 的主体。真正的 `Config` 结构体定义在这里（用宏生成），`from_file` / `insert_json5` / `get_json` 的实现也在这里。 |
| `commons/zenoh-config/src/mode_dependent.rs` | 定义 `ModeDependentValue<T>` 与 `ModeValues<T>`——「同一个值可以随 router/peer/client 角色取不同值」的核心类型。 |
| `commons/zenoh-config/src/defaults.rs` | 配置的**默认值**常量与各配置结构的 `Default` 实现（如默认 `mode = Peer`、默认监听端口）。 |
| `DEFAULT_CONFIG.json5` | 仓库根目录的一份**带详细注释的参考配置**，列出几乎所有可配置项；是写配置文件时最好的模板。 |
| `commons/zenoh-protocol/src/core/whatami.rs` | `WhatAmI` 枚举与 `WhatAmIMatcher` 的定义：三种角色及其字符串、位运算组合表示。 |

> 提醒：`commons/*` 下的 crate 属于 Zenoh **内部实现**（见《u1-l3》），不保证稳定。读源码理解原理时它们是主角；但写应用时你只会用到 `zenoh::Config` 这个公开封装。

## 4. 核心概念与源码讲解

本讲拆为三个最小模块：

- **4.1 Config 结构**：配置树为什么字段私有、怎么读写。
- **4.2 WhatAmI**：三种节点角色与角色匹配器。
- **4.3 默认配置**：`DEFAULT_CONFIG.json5` 与 `defaults.rs` 分别承担什么。

---

### 4.1 Config 结构

#### 4.1.1 概念说明

很多框架的配置就是「一个结构体 + 一堆 `pub` 字段」，用户可以直接 `config.mode = ...`。Zenoh 故意**不**这样做。它的 `Config` 把所有字段设为私有，对外只给四个以「字符串键」为参数的方法：

- `from_file(path)`：从文件加载（支持 `.json` / `.json5` / `.yaml`，实验性支持 `.toml`）。
- `from_json5(s)`：从一段 JSON5 字符串加载。
- `insert_json5(key, value)`：把 `value`（一段 JSON5）写进 `key` 指向的位置。
- `get_json(key)`：把 `key` 指向的子树读出来，返回 JSON 字符串。

为什么这样设计？公开文档说得很直接：

> The zenoh configuration is unstable, so no direct access to the fields is provided.

也就是说，**配置结构本身是会演进的**（今天有 `scouting`，明天可能拆成两个模块）。如果直接暴露字段，一旦内部重构，所有用户代码都会编译失败。改成「字符串键 + JSON5 值」后，内部怎么重构都行，只要用户写的键名仍然有效即可。这种思路在数据库（NoSQL 文档）和 Kubernetes（YAML 清单）里也很常见——**一棵带校验的、弱类型的键值树**。

这棵树用斜杠 `/` 分隔层级，例如：

- `mode` —— 顶层角色。
- `connect/endpoints` —— 连接端点列表。
- `scouting/multicast/enabled` —— 是否启用组播发现。
- `transport/unicast/max_sessions` —— 单播传输最大会话数。

你可以把它想象成一棵树：

```
config
├── id            (ZenohId)
├── mode          (WhatAmI)
├── connect
│   ├── endpoints
│   └── timeout_ms
├── listen
│   └── endpoints
└── scouting
    ├── multicast
    │   ├── enabled
    │   └── address
    └── gossip
        └── enabled
```

#### 4.1.2 核心流程

一次典型的「从配置文件启动」流程是：

```text
1. Config::from_file("my.json5")
     └─ 读文件内容 → 按扩展名选反序列化器(json5/yaml) → 填充内部 Config 结构
2. （可选）config.insert_json5("key", "value")  # 用代码改个别项
3. zenoh::open(config).await
     └─ 内部对 config 做校验 + 补默认值（expanded），生成 Runtime
```

其中第 1 步的「按扩展名选反序列化器」是关键：Zenoh 并不自己写 JSON 解析器，而是复用 `json5` / `serde_yaml` / `toml` 这几个成熟 crate，再用统一的 `Config::from_deserializer` 收口。

读写配置的底层机制是 `validated_struct` 这个 crate 提供的 `ValidatedMap` trait：`insert_json5` / `get_json` 都只是把调用转发给它。它负责「把斜杠路径定位到树里的某个节点」+「按该节点的 Rust 类型校验你塞进去的 JSON5」。

#### 4.1.3 源码精读

**公开封装层：`zenoh::Config`**

公开的 `Config` 就是一个 newtype，把内部 `zenoh_config::Config` 包了一层 `pub(crate)`：

[zenoh/src/api/config.rs:24-44](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/config.rs#L24-L44)

> 中文说明：这段文档注释直白地告诉用户「配置不稳定，所以不暴露字段，只能用 `from_file` / `from_json5` 加载，或用 `insert_json5` / `remove` 修改配置树」。下方 `pub struct Config(pub(crate) zenoh_config::Config);` 就是那个「私有包装」——内部字段对用户不可见。

加载与修改方法都极薄，基本只做错误类型转换：

[zenoh/src/api/config.rs:58-71](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/config.rs#L58-L71)

> 中文说明：`from_file` 直接委托给内部 crate；`from_json5` 则是「拿一段 JSON5 字符串 → 用 `json5::Deserializer` 反序列化 → 包成 `Config`」。注意它把错误分成两种：「正确反序列化但内容不合法」与「JSON 本身解析失败」。

[zenoh/src/api/config.rs:82-99](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/config.rs#L82-L99)

> 中文说明：`insert_json5(key, value)` 把一段 JSON5 写到指定键下；`get_json(key)` 把指定键下的子树读成 JSON 字符串。两者都委托给内部 `zenoh_config::Config`，只做错误类型转换。

**内部结构体：`zenoh_config::Config`**

真正的结构体在内部 crate，用一个 `validator!` 宏生成（宏会同时派生 serde、生成 setter/getter、做字段校验）：

[commons/zenoh-config/src/lib.rs:509-525](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-config/src/lib.rs#L509-L525)

> 中文说明：这里能看到 `Config` 的几个顶层字段——`id`（ZenohId）、`metadata`（任意 JSON 元数据，进 admin space）、`mode`（角色，类型是 `Option<WhatAmI>`）、`region_name`、`gateway`、`connect`、`listen`。注意 `#[serde(deny_unknown_fields)]`：写错字段名会被直接拒绝，避免你把 `listen` 拼错却静默忽略。`mode` 那行注释还特别指出「`router` 是 `zenohd` 的默认值」。

读写的三个核心方法实现也非常短，全部转发给 `ValidatedMap` trait：

[commons/zenoh-config/src/lib.rs:1293-1303](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-config/src/lib.rs#L1293-L1303)

> 中文说明：`get_json` 与 `insert_json5` 都只是 `<Self as ValidatedMap>::...` 的转发——真正「按斜杠路径定位 + 类型校验」的逻辑在 `validated_struct` crate 里，与具体的 Zenoh 配置无关。

**从文件加载：扩展名分派**

`from_file` 先读文件、再按扩展名挑反序列化器：

[commons/zenoh-config/src/lib.rs:1472-1521](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-config/src/lib.rs#L1472-L1521)

> 中文说明：`from_file` 公开入口会额外调用 `plugins.load_external_configs()`（加载插件的外部配置，属于《u11》的内容）；真正的解析在 `_from_file`：它 `match` 文件扩展名——`.json`/`.json5` 用 `json5::Deserializer`，`.yaml`/`.yml` 用 `serde_yaml`，`.toml` 仅在启用 `unstable` feature 时支持（且会打印「可能被移除」的警告）。不认识的扩展名直接 `bail!` 报错。

**「随角色变化」的值：`ModeDependentValue`**

许多配置项「对 router / peer / client 取不同值」。比如默认连接超时：router 和 peer 无限等待，client 立刻失败。Zenoh 用 `ModeDependentValue<T>` 表达这种「单值或三值」的二选一：

[commons/zenoh-config/src/mode_dependent.rs:40-81](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-config/src/mode_dependent.rs#L40-L81)

> 中文说明：`ModeValues<T>` 是一个三元组 `{router, peer, client}`，每个都是 `Option<T>`（可为空表示「该角色不适用」）；`ModeDependentValue<T>` 是 `Unique(T)`（三种角色用同一个值）或 `Dependent(ModeValues)`（三种角色各用各的）二选一。它的 serde 实现让你在配置文件里既能写 `timeout_ms: 0`（Unique），也能写 `timeout_ms: { router: -1, peer: -1, client: 0 }`（Dependent）——这就是 `DEFAULT_CONFIG.json5` 里大量注释「Accepts a single value ... or different values for router, peer and client」的由来。

#### 4.1.4 代码实践

**实践目标**：亲手用代码读写配置树，验证「字符串键」模型。

下面的程序（**示例代码**，仓库中没有现成对应示例）只操作 `Config`，不需要打开会话：

```rust
// 示例代码：演示 from_file / insert_json5 / get_json
use zenoh::Config;

fn main() {
    // 1. 从仓库根目录的参考配置加载
    let mut config = Config::from_file("DEFAULT_CONFIG.json5").unwrap();

    // 2. 读出顶层角色，确认默认值
    println!("mode = {}", config.get_json("mode").unwrap());

    // 3. 改一个嵌套的布尔项：关闭组播发现
    config.insert_json5("scouting/multicast/enabled", "false").unwrap();

    // 4. 改 listen 端点列表（一个数组值）
    config.insert_json5("listen/endpoints", r#"["tcp/127.0.0.1:77477"]"#).unwrap();

    // 5. 读回确认
    println!("scouting/multicast/enabled = {}", config.get_json("scouting/multicast/enabled").unwrap());
    println!("listen/endpoints = {}", config.get_json("listen/endpoints").unwrap());
}
```

**操作步骤**：

1. 在能找到 `zenoh` crate 的环境里（例如直接在 `examples` crate 里加一个临时 example，或自己建一个依赖 `zenoh` 的小 crate）新建上述 `main`。
2. 把工作目录设为仓库根，保证 `DEFAULT_CONFIG.json5` 路径可达；或改成绝对路径。
3. 编译运行：`cargo run`。

**需要观察的现象**：

- 第 2 步打印出 `mode = "peer"`（参考配置里 `mode: "peer"`）。
- 第 5 步打印出 `scouting/multicast/enabled = false` 与 `listen/endpoints = ["tcp/127.0.0.1:77477"]`，证明写入生效。

**预期结果**：`get_json` 读回的值与你 `insert_json5` 写入的完全一致，说明配置树确实被就地修改了。如果故意把键写错（例如 `scouting/multimistake/enabled`），`insert_json5` 会返回错误，体现 `deny_unknown_fields` 的校验。**待本地验证**：若你的 `zenoh` 启用了不同 feature，个别键的默认值可能略有差异。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `zenoh::Config` 不直接暴露 `pub mode: WhatAmI` 字段，而要用字符串键 `insert_json5("mode", ...)`？

> **参考答案**：因为配置结构本身被声明为 unstable（会随版本演进）。暴露字段意味着内部一重构，用户代码就编译失败。改用字符串键 + JSON5 值后，内部结构可以自由变化，只要保留键名兼容即可；同时配合 `#[serde(deny_unknown_fields)]` 还能在运行时拦截拼错的键名。

**练习 2**：`Config::from_file` 收到一个后缀为 `.toml` 的文件会发生什么？`.txt` 呢？

> **参考答案**：`.toml` 仅在启用 `unstable` feature 时被支持（用 `toml::Deserializer` 解析，并打印「不稳定、未来可能移除」的警告）；未启用 `unstable` 时会落入 `Some(other) => bail!(...)` 分支报「不支持」。`.txt` 则一定命中 `Some(other)` 分支，报「Unsupported file type，仅支持 .json/.json5/.yaml」。

**练习 3**：`ModeDependentValue<bool>` 在配置文件里既可以写成 `enabled: true` 又可以写成 `enabled: { router: true, peer: false, client: false }`，这是怎么做到的？

> **参考答案**：因为 `ModeDependentValue` 手写了 serde 的 `Deserialize`（见 `mode_dependent.rs`），它先尝试把值当成单个标量（→ `Unique`），失败再当成 `{router, peer, client}` 三元组（→ `Dependent`）。这就是 `DEFAULT_CONFIG.json5` 里大量「Accepts a single value ... or different values」注释的底层依据。

---

### 4.2 WhatAmI

#### 4.2.1 概念说明

`WhatAmI` 回答的是「**这个节点在网络里扮演什么角色**」。Zenoh 有三种角色，它们决定了节点如何发现彼此、如何路由消息：

| 角色 | 字符串 | 行为特点 |
| --- | --- | --- |
| **Router** | `"router"` | 路由器。维护预定义的网络拓扑，**不**自己主动发现节点，依赖静态配置（监听端口等别人连上来）。常作为基础设施部署。 |
| **Peer** | `"peer"` | 对等节点。**默认角色**。主动搜索其他节点并建立直连（可用组播发现 + gossip）。适合能互相可达的 mesh。 |
| **Client** | `"client"` | 客户端。只连**一个**接入点（router 或 peer），由该接入点作为网关转发。适合算力/带宽受限的设备。 |

> 关键区分（来自上一讲与源码注释）：**本端角色只能通过配置里的 `mode` 来观察**，公开 API 没有直接读「本端 whatami」的方法。`Transport::whatami()` 读到的是**对端**的角色。

`WhatAmI` 还有一个搭档类型 `WhatAmIMatcher`，用于「一次匹配多种角色」，主要用在发现（scouting）时过滤「我想自动连哪些类型的节点」。

#### 4.2.2 核心流程

`WhatAmI` 的设计把「一个角色」当成一个比特位：

\[
\text{Router} = 0b001,\quad \text{Peer} = 0b010,\quad \text{Client} = 0b100
\]

这样「匹配多种角色」就是按位或：

\[
\text{router} \,|\, \text{peer} = 0b001 \,|\, 0b010 = 0b011
\]

于是 `WhatAmIMatcher` 内部就是一个 `u8` 位掩码，判断「是否匹配某个角色」就是按位与：

\[
\text{matches}(w) \iff (\text{mask}\ \&\ w) \ne 0
\]

这套位编码让「router|peer|client」这样的组合可以用一个整数表达，序列化时再展开成 `["router","peer","client"]` 数组。它的意义在于：发现协议里要频繁表示「目标角色集合」，位运算比字符串集合更紧凑、更快。

三种角色之间还有一个**默认值**约定：`WhatAmI::default()` 是 `Peer`（见下方源码），所以「不配置 mode」时节点就是 peer。

#### 4.2.3 源码精读

**枚举定义与字符串**

[commons/zenoh-protocol/src/core/whatami.rs:21-45](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/core/whatami.rs#L21-L45)

> 中文说明：文档注释详细描述了三种模式的网络行为（peer 用组播/gossip 发现、client 只连一个接入点、router 依赖静态配置）。枚举体里 `Router = 0b001`、`Peer = 0b010`、`Client = 0b100`，并且 `#[default] Peer`——所以 `WhatAmI::default()` 是 Peer，这解释了为什么「不配置 mode 就是 peer」。

[commons/zenoh-protocol/src/core/whatami.rs:48-62](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/core/whatami.rs#L48-L62)

> 中文说明：三个字符串常量 `"router"`/`"peer"`/`"client"`，以及 `to_str()` 把枚举转成这些字符串。这就是配置文件里 `mode: "peer"` 能被识别的原因。

[commons/zenoh-protocol/src/core/whatami.rs:101-117](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/core/whatami.rs#L101-L117)

> 中文说明：`FromStr` 让 `"peer".parse::<WhatAmI>()` 能工作，配置文件里的字符串就这样被反序列化成枚举；非法字符串会 `bail!` 报错并列出三个合法值。

**serde 行为**

[commons/zenoh-protocol/src/core/whatami.rs:309-316](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/core/whatami.rs#L309-L316)

> 中文说明：`WhatAmI` 的 serde 序列化固定写成字符串（`serializer.serialize_str(self.to_str())`），所以 `get_json("mode")` 返回的是 `"peer"` 而不是 `2`。`WhatAmIMatcher` 则序列化成一个字符串数组（见同文件 364–378 行），对应配置里 `autoconnect: ["router", "peer"]` 这种写法。

**WhatAmIMatcher：角色集合**

[commons/zenoh-protocol/src/core/whatami.rs:131-149](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/core/whatami.rs#L131-L149)

> 中文说明：`WhatAmIMatcher` 是对 `NonZeroU8` 的透明包装（`#[repr(transparent)]`），内部就是位掩码。它预留了第 7 位（`U8_0 = 1<<7`）来保证「非空」（因为 `NonZeroU8` 不能存 0）。`empty()` 是空集合，`router()`/`peer()`/`client()` 用按位或把对应角色加进集合。

[commons/zenoh-protocol/src/core/whatami.rs:292-300](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/core/whatami.rs#L292-L300)

> 中文说明：`WhatAmI | WhatAmI` 通过重载 `BitOr` 直接得到一个 `WhatAmIMatcher`，例如 `WhatAmI::Router | WhatAmI::Peer` 表示「匹配 router 或 peer」。这正是 `scout` 函数（见《u6-l1》）用来指定「我想发现哪类节点」的类型。

#### 4.2.4 代码实践

**实践目标**：直观感受 `mode` 配置如何影响节点。这里用「对比实验」，而不是写新代码。

**操作步骤**：

1. 复用《u2-l1》里那个「打开 Session 并打印 zid」的小程序（或直接用 `examples/examples/z_info.rs`）。
2. 第一次运行：用默认配置（`Config::default()`，即 peer）。开两个终端各跑一个 peer，观察它们通过组播发现彼此并建立连接（`z_info` 在启用 `unstable` 时会打印 `transports` / `links`）。
3. 第二次运行：在代码里加上 `config.insert_json5("mode", "\"client\"").unwrap();`，再让这个 client 去连一个 peer/router（需要先有一个对端在监听），观察 client 只维持**一条**到接入点的连接。

**需要观察的现象**：

- peer 模式下，两个 peer 能互相发现并互联（看到对端 zid 出现在 `peers_zid`）。
- client 模式下，节点不会主动组播发现，只会连你指定的端点，且只保持单连接。

**预期结果**：`mode` 从 `peer` 改成 `client` 后，节点的连接行为从「主动发现多对端」变成「被动连单点」，这正是 `WhatAmI` 角色驱动的网络拓扑差异。**待本地验证**：组播发现在某些受限网络/容器环境下默认不可用，若 peer 间未自动互联，可改用显式 `connect/endpoints`。

#### 4.2.5 小练习与答案

**练习 1**：`WhatAmI::default()` 返回什么？这与 `DEFAULT_CONFIG.json5` 里的 `mode: "peer"` 一致吗？

> **参考答案**：返回 `WhatAmI::Peer`（枚举上标了 `#[default]`）。与参考配置里的 `mode: "peer"` 完全一致——peer 是 Zenoh 的默认角色。

**练习 2**：`WhatAmIMatcher` 为什么内部用 `NonZeroU8` 而不是普通 `u8`？

> **参考答案**：为了让「空集合」与「非空集合」在类型上区分开，并且 `NonZeroU8` 有更好的内存布局优化（与 `Option<NonZeroU8>` 等大）。它用第 7 位（`1<<7`）作为「非零标记」，低 3 位分别表示 router/peer/client 是否在集合中。

**练习 3**：`get_json("mode")` 返回 `"peer"` 而不是数字 `2`，是哪段代码决定的？

> **参考答案**：`WhatAmI` 的 `serde::Serialize` 实现（`whatmai.rs:309-316`）固定调用 `serializer.serialize_str(self.to_str())`，所以无论内部判别值是 `0b010`，序列化出来永远是字符串 `"peer"`。

---

### 4.3 默认配置

#### 4.3.1 概念说明

Zenoh 的「默认值」来自**两个地方**，理解它们的分工是本模块的重点：

1. **`DEFAULT_CONFIG.json5`（仓库根目录）**：一份**给人看**的、带详尽注释的参考配置。它列出几乎所有可配置项，并解释每一项的含义、取值与默认行为。但文件头有一句重要警告：

   > the values here are correctly typed, but may not be sensible, so copying this file to change only the parts that matter to you is **not good practice**.

   也就是说，它不是「生产默认值清单」，而是「配置项字典 + 示例」。**正确的做法**是：从空配置（或 `Config::default()`）出发，只覆盖你关心的键。

2. **`commons/zenoh-config/src/defaults.rs`（代码里）**：真正的**运行时默认值**。它用 Rust 常量和 `impl Default for XxxConfig` 给出每个字段在「用户没配」时的取值。例如 `mode = Peer`、router 默认监听 `tcp/[::]:7447`、peer 默认监听 `tcp/[::]:0`（随机端口）。

两者不冲突：代码里的 `Default` 是「真默认」，`DEFAULT_CONFIG.json5` 是「文档/模板」。当你 `Config::default()` 时，拿到的是代码默认值；当你 `Config::from_file("DEFAULT_CONFIG.json5")` 时，拿到的是文件里显式写出的值（覆盖了代码默认）。

另外还有一个「补全」步骤 `expanded()`：在真正进入 Runtime 前，Zenoh 会确保 `id` 与 `mode` 一定有值（没有就补随机 id 与默认 Peer）。

#### 4.3.2 核心流程

默认值产生与覆盖的流程：

```text
Config::default()        # 走 defaults.rs 里各结构的 Default impl
   │
   ├─ from_file / from_json5   # 用文件/字符串里的值覆盖对应键
   ├─ insert_json5(...)        # 用代码再覆盖个别键
   │
   └─ expanded()          # 进入 Runtime 前补全：id 缺→随机，mode 缺→Peer
```

几个值得记住的默认值：

| 配置项 | 默认值 | 出处 |
| --- | --- | --- |
| `mode` | `peer` | `defaults.rs` 的 `pub const mode: WhatAmI = WhatAmI::Peer;` |
| `listen/endpoints`（router） | `tcp/[::]:7447` | `impl Default for ListenConfig` |
| `listen/endpoints`（peer） | `tcp/[::]:0`（随机端口） | 同上 |
| `listen/endpoints`（client） | 无（`None`） | 同上 |
| `scouting/multicast/enabled` | `true` | `defaults.rs` 的 `scouting::multicast::enabled` |
| `scouting/multicast/address` | `224.0.0.224:7446` | 同上 |
| `scouting/timeout` | `3000` ms | 同上 |

#### 4.3.3 源码精读

**代码默认值：mode = Peer**

[commons/zenoh-config/src/defaults.rs:29-31](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-config/src/defaults.rs#L29-L31)

> 中文说明：`pub const mode: WhatAmI = WhatAmI::Peer;`——这就是「不配 mode 就是 peer」的代码级依据。注意它带了 `#[allow(dead_code)]`，因为这个常量是被宏生成的 setter 在运行时引用的。

**代码默认值：监听端点（随角色不同）**

[commons/zenoh-config/src/defaults.rs:164-184](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-config/src/defaults.rs#L164-L184)

> 中文说明：`impl Default for ListenConfig` 用 `ModeDependentValue::Dependent` 给出三种角色各自的默认监听端点——router 监听固定的 `tcp/[::]:7447`，peer 监听随机端口 `tcp/[::]:0`，client 为 `None`（客户端不监听、只主动连）。注意它们还被 `#[cfg(feature = "transport_tcp")]` 门控：没启用 TCP feature 时端点列表为空。

**代码默认值：scouting（发现）**

[commons/zenoh-config/src/defaults.rs:69-103](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-config/src/defaults.rs#L69-L103)

> 中文说明：scouting 的默认值——`timeout=3000`ms、`delay=500`ms、组播 `enabled=true`、组播地址 `([224,0,0,224], 7446)`（即 `224.0.0.224:7446`）、`ttl=1`。还有 `autoconnect` 子模块：peer/client 默认会自动连 router|peer|client（见 `empty().router().peer().client()`），而 router 默认 `empty()`（不自动连任何人）。

**补全：expanded()**

[commons/zenoh-config/src/lib.rs:1538-1548](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-config/src/lib.rs#L1538-L1548)

> 中文说明：`expanded()` 在进入 Runtime 前调用——若 `id` 为空就补一个随机 `ZenohId::default()`，若 `mode` 为空就补 `WhatAmI::default()`（即 Peer）。返回的 `ExpandedConfig` 保证所有 getter 都不会失败（`id()` / `mode()` 直接返回值而非 `Option`）。这就是「配置里的 `id`/`mode` 都是 `Option<...>`，但运行时一定有值」的原因。

**参考配置文件：DEFAULT_CONFIG.json5**

[DEFAULT_CONFIG.json5:11-12](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/DEFAULT_CONFIG.json5#L11-L12)

> 中文说明：文件里显式写出 `mode: "peer"`，与代码默认一致；但更重要的是文件头第 1–3 行的说明——它是「带文档的配置项清单」，值「类型正确但不一定合理」，不应整份拷贝来改。

[DEFAULT_CONFIG.json5:93-104](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/DEFAULT_CONFIG.json5#L93-L104)

> 中文说明：`listen.endpoints` 这里写成 `{ router: ["tcp/[::]:7447"], peer: ["tcp/[::]:0"] }`——即 Dependent 形态，正好对应 `defaults.rs` 里 `ListenConfig` 的默认值。文件注释还详尽说明了端点字符串的各种修饰语法（`#iface=`、`?prio=`、`?rel=`、`#bind=` 等）。

[DEFAULT_CONFIG.json5:139-149](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/DEFAULT_CONFIG.json5#L139-L149)

> 中文说明：`scouting.multicast` 段——`enabled: true`、`address: "224.0.0.224:7446"`、`ttl: 1`，与 `defaults.rs` 的常量一一对应。这也印证了「文件值 = 代码默认值的可读副本」。

#### 4.3.4 代码实践

**实践目标**：对照「代码默认值」与「参考配置文件」，确认二者一致，并亲手做一次「从空配置只覆盖必要项」的合理配置。

**操作步骤**：

1. 运行下面这段**示例代码**，打印空配置的几个默认值：

   ```rust
   // 示例代码：观察代码默认值
   use zenoh::Config;

   fn main() {
       let config = Config::default();   // 空配置，全部走代码默认
       println!("mode        = {:?}", config.get_json("mode"));                   // 注意：默认未显式设 mode，可能为 null
       println!("scouting/multicast/enabled = {:?}", config.get_json("scouting/multicast/enabled"));
       println!("listen/endpoints = {:?}", config.get_json("listen/endpoints"));
   }
   ```

2. 把输出与 `defaults.rs` 的常量、`DEFAULT_CONFIG.json5` 的值三方对照。
3. 实践「只覆盖必要项」：从 `Config::default()` 出发，仅 `insert_json5("listen/endpoints", r#"["tcp/127.0.0.1:77477"]"#)` 和 `insert_json5("mode", "\"client\"")`，得到一个最小可用配置。

**需要观察的现象**：

- 空配置下，`scouting/multicast/enabled` 等读取到的值应与 `defaults.rs` 一致（`true` 等）。
- `mode` 在纯 `Config::default()` 下可能是 `null`（因为 `mode` 字段是 `Option`，`Default` 不一定填），直到 `expanded()` 才补成 `peer`——这正是 `expanded()` 存在的意义。

**预期结果**：你能确认「代码默认值 = 参考配置文件里写出的值」，并且理解「从空配置出发只改几个键」才是推荐用法，而非整份拷贝 `DEFAULT_CONFIG.json5`。**待本地验证**：`Config::default()` 下某些 `Option` 字段读出来是 `null` 还是默认值，取决于宏生成的默认实现，以本地实际输出为准。

#### 4.3.5 小练习与答案

**练习 1**：`DEFAULT_CONFIG.json5` 可以直接当作生产配置用吗？为什么？

> **参考答案**：不建议整份拷贝使用。文件头明确说「值类型正确但不一定合理，拷贝来只改一部分不是好做法」。它是「配置项字典 + 示例」，用来查「有哪些键、各键含义」。正确做法是从 `Config::default()`（代码默认值）出发，只覆盖你需要的键。

**练习 2**：为什么 `Config` 里 `mode` 字段类型是 `Option<WhatAmI>`，但运行时却一定有值？

> **参考答案**：因为配置允许「用户不写 mode」，所以字段是 `Option`。但进入 Runtime 前会调用 `expanded()`，它检测到 `mode.is_none()` 时补上 `WhatAmI::default()`（Peer），返回的 `ExpandedConfig` 保证 `mode()` 一定有值。

**练习 3**：router 和 peer 的默认监听端点有什么不同？为什么 peer 用 `tcp/[::]:0`？

> **参考答案**：router 默认监听固定端口 `tcp/[::]:7447`（因为它是基础设施，别的节点要预先知道连哪里）；peer 默认监听 `tcp/[::]:0`（端口 0 表示由操作系统随机分配），因为 peer 之间靠发现（组播/gossip）互相找，不需要固定端口；client 默认 `None`（不监听，只主动连）。

---

## 5. 综合实践

把本讲三个模块串起来：**写一个「可切换角色」的最小 Zenoh 配置生成器**。

任务：

1. 写一个函数 `build_config(mode: &str, listen_port: u16, multicast: bool) -> zenoh::Config`。
2. 函数内部从 `Config::default()` 出发，用 `insert_json5` 做三件事：
   - 设 `mode` 为传入参数（`"router"` / `"peer"` / `"client"`）；
   - 设 `listen/endpoints` 为 `["tcp/127.0.0.1:<listen_port>"]`；
   - 设 `scouting/multicast/enabled` 为传入的 `multicast` 布尔值。
3. 用 `get_json` 把设好的三个值打印出来，自检无误。
4. 思考题：当 `mode = "client"` 时，`listen/endpoints` 还有意义吗？（提示：回顾 `ListenConfig` 默认值里 client 为 `None`。）

**检验标准**：

- 三次 `get_json` 读回的值与传入参数完全一致。
- 能说清楚：`from_file` / `from_json5`（4.1 的加载）、`WhatAmI` 三种角色的语义（4.2）、以及「从空配置出发只覆盖必要项」优于整份拷贝 `DEFAULT_CONFIG.json5`（4.3）。

这是一个纯配置层的小任务，不需要真正建立网络连接；当你之后在《u3》写 pub/sub、在《u6》做 scouting 时，会反复用到这里生成的 `Config`。

## 6. 本讲小结

- `zenoh::Config` 是对内部 `zenoh_config::Config` 的私有封装，字段不公开，只能用 `from_file` / `from_json5` / `insert_json5` / `get_json` / `remove` 以「字符串键 + JSON5 值」的方式读写——因为配置结构本身 unstable，这样内部重构不会破坏用户代码。
- `from_file` 按扩展名分派：`.json`/`.json5` 用 `json5`，`.yaml`/`.yml` 用 `serde_yaml`，`.toml` 仅 `unstable` 支持；底层统一收口于 `Config::from_deserializer`。
- `WhatAmI` 是三种节点角色 Router / Peer / Client，默认 Peer；它用比特位编码，`WhatAmIMatcher` 用位掩码表达「角色集合」，serde 下 `WhatAmI` 序列化为字符串、`WhatAmIMatcher` 序列化为字符串数组。
- `ModeDependentValue<T>`（`Unique` / `Dependent`）让同一个配置项对三种角色取不同值，这是配置文件里大量「单值或 `{router,peer,client}`」写法的底层依据。
- 默认值有两个来源：`defaults.rs`（代码运行时真默认，如 `mode=Peer`、router 监听 `7447`）与 `DEFAULT_CONFIG.json5`（给人看的带注释参考清单，不宜整份拷贝）。
- `expanded()` 在进入 Runtime 前补全缺失的 `id`（随机）与 `mode`（Peer），保证运行时一定有值。

## 7. 下一步学习建议

本讲把「配置 + 角色」补齐后，你已经具备了打开会话、设置角色、改监听端点的全部前置知识。接下来：

- **进入《u3 发布/订阅》**：用本讲生成的 `Config` 打开 `Session`，真正开始收发 `Sample`，把「配置」变成「能跑的通信」。
- **若对发现机制好奇**：可先跳读《u6-l1 Scouting》，那里会用到本讲的 `WhatAmIMatcher`（`WhatAmI::Router | WhatAmI::Peer`）来过滤要发现的节点类型，是本讲位运算设计的直接应用。
- **后续内部向**：当你想理解「配置在 Runtime 里如何被消费」，可阅读 `commons/zenoh-config/src/lib.rs` 中 `expanded()` 之后的 `ExpandedConfig`，以及《u7》Runtime 编排器如何读取 `connect`/`listen`/`scouting` 来建连——本讲是那条调用链的起点。
