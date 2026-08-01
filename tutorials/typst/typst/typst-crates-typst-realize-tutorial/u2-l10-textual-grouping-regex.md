# 文本分组与正则 show 规则

## 1. 本讲目标

本讲聚焦 typst-realize 里一条非常特殊、也最容易被初学者忽略的分组路径：**TEXTUAL 分组**，以及它如何让 `show "x, ": ...`、`show regex("[a-z]+"): ...` 这类「按文本内容匹配」的 show 规则真正生效。

学完后你应该能够：

- 说清楚为什么正则/文本 show 规则**不能**走普通 show 规则那条 `verdict()` 路径，必须由 TEXTUAL 分组单独处理。
- 描述 `finish_textual` 的三种结局（命中匹配 / 退回段落 / 播种段落），并解释它为什么是 `ParElem` 的「播种者」。
- 读懂 `find_regex_match_in_elems` 如何把一组文本元素拼成一段字符串并折叠空格，以及 `find_regex_match_in_str` 如何在一段同样式文本里找到「最左、非空、未被撤销」的正则匹配。
- 读懂 `visit_regex_match` 如何把匹配文本切片、重打包、套用 recipe，并用 `Style::Revocation` 防止同一条规则在自身输出上重复触发。
- 能够在本机用日志验证上述行为，并能解释 `crates/typst-library` 里 `Recipe`、`RecipeIndex`、`Revocation`、`Selector::Regex` 与本讲的协作关系。

## 2. 前置知识

本讲假设你已经学过 u2-l2（`visit_show_rules`）、u2-l3（`verdict`）、u2-l6（`GroupingRule` 框架）和 u2-l7（分组生命周期）。下面用最简短的方式回顾必要概念：

- **show 规则与 Recipe**：用户写 `show 选择器: 变换`，编译期会生成一个 `Recipe { selector, transform, .. }`，挂到样式链上。普通元素 show 规则（`show heading: it`）在 `visit_show_rules → verdict` 里逐元素判定。
- **Selector**：选择器有多种，其中 `Selector::Regex(Regex)` 专门用于文本匹配，由 `show "字面量"` 或 `show regex("...")` 产生。
- **GroupingRule / GroupingEffect**：分组规则是静态说明书，`effect` 把每个元素归类为 `Trigger`（开团）、`Inner`（并入）、`Neutral`（中性）、`Interrupt`（中断）。
- **分组栈**：`State.groupings` 是一个深度上限为 `MAX_GROUP_NESTING = 3` 的栈，规则靠 priority 严格递增才能嵌套。TEXTUAL 的 priority 是 3（最高），PAR 是 1（最低）。
- **Pair 与 sink**：`Pair<'a> = (&'a Content, StyleChain<'a>)`，realize 的产物就是 `Vec<Pair>`，分组在 `s.sink[start..]` 上就地暂存。

一个贯穿全讲的关键事实先点出来：普通 show 规则判定用的 `Selector::matches` 对 `Regex` 选择器**直接返回 `false`**。这意味着正则 show 规则在 `verdict()` 里永远拿不到判决，必须另寻出路——这就是 TEXTUAL 分组存在的根本理由。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `crates/typst-realize/src/lib.rs` | TEXTUAL 规则定义、`finish_textual`、`visit_textual`、`find_regex_match_in_elems`、`find_regex_match_in_str`、`visit_regex_match`、`slice_textual`、`RegexMatch` 全部在此 |
| `crates/typst-realize/src/spaces.rs` | `SpaceState` 四态、`collapse_state_textual`（正则匹配期间用的空格状态判定） |
| `crates/typst-library/src/foundations/styles.rs` | `Style`（含 `Recipe`/`Revocation`）、`Recipe::apply`、`RecipeIndex`、`StyleChain::recipes`/`entries` |
| `crates/typst-library/src/foundations/selector.rs` | `Selector::Regex`、`Selector::text`/`regex`，以及 `matches` 对 `Regex` 返回 `false` |

## 4. 核心概念与源码讲解

### 4.1 TEXTUAL 分组规则与 finish_textual 的回退逻辑

#### 4.1.1 概念说明

先回答一个「为什么」：为什么文本 show 规则需要专门一条分组路径？

普通元素 show 规则在 `verdict()` 里是这样判定的：取出一个元素，遍历样式链里的 recipes，对每个 recipe 调 `Selector::matches(elem, styles)`。但 `matches` 对 `Regex` 选择器**一律返回 `false`**：

[crates/typst-library/src/foundations/selector.rs:L132-L155](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/selector.rs#L132-L155) —— 正则、`before`、`after`、`within` 在此都返回 `false`，注释写明 "Not supported here"。

这不是疏忽，而是因为正则要匹配的「文本」根本不是一个孤立元素。一段 `What's up, she` 经过评估后，往往被拆成多个元素：`TextElem("What")`、`SmartQuoteElem`、`SpaceElem`、`TextElem("up,")`、`SpaceElem`、`TextElem("she")`……如果只能在单个元素上判定，`show "up,\" she": ...` 这种**跨元素**的匹配就永远不可能成立。

所以 Typst 的做法是：先把「连续的、同一样式的」文本元素攒到一起，拼成一段字符串，再在整段字符串上跑正则。这个「攒文本」的分组就是 **TEXTUAL**。它由两种方式触发：

1. `show "字面量"`：见 [crates/typst-library/src/foundations/selector.rs:L107-L113](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/selector.rs#L107-L113)，`Selector::text` 把字符串转义后包成 `Regex`。
2. `show regex("...")`：见 [crates/typst-library/src/foundations/selector.rs:L115-L124](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/selector.rs#L115-L124)，并校验正则不能匹配空串。

#### 4.1.2 核心流程

TEXTUAL 规则的字段（priority=3、tags=true、effect、interrupt、finish）决定了它的行为：

- `effect`：`TextElem`/`LinebreakElem`/`SmartQuoteElem` 是 `Trigger`（能贡献文本），`SpaceElem` 是 `Inner`，其余一切 `Interrupt`。
- `interrupt`：**任何**样式变化都中断本分组——因为正则不能跨样式边界匹配（不同样式链里的 recipe 集合可能不同）。
- `finish: finish_textual`：收尾时三条出路。

`finish_textual` 的决策流程（伪代码）：

```
fn finish_textual(group):
    if visit_textual(s, start)? 产生了正则匹配:   # 命中
        return Ok(())                             # 由 visit_regex_match 接管，结束
    # 没有匹配：这些文本要并进段落
    if 当前在一个「非 PAR」分组里 (in_non_par_grouping):
        把 sink[start..] 暂存 → 截断 → 收尾掉那些非 PAR 分组 → 把元素接回
    if groupings 为空 且 规则表里包含 PAR:
        播种一个 PAR 分组 (start = 当前 sink 长度)   # 让文本透明地流进段落
    return Ok(())
```

关键认识：**段落（PAR）通常不是由文本元素直接开团的，而是由 `finish_textual` 在「没有正则命中」时播种出来的**。这承接了 u2-l8 里「文本先进 TEXTUAL，再由 finish_textual 播种 PAR」的结论。

#### 4.1.3 源码精读

TEXTUAL 规则定义：

[crates/typst-realize/src/lib.rs:L1017-L1041](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L1017-L1041) —— 注意注释：`SymbolElem` 在文本 show 规则运行前就已经被 kind 规则转成 `TextElem` 了，所以这里不再判 `SymbolElem`；并且数学模式下文本规则是「手动」应用的（见 4.3 末尾），因此 effect 里也不用考虑数学元素。`interrupt: |_| true` 就是「任何样式都打断」。

`finish_textual` 主体：

[crates/typst-realize/src/lib.rs:L1130-L1161](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L1130-L1161) —— 三分支与上面伪代码一一对应。注意第 1139 行的 `in_non_par_grouping` 判断和第 1151 行用 `std::ptr::eq(rule, &PAR)` 在规则表里找 PAR 的写法。

辅助判定：

[crates/typst-realize/src/lib.rs:L1164-L1168](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L1164-L1168) —— 「最内层分组存在，且它不是 PAR（或虽是 PAR 但已被 interrupted）」时返回 true，意味着需要先把外层非段落分组收掉，才能播种段落。

`visit_textual` 是「有匹配」分支的入口：

[crates/typst-realize/src/lib.rs:L1244-L1257](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L1244-L1257) —— 找到匹配后，这里才真正调用 `collapse_spaces`（冷路径），把 `sink[start..]` 存进 arena、截断 sink，再交给 `visit_regex_match`。注意它返回 `bool`：`true` 表示「我认领了，finish_textual 直接返回」。

#### 4.1.4 代码实践

**实践目标**：用一个**不含**正则规则的普通文档，亲眼看到 `finish_textual` 走的是「播种 PAR」那条回退路径，从而验证「段落由 finish_textual 播种」。

**操作步骤**：

1. 在 [crates/typst-realize/src/lib.rs:L1130](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L1130) 的 `finish_textual` 入口加 `eprintln!("[finish_textual] start={start}");`。
2. 在三个出口分别加日志：
   - 第 1133 行 `if visit_textual(s, start)? {` 命中后（return 前）打印 `[finish_textual] 路径A: 命中正则匹配`；
   - 第 1139 行 `if in_non_par_grouping(s) {` 命中后打印 `[finish_textual] 路径B: 退回段落（先收尾非 PAR）`；
   - 第 1152 行 `s.groupings.push(...)` 前打印 `[finish_textual] 路径C: 播种 PAR`。
3. 准备一个最小文档 `doc.typ`：

   ```typst
   Hello World
   ```

4. 用调试版编译：`cargo run --bin typst -- compile doc.typ`（具体命令以本机 workspace 为准，待本地验证）。

**需要观察的现象**：日志里应大量出现 `路径C: 播种 PAR`，而几乎不出现 `路径A`——因为没有任何正则规则，所有文本都走「播种段落」回退。

**预期结果**：每次一段连续文本被 TEXTUAL 收尾时，由于 `visit_textual` 找不到匹配返回 `false`，程序落到路径 C，把文本并进一个新开的 PAR 分组。最终这些 `ParElem` 再回到 `visit()` 走兜底 push。具体输出行数待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：TEXTUAL 的 `interrupt` 写成 `|_| true`，意味着「任何样式变化都中断」。结合 4.2 的 `find_regex_match_in_elems`，说说为什么必须这样？

> **答案**：正则 recipe 存放在样式链里。相邻两个元素如果样式链不同，它们「看得见」的 recipe 集合就可能不同；在样式边界处把字符串拼起来再跑同一条正则，既语义不清也会跨规则边界误匹配。所以分组层和查找层都拒绝跨样式拼接。

**练习 2**：`finish_textual` 的路径 B（`in_non_par_grouping`）什么时候会触发？给一个能触发它的真实场景。

> **答案**：当 TEXTUAL 分组嵌套在另一个非 PAR 分组内部、且当前这段文本没有正则命中时。例如列表项（LIST 分组，priority 2）内部的一行文本：文本先进 TEXTUAL（priority 3，更内层），`finish_textual` 发现没有匹配，但外层是 LIST 不是 PAR，于是先把 LIST 收尾、把元素接回，再让文本流进段落分组。

---

### 4.2 正则匹配的查找：find_regex_match_in_elems / find_regex_match_in_str

#### 4.2.1 概念说明

TEXTUAL 分组攒下的 `s.sink[start..]` 是一组 `Pair`（元素 + 样式链）。要在它们身上找正则匹配，需要解决两个问题：

1. **拼字符串**：把这些元素的文本表示拼成一段 `&str`，供 `regex.find` 使用。难点是空格——文本元素里夹着 `SpaceElem`，而排版前的空格折叠规则（边缘空格丢弃、相邻空格合并）会影响「最终文本」。所以拼字符串时必须**同步做一次空格折叠**，否则正则匹配到的位置和最终排版位置对不上。
2. **找最左匹配**：在同样式的文本段上，遍历样式链里的 recipes，挑出「匹配位置最靠左、且匹配非空、且未被撤销」的那一条。

这两个职责分别由 `find_regex_match_in_elems`（拼字符串 + 切分样式段）和 `find_regex_match_in_str`（单段内找最左匹配）承担。匹配结果汇总成一个 `RegexMatch`：

[crates/typst-realize/src/lib.rs:L190-L202](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L190-L202) —— 记录匹配在合并字符串里的 `offset`、匹配到的 `text`、匹配段的 `styles`、命中的 `id: RecipeIndex`、以及指向那条 `recipe` 的引用。

#### 4.2.2 核心流程

`find_regex_match_in_elems` 的流程：

```
buf = 空的 bump 字符串;  base = 0;  leftmost = None;  current = 默认样式;  state = Destructive
for (content, styles) in elems:
    (new_state, text) = collapse_state_textual(content, styles)
    根据 new_state 更新 state（折叠空格：边缘/破坏性空格丢弃，相邻空格合并）
    若 new_state 是 Invisible（如 TagElem） → 跳过
    若 styles 变了 且 buf 非空:                       # 样式边界
        在 buf 上找最左匹配; 命中则 break
        base += buf.len(); buf 清空                    # 开新的一段
    current = styles; buf.push_str(text)
最后再对剩余 buf 找一次最左匹配
把段内 offset 加上 base，得到全局 offset，包成 RegexMatch 返回
```

`find_regex_match_in_str` 的流程（在一段同样式文本上）：

```
r = 0; revoked = ∅; leftmost = None; depth = styles 里 recipe 总数
for entry in styles.entries():           # 逐条样式
    若是 Revocation(idx)  → revoked ∪= {idx}; continue
    若是 Property         → continue
    // 到这里一定是 Recipe
    r += 1
    若 recipe.selector() 不是 Regex → continue
    m = regex.find(text);  若无或为空匹配 → continue
    若已有更靠左的匹配 → continue
    index = RecipeIndex(depth - (r - 1))   # 从链顶给 recipe 编号
    若 index ∈ revoked → continue           # 这条规则已被撤销
    leftmost = (m, index, recipe)
返回 RegexMatch { offset, text, id: index, recipe, styles }
```

两个关键不变量：

- **「最左」优先**：`find_regex_match_in_str` 只保留起始位置最小的匹配；`find_regex_match_in_elems` 又按文本先后逐段搜索，一旦某段命中就 `break`。所以整条管线永远先处理最左边的匹配，处理完后再对剩余文本递归——这正是 `visit_regex_match` 的工作方式。
- **index 稳定**：`depth = recipes().count()` 只数 `Recipe`，循环里的 `r` 也只对 `Recipe` 自增（`Revocation` 在自增前就 `continue` 了）。因此后续在输出上插入新的 `Revocation` **不会**改变任何 recipe 的 index，撤销者和被撤销者能精确对上。`recipes()` 的实现见 [crates/typst-library/src/foundations/styles.rs:L696-L699](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/styles.rs#L696-L699)。

#### 4.2.3 源码精读

`find_regex_match_in_elems` 全文：

[crates/typst-realize/src/lib.rs:L1268-L1316](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L1268-L1316) —— 注意第 1297 行 `if styles != current && !buf.is_empty()`：样式一变就先把已积累的 `buf` 搜一遍、命中即 `break`，否则累加 `base` 并清空缓冲。文档注释（1259–1267 行）解释了为什么顺带做空格折叠：避免在「每个文本分组」上都跑一遍 `collapse_spaces`，只在确有匹配的冷路径上才真正折叠（见 `visit_textual` 第 1249 行）。

`find_regex_match_in_str` 全文：

[crates/typst-realize/src/lib.rs:L1319-L1372](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L1319-L1372) —— 第 1344 行 `if m.range().is_empty() { continue }` 丢弃空匹配（避免在空串上无限触发）；第 1350 行用 `p.start() <= m.start()` 保留更靠左者；第 1357 行 `RecipeIndex(*depth - (r - 1))` 算编号；第 1358 行 `revoked.contains(index.0)` 跳过被撤销的规则。

空格状态判定（拼字符串时用）：

[crates/typst-realize/src/spaces.rs:L102-L122](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/spaces.rs#L102-L122) —— `collapse_state_textual` 把 `TagElem` 判为 `Invisible`（完全跳过）、`LinebreakElem` 判为 `Destructive`（贡献 `"\n"` 但会吞掉邻接空格）、`SpaceElem` 判为 `Space`（贡献 `" "`）、`TextElem`/`SmartQuoteElem` 判为 `Supportive`。这四个 `SpaceState` 的定义见 [crates/typst-realize/src/spaces.rs:L10-L21](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/spaces.rs#L10-L21)。空格折叠的完整算法将在 u2-l11 详讲，本讲只需知道它保证了「匹配位置 == 排版后位置」。

`Style::Revocation` 的文档说得很直白：

[crates/typst-library/src/foundations/styles.rs:L221-L226](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/styles.rs#L221-L226) —— 它「目前只对 regex recipe 生效，因为这是目前唯一需要它的地方；普通 show 规则改用直接挂在元素上的 guard」。这条注释是理解本讲「为什么正则要单独一套防重入机制」的钥匙。

#### 4.2.4 代码实践

**实践目标**：观察跨元素匹配，以及「不同样式的空格不会被匹配」这一由分组层决定的行为。

**操作步骤**：

1. 在 [crates/typst-realize/src/lib.rs:L1248](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L1248) 的 `find_regex_match_in_elems` 调用后加日志，打印 `m` 的 `offset`、`text`、`id.0`。
2. 在 `find_regex_match_in_str`（第 1341 行 `regex.find` 之后）打印每次 `regex.as_str()`、`text`、`m.range()`。
3. 准备文档（取自仓库测试 `tests/suite/styling/show-text.typ` 的 `show-text-space-collapsing` 与 `show-text-styled-space` 用例）：

   ```typst
   // (a) 跨多个元素 + 空格折叠匹配
   #show "i ther": set text(red)
   hi#[ ]#[ ]the#"re"

   // (b) 不同样式的空格不被匹配（注释说明：纯属分组规则导致）
   #show " ": "B"
   #show "X": "B"
   A C \
   A#text(red)[ ]C
   ```

   说明：`#[ ]` 是显式空格元素，`#"re"` 是插值产生的独立 `TextElem("re")`。所以 (a) 的内容会被拆成 `TextElem("hi")`、两个 `SpaceElem`、`TextElem("the")`、`TextElem("re")`，正则 `"i ther"` 跨越它们命中。

4. 编译运行（命令待本地验证）。

**需要观察的现象**：
- (a) 日志应显示一次命中，`text = "i ther"`，`offset` 对应合并后字符串 `"hi there"` 里 `i ther` 的起始位置。两个相邻空格被折叠成一个，所以仍能匹配。
- (b) 第一行 `A C`（默认样式空格）里的空格命中 `" "` → 被替换成 `B`；但第二行 `A#text(red)[ ]C` 里那个空格是**红色样式**的，与前后文本不在同一个 TEXTUAL 分组里，因此**不会**命中。

**预期结果**：(a) 中 `hi there` 的 `i ther` 部分变红；(b) 中只有默认样式的空格被替换成 `B`，红色空格保留。这与 `show-text.typ` 第 215–222 行测试断言的「Differently styled spaces between text are not matched by regex rules. This is solely due to grouping rules, not space collapsing.」一致。具体渲染待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`find_regex_match_in_str` 为什么在第 1344 行丢弃 `m.range().is_empty()` 的匹配？

> **答案**：空匹配（匹配到空串）会导致匹配位置不前进，若放行就会在同一位置反复触发、陷入死循环。`Selector::regex` 在构造时已经禁止「能匹配空串」的正则（selector.rs 第 120 行），这里是对「单次 find 返回空 range」的二次防御。

**练习 2**：假如样式链里有两条 regex recipe 都匹配同一段文本，`find_regex_match_in_str` 会选哪一条？

> **答案**：选**起始位置最靠左**的那一条；若起始位置相同，保留**先遍历到**的那一条（第 1350 行用 `p.start() <= m.start()` 判断，已有的更靠左或同等靠左时不替换）。遍历顺序由 `Entries` 决定（从最内层 link、按 `next_back()` 反向逐条吐出），因此同等靠左时是最内层那条胜出。

---

### 4.3 切片与重打包：visit_regex_match / slice_textual 与 Revocation

#### 4.3.1 概念说明

找到最左匹配后，要把这段匹配文本「切出来」套用 recipe，再把规则输出和匹配前后的文本重新喂回流水线。难点在于：匹配可能**横跨多个元素**，也可能**只落在一个元素的中间**。

`visit_regex_match` 用一个统一的「光标模型」处理所有情况：把分组元素从头到尾再扫一遍，维护一个 `cursor` 表示当前已处理的文本长度，每个元素占据一段 `[cursor, cursor+len)`（`len` 是它的文本长度）。匹配占据 `[match_start, match_end)`。于是每个元素相对于匹配区间只可能是五种情形之一：

```
完全在匹配之前：       elem <match>      → 整体原样 visit
跨匹配起点：           te<match>         → 切出前半 ..end 单独 visit
匹配本身（首个元素）：  <match>           → 套用 recipe，输出带 Revocation 重新 visit
跨匹配终点：           <match>xt         → 切出后半 start.. 单独 visit
完全在匹配之后：       <match> elem      → 整体原样 visit
```

此外，`TagElem`（内省标签）一律直接转发——这样标签即使落在匹配中间（`mat<tag>ch`）也不会丢，而是在匹配处理完紧接着被 visit。

「套用 recipe」这一步需要构造传给用户函数的元素。为了让用户代码总能访问 `.text` 字段，匹配文本元素**一定是 `TextElem` 或 `SymbolElem`**：

- 若整个匹配恰好落在一个可切片的 `TextElem`/`SymbolElem` 内 → 用 `slice_textual` 切出那一段，**保留原元素类型、span、label**。
- 否则（匹配跨了多个元素，或落在线段/空格/智能引号上）→ 新建一个 `TextElem::packed(text)`，只能继承第一个匹配元素的 span。

最后是**防重入**：套用 recipe 后，把 `Style::Revocation(id)` 链到 styles 上，再 visit 输出。`id` 就是 4.2 里算出的 `RecipeIndex`。这样输出文本若再次进入 TEXTUAL 跑正则，`find_regex_match_in_str` 会在第 1358 行跳过这条被撤销的规则，**同一条规则不会在它自己产生的文本上再次触发**。这就是 `show "Hello": text(red)[Hello]` 不会无限循环的原因。

#### 4.3.2 核心流程

`visit_regex_match` 的主循环（伪代码）：

```
match_range = [m.offset, m.offset + m.text.len())
cursor = 0;  m = Some(m)                       # 用 Option 表示「还没处理匹配本体」
for (content, styles) in elems:
    if content 是 TagElem:
        visit(s, content, styles); continue    # 标签直接转发
    len = TextElem/SymbolElem 的 .text 长度，否则 1   # 空格/换行/引号按 1 字节
    elem_range = [cursor, cursor+len);  cursor = elem_range.end

    if elem_range 与 match_range 完全不相交:
        visit(s, content, styles); continue    # 整体在匹配外

    if elem_range 起点在 match_range 起点之前:  # 跨起点 te<match>
        visit(s, slice_textual(content, ..end), styles)

    if let Some(RegexMatch{..}) = m.take():     # 在首个匹配元素处处理匹配本体
        matched_text = 整个匹配落在一个可切片元素内
                        ? slice_textual(content, 局部 range)      # 保留类型/span/label
                        : TextElem::packed(text).spanned(content.span())
        output = recipe.apply(engine, ctx, matched_text)?
        revocation = Style::Revocation(id)
        chained = styles.chain(revocation)     # 把撤销挂上去
        visit(s, output, chained)              # 输出重新进流水线

    if elem_range 终点在 match_range 终点之后:  # 跨终点 <match>xt
        visit(s, slice_textual(content, start..), styles)
```

`slice_textual` 很短：只对 `TextElem`/`SymbolElem` 操作，克隆后用范围切 `.text` 字段：

[crates/typst-realize/src/lib.rs:L1485-L1504](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L1485-L1504) —— 注释提到两个微妙点：切片时不能给 `TextElem` 设 `span_offset`，因为那会生成一个用户可见的带样式元素、把 `TextElem` 套娃，从而遮住 `.text` 字段；symbol 虽是单个字素簇也照切（已知问题，见 issue #8058）。

`recipe.apply` 的三条路径：

[crates/typst-library/src/foundations/styles.rs:L489-L511](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/styles.rs#L489-L511) —— `Transformation::Content`（静态替换，如 `show "x": [Y]`）、`Transformation::Func`（函数，如 `show "x": it => ..`，会带上 `Tracepoint::Show` 便于报错定位）、`Transformation::Style`（show-set，如 `show "x": set text(red)`，直接给匹配文本挂样式）。正则命中的文本会被这三种变换之一处理。

#### 4.3.3 源码精读

`visit_regex_match` 全文（含详细文档注释）：

[crates/typst-realize/src/lib.rs:L1389-L1481](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L1389-L1481) —— 重点几行：
- 第 1402 行：`TagElem` 直接转发。
- 第 1410–1416 行：`len` 的计算，`TextElem`/`SymbolElem` 取 `.text.len()`，其余（空格/换行/智能引号都是 ASCII）按 1。
- 第 1420 行：完全不相交时整体 visit。
- 第 1427–1432 行：跨起点的切片 `..end`。
- 第 1441–1460 行：匹配本体 `matched_text` 的两种构造方式（保留类型 vs 新建）。
- 第 1462–1468 行：**套用 recipe 并挂 Revocation**——这是防重入的核心：

[crates/typst-realize/src/lib.rs:L1462-L1468](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L1462-L1468) —— 先 `recipe.apply` 得到 `output`，再造 `Style::Revocation(id)`，用 `outer.chain(...)` 把撤销压到 styles 之上，最后 `visit(s, output, chained)`。
- 第 1471–1476 行：跨终点的切片 `start..`。
- 第 1479 行 `debug_assert!(m.is_none())`：保证确实处理过匹配本体。

`RecipeIndex` 与 `Revocation` 的定义：

[crates/typst-library/src/foundations/styles.rs:L526-L528](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/styles.rs#L526-L528) —— `RecipeIndex(pub usize)`，就一个数字。

关于数学模式的一个补充：TEXTUAL 规则的 `effect` 注释（4.1.3）提到「数学模式下文本规则是手动应用的」。数学 realization（`MATH_RULES`）里**没有** TEXTUAL 规则（见 [crates/typst-realize/src/lib.rs:L1014-L1015](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L1014-L1015)），数学元素里的文本 show 规则由数学排版路径单独调用本讲的查找/切片逻辑。这是为什么 `find_regex_match_*` / `visit_regex_match` / `slice_textual` 都被设计成可独立复用的函数。

#### 4.3.4 代码实践

**实践目标**：验证「同一条正则规则只在自己原始匹配的文本上触发一次（Revocation 生效）」，并观察跨元素切片。

**操作步骤**：

1. 在 [crates/typst-realize/src/lib.rs:L1437](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L1437) 的 `if let Some(RegexMatch { text, .. }) = m.take()` 块内加日志，打印 `[match] 命中 text={text:?} id={id:?} 是否新建元素={是否走 else 分支}`。
2. 在第 1464 行 `recipe.apply` 之后、第 1468 行 `visit` 之前打印 `[match] 应用 recipe，输出将带 Revocation({id:?}) 重新 visit`。
3. 准备文档（取自 `show-text.typ` 的 `show-text-cyclic` 与 `show-text-exactly-once`）：

   ```typst
   // (a) 直接循环：输出里又含被匹配的文本，但不应再次触发
   #show "Hello": text(red)[Hello]
   Hello World!

   // (b) 每条规则各替换一次，但不同规则可链式触发
   #show "A": [BB]
   #show "B": [CC]
   AA (8)
   ```

4. 编译运行（命令待本地验证）。

**需要观察的现象**：
- (a) `[match] 命中 text="Hello"` 只应打印**一次**。尽管 recipe 输出的 `[Hello]`（红色）里仍然含有 "Hello"，但因为输出带着 `Revocation(A规则 id)` 重新 visit，`find_regex_match_in_str` 会跳过这条规则，不会再次命中。最终结果：红色的 "Hello" 后接 " World!"，且不无限循环。
- (b) `AA` 的处理：第一个 `A` 命中 → `BB`（A 规则输出，带 A 的撤销）；这两个 `B` 在随后 visit 时命中 **B 规则**（B 规则未被撤销）→ `CCCC`。两个 `A` 各自如此，共得 8 个 `C`。日志里能看到 A 规则命中 2 次、B 规则命中若干次，但**同一段原始文本上的同一条规则不会重复命中**。

**预期结果**：(a) 输出红色 "Hello World!"，无卡死；(b) 输出 `CCCCCCCC (8)`，共 8 个 C。这与 `show-text.typ` 第 16–19、37–41 行测试一致。具体渲染待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `visit_regex_match` 在「匹配本体」处要区分「落在一个可切片元素内」和「跨多个元素」两种情况？统一新建 `TextElem` 不行吗？

> **答案**：统一新建虽然简单，但会丢失原元素的类型、span、label。当匹配恰好落在单个 `TextElem`/`SymbolElem` 内时，用 `slice_textual` 切片可以保留这些信息（例如用户给某个文字加的 `<label>`、报错定位用的 span）。只有当匹配确实跨了元素、无法归约到单个元素时，才退而求其次新建 `TextElem::packed(text)` 并只继承首个匹配元素的 span。

**练习 2**：把 `show "A": [BB]` 改成 `show "A": [A]`（输出里仍有 A），会无限循环吗？为什么？

> **答案**：不会无限循环。`visit_regex_match` 在 visit 输出 `[A]` 前，会把 `Style::Revocation(A规则 id)` 链到 styles 上；于是输出里的 "A" 再次经过 TEXTUAL 时，`find_regex_match_in_str` 在第 1358 行发现该 id 在 `revoked` 集合里而跳过它，A 规则不再触发。最终只替换一次。

**练习 3**：普通元素 show 规则（如 `show heading: it`）防重入用的是 `RecipeIndex` guard（挂在元素 lifecycle 位上，见 u2-l3），而正则 show 规则改用 `Style::Revocation`。结合 styles.rs 第 221–226 行注释，说说为什么不能用同一套机制。

> **答案**：普通 show 规则的判定对象是「单个元素」，可以在该元素自己的 lifecycle 位集上打个 guard 标记「这条规则已对它应用过」，下次再遇到同一元素时跳过。但正则规则判定的是「跨多个元素拼出的文本」，没有单个元素可以挂这个 guard；而且同一文本可能被规则输出重新产生。因此正则规则改用挂在**样式链**上的 `Revocation`——它随样式链流动，能在输出文本上继续生效，且其 index 由「只数 Recipe」的 `depth - (r-1)` 公式保证稳定。

---

## 5. 综合实践

把本讲三条主线（跨元素匹配、最左优先、Revocation 防重入）串起来验证一次。

**任务**：写一个文档，包含一条跨元素的正则 show 规则，并让它「看起来会循环」但其实不会。先用纸笔预测输出，再加日志核对。

```typst
// 规则：把 "ab" 替换成 "(ab)"，输出里故意仍含 "ab"，但不应循环
#show "ab": it => [(#it)]
#let a = "a"
#let b = "b"
#a#b cab
```

> 说明：`#a#b` 是两个**相邻的字符串插值**，各产生一个独立的 `TextElem`（参见 `show-text.typ` 中 `the#"re"` 把文本拆成独立元素的用法），且二者同默认样式，故能进同一个 TEXTUAL 分组。注意不要写成 `#ab`——那会被当成对变量 `ab` 的引用。

**预测与验证步骤**：

1. **先预测**：`#a#b` 展开为 `TextElem("a")` + `TextElem("b")`（两个插值元素，同默认样式），正则 `"ab"` 跨这两个元素命中 → `(ab)`。`cab` 是单个 `TextElem("cab")`，`"ab"` 命中其后半段 → `c(ab)`（切片保留单个元素，见 4.3）。两段合计：`(ab) c(ab)`。规则输出 `(ab)` 里虽含 "ab"，但 Revocation 生效，不会再次触发。
2. **加日志**：在 `find_regex_match_in_str`（第 1341 行后）打印每次正则查找；在 `visit_regex_match` 第 1437 行打印每次命中与切片方式。
3. **运行并比对**（命令待本地验证）：日志里应看到 2 次命中（`#a#b`、`cab` 各一次），且没有任何一次命中来自规则自身的输出文本 `(ab)`——从而验证「最左优先 + 逐段递归 + Revocation」三者协作的结果与预测一致。

> 若预测与实际不符，重点检查：(1) `#ab` 是否真的被评估成两个独立 TextElem（可在 `find_regex_match_in_elems` 里打印 `elems.len()`）；(2) `cab` 的命中是否走了 `slice_textual` 保留 span 的分支。

## 6. 本讲小结

- 正则/文本 show 规则**不走**普通 `verdict()` 路径，因为 `Selector::matches` 对 `Regex` 返回 `false`；它们由 priority 最高的 **TEXTUAL 分组**单独承载。
- TEXTUAL 把连续同样式的 `TextElem`/`SpaceElem`/`LinebreakElem`/`SmartQuoteElem` 攒成一段，任何样式变化都会打断它（`interrupt: |_| true`）。
- `finish_textual` 有三条出路：命中匹配交 `visit_textual`、退回并播种 PAR；**段落通常由 finish_textual 在无匹配时播种**。
- `find_regex_match_in_elems` 拼字符串并顺带空格折叠、按样式段切分；`find_regex_match_in_str` 在单段内找「最左、非空、未被撤销」的匹配，并用 `RecipeIndex(depth-(r-1))` 给 recipe 编号——该编号只数 Recipe，故对 Revocation 插入稳定。
- `visit_regex_match` 用光标模型把匹配横跨的元素切成「前/匹配本体/后」三段；匹配本体优先 `slice_textual` 保留类型与 span，否则新建 `TextElem`。
- 防重入靠 `Style::Revocation(id)`：套用 recipe 后把它挂到样式链上再 visit 输出，使同一条规则不在自身输出上重复触发；这与普通 show 规则的元素 guard 是两套互补机制。

## 7. 下一步学习建议

- **u2-l11（空格折叠 spaces.rs）**：本讲多次提到的 `SpaceState`、`collapse_spaces`、`collapse_state_textual` 都在那里详讲。学完它你能彻底理解「为什么正则匹配位置等于排版后位置」。
- **u3-l1（标签与内省）**：本讲里 `TagElem` 在 `visit_regex_match` 中「直接转发、可落在匹配中间」的处理，其完整语义（start/end tag 配对、跨分组边界）在 u3-l1 展开。
- **u3-l6（递归深度限制与错误处理）**：本讲的 Revocation 是防止**单条规则自递归**的机制；跨规则的循环则由 `MAX_SHOW_RULE_DEPTH` 兜底，二者关系在 u3-l6 系统讲解。
- 继续阅读建议：把 `tests/suite/styling/show-text.typ` 通读一遍，它几乎覆盖了本讲所有边界情形（跨行、跨智能引号、循环、show-set 文本规则、数学内文本规则）。
