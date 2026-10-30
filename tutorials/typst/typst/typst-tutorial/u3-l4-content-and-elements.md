# Content 与元素机制：文档的统一表示

## 1. 本讲目标

上一讲我们弄清楚了 Typst 脚本世界里的「值」——Value 枚举。本讲深入其中一个最重要的变体：Value::Content，也就是 Content 类型。学完本讲，你应该能够：

1. 说出 Content 如何用一套极小的变体集合（文本、元素、样式包装、空、序列）统一表示整篇文档；
2. 理解 #[elem] 宏为元素生成了什么：字段、必选（required）与可设置（settable）的区分、getter/setter 方法、以及内省信息（名字、标题、文档、关键词）的来源；
3. 解释 Label（标签）和 Selector（选择器）的数据表示，以及它们在 show 规则、where 过滤、query 查询中的匹配作用；
4. 能阅读 content.rs、elem.rs、selector.rs、label.rs 的关键代码，并把仓库自带的 Content 单元测试当作实践样板跑起来。

## 2. 前置知识

在开始之前，请确保你理解下面几个概念（不熟悉的话先回到 u3-l3 复习）：

- **Value 与 Content 的关系**：Typst 脚本中所有值统一装进 Value 枚举；排版产物是其中一类值，即 Value::Content。换句话说，Content 是「文档内容」在 Rust 侧的本体，Value 只是把它包进通用值系统的一层壳。
- **求值与排版的分界**：typst-eval 把语法树求值成 Content，typst-layout 把 Content 排成 Frame。Content 是两个阶段之间的交接数据，是排版的全部输入。
- **标记与代码两种模式**：标记模式下写出的文字、标题、列表，求值后都变成 Content；代码模式下 heading(level: 2) 这样的函数调用，返回值同样是 Content。
- **Arc 与共享**：Arc 是引用计数的共享指针，让克隆大对象变得廉价。Content 大量使用它来保证「克隆一份内容」几乎零开销。
- **过程宏（proc macro）**：#[elem] 是一个过程宏，在编译期读取你写的 Rust 结构体定义，自动生成大量样板代码（构造函数、字段访问、文档注册）。本讲会不断看到「写一行声明，宏生成一堆代码」的模式。
- **驻留字符串（interned string）**：把重复出现的字符串在全局只存一份、比较时只需比较指针的技术。Label 就基于它实现。

一个帮助建立直觉的类比：如果把排版引擎看作浏览器，Content 之于 Typst，就类似 DOM 之于 HTML——一个可查询、可改写、可挂样式规则的文档树。但 Content 比 DOM 更轻：它不可变（immutable）、可哈希（Hash），这两个性质正是增量编译缓存（comemo）能够工作的前提。

## 3. 本讲源码地图

本讲全部代码位于 typst-library crate 的 foundations（地基）目录，外加一个解剖用的具体元素。下表是本讲涉及的文件：

| 文件 | 规模 | 作用 |
| --- | --- | --- |
| crates/typst-library/src/foundations/content.rs | 1244 行 | Content 类型本体、内部表示 ContentInner、字段读写方法、show/label 等包装方法，文件末尾是单元测试样板 |
| crates/typst-library/src/foundations/elem.rs | 349 行 | Element 类型（全局注册表引用 + 索引）、NativeElementData 元数据，是 #[elem] 宏生成代码的运行时搭档 |
| crates/typst-library/src/foundations/elem.rs.md | 496 行 | #[elem] 宏的官方使用文档：字段规则、命名转换、宏选项（能力标记）一览 |
| crates/typst-library/src/foundations/selector.rs | 483 行 | Selector 枚举九种变体，以及 elem、show、select、matches 四个关键方法 |
| crates/typst-library/src/foundations/label.rs | 117 行 | Label 类型（驻留字符串）、LabelError、label! 宏 |
| crates/typst-library/src/foundations/mod.rs | 96 行 | foundations 模块装配处，re-export typst-macros 的 elem、func、scope、ty 四个宏 |
| crates/typst-library/src/model/heading.rs | 548 行 | HeadingElem 定义，本讲的解剖样本：一个真实的 #[elem] 用户 |

阅读建议：先通读 content.rs 的前 200 行建立骨架印象，再对照 elem.rs.md 的规则去读 heading.rs，最后用 selector.rs 和 label.rs 补齐匹配机制。永久链接统一指向当前 HEAD（64c5e8c）。

## 4. 核心概念与源码讲解

### 4.1 Content 数据结构：五个变体装下一篇文档

#### 4.1.1 概念说明

求值的产物千姿百态：一段文字、一个标题、一个函数返回的内容块、一个样式作用域……如果每种产物都设计一个类型，排版器就要认识几十种输入。Typst 的选择是定义一个统一的 Content 类型，让它既能当「一个文字片段」，也能当「一整篇文档」——就像乐高：最小的积木块和拼好的大作品是同一种东西。

Content 的内部表示只有五个变体（ContentInner 枚举）：

| 变体 | 含义 | 典型来源 |
| --- | --- | --- |
| Text(RawStr) | 一段纯文本 | 标记模式下直接写的文字 |
| Element(Element) | 一个元素实例（携带字段） | heading 函数调用、= 标题语法糖 |
| Empty | 空占位 | 从 Label 单独转换时（比如文档里孤立的 #<intro>） |
| Styled(StyledElem) | 把样式和内容打包在一起 | set / show 规则作用域 |
| Sequence(ContentVec) | 多个内容的有序序列 | 段落、整篇文档 |

注意两个微妙之处：

- 「空文档」并不是 Empty，而是一个空的 Sequence（见下文 empty() 的实现）；Empty 只在「内容为空但要挂标签」这类特殊场景出现。
- 文档的层级结构不是靠嵌套类型表达的：标题的正文、列表的条目都是元素字段里的 Content，天然形成树。

#### 4.1.2 核心流程

以一段标记求值为例，Content 是这样「拼」出来的：

1. 逐字文字片段变成 Text 变体；
2. 遇到语法糖（比如 = 开头的标题行），typst-eval 调用 heading 函数，产出 Element 变体（元素字段里装着 body 等 Content）；
3. 遇到 set/show 规则块，把块内求值结果包进 Styled 变体；
4. 同一层的多个产物依序收进 Sequence 变体。

伪代码示意：

（示例代码）
    eval(Markup [ Text("A "), Heading("Hi"), Text(" B") ])
    =>
    Content::Sequence([
        Content::Text("A "),
        Content::Element(heading_elem),   // 其 body 字段又是 Content
        Content::Text(" B"),
    ])

排版阶段（typst-layout）拿到这棵树后，会递归消费 Sequence、展开 Styled、对每个 Element 查规则表决定如何渲染——这是 u4 单元的主题。

#### 4.1.3 源码精读

**（1）Content 本体：一层薄包装**

见 [content.rs:L44-L46](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/content.rs#L44-L46)：Content 结构体只有一个字段 inner，真正的数据在下述 ContentInner 枚举里。这样设计的意图是：数据表示保持极小，而大量便捷方法挂在包装层上。

（示例代码，节选自源码）

    pub struct Content {
        inner: ContentInner,
    }

**（2）空内容与 From 转换族**

见 [content.rs:L48-L60](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/content.rs#L48-L60)：empty() 返回的是一个空的 Sequence，而不是 Empty 变体；同时单元类型 () 可以无成本转成空内容。

（示例代码，节选自源码）

    impl Content {
        /// Creates an empty content.
        pub const fn empty() -> Self {
            Self::Sequence(ContentVec::new())
        }
    }

    impl From<()> for Content {
        fn from(_: ()) -> Self {
            Self::empty()
        }
    }

紧接着是一组 From 实现，见 [content.rs:L61-L76](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/content.rs#L61-L76)：

- From&lt;Element&gt;：元素实例直接变成 Element 变体；
- From&lt;Label&gt;：先构造 Empty，再调用 label() 把标签挂上去——这正是文档里孤立的 #&lt;intro&gt; 标签求值后的形态；
- From&lt;TextElem&gt;：文本元素先转成 Element 再包装；
- From&lt;&amp;str&gt;：字符串经 text() 变成 Text 变体。

（示例代码，节选自源码）

    impl From<Element> for Content {
        fn from(elem: Element) -> Self { Self::Element(elem) }
    }

    impl From<Label> for Content {
        fn from(label: Label) -> Self { Self::Empty.label(label) }
    }

**（3）五个变体：ContentInner 枚举**

见 [content.rs:L78-L85](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/content.rs#L78-L85)：这就是 4.1.1 表格中五个变体的定义处。

（示例代码，节选自源码）

    pub enum ContentInner {
        /// A text string.
        Text(RawStr),
        /// A single element.
        Element(Element),
        /// Empty content.
        Empty,
        /// A styled element.
        Styled(StyledElem),
        /// A sequence of multiple contents.
        Sequence(ContentVec<Self>),
    }

**（4）构造与遍历方法**

见 [content.rs:L84-L93](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/content.rs#L84-L93)：text() 从字符串构造文本内容；plain() 返回一个压平后的「朴素内容」视图（PlainContent），供需要忽略样式包装的场景使用。

（示例代码，节选自源码）

    /// Create a plain text content.
    pub fn text(t: impl Into<RawStr>) -> Self {
        Self::Text(t.into())
    }

见 [content.rs:L124-L127](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/content.rs#L124-L127)：sequence() 把多个内容收进一个序列，值得注意的实现细节是——即使传入为空，也照样包一层 Sequence，不做特判折叠。

见 [content.rs:L133-L136](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/content.rs#L133-L136)：Content::new(elem) 是「从 Element 构造 Content」的显式入口（仓库自带的单元测试大量使用它，见下文 4.1.4）。

**（5）字段读取：脚本里 .level 的落点**

见 [content.rs:L151](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/content.rs#L151)：field(self, name) 按名字取出元素字段。当你在 Typst 脚本里写 heading(...).level 时，求值器最终调用的就是它——字段访问不需要任何注册表查找以外的魔法，就是一次按名取值。

**（6）字段写入与枚举**

见 [content.rs:L1074-L1082](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/content.rs#L1074-L1082)：fields() 家族（fields、into_fields、field_values）把元素的所有字段以迭代器形式交出来；[content.rs:L1088](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/content.rs#L1088) 的 with_field() 以 builder 风格「设置字段并返回新内容」；[content.rs:L1115](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/content.rs#L1115) 的 push_field() 则是就地附加字段的底层入口。宏为每个元素字段生成的便捷构建方法，正是建立在这些通用方法之上。

**（7）show 与 label：两类包装操作**

见 [content.rs:L1121-L1124](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/content.rs#L1121-L1124)：show() 接收选择器、引擎、样式链等参数，应用 show 规则后返回新内容——它的完整工作流程属于 u4-l2 的 realize 阶段，本讲只需记住「入口在这里」。

见 [content.rs:L1128-L1134](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/content.rs#L1128-L1134)：label(&amp;self, label: Label) 返回挂了标签的新内容。由于内容可能是一段序列，它会遍历其中的元素逐个设置标签——这就是 From&lt;Label&gt; 实现里 Self::Empty.label(label) 调用的方法。

**（8）孩子遍历**

见 [content.rs:L192-L197](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/content.rs#L192-L197)：children() 把序列的孩子依次交出。排版与 realize 阶段的递归遍历都以它为起点之一。

#### 4.1.4 代码实践

**实践目标**：亲眼看到「用 Rust 构造一个 heading 元素的 Content、设置字段、挂上标签」的完整调用链——仓库自带的单元测试就是最好的样板。

**操作步骤**：

1. 打开 [content.rs:L1182-L1216](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/content.rs#L1182-L1216) 的 tests 模块，找到三个关键构件：
   - 一个用名字从全局注册表取 heading 元素的静态变量（get_by_name("heading")）；
   - new_heading 辅助函数：Content::new(...) 构造内容，再设置 span；
   - new_heading_with_id 辅助函数：在上述基础上调用 label 相关代码挂标签。
2. 在仓库根目录运行：cargo test -p typst-library content —— 用关键字过滤出 content.rs 中的测试并执行（具体过滤出的测试数量以本地输出为准，待本地验证）。
3. 对照测试代码，指出 4.1.3 里学到的每个方法分别出现在哪一行：Content::new、字段设置、label。
4. 补充一个脚本侧小实验（不改任何源码）。新建 demo1.typ，内容如下：

（示例代码，typst 脚本）

    #let h = heading(level: 2)[Hello]
    #(type(h))
    #(h.level)
    #(h.body)

5. 运行 cargo run -p typst-cli -- compile demo1.typ（或已安装的 typst compile demo1.typ）。

**需要观察的现象**：

- 步骤 2 中测试是否全部通过；
- 步骤 5 编译出的 PDF 里依次出现 content（类型名）、2（level 字段的值）、Hello（body 字段渲染出的内容）。

**预期结果**：脚本里的 h.level 与 h.body 字段访问，对应 Rust 侧 Content::field（content.rs L151）按名取值；type(h) 返回 content 对应 Value::Content。若编译报错，请检查 typst 语法中的括号与分号。输出效果：待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：empty() 为什么返回空的 Sequence，而不是 Empty 变体？

参考答案：Empty 的语义是「占位载体」，它唯一的使用场景是 From&lt;Label&gt; 中先造空内容再挂标签。让常规空内容走空 Sequence，可以保持孩子遍历（children）等通用路径无需为 Empty 特判，两个变体各司其职。

**练习 2**：脚本里写 #&lt;intro&gt;（一个孤立的标签），求值后得到的 Content 是什么形态？

参考答案：From&lt;Label&gt; 实现（content.rs L65-L68）会构造 Self::Empty 再调用 label()，得到一个「空内容上挂着 intro 标签」的 Content。

**练习 3**：heading(level: 2)[Hi] 这个调用返回的 Content 是五个变体中的哪一个？其标题正文存在哪里？

参考答案：是 Element 变体；正文不在 Content 这一层，而是作为 heading 元素的字段（body）存在元素实例中，字段本身又是一个 Content，从而形成文档树。

### 4.2 元素字段与构造：#[elem] 宏的产物

#### 4.2.1 概念说明

ContentInner 的 Element 变体装的是 Element 类型的实例。可以把 Element 理解为「元素类型的运行时句柄」：它内部保存一个全局注册表（Globals）的引用和一个索引，凭这两个成员就能定位一种元素类型（比如 heading），再加上随实例存储的字段值（level、body 等）。

为什么不直接用 Rust 的静态类型？因为元素要在运行时被脚本按名字调用（heading(...)）、按名字查询（query），还要支持第三方包注入新元素——元素必须是运行时可寻址的数据，而不是编译期写死的类型。

元素的定义方式是声明式的：开发者写一个普通 Rust 结构体，标上 #[elem] 宏和若干能力标记（如 Show、Locatable），宏在编译期自动生成：

- 构造函数（收集 required 字段）；
- 每个字段的 getter 与 setter 方法；
- 一份 NativeElementData 元数据（元素名、标题、文档、搜索关键词），供 IDE 补全与文档站使用——这就是「内省信息的来源」。

字段分两类，对应脚本里的「必选参数」与「可选参数」：

- **required 字段**：调用者必须提供（如 heading 的 body），并且只有结构体的最后一个字段可以 required；
- **settable 字段**：有默认值，可通过 set 规则或调用参数覆盖（如 heading 的 level 默认为 1）。

#### 4.2.2 核心流程

以 heading 为例，从声明到使用的全链路：

（示例代码，节选自 model/heading.rs 的声明形态）

    #[elem(Show, Synthesize, Locatable, Countable, Referencable, Outlinable)]
    pub struct HeadingElem {
        #[required]
        pub body: Content,
        #[resolve] #[default(1)]
        pub level: unset<i64>,
    }

宏展开后（概念性描述，具体以展开结果为准）：

1. 名为 heading 的元素类型登记进全局注册表，附带 NativeElementData；
2. 生成 HeadingElem::new(body)——required 字段按位置收进构造函数；
3. 生成 level 的 getter/setter；
4. 字段名自动做 snake_case 到 kebab-case 的转换（Rust 侧写 hanging_indent，脚本侧用 hanging-indent）；
5. 能力标记（Show、Locatable 等）影响 realize 与 introspection 阶段的行为分派（u4 单元展开）。

脚本调用 heading(level: 2)[Hi] 时：构造函数从内容块取 body，从命名参数取 level（缺省回落到 default），最终产出 Content::Element。函数调用本身的参数传递机制（Args）属于 u3-l5 的主题。

#### 4.2.3 源码精读

**（1）宏的入口：统一 re-export**

见 [mod.rs:L44](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/mod.rs#L44)：elem、func、scope、ty 四个过程宏都来自 typst-macros crate，在 foundations 的装配处统一 re-export。model、text、layout 等模块只需 use foundations 里的 elem，不必直接依赖 typst-macros。

**（2）Element：类型句柄**

见 [elem.rs:L23-L32](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/elem.rs#L23-L32)：Element 结构体持有全局注册表（LazyHash&lt;Globals&gt;）的引用，并通过索引定位具体元素类型；from_index 允许只凭编号重建句柄。

（示例代码，节选自源码）

    pub struct Element {
        globals: LazyHash<Globals>,
        // 定位元素类型的索引标识
    }

**（3）NativeElementData：内省信息的家**

见 [elem.rs:L292-L299](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/elem.rs#L292-L299)：

（示例代码，节选自源码）

    pub struct NativeElementData {
        /// The element normal name (e.g. enum), as exposed to Typst.
        pub name: &static str,
        /// The element title case name (e.g. Numbered List).
        pub title: &static str,
        /// Documentation for the element.
        pub docs: &static str,
        /// The search keywords for this element.
        pub keywords: &static [&static str],
    }

说明：四个字段全部是编译期静态字符串，由宏从结构体的文档注释与宏参数中提取。IDE 补全候选、官方文档站、错误提示里的元素描述，数据源头都在这里。

**（4）宏的成文规则：elem.rs.md**

elem.rs.md 是 #[elem] 宏的官方使用文档（以 markdown 形式与代码同目录存放），几条关键规则：

- 见 [elem.rs.md:L17-L27](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/elem.rs.md#L17-L27)：被标注的 struct 成为 Typst 元素；字段即属性，字段类型必须可 cast 或可 IntoValue；
- 见 [elem.rs.md:L49-L51](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/elem.rs.md#L49-L51)：Rust 侧 snake_case 字段名自动映射为脚本侧 kebab-case 属性名；
- 见 [elem.rs.md:L62-L68](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/elem.rs.md#L62-L68)：required 字段的限制（只有最后一个字段可以 required）与可设置字段的默认值要求（实现 Default 或标注 #[default]）；
- 见 [elem.rs.md:L107-L109](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/elem.rs.md#L107-L109)：getter/setter 的生成规则（只为当前 crate 的元素生成，避免跨 crate 冒犯）；
- 见 [elem.rs.md:L136](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/elem.rs.md#L136) 起：完整的宏选项（能力标记）清单，包括 Show、Locatable、Synthesize、Countable、Referencable、Outlinable、Mounted、NotConstructible、Contextual、Uncopiable、Migrated 等。

**（5）真实样本：HeadingElem**

见 [heading.rs:L70-L77](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/model/heading.rs#L70-L77)：六个能力标记（Show 提供默认外观、Locatable 可被 query 定位、Synthesize 支持合成预处理、Countable 参与计数、Referencable 可被引用、Outlinable 可进目录），body 为 required。

（示例代码，节选自源码）

    #[elem(Show, Synthesize, Locatable, Countable, Referencable, Outlinable)]
    pub struct HeadingElem {
        #[required]
        pub body: Content,
    }

见 [heading.rs:L82-L85](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/model/heading.rs#L82-L85)：level 字段带 #[resolve] 与 #[default(1)]——前者声明「读取时需在样式链上解析」，后者给出默认值，所以它是典型的 settable 字段。

最后把（5）与 4.1.3 串起来：脚本里 heading(level: 2)[Hi] 的产物是 Content::Element；.level 字段访问走 content.rs 的 field()；而字段之所以存在、默认值之所以是 1，全部由 #[elem] 宏在编译期生成——声明与行为由此闭环。

#### 4.2.4 代码实践

**实践目标**：亲手验证 required 与 settable 字段的行为差异，并把观察结果与 heading.rs 里的声明一一对应。

**操作步骤**：

1. 新建 demo2.typ，写入以下内容：

（示例代码，typst 脚本）

    #let a = heading[Default]
    #let b = heading(level: 3)[Explicit]
    default level: #(a.level)
    explicit level: #(b.level)
    body of b: #(b.body)

2. 运行 cargo run -p typst-cli -- compile demo2.typ（或 typst compile demo2.typ）。
3. 打开生成的 PDF，记录两行 level 输出。
4. 回到 [heading.rs:L82-L85](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/model/heading.rs#L82-L85)，找到 #[default(1)] 标记，与步骤 3 的「default level」输出对照。
5. 进阶（源码阅读型）：打开 [elem.rs.md:L136](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/elem.rs.md#L136) 起的宏选项清单，为 HeadingElem 的六个能力标记（Show、Synthesize、Locatable、Countable、Referencable、Outlinable）各写一句话注释。

**需要观察的现象**：

- default level 一行输出 1（来自宏声明的默认值）；
- explicit level 一行输出 3（来自命名参数）；
- body of b 渲染出 Explicit 文本。

**预期结果**：字段访问 .level 与 .body 都能工作，证明宏生成的字段系统在脚本侧完整可用。若想看宏展开后的真实代码，可安装 cargo-expand 后运行 cargo expand -p typst-library heading。输出效果：待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 required 字段只能是结构体的最后一个字段？

参考答案：见 elem.rs.md L62-L68 的规则说明——构造函数按位置收集 required 参数，若 required 字段不在末尾，位置参数与后续字段的对应会产生歧义。这是宏强制的约定，不是 Rust 语言的限制。

**练习 2**：脚本里写的属性名是 hanging-indent，Rust 侧的字段名应该是什么？

参考答案：hanging_indent。转换规则（snake_case 与 kebab-case 互转）见 elem.rs.md L49-L51。

**练习 3**：NativeElementData 里的 docs 字段服务哪些场景？运行时有没有开销？

参考答案：docs 存放元素的文档字符串，供官方文档站生成、IDE 悬停提示、错误信息引用；它是编译期提取的静态字符串，运行时零构造开销。见 elem.rs L292-L299。

### 4.3 Selector 与 Label 体系：show 规则的匹配语言

#### 4.3.1 概念说明

有了内容和元素，还差最后一块拼图：怎么「指认」某些元素？Typst 用两个互补的概念回答——

- **Label（标签）**：给元素挂一个名字。脚本里写在元素后面，比如 = 引言 &lt;intro&gt;。数据层面它是一个驻留字符串：全局只存一份、比较只比指针，克隆零成本。
- **Selector（选择器）**：描述「什么样的元素算匹配」。脚本里的 heading、&lt;intro&gt;、heading.where(level: 2)、regex("序") 这些表达式，求值后都变成 Selector 类型的值。

Selector 有九种变体：

| 变体 | 典型脚本写法 | 语义 |
| --- | --- | --- |
| Elem(Element, Option&lt;Dict&gt;) | heading；heading.where(level: 2) | 按元素类型匹配，可选字典附加字段条件 |
| Label(Label) | &lt;intro&gt; | 按标签匹配 |
| Location(Location) | 内部表示 | 按文档中的唯一位置匹配 |
| Regex(Regex) | regex("序") | 按正则匹配文本 |
| Can(Capability) | 内部表示 | 按能力匹配 |
| Or(Vec) | a or b | 任一子选择器命中即命中 |
| And(Vec) | a and b | 全部子选择器命中才命中 |
| Before | sel.before(end) | 位置在另一元素之前 |
| After | sel.after(start) | 位置在另一元素之后 |

关键设计：Selector 是**数据**而不是回调函数。正因为它可哈希、可比较，才能参与 comemo 增量缓存；正因为它可序列化，才能安全跨越 WASM 边界（插件、web 编译器）；正因为它是一等值，脚本才能用 or、and、before、after 对它做组合运算。

此外 label.rs 还定义了 LabelError：从内容中提取标签失败的三种情形（内容没有标签 Unlabeled、类型不对 WrongType、标签不匹配 WrongLabel），供 show 规则与引用功能做精确报错。

#### 4.3.2 核心流程

一条 show 规则从书写到生效的旅程：

1. 脚本写 show heading.where(level: 2): it => text(red)[#it.body]；
2. 求值期：Selector::elem(heading) 先造出无条件的类型选择器，where 附加的字典条件让它升级为 Elem(Element, Some(Dict))；Selector::show() 再把它包装成一条 ShowRule；
3. 规则与后续内容一起被打包进 Content 的 Styled 变体；
4. realize 阶段逐元素测试：类型相等，且字典里每个键值条件都满足，才应用规则右侧的变换函数（u4-l2 展开）；
5. 若脚本写的是 query(&lt;intro&gt;)，则进入 introspection：按 Label 变体检索已布局完成的元素（u4-l7 展开）。

匹配过程的伪代码：

（示例代码，伪代码）

    fn matches(selector, elem):
        match selector:
            Elem(e, None)    => e 的类型 == elem 的类型
            Elem(e, Some(d)) => 类型相等 且 d 中每个条件都满足
            Label(l)         => elem 挂的标签 == l
            Or(ss)           => ss 中任一命中
            And(ss)          => ss 中全部命中
            ...              // 其余变体依次类推

#### 4.3.3 源码精读

**（1）Selector 枚举定义**

见 [selector.rs:L3-L24](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/selector.rs#L3-L24)：九个变体与 4.3.1 的表格一一对应。

（示例代码，节选自源码）

    pub enum Selector {
        /// Matches a specific type of element.
        Elem(Element, Option<Dict>),
        /// Matches the element at the specified location.
        Location(Location),
        /// Matches elements with a specific label.
        Label(Label),
        /// Matches text elements through a regular expression.
        Regex(Regex),
        /// Matches elements with a specific capability.
        Can(Capability),
        /// Matches if any of the subselectors match.
        Or(Vec<Self>),
        /// Matches if all of the subselectors match.
        And(Vec<Self>),
        Before { selector: Arc<Self>, end: Arc<Self>, inclusive: bool },
        After { selector: Arc<Self>, start: Arc<Self>, inclusive: bool },
    }

**（2）四个关键方法**

见 [selector.rs:L327-L336](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/selector.rs#L327-L336)：elem() 构造「只按类型匹配」的选择器，脚本里裸写 heading 求值即得 Self::Elem(elem, None)；where 附加的字段条件会填进那个 Option&lt;Dict&gt;。

（示例代码，节选自源码）

    pub fn elem(elem: Element) -> Self {
        Self::Elem(elem, None)
    }

见 [selector.rs:L341-L349](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/selector.rs#L341-L349)：show() 把选择器包装成 ShowRule——这是「选择器」通往「show 规则」的独木桥。

见 [selector.rs:L356-L363](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/selector.rs#L356-L363)：select() 在引擎与样式链的协助下，把选择器解析成一个具体的 Location（文档中的唯一位置），供 query 与引用功能使用。

见 [selector.rs:L365-L366](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/selector.rs#L365-L366)：matches() 是轻量判断入口——对 Elem 变体按元素类型直接比较（比较 Element 与内容的构造函数是否同一元素），其余变体在 realize / introspection 的完整匹配逻辑中展开。

**（3）selectors! 宏**

见 [selector.rs:L457-L462](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/selector.rs#L457-L462)：把一串值批量转换成选择器列表，支撑 query(a, b, ..) 这类接收多个选择器的函数签名。

**（4）Label：一个字段的新类型**

见 [label.rs:L27-L30](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/label.rs#L27-L30)：Label 只是对内部驻留表示的单字段包装；见 [label.rs:L104-L107](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/label.rs#L104-L107)：label! 宏让 Rust 侧以编译期常量的方式书写标签。

（示例代码，节选自源码）

    pub struct Label(Repr);

**（5）LabelError：三种失败**

见 [label.rs:L44-L53](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/label.rs#L44-L53)：

（示例代码，节选自源码）

    pub enum LabelError {
        Unlabeled,
        WrongType,
        WrongLabel(Selector),
    }

说明：从内容提取标签失败的三种情形；WrongLabel 携带 Selector，让错误信息能同时告诉用户「期望匹配什么、实际拿到什么」。

**（6）闭环**：Label 数据（label.rs）+ 挂标签方法（content.rs L1128-L1134 的 label()）+ 按标签匹配（Selector::Label 变体），三者合起来就是脚本里 = 引言 &lt;intro&gt; 与 show &lt;intro&gt;: ... 背后的全部机制。

#### 4.3.4 代码实践

**实践目标**：用三种选择器（纯类型、类型加条件、标签）匹配同一批标题，直观感受 Selector 变体的语义差异。

**操作步骤**：

1. 新建 demo3.typ：

（示例代码，typst 脚本）

    #show heading.where(level: 1): it => text(blue)[#it.body]
    #show <intro>: it => text(red)[#it]
    = 引言 <intro>
    == 背景
    = 方法

2. 编译：cargo run -p typst-cli -- compile demo3.typ。
3. 再用 query 子命令观察选择器命中了哪些元素：cargo run -p typst-cli -- query demo3.typ heading（输出的 JSON 数组长度应为 3，具体字段结构以本地输出为准）。

**需要观察的现象**：

- 一级标题「引言」「方法」显示为蓝色——Elem 加字典条件（level: 1）只命中一级标题；
- 二级标题「背景」不受影响——条件不满足；
- 「引言」同时显示为红色——标签选择器 Label(&lt;intro&gt;) 只命中挂了该标签的那一个；
- query 输出的数组长度是 3——纯类型选择器 Elem(heading, None) 命中全部标题。

**预期结果**：三种写法对应 Selector 的三种形态，匹配范围依次收窄（全部 3 个 → 一级 2 个 → 标签 1 个）。query 的 JSON 输出与编译效果：待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：show heading.where(level: 2) 中的 where 条件存储在 Selector 的什么位置？

参考答案：Elem 变体的第二个成员 Option&lt;Dict&gt;。带 where 时是 Some(条件字典)，裸写 heading 时是 None。见 selector.rs L3-L24。

**练习 2**：Label 为什么用驻留字符串而不是普通 String？

参考答案：每次 show / query 匹配都要比较标签。驻留后比较退化为指针比较、克隆零成本，高频路径开销显著降低；代价只是创建时的一次全局查表。见 label.rs L1-L30。

**练习 3**：query(heading) 与 query(&lt;intro&gt;) 分别对应哪个 Selector 变体？后者为什么需要多轮编译？

参考答案：分别是 Elem(heading, None) 与 Label(&lt;intro&gt;)。按标签（或位置）查询依赖已布局文档提供的 Location 元数据，因此 introspection 必须等第一遍排版完成后才能回答，编译器会迭代布局直到结果收敛——细节在 u4-l7 展开。

## 5. 综合实践

把本讲三个模块串成一个「带档案卡的迷你报告」任务：一篇三个标题的小文档，要求同时用到元素构造、字段访问、标签、三类选择器匹配与 query 查询。

**任务要求**：编写 report.typ，满足——

1. 三个一级标题，其中第一个挂上 &lt;start&gt; 标签；
2. 用 show heading.where(level: 1) 把一级标题统一改成加粗放大；
3. 用 show &lt;start&gt; 给第一个标题套一个红色边框；
4. 文末用 context 与 query 统计标题数量；
5. 编译成功后，再用 query 子命令导出 JSON 与文档内统计互相印证。

参考骨架（示例代码，typst 脚本）：

    #show heading.where(level: 1): it => text(weight: "bold", size: 1.2em)[#it.body]
    #show <start>: it => box(stroke: red, inset: 4pt)[#it]

    = 引言 <start>
    这里是引言。
    = 方法
    这里是方法。
    = 结论
    这里是结论。

    #context {
      let heads = query(heading)
      [本文共 #heads.len() 个标题。]
    }

操作：cargo run -p typst-cli -- compile report.typ，然后 cargo run -p typst-cli -- query report.typ heading。

观察点与知识点对照：

| 现象 | 对应知识点 |
| --- | --- |
| 三个标题全部加粗放大 | Selector 的 Elem 加 Dict 条件匹配（selector.rs L3-L24） |
| 只有第一个标题有红框 | Label 变体精确命中（label.rs + content.rs L1128-L1134） |
| it.body 取出标题文字 | Content::field 按名取字段（content.rs L151） |
| 文末统计数字 3 | query 经 introspection 收集已布局元素（u4-l7 伏笔） |
| query JSON 数组长度 3 | 同上，命令行侧验证 |

验收标准：编译成功、视觉效果符合上述表格、文档内统计与 query 输出一致；并且你能向同伴口头复述这条链路——heading(...) 构造 Content::Element，show 规则用 Selector 匹配元素，匹配后规则函数拿到的 it 就是原内容的 Content。具体输出效果：待本地验证。

## 6. 本讲小结

- **Content 是文档的统一表示**：五个变体（Text、Element、Styled、Empty、Sequence）以最小正交集合覆盖从单个字符到整篇文档的全部形态；它不可变、可哈希，因此天然适配增量编译缓存。
- **Element 是元素类型的运行时句柄**：凭全局注册表加索引定位类型、随实例携带字段值；脚本能按名字调用元素、query 能按名字检索元素，靠的都是这套运行时可寻址设计。
- **#[elem] 宏是声明式的元素工厂**：从结构体声明自动生成构造函数、每字段的 getter/setter、以及 NativeElementData 元数据；required 与 settable 之分，就是脚本里「必选参数 / 可选参数」在数据层的真相。
- **字段读写的落点**：脚本里的 .level 访问对应 Content::field（content.rs L151）；通用写入口是 with_field / push_field（L1088 / L1115）。
- **Label 与 Selector 构成匹配语言**：驻留字符串标签配合 Content::label 挂载；九变体选择器可组合（or / and / before / after），show() 负责把选择器升级成 show 规则。
- **测试即样板**：content.rs 文件末尾的单元测试（L1182 起）展示了用 Rust 构造、改写、检查 Content 的地道写法，值得反复模仿。

## 7. 下一步学习建议

- **下一讲 u3-l5（函数是一等公民：Func、Args 与类型转换 cast）**：本讲反复出现「构造函数」却没展开——heading(level: 2)[Hi] 的参数如何经 Args 消费协议填进宏生成的构造函数，正是下一讲的主菜。
- **u3-l7（样式系统：Styles 与 set/show 的数据模型）**：本讲的 Styled 变体只是个包装，样式链的查找优先级、set 与 show 在数据层的不同形态在那里展开。
- **u4-l2（realize 阶段）**：show 规则如何被逐元素应用、Selector::matches 的完整匹配逻辑如何被调用，属下一单元。
- **源码延伸阅读**：通读 elem.rs.md（宏规则的权威文档）；在 model 目录再挑两个元素（如 list.rs、figure.rs）对照 HeadingElem，巩固「看声明就能推断生成物」的读码能力。

（本讲完）
