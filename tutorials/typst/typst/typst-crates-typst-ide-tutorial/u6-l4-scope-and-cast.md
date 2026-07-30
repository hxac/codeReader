# scope_completions 与类型驱动补全

## 1. 本讲目标

本讲是「补全进阶」的核心一篇。读完本讲，你应当能够：

- 说清 `scope_completions` 如何把「局部作用域」和「全局/数学作用域」两套命名合并，并保证**局部优先于全局**。
- 解释 `check_value_recursively` 为什么能让「内部含颜色的字典/模块」也出现在颜色补全里。
- 读懂 `CastInfo` 这个描述「参数能接受什么」的元信息模型，以及 `cast_completions` 如何按它递归地生成补全候选。
- 把上述三块串起来，独立追踪 `#rect(fill: |)` 从光标一路走到候选列表的完整调用链。

本讲只新增 `scope_completions`、`cast_completions`、`check_value_recursively`、`CastInfo` 四个「新面孔」；`named_items` 与 `globals` 在前置讲义（u2-l3、u2-l5）已讲透，本讲作为「现成积木」复用并在第 2 节做最小回顾。

## 2. 前置知识

本讲假设你已读完 u2-l3（`named_items`）、u2-l5（`utils`）、u6-l1（规则与参数补全）。下面用三段话把要用到的旧知识压到一页，方便随时回看。

### 2.1 named_items —— 收集光标处可见的局部命名

[`named_items`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/matchers.rs#L9-L13) 以「沿祖先向上 + 沿前置兄弟向左」的双层遍历，近似 Typst 的词法作用域，把局部可见的命名项通过回调吐出来。每个项是一个 [`NamedItem`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/matchers.rs#L179-L188)，共四变体：

| 变体 | 来源 | `value()` 是否有值 |
|---|---|---|
| `Var` | `let` 普通变量、`for` 循环变量、闭包参数 | `None`（需 `analyze_expr` 才能推断运行时值） |
| `Fn` | `let f() = ..` 闭包绑定 | `None` |
| `Module` | `import "x"` / `import "x" as y` 整模块 | `Some(模块值)` |
| `Import` | `import "x": a, b` / `: *` 挑出的项 | `Some(该项的值)` |

关键性质：回调返回 `Some` 即整体短路（用于「找第一个」），返回 `None` 则继续（用于「全收集」）。本讲里 `scope_completions` 用的是后者。

### 2.2 globals —— 取当前模式的标准库作用域

[`globals`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/utils.rs#L175-L182) 只做一件事：看光标叶子节点的 `mode_after()`，是 `Math` 就返回 `library.math.scope()`，否则返回 `library.global.scope()`。它就是「标准库里所有名字」的入口。

### 2.3 Value 与 Type —— 补全里的两个基本对象

Typst 里每个运行时值都是 `Value`，每个值都有一个 `Type`（可用 `value.ty()` 取得）。`Scope`（作用域）是一个「名字 → 绑定」的有序表，绑定里藏着 `Value`。本讲反复出现两个动作：对值用 `value.ty()` 判类型、对作用域用 `scope.iter()` 枚举 `(name, binding)`。

## 3. 本讲源码地图

| 文件 | 本讲关注的内容 |
|---|---|
| `src/complete.rs` | `scope_completions`、`cast_completions`，以及串联用的 `param_value_completions` |
| `src/utils.rs` | `check_value_recursively`、`globals` |
| `src/matchers.rs` | `named_items`、`NamedItem`（复用） |
| `crates/typst-library/.../cast.rs` | `CastInfo` 枚举定义（依赖类型，非 typst-ide 内部） |

## 4. 核心概念与源码讲解

### 4.1 scope_completions —— 局部优先的作用域合并

#### 4.1.1 概念说明

当光标处在「需要填一个值」的位置（例如 `#rect(fill: |)`、`{ | }`、数学模式 `$|$`），补全引擎要回答一个问题：**这里可以填哪些名字？** 答案来自两个来源：

1. **局部作用域**：用户自己用 `let`、`import`、`for`、闭包参数建立的名字（由 `named_items` 收集）。
2. **全局作用域**：标准库里现成的名字（由 `globals` 给出，按 Markup/Math/Code 模式切换）。

`scope_completions` 把这两者合并，并定下一条铁律：**如果某个名字在局部已经定义，就不再用同名全局顶替**——也就是「局部优先于全局」。这符合人对作用域遮蔽（shadowing）的直觉：你 `#let red = ...` 之后，`red` 应该指向你自己的定义，而不是标准库的红色。

它还接收一个 `filter` 谓词和一个 `parens` 开关：`filter` 用来按类型筛选（比如「只要颜色」），`parens` 决定函数候选是否自动带括号。

#### 4.1.2 核心流程

```
scope_completions(parens, filter):
  1. 把 filter 包装成「递归版」：
       filter'(v) = check_value_recursively(v, filter)
     —— 这一步让「内含目标类型值的容器」也通过过滤（见 4.2）。
  2. defined = 空的 BTreeMap<名字, Option<Value>>
  3. 用 named_items 全收集局部命名（回调恒返回 None）：
       对每个 item：
         名字非空 且 item.value() 通过 filter' → defined.insert(名字, item.value())
  4. 遍历 defined，逐个产出补全：
       value 有值   → value_completion(名字, 值)        # 带 repr/文档的「富」补全
       value 为 None → 纯 Constant 补全（只写名字）       # Var/Fn：值未知
  5. 遍历 globals 里的 (name, binding)：
       filter'(值) 通过 且 defined 里没有同名项 → value_completion_full(..., parens, ...)
                                                          #↑ 局部优先的守卫
```

两个细节值得记住：

- `defined` 选用 `BTreeMap`（按名字排序），既保证补全列表**按字母序稳定输出**，又对同名项**按名字去重**（后插入者覆盖先插入者）。
- 第 5 步的 `!defined.contains_key(name)` 是「局部优先」的**唯一**守卫——全局项只要和任何局部项撞名就被压制。

#### 4.1.3 源码精读

[`scope_completions`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1426-L1462) 的全部代码如下（行号对应 `src/complete.rs`）：

```rust
fn scope_completions(&mut self, parens: bool, filter: impl Fn(&Value) -> bool) {
    // 当值的某个组成部分命中过滤时，也算命中。
    // 例如 #rect(fill: |) 不仅提示颜色，也提示"装着颜色的字典/模块"。
    let filter = |value: &Value| check_value_recursively(value, &filter);

    let mut defined = BTreeMap::<EcoString, Option<Value>>::new();
    named_items(self.world, self.leaf.clone(), |item| {
        let name = item.name();
        if !name.is_empty() && item.value().as_ref().is_none_or(filter) {
            defined.insert(name.clone(), item.value());
        }
        None::<()>
    });

    for (name, value) in &defined {
        if let Some(value) = value {
            self.value_completion(name.clone(), value);
        } else {
            self.completions.push(Completion {
                kind: CompletionKind::Constant,
                label: name.clone(),
                apply: None,
                detail: None,
            });
        }
    }

    for (name, binding) in globals(self.world, self.leaf).iter() {
        let value = binding.read();
        if filter(value) && !defined.contains_key(name) {
            self.value_completion_full(Some(name.clone()), value, parens, None, None);
        }
    }
}
```

逐行对照几个要点：

- [`let filter = |value: &Value| check_value_recursively(value, &filter);`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1431-L1431) 把原始谓词升级为「递归谓词」，是本讲后半段的主角，细节在 4.2。
- [`named_items(self.world, self.leaf.clone(), |item| { .. None::<()> })`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1434-L1441)：回调**永远返回 `None`**，这正是「全收集」用法——让遍历跑完整个作用域而不是中途停下。
- [`item.value().as_ref().is_none_or(filter)`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1436-L1436)：`Var`/`Fn` 的 `value()` 是 `None`，`is_none_or` 对 `None` 返回 `true`，所以**未知值的局部变量一定入表**（值未知不等于不存在）；而 `Module`/`Import` 携带真实值时，要拿真实值去过 `filter`。
- [`if let Some(value) = value`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1444-L1454) 这条分支解释了一个肉眼可见的现象：你写的 `#let x = 1` 在补全菜单里通常只显示一个名字 `x`（`Constant`、无 `detail`），而一个 `import` 进来的颜色却能显示它的 `repr`——因为前者 `value` 为 `None`，走「纯名字」分支。
- [`if filter(value) && !defined.contains_key(name)`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1458-L1458)：`!defined.contains_key(name)` 就是「局部优先于全局」的那一行守卫。

#### 4.1.4 代码实践

**目标**：亲眼看一次「局部优先于全局」与「模式决定 globals 取哪张表」。

**步骤**：

1. 打开 [`test_autocomplete_math_scope`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1670-L1676)，它演示了 `globals` 在不同模式间的切换。
2. 想象一段最小代码 `#let center = "hi"\n#align(|)`（光标在 `(` 后）。在心里走一遍 `scope_completions(true, |_| true)`：
   - `named_items` 会收集到局部 `center`（`Var`，值未知 → `defined["center"] = None`）。
   - `globals` 里也有一个标准库的 `center`（alignment），但 `defined.contains_key("center")` 为真，**被压制**。
3. 运行该测试以确认理解：

```bash
cargo test -p typst-ide --lib complete::tests::test_autocomplete_math_scope
```

**观察**：`$col$` 在数学模式补出 `colon` 而非 `colbreak`（说明 `globals` 切到了 `library.math`）；`$#col$` 则相反（切到 `library.global`）。

**预期结果**：测试通过；你能用自己的话说出「模式决定 globals 取哪张表」与「同名时局部压全局」两件事。

> 待本地验证：步骤 2 的 `#let center` 场景没有现成测试，你可以仿照 `test_autocomplete_math_scope` 写一个，断言补全里 `center` 的 `detail` 为 `None`（纯名字分支），以此验证它来自局部而非全局。

#### 4.1.5 小练习与答案

**练习 1**：若把第 5 步的 `!defined.contains_key(name)` 守卫删掉，用户 `#let red = ...` 后在 `#rect(fill: |)` 处会看到什么异常？
**答案**：标准库的 `red` 会被再次加入，导致补全列表里出现**两个 `red`**（一个来自局部、一个来自全局），用户的选择可能与语义不符。

**练习 2**：为什么局部 `Var`/`Fn` 即便 `value()` 为 `None` 也一定进 `defined`？
**答案**：`is_none_or(filter)` 对 `None` 恒为 `true`。「值暂时推断不出来」不代表这个名字不存在，它仍应被补全。

---

### 4.2 check_value_recursively —— 让容器里的目标值也通过

#### 4.2.1 概念说明

`scope_completions` 的 `filter` 通常长这样：「只要颜色」`|v| v.ty() == Type::of::<Color>()`。可真实代码里很少有人把颜色直接写成裸 `red`，更常见的是把一堆颜色装在一个字典或模块里：

```typst
#import "design.typ": clrs   // clrs = (a: red, b: blue)
#rect(fill: clrs)            // 完全合法！fill 接受的就是颜色，clrs 里有颜色
```

如果过滤只看「顶层值是不是颜色」，那 `clrs`（一个字典）就会被排除，补全体验会很差。`check_value_recursively` 解决的就是这个问题：**只要值的某个组成部分（递归地）满足谓词，整个值就算通过**。

#### 4.2.2 核心流程

它是一个带步数上限的深度优先搜索：

```
check_value_recursively(value, predicate):
  Searcher { steps:0, max_steps:1000, predicate }.find(value)

find(value):
  if predicate(value):        return true        # 命中，停
  if steps > max_steps:       return false       # 防递归爆炸，停
  steps += 1
  按 value 的类型下钻：
    Dict     → 对每个值递归
    Content  → 对每个字段值递归
    Module   → 对作用域里每个绑定值递归
    其它      → 不下钻
  return false
```

两个设计点：

- **短路**：一旦在任何深度命中谓词，立即返回 `true`，不继续搜索。
- **步数上限 `max_steps = 1000`**：防止遇到循环引用或巨型结构时把补全卡死。超过上限直接判 `false`（保守地不提示），这是 typst-ide 一贯的 best-effort 风格。

为什么只对 `Dict`/`Content`/`Module` 三种下钻？因为只有这三种是「装着别的 `Value` 的容器」。数组（`Array`）也是容器，但这里有意不递归——一则性能，二则颜色很少成数组传入。

#### 4.2.3 源码精读

公开入口 [`check_value_recursively`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/utils.rs#L186-L195) 只是 `Searcher` 的薄包装：

```rust
pub fn check_value_recursively(
    value: &Value,
    predicate: impl Fn(&Value) -> bool,
) -> bool {
    let mut searcher = Searcher { steps: 0, predicate, max_steps: 1000 };
    match searcher.find(value) {
        ControlFlow::Break(matching) => matching,
        ControlFlow::Continue(()) => false,
    }
}
```

真正干活的是 [`Searcher::find`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/utils.rs#L209-L234)：

```rust
fn find(&mut self, value: &Value) -> ControlFlow<bool> {
    if (self.predicate)(value) {
        return ControlFlow::Break(true);
    }
    if self.steps > self.max_steps {
        return ControlFlow::Break(false);
    }
    self.steps += 1;
    match value {
        Value::Dict(dict) => {
            self.find_iter(dict.iter().map(|(_, v)| v))?;
        }
        Value::Content(content) => {
            self.find_iter(content.fields().iter().map(|(_, v)| v))?;
        }
        Value::Module(module) => {
            self.find_iter(module.scope().iter().map(|(_, b)| b.read()))?;
        }
        _ => {}
    }
    ControlFlow::Continue(())
}
```

`ControlFlow::Break(true)` 表示「找到匹配」，`Break(false)` 表示「超步数上限主动放弃」，`Continue(())` 表示「这一层没命中，交给上层继续」。`?` 在 `find_iter` 里起到「命中即短路返回」的作用。

#### 4.2.4 代码实践

**目标**：亲手验证「含颜色的字典/模块」算颜色。

**步骤**：仓库里已经有一个精确对应的测试 [`test_autocomplete_value_filter`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1847-L1858)：

```rust
let world = TestWorld::new("#import \"design.typ\": clrs; #rect(fill: )")
    .with_source(
        "design.typ",
        "#let clrs = (a: red, b: blue); #let nums = (a: 1, b: 2)",
    );

test(&world, -2)
    .must_include(["clrs", "aqua"])
    .must_exclude(["nums", "a", "b"]);
```

阅读这段测试，然后运行：

```bash
cargo test -p typst-ide --lib complete::tests::test_autocomplete_value_filter
```

**观察与解释**：

- `clrs`（字典 `(a: red, b: blue)`）被**包含**——因为 `check_value_recursively` 下钻进字典找到 `red`/`blue`，谓词「是颜色」命中。
- `nums`（字典 `(a: 1, b: 2)`）被**排除**——下钻后全是整数，不命中。
- `aqua`（标准库颜色）被包含——来自 `globals`，顶层就是颜色。
- `a`、`b` 被排除——它们不是 `design.typ` 导出的名字（只导出了 `clrs` 和 `nums`），不在作用域里。

**预期结果**：测试通过，证明 `check_value_recursively` 让 `clrs` 这类容器通过了颜色过滤。

#### 4.2.5 小练习与答案

**练习 1**：一个「字典里嵌套字典，最深处有一个颜色」的值，能否通过颜色过滤？步数上限会不会成为障碍？
**答案**：能。`find` 对 `Dict` 逐层下钻，只要在 1000 步内命中颜色就返回 `true`。正常嵌套远达不到 1000 步。

**练习 2**：为什么 `Array` 不在下钻列表里？若想让它也参与，应改哪一行？
**答案**：见 4.2.2 的设计说明。若要支持，需在 `match value` 里加 `Value::Array(arr) => self.find_iter(arr.iter())?`（示例代码，原项目未实现）。

---

### 4.3 CastInfo —— 描述「参数能接受什么」的元信息

#### 4.3.1 概念说明

函数参数都有类型约束：`#rect(fill: ?)` 里 `fill` 要颜色，`#align(? )` 里 `alignment` 要对齐方式。typst 用一个枚举 `CastInfo` 来**自描述**这种约束——它是 typst-library 在 `cast!` 宏里为每个可转换类型生成的「形状说明」。`cast_completions` 就靠读这张「说明书」来决定该补出哪些候选。

`CastInfo` 只有四个变体，非常规整：

| 变体 | 含义 | 例子 |
|---|---|---|
| `Any` | 任意值都行 | 不产生任何补全（什么都接受就不提示） |
| `Value(Value, &'static str)` | 一个**具体值** + 一句文档 | `role: "alertdialog"` 这类枚举字符串 |
| `Type(Type)` | 某类型的**任意值** | `Color`、`bool`、`Label`、`integer` |
| `Union(Vec<Self>)` | 多选一（递归） | `Alignment` = 一堆关键字 ∪ 对齐类型 |

它天然是个递归结构：`Union` 里可以再套 `Union`/`Type`/`Value`，正好对应 Typst「一个参数可以接受多种形态」的类型系统。

#### 4.3.2 核心流程

`CastInfo` 本身只是数据，没有「流程」。要理解它怎么被填出来，只需知道：typst-library 里每个类型/参数用 `cast!` 宏实现 `Reflect::input() -> CastInfo`，编译期就生成好了。比如对齐类型的 `input()` 返回的大致是：

```
Union([
  Value("start", ..), Value("end", ..),
  Value("left", ..),  Value("right", ..),
  Value("top", ..),   Value("bottom", ..),
  Value("center", ..),Value("horizon", ..),
  Type(alignment),
])
```

（具体候选以 typst-library 的 `cast!` 定义为准；上面是根据 [`HAlignment`/`VAlignment` 的 `repr`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/align.rs#L334-L339) 推得的关键字集合。）这张「说明书」会在 4.4 里被 `cast_completions` 逐层拆开。

#### 4.3.3 源码精读

`CastInfo` 定义在 typst-library（是 typst-ide 的依赖，不在 `crates/typst-ide` 内）：

[`CastInfo` 枚举](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/cast.rs#L295-L304)：

```rust
pub enum CastInfo {
    /// Any value is okay.
    Any,
    /// A specific value, plus short documentation for that value.
    Value(Value, &'static str),
    /// Any value of a type.
    Type(Type),
    /// Multiple alternatives.
    Union(Vec<Self>),
}
```

记住这三个事实就够后续阅读：

1. `Value` 变体自带一句 `&'static str` 文档——这正是补全 `detail` 列的来源。
2. `Type` 变体只给「类型」，具体怎么补由 `cast_completions` 按类型特化处理（见 4.4）。
3. `Union` 让 `CastInfo` 可递归，`cast_completions` 也会递归处理它。

#### 4.3.4 代码实践

**目标**：从测试反推 `CastInfo` 的形状。

**步骤**：阅读 [`test_autocomplete_typed_html`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1970-L1976)：

```rust
test("#html.div(translate: )", -2).must_include(["true", "false"]).must_exclude([q!("yes"), q!("no")]);
test("#html.input(value: )", -2).must_include(["float", "string", "red", "blue"]);
test("#html.div(role: )", -2).must_include([q!("alertdialog")]);
```

**观察与对应**：

- `translate:` 补出 `true`/`false` → `translate` 的 `CastInfo` 含 `Type(bool)`。
- `value:` 补出 `float`/`string`/`red`/`blue` → 是个 `Union`，里面有若干 `Value(..)` 具体值。
- `role:` 补出 `"alertdialog"` → `CastInfo::Value(Str("alertdialog"), ..)`，补全的 `detail` 就是那句 `&'static str`。

**预期结果**：你能看着一个参数的补全结果，反推出它大概对应 `CastInfo` 的哪个变体。

#### 4.3.5 小练习与答案

**练习 1**：一个参数的 `CastInfo` 是 `Any`，`cast_completions` 会补出什么？
**答案**：什么都不补（`Any => {}`），因为「什么都接受」无从提示。

**练习 2**：为什么 `Value` 变体要带一句 `&'static str`？
**答案**：这句文档直接用作补全项的 `detail`，让用户在候选菜单里看到每个枚举值的含义，无需再查文档。

---

### 4.4 cast_completions —— 按 CastInfo 递归生成候选

#### 4.4.1 概念说明

`cast_completions` 是「类型驱动补全」的发动机：吃进一个 `CastInfo`，按其变体递归地产出补全候选。它被参数值补全调用——当一个具名参数（如 `fill:`）后面需要填值、且函数是原生函数时，引擎就把该参数的 `CastInfo` 喂给它。

它的两条主轴：

1. **递归展开**：遇到 `Union` 就逐个子项再调自己；遇到 `Type`/`Value` 就落地成具体补全。
2. **按类型特化**：对 `bool`、`Color`、`Label`、`Func`、`none`/`auto` 等常见类型，手写了一批更友好的 snippet，而不是干巴巴地扔一个类型名。

它还用 `seen_casts`（一个 `FxHashSet<u128>`）记录已处理过的 `CastInfo` 哈希，防止 `Union` 递归展开时产生重复候选。

#### 4.4.2 核心流程

```
cast_completions(cast):
  若 hash128(cast) 已在 seen_casts → 直接返回（去重）
  把 hash 加入 seen_casts
  match cast:
    Any          → 什么都不做
    Value(v, docs) → value_completion_full(None, v, false, None, Some(docs))   # 用自带文档
    Type(ty)     → 按 ty 特化：
        none    → snippet "none"
        auto    → snippet "auto"
        bool    → snippet "false" / "true"
        Color   → 一堆造色 snippet（luma/rgb/cmyk/oklab/oklch/...）
                   + scope_completions(false, |v| v.ty()==Color)   # 复用 4.1！
        Label   → label_completions()
        Func    → snippet "function"
        其它    → 一个占位 snippet（label=类型长名）
                   + scope_completions(false, |v| v.ty()==ty)
    Union(items) → 对每个子项递归 cast_completions
```

注意两个「复用」：

- `Color` 和「其它类型」分支都调了 [`scope_completions`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1426-L1462)，把第 4.1 节的整套机制（局部优先 + 递归过滤）搬过来，只是把 `filter` 换成「类型等于 `ty`」。**这就是 `#rect(fill: |)` 能补出 `clrs`、`aqua` 的最终一环**：`fill` 的 `CastInfo` 是 `Type(Color)`，进入 Color 分支后调 `scope_completions(false, |v| v.ty()==Color)`。
- `Label` 分支复用了 `label_completions`（见 u6-l3）。

#### 4.4.3 源码精读

[`cast_completions`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1336-L1421) 的骨架（为可读性折叠了部分 snippet）：

```rust
fn cast_completions(&mut self, cast: &CastInfo) {
    // 防止重复补全
    if !self.seen_casts.insert(typst::utils::hash128(cast)) {
        return;
    }

    match cast {
        CastInfo::Any => {}
        CastInfo::Value(value, docs) => {
            self.value_completion_full(None, value, false, None, Some(docs));
        }
        CastInfo::Type(ty) => {
            if *ty == Type::of::<NoneValue>() {
                self.snippet_completion("none", "none", "Nothing.");
            } else if *ty == Type::of::<AutoValue>() {
                self.snippet_completion("auto", "auto", "A smart default.");
            } else if *ty == Type::of::<bool>() {
                self.snippet_completion("false", "false", "No / Disabled.");
                self.snippet_completion("true", "true", "Yes / Enabled.");
            } else if *ty == Type::of::<Color>() {
                // ... luma() / rgb() / cmyk() / oklab() / oklch() / color.linear-rgb() ...
                self.scope_completions(false, |value| value.ty() == *ty);
            } else if *ty == Type::of::<Label>() {
                self.label_completions();
            } else if *ty == Type::of::<Func>() {
                self.snippet_completion("function", "(${params}) => ${output}", "A custom function.");
            } else {
                self.completions.push(Completion { /* label = ty.long_name() */ });
                self.scope_completions(false, |value| value.ty() == *ty);
            }
        }
        CastInfo::Union(union) => {
            for info in union {
                self.cast_completions(info);
            }
        }
    }
}
```

对照几个关键行：

- [`if !self.seen_casts.insert(typst::utils::hash128(cast))`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1338-L1338)：对 `CastInfo` 取 128 位哈希去重。`Union` 展开时同一个子类型可能反复出现（比如多个分支都含 `Type(Color)`），这里保证只处理一次。
- [`CastInfo::Value(value, docs) => self.value_completion_full(None, value, false, None, Some(docs))`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1344-L1346)：`Value` 变体的那句 `docs` 直接当成 `detail`。
- `Color` 分支末尾的 [`self.scope_completions(false, |value| value.ty() == *ty)`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1396-L1396)：这一行是 4.1 与 4.4 的交汇点。
- [`CastInfo::Union(union) => for info in union { self.cast_completions(info); }`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1415-L1419)：递归的源头。

那么 `cast_completions` 是被谁调用的？参数值补全链 [`param_value_completions`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L587-L600) 的末尾：

```rust
fn param_value_completions(ctx: &mut CompletionContext, func: &Func, param: &ParamInfo) {
    if param.name() == Some("font") {
        ctx.font_completions();
    } else if let Some(extensions) = path_completion(func, param) {
        ctx.file_completions_with_extensions(extensions);
    } else if func.name() == Some("figure") && param.name() == Some("body") {
        /* image / table snippet */
    }

    if let ParamInfo::Native(param) = param {
        ctx.cast_completions(&param.input);   // ← 类型驱动补全的入口
    }
}
```

也就是说：`fill:` 这类具名参数值，先走几个特化（字体/路径/figure body），最后只要函数是原生函数，就把参数的 `param.input`（一个 `CastInfo`）交给 `cast_completions`。

#### 4.4.4 代码实践

**目标**：追踪 `cast_completions` 对一个 alignment 类型参数的递归展开。

**步骤**：以 `#align(center|)`（或任意接受 `alignment` 的参数位置）为例。设 `alignment` 的 `CastInfo` 形如 4.3.2 给出的 `Union([Value("start",..), .., Value("horizon",..), Type(alignment)])`。

1. 第一次进入 `cast_completions(Union(...))`：`seen_casts` 记下该 Union 的哈希。
2. `Union` 分支：对每个子项递归。
3. 对 `Value("start", docs)`：走 `value_completion_full`，产出一个 `label = "start"`、`detail = docs` 的候选。
4. 依次产出 `end`/`left`/`right`/`top`/`bottom`/`center`/`horizon` 等关键字候选。
5. 对 `Type(alignment)`：落到 `else` 分支，先 push 一个占位候选（类型长名），再调 `scope_completions(false, |v| v.ty() == alignment)`——于是标准库里所有「alignment 类型」的值（其实就是上面那些关键字在全局作用域里的绑定）也会过一遍 `check_value_recursively` 参与。
6. 若 Union 里再次出现 `Type(alignment)`（或与已处理项哈希相同），`seen_casts` 会跳过，避免重复。

**观察**：候选菜单里既有具体关键字（`start`、`center`…），也可能有由 `scope_completions` 补出的同类型全局项；这正是 `Union` 与 `Type` 双重来源的叠加。

**预期结果**：你能用「Union → 递归 → Value/Type 分别落地」这三步，讲清任何一个多形态参数的补全过程。

> 待本地验证：alignment 的 `CastInfo` 精确成员取决于 typst-library 的 `cast!` 定义，本讲依据 [`HAlignment`/`VAlignment` 的 `repr`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/layout/align.rs#L334-L339) 推得关键字集合；若要确认完整列表，可在 typst-library 里阅读 `Alignment` 的 `cast!` 块。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `Color` 分支既加了一堆 `luma()`/`rgb()` snippet，又调了 `scope_completions`？
**答案**：snippet 覆盖「用户想**构造**一个新颜色」的需求；`scope_completions` 覆盖「用户想**引用**一个已有颜色」（标准库 `red`/`aqua`、或装着颜色的局部字典/模块）。两者互补。

**练习 2**：若没有 `seen_casts` 去重，一个含两次 `Type(Color)` 的 `Union` 会怎样？
**答案**：颜色相关的 snippet 和 `scope_completions` 结果会被产出两遍，候选列表出现重复项。

---

## 5. 综合实践

把第 4 节的四块串起来，完整解释本讲的招牌问题：**`#rect(fill: |)` 为什么会补出所有颜色，以及「内部含颜色的模块/字典」？**

调用链（从光标到候选）：

```
#rect(fill: |)
  └─ autocomplete → complete_params               (u6-l1：识别到参数列表 + 决定补"参数值")
       deciding 节点回溯到 ':' → named_param_value_completions(callee=rect, name="fill")
            └─ param_value_completions(func=rect, param=fill)
                 ├─ fill 不是 font/path/figure.body，跳过特化
                 └─ ParamInfo::Native → cast_completions(fill.input)
                        fill.input = CastInfo::Type(Color)
                        └─ Color 分支：
                             ├─ 造色 snippet：luma()/rgb()/cmyk()/oklab()/oklch()/...
                             └─ scope_completions(false, |v| v.ty()==Color)
                                   ├─ filter' = |v| check_value_recursively(v, 是颜色?)
                                   ├─ 局部 named_items：
                                   │     clrs = (a: red, b: blue) → 下钻命中 red → 通过 → 富补全
                                   │     nums = (a: 1, b: 2)       → 下钻无颜色 → 排除
                                   └─ 全局 globals(library.global)：
                                         red/aqua/... → 顶层即颜色 → 通过
                                         (任何与局部同名的全局被 defined.contains_key 压制)
```

**任务**：

1. 自己用一张纸画出上面这条链，标注每一步落在哪个函数、哪一行。
2. 解释「为什么 `nums` 不出现、`a`/`b` 不出现、`clrs` 却出现」——三者的原因各不相同（`nums`：递归过滤未命中；`a`/`b`：不在作用域；`clrs`：递归过滤命中）。
3. （选做）在 `src/complete.rs` 的 `tests` 模块里，仿照 [`test_autocomplete_value_filter`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1847-L1858) 新增一个用例：让一个**模块**（而非字典）内部含颜色，断言它出现在 `fill:` 的补全里。提示：可以用 `with_source` 准备一个 `#let palette = (red, blue)` 的数组，先预测它**不会**出现（数组不下钻），再运行验证你的预测。

**预期结果**：你能不查源码，向别人讲清「光标在 `fill:` 后，为什么菜单里既有 `rgb()` 又有 `aqua` 又有我自己的 `clrs`」。

## 6. 本讲小结

- `scope_completions` 合并**局部**（`named_items`）与**全局**（`globals`）两套命名，靠 `defined.contains_key(name)` 这一守卫保证**局部优先于全局**；`BTreeMap` 让输出按字母序稳定且按名字去重。
- 局部 `Var`/`Fn`（值未知）走「纯名字」补全，`Module`/`Import`（值已知）和全局项走带 `repr`/文档的「富」补全。
- `check_value_recursively` 是一个带 `max_steps=1000` 上限的深度优先搜索，只对 `Dict`/`Content`/`Module` 下钻，让「装着目标类型值的容器」也通过过滤——这就是「含颜色的字典/模块也算颜色」的根因。
- `CastInfo`（`Any`/`Value`/`Type`/`Union`）是 typst-library 自描述的「参数能接受什么」的递归模型，`Value` 变体自带的那句 `&'static str` 直接成为补全的 `detail`。
- `cast_completions` 按 `CastInfo` 递归展开：`Union` 分治、`Value` 落地、`Type` 按类型特化（`bool`/`Color`/`Label`/`Func`/`none`/`auto` 等），并用 `seen_casts` 的 128 位哈希去重。
- 4.1 与 4.4 在 `Color` 等 `Type` 分支交汇：`cast_completions` 调 `scope_completions(filter=按类型)`，把整套「局部优先 + 递归过滤」搬进类型驱动补全，这正是 `#rect(fill: |)` 行为的完整解释。

## 7. 下一步学习建议

- 下一讲 **u6-l5（apply 片段与 BracketMode）** 会接着 `value_completion_full` 往下讲：一个值最终被写成什么样的 `apply` 文本、函数调用为何有时补 `(...)` 有时补 `[...]`。本讲的 `scope_completions`/`cast_completions` 都是它的「候选来源」。
- 想巩固「容器参与过滤」的直觉，可重读 [`Searcher`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/utils.rs#L199-L245)，并尝试回答：若要让 `Array` 也参与下钻，需要改哪里、会有什么副作用。
- 想看更多 `CastInfo` 的真实形状，可去 `crates/typst-library` 里搜索 `cast!` 宏的用法，对照本讲 4.3/4.4 的分类去读。
