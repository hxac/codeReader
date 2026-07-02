# CUDA Tile IR 规范自动生成

## 1. 本讲目标

CUDA Tile IR 的官方规范文档（`ops.rst`）并不是手写的——它由构建期工具 `cuda-tile-tblgen` 从方言定义 `.td` 文件**自动生成**。本讲要回答三个问题：

1. `--gen-op-spec` 这个动作是如何注册、如何被 CMake 触发的？
2. `SpecGen.cpp` 的 `generateSpec` 如何把一堆散乱的 TableGen 记录整理成「分组 → 操作 → 签名/描述/约束/示例」的有序文档？
3. `Emitter` 如何用一组「值类型」把内存里的文档模型渲染成 RST（reStructuredText）文本，并把示例代码分离落盘？

学完后，你应当能够：追踪「一条 `.td` 注解（如 `mlirExamples`）最终变成规范里哪一段文字」的完整链路；理解 `CudaTileOp` 这个「元数据中间层」在原始 MLIR `Operator` 与渲染器之间起的缓冲作用；并知道新增一个操作时，规范文档为什么会「自动」跟上。

## 2. 前置知识

在进入本讲前，你需要熟悉以下概念（均来自前置讲义）：

- **TableGen 记录与后端**（u2-l3）：`.td` 文件用 `class`/`def` 声明「记录（Record）」，由 `tblgen` 工具的某个「后端（backend）」读取并产出 `.inc` / `.rst` 等产物。`cuda-tile-tblgen` 是上游 `mlir-tblgen` 的超集。
- **GenRegistration 自注册**（u2-l3、u8-l1）：MLIR 用 `mlir::GenRegistration` 的全局静态对象把「动作名 + 描述 + lambda」登记进工具，`MlirTblgenMain` 据此分派。
- **CudaTileOpDef 与操作分组**（u2-l2）：所有操作派生自 `CudaTileOpDef`，被归入 Bitwise/Integer/Floating Point/Atomics/Conversions/Core/Control Flow/Memory/Views/Miscellaneous/Testing 等 11 个分组，每个分组是一个 `.td` 子基类（如 `CudaTileMiscOpDef`）。
- **sinceVersion**（u7-l4、u8-l1）：操作、属性、参数都带有「自从哪个字节码版本引入」的元数据，既用于版本兼容判断，也用于规范里的小徽章（badge）。
- **RST（reStructuredText）**：Python/Sphinx 生态的标记语言，本讲生成的就是 RST 片段；`.. include::`、`.. literalinclude::`、`.. list-table::` 都是 RST 指令。

本讲不再重复上述机制的原理，而是聚焦于「规范生成」这一条具体的代码生成链路。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [tools/cuda-tile-tblgen/cuda-tile-tblgen.cpp](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/cuda-tile-tblgen.cpp) | 工具入口；注册 `gen-op-spec` 动作与 `--examples-directory` 选项，委托 `MlirTblgenMain`。 |
| [tools/cuda-tile-tblgen/SpecGen.cpp](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/SpecGen.cpp) | 规范生成核心；`generateSpec` 入口、按分组排序、逐操作 `emitOpDoc` 渲染、示例文本处理 `processExample`。 |
| [tools/cuda-tile-tblgen/SpecGen.h](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/SpecGen.h) | 暴露 `generateSpec` 的唯一声明。 |
| [tools/cuda-tile-tblgen/CudaTileOp.cpp](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/CudaTileOp.cpp) | 元数据中间层；把原始 `mlir::tblgen::Operator` 解析成 `OperationSignature`、提取 `mlirExamples` / `descriptionTables` / 分组 / 约束。 |
| [tools/cuda-tile-tblgen/CudaTileOp.h](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/CudaTileOp.h) | `OperationParameter`、`OperationConstraint` 变体、`CudaTileOp` 类的定义。 |
| [tools/cuda-tile-tblgen/Emitter.cpp](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/Emitter.cpp) | 渲染器实现；`Header` / `Table` / `Badge` 的 `operator<<`、示例落盘与附录写入。 |
| [tools/cuda-tile-tblgen/Emitter.h](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/Emitter.h) | 值类型与 `SpecEmitter` 类定义、各级标题级别常量。 |
| [include/cuda_tile/Dialect/CudaTile/IR/Dialect.td](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Dialect.td) | `CudaTileOpDef` / `CudaTileOpMetadata` / `CudaTileArgMetadata` / `OnlyVariants` / `Table` 等「单一数据源」定义。 |
| [include/cuda_tile/Dialect/CudaTile/IR/Ops.td](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td) | 真实操作定义，带 `mlirExamples` / `descriptionTables` 注解的样本来源。 |
| [include/cuda_tile/Dialect/CudaTile/IR/CMakeLists.txt](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/CMakeLists.txt) | 用 `tablegen(CUDA_TILE ops.rst -gen-op-spec ...)` 把工具接入构建，产出落到源码树的 `spec/` 目录。 |

---

## 4. 核心概念与源码讲解

### 4.1 gen-op-spec 注册：从命令行到 generateSpec

#### 4.1.1 概念说明

`cuda-tile-tblgen` 是上游 `mlir-tblgen` 的超集（见 u2-l3、u8-l1），它复用上游全部标准后端，并通过 `mlir::GenRegistration` 额外挂入本项目自定义的动作。规范生成就是其中一个自定义动作，名字叫 **`gen-op-spec`**。

注册的套路是「一个全局静态对象」：`GenRegistration` 的构造函数接收 `(动作名, 描述, 回调lambda)`，在 `main` 执行前就把这个三元组插入全局注册表；随后 `MlirTblgenMain` 解析命令行，看到 `--gen-op-spec` 就调用对应 lambda，把 TableGen 的全部记录 `RecordKeeper` 和一个输出流 `raw_ostream` 交给它。

#### 4.1.2 核心流程

```text
命令行: cuda-tile-tblgen --gen-op-spec Ops.td -o ops.rst [--examples-directory DIR]
        │
        ▼
MlirTblgenMain 解析参数 ──► 查注册表 ──► 命中 "gen-op-spec" 的 lambda
        │
        ▼
lambda: 取出 --examples-directory（可选） ──► 调用 generateSpec(os, records, examplesDirectory)
        │
        ▼
generateSpec 把记录分组、排序、逐操作渲染，把 RST 文本写入 os
```

注意一个本讲反复出现的硬约束（与 u7-l1 的翻译注册同源）：**所有 `GenRegistration` 与 `cl::opt` 必须在 `MlirTblgenMain` 解析命令行之前完成构造**。这里靠的是「静态对象在 `main` 入口前初始化」这一 C++ 特性来满足。

#### 4.1.3 源码精读

注册发生在工具入口 [cuda-tile-tblgen.cpp:43-52](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/cuda-tile-tblgen.cpp#L43-L52)：`GenRegistration genSpecificationRegister` 把 `"gen-op-spec"` 绑定到一个 lambda，lambda 内先读 `--examples-directory`（为空则传入 `std::nullopt`），再调用 `cudatile::tblgen::generateSpec`。

`--examples-directory` 选项本身定义在 [cuda-tile-tblgen.cpp:38-41](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/cuda-tile-tblgen.cpp#L38-L41)，是一个普通的 `llvm::cl::opt<std::string>`。它就是 4.4 节要讲的「示例分离落盘」开关：传了就把示例 `.mlir` 文件写到该目录、并在规范里用 `literalinclude` 引用；不传则示例只内联渲染、不落盘。

最后 [cuda-tile-tblgen.cpp:54](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/cuda-tile-tblgen.cpp#L54) 把控制权交给 `MlirTblgenMain(argc, argv)`，与 u8-l1 的字节码后端共用同一套分派骨架。

再看 CMake 侧如何把它接入构建：[include/cuda_tile/Dialect/CudaTile/IR/CMakeLists.txt:78](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/CMakeLists.txt#L78) 一行

```cmake
tablegen(CUDA_TILE ops.rst -gen-op-spec -examples-directory ${GEN_SPEC_DIR}/examples)
```

这里的 `tablegen(CUDA_TILE ...)` 是 MLIR 提供的 CMake 宏，它会把第一个参数 `CUDA_TILE` 解析为「使用本项目自己的 `cuda-tile-tblgen`」（而不是上游 `mlir-tblgen`），并以 `Ops.td`（由前一行 `set(LLVM_TARGET_DEFINITIONS Ops.td)` 指定）为输入、`ops.rst` 为输出。`GEN_SPEC_DIR` 在 [CMakeLists.txt:71](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/CMakeLists.txt#L71) 定义为 `${CMAKE_SOURCE_DIR}/spec`，所以规范产物**直接落到源码树的 `spec/` 目录**而非 build 目录。最终目标 `CudaTileSpecGen`（[CMakeLists.txt:88](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/CMakeLists.txt#L88)）被挂到 `mlir-doc`（[CMakeLists.txt:94](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/CMakeLists.txt#L94)），因此 `cmake --build build --target mlir-doc` 会连带触发规范生成。

#### 4.1.4 代码实践

**实践目标**：亲手触发规范生成，确认 `gen-op-spec` 的输入与输出落点。

**操作步骤**：

1. 按前置讲义配置并构建一次（启用 TOOLS，默认即开）：
   ```bash
   cmake -G Ninja -S . -B build -DCMAKE_BUILD_TYPE=Release -DCUDA_TILE_ENABLE_TOOLS=ON
   cmake --build build --target cuda-tile-tblgen
   ```
2. 触发规范生成目标：
   ```bash
   cmake --build build --target CudaTileSpecGen
   ```
3. 检查产物：
   ```bash
   ls spec/                       # 应能看到 ops.rst
   ls spec/examples/              # 应能看到 example_*.mlir 与 examples_appendix.rst
   ```

**需要观察的现象**：`spec/ops.rst` 顶部应有一行注释 `.. Autogenerated by cuda-tile-tblgen; don't manually edit`（即 4.4 节的 `AUTO_GENERATED_MESSAGE`）；`spec/examples/` 下每个示例都是独立的 `.mlir` 文件。

**预期结果**：能成功生成 `ops.rst` 与一批 `example_*.mlir`。若 `CUDA_TILE_TABLEGEN_EXE` 未定义，构建会在 [CMakeLists.txt:65-67](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/CMakeLists.txt#L65-L67) 处 `FATAL_ERROR`，说明 `cuda-tile-tblgen` 必须先于本目标构建完成（见 u1-l2 的 tblgen→include/lib 依赖链）。

> 若无法本地构建，标注「待本地验证」即可，可改为直接阅读源码理解流程。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `GenRegistration` 对象必须写成 `static` 局部变量（在 `main` 函数体内）？改成普通局部非 static 变量会怎样？

**参考答案**：`static` 保证它在 `main` 第一次执行到该声明时构造，且生命周期延续到程序结束；更重要的是，注册表是全局的，必须在 `MlirTblgenMain` 解析命令行（即 `main` 函数末尾的 `return MlirTblgenMain(...)`）之前完成登记。若去掉 `static`，该对象会在每次 `main` 执行时构造一次（程序只跑一次倒也能注册成功），但这是未定义行为的灰色地带——上游约定一律用 `static`，确保「定义即注册、且恰好一次」。

**练习 2**：`--examples-directory` 不传时，`generateSpec` 收到的第三个参数是什么？

**参考答案**：一个空的 `std::optional<std::string>`（即 `std::nullopt`）。见 [cuda-tile-tblgen.cpp:46-49](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/cuda-tile-tblgen.cpp#L46-L49)：`if (!opExamplesDirectory.empty())` 才赋值，否则保持默认的空 optional。

---

### 4.2 CudaTileOp 元数据中间层：.td 字段如何变成渲染数据

#### 4.2.1 概念说明

MLIR 自带的 `mlir::tblgen::Operator` 类已经很复杂，但它的 API 偏底层、字段命名面向 C++ 代码生成，直接拿来渲染人类可读文档会很别扭。因此 CUDA Tile 在 `Operator` 与渲染器之间塞了一个**中间层** `CudaTileOp`（连同 `OperationSignature` / `OperationParameter`），它的职责是：

- 把 `.td` 里 CUDA Tile 特有的注解（`mlirExamples`、`descriptionTables`、`metadata.cudaTileSpecGroup`、`CudaTileArgMetadata`、`OnlyVariants`）抽出来，变成易消费的 C++ 结构体；
- 把操作的各种 trait（`AllTypesMatch`、`SameOperandsAndResultElementType` 等）翻译成「人话约束」。

这一层是「数据源（`.td`）」与「表现层（`Emitter`）」之间的缓冲，让渲染器不必关心 TableGen 细节。

#### 4.2.2 核心流程：.td 注解 → C++ 字段

```text
.td 字段                            C++ 中间层                       渲染去向
─────────────────────────────────────────────────────────────────────────
metadata.cudaTileSpecGroup   ──►  CudaTileOp::getCudaTileSpecGroup() ──► 分组标题
mlirExamples (list<string>)  ──►  CudaTileOp::getMLIRExamples()      ──► Examples 段
descriptionTables (list<Table>) ─► getDescriptionTables()            ──► list-table
arguments/results            ──►  OperationSignature                 ──► Parameters/Results 表
  └ CudaTileArgMetadata        ──►  OperationParameter.specDesc/        ──► 每个参数的描述+版本徽章
                                   sinceVersion
  └ OnlyVariants              ──►  OperationParameter.selectedVariants ──► 枚举属性只显示选中变体
traits (AllTypesMatch, ...)  ──►  OperationConstraint (variant)      ──► Constraints 列表
```

#### 4.2.3 源码精读

**数据源侧（`.td`）**：所有注解都挂在操作基类上。[Dialect.td:84-98](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Dialect.td#L84-L98) 的 `CudaTileOpDef` 声明了三个关键字段：`mlirExamples`（`list<string>`，默认空）、`descriptionTables`（`list<Table>`，默认空）、以及一个 `metadata` 子记录（`CudaTileOpMetadata`）。`CudaTileOpMetadata` 定义在 [Dialect.td:78-82](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Dialect.td#L78-L82)，承载 `sinceVersion` / `cudaTileSpecGroup` / `cudaTileSpecSubGroup` 三项。各分组子基类（如 [Dialect.td:179-180](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Dialect.td#L179-L180) 的 `CudaTileMiscOpDef`）把 group 写死，因此单个操作 `def` 通常不必显式写 group。

`Table` / `TableHeader` / `TableRow` 三个 `.td` 类定义在 [Dialect.td:16-31](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Dialect.td#L16-L31)，是 `descriptionTables` 的元素类型。真实用例见 `addf` 操作的「修饰符表」[Ops.td:159-168](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L159-L168)，它用一行 `Table<"...", "...", [headers], [rows]>` 描述各浮点类型支持的舍入模式。

参数级注解通过 `CudaTileArg` 包装。[Dialect.td:217-218](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Dialect.td#L217-L218) 显示 `CudaTileArg<...>` 在原始 `Arg<...>` 的 decorators 列表末尾**自动追加** `CudaTileArgMetadata<version, desc>`。也就是说，前端作者只要用 `CudaTileArg` 声明参数，就免费得到了 `specDesc` 和 `sinceVersion`，无需额外手写。`CudaTileArgMetadata` 本身定义在 [Dialect.td:206-209](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Dialect.td#L206-L209)，是个 `OpVariableDecorator`。同类的 `OnlyVariants`（[Dialect.td:212-214](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Dialect.td#L212-L214)）则用来「只文档化枚举的某些变体」，例如某操作只支持 `atomic_rmw` 的部分模式。

**消费侧（C++）**：[CudaTileOp.h:162-182](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/CudaTileOp.h#L162-L182) 的 `CudaTileOp` 类把上述字段抽成易用方法。其中 `OperationSignature`（[CudaTileOp.h:137-160](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/CudaTileOp.h#L137-L160)）持有 `name` / `parameters` / `results` / `constraints` 四项，并提供 `getParamOrResult(name)` 按名查参数，供后续约束推导复用。`OperationConstraint` 是一个 `std::variant`（[CudaTileOp.h:132-135](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/CudaTileOp.h#L132-L135)），把 9 种约束（`AllTypesMatch` / `AllElementTypeMatch` / `SameOperandsAndResultShape` / `TypesMatchWith` / `AnyTypeOf` / `SameOperandsAndResultElementType` / `AllRanksMatch` / `SameTypeOperands` / `OperationTrait`）统一成一个类型，4.3 节会用 `std::visit` 分派渲染。

三个 getter 的实现都很短：`getMLIRExamples()` 直接读 `mlirExamples` 字段（[CudaTileOp.cpp:436-440](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/CudaTileOp.cpp#L436-L440)）；`getCudaTileSpecGroup()` 读 `metadata` 子记录的 `cudaTileSpecGroup`，**缺失时回落到字符串 `"Miscellanous"`**（[CudaTileOp.cpp:407-419](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/CudaTileOp.cpp#L407-L419)，注意源码里这个回落值是个拼写错误，少了一个 `e`，但它是真实存在的兜底分支）；`getDescriptionTables()`（[CudaTileOp.cpp:487-499](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/CudaTileOp.cpp#L487-L499)）逐个把 `Table` 记录经 `getTableFromRecord`（[CudaTileOp.cpp:442-485](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/CudaTileOp.cpp#L442-L485)）翻译成 `Table` 值对象。

最复杂的是 `OperationSignature` 构造函数 [CudaTileOp.cpp:289-398](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/CudaTileOp.cpp#L289-L398)。它对每个参数（`ins`）和结果（`outs`）：

1. 判断参数种类：`NamedTypeConstraint`（普通操作数）、`NamedAttribute`（属性）、`NamedProperty`（属性 property）——分别对应 `kArgument` / `kAttribute` / `kProperty`（[CudaTileOp.cpp:306-320](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/CudaTileOp.cpp#L306-L320)）；
2. 遍历该参数的「装饰器（decorators）」列表，识别 `CudaTileArgMetadata`（取出 `sinceVersion` 与 `specDesc`）和 `OnlyVariants`（取出 `selectedVariants`）（[CudaTileOp.cpp:326-342](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/CudaTileOp.cpp#L326-L342)）；
3. 最后调用 `getOperationConstraints(op, *this)` 从 trait 推导约束（[CudaTileOp.cpp:397](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/CudaTileOp.cpp#L397)）。

参数描述有个兜底设计值得注意：`OperationParameter::getDescription()`（[CudaTileOp.cpp:85-94](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/CudaTileOp.cpp#L85-L94)）若 `specDesc` 为空就 `PrintFatalError`——也就是说，**规范生成时每个参数都必须有描述**，这是用构建期断言强制「文档完整性」。

#### 4.2.4 代码实践

**实践目标**：在真实 `.td` 中识别本节讲的三类注解，并追踪它们对应的 getter。

**操作步骤**：

1. 打开 [include/cuda_tile/Dialect/CudaTile/IR/Ops.td](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td)，定位 `def CudaTile_AssertOp`（约第 213 行起）。
2. 找到它的 `let mlirExamples = [[{ ... }]]`（[Ops.td:231-237](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L231-L237)），注意其中以 `#` 开头的「脚手架」行（`# cuda_tile.module`、`# entry`、`# }`）。
3. 找到它的 `arguments`，确认用的是 `CudaTileArg<...>` 包装（[Ops.td:239-240](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L239-L240)）。
4. 在 [CudaTileOp.cpp](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/CudaTileOp.cpp) 中分别定位 `getMLIRExamples`、`OperationSignature` 构造里读 `CudaTileArgMetadata` 的循环，对照理解「`.td` 写了什么 → C++ 读到了什么」。

**需要观察的现象**：`assert` 操作的两个参数 `$condition` 和 `$message` 各自带一句描述（`"The condition tile to check."` 等）和版本 `"13.1"`，这些字符串会原样出现在最终规范的 Parameters 表里。

**预期结果**：能讲清楚「`CudaTileArg<..., "The condition tile to check.", "13.1">` 中的描述串和版本串，是如何经由 `CudaTileArgMetadata` 装饰器被 `OperationSignature` 构造函数读出」的完整路径。若仅做静态阅读，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `CudaTileArg` 要把 `CudaTileArgMetadata` 自动追加到 decorators 末尾，而不是让前端作者每次手写？

**参考答案**：单一数据源（DRY）。前端作者写 `CudaTileArg<Constraint, desc, version>` 时已经提供了 `desc` 和 `version`，包装器 [Dialect.td:217-218](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Dialect.td#L217-L218) 自动把它们包进 `CudaTileArgMetadata<version, desc>`，避免重复输入、也避免「代码生成用的 desc」与「规范用的 specDesc」不一致。

**练习 2**：`getCudaTileSpecGroup()` 在 `.td` 没写 `metadata` 时返回什么？这个返回值对分组排序有什么影响？

**参考答案**：返回拼写有误的字符串 `"Miscellanous"`（[CudaTileOp.cpp:419](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/CudaTileOp.cpp#L419)）。由于 4.3 节的排序表里没有这个名字，它会落到 `cudaTileGroupLabels` 之外、得分为 `-1`，从而被排到所有正常分组之前（最前面）。正因如此，几乎所有操作都通过分组子基类显式继承了正确的 group，避免触发这个兜底分支。

---

### 4.3 SpecGen.generateSpec：分组、排序与逐操作渲染

#### 4.3.1 概念说明

`generateSpec` 是规范生成的总调度。它要解决两个问题：

1. **顺序问题**：TableGen 记录在 `.td` 里的出现顺序对读者毫无意义，必须按「Core → Conversions → Control Flow → ...」的固定逻辑顺序排列，让规范可读。
2. **统一渲染问题**：每个操作都要按相同的骨架（标题 → 摘要 → 签名 → 描述 → 属性 → 表格 → 约束 → 示例）输出，避免手写文档常见的「格式漂移」。

它把上一节的 `CudaTileOp` 中间层与下一节的 `Emitter` 表现层缝合起来。

#### 4.3.2 核心流程

```text
generateSpec(os, records, examplesDirectory)
  │
  ├─ 构造 SpecEmitter（同时打开 examples_appendix.rst）
  ├─ emitComment(AUTO_GENERATED_MESSAGE)            # "don't manually edit"
  │
  ├─ splitBySections(records)                       # 按分组收集并排序
  │     ├─ getRequestedOpDefinitions()              #   筛出所有 Op 子类（可被 include/exclude 正则过滤）
  │     ├─ 按 getCudaTileSpecGroup() 分桶
  │     └─ 按 cudaTileSections[] 硬编码顺序排序
  │
  ├─ 收集所有 AttrDef 记录（供属性接口渲染时查实现者）
  │
  └─ for each (groupLabel, groupOps) in orderedSections:
        if groupLabel == "Testing": continue        # 跳过测试操作
        emitAnchor("op-group", normalizedLabel)     # RST 锚点
        emit Header(SECTION_HEADER_LEVEL, label)    # 分组大标题
        emit ".. include:: /sections/op_class_headings/<label>_heading.rst"
        for each opDef in groupOps:
            CudaTileOp cudaTileOp(Operator(opDef))
            emitOpDoc(emitter, cudaTileOp, attrDefs)  # 单操作渲染
```

#### 4.3.3 源码精读

总入口 `generateSpec` 在 [SpecGen.cpp:690-766](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/SpecGen.cpp#L690-L766)。它先把输出流包装成 `raw_indented_ostream`（支持缩进栈），构造 `SpecEmitter`（构造时就会打开附录文件，见 4.4 节），发出自动生成注释，然后调用 `splitBySections` 得到「分组名 → 操作记录列表」的有序序列，并另行收集所有 `AttrDef` 记录（[SpecGen.cpp:704-709](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/SpecGen.cpp#L704-L709)）。主循环 [SpecGen.cpp:711-765](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/SpecGen.cpp#L711-L765) 对每个分组：跳过 `"Testing"`（与 u2-l2 的 `TILE_IR_INCLUDE_TESTS` 保护呼应，测试操作不进公开规范）；发出 `op-group-<label>` 锚点；发出 `SECTION_HEADER_LEVEL`（=2）的大标题；发出一行 `.. include:: /sections/op_class_headings/<label>_heading.rst` 来引入**手写的分组导言**（这部分仍是手写 RST，把自动生成与人类叙述结合）；最后逐操作调用 `emitOpDoc`。

分组排序的核心是 `splitBySections`（[SpecGen.cpp:643-688](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/SpecGen.cpp#L643-L688)）。它先把操作按 `getCudaTileSpecGroup()` 装进 `unordered_map`（分桶），再依据一个**硬编码顺序数组**排序：

```cpp
static const char *const cudaTileSections[] = {
    "Core", "Conversions", "Control Flow", "Memory",
    "Floating Point", "Integer", "Bitwise", "Atomics",
    "Views", "Miscellaneous", "Testing",
};
```

见 [SpecGen.cpp:634-638](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/SpecGen.cpp#L634-L638)。排序逻辑（[SpecGen.cpp:670-685](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/SpecGen.cpp#L670-L685)）给数组中每个名字分配 `下标+1` 作为分数（`Core`=1, `Conversions`=2, …, `Testing`=11），不在表里的分组得 `-1`，从而被排到最前。**这意味着：分组的展示顺序由这个 C++ 数组唯一决定，与 `.td` 里 def 的书写顺序无关**——这正是规范「逻辑顺序稳定」的保证。

操作筛选由 `getRequestedOpDefinitions`（[SpecGen.cpp:79-102](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/SpecGen.cpp#L79-L102)）完成：遍历全部记录，保留 `Op` 的子类，再用 `--op-include-regex` / `--op-exclude-regex`（[SpecGen.cpp:62-69](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/SpecGen.cpp#L62-L69)）两个正则做白/黑名单过滤——这两个选项方便只重生成某一个操作的规范片段。

单操作渲染 `emitOpDoc` 在 [SpecGen.cpp:538-631](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/SpecGen.cpp#L538-L631)，它按固定骨架依次输出：

1. **操作标题**：`emitOpHeading(opName)`，级别 `OP_HEADER_LEVEL`=3（[SpecGen.cpp:545](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/SpecGen.cpp#L545)）；
2. **摘要** summary：若有则斜体输出（[SpecGen.cpp:563-564](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/SpecGen.cpp#L563-L564)）；
3. **签名**：`emitOperationSignature` 输出 Parameters 与 Results 两个小表（级别 4）；
4. **描述** description：`emitDescription`，支持 `:suffix:` 标记切分前后两段（[SpecGen.cpp:568-571](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/SpecGen.cpp#L568-L571)）；
5. **属性**：遍历 `getAttributes()`，分枚举属性 / 自定义属性 / 属性接口三类渲染（[SpecGen.cpp:574-577](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/SpecGen.cpp#L574-L577)）；
6. **描述表**：遍历 `getDescriptionTables()`，每个表加一个 `table-<op>-<i>` 锚点后直接 `<<` 给输出流（[SpecGen.cpp:580-588](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/SpecGen.cpp#L580-L588)）；
7. **约束**：遍历 `signature.constraints`，每条用 `emitOperationConstraint` 分派（[SpecGen.cpp:593-604](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/SpecGen.cpp#L593-L604)）；
8. **示例**：仅当 `getMLIRExamples()` 非空时输出 Examples 段（[SpecGen.cpp:608-617](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/SpecGen.cpp#L608-L617)）。

签名渲染 `emitOperationSignature`（[SpecGen.cpp:454-514](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/SpecGen.cpp#L454-L514)）值得细看：它为每个参数输出 `- **name** (类型描述) - 描述 [版本徽章]`，其中「版本徽章」当且仅当 `parameter.sinceVersion` 非空时附加 `Badge::successLine(...)`（[SpecGen.cpp:484-486](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/SpecGen.cpp#L484-L486) 与 [SpecGen.cpp:506-508](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/SpecGen.cpp#L506-L508)）。这就是「参数级 sinceVersion → 规范里的绿色版本徽章」的渲染点，它直接消费了 4.2 节 `CudaTileArgMetadata` 抽出的版本串。

约束渲染是 `std::variant` + `std::visit` 的典型用法：`emitOperationConstraint`（[SpecGen.cpp:350-372](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/SpecGen.cpp#L350-L372)）用一个 `overloaded` 访问器把 9 种约束变体分别路由到 `emitAllTypesMatch` / `emitSameOperandsAndResultElementType` 等小函数，每个负责把约束渲染成一句人话（如「`lhs` and `rhs` must have the same element type (...)」）。这把「机器约束」翻译成了「读者友好的句子」。

#### 4.3.4 代码实践

**实践目标**：在生成的 `ops.rst` 中验证本节讲的渲染骨架，对照源码确认每一段的出处。

**操作步骤**：

1. 先按 4.1.4 节生成 `spec/ops.rst`。
2. 在 `ops.rst` 中搜索 `addf`，定位到 Floating Point 分组下。
3. 对照检查以下要素是否齐全且顺序正确：
   - 操作标题（3 级，`=` 下划线）；
   - Parameters 列表，其中 `rounding_mode` / `flush_to_zero` 等参数后面应有版本徽章（如 `:bdg-success-line:\`13.1\``）；
   - 名为 `:code:\`addf\` Modifiers` 的描述表（来自 [Ops.td:159-168](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L159-L168) 的 `descriptionTables`），渲染成 `.. list-table::`；
   - Constraints 段，应含一句「`lhs` and `rhs` must have the same element type」之类的人话约束；
   - Examples 段（若 `addf` 配了 `mlirExamples`），含一个 `.. literalinclude::` 指向 `_spec_gen/examples/` 下的示例文件。
4. 在源码侧用 `--op-include-regex` 只生成单个操作（需直接调用工具，见 4.4.4 的命令），对比「全量」与「单操作」输出的差异。

**需要观察的现象**：`ops.rst` 里各分组的出现顺序与 [SpecGen.cpp:634-638](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/SpecGen.cpp#L634-L638) 的 `cudaTileSections[]` 完全一致（Core 最前），与 `Ops.td` 中 def 的物理顺序无关；Testing 分组完全缺席。

**预期结果**：能逐条把 `ops.rst` 里的段落对应回 `emitOpDoc` 的 8 个步骤。若未本地构建，标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：若把 `cudaTileSections[]` 数组里 `"Core"` 和 `"Integer"` 对调，会发生什么？

**参考答案**：Integer 分组会排到最前、Core 次之，其余不变。因为排序分数完全由「名字在数组中的下标」决定（[SpecGen.cpp:660-685](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/SpecGen.cpp#L660-L685)），改数组即改展示顺序，无需改任何 `.td`。这是「顺序集中管理」的好处。

**练习 2**：`emitOpDoc` 里描述表（descriptionTables）和属性（attributes）的渲染顺序是什么？约束（constraints）放在哪里？

**参考答案**：顺序是「标题 → summary → signature → description → attributes → descriptionTables → constraints → examples」。描述表在属性之后、约束之前（[SpecGen.cpp:574-604](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/SpecGen.cpp#L574-L604)），约束在示例之前。

---

### 4.4 Emitter 渲染：RST 值类型与示例分离落盘

#### 4.4.1 概念说明

`Emitter` 是表现层。它的设计有两个特点：

1. **值类型（value types）即渲染单元**：`Header`、`Table`、`Badge`、`Code`、`TileIRTy` 都是「数据 + `operator<<`」的小结构体，把 RST 排版细节封装在 `<<` 里，调用方只管构造数据、不管转义和缩进。
2. **示例分离落盘**：每条 `mlirExamples` 不是直接内联到 `ops.rst`，而是写成独立的 `.mlir` 文件落到 `spec/examples/`，再用 RST 的 `.. literalinclude::` 按行引用。这样同一份示例既能被 Sphinx 渲染、又能被测试工具直接消费。

#### 4.4.2 核心流程：一条示例的旅程

```text
.td: let mlirExamples = [[{ ...多行字符串... }]]
        │  CudaTileOp::getMLIRExamples() 取出
        ▼
emitOpDoc → emitOperationExample(exampleName, example)
        │
        ▼
processExample(example)                # 关键预处理
   ├─ 区分 '#' 开头行（脚手架）与普通行
   ├─ '#' 行：去掉 '#'，保留在内容里，但不计入 lineRanges
   ├─ 普通行/空行：计入 lineRanges
   ├─ 计算 reindent/dedent（统一缩进）
   └─ 压缩连续行号 → FormattedExample{lineRanges, content, dedent}
        │
        ▼
SpecEmitter::emitExample(...)
   ├─ writeExampleToDiskAndAppendToAppendix()
   │     ├─ 把完整 content 写入 spec/examples/example_<name>.mlir
   │     └─ 在 examples_appendix.rst 追加锚点+标题+literalinclude（无 :lines:）
   └─ emitLiteralInclude()              # 写进 ops.rst
         └─ .. literalinclude:: /_spec_gen/examples/example_<name>.mlir
              :lines: <压缩后的行范围>     # 只显示非脚手架行
              :language: mlir
              :dedent: <n>
```

**关键直觉**：以 `#` 开头的行是「脚手架」（如 `# cuda_tile.module @module {`、`# entry @example(...) {`、`# }`），它们让示例成为一段结构完整的代码，但读者关心的是中间那几行业务代码。`processExample` 通过「`#` 行不计入 `lineRanges`」让 `:lines:` 指令在最终展示时**自动隐藏脚手架**，只露出核心几行；而完整文件（含脚手架）仍写到磁盘，供需要完整上下文的场合使用。

#### 4.4.3 源码精读

**值类型与 `operator<<`**：[Emitter.cpp:35-51](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/Emitter.cpp#L35-L51) 的 `Header` 渲染把级别映射成 RST 下划线字符（1→`#`、2→`*`、3→`=`、4→`-`、5→`^`、6→`"`），并自动生成与标题等长的下划线串。由于规范会被 `.. include::` 嵌进更大的文档，根级别被刻意设为 2（[Emitter.h:40-42](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/Emitter.h#L40-L42)：`SECTION_HEADER_LEVEL=2`、`OP_HEADER_LEVEL=3`、`OP_DETAILS_HEADER_LEVEL=4`），把顶级标题让给外层文档。

`Table` 的 `<<`（[Emitter.cpp:53-102](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/Emitter.cpp#L53-L102)）渲染成 `.. list-table::` 指令，支持 `:widths:`、`:header-rows: 1`，并对 `kCode` 列用 `:code:`...`` 包裹——这正是 `addf` 修饰符表能渲染成带代码列的 RST 表格的原因。

`Badge`（[Emitter.cpp:104-139](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/Emitter.cpp#L104-L139)）对应 Sphinx 的 `sphinx-design` 徽章角色（如 `:bdg-success-line:\`13.1\``），用于在参数后标注引入版本。

**SpecEmitter 类**（[Emitter.h:198-297](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/Emitter.h#L198-L297)）聚合了一个 `raw_indented_ostream`、一个可选的 `examplesDirectory`、以及一个 `appendixFile`（`std::ofstream`）。它的构造函数 [Emitter.cpp:146-151](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/Emitter.cpp#L146-L151) 在创建时就打开 `examples_appendix.rst`——**即使没有示例也会创建这个文件**（因为路径来自 `examplesDirectory.value()`，若未传 `--examples-directory` 则构造会触发未定义行为，故 CMake 总是传入该参数，见 4.1.3）。

各类 `emit*` 方法封装了 RST 指令：`emitAnchor` 生成 `.. _<anchor>:`（[Emitter.h:268-272](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/Emitter.h#L268-L272)）；`emitDescription` 支持 `:suffix:` 切分（[Emitter.h:253-266](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/Emitter.h#L253-L266)）；`emitCodeBlock` 用两层 `indent()`（各 2 空格）生成 4 空格缩进的 `.. code-block::`（[Emitter.h:233-251](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/Emitter.h#L233-L251)）。

**示例预处理 `processExample`**（[SpecGen.cpp:104-186](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/SpecGen.cpp#L104-L186)）是本节最微妙的逻辑。逐行扫描时：

- 若行首非空白字符是 `#`（[SpecGen.cpp:121-129](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/SpecGen.cpp#L121-L129)）：把 `#` 删掉、该行保留在内容里，但**不**加入 `lineRanges`；
- 否则（普通行或空行）：加入 `lineRanges`（[SpecGen.cpp:130-140](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/SpecGen.cpp#L130-L140)）；
- 同时统计所有行的最小前导空白 `reindent`，用于统一去缩进。

随后把连续的 `(n,n)` 单行区间压缩成 `(a,b)` 跨行区间（[SpecGen.cpp:159-174](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/SpecGen.cpp#L159-L174)），减少 `:lines:` 参数里的噪点。最终返回 `FormattedExample{compressedLineRanges, content, dedent}`。

**示例落盘 `writeExampleToDiskAndAppendToAppendix`**（[Emitter.cpp:177-229](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/Emitter.cpp#L177-L229)）：若未设 `examplesDirectory` 直接返回（[Emitter.cpp:181-183](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/Emitter.cpp#L181-L183)）；否则创建目录、把完整示例内容写入 `<dir>/example_<name>.mlir`（[Emitter.cpp:206-228](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/Emitter.cpp#L206-L228)），并在 `appendixFile` 里追加一个 `.. _example_<name>:` 锚点、一个 `~` 下划线的小标题、以及一个**不带 `:lines:`** 的 `literalinclude`（即附录里展示完整文件）。

**内联引用 `emitLiteralInclude`**（[Emitter.cpp:153-175](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/Emitter.cpp#L153-L175)）：写出 `.. literalinclude:: /_spec_gen/examples/example_<name>.mlir`，并附加 `:lines: a-b,c-d`（仅脚手架之外的行）、`:language: mlir`、可选 `:dedent: n`。

二者由 `emitExample`（[Emitter.cpp:231-247](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/Emitter.cpp#L231-L247)）串联：先落盘+追加附录，再内联引用，最后补一句 `See :ref:\`example_<name>\` for the full example listing.` 指向附录里的完整清单。

#### 4.4.4 代码实践

**实践目标**：直接驱动 `cuda-tile-tblgen` 生成单操作规范，并对比「带 `--examples-directory`」与「不带」两种输出的差异；再尝试给某操作补一条 `mlirExamples`，观察落盘文件的变化。

**操作步骤**：

1. 直接调用工具生成单操作规范（需要带上 MLIR 的 TableGen include 路径，路径以本地 build 为准）：
   ```bash
   # 路径示例，实际 -I 以你的 LLVM/MLIR 源码或安装位置为准
   ./build/bin/cuda-tile-tblgen --gen-op-spec \
       --op-include-regex="cuda_tile.addf" \
       -I<path-to-mlir-include> \
       include/cuda_tile/Dialect/CudaTile/IR/Ops.td \
       -o /tmp/addf_spec.rst
   cat /tmp/addf_spec.rst
   ```
2. 再生成一次，显式带 `--examples-directory`：
   ```bash
   mkdir -p /tmp/spec-examples
   ./build/bin/cuda-tile-tblgen --gen-op-spec \
       --op-include-regex="cuda_tile.assert" \
       --examples-directory /tmp/spec-examples \
       -I<path-to-mlir-include> \
       include/cuda_tile/Dialect/CudaTile/IR/Ops.td \
       -o /tmp/assert_spec.rst
   ls /tmp/spec-examples/
   ```
3. 打开 `/tmp/spec-examples/` 下 `assert` 对应的 `example_*.mlir`，对比它与 `/tmp/assert_spec.rst` 中 `literalinclude` 的 `:lines:` 范围：文件里能看到 `# cuda_tile.module`、`# entry` 等脚手架行，但 `:lines:` 把它们排除在展示之外。
4. **（可选，在本地副本上实验）**：拷贝 `Ops.td` 到一个临时分支，给一个目前没有 `mlirExamples` 的操作补一条，例如仿照 [Ops.td:231-237](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L231-L237) 的写法加入 `let mlirExamples = [[{ ... }]];`，重新生成，观察：该操作的 `ops.rst` 片段多出 Examples 段、磁盘上多出一个 `example_*.mlir` 文件、`examples_appendix.rst` 多出一节。注意此步会改动源码 `.td`，请仅在本地实验分支进行，勿提交。

**需要观察的现象**：

- 步骤 1（不带 `--examples-directory`）：`addf_spec.rst` 里 Examples 段直接内联代码、无 `literalinclude`、无附录文件生成；
- 步骤 2/3（带）：示例被写成独立 `.mlir` 文件，`assert_spec.rst` 里用 `:lines:` 引用且隐藏了 `#` 脚手架行；
- 步骤 4：新增的 `mlirExamples` 使规范与磁盘示例同步出现。

**预期结果**：能解释「同一条 `mlirExamples` 在两种模式下渲染方式的差异」，并能定位是 [Emitter.cpp:181-183](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/Emitter.cpp#L181-L183) 的早返回决定了「不落盘」分支。若无法本地运行工具，标注「待本地验证」，可改为静态阅读 `processExample` 与 `emitExample` 的源码理解机制。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `ops.rst` 里某操作的示例只显示 3 行，而对应的 `example_*.mlir` 文件却有 5 行？

**参考答案**：因为示例源串里有 2 行以 `#` 开头的脚手架（如 `# entry @example(...) {` 和 `# }`）。`processExample` 把它们保留在落盘内容里（文件 5 行），但不计入 `lineRanges`（[SpecGen.cpp:121-129](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/SpecGen.cpp#L121-L129)），所以内联 `literalinclude` 的 `:lines:` 只选中那 3 行非脚手架代码。

**练习 2**：`SpecEmitter` 构造函数为什么一定会在 `examplesDirectory` 未传时出问题？项目又是如何规避的？

**参考答案**：构造函数 [Emitter.cpp:149-150](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/Emitter.cpp#L149-L150) 无条件调用 `examplesAppendixFile(examplesDirectory)`，后者 [Emitter.cpp:142-144](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/Emitter.cpp#L142-L144) 对空 optional 解引用 `.value()`，是未定义行为。项目靠 CMake 始终传入 `-examples-directory`（[CMakeLists.txt:78](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/CMakeLists.txt#L78)）来规避；这也是为什么不传该选项直接跑工具时需要小心（实践中 `generateSpec` 仍被调用、`SpecEmitter` 仍被构造，因此手动调用工具时建议总是带上 `--examples-directory`）。

---

## 5. 综合实践

把本讲四个最小模块串起来，完成一次「端到端规范追踪」任务：

**任务**：选取 `Ops.td` 中的 `alloca` 操作（[Ops.td:258-294](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L258-L294)），画出从 `.td` 注解到 `ops.rst` 段落的完整数据流图，并标注每一步对应的源码位置。

**要求**：

1. 在 `.td` 侧，列出 `alloca` 用到的全部本讲相关注解：它属于哪个分组（通过哪个子基类）？它的 `arguments` 里三个参数各自的 `specDesc` 与 `sinceVersion` 是什么（经 `CudaTileArgMetadata`）？它有没有 `mlirExamples`（[Ops.td:283-291](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L283-L291)）？有没有 `descriptionTables`？
2. 在中间层，说明 `OperationSignature` 构造函数如何把上述参数读成 `OperationParameter`，`getMLIRExamples()` 如何取出示例，`getCudaTileSpecGroup()` 如何得到分组名。
3. 在调度层，说明 `splitBySections` 把 `alloca` 放进哪个分组桶、该桶在 `cudaTileSections[]` 里的排序分数是多少。
4. 在渲染层，说明 `emitOpDoc` 为 `alloca` 输出哪几段、`processExample` 如何处理它的示例（哪些行被 `:lines:` 隐藏）、示例落到磁盘的哪个文件。
5. 生成规范（按 4.1.4 或 4.4.4 的命令），在 `ops.rst`（或单操作片段）中逐一印证你的推断。

**交付物**：一张数据流图（文字版即可）+ 一份对照清单（左列 `.td` 注解，中列源码函数与行号，右列 `ops.rst` 中的对应段落）。若未本地构建，对「生成规范印证」一栏标注「待本地验证」，其余栏应能纯靠静态阅读完成。

## 6. 本讲小结

- `cuda-tile-tblgen` 通过 `GenRegistration("gen-op-spec", ...)` 把规范生成挂进工具，CMake 用 `tablegen(CUDA_TILE ops.rst -gen-op-spec -examples-directory ...)` 触发，产物落到源码树的 `spec/` 目录，挂在 `mlir-doc` 目标下。
- `CudaTileOp` 是 `Operator` 与渲染器之间的「元数据中间层」，把 `mlirExamples` / `descriptionTables` / `metadata.cudaTileSpecGroup` / `CudaTileArgMetadata` / `OnlyVariants` 等 `.td` 注解抽成易消费的 C++ 结构体，并用构建期断言强制「每个参数必有描述」。
- `generateSpec` 先用 `splitBySections` 按**硬编码的 `cudaTileSections[]` 数组**对操作分组排序（顺序与 `.td` 书写顺序无关、跳过 Testing），再对每个操作用 `emitOpDoc` 按「标题→摘要→签名→描述→属性→表格→约束→示例」的固定骨架渲染。
- `Emitter` 用「值类型 + `operator<<`」（`Header`/`Table`/`Badge`/`Code`）封装 RST 排版，标题级别从 2 开始以让位给外层文档；示例经 `processExample` 区分 `#` 脚手架行后，写成独立 `.mlir` 文件并用 `literalinclude` 的 `:lines:` 选择性引用。
- 贯穿全讲的是**单一数据源**思想：同一份 `.td` 既驱动 C++ 代码生成（u8-l1 的字节码后端）、又驱动人类可读规范（本讲），新增操作只需写一次定义，规范即自动跟进。

## 7. 下一步学习建议

- **回到字节码代码生成**：对照 u8-l1，体会「同一份 `.td`、不同后端」的代码生成范式——`BytecodeGen` 产出机器消费的 `.inc`，`SpecGen` 产出人类消费的 `.rst`，二者共享 `CudaTileOp` 中间层的设计动机。
- **阅读真实规范产物**：若已本地构建，通读 `spec/ops.rst`，挑选一个复杂操作（如带 `descriptionTables` 的 `addf` 或 `mmaf`），验证本讲讲的每一条渲染规则。
- **进入优化器与 Pass 世界**：规范描述了「静态语义」，下一单元（u9）将进入「动态变换」，从 [FuseFMA](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/Transforms/FuseFMA.cpp) 等 Pass 看 IR 如何被改写；理解规范有助于判断「变换是否保持了语义」。
- **扩展练习**：尝试为本项目新增一个最小的自定义 TableGen 后端（仅打印操作名清单），复用 `GenRegistration` 与 `getRequestedOpDefinitions`，加深对「后端即回调」模型的理解。
