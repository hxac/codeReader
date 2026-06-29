# cargo i18n 命令行入口

## 1. 本讲目标

本讲是「cargo i18n CLI 提取器」单元（u7）的第一讲，聚焦命令行工具自身的入口。学完后你应当能够：

- 看懂 `crates/cli/src/main.rs` 如何用 `clap` 的 derive 宏把二进制包装成一个标准的 **cargo 子命令**（`cargo i18n`）。
- 读懂 `CargoCli` / `I18nArgs` 两个数据结构如何定义子命令、选项与位置参数。
- 掌握 `--translate` / `-t` 选项背后的 `translate_value_parser`：对 `"key => value"` 与纯文本两种输入分别如何解析成 `(key, value)` 元组。
- 理解 `add_translations` 如何把 `--translate` 传进来的翻译手动灌入结果集，以及它与提取器共用同一套「哈希表 key = 查找键、`Message.key` = 译文内容」的约定。
- 理清 `main` 函数把 `iter → extract → generate` 三段主流程串起来的执行顺序。

本讲只讲「命令行入口与主流程编排」，**不展开**源码遍历与 token 提取的细节（u7-l2），也不展开 `TODO.yml` 生成与去重逻辑（u7-l3）。

## 2. 前置知识

阅读本讲前，建议你已经掌握以下内容（对应依赖讲义 u5-l1）：

- **`I18nConfig` 与 `[package.metadata.i18n]`**：rust-i18n 把国际化配置写在 `Cargo.toml` 的 `[package.metadata.i18n]` 节里，由 support crate 的 `I18nConfig` 解析。CLI 工具会复用同一份配置来知道「有哪些 locale」「翻译文件放在哪个目录」「是否开启 minify_key」。
- **`t!` 宏与字面量**：源码里 `t!("hello")`、`t!("views.title")` 这类**首参是字符串字面量**的调用，提取器能自动识别；而 `t!(format!(...))`、`t!(some_var)` 这类**首参不是字面量**的调用，提取器抓不到，需要人工通过 `--translate` 补登记。
- **minify_key 短键**（u6 系列）：当配置开启 `minify_key` 时，长文案会被哈希成短键当作查找键；CLI 在手动添加翻译时也要用同一套算法算键，否则查找会 miss。

补充几个本讲要用到的 CLI 基础概念：

- **cargo 子命令**：cargo 允许第三方工具以 `cargo-<name>` 命名二进制，用户运行 `cargo <name> ...` 时，cargo 会去 `PATH` 里找 `cargo-<name>` 并以 `cargo-<name> <name> ...` 的形式调用它。本讲的二进制名是 `cargo-i18n`，对应子命令 `cargo i18n`。
- **clap derive**：用 `#[derive(Parser)]` / `#[derive(Args)]` 加属性宏的方式，把一个 Rust 结构体直接翻译成命令行参数定义，省去手写解析代码。
- **过程退出码**：`main` 返回 `Ok(())` 时进程退出码为 0（成功），返回 `Err` 或调用 `std::process::exit(1)` 时退出码非 0，后者常用于让 CI 在「还有未翻译文案」时失败。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| `crates/cli/src/main.rs` | CLI 二进制入口。定义 `CargoCli`/`I18nArgs`、`translate_value_parser`、`add_translations`、`main`，是本讲的绝对主角。 |
| `crates/cli/Cargo.toml` | 声明二进制名 `cargo-i18n`、依赖（clap、support 的 `codegen` feature、extract）。 |
| `crates/support/src/config.rs` | `I18nConfig::load` 从 `Cargo.toml` 读取配置，CLI 在 `main` 里调用它。 |
| `crates/extract/src/iter.rs` | `iter_crate` 用 `ignore` 库遍历源码目录、回调每个 `.rs` 文件。 |
| `crates/extract/src/extractor.rs` | `extract` / `Message`：基于 token 流识别 `t!`/`tr!` 并提取首参字面量。 |
| `crates/extract/src/generator.rs` | `generate`：对比已有翻译做去重，把新文案写成 `TODO.yml`。 |

其中后三个（iter / extractor / generator）属于 `rust-i18n-extract` 库，CLI 只是「调度者」，本讲只点到为止，细节留待 u7-l2、u7-l3。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：**CargoCli/I18nArgs（命令定义）→ translate_value_parser（输入解析）→ add_translations（手动灌入）→ main（主流程编排）**。它们正好对应一条数据从「命令行字符串」走到「最终被生成进 `TODO.yml`」的链路。

### 4.1 CargoCli / I18nArgs：用 clap derive 定义 cargo 子命令

#### 4.1.1 概念说明

`cargo i18n` 不是一个独立可执行文件的「原生命令」，而是一个 **cargo 子命令**。它的实现关键有两点：

1. 二进制必须命名为 `cargo-i18n`。这样 cargo 在用户输入 `cargo i18n` 时，才能在 `PATH` 中找到它，并以 `cargo-i18n i18n <其余参数>` 的形式启动（cargo 会把子命令名 `i18n` 作为第一个参数透传给二进制）。
2. 程序内部用 clap 把「顶层命令名」声明成 `cargo`，再用一个枚举变体 `I18n` 接收 `i18n` 子命令。这样 clap 解析到的 argv 形如 `["cargo-i18n", "i18n", ...]` 时，能把第一个 token `i18n` 匹配到 `I18n` 变体，并把后续参数交给 `I18nArgs`。

> 小提示：枚举变体名 `I18n` 经 clap 的命名转换会变成子命令名 `i18n`（驼峰 → 小写）。这就是「变体名」与「子命令名」的对应关系。

#### 4.1.2 核心流程

clap derive 的解析流程可以概括为：

```
用户输入: cargo i18n -t "A => B" -- ./src
          │
cargo 实际启动: cargo-i18n i18n -t "A => B" -- ./src
          │
clap 解析:
  顶层命令 name/bin_name = "cargo"
  ├─ 第 1 个 token "i18n"  →  匹配枚举变体 I18n
  └─ 剩余 token 交给 I18nArgs
        ├─ "-t" / "--translate"  →  translate 字段（可多次、自定义解析器）
        └─ 末尾 "-- ./src"      →  source 字段（位置参数、默认 "./"）
```

`I18nArgs` 暴露两个面向用户的输入：

- `-t` / `--translate`：手动添加翻译，可重复（`num_args(1..)`），值经 `translate_value_parser` 解析成 `(key, value)` 元组，整个字段类型是 `Option<Vec<(String, String)>>`。
- 位置参数 `SOURCE`：要扫描的源码目录，默认 `"./"`，且标记 `last = true` 表示它必须出现在 `--` 之后（即 `cargo i18n -- ./src`）。

#### 4.1.3 源码精读

先看二进制名声明，这是它能成为 cargo 子命令的前提——`[[bin]]` 的 `name` 必须是 `cargo-i18n`：

[crates/cli/Cargo.toml:16-18](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/cli/Cargo.toml#L16-L18) —— 声明二进制名为 `cargo-i18n`，入口为 `src/main.rs`。

再看顶层命令枚举 `CargoCli`。`name = "cargo"` 与 `bin_name = "cargo"` 让 clap 在生成帮助/用法时显示成 `cargo i18n ...`（与用户实际敲的命令一致），而非 `cargo-i18n i18n ...`：

[crates/cli/src/main.rs:8-13](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/cli/src/main.rs#L8-L13) —— `CargoCli` 枚举只有一个变体 `I18n(I18nArgs)`，对应子命令 `i18n`。

然后是承载实际选项的 `I18nArgs`。结构体上方的文档注释会成为 `--help` 的说明文字；`#[command(author, version)]` 让 clap 自动把 `Cargo.toml` 的作者与版本填进帮助：

[crates/cli/src/main.rs:15-40](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/cli/src/main.rs#L15-L40) —— `I18nArgs` 定义了 `translate` 与 `source` 两个字段。

其中两个字段的属性值得逐条拆开看：

- `translate` 字段：

[crates/cli/src/main.rs:35-36](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/cli/src/main.rs#L35-L36) —— `short`(`-t`)、`long`(`--translate`)、`num_args(1..)`(可接收多个值)、`value_parser = translate_value_parser`(自定义解析)，类型是 `Option<Vec<(String, String)>>`。`verbatim_doc_comment` 让文档注释原样输出到帮助。

- `source` 字段：

[crates/cli/src/main.rs:37-39](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/cli/src/main.rs#L37-L39) —— `default_value = "./"`(默认当前目录)、`last = true`(必须放在 `--` 之后)，是位置参数（无 `short`/`long`）。

> 注意：`source` 标了 `last = true`，所以它的「默认值 `./`」只有在用户**完全省略** `-- <SOURCE>` 时才生效。也就是说，`cargo i18n` 与 `cargo i18n -- ./` 等价，都扫描当前目录。

#### 4.1.4 代码实践

**实践目标**：通过实际运行帮助命令，验证 `I18nArgs` 的 clap 定义与「帮助文本」的对应关系。

**操作步骤**：

1. 在仓库根目录用本地源码运行（不必全局安装）：
   ```bash
   cargo run -p rust-i18n-cli -- i18n -h
   ```
   若想安装为真正的 `cargo i18n` 子命令，可执行 `cargo install rust-i18n-cli`。
2. 阅读输出的 `Usage:` 行、`Arguments:` 段、`Options:` 段。

**需要观察的现象**：

- `Usage:` 形如 `cargo i18n [OPTIONS] [-- <SOURCE>]`——注意 `[SOURCE]` 前的 `--`，正是 `last = true` 的体现。
- `Arguments:` 里 `SOURCE` 标注 `[default: ./]`。
- `Options:` 里有 `-t, --translate <TEXT>...`，且带有「This is useful for non-literal values in the `t!` macro.」的说明。

**预期结果**：帮助文本中能看到 source 默认值为 `./`、`-t/--translate` 选项存在并支持多值。具体的版本号字符串会取自 `Cargo.toml` 的 `version = "4.1.0"`（README 里 `cargo i18n -h` 示例显示的 `3.1.0` 是旧版快照，已过时）。

> 待本地验证：clap 在不同版本下帮助文本的精确排版（缩进、空行）可能略有差异，以你本地实际输出为准。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `CargoCli` 的 `#[command(bin_name = "cargo")]` 去掉，`cargo i18n -h` 的 `Usage:` 行会变成什么样？为什么？

**参考答案**：会变成形如 `cargo-i18n i18n [OPTIONS] [-- <SOURCE>]`，因为 `bin_name` 决定了 clap 在帮助里展示的程序调用方式；不显式设置时 clap 会用实际二进制名 `cargo-i18n`，这与用户敲的 `cargo i18n` 不一致。

**练习 2**：为什么 `I18nArgs` 用 `#[derive(Args)]` 而不是 `#[derive(Parser)]`？

**参考答案**：`I18nArgs` 是子命令的参数集（被嵌在 `CargoCli::I18n(I18nArgs)` 里），clap 中子命令参数用 `Args` 派生；而最外层负责「分发子命令」的 `CargoCli` 才用 `Parser` 派生。

---

### 4.2 translate_value_parser："key => value" 与纯文本两种解析

#### 4.2.1 概念说明

`--translate` 的值由自定义函数 `translate_value_parser` 解析，它要兼容两种用户写法：

1. **纯文本**：`-t "Hello, world!"`。此时用户只想「登记一条还没翻译的文案」，key 与内容相同，都等于这段文本。
2. **键值对**：`-t "Hello, world! => Hola, world!"`。此时用户直接给出「原文 => 译文」，key 是原文、内容是译文。

为什么要支持两种？因为 `t!` 宏里既有 `t!("hello")`（key 是简短标识）这种字面量调用，也有 `t!(format!("Hello, {}!", name))` 这种**首参不是字面量**的调用——后者提取器抓不到，用户只能用 `--translate` 手动登记；有时用户已经想好译文，就顺手用 `=>` 一次给全。

#### 4.2.2 核心流程

```
输入字符串 s
   │
   ├─ 含 "=>" ?  ──Yes──►  按 "=>" 切成 (key, msg)
   │                        两端 trim 后去掉首尾引号
   │                        返回 (key, msg)
   │
   └─ 否          ─────────►  返回 (s, s)   // key == 内容
```

注意 `split_once("=>")` 只在**第一次**出现 `=>` 处切分，因此译文里再出现 `=>` 也不会被误切。两端用 `trim()` 去空白，再经 `remove_quotes` 去掉首尾的 `"`（让用户可以给字符串加引号也能被正确识别）。

#### 4.2.3 源码精读

`remove_quotes` 是个小工具，去掉字符串首尾的引号字符：

[crates/cli/src/main.rs:43-53](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/cli/src/main.rs#L43-L53) —— 仅对首尾各一个 `"` 做裁剪，不做转义处理。

主体是 `translate_value_parser`。它的返回类型是 `Result<(String, String), std::io::Error>`——这是 clap 自定义 `value_parser` 要求的「返回 `Result<T, E>`」形态：

[crates/cli/src/main.rs:56-64](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/cli/src/main.rs#L56-L64) —— 用 `split_once("=>")` 区分两种输入；`else` 分支把整串同时当作 key 与内容。

两条分支对照：

| 输入 | `split_once("=>")` | 返回 `(key, msg)` |
| --- | --- | --- |
| `Hello, world!` | `None` | `("Hello, world!", "Hello, world!")` |
| `Hello, world! => Hola, world!` | `Some(("Hello, world! ", " Hola, world!"))` | `("Hello, world!", "Hola, world!")` |
| `"hello" => "你好"` | `Some(("\"hello\" ", " \"你好\""))` | `("hello", "你好")`（引号被 `remove_quotes` 去掉） |

> 注意：这个解析器**永远不会返回 `Err`**（两条分支都返回 `Ok`）。也就是说，任何字符串都能被接受为合法的 `--translate` 值。

#### 4.2.4 代码实践

**实践目标**：在不运行程序的情况下，纯靠阅读 `translate_value_parser` 推断若干输入的解析结果，验证你对两种分支的理解。

**操作步骤**：

1. 准备 3 个测试输入：
   - `t!("hello") is here`（注意：这只是个含特殊字符的普通字符串，用来测试纯文本分支）
   - `Hello, world! => Hola, world!`
   - `"views.title" => "标题"`
2. 对每个输入，手动模拟 `split_once("=>")` → `trim()` → `remove_quotes()` 三步。
3. 写出预期的 `(key, msg)`。

**需要观察的现象 / 预期结果**：

- 输入 1：无 `=>`，返回 `("t!(\"hello\") is here", "t!(\"hello\") is here")`。
- 输入 2：返回 `("Hello, world!", "Hola, world!")`。
- 输入 3：返回 `("views.title", "标题")`。

> 待本地验证：你可以把 `translate_value_parser` 的逻辑复制到一个独立的 `fn` 里写几个 `assert_eq!` 来确认（注意它依赖 `remove_quotes`，需一并复制）。

#### 4.2.5 小练习与答案

**练习 1**：`-t "a => b => c"` 会被解析成什么？

**参考答案**：`split_once("=>")` 只切第一次，得到 `("a ", " b => c")`，trim 后 key=`"a"`、msg=`"b => c"`。即译文里允许出现 `=>`。

**练习 2**：为什么解析器返回的是 `std::io::Error` 而不是自定义错误类型？

**参考答案**：clap 的自定义 `value_parser` 只要求返回 `Result<T, E>` 且 `E: Into<clap::Error>`（或满足其错误兼容约束）。这里复用 `std::io::Error` 是为了与项目里 `generator`、`I18nConfig::load` 等同样返回 `io::Result` 的代码风格保持一致；而且本解析器实际上从不产生错误。

---

### 4.3 add_translations：把 --translate 的值灌入结果集

#### 4.3.1 概念说明

`--translate` 收集到的 `Vec<(String, String)>` 并不能直接交给生成器，它要先被「翻译」成与提取器产出**同一种数据结构**的 `Message`，并合并进同一个 `HashMap<String, Message>` 结果集里。这件事由 `add_translations` 完成。

这里有一个贯穿整个提取器的关键约定（在 u7-l2 会再次遇到）：

- **HashMap 的 key** = 「查找键」（lookup key）：运行时 `t!` 实际用来查表的键。未开 minify_key 时就是文案本身（或其规范化形式）；开了 minify_key 时是哈希后的短键。
- **`Message.key` 字段** = 「译文内容」（content）：写入 `TODO.yml` 的默认值来源。

`add_translations` 严格遵循这个约定：用 `(item.0)` 计算查找键、用 `item.1` 填 `Message.key`。

#### 4.3.2 核心流程

```
对 list 里每个 item = (raw_key, content):
   │
   ├─ cfg.minify_key == true ?
   │     ├─ Yes → lookup_key = minify_key(raw_key, len, prefix, thresh)  // 哈希短键
   │     └─ No  → lookup_key = raw_key                                   // 原样
   │
   ├─ index = results.len()                       // 用于稳定排序
   │
   └─ results.entry(lookup_key).or_insert(Message {
          key: content,                            // 译文内容
          index,
          minify_key: cfg.minify_key,
          locations: vec![],                       // 手动添加，无源码位置
      })
```

`entry(lookup_key).or_insert(...)` 的语义是「如果该查找键已存在则不动，否则插入」——这意味着**提取器先抓到的同名键优先**，`--translate` 不会覆盖提取器已经登记的文案（因为 `add_translations` 总是在 `iter`/`extract` 之后调用，见 4.4）。

#### 4.3.3 源码精读

`add_translations` 的完整实现：

[crates/cli/src/main.rs:67-97](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/cli/src/main.rs#L67-L97) —— 遍历 `list`，按 minify_key 开关决定查找键，再 `entry().or_insert()` 合并进 `results`。

几个关键点逐条对应源码：

- 解构出 minify_key 相关的四个配置项（`minify_key` 总开关、`minify_key_len`、`minify_key_prefix`、`minify_key_thresh`）：

[crates/cli/src/main.rs:72-78](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/cli/src/main.rs#L72-L78) —— 用 `..` 忽略其它字段，只取 minify_key 四件套。

- 查找键的计算分支：

[crates/cli/src/main.rs:82-89](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/cli/src/main.rs#L82-L89) —— 开启时调用 `MinifyKey::minify_key(item.0, ...)`（trait 方法，由 support crate 提供，详见 u6-l1）；否则直接克隆 `item.0`。

- 插入 `Message`：

[crates/cli/src/main.rs:90-95](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/cli/src/main.rs#L90-L95) —— `Message.key = item.1`（内容），`locations` 为空数组。

> 对照 u4-l1 / u7-l2：`Message` 结构定义在 `crates/extract/src/extractor.rs`，字段为 `key`(内容)、`index`(顺序)、`minify_key`(开关)、`locations`(源码位置)。`add_translations` 与提取器的 `take_message` 用的就是同一个结构、同一套约定。

#### 4.3.4 代码实践

**实践目标**：理解 minify_key 开关如何影响 `add_translations` 写入的「查找键」与「内容」。

**操作步骤**（源码阅读型）：

1. 假设 `cfg.minify_key = false`，用户传了 `-t "Hello, world! => Hola, world!"`。回答：`results` 里会插入哪个 key？对应的 `Message.key` 是什么？
2. 假设 `cfg.minify_key = true`、参数为默认值。同样的 `-t` 输入，`results` 的 key 又会是什么？`Message.key` 呢？

**预期结果**：

- 情形 1（minify 关）：`results` 的 key = `"Hello, world!"`，`Message.key = "Hola, world!"`。
- 情形 2（minify 开）：`results` 的 key = `minify_key("Hello, world!", 24, "", 127)` 算出的短键（一个 base62 短串，待本地验证具体值），`Message.key` 仍是 `"Hola, world!"`。

**核心结论**：minify_key 只影响「查找键」（HashMap key），不影响「内容」（`Message.key`）。这与 u6-l3 讲的「键一致性铁律」一致——CLI 必须和运行时 `t!`、提取器用同一组 minify_key 参数，否则短键对不上。

#### 4.3.5 小练习与答案

**练习 1**：如果提取器已经抓到 `"hello"` 这个键，用户又用 `-t "hello"` 再次添加，会发生什么？

**参考答案**：因为 `add_translations` 在 `extract` 之后调用，且用的是 `entry(...).or_insert(...)`，所以已存在的 `"hello"` 不会被覆盖，`--translate` 的这条被静默忽略。即「提取器优先」。

**练习 2**：`add_translations` 里 `index = results.len()` 有什么作用？

**参考答案**：`index` 记录插入时的结果集大小，作为稳定排序键。`main` 最后会按 `index` 对所有 message 排序（见 4.4），从而让 `TODO.yml` 里的文案顺序大致按「先提取/添加的在前」，而不是 HashMap 的随机顺序。

---

### 4.4 main：串起 iter → extract → generate 的主流程

#### 4.4.1 概念说明

`main` 是整个 CLI 的「总调度」。它本身不扫描源码、不解析 token、不写文件，而是把三段真正干活的逻辑（都在 `rust-i18n-extract` 库里）按固定顺序串起来，并在中间插入「加载配置」和「合并手动翻译」两个步骤。

整体顺序是：

```
解析命令行  →  加载配置  →  遍历源码并提取  →  合并手动翻译  →  排序  →  生成 TODO.yml
```

理解这个顺序很重要，因为它解释了几个行为：手动翻译不会覆盖提取结果（因为提取在前）、`TODO.yml` 写入位置由配置的 `load-path` 决定、以及「还有未翻译文案」时进程会以非 0 退出（让 CI 失败）。

#### 4.4.2 核心流程

`main` 的伪代码：

```
1. args = CargoCli::parse()                      // 解析命令行，拿到 I18nArgs
2. results = HashMap::new()                      // 存放所有提取到的 Message
3. cfg = I18nConfig::load(source_path)            // 从 source_path/Cargo.toml 读配置
4. iter::iter_crate(source_path, |path, src| {    // 遍历每个 .rs 文件
       extractor::extract(&mut results, path, src, cfg.clone())
   })
5. if let Some(list) = args.translate {           // 有 --translate 才合并
       add_translations(&list, &mut results, &cfg)
   }
6. messages = results 收集并按 index 排序
7. output_path = source_path / cfg.load_path      // 例如 ./locales
8. generator::generate(output_path, cfg.available_locales, messages)
9. 若 generate 返回 Err → exit(1)                  // 让 CI 失败
```

注意第 3 步：配置是从 **`source_path` 指向的目录**里的 `Cargo.toml` 读取的，而不是 CLI 自己的 `Cargo.toml`。这正是 README 强调的「`load-path` 必须与你传给 `i18n!` 的路径一致」——CLI 和编译期 `i18n!` 读的是同一份 `Cargo.toml`、同一个 `load-path`。

#### 4.4.3 源码精读

`main` 的完整实现：

[crates/cli/src/main.rs:99-133](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/cli/src/main.rs#L99-L133) —— 解析参数、加载配置、遍历提取、合并翻译、排序、生成。

逐段拆开：

- 解析命令行，并用 `let ... = ...` 解构出唯一的 `I18n` 变体：

[crates/cli/src/main.rs:100](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/cli/src/main.rs#L100) —— `CargoCli::parse()` 返回枚举，模式匹配拿到内部的 `I18nArgs`。

- 加载配置（关键：配置来自 `source_path` 目录）：

[crates/cli/src/main.rs:104-106](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/cli/src/main.rs#L104-L106) —— `source.expect("Missing source path")` 取出位置参数（有默认值 `./`，正常不会 panic）；`I18nConfig::load` 读该目录下的 `Cargo.toml`。

> `I18nConfig::load` 的实现见 [crates/support/src/config.rs:54-63](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/config.rs#L54-L63)：它打开 `cargo_root.join("Cargo.toml")`，读出内容后交给 `I18nConfig::parse`（u5-l1 讲过的表头改名 + toml 反序列化逻辑）。若该 `Cargo.toml` 里没有任何 `[i18n]`/`[package.metadata.i18n]` 节，`parse` 会返回 `I18nConfig::default()`——所以**不写配置也能跑**，默认 `load_path = "./locales"`、`available_locales = ["en"]`。

- 遍历源码并提取：

[crates/cli/src/main.rs:108-110](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/cli/src/main.rs#L108-L110) —— `iter::iter_crate` 对每个 `.rs` 文件回调闭包，闭包调用 `extractor::extract` 把识别到的 `t!`/`tr!` 写进 `results`。`cfg.clone()` 是因为回调可能被多次调用，而 `extract` 按值接收 `cfg`。

> `iter_crate` 用 `ignore::WalkBuilder` 遍历目录（尊重 `.gitignore`），实现见 [crates/extract/src/iter.rs:6-45](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/extract/src/iter.rs#L6-L45)，详见 u7-l2。

- 合并手动翻译（条件执行）：

[crates/cli/src/main.rs:112-114](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/cli/src/main.rs#L112-L114) —— 仅当用户传了 `-t/--translate`（`Option` 为 `Some`）时才调用 `add_translations`。

- 排序：

[crates/cli/src/main.rs:116-117](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/cli/src/main.rs#L116-L117) —— 把 HashMap 收集成 Vec，按 `Message.index` 升序排列，保证输出顺序稳定。

- 计算输出路径并生成：

[crates/cli/src/main.rs:121-126](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/cli/src/main.rs#L121-L126) —— `output_path = source_path / cfg.load_path`（如 `./locales`），交给 `generator::generate`；若返回 `Err`，置 `has_error = true`。

- 失败退出：

[crates/cli/src/main.rs:128-130](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/cli/src/main.rs#L128-L130) —— 有错误时 `std::process::exit(1)`，让 CI 红灯。

> 关于「为什么 generate 返回 Err」：`generator::generate` 在「所有文案都已翻译」时返回 `Ok(())` 并打印 `All thing done.`；在「还有未翻译文案」时写出 `TODO.yml` 后**故意返回 `Err`**（见 [crates/extract/src/generator.rs:33-35](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/extract/src/generator.rs#L33-L35)）。这是一种刻意设计：让 `cargo i18n` 在 CI 里充当「翻译完整性检查门禁」，有遗漏就失败。详见 u7-l3。
>
> 关于 README 示例的过时之处：README 里 `cargo i18n` 的示例输出显示写成多个 `TODO.en.yml` / `TODO.fr.yml` 等按语言拆分的文件，但当前代码（generator.rs）实际只写**单个** `TODO.yml`（`_version: 2` 多语言合并格式，见 [crates/extract/src/generator.rs:15](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/extract/src/generator.rs#L15)）。以源码为准。

#### 4.4.4 代码实践

**实践目标**：跟踪 `main` 的执行顺序，回答若干「如果……会怎样」的问题，巩固对主流程的理解。

**操作步骤**（源码阅读型）：

1. 假设你在 `examples/foo` 目录下运行 `cargo i18n`（该目录 `Cargo.toml` 含 `[package.metadata.i18n]`，`available-locales = ["zh-CN"]`，`default-locale = "en"`，`load-path = "locales"`）。请回答：
   - `I18nConfig::load` 读的是哪个 `Cargo.toml`？得到的 `available_locales` 列表是什么（注意 u5-l1 讲过 `default_locale` 会被插到最前并去重）？
   - `output_path` 最终指向哪个目录？
2. 假设该目录的 `Cargo.toml` 完全没有 `[package.metadata.i18n]` 节。`I18nConfig::load` 会返回什么？`output_path` 会指向哪里？
3. 假设源码里所有 `t!` 文案都已在 `locales/` 下翻译完毕。`generator::generate` 会返回 `Ok` 还是 `Err`？进程退出码是几？

**预期结果**：

1. 读 `examples/foo/Cargo.toml`；`available_locales = ["en", "zh-CN"]`（`default-locale = "en"` 插入最前）；`output_path = examples/foo/locales`。
2. 返回 `I18nConfig::default()`：`default_locale = "en"`、`available_locales = ["en"]`、`load_path = "./locales"`；`output_path = ./locales`。
3. 返回 `Ok(())`，打印 `All thing done.`，`has_error` 保持 `false`，进程以退出码 0 结束（`main` 返回 `Ok(())`）。

> 待本地验证：情形 1 的实际 `available_locales` 顺序可对照 `crates/support/src/config.rs` 里的 `test_load` 测试用例（该测试加载 `examples/foo` 并断言 `available_locales == ["en", "zh-CN"]`）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `extractor::extract` 用的是 `cfg.clone()`，而不是传 `&cfg`？

**参考答案**：`iter::iter_crate` 的回调签名要求返回 `Result<(), Error>`，而 `extract` 按值接收 `cfg: I18nConfig`（内部 `Extractor` 结构持有所有权）。闭包会被多次调用，每次都要一份 `cfg`，所以只能 `clone`。若改成引用，`extract` 的签名与 `Extractor` 持有方式都要随之改为借用以匹配生命周期。

**练习 2**：如果想让 `cargo i18n` 在「没有未翻译文案」时也让 CI 失败，应该改哪里？

**参考答案**：当前设计恰好相反——`generate` 在全部翻译完成时返回 `Ok`。要反转语义，需改 `generator::generate`（u7-l3）的返回逻辑，或改 `main` 末尾对 `result` 的判定。这超出了「入口」范畴，但说明了一个设计要点：**「成功/失败的判定」封装在 generator 里，`main` 只是忠实地把 `Err` 翻译成退出码 1**。

## 5. 综合实践

把本讲四个模块串起来，做一个端到端的「命令行入口」跟踪练习。

**任务背景**：假设你的项目 `Cargo.toml` 含如下配置：

```toml
[package.metadata.i18n]
available-locales = ["en", "zh-CN"]
default-locale = "en"
load-path = "locales"
fallback = ["zh-CN"]
minify-key = false
```

源码 `src/main.rs` 里有一行 `t!(format!("Hello, {}!", "world"))`（首参不是字面量，提取器抓不到），你希望用 CLI 手动登记它的中文译文。

**请完成**：

1. 写出你会使用的完整命令（含 `-t` 的 `key => value` 写法），并说明 `source` 参数你打算怎么给（或省略）。
2. 用本讲学到的知识，逐步说明这条命令进入 `main` 后的执行顺序：
   - `translate_value_parser` 把你的 `-t` 值解析成什么 `(key, msg)`？
   - `I18nConfig::load` 得到的 `available_locales`、`load_path` 分别是什么？
   - `add_translations` 写入 `results` 的查找键与 `Message.key` 分别是什么？
   - `output_path` 指向哪里？最终会生成什么文件？
3. 解释：为什么这条文案必须用 `--translate` 而不能指望提取器自动抓？

**参考要点**：

1. 命令例如：`cargo i18n -t "Hello, world! => 你好，world!"`（`source` 省略，默认 `./`）。也可写 `cargo i18n -t "Hello, world! => 你好，world!" -- ./`。
2. 执行顺序：
   - `translate_value_parser("Hello, world! => 你好，world!")` → `("Hello, world!", "你好，world!")`。
   - `I18nConfig::load` → `available_locales = ["en", "zh-CN"]`、`load_path = "locales"`。
   - `add_translations`（minify_key 关）→ 查找键 = `"Hello, world!"`，`Message.key = "你好，world!"`。
   - `output_path = ./locales`，最终生成 `./locales/TODO.yml`（`_version: 2` 多语言格式）。
3. 因为 `t!(format!(...))` 的首参是 `format!` 宏调用、不是字符串字面量，提取器的 `take_message` 只取「宏首参是字面量」的调用（详见 u7-l2），所以抓不到，只能靠 `--translate` 手动登记。这正是 `-t` 选项「useful for non-literal values in the `t!` macro」的设计初衷。

> 待本地验证：在真实项目里跑一遍上述命令，检查 `locales/TODO.yml` 是否包含你手动登记的键，并确认进程退出码（有未翻译内容时应为 1）。

## 6. 本讲小结

- `cargo i18n` 是一个 **cargo 子命令**：二进制名必须是 `cargo-i18n`，程序内用 `CargoCli`（`name/bin_name = "cargo"`）+ 枚举变体 `I18n` 让 clap 把 `i18n` 识别为子命令。
- `I18nArgs` 用 `#[derive(Args)]` 定义两个输入：`-t/--translate`（可重复、自定义解析、类型 `Vec<(String,String)>`）与位置参数 `SOURCE`（默认 `./`、`last = true` 必须在 `--` 之后）。
- `translate_value_parser` 用 `split_once("=>")` 兼容两种写法：纯文本返回 `(s, s)`，`"k => v"` 返回 `(k, v)`（两端 trim、去引号）；它永不报错。
- `add_translations` 把 `--translate` 的值按「查找键 = item.0（minify 开则哈希）、`Message.key` = item.1」的约定灌入结果集，与提取器共用 `Message` 结构；用 `entry().or_insert()` 实现「提取器优先、手动翻译不覆盖」。
- `main` 把主流程串成：解析 → `I18nConfig::load`(读 `source_path` 的 `Cargo.toml`) → `iter`/`extract` → `add_translations` → 按 `index` 排序 → `generator::generate`(写到 `source_path/load_path`) → `Err` 则 `exit(1)`。
- 配置来自**被扫描项目**的 `Cargo.toml` 而非 CLI 自身；`load-path` 必须与编译期 `i18n!` 的路径一致；无配置时回落到 `I18nConfig::default()`。

## 7. 下一步学习建议

本讲只讲了「入口与调度」，真正的「干活」逻辑都在 `rust-i18n-extract` 库里。建议接下来学习：

- **u7-l2 源码遍历与 t! 提取**：深入 `iter::iter_crate` 如何用 `ignore` 库遍历源码、`Extractor::invoke` 如何基于 `proc_macro2` 的 token 流识别 `t!`/`tr!` 宏调用、`take_message` 如何取出首参字面量并记录行号。读完你会彻底明白为什么 `t!(format!(...))` 抓不到。
- **u7-l3 生成 TODO.yml 与合并去重**：深入 `generator::generate` 如何对比已有翻译做去重、用 `_version: 2` 格式写出 `TODO.yml`，以及「有遗漏就返回 `Err` 让 CI 失败」的完整设计。

同时建议回顾：

- **u5-l1**：`I18nConfig::parse` 的表头改名与 toml 反序列化细节，理解 CLI 为何能从 `[package.metadata.i18n]` 读到配置。
- **u6 系列**：minify_key 短键算法，理解 `add_translations` 里 `MinifyKey::minify_key` 调用的来龙去脉，以及「键一致性铁律」为何对 CLI 同样适用。
