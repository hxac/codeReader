# CudaTile 方言定义：命名空间与默认方言

## 1. 本讲目标

本讲精读 CUDA Tile 项目的「方言骨架」文件 `Dialect.td`。读完后你应当能够：

1. 说清楚 `cuda_tile` 方言的「名字空间、自包含性、默认类型/属性打印解析器」是怎么在 TableGen 里声明的，以及这些声明最终生成了什么 C++ 代码。
2. 看懂 `CudaTileOpDef` 这个「所有操作的公共基类」如何用 `group / subGroup / sinceVersion` 三项元数据把上百个操作组织成 11 个分组，并理解这套元数据在「规范生成」和「字节码版本兼容」中的双重作用。
3. 解释 `CudaTile_DefaultDialect` 这个小工具为什么能让 `entry`、`for` 等「块操作」内部的操作省略 `cuda_tile.` 前缀，从而写出更短、更易读的 IR。
4. 把本讲当作后续讲义（类型系统 u3、计算操作 u4、内存操作 u5、属性 u6、字节码 u7）的「地图索引」——后面遇到的每一个操作，都能在本讲的分组表里找到它所属的家族。

本讲只看「方言是怎么定义出来的」，不展开具体操作、类型、属性的语义（那是后续讲义的内容）。

## 2. 前置知识

在继续之前，先建立三个心智模型。它们来自上一讲 u2-l1（MLIR/LLVM 依赖）和更早的 u1-l1（项目定位）。

### 2.1 什么是 MLIR「方言（Dialect）」

MLIR 是一个可扩展的编译框架。它不预先规定一套固定的指令集，而是允许每个项目定义自己的「方言」。一个方言就是一组带有统一前缀的「操作（Op）」「类型（Type）」「属性（Attribute）」。例如：

- `cuda_tile.addf` —— `cuda_tile` 方言下的浮点加法操作。
- `!cuda_tile.tile<4x8xf32>` —— `cuda_tile` 方言下的 Tile 类型。

前缀（`cuda_tile.`）就是方言的名字。多个方言可以共存于同一段 IR，这是 MLIR 区别于传统编译器中间表示的核心设计。

### 2.2 TableGen（.td）与代码生成（.inc）

CUDA Tile 不手写每个操作的 C++ 类，而是用一门叫 **TableGen** 的领域专用语言（`.td` 文件）来「描述」操作、类型、属性，再由 `cuda-tile-tblgen` 工具（详见 u2-l3）把这些描述「翻译」成大量 C++ 胶水代码（`.inc` 文件，如 `Dialect.h.inc`、`Ops.h.inc`）。

关键直觉是：**`.td` 是「源」，`.inc` 和 C++ 是「产物」**。我们精读 `.td`，就能理解整个方言的骨架，而不必逐行读机器生成的 `.inc`。

TableGen 里最重要的两个语法元素：

- `class`：带参数的「模板/基类」，用来抽取共性。例如 `CudaTileOpDef<...>` 是所有操作的公共基类。
- `def`：从某个 `class` 实例化出来的「具体记录」。例如 `def CudaTile_Dialect : Dialect { ... }` 声明了这个方言本身。

### 2.3 本讲用到的关键术语

| 术语 | 含义 |
|------|------|
| 方言名（dialect name） | IR 里出现在操作/类型前缀里的字符串，本方言是 `cuda_tile` |
| cppNamespace | 生成的 C++ 代码所在的命名空间 |
| 自包含（self-contained） | 方言不依赖任何其它方言 |
| 操作分组（group） | 把功能相近的操作归类，如 `Integer`、`Memory` |
| sinceVersion | 某个操作/类型/属性是「从哪个字节码版本开始引入的」 |
| 块操作（block op） | 拥有 region（一段嵌套 IR）的操作，如 `entry`、`for` |

> 提醒：CUDA Tile 的「版本号」（如 13.3）、「锁定的 LLVM commit」和「字节码版本」是三件不同的事，上一讲 u2-l1 已做区分；本讲的 `sinceVersion` 指的是**字节码版本**（如 `"13.1"`、`"13.3"`）。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 | 本讲关注点 |
|------|------|-----------|
| `include/cuda_tile/Dialect/CudaTile/IR/Dialect.td` | 方言骨架的 TableGen 描述，是本讲的主角 | 方言定义、操作基类、默认方言工具 |
| `include/cuda_tile/Dialect/CudaTile/IR/Dialect.h` | 方言的 C++ 公共头文件，`#include` 了生成的 `Dialect.h.inc` | 命名空间与几个工具函数声明 |
| `lib/Dialect/CudaTile/IR/CudaTile.cpp` | 方言的 C++ 实现 | `CudaTileDialect::initialize()` 注册流程 |
| `lib/Dialect/CudaTile/IR/Attributes.cpp` | 属性实现 | 优化提示如何读取方言上的两个标志 |
| `include/cuda_tile/Dialect/CudaTile/IR/Ops.td` | 全部操作的具体定义 | 为 11 个分组各举一个真实操作例子 |
| `include/cuda_tile/Dialect/CudaTile/IR/TestingOps.td` | 仅测试用操作的定义 | 第 11 个分组 `Testing` 的例子 |

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块，对应用三个 TableGen 顶层结构：`CudaTile_Dialect`、`CudaTileOpDef`（及其 11 个分组子类）、`CudaTile_DefaultDialect`。

### 4.1 CudaTile_Dialect：方言本身的定义

#### 4.1.1 概念说明

在 TableGen 里，一个方言是用 `def : Dialect { ... }` 声明的。这个声明回答了 MLIR 的几个根本问题：

- 这个方言叫什么名字（IR 里的前缀）？
- 生成的 C++ 代码放在哪个命名空间里？
- 它依赖哪些其它方言？
- 它的类型和属性是用「默认自动生成」的打印/解析代码，还是手写自定义代码？

`cuda_tile` 方言的设计选择是：**自包含、不依赖任何其它方言、使用 MLIR 自动生成的类型/属性打印解析器**。这是一切后续语义能干净落地的基础。

#### 4.1.2 核心流程

方言定义从 `.td` 到运行时的流程如下：

1. 开发者在 `Dialect.td` 里写 `def CudaTile_Dialect : Dialect { ... }`，填好 `name`、`cppNamespace`、`description`、`extraClassDeclaration` 等字段。
2. 构建时 `cuda-tile-tblgen`（详见 u2-l3）读取它，生成 `Dialect.h.inc`，里面是一个 `class CudaTileDialect : public ::mlir::Dialect { ... }` 的 C++ 类骨架。
3. `Dialect.h` 用一行 `#include ".../Dialect.h.inc"` 把这个骨架接进项目。
4. 运行时，MLIR 的 `Context` 第一次需要 `cuda_tile` 方言时，会调用 `CudaTileDialect::initialize()`（在 `CudaTile.cpp` 里实现），把操作、类型、属性、接口注册进去。

#### 4.1.3 源码精读

先看方言的核心声明（注意 `name`、`cppNamespace`、`dependentDialects`、`description` 四个字段）：

[include/cuda_tile/Dialect/CudaTile/IR/Dialect.td:33-43](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Dialect.td#L33-L43) —— 声明方言名为 `cuda_tile`，C++ 命名空间为 `::mlir::cuda_tile`，`dependentDialects = []`（不依赖任何其它方言），并说明这是一个完全自包含的方言；同时开启默认的类型/属性打印解析器。

要点逐条解释：

- `let name = "cuda_tile";` —— 这就是 IR 里出现的前缀。于是操作写作 `cuda_tile.addf`，类型写作 `!cuda_tile.tile<...>`。
- `let cppNamespace = "::mlir::cuda_tile";` —— 生成的 C++ 类（如 `CudaTileDialect`、各 Op 类）都在 `mlir::cuda_tile` 命名空间里。`Dialect.h` 顶部也确实包了一层 `namespace mlir::cuda_tile { ... }`：
  [include/cuda_tile/Dialect/CudaTile/IR/Dialect.h:23-51](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Dialect.h#L23-L51) —— C++ 公共头里的命名空间，与 `.td` 里的 `cppNamespace` 一致。
- `let dependentDialects = [];` 与 `description` 里那句「entirely self-contained and independent」互相印证：这是 CUDA Tile 作为「前端 lowering 目标 IR」的重要定位（回顾 u1-l1），它自成一体，不混入 `arith`、`memref` 等上游方言。
- `let useDefaultTypePrinterParser = 1;` 和 `useDefaultAttributePrinterParser = 1;` —— 让 MLIR 根据 `Types.td` / `AttrDefs.td` 里的描述**自动**生成类型的打印/解析代码。这也是为什么类型能写成短形式的 `tile<4x8xf32>`（详见 u3）。

再看方言的「扩展声明」`extraClassDeclaration`。TableGen 允许在 `Dialect` 里直接嵌入一段 C++ 代码，原样塞进生成的 `CudaTileDialect` 类体。CUDA Tile 用它放进了两个布尔标志和一些私有成员：

[include/cuda_tile/Dialect/CudaTile/IR/Dialect.td:45-74](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Dialect.td#L45-L74) —— 通过 `extraClassDeclaration` 注入两个标志 `warnUnsupportedHints_` / `errorOnHints_` 及其 getter/setter，用来控制「优化提示（Optimization Hints）不被支持时怎么办」。

这两个标志是「优化提示」（详见 u6-l1）行为的总开关。直觉上：

- `warnUnsupportedHints_`（默认 `false`，即「静默」）：是否在遇到当前操作/硬件不支持的优化提示时**发一条诊断（告警）**。
- `errorOnHints_`（默认 `false`，即「仅告警」）：是否把上述告警**升级为错误**（注意它的语义依赖前者先打开）。

这套「两级开关」最终在字节码翻译入口被命令行选项设置（回顾 u1-l3 提到的 `cuda-tile-translate`）：

[lib/Bytecode/Translation/BytecodeTranslation.cpp:30-31](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Translation/BytecodeTranslation.cpp#L30-L31) —— 翻译注册时，把全局命令行选项 `--Wunsupported-hints` / `-Werr-hints` 的值写入方言对象上的这两个标志。

而在真正校验提示时，`Attributes.cpp` 会回读这两个标志来决定要不要发诊断、发什么级别的诊断：

[lib/Dialect/CudaTile/IR/Attributes.cpp:200-206](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/Attributes.cpp#L200-L206) —— 如果 `getWarnUnsupportedHints()` 为假就直接 `return success()`（完全静默），否则才逐项检查提示键是否被当前操作支持。

最后看一下生成产物是如何被「激活」的。生成的 `CudaTileDialect` 类的 `initialize()` 在 `CudaTile.cpp` 里实现：

[lib/Dialect/CudaTile/IR/CudaTile.cpp:5643-5651](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L5643-L5651) —— 方言初始化：先注册属性、再注册类型（对应 `extraClassDeclaration` 里声明的私有 `registerAttributes()` / `registerTypes()`），然后用 `GET_OP_LIST` 宏从 `Ops.cpp.inc` 里把全部操作一次性 `addOperations` 进来，最后挂上内联与汇编两个接口。

注意这里的 `registerAttributes()` / `registerTypes()` 正是 `Dialect.td` 的 `extraClassDeclaration` 里在 `private:` 段声明的那两个函数。`.td` 只声明，`CudaTile.cpp` 提供（间接）实现——这是 TableGen 与手写 C++ 协作的典型模式。

#### 4.1.4 代码实践

这是一个「源码阅读 + 命令行观察」型的实践。

1. **实践目标**：亲眼看到 `Dialect.td` 里的字段是如何「穿透」到运行时行为上的。
2. **操作步骤**：
   - 打开 `Dialect.td`，确认 `name`、`cppNamespace` 的值。
   - 在一个已构建的 `build/` 目录里找到生成的 `Dialect.h.inc`（通常在 `build/.../include/cuda_tile/Dialect/CudaTile/IR/Dialect.h.inc`），用只读方式打开，找到生成的 `class CudaTileDialect`，确认它继承了 `::mlir::Dialect`，并且类体里出现了 `warnUnsupportedHints_` 成员和 `getDefaultDialect` 之外的那几个 getter/setter。
   - 找一个带 `optimization_hints` 的测试文件（例如 `test/Dialect/CudaTile/opt_hints.mlir`），分别用下面三条命令跑 `cuda-tile-opt`（工具路径以你的构建为准），对比输出：
     - `cuda-tile-opt example.mlir`（默认，静默）
     - `cuda-tile-opt --Wunsupported-hints example.mlir`（发告警）
     - `cuda-tile-opt --Wunsupported-hints --Werr-hints example.mlir`（告警升级为错误）
3. **需要观察的现象**：随着开关逐级打开，原本被静默忽略的「不支持的提示键」会先变成告警、再变成错误。
4. **预期结果**：默认情况下无任何额外输出；加 `--Wunsupported-hints` 后出现类似 `... is not known hint for current Operation` 的告警；再加 `--Werr-hints` 后同一处提示导致验证失败、进程返回非零。
5. 若本机尚未构建成功或找不到工具，请标注「待本地验证」，但前面的 `.td` / `.inc` 阅读步骤仍可完成。

> 说明：上面用到的命令行选项名以本仓库 `BytecodeTranslation.cpp` 与 `CommandLineOptions` 的实际注册为准（`getWarnUnsupportedHints` / `getErrorUnsupportedHints`）。如在你本地版本中选项名有差异，以源码注册处为准。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `cppNamespace` 改成 `::mlir::foo`，会发生什么？
**答案**：所有生成的 C++ 类（`CudaTileDialect`、各 Op/Type/Attr 类）都会落到 `mlir::foo` 命名空间里；而 `Dialect.h` 顶部写死了 `namespace mlir::cuda_tile`，于是会编译失败（命名空间不匹配）。这正说明 `cppNamespace` 必须与手写头文件的 `namespace` 保持一致。

**练习 2**：`dependentDialects = []` 说明了什么？如果未来某个操作需要 `arith` 方言的类型，要不要改这里？
**答案**：说明本方言自包含、不依赖其它方言。若真引入了对其它方言的依赖（例如 lowering 时需要），通常要在 `dependentDialects` 里列出，以便 MLIR 在加载本方言时自动加载被依赖的方言。但 CUDA Tile 的设计目标是保持自包含，所以原则上不会轻易改动。

---

### 4.2 CudaTileOpDef：操作分组基类与元数据

#### 4.2.1 概念说明

CUDA Tile 有上百个操作。如果每个操作都从零声明，会有大量重复。`Dialect.td` 用两级抽象来治理这种复杂度：

1. 一个公共基类 `CudaTileOpDef`，承载**所有操作共有**的元数据（版本、示例、描述表格）。
2. 一组「分组子类」（`CudaTileIntegerOpDef`、`CudaTileMemOpDef` 等），把操作按功能归类，**自动填好** `group` 字段。

这套设计有两个直接收益：

- **规范文档自动生成**：`group / subGroup` 决定了人类可读规范里操作的章节归属（详见 u8-l2 的 `gen-op-spec`）。
- **版本兼容**：`sinceVersion` 标注「这个操作从哪个字节码版本开始存在」，读写器据此判断前后向兼容（详见 u7-l4）。

#### 4.2.2 核心流程

一个操作从「分组子类」到「分组归属」的流程：

1. 定义操作时，选择合适的分组子类，例如 `def CudaTile_AddIOp : CudaTileIntegerOpDef<"addi", "13.1", [...traits]>`。
2. 分组子类内部转发到公共基类 `CudaTileOpDef`，并把第二参数 `group` 固定为分组名（如 `"Integer"`）。
3. `CudaTileOpDef` 再用 `group / subGroup / version` 三项构造一个 `CudaTileOpMetadata` 记录，挂到操作上。
4. 规范生成器与字节码生成器分别读取 `metadata` 与 `operationVersion` 字段，完成各自工作。

伪代码示意：

```
CudaTileIntegerOpDef<"addi", "13.1", traits>
        │  group = "Integer", subGroup = ""
        ▼
CudaTileOpDef<"addi", "13.1", "Integer", "", traits>
        │  构造 metadata
        ▼
metadata = CudaTileOpMetadata<"13.1", "Integer", "">
        │
        ├── 规范生成器读取 group → 归入「Integer」章节
        └── 字节码生成器读取 operationVersion="13.1" → 版本兼容判断
```

#### 4.2.3 源码精读

先看公共基类与元数据记录：

[include/cuda_tile/Dialect/CudaTile/IR/Dialect.td:77-98](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Dialect.td#L77-L98) —— `CudaTileOpMetadata`（规范生成用的三元元数据）与 `CudaTileOpDef`（所有操作的公共基类，继承自 MLIR 的 `Op`，并固化方言为 `CudaTile_Dialect`）。

要点：

- `CudaTileOpDef` 的第一行 `: Op<CudaTile_Dialect, mnemonic, traits>` 把**方言固定为 `CudaTile_Dialect`**——所以后面所有分组子类、所有具体操作，都不必再重复指明方言。
- `operationVersion`：给字节码生成用的版本字符串（区别于 `metadata.sinceVersion`，二者当前取同值，但用途不同）。
- `mlirExamples`：一组写在 `.td` 里的 MLIR 示例片段，会被规范生成器原样渲染（详见 u8-l2）。这就是为什么操作定义里经常能看到 `# entry @example(...) { ... }` 形式的注释。
- `descriptionTables`：描述用的结构化表格（本文件顶部第 16–31 行定义了 `TableHeader` / `TableRow` / `Table` 三个辅助类），同样服务于规范生成。

再看分组子类。它们是最重要的「目录」：每个子类只是把 `group` 固定下来，转发给基类。下面这一段集中定义了 10 个分组（第 11 个 `Testing` 在条件编译块里）：

[include/cuda_tile/Dialect/CudaTile/IR/Dialect.td:142-186](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Dialect.td#L142-L186) —— 定义了 Bitwise / Integer / Floating Point / Atomics / Conversions / Core / Control Flow / Memory / Views / Miscellaneous 共 10 个分组子类；`Testing` 子类被 `#ifdef TILE_IR_INCLUDE_TESTS` 包裹（回顾 u1-l2 提到的「仅测试构建」宏），且会给助记符自动加 `testing$` 前缀，确保测试操作不会泄漏到正式发布里。

注意 `CudaTileTestingOpDef` 的小技巧：`CudaTileOpDef<"testing$" # mnemonic, ...>`，用 `#` 拼接字符串，让测试操作的最终助记符变成 `testing$xxx`，前缀与正式操作隔离。

为每个分组各举一个真实操作（均可在 `Ops.td` / `TestingOps.td` 中找到）：

| 分组（group） | 分组子类（.td） | 示例操作 | 助记符 | 定义位置 |
|---|---|---|---|---|
| Bitwise | `CudaTileBitwiseOpDef` | `CudaTile_AndIOp` | `andi` | [Ops.td:192](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L192) |
| Integer | `CudaTileIntegerOpDef` | `CudaTile_AddIOp` | `addi` | [Ops.td:123](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L123) |
| Floating Point | `CudaTileFloatingPointOpDef` | `CudaTile_AddFOp` | `addf` | [Ops.td:146](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L146) |
| Atomics | `CudaTileAtomicsOpDef` | `CudaTile_AtomicRMWTkoOp` | `atomic_rmw_tko` | [Ops.td:516](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L516) |
| Conversions | `CudaTileConversionOpDef` | `CudaTile_BitcastOp` | `bitcast` | [Ops.td:748](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L748) |
| Core | `CudaTileCoreOpDef` | `CudaTile_ConstantOp` | `constant` | [Ops.td:1157](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L1157) |
| Control Flow | `CudaTileControlFlowOpDef` | `CudaTile_BreakOp` | `break` | [Ops.td:938](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L938) |
| Memory | `CudaTileMemOpDef` | `CudaTile_AllocaOp` | `alloca` | [Ops.td:256](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L256) |
| Views | `CudaTileViewOpDef` | `CudaTile_LoadViewTkoOp` | `load_view_tko` | [Ops.td:2650](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L2650) |
| Miscellaneous | `CudaTileMiscOpDef` | `CudaTile_AssumeOp` | `assume` | [Ops.td:300](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L300) |
| Testing（仅测试构建） | `CudaTileTestingOpDef` | `CudaTile_Test_FuncOp` | `testing$func` | [TestingOps.td:33](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/TestingOps.td#L33) |

> 这张表是后续讲义的索引：u4 讲 Integer/Floating Point/Conversions/Core，u5 讲 Memory/Views/Control Flow，u6 讲 Miscellaneous（assume）/属性，Testing 分组只在测试构建中出现（详见 u1-l2 的 `TILE_IR_INCLUDE_TESTS`）。

最后看一个具体操作是如何使用分组子类的，以 `addf` 为例：

[include/cuda_tile/Dialect/CudaTile/IR/Ops.td:146-160](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L146-L160)（节选）—— `CudaTile_AddFOp` 继承 `CudaTileFloatingPointOpDef<"addf", "13.1", ...>`，于是它自动归属于 `Floating Point` 分组、`sinceVersion = "13.1"`；定义者只需关心参数、结果、汇编格式，不必再写分组与版本。

#### 4.2.4 代码实践

1. **实践目标**：把 11 个分组基类与真实操作对应起来，亲手做一次「目录归档」。
2. **操作步骤**：
   - 打开 `Dialect.td` 第 142–186 行，确认 11 个分组子类（10 个常规 + 1 个 Testing）。
   - 用下面的命令（只读检索）在 `Ops.td` / `TestingOps.td` 中分别为每个分组至少找到一个真实操作定义：
     ```
     grep -n 'CudaTileBitwiseOpDef\|CudaTileIntegerOpDef\|...' include/cuda_tile/Dialect/CudaTile/IR/Ops.td
     ```
   - 整理出类似上表的一张「分组 → 操作」对照表。
3. **需要观察的现象**：每个分组都能找到至少一个真实操作；`Testing` 分组只在 `TestingOps.td` 里出现，且助记符都带 `testing$` 前缀。
4. **预期结果**：得到一张 11 行的对照表；其中 10 个常规分组的操作在正式构建中存在，`Testing` 分组的操作只在开启测试（`CUDA_TILE_ENABLE_TESTING=ON`）时被编译进方言。
5. 这一步纯为源码阅读，无需运行，必定可得结果。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `CudaTileOpDef` 要单独存一个 `operationVersion`，而又通过 `metadata` 存一份 `sinceVersion`？两者看起来值一样。
**答案**：它们服务于不同的「消费者」。`operationVersion` 主要给字节码生成/读写器使用（决定一个操作在哪个字节码版本下可序列化，见 u7/u8）；`metadata`（含 `sinceVersion/group/subGroup`）主要给规范生成器使用（决定文档里的章节归属与「自某版本起」标注，见 u8-l2）。分开存放让两条生成链路解耦，即便未来二者取值不一致也不会互相干扰。

**练习 2**：`CudaTileTestingOpDef` 为什么要给助记符加 `testing$` 前缀，并放在 `#ifdef TILE_IR_INCLUDE_TESTS` 里？
**答案**：双重保险。`#ifdef` 保证测试操作在正式发布构建里**根本不被编译**；`testing$` 前缀则保证即便意外混入，其助记符也与正式操作不同，不会与真实指令冲突。这是「测试设施不污染生产语义」的常见做法。

**练习 3**：如果你想新增一个操作 `foo`，属于 `Core` 分组、自 13.3 引入，应该继承哪个类、怎么写版本？
**答案**：继承 `CudaTileCoreOpDef<"foo", "13.3", [...traits]>`。分组名 `Core` 由子类自动填入，无需手写；`"13.3"` 会同时成为 `operationVersion` 和 `metadata.sinceVersion`。

---

### 4.3 CudaTile_DefaultDialect：默认方言与前缀省略

#### 4.3.1 概念说明

MLIR 里，操作名默认要带方言前缀，写作 `cuda_tile.addf`。但当一大段 IR 几乎全是同一个方言的操作时，前缀会显得冗长。MLIR 提供了一个机制：如果一个「块操作」（拥有 region 的操作，如 `cuda_tile.module`、`entry`、`for`）通过 `OpAsmOpInterface::getDefaultDialect()` 声明了「默认方言」，那么它** region 内部**的操作就可以省略前缀。

CUDA Tile 把这段样板代码抽成了一个可复用的 TableGen 记录 `CudaTile_DefaultDialect`，让所有需要的块操作共享，避免每个操作各写一遍。

#### 4.3.2 核心流程

前缀省略的生效流程：

1. 某个块操作在自己的 `extraClassDeclaration` 里拼接 `CudaTile_DefaultDialect.classDecl`。
2. 这会在生成的 C++ Op 类里注入一个静态方法 `static StringRef getDefaultDialect()`，返回 `CudaTileDialect::getDialectNamespace()`（即 `"cuda_tile"`）。
3. MLIR 的汇编打印机/解析器在进入该操作 region 时，读取这个默认方言，对其内部操作自动省略/补全 `cuda_tile.` 前缀。

效果对照（示意，非项目原文）：

```
// 不使用默认方言（伪代码，仅示意）：
cuda_tile.entry @k {
  %r = cuda_tile.addf %a, %b : !cuda_tile.tile<4xf32>
}

// 使用默认方言后，region 内可省略前缀：
entry @k {
  %r = addf %a, %b : tile<4xf32>     // 均省略 cuda_tile. / !cuda_tile.
}
```

#### 4.3.3 源码精读

先看这个可复用工具本身的定义：

[include/cuda_tile/Dialect/CudaTile/IR/Dialect.td:276-290](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Dialect.td#L276-L290) —— `CudaTile_DefaultDialect` 只是一个「携带一段 C++ 代码（`classDecl`）的记录」；这段代码定义了静态方法 `getDefaultDialect()`，返回本方言的命名空间字符串。

关键点：

- 它本身**不是**一个操作、也不是一个类型，只是一个「代码片段容器」，供其它操作 `#` 拼接到自己的 `extraClassDeclaration`。
- `CudaTileDialect::getDialectNamespace()` 是 MLIR 自动为方言生成的静态方法，返回 `let name` 里设置的 `"cuda_tile"`。

再看它是怎么被「消费」的。以 `for` 操作为例：

[include/cuda_tile/Dialect/CudaTile/IR/Ops.td:1990-2012](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L1990-L2012) —— `ForOp` 的 `extraClassDeclaration` 用 `CudaTile_DefaultDialect.classDecl # [{ ... }]` 把默认方言代码与「归纳变量/region 迭代值」的辅助方法拼在一起。于是 `for` 内部的操作可以省略前缀。

`Ops.td` 中共有 7 处以同样方式拼接 `CudaTile_DefaultDialect.classDecl`（分别在行 1990、2208、2478、3041、3405、3969、4228），对应 `for`、`loop`、`if`、`entry`、`module` 等关键块操作。这就是为什么你在 README 和测试里看到的 `entry @kernel(...) { ... }` 块体内部，操作都不带 `cuda_tile.` 前缀。

> 小结：`CudaTile_DefaultDialect` 是「写法层面的便利」——它不改变 IR 的语义，只改变 IR 文本的「可读性」。生成的字节码（`.tilebc`）里前缀省略与否并不影响语义，详见 u7。

#### 4.3.4 代码实践

1. **实践目标**：观察「默认方言」对 IR 文本可读性的影响。
2. **操作步骤**：
   - 在 `Ops.td` 中检索 `CudaTile_DefaultDialect.classDecl`，数一下共有几处（应为 7 处），并确认它们都属于「带 region 的块操作」。
   - 打开 `test/Dialect/CudaTile/ops.mlir`（或任意带 `entry` 的测试文件），观察 `entry { ... }` 内部的操作是否省略了前缀。
   - 若已构建，挑一段测试用 `cuda-tile-opt` 做 round-trip（读入再打印），观察打印机是否会自动省略前缀、把类型打印成短形式 `tile<...>`。
3. **需要观察的现象**：`entry`/`for` 等 region 内部操作无 `cuda_tile.` 前缀；类型写作 `tile<4xf32>` 而非 `!cuda_tile.tile<4xf32>`。
4. **预期结果**：读入与打印后的 IR 在前缀上保持一致（都被省略），语义不变。
5. 若本机未构建，可只做 `.td`/`.mlir` 阅读部分，并标注 round-trip 部分为「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：如果把某个块操作的 `extraClassDeclaration` 里的 `CudaTile_DefaultDialect.classDecl` 去掉，会发生什么？
**答案**：该操作 region 内部的操作在**文本形式**上将不再自动省略前缀，需要显式写 `cuda_tile.addf` 等；但语义和字节码完全不变。这只是文本约定。

**练习 2**：`getDefaultDialect()` 返回的是 `getDialectNamespace()`，它和 `Dialect.td` 里的 `name` 是什么关系？
**答案**：`name = "cuda_tile"` 是 TableGen 里的方言名；MLIR 据此生成 `CudaTileDialect::getDialectNamespace()`，返回同一个字符串 `"cuda_tile"`。所以「默认方言」本质上就是把自己声明为 region 内的默认前缀。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「画一张方言骨架图」的小任务：

1. **绘制方言骨架**：用你喜欢的工具（纸笔/Markdown 表格/绘图工具）画出 `cuda_tile` 方言的骨架，至少包含：
   - 方言名、C++ 命名空间、自包含声明（来自 4.1）。
   - 公共基类 `CudaTileOpDef` 与 11 个分组子类的继承关系（来自 4.2）。
   - `CudaTile_DefaultDialect` 如何被块操作复用（来自 4.3）。
2. **填一张「分组索引表」**：把 4.2.3 节那张 11 行的「分组 → 示例操作」表抄录下来，并在「示例操作」一列补充每个操作的**一句话用途**（提示：用途可以从该操作 `summary` 字段读到，例如 `addi` 的 summary 是「Element-wise integer addition」）。
3. **追踪一个标志**：从命令行选项出发，追踪 `warnUnsupportedHints_` 的完整链路：
   `--Wunsupported-hints`（命令行）→ `BytecodeTranslation.cpp` 的 `setWarnUnsupportedHints` → 方言对象上的 `warnUnsupportedHints_` 字段 → `Attributes.cpp::OptimizationHintsAttr::verifyParamWithContext` 的早返回判断。
   把这条链路上涉及的文件与行号列出来。
4. **自我检查**：回答三个问题——
   - `cppNamespace` 改了不改手写头文件会怎样？（4.1.5）
   - 为什么 `operationVersion` 与 `metadata.sinceVersion` 分开存？（4.2.5）
   - `CudaTile_DefaultDialect` 改变的是语义还是可读性？（4.3.5）

完成上述任务后，你应当能够不看源码，向别人讲清楚「`cuda_tile` 方言在 `.td` 层面是怎么搭起来的」。

## 6. 本讲小结

- `cuda_tile` 方言在 `Dialect.td` 里用 `def CudaTile_Dialect : Dialect` 声明：名字 `cuda_tile`、C++ 命名空间 `::mlir::cuda_tile`、`dependentDialects = []`（自包含）、开启默认类型/属性打印解析器；并通过 `extraClassDeclaration` 注入了两个优化提示开关 `warnUnsupportedHints_` / `errorOnHints_`。
- 生成的 `CudaTileDialect::initialize()`（在 `CudaTile.cpp`）按「属性 → 类型 → 操作 → 接口」的顺序把方言内容注册进 MLIR Context。
- 所有操作共享公共基类 `CudaTileOpDef`，它固化方言、携带 `operationVersion / mlirExamples / descriptionTables / metadata`；`metadata` 的 `group / subGroup / sinceVersion` 同时服务于规范生成与版本兼容。
- 11 个分组子类（Bitwise/Integer/Floating Point/Atomics/Conversions/Core/Control Flow/Memory/Views/Miscellaneous/Testing）只是把 `group` 固定下来，让操作定义者只关心语义；`Testing` 分组被 `#ifdef TILE_IR_INCLUDE_TESTS` 保护并加 `testing$` 前缀，确保不污染正式语义。
- `CudaTile_DefaultDialect` 是一个可复用的「代码片段容器」，被 `entry`/`for`/`if`/`module` 等块操作拼接进 `extraClassDeclaration`，让 region 内部操作省略 `cuda_tile.` 前缀——它只影响文本可读性，不影响语义或字节码。
- 本讲是后续 u3（类型）、u4（计算操作）、u5（内存/控制流）、u6（属性）、u7/u8（字节码与代码生成）的「目录页」：后面遇到的每个操作/类型/属性，都能回到本讲的分组表与方言声明里找到它的归属。

## 7. 下一步学习建议

- **紧接着学 u2-l3（TableGen 与代码生成）**：本讲反复提到「`.td` 生成 `.inc`」，下一讲会打开 `cuda-tile-tblgen` 工具，亲手看到 `Dialect.h.inc` / `Ops.h.inc` 是怎么被生成出来的，把本讲的「源 → 产物」直觉坐实。
- **随后进入 u3（类型系统）**：本讲只看了「方言骨架」，还没讲 `tile<4x8xf32>`、`ptr<f32>`、各类 View 类型本身；u3 会精读 `Types.td`，对应本讲 4.1 里提到的「默认类型打印解析器」所服务的对象。
- **顺带读两段源码加深印象**：
  - 通读 `Dialect.td` 全文（不到 300 行），确认除了本讲讲的三个结构外，还有 `CudaTileI32EnumAttr` / `CudaTileTypeDef` / `CudaTileAttrDef` / `CudaTileArg` 等基类——它们分别对应后续讲义里的属性、类型、参数声明，提前建立印象。
  - 浏览 `Ops.td` 顶部第 1–120 行，体会「一个具体操作定义」长什么样（参数、结果、`assemblyFormat`、`description`），为 u4 做铺垫。
