# 模块导入与包含

## 1. 本讲目标

本讲聚焦 `typst-eval/src/import.rs`，讲清 Typst 中两条「代码复用」语句——`import` 与 `include`——是如何被求值的。读完本讲你应当能够：

1. 说清 `import` 语句的求值总分发流程：先求值 source、再把字符串/路径「物化」成模块、最后按 `bare / wildcard / 具名 / as 重命名 / 嵌套` 五种方式把名字搬进当前作用域。
2. 追踪一次 `import "@preview/foo:0.1.0": bar` 从字符串到绑定 `bar` 的完整调用链：`import` → `import_package` → `resolve_package`（读 `typst.toml`）→ `import_file`（递归 `eval`）。
3. 解释 `route.contains` 为什么在 `import_file` 里是「用户错误」、在 `eval` 里却是「内部 panic」。
4. 区分 `import`（搬「名字/作用域」，产出 `Value::None`）与 `include`（搬「排版内容」，产出 `Content`）的本质差异。

本讲依赖前置讲义 u1-l3（`eval` 入口与 `Route`/`Engine`/`Sink`）与 u4-l1（`Eval` trait 的分发模式、`Vm` 状态）。

## 2. 前置知识

在进入源码前，先用通俗语言建立三个直觉。

**直觉一：模块（Module）是「带名字的作用域 + 一段内容」。**
Typst 求值一个 `.typ` 文件，得到的不是一个值，而是一个 `Module`。`Module` 内部装了两样东西：一个 `Scope`（文件顶层所有 `let` 绑定的名字）和一段 `Content`（文件 markup 求值后排版出来的内容）。`import` 取走的是前者（名字），`include` 取走的是后者（内容）。这正是两条语句最根本的区别。

**直觉二：「导入源」可以是好几种东西。**
`import` 后面跟的 source 不一定是字符串路径。它可以是：

| source 形式 | 例子 | 求值后类型 |
| --- | --- | --- |
| 字符串路径 | `import "utils.typ"` | `Value::Str` |
| 包规约 | `import "@preview/foo:0.1.0"` | `Value::Str`（同样以 `@` 开头） |
| 已有模块值 | `import some_mod` | `Value::Module` |
| 带 scope 的函数 | `import calc` | `Value::Func`（内置函数自带 scope） |
| 类型 | `import some_type` | `Value::Type` |
| 有根路径值 | `import some_path` | `RootedPath`（可 `cast`） |

求值器会用一个 `match` 把这些异构来源统一「规整」成一个 `Module`，再去搬名字。

**直觉三：导入会「递归求值」。**
导入一个文件，本质上是「把这个文件再 `eval` 一遍」。于是会自然产生两个问题：会不会无限递归？（A 文件 import B，B 又 import A）——由 `route.contains` 防护；会不会重复求值？（多次 import 同一文件）——由 `eval` 上的 `#[comemo::memoize]` 缓存防护。这两点本讲都会落到源码上。

> 名词速查：`FileId` 是「全局驻留（intern）的文件标识」，由 `RootedPath`（一个 `VirtualRoot` + `VirtualPath`）转换而来；`VirtualRoot` 只有两种——`Project`（项目根）或 `Package(PackageSpec)`（某个包）。`PackageSpec` 形如 `@preview/foo:0.1.0`，由 `namespace / name / version` 三段组成。

## 3. 本讲源码地图

本讲主要围绕 `typst-eval/src/import.rs`（约 285 行，5 个公开/私有函数 + 2 个 `Eval` 实现），并牵涉若干跨 crate 类型：

| 文件 | 作用 |
| --- | --- |
| `typst-eval/src/import.rs` | 本讲主角：`ModuleImport` / `ModuleInclude` 的 `Eval`，以及 `import` / `import_file` / `import_package` / `resolve_package` |
| `typst-eval/src/lib.rs` | `eval()` 入口：内含 `route.contains` 的「panic 版」循环防护与 `Route::extend(...).with_id(id)` |
| `typst-syntax/src/ast.rs` | `ModuleImport` AST 节点的访问器：`source()` / `imports()` / `bare_name()` / `new_name()`，以及 `BareImportError` 枚举 |
| `typst-syntax/src/package.rs` | `PackageManifest` / `PackageInfo` / `PackageSpec` 及 `manifest.validate()` |
| `typst-syntax/src/path.rs` | `RootedPath` / `VirtualRoot` / `VirtualPath` / `intern()` |
| `typst-library/src/foundations/path.rs` | `PathOrStr::resolve()` / `resolve_if_some()`：把相对路径字符串解析成 `RootedPath` |
| `typst-library/src/engine.rs` | `Route` 的 `extend` / `with_id` / `contains` / `track` |
| `typst-library/src/foundations/module.rs` | `Module::with_name` / `with_content` / `content` / `scope` |

> `lib.rs` 通过 [`pub use self::import::import;`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L19) 把 `import` 函数对外暴露；而 `import_file` / `import_package` / `resolve_package` 都是私有函数，仅在 crate 内部使用。

---

## 4. 核心概念与源码讲解

### 4.1 ModuleImport：导入语句的求值与绑定方式

#### 4.1.1 概念说明

`import` 语句的语法是 `import <source>[: <items>] [as <new_name>]`。它的求值要做三件事：

1. **规整 source**：把 source 表达式求值成某个 `Value`，如果是字符串/路径，就「物化」成一个 `Module`。
2. **处理整体重命名**：如果有 `as new_name`，把整个 source 绑定到 `new_name`。
3. **按导入形式搬名字**：根据 `: items` 的有无与形态，决定 bare / wildcard / 具名三种绑定方式之一。

这里有个关键设计：**bare 导入（不写 `: items`）要求名字能被静态确定**。因为如果不写 `: items`，求值器必须自己推断「该把整个模块绑定到哪个名字」。这个名字只能来自：标识符、字段访问、或字符串（取文件 stem / 包名）。任何「运行时才算得出来」的来源都拿不到静态名字，于是要求用户显式写 `as`。这就是 `BareImportError` 存在的原因。

#### 4.1.2 核心流程

`ModuleImport::eval` 的伪代码：

```
fn eval(self, vm):
    source_expr = self.source()
    source = source_expr.eval(vm)?            # ① 求值 source
    replaced_source = false

    match source:                              # ② 规整 source 成 Module
      Func 且无 scope  → 报错「不能从用户函数导入」
      Type / Module    → 原样使用
      Str(path)        → source = import(path); replaced_source = true
      可 cast 成 RootedPath → source = import_file(id); replaced_source = true
      其它             → 报错「期望 path/module/func/type」

    if let Some(new_name) = self.new_name():   # ③ as 重命名（整体绑定）
        (若 new_name == 原名 → 警告「无意义重命名」)
        vm.define(new_name, source.clone())

    scope = source.scope()
    match self.imports():
      None     → 若无 new_name：用 bare_name() 算名 → 绑定整个 source
      Wildcard → 把 scope 里所有 (var, binding) 复制进 vm.scopes.top
      Items    → 逐 item 沿 path 走进子模块，取 binding 绑定

    return Value::None
```

注意返回值是 `Value::None`：**`import` 语句本身不产出任何值/内容**，它的全部作用是「副作用」——往当前作用域里塞名字。

#### 4.1.3 源码精读

**第一步——求值并规整 source**：这段 `match` 把五种合法来源收敛成一个 `Module`（或先报错）。[`src/import.rs:L19-L58`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/import.rs#L19-L58) 即这段逻辑。其中字符串与 `RootedPath` 两条分支会调用 `import` / `import_file`（见 4.2），并把 `replaced_source` 置 `true`——这个标志在后面 bare 导入判定时有用。函数类型的特殊校验值得一看：只有「自带 scope 的函数」（即内置/原生函数）才能被 import，用户定义的函数没有 scope，直接 `bail!`：

```rust
Value::Func(func) => {
    if func.scope().is_none() {
        bail!(source_span, "cannot import from user-defined functions");
    }
}
```

[文件:src/import.rs:L26-L30](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/import.rs#L26-L30) ——「不能从用户函数导入」。

**第二步——`as` 整体重命名**：[文件:src/import.rs:L60-L75](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/import.rs#L60-L75)。这里有一个温和的 lint：`import x as x` 这种「重命名成原名」会被警告，但仍照常绑定。

**第三步——三种绑定方式**：[`self.imports()`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/import.rs#L78-L178) 的三分支。其中最值得读的是 **Items 分支的嵌套导入**：导入路径可以是 `a.b.c`，求值器会逐段 `scope.get(component)` 走进子模块，只有最后一段才真正绑定，中间段必须是 `Module/Func/Type`，否则报错：

```rust
while let Some(component) = &path.next() {
    let Some(binding) = scope.get(component) else {
        errors.push(error!(component.span(), "unresolved import"));
        break;
    };
    if path.peek().is_some() {                 // 中间段：必须是子模块
        let value = binding.read();
        let Some(submodule) = value.scope() else { /* 报错 */ };
        scope = submodule;                      // 走进去
    } else {                                    // 最后一段：绑定
        vm.bind(item.bound_name(), binding.clone());
    }
}
```

[文件:src/import.rs:L113-L177](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/import.rs#L113-L177)。注意多个 item 的错误会被收集到 `errors` 向量里，最后一次性 `return Err(errors)`——这是 typst-eval 常见的「尽量多报错」模式。

**bare 导入与 BareImportError**：当 `imports()` 为 `None` 且没有 `as` 时，走 [文件:src/import.rs:L79-L107](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/import.rs#L79-L107)。它调用 `self.bare_name()` 拿名字，而 `bare_name()` 的实现在 AST 侧：

```rust
pub fn bare_name(self) -> Result<EcoString, BareImportError> {
    match self.source() {
        Expr::Ident(ident) => Ok(ident.get().clone()),          // 标识符
        Expr::FieldAccess(access) => Ok(access.field().get()..),// 字段访问
        Expr::Str(string) => { /* 取包名或文件 stem，再校验是合法标识符 */ }
        _ => Err(BareImportError::Dynamic),                      // 运行时才算得出 → 拒绝
    }
}
```

[文件:typst-syntax/src/ast.rs:L2502-L2528](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/ast.rs#L2502-L2528)。`BareImportError` 有三个变体 [`ast.rs:L2542-L2550`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/ast.rs#L2542-L2550)：`Dynamic`（拿不到静态名字）、`PathInvalid`（文件 stem 不是合法标识符）、`PackageInvalid`（包规约非法）。

回到求值侧的守卫，它把「能确定名字」的条件表达得很精确：

```rust
Ok(name) if !replaced_source || matches!(source_expr, ast::Expr::Str(_)) => {
    if matches!(source_expr, ast::Expr::Ident(_)) {
        vm.engine.sink.warn(warning!(source_expr.span(), "this import has no effect"));
    }
    vm.scopes.top.bind(name, Binding::new(source, source_span));
}
Ok(_) | Err(BareImportError::Dynamic) => bail!(… "dynamic import requires an explicit name"; hint: …),
Err(BareImportError::PathInvalid) => bail!(… "module name would not be a valid identifier"; hint: …),
Err(BareImportError::PackageInvalid) => unreachable!(),
```

[文件:src/import.rs:L83-L105](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/import.rs#L83-L105)。这条守卫的含义是：**只有当 source 是字符串字面量（`"x.typ"` / `"@pkg"`），或 source 根本没被「物化」（即它本来就是个标识符/字段访问指向的 Module/Func/Type）时，bare 名字才可信**。换句话说，`import some_runtime_path_value`（一个 `RootedPath` 值，被物化过，但又不是字符串字面量）拿不到可信名字，于是落进 `Ok(_) | Err(Dynamic)` 分支，要求用户写 `as`。对纯标识符（如 `import calc`）还会额外警告「这次导入没有效果」——因为名字没变、等于白导入。

#### 4.1.4 代码实践

**实践目标**：用源码阅读验证 `bare_name()` 对不同 source 的判定，不运行也 能预测结果。

**操作步骤**：

1. 打开 [typst-syntax/src/ast.rs:L2502-L2528](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/ast.rs#L2502-L2528) 的 `bare_name()`。
2. 对下面 5 条语句，先自己预测 `bare_name()` 返回什么、求值侧会走哪个分支，再对照源码核对。

| 语句 | bare_name() 返回 | 求值侧行为 |
| --- | --- | --- |
| `import "utils.typ"` | `Ok("utils")` | 字符串字面量 → 允许，绑定 `utils` |
| `import "@preview/foo:0.1.0"` | `Ok("foo")`（取包名） | 字符串字面量 → 允许，绑定 `foo` |
| `import "my utils.typ"` | `Err(PathInvalid)`（文件 stem 含空格不是标识符） | 报「module name would not be a valid identifier」 |
| `import calc` | `Ok("calc")` | 允许 + 警告「this import has no effect」 |
| `import (if cond { a } else { b })` | `Err(Dynamic)` | 报「dynamic import requires an explicit name」 |

**需要观察的现象**：第 5 条之所以被拒，是因为括号表达式不在 `Ident/FieldAccess/Str` 之列，直接落入 `_ => Err(Dynamic)`。

**预期结果**：理解了「bare 导入需要静态可确定的名字」这条核心约束。

#### 4.1.5 小练习与答案

**练习 1**：`import "@preview/foo:0.1.0": bar` 中，`bar` 是通过哪种绑定方式进入作用域的？走的是 `imports()` 的哪个分支？

> **答案**：走 `Some(Imports::Items(items))` 分支。item 的 path 只有一段 `bar`，`path.peek()` 为 `None`（是最后一段），于是直接 `vm.bind(item.bound_name(), binding.clone())`，把包模块 scope 里的 `bar` 绑定到当前作用域。

**练习 2**：为什么 `import "my file.typ"`（文件名含空格）会被拒绝？错误来自 `BareImportError` 的哪个变体？

> **答案**：`bare_name()` 取文件 stem `"my file"`，再用 `is_ident(&name)` 校验失败（空格不是合法标识符字符），返回 `Err(BareImportError::PathInvalid)`。求值侧据此报「module name would not be a valid identifier」并提示用 `as` 重命名。

**练习 3**：`import *` 形式不存在，那 `import "x.typ": *` 是怎么实现「全量导入」的？

> **答案**：走 `Some(Imports::Wildcard)` 分支，遍历 `scope.iter()`，把模块 scope 里的每一个 `(var, binding)` 用 `vm.scopes.top.bind(var.clone(), binding.clone())` 复制进当前作用域。见 [src/import.rs:L108-L112](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/import.rs#L108-L112)。

---

### 4.2 import 与 import_file：从字符串/路径到模块

#### 4.2.1 概念说明

4.1 里 `ModuleImport::eval` 调用的 `import()` 与 `import_file()`，是把「字符串/路径」真正变成 `Module` 的核心函数。它们的职责分层很清晰：

- `import(engine, from: &str, span)`：**分派器**。只看字符串首字符——以 `@` 开头当包处理，否则当相对文件路径处理。
- `import_file(engine, id: FileId, span)`：**真正干活的人**。加载源文件、查循环、递归调用 `eval`。

为什么要把「分派」和「求值」拆开？因为 `import` 既能被 `ModuleImport` 调用，也能被 `ModuleInclude` 调用（两者都要先把字符串变成模块），复用同一段分派逻辑更清晰。

#### 4.2.2 核心流程

```
fn import(engine, from, span):            # 分派器
    if from.starts_with('@'):
        spec = from.parse::<PackageSpec>()?    # @preview/foo:0.1.0
        return import_package(engine, spec, span)   # 见 4.3
    else:
        path = PathOrStr::Str(from).resolve_if_some(span.id())?  # 相对当前文件
        return import_file(engine, path.intern(), span)

fn import_file(engine, id, span):         # 求值一个文件
    source = engine.world.source(id)?         # ① 向 World 要 Source（解析在此触发）
    if engine.route.contains(source.id()):    # ② 循环防护（用户错误）
        bail!(span, "cyclic import")
    eval(world, library, traced, sink, route.track(), &source)  # ③ 递归求值
```

两个细节：第一，`resolve_if_some(span.id())` 把相对路径字符串（如 `"utils.typ"`）相对于「当前文件所在目录」解析成 `RootedPath`，再 `.intern()` 成 `FileId`——见 [`PathOrStr::resolve`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/path.rs#L193-L207)。第二，`engine.world.source(id)` 这一步才真正触发对该文件的**解析**（词法+语法），所以「解析」也是惰性的、按需发生的。

#### 4.2.3 源码精读

**`import` 分派器**：[文件:src/import.rs:L212-L223](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/import.rs#L212-L223)。注意包分支与文件分支的差别：包走 `import_package`（它会先读 `typst.toml`），文件走 `PathOrStr::Str(...).resolve_if_some(...)`。`span.id()` 返回当前文件（调用 import 的那个文件）的 `FileId`，作为相对路径解析的基准；若为 `None`（比如在某些无文件上下文里调用），`resolve_if_some` 会返回「cannot access file system from here」错误。

**`import_file` 三步**：[文件:src/import.rs:L227-L245](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/import.rs#L227-L245)。最关键的是第三步——它把当前 `engine` 持有的所有句柄（`world`、`library`、`traced`、`sink`、`route`）原样传给 `eval`，让被导入文件在「同一个世界、同一个 sink、同一条 route」下求值。其中 `TrackedMut::reborrow_mut(&mut engine.sink)` 把可变 sink 句柄「再借出」给子调用，使子文件的警告能汇总到同一个 sink；`engine.route.track()` 则把 route 传下去（`eval` 内部会用 `Route::extend(route).with_id(id)` 给这条 route 追加一段）。循环防护（第二步）详见 4.5。

#### 4.2.4 代码实践

**实践目标**：跟踪一次相对文件导入 `import "utils.typ"` 中字符串如何变成 `FileId`。

**操作步骤**：

1. 在 `import()`（[src/import.rs:L217-L221](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/import.rs#L217-L221)）看到 `PathOrStr::Str(from.into()).resolve_if_some(span.id())`。
2. 打开 [`PathOrStr::resolve`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/path.rs#L193-L207)：它取 `within.root()`（与当前文件同根）和 `within.vpath().parent()`（当前文件所在目录），把 `"utils.typ"` join 上去得到新 `VirtualPath`，再 `RootedPath::new(root, resolved)`。
3. 最后 `.intern()`（[RootedPath::intern](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/path.rs#L32-L34)）通过全局 interner 把 `RootedPath` 转成轻量、可比较的 `FileId`。

**需要观察的现象**：相对路径的「基准」始终是**当前文件的目录**，而不是工作目录。这就是为什么同一份 `.typ` 在不同机器上导入行为一致。

**预期结果**：能解释「为什么 Typst 的相对 import 与当前文件位置绑定」。若要在本机验证，可在一个小项目里把 `main.typ` 和 `utils.typ` 放同一目录，写 `import "utils.typ"`，确认能解析（实际运行「待本地验证」）。

#### 4.2.5 小练习与答案

**练习 1**：`import()` 如何区分「包」和「相对文件」？依据是什么？

> **答案**：看字符串首字符是否为 `@`。`from.starts_with('@')` 为真则 `parse::<PackageSpec>()` 走包分支，否则走相对文件分支。见 [src/import.rs:L213-L222](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/import.rs#L213-L222)。

**练习 2**：`import_file` 传给 `eval` 的五个参数里，哪一个用来「收集子文件的警告」？为什么用 `reborrow_mut`？

> **答案**：`TrackedMut::reborrow_mut(&mut engine.sink)`。因为 `engine.sink` 是当前 engine 持有的可变句柄，`reborrow_mut` 在不转移所有权的前提下，临时把可写权限借给子 `eval`，使被导入文件的警告汇总进同一个 sink。

---

### 4.3 import_package 与 resolve_package：包清单与入口解析

#### 4.3.1 概念说明

导入一个包（`@preview/foo:0.1.0`）比导入一个本地文件多一步：**要读这个包的 `typst.toml` 清单**。清单声明了包的 `name`、`version`、`entrypoint`（入口文件相对路径）等信息。`resolve_package` 的职责就是「读清单 → 校验 → 算出入口 `FileId`」，`import_package` 再用这个入口走 `import_file`，并把结果模块重命名为包名。

为什么要校验清单？因为 `@preview/foo:0.1.0` 这个字符串里的 `foo` / `0.1.0` 是「用户想要」的名字和版本，而包里 `typst.toml` 写的才是「包实际声明」的名字和版本。二者必须一致，否则说明包内容与标识不符。此外清单可以要求最低编译器版本（`compiler` 字段），也需要在此检查。

#### 4.3.2 核心流程

```
fn import_package(engine, spec, span):
    (name, id) = resolve_package(engine, spec, span)?   # 读清单 → 入口
    import_file(engine, id, span)                       # 递归 eval 入口
        .map(|module| module.with_name(name))           # 重命名为包名

fn resolve_package(engine, spec, span):
    manifest_id = RootedPath(Package(spec) root, "typst.toml")  # 包根下的清单
    bytes  = engine.world.file(manifest_id)?            # 读清单字节
    string = bytes.as_str()?
    manifest = toml::from_str(string)?                  # 解析 TOML
    manifest.validate(&spec)?                           # 校验 name/version/compiler
    entry_id = PathOrStr::Str(entrypoint).resolve(manifest_id).intern()  # 入口 FileId
    return (manifest.package.name, entry_id)
```

两个要点：清单的 `FileId` 用 `VirtualRoot::Package(spec)` 作为根（这样包内所有路径都「关」在这个包的沙箱里，无法逃逸到项目根）；入口 `entrypoint`（如 `"src/lib.typ"`）是相对于**清单位置**（即包根）解析的。

#### 4.3.3 源码精读

**`import_package`**：[文件:src/import.rs:L248-L255](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/import.rs#L248-L255)。注意末尾的 `.with_name(name)`——`eval` 内部给模块起的名字是「入口文件的 stem」（如 `lib`），但对包而言这个名字对外没意义，所以这里覆盖成清单里的包名（如 `foo`）。`Module::with_name` 见 [module.rs:L93-L96](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/module.rs#L93-L96)。

**`resolve_package`**：[文件:src/import.rs:L258-L284](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/import.rs#L258-L284)。清单的 `RootedPath` 构造很有代表性：

```rust
let manifest_id = RootedPath::new(
    VirtualRoot::Package(spec.clone()),        // 根：这个包
    VirtualPath::new("typst.toml").unwrap(),   // 路径：包根下的 typst.toml
).intern();
```

这把清单「钉」在包根上。随后 `engine.world.file(manifest_id)` 向 World 请求该文件字节——实际的包下载/缓存发生在 World 实现里（如 `typst-kit` / `typst-cli`），`typst-eval` 只管「我要这个文件」。

**清单解析与校验**：

```rust
let manifest: PackageManifest = toml::from_str(string)
    .map_err(|err| eco_format!("package manifest is malformed ({})", err.message()))
    .at(span)?;
manifest.validate(&spec).at(span)?;
```

[文件:src/import.rs:L271-L274](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/import.rs#L271-L274)。`PackageManifest` 由 `serde` + `toml` 反序列化得到，结构见 [`package.rs:L22-L34`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/package.rs#L22-L34)，其中 `[package]` 段对应 `PackageInfo`（含 `name` / `version` / `entrypoint`，见 [`package.rs:L100-L143`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/package.rs#L100-L143)）。

**`manifest.validate(&spec)` 做了三件事**：

```rust
pub fn validate(&self, spec: &PackageSpec) -> Result<(), EcoString> {
    if self.package.name != spec.name { return Err("mismatched name"); }
    if self.package.version != spec.version { return Err("mismatched version"); }
    if let Some(required) = self.package.compiler {
        if !current.matches_ge(&required) { return Err("requires newer Typst"); }
    }
    Ok(())
}
```

[文件:typst-syntax/src/package.rs:L157-L183](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/package.rs#L157-L183)。即：清单名 == 用户请求名、清单版本 == 用户请求版本、当前编译器版本 ≥ 清单要求版本。三者全过才返回入口 `FileId`。

**入口解析**：

```rust
Ok((
    manifest.package.name,
    PathOrStr::Str(manifest.package.entrypoint.into())
        .resolve(manifest_id)      // 相对于清单（包根）解析
        .at(span)?
        .intern(),
))
```

[文件:src/import.rs:L277-L283](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/import.rs#L277-L283)。`resolve(manifest_id)` 以清单的 `FileId` 为基准，把 `"src/lib.typ"` 解析成包内的 `RootedPath`，再 intern。

#### 4.3.4 代码实践

**实践目标**：端到端追踪 `import "@preview/foo:0.1.0": bar` 的包导入路径。

**操作步骤**（对照源码逐步标注）：

1. `ModuleImport::eval` 求值 source 字符串 → `Value::Str("@preview/foo:0.1.0")` → 命中 [`Value::Str(path)` 分支](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/import.rs#L33-L40)，调用 `import(engine, "@preview/foo:0.1.0", span)`，`replaced_source=true`。
2. [`import`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/import.rs#L212-L223) 见首字符 `@` → `from.parse::<PackageSpec>()` 得 `spec{namespace:preview, name:foo, version:0.1.0}` → 调 `import_package`。
3. [`import_package`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/import.rs#L248-L255) 先 `resolve_package` 读 `typst.toml`，校验 name=foo、version=0.1.0、compiler，得入口 `FileId`。
4. `import_package` 调 `import_file(entry_id)` → `engine.world.source(id)` 触发解析 → `route.contains` 查循环 → 递归 `eval(...)` 求值 `src/lib.typ` 得 `Module`。
5. `.map(|module| module.with_name("foo"))` 把模块名从 `lib` 改成 `foo`。
6. 回到 `ModuleImport::eval`，source 已是 `Value::Module`；`imports()` 为 `Some(Items([bar]))`，走 [Items 分支](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/import.rs#L113-L177)，从模块 scope 取出 `bar` 绑定到当前作用域。

**需要观察的现象**：第 4 步的 `route.contains` 与第 5 步的 `with_name` 是包导入特有的两处「包语义」——前者防循环，后者改名。实际下载发生在第 3 步 `world.file(...)` 内部（由 World 实现，如 `typst-kit` 的包缓存）。

**预期结果**：能画出从字符串到 `bar` 绑定的完整调用栈。「实际下载包」这一步依赖具体 World，运行验证「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：如果 `typst.toml` 里 `name = "bar"` 但用户写 `import "@preview/foo:0.1.0"`，会发生什么？

> **答案**：`manifest.validate(&spec)` 检测到 `manifest.package.name("bar") != spec.name("foo")`，返回 `Err("package manifest contains mismatched name `bar`")`，经 `.at(span)` 贴到 import 语句的 span 上报给用户。见 [package.rs:L157-L163](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-syntax/src/package.rs#L157-L163)。

**练习 2**：为什么导入包得到的模块名是 `foo` 而不是入口文件名 `lib`？

> **答案**：`eval` 内部用 `id.vpath().file_stem()` 给模块起名（`lib`），但 [`import_package`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/import.rs#L254) 随后用清单里的 `manifest.package.name` 通过 `.with_name(name)` 覆盖成 `foo`，因为对用户而言包的「身份名」是包名而非入口文件名。

**练习 3**：清单 `typst.toml` 的 `FileId` 是怎么构造的？它的根是什么？

> **答案**：`RootedPath::new(VirtualRoot::Package(spec.clone()), VirtualPath::new("typst.toml"))`。根是 `Package(spec)`，意味着清单（以及包内所有文件）都「关」在这个包的虚拟根里，路径无法逃逸到项目根。见 [src/import.rs:L264-L268](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/import.rs#L264-L268)。

---

### 4.4 ModuleInclude：内容包含，以及与 import 的本质区别

#### 4.4.1 概念说明

`include "chapter.typ"` 与 `import "chapter.typ"` 长得很像，但语义截然不同：

- **`import`** 搬的是「名字」。它把目标模块的 `Scope`（顶层绑定）里的名字搬进当前作用域，语句本身**不产生任何内容**（返回 `Value::None`）。用于复用函数、变量、子模块。
- **`include`** 搬的是「内容」。它把目标模块求值后排版出的 `Content`（即文件 markup 的输出）**就地插入**到当前位置。用于把一份文档拆成多个文件再拼起来。

一句话区分：`import` 让你**调用**目标文件里的东西，`include` 让你**看到**目标文件排出来的东西。

#### 4.4.2 核心流程

`ModuleInclude::eval` 比 `ModuleImport::eval` 简单得多，因为它不需要搬名字：

```
fn eval(self, vm) -> Content:
    source = self.source().eval(vm)?
    module = match source:
      Str(path)        → import(engine, path, span)     # 复用 import 分派器
      Module(module)   → module                          # 已是模块，直接用
      可 cast 成 RootedPath → import_file(engine, id, span)
      其它             → 报错「期望 path 或 module」
    return module.content()                              # 取出 Content 返回
```

注意：`include` 不接受 `Func` / `Type` 作为 source（`import` 接受），因为它们没有「排版内容」可言。`include` 的合法来源只有「路径/字符串」和「模块」。

#### 4.4.3 源码精读

[文件:src/import.rs:L184-L209](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/import.rs#L184-L209) 是完整实现。两点值得注意：

第一，**返回类型是 `Content`**（`type Output = Content`），而 `ModuleImport` 是 `Value`。这从类型上就体现了「include 产出内容、import 产出空」的差别。

第二，**复用 `import` / `import_file`**：字符串与路径分支调用的函数和 `ModuleImport` 完全一样，只是包了一层 `Tracepoint::Include`（用于错误追踪时显示「include ...」而非「import ...」），最后 `.content()` 取内容。`Module::content` 见 [module.rs:L150-L153](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/module.rs#L150-L153)——它尝试 `Arc::try_unwrap` 拿走内部 content，拿不走就 clone。

> 一个微妙之处：`include` 求出的 `Content` 是该模块**求值时**就排好的内容（此时还没有真正「排版」，只是 markup 求值出的 `Content` 树）。真正的排版发生在更后端的 layout 阶段。`include` 只负责把这段 `Content` 树拼进当前 markup 流。

#### 4.4.4 代码实践

**实践目标**：通过对比两段等价写法，直观体会 import 与 include 的差别。

**操作步骤**：

1. 设想 `chapter.typ` 内容为 `#let title = "Intro" = Heading`（即定义了 `title` 绑定，又排了一个标题）。
2. 对照下表预测两种用法的可见效果：

| 用法（在 `main.typ` 里） | `title` 是否可用？ | 标题是否出现？ |
| --- | --- | --- |
| `import "chapter.typ"` | 是 | 否（import 不产出内容） |
| `import "chapter.typ": title` | 是 | 否 |
| `include "chapter.typ"` | 否（include 不搬名字） | 是 |
| `#include "chapter.typ"` | 否 | 是（等价于上一行） |

3. 阅读源码核对：`ModuleImport::eval` 返回 `Value::None`（[L180](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/import.rs#L180)），`ModuleInclude::eval` 返回 `module.content()`（[L207](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/import.rs#L207)）。

**需要观察的现象**：`import` 既不输出内容、`include` 也不搬名字，两者各取所需。

**预期结果**：从源码层面说清「为什么 `import` 后看不到 chapter 的标题、`include` 后用不了 `title`」。运行验证「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `include calc`（`calc` 是内置函数模块）会报错，而 `import calc` 可以？

> **答案**：`ModuleInclude` 的 match 没有 `Value::Func` 分支，函数值会落入最后的 `v => bail!("expected path or module, found {}", v.ty())`。函数没有「排版内容」，include 它没有意义。见 [src/import.rs:L190-L205](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/import.rs#L190-L205)。

**练习 2**：`include "a.typ"` 和 `import "a.typ"` 都会触发对 `a.typ` 的求值吗？求值结果有何不同用途？

> **答案**：都会。两者都调用 `import`/`import_file` → 递归 `eval` 得到同一个 `Module`。`import` 用模块的 `.scope()`（搬名字），`include` 用模块的 `.content()`（取内容）。

---

### 4.5 route 循环防护：两道关卡、两种严重程度

#### 4.5.1 概念说明

因为导入会递归求值，就必须防止循环：A 导入 B、B 又导入 A，否则会无限递归。`typst-eval` 用 `Route`（前置讲义 u1-l3 已介绍）记录「当前求值调用链上都有哪些文件」。防护其实有**两道关卡**，而且严重程度不同——这是本讲最容易混淆、也最值得弄清的点：

1. **`import_file` 里的关卡**（[src/import.rs:L232-L234](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/import.rs#L232-L234)）：`if engine.route.contains(source.id()) { bail!(span, "cyclic import") }` —— 这是**用户错误**，返回友好的 `Err`。
2. **`eval` 里的关卡**（[lib.rs:L50-L52](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L50-L52)）：`if route.contains(id) { panic!(...) }` —— 这是**内部错误**，直接 panic。

为什么一个是 `bail!`、一个是 `panic!`？因为正常的导入流程总会先经过 `import_file` 的检查，循环会在那里被拦下并报给用户。`eval` 是 `#[comemo::memoize]` 的公共入口，可能被其他路径直接调用（如 CLI 编译主文件、或别的 crate）；如果某条路径绕过了 `import_file` 的检查却仍把一个已在 route 中的文件送进 `eval`，那说明调用方逻辑有 bug，属于不该发生的情况，因此用 `panic!` 表达「这是编译器内部错误」。

#### 4.5.2 核心流程

`Route` 是一条「沿调用链」的链表。每求值一个文件，`eval` 会通过 `Route::extend(route).with_id(id)` 给 route 追加一段并记下当前文件 id：

```
eval(X):
    if route.contains(X): panic              # 内部防线
    engine.route = Route::extend(route).with_id(X)   # 把 X 记入 route
    ... 求值 X ...
        遇到 import "Y":
            import_file(Y):
                if route.contains(Y): bail "cyclic import"   # 用户防线
                eval(Y):    # Y 进入 route，链上现在有 ...→X→Y
                    遇到 import "X":
                        import_file(X):
                            route.contains(X) == true  → bail!   # 拦下循环
```

`route.contains(id)` 的实现是递归沿 `outer` 链查找：[engine.rs:L400-L402](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L400-L402)。

```rust
pub fn contains(&self, id: FileId) -> bool {
    self.id == Some(id) || self.outer.is_some_and(|outer| outer.contains(id))
}
```

#### 4.5.3 源码精读

**`import_file` 的用户防线**：[文件:src/import.rs:L227-L245](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/import.rs#L227-L245)，重点是 L232-L234：

```rust
// Prevent cyclic importing.
if engine.route.contains(source.id()) {
    bail!(span, "cyclic import");
}
```

注意它检查在 `eval` **之前**：先确认目标文件不在当前 route 上，再递归求值。`span` 是 import 语句的 span，所以错误定位精确指向那条「造成循环」的 import。

**`eval` 的内部防线**：[文件:lib.rs:L40-L52](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L40-L52)：

```rust
let id = source.id();
if route.contains(id) {
    panic!("Tried to cyclically evaluate {:?}", id.vpath());
}
```

随后 `Route::extend(route).with_id(id)`（[lib.rs:L62](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L62)）把当前文件 id 记入 route，供深层 import 检查。

**`Route::extend` / `with_id`**：[engine.rs:L295-L307](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L295-L307)。`extend` 把外层 route 作为 `outer` 存起来、`len` 置 1（用于深度计数）；`with_id` 把当前文件 id 钉到这一段上。`Route` 结构体定义见 [engine.rs:L258-L281](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L258-L281)，核心字段是 `outer`（父链）、`id`（本段文件）、`len`（本段深度贡献）。

> 补充：`Route` 还兼任**调用深度限制**——`check_call_depth`（上限 `MAX_CALL_DEPTH = 80`，[engine.rs:L388-L393](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L388-L393)）。注意它防的是「函数调用嵌套过深」（逻辑深度），与 `contains` 防的「循环求值」（拓扑环）是两个不同问题。函数调用深度的防护在 u4-l1（`FuncCall`）与 u6-l3 讲，本讲只关注 `contains` 的循环防护。

#### 4.5.4 代码实践

**实践目标**：通过阅读源码，说清两道关卡的分工与严重程度差异。

**操作步骤**：

1. 读 [`import_file` 的检查](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/import.rs#L231-L234)与 [`eval` 的检查](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/lib.rs#L49-L52)。
2. 在纸上推演「A 导入 B、B 导入 A」的执行：`eval(A)` → route=[A] → `import_file(B)`：`contains(B)`=false → `eval(B)` → route=[A,B] → `import_file(A)`：`contains(A)`=**true** → `bail!("cyclic import")`，错误带 B 里那条 import 的 span。

**需要观察的现象**：循环在 `import_file`（用户防线）就被拦下，根本到不了 `eval` 的 panic。因此用户写循环导入只会看到友好的 `cyclic import` 错误，而非崩溃。

**预期结果**：能解释「为什么 `eval` 里用 `panic!` 而 `import_file` 里用 `bail!`」。若要本机验证，可建两个互相 import 的 `.typ` 文件编译，观察报错信息（「待本地验证」）。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `eval` 里的循环检查用 `panic!` 而不是 `bail!`？

> **答案**：正常导入流程下，循环会在 `import_file` 的 `bail!("cyclic import")` 处被拦下（用户错误）。`eval` 的检查是「内部不变量」防线——若它被触发，说明某条调用路径绕过了 `import_file` 的检查却仍把已在 route 中的文件送进 `eval`，属于编译器逻辑 bug，故用 `panic!` 表示内部错误。

**练习 2**：`route.contains(id)` 是如何沿调用链查找的？时间复杂度如何？

> **答案**：递归比较本段 `id`，不匹配则沿 `outer` 链继续查（[engine.rs:L400-L402](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/engine.rs#L400-L402)）。复杂度与调用链深度成正比，O(链长)。由于 `Route` 用 `Tracked` 句柄在 comemo 缓存间传递，查找代价与缓存机制配合得较经济。

**练习 3**：`Route::check_call_depth` 与 `Route::contains` 各阻止什么问题？

> **答案**：`contains` 阻止**循环求值/循环导入**（拓扑上的环，A→B→A）；`check_call_depth`（上限 80）阻止**函数调用嵌套过深**（如递归函数调用过深，即便不构成环）。前者关心「同一文件是否重复出现在链上」，后者关心「链有多长」。

---

## 5. 综合实践

把本讲的知识串起来，完成下面的「源码阅读 + 推演」综合任务。

**任务**：给定如下两个文件，推演它们的求值过程，并回答问题。

`main.typ`：
```typst
#import "utils.typ": helper
#import "@preview/foo:0.1.0": bar
#helper()
#include "chapter.typ"
```

`utils.typ`：
```typst
#let helper = () => [Hello]
```

**要求**：

1. 画出 `main.typ` 求值时，三行 import/include 各自触发的函数调用链（用到 `ModuleImport::eval` / `import` / `import_file` / `import_package` / `resolve_package` / `eval` / `ModuleInclude::eval` / `module.content()`）。
2. 指出在哪一步会向 `engine.world` 请求文件、在哪一步触发解析、在哪一步触发递归求值。
3. 指出三行语句各自在 `main.typ` 作用域里留下了什么（绑定了哪些名字 / 插入了什么内容）。
4. 假设把 `utils.typ` 改成 `#import "main.typ": helper`（互相导入），推演会在哪一行代码、用 `bail!` 还是 `panic!` 报错。

**参考思路**（先自己推演再对照）：

- 第 1 行 `import "utils.typ": helper`：`ModuleImport::eval` → source 是 `Value::Str` → [`import`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/import.rs#L212-L223)（非 `@`，走文件分支）→ `resolve_if_some` 得 `FileId` → [`import_file`](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/import.rs#L227-L245)：`world.source(id)` 请求并解析 utils.typ → `route.contains` 查循环 → 递归 `eval` 求出 `Module` → 回到 Items 分支绑定 `helper`。
- 第 2 行包导入走 4.3.4 描述的完整链，多一步 `resolve_package` 读 `typst.toml`，并把模块 `with_name("foo")`。
- 第 3 行 `include`：`ModuleInclude::eval` → `import` → `import_file` → 递归 `eval` 得 `Module` → `.content()` 把 chapter 的内容插入当前 markup 流；它**不**在 `main.typ` 作用域留任何名字。
- 第 4 问：互相导入时，`eval(main)` route=[main] → import utils → `eval(utils)` route=[main,utils] → utils 里 `import "main.typ"` → `import_file(main)`：`route.contains(main)` = true → [bail!("cyclic import")](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-eval/src/import.rs#L232-L234)（用户错误，不是 panic）。

---

## 6. 本讲小结

- `import` 的求值分三步：求值并规整 source（字符串/路径会被 `import`/`import_file` 物化成 `Module`）→ 处理 `as` 整体重命名 → 按 `bare / wildcard / 具名` 三种方式搬名字；语句本身返回 `Value::None`。
- bare 导入要求名字**静态可确定**（标识符 / 字段访问 / 字符串），否则 `BareImportError::{Dynamic, PathInvalid}` 触发「需要显式 `as`」错误；`BareImportError::PackageInvalid` 在求值侧被标为 `unreachable!`（包规约非法早在解析阶段就失败）。
- `import` 是分派器（`@` 开头走包、否则走相对文件），`import_file` 是真正干活的人（向 World 要 Source、查循环、递归 `eval`）；二者被 `ModuleImport` 与 `ModuleInclude` 复用。
- 包导入多一步 `resolve_package`：读 `typst.toml`、`manifest.validate` 校验 name/version/compiler、以清单为基准解析 entrypoint，最后 `import_package` 把结果模块 `with_name` 成包名。
- `import` 搬「名字」（用 `.scope()`），`include` 搬「内容」（用 `.content()`，返回 `Content`）；这是两者最根本的区别，也体现在 `Output` 类型（`Value` vs `Content`）上。
- 循环防护有两道关卡：`import_file` 里 `bail!("cyclic import")` 是用户错误（正常循环在此被拦），`eval` 里 `panic!` 是内部不变量防线；`route.contains` 沿 `outer` 链递归查找。

## 7. 下一步学习建议

- **u5-l2（set/show 规则求值）**：本讲的 `import`/`include` 与 `set`/`show` 同属「语句级」求值，且都通过 `eval_code` 流式处理；理解了 import 的「副作用」语义后，再看 set/show 的「样式作用域」语义会更顺。
- **u6-l3（递归安全、栈增长与缓存）**：本讲提到的 `route.contains` 循环防护、`Route::extend` 调用链、以及 `eval` 的 `#[comemo::memoize]` 缓存，会在 u6-l3 系统性地与 `check_call_depth`、`stacker::maybe_grow` 一起讲清「三道运行时安全防线」。
- **延伸阅读**：若想了解「包实际是如何下载与缓存的」，可阅读 `typst-kit/src/files.rs` 与 `typst-cli/src/world.rs`——`typst-eval` 只通过 `engine.world.file(id)` 请求文件字节，真正的网络/磁盘IO发生在 World 实现里。
