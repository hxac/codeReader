# 代码生成架构总览与设计取舍

> 本讲是 `typst-macros` 学习手册的收官篇。前面十四讲我们逐一拆解了七个过程宏的内部细节，本讲退后一步，把它们统一到同一张架构图下，并回答一个根本问题：**为什么 Typst 选择「编译期代码生成 + 运行时 trait 契约」，而不是运行时反射注册？**

## 1. 本讲目标

学完本讲，你应当能够：

1. 用一句话概括七个宏共享的「解析中间结构 → `quote!` 生成」流水线，并指出每个宏在哪一步产生差异。
2. 解释 `foundations` 路径简写背后「宏在编译期产出、运行时 `typst_library::foundations` 消费」的契约关系。
3. 说出 `DefSite` / `def_site_key` 的命名规则（类型、函数、字段三种），以及它为何用「文件内唯一键」而非行号来定位定义点。
4. 理解 `LazyLock` 在「初始化顺序」上的作用，以及 `static DATA` 与 `LazyLock` 的分工。
5. 能评估「重代码生成」架构的至少两条优点与一条代价。

## 2. 前置知识

本讲假设你已经读过 u1～u4 的前序讲义，熟悉每个宏的解析与生成细节。这里只补两个**跨宏**的、之前没有单独展开的概念：

- **trait 契约（trait contract）**：过程宏本身不实现 Typst 运行时的任何行为，它只是「替你写出实现某个 trait 的代码」。例如 `#[func]` 最终产出 `impl NativeFunc`、`#[elem]` 产出 `unsafe impl NativeElement`。运行时（`typst_library::foundations`）只认这些 trait。宏与运行时之间靠「trait 签名」对接，这层对接就叫契约。
- **代码生成 vs 反射**：有些语言（如 Python 装饰器、Java 注解 + 运行时反射）在程序运行时扫描元数据来注册函数/类型；Rust 的过程宏则不同——它在**编译期**就把「注册代码」以普通 Rust 源码的形式生成出来，编译后与手写代码无异，运行时没有任何反射开销。本讲要评估的正是这两种路线的取舍。

## 3. 本讲源码地图

本讲横跨四个文件，关注点与之前各讲不同——不再追单个宏的逻辑，而是看**跨宏复用**的骨架：

| 文件 | 本讲关注的内容 |
| --- | --- |
| [`src/lib.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/lib.rs) | 七个入口共享的「`parse_macro_input!` → 子模块 → `to_compile_error`」模板、`BoundaryStream` 边界 |
| [`src/util.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/util.rs) | `bail!` 宏、`foundations` 路径简写、`validate_attrs` |
| [`src/func.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs) | `parse`/`create` 骨架、`def_site_key`（函数名）、`LazyLock`、影子类型 |
| [`src/elem.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs) | `def_site_key`（元素名 + 字段名）、`NativeElement` 与 vtable 注册 |
| [`src/ty.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/ty.rs) | `def_site_key`（类型名）、`LazyLock`、`NativeType` 契约 |
| [`src/scope.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/scope.rs) | `NativeScope` 契约 |
| `crates/typst-utils/src/lib.rs` | `DefSite` 结构体定义与设计注释 |

> 注：前四个是讲义规格指定的关键源码，后三个用于补全契约链条（`ty.rs`、`scope.rs` 属同一 crate，`typst-utils` 是被引用的契约对端）。

## 4. 核心概念与源码讲解

### 4.1 各宏共用的 parse / create 模式

#### 4.1.1 概念说明

七个宏虽然形态各异（属性宏 / 函数式宏 / 派生宏），但**内部实现**都遵循同一个两段式骨架：

```
fn xxx(stream, item) -> Result<TokenStream> {
    let 中间结构 = parse(stream, item)?;   // 解析阶段：吃进 syn 语法树，吐出中间结构
    Ok(create(&中间结构, item))             // 生成阶段：吃进中间结构，用 quote! 吐出 TokenStream
}
```

- **`parse`**：负责「读懂用户写了什么」。它把零散的 `syn` 节点（属性参数、函数签名、字段列表等）归拢成一个强类型的**中间结构**（如 `func.rs` 的 `Func`、`elem.rs` 的 `Elem`、`ty.rs` 的 `Type`）。这一步只做「收集与校验」，不生成任何代码。
- **`create`**：负责「写出对应的 Rust 代码」。它读取中间结构，用 `quote!` 拼出 `impl NativeXxx { ... }`、包装闭包、`static DATA` 等，返回最终 `TokenStream`。

把「理解输入」与「生成输出」拆成两步的好处：校验错误集中在 `parse`、生成分支集中在 `create`，两者可以独立演进。这也是为什么前面每一讲都能分成「解析篇」与「生成篇」。

#### 4.1.2 核心流程

下面这张图把七个宏统一进同一条流水线（方括号里是该宏的「中间结构」类型）：

```
用户源码 (TokenStream)
   │  parse_macro_input!  ← lib.rs 入口处结构化为 syn 节点
   ▼
子模块入口 ──► parse() ──► 中间结构 [Func / Elem / Type / CastInput / Meta …]
   │                          │  （这里做属性取出、命名推导、互斥校验）
   │                          ▼
   │                       create() ──► quote! 拼装
   │                          │
   ▼                          ▼
出错? ──► bail! ──► syn::Error ──► to_compile_error  (绝不 panic)
顺利? ──► 返回 TokenStream（含改写后的原项 + 生成的 impl）
```

注意 `#[time]` 是这条流水线的最简特例：它的 `parse`/`create` 几乎只做一件事（在函数体开头注入一行 RAII 守卫），但它仍然遵守同一骨架——这是你阅读 u2-l1 时已经看到的。

#### 4.1.3 源码精读

七个入口在 [`src/lib.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/lib.rs) 里几乎是一个模子刻出来的。以 `#[func]` 为例：

[`src/lib.rs:103-109`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/lib.rs#L103-L109) — 入口先 `parse_macro_input!` 把 `item` 解析成 `syn::ItemFn`，再交给 `func::func`，出错回传编译错误：

```rust
#[proc_macro_attribute]
pub fn func(stream: BoundaryStream, item: BoundaryStream) -> BoundaryStream {
    let item = syn::parse_macro_input!(item as syn::ItemFn);
    func::func(stream.into(), &item)
        .unwrap_or_else(|err| err.to_compile_error())
        .into()
}
```

可以看到入口本身**不做事**，只做三件事：解析、转发、兜底错误。真正的工作在子模块里。`func::func` 就是上面骨架的具现：

[`src/func.rs:14-17`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L14-L17) — `parse` 产 `Func`，`create` 消费 `Func`：

```rust
pub fn func(stream: TokenStream, item: &syn::ItemFn) -> Result<TokenStream> {
    let func = parse(stream, item)?;
    Ok(create(&func, item))
}
```

对照一下 `#[ty]`（[`src/ty.rs:79-136`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/ty.rs#L79-L136) 的 `create`）与 `#[elem]`（`elem::elem` 同样先 `parse` 后 `create`），你会发现它们只是「中间结构不同、生成的 impl 不同」，骨架完全一致。这就是本讲要建立的第一层统一视图。

> **关键事实**：[`src/lib.rs:14`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/lib.rs#L14) 把编译器的 `proc_macro::TokenStream` 起别名为 `BoundaryStream`（边界流），并在入口用 `.into()` 转成 `proc_macro2::TokenStream`。这个命名点明了「编译器类型 ↔ proc-macro2 类型」的转换边界，是整条流水线的起点。

#### 4.1.4 代码实践

**实践目标**：亲手验证「七个入口共享同一模板」这个论断。

**操作步骤**：

1. 打开 [`src/lib.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/lib.rs)，定位七个 `pub fn`：`func`、`ty`、`elem`、`scope`、`cast`、`derive_cast`、`time`。
2. 对每个入口，记下两点：(a) `parse_macro_input!` 把 `item` 解析成哪种 `syn` 类型；(b) 它调用的子模块函数名。
3. 数一数：有几个入口的函数体最后一行是 `.unwrap_or_else(|err| err.to_compile_error()).into()`？

**需要观察的现象**：除了 `cast`（函数式宏，只有一个 `stream` 参数、没有 `item`）和 `derive_cast`（派生宏，参数名是 `item` 而非 `stream`/`item` 双参）外，其余五个属性宏的双参签名与三行模板逐字一致。

**预期结果**：你会得到一张表：

| 宏 | `item` 解析为 | 子模块调用 |
| --- | --- | --- |
| `func` | `syn::ItemFn` | `func::func` |
| `ty` | `syn::Item` | `ty::ty` |
| `elem` | `syn::ItemStruct` | `elem::elem` |
| `scope` | `syn::Item` | `scope::scope` |
| `cast` | （无 item）`cast::cast(stream)` | `cast::cast` |
| `derive_cast` | `DeriveInput` | `cast::derive_cast` |
| `time` | `syn::ItemFn` | `time::time` |

「三行模板 + 七次复制」正是这一架构可维护性的根基。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `cast` 和 `derive_cast` 的入口签名与其他五个不同？

**参考答案**：因为它们是不同**种类**的过程宏。`cast` 是函数式宏（`#[proc_macro]`），只接收宏调用体这一个 token 流；`derive_cast` 是派生宏（`#[proc_macro_derive]`），接收被派生的类型项；其余五个是属性宏（`#[proc_macro_attribute]`），接收「属性参数流 + 被装饰项」两个流。签名的参数个数直接由过程宏种类决定（详见 u1-l2）。

**练习 2**：如果把 `parse` 和 `create` 合并成一个函数，会损失什么？

**参考答案**：会损失「关注点分离」。合并后，属性校验错误（如「required 与 synthesized 互斥」）与代码生成分支会纠缠在一起；而且 `parse` 产出的中间结构（`Func`/`Elem` 等）是强类型快照，把它独立出来后，`create` 可以像操作普通数据一样遍历字段、拼装代码，逻辑更清晰、更易测试。

---

### 4.2 foundations 简写与 `::typst_library::foundations` 运行时契约

#### 4.2.1 概念说明

宏生成的代码里，到处是 `#foundations::NativeFunc`、`#foundations::NativeTypeData`、`#foundations::cast!` 这样的路径。这里的 `foundations` **不是**某个普通变量，而是 `util.rs` 里定义的一个「路径简写」类型——它在被 `quote!` 展开时，会输出固定的绝对路径 `::typst_library::foundations`。

这条路径揭示了一个重要的架构事实：**宏（`typst-macros`）与运行时（`typst-library`）是两个独立 crate**。宏在编译期生成代码，生成的代码里写死了对 `::typst_library::foundations` 下若干 trait 的实现；运行时则定义这些 trait 并据此调度。二者通过「trait 签名」对接，这就是「契约」一词的由来。

#### 4.2.2 核心流程

契约链条可以这样理解：

```
   typst-macros (编译期)                    typst-library (运行时)
   ─────────────────────                    ──────────────────────
   #[func] ──► 生成  impl NativeFunc  ──┐    pub trait NativeFunc { fn data() -> &'static NativeFuncData }
   #[ty]   ──► 生成  impl NativeType  ──┤    pub trait NativeType { const NAME; fn data() }
   #[elem] ──► 生成  unsafe impl                          ▲
                         NativeElement ─┤    pub trait NativeElement { const ELEM; ... }
   #[scope]──► 生成  impl NativeScope ─┤    pub trait NativeScope { fn constructor(); fn scope() }
   cast!   ──► 生成  impl Reflect /    ┘    pub trait Reflect / FromValue / IntoValue
                     FromValue / IntoValue
```

宏「按 trait 签名产出实现」，运行时「按 trait 签名调用」。任何一方单方面改签名都会导致编译失败——这正是强类型契约的好处。

#### 4.2.3 源码精读

`foundations` 简写的定义只有几行，却出现在几乎所有生成代码里：

[`src/util.rs:217-225`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/util.rs#L217-L225) — 一个实现了 `ToTokens` 的零大小类型，`quote!` 时输出绝对路径：

```rust
/// Shorthand for `::typst_library::foundations`.
#[expect(non_camel_case_types)]
pub struct foundations;

impl quote::ToTokens for foundations {
    fn to_tokens(&self, tokens: &mut TokenStream) {
        quote! { ::typst_library::foundations }.to_tokens(tokens);
    }
}
```

于是在 `func.rs` 里只要写 `#foundations::NativeFunc`，展开后就是 `::typst_library::foundations::NativeFunc`。例如 `create` 生成函数影子类型时就用它：

[`src/func.rs:261-266`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L261-L266) — 生成的 `impl` 直接对接到运行时 trait：

```rust
impl #foundations::NativeFunc for #ident {
    fn data() -> &'static #foundations::NativeFuncData {
        static DATA: #foundations::NativeFuncData = #data;
        &DATA
    }
}
```

`#[scope]` 则对接 `NativeScope` 契约，产出 `constructor()` 与 `scope()` 两个方法：

[`src/scope.rs:103-114`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/scope.rs#L103-L114) — `NativeScope` 的两个方法签名正是运行时约定的：

```rust
impl #foundations::NativeScope for #self_ty {
    fn constructor() -> ::std::option::Option<&'static #foundations::NativeFuncData> {
        #constructor
    }
    fn scope() -> #foundations::Scope {
        let mut scope = #foundations::Scope::deduplicating();
        #(#definitions;)*
        scope
    }
}
```

注意路径前缀全部是 `::typst_library::foundations`（绝对路径），这保证了无论用户在哪个 crate、哪个模块里写 `#[func]`，生成的代码都能正确找到运行时类型，不会被用户本地的模块树遮蔽。

#### 4.2.4 代码实践

**实践目标**：体会「路径简写 = 可维护的绝对路径」。

**操作步骤**：

1. 在 `src/func.rs`、`src/ty.rs`、`src/elem.rs` 中搜索 `#foundations::`，统计它总共被用来引用了多少个不同的运行时符号（trait、结构体、宏等）。
2. 假设运行时把 `foundations` 模块重命名为 `base`，思考需要改几处。

**需要观察的现象**：`foundations` 简写出现在几十处，但它们指向的运行时符号五花八门——`NativeFunc`、`NativeTypeData`、`Element`、`ContentVtable`、`cast!`、`Scope`、`Since`、`Packed`……

**预期结果**：正因为所有这些引用都经过 `foundations` 这一个简写，运行时模块一旦改名，只需改 [`src/util.rs:223`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/util.rs#L223) 一处即可——这是契约层「集中管理」的直接收益。

#### 4.2.5 小练习与答案

**练习 1**：为什么生成的路径必须以 `::`（绝对路径）开头，而不是写成 `typst_library::foundations`？

**参考答案**：相对路径会被用户的 `use` 语句或本地模块遮蔽。如果用户恰好在自己的模块里定义了 `mod typst_library`，相对路径就会解析到错误的位置。绝对路径 `::typst_library::foundations` 从 crate 根开始解析，是唯一确定的，杜绝了命名冲突。

**练习 2**：`foundations` 是一个 `struct`，但从不被实例化。这种「用类型来承载 `ToTokens`」的技巧相比直接写一个 `macro_rules!` 有什么好处？

**参考答案**：好处是它可以在 `quote!` 里像值一样被插值（`#foundations::NativeFunc`），语法上更自然、可读性更好；同时它是强类型的——编译器会检查 `foundations` 确实实现了 `ToTokens`。而 `macro_rules!` 无法在 `quote!` 的 `#...` 插值位置直接展开。

---

### 4.3 `DefSite` / `def_site_key`：编译期的精确定位

#### 4.3.1 概念说明

每个被宏注册的「定义」（一个函数、一个类型、一个元素、一个字段）都携带一个 `DefSite`，描述「它在哪个源文件的哪个位置」。`DefSite` 不是行号，而是 **(文件路径, 文件内唯一键)** 的二元组。这个键（`def_site_key`）由宏在编译期生成，命名规则因「定义的种类」而异：

- **类型**：就是类型名，如 `Str`。
- **顶层函数**：就是函数名，如 `double`。
- **作用域内的方法**：`父类型::方法名`，如 `Str::len`。
- **元素字段**：`元素名::字段名`，如 `HeadingElem::level`。

这套机制的目的是让 IDE、文档工具、错误诊断能在**生成的代码**里反向定位回**用户原始的定义点**。

#### 4.3.2 核心流程

`DefSite` 的设计与一条关键注释紧密相关。运行时 [`crates/typst-utils/src/lib.rs:459-466`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-utils/src/lib.rs#L459-L466) 这样解释它：

```
定义点的描述。
不含具体行号——因为宏展开会使 #[scope] impl 内的 #[func] 项
拿到整个 impl 的行号，没意义。改用一个「文件内唯一的 key」来定位，
key 里还可以带上语义父类型的 ident。
这额外的好处是：即便源码被编辑（热重载），也能可靠地找到定义点。
```

转化为流程：

```
宏在编译期 ──► 拼出 def_site_key 字符串（按命名规则）
            │      + file!() 拿到当前文件路径
            ▼
   DefSite { path, key } 嵌进 NativeXxxData 字面量
            │
            ▼
运行时/IDE ──► 用 (path, key) 反查源码，定位定义点（不依赖行号，抗编辑）
```

#### 4.3.3 源码精读

`DefSite` 结构体本身定义在运行时侧：

[`crates/typst-utils/src/lib.rs:467-476`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-utils/src/lib.rs#L467-L476)：

```rust
pub struct DefSite {
    /// 由 file!() 得到的文件路径（路径分隔符跨平台可能不同）。
    pub path: &'static str,
    /// 一个用于标识定义的 key，可在文件内据此查找定义。
    pub key: &'static str,
}
```

宏侧的命名规则体现在三处。先看**函数**（`func.rs`）——这是「命名规则分支」最完整的地方：

[`src/func.rs:309-315`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L309-L315) — 有父类型就用 `父::函数`，否则用函数名：

```rust
let def_site_key = if let Some(syn::Type::Path(path)) = parent
    && let Some(parent) = path.path.get_ident()
{
    format!("{parent}::{ident}")
} else {
    ident.to_string()
};
```

函数的**参数**则在此基础上再嵌一层，把参数 ident 追加到父 key 之后：

[`src/func.rs:430`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L430) — 参数的 key 是「父 key::参数名」：

```rust
let def_site_key = format!("{parent_def_site_key}::{ident}");
```

最终写入 `DefSite`：

[`src/func.rs:350`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L350) 与 [`src/func.rs:450`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L450)：

```rust
def_site: Some(::typst_utils::DefSite { path: file!(), key: #def_site_key }),
```

再看**元素**与**字段**（`elem.rs`）。元素本身用元素名：

[`src/elem.rs:389`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L389) — 元素的 key 就是 ident：

```rust
let def_site_key = ident.to_string();
```

而元素**字段**用 `元素::字段`（与函数作用域内方法的规则一致）：

[`src/elem.rs:472-475`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L472-L475)：

```rust
let def_site_key = format!("{elem_ident}::{ident}");
let def_site = quote! {
    ::typst_utils::DefSite { path: file!(), key: #def_site_key }
};
```

最后看**类型**（`ty.rs`），最简单——直接用类型名：

[`src/ty.rs:82`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/ty.rs#L82)：

```rust
let def_site_key = ident.to_string();
```

于是三类定义的命名规则可以归一张表：

| 定义种类 | `def_site_key` 规则 | 例 |
| --- | --- | --- |
| 类型（`#[ty]`） | `类型名` | `Str` |
| 顶层函数（`#[func]` 无 parent） | `函数名` | `double` |
| 作用域内方法（`#[func]` 有 parent） | `父类型::方法名` | `Str::len` |
| 方法参数 | `父类型::方法名::参数名` | `Str::len::default` |
| 元素（`#[elem]`） | `元素名` | `HeadingElem` |
| 元素字段 | `元素名::字段名` | `HeadingElem::level` |

#### 4.3.4 代码实践

**实践目标**：对比 `#[func]` 与 `#[elem]` 两类 `def_site_key` 的生成方式。

**操作步骤**：

1. 阅读 [`src/func.rs:309-315`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L309-L315) 与 [`src/elem.rs:389`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L389)、[`src/elem.rs:472`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L472)。
2. 写一份说明，回答：函数的 key 何时带父类型、何时不带？元素的 key 与元素字段的 key 又有何区别？为何元素字段要用 `Elem::field` 这种带父类型的形式？

**需要观察的现象**：`#[func]` 的 key 生成**有分支**（看 `parent` 是否存在），而 `#[elem]` 元素本身的 key **无分支**（恒为 ident）——因为元素永远是「顶层类型」，没有「嵌套在某父类型内」的概念；但元素**字段**则总是带元素名前缀，与「作用域内方法」规则一致。

**预期结果**：你应能解释——函数可能出现在顶层（无 parent）或 `#[scope]` impl 内（有 parent），所以需要分支；元素字段本身依附于元素，必须带元素名前缀才能在文件内唯一定位。这正是本讲综合实践的一部分。

> 待本地验证：若你本地编译 typst 并用 IDE 跳转到一个 native 函数，可以观察 `DefSite` 如何被用来反查源码位置。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `DefSite` 用 `key`（字符串）而不是行号？

**参考答案**：因为宏展开会扭曲行号——一个写在 `#[scope]` impl 里的 `#[func]`，展开后其调用点拿到的是整个 impl 块的行号，无法定位到具体函数。改用「文件内唯一的语义 key」（如 `Str::len`）后，既能在文件里唯一查找，又对源码编辑具有鲁棒性（行号会因编辑而漂移，但 `Str::len` 这种语义 key 不会），这对热重载尤其重要。

**练习 2**：如果两个不同的元素都叫 `level` 字段，`def_site_key` 会冲突吗？

**参考答案**：不会。因为字段 key 是 `元素名::字段名`，如 `HeadingElem::level` 与 `ParElem::level` 是两个不同的 key。前缀的元素名保证了文件内的唯一性。

---

### 4.4 `LazyLock` 延迟初始化与 `static DATA` 的分工

#### 4.4.1 概念说明

宏生成的「数据字面量」里有两类静态项，分工不同：

- **`static DATA: NativeXxxData = …;`**：把整个数据结构作为**常量静态量**存下来，满足 `data() -> &'static …` 对 `&'static` 返回值的要求。它的内容在编译期就完全确定（纯数据）。
- **`::std::sync::LazyLock::new(…)`**：包裹那些**不能在编译期求值**、或**存在循环引用**的字段，延迟到首次访问时再初始化。

为什么要延迟？因为类型之间会**互相引用**：`Str` 类型的 `scope` 里可能引用某个函数，而那个函数的数据又引用回 `Str`。如果在程序启动时就急切求值，就会撞上「初始化顺序」问题（谁的 `data()` 先被调用？）。`LazyLock` 把求值推迟到首次访问，天然规避了顺序依赖。

#### 4.4.2 核心流程

```
data() -> &'static NativeXxxData
   │
   ├─ static DATA: NativeXxxData {        ← 编译期常量（name/title/docs/def_site…）
   │     scope: LazyLock::new(|| …),      ← 首次访问才求值（可能引用别的类型）
   │     params: LazyLock::new(|| …),     ← 首次访问才求值
   │     returns: LazyLock::new(|| …),    ← 首次访问才求值
   │  }
   │
   └─ &DATA                               ← 返回静态引用
```

`LazyLock` 提供 `Deref`，访问 `.scope` 等字段时透明地触发首次初始化（线程安全），之后直接返回缓存值。

#### 4.4.3 源码精读

`#[func]` 的数据字面量最能体现这种分工：

[`src/func.rs:343-357`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L343-L357) — 纯数据走常量，可能引用别的类型的走 `LazyLock`：

```rust
#foundations::NativeFuncData {
    function: #foundations::NativeFuncPtr(&#closure),
    name: #name,
    title: #title,
    since: #since,
    docs: #docs,
    def_site: Some(::typst_utils::DefSite { path: file!(), key: #def_site_key }),
    keywords: &[#(#keywords),*],
    contextual: #contextual,
    scope: ::std::sync::LazyLock::new(&|| #scope),
    params: ::std::sync::LazyLock::new(&|| ::std::vec![#(#params),*]),
    returns:  ::std::sync::LazyLock::new(&|| <#returns as #foundations::Reflect>::output()),
}
```

可以看到 `name`/`title`/`docs`/`def_site` 等都是直接的字面量，而 `scope`/`params`/`returns` 三个被 `LazyLock::new` 包裹。尤其 `scope` 的内容可能是 `<#ident as NativeScope>::scope()`——这会引用到其他函数/类型的数据，必须延迟。

外层用局部 `static DATA` 来承载这个字面量，以满足 `&'static`：

[`src/func.rs:262-265`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L262-L265)：

```rust
fn data() -> &'static #foundations::NativeFuncData {
    static DATA: #foundations::NativeFuncData = #data;
    &DATA
}
```

`#[ty]` 也是同样的模式——`constructor` 与 `scope` 走 `LazyLock`：

[`src/ty.rs:115-118`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/ty.rs#L115-L118)：

```rust
def_site: ::typst_utils::DefSite { path: file!(), key: #def_site_key },
keywords: &[#(#keywords),*],
constructor: ::std::sync::LazyLock::new(|| #constructor),
scope: ::std::sync::LazyLock::new(|| #scope),
```

元素则更特殊——它不仅用 `LazyLock`，还用运行时提供的 `LazyElementStore` 来容纳 vtable 的延迟存储：

[`src/elem.rs:441-463`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L441-L463) — 元素的 `ELEM` 常量通过 `from_vtable` 注册，内部 `STORE` 与 `VTABLE` 都是 static：

```rust
const ELEM: #foundations::Element = #foundations::Element::from_vtable({
    static STORE: #foundations::LazyElementStore
        = #foundations::LazyElementStore::new();
    static VTABLE: #foundations::ContentVtable = …;
    &VTABLE
});
```

#### 4.4.4 代码实践

**实践目标**：区分哪些字段「必须」用 `LazyLock`，哪些「可以」用常量。

**操作步骤**：

1. 看 [`src/func.rs:343-357`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L343-L357) 的 `NativeFuncData` 字面量。
2. 逐字段判断：它的值是编译期已知的字面量（如字符串、bool），还是需要调用某个函数 / 引用别的类型（如 `<T as Reflect>::output()`、`<#ident as NativeScope>::scope()`）？
3. 思考：如果把 `returns` 字段也改成常量 `returns: <#returns as Reflect>::output()`，会怎样？

**需要观察的现象**：`returns` 的值是 `<#returns as #foundations::Reflect>::output()`——这是一个** trait 方法的调用结果**，不是编译期常量，无法直接放进 `static`（除非该 trait 方法是 `const`）。

**预期结果**：你会得出结论——`name`/`title`/`docs`/`contextual` 这类是字面量，可直接当常量；`scope`/`params`/`returns` 这类依赖运行时函数或跨类型引用，必须 `LazyLock`。如果把 `returns` 改成直接常量，编译器会报「不能在常量上下文中调用非 const 函数」之类错误。

> 待本地验证：可尝试在本地 fork 里把某个 `LazyLock::new` 去掉，观察编译报错，从而印证上述判断。

#### 4.4.5 小练习与答案

**练习 1**：为什么不能把 `data()` 直接写成 `fn data() -> &'static NativeFuncData { &NativeFuncData { … } }`（即不要 `static DATA`）？

**参考答案**：因为临时字面量的引用是局部的，生命周期不足以满足 `&'static`。必须把它绑定到一个 `static` 项上，其地址才会存在于整个程序生命周期内。`static DATA: … = …; &DATA` 是把字面量「固化」为静态量的标准写法。

**练习 2**：`LazyLock` 相比「启动时用一个 `init()` 函数填充全局变量」有什么优势？

**参考答案**：`LazyLock` 是**线程安全**且**按需**的——首次访问时自动初始化，无需显式调用 `init`，也不用操心多个类型初始化的先后顺序（这正是 Typst 类型间互相引用时最需要的）。而全局变量 + `init()` 需要人工安排顺序，容易在循环引用时死锁或访问未初始化数据。

---

### 4.5 `to_compile_error`：绝不 panic 的错误回传

#### 4.5.1 概念说明

过程宏的出错处理有一条铁律：**绝不 `panic!`**。如果宏 panic，编译器会向用户报一条晦涩的「internal compiler error」，体验极差。正确做法是把错误信息包装成一段 `compile_error!` 的 token 流返回给编译器——这样错误会像普通编译错误一样，带上下文、指向源码位置展示给用户。

`typst-macros` 的统一模式是：子模块返回 `syn::Result`（即 `Result<T, syn::Error>`），入口用 `.unwrap_or_else(|err| err.to_compile_error())` 把 `Err` 转成编译错误 token 流。`syn::Error` 携带 `Span`，所以错误能精确定位到出错的属性或参数。

#### 4.5.2 核心流程

```
解析/校验中发现问题
        │
        ▼
   bail!(item, "…")          ← util.rs 定义的宏，造一个带 span 的 syn::Error 并 return Err
        │
        ▼
   Result::Err(syn::Error)   ← 一路向上传递
        │
        ▼
   入口 .unwrap_or_else(|err| err.to_compile_error())
        │
        ▼
   TokenStream (含 compile_error!)  ← 当作宏的「展开结果」返回给编译器
        │
        ▼
   编译器向用户展示一条带定位的错误
```

#### 4.5.3 源码精读

`bail!` 宏定义在 `util.rs`，由 `#[macro_use]` 暴露给所有子模块：

[`src/util.rs:9-23`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/util.rs#L9-L23) — 两种形态：`callsite` 用调用点 span，普通形式用被装饰项的 span：

```rust
macro_rules! bail {
    (callsite, $($tts:tt)*) => {
        return Err(syn::Error::new(
            proc_macro2::Span::call_site(),
            format!("typst: {}", format!($($tts)*))
        ))
    };
    ($item:expr, $($tts:tt)*) => {
        return Err(syn::Error::new_spanned(
            &$item,
            format!("typst: {}", format!($($tts)*))
        ))
    };
}
```

错误信息都以 `typst:` 前缀，便于用户辨认这是 Typst 宏报的错。配合校验函数（如 `validate_attrs`）：

[`src/util.rs:94-102`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/util.rs#L94-L102) — 对无法识别的属性报错：

```rust
pub fn validate_attrs(attrs: &[syn::Attribute]) -> Result<()> {
    for attr in attrs {
        if !attr.path().is_ident("doc") && !attr.path().is_ident("derive") {
            let ident = attr.path().get_ident().unwrap();
            bail!(ident, "unrecognized attribute: {ident}");
        }
    }
    Ok(())
}
```

入口的兜底则在 `lib.rs` 的七处都一样，例如：

[`src/lib.rs:106-108`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/lib.rs#L106-L108)：

```rust
func::func(stream.into(), &item)
    .unwrap_or_else(|err| err.to_compile_error())
    .into()
```

`to_compile_error()` 是 `syn::Error` 的方法，它生成形如 `compile_error!("typst: …")` 的 token 流。这段流被当作宏的展开结果返回，编译器看到 `compile_error!` 就会报错——但**不会** panic，也不影响其他代码的编译。

#### 4.5.4 代码实践

**实践目标**：体会「带 span 的错误」对用户体验的价值。

**操作步骤**：

1. 在 `src/elem.rs` 中搜索 `bail!`，找几处校验（例如「required 与 synthesized 互斥」之类，见 u4-l1）。
2. 思考：如果不报 `bail!` 而是 `panic!("required + synthesized")`，用户编译时会看到什么？
3. （源码阅读型）追踪一条错误路径：`parse_field` 里某个 `bail!` → 返回 `Err` → `elem::elem` 返回 `Err` → [`src/lib.rs:216`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/lib.rs#L216) 的 `.unwrap_or_else(|err| err.to_compile_error())` → 编译器。

**需要观察的现象**：`bail!` 的第二个分支用 `new_spanned(&$item, …)`，把错误**锚定**到具体的 AST 节点上。这意味着用户写错某个字段属性时，编译器会用波浪线标出**那个字段**，而不是整个元素或整个文件。

**预期结果**：你能说清「panic → ICE（internal compiler error，定位差）」与「to_compile_error → 带定位的普通编译错误」的差别，以及为何后者是过程宏的唯一正解。

> 待本地验证：可在一个测试项目里故意写一个非法的 `#[elem]` 字段组合（如 `#[required] #[synthesized]`），编译并观察错误输出的定位精度。

#### 4.5.5 小练习与答案

**练习 1**：为什么入口写成 `.unwrap_or_else(|err| err.to_compile_error())` 而不是 `.unwrap_or(quote!{})`（出错就返回空流）？

**参考答案**：返回空流会让宏「悄悄成功」，用户得不到任何错误提示，后续还可能引发莫名其妙的「找不到该函数/类型」错误，极难排查。返回 `to_compile_error()` 才能把真正的错误原因、精确位置告诉用户，符合过程宏的错误处理规范。

**练习 2**：`bail!` 里的错误信息都加了 `typst:` 前缀。这是为什么？

**参考答案**：因为这些宏会被各类用户代码触发，加 `typst:` 前缀能帮助用户立刻识别「这是 Typst 的宏在抱怨」，而不是 Rust 编译器或其它库报的错，缩小排查范围。

---

## 5. 综合实践

本任务把第 4.3 节的 `def_site_key` 对比与本讲对「代码生成 vs 反射」的评估串起来，作为整本手册的收官练习。

### 任务一：对比 `#[func]` 与 `#[elem]` 的 `def_site_key` 生成方式

写一份简短说明（文字 + 示例），覆盖以下要点：

1. **`#[func]`**：阅读 [`src/func.rs:309-315`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L309-L315)。说明它**有分支**：无 parent 时 key = 函数名（如 `double`），有 parent 时 key = `父类型::函数名`（如 `Str::len`）。再说明其参数 key 在此基础上再嵌一层（[`src/func.rs:430`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/func.rs#L430)），形如 `Str::len::default`。
2. **`#[elem]`**：阅读 [`src/elem.rs:389`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L389)（元素 key = 元素名，如 `HeadingElem`）与 [`src/elem.rs:472`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L472)（字段 key = `元素名::字段名`，如 `HeadingElem::level`）。
3. **对比结论**：函数 key 之所以需要分支，是因为函数既可能出现在顶层、也可能出现在 `#[scope]` impl 内；而元素永远是顶层类型（key 无分支），但元素**字段**总是依附于元素，必须带元素名前缀，这与「作用域内方法」规则本质相同。

### 任务二：评估「编译期代码生成 vs 运行时反射注册」

结合本讲全部内容，写一份评估，要求**至少两条优点**与**一条代价**。可参考以下角度（需结合本讲源码给出依据，不要空泛）：

**优点方向（任选 ≥2）**：

- **零运行时反射开销**：所有「注册」在编译期就固化成 `static DATA` / `VTABLE`，运行时是纯指针/数据访问（如 `Element::from_vtable`，见 [`src/elem.rs:441`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/elem.rs#L441)）。
- **强类型契约**：宏与运行时靠 trait 签名（`NativeFunc`/`NativeType`/`NativeElement`/`NativeScope`）对接，签名不匹配会在编译期暴露，而非拖到运行时。
- **编译期校验**：`bail!` 在编译期就把非法字段组合（如 required + synthesized）挡掉，用户在写代码时就能收到错误（4.5 节）。
- **精确定位**：`DefSite` 让 IDE/诊断能反向跳回定义点，提升开发体验（4.3 节）。

**代价方向（任选 ≥1）**：

- **宏复杂度高、可读性差**：`#[elem]` 一个宏就要处理字段三态、vtable 分支、能力系统，生成代码量巨大，调试过程宏本身的展开很痛苦。
- **编译时间**：大量 `quote!` 拼装与泛型单态化（如每个 settable 字段都是一个唯一的 const generic 类型）会增加编译开销。
- **错误信息受限于 `syn`**：宏报错必须走 `to_compile_error`，有时定位不如手写代码精确（这也是 `DefSite` 要绕开行号的原因）。

把两条优点与一条代价写成一段连贯的论述，每条都引用本讲某个具体源码位置作为证据。

## 6. 本讲小结

- 七个宏统一在「`parse`（解析成中间结构）→ `create`（`quote!` 生成）」的同一骨架下，差异只在于中间结构的形状与生成的 trait impl；入口 [`src/lib.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-macros/src/lib.rs) 共享「解析 → 转发 → `to_compile_error` 兜底」三行模板。
- `foundations` 是 `util.rs` 定义的路径简写，展开为绝对路径 `::typst_library::foundations`，承载「宏在编译期产出、运行时消费」的 trait 契约（`NativeFunc`/`NativeType`/`NativeElement`/`NativeScope`）。
- `DefSite { path, key }` 用「文件内唯一键」而非行号定位定义点，`def_site_key` 的命名规则为：类型=类型名、顶层函数=函数名、作用域内方法/元素字段=`父::子`、方法参数=`父::方法::参数`；这套设计抗宏展开扭曲、抗源码编辑（利于热重载）。
- `static DATA` 承载编译期常量以满足 `&'static`，`LazyLock` 包裹跨类型引用或需运行时求值的字段（如 `scope`/`params`/`returns`），两者分工解决「初始化顺序」问题。
- 错误处理铁律是**绝不 panic**：子模块返回 `syn::Result`，用 `bail!` 造带 span 的错误，入口用 `.unwrap_or_else(|err| err.to_compile_error())` 回传为编译错误。
- 整体架构可概括为「**重代码生成 + 运行时 trait 契约**」：换来零反射开销、强类型校验、精确定位，代价是宏实现复杂、编译开销增加。

## 7. 下一步学习建议

本讲是 `typst-macros` 学习手册的终点，但只是理解 Typst 整体的起点。建议接着做以下事情：

1. **跨入运行时侧**：打开 `crates/typst-library`，阅读 `foundations` 模块下 `NativeFunc`、`NativeType`、`NativeElement`、`NativeScope`、`Reflect`/`FromValue`/`IntoValue` 的定义，把本讲的「契约对端」补全。重点看 `Element::from_vtable` 与 `ContentVtable`，理解宏产出的 vtable 是如何被运行时消费的（对应 u4-l3）。
2. **看一次真实展开**：用 `cargo expand`（或 `cargo +nightly rustc -- -Zunpretty=expanded`）展开一个真实的 `#[func]` 或 `#[elem]`，亲眼对照本讲描述的 `static DATA`、`LazyLock`、`DefSite`、包装闭包，把抽象架构落地为具体代码。
3. **动手扩展**：尝试为本 crate 的某个宏添加一个小的、带 `bail!` 校验的新属性（例如给 `#[func]` 加一个 `hidden` 标志），走一遍「`parse` 增字段 → `create` 增分支 → 加 `kw` 关键字」的完整流程，检验你是否真正掌握了本讲的架构骨架。
4. **回顾全册**：从 u1-l1 到本讲通读一遍，把每个宏的 `parse`/`create` 与本讲的五大跨切面（骨架、契约、定位、延迟初始化、错误回传）交叉对照，形成一张全局心智图。
