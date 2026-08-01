# 二次开发指南与测试

## 1. 本讲目标

本讲是 typst-syntax 学习手册的收官篇。前面九个单元我们分别读懂了词法器、解析器、CST、Span、AST、Source、增量重解析、文件身份与高亮。本讲不再介绍新的运行机制，而是站在「我想给 Typst 加一个新语法」的视角，把前面学过的零件**串成一条改动链**，并讲清这个 crate 的**测试组织方式**。

学完后你应当能够：

1. 说出新增一个语法构造（一个 `SyntaxKind` 变体）时，必须依次改动哪些源码文件、每处改什么。
2. 理解为什么 `name()`、`mode_after()`、`highlight()` 三处会被编译器**强制**提醒——它们都是 `SyntaxKind` 的穷尽 `match`，没有通配符。
3. 说明新增 kind / parser 规则对**高亮**（新 `Tag`）和**增量重解析**（reparse 覆盖范围）的连带影响。
4. 知道 typst-syntax 的测试以**各源码文件末尾的内联 `#[cfg(test)]` 模块**为主，且 parser.rs / lexer.rs **没有**自己的内联测试——它们的正确性靠消费方（node.rs、highlight.rs、kind.rs、reparser.rs）调用 `crate::parse` 来验证。

## 2. 前置知识

本讲默认你已经读过：

- **u2-l1 / u2-l2 / u2-l3**：`SyntaxKind` 枚举全貌、`is_*` 分类方法、`SyntaxSet` 位集。其中 u2-l1 提到一个硬约束——当前共 137 个变体（判别值 0–136），而 `SyntaxSet` 基于 `u128` 只能装下判别值 < 128 的 kind，**末尾 9 个结构节点变体（`ImportItems`…`DestructAssignment`）无法入集**。这个约束在本讲会反复出现。
- **u4-l3 / u4-l2**：markup 解析主循环 `markup_expr` 如何按当前 token 的 `SyntaxKind` 分发到 `strong()`、`heading()` 等函数；以及 `marker()` → `eat` → `wrap()` 的事后圈子树模式。
- **u7-l3**：AST 节点如何用 `node!` 宏声明 + 独立 `impl` 块从 CST 子节点抽取语义（以 `Raw`、`Heading` 为例）。
- **u9-l3**：增量重解析的 `reparse_markup` / `reparse_block` 两个钩子，以及「当前只重解析顶层与 content block 内的 markup，不重解析列表/标题内部 markup、也不重解析 math」这一取舍。
- **u10-l3**：高亮是 CST 之上的一道只读工序，`highlight()` 是按 `SyntaxKind` 主分派的纯函数，`Tag` 是高亮的「调色板」。

两个贯穿全讲的术语：

- **判别值（discriminator）**：`#[repr(u8)]` 下每个枚举变体的整数编号，从 0 开始递增。
- **穷尽 match（exhaustive match）**：Rust 中 `match` 一个枚举时若没有 `_ =>` 通配符，就必须列出全部变体；缺一个就编译失败。这是 typst-syntax 强制「改一处不忘其它」的关键武器。

## 3. 本讲源码地图

本讲涉及的源码文件及其在本讲中的角色：

| 文件 | 本讲中的角色 |
| --- | --- |
| `src/kind.rs` | 改动链的**起点**：声明新 `SyntaxKind` 变体，并补全 `name()` 与 `mode_after()` 两个穷尽 match。 |
| `src/lexer.rs` | 若新构造需要新 token，在这里教词法器在对应模式（markup/code/math）下识别它。 |
| `src/parser.rs` | 写解析规则：在主分发函数里加一支，再写一个类似 `strong()` 的解析函数。 |
| `src/set.rs` | 若新 token 要参与「能否起始某类表达式」的判断，把它加进预定义 `SyntaxSet` 常量。 |
| `src/ast.rs` | 用 `node!` 宏声明类型化 AST 节点，并写语义访问方法。 |
| `src/highlight.rs` | 给新节点/新颜色加 `Tag` 变体，补全 `highlight()` 这第三个穷尽 match。 |
| `src/reparser.rs` | 评估新构造对增量重解析覆盖范围的影响（多数情况下无需改动）。 |
| `src/node.rs` | 测试 CST 结构的内联测试所在地（`test_debug` 等）。 |

## 4. 核心概念与源码讲解

### 4.1 新增 SyntaxKind 的流程：在 kind.rs 立名

#### 4.1.1 概念说明

typst-syntax 里词法器产出的 token 与解析器构建的 CST 节点**共用同一套词汇表**，这就是 `SyntaxKind` 枚举。要新增任何语法构造，第一件事永远是在 `kind.rs` 里给它**立一个名字**——一个枚举变体。这个变体一旦加上，会立刻在另外两个地方「欠债」：`name()`（人类可读名）和 `mode_after()`（该节点之后的语法模式）。好消息是，Rust 编译器会替你记住这两笔债。

#### 4.1.2 核心流程

在 `kind.rs` 增加一个变体的最小流程：

1. 在 `SyntaxKind` 枚举里、按所属类别（Markup / Math / Code）插入一个变体，写好文档注释。
2. 注意它的**判别值**：变体在枚举里的位置决定判别值。若希望它将来能进 `SyntaxSet`（参与「能否起始某类表达式」的位集判断），其判别值必须 < 128，因此应插在 `ImportItems`（当前第一个判别值 ≥ 128 的结构节点）**之前**。
3. 编译器会报错指出 `name()` 与 `mode_after()` 两处 `match` 不再穷尽——逐一补上对应分支。

伪代码：

```
// 1) 加变体（注意位置 → 决定判别值）
/// A spoiler block: ||secret||
Spoiler,

// 2) 补 name()（编译器强制）
Self::Spoiler => "spoiler",

// 3) 补 mode_after()（编译器强制）
Self::Spoiler => Known(SyntaxMode::Markup),
```

#### 4.1.3 源码精读

`SyntaxKind` 是 `#[repr(u8)]` 的紧凑枚举，注释明确「可由 lexer 或 parser 创建」：[src/kind.rs:3-8](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/kind.rs#L3-L8)。它的变体按通用（End/Error/注释）、Markup、Math、Code 四段排列，**位置即判别值**。

`name()` 是一个穷尽 `match`，把每个 kind 翻译成可读英文字符串，供诊断系统拼接错误消息——注意它**没有 `_ =>` 通配符**，逐个变体列出：[src/kind.rs:386-526](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/kind.rs#L386-L526)。这意味着你新增一个变体，这里会立刻编译失败，逼你补上名字。

`mode_after()` 决定「紧跟在这个 kind 节点之后应处于哪种 `SyntaxMode`」，同样是穷尽 `match`、没有通配符：[src/kind.rs:570-725](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/kind.rs#L570-L725)。它返回私有的 `ModeAfter` 枚举（`Known(mode)` / `Parent` / `None` 等），服务于 IDE 的「光标处是什么模式」查询。新增变体时必须决定它之后该是什么模式——比如一个新的 markup 包裹节点，就和 `Strong`、`Emph` 一样填 `Known(SyntaxMode::Markup)`：[src/kind.rs:596-597](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/kind.rs#L596-L597)。

而 `set.rs` 的 `add` 用 `assert!((kind as u8) < BITS)`（`BITS = 128`）在编译期把判别值 ≥ 128 的 kind 挡在 `SyntaxSet` 之外：[src/set.rs:20-23](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/set.rs#L20-L23)。所以「新变体插在哪个位置」不只是美观问题，而是能否被位集使用的能力问题。

> **一句话直觉**：在 kind.rs 立名 = 加一个变体 + 还两笔编译器替你记着的债（`name`、`mode_after`），并顺便决定它「够不够格」进位集（看判别值）。

#### 4.1.4 代码实践

**实践目标**：亲手验证「编译器强制补全穷尽 match」这件事。

**操作步骤**（这是源码阅读 + 局部编辑型实践，**请在仓库的一个临时分支上进行，结束后丢弃，不要提交**）：

1. 打开 `src/kind.rs`，在 Markup 段、`Emph` 之后插入一个变体：
   ```rust
   /// A spoiler block: ||secret||.
   Spoiler,
   ```
2. 运行 `cargo build -p typst-syntax`。
3. 观察编译器报错：它会精确指出 `name()` 与 `mode_after()` 两个 `match` 缺少 `Spoiler` 分支。

**需要观察的现象**：

- 编译失败信息里会列出未覆盖的非穷尽 `match`，**正好是这两处**（外加 `highlight.rs` 的 `highlight()`，见 4.2）。
- 这印证了 typst-syntax 把「新增 kind 必须同步更新所有消费方」做成了**编译期硬约束**，而非靠人记。

**预期结果**：在 `name()` 加 `Self::Spoiler => "spoiler",`、在 `mode_after()` 加 `Self::Spoiler => Known(SyntaxMode::Markup),` 后，这两处报错消失（`highlight()` 那处仍报错，留给 4.2）。本步骤的编辑仅用于观察编译器行为，确认后请用 `git checkout src/kind.rs` 还原，避免污染源码。

#### 4.1.5 小练习与答案

**练习 1**：为什么 typst-syntax 选择让 `name()` 和 `mode_after()` 用**穷尽 match**而不是 `_ => "unknown"` 这样的通配兜底？

**答案**：穷尽 match 把「新增变体必须被显式处理」变成编译错误。一旦有人加了新 kind 却忘了给它命名或忘了指定其后继模式，构建直接失败，杜绝了「静默地用了一个错误的名字/模式」这类隐患。通配兜底会让遗漏顺滑地通过编译，是故意的反模式。

**练习 2**：假设你要加的新结构节点 `ImportItems`、`DestructAssignment` 那一类，判别值会 ≥ 128，这对它有什么限制？

**答案**：它将**无法被加入任何 `SyntaxSet`**，因为 `SyntaxSet::add` 在编译期 `assert!(kind < 128)`。所以若该新 kind 需要参与 parser 的 `at_set(...)` 决策（如「能否起始某类表达式」），就必须把它的声明位置挪到 `ImportItems` 之前，使其判别值 < 128。

---

### 4.2 parser 规则 + AST 节点 + 高亮联动

#### 4.2.1 概念说明

立完名（kind.rs）后，要让这个构造真正能被解析、能被类型化访问、能被着色，需要依次打通三处：

1. **parser.rs**：在主分发函数加一支（指向新的解析函数），并写出该解析函数——通常仿照最相近的现有构造（成对包裹仿 `strong()`，行首结构仿 `heading()`）。
2. **ast.rs**：用 `node!` 宏声明类型化包装结构体，并写语义访问方法，让下游求值层能安全地取数据。
3. **highlight.rs**：给新构造选一个 `Tag`（必要时新增 `Tag` 变体），并补全 `highlight()` 这个穷尽 match。

这三处加上 set.rs（按需），构成「让一个新构造从文本到着色端到端可用」的核心改动。

#### 4.2.2 核心流程

以**假想的新 markup 构造「spoiler」** `||secret||`（成对双竖线包裹，语义类似 `*strong*`）为贯穿示例。声明它需要两个 kind：定界符 token `SpoilerDelim`（lexer 产出）与结构节点 `Spoiler`（parser 产出），分别对应 `Star` 与 `Strong` 的角色。

```
# 假想语法：||这段是剧透||

改动链（端到端）：
kind.rs      : 加 SpoilerDelim(<128)、Spoiler 节点；补 name()、mode_after()
lexer.rs     : markup 模式识别 "||" → 产出 SpoilerDelim token
parser.rs    : markup_expr 加分支 SpoilerDelim => spoiler(p)
               仿 strong() 写 spoiler()：marker→assert→嵌套 markup→expect_closing→wrap(Spoiler)
set.rs       : 把 SpoilerDelim 加进 spoiler() 内层 markup 的 stop_set（仿 Star 的位置）
ast.rs       : node!{ struct Spoiler } + impl Spoiler { fn body(self)->Markup }
highlight.rs : （可选）新增 Tag::Spoiler，补 LIST/tm_scope/css_class 与 highlight() 分支
```

`spoiler()` 解析函数的伪代码（逐行对应 `strong()`）：

```
fn spoiler(p):
    with_nl_mode(StopParBreak):           # 允许跨行，遇段尾停
        m = p.marker()                    # 记位置戳（不含 trivia）
        p.assert(SpoilerDelim)            # 吃开界定界符
        markup(p, false, true,            # 解析内部 markup，trivia 无损圈入
               syntax_set!(SpoilerDelim, RightBracket, End))  # 遇闭界定界符停
        p.expect_closing_delimiter(m, SpoilerDelim)
        p.wrap(m, SyntaxKind::Spoiler)    # 事后圈成 Spoiler 节点
```

#### 4.2.3 源码精读

**parser 主分发**。`markup_expr` 按当前 token 的 `SyntaxKind` 选分支，`Star => strong(p)`、`Underscore => emph(p)`、`HeadingMarker if at_start => heading(p)` 等：[src/parser.rs:90-134](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L90-L134)。新构造的入口分支就加在这里——spoiler 加 `SyntaxKind::SpoilerDelim => spoiler(p)`。

**最佳模板 `strong()`**。成对包裹型构造几乎照抄它即可：`with_nl_mode(StopParBreak, ...)` 允许跨行；`marker()` 记戳；`assert(Star)` 吃开界；内层 `markup(..., true, ...)` 第二个布尔 `wrap_trivia=true` 把 trivia 无损圈入；`expect_closing_delimiter` 处理未闭合；最后 `wrap(m, Strong)`：[src/parser.rs:137-151](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L137-L151)。注意它用 `syntax_set!(Star, RightBracket, End)` 作为内层 markup 的停止集——spoiler 应换成 `syntax_set!(SpoilerDelim, RightBracket, End)`，这就用到了 set.rs 的位集。

**行首结构模板 `heading()`**。若新构造是行首触发（而非成对定界），仿它：`with_nl_mode(Stop, ...)` 单行结束，`assert(HeadingMarker)`，`markup(..., false, ...)`，`wrap`：[src/parser.rs:171-178](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L171-L178)。

**AST 节点声明**。`node!` 宏凭「结构体名 == `SyntaxKind` 变体名」的约定，自动生成 `&'a SyntaxNode` 的透明包装、kind 守门的 `from_untyped`、`to_untyped` 与 `placeholder`：[src/ast.rs:165-196](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/ast.rs#L165-L196)。所以声明 `node! { struct Spoiler }` 后，`SyntaxKind::Spoiler` 节点就能被 `cast` 成 `Spoiler<'_>`。然后仿 `Heading` 写语义方法——`Heading::body()` 用 `cast_first()` 取唯一 markup 子节点（缺失回退 `placeholder`）：[src/ast.rs:797-811](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/ast.rs#L797-L811)。spoiler 同样只需一个 `body(self) -> Markup`。

> 取子节点的三类范式（u7-l3 已总结）：叶子用 `leaf_text()`；唯一子节点用 `cast_first`/`cast_last`；多个用 `children().filter_map(cast)`。AST 方法依赖 parser 产出的**固定子结构**这一契约；若结构不符，用 `placeholder` 兜底而非 panic（ast.rs 顶部 `#![deny(clippy::unwrap_used, ...)]` 把「绝不 panic」定为编译期约束）。

**高亮联动**。`highlight()` 是按 `SyntaxKind` 主分派的穷尽 match，**没有通配符**，末尾以 `SyntaxKind::End => None` 收尾：[src/highlight.rs:142-311](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/highlight.rs#L142-L311)。新增 `SpoilerDelim`/`Spoiler` 变体后，这里会编译报错，逼你给颜色。若新构造的着色能用现有 `Tag` 表达（如新关键字直接用 `Tag::Keyword`），只需加一行映射；若需要独有颜色，就要新增 `Tag` 变体，并同步更新三处一一对应的输出：`Tag::LIST`（按下标）、`tm_scope()`（TextMate 作用域）、`css_class()`（网页 CSS 类）：[src/highlight.rs:5-50](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/highlight.rs#L5-L50)、[src/highlight.rs:56-136](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/highlight.rs#L56-L136)。

> **三处编译器强制的穷尽 match 汇总**：`kind.rs::name()`、`kind.rs::mode_after()`、`highlight.rs::highlight()`。新增任何 `SyntaxKind` 变体，这三处必然同时报错——这就是 typst-syntax 的「防遗忘」骨架。

#### 4.2.4 代码实践

**实践目标**：把 4.1 的 spoiler 在 parser 与 ast 层「接上线」，验证 CST 里能长出 `Spoiler` 节点（**纯源码阅读 + 设计型，不要求真改源码**）。

**操作步骤**：

1. 阅读 `strong()`：[src/parser.rs:137-151](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L137-L151)，逐行写出它如何用 marker→assert→markup→expect_closing→wrap 构造 `Strong`。
2. 阅读 `Heading` AST 节点：[src/ast.rs:797-811](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/ast.rs#L797-L811)，确认 `body()` 用 `cast_first()`。
3. 在纸上（或临时分支）写出 spoiler 的三件套：parser 分支 + `spoiler()` 函数 + `node!{ struct Spoiler }` 与 `impl Spoiler { fn body }`。
4. 若已临时改了源码，用 `crate::parse("||x||")` 在 node.rs 的测试风格下打印 CST（参考 4.4 的 `test_debug`），确认出现 `Spoiler` 节点；确认后 `git checkout` 还原。

**需要观察的现象**：

- spoiler 的内部 markup 子节点结构与 strong 完全同构：定界符 + 内层 `Markup` + 定界符。
- 若忘记给 `highlight()` 加 `Spoiler` 分支，编译会在 `highlight.rs` 报「non-exhaustive match」。

**预期结果**：仿照 strong/heading，spoiler 的 parser 函数不超过 10 行，AST 节点声明 + 一个 `body()` 方法即可。高亮分支若沿用 `Tag::Strong` 的颜色可省去新 `Tag`，但更合理的是新增 `Tag::Spoiler` 以便编辑器单独配色。

#### 4.2.5 小练习与答案

**练习 1**：`spoiler()` 里为什么不写 `p.marker()` 之后立刻 wrap，而要先 assert、解析内部、再 wrap？

**答案**：因为 typst-syntax 用「事后圈子树」模式——`marker()` 只记下当前 `nodes` 向量的下标位置戳，函数入口时还不知道子树会有多少节点、会不会因错误恢复而变形。等内部全部 eat 完，再用 `wrap(m, kind)` 把从戳到当前位置的所有节点打包成一个内部节点。这让函数不必在入口承诺边界，利于错误恢复与增量重解析（u4-l2）。

**练习 2**：如果 spoiler 想支持 `||多行\n内容||`，`with_nl_mode` 该用哪个 `AtNewline` 变体？参考 strong。

**答案**：用 `AtNewline::StopParBreak`——它允许跨普通换行继续，只在遇到段落分隔（空行）时停止，正是 `strong()` 的选择（[src/parser.rs:138](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L138)）。若用 `Stop` 则一行就结束，无法跨行。

**练习 3**：新增 `Tag::Spoiler` 后，必须同步改哪几处才不会留下隐患？

**答案**：必须同步：`Tag::LIST` 数组（按下标对应 `tag as usize`）、`tm_scope()` 的 match、`css_class()` 的 match（[src/highlight.rs:56-136](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/highlight.rs#L56-L136)）。漏改 `LIST` 会导致 `tag as usize` 越界或错位；漏改 `tm_scope`/`css_class` 会让该 tag 在编辑器或网页里没有对应作用域/类名。

---

### 4.3 增量重解析的连带影响

#### 4.3.1 概念说明

新增一个 markup 构造后，还要想一件事：**用户在编辑器里敲键盘时，typst-ide 的增量重解析能不能正确处理它？** 增量重解析（reparse）的目标是只重解析编辑点附近的一小段文本，保住其余节点的 Span 不变，从而命中下游缓存（u9）。它并不需要为新构造写专门代码——但它**只覆盖特定范围**，覆盖不到的地方会回退到全量解析，因此要确认新构造落在覆盖范围内、且重解析的「成功判据」不被破坏。

#### 4.3.2 核心流程

回忆 reparse 的覆盖规则（u9-l2/l3）：

```
try_reparse 自顶向下找「完全包住编辑范围」的最内层节点：
  路径 A: 单个 block 子节点（CodeBlock/ContentBlock）→ reparse_block 整块重解析
  路径 B: 顶层 或 ContentBlock 内的 Markup → reparse_markup 重解析表达式序列

不在覆盖范围（会触发全量回退，但仍正确）：
  - 列表项 / 标题 *内部* 的 markup（缩进换行边界易错，已刻意移除）
  - math 的任何局部重解析（未实现）
```

对新构造的影响判断：

- 若新构造是**顶层或 content block 内的 markup 表达式**（如 spoiler `||..||` 直接写在正文里）→ 落在 `reparse_markup` 路径 B 覆盖范围内，**通常无需改动**，因为它会被当作一个普通 markup 表达式参与增量重解析。
- 若新构造**只出现在列表项/标题内部** → 不被局部重解析，编辑它会回退全量，结果仍正确，只是慢一点。
- 关键纪律：新构造的解析必须**保持定界符平衡**（开界与闭界成对出现），因为 `reparse_block`/`reparse_markup` 的成功判据之一就是 `p.balanced`。

#### 4.3.3 源码精读

`reparse` 是入口调度器：先尝试 `try_reparse` 局部增量，失败则 `unwrap_or_else` 回退到全量 `parse` + `numberize`，返回 `0..text.len()` 表示「整树重做」：[src/reparser.rs:15-29](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/reparser.rs#L15-L29)。

覆盖范围的取舍写在 `try_reparse` 的文档注释里——明确说「当前只重解析顶层或 content block 内的 markup，**不**重解析列表/标题内部的 markup（曾实现但因缩进换行边界 bug 被移除）」，也「不重解析 math」：[src/reparser.rs:42-54](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/reparser.rs#L42-L54)。这段注释是评估新构造 reparse 影响时的权威依据。

`reparse_markup` 的成功判据是 `p.balanced && p.current_start() == range.end`——定界符平衡且重解析恰好消费到预期边界：[src/parser.rs:64-82](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L64-L82)。`reparse_block` 同理用 `p.balanced && p.prev_end() == range.end`：[src/parser.rs:749-755](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L749-L755)。`balanced` 是只降不升的布尔位，分隔符失衡即翻 false（u4-l5）。

> **结论**：只要新构造的解析规则**成对消费定界符**（仿 `strong()` 用 `expect_closing_delimiter`），它就不会破坏 `balanced` 判据；只要它出现在顶层/content block 正文里，就被 reparse 覆盖。多数情况下新增 markup 构造**对 reparser.rs 零改动**——这正是把 reparse 设计成「复用同一套 parser」的好处。

#### 4.3.4 代码实践

**实践目标**：判断假想 spoiler 在不同位置的增量重解析行为。

**操作步骤**：

1. 阅读覆盖范围注释：[src/reparser.rs:42-54](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/reparser.rs#L42-L54)。
2. 阅读成功判据：[src/parser.rs:81](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L81)。
3. 对两个用例给出预测：
   - 用例 A：正文 `Some ||secret|| text`，在 `secret` 中间插入字符。
   - 用例 B：列表项 `- item ||secret||`，在 `secret` 中间插入字符。

**需要观察的现象 / 预期结果**：

- 用例 A：编辑点落在顶层 Markup 的 spoiler 表达式内，`reparse_markup` 路径 B 覆盖 → 局部增量成功，返回范围远小于全文。
- 用例 B：spoiler 在列表项内部，当前实现不重解析列表项内 markup → `try_reparse` 在该层返回 `None`，向外层扩展仍可能命中（顶层 Markup 路径 B 会把整个列表项作为表达式重解析）；最坏回退全量。**无论哪条路径，结果都正确**，区别只在性能。

若想实测，可在临时分支实现 spoiler 后，参照 `src/reparser.rs` 测试模块（见 4.4）的 `Edit` 枚举写一个增量测试，对比增量与全量的 CST 是否一致。

#### 4.3.5 小练习与答案

**练习 1**：为什么 spoiler 仿 `strong()` 用 `expect_closing_delimiter` 对 reparse 很重要？

**答案**：`expect_closing_delimiter` 保证开界与闭界定界符成对处理，使解析结束时 `p.balanced` 保持 true。而 `reparse_markup`/`reparse_block` 的成功判据之一正是 `p.balanced`；若新构造会让分隔符失衡（比如只吃开界不收闭界），增量重解析会判失败而频繁回退全量，拖慢增量编译。

**练习 2**：如果你新增的是一个 **math** 构造（公式内部的新算符），reparse 会怎样？

**答案**：当前实现**完全不局部重解析 math**（[src/reparser.rs:52-54](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/reparser.rs#L52-L54)）。所以编辑公式内的任何内容都会让 `try_reparse` 在该层失败并向上回退，最终多半回退到全量解析。结果仍正确，只是没有增量收益——这是已知取舍，注释里还提到「把 equation 当作另一种 block 来重解析并不太难，可作为未来工作」。

---

### 4.4 各模块内联测试组织方式

#### 4.4.1 概念说明

typst-syntax 的测试几乎全部以**源码文件末尾的内联 `#[cfg(test)] mod`** 形式存在，本 crate **没有** `tests/` 集成测试目录。一个关键且容易踩坑的事实：**`src/parser.rs` 和 `src/lexer.rs` 自身没有内联测试模块**——解析器与词法器的正确性，是靠**消费它们产物的模块**（node.rs、highlight.rs、kind.rs、reparser.rs、source.rs）调用 `crate::parse` 后做断言来间接验证的。理解这一点，才知道新增构造时该把测试写在哪里。

#### 4.4.2 核心流程

各文件的测试职责与「该把新测试加在哪」：

```
kind.rs      mod test   → mode_after 的模式正确性（test_mode_after，经 parse + leaf_at）
node.rs      mod tests  → CST 结构正确性（test_debug：parse 后比对整棵树的 Debug 输出）
highlight.rs mod tests  → 着色正确性（test_highlighting：parse + highlight 比对 (Range, Tag) 列表）
reparser.rs  mod tests  → 增量重解析正确性（对比增量 vs 全量 CST 是否一致）
source.rs    mod tests  → Source 的 span↔字节范围 反查
parser.rs    （无）     → 由上面各模块经 crate::parse 间接覆盖
lexer.rs     （无）     → 同上
```

新增 spoiler 这类 markup 构造时，最自然的测试落点是 **node.rs 的 `test_debug` 风格**（验证 CST 形状）和 **highlight.rs 的 `test` 辅助函数**（验证着色）。运行方式统一为 `cargo test -p typst-syntax`。

#### 4.4.3 源码精读

**parser/lexer 无自有测试**。在 `src/` 下搜索 `#[cfg(test)]`，命中 ast.rs、node.rs、set.rs、path.rs、package.rs、highlight.rs、span.rs、kind.rs、lines.rs、reparser.rs、source.rs——**唯独没有 parser.rs 与 lexer.rs**。本 crate 根目录也只有 `Cargo.toml`、`README.md`、`src/`，没有 `tests/` 目录。所以解析行为的回归测试落在消费方。

**CST 结构测试范本 `test_debug`**（node.rs）：直接 `crate::parse("= Head <label>")` 然后断言整棵树的 `{:#?}` Debug 输出，连每个节点的 kind、文本、长度都精确比对——这是验证「新构造长出了正确的 CST」最直接的方式：[src/node.rs:1487-1525](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L1487-L1525)。

**高亮测试范本 `test_highlighting`**（highlight.rs）：内部 `test(text, goal)` 辅助函数先 `crate::parse(text)`，再递归 `highlight` 收集 `(Range<usize>, Tag)` 列表，与期望比对。新增 `Tag::Spoiler` 后，应在此加一条 `test("||x||", &[(.., Spoiler)])`：[src/highlight.rs:438-485](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/highlight.rs#L438-L485)。

**位集测试范本 `test_set`**（set.rs）：验证 `SyntaxSet::add`/`contains` 的基本行为，是加新预定义集合时的参考：[src/set.rs:160-166](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/set.rs#L160-L166)。

**增量重解析测试范本**（reparser.rs）：用 `Edit` 枚举描述插入/替换位置，用 `Reparse::All`/`Incr` 断言是全量还是增量，并对比两种路径产出的树是否一致：[src/reparser.rs:295-348](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/reparser.rs#L295-L348)。

> **测试纪律**：新构造至少加一条「CST 结构」断言（node.rs 风格）和一条「着色」断言（highlight.rs 风格）。若构造影响模式切换，还要在 kind.rs `test_mode_after` 加用例。`#[track_caller]` 标注的辅助函数会让失败时直接定位到断言调用处，便于排查。

#### 4.4.4 代码实践

**实践目标**：学会读现有测试、并为假想 spoiler 写一条解析测试。

**操作步骤**：

1. 读 `test_debug`：[src/node.rs:1487-1525](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L1487-L1525)，理解它如何用 `{:#?}` 锁定整棵 CST。
2. 读 highlight.rs 的 `test` 辅助：[src/highlight.rs:443-448](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/highlight.rs#L443-L448)。
3. 在纸上为 spoiler 设计一条测试（**示例代码，非项目原有**）：

   ```rust
   // 示例代码：放在 src/node.rs 的 mod tests 内
   #[test]
   fn test_spoiler() {
       // 假定 spoiler 已实现：||x|| → Spoiler{ SpoilerDelim, Markup{Text}, SpoilerDelim }
       assert_eq!(
           format!("{:#?}", crate::parse("||x||")),
           "\
   Markup: 4 [
       Spoiler: 4 [
           SpoilerDelim: \"||\",
           Markup: 1 [
               Text: \"x\",
           ],
           SpoilerDelim: \"||\",
       ],
   ]"
       );
   }
   ```

4. 在临时分支真正实现 spoiler 后，运行 `cargo test -p typst-syntax test_spoiler`，调整期望直到通过；结束后还原源码。

**需要观察的现象**：

- `{:#?}` 会把每个节点的 kind、长度、文本（叶子）或子节点（内部）层层缩进打印，是核对 CST 形状的最直观工具。
- `cargo test -p typst-syntax` 只跑本 crate 的测试，反馈快。

**预期结果**：一条精确的 Debug 字符串断言能同时验证 spoiler 的三层结构与子节点顺序。若 spoiler 解析规则写错（如忘了 `wrap`），Debug 输出会立刻与期望不符，定位明确。**待本地验证**：上面 Debug 字符串的具体缩进与长度值，需以你实际实现的 spoiler 在本机跑出的输出为准。

#### 4.4.5 小练习与答案

**练习 1**：为什么 parser.rs 没有自己的 `#[cfg(test)]` 模块却依然有测试覆盖？

**答案**：因为 node.rs 的 `test_debug`、highlight.rs 的 `test_highlighting`、kind.rs 的 `test_mode_after`、reparser.rs 的增量测试都先调用 `crate::parse(...)`，把 parser 的输出当作输入来断言。解析器产出错误 CST 时，这些消费方测试会先失败。这是一种「以消费方驱动」的测试组织——避免在 parser.rs 里重复一遍又一遍的 parse 调用样板。

**练习 2**：运行单个测试函数（如 `test_debug`）用什么命令？

**答案**：`cargo test -p typst-syntax test_debug`。`-p typst-syntax` 限定只在本 crate 内运行，后面的串是测试名过滤器（可只匹配部分名）。

---

## 5. 综合实践

**任务**：设计一个假想的新语法构造，走完「从 kind 到测试」的完整改动链。

请选择一个**成对定界的 markup 内联构造**（例如 spoiler `||..||`、mark 高亮等，自选其一并自定界符），完成下表。要求每处给出**具体改动要点**（函数名 / 变体名 / 分支写法），而非泛泛而谈。

| 文件 | 改动要点 |
| --- | --- |
| `src/kind.rs` | 新增哪几个变体？判别值是否 < 128（能否进位集）？在 `name()` / `mode_after()` 各加什么分支？ |
| `src/lexer.rs` | 在哪个模式（markup/code/math）下识别？产出哪个 token？ |
| `src/parser.rs` | 在 `markup_expr` 加哪一支？新解析函数仿 `strong()` 还是 `heading()`？内层 `markup` 的 stop_set 是什么？ |
| `src/set.rs` | 是否需要把新 token 加入某个预定义集合或 `syntax_set!`？ |
| `src/ast.rs` | `node!{ struct ? }` 声明 + 哪些语义方法？取子节点用哪种范式？ |
| `src/highlight.rs` | 复用现有 `Tag` 还是新增？若新增，列出 `LIST`/`tm_scope`/`css_class` 三处的改动；在 `highlight()` 加什么分支？ |
| `src/reparser.rs` | 新构造出现在顶层/列表项/标题/math 内时，reparse 分别会怎样？是否需要改动？ |
| 测试 | 在哪个文件的 `mod tests` 加测试？写出至少一条 CST 结构断言（`{:#?}` 风格）。 |

**自检清单**：

- [ ] 三处穷尽 match（`name`、`mode_after`、`highlight`）都补了分支。
- [ ] 新 token 的判别值与「是否需要进位集」一致。
- [ ] 解析函数成对消费定界符，保持 `balanced`。
- [ ] 至少一条解析测试 + 一条着色测试。
- [ ] 评估过 reparse 覆盖范围（顶层 OK；列表项/标题/math 内会回退全量但仍正确）。

> 这是「纸面设计」实践：重点是练就「改一处、想到所有连带处」的全局视野，不必真正合入源码。

## 6. 本讲小结

- **改动链**：新增一个语法构造的标准链路是 `kind.rs 立名 → lexer.rs 产 token → parser.rs 写规则 → set.rs 入位集（按需）→ ast.rs 加节点 → highlight.rs 上色 → 评估 reparser.rs`。
- **三道编译器强制关卡**：`kind.rs::name()`、`kind.rs::mode_after()`、`highlight.rs::highlight()` 都是 `SyntaxKind` 的**穷尽 match 无通配符**，新增变体必然同时报错——这是 typst-syntax 的防遗忘骨架。
- **判别值 < 128 的硬约束**：只有判别值 < 128 的 kind 才能进 `SyntaxSet`，决定新变体该插在枚举的哪个位置。
- **AST 节点 = `node!` 宏 + 一个 `impl` 块**：声明靠「结构体名 == SyntaxKind 变体名」约定，取子节点只用 `leaf_text`/`cast_first`/`children().filter_map(cast)` 三类范式，结构不符用 `placeholder` 兜底绝不 panic。
- **reparse 多数情况零改动**：只要新构造成对消费定界符（保持 `balanced`）且出现在顶层/content block 正文里，就被 `reparse_markup` 覆盖；出现在列表项/标题/math 内则回退全量，结果仍正确。
- **测试在消费方**：parser.rs/lexer.rs **无自有内联测试、本 crate 无 `tests/` 目录**，解析正确性由 node.rs（`test_debug`）、highlight.rs（`test_highlighting`）、kind.rs（`test_mode_after`）、reparser.rs 等经 `crate::parse` 间接验证；统一用 `cargo test -p typst-syntax` 运行。

## 7. 下一步学习建议

恭喜你读完整套 typst-syntax 讲义。建议的后续方向：

1. **真刀真枪做一次扩展**：挑一个 Typst 社区讨论中真实出现的小语法提案（或自己设计），在 fork 上完整实现并跑通测试，把本讲的改动链亲手走一遍。
2. **向上游走**：typst-syntax 产出的 AST 由 `typst-eval` 消费求值。阅读 `crates/typst-eval`，看一个 AST 节点（如 `Strong`、`Raw`）是如何被求值成内容（`Content`）的，理解「语法层」与「求值层」的接口边界。
3. **向 IDE 走**：阅读 `crates/typst-ide`，看 `Source`、`LinkedNode`、`highlight`、`reparse` 是如何被组装成 LSP 的补全、跳转、悬停提示与增量编译的。
4. **深入重解析论文**：typst-syntax 的增量重解析借鉴了一篇学位论文（README 与 reparser 注释均有提及）。对照 `try_reparse` 的「找最内层包围节点 → 失败向外扩展」策略阅读原文，理解其正确性证明。
5. **回读全景**：用 u1-l3 的模块地图和本讲的改动链互相印证，确认自己能闭着眼睛说出「一段文本从输入到被高亮、被增量更新，分别经过了哪些模块、哪些数据结构」。
