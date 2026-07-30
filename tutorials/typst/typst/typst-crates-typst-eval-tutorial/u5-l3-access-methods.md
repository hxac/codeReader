# 可变访问 Access 与内置方法

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清楚为什么 Typst 需要一个独立的 `Access` trait——它和返回「所有权值」的 `Eval` trait 有什么本质区别。
- 追踪一次 `array.insert(0, x)`、`array.at(0) = 5` 的完整求值路径，知道解释器在哪一步拿到了「指向真实存储位置的 `&mut Value`」。
- 理解 `call_method_mut`（push/pop/insert/remove）与 `call_method_access`（first/last/at）两套内置方法的分工。
- 解释 `access_dict` 对 symbol/content/module/func/args 与对普通类型（Length/Stroke 等）给出不同报错的原因。

本讲是「模块系统、样式规则与可变性」单元的收尾，专门回答一个问题：**Typst 的值都是按值克隆的，那「就地修改」是怎么做到的？**

## 2. 前置知识

本讲承接 **u4-l2（字段访问与方法调用分派）** 和 **u3-l4（一元与二元运算符求值）**，请确认你已经理解下面几个概念。

### 2.1 Typst 的值是「按值」流转的

回顾 `Eval` trait 的签名（见 [u1-l4](u1-l1-eval-trait-vm.md)）：

```rust
fn eval(self, vm: &mut Vm) -> SourceResult<Output>;
```

它消费 AST 节点，返回一个**全新的、拥有所有权的** `Value`。读一个变量、读一个字段、算一个表达式，产出的都是克隆出来的值副本。这对「读」没问题，但对「写」是个麻烦：

```typst
#let x = 1
#x = 2          // 赋值：要改的是 x 绑定本身，不是算出一个新值
#array.push(3)  // 变更方法：要改的是 array 内部的元素，不是返回新数组
#array.at(0) = 9 // 复合位置的赋值：要改的是数组第 0 个槽位
```

这三类操作都需要拿到**指向真实存储位置的 `&mut Value`**，而不是一个值副本。这就是本讲的全部动机。

### 2.2 Rust 的可变借用

`&mut Value` 是 Rust 的可变引用：同一时间只能有一个可变引用指向某块数据。typst-eval 在求值时大量利用「先求参数、再借目标」的顺序来满足这条规则——这一点会在后面反复出现，是理解多个函数为什么「参数先求」的关键。

### 2.3 u4-l2 留下的伏笔

u4-l2 讲了「读取型」的点号 `a.b`（走 `FieldAccess::eval → access_field`，返回 `Value`）和「调用型」的点号 `a.b(...)`（走 `FuncCall::eval → eval_field_callee`）。本讲要补上第三类：**可变型**的点号——`a.b = x`、`a.b() = x`、`a.push(x)`，它们走的是 `Access` trait 和 `methods.rs`，而不是 `Eval`。

> 一句话区分：`Eval` 负责「读」，`Access` 负责「改」。

## 3. 本讲源码地图

本讲涉及 4 个源文件，分两组：

| 文件 | 角色 | 本讲用到的核心内容 |
|------|------|--------------------|
| [src/access.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/access.rs) | 可变访问的「入口」 | `Access` trait 及 5 个 `impl`、`access_dict` |
| [src/methods.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/methods.rs) | 内置方法的「实现」 | `is_mutating_method`/`is_dict_mutating_method`/`is_accessor_method`、`call_method_mut`、`call_method_access` |
| [src/call.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs) | 调用链「拦截器」 | `FuncCall::eval` 中的变更方法拦截、`maybe_resolve_mutating` |
| [src/ops.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/ops.rs) | 赋值运算的「消费者」 | `apply_assignment`（来自 u3-l4，这里复看它如何用 `Access`） |

数据流向（自上而下）：

```
        FuncCall::eval (call.rs)          Binary::eval / apply_assignment (ops.rs)
             │                                       │
   is_mutating_method? ──yes──► maybe_resolve_mutating        │
             │no                              │ target.access │
             ▼                                ▼               │
      eval_field_callee              Access::access ◄─────────┘
      (常规方法/字段调用)                  │
                                  ┌───────┴───────┐
                          call_method_mut    call_method_access
                          (push/pop/...)      (first/last/at)
                                  │                   │
                                  └──► Array/Dict 的 &mut 方法（typst-library）
```

## 4. 核心概念与源码讲解

### 4.1 Access trait：统一可变访问接口

#### 4.1.1 概念说明

`Access` trait 是 typst-eval 为「就地修改」设计的统一接口。它的形态和 `Eval` 几乎对称，但有两个关键差异：

- 返回类型是 `&'a mut Value`——一个**可变引用**，指向值真正存放的地方（作用域里的某个绑定、字典的某个键值、数组的某个槽位）。
- 生命周期 `'a` 把引用绑定到借走的 `vm` 上，保证「引用活着的时候，虚拟机不能同时被别人可变借用」。

它的存在回答了一个设计问题：解释器如何在「值按值克隆」的模型下实现「改」？答案就是——**当需要改的时候，不调用 `eval`（克隆），而调用 `access`（借引用）**。

#### 4.1.2 核心流程

`Access` 只对「有明确存储位置」的表达式有意义。trait 本身只声明一个方法：

```rust
fn access<'a>(self, vm: &'a mut Vm) -> SourceResult<&'a mut Value>;
```

哪些表达式「有存储位置」？只有四类：

1. `Ident`（标识符）——指向作用域里的一个绑定。
2. `Parenthesized`（括号）——透传到内部表达式。
3. `FieldAccess`（`a.b`）——指向 `a` 的某个字段。
4. `FuncCall`（`a.first()`、`a.at(0)` 等访问器调用）——指向返回的可变槽位。

其余一切表达式（字面量、算式、函数调用的结果）都是「临时值」，没有存储位置，调用 `access` 会报错。

#### 4.1.3 源码精读

trait 定义极简，见 [src/access.rs:8-12](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/access.rs#L8-L12)。这段代码声明了「取一个可变引用」的统一约定。

「总分发器」是 `impl Access for ast::Expr`，它用一个 `match` 把四类表达式派发到各自实现，其余报「临时值不可变」，见 [src/access.rs:14-27](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/access.rs#L14-L27)。注意兜底分支的写法：

```rust
_ => {
    let _ = self.eval(vm)?;          // 先正常求值一遍（让内部的错误能抛出来）
    bail!(self.span(), "cannot mutate a temporary value");
}
```

这里有个细节：即便要报「不可变」，它**仍然先 `self.eval(vm)?`**。目的是让表达式中可能存在的其它错误（比如函数调用本身报错）优先暴露，而不是被一句笼统的「cannot mutate」盖掉。

四个具体实现：

- **Ident**（[src/access.rs:29-42](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/access.rs#L29-L42)）：先用 `vm.scopes.get` 做一次不可变读取（仅为命中 `inspected` span 时触发 IDE 追踪 `vm.trace`），再调用 `vm.scopes.get_mut(&self)` 拿到可变 `Binding`，最后 `b.write()` 取出 `&mut Value`。`write()` 这一步是「捕获变量只读」检查的关口（见 4.2 节与 [typst-library scope.rs:315-327](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/scope.rs#L315-L327)，对 `Captured` 绑定返回「variables from outside the function/context expression are read-only」）。
- **Parenthesized**（[src/access.rs:44-48](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/access.rs#L44-L48)）：`(a)` 的可变访问就是 `a` 的可变访问，括号透明。
- **FieldAccess**（[src/access.rs:50-54](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/access.rs#L50-L54)）：委托 `access_dict`（4.2 节详解），拿到 `&mut Dict`，再 `.at_mut(field)` 取到具体字段的 `&mut Value`。
- **FuncCall**（[src/access.rs:56-74](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/access.rs#L56-L74)）：只对「访问器方法」`first`/`last`/`at` 放行（4.4 节详解），其余按临时值报错。

#### 4.1.4 代码实践

**实践目标**：亲手验证「临时值不可变」与「标识符可变」的差异。

**操作步骤**（源码阅读型实践）：

1. 打开 [src/access.rs:14-27](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/access.rs#L14-L27)，确认 `ast::Expr` 的 `access` 只匹配四种 `Self::…`，其余走 `_` 分支。
2. 打开 [src/ops.rs:73-95](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/ops.rs#L73-L95) 的 `apply_assignment`，看它如何对 `=` 左侧调用 `binary.lhs().access(vm)`。
3. 据此预测下面两段 Typst 代码的行为：

```typst
// (A) 左侧是标识符
#let x = 1
#x = 2

// (B) 左侧是临时值（1 + a 的结果）
#(1 + 1) = 2
```

**需要观察的现象**：

- (A) 应当正常执行：`x.access` 命中 `Ident` 分支，拿到 `&mut Value` 写入 2。
- (B) 应当报错：`(1 + 1)` 是 `Binary` 表达式，不在 `Access` 的四种匹配内，走 `_` 分支先 `eval` 得到临时值 `2`，再 `bail!("cannot mutate a temporary value")`。

**预期结果**：(A) 成功；(B) 报 `cannot mutate a temporary value`。本地若有 typst CLI 可用 `typst compile` 验证；若无环境则标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Ident::access` 里要先 `vm.scopes.get`（不可变）做一次，再 `vm.scopes.get_mut`（可变）做一次？这两次查询能否合并成一次 `get_mut`？

**参考答案**：第一次 `get` 仅仅是为了在 `vm.inspected == Some(span)`（IDE 正在检查这个标识符）时，调用 `vm.trace(binding.read().clone())` 把值送给 IDE 的 hover/tooltip。它必须发生在可变借用之前，因为 `trace` 之后还要继续 `get_mut`。如果直接用 `get_mut`，就无法在「借出可变引用之前」把值读出来用于追踪。二者职责不同，不可合并。

**练习 2**：下面哪种写法能成功修改 `d`？解释原因。

```typst
#let d = (a: 1)
#d.a = 2        // 写法 1
#(d).a = 2      // 写法 2
```

**参考答案**：两种都能成功。写法 1 的 `d.a` 是 `FieldAccess`，直接命中 `impl Access for FieldAccess`。写法 2 多了一层括号 `(d).a`，但 `Parenthesized::access` 会透传到内部 `d`，最终同样命中 `FieldAccess`——括号不影响可访问性。

---

### 4.2 access_dict：字段访问的可变边界

#### 4.2.1 概念说明

`access_dict` 是 `FieldAccess::access` 的实际执行者，回答一个具体问题：「`obj.field` 想要一个可变引用，但 `obj` 不是字典怎么办？」

它的职责是：从 `obj` 的可变引用里**取出一个 `&mut Dict`**。只有当 `obj` 真的是字典时才能直接返回；对其它类型，它要给出**有区分度的报错**——这正是本讲要讲清的「为什么不同类型报不同错」。

#### 4.2.2 核心流程

`access_dict` 的判定流程是一个三选一的 `match`：

1. `Value::Dict(dict)` → 直接返回 `Ok(dict)`，字段可变。
2. `Symbol`/`Content`/`Module`/`Func`/`Args` → 报 **"cannot mutate fields on {ty}"**。这五类「有自己的字段读取器」(have their own field getters)，字段是只读的。
3. `fields_on(ty).is_empty()` → 报 **"{ty} does not have accessible fields"**。这类类型根本没有字段。
4. 其余（有静态字段但还没有 setter，如 `Length`/`Stroke`/`Version`/`Alignment`/`Rel`）→ 报 **"fields on {ty} are not yet mutable"**，并附 hint「试着构造一个新值」。

注意第 2、3、4 步的区别，这正是练习任务要回答的核心。

#### 4.2.3 源码精读

见 [src/access.rs:76-107](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/access.rs#L76-L107)。关键的三段报错逻辑集中在 [src/access.rs:82-104](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/access.rs#L82-L104)：

```rust
value => {
    let ty = value.ty();
    let span = access.target().span();
    if matches!(value,  // those types have their own field getters
        Value::Symbol(_) | Value::Content(_) | Value::Module(_)
            | Value::Func(_) | Value::Args(_)
    ) {
        bail!(span, "cannot mutate fields on {ty}");        // 分支 2
    } else if typst_library::foundations::fields_on(ty).is_empty() {
        bail!(span, "{ty} does not have accessible fields"); // 分支 3
    } else {
        // type supports static fields, which don't yet have setters
        Err(eco_format!("fields on {ty} are not yet mutable"))
            .hint(eco_format!("try creating a new {ty} ..."))  // 分支 4
            .at(span)
    }
}
```

为什么对五类（symbol/content/module/func/args）报「cannot mutate fields」，而对 Length/Stroke 等报「not yet mutable」？区别在于**字段的来源**：

- 五类类型的字段是通过**自定义 getter**（`value.field(...)`）读取的，是类型自身的语义（比如 `symbol.l` 取符号变体、`content.func` 取元素函数），**设计上就是只读**，未来也不打算开放写入，所以措辞是断然拒绝的「cannot mutate」。
- Length/Stroke/Version/Alignment/Rel 这些类型的字段是**静态字段**（由 [typst-library fields.rs:77-91](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/fields.rs#L77-L91) 的 `fields_on` 列出，如 `Length` 的 `em`/`abs`、`Stroke` 的 `paint`/`thickness` 等），**读取可行，只是还没有实现 setter**。源码注释明说「don't yet have setters」，所以措辞是留有余地的「not yet mutable」，并附上「构造一个新值」的修复 hint。

这种「按字段来源分级报错」的设计，让错误信息既准确又指明了出路。

#### 4.2.4 代码实践

**实践目标**：观察三类报错的差异。

**操作步骤**：

1. 在 [src/access.rs:85-93](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/access.rs#L85-L93) 确认五类只读类型清单。
2. 在 [typst-library fields.rs:77-91](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/fields.rs#L77-L91) 确认「有静态字段」的类型清单（Version/Length/Rel/Stroke/Alignment）。
3. 预测下面三段代码各自得到哪条报错（分支 2 / 3 / 4）：

```typst
#let s = sym.arrow
#s.l = sym.arrow.r        // (A) symbol 的字段
#let len = 10pt
#len.abs = 5pt            // (B) length 的静态字段
#let b = true
#b.x = 1                  // (C) bool 的「字段」
```

**需要观察的现象与预期结果**：

- (A) 命中分支 2 → `cannot mutate fields on symbol`。
- (B) 命中分支 4 → `fields on length are not yet mutable` + hint `try creating a new length with the updated field value instead`。
- (C) 命中分支 3 → `boolean does not have accessible fields`（bool 不在 `fields_on` 清单里，返回空）。

若本地可运行 typst，用 `typst compile` 三段分别验证；否则标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`access_dict` 为什么对 `Value::Dict` 直接放行，而不像分支 4 那样报「not yet mutable」？

**参考答案**：字典的字段就是它内部的键值对，`Dict`（底层 `IndexMap<Str, Value>`）天然支持通过 `at_mut`/`insert` 就地修改，有真正的 setter，所以直接返回 `&mut Dict`。而分支 4 的类型虽然有「静态字段」概念，但其字段是只读的计算属性（如 `Length.abs`），运行时结构里没有可写的槽位，故只能报「尚未可变」并建议重建。

**练习 2**：如果有人新增一种类型 `Foo`，希望它的字段可被赋值修改，需要改动哪些地方？

**参考答案**：不能只在 typst-eval 改。需要让 `Foo` 的运行时表示能像 `Dict`/`Array` 那样暴露 `&mut` 槽位（类似 `first_mut`/`at_mut`），并在 `access_dict` 的 `match` 里为它加一条返回 `&mut` 的分支——这正是当前架构只对 `Dict` 开放字段写入的根本约束。若只是只读字段，则应走 `fields_on` 清单（分支 4 路径）。

---

### 4.3 变更方法：is_mutating_method 与 call_method_mut

#### 4.3.1 概念说明

「变更方法」(mutating method) 指那些**就地修改接收者**、而非返回新值的内置方法，目前只有四个：`push`、`pop`、`insert`、`remove`。它们作用于数组（全部四个）和字典（仅 `insert`/`remove`）。

这些方法在 typst-eval 里是**特殊处理**的——它们不走 u4-l2 讲的那条「方法 → 把 target 作为首参 → 调用普通函数」的常规路径，而是在 `FuncCall::eval` 一开始就被拦截，转去走 `Access` 拿可变引用、再调用 `call_method_mut` 就地改。原因很简单：常规路径会把 target 当成一个**值参数**传进去，那就只能改副本、改不到原对象。

#### 4.3.2 核心流程

拦截发生在 `FuncCall::eval` 里，整条链路如下：

```
FuncCall::eval (callee 是 FieldAccess)
  │
  ├─ check_call_depth
  │
  ├─ is_mutating_method(field)?  ── 是 ──┐
  │                                      ▼
  │                          maybe_resolve_mutating
  │                          (1) 先 args.eval(vm)   ← 必须先求参数
  │                          (2) target.access(vm)  ← 再借可变引用
  │                          (3) 按 target 类型分派：
  │                              · Dict 且方法不在 dict 变更集 → 退回常规路径
  │                              · Array / Dict(且方法在集内) → call_method_mut
  │                              · 其它 → 退回常规路径
  │                          (4) 命中 → 返回 Value（通常是 none）
  │
  └─ 否 / 退回 ──► eval_field_callee（常规方法调用）
```

「退回常规路径」(refund) 是一个重要设计：如果 `target` 不是数组/字典（比如 `#str.push(1)`），拦截器不会硬报「无法变更」，而是把已经求好的 `(target, args)` 原样退回，让 `eval_field_callee` 走一遍，从而得到更准确的「type str has no method `push`」报错。

#### 4.3.3 源码精读

**方法分类**见 [src/methods.rs:9-21](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/methods.rs#L9-L21)：

```rust
pub(crate) fn is_mutating_method(method: &str) -> bool {
    matches!(method, "push" | "pop" | "insert" | "remove")
}
pub(crate) fn is_dict_mutating_method(method: &str) -> bool {
    matches!(method, "insert" | "remove")
}
pub(crate) fn is_accessor_method(method: &str) -> bool {
    matches!(method, "first" | "last" | "at")
}
```

注意 `is_mutating_method`（四全）与 `is_dict_mutating_method`（仅两个）的差集——正是这个差集决定了「字典上调用 push/pop 要退回常规路径」。

**拦截入口**在 [src/call.rs:37-44](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L37-L44)：当 callee 是 `FieldAccess` 且方法名属于变更集，调用 `maybe_resolve_mutating`；其返回 `Ok(value)` 表示已就地处理完直接返回，`Err((target, args))` 表示退回常规路径。

**核心拦截器** `maybe_resolve_mutating` 见 [src/call.rs:190-213](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L190-L213)。两处要点：

1. **参数先求**（[src/call.rs:197-199](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L197-L199)）：因为 `target.access(vm)` 会可变借用 `vm`，之后再也无法调 `args.eval(vm)`，所以必须颠倒顺序先求参数。源码注释明说了这一点。
2. **三路分派**（[src/call.rs:200-212](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L200-L212)）：`Dict 且非 dict 变更方法`退回；`Array | Dict(且是变更方法)`调 `call_method_mut`；其余退回。

**就地实现** `call_method_mut` 见 [src/methods.rs:24-63](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/methods.rs#L24-L63)。它接收 `&mut Value`，按内层类型与方法名分派到 typst-library 的 `Array`/`Dict` 方法：

- 数组分支 [src/methods.rs:35-47](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/methods.rs#L35-L47)：`push`（无返回值）、`pop`（返回被弹出的值）、`insert(index, value)`、`remove(index, default?)`。
- 字典分支 [src/methods.rs:49-56](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/methods.rs#L49-L56)：`insert(key, value)`、`remove(key, default?)`。

收尾的 `args.finish()`（[src/methods.rs:61](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/methods.rs#L61)）是参数校验关口——确保没有多余/缺失参数。注意 `output` 默认是 `Value::None`，只有 `pop`/`remove` 这类「取出元素」的方法才会改写它；`push`/`insert` 不返回有意义的值。

#### 4.3.4 代码实践

**实践目标**：追踪 `#let a = (1, 2); #a.insert(0, 0)` 的完整求值路径，验证数组被就地修改、调用返回 `none`。

**操作步骤**（调用链追踪型实践）：

1. 入口 `FuncCall::eval`（[src/call.rs:24-82](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L24-L82)）：`check_call_depth` 通过 → callee 是 `a.insert`（FieldAccess）。
2. `is_mutating_method("insert")` 为真 → 调 `maybe_resolve_mutating`（[src/call.rs:190](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L190)）。
3. 先求参数 `(0, 0)` 得 `Args`（[src/call.rs:199](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L199)）。
4. `target.access(vm)`：`a` 是 `Ident` → `Ident::access`（[src/access.rs:29-42](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/access.rs#L29-L42)）→ `scopes.get_mut("a")` → `Binding::write()` → `&mut Value` 指向数组绑定。
5. target 是 `Value::Array(_)`，命中 [src/call.rs:206-210](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L206-L210) → 调 `call_method_mut`。
6. `call_method_mut`（[src/methods.rs:38-40](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/methods.rs#L38-L40)）：数组分支 `insert` → `array.insert(args.expect("index")?, args.expect("value")?)`，即 `insert(0, 0)`。
7. `output` 保持 `Value::None`，`args.finish()` 通过，返回 `Value::None`。
8. 回到 `FuncCall::eval`（[src/call.rs:39](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L39)）：`Ok(value)` 分支直接 `return Ok(Value::None)`。

**需要观察的现象与预期结果**：调用后 `a` 变成 `(0, 1, 2)`（就地修改，不是返回新数组），整个表达式求值为 `none`。若在 typst 中用 `#a.insert(0, 0); #a` 打印，应看到 `(0, 1, 2)`。本地可编译验证；否则标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`#let d = (a: 1); #d.push(2)` 会发生什么？为什么？

**参考答案**：报错 `type dictionary has no method `push``。链路：`is_mutating_method("push")` 为真 → 进入 `maybe_resolve_mutating` → target 是 `Dict`，但 `is_dict_mutating_method("push")` 为**假**，命中 [src/call.rs:202-204](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L202-L204) 的退回分支，把 `(target, args)` 退回 → 走 `eval_field_callee` 常规分派 → 字典的 type scope 里没有 `push` → 报「无此方法」。这个「退回」设计保证了用户拿到的是准确的方法名错误，而不是误导性的「无法变更」。

**练习 2**：为什么 `maybe_resolve_mutating` 必须在 `target.access(vm)` **之前**调用 `args.eval(vm)`？

**参考答案**：因为 Rust 的借用规则。`target.access(vm)` 返回 `&'a mut Value`，其生命周期绑在 `&'a mut Vm` 上——一旦借出这个可变引用，`vm` 就被独占锁定，之后任何 `args.eval(vm)`（也需要 `&mut Vm`）都无法编译通过。所以必须先求完参数、释放对 `vm` 的借用，再去做可变访问。源码注释（[src/call.rs:197-199](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L197-L199)）明确写了这一点。

---

### 4.4 访问器方法：is_accessor_method 与 call_method_access

#### 4.4.1 概念说明

「访问器方法」(accessor method) 是另一类特殊的内置方法：`first`、`last`、`at`。它们既不是纯读取（因为要配合赋值用），也不是 `call_method_mut` 那种「无返回值的就地改」。它们的特殊之处在于：**返回的是一个 `&mut Value`**——指向数组/字典里某个槽位的可变引用。

这使它们能出现在赋值号左边：`array.first() = 9`、`dict.at("k") = 7`。这种「方法调用产生一个可写位置」的语义，就是由 `call_method_access` + `Access for FuncCall` 共同支撑的。

#### 4.4.2 核心流程

访问器方法的入口不是 `FuncCall::eval`（那条路返回的是值），而是 `Access for ast::FuncCall`，即当某个 `FuncCall` 出现在「需要可变引用」的语境里（赋值左侧、复合赋值左侧）时才走这条路：

```
apply_assignment (ops.rs): lhs.access(vm)
  └─► FuncCall::access (access.rs:56)
        ├─ callee 是 FieldAccess 且 is_accessor_method(method)?
        │     yes:
        │       (1) args.eval(vm)       ← 参数先求
        │       (2) target.access(vm)   ← 借可变引用（递归 Access）
        │       (3) call_method_access  ← 取出槽位的 &mut Value
        │       (4) trace 并返回该 &mut Value
        │     no:
        │       self.eval(vm) 后报 "cannot mutate a temporary value"
```

拿到 `&mut Value` 后，调用方（如 `apply_assignment`）就能往里写值。

#### 4.4.3 源码精读

**`Access for ast::FuncCall`** 见 [src/access.rs:56-74](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/access.rs#L56-L74)。它只对「callee 是 FieldAccess 且方法是访问器」放行；注意它和 `maybe_resolve_mutating` 一样**先求 args 再借 target**（[src/access.rs:62-64](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/access.rs#L62-L64)），同样的借用规则约束。之后调用 `call_method_access` 并包一层 `.trace(world, point, span)` 以产出正确的调用追踪点。

**`call_method_access`** 见 [src/methods.rs:66-98](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/methods.rs#L66-L98)。它返回 `&'a mut Value`，按类型/方法取槽位（[src/methods.rs:82-94](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/methods.rs#L82-L94)）：

- 数组：`first_mut()`、`last_mut()`、`at_mut(index)`。
- 字典：`at_mut(&key)`。

这些 `*_mut` 方法来自 typst-library（如 [array.rs:104-119](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/array.rs#L104-L119) 的 `first_mut`/`last_mut`/`at_mut`），它们返回的引用直指数组内部存储。

**临时值 vs 无此方法**的区分见 `temp_or_missing`（[src/methods.rs:73-80](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/methods.rs#L73-L80)）：它先查 `ty.scope().get(method)`，若类型其实有这个方法（只是当前是临时值不能改）报「cannot mutate a temporary value」，否则报 `missing_method`。这让 `#(1, 2, 3).first() = 9`（字面量数组临时值）报「临时值不可变」，而 `#(1, 2).len() = 9` 报「无此方法」。

**消费者** `apply_assignment` 见 [src/ops.rs:73-95](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/ops.rs#L73-L95)（u3-l4 已讲，此处复看它与 `Access` 的配合）。对普通赋值，它先求 rhs，再 `binary.lhs().access(vm)` 拿到 `&mut Value`，用经典的「读—改—写」三件套落盘（[src/ops.rs:91-93](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/ops.rs#L91-L93)）：

```rust
let location = binary.lhs().access(vm)?;
let lhs = std::mem::take(&mut *location);   // 读出旧值（位置变 none）
*location = op(lhs, rhs).at(binary.span())?; // 写回新值
```

`std::mem::take` 把槽位旧值「搬」出来（位置临时变 `none`），用 `op` 计算新值后再写回——这避免了「先克隆再赋值」的额外开销，也保证了 `+=` 这类复合赋值的原子性。

> 注意字段赋值 `obj.field = x` 是一个例外分支（[src/ops.rs:83-89](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/ops.rs#L83-L89)）：它直接用 `access_dict` 取 `&mut Dict` 再 `dict.insert`，因为字段赋值要能**创建新字段**（不仅仅是改已有字段），普通的 `Access` 路径做不到。这是 u3-l4 已讲过的细节，此处呼应。

#### 4.4.4 代码实践

**实践目标**：追踪 `#let a = (1, 2); #a.at(0) = 9` 的求值，确认第 0 个元素被改为 9。

**操作步骤**（调用链追踪型实践）：

1. `Binary::eval` 看到 `BinOp::Assign` → `apply_assignment`（[src/ops.rs:44](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/ops.rs#L44)）。
2. 求 rhs `9`。
3. lhs `a.at(0)` 不是 `FieldAccess`（是 `FuncCall`），跳过字段赋值特例分支（[src/ops.rs:83](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/ops.rs#L83)）。
4. `binary.lhs().access(vm)` → `FuncCall::access`（[src/access.rs:56](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/access.rs#L56)）：方法是 `at`，`is_accessor_method` 为真 → 先求 args `[0]`，再 `a.access(vm)` 借到数组引用。
5. `call_method_access(value, "at", args, span)`（[src/methods.rs:86](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/methods.rs#L86)）→ `array.at_mut(0)` → `&mut Value` 指向元素 `1`。
6. 回到 `apply_assignment`：`std::mem::take` 取出旧值 `1`，`op` 是恒等（纯 `=`），写回 `9`。

**需要观察的现象与预期结果**：`a` 变成 `(9, 2)`。整条链路全程没有克隆整个数组，只在第 0 槽位做了「取旧值—写新值」。本地可编译验证；否则标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：`#(1, 2, 3).first() = 9` 报什么错？为什么不是「无此方法」？

**参考答案**：报 `cannot mutate a temporary value`。链路：`FuncCall::access` 发现方法是 `first`、`is_accessor_method` 为真，于是走访问器路径——但 `target.access(vm)` 的 target 是 `(1, 2, 3)` 这个数组字面量。数组字面量是 `ast::Array`，不在 `Access` 的四种匹配（Ident/Parenthesized/FieldAccess/FuncCall）内，走 `impl Access for ast::Expr` 的 `_` 分支（[src/access.rs:21-25](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/access.rs#L21-L25)）报「临时值不可变」。因为数组**确实有** `first` 方法（只是字面量值没有存储位置），所以措辞是「临时值不可变」而非「无此方法」——这正是 `temp_or_missing` 要区分的（虽然本例的错误其实更早在 `Access` 层就产生了）。

**练习 2**：`call_method_mut` 返回 `Value`，`call_method_access` 返回 `&mut Value`。为什么两者返回类型不同？

**参考答案**：因为它们的用途不同。`call_method_mut` 服务于 `push`/`insert` 等「把数据塞进去、改完就完」的操作，调用方只需要知道成功与否（以及 `pop`/`remove` 取出的返回值），所以返回 `Value`。`call_method_access` 服务于 `first`/`at` 等「定位一个槽位」的操作，调用方（赋值语句）拿到这个槽位后还要往里写值，所以必须返回 `&mut Value`，让写入能落到真实存储位置上。

---

## 5. 综合实践

**任务**：设计一张「Typst 表达式可变性」速查表，并用源码验证每一行的归类。

请阅读 [src/access.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/access.rs)、[src/methods.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/methods.rs) 与 [src/call.rs:190-213](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L190-L213)，为下表每一行填上「能否成功修改」和「若失败，命中哪条报错」，并给出对应的源码依据：

| 表达式 | 能否就地修改？ | 命中的代码路径 / 报错 | 源码依据 |
|--------|----------------|----------------------|----------|
| `#let a=(1,2); #a.push(3)` | | | |
| `#let d=(k:1); #d.insert("m", 2)` | | | |
| `#let d=(k:1); #d.push(3)` | | | |
| `#let a=(1,2); #a.at(0) = 9` | | | |
| `#let a=(1,2); #a.first() += 1` | | | |
| `#(1,2).first() = 9` | | | |
| `#let s=sym.arrow; #s.l = sym.arrow.r` | | | |
| `#let x=10pt; #x.abs = 5pt` | | | |

**参考答案**（先自己填，再对照）：

1. `a.push(3)`：✅ 成功。`FuncCall::eval` → `is_mutating_method` → `maybe_resolve_mutating` → Array 分支 → `call_method_mut`（[methods.rs:36](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/methods.rs#L36)）。`a` 变 `(1,2,3)`。
2. `d.insert("m", 2)`：✅ 成功。同上但 target 是 Dict 且 `is_dict_mutating_method("insert")` 为真 → `call_method_mut` 字典分支（[methods.rs:50](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/methods.rs#L50)）。`d` 变 `(k:1, m:2)`。
3. `d.push(3)`：❌ 失败，报 `type dictionary has no method `push``。`maybe_resolve_mutating` 命中 Dict 退回分支（[call.rs:202-204](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/call.rs#L202-L204)）→ 常规分派 → 无此方法。
4. `a.at(0) = 9`：✅ 成功。`apply_assignment` → `FuncCall::access` → `call_method_access`（[methods.rs:86](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/methods.rs#L86)）→ `at_mut` → 写回。
5. `a.first() += 1`：✅ 成功。复合赋值走 `apply_assignment` 的 `op=ops::add`，`first()` 经 `call_method_access` 取 `first_mut`，再 `mem::take` 读旧值、`add` 后写回（[ops.rs:91-93](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/ops.rs#L91-L93)）。`a` 变 `(2,2)`。
6. `(1,2).first() = 9`：❌ 失败，报 `cannot mutate a temporary value`。数组字面量不在 `Access` 匹配集，命中 `_` 分支（[access.rs:21-25](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/access.rs#L21-L25)）。
7. `s.l = ...`：❌ 失败，报 `cannot mutate fields on symbol`。`access_dict` 分支 2（[access.rs:87-93](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/access.rs#L87-L93)）——symbol 有自定义只读 getter。
8. `x.abs = 5pt`：❌ 失败，报 `fields on length are not yet mutable` + hint。`access_dict` 分支 4（[access.rs:94-104](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/access.rs#L94-L104)）——length 有静态字段但无 setter。

完成这张表后，你就把本讲的「四类表达式 + 三种报错 + 两套内置方法」全部串起来了。

## 6. 本讲小结

- typst-eval 用独立的 `Access` trait（返回 `&mut Value`）补上 `Eval` trait（返回所有权 `Value`）无法覆盖的「就地修改」语义；只有 `Ident`/`Parenthesized`/`FieldAccess`/`FuncCall` 四类表达式有存储位置，其余报 `cannot mutate a temporary value`。
- 变更方法 `push`/`pop`/`insert`/`remove` 在 `FuncCall::eval` 一开始就被 `is_mutating_method` 拦截，转走 `maybe_resolve_mutating`：先求参数、再借 target，最终由 `call_method_mut` 就地改；非数组/字典的目标会「退回」常规路径以给出准确的方法名报错。
- 访问器方法 `first`/`last`/`at` 通过 `Access for FuncCall` + `call_method_access` 返回 `&mut Value`，从而能出现在赋值左侧（`a.at(0) = 9`），并由 `apply_assignment` 用 `std::mem::take` 完成「读—改—写」。
- `access_dict` 按字段来源分级报错：字典直接放行；symbol/content/module/func/args 因有自定义只读 getter 报「cannot mutate fields」；无字段类型报「does not have accessible fields」；有静态字段但无 setter 的类型（Length/Stroke 等）报「not yet mutable」并附重建 hint。
- 「先求参数、再借 target」的顺序在 `maybe_resolve_mutating` 和 `FuncCall::access` 中反复出现，根源是 Rust 的可变借用独占规则。

## 7. 下一步学习建议

本讲是「模块系统、样式规则与可变性」单元的最后一篇，可变性机制已讲透。接下来的学习建议：

1. **横向打通「改」的全景**：回顾 u3-l3（let 解构赋值的 `expr.access` 回调）、u3-l4（`apply_assignment` 的字段赋值特例）、u4-l2（字段读取 `access_field`），把本讲的 `Access` 放进「读 / 写 / 调用」三态对照表，形成完整的点号语义图。
2. **进入第 6 单元「深入机制」**：建议先读 [u6-l3 递归安全、栈增长与缓存](u6-l3-recursion-cache.md)，理解 `call_func` 为何要用 `stacker::maybe_grow` 保护调用栈——这与本讲「变更方法最终也走 `call_func`/方法调用」直接相关。
3. **源码延伸阅读**：若想了解 `Array`/`Dict` 的 `*_mut` 方法如何利用写时克隆（`make_mut`）实现「逻辑可变、物理共享」，可去 typst-library 阅读 [array.rs:104-119](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/array.rs#L104-L119) 与 dict.rs 的 `at_mut`/`insert`/`remove`，补全「就地修改」链路在运行时类型层的最后一公里。
