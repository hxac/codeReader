# #[ty] 类型宏：把 Rust 类型变成 Typst 类型

## 1. 本讲目标

学完本讲后，你应该能够：

- 说出 `#[ty]` 宏把一个 Rust 类型「注册」成 Typst 类型时，在编译期做了哪些事。
- 读懂 `src/ty.rs` 里 `ty` → `parse` → `create` 的三段式流水线，并能解释中间结构 `Type`、`Meta` 的每个字段从哪里来。
- 区分被装饰项的四种分支（`Struct` / `Type` / `Enum` / `Verbatim`）以及 `keep` 标志的作用，理解 `BareType` 为何只用于 `Verbatim`。
- 说清 `scope` 与 `cast` 两个开关如何改变生成代码：`scope` 改变 constructor 与 scope 的来源，`cast` 抑制自动生成的 `cast! { type #ident, }`。
- 手写一个简单类型经 `#[ty]` 展开后的 `NativeType` impl 与 `NativeTypeData` 字面量，并解释 `LazyLock` 延迟初始化的意义。

## 2. 前置知识

本讲承接 u1-l3（共享工具层 `util.rs`），假定你已经熟悉：

- 过程宏的「parse → create」流水线骨架（u1-l2）：入口把 token 流解析成中间结构，再用 `quote!` 生成新代码。
- `util.rs` 的几组工具：`documentation()` 提取文档、`determine_name_and_title()` 用 `heck` 推导 kebab-case 名与 Title Case 标题、`parse_flag` / `parse_string` / `parse_key_value` / `parse_string_array` 四个解析 helper、`kw` 自定义关键字、`foundations`（`::typst_library::foundations` 的简写）、`Since` 三态（`Forever` / `Version` / `Unreleased`）。
- 入口回传错误的统一模式：`.unwrap_or_else(|err| err.to_compile_error())`，子模块返回 `syn::Result`，绝不 panic。

补充一个本讲会用到的新概念：**NativeType**。Typst 运行时（`typst-library`）定义了一个 trait `NativeType`，任何想在 Typst 里被当作「类型」使用的 Rust 类型都要实现它。`#[ty]` 的工作就是替你自动写出这个 impl。我们会在 4.4 节贴出它的真身。

一句话定位：`#[time]`（u2-l1）只往函数体里注入一行代码，而 `#[ty]` 是第一个「围绕被装饰项生成完整 trait impl」的宏，它是后续 `#[func]`、`#[elem]` 的简化模型。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `crates/typst-macros/src/lib.rs` | 声明 `#[proc_macro_attribute] pub fn ty` 入口，把编译器 token 流交给 `ty::ty`。 |
| `crates/typst-macros/src/ty.rs` | 本讲主角：`ty` / `parse` / `create` 三段流水线，以及中间结构 `Type`、`Meta`。 |
| `crates/typst-macros/src/util.rs` | 共享工具：`BareType`、`determine_name_and_title`、`documentation`、`oneliner`、`foundations`、各 `parse_*` helper、`Since`。 |
| `crates/typst-macros/src/cast.rs` | 当 `cast` 标志为 `false` 时，`#[ty]` 会自动生成 `cast! { type #ident, }`；这个 `type` 前缀在 `cast.rs` 里被识别为「动态类型」模式。 |
| `crates/typst-library/src/foundations/ty.rs` | 运行时契约：`NativeType` trait 与 `NativeTypeData` 结构体的定义，也是生成代码要对齐的「模板」。 |

## 4. 核心概念与源码讲解

本讲按流水线拆成四个最小模块：

- **4.1** 入口 `ty::ty`：识别被装饰项的四种分支与 `keep` 标志（含 `BareType`）。
- **4.2** 中间结构 `Type` 与元数据 `Meta`：括号参数的解析。
- **4.3** 命名推导 `parse` 与代码生成 `create`：`NativeTypeData` 字面量是怎么拼出来的。
- **4.4** 运行时契约：`NativeType` trait、`NativeTypeData` 与 `LazyLock` 延迟初始化。

### 4.1 入口 ty::ty：四种 Item 分支与 keep 标志

#### 4.1.1 概念说明

`#[ty]` 是一个**属性宏**（attribute macro），它接收两段输入：

- `stream`：括号里的参数，即 `#[ty(...)]` 中的 `...`。
- `item`：被装饰的真实代码项，例如 `struct Str(EcoString);`。

入口 `ty::ty` 要做的第一件事，是判断 `item` 到底是哪一种「类型项」。Rust 里能表示「一个类型」的语法项有好几种：结构体、枚举、类型别名、甚至一个光秃秃的 `type Name;` 声明。`syn` 把它们建模成 `syn::Item` 枚举的不同变体，`#[ty]` 需要分别处理。

这里还藏着一个 `keep` 标志：对于结构体/枚举/类型别名，宏要把原代码项**原样保留**地重新输出（否则用户写的 `struct` 就消失了）；但对于光秃秃的 `type Name;`，它不是合法的独立项，必须被**吞掉**而不重新输出。

#### 4.1.2 核心流程

```
ty(stream, item):
  1. 把 stream 解析成 Meta（括号参数）
  2. 用 match &mut item 判断类型项种类：
       Struct  → ident, attrs, keep = true
       Type    → ident, attrs, keep = true   （type 别名）
       Enum    → ident, attrs, keep = true
       Verbatim→ 把裸 token 解析成 BareType，keep = false
       其它    → 报错 "invalid type item"
  3. parse(meta, ident, attrs) → 得到中间结构 Type
  4. 从 item 的 attrs 里删掉所有 #[doc = ...]（文档已被抽走，避免重复）
  5. create(&ty, keep.then_some(&item)) → 生成最终代码
```

`keep.then_some(&item)` 是关键：`keep` 为 `true` 时传 `Some(&item)`（原项会被重新输出），`keep` 为 `false` 时传 `None`（原项被丢弃）。

#### 4.1.3 源码精读

入口与 match 分支：

[crates/typst-macros/src/ty.rs:12-28](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/ty.rs#L12-L28) —— `pub fn ty` 先把括号参数解析成 `Meta`，再用 `match &mut item` 分流四种类型项；前三者 `keep = true`，`Verbatim` 分支把裸 token 解析为 `BareType` 且 `keep = false`，其余报 `invalid type item`。

注意 `Verbatim` 分支里的 `bare = syn::parse2(item.clone())?;`：`syn::Item::Verbatim` 是 syn 对「无法归类的裸 token 序列」的兜底变体。一个光秃秃的 `type Name;` 既不是完整结构体也不是带 `=` 的类型别名，syn 就把它塞进 `Verbatim`，由 `#[ty]` 自己用 `BareType` 重新解析出 `ident` 和 `attrs`。

`BareType` 的定义与解析在 util 里：

[crates/typst-macros/src/util.rs:251-268](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/util.rs#L251-L268) —— `BareType` 表示 `type Name;` 这种裸声明，按 `属性* type 标识符 ;` 的顺序解析。它带 `#[expect(dead_code)]`，因为 `type_token`、`semi_token` 两个字段只被解析、从不被读取。

文档剥离与最终生成：

[crates/typst-macros/src/ty.rs:25-27](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/ty.rs#L25-L27) —— 先调用 `parse` 得到中间结构，再用 `attrs.retain(...)` 删掉 `doc` 属性（文档已被 `documentation()` 抽走），最后把 `keep` 折算成 `Option<&syn::Item>` 交给 `create`。

#### 4.1.4 代码实践

**目标**：确认四种分支分别对应哪种 Rust 写法。

1. 打开 `crates/typst-library/src`，用编辑器搜索 `#[ty(`。
2. 找到这三处真实用法并记录它们分别命中哪个分支：
   - `#[ty(scope, cast, title = "String", since = "forever")] pub struct Str(...)` —— `Struct` 分支（[str.rs:74](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/str.rs#L74)）。
   - `#[ty(scope, name = "direction", since = "forever")] pub enum Dir { ... }` —— `Enum` 分支（[dir.rs:21](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/dir.rs#L21)）。
   - `#[ty(scope, cast, since = "0.8.0")] pub struct Type(...)` —— 也是 `Struct` 分支（[ty.rs:63](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/ty.rs#L63)）。
3. **观察现象 / 预期结果**：标准库里 `#[ty]` 几乎都加在 `struct`/`enum` 上（`keep = true`）。`Verbatim`/`BareType` 分支在日常代码里很少出现，它是一个处理裸 `type Name;` 的兜底通道。

> 待本地验证：如果你想亲眼看 `Verbatim` 分支触发，可以尝试对一个裸 `type Foo;` 加 `#[ty]` 编译（注意它本身不是合法独立项，通常只在特殊上下文里才有意义），观察宏是否吞掉声明、只产出 impl。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `#[ty]` 加在一个 `fn` 上，会怎样？
**答案**：`match` 走到 `_ => bail!(item, "invalid type item")` 分支，编译期报错 `typst: invalid type item`（[ty.rs:23](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/ty.rs#L23)），并通过 `to_compile_error` 回传给编译器。

**练习 2**：`keep` 为 `false` 时，原 `item` 还会出现在最终生成的代码里吗？
**答案**：不会。`keep.then_some(&item)` 变成 `None`，`create` 里 `#item` 占位符展开为空，原裸声明被丢弃，只保留生成的 `NativeType` impl。

---

### 4.2 中间结构 Type 与元数据 Meta

#### 4.2.1 概念说明

`ty::ty` 把输入拆成两半：

- **括号参数** → 解析成 `Meta`：用户在 `#[ty(...)]` 里写下的配置（要不要 scope、要不要自定义 cast、叫什么名字、哪个版本引入、搜索关键词）。
- **类型项本身** → 提取出 `ident` 与 `attrs`（文档注释）。

`parse`（下一节）会把这两半合并成一个完整的中间结构 `Type`。`Type` 是「生成代码所需的全部信息」的快照——之后 `create` 只依赖它，不再回头看原始 AST。

#### 4.2.2 核心流程

`Meta` 用 u1-l3 讲过的四个 helper 按固定顺序逐个消费括号参数，形成一张「键 × 值类型」的表：

| 字段 | 解析 helper | 值类型 | 含义 |
| --- | --- | --- | --- |
| `scope` | `parse_flag::<kw::scope>` | `bool` | 是否带 `#[scope]` 配套作用域 |
| `cast` | `parse_flag::<kw::cast>` | `bool` | 是否自己写 `cast!` |
| `name` | `parse_string::<kw::name>` | `Option<String>` | 自定义短名 |
| `title` | `parse_string::<kw::title>` | `Option<String>` | 自定义标题 |
| `since` | `parse_key_value::<kw::since, Since>` | `Option<Since>` | 引入版本 |
| `keywords` | `parse_string_array::<kw::keywords>` | `Vec<String>` | 搜索关键词 |

这些 helper 都「可缺省」：括号里没写某个键就返回 `false` / `None` / 空 `Vec`。顺序无要求，因为每个 helper 都用 `peek` 试探自己的关键字。

#### 4.2.3 源码精读

`Meta` 结构与它的 `Parse` 实现：

[crates/typst-macros/src/ty.rs:44-67](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/ty.rs#L44-L67) —— `Meta` 即「`#[ty(..)]` 中的 `..`」，六个字段对应上表；`impl Parse` 逐行调用四个 `parse_*` helper 把括号内容结构化。

中间结构 `Type`：

[crates/typst-macros/src/ty.rs:30-42](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/ty.rs#L30-L42) —— `Type` 汇总了 `meta`、`ident`（Rust 名）、`name`（Typst 短名）、`long`（诊断用长名）、`title`（文档用标题）、`docs`（文档全文）。`create` 后续只读它。

对照入口可知 `ident` 来自被装饰项本身（`&item.ident`），而 `attrs` 在 `parse` 里被 `documentation()` 消化成 `docs`。

#### 4.2.4 代码实践

**目标**：用一个真实例子验证 `Meta` 各字段的来源。

1. 阅读 [crates/typst-library/src/foundations/str.rs:74](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/str.rs#L74) 的 `#[ty(scope, cast, title = "String", since = "forever")]`。
2. 把它当成 `parse_flag` / `parse_string` / `parse_key_value` 的输入，逐键推断 `Meta` 各字段的值。
3. **预期结果**：`scope = true`、`cast = true`、`name = None`（未指定）、`title = Some("String")`、`since = Some(Since::Forever)`、`keywords = []`。

#### 4.2.5 小练习与答案

**练习**：为什么 `Meta` 的六个 helper 顺序可以任意？如果用户把同一个键写两次会怎样？
**答案**：每个 `parse_*` helper 都先用 `peek` 探测自己的关键字是否在流的开头，不匹配就立刻返回缺省值，不消费任何 token，所以六个 helper 的调用顺序不影响结果。同一键写两次时，第二次匹配到仍会再解析一次，属于用户笔误；helper 本身不去重，但实践中极少出现。

---

### 4.3 命名推导 parse 与代码生成 create

#### 4.3.1 概念说明

`parse` 负责「把 `Meta` + `ident` + `attrs` 加工成 `Type`」。其中最有意思的是三个名字的推导：

- **`name`**：暴露给 Typst 代码的短名，如 `str`、`direction`。默认由 Rust 标识符转 kebab-case 得到（`Str` → `str`），可用 `name = "..."` 覆盖。
- **`title`**：文档里用的 Title Case 名，如 `String`、`Direction`。默认由 `name` 转 Title Case，可用 `title = "..."` 覆盖。
- **`long`**：错误信息里用的长名，如 `string`。它**不可配置**，直接等于 `title.to_lowercase()`。

> 区分 `name` 与 `long`：`name` 用连字符、是代码标识符（`foo-bar`）；`long` 用空格、出现在诊断信息里（`foo bar`）。多词类型时二者不同。

`create` 则是本讲的「代码生成中枢」。它要决定三件可变的事，再拼出 `NativeTypeData` 字面量和一个 `impl NativeType`：

1. **constructor**：有 scope 时取 `NativeScope::constructor()`，否则 `None`。
2. **scope**：有 scope 时取 `NativeScope::scope()`，否则新建空 `Scope::new()`。
3. **cast**：**没有**自定义 cast 时，自动补一句 `cast! { type #ident, }`。

#### 4.3.2 核心流程

```
parse(meta, ident, attrs):
  docs        = documentation(attrs)              # 抽文档
  (name,title)= determine_name_and_title(...)      # kebab + Title Case，冗余配置报错
  long        = title.to_lowercase()               # 长名不可配
  → Type { meta, ident, name, long, title, docs }

create(ty, item):
  def_site_key = ident.to_string()                 # 定位键 = 类型名
  oneliner     = oneliner(docs)                    # 首句摘要
  since        = Some(#since) 或 None
  constructor  = scope ? NativeScope::constructor() : None
  scope        = scope ? NativeScope::scope()      : Scope::new()
  cast         = (!cast).then(|| cast! { type #ident, })   # 默认补动态 cast
  data         = NativeTypeData { name, long_name: long, title, since, docs,
                                 def_site, keywords, constructor, scope }
  输出: #[doc = oneliner]  #item（可选）  #cast（可选）
        impl NativeType for #ident { const NAME = #name; fn data() { static DATA = #data; &DATA } }
```

`cast` 那一行 `(!meta.cast).then(...)` 是本讲的要点：**当且仅当**用户**没有**声明 `cast` 标志时，宏才自动生成 `cast! { type #ident, }`。这个自动生成的 `cast!` 带一个 `type` 前缀，它会被 `cast.rs` 识别为「动态类型」模式（见 4.3.3）。

#### 4.3.3 源码精读

`parse` 全文：

[crates/typst-macros/src/ty.rs:70-76](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/ty.rs#L70-L76) —— 调 `documentation` 与 `determine_name_and_title`，再把 `title` 小写化为 `long`，组装成 `Type`。

`determine_name_and_title`：

[crates/typst-macros/src/util.rs:170-194](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/util.rs#L170-L194) —— `name` 默认 `ident.to_kebab_case()`；若用户显式给的 `name` 与默认值相同则 `bail!`「name was specified unnecessarily」。`title` 默认 `name.to_title_case()`，同理对冗余 `title` 报错。注意 `#[ty]` 调用时第 4 个参数传 `None`（不做前缀裁剪，这是 `#[elem]` 才有的「去掉 `Elem` 后缀」行为）。

`create` 三态分支：

[crates/typst-macros/src/ty.rs:90-106](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/ty.rs#L90-L106) —— `constructor` 与 `scope` 都以 `meta.scope` 为条件二选一；`cast` 以 `!meta.cast` 为条件决定是否补一句 `cast! { type #ident, }`。

`NativeTypeData` 字面量：

[crates/typst-macros/src/ty.rs:108-120](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/ty.rs#L108-L120) —— 逐字段填入：`name`/`long_name`(=`long`)/`title`/`since`/`docs` 来自 `Type`；`def_site` 用 `file!()` 配 `def_site_key`（类型名）；`keywords` 展开为 `&[#(#keywords),*]`；`constructor` 与 `scope` 都包在 `LazyLock::new(|| ...)` 里。

最终输出（含 `#[doc = #oneliner]`、原项、可选 cast、impl）：

[crates/typst-macros/src/ty.rs:122-135](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/ty.rs#L122-L135) —— 注意 `data()` 内部用了一个 `static DATA` 局部静态量，把字面量落到一个固定的静态地址上，再返回 `&DATA`（详见 4.4）。

自动生成的 `cast! { type #ident, }` 中的 `type` 前缀如何被消费：

[crates/typst-macros/src/cast.rs:126-155](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/cast.rs#L126-L155) —— `CastInput::parse` 一旦看到开头的 `type` 关键字，就把 `dynamic` 置 `true`。

`dynamic = true` 的效果（castable 与 from_value 都多了一段「动态下转」判断）：

[crates/typst-macros/src/cast.rs:213-221](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/cast.rs#L213-L221) —— `create_castable_body` 里，动态模式下先判断 `Value::Dyn` 能否 `is::<Self>()`；`create_from_value_body` 里则尝试 `dynamic.downcast::<Self>()` 取回原值（[cast.rs:310-318](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/cast.rs#L310-L318)）。也就是说，默认（无 `cast` 标志）的类型会被当成「装在 `Value::Dyn` 里的动态值」进行装箱/拆箱。

#### 4.3.4 代码实践

**目标**：手动展开 `#[ty(title="String")] struct Str(EcoString);`，验证你对 `create` 的理解。

> 注意：本例**没有** `cast` 标志，所以 `cast` 默认为 `false`，宏**会**自动补 `cast! { type Str, }`。

依据 [ty.rs:79-136](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/ty.rs#L79-L136)，推导各值：

- `ident = Str`，`name = "str"`（kebab-case），`title = "String"`（指定），`long = "string"`（title 小写）。
- `scope = false` → `constructor = None`，`scope = Scope::new()`。
- `cast = false` → 补 `cast! { type Str, }`。

**预期结果**（示例代码——这是宏展开后的等价形式，非项目原有代码）：

```rust
// 示例代码：#[ty(title = "String")] struct Str(EcoString); 的等价展开
#[doc = ""]
struct Str(EcoString);

::typst_library::foundations::cast! { type Str, }

impl ::typst_library::foundations::NativeType for Str {
    const NAME: &'static str = "str";

    fn data() -> &'static ::typst_library::foundations::NativeTypeData {
        static DATA: ::typst_library::foundations::NativeTypeData =
            ::typst_library::foundations::NativeTypeData {
                name: "str",
                long_name: "string",
                title: "String",
                since: None,
                docs: "",
                def_site: ::typst_utils::DefSite { path: file!(), key: "Str" },
                keywords: &[],
                constructor: ::std::sync::LazyLock::new(|| None),
                scope: ::std::sync::LazyLock::new(
                    || ::typst_library::foundations::Scope::new()
                ),
            };
        &DATA
    }
}
```

（对比真实代码：标准库的 `Str` 实际写作 `#[ty(scope, cast, title = "String", since = "forever")]`，见 [str.rs:74](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/str.rs#L74)，所以它会带 scope、不会自动补 cast、since 为 `Forever`。）

**关于练习任务的后半问——当 `cast` 标志为 `true` 时为何不再自动生成 `cast! { type #ident, }`**：

`cast: bool` 字段的含义是「我**自己**会另写一个 `cast!` 来定义这个类型与 `Value` 之间的转换」（[ty.rs:48-49](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/ty.rs#L48-L49)）。`create` 里 `(!meta.cast).then(...)` 表示：用户**没**声明 `cast` 时宏才补一份默认的动态 `cast!`；一旦用户声明了 `cast`，宏就**闭嘴**。原因是自动补的那份 `cast! { type #ident, }` 会生成 `Reflect` / `IntoValue` / `FromValue` 三个 impl（见 [cast.rs:80-114](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/cast.rs#L80-L114)），而用户手写的 `cast!` 也会生成同样的三个 impl——两份同时存在会造成 **trait impl 重复定义**的编译错误。例如 `Str` 自己提供了把字符串与 `Str` 互转的 `cast!`（[str.rs:845-855](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/str.rs#L845-L855) 的 `EcoString`/`String` 臂都以 `Str` 互转），若宏再补一份动态版本就会冲突。因此 `cast` 标志是一个「我接管了转换，请勿插手」的信号。

#### 4.3.5 小练习与答案

**练习 1**：`#[ty] struct FooBar;`（无任何参数）的 `name` / `title` / `long` 分别是什么？
**答案**：`name = "foo-bar"`（kebab-case）、`title = "Foo Bar"`（`name.to_title_case()`）、`long = "foo bar"`（`title.to_lowercase()`，用空格）。

**练习 2**：如果写 `#[ty(name = "str")] struct Str(...)`，会发生什么？
**答案**：`determine_name_and_title` 发现用户指定的 `name` 与默认值 `"str"` 完全相同，触发 `bail!(ident, "name was specified unnecessarily")`（[util.rs:179-181](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/util.rs#L179-L181)），编译报错。这迫使你只在默认名不合适时才显式给出名字。

**练习 3**：为什么 `constructor` 与 `scope` 用同一个条件 `meta.scope`，而 `cast` 用的是 `!meta.cast`？
**答案**：`constructor`/`scope` 都是「**有** scope 时从 `NativeScope` 取，否则给空/None」，方向一致，所以共用 `meta.scope`；`cast` 恰好相反——「**没有**自定义 cast 时才补默认值」，所以用 `!meta.cast`。

---

### 4.4 运行时契约：NativeType、NativeTypeData 与 LazyLock

#### 4.4.1 概念说明

`#[ty]` 生成的代码不是凭空设计的，它要对齐 `typst-library` 定义的两个运行时实体：

- **`NativeType` trait**：每个原生类型都要实现它，提供类型名（`NAME`）和完整的类型数据（`data()`）。
- **`NativeTypeData` 结构体**：一个类型的「档案」，包含名字、文档、关键词、构造器、作用域等。

宏生成的 `impl NativeType` 就是去填这份档案。注意两个细节：

- `data()` 返回 `&'static NativeTypeData`——一个**永久静态**的引用。宏用 `static DATA: NativeTypeData = ...; &DATA` 来满足这个生命周期。
- `constructor` 与 `scope` 字段是 `LazyLock`——**延迟**到第一次被访问时才求值。

#### 4.4.2 核心流程

为什么要延迟？因为：

- `scope()` 里通常要调用一堆 `#[func]`、`#[ty]`、`#[elem]` 生成的关联数据，而这些数据彼此互相引用，**初始化顺序无法保证**。若在编译期就急切求值，极易触发循环依赖或 panic。
- `LazyLock` 把求值推迟到运行时首次访问，配合 `static` 的线程安全一次性初始化，天然解决了「谁先谁后」的问题。

`NativeTypeData` 的 `constructor`/`scope` 在结构体定义里就是 `LazyLock` 类型（[ty.rs(runtime):228-230](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/ty.rs#L228-L230)），所以宏在字面量里必须写 `LazyLock::new(|| ...)` 来构造它们。

#### 4.4.3 源码精读

`NativeType` trait：

[crates/typst-library/src/foundations/ty.rs:194-208](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/ty.rs#L194-L208) —— 要求 `const NAME: &'static str` 与 `fn data() -> &'static NativeTypeData`，并提供默认的 `fn ty()`。宏生成的 impl 正好提供 `NAME` 与 `data`（[ty.rs:127-134](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/ty.rs#L127-L134)）。

`NativeTypeData` 结构体（生成代码要对齐的模板）：

[crates/typst-library/src/foundations/ty.rs:211-231](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/ty.rs#L211-L231) —— 字段顺序与 `create` 里的字面量一一对应：`name` / `long_name` / `title` / `since` / `docs` / `def_site` / `keywords` / `constructor: LazyLock<Option<&'static NativeFuncData>>` / `scope: LazyLock<Scope>`。

`Type` 的访问器展示这些字段的用途：

[crates/typst-library/src/foundations/ty.rs:67-133](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/ty.rs#L67-L133) —— `short_name()`/`long_name()`/`title()`/`since()`/`docs()`/`keywords()` 等方法直接读取 `NativeTypeData` 的字段；`constructor()` 与 `scope()` 则解引用 `LazyLock`（运行时才触发求值）。

#### 4.4.4 代码实践

**目标**：用运行时访问器反推 `LazyLock` 的延迟效果。

1. 阅读 [ty.rs(runtime):104-115](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/ty.rs#L104-L115) 的 `keywords()`（直接返回 `self.0.keywords`，一个 `&'static [&'static str]`）与 [ty.rs(runtime):117-121](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/ty.rs#L117-L121) 的 `scope()`（返回 `&(self.0).0.scope`，解 `LazyLock`）。
2. 对比：`keywords` 是编译期就定好的静态切片；`scope` 是个 `LazyLock<Scope>`，只有 `scope()` 被调用时才真正构建。
3. **预期结果 / 待本地验证**：如果在一个测试里打日志，你会观察到某个类型的 `Str::data().name` 在程序启动时就可用，而 `Str::data().scope()` 的内部闭包只在第一次访问 scope 时执行一次。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `data()` 内部要写 `static DATA: NativeTypeData = #data; &DATA`，而不是直接 `&#data`？
**答案**：`data()` 的返回类型是 `&'static NativeTypeData`，需要一个生命周期为 `'static` 的存储位置。`#data` 只是一个临时值，无法直接取 `'static` 引用；用 `static DATA` 把它落到一个永久静态变量上，再返回 `&DATA` 才能满足签名。

**练习 2**：`NativeTypeData` 的 `constructor` 字段类型是 `LazyLock<Option<&'static NativeFuncData>>`。结合 `create`，没有 scope 时它被初始化成什么？
**答案**：`LazyLock::new(|| None)`（[ty.rs:117](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/ty.rs#L117) 对应 `constructor = None` 的情况），表示该类型没有构造器，运行时 `constructor()` 会返回 `Err`（见 [ty.rs(runtime):109-115](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/ty.rs#L109-L115)）。

---

## 5. 综合实践

把本讲四个模块串起来，完成一次「当人肉编译器」的练习。

**任务**：给定下面这个**虚构**的类型定义（示例代码，非项目原有代码），完整写出 `#[ty]` 展开后的等价代码，并回答两个追问。

```rust
// 示例代码
/// A color in RGB space.
#[ty(scope, cast, title = "Color", since = "0.5.0", keywords = ["rgb", "colour"])]
pub struct Rgb(pub u8, pub u8, pub u8);
```

**步骤**：

1. **推导 `Type` 各字段**：
   - `ident = Rgb`
   - `name`：未指定 → `Rgb.to_kebab_case()` = `"rgb"`
   - `title = "Color"`（指定；注意它与默认 `"Rgb"` 不同，所以不会触发「specified unnecessarily」报错）
   - `long = "color"`（`title.to_lowercase()`）
   - `docs = "A color in RGB space."`
   - `oneliner = "A color in RGB space."`
2. **判断三态**：
   - `scope = true` → `constructor = <Rgb as NativeScope>::constructor()`，`scope = <Rgb as NativeScope>::scope()`
   - `cast = true` → **不**补 `cast! { type Rgb, }`（用户自己写）
3. **写出展开结果**（示例代码）：

   ```rust
   #[doc = "A color in RGB space."]
   pub struct Rgb(pub u8, pub u8, pub u8);
   // 注意：cast = true，所以这里没有自动生成的 cast! { type Rgb, }

   impl ::typst_library::foundations::NativeType for Rgb {
       const NAME: &'static str = "rgb";

       fn data() -> &'static ::typst_library::foundations::NativeTypeData {
           static DATA: ::typst_library::foundations::NativeTypeData =
               ::typst_library::foundations::NativeTypeData {
                   name: "rgb",
                   long_name: "color",
                   title: "Color",
                   since: Some(::typst_library::foundations::Since::Version([0, 5, 0])),
                   docs: "A color in RGB space.",
                   def_site: ::typst_utils::DefSite { path: file!(), key: "Rgb" },
                   keywords: &["rgb", "colour"],
                   constructor: ::std::sync::LazyLock::new(
                       || <Rgb as ::typst_library::foundations::NativeScope>::constructor()
                   ),
                   scope: ::std::sync::LazyLock::new(
                       || <Rgb as ::typst_library::foundations::NativeScope>::scope()
                   ),
               };
           &DATA
       }
   }
   ```

4. **追问 A**：若把 `since = "0.5.0"` 改成 `since = "forever"`，`since` 字段会变成什么？
   **答**：`Some(::typst_library::foundations::Since::Forever)`。`Since` 的三态 `Forever` / `Version([u32;3])` / `Unreleased` 由字符串解析决定（[util.rs:299-318](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/util.rs#L299-L318)），并经 `ToTokens` 还原成运行时枚举（[util.rs:334-345](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/util.rs#L334-L345)）。

5. **追问 B**：这里 `constructor`/`scope` 用了 `NativeScope`，但代码里并没有 `impl NativeScope for Rgb`，会编译失败吗？
   **答**：会，**除非**别处（通常是同一个 `Rgb` 上的 `#[scope] impl Rgb { ... }`）提供了 `NativeScope` 实现。`#[ty(scope, ...)]` 与 `#[scope]` 是**配对**使用的：`#[ty]` 负责生成 `NativeType` impl 并在字面量里引用 `NativeScope`，`#[scope]` 负责真正实现 `NativeScope`。这正是 u3-l3 要讲的内容。

## 6. 本讲小结

- `#[ty]` 走的是与 `#[time]` 相同的「parse → create」骨架，但生成的是一整个 `impl NativeType`，是第一个「围绕被装饰项生成 trait impl」的宏。
- 入口 `ty::ty` 用 `match &mut item` 分流四种类型项（`Struct`/`Type`/`Enum`/`Verbatim`）；前三者 `keep = true` 会原样保留原项，`Verbatim`（裸 `type Name;`，由 `BareType` 解析）`keep = false` 被丢弃。
- 括号参数解析成 `Meta`（`scope`/`cast`/`name`/`title`/`since`/`keywords` 六字段），再由 `parse` 合并 `ident`、`docs`，推导出 `name`（kebab）、`title`（Title Case）、不可配的 `long`（title 小写）。
- `create` 用三个条件分支决定 `constructor`/`scope`/`cast`，拼出 `NativeTypeData` 字面量；`def_site_key` 直接取类型名。
- **关键开关**：`cast` 标志为 `true` 表示用户自写 `cast!`，宏便不再补默认的 `cast! { type #ident, }`，否则会造成 `Reflect`/`IntoValue`/`FromValue` 的重复 impl。
- `data()` 用 `static DATA` 满足 `&'static` 返回；`constructor`/`scope` 用 `LazyLock` 延迟求值，规避类型间互相引用的初始化顺序问题。

## 7. 下一步学习建议

- **下一讲 u2-l3（cast! 函数式宏）**：本讲多次提到 `cast! { type #ident, }` 的「动态模式」。u2-l3 会完整拆解 `cast!` 的输入语法（`type` 前缀、`self =>`、`v: Ty =>`）、`Pattern::Str`/`Pattern::Ty` 两种模式臂，以及 `Reflect`/`IntoValue`/`FromValue` 三个 impl 的条件生成逻辑。学完它你会彻底理解「默认动态 cast」与「自定义 cast」的差别。
- **后续 u3-l3（#[scope] 作用域组装）**：本讲 5. 综合实践追问 B 指出 `#[ty(scope, ...)]` 需要配对 `#[scope]`。u3-l3 讲 `#[scope]` 如何遍历 impl 块的 `const`/`fn`/`type`/`elem` 成员，组装出 `NativeScope::scope()` 与 `constructor()`。
- **延伸阅读源码**：在 `typst-library` 里多翻几个 `#[ty(...)]` 用法对比，例如 [`Bytes`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/bytes.rs#L44)（`scope, cast`）、[`Counter`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/introspection/counter.rs#L217)（`scope`，无 cast，会用默认动态 cast），观察 `cast` 标志的有无如何与文件里是否另写 `cast!` 对应。
