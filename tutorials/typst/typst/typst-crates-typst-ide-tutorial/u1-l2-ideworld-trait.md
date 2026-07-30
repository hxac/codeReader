# IdeWorld —— IDE 功能的数据契约

## 1. 本讲目标

承接上一讲（u1-l1），我们已经知道 typst-ide 的所有公共函数（`autocomplete`、`tooltip`、`definition`、`jump_from_click` 等）的第一个参数几乎都是 `&dyn IdeWorld`。`IdeWorld` 就是这些函数与外部世界之间的「数据契约」。

本讲把这个契约本身拆开，让你学会：

1. 说清 `IdeWorld` 为什么不能直接复用编译器已有的 `World`，而要在它之上再扩展一层；
2. 读懂 `IdeWorld` 三个方法 `upcast` / `packages` / `files` 各自的语义、是必填还是可选、默认实现是什么；
3. 理解 `upcast` 背后的 Rust「trait 向上转型（trait upcasting）」问题；
4. 能判断「不实现 `packages()` 和 `files()` 时，哪些 IDE 功能会降级、哪些完全不受影响」。

## 2. 前置知识

在进入源码前，先用通俗语言把几个 Rust 与 Typst 的基础概念说清楚。

- **trait 与 supertrait（父 trait）**：写 `pub trait B: A` 表示「要实现 B，必须先实现 A」。这样 B 的对象不仅能调 B 自己的方法，也能调 A 的方法。
- **`dyn Trait`（trait 对象）**：编译期擦除具体类型、运行期通过虚表分派方法。typst-ide 的公共 API 统一收 `&dyn IdeWorld`，这样不同实现（tinymist、typst-lsp、测试用的 `TestWorld`）都能传进来，彼此解耦。
- **方法分派 vs. trait 对象转换**：在 `&dyn IdeWorld` 上调用其父 trait `World` 的方法（如 `world.library()`）是**允许**的；但要把 `&dyn IdeWorld` 这个 trait 对象**当作** `&dyn World` 使用，却需要「trait 向上转型」。这正是本讲要解释的关键点。
- **comemo 的 `track`**：Typst 的增量缓存框架。被 `#[comemo::track]` 的 trait（如 `World`）需要用 `.track()` 得到一个可被记忆化的引用。本讲只需知道 `upcast().track()` 这种写法存在即可。
- **上一讲术语**：`IdeWorld`（所有公共函数首参，数据来源）、`output: Option<impl AsOutput>`（上次编译产物，缺失则功能降级）。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `src/lib.rs` | `IdeWorld` trait 的定义本体，三个方法都在这里（L24–L51）。 |
| `crates/typst-library/src/lib.rs` | `World` trait 的定义（L60–L98），是 `IdeWorld` 的父 trait。 |
| `src/utils.rs` | `with_engine` 用 `upcast()` 拿到 `&dyn World` 去构造引擎（L30）。 |
| `src/analyze.rs` | `analyze_expr` 的回退分支也用 `upcast()` 去 trace（L45）。 |
| `src/complete.rs` | `package_completions`（L1139）消费 `packages()`；`file_completions`（L1162）消费 `files()`。 |
| `src/tests.rs` | `TestWorld` 实现 `IdeWorld` 的参考范例（L115–L125）。 |

## 4. 核心概念与源码讲解

### 4.1 World trait —— 编译运行的「世界」基类

#### 4.1.1 概念说明

`IdeWorld` 是 `World` 的子 trait。要看懂 `IdeWorld`，先得知道 `World` 给了什么。

`World` 描述「一次排版发生时的全部环境」，是 Typst 编译器运行的最小契约。任何能被编译的 Typst 文档，背后都有一个 `World` 在回答这些问题：标准库是什么、有哪些字体、主文件是谁、某个文件/源码/字体怎么拿、今天是几号。

关键点在于：`World` 只负责**按需解析「这一个」具体资源**——编译器遇到 `#import "a.typ"` 时，会调 `world.source(id)` 去拿那一个文件；它**不负责列出所有候选**。

#### 4.1.2 核心流程

`World` 一共七个方法，全部由 `#[comemo::track]` 包裹（即调用结果会被 comemo 自动缓存）：

```text
World
 ├── library()   -> 标准库（Library::build() 产物）
 ├── book()      -> 字体元信息（FontBook）
 ├── main()      -> 主文件 id
 ├── source(id)  -> 按 id 取某一个源码文件
 ├── file(id)    -> 按 id 取某一个二进制文件（图片等）
 ├── font(idx)   -> 按索引取某一个字体
 └── today(off)  -> 当前日期
```

实现者需要自行做内部缓存：文档注释里明确说，源码文件每次编译后应清空、字体可长期缓存（如 `typst watch`）。语言服务器这类长期运行的客户端，还可以保留源码并就地 `Source::edit` 以获得更好的增量性能。

#### 4.1.3 源码精读

`World` 的定义（`#[comemo::track]` 是 comemo 增量缓存的关键）：

[crates/typst-library/src/lib.rs:59-98](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-library/src/lib.rs#L59-L98) —— `pub trait World: Send + Sync`，带 `#[comemo::track]`，七个方法构成编译器运行所需的最小环境。

文档注释（L44–L58）值得细读：它解释了「为什么缓存由 World 负责、而非编译器负责」——因为 World 最清楚某个资源何时会变。

#### 4.1.4 代码实践

**实践目标**：建立「World = 按需解析单个资源」的直觉，为后面理解 IdeWorld 的「列出全部候选」做铺垫。

**操作步骤**：

1. 打开上面那段永久链接，对照七个方法，在笔记里写出「编译器遇到 `#image("cat.png")` 时会调 `World` 的哪个方法」。
2. 再回答：编译器在处理整份文档的过程中，有没有任何一个时机需要 World「把项目里所有 .png 文件都列出来」？

**预期结果**：

1. 会调 `world.file(id)`（`id` 由 `"cat.png"` 解析得到）。
2. 不会。`World` 从不主动枚举候选——这正是 `IdeWorld` 要补上的能力（见 4.4）。

#### 4.1.5 小练习与答案

- **问**：`World` 的加载方法为什么要求实现者自己做缓存？
  **答**：因为 World 比编译器更清楚资源何时变化——字体几乎不变可长期缓存，源码随时变应在每次编译后清空；把缓存职责交给 World 能让 `typst watch` 这类场景获得最优增量性能。

- **问**：`book()` 和 `font(idx)` 各自返回什么？
  **答**：`book()` 返回所有字体的**元信息**清单（`FontBook`，轻量）；`font(idx)` 才按索引返回真正的**字体数据**（`Font`，较重）。

---

### 4.2 IdeWorld trait —— 在 World 之上为 IDE 扩展的数据契约

#### 4.2.1 概念说明

回到核心问题：**为什么不直接用 `World`，要再造一个 `IdeWorld`？**

答案是「解析 vs. 枚举」的差别：

- 编译器要的是**解析**——「给我 `'a.typ'` 这个文件」。
- IDE 要的是**枚举**——「把项目里所有 `.typ` 文件、所有可用的 `@preview` 包都列出来，好让我在补全菜单里展示」。

枚举候选这件事，编译器根本不需要，所以不该塞进 `World`；但它对补全又必不可少。于是 typst-ide 单独定义 `IdeWorld: World`，把这部分「IDE 专属数据」挂在子 trait 上。

这样做有两个好处：

1. **核心 `World` 保持精简**，已有实现者（命令行、CI 渲染）无需关心 IDE 数据；
2. **想接 IDE 的实现者按需提供**：实现了 `packages()` / `files()` 就有对应补全，不实现也能优雅降级。

#### 4.2.2 核心流程

`IdeWorld` 一共三个方法，其中一个是必填、两个是可选：

```text
IdeWorld: World
 ├── upcast()                 -> &dyn World     【必填】手动向上转型
 ├── packages()               -> &[(PackageSpec, Option<描述>)]   【可选，默认空】
 └── files()                  -> Vec<FileId>    【可选，默认空】
```

调用方约定：typst-ide 的所有公共函数都收 `&dyn IdeWorld`。由于 `IdeWorld: World`，函数体里既可以直接调 `World` 的方法（方法分派找得到），也可以通过 `upcast()` 拿到 `&dyn World` 再交给需要该类型的子模块（如 comemo 的 `track`、`typst::trace`）。

#### 4.2.3 源码精读

`IdeWorld` 的完整定义与导入：

[src/lib.rs:19-22](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/lib.rs#L19-L22) —— 这四行 `use` 正好是 trait 签名里用到的类型：`EcoString`、`World`、`FileId`、`PackageSpec`。

[src/lib.rs:24-51](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/lib.rs#L24-L51) —— `IdeWorld` trait 主体。注意 `pub trait IdeWorld: World`（L25），三个方法中 `upcast` 无默认实现（必填），`packages`（L40–L42）与 `files`（L48–L50）各有空默认实现（可选）。文档注释里对每个方法都写明了是否「optional to implement」以及它增强的是哪项体验。

#### 4.2.4 代码实践

**实践目标**：把 `IdeWorld` 想象成一个「接口问卷」，判断每个方法对调用方的必要性。

**操作步骤**：

1. 阅读 [src/lib.rs:24-51](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/lib.rs#L24-L51) 的文档注释。
2. 在笔记里填一张表：三列分别是「方法名 / 必填还是可选 / 注释里说它增强哪项体验」。

**预期结果**：

| 方法 | 必填/可选 | 增强的体验 |
|---|---|---|
| `upcast` | 必填 | （非体验增强，而是基础设施：让函数体能拿到 `&dyn World`） |
| `packages` | 可选 | 包名补全 |
| `files` | 可选 | 文件路径补全 |

#### 4.2.5 小练习与答案

- **问**：为什么把 `packages` / `files` 放进 `IdeWorld` 而不是 `World`？
  **答**：因为它们服务的是「枚举候选」这一 IDE 专属需求，编译器从不需要。放进 `World` 会迫使所有 `World` 实现者（命令行渲染、CI 等）都提供无意义的数据；放进 `IdeWorld` 则让 IDE 能力按需开启、其余场景不受影响。

- **问**：一个类型实现了 `IdeWorld`，但没实现 `World`，能编译通过吗？
  **答**：不能。`IdeWorld: World` 是 supertrait 约束，实现 `IdeWorld` 必须先实现 `World` 的全部七个方法。

---

### 4.3 upcast —— 必填的「向上转型」方法

#### 4.3.1 概念说明

`upcast` 是三个方法里唯一**必填**的，也是最让人困惑的一个：既然 `IdeWorld: World`，为什么还要手写一个 `upcast()` 把自己变回 `&dyn World`？

这源于 Rust 的一个历史限制。在 Rust 里：

- 在 `&dyn IdeWorld` 上**调用** `World` 的方法是允许的（方法分派会通过 supertrait 找到）。
- 但把 `&dyn IdeWorld` 这个 trait 对象**当作** `&dyn World` 来用（即「trait 向上转型」），在 typst 设计这套接口时所支持的 Rust 版本里还是**实验性、不稳定**的。源码注释直接指出了这一点，并附上跟踪 issue `rust-lang/rust#65991`。

而 typst-ide 内部有些地方**必须**拿到确切的 `&dyn World`：比如 comemo 的 `Track`、以及 `typst::trace::<PagedDocument>(&dyn World, ..)`。于是 `IdeWorld` 要求实现者手写 `upcast`，把这个转换显式化。

> 补充事实：trait 向上转型最终在 Rust 1.86.0（2024-10）稳定。typst 仍保留 `upcast()`，是一种兼容较低工具链版本、且让转换意图更显式的稳妥做法。本讲以源码注释为准。

#### 4.3.2 核心流程

```text
   调用方传入 w: &dyn IdeWorld
            │
            ├── w.library()            ✅ 直接调 World 方法（方法分派可达）
            ├── w.source(id)           ✅ 同上
            │
            └── 需要 &dyn World 时：
                  w.upcast()  ──▶  &dyn World   ✅ 显式转型
                        │
                        ├── .track()     交给 comemo 构造引擎
                        └── typst::trace  交给 trace 子模块
```

实现侧极简：因为 `Self: World`，`&self` 可以协变成 `&dyn World`，所以实现体一行 `self` 即可。

#### 4.3.3 源码精读

trait 中的声明与注释：

[src/lib.rs:26-32](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/lib.rs#L26-L32) —— `upcast` 的文档注释解释了「为什么需要它」，并提示实现者直接返回 `self`。

两处消费点：

[src/utils.rs:21-38](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/utils.rs#L21-L38) —— `with_engine` 里，`world.library()` 可直接调（L29），但 `world: world.upcast().track()`（L30）必须先 `upcast` 成 `&dyn World` 再交给 comemo 的 `track`。

[src/analyze.rs:45](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/analyze.rs#L45) —— `analyze_expr` 回退到 trace 时，`typst::trace::<PagedDocument>(world.upcast(), node.span())` 同样需要 `&dyn World`。

参考范例（实现侧）：

[src/tests.rs:115-118](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tests.rs#L115-L118) —— `TestWorld` 实现 `upcast` 就是返回 `self`，印证了「实现者直接 `return self`」。

下面是说明上述「能调方法、却不能直接当对象用」的示意（**示例代码**，非项目源码）：

```rust
// 假设 w: &dyn IdeWorld
w.library();                   // ✅ 方法分派：通过 supertrait IdeWorld: World 找得到
let _: &dyn World = w;          // ❌ 旧版 Rust：需要 trait upcasting，不稳
let _: &dyn World = w.upcast(); // ✅ 借助手写方法显式拿到 &dyn World
```

#### 4.3.4 代码实践

**实践目标**：亲手验证「方法可调、对象不可直接转」这一不对称，并理解为什么 `with_engine` 非要 `upcast`。

**操作步骤**：

1. 阅读 [src/utils.rs:21-38](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/utils.rs#L21-L38)。
2. 在笔记里回答：如果删掉 `upcast()` 调用、直接写 `world.track()`，会出什么问题？（提示：`world` 是 `&dyn IdeWorld`，而 `Engine.world` 字段需要的是 `&dyn World` 的 tracked 引用。）
3. 再回答：为什么同一处 `world.library()`（L29）却不需要 `upcast`？

**需要观察的现象 / 预期结果**：

2. `world.track()` 中 `world: &dyn IdeWorld`，无法直接满足 `&dyn World` 的类型要求（trait upcasting 受限），所以必须 `upcast()` 先转型；这就是该方法是**必填**的根本原因。
3. 因为 `library()` 只是「在对象上调一个方法」，方法分派可通过 supertrait 找到，不涉及 trait 对象本身的类型转换，所以不需要 `upcast`。

> 完整把 `with_engine` 改成不调用 `upcast` 去复现编译错误，属于**待本地验证**的扩展练习（需要本地 cargo 环境）。

#### 4.3.5 小练习与答案

- **问**：`upcast` 是必填还是可选？为什么？
  **答**：必填。因为内部多处（`with_engine` 的 `track`、`analyze_expr` 的 `trace`）需要确切的 `&dyn World`，而 trait 向上转型在 typst 接口设计时还不稳定，必须由实现者显式提供。

- **问**：`upcast` 的实现体通常长什么样？
  **答**：就一行 `self`。因为实现 `IdeWorld` 的类型必定 `: World`，`&self` 可协变为 `&dyn World`，如 `TestWorld` 的实现所示。

---

### 4.4 packages 与 files —— 两个可选的增强数据源（及降级行为）

#### 4.4.1 概念说明

`packages()` 和 `files()` 是对称的两个**可选**方法，都带空默认实现，服务于「枚举候选」这一 IDE 专属需求：

- `packages()` 返回「所有可用包及其可选描述」，用于在 `#import "@preview/..."` 时补全包名。`@preview` 命名空间的包描述可从 `https://packages.typst.org/preview/index.json` 获取（注释里有说明）。
- `files()` 返回「所有已知文件的 id」，用于在 `#import` / `#include` / `#image(...)` 等处补全文件路径。

它们的「可选性」直接对应一种优雅降级：不实现，对应的补全就为空，但**其它所有 IDE 功能照常工作**。

> 注意区分：字体补全用的是 `World::book()`（必填，字体元数据），**不依赖** `files()`；所以即使不实现 `files()`，字体补全依然可用。

#### 4.4.2 核心流程

```text
complete.rs 补全管线
   ├── package_completions:  self.world.packages()  ──▶  排序、(namespace,name) 去重版本 ──▶ 包名补全
   └── file_completions:     self.world.files()      ──▶  过滤当前目录、按扩展名筛选 ──▶ 路径补全
```

两个方法若返回空（默认实现），对应补全列表就是空——不会报错，只是少了候选。

#### 4.4.3 源码精读

trait 中的可选方法与默认实现：

[src/lib.rs:34-50](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/lib.rs#L34-L50) —— `packages` 默认 `&[]`、`files` 默认 `vec![]`，注释都写明「optional to implement」及其增强的体验。

两处消费点：

[src/complete.rs:1137-1153](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1137-L1153) —— `package_completions` 调 `self.world.packages()`（L1139），按 `(namespace, name, version)` 排序，非 `all_versions` 时按 `(namespace, name)` 去重只保留最新版本。

[src/complete.rs:1155-1173](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/complete.rs#L1155-L1173) —— `file_completions` 调 `self.world.files()`（L1162），过滤掉当前文件、按扩展名筛选后，转成相对当前目录的路径作为补全项。

参考范例（实现侧）：

[src/tests.rs:120-125](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tests.rs#L120-L125) —— `TestWorld` 的 `files()` 把主文件、额外源码、额外资源三类 id 拼成一个 `Vec<FileId>`；这正是「枚举已知文件」的真实写法。

#### 4.4.4 代码实践

**实践目标**：建立「方法缺省 → 对应补全降级、其余功能不受影响」的判断力。

**操作步骤**：

1. 阅读 `package_completions` 与 `file_completions` 两处源码，确认它们各自**只**消费 `packages()` / `files()` 之一。
2. 在全 crate 内用搜索确认：除了这两处，`packages()` / `files()` 还有别的消费点吗？（预期：没有，分别只此一处。）

**预期结果（降级分析表）**：

| 场景 | 未实现 `packages()` | 未实现 `files()` | 两者都不实现 |
|---|---|---|---|
| 包名补全（`@preview/...`） | ❌ 无候选 | ✅ 不受影响 | ❌ 无候选 |
| 文件路径补全（`import`/`image`） | ✅ 不受影响 | ❌ 无候选 | ❌ 无候选 |
| 悬停 / 跳转定义 / 双向跳转 | ✅ 不受影响 | ✅ 不受影响 | ✅ 不受影响 |
| 字体补全、参数补全、字段补全、markup/math 补全 | ✅ 不受影响 | ✅ 不受影响 | ✅ 不受影响 |

结论：`packages()` / `files()` 的缺失**只**影响两类补全，其余 IDE 能力完全不受影响——这就是「可选增强 + 优雅降级」的设计意图。

> 在本地把 `TestWorld::packages` / `files` 改成返回空、再跑相关补全测试观察差异，属于**待本地验证**的扩展练习。

#### 4.4.5 小练习与答案

- **问**：不实现 `files()`，字体补全还能用吗？
  **答**：能。字体补全的数据来源是 `World::book()`（必填），与 `files()` 无关；`files()` 只服务文件路径补全。

- **问**：`package_completions` 在 `all_versions=false` 时如何去重？
  **答**：先按 `(namespace, name, Reverse(version))` 排序，再用 `dedup_by_key` 按 `(namespace, name)` 去重——由于已按版本降序排好，去重后保留下来的就是每个包的最新版本。

---

## 5. 综合实践

**任务**：为一个虚构的最小 IDE 后端，落实 `IdeWorld` 这个数据契约，并预测其能力边界。

**操作步骤**：

1. 设想一个 `struct MyIdeWorld { ... }`，它需要「能补全包名、能补全文件路径、能悬停、能跳转」。对照本讲，列出它**必须实现**的 `World` 方法与 `IdeWorld` 方法。
2. 写出 `IdeWorld` 三方法的实现草案：
   - `upcast(&self) -> &dyn World { self }`（必填）；
   - `packages(&self)` 返回你手头维护的包列表（可选，启用包补全）；
   - `files(&self)` 返回你已打开/已索引的文件 id 列表（可选，启用路径补全）。
   - 若你**暂时不想**支持包补全，就直接不覆盖 `packages()`（用默认空实现）。
3. 参考 [src/tests.rs:115-125](https://github.com/typst/typst/blob/146a58329a30f6cd38978c22c6bf0e430d8962a1/crates/typst-ide/src/tests.rs#L115-L125) 的 `TestWorld`，核对你的写法是否符合契约。
4. 回答：若你只实现了 `upcast`、不实现 `packages()` / `files()`，你的 IDE 后端能正常提供哪些功能？缺哪些？

**预期结果**：

- 必填：`World` 的七个方法 + `IdeWorld::upcast`。
- 只实现 `upcast` 时：悬停、跳转定义、双向跳转、字体/参数/字段/markup/math 补全全部可用；唯独「包名补全」「文件路径补全」两项为空。
- 要补齐这两项，分别需要额外维护一份「可用包列表」和「已知文件 id 列表」，并通过 `packages()` / `files()` 暴露出来。

> 完整编译运行需要先正确实现 `World` 的七个方法（涉及 `Library`、`FontBook`、文件读取等），这部分将在 u1-l3 用 `TestWorld` 给出可直接复用的范例，本讲聚焦契约本身，完整编译属**待本地验证**。

## 6. 本讲小结

- `IdeWorld: World` 是 typst-ide 所有公共函数的统一数据契约；`World` 负责「按需解析单个资源」，`IdeWorld` 额外提供「枚举全部候选」的 IDE 专属数据。
- 三个方法中 `upcast` 是**必填**，`packages` / `files` 是**可选**且带空默认实现。
- `upcast` 存在的根本原因：Rust 的 trait 向上转型在接口设计时尚不稳定，而内部多处（`with_engine` 的 `track`、`analyze_expr` 的 `trace`）需要确切的 `&dyn World`；实现体一行 `self` 即可。
- `packages()` 只被 `package_completions` 消费、`files()` 只被 `file_completions` 消费；二者缺失只影响对应的两类补全，其余 IDE 能力（悬停、定义、跳转、字体补全等）完全不受影响。
- 字体补全依赖必填的 `World::book()`，与 `files()` 无关。

## 7. 下一步学习建议

- 本讲只讲了「契约」，还没讲「如何造一个最小 World 并跑测试」。下一讲 **u1-l3 运行、构建与测试基础设施** 将用 `TestWorld` + 共享 `TestBase` 给出可直接复用的范例，建议紧接着读。
- 想提前了解 `upcast()` 拿到的 `&dyn World` 在引擎里如何被使用，可先扫一眼 `src/utils.rs` 的 `with_engine` 与 `globals`（u2-l5 会精读）。
- 想看「枚举候选」如何变成补全菜单项，可先浏览 `src/complete.rs` 的 `package_completions` / `file_completions`，u5 单元会系统讲解补全引擎。
