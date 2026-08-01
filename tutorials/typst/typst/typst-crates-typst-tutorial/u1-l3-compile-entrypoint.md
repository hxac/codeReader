# 调用 compile() 完成一次编译

## 1. 本讲目标

前两讲我们认识了 `typst` crate 的「门面」定位（u1-l1），也理解了嵌入编译器时必须实现的环境契约 `World`（u1-l2）。现在，要把这两者接起来——**真正调用一次编译**。

本讲聚焦于 `typst` crate 对外暴露的公开入口 `compile` 函数。学完本讲，你应当能够：

1. 看懂并解释 `pub fn compile<T>(world: &dyn World) -> Warned<SourceResult<T>>` 这一行签名里每一个部分的含义。
2. 理解泛型参数 `T: Output` 决定了「编译产物是什么类型」，并知道当前合法的产物有哪些（分页文档 `PagedDocument`、HTML 文档 `HtmlDocument`）。
3. 区分返回类型 `Warned<SourceResult<T>>` 里「真正的结果（成功/失败）」和「告警」两部分，理解为什么 Typst 即使成功也会带告警。
4. 知道 `#[typst_macros::time]` 这个计时注解的存在与作用。
5. 写出一段「构造 World → 调用 compile → 处理产物与告警」的伪代码骨架。

> 提醒：本讲**只看公开入口 `compile` 本身**。它内部调用的 `compile_impl` 主流程（求值、布局、稳定化循环）属于下一单元 u2-l1 的内容，本讲只会点到为止。

## 2. 前置知识

阅读本讲前，你需要先理解两个概念（它们在 u1-l1、u1-l2 已建立）：

- **门面（facade）**：`typst` crate 源码极小，主要职责是把各个子 crate 的能力打包再导出，对外只露 `compile` / `trace` 两个入口。
- **World trait**：编译器「只算不取」——它需要的标准库、字体、源码、日期等外部资源，全部由宿主通过 `World` 注入。所以 `compile` 的第一个参数就是 `&dyn World`。

本讲还会遇到几个 Rust 概念，先做最简解释：

| 术语 | 一句话解释 |
|------|-----------|
| 泛型函数 | 像 `compile<T>` 这样带类型参数 `T` 的函数，调用时用 `compile::<具体类型>()` 指明 `T`。 |
| trait bound | `T: Output` 表示「`T` 必须实现了 `Output` 这个 trait」，是对类型参数的约束。 |
| `Result<T, E>` | Rust 标准库的「可能失败」类型，`Ok(T)` 表示成功，`Err(E)` 表示失败。 |
| `EcoVec` | 来自 `ecow` crate 的「可廉价克隆的向量」，行为类似 `Vec`，但克隆开销很小，Typst 内部大量使用它。 |
| `comemo` | Typst 使用的增量缓存库；`world.track()` 得到一个「可被缓存追踪」的句柄 `Tracked<dyn World>`。 |

其余如 `SourceDiagnostic`、`Warned`、`SourceResult` 都会在本讲第 4 节逐一讲解。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|------|------|
| [crates/typst/src/lib.rs:63-82](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L63-L82) | `compile` 公开函数本身：创建 `Sink`、调用内部 `compile_impl`、把结果包成 `Warned` 返回。 |
| [crates/typst-library/src/diag.rs:208-210](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/diag.rs#L208-L210) | `SourceResult<T>` 类型别名，把「成功产物」与「一批错误」组合起来。 |
| [crates/typst-library/src/diag.rs:281-295](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/diag.rs#L281-L295) | `Warned<T>` 结构体及其 `map` 方法：把「产物」与「告警」捆绑在一起。 |
| [crates/typst-library/src/diag.rs:297-353](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/diag.rs#L297-L353) | `SourceDiagnostic` 结构体、`Severity` 枚举，以及构造错误/告警的方法。 |
| [crates/typst-library/src/foundations/target.rs:13-30](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/target.rs#L13-L30) | `Output` trait 定义：解释 `T: Output` 这个约束到底是什么。 |

记忆口诀：`lib.rs` 里有「入口」，`diag.rs` 里有「返回值的两个零件（`Warned` 和 `SourceResult`/`SourceDiagnostic`）」，`target.rs` 里有「`T` 必须满足的约束 `Output`」。

## 4. 核心概念与源码讲解

### 4.1 compile 函数

#### 4.1.1 概念说明

`compile` 是 `typst` crate 对外暴露的两个公开入口之一（另一个是 `trace`，用于 IDE 的值追踪，本讲不展开）。

它的角色非常薄——**它只是一个「外壳」**：

- 它负责把外部世界（`World`）和内部实现（`compile_impl`）对接；
- 它负责收集告警（通过 `Sink`）；
- 它负责把最终结果包装成统一的返回类型 `Warned<SourceResult<T>>`。

真正干活的「解析 → 求值 → 布局 → 导出」四步流水线，全部在 `compile_impl` 里（详见 u2-l1）。把 `compile` 写成薄外壳的好处是：调用方只需要面对一个干净、稳定的公开签名，而内部实现可以随时重构。

#### 4.1.2 核心流程

`compile` 的执行流程极其简短，伪代码如下：

```
fn compile<T: Output>(world):
    1. 新建一个空的 Sink（告警收集器）
    2. 调用 compile_impl::<T>(world 的追踪句柄, 默认 Traced, &mut sink)
         → 得到 SourceResult<T>（Ok 产物 或 Err 一批错误）
    3. 如果是 Err，用 deduplicate 去重
    4. 把结果连同 sink 里收集的告警，一起包进 Warned 返回
```

关键点：

- 第 2 步把 `world` 用 `world.track()` 转成 `comemo` 可追踪的句柄，这是增量缓存能生效的前提（见 u1-l2）。
- 第 3 步的 `deduplicate` 只在「出错」时才对错误列表去重；告警的去重在 `Sink` 内部完成。
- 第 4 步无论成功还是失败，都会把 `sink.warnings()` 一起返回——**所以失败时也可能同时带有告警**。

#### 4.1.3 源码精读

完整源码（含文档注释）只有 20 行：

> [crates/typst/src/lib.rs:63-82](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L63-L82) —— `compile` 公开函数。文档列出了支持的两种产物，并说明了返回结果两种情况：`Ok(output)` 表示无致命错误，`Err(errors)` 表示有致命错误。

我们把核心几行拆开看：

**第 1 行：计时注解。**

```rust
#[typst_macros::time]
```

> [crates/typst/src/lib.rs:73](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L73) —— `#[typst_macros::time]` 是一个属性宏（attribute macro），来自 `typst-macros` crate。它会把被标注的函数体包裹进一个计时作用域，在启用追踪时（例如 `--timing`）记录该函数的执行耗时。

需要知道的是：这个宏在 `wasm32` 目标上会被省略，以避免让不需要追踪的 Web 端体积膨胀。日常阅读源码时，把它当成「给这个函数加了个秒表」即可。

**第 2 行：函数签名。**

```rust
pub fn compile<T>(world: &dyn World) -> Warned<SourceResult<T>>
where
    T: Output,
```

> [crates/typst/src/lib.rs:74-76](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L74-L76) —— 这就是本讲的「主角签名」。

逐个拆解：

- `pub fn compile<T>`：泛型函数，类型参数 `T` 表示「想要的编译产物类型」。
- `world: &dyn World`：传入一个对 `World` 的引用（trait object）。注意是 `&dyn`，所以任何实现了 `World` 的类型都能以引用形式传入（u1-l2 讲过 `world_impl!` 宏还为 `Box`/`Arc`/`&` 自动生成了实现）。
- `-> Warned<SourceResult<T>>`：返回值是一个嵌套的类型（见 4.2、4.3 节）。
- `where T: Output`：约束 `T` 必须实现 `Output` trait（见下文）。

`Output` trait 长这样：

> [crates/typst-library/src/foundations/target.rs:13-30](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/foundations/target.rs#L13-L30) —— `Output` trait 定义了三个能力：`target()` 说明这种产物对应哪种编译目标；`create()` 真正「造出」产物；`introspector()` 提供对该产物的内省（introspection）能力。

目前仓库里实现了 `Output` 的产物有：

| 产物类型 | 定义位置 | `target()` 返回 | 含义 |
|---------|---------|----------------|------|
| `PagedDocument` | `typst-layout` crate | `Target::Paged` | 分页文档（用于导出 PDF/PNG/SVG） |
| `HtmlDocument` | `typst-html` crate | `Target::Html` | HTML 文档 |

例如分页文档的实现：

> [crates/typst-layout/src/document.rs:63-70](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-layout/src/document.rs#L63-L70) —— `impl Output for PagedDocument`，其中 `target()` 返回 `Target::Paged`。

所以调用时，你要用 **turbofish** 语法 `compile::<PagedDocument>(&world)` 明确告诉编译器「我想要分页文档」。这正是 `T` 决定产物类型的体现。

**第 3 行起：函数体。**

```rust
let mut sink = Sink::new();
let output = compile_impl::<T>(world.track(), Traced::default().track(), &mut sink)
    .map_err(deduplicate);
Warned { output, warnings: sink.warnings() }
```

> [crates/typst/src/lib.rs:78-81](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L78-L81) —— `compile` 的完整函数体：建 `Sink`、调 `compile_impl`、出错时去重、最后包成 `Warned`。

- `Sink::new()`：`Sink` 是编译过程中的「只增容器」，用来收集告警、延迟错误、内省记录、追踪值（详见 u2-l4）。这里先建一个空的。
- `world.track()`：把 `&dyn World` 转成 `comemo` 的追踪句柄 `Tracked<dyn World>`，让方法调用可被增量缓存。
- `Traced::default().track()`：`Traced` 用于「按 span 追踪某些值」（IDE 的 `trace()` 入口会用到）。普通 `compile` 传一个空的默认值即可。
- `compile_impl::<T>(...)`：真正干活的内部函数，返回 `SourceResult<T>`。
- `.map_err(deduplicate)`：`map_err` 是 `Result` 的方法，仅当结果是 `Err` 时，对里面的错误列表套用 `deduplicate` 去重。`deduplicate` 的细节（按 `span` + `message` 哈希去重）属于 u3-l4 的内容。
- `Warned { output, warnings: sink.warnings() }`：把「结果」和「告警列表」一起组装成 `Warned` 返回。注意这里无论 `output` 是 `Ok` 还是 `Err`，都会把告警一并返回。

#### 4.1.4 代码实践

**实践目标**：把 `compile` 那 3 行函数体「手动展开」成等价的、更啰嗦的伪代码，标注每一步的中间类型，帮助自己彻底看清数据流向。

**操作步骤**：

1. 打开 [crates/typst/src/lib.rs:74-82](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L74-L82)。
2. 对照下面这段「展开版」伪代码（示例代码，非项目原有代码），在每一行后面用注释写出中间变量的类型：

```rust
// 示例代码：compile 函数体的等价展开（仅用于理解，不是项目原码）
let mut sink: Sink = Sink::new();

let raw: SourceResult<T> = compile_impl::<T>(
    world.track(),              // 类型: Tracked<dyn World + '_>
    Traced::default().track(),  // 类型: Tracked<Traced>
    &mut sink,                  // 类型: &mut Sink
);
// raw 是 Result<T, EcoVec<SourceDiagnostic>>

let output: SourceResult<T> = raw.map_err(|errs: EcoVec<SourceDiagnostic>| {
    deduplicate(errs)           // 出错时才执行去重
});

let warnings: EcoVec<SourceDiagnostic> = sink.warnings();

Warned { output, warnings }     // 组装最终返回值
```

**需要观察的现象**：

- `output` 的类型是 `SourceResult<T>`，即它本身**已经是**一个 `Result`。
- `warnings` 永远会被提取出来，和 `output` 平级地放进 `Warned`。

**预期结果**：你能向别人讲清楚「`compile` 返回值里，哪一层表达成功/失败，哪一层表达告警」。

#### 4.1.5 小练习与答案

**练习 1**：如果调用方写的是 `typst::compile(&world)` 而不带 `::<PagedDocument>`，会发生什么？

> **答案**：编译无法通过。因为 `compile<T>` 的返回类型 `Warned<SourceResult<T>>` 里的 `T` 没有被推断出来，编译器无法确定 `T`。调用方必须用 `compile::<PagedDocument>(&world)` 这样的 turbofish 语法，或把结果绑定到一个明确了 `T` 的变量上，才能让 `T` 被推断出。

**练习 2**：`compile` 函数体里有 `.map_err(deduplicate)`，为什么没有对应的 `.map(...)` 来处理成功的情况？

> **答案**：因为成功时不需要对产物 `T` 做任何转换——直接原样放进 `Warned` 即可。`map_err` 只关心 `Err` 分支，`Ok` 分支保持不变，这正是我们想要的。

**练习 3**：`compile` 的第一个参数是 `world: &dyn World`，但函数体里传给 `compile_impl` 的却是 `world.track()`。为什么要做这个转换？

> **答案**：`world.track()` 把 `&dyn World` 转成 `comemo` 的 `Tracked<dyn World>` 句柄。`comemo` 需要这个句柄才能在方法调用时建立缓存键，从而实现增量编译——当 `World` 没变时复用上次的计算结果。这是 u1-l2 提到的「`World` 方法可被增量缓存」的具体接入点。

### 4.2 Warned 结构体

#### 4.2.1 概念说明

为什么要把返回值设计成 `Warned<SourceResult<T>>` 而不是直接返回 `SourceResult<T>`？

因为 **Typst 即使在「编译成功」的情况下，也可能产生告警（warning）**。例如：

- 文档里指定了一个找不到的字体，Typst 会回退到默认字体并继续编译（成功 + 告警）。
- 用了某个已废弃（deprecated）的函数。
- 某个 set 规则的参数组合不合理。

如果只用 `Result`，那么「成功但有告警」这个状态就无处安放——`Ok` 只能携带产物，`Err` 又会把告警升级成致命错误。所以 Typst 专门设计了 `Warned<T>`，把「产物 `T`」和「告警列表」**并列**捆绑：

```
Warned {
    output: T,                    // 产物（在 compile 里，T 本身又是个 Result）
    warnings: EcoVec<SourceDiagnostic>,
}
```

> 提醒：对 `compile` 而言，`Warned` 里的 `output` 本身就是 `SourceResult<T>`。也就是说返回类型是**两层嵌套**：外层 `Warned` 负责「告警」，内层 `SourceResult` 负责「成功/失败」。

#### 4.2.2 核心流程

`Warned<T>` 的语义：

- `output`：你真正关心的值。在 `compile` 里它是 `SourceResult<T>`（成功/失败）。
- `warnings`：过程中收集到的告警，**无论 `output` 成功还是失败都可能出现**。

它还提供了一个 `map` 方法，用来对 `output` 做变换而**保留原有告警**：

```
Warned { output: A, warnings }  --map(f)-->  Warned { output: f(A), warnings }
```

这在你拿到产物后想再做一步转换（例如把 `PagedDocument` 转成 PDF 字节），又不希望丢掉告警时很有用。

#### 4.2.3 源码精读

**结构体定义：**

> [crates/typst-library/src/diag.rs:281-288](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/diag.rs#L281-L288) —— `Warned<T>`：两个字段 `output`（产物）和 `warnings`（告警列表），都是 `pub`，调用方可直接读取。

```rust
pub struct Warned<T> {
    pub output: T,
    pub warnings: EcoVec<SourceDiagnostic>,
}
```

注意它派生了 `Debug, Clone, Eq, PartialEq, Hash`，所以 `Warned` 可以被比较、哈希、克隆——这一点对增量缓存很重要（缓存要以「输入 + 调用参数」为键，返回值需要可哈希）。

**map 方法：**

> [crates/typst-library/src/diag.rs:290-295](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/diag.rs#L290-L295) —— `Warned::map`：只对 `output` 应用函数 `f`，告警原样保留。

```rust
pub fn map<R, F: FnOnce(T) -> R>(self, f: F) -> Warned<R> {
    Warned { output: f(self.output), warnings: self.warnings }
}
```

#### 4.2.4 代码实践

**实践目标**：写一段伪代码，正确地从一个 `Warned<SourceResult<PagedDocument>>` 中**分别**处理「成功/失败」和「告警」。

**操作步骤**：

1. 阅读上面的结构体定义，确认 `output` 和 `warnings` 是两个 `pub` 字段。
2. 对照下面的示例代码（非项目原有代码），理解处理顺序：

```rust
// 示例代码：处理 compile 的返回值
let result: Warned<SourceResult<PagedDocument>> = typst::compile::<PagedDocument>(&world);

// 1. 先处理「成功 / 失败」
match result.output {
    Ok(document) => {
        // 编译成功，拿到分页文档，可以继续导出 PDF/SVG/PNG
        println!("成功，共 {} 页", document.pages.len());
    }
    Err(errors) => {
        // 编译失败，errors 是 EcoVec<SourceDiagnostic>
        for err in &errors {
            println!("错误: {}", err.message);
        }
    }
}

// 2. 再处理「告警」（无论成功失败都要看）
for warning in &result.warnings {
    println!("告警: {}", warning.message);
}
```

**需要观察的现象**：

- 即使走 `Ok` 分支，`result.warnings` 仍可能非空。
- 即使命名是 `warnings`，它的元素类型 `SourceDiagnostic` 和错误用的是**同一个类型**，靠 `severity` 字段区分（见 4.3）。

**预期结果**：你能说清楚「为什么处理 `compile` 返回值必须既看 `output` 又看 `warnings`，而不能只看其中之一」。

#### 4.2.5 小练习与答案

**练习 1**：`Warned<T>` 里的 `T` 在 `compile` 场景下具体是什么类型？

> **答案**：是 `SourceResult<U>`，其中 `U` 是产物类型（如 `PagedDocument`）。所以 `compile::<PagedDocument>` 返回 `Warned<SourceResult<PagedDocument>>`。

**练习 2**：假设你写了一个函数把 `PagedDocument` 渲染成 PDF 字节 `Vec<u8>`，希望返回「PDF 字节 + 告警」。你会怎么用 `Warned::map`？

> **答案**：`result.map(|doc| render_to_pdf(doc))`，其中 `result` 是 `Warned<SourceResult<PagedDocument>>`。不过要注意，`map` 只变换 `output`，不会自动拆开内层的 `Result`——如果你想在 `map` 里同时处理 `Ok/Err`，需要在闭包里先 `match`。完整写法类似 `result.map(|sr| sr.map(render_to_pdf))`，得到 `Warned<SourceResult<Vec<u8>>>`。

**练习 3**：为什么 `Warned` 派生了 `Hash`？

> **答案**：因为 `compile` 是被 `comemo` 增量缓存的函数。缓存系统需要把返回值纳入缓存键/值的可哈希性校验，所以返回类型（包括 `Warned`、`SourceResult`、`SourceDiagnostic`）一路都需要可哈希。

### 4.3 SourceResult / SourceDiagnostic

#### 4.3.1 概念说明

`compile` 返回值内层是 `SourceResult<T>`，它其实只是一个类型别名：

```rust
pub type SourceResult<T> = Result<T, EcoVec<SourceDiagnostic>>;
```

两个关键点：

1. **错误是一「批」而不是一个**：错误类型是 `EcoVec<SourceDiagnostic>` 而不是单个 `SourceDiagnostic`。Typst 会尽量在一次编译里收集**多个**错误（比如文件里同时有 3 处语法错误），而不是遇到第一个就停下。这对用户体验很重要——改一次能看见所有问题。
2. **`SourceDiagnostic` 是统一的「诊断信息」载体**：无论是致命错误还是普通告警，用的是同一个结构体，靠 `severity` 字段区分。

#### 4.3.2 核心流程

一个 `SourceDiagnostic` 携带五样东西：

| 字段 | 含义 | 举例 |
|------|------|------|
| `severity` | 严重程度 | `Error`（致命）或 `Warning`（告警） |
| `span` | 在源码中的位置 | 某个 `Span`，或外部文件的字节区间 |
| `message` | 给用户看的描述 | `"unknown variable: x"` |
| `trace` | 函数调用栈 | `["while calling `f`", "while calling `g`"]`，帮助定位错误是怎么传导过来的 |
| `hints` | 修复建议 | `"did you mean to use `xy`?"` |

它的生命周期大致是：

```
源码里某处出问题
   ↓
用 bail!(span, "...") 或 error!(span, "...") 宏构造一个 SourceDiagnostic（severity = Error）
   ↓
若致命：进入 SourceResult 的 Err(EcoVec<...>)，最终被 deduplicate 去重
若非致命：以 Warning 形式写入 Sink，最终出现在 Warned.warnings 里
```

> 注意区分：`SourceResult` 的 `Err` 分支装的是「致命错误」（`severity = Error`）；`Warned.warnings` 装的通常是「告警」（`severity = Warning`）。两者都用 `SourceDiagnostic` 类型，但语义不同。

#### 4.3.3 源码精读

**SourceResult 类型别名：**

> [crates/typst-library/src/diag.rs:208-210](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/diag.rs#L208-L210) —— `SourceResult<T> = Result<T, EcoVec<SourceDiagnostic>>`。注意错误类型是「一组」诊断，支持一次返回多个错误。

```rust
pub type SourceResult<T> = Result<T, EcoVec<SourceDiagnostic>>;
```

**SourceDiagnostic 结构体：**

> [crates/typst-library/src/diag.rs:297-321](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/diag.rs#L297-L321) —— `SourceDiagnostic`：五字段结构体（severity / span / message / trace / hints）。文档注释提示：推荐用 `error!` 或 `warning!` 宏来构造它。

```rust
pub struct SourceDiagnostic {
    pub severity: Severity,
    pub span: DiagSpan,
    pub message: EcoString,
    pub trace: EcoVec<Spanned<Tracepoint>>,
    pub hints: EcoVec<Spanned<EcoString, DiagSpan>>,
}
```

**Severity 枚举：**

> [crates/typst-library/src/diag.rs:323-330](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/diag.rs#L323-L330) —— `Severity` 只有两个值：`Error`（致命）和 `Warning`（非致命）。

```rust
pub enum Severity {
    Error,
    Warning,
}
```

**两个常用构造方法：**

> [crates/typst-library/src/diag.rs:332-353](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/diag.rs#L332-L353) —— `SourceDiagnostic::error(...)` 和 `SourceDiagnostic::warning(...)`：分别构造一个严重程度为 `Error` / `Warning` 的空 trace、空 hints 的诊断。

```rust
pub fn error(span: impl Into<DiagSpan>, message: impl Into<EcoString>) -> Self {
    Self { severity: Severity::Error, span: span.into(), trace: eco_vec![], message: message.into(), hints: eco_vec![] }
}

pub fn warning(span: impl Into<DiagSpan>, message: impl Into<EcoString>) -> Self {
    Self { severity: Severity::Warning, /* ...同上 */ }
}
```

实际项目中，更常用的是封装好的宏 `bail!`、`error!`、`warning!`（定义在同一文件上部），它们能在构造诊断的同时附带 hints、并支持提前 return。`compile` 内部出错时（如主文件读取失败）就是用这些机制生成 `SourceDiagnostic`，再包成 `EcoVec` 返回。

#### 4.3.4 代码实践

**实践目标**：通过阅读一个「构造并返回诊断」的现场，理解 `SourceDiagnostic` 的五字段从何而来。

**操作步骤**：

1. 打开 [crates/typst/src/lib.rs:208-244](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L208-L244) —— `hint_invalid_main_file` 函数。这是 `compile_impl` 在读取主源码失败时（[crates/typst/src/lib.rs:118-120](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst/src/lib.rs#L118-L120)）调用的错误处理函数。
2. 阅读它如何用 `SourceDiagnostic::error(Span::detached(), ...)` 构造一个错误诊断，并用 `.hint(...)` 往 `hints` 字段里追加修复建议。
3. 它最终返回 `eco_vec![diagnostic]`，即一个只含单个诊断的 `EcoVec<SourceDiagnostic>`，正好是 `SourceResult` 的 `Err` 类型。

**需要观察的现象**：

- 一条 `SourceDiagnostic` 可以带**多条** hint（用户把 `.typ` 写成 `.pdf` 时会得到多条提示）。
- 这里 `span` 用的是 `Span::detached()`，因为「主文件读不到」无法定位到源码里具体哪一行。

**预期结果**：你能指出在这个函数里，`severity`、`span`、`message`、`hints` 四个字段分别被如何赋值（`trace` 此处为空）。

> 说明：本实践是「源码阅读型」实践，不要求运行。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `SourceResult` 的错误类型是 `EcoVec<SourceDiagnostic>` 而不是单个 `SourceDiagnostic`？

> **答案**：为了让一次编译能汇报**多个**错误。例如源码里有 3 处独立错误，Typst 会把它们收集到一个 `EcoVec` 里一次性返回，用户改一次就能看到全部问题，而不是改一个、重编译、再看下一个。

**练习 2**：`SourceDiagnostic` 同时用于「错误」和「告警」，靠什么区分？

> **答案**：靠 `severity` 字段：`Severity::Error` 是致命错误，`Severity::Warning` 是非致命告警。在 `compile` 的返回值里，致命错误走 `SourceResult` 的 `Err` 分支（且会被 `deduplicate` 去重），告警则进入 `Warned.warnings`。

**练习 3**：`SourceDiagnostic` 的 `trace` 字段有什么用？

> **答案**：它记录「导致这个问题的函数调用栈」，是一串 `Tracepoint`（如 `Call`/`Show`/`Import`/`Include`）。当错误发生在被层层调用的函数深处时，`trace` 能告诉用户「这个错误是在调用 `f`、再调用 `g` 的过程中产生的」，帮助定位。它通过 `Trace` trait（[crates/typst-library/src/diag.rs:455-485](https://github.com/typst/typst/blob/32fd4cc3861e0ab99f4c42ca6bea281482ba9f51/crates/typst-library/src/diag.rs#L455-L485)）在错误向上传播时逐层追加。

## 5. 综合实践

把本讲三个最小模块串起来，完成下面的**端到端调用骨架**。

**实践目标**：写出一段完整的伪代码，覆盖「构造 World → 调用 `compile` → 处理产物与告警」全流程，并在每一步标注类型与依据。

**操作步骤**：

1. 回顾 u1-l2 中 `World` 的 7 个方法（`library`/`book`/`main`/`source`/`file`/`font`/`today`）。
2. 草拟一个最小 `MyWorld` 结构体，列出实现这 7 个方法需要持有的字段（不必完全可运行）。
3. 写出调用 `compile::<PagedDocument>` 的骨架，并按本讲 4.2.4 的方式分别处理 `output`（`Ok/Err`）与 `warnings`。

**参考骨架（示例代码，非项目原有代码，不可直接编译）**：

```rust
// 示例代码：端到端调用 compile 的骨架（仅示意，省略大量实现细节）

use typst::{World, compile};
use typst_layout::PagedDocument;
use typst_library::diag::{SourceDiagnostic, Severity};

// —— 第 1 步：构造 World ——
struct MyWorld {
    // 实现 World 的 7 个方法需要持有的字段，例如：
    // library: LazyHash<Library>,   // world.library()
    // book: LazyHash<FontBook>,     // world.book()
    // main_id: FileId,              // world.main()
    // sources: HashMap<FileId, Source>, // world.source(id)
    // files: HashMap<FileId, Bytes>,    // world.file(id)
    // fonts: Vec<Font>,             // world.font(id)
    // today: DateTime,              // world.today()
}

impl World for MyWorld {
    // ... 实现 7 个方法（详见 u1-l2） ...
}

fn main() {
    let world = MyWorld { /* ... */ };

    // —— 第 2 步：调用 compile，指明想要 PagedDocument ——
    let warned = compile::<PagedDocument>(&world);
    // warned 的类型: Warned<SourceResult<PagedDocument>>

    // —— 第 3 步 A：处理成功 / 失败（内层 SourceResult）——
    match &warned.output {
        Ok(document) => {
            // 拿到 PagedDocument，可继续交给 typst-pdf 等导出
            eprintln!("编译成功，共 {} 页", document.pages.len());
        }
        Err(errors) => {
            // errors: &EcoVec<SourceDiagnostic>，全部是 Severity::Error
            for diag in errors {
                eprintln!("[错误] {}", diag.message);
            }
        }
    }

    // —— 第 3 步 B：处理告警（无论成功失败都要看）——
    for diag in &warned.warnings {
        // 这里一般 severity == Warning，但类型上和错误相同
        let sev = match diag.severity { Severity::Warning => "告警", _ => "?" };
        eprintln!("[{sev}] {}", diag.message);
        for hint in &diag.hints {
            eprintln!("   提示: {}", hint.v);
        }
    }
}
```

**需要观察的现象**：

- 第 2 步的 turbofish `::<PagedDocument>` 是「选择产物类型」的唯一开关；换成 `::<HtmlDocument>` 就会走 HTML 编译路径。
- 第 3 步 A 与 B 是**两次独立的遍历**：一次看 `output`（成功/失败），一次看 `warnings`（告警）。
- 真正在仓库里调用 `compile` 的地方是命令行前端 `typst-cli`（它实现了完整的 `SystemWorld`，并在编译后把 `PagedDocument` 交给 PDF/SVG/PNG 导出器）。本骨架只是抽取其核心脉络。

**预期结果**：你能向别人完整复述「从一段 Typst 源码到一个 `PagedDocument`（或一堆诊断）」经过 `compile` 的全过程，并准确说出返回值每一层的职责。

> 若你希望真机运行：可以参照仓库根目录的 `crates/typst-cli`，使用 `cargo run -p typst-cli -- compile 输入.typ 输出.pdf` 来触发一次真实的 `compile::<PagedDocument>` 调用，观察控制台输出的告警与错误。具体运行结果待本地验证。

## 6. 本讲小结

- `compile` 是 `typst` crate 对外的公开编译入口，签名是 `pub fn compile<T>(world: &dyn World) -> Warned<SourceResult<T>> where T: Output`。
- 它是个薄外壳：建 `Sink` → 调内部 `compile_impl` → 出错时 `deduplicate` 去重 → 包成 `Warned` 返回。
- 泛型参数 `T: Output` 决定产物类型，目前有 `PagedDocument`（分页，对应 PDF/SVG/PNG）和 `HtmlDocument`（HTML）两种，调用时用 `compile::<PagedDocument>(&world)` 指明。
- `Warned<T>` 把「产物」和「告警」并列捆绑；在 `compile` 里 `T` 本身又是 `SourceResult`，所以返回值是两层嵌套——外层管告警，内层管成功/失败。
- `SourceResult<T> = Result<T, EcoVec<SourceDiagnostic>>`，错误是「一批」而非一个，支持一次汇报多个错误。
- `SourceDiagnostic` 是错误与告警的统一载体，五字段 `severity/span/message/trace/hints`，靠 `severity` 区分致命错误（`Error`）与告警（`Warning`）。
- `#[typst_macros::time]` 是计时注解，在启用追踪时记录 `compile` 的耗时。

## 7. 下一步学习建议

本讲只看了 `compile` 的「外壳」，真正干活的内部函数 `compile_impl` 还是个黑盒。下一步建议进入 **第二单元**：

- **u2-l1 `compile_impl` 编译主流程精读**：逐段精读 `compile_impl`——取 `library`、按 `target` 做特性门控、构造基础样式链、取出主源码、调用 `typst_eval::eval` 得到 `content`、进入稳定化循环、最后提升延迟错误。这是理解 Typst 编译器「四步流水线如何被串起来」的关键。
- **u2-l4 编译上下文 Engine / Sink / Route / Traced**：本讲多次提到的 `Sink`（告警收集器）和 `Traced`，将在这一讲与 `Engine`、`Route` 一起系统讲解。

如果你对「为什么 Typst 需要反复布局」更感兴趣，也可以先跳读 **u2-l2 内省稳定化循环**，但建议先有 u2-l1 的主流程地图作铺垫。

此外，推荐你顺手读一读 `crates/typst-library/src/diag.rs` 顶部的 `bail!` / `error!` / `warning!` 宏定义，理解 `SourceDiagnostic` 在项目里最常用的构造方式——这会让你在后续阅读任何一处错误处理代码时都更轻松。
