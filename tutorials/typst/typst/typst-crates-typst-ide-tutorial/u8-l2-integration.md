# 集成实践与架构取舍

## 1. 本讲目标

本讲是整本学习手册的收尾篇。前面 26 篇讲义已经把 typst-ide 的每个内部模块（补全、悬停、跳转定义、双向跳转、表达式分析）逐一拆解过；本讲换一个**面向集成者**的视角：如果你要自己写一个 Typst 语言服务器（LSP）或编辑器插件，把 typst-ide 接进去，到底要做什么、要注意什么。

读完本讲你应该能够：

- 说出实现 `IdeWorld` 的「最小方式」（只实现必填的 `upcast`）与「增强方式」（再实现可选的 `packages` / `files`），以及二者分别解锁了哪些能力。
- 理解 `AsOutput` 这个跨 crate 适配 trait 为什么存在，以及它如何让 LSP 功能复用「上一次编译的产物」而不必为每个功能重新编译。
- 对照一张「能力 × 依赖」表，判断补全、悬停、跳转定义、点击跳转四项功能在缺少 `output` / `packages` / `files` 时会如何优雅降级。
- 理解 `jump.rs` 里两个 sealed trait（`JumpFromDocument` / `JumpInDocument`）为什么用「公开空壳 + 私有 supertrait」封死外部扩展，以及这种封闭性带来的架构取舍。
- 领会贯穿 typst-ide 全库的 **best-effort 哲学**：宁可少给一点信息，也不要让功能整体失败。

---

## 2. 前置知识

本讲是「集成视角」，需要你已经理解 typst-ide 的内部结构。请确认你掌握了以下前置讲义中的概念（本讲不会重复展开它们的内部实现，只从外部调用方的角度使用它们）：

- **u1-l2（IdeWorld）**：`IdeWorld: World` 这个 supertrait 的三个方法 `upcast`（必填）、`packages` / `files`（可选，带空默认实现）。本讲会从「实现者」角度再次审视它。
- **u7-l1（jump_from_click 入口）**：`Jump` 枚举、sealed trait `JumpFromDocument` 的「公开空壳 + 私有 supertrait」两层结构、关联类型 `type Position`。本讲会把它的设计意图上升为「封闭后端集合」的架构取舍。
- **u8-l1（测试体系）**：`TestWorld` 是一个最小但完整的 `World + IdeWorld` 实现，本讲会把它当作「最小集成」的现实范例来引用。

此外需要一点 LSP（Language Server Protocol）的常识：语言服务器通常在一个常驻进程里维护项目状态，并在每次文档变更后重新编译，把编译结果缓存起来供补全、悬停、跳转等功能查询。typst-ide 的设计正是为了契合这种「**复用上一次编译产物**」的工作模式。

几个本讲会反复用到的术语：

- **output / 编译产物**：指 `typst::compile(world)` 得到的 `PagedDocument` 或 `HtmlDocument`。它实现了 `Output` trait，带一个 introspector（自省器），能回答「某个标签/位置对应文档里的哪个元素」。
- **降级（degrade gracefully）**：某项可选输入缺失时，功能不报错、不崩溃，只是少返回一些结果（例如包补全为空）。
- **sealed trait**：一种 Rust 惯用法，让一个 trait 只能在定义它的 crate 内被实现，外部 crate 无法自己 `impl`。

---

## 3. 本讲源码地图

本讲聚焦三个文件，外加一个跨 crate 的 trait 定义：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [src/lib.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/lib.rs) | 模块声明、再导出、`IdeWorld` trait 定义 | 集成者要实现的「数据契约」边界 |
| [src/jump.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/jump.rs) | 双向跳转（点击↔源码） | sealed trait 的封闭设计 |
| [Cargo.toml](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/Cargo.toml) | 依赖声明 | 看 typst-ide 依赖了哪些 crate，理解分工 |
| `crates/typst-library/src/foundations/target.rs` | `Output` / `AsOutput` / `Target` 定义 | `AsOutput` 为何存在 |

另外会从外部调用方角度引用 `tooltip.rs`、`complete.rs`、`definition.rs` 的公共函数签名，但只看签名、不展开内部。

---

## 4. 核心概念与源码讲解

### 4.1 IdeWorld 实现：最小方式与增强方式

#### 4.1.1 概念说明

集成 typst-ide 的第一步，是实现它的统一数据契约 `IdeWorld`。回顾 u1-l2：编译器只需要 `World`（按 id 取单个资源），而 IDE 还要**枚举**全部候选，所以 typst-ide 在 `World` 之上加了一层 `IdeWorld`。

对集成者来说，关键认知是：`IdeWorld` 的三个方法里，**只有一个必填，另外两个都是可选增强**。这意味着集成门槛可以非常低——你可以先用最小实现跑起来，再按需增强。

#### 4.1.2 核心流程

集成者的决策流程：

```
┌─────────────────────────────────────────────┐
│ 1. 你的项目已经有了一个 World 实现（编译用）？ │
│    是 → 进入第 2 步                          │
└─────────────────────────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────┐
│ 2. 为它 impl IdeWorld                        │
│    - upcast(&self) -> &dyn World { self }    │  ← 必填，一行搞定
│    - packages() → 默认 &[]                   │  ← 可选，先不实现
│    - files()    → 默认 vec![]                │  ← 可选，先不实现
└─────────────────────────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────┐
│ 3. 按需增强：                                │
│    - 想要「@包名 补全」？→ 实现 packages()    │
│    - 想要「路径补全」？   → 实现 files()      │
└─────────────────────────────────────────────┘
```

#### 4.1.3 源码精读

trait 定义在 [src/lib.rs:24-51](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/lib.rs#L24-L51)，三个方法分工清晰：

```rust
pub trait IdeWorld: World {
    // 必填：实现体就一行 self
    fn upcast(&self) -> &dyn World;

    // 可选：默认空切片，缺失只让「包名补全」为空
    fn packages(&self) -> &[(PackageSpec, Option<EcoString>)] {
        &[]
    }

    // 可选：默认空 vec，缺失只让「文件路径补全」为空
    fn files(&self) -> Vec<FileId> {
        vec![]
    }
}
```

- `upcast` 是必填方法（无默认实现），其注释说明原因（[src/lib.rs:26-32](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/lib.rs#L26-L32)）：Rust 当时的 trait 向上转型（`&dyn IdeWorld` → `&dyn World`）尚未稳定。它被内部两处真正消费——构造临时引擎的 `with_engine`（[src/utils.rs:30](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/utils.rs#L30)）和重跑文档求值的 `trace`（[src/analyze.rs:45](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/analyze.rs#L45)），二者都需拿到确切的 `&dyn World` 去 `track`。
- `packages` 带默认 `&[]`（[src/lib.rs:40-42](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/lib.rs#L40-L42)），其文档点明数据来源（`@preview` 命名空间可从 `https://packages.typst.org/preview/index.json` 获取）。
- `files` 带默认 `vec![]`（[src/lib.rs:48-50](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/lib.rs#L48-L50)）。

这两个可选方法的「唯一消费者」可以在补全模块里精确定位：

- `packages()` 只被 `package_completions` 消费（[src/complete.rs:1138-1139](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1138-L1139)）：`let mut packages: Vec<_> = self.world.packages().iter().collect();`
- `files()` 只被 `file_completions` 消费（[src/complete.rs:1156-1166](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1156-L1166)）：`self.world.files().iter()...`

这就是「可选增强」在代码里的落地：调用方直接对返回值 `.iter()`，缺失时迭代空集合，自然产出零条候选，既不报错也不影响其他功能。

> 现实范例：本仓库的测试基础设施 `TestWorld`（u1-l3、u8-l1）就是一个完整的 `IdeWorld` 实现，集成者可以直接照抄它的骨架。

#### 4.1.4 代码实践

**实践目标**：动手感受「最小实现 vs 增强实现」的差异。

**操作步骤**：

1. 阅读 [src/lib.rs:24-51](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/lib.rs#L24-L51)，确认 `upcast` 无默认实现、另两个有默认实现。
2. 假设你有一个 `struct MyWorld { ... }` 且已 `impl World for MyWorld`，请在本子目录之外（**示例代码，非项目原有代码**）写一段最小 `impl IdeWorld`：

   ```rust
   // 示例代码：最小 IdeWorld 实现
   impl typst_ide::IdeWorld for MyWorld {
       fn upcast(&self) -> &dyn typst::World { self }
       // packages / files 不写，沿用默认空实现
   }
   ```

3. 再写一个「增强版」，假设你能枚举项目里所有 `.typ` 文件：

   ```rust
   // 示例代码：增强 IdeWorld 实现
   impl typst_ide::IdeWorld for MyWorld {
       fn upcast(&self) -> &dyn typst::World { self }

       fn files(&self) -> Vec<typst::syntax::FileId> {
           self.known_typ_files.clone()   // 你自己维护的文件清单
       }
   }
   ```

**需要观察的现象**：最小实现下，调用 `autocomplete` 在 `#include "` 之后**得不到任何路径候选**（`files()` 返回空）；增强实现后，同位置会出现相对路径候选。

**预期结果**：`package_completions` 与 `file_completions` 是这两条可选通道的唯一出口；不实现对应方法，对应补全为空，其余功能（悬停、跳转、定义）完全正常。**待本地验证**：在你的集成项目里实际触发一次路径补全，确认候选列表随 `files()` 的返回内容变化。

#### 4.1.5 小练习与答案

**练习 1**：某集成者实现了 `upcast` 但忘了实现 `packages()`。下列哪些功能会受影响？(a) 悬停提示 (b) `#import "@preview/..."` 的包名补全 (c) 跳转定义 (d) 字体补全。

**参考答案**：只有 (b) 受影响（`packages()` 唯一消费者是 `package_completions`）。(a)(c) 不依赖 `packages()`；(d) 字体补全依赖必填的 `World::book()`，与 `packages()` 无关。

**练习 2**：为什么 `upcast` 不能也做成带默认实现的可选方法？

**参考答案**：因为默认实现需要返回 `&dyn World`，而在 trait upcasting 稳定前，trait 内部无法把 `self`（`&dyn IdeWorld`）自动转成 `&dyn World`；这个转换必须由「知道具体类型」的实现者手动写出 `self`（此时 `self` 的具体类型已知，可直接解引用为 `&dyn World`）。所以它只能是一个必填、由实现者兜底的方法。

---

### 4.2 AsOutput：让 LSP 功能复用上一次编译产物

#### 4.2.1 概念说明

LSP 的典型工作流是「文档一变更就重新编译」。typst-ide 的补全、悬停、跳转定义都需要一个 introspector（自省器）来回答「这个标签/位置对应文档里的什么」——而这个 introspector 就藏在**编译产物**里。

问题来了：编译产物有两种（分页的 `PagedDocument`、HTML 的 `HtmlDocument`），它们都实现了 `Output` trait。typst-ide 的公共函数想用一个统一签名接收「任意一种产物，而且可以是 `None`」。`AsOutput` 这个跨 crate 的小适配 trait 就是为这个需求而生的。

#### 4.2.2 核心流程

```
你的 LSP 进程
   │
   │  typst::compile(world) ──► PagedDocument（impl Output）
   │                              │
   │                              │ 缓存起来
   │                              ▼
   │  每次补全/悬停/跳转请求：
   │     tooltip(world, Some(&doc), source, cursor, side)
   │                         ▲
   │                         │
   │              这里需要一个「能被当成 &dyn Output」的参数
   │
   └─► AsOutput：让 &PagedDocument / &HtmlDocument / &dyn Output
        都能被同一个泛型函数接受
```

关键点：产物是 `Option` 的——**「有则增强、无则降级」**。`None` 时，依赖产物的子功能（如标签补全、引用跳转）直接跳过。

#### 4.2.3 源码精读

`AsOutput` 定义在 typst-library，而非 typst-ide（[crates/typst-library/src/foundations/target.rs:48-51](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/target.rs#L48-L51)）：

```rust
pub trait AsOutput {
    fn as_output(&self) -> &dyn Output;
}
```

它只有一行：把 `&self` 擦除成 `&dyn Output` trait 对象。配两条 blanket impl（[crates/typst-library/src/foundations/target.rs:53-63](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/target.rs#L53-L63)）覆盖所有情况：

```rust
impl AsOutput for &dyn Output { ... }   // 已经是 trait 对象
impl<T: Output> AsOutput for &T { ... } // 任意具体产物的引用
```

`Output` trait 本身（[crates/typst-library/src/foundations/target.rs:13-30](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/target.rs#L13-L30)）要求实现者提供 `introspector(&self) -> &dyn Introspector`——这正是 IDE 功能真正要用的东西。`PagedDocument`（[crates/typst-layout/src/document.rs:63](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/document.rs#L63)）和 `HtmlDocument`（[crates/typst-html/src/dom.rs:81](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs#L81)）各自实现了它。

三个公共 IDE 函数都用同一个签名模式 `output: Option<impl AsOutput>`：

- `tooltip`（[src/tooltip.rs:24-30](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tooltip.rs#L24-L30)）
- `autocomplete`（[src/complete.rs:37-43](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L37-L43)）
- `definition`（[src/definition.rs:27-33](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/definition.rs#L27-L33)）

`AsOutput` 文档（[crates/typst-library/src/foundations/target.rs:40-47](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/foundations/target.rs#L40-L47)）解释了为何不直接写 `&impl Output`：在泛型函数里，`&impl Output` 无法被转成 `&dyn Output`；直接收 `&dyn Output` 又不够方便（尤其是「可选」场景）。于是折中成 `impl AsOutput`，由调用方在传参时做擦除。

> 为什么强调「上一次编译产物」：introspector 只有在文档完整编译后才有意义（它需要知道所有标签的最终位置）。所以 IDE 不会自己编译，而是**复用** LSP 进程里已经缓存的那份产物。这就是「集成者负责编译并缓存，typst-ide 负责查询」的分工。

#### 4.2.4 代码实践

**实践目标**：理解 `output` 参数怎么传、传与不传的差别。

**操作步骤**：

1. 看 jump.rs 里 `analyze_labels` 的签名（[src/analyze.rs:104](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/analyze.rs#L104)）：`pub fn analyze_labels(output: impl AsOutput)`——注意它是**必填**（非 `Option`），因为标签分析离开产物毫无意义。
2. 对比三个公共函数的 `output: Option<impl AsOutput>`——它们都允许 `None`。
3. 在 jump.rs 的测试里看集成者实际怎么传产物（**项目原有代码**，[src/jump.rs:533-543](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/jump.rs#L533-L543)）：

   ```rust
   let world = world.acquire();
   let world = world.borrow();
   let doc: PagedDocument = typst::compile(world).output.unwrap();  // 编译得到产物
   let jump = jump_from_click(world, &doc, &PagedPosition { ... }); // 把 &doc 当后端传入
   ```

**需要观察的现象**：测试先 `typst::compile` 拿到 `doc`，再把 `&doc` 传给跳转函数——这正是「复用上一次编译产物」的微缩版（只是这里每次测试现编一份）。

**预期结果**：`&doc`（类型 `&PagedDocument`）能被 `jump_from_click` 的泛型参数 `D: JumpFromDocument` 接受，背后靠的就是 `PagedDocument` 同时实现了 `Output` 与 `JumpFromDocument`。

#### 4.2.5 小练习与答案

**练习 1**：为什么不把三个公共函数的签名写成 `output: &dyn Output`（非 Option）？

**参考答案**：那样调用方就**必须**先编译出一份产物才能调用，无法支持「还没编译过/编译失败」的场景。用 `Option<impl AsOutput>` 让 `None` 成为合法输入，对应功能直接降级跳过，契合 best-effort 哲学。

**练习 2**：`AsOutput` 有两个 blanket impl（`for &dyn Output` 和 `for &T where T: Output`）。一个具体的 `&PagedDocument` 会命中哪一个？

**参考答案**：命中 `impl<T: Output> AsOutput for &T`（`T = PagedDocument`）。`&dyn Output` 那条用于你已经手工擦除成 trait 对象的情况。

---

### 4.3 可选 output / packages / files 的优雅降级

#### 4.3.1 概念说明

「降级」是 typst-ide 最核心的架构特征之一。它把每一项 IDE 能力拆成「基础部分」（只需要 `IdeWorld`，甚至只需要 `World`）和「增强部分」（需要 `output`、`packages` 或 `files`）。缺失增强输入时，基础部分照常工作，增强部分静默跳过。

对集成者来说，这意味着可以**分阶段交付**：先把不依赖产物的功能上线，等编译缓存管道就绪后再补上依赖产物的功能。

#### 4.3.2 核心流程

下表是四项核心功能对三类「可选输入」的依赖关系（综合前序讲义与本讲源码核实）：

| 功能 | 公共入口 | 需要 `output`（编译产物）？ | 需要 `packages()`？ | 需要 `files()`？ |
| --- | --- | --- | --- | --- |
| 补全 | `autocomplete` | 仅标签补全需要（[complete.rs:37](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L37)) | 包名补全需要（[complete.rs:1139](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1139)) | 路径补全需要（[complete.rs:1162](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1162)) |
| 悬停 | `tooltip` | 仅标签 tooltip 需要（[tooltip.rs:38](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tooltip.rs#L38)) | 否 | 否 |
| 跳转定义 | `definition` | 仅 `@ref` 引用跳转需要（[definition.rs:29](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/definition.rs#L29)) | 否 | 否 |
| 点击↔源码跳转 | `jump_from_click` / `jump_from_cursor` | **必须有产物**（跳转对象就是产物本身） | 否 | 否 |

降级在代码里的典型写法是用 `?` 提前返回。以 tooltip 为例（[src/tooltip.rs:36-42](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tooltip.rs#L36-L42)）：

```rust
named_param_tooltip(world, &leaf)
    .or_else(|| font_tooltip(world, &leaf))
    .or_else(|| label_tooltip(output?, &leaf))   // ← output 为 None 时这里短路返回 None
    .or_else(|| import_tooltip(world, &leaf))
    ...
```

`label_tooltip(output?, &leaf)` 里的 `output?`：当 `output` 是 `None`，整个 `or_else` 闭包返回 `None`，分发链继续尝试下一个分支——也就是说**标签 tooltip 静默消失，但其他 tooltip 照常工作**。

#### 4.3.3 源码精读

「降级」在源码里有三种典型形态：

1. **`Option` + `?` 短路**：如上 `label_tooltip(output?, ...)`。`definition` 的 `Ref` 分支同理——它把 `output` 当成必需，缺失时引用跳转直接失败（详见 u4-l1）。
2. **默认空集合**：`packages()` / `files()` 返回空时，消费方 `.iter()` 得到零条候选，不报错。
3. **best-effort 返回 `None`**：jump.rs 的命中检测里，对不可逆变换、clip 外落点、零厚度描边一律 `continue` 或返回 `None`（详见 u7-l2），是同一种哲学在算法层的体现。

注意一个**例外**：点击↔源码跳转（`jump_from_click` / `jump_from_cursor`）**没有** `output: Option<...>` 参数——它直接接收 `&D`（文档本身）。因为跳转的「对象」就是产物，没有产物就无从跳转，所以这里不存在「降级」，而是「要么有、要么不调用」。

#### 4.3.4 代码实践

**实践目标**：亲手验证「缺 output 时降级、有 output 时增强」。

**操作步骤**：

1. 打开 [src/tooltip.rs:36-42](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tooltip.rs#L36-L42)，确认分发链里只有 `label_tooltip` 一处用到 `output?`。
2. 思考（不必运行）：若集成者第一次还没编译过文档，用户把鼠标悬停在 `@my-fig` 上，调用 `tooltip(world, None, source, cursor, side)` 会发生什么？

**需要观察的现象**：`output?` 让 `label_tooltip` 闭包立即返回 `None`，于是 `or_else` 链跳过它，继续尝试 `import_tooltip`、`expr_tooltip` 等。

**预期结果**：在 `@my-fig` 这种纯引用节点上，`expr_tooltip` 等也多半落空，最终 `tooltip` 返回 `None`——即「没有提示」，而不是报错。等集成者接入编译缓存、传入 `Some(&doc)` 后，同一位置就会出现标签详情。

#### 4.3.5 小练习与答案

**练习 1**：一个集成者想尽快上线「悬停」功能，但编译缓存还没做好。他能用 `tooltip(world, None, ...)` 吗？会损失什么？

**参考答案**：能。会损失且仅损失**标签 tooltip**（`label_tooltip`，依赖 `output?`）；字体、import、表达式、闭包等其余 tooltip 都正常工作。

**练习 2**：为什么 `jump_from_click` 不像 `tooltip` 那样把产物设计成 `Option`？

**参考答案**：跳转的输入和对象就是渲染产物本身（你要点的就是文档里的某个位置）。没有产物就根本不存在「点击」这件事，所以它不是「降级」问题，而是「前置条件不满足就不调用」。

---

### 4.4 sealed trait 边界：封死输出后端的扩展

#### 4.4.1 概念说明

`jump.rs` 用了两个 sealed trait：`JumpFromDocument`（点击→源码，u7-l1）和 `JumpInDocument`（源码→渲染位置，u7-l3）。本讲关注的是**它们为什么被封死**——这是一个有意识的架构取舍。

sealed trait 的含义：trait 在公开模块里只是一个空壳，真正的方法签名藏在同名的**私有**子模块里（`jump_from_document_sealed`）。外部 crate 能看到公开 trait 的名字，也能调用它的方法，但**无法为它提供新的实现**，因为它要求同时实现那个私有 supertrait，而私有东西外部碰不到。

#### 4.4.2 核心流程

```
公开层（外部可见）                      私有层（仅 crate 内可见）
─────────────────────                ──────────────────────────
pub trait JumpFromDocument:          mod jump_from_document_sealed {
    jump_from_document_sealed::          pub trait JumpFromDocument {
    JumpFromDocument {}        ◄────────── type Position;
                                          fn resolve_position(...) -> Option<Jump>;
                                      }
                                  }
impl JumpFromDocument for PagedDocument {}   ← 只有这两个实现
impl JumpFromDocument for HtmlDocument {}   ← 外部无法加第三个
```

外部想 `impl JumpFromDocument for MyDocument`？编译器会报错：因为那需要同时 `impl jump_from_document_sealed::JumpFromDocument`，而后者是私有的。

#### 4.4.3 源码精读

公开空壳与私有 supertrait 的两层结构在 [src/jump.rs:42-49](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/jump.rs#L42-L49)：

```rust
pub trait JumpFromDocument: jump_from_document_sealed::JumpFromDocument {}

// The actual implementations are in the sealed trait.
impl JumpFromDocument for PagedDocument {}
impl JumpFromDocument for HtmlDocument {}
```

- 公开 trait 体是空的 `{}`，自身不暴露任何方法。
- 它把私有 `jump_from_document_sealed::JumpFromDocument`（[src/jump.rs:50-68](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/jump.rs#L50-L68)）列为 supertrait，真正的 `type Position` 和 `resolve_position` 都在那里。
- 只为 `PagedDocument` 和 `HtmlDocument` 各写了一行 `impl`（[src/jump.rs:47-48](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/jump.rs#L47-L48)）。注释 `// The actual implementations are in the sealed trait.` 点明了这种「壳与肉分离」的写法。

反方向 `JumpInDocument` 完全对称（[src/jump.rs:367-371](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/jump.rs#L367-L371) 与私有 [src/jump.rs:374-390](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/jump.rs#L374-L390)），同样只对这两种后端实现。

**这个取舍的好处与代价**：

- 好处一：**关联类型 `type Position` 把「文档类型 ↔ 位置类型」的配对交给类型系统静态保证**。传 `PagedDocument` 必配 `PagedPosition`，传 `HtmlDocument` 必配 `HtmlPosition`（[src/jump.rs:70-71](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/jump.rs#L70-L71)、[src/jump.rs:84-85](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/jump.rs#L84-L85)），编译期就查得出类型错配。
- 好处二：**公共入口 `jump_from_click<D: JumpFromDocument>` 经单态化实现零成本抽象**（[src/jump.rs:33-40](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/jump.rs#L33-L40)），函数体只有一行 `document.resolve_position(...)`，没有运行时分派开销。
- 好处三：**封闭后端集合**，typst-ide 保留演进自由——将来若新增第三种输出后端，可以在这个 crate 内部加 `impl`，而不用担心外部已经写了冲突实现。
- 代价：**外部集成者无法为自定义的文档后端实现跳转**。如果你发明了一种全新的渲染产物，没法让它接入 `jump_from_click`。这是「封闭性」换「类型安全 + 演进自由」的取舍。

#### 4.4.4 代码实践

**实践目标**：从代码层面确认「外部无法扩展」这一封闭性。

**操作步骤**：

1. 读 [src/jump.rs:42-49](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/jump.rs#L42-L49)，注意公开 trait 体为空、supertrait 指向私有模块。
2. 读私有 supertrait [src/jump.rs:50-68](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/jump.rs#L50-L68)，确认 `type Position` 与 `resolve_position` 都在这里。
3. 思考（**示例代码，非项目原有代码**）：如果你在外部 crate 写：

   ```rust
   // 示例代码：尝试在外部 crate 扩展后端（预期编译失败）
   struct MyDoc;
   impl typst_ide::JumpFromDocument for MyDoc { ... }
   ```

   编译器会提示你必须实现 `jump_from_document_sealed::JumpFromDocument`，但该 trait 不可见——编译失败。

**需要观察的现象**：无法为 `MyDoc` 写出合法的 `impl`。

**预期结果**：确认 typst-ide 的输出后端集合被封闭在 `{PagedDocument, HtmlDocument}` 两个之内。**待本地验证**：若你有兴趣，可在一个依赖 typst-ide 的外部 crate 里尝试上面的 `impl`，观察编译错误信息。

#### 4.4.5 小练习与答案

**练习 1**：如果改用 `enum Backend { Paged, Html }` 加运行时 `match` 来替代 sealed trait，会失去什么？

**参考答案**：会失去「关联类型带来的静态配对保证」和「单态化零成本」。用 enum 时，位置类型只能统一成某种 `enum Position`，调用方拿到后还得 `match`，类型系统无法再保证「`PagedDocument` 必配 `PagedPosition`」。sealed trait 用编译期多态换来了更强的类型安全与零运行时开销。

**练习 2**：sealed 模式为什么用「公开空壳 trait + 私有 supertrait」两件套，而不是直接把 trait 整个设为私有？

**参考答案**：因为公共函数 `jump_from_click<D: JumpFromDocument>` 的泛型约束要让外部「看得见 trait 的名字」才能调用；但又要「不让外部实现它」。空壳公开满足前者，私有 supertrait 满足后者——二者缺一不可。

---

### 4.5 best-effort 哲学：贯穿全库的设计基调

#### 4.5.1 概念说明

把前四个模块串起来看，typst-ide 有一条贯穿始终的设计基调：**best-effort（尽力而为）**。它的含义是：IDE 功能面对的是「用户正在编辑、可能还不完整、甚至有语法错误」的源码，所以绝不能因为某条路径走不通就抛错或崩溃；正确的做法是「能给出多少信息就给多少，给不出就安静地返回 `None` / 空集」。

这条基调决定了集成者的体验：typst-ide 几乎所有公共函数都返回 `Option` 或 `Vec`，**永远不会 panic**（除非是内部 bug）。这让集成者可以放心地在每次按键后调用它们。

#### 4.5.2 核心流程

best-effort 在 typst-ide 里的三种落地形态：

```
best-effort 的三种形态
├── 1. 可选输入降级   ── output/packages/files 缺失 → 该子功能静默跳过（4.3）
├── 2. 静默失败       ─── 找不到目标 / 变换不可逆 / 值推断不出 → 返回 None（4.5）
└── 3. 内部兜底回退   ─── 局部落空 → 回退到更宽的策略（如标准库 globals 兜底）
```

#### 4.5.3 源码精读

best-effort 在 jump.rs 里尤为密集。`jump_from_click_in_frame` 是命中检测的核心，它面对各种「找不到」都选择优雅退出（[src/jump.rs:208-331](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/jump.rs#L208-L331)）。几个典型点：

- 不可逆变换直接放弃这个 group：`let Some(inv_transform) = group.transform.invert() else { continue; };`（[src/jump.rs:249-251](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/jump.rs#L249-L251)），注释 `// Realistic transforms should always be invertible.` 说明这是对极端情况的防御。
- clip 外的落点跳过：`if !clip.contains(...) { continue; }`（[src/jump.rs:241-245](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/jump.rs#L241-L245)）。
- 遍历完一无所获就返回 `None`（[src/jump.rs:330](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/jump.rs#L330)）。

把 `Span` 还原成文件偏移的 `Jump::from_span` 同样 best-effort：`span.id()?` 和 `world.range(span)?` 任意一步落空（比如 detached span）就返回 `None`（[src/jump.rs:25-31](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/jump.rs#L25-L31)）。

而在 `definition` 里，best-effort 体现为「**多级回退**」：先查 `named_items`（作用域），落空再 `analyze_expr`（运行时值），再落空用 `globals`（标准库兜底）（详见 u4-l1/u4-l2）。每一级都是「上一级没招了就试试下一级」，最终都没招才返回 `None`。

> 这种哲学对集成者的启示：你**不需要**在调用 typst-ide 之前先把源码修补完整或保证编译成功。即便文档处于半成品状态，typst-ide 也会尽量给出它能给出的提示。

#### 4.5.4 代码实践

**实践目标**：在「半成品源码」上观察 best-effort 行为。

**操作步骤**：

1. 读 jump.rs 测试里那些**期望 `None`** 的用例，例如点击空白处（[src/jump.rs:580-581](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/jump.rs#L580-L581)）：`test_click(s, point(0.0, 0.0), None);` 和 `test_click(s, point(70.0, 5.0), None);`——这两处点击不命中任何有 span 的图元，函数安静返回 `None`，而不是报错。
2. 读 clip 测试里期望 `None` 的用例（[src/jump.rs:634-638](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/jump.rs#L634-L638)）：点击被 `clip: true` 裁掉的子矩形内部，返回 `None`。

**需要观察的现象**：所有「点击不到有效目标」的场景，断言都是 `None`，函数从不 panic。

**预期结果**：确认 best-effort = 「尽力找，找不到就安静返回 `None`」。集成者可以安全地在任意时刻调用这些函数。

#### 4.5.5 小练习与答案

**练习 1**：best-effort 与「返回错误码/异常」相比，对 LSP 集成有什么好处？

**参考答案**：LSP 在每次按键后都可能调用补全/悬停，源码经常是不完整或带错的。best-effort 让函数在这些情况下返回 `None`/空集而非报错，集成者无需写大量 try/catch 或错误兜底逻辑，直接「没结果就不显示」即可。

**练习 2**：在 `definition` 的多级回退里，标准库 `globals` 被放在最后一级。为什么这个顺序本身也体现了 best-effort？

**参考答案**：因为它优先信任「更具体、更接近用户」的来源（作用域绑定 → 运行时值），只有这些都没招时才回退到「最通用」的标准库。这样即便用户用 `#let table = ...` 重定义了 `table`，也能跳到用户自己的定义而非标准库——「给最贴切的结果」正是 best-effort 的目标。

---

## 5. 综合实践

设计一个最小 LSP 服务方案，把本讲四个最小模块串起来。

**任务背景**：你要为自己的编辑器写一个 Typst 语言服务器，需要支持「补全、悬停、跳转定义、点击跳转」四项功能。请完成下面这张「集成清单」（建议用纸笔或文档填，不必写代码）：

1. **`IdeWorld` 实现计划**（对应 4.1）：
   - 你的 `World` 实现叫什么？为它写一行 `impl IdeWorld` 的 `upcast`（预期就一行 `self`）。
   - 你打算第一版就实现 `packages()` / `files()` 吗？如果先不实现，明确说出会损失哪两类补全（答案应指向 [complete.rs:1139](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1139) 与 [complete.rs:1162](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1162)）。

2. **编译产物缓存计划**（对应 4.2、4.3）：
   - 你的 LSP 进程会在每次文档变更后 `typst::compile` 得到一个 `PagedDocument`（或 `HtmlDocument`）。说明你会把它缓存起来。
   - 填下面这张「降级表」，标注每项功能在「还没编译过（`output=None`）」时是否可用、损失了什么：

   | 功能 | `output=None` 时是否可用？ | 损失了什么 |
   | --- | --- | --- |
   | 补全 | | |
   | 悬停 | | |
   | 跳转定义 | | |
   | 点击↔源码跳转 | | |

3. **后端选择计划**（对应 4.4）：
   - 你选 `PagedDocument` 还是 `HtmlDocument` 作为后端？确认二者都已实现 `Output` 与 `JumpFromDocument`（[crates/typst-layout/src/document.rs:63](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-layout/src/document.rs#L63)、[crates/typst-html/src/dom.rs:81](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-html/src/dom.rs#L81)）。
   - 思考：你能否为一种「全新的自定义渲染产物」接入 `jump_from_click`？结合 sealed trait 解释为什么（预期答案：不能，后端集合被封闭）。

4. **错误处理计划**（对应 4.5）：
   - 写一句话说明：因为 typst-ide 是 best-effort，你的 LSP 在调用这些函数时**不需要**做什么防御性处理？（预期：不需要 try/catch，函数会返回 `None`/空集，直接「没结果就不显示」即可。）

**参考要点（填完后再对照）**：

- 补全在 `output=None` 时仍可用，仅损失**标签补全**（`analyze_labels` / `label_completions` 依赖产物）；包名/路径补全是否可用取决于你是否实现了 `packages()`/`files()`。
- 悬停在 `output=None` 时仍可用，仅损失**标签 tooltip**（[tooltip.rs:38](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tooltip.rs#L38) 的 `output?`）。
- 跳转定义在 `output=None` 时仍可用，仅损失 **`@ref` 引用跳转**（`Ref` 分支）。
- 点击↔源码跳转**必须有产物**——它不是降级，而是「没产物就不调用」。

---

## 6. 本讲小结

- `IdeWorld` 的集成门槛很低：必填方法只有 `upcast`（实现体一行 `self`），`packages()` / `files()` 是可选增强，分别只被 `package_completions` 与 `file_completions` 消费，缺失只让对应补全为空。
- `AsOutput` 是跨 crate 的擦除适配 trait，把 `&PagedDocument` / `&HtmlDocument` / `&dyn Output` 统一成同一个泛型参数；三个公共 IDE 函数都用 `output: Option<impl AsOutput>`，让 LSP 能复用「上一次编译产物」而不必为每个功能重新编译。
- 「优雅降级」是 typst-ide 的核心特征：`output` / `packages` / `files` 任意缺失都不报错，只让依赖它的子功能静默跳过；补全、悬停、跳转定义的基础部分始终可用，点击跳转则必须先有产物。
- `jump.rs` 的两个 sealed trait（`JumpFromDocument` / `JumpInDocument`）用「公开空壳 + 私有 supertrait」封死了输出后端集合——只接受 `PagedDocument` 与 `HtmlDocument`，外部无法扩展；代价是失去了自定义后端的能力，换来关联类型的静态配对保证与单态化零成本。
- best-effort 哲学贯穿全库：找不到目标、变换不可逆、值推断不出都返回 `None` / 空集，从不 panic；集成者因此可以在每次按键后安全调用，无需额外的错误兜底。
- 总体架构取舍可以概括为：**实现私有、接口精简、输入可选、失败静默、后端封闭**——typst-ide 把复杂性留在内部，对外只暴露一组宽容、稳定、可渐进集成的 API。

---

## 7. 下一步学习建议

本讲是 typst-ide 学习手册的最后一篇。如果你已经读到这里，建议：

- **动手集成**：参考本仓库的测试设施 `TestWorld`（[src/tests.rs](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tests.rs)）写一个最小的 `IdeWorld` 实现，分别调用 `autocomplete` / `tooltip` / `definition` / `jump_from_click`，观察它们在「有 output」与「无 output」下的输出差异。
- **阅读真实集成者**：typst 生态里成熟的语言服务器（如 tinymist、typst-lsp）是 typst-ide 的主要消费者。阅读它们如何实现 `IdeWorld`、如何缓存编译产物、如何把 typst-ide 的返回值翻译成 LSP 消息，能让你把本讲的「集成清单」落到真实代码上。
- **回顾调用链**：本讲是横向视角；若想纵向加深，可回到 u5/u6（补全引擎）或 u7（双向跳转）重新走一遍某个具体功能的内部实现，体会「外部宽容的 API」与「内部细致的多级回退」之间的对应关系。
- **关注演进**：本讲提到的「trait upcasting 在 Rust 1.86 稳定」「`AsOutput` 的存在意义」等都随 Rust 语言与 typst 版本演进。若你使用的 typst 版本与本讲 HEAD（`146a5832`）不同，请先用 `git log` / `git diff` 核对 `lib.rs` 与 `jump.rs` 是否有结构变化，再对照本讲阅读。
