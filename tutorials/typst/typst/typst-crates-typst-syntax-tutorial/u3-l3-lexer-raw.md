# 原始文本 Raw 的词法处理

## 1. 本讲目标

本讲聚焦 typst-syntax 里一个很特殊的词法对象：**原始文本（raw）**，也就是用反引号包裹的 `` `...` `` 与 `` ```...``` ``。

学完本讲你应当能够：

- 说清楚为什么 raw **几乎完全在词法器（lexer）里**就被处理成一棵完整的子树，而语法器（parser）只是把它整个「吃下去」。
- 复述 `raw()` 的主流程：数反引号、匹配结尾、用定界符长度判定「行内 / 块级」。
- 区分四个与 raw 相关的 `SyntaxKind`：`Raw`、`RawDelim`、`RawLang`、`RawTrimmed`，并知道它们分别由谁产生。
- 理解块级 raw 的「公共缩进剔除（dedent）」与首末行裁剪规则。
- 动手用 `parse()` 解析几段含 raw 的文本，打印其 CST 子节点，验证你的理解。

## 2. 前置知识

本讲承接 u3-l2，默认你已经了解：

- **Lexer 的状态与 `next()` 分派**：`Lexer` 持有 `mode`（Markup/Math/Code），`next()` 按首字符分派。详见 u3-l1。
- **Lexer 是 crate 内部类型**：`Lexer` 被声明为 `pub(super)`，外部代码无法直接构造，只能通过 `parse` / `parse_code` / `parse_math` 三个入口间接调用。因此本讲的「代码实践」一律走 `parse()`（Markup 模式）。
- **CST 与 `SyntaxKind`**：词法和语法共用同一套 `SyntaxKind` 词汇表；CST 节点由 `SyntaxNode` 承载，分叶子节点和内部节点。详见 u2-l1、u5-l1。
- **`SyntaxNode` 的遍历**：`children()` 返回子节点的切片迭代器，`leaf_text()` 对叶子节点返回文本、对内部节点安全返回空串。注意 `SyntaxNode` **没有**返回迭代器的 `descendants()` 方法（`descendants()` 只是一个缓存的节点计数），遍历整棵树需要自己用 `children()` 递归。

> 关于 raw 的语义（语言标签会触发语法高亮、行内与块级的排版区别等），那是 `typst` 评估层的事；本讲只关心 **词法层如何把一段反引号文本切成一棵 CST 子树**。

## 3. 本讲源码地图

| 文件 | 本讲关注的范围 | 作用 |
|------|---------------|------|
| `src/lexer.rs` | `raw()`、`raw_lang_tag()`、`blocky_raw()`、`inline_raw()`、`add_raw_warnings()` | raw 的全部词法逻辑都在这里 |
| `src/kind.rs` | `Raw` / `RawDelim` / `RawLang` / `RawTrimmed` 四个变体，及其 `name()`、`mode_after()` | 定义 raw 相关的标签类型与模式切换规则 |
| `src/parser.rs` | 两处 `SyntaxKind::Raw => p.eat()` | 证明 parser 对 raw 「直接吃」，不做结构处理 |
| `src/ast.rs` | `Raw` 节点的文档与 `Raw::lines/lang/block` | 说明 lexer 产出的子结构如何被 AST 消费 |

数据流（本讲视角）：

```
文本中的 '`'  ──►  Lexer::next()  ──►  raw() 一次性产出一棵 Raw 子树
                                                      │
                                    子节点：RawDelim / RawLang / Text / RawTrimmed
                                                      │
                                              Parser 直接 p.eat()
                                                      │
                                          AST 的 Raw::lines()/lang()/block() 读取
```

## 4. 核心概念与源码讲解

### 4.1 Raw 的词法处理总策略：lexer 一次成型

#### 4.1.1 概念说明

「原始文本」是 Typst 里用来嵌入代码片段的语法，例如行内的 `` `typ` `` 或块级的：

`` `` `
typ
code
` `` ``（三个反引号包裹的多行内容）

它有别的 markup 元素没有的复杂性：

1. **变长定界符**：开头的反引号数量决定结尾要用多少个连续反引号才能闭合（1 个或 3 个以上）。
2. **可选的语言标签**：块级 raw 紧跟反引号的语言标签（如 `typ`、`rust`）会被单独切成 `RawLang`，供后续语法高亮使用。
3. **智能裁剪**：块级 raw 会剔除首行、末行的空白，并按「公共缩进」去除每行前导空白。

如果这些事交给 parser 来做，parser 就要在「普通文本」和「raw 内部」之间频繁来回切换模式、反复回头。typst 选择的策略是：**在词法阶段就把整段 raw 一次性切成一棵完整的 CST 子树**，parser 只需把它当作一个整体接收。这就是 `raw()` 函数注释里写的 "as a convenience to avoid going to and from the parser for each raw section"。

由此引出本讲的四个 `SyntaxKind`（定义在一起）：

- `Raw`：整段 raw 的**容器节点**（内部节点），是 parser 看到的顶层 kind。
- `RawDelim`：反引号定界符（开头和结尾各一个叶子）。
- `RawLang`：语言标签叶子（仅块级 raw 可能有）。
- `RawTrimmed`：被裁剪掉的空白片段（换行、缩进、首末行空白）。

#### 4.1.2 核心流程

```text
Lexer::next() 吃到一个 '`'（且不在 Math 模式）
        │
        ├──► return self.raw()     ← 提前返回！不走 next() 末尾的公共构造路径
                  │
                  └── raw() 内部自行组装一棵 Raw 子树并返回 (Raw, SyntaxNode)

Parser 在 markup / code 分派里遇到 SyntaxKind::Raw
        │
        └──► p.eat()    ← 直接吃掉整个 Raw 节点，不再下钻
```

关键点：`raw()` 是 `next()` 中少数几个**提前 `return`** 的分支之一。普通 token 会走到 `next()` 末尾用 `self.error.take()` 与 `SyntaxNode::leaf(...)` 统一收尾；而 `raw()` 自己构造好一个完整的内部节点（带多个子节点）后直接返回，绕过了那条公共路径。

#### 4.1.3 源码精读

先看 `next()` 如何把 '`' 分派给 `raw()`：

[src/lexer.rs:113](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L113) —— 注意 `return self.raw()`，这是提前返回；并且 Math 模式下不识别 raw（数学公式里反引号另有用途）。

再看 parser 两处「直接吃」的证据，注释直白地说明了设计意图：

[src/parser.rs:114](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L114)（markup 分派）与 [src/parser.rs:729](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L729)（code 分派），两处都是 `SyntaxKind::Raw => p.eat(), // Raw is handled entirely in the Lexer.`

那么 lexer 产出的子结构由谁消费？AST 文档把这件事说得很清楚——CST 里保留 `RawDelim`，但 AST 把反引号「抽象掉」，只用 `RawDelim` 来判断是块级还是行内：

[src/ast.rs:23-27](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/ast.rs#L23-L27) —— 解释 Raw 节点如何用子 `RawDelim` 判断 block/inline。

四个 kind 的定义集中在一起：

[src/kind.rs:43-50](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/kind.rs#L43-L50) —— `Raw`、`RawLang`、`RawDelim`、`RawTrimmed` 的文档注释已经点明各自含义：`RawDelim` 是「1 个或 3 个以上反引号」，`RawTrimmed` 是「要忽略的空白序列」。

#### 4.1.4 代码实践

**实践目标**：用 `parse()` 解析含 raw 的文本，遍历 CST，找到 `Raw` 节点并打印它的子节点，直观看到「一棵 raw 子树长什么样」。

**操作步骤**：在仓库外新建一个小 Rust 项目，`Cargo.toml` 加上 `typst-syntax` 依赖（或直接在本仓库 `cargo test -p typst-syntax` 的测试模块里写）。新建 `src/main.rs`：

```rust
// 示例代码：外部调用，经 parse() 体验 raw 的 CST 结构
use typst_syntax::{parse, SyntaxKind};

fn dump(node: &typst_syntax::SyntaxNode, depth: usize) {
    // leaf_text() 对叶子返回文本，对内部节点返回空串（不会 panic）
    println!("{}{:?} {:?}", "  ".repeat(depth), node.kind(), node.leaf_text());
    for child in node.children() {
        dump(child, depth + 1);
    }
}

fn main() {
    for src in ["`typ` code", "```typ\ncode\n```", "```\nblock\n```"] {
        println!("===== {src:?} =====");
        dump(&parse(src), 0);
    }
}
```

> 若在本仓库内部测试模块里运行，则可以直接 `use crate::lexer::Lexer;` 用 `Lexer::new(src, SyntaxMode::Markup)` 迭代 `next()`，效果等价。

**需要观察的现象**：每段文本的 CST 里都应出现一个 `Raw` 内部节点，其下有若干 `RawDelim`、（可能有的）`RawLang`、`Text`、`RawTrimmed` 子节点。

**预期结果**（仅列 `Raw` 子树部分，`{:?}` 会把换行显示为 `\n`）：

- `` `typ` code `` → `Raw` 的子节点为 `RawDelim("`")`、`Text("typ")`、`RawDelim("`")`（行内，**没有** `RawLang`）。
- `` ```typ\ncode\n``` `` → `Raw` 的子节点为 `RawDelim("```")`、`RawLang("typ")`、`RawTrimmed("\n")`、`Text("code")`、`RawTrimmed("\n")`、`RawDelim("```")`。
- `` ```\nblock\n``` `` → `Raw` 的子节点为 `RawDelim("```")`、`RawTrimmed("\n")`、`Text("block")`、`RawTrimmed("\n")`、`RawDelim("```")`（块级但**没有**语言标签，因为首行是空行）。

> 注意：规格里提到「验证 RawLang 的产生」——单反引号的 `` `typ` `` 是**行内** raw，不会产生 `RawLang`；只有 3 个以上反引号、且首行紧跟一个合法标识符时才会。本实践用第二个例子来观察 `RawLang`。如果输出与预期不符，请以本地实际运行为准（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `raw()` 要在 `next()` 里提前 `return`，而不能像普通 token 那样走到 `next()` 末尾统一构造节点？

> **参考答案**：因为普通 token 都是「单叶子」节点，`next()` 末尾的 `SyntaxNode::leaf(kind, text)` 只能造叶子；而 `raw()` 要产出一个**带多个子节点**（`RawDelim`/`RawLang`/`Text`/`RawTrimmed`）的内部节点 `SyntaxNode::inner(Raw, nodes)`，所以必须自己组装后直接返回，绕过公共路径。

**练习 2**：在 Math 模式下输入 '`' 会得到什么？

> **参考答案**：`next()` 的 raw 分派带有 `self.mode != SyntaxMode::Math` 守卫（lexer.rs:113），所以 Math 模式下不会进入 `raw()`，反引号会落到 `math()` 分派，按数学字符规则处理。

---

### 4.2 raw() 主流程：反引号定界、结尾匹配与首尾定界复用

#### 4.2.1 概念说明

`raw()` 要解决三个问题，顺序也是固定的：

1. **数开头反引号**：进入 `raw()` 时第一个 '`' 已被 `next()` 吃掉，所以从 1 开始数。反引号数量记为 `backticks`。
2. **找到结尾**：从当前游标继续吃字符，**只有连续出现 `backticks` 个反引号**才算闭合。中间任何非反引号字符都会把计数清零。
3. **按定界符长度分行内/块级**：`delim.len() >= 3` 走块级路径（`blocky_raw`，含语言标签与裁剪），否则走行内路径（`inline_raw`）。

有两个精妙的设计点：

- **`` `` ``（恰好 2 个反引号）是特例**：因为「2 个反引号」本身就是合法的「空行内 raw」（闭合条件立刻满足），它被单独处理成「两个 `RawDelim`、中间什么都没有」的空 raw。
- **首尾定界复用同一个叶子**：开头和结尾的反引号串文本完全相同（都是 `backticks` 个 '`'），所以代码只从**结尾**处截取一份 `delim`，然后既当开头（`delim.clone()`）又当结尾（`delim`）来用。

#### 4.2.2 核心流程

```text
raw():
  start = 游标 - 1                          # 指向第一个 '`'
  backticks = 1 + 后续连续的 '`' 个数
  if backticks == 2:                        # 特例：`` 就是空行内 raw
      return (Raw, inner(Raw, [RawDelim, RawDelim]))
  # 找结尾
  found = 0
  while found < backticks:
      吃一个字符 c
      if c == '`': found += 1
      else if 文本结束: return (Error, "unclosed raw text")
      else: found = 0                       # 任意非反引号字符清零
  end = 当前游标
  inner_text = 文本[start+backticks .. end-backticks]
  delim = 叶子(RawDelim, 结尾反引号串)        # 首尾复用
  nodes = [delim.clone()]                   # 先放「开头」定界
  if delim.len() >= 3:                      # 块级
      (tag, _) = raw_lang_tag(inner_text)
      若有 tag：nodes.push(叶子(RawLang, tag))
      blocky_raw(inner_text, &mut nodes)
  else:                                     # 行内
      inline_raw(inner_text, &mut nodes)
  nodes.push(delim)                         # 再放「结尾」定界
  add_raw_warnings(...)                     # 未来兼容警告
  return (Raw, inner(Raw, nodes))
```

#### 4.2.3 源码精读

整体函数（约 60 行）：

[src/lexer.rs:191-251](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L191-L251) —— `raw()` 主流程，开头注释点明「整个 raw 段在词法器里解析完，免去 parser 来回切换」。

数开头反引号：

[src/lexer.rs:195-198](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L195-L198) —— `let mut backticks = 1;`（因为首字符已被吃），随后 `while self.s.eat_if('`')` 累加。

`` `` `` 特例：

[src/lexer.rs:200-207](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L200-L207) —— 两个反引号直接返回由两个 `RawDelim` 组成的空 raw。这也解释了为何 `add_raw_warnings` 里说「空且无标签的情况只会出现在恰好两个反引号时，已由调用方处理」。

找结尾的循环（注意 `found = 0` 的清零逻辑）：

[src/lexer.rs:209-222](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L209-L222) —— 唯有**连续** `backticks` 个反引号才闭合；遇到非反引号字符则 `found = 0`；文本结束则报 `unclosed raw text` 错误。

首尾定界复用：

[src/lexer.rs:228-229](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L228-L229) 截取结尾反引号串作为 `delim`，`nodes = vec![delim.clone()]` 当开头；[src/lexer.rs:244](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L244) `nodes.push(delim)` 当结尾。因为开闭文本必然相同，这一复用是安全的。

块级/行内分派：

[src/lexer.rs:233-241](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L233-L241) —— `if delim.len() >= 3` 走 `raw_lang_tag` + `blocky_raw`，否则走 `inline_raw`。这正是「1 个反引号 = 行内，3 个以上 = 块级」的判定来源。

定界符还参与**语法模式切换**。`RawDelim` 在 `mode_after()` 里有专门的处理：

[src/kind.rs:540-544](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/kind.rs#L540-L544) 与 [src/kind.rs:600](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/kind.rs#L600) —— `RawDelim` 的 `mode_after` 是一个特殊变体：开定界之后进入 `None`（raw 内部不产生语法模式），闭定界之后回到父 `Raw` 的模式。也就是说，IDE/工具在判断「光标当前处于什么模式」时，靠 `RawDelim` 区分 raw 的内外。

#### 4.2.4 代码实践

**实践目标**：观察定界符数量如何决定行内/块级，以及 `` `` `` 特例。

**操作步骤**：把下面的字符串依次喂给 4.1.4 的 `dump(&parse(src), 0)`：

```rust
let cases = ["``", "`a`", "```a```", "`````'\"`"];
//            2个   1个    3个          5个（注意结尾需同样 5 个反引号）
```

**需要观察的现象**：

- `` `` `` → `Raw` 只有 `RawDelim`、`RawDelim` 两个子节点，中间无文本。
- `` `a` `` → `RawDelim("`")`、`Text("a")`、`RawDelim("`")`（行内，`delim.len()==1`）。
- `` ```a``` `` → `delim.len()==3`，进入块级路径；但因为 `a` 紧贴反引号、其后无空白，会被识别为语言标签 `RawLang("a")`（见 4.3），且由于首行无换行，可能触发裁剪与未来兼容警告。
- `` `````'\"` `` （5 个反引号开头、5 个结尾）→ `delim.len()==5 >= 3`，也是块级。

**预期结果**：`delim.len() >= 3` 走块级、`< 3` 走行内；`delim` 叶子的文本就是那串反引号本身。（具体每个例子的完整子节点序列，待本地验证。）

#### 4.2.5 小练习与答案

**练习 1**：字符串 `` ```x`` `y`` `` （3 个反引号开头）里，结尾需要几个反引号才能闭合？为什么 `` `y` `` 不会提前闭合？

> **参考答案**：需要 3 个连续反引号。在「找结尾」循环里，遇到非反引号字符会把 `found` 清零，单个 '`' 只能让 `found` 暂时变 1，紧接着的 'y' 又把它清零，所以单反引号片段无法闭合一个 3 反引号 raw。

**练习 2**：为何首尾定界符能复用同一个 `delim`？

> **参考答案**：闭合条件要求结尾恰好是 `backticks` 个连续反引号，而开头也是同样数量的反引号，二者文本必然相同，所以从结尾截取的一份字符串可以直接兼作开头。

---

### 4.3 语言标签 RawLang、行内 raw 与未来兼容警告

#### 4.3.1 概念说明

**语言标签（language tag）** 是块级 raw 反引号后面紧跟的标识符，如 `` ```rust `` 的 `rust`、`` ```typ `` 的 `typ`。它被切成独立的 `RawLang` 叶子，供下游做语法高亮。要点：

- **只在块级 raw 解析**：行内 raw（1 个反引号）不会产生 `RawLang`。`raw()` 只在 `delim.len() >= 3` 分支里调用 `raw_lang_tag`。
- **当前口径**：语言标签必须是一个**合法标识符**——以 `is_id_start` 开头、由 `is_id_continue` 续接（这两个函数基于 Unicode，允许 `_` 和 `-`，详见 u3-l4）。
- **未来口径会变**：未来版本会把「直到第一个空白或反引号之前的所有文本」都当作语言标签，这样 `C++`、`html.j2` 这类标签也能用。为了提前提醒用户，词法器会对比「当前标签长度」与「未来标签长度」，若不一致就发一条**警告**。

行内 raw 的处理则很简单（`inline_raw`）：把内容按换行切成多段，换行本身变成 `RawTrimmed`，其余变成 `Text`，不做任何去缩进。

#### 4.3.2 核心流程

`raw_lang_tag` 用**两次扫描**来同时得到「当前标签」和「未来标签」：

```text
raw_lang_tag(s):
  future_tag = 从起点吃到第一个「空白或反引号」为止        # 未来口径
  if future_tag 为空: return (None, None)               # 一开始就空白 → 无标签
  回到起点
  tag = 若首字符 is_id_start：吃一个标识符（is_id_continue 续接）  # 当前口径
  diff = (tag 不存在 或 tag.len() != future_tag.len()) ? Some(future_tag.len()) : None
  return (tag, diff)
```

`inline_raw` 的流程：

```text
inline_raw(s):
  从头到尾扫描
  遇到换行：先把累计的非换行文本作为 Text 推入，吃掉换行作为 RawTrimmed 推入
  扫描结束：把最后一段文本作为 Text 推入
```

#### 4.3.3 源码精读

`raw_lang_tag` 的两次扫描：

[src/lexer.rs:257-274](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L257-L274) —— 先 `eat_until(空白|反引号)` 得 `future_tag`；空则直接返回无标签；否则 `jump(start)` 回头，用 `is_id_start`/`is_id_continue` 吃「当前标签」。`diff_future_tag_len` 记录两者长度差，供警告使用。

调用处：仅在块级分支调用，并把结果推入 `nodes`：

[src/lexer.rs:233-237](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L233-L237) —— `(tag, diff_future_tag_len) = Self::raw_lang_tag(...)`，`if let Some(tag) = tag { nodes.push(leaf(RawLang, tag)) }`。

行内 raw：

[src/lexer.rs:400-414](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L400-L414) —— `inline_raw`：注释说明「换行作 `RawTrimmed`，其余非换行空白全部保留，不去缩进」。

未来兼容警告（注释非常重要，解释了为何要警告）：

[src/lexer.rs:416-431](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L416-L431) 文档，[src/lexer.rs:432-483](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L432-L483) 实现。它分两种情况发 `warn_at`：其一，`diff_future_tag_len` 存在（当前标签与未来标签长度不同），如 `C++` 当前只识别 `C`、未来识别 `C++`；其二，有标签但 raw 内容为空（`inner_len == tag.len()`），发 `empty raw text` 警告。注意这里产生的是**警告（warning）**而非错误（error），CST 里会以警告层包裹节点（见 u5-l4 诊断一讲）。

`RawLang` 的定义与命名：

[src/kind.rs:45-46](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/kind.rs#L45-L46) 定义；[src/kind.rs:404-405](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/kind.rs#L404-L405) `name()` 把它翻译成 `"raw language tag"`，供诊断消息使用。

#### 4.3.4 代码实践

**实践目标**：对比「行内 raw 无语言标签」与「块级 raw 有语言标签」，并观察未来兼容警告。

**操作步骤**：用 4.1.4 的 `dump`，再补充一个能收集诊断的版本。由于 `parse()` 产出的裸 CST 上挂载了警告，我们可以遍历收集（具体收集 API 见 u5-l4；这里先用结构观察法）：

```rust
// 示例代码
use typst_syntax::{parse, SyntaxKind};

fn main() {
    // 行内：无 RawLang
    dump_raws(&parse("`typ`"));
    // 块级 + 合法标签：有 RawLang，无警告
    dump_raws(&parse("```typ\ncode\n```"));
    // 块级 + 非标识符紧贴：触发「未来口径」警告（当前无标签，未来把 ++C 当标签）
    dump_raws(&parse("```++C\nx\n```"));
}

fn dump_raws(root: &typst_syntax::SyntaxNode) {
    fn walk(n: &typst_syntax::SyntaxNode) {
        if n.kind() == SyntaxKind::Raw {
            println!("Raw children:");
            for c in n.children() {
                println!("  {:?} {:?}", c.kind(), c.leaf_text());
            }
        }
        for c in n.children() { walk(c); }
    }
    walk(root);
}
```

**需要观察的现象**：

- `` `typ` `` 的 `Raw` 子节点里**没有** `RawLang`（行内）。
- `` ```typ\ncode\n``` `` 的 `Raw` 子节点里有 `RawLang("typ")`。
- `` ```++C\nx\n``` ``：当前口径下 `+` 不是 `is_id_start`，所以 `tag` 为 `None`，但 `future_tag = "++C"` 非空，于是 `diff_future_tag_len = Some(3)`，会产生一条 `no whitespace before raw text` 警告。

**预期结果**：`RawLang` 仅在块级且首字符为合法标识符起始时出现；非标识符开头的「未来标签」会触发警告而非产出 `RawLang`。（警告的具体收集方式与文本待本地验证，可对照 u5-l4。）

#### 4.3.5 小练习与答案

**练习 1**：`` ```C++\ncode\n``` `` 当前会把什么当作语言标签？raw 文本从哪里开始？

> **参考答案**：当前口径下，`is_id_start('C')` 成立，`eat_while(is_id_continue)` 只吃 `C`（`+` 不是 `is_id_continue`），所以语言标签是 `C`，raw 文本从 `++` 开始。由于当前标签长度（1）与未来标签长度（3）不同，会触发 `no whitespace between language tag and raw text` 警告。

**练习 2**：行内 raw（单反引号）里出现换行会怎样？

> **参考答案**：`inline_raw` 会把换行切成 `RawTrimmed`，换行前后的文本各成一个 `Text` 叶子；行内 raw 不做去缩进，所有非换行空白都保留在 `Text` 里。

---

### 4.4 块级 raw 的裁剪：blocky_raw、dedent 与 RawTrimmed

#### 4.4.1 概念说明

块级 raw 最复杂的部分是**空白裁剪**。`blocky_raw` 的目标是：去除首行、末行的整行空白，并按「公共缩进」统一剔除每行前导空白，使作者不必把代码顶格写。被剔除的空白一律变成 `RawTrimmed` 叶子，保留在 CST 里（保证 CST 仍能无损还原原文），但语义上被忽略。

裁剪分三类行，规则在源码注释里写得很细：

- **首行**（紧跟语言标签或开头反引号的那一行）：若整行全是空白，整行裁掉；否则只裁掉开头的一个空格（如果有）。
- **内部行**：先算「公共缩进（dedent）」——所有「有非空白内容的行」以及「末行」的前导空白字符数的最小值。然后每行裁掉换行 + dedent 个前导空白，剩余内容作为 `Text`。
- **末行**：若整行全空白，整行裁掉；否则像内部行处理，但如果末行最后一个非空白字符是 '`'（即紧贴闭定界符），则额外裁掉它前面的一个空格。

dedent 用公式表达：

\[
\text{dedent} \;=\; \min_{\ell \,\in\, L_{\text{non-blank}}\,\cup\,\{\ell_{\text{last}}\}} \;\bigl|\,\text{leadingWS}(\ell)\,\bigr|
\]

其中 \(\text{leadingWS}(\ell)\) 是行 \(\ell\) 的前导空白**字符**序列（按 `char` 计数，非字节），\(L_{\text{non-blank}}\) 是除首行外所有含非空白内容的行集合，\(\ell_{\text{last}}\) 是末行（即便全空白也计入）。

#### 4.4.2 核心流程

```text
blocky_raw(s, nodes):
  lines = split_newlines(s.after())          # 按换行切行（换行本身不保留在行内）
  # 1. 算 dedent
  dedent = min( lines[1..] 中「非全空白行」∪{末行} 的前导空白字符数 )
  # 2. 处理末行
  if 末行全空白: lines.pop()                  # 整行去掉
  else if 末行 trim_end 后以 '`' 结尾: 去掉末行末尾的一个空格
  # 3. 首行
  first = lines[0]
  if first 全空白: 游标前进（等后续并入 RawTrimmed）
  else:
      if 吃到一个 ' ': push(RawTrimmed)       # 去掉语言标签后的一个空格
      push(Text, 剩余首行)
  # 4. 内部行
  for line in lines[1..]:
      offset = line 前 dedent 个空白字符的字节长度
      eat_newline(); advance(offset); push(RawTrimmed)   # 换行 + 缩进
      advance(剩余); push(Text, 剩余内容)
  # 5. 若还有剩余（被裁掉的首行空白等），push 最终的 RawTrimmed
```

#### 4.4.3 源码精读

`blocky_raw` 的详细规则注释（强烈建议先读这段注释再看实现）：

[src/lexer.rs:276-303](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L276-L303) —— 把首行/内部行/末行的裁剪规则逐条写明。

dedent 的计算：

[src/lexer.rs:309-317](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L309-L317) —— `skip(1)` 跳过首行，`filter(!all whitespace)` 只留有内容的行，`chain(lines.last())` 把末行也算进来，`map(take_while(is_whitespace).count()).min()`。注意首行不参与 dedent，这是有意为之——语言标签所在行的缩进不应影响代码块。

末行处理：

[src/lexer.rs:321-330](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L321-L330) —— 全空白则 `pop`；否则若 `trim_end` 后以 '`' 结尾，剥掉末尾一个空格（注释解释：必须在此处理，因为首末行可能是同一行）。

首行处理：

[src/lexer.rs:345-379](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L345-L379) —— 全空白则只前进游标（随后由最终 `RawTrimmed` 兜底收走，附有大段「按情形证明」注释解释为何这里不立即 `push_leaf` 也不会丢文本）；否则吃一个空格作 `RawTrimmed`，剩余首行作 `Text`。

内部行处理：

[src/lexer.rs:382-389](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L382-L389) —— `offset` 是 dedent 个前导空白字符的**字节**长度（`map(char::len_utf8).sum()`，正确处理多字节空白）；`eat_newline` 吃换行（并处理 `\r\n`），`advance(offset)` 跨过缩进，二者合并为 `RawTrimmed`，剩余为 `Text`。

最终兜底：

[src/lexer.rs:392-394](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lexer.rs#L392-L394) —— 若游标未到末尾（例如首行被裁掉的空白），把剩余整体作为 `RawTrimmed` 推入。

`RawTrimmed` 的定义与命名：

[src/kind.rs:49-50](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/kind.rs#L49-L50) 定义（「要忽略的空白序列」），[src/kind.rs:405-406](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/kind.rs#L405-L406) `name()` 为 `"raw trimmed"`；其 `mode_after` 为 `None`（[src/kind.rs:601](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/kind.rs#L601)），即裁剪片段不产生语法模式。

AST 端如何利用这些子结构判断 block：

[src/ast.rs:716-725](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/ast.rs#L716-L725) —— `Raw::block()` 的判定：首个子节点是 `RawDelim` 且长度 ≥ 3，**并且**存在某个 `RawTrimmed` 含换行字符。也就是说，「是否块级」最终由 `RawDelim` 长度 + 是否有跨行 `RawTrimmed` 共同决定——这正是 lexer 精心切分这些叶子的回报。

#### 4.4.4 代码实践

**实践目标**：用一个带统一缩进的块级 raw，验证 dedent 把公共缩进全部剔除。

**操作步骤**：把下面这段（注意每行代码前有 2 个空格的缩进）交给 4.1.4 的 `dump`：

```rust
// 示例代码：字符串里的 "\n" 后各有 2 个空格
let src = "```typ\n  let x = 1\n  let y = 2\n```\n";
dump(&parse(src), 0);
```

**需要观察的现象**：在 `Raw` 子树里，两个代码行的 `Text` 内容应当是 `"let x = 1"` 与 `"let y = 2"`——即每行开头的 2 个空格被并入 `RawTrimmed`（与换行一起），而不出现在 `Text` 里。

**预期结果**：内部行对应的节点序列形如 `RawTrimmed("\n  ")`、`Text("let x = 1")`、`RawTrimmed("\n  ")`、`Text("let y = 2")`、`RawTrimmed("\n")`（末行空白被裁）。即 dedent = 2，两行的 2 空格缩进被统一剔除。（完整序列待本地验证。）

#### 4.4.5 小练习与答案

**练习 1**：若块级 raw 的首行有 4 个空格缩进，而内部代码行有 2 个空格缩进，dedent 是多少？首行的 4 个空格会被全部剔除吗？

> **参考答案**：dedent = 2（首行不参与 dedent 计算，内部非空白行的最小前导空白为 2）。内部行各自剔除 2 个空格。首行不按 dedent 处理：若首行（语言标签之后的剩余部分）全是空白，整行裁掉；否则只裁掉开头的一个空格——所以首行 4 个空格不会按 dedent 全部剔除。

**练习 2**：为何 `RawTrimmed` 仍要作为叶子保留在 CST 中，而不是直接丢弃？

> **参考答案**：CST 是「无损」的具体语法树，中序遍历必须能逐字还原原文。裁剪掉的空白在语义上被忽略，但在结构上必须保留，否则原文无法重建；这也是 `Raw::block()` 能靠「`RawTrimmed` 是否含换行」来判断块级的前提。

---

## 5. 综合实践

把本讲内容串起来：写一个小工具，给定任意 Typst 文本，找出其中所有 `Raw` 节点，并对每个 raw 报告：

1. 是行内还是块级（提示：参考 `Raw::block()` 的判定——首个 `RawDelim` 长度 ≥ 3 且存在含换行的 `RawTrimmed`；你也可以直接用 `typst_syntax::ast::Raw` 的 `block()` 方法，见 [src/ast.rs:717](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/ast.rs#L717)）。
2. 语言标签是什么（遍历子节点找 `RawLang`，或用 `Raw::lang()`）。
3. 实际保留的代码文本（把所有 `Text` 子叶子的 `leaf_text()` 拼起来，或用 `Raw::lines()` 迭代）。

```rust
// 示例代码：综合实践骨架
use typst_syntax::{parse, SyntaxKind, ast::Raw};

fn main() {
    let src = "Here is `inline` and a block:\n```rust\n  fn main() {}\n```\n";
    let root = parse(src);
    let mut stack = vec![&root];
    while let Some(n) = stack.pop() {
        if n.kind() == SyntaxKind::Raw {
            // 用 AST 视图读取语义（from_untyped 会校验 kind）
            if let Some(raw) = Raw::from_untyped(n) {
                println!("block: {}, lang: {:?}",
                    raw.block(),
                    raw.lang().map(|l| l.get().as_str()));
            }
        }
        for c in n.children() { stack.push(c); }
    }
}
```

**自检问题**（用本讲学到的规则预测，再运行验证）：

- 上例中块级 raw 的 dedent 是多少？`Text` 里 `fn main() {}` 前的 2 个空格是否被剔除？
- 把语言标签改成 `C++`（`` ```C++ ``），会触发哪条警告？当前会把什么当作标签？

> 说明：`ast::Raw` 与 `Raw::from_untyped` 的用法承接 u7-l1；若尚未学过 AST 视图，也可仅用 `SyntaxKind` 与 `leaf_text()` 手动判定，跳过 `Raw::from_untyped` 那行。

## 6. 本讲小结

- raw 是 typst-syntax 中**唯一在词法阶段就组装成完整子树**的构造：`next()` 遇到 '`' 时 `return self.raw()` 提前返回，parser 两处只 `p.eat()`。
- `raw()` 主流程：数开头反引号 → 匹配连续 `N` 个反引号的结尾 → 用 `delim.len() >= 3` 判定块级/行内；`` `` ``（2 个反引号）是「空 raw」特例；首尾定界符文本相同，复用同一份 `delim` 叶子。
- 四个相关 kind：`Raw`（容器）、`RawDelim`（定界符，兼作模式切换标记）、`RawLang`（语言标签，仅块级）、`RawTrimmed`（被裁剪的空白）。
- 语言标签 `RawLang` 由 `raw_lang_tag` 用 `is_id_start`/`is_id_continue` 解析；当前与未来口径不一致时会通过 `add_raw_warnings` 发出**警告**而非错误。
- 块级 raw 的 `blocky_raw` 实现「首行/末行整行或单空格裁剪 + 内部行公共缩进 dedent」，被裁空白以 `RawTrimmed` 保留以维持 CST 无损；AST 的 `Raw::block()` 正是靠 `RawDelim` 长度与含换行的 `RawTrimmed` 共同判定块级。

## 7. 下一步学习建议

- **u3-l4（字符判定工具函数）**：`raw_lang_tag` 用到的 `is_id_start` / `is_id_continue`、`blocky_raw` 用到的 `split_newlines` / `is_newline` 都在词法器的公共工具里，下一讲会系统讲解。
- **u5-l4（错误与警告诊断）**：本讲出现的 `warn_at` / `hint` / `empty raw text` 等警告如何被收集成 `SyntaxDiagnostic`，会在 CST 诊断一讲展开。
- **u7-l3（典型 AST 节点剖析）**：`Raw::lines()` / `lang()` / `block()` 如何从 lexer 产出的固定子结构中抽取语义，是「AST 依赖 parser/lexer 固定结构假设」的典型样本。
- 若想了解 raw 的语言标签如何驱动**语法高亮**，可预习 u10-l3（`highlight.rs`）。
