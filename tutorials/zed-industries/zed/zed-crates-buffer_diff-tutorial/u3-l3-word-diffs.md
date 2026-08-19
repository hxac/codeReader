# 词级差异：word_diff_ranges 与设置开关

## 1. 本讲目标

上一讲（u3-l2）我们讲完了 `HunkSink::process_change` 的「行号 → 三套坐标」换算链路，但刻意绕开了其中的一整块：词级差异（word diff）。本讲把它补全。行级 diff 只能告诉你「第 4 行改了」，而 word diff 能告诉你「第 4 行里改的是 `count` 这个词」——编辑器 gutter 里那种行内高亮（只把变动的单词涂色）就是靠它画出来的。学完本讲你应该能：

1. 说出 word diff 的完整触发条件：diff 选项存在（设置开启）、buffer 侧非空、两侧行数相等、行数不超过 `MAX_WORD_DIFF_LINE_COUNT = 5`。
2. 解释 `base_word_diffs`（相对字节偏移）与 `buffer_word_diffs`（锚点）为什么是两种不同的表示，以及消费端如何分别使用它们。
3. 讲清设置项 `word_diff_enabled` 如何经 `build_diff_options` 进入 diff 计算流程、关闭时整个 word diff 如何被「整体跳过」。
4. 读懂 language crate 里 `word_diff_ranges` 的算法：按词切分 token、二次 diff、相邻区间合并。

## 2. 前置知识

本讲建立在前几讲的概念之上，先快速回顾，再补充两个新名词。

- **hunk 与三种状态**（u2-l1）：一个 `DiffHunk` 描述一块差异；buffer 侧区间空为 Deleted、base 侧区间空为 Added、两侧都非空为 Modified。word diff 只在「两侧都非空」的 hunk 上才有意义——纯新增/纯删除的行没有「对应的另一侧」可以做词级比对。
- **锚点（Anchor）与字节偏移**（u1-l1、u3-l2）：diff 在后台线程异步计算，算完写回时 buffer 可能已被继续编辑。base 侧在计算阶段只是 `Arc<str>` + `Rope`，没有锚点系统，所以用字节偏移；buffer 侧是活的 buffer，用锚点才能跨编辑稳定。**「一边有身份系统、一边没有」正是本讲两种 word diff 表示的根源。**
- **`DiffOptions`**：language crate 导出的配置结构体，含 `language_scope`（按语言决定哪些字符算「词」）、`max_word_diff_len`、`max_word_diff_line_count` 三个字段。注意它同时服务于 language crate 自己的 `text_diff` 家族和本 crate 的 hunk 内 word diff，两边的消费方式不同（见 4.3）。
- **token 与 interner**（u3-l1）：imara-diff 只认「token 序列」。上一讲 token 是「一整行」；本讲 token 是「一个词」。同一个算法库，换一种切分粒度，就从行级 diff 变成了词级 diff——这是本讲最核心的洞见。
- **设置系统**：Zed 的语言设置（`LanguageSettings`）按语言可配，`word_diff_enabled` 是其中一个布尔项，控制「编辑器是否高亮修改行内变动的单词」。

## 3. 本讲源码地图

本讲主线在 buffer_diff.rs，但算法实现跨到 language crate：

| 位置 | 作用 |
| --- | --- |
| [buffer_diff.rs:L20](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L20) | `MAX_WORD_DIFF_LINE_COUNT = 5`：word diff 的行数上限常量 |
| [buffer_diff.rs:L115-L129](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L115-L129) | `DiffHunk`：公开形态，两个 word diff 字段及注释 |
| [buffer_diff.rs:L1159-L1182](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1159-L1182) | `build_diff_options`：设置 → `DiffOptions` 的翻译层（4.1 主角） |
| [buffer_diff.rs:L1933-L1968](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1933-L1968) | `update_diff`：调用 `build_diff_options` 并把选项带进后台任务 |
| [buffer_diff.rs:L1280-L1344](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1280-L1344) | `HunkSink::process_change`：word diff 分支（4.2 主角） |
| [text_diff.rs:L181-L216](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/language/src/text_diff.rs#L181-L216) | `word_diff_ranges`：language crate 的词级 diff 算法（4.3 主角） |
| [text_diff.rs:L237-L251](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/language/src/text_diff.rs#L237-L251) | `DiffOptions` 结构体与默认值 |
| [text_diff.rs:L322-L335](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/language/src/text_diff.rs#L322-L335) | `should_perform_word_diff_within_hunk`：language 侧的对照触发条件 |
| [text_diff.rs:L337-L373](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/language/src/text_diff.rs#L337-L373) | `diff_internal`：token 区间 → 字节区间的换算 |
| [text_diff.rs:L383-L411](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/language/src/text_diff.rs#L383-L411) | `tokenize`：按字符类别切词 |
| [buffer.rs:L579-L605](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/language/src/buffer.rs#L579-L605) | `CharKind` 与 `CharScopeContext`：切词的字符分类依据 |
| [language_settings.rs:L165-L171](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/language/src/language_settings.rs#L165-L171) | `word_diff_enabled` 设置项的定义与文档 |
| [multi_buffer.rs:L3488-L3515](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/multi_buffer/src/multi_buffer.rs#L3488-L3515) | 下游消费：两种表示如何在渲染前汇合 |
| [element.rs:L4596-L4615](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/editor/src/element.rs#L4596-L4615) | 下游消费：编辑器按视口过滤可见的 word diff |
| [buffer_diff.rs:L1399-L1433](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1399-L1433) | `compare_hunks`：word diff 参与新旧 hunk 相等性判定 |
| [buffer_diff.rs:L3162-L3252](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L3162-L3252) | `test_buffer_diff_compare`：word diff 影响变更范围检测的实证 |

## 4. 核心概念与源码讲解

### 4.1 build_diff_options：设置如何进入 diff 计算流程

#### 4.1.1 概念说明

word diff 是有代价的：每个 hunk 都要在行级 diff 之外再做一次（甚至多次）词级 diff。对大文件、大 hunk 来说这是纯开销，所以它必须既能被总量控制（行数上限），也能被整体关掉（用户设置）。`build_diff_options` 就是「用户设置 → 计算参数」的翻译层：它把 `LanguageSettings::word_diff_enabled` 这个布尔设置翻译成一个 `Option<DiffOptions>`——`Some(...)` 表示「要做 word diff，参数如下」，`None` 表示「整个 word diff 都不要算」。

用 `Option` 承载开关是本讲反复出现的模式：下游代码（`HunkSink`）只需一次 `if let Some(...)` 判断，就把「不做」的成本降到了零——连词级 diff 的调用都不会发生。

#### 4.1.2 核心流程

```text
update_diff(buffer, base_text_snapshot, base_text, cx)
  ├─ language = base_text_snapshot.language()          # 取 base 文本的语言
  ├─ diff_options = build_diff_options(
  │      language.map(|l| l.name()),                    # 语言名：查哪种语言的设置
  │      language.map(|l| l.default_scope()),           # 语言 scope：切词时用哪套词字符
  │      cx)
  │    ├─ 【仅 test/test-support 构建】若没有 SettingsStore 全局
  │    │     → 直接返回 Some(DiffOptions { max_word_diff_line_count: 5, ..默认 })
  │    └─ LanguageSettings::resolve(None, language, cx).word_diff_enabled
  │          .then_some(DiffOptions { language_scope, max_word_diff_line_count: 5, ..默认 })
  │          # 设置为 true  → Some(...)
  │          # 设置为 false → None（整体跳过）
  └─ 把 diff_options 移进后台任务 → compute_hunks → HunkSink
```

两个要点：

- `LanguageSettings::resolve(None, language.as_ref(), cx)` 按语言名解析设置，所以 `word_diff_enabled` 可以全局关，也可以只对某种语言关。
- `.then_some(...)` 是惯用的「布尔 → Option」转换：`false.then_some(x)` 得 `None`。设置关闭时不返回「空选项」，而是返回 `None`——这个区别在 4.2 会看到后果。

#### 4.1.3 源码精读

先看函数全貌：

[buffer_diff.rs:L1159-L1182](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1159-L1182) 这段代码先处理测试场景，再用真实设置解析。两个分支构造的 `DiffOptions` 内容一样：`language_scope` 透传进来，`max_word_diff_line_count` 硬编码为本 crate 的常量 5，其余字段取默认值。

[buffer_diff.rs:L1164-L1173](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1164-L1173) 这段是测试捷径：本 crate 的绝大多数测试（以及依赖 `test-support` 特性的下游测试）不会安装完整的设置系统，没有 `SettingsStore` 全局时读设置会失败，所以直接返回一份「word diff 开启、上限 5 行」的默认选项。**这意味着本 crate 自己的单元测试里 word diff 永远是开启的**——本讲实践环节正是建立在这个前提上。

[buffer_diff.rs:L1175-L1181](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1175-L1181) 这段是生产路径：解析语言设置，读 `word_diff_enabled`，再用 `then_some` 决定返回 `Some(选项)` 还是 `None`。

再看设置项本身的定义：

[language_settings.rs:L165-L171](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/language/src/language_settings.rs#L165-L171) 这段文档声明了设置语义：开启时「修改行内变动的单词会被高亮」，默认 `true`。用户在 `settings.json` 里写 `"languages": { "Rust": { "word_diff_enabled": false } }` 即可对 Rust 关闭。

最后看调用点：

[buffer_diff.rs:L1950-L1955](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1950-L1955) 这段代码从 base 文本快照取语言，把语言名和默认 scope 传给 `build_diff_options`。注意语言信息来自 **base 文本** 而非 buffer——diff 的两侧理应同属一个文件，取哪边通常等价，但实现上取的是基准侧。

选项随后被移进后台任务（[buffer_diff.rs:L1970-L1981](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1970-L1981)），最终传给 `compute_hunks` 与 `HunkSink::new`（u3-l2 已讲）。还要注意 `update_diff` 开头的 `unchanged_hunks` 快路径（[buffer_diff.rs:L1959-L1968](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1959-L1968)）：buffer 与 base 都没变时直接复用旧 hunk 树，此时 word diff 也不会重算——它跟着 hunk 树一起被缓存。

#### 4.1.4 代码实践

**实践目标**：直接调用私有的 `build_diff_options`，验证「测试环境下没有 SettingsStore 时返回 Some 且上限为 5」这一结论。`mod tests` 里的 `use super::*` 能把私有自由函数也带进来（u1-l3 讲过测试模块可以访问父模块私有项）。

**操作步骤**：在 `mod tests` 内新增（示例代码，非项目原有）：

```rust
#[gpui::test]
async fn test_build_diff_options_test_default(cx: &mut gpui::TestAppContext) {
    let options = cx.update(|cx| build_diff_options(None, None, cx)).unwrap();
    assert_eq!(options.max_word_diff_line_count, MAX_WORD_DIFF_LINE_COUNT);
}
```

运行：`cargo test -p buffer_diff test_build_diff_options_test_default`。

**需要观察的现象**：测试通过；`options` 是 `Some`，且行数上限等于 [buffer_diff.rs:L20](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L20) 的常量 5。

**预期结果**：通过——本 crate 测试不安装 `SettingsStore`，走的是 `#[cfg(any(test, feature = "test-support"))]` 分支（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么测试分支返回的是 `Some(...)` 而不是照搬设置解析？如果删掉这个分支，本 crate 的测试会怎样？

**答案**：测试里通常没有安装 `settings::SettingsStore` 全局，`LanguageSettings::resolve` 读不到设置会出问题；没有这个分支，测试要么 panic 要么拿到错误的默认。返回 `Some` 还有一个副作用：本 crate 单元测试的 word diff 永远开启，测试行为稳定、不受用户设置影响。

**练习 2**：`build_diff_options` 返回 `None` 与返回 `Some(DiffOptions { language_scope: None, .. })` 有什么本质区别？

**答案**：`None` 表示「整个 word diff 不算」，`HunkSink` 连 `word_diff_ranges` 都不会调用；`Some` 只是「没有特定语言的 scope」，切词退回默认字符分类，word diff 照算。一个是功能开关，一个是参数缺失。

### 4.2 HunkSink 的 word diff 分支：触发条件与两种表示

#### 4.2.1 概念说明

`process_change` 在算完 hunk 的三套坐标（u3-l2）后，还有最后一步：对这个 hunk 覆盖的两侧文本再做一次**词级** diff，把「改动的单词」记进 hunk。它不是无条件做的，触发条件可以写成：

\[ \text{worddiff}(h) \;=\; \text{opts} \neq \bot \;\wedge\; |h_{\text{buffer}}| \ge 1 \;\wedge\; |h_{\text{base}}| = |h_{\text{buffer}}| \;\wedge\; |h_{\text{base}}| \le \text{max\_line\_count} \]

用文字说就是四个条件同时成立：

1. **`diff_options` 是 `Some`**（设置开启；`None` 直接短路）；
2. **buffer 侧行数非空**（纯删除的 hunk 跳过）；
3. **base 侧行数 == buffer 侧行数**（等行数；纯新增以及「2 行改 3 行」这类不等行数的修改都跳过）；
4. **base 侧行数 ≤ `max_word_diff_line_count`（5）**（大 hunk 跳过，控制计算量）。

条件 2、3 合起来等价于「两侧都非空且行数相等」——即只有**等行数的 Modified hunk** 才有 word diff。条件 3 的动机源码没有注释，但可以从渲染契约推断（标注为推断）：编辑器把 word 高亮画在「修改行」内部，删除侧与新增侧的行需要一一配对（第 k 行 base 对第 k 行 buffer）；行数不等时不存在唯一的配对方式，词级比对就没有良定义的结果。

产出是两种表示，正是 `DiffHunk` 上那两个字段（[buffer_diff.rs:L125-L128](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L125-L128) 的注释原文）：

| 字段 | 类型 | 参照系 | 为什么 |
| --- | --- | --- | --- |
| `buffer_word_diffs` | `Vec<Range<Anchor>>` | 活 buffer 的锚点 | buffer 会被继续编辑，高亮必须跟着文本走；锚点跨编辑稳定（u2-l1 讲过同样理由） |
| `base_word_diffs` | `Vec<Range<usize>>` | **相对 hunk 的 `diff_base_byte_range.start` 的偏移** | base 文本不可变（只读 buffer），偏移永远不过期；存相对偏移还能让 hunk 在 base 文本更新前后语义自洽 |

注意 `base_word_diffs` 的偏移**不是**从 base 文本开头数起，而是从本 hunk 的 base 切片开头数起——消费端必须先加上 `diff_base_byte_range.start` 才能定位（4.4 会看到 multi_buffer 正是这么做的）。

#### 4.2.2 核心流程

```text
process_change(before, after) 已经算出：
  diff_base_byte_range   # base 侧字节区间（含行尾，行对齐）
  buffer_range           # buffer 侧锚点区间（行首到行首）

word diff 分支：
  if diff_options 存在
     && buffer 侧行数 > 0
     && base 行数 == buffer 行数
     && max_word_diff_line_count >= base 行数:
      base_text   = base Rope 按 diff_base_byte_range 切片
      buffer_text = buffer 按 buffer_range 切文本
      (base_word_diffs, buffer_word_diffs_relative)
          = word_diff_ranges(base_text, buffer_text, options)
      buffer_word_diffs = buffer_word_diffs_relative 的每个区间
          → 加上 buffer_range 起始偏移 → 在 buffer 上铸 anchor_after 锚
  else:
      两侧都存空 Vec
```

一个容易忽略的细节：铸锚用的是 `anchor_after`（向右偏置）而不是 `anchor_before`。这样当用户恰好在词边界插入字符时，词高亮区间倾向于「留在原词上」而不是吞掉新插入的字符。

#### 4.2.3 源码精读

[buffer_diff.rs:L1294-L1301](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1294-L1301) 这段代码先取两侧行数，再用 `if let Some(...) && ...` 的 let-chains 写下 4.2.1 的全部触发条件。`base_line_count`/`buffer_line_count` 就是 imara-diff 输出的 `before`/`after` 行区间长度（u3-l2）。

[buffer_diff.rs:L1302-L1306](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1302-L1306) 这段代码把 hunk 覆盖的两侧文本各切出来：base 侧用 Rope 的 `chunks_in_range`，buffer 侧用 `text_for_range`。两个区间都是行对齐的（行首到行首），所以切出的文本**以换行符结尾**——word diff 是含着这些换行符一起做的。

[buffer_diff.rs:L1308-L1315](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1308-L1315) 这段代码调用 language crate 的 `word_diff_ranges`，拿回 `(base 侧偏移列表, buffer 侧相对偏移列表)`——两个列表都是**相对各自切片开头**的字节区间。中间那个看似多余的 `DiffOptions { language_scope: ..., ..*diff_options }` 只是因为 `DiffOptions` 没有实现 `Clone`，用手写结构体语法做一次值拷贝以移交所有权，语义上没有任何改动。

[buffer_diff.rs:L1317-L1325](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1317-L1325) 这段代码把 buffer 侧的相对偏移换算成锚点：先求出 hunk 在 buffer 中的起始偏移 `buffer_start_offset`，每个词区间加上它得到绝对偏移，再逐个铸 `anchor_after` 锚。base 侧的偏移则原样存进 `base_word_diffs`，不做加法——参照系转换留给消费端。

[buffer_diff.rs:L1328-L1330](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1328-L1330) 这段是 else 分支：任何条件不满足时两侧都存空 `Vec`。注意「不触发」和「触发了但没有词差异」（两侧切片逐词相同）都表现为空列表，消费端不区分这两种情况。

[buffer_diff.rs:L1332-L1343](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1332-L1343) 这段代码把 word diff 与三套坐标一起装进 `InternalDiffHunk` 入树。查询 API 再原样透传给公开的 `DiffHunk`（[buffer_diff.rs:L1045-L1046](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1045-L1046) 取出，[buffer_diff.rs:L1121-L1128](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1121-L1128) 组装）。

最后是 word diff 的一个隐蔽副作用——它参与「diff 是否变了」的判定。[buffer_diff.rs:L1399-L1401](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1399-L1401) 这段代码在新旧 hunk 锚点起点相同时用 `new_hunk != old_hunk` 做整体比较，而 `InternalDiffHunk` 的 `PartialEq` 派生（[buffer_diff.rs:L132](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L132)）**包含 word diff 字段**。所以哪怕一次编辑前后 hunk 的行范围完全相同，只要词级差异变了，`compare_hunks` 也会判定「diff 变了」并发出更小范围的 `DiffChanged`。测试里有现成实证：[buffer_diff.rs:L3215-L3228](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L3215-L3228) 这段把 `SIX` 改成 `SIX.5`（hunk 行范围不变、word diff 变了），随后断言 `changed_range` 恰好是这一行——注释直白地写着「这次编辑影响 diff，因为它重算了 word diff」。（`compare_hunks` 的完整算法留给 u3-l5。）

#### 4.2.4 代码实践

**实践目标**：给 `process_change` 的 word diff 分支临时加一行日志，观察现有测试里各 hunk 的两侧行数与触发结果，把 4.2.1 的条件表变成亲眼所见。

**操作步骤**：

1. 在 [buffer_diff.rs:L1301](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1301) 的 `{` 后临时插入一行（仅本地实验，勿提交）：
   ```rust
   eprintln!("word diff? base_lines={base_line_count} buffer_lines={buffer_line_count}");
   ```
   同时在 else 分支（L1328）前加 `eprintln!("word diff skipped");`。
2. 运行一个有修改行的测试并显示输出：
   ```bash
   cargo test -p buffer_diff test_buffer_diff_compare -- --nocapture
   ```

**需要观察的现象**：`test_buffer_diff_compare` 的初始文本里，base（`zero` 到 `nine` 共 10 行）与 buffer（8 行）的差异是：删除 `zero`、删除 `two`、`six`→`SIX`、`nine`→`NINE`。日志应显示：两个删除 hunk 走 skipped（buffer 侧行数为 0）；`SIX`/`NINE` 两个 1 行改 1 行的 hunk 进入 word diff 分支（`base_lines=1 buffer_lines=1`）。此后测试还会把 `SIX` 编辑成 `SIX.5`，重复观察一轮。

**预期结果**：与上述一致（待本地验证）。观察完删掉这两行日志。

#### 4.2.5 小练习与答案

**练习 1**：下面五个 hunk，哪些会计算 word diff？（a）插入 2 行；（b）删除 3 行；（c）3 行改成 3 行；（d）6 行改成 6 行；（e）2 行改成 3 行。

**答案**：只有 (c)。(a) base 侧 0 行 ≠ 2 行；(b) buffer 侧为空；(c) 等行数且 ≤5，触发；(d) 等行数但 6 > 5；(e) 行数不等。顺带一提 (d) 的状态仍是 Modified——「Modified 状态」是 word diff 的必要条件但远不充分。

**练习 2**：为什么 `base_word_diffs` 可以存普通整数偏移，而 `buffer_word_diffs` 必须存锚点？

**答案**：word diff 是异步算的，写回时 buffer 可能又编辑过——只有锚点能在后续编辑下保持指向「原来那个词」；base 文本则来自只读的 `base_text_buffer`，从不变更，偏移天然不过期，用整数更省、比较更快。

**练习 3**：如果把条件 `base_line_count == buffer_line_count` 放宽为「两侧都非空」，会出什么问题？

**答案**：`word_diff_ranges` 本身能算（它是纯文本比对），但两侧切片长度不同的行失去一一配对，`base_word_diffs` 的第 i 个区间不再对应 `buffer_word_diffs` 的第 i 个区间，下游把删除侧高亮画进「已删除行」、新增侧画进「修改行」的配对渲染就错位了；同时大 hunk 的计算量失去等行数这层天然约束。（推断自渲染契约，源码未注释。）

### 4.3 word_diff_ranges：language crate 里的词级 diff 算法

#### 4.3.1 概念说明

`word_diff_ranges` 是 language crate 的公开函数（buffer_diff.rs 顶部导入，[buffer_diff.rs:L3-L6](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L3-L6)），输入两段文本和一个 `DiffOptions`，输出两个列表：**旧文本里变动的字节区间**与**新文本里变动的字节区间**。

它的算法正是 u3-l1 讲过的「token + imara-diff」模式的复用，只是切分粒度从「行」换成了「词」：

- 行级 diff（`compute_hunks`）：token = 一整行（`imara_diff::sources::lines`）；
- 词级 diff（`word_diff_ranges`）：token = 一个词（自写的 `tokenize`）。

「换 token 粒度就换 diff 粒度」——这是 imara-diff 设计的直接红利，也是整个 Zed diff 体系的骨架。

#### 4.3.2 核心流程

```text
word_diff_ranges(old_text, new_text, options):
  1. tokenize(old_text, scope) → 旧文本的词 token 序列
     tokenize(new_text, scope) → 新文本的词 token 序列
     切分规则：字符按 CharClassifier 分为 Word/Whitespace/Punctuation 三类，
     类别变化（或标点字符变化）处切开，如 "one two.5" → ["one", " ", "two", ".", "5"]
  2. InternedInput::default() + update_before/update_after 装载两侧 token
  3. Diff::compute(Histogram) + 逐 hunk 回调：
     diff_internal 把每个 token hunk 换算成两侧的字节区间
  4. 回调里收集非空区间，并把「相邻/相接」的区间合并成一个
  5. 返回 (old_ranges, new_ranges)，均为相对各自文本开头的字节偏移
```

区间合并规则值得注意：`last.end >= next.start` 就扩展 `last.end`——**相接即合并**（`>=` 而非 `>`）。例如 `"foo"` 改 `"bar"` 且紧随的 `","` 也变了，两个 token 区间 `0..3`、`3..4` 会合并成 `0..4`，渲染成一个连续高亮块。

#### 4.3.3 源码精读

[text_diff.rs:L181-L216](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/language/src/text_diff.rs#L181-L216) 这段是函数本体：先装载两侧 token，再跑 `diff_internal`，在回调里分别向 `old_ranges`/`new_ranges` 收集并合并非空区间。注意它**只使用** `options.language_scope`——`max_word_diff_len`、`max_word_diff_line_count` 这两个字段对它不存在，行数上限的守门人是调用方 buffer_diff（4.2）。

[text_diff.rs:L337-L373](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/language/src/text_diff.rs#L337-L373) 这段 `diff_internal` 把 imara-diff 的 token 区间换算成字节区间：跳过 hunk 之间的相同 token 时累计 `old_offset`/`new_offset`，hunk 内用 `token_len` 求和得出长度，得到 `old_offset..old_offset+len` 与 `new_offset..new_offset+len`。它与 u3-l2 的 `compute_line_offsets` 解决同一类问题（token 序列 ↔ 字节偏移），只是这里在遍历中在线累计，那边预先建表。

[text_diff.rs:L383-L411](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/language/src/text_diff.rs#L383-L411) 这段 `tokenize` 是切词器：用 `CharClassifier` 给每个字符分类，类别变化处（或同为标点但字符不同，如 `.` 和 `,`）切开。分类依据见 [buffer.rs:L579-L605](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/language/src/buffer.rs#L579-L605)：字符分 `Whitespace`/`Punctuation`/`Word` 三类；并且 `tokenize` 传的是 `CharScopeContext::Completion`——词的边界与**补全**的分词规则一致，例如 Tailwind 类名 `bg-yellow-100`、导入路径 `foo.ts` 里的 `-`/`.` 在相应语言里被当作词内字符。这就是 `build_diff_options` 要传 `language_scope` 的原因：同一个字符串，不同语言切出的词不一样。

对照一个容易混淆的近亲：[text_diff.rs:L322-L335](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/language/src/text_diff.rs#L322-L335) 这段 `should_perform_word_diff_within_hunk` 是 language crate 自己 `text_diff_with_options` 家族的触发条件——两侧字节长度各 ≤ `max_word_diff_len`（默认 512，[text_diff.rs:L6-L7](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/language/src/text_diff.rs#L6-L7)）且行数各 ≤ `max_word_diff_line_count`（language 侧默认 8）。buffer_diff **不用**这个函数，它自己在 `process_change` 里写了一份等价物（只查行数、不查字节数），并把上限换成自己的常量 5。同一个 crate 家族里存在两份相似而不相同的守门逻辑，读码时要小心区分。

[text_diff.rs:L237-L251](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/language/src/text_diff.rs#L237-L251) 这段是 `DiffOptions` 及其默认值。buffer_diff 构造它时显式覆盖 `max_word_diff_line_count = 5`，但 `max_word_diff_len` 保留默认 512——如前所述，这个字段在 `word_diff_ranges` 路径上实际不参与判断。

#### 4.3.4 代码实践

**实践目标**：绕开 buffer 层，直接调用 `word_diff_ranges` 感知它的输入输出与合并行为。`word_diff_ranges` 与 `DiffOptions` 已在 buffer_diff.rs 顶部导入，测试模块经 `use super::*` 可直接使用。

**操作步骤**：在 `mod tests` 内新增（示例代码，非项目原有）：

```rust
#[test]
fn test_word_diff_ranges_direct() {
    let (old, new) = word_diff_ranges(
        "one two three",
        "one twos three",
        DiffOptions::default(),
    );
    assert_eq!(old, vec![4..7]); // "two"
    assert_eq!(new, vec![4..8]); // "twos"
}
```

运行：`cargo test -p buffer_diff test_word_diff_ranges_direct`。

**需要观察的现象**：两侧各返回一个区间；旧区间是 `two` 的 `4..7`，新区间是 `twos` 的 `4..8`——两侧区间长度可以不同，且都是**相对各自文本**的偏移。

**预期结果**：通过（切词为 `["one"," ","two"," ","three"]` 对 `["one"," ","twos"," ","three"]`，仅第三个 token 变动；待本地验证）。可以再把文本换成 `"one two."` 对 `"one two!"` 观察 `.`/`!` 被切成独立 token 后的结果，加深对切词规则的理解。

#### 4.3.5 小练习与答案

**练习 1**：`word_diff_ranges("a-b", "a c", None scope)` 在「`-` 算标点」的语言里，两侧 token 各是什么？

**答案**：旧侧 `["a", "-", "b"]`，新侧 `["a", " ", "c"]`。diff 结果：`-`→` `（都是 1 字节，区间 `1..2`），`b`→`c`（区间 `2..3`）；两区间相接（`1..2` 与 `2..3`），触发合并，最终两侧各返回一个区间 `1..3`。

**练习 2**：为什么不直接对整份 base/buffer 文本做一次 `word_diff_ranges`，而是逐 hunk 调用？

**答案**：hunk 之外的内容两侧完全相同，对它们做词级 diff 是纯浪费；逐 hunk 调用把计算限制在真正变化的小片段上，也让「行数 ≤ 5」的上限有了明确的分母（每个 hunk 各自查自己的行数）。同时 word diff 结果存在 hunk 上，与 hunk 的生命周期（缓存、比较、失效）天然对齐。

### 4.4 消费端视角：两种表示如何在渲染前汇合

#### 4.4.1 概念说明

buffer_diff 只生产 `base_word_diffs`（相对偏移）和 `buffer_word_diffs`（锚点），真正画高亮的是编辑器。理解消费端能回答两个「为什么」：为什么 base 侧存相对偏移是安全的？为什么逐 hunk 存而不是全局存？

multi_buffer 在把单 buffer 的 hunk 汇总成多 buffer 视图时，会把两种表示**换算成同一种坐标**（multibuffer 偏移）：buffer 侧锚点直接解析；base 侧偏移则加上 hunk 起点偏移后，在**基准文本的快照上铸锚**再解析——因为编辑器展示已删除行时，那些行实际渲染的是 base 文本的内容。

#### 4.4.2 核心流程

```text
multi_buffer 汇总 hunk:
  word_diffs = []
  if 显示已删除行（show_deleted_hunks || 反转 diff）:
      hunk_start = base 文本上 hunk 起点的锚 → multibuffer 偏移
      word_diffs += base_word_diffs 的每个区间 + hunk_start
  if 正向 diff:
      word_diffs += buffer_word_diffs 的锚点 → multibuffer 偏移
editor 渲染:
  只保留与视口相交的 word_diffs，且只画在 status.is_modified() 的未折叠 hunk 上
```

#### 4.4.3 源码精读

[multi_buffer.rs:L3488-L3515](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/multi_buffer/src/multi_buffer.rs#L3488-L3515) 这段代码先判断 hunk 是否带 word diff，然后分两路汇合：`base_word_diffs` 一路（L3505-L3507）把相对偏移加上 hunk 在 base 文本上的起点偏移，换算成 multibuffer 偏移；`buffer_word_diffs` 一路（L3510-L3514）把锚点直接解析成 multibuffer 偏移。这正是 4.2 里「参照系转换留给消费端」的兑现处——buffer_diff 存相对偏移，multi_buffer 负责加基址。

[element.rs:L4596-L4615](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/editor/src/element.rs#L4596-L4615) 这段是渲染前的最后过滤：只取**与视口相交**的 word diff（源码注释解释了原因——一个 hunk 存着它整个范围的 word diff，大 hunk 只滚出一小截时不该为全长付代价），且仅当 hunk `status.is_modified()`（L4605）才取用——又一个「Added/Deleted hunk 无词级高亮」的下游印证。

#### 4.4.4 代码实践

**实践目标**（源码阅读型）：沿着「生产 → 汇合 → 过滤」读一遍调用链，画出坐标系的转换次数。

**操作步骤**：

1. 从 [buffer_diff.rs:L1297-L1330](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1297-L1330)（生产，buffer 偏移 → 锚点）读到 [multi_buffer.rs:L3488-L3515](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/multi_buffer/src/multi_buffer.rs#L3488-L3515)（汇合，base 相对偏移 + 基址 → multibuffer 偏移）再到 [element.rs:L4596-L4615](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/editor/src/element.rs#L4596-L4615)（过滤，multibuffer 偏移 → 显示坐标）。
2. 用一张三行的表记录每层看到的坐标系：层 / 输入坐标 / 输出坐标。

**需要观察的现象**：同一段「改动的词」在整个链路上至少经历 4 次坐标表示：词级 diff 的相对偏移 → buffer 绝对偏移 → 锚点 → multibuffer 偏移 → 显示坐标。

**预期结果**：能默画出这张表，并指出每一步换算由谁负责（word_diff_ranges / HunkSink / multi_buffer / editor）。无需运行代码。

#### 4.4.5 小练习与答案

**练习 1**：base 侧相对偏移的消费时机是「渲染时再加基址」。如果 hunk 的 `diff_base_byte_range` 因为 base 文本更新而整体平移了，`base_word_diffs` 里的旧偏移还有效吗？

**答案**：要看场景。`base_word_diffs` 是随整棵 hunk 树一起算出来的：base 文本变化会走 `set_base_text` 的重算流程生成全新的 hunk（含全新 word diff），旧的连同 hunk 一起被替换，不存在「旧偏移配新基址」的错配。这正是「偏移与 hunk 同生命周期」设计的自洽之处。

**练习 2**：编辑器为什么在 element.rs 里再按视口过滤一次，而不是让 buffer_diff 只返回视口内的 word diff？

**答案**：buffer_diff 是通用库，不知道任何视口；它的快照要服务 gutter、git 面板、stage 操作等多种消费者。把「视口」这种 UI 概念留在 UI 层过滤，是职责边界而非性能疏忽（源码注释同时说明了过滤的动机是避免大 hunk 全长开销）。

## 5. 综合实践

把本讲三个知识点串成一个测试：**等行数触发、超限关闭、设置开关的路径追踪**。在 `mod tests` 内新增（示例代码，非项目原有）：

```rust
#[gpui::test]
async fn test_word_diff_trigger_conditions(cx: &mut gpui::TestAppContext) {
    // 场景 A：3 行等行数修改 → word diff 非空，且能对上改动的单词位置
    let buffer = Buffer::new(
        ReplicaId::LOCAL,
        BufferId::new(1).unwrap(),
        "ONE\nTWO\nTHREE\n",
    );
    let diff = BufferDiffSnapshot::new_sync(&buffer, "one\ntwo\nthree\n".to_string(), cx);
    let hunks = diff.hunks(&buffer).collect::<Vec<_>>();
    assert_eq!(hunks.len(), 1);
    let hunk = &hunks[0];

    // base 侧：相对 hunk base 切片开头的偏移（"one\ntwo\nthree\n"）
    assert_eq!(hunk.base_word_diffs, vec![0..3, 4..7, 8..13]);
    // buffer 侧：锚点解析回偏移，应与 base 侧同形
    let buffer_offsets: Vec<Range<usize>> = hunk
        .buffer_word_diffs
        .iter()
        .map(|r| r.start.to_offset(&buffer)..r.end.to_offset(&buffer))
        .collect();
    assert_eq!(buffer_offsets, vec![0..3, 4..7, 8..13]);

    // 场景 B：6 行等行数修改 → 超过 MAX_WORD_DIFF_LINE_COUNT(5)，word diff 为空
    let buffer = Buffer::new(
        ReplicaId::LOCAL,
        BufferId::new(2).unwrap(),
        "ONE\nTWO\nTHREE\nFOUR\nFIVE\nSIX\n",
    );
    let diff = BufferDiffSnapshot::new_sync(&buffer, "one\ntwo\nthree\nfour\nfive\nsix\n".to_string(), cx);
    let hunks = diff.hunks(&buffer).collect::<Vec<_>>();
    assert_eq!(hunks.len(), 1);
    assert!(hunks[0].base_word_diffs.is_empty());
    assert!(hunks[0].buffer_word_diffs.is_empty());
}
```

操作步骤与验证要点：

1. **推演**：场景 A 中 hunk 是 `before 0..3 / after 0..3` 的单个修改 hunk。base 切片为 `"one\ntwo\nthree\n"`，按「Word/Whitespace 切词」得到 `one`、`\n`、`two`、`\n`、`three`、`\n` 六个 token，三个词各自被替换且互不相接，故两侧都是 `[0..3, 4..7, 8..13]`（`one` 是 0..3，换行占第 3 字节，`two` 是 4..7，依此类推）。
2. **运行**：`cargo test -p buffer_diff test_word_diff_trigger_conditions`。预期通过（待本地验证）；若 `base_word_diffs` 断言失败，用 `--nocapture` 配合打印实际区间，检查自己对切词规则的理解（例如是否把换行并进了词）。
3. **扩展 1（行数边界）**：把场景 B 改成 5 行改 5 行，断言 word diff **非空**——验证上限是「≥ base 行数」即 5 行仍触发。
4. **扩展 2（设置关闭的路径追踪，阅读型）**：在测试里无法直接关闭 `word_diff_enabled`（4.1 讲过，本 crate 测试没有 `SettingsStore` 时永远走默认开启分支）。请改为在源码上追踪 `None` 路径并写下每一步：`build_diff_options` 中 `.word_diff_enabled.then_some(...)` 返回 `None` → `update_diff` 把 `diff_options = None` 移入后台任务（[buffer_diff.rs:L1970-L1981](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1970-L1981)）→ `HunkSink.diff_options` 为 `None` → [buffer_diff.rs:L1297-L1301](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L1297-L1301) 的 `if let Some(...)` 第一个条件即失败 → 所有 hunk 的两侧 word diff 均为空 `Vec`（L1328-L1330）。结论：关闭设置时不是「算完再丢弃」，而是**从头就不算**——一次词级 diff 都不会执行。

## 6. 本讲小结

- word diff 的触发条件是四条与：`diff_options` 为 `Some`（`word_diff_enabled` 开启）、buffer 侧非空、base 与 buffer 侧行数相等、行数 ≤ `MAX_WORD_DIFF_LINE_COUNT = 5`；纯新增、纯删除、不等行数、大 hunk 一律不算。
- `build_diff_options` 是设置翻译层：生产路径读 `LanguageSettings::resolve(...).word_diff_enabled`，`false` 时经 `then_some` 变成 `None`，整个 word diff 被「整体跳过」；测试路径（无 `SettingsStore`）恒返回开启、上限 5 的默认选项。
- 两种表示源于两侧的身份系统差异：`buffer_word_diffs` 存锚点（buffer 会被继续编辑，用 `anchor_after` 铸锚）；`base_word_diffs` 存**相对 hunk base 切片开头**的字节偏移（base 只读不变，偏移不过期，加基址留给消费端）。
- `word_diff_ranges` 的算法是「换 token 粒度」：`tokenize` 按字符类别（Word/Whitespace/Punctuation，规则同补全分词、随 `language_scope` 而变）切词，imara-diff 二次计算，`diff_internal` 换算字节区间，相邻区间「相接即合并」。
- word diff 参与新旧 hunk 的 `PartialEq`：即使 hunk 行范围不变、仅词差异变化，`compare_hunks` 也会判定 diff 变化并发出精确到该行的 `DiffChanged`。
- 下游 multi_buffer 把 base 侧相对偏移加基址铸锚、buffer 侧锚点直接解析，统一成 multibuffer 偏移；editor 渲染时只画在 `is_modified()` 的 hunk 上并按视口过滤。

## 7. 下一步学习建议

本讲补完了 `process_change` 的最后一块，diff 计算链路（imara-diff → HunkSink → SumTree）至此全部讲完。下一讲 **u3-l4《BufferDiff 实体生命周期与异步更新》**转向实体层：`update_diff` 返回的 `Task<BufferDiffUpdate>` 如何被 `set_snapshot` 写回、`set_base_text` 的四步异步流程与版本检查、以及 `BaseTextChanged`/`DiffChanged` 两个事件的触发时机——本讲提到的「word diff 变化会触发 DiffChanged」将在那里得到完整机制解释。继续之前建议：

1. 重跑一遍 `cargo test -p buffer_diff`，确认对本讲的修改（如新增测试）没有破坏既有用例。
2. 精读 [buffer_diff.rs:L3162-L3252](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/buffer_diff/src/buffer_diff.rs#L3162-L3252) 的 `test_buffer_diff_compare`，体会「编辑一个字符也足以让 compare_hunks 报告变化」。
3. 若想继续深挖词级 diff，可以读 language crate 中 `text_diff.rs` 的 `text_diff_with_options`（[text_diff.rs:L255-L308](https://github.com/zed-industries/zed/blob/4c7244790a075e862eeb4e5ccc12d6c8f5da6f7e/crates/language/src/text_diff.rs#L255-L308)），对比它与 `word_diff_ranges` 在「先按行、hunk 内再按词」两段式结构上的异同。
