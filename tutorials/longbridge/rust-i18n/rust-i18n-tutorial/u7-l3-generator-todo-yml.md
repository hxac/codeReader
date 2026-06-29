# 生成 TODO.yml 与合并去重

## 1. 本讲目标

本讲是「cargo i18n CLI 提取器」单元的收尾篇。在 u7-l1 里我们看清了命令行入口 `main` 如何把流程串起来，在 u7-l2 里我们跟着 `iter_crate` / `extract` 把源码里的 `t!` 调用提取成了一个 `Results = HashMap<查找键, Message>`。本讲要回答最后两个问题：

1. 提取出来的这些 `Message`，**怎么和已有的翻译文件对比、把没翻译的部分写成一个新的 `TODO.yml`**？
2. 为什么 `cargo i18n` 在「还有未翻译文案」时进程会以**非零退出码**结束——这是 bug 还是设计？

学完本讲你应该能够：

- 说清 `generate` 这个总入口的四步编排：去重 → 判空 → 序列化 → 落盘，以及最后一步故意返回 `Err` 的用意。
- 复述 `generate_result` 如何用「查找键是否已存在于某 locale 的翻译表」来判断某条文案「已经翻译过而跳过」。
- 解释 `ignore_file` 回调为什么要跳过 `TODO.yml` 自身，否则会产生什么反馈循环。
- 看懂 `convert_text` 如何把结果序列化成 `_version: 2` 的多语言嵌套格式（YAML / JSON / TOML 三选一）。
- 独立阅读并运行 `generator.rs` 内置的单元测试 `test_convert_text`。

## 2. 前置知识

本讲默认你已经掌握 u7-l1、u7-l2 的内容。这里复习三个关键概念，它们在本讲会反复出现：

- **查找键（lookup key） vs 译文内容**：这是贯穿整个提取器的核心约定。`Results` 这个 `HashMap` 的 **key 是查找键**（关闭 minify_key 时就是 `t!` 的首参字面量；开启 minify_key 时是哈希后的短键），而 `Message.key` 字段存的是**译文内容**（即原文案本身）。详见 u7-l2、u6-l3 的「键一致性铁律」。
- **`_version: 2` 多语言格式**：详见 u1-l4。一个文件里同时存放多种语言，结构是「消息键 → { locale → 译文 }」的嵌套 map。本讲生成的 `TODO.yml` 正是这种格式。
- **`load_locales(locales_path, ignore_if)`**：support crate 提供的加载函数，返回 `BTreeMap<locale, BTreeMap<查找键, 译文>>`。`ignore_if` 是个回调，对每个待加载文件返回 `true` 表示**跳过**该文件。这正是 generator 用来「跳过自己上一次输出」的钩子。

另外需要一点 Rust 基础：`std::io::Result<T>`、`HashMap` 的 `entry().or_default()` 写法、`serde_json::Value` 的动态对象构造，以及 `match` 分发。

## 3. 本讲源码地图

本讲几乎全部内容集中在一个文件里：

| 文件 | 作用 |
| --- | --- |
| [crates/extract/src/generator.rs](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/extract/src/generator.rs) | 本讲主角。四个函数 `generate` / `generate_result` / `convert_text` / `write_file` 全在这里，外加一个内联单元测试。 |
| [crates/extract/src/extractor.rs](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/extract/src/extractor.rs) | 提供 `Message` 结构体定义（被 generator 引用）。 |
| [crates/cli/src/main.rs](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/cli/src/main.rs) | 调用 `generator::generate` 的上游，决定「出错就 `exit(1)`」。 |
| [crates/support/src/lib.rs](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs) | 提供 `load_locales` 及 `ignore_if` 的真实语义。 |

## 4. 核心概念与源码讲解

整个 generator 的数据流是一条直线：

```
messages (HashMap<查找键, Message>)
        │
        ▼
generate_result ──► 对比已有翻译 ──► trs (未翻译子集: HashMap<查找键, HashMap<locale, 占位值>>)
        │
        ▼
   trs 为空? ──是──► Ok(())           （「All thing done.」，退出码 0）
        │ 否
        ▼
   convert_text ──► _version:2 文本
        │
        ▼
   write_file ──► 落盘 TODO.yml
        │
        ▼
   故意返回 Err  ──► main 里 exit(1)   （让 CI 失败）
```

下面按四个最小模块依次拆开。

### 4.1 `generate`：编排入口与「让 CI 失败」的设计

#### 4.1.1 概念说明

`generate` 是 generator 对外暴露的唯一入口，它本身不做具体活，只负责**编排**：把去重、判空、序列化、落盘四步按顺序串起来。它最「反直觉」的设计在最后——当确实有未翻译文案、并且已经成功写完 `TODO.yml` 之后，它**故意返回一个 `io::Error`**。

这不是 bug。它的目的是让 `cargo i18n` 在 CI 里以**非零退出码**结束，从而让持续集成流水线「红」。换句话说：只要你的代码里还有没翻译完的文案，`cargo i18n` 就会让 CI 失败，强迫团队把翻译补齐。这是一种用进程退出码充当「翻译完整性闸门」的工程实践。

#### 4.1.2 核心流程

伪代码：

```text
fn generate(output_path, all_locales, messages):
    filename = "TODO.yml"
    format   = "yaml"

    trs = generate_result(output_path, filename, all_locales, messages)  # 去重

    if trs 为空:
        打印 "All thing done."
        return Ok(())                  # 退出码 0

    打印 "Found N new texts need to translate."
    text = convert_text(trs, format)   # 序列化成 _version:2
    write_file(output_path, filename, text)  # 落盘

    return Err(io::Error::new(Other, ""))    # 故意失败 → CI 红
```

注意三个细节：

1. **输出文件名和格式是硬编码的**：`filename = "TODO.yml"`、`format = "yaml"`。所以无论你配了多少种语言，最终只生成**一个** `TODO.yml`（用 `_version: 2` 把所有语言装在一起）。> 注：README 的 Extractor 章节里展示的 `TODO.en.yml` / `TODO.fr.yml` 按 locale 拆分多个文件、以及 `--locale` 选项，是**旧版行为**，与当前源码不符；以源码为准。
2. **`trs.is_empty()` 是「全部翻译完毕」的唯一判据**。只要去重后还有任何 (查找键, locale) 组合没翻译，就走写盘 + 报错分支。
3. **报错在写盘之后**：先保证 `TODO.yml` 已经写好（供译者参考），再返回错误。

#### 4.1.3 源码精读

入口与四步编排，参见 [crates/extract/src/generator.rs:L10-L36](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/extract/src/generator.rs#L10-L36)。其中「空集合直接 Ok」的分支在 L20-L24；写盘后故意构造错误的「让 CI fail」逻辑在 L33-L35，注释原话就是 `// Finally, return error for let CI fail`。

而真正消费这个错误的，是上游 CLI 的 `main`：它把 `generate` 的 `Result` 转成 `has_error`，再 `exit(1)`，见 [crates/cli/src/main.rs:L119-L130](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/cli/src/main.rs#L119-L130)。所以「翻译没补全 → CI 红」这条链是 generator 的 `Err` 与 main 的 `exit(1)` **两端配合**完成的。

#### 4.1.4 代码实践

**实践目标**：亲眼看一次「有未翻译 → 退出码 1」和「全翻译完 → 退出码 0」的差别。

**操作步骤**（源码阅读型 + 本地验证）：

1. 在 `crates/cli/src/main.rs:L123` 处确认 `generate` 的返回值被赋给 `result`，`is_err()` 置位 `has_error`。
2. （本地验证，需要先 `cargo install --path crates/cli` 装出 `cargo-i18n` 二进制）在一个含 `t!("hello.world")` 但 `locales/en.yml` 里没有 `hello.world` 键的小项目里运行 `cargo i18n`，然后立刻执行 `echo $?` 查看退出码。
3. 把生成的 `locales/TODO.yml` 内容并回 `locales/en.yml`（补上真实译文），删除 `TODO.yml`，再次 `cargo i18n`，看到 `All thing done.` 后再 `echo $?`。

**需要观察的现象**：第一次退出码应为 `1`，且 stderr 打印 `Found N new texts need to translate.`；第二次退出码应为 `0`。

**预期结果**：退出码分别为 1 与 0。> 若本地未安装 `cargo-i18n`，则跳过运行，仅完成步骤 1 的源码阅读，并标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：如果把 L33-L35 的 `return Err(...)` 改成 `return Ok(())`，CI 会发生什么变化？

**参考答案**：`cargo i18n` 永远以退出码 0 结束，CI 不再因为「有未翻译文案」而失败。`TODO.yml` 仍会生成，但「翻译完整性闸门」失效——团队可能长期遗留未翻译文案而无感知。这正是该错误返回存在的意义。

**练习 2**：为什么写盘（`write_file`）要放在返回 `Err` 之前，而不是之后？

**参考答案**：因为函数一旦 `return Err`，后续语句不再执行。若先返回错误再写盘，`TODO.yml` 根本不会被写出来，译者就失去了「该翻译哪些文案」的依据。当前顺序保证「先留下待办清单，再让 CI 红」。

---

### 4.2 `generate_result`：对比已有翻译做去重

#### 4.2.1 概念说明

这是整个 generator 的**大脑**。它接收上一步提取到的全部 `messages`，逐一与「磁盘上已有的翻译」对比，只把**还没翻译**的部分挑出来，返回一个 `Translations`：

```rust
type Translations = HashMap<String, HashMap<String, String>>;
//                       ^^^^^^    ^^^^^^^^^^^^^^^^^^
//                       查找键     locale → 占位译文
```

这里有两个关键设计：

- **「已翻译」的判定标准非常具体**：某条文案是否算「已翻译」，是**按 locale 分别判断**的。同一条文案可能在 `en` 里已经翻译了，但在 `zh-CN` 里还没有——那么 `en` 会被跳过，`zh-CN` 会被写进 `TODO.yml`。
- **跳过自己上一次的输出**：它通过 `ignore_file` 回调让 `load_locales` 跳过 `TODO.yml` 自身。否则会产生反馈循环——见 4.2.3。

#### 4.2.2 核心流程

「跳过（已翻译）」的判定可以用一个谓词表达。设 `data` 为加载到的已有翻译（`data[locale]` 是该 locale 的「查找键 → 译文」表），\(k\) 是当前消息的查找键，\(\ell\) 是当前 locale，则：

\[
\text{skip}(k,\ell) \iff \text{data}[\ell] \ni k
\]

即「该 locale 的翻译表里已经有这个查找键，就跳过」。写成代码就是两层 `get`。

而写进 `TODO.yml` 的**占位值**（给译者看的初值）也有两条分支。设 \(m.\text{key}\) 是 `Message.key`（译文内容/原文案），\(\text{lastSeg}(s)\) 表示取 \(s\) 按 `.` 切分后的最后一段：

\[
\text{placeholder}(m) = \begin{cases} m.\text{key} & \text{若 } m.\text{minify\_key} = \text{true} \\ \text{lastSeg}(m.\text{key}) & \text{否则} \end{cases}
\]

整体流程伪代码：

```text
fn generate_result(output_path, output_filename, all_locales, messages):
    trs = {}
    for locale in all_locales:
        ignore_file = |fname| fname.ends_with(output_filename)   # 跳过 TODO.yml
        data = load_locales(output_path, ignore_file)            # 重新加载已有翻译

        for (key, m) in messages:
            if data[locale] 含有 key:     # 已翻译
                continue                  # 跳过
            value = m.minify_key ? m.key : m.key 按 '.' 切的最后一段
            trs[key][locale] = value      # 记为待翻译
    return trs
```

> 小观察：`data = load_locales(...)` 放在 `for locale` 循环**内部**（[L77](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/extract/src/generator.rs#L77)），但 `load_locales` 一次性返回**全部** locale 的表。所以每个 locale 都重复加载了一遍整张表——这是历史遗留（来自按 locale 拆分输出的旧版本），功能上无害，只是多了几次磁盘读取。

#### 4.2.3 源码精读

去重判定本体在 [crates/extract/src/generator.rs:L88-L92](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/extract/src/generator.rs#L88-L92)：`data.get(locale)` 拿到该 locale 的「查找键→译文」表，再用 `trs.get(key).is_some()`（注意这里的 `trs` 是内层 `Some(trs)` 绑定的局部变量，遮蔽了外层，实为该 locale 的键表）判断查找键是否已存在，存在则 `continue`。占位值的两条分支在 [L94-L98](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/extract/src/generator.rs#L94-L98)。

跳过自身输出的回调在 [L76](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/extract/src/generator.rs#L76)：`let ignore_file = |fname: &str| fname.ends_with(&output_filename);`。`ignore_if` 返回 `true` 即跳过该文件，这一点在 support 侧 [crates/support/src/lib.rs:L125-L127](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L125-L127) 由 `if ignore_if(...) { continue; }` 落实。

**为什么要跳过 `TODO.yml`？** 想象没有这个跳过的后果：第一次运行写出 `TODO.yml`，里面每条文案都带了一个「占位译文」（比如 `title`）。第二次运行时，`load_locales` 会把 `TODO.yml` 也当作合法翻译读进来，于是这些占位值会被当成「已经翻译好」——`generate_result` 认为所有键都已存在，直接返回空 `trs`，打印 `All thing done.`。结果就是：**只要跑过一次，以后永远提示全部完成**，真正的译文根本没补，反馈循环把闸门永久卡死。跳过 `TODO.yml` 正是为了切断这个循环。

另外，L80-L86 有一段关于 `m.locations` 的空循环和 TODO 注释：作者本想把「文件名 + 行号」作为 YAML 注释写进去，但 `serde_yaml` 不支持写注释（参见引用的 dtolnay/serde-yaml#145），所以目前只保留位置信息在 `Message` 里、并未真正落盘。阅读时知道这段是「预留未实现」即可。

#### 4.2.4 代码实践

**实践目标**：亲手验证「查找键已存在于某 locale 翻译表 → 跳过」这条判定，并解释 `generate_result` 如何判断某 key 已翻译过。

**操作步骤**（源码阅读型，可手算）：

1. 准备一组 `messages`（查找键 → `Message`），假设关闭 minify_key：

   | 查找键 \(k\) | `Message.key`（原文案） |
   | --- | --- |
   | `"hello"` | `"hello"` |
   | `"views.title"` | `"views.title"` |

2. 假设磁盘上 `locales/en.yml`（v1，文件名推导出 locale = `en`）已有内容：

   ```yaml
   hello: "你好"
   ```

3. 手算：对 locale = `en`，`data["en"] = { "hello" → "你好" }`。对 `messages` 里每条：
   - `key = "hello"`：`data["en"]` 含 `"hello"` → **跳过**。
   - `key = "views.title"`：`data["en"]` 不含 → 写入。占位值 = `"views.title".split('.').last()` = `"title"`。
4. 预测 `trs` = `{ "views.title" → { "en" → "title" } }`，`hello` 不在其中。

**需要观察的现象 / 预期结果**：只有 `views.title` 出现在待翻译集合里；`hello` 因已存在被跳过。占位值是 `title`（点号最后一段），不是整句。

> 想运行验证，可在 `crates/extract` 下仿照内联测试写一个调用 `generate_result`（传入临时目录与一个最小 `en.yml`）的 `#[test]`，用 `cargo test -p rust-i18n-extract` 运行；若不便构造磁盘文件，标注「待本地验证」并保留手算结论。

#### 4.2.5 小练习与答案

**练习 1**：假设 `available_locales = ["en", "zh-CN"]`，`hello` 在 `en.yml` 里有译文但 `zh-CN.yml` 里没有。`generate_result` 返回的 `trs` 里 `hello` 这一项长什么样？

**参考答案**：`trs["hello"] = { "zh-CN" → <占位值> }`，**不包含 `en`**。因为去重是按 locale 分别判断的——`en` 已翻译被跳过，`zh-CN` 未翻译被收录。最终 `convert_text` 会把 `{ "zh-CN": ... }` 写进 `TODO.yml` 的 `hello:` 下面。

**练习 2**：若打开 minify_key，占位值用的是 `m.key`（原文案整句）而不是 `split('.').last()`，为什么？

**参考答案**：开启 minify_key 时，`Message.key` 存的是**原文案**（如 `"Hello world"`），而查找键是哈希短键。此时 `split('.').last()` 对原文案没有意义（文案里通常没有点号结构），直接用整句原文案当占位值更直观，方便译者照着原文翻译。关闭 minify_key 时，查找键本身常是 `"a.b.c"` 这种点号键，取最后一段 `"c"` 作为占位值是一种「推测性默认值」。

---

### 4.3 `convert_text`：序列化成 `_version:2` 多语言格式

#### 4.3.1 概念说明

`convert_text` 负责把 `Translations`（`HashMap<查找键, HashMap<locale, 占位值>>`）序列化成文本。它统一以 `serde_json::Value` 作为**中间表示**来构造目标结构，再按 `format` 选不同的序列化器输出。

目标结构正是 u1-l4 讲过的 `_version: 2` 嵌套 locale 格式：

```yaml
_version: 2
hello:        # 查找键
  en: Hello   # locale → 译文
  zh: 你好
```

#### 4.3.2 核心流程

```text
fn convert_text(trs, format):
    value = JSON 对象 {}
    value["_version"] = 2                      # 顶层固定写 _version: 2
    for (key, locale_map) in trs:
        obj = {}
        for (locale, text) in locale_map:
            obj[locale] = text                 # 嵌套 { locale: text }
        value[key] = obj                       # 查找键 → 嵌套对象

    match format:
        "json"  => serde_json::to_string_pretty(value)
        "yaml"  => serde_yaml::to_string(value)，再去掉开头的 "---"
        "toml"  => toml::to_string_pretty(value)
```

两个要点：

- **`_version` 永远是数字 `2`**（`Number::from(2)`），对应 u1-l4 里「只有数字 2 才走 v2 解析」的规则。生成端和解析端用的是同一套版本约定。
- **YAML 要额外去掉开头的 `---`**：`serde_yaml::to_string` 会自动加 YAML 文档起始符 `---`，而 rust-i18n 的 locale 文件约定不写这个头，所以用 `trim_start_matches("---").trim_start()` 清掉。

#### 4.3.3 源码精读

完整实现见 [crates/extract/src/generator.rs:L38-L60](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/extract/src/generator.rs#L38-L60)。L39-L40 构造空对象并写入 `_version: 2`；L42-L48 双层循环把每个查找键展开成 `{ locale: text }` 嵌套对象；L50-L59 的 `match` 按格式分发，`_ => unreachable!()` 因为 `format` 由 `generate` 硬编码为 `"yaml"`，外部无法传入别的值。

> 关于「为什么用 `serde_json::Value` 当中间表示」：因为 `serde_json::Value` 是动态类型容器，可以随手构造任意嵌套对象，再被 `serde_yaml` / `toml` 反向序列化。这比给三种格式各写一套「先构造具体结构体再 derive Serialize」要省事得多——一份中间结构，三种输出。这也是 extract crate 在 [crates/extract/Cargo.toml](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/extract/Cargo.toml) 里同时依赖 `serde_json`、`serde_yaml`、`toml` 三个序列化器的原因。

#### 4.3.4 代码实践

**实践目标**：直接运行内置的 `test_convert_text`，观察同一份数据在三种格式下的输出形态。

**操作步骤**：

1. 打开 [crates/extract/src/generator.rs:L137-L189](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/extract/src/generator.rs#L137-L189)，阅读 `test_convert_text`。它先测空 `trs` 只输出 `{ "_version": 2 }`，再插入 `hello → {en: "Hello", zh: "你好"}`，分别断言 JSON / YAML / TOML 三种输出。
2. 在仓库根目录运行：

   ```bash
   cargo test -p rust-i18n-extract test_convert_text -- --nocapture
   ```

**需要观察的现象**：测试通过。三种预期输出分别是

```json
{ "_version": 2, "hello": { "en": "Hello", "zh": "你好" } }
```

```yaml
_version: 2
hello:
  en: Hello
  zh: 你好
```

```toml
_version = 2

[hello]
en = "Hello"
zh = "你好"
```

**预期结果**：三条断言全部通过，证明 `convert_text` 对三种格式都产出正确的 `_version: 2` 嵌套结构。

#### 4.3.5 小练习与答案

**练习 1**：为什么 YAML 分支要去掉 `---`，而 JSON / TOML 分支不需要？

**参考答案**：`serde_yaml::to_string` 会自动在文档开头加 YAML 的文档起始符 `---`，而 rust-i18n 的 locale 文件不使用这个头（参见真实示例文件，如 `examples/app/locales/en.yml` 顶部没有 `---`）。JSON 与 TOML 的序列化器不会产生这种额外标记，所以无需处理。

**练习 2**：如果把 `value["_version"]` 改成 `Value::String("2".into())`（字符串 "2" 而非数字 2），下游 `i18n!` 加载这个文件时会怎样？（提示：回顾 u1-l4 / u2-l2 的 `get_version`。）

**参考答案**：`get_version` 用 `unwrap_or(1)` 兜底，且只有**数字 2**才走 v2 分支（u1-l4 已强调「数字 2，非字符串」）。写成字符串 `"2"` 会导致该文件被当作 v1 解析，locale 改由文件名推导，嵌套的 `{locale: text}` 结构会被错误挂载，译文查找全部失效。这就是为什么这里必须用 `Number::from(2)`。

---

### 4.4 `write_file`：落盘与目录创建

#### 4.4.1 概念说明

`write_file` 是最简单的一个模块：把序列化好的文本写到 `output_path/TODO.yml`。它有两个小职责：**确保父目录存在**（首次运行时 `locales/` 可能还没建）、**覆盖式写文件**。

#### 4.4.2 核心流程

```text
fn write_file(output, filename, data):
    output_file = output.join(filename)        # 例如 locales/TODO.yml
    folder = output_file.parent()              # locales/
    if folder 不存在: create_dir_all(folder)
    output = File::create(output_file)         # 不存在则创建，存在则截断
    writeln!(output, data)
    return Ok(())
```

关键点：`File::create` 是**覆盖写**——每次运行 `cargo i18n` 都会用本次的去重结果**整体覆盖** `TODO.yml`，而不是追加。这与「`TODO.yml` 是上一次运行的、本次重新计算」的语义一致。

#### 4.4.3 源码精读

实现见 [crates/extract/src/generator.rs:L109-L124](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/extract/src/generator.rs#L109-L124)。L113-L116 用 `create_dir_all` 保证目录存在；L118-L119 用 `File::create`（截断式）打开，失败则 panic；L121 用 `writeln!` 写入并追加一个换行。

注意这里的错误处理风格与 `generate` 不同：`write_file` 内部对 `create_dir_all` 和 `File::create` 用了 `unwrap()`/`panic!`（「写不了就直接崩」），只把最终的 `Ok(())` 作为 `Result` 返回。这是有意的——写盘失败属于不可恢复的环境问题，直接 panic 比返回错误更直接。而 `generate` 末尾返回的 `Err` 是**业务层面**的「还有未翻译文案」，两者性质不同。

#### 4.4.4 代码实践

**实践目标**：验证 `write_file` 的「覆盖写」与「自动建目录」行为。

**操作步骤**（源码阅读型）：

1. 阅读 [L109-L124](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/extract/src/generator.rs#L109-L124)，确认 `File::create` 的截断语义。
2. （本地验证）在一个 `locales/` 目录尚不存在的小项目里运行 `cargo i18n`（含未翻译文案），观察它会自动创建 `locales/` 并写入 `TODO.yml`；再人为往 `TODO.yml` 里塞一行垃圾再重跑，确认内容被整体覆盖、垃圾消失。

**需要观察的现象**：目录被自动创建；旧内容被新结果整体替换。

**预期结果**：每次运行后 `TODO.yml` 的内容严格等于本次 `generate_result` 的去重输出。> 未安装 CLI 时标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：如果 `output_path` 指向的 `locales/` 目录不存在，`write_file` 会报错吗？

**参考答案**：不会。L113-L116 的 `create_dir_all` 会递归创建所有缺失的父目录，然后再创建文件。只有当 `create_dir_all` 本身因权限等问题失败时才会 `unwrap` panic。

**练习 2**：`write_file` 用 `File::create` 而非 `OpenOptions::append`，这意味着什么？

**参考答案**：`File::create` 会把已存在的文件**截断为 0 再写**，即覆盖。所以 `TODO.yml` 永远反映「最新一次」的去重结果，不会把多次运行的待翻译文案累积起来。`append` 则会保留旧内容并追加，那样会把已翻译后理应消失的条目残留下来，破坏「`TODO.yml` = 当前缺口」的语义。

## 5. 综合实践

把四个模块串起来，做一个端到端的小任务：

**场景**：你有一个小项目，源码里有两处 `t!` 调用，但翻译文件只补了一半。你要用 `cargo i18n` 找出缺口，补齐，再验证 CI 闸门。

**步骤**：

1. 新建一个 Cargo 项目，依赖 `rust-i18n`，在 `src/main.rs` 写：

   ```rust
   // 示例代码
   use rust_i18n::t;
   rust_i18n::i18n!("locales");
   fn main() {
       println!("{}", t!("greeting"));
       println!("{}", t!("farewell"));
   }
   ```

2. 在 `locales/en.yml`（v1）只写一条：

   ```yaml
   greeting: "Hello"
   ```

   故意不写 `farewell`。

3. 在 `Cargo.toml` 里加上（让 CLI 知道去哪找翻译，见 u5-l1）：

   ```toml
   [package.metadata.i18n]
   available-locales = ["en", "zh-CN"]
   load-path = "locales"
   ```

4. 安装并运行提取器：

   ```bash
   cargo install --path crates/cli     # 在 rust-i18n 仓库内
   cd /path/to/your/project
   cargo i18n
   echo $?                              # 期望 1
   ```

5. 打开生成的 `locales/TODO.yml`，确认它是 `_version: 2` 格式，且同时包含 `en` 和 `zh-CN` 两个 locale 下 `farewell` 的占位条目（`greeting` 因 `en` 已翻译而**不**出现在 `en` 下）。
6. 把 `TODO.yml` 里的占位值改成真实译文，并合并回你的正式翻译文件（例如 `en.yml` 与 `zh-CN.yml`），删除 `TODO.yml`，再次 `cargo i18n`。
7. 预期看到 `All thing done.`，`echo $?` 为 `0`。

**需要解释的关键点**（这正是本讲实践任务要求的）：`generate_result` 如何判断某 key 已翻译过而跳过？答：它在 `for locale` 循环里调用 `load_locales`（并用 `ignore_file` 跳过 `TODO.yml` 自身）得到已有翻译表 `data`，对每条 message 的查找键 \(k\)，检查 `data[locale]` 是否**含有** \(k\)——含有即视为该 locale 下已翻译，`continue` 跳过；只有缺失时才把 `(k, locale)` 收进待翻译集合 `trs`。因此 `greeting` 在 `en` 下被跳过、在 `zh-CN` 下被收录，而 `farewell` 在两个 locale 下都被收录。

> 若本地不便安装 CLI，可退化为「源码阅读型」：对照本讲 4.2 的手算例子，把步骤 5 的 `TODO.yml` 内容按 `convert_text` 的规则手写出来，再与 4.3 的三种格式断言相互印证，标注「待本地验证」。

## 6. 本讲小结

- `generate` 是编排入口：去重 → 判空 → 序列化 → 落盘，**写盘成功后故意返回 `Err`**，配合 CLI `main` 的 `exit(1)`，把「还有未翻译文案」变成 CI 失败信号。
- `trs.is_empty()` 是「全部翻译完毕」的唯一判据；非空时才写 `TODO.yml` 并报错。
- `generate_result` 是去重大脑：**按 locale 分别判断**，查找键 \(k\) 已存在于 `data[locale]` 即跳过，谓词为 \(\text{skip}(k,\ell) \iff \text{data}[\ell] \ni k\)。
- `ignore_file` 回调让 `load_locales` 跳过 `TODO.yml` 自身，**切断「把自己上次的占位输出当成已翻译」的反馈循环**，否则跑过一次后就永远提示 `All thing done.`。
- 占位值两条分支：开启 minify_key 用原文案整句 `m.key`，关闭时用 `m.key` 按点号切分的最后一段。
- `convert_text` 以 `serde_json::Value` 为中间表示，统一构造 `_version: 2` 的嵌套 locale 结构，再分发到 YAML（去掉 `---`）/ JSON / TOML 三种输出。
- `write_file` 用 `create_dir_all` 自动建目录、用 `File::create` 覆盖式写盘，保证 `TODO.yml` 始终等于本次去重结果。

## 7. 下一步学习建议

本讲完结了「cargo i18n CLI 提取器」单元（u7）。提取器的完整闭环你已经走通：入口（u7-l1）→ 遍历与提取（u7-l2）→ 去重与生成（本讲）。接下来建议进入第八单元「并发、性能与工程实践」：

- **u8-l1 全局 locale 与 AtomicStr 线程安全**：回头看 `t!` 在运行时是如何与全局 `CURRENT_LOCALE` 配合的，理解 `arc-swap` 的无锁读写。
- **u8-l4 测试体系与质量保障**：本讲多次提到「集成测试需 `RUST_TEST_THREADS=1`」，u8-l4 会解释为何全局 locale 是共享状态、必须在单线程下测试。

如果想继续深挖提取器本身的边角，建议重读 [crates/extract/src/generator.rs:L80-L86](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/extract/src/generator.rs#L80-L86) 那段「想把行号写成 YAML 注释但受限于 serde_yaml」的 TODO，思考：若改用支持注释的 YAML 库（如 `yaml-rust2`）手写输出，能否把 `Message.locations` 的文件名与行号真正落进 `TODO.yml`，方便译者定位。
