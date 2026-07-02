# TableGen 与代码生成：从 .td 到 .inc

> 本讲承接 [u2-l2 方言定义](u2-l2-dialect-definition.md)。上一讲我们读完了 `Dialect.td`，知道 `cuda_tile` 方言是用 TableGen 的 `.td` 文件「声明」出来的。本讲回答下一个自然的问题：**这些 `.td` 声明是如何变成可以编译、可以链接、甚至可以给人阅读的产物的？** 答案就是 `cuda-tile-tblgen` 工具与它驱动的代码生成（code generation / codegen）流水线。

## 1. 本讲目标

学完本讲，你应当能够：

1. 说清楚「TableGen 记录（Record）— 后端（Backend）— 生成产物」三者的关系，理解 `.td` 文件为什么不是源码、而是一种「描述数据」。
2. 区分 CUDA Tile 里两类代码生成：标准 MLIR 后端（产出 `Ops.h.inc`、`Types.h.inc`、`AttrDefs.h.inc` 等 C++ 胶水代码）和 CUDA Tile 自定义后端（产出规范文档 `ops.rst` 与字节码胶水代码）。
3. 解释 `cuda-tile-tblgen` 为什么必须**最先构建**，以及它在跨编译场景下为什么需要「宿主工具」机制。
4. 读懂 `gen-op-spec` 后端的注册方式，以及 `SpecGen` / `Emitter` 如何把操作上的元数据（`summary`、`description`、`mlirExamples`、`descriptionTables`、`sinceVersion`、分组 `group`）渲染成人类可读的 RST 规范文档。
5. 亲手在构建产物里找到生成的 `.inc` 文件，并运行一次规范生成，观察输出结构。

## 2. 前置知识

如果你对下面这些概念完全陌生，建议先建立直觉再继续：

- **TableGen 是什么**：它是 LLVM 的「数据描述语言 + 记录生成器」。你写 `.td` 文件，里面有 `class`（模板）和 `def`（实例化一条记录）。TableGen 不生成代码本身——它只把所有 `def` 汇总成一张「记录表」（`RecordKeeper`）。真正「把记录翻译成代码」的是一个个**后端**（backend）程序。
- **后端（Backend）**：一个独立的 C++ 程序（如 `mlir-tblgen`、`cuda-tile-tblgen`），它读入记录表，按某种规则输出文本（C++ 头文件、Markdown、RST……）。一个后端程序里通常注册了多个「生成动作」（gen action），用命令行参数选择，例如 `-gen-op-decls`、`-gen-op-spec`。
- **`.inc` 文件**：后端生成的、被 `#include` 进真实头文件的代码片段（inc = include）。它不是手写的，构建时动态产生，因此**绝不会出现在 git 仓库里**，只出现在 `build/` 目录中。
- **RST 与 Sphinx**：reStructuredText 是一种纯文本标记语言；Sphinx 是把 RST 渲染成 HTML/PDF 的工具。CUDA Tile 的规范文档（spec）以 RST 形式由 `cuda-tile-tblgen` 自动生成。
- **`sinceVersion` / 分组 `group`**：上一讲提到，每个操作在 `.td` 里带 `version`（如 `"13.1"`）和 `group`（如 `"Floating Point"`）两项元数据。本讲你会看到它们如何同时服务于「规范文档生成」和「字节码版本兼容」。

> 关键直觉：**`.td` 是单一数据源（single source of truth），多个后端各取所需。** 同一个 `CudaTile_AddFOp` 记录，标准 MLIR 后端读它生成 C++ 类 `AddFOp`，规范后端读它生成文档章节，字节码后端读它生成读写胶水代码。数据写一遍，产物出多份——这就是代码生成的核心价值。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [tools/cuda-tile-tblgen/cuda-tile-tblgen.cpp](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/cuda-tile-tblgen.cpp) | 工具的 `main`，注册 `gen-op-spec` 等生成动作，最终委托给 `MlirTblgenMain`。 |
| [tools/cuda-tile-tblgen/CMakeLists.txt](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/CMakeLists.txt) | 用 `add_tablegen` 把它构建为一个 TableGen 宿主工具。 |
| [CMakeLists.txt](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/CMakeLists.txt) | 顶层构建脚本，决定 `cuda-tile-tblgen` 最先构建的顺序，并设置 `CUDA_TILE_TABLEGEN_EXE`。 |
| [include/cuda_tile/Dialect/CudaTile/IR/CMakeLists.txt](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/CMakeLists.txt) | 调用 `mlir_tablegen` 生成 `.inc`，调用 `tablegen(CUDA_TILE ...)` 生成规范；定义 `CudaTileIncGen`、`CudaTileSpecGen` 等目标。 |
| [include/cuda_tile/Dialect/CudaTile/IR/Ops.h](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.h) | 用 `#define GET_OP_CLASSES` + `#include "Ops.h.inc"` 把生成代码「拼」进真实头文件。 |
| [tools/cuda-tile-tblgen/SpecGen.cpp](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/SpecGen.cpp) | 规范生成主逻辑：`generateSpec` 入口、按分组切分、逐操作 `emitOpDoc`。 |
| [tools/cuda-tile-tblgen/Emitter.cpp](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/Emitter.cpp) 与 [Emitter.h](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/Emitter.h) | `SpecEmitter` 及 RST 渲染辅助类型（`Header`/`Table`/`Badge`/`Code`）。 |
| [tools/cuda-tile-tblgen/CudaTileOp.h](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/CudaTileOp.h) 与 [CudaTileOp.cpp](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/CudaTileOp.cpp) | 元数据中间层：把 MLIR 的 `Operator` 包装成 `CudaTileOp`，提取分组、示例、参数版本等。 |
| [include/cuda_tile/Dialect/CudaTile/IR/Ops.td](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td) 与 [Dialect.td](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Dialect.td) | 被生成器消费的「数据源」：操作定义与元数据基类。 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

1. **4.1** `cuda-tile-tblgen` 工具入口：`main` 与 `GenRegistration`
2. **4.2** 从 `.td` 到 `.inc`：两类 TableGen 代码生成
3. **4.3** `gen-op-spec` 后端：注册与 `generateSpec` 主流程
4. **4.4** `SpecEmitter` 与 `Emitter`：把元数据渲染成规范

---

### 4.1 cuda-tile-tblgen 工具入口：main 与 GenRegistration

#### 4.1.1 概念说明

`cuda-tile-tblgen` 是一个**命令行可执行程序**，本质是 MLIR 上游 `mlir-tblgen` 的「扩展版」。它的全部职责是：读入一张 TableGen 记录表，根据命令行指定的生成动作（如 `-gen-op-spec`），把记录翻译成文本输出到标准输出（或文件）。

MLIR 为这类工具提供了一个现成的框架 `MlirTblgenMain`：你只要写一个 `main`，把「自定义生成动作」**注册**进去，剩下的命令行解析、记录表加载、错误处理都由框架包办。注册的机制是 `mlir::GenRegistration`——一个全局对象，其构造函数把「动作名 + 描述 + 回调函数」登记进一个全局表。`main` 里只需保证这些全局对象在 `MlirTblgenMain` 被调用前已完成构造（C++ 静态初始化天然满足这一点）。

#### 4.1.2 核心流程

```
启动 cuda-tile-tblgen --gen-op-spec ...
        │
        ├─ 静态全局对象先于 main 完成「自注册」
        │     · print-records
        │     · gen-op-spec          ← 自定义后端（本讲重点）
        │     · (MLIR 库自带：-gen-op-decls、-gen-op-doc 等)
        │
        └─ main() → MlirTblgenMain(argc, argv)
              │
              ├─ 解析命令行，选定 --gen-op-spec
              ├─ 加载所有 .td，得到 RecordKeeper（记录表）
              └─ 调用 gen-op-spec 回调 → generateSpec(os, records, ...)
```

关键点：**注册必须发生在命令行解析之前**。因为 `main` 里这些 `GenRegistration` 是函数内的 `static` 局部对象，它们在第一次进入 `main` 时构造，而 `MlirTblgenMain` 在它们之后调用——顺序上注册先于解析，安全。

#### 4.1.3 源码精读

工具 `main` 极其简短，全部含义集中在几个注册语句上：

[tools/cuda-tile-tblgen/cuda-tile-tblgen.cpp:L28-L56](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/cuda-tile-tblgen.cpp#L28-L56) —— 整个 `main`：先注册两个生成动作，最后把控制权交给 `MlirTblgenMain`。

其中第一个注册是调试用的「打印全部记录」：

[tools/cuda-tile-tblgen/cuda-tile-tblgen.cpp:L30-L34](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/cuda-tile-tblgen.cpp#L30-L34) —— `print-records` 动作：把记录表原样打到标准输出，是排查「我写的 `.td` 到底被解析成什么样了」的利器。

第二个注册才是本讲核心的规范生成动作：

[tools/cuda-tile-tblgen/cuda-tile-tblgen.cpp:L43-L52](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/cuda-tile-tblgen.cpp#L43-L52) —— `gen-op-spec` 动作：读取可选的 `--examples-directory`，调用 `cudatile::tblgen::generateSpec(os, records, examplesDirectory)`。

注意 [tools/cuda-tile-tblgen/cuda-tile-tblgen.cpp:L38-L41](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/cuda-tile-tblgen.cpp#L38-L41) 中的 `--examples-directory` 选项：它让规范里的 MLIR 示例**分离**到独立文件（便于编辑器语法高亮、便于测试），而非内联进 RST。

最后，构建侧用 `add_tablegen` 把它注册为项目 `CUDA_TILE` 的 TableGen 工具：

[tools/cuda-tile-tblgen/CMakeLists.txt:L5-L21](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/CMakeLists.txt#L5-L21) —— `add_tablegen(cuda-tile-tblgen CUDA_TILE ...)` 声明源文件清单。这个 LLVM 宏会设置 `CUDA_TILE_TABLEGEN_EXE`，让后续 `tablegen(CUDA_TILE ...)` 调用自动指向这个二进制。

并且它链接了 `MLIRTblgenLib`：

[tools/cuda-tile-tblgen/CMakeLists.txt:L36-L41](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/CMakeLists.txt#L36-L41) —— 链接 `MLIRTblgenLib` 意味着 `cuda-tile-tblgen` **继承了上游 `mlir-tblgen` 的全部后端**（如 `-gen-op-decls`、`-gen-op-doc`），自己只额外加了 `-gen-op-spec` 和字节码后端。换句话说，它是 `mlir-tblgen` 的超集。

#### 4.1.4 代码实践

1. **实践目标**：在不完整构建的前提下，观察 `cuda-tile-tblgen` 的命令行接口与「打印记录」能力。
2. **操作步骤**：
   - 完成一次最小构建（参见 u1-l2），确保 `build/bin/cuda-tile-tblgen` 存在。
   - 运行 `build/bin/cuda-tile-tblgen --help`，在输出里找到 `--gen-op-spec`、`--print-records`、`--examples-directory` 三项，确认它们都来自本节讲到的注册代码。
   - 运行 `build/bin/cuda-tile-tblgen include/cuda_tile/Dialect/CudaTile/IR/Ops.td --print-records | head -40`，观察 TableGen 把 `Ops.td` 解析成的原始记录。
3. **需要观察的现象**：`--help` 中既有 MLIR 通用的 `-gen-op-decls` 等，也有 CUDA Tile 专属的 `-gen-op-spec`；`--print-records` 输出的是类 `def` 形式的文本，能看到 `CudaTile_AddFOp` 这类记录名。
4. **预期结果**：能区分「上游继承的后端」与「本项目新增的后端」。
5. 若环境未构建，此步标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `main` 里的 `GenRegistration` 对象声明成 `static`，而不是普通局部变量？

> **参考答案**：`static` 保证它只在第一次进入 `main` 时构造一次，并持续存活到程序结束，从而在 `MlirTblgenMain` 解析命令行、查找动作时，注册项始终存在于全局注册表里。若是普通局部变量，作用域结束后即析构、注销，后续就找不到该动作了。

**练习 2**：如果删掉 `cuda-tile-tblgen.cpp` 里对 `MLIRTblgenLib` 的链接，命令行里哪个动作会消失？

> **参考答案**：所有从上游继承的标准 MLIR 后端会消失（如 `-gen-op-decls`、`-gen-op-doc`、`-gen-dialect-decls`），因为它们由 `MLIRTblgenLib` 里的静态 `GenRegistration` 提供。本项目自有的 `-gen-op-spec` 仍会保留（它就定义在本文件里）。

---

### 4.2 从 .td 到 .inc：两类 TableGen 代码生成

#### 4.2.1 概念说明

CUDA Tile 的构建里实际并存**两类**代码生成，区分它们是理解整个流水线的关键：

| 维度 | 标准 MLIR 代码生成 | CUDA Tile 自定义代码生成 |
| --- | --- | --- |
| 调用宏 | `mlir_tablegen(...)` | `tablegen(CUDA_TILE ...)` |
| 实际执行的工具 | 上游 `mlir-tblgen` | 本项目 `cuda-tile-tblgen` |
| 典型后端 | `-gen-op-decls`、`-gen-typedef-decls`、`-gen-attrdef-decls` | `-gen-op-spec`、`-gen-cuda-tile-bytecode`、`-gen-cuda-tile-opcodes` |
| 典型产物 | `Ops.h.inc`、`Types.h.inc`、`AttrDefs.h.inc`（C++ 胶水） | `ops.rst`（规范）、`Bytecode.inc`（字节码胶水） |
| 产物去向 | 被 `#include` 进头文件，参与编译 | 文档单独发布；字节码胶水被读写器 `#include` |

第一类生成 C++ 类骨架：例如记录 `CudaTile_AddFOp`（助记符 `addf`）会被 `-gen-op-decls` 生成一个 `AddFOp` C++ 类，包含构造器、`build` 方法、操作数/结果访问器等。手写头文件用一个**约定好的宏 + include** 把这段生成代码「拼」进来。

#### 4.2.2 核心流程

标准 MLIR 类型的「拼接」模式（几乎所有 MLIR 方言通用）：

```
头文件 Ops.h:
    #define GET_OP_CLASSES            ← 开关：告诉 .inc「现在请生成类声明」
    #include "Ops.h.inc"              ← 粘贴生成内容

构建时（include/.../IR/CMakeLists.txt）:
    mlir_tablegen(Ops.h.inc -gen-op-decls)   ← mlir-tblgen 产出该文件
    add_public_tablegen_target(CudaTileIncGen) ← 暴露为一个 CMake 目标
```

为何要把生成与手写分离？因为 `Ops.h.inc` 里包含大量样板（每个操作几十行），手写易错、改动重复。`.td` 一处声明，多个产物同步。

#### 4.2.3 源码精读

**手写头文件如何引入生成代码**——这是 MLIR 代码生成最经典的「include 技巧」：

[include/cuda_tile/Dialect/CudaTile/IR/Ops.h:L53-L54](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.h#L53-L54) —— 先 `#define GET_OP_CLASSES`，再 `#include "Ops.h.inc"`。`Ops.h.inc` 内部用 `#ifdef GET_OP_CLASSES` 守卫，只有定义了这个宏才会吐出操作类声明，否则吐出别的片段（如 `GET_OP_LIST`）。

类型用完全一样的套路：

[include/cuda_tile/Dialect/CudaTile/IR/Types.h:L22-L23](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Types.h#L22-L23) —— `#define GET_TYPEDEF_CLASSES` + `#include "Types.h.inc"`。

**构建侧如何驱动这些生成**——`include/.../IR/CMakeLists.txt` 集中调用 `mlir_tablegen`：

[include/cuda_tile/Dialect/CudaTile/IR/CMakeLists.txt:L30-L34](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/CMakeLists.txt#L30-L34) —— 对 `Ops.td` 调用 `-gen-op-decls` 产出 `Ops.h.inc`、`-gen-op-defs` 产出 `Ops.cpp.inc`，并把它们汇总成公共目标 `CudaTileIncGen`。

类型与属性同理：

[include/cuda_tile/Dialect/CudaTile/IR/CMakeLists.txt:L14-L24](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/CMakeLists.txt#L14-L24) —— `Types.h.inc`（`-gen-typedef-decls`）、`AttrDefs.h.inc`（`-gen-attrdef-decls`）、`Enums.h.inc`（`-gen-enum-decls`）全部由 `mlir_tablegen` 产出。

**被生成的「数据源」**：以 `addf` 为例，看 `.td` 里如何声明一个操作——

[include/cuda_tile/Dialect/CudaTile/IR/Ops.td:L143-L186](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L143-L186) —— `CudaTile_AddFOp` 定义。注意它同时携带「给 C++ 后端用的信息」（`arguments`/`results`/`assemblyFormat`/`hasVerifier`）和「给规范后端用的信息」（`summary`/`description`/`descriptionTables`）。同一份记录，多个后端各取所需。其中数学公式以 `.. math::` 块写出：

\[
\text{addf}(x, y)_i = x_i + y_i
\]

它会被规范后端原样渲染进 RST。

而 `CudaTile_AddFOp` 的基类 `CudaTileFloatingPointOpDef` 把 `group` 固定为 `"Floating Point"`、`version` 透传：

[include/cuda_tile/Dialect/CudaTile/IR/Dialect.td:L151-L152](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Dialect.td#L151-L152) —— 浮点操作分组基类；其父类 `CudaTileOpDef` 把 `version`/`group`/`subGroup` 装进一个 `metadata` 字段：

[include/cuda_tile/Dialect/CudaTile/IR/Dialect.td:L85-L98](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Dialect.td#L85-L98) —— `CudaTileOpDef` 与 `CudaTileOpMetadata` 的定义。`operationVersion` 服务字节码后端，`metadata.cudaTileSpecGroup` 服务规范后端（下一节详解）。

**自定义后端则用 `tablegen(CUDA_TILE ...)`**，走 `cuda-tile-tblgen`：

[include/cuda_tile/Dialect/CudaTile/IR/CMakeLists.txt:L76-L78](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/CMakeLists.txt#L76-L78) —— `tablegen(CUDA_TILE ops.rst -gen-op-spec ...)`：注意这里是 `tablegen(CUDA_TILE ...)` 而非 `mlir_tablegen`，故执行的是 `cuda-tile-tblgen`，调用本讲的 `-gen-op-spec` 后端。字节码胶水同理（详见 [lib/Bytecode/Writer/CMakeLists.txt:L24-L36](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Writer/CMakeLists.txt#L24-L36) 的 `cuda_tile_tablegen` 宏）。

#### 4.2.4 代码实践

1. **实践目标**：亲眼看到「同一份 `.td` 产出多份 `.inc`」。
2. **操作步骤**：
   - 构建完成后，进入 `build/include/cuda_tile/Dialect/CudaTile/IR/`（路径依配置而定）。
   - 用 `ls *.inc` 列出生成文件，确认 `Ops.h.inc`、`Types.h.inc`、`AttrDefs.h.inc`、`Enums.h.inc` 等都存在。
   - 在 `Ops.h.inc` 中检索 `class AddFOp`，阅读其生成的构造函数与访问器骨架（如 `lhs()`、`rhs()`、`result()`）。
3. **需要观察的现象**：`AddFOp` 类的方法签名与 `Ops.td` 里 `arguments`/`results` 声明一一对应；文件顶部通常有 `/* Autogenerated ... */` 注释。
4. **预期结果**：能指出「`Ops.td` 的 `$lhs` → `AddFOp::lhs()`」的对应关系。
5. 若未构建，标注「待本地验证」；可改为阅读 `Ops.td` 推断 `AddFOp` 应有哪些访问器。

#### 4.2.5 小练习与答案

**练习 1**：`Ops.h` 里为什么要先 `#define GET_OP_CLASSES` 再 `#include "Ops.h.inc"`？能否去掉这个宏直接 include？

> **参考答案**：`Ops.h.inc` 是一个「多用途」生成文件，内部用 `#ifdef GET_OP_CLASSES` / `#ifdef GET_OP_LIST` 等宏区分要吐出的片段。不定义宏直接 include，可能什么都生成不出来或生成错误的片段。宏是「选择器」，告诉生成代码本次调用想要哪一部分。

**练习 2**：`Ops.h.inc` 是 C++ 后端的产物，那 `Ops.cpp.inc` 由哪个后端产出、又该被谁 include？

> **参考答案**：由 `-gen-op-defs` 产出（见 CMakeLists L32），包含方法的**定义**（而非声明）。它应在 `Ops.cpp`（实现文件）里被 include，配合 `#define GET_OP_DEFS`（或等价开关）使用，与 `Ops.h.inc`（声明）成对出现。

**练习 3**：为什么字节码胶水 `Bytecode.inc` 用 `tablegen(CUDA_TILE ...)` 而不是 `mlir_tablegen`？

> **参考答案**：因为生成字节码读写胶水的后端（`-gen-cuda-tile-bytecode` 等）是 CUDA Tile **自定义**的，只注册在 `cuda-tile-tblgen` 里，上游 `mlir-tblgen` 没有。`tablegen(CUDA_TILE ...)` 才会调用 `cuda-tile-tblgen`。

---

### 4.3 gen-op-spec 后端：注册与 generateSpec 主流程

#### 4.3.1 概念说明

`gen-op-spec` 后端的目标是：**把方言里所有操作，按分组组织，渲染成一份人类可读的规范文档**（RST）。这份文档就是 README 里反复提到的「CUDA Tile IR specification」的源头。

它需要解决三个问题：

1. **收集**：从记录表里挑出所有「操作」记录（继承自 `Op` 类的 `def`），并支持按正则过滤。
2. **分组与排序**：用 `.td` 里写死的 `group`（Core/Floating Point/…）把操作分箱，并按一个**预定义的章节顺序**排序。
3. **渲染**：对每个操作，依次输出标题、摘要、签名（参数/结果）、描述、属性说明、约束、示例。

这里有一个**元数据中间层** `CudaTileOp`：它把 MLIR 通用的 `Operator` 包装一层，专门暴露「规范关心的字段」（分组、示例、参数版本等），让 `SpecGen` 不直接碰底层 TableGen API，逻辑更清晰。

#### 4.3.2 核心流程

```
generateSpec(os, records, examplesDirectory)
   │
   ├─ 新建 SpecEmitter（负责实际写 RST）
   ├─ emitComment(AUTO_GENERATED_MESSAGE)   ← 「请勿手改」标记
   ├─ splitBySections(records):
   │     · getRequestedOpDefinitions()  收集 + 正则过滤操作
   │     · 按 getCudaTileSpecGroup() 分箱
   │     · 按 cudaTileSections[] 固定顺序排序
   ├─ 收集所有 AttrDef 记录（供属性说明交叉引用）
   └─ 逐分组:
         · 跳过 "Testing" 分组
         · 输出分组锚点 + 标题 + include 预写小节文字
         · 对分组内每个操作 → emitOpDoc()
               ├─ emitOpHeading（操作标题 + 锚点）
               ├─ emitSummary（摘要）
               ├─ emitOperationSignature（参数/结果表）
               ├─ Description + Attributes + DescriptionTables
               ├─ Constraints（类型约束/trait 文本）
               └─ Examples（mlirExamples → emitExample）
```

#### 4.3.3 源码精读

**入口函数**——整体编排：

[tools/cuda-tile-tblgen/SpecGen.cpp:L690-L766](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/SpecGen.cpp#L690-L766) —— `generateSpec`：先写自动生成标记，再 `splitBySections` 得到有序分组，收集 `AttrDef`，最后双层循环「分组 → 操作」调用 `emitOpDoc`。注意 [SpecGen.cpp:L719-L721](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/SpecGen.cpp#L719-L721) 显式跳过 `Testing` 分组——测试用操作不出现在公开规范里。

**分组顺序是硬编码的**：

[tools/cuda-tile-tblgen/SpecGen.cpp:L634-L641](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/SpecGen.cpp#L634-L641) —— `cudaTileSections[]` 定义了规范里 11 个分组的固定出现顺序（Core 在最前，Testing 在最后）。`splitBySections`（[SpecGen.cpp:L643-L688](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/SpecGen.cpp#L643-L688)）据此给每个分组打分排序，保证规范章节顺序稳定、可控。

**操作筛选**：

[tools/cuda-tile-tblgen/SpecGen.cpp:L79-L102](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/SpecGen.cpp#L79-L102) —— `getRequestedOpDefinitions`：遍历所有 `def`，挑出 `Op` 的子类，再用 `--op-include-regex`/`--op-exclude-regex` 过滤。这让「只重生成某一个操作的规范」成为可能。

**单操作渲染**：

[tools/cuda-tile-tblgen/SpecGen.cpp:L538-L631](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/SpecGen.cpp#L538-L631) —— `emitOpDoc`：依次输出标题、摘要、签名（参数/结果）、描述、属性、描述表格、约束、示例。其中 [SpecGen.cpp:L454-L514](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/SpecGen.cpp#L454-L514) 的 `emitOperationSignature` 会把每个参数的 `sinceVersion`（如 `13.1`）渲染成一个绿色徽章（`Badge::successLine`），让读者一眼看出「这个参数从哪个版本起支持」。

**元数据中间层如何取分组**：

[tools/cuda-tile-tblgen/CudaTileOp.cpp:L407-L419](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/CudaTileOp.cpp#L407-L419) —— `getCudaTileSpecGroup`：从操作记录里读 `metadata` 子记录的 `cudaTileSpecGroup` 字段；若读不到，回退到 `"Miscellanous"`。这个字段正是 `Dialect.td` 里 `CudaTileOpMetadata` 装进去的：

[include/cuda_tile/Dialect/CudaTile/IR/Dialect.td:L77-L82](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Dialect.td#L77-L82) —— `CudaTileOpMetadata` 同时持有 `sinceVersion`、`cudaTileSpecGroup`、`cudaTileSpecSubGroup`，把「同一份版本/分组信息」既喂给规范后端，又（通过 `operationVersion`）喂给字节码后端。这是「单一数据源」思想的直接体现。

`mlirExamples` 的提取同理：

[tools/cuda-tile-tblgen/CudaTileOp.cpp:L436-L440](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/CudaTileOp.cpp#L436-L440) —— `getMLIRExamples` 直接读 `.td` 里 `mlirExamples = [...]` 的字符串列表。`OperationSignature` 与 `OperationParameter` 的结构定义见 [CudaTileOp.h:L137-L160](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/CudaTileOp.h#L137-L160) 与 [CudaTileOp.h:L47-L65](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/CudaTileOp.h#L47-L65)。

**构建侧如何产出最终规范文件**：

[include/cuda_tile/Dialect/CudaTile/IR/CMakeLists.txt:L71-L88](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/CMakeLists.txt#L71-L88) —— 规范输出到源码树的 `spec/ops.rst`（`GEN_SPEC_DIR = ${CMAKE_SOURCE_DIR}/spec`），示例输出到 `spec/examples/`，并定义目标 `CudaTileSpecGen`。这意味着运行规范生成会把产物写回**源码树**（而非 build 目录），便于提交审阅。

#### 4.3.4 代码实践

1. **实践目标**：手动触发规范生成并阅读其结构。
2. **操作步骤**：
   - 构建 `cuda-tile-tblgen` 后，执行：
     `build/bin/cuda-tile-tblgen include/cuda_tile/Dialect/CudaTile/IR/Ops.td -I include -gen-op-spec > /tmp/ops.rst`（`-I` 提供 `.td` include 路径，按实际构建参数调整；待本地验证具体参数）。
   - 或更稳妥地用 CMake 目标：`cmake --build build --target CudaTileSpecGen`，然后查看 `spec/ops.rst`。
   - 在 `spec/ops.rst` 里检索 `addf`，定位到 `Floating Point` 分组下它的章节，确认包含 Summary、Parameters、Results、Description、Constraints、Examples 各小节。
3. **需要观察的现象**：分组顺序与 `cudaTileSections[]` 一致；Testing 操作（带 `testing$` 前缀）不出现在规范里；参数后的 `13.1` 徽章来自 `sinceVersion`。
4. **预期结果**：能对照 `Ops.td` 里 `CudaTile_AddFOp` 的字段，在 `ops.rst` 里找到每一项的渲染结果。
5. 若工具参数不确定，标注「待本地验证」并以 CMake 目标方式为准。

#### 4.3.5 小练习与答案

**练习 1**：如果想让某个新操作出现在规范的 `Floating Point` 分组下，需要在 `.td` 里做哪件事？

> **参考答案**：让该操作继承 `CudaTileFloatingPointOpDef`（或直接用 `CudaTileOpDef` 并把 `group` 参数设为 `"Floating Point"`）。基类会把 `"Floating Point"` 写进 `metadata.cudaTileSpecGroup`，`splitBySections` 据此把它归入对应分箱。

**练习 2**：`splitBySections` 为什么不用记录在 `.td` 中出现的先后顺序，而要按 `cudaTileSections[]` 显式排序？

> **参考答案**：TableGen 的 `def` 顺序并不稳定可靠，且无法保证分组之间有序。显式数组让规范章节顺序独立于 `.td` 书写顺序，稳定可控、可读（Core 在前、Testing 在后），也方便统一跳过 Testing。

**练习 3**：`generateSpec` 里专门收集了一份 `attrDefs`（所有 `AttrDef` 子类记录）传给 `emitOpDoc`，作用是什么？

> **参考答案**：供属性说明部分做**交叉引用/展开**——当某个操作的属性是自定义 `AttrDef` 或实现了某属性接口时，`emitAttributeDef`/`emitAttributeInterface`（[SpecGen.cpp:L403-L438](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/SpecGen.cpp#L403-L438)）需要在全部属性定义里查找实现者，把它的说明也渲染出来。

---

### 4.4 SpecEmitter 与 Emitter：把元数据渲染成规范

#### 4.4.1 概念说明

`SpecGen` 决定「输出什么内容、按什么顺序」，而 `SpecEmitter`（定义在 `Emitter.h/cpp`）负责「**怎么**把内容写成合规的 RST」。它把 RST 的琐碎语法（标题下划线、列表表格、代码块、锚点、徽章）封装成一组易用方法，让 `SpecGen` 的代码贴近业务逻辑而非排版细节。

`Emitter.h` 还定义了一组**值类型**（`Header`、`Table`、`Badge`、`Code`、`TileIRTy`），它们都重载了 `operator<<`，可以像普通值一样 `os << Header(...)`，使渲染代码读起来像声明式模板。

#### 4.4.2 核心流程

```
SpecEmitter(os, examplesDirectory)
   │  持有 raw_indented_ostream（支持缩进）+ 可选 examplesDirectory + appendixFile
   │
   ├─ emitComment(msg)        → ".. msg\n\n"
   ├─ emitAnchor(type, name)  → ".. _type-name:\n\n"      （供 :ref: 跳转）
   ├─ emitOpHeading(name)     → 操作级标题(===) + 锚点
   ├─ emitSummary(text)       → 斜体摘要
   ├─ emitCodeBlock(cb)       → ".. code-block::\n\n  ..." （自动缩进）
   ├─ emitDescription(text)   → 处理 ":suffix:" 分段并重排缩进
   └─ emitExample(name, fmt)  → 写示例文件到磁盘 + 追加附录 + literalinclude
```

`emitExample` 是最值得注意的：当提供了 `--examples-directory` 时，它**不**把示例代码内联进 RST，而是 (1) 把示例写成独立 `.mlir` 文件、(2) 在附录 `examples_appendix.rst` 里登记、(3) 在正文用 `.. literalinclude::` 引用。这样示例既是规范的一部分，又能被编辑器/测试单独使用。

#### 4.4.3 源码精读

**SpecEmitter 类与辅助类型总览**：

[tools/cuda-tile-tblgen/Emitter.h:L198-L297](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/Emitter.h#L198-L297) —— `SpecEmitter` 类：持有输出流 `os`、`examplesDirectory`、`appendixFile`，并提供 `emitOpHeading`/`emitSummary`/`emitCodeBlock`/`emitDescription`/`emitAnchor`/`emitComment`/`emitExample` 等方法。其中 [Emitter.h:L40-L42](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/Emitter.h#L40-L42) 定义了三级标题层次常量（`SECTION_HEADER_LEVEL=2`、`OP_HEADER_LEVEL=3`、`OP_DETAILS_HEADER_LEVEL=4`）——之所以从 2 级开始，是因为规范会被嵌入更大文档，根级标题留给外层。

**RST 标题渲染**：

[tools/cuda-tile-tblgen/Emitter.cpp:L35-L51](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/Emitter.cpp#L35-L51) —— `Header` 的 `operator<<`：按层级选择下划线字符（`#*=^"`），再生成一行等长的下划线。这是 RST 标题的标准写法。

**徽章（版本标记）**：

[tools/cuda-tile-tblgen/Emitter.h:L146-L193](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/Emitter.h#L146-L193) 与 [Emitter.cpp:L104-L139](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/Emitter.cpp#L104-L139) —— `Badge` 类型映射到 sphinx-design 的 `:bdg-success-line:` 等角色，正是参数后那个绿色 `13.1` 标记的来源。

**示例分离机制**：

[tools/cuda-tile-tblgen/Emitter.cpp:L231-L247](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/Emitter.cpp#L231-L247) —— `emitExample`：先 `writeExampleToDiskAndAppendToAppendix` 写盘+登记，再 `emitLiteralInclude` 在正文引用。其写盘实现见 [Emitter.cpp:L177-L229](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/Emitter.cpp#L177-L229)：若未设置 `examplesDirectory` 则直接 `return`（即默认内联模式）。

**构造与自动生成标记**：

[tools/cuda-tile-tblgen/Emitter.cpp:L146-L151](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/Emitter.cpp#L146-L151) —— 构造时打开附录文件流。`AUTO_GENERATED_MESSAGE` 定义在 [Emitter.h:L33-L34](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/Emitter.h#L33-L34)，被 `generateSpec` 写在文档最前面，提醒读者「本文件由工具生成、勿手改」。

#### 4.4.4 代码实践

1. **实践目标**：理解 `--examples-directory` 如何改变输出形态。
2. **操作步骤**：
   - 用 CMake 目标 `CudaTileSpecGen` 生成规范（它已经带 `-examples-directory ${GEN_SPEC_DIR}/examples`，见 4.3.3 引用的 CMake L78）。
   - 查看 `spec/examples/` 目录，确认里面有形如 `example_cuda_tile.addf_0.mlir` 的示例文件。
   - 打开 `spec/examples/examples_appendix.rst`，确认每个示例都有锚点与 `literalinclude`。
   - 在 `ops.rst` 里找到 `addf` 的 Examples 小节，确认它用 `See :ref:` 指向附录而非内联代码。
3. **需要观察的现象**：正文是「引用」，示例内容实际在附录与独立 `.mlir` 文件中。
4. **预期结果**：能解释「为何要把示例分离」——便于单独测试与语法高亮。
5. 若未运行生成，标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：`SpecEmitter` 为何从 `SECTION_HEADER_LEVEL = 2` 开始，而不是 1？

> **参考答案**：规范是更大文档的一个子片段，根级（1 级）标题留给外层宿主文档。从 2 级开始保证被嵌入时标题层级不冲突。这是 RST 文档分块组合的常见约定。

**练习 2**：如果删掉 `emitExample` 里对 `writeExampleToDiskAndAppendToAppendix` 的调用、只保留 `emitLiteralInclude`，会发生什么？

> **参考答案**：`emitLiteralInclude` 引用的示例文件不会被创建，`literalinclude` 会指向不存在的路径，构建文档时报「文件找不到」。两个调用必须配套：先写盘登记，再引用。

**练习 3**：`Code` 与 `TileIRTy` 这两个类型为什么都定义了 `operator std::string()` 与 `operator<<`？

> **参考答案**：为了让它们能既作为值参与字符串拼接（`std::string(Code{"lhs"})`），又能直接流式输出到 `raw_ostream`（`os << Code{"lhs"}`）。前者生成 `:code:\`lhs\``、后者生成 `:tileirty:\`...\``，统一了「行内代码」与「TileIR 类型引用」的 RST 角色渲染。

---

## 5. 综合实践

把本讲四个模块串起来，完成一次「**追踪一条记录从声明到全部产物**」的端到端观察。以 `addf`（`CudaTile_AddFOp`）为线索：

1. **数据源**：阅读 [Ops.td:L143-L186](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L143-L186) 的 `CudaTile_AddFOp`，列出它携带的全部信息，并标注每条信息「主要被哪个后端消费」（C++ 后端 / 规范后端 / 字节码后端）。例如：`arguments` → C++ 后端（生成访问器）；`summary`/`description` → 规范后端；`"13.1"` 版本号 → 规范后端（徽章）+ 字节码后端（版本兼容）。

2. **C++ 产物**：构建后打开 `build/.../Ops.h.inc`，找到 `class AddFOp`，对照你在第 1 步列出的 `arguments`/`results`，逐个确认生成的访问器方法。再打开 `Ops.h` 看 `#define GET_OP_CLASSES` + `#include "Ops.h.inc"`（[Ops.h:L53-L54](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.h#L53-L54)），理解拼接点。

3. **规范产物**：运行 `cmake --build build --target CudaTileSpecGen`，在 `spec/ops.rst` 的 `Floating Point` 分组下找到 `addf` 章节，确认它的 Summary / Parameters（带 `13.1` 徽章）/ Results / Description（含数学公式）/ Constraints / Examples 各小节都能追溯到第 1 步的某条 `.td` 字段，以及 `SpecGen.cpp` / `Emitter.cpp` 中的某段渲染逻辑。

4. **工具视角**：运行 `build/bin/cuda-tile-tblgen --help`，把帮助里出现的生成动作分成两组——「上游继承的」与「CUDA Tile 自有的」，并用本讲源码说明后者来自哪里（`cuda-tile-tblgen.cpp` 的 `GenRegistration`，以及 `MLIRTblgenLib` 之外的其他 `*.cpp` 文件，如 `BytecodeGen.cpp`）。

5. **写下结论**：用一段话回答——「为什么 CUDA Tile 要维护一个自己的 `cuda-tile-tblgen`，而不是直接用 `mlir-tblgen`？」预期答案要点：需要自定义后端（规范生成、字节码胶水）；通过链接 `MLIRTblgenLib` 复用全部上游后端，是超集而非替换；单一数据源 `.td` 同时驱动 C++ / 规范 / 字节码三类产物。

> 若构建环境不可用，第 2、3 步可降级为「源码阅读型实践」：凭 `.td` 字段与 `SpecGen.cpp`/`Emitter.cpp` 的渲染逻辑，**推断** `AddFOp` 类与 `addf` 规范章节应分别包含哪些内容，并明确标注「待本地验证」。

## 6. 本讲小结

- **TableGen 是数据，后端是翻译器**：`.td` 里的 `def` 汇成一张记录表，由后端程序按规则翻译成 C++ / RST / 字节码胶水等多份产物，实现「单一数据源」。
- **两类代码生成并存**：`mlir_tablegen(...)` 走上游 `mlir-tblgen`，产出 `Ops.h.inc`/`Types.h.inc`/`AttrDefs.h.inc` 等 C++ 胶水；`tablegen(CUDA_TILE ...)` 走本项目 `cuda-tile-tblgen`，产出规范 `ops.rst` 与字节码胶水。
- **`cuda-tile-tblgen` 是 `mlir-tblgen` 的超集**：它链接 `MLIRTblgenLib`、复用全部上游后端，额外注册 `-gen-op-spec` 与字节码后端。
- **include 技巧**：手写头文件用 `#define GET_OP_CLASSES` + `#include "Ops.h.inc"` 把生成代码拼接进来；类型用 `GET_TYPEDEF_CLASSES`。
- **`gen-op-spec` 的三步**：`splitBySections` 按硬编码分组顺序分箱排序 → 收集 `AttrDef` → 对每个操作 `emitOpDoc` 渲染标题/签名/描述/约束/示例。
- **`SpecEmitter` 封装排版**：把 RST 标题/徽章/表格/锚点/示例分离等细节收口，使 `SpecGen` 专注业务；`--examples-directory` 让示例落盘为独立 `.mlir` 文件并在附录登记。

## 7. 下一步学习建议

- **横向**：本讲只讲了「规范后端」与「标准 C++ 后端」。字节码后端（`BytecodeGen.cpp`、`BytecodeReaderGen.cpp`、`BytecodeTypeCodeGen.cpp`、`BytecodeAttrCodeGen.cpp`）是 `cuda-tile-tblgen` 的另一半自定义后端，将在 **u8-l1（cuda-tile-tblgen 字节码代码生成）** 精读。
- **纵向**：有了 `.inc` 产物的概念，下一单元 **u3（类型系统）** 会直接阅读 `Types.td` 与手写的 `Types.h`/`Types.cpp`，你会真切看到「生成的 `Types.h.inc` 与手写代码如何协作」。
- **延伸阅读**：想深入 MLIR 代码生成机制，可阅读上游 `mlir/TableGen/` 与 `mlir/Tools/mlir-tblgen/` 源码，对照本讲理解 `GenRegistration`、`GET_OP_CLASSES` 的标准实现。
- **动手**：尝试在 `Ops.td` 里给 `CudaTile_AddFOp` 追加一条 `mlirExamples`（参照已有操作写法），重新运行 `CudaTileSpecGen`，观察 `spec/ops.rst` 与 `spec/examples/` 的增量变化——这是检验你是否理解「数据→产物」链路的最直接方式。
