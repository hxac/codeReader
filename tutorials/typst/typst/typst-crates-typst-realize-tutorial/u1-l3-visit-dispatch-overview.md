# visit() 调度流水线总览

## 1. 本讲目标

上一讲我们读完了 `realize()` 的入口与核心数据类型，知道它「搭好 `State`、调用 `visit()`、再 `finish()` 收尾」。但 `visit()` 内部到底是怎么把一棵任意的 content 树，一步步变成扁平的「已知元素清单」的？本讲就来拆开这条调度流水线。

学完本讲，你应该能够：

- 说出 `visit()` 中 7 条 `if` 分支 + 1 条最终 `push` 的**固定判定顺序**，以及每条命中后为何立即 `return`（短路语义）。
- 解释为什么顺序是这样排的（例如「kind 规则必须早于 show 规则」「show 规则必须早于序列/样式递归」）。
- 理解「well-known elements（后端已知元素）」与 `sink` 输出的关系：哪些元素最终会落到 `sink`，哪些会被中途拦截、改写或丢弃。
- 在脑海里建立起 **realize 主循环的整体心智模型**：`realize()` 只调用一次 `visit(root)`，而 `visit()` 是一个递归下降的过程，所有内容都从这一个入口流过。

本讲是**总览**：我们只看 `visit()` 的骨架与四个 `visit_*_rules` 子函数的分工，不深入任何一个子函数的内部细节——那是后续 u2 系列讲义的主题。

## 2. 前置知识

本讲承接 u1-l1 与 u1-l2，假定你已经了解：

- **realization（具现化）**：递归套用样式与 show 规则，把任意、用户可扩展的 content 树规整成「全部由后端已知元素组成、且样式齐全」的扁平清单。（见 u1-l1）
- **`realize()` 的签名与返回值**：输入 `content` 与 `styles`（一条 `StyleChain`），输出 `Vec<Pair>`，其中 `Pair<'a> = (&'a Content, StyleChain<'a>)`。（见 u1-l2）
- **`RealizationKind`** 有五个变体（`Bundle`/`Document`/`Fragment`/`Par`/`Math`），决定本次具现化用哪张静态分组规则表。（见 u1-l2）
- **`State`** 是具现化的可变状态机，其中 `sink: Vec<Pair>` 是输出槽，`visit()` 最终把已知元素推进这里。（见 u1-l2）

补充两个本讲会反复用到、但前面没展开的概念：

- **Content（内容）**：Typst 里一切可见之物（文本、标题、列表、段落……）的统一表示。一棵 content 树的内部节点通常是 `SequenceElem`（多个内容并列）和 `StyledElem`（给子内容附加一组样式），叶子才是文本、标题这类「实际」元素。
- **短路（short-circuit）**：`visit()` 里每条 `if` 分支一旦「认领」了当前元素，就立刻 `return Ok(())`，不再往下走。所以**每个元素只会被一条分支处理**（递归除外）。理解这一点是理解整条流水线的关键。

## 3. 本讲源码地图

本讲几乎只看一个文件，但会顺手点出它如何被入口调用：

| 文件 | 作用 |
| --- | --- |
| [src/lib.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs) | typst-realize 的全部主逻辑。本讲的焦点 `visit()` 与四个 `visit_*_rules` 子函数都在这里。 |
| [src/lib.rs（入口 `realize()`）](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L43-L74) | `realize()` 在第 70 行调用 `visit()`、第 71 行调用 `finish()`。本讲从第 70 行展开。 |

> 链接基准 HEAD：`32fd4cc3861e0ab99f4c42ca6bea281482ba9f51`。下面所有永久链接均基于此 HEAD。

## 4. 核心概念与源码讲解

### 4.1 visit() 的分发骨架：固定顺序与短路逻辑

#### 4.1.1 概念说明

`visit()` 是整条具现化流水线的**核心调度器**。你可以把它想象成机场的「值机柜台分流」：每一件来到它面前的 content，都要依次经过 7 个检查站，只要在某个站被「认领」，就不再继续往后走；如果一路畅通通过所有检查站，才会被「装上飞机」——推进 `sink`。

为什么需要这样一条**固定顺序**的流水线？因为不同变换之间有依赖：

- **kind 规则**（按具现化类型变换）必须**早于** show 规则：例如在非数学上下文里，`SymbolElem`（符号）要先转成 `TextElem`，后续 show 规则才有意义；源码注释明确写着 *Needs to happen before show rules*。
- **show 规则**（含 `prepare` 准备）必须**早于**序列/样式递归：因为 `SequenceElem`/`StyledElem` 这类容器**也可能带 label**，需要先在容器自身上跑 `prepare`（分配 location、生成 tag），然后才能安全地拆开递归到子元素。源码注释：*Styled elements and sequences can currently also have labels, so this needs to happen before they are handled.*
- **分组**必须**晚于** show 与递归：分组是把若干「已经完全具现化的叶子」收集成复合元素（如把一段文字收成 `ParElem`、把若干列表项收成 `ListElem`）。只有当 content 被拆到叶子、且 show 规则已套用完毕，分组才有意义。
- **过滤**放在分组之后：在非 `Par`/`Math` 具现化中，落单的空格、段落断点等需要被悄悄丢弃。

「well-known elements」就是这条流水线的**终点产物**：layout（排版）与 export（如 HTML）后端认识、能直接处理的元素。`visit()` 的职责正是通过层层变换，把任意 content 归约成这些已知元素，再 `push` 进 `sink`。

#### 4.1.2 核心流程

`visit()` 对每个收到的 content，按如下**严格顺序**逐条尝试，命中即 `return`（短路）：

```
visit(content, styles):
  1. 是 TagElem?            → 直接 push 到 sink，返回          # 元数据，永不变换
  2. visit_kind_rules 命中? → 返回                            # 按类型变换（math/flow）
  3. visit_show_rules 命中? → 返回                            # 套用 show 规则 + prepare
  4. 是 SequenceElem?       → 对每个 child 递归 visit，返回    # 拆开并列容器
  5. 是 StyledElem?         → 转交 visit_styled，返回          # 拆开带样式容器
  6. visit_grouping_rules 命中? → 返回                         # 收集多元素成复合元素
  7. visit_filter_rules 命中? → 返回                           # 丢弃不该出现的元素
  8. （兜底）push 到 sink，返回                                # 这是一件 well-known 元素
```

几个要点：

- **短路**：步骤 1–7 任一命中都立即返回，只有全部不命中，元素才走到步骤 8 被直接输出。这意味着一件叶子元素要么被改写/丢弃，要么原样进 `sink`。
- **递归**：步骤 4、5 会再次调用 `visit()`（步骤 5 经由 `visit_styled`）。此外，show 规则产出的新内容、分组完成后新生成的 `ParElem`/`ListElem` 等，也都会**再次进入 `visit()`**。所以 `visit()` 是整个具现化的唯一递归入口。
- **「命中」的返回值约定**：四个 `visit_*_rules` 子函数都返回 `SourceResult<bool>`，`true` 表示「我处理了，调用方应短路返回」，`false` 表示「与我无关，继续往下」。`visit()` 用 `if visit_xxx(...)? { return Ok(()); }` 这一统一句式消费它们。

#### 4.1.3 源码精读

先看 `realize()` 如何启动 `visit()`（第 70–71 行）——`realize()` 自身不含递归，它只负责搭好 `State` 然后调一次 `visit()`：

[src/lib.rs:L70-L71](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L70-L71) —— `realize()` 调用 `visit(&mut s, content, styles)` 递归处理整棵 content 树，再调用 `finish()` 收尾未关闭的分组。

再看 `visit()` 全貌（分四段讲解）：

[src/lib.rs:L242-L294](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L242-L294) —— `visit()` 函数体。它按固定顺序尝试 7 条分支，最后兜底 `push`。

**步骤 1：TagElem 直推。**

[src/lib.rs:L247-L251](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L247-L251) —— Tag（内省用标签）是元数据，不需要任何变换，直接连同样式 push 到 `sink`。这是整条流水线里最轻的路径。

**步骤 2：kind 规则。**

[src/lib.rs:L255-L257](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L255-L257) —— 调用 `visit_kind_rules`。注释强调它**必须早于** show 规则。

**步骤 3：show 规则与 prepare。**

[src/lib.rs:L260-L262](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L260-L262) —— 调用 `visit_show_rules`，套用用户/内置 show 规则，并在首次访问时执行 `prepare`（分配 location、合成字段、生成 tag）。

**步骤 4：序列递归。**

[src/lib.rs:L264-L271](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L264-L271) —— 若 content 是 `SequenceElem`，则对其每个 child **递归调用 `visit()`**。注释解释了为什么序列/样式容器要在 show 之后才拆：它们也可能带 label，需要先在第 3 步被 prepare。

**步骤 5：样式递归。**

[src/lib.rs:L273-L276](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L273-L276) —— 若 content 是 `StyledElem`，转交 `visit_styled`（它会在恰当处理样式后再次进入 `visit()`）。注意这里直接 `return visit_styled(...)`，没有额外的 `if ... { return }` 包裹——因为 `StyledElem` 命中后必然要走它。

**步骤 6：分组。**

[src/lib.rs:L278-L282](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L278-L282) —— 调用 `visit_grouping_rules`。分组会把连续的叶子元素收集起来，时机成熟时合并成一个复合元素（如 `ParElem`）。

**步骤 7：过滤。**

[src/lib.rs:L284-L287](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L284-L287) —— 调用 `visit_filter_rules`。在某些 kind 下，落单的空格、段落断点、attach 间距等会被悄悄丢弃。

**步骤 8：兜底 push。**

[src/lib.rs:L289-L293](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L289-L293) —— 七道关卡都没拦下，说明这是一件**已完全具现化的 well-known 元素**，连同样式 push 进 `sink`。这正是 layout/export 后端最终拿到的东西。

最后看一眼输出的目的地：

[src/lib.rs:L95-L96](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L95-L96) —— `State.sink: Vec<Pair<'a>>`，所有 `push` 的终点，也是 `realize()` 最终 `Ok(s.sink)` 返回给调用方的清单。

#### 4.1.4 代码实践

**实践目标**：用插桩（instrumentation）亲眼看到 `visit()` 各分支的命中顺序与次数，验证短路语义。

**操作步骤**：

1. 在 typst 仓库根目录构建 CLI（首次较慢）：

   ```bash
   cargo build -p typst-cli
   ```

2. 打开 `crates/typst-realize/src/lib.rs`，在文件顶部 `use` 之后插入一组全局原子计数器与便捷宏（示例代码，仅供学习，验证后请还原）：

   ```rust
   // ===== 仅供学习插桩，验证后请删除 =====
   use std::sync::atomic::{AtomicUsize, Ordering};
   macro_rules! ctr { ($id:ident) => { static $id: AtomicUsize = AtomicUsize::new(0); }; }
   ctr!(C_TAG); ctr!(C_KIND); ctr!(C_SHOW);
   ctr!(C_SEQ); ctr!(C_STYLED); ctr!(C_GROUP);
   ctr!(C_FILTER); ctr!(C_PUSH);
   macro_rules! hit { ($c:ident) => { $c.fetch_add(1, Ordering::Relaxed); }; }
   // =====================================
   ```

3. 在 `visit()` 的 8 个出口分别插入 `hit!(...)`（每个 `return Ok(())` 之前，以及最后的 `s.sink.push` 之前）。例如 TagElem 分支：

   ```rust
   if content.is::<TagElem>() {
       hit!(C_TAG);
       s.sink.push((content, styles));
       return Ok(());
   }
   ```

   依次给 kind / show / seq / styled / group / filter / push 七处也加上对应的 `hit!(...)`。

4. 在 `realize()` 返回前（第 73 行 `Ok(s.sink)` 之前）打印并清零，这样**每次 `realize()` 调用**会输出一行各分支计数：

   ```rust
   eprintln!(
       "[realize] tag={} kind={} show={} seq={} styled={} group={} filter={} push={}",
       C_TAG.load(Ordering::Relaxed), C_KIND.load(Ordering::Relaxed),
       C_SHOW.load(Ordering::Relaxed), C_SEQ.load(Ordering::Relaxed),
       C_STYLED.load(Ordering::Relaxed), C_GROUP.load(Ordering::Relaxed),
       C_FILTER.load(Ordering::Relaxed), C_PUSH.load(Ordering::Relaxed),
   );
   for c in [&C_TAG, &C_KIND, &C_SHOW, &C_SEQ, &C_STYLED, &C_GROUP, &C_FILTER, &C_PUSH] {
       c.store(0, Ordering::Relaxed);
   }
   ```

5. 准备一个含**文本、加粗（样式）、列表与段落**的最小文档 `doc.typ`：

   ```typ
   Hello *world*.

   #list[
     - A
     - B
   ]
   ```

6. 运行并把 stderr 重定向到文件：

   ```bash
   cargo run -p typst-cli -- compile doc.typ 2> visit.log
   ```

**需要观察的现象**：

- `visit.log` 里会出现多行 `[realize] ...`，每行对应**一次** `realize()` 调用（排版过程中 flow/inline/pages 会多次调用 realize，这是正常的）。
- 找到对应「整页内容」的那一行（通常 `push`、`show`、`seq` 较大）。

**预期结果**（定性，**精确计数待本地验证**）：

- `seq > 0`：顶层 content 通常是 `SequenceElem`，所以序列递归一定发生。
- `show > 0` 且通常是最大的一项：几乎每个元素首次访问都要 `prepare` 或套用 show 规则。
- `group > 0`：文本被收进段落、列表项被收进 `ListElem`。
- `push > 0`：最终落地的 well-known 元素。
- `tag`：若文档里没有带 label 的元素，可能为 0；给某元素加 `<my-label>` 后应能看到 `tag > 0`。

> 提示：若输出过多难以阅读，可先把 `doc.typ` 缩到只有一行文字 `Hi`，观察最简单情形下的计数，再逐步加复杂度。

#### 4.1.5 小练习与答案

**练习 1**：如果把步骤 3（show 规则）和步骤 4（序列递归）的顺序对调，会出现什么问题？

> **参考答案**：带 label 的 `SequenceElem` 容器将来不及被 `prepare`（分配 location、生成 start/end tag）。因为 show 规则里包含了首次访问时的 `prepare` 步骤，它必须先在容器自身上运行，才能安全地拆开递归到子元素。顺序对调会破坏 tag/location 的正确生成，进而影响内省（如 `query`、`locate`）。

**练习 2**：一个普通文字 `Hi`（`TextElem`）从进入 `visit()` 到最终落地，最短路径会经过哪几步？为什么不会进入分组（步骤 6）？

> **参考答案**：最短路径是：步骤 3（show 规则：首次访问时 `prepare`，若没有匹配的 show 规则则原样返回）→ 步骤 6（分组：`TextElem` 是 `PAR`/`TEXTUAL` 分组的 Trigger，会被收集进段落分组，所以**其实会**进入步骤 6）。注意：单独一个 `TextElem` 通常会被分组「收走」而不是直接 push；只有当它处于 `Par`/`Math` kind、或分组规则表里没有对应 Trigger 时，才可能走到步骤 8 的兜底 push。这道题的陷阱正在于「分组会拦截大多数行内元素」。

**练习 3**：步骤 8（兜底 push）里的元素，和步骤 6（分组）产出的元素，有什么本质联系？

> **参考答案**：分组（步骤 6）把一批叶子元素收集后，会通过 `finish_*` 函数合成出 `ParElem`/`ListElem` 等**新的 well-known 元素**，并**再次调用 `visit()`** 让它重新走一遍流水线。这些新合成的元素不会再触发分组（它们对分组规则返回 Interrupt 而非 Trigger），也不会被过滤，于是最终落到步骤 8 被 push 进 `sink`。所以步骤 6 的产物，正是步骤 8 的重要输入来源之一。

---

### 4.2 四个 visit_*_rules 子函数的分工速览

#### 4.2.1 概念说明

`visit()` 的骨架很清晰，但「真正干活」的是它调用的四个 `visit_*_rules` 子函数。本节只看它们各自的**职责边界**与**返回值约定**，不深入内部——那是 u2 系列的主题。先记住一句话总结：

| 子函数 | 一句话职责 | 何时返回 `true`（认领） |
| --- | --- | --- |
| `visit_kind_rules` | 按 `RealizationKind` 做「上下文相关」的元素变换 | 在 math 里展开嵌套 equation / 对符号/文本套正则；在非 math 里把数学内容包成 equation、把符号转成文本 |
| `visit_show_rules` | 套用 show 规则 + 首次访问时 `prepare` | 当 `verdict()` 判定「有 show 步骤、或有样式映射、或需要 prepare」时 |
| `visit_grouping_rules` | 把连续的叶子元素收集成分组 | 当某条分组规则对当前元素返回 `Trigger`，或元素可并入当前活跃分组 |
| `visit_filter_rules` | 丢弃在某些 kind 下不该出现的元素 | 在非 `Par`/`Math` kind 下遇到落单空格、段落断点、或可折叠的 attach 间距 |

它们的共同接口约定：返回 `SourceResult<bool>`，`true` = 「我处理了，调用方短路返回」，`false` = 「与我无关，继续」。`visit()` 用完全相同的句式 `if visit_xxx(...)? { return Ok(()); }` 调用它们，因此**追加或调整一道关卡非常容易**，这也是这条流水线可维护性高的原因。

#### 4.2.2 核心流程

四个子函数在 `visit()` 中的调用顺序，以及它们各自内部「先做什么、再做什么」的高层流程：

```
visit_kind_rules(content):
  if kind == Math:
      若是 EquationElem  → 透明递归进它的 body（让 $pi$ 可被复用）
      若是 Symbol/Text   → 单元素上找正则 show 规则匹配
  else (非 math):
      若 content「是数学的」且非 EquationElem → 包成 EquationElem 再 visit
      若是 SymbolElem    → 转成 TextElem 再 visit
  否则返回 false

visit_show_rules(content):
  verdict = 判定「要不要 prepare / 套 show 规则 / 应用样式映射」
  若 verdict 为 None → 返回 false
  否则：必要时 prepare → 套用 ShowStep（用户 Recipe 或内置）→
       生成 start/end tag → 递归 visit_styled 处理产出 → 返回 true

visit_grouping_rules(content):
  matching = 找一条对 content 返回 Trigger 的规则
  while 有活跃分组:
      若 matching 优先级更高 → break（准备开嵌套分组）
      若 content 可并入活跃分组 → push 并返回 true
      否则 finish 最内层分组（含 512 次防死循环守卫）
  若有 matching → 开新分组、push、返回 true
  否则返回 false

visit_filter_rules(content):
  if kind 是 Par 或 Math → 返回 false（不过滤）
  若是 SpaceElem     → 丢弃（返回 true）
  若是 ParbreakElem  → 记录边界、不进 sink（返回 true）
  若不可 attach 且是 attach VElem → 折叠（返回 true）
  否则更新 may_attach 标志，返回 false
```

可以看到，这四个子函数其实**覆盖了 realize 的全部「语义变换」**：`visit()` 只负责排序与短路，真正「改变内容」的逻辑都在这四个函数（以及它们调用的 `verdict`/`prepare`/`finish_*` 等）里。

#### 4.2.3 源码精读

**`visit_kind_rules`**——按类型变换，**必须早于 show 规则**：

[src/lib.rs:L296-L349](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L296-L349) —— 整个函数。它分 `Math` 与非 `Math` 两个分支，处理「同一种内容在不同上下文里应被不同看待」的需求。最具代表性的是非 math 分支里把 `SymbolElem` 转成 `TextElem`：

[src/lib.rs:L338-L345](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L338-L345) —— 在非数学具现化中，`SymbolElem` 被透明地改写成 `TextElem`，这样后端 layout 就不必单独处理符号。改写后会**再次 `visit()`** 转换后的元素，并返回 `true`。

**`visit_show_rules`**——套 show 规则 + prepare：

[src/lib.rs:L351-L433](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L351-L433) —— 整个函数。先由 `verdict()` 决定如何处理；若 `verdict` 为 `None`（无需任何操作）则返回 `false`，把元素交给后续关卡：

[src/lib.rs:L358-L362](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L358-L362) —— `verdict()` 返回 `None` 时直接 `return Ok(false)`，意为「这个元素不需要 show/prepare，继续走流水线」。这正是大量普通元素能一路滑到分组或兜底 push 的原因。

**`visit_grouping_rules`**——收集与嵌套分组：

[src/lib.rs:L694-L749](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L694-L749) —— 整个函数。它先在规则表里找一条对当前元素返回 `Trigger` 的规则（`matching`），然后尝试把元素并入最内层活跃分组；并不进就 `finish` 掉再试；都没有就开新分组。返回 `false` 表示「没有任何分组规则对它感兴趣」。

**`visit_filter_rules`**——边界与丢弃：

[src/lib.rs:L751-L785](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L751-L785) —— 整个函数。注意开头：在 `Par`/`Math` kind 下直接返回 `false`（这两种 kind 里空格是顶层内容，不能丢）：

[src/lib.rs:L758-L760](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L758-L760) —— `Par`/`Math` 具现化不过滤任何元素，直接返回 `false`。

#### 4.2.4 代码实践

**实践目标**：通过**源码阅读型跟踪**，判断给定元素会命中哪个子函数，巩固「分工边界」的认知（无需编译）。

**操作步骤**：

1. 打开 [src/lib.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs)，依次跳到 `visit_kind_rules`（L296）、`visit_show_rules`（L351）、`visit_grouping_rules`（L694）、`visit_filter_rules`（L751）。

2. 对照下表，对每个「元素 + 上下文」组合，预测它在 `visit()` 里**第一个返回 `true` 的关卡**是哪一个：

   | 元素（content） | 具现化 kind | 第一个认领它的关卡是？ |
   | --- | --- | --- |
   | `SymbolElem`（如 `#sym.alpha`） | 非 math | （待你判断） |
   | `EquationElem` 嵌套在数学里 | `Math` | （待你判断） |
   | 带 `<my-label>` 的 `SequenceElem` | `Document` | （待你判断） |
   | 落单的 `SpaceElem` | `Document` | （待你判断） |
   | 落单的 `SpaceElem` | `Par` | （待你判断） |
   | `TextElem("Hi")` | `Document`（含 PAR 规则） | （待你判断） |

3. 填完后，用 4.1.4 的插桩（或直接在对应子函数入口加 `eprintln!`）验证你的判断。

**预期结果**（参考答案）：

- `SymbolElem`（非 math）→ **kind 规则**（被改写成 `TextElem` 再 `visit`）。
- 嵌套 `EquationElem`（math）→ **kind 规则**（透明展开 body）。
- 带 label 的 `SequenceElem` → **show 规则**（因为带 label 需要 `prepare` 生成 tag/location，`verdict` 不会返回 `None`）。
- 落单 `SpaceElem`（Document）→ **过滤规则**（被丢弃）。
- 落单 `SpaceElem`（Par）→ 跳过过滤（返回 false），后续被**分组规则**当作段落 `Inner` 收进 `ParElem`。
- `TextElem("Hi")`（Document）→ **分组规则**（PAR 的 Trigger，收进段落）。

> **待本地验证**：`Par` kind 下空格的确切归属，建议用插桩确认，因为这取决于 `PAR_RULES` 表是否被选用以及分组是否处于活跃状态。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `visit_filter_rules` 在 `Par`/`Math` kind 下要直接返回 `false`？

> **参考答案**：因为在 `Par` 与 `Math` 具现化中，空格是**有意义的顶层内容**（段落内部的空格、数学公式里的间距都需要保留并由后续处理）。过滤规则是为「文档/片段」级具现化准备的——那里落单的空格只是段落分组的边角料，没有进 `sink` 的必要。

**练习 2**：四个子函数里，哪一个是「唯一会改变元素类型身份」的（即把 A 类元素变成 B 类元素）？哪一个「只收集不改变」？

> **参考答案**：`visit_kind_rules`（如 Symbol→Text、Mathy→Equation）和 `visit_show_rules`（show 规则可把元素替换成任意新内容）都会改变元素身份；`visit_grouping_rules` 的**收集阶段**只把元素原样暂存进 `sink` 的一个区间，**不改变**它们——真正改变身份的是分组完成时的 `finish_*` 函数（把多个元素合成一个 `ParElem` 等），那时合成出的新元素会再次进入 `visit()`。

**练习 3**：如果一个元素让 `visit_kind_rules`、`visit_show_rules` 都返回 `false`，它又不是 `SequenceElem`/`StyledElem`，也没有分组/过滤规则认领它，它的命运是什么？

> **参考答案**：它会走到步骤 8，连同样式被原样 `push` 进 `sink`，成为输出清单里的一项。这通常意味着它本来就是一件 well-known 元素（如已经具现化完毕的 `ParElem`、`ImageElem` 等），无需再加工。

## 5. 综合实践

把本讲的「顺序、短路、分工」串起来，做一次完整的「预测 + 验证」。

**任务**：对下面这个文档，**手画** 顶层 content 树，并标注每个顶层节点在 `visit()` 中第一个认领它的关卡；然后用 4.1.4 的插桩验证。

```typ
#highlight[Hello] #list[- A - B] <mylist>
```

**操作步骤**：

1. **画树**：顶层大致是一个 `SequenceElem`，children 包括：
   - `StyledElem(highlight 样式, "Hello")`
   - `SpaceElem`
   - 带 label `<mylist>` 的 `ListElem`（其 children 是两个列表项）
2. **预测**（按下表填）：

   | 顶层节点 | 第一个认领关卡 | 理由 |
   | --- | --- | --- |
   | 顶层 `SequenceElem` | 步骤 3 show（prepare，因……？）/ 步骤 4 序列递归 | （待你判断：它带 label 吗？） |
   | `StyledElem(highlight, "Hello")` | 步骤 3 show / 步骤 5 styled | （待你判断） |
   | `SpaceElem` | 步骤 6 分组 / 步骤 7 过滤 | （待你判断，注意 kind） |
   | `ListElem` | 步骤 3 show / 步骤 8 push | （提示：已具现化的 ListElem 对分组返回 Interrupt） |

3. **验证**：用 4.1.4 的计数器运行，找到对应这次具现化的那一行 `[realize] ...`，核对你的预测。
4. **进阶**：给 `ListElem` 加上 `<mylist>` label 后，观察 `tag` 计数是否变为非零，并解释为什么（提示：label 会触发 `prepare` 分配 location 并生成 start/end tag，而这些 tag 本身会作为 `TagElem` 走步骤 1 直推）。

**预期结果**（定性，**精确值待本地验证**）：

- 顶层 `SequenceElem` 命中步骤 4（序列递归），但若它带了 label，会**先**在步骤 3 被 `prepare`（`verdict` 非 `None`）。
- `highlight` 包裹的 `Hello` 会进入 show 规则（`highlight` 有内置 show 行为），产出的新内容再次 `visit`。
- 落单空格在 `Document` kind 下被步骤 7 过滤丢弃。
- `ListElem` 已是 well-known，最终落到步骤 8 被 push。

## 6. 本讲小结

- `visit()` 是整条具现化流水线的**唯一递归入口**；`realize()` 只调它一次，所有内容都从这一个函数流过。
- 它按**固定顺序**尝试 7 条分支（TagElem 直推 → kind 规则 → show 规则 → 序列递归 → 样式递归 → 分组 → 过滤），命中即 `return`（短路），全部不命中才兜底 `push` 到 `sink`。
- 顺序有依赖：**kind 必须早于 show**（如 Symbol→Text 要先做）；**show 必须早于序列/样式递归**（带 label 的容器要先 prepare）。
- 四个 `visit_*_rules` 子函数分工明确，统一用 `SourceResult<bool>` 接口：`true` 表示「已认领，短路」，`false` 表示「与我无关，继续」。
- 「well-known elements」是流水线终点产物；分组等变换合成出的新元素（如 `ParElem`）会**再次进入 `visit()`**，最终经兜底 `push` 落进 `sink`。

## 7. 下一步学习建议

本讲建立了 `visit()` 的全局心智模型，但每个子函数的内部都是一座富矿。建议按以下顺序继续：

- **u2-l1（State 状态机与分组栈）**：`visit()` 反复读写的 `State` 到底有哪些字段？尤其 `groupings` 栈如何驱动分组——这是理解步骤 6 的前置。
- **u2-l2（show 规则的应用流程 `visit_show_rules`）**：深入步骤 3，看 `verdict`/`ShowStep`/`prepare` 如何协作。
- **u2-l6 / u2-l7（分组规则框架与生命周期）**：深入步骤 6，看 `GroupingRule`、`GroupingEffect` 与 `finish_innermost_grouping`。
- 如果你想先看「上游怎么调用 realize」，可跳到 **u3-l5（与 layout / html / bundle / math 的集成）**，了解 `Routines::realize` 的各调用点与所用 `RealizationKind`。

阅读源码时，建议始终把本讲的「8 步流水线」当作地图：每读一个子函数，就回想它对应步骤几、命中后如何短路返回。
