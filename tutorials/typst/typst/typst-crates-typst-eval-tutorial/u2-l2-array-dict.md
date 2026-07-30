# 集合类型求值：数组与字典

## 1. 本讲目标

本讲聚焦 typst-eval 中两类「集合字面量」的求值：**数组**（`Array`）与**字典**（`Dict`）。学完本讲后，你应该能够：

- 说出 `ast::Array` 与 `ast::Dict` 节点在 `Expr::eval` 总分发器中是如何被适配成 `Value::Array` / `Value::Dict` 的；
- 读懂 `Array::eval` 对「位置项」`Pos` 与「展开项」`Spread` 的处理，尤其是展开运算符 `..` 遇到 `Value::None` / `Value::Array` / `Value::Dict` 时的三种不同分支；
- 读懂 `Dict::eval` 对 `Named` / `Keyed` / `Spread` 三类项的处理，并理解为什么字典的 key 必须是 `Str`、以及非法 key 是如何「收集后再统一报错」的；
- 解释 `all_dict_spreads` 这种「前瞻式（lookahead）」错误诊断的设计意图——为什么「全是字典展开」的数组会被提示「加个冒号变成字典」。

本讲承接 [u2-l1 基础字面量与标识符求值](u2-l1-literals-idents.md)：那里讲的是叶子节点（字面量、标识符），这里讲的则是第一类「由多个子表达式组合而成」的结构。

## 2. 前置知识

在进入源码前，先用通俗语言把几个关键概念说清楚。

- **集合字面量**：Typst 源码里用小括号写的复合结构。`(1, 2, 3)` 是数组；`(name: "Typst", year: 2023)` 是字典。语法层把它们解析成 `ast::Array` 与 `ast::Dict` 节点，求值层再把它们变成运行时的 `Array` / `Dict` 值。
- **数组 `Array` vs 字典 `Dict`**：数组是有序、按下标访问的序列；字典是无序、按键（字符串）访问的映射。在运行时，`Array` 底层是 `EcoVec<Value>`，`Dict` 底层是 `IndexMap<Str, Value>`（保留插入顺序、键唯一）。
- **展开运算符 `..`（spread）**：把一个已存在的集合「摊平」进正在构造的字面量。例如 `(..a, 4, ..b)` 会把数组 `a` 和 `b` 的元素依次塞进新数组；`(:..x, ..y)` 会把字典 `x`、`y` 的键值对并入新字典。展开的目标可以是任意表达式（包括函数调用结果，如 `..x.at(0)`）。
- **`Eval` trait 与 `Vm`**：每个 AST 节点都实现 `Eval`，签名是 `fn eval(self, vm: &mut Vm) -> SourceResult<Output>`。`Array::eval` 的 `Output` 是 `Array`，`Dict::eval` 的 `Output` 是 `Dict`。`Vm` 提供作用域、追踪、错误收集等运行时状态。详见 [u1-l4 Eval trait 与 Vm 虚拟机](u1-l1-eval-trait-vm.md)。
- **「前瞻式」诊断**：一种报错策略——在决定报什么错之前，先「向前看一眼」剩下的输入，据此判断用户的真实意图，从而给出更精准的修复提示。本讲的 `all_dict_spreads` 就是典型例子。

> 一个容易混淆的语法点：Typst 用「是否出现冒号」来区分 `()` 里到底是数组还是字典。只要有一项是 `name: value`（命名对）或 `"key": value`（带引号的键值对），或者开头有个裸 `:`（如 `(:)`、`(:..x)`），整个就是字典；否则就是数组。本讲后半段会看到，这条规则正是「全是字典展开的数组」需要特殊提示的根源。

## 3. 本讲源码地图

本讲主要阅读 `crates/typst-eval/src/code.rs`，并少量涉及语法层与运行时类型定义：

| 文件 | 作用 |
| --- | --- |
| `crates/typst-eval/src/code.rs` | **本讲主战场**。包含 `Array::eval`、`Dict::eval`，以及表达式总分发器 `ast::Expr::eval`。 |
| `crates/typst-syntax/src/ast.rs` | 定义 `ArrayItem`（`Pos`/`Spread`）、`DictItem`（`Named`/`Keyed`/`Spread`）、`Spread`、`Named`、`Keyed` 等 AST 节点及其访问器方法。 |
| `crates/typst-library/src/foundations/array.rs` | 运行时 `Array` 的定义（`EcoVec<Value>`）。 |
| `crates/typst-library/src/foundations/dict.rs` | 运行时 `Dict` 的定义（`IndexMap<Str, Value>`）。 |
| `tests/suite/foundations/array.typ` / `dict.typ` | 覆盖展开行为的集成测试，是本讲「预期结果」的权威依据。 |

## 4. 核心概念与源码讲解

### 4.1 数组求值：Array::eval 与 ArrayItem（Pos / Spread）

#### 4.1.1 概念说明

`ast::Array` 节点代表源码里的数组字面量，比如 `(1, 2, 3)` 或 `(..l, 4, ..r)`。它的每一项是 `ast::ArrayItem`，只有两种变体：

- `Pos(Expr)`：一个「普通」表达式，直接作为数组的一个元素；
- `Spread(Spread)`：一个 `..expr` 展开项，要把 `expr` 求值结果「摊平」后逐个塞进数组。

`Array::eval` 的工作就是遍历这些项，逐个求值，把它们累积进一个 `EcoVec<Value>`，最后包成运行时 `Array`。难点不在位置项，而在 spread：被展开的值在运行时可能是 `None`（跳过）、`Array`（逐个并入）、`Dict`（语义错误——把字典塞进数组没意义），需要分别处理。

#### 4.1.2 核心流程

`Array::eval` 的执行过程可以用下面的伪代码概括：

```
输入：self（ast::Array），vm
输出：Array

1. items = self.items()                // 取得 ArrayItem 迭代器
2. vec = 空 EcoVec（按 size_hint 预分配容量）
3. all_dict_spreads = true             // 「至今为止是否全都是字典展开」标记
4. 对 items 中的每一项 item：
     - 若 item 是 Pos(expr)：
         all_dict_spreads = false      // 出现位置项 → 不可能全是字典展开
         vec.push(expr 求值)
     - 若 item 是 Spread(spread)：
         v = spread.expr() 求值
         - v 是 None      → 什么都不做（且不重置标记）
         - v 是 Array(a)  → all_dict_spreads = false；vec 并入 a 的所有元素
         - v 是 Dict 且 all_dict_spreads 仍为真
                            且「剩余项全是字典展开」→ 报「加冒号变字典」的错
         - 否则            → 报「cannot spread {类型} into array」
5. 返回 vec 包装成的 Array
```

最值得关注的是第 4 步里针对 `Dict` 的两条分支：一条是「带前瞻的友好提示」，另一条是「通用报错」。它们共享同一个 `all_dict_spreads` 标记。

#### 4.1.3 源码精读

先看入口适配。在 `ast::Expr::eval` 这个总分发器里，`Array` 与 `Dict` 两个分支各自调用对应的 `eval`，再用 `.map(Value::Array)` / `.map(Value::Dict)` 把「具体的集合类型」适配成统一的 `Value`——这正是 [u2-l1](u2-l1-literals-idents.md) 讲过的「产生集合的节点经 `.map(...)` 适配成 `Value`」模式：

> [crates/typst-eval/src/code.rs:126-L127](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L126-L127) —— 把 `Array` / `Dict` 节点的求值结果适配成 `Value::Array` / `Value::Dict`。

接着是 `Array::eval` 主体。先看「位置项」与「展开项」的主干（省略了最复杂的 Dict 分支细节）：

> [crates/typst-eval/src/code.rs:231-L257](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L231-L257) —— `Array::eval`：按 `size_hint` 预分配容量，初始化 `all_dict_spreads` 标记，遍历 `Pos`（置标记为 false 并 push）与 `Spread`（按值类型分派）。

关键代码片段：

```rust
let mut vec = EcoVec::with_capacity(items.size_hint().0);

// 若数组里出现「展开字典」，通常要报错；但如果「所有项都是字典展开」，
// 用户多半是想写 (: ..dict_a, ..dict_b) 来造一个字典，而不是数组。
let mut all_dict_spreads = true;

while let Some(item) = items.next() {
    match item {
        ast::ArrayItem::Pos(expr) => {
            all_dict_spreads = false;
            vec.push(expr.eval(vm)?);
        }
        ast::ArrayItem::Spread(spread) => match spread.expr().eval(vm)? {
            Value::None => {}
            Value::Array(array) => {
                all_dict_spreads = false;
                vec.extend(array);
            }
            // ... Dict 分支见 4.1.4 的实践剖析 ...
            v => bail!(spread.span(), "cannot spread {} into array", v.ty()),
        },
    }
}
```

注意三个细节：

1. **预分配容量**：`EcoVec::with_capacity(items.size_hint().0)` 用迭代器的 `size_hint` 下界预分配，减少扩容拷贝。这只是优化，不影响正确性。
2. **`Value::None` 是「透明」的**：`Value::None => {}` 既不 push 任何东西，**也不重置** `all_dict_spreads`。这意味着 `(..none, ..dict)` 仍然会被当作「全是字典展开」看待（见 4.1.4）。
3. **位置项与数组展开都会重置标记**：只要出现一个 `Pos` 或一个 `Array` 展开，`all_dict_spreads` 就被置为 `false`，后续即使遇到字典展开也只会走通用报错分支。

语法层侧，`ArrayItem` 与 `Spread` 的定义如下，确认了 `Spread::expr()` 返回被展开的表达式：

> [crates/typst-syntax/src/ast.rs:1615-L1620](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/ast.rs#L1615-L1620) —— `ArrayItem` 只有 `Pos(Expr)` 与 `Spread(Spread)` 两种变体。
>
> [crates/typst-syntax/src/ast.rs:1746-L1748](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/ast.rs#L1746-L1748) —— `Spread::expr()` 取出被展开的表达式（如 `..x.at(0)` 中的 `x.at(0)`）。

#### 4.1.4 代码实践

本实践对应任务规格中要求剖析的 `all_dict_spreads` 前瞻逻辑。

**实践目标**：理解「全是字典展开」时为什么 typst-eval 会给出「加个冒号变成字典」的修复提示，以及为什么 `((1,2))` 被当作普通数组。

**操作步骤**：

1. 打开 [crates/typst-eval/src/code.rs:257-L275](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L257-L275)，阅读 `v @ Value::Dict(_) if all_dict_spreads && items.all(...) =>` 这条带守卫的分支。关键源码：

   ```rust
   v @ Value::Dict(_)
       if all_dict_spreads
       // 向前看：剩余项是否也都是字典展开
       && items.all(|item| matches!(
           item,
           ast::ArrayItem::Spread(spread) if matches!(
               spread.expr().eval(vm),
               Ok(Value::Dict(_)),
           ),
       )) =>
   {
       let fixed = self.to_untyped().full_text().replacen("(", "(: ", 1);
       bail!(
           spread.span(), "cannot spread {} into array", v.ty();
           hint: "add a colon to create a dictionary instead: `{fixed}`";
       )
   }
   ```

2. 对照仓库根的权威测试用例 [tests/suite/foundations/dict.typ:67-L101](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/tests/suite/foundations/dict.typ#L67-L101)，它精确给出了各种场景下的错误信息和提示。

**需要观察的现象与预期结果**：

- **`(..x, ..y)`，二者都是字典**：命中带守卫分支。`all_dict_spreads` 自始至终为 `true`，且 `items.all(...)` 向前看确认剩余项也都是字典展开 → 报错并提示 `add a colon to create a dictionary instead: \`(: ..x,..y)\``。`fixed` 的构造方式是把整个字面量的原文 `(..x,..y)` 里的第一个 `(` 替换成 `(: `，恰好得到 `(: ..x,..y)`。
- **`(..x)`，单个字典展开**：同样命中带守卫分支。因为「剩余项」为空，而 `Iterator::all` 对空迭代器返回 `true`，所以仍判定为「全是字典展开」→ 提示 `(: ..x)`。
- **`(..x, ..y)`，其中 `x` 是字典、`y` 是数组**：`items.all(...)` 在遇到 `..y` 时求值为 `Array`，`matches!` 不匹配 → 返回 `false` → 守卫失败，落到通用分支 `v => bail!("cannot spread dictionary into array")`。**没有**「加冒号」的提示——因为此时「用户的意图不明确」（也许真想造数组），贸然建议改成字典反而误导。这正是带守卫分支存在的意义。
- **`(..x, "item")`，字典展开后跟一个位置项**：与上一条同理，`items.all` 遇到 `Pos` 项返回 `false`，走通用报错，不给提示。
- **`((1,2))` 为什么是数组**：`(1, 2)` 是数组字面量语法（两个位置项、无冒号），外层括号只是 `Parenthesized` 包裹，求值时 `Parenthesized::eval` 直接委托内层表达式。整条链路里没有任何字典展开，`all_dict_spreads` 在第一个 `Pos` 处就被置为 `false`，自然不会触发任何「变字典」的提示——它本就只能是数组。

> 关于 `items.all(...)` 的一个细节：它在守卫里会对**剩余项**的 spread 表达式再次调用 `spread.expr().eval(vm)` 来判断类型。也就是说，剩余字典展开的求值发生在「前瞻」阶段，而非主循环里（主循环已在 `match spread.expr().eval(vm)?` 处求值过当前项）。这是为了在不破坏主循环结构的前提下「偷看」未来。其副作用（如 sink 警告）在正常用例下可忽略；若你关心极端情况，可标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：若写成 `(#let none-val = none; (..none-val, ..(a: 1)))`，会触发「加冒号变字典」的提示吗？为什么？

> **答案**：会。`..none-val` 求值为 `Value::None`，命中 `Value::None => {}`，**不重置** `all_dict_spreads`；接着 `..(a:1)` 是字典展开，此时 `all_dict_spreads` 仍为 `true`，向前看剩余项为空（`all` 返回 `true`），于是命中带守卫分支，给出提示。这正是测试 `spread-none-and-dict`（`(..none,..(one:1))`）所覆盖的情形。

**练习 2**：把 `bail!(spread.span(), ...)` 里的 `spread.span()` 换成 `self.span()`（整个数组的 span），会对用户体验造成什么影响？

> **答案**：错误高亮会从「出问题的那个 `..x`」变成「整个数组」，定位变模糊。用 `spread.span()` 能精确指向真正非法的那一项，这是 typst-eval 诊断「精确定位 sub-range」哲学的体现。

---

### 4.2 字典求值：Dict::eval 与 DictItem（Named / Keyed / Spread）

#### 4.2.1 概念说明

`ast::Dict` 节点代表字典字面量，比如 `(name: "Typst", year: 2023)` 或 `("spacy key": true)`。它的每一项是 `ast::DictItem`，有三种变体：

- `Named(Named)`：命名对，形如 `name: value`，键是一个**标识符**（`name`）；
- `Keyed(Keyed)`：键值对，形如 `"key": value` 或 `(expr): value`，键是一个**任意表达式**，求值后必须能得到字符串；
- `Spread(Spread)`：`..expr`，把 `expr`（必须是字典）的键值对并入当前字典。

`Dict::eval` 要把这三类项统一累积进一个 `IndexMap<Str, Value>`，并处理两件事：(1) `Keyed` 的键必须是 `Str`，否则要收集错误；(2) `Spread` 只接受 `None`（跳过）或 `Dict`，其他类型直接报错。

#### 4.2.2 核心流程

```
输入：self（ast::Dict），vm
输出：Dict

1. map = 空 IndexMap<Str, Value>
2. invalid_keys = 空错误列表
3. 对 self.items() 中的每一项 item：
     - Named(named)：键 = named.name() 的文本；值 = named.expr() 求值；插入 map
     - Keyed(keyed)：
         raw_key = keyed.key()
         key = raw_key 求值
         把 key cast 成 Str：
           - 成功 → 用作键
           - 失败 → 把错误收集进 invalid_keys，键临时用 Str::default() 占位
         插入 map
     - Spread(spread)：
         v = spread.expr() 求值
         - None  → 跳过
         - Dict  → map 并入 v 的所有键值对
         - 其他  → 报「cannot spread {类型} into dictionary」
4. 若 invalid_keys 非空 → 返回 Err（一次性报告全部非法键）
5. 返回 map 包装成的 Dict
```

这里有两个设计要点：键的「收集后统一报错」与「`Str::default()` 占位以继续处理」。

#### 4.2.3 源码精读

`Dict::eval` 主体：

> [crates/typst-eval/src/code.rs:284-L319](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L284-L319) —— `Dict::eval`：三类项分派、键类型校验与错误收集。

关键片段：

```rust
let mut map = indexmap::IndexMap::default();
let mut invalid_keys = eco_vec![];

for item in self.items() {
    match item {
        ast::DictItem::Named(named) => {
            map.insert(named.name().get().clone().into(), named.expr().eval(vm)?);
        }
        ast::DictItem::Keyed(keyed) => {
            let raw_key = keyed.key();
            let key = raw_key.eval(vm)?;
            let key =
                key.cast::<Str>().at(raw_key.span()).unwrap_or_else(|errors| {
                    invalid_keys.extend(errors);
                    Str::default()
                });
            map.insert(key, keyed.expr().eval(vm)?);
        }
        ast::DictItem::Spread(spread) => match spread.expr().eval(vm)? {
            Value::None => {}
            Value::Dict(dict) => map.extend(dict),
            v => bail!(spread.span(), "cannot spread {} into dictionary", v.ty()),
        },
    }
}

if !invalid_keys.is_empty() {
    return Err(invalid_keys);
}
Ok(map.into())
```

逐点说明：

- **`Named` 分支**：键来自标识符。`named.name()` 返回 `Ident`，其 `get()` 返回 `&EcoString`（见 [ast.rs:1693-L1705](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/ast.rs#L1693-L1705) 中 `Named` 的定义），`.clone().into()` 把它转成字典键类型 `Str`。因为键来自合法标识符，这里不需要任何校验。
- **`Keyed` 分支**：键是任意表达式，必须 `cast::<Str>()`。`Value::cast::<T>()` 返回 `HintedStrResult<T>`（见 [value.rs:152-L154](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/value.rs#L152-L154)），`.at(raw_key.span())` 借 `At` trait 把它转成带 span 的 `SourceResult<Str>`，`.unwrap_or_else(...)` 在失败时把诊断收集进 `invalid_keys` 并用 `Str::default()`（空串）占位，**让循环继续**，从而收集后续可能存在的更多非法键。
- **`Spread` 分支**：与数组里的 spread 对称，但这里字典展开只接受 `None`（跳过）和 `Dict`（`map.extend`），其它一律 `bail!`。
- **结尾统一报错**：`if !invalid_keys.is_empty() { return Err(invalid_keys); }` 一次性把所有非法键的诊断都抛出。

运行时类型佐证：`Dict` 底层是 `Arc<IndexMap<Str, Value, FxBuildHasher>>`（键为 `Str`），这正是 `cast::<Str>()` 必须成立的原因：

> [crates/typst-library/src/foundations/dict.rs:80-L81](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/dict.rs#L80-L81) —— `Dict` 的键类型是 `Str`，所以 `Keyed` 的键必须能 cast 成 `Str`。

关于键的唯一性：`IndexMap::insert` 在键已存在时会**覆盖旧值但保留最初的插入位置**；`Spread` 的 `map.extend` 同样会覆盖。因此 `(a: 1, a: 2)` 得到 `a: 2`，`(..x, a: 1)` 里后写的 `a: 1` 会覆盖 `x` 带来的 `a`。

#### 4.2.4 代码实践

**实践目标**：亲手触发字典非法键错误，观察「收集后统一报错」的行为。

**操作步骤**：

1. 准备一个测试文件 `dict-keys.typ`，内容故意包含两个非法键（键表达式求值后不是字符串）：

   ```typst
   // 1 是整数，不能当键；1.5 是浮点数，也不能当键
   #((1: "a", 1.5: "b"))
   ```

2. 用 typst CLI 编译（若已安装）：`typst compile dict-keys.typ`。

**需要观察的现象**：

- 报错信息应当同时包含「键 1 不是字符串」和「键 1.5 不是字符串」两条诊断，而不是只报第一个就停下。

**预期结果**：

- 依据 `Dict::eval` 的逻辑，两个非法键都会被 `unwrap_or_else` 收集进 `invalid_keys`，最后在 `if !invalid_keys.is_empty() { return Err(invalid_keys); }` 处一次性抛出。因此你会**同时看到两条错误**，这正是「占位 + 收集」设计带来的体验提升——用户一次改完所有键，不必改一个、编译一次、再发现下一个。
- 若你本地无法运行 typst，可改为阅读 [tests/suite/foundations/dict.typ](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/tests/suite/foundations/dict.typ) 中与键相关的 `// Error:` 用例，对照断言理解行为；此时请标注「待本地验证」运行时输出。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Named` 分支不需要像 `Keyed` 那样做 `cast::<Str>()`？

> **答案**：`Named` 的键来自标识符 `named.name()`，标识符本身就是合法的字符串（由词法分析保证），`.clone().into()` 直接转成 `Str`，不可能失败。`Keyed` 的键则是任意表达式，求值后可能是整数、数组等任意 `Value`，必须校验能否转成 `Str`。

**练习 2**：`(a: 1, ..(a: 2, b: 3))` 的最终结果里 `a` 的值是多少？为什么？

> **答案**：`a` 为 `2`。处理顺序是先由 `Named` 插入 `a: 1`，再由 `Spread` 用 `map.extend` 并入 `(a: 2, b: 3)`。`IndexMap` 对已存在的键 `a`，`extend` 会用**新值覆盖旧值**（但保留键的首次插入位置），所以 `a` 最终被覆盖成 `2`，`b` 为 `3`。这条练习提醒读者：**后写的 spread 会覆盖先前同名键**。

---

### 4.3 spread 展开运算符的统一语义（Value::None / Array / Dict）

#### 4.3.1 概念说明

`..` 是同一个语法符号，但在「数组上下文」和「字典上下文」里，它接受的值类型、对每种类型的处理方式都不同。把两处实现并排放在一起看，能提炼出 spread 的「统一语义模型」，也能看清 typst-eval 如何用相同的模式处理两类集合。

核心规则可以概括为：**spread 把被展开的值「摊平」进当前正在构造的集合；摊不动（类型不匹配）就报错；摊了等于没摊（None）就跳过；摊得动就逐元素并入。**

#### 4.3.2 核心流程

用一张表把 spread 在两种上下文下的行为对照清楚：

| spread 表达式求值结果 | 在 `Array::eval` 中 | 在 `Dict::eval` 中 |
| --- | --- | --- |
| `Value::None` | 跳过（`{}`），**不重置** `all_dict_spreads` | 跳过（`{}`） |
| `Value::Array(a)` | `vec.extend(a)`，并入每个元素 | `bail!("cannot spread array into dictionary")` |
| `Value::Dict(d)` | 多数情况 `bail!("cannot spread dictionary into array")`；「全是字典展开」时给「加冒号」提示 | `map.extend(d)`，并入每个键值对 |
| 其它类型（`Int`/`Str`/`Func`…） | `bail!("cannot spread {ty} into array")` | `bail!("cannot spread {ty} into dictionary")` |

观察这张表能得到三条规律：

1. **`None` 永远是「无害跳过」**——两个上下文都把它当空集合处理。
2. **类型必须与上下文匹配**——数组里只能展开数组（字典不行），字典里只能展开字典（数组不行）。不匹配即报错，且错误信息里会带上实际类型 `{ty}`，方便定位。
3. **错误信息模板高度一致**：`"cannot spread {ty} into {array|dictionary}"`，只是上下文名不同。这是 typst-eval 在两类集合间复用诊断模式的体现。

#### 4.3.3 源码精读

两处 spread 分支并排对照（已分别精读，这里聚焦对比）：

> 数组侧 spread：[crates/typst-eval/src/code.rs:251-L276](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L251-L276) —— `None` 跳过 / `Array` 并入 / `Dict` 特殊诊断或报错 / 其它报错。
>
> 字典侧 spread：[crates/typst-eval/src/code.rs:306-L310](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L306-L310) —— `None` 跳过 / `Dict` 并入 / 其它报错。

```rust
// 数组侧
ast::ArrayItem::Spread(spread) => match spread.expr().eval(vm)? {
    Value::None => {}
    Value::Array(array) => { all_dict_spreads = false; vec.extend(array); }
    v @ Value::Dict(_) if /* all_dict_spreads 前瞻 */ => { /* 加冒号提示 */ }
    v => bail!(spread.span(), "cannot spread {} into array", v.ty()),
},
```

```rust
// 字典侧
ast::DictItem::Spread(spread) => match spread.expr().eval(vm)? {
    Value::None => {}
    Value::Dict(dict) => map.extend(dict),
    v => bail!(spread.span(), "cannot spread {} into dictionary", v.ty()),
},
```

可以看到，两边都是「先求值 `spread.expr()`，再 `match` 值类型」的同构骨架，差别只在于：哪种集合类型是「合法可并入」的、以及数组侧多了一条「全是字典展开」的友好诊断。

运行时类型层面，`vec.extend(array)` 与 `map.extend(dict)` 之所以都成立，是因为 `Array` 底层就是 `EcoVec<Value>`（[array.rs:75-L75](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/array.rs#L75-L75)），`Dict` 底层就是 `IndexMap<Str, Value>`（[dict.rs:80-L81](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/dict.rs#L80-L81)），各自的 `extend` 直接把同构元素批量灌进去。

#### 4.3.4 代码实践

**实践目标**：用一个文件同时验证表中四种 spread 行为，加深对「统一语义」的直觉。

**操作步骤**：

1. 创建 `spread.typ`，内容如下（合法与非法用例各几条）：

   ```typst
   #{
     // 合法：数组展开数组、字典展开字典
     test((..(1,2), ..(3,4)), (1,2,3,4))
     test((:..(a:1), ..(b:2)), (a:1, b:2))
     // None 在两处都无害
     test((..none), ())
     test((:..none), (:))
   }
   // 非法：数组里展开字典 —— 预期报错
   // #((1, ..(a: 1)))
   // 非法：字典里展开数组 —— 预期报错
   // #((:..(1, 2), x: 1))
   ```

2. 先编译合法部分，确认 `test` 全部通过；再依次取消注释两条非法用例，分别编译。

**需要观察的现象与预期结果**：

- 合法部分：四个 `test` 全部通过。`(..none)` 得到空数组 `()`，`(:..none)` 得到空字典 `(:)`。
- 取消注释 `#((1, ..(a: 1)))`：报 `cannot spread dictionary into array`。注意此处 `all_dict_spreads` 因前面的位置项 `1` 已被置为 `false`，所以**不会**出现「加冒号」提示（意图不明确）。这与测试 [tests/suite/foundations/array.typ:64-L66](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/tests/suite/foundations/array.typ#L64-L66)（`spread-dict-into-array`，`(1, 2, ..(a: 1))`）一致。
- 取消注释 `#((:..(1, 2), x: 1))`：报 `cannot spread array into dictionary`，对应测试 [tests/suite/foundations/dict.typ:111-L113](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/tests/suite/foundations/dict.typ#L111-L113)（`spread-array-into-dict`，`(..(1, 2), a: 1)`）。
- 若本地无 typst CLI，以上预期可全部由所引测试文件佐证；运行时输出请标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `Value::None` 在数组侧不重置 `all_dict_spreads`，而这个细节在字典侧根本不存在？

> **答案**：数组侧有个「全是字典展开才提示改字典」的诊断，`all_dict_spreads` 是为它服务的；`None` 展开对结果毫无贡献，逻辑上等价于「不存在」，所以不应破坏「至今是否全是字典展开」的判定，故不重置。字典侧没有这种「跨项前瞻」的诊断，每项独立处理，自然没有这个标记。

**练习 2**：如果把数组侧的 `Value::Array(array) => { vec.extend(array); }` 改成逐个 `for v in array { vec.push(v) }`，行为会变化吗？

> **答案**：不会。`EcoVec::extend` 本质上就是逐个追加，二者等价；`extend` 只是更地道、可能略快的写法。

## 5. 综合实践

把本讲的三条主线（位置项、spread、键校验）串起来，完成下面这个「配置合并器」阅读 + 改造任务。

**任务背景**：假设有一段 Typst 代码，用 spread 合并多份配置字典，并期望得到最终生效的键值。代码如下：

```typst
#let base = (font: "Arial", size: 11pt)
#let theme = (color: "navy", size: 12pt)   // 覆盖 base.size
#let extra = (lang: "zh")

// 目标：合并三者，后写的覆盖先写的
#let cfg = (:..base, ..theme, ..extra)
#test(cfg, (font: "Arial", size: 12pt, color: "navy", lang: "zh"))
```

**要求**：

1. **预测**：先不看源码，依据 4.2 讲的「`IndexMap::extend` 后写覆盖先写、保留首次插入位置」规则，预测 `cfg.size` 的值，以及 `cfg` 各键的出现顺序。
2. **求证**：阅读 [crates/typst-eval/src/code.rs:306-L310](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/code.rs#L306-L310) 的 `Spread` 分支，确认 `(:..base, ..theme, ..extra)` 会依次执行三次 `map.extend`，从而验证你的预测。
3. **构造错误**：把 `extra` 改成数组 `#let extra = (1, 2)`，重新求值 `(:..base, ..theme, ..extra)`。依据 4.3 的对照表预测报错信息，再与测试 [tests/suite/foundations/dict.typ:111-L113](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/tests/suite/foundations/dict.typ#L111-L113) 比对。
4. **（进阶）反思设计**：如果要求「键冲突时报错而非静默覆盖」，你会如何修改 `Dict::eval`？用一两句话描述思路（不必实现）。

**预期结果**：

1. `cfg.size` 应为 `12pt`（`theme` 后于 `base` 写入，覆盖之）；键的出现顺序按「首次插入」排列：`font, size, color, lang`（`size` 虽被覆盖，但位置仍是 `base` 首次插入时的第二位）。
3. 改成数组后应报 `cannot spread array into dictionary`。
4. 思路示例：在 `Named`/`Keyed`/`Spread` 三处插入前，先 `map.contains_key(&key)` 检查是否已存在；存在则收集一条「duplicate key」诊断到某个 `duplicate_keys` 列表，结尾与 `invalid_keys` 一样统一抛出。

> 运行结果请以本地 typst 编译为准；若无法运行，第 1、3 问可由本讲引用的源码与测试直接推断，第 4 问为开放设计题。

## 6. 本讲小结

- `ast::Array` / `ast::Dict` 在 `Expr::eval` 总分发器里分别经 `.map(Value::Array)` / `.map(Value::Dict)` 适配成统一的 `Value`，自身 `Eval` 的 `Output` 是 `Array` / `Dict`。
- `Array::eval` 遍历 `ArrayItem::Pos`（直接 push）与 `ArrayItem::Spread`（按 `None`/`Array`/`Dict` 分派），并用 `size_hint` 预分配容量。
- `all_dict_spreads` 是一种「前瞻式」诊断：只有当数组里**全是字典展开**（`None` 不算破坏）时，才提示用户「加个冒号变成字典」；若混入了位置项或数组展开，意图不明确，只给通用报错。
- `Dict::eval` 处理 `Named`（标识符键）、`Keyed`（表达式键，须 cast 成 `Str`）、`Spread`（并入字典）三类项；非法键用 `Str::default()` 占位、收集后一次性统一报错。
- spread 的统一语义：`None` 跳过、同类型集合并入、异类型报错，数组与字典两侧共享几乎相同的 `match` 骨架，仅「合法类型」与「额外诊断」不同。
- 键的唯一性由 `IndexMap` 决定：后写覆盖值、保留首次插入位置——这是预测合并结果的关键。

## 7. 下一步学习建议

本讲讲完了两类「集合字面量」的求值，接下来可以沿着两条线索继续：

1. **代码块与作用域**：进入 [u2-l3 代码块、内容块与作用域进出](u2-l3-blocks-scopes.md)，看 `CodeBlock` / `ContentBlock` 如何用 `scopes.enter`/`exit` 划分词法作用域，以及 `eval_code` 如何用 `ops::join` 把多条表达式的结果拼接起来——本讲里数组/字典都是「单条表达式」，而代码块是「多条表达式流」的求值。
2. **键的「访问」视角**：本讲只讲了字典的**构造**；字典的**读取与可变访问**（`dict.key`、`dict.at("key")`、`dict.insert(...)`）涉及 `FieldAccess`、`Access` trait 与内置方法，将在 u4（字段访问与方法分派）、u5（可变访问 Access 与内置方法）深入。
3. **参数求值中的 spread**：函数调用 `f(a, b: 1, ..rest)` 的 `Args::eval` 也用到了 spread 展开模式（展平 `None`/`Array`/`Dict`/`Args`），与本讲高度同构，建议学完 [u4-l1 函数调用与参数求值](u4-l1-func-call-args.md) 后回头对比两处 spread 的异同。
