# cuda-tile-optimize 工具与优化器管线

## 1. 本讲目标

学完本讲后，你应当能够：

1. 说清 `cuda-tile-optimize` 这个独立工具在整条工具链里的位置：它是一个**字节码进、字节码出**的优化器，和 `cuda-tile-opt`（操作 MLIR 文本的 pass 驱动器）、`cuda-tile-translate`（MLIR↔字节码翻译器）三者各司其职。
2. 看懂命令行选项：`--opt-level`/`-O`、`--fuse-fma`、`--before`/`--after`、`--emit-bytecode`、`-o`、`--quiet`、`--verbose`，并能用它们组合出想要的优化与输出。
3. 理解 `TileIROptimizerConfig` / `TileIROptInput` / `TileIROptOutput` 三个配置结构如何同时服务于命令行工具和 C API。
4. 说清 `optimizeTileIR` 的「读入 → 建管线 → 跑管线 → 输出」四阶段流程，以及默认 `-O3` 管线由哪些 pass 组成。
5. 把一段 `.tilebc` 跑过优化器，并验证它确实被改写了。

本讲承接 u9-l1（FuseFMA）与 u9-l2（LoopSplit）讲清的两个具体变换，现在把它们**组装成一条可配置的管线**；同时承接 u7-l1（翻译工具入口）的命令行选项套路。

## 2. 前置知识

在进入源码前，先用三句话建立直觉：

- **Pass（变换 Pass）**：MLIR 里对一个 IR 模块做一次确定变换的程序单元，比如「把所有 `mulf+addf` 融合成 `fma`」。多个 Pass 串成一个 **pipeline（管线）**。
- **OpPassManager**：管理「对哪一类操作跑哪些 Pass」的容器。可以嵌套（nest），例如「先定位到 `entry` 函数，再在函数体里跑一系列 pass」。
- **Pass 的文本语法**：MLIR 允许用一段文本字符串描述一条管线，例如 `"canonicalize,cse"`，再用 `parsePassPipeline` 解析成真实的 Pass 链。本讲的 `--before`/`--after` 就用到了它。

为什么需要一个「字节码进、字节码出」的优化器？因为 CUDA Tile IR 的最终交付物是 `.tilebc`（见 u7-l2）。生产场景里，前端 lowering 出字节码后，往往要在**不回到 MLIR 文本**的前提下做一轮优化，再把优化后的字节码交给 `tileiras`（AoT）或驱动（JIT）跑。`cuda-tile-optimize` 正是为这条「字节码 → 优化 → 字节码」链路准备的独立入口；它内部复用的 `optimizeTileIR` 同时被 C API（见 u10-l1）调用，这样命令行用户和嵌入式集成者拿到的是**同一条优化逻辑**。

> 一句话区分三个名字相近的工具：
> - `cuda-tile-translate`：MLIR 文本 ↔ 字节码 翻译（u7-l1）。
> - `cuda-tile-opt`：操作 **MLIR 文本** 的通用 pass 驱动器，用来手工跑单个 pass、调试（u9-l4）。
> - `cuda-tile-optimize`：**字节码进、字节码出** 的成品优化器，本讲主角。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [tools/cuda-tile-optimize/cuda-tile-optimize.cpp](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-optimize/cuda-tile-optimize.cpp) | 命令行工具入口：定义全部 `cl::opt` 选项，把它们装进 `TileIROptimizerConfig`，调用 `optimizeTileIR`。 |
| [include/cuda_tile/Dialect/CudaTile/Optimizer/CudaTileOptimizer.h](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/Optimizer/CudaTileOptimizer.h) | 公共头文件：声明 `TileIROptimizerOptions` / `TileIROptInput` / `TileIROptOutput` / `TileIROptimizerConfig` 配置结构，以及 `optimizeTileIR` / `optimizeTileIRModule` 两个入口函数。 |
| [lib/Dialect/CudaTile/Optimizer/CudaTileOptimizer.cpp](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/Optimizer/CudaTileOptimizer.cpp) | 核心实现：解析输入（字节码或 MLIR 文本）、构建默认管线、跑管线、按配置输出。 |

辅助但重要的两个点（不在「关键源码」清单里，但讲清流程会引用到）：

- `extractCudaTileModuleOp`（声明于 `include/cuda_tile/Dialect/CudaTile/IR/Ops.h`，实现于 `lib/Dialect/CudaTile/IR/CudaTile.cpp`）：在可能被 `mlir::ModuleOp` 包了一层的情况下，把内层的 `cuda_tile::ModuleOp` 抠出来。
- `lib/CAPI/Dialect/CudaTileOptimizer.cpp`：C API 侧调用 `optimizeTileIRModule`，证明「工具与 C API 共享同一管线」。

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：先看命令行工具本身（4.1），再看它喂进去的配置结构（4.2），最后看配置如何驱动一条真实的优化管线（4.3）。

### 4.1 cuda-tile-optimize main 与 Options

#### 4.1.1 概念说明

`cuda-tile-optimize` 是一个独立的可执行程序，文件头注释把它定位得很清楚：它做的是 **CUDA Tile IR Bytecode -> CUDA Tile IR Bytecode** 变换。也就是说，它的输入和输出都应当能是 `.tilebc`（当然也兼容 MLIR 文本输入与文本输出，方便调试）。

它的全部「用户接口」就是一组 LLVM 命令行选项（`llvm::cl::opt`），集中在一个 `Options` 结构体里。理解这个工具，第一步就是把这组选项分类记牢。

#### 4.1.2 核心流程

工具 `main` 的执行顺序很简洁：

1. 构造 `Options options;`（构造即注册所有 `cl::opt`）。
2. 设好版本打印器，调用 `ParseCommandLineOptions` 解析命令行；失败则返回 1。
3. 检查是否给了输入文件（位置参数），没给则报错并打印帮助。
4. 把选项翻译成一个 `TileIROptimizerConfig cfg`（见 4.2）。
5. 调用 `optimizeTileIR(cfg)`；失败则返回 1，成功返回 0。

整个 `main` **不直接碰 MLIR IR**，它只负责「把命令行翻译成配置」和「调用库函数」。真正干活的是 `optimizeTileIR`（4.3）。这是一种很干净的分层：**工具层薄、库层厚**，便于 C API 复用。

#### 4.1.3 源码精读

先看选项定义。`Options` 结构体用一个 `OptionCategory` 把所有选项归到「TileIR Optimizer Options」一类，便于 `--help` 分组显示：

[tools/cuda-tile-optimize/cuda-tile-optimize.cpp:33-67](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-optimize/cuda-tile-optimize.cpp#L33-L67) —— 定义位置参数 `inputFile`（输入字节码文件）、`outputFile`（`-o` 的本体）、`quiet`（`-q`，不向文件/屏幕产输出）。

接着是两类最关键的选项：**输出形态**与**管线开关**。

输出形态由两个选项组合决定：

[tools/cuda-tile-optimize/cuda-tile-optimize.cpp:68-73](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-optimize/cuda-tile-optimize.cpp#L68-L73) —— `emitBytecode`（`--emit-bytecode`）：为真时输出字节码，否则输出 MLIR 文本。

也就是说，**「输出到文件还是屏幕」和「输出字节码还是文本」是两个独立维度**：到屏幕永远是 MLIR 文本（屏幕只给人看），到文件则由 `--emit-bytecode` 决定是字节码还是文本。

管线相关的四个选项是本讲的另一组重点：

[tools/cuda-tile-optimize/cuda-tile-optimize.cpp:74-121](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-optimize/cuda-tile-optimize.cpp#L74-L121) —— 依次定义：
- `pipelinePre`：长名 `--run-before-default-pipeline`，别名 `--before`，在默认管线**之前**插入用户指定的文本管线；
- `pipelinePost`：长名 `--run-after-default-pipeline`，别名 `--after`，在默认管线**之后**插入；
- `fuseFMA`：`--fuse-fma`，开关 FuseFMA pass（u9-l1）；
- `optLevel`：长名 `--opt-level`，别名 `-O`，默认 3，控制默认管线跑到哪一档。

注意一个容易踩的细节：**这里没有 `--split-threshold` 选项**。LoopSplit 的阈值（见 u9-l2）在工具里固定为结构体默认值 1，工具不暴露它；若想改阈值，只能走 for/entry 上的 `cuda_tile.loop_split` 属性或 C API（4.3 会再提）。

还有两个体验类选项：

[tools/cuda-tile-optimize/cuda-tile-optimize.cpp:122-140](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-optimize/cuda-tile-optimize.cpp#L122-L140) —— `verbose`（`-v`，让优化器打印管线文本与 remark）和 `enableMultithread`（开启 MLIR 多线程跑 pass）。

再看 `main` 的核心：把命令行选项翻译成 `cfg`，并决定输出模式：

[tools/cuda-tile-optimize/cuda-tile-optimize.cpp:186-219](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-optimize/cuda-tile-optimize.cpp#L186-L219) —— 这段是「工具层 → 配置层」的桥梁。注意三点：
1. `cfg.input = TileIROptInput::fromFile(options.inputFile);`：输入恒为「从文件读」。
2. 管线开关用**正逻辑**逐条赋值（注释 `// Pipeline toggles (positive logic now)` 也点明了这一点）：`enableFuseFMA`、`optLevel`、`enableMultithread`、`pipelinePreText`、`pipelinePostText` 直接拷过去。
3. 输出模式的优先级注释写得很明白（见下面 4.1.4 的表格）。

最后是对库函数的调用，整个 `main` 在此收尾：

[tools/cuda-tile-optimize/cuda-tile-optimize.cpp:226-229](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-optimize/cuda-tile-optimize.cpp#L226-L229) —— `if (failed(optimizeTileIR(cfg))) return 1;`。错误诊断已在 `optimizeTileIR` 内部发到 stderr，这里只负责把退出码传出去。

#### 4.1.4 代码实践：阅读选项与输出模式优先级

1. **实践目标**：把 `cuda-tile-optimize` 的输出模式判定逻辑用一张表整理出来，能预测任意选项组合的输出。
2. **操作步骤**：
   - 打开 [tools/cuda-tile-optimize/cuda-tile-optimize.cpp:198-219](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-optimize/cuda-tile-optimize.cpp#L198-L219)，对照下面的表把每条分支填进去。
   - 构建一次带工具的可执行（默认 `CUDA_TILE_ENABLE_TOOLS=ON`，见 u1-l2）。
   - 运行 `cuda-tile-optimize --help`，确认上面列出的选项都在，并观察它们被分到「TileIR Optimizer Options」这一类。
3. **需要观察的现象**：`--help` 输出里 `-O`/`-o`/`-q`/`-v`/`--before`/`--after` 都作为别名（alias）显示，且其长名（aliasopt）是它们指向的本体。
4. **预期结果**：输出模式判定表如下（来自源码 198–219 行的注释与逻辑）：

| 给定条件 | 输出模式 |
| --- | --- |
| 指定了 `outputFile` 且 `--emit-bytecode` | `BytecodeFile`（写字节码到文件） |
| 指定了 `outputFile` 且**未** `--emit-bytecode` | `MlirFile`（写 MLIR 文本到文件） |
| 上述任一 + `--verbose` | 额外「按位或」上 `MlirStdout`（同时打到屏幕） |
| 未指定 `outputFile` 且**未** `--quiet` | `MlirStdout`（默认打到屏幕） |
| 未指定 `outputFile` 且 `--quiet` | `None`（不输出，仅做「优化并校验」） |

注意输出模式是**位掩码**（见 4.2.1），所以「文件 + 屏幕」可以同时存在。

5. 若无法本地构建运行，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：用户运行 `cuda-tile-optimize in.tilebc -o out.tilebc --emit-bytecode -v`，会得到哪些输出？

**答案**：因为指定了 `outputFile=out.tilebc` 且 `--emit-bytecode`，模式为 `BytecodeFile`；又因 `-v`（verbose），会额外「按位或」上 `MlirStdout`。所以最终既把字节码写入 `out.tilebc`，又把优化后的 MLIR 文本打到屏幕（便于人眼对照）。

**练习 2**：`--before` 和 `--run-before-default-pipeline` 是什么关系？为什么用户写 `--before` 时**不能**带 op 锚点（如 `cuda_tile.entry(...)`）？

**答案**：`--before` 是 `--run-before-default-pipeline` 的 `cl::alias`（别名），二者完全等价。不能带锚点是因为（4.3 会讲）这段文本会被 `parsePassPipeline` 解析进一个**已经嵌套在 `cuda_tile::EntryOp` 上的 `OpPassManager`**，锚点已经在嵌套时给出了，再写一遍会解析失败。

### 4.2 TileIROptimizerConfig / Input / Output

#### 4.2.1 概念说明

`cuda-tile-optimize` 的 `main` 不直接调 MLIR 的 PassManager，而是先填一个配置结构 `TileIROptimizerConfig`。这个结构是**工具层与库层之间的契约**，C API（`mlirCudaTileApplyOptimizations`）也通过它（间接）与命令行工具共享同一套优化逻辑。它由三部分组成：

- **`TileIROptimizerOptions opt`**：管「跑什么管线」。包含 `optLevel`、`enableFuseFMA`、`enableMultithread`、`loopSplitThreshold`、`pipelinePreText`、`pipelinePostText`。
- **`TileIROptInput input`**：管「从哪里读」。要么是文件名，要么是内存 buffer。
- **`TileIROptOutput output`**：管「往哪里写、写什么形态」。核心是一个位掩码 `mode`。

这里有一个值得记住的设计：**输出模式是位掩码枚举（bitmask enum）**，可以按位「或」组合。比如「写字节码文件 + 同时把 MLIR 文本打到屏幕」就是 `BytecodeFile | MlirStdout`。这解释了 4.1.4 里「verbose 叠加文件输出」的实现方式。

#### 4.2.2 核心流程

配置的装配与消费链路是：

1. `main`（工具层）把命令行选项逐字段写入 `cfg.opt`、`cfg.input`、`cfg.output`、`cfg.verbose`。
2. `optimizeTileIR(cfg)`（库层）读取 `cfg.input` 加载模块，读取 `cfg.opt` 建管线，读取 `cfg.output` 决定如何输出。
3. C API 路径只构造 `TileIROptimizerOptions`（不经过 Input/Output，因为它已经持有一个内存里的 `moduleOp`），直接调更底层的 `optimizeTileIRModule`。

也就是说，`optimizeTileIR` 是「带 I/O 的完整入口」（命令行用），`optimizeTileIRModule` 是「只跑管线、不做 I/O」的窄入口（C API 复用）。

#### 4.2.3 源码精读

先看输出模式的位掩码定义：

[include/cuda_tile/Dialect/CudaTile/Optimizer/CudaTileOptimizer.h:28-45](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/Optimizer/CudaTileOptimizer.h#L28-L45) —— `TileIROptOutputMode` 共四个可组合位：`None=0`、`BytecodeFile`、`BytecodeMemory`、`MlirFile`、`MlirStdout`。下面的 `LLVM_DECLARE_ENUM_AS_BITMASK` 把它注册为 LLVM 的位掩码枚举，使其支持 `|`、`&` 运算。注意 `BytecodeMemory`（把字节码写进一个 `std::string*`）只服务于 C API/嵌入者，命令行工具不会用到它。

再看管线选项结构：

[include/cuda_tile/Dialect/CudaTile/Optimizer/CudaTileOptimizer.h:49-68](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/Optimizer/CudaTileOptimizer.h#L49-L68) —— `TileIROptimizerOptions` 的六个字段。注意每个字段都有默认值：`enableMultithread=false`、`enableFuseFMA=false`、`optLevel=3`、`loopSplitThreshold=1`、`pipelinePreText=""`、`pipelinePostText=""`。头文件里同时声明了 `registerTileIROptPasses()` 和 `optimizeTileIRModule()`。

输入与输出结构：

[include/cuda_tile/Dialect/CudaTile/Optimizer/CudaTileOptimizer.h:70-106](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/Optimizer/CudaTileOptimizer.h#L70-L106) —— `TileIROptInput` 用 `std::variant<BufferT, FileT>` 表示「要么是内存 buffer（`StringRef`），要么是文件名（`std::string`）」，并提供两个工厂 `fromBuffer` / `fromFile`。`TileIROptOutput` 则把「模式位掩码」与「各模式需要的载体」（文件名、buffer 指针、屏幕 ostream）打包到一起。

最后是聚合三者的总配置：

[include/cuda_tile/Dialect/CudaTile/Optimizer/CudaTileOptimizer.h:108-125](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/Optimizer/CudaTileOptimizer.h#L108-L125) —— `TileIROptimizerConfig` 把 `input`、`output`、`opt`、`verbose` 收拢成一个结构，并由 `optimizeTileIR(TileIROptimizerConfig &cfg)` 消费。

#### 4.2.4 代码实践：阅读 C API 如何复用同一管线

1. **实践目标**：亲眼确认「命令行工具与 C API 跑的是同一条优化逻辑」。
2. **操作步骤**：
   - 打开 [lib/CAPI/Dialect/CudaTileOptimizer.cpp:40-88](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/CAPI/Dialect/CudaTileOptimizer.cpp#L40-L88)。
   - 定位 `toCpp(const mlirCudaTileOptConfig &c)`：它把 C 结构体的字段（`flags`、`optLevel`、`loopSplitThreshold`）翻译成一个 `TileIROptimizerOptions`。
   - 定位 `mlirCudaTileApplyOptimizations`：它调 `extractCudaTileModuleOp` 拿到 cuda_tile 模块，再调 `optimizeTileIRModule(cudaTileModuleOp, toCpp(*config), ...)`。
3. **需要观察的现象**：C API 不走 `optimizeTileIR`（那个带文件 I/O），而是直接调更窄的 `optimizeTileIRModule`，因为它手里已经有内存模块了。
4. **预期结果**：你能画出两条调用路径——命令行 `main → optimizeTileIR → optimizeTileIRModule`，C API `mlirCudaTileApplyOptimizations → optimizeTileIRModule`——它们在 `optimizeTileIRModule` 这一点汇合。这正是「单一管线、两个入口」的设计。
5. 若没有 C API 构建环境，**待本地验证**（但仍可通过阅读源码确认结构）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `TileIROptOutputMode` 要做成位掩码，而 `TileIROptInput` 不用位掩码？

**答案**：输出可以**同时**去多个目的地（文件 + 屏幕、文件 + 内存），是「多选」语义，用位掩码可以 `|` 组合。输入只有一个来源（要么文件、要么内存 buffer），是「二选一」语义，所以用 `std::variant<BufferT, FileT>` 表达互斥即可。

**练习 2**：C API 的 `mlirCudaTileOptConfig` 里有 `loopSplitThreshold`，而 `cuda-tile-optimize` 的命令行选项里没有它。这说明什么？

**答案**：说明「配置结构的字段集合」≠「命令行暴露的选项集合」。工具把不常改的 `loopSplitThreshold` 固定在结构体默认值（1），只把最常用的开关（`-O`/`--fuse-fma`/`--before`/`--after`）做成命令行选项；嵌入者走 C API 时可以细粒度地设阈值。

### 4.3 optimizeTileIR / optimizeTileIRModule

#### 4.3.1 概念说明

有了配置，就该跑管线了。这是本讲的「主菜」。两个函数各管一段：

- **`optimizeTileIR(cfg)`**：带 I/O 的完整入口。负责建 `MLIRContext`、注册方言、校验配置、读入模块、调 `optimizeTileIRModule` 跑管线、按 `cfg.output` 输出。命令行工具只调它。
- **`optimizeTileIRModule(module, opts, verbose)`**：只跑管线、不做任何 I/O。它建一个 `PassManager`，把「默认管线 + 用户前后插入」填进去，然后 `pm.run(module)`。C API 也复用它。

默认管线长什么样？这是本讲最重要的结论之一。在默认 `-O3` 下，管线依次是（条件性包含 FuseFMA）：

1. （仅当 `--fuse-fma`）`FuseFMA`（u9-l1）
2. （`optLevel>=1`）`Canonicalize` + `CSE`
3. （`optLevel>=2`）`LoopInvariantCodeMotion`（LICM，循环不变量外提）
4. （`optLevel>=3`）`LoopSplit`（u9-l2）+ 再来一遍 `Canonicalize`

注意 FuseFMA **不计入 optLevel 分档**，它由独立的 `enableFuseFMA` 开关控制，且总是最先跑。这是一个有意的取舍：FuseFMA 是「非数值保持」变换（u9-l1），必须显式 opt-in，不能因为 `-O3` 就默认开启。

另一个关键设计是 **pipeline 的嵌套锚点**：整条管线被嵌套在 `cuda_tile::EntryOp` 上（用 `pm.nestAny()`）。这意味着所有 pass 都是在「每个 entry 函数体内」跑的，而不是在模块顶层。这也解释了 4.1.5 里 `--before`/`--after` 文本为何不能带 op 锚点——锚点已经在嵌套时给好了。

#### 4.3.2 核心流程

`optimizeTileIR` 的四阶段流程：

```
optimizeTileIR(cfg)
 ├─ 1. 建 MLIRContext（注册 CudaTileDialect；按 enableMultithread 开多线程；
 │       verbose 时挂 remark/warning/error 诊断到 stderr）
 ├─ 2. validateConfig(cfg)      // 校验输入输出配置自洽
 ├─ 3. loadInputModule(cfg)     // 读文件/buffer，按 magic 判定字节码 or MLIR 文本
 │       └─ extractCudaTileModuleOp(...)  // 抠出内层 cuda_tile::ModuleOp
 ├─ 4. registerTileIROptPasses()  // 注册 cuda_tile + mlir::Transforms 的 pass
 ├─ 5. optimizeTileIRModule(module, cfg.opt, cfg.verbose)
 │       └─ buildCudaTileOptimizationPipeline
 │            ├─ parseTextInto(pipelinePreText)   // --before 的文本
 │            ├─ buildDefaultCudaTilePipeline      // 默认 -O 管线
 │            └─ parseTextInto(pipelinePostText)  // --after 的文本
 │       └─ pm.run(module)
 └─ 6. emitOutputs(cfg, parentModule)  // 按 mode 位掩码写字节码/文本到文件/屏幕
```

输入读取里有一个聪明的细节：`loadInputModule` 用 `isTileIRBytecode(ref)` 检查 magic number（见 u7-l2/u7-l3）来**自动判定**输入是字节码还是 MLIR 文本，不需要用户告诉它。这使工具既能吃 `.tilebc`，也能吃 `.mlir`。

#### 4.3.3 源码精读

先看默认管线是怎么按 `optLevel` 分档构建的：

[lib/Dialect/CudaTile/Optimizer/CudaTileOptimizer.cpp:55-79](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/Optimizer/CudaTileOptimizer.cpp#L55-L79) —— `buildDefaultCudaTilePipeline`。逐条对照概念说明里的清单：FuseFMA 独立于 optLevel；`optLevel>=1` 加 Canonicalize+CSE；`optLevel>=2` 加 LICM；`optLevel>=3` 加 LoopSplit（用 `opts.loopSplitThreshold` 构造）+ 再一轮 Canonicalize。注意 LoopSplit 的阈值取自 `opts.loopSplitThreshold`（工具里恒为 1）。

再看「前后插入 + 默认」如何拼成一条管线：

[lib/Dialect/CudaTile/Optimizer/CudaTileOptimizer.cpp:81-98](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/Optimizer/CudaTileOptimizer.cpp#L81-L98) —— `buildCudaTileOptimizationPipeline`。注意 `pm.nestAny()` 把后续所有 pass 嵌套到一个任意 op 的 OpPassManager 里（实际跑在每个 `entry` 上），然后依次 `parseTextInto(pipelinePreText)` → `buildDefaultCudaTilePipeline` → `parseTextInto(pipelinePostText)`。

文本管线解析函数：

[lib/Dialect/CudaTile/Optimizer/CudaTileOptimizer.cpp:40-53](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/Optimizer/CudaTileOptimizer.cpp#L40-L53) —— `parseTextInto`。空串直接成功（不插入任何 pass）；否则 `parsePassPipeline(text, PM)` 把文本（如 `"canonicalize,cse"`）解析进**已嵌套**的 PM。注释明确提醒：因为 PM 已经为 `EntryOp` 嵌套好了，文本里**不要**再带 op 锚点。

输入读取——字节码判定与解析：

[lib/Dialect/CudaTile/Optimizer/CudaTileOptimizer.cpp:104-123](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/Optimizer/CudaTileOptimizer.cpp#L104-L123) —— `parseTileIRBytecode`：先用 `isTileIRBytecode` 验 magic，再 `readBytecode` 反序列化成 `cuda_tile::ModuleOp`，最后**包进一个 `mlir::ModuleOp`** 外壳。这个外壳是关键：后续 pass 与输出都在外层 `mlir::ModuleOp` 上操作，写回时再用 `extractCudaTileModuleOp` 把内层抠出来。

[lib/Dialect/CudaTile/Optimizer/CudaTileOptimizer.cpp:176-227](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/Optimizer/CudaTileOptimizer.cpp#L176-L227) —— `loadInputModule`：文件路径走 `MemoryBuffer::getFile`（`IsText=false`，原始字节读取，保证 magic 判定可靠）；buffer 路径构造非拥有视图；随后按 `isTileIRBytecode` 分流到字节码解析或 MLIR 文本解析（`parseSourceFile<mlir::ModuleOp>`）。

输出阶段：

[lib/Dialect/CudaTile/Optimizer/CudaTileOptimizer.cpp:229-281](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/Optimizer/CudaTileOptimizer.cpp#L229-L281) —— `emitOutputs`。先用位掩码判断要不要写字节码（`BytecodeFile | BytecodeMemory`），要则**只生成一次**字节码到内存，再按需落盘或写进 buffer；再判断要不要写 MLIR 文本（`MlirFile | MlirStdout`），同样**只打印一次**到字符串再分发到文件/屏幕。写字节码时用 `extractCudaTileModuleOp` 把内层模块抠出来，固定写 `BytecodeVersion::kCurrentVersion`（13.3，见 u7-l4）。

两个对外入口函数：

[lib/Dialect/CudaTile/Optimizer/CudaTileOptimizer.cpp:287-312](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/Optimizer/CudaTileOptimizer.cpp#L287-L312) —— `registerTileIROptPasses`（注册 cuda_tile 自有 pass + MLIR 通用 Transforms pass）与 `optimizeTileIRModule`（建 PM、建管线、verbose 时打印管线文本、`pm.run`）。注意 verbose 模式下 `pm.printAsTextualPipeline` 会把最终管线打到一条 remark 里——这是排查「我到底跑了哪些 pass」的最快手段。

[lib/Dialect/CudaTile/Optimizer/CudaTileOptimizer.cpp:320-368](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/Optimizer/CudaTileOptimizer.cpp#L320-L368) —— `optimizeTileIR`。注意它建 `MLIRContext` 时只 insert 了 `CudaTileDialect`（自包含方言，依赖见 u2-l2）；verbose 时挂的诊断 handler 会把 remark/warning/error 全打到 stderr；末尾若 `output.mode == None` 直接成功返回（对应 `--quiet` 场景：只优化、只校验、不输出）。

#### 4.3.4 代码实践：把一段字节码跑过优化器并对比

1. **实践目标**：亲手把一段 `.tilebc` 经 `cuda-tile-optimize` 优化后重新输出，并用 `cuda-tile-translate` 反翻译为 MLIR，肉眼确认优化发生。
2. **操作步骤**：
   - 先准备一段含可优化模式的 MLIR。借鉴 u9-l1，写一个含 `mulf` 紧跟 `addf`（且 `mulf` 单一使用）的内核，存为 `pre.mlir`。
   - 用 `cuda-tile-translate` 把它编译成字节码（命令见 u7-l1，例如 `cuda-tile-translate --mlir-to-cudatilebc pre.mlir -o pre.tilebc`）。**待本地验证**具体参数。
   - 跑优化器，开启 FuseFMA 并输出字节码：

     ```bash
     cuda-tile-optimize pre.tilebc --emit-bytecode -O3 --fuse-fma -o opt.tilebc
     ```

   - 把优化后的字节码反翻译回 MLIR 文本对比：

     ```bash
     cuda-tile-translate --cudatilebc-to-mlir opt.tilebc -o opt.mlir
     ```

   - 用 `diff pre.mlir opt.mlir`（或文本对比）查看差异。
3. **需要观察的现象**：原本分离的 `mulf`/`addf` 应合并为单条 `fma`（u9-l1 的 `MulAddPattern`）。若你的输入里还有「归纳变量与循环不变量比较」的 if，应观察到循环被分裂（u9-l2）。
4. **预期结果**：`opt.mlir` 里出现 `cuda_tile.fma`，且对应的 `mulf`+`addf` 消失；`pre.mlir` 与 `opt.mlir` 的 diff 能清楚看到这一点。若想看优化器实际跑了哪些 pass，加 `-v`，stderr 会打印一条形如 `Pipeline: ...` 的 remark。
5. **进阶**：用 `--before`/`--after` 在默认管线前后各插一个 pass（注意不带锚点），例如：

   ```bash
   cuda-tile-optimize pre.tilebc -O3 --before "canonicalize" --after "cse" -o opt2.tilebc
   ```

   观察 `-v` 打印的管线文本里，`canonicalize` 出现在默认管线之前、`cse` 出现在默认管线之后。**待本地验证**该 pass 名是否被注册（`registerTileIROptPasses` 注册了 `registerTransformsPasses`，故 `canonicalize`/`cse` 等通用 pass 可用）。
6. 若没有可用的预编译工具链，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：在默认 `-O3` 且**未**加 `--fuse-fma` 的情况下，FuseFMA 会不会跑？为什么？

**答案**：不会。`buildDefaultCudaTilePipeline` 里 FuseFMA 由 `if (opts.enableFuseFMA)` 单独控制，与 `optLevel` 无关；`enableFuseFMA` 默认 `false`，只有命令行 `--fuse-fma` 才会置真。原因是 FuseFMA 是「非数值保持」变换（双轮舍入变单轮舍入，位级结果会变），不能默认开启，必须显式 opt-in。

**练习 2**：为什么 `loadInputModule` 能在「不问用户」的情况下同时吃 `.tilebc` 和 `.mlir`？

**答案**：因为它用 `isTileIRBytecode(ref)` 检查 buffer 的 magic number（`{0x7F,'T','i','l','e','I','R',0x00}`，见 u7-l2）。magic 对上就走字节码路径（`readBytecode`），对不上就把 buffer 当 MLIR 文本走 `parseSourceFile`。读取文件时还显式用了 `IsText=false`，避免平台对文本做 CRLF 翻译而破坏 magic 判定。

**练习 3**：`--quiet` 时 `optimizeTileIR` 还会跑优化吗？它的价值是什么？

**答案**：会。`--quiet` 只是把 `output.mode` 设为 `None`，`optimizeTileIR` 仍会校验配置、读入模块、跑完整管线，并在 `output.mode == None` 时跳过 `emitOutputs` 直接成功返回。它的价值是**把优化器当成一个带完整 pass 校验的合法性检查器**：任何 verifier 报错都会以非零退出码体现，但不产生输出文件。

## 5. 综合实践

把本讲的三条主线（命令行选项、配置结构、管线执行）串起来，完成下面这个端到端任务：

**任务**：用 `cuda-tile-optimize` 对一段自己写的内核做一次「FuseFMA + 默认 -O3 + 自定义后置 pass」的优化，并解释每一步在源码里对应哪段代码。

要求：

1. 写一段 `entry` 内核 MLIR，包含一个明显的 `mulf` + `addf` 模式（`mulf` 单一使用、舍入一致），以及一个含 if 分支的 `for` 循环（用于触发 LoopSplit）。参考 u9-l1 与 u9-l2 的测试文件 `test/Transforms/fuse-fma.mlir`、`test/Transforms/loop_split.mlir` 的写法。
2. 把它编译成 `in.tilebc`（用 `cuda-tile-translate --mlir-to-cudatilebc`）。
3. 运行：

   ```bash
   cuda-tile-optimize in.tilebc --emit-bytecode -O3 --fuse-fma --after "canonicalize" -v -o out.tilebc
   ```

4. 把 `out.tilebc` 反翻译成 MLIR，确认：(a) `mulf`+`addf` 变成了 `fma`；(b) 循环被分裂；(c) `-v` 打印的 `Pipeline:` remark 里，默认管线**之后**多了一个 `canonicalize`。
5. 对照源码写一份「命令行选项 → 配置字段 → 管线步骤」的映射表，例如：

   | 命令行 | 配置字段 | 管线效果 | 源码位置 |
   | --- | --- | --- | --- |
   | `--fuse-fma` | `opt.enableFuseFMA` | 最先加 `FuseFMA` | `CudaTileOptimizer.cpp:60-61` |
   | `-O3` | `opt.optLevel` | 跑满四档默认 pass | `CudaTileOptimizer.cpp:63-77` |
   | `--after "canonicalize"` | `opt.pipelinePostText` | 默认管线后再加 canonicalize | `CudaTileOptimizer.cpp:97` |

   （行号请以你本地 HEAD 为准。）

**预期结果**：你能用这一条命令把三个最小模块全部走一遍，并能用源码行号解释每个开关的去向。若任何一步在本地无法运行，明确标注「待本地验证」。

## 6. 本讲小结

- `cuda-tile-optimize` 是「字节码进、字节码出」的独立优化器；它的 `main` 极薄，只把命令行选项翻译成 `TileIROptimizerConfig` 再调 `optimizeTileIR`。
- 输出形态由「文件 vs 屏幕」与「字节码 vs MLIR 文本」两个独立维度决定，`TileIROptOutputMode` 是位掩码，可组合（如文件 + 屏幕）。
- 关键选项：`-O`/`--opt-level`（默认 3，分档决定默认管线深度）、`--fuse-fma`（显式开关 FuseFMA）、`--before`/`--after`（在默认管线前后插入文本管线，**不带锚点**）。
- 默认 `-O3` 管线为（条件性 FuseFMA）→ Canonicalize → CSE → LICM → LoopSplit → Canonicalize，整条嵌套在 `cuda_tile::EntryOp` 上。
- FuseFMA 不随 `-O3` 自动开启，因为它是「非数值保持」变换，必须显式 opt-in；`loopSplitThreshold` 工具不暴露，固定为默认值 1。
- 命令行（`optimizeTileIR`）与 C API（`mlirCudaTileApplyOptimizations`）在 `optimizeTileIRModule` 这一点汇合，共享同一条优化逻辑——「单一管线、两个入口」。

## 7. 下一步学习建议

- 继续本单元：阅读 **u9-l4（调试信息合成与规范化）**，看 `cuda-tile-opt` 如何注册 `canonicalize`/`cse`/`inline` 与 `SynthesizeDebugInfoScopes`，理解本讲默认管线里那些通用 pass 的注册细节与 OpsCanonicalization.td。
- 横向对照：回顾 **u7-l1（翻译工具入口）** 的命令行选项套路（`cl::opt` + 外部存储 + getter），与本讲的 `Options` 结构体对比，体会 CUDA Tile 工具统一的命令行风格。
- 走向集成：学习 **u10-l1（C API 集成接口）**，看 `mlirCudaTileApplyOptimizations` 如何把本讲的 `optimizeTileIRModule` 暴露给第三方 C/C++ 项目，把这条优化管线嵌入你自己的编译流程。
- 深读 pass：若想改默认管线，重点读 `lib/Dialect/CudaTile/Optimizer/CudaTileOptimizer.cpp` 的 `buildDefaultCudaTilePipeline` 与 `buildCudaTileOptimizationPipeline`，以及 `include/cuda_tile/Dialect/CudaTile/Transforms/Passes.td`（u9-l1/u9-l2 已介绍）。
