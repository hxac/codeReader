# 属性系统与优化提示（Optimization Hints）

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `cuda_tile` 方言里「属性（Attribute）」是如何被声明、生成和校验的，理解 `CudaTileAttrDef` 与一组枚举属性（signedness / overflow / rounding / 比较谓词 / 原子模式 / 内存序 / 内存作用域）各自的角色。
- 读懂 `OptimizationHintsAttr` 的「按 SM 架构分桶 + 逐操作给提示」设计，知道哪些提示挂在 `entry`、哪些挂在访存操作上，以及取值边界。
- 理解「不支持的提示默认静默」这一关键设计，以及 `-Wunsupported-hints` / `-Werr-hints` 两个命令行开关如何把静默变成告警、再把告警升级成错误。
- 能够亲手给一段 `entry` 内核附加 `optimization_hints`，并用 `cuda-tile-opt` 的三种运行方式观察行为差异。

本讲承接 u2-l2（方言定义与 `extraClassDeclaration`）与 u2-l3（TableGen 代码生成），是属性层的深入；同时为 u5（内存操作，那里会出现 `latency`/`allow_tma` 提示）和 u6-l3（调试信息属性）打基础。

## 2. 前置知识

- **属性（Attribute）与操作（Operation）的关系**：在 MLIR 里，操作可以有「附属属性」。属性是编译期的常量值（比如一个枚举、一个字典），挂在操作上用来指导编译器，但本身不参与运行时计算。本讲讨论的「优化提示」就是一种属性。
- **TableGen `.td` 与 `.inc`**：参见 u2-l3。方言的属性先用 `.td` 声明，再由 `cuda-tile-tblgen` / `mlir-tblgen` 生成 C++ 胶水（`AttrDefs.h.inc`、`AttrDefs.cpp.inc`），手写的校验逻辑写在 `Attributes.cpp` 里。
- **CTA、CGA、warp、TMA**：GPU 里的执行单元层次。CTA（Cooperative Thread Array，对应一个线程块）是 GPU 调度的基本单位；CGA（线程块集群）把多个 CTA 编成一组，允许它们协同访问显存；warp 是 CTA 内的线程束；TMA（Tensor Memory Accelerator）是 Hopper/Blackwell 及以后架构里专门做批量异步显存搬运的硬件单元。这些是优化提示想要控制的对象——你不必现在就精通它们，只需知道「提示是告诉编译器在某个 SM 架构上，这个内核应该用几个 CTA 组集群、每个 CTA 用几个 worker warp、是否走 TMA 搬数据」。
- **SM 架构名**：如 `sm_80`（Ampere）、`sm_89`（Ada）、`sm_90`（Hopper）、`sm_100`/`sm_103`/`sm_110`（Blackwell）、`sm_120`/`sm_121`。提示是「按架构分别给」的，因为不同架构的硬件能力不同。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [include/cuda_tile/Dialect/CudaTile/IR/AttrDefs.td](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/AttrDefs.td) | 全部属性的 TableGen 声明，含枚举属性族与 `OptimizationHintsAttr` 定义及其 `extraClassDeclaration`。 |
| [include/cuda_tile/Dialect/CudaTile/IR/Dialect.td](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Dialect.td) | 方言骨架，其中 `extraClassDeclaration` 注入了 `warnUnsupportedHints_`/`errorOnHints_` 两个开关及其 getter/setter。 |
| [lib/Dialect/CudaTile/IR/Attributes.cpp](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/Attributes.cpp) | 属性校验的 C++ 实现：提示取值校验、`verifyParamWithContext`、`verifyWithOp`、各 getter 与自定义解析/打印。 |
| [include/cuda_tile/Dialect/CudaTile/IR/SharedVerifiers.h](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/SharedVerifiers.h) | 把「提示校验」封装成可被多个操作复用的 `verifyOptHintsCommon` 模板，是操作 verifier 与属性校验之间的粘合层。 |
| [lib/Bytecode/Common/CommandLineOptions.cpp](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Common/CommandLineOptions.cpp) | 注册 `-Wunsupported-hints` / `-Werr-hints` 两个命令行选项及对应的全局变量。 |
| [tools/cuda-tile-opt/cuda-tile-opt.cpp](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-opt/cuda-tile-opt.cpp) | `cuda-tile-opt` 工具入口，读取命令行开关并通过方言扩展（extension）把它们写进方言对象。 |
| [test/Dialect/CudaTile/opt_hints.mlir](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/opt_hints.mlir) | 提示合法用法的 round-trip 测试。 |
| [test/Dialect/CudaTile/entry_opt_hints_error.mlir](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/entry_opt_hints_error.mlir) | 各类非法提示的「告警 + 错误」用例。 |

## 4. 核心概念与源码讲解

### 4.1 属性定义机制：CudaTileAttrDef 与枚举属性族

#### 4.1.1 概念说明

MLIR 的属性分两大类：

- **带参数的自定义属性（AttrDef）**：由若干参数构成，有独立的 mnemonic（文本助记符）和解析/打印格式。本讲的 `OptimizationHintsAttr`、`DivByAttr`、`SameElementsAttr`、`BoundedAttr` 都属此类。
- **枚举属性（EnumAttr）**：取值在一个有限集合里，底层是一个 I32/I64 枚举。`signedness`、`overflow`、`rounding`、比较谓词、原子模式、内存序、内存作用域等都是枚举属性。

`cuda_tile` 用一个统一包装类 `CudaTileAttrDef` 来声明自定义属性，它在 MLIR 原生 `AttrDef` 基础上额外记录 `sinceVersion`（版本兼容，参见 u2-l3）和 `mlirExamples`/`descriptionTables`（规范文档生成用）。枚举属性则由 `CudaTileI32EnumAttr` / `CudaTileEnumAttr` 这套包装承载，每个枚举值都带上 `description` 和 `sinceVersion`。

> 一个关键点：本讲关注的是**挂在操作上、指导编译器的属性**。它们与 u3（类型）里讨论的「类型」正交——类型描述数据的形状（如 `tile<4x8xf32>`），属性描述操作的附加语义（如「按 nearest_even 舍入」「这是个 entry 内核，建议 sm_100 上用 8 个 CTA 组集群」）。

#### 4.1.2 核心流程

一个自定义属性从声明到可用的流程：

1. 在 `.td` 里用 `CudaTileAttrDef<"名字", "mnemonic", "版本">` 声明，给出 `parameters`、`description`，可选地给出 `assemblyFormat` 或 `hasCustomAssemblyFormat = 1`（手写解析/打印）、`genVerifyDecl = 1`（生成 verify 声明）。
2. TableGen 生成 `AttrDefs.h.inc`（C++ 类声明，含 getter）和 `AttrDefs.cpp.inc`（类的部分实现），手写的 `Attributes.cpp` 用 `#include "...AttrDefs.cpp.inc"` 把它们拼进来，并实现手写 verify / parse / print。
3. 方言在 `initialize()` 里调用 `registerAttributes()`（经 `GET_ATTRDEF_LIST` 宏展开）把所有属性注册到上下文。

枚举属性的流程类似，只是用 `CudaTileI32EnumAttr` 声明、`CudaTileEnumAttr` 包装出最终可挂载到操作上的属性类型。

#### 4.1.3 源码精读

`CudaTileAttrDef` 包装类在 `Dialect.td` 末尾，比 MLIR 原生 `AttrDef` 多了 `sinceVersion`、`mlirExamples`、`descriptionTables` 三个字段，用于版本兼容与规范生成：

[Dialect.td:264-274](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Dialect.td#L264-L274) —— 自定义属性包装类 `CudaTileAttrDef`，给每个属性打上 `sinceVersion` 与示例/表格元数据。

枚举属性的「带描述 + 带版本」枚举值包装在 `Dialect.td` 顶部，这是 `rounding`/`signedness`/`overflow` 等枚举的公共骨架：

[Dialect.td:105-118](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Dialect.td#L105-L118) —— `CudaTileI32EnumAttrCase` / `CudaTileI32EnumAttr`，让每个枚举值既有人类可读描述，又有版本标注。

`AttrDefs.td` 里集中了全部枚举属性的定义，是「各枚举属性族」的总目录。以下是其中代表性的几族（舍入、整数溢出、原子模式），它们的共同点是「底层 I32 枚举 + 一段规范前缀/后缀说明」：

- [AttrDefs.td:193-213](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/AttrDefs.td#L193-L213) —— `CudaTile_RoundingMode` 七种舍入模式（与 u4-l3 浮点算术呼应）。
- [AttrDefs.td:63-87](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/AttrDefs.td#L63-L87) —— `CudaTile_IntegerOverflow`（none/NSW/NUW/NW，与 u4-l2 整数算术呼应）。
- [AttrDefs.td:263-280](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/AttrDefs.td#L263-L280) —— `CudaTile_AtomicRMWModeAttr` 十种原子读改写模式（与 u5-l3 原子操作呼应）。

注册发生在 `Attributes.cpp` 末尾，用 `GET_ATTRDEF_LIST` 宏把所有 `CudaTileAttrDef` 派生属性一次性塞进方言：

[Attributes.cpp:593-598](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/Attributes.cpp#L593-L598) —— `CudaTileDialect::registerAttributes`，通过生成的宏列表注册全部自定义属性。

#### 4.1.4 代码实践

**实践目标**：建立「`.td` 声明 → 注册」的直觉。

**操作步骤**：

1. 打开 [AttrDefs.td:263-280](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/AttrDefs.td#L263-L280)，数一下 `AtomicRMWMode` 有几个枚举值，记下它们的助记符（`and/or/xor/add/addf/max/min/umax/umin/xchg`）。
2. 想一想：这个枚举属性本身有 `mnemonic` 吗？（注意它没有像 `OptimizationHintsAttr` 那样单独的 `def CudaTile_AtomicRMWModeAttr` 包装，而是由各操作直接以 `AtomicRMWModeAttr` 形式引用——这正是 `genSpecializedAttr = 0` 的效果，见 u2-l3。）

**需要观察的现象**：枚举属性在 `.td` 里只定义「枚举本体」，操作通过 `CudaTileEnumAttr<...>` 引用它；而带参数的 `OptimizationHintsAttr` 有自己的 `mnemonic`（`optimization_hints`）和独立的打印格式。

**预期结果**：能区分「枚举属性」与「带参数的自定义属性」两种声明形态。无需运行命令（源码阅读型实践）。

#### 4.1.5 小练习与答案

**练习 1**：`CudaTile_RoundingMode` 一共有几种取值？其中哪些是 IEEE 标准舍入模式？

**参考答案**：7 种（见 [AttrDefs.td:193-209](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/AttrDefs.td#L193-L209)）。四个 IEEE 模式是 `nearest_even`、`zero`、`negative_inf`、`positive_inf`；另三个是 `approx`、`full`、`nearest_int_to_zero`（整数舍入专用）。

**练习 2**：`CudaTileAttrDef` 相比 MLIR 原生 `AttrDef` 多了哪三个字段？为什么需要它们？

**参考答案**：多了 `sinceVersion`（驱动字节码版本兼容）、`mlirExamples`、`descriptionTables`（后两者驱动规范文档生成）。这与 u2-l3 讲的「单一数据源」思想一致：同一份 `.td` 同时服务于 C++、规范和字节码。

---

### 4.2 OptimizationHintsAttr 的结构：按 SM 架构分桶

#### 4.2.1 概念说明

`OptimizationHintsAttr` 是本讲的主角。它的设计动机是：**同一个内核，在不同 SM 架构上应该用不同的资源策略**。比如 Blackwell（`sm_100`）支持较大的 CGA，而 Ampere（`sm_80`）根本不支持多 CTA 集群。如果只用一个全局数值，就无法表达「架构相关」的策略。

因此这个属性采用「嵌套字典」结构：

```
optimization_hints=<
  sm_100 = {num_cta_in_cga = 8, num_worker_warps_per_cta = 8},
  sm_120 = {num_cta_in_cga = 16, num_worker_warps_per_cta = 4}
>
```

外层字典的键是架构名（`sm_80`..`sm_121`）或特殊键 `default`；每个架构对应一个内层字典，键是具体的提示名（`num_cta_in_cga` 等），值是该提示的取值。

整个属性的底层参数只有一个 `DictionaryAttr value`（见 [AttrDefs.td:94](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/AttrDefs.td#L94)），也就是「一个嵌套字典」；它的语义完全由 `extraClassDeclaration` 里的方法和 `Attributes.cpp` 里的校验逻辑赋予。

#### 4.2.2 核心流程

属性支持五类提示，挂在两类操作上：

| 提示名 | 含义 | 适用操作 | 取值约束（源码为准） |
| --- | --- | --- | --- |
| `num_cta_in_cga` | CGA 中 CTA 的个数 | `entry` | 正整数、2 的幂、≤16；`sm_80/86/87/88/89` 只能为 1 |
| `num_worker_warps_per_cta` | 每个 CTA 的 worker warp 数 | `entry` | 正整数、2 的幂、≤32；功能上仅 4/8 受支持，其余被 clamp 到 [4,8] |
| `occupancy` | 占用度提示 | `entry` | 整数，范围 [1, 32] |
| `allow_tma` | 是否使用 TMA 搬数据 | `load_view_tko` / `store_view_tko` | 布尔 |
| `latency` | 访存延迟提示 | `load_view_tko`/`store_view_tko`/`load_ptr_tko`/`store_ptr_tko` | 整数，范围 [1, 10] |

查询流程（`getNumCTAInCGA(sm)` 等 getter）采用「先精确架构、后 `default` 兜底」的策略：先在该 SM 的内层字典里找；找不到，再去 `default` 内层字典里找；都没有则返回 `std::nullopt`。这样你可以只为 `default` 写一份通用提示，再为个别架构覆盖。

> 注意一个容易踩的坑：`.td` 里的散文描述与支持矩阵表只列了 4 种提示（[AttrDefs.td:119-128](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/AttrDefs.td#L119-L128)），漏了 `occupancy`；但代码与测试都明确支持 `occupancy`（[AttrDefs.td:166](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/AttrDefs.td#L166)、[opt_hints.mlir:9](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/opt_hints.mlir#L9)）。读源码时以 C++ 校验逻辑与测试为准。

#### 4.2.3 源码精读

属性定义与描述在：

[AttrDefs.td:93-132](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/AttrDefs.td#L93-L132) —— `CudaTile_OptimizationHintsAttr` 定义。注意 `parameters = (ins "DictionaryAttr":$value)`、`hasCustomAssemblyFormat = 1`（手写 parse/print）和 `genVerifyDecl = 1`（生成 verify 声明，实现在 `Attributes.cpp`）。

`extraClassDeclaration` 里有两套架构键数组，含义不同，值得仔细区分：

[AttrDefs.td:137-172](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/AttrDefs.td#L137-L172) —— 两套键数组与五个提示常量。
- `knownKeysArr`（私有，`isKnownKey`）：11 个 SM 架构 **加上 `default`**，共 12 项。校验时用它判断「这个外层键是不是合法的架构/默认键」。
- `allowedKeysArr`（公有，`isAllowedKey`）：只有 11 个 SM 架构，**不含 `default`**。这是对外暴露的「真实可编译目标架构」清单——`default` 不是一个真实架构，所以不能出现在这里。

提示常量定义在 [AttrDefs.td:162-166](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/AttrDefs.td#L162-L166)，包括文档表里没提的 `kOccupancy`。

「精确架构优先、`default` 兜底」的查询逻辑实现为 `getAttributeForSmOrDefault`：

[Attributes.cpp:181-198](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/Attributes.cpp#L181-L198) —— 先尝试 SM 专属条目，再回退到 `default` 条目，都没有则返回 `nullopt`。

各 getter（如 `getNumCTAInCGA`）在此基础上还做一次取值校验，校验失败则返回 `nullopt`，保证「读到的值一定是合法的」：

[Attributes.cpp:305-316](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/Attributes.cpp#L305-L316) —— `getNumCTAInCGA`：取出属性后调 `validateNumCTAInCGA`，只在 `isValid()` 时返回值。

#### 4.2.4 代码实践

**实践目标**：体会「按架构分桶 + default 兜底」的查询语义。

**操作步骤**：阅读 [Attributes.cpp:181-198](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/Attributes.cpp#L181-L198) 与 [Attributes.cpp:305-316](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/Attributes.cpp#L305-L316)，在纸上推演：给定 `optimization_hints=<default = {num_cta_in_cga = 4}, sm_100 = {num_cta_in_cga = 8}>`，对 `sm_100` 调 `getNumCTAInCGA("sm_100")` 返回什么？对 `sm_120` 调呢？

**需要观察的现象**：`sm_100` 命中专属条目返回 8；`sm_120` 没有专属条目，回退到 `default` 返回 4。

**预期结果**：理解「default 是兜底而非真实架构」。源码阅读型实践，待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`isKnownKey("default")` 和 `isAllowedKey("default")` 分别返回什么？为什么不同？

**参考答案**：`isKnownKey("default")` 返回 `true`（`default` 在 `knownKeysArr` 里，校验时允许它作外层键），`isAllowedKey("default")` 返回 `false`（`default` 不在 `allowedKeysArr` 里）。因为 `default` 只是兜底标记，不是真实可编译的 SM 架构，所以「允许作为键」与「是合法编译目标」两件事被分别建模。

**练习 2**：`num_cta_in_cga = 7` 在 `sm_100` 上合法吗？在 `sm_80` 上呢？

**参考答案**：`sm_100` 上不合法——7 不是 2 的幂（[Attributes.cpp:87-91](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/Attributes.cpp#L87-L91)）；`sm_80` 上同样不合法，且 Ampere/Ada 架构（`sm_80/86/87/88/89`）要求该值必须为 1（[Attributes.cpp:75-85](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/Attributes.cpp#L75-L85)），连 2、4 都不行。

---

### 4.3 提示校验：取值规则与逐操作支持矩阵

#### 4.3.1 概念说明

`OptimizationHintsAttr` 的校验分成两个层次：

1. **属性自身的 `verify`**：只检查「外层每个条目的值都必须是字典」（结构合法性）。
2. **`verifyWithOp(op, value)`**：把属性和具体操作结合起来检查——某个提示键对这个操作是否有意义、取值是否落在合法区间。这一步是「逐操作」的。

第二层之所以要带 `op`，是因为同一个提示属性可以挂在多种操作上，但不同操作支持的提示子集不同：`num_cta_in_cga` 只对 `entry` 有意义，`allow_tma` 只对视图访存有意义，`latency` 对所有四种访存都有意义。校验必须知道「当前挂在哪种操作上」才能判断「这个提示键是否被支持」。

#### 4.3.2 核心流程

`verifyWithOp` 的流程（[Attributes.cpp:267-303](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/Attributes.cpp#L267-L303)）：

1. 按操作类型构造「该操作支持的提示键列表」`keysValidForOperation`：
   - `EntryOp` → `num_cta_in_cga`、`num_worker_warps_per_cta`、`occupancy`；
   - `LoadViewTkoOp`/`StoreViewTkoOp`/`LoadPtrTkoOp`/`StorePtrTkoOp` → `latency`；
   - 其中 `LoadViewTkoOp`/`StoreViewTkoOp` 再加 `allow_tma`。
2. 遍历外层字典的每个架构条目，对每个内层条目调 `verifyParamWithContext`：
   - 若架构键不在 `isKnownKey` 中 → 报「unknown hint key」；
   - 若提示键不在该操作的 `keysValidForOperation` 中 → 报「... is not known hint for current Operation」；
   - 否则按提示名分派到对应的 `validate*` 函数检查取值。
3. 只要任一条目校验失败 **且** `errorOnHints` 为真，`verifyWithOp` 才返回 `failure()`。

各 `validate*` 函数封装了取值边界。`num_cta_in_cga` 还有一个架构特例：Ampere/Ada 不支持多 CTA 集群，故必须为 1。

> 「2 的幂」如何用位运算判断：`n > 0 && (n & (n - 1)) == 0`。因为 2 的幂的二进制只有一个 1，减 1 后该位变 0、低位全变 1，二者按位与为 0。代码里 [Attributes.cpp:87](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/Attributes.cpp#L87) 正是这样写的。

#### 4.3.3 源码精读

`verifyWithOp` 根据操作类型动态决定支持哪些提示键——这是「逐操作支持矩阵」的真正来源：

[Attributes.cpp:267-285](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/Attributes.cpp#L267-L285) —— 用 `isa<EntryOp>(op)` / `isa<LoadViewTkoOp, ...>(op)` 分别填充 `keysValidForOperation`，再逐架构条目校验。

`num_cta_in_cga` 的校验函数，体现「架构特例 + 通用 2 的幂上限」：

[Attributes.cpp:66-93](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/Attributes.cpp#L66-L93) —— `validateNumCTAInCGA`：先要求是整数；Ampere/Ada 要求为 1；其余要求非零、2 的幂、≤16。

`occupancy` 与 `latency` 是简单的区间校验：

[Attributes.cpp:158-177](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/Attributes.cpp#L158-L177) —— `validateOccupancy` 要求整数在 [1, 32]。
[Attributes.cpp:134-153](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/Attributes.cpp#L134-L153) —— `validateLatency` 要求整数在 [1, 10]。

`num_worker_warps_per_cta` 有一个「clamp」细节：合法区间是 2 的幂且 ≤32，但功能上只真正支持 4 或 8，其它合法值（如 16）会被 `std::clamp` 到 [4,8]：

[Attributes.cpp:95-115](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/Attributes.cpp#L95-L115) —— `validateNumWorkerWarpsPerCTA`。

#### 4.3.4 代码实践

**实践目标**：把「逐操作支持矩阵」与 `.td` 的描述表对齐。

**操作步骤**：对照 [AttrDefs.td:119-128](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/AttrDefs.td#L119-L128) 的表格与 [Attributes.cpp:267-285](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/Attributes.cpp#L267-L285) 的 `isa` 判定，回答：把 `latency = 5` 挂在 `entry` 上合法吗？把 `num_cta_in_cga = 4` 挂在 `load_view_tko` 上合法吗？

**需要观察的现象**：`latency` 对 `entry` 不在支持列表里，`num_cta_in_cga` 对访存操作也不在支持列表里——二者都会触发「is not known hint for current Operation」。

**预期结果**：理解「提示键是否被支持，取决于挂在哪种操作上」。源码阅读型实践，待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`num_worker_warps_per_cta = 16` 会通过校验吗？若通过，实际生效的值是多少？

**参考答案**：会通过（16 是 2 的幂且 ≤32），但功能上仅支持 4/8，故被 `std::clamp(numWarps, 4, 8)` 收敛到 8（[Attributes.cpp:111-113](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/Attributes.cpp#L111-L113)）。注意 clamp 不改变合法性，只改变生效值。

**练习 2**：为什么 `verifyWithOp` 需要传入 `Operation *op`，而属性自身的 `verify` 不需要？

**参考答案**：属性自身的 `verify` 只能检查结构（每个外层条目是不是字典），与「挂在哪种操作上」无关，所以不需要 `op`。`verifyWithOp` 要判断「这个提示键对该操作是否有意义」「取值对该架构是否合法」，必须知道操作类型，因此需要 `op`。这是一种把「与上下文相关的校验」推迟到操作 verifier 阶段的设计。

---

### 4.4 告警/错误两级开关与命令行接线

#### 4.4.1 概念说明

这是本讲最关键、也最反直觉的设计：**默认情况下，非法/不支持的优化提示是完全静默的——编译器既不报错也不告警，只是忽略它们**。

为什么要这样设计？因为优化提示是「锦上添花」的元信息，不是内核正确性的保证。前端 lowering 工具可能为它不熟悉的架构生成不精确的提示，如果一遇到不支持的提示就让编译失败，会严重损害前向兼容（新架构出现时旧 IR 仍应能编译）。所以默认策略是宽容：能用的提示就用，用不上的就丢，绝不因此中断编译。

但开发者调试时又确实想知道「我写的提示到底有没有被识别」。于是方言提供了两级开关（用 `-W` 前缀，模仿 GCC/Clang 的告警开关约定）：

- `-Wunsupported-hints`：开启告警。非法/不支持的提示会打印一条 **warning**，但内核仍然校验通过。
- `-Werr-hints`：把告警升级为 **硬错误**。注意它依赖 `-Wunsupported-hints`（单独给 `-Werr-hints` 无效），开启后非法提示会让整个操作校验失败。

这两个开关是方言对象上的两个布尔字段 `warnUnsupportedHints_` / `errorOnHints_`（[Dialect.td:51-73](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Dialect.td#L51-L73)），由 `setWarnUnsupportedHints` / `setErrorOnHints` 设置。命令行选项通过方言扩展（dialect extension）在方言被加载时把值写进去。

#### 4.4.2 核心流程

从「敲下命令行」到「提示被检查」的完整链路：

1. `cuda-tile-opt` 的 `main` 解析 `-Wunsupported-hints` / `-Werr-hints` 两个 `llvm::cl::opt<bool>`（[cuda-tile-opt.cpp:27-36](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-opt/cuda-tile-opt.cpp#L27-L36)）。
2. 注册一个方言扩展，在 `CudaTileDialect` 被加载时调用 `dialect->setWarnUnsupportedHints(...)` / `setErrorOnHints(...)`，把命令行值灌进方言对象（[cuda-tile-opt.cpp:41-46](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-opt/cuda-tile-opt.cpp#L41-L46)）。
3. 操作校验时，`verifyOptHintsCommon` 取出 `optimization_hints` 属性并调 `verifyWithOp`（[SharedVerifiers.h:30-38](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/SharedVerifiers.h#L30-L38)）。
4. `verifyWithOp` 遍历架构条目，对每个调 `verifyParamWithContext`。后者第一步就检查 `getWarnUnsupportedHints()`——若为假直接 `return success()`，**完全不检查**（[Attributes.cpp:201-206](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/Attributes.cpp#L201-L206)）。
5. 若 `getWarnUnsupportedHints()` 为真，则逐项检查并 `emitDiagnostic`（默认 warning 级别）。若检查失败且 `errorOnHints` 为真，`verifyWithOp` 返回 `failure()`。
6. `verifyOptHintsCommon` 收到 `failure()` 后，向操作本身发出一条 op 级 error：`Optimization hints verification failed`。

> 一个重要细节：`-Werr-hints` **并不** 把每条 per-param 诊断从 warning 改成 error。即便同时给了两个开关，per-param 诊断仍是 warning；真正变成 error 的是 op 级的「Optimization hints verification failed」。这一点在 `entry_opt_hints_error.mlir` 里体现为每条用例同时有 `expected-warning`（per-param）和 `expected-error`（op 级）两条断言。

#### 4.4.3 源码精读

方言对象上的两级开关及其 getter/setter，由 `extraClassDeclaration` 注入：

[Dialect.td:51-73](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Dialect.td#L51-L73) —— `warnUnsupportedHints_`（默认 false，静默）与 `errorOnHints_`（默认 false，仅告警）。注释明确写着 errorOnHints 「requires warnUnsupportedHints_」。

命令行选项注册（在通用库 `CommandLineOptions.cpp`，供多个工具复用）：

[CommandLineOptions.cpp:89-101](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Common/CommandLineOptions.cpp#L89-L101) —— `-Wunsupported-hints` 与 `-Werr-hints` 两个 `llvm::cl::opt<bool, true>`，用外部存储 `warnUnsupportedHintsVar`/`errorUnsupportedHintsVar`。

`cuda-tile-opt` 工具入口通过方言扩展接线：

[cuda-tile-opt.cpp:25-46](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-opt/cuda-tile-opt.cpp#L25-L46) —— `main`：声明两个本地 `cl::opt`，再 `addExtension` 在方言加载时把它们写入方言对象。

「默认静默」的关键早返回：

[Attributes.cpp:201-210](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/Attributes.cpp#L201-L210) —— `verifyParamWithContext` 开头：`if (!...->getWarnUnsupportedHints()) return success();`。这一行就是「默认完全不校验提示」的全部秘密。

`emitDiagnostic` 默认是 warning，`verifyWithOp` 在 errorOnHints 时才返回失败：

[Attributes.cpp:56-61](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/Attributes.cpp#L56-L61) —— `emitDiagnostic` 工厂：默认 severity 为 Warning。
[Attributes.cpp:287-302](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/Attributes.cpp#L287-L302) —— `verifyWithOp` 主循环：对每个架构条目调 `verifyParamWithContext`，仅在 `errorOnHints` 时把失败向上传递。

操作 verifier 复用的模板，发出 op 级 error：

[SharedVerifiers.h:29-38](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/SharedVerifiers.h#L29-L38) —— `verifyOptHintsCommon`：属性非空、字典非空且 `verifyWithOp` 失败时，发 `Optimization hints verification failed`。

`OptimizationHintsAttr` 的自定义解析/打印（之所以手写，是因为 MLIR 内建 assemblyFormat 不便表达「外层是任意键名、内层是无类型字典」的结构）：

[Attributes.cpp:369-420](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/Attributes.cpp#L369-L420) —— `parse`/`print`：解析 `<...>` 内逗号分隔的 `key = {...}` 条目，打印时对内层值用 `printAttributeWithoutType`（所以整数显示为 `2` 而非 `2 : i32`）。

#### 4.4.4 代码实践

**实践目标**：亲手观察「默认静默 → 告警 → 错误」三态。

**操作步骤**：

1. 创建文件 `hints_demo.mlir`，内容（参考 [opt_hints.mlir:9](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/opt_hints.mlir#L9) 与 [entry_opt_hints_error.mlir:37](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/entry_opt_hints_error.mlir#L37)）：

   ```mlir
   cuda_tile.module @demo {
     entry @k(%arg0: !cuda_tile.tile<ptr<f32>>)
         optimization_hints=<sm_100 = {num_cta_in_cga = 7, num_worker_warps_per_cta = 8}> {
       return
     }
   }
   ```
   这里 `num_cta_in_cga = 7` 是非法值（7 不是 2 的幂）。

2. 依次运行（需先按 u1-l2 构建出 `cuda-tile-opt`，并将其加入 `PATH` 或用全路径）：

   ```bash
   cuda-tile-opt hints_demo.mlir                       # 默认
   cuda-tile-opt -Wunsupported-hints hints_demo.mlir   # 仅告警
   cuda-tile-opt -Wunsupported-hints -Werr-hints hints_demo.mlir  # 告警 + 错误
   ```

**需要观察的现象**：

- 第 1 条命令：**无任何输出报错**，正常打印合法化后的 IR。非法提示被静默忽略。
- 第 2 条命令：打印出 `warning: ... expected power-of-two ≤ 16 for sm_100.num_cta_in_cga`，但 IR 仍正常输出（内核校验通过）。
- 第 3 条命令：同样有上面那条 warning，**额外**有一条 `error: Optimization hints verification failed`，进程以非零状态退出。

**预期结果**：三态差异与上述一致。若本地尚未构建工具链，则记为「待本地验证」，但可先阅读 [entry_opt_hints_warn.mlir](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/entry_opt_hints_warn.mlir) 与 [entry_opt_hints_error.mlir](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/entry_opt_hints_error.mlir) 里的 `expected-warning`/`expected-error` 断言来印证。

#### 4.4.5 小练习与答案

**练习 1**：只给 `-Werr-hints`（不给 `-Wunsupported-hints`），非法提示会报错吗？为什么？

**参考答案**：不会。`verifyParamWithContext` 第一步就因 `getWarnUnsupportedHints()` 为假而 `return success()`（[Attributes.cpp:205-206](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/Attributes.cpp#L205-L206)），根本不会进入检查分支。`-Werr-hints` 在语义上依赖 `-Wunsupported-hints`（[Dialect.td:71-72](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Dialect.td#L71-L72) 注释）。

**练习 2**：在 `-Wunsupported-hints -Werr-hints` 下，per-param 诊断（如「expected power-of-two ≤ 16」）是 warning 还是 error？op 级的「Optimization hints verification failed」又是哪个级别？

**参考答案**：per-param 诊断仍是 **warning**（`emitDiagnostic` 默认 Warning，`-Werr-hints` 不改变它的级别）；op 级的「Optimization hints verification failed」是 **error**（由 `emitOpError` 发出，见 [SharedVerifiers.h:35](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/SharedVerifiers.h#L35)）。两者同时出现，这正是 `entry_opt_hints_error.mlir` 每条用例有两条断言的原因。

---

## 5. 综合实践

把本讲四个模块串起来，完成一个「合法 + 非法混合」的提示调试任务。

**任务**：写一个 `entry` 内核，同时给出合法与非法的优化提示，用三种命令行模式分别运行，并解释每一种现象。

**步骤**：

1. 创建 `mixed_hints.mlir`：

   ```mlir
   cuda_tile.module @mixed {
     // 合法：sm_100 上 8 个 CTA、8 个 worker warp；并用 default 兜底 occupancy
     entry @good(%arg0: !cuda_tile.tile<ptr<f32>>)
         optimization_hints=<
           default = {occupancy = 4},
           sm_100 = {num_cta_in_cga = 8, num_worker_warps_per_cta = 8}
         > {
       return
     }
     // 非法：未知架构键 sm_100a；非法提示键 num_qqq；越界 latency（latency 不适用于 entry）
     entry @bad(%arg0: !cuda_tile.tile<ptr<f32>>)
         optimization_hints=<sm_100a = {num_qqq = 1}, sm_120 = {num_cta_in_cga = 99}> {
       return
     }
   }
   ```

2. 分别运行：

   ```bash
   cuda-tile-opt mixed_hints.mlir
   cuda-tile-opt -Wunsupported-hints mixed_hints.mlir
   cuda-tile-opt -Wunsupported-hints -Werr-hints mixed_hints.mlir
   ```

3. 对每条命令，回答：
   - `@good` 是否始终通过？它的 `default = {occupancy = 4}` 在 `sm_120` 上查询 `getOccupancy("sm_120")` 会返回什么？（答：回退到 default 返回 4。）
   - `@bad` 在三种模式下各产生几条 warning、几条 error？把它们对应到 [Attributes.cpp](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/Attributes.cpp) 里具体哪一行发出的。

**预期结果**：

- 默认：`@good`、`@bad` 都正常打印，零诊断。
- `-Wunsupported-hints`：`@bad` 产生若干 warning（`unknown hint key sm_100a`、`num_qqq is not known hint`、`expected power-of-two ≤ 16 for sm_120.num_cta_in_cga`），`@good` 无 warning；二者 IR 仍输出。
- `-Wunsupported-hints -Werr-hints`：`@bad` 在上述 warning 之外再加一条 op 级 error 并导致失败。

如果尚未构建工具链，标为「待本地验证」，并对照 [entry_opt_hints_warn.mlir](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/entry_opt_hints_warn.mlir) 与 [entry_opt_hints_error.mlir](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Dialect/CudaTile/entry_opt_hints_error.mlir) 的断言核对每条诊断文本。

## 6. 本讲小结

- `cuda_tile` 的属性分两类：带参数的自定义属性（`CudaTileAttrDef` 包装，如 `OptimizationHintsAttr`、`DivByAttr`）与枚举属性（`CudaTileI32EnumAttr` + `CudaTileEnumAttr`，如舍入/溢出/原子模式/内存序等）。
- `OptimizationHintsAttr` 用「外层按 SM 架构（`sm_80`..`sm_121`）或 `default` 分桶、内层逐提示给值」的嵌套字典，表达「架构相关」的资源策略；查询时先精确架构、后 `default` 兜底。
- 五类提示挂在两类操作上：`entry` 接 `num_cta_in_cga`/`num_worker_warps_per_cta`/`occupancy`；访存操作接 `latency`，其中视图访存再加 `allow_tma`。逐操作的支持矩阵由 `verifyWithOp` 用 `isa<...>` 动态决定，而非写死在属性里。
- 默认情况下非法/不支持的提示**完全静默**——`verifyParamWithContext` 开头即 `if (!getWarnUnsupportedHints()) return success()`；这是为了前端 lowering 的前向兼容。
- `-Wunsupported-hints` 把静默变成 per-param warning，`-Werr-hints`（依赖前者）让 `verifyWithOp` 返回失败、由 `verifyOptHintsCommon` 发出 op 级 error `Optimization hints verification failed`；注意 `-Werr-hints` 不改变 per-param 诊断的 warning 级别。

## 7. 下一步学习建议

- **u6-l2（assume 与静态假设谓词）**：继续在本章属性层深入，看 `DivByAttr`/`SameElementsAttr`/`BoundedAttr` 这三个同样由 `CudaTileAttrDef` 声明的属性如何通过 `AssumePredicateAttrInterface` 接口约束 `assume` 操作，对照本讲的 `extraClassDeclaration` + 手写 verify 模式。
- **u6-l3（调试信息属性）**：看 `DICompileUnit`/`DISubprogram`/`DILoc` 等属性如何用 `CudaTile_DINodeAttr`/`DIScopeAttr` 类层级组织，理解属性之间的继承关系（本讲的 `OptimizationHintsAttr` 是扁平字典，DI 属性则是分层 scope）。
- **u5-l2 / u5-l3（视图访存与原子操作）**：回到操作层，亲手给 `load_view_tko`/`store_view_tko` 挂 `allow_tma`/`latency` 提示，验证本讲的「逐操作支持矩阵」在真实访存场景下如何生效。
- **延伸阅读**：对比 MLIR 上游 `LLVM` 方言与 `NVVM` 方言的 `ReqLLVMArg`/属性约定，体会 CUDA Tile 用 `CudaTileAttrDef` 多带 `sinceVersion`/`mlirExamples` 的「单一数据源」设计取舍。
