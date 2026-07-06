# CodeGen：从 Quake 到 QIR/LLVM（含输出记录解析）

## 1. 本讲目标

本讲是「编译器」单元的最后一讲，负责把读者从「Quake/CC 中间表示」一路带到「可被模拟器或硬件执行的最终代码」，并补上整条链路里最容易被忽视、却又最贴近运行时正确性的一环——**输出记录（output record）的生成与解析**。

学完本讲，你应当能够：

- 说清 QIR 是什么、CUDA-Q 的三种 QIR profile（`qir-full` / `qir-base` / `qir-adaptive`）各自的特点，以及 lowering 故意「拖到最后」的原因。
- 在源码里指出 `ConvertToQIR`、`QuakeToLLVM`、`QuakeToExecMgr`、`ReturnToOutputLog` 这几个 Pass 的职责边界，并能解释一条 `quake.h` 是如何变成 `__quantum__qis__h__body(...)` 调用的。
- 描述「宿主（host）↔ 设备（device）」的衔接：内核的返回值如何被 `ReturnToOutputLog` 改写成 `__quantum__rt__*_record_output` 调用，再由 `NVQIR` 运行时拼成文本日志。
- 读懂 `RecordLogParser` 的记录帧格式，理解 2026 年 7 月那次「输出记录解析加固」（PR #4832）做了哪些健壮性检查：帧校验、schema 元数校验、整数/浮点完整 token 校验、对非有限浮点（`inf`/`nan`）的大小写无关支持，以及解析失败时抛 `runtime_error` 的错误路径。

本讲依赖 u4-l6（优化 Pass 流水线）。在阅读本讲前，你应当已经知道 `AggressiveInlining`、`LambdaLifting`、`LoopUnroll` 这些变换把内核「拍平」成什么样，因为 CodeGen 阶段接收的正是这些 Pass 之后的产物。

## 2. 前置知识

本讲会用到下面这些概念，如果你还不熟悉，可以先回看相关讲义或简要说明：

- **MLIR 与方言**：MLIR 用「方言（Dialect）+ 操作（Operation）」来分层表示程序，并靠「Pass」做变换、靠「lowering」把高层方言逐步降到低层。详见 u4-l1。
- **Quake / CC 方言**：Quake 是 CUDA-Q 的量子方言（门、测量、apply），CC 是经典计算方言（循环、数组、结构体）。详见 u4-l2。
- **QIR（Quantum Intermediate Representation）**：一个建立在 LLVM IR 之上的开放量子 IR 规范。它规定了一组以 `__quantum__` 为前缀的函数名约定，例如 `__quantum__qis__h__body` 表示「作用 H 门」，`__quantum__rt__result_record_output` 表示「输出一个测量结果」。任何声称兼容 QIR 的后端，只要实现这些函数即可。
- **`__qpu__` 内核**：用 `__qpu__` 标注、最终会被翻译成设备代码的函数。详见 u1-l4 / u4-l4。
- **链接期决定后端**：源码里不写死模拟器，具体后端在链接时由注册宏选择。详见 u1-l3 / u6-l1。

> 名词速查：本讲里反复出现的「**lowering（降低）**」= 把一种较高层的 IR 翻译成更接近底层的 IR；「**profile**」= QIR 规范针对不同硬件能力的子集约定；「**shot**」= 内核的一次完整执行。

## 3. 本讲源码地图

本讲涉及的文件集中在 `cudaq/lib/Optimizer/CodeGen/`（编译器后端）和 `runtime/common/`、`runtime/nvqir/`（运行时）。它们按职责分成「生成 QIR」与「解析输出记录」两组：

| 文件 | 作用 | 归属 |
|------|------|------|
| [cudaq/lib/Optimizer/CodeGen/Pipelines.cpp](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/cudaq/lib/Optimizer/CodeGen/Pipelines.cpp) | 编排整个 CodeGen 流水线：选择 profile、挂上 QIR 转换、挂上 `ReturnToOutputLog` | 编译器 |
| [cudaq/lib/Optimizer/CodeGen/ConvertToQIR.cpp](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/cudaq/lib/Optimizer/CodeGen/ConvertToQIR.cpp) | Quake → QIR（v0.1 全量）的总驱动 Pass | 编译器 |
| [cudaq/lib/Optimizer/CodeGen/QuakeToLLVM.cpp](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/cudaq/lib/Optimizer/CodeGen/QuakeToLLVM.cpp) | 真正的 Quake 操作 → LLVM IR 转换模式（门、测量、分配……） | 编译器 |
| [cudaq/lib/Optimizer/CodeGen/QuakeToExecMgr.cpp](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/cudaq/lib/Optimizer/CodeGen/QuakeToExecMgr.cpp) | 「库模式」路径：把 Quake 降到对 ExecutionManager（`CudaqEM*`）的调用 | 编译器 |
| [cudaq/lib/Optimizer/CodeGen/ReturnToOutputLog.cpp](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/cudaq/lib/Optimizer/CodeGen/ReturnToOutputLog.cpp) | 把内核返回值改写成 `__quantum__rt__*_record_output` 调用，并生成类型标签 | 编译器 |
| [runtime/nvqir/NVQIR.cpp](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/runtime/nvqir/NVQIR.cpp) | 运行时：实现 `record_output` 系列，把记录拼成文本写入 `outputLog` | 运行时 |
| [runtime/cudaq/algorithms/run.cpp](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/runtime/cudaq/algorithms/run.cpp) | 宿主侧：清空日志 → 跑内核 → 把日志交给解析器 → 拷出二进制缓冲区 | 运行时 |
| [runtime/common/RecordLogParser.h](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/runtime/common/RecordLogParser.h) | 输出记录解析器：文本日志 → C++ 二进制结构（本讲加固重点） | 运行时 |
| [runtime/common/RecordLogParser.cpp](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/runtime/common/RecordLogParser.cpp) | 解析器实现（本讲加固重点） | 运行时 |
| [cudaq/include/cudaq/Optimizer/CodeGen/QIRAttributeNames.h](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/cudaq/include/cudaq/Optimizer/CodeGen/QIRAttributeNames.h) | QIR 属性名常量（`requiredResults` / `required_num_results` 等） | 编译器 |

理解本讲的关键是抓住一条数据流：

```
内核返回值
  └─[ReturnToOutputLog]─▶ __quantum__rt__*_record_output(...) 调用
                                 └─[NVQIR 运行时]─▶ "OUTPUT\t<type>\t<value>\t<label>\n" 文本
                                                                 └─[RecordLogParser::parse]─▶ 二进制 buffer
```

这条「文本日志」是设备代码（内核里写的 `return ...`）与宿主代码（拿到结构化结果的 C++/Python）之间的**唯一桥梁**。本讲后半部分的所有加固，都是在守护这座桥。

## 4. 核心概念与源码讲解

### 4.1 QIR 规范与 CUDA-Q 的 Lowering 目标

#### 4.1.1 概念说明

QIR 不是某一家公司的私有格式，而是一套**约定**：它规定了一套以 `__quantum__` 开头的函数名和它们的语义。只要后端实现了这些函数（例如「分配一个比特」「作用 H 门」「输出一个测量结果」），它就能执行任何符合 QIR 的量子程序。CUDA-Q 的最终 lowering 目标就是把 Quake/CC MLIR 变成「调用这些 QIR 函数的 LLVM IR」，再由 LLVM 编译成机器码或交给解释器。

QIR 规范针对不同硬件能力划分了几个 **profile（子集）**，CUDA-Q 支持三种，名字直接出现在编译命令里：

- **`qir-full`（也叫 `qir`）**：最自由的全量 QIR。允许动态分配结果（dynamic result management），所以本地模拟器目标用它。
- **`qir-base`**：基线 profile，要求测量可延迟、结果数在编译期固定，适合能力受限的真实硬件。
- **`qir-adaptive`**：自适应 profile，允许中途测量驱动的分支，介于两者之间。

CUDA-Q 的设计原则（见 u4-l1）是 **lowering 故意放在编译流水线的最后**：先把所有能在 Quake 层做的量子优化（化简、内联、分解）做完，最后才把硬件无关的 Quake 一次性降到 QIR。这样能最大化优化空间。

#### 4.1.2 核心流程

一次 CodeGen 流水线的骨架大致是：

1. 收到一个已经被 `AggressiveInlining` 等变换「拍平」过的 Quake/CC 模块。
2. 根据目标选择 profile（`full` / `base` / `adaptive`）。
3. 跑通用清理 Pass（规范化、CSE、循环展开、栈帧预分配等）。
4. 跑 QIR 转换（`ConvertToQIR` / `ConvertToQIRAPI`），把 Quake 操作变成对 `__quantum__qis__*` / `__quantum__rt__*` 的调用。
5. 跑 `ReturnToOutputLog`，把返回值变成 `record_output` 调用。
6. 跑 `CCToLLVM`，把剩余的经典计算（CC 方言）也降到 LLVM。
7. 整个模块此时已是合法的 LLVM/QIR IR，交给 LLVM 后端生成代码或交给运行时解释。

这条骨架的代码就在 `Pipelines.cpp` 里，下一节精读。

#### 4.1.3 源码精读

profile 的选择发生在 [Pipelines.cpp:31-47](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/cudaq/lib/Optimizer/CodeGen/Pipelines.cpp#L31-L47)，`addQIRConversionPipeline` 用一个冒号分隔的字符串（如 `qir-base:...`）分流到不同分支——`qir`/`qir-full` 走 `full`，`qir-base` 会先插入一个 `DelayMeasurements`（基线 profile 要求测量可延迟），`qir-adaptive` 走 `adaptive`。

整个 CodeGen 流水线的总装在 [Pipelines.cpp:97-121](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/cudaq/lib/Optimizer/CodeGen/Pipelines.cpp#L97-L121) 的 `createTargetCodegenPipeline`：先跑 `createCommonTargetCodegenPipeline`（通用清理），再跑 QIR 转换（`addQIRConversionPipeline`），最后三步是本讲的重点之一：

```cpp
::addQIRConversionPipeline(pm, options.target);
cudaq::opt::addLowerToCFG(pm);
cudaq::opt::ReturnToOutputLogOptions opts;
// Only allow dynamic results with full QIR (local simulator targets).
auto tgt = StringRef(options.target).split(':').first;
opts.allowDynamicResult = tgt == "qir" || tgt == "qir-full";
pm.addPass(cudaq::opt::createReturnToOutputLog(opts));
pm.addPass(createConvertMathToFuncs());
pm.addPass(createSymbolDCEPass());
pm.addPass(cudaq::opt::createCCToLLVM());
```

注意 [Pipelines.cpp:115-116](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/cudaq/lib/Optimizer/CodeGen/Pipelines.cpp#L115-L116) 那行注释和判断：**只有全量 QIR（本地模拟器目标）才允许「动态结果」**。这是因为在基线/自适应 profile 下，硬件要求结果数量在编译期固定；而本地模拟器可以返回任意长度的向量，需要一组「span」版本（`*_span_record_output`）的输出函数。这个开关会一路传到 `ReturnToOutputLog`，决定动态大小向量能否被输出。

profile 相关的属性名常量集中在 [QIRAttributeNames.h:34-63](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/cudaq/include/cudaq/Optimizer/CodeGen/QIRAttributeNames.h#L34-L63)：QIR 0.1 用 `requiredResults`，QIR 1.0 用 `required_num_results`。这两个名字后面会在「输出记录解析」里作为 `METADATA` 行再次出现——解析器要把它们映射成统一的内部键 `required_results`。

#### 4.1.4 代码实践

**实践目标**：亲眼看到 profile 是如何被选中的，以及它如何改变最终 IR。

**操作步骤**：

1. 准备一个最小的 Quake IR 文件 `bell.qke`（可仿照仓库里的 [cudaq/test/Translate/array_record_insert.qke](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/cudaq/test/Translate/array_record_insert.qke) 的内核部分）。
2. 分别用两个 profile 跑 `cudaq-opt`：
   ```bash
   cudaq-opt --convert-to-qir-api=api=full bell.qke > bell_full.ll
   cudaq-opt --convert-to-qir-api=api=base-profile bell.qke > bell_base.ll
   ```
3. 对比两份输出里入口函数的 `passthrough` 属性（`qir_profiles`、`requiredResults` 等）。

**需要观察的现象**：`base-profile` 版本里会出现 `DelayMeasurements` 带来的测量位置调整，且属性里 `qir_profiles` 值为 `base_profile`；`full` 版本则没有这些限制。

**预期结果**：两个文件的入口函数 attributes 段不同，证明 profile 在编译期就改变了 IR 形态。

> 若本地尚未构建出 `cudaq-opt`，此步骤为「待本地验证」；可先阅读 `.qke` 文件里的 `// CHECK` 行理解预期 IR。

#### 4.1.5 小练习与答案

**练习 1**：为什么基线 profile 需要一个专门的 `DelayMeasurements` Pass，而全量 QIR 不需要？

> **参考答案**：基线 profile 面向能力受限的真实硬件，要求所有测量尽量靠后、甚至集中到程序末尾，以便硬件批量读取；全量 QIR 面向本地模拟器，测量位置不影响模拟正确性，因此不需要这一步。

**练习 2**：`Pipelines.cpp` 里 `allowDynamicResult` 只在 `qir` / `qir-full` 时为真。如果你在一个基线 profile 目标上让内核返回一个运行时才知道长度的 `std::vector<double>`，会发生什么？

> **参考答案**：`ReturnToOutputLog` 在动态大小向量且 `allowDynamic` 为假时，会直接 `return` 不生成输出（见 4.4.3 的源码），最终该返回值不会被记录，宿主侧读不到这部分结果——这正是 profile 约束在编译期的体现。

---

### 4.2 QuakeToLLVM 与 ConvertToQIR：从量子方言到 QIR 函数

#### 4.2.1 概念说明

把 Quake 操作变成 QIR 调用，靠的是一组「转换模式（Conversion Pattern）」。每个模式负责一类 Quake 操作，告诉 MLIR「遇到 `quake.h`，就用这样的 LLVM `call` 替换它」。这些模式分成两个层次：

- **`ConvertToQIR`（[ConvertToQIR.cpp](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/cudaq/lib/Optimizer/CodeGen/ConvertToQIR.cpp)）**：一个总驱动 Pass。它的头注释写得很直白——[ConvertToQIR.cpp:44-49](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/cudaq/lib/Optimizer/CodeGen/ConvertToQIR.cpp#L44-L49)：「This pass translates Quake to full QIR. This pass *only* supports QIR version 0.1.」它负责设置 `LLVMTypeConverter`、注册所有标准 MLIR lowering（arith→LLVM、complex→LLVM 等）以及 CUDA-Q 自己的 `QuakeToLLVM` / `CCToLLVM` 模式，然后做一次 `applyFullConversion`。
- **`QuakeToLLVM`（[QuakeToLLVM.cpp](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/cudaq/lib/Optimizer/CodeGen/QuakeToLLVM.cpp)）**：真正干活的转换模式集合，约 1500 行，是本节的主角。

#### 4.2.2 核心流程

一条 `quake.h %q` 的 lowering 思路：

1. `ConvertToQIR::runOnOperation` 先做若干 ad-hoc 清理（如把 `cc.const_array` 展开成一串标量 store），再设置类型转换器。
2. 类型转换器把 Quake 类型映射到 LLVM 类型：`!quake.veq<N>` → QIR 的 `%Qubit*` 数组类型；`!quake.ref` → `%Qubit*`；`!quake.measure` → `i1`（见 [ConvertToQIR.cpp:195-213](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/cudaq/lib/Optimizer/CodeGen/ConvertToQIR.cpp#L195-L213) 的 `initializeTypeConversions`）。
3. 转换模式按操作类型分发：`OneTargetRewrite` 处理无参数单比特门（h/x/y/z/s/t），`OneTargetOneParamRewrite` 处理 rx/ry/rz/r1，`MeasureRewrite` 处理测量，等等。
4. 每个模式用「前缀 + 操作名 + 后缀」拼出 QIR 函数名，例如 `__quantum__qis__` + `h` + `__body` = `__quantum__qis__h__body`，然后生成一条 `call`。

#### 4.2.3 源码精读

门到 QIR 函数名的拼装在 `OneTargetRewrite` 里（[QuakeToLLVM.cpp:697-728](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/cudaq/lib/Optimizer/CodeGen/QuakeToLLVM.cpp#L697-L728)）。关键三行：

```cpp
std::string qirQisPrefix{cudaq::opt::QIRQISPrefix};        // "__quantum__qis__"
std::string instName = instOp->getName().stripDialect().str(); // "h"
...
auto qirFunctionName =
    qirQisPrefix + instName + (instOp.getIsAdj() ? "__adj" : ""); // "__quantum__qis__h__body" 或 "...__h__adj"
```

这段中文说明：它取 Quake 操作名（去掉方言前缀），加上固定的 `__quantum__qis__` 前缀；如果操作是伴随（adjoint），再加 `__adj` 后缀，否则 QIR 约定单比特门用 `__body`。于是 `quake.h` → `__quantum__qis__h__body`，`quake.s` 的伴随 → `__quantum__qis__s__adj`。若有控制位，则走 [QuakeToLLVM.cpp:620-694](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/cudaq/lib/Optimizer/CodeGen/QuakeToLLVM.cpp#L620-L694) 的多控路径，改用 `invokeWithControlBits` / `invokeWithControlRegisterOrBits` 运行时函数。

测量 lowering（[QuakeToLLVM.cpp:1089-1133](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/cudaq/lib/Optimizer/CodeGen/QuakeToLLVM.cpp#L1089-L1133)）值得专门看，因为它涉及「寄存器命名」——这正好和后面输出记录里的 `label` 衔接。如果 `mz` 带了显式寄存器名，函数名加上 `__to__register` 后缀并传入名字；如果没有，就用一个递增计数器 `measureCounter` 造一个零填充的名字 `r00000`、`r00001`……（[QuakeToLLVM.cpp:1118-1132](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/cudaq/lib/Optimizer/CodeGen/QuakeToLLVM.cpp#L1118-L1132)）。零填充的目的是让顺序测量的名字按字典序排列，便于后端聚合。

全部模式的注册清单在文件末尾的 `populateQuakeToLLVMPatterns`（[QuakeToLLVM.cpp:1447-1473](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/cudaq/lib/Optimizer/CodeGen/QuakeToLLVM.cpp#L1447-L1473)）：你能看到 H/X/Y/Z/S/T 走 `OneTargetRewrite`，Rx/Ry/Rz/R1 走 `OneTargetOneParamRewrite`，PhasedRx/U2 走 `OneTargetTwoParamRewrite`，U3 走 `OneTargetThreeParamRewrite`，Swap 走 `TwoTargetRewrite`，测量走 `MeasureRewrite<MzOp>`。这是一个典型的「按目标数 × 参数数」分类的模板化设计（u4-l2 提到过 Quake 门 TableGen 的同类分组）。

> 注意：`mx`/`my` 不在这个清单里，因为它们在更早的 `populateQuakeToCCPrepPatterns`（`MxToMzRewrite`/`MyToMzRewrite`）里已经被改写成 `H;MZ` 或 `S;H;MZ` 了，到这里只剩 `mz`。

#### 4.2.4 代码实践

**实践目标**：追踪一条 `x(q)` 调用，确认它最终生成的 QIR 函数名。

**操作步骤**：

1. 写一个最小内核 `flip.cpp`：
   ```cpp
   #include <cudaq.h>
   struct Q {
     __qpu__ void operator()() {
       cudaq::qubit q;
       x(q);
       mz(q);
     }
   };
   ```
2. 用 `nvq++ -emit-llvm`（或等价的 `--dry-run`/verbose 模式）查看 lowering 后的 IR；若不便，直接对照仓库测试 [cudaq/test/Translate/array_record_insert.qke](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/cudaq/test/Translate/array_record_insert.qke#L76-L78) 的 `// CHECK` 行。
3. 在 `QuakeToLLVM.cpp` 里用搜索定位 `OneTargetRewrite`，亲手验证 `x` 是如何拼成 `__quantum__qis__x__body` 的。

**需要观察的现象**：生成的 IR 里出现 `call @__quantum__qis__x__body(%Qubit*)` 与 `call @__quantum__qis__mz__body(...)`。

**预期结果**：函数名与 4.2.3 的拼接规则完全一致。

> 若没有构建环境，此为「源码阅读型实践」：重点是建立「Quake 操作名 → QIR 函数名」的确定性映射直觉。

#### 4.2.5 小练习与答案

**练习 1**：`quake.x` 带一个控制位（即 CNOT）会被 lower 成什么？

> **参考答案**：因为存在控制位，`OneTargetRewrite` 不会直接拼 `__body`，而是转入 `ConvertOpWithControls` 的多控路径，生成对 `invokeWithControlBits` 的调用，把目标门函数指针和控制比特传给它（见 [QuakeToLLVM.cpp:642-650](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/cudaq/lib/Optimizer/CodeGen/QuakeToLLVM.cpp#L642-L650)）。

**练习 2**：为什么测量计数器要做成「零填充 5 位」的 `r%05d`？

> **参考答案**：为了让按字典序排序时，顺序测量的寄存器名与测量先后顺序一致（`r00000 < r00001 < ...`），方便后端在不知道具体名字含义的情况下按序聚合结果。

---

### 4.3 设备代码与执行管理器衔接：QuakeToExecMgr（库模式）

#### 4.3.1 概念说明

CUDA-Q 其实有**两条**把 Quake 降到「可执行」的路径，初学者常把它们混淆：

- **QIR 路径**（上一节）：把 Quake 降到对 `__quantum__qis__*` / `__quantum__rt__*` 的 LLVM 调用，最终是「一段会被 LLVM 编译/链接的代码」。本讲的主角。
- **库模式 / ExecutionManager 路径**（本节）：把 Quake 降到对一组 `CudaqEM*`（Cudaq Execution Manager）运行时函数的 CC 方言调用，由运行时逐条解释执行。`QuakeToExecMgr.cpp` 干的就是这件事。

为什么要分两条？因为有些场景（如 JIT、远程、动态内核）更适合「把线路当成一组对运行时的指令」来解释，而不是生成一整段机器码。本节简要交代这条路径，让你在源码里遇到 `CudaqEMApply` 时不会迷惑。注意：`QuakeToExecMgr` 降到的是 **CC 方言**（不是 LLVM），后续再由 `CCToLLVM` 收尾。

#### 4.3.2 核心流程

`QuakeToExecMgr` 的统一策略在文件头注释里说得很清楚（[QuakeToExecMgr.cpp:95-100](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/cudaq/lib/Optimizer/CodeGen/QuakeToExecMgr.cpp#L95-L100)）：把 Quake 的 `ref`（单比特）和 `veq`（比特数组）统一表示成一个「span」结构（`{指针, 长度}`），前者是长度为 1 的 span。这样所有量子操作都变成对 span 的运行时调用：

1. `quake.alloca` → 调用 `CudaqEMAllocate` 申请比特，包装成 span。
2. `quake.h/x/...` → 调用 `CudaqEMApply(操作名, 参数数, 参数数组, 控制span, 目标span, 是否伴随)`。
3. `quake.mz` → 调用 `CudaqEMMeasure(目标span, 寄存器名)`，返回 `i32`。
4. `quake.dealloc` → 调用 `CudaqEMReturn`。

#### 4.3.3 源码精读

通用门 lowering 的模板 `GenericRewrite` 在 [QuakeToExecMgr.cpp:306-360](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/cudaq/lib/Optimizer/CodeGen/QuakeToExecMgr.cpp#L306-L360)，它把门名做成全局字符串、把参数打包成 `f64` 数组、把控制/目标打包成 span，最终发出一条 `CudaqEMApply` 调用：

```cpp
rewriter.template replaceOpWithNewOp<func::CallOp>(
    qop, mlir::TypeRange{}, cudaq::opt::CudaqEMApply,
    ValueRange{opString, numParams, params, controls, targets, isAdj});
```

这段中文说明：库模式下「门」不是被编译成具体的 QIR 函数，而是被记录成「一个名字字符串 + 参数 + 比特 span」，交给运行时的 ExecutionManager 解释——这与 QIR 路径「每个门一个固定函数」形成鲜明对比。

测量 lowering 见 [QuakeToExecMgr.cpp:372-415](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/cudaq/lib/Optimizer/CodeGen/QuakeToExecMgr.cpp#L372-L415)：如果 `mz` 没有显式名字，就用测量源码位置 hash 出一个 `rXXXX` 名字（和 QIR 路径的「计数器」不同，这里用 hash），然后调用 `CudaqEMMeasure`。

完整的模式注册在 [QuakeToExecMgr.cpp:467-482](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/cudaq/lib/Optimizer/CodeGen/QuakeToExecMgr.cpp#L467-L482) 的 `populateQuakeToCCPatterns`。

#### 4.3.4 代码实践

**实践目标**：理解两条路径在 IR 层面的差别。

**操作步骤**：

1. 阅读本节引用的 `GenericRewrite` 与上一节的 `OneTargetRewrite`，在纸上各写出一个 `quake.h` 会被替换成的伪 IR。
2. 对比：QIR 路径产出 `call @__quantum__qis__h__body(%Qubit*)`；库模式产出 `call @CudaqEMApply("h", 0, null, ctrlspan, tgtspan, 0)`。

**需要观察的现象**：两条路径对「门」的表示完全不同——一个是「函数」，一个是「数据（名字字符串）」。

**预期结果**：你能向别人解释清楚 `CudaqEMApply` 和 `__quantum__qis__h__body` 分别属于哪条路径、各自适用什么场景。

#### 4.3.5 小练习与答案

**练习 1**：库模式里 `CudaqEMApply` 的第一个参数为什么是「门名字符串」而不是函数指针？

> **参考答案**：因为库模式由运行时解释执行，运行时需要知道「当前要作用什么门」才能分派到模拟器的具体实现；把名字当数据传，运行时就能统一用 `apply(name, ...)` 这一个入口处理所有门，便于动态构造线路（如 `kernel_builder`，见 u7-l5）。

**练习 2**：`QuakeToExecMgr` 降到 CC 方言而不是直接降到 LLVM，这有什么好处？

> **参考答案**：降到 CC 方言后，量子操作与经典计算仍处于同一层抽象，可以复用后续 `CCToLLVM` 的统一经典 lowering，保持「量子/经典混合」处理的简洁，也方便在此层做进一步变换。

---

### 4.4 输出记录的生成：从 `return` 到 `OUTPUT` 文本

#### 4.4.1 概念说明

内核写 `return someValue;` 时，`someValue` 可能是 `bool`、`int`、`double`，也可能是数组或结构体。但设备代码跑完后，宿主代码（C++ 主程序或 Python）要拿到一个**结构化的、类型正确的**结果。CUDA-Q 的做法是：把「返回值」翻译成一串对 `__quantum__rt__*_record_output` 的调用，每个调用把一个值连同它的类型标签「记录」下来；运行时把这些记录拼成一段文本日志；最后由 `RecordLogParser` 把文本还原成二进制结构。

这里有两个关键 Pass/运行时部件：

- **`ReturnToOutputLog`**（[ReturnToOutputLog.cpp](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/cudaq/lib/Optimizer/CodeGen/ReturnToOutputLog.cpp)）：把 `cc::LogOutputOp`（内核返回的「待输出」操作）改写成 `record_output` 调用，并负责生成类型标签（如 `array<i1 x 2>`、`tuple<i32, f64>`）。
- **NVQIR 运行时**（[NVQIR.cpp](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/runtime/nvqir/NVQIR.cpp)）：实现这些 `record_output` 函数，把记录追加到一个字符串 `outputLog` 里。

#### 4.4.2 核心流程

一条 `return std::vector<int>{1,2,3};` 的命运：

1. lowering 前期，`return` 已被转成 `cc::LogOutputOp`（携带返回值）。
2. `ReturnToOutputLog` 识别出返回值是数组类型，发出：
   - 一条 `__quantum__rt__array_record_output(3, "array<i32 x 3>")`（数组头：长度 + 类型标签）；
   - 三条 `__quantum__rt__int_record_output(1, "[0]")`、`...(2, "[1]")`、`...(3, "[2]")`（每个元素 + 下标标签）。
3. 运行时把每条调用写成一行 `OUTPUT\tINT\t1\t[0]\n`……追加到 `outputLog`。
4. `RecordLogParser::parse(outputLog)` 读这些行，重建出一个长度为 3 的 `int` 数组放进二进制缓冲区。

注意「标签」的两层含义：

- **类型标签**（如 `array<i32 x 3>`、`i32`、`f64`）：描述这一项的类型与形状，由 `translateType` 生成。
- **下标标签**（如 `[0]`、`.1`）：对数组/元组的元素，标注它在容器里的位置，允许乱序到达。

#### 4.4.3 源码精读

入口是 `ReturnRewrite::matchAndRewrite`（[ReturnToOutputLog.cpp:34-45](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/cudaq/lib/Optimizer/CodeGen/ReturnToOutputLog.cpp#L34-L45)）：对返回值的每个 operand 调 `genOutputLog`。`genOutputLog` 是一个大 `TypeSwitch`，按类型分支（[ReturnToOutputLog.cpp:47-238](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/cudaq/lib/Optimizer/CodeGen/ReturnToOutputLog.cpp#L47-L238)）：

- 整数（[L52-78](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/cudaq/lib/Optimizer/CodeGen/ReturnToOutputLog.cpp#L52-L78)）：1 位用 `QIRBoolRecordOutput`，其它先符号扩展到 `i64` 再用 `QIRIntegerRecordOutput`，标签是 `i<宽度>`（如 `i32`）。
- 浮点（[L79-93](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/cudaq/lib/Optimizer/CodeGen/ReturnToOutputLog.cpp#L79-L93)）：先转 `double`，用 `QIRDoubleRecordOutput`，标签 `f<宽度>`。
- 结构体（[L94-112](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/cudaq/lib/Optimizer/CodeGen/ReturnToOutputLog.cpp#L94-L112)）：先发 `QIRTupleRecordOutput(成员数, "tuple<...>")`，再对每个成员递归 `genOutputLog`，下标标签 `.0`、`.1`……
- 数组（[L113-130](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/cudaq/lib/Optimizer/CodeGen/ReturnToOutputLog.cpp#L113-L130)）：先发 `QIRArrayRecordOutput(长度, "array<... x N>")`，再对每个元素递归，下标标签 `[0]`、`[1]`……
- 动态大小向量（[L131-231](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/cudaq/lib/Optimizer/CodeGen/ReturnToOutputLog.cpp#L131-L231)）：只有 `allowDynamic` 为真时才用 span 版本（`QIRBoolSpanRecordOutput` 等），否则直接返回不输出——这就是 4.1 提到的 profile 约束。

类型标签的字符串拼装集中在 `translateType`（[ReturnToOutputLog.cpp:240-271](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/cudaq/lib/Optimizer/CodeGen/ReturnToOutputLog.cpp#L240-L271)）：整数 → `i32`，浮点 → `f64`，结构体 → `tuple<i32, f64>`，数组 → `array<i1 x 2>`。这些字符串就是后面 `RecordLogParser` 要解析的「label」。

Pass 本体在 [ReturnToOutputLog.cpp:286-325](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/cudaq/lib/Optimizer/CodeGen/ReturnToOutputLog.cpp#L286-L325)：它先用 `loadIntrinsic` 把 `QIRArrayRecordOutput` 等函数声明加载进模块，若 `allowDynamicResult` 则再加载三个 span 版本（[L306-317](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/cudaq/lib/Optimizer/CodeGen/ReturnToOutputLog.cpp#L306-L317)），然后跑改写。

运行时侧的发射在 NVQIR 里。核心是模板函数 `quantumRTGenericRecordOutput`（[NVQIR.cpp:266-276](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/runtime/nvqir/NVQIR.cpp#L266-L276)）：

```cpp
template <typename VAL>
void quantumRTGenericRecordOutput(const char *type, VAL val, const char *label) {
  auto *circuitSimulator = nvqir::getCircuitSimulatorInternal();
  std::ostringstream ss;
  ss << "OUTPUT\t" << type << "\t" << val << '\t';
  if (label) ss << label;
  ss << '\n';
  circuitSimulator->outputLog += ss.str();
}
```

这段中文说明：它把「类型名 + 数值 + 标签」用制表符 `\t` 拼成一行，追加到模拟器实例的 `outputLog` 字符串。注意 `val` 是用 `<<` 输出的，所以浮点数会走 `std::ostream` 的默认格式——这一点在 4.5 讲「非有限浮点」时很重要，因为 `inf`/`nan` 正是 `ostream` 默认会输出的拼写。一组 `extern "C"` 入口（[NVQIR.cpp:460-476](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/runtime/nvqir/NVQIR.cpp#L460-L476)）按类型分派到它：`__quantum__rt__{bool,int,double,tuple,array}_record_output`。

宿主侧把这些文本收回结构化结果的逻辑在 [run.cpp:54-62](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/runtime/cudaq/algorithms/run.cpp#L54-L62)（`cudaq::detail::runTheKernel`）：先清空 `outputLog`，跑内核，然后 `RecordLogParser parser(layoutInfo); parser.parse(circuitSimulator->outputLog);`，最后把解析器产出的二进制 buffer `memcpy` 出来交给调用方。这就把 4.4（生成）与 4.5（解析）首尾接上了。

> 说明：`record_output` 是 QIR 规范里「把一个结果回报给宿主」的标准函数族。本地模拟器用文本日志实现它；真实硬件/远程模拟器则由对方的服务端生成同样格式的文本（见 `python/tests/utils/mock_qpu/` 与 `runtime/cudaq/platform/default/rest/helpers/qci/`），宿主用同一个 `RecordLogParser` 解析——这也是为什么解析器必须对「外部产生的、可能格式不规范的」文本足够健壮。

#### 4.4.4 代码实践

**实践目标**：看清「类型标签」是如何被生成的。

**操作步骤**：

1. 在 `ReturnToOutputLog.cpp` 的 `translateType`（[L240-271](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/cudaq/lib/Optimizer/CodeGen/ReturnToOutputLog.cpp#L240-L271)）里，针对 `i1`、`f64`、`array<i1 x 2>`、`tuple<i32, f64>` 这四种类型，逐一确认它们分别由哪个分支产生。
2. 对照测试 [array_record_insert.qke](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/cudaq/test/Translate/array_record_insert.qke) 的 `// CHECK` 行（如 [L120-L126](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/cudaq/test/Translate/array_record_insert.qke#L120-L126)），确认数组头 `array<i1 x 4>` 与逐元素 `result_record_output` 的调用顺序。

**需要观察的现象**：数组/元组总是「先发一个头记录（带类型标签），再逐元素发记录（带下标标签）」。

**预期结果**：你能在脑中画出「一个 `vector<int>{1,2,3}` 返回值」对应的若干行 `OUTPUT\t...` 文本。

#### 4.4.5 小练习与答案

**练习 1**：为什么数组记录要「先发头、再发元素」，而不是直接逐个元素输出？

> **参考答案**：头记录（`array<i32 x 3>`）告诉解析器「接下来是一个长度为 3 的 i32 数组」，解析器据此预分配缓冲区；之后元素可以带 `[i]` 下标乱序到达并直接写入对应位置。没有头记录，解析器无法知道容器形状与元素类型。

**练习 2**：`ReturnToOutputLog` 对一个「未知元素类型的动态向量」会怎么做？

> **参考答案**：在 `stdvec` 分支里，若元素类型不被支持，会发出一条 `QISTrap`（[L222-230](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/cudaq/lib/Optimizer/CodeGen/ReturnToOutputLog.cpp#L222-L230)），运行时主动陷入错误——因为这种返回值无法被序列化。

---

### 4.5 输出记录的解析与校验：RecordLogParser（本讲加固重点）

#### 4.5.1 概念说明

`RecordLogParser` 是一座「文本 → 二进制」的桥。它的输入是 4.4 生成的那段 `OUTPUT\t...` 文本（以及若干控制行），输出是一块布局与 C++ 宿主代码二进制兼容的内存缓冲区（直接被 `reinterpret_cast` 成 `int`、`double`、`std::vector` 等）。

因为这段文本**有时来自本地模拟器（可信），有时来自远程服务端或第三方硬件（不可信、可能有 bug、可能格式漂移）**，解析器绝不能假设输入「永远正确」。**这正是 2026 年 7 月的 PR #4832（commit `92794d0975`，"More hardening for the QIR output-record parsing"）要加固的地方**。它做的事可以概括为：

1. **帧校验（framing）**：区分「带 `START`/`END` 框的多 shot 日志」和「不带框的裸 `OUTPUT` 日志」，禁止二者混用，禁止嵌套/未闭合的 shot。
2. **schema 元数校验（arity）**：根据声明的 schema（`LABELED` 需 4 列、`ORDERED` 需 3 列）校验每条 `OUTPUT` 记录的字段数。
3. **完整 token 校验**：整数和浮点都必须是「整串都能解析」的合法值，拒绝 `1junk`、`+-1`、带前导空格的 ` 1`。
4. **非有限浮点支持**：大小写无关地接受 `inf`/`infinity`/`nan` 及其符号——因为这些正是运行时 `ostream` 自己输出的拼写，必须能「往返（round-trip）」。
5. **数值范围校验**：拒绝整数越界（如 `128` 塞进 `i8`）、`size_t` 溢出。

#### 4.5.2 核心流程

记录日志由若干「行」组成，每行用 `\t` 分成若干「列」。行首关键字决定行类型：

| 关键字 | 含义 | 示例 |
|--------|------|------|
| `HEADER` | 声明 schema | `HEADER\tschema_id\tlabeled` |
| `METADATA` | 元数据（如结果数） | `METADATA\trequiredResults\t2` |
| `OUTPUT` | 一条输出记录 | `OUTPUT\tINT\t7\ti32` |
| `START` | 一个 shot 的开始（框模式） | `START` |
| `END` | 一个 shot 的结束，带状态码；0=成功 | `END\t0` |

`OUTPUT` 行的列结构是 `OUTPUT \t <recType> \t <recValue> \t [label]`，其中 `label` 仅 `LABELED` schema 才有。`recType` 取值有 `RESULT`、`BOOL`、`INT`、`DOUBLE`、`ARRAY`、`TUPLE`。

`parse()` 的总体逻辑（加固后）：

1. 按行切分，逐行用 `\t` 切列。
2. 用一组布尔状态 `sawFramedOutput` / `sawUnframedOutput` / `sawOutput` / `processingShot` / `declaredSchema` 跟踪上下文。
3. 遇到 `OUTPUT`：先 `validateOutputRecord` 校验列数与 schema 是否一致；若在 `START`/`END` 框内（`processingShot`），只记录起始下标、不立即处理（要等 `END` 确认该 shot 成功才一并处理）；若在框外，直接 `handleOutput`。
4. 遇到 `END`：用 `parseSize` 解析状态码，**只有状态为 0（成功）才处理该 shot 的数据**，否则丢弃；非零状态被视为「shot 失败」。
5. 循环结束后，若仍处于 `processingShot`，说明 shot 未闭合，抛错。

数值解析的统一入口是两个 `detail::` 自由函数：`parseInteger<T>`（带符号、范围检查）和 `parseSize`（无符号、整串校验）。浮点走 `FloatConverter`，内部用 `std::stof`/`std::stod` 并额外检查「整串消费 + 无前导空白」。

#### 4.5.3 源码精读

**（a）枚举与类型**（[RecordLogParser.h:27-31](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/runtime/common/RecordLogParser.h#L27-L31)）：定义了 `RecordSchemaType{LABELED,ORDERED}`、`RecordType`、`OutputType{RESULT,BOOL,INT,DOUBLE}`、`ContainerType{NONE,ARRAY,TUPLE}`。`LABELED` 表示每条记录带类型/下标标签（4 列），`ORDERED` 表示按顺序、无标签（3 列）。

**（b）两个加固新增的解析函数**（[RecordLogParser.h:35-68](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/runtime/common/RecordLogParser.h#L35-L68)）。`parseInteger<T>` 用 `std::from_chars` 严格解析，并做了三件加固：

```cpp
std::int64_t parsed = 0;
const auto [end, error] =
    std::from_chars(input.data(), input.data() + input.size(), parsed);
if (error != std::errc{} || end != input.data() + input.size() ||  // 整串必须消费完
    parsed < static_cast<std::int64_t>(std::numeric_limits<T>::min()) ||
    parsed > static_cast<std::int64_t>(std::numeric_limits<T>::max()))   // 范围检查
  throw std::runtime_error("Invalid integer value");
```

这段中文说明：`from_chars` 返回的 `end` 必须正好指到串尾（否则像 `1junk` 这种前缀合法但尾部有垃圾的串会被拒），并且解析出的 `int64` 必须落在目标类型 `T`（如 `i8`）的范围内。对前导 `+` 也做了单独处理，但 `+-1`、`++1` 这类仍会被拒。`parseSize` 同理，只是目标是 `size_t`。这两个函数替代了旧版直接用 `std::stoi`/`std::stoul` 的脆弱写法。

**（c）浮点转换器**（[RecordLogParser.h:101-134](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/runtime/common/RecordLogParser.h#L101-L134)）。这是加固最微妙的地方，源码注释解释了「为什么不用 `std::from_chars` 的浮点重载」：

```cpp
// The `<charconv>` float overloads are annotated as introduced in
// macOS 26.0 and are unavailable at CUDA-Q's macOS 13.0 deployment
// target. Use `std::stof`/`std::stod` and enforce the same
// "full-token, no leading whitespace" grammar via an explicit
// length check and an `isspace` prefix guard. `stof`/`stod` accept
// `inf`/`infinity`/`nan` case-insensitively; those non-finite
// spellings are the runtime's own output (see the `ostringstream`
// emitter) and must round-trip.
```

中文说明：由于 CUDA-Q 的 macOS 部署目标（13.0）低于 `<charconv>` 浮点重载可用版本（macOS 26.0），这里改用 `std::stof`/`std::stod`，并通过两道检查来达到与 `from_chars` 相同的严格性：

1. **前导空白守卫**：`std::isspace(value.front())` 为真就抛错（拒绝 ` 1`）。
2. **整串消费检查**：`parsedLength != value.size()` 就抛错（拒绝 `1junk`），其中 `parsedLength` 是 `stof`/`stod` 通过出参返回的「实际消费字符数」。

而 `stof`/`stod` 天生大小写无关地接受 `inf`/`infinity`/`nan`，这恰好和 4.4 里 `ostringstream` 发射的 `inf`/`-INFINITY`/`nan` 等拼写对得上——保证「运行时发什么，解析器就能收什么」。所有异常被统一捕获并转成 `runtime_error("Invalid floating-point value")`（[L127-132](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/runtime/common/RecordLogParser.h#L127-L132)），无论底层是 `invalid_argument` 还是 `out_of_range`。

**（d）记录元数校验函数**（[RecordLogParser.cpp:36-47](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/runtime/common/RecordLogParser.cpp#L36-L47)）：`validateOutputRecord` 要求 `OUTPUT` 行有 3 或 4 列；若已声明 schema，则 `LABELED` 必须恰好 4 列、`ORDERED` 必须恰好 3 列。这一步把旧的「在 `handleOutput` 内部松散检查」提前到了统一入口。

**（e）带状态机的 `parse()` 主循环**（[RecordLogParser.cpp:50-127](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/runtime/common/RecordLogParser.cpp#L50-L127)）。加固新增了大量帧一致性检查：

- `HEADER` 出现在任何 `OUTPUT` 之后 → 抛 `"HEADER record after output"`（[L77-78](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/runtime/common/RecordLogParser.cpp#L77-L78)）。
- `START` 必须恰好 1 列，且不能嵌套、不能在裸输出之后出现（[L83-88](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/runtime/common/RecordLogParser.cpp#L83-L88)）。
- `OUTPUT` 必须先过 `validateOutputRecord`；框内/框外互斥，混用抛 `"Mixed framed and unframed output"`（[L91-102](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/runtime/common/RecordLogParser.cpp#L91-L102)）。
- `END` 必须恰好 2 列、必须有配对的 `START`、状态码用 `parseSize` 解析（[L103-108](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/runtime/common/RecordLogParser.cpp#L103-L108)）。
- 循环结束仍 `processingShot` → 抛 `"Unterminated shot"`（[L125-126](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/runtime/common/RecordLogParser.cpp#L125-L126)）。

**错误处理路径**：所有校验失败都抛 `std::runtime_error`，最终冒泡到 `runTheKernel` 的调用者，表现为一次清晰的运行时异常（而不是旧版里 `std::stoi` 抛出的、语义模糊的 `std::invalid_argument`，或更糟的、静默截断 `1junk` 为 `1`）。这是这次加固在「可观测性」上的核心收益。

**（f）HEADER 与 METADATA 处理**（[RecordLogParser.cpp:129-164](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/runtime/common/RecordLogParser.cpp#L129-L164)）：`handleHeader` 现在把「显式声明的 schema」与「从旧式 labeled 记录推断出的 schema」分开存放（`declaredSchema` vs `schema`），并在二者冲突时抛 `"Conflicting schema declarations"`（[L142-143](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/runtime/common/RecordLogParser.cpp#L142-L143)）。`handleMetadata` 在保存 `requiredResults` 前会先用 `parseSize` 校验它确实是合法无符号整数（[L157](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/runtime/common/RecordLogParser.cpp#L157)），拒绝 `1junk` 这类伪数字。

**（g）回归测试**：这次加固在 [unittests/output_record/RecordParserTester.cpp](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/unittests/output_record/RecordParserTester.cpp) 新增了三个 `CUDAQ_TEST`：`checkFloatingPointValueParsing`（验证 `1e+3`、`2.5e-2`、`-3.5`、`INF`、`-INFINITY`、`+NAN`、`inf`、`-InFiNiTy`、`nan` 都能正确往返）、`checkStrictValueValidation`（验证 `1junk`、`128` 入 `i8`、`-129` 入 `i8`、`+-1`、`1e99999`、` 1`、`size_t` 溢出等都被拒；同时 `END\t00` 前导零仍被接受）、`checkRecordGrammarValidation`（验证各种帧/schema 错误都被拒，合法的 `ordered`/`labeled` 仍被接受）。这些测试是理解「什么算合法记录」的最佳文档。

#### 4.5.4 代码实践

**实践目标**：用解析器亲手喂入「合法」与「非法」记录，观察校验行为。这是本讲的核心实践。

**操作步骤**：

1. 阅读 [RecordLogParser.h:35-134](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/runtime/common/RecordLogParser.h#L35-L134) 与 [RecordLogParser.cpp:36-127](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/runtime/common/RecordLogParser.cpp#L36-L127)，列出帧格式与数值校验规则。
2. 阅读新增的三个测试用例 `checkFloatingPointValueParsing`、`checkStrictValueValidation`、`checkRecordGrammarValidation`（在 [RecordParserTester.cpp](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/unittests/output_record/RecordParserTester.cpp) 中），对每条「非法日志」预测它命中的是哪条 `throw`。
3. 若已构建：在 build 目录运行
   ```bash
   ctest -R RecordParserTester --output-on-failure
   ```
   或单独跑：
   ```bash
   ./unittests/output_record/RecordParserTester --gtest_filter='*StrictValueValidation*:*RecordGrammarValidation*:*FloatingPointValueParsing*'
   ```
4. 若想自己构造用例，可在测试文件里照葫芦画瓢新增一个 `CUDAQ_TEST`，喂入例如 `"OUTPUT\tDOUBLE\tnan\tf64\n"`，断言 `parser.parse(log)` 不抛错且 `getBufferSize()==sizeof(double)`。

**需要观察的现象**：

- 非有限浮点 `INF`/`-INFINITY`/`nan`（含大小写混写）都能解析，且符号位正确（`std::signbit` 符合预期）。
- `1junk`、`128`(入 i8)、` 1`(前导空格)、`+-1`、`1e99999`(溢出) 全部抛 `runtime_error`。
- 帧错误（`END` 无配对 `START`、`START` 嵌套、框内框外混用、未闭合 shot、`HEADER` 在输出之后）全部抛错。

**预期结果**：你能复述「输出记录的帧格式」与「至少 5 类数值/语法校验」，并指出解析失败时异常从哪条 `throw` 冒出、最终如何传到宿主。

> 若本地未构建，第 3、4 步为「待本地验证」；前两步的源码阅读不受构建环境影响，是本实践的主体。

#### 4.5.5 小练习与答案

**练习 1**：旧版 `FloatConverter` 直接 `std::stod(value)`，为什么 `1junk` 在旧版可能被「静默接受为 1」而新版会拒绝？这种静默接受有什么危害？

> **参考答案**：`std::stod("1junk")` 会解析出 `1.0` 并把 `parsedLength` 设为 1，旧版没有检查 `parsedLength` 是否等于整串长度，于是把 `1junk` 当成 `1` 静默接受。危害是：来自远程后端的、本应被发现的格式错误（可能是对方 bug 或传输损坏）会被悄悄当成合法数据，导致宿主拿到错误结果却毫无察觉。新版用 `parsedLength != value.size()` 守卫堵住了这个洞。

**练习 2**：为什么 `END` 的状态码非 0 时要「丢弃该 shot 的数据」而不是抛错？

> **参考答案**：状态码非 0 表示「这次 shot 执行失败」（如硬件报错），这是正常的、可恢复的运行时情况，不是日志格式错误。解析器应当跳过这次失败 shot 的输出，而不是让整个 `parse` 崩溃——这区别于「帧不合法」这种应当抛错的硬错误。

**练习 3**：`HEADER` 声明 `labeled`，但某条 `OUTPUT` 只有 3 列（缺 label），解析器会怎样？

> **参考答案**：`validateOutputRecord` 检测到 `declaredSchema == LABELED` 要求 4 列而实际只有 3 列，抛 `"Unexpected record size for schema"`。这就是 schema 元数校验的作用——让 schema 声明真正具有约束力。

## 5. 综合实践

把本讲四条主线串起来，做一次「端到端追踪」：

**任务**：写一个返回 `std::vector<double>` 的内核，完整追踪它的返回值从 IR 到宿主二进制缓冲区的全过程，并验证加固后的解析器对异常输入的健壮性。

**步骤**：

1. **内核与 IR**：写内核
   ```cpp
   #include <cudaq.h>
   #include <vector>
   struct K {
     __qpu__ std::vector<double> operator()() {
       cudaq::qvector q(2);
       h(q[0]);
       x(q[0], q[1]);
       mz(q);
       return {0.5, -0.25, std::numeric_limits<double>::infinity()};
     }
   };
   ```
   对照 4.2，确认 `h`/`x`/`mz` 各自会被 lower 成哪个 QIR 函数。

2. **返回值改写**：对照 4.4，预测 `ReturnToOutputLog` 会把 `return {...}` 改写成：一条 `array_record_output(3, "array<f64 x 3>")` + 三条 `double_record_output(...)`，下标标签 `[0]/[1]/[2]`。

3. **文本日志**：用 `CUDAQ_LOG_LEVEL=info`（或直接看 [run.cpp](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/runtime/cudaq/algorithms/run.cpp) 的 `CUDAQ_DBG("Parsing log:\n{}", outputLog)` 触发点）打印出 `outputLog`，确认它形如：
   ```
   OUTPUT\tARRAY\t3\tarray<f64 x 3>
   OUTPUT\tDOUBLE\t0.5\t[0]
   OUTPUT\tDOUBLE\t-0.25\t[1]
   OUTPUT\tDOUBLE\tinf\t[2]
   ```
   特别留意第三个元素是 `inf`——这正是 4.5 加固要保证能「往返」的非有限浮点。

4. **解析与往返**：确认宿主最终拿到的 `std::vector<double>` 第三元素满足 `std::isinf(...)` 为真。这一步验证了「运行时 `ostringstream` 发 `inf` → `RecordLogParser` 的 `FloatConverter` 收 `inf`」的往返闭环。

5. **加固验证（选做）**：在 [RecordParserTester.cpp](https://github.com/NVIDIA/cuda-quantum/blob/9a967173787bb85dda5e6b06b09aeef13320dc4a/unittests/output_record/RecordParserTester.cpp) 里仿照 `checkFloatingPointValueParsing` 新增一个用例，喂入一条故意损坏的日志（如把 `0.5` 改成 `0.5x`），断言 `EXPECT_ANY_THROW(parser.parse(log));`，运行 `ctest -R RecordParserTester` 通过。

**预期成果**：你能画出一张完整数据流图——从 `quake.h` 一路到宿主 `vector<double>` 的每个元素，并在图上标出 4.2（QIR 函数名）、4.4（record_output 与标签）、4.5（解析校验）分别发生在哪一段。

## 6. 本讲小结

- CUDA-Q 的 CodeGen 把已经被优化「拍平」的 Quake/CC MLIR 一次性降到 QIR/LLVM，三种 profile（`full`/`base`/`adaptive`）对应不同的硬件能力约束；lowering 故意放在最后以最大化量子优化空间。
- `ConvertToQIR` 是总驱动，`QuakeToLLVM` 是真正的转换模式集合；一条 `quake.h` 经 `OneTargetRewrite` 变成 `call @__quantum__qis__h__body(...)`，函数名由「前缀 + 操作名 + `__body`/`__adj`」拼成。
- `QuakeToExecMgr` 是另一条「库模式」路径，把门降成对 `CudaqEMApply(name, ...)` 的 CC 调用，由运行时解释执行，与 QIR 路径并行存在、适用不同场景。
- 内核返回值经 `ReturnToOutputLog` 改写成 `__quantum__rt__*_record_output` 调用（带类型标签与下标标签），再由 NVQIR 运行时拼成 `OUTPUT\t<type>\t<value>\t<label>\n` 文本日志——这是设备代码与宿主之间的唯一结果桥梁。
- `RecordLogParser` 把这段文本还原成 C++ 二进制缓冲区；2026 年 7 月的 PR #4832 对它做了系统性加固——帧校验、schema 元数校验、整数/浮点完整 token 校验、非有限浮点的大小写无关往返、数值范围校验——所有失败统一抛 `runtime_error`，把过去「静默截断」或「语义模糊异常」变成了清晰可观测的错误。

## 7. 下一步学习建议

- **向后端延伸**：本讲生成的 QIR 最终由模拟器后端实现那些 `__quantum__qis__*` 函数。建议进入单元 6，尤其 u6-l1（`CircuitSimulator` API）与 u6-l2（CPU/GPU 模拟器），看 `__quantum__qis__h__body` 在状态向量后端里到底做了什么。
- **向远程/互操作延伸**：本讲提到远程后端用同一套文本日志格式。建议读 u6-l5（远程 QPU 与 OpenQASM 导出）和 `runtime/cudaq/platform/default/rest/helpers/qci/` 的服务端代码，理解「外部产生的日志」为何必须被严格校验。
- **向测试体系延伸**：本讲反复引用 `unittests/output_record/`。建议读 u8-l1（测试体系），系统了解 `CUDAQ_TEST`、FileCheck、targettests 三类测试的分工与运行方式。
- **动手加深**：尝试在 `RecordLogParser` 里新增一种容器类型（如嵌套数组）的支持，体会 `ContainerMetadata` / `BufferHandler` 的扩展点——这是把本讲知识转化为二次开发能力的最佳练习。
