# 增量编译与 reparse 入口

## 1. 本讲目标

本讲是「增量重解析」单元（U9）的第一篇。学完后你应当能够：

- 说清「编辑一段 Typst 源码后，typst-syntax 为什么要尽量避免重新解析整篇文档」。
- 复述 `Source::edit` → `reparse` 这条调用链，以及它返回的「实际重解析范围」是什么含义。
- 解释 `reparse` 的核心策略：**先尝试局部增量重解析，失败就回退到全量 `parse` + `numberize`**。
- 区分「文本与行索引的更新」（`Lines::edit`）与「语法树的更新」（`reparse`）这两件互不干扰的事。
- 动手用 `Source::edit` 做一次小修改，并打印返回的字节范围，亲眼看到它远小于全文。

本讲只聚焦**入口与兜底机制**，`try_reparse` 内部如何自顶向下找到重解析窗口、`reparse_markup` / `reparse_block` 两个钩子如何工作，留给 u9-l2、u9-l3 详讲。

---

## 2. 前置知识

本讲假设你已经学完以下内容（关键结论会直接引用，不再重复推导）：

- **u1-l4 端到端体验**：`Source::new` 的三步流水线 `parse`（建裸 CST）→ `numberize`（给每个节点盖编号 Span）→ `Lines::new`（建行索引）。
- **u4-l1 Parser 架构**：`parse` / `parse_code` / `parse_math` 三个入口，以及 Parser 是「递归下降 + marker 事件式」混合。
- **u6-l1 / u6-l2 Span 与 numberize**：节点用「编号」而非「字节范围」标识；`numberize` 保证两条不变量——父节点编号小于任意子节点、兄弟节点从左到右递增；`Span::FULL = 2..2^47` 是整棵新树的编号区间。
- **u8-l1 Source 文件抽象**：`Source` 是 `Arc<LazyHash<SourceInner>>`，内部只有 `id` / `root` / `lines` 三字段，文本是「唯一真相」，存在 `Lines` 里。
- **u8-l3 文本编辑与行重建**：`Lines::edit` 用公共前后缀 diff 求最小编辑，并用 `Arc::make_mut` 写时复制、只重算受影响行的起点。

如果上面任何一条你还陌生，建议先回去看对应讲义。本讲反复用到「编号」「全量 numberize」「行表写时复制」这些概念。

**一个直觉**：Typst 是「增量编译」的文档系统——你每敲一个字，它都要重新解析、重新求值。如果每次都从头解析整篇几万字的文档，响应会明显卡顿。增量重解析的目标就是：**只重新解析被编辑波及的那一小段文本**，让其余绝大部分节点（以及它们对应的求值结果缓存）原封不动地保留下来。

---

## 3. 本讲源码地图

本讲涉及 5 个源码文件，主次如下：

| 文件 | 在本讲的角色 |
| --- | --- |
| `src/reparser.rs` | **核心**。包含本讲的两位主角：对外入口 `reparse`，以及增量算法 `try_reparse`。 |
| `src/source.rs` | **编排入口**。`Source::edit` / `Source::replace` 是用户真正调用的 API，它们内部把「改文本」与「重解析」串起来。 |
| `src/lines.rs` | **文本与行重建**。`Lines::edit` 负责更新文本和行表，与重解析解耦。`replacement_range` 给 `Source::replace` 提供最小 diff。 |
| `src/parser.rs` | **局部重解析钩子**。`reparse_markup` / `reparse_block` 复用 Parser 重新解析一小段文本（本讲只看它们的成功/失败条件）。 |
| `src/node.rs` | **编号与子节点替换**。`numberize`、`replace_children`、`update_parent` 支撑增量更新（本讲从调用方视角引用）。 |

数据流（编辑发生时）：

```text
用户调用 Source::edit(replace, with)
        │
        ├─ inner.lines.edit(...)        ← 改文本 + 重建行表（lines.rs）
        │
        └─ reparse(&mut root, text, replace, with.len())   ← 改语法树（reparser.rs）
                │
                ├─ try_reparse(...)      ← 先试增量（成功就返回小范围）
                │
                └─ 兜底：parse(text) + numberize(id, FULL)  ← 失败就全量（返回 0..text.len()）
```

---

## 4. 核心概念与源码讲解

### 4.1 增量重解析的动机：为什么编辑后不全量解析

#### 4.1.1 概念说明

「重解析（reparse）」回答的问题是：**源码文本被局部修改后，语法树要怎么跟着变？**

最朴素的做法是「全量重建」：把新文本整个丢给 `parse`，得到一棵全新的 CST，再 `numberize` 重新编号。这个做法**正确，但慢**——解析成本大致是 \( O(n) \)（n 为文档长度），且整棵树的 Span 编号全部改变。

为什么「Span 编号全部改变」是个问题？因为 Typst 的下游（求值层、增量编译）会把**计算结果按 Span 缓存**。如果一次小编辑导致所有节点的 Span 都变了，缓存就大面积失效，增量编译退化成全量编译。所以理想情况是：**编辑只改变少数节点的结构和编号，其余节点的 Span 保持不变**，从而保住缓存命中率。

「增量重解析」就是为此设计：它尽量只重新解析被编辑波及的一个**小窗口**，把窗口内重新解析出的子树「嫁接」回原树，并只对这个局部重新编号。

#### 4.1.2 核心流程

两种策略的对比：

| 维度 | 全量重建 | 增量重解析（理想） |
| --- | --- | --- |
| 重新解析的文本量 | 全文 \( O(n) \) | 编辑点附近的一个小窗口 |
| 重新 numberize 的范围 | 整棵树 | 受影响子树 |
| Span 变化范围 | 几乎全部节点变 | 仅局部节点变 |
| 下游缓存命中 | 大面积失效 | 大面积保留 |
| 实现复杂度 | 简单 | 复杂（有正确性边界，可能失败） |

正因为增量重解析「可能失败」（例如编辑破坏了定界符平衡，或局部无法保证编号不变），所以策略必然是：**先试增量，失败兜底全量**。这就是下一节 `reparse` 的形态。

#### 4.1.3 源码精读

全量重建长什么样？看 `Source::new`（首次创建文件时的正规流程），它就是「parse + numberize」两步：

[crates/typst-syntax/src/source.rs:36-41](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/source.rs#L36-L41) —— 创建一个新源文件：先 `parse(&text)` 建裸 CST，再用 `Span::FULL`（整个编号区间）对整棵树 `numberize`。

注意第 39 行的 `.unwrap()`：这里能放心 unwrap，是因为全新文本 + 整个 `Span::FULL` 区间一定够分配（参见 u6-l2）。本讲的兜底分支也会复用同样的「全量 parse + numberize」逻辑。

#### 4.1.4 代码实践

**实践目标**：建立「全量解析是有成本的」的直观印象，为后续理解「为什么要增量」铺垫。

**操作步骤**：

1. 打开 `src/source.rs`，定位 `Source::new`（上面的链接）。
2. 注意第 37 行的 `typst_timing::TimingScope::new("create source")`——它把「创建源文件」这一段纳入了 Typst 的计时系统。
3. 在本仓库根目录搜索是否启用了计时：阅读 `typst-timing` 的用法（可执行文件通常带 `--timing` 之类参数）。

**需要观察的现象**：当文档很长时，「create source」这一段在全量编译里是会出现在性能火焰图中的耗时项；增量重解析正是为了在「编辑」这一高频路径上避开它。

**预期结果**：理解 `Source::new` 等价于一次全量 parse + numberize；编辑时若每次都走这条路，开销随文档长度线性增长。

> 本实践为「源码阅读型」，不需要运行命令。计时功能的具体开关方式请在本仓库或 `typst-timing` crate 中确认。

#### 4.1.5 小练习与答案

**练习 1**：为什么 Typst 不直接用「字节范围」给节点定位，而要用「编号（Span number）」？这对增量重解析有什么好处？

> **参考答案**：字节范围在编辑后会整体漂移（在编辑点之后的所有节点范围都要前移/后移），无法稳定地作为缓存键。编号则可以在 numberize 时被刻意设计成「只在局部变化」（父编号小于子、兄弟递增，且留有编号余量），从而让未被编辑波及的节点编号保持不变，保住下游缓存命中。

**练习 2**：如果增量重解析失败、回退到全量 `numberize(id, Span::FULL)`，下游缓存会怎样？

> **参考答案**：整棵树会被重新编号，绝大多数节点的 Span 都会改变，导致下游按 Span 缓存的求值结果大面积失效，本次编译实质退化为全量编译。这正是兜底要尽量避免、而 `try_reparse` 要尽力成功的原因。

---

### 4.2 reparse 入口：增量优先、全量兜底

#### 4.2.1 概念说明

`reparse` 是 `reparser.rs` 里**对外公开的入口函数**（但 `reparser` 模块本身在 crate 内部私有，外部经由 `Source::edit` 间接调用）。它的契约非常清晰，浓缩在文档注释里：

> 接收新文本 `text`、被替换的旧范围 `replaced`、替换进去的新串长度 `replacement_len`，返回**新文本中最终被重新解析的字节范围**。

这句话有两层意思：

1. 返回的是「在新文本里的范围」，不是旧文本的范围。
2. 「最终被重新解析」——意味着这个范围可能比 `replaced` 大（增量重解析为了保证语法正确，往往会向两侧扩展），也可能是全文（兜底时）。

`reparse` 本身只是个**调度器**：它把活儿交给 `try_reparse`，若返回 `None`（增量失败），就执行兜底。

#### 4.2.2 核心流程

```text
reparse(root, text, replaced, replacement_len):
    range = try_reparse(...)                  # 先试增量
    if range.is_some():
        return range                           # 成功：返回局部小范围
    else:
        # 兜底：全量重建
        id = root.span().id()
        *root = parse(text)                    # 整篇重新解析
        if id 存在:
            root.numberize(id, Span::FULL)     # 整棵树重新编号
        return 0..text.len()                   # 返回「全文都被重解析了」
```

两个关键点：

- **兜底里重新拿 `id`**：兜底要重新 numberize，必须知道文件 `FileId`。它从 `root.span().id()` 读出来（root 节点的 span 里编码了文件号，见 u6-l1）。若 `id` 为 `None`（detached span，多见于测试中的裸树），就跳过 numberize，只重建结构。
- **兜底返回 `0..text.len()`**：这是一个明确的「我重解析了全文」的信号，调用方（及测试）据此区分「增量」与「全量」。

#### 4.2.3 源码精读

[crates/typst-syntax/src/reparser.rs:15-29](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/reparser.rs#L15-L29) —— `reparse` 函数本体：第 21 行调用 `try_reparse`，`.unwrap_or_else(|| { ... })` 接住失败的 `None`。

[crates/typst-syntax/src/reparser.rs:21-28](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/reparser.rs#L21-L28) —— 兜底闭包：`*root = parse(text)` 全量重解析；若 `root.span().id()` 有值，则 `root.numberize(id, Span::FULL).unwrap()` 全量重新编号；最后返回 `0..text.len()`。

这里的 `.unwrap()` 同样安全：全量重建一棵新树、分配整个 `Span::FULL` 区间，必然够用（u6-l2 已论证）。

注意函数签名里 `root: &mut SyntaxNode`：它**就地修改**传入的根节点。增量成功时只改局部子树，兜底时把整棵树替换掉。调用方 `Source::edit` 正是传入自己的 `inner.root`。

#### 4.2.4 代码实践

**实践目标**：从源码层面确认「`reparse` 的返回值只有两种形态」——要么是 `try_reparse` 给的局部范围，要么是兜底的 `0..text.len()`。

**操作步骤**：

1. 阅读 [crates/typst-syntax/src/reparser.rs:15-29](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/reparser.rs#L15-L29)。
2. 数一数函数里有几条 `return` / 求值路径会决定最终返回值。

**需要观察的现象**：整个 `reparse` 函数只有两个出口——`try_reparse(...)` 的 `Some(range)`，以及 `unwrap_or_else` 闭包里的 `0..text.len()`。

**预期结果**：确认 `reparse` 是一个纯调度器，真正的算法全在 `try_reparse` 与兜底里。

> 本实践为「源码阅读型」，无需运行。

#### 4.2.5 小练习与答案

**练习 1**：兜底分支里为什么要先 `let id = root.span().id();`，再 `*root = parse(text);`，而不是反过来？

> **参考答案**：因为 `*root = parse(text)` 会用一棵**全新的、span 全为 detached** 的树覆盖掉原树。如果先覆盖，原 root 里编码的文件 `id` 就丢了，无法在 numberize 时把编号关联到正确的文件。所以必须先从旧 root 读出 `id`，再覆盖，最后用这个 `id` 给新树编号。

**练习 2**：若 `root.span().id()` 返回 `None`，兜底还会 numberize 吗？为什么这种情况下不 numberize 是可接受的？

> **参考答案**：不会（`if let Some(id) = id` 不进入）。`id` 为 `None` 说明这棵树原本就是 detached（常见于测试用的裸 CST），不存在「关联到具体文件」的需求，下游缓存也不依赖它，因此跳过 numberize、只重建结构即可。

---

### 4.3 Source::edit / Source::replace：编辑与重解析的编排

#### 4.3.1 概念说明

`reparse` 是 crate 内部函数，用户真正调用的是 `Source` 上的两个方法：

- `Source::edit(replace, with)`：把 `self.text()[replace]` 这段替换成 `with`，返回实际重解析范围。
- `Source::replace(new)`：用一个全新的字符串替换全文，返回实际重解析范围。

`edit` 是「编辑」的主入口，也是本讲的实践主角。它做两件**互不干扰**的事：

1. **改文本与行表**：交给 `inner.lines.edit(...)`（lines.rs，u8-l3 已详讲）。
2. **改语法树**：交给 `reparse(&mut inner.root, ...)`。

这两步的解耦很关键：行表的重建完全只依赖纯文本（公共前后缀 diff + 重算受影响行），它不需要知道语法树长什么样；语法树的增量重解析也只依赖「新文本 + 被替换范围」，不需要知道行表怎么变。所以 `Lines::edit` 和 `reparse` 可以独立演进、独立正确。

`replace` 是 `edit` 的「智能包装」：当用户只知道「旧文本变成了新文本」、不知道具体改了哪里时，`replace` 先用 `Lines::replacement_range` 做一次公共前后缀 diff，求出**最小的等价单次编辑**，再交给 `edit`。

#### 4.3.2 核心流程

`Source::edit` 的流程：

```text
edit(replace, with):
    inner = Arc::make_mut(&mut self.0)     # 写时复制：唯一引用时原地改，否则克隆
    inner.lines.edit(replace, with)         # ① 改文本 + 重建行表（lines.rs）
    return reparse(                         # ② 增量重解析语法树（reparser.rs）
        &mut inner.root,
        inner.lines.text(),                 #    注意：传的是【新】文本
        replace,                            #    旧文本中的被替换范围
        with.len()                          #    替换进去的新串长度
    )
```

`Source::replace` 的流程：

```text
replace(new):
    (prefix, suffix) = lines.replacement_range(new)   # 公共前后缀 diff
    replace_range = prefix .. old.len() - suffix       # 旧文本里真正需要改的范围
    with = new[prefix .. new.len() - suffix]           # 新文本里对应要插入的串
    return self.edit(replace_range, with)              # 归约到 edit
```

几个要点：

- `Arc::make_mut` 是写时复制（u8-l1、u8-l3 都提到 `Source` 用 `Arc` 共享）。如果你 clone 了一份 `Source` 再编辑，只有真正被编辑的那一份会复制内部数据。
- 传给 `reparse` 的文本是 `inner.lines.text()`——**编辑之后的全文**；而 `replace` 是**旧文本中的范围**。`reparse` 内部要靠 `with.len()`（新串长度）把旧范围换算成新文本里的范围。
- `replacement_range` 若返回 `None`（新旧文本完全相同），`replace` 直接返回 `0..0`，什么也不做。

#### 4.3.3 源码精读

[crates/typst-syntax/src/source.rs:98-112](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/source.rs#L98-L112) —— `Source::edit`。第 105 行 `Arc::make_mut` 写时复制；第 108 行 `inner.lines.edit(...)` 改文本与行表；第 111 行 `reparse(&mut inner.root, inner.lines.text(), replace, with.len())` 增量重解析语法树。

[crates/typst-syntax/src/source.rs:85-96](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/source.rs#L85-L96) —— `Source::replace`。第 88 行调用 `replacement_range` 求 `(prefix, suffix)`；第 93–94 行据此换算出旧范围与新串；第 95 行归约到 `self.edit`。

[crates/typst-syntax/src/lines.rs:205-229](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lines.rs#L205-L229) —— `Lines::edit`（u8-l3 已详讲）。它只改文本和行表，与语法树无关。这正是 `edit` 能把「改文本」与「重解析」解耦的根基。

[crates/typst-syntax/src/lines.rs:172-197](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/lines.rs#L172-L197) —— `replacement_range`：用公共前后缀求最小编辑，并用 `is_char_boundary` 做字符边界对齐，避免在 UTF-8 字符中间切开。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：用 `Source::edit` 做一次小修改，打印返回的「实际重解析范围」，亲眼看到它远小于全文长度。

**操作步骤**：

1. 在本仓库内新建一个示例文件 `crates/typst-syntax/examples/reparse_demo.rs`（这是调用本 crate 公共 API 最可靠的方式，因为 `typst-syntax` 依赖大量 workspace 内部 crate，外部独立项目难以直接引用）。
2. 写入下面的**示例代码**：

```rust
// 示例代码：演示 Source::edit 返回的「实际重解析范围」
use typst_syntax::Source;

fn main() {
    // 一段含多个 ~ 标记的正文（~ 在 Typst 中会产生独立的文本 token，
    // 便于观察增量重解析的边界）
    let mut src = Source::detached("abc~def~gh~");
    println!("编辑前全文: {:?}", src.text());
    println!("编辑前全文长度: {}", src.text().len());

    // 把第 5 个字节处的 'e' 替换成 '+'（范围 5..6 = 单字符 'e'）
    let reparsed = src.edit(5..6, "+");
    let text = src.text();

    println!("编辑后全文: {:?}", text);
    println!("实际重解析范围: {:?}", reparsed);
    println!("实际重解析的文本片段: {:?}", &text[reparsed.clone()]);
    println!("全文长度 = {}, 重解析长度 = {}", text.len(), reparsed.len());
}
```

3. 运行：`cargo run -p typst-syntax --example reparse_demo`。

**需要观察的现象**：返回的 `reparsed` 范围对应的文本片段，应当只覆盖编辑点附近的一小段，而不是整篇 `"abc~d+f~gh~"`。

**预期结果**（与 `reparser.rs` 测试 `test_reparse_markup` 中 `test("abc~def~gh~", Edit::Range(5..6), "+", Incr("abc~d+f~"))` 一致，可在本地运行确认）：

```text
编辑前全文: "abc~def~gh~"
编辑前全文长度: 11
编辑后全文: "abc~d+f~gh~"
实际重解析范围: 0..8
实际重解析的文本片段: "abc~d+f~"
全文长度 = 11, 重解析长度 = 8
```

注意：重解析范围 `0..8`（覆盖 `"abc~d+f~"`）虽然比编辑点 `5..6` 大，但仍小于全文 11 字节——这就是「局部重解析」。范围比编辑点大的原因是 `try_reparse` 为了保证语法正确，会向两侧扩展重解析窗口（u9-l2 详讲）。

> 若不想新增文件，也可直接运行本 crate 自带测试观察等价行为：
> `cargo test -p typst-syntax test_reparse_markup`
> 该测试通过 `Edit` 枚举与 `Reparse::Incr/All` 断言来验证同样的范围结果（见 `reparser.rs` 末尾的 `#[cfg(test)] mod tests`）。

#### 4.3.5 小练习与答案

**练习 1**：`Source::edit` 第 111 行传给 `reparse` 的是 `inner.lines.text()`，而不是编辑前的旧文本。为什么必须传「新文本」？

> **参考答案**：`reparse` 的职责是把**当前**语法树更新到与新文本一致。它内部要把「旧范围 `replace`」换算成「新文本里的范围」、并可能调用 `reparse_markup`/`reparse_block` 重新解析新文本的一段，这些都只能基于新文本完成。旧文本在 `Lines::edit` 之后已经被覆盖，也不再需要。

**练习 2**：用 `Source::replace("abc~def~gh~")` 替换一个内容完全相同的 `Source`，会发生什么？返回值是什么？

> **参考答案**：`replacement_range` 发现新旧文本相同，返回 `None`；`replace` 在第 88 行的 `let Some((prefix, suffix)) = ... else { return 0..0; }` 提前返回 `0..0`，不触发任何重解析。

---

### 4.4 try_reparse 与全量回退的协作（失败兜底）

#### 4.4.1 概念说明

`try_reparse` 是增量重解析的**真正算法**（u9-l2 会深入）。本讲只需理解它如何与兜底协作：

- `try_reparse` 返回 `Option<Range<usize>>`：`Some(range)` 表示增量成功、返回重解析范围；`None` 表示**当前子树增量失败，请向外层 / 向兜底求助**。
- `reparse` 用 `.unwrap_or_else(...)` 接住这个 `None`，执行全量兜底。

`try_reparse` 失败的典型情形有：

1. **定界符不平衡**：例如编辑删掉了一个 `]` 或 `}`，局部重解析出来的子树定界符不平衡，钩子 `reparse_markup` / `reparse_block` 会返回 `None`。
2. **span 编号耗尽**：增量更新时，受影响子树可用编号区间过窄，`replace_children` 重新 numberize 失败（返回 `Err(Unnumberable)`），见 node.rs 的 `replace_children`。
3. **结构不满足重解析条件**：例如编辑点不在「顶层 markup」或「markup 块直接子节点」中（如位于列表项、标题内部），当前实现选择不重解析（见 `try_reparse` 文档注释说明）。

无论哪种失败，`reparse` 的兜底都保证**最终结果是正确的**——最坏情况就是全量重建。

#### 4.4.2 核心流程

```text
reparse:
  ├─ try_reparse(自顶向下找最内层包住编辑范围的节点)
  │     ├─ 单个 block 子节点完全包住编辑 → 在其内部递归 try_reparse，
  │     │     或调用 reparse_block 重解析整个块
  │     ├─ 顶层/markup 块的一组 markup 表达式 → expand_and_reparse_markup
  │     │     （指数级向外扩展窗口，直到成功或耗尽）
  │     └─ 都不行 → 返回 None（逐层向上传播）
  │
  └─ None → 兜底：parse + numberize(FULL)，返回 0..text.len()
```

成功条件由两个钩子的返回值决定（本讲看条件，u9-l3 看实现）：

- `reparse_markup` 成功 ⟺ `p.balanced && p.current_start() == range.end`（定界符平衡，且恰好解析到窗口末尾）。
- `reparse_block` 成功 ⟺ `p.balanced && p.prev_end() == range.end`（同上）。

#### 4.4.3 源码精读

[crates/typst-syntax/src/reparser.rs:31-54](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/reparser.rs#L31-L54) —— `try_reparse` 的文档注释，用自然语言描述了整个策略：深度优先找最内层完全包住编辑范围的节点，对 block 或 markup 表达式序列重解析，要求定界符平衡且嵌套层级不变；否则向外扩展或向上返回，直到成功或解析全文。注释还诚实说明了**当前的取舍**：不重解析列表项/标题内部的 markup，也不重解析 math（这些情况下会回退或向外扩展）。

[crates/typst-syntax/src/reparser.rs:55-126](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/reparser.rs#L55-L126) —— `try_reparse` 函数体。本讲只看它如何「失败即返回 `None`」：

- 第 63 行 `overlapping_children(...)?`：找不到完全覆盖编辑范围的子节点就立即 `None`。
- 第 81–95 行：先递归下钻到单个子节点内部尝试，成功就用 [crates/typst-syntax/src/node.rs:594-604](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L594-L604) 的 `update_parent` 更新父节点缓存（len/descendants）并返回。
- 第 99–108 行：若是 block 且 `reparse_block` 成功，用 [crates/typst-syntax/src/node.rs:581-591](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/node.rs#L581-L591) 的 `replace_children` 嫁接新子树；`replace_children` 返回 `NumberingResult`，若编号失败则 `.is_ok().then_some(...)` 退化为 `None`（触发兜底）。
- 第 111–125 行：若是顶层或 markup 块直接子节点，调用 `expand_and_reparse_markup`；否则 `None`。

[crates/typst-syntax/src/parser.rs:64-82](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L64-L82) —— `reparse_markup` 钩子。第 81 行 `(p.balanced && p.current_start() == range.end).then(|| p.finish())` 是成功条件：定界符平衡且刚好解析到窗口末尾才返回 `Some`，否则 `None`。

[crates/typst-syntax/src/parser.rs:749-755](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/parser.rs#L749-L755) —— `reparse_block` 钩子，第 753 行的成功条件同理。

把这条链串起来：钩子返回 `None` → `try_reparse` 向外/向上传播 `None` → `reparse` 兜底全量重建。**任何一环失败都会被兜底接住，正确性永远有保证**。

#### 4.4.4 代码实践

**实践目标**：制造一个「增量重解析必然失败、触发兜底全量」的场景，观察返回范围变成 `0..text.len()`。

**操作步骤**：

1. 仍用上面的 `examples/reparse_demo.rs`（或新建一个），把 `main` 改成下面的**示例代码**：

```rust
// 示例代码：制造一次「几乎全改」的编辑，触发全量兜底
use typst_syntax::Source;

fn main() {
    let mut src = Source::detached("some content");
    // 把整段 "some content" 替换成 "do it"——改动太大，增量难以成立
    let text_old = "some content";
    let start = src.text().find(text_old).unwrap();
    let reparsed = src.edit(start..start + text_old.len(), "do it");

    let text = src.text();
    println!("编辑后全文: {:?}", text);
    println!("实际重解析范围: {:?}", reparsed);
    println!("是否等于全文? {}", reparsed == (0..text.len()));
}
```

2. 运行：`cargo run -p typst-syntax --example reparse_demo`。

**需要观察的现象**：返回的 `reparsed` 应当等于 `0..text.len()`，即「整篇都被重解析了」——这正是兜底的信号。

**预期结果**（与 `test_reparse_basic` 中 `test("some content", Edit::Match("some content"), "do it", All)` 一致，可在本地运行确认）：

```text
编辑后全文: "do it"
实际重解析范围: 0..5
是否等于全文? true
```

**对比两次实践**：4.3.4 的单字符编辑只重解析了 8/11 字节；这里的大改动重解析了全文。两者的差异正是「增量优先、全量兜底」策略的直观体现。

> 想看更多触发兜底的边界用例（如未闭合字符串 `#\"a\nb\nc` 补 `\"`），可阅读 [crates/typst-syntax/src/reparser.rs:458-475](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/reparser.rs#L458-L475) 的 `test_reparse_unclosed_embedded` 测试。

#### 4.4.5 小练习与答案

**练习 1**：`replace_children` 在 `reparser.rs:104-107` 里写成 `.is_ok().then_some(new_range)`。这说明什么？

> **参考答案**：`replace_children` 返回 `NumberingResult`（即 `Result<(), Unnumberable>`）。即便局部重新解析的语法结构是对的，仍可能因为**可用 span 编号区间过窄**而重新 numberize 失败（`Err`）。此时 `.is_ok()` 为假，`then_some` 返回 `None`，让 `try_reparse` 继续向外扩展或最终由 `reparse` 兜底全量重建。这是一种「结构正确但编号失败也要回退」的保守保证。

**练习 2**：为什么 `reparse_markup` / `reparse_block` 都要求 `p.balanced`（定界符平衡）？

> **参考答案**：增量重解析只重新解析一个局部窗口，窗口外的定界符是「原样保留」的。如果局部重解析出的定界符不平衡，把它嫁接回去就会破坏整棵树的定界符配对，导致 CST 结构错误。要求 `balanced` 是为了保证「局部重解析 + 嫁接」后整棵树仍然正确。一旦不平衡，钩子返回 `None`，由 `try_reparse` 向外扩大窗口或最终兜底。

---

## 5. 综合实践

把本讲的三件事（编辑入口、增量优先、全量兜底）串起来：

**任务**：写一个示例程序，对同一段文本 `#let x = 1 + 2` 做**三次**性质不同的编辑，分别打印每次返回的「实际重解析范围」与「是否等于全文」，并解释每次结果。

**操作步骤**：

1. 在 `crates/typst-syntax/examples/reparse_compare.rs` 写入下面的**示例代码**：

```rust
// 示例代码：对比三种编辑的重解析范围
use typst_syntax::Source;

fn show(label: &str, src: &Source, reparsed: std::ops::Range<usize>) {
    let full = 0..src.text().len();
    println!("{label}");
    println!("  全文: {:?}", src.text());
    println!("  重解析范围: {:?}", reparsed);
    println!("  是否全量: {}", reparsed == full);
}

fn main() {
    // 编辑一：小改动（改一个数字），预期增量成功
    let mut a = Source::detached("#let x = 1 + 2");
    let r1 = a.edit(a.text().find('1').unwrap()..a.text().find('1').unwrap() + 1, "3");
    show("编辑一：把 1 改成 3（小改动）", &a, r1);

    // 编辑二：在代码块内部小改动，预期增量成功（块内重解析）
    let mut b = Source::detached("#{1 + 2}");
    let pos = b.text().find('1').unwrap();
    let r2 = b.edit(pos..pos + 1, "9");
    show("编辑二：在代码块内改 1→9", &b, r2);

    // 编辑三：几乎全改，预期全量兜底
    let mut c = Source::detached("#let x = 1 + 2");
    let r3 = c.edit(0..c.text().len(), "#let y = 999");
    show("编辑三：整段替换（大改动）", &c, r3);
}
```

2. 运行：`cargo run -p typst-syntax --example reparse_compare`。

**需要观察与解释的现象**：

- 编辑一：重解析范围应**小于全文**（增量成功），但比单字符编辑点大——因为 `try_reparse` 会扩展窗口。
- 编辑二：代码块 `#{ }` 是 block，编辑点在块内，`try_reparse` 会下钻到块内并用 `reparse_block` 重解析块内容，范围仍小于全文。
- 编辑三：返回 `0..text.len()`（全量兜底）。

**预期结果**：待本地验证（具体范围数值取决于 `try_reparse` 的窗口扩展策略，u9-l2 会讲清）。但「编辑三一定全量、编辑一/二一定小于全文」的定性结论是确定的。

**反思题**：用一句话总结，`Source::edit` 的返回范围从「很小」到「等于全文」分别对应了 `reparse` 内部的哪条路径？

> **参考答案**：「很小」= `try_reparse` 增量成功，返回局部范围；「等于全文」= 增量失败、`reparse` 兜底全量重建并返回 `0..text.len()`。

---

## 6. 本讲小结

- `Source::edit` 是编辑的主入口，它把「改文本+行表」（`Lines::edit`）与「改语法树」（`reparse`）两件事**解耦**：前者只依赖纯文本，后者只依赖「新文本+被替换范围+新串长度」。
- `Source::replace` 是 `edit` 的智能包装：用 `replacement_range` 做公共前后缀 diff，求出最小等价单次编辑后再交给 `edit`；新旧相同则直接返回 `0..0`。
- `reparse` 是一个**调度器**：先 `try_reparse` 试增量，成功返回局部小范围；失败用 `unwrap_or_else` 兜底——`parse(text)` 全量重建 + `numberize(id, Span::FULL)` 全量重新编号，返回 `0..text.len()`。
- 兜底必须**先**从旧 root 读出 `id`，再覆盖整棵树，最后用该 `id` 给新树 numberize；`id` 为 `None`（detached）时跳过 numberize。
- 增量重解析的价值：只重新解析编辑点附近的小窗口、只对局部重新编号，让其余节点 Span 保持不变，从而保住下游（求值/增量编译）的缓存命中，避免每次按键都 \( O(n) \) 全量解析。
- 正确性永远有保证：任何局部失败（定界符不平衡、span 编号耗尽、不满足重解析条件）都会沿 `try_reparse → reparse` 链路被全量兜底接住。

---

## 7. 下一步学习建议

本讲只看了「入口与兜底」这一层。要真正理解增量重解析，建议继续：

- **u9-l2 try_reparse 核心算法**：深入 `overlapping_children` 如何定位受影响子节点、`expand_and_reparse_markup` 如何指数级向外扩展窗口、`update_parent` / `replace_children` 如何做差量更新。建议带着本讲 4.3.4 的实践结果（重解析范围 `0..8`）去读，理解窗口是如何被扩展出来的。
- **u9-l3 markup / block 重解析钩子**：细读 `reparse_markup` / `reparse_block` 如何复用 Parser、`at_start` / `nesting` / `top_level` 参数的含义，以及为何要求定界符平衡。
- 配套阅读：回到 [crates/typst-syntax/src/reparser.rs:295-476](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-syntax/src/reparser.rs#L295-L476) 的测试模块，里面有大量「增量 vs 全量」的对照用例，是验证你理解的最佳材料。
