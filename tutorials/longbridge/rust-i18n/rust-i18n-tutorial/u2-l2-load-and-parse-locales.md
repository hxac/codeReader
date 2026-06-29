# 编译期加载与解析本地化文件

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清楚 `i18n!` 宏在编译期是**怎样把磁盘上的 `locales/*.yml` 文件读进来**的：从 glob 扫描、到扩展名分发、到 serde 解析。
- 解释 `try_load_locales` 如何用 `**/*.{yml,yaml,json,toml}` 模式枚举文件，并用 `ignore_if` 回调筛选。
- 用 `file_stem` + `split('.').last()` 这条规则，**从文件名反推出 locale 名**（包括 `view.en.yml` 这种「模块名.语言」前缀命名）。
- 区分 `parse_file_v1`（整个文件归属单一 locale）与 `parse_file_v2`（locale 由文件内容里的语言子键决定）两条解析分支，并理解 `get_version` 如何根据 `_version` 字段做路由。

本讲是「编译期代码生成主链路」的第二环：上一讲（u2-l1）讲的是 `i18n!` 如何**解析宏参数**，本讲讲的是参数确定后，宏主体如何**把翻译文件真正读进内存**；下一讲（u2-l3）会讲读进来之后如何**合并与扁平化**，u2-l4 讲如何**生成运行时代码**。

## 2. 前置知识

在进入源码前，先建立三个直觉：

1. **这一整套逻辑只在编译期运行一次。** 本讲讨论的所有函数（`try_load_locales`、`parse_file` 等）都带 `#[cfg(feature = "codegen")]` 守卫，意味着它们**不会进用户的运行时二进制**。编译期把翻译数据「算好」，运行时只查表。这正是 rust-i18n 与「运行时读文件」类库（如 gettext）的根本区别。

2. **「文件 → locale → 键值」的三段式。** 一个翻译文件被读进来后，要回答三个问题：这个文件属于**哪个语言**（locale）？文件里的内容是**哪种格式版本**（v1/v2）？内容里的键最终**拍平成什么样子**（点号键）？本讲负责前两个问题，第三个交给 u2-l3。

3. **serde_json::Value 是统一中间表示。** 无论源文件是 YAML、JSON 还是 TOML，解析后都先统一成 `serde_json::Value`（一种带标签的 JSON 树），后续 `get_version`、`merge_value`、`flatten_keys` 都在这棵 `Value` 树上操作。这样就**不用为每种格式写一套合并/扁平化逻辑**。

如果你对 v1/v2 两种文件风格的字面写法还不熟，请先回顾 u1-l4；对 `i18n!` 宏如何拿到 `locales_path` 这个参数，请回顾 u2-l1。

## 3. 本讲源码地图

本讲几乎全部围绕一个文件：

| 文件 | 作用 |
| --- | --- |
| [crates/support/src/lib.rs](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs) | 编译期加载与解析的全部实现：`try_load_locales`（glob 扫描入口）、`parse_file`（按扩展名 + 版本分发）、`parse_file_v1` / `parse_file_v2`（两种风格解析）、`get_version`（版本路由），以及辅助函数 `format_keys`、`merge_value`、`flatten_keys`。 |

补充对照（本讲会引用其测试与示例，但不深入源码）：

- [examples/app/locales/en.yml](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/examples/app/locales/en.yml) — v1 风格的最小示例。
- [examples/app/locales/view.en.yml](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/examples/app/locales/view.en.yml) — 「模块名.locale」前缀命名示例，用来演示 `file_stem` 推导 locale。
- [examples/app-minify-key/locales/v2.yml](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/examples/app-minify-key/locales/v2.yml) — v2 风格（多语言合并一文件）示例。

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块，对应规格要求的 `try_load_locales`、`parse_file`、`parse_file_v1`、`parse_file_v2`、`get_version`。

### 4.1 全景数据流与 `try_load_locales` 入口

#### 4.1.1 概念说明

`i18n!` 宏主体（见 u2-l1）最终会调用 `load_locales`，它是一个**会 panic 的薄封装**：

[crates/support/src/lib.rs:52-61](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L52-L61) — `load_locales` 把「成功返回数据」与「失败 panic 中止编译」两种结果二选一，内部转调 `try_load_locales(..., false)`。

真正干活的是 `try_load_locales`。它的职责是：给定一个 `locales_path` 目录和一个 `ignore_if` 回调，把目录下所有翻译文件读进来，整理成 `BTreeMap<Locale, BTreeMap<键, 值>>` 这种「语言 →（点号键 → 文案）」的两层结构。

> 为什么要有 `try_load_locales` 这个「会返回 Result」的版本，而不是只有 `load_locales`？因为 `load-path` feature 开启时，业务代码需要在**运行时**用 `try_load_locales` 重新加载文件（参见 u5-l3），此时不能再像编译期那样直接 panic，而要把错误优雅地返回给调用者。所以 `report_file_lookup_errors` 参数就是用来切换「严格报错」与「静默返回空」两种策略的。

#### 4.1.2 核心流程

`try_load_locales` 的执行过程可以概括为下面这段伪代码：

```text
fn try_load_locales(locales_path, ignore_if, report_errors) -> Result<Map<Locale, Map<键,值>>>:
    1. 规范化路径 locales_path（normalize），拿不到就按 report_errors 决定返回 Err 或空 Ok
    2. 拼 glob 模式: "{locales_path}/**/*.{yml,yaml,json,toml}"
    3. 若目录不存在 → 同上处理
    4. for 每个 glob 命中的文件 entry:
         a. 若 ignore_if(entry 路径) 为真 → 跳过
         b. locale = 文件名 file_stem 再 split('.').last()
         c. ext  = 文件扩展名
         d. 读文件全部内容到字符串 content
         e. trs = parse_file(content, ext, locale)   # ← 本讲核心，见 4.2
         f. 把 trs（locale -> Value 树）深度合并进 translations
    5. for 每个 (locale, value 树) in translations:
         result[locale] = flatten_keys("", value)    # 把嵌套树拍平成点号键（见 u2-l3）
    6. 返回 result
```

注意步骤 4f 和 5：`parse_file` 返回的是**嵌套的 `Value` 树**（还没拍平），先按 locale 多文件深度合并，**最后才**统一 `flatten_keys` 拍平。合并与扁平化的细节是 u2-l3 的主题，本讲只需知道「它们发生在 `parse_file` 之后」。

#### 4.1.3 源码精读

入口与路径规范化：

[crates/support/src/lib.rs:64-68](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L64-L68) — 函数签名，返回两层 `BTreeMap`；第三个参数 `report_file_lookup_errors` 控制错误是否上抛。

[crates/support/src/lib.rs:72-97](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L72-L97) — 用 `normpath` 规范化 `locales_path`（消除 `..`、`.` 等），失败时按 `report_file_lookup_errors` 决定返回 `Err` 还是空 `Ok`。

拼出 glob 模式并校验目录存在：

[crates/support/src/lib.rs:99-115](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L99-L115) — 模式串 `"{locales_path}/**/*.{yml,yaml,json,toml}"` 表示「该目录任意深度下、扩展名为这四种之一的任意文件」；`**` 是递归通配，`{...}` 是 brace 展开。随后检查目录是否存在，不存在则按策略返回。

glob 遍历与文件筛选的核心循环：

[crates/support/src/lib.rs:117-156](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L117-L156) — 用 `globwalk::glob` 枚举每个匹配文件，逐个处理。

其中几处关键点：

- [crates/support/src/lib.rs:125-127](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L125-L127) — `ignore_if` 回调：调用方传入一个「拿到文件路径字符串、返回是否跳过」的闭包，返回 `true` 就 `continue` 跳过该文件。`i18n!` 宏里默认传 `|_| false`（不跳过任何文件），但提取器等场景会用它来排除自动生成的 `TODO.yml`。
- [crates/support/src/lib.rs:129-133](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L129-L133) — **从文件名推导 locale**：先 `file_stem()` 去掉扩展名，再 `split('.').last()` 取最后一段。详见 4.1 的下文「file_stem 推导 locale」。
- [crates/support/src/lib.rs:135](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L135) — 取扩展名 `ext`，交给 `parse_file` 做格式分发。
- [crates/support/src/lib.rs:146-148](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L146-L148) — 调用 `parse_file(content, ext, locale)` 把文件内容解析成 `Translations`（即 `BTreeMap<Locale, Value>`）。
- [crates/support/src/lib.rs:150-155](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L150-L155) — 把单文件解析结果按 locale **深度合并**（`merge_value`）进汇总表 `translations`，使同名语言的多份文件能叠加。

最后统一拍平：

[crates/support/src/lib.rs:158-160](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L158-L160) — 对每个 locale 调用 `flatten_keys("", trs)` 把嵌套 `Value` 树拍平成 `键 -> 文案`，塞进最终 `result`。

#### file_stem 推导 locale（文件名即语言）

这是 v1 风格下「locale 从哪来」的关键，单独讲清。以 `examples/app/locales/` 下的真实文件为例：

| 文件名 | `file_stem()` 结果 | `split('.').last()` 结果（=locale） |
| --- | --- | --- |
| `en.yml` | `en` | `en` |
| `view.en.yml` | `view.en` | `en` |
| `view.fr.yml` | `view.fr` | `fr` |

也就是说，`split('.').last()` 取的是**最后一段**，于是 `view.en.yml` 这种「模块名.语言.yml」命名也能正确抽出 `en`。这让一个模块可以把自己的翻译拆成多个语言文件，文件名里带模块前缀以便归类，而 locale 推导不受影响。

> 注意：这条 `file_stem + split('.')` 规则**只在 v1 风格生效**。v2 风格下 locale 由文件**内容**里的语言子键决定（见 4.4），文件名被忽略——这也是为什么 v2 的测试用例里 locale 形参传的是 `"filename"` 占位符。

#### 4.1.4 代码实践

**实践目标：** 直观看到 `try_load_locales` 实际枚举了哪些文件、推导出哪些 locale。

**操作步骤：**

1. 在项目根目录设置环境变量 `RUST_I18N_DEBUG=1`（由 [crates/support/src/lib.rs:18-20](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L18-L20) 的 `is_debug()` 识别）。
2. 编译 `examples/app`：
   ```bash
   RUST_I18N_DEBUG=1 cargo build -p app --example app
   ```
   > 待本地验证：该 example 的确切构建命令以本地 `examples/app/Cargo.toml` 为准，必要时改用 `cargo build --manifest-path examples/app/Cargo.toml`。

**需要观察的现象：** 编译输出中会出现形如 `cargo:i18n-locale=...locales/**/*.yml,yaml,json,toml` 的行，以及每个被加载文件一行 `cargo:i18n-load=.../en.yml`、`cargo:i18n-load=.../view.en.yml` 等（对应 [crates/support/src/lib.rs:101-103](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L101-L103) 与 [crates/support/src/lib.rs:121-123](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L121-L123)）。

**预期结果：** 你能从 `cargo:i18n-load=` 行里数出 `en.yml`、`fr.yml`、`view.en.yml`、`view.fr.yml` 四个文件，验证 `view.en.yml` 也会被加载（brace 展开命中），并理解它们最后都被归类进 `en` / `fr` 两个 locale。

#### 4.1.5 小练习与答案

**练习 1：** 如果有人在 `locales/` 下放了 `readme.md`，会被 `try_load_locales` 加载吗？为什么？

**答案：** 不会。glob 模式是 `**/*.{yml,yaml,json,toml}`，`md` 不在扩展名白名单里，直接不会被枚举进来。

**练习 2：** `ignore_if` 回调默认在 `i18n!` 宏里被设成什么？它的设计目的是什么？

**答案：** 默认 `|_| false`（不跳过任何文件）。它存在的目的是让调用方（如 `cargo i18n` 提取器在重新生成翻译时）能跳过自己产出的 `TODO.yml` 之类文件，避免把生成物当成源翻译再次读入。

---

### 4.2 `parse_file` 分发与 `get_version` 路由

#### 4.2.1 概念说明

`try_load_locales` 拿到文件内容字符串后，交给 `parse_file`。这个函数只做两件事：

1. **按扩展名选 serde 解析器**，把文本解析成统一的 `serde_json::Value`；
2. **读 `_version` 字段**，决定走 v1 还是 v2 的后续处理。

它本身**不关心文件名**（locale 由调用方在 v1 时传入，v2 时根本不用），只关心「内容 + 扩展名 + 版本」。这种「先归一成 Value，再按版本分支」的设计，把「格式（yml/json/toml）」与「风格（v1/v2）」两个维度解耦了。

#### 4.2.2 核心流程

```text
fn parse_file(content, ext, locale) -> Result<Translations, String>:
    1. result = match ext:
         "yml"|"yaml" -> serde_yaml::from_str::<Value>(content)
         "json"       -> serde_json::from_str::<Value>(content)
         "toml"       -> toml::from_str::<Value>(content)
         其它          -> Err("Invalid file extension")
    2. match get_version(&value):
         2  -> parse_file_v2("", &value)   // locale 由内容决定
         _  -> parse_file_v1(locale, &value) // locale 由文件名传入
```

注意 `_ =>` 这条**默认分支**：只要 `get_version` 返回的不是 `2`（包括 `1`、缺失、甚至非数字），一律走 v1。这与 u1-l4 讲过的「`get_version` 用 `unwrap_or(1)`，只有显式数字 `2` 才走 v2」完全对应。

#### 4.2.3 源码精读

扩展名分发：

[crates/support/src/lib.rs:165-175](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L165-L175) — `match ext` 把四种扩展名分别交给 `serde_yaml` / `serde_json` / `toml` 三个解析器，且都反序列化成**同一个** `serde_json::Value` 类型；未知扩展名直接报错。

版本路由：

[crates/support/src/lib.rs:177-189](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L177-L189) — 解析成功后调用 `get_version` 判断版本；`2` 走 `parse_file_v2`（注意它**不接收** `locale` 形参），其余走 `parse_file_v1(locale, ...)`。

`get_version` 本体非常短：

[crates/support/src/lib.rs:238-245](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L238-L245) — 读取顶层 `_version` 字段；存在则取其 `u64` 值，**否则默认 `1`**（`unwrap_or(1)`）。这就是「不写 `_version` 等于 v1」的根因。

#### 4.2.4 代码实践

**实践目标：** 用项目自带的单元测试，验证 `parse_file` 的扩展名分发与版本路由行为。

**操作步骤：**

1. 阅读内联测试 [crates/support/src/lib.rs:316-330](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L316-L330)（`test_parse_file_in_yaml`）和 [crates/support/src/lib.rs:356-367](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L356-L367)（`test_get_version`）。
2. 运行 support crate 的单元测试：
   ```bash
   cargo test -p rust-i18n-support --features codegen
   ```

**需要观察的现象：** `test_parse_file_in_yaml` 里有一行 `parse_file(content, "foo", "en").expect_err("Should error");`——它断言传 `"foo"` 扩展名会**报错**。

**预期结果：** 测试通过，说明未知扩展名会被 `match ext` 的 `_ => Err(...)` 分支拦下；`test_get_version` 通过，说明缺省 `_version` 返回 `1`。

#### 4.2.5 小练习与答案

**练习 1：** 一个 JSON 文件内容是 `{"_version": "2", ...}`（注意 `2` 是**字符串**），会走哪条分支？

**答案：** 走 v1。因为 `get_version` 用 `as_u64()` 解析，字符串 `"2"` 无法转成 `u64`，`unwrap_or(1)` 兜底返回 `1`。只有**数字** `2`（`_version: 2`）才会走 v2。

**练习 2：** 为什么 `parse_file` 对 yml/yaml/json/toml 都反序列化成 `serde_json::Value` 而不是各自的原生类型？

**答案：** 为了得到**统一的中间表示**，让后续的 `get_version`、`merge_value`、`flatten_keys` 只需写一套基于 `serde_json::Value` 的逻辑，不必为每种格式重复实现。

---

### 4.3 `parse_file_v1`：整文件归属单一 locale

#### 4.3.1 概念说明

v1 风格的核心规则极简：**一个文件 = 一个 locale**。文件被解析成 `Value` 树后，直接整体挂到「由文件名推导出的那个 locale」名下，不做任何按语言重组。这也是为什么 v1 必须靠文件名（`file_stem`）来定 locale——内容里根本没有任何 locale 标识。

#### 4.3.2 核心流程

```text
fn parse_file_v1(locale, data) -> Translations:
    return { locale -> data.clone() }   // 整棵树原样挂到这一个 locale
```

就这一行。真正的「按 locale 把多个文件合并」是在 `try_load_locales` 的循环里，用 `merge_value` 把多个 v1 文件（同名 locale）叠到一起完成的，而非在 `parse_file_v1` 内部。

#### 4.3.3 源码精读

[crates/support/src/lib.rs:192-195](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L192-L195) — `parse_file_v1` 用 `Translations::from([(locale.to_string(), data.clone())])` 构造一个只含一个条目的 map，键是 locale、值是整棵解析树。

对照真实 v1 文件 [examples/app/locales/en.yml](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/examples/app/locales/en.yml)，其内容是 `hello: Hello, %{name}!`。经过 `parse_file_v1` 后，得到 `{"en": {"hello": "Hello, %{name}!"}}`——`en` 来自文件名，内容原样挂上。

#### 4.3.4 代码实践

**实践目标：** 验证 v1 文件「整树挂单 locale」的语义。

**操作步骤：** 阅读测试 [crates/support/src/lib.rs:316-330](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L316-L330)。其中关键断言：

```rust
let content = "foo: Foo\nbar: Bar";
let trs = parse_file(content, "yml", "en").expect("Should ok");
assert_eq!(trs["en"]["foo"], "Foo");
trs = parse_file(content, "yml", "zh-CN").expect("Should ok");
assert_eq!(trs["zh-CN"]["foo"], "Foo");
```

**需要观察的现象：** 同一段内容 `foo: Foo\nbar: Bar`，分别传 locale `"en"` 与 `"zh-CN"`，结果分别挂在 `trs["en"]` 和 `trs["zh-CN"]` 下——**内容完全相同，只是 locale 标签不同**。

**预期结果：** 这恰好印证「v1 的 locale 完全由调用方（文件名）传入，与文件内容无关」。

#### 4.3.5 小练习与答案

**练习：** 假设 `locales/` 下同时有 `en.yml` 和 `view.en.yml`，两个文件里都有顶层 `hello` 键，最终 `en` locale 下的 `hello` 会是什么？

**答案：** 取决于 glob 枚举顺序与 `merge_value` 的合并语义——后者覆盖前者同名键。两个文件都被推导为 locale `en`，在 `try_load_locales` 的 [crates/support/src/lib.rs:150-155](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L150-L155) 处用 `merge_value` 深度合并，后处理的文件会覆盖先处理的同名叶子键。（合并细节见 u2-l3。）

---

### 4.4 `parse_file_v2`：按 locale 键重组嵌套结构

#### 4.4.1 概念说明

v2 风格把**多种语言合并进同一个文件**，结构是「顶层键 → { 语言: 文案 }」。因此 v2 不能再用文件名定 locale——locale 来自**内容里的语言子键**（如 `en`、`zh-CN`）。`parse_file_v2` 的工作就是遍历这棵「键 → {locale: 文案}」的树，**按 locale 把文案重新分组**，输出和 v1 同样的 `Translations` 结构（locale → Value 树）。

另一个 v2 特性是**点号键名**：YAML 里可以直接写 `welcome.sub:` 这样的键名，`parse_file_v2` 用 `format_keys` 把多级键前缀拼起来，生成 `welcome.sub` 这样的点号键，省去嵌套。

#### 4.4.2 核心流程

以测试用例（[crates/support/src/lib.rs:387-407](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L387-L407)）为例，输入：

```yaml
_version: 2
welcome:
    en: Welcome
    zh-CN: 欢迎
welcome.sub:
    en: Welcome 1
    zh-CN: 欢迎 1
```

处理流程（伪代码）：

```text
fn parse_file_v2(key_prefix, data):
    for (key, value) in data 顶层对象:        # key = "welcome" 或 "welcome.sub"
        value 必须是对象 sub_messages = {locale: text}
        for (locale, text) in sub_messages:    # locale = "en" / "zh-CN"
            若 text 是字符串:
                full_key = format_keys([key_prefix, key])   # 拼点号键
                trs[locale][full_key] = text                # 按 locale 分组写入
    返回 trs
```

最终输出（以 `en` 为例）：`{"en": {"welcome": "Welcome", "welcome.sub": "Welcome 1"}}`——和 v1 输出结构一致，殊途同归。

`format_keys` 的作用就是把多段非空键用 `.` 连起来：

[crates/support/src/lib.rs:247-254](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L247-L254) — 过滤掉空字符串段后用 `.` 拼接。首层调用 `format_keys(["", "welcome"])` 时，空前缀被过滤，结果就是 `welcome`。

#### 4.4.3 源码精读

[crates/support/src/lib.rs:197-236](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L197-L236) — `parse_file_v2` 主体。逐段说明：

- 它要求 `data` 是 `Value::Object`（[L201](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L201)），且每个顶层 `value` 也是对象（[L203](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L203)）——即「键 → {locale: 文案}」结构。
- [L205-214](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L205-L214)：当某 locale 下的 `text` 是字符串时，用 `format_keys` 拼出完整键，构造 `sub_trs = {full_key: text}`，再用 `merge_value` 并入 `trs[locale]`。这一步把「按 key 组织」的输入翻转成「按 locale 组织」的输出。
- [L216-225](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L216-L225)：当 `text` 是对象时，`parse_file_v2` 会**递归**，以扩展后的键前缀重新处理整个 `value` 对象，用于支持更深的嵌套分组。
- [L231-235](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L231-L235)：若解析后 `trs` 为空（结构不符合 v2 预期），返回 `None`，调用方 `parse_file` 据此报「Invalid locale file format」错误（见 [L184](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L184)）。

注意 v2 的测试里 locale 形参传的是 `"filename"` 占位符（[L400](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L400)），但断言结果落在 `trs["en"]`、`trs["zh-CN"]`——这正证明 v2 **不使用文件名 locale**，locale 完全来自内容。

#### 4.4.4 代码实践

**实践目标：** 对照真实 v2 文件，理解「键 → {locale: 文案}」如何被翻转成「locale → {键: 文案}」。

**操作步骤：**

1. 打开 [examples/app-minify-key/locales/v2.yml](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/examples/app-minify-key/locales/v2.yml)。它的顶层 `_version: 2`，下面是若干「短键 → {en/de/fr/...: 文案}」条目。
2. 阅读测试 [crates/support/src/lib.rs:369-384](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L369-L384)（`test_parse_file_in_json_with_nested_locale_texts`），手动跟踪这个 JSON：

   ```json
   { "_version": 2,
     "welcome": { "en": "Welcome", "zh-CN": "欢迎", "zh-HK": "歡迎" } }
   ```

**需要观察的现象：** `parse_file` 收到这个内容、扩展名 `json`、locale 形参 `"filename"`，但返回的 `trs` 里键是 `en` / `zh-CN` / `zh-HK`。

**预期结果：** 断言 `trs["en"]["welcome"] == "Welcome"`、`trs["zh-CN"]["welcome"] == "欢迎"`、`trs["zh-HK"]["welcome"] == "歡迎"` 全部成立，印证 locale 来自内容、文件名被忽略。

#### 4.4.5 小练习与答案

**练习 1：** 一个文件写了 `_version: 2`，但顶层某个键的值不是对象而是普通字符串（如 `foo: bar`），会发生什么？

**答案：** 该条目被跳过。`parse_file_v2` 在 [L203](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L203) 用 `if let Value::Object(sub_messages) = value` 守卫，非对象的 `value` 不进入内层循环。若整个文件没有任何合法条目，`trs` 为空，返回 `None`，`parse_file` 进而报「Invalid locale file format」。

**练习 2：** v1 和 v2 解析后输出的 `Translations` 结构是否一致？为什么要让它们一致？

**答案：** 一致，都是 `BTreeMap<Locale, Value>`。让它们一致，是为了让 `try_load_locales` 后续的合并（`merge_value`）与扁平化（`flatten_keys`）对两种风格走**同一套代码**，`t!` 查表时也无需区分来源风格。

---

## 5. 综合实践

**任务：** 完整跟踪一个 `en.yml` 文件从被 glob 命中到进入最终 `result` 的全过程，并把每一步对应的源码行号标注出来。

请按以下步骤完成（不修改任何源码，只阅读与记录）：

1. **准备文件。** 在 `examples/app/locales/` 下确认存在 `en.yml`（内容见 [examples/app/locales/en.yml](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/examples/app/locales/en.yml)，即 `hello: Hello, %{name}!`）。它没有 `_version` 字段，因此是 **v1** 风格。

2. **建立跟踪表。** 复制下表，逐行填入「发生什么」和「对应源码行号」：

   | 步骤 | 发生什么 | 源码位置 |
   | --- | --- | --- |
   | ① glob 命中 | 模式 `{path}/**/*.{yml,...}` 枚举到 `en.yml` | [L99](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L99)、[L117-119](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L117-L119) |
   | ② ignore_if | 默认 `|_| false`，不跳过 | [L125-127](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L125-L127) |
   | ③ 推导 locale | `file_stem()` = `en`，`split('.').last()` = `en` | [L129-133](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L129-L133) |
   | ④ 取扩展名 | `ext` = `yml` | [L135](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L135) |
   | ⑤ 读内容 | 读成字符串 | _自行填写_ |
   | ⑥ parse_file 分发 | `match ext` → `serde_yaml::from_str` | _自行填写_ |
   | ⑦ get_version 路由 | 无 `_version` → 返回 `1` → 走 v1 | _自行填写_ |
   | ⑧ parse_file_v1 | 整树挂到 locale `en` | _自行填写_ |
   | ⑨ 合并 + 拍平 | `merge_value` 后 `flatten_keys` | _自行填写_ |

3. **回答两个关键问题（写入你的跟踪笔记）：**
   - `file_stem` 如何决定 locale 名？（答：见 4.1 的「file_stem 推导 locale」小节。）
   - `_version` 如何决定走哪条分支？（答：见 4.2 的 `get_version`，缺省 `1` → v1，显式数字 `2` → v2。）

4. **进阶验证（可选）。** 把同一个 `hello` 键同时写进 `en.yml` 和 `view.en.yml`（两个文件都推导为 locale `en`），开 `RUST_I18N_DEBUG=1` 重新编译 `examples/app`，观察两份文件都被 `cargo:i18n-load=` 列出，并思考它们如何在步骤 ⑨ 被 `merge_value` 合并（合并细节留给 u2-l3 深入）。

> 本任务为「源码阅读型实践」：不需要运行新命令验证输出，重点是能把磁盘文件到内存数据结构的每一跳都对上源码行号。如需运行，`RUST_I18N_DEBUG=1` 的具体构建命令请以本地 `examples/app/Cargo.toml` 为准。

## 6. 本讲小结

- 编译期加载入口是 `try_load_locales`，`load_locales` 只是它「失败即 panic」的薄封装；`report_file_lookup_errors` 参数让同一套代码既能用于编译期（panic），也能用于运行时（`load-path` feature 下优雅返回错误）。
- glob 模式 `**/*.{yml,yaml,json,toml}` 决定**哪些文件**被加载，`ignore_if` 回调决定**跳过哪些**（默认不跳过）。
- **v1 的 locale 来自文件名**：`file_stem()` 再 `split('.').last()`，支持 `view.en.yml` 这种「模块名.语言」前缀命名。
- `parse_file` 先按扩展名用对应 serde 解析器归一成 `serde_json::Value`，再用 `get_version`（缺省 `1`、仅数字 `2` 走 v2）做版本路由。
- `parse_file_v1` 把整棵树原样挂到单一 locale；`parse_file_v2` 把「键 → {locale: 文案}」翻转成「locale → {键: 文案}」，locale 来自内容而非文件名，并用 `format_keys` 支持点号键名。
- 两种风格解析后都产出相同的 `Translations` 结构，使后续合并与扁平化对两者走同一套代码。

## 7. 下一步学习建议

本讲到 `parse_file` 返回 `Translations` 为止。接下来：

- **u2-l3 多文件合并与键扁平化**：深入 `merge_value`（同名 locale 多文件深度合并）、`flatten_keys`（嵌套 `Value` 树拍平成 `a.b.c` 点号键）、`format_keys`（多级键前缀拼接），把本讲结尾留下的「步骤 ⑨」彻底讲清。
- **u2-l4 generate_code 生成的运行时代码**：看 `try_load_locales` 返回的两层 `BTreeMap` 最终如何被 `quote!` 成静态 `SimpleBackend` 与 `_rust_i18n_translate` 查找函数，闭合整条编译期主链路。
- 若想提前看运行时如何用这套加载能力，可跳到 **u5-l3 Feature flags** 了解 `load-path` feature 下 `try_load_locales` 在运行时被业务代码直接调用的场景。
