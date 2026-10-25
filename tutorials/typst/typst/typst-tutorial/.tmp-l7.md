# 样式系统：Styles 与 set/show 的数据模型

> 阅读前提：本讲属于第三单元「类型与求值」，依赖 u3-l4（Content 与元素机制）。本讲只讲「样式如何表示与查找」，show 规则的真正执行（realize 阶段）留给下一单元。

## 1. 本讲目标

学完本讲，你应当能够：

1. 说出 set 规则与 show 规则在数据层上的真实形态：前者是 `Property` 列表，后者是 `Recipe`，二者共同住在 `Styles` 容器里。
2. 解释 `StyleChain` 的链式结构与查找优先级：实例字段 > 最近的 set > 外层 set > 元素默认值。
3. 理解 `Fold` 与 `Resolve`：多个样式值如何合并（如嵌套字号的「线性函数复合」）、相对值如何解析为绝对值。
4. 描述 `context` 表达式的实现：为什么 `#context text.size` 要等到排版期才能求值，以及缺少上下文时那条著名报错「can only be used when context is known」从哪行代码发出。
5. 为下一单元的 realize 阶段（show 规则的执行引擎）打好数据模型基础。

## 2. 前置知识

**（1）样式不是全局状态，是挂在内容上的数据。** 这是本讲最重要的一条直觉。`#set text(size: 12pt)` 并不修改任何全局变量，而是生成一份不可变的 `Styles` 数据，包裹住「它之后的内容」。这带来三个直接后果：

- 样式天然可组合（数据可以嵌套包裹）；
- 样式天然可缓存（不可变 + 可哈希，服务 comemo 增量编译）；
- 「set 只作用于其后内容」不是魔法，是求值器递归结构的结果（4.2 节会看到只有 8 行代码）。

**（2）作用域（Scope）与样式（Styles）是两套系统。** 作用域管「变量名到值」的查找，在求值期（typst-eval）随函数调用压栈弹栈；样式管「元素字段最终取什么值」，主要在排版期（realize/layout）沿 `StyleChain` 查找。初学者常把 `#set` 理解成「给变量赋值」，这是本讲要纠正的第一个误解。

**（3）set/show 语法回顾。**

```typst
#set text(size: 12pt)          // 之后的文本默认 12pt
#show heading: it => it.body   // 之后的标题只显示正文部分
```

**（4）LazyHash 与 comemo。** u3-l1 讲过：comemo 依赖参数的哈希值判断缓存是否命中。`LazyHash<T>` 把「计算哈希」从每次比较变成构造时一次。样式系统里几乎每个数据都被 `LazyHash` 包裹，就是为了增量编译。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `crates/typst-library/src/foundations/styles.rs` | 本讲主战场：`Styles`、`Style`、`Property`、`Recipe`、`Transformation`、`StyleChain`、`Fold`/`Resolve` 全部在此（全文约 1164 行） |
| `crates/typst-eval/src/rules.rs` | 求值侧：`SetRule` 求值为 `Styles`，`ShowRule` 求值为 `Recipe`（全文仅 95 行，值得整读） |
| `crates/typst-eval/src/markup.rs` | `eval_markup` 中把样式挂到「其后内容」上的 8 行关键代码 |
| `crates/typst-library/src/foundations/content/mod.rs` | `Content::styled` / `styled_with_map` / `styled_with_recipe`：样式如何包住内容 |
| `crates/typst-library/src/foundations/context.rs` | `Context` 与 `ContextElem`：`context` 表达式的数据面（全文 87 行） |
| `crates/typst-library/src/foundations/scope.rs` | 对照组：`Scopes` 是变量栈，与样式无关 |
| `crates/typst-library/src/text/mod.rs` | `TextElem.size` 及 `TextSize` 的 `Fold`/`Resolve` 实现：样式属性的真实消费者 |
| `crates/typst-library/src/lib.rs` | `Library.styles`：文档级默认样式（样式链的根） |

## 4. 核心概念与源码讲解

### 4.1 模块一：Styles 与 Style —— 样式的基本载体

#### 4.1.1 概念说明

样式系统分三层：

- **`Styles`**：一次 set/show 规则产生的「样式列表」，是一个有序、可重复的序列（不是字典！顺序本身就是语义）。
- **`Style`**：列表中的单条样式，只有三个变体。
- **`Property` / `Recipe`**：真正的负载数据。

为什么 `Styles` 是列表而不是哈希表？因为样式的语义是「叠加」而非「覆盖」：同一字段可以先后被 set 多次，最终谁生效由**位置**（离元素近不近）和**折叠规则**（Fold）决定，而不是由键的覆盖决定。这个设计决策贯穿整个文件。

#### 4.1.2 核心流程

```text
#set text(size: 12pt)
      │ 求值（typst-eval/rules.rs）
      ▼
Styles = [ Property{elem: text, id: size, value: 12pt, ...} ]
      │ 包裹其后内容（typst-eval/markup.rs）
      ▼
Content::Styled { child: 其后内容, styles: 上述 Styles }
      │ 排版期逐层下潜时串成链
      ▼
StyleChain → 查找/折叠出字段最终值
```
#### 4.1.3 源码精读

`Styles` 的定义只有一行——一个被 `LazyHash` 包裹的小向量：

- [styles.rs:L22-L25](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/styles.rs#L22-L25)：`Styles` 就是一个 `EcoVec<LazyHash<Style>>`。`EcoVec` 是小向量优化（短向量内联存储），`LazyHash` 为 comemo 缓存服务。

写入与合并的操作：

- [styles.rs:L61-L78](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/styles.rs#L61-L78)：`set` 把一个字段值变成 `Property` 压入列表；`push` 压入任意样式；`unset` 弹出最后一条（供 Rust 侧代码使用）。
- [styles.rs:L80-L89](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/styles.rs#L80-L89)：`apply(outer)` 把外层样式合并进来——注意方向：`outer` 排在前面（更外层），自身（更内层）排在后面。列表中**越靠后越内层**，这个约定与后面 `StyleChain` 的查找方向严格对应。

`Style` 是三变体枚举：

- [styles.rs:L222-L234](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/styles.rs#L222-L234)：`Style::Property`（来自 set 规则或元素构造参数）、`Style::Recipe`（来自 show 规则）、`Style::Revocation`（禁用某条已存在的 show 规则；源码注释说明目前只用于 regex 类 show 规则）。

`Property` 的六个字段值得逐个认识：

- [styles.rs:L324-L339](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/styles.rs#L324-L339)：`elem`（属于哪个元素，运行时句柄）、`id`（字段编号，一个 `u8`）、`value`（类型擦除的值，装在 `Block` 里）、`span`（来源 set 规则的位置，报错用）、`liftable`（允许被提升到页面级）、`outside`（是否在 show 规则之外应用，realize 阶段标记）。

`elem + id` 这对组合是宏生成代码的产物：`#[elem]` 宏在编译期就给每个字段编号（`Field<E, I>` 里的 `const I: u8`），运行时不需要字符串比较就能定位字段。

#### 4.1.4 代码实践

**实践目标**：直观感受「样式是数据」——同一个字段被 set 两次时，两条 `Property` 都存在，谁生效取决于位置。

**操作步骤**：

1. 新建 `sizes.typ`，写入：

```typst
{set text(size: 14pt)
14pt 的正文
}text(size: 0.5em)#字号是正文的一半#
```

2. 编译：`typst compile sizes.typ`（或 `cargo run -p typst-cli -- compile sizes.typ`，见 u1-l2）。
3. 打开 [styles.rs:L61-L68](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/styles.rs[L61-L68)，对照理解 `]set text(size: 14pt)` 产生的正是一条 `Property # elem: text 元素, id: size 字段编号, value: 14pt #`。

**需要观察的现象**：方括号里的文字明显小于正文（约为其一半）。

**预期结果**：`0.5em` 在 14pt 环境下解析为 7pt。`#text(size: ..)` 传入的是**实例字段值**（优先级最高，见 4.4 节），它与链上的 `14pt` 发生折叠（em 相对值相乘），最终 14pt 乘以 0.5 等于 7pt。字号换算细节待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Styles` 用 `EcoVec` 而不是 `HashMap<(Element, u8), Value>`？

**答案**：样式的语义是有序叠加而非键覆盖：同一字段可以被 set 多次、可以有折叠语义、`Recipe` 也依赖顺序；哈希表会丢掉顺序信息。同时 `LazyHash` 包裹让整条样式列表的哈希可被缓存，服务 comemo。

**练习 2**：`Style::Revocation` 变体存在的意义是什么？

**答案**：撤销链上已存在的一条 show 规则（按 `RecipeIndex` 从链顶计数定位）。源码注释说明目前只对 regex 类 show 规则生效，普通元素 show 规则用元素上的 guard 直接处理。

**练习 3**：`Property.value` 为什么用 `Block`（即 `Box<dyn Blockable>`）做类型擦除，而不是统一存成 `Value`？

**答案**：样式字段值大多是 Rust 原生类型（如 `TextSize`），存 `Value` 会强制所有字段走脚本类型系统的转换；`Block` 保留 Rust 类型本身，读取时按 `(elem, id)` 精确 downcast，避免脚本层包装开销。脚本层的 `Value` 只在入口（set 规则求值、函数参数）出现一次。

### 4.2 模块二：set 规则的数据形态 —— 从 set 到 Property

#### 4.2.1 概念说明

`#set text(size: 12pt)` 求值时做三件事：

1. **校验目标**：set 的目标必须是**元素函数**（`text`、`heading` 等），普通函数不行；
2. **解析参数**：把参数按元素字段解析成一条条 `Property`（只有 settable 字段允许）；
3. **打标记**：给所有 `Property` 标上来源 `span`（报错定位）和 `liftable`。

然后，markup 求值器把「这条 set 之后的所有内容」**递归求值成一个 `Content`**，再把 `Styles` 作为外壳包上去。「set 只作用于其后内容」的全部实现就这么几行。

#### 4.2.2 核心流程

```text
eval_markup 迭代表达式
  ├─ 遇到 SetRule：
  │    1. set.eval(vm) → Styles          （rules.rs）
  │    2. eval_markup(vm, exprs) → 其后内容的 Content   （递归！迭代器被消耗）
  │    3. content.styled_with_map(styles) → StyledElem 包壳
  │    4. push 进 seq，外层循环自然结束
  └─ 其他表达式：正常求值 push
```

#### 4.2.3 源码精读

set 规则的求值实现（全文只有 25 行）：

- [rules.rs:L11-L35](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/rules.rs#L11-L35)：`impl Eval for ast::SetRule`，输出类型就是 `Styles`。其中：
  - [rules.rs:L15-L19](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/rules.rs#L15-L19)：`set x(..) if cond` 的条件分支——条件求值为假时直接返回**空 `Styles`**，等价于这条 set 不存在；
  - [rules.rs:L26-L29](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/rules.rs#L26-L29)：目标求值后必须能 `to_element()`，否则报「only element functions can be used in set rules」；
  - [rules.rs:L33](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/rules.rs#L33)：核心一行——调用元素函数的 set 能力把参数转成 `Property` 列表，返回值再依次 `.spanned(..)`（打上报错定位）与 `.liftable(..)`（允许提升到页面级）。

「只作用于其后」的 8 行：

- [markup.rs:L36-L43](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/markup.rs#L36-L43)：SetRule 分支。注意 `eval_markup(vm, exprs)` 是**递归调用**，把剩余表达式整体求值成一个 `Content`，再用 `styled_with_map` 包壳；迭代器被递归消耗完，外层 `while` 循环随之结束。

样式如何包住内容：

- [content/mod.rs:L376-L386](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/content/mod.rs#L376-L386)：`styled_with_map`——若目标已经是 `StyledElem`（连续两条 set），直接把新样式 apply 进去（合并外壳）；否则新建一层 `StyledElem::new(child, styles)`。所以连续 set 会被优化成一层外壳、列表内多条 `Property`，而**跨内容块**的 set 则形成嵌套的 `StyledElem` 层级。
- [content/mod.rs:L366-L373](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/content/mod.rs#L366-L373)：单条样式的 `styled`，连续包装时用 `apply_one` 前插（保持「越前越外层」的约定）。

文档级默认样式的来源：

- [lib.rs:L179](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/lib.rs#L179)：`Library` 结构体的 `styles` 字段。宿主可以在 `LibraryBuilder` 里注入文档级默认样式，它将成为样式链最底部的根。

#### 4.2.4 代码实践

**实践目标**：验证「跨内容块的 set 形成嵌套层级、块内的 set 不外泄」。

**操作步骤**：

1. 新建 `nested-set.typ`：

```typst
#set text(size: 12pt)
外层 12pt
#[
  #set text(size: 0.5em)
  块内 6pt
]
块外仍是 12pt
```

2. 编译并观察三行文字的字号。
3. 对照 [markup.rs:L36-L43](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/markup.rs#L36-L43) 解释：第二条 set 在**内容块内部**求值，它递归包住的只是块内内容；块结束后回到外层，`StyledElem` 层级随之弹回。

**需要观察的现象**：三行字号呈 12pt / 6pt / 12pt 阶梯。

**预期结果**：如上。若与你观察到的结果不符（例如 em 行为细节），以本地编译结果为准，并回到 [text/mod.rs:L291-L294](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/text/mod.rs#L291-L294) 的字段定义处核对折叠语义。

#### 4.2.5 小练习与答案

**练习 1**：把 `#set text(size: 12pt)` 里的 `text` 换成普通函数 `upper`，会发生什么？

**答案**：编译报错「only element functions can be used in set rules」。对应 [rules.rs:L26-L29](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/rules.rs#L26-L29) 的 `to_element()` 失败路径。

**练习 2**：`#set heading(level: 2) if cond` 这类带条件的 set 是怎么实现的？

**答案**：[rules.rs:L15-L19](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/rules.rs#L15-L19)：条件在求值期求值，为假时返回空 `Styles`。注意这发生在**求值期**——如果条件依赖排版信息（如页码），必须配合 `context` 使用。

**练习 3**：连续写两条 set（中间没有内容块），会产生两层 `StyledElem` 吗？

**答案**：不会。[content/mod.rs:L380-L382](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/content/mod.rs#L380-L382)：外层发现目标已是 `StyledElem` 时直接合并样式列表，只有一层外壳、两条 `Property`。

### 4.3 模块三：show 规则的数据形态 —— Recipe 与 Transformation

#### 4.3.1 概念说明

`#show heading: it => ...` 的求值产物是一条 `Recipe`。关键认知：**show 规则在求值期不执行任何转换**，它只是把「选择器 + 变换」打包成数据挂到内容上，等 realize 阶段遇到匹配的元素才真正执行（下一讲的主题）。本讲的任务是讲清它的数据形态。

`Recipe` 四个字段，`Transformation` 三个变体，对应 show 规则的三种写法：

| 写法 | Transformation 变体 | 含义 |
|---|---|---|
| `#show heading: [替代内容]` | `Content` | 匹配元素直接替换为固定内容 |
| `#show heading: it => ..` | `Func` | 匹配元素交给函数处理 |
| `#show heading: set text(..)` | `Style` | 变换本身是一条 set 规则（给匹配元素套样式） |

#### 4.3.2 核心流程

```text
eval_markup 遇到 ShowRule：
  1. show.eval(vm) → Recipe { selector, transform, span }
  2. eval_markup(vm, exprs) → 其后内容的 Content（递归，与 set 对称）
  3. content.styled_with_recipe(recipe)：
       - selector 为 None（show: rest => ..）→ 立即执行 recipe.apply（eager）
       - selector 为 Some → 包成 StyledElem，等 realize 匹配时执行（lazy）
```

#### 4.3.3 源码精读

`Recipe` 的字段：

- [styles.rs:L453-L469](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/styles.rs#L453-L469)：`selector: Option<Selector>`（`None` 表示无选择器的 `show: rest => ..`，源码注释明确它会被**立即应用**）、`transform: Transformation`、`span`（报错定位）、`outside`。

`Transformation` 三变体：

- [styles.rs:L546-L552](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/styles.rs#L546-L552)：`Content(Content)` / `Func(Func)` / `Style(Styles)`。随后的 `cast!` 宏让脚本层的 `Content` 和 `Func` 值能自动转换成对应变体。

show 规则的求值：

- [rules.rs:L37-L64](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/rules.rs#L37-L64)：`impl Eval for ast::ShowRule`，输出 `Recipe`。其中：
  - [rules.rs:L41-L50](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/rules.rs#L41-L50)：选择器求值后被 cast 成 `ShowableSelector`——并不是所有 `Selector` 变体（u3-l4 讲过九种）都能用于 show 规则；
  - [rules.rs:L52-L56](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/rules.rs#L52-L56)：变换的分支——如果 transform 位置写的是一条 set 规则，就包装成 `Transformation::Style(styles)`；否则求值后 cast 成 `Transformation`（Content 或 Func）；
  - [rules.rs:L58-L60](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/rules.rs#L58-L60)：组装 `Recipe` 并做两个防呆检查（`show page` 无效警告等，见 [rules.rs:L66-L77](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/rules.rs#L66-L77)）。

eager 与 lazy 的分岔点：

- [markup.rs:L44-L52](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/markup.rs#L44-L52)：ShowRule 分支，结构与 set 分支完全对称。
- [content/mod.rs:L338-L349](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/content/mod.rs#L338-L349)：`styled_with_recipe`——**selector 为 `None` 时立即调用 `recipe.apply`**（eager，`show: rest => ..` 的语义就是「现在就处理剩余内容」）；有 selector 则作为普通样式包壳，延迟到 realize。

realize 期真正执行的入口（先睹为快）：

- [styles.rs:L497-L524](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/styles.rs#L497-L524)：`Recipe::apply`——`Content` 变体直接替换；`Func` 变体调用函数并把结果 display（其中还给错误栈同时加上「show 规则定义点」与「元素应用点」两个 trace）；`Style` 变体调用 `styled_with_map`。产物若 span 脱离则用 recipe 的 span 补上。


#### 4.3.4 代码实践

**实践目标**：体验 `Transformation` 三种变体的行为差异。

**操作步骤**：

1. 新建 `transforms.typ`：

```typst
#show heading: [被替换了]        // Content 变体
#show strong: it => it.body      // Func 变体
#show emph: set text(red)        // Style 变体
= 原标题
*加粗* 与 _强调_
```

2. 编译并观察三处变化。
3. 对照 [rules.rs:L52-L56](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/rules.rs#L52-L56)：确认三种写法分别走了哪个分支。

**需要观察的现象**：标题变成固定文字「被替换了」；「加粗」失去粗体（只剩正文）；「强调」的文字变红。

**预期结果**：如上。`emph` 的斜体是否保留取决于其默认 show 规则（`set text(style: "italic")`）与本条 `Style` 变体的叠加顺序，细节待本地验证。
#### 4.3.5 小练习与答案

**练习 1**：`show: rest => rest` 这种没有 selector 的 show 规则在求值期会立即执行，还是等 realize 才执行？

**参考答案**：立即执行。`selector` 为 `None` 时 `styled_with_recipe` 直接调用 `recipe.apply`，闭包拿到的 `rest` 就是「这一行之后的所有内容」包装成的 Content；返回值替换原文继续参与后续求值。

**练习 2**：`Recipe::apply` 的 Func 分支为什么要给错误栈加两个 trace 点？

**参考答案**：闭包体报错时，出问题的地方有两处可归因——闭包里写的代码（调用点）和写出这条 show 规则的地方（定义点）。第一个 trace 指向规则定义处的 span，第二个指向规则被应用到的元素，用户据此能快速定位是哪条 show 规则、作用在哪个元素上出错。

**练习 3**：为什么 show 规则右侧不能随便写一个 `set` 之外的表达式，比如 `show heading: 42`？

**参考答案**：右侧表达式会被强转为 `Transformation`，只接受三种：Content、函数、或求值结果为 Styles 的规则（目前即 `set` 规则）。`42` 是普通整数，转换失败会在编译期报错。这也是「数据层只有三种转法」的直接体现。

### 4.4 StyleChain：样式的查找与折叠

#### 4.4.1 概念说明

Styles 只是一叠「样式条目」，真正回答「某个元素的某属性**现在**取什么值」的，是 StyleChain（样式链）。它把许多层 Styles 串成一条从内向外的链，realize 与布局期用它查值。

三个关键认识：

1. **链式结构**：StyleChain 由 head（最近一层 Styles）与 tail（外层链）组成，本质是一条不可变借用链表，逐层拼接零拷贝。
2. **查找优先级**：从 head 向外找，第一个命中的 Property 胜出；元素实例自身字段比任何 set 都近；全都找不到时回落到元素定义的默认值。
3. **两种加工**：Fold（沿链把多层值复合成一个）与 Resolve（拿整条链把相对值换算为绝对值）。字号 TextSize 两者都用。

与 u3-l4 呼应：Content 树回答「有哪些元素」，StyleChain 回答「此刻每个属性取什么值」，两者在 realize 期汇合。

#### 4.4.2 核心流程

属性查找：

1. 元素实例自身字段（构造时显式传入的参数）优先。
2. StyleChain 沿 head→tail 方向找第一个匹配 (elem, id) 的 Property。
3. 若属性声明为 fold：把链上所有命中值从外到内依次复合成一个值。
4. 仍无结果则取元素定义处的默认值。
5. 声明为 resolve 的属性最后用整条链换算（如 em→pt）。

字号复合的数学形态：每条 set text(size: s) 等价于一个一次函数 f(x) = e·x + a（相对部分 e 与绝对部分 a），嵌套 set 的效果就是函数复合：
\[ (f_i \circ f_o)(x) = e_i\,(e_o x + a_o) + a_i = (e_i e_o)\,x + (e_i a_o + a_i) \]

即：相对量相乘；外层绝对量先被内层相对量缩放，再与内层绝对量相加。


#### 4.4.3 源码精读

- [styles.rs:L579-L584](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/styles.rs#L579-L584)：StyleChain 定义——`head: &Styles` 与 `tail: Option<&Self>` 两个引用字段，链表式结构。
- [styles.rs:L698-L701](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/styles.rs#L698-L701)：`chain()`——把一层 Styles 压到链头。越晚压入的越靠 head，查找时越先被看到，这就是「内层覆盖外层」的实现。
- [styles.rs:L602-L660](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/styles.rs#L602-L660)：`get` 系列入口——`get` 直接返回引用、`get_cloned` 克隆、`get_ref` 拿原始字段，都要求属性实现了 Fold。
- [styles.rs:L666-L691](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/styles.rs#L666-L691)：`get_folded` 与 `find`——find 沿链逐层找 Property；get_folded 在 find 基础上逐层做 fold；`properties` 惰性遍历链上所有属性条目。
- [text/mod.rs:L291-L294](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/text/mod.rs#L291-L294)：TextElem 的 size 字段声明为 #[fold]——一行宏属性决定了它走折叠路径。
- [text/mod.rs:L1150-L1160](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/text/mod.rs#L1150-L1160)：TextSize 的 Fold 实现——相对量相乘、绝对量 `em_i·abs_o + abs_i`，正是上文一次函数复合公式。
- [styles.rs:L910-L941](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/styles.rs#L910-L941)：Fold trait 定义与文档示例（`set rect(stroke: red)` 后再 `set rect(stroke: 4pt)`，结果是 4pt 红描边——颜色与宽度各取所长）；Option 的 Fold 在 Some/None 间择一。
- [text/mod.rs:L1162-L1175](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/text/mod.rs#L1162-L1175)：TextSize 的 Resolve——用整条链把 em 换算成 pt（含公式字号联动）。

#### 4.4.4 代码实践

**实践目标**：用三层嵌套字号验证「折叠=一次函数复合」。

**操作步骤**：

1. 新建 `fold-lab.typ`：

```typst
#set text(size: 12pt)
12pt 的基准
#[ #set text(size: 14pt)
  14pt 文本
  #[ #set text(size: 0.8em)
    0.8em：14pt × 0.8 = 11.2pt
  ]
]
```

2. 编译，观察三段文字字号递变。
3. 把最内层改成 `1.2em`，预期 14 × 1.2 = 16.8pt；再改成 `16pt`（绝对值），预期所有层固定 16pt——绝对值替换外层相对量。

**需要观察的现象**：内层 0.8em 不是「0.8 × 12pt」而是「0.8 × 14pt」——相对值锚定在**直接外层**的实际字号上。

**预期结果**：如上。可用 PDF 阅读器放大目测或编译成 PNG 对比（`typst compile fold-lab.typ --format png`），精确数值待本地验证。
#### 4.4.5 小练习与答案

**练习 1**：为什么 StyleChain 用「借用链表」而不是一开始就把所有层合并成一个大 Styles？

**参考答案**：两层 Styles 拼接（apply）需要遍历拷贝整段数据，而链表拼接只是把 head 指向新层、tail 指向旧链，O(1) 完成且不破坏原链；嵌套 markup 每进出一层就压/弹一层，链表正好匹配这种栈式用法。此外指针相等的 PartialEq 让 comemo 对相同前缀的链命中缓存，避免重复求值。

**练习 2**：`text(size: 10pt) abc ` 与 `#set text(size: 10pt)` 都能改变字号，谁的优先级高？

**参考答案**：前者。元素调用传的是实例字段，构造时就写进元素自身，查值时先于链上任何 Property；set 只能影响「没有显式给值的」那部分。这也解释了 show 规则闭包里 `it.body` 的字段永远保持作者写下的原值。

**练习 3**：非 fold 属性查值只要链上第一个命中，fold 属性却要收集**所有**命中并复合——为什么 stroke 需要后者？

**参考答案**：stroke 是结构体（颜色、粗细、端点样式……），用户常分层设置：外层只定颜色、内层只加粗细。若只取第一个命中，外层颜色会被内层整条覆盖。Fold 把外层值与内层值逐字段复合，各层的贡献都保留。数学上即要求折叠运算满足结合律，任意嵌套顺序结果一致。

### 4.5 context 机制：把样式与环境交给求值期

#### 4.5.1 概念说明

Typst 是多轮编译语言：布局位置、最终字号这些信息，**求值期（eval）还不存在**，要到 realize/布局期才确定。可有时用户就是想在代码模式下拿到「当前位置」「当前字号」。

`context expr` 就是这个通道：它把 `expr` 打包成闭包挂起，推迟到 realize 期，在真正知道位置与样式的地方带着完整 Context 再执行一次。

与之对照，普通变量走的是 **Scopes（作用域栈）**——另一套完全独立的机制，管的是「这个名字此刻绑定哪个值」；Context 管的是「这段代码此刻身处何处」。两者常被混淆，务必区分：

| 维度 | Scopes | Context |
| --- | --- | --- |
| 回答什么 | 名字绑定到哪个值 | 代码在哪里、样式链是什么 |
| 数据结构 | 栈（enter/exit） | location + styles 两个字段 |
| 何时存在 | 求值期全程 | realize 期组装，求值期多数情况没有 |
| 典型用户 | 普通变量与函数参数 | `context` 表达式、样式查询 |

#### 4.5.2 核心流程

context 表达式的一生：

```text
源码: context here()
  ↓ 求值期（code.rs）
body 打包成闭包 ClosureNode::Context
  ↓ 包成 Func，造一个 ContextElem 装进去
Content 树里多出一个透明元素：ContextElem(func)
  ↓ realize 期（CONTEXT_RULE）
从引擎处组装 Context，location 与 styles 就位
  ↓
以 Context 为参数调用闭包 → here() 得到真实位置
  ↓
返回值参与后续 realize
```

关键理解：**ContextElem 是 Content 与样式世界的桥**——求值期它是普通 Content，realize 期它的 show 规则触发闭包回放。


#### 4.5.3 源码精读

- [context.rs:L16-L21](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/context.rs#L16-L21)：Context 只有两个字段——`location`（当前位置）与 `styles`（当前样式链），是「我在哪、长什么样」的最小载体。
- [context.rs:L35-L51](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/context.rs#L35-L51)：[]comemo::track# 修饰的一组只读访问器——location、styles、introspect。track 让纯函数式缓存感知字段变化：字段没变就复用上次结果。
- [context.rs:L55-L61](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/context.rs#L55-L61)：`require()`——上下文不存在时的报错与提示的来源。你在函数里写 `here()` 却忘了套 context，看到的那句错误就来自这里。
- [context.rs:L64-L70](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/context.rs#L64-L70)：ContextElem 定义——标记 `Construct` 与 `Locatable` 的内部元素，用户无法直接构造。
- [context.rs:L78-L87](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/context.rs#L78-L87)：CONTEXT_RULE——realize 期规则：取 location、取 styles、组装 Context、调用元素携带的闭包。这是整个机制的收口点。
- [code.rs:L401-L412](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-eval/src/code.rs#L401-L412)：求值期入口——body 转闭包（ClosureNode::Context）、闭包转 Func、装进 ContextElem 打包返回。挂起而非执行。
- #scope.rs:L21-L48[(https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/scope.rs]L21-L48)：对照组——Scopes 的 enter/exit 只做栈的压入弹出，与样式链毫无交集。
#### 4.5.4 代码实践

**实践目标**：亲眼看到「不套 context 就拿不到样式」的报错，以及套上后能取到折叠后的字号。

**操作步骤**：

1. 新建 `ctx-lab.typ`：

```typst
#set text(size: 14pt)

字体大小：这里会报错——把下面一行的注释去掉试试：
// #text.size

这样才对：字体大小是 #context text.size
```

2. 先整体编译一次（第二处仍被注释），应成功输出「字体大小是 14pt」。
3. 去掉 `// ` 让 `#text.size` 生效，再编译。

**需要观察的现象**：第 3 步编译失败，错误信息提示 text.size 只能在 context 已知时使用，并给出「用 context 包起来」的修复建议；第 2 步成功且数值 14pt 正是链上折叠的结果。

**预期结果**：如上，报错文案与修复提示来自 context.rs 的 require 路径；具体措辞待本地验证。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `text.size` 不能在普通代码位置直接取，非要包一层 context？

**参考答案**：字号是折叠+解析后的结果，取决于该位置的完整样式链；而求值是先把整篇 markup 跑完再布局，普通表达式执行时这条链还不存在。context 把取值动作推迟到 realize 期、链已组装好的时刻。

**练习 2**：ContextElem 明明携带的是函数，为什么要伪装成 Content 树里的一个元素？

**参考答案**：Content 是唯一贯穿求值与 realize 两个阶段的数据载体。做成元素，它就能自然嵌进文档流、跟随所在位置参与 realize；其 show 规则（CONTEXT_RULE）被触发时就地回放闭包。这复用了整套 show 机制，不用另开一条求值通道。

**练习 3**：`context here()` 在同一次编译里可能得到不同值吗？

**参考答案**：会。每个 ContextElem 的 realize 都在自己所在位置组装 Context，location 各不相同；且多轮布局（交叉引用需要迭代）时同一处的位置也可能在轮次间变化，这正是 comemo 按 location/styles 变化做增量重算的意义。

## 5. 综合实践

把本讲三样东西——嵌套 set、show 的两种变身、context 查询——放进同一个文件 `styles-lab.typ`：

```typst
#set text(size: 12pt)
#show heading: it => text(fill: navy, size: 14pt)[#it.body]
#show heading: set text(size: 14pt)

= 标题 A

正文 12pt。
#[ #set text(size: 14pt)
  14pt 文本
  #[ #set text(size: 0.8em)
    0.8em → 11.2pt
  ]
]

当前字号（context 查询）：#context text.size
```

1. 编译，确认：标题为 14pt 藏青色；三段正文依次 12pt、14pt、11.2pt；末行输出 12pt。
2. 两条 show 规则只保留第一条，重新编译，标题仍应等效——体会 Func 变体与 Style 变体殊途同归。
3. 画（或对照下面的参考图写出）这份文档求值后的数据形态：

```text
Document
├─ StyledElem(styles: 12pt 的 Property, child: ……)       ← set 包壳
│   └─ StyledElem(recipe: show heading → 闭包, child: ……) ← show 包壳
│       └─ HeadingElem(body: 标题 A)
├─ TextElem(正文 12pt。)
├─ StyledElem(styles: 14pt 的 Property, child:
│   └─ StyledElem(styles: 0.8em 的 Property, child:
│       └─ TextElem(11.2pt 文本)))
└─ TextElem(前缀) + ContextElem(闭包: text.size) + …
```

4. 用一句话总结每种规则在数据层的形态差异。参考答案：**set 求值期立即变成 Property 条目装进 Styles，随嵌套就近生效、fold 属性还会逐层复合；show 变成 Recipe 条目同住 Styles，但只是登记，要等 realize 匹配到目标元素才执行其 Transformation；context 则把闭包塞进 ContextElem 挂起，realize 期组装 Context 后回放**。三者的共同点是都以 Content 树上的 StyledElem/内部元素为载体，差异全在「何时被消费」。
## 6. 本讲小结

- **Styles 是容器不是答案**：它只是一叠 Style 条目；Property（set 的产物）、Recipe（show 的产物）、Revocation 三种条目混装，真正回答「属性取什么值」的是 StyleChain。
- **set 立即、show 延迟**：set 规则求值当场变成 Property 装进 Styles 包住后续内容；show 规则只是登记 Recipe，等 realize 匹配到元素才执行（无 selector 的 `show: rest => …` 是唯一例外，求值期立即执行）。
- **链式查找有优先级**：实例字段 > 内层 set > 外层 set > 元素默认值；StyleChain 以 head/tail 借用链表实现，O(1) 拼接、指针相等即相等，comemo 缓存友好。
- **Fold 与 Resolve 分工**：fold 属性（如字号、stroke）把多层值按结合律复合——字号即一次函数复合；resolve 用整条链把相对值换算为绝对值。
- **context 是两阶段的桥**：求值期挂起闭包进 ContextElem，realize 期由 CONTEXT_RULE 组装 location+styles 回放；Scopes 管名字绑定，与 Context 互不相干。
- **Transformation 三变体**：Content 直接替换、Func 调用闭包（错误双 trace）、Style 注入样式——`show x: set …` 只是第三种的语法糖。

## 7. 下一步学习建议

本讲看到了 Recipe 被登记、ContextElem 挂起，但它们真正的消费现场——realize——还没展开。下一讲建议：

1. 学习 realize 流程：StyledElem 如何被剥开、recipes() 如何被逐条匹配、show 产物如何递归处理（对应本讲引用过的 Recipe::apply 调用方）。
2. 阅读 [styles.rs:L1012-L1013](https://github.com/typst/typst/blob/64c5e8c02fe0394d913ead36e42ef8c1334d2107/crates/typst-library/src/foundations/styles.rs#L1012-L1013) 起的 NativeRuleMap——每个元素自带的原生 show 规则表，realize 匹配时的最后一级回落。
3. 留两个本讲没拆的钩子：Property/Recipe 的 `outside` 标志位与 liftable（它们改写条目在链上的位置语义），可在阅读 realize 源码时回头验证。
