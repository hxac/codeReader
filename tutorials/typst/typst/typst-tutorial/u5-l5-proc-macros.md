# 宏的魔法：typst-macros 与声明式库开发

## 1. 本讲目标

Typst 标准库里有 600 多个内置函数、100 多个元素，但它们在 Rust 侧的声明大多只有寥寥数行。这背后的功臣是 `typst-macros` 这个过程宏 crate：你在 [u3-l5](u3-l5-func-and-args.md) 已经学过 `Func`、`Args` 与 cast 体系的**运行时**机制，本讲把镜头拉到**编译期**，看这些运行时结构是如何被自动生成出来的。

学完本讲，你应该能够：

1. 说出 `typst-macros` 导出的全部宏（`func`、`ty`、`elem`、`scope`、`cast!`、`derive(Cast)`、`time`）各自的职责与输入输出。
2. 手工推导一个 `#[func]` 函数（如 `upper`）的完整展开产物：清洗后的原函数、影子类型、`NativeFuncData` 常量与参数解析闭包。
3. 解释 `#[elem]` 如何从结构体声明生成字段存储、`Construct`/`Set` 实现与能力 vtable。
4. 用 `cargo expand`（或运行时反射测试）亲眼验证宏展开结果。
5. 讲清楚「一份声明、三处消费」的同步机制：同一份 Rust 声明同时驱动运行时注册、IDE 元数据与官方文档站。

## 2. 前置知识

**过程宏（procedural macro）**是 Rust 中「接收 token 流、返回 token 流」的编译期代码生成器。它分三种，本讲全都用到：

- **属性宏**：`#[func]`、`#[elem]`、`#[ty]`、`#[scope]`、`#[time]`——贴在条目上，改写这个条目；
- **函数式宏**：`cast! { .. }`——像函数调用一样接收 token 块；
- **派生宏**：`#[derive(Cast)]`——为枚举自动实现 trait。

写过程宏的标配三件套是 `syn`（把 token 解析成语法树）、`quote`（把语法树拼回 token）和 `proc-macro2`/`heck`（基础设施与命名风格转换）。`typst-macros` 的 `Cargo.toml` 里依赖恰好只有这四个，且 `[lib] proc-macro = true`——这是一个纯粹的编译期 crate，不含任何运行时代码。

一个关键的语言知识点（宏文档里也专门强调了）：**Rust 中函数和类型住在两个独立的命名空间**。所以可以同时存在 `fn upper()` 和 `enum upper {}`，互不冲突——宏正是利用这一点，为每个函数生成一个同名的「影子类型」，作为携带元数据的类型级句柄。

还需要回忆 [u3-l5](u3-l5-func-and-args.md) 的两个结论：`Args` 是消费式协议（`expect`/`named`/`eat`/`find`/`all` 各取所需、`finish` 兜底报错）；cast 体系由 `Reflect`（描述类型）、`IntoValue`（Rust→Typst）、`FromValue`（Typst→Rust）三个 trait 构成。本讲你会看到消费这些协议的样板代码正是宏生成的。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `crates/typst-macros/src/lib.rs` | 过程宏 crate 入口，声明全部 7 个宏及其完整使用文档 |
| `crates/typst-macros/src/func.rs` | `#[func]` 的解析与代码生成（本讲主角） |
| `crates/typst-macros/src/elem.rs` | `#[elem]` 的解析与代码生成 |
| `crates/typst-macros/src/cast.rs` | `cast!` 函数式宏与 `derive(Cast)` 的实现 |
| `crates/typst-macros/src/scope.rs` | `#[scope]`：把 impl 块变成 `NativeScope` 实现 |
| `crates/typst-macros/src/time.rs` | `#[time]`：向函数体注入计时作用域 |
| `crates/typst-macros/src/util.rs` | 共享工具：doc 注释抽取、命名推导、`Since` 解析 |
| `crates/typst-library/src/foundations/func.rs` | 消费端：`NativeFunc`、`NativeFuncData`、`NativeParamInfo` |
| `crates/typst-library/src/foundations/cast.rs` | 消费端：`Reflect`/`IntoValue`/`FromValue`/`IntoResult` trait 契约 |
| `crates/typst-library/src/text/case.rs` | 样本：`upper`/`lower` 函数与 `cast!`、`derive(Cast)` 实战 |
| `crates/typst-library/src/model/heading.rs` | 样本：`HeadingElem` 的真实声明 |
| `docs/src/reflect.rs` | 文档站反射：消费 `NativeFuncData` 生成文档页面 |

## 4. 核心概念与源码讲解

### 4.1 过程宏清单：七个宏与一条重导出链

#### 4.1.1 概念说明

`typst-macros` 是一个独立的 proc-macro crate，它把「定义一个 Typst 可见实体」这件事**声明式化**：你只写一个普通 Rust 函数/结构体/枚举，加上属性宏和 doc 注释，宏负责生成注册、参数解析、元数据等全部样板。

整条流水线上每个宏对应一种「Typst 世界里的东西」：

| 宏 | 种类 | 贴在哪 | 生成的核心产物 |
| --- | --- | --- | --- |
| `#[func]` | 属性宏 | `fn` | 影子类型 + `NativeFunc` 实现（含 `NativeFuncData`） |
| `#[ty]` | 属性宏 | 类型 | `NativeType` 实现（类型名、作用域） |
| `#[elem]` | 属性宏 | 结构体 | `NativeElement` 实现 + 字段体系 + `Construct`/`Set` |
| `#[scope]` | 属性宏 | `impl` 块 | `NativeScope` 实现（方法/常量/子类型的注册表） |
| `cast!` | 函数式宏 | 类型声明处 | `Reflect` + `IntoValue` + `FromValue` 三件套 |
| `#[derive(Cast)]` | 派生宏 | 枚举 | 委托给 `cast!` 的字符串枚举转换 |
| `#[time]` | 属性宏 | `fn` | 函数体首行插入 `TimingScope`（性能计时） |

一个容易被忽略的架构细节：这些宏**不直接被库代码引用**，而是先由 `typst-library` 的「门户模块」重导出，库内部统一从 `crate::foundations` 导入：

- `func` 宏从 [crates/typst-library/src/foundations/func.rs:2](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/foundations/func.rs#L2) 重导出；
- `cast!` 与 `Cast` 派生从 [crates/typst-library/src/foundations/cast.rs:3](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/foundations/cast.rs#L3) 重导出；
- `elem` 宏从 [crates/typst-library/src/foundations/content/mod.rs:12](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/foundations/content/mod.rs#L12) 重导出；
- `scope` 与 `ty` 从 [crates/typst-library/src/foundations/mod.rs:71](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/foundations/mod.rs#L71) 重导出。

这样 `case.rs` 里只需一行 `use crate::foundations::{Cast, Content, Str, cast, func};` 就集齐了全部宏（见 [crates/typst-library/src/text/case.rs:1](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/text/case.rs#L1)），使用者的导入面保持最小。

#### 4.1.2 核心流程

proc-macro crate 的生命周期非常简单：

```text
cargo 编译 typst-library
  → 遇到 #[func] 等属性
  → 先编译 typst-macros（proc-macro = true 的动态库）
  → 对每个被标注条目，调用对应宏函数：(属性参数 token 流, 条目 token 流) → 新 token 流
  → 用返回的 token 流替换原条目，继续编译
```

每个属性宏入口的模式完全一致（以 `func` 为例）：

1. `syn::parse_macro_input!` 把条目解析成 `syn::ItemFn`；
2. 调用同名子模块的实现函数（`func::func`）；
3. 出错时 `err.to_compile_error()` 把编译错误注入 token 流，成功则返回生成代码。

#### 4.1.3 源码精读

`lib.rs` 顶部声明了全部子模块，注意 `#[macro_use] mod util;` 让工具函数（如 `bail!`）在子模块中可用：[crates/typst-macros/src/lib.rs:5-12](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-macros/src/lib.rs#L5-L12)。

七个宏的入口分别是：

- `#[func]`：[crates/typst-macros/src/lib.rs:103-109](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-macros/src/lib.rs#L103-L109)——解析 `ItemFn` 后交给 `func::func`。它上方的文档注释（[L17-102](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-macros/src/lib.rs#L17-L102)）是全宏体系最详尽的使用手册，列出了 `scope`/`contextual`/`name`/`title`/`keywords`/`constructor` 六个属性和 `#[named]`/`#[default]`/`#[variadic]`/`#[external]` 四个参数属性，以及 `engine`/`context`/`args`/`span` 四个「运行时特供参数」。
- `#[ty]`：[crates/typst-macros/src/lib.rs:136-142](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-macros/src/lib.rs#L136-L142)，接收任意 `syn::Item`（结构体或枚举）。
- `#[elem]`：[crates/typst-macros/src/lib.rs:212-218](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-macros/src/lib.rs#L212-L218)，只接受 `syn::ItemStruct`。
- `#[scope]`：[crates/typst-macros/src/lib.rs:261-267](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-macros/src/lib.rs#L261-L267)。
- `cast!`：[crates/typst-macros/src/lib.rs:297-302](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-macros/src/lib.rs#L297-L302)，注意它是 `#[proc_macro]`（函数式），只接收一段 token 流。
- `#[derive(Cast)]`：[crates/typst-macros/src/lib.rs:323-329](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-macros/src/lib.rs#L323-L329)。
- `#[time]`：[crates/typst-macros/src/lib.rs:363-369](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-macros/src/lib.rs#L363-L369)。

`#[time]` 的实现最能体现「属性宏=函数体改写」的本质：它只是在函数体最前面插一句 `let __scope = TimingScope::new(name);`，靠 RAII 在作用域结束时记录耗时——[crates/typst-macros/src/time.rs:29-44](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-macros/src/time.rs#L29-L44)。这正是 [u4-l1](u4-l1-compile-pipeline-overview.md) 里 `--timings` 计时功能的代码注入点。

依赖清单印证了「纯编译期 crate」的判断——只有 `heck`、`proc-macro2`、`quote`、`syn` 四个依赖：[crates/typst-macros/Cargo.toml:15-22](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-macros/Cargo.toml#L15-L22)。

#### 4.1.4 代码实践

**实践目标**：不运行任何东西，先建立「宏使用密度」的定量直觉。

**操作步骤**：

1. 在仓库根目录执行（只读命令）：
   ```bash
   rg -c '#\[func' crates/typst-library/src | awk -F: '{s+=$2} END {print "func:", s}'
   rg -c '#\[elem' crates/typst-library/src | awk -F: '{s+=$2} END {print "elem:", s}'
   rg -c 'cast! \{' crates/typst-library/src | awk -F: '{s+=$2} END {print "cast!:", s}'
   rg -c 'derive\(.*Cast' crates/typst-library/src | awk -F: '{s+=$2} END {print "derive(Cast):", s}'
   ```
2. 再用 `rg '#\[func\(' crates/typst-library/src -l | head` 看看哪些模块最依赖宏。

**需要观察的现象**：`#[func]` 的计数是数百量级，`cast!`/`derive(Cast)` 也有上百处——手工维护这些样板是不可想象的。

**预期结果**：数字佐证「声明式宏体系是标准库的开发方式，而非点缀」。具体数值待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `typst-macros` 能生成一个和函数同名的 `enum` 而不报「重复定义」？

**答案**：Rust 有三个命名空间（类型、值、宏），函数 `fn upper` 位于值命名空间，`enum upper` 位于类型命名空间，二者不冲突。宏文档在 [crates/typst-macros/src/lib.rs:17-27](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-macros/src/lib.rs#L17-L27) 明确说明了这一设计。

**练习 2**：`#[time]` 宏为什么能无侵入地计时？

**答案**：它在函数体首行插入 `TimingScope` 绑定，函数返回时该值析构，析构函数记录本次调用的结束时间与耗时（RAII 模式），见 [crates/typst-macros/src/time.rs:36-41](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-macros/src/time.rs#L36-L41)。

### 4.2 `#[func]` 展开产物：影子类型、`NativeFuncData` 与参数解析闭包

#### 4.2.1 概念说明

`#[func]` 解决的问题是：把一个「普通 Rust 函数」变成「Typst 世界的一等函数」。回顾 [u3-l5](u3-l5-func-and-args.md)：运行时调用一个原生函数时，实际执行的是签名 `dyn Fn(&mut Engine, Tracked<Context>, &mut Args) -> SourceResult<Value>` 的函数指针，并且需要一份描述参数表的 `NativeFuncData` 元数据。手写这两样东西（从 `Args` 里逐个取参数、转换类型、报告错误、登记参数文档）极其繁琐且容易漏——`#[func]` 把这一切从函数签名和 doc 注释**自动推导**出来。

对每个被标注的自由函数，宏生成**三件套**：

1. **清洗后的原函数**（剥掉自定义属性、加上单行 doc）；
2. **影子类型**：一个零变体 `enum`，名字与函数相同，纯类型级句柄；
3. **`NativeFunc` 实现**：`data()` 返回一个静态初始化的 `NativeFuncData`，内含函数指针、名称、文档、参数表。

而当函数位于 `#[scope]` 标注的 impl 块内时，无法在 impl 块里再定义类型，宏改为一对一生成 `{name}_data()` 自由函数（由 `#[scope]` 负责接线），这在 [crates/typst-macros/src/lib.rs:23-27](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-macros/src/lib.rs#L23-L27) 有说明。

#### 4.2.2 核心流程

`#[func]` 的展开分两个阶段，伪代码如下：

```text
阶段一：解析（func::parse）
  解析 #[func(..)] 元属性 → Meta { scope, contextual, name, title, since, keywords, constructor, parent }
  遍历函数签名参数：
    名为 engine/context/args/span 的参数 → 记入 SpecialParams（不进参数表）
    &self/&mut self        → SpecialParams.self_（需显式 parent 类型）
    其余参数 → Param {
      named ← #[named]       （命名可选参数）
      variadic ← #[variadic] （变长参数，类型须为 Vec<_>）
      external ← #[external] （仅进文档，不生成解析代码）
      default ← #[default] 或 #[default(expr)]
    }
  从 doc 注释抽取文档串

阶段二：生成（func::create）
  1) rewrite_fn_item：剥掉参数上的全部属性、剥掉函数属性 → 干净的 fn
  2) create_func_ty：生成零变体影子 enum（仅自由函数）
  3) create_func_data：拼出 NativeFuncData 字面量
     ├─ create_wrapper_closure：参数解析闭包（真正的调用入口）
     ├─ create_param_info × N：每个参数一条 NativeParamInfo
     └─ returns: <返回类型 as Reflect>::output()
  4) 输出：清洗后的 fn + 影子 enum + impl NativeFunc
```

其中参数解析闭包是精华，它固定为四段结构（对应 `create_wrapper_closure`）：

```text
|engine, context, args| {
    let __typst_func = 原函数;            // ① 绑定
    ② 逐参数解析（create_param_parser）
    ③ args.take().finish()?;              // 兜底：报告多余参数（仅当没有手动 args 参数）
    ④ let output = __typst_func(..); IntoResult::into_result(output, args.span)
}
```

每个参数按属性选择不同的 `Args` 消费方法，这个映射表是宏的核心逻辑（[crates/typst-macros/src/func.rs:459-477](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-macros/src/func.rs#L459-L477)）：

| 参数声明 | 生成的解析代码 | Args 方法 |
| --- | --- | --- |
| `#[variadic] values: Vec<T>` | `let mut values: Vec<T> = args.all()?;` | `all`（吃光所有剩余位置参数） |
| `#[named] x: T` | `let mut x: T = args.named("x")?.unwrap_or_else(默认值);` | `named` |
| 位置参数带 `#[default]` | `let mut x: T = args.eat()?.unwrap_or_else(默认值);` | `eat`（取一个，没有则 None） |
| 普通位置参数 | `let mut x: T = args.expect("x")?;` | `expect`（必须有，否则报错） |

#### 4.2.3 源码精读

**宏侧**。解析入口在 [crates/typst-macros/src/func.rs:14-17](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-macros/src/func.rs#L14-L17)；`Func` 结构体（[L20-51](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-macros/src/func.rs#L20-L51)）汇集了名字、文档、特殊参数与参数表。

参数识别的关键代码：四个运行时特供参数按名字硬匹配，其余才进参数表——[crates/typst-macros/src/func.rs:222-246](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-macros/src/func.rs#L222-L246)。这段还做了一件事：参数的 Typst 名取 Rust 标识符的 kebab-case（`ident.to_string().to_kebab_case()`），所以 `lightness` 无需任何标注就叫 `lightness`。

总装函数 `create` 决定输出三件套的形态——自由函数走「影子 enum + `impl NativeFunc`」，作用域内函数走 `{name}_data()`：[crates/typst-macros/src/func.rs:252-290](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-macros/src/func.rs#L252-L290)。

`create_func_data` 拼出 `NativeFuncData` 字面量，注意三个 `LazyLock`：作用域、参数表、返回值描述都是**惰性求值**的静态数据（首次访问才构造，编译期零成本）：[crates/typst-macros/src/func.rs:339-353](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-macros/src/func.rs#L339-L353)。`def_site` 用 `file!()` 宏记录定义所在的源文件路径，为 IDE 跳转和文档「查看源码」提供锚点（[L346](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-macros/src/func.rs#L346)）。

四段式闭包的生成在 [crates/typst-macros/src/func.rs:371-419](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-macros/src/func.rs#L371-L419)：`handlers` 跳过 `#[external]` 参数（[L373-384](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-macros/src/func.rs#L373-L384)）；`finish` 只在没有手动 `args` 参数时插入（[L387](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-macros/src/func.rs#L387)）；调用点把特供参数按固定顺序 `self, engine, context, args, span` 排列转发（[L390-405](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-macros/src/func.rs#L390-L405)）；最后 `IntoResult::into_result` 统一把返回值（`T`/`StrResult<T>`/`SourceResult<T>`）包装成 `SourceResult<Value>` 并附加调用点 span（[L410-418](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-macros/src/func.rs#L410-L418)）。

`NativeParamInfo` 的生成在 [crates/typst-macros/src/func.rs:422-456](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-macros/src/func.rs#L422-L456)，其中 `positional`、`required` 等布尔位全部由参数属性推导，`input` 字段调用 `<T as Reflect>::input()` 获得该参数可接受类型的描述——**宏只组装结构，类型信息来自 cast 体系**，这就是 4.4 节同步机制的基石。

**库侧（消费端）**。`NativeFunc` trait 提供默认的 `func()` 并要求实现 `data()`：[crates/typst-library/src/foundations/func.rs:689-699](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/foundations/func.rs#L689-L699)。`NativeFuncData` 的字段与宏生成的字面量一一对应（[L701-726](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/foundations/func.rs#L701-L726)）；函数指针类型 `NativeFuncSignature` 正是闭包的签名（[L733-738](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/foundations/func.rs#L733-L738)）；`DynLazyLock` 是「以静态闭包初始化的 LazyLock」的类型别名，解释了宏里 `LazyLock::new(&|| ..)` 的写法（[L746-753](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/foundations/func.rs#L746-L753)）。

**样本：`upper`**。它的完整声明只有五行（[crates/typst-library/src/text/case.rs:28-34](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/text/case.rs#L28-L34)）：

```rust
#[func(title = "Uppercase", since = "forever")]
pub fn upper(
    /// The text to convert to uppercase.
    text: Caseable,
) -> Caseable {
    case(text, Case::Upper)
}
```

依据宏源码逐行推导（下节实践会验证），它展开为（**示例代码：手工推导的展开形态**）：

```rust
// ① 清洗后的原函数（doc 换成首行摘要）
#[doc = "Converts a string or content to uppercase."]
#[expect(dead_code)]
#[expect(rustdoc::broken_intra_doc_links)]
pub fn upper(text: Caseable) -> Caseable { case(text, Case::Upper) }

// ② 影子类型：零变体 enum，仅作类型级句柄
#[doc(hidden)]
#[expect(non_camel_case_types)]
pub enum upper {}

// ③ NativeFunc 实现：静态 NativeFuncData
impl NativeFunc for upper {
    fn data() -> &'static NativeFuncData {
        static DATA: NativeFuncData = NativeFuncData {
            function: NativeFuncPtr(&|engine, context, args| {
                let __typst_func = upper;
                let mut text: Caseable = args.expect("text")?;  // 参数解析
                args.take().finish()?;                          // 多余参数报错
                let output = __typst_func(text);
                IntoResult::into_result(output, args.span)      // 错误附上调用点
            }),
            name: "upper",
            title: "Uppercase",
            since: Some(Since::Forever),
            docs: "Converts a string or content to uppercase.\n...",
            def_site: Some(DefSite { path: file!(), key: "upper" }),
            keywords: &[],
            contextual: false,
            scope: LazyLock::new(&|| Scope::new()),
            params: LazyLock::new(&|| vec![NativeParamInfo {
                name: "text",
                docs: "The text to convert to uppercase.",
                def_site: Some(DefSite { path: file!(), key: "upper::text" }),
                input: <Caseable as Reflect>::input(),
                default: None,
                positional: true, named: false,
                variadic: false, required: true, settable: false,
            }]),
            returns: LazyLock::new(&|| <Caseable as Reflect>::output()),
        };
        &DATA
    }
}
```

五行声明换来近四十行展开体——这就是「声明式库开发」的含义：**样板即生成物**。

#### 4.2.4 代码实践

**实践目标**：不靠想象，用两种方式亲眼确认 `upper` 的展开产物。

**方式 A（推荐，零额外工具）：运行时反射**。`NativeFuncData` 是公开的静态数据，`upper` 的影子 enum 经 `pub use self::case::*`（[crates/typst-library/src/text/mod.rs:18](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/text/mod.rs#L18)）重导出为 `typst_library::text::upper`。新建临时文件 `crates/typst-library/tests/inspect-upper.rs`（练习后删除，不入库）：

```rust
// 示例代码：临时集成测试
use typst_library::foundations::NativeFunc;

#[test]
fn inspect_upper() {
    let data = typst_library::text::upper::data();
    println!("name:    {}", data.name);
    println!("title:   {}", data.title);
    println!("docs:    {}", data.docs);
    for p in data.params.iter() {
        println!(
            "param {name}: positional={p} named={n} variadic={v} required={r} settable={s}",
            name = p.name, p = p.positional, n = p.named,
            v = p.variadic, r = p.required, s = p.settable,
        );
        println!("  input: {:?}", p.input);
        println!("  docs:  {}", p.docs);
    }
    println!("returns: {:?}", *data.returns);
}
```

运行：`cargo test -p typst-library --test inspect-upper -- --nocapture`。

**方式 B：cargo expand**。需要先 `rustup toolchain install nightly` 与 `cargo install cargo-expand`，然后：

```bash
cargo +nightly expand -p typst-library text::case
```

模块过滤器 `text::case` 会把输出收窄到 case.rs 的展开。

**需要观察的现象**：方式 A 打印出 `name: upper`、`title: Uppercase`、单个参数 `text`（`positional=true, required=true`），且 `input` 是 `Caseable` 的 `Reflect::input()` 结果（str 或 content 二选一）；方式 B 里能看到与 4.2.3 推导一致的闭包与 `NativeFuncData` 字面量。

**预期结果**：打印内容与 4.2.3 的推导逐项吻合；`#[doc(hidden)]` 的影子 enum 确实存在且可寻径。具体输出格式待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：如果给 `upper` 加一个 `#[named] #[default(0)] repeat: i64` 参数，展开后的解析代码是什么？

**答案**：`let mut repeat: i64 = args.named("repeat")?.unwrap_or_else(|| 0);`——`#[named]` 走 `args.named`，`#[default]` 在 `None` 时兜底，对应 [crates/typst-macros/src/func.rs:462-474](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-macros/src/func.rs#L462-L474) 的两个分支组合。

**练习 2**：为什么闭包末尾要 `IntoResult::into_result(output, args.span)`，而不是直接 `Ok(output.into_value())`？

**答案**：原生函数的返回类型有三种：`T`、`StrResult<T>`（错误无 span）与 `SourceResult<T>`。`IntoResult` 为三者分别实现了统一的转换，`StrResult` 的错误在这一步被 `.at(span)` 附上函数调用点的 span，让用户看到准确的报错位置。trait 定义见 [crates/typst-library/src/foundations/cast.rs:210-241](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/foundations/cast.rs#L210-L241)。

**练习 3**：`luma` 的三个参数全部标了 `#[external]`，为什么？

**答案**：`luma` 的参数语义有互斥歧义（给颜色时不能再给亮度），无法用统一的机械解析表达，所以参数只进文档表（`create_param_info` 不过滤 external），实际解析由函数体内的 `args.find::<Color>()` / `args.expect("lightness component")` 手工完成（[crates/typst-library/src/visualize/color.rs:384-403](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/visualize/color.rs#L384-L403)），因此它的签名里有特供参数 `args: &mut Args` 且宏不注入 `finish` 兜底。

### 4.3 `#[elem]` 展开产物：字段存储、`Construct`/`Set` 与能力 vtable

#### 4.3.1 概念说明

`#[elem]` 面向的是 [u3-l4](u3-l4-content-and-elements.md) 讲过的元素系统：一个元素 = 一个结构体 + 一组可从 Typst 脚本设置的字段 + 一份类型元数据。宏从结构体声明生成**六类产物**：

1. **改写后的结构体**：字段按种类替换存储类型（见下表）；
2. **固有 impl**：`new()`（只收 required 字段）+ 每个可选字段的 builder 式 `with_x()` 方法 + 每个真实字段的 `Field<Self, i>` 常量（供样式链读写）；
3. **`NativeElement` 实现**：一个静态 vtable 常量 `ELEM`，携带名称、标题、文档、字段描述表和能力探针；
4. **逐字段 impl**：按字段种类实现 `RequiredField`/`SynthesizedField`/`SettableField`/`SettableProperty`/`ExternalField` 之一；
5. **`Construct` 实现**（除非手工提供）：从 `Args` 构造元素实例；
6. **`Set` 实现**（除非手工提供）：支持 `set` 规则。

字段种类决定存储类型，这是理解元素结构的钥匙（生成逻辑在 `create_field`）：

| 字段属性 | 结构体里的存储类型 | 语义 |
| --- | --- | --- |
| `#[required]` | 裸 `T` | 位置必选参数（如 heading 的 `body`） |
| `#[synthesized]` | `Option<T>` | 不能由用户设置，show 前由 `Synthesize` trait 填充 |
| 默认（settable） | `Settable<Self, i>` | 可选：实例上直设，或走样式链 |
| `#[ghost]` | 不进结构体 | 只存在于样式链（如 `TextElem` 的排版属性） |
| `#[external]` | 不进结构体 | 仅文档展示 |
| `#[internal]` | 照常进结构体 | 存在但对用户隐藏（不进 vtable 字段表） |

一个重要澄清：`#[elem(Show, Count, Locatable, ..)]` 列表里的大多数名字**不是**让宏实现这些 trait——`Show`、`Synthesize`、`Count` 等能力的真正实现是库作者手写的（如 [crates/typst-library/src/model/heading.rs:249-365](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/model/heading.rs#L249-L365) 中对 `Packed<HeadingElem>` 的六个手工 impl）。宏对这份列表做三件事：挑选特殊生成物（`Construct`/`Set`/`Debug` derive 等）、生成能力探针闭包（运行时按 `TypeId` 动态查能力）、提取内省能力位（`Locatable`/`Unqueriable`/`Tagged`）。

#### 4.3.2 核心流程

```text
#[elem] 展开流程
  parse：解析元属性（name/title/since/keywords + 能力列表）
         逐字段解析属性 → Field { positional, required, variadic,
                                   fold, internal, external, ghost,
                                   synthesized, parse, default }
         校验组合合法性（required 不能又 synthesized/default/fold…）
         internal 字段排序沉底，编号 i 作为类型级索引
  create：
    ├─ create_struct         结构体（字段类型替换）
    ├─ create_inherent_impl  new + with_x + Field 常量
    ├─ create_native_elem_impl  ELEM vtable 常量
    ├─ create_field_impl × N    逐字段 trait + 元数据
    ├─ create_construct_impl    args → Self{..}（可被手工 Construct 覆盖）
    ├─ create_set_impl          args → Styles（可被手工 Set 覆盖）
    └─ create_mathy_impl        数学模式标记（可选）
    全部包进 const _: () = { .. } 匿名作用域，避免泄漏局部定义
```

`Construct` 的字段解析器与 `#[func]` 的参数解析器几乎同构，映射表在 `create_field_parser`：variadic→`args.all`、required→`args.expect`、位置可选→`args.find`、命名→`args.named`（[crates/typst-macros/src/elem.rs:637-654](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-macros/src/elem.rs#L637-L654)）。`#[parse({ .. })]` 属性可整块替换默认解析逻辑。

#### 4.3.3 源码精读

**真实样本 `HeadingElem`** 的声明：能力列表与结构体开头在 [crates/typst-library/src/model/heading.rs:77-88](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/model/heading.rs#L77-L88)。它的字段恰好覆盖了主要种类：

- `level: Smart<NonZeroUsize>`（[L107](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/model/heading.rs#L107)）——无属性，settable，存储为 `Settable<Self, 0>`；
- `#[default(NonZeroUsize::ONE)] depth`（[L116-117](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/model/heading.rs#L116-L117)）——settable + 默认值；
- `#[internal] #[synthesized] numbers: EcoString`（[L155-157](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/model/heading.rs#L155-L157)）——内部 + 合成，存储为 `Option<EcoString>`，不进构造函数；
- `#[required] body: Content`（[L236-237](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/model/heading.rs#L236-L237)）——裸 `Content`，是 `new()` 的唯一参数。

**宏侧关键函数**。字段属性解析与合法性校验在 [crates/typst-macros/src/elem.rs:199-246](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-macros/src/elem.rs#L199-L246)，`variadic` 隐含 `required`、`required` 隐含 `positional` 的推导在这里。总装 `create` 列出六类产物并用 `const _: () = { .. }` 包裹：[crates/typst-macros/src/elem.rs:248-277](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-macros/src/elem.rs#L248-L277)。

三种存储类型的替换逻辑只有十行：[crates/typst-macros/src/elem.rs:298-307](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-macros/src/elem.rs#L298-L307)。`new()` 只把 required 字段提为参数：[L337-360](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-macros/src/elem.rs#L337-L360)——所以 Rust 侧构造一个 heading 是 `HeadingElem::new(body)`，与 Typst 侧 `heading(level: 2)[..]` 的「必选参数 + 可选命名」语义完全对齐。

vtable 常量的组装在 [crates/typst-macros/src/elem.rs:437-463](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-macros/src/elem.rs#L437-L463)：`ContentVtable::new::<#ident>(名称, 标题, since, 文档, DefSite, 字段表, 字段名→索引闭包, 能力探针, 内省能力, STORE)`，再按需 `.with_keywords()`、`.with_repr()` 等。字段描述表按种类选择五套 vtable 之一（[L393-410](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-macros/src/elem.rs#L393-L410)），逐字段 trait 实现在 [L467-570](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-macros/src/elem.rs#L467-L570)——注意 settable 字段的 `OnceLock` 槽位（[L520-525](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-macros/src/elem.rs#L520-L525)）：默认值全局只算一次。

能力探针闭包用悬垂指针 + `TypeId` 比较实现「动态查询元素是否实现某 trait」：[crates/typst-macros/src/elem.rs:657-700](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-macros/src/elem.rs#L657-L700)——这是 [u4-l2](u4-l2-realize-stage.md) 里 realize 阶段按能力（如 `Show`）派发规则的底层支撑。

`Construct` 与 `Set` 的生成在 [crates/typst-macros/src/elem.rs:573-634](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-macros/src/elem.rs#L573-L634)：`Set` 为每个可设置字段生成「解析到值就 `styles.set(Self::field, value)`」，这正是 `set heading(depth: 2)` 在数据层落地的入口（衔接 [u3-l7](u3-l7-styles-and-show-rules.md) 的 `Styles`）。

#### 4.3.4 代码实践

**实践目标**：把 `HeadingElem` 的 8 个公开字段逐一归类，画出「声明 → 生成物」对照表。

**操作步骤**：

1. 通读 [crates/typst-library/src/model/heading.rs:88-238](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/model/heading.rs#L88-L238) 的全部字段声明；
2. 对每个字段记录：属性组合、宏赋予的种类、存储类型、`new()` 参数与否、进不进 vtable 字段表；
3. 用 `rg 'pub fn new|fn with_' crates/typst-library/src/model/heading.rs` 观察哪些是手写方法、哪些符合宏生成模式（注意 heading.rs 里有手写的 `impl HeadingElem` 块如 [L240-245](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/model/heading.rs#L240-L245) 的 `resolve_level`，与宏生成的固有 impl 共存）。

**需要观察的现象**：`with_` 方法数量等于非 required、非 ghost 字段数；手写方法（如 `resolve_level`）与生成方法和平共处，因为宏产物在匿名 `const _` 作用域之外只有结构体本身。

**预期结果**：得到一张 8 行对照表，其中 `body` 是唯一 required 字段（`new(body)`），`numbers` 是唯一的 synthesized 字段，其余全部是 settable。具体字段集合以源码为准，待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `#[ghost]` 字段不能出现在结构体里却仍要写在声明中？

**答案**：ghost 字段（如 `TextElem` 的排版属性）只需要样式链存取通道，不需要实例存储。宏把它排除在 `struct_fields()` 之外（不进结构体、无 `with_` 方法），但保留 `Field<Self, i>` 常量与 `SettableProperty` impl 供 `styles.set/get` 使用。若元素有公开 ghost 字段，则禁止自动生成 `Construct`，校验见 [crates/typst-macros/src/elem.rs:176-183](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-macros/src/elem.rs#L176-L183)。

**练习 2**：`#[elem]` 元属性列表里的 `Construct` 和 `Set` 与其他能力名有什么不同？

**答案**：它们是「负向开关」——写在列表里表示「我要手写实现，宏别自动生成」。`create()` 中 `element.cannot("Construct").then(|| create_construct_impl(element))`：只有**未**列出时才生成，见 [crates/typst-macros/src/elem.rs:258-260](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-macros/src/elem.rs#L258-L260)。而 `Show`、`Count` 等列出了才具备，且实现由库作者手写。

**练习 3**：字段在结构体里的顺序为何会被宏重排？

**答案**：`parse` 阶段按 `internal` 排序，内部字段沉底，然后统一编号 `i`（[crates/typst-macros/src/elem.rs:170-174](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-macros/src/elem.rs#L170-L174)）。编号以类型参数形式进入 `Settable<Self, i>` 与 `Field<Self, i>`，是样式链中区分字段的类型级索引。

### 4.4 文档与注册同步：一份声明、三处消费

#### 4.4.1 概念说明

声明式宏体系最深的价值不是省代码，而是**同步**：函数名、参数表、默认值、文档、版本号、关键字、源码位置，全部萃取自同一份 Rust 声明，因此运行时行为、IDE 补全与官方文档站永远一致——不存在「文档忘了更新」的第三处真相。

三个消费端分别是：

1. **运行时注册**：`#[func]` 生成影子类型，`define_func::<upper>()` 这样的泛型调用把 `NativeFuncData` 装进全局作用域；
2. **IDE/反射**：`NativeParamInfo` 的每个布尔位与 `CastInfo` 直接服务补全与错误提示（[u3-l3](u3-l3-value-type-system.md) 提过 `Reflect` 对自动补全的重要性）；
3. **文档站**：`docs` crate 的 reflect 模块在运行时遍历标准库，把 `NativeFuncData` 翻译成文档页面数据。

#### 4.4.2 核心流程

doc 注释的旅程：

```text
/// Converts a string ...
#[func]
pub fn upper(/// The text ... text: Caseable) ..
        │
        ▼ documentation()：syn 属性里抽出 #[doc = "..."] 字符串
        ▼ oneliner()：取首段首行作为摘要
   NativeFuncData { docs, params: [NativeParamInfo { docs, .. }] }
        │
        ├──（注册）text/mod.rs: define_func::<upper>() → 全局作用域 → 编译器可用
        └──（文档）docs/src/reflect.rs: describe_func(func)
                     → dict! { name, title, docs, params: [...], returns, ... }
                     → docs/components/reflect.typ 渲染成页面
```

配套信息走同一条管道：`since` 版本号（解析为 `Since::Forever`/`Version`/`Unreleased` 三态，见 [crates/typst-macros/src/util.rs:349-374](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-macros/src/util.rs#L349-L374)）、`keywords` 搜索别名、`DefSite { path: file!(), key }` 源码定位。

命名也有自动同步规则：未指定 `name` 时取标识符的 kebab-case（`HeadingElem` 去掉 `Elem` 后缀变 `heading`），未指定 `title` 时取 title case；如果显式写出的名字和默认值相同，宏直接报错「name was specified unnecessarily」——逼你删掉冗余声明，见 [crates/typst-macros/src/util.rs:196-220](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-macros/src/util.rs#L196-L220)。

#### 4.4.3 源码精读

**萃取**。`documentation()` 遍历条目的 syn 属性，收集所有 `#[doc = "..."]` 并去掉每行 leading 空格：[crates/typst-macros/src/util.rs:41-56](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-macros/src/util.rs#L41-L56)——doc 注释在 syn 眼里就是普通属性，宏只是把编译器丢弃的信息捡了回来。

**注册**。`#[func]` 生成的影子类型让注册调用变得极其简洁——`upper` 既指函数又指类型，`define_func::<upper>()` 里的 `upper` 解析到影子 enum，靠 `NativeFunc::data()` 拿到元数据：[crates/typst-library/src/text/mod.rs:69-70](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/text/mod.rs#L69-L70) 中两行完成 `lower`/`upper` 注册。foundations 模块的 `define` 函数里成片的 `global.define_type::<..>()`/`define_func::<..>()` 是整个标准库的装配现场：[crates/typst-library/src/foundations/mod.rs:90-118](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/foundations/mod.rs#L90-L118)。

**作用域内函数的接线**。`#[scope]` 标注 `impl Color` 块时，`handle_fn` 检测到成员函数上的 `#[func]`，自动改写为 `#[func(.., parent = Color)]` 并发出 `scope.define_func_with_data(Color::luma_data())`：[crates/typst-macros/src/scope.rs:249-270](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-macros/src/scope.rs#L249-L270)。`#[scope]` 最终生成 `NativeScope::scope()`，函数体就是一串 `scope.define/define_func/define_type/define_elem` 语句：[crates/typst-macros/src/scope.rs:120-137](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-macros/src/scope.rs#L120-L137)。`Color` 的声明展示了 `#[ty(scope, cast)]` + `#[scope]` 的组合：[crates/typst-library/src/visualize/color.rs:175-185](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/visualize/color.rs#L175-L185)，`#[constant]` 常量（BLACK/WHITE…）与 `luma` 等构造函数都挂在这个作用域里。`#[func(constructor)]` 变体会被登记进 `NativeScope::constructor()` 而非作用域表，同时函数名改用父类型的 `NAME`（[crates/typst-macros/src/func.rs:331-335](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-macros/src/func.rs#L331-L335)）。

**文档站**。`docs/src/reflect.rs` 自己就用 `#[func]` 暴露了 `binding` 和 `describe` 两个查询函数（[L43-77](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/docs/src/reflect.rs#L43-L77)）——文档站的 typst 脚本通过它们反查标准库。`describe_func` 把 `Func` 的全部元数据（name/title/since/docs/def_site/keywords/scope）连同 `func.params()` 逐项搬进字典：[docs/src/reflect.rs:80-100](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/docs/src/reflect.rs#L80-L100)；`describe_param` 输出参数的六个布尔位、`CastInfo` 描述与默认值（默认值是 `fn() -> Value` 闭包，现场调用取值，[L103-122](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/docs/src/reflect.rs#L103-L122)）。你在 [typst.app/docs](https://typst.app/docs) 上看到的每个参数表、每个「默认值」示例，源头都是宏填进去的 `NativeParamInfo`。

**cast 侧的同构**。`cast!` 宏为类型生成 `Reflect`/`IntoValue`/`FromValue` 时，同样把每个 match 臂的 doc 注释变成 `CastInfo::Value(值, 文档)`——错误消息「expected X, found Y」里的候选列表就是这么来的：[crates/typst-macros/src/cast.rs:242-271](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-macros/src/cast.rs#L242-L271)。`Caseable` 的 `cast!`（[crates/typst-library/src/text/case.rs:50-58](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/text/case.rs#L50-L58)）声明「str 或 content 皆可」，于是 `upper` 参数表里的 `input` 自动是「string 或 content」，文档站与错误消息同步展示这一信息——无需任何人手写第二遍。

#### 4.4.4 代码实践

**实践目标**：验证「改一处 doc 注释，三处数据同时变」。

**操作步骤**：

1. 临时修改 [crates/typst-library/src/text/case.rs:30](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/text/case.rs#L30) 的参数 doc 注释（如把 `The text to convert to uppercase.` 改为 `PRACTICE: the text.`）；
2. 重跑 4.2.4 方式 A 的测试，观察 `param docs:` 输出变化；
3. 在 `tests/suite/` 下建一个临时 `.typ` 文件，内容 `#test(1, 1)` 之类即可，重点是在另一行写 `#upper()` 触发缺参错误，用 `cargo run -p typst-cli -- compile 临时.typ` 观察报错是否引用新注释（缺参报错来自 `args.expect`，其中参数名 `text` 就出自宏生成的参数表）；
4. 完成后还原 case.rs，删除临时文件。

**需要观察的现象**：方式 A 的输出携带新注释；CLI 的缺参错误消息包含参数名 `text`（消息形如 `missing argument: text`，具体措辞待本地验证）。

**预期结果**：确认 doc 注释与参数名这两个信息只存在于声明处，其余全部是衍生数据——改一处即全局生效。

#### 4.4.5 小练习与答案

**练习 1**：文档站上函数页的「Parameters」表格数据从哪里来？追一条完整链路。

**答案**：参数 doc 注释 → `#[func]` 的 `create_param_info` 塞进 `NativeParamInfo.docs`（[crates/typst-macros/src/func.rs:442-455](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-macros/src/func.rs#L442-L455)）→ `Func::params()` 运行时读出 → `describe_func`/`describe_param`（[docs/src/reflect.rs:90-122](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/docs/src/reflect.rs#L90-L122)）转成字典 → 文档站 typst 模板渲染成表格。

**练习 2**：`#[func(title = "Uppercase")]` 不写会怎样？写了 `"Upper"` 又会怎样？

**答案**：不写则默认 title case 得到 `Uppercase`（kebab 名 `upper` 转 title case）；写 `"Upper"` 就用自定义值。但如果显式写的值恰好等于默认值（如再写 `"Uppercase"`），编译直接失败——`determine_name_and_title` 的防冗余校验，见 [crates/typst-macros/src/util.rs:211-217](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-macros/src/util.rs#L211-L217)。

## 5. 综合实践

**任务：给 `#[func]` 体系写一份「展开对照报告」**。

以 `upper` 为对象，从三个角度采集数据并互相印证：

1. **声明侧**：抄录 [crates/typst-library/src/text/case.rs:28-34](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-library/src/text/case.rs#L28-L34) 的五行声明（含属性与 doc 注释）。
2. **静态展开侧**：`cargo +nightly expand -p typst-library text::case`（或依据 4.2.3 的推导），截取 `upper` 的三件套：清洗后的 fn、影子 enum、`NativeFuncData` 字面量。逐项核对：`name` 从哪来、`title` 为何是 `"Uppercase"`、`since` 为何是 `Forever`、参数解析为什么选中 `args.expect("text")`、`finish` 兜底为什么存在。
3. **运行时反射侧**：运行 4.2.4 方式 A 的测试，把打印的参数表与展开侧的 `NativeParamInfo` 字面量对照；再故意在 typst 脚本里写 `#upper(1, 2)`，用 CLI 编译观察多余参数报错——它正是闭包第三段 `args.take().finish()?` 的产物。

最后用一张表总结「错误处理样板」：缺参（`expect` 的 `missing argument`）、类型不符（`FromValue` 经 `Caseable` 的 cast 臂报 `expected string or content`）、多余参数（`finish`）、调用点定位（`into_result(.., args.span)`）各自由宏生成的哪一行负责。

产出：一份 Markdown 报告，包含三侧数据与总结表。全部命令对仓库无持久修改（临时测试文件与脚本用后即删）。

## 6. 本讲小结

- `typst-macros` 提供七个宏：`#[func]`、`#[ty]`、`#[elem]`、`#[scope]`、`cast!`、`#[derive(Cast)]`、`#[time]`，全部经 `typst-library` 的门户模块重导出，只依赖 `syn`/`quote`/`heck`/`proc-macro2`。
- `#[func]` 为自由函数生成三件套：清洗后的原函数、同名零变体影子 enum、静态 `NativeFuncData`；作用域内函数则生成 `{name}_data()`。核心是四段式参数解析闭包：绑定 → 逐参解析 → `finish` 兜底 → `into_result` 附 span。
- 参数属性到 `Args` 消费方法有固定映射：`#[variadic]`→`all`、`#[named]`→`named`、带默认值的位置参数→`eat`、普通位置参数→`expect`；`engine`/`context`/`args`/`span` 四个特供参数按名字识别、不进参数表。
- `#[elem]` 生成六类产物；字段种类（required/synthesized/settable/ghost/external/internal）决定存储类型与逐字段 trait；能力列表大多只进动态 vtable 探针，真正的 `Show`/`Synthesize` 等仍由库作者手写；`Construct`/`Set` 写在列表里表示要手写。
- 同步机制的实质：doc 注释、`since`、`keywords`、命名、`DefSite` 全部萃取自唯一声明，经 `NativeFuncData`/`ContentVtable` 分别供给运行时注册、IDE 元数据与 `docs/src/reflect.rs` 文档站——一份声明、三处消费，永不漂移。

## 7. 下一步学习建议

本讲之后，你对「标准库如何被制造」已有完整认知，建议：

1. **[u5-l6 语言工具](u5-l6-ide-tooling.md)**：看 `typst-ide` 如何消费本讲生成的 `NativeParamInfo`/`CastInfo` 做自动补全与签名提示——元数据的第二个消费者。
2. **通读 [crates/typst-macros/src/lib.rs](https://github.com/typst/typst/blob/84cca406aafe7f830ed3f37f1cc6f0c8b6b65a03/crates/typst-macros/src/lib.rs) 的宏文档注释**：它是贡献新内置函数/元素的官方指南，本讲未覆盖的边角（`#[scope(ext)]`、`#[defs]`、`#[parse]` 的写法细节）都在其中。
3. **动手试写**：仿照 `case.rs` 的模式，在本地分支给某个类型加一个最小的 `#[func]` 方法（如给 `Version` 加一个格式化函数），跑通 `cargo build` 后用 `cargo expand` 检查自己的展开，是检验本讲掌握程度的最快方式（改完记得还原）。
4. **回看 [u3-l5](u3-l5-func-and-args.md)**：此时再读 `Args::expect`/`finish` 的实现，你会认出其中每个分支正是宏生成代码的对接点。
