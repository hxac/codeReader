# Lexer 结构与工作流程

## 1. 本讲目标

本讲是「词法分析 Lexer」单元的第一篇。学完后你应该能够：

- 说清 `Lexer` 结构里每个字段（`s` / `mode` / `newline` / `error`）的职责，以及它为什么基于 `unscanny::Scanner`。
- 顺着 `next()` 的 match 表，预测任意一个字符在给定模式下会被分派到哪条词法分支（whitespace / comment / raw / markup / math / code）。
- 解释 `error()` / `hint()` 如何在「产出一个 `Error` token」的同时附带上给用户的修正建议，以及这套机制的两条不变量。
- 读懂 Parser 是怎样把 `Lexer` 当成一个迭代器来消费的（`newline` 标记在哪儿被用掉）。

本讲只聚焦词法器的**骨架与分发逻辑**，不深入 markup / math / code 三套模式各自的具体词法规则——那是 u3-l2 ~ u3-l4 的主题。

## 2. 前置知识

在进入源码前，先建立三个直觉。

### 2.1 词法器在整个链路中的位置

回顾 u1-l4 的端到端数据流：`文本 → Lexer(词法) → Parser(语法/CST) → numberize → Lines → Source`。Lexer 是这条流水线的**第一站**，它把一串纯文本切成一个个带类型的「词」——token。后续的 Parser 拿着这些 token 拼装出 CST。

一个关键认知（来自 u2-l1）：**lexer 产出的 token 和 parser 构建的 CST 节点共用同一套 `SyntaxKind` 词汇表**。比如 `SyntaxKind::Ident` 既可以是一个 lexer token（「这里有一个标识符」），最终也会成为 CST 里的一个叶子节点。所以读 lexer 源码时，你会看到它到处都在 `return SyntaxKind::XXX`。

### 2.2 unscanny::Scanner 是什么

`unscanny` 是一个极简的「字符串扫描器」crate。你可以把它理解成一根**游标（cursor）**压在源文本上，提供一组便利方法来移动游标、偷看字符。Lexer 用到的核心方法有：

| 方法 | 作用 |
|------|------|
| `Scanner::new(text)` | 在文本上创建一根起始游标 |
| `eat()` | 吃掉游标处的一个字符并返回它（游标前移） |
| `eat_if(x)` | 若游标处匹配 `x` 就吃掉，返回是否吃到 |
| `eat_while(p)` / `eat_until(p)` | 一直吃，直到谓词为假 / 为真 |
| `peek()` | 偷看游标处字符，不吃掉 |
| `scout(n)` | 偷看相对游标偏移 `n` 的字符（可为负，看前面） |
| `at(x)` / `done()` | 游标是否在 `x` 上 / 是否到末尾 |
| `cursor()` / `jump(i)` | 读取 / 直接设置游标的字节下标 |
| `before()` / `after()` / `from(start)` | 取游标前 / 后 / 从 `start` 到游标的文本片段 |

整篇 lexer.rs 几乎所有字符处理都建立在这张表上。记住「游标」这个意象，后面的代码就好读了。

### 2.3 三种模式决定同一字符的词法结果

来自 u1-l1 / u1-l4：`SyntaxMode` 有 `Markup`（正文）、`Math`（公式）、`Code`（`#` 后的代码）三种。同一个字符在不同模式下可能产出完全不同的 token——比如 `-` 在 markup 里可能是列表标记 `ListMarker`，在 code 里是减号 `Minus`。`Lexer` 因此持有一个 `mode` 字段，`next()` 会按模式分派。这正是 u3-l2 要专题对比的内容，本讲只需知道「模式驱动分派」即可。

## 3. 本讲源码地图

本讲只读一个文件，但会顺带引用它上下游各一处：

| 文件 | 作用 |
|------|------|
| `src/lexer.rs` | **本讲主角**。`Lexer` 结构、`next()` 分发、`error()`/`hint()` 机制、以及 markup/math/code/raw 各分支的入口 |
| `src/parser.rs` | 上游消费者。其中的 `Parser::lex` 把 `Lexer` 包成一个「跳过 trivia 的迭代器」，能帮你看清 `next()` 的返回值与 `newline` 标记怎么被用掉 |
| `src/kind.rs` | 提供 `SyntaxKind` 枚举与 `is_trivia()` 等分类方法（u2-l1 / u2-l2 已讲），本讲引用它的 trivia 定义 |

## 4. 核心概念与源码讲解

### 4.1 Lexer 结构：一根带状态的游标

#### 4.1.1 概念说明

`Lexer` 是一个**有状态的迭代器**。它的「无状态部分」就是上游 `unscanny::Scanner`（那根游标）；在游标之外，它还需要三块额外状态才能完成词法分析：

1. **`mode`**：当前处于 Markup / Math / Code 哪种模式——决定分派规则。
2. **`newline`**：上一个产出的 token 是否包含换行。这个信息对 Parser 至关重要：在 Code 模式下，换行往往是「一条语句结束了」的信号（详见 u4-l5）。但换行属于 trivia（会被 Parser 跳过），所以无法事后从 token 本身恢复，必须由 Lexer 在产出时顺手记下。
3. **`error`**：**当前正在构造的这个 token** 的错误消息与提示列表。它是一个「临时暂存区」——在 `next()` 执行期间可能被填入，并在 `next()` 结束时被取走清空。

#### 4.1.2 核心流程

`Lexer` 的生命周期可以概括为：

```
Lexer::new(text, mode)          // 建一根游标在 text 开头，mode/newline/error 复位
        │
        ▼
  loop { lexer.next() }         // Parser 反复调用 next()
        │
        ├─ 进入 next()：debug_assert(error 为 None)   ← 不变量 1
        ├─ 按首字符 + 模式分派到具体词法函数
        │     这些函数可能调用 error()/hint() 填充 error 暂存区
        ├─ 取出本 token 的文本片段
        └─ 从 error 暂存区构造 SyntaxNode：有错→Error 节点(带 hints)，无错→leaf 节点
              并清空 error                                  ← 不变量 2：调用间 error 必为 None
        │
        ▼
   返回 (SyntaxKind, SyntaxNode)，游标已前进到下一个 token 起点
```

#### 4.1.3 源码精读

先看结构定义本身：

[src/lexer.rs:15-27 — `Lexer` 结构与四个字段](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L15-L27)

> `s` 是底层扫描器（游标）；`mode` 决定分派；`newline` 记录上个 token 是否含换行；`error` 是「当前 token 的错误+提示暂存区」，注释明确写道「在 `next()` 调用之间恒为 `None`」。

注意它的可见性是 `pub(super)`：

[src/lexer.rs:14-16 — `pub(super) struct Lexer`，仅 crate 内部可见](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L14-L16)

> 这意味着 **`Lexer` 是 crate 内部实现细节，外部代码无法直接构造它**。对外暴露词法能力的公共入口是 `parse` / `parse_code` / `parse_math`（见 [src/lib.rs:28](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lib.rs#L28)）。本讲的代码实践因此会通过 `parse_code` 来间接观察 token 流，而不是直接 `Lexer::new`。

构造器与一组访问/设置方法：

[src/lexer.rs:32-72 — `new` 与 mode/cursor/newline/column 等基础方法](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L32-L72)

> `new` 把游标放在文本开头，`newline`、`error` 复位。`cursor()`/`jump()` 直接转发给底层 `Scanner`。`column(index)` 会临时复制一份 `Scanner`（注释说这很廉价，因为 `Scanner` 是只持引用的轻量结构），从 `index` 往前数到最近换行的字符数——这是给 Parser 判断「换行后是否回到行首」用的（决定列表/标题等 markup 标记是否生效）。

#### 4.1.4 代码实践

**实践目标**：在脑海里把 `Lexer` 拆成「无状态游标 + 三块状态」，确认你分得清哪个字段解决哪个问题。

**操作步骤**：

1. 打开 [src/lexer.rs:15-27](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L15-L27)，遮住注释，逐个字段说出它的用途。
2. 回答：为什么 `newline` 不能在事后从 CST 节点反推？（提示：换行属于 trivia，会被 Parser 跳过丢弃。）
3. 回答：为什么 `error` 字段必须是 `Option`，且注释强调「调用间恒为 `None`」？（提示：它与 `next()` 的「构造完就取走」配对，见 4.3。）

**预期结果**：你能不看书复述四个字段的职责，并解释 `error` 字段是「单 token 生命周期」的暂存区。无需运行命令。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `mode` 字段去掉，Lexer 还能正确工作吗？为什么？
**答案**：不能。同一字符在不同模式下产出不同 token（如 `-` 在 Markup 可能是 `ListMarker`，在 Code 是 `Minus`），`next()` 的分派依赖 `mode`。即便 Parser 能在解析中途切模式（`$..$` 进 Math、`#` 进 Code），也需要通过 `set_mode` 改这个字段，而不是去掉它。

**练习 2**：`column(index)` 为什么要「临时复制一份 `Scanner`」而不是直接移动 `self.s`？
**答案**：因为查询列号不应有副作用——不能为了数列号就把真正的词法游标移走。复制一个临时的 `Scanner`（只拷贝一个引用 + 下标，极廉价）来反向扫描，是典型的「无副作用查询」写法。

---

### 4.2 next()：按字符 + 模式的分发枢纽

#### 4.2.1 概念说明

`next()` 是 `Lexer` 的心脏。它做三件事：

1. **吃一个首字符**，作为分派的依据；
2. **按「公共前缀优先」+「模式分派」** 决定走哪条词法分支；
3. **统一收尾**：截取本 token 的文本，根据 `error` 暂存区是否有内容，造出一个 `SyntaxNode`（普通 leaf 或带 hints 的 Error 节点）。

它有两个**提前返回的逃逸出口**（`raw` 和「附带子树的 math token」），因为这两种 token 不是简单的「一段文本 + 一个 kind」，而是自身就携带一棵小子树（比如 raw 会被切成 `RawDelim`/`RawLang`/`Text`/`RawTrimmed` 等子节点）。这两种情况由分支函数自己构造 `SyntaxNode` 并直接 `return`，跳过统一收尾。

#### 4.2.2 核心流程

`next()` 的分派优先级（从上到下，命中即止）：

```
next():
  reset newline = false
  match 首字符 c:
    ├─ 空白 (is_space, 与模式相关) ──────────► whitespace()
    ├─ '#' 且在文件首且后跟 '!' ─────────────► shebang()      (#!shebang 行)
    ├─ '/' 且后跟 '/' ───────────────────────► line_comment()
    ├─ '/' 且后跟 '*' ───────────────────────► block_comment()
    ├─ '*' 且后跟 '/' ───────────────────────► error: "unexpected end of block comment" (+hint)
    ├─ '`' 且不在 Math 模式 ─────────────────► return raw()   (提前返回，自带子树)
    └─ 其它 ── 按 mode 分派:
          ├─ Markup ─► markup()
          ├─ Math   ─► math()   (可能提前返回，因为 math token 可能附带 SyntaxNode)
          └─ Code   ─► code()

  收尾（非提前返回路径）:
    text = 从 token 起点到当前游标的文本
    node = 若 error 暂存区有内容 → SyntaxNode::error(msg, text).with_hints(hints)
           否则                  → SyntaxNode::leaf(kind, text)
    return (kind, node)
```

注意分派顺序的精妙之处：

- **注释优先于模式分派**：`//`、`/*` 在任何模式下都是注释，所以放在模式分派之前。
- **`*/` 单独处理**：在文本里出现 `*/` 而没有配对的 `/*` 是错误，这里直接 `error()` + `hint()`（建议转义或开注释）。
- **`is_space` 依赖模式**：Markup 下只有 `' '`/`'\t'`/换行是空白；Code/Math 下用 `char::is_whitespace()`。详见 `is_space` 函数。
- **`raw` 仅在非 Math 模式**：Math 模式下的反引号交给 `math()` 处理。

#### 4.2.3 源码精读

`next()` 主体：

[src/lexer.rs:95-132 — `next()` 的分派与统一收尾](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L95-L132)

> 第 96 行 `debug_assert!(self.error.is_none())` 是「调用间 error 必为 None」这条不变量的运行时检查。第 99 行把 `newline` 复位为 `false`——它只反映「最近这一个 token」是否含换行。第 126-130 行是统一收尾：用 `self.error.take()` 取走暂存区（取出后变 `None`，维持不变量），有错造 Error 节点并 `.with_hints(hints)`，无错造普通 leaf。

两个提前返回出口：

[src/lexer.rs:113-119 — `raw` 与「带子树的 math token」提前返回](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L113-L119)

> `raw()` 返回 `(SyntaxKind, SyntaxNode)`，直接透传给 `next()` 的返回值，**不走**第 126-131 行的统一收尾——所以 raw 的错误（如未闭合）是在 `raw()` 内部自行用 `SyntaxNode::error` 构造的。math 同理：`math()` 返回 `(kind, Option<SyntaxNode>)`，`Some(node)` 时提前返回。

几个共享分支（所有模式通用）：

[src/lexer.rs:135-184 — whitespace / shebang / line_comment / block_comment](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L135-L184)

> `whitespace()` 贪婪吃掉连续空白，用 `count_newlines` 数其中换行数，据此设置 `self.newline`；在 Markup 模式下，连续 ≥2 个换行产出 `Parbreak`（段落分隔），否则产出 `Space`。`block_comment()` 用一个 `depth` 计数器处理 `/* /* 嵌套 */ */`，找到配对的 `*/` 才停。

那三个模式分派入口（本讲只看它们的「存在与签名」，具体规则留给后续讲义）：

[src/lexer.rs:488-523 — `markup()` 分派入口](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L488-L523)

[src/lexer.rs:669-760 — `math()` 分派入口（返回值带可选 SyntaxNode）](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L669-L760)

[src/lexer.rs:854-895 — `code()` 分派入口](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L854-L895)

> 注意签名差异：`markup` / `code` 返回 `SyntaxKind`（走统一收尾），而 `math` 返回 `(SyntaxKind, Option<SyntaxNode>)`——因为 math 里某些「原子」（如多字符的字段访问 `a.b`）需要直接产出一棵小子树，所以预留了自带节点的出口。

#### 4.2.4 代码实践

**实践目标**：手动追踪 `Lexer` 对 `"// c\n#x"`（Code 模式）的切分，得到完整 token 序列；再用公共入口 `parse_code` 交叉验证。

**背景**：因为 `Lexer` 是 `pub(super)`（见 4.1.3），我们无法在 crate 外直接 `Lexer::new`。但 `parse_code` 内部就是「以 `SyntaxMode::Code` 驱动 `Lexer`」，且产出的 CST 会**保留 trivia 节点**（注释、空白都还在树里），所以遍历 CST 的叶子就能看到 lexer 切出的 token 序列。

**操作步骤**：

1. **先纯手工追踪**（不运行），对照 [src/lexer.rs:95-132](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L95-L132) 与 [src/lexer.rs:854-895](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L854-L895)：

   | 第几次 next() | 首字符 | 命中分支 | 产出 kind | 文本 | newline |
   |---|---|---|---|---|---|
   | 1 | `/` | `//` → `line_comment()` | `LineComment` | `"// c"` | false |
   | 2 | `\n` | `is_space` → `whitespace()` | `Space` | `"\n"` | **true** |
   | 3 | `#` | mode=Code → `code()` → 落到 `invalid_char_in_code('#')` | `Error`（消息「the character `#` is not valid in code」，2 条 hints） | `"#"` | false |
   | 4 | `x` | mode=Code → `code()` → `is_id_start` → `ident()` | `Ident` | `"x"` | false |
   | 5 | （无） | `None` | `End` | `""` | false |

   追踪要点：第 1 步 `line_comment` 用 `eat_until(is_newline)` 吃到 `\n` 之前（不含 `\n`），所以注释文本是 `"// c"`；第 2 步 `\n` 本身被 `whitespace` 吃掉，且因含换行把 `newline` 置 true；第 3 步 `#` 在 Code 模式不是合法操作符，进入 `invalid_char_in_code`，该方法对 `#` 给出专属 hints（见 4.3.3）。

2. **可选运行验证**（待本地验证）。下面这段**示例代码**（非项目原有代码）用公共 `parse_code` 遍历 CST 叶子，可与上表对照：

   ```rust
   // 示例代码：通过公共入口 parse_code 间接观察 Lexer 的 token 序列
   use typst_syntax::SyntaxNode;

   fn walk(node: &SyntaxNode) {
       let text = node.leaf_text();
       // 内部容器节点的 leaf_text 为空；只有叶子/错误节点直接对应 lexer 产出的 token
       if !text.is_empty() {
           println!(
               "kind={:?}\ttext={:?}\tnewline_in_text={}",
               node.kind(),
               text,
               text.contains('\n'),
           );
       }
       for child in node.children() {
           walk(child);
       }
   }

   fn main() {
       // parse_code 内部即以 SyntaxMode::Code 驱动 Lexer
       let root = typst_syntax::parse_code("// c\n#x");
       walk(&root);
   }
   ```

**需要观察的现象**：CST 的叶子序列为 `LineComment("// c")` → `Space("\n")` → `Error("#")` → `Ident("x")`。

**预期结果**：输出与上表的 kind/text 列一致。注意 `newline` 这一列在 CST 里**无法直接读出**——它是 Lexer 迭代过程中的瞬时状态，被 Parser 在 `lex()` 里消费掉了（见 4.2.5 / 下面的 parser 引用）。示例代码用「文本是否包含 `\n`」近似还原，这只能识别「文本里的换行」，与 lexer 的 `newline()` 标记在此例中恰好一致。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `next()` 的 match 表里，注释判断（`//`、`/*`）要写在模式分派（markup/math/code）之前？
**答案**：因为注释在任何模式下都应被识别为注释，是「跨模式共享」的规则。若放在模式分派之后，`/` 在 Code 模式会被当成除号 `Slash`，就永远进不了注释分支。

**练习 2**：`Parser` 是怎么用掉 `Lexer` 的 `newline` 标记的？
**答案**：见 [src/parser.rs:1854-1886](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L1854-L1886) 的 `Parser::lex`。它反复调用 `lexer.next()`，期间用 `had_newline |= lexer.newline()` 累积「这一轮跳过的 trivia 里有没有换行」，再把这个 `newline` 信息连同当前 token 打包进 `Token` 结构返回给 Parser。Parser 据此决定是否在 Code 模式插入一个临时 `SyntaxKind::End` 来终止语句（u4-l5 专题）。

---

### 4.3 error() 与 hint()：把诊断信息挂到 token 上

#### 4.3.1 概念说明

词法错误不是「抛异常」，而是「产出一个 `SyntaxKind::Error` 的 token，并附带面向用户的诊断信息」。这样做的好处是：**解析不会因为一个坏字符就中断**——错误被当成普通 token 喂给 Parser，Parser 可以在错误位置继续恢复（error recovery，u4-l5），把多个错误一次性报全。

每个 Error token 可以携带：

- 一条**消息**（message）：说明哪里错了；
- 零到多条**提示**（hints）：给出可操作的修正建议。

`error()` 和 `hint()` 就是往 4.1 里那个 `error` 暂存区里写东西的两个方法。

#### 4.3.2 核心流程

```
在某个词法分支内部：
  error("xxx 不合法")      // 开启暂存区：Some((msg, []))，并返回 SyntaxKind::Error 作为 kind
  hint("建议你 ...")        // 往暂存区的 hints 列表里追加（仅在已有 error 时生效）
  hint("或者 ...")
  return SyntaxKind::Error

回到 next() 收尾：
  take 出 (msg, hints)
  → SyntaxNode::error(msg, text).with_hints(hints)   // 错误+提示一起落到节点上
```

这里有两条互相支撑的不变量：

- **不变量 A**：`error()` 假设暂存区为空（开头 `debug_assert!(self.error.is_none())`），即一个 token 只能开始一次错误。
- **不变量 B**：`hint()` 只在暂存区已存在时追加——所以**必须先 `error()` 再 `hint()`**，顺序不能反；若没有 error，`hint()` 是无操作（`if let Some(...)` 不匹配就什么也不做）。
- **不变量 C**：`next()` 收尾用 `take` 取走暂存区，保证「调用之间 error 必为 None」（与 4.1 的字段注释呼应）。

#### 4.3.3 源码精读

[src/lexer.rs:76-88 — `error()` 开启暂存区、`hint()` 追加提示](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L76-L88)

> `error()` 把暂存区置为 `Some((message, eco_vec![]))` 并返回 `SyntaxKind::Error`——注意它**既写了状态、又返回了 kind**，所以调用处可以写成 `return self.error("...");` 一气呵成。`hint()` 取出暂存区的可变引用，往 `hints` 里 `push`。

`next()` 收尾处如何把暂存区变成节点：

[src/lexer.rs:126-131 — 有错造 Error 节点（带 hints），无错造 leaf](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L126-L131)

> `self.error.take()` 是「取出并清空」——取走后字段回到 `None`，维持不变量 C。`SyntaxNode::error(message, text).with_hints(hints)` 把消息和提示都挂到节点上，下游诊断系统可以一并取出（u5-l4）。

一个把 `error` + 多条 `hint` 配合得很好的真实例子——`invalid_char_in_code`：

[src/lexer.rs:899-940 — 对 Code 模式里常见易错字符给出专属 hints](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L899-L940)

> 例如用户在 Code 模式写了 `&&`：第一个 `&` 落到 `invalid_char_in_code`，匹配 `& if self.s.eat_if('&')` 后调用 `error("`&&` is not valid in code")`，紧接 `hint("in Typst, `and` is used for logical AND")`。同理 `||`→提示用 `or`，`!`→提示用 `not` 或 `!=`，`#`→提示「你已经在 code 模式，试试去掉 `#`」。这些 hints 正是借助 4.3 的暂存区机制挂到 Error token 上的。

#### 4.3.4 代码实践

**实践目标**：观察一个 Error token 上同时挂着「消息 + 多条 hints」。

**操作步骤**：

1. 阅读本讲 4.2.4 的追踪表里第 3 步（`#` 在 Code 模式出错），并对照 [src/lexer.rs:899-940](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L899-L940) 中 `'#'` 分支。
2. 回答：当用户在 Code 模式写下 `"#let"`（误把 markup 的 `#let` 写到了纯 code 上下文），词法器会产出什么 token？消息和 hints 分别是什么？

**预期结果**：`#` 命中 `invalid_char_in_code` 的 `'#'` 分支：消息为「the character `#` is not valid in code」；hints 有两条——「you are already in code mode」与「try removing the `#`」。随后 `let` 会被 `ident()` 经 `keyword("let")` 识别成关键字 token `Let`。**待本地验证**（可借助 `parse_code("#let")` 后调用 `root.errors_and_warnings()` 收集诊断，API 细节见 u5-l4）。

#### 4.3.5 小练习与答案

**练习 1**：如果把代码写成「先 `hint("...")` 再 `error("...")`」，会发生什么？
**答案**：`hint()` 内部是 `if let Some(...) = &mut self.error`，调用时暂存区还是 `None`，所以这条 hint 被默默丢弃。接着 `error()` 才开启暂存区。最终 Error token 只有消息、没有这条 hint。这就是不变量 B 的现实后果：必须先 error 后 hint。

**练习 2**：为什么词法错误选择「产出一个 Error token」而不是 `panic!` 或 `Result::Err`？
**答案**：为了让解析能**继续**并一次报全多个错误。Error token 和普通 token 一样进入 Parser，Parser 可以在错误处做恢复（比如跳过这个坏字符）继续解析后续内容；诊断信息挂在本讲 4.1 的暂存区里，最终落到节点上供下游汇总（u5-l4）。若用 `Result`/`panic`，一个坏字符就会终止整个词法，无法收集后续错误。

---

## 5. 综合实践

把本讲的「结构 → 分发 → 错误提示」三块串起来，做一次完整的词法追踪。

**任务**：对字符串 `"1 && 2"`（Code 模式）完成下列两件事。

1. **手工产出 token 序列表**：逐次模拟 `next()`，对每个 token 写出「命中分支 / kind / 文本 / newline」。重点关注 `&&`：第一个 `&` 会走 [src/lexer.rs:899-940](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L899-L940) 的 `& if self.s.eat_if('&')` 分支，吃掉两个 `&` 产出一个 Error token。
2. **定位 hints**：写出这个 `&&` Error token 的消息和 hint 文案，并解释它们是怎样经过 `error()` → `hint()` → `next()` 收尾三步落到节点上的。

**参考答案（先自己做完再对照）**：

| 次 | 命中分支 | kind | 文本 | newline |
|---|---|---|---|---|
| 1 | code() → `0..='9` → `number()` | `Int` | `"1"` | false |
| 2 | is_space → whitespace() | `Space` | `" "` | false |
| 3 | code() → 落到 invalid_char_in_code，`&`+eat_if('&') | `Error` | `"&&"` | false |
| 4 | is_space → whitespace() | `Space` | `" "` | false |
| 5 | code() → number() | `Int` | `"2"` | false |

`&&` 的 Error token：消息「`&&` is not valid in code」；hint「in Typst, `and` is used for logical AND」。三步流程：`error(...)` 把暂存区置为 `Some((msg, []))` 并返回 `Error` 作 kind → `hint(...)` 往暂存区 hints 追加 → 回到 `next()` 第 127-128 行 `self.error.take()` 取出 `(msg, hints)`，造 `SyntaxNode::error(msg, text).with_hints(hints)`，暂存区回到 `None`。**待本地验证**。

## 6. 本讲小结

- `Lexer` = 无状态的 `unscanny::Scanner` 游标 + 三块状态：`mode`（决定分派）、`newline`（上个 token 是否含换行，供 Parser 终止语句用）、`error`（当前 token 的错误+提示暂存区）。
- `next()` 是分发枢纽：先吃一个首字符，按「公共前缀（注释/空白/raw）优先 + 模式分派（markup/math/code）」选分支；普通路径走统一收尾（截文本、据暂存区造 leaf 或 Error 节点），`raw` 与「带子树的 math token」两条路径提前返回、自带节点。
- `newline` 字段是迭代过程的瞬时状态，被 [src/parser.rs:1854-1886](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L1854-L1886) 的 `Parser::lex` 消费，无法事后从 CST 反推。
- 词法错误不抛异常，而是产出 `SyntaxKind::Error` token，让解析继续、错误报全。
- `error()`/`hint()` 围绕暂存区协作，遵守三条不变量：一个 token 只 `error()` 一次、必须先 `error()` 后 `hint()`、`next()` 收尾用 `take` 清空暂存区保证「调用间为 None」。
- `Lexer` 是 `pub(super)` 的 crate 内部类型；外部应通过公共入口 `parse` / `parse_code` / `parse_math` 间接使用词法能力。

## 7. 下一步学习建议

- **u3-l2 Markup / Code / Math 三模式词法**：本讲只点到了 `markup()` / `code()` / `math()` 的入口签名，下一讲会逐字符对比同一字符在三种模式下的不同 token，是本讲的自然延伸。
- **u3-l3 原始文本 Raw 的词法处理**：深入本讲提到的那个「提前返回」的 `raw()` 出口，看反引号定界、`RawLang` 语言标签、`RawTrimmed` 去空白如何在一处词法里完成。
- **u4-l5 新行处理与错误恢复**：本讲反复提到的 `newline` 标记与 Error token，最终都在 Parser 的语句终止与错误恢复里发挥作用，学完 u4 系列再回看本讲会有更深体会。
- 想直接验证本讲结论，可在仓库内运行 `cargo test -p typst-syntax`，或在 crate 的测试里加用例观察 `parse_code` 的 CST（注意不要修改 src 业务代码）。
