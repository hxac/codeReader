# 空格折叠 spaces.rs

## 1. 本讲目标

本讲是进阶单元（u2）的最后一篇，专门拆解 `crates/typst-realize/src/spaces.rs` 这个只有 122 行、却贯穿整条具现化流水线的小模块：**空格折叠（space collapsing）**。

前面几讲里，`collapse_spaces` 反复以「配角」身份出现——`finish_par` 折叠完空格才 `repack`（u2-l8），`finish` 的 Fragment 行内回退路径要折叠（u2-l8），正则命中后 `visit_textual` 也要折叠（u2-l10）。本讲把它从配角请到舞台中央。

学完后你应该能够：

- 说清 `SpaceState` 四态（`Invisible`/`Destructive`/`Supportive`/`Space`）各自的语义，以及「空格只在两个支撑元素之间存活、相邻空格合并为第一个空格的样式」这条核心规则的来源。
- 逐步模拟 `collapse_spaces` 的「读头 `i` / 写头 `cursor`」原地左移算法，解释为什么可以用 `copy_within` 就地完成而**不必分配新缓冲**。
- 区分 `collapse_state` 与 `collapse_state_textual` 两个判定函数的适用场景、返回类型差异，以及为什么后者会在遇到非文本元素时 `panic`。
- 能够在本机用日志验证：段首/段尾空格被丢弃、相邻空格被合并、换行符两侧的空格被「吃掉」。

## 2. 前置知识

本讲假设你已学过 **u2-l8（段落分组与 ParElem 构建）**（这是本讲的直接依赖）以及 **u2-l10（文本分组与正则 show 规则）**。用最短的话回顾必要概念：

- **Pair 与 sink**：`Pair<'a> = (&'a Content, StyleChain<'a>)`，realize 的产物是 `Vec<Pair>`；分组在 `s.sink[start..]` 这段切片上就地暂存元素，`start` 是分组在 sink 中的起始下标。本讲所有操作都发生在这段切片上。
- **finish_par 四步**（u2-l8）：`collapse_spaces` 折叠空格 → `select_span` 取 span → `repack` 打包 → `end()` 截断后 `visit(ParElem, trunk)`。本讲要补全的就是其中的第一步。
- **TEXTUAL 分组与正则冷路径**（u2-l10）：`find_regex_match_in_elems` 在「拼字符串找正则匹配」时，会顺带做一遍空格折叠（用 `collapse_state_textual`），避免每次都单独跑一遍 `collapse_spaces`；只有真正命中匹配的冷路径（`visit_textual`）才会回过头调 `collapse_spaces`。
- **空格元素**：源码里的空白（缩进、连续空格、换行）在评估后大多变成 `SpaceElem`（普通空格）或 `LinebreakElem`（强制行内换行 `\
`），它们都是带样式的元素，会进 sink。

**一个直觉性的「为什么」先点出来**：在排版里，源文本的空白不应该原样出现。一段被缩进的源码、用户随手敲的两个空格、行尾的换行——如果统统保留，输出就会满是大洞。所以 Typst（和 HTML/CSS 一样）需要一套「空格折叠」规则：**连续空白合并成一个空格、段落边缘的空白被裁掉、紧挨着「破坏性」边界（如换行）的空白被吃掉**。本讲讲的，就是这条规则在 realize 阶段的实现——它发生在排版之前，把 sink 里的 `SpaceElem` 数量精简到位。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `crates/typst-realize/src/spaces.rs` | 全部核心逻辑：`SpaceState` 四态枚举、`collapse_spaces` 原地左移算法、`collapse_state`（通用判定）、`collapse_state_textual`（正则期判定） |
| `crates/typst-realize/src/lib.rs` | `collapse_spaces` 与 `collapse_state_textual` 的所有调用点：`finish_par`、`finish`（Fragment 回退 + Par/Math 顶层）、`visit_textual`、`find_regex_match_in_elems` |

> 提醒：`collapse_state` 是 `pub(crate)` 但**只在本模块内被 `collapse_spaces` 使用**，`lib.rs` 的 `use` 语句并没有把它引进来（见 [crates/typst-realize/src/lib.rs:L39](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L39) 只导入了 `SpaceState, collapse_spaces, collapse_state_textual`）。它是一个「内部实现细节」，而 `collapse_state_textual` 才是被 `lib.rs` 跨模块调用的那个。

## 4. 核心概念与源码讲解

### 4.1 SpaceState：空格折叠的四态状态机

#### 4.1.1 概念说明

`collapse_spaces` 在遍历一段切片时，需要为「当前这个元素如何影响空格」做一个分类。这个分类只有四种取值，就是 `SpaceState`：

[crates/typst-realize/src/spaces.rs:L10-L21](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/spaces.rs#L10-L21) —— 四个变体各自带一句文档注释。

用直白的话翻译这四态：

| 状态 | 含义 | 典型元素 | 对空格的影响 |
| --- | --- | --- | --- |
| `Invisible` | 透明，不参与折叠 | `TagElem`（内省标签）、定宽非弱的 `HElem`（`#h(10pt)`） | 状态机**保持原状态**，元素自身照抄进结果 |
| `Destructive` | 破坏性，吃掉两侧空格 | `LinebreakElem`（`\
`）、分数/弱的 `HElem`（`1fr`、weak）、会折叠空白的 HTML 标签 | 若之前留着尾随空格，**先把那个空格删掉**，再把状态置为 `Destructive` |
| `Supportive` | 支撑性，普通内容 | `TextElem`、行内 box、普通行内元素 | 状态置为 `Supportive`；空格紧挨着它才「有资格」存活 |
| `Space` | 一个空格元素 | `SpaceElem` | 见下文核心规则 |

整条算法的灵魂是这一句（文档注释里写的）：

> **Spaces are only kept if supported on both sides. Adjacent spaces collapse as one with the styles of the first space.**

翻译成三条可执行的规则：

1. 一个 `SpaceElem` 只有在「前一个有效状态是 `Supportive`」时才会被保留；否则直接丢弃（`continue`，不抄进结果）。
2. 连续多个 `SpaceElem` 中，只有第一个被保留，后续的都丢弃——而保留的那个带着**它自己**的样式（即「第一个空格的样式」）。
3. 一旦遇到 `Destructive` 元素、或到达切片末尾，若此前保留了一个尾随空格，就把这个空格**删掉**（左移覆盖）。

为什么强调「第一个空格的样式」？因为 `SpaceElem` 和别的元素一样，各自背着一条 `StyleChain`（比如它可能处在某个红色字体的样式作用域里）。当多个空格合并成一个时，必须确定这一个空格用谁的样式——Typst 选择**保留先出现的那个空格的样式链**，这样语义最稳定、也最容易和后续 `repack` 的 trunk 提取对齐。

#### 4.1.2 核心流程

把状态机画成一张「转移表」。行是「处理完上一个元素后的运行状态 `state`」，列是「当前元素的分类」，单元格是「执行的动作 + 进入的新状态」：

| 当前元素 ↓ ＼ state → | Destructive（含初值） | Supportive | Space |
| --- | --- | --- | --- |
| `Invisible` | 不变 / 照抄 | 不变 / 照抄 | 不变 / 照抄 |
| `Destructive` | 删尾空格(若有)→Destructive / 照抄 | 删尾空格→Destructive / 照抄 | 删尾空格→Destructive / 照抄 |
| `Supportive` | →Supportive / 照抄 | →Supportive / 照抄 | →Supportive / 照抄 |
| `Space` | 丢弃（continue） | 记 `prev_space`→Space / 照抄 | 丢弃（continue） |

> 注：「删尾空格」只在 `state == Space` 时才真正触发左移；`Space` 行的「丢弃」用 `continue` 跳过抄写。

读这张表的关键结论：

- **初值是 `Destructive`**——所以切片最开头的空格（段首空格）一定被丢弃，因为「开头」被当作「前面有一个破坏性元素」。
- **`Supportive` 是唯一能让空格存活的「左支撑」**；空格自身（`Space` 态）不能当左支撑，所以相邻空格里只有第一个活下来。
- **`Destructive` 既吃左空格也吃右空格**：吃左空格靠「删尾空格」，吃右空格靠「下一个空格遇到 Destructive 态被丢弃」。

#### 4.1.3 源码精读

枚举定义本身很简单，已在 4.1.1 引用（[spaces.rs:L10-L21](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/spaces.rs#L10-L21)）。它派生了 `Debug, Copy, Clone, PartialEq, Eq`——`Copy` + `PartialEq` 是状态机能高效运转的前提：状态是一个可在寄存器里传递、可 `==` 比较的小值。

真正把这四态用起来的，是下面两个模块：4.2 的 `collapse_spaces`（驱动状态机）和 4.3 的两个判定函数（决定每个元素属于哪一态）。

#### 4.1.4 代码实践

**实践目标**：通过阅读 + 手动模拟，确认你理解了「初值 Destructive 丢段首空格、相邻空格只留第一个、尾随空格被裁」这三条规则。

**操作步骤**：

1. 打开 [crates/typst-realize/src/spaces.rs:L30-L75](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/spaces.rs#L30-L75)，找到第 33 行 `let mut state = SpaceState::Destructive;`。
2. 取一段切片内容（示例数据）：`[Space, Space, Text("Hi"), Space, Space, Text("!"), Space]`（对应源文本大致是 `"  Hi  !  "`）。
3. 用 4.1.2 的转移表，在纸上逐个元素推进 `state`、`cursor`，预测最终保留哪些元素。
4. 把你的预测和 4.2.2 的完整跟踪对照。

**需要观察的现象**：最终结果应当是 `[Text("Hi"), Space, Text("!")]`——即 `"Hi !"`：两个前导空格没了、中间两个空格合并成一个（带第一个空格的样式）、两个尾随空格没了。

**预期结果**：手动模拟应得到三元素结果。如果你算出多于或少于三个，回头检查「`Space` 行在 state 为 `Space` 时是 continue（丢弃）」这一格。具体落盘验证见 4.2.4。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `SpaceState` 的初值设成 `Destructive` 而不是 `Supportive`？

> **答案**：为了让切片**起始处**的空格被丢弃。如果初值是 `Supportive`，段首第一个空格就会被当成「有左支撑」而保留下来，导致段落开头多出一个空格。设成 `Destructive` 等价于「假装位置 0 之前有一个破坏性元素」，于是段首空格遇到非 `Supportive` 态直接 `continue` 丢弃——和「段尾裁剪」对称。

**练习 2**：`Invisible` 和 `Supportive` 都会让元素「照抄进结果」，它们的区别在哪里？给一个会暴露这个区别的例子。

> **答案**：区别在于**是否改变运行状态**。`Supportive` 会把状态置为 `Supportive`（从而为后续空格提供「左支撑」）；`Invisible` 让状态**保持不变**。例：`[Text, Space, HElem(定宽), Space, Text]`。中间的定宽 `HElem` 是 `Invisible`，所以第二个 `Space` 看到的状态仍是 `Space`（被丢弃），结果是 `[Text, Space, HElem, Text]`——空格只在 `Text` 与 `HElem` 之间留了一个。若 `HElem` 被误判成 `Supportive`，第二个空格就会存活，变成两个空格。

---

### 4.2 collapse_spaces 的原地左移算法

#### 4.2.1 概念说明

`collapse_spaces(buf, start)` 要在 `buf[start..]` 这段切片上「删掉一些空格、合并一些空格」。最朴素的写法是：建一个新 `Vec`，把要保留的元素 push 进去，最后替换回去。但 Typst 没有这么干，而是**就地（in-place）左移**。

核心不变量是：

> **任意时刻都有 `cursor <= i`。**

- `i` 是「读头」：当前正在审视的原始元素下标（`for i in start..buf.len()`）。
- `cursor` 是「写头」：下一个该写入结果的位置。

因为我们**只删不增**（要么丢弃一个空格、要么把一段左移覆盖一个空格），写头永远不会超过读头。既然 `cursor <= i` 恒成立，那么把 `buf[i]` 抄到 `buf[cursor]` 就绝不会破坏尚未读取的数据——这就是「原地」得以成立的原因。

「左移」发生在需要删掉一个**已经抄进结果的**尾随空格时：这个空格位于 `prev_space`，它后面还跟着 `cursor - prev_space - 1` 个已抄好的元素。把这批元素整体左移一格去覆盖那个空格即可。Rust 的 `Vec::copy_within(src, dest)` 正是一次 `memmove`，完美贴合这个需求：

```rust
buf.copy_within(prev_space + 1..cursor, prev_space);
cursor -= 1;
```

为什么要用 `copy_within` 而不是 `Vec::remove` 或手写循环？

- `Vec::remove(i)` 会把 `i` 之后**所有**元素左移一格——但在状态机里，空格被删时它后面常常还有很多元素，`remove` 的语义虽对，却把「左移」和「状态推进」耦合在一起，且每次都从 `prev_space` 移到末尾，范围比需要的大。
- `copy_within` 精确地只移动 `[prev_space+1 .. cursor)` 这一段到 `prev_space`，是一次显式的 `memmove`，意图清晰、范围最小。
- 关键还有：`s.sink` 是分组机制共享的缓冲，分组靠 `start` 下标切片访问（见 u2-l7 的 `Grouped`）。**就地修改 sink** 意味着所有 `start` 下标始终有效，不需要重接线；若另开新 `Vec` 再赋值回去，还要操心 `groupings` 栈里记录的下标。

#### 4.2.2 核心流程

先用伪代码概括 `collapse_spaces` 的一次扫描：

```
fn collapse_spaces(buf, start):
    cursor = start
    prev_space = start          # 最近一次保留的空格落在哪个写位置
    state = Destructive         # 初值：丢段首空格
    for i in start..buf.len():
        分类 = collapse_state(buf[i])
        match 分类:
            Invisible => # state 不变，照抄
            Destructive =>
                if state == Space: 左移删掉 prev_space 处的空格; cursor -= 1
                state = Destructive
            Supportive => state = Supportive
            Space =>
                if state != Supportive: continue   # 丢弃这个空格，不抄
                prev_space = cursor; state = Space
        # 照抄当前元素到写头（Space 被丢弃时已 continue，不会走到这）
        if cursor < i: buf[cursor] = buf[i]
        cursor += 1
    if state == Space:          # 末尾还挂着尾随空格
        左移删掉 prev_space 处的空格; cursor -= 1
    buf.truncate(cursor)        # 删掉左移后留下的「空洞」
```

下面用一个**完整跟踪**验证。输入切片（对应文本 `"  Hi  !  "`，`S`=Space、`T("Hi")`=Text）：

`[S, S, T("Hi"), S, S, T("!"), S, S]`，`start=0`。初值 `cursor=0, prev_space=0, state=Destructive`。

| 步骤 | i | 元素 | 分类 | 状态转移 | 动作 | cursor |
| --- | --- | --- | --- | --- | --- | --- |
| 0 | 0 | S | Space | Destructive≠Supportive → 丢弃 | continue | 0 |
| 1 | 1 | S | Space | 同上 → 丢弃 | continue | 0 |
| 2 | 2 | T("Hi") | Supportive | →Supportive | `buf[0]=buf[2]` | 1 |
| 3 | 3 | S | Space | Supportive→Space | `prev_space=1`; `buf[1]=buf[3]` | 2 |
| 4 | 4 | S | Space | Space≠Supportive → 丢弃 | continue | 2 |
| 5 | 5 | T("!") | Supportive | →Supportive | `buf[2]=buf[5]` | 3 |
| 6 | 6 | S | Space | Supportive→Space | `prev_space=3`; `buf[3]=buf[6]` | 4 |
| 7 | 7 | S | Space | Space≠Supportive → 丢弃 | continue | 4 |
| 末 | — | — | — | state==Space | `copy_within(4..4,3)` 空; `cursor=3` | 3 |

`truncate(3)`。最终 `buf[0..3] = [T("Hi"), S(来自 i=3, 第一个中间空格, 带它的样式), T("!")]`，即 `"Hi !"`。完全符合预期：前导丢、中间合并成一个（保留第一个空格样式）、尾随丢。

再看一个体现 `Destructive` 「吃左侧空格」的例子。输入 `[T("A"), S, Linebreak, T("B")]`（`Linebreak` 是破坏性的）：

| 步骤 | i | 元素 | 分类 | 状态转移 | 动作 | cursor |
| --- | --- | --- | --- | --- | --- | --- |
| 0 | 0 | T("A") | Supportive | →Supportive | `cursor<i` 否; | 1 |
| 1 | 1 | S | Space | Supportive→Space | `prev_space=1`; | 2 |
| 2 | 2 | Linebreak | Destructive | state==Space→删 | `copy_within(2..2,1)` 空; `cursor=1`; `buf[1]=buf[2]` | 2 |
| 3 | 3 | T("B") | Supportive | →Supportive | `buf[2]=buf[3]` | 3 |

`truncate(3)`。结果 `[T("A"), Linebreak, T("B")]`——换行符**前面**的空格被删掉了。（换行符**后面**的空格同理会被吃：下一个空格遇到 `Destructive` 态直接 continue 丢弃。）

#### 4.2.3 源码精读

完整函数：

[crates/typst-realize/src/spaces.rs:L23-L75](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/spaces.rs#L23-L75) —— 文档注释（L23-L29）讲清了三件事：丢弃边缘/破坏性附近的空格、把相邻空格合并成第一个空格的样式、用原地左移实现。

逐段对照：

- **L30-L33 初始化**：`cursor=start`、`prev_space=start`、`state=Destructive`。注意 `prev_space` 初值取 `start` 只是一个「无副作用」的占位——真正赋值发生在第一个被保留的空格处。
- **L39-L59 状态机循环**：四个 match 臂与 4.1.2 的转移表一一对应。重点看两处：
  - L44-L48 `Destructive` 臂里的 `copy_within(prev_space + 1..cursor, prev_space)` + `cursor -= 1`——这就是「删尾空格」的左移。
  - L52-L58 `Space` 臂：`if state != SpaceState::Supportive { continue; }` 决定丢弃；否则记下 `prev_space = cursor`。注意它**不 `continue`**，会落到 L62 的照抄逻辑，把空格抄进结果。
- **L61-L65 照抄逻辑**：`if cursor < i { buf[cursor] = buf[i]; } cursor += 1;`。`cursor < i` 的判断是为了避免「读写同一位置」的冗余赋值（当没有发生任何丢弃时 `cursor == i`，跳过即可）。
- **L68-L71 末尾裁剪**：循环结束后若 `state == Space`，说明切片以一个保留的空格结尾（段尾空格），用同样的左移把它删掉。
- **L74 `buf.truncate(cursor)`**：左移在切片尾部留下了 `buf.len() - cursor` 个「空洞」元素，直接截断。

调用点一览（都在 `lib.rs`）：

- [crates/typst-realize/src/lib.rs:L1190-L1192](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L1190-L1192) —— `finish_par` 在 `repack` 之前先 `collapse_spaces(sink, start)`，把段落分组暂存区里的空格收拾干净。这是最常见的入口。
- [crates/typst-realize/src/lib.rs:L792-L798](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L792-L798) —— `finish` 的 Fragment 行内回退路径（`is_fully_inline_or_neutral` 命中，放弃生成 `ParElem`）：`collapse_spaces(&mut s.sink, 0)`。
- [crates/typst-realize/src/lib.rs:L804-L807](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L804-L807) —— Par / Math 两种 kind 的顶层：realize 完成后顶层空格直接折叠。
- [crates/typst-realize/src/lib.rs:L1246-L1257](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L1246-L1257) —— 正则命中的冷路径 `visit_textual`：找到匹配后才 `collapse_spaces(&mut s.sink, start)`，因为只有命中才需要真正把元素交给 `visit_regex_match` 切片。

#### 4.2.4 代码实践

**实践目标**：用一个含「连续空格 + 段首/段尾空格 + 行内换行」的段落，在 `copy_within` 处加日志，亲眼看到原地左移如何丢弃边缘空格、合并相邻空格。

**操作步骤**：

1. 在 [crates/typst-realize/src/spaces.rs:L46](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/spaces.rs#L46)（`Destructive` 臂的 `copy_within` 那一行）之前插入日志（示例代码）：

   ```rust
   eprintln!("[collapse] 删尾空格 prev_space={prev_space} cursor={cursor} (命中 Destructive)");
   buf.copy_within(prev_space + 1..cursor, prev_space);
   ```

2. 在 [spaces.rs:L69](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/spaces.rs#L69)（末尾裁剪的 `copy_within`）之前再加一条类似日志，标注「末尾裁剪」。
3. 在 [spaces.rs:L74](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/spaces.rs#L74) 的 `truncate` 前打印 `buf.len()` 与 `cursor`，看截掉了多少。
4. 准备一个最小文档 `spaces.typ`（示例代码）：

   ```typst
   #box[
      Hello   World \
      Done
   ]
   ```

   （`#box[...]` 内是 Fragment 实现；缩进产生段首空格、`Hello` 与 `World` 间三个空格、`\
` 是行内换行、换行前后各隐含空格。）

5. 用调试版编译：`cargo run -p typst -- compile spaces.typ`（具体命令以本机 workspace 为准，待本地验证）。

**需要观察的现象**：

- 段首那些由缩进产生的空格**不会**触发 `copy_within` 日志——它们在 `Space` 臂里被 `continue` 静默丢弃了（因为初值是 `Destructive`）。
- `Hello` 与 `World` 之间三个空格里，只有第一个被保留，后两个被 `continue` 丢弃。
- 换行符 `Linebreak` 命中 `Destructive` 臂时，应看到一条「删尾空格」日志（删掉换行前的空格）。
- 末尾若有尾随空格，会看到一条「末尾裁剪」日志。
- `truncate` 前打印的 `buf.len() - cursor` 就是这次折叠删掉的元素总数。

**预期结果**（待本地验证）：最终 sink 切片里 `Hello` 与 `World` 之间只剩一个 `SpaceElem`，且其样式来自三个空格里**第一个**的 `StyleChain`；换行符两侧不再有空格。具体日志行数与 `cursor` 数值依文档而定，需本地运行确认。

#### 4.2.5 小练习与答案

**练习 1**：如果 `collapse_spaces` 改成「每次需要删空格时调 `buf.remove(prev_space)`」，功能上是否等价？为什么 Typst 仍然选择 `copy_within`？

> **答案**：功能上等价（`remove` 也是左移覆盖）。但 `copy_within(prev_space+1..cursor, prev_space)` 显式地只移动 `[prev_space+1, cursor)` 这一段，意图是「把写头之前那批已抄好的元素左移一格」；`remove(prev_space)` 则会一直左移到 `buf` 末尾，移动范围更大、语义也更泛。更重要的是，`copy_within` 配合 `cursor` 这个显式写头，让「原地、`cursor<=i`」的不变量一目了然，可读性和可维护性更好。

**练习 2**：第 62 行的照抄逻辑写成 `if cursor < i { buf[cursor] = buf[i]; }`，这个 `if` 省掉会怎样？

> **答案**：功能仍正确（把元素赋值给自身是无害的），但会多做许多「自我赋值」——当没有发生任何丢弃时 `cursor == i`，此时 `buf[i] = buf[i]` 是纯冗余。加上 `if` 是一次廉价的自我赋值消除，在大段落里能省掉可观的冗余拷贝。

**练习 3**：为什么循环结束后还要单独处理一次 `if state == Space { ... }`（L68-L71）？

> **答案**：状态机只能在「遇到下一个元素」时才知道某个保留的空格该不该删——`Destructive` 臂删的是「前一个保留的尾随空格」。但切片末尾的空格后面**再没有元素**来触发删除，所以必须在循环外补一刀：若扫描完 `state` 仍是 `Space`，说明结尾挂着段尾空格，要用同样的左移把它删掉。

---

### 4.3 两个判定函数：collapse_state 与 collapse_state_textual

#### 4.3.1 概念说明

状态机需要一个「分类器」：给定一个 `(content, styles)`，告诉我它属于四态中的哪一态。`spaces.rs` 提供了**两个**分类器，长得像但用途不同。

**`collapse_state`** —— 通用分类器，只返回 `SpaceState`。它是 `collapse_spaces` 唯一的分类依据，处理**所有**可能在段落里出现的元素类型：`TagElem`、`HElem`、`LinebreakElem`、`HtmlElem`、`SpaceElem`，以及兜底的「其余一切 → `Supportive`」。因为它要兜底，所以**永远不会 panic**。

**`collapse_state_textual`** —— 文本分类器，返回 `(SpaceState, &str)`，多带一个「这个元素贡献给正则匹配的字符串」。它只在 `find_regex_match_in_elems`（u2-l10）里被调用——也就是 TEXTUAL 分组「拼字符串找正则」的时候。因为它只可能见到文本类元素，所以遇到不认识的元素会直接 `panic`（防御性断言）。

为什么要两个、而不是一个带可选字符串的通用函数？

1. **职责分离**：`collapse_spaces` 面向「修整 sink 里的元素」，根本不需要字符串；让它去算每个元素的字符串贡献是纯浪费。而 `find_regex_match_in_elems` 面向「拼一段字符串跑正则」，必须拿到字符串，但**只**会见到文本元素（TEXTUAL 分组的 `effect` 已经把非文本元素挡在门外，见 u2-l10 的 4.1）。
2. **元素集合不同**：通用分类器要处理 `HElem`、`HtmlElem` 这些「非文本但会出现在段落里」的元素；文本分类器压根不会见到它们（见到了说明调用方违约，直接 panic）。
3. **避免冷路径开销**：u2-l10 讲过，`find_regex_match_in_elems` 在「拼字符串」时**顺带**做了一遍空格折叠，这样「没有正则命中」的热路径就完全不必调 `collapse_spaces`（注释见 [lib.rs:L1264-L1267](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L1264-L1267)）。只有在命中匹配的冷路径（`visit_textual`）才会回头调一次真正的 `collapse_spaces`。两个分类器正是这种「热/冷路径分离」的体现。

#### 4.3.2 核心流程

把两个分类器对各类元素的判定并列对比（`—` 表示「该函数不会处理这种元素」）：

| 元素 | `collapse_state` | `collapse_state_textual` | 备注 |
| --- | --- | --- | --- |
| `TagElem` | `Invisible` | `(Invisible, "")` | 内省标签，两者一致：透明 |
| `SpaceElem` | `Space` | `(Space, " ")` | 空格贡献一个半角空格字符 |
| `LinebreakElem` | `Destructive` | `(Destructive, "\n")` | 两者都判破坏性；文本版贡献 `\n` 以便正则跨行匹配 |
| `TextElem` | `Supportive`（兜底） | `(Supportive, &elem.text)` | 文本版给出真实文本内容 |
| `SmartQuoteElem` | `Supportive`（兜底） | `(Supportive, "\""` 或 `"'"`） | 文本版按 `double` 字段给出引号字符 |
| `HElem`（定宽非弱） | `Invisible` | —（panic） | 仅通用版处理：透明 |
| `HElem`（分数 `1fr` 或 weak） | `Destructive` | —（panic） | 仅通用版处理：吃空格 |
| `HtmlElem`（折叠空白的标签） | `Destructive` | —（panic） | 仅通用版处理；见下方注释 |
| 其它任何元素 | `Supportive`（兜底） | **panic** | 通用版兜底；文本版视为编程错误 |

读这张表的关键结论：

- **共享元素类型（Tag/Space/Linebreak/Text/SmartQuote）在两函数里的 `SpaceState` 取值完全一致**——这是设计上的刻意对齐，保证「拼字符串时的折叠」和「真正改 sink 时的折叠」语义统一。
- **`HElem` 与 `HtmlElem` 只有通用版认识**：它们不是文本，不会进 TEXTUAL 分组，所以文本版用 panic 守门。
- **`TextElem`/`SmartQuoteElem` 在通用版里走的是兜底 `else` 分支**（返回 `Supportive`），而文本版显式处理它们以取出字符串——这是两者最直观的代码差异。
- **那个 panic 不是给用户准备的**：它断言「调用方只喂文本元素」。由于 TEXTUAL 的 `effect` 只让 `TextElem/LinebreakElem/SmartQuoteElem`（Trigger）和 `SpaceElem`（Inner）进组、加上 `TagElem`（ Invisible 直推），正常流程下文本分类器见不到别的元素；panic 只在开发者改坏调用约定时才可能触发。

#### 4.3.3 源码精读

通用分类器：

[crates/typst-realize/src/spaces.rs:L77-L100](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/spaces.rs#L77-L100) —— 注意三处：

- L81-L86 `HElem` 分支：`if elem.amount.is_fractional() || elem.weak.get(styles)` 决定 `Destructive` 否则 `Invisible`。分数间距（`1fr`）和 weak 间距会「吃」掉相邻空格；普通定宽间距（`#h(10pt)`）则透明。
- L87-L94 `LinebreakElem` 与 `HtmlElem` 分支：两者都判 `Destructive`。注意 L88-L92 的注释——之所以要把它们设为破坏性，是为了「折叠掉那些本会被保护、并以 `white-space: pre-wrap` 形式呈现成 span 的空格」。换言之，换行符和某些 HTML 标签是空格的「硬边界」。
- L95-L99 `SpaceElem` 与兜底：`SpaceElem → Space`，其余一切 `→ Supportive`。

文本分类器：

[crates/typst-realize/src/spaces.rs:L102-L122](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/spaces.rs#L102-L122) —— 注意四点：

- 返回类型是 `(SpaceState, &'a str)`，字符串借用自传入的 `content`（如 `&elem.text`），零分配。
- L109 `LinebreakElem` 返回 `(Destructive, "\n")`：换行贡献 `\n`，让 `show regex("a\nb"): ..` 这类跨行匹配成为可能。
- L113-L117 `TextElem`/`SmartQuoteElem` 返回 `(Supportive, ...)`，其中 `SmartQuoteElem` 按 `elem.double.get(styles)` 决定是 `"` 还是 `'`。
- L118-L121 `else` 分支直接 `panic!("tried to find regex match in a non-textual element: {name}")`——这是 4.3.1 说的防御性断言。

文本分类器在 `lib.rs` 里的唯一使用点：

[crates/typst-realize/src/lib.rs:L1268-L1316](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L1268-L1316) —— `find_regex_match_in_elems`。重点看 L1276-L1295：它**复制了 `collapse_spaces` 的状态机骨架**（初值同样是 `SpaceState::Destructive`、同样的四臂转移），区别只是把「抄进 sink」换成「`buf.push_str(text)` 把字符串拼进 `BumpString`」、把「删尾空格左移」换成「`buf.pop()` 弹掉末尾那个空格字符」。这段代码就是 4.3.1 所说「拼字符串时顺带折叠空格」的实现——它和 `collapse_spaces` 是同一段状态机逻辑的两个具体化（一个改 sink、一个改字符串）。

#### 4.3.4 代码实践

**实践目标**：确认 `collapse_state` 与 `collapse_state_textual` 对共享元素类型给出**相同**的 `SpaceState`，并理解后者如何把空格折叠「投影」到字符串侧。

**操作步骤**：

1. 在 [lib.rs:L1279](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L1279)（`let (new_state, text) = collapse_state_textual(content, styles);`）之后加日志（示例代码）：

   ```rust
   eprintln!("[textual] {:?} -> state={:?} text={text:?}",
       content.elem().name(), new_state);
   ```

2. 在 [lib.rs:L1282-L1283](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L1282-L1283)（`Destructive` 臂里 `if state == Space { buf.pop(); }`）处也加一条日志，看字符串侧如何「删尾空格」。
3. 准备一个带正则 show 规则的文档 `regex.typ`（示例代码），让 `find_regex_match_in_elems` 被触发：

   ```typst
   #show regex("H.+o"): it => [#it]
   Hello   World
   ```

4. 编译（命令同 4.2.4，待本地验证），观察日志。

**需要观察的现象**：

- 对每个文本元素，日志打印的 `state` 取值，应当和 4.3.2 表中 `collapse_state_textual` 列完全一致。
- 三个连续空格里，只有第一个让 `buf` 多了 `" "`，后两个因为「state 不是 Supportive」被 `continue` 跳过——和 `collapse_spaces` 里 `continue` 丢弃空格的行为**对称**。
- 当遇到 `Destructive`（如 `Linebreak`）时，若此前 `state==Space`，会看到 `buf.pop()` 把刚 push 进去的空格字符弹掉——这就是「删尾空格」的字符串版。

**预期结果**（待本地验证）：`find_regex_match_in_elems` 拼出的字符串应当是已经折叠过空格的（如 `"Hello World"` 而非 `"Hello   World"`），与 `collapse_spaces` 修整后的 sink 在「空格数量」上语义一致——这正是「只有命中匹配的冷路径才需要再调一次 `collapse_spaces`」能够成立的根基。

#### 4.3.5 小练习与答案

**练习 1**：`collapse_state` 把 `TextElem` 判为 `Supportive` 是通过兜底的 `else` 分支；`collapse_state_textual` 却为 `TextElem` 写了显式分支。为什么后者不能也走兜底？

> **答案**：因为文本分类器还必须返回「这个元素贡献的字符串」。`TextElem` 贡献的是 `&elem.text`，这必须显式取出；兜底分支无从知道该返回什么字符串。同理 `SmartQuoteElem` 也要按 `double` 字段算出 `"` 或 `'`。所以文本版凡是 `Supportive` 的元素都得显式列出以取出字符串，只有 `Invisible`/非文本元素才无法处理而 panic。

**练习 2**：`find_regex_match_in_elems` 在 L1283 用 `buf.pop()` 删尾空格，而 `collapse_spaces` 用 `copy_within`。为什么这里能用更简单的 `pop()`？

> **答案**：因为字符串侧的「空格」只占 `BumpString` 末尾**一个字符**（空格被 push 进去就是末尾的一个字节），删它只需 `pop()` 一次。而 `collapse_spaces` 操作的是 `sink` 里的 `Pair` 元素，被删的尾随空格可能**不**在末尾（它后面还跟着已抄好的元素），所以必须用 `copy_within` 把后续元素左移覆盖。两个删除操作面对的数据结构不同（末尾单字符 vs 中间一个元素），故手法不同。

**练习 3**：`collapse_state` 里把定宽非弱的 `HElem` 判为 `Invisible` 而不是 `Supportive`。结合状态机，说说这对 `Text, Space, HElem(定宽), Space, Text` 这段输入的影响。

> **答案**：`HElem` 为 `Invisible` 意味着状态机在经过它时**保持原状态**。跟踪：`Text→Supportive`、`Space→保留(state=Space, prev_space 记下)`、`HElem→Invisible(状态仍为 Space)`、第二个 `Space→state 是 Space≠Supportive → 丢弃`、`Text→Supportive`。结果只保留 `[Text, Space, HElem, Text]`，即 `HElem` 两侧只留一个空格（左侧那个）。若 `HElem` 是 `Supportive`，第二个空格也会存活，变成两个空格——这不符合「定宽间距是透明间距」的直觉。

---

## 5. 综合实践

把本讲三个模块（四态状态机、原地左移、两个判定函数）串起来，做一次「一段混合内容如何被折叠」的端到端追踪。

**任务**：阅读下面这段 `finish_par` 即将处理的切片（示例数据，`T`=TextElem、`S`=SpaceElem、`LB`=LinebreakElem、`TAG`=TagElem、`H1fr`=分数 HElem），给出 `collapse_spaces` 处理后的结果，并指出每一步对应的状态机动作与源码行号。

```
[TAG, T("A"), S, S, LB, S, T("B"), H1fr, S, T("C"), S, TAG]
```

**要求你讲清**：

1. **分类**：用 4.3 的两个表，给每个元素标出 `collapse_state` 的取值（注意 `TAG`→Invisible、`LB`→Destructive、`H1fr`→Destructive、`S`→Space、`T`→Supportive）。
2. **段首**：开头那个 `TAG` 是 `Invisible`，状态机初值是 `Destructive`——它如何「透传」而不影响后续空格判定？紧随其后的 `T("A")` 把状态扳成什么？
3. **相邻空格 + 换行**：`T("A"), S, S, LB` 这一段里，第一个 `S` 为何被保留、第二个 `S` 为何被 `continue` 丢弃？`LB` 命中 `Destructive` 臂时，触发了哪一行 `copy_within`（参考 [spaces.rs:L46](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/spaces.rs#L46)）删掉了哪个保留空格？
4. **换行后空格**：`LB` 之后那个 `S` 为何被丢弃？（提示：`LB` 把状态置成了什么？）
5. **分数间距**：`H1fr` 是 `Destructive`，它如何吃掉 `T("B")` 与 `T("C")` 之间两侧的空格？
6. **段尾**：倒数第二个 `S` 与末尾 `TAG`——末尾 `TAG` 是 `Invisible` 不会触发裁剪，那最后一个 `S` 是靠哪一段代码（参考 [spaces.rs:L68-L71](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/spaces.rs#L68-L71)）被删掉的？
7. **结果**：最终保留的元素序列是什么？其中保留的 `SpaceElem` 各自带的是「哪一个原空格」的样式？

**交付物**：一张逐元素的状态推进表（仿 4.2.2），标出每个元素的分类、状态转移、动作、`cursor` 值，并写出最终结果序列。

> 参考答案（请在自己跟踪后再对照）：最终结果为 `[TAG, T("A"), LB, T("B"), H1fr, T("C"), TAG]`——`A` 后两空格被换行吃掉、`B/C` 之间的空格被 `1fr` 吃掉、段尾空格被循环外的裁剪删掉；全程没有任何保留的 `SpaceElem` 存活，因此也没有「第一个空格的样式」需要继承。具体推进待本地验证。

## 6. 本讲小结

- `spaces.rs` 用一个四态状态机 `SpaceState`（`Invisible`/`Destructive`/`Supportive`/`Space`）实现空格折叠，核心规则是「空格只在两个支撑元素之间存活；相邻空格合并为一个，保留**第一个空格**的样式」。
- `collapse_spaces` 用「读头 `i` / 写头 `cursor`」实现**原地左移**，依靠 `cursor <= i` 的不变量安全地边读边写；删尾空格时用 `Vec::copy_within(prev_space+1..cursor, prev_space)` 做一次最小范围的 `memmove`，末尾再 `truncate` 掉空洞。
- 状态机**初值为 `Destructive`**，等价于「段首之前有一个破坏性元素」，于是段首空格自然被 `continue` 丢弃；段尾空格则由循环外的一次额外裁剪处理。
- `collapse_state`（通用，返回 `SpaceState`，兜底 `Supportive`，永不 panic）与 `collapse_state_textual`（文本，返回 `(SpaceState, &str)`，遇非文本 panic）职责分离；共享元素类型在两函数里取值一致，保证「拼字符串时的折叠」与「改 sink 时的折叠」语义统一。
- `collapse_spaces` 在 `lib.rs` 有四个调用点：`finish_par`（最常见）、`finish` 的 Fragment 行内回退、Par/Math 顶层、正则命中的冷路径 `visit_textual`；`collapse_state_textual` 仅被 `find_regex_match_in_elems` 使用，它把同一段状态机逻辑「投影」到字符串侧，使热路径免跑 `collapse_spaces`。

## 7. 下一步学习建议

- **u3-l1（标签与内省 TagElem）**：本讲里 `TagElem` 反复以「`Invisible` 透明元素」出现——它穿过状态机却又不影响空格判定。u3-l1 会讲清 `TagElem`/`Tag`/`TagFlags` 与内省系统的关系，以及 `prepare` 生成的 start/end tag 为何要被空格折叠「视而不见」。
- **u3-l2（过滤规则与边界元素）**：和本讲互为补充——`visit_filter_rules` 在非 Par/Math 的 realize 里会**整类丢弃**顶层 `SpaceElem`，而本讲讲的是「进入段落后的空格」如何被折叠。把两者合起来看，才能得到「空格在整个 realize 里去哪儿了」的完整图景。
- **u3-l3（多生命周期与 arena 内存分配）**：若你对 `find_regex_match_in_elems` 里用 `BumpString` 拼字符串、`buf.pop()`/`push_str` 的 arena 用法感兴趣，u3-l3 会系统讲解 `Arenas`（typed_arena + bumpalo）如何延长生命周期。
- 若想横向对照「排版阶段对空格的二次处理」，可在学完本讲后直接阅读 `crates/typst-layout` 的行内排版（`inline/mod.rs`）相关代码——realize 只负责「把 `SpaceElem` 数量折叠到位」，真正决定空格宽度的字形整形与断行发生在 layout。
