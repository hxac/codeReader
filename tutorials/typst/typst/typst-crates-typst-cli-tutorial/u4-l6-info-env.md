# info 命令与环境内省

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 `typst info` 收集了哪六大类信息（version / build / features / fonts / packages / env），以及它们分别来自「编译期常量」「运行时环境变量」还是「平台默认值」。
- 读懂 `src/info.rs` 里那棵 `Info` 结构树，并解释两套截然不同的 `serde` 命名规则——`kebab-case` 与 `SCREAMING_SNAKE_CASE`——为什么分别用在不同的结构上。
- 读懂 `get_vars` 如何逐个读取环境变量、对非 UTF-8 值做校验，以及为什么 `XDG_CACHE_HOME`/`XDG_DATA_HOME`/`FONTCONFIG_FILE`/`OPENSSL_CONF` 这几个字段是「平台相关」的（用 `#[cfg(...)]` 门控）。
- 读懂 `parse_features` 与 `parse_bool` 如何复用 clap 的值解析能力，把字符串环境变量解析成强类型；以及它们「尽力而为」的错误处理风格。
- 读懂 `format_human_readable` 如何用「青色键 / 绿色值 / 蓝色特殊值」三色加对齐填充，把同一份 `Info` 渲染成人能读的样子，并与 `--format json/yaml` 的机器可读输出形成对照。

本讲是专家层（u4）的一篇。它承接 u1-l3「命令行参数模型」对 `args.rs` 的讲解（这里要复用 `Feature`、`InfoCommand`、`SerializationFormat` 等类型），也承接 u3-l2「包存储与解析」对 `FsPackages` 默认路径的讲解（这里要在 `info` 输出里把它们展示出来）。本讲不再解释 clap 派生宏或包下载机制本身，而是聚焦「`typst` 如何把自身的运行环境原原本本地报告给用户与脚本」。

## 2. 前置知识

在进入源码前，先建立四个直觉。

**直觉一：什么是「环境内省（environment introspection）」命令。**

很多命令行工具都有一个 `info` / `doctor` / `version --verbose` 之类的子命令，用来把「我这个程序当前到底处在什么环境里」一次性打印出来：版本号、编译选项、它会在哪些目录找字体和包、它读到了哪些环境变量。这对两类人有用：

- **人类用户**用来排查「为什么我的字体没生效」「为什么包下载到了别的地方」——看一眼 `info` 输出就能定位。
- **脚本/CI** 用来读取机器可读的版本与配置，做条件判断。所以 `typst info` 同时提供「人类可读」和「机器可读（json/yaml）」两种输出。

`typst info` 的本质就是：**把一堆散落在编译期常量和运行时环境变量里的信息，收拢成一棵结构体树，再按需渲染**。

**直觉二：什么是 serde 的 `rename_all`。**

`serde` 是 Rust 最常用的序列化框架。给一个结构体加上 `#[derive(Serialize)]` 后，它的字段名默认会原样变成 JSON/YAML 的键名。但 Rust 的字段命名习惯是 `snake_case`（如 `package_path`），而不同场景期望的键名风格不同：

- `kebab-case`：`package-path`（小写、连字符分隔），适合「配置项」风格。
- `SCREAMING_SNAKE_CASE`：`TYPST_PACKAGE_PATH`（全大写、下划线分隔），这正是**环境变量**的惯用风格。

`#[serde(rename_all = "...")]` 这个属性就是一键切换整棵结构体的键名风格。本讲会看到 `info.rs` 同时用了这两种风格，妙处在于：让 JSON 输出的键名和它背后那个环境变量的真实名字**长得一模一样**，读起来零认知负担。

**直觉三：复用 clap 的值解析器。**

clap 不只能解析命令行参数，它内部还提供了一批「值解析器（value parser）」，比如 `BoolValueParser` 能把字符串 `"true"`/`"false"`/`"1"`/`"0"` 解析成 `bool`；派生了 `ValueEnum` 的枚举会自动生成一个 `from_str(input, ignore_case)` 方法。`info.rs` 聪明地**复用**了这些能力来解析环境变量字符串，而不是自己手写一套 `if val == "true"` 的脆弱逻辑——这样命令行和环境变量两套入口的「合法取值」天然保持一致。

**直觉四：什么是 TTY 与终端颜色。**

终端（TTY）能解释 ANSI 转义序列来显示彩色；但若输出被重定向到管道或文件（非 TTY），这些转义序列就变成了乱码。`typst info` 的人类可读输出带颜色，它依赖 u2-l4 讲过的 `terminal::out()`（`TermOut`）抽象：非 TTY 时自动关掉颜色。本讲会看到 `format_human_readable` 如何通过 `TermOut` 写入带颜色的键值。

> 术语对照：「编译期常量」指 `cfg!`/`env!` 这类在 `cargo build` 时就定死的值（如是否启用了 `self-update` feature、目标平台）；「运行时环境变量」指用户在 shell 里 `export` 的 `TYPST_*` 之类变量，程序启动时才读取。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲角色 |
| --- | --- | --- |
| `src/info.rs` | `typst info` 子命令的全部实现 | **主战场**：数据模型、环境收集、解析、人类可读渲染都在这里 |
| `src/terminal.rs` | 彩色终端输出抽象 `TermOut` | 人类可读渲染的颜色出口与 singleton |
| `src/args.rs` | 命令行参数定义 | 提供 `InfoCommand`、`Feature`、`SerializationFormat` 类型 |
| `src/main.rs` | 入口、命令分发与共享 `serialize` | 把 `Info` 接到 `info::info`，并提供 json/yaml 序列化函数 |
| `crates/typst-utils/src/version.rs` | 版本信息读取 | 提供 `version()`、`TypstVersion::raw()`/`commit()`、`display_commit()` |

## 4. 核心概念与源码讲解

本讲拆成五个最小模块，按「数据从哪来 → 如何解析 → 如何渲染」的顺序展开：先讲顶层 `Info` 数据模型与它的 serde 命名（4.1），再讲环境变量如何被收集与 UTF-8 校验（4.2），接着讲字符串如何被解析成强类型（4.3），然后讲人类可读渲染（4.4），最后讲机器可读输出（4.5）。

### 4.1 Info 数据模型与 serde 序列化

#### 4.1.1 概念说明

`typst info` 要展示的信息来源很杂：有的在编译期就定死了（目标平台、是否启用 `self-update`），有的要等运行时读环境变量（`TYPST_FONT_PATHS`），有的要从别的子系统算出来（包的默认目录）。为了把这些杂乱来源统一成「一份可以序列化的数据」，`info.rs` 设计了一棵结构体树，根节点是 [`Info`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/info.rs#L19-L39)，它有六个字段，恰好对应输出里的六大块：

| 字段 | 类型 | 信息来源 |
| --- | --- | --- |
| `version` | `&'static str` | 编译期注入的 `TYPST_VERSION` |
| `build` | `Build` | 编译期常量（commit、平台、feature 开关） |
| `features` | `Features` | 运行时环境变量 `TYPST_FEATURES` |
| `fonts` | `Fonts` | 运行时环境变量 `TYPST_FONT_PATHS` 等 |
| `packages` | `Packages` | 环境变量 `TYPST_PACKAGE_PATH` 或平台默认 |
| `env` | `Environment` | 一大批运行时环境变量的原值 |

这棵树最关键的设计是：**同一个 `Info` 值既能喂给 serde 做机器可读输出，又能喂给 `format_human_readable` 做人类可读输出**——两套渲染共享同一份数据，不会出现「JSON 里有的字段，终端里漏了」的不一致。

#### 4.1.2 核心流程

`info` 命令的整体数据流可以画成这样：

```
   编译期常量                运行时环境变量                平台默认值
   ──────────              ──────────────              ──────────
   cfg!(feature=...)       TYPST_FEATURES              FsPackages::system_data()
   std::env::consts::OS    TYPST_FONT_PATHS            FsPackages::system_cache()
   TYPST_VERSION           TYPST_PACKAGE_PATH          (来自 typst-kit)
   TYPST_COMMIT_SHA        TYPST_IGNORE_SYSTEM_FONTS
                           ... 一批 TYPST_* / XDG_* / *_PROXY
                                │
                                ▼  get_vars() 收集
                          ┌──────────┐
                          │Environment│
                          └────┬─────┘
                               │  + 解析(parse_features/parse_bool)
                               ▼
                        ┌─────────────────┐
                        │   Info { ... }   │  ← 一棵结构体树(单一数据源)
                        └────────┬────────┘
                                 │
                ┌────────────────┼─────────────────┐
                ▼                                  ▼
      format_human_readable()              crate::serialize()
      (彩色 key/value/desc)               (json / yaml)
                │                                  │
                ▼                                  ▼
            终端 stderr                       stdout
```

注意一个贯穿全讲的细节：**`info` 只反映「环境变量」与「平台默认」，不反映命令行 `--font-path`/`--package-path` 这类一次性 flag**。原因是 `info` 的职责是报告「默认环境下 typst 会怎么表现」，而 `InfoCommand` 本身也只接受 `--format` 与 `--pretty` 两个参数（见 4.5）。所以 `typst compile --font-path X foo.typ` 里那个 `X` **不会**出现在 `typst info` 的输出里——只有你把它 `export` 成 `TYPST_FONT_PATHS` 才会出现。

#### 4.1.3 源码精读

顶层 `Info` 结构用 `kebab-case` 命名，所以 JSON 里它的键是 `version`、`build`、`features`、`fonts`、`packages`、`env`：

[Info 结构与 kebab-case 命名](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/info.rs#L19-L39) — `#[serde(rename_all = "kebab-case")]` 把字段名转成连字符风格；六个字段对应六大类信息。

`Build` 与它的子结构都用 `kebab-case`，其中 `Platform` 用 `const fn new()` 直接读编译期常量 `std::env::consts::OS`/`ARCH`：

[Platform：编译期常量决定平台信息](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/info.rs#L56-L80) — `os` 与 `arch` 在编译时就定死（如 `linux`/`x86_64`），所以同一份二进制跑在不同机器上，`info` 报告的永远是它**被编译时**的目标平台，而非运行时宿主。

`Settings` 记录两个编译期 feature 开关，并用 `compile_features()` 方法把每个开关连同人类可读描述打包成 `KeyValDesc`（键-值-描述三元组，供 4.4 渲染）：

[Settings 与 compile_features()](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/info.rs#L83-L105) — `cfg!(feature = "self-update")` 在编译期求值，二进制里这个值是固定的 `true`/`false`；`KeyValDesc` 让同一份数据既能序列化（结构体字段）又能带描述地打印。

真正体现「两套命名规则」对比的是 `Environment`——它**单独**用了 `SCREAMING_SNAKE_CASE`：

[Environment 与 SCREAMING_SNAKE_CASE](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/info.rs#L183-L209) — 字段名是小写的 `typst_cert`、`xdg_cache_home`（遵循 Rust 习惯），但 serde 把它们序列化成 `TYPST_CERT`、`XDG_CACHE_HOME`，与真实环境变量名一致；注意四个字段（`xdg_cache_home` 等）带 `#[cfg(not(any(target_os = "windows", ...)))]`，只在非 Windows/macOS/iOS 编译，详见 4.2。

最后看 `info()` 入口如何把这棵树**装配**出来。版本号来自 `typst::utils::version()`（一个返回 `TypstVersion` 的 singleton），其余字段从 `env`（即 `get_vars()` 的结果）派生：

[info() 装配 Info 树](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/info.rs#L301-L356) — 注意 `packages` 字段：`TYPST_PACKAGE_PATH` 优先，否则回落到 `FsPackages::system_data()` 的平台默认目录（与 u3-l2 一致）；`fonts.system` 是 `!parse_bool(TYPST_IGNORE_SYSTEM_FONTS)`——「忽略系统字体」取反才是「包含系统字体」。

#### 4.1.4 代码实践

1. **实践目标**：把 JSON 输出的每一个键，回溯到 `info.rs` 里定义它的结构体字段，验证两套命名规则。
2. **操作步骤**：
   - 在仓库根用 `cargo build` 生成二进制（参见 u1-l1）。
   - 运行 `./target/debug/typst info --format json --pretty`。
   - 拿到输出后，逐层对照：顶层键 `version`/`build`/`features`/`fonts`/`packages`/`env` ↔ [`Info`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/info.rs#L19-L39) 的字段；`build` 下面的 `commit`/`platform`/`settings` ↔ [`Build`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/info.rs#L42-L53)；`env` 下面的全大写键 ↔ [`Environment`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/info.rs#L183-L209)。
3. **需要观察的现象**：`env` 里的键是 `SCREAMING_SNAKE_CASE`（如 `TYPST_ROOT`），而其它所有结构都是 `kebab-case`（如 `package-cache-path`、`a11y-extras`）。
4. **预期结果**：`a11y_extras` 字段在 JSON 里显示为 `a11y-extras`（kebab）；`typst_root` 显示为 `TYPST_ROOT`（screaming snake）。确切的版本号字符串与平台值**待本地验证**（取决于你编译时的目标）。
5. 若尚未构建二进制，可改为纯阅读：对照源码列出「字段名 → 序列化键名」的映射表。

#### 4.1.5 小练习与答案

**练习 1**：假如把 [`Environment`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/info.rs#L183-L209) 上的 `rename_all` 改成和 `Info` 一样的 `kebab-case`，JSON 输出会有什么变化？为什么作者偏要用 `SCREAMING_SNAKE_CASE`？

> **答案**：`env` 里的键会从 `TYPST_ROOT` 变成 `typst-root`。作者刻意用 `SCREAMING_SNAKE_CASE` 是因为这些键代表的就是环境变量本身，让 JSON 键名与 `export TYPST_ROOT=...` 里的变量名一字不差，用户一眼就能对上号。

**练习 2**：`Platform` 的 `os`/`arch` 是「编译时」还是「运行时」决定的？如果你把一个为 Linux 编译的 typst 拷到 macOS 上跑，`typst info` 会显示什么？

> **答案**：编译时决定（来自 `std::env::consts::OS`/`ARCH`，是编译期常量）。拷到 macOS 上跑，`info` 仍显示 `linux`/`x86_64`（或编译时的目标），因为它报告的是「二进制被编译时的目标平台」，而非运行宿主。

### 4.2 get_vars：环境变量收集与 UTF-8 校验

#### 4.2.1 概念说明

`Environment` 这棵子树要展示一大批环境变量的**原值**（未经解析的字符串）。`get_vars` 就是负责把它们从进程环境里逐个取出来的函数。它要处理三种情况：变量存在且是合法文本、变量不存在、变量存在但值不是合法 UTF-8（比如路径里带非法字节的极少数情况）。这第三种情况是本模块的重点——Rust 的 `std::env::var` 返回的是 `String`，遇到非 UTF-8 值会报 `VarError::NotUnicode`，`info` 必须决定怎么应对。

#### 4.2.2 核心流程

`get_vars` 内部定义了一个闭包 `get_var(key)`，统一处理三种分支：

```
get_var(key):
  match std::env::var(key):
    Ok(val)            → Ok(Some(val))        # 正常拿到字符串
    Err(NotPresent)    → Ok(None)             # 没设这个变量 → None
    Err(NotUnicode(_)) → set_failed()         # 值非 UTF-8
                         print_error("...was not valid UTF-8")
                         Ok(None)             # 降级为 None，继续
```

关键结论（也是本讲的一个细节）：**只有「值非 UTF-8」这一种情况会调用 `crate::set_failed()`**，把退出码改成 `FAILURE`；而变量不存在只是平淡地返回 `None`。这条分支延续 u1-l2 讲过的「软失败」模式：函数照样返回 `Ok`，但通过 `thread_local` 的 `EXIT` 把退出码标记为失败。

`get_vars` 对每个关心的变量调用一次 `get_var`，用 `?` 把可能的 `Err`（打印错误时发生的 IO 错误）冒泡出去：

[get_vars：逐个读取环境变量](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/info.rs#L369-L410) — 列表里既有 `TYPST_*` 一族，也有 `SOURCE_DATE_EPOCH`、`NO_COLOR`、`*_PROXY` 等通用变量；四个 `#[cfg(...)]` 标注的字段是平台相关的（见下）。

#### 4.2.3 源码精读

闭包 `get_var` 的三分支处理是本模块核心：

[get_var 闭包：三分支与 UTF-8 校验](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/info.rs#L370-L383) — `NotUnicode` 分支调用 `crate::set_failed()` 标记失败码，再调 `crate::print_error` 打印提示，最后返回 `Ok(None)` 而非中断——这是「尽量报告，不崩溃」的软失败风格。

平台相关字段用 `#[cfg(not(any(target_os = "windows", target_os = "macos", target_os = "ios")))]` 门控，意思是「只在 Linux/Android/BSD 等 Unix 平台编译」：

[四个平台相关变量](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/info.rs#L196-L203) — `XDG_CACHE_HOME`、`XDG_DATA_HOME`（XDG 基础目录规范，Linux 专属）、`FONTCONFIG_FILE`（fontconfig 配置，Linux 字体系统）、`OPENSSL_CONF`（OpenSSL 配置）。这些在 Windows/macOS/iOS 上根本不存在相应约定，所以编译时直接剔除，既不收集也不展示。

同样的 `#[cfg]` 在 `Environment::vars()`（负责把字段变成「键-值」对供 4.4 渲染）里**重复出现**，保证「结构体定义」与「遍历展示」两侧的平台条件完全一致：

[vars() 里同样带 cfg 的数组](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/info.rs#L255-L298) — 每个变量映射成 `(环境变量名, Value::String(值) 或 Value::Unset)`；`Value::Unset` 表示该变量未设置，会在人类可读输出里显示成蓝色的 `<unset>`。

`Environment::vars()` 的实现有个值得学习的 Rust 技巧：它在解构 `self` 时把所有 `#[cfg]` 字段也都列出来，这样无论编译到哪个平台，解构都是穷尽的、不会因为「某个字段在当前平台不存在」而报错。

#### 4.2.4 代码实践

1. **实践目标**：验证 `get_vars` 收集的范围，并观察平台相关变量在 Linux 上的出现。
2. **操作步骤**：
   - 运行 `export TYPST_ROOT=/tmp/mytypst`（随便给一个值）。
   - 运行 `./target/debug/typst info`（不加 `--format`，看人类可读输出的 Environment variables 段）。
   - 在 Linux 上确认 `XDG_CACHE_HOME`、`XDG_DATA_HOME`、`FONTCONFIG_FILE`、`OPENSSL_CONF` 这四行存在（多半显示 `<unset>`）。
3. **需要观察的现象**：`TYPST_ROOT` 显示你刚设的值；未设置的变量显示 `<unset>`。
4. **预期结果**：Environment variables 段会出现 `TYPST_ROOT /tmp/mytypst`。确切的默认值与是否存在 `XDG_*` **待本地验证**（取决于平台与你的 shell 环境）。
5. 想验证「非 UTF-8 触发 set_failed」较难手工复现（需要构造非法字节的环境变量），建议改为**源码阅读型实践**：对照 [get_var 闭包](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/info.rs#L370-L383) 画出三分支表，并解释为什么变量不存在不算失败、而非 UTF-8 算失败。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `XDG_CACHE_HOME` 要用 `#[cfg(not(any(target_os = "windows", target_os = "macos", target_os = "ios")))]` 排除掉 Windows/macOS/iOS？

> **答案**：XDG 基础目录规范是 Linux/Unix 桌面的约定，Windows 与 macOS 各有自己的标准目录（如 `%APPDATA%`、`~/Library`），不存在 `XDG_CACHE_HOME` 这个变量，收集它没有意义；用 `#[cfg]` 在编译期剔除，能让这些平台上的输出更干净、二进制也略小。

**练习 2**：`get_var` 在 `NotUnicode` 分支里既调用 `set_failed()` 又返回 `Ok(None)`，而不是返回 `Err`。这种「软失败」对用户有什么好处？

> **答案**：哪怕某一个环境变量值损坏，`info` 仍会把**其余所有**信息打印出来（只是损坏的那项显示为空并把退出码标成失败），而不是整个命令直接报错退出。用户因此能拿到几乎完整的诊断信息去排查问题。

### 4.3 parse_features / parse_bool：把字符串解析成强类型

#### 4.3.1 概念说明

`get_vars` 只负责把环境变量**原样**取回来（字符串）。但 `info` 的有些字段需要的是**强类型**值：`TYPST_FEATURES` 是逗号分隔的 feature 名列表，要解析成 `Features { html, bundle, a11y_extras }`；`TYPST_IGNORE_SYSTEM_FONTS` 应该是个布尔值。把字符串变成这些结构的工作，由 `parse_features` 和 `parse_bool` 两个函数完成。

它们的妙处在于**复用 clap 的解析能力**：

- `parse_features` 用 `Feature::from_str(name, true)`——这个 `from_str` 是 clap 给 `#[derive(ValueEnum)]` 自动生成的（见 u1-l3），第二个参数 `true` 表示忽略大小写。于是 `html`、`HTML`、`Html` 都能识别，且「合法 feature 名」与命令行 `--features` 完全同源。
- `parse_bool` 用 clap 的 `BoolValueParser`，能识别 `true/false/1/0` 等多种写法，与 clap 解析布尔 flag 的逻辑一致。

#### 4.3.2 核心流程

两个函数都采用「**尽力而为**」的错误风格：

```
parse_features("html,bundle"):
  对每个逗号分隔的非空 token:
    Feature::from_str(token, ignore_case=true)
      Ok(Feature::Html)   → features.html = true
      Err(_)              → print_error("unknown runtime feature: ...")  # 仅提示，不中断
  返回 Ok(features)        # 不管中间有几个未知 token，都返回已解析的部分

parse_bool(cmd, "true", "TYPST_IGNORE_SYSTEM_FONTS"):
  BoolValueParser.parse_ref(cmd, None, "true")
    Ok(true)  → Some(true)
    Err(_)    → print_error("invalid value ... expected true or false")  # 仅提示
                None      # 返回 None，调用方 unwrap_or(false) 兜底
```

注意一个**贯穿全讲的细节**（与 4.2 对比）：`parse_features` 和 `parse_bool` 在遇到非法值时**只打印错误、不调用 `set_failed()`**。也就是说，如果你把 `TYPST_FEATURES` 设成了一个不存在的 feature 名，`typst info` 会打印一行错误提示，但退出码仍是 0、输出照常。这与 `get_vars` 里「非 UTF-8 会 set_failed」形成对照——可以理解为：环境变量**存在但格式错**算「可恢复的提示」，而环境变量**根本无法读取**才算「需要警示的失败」。

#### 4.3.3 源码精读

`parse_features` 把逗号列表解析成 `Features`：

[parse_features：逗号分隔 + Feature::from_str](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/info.rs#L429-L447) — `split(',').filter(|s| !s.is_empty())` 跳过空段（处理 `"html,,bundle"` 这类）；未知 token 走 `print_error` 后继续，最终返回已识别的部分。

`Feature` 枚举本身定义在 `args.rs`，派生了 `ValueEnum`（这才有 `from_str`）和 `Serialize`：

[Feature 枚举](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L646-L654) — 三个变体 `Html`/`Bundle`/`A11yExtras`，对应 `typst compile --features` 的取值；`display_possible_values!` 宏（来自 typst-utils）为之实现 `Display`，详见 u1-l3。

`parse_bool` 复用 clap 的 `BoolValueParser`，并需要传入一个 `&Command`（用于错误信息上下文）——这就是为什么 `info()` 一开头要先 `let cmd = CliArguments::command();`：

[parse_bool：复用 BoolValueParser](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/info.rs#L413-L425) — `BoolValueParser::new().parse_ref(cmd, None, val.as_ref())` 把字符串解析成 `bool`，失败时打印错误并返回 `None`；注意它用 `.expect("failed to print error")` 处理「打印本身失败」的 IO 错误。

回到 `info()` 看 `parse_bool` 如何被消费：`Fonts.system` 字段是「`TYPST_IGNORE_SYSTEM_FONTS` 解析成的 bool，再取反」——因为「忽略系统字体」取反才是「包含系统字体」：

[Fonts.system 的取反逻辑](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/info.rs#L330-L342) — `!env.typst_ignore_system_fonts.and_then(parse_bool).unwrap_or(false)`：未设变量时默认 `false`（忽略），取反得 `system: true`（包含系统字体），与 typst 默认会搜索系统字体一致。

#### 4.3.4 代码实践

1. **实践目标**：验证 `parse_features` 与 `parse_bool` 的解析行为与错误风格。
2. **操作步骤**：
   - `export TYPST_FEATURES=html,bundle`，运行 `./target/debug/typst info`，看 Features 段。
   - `export TYPST_IGNORE_SYSTEM_FONTS=true`，再运行一次，看 Fonts 段的 `System fonts`。
   - `export TYPST_FEATURES=html,not-a-real-feature`，再运行，观察是否有错误提示、退出码是多少（用 `echo $?`）。
3. **需要观察的现象**：第一步 `html`/`bundle` 对应的值变成 `on`；第二步 `System fonts` 变成 `off`；第三步会打印一行 `error: unknown runtime feature: not-a-real-feature`，但 `info` 仍正常输出、`echo $?` 为 0。
4. **预期结果**：如上。退出码在第三步仍为 0（因为 `parse_features` 不调用 `set_failed`）——这一点**待本地验证**，请用 `echo $?` 确认。
5. 若无法运行，可对照 [parse_features](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/info.rs#L429-L447) 与 [parse_bool](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/info.rs#L413-L425) 静态推断：列出「输入字符串 → 输出」的对照表，并标注哪些输入会触发 `print_error`。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `parse_features` 用 `Feature::from_str(token, true)` 而不是自己 `match token`？第二个参数 `true` 有什么作用？

> **答案**：复用 `Feature::from_str` 能让「环境变量 `TYPST_FEATURES` 的合法取值」与「命令行 `--features` 的合法取值」天然保持一致（都来自同一个 `ValueEnum`），避免两处维护。第二个参数 `true` 表示忽略大小写，所以 `HTML`/`Html`/`html` 都能识别。

**练习 2**：把 `TYPST_IGNORE_SYSTEM_FONTS` 设成 `"yes"` 会发生什么？`Fonts.system` 最终会是什么值？

> **答案**：`BoolValueParser` 不接受 `"yes"`（它只认 `true/false/1/0` 等），`parse_bool` 打印 `invalid value ... expected true or false` 并返回 `None`；调用方 `unwrap_or(false)` 兜底为 `false`（即「忽略」），取反后 `Fonts.system = true`（包含系统字体）。也就是说，一个非法值等价于「没设这个变量」。

### 4.4 format_human_readable：彩色 key/value/desc 与对齐填充

#### 4.4.1 概念说明

不加 `--format` 时，`typst info` 走人类可读分支 `format_human_readable`。它把同一棵 `Info` 树渲染成分段的彩色文本：每个 section（Build settings、Features、Fonts、Packages、Environment variables）有一个标题，下面是若干「键 值 (描述)」行。这套渲染靠三个要素：

- **三色分工**：键（key）用青色（cyan）、普通值（路径/字符串）用绿色（green）、特殊值（`on`/`off`/`<unset>`/`<none>`）用蓝色（blue）。
- **对齐填充**：每个 section 内，先算出最长键名（或值）的宽度，再用 `format!("{key: <pad$}")` 把所有键右补齐到等宽，让冒号前的列对齐。
- **`Value` 枚举**：把「bool / 路径 / 字符串 / 未设置」抽象成统一的 [`Value`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/info.rs#L476-L481) 类型，统一决定颜色与文案（`true`→`on`、`false`→`off`、`Unset`→`<unset>`）。

颜色全部经 u2-l4 讲过的 `terminal::out()`（`TermOut`）写出，非 TTY 时自动降级为无色。

#### 4.4.2 核心流程

`format_human_readable` 是个很长但结构清晰的大函数，逐段写出一个 section：

```
format_human_readable(value):
  out = terminal::out()
  1. 写 Version 行:  Version <ver> (<commit>, <os> on <arch>)
  2. 写 "Build settings" 段:
       key_pad = 各 compile_feature 键名的最大长度
       对每个 feature: feature.format(out, key_pad, val_pad=3)   # "  <key> <on/off> (desc)"
  3. 写 "Features" 段: 同上，用 runtime features
  4. 写 "Fonts" 段:
       "Custom font paths": 空则 <none>，否则逐行 "    - <path>"
       "System fonts" / "Embedded fonts": bool → on/off
  5. 写 "Packages" 段: "Package path" / "Package cache path" → 路径或 <unset>
  6. 写 "Environment variables" 段: 逐个变量 → 字符串或 <unset>
```

每个 `KeyValDesc::format` 负责渲染一行「键 值 (描述)」：

[KeyValDesc::format](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/info.rs#L456-L472) — 写两个前导空格、键（带右补齐）、一个空格、值（带右补齐）、最后 ` (描述)`；`key_pad`/`val_pad` 都是 `Option<usize>`，`None` 表示不补齐。

#### 4.4.3 源码精读

`Value` 枚举与它的 `format` 是「颜色与文案」的单一决策点：

[Value 枚举与 format](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/info.rs#L476-L494) — `Unset`→`<unset>`、`Bool(true)`→`on`、`Bool(false)`→`off`（都走蓝色 `write_value_special`）；`Path`/`String` 走绿色 `write_value_simple`。

三种写入函数只是「换颜色 + 可选补齐」的小工具：

- [write_key：青色](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/info.rs#L497-L507) — `set_fg(Some(Color::Cyan))`，用 `{key: <pad$}` 右补齐。
- [write_value_simple：绿色](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/info.rs#L510-L524) — `Color::Green`，给路径/字符串用。
- [write_value_special：蓝色](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/info.rs#L527-L541) — `Color::Blue`，给 `on/off/<unset>/<none>` 这类「状态词」用，视觉上与真实路径区分开。

补齐宽度的计算是「函数式」的：用迭代器 `map(|f| f.key.len()).max()` 求出本段最长键名，作为统一的 `key_pad`，例如 [Build settings 段](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/info.rs#L557-L562) 与 [Environment variables 段](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/info.rs#L609-L619) 都这么做。值的补齐 `Some(3)` 用在 feature 段（`on`=2 字符、`off`=3 字符，补到 3 让后续描述列对齐）。

颜色最终落到 `TermOut`，它来自 `terminal.rs`：

[terminal::out() singleton](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/terminal.rs#L9-L14) — 返回一个复用全局 `TermOutInner` 的句柄；`TermOut` 实现了 `WriteColor`，`set_color`/`reset` 直接转发给内部带锁的 `termcolor::StandardStream`。

[TermOutInner::new 的颜色选择](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/terminal.rs#L80-L93) — `--color=auto` 且 stderr 是 TTY 时才开启颜色，否则 `ColorChoice::Never`；这就是为什么 `typst info | cat` 看不到颜色转义码。这与 u2-l4 讲的诊断输出共用同一条 stderr 通道与同一个颜色判定。

还有一个容易忽略的小细节：人类可读输出的 commit 哈希是**截断**的。Version 行用的是 `typst_utils::display_commit(value.build.commit)`：

[Version 行使用 display_commit](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/info.rs#L543-L555) — `display_commit` 把哈希截到前 8 个字符、未知时显示 `unknown commit`；而机器可读输出里的 `build.commit` 是完整的 `Option<&'static str>`。所以两种输出的 commit 精度不同（见 4.5）。

#### 4.4.4 代码实践

1. **实践目标**：观察三色分工、对齐填充，以及非 TTY 下降级。
2. **操作步骤**：
   - 在 TTY 终端运行 `./target/debug/typst info`，肉眼确认键是青色、路径是绿色、`on/off/<unset>` 是蓝色。
   - 运行 `./target/debug/typst info 2>&1 | cat`（或重定向到文件后 `cat`），对比是否还能看到颜色。
   - 运行 `./target/debug/typst info --color=always 2>&1 | cat -v`，强制开色后用 `cat -v` 把 ANSI 转义码显化（会看到 `^[[36m` 之类）。
3. **需要观察的现象**：第二步无颜色、列依然对齐；第三步能看到原始转义序列。
4. **预期结果**：颜色与否由 [TermOutInner::new](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/terminal.rs#L80-L93) 的 TTY 判定决定。具体的颜色渲染效果**待本地验证**（取决于你的终端）。
5. 对照 [format_human_readable](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/info.rs#L543-L622) 数清楚输出共有几个 section 标题（应为 Version 行 + Build settings / Features / Fonts / Packages / Environment variables 五段）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 feature 段的值补齐宽度固定写成 `Some(3)`，而不是像键那样用 `.max()` 算？

> **答案**：因为 feature 的值只会是 `on`（2 字符）或 `off`（3 字符），最大就是 3，写死 `Some(3)` 等价于动态算，但更直接；补齐到 3 是为了让所有行后面的 `(描述)` 列左边界对齐。

**练习 2**：`typst info | less` 看不到颜色，但 `typst info --color=always | less` 能看到。这个判定的代码在哪？

> **答案**：在 [TermOutInner::new](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/terminal.rs#L80-L93)：`--color=auto`（默认）时会检查 `std::io::stderr().is_terminal()`，管道时为 false 故选 `ColorChoice::Never`；`--color=always` 则无视 TTY 强制开色。

### 4.5 --format json / yaml：机器可读输出与共享 serialize

#### 4.5.1 概念说明

加 `--format json` 或 `--format yaml` 时，`typst info` 走机器可读分支：把同一棵 `Info` 树直接交给 serde 序列化，再 `println!` 到 stdout。这套机制几乎不用 `info.rs` 自己写序列化逻辑——它复用了 `main.rs` 里一个**公共的** `serialize` 函数，这个函数也被 `query`（u4-l2）和 `eval`（u4-l1）复用。所以 typst-cli 里「把数据序列化成 json/yaml」只有这一个真相源。

`InfoCommand` 只暴露两个开关：

[InfoCommand 参数](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L275-L290) — `--format`/`-f` 是 `Option<SerializationFormat>`（不传则人类可读），`--pretty` 控制是否美化打印（**只对 JSON 生效**）。

#### 4.5.2 核心流程

```
info(command):
  ... 装配 value: Info ...
  if let Some(format) = command.format:        # 传了 --format
    serialized = crate::serialize(&value, format, command.pretty)
    println!("{serialized}")                    # 走 stdout
  else:                                         # 没传
    format_human_readable(&value)               # 走 stderr(TermOut)
```

`crate::serialize` 按 `SerializationFormat` 分发：

[crate::serialize 分发](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/main.rs#L113-L132) — `Json` 时按 `pretty` 选 `to_string_pretty` 或紧凑版；`Yaml` 时直接 `serde_yaml::to_string`（YAML 天然多行，不受 `pretty` 影响）。

`SerializationFormat` 是个二选一枚举：

[SerializationFormat 枚举](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L715-L723) — `Json`（默认）与 `Yaml`，派生 `ValueEnum`，所以命令行里写 `--format json`/`--format yaml`。

值得对比的两点：

1. **输出通道不同**：人类可读走 `TermOut`（stderr），机器可读走 `println!`（stdout）。这意味着 `typst info --format json` 的结果可以直接用管道喂给 `jq`，而人类可读的彩色输出不会污染它。
2. **commit 精度不同**：人类可读用 `display_commit` 截到 8 字符；JSON/YAML 里的 `build.commit` 是完整哈希或 `null`。注释 [InfoCommand 文档](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L278-L281) 也明确提醒了这一点。

#### 4.5.3 源码精读

`info()` 的输出分发只有短短几行，却是「双输出」的关键岔路口：

[info() 的 format 分支](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/info.rs#L358-L363) — `command.format` 是 `Option<SerializationFormat>`：`Some` 走 serde 序列化 + stdout，`None` 走 `format_human_readable`（其内部用 `terminal::out()` 即 stderr）。

序列化本身完全委派给公共函数，错误用 `eco_format!` 转成 `StrResult` 冒泡（与 query/eval 同构）：

[crate::serialize 实现](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/main.rs#L115-L132) — 这是 typst-cli 里唯一的序列化真相源；注意 `pretty` 只在 `Json` 分支被读取。

顶层 `info()` 用 `crate::serialize(&value, format, command.pretty)?` 调用它，与 eval 里 `crate::serialize(value, command.format, command.pretty)?` 几乎一模一样——三个命令（info/eval/query）共享同一套「序列化数据并打印」的骨架。

#### 4.5.4 代码实践

1. **实践目标**：对比 json / yaml 两种机器可读输出，并验证 commit 精度差异。
2. **操作步骤**：
   - `./target/debug/typst info --format json --pretty`，用 `jq '.build.commit'` 取出 commit（完整哈希或 `null`）。
   - `./target/debug/typst info --format yaml`，对比 YAML 的字段名与嵌套。
   - `./target/debug/typst info`（不加 format），对比 Version 行里的 commit 是否只有 8 个字符。
3. **需要观察的现象**：JSON 适合用 `jq` 处理；YAML 更短但字段名一致；人类可读的 commit 是截断的，JSON 里的是完整值。
4. **预期结果**：`jq '.build.commit'` 输出完整哈希字符串或 `null`；人类可读 Version 行的括号里只有前 8 字符或 `unknown commit`。确切的哈希值**待本地验证**（取决于编译时是否注入了 `TYPST_COMMIT_SHA`）。
5. 额外验证 `--pretty` 只影响 JSON：运行 `--format yaml --pretty`，确认 YAML 输出与不加 `--pretty` 时一样（因为 [serialize](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/main.rs#L113-L132) 在 YAML 分支根本没读 `pretty`）。

#### 4.5.5 小练习与答案

**练习 1**：`typst info --format json` 的输出能不能直接喂给 `jq`？为什么人类可读输出不行？

> **答案**：能。因为机器可读走 `println!`（stdout）且是合法 JSON；而人类可读走 `TermOut`（stderr）且带分段标题与颜色，不是 JSON。即便颜色关闭，分段文本也不是 JSON 结构，无法被 `jq` 解析。

**练习 2**：为什么 `--pretty` 对 `--format yaml` 没有效果？

> **答案**：[crate::serialize](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/main.rs#L115-L132) 只在 `Json` 分支里根据 `pretty` 选 `to_string_pretty` 或紧凑版；YAML 分支直接用 `serde_yaml::to_string`，它默认就是多行缩进格式，没有「紧凑/美化」之分，所以 `pretty` 被忽略。

## 5. 综合实践

把本讲五个模块串起来，做一次「从环境到输出」的完整追踪。

**场景**：你想确认一台机器上 typst 的字体与包配置是否符合预期，并把它喂给脚本。

1. 先设置一组环境变量，模拟自定义环境：
   ```bash
   export TYPST_FONT_PATHS=/tmp/fonts:/opt/fonts
   export TYPST_IGNORE_SYSTEM_FONTS=false
   export TYPST_FEATURES=html
   export TYPST_PACKAGE_CACHE_PATH=/tmp/typst-cache
   ```
2. 运行 `./target/debug/typst info`（人类可读），对照 [format_human_readable](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/info.rs#L543-L622) 逐段核对：
   - **Fonts → Custom font paths**：应列出 `/tmp/fonts` 与 `/opt/fonts` 两行。这里有个**进阶观察点**——[`info.rs` 用硬编码的 `':'` 切分 `TYPST_FONT_PATHS`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/info.rs#L309-L316)，而 [`args.rs` 里命令行/环境的同名字段用的是平台相关的 `ENV_PATH_SEP`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/args.rs#L21-L23)（Windows 上为 `;`）。所以在 Windows 上，`typst info` 显示的字体路径切分可能与实际编译行为不一致——这是一个值得留意的小边界。
   - **Fonts → System fonts**：`TYPST_IGNORE_SYSTEM_FONTS=false` 经 [parse_bool](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/info.rs#L413-L425) 得 `false`，取反为 `on`（包含系统字体）。
   - **Features**：`html` 经 [parse_features](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/info.rs#L429-L447) 后 `html=on`、`bundle=off`、`a11y-extras=off`。
   - **Packages → Package cache path**：应显示 `/tmp/typst-cache`（因为 `TYPST_PACKAGE_CACHE_PATH` 优先于 [FsPackages::system_cache](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/info.rs#L349-L353) 的平台默认）。
3. 再运行 `./target/debug/typst info --format json --pretty`，用 `jq` 抽取关键字段并对照源码确认命名规则：
   ```bash
   ./target/debug/typst info --format json | jq '{fonts: .fonts, env: .env.TYPST_FONT_PATHS, pkg: .packages."package-cache-path"}'
   ```
   - 顶层 `.fonts` 是 kebab-case 下的键；`.env.TYPST_FONT_PATHS` 是 SCREAMING_SNAKE_CASE；`.packages."package-cache-path"` 又回到 kebab-case——一次输出里同时验证两套命名。
4. 验证软失败：`export TYPST_FEATURES=html,bogus` 后运行 `./target/debug/typst info --format json; echo "exit=$?"`，确认会打印一行 `error: unknown runtime feature: bogus`，但 JSON 仍正常输出且 `exit=0`（因为 [parse_features](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/info.rs#L429-L447) 不调用 `set_failed`）。

> 说明：以上命令的具体输出值（路径、版本号、哈希）**待本地验证**，取决于你的编译目标与 shell 环境；但「字段是否出现、命名风格、退出码行为」可由源码直接推断。

## 6. 本讲小结

- `typst info` 把杂乱来源（编译期常量 + 运行时环境变量 + 平台默认）收拢成一棵 [`Info`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/info.rs#L19-L39) 结构树，六大字段（version/build/features/fonts/packages/env）对应六大类信息，是双输出的单一数据源。
- serde 命名分两套：绝大多数结构用 `kebab-case`，唯独 [`Environment`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/info.rs#L183-L209) 用 `SCREAMING_SNAKE_CASE`，让 JSON 键名与真实环境变量名一致。
- [`get_vars`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/info.rs#L369-L410) 逐个读取环境变量，三分支处理；只有「值非 UTF-8」会 `set_failed()`，变量不存在只返回 `None`；`XDG_*`/`FONTCONFIG_FILE`/`OPENSSL_CONF` 四个字段用 `#[cfg]` 仅在 Unix 编译。
- [`parse_features`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/info.rs#L429-L447)/[`parse_bool`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/info.rs#L413-L425) 复用 clap 的 `ValueEnum::from_str` 与 `BoolValueParser`，让环境变量与命令行的合法取值同源；非法值仅打印错误、不改退出码（尽力而为）。
- 人类可读输出靠 [`format_human_readable`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/info.rs#L543-L622) 三色（青键/绿值/蓝特殊值）+ 对齐填充渲染，走 stderr 的 [`TermOut`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/terminal.rs#L80-L93)，非 TTY 自动无色；commit 哈希会被 `display_commit` 截到 8 字符。
- 机器可读输出走公共 [`crate::serialize`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/main.rs#L113-L132)（json/yaml，与 query/eval 共享），输出到 stdout、commit 给完整值，便于 `jq` 处理。

## 7. 下一步学习建议

- **横向对照「序列化共享」**：阅读 [src/eval.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/eval.rs) 与 [src/query.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-cli/src/query.rs) 对 `crate::serialize` 的调用，体会 info/eval/query 三个命令如何共用同一套「数据 → json/yaml」骨架（对应 u4-l1、u4-l2）。
- **深入包/字体默认路径**：回到 u3-l2，阅读 `typst-kit` 里 `FsPackages::system_data()`/`system_cache()` 的实现，理解 `info` 里 `packages` 字段「环境变量优先、否则平台默认」的回落链路在底层是如何确定那两个默认目录的。
- **端到端验证**：继续 u4-l7 的 smoke 测试，参考 `tests/smoke.rs` 为 `typst info`（尤其是 `--format json` 与带 `TYPST_*` 环境变量的场景）补一个最小端到端测试，把本讲推断的行为固化为回归用例。
