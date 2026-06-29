# Cargo.toml 中的 i18n 配置

## 1. 本讲目标

学完本讲后，你应当能够：

- 看懂 `Cargo.toml` 里 `[package.metadata.i18n]`（或 `[workspace.metadata.i18n]`）每一条配置项的含义、字段名与默认值。
- 理解 `I18nConfig` 这个结构体如何用 `serde` + `toml` 把 `Cargo.toml` 的一段文本反序列化成内存里的配置对象。
- 掌握 `parse` 函数里那个「把 `[package.metadata.i18n]` 字符串重写成 `[i18n]` 再交给 toml 解析」的小技巧，以及它为什么要这么做。
- 解释 `available_locales` 为什么会「自动把 `default_locale` 插到最前面并去重」。
- 理清这套 `Cargo.toml` 配置在 `i18n!` 宏「三级优先级」中处于哪一级（中等优先级基线），以及它和宏显式参数、硬编码默认值的关系。

本讲承接 [u2-l1 i18n! 宏入口与参数解析](u2-l1-i18n-macro-arg-parsing.md)。u2-l1 讲了 `i18n!` 宏如何解析参数并提到「`Cargo.toml` 的 metadata 是低优先级默认值」，本讲就钻进这个 metadata 的具体解析实现。

---

## 2. 前置知识

在开始之前，你需要了解几个基础概念。如果你已经熟悉，可以跳过。

- **`Cargo.toml` 的 `[package.metadata]` 表**：Cargo 规定 `[package.metadata]` 是一个「保留给第三方工具自由使用」的表，Cargo 自己不会校验里面的内容，也不会用它来影响构建。rust-i18n 正是借用这个口子，把自己的配置塞进 `[package.metadata.i18n]`。同理 workspace 级别有 `[workspace.metadata.i18n]`。
- **`serde` 反序列化**：`serde` 是 Rust 生态里把「某种格式的数据（这里是 TOML 文本）」转换成「Rust 结构体」的标准库。给结构体加上 `#[derive(Deserialize)]`，再用 `toml::from_str` 就能把文本变成结构体实例。
- **kebab-case 与 snake_case**：
  - `kebab-case`：单词之间用连字符 `-` 连接，全小写，如 `default-locale`、`minify-key-len`。TOML/YAML 配置文件习惯用这种。
  - `snake_case`：单词之间用下划线 `_` 连接，如 `default_locale`、`minify_key_len`。Rust 的标识符和宏参数习惯用这种。
  - 本讲的一个关键点就是：**配置文件里写 kebab-case，但 Rust 代码（宏参数、字段）里用 snake_case**，两者靠 serde 的 `rename_all` 桥接。
- **三级优先级**（来自 u2-l1）：`i18n!` 宏最终采用的某个配置值，由三档决定，优先级从低到高是：

  1. 硬编码默认值（写死在代码里的常量）。
  2. `Cargo.toml` 里 `[package.metadata.i18n]` 的值 ← **本讲主角**。
  3. `i18n!("...", ...)` 宏调用里显式写的参数（最高优先级，会覆盖前两档）。

---

## 3. 本讲源码地图

本讲涉及三个关键文件：

| 文件 | 作用 |
| --- | --- |
| [crates/support/src/config.rs](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/config.rs) | **本讲核心**。定义 `I18nConfig` 结构体、`parse`、`load`、`MainConfig`，以及配套的单元测试。 |
| [examples/foo/Cargo.toml](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/examples/foo/Cargo.toml) | 一个真实的最小示例，演示 `[package.metadata.i18n]` 的写法。 |
| [crates/macro/src/lib.rs](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs) | 里面的 `load_metadata` 函数，是 `i18n!` 宏调用 `I18nConfig::load`、把配置搬进宏参数结构 `Args` 的桥梁。 |

另外会顺带引用两个常量来源：
- [crates/support/src/minify_key.rs](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/minify_key.rs)：`minify_key` 的四个默认常量值。
- [crates/support/src/lib.rs](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs)：把 `I18nConfig` 对外 `pub use` 的那一行。

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

1. **`I18nConfig`**：i18n 配置的「内存模型」结构体。
2. **`parse`**：把 `Cargo.toml` 文本变成 `I18nConfig` 的核心函数（含字符串重写技巧）。
3. **`load`**：从磁盘读取 `Cargo.toml` 文件的薄封装。
4. **kebab-case serde**：配置文件写法与宏参数之间的命名差异如何桥接。

---

### 4.1 I18nConfig：i18n 配置的内存模型

#### 4.1.1 概念说明

`I18nConfig` 是一个普通的结构体，它把 `Cargo.toml` 里 `[package.metadata.i18n]` 的所有配置项「一对一」地装进自己的字段。可以把它理解成一张「配置登记表」：

| 字段（snake_case） | 配置文件写法（kebab-case） | 类型 | 默认值 | 含义 |
| --- | --- | --- | --- | --- |
| `default_locale` | `default-locale` | `String` | `"en"` | 默认语言 |
| `available_locales` | `available-locales` | `Vec<String>` | `["en"]` | 可用语言列表 |
| `load_path` | `load-path` | `String` | `"./locales"` | 翻译文件目录（相对本 crate） |
| `fallback` | `fallback` | `Vec<String>` | `[]`（空） | 显式回退语言列表 |
| `minify_key` | `minify-key` | `bool` | `false` | 是否启用短键 |
| `minify_key_len` | `minify-key-len` | `usize` | `24` | 短键长度 |
| `minify_key_prefix` | `minify-key-prefix` | `String` | `""`（空） | 短键前缀 |
| `minify_key_thresh` | `minify-key-thresh` | `usize` | `127` | 短键启用阈值（字节） |

注意几个要点：

- **字段名是 snake_case，配置文件是 kebab-case**。这靠结构体上方的 `#[serde(rename_all = "kebab-case")]` 自动转换，本讲 4.4 节专门讲。
- **每个字段都带 `#[serde(default = "...")]`**。意思是：如果 `Cargo.toml` 里没写这一项，就调用对应的默认函数来填值，而不是报错。这让用户「只写自己关心的几项」成为可能。
- **默认值集中写在 `impl Default for I18nConfig` 里**，每个 `default = "xxx"` 函数只是转手返回 `I18nConfig::default()` 的对应字段。这是「单一数据源」的写法——改默认值只改一个地方。

#### 4.1.2 核心流程

`I18nConfig` 这个结构体的「生命周期」可以概括成：

```
Cargo.toml 文本
   │  (1) toml 反序列化 + kebab→snake 映射 + 缺省字段填默认值
   ▼
I18nConfig 实例（内存模型）
   │  (2) 在 parse 里做后处理：插入 default_locale、去重
   ▼
最终 I18nConfig（available_locales 已规范化）
   │  (3) 被 macro 的 load_metadata 读取，搬进 Args
   ▼
i18n! 宏代码生成（作为中等优先级基线）
```

构造它的入口有三个，用途不同：

- `I18nConfig::default()`：拿到全默认配置（不读任何文件）。
- `I18nConfig::new()`：等价于 `default()`，就是个便捷构造器。
- `I18nConfig::load(path)` / `I18nConfig::parse(text)`：从真实 `Cargo.toml` 解析（4.2、4.3 节讲）。

#### 4.1.3 源码精读

先看结构体本体——每个字段、每个 `#[serde(...)]` 属性都对应配置文件里的一项：

[crates/support/src/config.rs:L13-L32](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/config.rs#L13-L32) —— 这就是 `I18nConfig` 结构体。第 14 行的 `#[serde(rename_all = "kebab-case")]` 是命名桥接的关键；第 16、18、20…30 行的 `#[serde(default = "...")]` 保证缺省字段不报错。

再看默认值集中存放处：

[crates/support/src/config.rs:L34-L47](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/config.rs#L34-L47) —— `impl Default`。注意第 41–44 行的四个 `minify_key*` 默认值并不是写死的字面量，而是引用 `crate::DEFAULT_MINIFY_KEY` 等常量。这些常量定义在 minify_key 模块里：

[crates/support/src/minify_key.rs:L6-L15](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/minify_key.rs#L6-L15) —— 四个默认常量：`DEFAULT_MINIFY_KEY = false`、`DEFAULT_MINIFY_KEY_LEN = 24`、`DEFAULT_MINIFY_KEY_PREFIX = ""`、`DEFAULT_MINIFY_KEY_THRESH = 127`。短键机制本身在 u6 系列讲义详细展开，这里你只需要记住「默认是关闭、长度 24、无前缀、阈值 127 字节」即可。

> 为什么阈值是 127？`minify_key_thresh` 的含义是「文案字节数超过这个阈值才启用短键压缩」。127 接近一个字节能表示的上限，意味着「绝大多数普通短文案都不会被压缩，只有特别长的文案才压缩」。具体原理留待 u6-l1。

最后看那组「转手」的默认函数——它们的存在纯粹是为了塞进 `#[serde(default = "...")]`（serde 要求传一个函数名，不能直接传值）：

[crates/support/src/config.rs:L98-L128](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/config.rs#L98-L128) —— 八个 `default_xxx()` 函数，每个都只是 `I18nConfig::default().某字段` 的转发。这样做的好处是：万一以后改默认值，只动 `impl Default` 一处，这些函数自动跟着变。

#### 4.1.4 代码实践

**实践目标**：亲手验证「缺省字段会自动填默认值」。

**操作步骤**（源码阅读型，无需运行）：

1. 打开 [crates/support/src/config.rs](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/config.rs) 的 `test_parse` 测试。
2. 找到其中第二个测试用例（约 L160–L168），它的输入只写了 `available-locales` 和 `load-path` 两项：

   ```toml
   [i18n]
   available-locales = ["zh-CN", "de", "de"]
   load-path = "./my-locales"
   ```

3. 阅读紧跟其后的断言（约 L165–L168）。

**需要观察的现象**：即便输入完全没有 `default-locale`、`minify-key` 等字段，断言里 `cfg.default_locale` 仍然等于 `"en"`，`cfg.load_path` 等于 `"./my-locales"`（被显式覆盖）。

**预期结果**：`#[serde(default = "...")]` 让缺失字段走默认值，显式字段走用户值。这正是「用户只写关心的几项」能工作的根本原因。该断言已在仓库测试中存在，运行 `cargo test -p rust-i18n-support test_parse` 即可看到它通过（见本讲 4.2.4 的运行实践）。

#### 4.1.5 小练习与答案

**练习 1**：如果用户在 `Cargo.toml` 里既不写 `default-locale` 也不写 `available-locales`，最终 `I18nConfig` 的这两个字段分别是什么？

**参考答案**：`default_locale = "en"`、`available_locales = ["en"]`。两者都来自 `impl Default`（[L34-L47](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/config.rs#L34-L47)），再经 serde 的 `default` 函数注入。`test_load_default`（[L202-L210](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/config.rs#L202-L210)）正是验证 support crate 自己的 Cargo.toml（没写 i18n 配置）会得到这个结果。

**练习 2**：为什么 `minify_key*` 的默认值不直接在 `impl Default` 里写 `false`、`24`，而要绕一层 `crate::DEFAULT_MINIFY_KEY` 常量？

**参考答案**：因为这些默认值还会被 `i18n!` 宏（[crates/macro/src/lib.rs:L185-L188](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L185-L188)）当作「硬编码默认值」复用。抽成常量后，support 和 macro 两个 crate 共享同一份真相，避免两处写不同的数字导致行为不一致。

---

### 4.2 parse：字符串重写 + toml 反序列化技巧

#### 4.2.1 概念说明

`parse` 是本讲最精巧的函数。它要解决一个矛盾：

- `Cargo.toml` 里配置写在 `[package.metadata.i18n]`（或 `[workspace.metadata.i18n]`）这个**多层嵌套的表头**下。
- 但我们想让 serde 直接把它反序列化成一个简单的 `struct MainConfig { i18n: I18nConfig }`，期望的 TOML 表头是扁平的 `[i18n]`。

「正确」的做法是先完整解析整个 `Cargo.toml` 成一棵 TOML 树，再下钻到 `package.metadata.i18n` 节点。但那样要依赖能解析完整 `Cargo.toml`（含 `[[bin]]`、`[dependencies]` 等复杂结构）的库，且写起来啰嗦。

rust-i18n 用了一个**取巧但有效**的办法：既然 `[package.metadata.i18n]` 这个表头只是个字符串，那就**在文本层面把它字符串替换成 `[i18n]`**，替换后整段文本里就有了一个标准的 `[i18n]` 表，再丢给 `toml::from_str` 反序列化即可。其余没被替换的内容（如 `[dependencies]`）会被反序列化进 `MainConfig` 时自动忽略——因为 `MainConfig` 只声明了 `i18n` 一个字段，serde 默认忽略未知字段。

#### 4.2.2 核心流程

`parse` 的执行步骤（伪代码）：

```
fn parse(contents):
    1. 检查 contents 里有没有这三种表头之一：
         [i18n]  /  [package.metadata.i18n]  /  [workspace.metadata.i18n]
    2. 如果一个都没有 → 直接返回 I18nConfig::default()（用户根本没配 i18n）
    3. 否则，在文本层面把表头字符串替换成 [i18n]：
         [package.metadata.i18n] → [i18n]
         [workspace.metadata.i18n] → [i18n]
         （如果本来就是 [i18n]，原样不动）
    4. toml::from_str::<MainConfig>(替换后的文本)
         → MainConfig { i18n: I18nConfig{...} }
         （缺省字段由 #[serde(default)] 补齐）
    5. 后处理 available_locales：
         a. 把 default_locale 插到 available_locales 最前面（insert(0, ...)）
         b. 用 itertools::unique 去重
    6. 返回 config.i18n
```

第 5 步的后处理是另一个容易忽略的设计点：**`available_locales` 一定会包含 `default_locale`，且排在第一位**。这样即便用户忘了把默认语言列进 `available-locales`，系统也不会丢掉它。

#### 4.2.3 源码精读

完整函数如下，逐段解读：

[crates/support/src/config.rs:L65-L95](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/config.rs#L65-L95) —— `parse` 函数。

- **L66–L67**：用 `contents.contains(...)` 探测三种表头是否存在。注意这是朴素字符串匹配，不解析 TOML 结构。
- **L69–L71**：三种表头一个都没有时，直接返回默认配置。这是一个重要的「快速出口」——意味着**不写任何 i18n 配置的项目也能正常用 rust-i18n**（全走默认值）。
- **L73–L79**：核心的字符串重写。`contents.replace("[package.metadata.i18n]", "[i18n]")` 把多层表头压平成 `[i18n]`。三个分支分别处理三种表头来源。
- **L81–L82**：`toml::from_str` 把替换后的文本反序列化成 `MainConfig`。失败时包装成 `io::Error(InvalidData)` 返回。
- **L84–L88**：把 `default_locale` 用 `insert(0, ...)` 插到 `available_locales` 最前面。
- **L90–L92**：用 `itertools::unique` 去重（依赖 `use itertools::Itertools;`，见 [L6](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/config.rs#L6)）。注意去重发生在 `insert` 之后，所以「默认语言被重复列出」也能被正确收敛。

配套的 `MainConfig` 只是一个薄包装，存在的唯一目的就是给 serde 一个「期望表头是 `[i18n]`」的落脚点：

[crates/support/src/config.rs:L130-L134](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/config.rs#L130-L134) —— `MainConfig { i18n: I18nConfig }`。它也带 `#[serde(rename_all = "kebab-case")]`，虽然对它自己没影响（字段名 `i18n` 本就是小写），保持一致风格。

> **关于这个技巧的边界**：字符串替换之所以安全，是因为 `[package.metadata.i18n]` 这种字面量在合法 `Cargo.toml` 里只会作为表头出现一次，且不会出现在值或注释里产生歧义。这是一种「在已知约束下用最简手段达成目的」的工程取舍，而非通用方案。

#### 4.2.4 代码实践

**实践目标**：亲眼看到 `parse` 把 `[package.metadata.i18n]` 解析成正确的 `I18nConfig`，并理解 `available_locales` 的去重行为。

**操作步骤**：

1. 在仓库根目录运行 support crate 的单元测试：

   ```bash
   cargo test -p rust-i18n-support test_parse
   cargo test -p rust-i18n-support test_parse_with_metadata
   ```

2. 阅读这两个测试的输入与断言：
   - `test_parse`（[L136-L175](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/config.rs#L136-L175)）：输入用 `[i18n]` 表头，含全部 8 个字段，断言逐一核对。
   - `test_parse_with_metadata`（[L177-L200](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/config.rs#L177-L200)）：输入用 `[package.metadata.i18n]` 表头，字段完全相同，断言也完全相同——证明字符串重写后两种写法结果一致。

**需要观察的现象**：

- `test_parse` 第二个用例输入 `available-locales = ["zh-CN", "de", "de"]`（`de` 重复），断言结果是 `["en", "zh-CN", "de"]`——既插入了 `default_locale = "en"` 到最前，又去掉了重复的 `de`。
- 两个测试都通过。

**预期结果**：两条 `cargo test` 命令均输出 `test result: ok`，且 `available_locales` 经过 `insert(0, default)` + `unique` 后变为 `["en", "zh-CN", "de"]`。

> 如果你的环境无法编译 support crate（例如缺少 `codegen` feature），运行命令可能报错，此时请转为「源码阅读型实践」：对照 L84–L92 的后处理逻辑，手算 `["zh-CN", "de", "de"]` 经过 `insert(0, "en")` 变成 `["en", "zh-CN", "de", "de"]`，再经 `unique` 变成 `["en", "zh-CN", "de"]`，与断言一致即可。

#### 4.2.5 小练习与答案

**练习 1**：如果用户把 `default-locale` 设成 `"zh-CN"`，但 `available-locales` 里只写了 `["en"]`，最终 `available_locales` 是什么？

**参考答案**：`["zh-CN", "en"]`。因为 L84–L88 会把 `default_locale`（`"zh-CN"`）`insert(0, ...)` 到最前面，得到 `["zh-CN", "en"]`，去重后不变。这保证默认语言一定在可用列表里且排首位。

**练习 2**：为什么 `parse` 在没有任何 i18n 表头时返回 `Ok(I18nConfig::default())` 而不是 `Err`？

**参考答案**：为了让「不配置也能用」成为合法路径。rust-i18n 的所有字段都有合理默认值，没写配置等于「全部用默认」。如果改成报错，那么任何一个只想用默认 `en` 的项目都被迫写一段空的 `[package.metadata.i18n]`，体验很差。`test_load_default`（[L202-L210](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/config.rs#L202-L210)）验证了 support crate 自己没配 i18n 时也能拿到默认值。

---

### 4.3 load：从磁盘读取 Cargo.toml

#### 4.3.1 概念说明

`parse` 处理的是「已经在内存里的字符串」，而 `load` 处理的是「磁盘上的 `Cargo.toml` 文件」。`load` 做的事很简单：打开文件 → 读成字符串 → 调 `parse`。它是个薄封装（thin wrapper），把文件 IO 和解析解耦，方便单独测试 `parse`（测试时直接传字符串，不用真的去读文件）。

#### 4.3.2 核心流程

```
fn load(cargo_root):
    1. cargo_root.join("Cargo.toml") → 拼出 Cargo.toml 路径
    2. fs::File::open 打开（失败则 panic，带友好提示）
    3. read_to_string 读成 String
    4. Self::parse(&contents) → 返回 I18nConfig
```

注意第 2 步：打开文件失败时用的是 `unwrap_or_else(|e| panic!(...))`，即**直接 panic** 而不是返回 `Result::Err`。这是因为 `Cargo.toml` 对一个 crate 来说几乎不可能不存在（没有它 cargo 本身就跑不起来），所以把它当「程序员级错误」直接 panic。而后续的 `read_to_string` 和 `parse` 仍走 `?` 返回 `io::Result`。

#### 4.3.3 源码精读

[crates/support/src/config.rs:L54-L63](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/config.rs#L54-L63) —— `load` 函数。第 56–57 行的 `unwrap_or_else` 是「打开失败即 panic」的实现；第 60 行 `read_to_string` 用 `?` 透传 IO 错误；第 62 行转交 `parse`。

那么 `load` 又是被谁调用的呢？是 `i18n!` 宏里的 `load_metadata`：

[crates/macro/src/lib.rs:L125-L146](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L125-L146) —— `load_metadata`。第 127 行读取编译期环境变量 `CARGO_MANIFEST_DIR`（这是 cargo 在编译每个 crate 时自动注入的「该 crate 根目录」），第 129 行调用 `I18nConfig::load(&current_dir)`，第 132–140 行把解析出的字段逐一搬进宏的 `Args` 结构。这一步就是把「`Cargo.toml` 配置」注入「宏参数」的桥梁，也就是三级优先级里的「中等优先级基线」。

> 关键衔接点：`load_metadata` 把 `cfg.load_path` 赋给 `self.locales_path`（L132）。这意味着 `Cargo.toml` 里写的 `load-path` 会成为 `i18n!` 扫描翻译文件的目录——前提是用户没有在宏调用里显式写 `i18n!("my-locales")` 覆盖它（显式参数优先级更高，见 u2-l1）。

#### 4.3.4 代码实践

**实践目标**：验证 `load` 能正确读取一个真实的 `Cargo.toml`。

**操作步骤**：

1. 阅读 [examples/foo/Cargo.toml:L12-L15](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/examples/foo/Cargo.toml#L12-L15)，这是一个真实的最小配置：

   ```toml
   [package.metadata.i18n]
   available-locales = ["en", "zh-CN"]
   default-locale = "en"
   ```

2. 运行针对它的测试：

   ```bash
   cargo test -p rust-i18n-support test_load
   ```

3. 阅读 `test_load`（[crates/support/src/config.rs:L212-L220](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/config.rs#L212-L220)）。它把 `CARGO_MANIFEST_DIR` 拼上 `../../examples/foo` 得到 foo 的根目录，再 `I18nConfig::load` 读取。

**需要观察的现象**：foo 的 `Cargo.toml` 写了 `available-locales = ["en", "zh-CN"]`、`default-locale = "en"`，但断言（L218–L219）期望 `available_locales == ["en", "zh-CN"]`。

**预期结果**：测试通过。注意这里 `default_locale` 是 `"en"`，`insert(0, "en")` 后变成 `["en", "en", "zh-CN"]`，去重后正好 `["en", "zh-CN"]`，与用户写的顺序一致——恰好因为默认语言已经在列表里且排第一。

> **待本地验证**：若 `cargo test -p rust-i18n-support` 因 feature/依赖问题无法在你的环境编译，请改为源码阅读：沿 `test_load → load → parse` 的调用链，确认 foo 的配置经处理后得到 `default_locale = "en"`、`available_locales = ["en", "zh-CN"]`。

#### 4.3.5 小练习与答案

**练习 1**：`load` 打开文件失败时 panic，但 `parse` 失败时返回 `Err`。为什么对待方式不同？

**参考答案**：`Cargo.toml` 不存在属于「几乎不可能、且 cargo 自身也无法工作」的情况，当致命错误 panic 合理；而 `parse` 失败（比如用户把 `[package.metadata.i18n]` 里的某个值写错了类型，toml 反序列化失败）是「用户配置错误」，应当作为可恢复的 `Err` 往上传，最终在 `load_metadata` 里（[L129-L130](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L129-L130)）变成一条友好的编译错误信息。

**练习 2**：`load_metadata` 里用的是 `std::env::var("CARGO_MANIFEST_DIR")`。如果这个环境变量取不到（`Err`），会发生什么？

**参考答案**：看 [L141-L143](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L141-L143)：只有在 `is_debug()`（即设置了 `RUST_I18N_DEBUG=1`）时才返回一条错误提示；否则静默跳过，`Args` 各字段保持硬编码默认值。正常 cargo 构建一定会注入 `CARGO_MANIFEST_DIR`，所以这是一条兜底分支。

---

### 4.4 kebab-case serde：配置文件写法与宏参数的差异

#### 4.4.1 概念说明

本模块专门讲清楚一个容易让初学者踩坑的细节：**同一个概念，在 `Cargo.toml` 里和在 `i18n!` 宏参数里，名字长得不一样**。

| 概念 | `Cargo.toml` 写法（kebab-case） | `i18n!` 宏参数写法（snake_case） |
| --- | --- | --- |
| 默认语言 | `default-locale` | （宏参数不支持，只能来自 metadata） |
| 回退列表 | `fallback` | `fallback`（恰好相同） |
| 短键开关 | `minify-key` | `minify_key` |
| 短键长度 | `minify-key-len` | `minify_key_len` |
| 短键前缀 | `minify-key-prefix` | `minify_key_prefix` |
| 短键阈值 | `minify-key-thresh` | `minify_key_thresh` |

差异的根源是两种场景的惯例不同：TOML/YAML 配置文件习惯用连字符（kebab-case），而 Rust 标识符和宏参数习惯用下划线（snake_case）。rust-i18n 用 serde 的 `rename_all` 把两者桥接起来。

#### 4.4.2 核心流程

桥接靠两处不同的机制：

```
Cargo.toml (kebab-case)
   │  serde #[serde(rename_all = "kebab-case")]
   │  反序列化时把 "minify-key-len" 自动映射到字段 minify_key_len
   ▼
I18nConfig 字段 (snake_case: minify_key_len)
   │  macro 的 load_metadata 直接按字段名搬运
   ▼
Args 字段 (snake_case: minify_key_len)
   │  consume_options 里 match ident.as_str() 匹配 "minify_key_len"
   ▼
i18n!("...", minify_key_len = 12)  ← 用户在宏里必须用 snake_case
```

也就是说：**配置文件用 kebab-case，是因为 serde 在反序列化那一层帮你转了**；**宏参数用 snake_case，是因为宏参数是 Rust token，标识符本来就不能含连字符**。两者各遵循各自场景的惯例，互不冲突。

#### 4.4.3 源码精读

serde 桥接的那一行：

[crates/support/src/config.rs:L13-L14](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/config.rs#L13-L14) —— 第 14 行 `#[serde(rename_all = "kebab-case")]` 告诉 serde：「反序列化时，把结构体字段名（snake_case）当成 kebab-case 去匹配 TOML 的 key」。于是字段 `minify_key_len` 会去匹配 TOML 里的 `minify-key-len`。`MainConfig`（[L130-L131](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/config.rs#L130-L131)）也带同样的属性，保持一致。

而宏参数侧，匹配靠的是 `consume_options` 里的字符串 `match`：

[crates/macro/src/lib.rs:L88-L111](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L88-L111) —— 第 89 行 `input.parse::<Ident>()?.to_string()` 把宏参数的标识符读成字符串，然后 `match ident.as_str()`，分支写的是 `"minify_key_len"`（snake_case）。所以宏调用里必须写 `minify_key_len = 12`，写 `minify-key-len` 会因为不是合法 Rust 标识符而根本无法解析。

> 一个有趣的对比：宏参数里**没有** `default_locale` 这个分支（看 L93–L110 的 match 列表），印证了 u2-l1 讲过的一点——**默认语言只能通过 `Cargo.toml` 配置，不能用宏参数设置**。

#### 4.4.4 代码实践

**实践目标**：亲手验证「kebab-case 配置 → snake_case 字段」的映射，并理解宏参数必须用 snake_case。

**操作步骤**（在仓库里运行已有测试 + 自行实验）：

1. 运行 `cargo test -p rust-i18n-support test_parse_with_metadata`，确认 `[package.metadata.i18n]` 里写 `minify-key-len = 12`（kebab-case）能被正确解析成 `cfg.minify_key_len == 12`（snake_case 字段）。对应断言见 [L198](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/config.rs#L198)。

2. 在你自己的一个临时项目里（**示例代码**，不是仓库原有代码），写一个独立测试，直接调用公开导出的 `I18nConfig::parse`（`I18nConfig` 通过 [crates/support/src/lib.rs:L16](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L16) 的 `pub use` 对外可用）：

   ```rust
   // 示例代码：验证 kebab-case → snake_case 映射
   use rust_i18n_support::I18nConfig;

   #[test]
   fn kebab_maps_to_snake() {
       let toml = r#"
           [package.metadata.i18n]
           default-locale = "en"
           fallback = ["zh"]
           minify-key = true
           minify-key-len = 12
           minify-key-prefix = "T_"
           minify-key-thresh = 16
       "#;
       let cfg = I18nConfig::parse(toml).unwrap();
       assert_eq!(cfg.default_locale, "en");          // default-locale → default_locale
       assert_eq!(cfg.fallback, vec!["zh".to_string()]);
       assert!(cfg.minify_key);                        // minify-key → minify_key
       assert_eq!(cfg.minify_key_len, 12);             // minify-key-len → minify_key_len
       assert_eq!(cfg.minify_key_prefix, "T_");
       assert_eq!(cfg.minify_key_thresh, 16);
   }
   ```

   > 注意：要把 `rust-i18n-support` 作为依赖加入你的临时项目（例如 `rust-i18n-support = { path = "../../crates/support" }`），且因为 `parse` 内部用 `toml`，需确保该依赖带了能解析 toml 的 feature（参考 u5-l3 的 feature 说明）。如果不想搭项目，可直接阅读仓库里等价的 `test_parse_with_metadata`（[L177-L200](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/config.rs#L177-L200)），断言完全一致。

**需要观察的现象**：用 kebab-case 写的配置 key，能被正确读进 snake_case 的字段，断言全部通过。

**预期结果**：测试通过，`cfg` 各字段与写入值一一对应。

#### 4.4.5 小练习与答案

**练习 1**：如果用户在 `Cargo.toml` 里误写成 snake_case 的 `minify_key_len = 12`（而非 `minify-key-len`），会发生什么？

**参考答案**：serde 反序列化时找不到名为 `minify-key-len` 的 key（因为文件里写的是 `minify_key_len`），于是这个字段走 `#[serde(default = "minify_key_len")]`，得到默认值 `24`，而不是用户写的 `12`。而且由于 serde 默认忽略未知字段，`minify_key_len` 这个「陌生 key」会被静默忽略，不报错。这是一种容易踩的坑：**写错命名风格不会报错，只会悄悄用默认值**。

**练习 2**：为什么 `i18n!` 宏参数只能用 snake_case，而 `Cargo.toml` 却「偏好」kebab-case？

**参考答案**：宏参数是 Rust 源码里的 token，标识符受 Rust 语法约束，不能包含连字符 `-`，所以只能用 snake_case（`minify_key_len`）。而 `Cargo.toml` 是 TOML 文本，TOML 的 key 允许连字符，且生态惯例偏好 kebab-case（Cargo 自己的 `Cargo.toml` 字段如 `default-features` 也是 kebab-case）。serde 的 `rename_all = "kebab-case"` 正是用来消除这两种惯例之间的鸿沟。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面这个贯穿任务：

**任务**：为一个虚构的 `my-app` 项目设计 `Cargo.toml` 的 i18n 配置，并验证它被正确解析。

1. **写配置**。参照 [examples/foo/Cargo.toml:L12-L15](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/examples/foo/Cargo.toml#L12-L15) 的写法，为 `my-app` 写一段 `[package.metadata.i18n]`，要求同时包含：
   - `default-locale`、`available-locales`（故意不把默认语言列进去，测试自动插入）
   - `fallback`（一个列表，例如 `["zh", "en"]`）
   - `minify-key = true` 以及 `minify-key-len`、`minify-key-prefix`、`minify-key-thresh` 三项

   ```toml
   # 示例代码：my-app/Cargo.toml 片段
   [package.metadata.i18n]
   default-locale = "en"
   available-locales = ["zh-CN", "ja"]
   fallback = ["zh", "en"]
   minify-key = true
   minify-key-len = 12
   minify-key-prefix = "T_"
   minify-key-thresh = 16
   ```

2. **预测结果**。不运行，先手算：这段配置经过 `I18nConfig::parse` 后，`default_locale`、`available_locales`、`fallback`、`minify_key_len` 分别是什么？（提示：`available_locales` 会经过 `insert(0, default)` + 去重。）

3. **验证**。把上面这段 TOML 文本传给 `I18nConfig::parse`（在你自己的临时测试里，或对照仓库 `test_parse_with_metadata`），用断言核对：
   - `available_locales` 应为 `["en", "zh-CN", "ja"]`（`en` 被自动插到最前）。
   - 其余字段与写入值一致。

4. **回答两个理解性问题**（结合源码）：
   - 如果在 `i18n!("locales", minify_key_len = 8)` 里又显式写了 `minify_key_len`，最终生效的是 12 还是 8？为什么？（提示：回顾三级优先级与 [crates/macro/src/lib.rs:L176-L191](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L176-L191) 里 `load_metadata` 先于 `consume_options` 执行的顺序。）
   - 如果把 `minify-key-len` 误写成 `minify_key_len`，断言里 `minify_key_len` 会是多少？（提示：见 4.4.5 练习 1。）

**预期结果**：第 2 步手算结果与第 3 步断言一致；第 4 步能答出「显式宏参数 8 胜出，因为它后执行并覆盖了 metadata 的 12」「误写 snake_case 会静默落到默认值 24」。

> **待本地验证**：第 3 步若无法搭建独立项目运行，可改为阅读仓库 `test_parse_with_metadata`（[L177-L200](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/config.rs#L177-L200)），其输入与断言与本任务几乎一致，可作为「等价证据」。

---

## 6. 本讲小结

- `I18nConfig`（[config.rs:L13-L32](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/config.rs#L13-L32)）是 i18n 配置的内存模型，8 个字段一一对应 `Cargo.toml` 的配置项，每个字段都带 `#[serde(default)]` 以支持「只写关心的几项」。
- 默认值集中在 `impl Default`（[L34-L47](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/config.rs#L34-L47)），其中 `minify_key*` 引用 `crate::DEFAULT_MINIFY_KEY*` 常量，与 macro crate 共享同一份真相。
- `parse`（[L65-L95](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/config.rs#L65-L95)）用「字符串把 `[package.metadata.i18n]` 重写成 `[i18n]`，再 `toml::from_str`」的取巧办法完成反序列化，并在之后把 `default_locale` 插入 `available_locales` 最前、去重。
- `load`（[L54-L63](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/config.rs#L54-L63)）是文件 IO 的薄封装，被宏的 `load_metadata`（[macro/src/lib.rs:L125-L146](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/macro/src/lib.rs#L125-L146)）调用，把 `Cargo.toml` 配置搬进 `Args`——这就是三级优先级里的「中等优先级基线」。
- 配置文件用 kebab-case、宏参数用 snake_case，靠 `#[serde(rename_all = "kebab-case")]`（[L14](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/config.rs#L14)）桥接；写错命名风格不会报错，只会悄悄走默认值。
- 不写任何 i18n 配置也是合法路径（[L69-L71](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/config.rs#L69-L71) 直接返回默认值），这是「零配置可用」的保障。

---

## 7. 下一步学习建议

本讲把「配置如何被解析成 `I18nConfig`」讲透了，接下来可以按两条线推进：

- **构建与增量重编译（[u5-l2 构建脚本与增量重编译](u5-l2-build-script.md)）**：`Cargo.toml` 里的 `load-path` 决定了 `i18n!` 去哪里扫翻译文件，而 `build.rs` 负责对这些文件发出 `cargo:rerun-if-changed`，让「改了 yml 就自动重编译」成立。建议接着读 `build.rs` 和 `workdir()` 的工程根定位逻辑。
- **Feature flags 与可选依赖（[u5-l3 Feature flags 与可选依赖](u5-l3-feature-flags.md)）**：本讲提到 `parse` 内部依赖 `toml`、`load_path` 可在运行时用 `try_load_locales` 加载，这些都与 feature 有关。下一讲会讲 `codegen` / `load-path` / `log-miss-tr` 三个 feature 如何控制哪些代码进二进制。

如果你对 `minify_key` 的那几个默认常量（`24`、`127` 等）的来历好奇，可以先跳到 [u6-l1 minify_key 短键算法原理](u6-l1-minify-key-algorithm.md) 了解 SipHash + base62 的完整算法，再回到 u5-l2、u5-l3。
