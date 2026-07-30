# 类型化 HTML API 的生成机制

## 1. 本讲目标

学完本讲，你应该能够：

- 说清楚 `html.div`、`html.span`、`html.a` 这类「类型化构造函数」**并不是一个个手写的 Rust 函数**，而是由 `typst-assets` 里收录的 HTML 规范数据，在运行时被 `typed.rs` 批量「长出来」的。
- 沿着 `define() → FUNCS → create_func_data → create_param_info → construct` 这条链，讲清楚每一个 HTML 标签函数的**签名（有哪些参数）**和**实现（参数怎么变成属性）**是如何生成的。
- 解释为什么 void 标签（如 `img`、`br`）没有 `body` 参数，为什么 raw 标签（如 `script`、`style`）的 `body` 只接受字符串而非 Typst 内容。
- 把本讲与前两讲串起来：`create_param_info` 复用了 u2-l4 的 `tag::is_void` / `tag::is_raw` 分类，`construct` 复用了 u2-l3 的 `HtmlAttrs` 与 `HtmlAttr::constant`。

## 2. 前置知识

本讲建立在 **u2-l3（HtmlAttr/HtmlAttrs 属性系统）** 与 **u2-l4（标签常量与内容模型分类）** 之上，请先确认以下概念：

- **字符串驻留（interning）**：`HtmlTag`、`HtmlAttr` 是把名字驻留成轻量句柄的 newtype（u2-l2/u2-l3）。`HtmlTag::constant` 与 `HtmlAttr::constant` 是编译期 `const fn`，失败会 panic，常用于在常量表里预定义标准标签/属性名。
- **内容模型分类**：`tag::is_void(tag)` 判断「自闭合、不能有子节点」的标签；`tag::is_raw(tag)` 判断「内容是原样文本、不做 HTML 转义」的标签（`script`/`style`）。
- **Typst 的原生函数元数据**：标准库里每个对用户暴露的函数（`align`、`html.div`……）在内部都由一份 `NativeFuncData` 描述——函数指针、名字、文档、参数列表（`NativeParamInfo`）、返回类型等。`#[elem]` / `#[func]` 宏会为「手写的」函数自动生成这份元数据；而类型化 HTML API 的特殊之处正在于——它是**手工拼装**这份元数据的，数据来源是 `typst-assets`。
- **`typst-assets` 是「纯数据」crate**（见 u1-l1）：其中 `typst_assets::html` 模块用一张表描述了所有 HTML 元素及其合法属性。本讲的 `typed.rs` 就是这张表的「解释器」，把规范数据翻译成 Typst 可调用的函数。

> 术语约定：本讲把 `typed.rs` 中 `use typst_assets::html as data;` 引入的模块简称为 **`data`**。`data::ElemInfo` 描述一个元素（含 `name`、`docs`、`attributes()`、`get_attr(name)`），`data::Type` 描述一个属性的类型。它们的权威定义位于隔壁 crate `typst-assets`（不在本 crate 的链接范围内），本讲只依据 `typed.rs` 如何**消费**它们来讲解。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [`src/typed.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/typed.rs) | **本讲主角**：类型化 HTML API 的全部生成逻辑（`define`/`FUNCS`/`create_func_data`/`create_param_info`/`construct`） |
| [`src/lib.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/lib.rs) | `module()` 调用 `typed::define`，把生成出的函数注册进 `html` 作用域 |
| [`src/dom.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs) | 提供 `HtmlElem::new`、`HtmlAttr::constant`、`HtmlAttrs::push` 等 `construct` 依赖的积木 |
| [`src/tag.rs`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/tag.rs) | 提供 `is_void` / `is_raw`，决定是否生成 `body` 参数 |
| `crates/typst-library/src/foundations/func.rs` | `NativeFuncData` / `NativeParamInfo` 的结构定义（隔壁 crate，供对照） |

## 4. 核心概念与源码讲解

### 4.1 注册入口 define()

#### 4.1.1 概念说明

用户在 Typst 里写 `#html.div[..]`、`#html.a(href: "...")[..]` 时，`div`、`a` 这些名字必须先出现在 `html` 这个**作用域（Scope）**里，才能被解析器找到。`define()` 就是把「由规范数据生成的一整批函数」一次性塞进作用域的注册入口。

它本身极短，真正的重量级工作都在它调用的 `FUNCS` 静态里。所以本模块的重点不是 `define()` 的逻辑（几乎没有），而是理解它在整条链里的**位置**：它是「规范数据 → 可调用函数」这条流水线的最末端阀门。

#### 4.1.2 核心流程

```
module()                  （lib.rs，组装标准库模块）
   └─ typed::define(&mut html)   把整批函数注册进 html 作用域
         └─ 遍历 FUNCS（懒加载的所有 NativeFuncData）
               └─ html.define_func_with_data(data)  按 data.name 注册一个 Func
```

`define_func_with_data` 的语义是「用现成的函数元数据注册一个原生函数」：它读取 `data.name` 作为函数名，把 `data` 包成 `Func` 绑定到作用域里。每个类型化函数（`div`、`span`、`a`……）对应一个 `NativeFuncData`。

#### 4.1.3 源码精读

`define()` 的完整实现，逐行注释如下——它把 `FUNCS` 静态里每一个 `NativeFuncData` 注册进传入的作用域：

[typed.rs:31-35](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/typed.rs#L31-L35) —— 遍历 `FUNCS`，逐个调用 `define_func_with_data` 把函数挂到 `html` 作用域上。

调用点在 `module()` 里，紧挨着手写的两个原生元素 `HtmlElem`、`FrameElem` 之后：

[lib.rs:34-41](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/lib.rs#L34-L41) —— `module()` 先注册 `html.elem`/`html.frame`，再调用 `typed::define` 批量注册 `html.div` 等类型化函数。

> 关键对比：`html.elem` 用的是 `define_elem::<HtmlElem>()`（走 `#[elem]` 宏自动生成的元数据），而 `html.div` 用的是 `define_func_with_data`（走本讲手工拼装的元数据）。两条注册路径，但最终在作用域里都是「一个名字 → 一个 `Func`」。

`define_func_with_data` 的定义在隔壁 crate，仅作对照：

[scope.rs:144-149](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/scope.rs#L144-L149) —— 用 `data.name` 当键，把 `NativeFuncData` 包成 `Func` 绑定到作用域。

#### 4.1.4 代码实践

**实践目标**：确认类型化函数确实由 `define()` 批量注册，并估算数量级。

1. 打开 `src/lib.rs`，定位 `module()` 中的 `crate::typed::define(&mut html);` 一行。
2. 打开 `src/typed.rs`，确认 `define()` 体只有一个 `for` 循环。
3. 追问：循环次数由谁决定？（答：由 `FUNCS` 的长度决定，而 `FUNCS` 由 `data::ELEMS` 决定，即 `typst-assets` 收录了多少个 HTML 元素。）

**需要观察的现象 / 预期结果**：你会看到类型化 API 的「数量」完全不在 typst-html 这一侧硬编码，而是随 `typst-assets` 的规范表增长而自动增长。这是本讲最核心的设计直觉。

#### 4.1.5 小练习与答案

- **练习**：如果有人新增了一个 HTML 元素到 `typst-assets`，typst-html 这边需要改 `define()` 吗？
- **答案**：不需要。`define()` 是数据驱动的循环，新增元素会自动多出一个 `html.<新标签>` 函数。这正是「数据驱动生成」相比「手写函数」的优势。

---

### 4.2 惰性静态 FUNCS

#### 4.2.1 概念说明

`FUNCS` 是一个 `LazyLock<Vec<NativeFuncData>>`：一个**懒加载的全局静态**，存放「所有类型化 HTML 构造函数」的元数据。它第一次被 `define()` 触碰时才真正构造，且只构造一次。

为什么需要它？因为 `NativeFuncData` 的不少字段是 `&'static` 引用（函数指针、名字、文档、参数闭包……），而这些东西要依据运行时的 `data::ELEMS` 表动态拼装出来——动态拼装出的闭包和字符串没有天然的生命周期，必须放进一个「永不释放」的地方。`FUNCS` 用两种手段解决生命周期问题：

1. 一个被 `Box::leak` 泄漏的 `Bump` 内存池，用来分配那些必须 `'static` 的闭包与字符串；
2. `LazyLock` 本身作为 static，使其内部 `Vec` 的元素天然拥有 `'static` 生命周期（供 `define_func_with_data` 取 `&'static NativeFuncData`）。

#### 4.2.2 核心流程

```
首次访问 FUNCS
  └─ Box::leak(Box::new(Bump::new()))   创建一个 'static 的 bump arena
  └─ data::ELEMS.iter()                  遍历规范表里每个 ElemInfo
        └─ create_func_data(info, bump)  把一个元素 → 一份 NativeFuncData
              （闭包/字符串分配在 bump 里，拿到 'static 引用）
  └─ .collect() 收集成 Vec<NativeFuncData>
```

#### 4.2.3 源码精读

[typed.rs:38-43](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/typed.rs#L38-L43) —— `FUNCS` 静态：泄漏一个 `Bump`，把 `data::ELEMS` 每个元素经 `create_func_data` 映射成 `NativeFuncData` 后收集。

两个关键细节：

- **「Leaking is okay here」**：注释解释，因为 `FUNCS` 本身是 static，泄漏一块内存在语义上和「静态分配的值」没有区别——程序生命周期内本就不释放。`bumpalo::Bump` 是一个 bump allocator，分配快、只能整体释放；这里靠泄漏换取「分配出的引用是 `'static`」。
- **数据源 `data::ELEMS`**：这是 `typst-assets` 提供的元素表，`typed.rs` 顶部用 `use typst_assets::html as data;` 引入。每个 `&'static data::ElemInfo` 含元素名、文档以及属性信息。

> 为什么是 `LazyLock` 而不是 `once_cell` / 直接 `const`？因为内容依赖运行时遍历 `data::ELEMS` 并在 `Bump` 里分配闭包，无法在 `const` 上下文完成；`LazyLock` 提供线程安全的一次性初始化。

#### 4.2.4 代码实践

**实践目标**：理解「泄漏换 `'static`」这一取舍。

1. 阅读 `FUNCS` 上方的三行注释（typed.rs:39-41）。
2. 思考：如果不泄漏 `Bump`，而是用普通 `Box<Bump>` 存进某个字段，`create_func_data` 里分配的闭包引用还能是 `'static` 吗？
3. 进一步：`define_func_with_data` 要求 `&'static NativeFuncData`（见 4.1.3 的 scope.rs）。追问：`FUNCS.iter()` 产出的 `&NativeFuncData` 凭什么是 `'static`？

**预期结果**：`FUNCS` 是 static，对其解引用得到 `&'static Vec<...>`，迭代得到 `&'static NativeFuncData`——这是安全的，无需 `unsafe`。而 `Bump` 内部的闭包/字符串则需要靠泄漏拿到 `'static`。两者共同满足 `NativeFuncData` 全字段 `'static` 的要求。

#### 4.2.5 小练习与答案

- **练习 1**：`FUNCS` 的元素个数等于什么？
- **答案**：等于 `data::ELEMS.len()`，即 `typst-assets` 收录的 HTML 元素总数。
- **练习 2**：为什么用 `Bump` 而不是 `Vec` 来分配闭包？
- **答案**：`NativeFuncData.function` 等字段需要指向「具体某个元素」的闭包（闭包捕获了 `element`），每个元素一个独立闭包。`Bump` 适合批量分配大量小对象且统一存活到程序结束，配合泄漏正好满足 `'static`。

---

### 4.3 元数据生成 create_func_data

#### 4.3.1 概念说明

`create_func_data` 把「一个 `data::ElemInfo`」翻译成「一份完整可调用的 `NativeFuncData`」。这是「规范数据 → Typst 函数」的核心翻译步骤。

回顾 `NativeFuncData` 的字段（隔壁 crate 定义，供对照）：函数指针 `function`、`name`、`title`、`since`、`docs`、`keywords`、`contextual`、`scope`、`params`、`returns`。[func.rs:633-656](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/func.rs#L633-L656) 给出了这些字段的含义。

本函数的任务就是把这些字段逐一填好：函数指针指向统一的 `construct`（见 4.5），名字/文档取自规范表，参数列表委托给 `create_param_info`（见 4.4）。

#### 4.3.2 核心流程

对单个 `element: &'static data::ElemInfo`：

```
create_func_data(element, bump):
  function   = 在 bump 里分配一个闭包，捕获 element，调用 construct(element, args)
  name       = element.name                       （如 "a"、"div"）
  title      = element.name 首字母大写              （如 "A"、"Div"）
  since      = Some(Since::Version([0, 14, 0]))   类型化 API 自 0.14.0 引入
  docs       = element.docs
  keywords   = &["typed-html"]                    统一的检索关键词
  contextual = false
  scope      = 空作用域
  params     = create_param_info(element)          （见 4.4）
  returns    = Content 类型
```

注意 `function`、`params`、`returns`、`scope` 都用 `LazyLock::new(&|| ...)` 包裹，且其中的闭包/字符串分配在传入的 `bump` 上，从而得到 `'static` 引用——这正是 4.2 中 `Bump` 的用途。

#### 4.3.3 源码精读

[typed.rs:46-71](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/typed.rs#L46-L71) —— `create_func_data`：逐字段装配 `NativeFuncData`。

几个值得注意的点：

- **所有元素共享同一个函数实现**：`function` 指向的闭包体只是 `construct(element, args)`（[typed.rs:51-55](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/typed.rs#L51-L55)）。`html.div` 与 `html.a` 跑的是同一段代码，差别只在于各自闭包**捕获的 `element` 不同**——`element` 决定了合法属性集合和标签名。
- **`title` 的小技巧**：用 `bump.alloc_str(element.name)` 复制名字，再原地 `[0..1].make_ascii_uppercase()` 把首字母大写（[typed.rs:57-61](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/typed.rs#L57-L61)）。所以 `a` 的标题是 `A`，`div` 的标题是 `Div`，用于文档与自动补全。
- **统一的 `since` 与 `keywords`**：所有类型化函数都标记为 `0.14.0` 引入、关键词 `typed-html`（[typed.rs:62-65](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/typed.rs#L62-L65)）。这与 u1-l1 提到的「类型化 API 自 0.14.0 引入」一致。
- **`params` 与 `returns` 是惰性的**：用 `LazyLock::new(bump.alloc(move || create_param_info(element)))`（[typed.rs:68-69](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/typed.rs#L68-L69)），只有真正需要参数信息（如生成文档、校验调用）时才计算。

> 对照 `NativeFuncPtr` 与函数签名 `dyn Fn(&mut Engine, Tracked<Context>, &mut Args) -> SourceResult<Value>`：[func.rs:664-668](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/func.rs#L664-L668)。本讲的闭包忽略了 `Engine` 与 `Context`（用 `_`），因为 `construct` 只做参数装配，不触发排版。

#### 4.3.4 代码实践

**实践目标**：体会「所有标签共用一套实现，差异只来自 `element`」。

1. 在 [typed.rs:46-71](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/typed.rs#L46-L71) 中找到 `function` 字段，确认闭包体只有 `construct(element, args)`。
2. 问自己：`html.img(...)` 和 `html.div(...)` 在运行时进入的是不是同一个 Rust 函数？
3. 思考：既然实现相同，那 `img` 比 `div` 少一个 `body` 参数这件事，是在哪一层决定的？（答：在 `create_param_info` 生成的**签名**里，而非 `construct` 的实现里——见 4.4。）

**预期结果**：你会理解到「签名（能不能传 body）」与「实现（参数怎么处理）」是分离的两件事，分别由 `create_param_info` 和 `construct` 负责。

#### 4.3.5 小练习与答案

- **练习**：`title` 字段为什么要在 `bump` 里重新 `alloc_str` 一份，而不是直接用 `element.name`？
- **答案**：因为要对副本做 `make_ascii_uppercase()` 原地修改首字母，不能破坏规范表里的原始字符串；副本必须 `'static`，所以分配在 `bump` 上。

---

### 4.4 参数签名生成 create_param_info

#### 4.4.1 概念说明

`create_param_info` 决定一个类型化函数「**接受哪些参数**」。它产出一组 `NativeParamInfo`，Typst 据此做参数校验、文档生成与自动补全。

这里的精髓在于：参数列表是**依据 HTML 规范动态推导**的——

1. 遍历 `element.attributes()`，为每个合法属性生成一个**命名参数**（`named: true`），其类型由 `AttrType::convert(attr.ty)` 决定。
2. 再依据标签本身是否 `void` / `raw`，决定要不要追加一个**位置参数 `body`**。

这正是 u2-l4 的 `tag::is_void` / `tag::is_raw` 在本讲的直接调用点。

#### 4.4.2 核心流程

```
create_param_info(element):
  params = []
  for attr in element.attributes():        # 每个合法 HTML 属性
      params.push(命名参数 attr.name，类型 = AttrType::convert(attr.ty).input())
  tag = HtmlTag::constant(element.name)
  if !is_void(tag):                        # 不是自闭合标签才有 body
      raw = is_raw(tag)
      params.push(位置参数 "body"：
          文档 = raw ? "The text content..." : "The contents..."
          类型 = raw ? Str : Content)
  return params
```

三条规则一目了然：

| 标签类别 | 例子 | 是否有 body | body 类型 |
| --- | --- | --- | --- |
| void | `img` `br` `input` `meta` | **无** | —— |
| raw | `script` `style` | 有 | `Str`（原样文本） |
| 普通 | `div` `a` `span` | 有 | `Content`（Typst 内容） |

#### 4.4.3 源码精读

属性参数的生成循环：

[typed.rs:74-89](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/typed.rs#L74-L89) —— 遍历 `element.attributes()`，为每个属性造一个 `named: true` 的参数，`input` 取自 `AttrType::convert(attr.ty).input()`。

> `AttrType::convert` 把规范里的 `data::Type` 翻译成 `typed.rs` 内部的 `AttrType` 枚举（[typed.rs:176-207](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/typed.rs#L176-L207)），`.input()` 给出该类型在 Typst 里能接受什么值（`CastInfo`）。属性类型的完整体系（`Presence`/`Native`/`Strings`/`Union`/`List` 及各种布尔编码）是 **u6-l3** 的主题，本讲只需把它当作「把规范类型翻译成 Typst 类型」的黑盒。

`body` 参数的条件追加——本模块的核心：

[typed.rs:90-113](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/typed.rs#L90-L113) —— 用 `HtmlTag::constant(element.name)` 取标签，若 `!is_void(tag)` 才追加 `body`；`is_raw(tag)` 决定 body 类型是 `Str` 还是 `Content`。

逐行看点：

- **`HtmlTag::constant(element.name)`**（[typed.rs:90](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/typed.rs#L90)）：复用 u2-l2 的编译期驻留把元素名变成 `HtmlTag`，便于喂给 `is_void`/`is_raw`。这里用 `constant` 而非 `intern`，因为元素名都是规范预定义的合法标准标签，可在编译期完成；`test_tags_and_attr_const_internible` 测试（[typed.rs:734-743](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/typed.rs#L734-L743)）专门保证 `data::ELEMS` 里所有名字都能通过 `constant` 校验。
- **`is_void` 守卫**（[typed.rs:91](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/typed.rs#L91)）：void 标签（u2-l4：`area`/`base`/`br`/`col`/`embed`/`hr`/`img`/`input`/`link`/`meta`/`source`/`track`/`wbr`）直接跳过 body，于是 `html.img(src: "...")` 根本没有 body 槽位——传了内容会在 Typst 侧报参数错误。
- **`is_raw` 分流**（[typed.rs:92](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/typed.rs#L92)）：`script`/`style` 的内容是原样文本，body 类型用 `Str`；普通标签 body 是 `Content`（[typed.rs:101-105](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/typed.rs#L101-L105)）。文档字符串也随之不同（[typed.rs:95-99](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/typed.rs#L95-L99)）。
- **body 是位置参数**：`positional: true, named: false`（[typed.rs:107-108](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/typed.rs#L107-L108)），对应 Typst 语法 `html.div[内容]` 里方括号包裹的位置内容；而属性参数都是 `named: true`，对应 `html.a(href: "...")` 的键值对。

#### 4.4.4 代码实践

**实践目标**：用源码预测三类标签的参数表差异。

1. 选三个标签：`img`（void）、`script`（raw）、`a`（普通）。
2. 对照 [tag.rs:125-147](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/tag.rs#L125-L147) 确认 `is_void("img")` 为真、`is_raw("script")` 为真、`a` 两者皆假。
3. 手工套用 `create_param_info` 的流程，分别列出三者「是否有 body 参数、body 类型是什么」。

**预期结果**：

- `html.img`：只有命名属性参数（如 `src`、`alt`），**无 body**。
- `html.script`：命名属性 + 一个 `Str` 类型的 `body`。
- `html.a`：命名属性（如 `href`）+ 一个 `Content` 类型的 `body`。

（如需核对完整属性清单，需查阅 `typst-assets` 的元素表，本 crate 内不重复存储。）

#### 4.4.5 小练习与答案

- **练习 1**：为什么 void 标签不能有 body？请同时从 HTML 规范和本讲源码两个角度回答。
- **答案**：HTML 规范规定 void 元素（如 `img`）不能有子节点、不能有结束标签；源码里 `create_param_info` 用 `if !tag::is_void(tag)` 守卫，跳过 body 参数的追加，从 Typst 侧就不允许传入内容。
- **练习 2**：`html.script[...]` 的方括号内容会被当作什么类型？为什么不是 `Content`？
- **答案**：当作 `Str`。因为 `script` 是 raw 标签，其内容是原样文本、不做 HTML 转义也不应被当作可排版的 Typst 内容，所以 body 类型在 [typed.rs:101-105](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/typed.rs#L101-L105) 被设为 `Str`。

---

### 4.5 运行时构造 construct()

#### 4.5.1 概念说明

`construct` 是所有类型化函数**真正执行**时跑的代码（4.3 中每个闭包都调用它）。它接收用户传进来的 `Args`，把命名参数装配成 `HtmlAttrs`，再连同 body 组装出一个 `HtmlElem`（即 `html.elem` 对应的那个原生元素，u1-l4）。

换句话说，`html.a(href: "x")[文本]` 在运行时等价于程序化地构造了一个 `html.elem("a", attrs: (href: "x"))[文本]`。类型化 API 的运行时本质就是 `html.elem` 的语法糖。

#### 4.5.2 核心流程

```
construct(element, args):
  attrs = HtmlAttrs::default()
  errors = []

  # 第一遍：用 retain「边遍历边消费」命名参数
  args.items.retain(|item| {
      名字为空(位置参数)?        → 保留 return true
      该名字不是 element 的合法属性? → 保留 return true（留给后续）
      取出值，按 AttrType::convert(attr.ty).cast(value) 转换：
          Ok(Some(s)) → attrs.push(HtmlAttr::constant(attr.name), s)
          Ok(None)    → 跳过（如布尔 presence 为 false）
          Err(diags)  → 收集错误
      return false  # 已处理，从 items 移除
  })
  有错误则返回 Err

  tag = HtmlTag::constant(element.name)
  elem = HtmlElem::new(tag)
  若 attrs 非空：elem.attrs.set(attrs)

  if !is_void(tag):                 # 非 void 才取 body
      body = is_raw(tag) ? args.eat::<Spanned<Str>>() : args.eat::<Content>()
      elem.body.set(body)

  return elem.into_value()
```

#### 4.5.3 源码精读

`construct` 全貌：

[typed.rs:118-160](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/typed.rs#L118-L160) —— 统一的构造函数：把命名参数变成 `HtmlAttrs`，按 void/raw 处理 body，产出 `HtmlElem`。

三个关键技巧：

1. **`retain` 双重过滤**（[typed.rs:122-136](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/typed.rs#L122-L136)）：`retain` 保留返回 `true` 的项。对每个参数项：
   - 没有名字（位置参数，即未来的 body）→ 返回 `true` 保留；
   - 有名字但 `element.get_attr(name)` 查不到（不是该元素的合法属性）→ 返回 `true` 保留（让 Typst 后续按「未知参数」报错）；
   - 命中合法属性 → 用 `AttrType::convert(attr.ty).cast(value)` 转成字符串，成功则 `attrs.push(...)`，失败则收集诊断；最后返回 `false` 把它从 `args.items` 移除。
   
   这个写法把「识别 + 转换 + 清理」三件事揉进一遍遍历，干净利落。
2. **属性名走 `HtmlAttr::constant`**（[typed.rs:130](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/typed.rs#L130)）：复用 u2-l3 的编译期驻留。规范表里的属性名都是合法标准属性名，`test_tags_and_attr_const_internible`（[typed.rs:739-741](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/typed.rs#L739-L741)）保证它们都能 `constant` 成功。
3. **body 的 void/raw 二次判定**（[typed.rs:148-157](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/typed.rs#L148-L157)）：与 `create_param_info` 的判定**完全对称**——签名里不生成 body 参数的 void 标签，这里也不读取 body；raw 标签用 `args.eat::<Spanned<Str>>()` 取字符串（再 `TextElem::packed` 包成内容），普通标签用 `args.eat::<Content>()`。这种「签名与实现成对一致」保证了类型安全。

> 值得注意的是 `construct` **完全不碰 `Engine` / `Context`**（4.3 的闭包用 `_` 忽略了它们）。它只做纯粹的参数→DOM 装配，不触发排版或求值——排版发生在文档编译主链路（u3 单元）把 `HtmlElem` realize 之后。

最终产出 `HtmlElem`（即 `html.elem`，见 [lib.rs:64-104](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/lib.rs#L64-L104)），其 `attrs` 字段正是 u2-l3 讲过的、带 `#[fold]` 语义的 `HtmlAttrs`。

#### 4.5.4 代码实践（本讲主实践）

**实践目标**：端到端追踪 `#html.a(href: "https://typst.app")[Typst]` 的执行路径，把参数如何被 cast 并写入 `HtmlAttrs` 讲清楚。

1. **解析阶段**：Typst 把 `html.a(href: "https://typst.app")[Typst]` 解析成一次函数调用，`href: "..."` 是命名参数项，`[Typst]` 是位置参数项，统一放进 `Args`。
2. **查函数**：在 `html` 作用域里找到 `a`——它正是 `define()`（4.1）从 `FUNCS`（4.2）里注册的那个 `NativeFuncData`，其 `element` 是 `<a>` 的 `ElemInfo`。
3. **校验签名**：Typst 用 `create_param_info`（4.4）生成的参数表校验：`a` 不是 void、不是 raw，所以接受 `href` 命名属性 + 一个 `Content` body。`href` 是 `<a>` 的合法属性，类型经 `AttrType::convert`（`Str` 之类）后 `href: "https://typst.app"` 合法。
4. **执行 construct**（4.5）：
   - `retain` 遍历：`href` 命中 `<a>` 的合法属性 → `AttrType::convert(attr.ty).cast("https://typst.app")` 成功得到字符串 → `attrs.push(HtmlAttr::constant("href"), "https://typst.app")`，该项被移除；
   - `[Typst]` 是位置参数，`retain` 返回 `true` 保留；
   - `attrs` 非空 → `elem.attrs.set(attrs)`；
   - `a` 非 void → `args.eat::<Content>()` 取出 `Typst` 设为 body。
5. **返回**：`Ok(elem.into_value())`，得到一个 `tag = "a"`、`attrs = [(href, "https://typst.app")]`、`body = Typst` 的 `HtmlElem`。

**需要观察的现象 / 预期结果**：

- 最终产物与手写 `#html.elem("a", attrs: (href: "https://typst.app"))[Typst]` **完全等价**——这正是「类型化 API 是 `html.elem` 的语法糖」的体现。
- 若改成 `#html.img(src: "x")[不该有内容]`：因为 `img` 是 void，`create_param_info` 没生成 body 参数，Typst 在校验阶段就会对多余的位置内容报错，根本进不到 `construct` 的 body 分支——这就是 void 标签没有 body 的根因。
- 待本地验证：可在本地用 `typst compile --format html` 实际编译上述片段，对照生成的 HTML 中 `<a href="...">Typst</a>` 与 `<img src="x">`（自闭合、无内容）。

#### 4.5.5 小练习与答案

- **练习 1**：`construct` 里的 `retain` 对「用户传了一个该元素不认识的属性名」会怎么处理？
- **答案**：`element.get_attr(name)` 返回 `None`，闭包返回 `true` 保留该项；它不会被消费，随后 Typst 会按「未知参数」报错（[typed.rs:123-124](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/typed.rs#L123-L124)）。这正是类型化 API「按规范限制合法属性」的机制。
- **练习 2**：为什么 `construct` 里对 body 的 void/raw 判定要和 `create_param_info` 保持一致？
- **答案**：两者必须成对一致才能保证类型安全——签名声明了「无 body」（void），实现就绝不能去 `eat` body；签名声明了「body 是 `Str`」（raw），实现就用 `eat::<Spanned<Str>>()`。任一边偏离都会导致参数被错误消费或漏掉。

---

## 5. 综合实践

**任务**：化身「规范解释器」，用一张表把本讲五件事串起来。

给定标签 `a`（普通）、`img`（void）、`script`（raw），请填写下表并标注每格结论来自哪个函数：

| 标签 | `is_void` | `is_raw` | `create_param_info` 是否生成 body | body 类型 | `construct` 如何取 body | 运行时产物 |
| --- | --- | --- | --- | --- | --- | --- |
| `a` | ? | ? | ? | ? | ? | ? |
| `img` | ? | ? | ? | ? | ? | ? |
| `script` | ? | ? | ? | ? | ? | ? |

**操作步骤**：

1. 用 [tag.rs:125-147](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/tag.rs#L125-L147) 填 `is_void` / `is_raw` 两列。
2. 用 [typed.rs:90-113](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/typed.rs#L90-L113)（`create_param_info` 的 body 分支）填第 3、4 列。
3. 用 [typed.rs:148-157](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/typed.rs#L148-L157)（`construct` 的 body 分支）填第 5 列。
4. 最后一列用一句话描述 `construct` 产出的 `HtmlElem` 长什么样（标签、属性、body）。

**参考答案**：

| 标签 | `is_void` | `is_raw` | 生成 body | body 类型 | `construct` 取 body | 运行时产物 |
| --- | --- | --- | --- | --- | --- | --- |
| `a` | 否 | 否 | 是 | `Content` | `args.eat::<Content>()` | `<a>` 元素，命名属性入 `HtmlAttrs`，内容为 body |
| `img` | 是 | 否 | 否 | —— | 不取（跳过 body 分支） | `<img>` 元素，仅属性、无 body（自闭合） |
| `script` | 否 | 是 | 是 | `Str` | `args.eat::<Spanned<Str>>()` | `<script>` 元素，原样文本字符串作 body |

**延伸思考**：把这张表与 u2-l4 的内容模型分类对照——你会发现 void/raw 这两个「语法分类」不仅驱动编码（u5-l1/u5-l2），还直接决定了类型化 API 的**函数签名形状**。同一份规范数据，在 typst-html 的不同模块里被反复「解释」出不同用途。

## 6. 本讲小结

- 类型化 HTML API（`html.div` 等）**不是手写的**，而是 `typed.rs` 依据 `typst-assets::html` 的规范数据在运行时批量生成的，元素数量随规范表自动增长。
- 生成链为 `define()` → `FUNCS`（懒加载 + 泄漏 `Bump` 换 `'static`）→ `create_func_data`（装配 `NativeFuncData`）→ `create_param_info`（推导参数签名）→ `construct`（运行时装配）。
- **所有类型化函数共用同一个 `construct` 实现**，差异只来自各自闭包捕获的 `element`（决定合法属性集合与标签名）。
- `create_param_info` 用 u2-l4 的 `is_void`/`is_raw` 决定是否生成 `body` 参数及其类型：void 无 body，raw 的 body 是 `Str`，普通标签的 body 是 `Content`。
- `construct` 用 u2-l3 的 `HtmlAttrs`/`HtmlAttr::constant` 把命名参数 cast 成字符串压入属性，并复用 `HtmlElem::new` 产出与 `html.elem` 等价的结果——类型化 API 本质是 `html.elem` 的强类型语法糖。
- 「签名（`create_param_info`）」与「实现（`construct`）」对 void/raw 的判定**成对一致**，是这套生成机制类型安全的基石。

## 7. 下一步学习建议

- **深入属性类型系统**：本讲把 `AttrType::convert` 当作黑盒。它如何把 `data::Type` 的 `Presence`/`Native`/`Strings`/`Union`/`List` 变体翻译成 Typst 类型与字符串、各种布尔编码（`TrueFalseBool`/`YesNoBool`/`OnOffBool`）有何区别，请接着读 **u6-l3「类型化属性类型系统深入」**。
- **追踪产出物的去向**：`construct` 产出 `HtmlElem` 后，它如何进入文档编译主链路、被 realize 成 DOM、最终编码成 HTML，是 **u3 单元（编译与转换主流程）** 的主题，建议按 u3-l1 → u3-l3 顺序阅读。
- **对照 void/raw 的下游影响**：本讲看到 void/raw 影响函数签名；在 **u5-l1（DOM 到 HTML 编码）** 与 **u5-l2（字符集与转义）** 中，它们还会决定自闭合标签和原样文本的编码路径，可对照阅读以建立完整闭环。
