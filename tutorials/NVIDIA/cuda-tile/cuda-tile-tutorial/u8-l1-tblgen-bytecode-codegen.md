# cuda-tile-tblgen 字节码代码生成

## 1. 本讲目标

本讲面向已经理解「`.td` 单一数据源」思想（见 u2-l3）和「字节码写入端二进制格式」（见 u7-l2）的读者，回答一个工程核心问题：

> 当我们为 `cuda_tile` 方言新增一个操作、一个类型或一个属性时，它在字节码（`.tilebc`）里的「读」和「写」代码从哪里来？

答案是：**全部由 `cuda-tile-tblgen` 工具自动生成**。本讲学完后，你应当能够：

1. 说出三组 `.td` 文件（`BytecodeOpcodes.td` / `BytecodeTypeOpcodes.td` / `BytecodeAttrOpcodes.td`）分别定义了「操作 / 类型 / 属性」到数值编码的映射，以及它们为何一旦分配就**永不重新编号**。
2. 看懂 `BytecodeGen.cpp`（写入端）与 `BytecodeReaderGen.cpp`（读取端）如何把一条 `.td` 声明翻译成对应的 `write<OpName>` / `parse<OpName>` C++ 函数，并组装出分发表。
3. 理解 `BytecodeTypeCodeGen.cpp` 与 `BytecodeAttrCodeGen.cpp` 生成「类型序列化函数 / `TypeTag` 枚举 / `AttributeTag` 枚举 / 枚举属性版本检查」的流程。
4. 追踪新增一个操作时，在「定义→生成→使用」整条链路上需要触碰的全部位置。

---

## 2. 前置知识

在进入源码前，先建立几个直觉。本讲是 u2-l3（TableGen 代码生成）与 u7-l2（字节码写入器）的合流，因此下述概念默认你已接触过：

- **TableGen 记录（Record）与后端（Backend）**：`.td` 文件声明「数据」，`tblgen` 工具里的某个 `GenRegistration` 后端把数据翻译成「C++ 代码」。一个后端 = 一种翻译动作。
- **`.inc` 产物与 include 技巧**：生成的 C++ 代码不写成完整 `.cpp`，而是写成「带 `#ifdef GEN_xxx` 守卫」的片段（`.inc`）。手写源文件用 `#define GEN_xxx` + `#include "xxx.inc"` 的方式，把想要的片段「切」出来编译。同一份 `.inc` 可被多次 `#include`，每次取不同片段。
- **opcode / tag 是「身份号」**：字节码里不能写操作名（如 `"cuda_tile.addf"`）这种长字符串，那样太浪费空间。取而代之，每个操作、每种类型、每种属性都被分配一个**定长小整数**（操作叫 opcode，类型/属性叫 tag）。读写双方靠这个数字互相识别。
- **冻结（FROZEN）与版本兼容**：一旦某个操作拿到了 opcode `0x2`，它在所有未来版本里都必须是 `0x2`。否则旧工具读到的 `0x2` 就会指错操作。这就是 u7-l4 讲过的「opcode 永不重新编号」铁律在代码生成层的落地。
- **写入端 vs 读取端对称**：序列化（写）与反序列化（读）是互逆过程，因此 tblgen 为它们各自生成一套代码，且二者必须严格对应——写了几个 varint，就得读几个 varint。

> 关键直觉：本讲讲的是「**编译 IR 编译器的编译器**」。`cuda-tile-tblgen` 在构建期运行，产物是读写器赖以工作的 C++ 胶水代码；它本身不参与运行时序列化。

---

## 3. 本讲源码地图

| 文件 | 角色 |
|------|------|
| [`include/cuda_tile/Dialect/CudaTile/IR/BytecodeOpcodes.td`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/BytecodeOpcodes.td) | 操作→opcode 的「单一数据源」 |
| [`include/cuda_tile/Dialect/CudaTile/IR/BytecodeTypeOpcodes.td`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/BytecodeTypeOpcodes.td) | 类型→tag 的「单一数据源」 |
| [`include/cuda_tile/Dialect/CudaTile/IR/BytecodeAttrOpcodes.td`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/BytecodeAttrOpcodes.td) | 属性→tag 的「单一数据源」 |
| [`tools/cuda-tile-tblgen/BytecodeGen.cpp`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/BytecodeGen.cpp) | 写入端后端：`gen-cuda-tile-bytecode` 等 4 个后端 |
| [`tools/cuda-tile-tblgen/BytecodeReaderGen.cpp`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/BytecodeReaderGen.cpp) | 读取端后端：`gen-cuda-tile-bytecode-reader` 等 2 个后端 |
| [`tools/cuda-tile-tblgen/BytecodeTypeCodeGen.cpp`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/BytecodeTypeCodeGen.cpp) | 类型胶水生成器（被 4.2/4.3 调用） |
| [`tools/cuda-tile-tblgen/BytecodeAttrCodeGen.cpp`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/BytecodeAttrCodeGen.cpp) | 属性胶水生成器（枚举/版本检查） |
| [`lib/Bytecode/Writer/CMakeLists.txt`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Writer/CMakeLists.txt) | 把后端挂到构建系统的「胶水」 |
| [`lib/Bytecode/BytecodeEnums.h`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/BytecodeEnums.h) | 读写器共享的 `TypeTag`/`AttributeTag` 枚举入口 |

---

## 4. 核心概念与源码讲解

### 4.1 opcode / tag 的「单一数据源」：三组 .td 定义

#### 4.1.1 概念说明

字节码里要编码三类对象：**操作**（如 `addf`）、**类型**（如 `tile<4x8xf32>`）、**属性**（如舍入模式 `RoundingModeAttr`）。它们分别由三份 `.td` 文件集中声明「该对象 = 哪个数字」：

- 操作 → `PublicOpcode<Op, opcode>`（在 `BytecodeOpcodes.td`）
- 类型 → `BytecodeTypeTag<typeName, tag>`（在 `BytecodeTypeOpcodes.td`）
- 属性 → `BytecodeAttrTag<attrName, tag>`（在 `BytecodeAttrOpcodes.td`）

这三份文件就是「单一数据源」：tblgen 读它们，生成 `Opcode` / `TypeTag` / `AttributeTag` 三个枚举；读写器运行时只认数字。把映射集中在 `.td`、而不是散落在 C++ 里，好处是新增对象只需改一行 `.td`，枚举、读写函数、版本检查全部自动跟上。

#### 4.1.2 核心流程

操作 opcode 的分配流程：

```text
BytecodeOpcodes.td                  cuda-tile-tblgen                   StaticOpcodes.inc
─────────────────                   ────────────────                   ──────────────────
def : PublicOpcode<                 gen-cuda-tile-opcodes    ───▶      enum class Opcode {
    CudaTile_AddFOp, 0x2>;                                                AddFOp = 0x2,
                                                                          ...
                                                                        };
                                                                        + 字符串→Opcode 映射表
                                                                        + 每版本最大 opcode 表
```

三组 `.td` 共享同一种「铁律」：**数字一旦分配，永不更改**。源码注释把这一点反复标记为 `FROZEN`。

#### 4.1.3 源码精读

**操作的 opcode 基类与公共分配**。`BytecodeOpcode` 是基类，`PublicOpcode` 表示「所有构建都可见」的正式操作，二者由一个 Op 与一个整数构成；`PublicOpcodeRange` 约束了合法取值区间：

[include/cuda_tile/Dialect/CudaTile/IR/BytecodeOpcodes.td:26-46](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/BytecodeOpcodes.td#L26-L46) — 定义 `BytecodeOpcode`/`PublicOpcode` 类与取值范围 `0x0-0xFFF`（公共）/ `0x3000+`（仅测试构建）。

随后是冻结的分配表，逐行声明每个操作对应的 opcode，例如 `addf=0x2`、`constant=0x10`、`fma=0x28`、`mmaf_scaled=0x72`（13.3 新增，追加在表尾）：

[include/cuda_tile/Dialect/CudaTile/IR/BytecodeOpcodes.td:71-170](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/BytecodeOpcodes.td#L71-L170) — 公共操作 opcode 分配表（FROZEN）。

**类型的 tag 基类与分配**。类型有整数、浮点、自定义三类 tag，分别承载位宽 / MLIR 类型名 / 无额外信息：

[include/cuda_tile/Dialect/CudaTile/IR/BytecodeTypeOpcodes.td:26-46](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/BytecodeTypeOpcodes.td#L26-L46) — `BytecodeTypeTag` 与 `IntegerTypeTag`/`FloatTypeTag`/`CudaTileTypeTag` 三个子类；它的分配表按版本分组，13.1 的类型占据 `0-17`，13.2 新增 `Float8E8M0FNU=18`，13.3 新增 `Float4E2M1FN=19`、`GatherScatterViewType=20`、`StridedViewType=21`、`Int4=22`，全部**追加在尾部**：

[include/cuda_tile/Dialect/CudaTile/IR/BytecodeTypeOpcodes.td:52-79](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/BytecodeTypeOpcodes.td#L52-L79) — 类型 tag 分配表，按 13.1/13.2/13.3 分组。

**属性的 tag 基类与分配**。属性 tag 类只需属性名与整数：

[include/cuda_tile/Dialect/CudaTile/IR/BytecodeAttrOpcodes.td:28-49](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/BytecodeAttrOpcodes.td#L28-L49) — `BytecodeAttrTag` 类与属性 tag 分配表（`Integer=1` … `Bounded=12`，测试 `BytecodeTestValue=200`）。注意属性 tag 从 `1` 起，与类型从 `0` 起不同。

> 注意：这里只声明了「属性**种类**」的 tag（如 `DivBy`/`OptimizationHints`/`Bounded`，对assume谓词与优化提示等自定义属性）。MLIR 内建属性（如 `IntegerAttr`/`FloatAttr`）虽也出现在表里，但它们的版本/枚举信息实际来自 `AttrDefs.td`（见 `BytecodeAttrTag` 类注释），这是「属性」比「操作/类型」更复杂的地方，详见 4.4。

#### 4.1.4 代码实践

**实践目标**：亲手核对「opcode 一旦冻结就不变」这一承诺，并理解版本分组。

**操作步骤**：

1. 打开 `BytecodeOpcodes.td`，找到 `CudaTile_AddFOp` 与 `CudaTile_MmaFScaledOp` 两行，记录它们的 opcode。
2. 对照本系列 u4-l3（浮点算术）与 u4-l5（MMA）——`addf` 是 13.1 就存在的老操作，`mmaf_scaled` 是 13.3 引入的新操作。观察新操作的 opcode（`0x72`）是否落在表的最末尾。
3. 打开 `BytecodeTypeOpcodes.td`，确认 13.3 新增的 `Int4`（tag `22`）同样追加在 13.1 类型之后，没有插入到中间。

**预期结果**：所有「新版本新增」的编码都严格**追加**在已有编码之后，从不挤占中间位置——这正是「永不重新编号」的物理体现。

**待本地验证**：若你已构建项目，可在 `build/lib/Bytecode/Writer/StaticOpcodes.inc` 里搜到 `enum class Opcode { ... AddFOp = 0x2 ... }`，与 `.td` 一一对应。

#### 4.1.5 小练习与答案

**练习 1**：为什么测试操作（如 `CudaTile_Test_FuncOp`）被分配到 `0x3000+` 而不是和公共操作挤在 `0x0-0xFFF`？
**答案**：测试操作只在 `TILE_IR_INCLUDE_TESTS` 宏打开时存在，正式发布构建里没有它们。用一段完全分离的高位区间（`TestingOpcodeRange = 0x3000-0xFFFFFFFF`），可保证测试操作永远不占用公共区间，避免编号碰撞与正式构建体积膨胀。

**练习 2**：如果有人把 `CudaTile_AddFOp` 的 opcode 从 `0x2` 改成 `0x100`，会发生什么？
**答案**：旧版工具写出的字节码里 `0x2` 原本表示 `addf`，新工具读 `0x2` 却找不到对应（或指向别的操作），导致反序列化错误或静默错读——破坏向后兼容。这正是 tblgen 校验「无重复 opcode」也无法挽救的场景（它只查重复，不查历史值），所以才需要靠纪律「FROZEN」来约束。

---

### 4.2 BytecodeGen：写入端代码生成（write\<OpName\> 与分发）

#### 4.2.1 概念说明

`BytecodeGen.cpp` 注册了 4 个 tblgen 后端，其中两个直接面向「操作」：

- `gen-cuda-tile-bytecode`：为**每个操作**生成一个 `write<OpName>` 函数（负责把该操作的结果类型、属性、操作数、region 依次写成字节流），并生成一个 `TypeSwitch` 分发表（根据运行时操作类型派发到对应 `write` 函数）。
- `gen-cuda-tile-opcodes`：生成 `Opcode` 枚举、字符串→Opcode 映射表、版本→最大 opcode 映射（驱动 `isOpcodeAvailableInVersion`）。

另外两个后端（`gen-cuda-tile-type-bytecode`、`gen-cuda-tile-attr-bytecode`）委托给 4.4 讲的 Type/Attr CodeGen，本节先聚焦操作。

#### 4.2.2 核心流程

为单个操作生成写入函数的组装顺序（对应 `generateOpWriter`）：

```text
write<OpName>(op, writer, typeMgr, constMgr, strMgr, config):
  1. 写结果类型    generateResultTypeSerialization   (含版本感知：新结果在老目标下跳过)
  2. 写 flags 字段 generateFlagsFieldSerialization   (可选属性/操作数的位图)
  3. 写属性        generateAttributeSerialization     (每个属性→writeOpAttribute)
  4. 写操作数      generateOperandSerialization       (AttrSized 段或普通)
  5. 写 region     generateRegionSerialization        (递归 writeRegion)
  return 已序列化结果数
```

`flags` 字段是版本兼容的关键机制：它是一个 varint，用**按版本排序的位**编码每个可选属性/操作数是否存在（位布局 \( \text{bit}_k = \text{第 } k \text{ 个（按版本序）可选字段是否存在} \)）。新增可选字段只能追加新位，不能挪动旧位，从而保证旧读取器读到的低位布局不变。

#### 4.2.3 源码精读

**入口 `generateBytecode`：用 `#ifdef` 切出两块产物**。整个后端把内容包进 `GEN_OP_WRITERS`（一堆 `write` 函数）与 `GEN_OP_WRITER_DISPATCH`（TypeSwitch）两个守卫，写入 `Bytecode.inc`：

[tools/cuda-tile-tblgen/BytecodeGen.cpp:725-741](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/BytecodeGen.cpp#L725-L741) — `generateBytecode` 把「writer 实现」与「dispatch switch」分别包进两个宏守卫输出。

**单个操作的 writer 组装**。`generateOpWriter` 严格按 5 步顺序拼出函数体，最后返回 `numSerializedResults`（供 `valueIndexMap` 更新）：

[tools/cuda-tile-tblgen/BytecodeGen.cpp:644-656](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/BytecodeGen.cpp#L644-L656) — `generateOpWriter` 调用 5 个子生成器并收尾。

**flags 字段的版本感知写法**。`generateFlagsFieldSerialization` 对每个可选属性/操作数，先查它属于哪个版本（`extractVersionFromAttribute`），若该字段是「操作诞生之后才加的」，就生成一段「仅在目标版本足够新时才写 flags」的代码，否则照常写——这让 13.2 写入器仍能以 `--bytecode-version=13.1` 输出旧格式：

[tools/cuda-tile-tblgen/BytecodeGen.cpp:263-398](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/BytecodeGen.cpp#L263-L398) — flags 字段序列化，含「首个可选字段版本晚于操作基线时才生成版本检查」的兼容逻辑（见 369-397 行的注释与分支）。

**dispatch：TypeSwitch 派发**。`generateDispatchSwitch` 为每个操作生成一个 `.Case<类名>([&](auto op){ return write类名(op, ...); })`，未覆盖的类型落到 `Default` 报错：

[tools/cuda-tile-tblgen/BytecodeGen.cpp:684-722](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/BytecodeGen.cpp#L684-L722) — `generateDispatchSwitch` 生成 `TypeSwitch<Operation*, FailureOr<size_t>>`。

**opcode 枚举与校验**。`generateOpcodeEnumDefinition` 先调用 `validateAllOpcodeAssignments` 做四项检查（重复值、重复操作、范围合法、测试/公共分离），再调用 `validateAllOperationsHaveOpcodes` 确保**每个** `CudaTileOpDef` 都有 opcode 分配——这意味着「新增操作却忘了在 `BytecodeOpcodes.td` 登记」会在构建期直接 `PrintFatalError`：

[tools/cuda-tile-tblgen/BytecodeGen.cpp:48-60](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/BytecodeGen.cpp#L48-L60) — `validateAllOperationsHaveOpcodes` 对每个操作缺 opcode 的情况报致命错误。

[tools/cuda-tile-tblgen/BytecodeGen.cpp:158-195](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/BytecodeGen.cpp#L158-L195) — `generateOpcodeEnumDefinition` 生成 `enum class Opcode { ... }`。

**版本→最大 opcode 映射**。`generateVersionConstants` 扫描每个 opcode 所属操作的 `operationVersion`，按版本累计最大 opcode，再前向传播（后版本继承前版本的最大值），供读取端 `isOpcodeAvailableInVersion` 做 O(1) 判定（详见 u7-l4）：

[tools/cuda-tile-tblgen/BytecodeGen.cpp:744-824](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/BytecodeGen.cpp#L744-L824) — `generateVersionConstants` 生成 `getVersionToMaxOpcodeMap()`。

**后端注册**。4 个后端用 `mlir::GenRegistration` 静态全局对象自注册（与 u2-l3 讲的机制一致）：

[tools/cuda-tile-tblgen/BytecodeGen.cpp:1049-1075](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/BytecodeGen.cpp#L1049-L1075) — 注册 `gen-cuda-tile-bytecode`、`gen-cuda-tile-opcodes`、`gen-cuda-tile-type-bytecode`、`gen-cuda-tile-attr-bytecode`。

#### 4.2.4 代码实践

**实践目标**：验证「每个操作都被强制登记 opcode」，并观察「忘登记」的报错路径。

**操作步骤**：

1. 阅读 `validateAllOpcodeAssignments`（68-155 行），列出它做的四类校验。
2. 想象在 `Ops.td` 新增一个 `def CudaTile_MyOp : CudaTile_CoreOpDef<...>` 但**故意不**在 `BytecodeOpcodes.td` 加 `PublicOpcode`。
3. 推测构建会停在哪一步、报什么错。

**需要观察的现象**：构建 `cuda-tile-tblgen` 本身不会失败（它只是个工具）；但当 `lib/Bytecode/Writer` 调用 `tablegen(... -gen-cuda-tile-opcodes)` 生成 `StaticOpcodes.inc` 时，`validateAllOperationsHaveOpcodes` 会命中并 `PrintFatalError`，报「operation 'cuda_tile.my_op' is missing BytecodeOpcode assignment」，整个构建中止。

**预期结果**：「单一数据源」+ 构建期强校验共同保证：**没有操作能逃出 opcode 表**。这是防止「静默序列化失败」的第一道闸。

**待本地验证**：若手头有可构建环境，可临时在一个分支上做此实验；无环境则按上述推理确认即可。

#### 4.2.5 小练习与答案

**练习 1**：`generateOpWriter` 里「结果类型序列化」为什么要做版本感知（`generateVersionAwareResultSerialization`），而属性序列化里有些分支却直接写、不做检查？
**答案**：一个操作可能在后续版本**新增结果**（如某个操作 13.3 多了个返回值）。若目标版本低于该结果引入的版本，写入器必须跳过这个结果，否则旧读取器会多读一个类型、错位。而「与操作同时诞生」的属性/结果无需检查，因为只要操作本身在该版本可用，它的原始字段就一定在。

**练习 2**：flags 字段为什么按「版本顺序」分配位，而不是按 `.td` 声明顺序？
**答案**：保证向后兼容。新可选字段只能追加在高位，旧字段占据的低位永远不变；这样旧读取器读 flags 时低位布局与它认知一致，高位即使存在也被忽略。若按声明顺序，重排声明就会移动位，破坏兼容。

---

### 4.3 BytecodeReaderGen：读取端代码生成（parse\<OpName\> 与 switch 分发）

#### 4.3.1 概念说明

`BytecodeReaderGen.cpp` 是 `BytecodeGen.cpp` 的镜像：写入端写什么，读取端就按相同顺序读什么。它注册 2 个后端：

- `gen-cuda-tile-bytecode-reader`：为每个操作生成 `parse<OpName>` 函数（按写入顺序读结果类型、flags、属性、操作数、region，最后 `createOperationGeneric` 造出操作），并生成一个 `switch(opcode)` 分发器。
- `gen-cuda-tile-type-bytecode-reader`：生成类型反序列化函数（委托 4.4）。

读取端比写入端多一层复杂性：它还要处理「读取旧字节码时，把后版本才有的字段填默认值」——这是后向兼容的核心。

#### 4.3.2 核心流程

单个操作读取函数的组装（对应 `generateOpReader`）：

```text
parse<OpName>(reader, builder, loc, valueIndexList, ..., bytecodeVersion):
  1. 读结果类型   generateResultTypeDeserialization   (新结果在旧文件里→用默认类型，不进 valueIndexList)
  2. 读 flags     generateFlagsFieldDeserialization   (仅当版本够新才读)
  3. 读属性       generateAttributeDeserialization     (缺省→默认值)
  4. 读操作数     generateOperandDeserialization       (AttrSized 段大小来自 flags/varint)
  5. 读 region    generateRegionDeserialization
  6. createOperationGeneric("cuda_tile.xxx", resultTypes, operands, attrs, ...)
```

分发器是一个紧凑的 `switch`，每个 `case Opcode::Xxx: parseXxx(...); break;`。

#### 4.3.3 源码精读

**函数签名模板**。所有 `parse<OpName>` 共用一套形参表（reader、builder、loc、`valueIndexList`——按 SSA 编号查已读值、类型表、常量缓存、调试信息迭代器、上下文、**字节码版本**）。版本是读取端做兼容判断的依据：

[tools/cuda-tile-tblgen/BytecodeReaderGen.cpp:35-46](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/BytecodeReaderGen.cpp#L35-L46) — `functionSignatureTemplate`。

**opcode 分发的 case 模板与生成**。`dispatchCaseTemplate` 把 `case Opcode::类名: parse类名(...); break;` 拼出来，`generateOpReaderDispatch` 遍历所有操作生成完整 `switch`，`default` 分支报未知 opcode：

[tools/cuda-tile-tblgen/BytecodeReaderGen.cpp:176-181](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/BytecodeReaderGen.cpp#L176-L181) — `dispatchCaseTemplate`。

[tools/cuda-tile-tblgen/BytecodeReaderGen.cpp:689-705](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/BytecodeReaderGen.cpp#L689-L705) — `generateOpReaderDispatch` 生成 `switch(opcode)`。

**单个操作 reader 的组装**。与 writer 一一对应，6 步顺序读：

[tools/cuda-tile-tblgen/BytecodeReaderGen.cpp:657-672](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/BytecodeReaderGen.cpp#L657-L672) — `generateOpReader`。

**后向兼容的关键：版本化属性填默认值**。读取属性时，若字节码版本低于属性引入版本，`generateAttributeDeserialization` 的 `else` 分支用 `defaultValue` 构造属性；若是可选属性则填 `nullptr`（缺失）。这保证「新读取器读旧文件」时不缺字段：

[tools/cuda-tile-tblgen/BytecodeReaderGen.cpp:407-480](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/BytecodeReaderGen.cpp#L407-L480) — 版本化属性解析，含 `else` 分支按属性种类构造默认值。

**版本化结果的 SSA 编号保护**。读取新结果（文件版本够新）时才把它加入 `valueIndexList`；旧文件里不存在的结果用默认类型构造但**不入编号表**，从而不破坏后续指令对 SSA 编号的引用——这就是 u7-l3 讲的「不破坏 SSA 编号」在代码生成层的实现：

[tools/cuda-tile-tblgen/BytecodeReaderGen.cpp:624-628](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/BytecodeReaderGen.cpp#L624-L628) — `numResultsForValueIndex = expectedResults`（只计入兼容结果）。

**后端注册**：

[tools/cuda-tile-tblgen/BytecodeReaderGen.cpp:757-769](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/BytecodeReaderGen.cpp#L757-L769) — 注册 `gen-cuda-tile-bytecode-reader`、`gen-cuda-tile-type-bytecode-reader`。

#### 4.3.4 代码实践

**实践目标**：体会「读写对称」——同一段信息，写入端写的字段顺序与读取端读的顺序必须逐字段一致。

**操作步骤**：

1. 在 `BytecodeGen.cpp` 的 `generateOpWriter`（644-656 行）记下写入顺序：结果→flags→属性→操作数→region。
2. 在 `BytecodeReaderGen.cpp` 的 `generateOpReader`（657-672 行）记下读取顺序。
3. 逐项对照，确认二者完全一致。
4. 思考：若有人改了写入端的顺序（比如把属性挪到操作数之后）却忘了同步读取端，会出现什么现象？

**预期结果**：顺序一一对应。一旦错位，读取端会把「属性的字节」当成「操作数」解析，导致类型不匹配报错或更隐蔽的语义错乱。这正是「读写两端由**两份独立**生成器维护、却必须严格对齐」的工程风险点——也是为何本项目坚持二者都从同一份 `.td` 派生，以最大限度减少人工漂移。

**待本地验证**：纯源码对照即可完成，无需运行。

#### 4.3.5 小练习与答案

**练习 1**：为什么读取端的 `parse<OpName>` 需要 `valueIndexList` 参数，而写入端的 `write<OpName>` 不需要？
**答案**：字节码里操作数不是按名字记录，而是按「它在函数体中第几个被定义的 SSA 值」记录（一个 varint 索引）。读取端需要一个 `valueIndexList` 把「索引 → Value」的表逐步建起来，读到一个索引就去表里查回对应的 SSA 值。写入端反过来——它手上有真实的 `op->getOperands()`，只需把每个操作数的索引写出去。

**练习 2**：`generateOpReaderDispatch` 用 `switch(opcode)`，而写入端的 `generateDispatchSwitch` 用 `TypeSwitch<Operation*>`。为什么两端选了不同的分派结构？
**答案**：写入端拿到的是内存里的 `Operation*`，要按「具体 C++ 类」分派，`TypeSwitch` 正是为「按类型分派」设计；读取端从字节流读出的是一个整数 opcode，天然适合 `switch(整数)`。结构选择服从输入数据形态。

---

### 4.4 BytecodeTypeCodeGen 与 BytecodeAttrCodeGen：类型/属性胶水

#### 4.4.1 概念说明

操作有「操作数/属性/结果/region」这种相对统一的骨架，所以 4.2/4.3 能用一个通用模板批量生成。但**类型**和**属性**的内部结构差异更大（类型有形状数组、步长、元素类型；属性有枚举、字典、稠密元素），因此它们各自有专门的生成器：

- `BytecodeTypeCodeGen.cpp`：被 `gen-cuda-tile-type-bytecode`（写）和 `gen-cuda-tile-type-bytecode-reader`（读）调用。生成 `TypeTag` 枚举、每个类型的 `serialize类型名` / `parse类型名` 函数、以及 `TypeSwitch`/`switch` 分派。
- `BytecodeAttrCodeGen.cpp`：被 `gen-cuda-tile-attr-bytecode` 调用。生成 `AttributeTag` 枚举、属性/枚举的版本检查函数（`isAttrTagAvailableInVersion` 等），以及供读写器识别枚举的类型 trait（`is_cuda_tile_enum`、`symbolizeEnum` 特化）。

#### 4.4.2 核心流程

**类型的可选参数位域（unified bitfield）** 是这部分的精华。一个类型（如视图类型）可能含若干可选参数。13.3 起统一用一个 bitfield 编码「哪些可选参数存在」，<13.3 用旧的内联 flag 字节。代码生成时按类型引入版本自动选择分支：

```text
类型 serialize 类型名(type, writer, config):
  若该类型需版本检查: 校验 config.bytecodeVersion >= 类型 sinceVersion
  写 TypeTag
  若有可选参数: 写 optionalFlags 位域（仅当版本足够新）
  逐参数序列化（可选参数查位域；版本化参数查目标版本）
```

**属性的版本检查** 则更细：不仅整个属性有版本，枚举属性的**单个枚举值**也可能后加（如某个舍入模式 13.2 才引入），因此 `generateEnumValueVersionCheck` 为每个枚举值生成独立的版本判定。

#### 4.4.3 源码精读

**TypeTag 枚举**。从分析结果 `structure.allTypeTags` 直接渲染：

[tools/cuda-tile-tblgen/BytecodeTypeCodeGen.cpp:196-206](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/BytecodeTypeCodeGen.cpp#L196-L206) — `generateTypeTagEnum` 生成 `enum class TypeTag : uint8_t`。

**单类型的 serializer**。先写 tag，再处理可选参数位域，再逐参数序列化（区分「原始参数」与「版本化参数」，后者在老目标下必须等于默认值否则报错）：

[tools/cuda-tile-tblgen/BytecodeTypeCodeGen.cpp:457-520](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/BytecodeTypeCodeGen.cpp#L457-L520) — `generateCudaTileTypeSerializer`。

**可选参数位域（写入端）**。按「类型是否 ≥13.3」分别生成不同的版本判断分支，体现 unified bitfield 的演进：

[tools/cuda-tile-tblgen/BytecodeTypeCodeGen.cpp:376-445](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/BytecodeTypeCodeGen.cpp#L376-L445) — `generateOptionalParamFlags`。

**类型分派（写入 `TypeSwitch` / 读取 `switch`）**。两端各生成一个，覆盖内建类型（Integer/Float）、CudaTile 自定义类型、`FunctionType`，其余报错：

[tools/cuda-tile-tblgen/BytecodeTypeCodeGen.cpp:759-798](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/BytecodeTypeCodeGen.cpp#L759-L798) — `generateSerializerDispatch`。

[tools/cuda-tile-tblgen/BytecodeTypeCodeGen.cpp:825-857](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/BytecodeTypeCodeGen.cpp#L825-L857) — `generateDeserializerDispatch`。

**AttributeTag 枚举与属性版本检查**：

[tools/cuda-tile-tblgen/BytecodeAttrCodeGen.cpp:31-47](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/BytecodeAttrCodeGen.cpp#L31-L47) — `generateAttrTagEnum`。

[tools/cuda-tile-tblgen/BytecodeAttrCodeGen.cpp:53-86](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/BytecodeAttrCodeGen.cpp#L53-L86) — `generateAttrVersionCheck` 生成 `isAttrTagAvailableInVersion`。

**逐枚举值版本检查**。这是属性侧最精细的一层，每个枚举值（case）都有自己的版本门：

[tools/cuda-tile-tblgen/BytecodeAttrCodeGen.cpp:183-225](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/tools/cuda-tile-tblgen/BytecodeAttrCodeGen.cpp#L183-L225) — `generateEnumValueVersionCheck`，对枚举的每个 case 生成 `case Xxx: return version >= ...`，未列出的值返回 false。

#### 4.4.4 代码实践

**实践目标**：把生成端与消费端连起来——找到这些生成产物在读写器里被「切」出来的确切位置。

**操作步骤**：

1. 打开 [`lib/Bytecode/BytecodeEnums.h`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/BytecodeEnums.h)，看 46-48 行如何用 `#define GEN_TYPE_TAG_ENUM` + `#include "../Writer/TypeBytecode.inc"` 切出 `TypeTag` 枚举，77-79 行同理切出 `AttributeTag`。注意读写器共用同一份 `.inc`（位于 Writer 构建目录，Reader 通过 `include_directories(.../Writer)` 引用）。

2. 打开 [`lib/Bytecode/Writer/BytecodeWriter.cpp`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Writer/BytecodeWriter.cpp)，定位：
   - 938-942 行：`GEN_OPCODE_ENUM` 与 `GEN_OPCODE_MAP` 从 `StaticOpcodes.inc` 切出操作 opcode 枚举与映射。
   - 1300-1301 行：`GEN_OP_WRITERS` 从 `Bytecode.inc` 切出全部 `write` 函数。
   - 1313-1314 行：`GEN_OP_WRITER_DISPATCH` 从同一份 `Bytecode.inc` 切出 TypeSwitch 分派。

3. 打开 [`lib/Bytecode/Reader/BytecodeReader.cpp`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Bytecode/Reader/BytecodeReader.cpp)，定位：
   - 433-434 行：`GEN_OPCODE_ENUM` 复用 Writer 的 `StaticOpcodes.inc`。
   - 1848-1849 行：`GEN_OP_READERS` 从 `BytecodeReader.inc` 切出全部 `parse` 函数。
   - 1877-1878 行：`GEN_OP_READER_DISPATCH` 从同一份切出 `switch(opcode)`，嵌在 `parseOperation` 里。

**需要观察的现象**：同一份 `.inc` 被多次 `#include`，每次靠不同 `#define` 取出不同片段；读写器复用 Writer 目录下的 `StaticOpcodes.inc` / `TypeBytecode.inc` / `AttrBytecode.inc`，只有「操作读写函数」因读写不对称而分成 `Bytecode.inc`（写）与 `BytecodeReader.inc`（读）两份。

**预期结果**：你能画出生成产物到使用点的完整对应表（见综合实践）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `TypeTag`/`AttributeTag` 枚举放在共享的 `BytecodeEnums.h`，而操作 `Opcode` 枚举却分别在 Writer（938 行）和 Reader（433 行）各 `#include` 一次？
**答案**：`TypeTag`/`AttributeTag` 是「字面量枚举」，读写两端都需要同一份定义且定义完全相同，放共享头最简洁。操作 `Opcode` 虽也是枚举，但读写器各自还需要不同的伴生产物（写入端的映射表用于「操作→opcode」，读取端的 switch 用于「opcode→操作」），所以各自在自己的 `.cpp` 里 include，以便同时取出枚举与各自的伴生片段。

**练习 2**：`generateEnumValueVersionCheck` 里，对未在 `switch` 列出的枚举值返回 `false`（拒绝），这有什么好处？
**答案**：它使读取端能拒绝「未来版本才有的未知枚举值」或「非法脏数据」，而不是默默接受一个无法理解的值。结合 `default → false`，这是一种防御式反序列化：只放行 `.td` 明确登记且当前版本支持的值。

---

## 5. 综合实践

**综合任务**：在脑中为一个**假想的新操作** `cuda_tile.foo` 走通「定义 → 代码生成 → 使用」全链路，列出新增一个操作到字节码所需的**全部改动位置**。

**背景设定**：假设 `foo` 是一个 Core 组操作，输入一个 `tile<f32>`、输出一个 `tile<f32>`，带一个可选枚举属性 `mode`（13.3 引入），无 region。

**操作步骤**：

1. **声明操作语义**：在 `include/cuda_tile/Dialect/CudaTile/IR/Ops.td` 里 `def CudaTile_FooOp : CudaTile_CoreOpDef<...>`，并给出 `operationVersion = "13.3"`、可选属性 `mode` 的 `sinceVersion`。同时把 `mode` 枚举写在 `AttrDefs.td`（用 `CudaTileI32EnumAttr`）。

2. **分配 opcode（关键且唯一的手动编号）**：在 [`BytecodeOpcodes.td:170`](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/BytecodeOpcodes.td#L170) 之后**追加**一行 `def : PublicOpcode<CudaTile_FooOp, 0x76>;`（取当前最大值 `0x75` 之后的下一个空闲值，绝不复用中间值）。

3. **若引入了新属性种类**（本例 `mode` 是已有 `CudaTileI32EnumAttr`，复用即可，无需新 tag）：只有当属性是**全新种类**时，才需在 `BytecodeAttrOpcodes.td` 追加 `BytecodeAttrTag`。本例跳过。若 `foo` 还引入新类型，则同样在 `BytecodeTypeOpcodes.td` 追加 tag。

4. **重建**：`cmake --build build`。构建系统会自动：
   - 跑 `gen-cuda-tile-opcodes`（因 `BytecodeOpcodes.td` 变了）→ 刷新 `StaticOpcodes.inc`，新 `Opcode::FooOp = 0x76` 进枚举与映射，`getVersionToMaxOpcodeMap` 的 13.3 项更新为 `0x76`。
   - 跑 `gen-cuda-tile-bytecode` 与 `gen-cuda-tile-bytecode-reader`（因 `Ops.td` 变了）→ 在 `Bytecode.inc` 新增 `writeFooOp`（含对 `mode` 的版本感知 flags 位），在 `BytecodeReader.inc` 新增 `parseFooOp`（含 `mode` 在老版本下的默认值分支）与 dispatch 的 `case Opcode::FooOp`。
   - `validateAllOperationsHaveOpcodes` 通过（你已登记），`validateAllOpcodeAssignments` 通过（无重复、在公共区间）。

5. **使用点无需手改**：`BytecodeWriter.cpp` 与 `BytecodeReader.cpp` 里的 `#include` 不变——它们用的是「遍历所有 Op」生成的产物，新操作自动被覆盖。

**产出**：一张「新增操作改动清单」：

| 位置 | 是否必须 | 说明 |
|------|----------|------|
| `Ops.td` 新增 `CudaTile_FooOp` | 是 | 定义操作语义、版本、属性 |
| `BytecodeOpcodes.td` 追加 `PublicOpcode` | **是（手动编号）** | 唯一需要人决定数字的一步 |
| `AttrDefs.td` 新增枚举属性 | 视情况 | 仅当引入新属性时 |
| `BytecodeTypeOpcodes.td` / `BytecodeAttrOpcodes.td` | 视情况 | 仅当引入全新类型/属性种类 |
| 读写器 `.cpp` | 否 | 完全自动，无需手改 |

**核心体会**：人工只需做两件事——定义语义、分配一个冻结的 opcode；其余胶水代码（写入函数、读取函数、分派、版本检查、枚举）全部由 tblgen 自动生成并接好。这就是「单一数据源」在字节码层的完整兑现。

**待本地验证**：本实践为源码阅读/推演型，不修改真实源码；若在可构建分支上实操，构建后应在 `build/lib/Bytecode/Writer/Bytecode.inc` 搜到 `writeFooOp`，在 `BytecodeReader.inc` 搜到 `parseFooOp` 与 `case Opcode::FooOp`。

---

## 6. 本讲小结

- 三份 `.td`（`BytecodeOpcodes.td` / `BytecodeTypeOpcodes.td` / `BytecodeAttrOpcodes.td`）是「操作 / 类型 / 属性 → 数值编码」的单一数据源，编码一旦分配即 `FROZEN`、永不重新编号，且新编码只能追加在尾部。
- `BytecodeGen.cpp`（写入端）为每个操作生成 `write<OpName>` 与 `TypeSwitch` 分派，并生成 opcode 枚举、字符串映射、版本→最大 opcode 表；构建期强校验「每个操作必须有 opcode」。
- `BytecodeReaderGen.cpp`（读取端）是其镜像，生成 `parse<OpName>` 与 `switch(opcode)` 分派；多出的职责是「读旧文件时为新字段填默认值」并保护 SSA 编号不被破坏。
- `BytecodeTypeCodeGen.cpp` 处理类型的序列化/反序列化与可选参数位域（13.3 unified bitfield），`BytecodeAttrCodeGen.cpp` 处理 `AttributeTag` 与逐枚举值版本检查。
- 生成的 `.inc` 产物（`Bytecode.inc` / `BytecodeReader.inc` / `StaticOpcodes.inc` / `TypeBytecode.inc` / `AttrBytecode.inc`）经 CMake 的 `tablegen()` 调用产出，被读写器用 `#define GEN_xxx` + `#include` 的 include 技巧切出使用；读写器复用 Writer 目录下的共享 `.inc`。
- 新增一个操作，人工只需「定义语义 + 分配一个冻结 opcode」两步，其余胶水全部自动生成。

---

## 7. 下一步学习建议

- **回到读写器本体**：本讲只讲「胶水怎么来」。要理解这些生成函数在运行时如何被编排进整段字节码，结合 u7-l2（写入器二进制格式）与 u7-l3（读取器反序列化）阅读 `BytecodeWriter.cpp` / `BytecodeReader.cpp` 的手写部分（如 `writeOperation`、`parseOperation`、各 Manager）。
- **版本兼容的纵深**：本讲多次提到 `sinceVersion` 与版本检查。完整版本模型见 u7-l4（字节码版本与兼容性），可对照 `test/Bytecode/versioning/` 下的演进测试，观察 opcode/tag 追加如何被前后向兼容机制吸收。
- **规范生成（姊妹后端）**：tblgen 不只生成字节码胶水，还生成人类可读规范，见下一讲 u8-l2（CUDA Tile IR 规范自动生成），二者共享 `CudaTileOp` 元数据中间层。
- **动手建议**：若你想真正加深理解，可在本地一个实验分支上按综合实践的步骤新增一个仅测试用的操作（`CudaTile_Test_FooOp`，opcode 落 `0x3000+`），构建后用 `cuda-tile-translate` 把含该操作的 MLIR 往返成字节码，亲眼确认自动生成的读写函数工作正常。
