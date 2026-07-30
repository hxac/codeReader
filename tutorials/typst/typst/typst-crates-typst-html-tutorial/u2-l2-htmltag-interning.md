# HtmlTag 标签与字符串驻留

## 1. 本讲目标

上一讲（u2-l1）我们把 `HtmlElement` 的七个字段逐个拆开，其中第一个字段就是：

```rust
pub tag: HtmlTag,
```

当时我们只说了一句「`HtmlTag` 本质是一个被驻留（intern）的标签名」，把细节全部推迟到了本讲。本讲就来把这个「标签名」彻底讲透。

读完本讲，你应当能够：

- 说清**字符串驻留（string interning）**是什么、为什么 typst-html 要用它来表示标签名，以及 `PicoStr` 用了哪三种内部表示来兼顾「省内存」与「编译期可用」。
- 逐字符追踪 `HtmlTag::intern` 的校验逻辑，特别是**含连字符的标签会被当作自定义元素（custom element）**这一关键分支，并列出合法自定义元素名必须满足的全部条件。
- 区分 `intern` / `constant` / `resolve` 三个方法：哪些是「创建」、哪个是「取回」，分别在什么时机用、失败时如何表现。
- 解释 `charsets::is_valid_in_tag_name` 为什么把字符约束定义得这么窄，以及它和属性名约束 `is_valid_in_attribute_name` 的本质区别。

本讲只看一个很小的类型 `HtmlTag`，但它是整棵 DOM 树里出现频率最高的「原子」——每一个 `HtmlElement` 都带一个。理解了它，你才能理解为什么后续转换器、编码器里到处可以直接用 `==` 比较标签。

## 2. 前置知识

进入源码前，先建立三个直觉。

**字符串驻留（string interning）。** 一份 HTML 文档里，标签名的取值集合极小：`div`、`span`、`p`、`li`……满打满算也就一百多个标准标签，再加上少量用户自定义元素。但它们会在树里**反复出现成千上万次**（每一个 `<li>` 都是一个 `HtmlElement`，`tag` 字段都等于 `"li"`）。如果每次都存一份完整的 `String`，既浪费内存，比较两个标签时还得逐字符扫描。

字符串驻留的思路是：**全局只存一份字符串内容，到处只传它的「句柄」（handle / id）**。这样：

- 同一个标签名在整棵树里只占一份字符串内存；
- 比较两个标签变成「比句柄（一个整数）」，O(1)；
- 句柄本身可以做到很小（typst 里是 8 字节）、可 `Copy`。

`HtmlTag` 内部就是这个句柄。typst 把这套通用机制实现在了 `typst-utils` crate 的 `PicoStr` 类型里，`HtmlTag` 只是它的一层薄包装。

**`const fn` 与「编译期 vs 运行时」。** Rust 的 `const fn` 可以在编译期求值。typst-html 在 `src/tag.rs` 里用 `pub const div: HtmlTag = HtmlTag::constant("div");` 这样的写法，把所有标准标签在**编译期**就驻留好，变成程序里的常量。这意味着这些标签名在运行时「零成本」——既不查表也不分配。而用户在 Typst 脚本里临时写的 `html.elem("my-widget")` 则是**运行时**才到达的字符串，必须走另一条带校验、可能失败的路径。本讲的核心张力就是这两条路径的对比。

**HTML 标签名、属性名、文本，三者的字符约束各不相同。** 这一点很容易忽略：`<div>` 的标签名只允许字母、数字和连字符；属性名（如 `data-x`）允许的字符多得多；而元素文本里连 `<` 都不能直接出现。typst-html 把这三套规则分别放在 `charsets.rs` 里。本讲只看标签名那一套，但我们会顺手对比一下属性名约束，体会「为什么不能共用一个函数」。

> 回顾：`EcoString` / `EcoVec` 是 ecow 提供的写时复制容器（u2-l1 已介绍）。本讲里 `PicoStr` 是另一种字符串策略——它不是「写时复制」，而是「全局唯一句柄」，目的类似但实现更激进。

## 3. 本讲源码地图

本讲围绕一个类型展开，涉及四个文件：

| 文件 | 作用 | 本讲关注 |
| --- | --- | --- |
| `src/dom.rs` | 定义 `HtmlTag` 类型及其全部方法（`intern` / `constant` / `resolve` / `into_inner`）、`Display`、`cast!` | `HtmlTag` 的定义与方法 |
| `src/charsets.rs` | 定义 HTML 各类语法成分的字符有效性规则 | `is_valid_in_tag_name`（并对比 `is_valid_in_attribute_name`） |
| `src/tag.rs` | 用 `HtmlTag::constant` 预定义全部标准标签常量 | `constant` 的实际用法 |
| `crates/typst-utils/src/pico.rs` | `PicoStr` 驻留机制的通用实现（`HtmlTag` 的内部类型） | 驻留原理背景：`intern` / `constant` / `resolve` / bitcode |

可以这么记关系：`charsets.rs` 给出「什么是合法标签名」，`dom.rs` 的 `HtmlTag` 在这之上加上「自定义元素」等额外规则并调用 `PicoStr`，`pico.rs` 负责真正的「全局只存一份」，`tag.rs` 则是 `constant` 路径的最大消费者。

## 4. 核心概念与源码讲解

本讲按「先看类型本身 → 看它依赖的字符约束 → 看运行时创建路径 → 看编译期创建路径」的顺序，拆成四个最小模块：`HtmlTag`、`charsets::is_valid_in_tag_name`、`HtmlTag::intern`、`HtmlTag::constant`。

### 4.1 HtmlTag：标签名的句柄包装

#### 4.1.1 概念说明

`HtmlTag` 是一个**新型别名（newtype）**：它在结构上只有一个字段，就是那个驻留句柄 `PicoStr`：

```rust
#[derive(Copy, Clone, Eq, PartialEq, Hash)]
pub struct HtmlTag(PicoStr);
```

为什么要单独包一层，而不是直接全代码用 `PicoStr`？两个理由：

1. **语义清晰**：`PicoStr` 是「任意被驻留的字符串」，可能是标签名、属性名，也可能是别的东西；而 `HtmlTag` 明确表示「这是一个 HTML 标签名」。类型系统帮我们把「一个标签」和「一个属性」区分开，不会把 `HtmlTag` 误传给需要 `HtmlAttr` 的地方。
2. **承载校验**：`PicoStr` 本身不校验内容（任何字符串都能驻留），而 `HtmlTag` 的构造方法会强制「这必须是合法的 HTML 标签名」。校验逻辑放在这一层，既不污染通用的 `PicoStr`，又能保证「只要你拿到一个 `HtmlTag`，它就一定是合法的」。

它的派生属性也值得注意：`Copy` + `Clone` 意味着传递一个 `HtmlTag` 只是复制 8 字节，无需考虑所有权；`Eq` + `PartialEq` + `Hash` 意味着它可以做相等比较、可以放进哈希表——而这三个操作都因为「句柄比较」而极其廉价。

#### 4.1.2 核心流程

一个 `HtmlTag` 的一生：

1. **创建**：通过 `intern`（运行时，带完整校验）或 `constant`（编译期，带基本校验）从字符串得到。二者最终都落到 `PicoStr::intern` / `PicoStr::constant` 上。
2. **复制与比较**：作为 `HtmlElement::tag` 随元素到处传递；比较时直接比内部的 `PicoStr`（整数级）。
3. **取回字符串**：需要输出或调试时，用 `resolve()` 把句柄还原成字符串切片；或用 `into_inner()` 拿到底层的 `PicoStr` 交给其它也用 `PicoStr` 的代码。
4. **与 Typst 值互转**：通过 `cast!` 宏，`HtmlTag` 可以和 Typst 脚本里的字符串（`Str`）互转——这正是 `html.elem("div")` 能成立的桥梁。

#### 4.1.3 源码精读

类型定义只有一行，但派生项信息量很大：

[`HtmlTag` 类型定义 — dom.rs:L249-L251](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs#L249-L251) — newtype 包装 `PicoStr`；`Copy/Clone/Eq/PartialEq/Hash` 全部派生，因为 `PicoStr` 本身是一个 8 字节的、可按整数比较哈希的句柄。

`resolve` 是「取回字符串」的入口，它直接转发给 `PicoStr::resolve`：

[`HtmlTag::resolve — dom.rs:L333-L336](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs#L333-L336) — 把句柄解码回 `ResolvedPicoStr`（一个借出字符串切片的类型）。注意它消费 `self`（因为是 `Copy`，消费的只是 8 字节副本）。

`into_inner` 则把 `HtmlTag` 拆开，交出底层的 `PicoStr`，方便与其它使用 `PicoStr` 的代码互通：

[`HtmlTag::into_inner — dom.rs:L338-L341](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs#L338-L341) — `const fn`，零成本拆包装。

`Display` 实现决定了调试时 `HtmlTag` 长什么样——它会带尖括号输出，例如 `<div>`：

[`Display for HtmlTag — dom.rs:L350-L354](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs#L350-L354) — 用 `<{resolve()}>` 格式化；`Debug` 直接复用 `Display`（见上一段 L344-L348），所以 `dbg!` 一个元素时你能直接看到 `<div>` 这样的标签。

最后是连接 Typst 脚本世界的 `cast!`：

[`cast! for HtmlTag — dom.rs:L356-L360](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs#L356-L360) — 从 `HtmlTag` 转 `Value` 时先 `resolve()` 成字符串；从 `Str` 转 `HtmlTag` 时调用 `Self::intern(&v)?`。**这一行就是用户写的 `html.elem("my-widget")` 进入校验的入口**——字符串在这里被 `intern` 检查，不合法就变成 Typst 文档里的运行时错误。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**，目标是验证「句柄比较」真的成立。

1. **实践目标**：确认两个内容相同、但分别由不同路径创建的 `HtmlTag`，比较结果相等。
2. **操作步骤**：
   - 打开 `src/dom.rs`，找到 `HtmlTag` 的定义与 `cast!`。
   - 设想这样一段 Rust（**示例代码**，非项目原有）：

     ```rust
     // 一个来自编译期常量
     let a: HtmlTag = tag::div;
     // 一个来自运行时字符串（绕过 cast!，直接调 intern）
     let b = HtmlTag::intern("div").unwrap();
     assert_eq!(a, b); // 会通过吗？
     ```
   - 阅读下文 4.4 里 `PicoStr::intern` 的实现：它会先尝试 `try_constant`（bitcode / 异常表），而 `"div"` 由纯小写字母组成、长度 ≤ 12，能被 bitcode 内联编码——与 `tag::div` 走的 `PicoStr::constant("div")` 产出**同一个 64 位编码**。
3. **需要观察的现象**：`assert_eq!(a, b)` 应当通过，即两者内部 `PicoStr` 的 64 位值完全一致。
4. **预期结果**：相等成立。这说明「标准短标签」无论从常量来还是从字符串来，最终都是同一个句柄，比较是 O(1) 的整数比较。
5. 若要在本地真正运行，需要把 `typst-html` 作为依赖写一个 `#[test]`；否则记为「待本地验证」——但结论可由 `pico.rs` 的 `intern` 实现直接推出。

#### 4.1.5 小练习与答案

**练习 1**：`HtmlTag` 为什么派生 `Hash`？这对后续代码有什么直接好处？

> **答案**：因为 `PicoStr` 内部是一个 `NonZeroU64`，对整数求哈希既快又确定。派生 `Hash` 后，`HtmlTag` 可以作为 `HashMap` / `HashSet` 的键。后续如内省器、规则分派（`rules.rs` 里按标签找 show 规则）都需要按标签快速查表。

**练习 2**：`Display` 把 `HtmlTag` 格式化成 `<div>`（带尖括号），而 `cast!` 里「`HtmlTag` → `Value`」却返回不带尖括号的 `"div"`。这两个看似矛盾的输出为什么都合理？

> **答案**：它们服务于不同目的。`Display`/`Debug` 面向**人类阅读调试**，带尖括号一眼就能看出「这是个标签」，例如在错误信息里写「元素 `<div>` ……」。而 `cast!` 负责**与 Typst 值互转**，Typst 脚本里的标签名就是不带尖括号的字符串 `"div"`，所以转回 `Value` 时要去掉尖括号、还原成原始字符串。

### 4.2 charsets::is_valid_in_tag_name：标签名的字符约束

#### 4.2.1 概念说明

在讲 `intern` 的校验之前，先看它依赖的最底层谓词：`is_valid_in_tag_name`。它回答一个极简单的问题——「这个字符能不能出现在 HTML 标签名里？」

这个函数之所以单独存在、放在 `charsets.rs`，是因为 HTML 规范对不同语法成分的字符要求差别巨大。把每套规则写成独立的 `const fn`，既贴近规范、又方便在不同校验点复用。标签名的约束是**最严格**的之一。

#### 4.2.2 核心流程

判定的决策非常短，可以一字不漏地描述：

- 若字符是 ASCII 字母或数字（`a-z`、`A-Z`、`0-9`）→ 合法；
- 若字符是连字符 `-` → 合法；
- 其它一切字符（空格、`/`、`:`、中文、emoji……）→ 非法。

注意它**允许大写字母**。这一点很重要：`is_valid_in_tag_name` 只管「字符种类」，不管「大小写语义」。大小写相关的规则（自定义元素必须全小写）是 `intern` 在这一层之上额外加的，本模块（4.3）会详细讲。

#### 4.2.3 源码精读

整个函数只有一行逻辑：

[`is_valid_in_tag_name — charsets.rs:L3-L6](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/charsets.rs#L3-L6) — `c.is_ascii_alphanumeric() || c == '-'`。`const fn` 使它能在 `constant`（编译期）和 `intern`（运行时）两条路径下被同一个函数复用。

作为对比，看看属性名约束宽松到什么程度：

[`is_valid_in_attribute_name — charsets.rs:L8-L19](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/charsets.rs#L8-L19) — 只显式禁止少数几个控制字符（`\0`、空格、`"`、`'`、`>`、`/`、`=`）以及 WHATWG 定义的「非字符」和控制字符，**其它一切皆允许**（注释甚至写「Go wild.」）。这正是两个函数不能合并的原因：标签名是严格白名单，属性名是宽松黑名单。

#### 4.2.4 代码实践

1. **实践目标**：体会标签名与属性名字符约束的巨大落差。
2. **操作步骤**：阅读上面两个函数，然后判断下列字符在「标签名」和「属性名」中分别是否合法：`:`、`@`、空格、`-`、中文字符「标」。
3. **需要观察的现象**：填出一张二行五列的小表。
4. **预期结果**（依据源码直接推出）：

   | 字符 | 标签名 | 属性名 |
   | --- | --- | --- |
   | `:` | ❌（非字母数字/连字符） | ✅（不在黑名单） |
   | `@` | ❌ | ✅ |
   | 空格 | ❌ | ❌（黑名单） |
   | `-` | ✅ | ✅ |
   | 「标」(U+6807) | ❌（非 ASCII） | ✅（不在黑名单，也非控制/非字符） |

5. 这张表完全由源码推出，无需运行；如需验证可在本地写一个调用这两个 `pub const fn` 的小测试。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `is_valid_in_tag_name` 要写成 `const fn`，而很多普通校验函数不需要？

> **答案**：因为它要同时服务于 `HtmlTag::constant`（一个 `const fn`，在编译期执行）和 `HtmlTag::intern`（运行时）。`const fn` 内部只能调用其它 `const fn`，所以这个谓词必须是 `const fn`，否则编译期路径用不了它。

**练习 2**：一个标签名 `"data:x"` 会被 `is_valid_in_tag_name` 接受吗？如果单独用这个谓词判断，结论和 HTML 规范一致吗？

> **答案**：`:` 不是 ASCII 字母数字也不是 `-`，所以 `is_valid_in_tag_name` 会拒绝 → 不被接受。这与 HTML 规范一致（HTML 标签名不允许 `:`，`:` 只在 XML/Namespaces 语境出现）。但要注意：`intern` 并不是「只」调用这个谓词，它在通过该谓词后，对含 `-` 的标签还有额外的「自定义元素」规则（见 4.3）。

### 4.3 HtmlTag::intern：运行时驻留与自定义元素校验

#### 4.3.1 概念说明

`intern` 是用户输入进入 typst-html 的**主入口**。当你在 Typst 里写 `html.elem("my-widget")[...]`，那个字符串 `"my-widget"` 会经由 `cast!`（4.1.3 已看到）调到 `HtmlTag::intern`。

它做两件事：

1. **校验**：确认这个字符串是合法的 HTML 标签名。校验分两层——先用 `is_valid_in_tag_name` 检查每个字符，再对「含连字符的标签」施加一套更严格的**自定义元素命名规范**。
2. **驻留**：校验通过后，调 `PicoStr::intern(string)` 把字符串登记进全局驻留表，拿到句柄，包成 `HtmlTag` 返回。

关键设计：`intern` 返回的是 `StrResult<Self>`，即**失败时返回错误而不是 panic**。这很合理——用户输入是运行时才到达的，一个非法标签名应当是一次「文档编译错误」，而不是让整个编译器崩溃。

#### 4.3.2 核心流程

用伪代码描述 `intern` 的判定流程：

```
fn intern(string) -> StrResult<HtmlTag>:
    if string 为空:           报错 "tag name must not be empty"

    has_hyphen = false
    has_uppercase = false
    for c in string:
        if c == '-':           has_hyphen = true        # 记下：这是自定义元素候选
        elif not is_valid_in_tag_name(c):  报错 "字符 c 非法"
        else:                  has_uppercase |= c 是大写  # 记下：是否含大写

    # —— 进入「自定义元素」额外校验（仅当含连字符）——
    if has_hyphen:
        if not 首字符是 ASCII 小写字母:  报错 "custom element name must start with a lowercase letter"
        if has_uppercase:               报错 "custom element name must not contain uppercase letters"
        if string 属于 8 个保留名:       报错 "name is reserved and not valid for a custom element"

    Ok(HtmlTag(PicoStr::intern(string)))
```

这里有一个**容易被忽略的细节**：`-` 本身是 `is_valid_in_tag_name` 允许的字符，所以循环里对 `-` 单独走第一个分支并不是为了「校验合法性」，而是为了**置位 `has_hyphen` 标记**，从而触发后续的自定义元素分支。换句话说，含连字符的标签在 typst-html 眼里**一律按自定义元素对待**。

另一个值得注意的点：**只有自定义元素才被强制小写**。一个不含连字符、但含大写字母的标签名（如 `"DIV"`）会通过 `intern`——因为大写字母是 `is_valid_in_tag_name` 允许的，而 `has_hyphen` 为假，不会进入强制小写的分支。这是源码的真实行为（见练习）。

#### 4.3.3 源码精读

方法开头先排除空串，然后进入逐字符循环：

[`HtmlTag::intern 开头与逐字符循环 — dom.rs:L254-L271](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs#L254-L271) — 循环里用三个分支分别处理 `-`、非法字符、合法字符（顺带统计大写）。注意 `-` 分支不调用 `is_valid_in_tag_name`（虽然调了也会通过），它的唯一作用是置 `has_hyphen = true`。

紧接着是全篇最关键的注释，完整列出了合法自定义元素名的规范依据：

[`自定义元素命名规范注释 — dom.rs:L273-L283](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs#L273-L283) — 引用 WHATWG HTML 规范，列出五条要求。这段注释本身就是一份精确的「验收清单」，下面的代码就是逐条实现它。

三条校验依次落地：

[`自定义元素三条校验 — dom.rs:L284-L307](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs#L284-L307) — ①首字符必须是小写字母；②不得含大写字母；③不得是 8 个保留名之一。保留名 `annotation-xml` / `color-profile` / `font-face` / `font-face-src` / `font-face-uri` / `font-face-format` / `font-face-name` / `missing-glyph` 都是 SVG / MathML 的外协元素名——`html.elem` 只用于创建 **HTML** 元素，所以它们被禁用。

全部通过后才真正驻留：

[`驻留并返回 — dom.rs:L309](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs#L309) — `Ok(Self(PicoStr::intern(string)))`。校验与驻留职责分离：`HtmlTag` 管「合法性」，`PicoStr` 管「全局唯一」。

#### 4.3.4 代码实践（本讲主任务）

这是本讲规格要求的实践：阅读 `intern` 中针对含连字符标签的校验分支，列出合法自定义元素名必须满足的条件，并各构造一个会被接受、一个会被拒绝的例子。

1. **实践目标**：把 `intern` 的自定义元素规则变成可预测的判断能力。
2. **操作步骤**：
   - 重读 [dom.rs:L273-L307](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs#L273-L307)，整理出合法自定义元素名的全部条件。
   - 据此预测下列 Typst 片段的行为（**示例代码**）：

     ```typst
     // A：预测「接受」
     #html.elem("my-widget")[hello]

     // B：预测「拒绝」
     #html.elem("my-Widget")[hello]
     ```
3. **整理条件清单**（合法自定义元素名必须同时满足）：
   - 至少含一个连字符 `-`（这是进入该分支的前提，由 `has_hyphen` 标记）；
   - 以一个 ASCII 小写字母 `a-z` 开头；
   - 不含任何 ASCII 大写字母 `A-Z`；
   - 不是 8 个保留名（`font-face` 等）之一；
   - 全部字符都通过 `is_valid_in_tag_name`，即只含 ASCII 字母、数字和连字符。
4. **需要观察的现象**：
   - **A `html.elem("my-widget")`** → 应被**接受**：含 `-`、首字母 `m` 小写、无大写、非保留名、字符全合法。最终生成 `<my-widget>hello</my-widget>`。
   - **B `html.elem("my-Widget")`** → 应被**拒绝**：含 `-` 进入自定义元素分支，但 `W` 是大写 → 命中 L288-L290，报错 `custom element name must not contain uppercase letters`。
5. **更多可对照的拒绝例子**（均据源码推出）：
   - `html.elem("1-widget")` → 首字符 `1` 非小写字母 → `custom element name must start with a lowercase letter`（L285-L287）。
   - `html.elem("font-face")` → 命中保留名 → `name is reserved and not valid for a custom element`（L294-L306）。
6. **预期结果**：A 接受、B 拒绝，错误信息与上面标注的分支一致。可在本地用 `typst compile --format html` 实际编译上面片段验证；若暂无环境，结论已由源码逐行确定，记为「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`html.elem("DIV")[...]`（不含连字符、全大写）会被接受还是拒绝？为什么？

> **答案**：**接受**。`D/I/V` 都是 `is_valid_in_tag_name` 允许的 ASCII 字母；`has_hyphen` 为假，所以根本不会进入「自定义元素」那套强制小写的校验。typst-html 对**非自定义**标签不强制大小写。这是「读源码读出来的反直觉结论」——仅凭直觉容易误判它会被拒。

**练习 2**：为什么 `intern` 返回 `StrResult` 而 `constant` 失败时 `panic`？两者面对的「调用方」有何不同？

> **答案**：`intern` 处理的是**运行时到达的用户输入**（来自 Typst 脚本），非法输入是常态之一，应当变成可恢复的「文档编译错误」并附带友好提示，所以用 `StrResult`。`constant` 处理的是**编译期常量**（写在 `tag.rs` 里的标准标签），它们由 typst 开发者维护，若是非法那属于「程序 bug」，应当尽早暴露——在编译期 `panic` 正是「fail fast」，能在编译 typst-html 本身时立刻发现错误。

**练习 3**：`html.elem("-")`（单个连字符）会发生什么？

> **答案**：循环里 `-` 置 `has_hyphen = true`；随后进入自定义元素分支，`starts_with(is_ascii_lowercase)` 对首字符 `-` 为假 → 报错 `custom element name must start with a lowercase letter`。即被拒绝。

### 4.4 HtmlTag::constant：编译期常量

#### 4.4.1 概念说明

`constant` 是 `intern` 的「编译期孪生兄弟」。typst-html 把全部标准标签（一百多个）预先写死在 `src/tag.rs` 里，每个都是一行：

```rust
pub const div: HtmlTag = HtmlTag::constant("div");
```

这些常量在程序启动前就已经存在，运行时使用它们**零分配、零查表**。`constant` 是 `const fn`，意味着它在编译期求值；它的校验比 `intern` 更**基础**——只检查「字符是否合法」，**不**检查自定义元素规则（因为标准标签常量里没有自定义元素）。

关键约束：`constant` 失败时 `panic`。这一点和 `intern` 返回错误形成鲜明对比，原因见练习 2。

#### 4.4.2 核心流程

`constant` 自身的流程很薄：

```
fn constant(string) -> HtmlTag:   // const fn
    if string 为空:                panic "tag name must not be empty"
    for each byte b in string:
        if b 非 ASCII 或 not is_valid_in_tag_name(b):  panic "not all characters are valid"
    HtmlTag(PicoStr::constant(string))
```

但真正的精彩在它转交的 `PicoStr::constant` 里——typst 用了一套**位编码（bitcode）+ 异常表**的机制，让绝大多数标准标签的字符串内容**直接内联进那个 64 位整数**，连全局驻留表都不用查。这是「编译期驻留」能做到零成本的根本原因。

`PicoStr` 的 64 位有三种解读（由最高位和一个异常表区分）：

1. **bitcode 内联**：最高位（bit 63）为标记位。长度 ≤ 12、且只含 `a-z` / `1-4` / `-` 的字符串，会被 5 位一组压进整数。`div`、`span`、`h1` 这类都能这样存。
2. **异常表**：能编译期确定、但不满足 bitcode 字符集的字符串（例如 `h5`、`h6`，因为 `5`/`6` 不在内联字符集里），会被预先登记在 `exceptions::LIST`，用一个小整数下标表示。
3. **运行时驻留表**：`PicoStr::intern` 才会用到。前两种都搞不定时，去全局 `INTERNER` 表里登记或查找。

理解这一点后，你就能解释 4.1.4 里的现象：`tag::div`（走 `constant`，bitcode 内联）和 `HtmlTag::intern("div")`（走 `intern`，但 `intern` 会先 `try_constant`，同样命中 bitcode 内联）**得到完全相同的 64 位值**，所以两者 `==` 相等。

#### 4.4.3 源码精读

`HtmlTag::constant` 用手写的 `while` 循环（因为 `const fn` 里不能用 `for` 迭代字节）逐字节校验：

[`HtmlTag::constant — dom.rs:L312-L331](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs#L312-L331) — 注意两处 `panic!`：空标签、含非 ASCII 或非法字符的标签。校验通过后调 `PicoStr::constant(string)`。它**没有**自定义元素分支——标准标签常量里不存在自定义元素，也无需在编译期承担用户输入的校验职责。

它的最大消费者是 `tag.rs`，里面是一长串常量定义：

[`tag.rs 常量定义示例 — tag.rs:L36-L60](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/tag.rs#L36-L60) — `pub const div`、`pub const h1` … 一行一个标准标签，全部用 `HtmlTag::constant("...")` 在编译期生成。这些常量就是后续转换器（`convert.rs`）、规则（`rules.rs`）、编码器（`encode.rs`）里 `tag::div`、`tag::span` 这类写法的来源。

要理解「为什么这能零成本」，必须看 `PicoStr`。先看它的驻留机制背景（位于 `typst-utils` crate）：

[`PicoStr 类型与三种内部表示 — pico.rs:L25-L40](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-utils/src/pico.rs#L25-L40) — 文档说明：8 字节、可 `Copy`、`Option<PicoStr>` 也是 8 字节（null-optimized）；支持两种编译期内联（bitcode 与 exceptions），运行时则无限制。

运行时 `intern` 会优先复用编译期表示：

[`PicoStr::intern — pico.rs:L43-L68](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-utils/src/pico.rs#L43-L68) — 先 `try_constant`（命中 bitcode / 异常表就直接返回，**无需查全局表**）；都命中不了才加写锁查 `INTERNER`，若仍没有就把字符串 `Box::leak` 永久驻留并登记。这正是 4.1.4 里「常量与运行时字符串相等」的根源。

编译期 `constant` 则严格依赖 `try_constant`：

[`PicoStr::constant 与 try_constant — pico.rs:L87-L118](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-utils/src/pico.rs#L87-L118) — `constant` 失败时调 `failed_to_compile_time_intern`（即 panic）。`try_constant` 先试 bitcode（成功则置最高标记位），失败则查异常表，再不行才返回 `Err`。

bitcode 的字符集决定了哪些标签能被内联：

[`bitcode 编码字符表 DECODE — pico.rs:L145-L151](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-utils/src/pico.rs#L145-L151) — `DECODE = "\0abcdefghijklmnopqrstuvwxyz-1234"`。也就是说内联字符集是 `a-z`、`-`、`1-4`。由此可推出：`tag.rs` 里的 `h5`、`h6`（含 `5`/`6`，不在内联字符集）无法走 bitcode，要作为编译期常量存在，就必须事先登记进 `exceptions::LIST`——否则 `constant("h5")` 会在编译 typst-html 时 panic，整个 crate 根本编不出来。这是从源码契约推出的必然结论。

最后，`resolve` 是 `intern`/`constant` 的逆操作，把句柄还原成字符串：

[`PicoStr::resolve — pico.rs:L120-L136](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-utils/src/pico.rs#L120-L136) — 最高位置位则 bitcode 解码；否则按下标从 `exceptions::LIST` 或运行时 `INTERNER.strings` 取回字符串。

把三种「字符串 ↔ HtmlTag」交互方式放在一起对比：

| 方法 | 方向 | 时机 | 失败行为 | 典型用途 |
| --- | --- | --- | --- | --- |
| `HtmlTag::intern(s)` | `str → HtmlTag` | 运行时 | 返回 `Err`（StrResult） | 用户在脚本里写的标签 |
| `HtmlTag::constant(s)` | `str → HtmlTag` | 编译期 | `panic` | `tag.rs` 里的标准标签常量 |
| `HtmlTag::resolve()` | `HtmlTag → str` | 运行时 | 不会失败 | 输出/调试时取回字符串 |

#### 4.4.4 代码实践

1. **实践目标**：亲手确认「标准短标签经两条路径得到同一个句柄」并理解 bitcode 字符集的边界。
2. **操作步骤**：
   - 阅读 [`pico.rs:L145-L151`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-utils/src/pico.rs#L145-L151) 的 `DECODE` 表，列出哪些字符能被 bitcode 内联。
   - 判断 `tag.rs` 里下列常量分别走哪种内部表示：`div`、`h1`、`h5`、`h6`。
3. **需要观察的现象 / 预期结果**（据源码契约推出）：
   - `div`：字符 `d/i/v` ∈ `a-z`，长度 3 ≤ 12 → **bitcode 内联**。
   - `h1`：`h` ∈ `a-z`，`1` ∈ `1-4` → **bitcode 内联**。
   - `h5`：`5` ∉ 内联字符集 → 无法 bitcode；既然它作为 `constant("h5")` 出现在 `tag.rs` 且 crate 能编译，它**必然**登记在 `exceptions::LIST` → 走**异常表**表示。
   - `h6`：同理 → **异常表**。
4. 想要进一步验证，可在本地 `typst-utils` 里 `println!` 出 `PicoStr::constant("h5").into_inner()` 与 `PicoStr::constant("div").into_inner()` 的原始整数，观察前者无 bitcode 标记位、后者有。若暂无环境，记为「待本地验证」，但归属判断已由源码确定。
5. 另一个思考题（不必运行）：若有人误把 `pub const foo: HtmlTag = HtmlTag::constant("标题");`（含非 ASCII 中文）加进 `tag.rs`，会发生什么？
   > 据 [dom.rs:L323-L326](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs#L323-L326)，非 ASCII 字节会触发 `panic!("not all characters are valid in a tag name")`，且因为 `constant` 是 `const fn`，这个 panic 发生在**编译 typst-html 时**——这正是「fail fast」的价值。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `HtmlTag::constant` 的循环写成 `while i < bytes.len()` 手动索引，而不是 `for c in string.chars()`？

> **答案**：因为 `constant` 是 `const fn`，而（在定义它时）`for` 对字符串/切片的迭代在 `const` 上下文里不可用；手动 `while` + 字节索引是 `const fn` 里遍历字符串的惯用写法。`intern` 不是 `const fn`，所以可以用 `for c in string.chars()`。

**练习 2**：`constant("font-face")` 会成功吗？它和 `intern("font-face")` 的结果一样吗？

> **答案**：`constant("font-face")` 会**成功**。`f/o/n/t/-/f/a/c/e` 全是 ASCII 字母和 `-`，都通过 `is_valid_in_tag_name`；`constant` 不检查自定义元素保留名（那是 `intern` 才有的规则）。而 `intern("font-face")` 会**失败**——它含 `-` 进入自定义元素分支，并命中保留名检查报错。这说明同一字符串在两条路径下的「合法性」并不相同：`constant` 只做基本字符校验，`intern` 额外承担用户输入的语义校验。

**练习 3**：`PicoStr::intern` 为什么要「先试 `try_constant`，再查全局表」？

> **答案**：两个好处。一是**省锁**：能被 bitcode 或异常表表示的字符串（绝大多数常见标签）直接算出句柄，根本不必碰全局 `INTERNER` 的读写锁，无锁争用。二是**与常量一致**：让运行时创建的句柄与编译期 `constant` 产生的句柄**数值相同**，从而 `tag::div == HtmlTag::intern("div").unwrap()` 成立（即 4.1.4 的现象）。只有既不能内联、又不在异常表里的字符串（典型是用户的长自定义元素名）才落到全局表。

## 5. 综合实践

把本讲四个模块串起来，完成一次「标签名合法性审查」。

**任务**：假设你要给 typst-html 写一个小工具，对一批用户提交的标签名做静态预测，分类成「标准标签（能匹配 `tag.rs` 常量）」「合法自定义元素」「非法」。请基于本讲源码，给出下列 8 个名字的分类，并写出非法者的具体报错分支：

`"section"`、`"my-chart"`、`"My-Chart"`、`"color-profile"`、`"h6"`、`""`（空串）、`"data:x"`、`"a-"`。

**参考答案**（全部依据 [dom.rs:L254-L310](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs#L254-L310) 与 [charsets.rs:L3-L6](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/charsets.rs#L3-L6) 推出）：

| 名字 | 分类 | 依据 / 报错分支 |
| --- | --- | --- |
| `section` | 标准标签 | 不含 `-`，全合法；与 `tag::section` 常量相等 |
| `my-chart` | 合法自定义元素 | 含 `-`、首字母小写、无大写、非保留、字符合法 |
| `My-Chart` | **非法** | 含 `-` 但首字符 `M` 非小写 → L285-L287 |
| `color-profile` | **非法** | 含 `-` 且命中保留名 → L294-L306 |
| `h6` | 标准标签 | 不含 `-`，全合法；对应 `tag::h6`（异常表表示） |
| ``（空串） | **非法** | L256-L258 `tag name must not be empty` |
| `data:x` | **非法** | `:` 非 `is_valid_in_tag_name` → L266-L267 |
| `a-` | 合法自定义元素 | 含 `-`、首字母 `a` 小写、无大写、非保留、字符合法 |

**延伸**：挑出两个「合法自定义元素」，用 `html.elem` 包成 Typst 片段，在本地 `typst compile --format html` 验证它们确实产出对应标签；再挑两个「非法」验证报错信息与上表分支一致。无本地环境则记为「待本地验证」，但分类结论已由源码逐行确定。

## 6. 本讲小结

- `HtmlTag` 是 `PicoStr` 的一层 newtype：把「任意驻留字符串」收窄为「合法 HTML 标签名」，并 `Copy/Eq/Hash` 全派生，使标签比较成为廉价的整数比较。
- `charsets::is_valid_in_tag_name` 是最底层的字符白名单：只允许 ASCII 字母、数字和 `-`；它与属性名的宽松黑名单 `is_valid_in_attribute_name` 形成鲜明对比，二者不能合并。
- `HtmlTag::intern` 是用户输入的主入口，**返回 `StrResult`**：先逐字符校验，再对含 `-` 的标签施加自定义元素命名规范（首字母小写、不含大写、非保留名）。
- `HtmlTag::constant` 是编译期路径，**失败 `panic`**：只做基本字符校验，供 `tag.rs` 把一百多个标准标签预定义为零成本常量。
- `PicoStr` 用「bitcode 内联 + 异常表 + 运行时全局表」三种表示，让绝大多数标签的句柄直接内联进 8 字节；`intern` 会先 `try_constant`，所以运行时与编译期创建的同名标签**数值相等**。
- `resolve()` 是 `intern`/`constant` 的逆操作，把句柄还原成字符串；`cast!` 则让 `HtmlTag` 与 Typst 脚本里的字符串互转，`html.elem("...")` 正是经此进入 `intern` 校验。

## 7. 下一步学习建议

下一篇 **u2-l3「HtmlAttr 与 HtmlAttrs 属性系统」** 是本讲的天然对照：`HtmlAttr` 在结构上几乎和 `HtmlTag` 一模一样（同样是 `PicoStr` 的 newtype、同样有 `intern`/`constant`/`resolve`），所以你已经掌握的驻留机制可以直接迁移。重点会转向**不同**的部分——属性名用更宽松的 `is_valid_in_attribute_name` 校验，且 `HtmlAttrs` 作为列表带 `Fold` 合并语义与 `Dict` 互转。

如果你想先把「驻留」这条线看到底，可以直接去读 `crates/typst-utils/src/pico.rs` 的完整实现（特别是 `bitcode` 模块的 `encode`/`decode` 和 `exceptions` 列表），那是一个独立于 HTML 的、设计相当精巧的通用字符串驻留库。

之后，u2-l4 会回到 `tag.rs`，讲解这些常量之外的内容模型分类函数（`is_void`/`is_raw`/`is_flow_content` 等）——它们决定了「拿到一个合法 `HtmlTag` 之后，编码器和转换器该如何对待它」。
