# 特殊场景 tooltip：字体、标签、import、闭包捕获

## 1. 本讲目标

在 [u3-l2](u3-l2-tooltip-dispatch.md) 中我们看到，`tooltip` 入口用一条短路分发链把光标交给不同的分支；在 [u3-l3](u3-l3-expr-tooltip.md) 中精读了其中最通用、也最昂贵的 `expr_tooltip`。本讲把分发链里其余四个「特殊场景」分支一次性讲透：

- `font_tooltip`：悬停在 `#text(font: "Arial")` 的字符串上，给出该字体家族的摘要。
- `summarize_font_family`：把一个字体家族下的所有变体压成一行摘要（`font_tooltip` 的数据引擎）。
- `label_tooltip`：悬停在 `<label>` 或 `@ref` 上，给出该标签的详情（如 figure 的 caption）。
- `import_tooltip`：悬停在 `#import "x.typ": *` 的星号 `*` 上，列出会导入哪些名字。
- `closure_tooltip`：悬停在闭包的 `=` 或 `=>` 上，列出该闭包捕获了哪些外部变量。

学完本讲，你应当能够：

1. 说出这四类特殊 tooltip 各自的触发条件、数据来源和返回内容。
2. 理解 `closure_tooltip` 为什么只在 `=` / `=>` 上触发（降噪设计），且为什么它不需要 `world`、不需要 `output`。
3. 明白 `import_tooltip` 如何借助 `analyze_import` 真正解析模块、再枚举星号导入的名字。
4. 看懂 `summarize_font_family` 如何把多个 `FontInfo` 变体聚合为「Variable. Weight 100–900. Has italics.」这样的单行摘要。

## 2. 前置知识

本讲假设你已掌握以下内容（来自前序讲义）：

- **`tooltip` 的短路分发链与 `Tooltip::Text/Code` 两类返回值**（u3-l2）。
- **`Side::Before/After`、`LinkedNode`、`leaf.kind()`** 等光标定位与节点判定（u2-l1）。
- **`analyze_import`：把 import 源解析成模块 `Value`**（u2-l4）。
- **`analyze_labels`：从编译产物里收集文档标签与参考文献，返回 `(labels, split)`**（u2-l4）。
- **`summarize_font_family` 与 `MetricRange`** 作为 utils 工具集的一员（u2-l5，本讲会深入展开）。

几个会反复出现的术语：

- **`SyntaxKind`**：typst-syntax 给每个语法节点打的「种类」标签，如 `Str`、`Star`、`Label`、`RefMarker`、`Eq`、`Arrow`、`Closure`、`Named`。本讲大量使用 `leaf.kind() == ...` 来做触发判定。
- **`ast::Str` / `ast::Named` / `ast::ModuleImport`**：无类型 `SyntaxNode` 经 `cast()` 得到的强类型 AST 视图（u2-l2 已介绍过 `cast`）。
- **`world.book()`**：`World` 必填方法，返回字体簿 `&FontBook`，可枚举字体家族（u1-l2）。
- **`CapturesVisitor`**：typst-eval 提供的访问器，遍历一段语法子树，收集其中引用的外部变量（即闭包捕获）。
- **best-effort / 降噪**：IDE 功能追求「能给的尽量给、会给错或太吵的就不给」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/tooltip.rs` | tooltip 总入口与全部分支，本讲的 `font_tooltip` / `label_tooltip` / `import_tooltip` / `closure_tooltip` 都在这里。 |
| `src/utils.rs` | 共享工具集；本讲的 `summarize_font_family` 与辅助类型 `MetricRange` 在这里。 |
| `src/analyze.rs` | `analyze_import`（被 `import_tooltip` 调用）与 `analyze_labels`（被 `label_tooltip` 调用）的数据来源，本讲只复用、不重讲。 |

## 4. 核心概念与源码讲解

先回顾一下分发链，看清这四个分支在整体里的位置：

[tooltip.rs:36-41](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tooltip.rs#L36-L41) —— 六个分支用 `or_else` 串成短路链，**首个命中即返回**：

```rust
named_param_tooltip(world, &leaf)            // ① 具名参数名/字符串值
    .or_else(|| font_tooltip(world, &leaf))   // ② 字体家族摘要
    .or_else(|| label_tooltip(output?, &leaf))// ③ 标签/引用详情（需要 output）
    .or_else(|| import_tooltip(world, &leaf)) // ④ 星号导入项列表
    .or_else(|| expr_tooltip(world, &leaf))   // ⑤ 表达式取值（u3-l3）
    .or_else(|| closure_tooltip(&leaf))       // ⑥ 闭包捕获
```

两点需要先记住，后面会反复用到：

1. **只有 `label_tooltip` 依赖 `output`**：写成 `label_tooltip(output?, &leaf)`，那个 `?` 表示「没有编译产物就直接让整个分支返回 `None`」（优雅降级）。其余三个分支要么只要 `world`，要么连 `world` 都不要。
2. **顺序体现「便宜且具体的在前」**：字体/标签/import 都是局部查表，排在昂贵的 `expr_tooltip`（要重跑整篇文档）之前；`closure_tooltip` 排最后，因为它只在 `=`/`=>` 上触发，而 `expr_tooltip` 不会消费这两个节点，所以不会互相抢。

下面逐个精读。

---

### 4.1 font_tooltip：从字符串定位到字体家族

#### 4.1.1 概念说明

当用户写 `#text(font: "Linux Libertine")[...]` 并把鼠标悬停在 `"Linux Libertine"` 上时，IDE 希望告诉他：「这个字体家族在我本地装了吗？有几个变体？支不支持斜体/可变字重？」。

难点有两个：

- 这个字符串只是普通的 `ast::Str`，语法层面并不知道它「表示一个字体」——它之所以是字体，仅仅因为它是 `font:` 这个具名参数的值。
- 一个家族名可能对应**多个 `FontInfo` 变体**（常规、粗体、斜体、可变字体……），需要一个聚合函数把这些变体压缩成一行人类可读的摘要。

`font_tooltip` 负责第一个难点（定位），`summarize_font_family`（4.2 节）负责第二个难点（聚合）。

#### 4.1.2 核心流程

```
leaf 是 ast::Str?
  └─ 父节点是 ast::Named 且名字 == "font"?
       └─ 在 world.book() 里按家族名（小写）查找
            └─ 命中：取该家族所有字体 id 的 FontInfo 迭代器
                 └─ summarize_font_family(...) → Tooltip::Text(摘要)
            └─ 未命中：返回 None（字体不在本地，不提示）
```

关键设计：**只认 `font:` 这一个具名参数**。其他地方出现的字符串（如 `#read("a.txt")`）即使长得像字体名也不会触发——这是用语法上下文（`parent` 是 `Named` 且 `name == "font"`）来消歧，避免误报。

#### 4.1.3 源码精读

[tooltip.rs:263-284](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tooltip.rs#L263-L284) —— `font_tooltip` 全貌：

```rust
fn font_tooltip(world: &dyn IdeWorld, leaf: &LinkedNode) -> Option<Tooltip> {
    if let Some(string) = leaf.cast::<ast::Str>()        // ① 必须是字符串字面量
        && let lower = string.get().to_lowercase()       //    家族名忽略大小写

        && let Some(parent) = leaf.parent()
        && let Some(named) = parent.cast::<ast::Named>()
        && named.name().as_str() == "font"               // ② 父节点是 font: 具名参数

        && let book = world.book()                        // ③ 取字体簿（World 必填方法）
        && let Some((_, iter)) = book
            .families()
            .find(|&(family, _)| family.to_lowercase().as_str() == lower.as_str())
    {
        let detail = summarize_font_family(iter.filter_map(|id| book.info(id)));
        return Some(Tooltip::Text(detail));
    }
    None
}
```

几个要点：

- `leaf.cast::<ast::Str>()` 把无类型叶子强类型转为字符串 AST 节点；不是字符串就直接落到末尾 `None`。注意它**不限定**父节点一定是 `text(...)` 的参数——任何 `xxx(font: "...")` 都会触发。实践中 `font:` 几乎只出现在 `text`/`text` 派生元素上，所以这种宽松判定是安全的。
- `book.families()` 返回一个迭代器，产出 `(家族名, 该家族字体 id 的迭代器)`。`find` 按小写比对家族名，命中后 `iter` 就是「该家族下所有字体的 id 迭代器」。
- `iter.filter_map(|id| book.info(id))` 把 id 翻译成 `&FontInfo`，交给 4.2 节的聚合函数。

注意它**不需要 `output`**，也不需要 `IdeWorld` 的可选方法 `packages()`/`files()`——只要 `World::book()`（必填），所以字体提示永远不会因数据缺失而降级。

#### 4.1.4 代码实践

**实践目标**：理解 `font_tooltip` 的触发边界——同样是字符串，为什么有的触发、有的不触发。

**操作步骤**（源码阅读 + 本地验证）：

1. 阅读上面的 `font_tooltip` 源码，确认触发需要满足「`Str` + 父为 `font:` 具名参数」两个条件。
2. 假设本地安装了 `DejaVu Sans` 字体（typst 内置测试字体里有它），手算下列三段在指定光标处是否会有字体提示：

   | 源码片段 | 悬停对象 | 你的预测 |
   | --- | --- | --- |
   | `#text(font: "DejaVu Sans")[x]` | 字符串内部 | ? |
   | `#read("DejaVu Sans")` | 字符串内部 | ? |
   | `#text(family: "DejaVu Sans")[x]` | 字符串内部 | ? |

3. 若想用测试验证，可仿照 `tooltip.rs` 末尾 `mod tests` 的 `test(...)` 助手写法，构造一段含 `#text(font: "DejaVu Sans")[x]` 的 world，悬停在字符串上断言 `must_be_text(...)`。

**需要观察的现象 / 预期结果**：

- 第一行：父节点是 `font:` 具名参数 → 触发，返回类似 `"N variants. Weight ..." ` 的摘要。
- 第二行：父节点是普通函数调用的位置参数，不是 `font:` → 不触发，返回 `None`。
- 第三行：具名参数名是 `family` 而非 `font` → 不触发，返回 `None`。

> 摘要的**具体文本**取决于本地字体，精确字符串「待本地验证」；但「是否触发」完全可以由上面的两个条件静态判定。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `font_tooltip` 不需要像 `label_tooltip` 那样依赖 `output`（编译产物）？

**参考答案**：字体家族信息来自 `World::book()`（字体簿，是 `World` 的必填方法），与文档是否编译过无关；而标签详情必须等文档渲染后才能从 introspector 里查到，所以 `label_tooltip` 需要 `output`。

**练习 2**：若用户写 `#let f = "Arial"; #text(font: f)[x]`，悬停在 `f` 上会得到字体摘要吗？

**参考答案**：不会。`font_tooltip` 只在叶子直接是 `ast::Str` 且父为 `font:` 时触发；这里叶子是变量 `Ident`，不是字符串字面量，分支直接返回 `None`（随后会落到 `expr_tooltip`，给出 `f` 的值 `"Arial"` 而非字体摘要）。

---

### 4.2 summarize_font_family：把字体变体压成一行摘要

#### 4.2.1 概念说明

`font_tooltip` 拿到的是一个家族下「若干个 `FontInfo` 变体」的迭代器。这些变体可能涵盖不同的字重（400/700）、不同的宽度（75%–100%）、斜体/正体、是否可变字体、是否支持光学尺寸轴，甚至带自定义轴（如 `SOFT`、`WONK`）。直接把每个变体列出来太啰嗦；用户只想看一句话。

`summarize_font_family` 的任务就是：**遍历一个家族的全部 `FontInfo`，把它们的指标聚合（取并集/范围），输出一行紧凑摘要**。

#### 4.2.2 核心流程

```
初始化各聚合量：count=0, variable=false, has_italics=false,
                has_obliques=false, supports_opsz=false,
                weight_range=MetricRange::空, stretch_range=空, variations=空
for 每个变体 info:
    解析标准轴 StandardAxes::parse(info.axes)
    variable      |= 是否可变字体标志
    has_italics   |= 是斜体 或 有 ital 轴
    has_obliques  |= 是倾斜体 或 有 slnt 轴
    supports_opsz |= 有 opsz 轴
    weight_range   按「静态 variant.weight」与「wght 轴的 [min,max]」整体扩张
    stretch_range  同理用 variant.stretch 与 wdth 轴扩张
    对每个非标准轴（StandardAxes 不认识的自定义轴）记入 variations 并扩张
    count += 1

拼接字符串：
    可变字体 ? "Variable." : "{count} variant(s)."
    + weight_range 非默认 ? " Weight {min}–{max}."
    + stretch_range 非默认 ? " Stretch {min}–{max}."
    + has_italics ? " Has italics."
    + has_obliques ? " Has obliques."
    + supports_opsz ? " Supports optical sizing."
    + 每个自定义轴 ? " {TAG} {range}."
```

其中「扩张」的语义是：让一个 `MetricRange` 的 `[min, max]` 区间始终包含新来的值；`MetricRange` 内部用 `Option<(T,T)>` 表示「还没初始化」的空状态。

#### 4.2.3 源码精读

[utils.rs:41-121](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/utils.rs#L41-L121) —— 聚合主循环与字符串拼接。关键片段：

```rust
for info in variants {
    let axes = StandardAxes::parse(&info.axes);
    variable |= info.flags.contains(FontFlags::VARIABLE);
    has_italics |= info.variant.style == FontStyle::Italic || axes.ital.is_some();
    has_obliques |= info.variant.style == FontStyle::Oblique || axes.slnt.is_some();
    supports_opsz |= axes.opsz.is_some();

    weight_range.expand(info.variant.weight);      // 静态字重
    stretch_range.expand(info.variant.stretch);    // 静态宽度

    if let Some(axis) = axes.wght {
        weight_range.expand_axis(axis, FontWeight::from_wght);  // 可变字重轴
    }
    if let Some(axis) = axes.wdth {
        stretch_range.expand_axis(axis, FontStretch::from_wdth);// 可变宽度轴
    }
    for axis in &info.axes {
        if !StandardAxes::knows(axis.tag) {        // 自定义轴
            variations.entry(axis.tag).or_default()
                .expand_axis(axis, |v| Scalar::new(v.0.into()));
        }
    }
    count += 1;
}
```

注意它**同时处理静态变体和可变字体轴**：静态字体通过 `variant.weight`/`variant.stretch` 给出固定值（`expand` 把单点当 `[v,v]` 并入区间）；可变字体通过 `wght`/`wdth` 轴给出 `[min,max]` 范围（`expand_axis` 把两端分别并入）。最终 `weight_range` 是「这个家族所有形态下字重的最小到最大」。

拼接时，`display_if_not_default` 会**跳过等于默认值**的指标——比如全是字重 400 的家族就不会输出 `Weight` 那一段：

[utils.rs:86-118](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/utils.rs#L86-L118)：

```rust
if variable {
    write!(detail, "Variable.").unwrap();
} else {
    write!(detail, "{count} variant{}.", if count == 1 { "" } else { "s" }).unwrap();
}
if let Some(range) = weight_range.display_if_not_default() {
    write!(detail, " Weight {range}.").unwrap();
}
if let Some(range) = stretch_range.display_if_not_default() {
    write!(detail, " Stretch {range}.").unwrap();
}
if has_italics  { detail.push_str(" Has italics."); }
if has_obliques { detail.push_str(" Has obliques."); }
if supports_opsz{ detail.push_str(" Supports optical sizing."); }
for (tag, range) in variations {
    if let Some(range) = range.display() {          // 自定义轴总是显示（用 display 而非 display_if_not_default）
        write!(detail, " {} {range}.", tag.to_str_lossy()).unwrap();
    }
}
```

`MetricRange` 本身是个小巧的可空区间抽象，用 `Option<(T,T)>` 编码「未初始化」，`expand_range` 维护 min/max：

[utils.rs:142-147](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/utils.rs#L142-L147)：

```rust
fn expand_range(&mut self, min: T, max: T) {
    self.0 = Some(match self.0 {
        None => (min, max),
        Some((prev_min, prev_max)) => (prev_min.min(min), prev_max.max(max)),
    });
}
```

`display`（[utils.rs:150-156](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/utils.rs#L150-L156)）在 `min == max` 时只输出单个值，否则输出 `min–max`（用连字符 `–`）。这正是测试里 `Weight 400–700`、单值家族显示 `Weight 400` 的来源。

这个函数有现成的测试，可以直接看真实输出：

[utils.rs:253-286](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/utils.rs#L253-L286) —— 用 `typst_dev_assets` 提供的内置字体断言摘要文本：

```rust
// 静态字体
assert_eq!(summarize("Cascadia Mono"), "2 variants. Weight 400–700.");
assert_eq!(summarize("HK Grotesk"), "16 variants. Weight 100–900. Has italics.");
assert_eq!(summarize("IBM Plex Sans"), "5 variants. Weight 300–700. Stretch 75%–100%.");
// 可变字体
assert_eq!(summarize("Cantarell"), "Variable. Weight 100–800.");
assert_eq!(summarize("Fraunces"),
    "Variable. Weight 100–900. Has italics. Supports optical sizing. SOFT 0–100. WONK 0–1.");
```

这段测试是理解输出格式最直观的材料：`SOFT 0–100. WONK 0–1.` 就是 Fraunces 的两个自定义轴被原样列出。

#### 4.2.4 代码实践

**实践目标**：动手跑一次 `test_summarize_font_family`，亲眼看到聚合输出。

**操作步骤**：

1. 在 typst-ide crate 目录下运行：
   ```bash
   cargo test --package typst-ide test_summarize_font_family -- --nocapture
   ```
2. 观察输出，确认上面四组断言全部通过。

**需要观察的现象 / 预期结果**：测试通过；若把其中某个家族的断言值改错（例如把 `Weight 400–700` 改成 `Weight 400-700`，注意是连字符 `–` 不是减号 `-`），测试会失败并打印实际值——以此验证你对输出格式的理解。

> 这是少数能直接给出确定输出的实践之一，因为内置字体（`typst-dev-assets`）是固定的。具体字体名是否存在于你的环境「待本地验证」，但测试里列出的家族一定存在。

#### 4.2.5 小练习与答案

**练习 1**：一个家族只有字重 400 的单个静态变体，输出会是怎样的？

**参考答案**：`"1 variant."`——`count == 1` 所以是单数 `variant`；字重 400 是 `FontWeight` 的默认值，`weight_range.display_if_not_default()` 返回 `None`，不输出 `Weight` 段；其余标志全为假，故只有开头的 `1 variant.`。

**练习 2**：为什么自定义轴（`SOFT`、`WONK`）用 `display()`，而字重/字宽用 `display_if_not_default()`？

**参考答案**：字重/字宽有公认的默认值（400 / 100%），全是默认时输出它们对用户没信息量，故过滤；自定义轴没有「默认」概念，只要存在就值得告诉用户，所以一律用 `display()` 显示。

---

### 4.3 label_tooltip：标签与引用的详情

#### 4.3.1 概念说明

Typst 里用 `<label>` 给元素打标签、用 `@ref` 引用它，例如：

```typst
#figure(caption: [Hi])[]<f>
@f
```

用户悬停在 `<f>` 或 `@f` 上时，IDE 想显示这个标签指向的内容的「详情」——对 figure 来说就是它的 caption（这里是 `Hi`）。

这有两个特点：

1. **必须依赖编译产物**。标签指向的 caption 是文档渲染后才知道的（要从 introspector 查 figure 元素），源码静态分析拿不到。所以这条分支在分发链里写成 `label_tooltip(output?, &leaf)`——没有 `output` 就直接放弃。
2. **数据复用 `analyze_labels`**。u2-l4 讲过，`analyze_labels(output)` 返回 `(Vec<(Label, Option<详情>)>, split)`，其中详情来自 figure 的 caption 或元素 body 的纯文本。本分支只取 `.0`（整个 Vec），在里面按名字匹配。

#### 4.3.2 核心流程

```
leaf.kind() 是 RefMarker(@x) ? 去掉前导 @ 得到 target
leaf.kind() 是 Label(<x>)    ? 去掉 < > 得到 target
都不是 ? 返回 None

for (label, detail) in analyze_labels(output).0:   # 不使用 split
    if label.resolve().as_str() == target:
        return Tooltip::Text(detail?)   # 注意 detail 上的 ?
return None
```

#### 4.3.3 源码精读

[tooltip.rs:187-203](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tooltip.rs#L187-L203) —— `label_tooltip` 全貌：

```rust
fn label_tooltip(output: impl AsOutput, leaf: &LinkedNode) -> Option<Tooltip> {
    let target = match leaf.kind() {
        SyntaxKind::RefMarker => leaf.leaf_text().trim_start_matches('@'),
        SyntaxKind::Label => {
            leaf.leaf_text().trim_start_matches('<').trim_end_matches('>')
        }
        _ => return None,
    };

    for (label, detail) in analyze_labels(output).0 {
        if label.resolve().as_str() == target {
            return Some(Tooltip::Text(detail?));
        }
    }
    None
}
```

要点：

- 触发只认两种 `SyntaxKind`：`RefMarker`（`@xxx`）和 `Label`（`<xxx>`）。用 `leaf.leaf_text()` 取原始文本，再 `trim` 掉前后的标记符号得到纯名字。
- `analyze_labels(output).0` 取第一个返回值（标签列表）。注意它**没有使用 `split`**——文档元素标签和参考文献键都在同一个 Vec 里，都用 `Label` 类型表示，所以 `@bibkey` 同样能在这里匹配到参考文献条目（如果它有详情的话）。
- `detail?` 这一行有个微妙之处：`detail` 是 `Option<EcoString>`。对**文档元素标签**，`analyze_labels` 总是 push `Some(details)`（见 [analyze.rs:133](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/analyze.rs#L133)），所以一定有详情；对**参考文献键**（`BibliographyElem::keys` 返回），详情可能为 `None`，此时 `detail?` 会直接让整个函数提前返回 `None`，而**不是**继续往后找别的同名条目。这是当前实现的一个特性。

#### 4.3.4 代码实践

**实践目标**：用一个已有的测试直观验证 `label_tooltip` 的输入输出。

**操作步骤**（源码阅读型）：

1. 打开 [tooltip.rs:524-527](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tooltip.rs#L524-L527)：

   ```rust
   #[test]
   fn test_tooltip_reference() {
       test("#figure(caption: [Hi])[]<f> @f", -1, Side::Before).must_be_text("Hi");
   }
   ```

2. 解释这条断言为什么成立：`@f` 是 `RefMarker`，target = `"f"`；编译后 figure 的 caption 是 `[Hi]`；`analyze_labels` 把 `f → Some("Hi")` 收进列表；匹配命中，返回 `Tooltip::Text("Hi")`。
3. 若想本地验证，运行：
   ```bash
   cargo test --package typst-ide test_tooltip_reference
   ```

**需要观察的现象 / 预期结果**：测试通过，悬停 `@f` 得到 `Hi`。若你把 `<f>` 换成一个**没有 caption 的普通元素**（如 `#box[]<g> @g`），详情会退化为该元素 `body` 的纯文本（由 `analyze_labels` 内部的 `plain_text()` 计算）。

> 详情的确切文本取决于内容，但「`@ref` 能命中同名 `<label>` 并返回其 caption/body 纯文本」这一行为是确定的。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `label_tooltip` 在分发链里写成 `label_tooltip(output?, &leaf)`，而 `font_tooltip` 不加 `?`？

**参考答案**：`label_tooltip` 的数据来自编译产物（`analyze_labels(output)`），没有 `output` 就无法工作，`?` 让整个分支在没有产物时立刻返回 `None`（优雅降级）；`font_tooltip` 的数据来自 `world.book()`（必填），不需要 `output`，故无 `?`。

**练习 2**：`label_tooltip` 用了 `analyze_labels(output).0`，没有用到第二个返回值 `split`。`split` 是给谁用的？

**参考答案**：`split` 区分「文档元素标签」与「参考文献键」，主要给补全（`label_completions`）用——在 `@`、`< >`、`#cite` 不同上下文里只补其中一类。`label_tooltip` 不需要区分，两类标签都能被引用，所以直接在整个 `.0` 里匹配即可（见 u2-l4）。

---

### 4.4 import_tooltip：通配星号导入的项列表

#### 4.4.1 概念说明

`#import "other.typ": *` 把另一个模块的全部导出都引入当前作用域。但用户悬停在这个 `*` 上时，IDE 想告诉他：「这次星号导入到底会拉进来哪些名字？」。

难点在于：`*` 本身**没有列出任何名字**。要知道它会导入什么，必须**真正去解析并执行这次 import**，拿到目标模块的 `Value::Module`，再枚举其作用域里的所有名字。这正是 `analyze_import`（u2-l4）做的事。

#### 4.4.2 核心流程

```
leaf.kind() == Star ?
  └─ 父节点是 ast::ModuleImport ?
       └─ parent.find(import.source().span())  # 定位到 import 的「源」节点（字符串/路径）
            └─ analyze_import(world, 源节点) -> Option<Value>   # 真正解析模块
                 └─ value.scope() 存在 ?
                      └─ 枚举 scope 全部 name，包成 `name` 的列表
                           └─ separated_list(names, "and") → "This star imports `a`, `b`, and `c`"
```

#### 4.4.3 源码精读

[tooltip.rs:125-140](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tooltip.rs#L125-L140) —— `import_tooltip` 全貌：

```rust
fn import_tooltip(world: &dyn IdeWorld, leaf: &LinkedNode) -> Option<Tooltip> {
    if leaf.kind() == SyntaxKind::Star
        && let Some(parent) = leaf.parent()
        && let Some(import) = parent.cast::<ast::ModuleImport>()
        && let Some(node) = parent.find(import.source().span())   # 定位「源」子节点
        && let Some(value) = analyze_import(world, &node)          # 解析成模块值
        && let Some(scope) = value.scope()                         # 取其作用域
    {
        let names: Vec<_> =
            scope.iter().map(|(name, ..)| eco_format!("`{name}`")).collect();
        let list = repr::separated_list(&names, "and");
        return Some(Tooltip::Text(eco_format!("This star imports {list}")));
    }
    None
}
```

要点：

- 触发条件很严：叶子**必须正好是 `Star` 节点**（即 `*`），且**父节点是 `ModuleImport`**。这确保它只对 `#import "x": *` 里的星号生效，不会误伤别处的乘号 `*`（数学里的 `*` 父节点不是 `ModuleImport`）。
- `parent.find(import.source().span())`：`import.source()` 拿到 import 语句的「源」AST（如字符串 `"other.typ"`），用它的 `span()` 在父节点树里定位回对应的 `LinkedNode`。这一步是为了得到 `analyze_import` 需要的节点上下文（`analyze_import` 要用节点的 `span` 来解析相对路径，见 [analyze.rs:82](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/analyze.rs#L82)）。
- `analyze_import` 真正执行 import（对字符串路径，它会在临时 engine 上调用 `typst_eval::import`），得到 `Value::Module`；`.scope()` 取出模块的作用域，`scope.iter()` 枚举所有导出名。
- `repr::separated_list(&names, "and")`（typst-library 的工具函数）把名字列表拼成英文列举：1 项 → `` `a` ``；2 项 → `` `a` and `b` ``；3 项及以上 → `` `a`, `b`, and `c` ``（带牛津逗号）。注意每个名字被 `eco_format!("`{name}`")` 包了反引号。

注意它**不需要 `output`**——`analyze_import` 内部用 `with_engine` 起一个临时 engine 来跑 import（u2-l4 / u2-l5），不依赖已渲染的文档。

测试里有现成的两段断言：

[tooltip.rs:509-515](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tooltip.rs#L509-L515)：

```rust
#[test]
fn test_tooltip_star_import() {
    let world = TestWorld::new("#import \"other.typ\": *")
        .with_source("other.typ", "#let (a, b, c) = (1, 2, 3)");
    test(&world, -2, Side::Before).must_be_none();
    test(&world, -2, Side::After).must_be_text("This star imports `a`, `b`, and `c`");
}
```

这里还顺带展示了 `Side` 的作用：倒数第 2 个字符位置（`*` 与前一空格的交界处），`Side::Before` 选中前一个 token（空格，trivia，入口已过滤为 `None`），`Side::After` 才选中 `*`——同一光标、不同 `Side` 得到不同结果（呼应 u2-l1 / u3-l2）。

#### 4.4.4 代码实践

**实践目标**：验证「星号导入的名字列表」来自被导入模块的真实导出。

**操作步骤**（源码阅读 + 本地验证）：

1. 阅读上面的测试：被导入模块定义了 `(a, b, c)`，星号导入提示列出 `` `a`, `b`, and `c` ``。
2. 自行预测：若把 `other.typ` 改成 `#let a = 1; #let b = 2`（只导出 `a`、`b`），提示文本应变成什么？
3. 本地验证：
   ```bash
   cargo test --package typst-ide test_tooltip_star_import
   ```

**需要观察的现象 / 预期结果**：原测试通过；按第 2 步修改后应得到 `` This star imports `a` and `b` ``（两项用 `and` 连接）。

#### 4.4.5 小练习与答案

**练习 1**：`#import "other.typ": a, b`（具名导入，非星号）里悬停在 `a` 上会得到「This star imports ...」吗？

**参考答案**：不会。`import_tooltip` 的第一个条件就是 `leaf.kind() == SyntaxKind::Star`，具名导入里没有星号节点，分支直接返回 `None`；随后 `a` 会落到 `expr_tooltip`，给出它的值（如 `1`）。

**练习 2**：为什么 `import_tooltip` 必须调用 `analyze_import`，而不能像 `named_items` 那样只做静态分析？

**参考答案**：因为 `*` 不含名字信息，必须真正解析 `other.typ` 这个模块才知道它导出了什么；`analyze_import` 通过 `typst_eval::import` 真正执行导入、拿到 `Value::Module`，这是动态行为，无法仅凭当前文件语法静态得出。

---

### 4.5 closure_tooltip：闭包捕获的变量

#### 4.5.1 概念说明

闭包（函数）会「捕获」它引用的外部变量。例如：

```typst
#let y = 10
#let f(x) = x + y    # f 捕获了外部变量 y
```

悬停在 `f` 的定义处的 `=` 上时，IDE 想提示「这个闭包捕获了 `y`」，帮助用户理解闭包的依赖。

这条 tooltip 有两个鲜明的特点：

1. **它是唯一一个既不需要 `world`、也不需要 `output` 的分支**——函数签名是 `fn closure_tooltip(leaf: &LinkedNode)`，纯粹基于语法树分析。
2. **它只在 `=` 或 `=>` 上触发**，而不是整个闭包子树。这是刻意的降噪设计。

#### 4.5.2 核心流程

```
leaf.kind() 是 Eq(=) 或 Arrow(=>) ? 否则返回 None    # 降噪：只在这两个符号上触发
  └─ leaf.parent() 是 Closure 节点 ? 否则返回 None
       └─ CapturesVisitor::new(None, Capturer::Function)
          visitor.visit(parent)        # 遍历整个闭包子树，收集捕获的外部变量
          captures = visitor.finish()  # 得到一个 Scope（捕获变量名集合）
            └─ captures 为空 ? 返回 None            # 无捕获时不提示
                 └─ 名字排序 → separated_list → "This closure captures `a`, `y`, and `z`"
```

#### 4.5.3 源码精读

[tooltip.rs:143-171](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tooltip.rs#L143-L171) —— `closure_tooltip` 全貌：

```rust
fn closure_tooltip(leaf: &LinkedNode) -> Option<Tooltip> {
    // Only show this tooltip when hovering over the equals sign or arrow of
    // the closure. Showing it across the whole subtree is too noisy.
    if !matches!(leaf.kind(), SyntaxKind::Eq | SyntaxKind::Arrow) {
        return None;
    }

    // Find the closure to analyze.
    let parent = leaf.parent()?;
    if parent.kind() != SyntaxKind::Closure {
        return None;
    }

    // Analyze the closure's captures.
    let mut visitor = CapturesVisitor::new(None, Capturer::Function);
    visitor.visit(parent);

    let captures = visitor.finish();
    let mut names: Vec<_> =
        captures.iter().map(|(name, ..)| eco_format!("`{name}`")).collect();
    if names.is_empty() {
        return None;                       # 无捕获 → None
    }

    names.sort();                          # 排序，保证输出稳定

    let tooltip = repr::separated_list(&names, "and");
    Some(Tooltip::Text(eco_format!("This closure captures {tooltip}")))
}
```

要点：

- **降噪设计**（[tooltip.rs:144-148](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tooltip.rs#L144-L148)）：注释写得很清楚——「Only show this tooltip when hovering over the equals sign or arrow of the closure. Showing it across the whole subtree is too noisy.」。如果整个闭包子树都显示「captures ...」，用户在函数体里随便移动鼠标都会弹出这条提示，非常吵；只在 `=`/`=>` 这一处显示，既点出了「这是闭包定义点」，又避免噪音。
- **`CapturesVisitor`**（typst-eval 提供）：`new(None, Capturer::Function)` 创建一个针对函数闭包的捕获访问器；`visit(parent)` 让它遍历整个闭包（参数 + 函数体），记下所有引用了「外部作用域」的标识符；`finish()` 返回一个 `Scope`，其条目就是被捕获的变量。`Capturer::Function` 表示按「函数捕获」语义（与之相对的还有 `Context` 等语义），确保只算真正的闭包捕获、不算 context 捕获。
- **无捕获返回 `None`**（[tooltip.rs:163-165](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tooltip.rs#L163-L165)）：`if names.is_empty() { return None; }`——一个不依赖任何外部变量的「纯函数」不显示这条提示，这也是降噪的一部分。
- **排序去重**：`names.sort()` 让输出按字母序稳定（`CapturesVisitor` 本身已对同一变量的多次引用去重，这里再排序）。注意 `sort` 在 `is_empty` 检查之后，避免无谓排序。

测试覆盖了多种形态：

[tooltip.rs:481-500](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tooltip.rs#L481-L500)：

```rust
test("#let f(x) = x + y", 11, Side::Before)             // = 形式，捕获 y
    .must_be_text("This closure captures `y`");
test("#let y = 10; #let f(x) = x + y", 24, Side::Before)// y 先定义，仍捕获 y
    .must_be_text("This closure captures `y`");
test("#let f(x) = x + y + z + a", 11, Side::Before)     // 多个 → 排序
    .must_be_text("This closure captures `a`, `y`, and `z`");
test("#let f(x) = x + y + z + y", 11, Side::Before)     // 重复引用 → 去重
    .must_be_text("This closure captures `y` and `z`");
test("#let f = (x) => x + y", 15, Side::Before)         // => 箭头形式
    .must_be_text("This closure captures `y`");
test("#let f = (x) => x + y + f", 13, Side::After)      // f 自引用也算捕获
    .must_be_text("This closure captures `f` and `y`");
```

特别注意最后一条：箭头形式里函数体引用了 `f` 自己，也算捕获（因为此时 `f` 尚未完成绑定，是外部名字）——这是 `Capturer::Function` 语义的体现。

#### 4.5.4 代码实践

**实践目标**：补一个测试场景，验证「闭包无任何捕获时返回 `None`」，并解释降噪设计。

**操作步骤**：

1. 打开 [tooltip.rs:481](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tooltip.rs#L481) 的 `test_tooltip_closure` 测试作为模板。
2. 在末尾追加一条用例。一个不捕获任何外部变量的闭包是 `#let f(x) = x`（函数体只用到了自己的参数 `x`）。`=` 的位置：在 `#let f(x) = x` 中，`=` 落在字节 10（索引从 0 计），其后空格从索引 11 开始；参照已有用例「在 `=` 之后的空格处用 `Side::Before` 选中 `=`」的写法，应写为：

   ```rust
   // 闭包无任何捕获时不显示提示。
   test("#let f(x) = x", 11, Side::Before).must_be_none();
   ```

   （箭头形式同理可写 `test("#let f = (x) => x", 15, Side::Before).must_be_none();`）
3. 本地验证：
   ```bash
   cargo test --package typst-ide test_tooltip_closure
   ```

**需要观察的现象 / 预期结果**：新用例通过——`CapturesVisitor` 对 `#let f(x) = x` 收集到的捕获为空，`names.is_empty()` 命中，函数返回 `None`，`must_be_none()` 成立。

**解释「为什么不在整个闭包子树上显示」**：参见源码注释与 [tooltip.rs:146](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tooltip.rs#L146) 的 `matches!(leaf.kind(), SyntaxKind::Eq | SyntaxKind::Arrow)` 守卫。若放宽这个守卫，让函数体里任意节点都能命中，用户每次把鼠标移进函数体都会弹「This closure captures ...」，信息冗余且干扰阅读；限定在定义点的 `=`/`=>` 上，既明确「这是闭包定义」，又只在该看的时候出现。

> 新用例的**确切光标索引**建议本地用 `Side::Before` 在 `=` 后一格试验确认；上述 11 是按 `#let f(x) = x` 手算给出，若解析边界略有出入，「待本地验证」后微调即可。但「无捕获 → `None`」这一结论由 `names.is_empty()` 分支保证，不依赖光标。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `closure_tooltip` 的签名里没有 `world` 和 `output` 参数？

**参考答案**：捕获分析完全基于语法树：`CapturesVisitor::visit` 遍历闭包子树、判定哪些标识符引用了外部作用域，不需要求值、不需要文档渲染结果。所以它不依赖 `IdeWorld` 的任何数据，也不依赖编译产物，是纯语法分析。

**练习 2**：若把守卫从 `Eq | Arrow` 放宽为「整个 `Closure` 子树」，会带来什么问题？

**参考答案**：闭包体内任意标识符（如函数体里的 `x + y`）都会命中 `closure_tooltip`，导致用户在函数体内移动鼠标时频繁弹出「This closure captures ...」，提示内容却都一样，属于噪音；这正是源码注释所说的「too noisy」。限定在 `=`/`=>` 是用最小的触发面承载这条信息。

**练习 3**：`#let f(x) = x + y + z + y` 的提示为什么是 `` captures `y` and `z` `` 而不是列出三个 `y`？

**参考答案**：`CapturesVisitor.finish()` 返回的 `Scope` 本身按名字去重（`y` 被引用两次只保留一项），所以枚举出的 `names` 里 `y` 只出现一次，最终用 `and` 连接成 `` `y` and `z` ``。

---

## 5. 综合实践

**任务**：给一段同时包含「字体、标签、import、闭包捕获」的 Typst 文档，逐一预测每个光标处会命中分发链的哪个分支、得到什么 tooltip，再用 `tooltip.rs` 的 `test(...)` 测试助手验证。

示例文档（沿用项目的 `TestWorld` 设施）：

```typst
#import "lib.typ": *
#let caption-text = "My Figure"
#let draw(filled) = (
  #rect(fill: black)[]
)
#figure(caption: caption-text)[
  #draw(true)
]<fig>
Hover over: @fig, the star *, font below.
#text(font: "DejaVu Sans")[Hi]
```

要求：

1. 对下列每个光标目标，先**静态预测**命中哪条分支（`font` / `label` / `import` / `closure` / `expr` / `named_param` / 都不命中）：
   - `@fig` 的引用标记 → ？
   - `#import "lib.typ": *` 里的 `*` → ？
   - `#text(font: "DejaVu Sans")` 里的 `"DejaVu Sans"` → ？
   - `#let draw(filled) = (...)` 里的 `=`（假设 `draw` 在别处被使用，从而捕获了外部变量）→ ？
   - `#figure(...)` 里的 `caption:` 参数名 → ?
2. 对你能在源码/测试里找到确切答案的（如 `*`、`@fig`），写出预期文本；不确定的标注「待本地验证」。
3. 挑其中至少两项，仿照 `test_tooltip_star_import` 的写法，用 `TestWorld::new(...).with_source(...)` 构造跨文件场景，写成可运行断言。

**参考思路**：

- `@fig` → `label_tooltip`（`RefMarker`），命中 `analyze_labels` 里 `fig → caption-text 的渲染文本`；确切文本取决于渲染，「待本地验证」。
- `*` → `import_tooltip`，列出 `lib.typ` 的全部导出名。
- `"DejaVu Sans"` → `font_tooltip`，给出该家族摘要。
- `draw` 的 `=` → `closure_tooltip`（若 `draw` 确实捕获了外部变量），列出捕获名；若 `draw` 是纯函数无捕获则返回 `None`（本题 `draw` 未引用外部变量，预期 `None`）。
- `caption:` → `named_param_tooltip`（u3-l2 / 后续讲义），给出 `caption` 参数的文档——注意这条分支排在最前，会抢先命中。

这个综合任务把本讲四个特殊分支和分发链的整体短路顺序串起来，帮助你建立「给定光标，预测命中分支」的直觉。

## 6. 本讲小结

- 分发链里除 `expr_tooltip` 外有四个特殊分支：`font`（2）、`label`（3）、`import`（4）、`closure`（6）；它们都用局部/语法信息或一次轻量 import 解析即可工作，排在昂贵的 `expr_tooltip` 之前（`closure` 因只在 `=`/`=>` 触发，排最后也不冲突）。
- `font_tooltip` 只在「字符串 + 父为 `font:` 具名参数」时触发，靠 `world.book()` 查家族，不需要 `output`；摘要由 `summarize_font_family` 把家族下所有 `FontInfo` 变体聚合（字重/字宽取范围、斜体/可变/光学尺寸取并集、自定义轴原样列出）为一行。
- `label_tooltip` 只认 `RefMarker`/`Label`，**依赖 `output`**（`label_tooltip(output?, ...)`，无产物即降级），在 `analyze_labels(output).0` 里按名字匹配，返回 figure caption 或元素 body 的纯文本。
- `import_tooltip` 只认 `ModuleImport` 下的 `Star` 节点，靠 `analyze_import` 真正解析模块、枚举其作用域名，拼成 `` This star imports `a`, `b`, and `c` ``。
- `closure_tooltip` 是唯一既不要 `world` 也不要 `output` 的分支（纯语法分析，用 `CapturesVisitor`）；**只在 `=`/`=>` 上触发、无捕获返回 `None`**，两处都是刻意的降噪设计。
- 四类提示的输出统一用 `repr::separated_list(.., "and")` 拼英文列举（1 项直出、2 项 `and`、3+ 项带牛津逗号），闭包捕获还会先 `sort` 保证稳定。

## 7. 下一步学习建议

本讲讲完了 tooltip 的全部分支。接下来可以：

- 进入 **u4（跳转定义 definition）**：`definition` 同样用 `deref_target` + `named_items` + `analyze_expr`，与本讲的 `import_tooltip`（`analyze_import`）、`label_tooltip`（`Selector::Label` 思路）一脉相承，正好承接。
- 或者进入 **u5/u6（自动补全）**：补全里的 `font_completions` / `label_completions` / `import_item_completions` 与本讲的 `font_tooltip` / `label_tooltip` / `import_tooltip` 共享同一批数据源（`book` / `analyze_labels` / `analyze_import`），对照阅读能加深对「同一数据、不同消费方式」的理解。
- 想深挖字体摘要的细节，可继续阅读 typst 的 `FontInfo` / `FontAxis` / `StandardAxes` 定义（typst crate 的 `text` 模块），理解 `summarize_font_family` 里每个轴的含义。
