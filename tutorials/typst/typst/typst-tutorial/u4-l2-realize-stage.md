# u4-l2 realize 阶段：show 规则如何改写文档

## 1. 本讲目标

学完本讲，你应当能够：

- 说出 realize 阶段在编译流水线中的位置：它消费求值产出的 `Content` 树与一条样式链，输出「元素 + 样式链」的扁平列表 `Vec<Pair>`，供排版阶段直接消费。
- 描述 `visit` 流水线的几道关卡（kind 规则、show 规则、序列/样式展开、分组、过滤），以及段落在哪里被"组装"出来。
- 解释用户 show 规则、show-set 规则与内置 show 规则三者的裁决顺序，以及 `RecipeIndex` 守卫如何防止 show 规则无限自我触发。
- 跟踪内置规则表 `NativeRuleMap` 的完整装配链：`Routines::rules` → `typst_layout::register` / `typst_html::register` → `Format::register_rules`，并据此定位任一内置元素默认外观的源码。
- 掌握 `visit_styled` 中样式真正"挂链"的时机，以及 document/page 样式的边界校验。

## 2. 前置知识

本讲直接建立在 u3-l7（样式系统）之上。先用三段话唤醒记忆：

- **Styles 与 Recipe**：求值阶段执行 `#set` 立即产出 `Property`；执行 `#show` 只是登记一条 `Recipe`（内含 `Transformation`，可以是 `Content`、`Func` 或 `Style`——最后一种即 show-set 规则）。两者混装在有序列表 `Styles` 中，由 `StyledElem` 包住其后的内容。
- **StyleChain**：从外到内的样式链，realize 递归时随身携带，最终每个输出元素都配一条链。
- **Content 与 Element**：文档的统一表示，`heading`、`strong`、`text` 都是元素实例（u3-l4）。

本讲新增的术语：

- **Realization（实现化）**：把语义层元素（heading、strong、cite……）翻译成排版引擎认识的"知名元素"（block、text、v……）的过程。typst-realize 的 crate 文档写得很直白：*Realization is the process of recursively applying styling and, in particular, show rules to produce well-known elements that can be processed further.*（见 [crates/typst-realize/src/lib.rs:1-5](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-realize/src/lib.rs#L1-L5)）
- **Pair**：`(&'a Content, StyleChain<'a>)` 二元组，realize 的输出单元（下文会看到它的定义）。
- **Arena（临时竞技场分配器）**：realize 过程中会产生大量短命 `Content`/`Styles`，放进一次性分配的 arena 里延长生命周期，整趟编译结束后统一释放，避免细碎的堆分配。
- **Tag / TagElem**：内省系统的"路标"，成对包住可定位元素，排版后可据此反查元素落在了哪一页哪个位置（u4-l7 展开）。
- **ShowFn / NativeShowRule**：内置 show 规则的函数指针类型及其类型擦除包装。

## 3. 本讲源码地图

| 文件 | 角色 |
|---|---|
| `crates/typst-realize/src/lib.rs`（1542 行） | 本讲主战场：realize 入口、visit 流水线、verdict/prepare、分组规则表 |
| `crates/typst-realize/src/spaces.rs` | 辅助：空格折叠（`collapse_spaces`） |
| `crates/typst-library/src/routines.rs` | 契约：`RealizationKind`、`Arenas`、`Pair` 的定义；`Routines::realize` 函数指针签名 |
| `crates/typst-library/src/lib.rs` | 契约：`Library.rules` 字段；`LibraryBuilder::build` 的规则装配 |
| `crates/typst-library/src/foundations/styles.rs` | 契约：`NativeRuleMap`、`ShowFn`、`NativeShowRule` 的定义 |
| `crates/typst/src/lib.rs` | 总装：`ROUTINES` 表，把 `typst_realize::realize`、`typst_layout::register` 等接进标准库 |
| `crates/typst-layout/src/rules.rs`（829 行） | Paged 目标的全部内置 show 规则与 `register()` |
| `crates/typst-layout/src/pages/mod.rs`、`crates/typst-layout/src/flow/mod.rs` | 调用点：`layout_document` / `layout_fragment` 如何发起 realize |
| `crates/typst-library/src/format.rs` | 扩展：`Format::with_rules`，导出格式注册专属规则（u5-l3 展开） |

再次体会 u1-l3 介绍的架构决策：**类型（`NativeRuleMap`、`RealizationKind`）住在 typst-library，行为（realize 主循环、内置规则）拆在 typst-realize / typst-layout**，两者由总装 crate 的 `ROUTINES` 函数指针表接线。

## 4. 核心概念与源码讲解

### 4.1 realize 主循环：visit 流水线与分组

#### 4.1.1 概念说明

u4-l1 讲过，编译流水线是 parse → eval → realize → layout → export。求值（typst-eval）产出的 `Content` 树里，既有用户直接写的语义元素（`heading`、`strong`、`list`），也有散落的行内文本片段。排版引擎（typst-layout）并不认识 `heading` 的默认外观——那正是 realize 要做的事：

1. **应用样式与 show 规则**：用户 show 规则和内置 show 规则把语义元素改写成基础元素（`heading` → 带 inset 的 `block`，`strong` → 带字重增量的 `text`）。
2. **分组（grouping）**：把相邻的同类元素收集起来组装成整体——行内文本攒成 `par`，连续 `cite` 攒成 `CiteGroup`，连续列表项攒成 `list`/`enum`/`terms`。
3. **生成内省路标**：为可定位/带标签的元素插入成对的 Start/End Tag。

输出的不再是树，而是一个扁平的 `Vec<Pair>`，每个 Pair 是"一个知名元素 + 它生效的样式链"。

#### 4.1.2 核心流程

入口 `realize()` 的骨架：

```
realize(kind, engine, locator, arenas, content, styles):
    s = State {
        rules:   按 kind 选分组规则表（Bundle/Document/Fragment → FLOW_RULES，
                 Par → PAR_RULES，Math → MATH_RULES）
        sink:    空的输出列表
        groupings: 当前活跃的分组栈
        outside: kind == Document（用于页面样式提升）
    }
    visit(s, content, styles)     # 递归改写
    finish(s)                      # 收尾所有未闭合的分组
    return s.sink
```

`visit()` 对每段内容依次过五道关卡，任何一道接手处理就返回：

```
visit(content, styles):
  1. TagElem            → 直接推入 sink（路标不需要改写）
  2. visit_kind_rules   → 按 RealizationKind 做前置变换（如符号转文本）
  3. visit_show_rules   → 准备 + 应用 show 规则（4.2 详解）
  4. SequenceElem       → 递归每个孩子
     StyledElem        → 走 visit_styled 挂样式链（4.4 详解）
  5. visit_grouping_rules → 尝试并入/新开一个分组
  6. visit_filter_rules  → 丢弃顶层散落的空格、段落分隔符
  都没接手 → 推入 sink
```

注意第 3 关在分组之前：`strong`、`emph` 这类行内语义包装会先被 show 规则"溶解"成带样式的 `text`，分组看到的已经是纯基础元素了。

分组的规则由 `GroupingRule` 描述，四种效果构成一个小状态机：

- `Trigger`：触发/延续该分组（如 `TextElem` 对 PAR 分组）；
- `Inner`：只能出现在分组内部（如空格）；
- `Neutral`：可穿插但不触发（HTML 混排场景）；
- `Interrupt`：终止分组。

不同分组有 `priority`（TEXTUAL=3 > CITES/LIST/ENUM/TERMS=2 > PAR=1），高优先级规则到来时开嵌套分组。当分组被触发收尾时，`finish` 函数把攒下的元素打包：`finish_par` 造 `ParElem`、`finish_cites` 造 `CiteGroup`、`finish_list_like` 造 `ListElem`/`EnumElem`/`TermsElem`——**造完还要再 `visit` 一次**，让新元素继续走 show 规则流程。

#### 4.1.3 源码精读

入口与状态（[crates/typst-realize/src/lib.rs:41-74](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-realize/src/lib.rs#L41-L74)）：这段是 `realize` 函数本体——按 `RealizationKind` 选分组规则表、初始化 `State`，然后 `visit` + `finish`，最后返回 `s.sink`。整个 crate 的全部可变状态都装在 `State` 里（[L86-L108](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-realize/src/lib.rs#L86-L108)）：输出 `sink`、分组规则表 `rules`、活跃分组栈 `groupings`，以及几个控制空格与页面样式的布尔量。

`RealizationKind` 与输出类型的定义在 typst-library（[crates/typst-library/src/routines.rs:151-194](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/routines.rs#L151-L194)）：这段定义了五种实现化种类（`Bundle`/`Document`/`Fragment`/`Par`/`Math`）、`FragmentKind`、临时存储的 `Arenas`，以及本讲的输出单元 `type Pair<'a> = (&'a Content, StyleChain<'a>)`。

visit 主循环（[crates/typst-realize/src/lib.rs:241-294](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-realize/src/lib.rs#L241-L294)）：这一段就是上面伪代码的真身——五道关卡的顺序在这里一目了然，注释明确写着 "Apply show rules and preparation" 在 "Recurse into sequences" 之前。

分组规则结构（[crates/typst-realize/src/lib.rs:110-147](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-realize/src/lib.rs#L110-L147)）：`GroupingRule` 的四个字段（`effect`/`interrupt`/`finish` 三个函数指针加优先级）与 `GroupingEffect` 枚举，是分组状态机的全部静态描述。

分组规则表的选择（[crates/typst-realize/src/lib.rs:1022-1032](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-realize/src/lib.rs#L1022-L1032)）：`FLOW_RULES = [TEXTUAL, PAR, CITES, LIST, ENUM, TERMS]` 是文档与容器共用的表；段落与数学模式用裁剪过的表；bundle 目标完全不分组。

TEXTUAL 与 PAR 两条规则（[crates/typst-realize/src/lib.rs:1034-1088](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-realize/src/lib.rs#L1034-L1088)）：TEXTUAL 把相邻文本攒起来，给正则 show 规则（如 `#show "好": ...`）一个跨元素匹配的机会（[L1139-L1178](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-realize/src/lib.rs#L1139-L1178) 的 `finish_textual`）；PAR 的触发条件列出全部行内元素，HTML 元素按标签决定 Trigger 或 Neutral。

段落如何诞生（[crates/typst-realize/src/lib.rs:1205-1220](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-realize/src/lib.rs#L1205-L1220)）：`finish_par` 折叠空格、把攒下的孩子打包成 `ParElem`，然后**重新 `visit`**——这就是"段落是 realize 组装出来的"的直接证据。

列表组装（[crates/typst-realize/src/lib.rs:1236-1259](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-realize/src/lib.rs#L1236-L1259)）：`finish_list_like` 用 `StyleChain::trunk` 找出所有列表项样式链的公共主干，把每项的"局部样式"单独存回元素；`tight` 的判定就一行——孩子里只要出现 `ParbreakElem` 就算松散列表。

过滤关卡（[crates/typst-realize/src/lib.rs:768-802](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-realize/src/lib.rs#L768-L802)）：没被分组收走的顶层空格、段落分隔符在这里被丢弃——空格只是分组内部的粘合剂，`ParbreakElem` 只当边界用。

#### 4.1.4 代码实践

**实践目标**：验证「tight / loose 列表的判定发生在 realize 的分组阶段」。

1. 新建 `list-tight.typ`，内容两行：`- 甲` 换行 `- 乙`；再建 `list-loose.typ`，两个列表项之间空一行。
2. 分别编译：`cargo run -p typst-cli -- compile list-tight.typ tight.pdf` 与 `cargo run -p typst-cli -- compile list-loose.typ loose.pdf`。
3. 打开两份 PDF 对比条目间距；也可把输出扩展名换成 `.png` 逐像素看。

**需要观察的现象**：紧贴的列表项间距小（tight），隔空行的列表项间距明显更大（loose）。

**预期结果**：与 [L1241](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-realize/src/lib.rs#L1241) 的判定 `let tight = !elems.iter().any(|(c, _)| c.is::<ParbreakElem>())` 完全对应——空行在求值后会产出 `ParbreakElem`，realize 分组时据此把列表标记为 loose。（本实践在本地运行即可验证。）

#### 4.1.5 小练习与答案

1. **问**：`visit` 的关卡里，为什么 show 规则必须在分组之前执行？
   **答**：分组规则只认识基础元素（text、box 等）。`strong`、`emph` 这类语义包装若不先被 show 规则溶解成带样式的 `text`，就会作为 Interrupt 打断 PAR 分组，段落会被错误地切碎。
2. **问**：`finish_par` 组装出 `ParElem` 之后为什么还要再调用一次 `visit`？
   **答**：`ParElem` 也是普通元素——用户可能写了 `#show par: ...`，或过滤关卡需要对它生效；所有元素都必须走同一条 visit 流水线，没有捷径。
3. **问**：`RealizationKind::Fragment` 与 `Document` 共用 `FLOW_RULES`，但行为仍有差异，差异来自哪里？
   **答**：来自 `State` 的 `outside` 标志（仅 Document 初始为 true，影响页面样式提升）以及 `finish` 里对 Fragment 的特殊处理——若内容全是行内元素，就不强制成段（[L806-L819](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-realize/src/lib.rs#L806-L819)），并把 `FragmentKind` 改写为 `Inline` 供上层按行内排版。

### 4.2 show 规则的裁决：verdict、prepare 与 visit_show_rules

#### 4.2.1 概念说明

同一个元素可能同时面对三类"改写者"：

- **用户 transformational show**（`#show heading: it => ...`、`#show heading: [替换]`）；
- **用户 show-set**（`#show heading: set text(red)`）——u3-l7 讲过它在数据层是一条 `Transformation::Style` 的 Recipe；
- **内置 show 规则**（`NativeRuleMap` 里的 HEADING_RULE 等，4.3 详解）。

裁决规则是：

1. **show-set 全部生效**：所有匹配的 show-set 样式累加进一个局部 `map`（与 prepare 阶段发现的内置 show-set 汇合）。
2. **transformational 只挑一条**：从样式链里找第一条"匹配且尚未应用过"的用户规则，作为 `step`。
3. **内置规则兜底**：没有用户规则时才查 `NativeRuleMap`。

另有两个精妙设计：

- **守卫（guard）防无限递归**：规则应用到元素时带一个 `RecipeIndex` 编号，并把守卫记在内容上；若规则的输出里又出现同一元素（比如 `#show heading: it => heading(..)`），verdict 检测到该编号已被守卫就跳过这条规则，避免 show 调 show 的死循环。
- **错误延迟**：show 规则内的报错不会立刻中断编译，而是记下并继续（内容置空），配合 introspection 多轮迭代，让"只在前几轮才发生的错误"有机会消失，也让一次编译能同时报告多个错误。

#### 4.2.2 核心流程

```
verdict(elem, styles) -> Option<Verdict>:
    若未准备 且 元素可合成(Synthesize): 克隆并预合成
        （为了让 show figure.where(kind: table) 能按合成字段匹配）
    depth = 链上 recipe 总数
    对每条 recipe:
        选择器不匹配 → 跳过
        Transformation::Style(show-set) → 并入 map，继续
        已有 step → 跳过
        index = depth - 位置; 元素已被 index 守卫 → 跳过
        否则 step = Recipe(recipe, index)
    step 仍为空 → 查 engine.library.rules.get(target, elem)
                 命中则 step = Builtin(rule)
    无事可做（无 step、map 空、且不需要准备）→ 返回 None

visit_show_rules(content, styles):
    verdict 没接手 → 返回 false
    未准备过 → prepare(): 分配 Location、内置 show-set、
               Synthesize 字段合成、materialize 物化样式字段、
               生成成对 Tag、mark_prepared
    有 step → 应用之（用户规则走 recipe.apply，内置规则走 rule.apply）
    出错 → 延迟，内容置空
    推 Start Tag → visit_styled(改写结果, map) → 推 End Tag
```

#### 4.2.3 源码精读

verdict 主体（[crates/typst-realize/src/lib.rs:435-530](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-realize/src/lib.rs#L435-L530)）：先做预合成（注释坦承这是为了让 `show figure.where(kind: table)` 能匹配合成后的字段，"It's really unfortunate that we have to do this"），再遍历 recipes。守卫判断在 [L489-L493](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-realize/src/lib.rs#L489-L493)：`RecipeIndex(*depth - r)` 配合 `elem.is_guarded(index)`。

内置规则兜底（[crates/typst-realize/src/lib.rs:506-512](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-realize/src/lib.rs#L506-L512)）：`engine.library.rules.get(target, elem)`——用户规则优先、内置兜底的分叉点就在这一行，其中 `target` 从样式的 `TargetElem::target` 读出（Paged/Html/Bundle，u5-l3 展开）。

prepare 一次性准备（[crates/typst-realize/src/lib.rs:532-589](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-realize/src/lib.rs#L532-L589)）：按顺序做五件事——分配 Location（只给可定位/带标签的元素）、应用内置 show-set、`Synthesize` 合成派生字段、`materialize` 把样式链上的 settable 字段固化进元素自身、生成成对的 Start/End Tag 并 `mark_prepared`。整个函数对每个元素只执行一次。

step 应用与错误延迟（[crates/typst-realize/src/lib.rs:351-433](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-realize/src/lib.rs#L351-L433)）：`ShowStep::Recipe` 分支构造 `Context`（供规则里的 `context` 表达式取位置与样式，呼应 u3-l7）后调用 `recipe.apply`，并用 `output.into_owned().guarded(guard)` 把守卫种进传给规则的内容；`ShowStep::Builtin` 分支带一个按元素名命名的计时 scope。错误经 `s.engine.delay(result)`（[L396-L403](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-realize/src/lib.rs#L396-L403)）延迟。递归深度由 `route.check_show_depth()`（[L419-L420](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-realize/src/lib.rs#L419-L420)）限制——守卫之外的第二道防环保险。

#### 4.2.4 代码实践

**实践目标**：验证「用户 show 规则替换的是默认外观（show 通道），但 show-set 通道的样式仍然生效」——两条通道互不干扰。

1. 写 `head.typ`：

   ```typst
   = 默认标题

   #show heading: it => text(red, it.body)
   = 自定义标题
   ```

2. 编译：`cargo run -p typst-cli -- compile head.typ head.pdf`。

**需要观察的现象**：第一个标题是默认黑色大字；第二个标题文字变红，但字号、字重仍保持标题规格，并没有退回正文样式。

**预期结果**：用户规则替换了 HEADING_RULE 的输出（不再包 block），但标题的字号/字重来自元素自身的样式通道（prepare 阶段并入 `map`，见 [L558-L562](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-realize/src/lib.rs#L558-L562)），与 show 替换无关，所以继续生效。（本地运行即可验证。）

#### 4.2.5 小练习与答案

1. **问**：为什么 verdict 要区分 `prepared` 与未准备两种状态？同一元素会被 visit 多次吗？
   **答**：会。元素可能出现在多轮 layout 迭代、或被 show 规则输出后再次进入 visit。`prepare` 里的 Location 分配、字段合成、Tag 生成都必须"只发生一次"，`mark_prepared`（[L586](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-realize/src/lib.rs#L586)）就是幂等开关；已准备的元素再次到来时跳过这些步骤。
2. **问**：`#show heading: it => block(text(blue, it.body))` 里的 body 还会触发这条规则吗？
   **答**：不会。规则应用时内容带了 `RecipeIndex` 守卫；若输出里再出现 heading，verdict 在 [L489-L493](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-realize/src/lib.rs#L489-L493) 发现该编号已守卫就跳过这条规则（输出里的 heading 会改走内置规则或其它规则）。深度保险 `check_show_depth` 兜住其余情况。
3. **问**：show 规则里的错误为什么"延迟"而不是立刻中止编译？
   **答**：introspection 会让 layout 多轮迭代（u4-l1/u4-l7），前几轮布局不稳可能诱发规则报错，下一轮就消失；延迟还能把多个错误攒起来一起报告（[L396-L402](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-realize/src/lib.rs#L396-L402) 的注释写明了这两点）。

### 4.3 内置规则注册表：NativeRuleMap 的三级装配

#### 4.3.1 概念说明

"内置元素的默认外观"没有硬编码在 realize 主循环里，而是一张独立的查询表：**键是 `(Element, Target)`（元素 × 编译目标），值是 `NativeShowRule`（一个类型擦除的函数指针）**。这样做有三个好处：

- realize 主循环只管流程，不关心每个元素长什么样，职责干净；
- 同一元素在 Paged（纸张）与 Html（网页）目标下可以有不同默认外观——表按目标索引；
- 新导出格式（Format）可以注册自己的专属规则而不必改动编译器核心。

#### 4.3.2 核心流程

装配链（从定义到可用）：

```
① NativeRuleMap::new()                      预置极少数特殊规则
      （CONTEXT_RULE、COUNTER_DISPLAY_RULE、若干"恒空"规则）
② ROUTINES.rules = || { new() + typst_layout::register
                            + typst_html::register }   （总装 crate 接线）
③ LibraryBuilder::build():
      rules = (routines.rules)()
      for format in formats: format.register_rules(&mut rules)  （格式专属）
④ Library.rules = rules                     （随 Library 分发给 Engine）
⑤ verdict() 运行期查表: library.rules.get(target, elem)
```

查询用 `(content.func(), target)` 做键，`O(1)` 命中。重复注册会 panic（"duplicate native show rule"），保证表的一致性；要替换有显式的 `replace` 方法。

#### 4.3.3 源码精读

数据结构与签名（[crates/typst-library/src/foundations/styles.rs:1010-1021](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/styles.rs#L1010-L1021)）：`NativeRuleMap` 内部是 `IndexMap<(Element, Target), NativeShowRule>`；`ShowFn<T>` 是内置规则的统一签名——吃进打包元素、引擎与样式链，吐出新的 `Content`。

构造与注册（[crates/typst-library/src/foundations/styles.rs:1023-1073](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/styles.rs#L1023-L1073)）：`new()` 只预置跨目标通用的少数规则（上下文元素、计数器显示、以及几个"只服务于内省、输出恒为空"的元素）；`register` 把 `(T::ELEM, target)` 插入表，重复即 panic。查询入口 `get` 见 [L1089-L1093](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/styles.rs#L1089-L1093)。

类型擦除的 `NativeShowRule`（[crates/typst-library/src/foundations/styles.rs:1116-1145](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/styles.rs#L1116-L1145)）：因为 `Packed<T>` 是 `Content` 的透明包装，构造时用 `transmute` 把具体类型的 `ShowFn<T>` 擦成 `fn(&Content, ...)` 存起来——注释明说这是"两函数指针类型仅首参不同"的转换，代价是 `apply` 只能信任调用方传入的元素类型（verdict 按元素查表，类型必然匹配）。

总装接线（[crates/typst/src/lib.rs:325-342](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst/src/lib.rs#L325-L342)）：`ROUTINES` 静态表里 `rules` 闭包先 `NativeRuleMap::new()` 再依次调 `typst_layout::register`（Paged 目标）与 `typst_html::register`（Html 目标）；紧随其后的 `realize: typst_realize::realize` 就是 u1-l3 说的"函数指针表接线"，让 typst-library 的签名与 typst-realize 的实现隔 crate 相认。

Library 持有与构建（[crates/typst-library/src/lib.rs:161-188](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/lib.rs#L161-L188) 与 [L230-L238](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/lib.rs#L230-L238)）：`Library.rules` 字段就是这张表；`LibraryBuilder::build` 里 `let mut rules = (self.routines.rules)();` 之后，逐个 `format.register_rules(&mut rules)` 把格式专属规则叠上去。

Paged 目标的注册清单（[crates/typst-layout/src/rules.rs:38-111](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/rules.rs#L38-L111)）：`register()` 按 Model / Text / Layout / Visualize / Math / PDF 分组，把 60 余条 `*_RULE` 常量注册到 `Target::Paged`。这张清单就是"找某元素默认外观"的索引页。

两个代表性规则：

- 最简规则（[crates/typst-layout/src/rules.rs:113-121](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/rules.rs#L113-L121)）：`STRONG_RULE` 只有一句——取出 body，给它叠加一个字重增量样式；`EMPH_RULE` 同理叠一个斜体开关。**内置 show 规则的本质常常就是"拿样式包一层"**。
- 最复杂规则之一（[crates/typst-layout/src/rules.rs:245-309](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/rules.rs#L245-L309)）：`HEADING_RULE` 要处理编号（读 counter、甚至借 `layout_frame` 测量编号宽度决定悬挂缩进）、拼接 spacing 与 body，最后包进带 inset 的 `BlockElem`。注意它输出的是 `block` 排版原语；`LIST_RULE`（[L123-L140](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/rules.rs#L123-L140)）则更进一步，把真正的排版延迟为挂进 `BlockElem` 的布局回调（`crate::lists::layout_list`）——realize 决定"是什么"，layout 决定"怎么摆"。

格式专属规则（[crates/typst-library/src/format.rs:133-149](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/format.rs#L133-L149)）：`Format::with_rules` 存一个注册函数，`register_rules` 在 Library 构建时回调——新导出格式可以自带规则增量，机制细节留到 u5-l3。

最后补一个 nuance：**不是每个元素都有内置规则**。`ParElem`、`BlockElem` 这类排版原语没有注册任何规则，由 layout 阶段直接消费；表里只登记"需要改写"的语义元素。

#### 4.3.4 代码实践

**实践目标**：亲手统计 Paged 目标的内置规则数量，并定位 heading 的默认外观。

1. 统计注册条数：`grep -c "rules.register(Paged" crates/typst-layout/src/rules.rs`。
2. 定位标题默认外观：`grep -n "HEADING_RULE" crates/typst-layout/src/rules.rs`，再打开对应行号（应在 245 行附近）阅读。
3. 对照另一目标：`grep -n "rules.register" crates/typst-html/src/rules.rs | head`，看看 Html 目标注册了哪些（以实际代码为准）。

**需要观察的现象**：Paged 注册条数是几十条量级；两个目标的规则清单并不相同。

**预期结果**：register() 清单与 4.3.3 引用的行号一一对应；HTML 目标对纯纸张概念（如 page 相关元素）不注册规则。（grep 命令本地可验证。）

#### 4.3.5 小练习与答案

1. **问**：为什么 `NativeRuleMap` 的键要包含 `Target`，而不只是元素？
   **答**：同一元素在不同导出目标下默认外观不同（heading 在 Paged 下是 block + 编号，在 Html 下生成标题标签结构），按 `(Element, Target)` 索引才能一表服务多目标。
2. **问**：`register` 在重复注册时直接 panic 而不是静默覆盖，为什么？
   **答**：重复注册几乎必然是编码错误（两个模块都以为自己在定义该元素的默认外观），静默覆盖会让 bug 难以定位；需要替换时有显式的 `replace` 方法（[styles.rs:1075-1087](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/styles.rs#L1075-L1087)）。
3. **问**：如果让你给某个新元素加默认外观，要改哪些文件？
   **答**：在 `crates/typst-layout/src/rules.rs` 写一个 `const XXX_RULE: ShowFn<XxxElem>`，在 `register()` 里 `rules.register(Paged, XXX_RULE)`；Html 目标同理去 `crates/typst-html/src/rules.rs`。realize 主循环零改动。

### 4.4 样式应用时机：visit_styled 与边界

#### 4.4.1 概念说明

u3-l7 里 `StyledElem` 只是"求值期登记的样式容器"；到 realize，它要被真正摊开：**局部样式列表变成样式链上的一个新链接**。三个关键时机：

1. **show 输出重入**：`visit_show_rules` 最后调用 `visit_styled(s, realized, Cow::Owned(map), styles)`——`map` 是 verdict/prepare 攒下的样式，作为新的一环链到元素上。
2. **源码中的 `StyledElem` 展开**：`#text(red)[...]` 求值成 StyledElem 后，`visit` 递归到 `visit_styled`，把它的 styles 挂链后继续 visit 孩子。
3. **materialize**：prepare 阶段把样式链上的 settable 字段读出并"物化"进元素实例，让后续 show 规则与 layout 直接在元素上读到最终值。

同时，`visit_styled` 还是几条编译器规则的执法现场：

- `#set document(...)` 与格式选项只允许出现在顶层（Document kind）；
- `#set page(...)` 在 Paged 顶层会"破门而出"触发弱分页；在容器内直接报错；
- `finish_interrupted`：当新样式涉及 ParElem/AlignElem 等元素时，先收掉当前活跃的段落分组，保证样式边界两侧的段落独立。

#### 4.4.2 核心流程

```
visit_styled(content, local(样式列表), outer(外层链)):
    local 为空 → 直接 visit(content, outer)
    遍历 local 中的样式:
        DocumentElem 样式 → 只有 Document kind 允许，写入文档信息；
                            否则 bail "document set rules are not allowed ..."
        TextElem 样式     → 顶层时推断文档 locale
        PageElem 样式     → Paged 顶层: pagebreak=true, outside=true
                            （Html 下警告并忽略；容器内 bail）
        Format 专属元素样式 → 写入格式选项；容器内 bail
    若 outside → 把 local 标记为"外部"（可提升到页面级）
    把 local/outer 分配进 arena 延长生命周期
    若 pagebreak → 前置弱分页符（只带截至分页的样式）
    finish_interrupted → visit(content, outer.chain(local)) → finish_interrupted
    若 pagebreak → 后置边界分页符
```

#### 4.4.3 源码精读

入口与空样式短路（[crates/typst-realize/src/lib.rs:591-609](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-realize/src/lib.rs#L591-L609)）：`visit_styled` 的签名把"局部样式列表"与"外层链"分开传；空列表直接透传，是常见快路径。

document/page/format 三类校验（[crates/typst-realize/src/lib.rs:610-670](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-realize/src/lib.rs#L610-L670)）：DocumentElem 样式在容器内触发 "document set rules are not allowed inside of containers"；PageElem 样式在 Paged 文档目标下设 `pagebreak = true` 并把 `outside` 置真——注释称之为 "break free from our show rule cage"（从 show 规则的笼子里破门而出，因为 show 输出里再出现 `set page` 也要产生真实分页）；容器内则 bail "page configuration is not allowed inside of containers"。

outside 提升与 arena（[crates/typst-realize/src/lib.rs:673-684](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-realize/src/lib.rs#L673-L684)）：处于顶层（outside）时把样式标记为可提升到页面级；随后把外层链与新样式分配进 `Arenas`——这就是"样式列表变成链上新链接"的落点。

弱分页包夹（[crates/typst-realize/src/lib.rs:686-709](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-realize/src/lib.rs#L686-L709)）：有 pagebreak 时，前面插一个只带"截至分页样式"的弱分页符，内容 visit 完再补一个边界分页符；中间两处 `finish_interrupted`（[L829-L848](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-realize/src/lib.rs#L829-L848)）按样式涉及的元素收掉被中断的分组。

materialize 的时机（[crates/typst-realize/src/lib.rs:571-573](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-realize/src/lib.rs#L571-L573)）：prepare 尾声把 `styles.chain(map)` 上的 settable 字段固化进元素，之后 show 规则拿到的元素已经带着最终字段值。

调用侧闭环（[crates/typst-layout/src/pages/mod.rs:164-175](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/pages/mod.rs#L164-L175)）：`layout_document` 以 `RealizationKind::Document` 发起整棵树的 realize，拿到 `children`（Pair 列表）后交给 `layout_pages`；嵌套容器则由 [crates/typst-layout/src/flow/mod.rs:153-173](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/flow/mod.rs#L153-L173) 以 `RealizationKind::Fragment` 再次进入 realize——**realize 不只跑一次，每个容器都会带着自己的样式链重新实现化其内容**。

#### 4.4.4 代码实践

**实践目标**：亲眼看到 visit_styled 的边界校验。

1. 写 `page-top.typ`：顶层一句 `#set page(width: 15cm)` 加正文；编译应成功，页面变窄。
2. 写 `page-inner.typ`：`#block[#set page(width: 15cm) 容器内改页面]`；编译：`cargo run -p typst-cli -- compile page-inner.typ out.pdf`。

**需要观察的现象**：第一个正常出 PDF；第二个编译失败，报错信息为 "page configuration is not allowed inside of containers"。

**预期结果**：报错来自 [L653-L656](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-realize/src/lib.rs#L653-L656)：block 的内容走 `RealizationKind::Fragment`，不匹配 Document/Bundle 分支即 bail。（本地运行即可验证。）

#### 4.4.5 小练习与答案

1. **问**：`s.outside` 什么时候为 true，为什么需要它？
   **答**：初始时仅 Document kind；进入 show 规则输出或容器后变 false，遇到顶层 page 样式时重新置真（[L636-L644](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-realize/src/lib.rs#L636-L644)）。它决定样式能否被"提升"到页面级参与分页决策（[L675-L677](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-realize/src/lib.rs#L675-L677)）。
2. **问**：show 规则输出和源码里的 `#text(red)[..]` 走的是同一条挂链路径吗？
   **答**：是。前者在 `visit_show_rules` 尾部显式调 `visit_styled(realized, map, styles)`（[L422](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-realize/src/lib.rs#L422)），后者经 `visit` 的 StyledElem 分支（[L273-L275](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-realize/src/lib.rs#L273-L275)）殊途同归。
3. **问**：为什么 block 布局里还要再跑一次 realize（Fragment kind）？eval 一次性产出后内容不是已经定型了吗？
   **答**：容器的 show 规则可能动态生成内容（如 outline 生成条目、bibliography 生成文献表），这些"晚产"的内容同样要经过 show 规则与分组才能排版；realize 是按需递归进入的，不是全文档一次性完成。

## 5. 综合实践

**任务**：给 realize 加临时日志，对比「有 / 无用户 show heading 规则」两种文档在 verdict 处的裁决差异，直观看到用户规则如何抢占内置规则。本实践只改一处源码、用完即还原——临时打点是调试手段，完成后必须 `git checkout` 恢复，遵守"不修改源码"的约定。

1. **准备两份文档**（示例代码）：

   `doc-a.typ`（无用户规则）：

   ```typst
   = 引言
   这是一段普通文字。
   ```

   `doc-b.typ`（有用户规则）：

   ```typst
   #show heading: it => text(red, it.body)
   = 引言
   这是一段普通文字。
   ```

2. **加打点**（示例代码）。打开 `crates/typst-realize/src/lib.rs`，在 `verdict` 函数返回前——[L529](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-realize/src/lib.rs#L529) 的 `Some(Verdict { prepared, map, step })` 上一行——插入：

   ```rust
   let kind = match &step {
       Some(ShowStep::Recipe(..)) => "user recipe",
       Some(ShowStep::Builtin(_)) => "builtin rule",
       None => "no step",
   };
   eprintln!("[verdict] {} -> {kind}", elem.elem().name());
   ```

3. **重新构建并编译**：

   ```bash
   cargo build -p typst-cli
   cargo run -p typst-cli -- compile doc-a.typ a.pdf 2>log-a.txt
   cargo run -p typst-cli -- compile doc-b.typ b.pdf 2>log-b.txt
   ```

4. **对比日志**：`grep "heading" log-a.txt` 与 `grep "heading" log-b.txt`。
5. **还原源码**：`git checkout -- crates/typst-realize/src/lib.rs`，再 `cargo build -p typst-cli` 恢复原二进制。

**需要观察的现象**：log-a 中 heading 对应 `builtin rule`（走 HEADING_RULE）；log-b 中对应 `user recipe`（用户规则抢占，内置规则不再触发）。此外能看到 text、par 等元素也频繁经过 verdict（多数是 `builtin rule` 或 `no step`）；由于 introspection 多轮迭代，同一元素的日志可能重复出现多轮。

**预期结果**：与 4.2 的裁决规则一致——用户 transformational 规则命中即短路，`step.is_none()` 时才查内置表。两份 PDF 的标题外观差异（红字 vs 默认样式）是同一差异的可见面。

**待本地验证**：日志的具体行数与重复次数取决于迭代轮数与文档规模，属正常现象；若 `match &step` 因编译器版本报借用错误，可改用等价写法（如先 `let kind = ...` 再用 `matches!` 宏判断）。

## 6. 本讲小结

- realize 是 eval 与 layout 之间的"翻译官"：输入 `Content` 树 + `StyleChain`，输出扁平的 `Vec<Pair>`（元素 + 生效样式链）；paragraph、list、CiteGroup 都是在这里由分组规则组装、再重新进入 visit 的。
- `visit` 流水线的关卡顺序有讲究：kind 规则 → show 规则 → 序列/样式展开 → 分组 → 过滤；语义包装先溶解、后分组。
- show 裁决三原则：show-set 全生效、用户 transformational 规则只挑一条（带 `RecipeIndex` 守卫防自递归）、内置 `NativeRuleMap` 兜底；错误延迟到迭代收敛后再报。
- `prepare` 是每个元素的一次性仪式：Location 分配、show-set、字段合成、materialize、成对 Tag。
- 内置规则表按 `(Element, Target)` 索引，经 `Routines::rules` → `typst_layout::register` / `typst_html::register` → `Format::register_rules` 三级装配进 `Library.rules`；给元素加默认外观只需在 rules.rs 增改条目，realize 主循环零改动。
- 样式真正挂链发生在 `visit_styled`；document/page/format 样式的合法性边界与页面"破门"（break free）也在这里执法；每个容器布局时会以 Fragment kind 递归再跑一次 realize。

## 7. 下一步学习建议

realize 的输出 `Vec<Pair>` 紧接着被 `layout_pages` / `layout_flow` 消费——下一讲 **u4-l3 页面与文档：layout_document 与 PagedDocument** 正是从 [crates/typst-layout/src/pages/mod.rs:164](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-layout/src/pages/mod.rs#L164) 这个调用点继续往下读。建议：

1. 先读 `crates/typst-layout/src/pages/mod.rs` 的 `layout_document` 与 `layout_pages`，看 Pair 列表如何被切成页；
2. 再读 `crates/typst-layout/src/flow/mod.rs`，看每个 Pair 如何按元素类型分派（本讲 4.4.3 已见其入口）；
3. 想先验证 show 规则行为的话，跑一遍 `cargo testit show-selector-basic`——测试位于 [tests/suite/styling/show.typ](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/tests/suite/styling/show.typ#L3)，分节头 `--- show-selector-basic paged ---` 即测试名，用法见 u1-l5。
