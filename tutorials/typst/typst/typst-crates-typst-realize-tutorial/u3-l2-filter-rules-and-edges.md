# 过滤规则与边界元素

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 `visit_filter_rules` 在整条具现化（realization）调度流水线中的**位置**——它是兜底 `push` 之前的最后一道关卡，专门处理「边界元素」。
- 区分三种被静默过滤/记录的元素：游离的 `SpaceElem`、作为分组边界的 `ParbreakElem`、以及「附着型」`VElem`，并解释**为什么**它们不能原样进入 `sink`。
- 理解 `may_attach` 这个一位状态机：它如何只被 `ParElem` 置位、被 `parbreak` 复位，从而决定紧凑列表的前导垂直间距能否存活。
- 掌握 `attach` 字段从 typst-layout 的内置 show 规则产生、到 typst-realize 消费、再到下游布局再也看不到它的完整生命周期。

本讲是专家层（u3）的第 2 篇，承接 u1-l3 的 `visit()` 调度骨架，聚焦其中最容易被忽视、却直接决定「文档边界处空白是否正确」的一步。

## 2. 前置知识

阅读本讲前，请确保你已经理解以下概念（它们在 u1-l1 ~ u1-l3、u2-l1 已建立）：

- **realization（具现化）**：递归套用样式与 show 规则，把任意 content 树规整成扁平的、由后端已知元素组成的清单 `Vec<Pair>`。
- **`visit()` 调度流水线**：对每件 content 按固定顺序尝试 8 步（TagElem 直推 → kind 规则 → show 规则 → 序列递归 → 样式递归 → 分组 → **过滤** → 兜底 push）。任一命中即短路返回。本讲讲的就是第 7 步「过滤」。
- **`Pair<'a> = (&'a Content, StyleChain<'a>)`**：`sink` 的基本单元。
- **`State`**：realize 过程唯一的可变状态机，`visit()` 每命中一条分支就改写它的某个字段。
- **`RealizationKind`**：五种具现化场景（Bundle / Document / Fragment / Par / Math），不同 kind 选用不同的规则表。

一个关键直觉：**并不是所有 content 都该进入最终输出**。源文本里到处都是空格、空行、段落断行，它们在「拼成段落」「分隔块」时有用，但一旦完成了自己的「边界」使命，就不应再作为独立元素躺进输出清单——否则下游排版会在文档首尾、块与块之间多出一堆莫名的空白。`visit_filter_rules` 就是负责把这些「完成使命的边界元素」清理掉的清道夫。

## 3. 本讲源码地图

本讲几乎全部聚焦于一个文件，辅以两个「上下游」文件说明数据来源与去向：

| 文件 | 作用 |
| --- | --- |
| [crates/typst-realize/src/lib.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs) | 本讲主角。`visit_filter_rules`、`visit()` 调度顺序、`State` 的 `may_attach`/`saw_parbreak` 字段都在此。 |
| [crates/typst-library/src/layout/spacing.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/layout/spacing.rs) | `VElem` 的定义，包含本讲的核心字段 `attach: bool`。 |
| [crates/typst-layout/src/rules.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-layout/src/rules.rs) | 「附着型」`VElem` 的**产生地**：紧凑列表（list/enum/terms）的内置 show 规则在这里用 `.with_attach(true)` 生成前导间距。 |
| [crates/typst-library/src/text/space.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/text/space.rs) | `SpaceElem` 的定义。 |
| [crates/typst-library/src/model/par.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/model/par.rs) | `ParbreakElem`（段落断行）与 `ParElem`（成品段落）的定义。 |

---

## 4. 核心概念与源码讲解

### 4.1 过滤规则在调度流水线中的位置

#### 4.1.1 概念说明

回忆 u1-l3：`visit()` 对每件 content 按固定顺序尝试 8 步，任一命中即短路返回。这 8 步里，真正会**改写或归并**内容的是四个 `visit_*_rules` 子函数（kind / show / grouping / filter）。其中 `visit_filter_rules` 排在**倒数第二**——紧跟在「分组」之后、兜底 `push` 之前。

为什么是这个位置？因为过滤面对的是「**前面所有步骤都不认领、却又不该原样输出**」的元素。这些元素绝大多数是「边界元素」：它们在帮助分组、分页、分段的时刻发挥过作用，但作用已经结束。把它们放在分组之后，意味着：能被分组吸收的（比如段落内的空格）早已被 `visit_grouping_rules` 收走；轮到过滤这一步时，剩下的都是「游离」的边界残片。

一个要点：`visit_filter_rules` 与其它三个 `visit_*_rules` 共用同一个 `SourceResult<bool>` 接口约定——返回 `true` 表示「我已认领这个元素，调用方短路」，返回 `false` 表示「与我无关，继续下一步」。

#### 4.1.2 核心流程

`visit()` 中调用过滤的位置（注意它在分组之后）：

```
... 分组尝试（visit_grouping_rules）若命中则 return ...

// Some elements are skipped based on specific circumstances.
if visit_filter_rules(s, content, styles)? {
    return Ok(());      // 被过滤认领，不再 push
}

// 兜底：直接 push 到 sink
s.sink.push((content, styles));
```

`visit_filter_rules` 自身的判定顺序（伪代码）：

```
若 kind 是 Par 或 Math：
    直接返回 false（这两种 realize 不做过滤，空格在它们里是「一等公民」）

若 content 是 SpaceElem：           → 丢弃（返回 true）
否则若 content 是 ParbreakElem：    → 记录边界、复位 may_attach（返回 true）
否则若 (may_attach 为 false) 且 (content 是 attach=true 的 VElem)：
                                    → 丢弃（返回 true）
否则：
    may_attach ← (content 是否为 ParElem)   // 维护状态机
    返回 false（交由兜底 push）
```

注意三个细节：

1. **Par / Math 早退**：过滤逻辑只对块级（Document / Fragment / Bundle）realize 生效。
2. **三个分支短路**：SpaceElem、ParbreakElem、被丢弃的 attach-VElem 都在更新 `may_attach` **之前** `return`，因此它们**不改动** `may_attach`（其中 ParbreakElem 显式复位为 `false`）。
3. **状态机维护在最末**：只有「幸存」到最后的元素才会执行 `may_attach = content.is::<ParElem>()`。

#### 4.1.3 源码精读

先看 `visit()` 里过滤这一步的定位——它是兜底 push 之前的最后一道关卡：

[crates/typst-realize/src/lib.rs:L284-L293](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L284-L293) —— `visit()` 中调用 `visit_filter_rules`：命中则短路、不 push；全不命中才推进 `sink`。

再看 `visit_filter_rules` 全函数主体：

[crates/typst-realize/src/lib.rs:L751-L785](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L751-L785) —— 过滤规则本体。函数顶部的文档注释一句道破天机：「Some elements don't make it to the sink depending on the realization kind and current state.」（有些元素依 realiaztion kind 与当前状态，不会进入 sink）。

最后看两个被过滤逻辑读写的状态字段在 `State` 中的声明：

[crates/typst-realize/src/lib.rs:L104-L107](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L104-L107) —— `may_attach`（后续的 attach 间距能否存活）与 `saw_parbreak`（是否遇到过段落断行）。

它们在 `realize()` 入口被初始化为 `false`：

[crates/typst-realize/src/lib.rs:L65-L66](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L65-L66) —— `may_attach: false, saw_parbreak: false`，即每次 realize 调用开始时，「尚未遇到任何段落」。

#### 4.1.4 代码实践

**实践目标**：直观感受「过滤是兜底前的最后一道关卡」，并统计一次 realize 中各分支的命中次数。

**操作步骤**：

1. 打开 `crates/typst-realize/src/lib.rs`，在 `visit_filter_rules` 函数体最开头（第 758 行的 `if matches!` 之前）临时插入一行计数日志：

   ```rust
   // 示例代码：仅用于学习，验证后请删除
   eprintln!("[filter] kind={:?} elem={}", s.kind, content.elem().name());
   ```

   （`RealizationKind` 已实现 `Debug`；`content.elem().name()` 返回元素类型名。）

2. 准备一个最小 Typst 文档 `filter.typ`：

   ```typst
   第一段文字。

   - 紧凑列表项 A
   - 紧凑列表项 B

   第二段文字。
   ```

3. 用本地构建的 typst 编译，观察 stderr：

   ```bash
   cargo run --release -p typst-cli -- compile filter.typ 2>filter.log
   ```

**需要观察的现象**：日志里应出现 `SpaceElem`、`ParbreakElem`、`ListElem`/`ParElem` 等不同元素名，且 `SpaceElem` 与 `ParbreakElem` 的出现次数与你直觉中「游离空格 / 空行」的数量相关。

**预期结果**：每遇到一个游离 `SpaceElem` 或 `ParbreakElem`，都会打印一行 filter 日志——因为它们正是被本函数「认领并丢弃」的边界元素。

> 若本地无法构建完整 typst CLI，可仅做源码阅读：在脑中（或纸面上）把上面 `filter.typ` 的每个 token 过一遍 `visit()` 的 8 步，标出哪些会落到 `visit_filter_rules`。具体运行输出**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `visit_filter_rules` 从 `visit()` 中删掉（直接让所有未被认领的元素走兜底 push），文档外观最可能在哪类位置出错？

**参考答案**：在文档首尾、块与块之间会出现多余的垂直/水平空白，并可能多出空段落。因为游离的 `SpaceElem` 与 `ParbreakElem` 会原样进入 `sink`，被下游排版当成真实内容处理。

**练习 2**：`visit_filter_rules` 返回 `true` 与返回 `false` 分别对 `visit()` 的调用方意味着什么？

**参考答案**：`true` = 「我已认领该元素（丢弃或记录）」，调用方立刻 `return Ok(())`，不再走兜底 push；`false` = 「与我无关」，调用方继续执行 `s.sink.push(...)`。

---

### 4.2 SpaceElem 与 ParbreakElem 的处理

#### 4.2.1 概念说明

**SpaceElem（文本空格）** 与 **ParbreakElem（段落断行）** 是两类典型的「边界元素」。它们的共同点是：**在块级（FLOW）realize 里，它们的使命是充当分组的触发器或边界，而不是成为独立输出**。

- `SpaceElem` 在 u2-l8 中已介绍：它是 `PAR` 分组的 `Inner` 元素（段落内的空格被段落吸收）。但当一个空格**游离在段落之外**——比如两个块级元素之间的空格，或文档开头的空格——它无法被任何活动分组吸收，就成了「残片」。在 FLOW realize 里，这类残片空格没有任何意义，应当丢弃。
- `ParbreakElem` 表示一次段落断行（源码里的空行，或显式的 `#parbreak()`）。它在 FLOW realize 里**仅作为分组的边界信号**存在——它的出现会影响 `saw_parbreak`（进而影响 Fragment 的行内回退，见 u2-l8），但它的「本体」不应进入 `sink`。

需要特别强调一句注释里的话：**「spaces that were not collected by the paragraph grouper don't interest us」**（未被段落分组收集的空格，我们不感兴趣）。这把 SpaceElem 的丢弃限定得很精确——不是「所有空格都丢」，而是「段落分组没收走的空格才丢」。被段落分组收走的空格，其折叠由 u2-l11 的 `spaces.rs` 在段落内部处理，与本讲的过滤无关。

#### 4.2.2 核心流程

两种元素的处理可以并排对比：

| 元素 | 处理动作 | 是否进入 sink | 是否改 may_attach |
| --- | --- | --- | --- |
| `SpaceElem` | 直接丢弃 | 否 | 否（保持原值） |
| `ParbreakElem` | 复位 `may_attach=false`、置 `saw_parbreak=true` | 否 | 是（置 false） |

为什么 `SpaceElem` 不改 `may_attach`，而 `ParbreakElem` 要显式复位？这是 4.3 节的关键伏笔：游离空格在「段落 → attach 间距」之间是**透明**的（不影响附着判定），而显式段落断行会**切断**附着关系。

还有一个前置门槛：**整个过滤逻辑只对非 Par / 非 Math 的 realize 生效**。原因有二：

- 在 **Par realize** 里，空格是行内排版的一等公民（行首/行尾/词间空格由 `collapse_spaces` 在段落顶层处理，见 u2-l11），不能丢。
- 在 **Math realize** 里同理，数学空格有实际间距含义。

所以函数开头一句 `matches!(s.kind, Par | Math)` 就 `return Ok(false)`，把这两种 kind 完全排除在过滤之外。

#### 4.2.3 源码精读

先看 SpaceElem 与 ParbreakElem 的类型定义，建立直觉：

[crates/typst-library/src/text/space.rs:L8-L10](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/text/space.rs#L8-L10) —— `SpaceElem` 是一个无字段的标记元素（「A text space」）。

[crates/typst-library/src/model/par.rs:L707-L726](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/model/par.rs#L707-L726) —— `ParbreakElem`，文档明确：「This starts a new paragraph.」「Multiple consecutive paragraph breaks collapse into a single one.」它同样是无字段的标记元素。

再看过滤函数中处理这两种元素的三个分支：

[crates/typst-realize/src/lib.rs:L758-L771](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L758-L771) —— 先排除 Par/Math；`SpaceElem` 直接 `return Ok(true)` 丢弃；`ParbreakElem` 在 `return` 前执行 `s.may_attach = false;` 与 `s.saw_parbreak = true;`。两条注释直接点明设计意图：「spaces that were not collected by the paragraph grouper don't interest us」与「Paragraph breaks are only a boundary for paragraph grouping, we don't need to store them.」

`saw_parbreak` 的唯一消费者在 `is_fully_inline_or_neutral`：

[crates/typst-realize/src/lib.rs:L1173-L1186](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L1173-L1186) —— Fragment realize 中，一旦 `saw_parbreak` 为真，就不再满足「完全行内」的条件，从而放弃行内回退、强制生成 `ParElem`。也就是说：显式段落断行会把一个本可保持行内的 Fragment 内容「提升」为段落。

#### 4.2.4 代码实践

**实践目标**：分别触发 SpaceElem 丢弃与 ParbreakElem 记录，验证二者都不进入 `sink`、且 ParbreakElem 会翻转 `saw_parbreak`。

**操作步骤**：

1. 在 `visit_filter_rules` 的 SpaceElem 分支与 ParbreakElem 分支各加一条日志（示例代码）：

   ```rust
   if content.is::<SpaceElem>() {
       eprintln!("[filter] DROP SpaceElem (may_attach={} the drop)", s.may_attach);
       return Ok(true);
   } else if content.is::<ParbreakElem>() {
       eprintln!("[filter] RECORD ParbreakElem (saw_parbreak -> true)");
       s.may_attach = false;
       s.saw_parbreak = true;
       return Ok(true);
   }
   ```

2. 准备文档 `edge.typ`（块级游离空格 + 显式空行）：

   ```typst
   #align(center)[居中块]

   #align(right)[右对齐块]
   ```

   两行之间的空行会产生 `ParbreakElem`；块之间可能产生游离 `SpaceElem`。

3. 编译并查看日志：

   ```bash
   cargo run --release -p typst-cli -- compile edge.typ 2>edge.log
   ```

**需要观察的现象**：日志中 `RECORD ParbreakElem` 与 `DROP SpaceElem` 各出现若干次；最终 PDF 中两个对齐块之间没有多余空段落。

**预期结果**：确认 ParbreakElem 仅作为边界被记录、SpaceElem 被静默丢弃，二者都不出现在最终 `sink` 里。具体次数**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `visit_filter_rules` 在 Par realize 与 Math realize 里直接返回 `false`、什么也不过滤？

**参考答案**：因为在段落和数学排版中，空格是行内排版的一等公民——词间距、行首/行尾空格都需要保留并由 `collapse_spaces`（u2-l11）处理。若在这里丢弃，会破坏行内空格语义。

**练习 2**：`saw_parbreak` 被置为 `true` 后，会在哪里、产生什么后果？

**参考答案**：在 `is_fully_inline_or_neutral`（u2-l8）中被读取。对 Fragment realize，`saw_parbreak` 为真意味着内容不再是「完全行内」，于是放弃行内回退、强制把内容包成 `ParElem`。

---

### 4.3 VElem attach 折叠与 may_attach 状态机

#### 4.3.1 概念说明

这是本讲最精巧的部分。先看一个现象：在 Typst 里写一个**紧凑列表**（tight list，即项之间没有空行的列表）紧跟在一段正文后面，列表会「贴」着正文，二者之间的间距与正文行距一致；而如果列表紧跟在另一个**块**（比如标题、另一个列表）后面，这段间距会消失。

实现这个语义的关键，就是一个名为 `attach` 的 `VElem` 内部字段，加上一位状态机 `may_attach`。

- **`VElem`** 是垂直间距元素（`#v(...)`、以及各种自动产生的块间间距）。
- **`attach: bool`** 是它的一个 `#[internal]` 字段，文档原文：「Whether the spacing collapses if not immediately preceded by a paragraph.」（若不紧跟在段落之后，该间距折叠/消失）。
- **谁会产生 `attach=true` 的 VElem？** 是 typst-layout 里**紧凑列表的内置 show 规则**。当 list/enum/terms 是 tight 时，它们的内置 show 规则会在自身前面拼一段「弱、附着」的前导间距（见 4.3.3）。这段间距的本意是：「我想作为前一个段落的尾随行距附着上去」。
- **`may_attach`** 是 realize 的一位状态：**只有当刚刚处理过的（未被过滤的）元素是一个 `ParElem` 时**，它才为 `true`。它回答的问题是：「下一个到来的 attach 间距，前面是否紧跟着一个段落？」

于是折叠逻辑呼之欲出：一个 `attach=true` 的 VElem 到来时，若 `may_attach` 为 `false`（前面不是段落），就把它丢弃；若为 `true`（前面是段落），就放行，让它进入 `sink` 成为真实的垂直间距。

#### 4.3.2 核心流程

`may_attach` 状态机的转移规则（这是本节的核心）：

```
初始（realize 入口）：may_attach = false

每处理一个「幸存」元素（未被过滤短路）：
    may_attach ← (该元素是否为 ParElem)

遇到 ParbreakElem：may_attach ← false   （显式复位）
遇到 SpaceElem：    不变                  （游离空格透明）
遇到被丢弃的 attach-VElem：不变          （短路返回前不更新）
遇到幸存的 attach-VElem：may_attach ← false（VElem 不是 ParElem）
```

关键不变量：**`may_attach` 为 `true` 的窗口，只存在于「一个 ParElem 之后」到「下一个非 ParElem（且非游离空格）元素之前」之间**。游离空格不会关闭这个窗口——所以「段落 → 若干游离空格 → attach 间距」中，attach 间距仍能存活。而一个显式 `parbreak` 会**主动关闭**这个窗口。

attach-VElem 的判定与折叠：

```
若 (may_attach == false) 且 (content 是 VElem 且其 attach 字段在 styles 下为 true)：
    丢弃（返回 true）
```

注意三个要点：

1. **attach 通过样式链读取**：`elem.attach.get(styles)`，和其它元素字段一样从 `StyleChain` 解析（可被 set 规则覆盖，尽管它是 internal 字段）。
2. **只折叠 attach=true 的 VElem**：普通 `#v(10pt)` 的 `attach` 默认是 `false`（构造时 `#[parse(Some(false))]`），永远不会被这里丢弃。
3. **折叠只发生在非 Par / 非 Math realize**：因为开头那句 `matches!(s.kind, Par | Math)` 已经把这两种 kind 排除。

#### 4.3.3 源码精读

先看 `VElem` 的 `attach` 字段定义：

[crates/typst-library/src/layout/spacing.rs:L116-L121](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/layout/spacing.rs#L116-L121) —— `attach: bool`，标注 `#[internal]` 与 `#[parse(Some(false))]`。`#[parse(Some(false))]` 意味着用户构造 VElem 时该字段默认 `false`；它是内部字段，只能由 Typst 自身代码设置。

再看「attach=true 的 VElem」是哪里产生的——紧凑列表的内置 show 规则：

[crates/typst-layout/src/rules.rs:L124-L141](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-layout/src/rules.rs#L124-L141) —— `LIST_RULE`：当列表为 tight 时，用 `VElem::new(spacing.into()).with_weak(true).with_attach(true)` 造一段「弱且附着」的前导间距，并拼在 realized 内容最前面（`realized = v + realized`）。`ENUM_RULE`（[L143-L160](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-layout/src/rules.rs#L143-L160)）与 `TERMS_RULE`（[L204-L215](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-layout/src/rules.rs#L204-L215)）对枚举与术语列表做完全相同的事。

> 这三处是「attach 间距的唯一产生地」。所以本讲的 attach 折叠，本质上是为「紧凑列表紧贴前段」这个排版惯例服务的。

然后看 realize 里消费 `attach` 的折叠分支：

[crates/typst-realize/src/lib.rs:L772-L779](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L772-L779) —— 条件 `!s.may_attach && content.to_packed::<VElem>().is_some_and(|elem| elem.attach.get(styles))` 成立时丢弃。注释：「Attach spacing collapses if not immediately following a paragraph.」

紧随其后的状态机维护：

[crates/typst-realize/src/lib.rs:L781-L784](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L781-L784) —— `s.may_attach = content.is::<ParElem>();`，注释「Remember whether following attach spacing can survive.」

最后，确认一个重要事实：**下游布局再也看不到 `attach` 字段**。typst-layout 的 `collect.rs` 在收集 VElem 时只读 `weak`，不读 `attach`：

[crates/typst-layout/src/flow/collect.rs:L147-L154](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-layout/src/flow/collect.rs#L147-L154) —— `v()` 把 VElem 收成 `Child::Rel` / `Child::Fr`，只用到了 `elem.amount` 与 `elem.weak`。这证明 `attach` 是一个**纯 realize 期**的字段：它在具现化阶段决定「存留」，一旦决定存活，进入布局后就和普通 VElem 无异。

#### 4.3.4 代码实践

**实践目标**：观察 `may_attach` 在「段落 → 紧凑列表」与「块 → 紧凑列表」两种场景下的差异，并验证 attach 间距的存留。

**操作步骤**：

1. 在折叠分支与状态机维护处各加日志（示例代码）：

   ```rust
   } else if !s.may_attach
       && content.to_packed::<VElem>().is_some_and(|elem| elem.attach.get(styles))
   {
       eprintln!("[filter] DROP attach-VElem (may_attach=false)");
       return Ok(true);
   }
   eprintln!("[filter] SURVIVE elem={} -> may_attach={}",
       content.elem().name(), content.is::<ParElem>());
   s.may_attach = content.is::<ParElem>();
   ```

2. 准备两个对照文档。

   场景 A（列表紧跟段落，attach 间距应存活）：

   ```typst
   这是一段正文。
   - 紧凑项一
   - 紧凑项二
   ```

   场景 B（列表紧跟另一个块，attach 间距应被丢弃）：

   ```typst
   = 标题
   - 紧凑项一
   - 紧凑项二
   ```

3. 分别编译，对比日志：

   ```bash
   cargo run --release -p typst-cli -- compile sceneA.typ 2>A.log
   cargo run --release -p typst-cli -- compile sceneB.typ 2>B.log
   ```

**需要观察的现象**：

- 场景 A：日志里应先出现 `SURVIVE elem=par -> may_attach=true`（成品段落 `ParElem` 把 `may_attach` 抬起），随后到来的 attach-VElem **不出现 DROP**，而是存活（或在存活日志里可见）。
- 场景 B：标题不是 `ParElem`，`may_attach` 保持 `false`，attach-VElem 到来时打印 `DROP attach-VElem (may_attach=false)`。

**预期结果**：两份 PDF 中，列表与上方内容的间距不同——场景 A 列表贴着段落行距，场景 B 列表与标题之间无额外前导间距。日志中 `may_attach` 的翻转与 DROP 是否发生应与上述一致。具体输出**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：假设把状态机维护那行 `s.may_attach = content.is::<ParElem>();` 改成 `s.may_attach = true;`（永远为真），紧凑列表的排版会在什么场景下表现异常？

**参考答案**：attach 间距会**永远存活**。于是即使列表紧跟在另一个块（标题、另一个列表、图片）后面，那段本应折叠的前导间距也会保留，导致块与紧凑列表之间多出一段不该有的垂直间距。

**练习 2**：为什么游离 `SpaceElem` 不复位 `may_attach`，而 `ParbreakElem` 要复位？结合「段落 → 空格 → 紧凑列表」设想。

**参考答案**：因为游离空格在「段落与紧随其后的 attach 间距」之间应当是**透明**的——用户在段落和列表之间多敲了几个空格，不应切断列表对段落的附着。所以 `SpaceElem` 保持 `may_attach` 不变。而 `ParbreakElem` 代表一次**显式的段落断行**，语义上意味着「段落到此结束」，应当切断附着关系，故显式复位为 `false`。

**练习 3**：用户在源码里写 `#v(10pt)`，这段间距会被 `visit_filter_rules` 丢弃吗？为什么？

**参考答案**：不会。`#v(10pt)` 产生的 VElem 其 `attach` 字段默认为 `false`（构造期 `#[parse(Some(false))]`），折叠分支的条件 `elem.attach.get(styles)` 不成立，故不会被丢弃，会正常进入 `sink`。

---

## 5. 综合实践

把本讲三个模块串起来，做一个「边界元素追踪」实验。

**任务**：用一张表预测下列文档中每个关键元素在 `visit_filter_rules` 处的命运（丢弃 / 记录 / 存活），然后用日志验证。

文档 `combine.typ`：

```typst
引言段落，后面紧跟一个紧凑列表。

- 项一
- 项二

#v(8pt)

= 章节标题

   （这行前面有缩进空格，是游离 SpaceElem 的来源）

尾声段落。
```

**预测步骤**：

1. 「引言段落」会被分组收成 `ParElem` 并存活 → `may_attach` 抬起为 `true`。
2. 紧凑列表的内置 show 规则产生 attach-VElem → 因 `may_attach=true` 而**存活**。
3. 段落与列表之间的空行产生 `ParbreakElem` → 被记录、复位 `may_attach=false`、置 `saw_parbreak=true`。
4. `#v(8pt)` 是普通 VElem（attach=false）→ **存活**。
5. `= 章节标题` 不是 `ParElem` → `may_attach` 复位为 `false`。
6. 标题后的缩进空格是游离 `SpaceElem` → **丢弃**（且不改 `may_attach`）。
7. 「尾声段落」收成 `ParElem` → `may_attach` 再次抬起。

**验证**：把 4.1.4、4.2.4、4.3.4 三处的日志合并启用，编译 `combine.typ`，对照 stderr 输出逐行核对你的预测。重点关注：attach-VElem 是否如预测在第 2 步存活、在第 5 步之后若有 attach-VElem 是否会被丢弃。

> 这是「源码阅读 + 行为预测」型实践。若本地可构建 typst，请用 `cargo run --release -p typst-cli -- compile combine.typ 2>combine.log` 实测；运行结果**待本地验证**。

---

## 6. 本讲小结

- `visit_filter_rules` 是 `visit()` 调度流水线的第 7 步（兜底 push 前的最后一关），用 `SourceResult<bool>` 接口与其它三个 `visit_*_rules` 一致：`true` 表示认领（丢弃/记录）、`false` 表示放行。
- 它只对**非 Par / 非 Math** 的 realize 生效——段落与数学排版里空格是一等公民，不能丢。
- **`SpaceElem`**：未被段落分组收走的游离空格被静默丢弃；被段落收走的空格其折叠由 `spaces.rs`（u2-l11）在段内处理，与本讲无关。
- **`ParbreakElem`**：仅作为分组边界被记录（`saw_parbreak=true`、`may_attach=false`），本体不进 `sink`；`saw_parbreak` 会取消 Fragment 的行内回退（u2-l8）。
- **`VElem` 的 attach 折叠**：紧凑列表内置 show 规则（typst-layout 的 LIST/ENUM/TERMS_RULE）产生 `attach=true` 的前导间距；它仅在 `may_attach=true`（即前面紧跟一个 `ParElem`）时存活，否则被丢弃。
- **`may_attach` 状态机**：仅 `ParElem` 置 `true`、`ParbreakElem` 显式复位、游离 `SpaceElem` 透明不改；下游布局（`collect.rs`）只读 `weak` 不读 `attach`，证明 attach 是纯 realize 期字段。

## 7. 下一步学习建议

本讲讲完了 `visit()` 的第 7 步「过滤」。接下来建议：

- **横向补全调度流水线**：若还未读 u3-l1（标签与内省 TagElem），建议接着读——它与本讲同属 `visit()` 的「边缘」机制，TagElem 是第 1 步直推、过滤是第 7 步，二者都涉及「元素如何绕过常规改写」。
- **纵向深入状态机**：`may_attach` 与 `saw_parbreak` 都是 `State` 的工作区标志位，可对照 u2-l1（State 状态机与分组栈）复习「配置型字段 vs 工作区字段」的分类。
- **跨 crate 视角**：本讲首次引入了 typst-layout 的 `rules.rs` 与 `collect.rs`。若想系统理解 realize 与 layout 的衔接，可继续阅读 u3-l5（与 layout / html / bundle / math 的集成），那里会画出完整的 routine 调用关系图。
