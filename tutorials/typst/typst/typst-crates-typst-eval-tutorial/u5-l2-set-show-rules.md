# set 与 show 规则求值

## 1. 本讲目标

本讲专门拆解 Typst 中两条最重要的样式语句——`set` 与 `show`——在解释器里的求值实现。读完本讲，你应当能够：

- 说清 `SetRule::eval` 如何处理「条件守卫」、如何校验目标必须是「元素函数」，并把参数交给 `Element::set` 产出 `Styles`；
- 说清 `ShowRule::eval` 如何把一个可选的 `selector` 与一个 `transform` 组装成一个 `Recipe`，以及 `Transformation` 的三种变体（`Content` / `Func` / `Style`）分别对应哪种写法；
- 理解 `set`/`show` 为什么不是「普通表达式」，而是作为「样式作用域」作用于它后续的 tail（剩余内容）；
- 读懂 `check_show_page_rule` 与 `check_show_par_set_block` 两条兼容性 lint 的检测条件与迁移建议，并理解它们为何放在求值阶段而非解析阶段；
- 理解 `hint_if_shadowed_std` 这个统一诊断增强函数的判定逻辑。

本讲只聚焦一个文件 [src/rules.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/rules.rs)（共 96 行），辅以 typst-library 中的相关类型定义。

## 2. 前置知识

在进入正文前，先确认你已经建立下面这些认知（来自前置讲义，本讲直接承接，不再重复推导）：

- **`Eval` trait 与 `Vm`**（u1-l4）：每个 AST 节点实现 `Eval`，签名 `fn eval(self, vm: &mut Vm) -> SourceResult<Output>`，用关联类型 `Output` 声明各自的输出类型。本讲里 `SetRule` 的输出是 `Styles`，`ShowRule` 的输出是 `Recipe`。
- **`eval_code` / `eval_markup` 的流式求值与 tail 机制**（u2-l3、u2-l4）：代码模式与标记模式都用一个 `while let` 逐条吃表达式。遇到 `set`/`show` 时，会先求出样式/配方，再**递归求值剩余的所有表达式**（tail），用 `styled_with_map` / `styled_with_recipe` 把样式包裹到 tail 上。这正是 `set`/`show` 不能作为普通表达式出现的根本原因。
- **元素函数（element function）**（u2-l4、u4-l1）：typst-library 中像 `heading`、`text`、`par`、`page`、`block` 这样的「元素」，都对应一个 `Func`，且 `Func::to_element()` 能返回它背后的 `Element` 句柄。元素函数既能 `construct`（构造实例），也能 `set`（产出样式）。

几个本讲会反复出现的运行时类型，先列一个速查表：

| 类型 | 所在 | 含义 |
|------|------|------|
| `Styles` | typst-library | 一组样式条目（style map），`set` 规则的求值产物 |
| `Element` | typst-library | 元素的类型句柄，带 `set`/`construct` 等 vtable 方法 |
| `Selector` | typst-library | 元素过滤器（`Elem`/`Label`/`Regex`/`Or`/`And`…） |
| `ShowableSelector` | typst-library | `Selector` 的 newtype，限定 show 规则选择器位置可接受的输入 |
| `Recipe` | typst-library | 一条 show 配方 = 选择器 + 变换 + span |
| `Transformation` | typst-library | 变换的三种变体：`Content` / `Func` / `Style` |

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/rules.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/rules.rs) | **本讲核心**。定义 `SetRule`、`ShowRule` 两个 `Eval` 实现，以及两条兼容性 lint。 |
| [src/code.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs) | `eval_code` 中对 `SetRule`/`ShowRule` 的 tail 处理，以及在表达式位置禁止 `set`/`show` 的 `forbidden` 闭包。 |
| [src/markup.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/markup.rs) | `eval_markup` 中对 `SetRule`/`ShowRule` 的 tail 处理（与 code.rs 同构）。 |
| [src/vm.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/vm.rs) | `hint_if_shadowed_std` 的定义。 |
| typst-library `foundations/styles.rs` | `Recipe`、`Transformation`、`Styles::liftable` 的定义。 |
| typst-library `foundations/selector.rs` | `Selector` 枚举与 `ShowableSelector`。 |
| typst-library `foundations/content/element.rs` | `Element::set`。 |
| typst-library `foundations/content/mod.rs` | `Content::styled_with_map` / `styled_with_recipe`。 |
| tests/suite/layout/page.typ、tests/suite/model/par.typ | 两条 lint 警告的回归测试用例。 |

## 4. 核心概念与源码讲解

本讲拆成五个最小模块：`SetRule` 求值、`ShowRule` 求值、`check_show_page_rule`、`check_show_par_set_block`、`hint_if_shadowed_std`。

### 4.1 set 规则求值：条件守卫与元素函数校验

#### 4.1.1 概念说明

`set` 规则的语法形如 `set heading(size: 12pt)`，或带条件的 `set text(lang: "zh") if condition`。它的求值产物不是普通 `Value`，而是一组 `Styles`——一组「样式条目」。这组样式不会立即生效，而是被求值器包裹到**后续的 tail 内容**上（见 4.1.2 的流程与 4.3 的补充说明）。

理解 `set` 的关键是三个约束：

1. **目标是元素函数**：只有像 `heading`、`text` 这样的「元素函数」才能 `set`，因为只有它们才带 `set` 能力（通过 `Element::set`）。普通函数、用户闭包不能 `set`。
2. **参数会校验**：多余的、未知的字段会在 `args.finish()` 时报错。
3. **可选条件守卫**：`if condition` 不是真值判断，而是把条件强制 `cast` 成 `bool`，cast 失败同样报错。

#### 4.1.2 核心流程

`SetRule::eval` 的执行流程（伪代码）：

```
1. 若存在条件 condition:
     求 condition → cast<bool>（失败即报错）
     若结果为 false → 直接返回空 Styles::new()
2. 求 target 表达式 → cast<Func>（失败附加 hint_if_shadowed_std）
3. func.to_element() 必须是 Some(elem)，否则报 "only element functions can be used in set rules"
4. 求 args，贴上 set 规则的 span
5. elem.set(engine, args) → 得到 Styles
6. Styles 再贴 span，调用 .liftable()，返回
```

注意第 1 步：条件为假时返回的是 `Styles::new()`（空样式），而不是 `Value::None`——因为 `Output = Styles`。空样式包裹到 tail 上等于不改变任何样式，语义正确。

#### 4.1.3 源码精读

`SetRule` 的 `Eval` 实现，输出类型是 `Styles`：

> [src/rules.rs:11-35](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/rules.rs#L11-L35)：`SetRule::eval` 的完整实现——条件守卫、目标求值与元素校验、`Element::set` 产出样式。

**条件守卫**（15-19 行）：

> [src/rules.rs:15-19](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/rules.rs#L15-L19)：用 `let` 链 + `&&`，把「有条件、且条件求值为假」两种情况合并处理，返回空 `Styles::new()`。

这里用了 Rust 的 `let`-`else` 风格链式判断：`if let Some(condition) && !condition.eval(vm)?.cast::<bool>().at(...)?`。`cast::<bool>()` 会把非布尔值（如整数、字符串）当作 cast 失败并报错——这与 Typst「不做隐式真值判断」的整体设计一致（参见 u3-l4 中 `and`/`or` 的短路判断）。

**目标校验链**（21-31 行）：

> [src/rules.rs:21-31](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/rules.rs#L21-L31)：求 target → cast 成 `Func` → `to_element()` 校验，三级 `map_err`/`and_then` 串联。

这三级串联值得细看：
- `.cast::<Func>()` 把目标表达式（通常是标识符 `heading`）求值后转成函数值；如果用户写了 `set 123(...)`，这一步就失败。
- `.map_err(|err| hint_if_shadowed_std(vm, &target_expr, err))`：cast 失败时，若目标名被标准库遮蔽（如用户用 `let text = "..."` 覆盖了 `text`），附加一条「用 `std.text` 访问被遮蔽的标准库函数」的修复提示（见 4.5）。
- `.and_then(|func| func.to_element().ok_or_else(...))`：只有元素函数能进 `set`，否则 `"only element functions can be used in set rules"`。

`to_element` 的实现很直接——只有 `FuncInner::Element` 变体才返回 `Some`：

> [crates/typst-library/src/foundations/func.rs:307-313](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/func.rs#L307-L313)：`Func::to_element`，只有元素函数返回 `Some(Element)`。

**产出样式**（32-33 行）：

> [src/rules.rs:32-33](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/rules.rs#L32-L33)：求 args、调用 `target.set`、贴 span、调用 `.liftable()`。

`Element::set` 调用元素 vtable 上的 `set` 函数并 `args.finish()` 做参数收尾校验：

> [crates/typst-library/src/foundations/content/element.rs:75-79](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/element.rs#L75-L79)：`Element::set`，执行元素的 set 规则并 `args.finish()` 校验剩余参数。

末尾的 `.liftable()` 是一个标记操作，把样式条目标记为「允许上浮到页面/root 级别」。这对于页眉页脚、脚注等需要样式从用户内容继承到根的场合很关键：

> [crates/typst-library/src/foundations/styles.rs:104-112](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/styles.rs#L104-L112)：`Styles::liftable`，把每个 `Property` 条目的 `liftable` 标志置真，允许其上浮到页面级。

#### 4.1.4 代码实践

1. **实践目标**：验证「只有元素函数能用于 set 规则」，并追踪一次条件 set 的求值路径。
2. **操作步骤**：
   - 打开 [src/rules.rs:11-35](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/rules.rs#L11-L35)，逐行对照 4.1.2 的流程图，给每一行注释它属于哪一步。
   - 思考下面三段 Typst 代码分别会在哪一步报错：
     - `#set 123(arg: 1)`（目标不是函数）
     - `#let myfunc = (x) => x; #set myfunc(x: 1)`（目标是用户闭包，`to_element` 返回 `None`）
     - `#set text(size: 1) if "yes"`（条件 cast bool 失败）
3. **需要观察的现象**：三种写法分别命中 `cast::<Func>` 失败、`to_element` 返回 `None`、条件 `cast::<bool>` 失败。
4. **预期结果**：
   - 第 1 段：cast Func 失败（并可能附加 hint）。
   - 第 2 段：报 `only element functions can be used in set rules`。
   - 第 3 段：条件处报「无法转成 bool」之类的 cast 错误，而非被当作真。
5. **待本地验证**：上述具体报错文案与行为待用 typst CLI 本地编译确认。

#### 4.1.5 小练习与答案

**练习 1**：为什么条件为假时返回的是 `Styles::new()` 而不是提前返回一个「特殊跳过值」？

**参考答案**：因为 `SetRule` 的 `Output = Styles`，调用方（`eval_code`/`eval_markup`）会用 `tail.styled_with_map(styles)` 把它裹到 tail 上；空 `Styles` 在 `styled_with_map` 里是「什么都不做」（见 4.3 引用的 `styled_with_map` 实现，`styles.is_empty()` 时直接返回原内容），所以语义等价于「这条 set 不生效」，无需引入特殊值。

**练习 2**：`set heading(level: 1)` 里 `heading` 是怎么被识别为「元素函数」的？

**参考答案**：`heading` 这个标识符求值后是一个 `Func`，其内部是 `FuncInner::Element`；`to_element()` 据此返回 `Some(Element)`，于是通过 4.1.3 的第三级校验。

---

### 4.2 show 规则求值：selector + transform 组装 Recipe

#### 4.2.1 概念说明

`show` 规则的语法有三种形态：

- `show heading: it => [大标题]`——带选择器、带变换函数；
- `show heading: set text(red)`——带选择器、变换是一个 **set 子规则**；
- `show: rest => ...`——**不带选择器**，变换作用于「作用域剩余内容」（eagerly applied）。

无论哪种形态，`ShowRule::eval` 的产物都是一个 `Recipe`：选择器 + 变换 + span 的三元组（外加一个内部 `outside` 标志）。`Recipe` 本身只是「配方」，真正把它应用到内容上是 `eval_code`/`eval_markup` 里 `styled_with_recipe` 的职责（见 4.3）。

#### 4.2.2 核心流程

```
1. selector（可选）:
     若存在 → 求值 → cast<ShowableSelector>（失败附加 hint）→ 取 .0 得 Selector
     若不存在（show: rest => ...）→ None
2. transform:
     若 transform 是 ast::Expr::SetRule(set)（即 "show x: set ..."）→ 递归 set.eval(vm) 得 Styles
         → 包成 Transformation::Style(styles)
     否则 → 求值表达式 → cast<Transformation>（Content→Content / Func→Func）
3. Recipe::new(selector, transform, self.span())
4. 跑两条兼容性 lint：check_show_page_rule、check_show_par_set_block
5. 返回 recipe
```

第 2 步是这个模块最精巧的地方：`Transformation` 是一个枚举，但用户在源码里写的变换表达式可以是 `Content`（直接替换内容）、`Func`（函数变换）或一整条 `set` 子规则。代码用一个 `match transform` 区分：命中 `SetRule` 就走「子规则」分支，其余统一 `cast::<Transformation>`。而 `cast!` 规则把 `Content` 映射到 `Transformation::Content`、`Func` 映射到 `Transformation::Func`。

#### 4.2.3 源码精读

`ShowRule` 的 `Eval` 实现，输出类型是 `Recipe`：

> [src/rules.rs:37-64](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/rules.rs#L37-L64)：`ShowRule::eval` 完整实现——求 selector、求 transform、组装 Recipe、跑两条 lint。

**selector 求值**（41-50 行）：

> [src/rules.rs:41-50](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/rules.rs#L41-L50)：`self.selector()` 是 `Option`；用 `.map(...).transpose()?` 把 `Option<Result<...>>` 翻成 `Result<Option<...>>`，再 `.map(|s| s.0)` 解包 `ShowableSelector`。

`ShowableSelector` 是一个 newtype，用 `Reflect`/`castable` 限定 show 选择器位置只接受 symbol/str/label/func/regex/selector 这几类，避免把任意值当选择器：

> [crates/typst-library/src/foundations/selector.rs:475-500](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/selector.rs#L475-L500)：`ShowableSelector(pub Selector)`，及其 `castable` 列出可接受的输入类型。

**transform 求值**（52-56 行）：

> [src/rules.rs:52-56](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/rules.rs#L52-L56)：对 `transform` 做 `match`：`SetRule` 走子规则分支递归求值，其余 `cast::<Transformation>`。

`Transformation` 枚举与对应的 `cast!` 规则：

> [crates/typst-library/src/foundations/styles.rs:530-555](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/styles.rs#L530-L555)：`Transformation` 三变体（`Content`/`Func`/`Style`）及把 `Content`→`Content`、`Func`→`Func` 的 `cast!`。

**组装与 lint**（58-62 行）：

> [src/rules.rs:58-62](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/rules.rs#L58-L62)：`Recipe::new(selector, transform, self.span())` 后立即跑两条 lint（见 4.3、4.4）。

`Recipe` 结构体与 `new` 构造器：

> [crates/typst-library/src/foundations/styles.rs:447-471](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/styles.rs#L447-L471)：`Recipe` 的字段（selector/transform/span/outside）与 `Recipe::new`。

注意 `selector` 是 `Option<Selector>`：当 `show: rest => ...` 没有选择器时为 `None`，此时这条 recipe 会被「急切应用」（eagerly applied）到作用域剩余内容——这是 `Content::styled_with_recipe` 里 `selector().is_none()` 分支的语义。

#### 4.2.4 代码实践

1. **实践目标**：根据 `Transformation` 的派发规则，预测不同 show 写法产出的 `Recipe.transform` 变体。
2. **操作步骤**：对下面四条 show 规则，分别判断它们会命中 [src/rules.rs:52-56](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/rules.rs#L52-L56) 的哪个分支、产出哪个 `Transformation` 变体、`selector` 是否为 `None`：
   - `#show heading: [标题]`
   - `#show heading: it => emph(it.body)`
   - `#show heading: set text(red)`
   - `#show: rest => rest`
3. **需要观察的现象**：前三条 selector 非 `None`，最后一条 selector 为 `None`；第 3 条命中 `SetRule` 分支。
4. **预期结果**：
   - 第 1 条：`Transformation::Content`（`[标题]` 是 Content）。
   - 第 2 条：`Transformation::Func`（闭包是 Func）。
   - 第 3 条：`Transformation::Style`（命中 `SetRule` 分支，递归 `set.eval` 得 Styles）。
   - 第 4 条：`Transformation::Func`，且 `selector = None`（会被急切应用到 tail）。
5. **待本地验证**：可借助 IDE 的求值追踪或阅读 `Recipe::apply` 确认各变体的应用行为。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `show heading: set text(red)` 里的 `set text(red)` 能在「表达式位置」出现，而单独写 `#(set text(red))` 却会报错（参见 u2-l1 的 `forbidden` 闭包）？

**参考答案**：在 `ShowRule::eval` 里，`set` 子规则是作为 `transform` 的**特判分支**被 `set.eval(vm)` 直接求值的，并没有经过 `Expr::eval` 总分发器，所以不触发 `forbidden`。而 `#(set text(red))` 把 `set` 放进括号表达式，会走 `Expr::eval`，命中 `Self::SetRule(_) => bail!(forbidden("set"))`，因为 set/show 在普通表达式上下文里没有「后续 tail」可作用。

**练习 2**：`Recipe::new` 的第三参数为什么是 `self.span()`（show 规则本身的 span）而不是 transform 的 span？

**参考答案**：`recipe.span()` 被两条 lint 和后续报错用来定位整条 show 规则，方便用户一眼看到是哪条 show 出了问题，而不是只看到它的变换部分。

---

### 4.3 兼容性 lint：check_show_page_rule

#### 4.3.1 概念说明

`show page: none`（或任何针对 `page` 元素的 show 规则）当前**没有任何效果**——page 的渲染不经过普通 show 规则机制。为了让用户尽早知道这一点，求值器在每条 `Recipe` 生成后跑一遍 `check_show_page_rule`：如果选择器正好是 `page` 元素，就发一条警告，并提示用 `set page(..)` 替代。这是一种**「lint 式」警告**——不阻断编译，只是告知。

#### 4.3.2 核心流程

```
if recipe.selector() 是 Selector::Elem(elem, _) 且 elem == Element::of::<PageElem>():
    发警告 "`show page` is not supported and has no effect"
    附 hint: "customize pages with `set page(..)` instead"
```

判断方式是模式匹配 `Selector::Elem(elem, _)`，再用 `Element::of::<PageElem>()` 取得 page 元素的类型句柄做相等比较。

#### 4.3.3 源码精读

> [src/rules.rs:66-77](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/rules.rs#L66-L77)：`check_show_page_rule`——匹配 page 元素选择器并发警告。

这里再次用了 `let` 链 `if let Some(...) && *elem == Element::of::<PageElem>()`。警告通过 `vm.engine.sink.warn(warning!(...))` 发到 sink（非致命），与 `eval` 入口收集错误/警告的机制衔接（u1-l3）。

对应的回归测试用例（测试框架用 `// Warning:` 注释声明期望警告）：

> [tests/suite/layout/page.typ:351-354](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/tests/suite/layout/page.typ#L351-L354)：`page-show-warning` 用例，断言 `#show page: none` 会产生该警告与 hint。

#### 4.3.4 代码实践

1. **实践目标**：确认 `show page` 警告的触发条件与文案。
2. **操作步骤**：阅读上面引用的测试用例，注意 `// Warning: 2-17` 标注的列范围对应的是 `#show page: none` 这一行第 2-17 列（即整条 show 规则）。
3. **需要观察的现象**：警告 span 覆盖整条 show 语句，而非只覆盖 `page` 这个词。
4. **预期结果**：编译不报错（只是警告），文档仍能生成。
5. **待本地验证**：用 typst CLI 编译一份只含 `#show page: none` 的文档，确认 stderr 输出该警告。

#### 4.3.5 小练习与答案

**练习**：`show page` 的选择器为什么会被匹配成 `Selector::Elem(PageElem, None)` 而不是别的变体？

**参考答案**：`show page: ...` 里的 `page` 是一个元素函数；按 [selector.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/selector.rs) 的 cast 规则，一个裸元素函数会被转成 `Selector::Elem(elem, None)`（无字段过滤），所以 `check_show_page_rule` 的模式匹配能命中。

---

### 4.4 兼容性 lint：check_show_par_set_block

#### 4.4.1 概念说明

这是 typst 演进过程中留下的一条迁移提示。旧版本里段落（`par`）被视为 block，用户曾用 `show par: set block(spacing: ..)` 来调整段落间距。新版本不再把段落当作 block，这种写法**失效了**。求值器检测到这种「针对 par、变换是 `set block(...)`、且设置了 above/below（即 spacing）」的 recipe 时，发警告建议改写成 `set par(spacing: ..)`。

#### 4.4.2 核心流程

```
if selector == ParElem
   且 transform == Transformation::Style(styles)
   且 用 StyleChain 读 styles，has(BlockElem::above) 或 has(BlockElem::below):
    发警告 "`show par: set block(spacing: ..)` has no effect anymore"
    附两条 hint:
      - "write `set par(spacing: ..)` instead"
      - "this is specific to paragraphs as they are not considered blocks anymore"
```

`spacing` 在 `block` 上会被拆解成 `above`/`below` 两个字段，所以检测的是 `BlockElem::above`/`below` 是否出现在样式里。

#### 4.4.3 源码精读

> [src/rules.rs:79-95](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/rules.rs#L79-L95)：`check_show_par_set_block`——用 `let` 链串联四重条件，命中即发带两条 hint 的警告。

四重条件缺一不可：选择器是 `ParElem`、变换是 `Style`、用 `StyleChain::new(styles)` 包装后、样式里含 `BlockElem::above` 或 `below`。这条 lint 与 4.3 共享同一套 `Selector::Elem(elem, _)` 匹配骨架。

回归测试用例（注意它声明了两条 hint）：

> [tests/suite/model/par.typ:576-580](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/tests/suite/model/par.typ#L576-L580)：`show-par-set-block-hint` 用例，断言 `#show par: set block(spacing: 12pt)` 触发警告与两条迁移 hint。

**为什么这类检查放在求值阶段而非解析阶段？** 关键在于：检测条件 `transform == Style(styles)` 且 `styles.has(BlockElem::above)` 需要**已经求值出真实的 `Styles` 值**才能判断。解析阶段只有语法树，看不到 `set block(spacing: ..)` 求值后会变成哪些具体样式条目（spacing 可能来自任意表达式）。只有等 `set.eval(vm)` 把它求成 `Styles`、再由 `StyleChain` 查询字段是否存在，才能可靠判定。这正是把它放在 `ShowRule::eval` 末尾（recipe 已组装完毕）的原因。

#### 4.4.4 代码实践

1. **实践目标**：对照测试用例，理解这条 lint 的检测边界。
2. **操作步骤**：
   - 阅读 [tests/suite/model/par.typ:576-580](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/tests/suite/model/par.typ#L576-L580)，确认 `#show par: set block(spacing: 12pt)` 会触发。
   - 思考下面两种「相似但不触发」的写法为什么不会命中：`#show par: set text(red)`（transform 是 Style，但没有 above/below）；`#show par: set block(width: 100%)`（设置了 block 字段但不是 spacing）。
3. **需要观察的现象**：只有同时满足「par 选择器 + set block 的 spacing(above/below)」才触发。
4. **预期结果**：上述两种变体不产生该警告。
5. **待本地验证**：用 typst CLI 编译各变体确认是否触发警告。

#### 4.4.5 小练习与答案

**练习**：如果把 `#show par: set block(spacing: 12pt)` 改成 `#show par: set par(spacing: 12pt)`，还会触发这条警告吗？为什么？

**参考答案**：不会。改写后 transform 仍是 `Transformation::Style`，但样式里设置的是 `ParElem` 的字段而非 `BlockElem::above`/`below`，`styles.has(BlockElem::above)` 与 `styles.has(BlockElem::below)` 都为假，四重条件不满足。事实上这正是警告 hint 建议的目标写法。

---

### 4.5 hint_if_shadowed_std：统一增强诊断

#### 4.5.1 概念说明

`hint_if_shadowed_std` 是一个跨模块复用的**诊断增强函数**：当某个标识符（如 `text`、`heading`）被用户的局部绑定遮蔽了同名的标准库函数，而用户又试图把它当函数用时，cast 会失败。这时光报「类型不对」不够友好，应该再附一条「你是不是覆盖了标准库？用 `std.text` 访问」。`SetRule` 和 `ShowRule` 的 selector 求值都在 cast 失败路径上调用了它。

#### 4.5.2 核心流程

```
fn hint_if_shadowed_std(vm, callee: &ast::Expr, mut err: HintedString) -> HintedString:
    if callee 是 Ident(ident) 且 vm.scopes.check_std_shadowed(ident.get()):
        err.hint("use `std.{ident}` to access the shadowed standard library function")
    err
```

它只做一件事：如果出错的表达式恰好是个标识符，且这个标识符在当前作用域里「遮蔽了标准库同名项」，就给错误追加一条 hint，否则原样返回错误。

#### 4.5.3 源码精读

> [src/vm.rs:95-109](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/vm.rs#L95-L109)：`hint_if_shadowed_std`——判定标识符是否遮蔽标准库并附加 hint。

它在 `rules.rs` 的两处被调用：`SetRule` 的 target cast 失败时（4.1.3 的 25 行），以及 `ShowRule` 的 selector cast 失败时（4.2.3 的 46 行）。调用形态都是 `.map_err(|err| hint_if_shadowed_std(vm, &target_expr/&sel, err))`——把一个普通错误喂进去，可能得到一个「带 hint 的」错误。

这体现了 typst-eval 一贯的「错误信息 + 精确定位 + 修复提示」三要素诊断哲学（见 u1-l4、u6-l1）：错误本身描述发生了什么，span 定位在哪里，hint 告诉用户怎么改。

#### 4.5.4 代码实践

1. **实践目标**：预测 `hint_if_shadowed_std` 在何种代码下会附加 hint。
2. **操作步骤**：考虑下面两段代码，判断 set 规则报错时是否会附加「use `std.text` …」hint：
   - `#let text = "hello"; #set text(size: 12pt)`（局部 `text` 遮蔽了标准库 `text`）
   - `#set "notafunc"(size: 12pt)`（目标是字符串字面量，不是标识符）
3. **需要观察的现象**：第 1 段 target 是 `Ident(text)` 且被遮蔽，命中 hint；第 2 段 target 不是 `Ident`，直接返回原错误。
4. **预期结果**：第 1 段附加 hint，第 2 段不附加。
5. **待本地验证**：用 typst CLI 编译确认 hint 是否出现。

#### 4.5.5 小练习与答案

**练习**：`hint_if_shadowed_std` 为什么只在 `callee` 是 `ast::Expr::Ident` 时才检查，而不是对所有表达式都查？

**参考答案**：因为「遮蔽」这个概念只对**标识符**有意义——只有标识符才会按作用域查找到「被覆盖的标准库项」。像字符串字面量、字段访问、函数调用结果等表达式，本身不存在「遮蔽标准库」一说，查了也没意义，反而可能误报。所以函数用 `if let ast::Expr::Ident(ident)` 精确缩小检查范围。

---

### 4.6 补充：set/show 如何作用于 tail（承接 u2-l3 / u2-l4）

虽然本讲的五个最小模块已覆盖 `rules.rs` 全部内容，但要真正理解 `set`/`show` 的运行时行为，必须知道求值器拿到 `Styles`/`Recipe` 之后做了什么。这部分在 `eval_code`（u2-l3）与 `eval_markup`（u2-l4）中已详述，这里只做最小回顾，把闭环接上。

在代码模式中，遇到 `SetRule` 先求出 `styles`，然后**递归求值剩余表达式流**得到 tail，再用 `styled_with_map` 把样式裹上去：

> [src/code.rs:36-44](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L36-L44)：`eval_code` 对 `SetRule` 的 tail 处理。

> [src/code.rs:45-57](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L45-L57)：`eval_code` 对 `ShowRule` 的 tail 处理，用 `styled_with_recipe`。

标记模式完全同构，只是累加器从 `Value`+`ops::join` 换成 `Vec<Content>`+`sequence`：

> [src/markup.rs:35-51](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/markup.rs#L35-L51)：`eval_markup` 对 set/show 的 tail 处理。

而 `styled_with_map` / `styled_with_recipe` 的真实实现：前者在空样式时直接返回原内容，否则包成一个 `StyledElem`；后者在无选择器时「急切应用」recipe，否则只把 recipe 挂到内容上等排版期再匹配。

> [crates/typst-library/src/foundations/content/mod.rs:337-348](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/mod.rs#L337-L348)：`Content::styled_with_recipe`，无选择器时急切 apply。

> [crates/typst-library/src/foundations/content/mod.rs:375-386](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/content/mod.rs#L375-L386)：`Content::styled_with_map`，空样式直接返回。

正因 `set`/`show` 需要「后续 tail」才能作用，它们在普通表达式位置（如 `#(set text(size: 1))`）会被 `Expr::eval` 的 `forbidden` 闭包拦截：

> [src/code.rs:136-137](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L136-L137)：`Self::SetRule(_)` / `Self::ShowRule(_)` 走 `forbidden` 报错。

## 5. 综合实践

**任务**：把本讲五个模块串起来，做一次完整的「源码 → 行为 → 诊断」追踪。

**步骤**：

1. **写一份最小文档**（保存为 `setshow.typ`）：
   ```typ
   // 第 1 行：合法的条件 set，作用于后续正文
   #set text(size: 12pt) if true
   Hello

   // 第 4 行：带 set 子规则的 show（命中 Transformation::Style，但 selector 非 page/par，不触发 lint）
   #show heading: set text(red)
   = 标题

   // 第 8 行：触发 check_show_page_rule
   #show page: none

   // 第 11 行：触发 check_show_par_set_block
   #show par: set block(spacing: 12pt)
   正文段落。
   ```
2. **源码追踪**：对照本讲，逐条标注：
   - 第 1 行走 `SetRule::eval` 的条件守卫（`true` 通过）→ `Element::set` → `liftable` → 被 `eval_code` 裹到 `Hello` 上。
   - 第 4 行走 `ShowRule::eval`，transform 命中 `SetRule` 分支 → `Transformation::Style`；selector 是 `heading`，两条 lint 都不命中。
   - 第 8 行组装 `Recipe` 后命中 `check_show_page_rule`，发警告。
   - 第 11 行组装 `Recipe` 后命中 `check_show_par_set_block`，发带两条 hint 的警告。
3. **本地验证**：用 typst CLI 编译该文档（如 `typst compile setshow.typ`），观察 stderr 是否输出第 8、11 行的两条警告；再分别注释掉第 8、11 行，确认警告消失。
4. **进阶思考**：如果把第 1 行的条件改成 `if "yes"`，会在哪一步报错？为什么不会走到 `Styles::new()` 分支？

**预期结果**：第 8、11 行各产生一条（组）警告，文档仍能成功编译；第 1 行若条件非法则在条件 `cast::<bool>` 处报错。

## 6. 本讲小结

- `SetRule::eval` 的 `Output` 是 `Styles`：先处理可选条件守卫（cast 成 `bool`，假则返回空 `Styles`），再校验目标是元素函数（`to_element`），最后 `Element::set` 产出样式并 `.liftable()` 标记可上浮。
- `ShowRule::eval` 的 `Output` 是 `Recipe`：求可选 selector（`ShowableSelector`）、求 transform（`SetRule` 子规则走 `Transformation::Style`，其余 `cast::<Transformation>`），组装成 `Recipe`。
- `set`/`show` 不是普通表达式——它们需要「后续 tail」才能作用，所以在 `Expr::eval` 里被 `forbidden` 拦截，只在语句流（`eval_code`/`eval_markup`）里以「样式作用域」形式作用于剩余内容。
- `check_show_page_rule` 检测 `show page` 无效；`check_show_par_set_block` 检测过时的 `show par: set block(spacing: ..)` 写法——后者依赖已求值的 `Styles`，故只能放在求值阶段。
- `hint_if_shadowed_std` 在 cast 失败时，若目标标识符遮蔽了标准库同名项，附加「用 `std.xxx` 访问」的修复提示，体现「错误信息 + 定位 + 修复提示」三要素诊断哲学。

## 7. 下一步学习建议

- 下一讲 **u5-l3（可变访问 Access 与内置方法）**会讲解 `push`/`pop`/`insert`/`remove` 等变更方法如何通过 `Access` trait 拿到 `&mut Value` 就地修改，与 set 规则「产出新样式」的不可变思路形成对照。
- 若想深入理解 `Recipe` 在排版期如何被真正应用，建议阅读 typst-library 的 `Content::styled_with_recipe` 与 `Recipe::apply`，以及排版阶段对 show 规则的匹配逻辑（超出 typst-eval 范围）。
- 若对诊断体系感兴趣，可跳读 **u6-l1（错误处理、诊断与提示体系）**，看 `bail!`/`error!`/`warning!` 宏与 `Hint`/`SubRange` 如何系统化地构造高质量诊断。
