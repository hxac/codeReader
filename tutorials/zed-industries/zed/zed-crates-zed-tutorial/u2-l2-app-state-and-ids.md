# u2-l2 AppState、身份标识与遥测

## 1. 本讲目标

上一讲（u2-l1）我们把 `app.run` 闭包里的近百个 init 调用看作一条分层流水线，并建立了 `Global`、`xxx::init(cx)`、`observe_global` 这些词汇。本讲把镜头推近到这条流水线中段的一段「身份与遥测」装配，学完后你应当能够：

1. 说出 `AppState` 这个全局枢纽结构里装了哪些句柄、它们分别在 init 序列的哪一步就绪、为什么它只能在流水线中段构造。
2. 区分 Zed 的三个身份标识：`system_id`（一台机器）、`installation_id`（一次安装）、`session_id`（一次运行），并读懂它们的持久化载体（SQLite KVP 表）与读取/迁移逻辑。
3. 解释 `telemetry::start`、`App First Opened` 系列事件的判定矩阵，以及 `authenticate` 在终端（pty）与非终端两种启动方式下的行为差异。

## 2. 前置知识

- **KVP（Key-Value Pair）存储**：Zed 不用独立的配置数据库，而是在 SQLite 里建了一张极简的 `kv_store(key TEXT PRIMARY KEY, value TEXT)` 表，通过 `db` crate 的 `KeyValueStore` 读写。你可以把它理解为「藏在 sqlite 文件里的一个持久化 HashMap」。
- **UUID v4**：128 位随机标识符，Zed 用 `Uuid::new_v4()` 生成新身份 ID，再转成字符串存进 KVP。它只要求「全局几乎不重复」，不承载任何语义。
- **pty（pseudo-terminal，伪终端）**：程序从 shell 手动启动时，它的标准输出连接的是一个终端设备；从桌面图标启动时则没有。Rust 标准库的 `io::stdout().is_terminal()` 可以区分这两种情况。Zed 大量利用这一点决定「CLI 模式」还是「GUI 模式」行为（u1-l4 已在日志初始化中见过一次）。
- **遥测（telemetry）**：产品匿名上报使用事件（如「应用被打开」「设置被修改」）的机制。Zed 的遥测默认可用设置开关控制，事件先进入进程内队列，再由后台任务上报。
- **GPUI Entity 与 Global**：`Entity<T>` 是 GPUI 里带版本管理的共享状态句柄；`Global` 是「类型即键」的全局存取机制（u2-l1 已讲）。本讲会看到二者的组合：`AppState` 作为一个 Global，内部又持有 `Entity<AppSession>`。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `crates/zed/src/main.rs` | 主程序入口，本讲主角 | 身份 ID 的后台生成与前台等待、`telemetry::start`、首启事件判定、`AppState` 构造、`authenticate` |
| `crates/session/src/session.rs` | 会话类型定义 | `Session` 与 `AppSession` 的分工、旧会话 ID 与窗口栈的持久化 |
| `crates/db/src/kvp.rs` | KVP 存取 | `kv_store` 表结构、`read_kvp`/`write_kvp`、`GlobalKeyValueStore` |
| `crates/db/src/db.rs` | 数据库打开与作用域 | `0-global` 与 `0-{channel}` 两种数据库作用域 |
| `crates/client/src/telemetry.rs` | 遥测实现 | `start` 如何填充状态、事件队列的接线 |
| `crates/telemetry/src/telemetry.rs` | `event!` 宏与事件队列 | 事件的进程内流转 |
| `crates/client/src/client.rs` | 云端客户端 | `IMPERSONATE_LOGIN`、`has_credentials`、`sign_in_with_optional_connect` |
| `crates/zed/src/zed.rs` | 装配逻辑 | 测试侧 `init_test_with_state` 如何复用同一套 `AppState` 装配 |

> 说明：前四个文件中只有 `main.rs`、`zed.rs` 位于 `crates/zed` 内；对其余文件，本讲使用仓库根路径的永久链接（同一 HEAD commit）。

## 4. 核心概念与源码讲解

### 4.1 AppState 构成：全局枢纽与 AppSession/Session 分工

#### 4.1.1 概念说明

`AppState` 定义在 `workspace` crate 中，是一个纯数据结构：八个字段、没有方法逻辑，唯一的使命是把「构建一个窗口所需的全部外部句柄」打包成一个 `Arc`，注册为 GPUI 全局。这样一来，任何深层模块拿到 `App` 上下文就能取回这些句柄，而不必把八个参数一层层传下去。

它内部持有一个特殊成员：`session: Entity<AppSession>`。这里存在一对容易混淆的类型：

- `Session`：**纯数据**。三个字段——本次 `session_id`、上一次运行的 `old_session_id`、上一次运行结束时的窗口栈 `old_window_ids`。它在 `app.run` 之前就能在后台线程构造完成（因为它只依赖 KVP 读取）。
- `AppSession`：**GPUI Entity**。包住 `Session`，并追加两个只有拿到 GPUI 上下文才能注册的东西：一个每 500ms 把当前窗口栈写入 KVP 的序列化任务，和一个 `on_app_quit` 退出钩子。

拆成两个类型的原因在本讲的场景里看得很清楚：`app.run` 一开始就需要 `session.id()` 去启动遥测（此时 `Session` 已就绪），而窗口栈序列化必须等窗口系统跑起来才有意义。

#### 4.1.2 核心流程

`AppState` 的构造发生在 init 序列的中段，时序上严格位于其全部依赖就绪之后：

```
client 构造（L526） → languages/user_store/workspace_store/node_runtime 就绪
        ↓
block_on 等待三个后台身份任务（L595-597，见 4.2）
        ↓
telemetry.start（L600，见 4.3） → 首启事件判定（L628-641）
        ↓
cx.new(AppSession::new(session, cx))（L642）
        ↓
Arc::new(AppState { ... }) + AppState::set_global（L644-654）
        ↓
后续 init 以 app_state 为参数展开：
  reliability::init(... workspace_store ...)
  extension_host::init(... fs, client, node_runtime ...)
  workspace::init(app_state.clone(), cx)
  agent_ui::init(... fs, languages, ...)
```

这正是 u2-l1 所讲「`set_global` 必须先于所有 `global::<T>()` 读取」的具体体现：`AppState` 的每个字段都是前面某一步 init 的产物，少一步就构造不出来。

#### 4.1.3 源码精读

`AppState` 的构造点在 [src/main.rs:L642-L654](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L642-L654)：先用 `cx.new` 把 `Session` 包装成 `AppSession` 实体，再把八个句柄装进结构体并 `AppState::set_global` 注册为全局。

结构体本身定义在 workspace crate，见 [crates/workspace/src/workspace.rs:L1122-L1135](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/workspace/src/workspace.rs#L1122-L1135)：字段依次是 `languages`（语言注册表）、`client`（云端客户端）、`user_store`（用户信息）、`workspace_store`（工作区登记处）、`fs`（文件系统抽象）、`build_window_options`（一个函数指针，指向 zed crate 的窗口选项工厂，u4-l1 会精读）、`node_runtime`（Node 运行时）、`session`。紧接着的 `struct GlobalAppState(Arc<AppState>)` + `impl Global for GlobalAppState {}` 就是 GPUI 全局注册的标准写法。

`Session` 与 `AppSession` 的定义见 [crates/session/src/session.rs:L5-L14](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/session/src/session.rs#L5-L14)：`Session` 只有三个数据字段，两个 KVP 键常量（`session_id` 与 `session_window_stack`）紧随其后。

`Session::new` 的读取-覆盖顺序是理解「上次会话」的关键，见 [crates/session/src/session.rs:L15-L38](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/session/src/session.rs#L15-L38)：先读旧的 `session_id` 存为 `old_session_id`，再把本次新 ID 覆盖写回，最后读取旧窗口栈 JSON 并反序列化为 `Vec<WindowId>`。注意 `write_kvp` 的错误只是 `log_err()` 记录——会话 ID 写失败不应让编辑器启动失败。

`AppSession::new` 见 [crates/session/src/session.rs:L70-L103](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/session/src/session.rs#L70-L103)：非测试构建下 `cx.spawn` 一个无限循环，每 500ms 比较当前窗口栈与上次落盘值，变化才写库；测试构建下用 `Task::ready(())` 占位，注释说明了原因（无限循环会绕过 GPUI 测试的「禁止 park」检查导致挂起而非 panic）。

对外只读接口见 [crates/session/src/session.rs:L114-L129](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/session/src/session.rs#L114-L129)：`id()` 转发给内部 `Session`，`last_session_id()` 与 `last_session_window_stack()` 把旧会话信息暴露给恢复逻辑。`main.rs` 中实际消费它们的位置在 [src/main.rs:L915-L921](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L915-L921)——取出当前/上一会话 ID，交给工作区垃圾回收任务（u2-l3 会展开恢复全流程）。

另一个值得注意的用法在 [src/main.rs:L465-L476](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L465-L476)：macOS 的「点击 Dock 图标重新打开」回调里用的是 `AppState::try_global(cx)` 而非会 panic 的 `global`——这个回调注册于 `app.run` 之前，必须防御 `AppState` 尚未注册的情况。

#### 4.1.4 代码实践：AppState 字段考古

1. **实践目标**：验证「`AppState` 的每个字段都在构造前某步 init 中诞生」，建立字段 → 诞生地 → 消费地的映射。
2. **操作步骤**：
   - 在 `crates/zed/src/main.rs` 中搜索 `app_state.`，记录每个字段名出现的行号。
   - 对每个字段向上追溯它的绑定语句（如 `let user_store = cx.new(...)` 在 L562）。
   - 向下找到至少一个把它传给后续 init 的调用（如 `reliability::init(client, app_state.workspace_store.clone(), cx)`）。
   - 对照测试侧装配 [src/zed.rs:L6142-L6144](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/zed.rs#L6142-L6144) 与 [src/zed.rs:L6156-L6168](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/zed.rs#L6156-L6168)：测试用 `AppState::test` 生成状态，再走 `AppState::set_global` + 精简版 init 序列。
3. **需要观察的现象**：`app_state.` 的命中会密集出现在 L654（set_global）之后；`build_window_options` 字段在测试装配里被单独补写（`state.build_window_options = build_window_options`），因为 `AppState::test` 无法引用 zed crate 的这个函数。
4. **预期结果**：得到一张八行三列的表（字段 / 诞生行 / 消费 init），能直观看出 `AppState` 是流水线中段的「汇总点」。

#### 4.1.5 小练习与答案

**练习 1**：`Session` 和 `AppSession` 各保存什么？为什么要拆成两个类型？

答案：`Session` 只保存数据（本次 `session_id`、上次 `old_session_id`、上次窗口栈 `old_window_ids`），不依赖 GPUI，可在后台线程构造；`AppSession` 是 GPUI Entity，包住 `Session` 并追加 500ms 窗口栈序列化任务和 `on_app_quit` 钩子。拆分是因为遥测在 `app.run` 一开始就需要 `session.id()`，而窗口栈序列化只有进入 GPUI 世界后才可能实现。

**练习 2**：如果把 `AppState::set_global` 提前到 `let client = Client::production(cx)` 之前，会发生什么？

答案：根本编译/运行不过——`AppState` 的字段 `user_store`、`workspace_store`、`session` 等此时还不存在，无法构造该结构体。这正是 init 顺序不可调换的具体原因：每个字段的诞生语句都是流水线上更早的一步。

**练习 3**：`app.on_reopen` 回调为什么用 `AppState::try_global(cx)`？

答案：该回调在 `app.run` 之前注册（L465），而 `AppState` 在 `app.run` 内部才 `set_global`；若用户操作在注册与全局设置之间触达回调，`global` 版本会 panic，`try_global` 返回 `Option` 让代码安全跳过。

### 4.2 身份标识与 KVP 存储：system_id、installation_id、session_id

#### 4.2.1 概念说明

Zed 用三个粒度递减的 UUID 标识一次运行所依附的实体：

| 标识 | 粒度 | 生成时机 | 存储位置 |
| --- | --- | --- | --- |
| `system_id` | 一台机器（跨 Zed 通道共享） | 首次读取失败时 | `0-global` 作用域数据库 |
| `installation_id` | 一次安装（按发布通道隔离） | 首次读取失败时，或从 legacy `device_id` 迁移 | `0-{channel}` 作用域数据库 |
| `session_id` | 一次进程运行 | 每次启动 `Uuid::new_v4()` | 仅写入 KVP 供下次读取，不做迁移 |

读取结果统一用 main.rs 私有的 `IdType` 枚举表达——`New(String)`（本次新生成）或 `Existing(String)`（此前已存在）。这个「新旧」信息本身不上报，却直接决定 4.3 的首启遥测事件。

承载它们的是 `db` crate 的 KVP 表。这里有一个容易忽略的设计：Zed 的数据目录下有**两个** SQLite 作用域。`AppDatabase::new()` 按当前发布通道打开（dev 构建即 `0-dev/db.sqlite`），而 `GlobalKeyValueStore` 使用 `GlobalDbScope` 打开 `0-global/db.sqlite`。`system_id` 走后者——它的语义是「这台机器」，所以 stable、dev、preview 多通道共存时应当共享同一个值；`installation_id` 走前者——每个通道的安装各自计数。

#### 4.2.2 核心流程

身份生成横跨 `app.run` 前后，是「后台提前算、前台同步等」模式的典型样本：

```
app.run 之前（L345-354）：
  app_db = AppDatabase::new()            # 打开 0-{channel}/db.sqlite
  后台任务1: system_id()                  # 内部用 GlobalKeyValueStore::global()（0-global）
  后台任务2: installation_id(KVS(app_db)) # 显式传入按通道的 KVP 句柄
  session_id = Uuid::new_v4()             # 纯内存生成
  后台任务3: Session::new(session_id, KVS(app_db))

app.run 之内（L595-597）：
  前台 block_on 三个任务 → 拿到 IdType / Session
```

之所以提前到 `app.run` 之前 spawn，是因为 SQLite 读取是磁盘 I/O，放到后台执行可以与 `app.run` 前的其余准备工作（单实例检查、crash handler 安装、文件系统构造等）并行；而 `app.run` 内的 `block_on` 是一个显式同步点——遥测启动必须先拿到 ID。

`installation_id` 的三分支判定（先查 legacy 键）：

```
读 device_id（legacy）──命中──→ 写 installation_id = 旧值；删除 device_id；返回 Existing
        │ 未命中
读 installation_id ────命中──→ 返回 Existing
        │ 未命中
生成 UUID v4 ──写入──→ 返回 New
```

#### 4.2.3 源码精读

后台任务的发起处在 [src/main.rs:L345-L354](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L345-L354)：注意三个任务拿到的存储句柄并不相同——`system_id()` 无参数（函数内部自取全局库），`installation_id` 与 `Session::new` 都接收 `KeyValueStore::from_app_db(&app_db)` 派生的按通道句柄。

前台同步等待见 [src/main.rs:L595-L597](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L595-L597)：`block_on(...).ok()` 把 `Result<IdType>` 折叠成 `Option<IdType>`——数据库读取失败不会阻断启动，只是让本次运行「没有身份」。

`system_id` 见 [src/main.rs:L1381-L1394](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L1381-L1394)：读全局库的 `system_id` 键，命中即 `Existing`；未命中生成 UUID 写回并返回 `New`。写入用的是 `?` 传播——读失败可以容忍，但**新生成后写失败**会让函数整体返回 `Err`（避免「认为已持久化实则丢失」的错账）。

`installation_id` 见 [src/main.rs:L1396-L1416](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L1396-L1416)：比 `system_id` 多出最前面的 legacy 迁移分支——`device_id` 是历史版本的键名，存在则把它搬运到 `installation_id` 并删除旧键，返回 `Existing`（沿用旧身份，不视为新安装）。

`IdType` 定义见 [src/main.rs:L1795-L1807](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L1795-L1807)：两个变体共享同一个 `ToString` 实现（都返回内部字符串），供 `telemetry.start` 直接使用。

KVP 底层：表结构在 [crates/db/src/kvp.rs:L20-L39](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/db/src/kvp.rs#L20-L39)——`kv_store` 就是 `key TEXT PRIMARY KEY, value TEXT` 两列。读写删三件套见 [crates/db/src/kvp.rs:L66-L86](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/db/src/kvp.rs#L66-L86)，全部是一条 SQL 搞定的查询宏。`KeyValueStore::from_app_db` 见 [crates/db/src/kvp.rs:L15-L17](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/db/src/kvp.rs#L15-L17)，它克隆 `AppDatabase` 的连接句柄，不新开数据库。

两个作用域的分歧点：`AppDatabase::new` 按当前 `RELEASE_CHANNEL` 开库，见 [crates/db/src/db.rs:L60-L67](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/db/src/db.rs#L60-L67)；而 `GlobalDbScope` 固定返回 `"global"`，路径拼接规则为 `0-{scope}/db.sqlite`，见 [crates/db/src/db.rs:L154-L168](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/db/src/db.rs#L154-L168)。`GlobalKeyValueStore` 的静态实例与 `global()` 访问器见 [crates/db/src/kvp.rs:L243-L255](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/db/src/kvp.rs#L243-L255)——它是 `LazyLock` 全局，首次访问时才打开 `0-global` 库，因此可以在 `app.run` 之前的后台线程使用。

#### 4.2.4 代码实践：条件分支表 + 本地数据库验证

1. **实践目标**：把 `system_id` 与 `installation_id` 两个函数翻译成「已存在 / 新建 / legacy 迁移」三态分支表，并在本地数据库中实地核对。
2. **操作步骤**：
   - 精读上面两个函数，填写下表（参考答案已给出，先自己填再对照）：

     | 函数 | 触发条件 | 动作 | 返回值 |
     | --- | --- | --- | --- |
     | `system_id` | 全局库 `system_id` 键有值 | 无写入 | `Existing(旧值)` |
     | `system_id` | 键不存在或读失败 | 生成 UUID 并写入；写失败则整个函数返回 `Err` | `New(新值)` |
     | `installation_id` | legacy `device_id` 键有值 | 写 `installation_id`、删 `device_id` | `Existing(旧值)` |
     | `installation_id` | `installation_id` 键有值 | 无写入 | `Existing(旧值)` |
     | `installation_id` | 两键皆无 | 生成 UUID 并写入 | `New(新值)` |

   - 用 sqlite3 查看本机数据（Linux 默认路径，macOS 在 `~/Library/Application Support/Zed/` 下；dev 构建通道目录为 `0-dev`）：
     ```bash
     sqlite3 ~/.local/share/zed/0-global/db.sqlite \
       "SELECT key, value FROM kv_store WHERE key = 'system_id';"
     sqlite3 ~/.local/share/zed/0-dev/db.sqlite \
       "SELECT key, value FROM kv_store WHERE key IN ('installation_id', 'device_id', 'session_id');"
     ```
   - 删除 `installation_id` 所在行后重启 dev 构建，再次查询，观察键被重新生成。
3. **需要观察的现象**：`system_id` 只出现在 `0-global` 库；`installation_id`、`session_id` 出现在按通道的库；`device_id` 通常已不存在（迁移完成即删除）。
4. **预期结果**：两个库里的键分布与 4.2.1 的表格一致；删除后重启会重新生成 `installation_id`。具体查询输出**待本地验证**（取决于本机是否运行过 Zed 及通道情况）。

#### 4.2.5 小练习与答案

**练习 1**：legacy 迁移分支为什么返回 `Existing` 而不是 `New`？

答案：`device_id` 有值说明这台机器早已安装过 Zed，只是换了存储键名；返回 `New` 会让遥测误判为首次安装，错误触发 `App First Opened` 事件。

**练习 2**：dev 与 stable 两个通道共存于同一台机器时，`system_id` 和 `installation_id` 各有几个不同的值？

答案：`system_id` 只有一个（存在跨通道共享的 `0-global` 库）；`installation_id` 有两个（各自存在 `0-dev` 与 `0-stable` 库）。这正好匹配「机器级」与「安装级」两种语义。

**练习 3**：如果 `block_on(installation_id)` 返回 `Err`，启动流程会怎样？

答案：`.ok()` 把它折叠成 `None`，启动继续；`telemetry.start` 收到 `None` 的 `installation_id`；4.3 的 `(Some, Some)` 守卫不成立，本次运行不发任何「打开」类事件，`is_new_install` 也为 `false`。

### 4.3 telemetry 启动流程：首启事件判定与 authenticate

#### 4.3.1 概念说明

Zed 的遥测由两层组成：

- **`telemetry` crate**：只提供 `event!` 宏和一个进程内的 `OnceLock<UnboundedSender>` 队列。宏在任何地方都能调用，事件被塞进队列了事。
- **`client::telemetry::Telemetry`**：真正的消费者。构造时通过 `telemetry::init(tx)` 把自己的接收端注册进上面的队列，并 spawn 一个后台任务逐条取出上报。它还持有一份「公共状态」——`system_id`、`installation_id`、`session_id`、`app_version`、`os_name`——每条事件上报时都会附带。

`telemetry.start(...)` 做的事非常克制：只是往这份公共状态里填 ID。真正有判定逻辑的是紧随其后的 `App First Opened` 事件分支——用 4.2 的 `IdType` 新旧组合，推断这台机器/这次安装/这个通道是不是「第一次见」。

另外要区分两个名字相近的东西：`"App First Opened"` 是**遥测事件**（本讲内容，由 ID 新旧推断）；`onboarding::FIRST_OPEN` 是**界面引导标记**（KVP 布尔位，决定是否展示新手引导视图，属于 u2-l3 的恢复话题）。

`authenticate` 则回答另一个问题：启动时要不要自动登录？答案取决于「是否从终端启动」（`stdout_is_a_pty()`）以及「本地有没有已保存的凭据」。

#### 4.3.2 核心流程

```
Client::production(cx)（L526）
  └─ 内部构造 Telemetry：telemetry::init(tx) 接通事件队列
block_on 三个身份任务（L595-597）
telemetry.start(system_id, installation_id, session.id(), cx)（L600-605）
  └─ 填充公共状态：三个 ID + app_version + os_name

首启事件判定（L625-641）：
  is_new_install = installation_id 是 New
  match (system_id, installation_id):
    (New,     New)      → "App First Opened" + "App First Opened For Release Channel"
    (Existing,New)      → "App First Opened For Release Channel"   # 机器旧、此通道安装新
    (_,       Existing) → "App Opened"
    任一为 None          → 不发事件（守卫 if let (Some, Some)）
  is_new_install 随后传给 agent_ui::init（L713-720）驱动首装行为

启动尾声：
  "Settings Changed"(theme/keymap) 两个事件 + telemetry.flush_events()（L832-842）
  cx.spawn(authenticate(client, cx)).detach_and_log_err(cx)（L875-879）
```

`authenticate` 的分支逻辑：

```
stdout_is_a_pty() == true（从终端启动）:
    IMPERSONATE_LOGIN 有值 → sign_in_with_optional_connect(false)   # 员工调试登录
    否则有已存凭据         → sign_in_with_optional_connect(true)    # 静默续登
    否则                   → 不动作（等用户在界面里登录）
stdout_is_a_pty() == false（桌面启动）:
    有已存凭据             → sign_in_with_optional_connect(true)
    否则                   → 不动作
```

差异的直觉：终端场景是「开发者主动驱动」，允许启用 `ZED_IMPERSONATE` 这类内部调试手段；桌面场景是「用户点图标」，只做无感的凭据续登，一切显式交互都交给 UI。而 `stdout_is_a_pty()` 本身是 `!FORCE_CLI_MODE && io::stdout().is_terminal()`——`ZED_FORCE_CLI_MODE` 环境变量可以强制按 CLI 模式对待（u1-l4 讲过该变量读后即从环境中移除）。

#### 4.3.3 源码精读

`telemetry.start` 的调用见 [src/main.rs:L599-L605](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L599-L605)：`IdType` 经 `to_string()` 转成字符串，读取失败产生的 `None` 也原样传入。实现端见 [crates/client/src/telemetry.rs:L356-L368](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/client/src/telemetry.rs#L356-L368)：加锁、填五个字段、解锁，没有任何 I/O——「启动遥测」只是登记身份，不产生网络请求。

首启事件判定见 [src/main.rs:L625-L641](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L625-L641)。L627 的注释值得读：作者自己也承认这三个事件名（`App First Opened` / `App First Opened For Release Channel` / `App Opened`）语义不够直白，未来应改名——读源码时遇到这种注释，说明事件语义要以这段判定逻辑为准，而非望文生义。`(New, New)` 表示机器和安装都是新的（真正的第一次）；`(Existing, New)` 表示机器见过 Zed、但这个通道的安装是新的（比如 stable 用户第一次装 dev）。

事件的生产端：`event!` 宏与队列见 [crates/telemetry/src/telemetry.rs:L21-L54](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/telemetry/src/telemetry.rs#L21-L54)——宏构造 `Event` 结构后调用 `send_event`，后者仅在有队列时 `unbounded_send`。队列的接线在 Telemetry 构造时完成，见 [crates/client/src/telemetry.rs:L244-L258](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/client/src/telemetry.rs#L244-L258)：`::telemetry::init(tx)` 注册发送端，后台任务循环取事件调用 `report_event`。这条链路由 `Client::production`（[crates/client/src/client.rs:L584-L591](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/client/src/client.rs#L584-L591)）委托 `Client::new`、后者在 [crates/client/src/client.rs:L566](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/client/src/client.rs#L566) 构造 Telemetry 时接通。

启动尾声的事件冲刷见 [src/main.rs:L832-L842](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L832-L842)：主题与键位设置各报一个 `Settings Changed` 事件后调用 `flush_events()`，确保启动期事件尽快离队。

`authenticate` 的调用点是 [src/main.rs:L875-L879](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L875-L879)：一个被 `detach_and_log_err` 的后台任务——登录失败只记日志，绝不阻塞界面。函数本体见 [src/main.rs:L1367-L1379](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L1367-L1379)，分支结构与 4.3.2 的伪代码一一对应。

`stdout_is_a_pty` 见 [src/main.rs:L1682-L1684](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L1682-L1684)，其否决开关 `FORCE_CLI_MODE` 见 [src/main.rs:L1676-L1680](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L1676-L1680)。`IMPERSONATE_LOGIN` 来自 `ZED_IMPERSONATE` 环境变量，定义见 [crates/client/src/client.rs:L67-L71](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/client/src/client.rs#L67-L71)。`has_credentials` 与 `sign_in_with_optional_connect` 分别定义于 [crates/client/src/client.rs:L873](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/client/src/client.rs#L873) 和 [crates/client/src/client.rs:L1042](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/client/src/client.rs#L1042)——后者签名中的 `try_provider: bool` 正是 `authenticate` 两个分支传不同布尔值的落点。

#### 4.3.4 代码实践：authenticate 分支表与事件观察

1. **实践目标**：写出 `authenticate` 在 `stdout_is_a_pty()` 真假两种情况下的完整行为分支表，并（可选）观察一次真实启动中的遥测事件流转。
2. **操作步骤**：
   - 精读 [src/main.rs:L1367-L1379](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L1367-L1379)，填写下表（参考答案已给出）：

     | `stdout_is_a_pty()` | `IMPERSONATE_LOGIN` | `has_credentials` | 行为 |
     | --- | --- | --- | --- |
     | `true` | 有 | 任意 | `sign_in_with_optional_connect(false)` |
     | `true` | 无 | `true` | `sign_in_with_optional_connect(true)` |
     | `true` | 无 | `false` | 不做任何登录动作 |
     | `false` | 任意 | `true` | `sign_in_with_optional_connect(true)` |
     | `false` | 任意 | `false` | 不做任何登录动作 |

   - 解释关键差异：为什么 `false`（桌面启动）分支完全不含 impersonate 检查？
   - 可选运行验证：按 `event!` 宏文档的提示，用 `RUST_LOG=telemetry=trace cargo run` 启动（仓库根目录），观察日志中遥测相关的输出。
3. **需要观察的现象**：终端启动（`cargo run` 挂在 pty 上）与桌面启动行为不同；impersonate 分支只在设置了 `ZED_IMPERSONATE` 时才可能走到。
4. **预期结果**：分支表与上表一致；差异解释的参考答案——impersonate 是员工调试手段，仅应服务于「人正在终端里主动操作」的场景，桌面启动无人值守，静默续登之外不应有任何隐式登录尝试。日志观察部分**待本地验证**（遥测默认是否输出 trace 日志取决于构建与设置）。

#### 4.3.5 小练习与答案

**练习 1**：`"App First Opened"`（遥测事件）与 `onboarding::FIRST_OPEN`（onboarding 常量）有什么区别？

答案：前者是上报给 Zed 服务端的遥测事件，由 `IdType` 的 `New`/`Existing` 组合在每次启动时推断；后者是本机 KVP 里的界面引导标记，决定是否展示新手引导视图。一个面向数据，一个面向 UI，判定来源也完全不同。

**练习 2**：一台用了半年 stable 的机器上第一次安装 dev 构建，会发哪些「打开」类事件？`is_new_install` 是多少？

答案：`system_id` 为 `Existing`（存在 `0-global` 库），dev 通道的 `installation_id` 为 `New`，命中 `(Existing, New)` 分支，只发 `App First Opened For Release Channel`；`is_new_install = true`，随后会影响 `agent_ui::init` 的首装行为。

**练习 3**：`telemetry::event!("App Opened")` 这行代码执行后，事件去了哪里？

答案：进入 `telemetry` crate 的 `OnceLock` 队列（unbounded channel），由 `client::telemetry` 在 Telemetry 构造时 spawn 的后台任务取出，调用 `report_event` 携带公共状态上报；启动尾声 `main.rs` 还会调用 `flush_events()` 主动冲刷。宏本身不做任何网络 I/O。

## 5. 综合实践

**任务：绘制「身份与遥测」启动时间线并用本地数据交叉验证。**

把本讲三个模块串成一条从进程启动到登录尝试的完整时间线，产出一份包含以下要素的笔记：

1. **时间线图**（文字版时序图即可），至少覆盖这些节点及其行号：
   - `app.run` 之前：`AppDatabase::new`、三个后台任务 spawn（L345-354）；
   - `app.run` 之内：`Client::production`（L526）→ `block_on` 等待（L595-597）→ `telemetry.start`（L600-605）→ 首启事件判定（L628-641）→ `AppSession::new` 与 `AppState` 构造（L642-654）→ `authenticate` spawn（L875-879）。
2. **标注每一步的失败语义**：哪一步失败会让启动中止（几乎没有），哪一步失败只是降级（`.ok()`、`log_err`、`detach_and_log_err`），并用一句话说明这种「身份与遥测绝不能挡住编辑器启动」的错误分级设计。
3. **本地交叉验证**（待本地验证）：
   - 用 4.2.4 的 sqlite 命令查出本机的 `system_id` 与 `installation_id`；
   - 删除 `0-{channel}` 库中的 `installation_id` 后重启，推断下一次启动会命中哪个事件分支（`(Existing, New)` → `App First Opened For Release Channel`），再查库确认新 ID 已写入；
   - 对比自己推断的事件与实际行为是否一致。

## 6. 本讲小结

- `AppState` 是 workspace crate 定义的八个字段纯数据枢纽，在 init 序列中段（所有依赖句柄就绪后）以 `Arc` 注册为 GPUI 全局，是后续几乎所有 UI 装配函数的公共参数。
- `Session` 是可在后台线程构造的纯数据（本次 ID + 上次 ID + 上次窗口栈）；`AppSession` 是包住它的 GPUI Entity，追加 500ms 窗口栈序列化与退出钩子；拆分服务于「遥测要早、序列化要晚」两个时点。
- 三个身份标识粒度递减：`system_id`（机器级，存 `0-global` 库）、`installation_id`（安装级，存按通道库，含 `device_id` legacy 迁移）、`session_id`（每次启动新生成）；读取结果用 `IdType::New/Existing` 表达新旧。
- 身份生成采用「后台提前 spawn、前台 `block_on` 同步」模式；读取失败一律降级为 `None`，绝不阻断启动。
- `telemetry.start` 只登记身份；`App First Opened` 系列事件由 `IdType` 组合矩阵判定，`is_new_install` 还会下传影响 agent 面板首装行为。
- `authenticate` 依据 `stdout_is_a_pty()` 与本地凭据决定是否静默续登；impersonate 调试登录仅存在于终端场景。

## 7. 下一步学习建议

下一讲 **u2-l3 会话恢复与首启引导** 将顺流而下：`Session.old_window_ids` 与 `last_session_id` 如何被 `restore_or_create_workspace` 消费、`RestoreOnStartupBehavior` 的三分支如何决定恢复哪个工作区，以及本讲提到的 `onboarding::FIRST_OPEN` 标记如何驱动新手引导。阅读建议：先重读 [src/main.rs:L1418](https://github.com/zed-industries/zed/blob/a7d74150ac7a663fdaa01ec5177e303baaf6c331/crates/zed/src/main.rs#L1418) 起的 `restore_or_create_workspace`，再对照 `crates/session/src/session.rs` 的窗口栈持久化。若对遥测的后续（内存监控、卡顿检测如何复用 `Client` 与遥测通道）更感兴趣，可以先跳到 **u6-l1 reliability 总览**，再回头补会话恢复。
