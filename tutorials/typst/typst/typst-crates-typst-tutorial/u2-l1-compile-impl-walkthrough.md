# compile_impl 编译主流程精读

## 1. 本讲目标

在上一讲（u1-l3）里，我们只把公开入口 `compile` 当成一个「薄外壳」看了个大概：它建一个 `Sink`、把 `&dyn World` 转成 `Tracked<dyn World>`、再委托给一个名字里带 `impl` 的内部函数，然后就返回了。那个被委托的内部函数，就是本讲的主角——**`compile_impl`**。

读完本讲，你应当能够：

1. 跟踪 `compile_impl` 从 `world.library()` 到 `Ok(document)` 的**完整执行路径**，并能说出它大致分成哪几个阶段。
2. 理解「基础样式链 + `TargetElem::target`」是如何把「这次编译的目标是 PDF 还是 HTML」这件事，**注入到 Typst 的样式系统**里、从而让下游元素能感知到的。
3. 理解当主源码读取失败时，`hint_invalid_main_file` 是如何给出**对人类友好的提示**（比如「你大概是想写 `.typ` 吧」）的。
4. 理解稳定化循环结束后那一句「**提升延迟错误**」(promote delayed errors) 解决的是什么问题，为什么有些错误要等到最后一刻才决定是否致命。
5. 把 `typst_eval::eval` 的签名和它内部做了什么，跟 `compile_impl` 里那一处调用对上号。

> 本讲是「会读主流程」的进阶讲。内省稳定化循环的**收敛判定细节**（`constraint.validate`、`history`、`analyze`）属于 u2-l2 / u2-l3 的范围，本讲只看循环的「骨架」并把位置交代清楚。

## 2. 前置知识

本讲默认你已经掌握 u1-l3 的内容，这里只快速复习三个概念，不展开：

- **`SourceResult<T>`**：就是 `Result<T, EcoVec<SourceDiagnostic>>`。`Err` 表示「一批致命错误」，会让整次编译失败；`Ok` 表示成功产出。`?` 运算符遇到 `Err` 会提前 return。
- **`Sink`**：一个「只增容器」，编译过程中产生的告警（warning）、延迟错误（delayed error）、内省记录、追踪值都往里塞。`compile_impl` 拿到的是它的 `&mut Sink`。
- **`Output` trait**：编译产物的抽象，泛型参数 `T: Output` 决定这次编译要产出什么。它有三个关键关联函数：`target()` 告诉编译器「我是哪种目标」、`create()` 真正产出文档、`introspector()` 取出文档的内省器。

另外需要两个新概念铺垫：

- **样式链 `StyleChain`**：Typst 里「样式」不是全局变量，而是一条**可叠加的链**。底层是库的默认样式，上面再 chain 一层局部覆盖。`compile_impl` 里要构造的就是「默认样式 + 编译目标」这条最底层的链。
- **`comemo::Constraint`**：comemo 增量缓存框架里的一把「约束锁」。`compile_impl` 在循环里会用 `constraint.validate(...)` 判断「这一轮布局的产物，和我先前给出的内省信息，是否互相一致」——一致就说明稳定了。这句话现在看不懂没关系，本讲只用它来解释循环为什么会停；细节留给 u2-l2。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用到的部分 |
| --- | --- | --- |
| [`crates/typst/src/lib.rs`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs) | 本 crate 全部逻辑，主角 | `compile_impl` 函数体、`hint_invalid_main_file`、循环末尾的提升逻辑 |
| [`crates/typst-eval/src/lib.rs`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-eval/src/lib.rs) | 求值器入口 | `eval` 函数（`compile_impl` 唯一调用的「求值」步骤） |
| [`crates/typst-library/src/foundations/target.rs`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/target.rs) | 编译目标抽象 | `Output` trait、`Target` 枚举 |
| [`crates/typst-library/src/engine.rs`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs) | 中央上下文 | `Engine` 结构体、`Sink::delayed` / `extend_from_sink` |
| [`crates/typst-library/src/introspection/convergence.rs`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/introspection/convergence.rs) | 收敛常量 | `MAX_ITERS`、`ITER_NAMES` |
| [`crates/typst-library/src/introspection/introspector.rs`](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/introspection/introspector.rs) | 内省器 | `EmptyIntrospector` |

## 4. 核心概念与源码讲解

### 4.1 compile_impl 函数体全景与七阶段

#### 4.1.1 概念说明

`compile_impl` 是 `typst` crate 里**唯一真正干活的函数**。上一讲的 `compile` 只是它的一层包装；同文件的 `trace` 也复用它。它接收三个东西：

- `world`：被 comemo 追踪过的世界句柄 `Tracked<dyn World>`（只读地提供源码、字体、库等外部资源）；
- `traced`：被追踪的 span，决定是否要在求值时顺便收集某个表达式位置的值（`trace()` 用，普通编译传默认值）；
- `sink`：可变的告警/延迟错误/内省记录收集器。

它的返回值是 `SourceResult<T>`，即「要么成功产出 `T`（`PagedDocument` / `HtmlDocument`），要么一批致命错误」。

#### 4.1.2 核心流程

把 `compile_impl` 的函数体切成七个阶段，按执行顺序是：

```text
[1] 取库        world.library()
[2] 目标门控    match T::target() { Paged | Html | Bundle }  // 特性开关：报错还是只警告
[3] 装配样式链  base = 库默认样式; 再 chain 一层「编译目标」
[4] 取主源码    world.source(world.main())  // 失败则 hint_invalid_main_file
[5] 求值        typst_eval::eval(...).content()  // 源码 → Content
[6] 稳定化循环  反复 T::create(...) 直到 introspection 不再变化（≤ MAX_ITERS 轮）
[7] 提升延迟错误  sink.delayed() 非空 → 把它变成致命错误返回
```

其中 [1]–[5] 只跑一次；[6] 是一个 `loop`，会反复布局；[7] 在循环结束后才执行。一个关键直觉：**阶段 [2] 和 [4] 可能直接 `return Err(...)` 中断**，而 [5][6] 里的错误大多先收进 `sink`，只有「延迟错误」才会被 [7] 提升为致命。

#### 4.1.3 源码精读

先看函数签名与前三阶段：

`compile_impl` 的签名与 [1][2] 阶段——[crates/typst/src/lib.rs:99-109](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L99-L109)。这段先取库，再按 `T::target()` 分发：`Paged` 什么都不做；`Html` / `Bundle` 要看特性开关是否打开，决定是「报致命错」还是「只发一条警告继续」。

```rust
fn compile_impl<T: Output>(
    world: Tracked<dyn World + '_>,
    traced: Tracked<Traced>,
    sink: &mut Sink,
) -> SourceResult<T> {
    let library = world.library();
    match T::target() {
        Target::Paged => {}
        Target::Html => warn_or_error_for_html(&library.features, sink)?,
        Target::Bundle => warn_or_error_for_bundle(&library.features, sink)?,
    }
```

阶段 [3] 装配样式链——[crates/typst/src/lib.rs:111-114](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L111-L114)。`base` 是「库默认样式」这条底链；`target` 是一个只含「编译目标」字段的局部样式；最终 `styles = base.chain(&target)` 把两者串成一条链，后续布局都会看到它。

```rust
    let base = StyleChain::new(&library.styles);
    let target = TargetElem::target.set(T::target()).wrap();
    let styles = base.chain(&target);
    let empty_introspector = EmptyIntrospector;
```

> **「编译目标」如何注入样式系统？** Typst 的每个「编译目标」是一个普通的元素样式字段 `TargetElem::target`。`compile_impl` 用 `.set(T::target())` 把当前目标写进这个字段，再 `.wrap()` 包成一层 `Styles`，最后 chain 到默认样式上。这样，下游任何元素只要读 `TargetElem::target` 这条样式，就能知道自己是在为 PDF/Paged 还是 HTML 布局，从而走不同的分支。`Output` trait 与 `Target` 枚举的一一对应关系定义在 [crates/typst-library/src/foundations/target.rs:13-30](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/target.rs#L13-L30)。

关于阶段 [2] 的特性门控细节：`warn_or_error_for_html` 与 `warn_or_error_for_bundle` 各自检查对应的 `Feature` 是否启用——[crates/typst/src/lib.rs:247-286](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L247-L286)。启用时只 `sink.warn(...)` 一条「实验性，行为可能变化」的提示（**只产生 warning**）；未启用时 `bail!(...)` 直接 `return Err(...)`（**致命错误**）。这就是「同一个函数，因 feature 开关不同而要么警告要么报错」的来源，详情会在 u3-l4 展开。

#### 4.1.4 代码实践

**实践目标**：在脑子里把 `compile_impl` 的七阶段过一遍，并区分每一步「能不能让编译直接失败」。

**操作步骤**（源码阅读型）：

1. 打开 [crates/typst/src/lib.rs:99-194](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L99-L194)。
2. 用笔在纸上画出这七个阶段，每个阶段旁边标一个记号：
   - 🟥 = 可能直接 `return Err`（致命）
   - 🟨 = 只往 `sink` 里放 warning（不致命）
   - ⬜ = 一般不产生诊断
3. 对照参考答案（见 4.1.5）。

**需要观察的现象 / 预期结果**：你会发现致命路径其实很集中——主要在 [2] 门控的 `bail!`、[4] 取主源码失败、以及 [7] 提升延迟错误。中间的求值与布局更倾向于「先收进 sink」。

> 本实践为源码阅读型，无需运行命令；如要实跑可参考本讲「综合实践」。

#### 4.1.5 小练习与答案

**练习 1**：阶段 [3] 装配样式链时，为什么需要单独拿一个 `empty_introspector`？

**参考答案**：因为首轮布局时还没有任何「上一轮的产物」可供内省（计数器、页码等都还没生成）。`empty_introspector` 就是首轮迭代用的「空内省器」，保证第一轮 `T::create` 也能拿到一个合法的 introspector 句柄，不至于读到不存在的页面信息。

**练习 2**：如果把 `T::target()` 换成在运行时由 `world` 决定，会破坏什么？

**参考答案**：`T::target()` 是 `Output` trait 的关联函数，在编译期（Rust 编译期）就由泛型 `T` 决定，编译器据此做特性门控并选择 `T::create`。如果改成运行时由 world 决定，就必须在 `compile_impl` 内部用动态分发去匹配目标、且无法再靠泛型 `T` 选定产物类型，整个「`compile::<PagedDocument>` ↔ `compile::<HtmlDocument>`」的静态分派设计就会被打破。

---

### 4.2 取主源码与 hint_invalid_main_file 友好提示

#### 4.2.1 概念说明

阶段 [4] 要拿到「主文件」的源码。主文件由 `world.main()` 给出一个 `FileId`，再用 `world.source(id)` 取回 `Source`。这一步可能失败——比如文件不存在、是目录、编码不是 UTF-8。失败时，Typst 不满足于甩一句冷冰冰的 `FileError`，而是调用 `hint_invalid_main_file` **补上对人类有用的提示**（hint）。这是「编译器诊断友好度」的一个典型例子。

#### 4.2.2 核心流程

```text
world.main()                    // → FileId（主文件 id）
  └─ world.source(id)           // Result<Source, FileError>
        ├─ Ok(source)           // 继续往下走
        └─ Err(err)             // 调 hint_invalid_main_file(world, err, id)
              └─ 若是 UTF-8 错误：
                    ├─ 扩展名是 .typ     → 不加 hint（文件确实是坏的）
                    ├─ 有其他扩展名(如 .pdf) → 提示「.pdf 通常不是 Typst 文件」
                    └─ 若把扩展名改成 .typ 后能读到 → 提示「检查是否想用 .typ」
              └─ 返回 eco_vec![diagnostic]（作为致命错误）
```

#### 4.2.3 源码精读

阶段 [4] 的调用点——[crates/typst/src/lib.rs:116-120](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L116-L120)。注意 `?`：`world.source(main)` 返回 `Err` 时，`.map_err` 把原始 `FileError` 包成带提示的诊断，然后 `?` 立刻让 `compile_impl` `return Err(...)`。这是一条**致命路径**。

```rust
    let main = world.main();
    let main = world
        .source(main)
        .map_err(|err| hint_invalid_main_file(world, err, main))?;
```

`hint_invalid_main_file` 的实现——[crates/typst/src/lib.rs:208-244](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L208-L244)。它只在「UTF-8 解码失败」时才尝试给提示，并按主文件的扩展名分支处理：

```rust
    if is_utf8_error {
        match input.vpath().extension() {
            Some("typ") => return eco_vec![diagnostic],     // 已是 .typ，确实坏了
            Some(ext) => { /* 提示 .ext 通常不是 Typst 文件 */ }
            None => { /* 提示无扩展名通常不是 Typst 文件 */ }
        }
        if world.source(input.map(|p| p.with_extension("typ")).intern()).is_ok() {
            diagnostic.hint("check if you meant to use the `.typ` extension instead");
        }
    }
```

最巧妙的是最后一句：它**真的去 `world` 里试着把扩展名换成 `.typ` 再读一次**，如果能读到，就额外提示「你大概是想用 `.typ` 吧」。这正是「编译器替用户猜 typo」的实例。

#### 4.2.4 代码实践

**实践目标**：体会 `hint` 对最终用户提示的影响。

**操作步骤**（源码阅读型）：

1. 阅读 [crates/typst/src/lib.rs:208-244](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L208-L244)。
2. 假设宿主把 `main` 指向了一个 `report.pdf` 文件，回答：
   - `is_utf8_error` 大概率为真还是假？（PDF 是二进制，按 UTF-8 解会失败 → 真）
   - 最终用户会看到哪几条 hint？

**预期结果**：用户会先看到「a file with the `.pdf` extension is not usually a Typst file」；如果同目录恰好存在 `report.typ`，还会再看到「check if you meant to use the `.typ` extension instead」。两条提示合在一起，几乎就是在告诉用户「你把主文件名写错了」。

> 待本地验证：实际触发需要构造一个会把 `main` 设成 `.pdf` 的 World；本练习侧重读懂 hint 的拼装逻辑。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `hint_invalid_main_file` 对「扩展名是 `.typ`」的情况**不加**任何 hint？

**参考答案**：因为当文件本身就是 `.typ` 却仍然 UTF-8 解码失败时，问题就是「文件内容真的坏了」，没有更友好的猜测可给。此时直接返回原始诊断，避免给出误导性的提示（比如暗示用户改扩展名，但扩展名本来就对）。

**练习 2**：这一步产生的诊断，会走 `SourceResult::Err`（致命）还是只进 `sink` 的 warning？

**参考答案**：走致命路径。`map_err(...)?` 会让 `compile_impl` 直接 `return Err(eco_vec![diagnostic])`，因此取不到主源码是一次硬失败，不会被当成 warning。

---

### 4.3 typst_eval::eval 调用：从源码到 Content

#### 4.3.1 概念说明

阶段 [5] 是整条流水线里「解析 → 求值」交界的地方。在 `compile_impl` 里它只是一次函数调用，但背后是整个 `typst-eval` crate。本模块的目标是：**把这个调用点的参数对上号，并大致了解 `eval` 内部做了什么**——这样你既知道 `compile_impl` 在哪把活交出去，也知道接活的人长什么样。

#### 4.3.2 核心流程

`compile_impl` 这一侧（调用方）：

```text
typst_eval::eval(
    world,            // 同一个 world
    library,          // [1] 取到的库
    traced,           // trace 用
    sink.track_mut(), // 把自己的 sink 交给 eval 写告警/延迟错误
    Route::default().track(),  // 一条全新的、空的调用路由
    &main,            // 主源码
)?                     // 出错 → 致命 Err
  .content();          // 成功 → 取出模块里的 Content
```

`eval` 内部（被调用方）做的事，概括为：

```text
1. 循环求值检测：route.contains(id) → 防止源文件循环包含自己
2. 构造 Engine（首轮用 EmptyIntrospector，因为求值阶段还没有页面布局）
3. 构造虚拟机 Vm（作用域、上下文、根 span）
4. 先收集语法树自带的 errors/warnings；有语法错误且不在 trace 模式 → 提前 Err
5. 求值 markup → 得到 output（Content）
6. 处理控制流（return/break 等出现在不该出现的地方 → bail）
7. 组装成 Module（名字、顶层 scope、content、file id）返回
```

#### 4.3.3 源码精读

`compile_impl` 里的调用点——[crates/typst/src/lib.rs:122-131](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L122-L131)。`.content()` 把求值得到的 `Module` 里的「文档内容」取出来——`Module` 同时持有 scope（导出的值）和 content（要排版的内容），编译主流程只关心后者。

```rust
    let content = typst_eval::eval(
        world, library, traced, sink.track_mut(),
        Route::default().track(), &main,
    )?
    .content();
```

`eval` 的签名与首部——[crates/typst-eval/src/lib.rs:38-52](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-eval/src/lib.rs#L38-L52)。注意它带着 `#[comemo::memoize]`（相同输入可增量缓存）和 `#[typst_macros::time(...)]`（计时）。第一步就是**循环包含检测**：如果当前源文件的 id 已经在 `route` 里，说明出现了 `#include` 循环，直接 panic。

```rust
#[comemo::memoize]
#[typst_macros::time(name = "eval", span = source.root().span())]
pub fn eval(...) -> SourceResult<Module> {
    let id = source.id();
    if route.contains(id) {
        panic!("Tried to cyclicly evaluate {:?}", id.vpath());
    }
```

`eval` 构造 Engine 时用的也是 `EmptyIntrospector`——[crates/typst-eval/src/lib.rs:54-63](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-eval/src/lib.rs#L54-L63)。这是个关键点：**求值阶段拿不到真实的页面布局**（页面要等后面布局才有），所以这里只能给空内省器。这也是为什么很多依赖「页码/计数器最终值」的 show 规则，在求值早期会报错——而这些错误会被设计成「延迟错误」，留到本讲 4.5 处理。

`eval` 的产出是 `Module`——[crates/typst-eval/src/lib.rs:93-96](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-eval/src/lib.rs#L93-L96)。`compile_impl` 紧接着 `.content()` 取走其中的内容树。

#### 4.3.4 代码实践

**实践目标**：把「调用点」与「被调用者签名」逐参数对齐。

**操作步骤**（源码阅读型）：

1. 同时打开两个位置：
   - 调用方 [crates/typst/src/lib.rs:123-130](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L123-L130)
   - 被调用方 [crates/typst-eval/src/lib.rs:40-47](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-eval/src/lib.rs#L40-L47)
2. 列一张表，把调用方传的 6 个实参，与 `eval` 形参（`world / library / traced / sink / route / source`）一一对应。
3. 注意 `sink.track_mut()` 的类型是 `TrackedMut<Sink>`，正好匹配形参 `sink: TrackedMut<Sink>`——这就是为什么 `eval` 里 `vm.engine.sink.warn(...)` 写进去的告警，最终会出现在 `compile_impl` 持有的那个 `sink` 里。

**预期结果**：你会清楚地看到，`eval` 并没有「自己的」sink，它借用的是 `compile_impl` 的 sink。因此求值阶段产生的所有 warning / 延迟错误，都被直接灌进了主流程的同一个收集器。

> 待本地验证：参数对齐属于阅读练习，无需运行。

#### 4.3.5 小练习与答案

**练习 1**：`compile_impl` 调用 `eval` 时传的 `Route::default().track()` 是「空路由」。如果传一个非空路由会怎样？

**参考答案**：`Route` 用来追踪嵌套调用链，`route.contains(id)` 据此检测循环 `#include`。主源码是整条调用链的起点，所以应当从空路由开始。若传非空路由，相当于「假装我们已经从某些文件 include 过来了」，可能让本应被检测到的循环包含漏检，或让深度计数错误。因此主入口必须用 `Route::default()`。

**练习 2**：为什么 `eval` 内部用 `EmptyIntrospector`，而不是等到布局结果出来再求值？

**参考答案**：求值和布局是两个分离的阶段，且布局**依赖**求值产出的 `Content`，所以求值必然先于布局发生。求值时根本没有页面布局可用，只能给空内省器。Typst 用「先求值出一个初步 Content → 反复布局到稳定」的方式打破这个鸡生蛋问题，而不是试图把两者合并。

---

### 4.4 稳定化循环骨架与 T::create

#### 4.4.1 概念说明

阶段 [6] 是 `compile_impl` 里最精巧的部分：一个 `loop`。它要解决的问题在本讲只讲**直觉**：Typst 文档里有很多「依赖最终布局结果」的东西——目录页码、交叉引用、计数器最终值、`query` 查询结果。可这些结果又**会影响布局本身**（比如目录占了多少页，会影响后面正文的页码）。这就形成一个「鸡生蛋」循环，解法是**反复布局，直到内省信息不再变化**（即「收敛」）。

本模块只交代循环的**骨架**：它每一轮做哪些事、靠什么判断该停、最多跑几轮。**收敛判定的内部细节**（`constraint.validate` 到底在比什么、`history` 如何记录、没收敛时怎么生成诊断）留给 u2-l2 / u2-l3。

#### 4.4.2 核心流程

```text
loop {
  (a) 给本轮开一个计时作用域 ITER_NAMES[history.len()]   // "iter (1)".."iter (5)"
  (b) 取内省器：若有上一轮产物 → 用它的 introspector；否则用 empty_introspector
  (c) 新建一把 comemo constraint + 一个本轮用的 subsink + 本轮 Engine
  (d) document = T::create(&mut engine, &content, styles)?   // 真正的「布局/导出」实现在这
  (e) 若 constraint.validate(document.introspector()) 为真 → 已稳定：
        把本轮 subsink 合并进主 sink，break
  (f) 若 history 已满（放不下更多历史）→ 超过上限，调 analyze() 生成「未收敛」告警后 break
  (g) 否则：history.push(document)，进入下一轮
}
```

要点：

- **最多 `MAX_ITERS = 5` 轮**：`history` 是一个容量为 `MAX_ITERS - 1 = 4` 的 `ArrayVec`，配合首轮的空内省器，总共至多 5 次布局。
- **每轮一个 `subsink`**：本轮产生的告警/延迟错误先收进 `subsink`；只有当这轮被认定为「稳定的那一轮」时，才 `extend_from_sink` 合并进主 `sink`。这样可以避免把中间不稳定轮次的噪声诊断暴露给用户。
- **`T::create` 是真正的布局入口**：对 `PagedDocument` 来说，它内部会调到 `typst_layout::layout_document`（经 ROUTINES 接线，见 u3-l2）。

#### 4.4.3 源码精读

循环开头：计时作用域 + 取内省器——[crates/typst/src/lib.rs:138-144](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L138-L144)。首轮 `history` 为空，`history.last()` 是 `None`，于是用 `empty_introspector`；之后每轮用上一轮产物的 introspector。

```rust
    loop {
        let _scope = TimingScope::new(ITER_NAMES[history.len()]);
        let introspector = history
            .last()
            .map(|doc| doc.introspector())
            .unwrap_or(&empty_introspector);
        let constraint = comemo::Constraint::new();
```

构造本轮 Engine 并调用 `T::create`——[crates/typst/src/lib.rs:146-156](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L146-L156)。`Engine` 的六个字段（`library / world / introspector / traced / sink / route`，定义见 [crates/typst-library/src/engine.rs:18-36](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L18-L36)）在这里被填满；其中 `introspector` 带上了本轮的 `constraint`，这样 comemo 才能做「约束校验」。

```rust
        let mut subsink = Sink::new();
        let mut engine = Engine { library, world,
            introspector: Protected::new(introspector.track_with(&constraint)),
            traced, sink: subsink.track_mut(), route: Route::default(),
        };
        document = T::create(&mut engine, &content, styles)?;
```

收敛判定与合并本轮 sink——[crates/typst/src/lib.rs:158-161](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L158-L161)。`constraint.validate(...)` 返回真表示「本轮给出的内省信息，和我之前承诺的一致」→ 稳定，跳出。**注意：只有这一支才会 `sink.extend_from_sink(subsink)`**。

```rust
        if timed!("check stabilized", constraint.validate(document.introspector())) {
            sink.extend_from_sink(subsink);
            break;
        }
```

超过上限的处理——[crates/typst/src/lib.rs:163-184](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L163-L184)。`history.is_full()` 为真意味着已经跑了 `MAX_ITERS` 轮还没收敛：此时收集所有历史 introspector，交给 `analyze()` 生成「文档未在五次内收敛」的告警（**只产生 warning，不是致命错误**），合并 sink 后 break；否则 `history.push(document)` 继续下一轮。

相关常量定义在 [crates/typst-library/src/introspection/convergence.rs:16-18](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/introspection/convergence.rs#L16-L18)：`MAX_ITERS = 5`，`ITER_NAMES` 是 `["iter (1)", ..., "iter (5)"]` 这五个计时标签。

#### 4.4.4 代码实践

**实践目标**：用计数器读出循环的「形状」——多少轮、何时停。

**操作步骤**（源码阅读型）：

1. 假设一份文档**第 1 轮就收敛**（内省信息从一开始就不依赖布局，比如没有目录/交叉引用）。请按 4.4.2 的 (a)–(g) 推演：
   - 首轮：`history.len()` = 0，用 `empty_introspector`；`T::create` 后 `constraint.validate` 返回真 → break。
   - 此时 `history` 里 push 过吗？没有。
2. 再假设一份文档**始终不收敛**（极端的「页码 = 总页数」相互依赖）：
   - 追踪 `history` 的容量：它是 `ArrayVec<T, { MAX_ITERS - 1 }>` 即容量 4（见 [crates/typst/src/lib.rs:133](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L133)）。
   - 数一数：第 1 轮 history 空；第 2 轮 history=[d1]；第 3 轮 =[d1,d2]；第 4 轮 =[d1..d3]；第 5 轮 history 已满（4 个），走 `analyze` 分支 break。

**预期结果**：循环至多执行 5 次 `T::create`；无论收敛与否都不会无限循环。这正是 `MAX_ITERS` 这个硬上限的意义。

> 待本地验证：精确的「第几轮 validate 返回真」取决于具体文档的内省依赖，属 u2-l2 范畴；本练习只确认「至多 5 轮」这一结构事实。

#### 4.4.5 小练习与答案

**练习 1**：为什么用 `subsink` 收集本轮诊断，而不是直接写主 `sink`？

**参考答案**：因为中间不稳定轮次产生的诊断（尤其是一些 show 规则在内省未就绪时报的错）很可能是「假阳性」，会随收敛消失。只有被判定为「稳定那一轮」的诊断才该暴露给用户。用 `subsink` 暂存、只在收敛时合并，可以屏蔽掉这些噪声；未收敛时则由 `analyze()` 另作处理。

**练习 2**：`EmptyIntrospector` 在循环里一共可能出现几次？

**参考答案**：只在首轮出现一次——首轮 `history` 为空，`history.last()` 为 `None`，于是 fallback 到 `empty_introspector`。从第二轮起，`history.last()` 总能给出上一轮的真实 introspector，不再用空的那一个。（它也在 `eval` 内部用过一次，但那是求值阶段，不在本循环里。）

---

### 4.5 提升延迟错误（promote delayed errors）

#### 4.5.1 概念说明

阶段 [7]，循环结束后的最后一段代码很短，但意义重要。它处理的是「**延迟错误**」(delayed errors)。

延迟错误是一种特殊的错误：它在求值/布局的**早期迭代**里被记录，但**先不致命**。原因正是 4.3 提到的——求值早期用 `EmptyIntrospector`，很多依赖最终布局的 show 规则会因此报错，但这些错误很可能在收敛后就消失了。如果一上来就当成致命错误，用户会被一堆「其实过几轮就会消失」的错误淹没。

所以策略是：把这类错误丢进 `sink` 的 `delayed` 区，**等循环全部结束后**再看——如果那时它还在，说明它是真错误，就「提升」为致命错误返回；否则（已经被收敛消解）就忽略。

#### 4.5.2 核心流程

```text
循环结束后：
  delayed = sink.delayed()        // 取出（并清空）所有延迟错误
  if !delayed.is_empty() {
      return Err(delayed);        // 提升为致命错误
  }
  Ok(document)                    // 否则正常返回产物
```

关键：`sink.delayed()` 是 `std::mem::take`，取走的同时清空字段（[crates/typst-library/src/engine.rs:184-186](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L184-L186)）。

#### 4.5.3 源码精读

阶段 [7] 的全部代码——[crates/typst/src/lib.rs:187-193](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L187-L193)：

```rust
    // Promote delayed errors.
    let delayed = sink.delayed();
    if !delayed.is_empty() {
        return Err(delayed);
    }

    Ok(document)
}
```

延迟错误的「产生」侧在 `Sink`——[crates/typst-library/src/engine.rs:212-214](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L212-L214)：`delayed_error` 只是把错误 push 进 `delayed` 区，**不影响当前调用**。字段含义在结构体注释里说得很清楚——[crates/typst-library/src/engine.rs:155-160](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/engine.rs#L155-L160)：这些错误「我们可以忽略直到最后一轮迭代」。

> 一句话区分三种「错」的去向：
> - 普通 warning → `sink.warn`，最终进 `Warned` 的 warnings，**不致命**；
> - 延迟错误 → `sink.delayed_error`，先攒着，**末轮仍在才致命**（4.5）；
> - 即时错误 → `?` 直接 `return Err`，**当场致命**（如取主源码失败）。

#### 4.5.4 代码实践

**实践目标**：构造一个「会消失的延迟错误」与一个「不会消失的延迟错误」的对比。

**操作步骤**（源码阅读型）：

1. 想象一条 show 规则，它要 `query(heading)` 来生成目录，但只在 `target == Paged` 且「页面布局已完成」时才合法。
2. 早期迭代（空内省器）它报错 → 被某处写成 `delayed_error`（具体哪些代码路径用 `delayed_error`，可在仓库里全局搜 `delayed_error`/`delayed_errors` 自行追踪，本讲只理解机制）。
3. 收敛后内省器就绪，这条规则不再报错 → `delayed` 区为空 → `Ok(document)`，用户**完全看不到**这次错误曾经发生。
4. 反之，若这条规则因别的原因（例如引用了不存在的函数）在每一轮都报错，那么循环结束时 `delayed` 非空 → 提升为致命错误。

**预期结果**：你能解释「为什么 Typst 有时第一次编译看起来很正常，却没有报某个其实出过错的错」——因为那个错是延迟错误，被收敛消解了。

> 待本地验证：哪些具体场景会触发 `delayed_error` 需要结合 show 规则与内省的具体实现；本练习聚焦于「提升」这一步的语义。

#### 4.5.5 小练习与答案

**练习 1**：如果把阶段 [7] 删掉（直接 `Ok(document)`），会有什么后果？

**参考答案**：所有延迟错误都会被静默吞掉。本该在末轮仍然存在的真错误（比如 show 规则里调用了一个不存在的函数）将不再上报，用户会得到一个「看起来成功」但其实有问题的文档。这正是「提升」存在的理由：给延迟错误一个在末轮「转正」的机会。

**练习 2**：`sink.delayed()` 为什么用 `mem::take` 而不是返回引用？

**参考答案**：因为它要**取出所有权**交给 `return Err(delayed)`（`EcoVec<SourceDiagnostic>` 需要按值返回）。`mem::take` 在拿走内容的同时把字段清空成默认值，既满足了所有权转移，又顺带清空了 sink，一举两得。

## 5. 综合实践

把本讲所有内容串起来，完成下面这个**贯穿性任务**——它正是本讲规格里指定的实践。

**任务**：为 `compile_impl` 的七个阶段画一张**时序流程图**，并标注每一步的「致命性」。

**操作步骤**：

1. 在纸上或任意画图工具里，从左到右画出这七个阶段：`取库 → 门控 → 样式 → 取主源码 → 求值 → 循环 → 提升错误`，参考骨架见 [crates/typst/src/lib.rs:99-194](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L99-L194)。
2. 在「循环」节点内部，画出它的子流程：`取内省器 → T::create → validate? →（真）合并sink&break /（满）analyze&break /（否）push&下一轮`。
3. 给每个阶段用颜色标注它**最可能**产生的诊断类型：
   - 🟥 `SourceResult::Err`（致命，`?` 或显式 `return Err`）
   - 🟨 warning（进 `sink.warn`，不致命）
   - 🟦 delayed error（进 `sink.delayed_error`，末轮才定生死）
   - ⬜ 一般无诊断
4. 参考下面的标注表自检：

| 阶段 | 关键源码行 | 标注 | 说明 |
| --- | --- | --- | --- |
| 取库 `world.library()` | L104 | ⬜ | 一般无诊断 |
| 目标门控 `match T::target()` | L105-109 | 🟥/🟨 | feature 未开 → `bail!` 致命；已开 → 只 warn |
| 装配样式链 | L111-114 | ⬜ | 纯构造 |
| 取主源码 | L116-120 | 🟥 | 失败经 `hint_invalid_main_file` + `?` 致命 |
| 求值 `typst_eval::eval` | L122-131 | 🟥/🟨/🟦 | 语法/求值即时错误 🟥；warning 🟨；内省相关可能 🟦 |
| 循环 `T::create` + validate | L138-185 | 🟨 | 未收敛时 `analyze` 产 warning；各轮错误先入 `subsink` |
| 提升延迟错误 | L187-191 | 🟥 | `delayed` 非空 → 致命 |

5. **进阶**：在你的图上额外标出「`eval` 借用的是 `compile_impl` 的同一个 `sink`」（参数 `sink.track_mut()`），以及「只有稳定那一轮的 `subsink` 才会被 `extend_from_sink` 合并」这两条信息流。

**预期结果**：一张能让人一眼看清「致命路径集中在门控/取主源码/提升错误三处，而循环内部偏柔性」的流程图。这张图也是你阅读 u2-l2（收敛循环深挖）时的导航。

> 本实践为源码阅读 + 画图型，无需运行编译器；标注表里的行号均可在当前 HEAD（`32fd4cc38`）下点击核对。

## 6. 本讲小结

- `compile_impl` 是 `typst` crate 里唯一真正干活的函数，可切成 **七个阶段**：取库 → 目标门控 → 装配样式链 → 取主源码 → 求值 → 稳定化循环 → 提升延迟错误。
- 编译目标通过 `TargetElem::target` 这个样式字段注入样式系统，让下游元素能感知「我在为 Paged 还是 Html 布局」。
- 取主源码失败时，`hint_invalid_main_file` 会针对 UTF-8 错误给出友好提示（包括「试着把扩展名换成 `.typ` 再读一次」），但这一步本身是**致命**的。
- 求值入口 `typst_eval::eval` 借用的是 `compile_impl` 的同一个 `sink`，求值阶段只能用 `EmptyIntrospector`（页面布局此时还不存在）。
- 稳定化循环至多 `MAX_ITERS = 5` 轮，每轮用一个临时 `subsink`，只有「稳定那一轮」的诊断才合并进主 `sink`。
- 「延迟错误」先攒着、循环结束后才提升为致命错误，以此屏蔽掉早期迭代中「会随收敛消失」的假阳性。

## 7. 下一步学习建议

本讲把 `compile_impl` 的**骨架**走完了，但故意把两块硬骨头留给后续：

- **想搞懂循环到底怎么判定「稳定」、没稳定时怎么生成诊断**：请学 u2-l2《内省稳定化循环》与 u2-l3《内省记录与非收敛检测》，那里会精读 `constraint.validate`、`history`、`analyze()`、`Introspect` trait。
- **想搞懂贯穿全程的上下文**：请学 u2-l4《Engine / Sink / Route / Traced》，那里会讲透 `Sink` 的去重、`Route` 的循环检测与深度上界、`Traced` 的 span 过滤。
- **想从架构层面看「编译目标」「ROUTINES 接线」**：可跳到 u3 层的 u3-l1（Target/Output 多目标抽象）与 u3-l2（ROUTINES 与 crate 切分）。

建议阅读顺序：**u2-l2 → u2-l3 → u2-l4**，最后再回过头用本讲的七阶段图作为全局导航。
