# 类型与属性定义：Tensor、TQue 类型与枚举属性

## 1. 本讲目标

本讲是第 5 单元第二讲。上一讲（u5-l1）建立了 ASC Dialect 的「地图」：Dialect/Type/Attribute/Interfaces/Operation 四大件、td 文件组织与「四名合一」反查法。本讲下钻其中两件——**Type（类型）与 Attribute（属性）**——精读：

- `include/ascir/Dialect/Asc/IR/Core/Types.td`：`AscendC_TBuf`、`AscendC_Queue`、`AscendC_LocalTensor/GlobalTensor`、`AscendC_Matmul` 等 TypeDef；
- `include/ascir/API/Types.td`：几百个参数结构体类型的「声明清单」与自动生成管线；
- `include/ascir/Dialect/Asc/IR/Core/Attributes.td`：`TPositionAttr`、`CubeFormatAttr` 等枚举属性如何一一镜像 Ascend C 枚举；
- `lib/Dialect/Asc/IR/Types.cpp`：TableGen 生成不了、必须手写的那部分（自定义 parse/print 与类型注册）。

学完后你应该能够：

1. 读懂一个 TypeDef 的五个组成部分：基类、`parameters`、`assemblyFormat`、`builders`、`extraClassDeclaration`，并能独立写出一个新的；
2. 讲清 `include/ascir/API/Types.td` 里一条记录如何经 `gen-api-typedefs`、`gen-typedef-defs`、`gen-pybind-defs-types` 三条 TableGen 管线分别生成 td 片段、C++ 类和 pybind 绑定；
3. 在 dump 出的 `.mlir` 文本里读懂 `!ascendc.queue<vecin, 2>`、`!ascendc.local_tensor<64xf32>`、`!ascendc.matmul<gm, 0 : i32, f32, false, ...>` 这三类打印，并解释同一行里为什么有的枚举打印成 `vecin`、有的打印成 `0 : i32`；
4. 说出 `AscendC_Matmul` 为什么要带十余个参数，以及这些参数最终如何变成 Ascend C 的 `matmul::Matmul<matmul::MatmulType<...>>` 模板实参；
5. 为一个假想的新缓冲类型 `MyBuf` 写出完整 TypeDef 片段，并列出「让它编译通过」需要在哪些文件登记。

## 2. 前置知识

阅读本讲前，请确认以下概念（前几讲已建立，这里只做一句话回顾）：

- **MLIR Type / Attribute**（u5-l1）：Type 描述「值是什么」（张量、队列、结构体），Attribute 是**编译期常量**，挂在 Operation 的属性表或类型参数上。两者都在 `MLIRContext` 内按内容唯一化（uniquing）——参数相同的 Type 全进程只有一份。
- **TableGen 与 .td 文件**（u1-l3、u5-l1）：用声明式语言描述结构，由 TableGen backend 生成 C++ 代码。一个 `.td` 文件生成什么，由 CMakeLists 里的 `mlir_tablegen` / `tablegen` 规则决定。
- **TPosition / HardEvent**（u2-l4）：Python 侧是 `IntEnum`，IR 侧是 `I32EnumAttr`（本讲的主角之一），最终成为 Ascend C 的模板参数（如 `AscendC::TPosition::VECIN`）。
- **TPipe/TQue/TBuf**（u2-l6）：队列与缓冲的框架化封装。当时说过「pos/depth 等为编译期常量并**编入队列 IR 类型**」——本讲就看这句话在源码里长什么样。
- **LocalTensor/GlobalTensor**（u2-l2）：Python 侧的 Tensor 抽象。本讲看它们在 IR 侧的类型定义与自定义打印。
- **pybind 桥接层**（预告 u5-l5）：`python/src/OpBuilder.cpp` 把 C++ 的 builder 能力暴露给 Python；本讲会提前碰到它的三个类型构造接口。

一个贯穿全讲的直觉：**Ascend C 中「写在尖括号里的模板参数」（`TQue<TPosition::VECIN, 2>`）在 MLIR 里没有对应的语法位置，pyasc 的做法是把它们变成类型的 `parameters`，再由发射层原样拼回 C++ 模板实参。** 理解这一点，四个最小模块就都通了。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲视角 |
|------|------|----------|
| `include/ascir/Dialect/Asc/IR/Core/Base.td` | 类型定义基座：`AscendC_Type`、`AscendC_BaseQueueType`、`AscendC_BaseTensorType` | 所有 TypeDef 的公共模板 |
| `include/ascir/Dialect/Asc/IR/Core/Types.td` | 手写 TypeDef 主战场（TBuf/Queue/QueBind/Matmul/LocalTensor...），末尾 include 自动生成的 `Types.td.inc` | 4.1、4.2、4.4 |
| `include/ascir/API/Types.td` | Ascend C API 类型清单：`APIType` 记录（mnemonic + apiName） | 4.1 的自动生成管线 |
| `include/ascir/API/CMakeLists.txt` | 声明 API/Types.td 的三条生成规则 | 4.1 |
| `include/ascir/Dialect/Asc/IR/Core/Attributes.td` | 枚举属性与非枚举属性（`MatmulConfigAttr`、`APIAttr`）定义 | 4.3 |
| `lib/Dialect/Asc/IR/Attributes.cpp` | `parsePrettyTPosition`/`printPrettyTPosition` 等手写美化打印、属性注册 | 4.2、4.3 |
| `lib/Dialect/Asc/IR/Types.cpp` | `BaseTensorImpl` 手写 parse/print、各 Tensor 类型 get 转发、`registerTypes` | 4.4 |
| `include/ascir/Dialect/Asc/IR/CMakeLists.txt` | 决定「哪个 td 生成哪份 .inc」的规则表 | 4.1、4.3 |
| `lib/TableGen/GenAPITypedefs.cpp`、`GenAPITypes.cpp`、`GenPybindDefsTypes.cpp` | 三条自定义 TableGen backend | 4.1 |
| `python/src/OpBuilder.cpp` | 手写的 `get_queue_type` 等类型构造 pybind 绑定 | 4.2 |
| `python/asc/language/fwk/tpipe.py` | 前端 `TBuf`/`TQue` 构造函数里创建 IR 类型的调用点 | 4.2 |
| `lib/Target/AscendC/CodeEmitter.cpp` | 类型发射：IR 类型 → Ascend C 类型文本 | 4.2、4.4 |
| `test/Dialect/AscendC/IR/types.mlir`、`test/Target/AscendC/matmul.mlir` | 类型 round-trip 与发射的 lit 测试 | 各模块实践 |

## 4. 核心概念与源码讲解

### 4.1 TypeDef：类型定义的两种来源

#### 4.1.1 概念说明

MLIR 里每个方言的类型分两类：

- **无参类型**（static type）：如 `!ascendc.pipe`、`!ascendc.mask`，全局一份，没有参数；
- **参数化类型**（parametric type）：如 `!ascendc.queue<vecin, 2>`，参数（位置、深度）参与唯一化——`queue<vecin, 2>` 与 `queue<vecout, 2>` 是两个不同的 Type 对象。

在 pyasc 中，写一个 TypeDef 有**两条路**：

1. **手写**：在 `Core/Types.td` 里完整写 `parameters`/`assemblyFormat`/`builders`，用于复杂类型（TBuf、TQue、Matmul、LocalTensor 等需要携带结构化参数的）；
2. **自动生成**：在 `include/ascir/API/Types.td` 里只登记「名字 + mnemonic + Ascend C 类名」，由自定义 backend `gen-api-typedefs` 展开成一个最简单的 `AscendC_Type` 定义，用于数量庞大的**参数结构体类型**（`DataCopyParams`、`BinaryRepeatParams` 等上百个）。

这条路分流的开关就藏在 `APIType` 类的一个小逻辑里——模板参数是否为空。

#### 4.1.2 核心流程

两条来源最终汇入同一份生成物，全流程如下：

```text
路线 A（手写，复杂类型）
  Core/Types.td 中的 def AscendC_TBuf : AscendC_BaseQueueType<...> {...}

路线 B（自动生成，简单类型）
  API/Types.td:  def DataCopyParams : APIType<"DataCopyParams">
       │  tablegen(AscIR Types.td.inc -gen-api-typedefs)      ← include/ascir/API/CMakeLists.txt
       ▼
  Types.td.inc:  def AscendC_DataCopyParams : AscendC_Type<"DataCopyParams", "data_copy_params"> {...}
       │  被 Core/Types.td 末尾 include 进来
       ▼
  两条路线汇合于 Core/Types.td
       │  mlir_tablegen(AscendCTypes.h.inc  -gen-typedef-decls)
       │  mlir_tablegen(AscendCTypes.cpp.inc -gen-typedef-defs)   ← IR/CMakeLists.txt
       ▼
  C++ 类 ascendc::TBufType / DataCopyParamsType ...
       │  Types.cpp 的 AscendCDialect::registerTypes()（GET_TYPEDEF_LIST 宏）
       ▼
  注册进方言，可 parse/print、可被 Op 使用
```

另有两条旁支（同一份 `API/Types.td` 派生）：

- `-gen-pybind-defs-types` → `AscTypeBindings.h.inc`：给每个自动生成类型配一个 `get_asc_XxxType` 的 pybind 方法（无参构造）；
- `-gen-api-types` → `Types.h.inc`：生成「IR 类型 → Ascend C 类名」的发射映射宏块（`GEN_EMITTER`）——该文件由 CMake 生成，但**当前仓库源码中未发现被 `#include` 的消费点**（用 grep 检索 `Types.h.inc`/`GEN_EMITTER` 仅命中生成规则本身），用途待确认；不要把它当成在用的链路。

#### 4.1.3 源码精读

**① 类型基座：三个基类**

[Core/Base.td:L17-L20](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Core/Base.td#L17-L20) 定义最普通的类型基类：所有 Ascend C 类型默认实现 `MemRefElementTypeInterface`（允许作为 memref 的元素类型，这是「tensor 的 dtype」能放进 memref 类型系统的前提）。

[Core/Base.td:L22-L24](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Core/Base.td#L22-L24) 在此之上加 `AscendC_BaseQueueTypeInterface`，是 TBuf/Queue/QueBind 的共同基类——后面 Pass（如 UnifyPipe）与发射层可以借此统一识别「队列类」类型。

[Core/Base.td:L30-L46](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Core/Base.td#L30-L46) 是 Tensor 家族基类，要点有三：

- 固定携带 `shape`（`int64_t` 数组参数）与 `elementType` 两个参数；
- `hasCustomAssemblyFormat = 1` + `skipDefaultBuilders = 1`：**声明式 assemblyFormat 被关闭**，parse/print 全部手写（写在 Types.cpp，见 4.4）；
- 预置三个 builder 签名：按 `(shape, elementType)`、只按 `elementType`、按一个已有的 `BaseTensorType` 克隆。

**② 手写路线的最简样本**

无参类型 `AscendC_Mask` 一行基类即可：[Core/Types.td:L66-L68](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Core/Types.td#L66-L68)，IR 文本打印为 `!ascendc.mask`。

带一个元素类型参数的样本 `AscendC_DataCopyPadExtParams`：[Core/Types.td:L40-L44](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Core/Types.td#L40-L44)——`parameters` 声明一个 `Type` 型参数，`assemblyFormat` 声明「`<` 元素类型 `>`」，于是 IR 文本是 `!ascendc.data_copy_pad_ext_params<f32>` 这样的形式。这两个字段合起来就是「参数化类型」的最小完整示例。

**③ 自动生成路线：APIType 类与两条分流**

[API/Types.td:L15-L30](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/API/Types.td#L15-L30) 定义 `APIType` 类，五个字段各司其职：

| 字段 | 含义 |
|------|------|
| `mnemonic` | IR 助记符（如 `data_copy_params`） |
| `apiName` | 对应的 Ascend C 类全名（如 `AscendC::DataCopyParams`） |
| `typeName` | IR C++ 类名，来自模板参数 |
| `genTypedef = !not(!empty(mlirTypeName))` | **模板参数非空才生成 TypeDef** |
| `genEmitter` | 同上，控制是否进入发射映射 |

也就是说：**带模板参数的记录（如 [L137-L140](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/API/Types.td#L137-L140) 的 `def DataCopyParams : APIType<"DataCopyParams">`）自动生成；不带的（如 [L392-L395](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/API/Types.td#L392-L395) 的 `def TBuf : APIType`）不生成任何 TypeDef**，它们在 Core/Types.td 里手写。对照两份文件可以验证这条对应关系是严格 1:1 的：`TBuf`、`TQue`、`TQueBind`、`Matmul`、`GlobalTensor`、`LocalTensor`、`FixpipeParams`、`DataCopyPadExtParams`……每个「不带模板参数的 APIType 记录」都能在 Core/Types.td 找到同名手写定义。

一个体现 `typeName` 决定 C++ 类名的细节：[API/Types.td:L397-L400](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/API/Types.td#L397-L400) 记录名叫 `TBuffAddr`，但模板参数是 `"BufAddr"`，所以生成的定义是 `def AscendC_BufAddr`（C++ 类 `BufAddrType`）。

**④ 展开器：GenAPITypedefs.cpp**

[GenAPITypedefs.cpp:L29-L42](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/TableGen/GenAPITypedefs.cpp#L29-L42) 的全部逻辑只有 13 行：遍历所有 `APIType` 记录，跳过 `genTypedef=0` 的，然后输出：

```text
def AscendC_<typeName> : AscendC_Type<"<typeName>", "<mnemonic>"> {
  let description = "Represents <apiName>";
}
```

**⑤ 生成规则表：两份 CMakeLists**

[include/ascir/API/CMakeLists.txt:L9-L16](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/API/CMakeLists.txt#L9-L16) 声明三条规则（`Types.td.inc`、`Types.h.inc`、`AscTypeBindings.h.inc`）；[include/ascir/Dialect/Asc/IR/CMakeLists.txt:L21-L23](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/CMakeLists.txt#L21-L23) 把 `Core/Types.td`（连同它 include 进来的 `Types.td.inc`）整体交给标准 MLIR backend，生成 `AscendCTypes.h.inc/.cpp.inc`。最后，汇合点在 [Core/Types.td:L187](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Core/Types.td#L187) 的 `include "ascir/API/Types.td.inc"`。

**⑥ pybind 旁支**

[GenPybindDefsTypes.cpp:L35-L47](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/TableGen/GenPybindDefsTypes.cpp#L35-L47) 为每个自动生成类型产出 `.def("get_asc_XxxType", ...)` 片段；这份 `.inc` 被 [python/src/OpBuilder.cpp:L411](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/src/OpBuilder.cpp#L411) 直接 include 进 pybind 注册链——这就是 Python 侧 `builder.get_asc_MaskType()` 一类无参类型构造的来源（同文件 [L384](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/src/OpBuilder.cpp#L384) 附近可见手写与生成的绑定混排）。

#### 4.1.4 代码实践

**实践目标**：亲手验证「两条来源 → 一份 C++」的生成管线，而不依赖运行完整编译。

1. **打开两份清单做交叉对照**。在仓库根目录执行（源码阅读，无需构建）：

   ```bash
   grep -n 'APIType<' include/ascir/API/Types.td | head -20
   ```

   挑三个带模板参数的记录（如 `DataCopyParams`、`BinaryRepeatParams`、`TCubeTiling`），按 4.1.3 ④ 的展开模板**手写**出它们将生成的 td 代码，填入下表：

   | APIType 记录 | 生成的 def 名 | mnemonic | 生成的 C++ 类名 |
   |--------------|---------------|----------|-----------------|
   | `DataCopyParams` | `AscendC_DataCopyParams` | `data_copy_params` | `DataCopyParamsType` |
   | （你挑的） | ... | ... | ... |

2. **若本地已构建过**（`PYASC_SETUP_DEVTOOLS=1` 或保留过 build 目录，见 u1-l2/u7-l5），在构建目录找生成物核对：

   ```bash
   find build -name 'Types.td.inc' -o -name 'AscendCTypes.h.inc' | head
   ```

   打开 `Types.td.inc`，与你手写的展开逐字对比；再打开 `AscendCTypes.h.inc` 搜索 `DataCopyParamsType`，观察标准 backend 生成的类声明（`get` 方法、参数 accessor）。

3. **反向练习**：在 `Core/Types.td` 中找出所有**不是**从 `Types.td.inc` 来的手写 `def AscendC_*`，数一数有多少个（提示：从 L16 的 `AscendC_BaseGlobalTensor` 到 L185 的 `AscendC_TBufPool` 共 14 个，外加 `Types.td.inc` 汇入的一大批）。

**需要观察的现象**：`Types.td.inc` 里的定义全部是最简形态（只有 description，没有 parameters/builders）；而 `AscendCTypes.h.inc` 中同一个类的 C++ 声明却有完整的 `get`/`parse`/`print`——体会「td 定义薄、生成代码厚」的收益。

**预期结果**：能准确说出 `def AscendC_DataCopyParams` 由哪条 CMake 规则的哪个 backend 生成、又被哪条规则消费。若本地无构建环境，第 2 步标注「待本地验证」即可，第 1、3 步纯读源码即可完成。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `def TQue : APIType` 不带模板参数？如果带上会发生什么？

**答案**：TQue 的 IR 类型需要携带 `TPositionAttr` 与 `depth` 两个参数（`!ascendc.queue<vecin, 2>`），而 `gen-api-typedefs` 只会生成**无参**的 `AscendC_Type` 薄定义，无法表达 parameters/assemblyFormat/builders。带上模板参数会导致生成一个无参的 `AscendC_Queue` 定义，与 Core/Types.td 里手写的带参定义冲突。所以规则是：复杂类型不登记模板参数、由 Core/Types.td 手写（[developer_guide.md:L395-L414](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/developer_guide.md#L395-L414) 也把「包含模板参数的 AscendC_Queue」列为需要手动处理的场景）。

**练习 2**：`AscendC_Type` 基类里挂的 `MemRefElementTypeInterface` 有什么用？

**答案**：它允许该类型作为 memref 的元素类型出现。pyasc 的设备指针参数在 IR 里是 memref 形态（u3-l3 的 PointerArgType），`local_tensor`、各 params 结构体类型要能出现在 memref/值类型的位置上，就必须实现该接口。

---

### 4.2 TBuf/TQue 类型：把模板参数编进类型

#### 4.2.1 概念说明

Ascend C 里队列的完整类型是模板：`AscendC::TQue<TPosition::VECIN, 2>`。MLIR 类型系统没有「模板」语法，pyasc 的做法是把这些模板实参变成**类型参数**：

- `AscendC_TBuf`：一个参数 `tPositionAttr`；
- `AscendC_Queue`：两个参数 `tPositionAttr` + `depth`（int64）；
- `AscendC_QueBind`：`srcPositionAttr` + `dstPositionAttr` + `depth`；
- `AscendC_TBufPool`：`tPositionAttr` + `bufIDSize`（uint32）。

好处是：**pos/depth 一旦进入类型，就像 dtype 一样成为 IR 的一部分**——Pass 可以用类型匹配（UnifyPipe、InsertSync 都依赖队列类型里的位置信息），两处使用 `queue<vecin, 2>` 的值天然同型，缓存/查表也免费获得唯一化。代价是：创建、打印、解析、发射四个环节都要把这组参数照顾到，这正是本模块要看的三件套 `builders` / `assemblyFormat` / `extraClassDeclaration`。

还有一个专门概念：**pretty 打印**。枚举属性默认打印成 `9 : i32` 这样没人读得懂的形态；`custom<PrettyTPosition>` 让 TPosition 参数打印成 `vecin` 关键字，需要一对配套的手写 `parsePrettyTPosition`/`printPrettyTPosition` 函数。

#### 4.2.2 核心流程

以 `TQue(TPosition.VECIN, 2)` 为例，从前端到 Ascend C 的完整链路：

```text
Python: TQue(pos=VECIN, depth=2)                    # u2-l6 的前端封装
  │ python/asc/language/fwk/tpipe.py: builder.get_queue_type(pos, depth)
  ▼
pybind: OpBuilder.cpp get_queue_type(position, depth)
  │ ascendc::symbolizeTPosition(uint8) → optional<TPosition>   # 数值 → 枚举
  │ self->getType<ascendc::QueueType>(pos, depth)
  ▼
C++: QueueType::get(ctx, position, depth)           # Types.td 里 TypeBuilder 的函数体
  │ 内部 TPositionAttr::get(ctx, position)          # 枚举 → 属性
  ▼
IR 值类型: !ascendc.queue<vecin, 2>                 # 声明式 assemblyFormat + PrettyTPosition 打印
  │ CodeEmitter::emitAscQueueType
  ▼
Ascend C 文本: AscendC::TQue<AscendC::TPosition::VECIN, 2>
```

反向（读 `.mlir` 文件）时走同一对函数的 parse 侧：`vecin` 关键字经 `parsePrettyTPosition` → `symbolizeTPosition` → `TPositionAttr` 重建类型。lit 测试 `types.mlir` 的「`ascir-opt %s | ascir-opt | FileCheck`」正是靠 parse→print 往返一致来锁定这套格式。

#### 4.2.3 源码精读

**① TBuf：三件套的最小完整样本**

[Core/Types.td:L26-L38](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Core/Types.td#L26-L38) 逐行拆开：

- `parameters = (ins "TPositionAttr":$tPositionAttr)`：类型携带一个枚举属性参数；
- `assemblyFormat`：打印/解析格式——字面 `` `<` ``、一个自定义 directive `custom<PrettyTPosition>($tPositionAttr)`、字面 `` `>` ``，产出 `!ascendc.tbuf<vecin>`；
- `builders` 里的 `TypeBuilder<(ins "TPosition":$position), [{ ... }]>`：提供以**裸枚举** `TPosition` 为入参的便捷 `get`，函数体里 `TPositionAttr::get($_ctxt, position)` 先把枚举包成属性再调 TableGen 生成的底层 `get`（`$_get`/`$_ctxt` 是预置替换变量）；
- `extraClassDeclaration`：往生成的 C++ 类里追加 `getTPosition()` 便捷方法——发射层与 Pass 用它拿回裸枚举，省去 `.getTPositionAttr().getValue()` 两跳。

**② Queue 与 QueBind：参数再进一层**

[Core/Types.td:L157-L171](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Core/Types.td#L157-L171) 的 `AscendC_Queue` 在 TBuf 模式上加 `int64_t` 的 `depth`，assemblyFormat 变为 `` `<` pos `,` depth `>` ``，即 `!ascendc.queue<vecin, 2>`。注意 `depth` 没有包 custom directive，按整数原样打印。

[Core/Types.td:L135-L155](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Core/Types.td#L135-L155) 的 `AscendC_QueBind` 用两个 PrettyTPosition 表达「源位置→目的位置」：`!ascendc.que_bind<gm, vecin, 1>`（真实用例见 [test/Dialect/AscendC/Transforms/diagnostic/verify-sync.mlir:L12](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Dialect/AscendC/Transforms/diagnostic/verify-sync.mlir#L12)）。`TBufPool`（[L173-L185](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Core/Types.td#L173-L185)）同构，只是第二个参数换成 `uint32_t bufIDSize`。

**③ Pretty 打印的手写实现**

[Attributes.cpp:L30-L45](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/IR/Attributes.cpp#L30-L45)：`parsePrettyTPosition` 用 `parseKeyword` 读进一个词，交给枚举生成函数 `symbolizeTPosition` 反查，成功则包成 `TPositionAttr`，失败则报「position is not recognized」；`printPrettyTPosition` 只有一行——`stringifyTPosition(attr.getValue())` 打印小写关键字。这两个函数在 [include/ascir/Dialect/Asc/IR/Asc.h:L44-L45](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Asc.h#L44-L45) 声明，被 assemblyFormat 的 custom directive 按名字约定（`parse<Directive>`/`print<Directive>`）调用。同文件还有结构完全相同的 `PrettyCubeFormat`、`PrettyLayoutMode`、`PrettyCO2Layout`（[L51-L108](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/IR/Attributes.cpp#L51-L108)）——新增 pretty directive 就是照抄这对函数。

**④ Python 侧创建点**

[python/asc/language/fwk/tpipe.py:L554-L566](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/fwk/tpipe.py#L554-L566) 是 `TQue.__init__` 的落地代码：`require_constexpr` 保证 pos/depth 是编译期常量，然后 `builder.get_queue_type(pos, depth)` 拿到 IR 类型、`create_asc_QueueOp(ir_type)` 创建队列操作。TBuf 对应在 [L213-L224](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/fwk/tpipe.py#L213-L224)（`get_buffer_type(pos)`）。pybind 侧的手写绑定在 [python/src/OpBuilder.cpp:L284-L324](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/src/OpBuilder.cpp#L284-L324)：`get_queue_type`/`get_quebind_type`/`get_buffer_type`/`get_tbuf_pool_type` 四个函数每个都先 `symbolizeTPosition` 校验再调 `getType<...>`——这印证了 4.1 的结论：**带参数的复杂类型无法走 `AscTypeBindings.h.inc` 自动绑定，必须在 OpBuilder.cpp 手写**。

**⑤ 发射侧：类型怎么变回 C++ 模板**

[CodeEmitter.cpp:L137-L148](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/CodeEmitter.cpp#L137-L148) 在 `createTypeEmitMapper` 里按 `TypeID` 注册每个 ascendc 类型的发射函数；[L445-L481](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/CodeEmitter.cpp#L445-L481) 的三个实现把类型参数逐个拼回模板实参：

- `emitAscTBufType` → `AscendC::TBuf<` + 位置 + `>`；
- `emitAscQueueType` → `AscendC::TQue<` + 位置 + `, ` + depth + `>`；
- `emitAscQueBindType` → `AscendC::TQueBind<src, dst, depth>`。

其中位置由 [CodeEmitter.cpp:L49-L57](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/CodeEmitter.cpp#L49-L57) 的 `emitTPosition` 逐值转成 `AscendC::TPosition::VECIN` 形态（`ascNamespace` 是定义在 [include/ascir/Target/Asc/CodeEmitter.h:L26](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Target/Asc/CodeEmitter.h#L26) 的字符串常量 `"AscendC"`）。

**⑥ round-trip 测试**

[test/Dialect/AscendC/IR/types.mlir:L44-L62](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Dialect/AscendC/IR/types.mlir#L44-L62) 集中列出带位置的类型样例：`!ascendc.tbuf<gm>`、`!ascendc.tbuf<vecin>`、`!ascendc.que_bind<gm, veccalc, 2>`、`!ascendc.queue<vecin, 101>` 等；文件头部的 `RUN: ascir-opt %s | ascir-opt | FileCheck %s`（[L9](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Dialect/AscendC/IR/types.mlir#L9)）两遍经过解析器，锁定「打印出来还能解析回去」。

#### 4.2.4 代码实践

**实践目标**：在真实 dump 产物中找到队列类型，并沿 4.2.2 的链路各站各对一次源码。

1. **准备 dump**（需要按 u1-l2 装好环境；无 NPU 用 Model 仿真模式即可）：

   ```bash
   cd examples/02_add_framework
   PYASC_DUMP_PATH=./dump python3 add_framework.py -r Model -v Ascend910B1
   ```

2. **观察现象**：打开 `dump/codegen.mlir`，搜索 `queue`，应能看到形如：

   ```text
   ... : !ascendc.queue<vecin, 2> ...
   ... : !ascendc.queue<vecout, 2> ...
   ```

   （深度数值取决于示例配置；若打印形态不同，记录实际文本。）

3. **逐站对照源码**：把你在 dump 里看到的那一行类型，依次在四个文件中找到对应代码并各摘一行：`tpipe.py` 的 `get_queue_type` 调用 → `OpBuilder.cpp` 的 `symbolizeTPosition` → `Types.td` 的 `TypeBuilder` 函数体 → `Types.td` 的 assemblyFormat。若本地还能构建 devtools，可进一步跑：

   ```bash
   ascir-opt dump/codegen.mlir | grep queue | head
   ```

   验证 round-trip（无 devtools 则标注「待本地验证」）。

4. **终点验证**：打开 `dump/ascendc.cpp`，找到 `AscendC::TQue<AscendC::TPosition::VECIN, ...>` 的 C++ 声明，确认 4.2.3 ⑤ 的发射格式。

**预期结果**：一张四列小表（IR 文本 / 前端调用 / pybind 绑定 / C++ 发射文本），每一列都来自真实文件行号。

#### 4.2.5 小练习与答案

**练习 1**：`queue<vecin, 2>` 和 `queue<vecin, 4>` 是同一个类型吗？这带来什么后果？

**答案**：不是。`depth` 是类型参数，参与 MLIR 的类型唯一化，所以它们是两个不同的 Type 对象。后果是：`init_buffer` 时改 depth 会改变后续所有相关值的类型；同时它是编译期信息，会被原样发射成 `TQue<..., 4>` 模板实参（u2-l6 说「pos/depth 编入队列 IR 类型」的源码依据即此）。

**练习 2**：如果把 `AscendC_Queue` 的 assemblyFormat 里 `custom<PrettyTPosition>($tPositionAttr)` 改成 `$tPositionAttr`，IR 文本会变成什么样？

**答案**：变成默认属性打印，形如 `!ascendc.queue<9 : i32, 2>`（数值 + 类型），可读性变差但语义等价。Matmul 类型里的 CubeFormat/LayoutMode 参数就是这种默认打印（见 4.4.3 与 [test/Target/AscendC/matmul.mlir:L19](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Target/AscendC/matmul.mlir#L19)），两种风格在同一个项目里并存。

---

### 4.3 枚举 Attribute：Attributes.td 如何镜像 Ascend C 枚举

#### 4.3.1 概念说明

u2-l4 讲过枚举的传递路径是「Python IntEnum → IR I32EnumAttr → C++ 模板参数」，本模块看中间那一环的定义处。`Core/Attributes.td` 里绝大多数内容是 `I32EnumAttr`（少数 `I64EnumAttr`）：每条 `I32EnumAttrCase` 声明「枚举名 → 整数值 → 可选的字符串拼写」。TableGen 的 `-gen-enum-decls/-gen-enum-defs` 会为每个枚举生成一组 C++ 设施：

- `enum class TPosition : uint8_t { GM, A1, ... };`
- `stringifyTPosition(TPosition)`：枚举 → 字符串（打印用）；
- `symbolizeTPosition(...)`：字符串或数值 → `std::optional<TPosition>`（解析与 pybind 校验用）；
- 包装类 `TPositionAttr`（由 `-gen-attrdef-*` 生成），让枚举可以作为属性/类型参数存在。

三个设计约定值得注意：

1. **名字与取值必须与 Ascend C 保持一致**（[developer_guide.md:L646](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/developer_guide.md#L646) 明确要求），这是「一一镜像」原则在属性层的体现；
2. `underlyingType = "uint8_t"`：枚举值在 IR 属性里按 8 位无符号存储，省空间；
3. 除枚举外，还有少量**结构化属性**（参数袋），如 `MatmulConfigAttr`、`APIAttr`，用 `struct(params)` 打印。

#### 4.3.2 核心流程

一个枚举属性从定义到四类消费点：

```text
Core/Attributes.td: def AscendC_TPositionAttr : I32EnumAttr<...>
   │  mlir_tablegen(AscendCEnums.h/.cpp.inc   -gen-enum-decls/-gen-enum-defs)
   │  mlir_tablegen(AscendCAttributes.h/.cpp.inc -gen-attrdef-decls/-gen-attrdef-defs)
   ▼
C++: enum class TPosition + TPositionAttr（Types.cpp/Attributes.cpp include 这些 .inc）
   ├─→ 作为类型参数：TBuf/Queue/Matmul 的 parameters（4.2、4.4）
   ├─→ 作为 Op 属性：如 SetFlagOp 的 HardEvent（u2-l4）
   ├─→ Pretty 打印：parsePrettyTPosition/printPrettyTPosition（4.2.3 ③）
   └─→ Ascend C 发射：stringify + 大写 → "AscendC::MaskMode::NORMAL" 形态
```

注册入口是 `AscendCDialect::registerAttributes()`（[Attributes.cpp:L114-L120](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/IR/Attributes.cpp#L114-L120)），与类型的 `registerTypes()` 结构相同——都是把宏生成的列表整个塞进方言。

#### 4.3.3 源码精读

**① 属性基类**

[Core/Attributes.td:L19-L23](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Core/Attributes.td#L19-L23) 定义 `AscendC_Attr`：`AttrDef` 的薄封装，固定命名空间 `::mlir::ascendc` 与 mnemonic。枚举属性不走它（直接继承 MLIR 的 `I32EnumAttr`），只有 `APIAttr`、`MatmulConfigAttr`、`SampleAttr` 等结构化属性走它。

**② TPositionAttr：类型参数用的枚举**

[Core/Attributes.td:L396-L414](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Core/Attributes.td#L396-L414) 定义 13 个取值：`GM/A1/A2/B1/B2/C1/C2/CO1/CO2`（矩阵组）与 `VECIN/VECOUT/VECCALC`（矢量组），每个都带小写第三参数作为 IR 拼写（`vecin` 等），`MAX` 是哨兵。u2-l4 讲过的 TPosition 语义（逻辑位置映射 UB/L1/L0）在这里落成 IR 数据。

**③ CubeFormatAttr 与 LayoutModeAttr：Matmul 的格式枚举**

[Core/Attributes.td:L147-L160](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Core/Attributes.td#L147-L160) 的 `CubeFormat`（nd/nz/zn/zz/nn/nd_align/scalar/vector 共 8 种）与 [L271-L281](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Core/Attributes.td#L271-L281) 的 `LayoutMode`（none/normal/bsngd/sbngd/bngs1s2）是 Matmul 类型每个操作数都要携带的两组枚举（见 4.4）。注意它们**没有 pretty directive**，所以在 IR 文本里打印为整数。

**④ HardEventAttr：全量方向枚举**

[Core/Attributes.td:L190-L231](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Core/Attributes.td#L190-L231) 列出 36 个「生产者_消费者」方向（`MTE2_V`、`V_MTE3`、`MTE3_MTE2`……），覆盖 u1-l4 双缓冲流水里用到的三方向及更多组合——它是 `set_flag/wait_flag` Op 的属性类型，也为 InsertSync Pass（u6-l3）自动选方向提供依据。

**⑤ 结构化属性两个样本**

- [L50-L64](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Core/Attributes.td#L50-L64) `AscendC_APIAttr`：五个参数（group/name/pipe_in/pipe_out/args，后三个可选），`assemblyFormat = "`<` struct(params) `>`"`——这是 Op 侧「API 元数据」属性的形态，u5-l3 讲 AscendCOpInterface 时会再用到它；
- [L292-L344](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Core/Attributes.td#L292-L344) `AscendC_MatmulConfigAttr`：约 50 个 `OptionalParameter`（do_norm/basic_m/basic_n/.../batch_out_mode），全部可选、按 struct 打印——它就是 Matmul 类型最后一个参数（4.4）。

**⑥ 枚举值进 Ascend C 的方式**

发射层常直接用 stringify + 大写拼出 C++ 枚举引用。例如 [lib/Target/AscendC/Common.cpp:L48-L56](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/Common.cpp#L48-L56) 中 `AscendC::MaskMode::` + `stringifyEnum(op.getMode()).upper()` 生成 `AscendC::MaskMode::NORMAL`。也就是说枚举链路的终点是把 IR 的 `normal` 拼写还原成 Ascend C 的 `NORMAL`——**td 里第三参数拼写要与 Ascend C 对得上，发射才能还原**。

**⑦ 生成规则**

[include/ascir/Dialect/Asc/IR/CMakeLists.txt:L29-L33](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/CMakeLists.txt#L29-L33)：`Core/Attributes.td` 同时派生两对 .inc（枚举对 + 属性对）。对比 L21-L23 的类型规则可以记住规律：**一个 td 文件按 backend 拆成多份 .inc，每份 .inc 对应一类生成物**。

#### 4.3.4 代码实践

**实践目标**：用同一段真实 IR 文本对比「pretty 打印」与「默认打印」两种枚举形态，加深对 custom directive 的理解。

1. 打开 [test/Target/AscendC/matmul.mlir:L19](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Target/AscendC/matmul.mlir#L19)，观察一个 `!ascendc.matmul<...>` 类型的完整打印。你会发现**同一个类型里**：
   - TPosition 打印为 `gm`（pretty）；
   - CubeFormat 打印为 `0 : i32`（默认）；
   - LayoutMode 打印为 `0 : i32`（默认）；
   - MatmulConfig 打印为 `<do_norm = true, ...>`（struct）。
2. 对照 4.3.3 的定义，解释差异来源：`gm` 来自 `custom<PrettyTPosition>`；`0 : i32` 是因为 assemblyFormat 里写的是裸 `$cubeFormatAAttr`。
3. 动手环节（可纯文本完成）：为 `CubeFormatAttr` 写出 pretty 版本的四个改动点——① assemblyFormat 中替换为 `custom<PrettyCubeFormat>($cubeFormatAAttr)`；② 在 `Asc.h` 声明 `parsePrettyCubeFormat/printPrettyCubeFormat`；③ 在 `Attributes.cpp` 实现它们（照抄 [L51-L66](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/IR/Attributes.cpp#L51-L66)——**这对函数其实已经存在**，所以只需第 ① 步真正改 td）；④ 更新 matmul 相关 lit 测试的 CHECK 行。注意第 ③ 步是「验证性发现」：`parsePrettyCubeFormat` 早已实现却没被类型的 assemblyFormat 引用，这是一个观察源码事实的好机会。
4. 若本地可运行 lit（构建了 devtools，见 u7-l5），跑 `ascir-opt` 解析 matmul.mlir 验证现状；否则标注「待本地验证」。

**预期结果**：能说清「为什么同一个项目里枚举打印有两种风格」以及统一成 pretty 需要动哪几处。

#### 4.3.5 小练习与答案

**练习 1**：`underlyingType = "uint8_t"` 改成 `"int32_t"` 会有什么影响？

**答案**：枚举在 C++ 里的底层类型与存储宽度会变化（`enum class TPosition : int32_t`），属性编码也随之变宽。对于已定义枚举这是破坏 IR 兼容性的改动（数值布局虽不变、但生成代码全变）；除非枚举取值超过 255（如 `QuantMode` 用了 `uint32_t`，见 [Core/Attributes.td:L453-L457](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Core/Attributes.td#L453-L457)），否则按 Ascend C 侧的底层类型选择即可。

**练习 2**：`I32EnumAttrCase` 第三个字符串参数（如 `"vecin"`）不写会怎样？

**答案**：TableGen 会用枚举名自动生成拼写（如 `VECIN`）。TPosition 手动给小写拼写是为了让 IR 文本更贴近 Ascend C 文档里的惯用小写（`TPosition::VECIN` 在 Ascend C 是大写，但 IR 侧统一小写助记符），也避免打印出全大写。CubeFormat 等给了小写第三参数（`nd/nz/...`）但没接 pretty directive，所以这些拼写只影响 `stringify` 的结果、暂不体现在类型打印里。

---

### 4.4 Matmul 类型与 Types.cpp 手写部分

#### 4.4.1 概念说明

**为什么 Matmul 要带十余个参数？** Ascend C 的矩阵乘类型本身就是一个「参数包」：

```cpp
matmul::Matmul<MatmulType<A 位置, A 格式, A 数据类型, A 转置, A Layout>,
               MatmulType<B 位置, B 格式, B 数据类型, B 转置, B Layout>,
               MatmulType<C 位置, C 格式, C 数据类型, C 转置, C Layout>,
               MatmulType<Bias 位置, Bias 格式, Bias 数据类型>,
               MatmulConfig>
```

A、B、C 三个操作数各带一组「位置/格式/类型/转置/Layout」五元组（Bias 少转置与 Layout），再外挂一个几十项的 `MatmulConfig`。这些**全部是模板参数**——运行期不能变，所以 IR 侧必须把它们全部编进 `AscendC_Matmul` 类型的 17 个参数里（16 个逐项参数 + 1 个 config 属性）。这就是「十余个参数」的设计原因：**类型即模板实参包，IR 类型完整保留 Ascend C 模板的全部静态信息，发射时才能原样还原**。

**Types.cpp 手写部分解决什么？** Tensor 家族（Local/Global/BaseTensor）在 Base.td 里关掉了声明式 assemblyFormat（`hasCustomAssemblyFormat = 1`），因为它的打印格式要模仿 MLIR 内置 tensor 的形态（`<15xi32>`、`<?xf32>`、`<*xf16>`——形状列表拼元素类型，`?` 表动态维，`*` 表无秩）。这类「维数列表 + 动态标记 + 无秩」解析逻辑用声明式格式写不出来，于是四个 Tensor 类型共用一份手写的 CRTP 模板 `BaseTensorImpl`。

#### 4.4.2 核心流程

**Matmul 类型的生命周期**：

```text
Python: register_matmul(...) → builder.get_matmul_type(16+ 个实参)
  │ OpBuilder.cpp: symbolizeTPosition/CubeFormat/LayoutMode 逐个校验
  │             50 项 bool/int 打包成 MatmulConfigAttr
  ▼
C++: MatmulType::get(ctx, srcA, fmtA, typeA, isTransA, layoutA, ...， config)
  │   （Types.td 的 TypeBuilder 函数体把裸枚举包成 Attr）
  ▼
IR: !ascendc.matmul<gm, 0 : i32, f32, false, 0 : i32, gm, ..., <do_norm = true, ...>>
  │ CodeEmitter::emitAscMatmulType（含 config → MatmulConfig 发射）
  ▼
Ascend C: matmul::Matmul<matmul::MatmulType<AscendC::TPosition::GM, CubeFormat::ND,
                       float, false, LayoutMode::NONE>, ..., CFG>
```

**Tensor 类型的解析/打印**（手写部分）：

```text
读 ".mlir" → BaseTensorImpl::parse
  parseLess → 尝试 '*'（无秩）或维数列表（'?' 记为动态维）→ parseType → parseGreater
打印 → BaseTensorImpl::print
  空形状打 "*x"；每维打 "N" 或 "?" 并补 "x"；最后拼元素类型
```

#### 4.4.3 源码精读

**① Matmul 的参数表**

[Core/Types.td:L72-L83](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Core/Types.td#L72-L83) 列出 17 个参数，按 A（5 项）→ B（5 项）→ C（5 项）→ Bias（位置/格式/类型 3 项）→ `matmulConfig` 排列，与上面 Ascend C 原型逐项对应。

**② Matmul 的三件套**

- assemblyFormat（[L84-L90](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Core/Types.td#L84-L90)）：17 个参数依次排列，位置用 pretty，其余默认打印——这就是 4.3.4 看到的 `matmul<gm, 0 : i32, f32, false, 0 : i32, ...>` 长串的来源；
- builders（[L91-L113](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Core/Types.td#L91-L113)）：入参用**裸枚举 + bool + Type**，函数体逐个 `TPositionAttr::get`/`CubeFormatAttr::get`/`LayoutModeAttr::get` 包成属性再调 `$_get`——与 TBuf 的 builder 同一模式，只是数量膨胀到 16 行；pybind 侧的手写对应物在 [python/src/OpBuilder.cpp:L326-L383](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/src/OpBuilder.cpp#L326-L383)（50 项 config 逐项打包）；
- extraClassDeclaration（[L114-L126](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Core/Types.td#L114-L126)）：为每个位置/格式/Layout 参数生成取回裸枚举的便捷方法（`getSrcAPosition()`、`getCubeFormatB()`...），发射层 `emitAscMatmulType`（[CodeEmitter.cpp:L155-L157](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/CodeEmitter.cpp#L155-L157) 注册，[L738-L777](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/CodeEmitter.cpp#L738-L777) 附近实现四组操作数的取用）正是靠这些方法拼出 C++ 模板实参。

**③ Matmul 类型的真实 IR 文本与发射结果**

[test/Target/AscendC/matmul.mlir:L19](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Target/AscendC/matmul.mlir#L19) 的函数签名里是完整的 `!ascendc.matmul<...>` 打印（含 `<do_norm = true, ...>` 的 config struct）；[L28](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Target/AscendC/matmul.mlir#L28) 的 CHECK 行则是发射出的 Ascend C 形态 `constexpr static MatmulConfig CFG{...}; matmul::Matmul<matmul::MatmulType<AscendC::TPosition::GM, CubeFormat::ND, float, false, LayoutMode::NONE>, ...>`——两端逐 token 对应，是「类型即模板实参包」的最好注脚。

**④ BaseTensorImpl：手写 parse/print**

[Types.cpp:L28-L94](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/IR/Types.cpp#L28-L94) 是一个 CRTP 模板，四个静态方法解决「构造、解析、打印、克隆」：

- [L45-L66](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/IR/Types.cpp#L45-L66) `parse`：先吃 `<`；`parseOptionalStar` 尝试 `*`（无秩），否则 `parseDimensionList` 读维数（自动把 `?` 记为动态维）；再读元素类型与 `>`。读不出维数也不是 `*` 时报「either dimension list ... or '*' symbol ... must be declared」；
- [L68-L84](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/IR/Types.cpp#L68-L84) `print`：形状为空打 `*x`，否则逐维打数字或 `?` 并统一补 `x`，最后拼元素类型——产出 `local_tensor<*xf16>`、`local_tensor<58x?x78x900x?xi16>` 这类文本（样例全在 [types.mlir:L12-L42](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Dialect/AscendC/IR/types.mlir#L12-L42)）；
- [L86-L94](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/IR/Types.cpp#L86-L94) `cloneWith`/`hasRank`：实现 `ShapedTypeInterface`（形状工具）。

**⑤ 四份转发样板**

[Types.cpp:L199-L217](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/IR/Types.cpp#L199-L217) 是 `LocalTensorType` 的五个成员函数，每个都只有一行——转交给 `BaseTensorImpl<LocalTensorType>`。同结构的还有 `GlobalTensorType`（L169-L193）、`BaseGlobalTensorType`（L100-L130）、`BaseLocalTensorType`（L136-L163）：**模板吃掉共性，每个具体类型只留转发**。这是「手写部分」的真实体量——不算多，但缺了它四种 Tensor 类型都无法读写 IR 文本。

**⑥ 注册收口**

[Types.cpp:L223-L229](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/IR/Types.cpp#L223-L229) 的 `registerTypes()` 用 `GET_TYPEDEF_LIST` 宏（来自 `AscendCTypes.cpp.inc`）把**全部**类型一次性 add 进方言——手写的、自动生成的、参数化的，在这里不再有区别。`#define GET_TYPEDEF_CLASSES` + include（[L18-L19](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/IR/Types.cpp#L18-L19)）是 MLIR 生成代码的标准消费姿势。

#### 4.4.4 代码实践

**实践目标**：把一条真实 Matmul 类型逐参数「翻译」回 Ascend C，验证 17 参数与模板实参的对应关系。

1. **取材**：优先用示例 dump（03/04 示例需 `PYASC_DUMP_PATH`，Model 模式即可；MIX 示例在仿真器上的可用性以 examples/README 为准，跑不通则直接用测试文件替代）；无环境时直接精读 [test/Target/AscendC/matmul.mlir:L19](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Target/AscendC/matmul.mlir#L19) 的类型文本。
2. **填表**：把该类型的 17 个参数抄进下表前两列，第三列写对应的 Ascend C 模板实参（对照 [L28](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Target/AscendC/matmul.mlir#L28) 的 CHECK）：

   | # | IR 打印 | Ascend C 实参 | 来源 |
   |---|---------|----------------|------|
   | 1 | `gm` | `AscendC::TPosition::GM` | PrettyTPosition |
   | 2 | `0 : i32` | `CubeFormat::ND` | CubeFormatAttr(值 0=ND) |
   | 3 | `f32` | `float` | emitType |
   | 4 | `false` | `false` | bool 直印 |
   | 5 | `0 : i32` | `LayoutMode::NONE` | LayoutModeAttr |
   | ... | （A 组完） | B 组同构重复 | |
   | 17 | `<do_norm = true, ...>` | `MatmulConfig CFG{...}` | struct 属性 |

3. **校验工具**：对照 [Core/Attributes.td:L147-L160](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Core/Attributes.td#L147-L160) 与 [L271-L281](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Core/Attributes.td#L271-L281) 把每个 `0 : i32`/`1 : i32` 翻译回枚举名——`0`→ND/NONE、`1`→NZ/NORMAL，以此类推。
4. **延伸（可选）**：数一数 `MatmulConfigAttr` 的参数个数（[L292-L342](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Core/Attributes.td#L292-L342)），并与 L28 CHECK 里 `MatmulConfig CFG{1,0,0,...}` 的花括号项数对比，确认一一对应。

**预期结果**：一张完整的三列对照表；今后在 dump 里再看到 `!ascendc.matmul<...>` 长串，能逐段读出 A/B/C/Bias 的位置、格式与转置配置。

#### 4.4.5 小练习与答案

**练习 1**：`local_tensor<*xf16>` 里的 `*` 是什么意思？为什么 Add 示例 dump 里常见的是 `*x` 而不是具体形状？

**答案**：`*` 表示无秩（unranked）——类型不携带形状信息（解析逻辑见 [Types.cpp:L51-L61](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/IR/Types.cpp#L51-L61) 的 `parseOptionalStar` 分支）。pyasc 前端 LocalTensor 只记录 dtype 与字节长度，形状是运行期概念（u2-l2 的 ShapeInfo/TensorShape 之分），所以 dump 出的 tensor 类型多为无秩或动态维（`?`）形态；`local_tensor<64xf32>` 这类带形状的形态多出现在 Pass 物化或手写测试里。

**练习 2**：如果让你新增第五种 Tensor 类型 `ScatterTensor`，`BaseTensorImpl` 需要改吗？

**答案**：不需要。`BaseTensorImpl` 是 CRTP 模板，只要新类型继承 `AscendC_BaseTensorType`（自动获得 shape/elementType 参数与 builder 声明）并在 Types.cpp 里照抄 LocalTensorType 那五段一行转发即可；parse/print/cloneWith 的共性全部由模板承担。这正是模板基座 + 转发样板分层的好处。

**练习 3**：Matmul 的 `isTransA` 是 `bool` 参数而不是 `BoolAttr` 属性，这样设计有什么便利？

**答案**：TableGen 对原生 C++ 类型参数（bool/int64_t/uint32_t）会自动做「存储 + 比较 + 打印」，参数表更简洁，builder 入参也可直接传 C++ 标量；只有需要复用枚举设施（stringify/symbolize）或结构化打包（如 MatmulConfigAttr 的 50 个可选项）时才包成 Attr。TBuf 的 `depth`、TBufPool 的 `bufIDSize` 同理。

## 5. 综合实践

**任务：为假想的新缓冲类型 `MyBuf` 走一遍完整的类型定义流程（纸面设计 + 登记清单）。**

设定需求：`MyBuf` 是一种带逻辑位置和固定容量（字节数）的缓冲，Ascend C 侧假想原型为 `AscendC::MyBuf<TPosition pos, uint32_t capacity>`。

### 第一步：写出 TypeDef 片段（示例代码，非项目原有）

仿照 TBuf/TBufPool（4.2.3），在 `Core/Types.td` 中应写入：

```tablegen
def AscendC_MyBuf : AscendC_BaseQueueType<"MyBuf", "my_buf"> {
  let description = "Represents AscendC::MyBuf";
  let parameters = (ins "TPositionAttr":$tPositionAttr,
                        "uint32_t":$capacity);
  let assemblyFormat = [{
    `<` custom<PrettyTPosition>($tPositionAttr) `,` $capacity `>`
  }];
  let builders = [
    TypeBuilder<(ins "TPosition":$position, "uint32_t":$capacity), [{
      return $_get($_ctxt, TPositionAttr::get($_ctxt, position), capacity);
    }]>,
  ];
  let extraClassDeclaration = [{
    TPosition getTPosition() { return getTPositionAttr().getValue(); }
  }];
}
```

自检三个问题：为什么继承 `AscendC_BaseQueueType`（想让 Pass/发射把它当队列族处理；若不想，改继承 `AscendC_Type`，如 `AscendC_Mask`）？为什么 TPosition 用 Attr 参数而 capacity 用裸 `uint32_t`（前者要复用枚举设施与 pretty 打印，后者只是整数）？`extraClassDeclaration` 里那个方法给谁用（发射层与 Pass 免两跳取枚举）？

### 第二步：说明这 13 行会生成什么 C++ 代码

由 [include/ascir/Dialect/Asc/IR/CMakeLists.txt:L21-L23](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/CMakeLists.txt#L21-L23) 的两条既有规则（**无需改 CMake**）：

- `AscendCTypes.h.inc`：类 `ascendc::MyBufType` 声明——`get(MLIRContext*, TPositionAttr, uint32_t)`（TableGen 底层 get）、builder 生成的 `get(TPosition, uint32_t)`（内联你写的函数体）、`getTPositionAttr()`/`getCapacity()` 参数 accessor、`classof/TypeID` 识别、assemblyFormat 对应的 parse/print（`custom<PrettyTPosition>` 处会调用你在 Asc.h/Attributes.cpp 提供的 `parsePrettyTPosition/printPrettyTPosition`——**这两个已存在，直接复用**）；
- `AscendCTypes.cpp.inc`：上述方法的定义、按参数的 uniquing 存储与哈希/相等比较；
- `GET_TYPEDEF_LIST` 宏自动把 `MyBufType` 加进 [Types.cpp:L223-L229](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/IR/Types.cpp#L223-L229) 的注册列表——**registerTypes 无需改**。

### 第三步：登记清单（让它真正编译并可用）

| # | 文件 | 改什么 | 依据 |
|---|------|--------|------|
| 1 | `include/ascir/Dialect/Asc/IR/Core/Types.td` | 第一步的 TypeDef | 手写复杂类型的唯一入口（developer_guide 的 [Type类型定义介绍](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/developer_guide.md#L579-L617)） |
| 2 | `include/ascir/API/Types.td` | （可选）`def MyBuf : APIType { mnemonic = "my_buf"; apiName = "AscendC::MyBuf"; }`，**不带模板参数**以免与 #1 冲突 | 4.1 的分流规则；TBuf/TQue 均如此登记 |
| 3 | `python/src/OpBuilder.cpp` | 手写 `.def("get_mybuf_type", ...)`（`symbolizeTPosition` 校验 + `getType<MyBufType>`） | 带参类型不能自动 pybind（[developer_guide.md:L395-L414](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/docs/developer_guide.md#L395-L414)；参照 [OpBuilder.cpp:L306-L314](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/src/OpBuilder.cpp#L306-L314) 的 get_buffer_type） |
| 4 | `lib/Target/AscendC/CodeEmitter.cpp`（+ `include/ascir/Target/Asc/CodeEmitter.h`） | `createTypeEmitMapper` 注册 `emitTypeMapper[TypeID::get<ascendc::MyBufType>()]`；实现 `emitAscMyBufType` 打印 `AscendC::MyBuf<AscendC::TPosition::XXX, N>` | [CodeEmitter.cpp:L137-L172](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/CodeEmitter.cpp#L137-L172) 与 [L445-L461](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/CodeEmitter.cpp#L445-L461) 的 TBuf 样板 |
| 5 | `test/Dialect/AscendC/IR/types.mlir` | 加一行 `%my: !ascendc.my_buf<vecin, 128>` 与对应 CHECK | round-trip 实践（[types.mlir:L44-L62](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Dialect/AscendC/IR/types.mlir#L44-L62) 的写法） |
| 6 | （可选）`python/asc/language/fwk/tpipe.py` | 仿 [L213-L224](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/fwk/tpipe.py#L213-L224) 的 TBuf 包装一个 Python 类 | 前端封装 |
| 7 | 重建 | `python3 -m pip install -e .`（改了 C++ 必须 重编译，u1-l2） | — |

**验收标准**：不看讲义，能对同伴讲清「这 13 行 td 经过哪两条 CMake 规则、生成了哪两份 .inc、各自含什么；为什么第 3、4 步无法自动生成」。

## 6. 本讲小结

- ASC Dialect 的类型有**两条定义来源**：复杂类型（TBuf/TQue/QueBind/Matmul/Tensor 家族）在 `Core/Types.td` 手写完整 TypeDef；上百个参数结构体类型只在 `API/Types.td` 登记名字，由 `gen-api-typedefs` 展开成薄定义汇入同一份生成管线——分流开关是 `APIType` 的模板参数是否为空。
- 一个参数化 TypeDef 的五件套：基类（继承谁决定接口与族属）、`parameters`（模板实参编入类型）、`assemblyFormat`（IR 文本形态）、`builders`（裸枚举/标量 → Attr 的便捷 get）、`extraClassDeclaration`（取回裸枚举的快捷方法）。
- 枚举属性用 `I32EnumAttr` 一一镜像 Ascend C 枚举（名字与取值要求一致），生成 `enum class` + `stringify/symbolize` + Attr 包装；`custom<PrettyTPosition>` 让类型里的枚举打印成 `vecin` 关键字，靠 `Attributes.cpp` 里成对的 parse/print 手写函数支撑——没有接 pretty 的枚举（CubeFormat/LayoutMode）就打印成 `0 : i32`。
- `AscendC_Matmul` 的 17 个参数是「类型即模板实参包」的极致样本：A/B/C/Bias 四组位置/格式/类型/转置/Layout 加 MatmulConfig，发射层逐项还原成 `matmul::Matmul<matmul::MatmulType<...>, ...>`。
- `Types.cpp` 的手写部分集中在 Tensor 家族：`BaseTensorImpl` CRTP 模板实现 `<15xi32>`/`<?xf32>`/`<*xf16>` 形态的 parse/print/cloneWith，四个具体类型只留一行转发；`registerTypes()` 用宏把全部类型（手写+生成）统一注册。
- 同一类型定义会派生多份 .inc：`-gen-typedef-decls/-gen-typedef-defs` 出 C++ 类、`-gen-enum-*` 出枚举、`-gen-attrdef-*` 出属性、`-gen-pybind-defs-types` 出 Python 绑定——「哪个 td 生成哪份 .inc」永远以 CMakeLists 为准。

## 7. 下一步学习建议

1. **u5-l3 Operation 定义与 API 接口约定**：类型是「静态信息」，Operation 才是「动作」。下一讲展开 `Basic/OpVecBinary.td` 的 `defm Add : BinaryTemplateL0123Op`、`Core/Interfaces.td` 的 `AscendCOpInterface`（`getAPIName`/`getComment`），以及 `paramTypeLists` 如何编码「运行时必选-模板必选-运行时可选-模板可选」的参数顺序——本讲的类型与枚举属性都会作为 Op 的参数类型再次登场。
2. **回头印证**：带着本讲知识重读 u2-l6 的 TPipe/TQue 一节，确认「pos/depth 编入队列 IR 类型」这句话如今能在源码里指出具体行（`tpipe.py` L564 → `OpBuilder.cpp` L285 → `Types.td` L164）。
3. **顺路浏览**：`include/ascir/Dialect/EmitAsc/IR/CMakeLists.txt` 里也有同款的 `-gen-typedef-decls/-gen-typedef-defs` 规则——EmitAsc 方言（u6-l6）的类型体系与本讲同构，提前看一眼可以在第 6 单元省一次概念开销。
