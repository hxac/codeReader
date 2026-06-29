# 本地化文件格式：v1 与 v2 两种风格

## 1. 本讲目标

学完本讲后，你应该能够：

1. 说清楚 `_version` 这个字段是什么、为什么它和 rust-i18n 这个 crate 的版本号不是一回事。
2. 区分 **v1 风格**（一种语言一个文件）和 **v2 风格**（所有语言合并到一个文件）的写法差异。
3. 解释文件名（`file_stem`）如何在 v1 里决定一个 locale，而在 v2 里被忽略。
4. 知道 `yml / yaml / json / toml` 四种扩展名都被支持，并能根据团队协作方式选择合适的版本风格。

本讲是入门单元的最后一讲，承接 [u1-l3 快速上手](./u1-l3-quick-start-example.md) 中「修改 yml 后重编译即可生效」这一结论，把「翻译文件到底长什么样、为什么有两种写法」彻底讲透。

## 2. 前置知识

阅读本讲前，你需要先了解（这些在 u1-l1 ~ u1-l3 已建立）：

- **i18n / l10n / locale**：i18n 是 internationalization（国际化）的缩写，l10n 是 localization（本地化），locale 指一种「语言＋地区」的组合，例如 `en`（英语）、`zh-CN`（中国大陆简体中文）、`zh-TW`（台湾繁体中文）。
- **编译期代码生成**：rust-i18n 在 `cargo build` 阶段就把翻译文件读进来，生成进二进制，运行时不读文件。
- **`i18n!("locales")` 宏**：它扫描 `locales/` 目录下的文件，这是本讲要细看的「被扫描的文件」。
- **点号键**：嵌套的 YAML 结构会被编译期「拍平」成形如 `messages.hello` 的点号分隔键（详见后续 u2-l3）。

如果你还不熟悉 `i18n!` / `t!` 的基本用法，建议先看 [u1-l3 快速上手](./u1-l3-quick-start-example.md)。

一个关键提醒：本讲只讲**文件怎么写**，不讲运行时怎么取值。`%{name}` 这种占位符替换是运行时行为，会在 [u3-l3 变量插值与格式化](./u3-l3-interpolation-and-format.md) 详讲，本讲你只要知道「两种风格的文件里都可以写 `%{name}`」即可。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/README.md) | 官方文档，定义并解释了 v1、v2 两种风格及示例。 |
| [examples/app/locales/en.yml](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/examples/app/locales/en.yml) | 一个**最真实的 v1 文件**：没有 `_version` 行，默认就是 v1。 |
| [examples/app/locales/fr.yml](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/examples/app/locales/fr.yml) | 配套的另一个 v1 文件，演示「一种语言一个文件」。 |
| [examples/app-minify-key/locales/v2.yml](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/examples/app-minify-key/locales/v2.yml) | 一个**典型的 v2 文件**：`_version: 2`，多个 locale 合并在同一文件里。 |
| [crates/support/src/lib.rs](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs) | 真正负责扫描、判断版本、按 v1/v2 分别解析的源码。 |

> 说明：规格里点名的关键源码是 README 和两个示例 yml；但「为什么这样写就能被正确识别」的**机制**在 `crates/support/src/lib.rs` 里，本讲会引用它来让你「知其然也知其所以然」。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- 4.1 `_version` 字段：决定解析路径的版本开关
- 4.2 v1 风格：一种语言一个文件
- 4.3 v2 风格：多语言合并到一个文件（嵌套 locale 结构）

### 4.1 `_version` 字段：决定解析路径的版本开关

#### 4.1.1 概念说明

每一个翻译文件**最顶层**可以有一个特殊的键：`_version`。它告诉 rust-i18n：「请用 v1 还是 v2 的方式来读我这个文件」。

这里有一个最容易踩的坑：**`_version` 是「翻译文件格式版本」，不是 rust-i18n 这个 crate 的版本号**。也就是说，即便你用的是 rust-i18n 4.0.0，你的翻译文件仍然可以是 `_version: 1` 或 `_version: 2`，两者互不相干。

官方文档把这一点写得很明确：

[README.md:96-101](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/README.md#L96-L101) — README 说明 `_version` 是 locale file 的版本（注意括号里特意提醒「不是 rust-i18n 版本」），默认值为 `1`；并列出了两种风格各自的用途。

两条官方解释（翻译）：

- `_version: 1` —— 把每种语言拆成不同文件，适合「把翻译工作拆分给不同人/团队」的场景。
- `_version: 2` —— 把所有语言的文案放进同一个文件，适合「用 AI（例如 GitHub Copilot）快速翻译」——你写完一行原文，按回车，AI 自动补出其他语言的翻译。

如果不写 `_version`，默认按 v1 处理。这正是 [examples/app/locales/en.yml](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/examples/app/locales/en.yml) 的做法：它**根本没有** `_version` 行，因此被当作 v1。

#### 4.1.2 核心流程

判断版本和分发解析的逻辑可以用下面的伪代码描述：

```
读入文件内容 content 和扩展名 ext
  ↓
按 ext（yml/yaml/json/toml）解析成统一的 JSON Value
  ↓
get_version(value)：读顶层 "_version" 字段
  ├── 存在且是数字 → 用该数字
  └── 不存在 / 不是数字 → 默认 1
  ↓
switch version:
  case 2 → 走 parse_file_v2（多语言合并解析）
  其它   → 走 parse_file_v1（单一语言解析）
```

关键点：**版本判定发生在「解析成内存对象之后、按结构分发之前」**。也就是说，文件先被 serde 解析成通用的 `serde_json::Value`，再由 `get_version` 看顶层有没有 `_version`。

#### 4.1.3 源码精读

先看版本判定函数 `get_version`：

[crates/support/src/lib.rs:239-245](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L239-L245) — 读取顶层 `_version` 字段，转成 `u64`；如果字段不存在或不是数字（`unwrap_or(1)`），就返回默认值 `1`。这一行 `unwrap_or(1)` 就是「不写 `_version` 等于 `_version: 1`」的来源。

再看分发逻辑 `parse_file`：

[crates/support/src/lib.rs:166-190](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L166-L190) — 这段代码做两件事：

1. 第 167-175 行：按扩展名 `yml/yaml/json/toml` 分别调用对应的 serde 解析器（`serde_yaml` / `serde_json` / `toml`），统一产出 `serde_json::Value`；其它扩展名直接报错 `"Invalid file extension"`。
2. 第 178-187 行：拿到 Value 后调用 `get_version`，如果是 `2` 就走 `parse_file_v2`，**其它所有情况**（包括 `1` 和任何非 2 的值）都走 `parse_file_v1`。

注意第 186 行的 `_ => Ok(parse_file_v1(...))`：这意味着写 `_version: 3` 或 `_version: 999` 不会报错，而是**被当成 v1**处理。所以「不是 2 就当 v1」是当前实现的事实行为。

对应的行为有单元测试佐证：

[crates/support/src/lib.rs:356-367](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L356-L367) — `test_get_version` 断言：`_version: 2` 返回 2，`_version: 1` 返回 1，而一个不含 `_version` 的文件 `foo: Foo` 返回默认值 1。

#### 4.1.4 代码实践

**实践目标**：亲手验证「不写 `_version` 等于 v1」。

**操作步骤**：

1. 打开 [examples/app/locales/en.yml](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/examples/app/locales/en.yml)，确认它只有一行 `hello: Hello, %{name}!`，没有任何 `_version`。
2. 阅读上面的 `test_get_version` 单元测试，理解 `unwrap_or(1)` 的含义。
3. 在本地新建一个临时 YAML 文件，内容只有 `foo: Foo`（不带 `_version`），在心里走一遍 `get_version`：因为没有 `_version` 键，`data.get("_version")` 返回 `None`，函数走到第 244 行返回 `1`。

**需要观察的现象**：

- 不写 `_version` 时，rust-i18n 不会报错，而是静默按 v1 解析。
- 只有显式写 `_version: 2`，才会进入 v2 解析分支。

**预期结果**：你能解释为什么 `examples/app` 的 en.yml / fr.yml 都没有 `_version` 行却工作正常——因为它们都被当作默认的 v1。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `_version` 写成 `_version: "2"`（注意是字符串 `"2"` 而不是数字 `2`），会发生什么？

**参考答案**：会被当成 v1。因为 `get_version` 里用的是 `version.as_u64()`（[lib.rs:241](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L241)），字符串 `"2"` 调 `as_u64()` 返回 `None`，于是 `unwrap_or(1)` 给出 1，走 v1 分支。所以 `_version` 的值**必须是数字**。

**练习 2**：写 `_version: 3` 会报错吗？

**参考答案**：不会报错。`parse_file` 的 match 里只有 `2 =>` 一个明确分支，其余都落到 `_ => Ok(parse_file_v1(...))`（[lib.rs:178-187](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L178-L187)），所以 `_version: 3` 等同于 v1。

---

### 4.2 v1 风格：一种语言一个文件

#### 4.2.1 概念说明

v1 的核心思想很简单：**一个文件只装一种语言**。文件名说明它是哪种语言，文件内容就是这种语言下的所有键值对。

举最真实的例子，[examples/app/locales/en.yml](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/examples/app/locales/en.yml) 全文只有一行：

```yaml
hello: Hello, %{name}!
```

文件名叫 `en.yml`，所以它属于 `en`（英语）这个 locale；键 `hello` 的英文值是 `Hello, %{name}!`。

配套地，[examples/app/locales/fr.yml](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/examples/app/locales/fr.yml) 同样只有一行：

```yaml
hello: Bonjour, %{name}!
```

文件名叫 `fr.yml`，所以键 `hello` 的法语值是 `Bonjour, %{name}!`。

两个文件、同一个键 `hello`、两种语言——这就是 v1 的典型布局：**翻译工作被天然拆分到不同文件，方便分给不同人维护**。

#### 4.2.2 核心流程

v1 解析的关键是「从文件名推导 locale」。流程如下：

```
对 locales/ 下每个匹配的文件：
  1. 取 file_stem（去掉扩展名后的文件名主体）
       en.yml      → "en"
       zh-CN.yml   → "zh-CN"
       app.en.yml  → "app.en"
  2. 对 file_stem 用 '.' 切分，取最后一段作为 locale
       "en"       → "en"
       "zh-CN"    → "zh-CN"
       "app.en"   → "en"      ← 支持「模块名.语言.yml」的命名
  3. 整个文件的键值对，全部归属到这个 locale
```

第 2 步那个 `split('.').last()` 是个有用的小设计：它允许你把文件命名为 `common.en.yml`、`nav.zh-CN.yml` 这类「带模块前缀」的名字，rust-i18n 仍能正确提取出语言部分。最简单的 `en.yml` 也能正确工作（切分后只有一段，最后一段就是它本身）。

#### 4.2.3 源码精读

从文件名推导 locale 的代码在扫描循环里：

[crates/support/src/lib.rs:129-135](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L129-L135) — 这段代码用 `file_stem()` 取文件名主体，再 `split('.').last()` 取最后一段作为 `locale`；用 `extension()` 取扩展名 `ext`。注意这个 `locale` 是从文件名算出来的，与文件**内容**无关。

> 提醒：这个扫描循环本身（`try_load_locales`）会在 [u2-l2 编译期加载与解析本地化文件](./u2-l2-load-and-parse-locales.md) 详细讲，本讲你只需要关注「locale 怎么从文件名算出来」这一步。

然后是 v1 的解析函数，极其简短：

[crates/support/src/lib.rs:193-195](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L193-L195) — `parse_file_v1` 把传入的 `locale`（从文件名算出来的）和整个文件内容 `data` 直接打包成一个 `{ locale => data }` 的映射。也就是说：**v1 把整个文件原封不动地挂到「文件名对应的那个 locale」名下**。

这也解释了 v1 为什么「一个文件一种语言」：文件内容里没有 locale 信息，locale 完全由文件名决定。

README 给出的 v1 三种格式示例（YAML / JSON / TOML），文件名分别是 `en.yml` / `en.json` / `en.toml`，内容都直接是键值对：

[README.md:122-127](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/README.md#L122-L127) — v1 的 YAML 写法，顶层是 `_version: 1` 加若干键，键名可以用点号（如 `messages.hello`），也可以是 minify_key 生成的短键（如 `t_4Cct6Q289b12SkvF47dXIx`）。

[README.md:131-138](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/README.md#L131-L138) — v1 的 JSON 写法，把同样的内容写成 JSON 对象。

[README.md:140-146](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/README.md#L140-L146) — v1 的 TOML 写法，注意 TOML 里嵌套键用 `[messages]` 段表（section table）表达，但拍平后仍是 `messages.hello`。

#### 4.2.4 代码实践

**实践目标**：用 v1 格式为一个含 `greeting` 键的项目写 `en` 和 `zh-CN` 两个翻译文件。

**操作步骤**：

1. 在你的 Cargo 项目里建 `locales/` 目录。
2. 新建 `locales/en.yml`，内容：

   ```yaml
   greeting: Hello!
   ```

3. 新建 `locales/zh-CN.yml`，内容：

   ```yaml
   greeting: 你好！
   ```

4. 在 `src/main.rs` 顶部写 `rust_i18n::i18n!("locales");`，然后用 `println!("{}", t!("greeting"));` 取值。
5. 用 `rust_i18n::set_locale("zh-CN");` 切到中文后再打印一次。

**需要观察的现象**：

- 默认 locale（`en`）下，`t!("greeting")` 输出 `Hello!`。
- `set_locale("zh-CN")` 后，输出变成 `你好！`。

**预期结果**：两个文件名 `en.yml` / `zh-CN.yml` 直接决定了 locale，文件内容里完全不需要再写语言标记。这就是 v1 的简洁之处。

> 待本地验证：如果你之前没跑过 `examples/app`，可参照 [examples/app](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/examples/app/main.rs) 的写法先把示例跑通，再换成自己的 `greeting` 键。

#### 4.2.5 小练习与答案

**练习 1**：在 v1 下，如果把 `greeting: 你好！` 写进 `locales/en.yml`（注意是 en 文件里写中文），会发生什么？

**参考答案**：rust-i18n 不检查内容语言，它只看文件名。`en.yml` 的内容会被挂到 `en` 这个 locale 下，于是 `t!("greeting", locale = "en")` 会返回 `你好！`。也就是说 v1 里「语言归属完全由文件名决定」，内容写什么语言是写代码的人自己的责任。

**练习 2**：文件命名为 `common.zh-CN.yml` 时，locale 会被识别成什么？为什么？

**参考答案**：识别成 `zh-CN`。因为 [lib.rs:130-133](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L130-L133) 先取 `file_stem()` 得到 `common.zh-CN`，再 `split('.').last()` 取最后一段 `zh-CN`。

---

### 4.3 v2 风格：多语言合并到一个文件（嵌套 locale 结构）

#### 4.3.1 概念说明

v2 的核心思想正好相反：**一个文件装下所有语言**。文件名不再决定 locale（事实上 v2 解析时**忽略**文件名算出来的 locale），locale 信息写在文件**内容**里——每个键下面，再用「语言代码」作为子键，列出各语言的译文。

看 [examples/app-minify-key/locales/v2.yml](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/examples/app-minify-key/locales/v2.yml) 的开头：

[examples/app-minify-key/locales/v2.yml:1-12](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/examples/app-minify-key/locales/v2.yml#L1-L12) — 第 1 行 `_version: 2` 声明这是 v2 文件；随后每个顶层键（这里是 minify_key 生成的短键 `T_29xGXAUPAkgvVzCf9ES3q8`，代表「苹果」这条文案）下面，用 `en / de / fr / ... / zh / zh-TW` 等 locale 作为子键，分别给出对应语言的译文。

这种「键 → { 语言: 译文 }」的两层结构，就是本模块要讲的**嵌套 locale 结构**。它的好处正如 README 所说：你写完一行 `en: Apple`，按回车，AI（如 GitHub Copilot）会自动帮你补出 `de: Apfel`、`fr: Pomme` 等其它语言——因为所有语言的译文就在同一个键的正下方，AI 一眼能看到上下文。

#### 4.3.2 核心流程

v2 的解析过程是把「键 → { 语言: 译文 }」的结构，**重新组织**成「语言 → 键 → 译文」的内部表示。伪代码如下：

```
parse_file_v2(key_prefix="", data):
  对 data 顶层每个 (key, value):
    若 value 是对象（即 { 语言: 译文 } 这种结构）:
      对其中每个 (locale, text):
        若 text 是字符串:
          把 trs[locale][拼接(key_prefix, key)] = text   ← 正常情况
        若 text 又是对象:
          递归 parse_file_v2(key_prefix=拼接(key_prefix, key), value)  ← 更深嵌套
  返回按 locale 分组的翻译表
```

两个要点：

1. **键的拼接**用 `format_keys`，把前缀和当前键用 `.` 连起来，空前缀会被过滤掉（[lib.rs:248-254](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L248-L254)）。
2. **输出按 locale 分组**：解析完得到的是 `{ en: {key: text}, zh-CN: {key: text}, ... }`，和 v1 多文件合并后的最终形态一致——这也是为什么两种风格最终都能被同一套 `t!` 查找逻辑使用。

换句话说，v1 和 v2 只是**输入写法不同**，解析后的内部数据结构殊途同归。

#### 4.3.3 源码精读

核心解析函数 `parse_file_v2`：

[crates/support/src/lib.rs:198-236](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L198-L236) — 这段代码做的事：

- 第 201-202 行：遍历顶层对象的每个 `(key, value)`。
- 第 203-205 行：只有当 `value` 是对象时才处理；对它内部的每个 `(locale, text)`，若 `text` 是字符串，就用 `format_keys` 拼出完整键，写入 `trs[locale][key] = text`（第 206-213 行）。
- 第 216-225 行：若 `text` 还是对象（更深嵌套），则递归调用 `parse_file_v2`，把当前 `key` 作为新的前缀继续往下拆。
- 第 231-235 行：只要收集到了内容就返回 `Some(trs)`，否则返回 `None`（此时外层会报「Invalid locale file format」）。

注意分发处对返回值的处理：

[crates/support/src/lib.rs:178-186](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L178-L186) — 当 `get_version` 返回 2 时，调用 `parse_file_v2("", &v)`，**传入空字符串 `""` 作为 key_prefix，完全不传文件名算出来的 locale**。这就是「v2 忽略文件名、locale 由内容决定」在代码层面的体现。

README 给出的标准 v2 写法（用普通键名，而不是短键）：

[README.md:173-185](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/README.md#L173-L185) — 顶层是 `_version: 2`，然后 `hello:` 下面有 `en: Hello world` 和 `zh-CN: 你好世界`；`messages.hello:` 下面同样列出各语言。注意这里的 `messages.hello` 是**键名里直接带点号**，它不会被拆成嵌套，而是作为一个完整的扁平键（因为它是顶层 key，经 `format_keys` 拼接后原样保留）。

v2 的嵌套 locale 结构有单元测试覆盖，包括「键名带点号」的情况：

[crates/support/src/lib.rs:386-405](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L386-L405) — `test_parse_file_in_yaml_with_nested_locale_texts`：对一个 `welcome:` 下面含 `en/zh-CN/jp` 三种语言的 v2 文件，断言解析后 `trs["en"]["welcome"] == "Welcome"`、`trs["zh-CN"]["welcome"] == "欢迎"` 等；同时对键名 `welcome.sub`（带点号），断言它作为一个完整键 `trs["en"]["welcome.sub"] == "Welcome 1"`，**不会**被拆成 `welcome → sub` 的嵌套。

还有 JSON 版本的同类测试：

[crates/support/src/lib.rs:369-384](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L369-L384) — `test_parse_file_in_json_with_nested_locale_texts`：对 JSON 写的 v2 文件做同样的断言，证明 v2 的嵌套 locale 结构与文件格式（JSON/YAML/TOML）无关。

#### 4.3.4 代码实践

**实践目标**：用 v2 格式把上一节（4.2）的 `greeting` 键写进**同一个文件**，同时包含 `en` 和 `zh-CN`。

**操作步骤**：

1. 在 `locales/` 下新建一个文件 `locales/app.yml`（文件名随意，v2 不靠文件名决定语言）。
2. 写入：

   ```yaml
   _version: 2
   greeting:
     en: Hello!
     zh-CN: 你好！
   ```

3. 顶部仍写 `rust_i18n::i18n!("locales");`，用 `t!("greeting")` 取值，分别测试默认和 `set_locale("zh-CN")` 后的输出。

**需要观察的现象**：

- 和 4.2 的 v1 写法得到的运行结果**完全一样**：默认 `Hello!`，切到 `zh-CN` 后 `你好！`。
- 区别只在于：v2 只用了一个文件 `app.yml`，而 v1 用了 `en.yml` 和 `zh-CN.yml` 两个文件。

**预期结果**：你亲眼确认「v1 和 v2 只是写法不同，最终行为一致」。

**选做（验证文件名无关）**：把 `app.yml` 改名成 `anything-here.yml`，重新 `cargo build` 并运行，结果应当不变——因为 v2 忽略文件名算出的 locale。

#### 4.3.5 小练习与答案

**练习 1**：在 v2 文件里，文件名算出来的 locale（例如 `app.yml` 算出 `app`）会被用到吗？

**参考答案**：不会。在 [lib.rs:180](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L180)，v2 分支调用的是 `parse_file_v2("", &v)`，传入空前缀，且 `parse_file_v2` 内部（[lib.rs:198-236](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L198-L236)）完全不使用外部传入的 locale，locale 全部来自文件内容里的语言子键。

**练习 2**：v2 文件里写 `messages.hello:`（键名带点号）和写成真正嵌套的 `messages:` → `hello:`，效果一样吗？

**参考答案**：不一定一样。键名 `messages.hello` 经 `format_keys` 拼接后作为一个**完整扁平键** `messages.hello` 保留（见 [lib.rs:404](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L404) 的断言）；而真正嵌套的 `messages: { hello: { en: ... } }` 会触发 `text.is_object()` 的递归分支（[lib.rs:216-225](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L216-L225)）。在 v2 里，**推荐用「键名带点号 + 每键下列各语言」的标准写法**（README 示例即如此），避免不必要的深层嵌套。

**练习 3**：如果一个文件写了 `_version: 2`，但内容里没有任何「键 → {语言: 译文}」的结构，会怎样？

**参考答案**：`parse_file_v2` 收集不到任何内容，返回 `None`（[lib.rs:231-235](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L231-L235)），外层 [lib.rs:184](https://github.com/longbridge/rust-i18n/blob/97cf091c24e4bc09a0acb397a8d9d7da8b6abc56/crates/support/src/lib.rs#L184) 返回错误 `"Invalid locale file format, please check the version field"`，编译期报错。

---

## 5. 综合实践

**任务**：分别用 v1 和 v2 两种格式，各写一套含 `greeting` 键的 `en` / `zh-CN` 翻译文件，运行后对比两者的写法差异，并说明各自适用场景。

**步骤**：

1. 新建一个 Cargo 项目，加入 `rust-i18n` 依赖。
2. **v1 版**：在 `locales/` 下建两个文件：

   - `locales/en.yml`：

     ```yaml
     greeting: Hello!
     farewell: See you!
     ```

   - `locales/zh-CN.yml`：

     ```yaml
     greeting: 你好！
     farewell: 再见！
     ```

3. **v2 版**：删掉上面两个文件，改成一个 `locales/app.yml`：

   ```yaml
   _version: 2
   greeting:
     en: Hello!
     zh-CN: 你好！
   farewell:
     en: See you!
     zh-CN: 再见！
   ```

4. `src/main.rs`：

   ```rust
   rust_i18n::i18n!("locales");
   use rust_i18n::t;

   fn main() {
       println!("{}", t!("greeting"));
       rust_i18n::set_locale("zh-CN");
       println!("{}", t!("greeting"));
   }
   ```

5. 分别在 v1 版和 v2 版配置下 `cargo run`，确认两次输出都是先 `Hello!` 后 `你好！`。

**需要观察的现象与思考**：

| 维度 | v1（一语言一文件） | v2（多语言合并） |
| --- | --- | --- |
| 文件数量 | 每种语言一个文件 | 所有语言一个文件（或按模块拆几个） |
| locale 来源 | 文件名（`file_stem`） | 文件内容里的语言子键 |
| 改一条译文 | 只动对应语言文件，diff 干净 | 改一个文件，但同文件里多语言并列 |
| 适合谁 | 多人/多团队各负责一种语言 | 单人 + AI 辅助翻译（Copilot 看上下文补全） |
| 是否需要 `_version` | 可省略（默认 1），也可显式写 `_version: 1` | 必须显式写 `_version: 2` |

**预期结果**：

- 两种写法运行结果完全一致，证明 v1/v2 只是输入形态不同。
- 你能根据团队情况给出选择：**翻译由不同人或外包团队各做一种语言 → 选 v1**（每人只动自己的文件，互不冲突）；**翻译主要由你一个人写、并借助 AI 批量补全多语言 → 选 v2**（一个键下面所有语言排在一起，AI 一目了然）。

> 待本地验证：实际运行行为以你本机 `cargo run` 的输出为准。若改了 yml 却没生效，记得检查是不是触发了重编译（详见 [u5-l2 构建脚本与增量重编译](./u5-l2-build-script.md)）。

## 6. 本讲小结

- `_version` 是**翻译文件格式版本**，不是 rust-i18n crate 的版本号；不写时默认 `1`；只有显式 `_version: 2`（数字 2）才走 v2。
- **v1**：一个文件一种语言，locale 由文件名（`file_stem` + `split('.').last()`）决定，整个文件原样挂到该 locale 下（`parse_file_v1`）。
- **v2**：一个文件多种语言，采用「键 → { 语言: 译文 }」的**嵌套 locale 结构**；解析时忽略文件名，由 `parse_file_v2` 把数据重组成按 locale 分组的内部表示。
- v1 和 v2 解析后**内部数据结构殊途同归**，所以都能被同一套 `t!` 查找逻辑使用。
- `yml / yaml / json / toml` 四种扩展名在两种风格下都受支持（由 `parse_file` 按扩展名分发到对应 serde 解析器）。
- 选型依据：多人分语言维护用 v1，单人 + AI 辅助翻译用 v2。

## 7. 下一步学习建议

本讲你只看了「文件长什么样」和「版本怎么判定」，还没有深入**扫描与解析的完整流程**。建议接下来：

1. 进入 [u2-l2 编译期加载与解析本地化文件](./u2-l2-load-and-parse-locales.md)：看 `try_load_locales` 如何用 glob 扫描 `**/*.{yml,yaml,json,toml}`、如何把多个文件合并。
2. 进入 [u2-l3 多文件合并与键扁平化](./u2-l3-merge-and-flatten-keys.md)：理解本讲提到的「嵌套键拍平成点号键」是怎么由 `flatten_keys` 完成的。
3. 如果你对运行时取值更感兴趣，可以先跳到 [u3-l1 t! 宏的完整调用链](./u3-l1-t-macro-call-chain.md)，但注意它依赖 [u2-l4 generate_code 生成的运行时代码](./u2-l4-generate-code.md)，按顺序学会更扎实。
