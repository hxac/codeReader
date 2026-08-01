# 宏的两种入口：timed! 宏与 #[time] 属性宏

## 1. 本讲目标

学完本讲，你应当能够：

- 读懂 `typst-timing` 里 `timed!` 这个 `macro_rules!` 声明式宏的**两个匹配分支**，并写出它们各自的展开结果。
- 读懂 `typst-macros` 里 `#[time]` 这个**过程宏属性（proc-macro attribute）**如何解析 `name` / `span` 参数、如何把一条 `let __scope = ...;` 插到函数体首行。
- 说清两种入口在「路径写法」「span 处理」上的关键差异：为什么 `timed!` 用 `$crate::`、而 `#[time]` 用 `::typst_timing::` 绝对路径；为什么 `timed!` 的 `span = ...` 直接塞进 `Some(...)`、而 `#[time]` 要补一个 `.into_raw()`。
- 解释 `Span` 为什么要通过 `into_raw()` 转成 `NonZeroU64` 才能交给 `typst-timing`，以及这如何**规避 `typst-syntax` ↔ `typst-timing` 的循环依赖**。

本讲承接 u1-l2（你在那里第一次用了 `timed!` 宏）和 u3-l1（你在那里确认了「两个宏入口汇入同一道门 `with_span`」）。本讲不再讲门控本身，而是把镜头推近到**两个宏各自的展开产物**上，回答：同样是「造一个 `TimingScope`」，声明式宏和过程宏各自怎么生成代码？它们对 `span` 的处理为什么不一样？

## 2. 前置知识

### 2.1 回顾：TimingScope 的两种构造（来自 u1-l2 / u3-l1）

- `TimingScope::new(name)` 和 `TimingScope::with_span(name, span)` 都返回 **`Option<TimingScope>`**。
- `with_span` 是门控的唯一入口；`new` 只是 `with_span(name, None)` 的包装。
- 拿到的 `Option<TimingScope>` 绑到一个局部变量 `__scope` 上：进入作用域记 `Start`，离开作用域（`Drop`）记 `End`。关闭计时时返回 `None`，连 `Drop` 都不会触发——这就是零成本门控。

### 2.2 声明式宏 `macro_rules!` vs 过程宏 `proc_macro_attribute`

Rust 有两套宏系统，本讲两种入口各占一种：

- **声明式宏（`macro_rules!`）**：靠**模式匹配**做文本替换。`timed!` 属于这一类。它写在 `typst-timing` 自己的源码里，编译期由编译器展开，能用 `$crate` 引用「定义该宏的 crate」，跨 crate 调用时路径自动正确。
- **过程宏属性（`#[proc_macro_attribute]`）**：是一段**操作语法树的 Rust 程序**，接收 `TokenStream`、返回新的 `TokenStream`。`#[time]` 属于这一类，它定义在独立的 `typst-macros` crate 里。过程宏**拿不到 `$crate`**，所以它生成的代码里引用其它 crate 时，必须写**绝对路径**（如 `::typst_timing::...`）。

> 一句话：`timed!` 是「带 `$crate` 卫生的文本模板」；`#[time]` 是「改写函数语法树的小程序」。

### 2.3 Span 与裸数值（来自 u2-l1）

- `Event` 结构体里 `span: Option<NonZeroU64>` 字段存的是**裸数字**，而不是 `typst-syntax` 的 `Span` 类型。
- 用 `NonZeroU64` 的好处之一：`0` 天然表示 `None`，内存上「一个可能缺失的值」不需要额外判别位。

## 3. 本讲源码地图

本讲横跨三个 crate 的少量代码：

| 文件 | 作用 |
| --- | --- |
| [crates/typst-timing/src/lib.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs) | `timed!` 宏定义（两个分支）、`TimingScope::new` / `with_span`、以及「为什么 span 是裸数字」的官方注释。 |
| [crates/typst-macros/src/time.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-macros/src/time.rs) | `#[time]` 属性宏的全部展开逻辑（解析参数 + 改写函数体）。 |
| [crates/typst-macros/src/lib.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-macros/src/lib.rs) | `#[time]` 作为 `#[proc_macro_attribute]` 的注册入口。 |
| [crates/typst-macros/src/util.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-macros/src/util.rs) | `#[time]` 复用的参数解析工具：`parse_string`、`parse_key_value` 和自定义关键字 `kw::name` / `kw::span`。 |
| [crates/typst-syntax/src/span.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs) | `Span::into_raw()` 的定义——把 `Span` 变成 `NonZeroU64` 的唯一官方出口。 |
| [crates/typst/src/lib.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs) | 全仓库**唯一在用**的 `timed!` 真实调用点（第 158 行）。 |

## 4. 核心概念与源码讲解

### 4.1 timed! 宏（macro_rules!）的两种分支

#### 4.1.1 概念说明

`timed!` 是 `typst-timing` 对外暴露的「轻量埋点」入口：你给它一个名字和一个表达式，它就把这个表达式包进一个 `TimingScope`，并原样返回表达式的值。它用 `macro_rules!` 写成，靠两条匹配分支区分「带 span」和「不带 span」两种用法。

之所以要做成**宏**而不是普通函数，是因为它要达成两件普通函数做不到的事：

1. **包裹任意表达式并返回其值**：宏把表达式 `$body` 放在 `let __scope = ...;` 之后，让 `__scope` 的生命周期正好覆盖整个 `$body`，且不改变 `$body` 的返回值。
2. **在「计时关闭」时彻底消失**：`new`/`with_span` 返回 `Option`，关闭时是 `None`，没有锁、没有分配（详见 u3-l1）。

#### 4.1.2 核心流程

`timed!` 的展开流程可以概括为：

```text
timed!("foo", expr)
  └─ 匹配第二条分支（无 span）
       └─ 展开成 { let __scope = TimingScope::new("foo"); expr }

timed!("foo", span = s, expr)
  └─ 匹配第一条分支（有 span）
       └─ 展开成 { let __scope = TimingScope::with_span("foo", Some(s)); expr }
```

注意两条分支的**末尾都是 `$body`**——这正是「原样返回被包裹表达式之值」的实现：一个块表达式的值，就是它最后一条表达式的值；前面的 `let __scope = ...;` 只负责把作用域「架」起来。

#### 4.1.3 源码精读

宏定义整体在 [crates/typst-timing/src/lib.rs:34-44](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L34-L44)：

```rs
#[macro_export]
macro_rules! timed {
    ($name:expr, span = $span:expr $(,)?) => {{
        let __scope = $crate::TimingScope::with_span($name, Some($span));
        $body
    }};
    ($name:expr, $body:expr $(,)?) => {{
        let __scope = $crate::TimingScope::new($name);
        $body
    }};
}
```

逐点拆解：

- `#[macro_export]` 让宏从 crate 根导出，外部 crate `use typst_timing::timed` 后即可直接写 `timed!(...)`。
- 第一条分支 `($name:expr, span = $span:expr $(,)?)`：匹配形如 `timed!("foo", span = X, body)` 的调用。`span = ` 是**字面 token**，编译器靠它区分两条分支；`$(,)?` 允许尾部多一个可选逗号。
- 第二条分支 `($name:expr, $body:expr $(,)?)`：匹配不带 span 的 `timed!("foo", body)`。**声明式宏自上而下尝试匹配**，所以「带 span」的分支必须写在前面，否则会被「无 span」分支抢先吞掉 `span = X` 这段 token。
- `$crate::TimingScope::...`：`$crate` 是「定义本宏的 crate」的占位符。即便在 `typst-eval`、`typst-layout` 等外部 crate 里展开，`$crate` 也会正确解析为 `typst_timing`。这是声明式宏跨 crate 安全引用自身 crate 的标准手段。
- 两条分支生成的语句分别落到 `with_span` 与 `new`：

[crates/typst-timing/src/lib.rs:161-164](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L161-L164) 的 `new` 只是 `with_span(name, None)` 的转发：

```rs
pub fn new(name: &'static str) -> Option<Self> {
    Self::with_span(name, None)
}
```

最终都汇入 [crates/typst-timing/src/lib.rs:171-177](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L171-L177) 的 `with_span`——这就是 u3-l1 讲过的「唯一门控」：

```rs
pub fn with_span(name: &'static str, span: Option<NonZeroU64>) -> Option<Self> {
    if is_enabled() {
        return Some(Self::new_impl(name, span));
    }
    None
}
```

全仓库目前**唯一在用**的 `timed!` 真实调用点在 [crates/typst/src/lib.rs:158](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L158)，用的是「无 span」两参数形式：

```rs
if timed!("check stabilized", constraint.validate(document.introspector())) {
```

它展开后等价于：

```rs
if { let __scope = ::typst_timing::TimingScope::new("check stabilized");
     constraint.validate(document.introspector())
} {
```

——`__scope` 在块结束时 drop，记下 End；`constraint.validate(...)` 的布尔结果原封不动成为 `if` 的条件。

#### 4.1.4 代码实践

**实践目标：** 用 `cargo expand` 亲眼看到 `timed!` 的展开产物，验证「`__scope` 绑定 + 原样返回」。

**操作步骤：**

1. 在 `typst-timing` 之外新建一个最小二进制 crate，或直接复用仓库里任一已依赖 `typst-timing` 的 crate。
2. 写一段调用：
   ```rs
   use typst_timing::{enable, timed};
   fn main() {
       enable();
       let n = timed!("calc", 1u32 + 2);
       println!("{n}");
   }
   ```
3. 安装并运行展开工具（在 crate 根目录）：`cargo install cargo-expand` 之后 `cargo expand`。
4. 在输出里定位到 `main`，观察 `timed!("calc", 1u32 + 2)` 被替换成 `let __scope = ...TimingScope::new("calc"); 1u32 + 2`。

**需要观察的现象：** 展开后的代码里出现 `__scope` 绑定，且 `1u32 + 2` 仍位于块末尾、作为整个块的值。

**预期结果：** `println!("{n}")` 打印 `3`——证明宏没有改变表达式的值。展开文本里能看到 `$crate` 已被解析为 `::typst_timing`（或等价的 crate 路径）。

**若本地不便安装 `cargo-expand`：** 可改为手工在纸上把上面的调用按 4.1.2 的规则改写一遍，对照本节源码核对。运行结果（打印 `3`）「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1：** 如果把 `macro_rules! timed` 里两条分支的顺序对调（先写「无 span」分支，再写「有 span」分支），会发生什么？

**参考答案：** 声明式宏自上而下匹配。「无 span」分支 `($name:expr, $body:expr)` 会贪婪地把 `"foo"` 当 `$name`、把 `span = X, body` 整段当成一个 `$body:expr`——但这通常无法解析为一个合法表达式，从而编译失败；即便侥幸解析通过，`span = ...` 也被当成了普通表达式而非 span 参数。所以「带 span」分支必须在前。

**练习 2：** 为什么宏里用 `$crate::TimingScope`，而不是直接写 `::typst_timing::TimingScope`？

**参考答案：** `$crate` 保证「无论在哪个 crate 里展开，都指向定义 `timed!` 的那个 crate（即 `typst_timing`）」，避免路径写死在外部 crate 里可能的名字冲突或重命名问题；对声明式宏而言这是唯一的跨 crate 卫生手段。

---

### 4.2 #[time] 属性宏在 typst-macros 中的展开逻辑

#### 4.2.1 概念说明

`#[time]` 是 `timed!` 的「函数版兄弟」：它贴在一个 `fn` 上，让**整个函数体**被一个 `TimingScope` 包住。函数进入时记 `Start`、函数返回时（无论正常返回还是 `?` 提前返回）记 `End`。

它是一个**过程宏属性**，定义在独立的 `typst-macros` crate 里。和 `timed!` 的「文本模板」不同，它是一段**接收语法树、改写语法树、再吐回新语法树**的 Rust 程序。它的全部展开逻辑只有 45 行，但麻雀虽小、五脏俱全：解析参数、决定构造方式、把语句插到函数体最前面。

> 诚实的现状说明：在当前 HEAD，`#[time]` 在整个 typst 仓库里**没有在用的调用点**（只有它的定义和文档示例）。它是为「给整个函数加计时」预留的基础设施。但理解它的展开逻辑，正好和 `timed!` 形成清晰对照，也是本讲的价值所在。

#### 4.2.2 核心流程

`#[time]` 的处理分三步：

```text
1. 入口  time(stream, item)
        └─ 把属性参数 stream 解析成 Meta { name, span }
        └─ 把函数 item 交给 create()

2. 决策  create(meta, item)
        └─ 名字：meta.name，没有就用函数名 ident 转字符串
        └─ 构造：有 span → with_span(name, Some(span.into_raw()))
                  无 span → new(name)

3. 改写  在 item.block.stmts 的下标 0 处插入：
        let __scope = ::typst_timing::TimingScope::<构造>;
        其余语句原样保留 → 函数返回值不变
```

关键细节：**只插一条首行语句，不动函数签名、不动其余函数体**。这意味着原函数的返回值完全保留，只是「进门时多登记了一下」。

#### 4.2.3 源码精读

入口注册在 [crates/typst-macros/src/lib.rs:362-368](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-macros/src/lib.rs#L362-L368)，标准的 `#[proc_macro_attribute]`：

```rs
#[proc_macro_attribute]
pub fn time(stream: BoundaryStream, item: BoundaryStream) -> BoundaryStream {
    let item = syn::parse_macro_input!(item as syn::ItemFn);
    time::time(stream.into(), item)
        .unwrap_or_else(|err| err.to_compile_error())
        .into()
}
```

`item` 被解析成 `syn::ItemFn`（一个函数语法树节点）；如果用户把 `#[time]` 贴到了非函数的东西上，`parse_macro_input!` 会直接报错。

真正的展开在 [crates/typst-macros/src/time.rs:9-44](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-macros/src/time.rs#L9-L44)，分两段看。先看参数解析——[crates/typst-macros/src/time.rs:15-27](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-macros/src/time.rs#L15-L27)：

```rs
pub struct Meta {
    pub span: Option<syn::Expr>,
    pub name: Option<String>,
}

impl Parse for Meta {
    fn parse(input: ParseStream) -> Result<Self> {
        Ok(Self {
            name: parse_string::<kw::name>(input)?,
            span: parse_key_value::<kw::span, syn::Expr>(input)?,
        })
    }
}
```

- `name` 和 `span` **都是可选的**（返回 `Option`），所以 `#[time]`、`#[time(name = "foo")]`、`#[time(span = span)]`、`#[time(name = "foo", span = span)]` 都合法。
- 解析**有固定顺序**：先 `name` 再 `span`，所以两个都写时 `name` 必须在前。这复用了 `util.rs` 里的两个工具：`parse_string` 要求 `name = "字符串字面量"`（[crates/typst-macros/src/util.rs:136-140](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-macros/src/util.rs#L136-L140)），`parse_key_value` 则把 `span = ` 后面当成**任意表达式** `syn::Expr`（[crates/typst-macros/src/util.rs:114-126](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-macros/src/util.rs#L114-L126)）。关键字 `name` / `span` 本身在 [crates/typst-macros/src/util.rs:270-282](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-macros/src/util.rs#L270-L282) 用 `syn::custom_keyword!` 定义。

再看改写逻辑——[crates/typst-macros/src/time.rs:29-44](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-macros/src/time.rs#L29-L44)：

```rs
fn create(meta: Meta, mut item: syn::ItemFn) -> TokenStream {
    let name = meta.name.unwrap_or_else(|| item.sig.ident.to_string());
    let construct = match meta.span.as_ref() {
        Some(span) => quote! { with_span(#name, Some(#span.into_raw())) },
        None => quote! { new(#name) },
    };

    item.block.stmts.insert(
        0,
        parse_quote! {
            let __scope = ::typst_timing::TimingScope::#construct;
        },
    );

    item.into_token_stream()
}
```

四个要点：

1. **名字默认值**：`meta.name.unwrap_or_else(|| item.sig.ident.to_string())`——没写 `name = ...` 就用函数名。所以 `#[time]` 贴在 `fn fibonacci` 上，事件名就是 `"fibonacci"`。
2. **构造分两路**：有 `span` 生成 `with_span(#name, Some(#span.into_raw()))`，无 `span` 生成 `new(#name)`。注意 `#name` 是 `String`，被插进 `&'static str` 形参——这里依赖 `From<String>` 把字面量字符串插值成 `&'static str` 字面量（`quote!` 产出的是字符串字面量 token）。
3. **绝对路径**：生成的是 `::typst_timing::TimingScope::...`。过程宏拿不到 `$crate`，所以必须用绝对路径，确保在被贴宏的任意 crate 里都能找到 `typst_timing`。
4. **插到下标 0**：`item.block.stmts.insert(0, ...)` 把 `let __scope = ...;` 放到函数体**第一条语句**之前。`__scope` 的作用域是整个函数块，函数无论从哪条路径返回，都会触发它的 `Drop` → 记 `End`。其余语句一字未动，所以返回值不变。

#### 4.2.4 代码实践

**实践目标：** 手工模拟 `#[time(name = "foo")]` 的展开，验证「插一条首行语句、函数体其余不变」。

**操作步骤：**

1. 阅读上面的 [time.rs:29-44](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-macros/src/time.rs#L29-L44)。
2. 给定这样一个普通函数（示例代码，非项目原有）：

   ```rs
   // 示例代码
   #[time(name = "foo")]
   fn bar(x: i32) -> i32 {
       let y = x * 2;
       y + 1
   }
   ```

3. 按 `create` 的规则手工改写：`meta.name = Some("foo")`，`meta.span = None` → `construct = new("foo")` → 在函数体首行插入 `let __scope = ::typst_timing::TimingScope::new("foo");`。

**需要观察的现象 / 预期结果：** 改写后的函数体应为：

```rs
// 示例代码：#[time(name = "foo")] 展开后
fn bar(x: i32) -> i32 {
    let __scope = ::typst_timing::TimingScope::new("foo");
    let y = x * 2;
    y + 1
}
```

即：首行多出一条 `let __scope = ...;`，其余语句（`let y = ...; y + 1`）和函数签名完全不变。若去掉 `name` 写成 `#[time]`，则事件名变为 `"bar"`（取自函数 ident）。

**运行验证：** 「待本地验证」——可在一个依赖 `typst-macros` 与 `typst-timing` 的 crate 里真实贴上 `#[time]` 并 `cargo expand` 对照。

#### 4.2.5 小练习与答案

**练习 1：** 为什么 `#[time]` 生成的代码写 `::typst_timing::TimingScope`，而 `timed!` 写 `$crate::TimingScope`？

**参考答案：** 过程宏（`#[time]`）在 `typst-macros` 里定义，但它生成的代码要插入到**别的 crate**（如 `typst-eval`）里。过程宏没有 `$crate` 机制，只能写绝对路径 `::typst_timing::...` 保证解析正确。而 `timed!` 是声明式宏且就定义在 `typst-timing` 内，可以用 `$crate` 自动卫生。

**练习 2：** 如果把 `#[time]` 贴到一个没有函数体、只有签名的项上（比如 `type Foo;`），会发生什么？

**参考答案：** 入口 [lib.rs:364](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-macros/src/lib.rs#L364) 用 `syn::parse_macro_input!(item as syn::ItemFn)` 强制把被贴项解析成函数；遇到 `type Foo;` 会解析失败，`parse_macro_input!` 直接抛出编译错误。

---

### 4.3 span 转为 NonZeroU64 与循环依赖规避

#### 4.3.1 概念说明

现在来看两个宏最微妙的差异：**对 `span` 的处理**。

- `timed!` 的 `span = $span`：宏把 `$span` **直接**塞进 `Some($span)`，交给 `with_span(name, Option<NonZeroU64>)`。也就是说，调用者提供的表达式**本身就得是 `NonZeroU64`**。
- `#[time]` 的 `span = $expr`：宏会**替你补一个 `.into_raw()`**，生成 `with_span(name, Some($expr.into_raw()))`。也就是说，调用者提供的是一个 `Span`，宏负责把它拍扁成裸数字。

这一节回答两个问题：为什么要在 `Span` 和 `NonZeroU64` 之间转换？又为什么是「一个宏替你转、一个宏要你自己转」？

#### 4.3.2 核心流程

```text
Span（typst-syntax 里的类型，内部就是一个 NonZeroU64）
   │
   │  Span::into_raw()   ← 唯一的官方「取裸数值」出口
   ▼
NonZeroU64
   │
   │  塞进 Event.span / with_span 的 Option<NonZeroU64>
   ▼
typst-timing 只认 NonZeroU64，从不认识 Span
```

为什么 `typst-timing` 只认裸数字？因为依赖方向是**单向**的：

```text
typst-syntax  ──depends on──▶  typst-timing
```

`syntax`、`eval`、`layout` 等几乎所有核心 crate 都依赖 `typst-timing`（要在热路径埋点）。如果 `typst-timing` 反过来依赖 `typst-syntax`（为了用 `Span` 类型），就会形成 `typst-syntax → typst-timing → typst-syntax` 的**循环依赖**，Rust 的 crate 图不允许环。解决办法：`typst-timing` 用 `NonZeroU64`（标准库类型）做「不透明句柄」，把 `Span` 挡在门外；需要时由调用方用 `into_raw()` 转一下再传进来。

#### 4.3.3 源码精读

这个设计在 `typst-timing` 的 `with_span` 文档里写得明明白白——[crates/typst-timing/src/lib.rs:166-172](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L166-L172)：

```rs
/// Create a new scope with a span if timing is enabled.
///
/// The span is a raw number because `typst-timing` can't depend on
/// `typst-syntax` (or else `typst-syntax` couldn't depend on
/// `typst-timing`).
#[inline]
pub fn with_span(name: &'static str, span: Option<NonZeroU64>) -> Option<Self> {
```

注释一句话点破：**「span 是裸数字，因为 `typst-timing` 不能依赖 `typst-syntax`，否则 `typst-syntax` 就没法依赖 `typst-timing`。」**

再看 `#[time]` 是怎么补 `into_raw()` 的——[crates/typst-macros/src/time.rs:31-34](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-macros/src/time.rs#L31-L34)：

```rs
let construct = match meta.span.as_ref() {
    Some(span) => quote! { with_span(#name, Some(#span.into_raw())) },
    None => quote! { new(#name) },
};
```

带 span 时生成 `Some(#span.into_raw())`，这里的 `#span` 就是你写的 `span = ...` 后面那个表达式（通常是函数的 `span: Span` 参数）。宏假设它是个 `Span`，于是调 `.into_raw()`。

而 `Span::into_raw()` 在 [crates/typst-syntax/src/span.rs:189-192](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L189-L192)，干的事就是把内部那个 `NonZeroU64` 取出来：

```rs
/// Extract the raw underlying number.
pub const fn into_raw(self) -> NonZeroU64 {
    self.0
}
```

对照之下，`timed!`（[lib.rs:36-39](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L36-L39)）**没有**补这步：

```rs
($name:expr, span = $span:expr $(,)?) => {{
    let __scope = $crate::TimingScope::with_span($name, Some($span));
    $body
}};
```

`$span` 被原样塞进 `Some(...)`，所以它必须已经是 `NonZeroU64`（或能被强转成它）。`timed!` 的文档示例 `span = Span::detached()`（[lib.rs:20-26](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-timing/src/lib.rs#L20-L26)）写在 `rs` 文档块里（不参与编译）；严格照抄会因类型不匹配无法通过编译，因为 `Span` 不会自动转成 `NonZeroU64`。这正好说明：**如果你手头是一个 `Span`，要么用 `#[time]` 让它替你 `into_raw()`，要么自己在 `timed!` 里写 `span = my_span.into_raw()`。**

两相对照，一表汇总：

| 维度 | `timed!`（声明式宏） | `#[time]`（过程宏属性） |
| --- | --- | --- |
| 定义位置 | `typst-timing/src/lib.rs` | `typst-macros/src/time.rs` |
| 宏系统 | `macro_rules!` | `#[proc_macro_attribute]` |
| 引用 crate | `$crate::TimingScope`（卫生） | `::typst_timing::TimingScope`（绝对路径） |
| 作用于 | 单个表达式 | 整个函数 |
| `span = ...` 期望 | 已是 `NonZeroU64`（直接塞 `Some(...)`） | 一个 `Span`（宏补 `.into_raw()`） |
| 名字默认值 | 必须显式传 `$name` | 缺省时取函数名 |
| 门控入口 | `with_span` / `new` | `with_span` / `new`（同一道门） |

最后一行是承上启下的关键：**不管走哪个入口、不管 span 怎么传，最终都汇入 `with_span` 这同一道门**（u3-l1 讲过的零成本门控）。

#### 4.3.4 代码实践

**实践目标：** 解释 `#[time]` 为什么在带 span 时生成 `Some(#span.into_raw())`，而 `timed!` 不生成。

**操作步骤：**

1. 重读 [time.rs:31-34](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-macros/src/time.rs#L31-L34) 的 `construct` 匹配。
2. 重读 [span.rs:189-192](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/span.rs#L189-L192) 的 `into_raw`。
3. 在脑子里给下面这个（示例代码）函数做 `#[time(span = span)]` 的展开：

   ```rs
   // 示例代码
   #[time(span = span)]
   fn fizz(span: Span, n: u64) -> u64 { n + 1 }
   ```

**需要观察的现象 / 预期结果：** 展开后函数体首行应为：

```rs
let __scope = ::typst_timing::TimingScope::with_span("fizz", Some(span.into_raw()));
```

- 名字 `"fizz"` 来自函数 ident（未写 `name = ...`）。
- `span.into_raw()` 把 `Span` 转成 `NonZeroU64`，正好喂给 `with_span` 的 `Option<NonZeroU64>`。
- 若改用 `timed!`，你得自己写 `timed!("fizz", span = span.into_raw(), n + 1)`——宏不会替你补 `into_raw()`。

**解释（为什么用 `into_raw()` 取裸数值）：** `typst-timing` 不能依赖 `typst-syntax`（否则形成 `syntax → timing → syntax` 的环），所以它的 API 只接受标准库的 `NonZeroU64`。`Span` 是 `typst-syntax` 的私有包装（内部就是一个 `NonZeroU64`），`into_raw()` 是把它「拆包」成裸数字的唯一官方出口。`#[time]` 作为过程宏，能在生成代码时自动补上这一步；`timed!` 作为纯文本模板，做不到「按类型自动加方法调用」，只能要求调用者自己传裸数值。

#### 4.3.5 小练习与答案

**练习 1：** 假如把 `with_span` 的签名改成接收 `Option<Span>`，会引发什么问题？

**参考答案：** `typst-timing` 就必须 `use typst_syntax::Span`，即在 `Cargo.toml` 里依赖 `typst-syntax`。而 `typst-syntax` 本来就依赖 `typst-timing`（要在解析器里埋点），于是形成 `typst-syntax → typst-timing → typst-syntax` 的循环依赖，cargo 拒绝编译。用 `NonZeroU64` 这类标准库类型做不透明句柄，正是为了切断这条潜在的环。

**练习 2：** 为什么选 `NonZeroU64` 而不是普通 `u64` 来做这个不透明句柄？

**参考答案：** 至少两个好处。其一，`NonZeroU64` 是 `#[repr(transparent)]` 的，且 `Option<NonZeroU64>` 与 `u64` 同大小（NICHE 优化），`Option<NonZeroU64>` 不比 `u64` 多占内存——正好让 `Event.span: Option<NonZeroU64>` 不引入额外判别位（见 u2-l1）。其二，`NonZero` 保证 `0` 永远不是合法 span 值，天然与 `Span` 内部「非零」的不变量吻合，`into_raw()` 不会产生 `0`。

---

## 5. 综合实践

把本讲的两个宏入口与 span 处理串起来，完成下面这个贯穿性小任务。

**任务：** 给同一个简单函数，分别用 `#[time]` 和 `timed!` 加计时，对比它们的展开产物与 span 处理。

**步骤：**

1. 选一段示例代码（非项目原有）：

   ```rs
   // 示例代码
   #[time(name = "foo")]
   fn foo(span: Span, x: u64) -> u64 {
       let step = timed!("inner", x.pow(2));
       step + 1
   }
   ```

2. **展开 `#[time]` 层：** 按 4.2 的规则，`#[time(name = "foo")]` 在 `foo` 函数体首行插入一条语句。写出展开后的 `foo`：
   ```rs
   fn foo(span: Span, x: u64) -> u64 {
       let __scope = ::typst_timing::TimingScope::new("foo");
       let step = timed!("inner", x.pow(2));
       step + 1
   }
   ```

3. **再展开内层的 `timed!`：** `timed!("inner", x.pow(2))` 套用 4.1 的「无 span」分支：
   ```rs
   let step = { let __scope = ::typst_timing::TimingScope::new("inner"); x.pow(2) };
   ```
   注意这里出现了**两个不同作用域的 `__scope`**——外层覆盖整个 `foo`（事件名 `"foo"`），内层只覆盖 `x.pow(2)`（事件名 `"inner"`）。它们互不冲突，因为内层 `__scope` 在自己的块作用域里。

4. **改造为带 span 版本：** 把外层 `#[time]` 换成 `#[time(name = "foo", span = span)]`，按 4.3 写出新增的首行：
   ```rs
   let __scope = ::typst_timing::TimingScope::with_span("foo", Some(span.into_raw()));
   ```
   再把内层 `timed!` 换成带 span 的 `timed!("inner", span = span.into_raw(), x.pow(2))`，指出：`#[time]` 替你写了 `into_raw()`，而 `timed!` 你得自己写。

**预期结果 / 需要观察的现象：**

- 两个宏都只「插入 `let __scope = ...;`」，不改变函数返回值。
- 嵌套调用会产生嵌套的 `TimingScope`，对应 Chrome Trace 里**嵌套的 B/E 事件对**（外层 `"foo"` 包住内层 `"inner"`）。
- 带 span 时，`#[time]` 走 `with_span(.., Some(span.into_raw()))`，`timed!` 要求你手动 `into_raw()`。

**运行验证：** 「待本地验证」——若你已读过 u2-l4 的 `export_json`，可在此基础上 `enable()` 后调用该函数，再把事件导出为 JSON，用 chrome://tracing 或 Perfetto 打开，确认看到一对嵌套的事件，且带 span 的事件 `args` 里出现 `(file, line)`。

## 6. 本讲小结

- `timed!` 是 `typst-timing` 内的 `macro_rules!` 声明式宏，有**两条匹配分支**：带 `span = ...` 展开为 `with_span($name, Some($span))`，不带展开为 `new($name)`；末尾放 `$body` 以原样返回表达式之值。
- `#[time]` 是 `typst-macros` 内的 `#[proc_macro_attribute]` 过程宏：解析可选的 `name`（字符串字面量）与 `span`（任意表达式），在函数体**下标 0 处插入**一条 `let __scope = ::typst_timing::TimingScope::...;`，不动函数签名与返回值。
- 路径写法不同源于宏系统不同：`timed!` 用 `$crate::`（声明式宏的跨 crate 卫生），`#[time]` 用 `::typst_timing::` 绝对路径（过程宏没有 `$crate`）。
- `name` 默认值也不同：`timed!` 必须显式传名字；`#[time]` 缺省时取函数 ident 作字符串。
- **span 处理是最大差异**：`timed!` 把 `$span` 直接塞进 `Some(...)`（要求已是 `NonZeroU64`）；`#[time]` 补一个 `.into_raw()`（接收 `Span`）。
- 用裸 `NonZeroU64` 而非 `Span` 是为了**切断循环依赖**——`typst-timing` 不能依赖 `typst-syntax`，否则 `typst-syntax → typst-timing → typst-syntax` 成环；但两个宏入口最终都汇入 `with_span` 这同一道门。

## 7. 下一步学习建议

- 下一讲 **u3-l3《WASM 支持与 WebAssembly 计时：WasmTimer》** 会转向另一个跨 crate 的设计问题：在浏览器里如何取时间。它会用到本讲提到的 `THREAD_DATA`，并解释 `#[cfg(target_arch = "wasm32")]` 这层条件编译网关。
- 若想巩固「过程宏改写语法树」的直觉，可读 `typst-macros` 里更复杂的属性宏：`#[func]`（[crates/typst-macros/src/func.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-macros/src/func.rs)）、`#[elem]`，它们同样用 `item.block.stmts.insert(...)` 改写函数体。
- 若想验证本讲的展开结论，推荐在仓库内任一已依赖 `typst-timing` 的 crate 上跑 `cargo expand`，直接对照 `timed!` 与 `#[time]` 的产物。
- 综合集成视角可留到 **u3-l4《集成实践：typst-kit Timer 与端到端计时导出》**，那里会把本讲的「埋点」和 u2-l4 的「导出」用 `typst-kit` 的 `Timer` 串成一条完整链路。
