# u5-l1 MLIR 与 ASC Dialect 入门

## 1. 本讲目标

学完本讲，你应该能够：

1. 用自己的话解释 MLIR 的分层中间表示思想，说清 Dialect、Operation、Type、Attribute 四个概念分别对应什么。
2. 读懂 `Dialect.td`、`Base.td`、`Ops.td` 三个文件在 ASC Dialect（本项目内部叫 AscendC Dialect）中各自承担的角色，并理解 TableGen「定义驱动开发」的运作方式。
3. 掌握 ASC Dialect 的 Operation 命名规则（`Dialect_类名_成员函数`），能从一条 IR 操作（如 `ascendc.que_bind.alloc_tensor`）反查出它的 td 定义文件、C++ 类名和 Python 前端的 `create_asc_*` 调用。
4. 独立完成「从 `PYASC_DUMP_PATH` 导出的 codegen.mlir 中挑出操作 → 在 include/ascir/Dialect/Asc/IR 下找到 td 定义」的反查流程，这是阅读后端全部源码的基本功。

本讲是第 5 单元（ASC-IR 后端基础）的第一讲，只建立「地图」：不深入某个具体 Pass 或发射函数，只解决「ASC-IR 长什么样、定义在哪里、名字怎么起」三个问题。

## 2. 前置知识

### 2.1 什么是中间表示（IR）

编译器不会把 Python 一步翻译成机器码，而是先翻译成一种「中间语言」，再层层向下翻译。每一层中间语言叫一级中间表示（Intermediate Representation，IR）。上层的 IR 贴近源语言语义，下层的 IR 贴近目标硬件。pyasc 的编译链路是：

```text
Python 源码 → AST → ASC-IR（MLIR）→ Ascend C 代码 → 毕昇编译 → Kernel 二进制
```

u1-l5 已经讲过这条链路的全貌；本讲进入第三级——ASC-IR。

### 2.2 MLIR 的四个核心概念

MLIR（Multi-Level IR）是 LLVM 社区开源的编译器基础设施，它的核心创新是「多级方言」。四个必须掌握的概念：

| 概念 | 通俗解释 | 在 pyasc 中的例子 |
|------|----------|-------------------|
| **Dialect（方言）** | 一组相关定义的命名空间，名字会成为 IR 文本中的前缀 | `ascendc`（本项目自定义）、`scf`（结构化控制流）、`arith`（算术） |
| **Operation（操作）** | IR 中的「一条指令」，是 MLIR 的原子单位，带操作数、结果和属性 | `ascendc.add_l2`、`ascendc.set_flag` |
| **Type（类型）** | 值的类型，附加在操作数和结果上 | `!ascendc.local_tensor<2048xf32>` |
| **Attribute（属性）** | 编译期常量，挂在 Operation 上，不参与数据流 | `#ascendc<"vecin">` 这样的枚举属性（如 TPosition） |

一段 MLIR 文本大致长这样（摘自项目测试文件，见 4.1.3）：

```mlir
%0 = ascendc.tbuf.get_tensor %arg0 : !ascendc.tbuf<vecin>, !ascendc.local_tensor<?xf32>
```

含义是：对 `!ascendc.tbuf<vecin>` 类型的值 `%arg0` 调用 `get_tensor`，得到一个局部张量。它像函数调用，又像一条指令——这正是 MLIR Operation 的形态。

> **SSA 提示**：`%0`、`%arg0` 这类名字是 SSA（静态单赋值）值的名字，一个值只被赋值一次，之后只能被使用。u2-l3 讲过的 IRHandle 就是 Python 侧对这种 SSA 值的包装。

### 2.3 TableGen 与 .td 文件

TableGen 是 LLVM 的代码生成语言：你在 `.td` 文件里写「声明式定义」，TableGen 工具把它膨胀成成千上万行 C++ 代码（`.inc` 文件）。三个语法要点：

- `def`：定义一个具体记录（一个具体的 Op、Type 或 Attr）。
- `class`：定义模板，可被继承，继承时可以填参数。
- `defm` + `multiclass`：批量实例化——一个 `defm` 展开成多个 `def`，是 ASC Dialect 应对 L0/L1/L2/L3 API 分级的关键武器。

为什么要用 TableGen？因为 Ascend C 类库有上千个 API，每个 API 都要有 IR 定义、C++ 类、pybind 绑定、Ascend C 发射函数。手写四份高度重复的代码不可维护，TableGen 让你「写一份 td，生成四份 C++」。u1-l3 已引入 TableGen 术语，本讲将看到它的完整运作。

### 2.4 与前面讲义的衔接

- u1-l3 建立的目录镜像规律（`python/asc/language` 的 basic/adv/core/fwk 四象限 ↔ `include/ascir/Dialect/Asc/IR` 的同名大写目录）在本讲正式落地。
- u2 系列讲的 `create_asc_AddL2Op`、`asc.TPosition` 等 Python 接口，本讲揭示它们在 td 中的「源头」。
- u3-l4 讲的 Pass 流水线操作的就是本讲定义的这些 IR 节点。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲用途 |
|------|------|----------|
| [include/ascir/Dialect/Asc/IR/Dialect.td](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Dialect.td) | ASC Dialect 的「出生证明」：声明方言名、C++ 命名空间 | 模块 4.2 |
| [lib/Dialect/Asc/IR/Dialect.cpp](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/IR/Dialect.cpp) | Dialect 的 C++ 注册入口（手写部分仅 40 余行） | 模块 4.2 |
| [include/ascir/Dialect/Asc/IR/CMakeLists.txt](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/CMakeLists.txt) | 声明「哪个 td 生成哪份 .inc」的规则表 | 模块 4.2 |
| [include/ascir/Dialect/Asc/IR/Ops.td](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Ops.td) | 全部 Op 定义的汇总入口（一堆 include + 少数公共 Op） | 模块 4.3、4.4 |
| [include/ascir/Dialect/Asc/IR/Base.td](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Base.td) | Op 模板族基类：AscendC_Op、APIOp、L0/L1/L2/L3 系列 | 模块 4.3 |
| [include/ascir/Dialect/Asc/IR/Interfaces.td](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Interfaces.td) | Op 接口层：APIOpInterface 等 | 模块 4.3 |
| include/ascir/Dialect/Asc/IR/Core/、Basic/、Adv/、Fwk/ 四个子目录 | 具体的 Type/Attr/Op 定义，按 Ascend C 类库分类 | 模块 4.4 |
| [test/Dialect/AscendC/IR/types.mlir](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Dialect/AscendC/IR/types.mlir) | 后端 lit 测试，展示了 IR 的真实打印形态 | 模块 4.1、4.4 |
| [lib/TableGen/GenPybindDefs.cpp](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/TableGen/GenPybindDefs.cpp) | 自研 TableGen 后端：生成 pybind 绑定（`create_asc_*` 的来源） | 模块 4.4 |

## 4. 核心概念与源码讲解

### 4.1 MLIR 基础概念：ASC-IR 里的 Dialect 四大件

#### 4.1.1 概念说明

ASC-IR 不是 pyasc 从零发明的数据结构，而是「一个 MLIR 模块」。打开 `PYASC_DUMP_PATH` 导出的 codegen.mlir，你会看到多种前缀混居：

- `ascendc.` 前缀：pyasc 自定义方言（源码中称 AscendC Dialect），每个操作镜像一个 Ascend C API——这是 pyasc 的主体贡献。
- `func.`、`scf.`、`arith.`、`memref.` 前缀：MLIR 自带的通用方言，分别管函数、循环/分支、算术、内存视图。

这就是「多级方言」的价值：pyasc 只需为 Ascend C 专有概念造轮子（tensor、queue、搬运、同步），控制流等通用语义直接复用上游基础设施。u3-l4 讲的 Pass 流水线（如 InsertSync）也只在 `ascendc.` 操作上做文章，通用方言交给 MLIR 标准 Pass 处理。

**Dialect 四大组成**在本项目中的对应：

| MLIR 概念 | ASC Dialect 中的角色 | 定义文件 |
|-----------|---------------------|----------|
| Type | `local_tensor`、`global_tensor`、`tbuf`、`queue`、`matmul` 等类型 | Core/Types.td（u5-l2 精读） |
| Attribute | TPosition、HardEvent、CubeFormat 等枚举属性的镜像 | Core/Attributes.td（u5-l2 精读） |
| Interfaces | APIOpInterface 等统一访问接口 | Interfaces.td 与 Core/Interfaces.td（u5-l3 精读） |
| Operation | 每个 Ascend C API 对应一个（或一族）Op | Basic/Adv/Core/Fwk 下的 Op*.td |

#### 4.1.2 核心流程

一个 `ascendc.` Operation 从定义到使用的生命周期：

```text
.td 中 def 一条记录
   ↓ TableGen（构建期，一次性）
生成 C++ 类（Op 声明/实现）、pybind 绑定、发射声明（.inc 文件）
   ↓ 构建产物 libpyasc.so
Python 前端经 builder.create_asc_XxxOp(...) 创建 Operation 实例
   ↓ 写入 ir.ModuleOp
Pass 流水线加工（u3-l4、第 6 单元）
   ↓ Translation
发射为一条 Ascend C 调用（u6-l5）
```

记住这个链条，后面每个模块都在讲其中一环。

#### 4.1.3 源码精读

ASC Dialect 的真实打印形态可以从后端 lit 测试直接确认。[test/Dialect/AscendC/IR/types.mlir:L13-L23](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Dialect/AscendC/IR/types.mlir#L13-L23) 声明了一组带 `ascendc.` 前缀的张量类型参数：

```mlir
func.func @test_local_tensor(
  %one_dim: !ascendc.local_tensor<15xi32>,
  %one_dim_dynamic: !ascendc.local_tensor<?xf32>,
  ...
```

`!` 开头的是自定义类型语法；`local_tensor<15xi32>` 表示「15 个 int32 元素的 UB 局部张量」，`?` 表示运行时才确定的动态维度（对应 u2-l2 讲过的 ShapeInfo）。

同文件 [test/Dialect/AscendC/IR/types.mlir:L86-L90](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Dialect/AscendC/IR/types.mlir#L86-L90) 则展示了一条完整 Operation：

```mlir
func.func @test_get_tensor_basic(%arg0: !ascendc.tbuf<vecin>) -> !ascendc.local_tensor<?xf32> {
  %0 = ascendc.tbuf.get_tensor %arg0 : !ascendc.tbuf<vecin>, !ascendc.local_tensor<?xf32>
  return %0 : !ascendc.local_tensor<?xf32>
}
```

`ascendc.tbuf.get_tensor` 这一个名字里 packed 了三层信息：方言 `ascendc`、类 `tbuf`（对应 Ascend C 的 TBuf 类）、成员函数 `get_tensor`——这正是本讲模块 4.4 要展开的命名规则。

类型的 td 侧定义预览一例。[include/ascir/Dialect/Asc/IR/Core/Types.td:L26-L40](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Core/Types.td#L26-L40) 定义了 TBuf 类型：

```tablegen
def AscendC_TBuf : AscendC_BaseQueueType<"TBuf", "tbuf"> {
  let description = "Represents AscendC::TBuf";
  let parameters = (ins "TPositionAttr":$tPositionAttr);
  let assemblyFormat = "`<` custom<PrettyTPosition>($tPositionAttr) `>`";
  ...
```

这段代码声明：TBuf 类型携带一个 TPositionAttr 参数，打印成 `<vecin>` 这样的形式（`custom<PrettyTPosition>` 指定美化打印）。所以 `!ascendc.tbuf<vecin>` 里的 `vecin` 不是魔法字符串，而是一个枚举属性值——u2-l4 讲过的 TPosition 枚举在这里进入 IR。

其基类 [include/ascir/Dialect/Asc/IR/Core/Base.td:L17-L24](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Core/Base.td#L17-L24) 表明所有自定义类型都挂在 `AscendC_Dialect` 名下并实现 MemRefElementTypeInterface（可与 memref 通用方言互操作）：

```tablegen
class AscendC_Type<string name, string typeMnemonic, list<Trait> traits = []>
    : TypeDef<AscendC_Dialect, name, [MemRefElementTypeInterface] # traits> {
  let mnemonic = typeMnemonic;
}
```

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：不借助任何运行环境，仅靠阅读 lit 测试认全 Dialect 四大件的真实形态。
2. **操作步骤**：
   - 打开 [test/Dialect/AscendC/IR/types.mlir](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Dialect/AscendC/IR/types.mlir)，通读一遍。
   - 找出至少 3 个 **Type**（如 `local_tensor`、`tbuf`、`queue`、`que_bind`、`pipe`）。
   - 找出至少 1 个 **Operation**（`ascendc.pipe`、`ascendc.tbuf`、`ascendc.tbuf.get_tensor`）。
   - 数一数文件里出现了几种方言前缀（答案：只有 `func` 和 `ascendc` 两种）。
3. **需要观察的现象**：每个 `ascendc.` 类型都带 `!` 前缀；`ascendc.tbuf<gm>` 与 `ascendc.queue<vecin, 101>` 的尖括号内容不同——前者一个枚举参数，后者「枚举 + 深度」两个参数。
4. **预期结果**：能说出「Type 描述值是什么，Operation 描述对值做什么，Attribute 编码编译期常量」。
5. 本实践为纯源码阅读，无需运行验证；若本地已按 u7-l5 构建 devtools，可用 `ascir-opt` 跑该文件验证（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：`!ascendc.local_tensor<?xf32>` 中的 `?` 与 `f32` 分别由 TypeDef 的哪个 parameter 承载？

**答案**：`f32` 是 `elementType`（一个 MLIR Type 参数），`?` 来自 `shape` 参数（`int64_t` 数组中的 `-1`）。两者都在基类 [Core/Base.td:L30-L46](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Core/Base.td#L30-L46) 的 `AscendC_BaseTensorType` 中声明：`parameters = (ins ArrayRefParameter<"int64_t">:$shape, "Type":$elementType)`。

**练习 2**：为什么 pyasc 不需要为 `for` 循环定义 `ascendc.for` 操作？

**答案**：循环是通用控制流语义，MLIR 上游的 `scf` 方言已提供 `scf.for`（u4-l3 讲过 FunctionVisitor 生成它）。ASC Dialect 只镜像 Ascend C 专有概念，遵循「不重复造轮子」的分层原则。

**练习 3**：`ascendc.tbuf.get_tensor` 中 `%arg0` 的类型 `!ascendc.tbuf<vecin>`，`vecin` 在 td 里是什么？

**答案**：是一个 `TPositionAttr` 枚举属性的值。定义在 [Core/Types.td:L26-L40](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Core/Types.td#L26-L40) 的 `let parameters = (ins "TPositionAttr":$tPositionAttr)`，枚举本体在 [Core/Attributes.td:L396-L414](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Core/Attributes.td#L396-L414)（`VECIN = 9` 打印为 `vecin`）。

### 4.2 Dialect.td 与 Dialect.cpp：ASC Dialect 的声明与注册

#### 4.2.1 概念说明

每个 MLIR 方言都要有一份「户籍登记」：方言叫什么名字、C++ 代码放在哪个命名空间、包含哪些组件。这份登记就是 `Dialect.td`；而把登记落到实处（真正向 MLIRContext 注册属性、类型、操作）的是 `Dialect.cpp`。pyasc 的巧妙之处在于：手写的 C++ 只有几十行，其余全部由 TableGen 生成——「定义驱动开发」在这里最直观。

#### 4.2.2 核心流程

ASC Dialect 从声明到可用的步骤：

```text
Dialect.td 声明 AscendC_Dialect（名字 ascendc、命名空间 ::mlir::ascendc）
   ↓ CMake 的 mlir_tablegen 规则（构建期）
生成 AscendCDialect.h.inc / AscendCDialect.cpp.inc（Dialect 类骨架）
   ↓ 编译进 libpyasc.so
进程首次使用 ir.Context 时，MLIR 调 AscendCDialect::initialize()
   ↓ initialize 依次调用
registerAttributes() → registerTypes() → registerOps()
   ↓ 此后
所有 ascendc.* 的 Operation/Type/Attribute 都可创建、打印、解析
```

#### 4.2.3 源码精读

方言声明本体在 [include/ascir/Dialect/Asc/IR/Dialect.td:L16-L35](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Dialect.td#L16-L35)：

```tablegen
def AscendC_Dialect : Dialect {
  let name = "ascendc";
  let summary = "A special dialect to support Ascend C API";
  let cppNamespace = "::mlir::ascendc";
  let useDefaultTypePrinterParser = 1;
  let useDefaultAttributePrinterParser = 1;
  let extraClassDeclaration = [{
    void registerAttributes();
    void registerTypes();
    void registerOps();
  }];
}
```

逐字段解读：

- `let name = "ascendc"`：IR 文本前缀。**注意是 `ascendc.` 而不是 `asc.`**——dump 出的 codegen.mlir 里所有本方言操作都是 `ascendc.add_l2` 这种形式（模块 4.4 会解释 `asc` 缩写出现在哪里）。
- `cppNamespace`：TableGen 生成的所有 C++ 类都放进 `::mlir::ascendc` 命名空间。
- `useDefaultTypePrinterParser`：类型/属性的打印与解析直接复用 TableGen 按 assemblyFormat 生成的默认实现，不必手写。
- `extraClassDeclaration`：向生成的 Dialect 类注入三个注册函数声明——它们在 `.cpp.inc` 中由 TableGen 依据全部 Type/Attr/Op 定义自动生成函数体。

C++ 侧的手写部分极短。[lib/Dialect/Asc/IR/Dialect.cpp:L27-L32](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/IR/Dialect.cpp#L27-L32) 是唯一的逻辑代码：

```cpp
void AscendCDialect::initialize()
{
    registerAttributes();
    registerTypes();
    registerOps();
}
```

而 [lib/Dialect/Asc/IR/Dialect.cpp:L18](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/IR/Dialect.cpp#L18) 直接 include 生成文件：

```cpp
#include "ascir/Dialect/Asc/IR/AscendCDialect.cpp.inc"
```

「哪个 td 生成哪份 .inc」记录在 [include/ascir/Dialect/Asc/IR/CMakeLists.txt:L9-L33](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/CMakeLists.txt#L9-L33)，摘录关键几行：

```cmake
set(LLVM_TARGET_DEFINITIONS Dialect.td)
mlir_tablegen(AscendCDialect.h.inc -gen-dialect-decls -dialect=ascendc)
...
set(LLVM_TARGET_DEFINITIONS Ops.td)
mlir_tablegen(AscendCOps.h.inc -gen-op-decls)
...
set(LLVM_TARGET_DEFINITIONS Core/Attributes.td)
mlir_tablegen(AscendCEnums.h.inc -gen-enum-decls)
```

规则一目了然：`Dialect.td` 生成方言骨架，`Ops.td` 生成全部 Op 的 C++ 类，`Core/Attributes.td` 生成枚举。同一份 [CMakeLists.txt:L41-L48](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/CMakeLists.txt#L41-L48) 还调用 pyasc 自研的两个 TableGen 后端（`-gen-pybind-defs` 生成 Python 绑定、`-gen-opemit-*` 生成 Ascend C 发射代码），它们分别由 u5-l4 精读。

另外，[lib/Dialect/Asc/IR/Dialect.cpp:L38-L43](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/IR/Dialect.cpp#L38-L43) 给方言挂了一个宽松的内联接口（`PermissiveInlinerInterface`），允许任意函数被内联进调用者——这为 u4-l4 讲的「Device 子函数内联」在 IR 层开了绿灯。

#### 4.2.4 代码实践（源码跟踪型）

1. **实践目标**：亲手走通「td 声明 → CMake 规则 → 生成代码 → 注册调用」这条链，确认 TableGen 不是黑盒。
2. **操作步骤**：
   - 在 [Dialect.td](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Dialect.td) 中找到 `extraClassDeclaration` 声明的三个函数名。
   - 在 [Dialect.cpp:L27-L32](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/IR/Dialect.cpp#L27-L32) 找到对它们的调用。
   - 在 [CMakeLists.txt:L9-L11](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/CMakeLists.txt#L9-L11) 找到生成 `AscendCDialect.cpp.inc` 的规则。
   - 若本地做过源码构建（u1-l2），在构建目录中搜索 `AscendCDialect.cpp.inc` 文件并打开，找到 `registerOps` 的生成实现；未构建则记为「待本地验证」。
3. **需要观察的现象**：生成的 `.inc` 中 `registerOps()` 函数体是一长串 `addOperations<...>()`，把每个 Op 类逐个挂到方言上。
4. **预期结果**：理解「改一个 td 文件必须重新跑 CMake 构建，改 .cpp 有时只需重编」的原因——`.inc` 是构建期产物。
5. 构建产物形态随版本变化，未构建环境请以步骤 1-3 的静态阅读为准。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `Dialect.td` 里的 `let name = "ascendc"` 改成别的字符串而不改其他代码，哪些东西会坏？

**答案**：至少三类——(1) IR 文本解析：所有已有的 `.mlir` 测试与 dump 文件里的 `ascendc.` 前缀失效；(2) 自研 TableGen 后端 [GenPybindDefs.cpp:L53-L55](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/TableGen/GenPybindDefs.cpp#L53-L55) 中对 `"ascendc"` 的特判失效，Python 绑定方法名跟着变；(3) CMake 众多 `-dialect=ascendc` 参数失效。这说明方言名是贯穿前后端的强契约。

**练习 2**：`registerAttributes/registerTypes/registerOps` 三个函数的函数体写在哪里？

**答案**：写在 TableGen 生成的 `AscendCDialect.cpp.inc` 里（由 [CMakeLists.txt:L10-L11](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/CMakeLists.txt#L10-L11) 的 `-gen-dialect-defs` 规则生成），`Dialect.td` 的 `extraClassDeclaration` 只提供声明，`Dialect.cpp` 的 `initialize()` 提供调用点。

### 4.3 Base.td 公共模板：从 AscendC_Op 到 L0/L1/L2/L3 模板族

#### 4.3.1 概念说明

ASC Dialect 有上千个 Op 定义，但它们共享大量结构。`Base.td` 把公共结构抽成一层层模板（TableGen class），具体 Op 文件只需一行 `defm` 就能实例化整族操作。这是「定义驱动开发」的精髓：**模板越抽象，新增一个 API 的成本越低**。

Base.td 的贡献分三块：

1. **三个语义 Trait**：标记一个 Op 代表「构造函数调用 / 成员函数调用 / 普通函数调用」，决定发射层能否自动生成代码。
2. **`AscendC_Op` 基类**：规定所有 Op 的统一打印格式、`genEmitter` 开关与 `paramTypeLists` 参数映射表。
3. **APIOp 与 L0/L1/L2/L3 模板族**：把「同名 API 的多种重载形态」批量展开。

其中 L0/L1/L2/L3 是 Ascend C 的 API 分级（u2-l5 引入过）：L0 是最底层的 mask + repeatTimes + repeatParams 形态，L2 是「连续 count 个元素」的便捷形态，L3 是张量运算符重载（如 `operator+`）。

#### 4.3.2 核心流程

一条向量算子定义的展开过程（以 Add 为例）：

```text
Basic/OpVecBinary.td:  defm Add : BinaryTemplateL0123Op<"add", "Add", "operator+">;
   ↓ defm 触发 multiclass 展开
BinaryTemplateL0123Op 内部先 defm BinaryTemplateL012Op（展开 L0/L1/L2 三个 def）
                      再 def L3Op（展开 L3 一个 def）
   ↓ 得到 4 个 TableGen 记录
AddL0Op(mnemonic "add_l0") / AddL1Op("add_l1") / AddL2Op("add_l2") / AddL3Op("add_l3")
   ↓ TableGen 各后端
每个记录再生成：C++ Op 类 + pybind 的 create_asc_AddXOp + 发射函数
```

即一条 `defm` 膨胀为 4 个 Op、约 12 份生成物。设一个 td 文件里有 \( n \) 条 `defm`，每条展开 \( k=4 \) 个变体，则该文件产出 \( n \times k \) 个 Op 定义——这正是 Ascend C 上千 API 能被十几个 td 文件管理的原因。

#### 4.3.3 源码精读

**第一块：三个语义 Trait**，位于 [include/ascir/Dialect/Asc/IR/Base.td:L19-L21](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Base.td#L19-L21)：

```tablegen
def AscConstructor : NativeOpTrait<"AscConstructorTrait">;
def AscMemberFunc : NativeOpTrait<"AscMemberFuncTrait">;
def AscFunc : NativeOpTrait<"AscFuncTrait">;
```

**第二块：AscendC_Op 基类**，[Base.td:L23-L58](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Base.td#L23-L58)，三个关键成员：

```tablegen
class AscendC_Op<string mnemonic, list<Trait> traits = []>
    : Op<AscendC_Dialect, mnemonic, traits> {
  let cppNamespace = "::mlir::ascendc";
  let assemblyFormat = "operands attr-dict `:` qualified(type(operands))";
  bit genEmitter = !foldl(0, traits, init, trait, /* 含三个 Trait 之一则为 1 */);
  list<int> paramTypeLists = [];
}
```

- `assemblyFormat`：统一打印成「操作数 + 属性字典 + `:` + 带限定类型」的形式，所以你 dump 出来的每条 ascendc 操作尾部都有 `: !ascendc.local_tensor<...>, i32` 这样的类型标注。
- `genEmitter`：只要 Op 带三个语义 Trait 之一，发射函数（u6-l5 的「IR → Ascend C 调用」）就可以自动生成，不必手写。
- `paramTypeLists`：与操作实参一一对应的整数表，告诉发射层每个参数如何映射成 Ascend C 的「函数实参 / 模板实参」。注释里给了完整编码表，摘录几个：`0` 普通实参、`3` 枚举模板参数、`4` 常规值模板参数、`-1` 属性不算参数、`2` 从 `LocalTensor<T>` 中抽取元素类型 `T` 作模板参数。

**第三块：APIOp 与模板族**。[Base.td:L64-L73](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Base.td#L64-L73) 定义所有「镜像 Ascend C API」的 Op 的父类：

```tablegen
class APIOp<string mnemonic, string apiName, list<Trait> traits = []>
    : AscendC_Op<mnemonic, [APIOpInterface] # traits> {
  let summary = "Call `AscendC::" # apiName # "` function";
  code extraClassDeclarationBase = [{
    static StringRef getAPIName() { return "}] # apiName # [{"; }
    ...
```

注意 `apiName` 被拼接进 `getAPIName()` 静态函数——发射层凭它知道这条 IR 对应 Ascend C 的哪个库函数（u5-l3 的接口精读会展开）。

L2 模板示例，[Base.td:L166-L171](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Base.td#L166-L171)：

```tablegen
class BinaryTemplateL2Op<string mnemonic, string apiName, list<Trait> traits = []>
    : BinaryOp<mnemonic, apiName, traits> {
  let arguments = (ins AnyType:$dst, AnyType:$src0, AnyType:$src1,
                   AnyType:$calCount, UnitAttr:$isSetMask);
}
```

三目操作 dst/src0/src1 加一个 `calCount`（连续元素数）——与 u2-l5 讲的 `asc.add(z, x, y, count)` L2 用法完全对应。

L3 模板，[Base.td:L196-L201](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Base.td#L196-L201)：

```tablegen
class BinaryL3Op<string mnemonic, string apiName, list<Trait> traits = []>
    : BinaryOp<mnemonic, apiName, [BinaryL3OpInterface] # traits> {
  let summary = "Call `LocalTensor::" # apiName # "` method";
  let arguments = (ins AnyType:$dst, AnyType:$src0, AnyType:$src1);
}
```

L3 没有计数参数，因为它映射的是张量运算符重载（如 `operator+`），长度信息隐含在张量类型里。

两个汇总 multiclass，[Base.td:L221-L231](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Base.td#L221-L231)：

```tablegen
multiclass BinaryL0123Op<string baseMnemonic, string apiName, string l3operator,
                        list<Trait> traits = []> {
  defm "" : BinaryL012Op<baseMnemonic, apiName, traits>;
  def L3Op : BinaryL3Op<baseMnemonic # "_l3", l3operator, traits>;
}

multiclass BinaryTemplateL0123Op<string baseMnemonic, string apiName, string l3operator,
                        list<Trait> traits = []> {
  defm "" : BinaryTemplateL012Op<baseMnemonic, apiName, traits>;
  def L3Op : BinaryL3Op<baseMnemonic # "_l3", l3operator, traits>;
}
```

第三个模板参数 `l3operator` 专门用来填 L3 的运算符名。于是具体算子只需一行——[include/ascir/Dialect/Asc/IR/Basic/OpVecBinary.td:L23](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Basic/OpVecBinary.td#L23)：

```tablegen
defm Add : BinaryTemplateL0123Op<"add", "Add", "operator+">;
```

这一行展开出 4 个 Op，其中 L2 形态最终成为 `asc.add(z, x, y, count)` 在 IR 里的落点：`ascendc.add_l2`。Python 侧的对接可在 [python/asc/language/basic/vec_binary.py:L46-L48](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/basic/vec_binary.py#L46-L48) 看到三个 builder 方法正是按 L0/L1/L2 准备的：

```python
op_impl("add", dst, src0, src1, args, kwargs, builder.create_asc_AddL0Op, builder.create_asc_AddL1Op,
        builder.create_asc_AddL2Op)
```

`create_asc_` 前缀的来历见下一模块。

#### 4.3.4 代码实践（推演验证型）

1. **实践目标**：手推一条 `defm` 的展开结果，验证「td 记录名 → Python builder 方法名」的对应关系。
2. **操作步骤**：
   - 读 [OpVecBinary.td:L23](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Basic/OpVecBinary.td#L23) 的 `defm Add : BinaryTemplateL0123Op<"add", "Add", "operator+">;`
   - 对照 [Base.td:L227-L231](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Base.td#L227-L231)，先展开 `defm "" : BinaryTemplateL012Op<...>`（L0/L1/L2），再展开 `def L3Op`。
   - 写下你预测的 4 个记录名：`AddL0Op`、`AddL1Op`、`AddL2Op`、`AddL3Op`，以及 4 个 mnemonic：`add_l0`、`add_l1`、`add_l2`、`add_l3`。
   - 用 `grep -n "create_asc_Add" python/asc/language/basic/vec_binary.py` 对照步骤 3 的预测（其中 L0/L1/L2 三个方法应出现在同一行）。
3. **需要观察的现象**：预测名与 grep 结果一致；再对 `defm Sub`（同文件 L38）重复一遍。
4. **预期结果**：以后看到任何 `create_asc_XxxOp` 都能立刻推出它的 mnemonic 与所在 td 文件。
5. grep 命令在你的环境可直接运行；若不方便执行，对照本讲引用的 [vec_binary.py:L46-L48](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/basic/vec_binary.py#L46-L48) 源码亦可完成验证。

#### 4.3.5 小练习与答案

**练习 1**：`defm Mul : BinaryTemplateL0123Op<"mul", "Mul", "operator*">;`（OpVecBinary.td L34）会生成哪些 mnemonic？apiName 是什么？

**答案**：生成 `ascendc.mul_l0`、`ascendc.mul_l1`、`ascendc.mul_l2`、`ascendc.mul_l3` 四个操作；`getAPIName()` 全部返回 `"Mul"`，即发射层统一映射到 `AscendC::Mul`（L3 映射到 `operator*`）。

**练习 2**：`paramTypeLists = [3, 0]`（SetFlagOp 中出现）是什么含义？

**答案**：与该 Op 的两个实参一一对应——第 1 个参数（HardEventAttr 枚举）按「非类型模板参数（枚举值）」生成（编码 3），第 2 个参数（eventId）按「普通函数实参」直接传递（编码 0）。见 [Basic/OpBlockSync.td:L47-L52](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Basic/OpBlockSync.td#L47-L52)，编码表在 [Base.td:L42-L57](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Base.td#L42-L57) 的注释。这与 u2-l4 讲的「方向是编译期模板参数、event_id 是运行时实参」精确对齐。

**练习 3**：为什么不把 L0/L1/L2/L3 做成一个带级别参数的 Op？

**答案**：因为不同级别的**参数表本身不同**（L0 有 mask/repeatTimes/repeatParams，L2 只有 calCount），不是同一组操作数的不同取值；TableGen 的 `arguments` 是每个 def 静态固定的，天然适合拆成多个 Op。拆开后还各自挂不同 Interface（如 `BinaryL2OpInterface`），Pass 可以按级别精确匹配。

### 4.4 Operation 命名规则与目录组织：从 IR 名字反查 td 定义

#### 4.4.1 概念说明

本模块是全讲的核心产出：一套**三段式命名规则**和一张**目录对照表**，让你拿到任何一条 ascendc 操作都能在 30 秒内找到它的定义源码。

三段式命名规则（对「类成员函数」型 API）：

\[ \text{td 记录名} = \underbrace{\text{AscendC}}_{\text{方言}} \; \underbrace{\text{\_}}{} \; \underbrace{\text{类名}}_{\text{如 TQueBind}} \; \underbrace{\text{\_}}{} \; \underbrace{\text{成员函数}}_{\text{如 AllocTensor}} \; \underbrace{\text{Op}}{} \]

同一概念在四个世界里各有名字，对照如下：

| 世界 | 名字 | 例子 |
|------|------|------|
| IR 文本（codegen.mlir 里看到的） | `方言.类名.成员函数`（snake_case） | `ascendc.que_bind.alloc_tensor` |
| td 记录（include 下看到的） | `AscendC_类名成员函数Op` | `AscendC_TQueBindAllocTensorOp` |
| C++ 类（lib 下看到的） | `命名空间::类名成员函数Op` | `mlir::ascendc::TQueBindAllocTensorOp` |
| Python 前端（language 下看到的） | `create_asc_` + td 记录名去方言段 | `builder.create_asc_TQueBindAllocTensorOp(...)` |

对「全局函数」型 API（无类），IR 名退化为 `方言.函数名[_级别]`，如 `ascendc.set_flag`、`ascendc.add_l2`、`ascendc.get_block_idx`。

> **易混淆点（务必记住）**：dump 出的 IR 文本前缀是 **`ascendc.`**（来自 Dialect.td 的 `let name = "ascendc"`）；而 Python builder 方法前缀是 **`create_asc_`**——这是因为 pybind 生成器特意把长方言名缩短了。证据见下文 GenPybindDefs.cpp。学习手册前面各讲口语中说的「asc.Add 操作」，落在真实 dump 里就是 `ascendc.add_l2`（L2 形态）。

#### 4.4.2 核心流程

反查一条操作的固定动作：

```text
在 codegen.mlir 里看到 ascendc.que_bind.alloc_tensor
1. 去掉方言前缀 ascendc. → 类名.成员函数 = que_bind.alloc_tensor
2. 类名转 PascalCase：que_bind → QueBind（TQueBind 的 td 类名用 T 前缀，按目录 Fwk/TQue.td 对号）
3. 成员函数转 PascalCase：alloc_tensor → AllocTensor
4. 拼出候选记录名 AscendC_TQueBindAllocTensorOp，在 include/ascir/Dialect/Asc/IR 下 grep
5. 命中 Fwk/TQue.td → 读取 def，顺带得到 apiName "AllocTensor"（即 Ascend C 的 API 名）
6. 需要发射实现时，再去 lib/Target/AscendC 同象限目录找（u6-l5）
```

若第 1 步得到的是无类名形式（如 `add_l2`），则按 `Basic/OpVecXxx.td` 命名习惯直接 grep `defm Add` 或 `"add_l2"`。

#### 4.4.3 源码精读

**（1）目录组织：Ops.td 是总入口。** [include/ascir/Dialect/Asc/IR/Ops.td:L14-L71](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Ops.td#L14-L71) 用几十个 include 把四个子目录的全部定义汇总：

```tablegen
include "Adv/Matmul.td"        // 高阶 API：Matmul 等
...
include "Basic/OpVecBinary.td" // 基础 API：向量双目算子
include "Basic/OpDataCopy.td"  // 基础 API：数据搬运
...
include "Core/Tensor.td"       // 核心：张量对象及其成员
...
include "Fwk/TQue.td"          // 框架：队列/管道
```

四个子目录与 Ascend C 类库、Python 前端的对应关系：

| td 子目录 | 内容 | Ascend C 对应 | Python 前端对应（u1-l3 镜像律） |
|-----------|------|---------------|--------------------------------|
| `Core/` | 类型、枚举属性、接口、张量对象 Op、内存分配器 | 基础对象（Tensor 等） | `asc/language/core/` |
| `Basic/` | 向量算子、搬运、同步、标量等 API Op | Ascend C 基础 API | `asc/language/basic/` |
| `Adv/` | Matmul、激活、归一化等高阶 API | 高阶 API | `asc/language/adv/` |
| `Fwk/` | TPipe/TQue/TBuf 框架对象 | 框架类 | `asc/language/fwk/` |

Ops.td 也定义少量不属于任何类库分类的公共 Op，如 [Ops.td:L84-L105](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Ops.td#L84-L105) 的 `AscendC_ConstructOp`（构造 Ascend C 结构体/枚举对象，u2-l5 讲的 DataCopyParams 参数结构体在 IR 里就是它）。

**（2）类成员函数型命名实例。** [include/ascir/Dialect/Asc/IR/Fwk/TQue.td:L23-L31](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Fwk/TQue.td#L23-L31)：

```tablegen
def AscendC_TQueBindAllocTensorOp : APIOp<"que_bind.alloc_tensor", "AllocTensor"> {
  let summary = "Allocate tensor on queue wrapped buffer";
  let arguments = (ins AscendC_BaseQueueTypeInterface:$queue);
  let results = (outs AscendC_LocalTensor:$tensor);
  ...
```

- mnemonic 是带点的 `"que_bind.alloc_tensor"` → IR 全名 `ascendc.que_bind.alloc_tensor`。
- `apiName` 是 `"AllocTensor"` → 发射为 `xxx.AllocTensor(...)` 形式的成员调用。
- 这条 Op 正是 u2-l6 讲的 `TQue.alloc_tensor()` 在 IR 里的落点。

张量成员函数同理，[include/ascir/Dialect/Asc/IR/Core/Tensor.td:L139-L148](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Core/Tensor.td#L139-L148)：

```tablegen
def AscendC_GlobalTensorSetGlobalBufferOp : APIOp<"global_tensor.set_global_buffer", "SetGlobalBuffer", [AscMemberFunc]> {
  let summary = "Set data buffer of global tensor";
  let arguments = (ins AscendC_GlobalTensor:$tensor,
                       AnyRankedOrUnrankedMemRef:$buffer,
                       Optional<AnyInteger>:$size);
```

u2-l2 讲的 `set_global_buffer` 二段式创建，在 IR 里就是 `ascendc.global_tensor.set_global_buffer`。切片操作则是 [Core/Tensor.td:L128-L137](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Core/Tensor.td#L128-L137) 的 `ascendc.global_tensor.subindex`（assemblyFormat 用 `$tensor `[` $index `]`` 打印成下标样子）。

**（3）全局函数型命名实例。** [include/ascir/Dialect/Asc/IR/Basic/OpBlockSync.td:L47-L52](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Basic/OpBlockSync.td#L47-L52) 的 `SetFlagOp`（mnemonic 无点号）：IR 名 `ascendc.set_flag`，apiName `SetFlag`。[Basic/OpSysVar.td:L29](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Basic/OpSysVar.td#L29) 的 `AscendC_GetBlockIdxOp`：IR 名 `ascendc.get_block_idx`。[Basic/OpDataCopy.td:L83-L88](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Basic/OpDataCopy.td#L83-L88) 的 `AscendC_DataCopyL2Op`：IR 名 `ascendc.data_copy_l2`——01_add 示例中 `asc.data_copy(x_local[...], x_gm[...], tile_length)` 的 L2 搬运形态。

**（4）`create_asc_` 缩写的出处。** [lib/TableGen/GenPybindDefs.cpp:L51-L56](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/TableGen/GenPybindDefs.cpp#L51-L56) 是 pyasc 自研的 pybind 绑定生成器，其中硬编码了缩写规则：

```cpp
os << ".def(\"create_";
auto dialectName = def->getValueAsDef("opDialect")->getValueAsString("name");
if (dialectName == "ascendc") {
    os << "asc";        // 方言名 ascendc 在 Python 方法名中缩短为 asc
} else {
    os << dialectName;
}
os << '_' << name << "\", ...
```

生成的绑定经 [python/src/OpBuilder.cpp:L949](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/src/OpBuilder.cpp#L949) 一行 `#include "ascir/Dialect/Asc/IR/AscOpBindings.h.inc"` 挂进 `PyOpBuilder` 类。于是 Python 世界的 `create_asc_AddL2Op`（u2-l5 首次出现）与 IR 世界的 `ascendc.add_l2`、td 世界的 `defm Add` 三者贯通。

**（5）接口层如何利用命名产物。** [include/ascir/Dialect/Asc/IR/Interfaces.td:L63-L66](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Interfaces.td#L63-L66) 的 `APIOpInterface` 只要求两个方法：`getAPIName` 与 `getComment`——前者正是 APIOp 模板拼进去的。更有行为的是 [Interfaces.td:L77-L99](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Interfaces.td#L77-L99) 的 `DataCopyOpInterface`，其 `getDirection` 依据 dst/src 张量类型推断搬运方向（gm→ubuf / ubuf→gm / gm→gm / ubuf→ubuf），印证了 u2-l5 讲的「data_copy 方向由张量类型组合决定」在 IR 层的实现位置。

#### 4.4.4 代码实践（反查训练型）

1. **实践目标**：独立完成三次「IR 名 → td 定义」反查，形成肌肉记忆。
2. **操作步骤**：以下三个名字来自 01_add 示例会真实生成的操作（对照 [examples/01_add/add.py:L53-L69](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/01_add/add.py#L53-L69) 的 `data_copy`/`set_flag`/`wait_flag`/`add` 调用与 [L31](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/01_add/add.py#L31) 的 `get_block_idx`）：
   - `ascendc.set_flag` → 按无类名规则 grep：`grep -rn "\"set_flag\"" include/ascir/Dialect/Asc/IR/`
   - `ascendc.get_block_idx` → 同上。
   - `ascendc.data_copy_l2` → grep `"data_copy_l2"`。
   - 每命中一个 def，记录：文件路径、td 记录名、apiName、带哪些语义 Trait、paramTypeLists 值。
3. **需要观察的现象**：三个 def 分别落在 Basic/OpBlockSync.td（L47，`[AscFunc]`）、Basic/OpSysVar.td（L29，`[Pure]`）、Basic/OpDataCopy.td（L83，`[AscFunc]`）；SetFlag 的 `paramTypeLists = [3, 0]`，GetBlockIdx 没有显式 paramTypeLists。
4. **预期结果**：产出一行式映射表，例如 `ascendc.set_flag ↔ AscendC_SetFlagOp @ Basic/OpBlockSync.td:47 ↔ AscendC::SetFlag ↔ create_asc_SetFlagOp`。 Trait 差异也值得记一笔：`Pure` 表示无副作用可被优化移动，`AscFunc` 表示发射层可自动生成调用代码。
5. grep 可直接在本仓库运行验证；`create_asc_SetFlagOp` 的存在可再 grep `python/asc/language` 交叉确认（同步接口封装在 basic 目录下）。

#### 4.4.5 小练习与答案

**练习 1**：`ascendc.local_tensor.subindex` 对应哪个 td 记录？它镜像 Ascend C 的什么？

**答案**：对应 `AscendC_LocalTensorSubIndexOp`（[Core/Tensor.td:L297](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Core/Tensor.td#L297) 起），summary 写明镜像 `AscendC::LocalTensor::operator[]`，即张量下标切片；u2-l2 讲的 `t[k:]` 切片在 LocalTensor 侧就落到这里（GlobalTensor 侧是 L128 的 SubIndexOp）。

**练习 2**：为什么 `AscendC_TQueBindAllocTensorOp` 的记录名里有 `TQueBind`，而 IR mnemonic 却是 `que_bind` 而非 `tque_bind`？

**答案**：记录名沿用 Ascend C 类名 `TQueBind`（保留 T 前缀以便与 C++ 类一一对应），而 mnemonic `"que_bind"` 是 td 中手写的显示名（不带 t）。两者都出自 [Fwk/TQue.td:L23](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Fwk/TQue.td#L23) 的 `APIOp<"que_bind.alloc_tensor", "AllocTensor">`——mnemonic 是给人看的 IR 文本，记录名是给生成器的标识，允许不同。

**练习 3**：前端 `builder.create_asc_MulL2Op` 创建的 Operation，其 IR 文本名、apiName、所在 td 文件分别是什么？

**答案**：IR 文本名 `ascendc.mul_l2`；apiName `Mul`（发射为 `AscendC::Mul`）；定义源头是 [Basic/OpVecBinary.td:L34](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Basic/OpVecBinary.td#L34) 的 `defm Mul : BinaryTemplateL0123Op<"mul", "Mul", "operator*">;` 经 multiclass 展开的 L2 分支。

## 5. 综合实践

**任务：制作你的第一张「IR 操作 → td 定义」映射表。**

前置条件：已按 u1-l2 完成源码安装（Model 仿真模式即可，无需 NPU）。

1. **导出 IR**：设置 `PYASC_DUMP_PATH` 环境变量后运行 Add 示例（u1-l4 讲过运行方式）：

   ```bash
   cd examples/01_add
   PYASC_DUMP_PATH=./dump python3 add.py -r Model -v Ascend910B1
   ```

   产物 `dump/` 下应有 codegen.mlir（Pass 前的 ASC-IR）等文件（具体文件名与组织以 u1-l5 讲的四级产物为准；若 dump 未生成，检查环境变量拼写与目录写权限）。

2. **挑选操作**：打开 codegen.mlir，从中挑出 **3 个** `ascendc.` 前缀的操作。建议覆盖两种形态：
   - 类成员函数型：如 `ascendc.global_tensor.set_global_buffer`、`ascendc.global_tensor.subindex`、`ascendc.local_tensor.*`；
   - 全局函数型：如 `ascendc.data_copy_l2`、`ascendc.add_l2`、`ascendc.set_flag`、`ascendc.get_block_idx`。

3. **反查定义**：对每个操作执行 4.4.2 的六步流程，在 `include/ascir/Dialect/Asc/IR/` 下定位 td 定义，填写下表（示例第一行已给出答案，可直接核对）：

   | IR 操作名 | td 文件 | td 记录名 | apiName（Ascend C 侧） | 语义 Trait |
   |-----------|---------|-----------|------------------------|------------|
   | ascendc.set_flag | Basic/OpBlockSync.td | AscendC_SetFlagOp | SetFlag | AscFunc |
   | （你挑的第 1 个） | ... | ... | ... | ... |
   | （你挑的第 2 个） | ... | ... | ... | ... |
   | （你挑的第 3 个） | ... | ... | ... | ... |

4. **交叉验证**：任选表中一个记录名，到 `python/asc/language/` 下 grep 对应的 `create_asc_XxxOp` 调用点，确认「td 定义 → Python 封装」两端对得上。

5. **无构建环境的替代方案（源码阅读型）**：若暂时无法运行示例，改用 [test/Dialect/AscendC/IR/types.mlir](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Dialect/AscendC/IR/types.mlir) 与 [test/Dialect/AscendC/Transforms/](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Dialect/AscendC/Transforms/materialize-tensor.mlir) 目录下的 lit 用例作为操作来源，同样完成三行映射表（此时步骤 3 标注「待本地验证」即可）。

**验收标准**：不看本讲义，能在 1 分钟内说出任一 `ascendc.*` 操作的 td 文件位置与 apiName。

## 6. 本讲小结

- ASC-IR 本质是一个 MLIR 模块：`ascendc.` 自定义方言负责镜像 Ascend C API，`scf`/`arith`/`func`/`memref` 等通用方言负责控制流与算术，各司其职。
- ASC Dialect 由四大件组成：Type（Core/Types.td）、Attribute（Core/Attributes.td，枚举镜像）、Interfaces（Interfaces.td）、Operation（Basic/Adv/Core/Fwk 下的 Op*.td）。
- `Dialect.td` 声明方言（名字 `ascendc`、命名空间 `::mlir::ascendc`），`Dialect.cpp` 的 `initialize()` 调用三个 TableGen 生成的注册函数；CMakeLists.txt 里的 `mlir_tablegen` 规则决定「哪个 td 生成哪份 .inc」。
- `Base.td` 是模板族基座：`AscendC_Op` 统一打印格式并引入 `genEmitter`/`paramTypeLists`；`APIOp` 拼入 `getAPIName()`；`multiclass` 让一行 `defm`（如 `defm Add`）批量展开 L0/L1/L2/L3 四个变体。
- 命名规则三段式 `AscendC_类名_成员函数Op`：IR 文本用 `ascendc.类名.成员函数`（snake_case），Python 前端用 `create_asc_类名成员函数Op`（`asc` 是 GenPybindDefs 对 `ascendc` 的刻意缩写）——**dump 里看到的前缀是 `ascendc.`，不是 `asc.`**。
- 掌握「IR 名 ↔ td 记录 ↔ C++ 类 ↔ create_asc_*」四名合一的反查法，是阅读 Pass（第 6 单元）与发射层源码的前置技能。

## 7. 下一步学习建议

本建立完「地图」，下一讲进入「细节」：

1. **u5-l2 类型与属性定义**：精读 Core/Types.td 中 `AscendC_Matmul` 的十余个参数、Core/Attributes.td 的枚举属性体系，以及 Types.cpp 中手写的自定义打印。本讲 4.1.3 的 TBuf 定义是它的热身。
2. **u5-l3 Operation 定义与 API 接口约定**：展开 APIOpInterface、DataCopyOpInterface 的接口方法，理解「运行时必选-模板必选-运行时可选-模板可选」参数顺序在 IR 中的编码方式（`paramTypeLists` 的完整语义）。
3. **提前浏览**：用本讲的反查法逛一逛 [include/ascir/Dialect/Asc/IR/Basic/](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Basic/OpDataCopy.td) 下你感兴趣的 Op 文件——挑一个你在 examples 里用过的 API，找到它的全部 L0/L1/L2 变体并比较参数表差异，为 u5-l3 做准备。
