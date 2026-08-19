# compute_hunks：imara-diff 的使用方式

> 单元三第 1 讲。从本讲开始,我们终于要回答一个最根本的问题:上一单元里反复出现的 `DiffHunk`,到底是怎么算出来的?

## 1. 本讲目标

学完本讲,你应该能够:

1. 说出 `compute_hunks` 函数的输入、输出,以及它在 `update_diff` 后台任务中的位置。
2. 独立使用 imara-diff 的三步 API:`InternedInput::new` 装载文本 → `Diff::compute(Algorithm::Histogram, ...)` 计算 → `hunks()` 取结果。
3. 解释 `postprocess_lines`(git 的 slider/indent 启发式)为什么不是「美化」,而是多基准 diff(HEAD vs index)正确性的前提。
4. 准确描述两个不走 diff 算法的特例:空 buffer(`"\n"`)与 diff base 缺失时的输出形态。

## 2. 前置知识

### 2.1 行级差异(diff)的通用模型

上一单元我们学习了 hunk 的**数据结构**(`DiffHunk`、锚点区间、base 字节区间);本讲讲它们的**生产过程**。行级差异算法的输入是两个行序列:

\[ A = (a_1, a_2, \ldots, a_m), \quad B = (b_1, b_2, \ldots, b_n) \]

算法寻找两者的最长公共子序列(LCS),把 A 变成 B 的最小编辑代价为:

\[ \text{cost} = \underbrace{(m - |LCS|)}_{\text{删除的行数}} + \underbrace{(n - |LCS|)}_{\text{新增的行数}} \]

输出是一组「变更对」:每对包含 A 侧的一个行号区间(被删除/替换的旧文本)和 B 侧的一个行号区间(新插入的文本)。这就是 `DiffHunk` 里 `diff_base_byte_range`(A 侧)与 `buffer_range`(B 侧)的来源。

### 2.2 什么是最小差异的「歧义」

最小编辑脚本**不唯一**。当文本里有重复内容时,同一份代价最小的 diff 可以有多种「摆放方式」(placement)。最简单的例子:

```text
A = [ "aaa\n", "",   "bbb\n", "ccc\n", "",   "ddd\n" ]   (base)
B = [ "aaa\n", "",   "ddd\n" ]                          (buffer)
```

要把 A 变成 B 需要删掉三行:`"bbb\n" "ccc\n"` 必须删,另外还要删一个空行。但删**第一个**空行(紧跟 aaa 的那个)和删**第二个**空行(紧跟 ccc 的那个),代价完全相同、最终文本完全相同。这两种摆放方式在语义上等价,却在**行号上不同**——删除区间可以锚定在 `3..6`(bbb 起)或 `2..5`(第一个空行起)。

对「只看一份 diff」的场景,选哪种都无所谓;但对 Zed 这种**同时维护多份 diff 并在它们之间对齐 hunk** 的系统(下一单元的 secondary diff),两种摆放方式不一致就是正确性 bug。这是本讲最重要的一个认知,4.3 节展开。

### 2.3 imara-diff 是什么

[imara-diff](https://docs.rs/imara-diff) 是一个 Rust 差异计算库。Zed 在 workspace 根的 [Cargo.toml:L660](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/Cargo.toml#L660) 声明 `imara-diff = "0.2.0"`,buffer_diff 通过 [Cargo.toml:L19](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/Cargo.toml#L19) 引用。本 crate 只用到它的四个名字:

[src/buffer_diff.rs:L2](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L2) — 引入 `Algorithm`、`Diff`、`InternedInput` 和 `sources::lines` 四个条目,这也是 crate 对 imara-diff 的全部依赖面。

一个小历史:Zed 最初用 libgit2(git2 crate)算 diff,后来迁移到 imara-diff(可自行运行 `git log --follow -- src/buffer_diff.rs` 观察);而 `postprocess_lines` 则是 imara-diff 0.2.0 才加入的能力,升级动机见 4.3 节。

### 2.4 行尾归一化:进入算法之前的第一步

diff 对行尾符极其敏感——CRLF 与 LF 的差异会让每一行都「不相等」,产生整文件伪差异。所以 base 文本在进入 `compute_hunks` 之前一定先被归一化为 LF:

- 异步路径在 [src/buffer_diff.rs:L1940](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1940) 调用 `text::LineEnding::normalize_arc`;
- 测试同步路径 [new_with_base_text](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1652-L1659) 里也有一份 `text::LineEnding::normalize`。

## 3. 本讲源码地图

本讲全部源码集中在 `crates/buffer_diff/src/buffer_diff.rs` 这一个文件(整个 crate 的实现都在这里):

| 区段 | 作用 |
| --- | --- |
| [L1184-L1242](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1184-L1242) | `compute_hunks` 本体,本讲主角 |
| [L1933-L1990](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1933-L1990) | `update_diff`,唯一调用方,后台线程入口 |
| [L1243-L1277](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1243-L1277) | `HunkSink` 的构造与行偏移表(下一讲 u3-l2 的主角,本讲只当边界) |
| [L1280-L1295](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1280-L1295) | `HunkSink::process_change`,行号 → 区间的换算入口 |
| [L2385-L2423](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L2385-L2423) | `assert_hunks`,四元组断言工具 |
| [L2442-L2496](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L2442-L2496) | `test_buffer_diff_simple`,实践模板 |
| [L3866-L3870](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L3866-L3870) | `test_changed_ranges` 里**直接调用** `compute_hunks` 的样例 |

## 4. 核心概念与源码讲解

### 4.1 compute_hunks:diff 计算的唯一入口

#### 4.1.1 概念说明

`compute_hunks` 是一个**私有自由函数**(不属于任何 `impl` 块),是整个 crate 唯一真正执行 diff 计算的地方。它的职责非常纯粹:

- 输入:一份可选的基准文本 `Option<(Arc<str>, Rope)>`(字符串用于按行切分,Rope 用于后续换算 Point)、buffer 的不可变快照、可选的 diff 选项(词级 diff 用,本讲末尾提一句)。
- 输出一棵 `SumTree<InternalDiffHunk>`——就是 u2-l2 讲过的那棵 hunk 区间树。

「私有」意味着外部世界只能通过 `BufferDiff::update_diff` 间接使用它;但**测试模块不受此限**——`mod tests` 通过 `use super::*` 可以直接调用它,这让本讲的实践非常方便。

#### 4.1.2 核心流程

用伪代码描述整体骨架:

```text
fn compute_hunks(base, buffer, diff_options) -> SumTree:
    tree = 空树
    match base:
    case Some((base_text, base_rope)):
        if buffer 文本 == "\n" 且 base 非平凡且以 "\n" 结尾:   # 特例一(4.4 节)
            手工 push 一个「整文件删除」hunk
            return tree
        input = InternedInput::new(lines(base_text), lines(buffer 文本))  # 4.2 节
        diff  = Diff::compute(Algorithm::Histogram, &input)
        diff.postprocess_lines(&input)                              # 4.3 节
        sink = HunkSink::new(base_text, base_rope, buffer, ...)      # u3-l2 讲
        for hunk in diff.hunks():
            sink.process_change(hunk.before, hunk.after)             # 行号 → 区间
        for internal_hunk in sink.finish():
            tree.push(internal_hunk, buffer)
    case None:                                                      # 特例二(4.4 节)
        手工 push 一个「整文件新增」hunk
    return tree
```

它在整个调用链中的位置(u3-l4 会完整讲生命周期,这里只看位置):

```text
recalculate_diff_sync / set_base_text 等入口
  └─ update_diff(...)                          # 组装参数、判断是否可复用
       └─ cx.background_executor().spawn(...)  # 丢到后台线程,不卡 UI
            └─ compute_hunks(...)              # 本讲
                 └─ imara-diff 算法 + HunkSink
       └─ 返回 Task<BufferDiffUpdate>
  └─ set_snapshot(update, ...)                 # 回到前台,写回实体并广播事件(u3-l5)
```

#### 4.1.3 源码精读

先看签名与整体分支:

[src/buffer_diff.rs:L1184-L1191](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1184-L1191) — `compute_hunks` 接受可选 base、buffer 快照与 diff 选项,返回 `SumTree<InternalDiffHunk>`;`if let Some(...)` 把主路径与「无 base」路径分开。

再看唯一调用方 `update_diff` 的三个分支:

[src/buffer_diff.rs:L1959-L1968](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1959-L1968) — **unchanged 快路径**:只有当「base 是否存在的旗标、base 版本、buffer 版本」三者都与上次一致时,直接克隆旧的 hunk 树,完全不进入 `compute_hunks`。这是最重要的性能 shortcut。

[src/buffer_diff.rs:L1970-L1981](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1970-L1981) — 在后台 executor 上执行:有 base 走 `compute_hunks(Some(...))`(同时传入 base 的 Rope 快照),无 base 走 `compute_hunks(None, ...)`,结果装进 `BufferDiffUpdate` 返回。

[src/buffer_diff.rs:L1940-L1944](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1940-L1944) — 进入前先把 base 文本 LF 归一化,并用 `debug_assert` 保证传入的快照与文本一致(只在校验构建中生效)。

测试中直接调用 `compute_hunks` 的现成样例:

[src/buffer_diff.rs:L3866-L3870](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L3866-L3870) — `test_changed_ranges` 把 base 文本和它的 `Rope` 一起包成元组传入,第三个参数 `None` 表示不算词级差异。本讲实践直接抄这个形状。

#### 4.1.4 代码实践:直接调用 compute_hunks

**实践目标**:绕过实体和异步,把 `compute_hunks` 当纯函数调用,建立「输入文本 → 输出 hunk 树」的手感。

**操作步骤**:

1. 在仓库根目录运行现有测试确认环境正常:`cargo test -p buffer_diff test_buffer_diff_simple`。
2. 打开 `crates/buffer_diff/src/buffer_diff.rs`,滚动到文件底部的 `mod tests`,仿照 [L3866-L3870](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L3866-L3870) 新增一个测试(示例代码,非项目原有):

```rust
#[gpui::test]
async fn test_compute_hunks_direct(cx: &mut gpui::TestAppContext) {
    let buffer = Buffer::new(
        ReplicaId::LOCAL,
        BufferId::new(1).unwrap(),
        "one\nTWO\nthree\n".to_string(),
    );
    let base = "one\ntwo\nthree\n";
    let tree = compute_hunks(
        Some((Arc::from(base), Rope::from(base))),
        buffer.snapshot(),
        None,
    );
    // 测试模块与实现同文件,可以访问 SumTree 的摘要(见 u2-l2):
    // 1 行被替换 → added_rows = 1, removed_rows = 1
    let summary = tree.summary();
    assert_eq!(summary.added_rows, 1);
    assert_eq!(summary.removed_rows, 1);
}
```

3. 运行:`cargo test -p buffer_diff test_compute_hunks_direct`。

**需要观察的现象**:测试通过说明行数统计与预期一致。

**预期结果**:`two → TWO` 是一次单行修改,`DiffHunkSummary` 的 `added_rows`/`removed_rows` 均为 1(字段定义见 [L176-L181](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L176-L181))。若把 buffer 改成两行不同,则两个计数变为 2。本例未在撰写环境中运行,断言细节待本地验证。

#### 4.1.5 小练习与答案

**练习 1**:`compute_hunks` 为什么设计成自由函数而不是 `BufferDiff` 的方法?

**答案**:它的全部输入都通过参数传入(base 文本、buffer 快照、选项),不读写 `BufferDiff` 的任何字段;这意味着它是无状态的纯计算,可以在后台线程上安全执行(不需要 gpui 的 `App` 上下文),也便于像 [L3866](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L3866) 那样在测试里直接调用。

**练习 2**:`update_diff` 里的 unchanged 快路径省掉的是整个 diff 计算,它靠什么判断「可以复用」?

**答案**:比较三个版本信号([L1959-L1968](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1959-L1968)):上次快照的 `base_text_exists` 旗标、base buffer 的版本号、主 buffer 的版本号。三者都一致时旧 hunk 树仍然有效,直接克隆返回。

### 4.2 imara-diff 三件套:lines、InternedInput 与 Diff::compute

#### 4.2.1 概念说明

`compute_hunks` 中真正与 imara-diff 打交道的只有三行代码,但每一行都有讲究:

1. **`lines(text)`** — 把 `&str` 切成行的迭代器,**每行包含自己的行尾符**(`"two\n"` 是一个 token,而不是 `"two"`)。这个性质至关重要:所有 token 按顺序拼接后能无损还原原文,这正是下一讲 `compute_line_offsets` 能用「逐行累加 `line.len()`」来建立行号→字节偏移表的前提。顺带一提,git diff 视角下内容相同但行尾符不同的两行会被视为不同行——因为 token 里带着行尾符。
2. **`InternedInput::new(before, after)`** — 把两侧的行收集并「驻留」(intern):相同的行共享一个整数编号,算法随后在整数序列上运行,比直接比较字符串快得多。第一个参数是 before/旧侧(diff base),第二个是 after/新侧(buffer)。
3. **`Diff::compute(Algorithm::Histogram, &input)`** — 用 Histogram 算法计算差异,得到 `Diff` 对象;随后 `diff.hunks()` 迭代出 `Hunk { before: Range<u32>, after: Range<u32> }`——`before` 是 **base 侧的行号区间**,`after` 是 **buffer 侧的行号区间**。哪个在前哪个在后,由 `InternedInput::new` 的参数顺序决定。

#### 4.2.2 核心流程

```text
"one\ntwo\nthree\n"            "one\nTWO\nthree\n"
        │ lines()                       │ lines()
        ▼                               ▼
["one\n","two\n","three\n"]    ["one\n","TWO\n","three\n"]
        │ InternedInput::new(before, after)
        ▼
行号序列 [0, 1, 2] vs [0, 3, 2]      (相同行共享编号,"TWO\n" 是新编号)
        │ Diff::compute(Histogram, &input)
        ▼
hunks() = [ Hunk { before: 1..2, after: 1..2 } ]   (第 1 行被替换)
        │ HunkSink::process_change(before, after)
        ▼
diff_base_byte_range = 4..8, buffer_row_range = 1..2, …(u3-l2 的内容)
```

关于 `Algorithm::Histogram`:它是与 `git diff --histogram` 同族的算法,相比经典 Myers 在重复内容多的文本上不易产生病态结果。Zed 没有提供算法配置,hard-code 选择了 Histogram。

#### 4.2.3 源码精读

[src/buffer_diff.rs:L1212-L1213](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1212-L1213) — 用 `lines()` 切分两侧文本构造 `InternedInput`(base 在前、buffer 在后),再用 Histogram 算法算出 `Diff`。注意 `diff` 被声明为 `mut`,因为下一步的 `postprocess_lines` 需要 `&mut self`。

[src/buffer_diff.rs:L1221-L1227](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1221-L1227) — 遍历 `diff.hunks()` 把每个 hunk 的 `before`/`after` 行号区间喂给 `HunkSink::process_change`,最后 `finish()` 取出攒好的 `InternalDiffHunk` 逐个 push 进 SumTree。

[src/buffer_diff.rs:L1280-L1292](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1280-L1292) — `process_change` 的参数语义在这里得到印证:`before` 的行号用来索引 `old_line_offsets`(base 的字节偏移表),`after` 的行号直接当作 buffer 的行区间并转成锚点。**imara-diff 输出的只是行号;一切「行号 → 字节区间 / 锚点 / Point」的换算都发生在本 crate 的 HunkSink 里**——这正是 before/after 命名与 buffer_diff 内部命名的对应关系。

#### 4.2.4 代码实践:裸用 imara-diff,看清算法的原始输出

**实践目标**:不经 `compute_hunks`,直接用 imara-diff 原生 API 打印 hunk 的行号区间,体会「算法只给行号」这一边界。

**操作步骤**:

1. 在 `mod tests` 里新增(示例代码,非项目原有;imara-diff 是 crate 依赖,测试模块可用):

```rust
#[test]
fn test_imara_diff_raw() {
    use imara_diff::{Algorithm, Diff, InternedInput, sources::lines};

    let base = "aaa\n\nbbb\nccc\n\nddd\n";
    let buffer = "aaa\n\nddd\n";
    let input = InternedInput::new(lines(base), lines(buffer));
    let mut diff = Diff::compute(Algorithm::Histogram, &input);
    diff.postprocess_lines(&input);
    for hunk in diff.hunks() {
        println!("before {:?} after {:?}", hunk.before, hunk.after);
    }
}
```

2. 运行并查看输出:`cargo test -p buffer_diff test_imara_diff_raw -- --nocapture`。
3. 把第 2.2 节的文本抄进去:预期只有**一个** hunk,`before` 覆盖三个 base 行(两行内容 + 一个空行),`after` 为空区间(纯删除)。
4. 再删掉 `diff.postprocess_lines(&input);` 这一行,重复运行,记录 `before` 的起止是否变化。

**需要观察的现象**:hunk 数量与 `before`/`after` 的行号;有无 `postprocess_lines` 两次运行的输出差异。

**预期结果**:两行内容 `"bbb\n" "ccc\n"` 一定在 `before` 区间内,外加某一个空行;`after` 为空(纯删除场景)。至于具体锚定在哪个空行、以及注释 `postprocess_lines` 后位置是否漂移,取决于算法内部的摆放选择——**请如实记录你观察到的值,待本地验证**,并与 4.3 节的实践相互印证。

#### 4.2.5 小练习与答案

**练习 1**:为什么 `lines()` 必须让每个 token 包含行尾符?如果不含,`compute_hunks` 的哪个下游环节会坏?

**答案**:下一讲的 `compute_line_offsets`([L1268-L1276](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1268-L1276))逐行累加 `line.len()` 生成字节偏移表;若 token 不含行尾符,累加和会小于真实偏移,`process_change` 算出的 `diff_base_byte_range` 将整体前移、错位。附带影响:行尾不同的行也会被误判为相同。

**练习 2**:`Diff::compute` 返回的 hunk 里,`before` 与 `after` 分别属于哪份文本?

**答案**:`before` 属于 `InternedInput::new` 的第一个参数(diff base,旧侧),`after` 属于第二个参数(buffer,新侧)。在 buffer_diff 中对应 `diff_base_byte_range` 与 buffer 行区间的换算起点。

### 4.3 postprocess_lines:规范化歧义 hunk 的锚定位置

#### 4.3.1 概念说明

2.2 节说过:存在重复内容时,代价最小的 diff 有多种摆放方式,算法库**任选其一**。`Diff::postprocess_lines(&input)` 是 imara-diff 对 git xdiff 中 slider/indent 启发式的移植,作用是**把歧义 hunk 的锚定位置规范化成唯一确定的那个**——只依据 hunk 自身的局部内容做决定,与输入的其他部分无关。

为什么这对 Zed 是正确性问题而非美观问题?源码注释写得非常清楚,这段注释值得逐句精读:

[src/buffer_diff.rs:L1214-L1220](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1214-L1220) — 注释说明:若不规范化,**同一份 buffer 对不同基准文本**(例如 HEAD 与 index)算出的 diff,可能把同一个逻辑改动锚定在不同的行;而所有「在这两份 diff 之间对齐 hunk」的代码都假设位置一致,否则 hunk 会被错误地渲染成已 staged,甚至 stage/unstage 操作会**损坏 git index**。随后的 `diff.postprocess_lines(&input)` 一行就是修复。

这段代码有一段真实历史(可用 `git log -L 1214,1220:src/buffer_diff.rs` 查证,引入提交为 `eb962794a3`,修复 issue #60424):

- **现象**:对含重复 `end\n\n` 行的文件,staging 第一个删除 hunk 后,UI 把第二个删除 hunk 也标成已 staged;点击(错误显示的)Unstage 按钮,每次都会向 git index **复制一份**该 hunk 的内容。
- **根因**:uncommitted diff(HEAD vs 工作区)与 unstaged diff(index vs 工作区)对同一逻辑删除锚定了不同行,跨 diff 的 hunk 对齐全部失配。
- **修复**:把 imara-diff 从 0.1.8 升到 0.2.0 并调用 `postprocess_lines`。迁移顺带解释了当前 API 形状——0.2 移除了旧的 `Sink` trait 与顶层 `diff()` 函数,调用方改为 `Diff::compute` + `hunks()`;`lines_with_terminator` 改名为 `lines` 且默认 tokenization 就包含行尾符(这正是 4.2 节的依据)。
- **收益**:规范化后 Zed 的 hunk 摆放与 `git diff` 命令行输出一致(git 自 2.11 起默认启用 indent 启发式),肉眼核对更方便。
- 该提交还添加了回归测试 `test_staging_hunks_with_ambiguous_placement`(位于 `crates/project/tests/integration/project_tests.rs`,行号待确认,可用 `git grep -n test_staging_hunks_with_ambiguous_placement` 定位),并在 `compute_uncommitted_index_edits` 里加了纵深防御(u4-l4 会讲到)。

一句话总结:**单一 diff 的「等价摆放」在多 diff 对齐的语境下不再等价,postprocess_lines 把选择权从算法实现手里收走,变成确定性的规范**。

#### 4.3.2 核心流程

```text
Diff::compute(...)          # 可能返回多种最小编辑脚本中的任意一种
        │ postprocess_lines(&input)
        ▼
规范化后的 Diff            # 歧义 hunk 的锚定唯一确定:
                            #   同一 buffer + 任意 base → 同一逻辑改动 → 同一行
```

用 2.2 节的例子:base 有两个空行夹着 `"bbb\n" "ccc\n"`,删除区间可摆成 `2..5` 或 `3..6`。规范化后只会有一种结果(具体选哪种由启发式依据局部内容决定,不依赖 buffer 其余部分)。

#### 4.3.3 源码精读

调用点只有一个:

[src/buffer_diff.rs:L1213-L1220](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1213-L1220) — `Diff::compute` 之后、`hunks()` 之前插入 `postprocess_lines(&input)`。顺序不可颠倒:必须在消费 hunk 之前完成规范化。imara-diff 的内部实现不在本仓库中,以[其公开文档](https://docs.rs/imara-diff)为准。

#### 4.3.4 代码实践:复现「同一 buffer、两个 base、锚定必须一致」

这是本讲的核心实践,对应本讲规格中的实践任务。

**实践目标**:亲手验证——同一份 buffer 对两个内容略有差异的 base 文本计算 diff,规范化后同一逻辑改动的 `buffer_range` 一致;去掉规范化后观察是否漂移。

**操作步骤**:

1. 在 `mod tests` 里新增测试(示例代码,非项目原有):

```rust
#[gpui::test]
async fn test_ambiguous_placement_canonicalized(cx: &mut gpui::TestAppContext) {
    // buffer:删掉了 bbb/ccc 两行,前后各剩一个空行
    let buffer = Buffer::new(
        ReplicaId::LOCAL,
        BufferId::new(1).unwrap(),
        "aaa\n\nddd\n".to_string(),
    );

    // base A:空行在删除块前后各一个 → 删除区间存在两种等价摆放
    let base_a = "aaa\n\nbbb\nccc\n\nddd\n";
    // base B:只剩前一个空行 → 删除块后面紧贴 ddd,摆放唯一
    let base_b = "aaa\n\nbbb\nccc\nddd\n";

    for base in [base_a, base_b] {
        let diff = BufferDiffSnapshot::new_sync(&buffer, base.to_string(), cx);
        for hunk in diff.hunks(&buffer) {
            println!("base={:?} hunk buffer_range={:?}", base, hunk.range);
        }
    }
}
```

   (提示:`BufferDiffSnapshot::hunks` 的签名见 [L430-L438](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L430-L438),它内部就是用 min/max 锚点区间取全部 hunk;更贴近真实场景的构造见步骤 4。)
2. 运行并记录输出:`cargo test -p buffer_diff test_ambiguous_placement_canonicalized -- --nocapture`。
3. 在**练习分支**上临时注释掉 [L1220](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1220) 的 `diff.postprocess_lines(&input);`,重新运行,对比两个 base 的 `buffer_range` 是否不再一致。**观察完务必还原**(这是练习分支上的临时改动,不属于本教程的正式修改)。
4. 对照命令行 git 的选择:把 base 与 buffer 分别存成文件,运行 `git diff --no-index base.txt buffer.txt`,查看 git 把删除块锚定在哪个空行。按 4.3.1 引用的提交说明,启用 `postprocess_lines` 后 Zed 的摆放应与 git 一致。

**需要观察的现象**:步骤 2 中两个 base 产出的同一逻辑删除 `buffer_range` 是否相同;步骤 3 去掉规范化后是否出现漂移;步骤 4 中 zed 与 git 是否一致。

**预期结果**:启用 `postprocess_lines` 时两个 base 的锚定一致;去掉后**可能**不一致(取决于算法内部选择——历史上 #60424 的真实样本确实不一致)。各步骤的具体行号数值待本地验证。

#### 4.3.5 小练习与答案

**练习 1**:「diff 结果等价」与「diff 摆放一致」是两个不同的要求。请各用一句话说明 imara-diff 与 buffer_diff 分别关心哪个。

**答案**:imara-diff 关心等价性——两种摆放的应用结果文本相同、编辑代价相同;buffer_diff(的多基准对齐逻辑)关心一致性——同一逻辑改动在不同 diff 里必须落在相同行,否则 staging 状态判断与 index 编辑计算全部失配,`postprocess_lines` 负责把前者规范成后者。

**练习 2**:如果去掉 `postprocess_lines`,单基准、只看编辑器 gutter 里的 diff 标记,用户大概率感知不到问题。为什么 Zed 仍然必须加这一行?

**答案**:因为 damage 出在跨 diff 对齐:uncommitted diff(HEAD 基准)与 secondary diff(index 基准)各自独立计算,摆放不一致时 `hunks_intersecting_range_impl` 的 secondary 匹配和 `compute_uncommitted_index_edits` 的投影都会算错——前者误标 staged,后者会向 index 重复插入内容(即 #60424)。这正是注释里「hunks render as staged when they aren't, and staging or unstaging them corrupts the index」的含义。

### 4.4 两个特例:空 buffer 与缺失 base

#### 4.4.1 概念说明

`compute_hunks` 里有两个分支**完全绕过 diff 算法**,手工构造唯一的 hunk。它们的共同点是:走正常算法会得到「形式上正确、语义上怪异」的结果。

- **特例一:空 buffer。** Zed 里「空 buffer」不是空字符串而是一个 `"\n"`(单个换行符,即一个空行)。如果拿 `"\n"` 与一个多行 base 直接跑算法,LCS 为空,会得到「删掉全部 base 行 + 新增一个空行」的 hunk——buffer 里凭空多出一条「新增空行」的记录,即注释所说的 *a "preserved" line in the middle, which is a bit odd*。
- **特例二:base 缺失。** `diff_base` 为 `None` 表示文件根本没有基准版本——典型场景是 git 里未跟踪(untracked)的新文件。此时没有任何东西可 diff,唯一合理的结果是「整个文件都是新增的」。

#### 4.4.2 核心流程

```text
特例一(命中条件:buffer_text == "\n" 且 base 以 "\n" 结尾且 len > 1):
    输出 1 个 hunk:
      buffer_range          = anchor_before(0)..anchor_before(0)     # 空 → Deleted
      diff_base_byte_range  = 0..base.len() - 1                      # 去掉最后一个换行字节
      diff_base_point_range = (0,0)..offset_to_point(base.len()-1)
    语义:整个 base 被删除,buffer 只剩一个末尾换行(与末行换行「对齐」)

特例二(base 为 None):
    输出 1 个 hunk:
      buffer_range          = Anchor::min_max_range_for_buffer(...)  # 整个 buffer
      diff_base_byte_range  = 0..0                                    # 空 → Added
    语义:整文件新增(is_created_file() == true)
```

两个特例的 status 判定完全复用 u2-l1 讲过的规则([L2297-L2309](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L2297-L2309)):buffer 侧区间空 → `Deleted`;base 侧区间空 → `Added`。特例二还满足 [is_created_file](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L2291-L2295) 的全部条件(`diff_base_byte_range == 0..0` 且锚点为 min/max 哨兵)。

#### 4.4.3 源码精读

[src/buffer_diff.rs:L1194-L1210](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1194-L1210) — 空 buffer 特例:注释解释动机;命中后手工 push 一个 hunk 并提前 `return`。三个条件的细节:buffer 文本恰为 `"\n"`;base 以换行结尾(归一化后的非空 base 总是满足,此检查是防御性的);`diff_base.len() > 1` 排除 base 本身就是单个换行的平凡情形(那种情况走正常算法,LCS 匹配,结果为空树)。

`diff_base_byte_range` 取 `0..len-1` 而不是 `0..len`:**排除 base 的最后一个换行字节**,把它当作与 buffer 中那个仅剩的换行「相同」——这样 diff 语义变成「删掉末行换行之前的所有内容」,而不是「连末尾换行也删掉、再新增一个换行」。

[src/buffer_diff.rs:L1228-L1239](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1228-L1239) — 无 base 特例:用 min/max 哨兵锚点覆盖整个 buffer,base 两侧区间全部为空,词级差异留空。

注意区分「无 base 的 hunk」与「尚未计算」:[snapshot()](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L2164-L2175) 在 `diff_snapshot` 为 `None`(实体刚创建、还没跑过任何 diff)时返回的是 `base_text_exists = false` 的**空**兜底快照——树里没有 hunk;只有当 `update_diff` 真的以 `base_text: None` 调用过 `compute_hunks` 后,树里才会有那个「整文件新增」hunk。test_buffer_diff_simple 的结尾([L2485-L2495](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L2485-L2495))验证的正是前者:刚 `BufferDiff::new` 出来、没设 base 时,hunks 为空。

#### 4.4.4 代码实践:验证两个特例的输出形态

**实践目标**:分别触发两个特例,断言 hunk 的形态与 status。

**操作步骤**:

1. 在 `mod tests` 新增(示例代码,非项目原有):

```rust
#[gpui::test]
async fn test_compute_hunks_special_cases(cx: &mut gpui::TestAppContext) {
    // 特例一:空 buffer("\n")对多行 base
    let buffer = Buffer::new(ReplicaId::LOCAL, BufferId::new(1).unwrap(), "\n".to_string());
    let diff = BufferDiffSnapshot::new_sync(&buffer, "one\ntwo\n".to_string(), cx);
    let hunks = diff.hunks(&buffer).collect::<Vec<_>>();
    assert_eq!(hunks.len(), 1);
    assert_eq!(hunks[0].status().kind, DiffHunkStatusKind::Deleted);
    assert_eq!(hunks[0].diff_base_byte_range, 0.."one\ntwo\n".len() - 1);

    // 特例二:base 缺失(直接调 compute_hunks 的 None 分支)
    let buffer2 = Buffer::new(ReplicaId::LOCAL, BufferId::new(2).unwrap(), "new\n".to_string());
    let tree = compute_hunks(None, buffer2.snapshot(), None);
    assert_eq!(tree.summary().added_rows, 1);
}
```

2. 运行:`cargo test -p buffer_diff test_compute_hunks_special_cases`。
3. 把 base 换成 `"one"`(不以 `\n` 结尾)再跑,观察特例一是否不再命中(归一化后的正常路径不会出现这种输入,这一步只是验证条件判断)。

**需要观察的现象**:特例一产出单个 Deleted hunk 且 base 字节区间不含末尾换行;特例二的树摘要 `added_rows` 等于 buffer 行数。

**预期结果**:如上;`status().kind` 的写法依据 [L86-L97](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L86-L97)(`kind` 字段与 `DiffHunkStatusKind` 枚举均为公开),也可以改用 `assert_hunks` 直接断言四元组。两个特例的断言数值未在撰写环境中运行过,待本地验证。

#### 4.4.5 小练习与答案

**练习 1**:特例一里为什么 `diff_base_byte_range` 是 `0..len-1` 而不是 `0..len`?

**答案**:buffer 仅剩的 `"\n"` 被视为与 base 的末行换行相同,删除区间只覆盖到最后一个换行字节之前的内容;若取 `0..len`,语义上等于「连末尾换行一起删掉」,那么 buffer 里的换行就成了无中生有的新增行——这正是特例想避免的怪异输出。

**练习 2**:一个刚 `BufferDiff::new(...)` 出来、还没设置 base 也没跑过 diff 的实体,`snapshot()` 里的 hunk 树是「整文件新增」吗?

**答案**:不是。`diff_snapshot` 字段为 `None`,`snapshot()` 返回 `base_text_exists = false` 的空兜底快照,树里没有任何 hunk;「整文件新增」hunk 只有在 `update_diff` 以 `base_text: None` 真正调用过 `compute_hunks(None, ...)` 之后才会出现。

**练习 3**:想一想特例二的 hunk 为什么必须用 `Anchor::min_max_range_for_buffer` 哨兵锚点,而不是 `anchor_before(0)..anchor_after(len)` 这类具体位置?

**答案**:用 min/max 哨兵既保证区间覆盖整个 buffer(包括未来的任何编辑),又使 `is_created_file()` 的判定(`start.is_min() && end.is_max()`)成立;具体位置锚点在 buffer 后续编辑后可能不再覆盖全文,且无法被识别为「整文件新增」。(锚点稳定性见 u2-l1。)

## 5. 综合实践

把本讲三个知识点串成一条流水线:**算法输出 → 规范化 → 特例**。

任务:写一个测试 `test_compute_hunks_pipeline`(示例代码,非项目原有),对一个三行 base `"...one\ntwo\nthree\n"` 完成以下步骤并在每步打印观察:

1. **正常路径**:buffer 把 `two` 改成 `TWO`,用 `BufferDiffSnapshot::new_sync` 计算,`assert_hunks` 断言单个 Modified hunk(模仿 [test_buffer_diff_simple](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L2442-L2483) 的写法)。
2. **裸算法对照**:同一段文本直接用 `InternedInput::new` + `Diff::compute` + `hunks()` 打印 `before/after` 行号,人工核对与步骤 1 的 hunk 行区间、base 字节区间吻合(用 4.2.4 的代码)。
3. **歧义规范化**:换用 4.3.4 的空行滑动文本,记录规范化开启/注释两种情况下的 `buffer_range`(记得还原)。
4. **特例**:把 buffer 全部删空(编辑成 `"\n"`),`recalculate_diff_sync`([L2272-L2283](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L2272-L2283) 是同步重算入口)后断言得到单个整文件 Deleted hunk;再对一个 base 为 `None` 的调用断言整文件 Added。

预期:四个步骤分别覆盖 `compute_hunks` 的主路径、imara-diff 原始输出、规范化必要性、两个手写特例。具体断言数值待本地验证,不确定处先用 `println!` 观察、再收紧断言。

## 6. 本讲小结

- `compute_hunks`([L1184-L1242](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1184-L1242))是全 crate 唯一的 diff 计算点:无状态纯函数,在 `update_diff` 的后台线程里运行,输出 `SumTree<InternalDiffHunk>`。
- imara-diff 三步用法:`InternedInput::new(lines(base), lines(buffer))` 装载(token 含行尾符)→ `Diff::compute(Algorithm::Histogram, &input)` 计算 → `hunks()` 取 `before`(base 行号)/`after`(buffer 行号)对;行号到字节/锚点的换算全部由本 crate 的 HunkSink 完成(下一讲)。
- `postprocess_lines` 把歧义 hunk 的摆放规范化为唯一结果;没有它,同一 buffer 对 HEAD 与 index 的两份 diff 可能把同一逻辑改动锚定在不同行,导致 staged 状态误判甚至 index 损坏(#60424,修复提交 `eb962794a3`),且规范化后 Zed 的摆放与 `git diff` 一致。
- 空 buffer(`"\n"`)特例产出单个整文件 Deleted hunk,base 区间不含末尾换行;base 缺失(`None`)特例产出单个整文件 Added hunk(`is_created_file()` 为真);两者都不走 diff 算法。
- base 文本进入计算前统一被 LF 归一化;「实体尚未计算 diff」与「无 base」是两种不同状态,前者返回空兜底快照。

## 7. 下一步学习建议

- 下一讲 **u3-l2《HunkSink:从行号到字节区间与锚点》**承接本讲留下的边界:imara-diff 只给行号,`HunkSink` 的 `compute_line_offsets` 行偏移表与 `process_change` 如何把行号变成 `diff_base_byte_range`、锚点区间与 `diff_base_point_range`。
- 之后 **u3-l3** 讲词级差异(`build_diff_options` 与 `word_diff_ranges`),解释 `compute_hunks` 第三个参数 `diff_options` 的用途。
- 建议同步阅读:imara-diff 的[公开文档](https://docs.rs/imara-diff)(特别是 `postprocess_lines` 与 histogram 算法的说明),以及 `crates/language/src/text_diff.rs` 中另一个 `Diff::compute` 消费者如何复用同一套 API。
- 想深入规范化历史的读者,可在仓库根运行 `git show eb962794a3` 阅读完整修复提交及其回归测试。
