# RealizationKind 各模式深入对比

## 1. 本讲目标

本讲是专家层的第四篇。前面 u2-l6 已经建立了「四张静态分组规则表 + 三档优先级」的骨架，u2-l8 已经讲过 `is_fully_inline_or_neutral` 与 Fragment 行内回退的雏形。本讲把视角拉高，**从 `RealizationKind` 这个『场景标签』出发**，把五种具现化场景逐一对比清楚。

学完本讲，你应当能够：

1. 准确说出 `Bundle` / `Document` / `Fragment` / `Par` / `Math` 五种 kind 各自**选用哪张规则表**、**`outside` 初值是什么**，并能解释为什么是这张表。
2. 读懂 `visit_kind_rules`，说清 **math 与非 math 两条分支各自做什么内容改写**，以及它为什么必须排在 show 规则之前。
3. 掌握 **Fragment 的 `Block`/`Inline` 自动判定逻辑**（`is_fully_inline_or_neutral` 的四个条件）与 `finish()` 中的回退路径，理解 `saw_parbreak` 为何能取消回退。

---

## 2. 前置知识

本讲默认你已经读过 u2-l6（分组规则框架）与 u2-l8（段落分组与 ParElem 构建）。为避免遗忘，先用三句话回顾关键概念：

- **RealizationKind（具现化场景标签）**：每次调用 `realize()` 都带一个 `kind`，它标识「这次具现化发生在什么上下文里」——是整篇文档的根、还是某个容器内部、还是段落/数学内部。它定义在 [crates/typst-library/src/routines.rs:154-169](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/routines.rs#L154-L169)。
- **四张静态规则表**：`BUNDLE_RULES`（空）、`FLOW_RULES`（全）、`PAR_RULES`（去 PAR）、`MATH_RULES`（去 PAR 与 TEXTUAL）。它们之间存在逐级裁剪关系 `FLOW ⊃ PAR ⊃ MATH`。优先级只有三档 `{1,2,3}`，严格更高才嵌套，因此分组栈深度上限 `MAX_GROUP_NESTING = 3`。
- **`outside` 标志**：标记「内容当前是否在文档最外层、且非 show 规则产物」。只有它为真时，页面级样式才能在排版期被提升（lift）到 page 层。详见 u2-l5。
- **FragmentKind**：Fragment 具现化回填的结果枚举，只有 `Inline` / `Block` 两值，定义在 [crates/typst-library/src/routines.rs:172-180](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/routines.rs#L172-L180)。

如果上面任何一条你觉得陌生，建议先回到对应讲义复习，再继续本讲。

---

## 3. 本讲源码地图

本讲只精读两个文件：

| 文件 | 作用 |
|------|------|
| [crates/typst-realize/src/lib.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs) | realize 的全部主逻辑：入口分发、`visit_kind_rules`、`finish`、`is_fully_inline_or_neutral`、四张规则表 |
| [crates/typst-library/src/routines.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/routines.rs) | `RealizationKind` 与 `FragmentKind` 两个枚举的类型定义（它们「概念上」属于 realize，但为支持 crate 拆分而放在 routines 里） |

---

## 4. 核心概念与源码讲解

本讲拆三个最小模块：① `realize()` 的 kind→rules 分发表；② `visit_kind_rules` 的 math/非 math 改写；③ Fragment 的 inline 回退。

### 4.1 kind → rules 映射：realize() 的分发表

#### 4.1.1 概念说明

`realize()` 是整条具现化流水线的唯一对外入口，但它要服务五种截然不同的场景：渲染整篇文档、渲染某个容器（block/html.div）、渲染段落内部、渲染数学内部、以及把内容打包成 bundle。这些场景共用同一套 `visit()` 调度骨架，差异全部浓缩在一个 `kind` 参数里。

`kind` 在 `realize()` 入口处做了两件「一锤定音」的事：

1. **选定一张静态分组规则表**——决定本次具现化里哪些分组规则生效，从而决定能生成哪些合成元素（ParElem、ListElem 等）。
2. **设定 `outside` 初值**——决定页面级样式能否被提升。

可以把它理解成一张「分发表」：同一个 `kind` 进去，同一套行为出来。

#### 4.1.2 核心流程

`realize()` 的分发逻辑可以用下面的伪代码概括：

```
fn realize(kind, ...):
    state.rules = match kind:        # ① 选表
        Bundle      => BUNDLE_RULES  # 空
        Document    => FLOW_RULES    # 全
        Fragment    => FLOW_RULES    # 全
        Par         => PAR_RULES     # 去 PAR
        Math        => MATH_RULES    # 去 PAR + TEXTUAL
    state.outside = (kind is Document)  # ② 仅 Document 为 true
    visit(state, content, styles)
    finish(state)
    return state.sink
```

五张表与五种 kind 的对应关系，以及由此推导出的**最大分组嵌套深度**，汇总如下（优先级：TEXTUAL=3，CITES/LIST/ENUM/TERMS=2，PAR=1；严格更高才嵌套）：

| kind | 规则表 | 生效的分组规则 | 最大嵌套深度 |
|------|--------|---------------|------------|
| `Bundle` | `BUNDLE_RULES` | （空） | 0 |
| `Document` | `FLOW_RULES` | TEXTUAL, PAR, CITES, LIST, ENUM, TERMS | 3 |
| `Fragment` | `FLOW_RULES` | TEXTUAL, PAR, CITES, LIST, ENUM, TERMS | 3 |
| `Par` | `PAR_RULES` | TEXTUAL, CITES, LIST, ENUM, TERMS | 2 |
| `Math` | `MATH_RULES` | CITES, LIST, ENUM, TERMS | 1 |

几个值得记住的观察：

- **`Document` 与 `Fragment` 用同一张表**（`FLOW_RULES`）。因为容器（block、html.div）和整篇文档一样，都是「块级上下文」，段落会在其中自然形成。两者的差异不在分组，而在 `outside` 初值与回填目标（见 4.1.3 与 4.3）。
- **逐级裁剪**：`FLOW_RULES ⊃ PAR_RULES ⊃ MATH_RULES`。`Par` 去掉 PAR（段落不嵌套段落），`Math` 再去掉 TEXTUAL（数学里正则 show 规则改为逐元素处理，见 4.2）。
- **最大嵌套深度随表收窄而下降**：`FLOW` 可达 3 层（TEXTUAL 套在列表/PAR 之上），`PAR` 表无 PAR 故至多 2 层，`MATH` 表只剩同级 priority=2 的规则、互不嵌套故只有 1 层。这正是 `MAX_GROUP_NESTING = 3` 这个常量能 cover 全部场景的原因。
- **`Bundle` 是特例**：它把内容具现成「文档与资源」而不做任何分组，所以表为空，`groupings` 栈永远不增长。

#### 4.1.3 源码精读

入口函数 `realize()` 把 `kind` 同时用于选表和设初值——见 [crates/typst-realize/src/lib.rs:43-74](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L43-L74)，其中关键两段：

[crates/typst-realize/src/lib.rs:55-61](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L55-L61)：依 `kind` 选用四张静态表之一。注意 `Document` 与 `Fragment` 都映射到 `FLOW_RULES`。

```rust
rules: match kind {
    RealizationKind::Bundle => BUNDLE_RULES,
    RealizationKind::Document { .. } => FLOW_RULES,
    RealizationKind::Fragment { .. } => FLOW_RULES,
    RealizationKind::Par => PAR_RULES,
    RealizationKind::Math => MATH_RULES,
},
```

[crates/typst-realize/src/lib.rs:64](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L64)：`outside` 初值仅 `Document` 为真。

```rust
outside: matches!(kind, RealizationKind::Document { .. }),
```

四张表本身的定义收在文件末尾——[crates/typst-realize/src/lib.rs:1005-1015](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L1005-L1015)，可以清楚看到逐级裁剪：

```rust
static BUNDLE_RULES: &[&GroupingRule] = &[];
static FLOW_RULES:   &[&GroupingRule] = &[&TEXTUAL, &PAR, &CITES, &LIST, &ENUM, &TERMS];
static PAR_RULES:    &[&GroupingRule] = &[&TEXTUAL, &CITES, &LIST, &ENUM, &TERMS];
static MATH_RULES:   &[&GroupingRule] = &[&CITES, &LIST, &ENUM, &TERMS];
```

五种 kind 的语义则写在枚举的文档注释里——[crates/typst-library/src/routines.rs:154-169](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/routines.rs#L154-L169)。注意 `Document` 与 `Fragment` 各自带一个 `&mut` 引用，分别用来**回填** `DocumentInfo` 与 `FragmentKind`：

```rust
pub enum RealizationKind<'a> {
    Bundle,
    Document { info: &'a mut DocumentInfo },
    Fragment { kind: &'a mut FragmentKind },
    Par,
    Math,
}
```

这两个可变引用是 kind 之间最本质的差异之一：`Document` 把元信息写进 `info`，`Fragment` 把「是否全行内」的判决写进 `kind`，而 `Bundle`/`Par`/`Math` 不携带任何回填通道。

#### 4.1.4 代码实践

**实践目标**：亲眼看到五种 kind 与所选规则表、`outside` 初值的对应关系。

**操作步骤**：

1. 在 [crates/typst-realize/src/lib.rs:51](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L51) `let mut s = State {` 之前，临时插入一行诊断日志：

   ```rust
   eprintln!("[realize] kind={:?} rules_len={} outside_init={}",
       kind,
       match kind {
           RealizationKind::Bundle => BUNDLE_RULES.len(),
           RealizationKind::Document { .. } | RealizationKind::Fragment { .. } => FLOW_RULES.len(),
           RealizationKind::Par => PAR_RULES.len(),
           RealizationKind::Math => MATH_RULES.len(),
       },
       matches!(kind, RealizationKind::Document { .. }));
   ```

   （`RealizationKind` 的 `Debug` 派生未启用时，可改为手动匹配打印变体名；本步仅为观察。）

2. 用一个最小文档编译，例如 `typst compile` 一份含普通段落、一个 `block`、一段数学 `$a^2$` 的 `.typ` 文件。

**需要观察的现象**：日志里会按嵌套顺序出现多条 `[realize]` 行——根 `Document`（rules_len=6, outside=true）→ 容器 `Fragment`（rules_len=6, outside=false）→ 数学 `Math`（rules_len=4, outside=false），偶尔还有段落内的 `Par`（rules_len=5）。

**预期结果**：规则表长度依次为 6/6/4/5，与上表一致；`outside` 只有 `Document` 那行打印 `true`。

> 若不确定本地能否编译整条工具链，可只做静态阅读：在 `realize()` 入口对照上表人工推演，标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Par` kind 用的 `PAR_RULES` 要去掉 `PAR` 规则本身？

**参考答案**：因为 `Par` kind 表示「我们已经在段落内部」做具现化（由 `finish_par` 生成 `ParElem` 后，对其 `body` 再做一次 `Par` 具现化）。段落不能嵌套段落，若保留 `PAR` 规则，行内元素又会被收集成一个新的 `ParElem`，形成无意义嵌套。去掉后，行内元素直接留在 sink 顶层（随后由 `finish` 做 `collapse_spaces`）。

**练习 2**：`Bundle` 的规则表为空，意味着 `groupings` 栈永远为空。这是否会让分组元素（如 `ParElem`）彻底消失？

**参考答案**：不会消失，只是不会在 Bundle 具现化内「合成」。Bundle 通常在 Document 具现化**之后**对成品元素再跑一次，此时 `ParElem` 等已经是 well-known 元素，会经 `visit()` 的兜底 `push` 直接落入 sink，无需分组。

---

### 4.2 visit_kind_rules：math 与非 math 的内容改写

#### 4.2.1 概念说明

`visit()` 调度流水线的第 2 步是 `visit_kind_rules`（排在 TagElem 直推之后、show 规则之前）。它专门处理「因 kind 不同而需要改写的内容」——这些改写**与 show 规则无关**，但必须先于 show 规则发生，否则下游会拿到错误类型的元素。

`visit_kind_rules` 内部按 `s.kind` 二分：只有 `Math` 走 math 分支，其余四种（Bundle/Document/Fragment/Par）共用 else 分支。换言之，真正「按 kind 分叉」的不是五种而是两类：**math 与非 math**。

#### 4.2.2 核心流程

**math 分支**做两件事：

1. **透明展开 `EquationElem`**：在数学内部遇到嵌套的 `$...$`（EquationElem）时，不把它当作整体，而是递归进它的 `body`，让数学内容直接流淌。这是为了让下面的写法成立：
   ```
   #let my = $pi$
   $ my r^2 $
   ```
   这里 `my` 绑定到一个 `EquationElem`，在 `$ my r^2 $` 的数学具现化里，需要把它的 `body`（`pi`）透明地展开到当前数学流里。
2. **逐元素应用正则 show 规则**：对 `SymbolElem`/`TextElem` 调 `find_regex_match_in_str` 直接在单个元素上找正则匹配。这取代了非 math 场景的 `TEXTUAL` 分组——因此 `MATH_RULES` 里没有 TEXTUAL。

**非 math 分支**也做两件事：

1. **把数学元素包成 `EquationElem`**：任何实现了 `Mathy` 的元素（即数学内容，如行内 `$x$`），只要它还不是 `EquationElem`，就包一层 `EquationElem` 再递归 visit。
2. **把 `SymbolElem` 转成 `TextElem`**：符号元素在非数学排版里没有直接处理路径，统一转成文本元素，并把可能存在的 label 一并迁移。

**为什么必须排在 show 规则之前？** 因为 TEXTUAL 分组和文本类 show 规则都作用于 `TextElem`，而 `SymbolElem` 必须先转成 `TextElem` 才能被它们正确处理（TEXTUAL 规则的注释也明确写道：「`SymbolElem` 在文本 show 规则运行前就转成了 `TextElem`」）。同理，数学元素需要先包成 `EquationElem`，其内置 show 规则才能接管。

#### 4.2.3 源码精读

`visit_kind_rules` 的位置与签名见 [crates/typst-realize/src/lib.rs:297-349](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L297-L349)。它在 `visit()` 中的调用点——[crates/typst-realize/src/lib.rs:255-257](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L255-L257)——紧随 TagElem 直推、早于 show 规则：

```rust
if visit_kind_rules(s, content, styles)? {
    return Ok(());
}
```

**math 分支**——[crates/typst-realize/src/lib.rs:302-327](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L302-L327)：先透明展开 EquationElem，再对单个 Symbol/Text 元素做正则匹配：

```rust
if let RealizationKind::Math = s.kind {
    // 透明展开嵌套 equation
    if let Some(elem) = content.to_packed::<EquationElem>() {
        visit(s, &elem.body, styles)?;
        return Ok(true);
    }
    // 逐元素正则（取代 TEXTUAL 分组）
    if let Some(elem) = content.to_packed::<SymbolElem>() {
        if let Some(m) = find_regex_match_in_str(elem.text.as_str(), styles) {
            visit_regex_match(s, &[(content, styles)], m)?;
            return Ok(true);
        }
    } else if let Some(elem) = content.to_packed::<TextElem>()
        && let Some(m) = find_regex_match_in_str(&elem.text, styles)
    {
        visit_regex_match(s, &[(content, styles)], m)?;
        return Ok(true);
    }
}
```

注意这里把元素包成单元素切片 `&[(content, styles)]` 传给 `visit_regex_match`——与非 math 场景里 TEXTUAL 分组攒出多元素切片不同，math 的正则只作用在「单个」元素上。

**非 math 分支**——[crates/typst-realize/src/lib.rs:328-346](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L328-L346)：把 Mathy 包成 EquationElem、把 SymbolElem 转成 TextElem：

```rust
} else {
    // 数学内容 → EquationElem
    if content.can::<dyn Mathy>() && !content.is::<EquationElem>() {
        let eq = EquationElem::new(content.clone()).pack().spanned(content.span());
        visit(s, s.store(eq), styles)?;
        return Ok(true);
    }
    // 符号 → 文本
    if let Some(elem) = content.to_packed::<SymbolElem>() {
        let mut text = TextElem::packed(elem.text.clone()).spanned(elem.span());
        if let Some(label) = elem.label() { text.set_label(label); }
        visit(s, s.store(text), styles)?;
        return Ok(true);
    }
}
```

两段都通过 `s.store(...)` 把新元素寿命延长到 arena（见 u3-l3），再 `visit` 重新喂回流头。

#### 4.2.4 代码实践

**实践目标**：验证 math 与非 math 两条改写路径分别被触发，并产出不同元素类型。

**操作步骤**：

1. 在 [crates/typst-realize/src/lib.rs:309](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L309)（math 分支的 EquationElem 展开处）与 [crates/typst-realize/src/lib.rs:338](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L338)（非 math 的 SymbolElem→TextElem 处）各插一行 `eprintln!`，打印元素类型名（如 `content.elem().name()`）。
2. 准备一份测试文档：
   ```typst
   #let my = $pi$
   $ my r^2 $        // 触发 math 分支的 EquationElem 透明展开

   符号：#sym.alpha  // 触发非 math 分支的 SymbolElem→TextElem
   ```
3. 编译并观察日志。

**需要观察的现象**：第一段数学里，`my`（一个 EquationElem）会在 math 分支被展开；正文里的 `#sym.alpha` 会在非 math 分支被转成 `TextElem`。

**预期结果**：math 分支日志打印的是被展开的 EquationElem 的 body 元素；非 math 分支日志打印 `SymbolElem` 随即转成 `TextElem`。若没有看到对应行，说明该元素走了更早的分支（如已 prepared），需要回溯确认。

> 行号与分支命中关系以本地实际编译为准，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 math 分支用 `find_regex_match_in_str` 逐元素匹配，而不是像非 math 那样用 TEXTUAL 分组跨元素匹配？

**参考答案**：因为在数学里，相邻的文本/符号元素并不构成一个「连续字符串」——它们之间往往有结构性的数学元素（如运算符、上下标）。跨元素拼接字符串再做正则匹配在语义上是错的（会把不相连的字符合到一起）。因此 math 改为在「单个」元素上匹配，并相应地把 TEXTUAL 规则从 `MATH_RULES` 中移除。

**练习 2**：非 math 分支里 `content.can::<dyn Mathy>() && !content.is::<EquationElem>()` 的第二个条件有什么用？

**参考答案**：防止对已经是 `EquationElem` 的内容重复包装。`EquationElem` 自身也满足 `Mathy`，若不加这个守卫，会无限递归地把 EquationElem 包进新的 EquationElem。这与「show 规则防重入」是同一类问题，但这里用类型判断直接短路。

---

### 4.3 Fragment 的 inline 回退优化

#### 4.3.1 概念说明

`Fragment` kind 用于「容器内部」的具现化（如 `block`、`html.div` 的内容）。它和 `Document` 用同一张 `FLOW_RULES` 表，但有两点独特之处：一是 `outside` 初值为 `false`（容器不在文档最外层）；二是携带一个 `&mut FragmentKind`，用来把**「这段容器内容是否完全由行内元素组成」的判决**回填给调用方。

`FragmentKind` 只有两值——[crates/typst-library/src/routines.rs:172-180](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/routines.rs#L172-L180)：

- `Block`：内容含非行内元素，行内内容被强制包成段落，产物是块级的。
- `Inline`：内容完全行内，**不**生成 `ParElem`，产物保持行内。

为什么要做这个判定？因为一个容器的内容可能是纯文本（如 `#[粗体]文字`），也可能是带段落的块级内容。如果无脑把纯行内内容也包成 `ParElem`，下游排版就要按块级处理，丢失「这段其实可以和其他行内内容排在一起」的信息。回退优化让纯行内的 fragment 保持行内，使行内容器（如行内的 `box`）工作正常。

#### 4.3.2 核心流程

回退的核心是一个谓词 `is_fully_inline_or_neutral` 加 `finish()` 里的一段特判。谓词要同时满足**四个条件**才判定为「全行内」：

1. **kind 是 `Fragment`**——只有 Fragment 才回退；Document 即便全行内也照常生成段落（整篇文档总要分页排版）。
2. **没有遇到过 `ParbreakElem`**（`!s.saw_parbreak`）——空行（段落分隔）是用户「我要分段」的明确意图，一旦出现就不再回退。`saw_parbreak` 在 `visit_filter_rules` 里被置位。
3. **当前有且仅有一个活动分组，且它是 `PAR`**（`let [grouping] = s.groupings.as_slice()` 且 `ptr::eq(grouping.rule, &PAR)`）——说明所有行内元素被收进唯一一个段落分组，没有列表/引用等并列分组。
4. **该 PAR 分组之前的 sink 前缀全部是 tag 或 neutral 元素**——前缀里不能有别的块级内容。

四个条件全满足时，`finish()` 会：把回填的 `FragmentKind` 改写成 `Inline`、**弹出**那个 PAR 分组（放弃生成 `ParElem`）、对 sink 做 `collapse_spaces`，并返回 `false` 让 `finish_grouping_while` 停止迭代。

此外，`finish()` 末尾还有一段与 kind 相关的处理：对 `Par` 和 `Math` kind，在顶层做一次 `collapse_spaces`（因为这两种 kind 里空格是顶层元素，不会被 `finish_par` 收走）。

#### 4.3.3 源码精读

谓词 `is_fully_inline_or_neutral`——[crates/typst-realize/src/lib.rs:1173-1186](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L1173-L1186)，四个条件用 `let` 链一次性串起：

```rust
fn is_fully_inline_or_neutral(s: &State) -> bool {
    if let RealizationKind::Fragment { .. } = s.kind       // ① Fragment
        && !s.saw_parbreak                                  // ② 无 parbreak
        && let [grouping] = s.groupings.as_slice()          // ③ 唯一 PAR 分组
        && std::ptr::eq(grouping.rule, &PAR)
        && s.sink[..grouping.start].iter().all(|(c, _)| {   // ④ 前缀全 tag/neutral
            c.is::<TagElem>() || (grouping.rule.effect)(c) == GroupingEffect::Neutral
        })
    { true } else { false }
}
```

`saw_parbreak` 的置位点在过滤规则——[crates/typst-realize/src/lib.rs:766-771](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L766-L771)，`ParbreakElem` 被记录为分组边界、置位 `saw_parbreak` 但不入 sink：

```rust
} else if content.is::<ParbreakElem>() {
    s.may_attach = false;
    s.saw_parbreak = true;
    return Ok(true);
}
```

回退发生在 `finish()`——[crates/typst-realize/src/lib.rs:788-810](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L788-L810)。注意它把判定作为 `finish_grouping_while` 的循环条件传入，命中时改写 `FragmentKind` 并 `pop` 分组：

```rust
fn finish(s: &mut State) -> SourceResult<()> {
    finish_grouping_while(s, |s| {
        if is_fully_inline_or_neutral(s) {
            if let RealizationKind::Fragment { kind } = &mut s.kind {
                **kind = FragmentKind::Inline;   // 回填 Inline
            }
            s.groupings.pop();                    // 放弃 ParElem
            collapse_spaces(&mut s.sink, 0);
            false                                 // 停止迭代
        } else {
            !s.groupings.is_empty()              // 否则继续收尾剩余分组
        }
    })?;

    // Par/Math 顶层空格折叠
    if matches!(s.kind, RealizationKind::Par | RealizationKind::Math) {
        collapse_spaces(&mut s.sink, 0);
    }
    Ok(())
}
```

这里的 `**kind` 双层解引用：外层 `kind` 是 `&mut &'a mut FragmentKind`（`RealizationKind::Fragment { kind }` 模式绑定出的是 `&mut &mut FragmentKind`，因为 `s.kind` 本身已被 `&mut` 借用），内层才是真正的 `FragmentKind`。

#### 4.3.4 代码实践

**实践目标**：对比 `Document` 与 `Fragment` 两种 kind 的规则表与 `outside` 初值；并亲手触发 Fragment 的 inline 回退，验证 `FragmentKind` 被改写成 `Inline`。

**操作步骤（第一部分：对比两种 kind）**：

1. 阅读 [crates/typst-realize/src/lib.rs:55-64](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L55-L64)，填出下表（答案应与 4.1.2 一致）：

   | kind | rules 表 | outside 初值 | 回填目标 | 有 inline 回退？ |
   |------|---------|------------|---------|---------------|
   | Document | ？ | ？ | DocumentInfo | ？ |
   | Fragment | ？ | ？ | FragmentKind | ？ |

2. 结论：两者**同表（FLOW_RULES）**、**`outside` 不同（Document=true, Fragment=false）**、**回填目标不同**、**只有 Fragment 有 inline 回退**。

**操作步骤（第二部分：触发回退）**：

3. 在 [crates/typst-realize/src/lib.rs:793](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs#L793) 的 `**kind = FragmentKind::Inline;` 前插入 `eprintln!("[fallback] Fragment → Inline");`。
4. 写一个**完全行内**的容器内容，例如：

   ```typst
   一个行内盒子：#box[粗体 _斜体_ 文本]。
   ```

   以及一个**会破坏回退**的对照文档（含段落分隔）：

   ```typst
   #box[
     第一段。

     第二段。
   ]
   ```

5. 编译两者，观察日志。

**需要观察的现象**：第一个文档（纯行内）应触发 `[fallback] Fragment → Inline`，容器内容不被包成 `ParElem`；第二个文档因含空行（`ParbreakElem`）使 `saw_parbreak=true`，回退被取消，`FragmentKind` 保持 `Block`。

**预期结果**：日志只在第一个文档出现；第二个文档不出现该行。若两个都出现或都不出现，需检查 `is_fully_inline_or_neutral` 的四个条件是否被正确满足（尤其 `saw_parbreak` 与唯一 PAR 分组两项）。

> 上述日志现象以本地编译为准，标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `is_fully_inline_or_neutral` 里的 `!s.saw_parbreak` 条件去掉，会出现什么问题？

**参考答案**：用户用空行明确表达「这里要分段」时，回退仍会生效，把本应分成两段的容器内容强行当成一个行内片段、不生成 `ParElem`，导致段落分隔丢失、两段被合并排版。`saw_parbreak` 正是为了尊重用户的分段意图而设的「否决票」。

**练习 2**：为什么 `Document` kind 即便内容全行内，也不做这个回退？

**参考答案**：整篇文档总要进入分页排版流程，必须以段落（`ParElem`）为基本排版单元。Document 的职责是产出可分页的块级流，而非行内片段；且 `RealizationKind::Document` 模式不匹配 `Fragment { kind }`，回退分支根本不会执行改写。回退是**容器**场景下「避免无谓段落包装」的优化，对文档根没有意义。

**练习 3**：`finish()` 末尾为什么只对 `Par` 和 `Math` kind 做 `collapse_spaces`？

**参考答案**：在 `Par`/`Math` 具现化里，`SpaceElem` 是顶层元素（直接落在 sink），不会被某个 `finish_par` 收走折叠；而 `Document`/`Fragment` 的顶层空格会在段落分组的 `finish_par` 里被 `collapse_spaces` 处理。所以只有 `Par`/`Math` 需要在 `finish()` 里补做一次顶层折叠。`Bundle` 不做分组也不关心空格，故不在其列。

---

## 5. 综合实践

**任务**：用一张「kind 决策表」把本讲三个模块串起来，并用源码追踪验证。

请你完成以下步骤：

1. **建表**：画一张表，行为五种 kind，列至少包含：选用规则表、`outside` 初值、`visit_kind_rules` 走哪条分支、是否回填及回填目标、最大分组嵌套深度、是否做顶层 `collapse_spaces`。全部内容必须能在 [crates/typst-realize/src/lib.rs](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-realize/src/lib.rs) 中找到出处。

2. **追踪一条 Fragment 调用链**：写一份文档 `#box[行内文本]`，按下面顺序在源码中标注每一步发生的位置：
   - `realize()` 选表与设 `outside`（4.1.3）；
   - 行内文本先进 TEXTUAL、由 `finish_textual` 播种出 PAR 分组（回顾 u2-l8）；
   - `finish()` 命中 `is_fully_inline_or_neutral`、改写 `FragmentKind::Inline` 并 `pop` 分组（4.3.3）。

3. **构造反例**：分别给出会**破坏**回退四个条件中任意一个的最小文档（例如插入 `ParbreakElem` 破坏条件②，插入一个列表破坏条件③），并预测每种情况下 `FragmentKind` 的最终取值。

   参考结论：只要任一条件被破坏，`FragmentKind` 保持 `Block`，容器内容被正常包成 `ParElem`。

> 本任务以源码阅读与人工推演为主；若本地可编译，可在 `finish()` 的回退分支与 `realize()` 入口加日志佐证。

---

## 6. 本讲小结

- `realize()` 入口用一张 `match` 把 `kind` 映射到四张静态规则表：`Bundle→空`、`Document/Fragment→FLOW`、`Par→PAR`、`Math→MATH`，呈现 `FLOW ⊃ PAR ⊃ MATH` 的逐级裁剪；`outside` 初值**仅 `Document` 为真**。
- 最大分组嵌套深度随规则表收窄而下降（FLOW=3、PAR=2、MATH=1、Bundle=0），这正是 `MAX_GROUP_NESTING=3` 能覆盖全部场景的原因。
- `visit_kind_rules` 是 `visit()` 第 2 步，按 **math/非 math** 二分：math 透明展开 `EquationElem` 并逐元素做正则匹配；非 math 把 `Mathy` 内容包成 `EquationElem`、把 `SymbolElem` 转成 `TextElem`。它必须早于 show 规则。
- `Document` 与 `Fragment` 同表但不同命：前者 `outside=true` 且回填 `DocumentInfo`，后者 `outside=false` 且回填 `FragmentKind`，并独享 inline 回退。
- Fragment 的 inline 回退由 `is_fully_inline_or_neutral` 的**四条件**（Fragment + 无 parbreak + 唯一 PAR 分组 + 前缀全 tag/neutral）触发，命中时在 `finish()` 里把 `FragmentKind` 改写成 `Inline` 并放弃生成 `ParElem`。
- `finish()` 还对 `Par`/`Math` kind 补做一次顶层 `collapse_spaces`，因为这两种 kind 里空格是顶层元素。

---

## 7. 下一步学习建议

本讲把五种 kind 在 realize **内部**的差异讲透了，但尚未回答「这五种 kind 分别被谁、在什么场景下调用」。这正是下一讲 **u3-l5《与 layout / html / bundle / math 的集成》** 的主题：它会梳理 `Routines::realize` 这个函数指针在 `typst-layout`（flow/inline/pages）、`typst-html`（fragment/document）、`typst-bundle`、`typst-library`(math) 中的各处调用点，画出调用关系图。

建议你在进入 u3-l5 之前：

- 回看本讲的「kind 决策表」，带着「每个调用点为什么选这个 kind」的问题去读 u3-l5；
- 复习 u3-l3 关于 `Arenas` 与多生命周期的内容，因为下游调用方正是 `Arenas` 的创建者，理解它能帮你读懂调用点的参数构造。
