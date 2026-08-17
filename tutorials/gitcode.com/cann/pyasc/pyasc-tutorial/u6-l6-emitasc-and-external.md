# u6-l6 EmitAsc 方言与外部方言降级

## 1. 本讲目标

上一讲（u6-l5）我们走完了「ASC-IR → Ascend C 源码」的发射主线：`Translation.cpp` 的 `emitOperation` 白名单分发、`CodeEmitter` 的命名栈与作用域。但那份白名单里除了 `ascendc::*` 一大批算子，还有几组「外来户」：

- `emitasc::*` —— pyasc 自己定义的第二个方言；
- `func::*`、`scf::*`、`arith::*`、`math::*`、`memref::*`、`emitc::*` —— MLIR 上游通用方言。

本讲回答三个问题：

1. 为什么 pyasc 需要一个 EmitAsc 中间方言，而不是把所有东西都塞进 Asc 方言、或直接在 Python 里拼 C 代码字符串？
2. `lib/Target/AscendC/External/` 下六个文件（Arith/Math/Scf/Func/MemRef/Emitc）如何把通用 MLIR 方言操作翻译成 C 表达式？
3. 一个 Python `for` 循环，如何走完「FunctionVisitor → `scf.for` → C `for` 语句」的全链路？

学完本讲，你应该能独立读懂 dump 出的 `ascir.mlir` 里任何一条非 `ascendc.` 前缀的操作，并说出它最终在 `ascendc.cpp` 里生成的 C 代码形态。

## 2. 前置知识

- **SSA 与可变变量的对立**：MLIR 中每个值只赋值一次（Static Single Assignment），循环携带变量靠「块参数 + yield」表达；而 C 是命令式语言，变量可以反复赋值。本讲最重要的算法——`scf.for` 发射——本质上就是「SSA → 可变变量」的机械翻译。
- **memref 充当指针**：发射层用 `memref<?xT>` 表示 C 指针 `T*`，用 `memref<1xT>` 表示「单个可变变量」。这个约定来自上游 emitc 方言，EmitAsc 沿用。
- **上游 emitc 方言**：MLIR 官方有一个 `emitc` 方言，专门承载「贴近 C 但还没落到具体 API」的操作（如 `emitc.include`、`emitc.verbatim`）。pyasc 同时使用上游 `emitc` 与自研 `emitasc`，两者分工见 4.1。
- **LogicalResult / FAIL_OR**：发射函数返回 `LogicalResult`，`FAIL_OR(expr)` 宏在失败时立刻 `return failure()`（[include/ascir/Target/Asc/Common.h:44-46](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Target/Asc/Common.h#L44-L46)），失败沿调用链逐层上传，最终在 Python 侧变成异常（u6-l5 已讲）。
- **块参数（BlockArgument）**：区域的入口值。`scf.for` 的归纳变量与迭代参数就是 body 块的块参数。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [include/ascir/Dialect/EmitAsc/IR/Dialect.td](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/EmitAsc/IR/Dialect.td) | 声明 `emitasc` 方言（名字、命名空间） |
| [include/ascir/Dialect/EmitAsc/IR/Ops.td](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/EmitAsc/IR/Ops.td) | 12 个 EmitAsc 操作的 TableGen 定义 |
| [include/ascir/Dialect/EmitAsc/IR/Types.td](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/EmitAsc/IR/Types.td) | `PyStruct` 类型定义 |
| [include/ascir/Dialect/EmitAsc/IR/Attributes.td](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/EmitAsc/IR/Attributes.td) | `KernelArgument` 枚举属性（`emitasc.kernel_arg`） |
| [lib/Dialect/EmitAsc/IR/Dialect.cpp](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/EmitAsc/IR/Dialect.cpp) | 方言初始化与宽松内联接口注册 |
| [lib/Target/AscendC/EmitAsc.cpp](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/EmitAsc.cpp) | 12 个 EmitAsc 操作的发射实现 |
| [lib/Target/AscendC/Translation.cpp](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/Translation.cpp) | 白名单 `PrintableOpTypes` 与 `emitOperation` 分发 |
| [lib/Target/AscendC/External/Func.cpp](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/External/Func.cpp) | `func.func/call/return` → C 函数骨架 |
| [include/ascir/Target/Asc/External/Arith.h](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Target/Asc/External/Arith.h) + [lib/Target/AscendC/External/Arith.cpp](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/External/Arith.cpp) | `arith.*` → C 算术表达式 |
| [lib/Target/AscendC/External/Scf.cpp](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/External/Scf.cpp) | `scf.for/if/while/...` → C 控制流（本讲主角） |
| [lib/Target/AscendC/External/Emitc.cpp](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/External/Emitc.cpp) | 上游 `emitc.*` → `#include`、逐字代码 |
| [lib/Target/AscendC/External/MemRef.cpp](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/External/MemRef.cpp) | `memref.*` → 数组声明与下标访问 |
| [lib/Target/AscendC/External/Math.cpp](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/External/Math.cpp) + [include/ascir/Target/Asc/External/Math.h](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Target/Asc/External/Math.h) | `math.*` → 数学函数调用 |
| [test/Target/AscendC/emitasc.mlir](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Target/AscendC/emitasc.mlir)、[arith.mlir](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Target/AscendC/arith.mlir)、[scf.mlir](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Target/AscendC/scf.mlir) | lit + FileCheck 黄金用例（实践素材） |
| [python/asc/language/core/ir_value.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/ir_value.py)、[ops.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/ops.py)、[struct.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/struct.py) | 前端创建 EmitAsc 操作的三个入口 |

## 4. 核心概念与源码讲解

### 4.1 EmitAsc 方言：为「C 形状」概念专设的桥下方言

#### 4.1.1 概念说明

回顾 u5-l1 的结论：Asc 方言的设计原则是「一条 Op 镜像一条 Ascend C API」。但前端实现过程中会遇到一批**没有 Ascend C API 对应物**的需求：

- `x + offset`（`GlobalAddress` 加整数）——这是 C 的指针算术 `ptr + n`，不是任何 Ascend C 接口；
- 访问 Struct 的字段 `s.field`、给字段赋值——C 的成员访问 `.`/`->`；
- 声明一个 Struct 的 C 结构体（`#pragma pack` 那段）；
- 按名字调用任意 C++ 函数（比如 `AscendC::Add` 之外的辅助函数）；
- 把一段现成 C 代码逐字塞进生成结果（逃生舱）。

这些操作有两个共同点：**语义上是 C 语法**，且**必须参与 IR 数据流**（它们的输入输出是 SSA 值，要被后续操作引用、被 Pass 分析）。如果把它们做成 Python 里直接拼字符串，就绕过了 IR——无法校验、无法被 Pass 改写、无法参与支配分析。如果塞进 Asc 方言，又会污染「Asc 方言 = Ascend C API 镜像」这条干净的分层原则。

pyasc 的解法是定义第二个小方言 **EmitAsc**（"C++ emission support dialect"），专门承载这些「贴近 C 语法、但不属于任何 Ascend C API」的操作。它与上游 `emitc` 方言分工：`emitc` 提供通用的 `include`/`verbatim`（由 External/Emitc.cpp 发射），`emitasc` 提供 pyasc 特有的 `PyStruct`、指针算术、kernel 参数属性等。

#### 4.1.2 核心流程

EmitAsc 操作有三个生产者、一个消费者：

```text
生产者 1: Python 前端（JIT 编译期）
  GlobalAddress.__add__   → emitasc.ptr_offset
  asc.inline(...)         → emitasc.verbatim
  Struct 字段读写/本地副本 → emitasc.member / set_member / copy_struct
                                    │
生产者 2: Transforms Pass                       │
  DeclarePyStructPass     → emitasc.declare_py_struct
  LegalizeKernelArgs      → emitasc.kernel_arg 属性（打在 func 参数上）
                                    │
                                    ▼
            ir.ModuleOp（与 ascendc.* 操作混居同一模块）
                                    │
消费者: lib/Target/AscendC/EmitAsc.cpp 的 12 个 printOperation
        （经 Translation.cpp 白名单分发）
```

注意：EmitAsc **没有独立的降级 Pass**。它不是「Asc → EmitAsc → C」两跳，而是「各类操作（ascendc/emitasc/scf/arith/...）并列 → 统一发射」一跳。EmitAsc 的价值在于把 C 形状的概念**沉淀为可复用、可校验的 IR 节点**，而不是引入一层中间转换。

#### 4.1.3 源码精读

**方言声明**。[Dialect.td:16-30](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/EmitAsc/IR/Dialect.td#L16-L30) 定义了名为 `emitasc` 的方言，`cppNamespace` 为 `::mlir::emitasc`，并通过 `extraClassDeclaration` 预告了 `registerAttributes/registerTypes/registerOps` 三个注册函数——它们在 [lib/Dialect/EmitAsc/IR/Dialect.cpp:25-30](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/EmitAsc/IR/Dialect.cpp#L25-L30) 的 `initialize()` 中依次调用（内容全部由 TableGen 生成）。

同一个文件还注册了**宽松内联接口**：[Dialect.cpp:36-40](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/EmitAsc/IR/Dialect.cpp#L36-L40) 调用 `registerExternalModels`，为方言挂上 `PermissiveInlinerInterface`——三个 `isLegalToInline` 重载全部返回 `true`（[include/ascir/Dialect/Utils/Inlining.h:26-38](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Utils/Inlining.h#L26-L38)）。这对应 u4-l4 讲过的「Device 子函数内联」：子函数体里含 `emitasc.*` 操作时，内联合法性检查直接放行。

**12 个操作一览**。[Ops.td:22-25](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/EmitAsc/IR/Ops.td#L22-L25) 先定义基类 `EmitAsc_Op`，随后逐个声明：

| Op（IR 名） | 定义位置 | 对应 C 语义 |
| --- | --- | --- |
| `emitasc.call_opaque` | [Ops.td:27-39](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/EmitAsc/IR/Ops.td#L27-L39) | 按名字调用任意 C++ 函数 `callee(args)` |
| `emitasc.copy_struct` | [Ops.td:41-46](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/EmitAsc/IR/Ops.td#L41-L46) | 在设备侧新建结构体并逐字节拷贝 |
| `emitasc.declare_py_struct` | [Ops.td:48-52](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/EmitAsc/IR/Ops.td#L48-L52) | 声明 `#pragma pack(8)` 结构体 |
| `emitasc.dereference` | [Ops.td:54-59](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/EmitAsc/IR/Ops.td#L54-L59) | 一元 `*ptr` |
| `emitasc.member` | [Ops.td:61-67](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/EmitAsc/IR/Ops.td#L61-L67) | `base.field`（读成员） |
| `emitasc.member_ptr` | [Ops.td:69-84](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/EmitAsc/IR/Ops.td#L69-L84) | `&base->member`（取成员地址） |
| `emitasc.member_ref` | [Ops.td:86-101](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/EmitAsc/IR/Ops.td#L86-L101) | `base->member`（取成员引用） |
| `emitasc.set_member` | [Ops.td:103-108](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/EmitAsc/IR/Ops.td#L103-L108) | `base.field = value` |
| `emitasc.ptr_offset` | [Ops.td:110-122](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/EmitAsc/IR/Ops.td#L110-L122) | `ptr + n`（实现 ViewLike 接口、可折叠） |
| `emitasc.reinterpret_cast` | [Ops.td:124-131](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/EmitAsc/IR/Ops.td#L124-L131) | `reinterpret_cast<T>(x)` |
| `emitasc.variable` | [Ops.td:133-165](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/EmitAsc/IR/Ops.td#L133-L165) | 定义可变变量 `T v[1]{init}` |
| `emitasc.verbatim` | [Ops.td:167-170](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/EmitAsc/IR/Ops.td#L167-L170) | 逐字输出代码串（支持 `$0` 占位替换） |

两个值得注意的细节：

- `ptr_offset` 带 `DeclareOpInterfaceMethods<ViewLikeOpInterface>` 且 `hasFolder = 1`——它是一个规范的 View 操作，能被 MLIR 的通用折叠机制处理；
- `variable` 的 builder 接受 `OpFoldResult`（[Ops.td:145-160](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/EmitAsc/IR/Ops.td#L145-L160)），把「编译期常量初始化」与「运行时值初始化」两种来源统一成静态属性或动态操作数——这是「一个 Op 两种姿态」的典型 TableGen 手法。

**类型与属性**。[Types.td:22-25](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/EmitAsc/IR/Types.td#L22-L25) 定义唯一的类型 `PyStruct`：三个参数（结构体名、字段类型数组、字段名数组）全部编进类型，打印为 `!emitasc.py_struct<...>`。这正是 u3-l3 讲过的「Struct 三面体」中 IR 侧那一面。

[Attributes.td:25-41](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/EmitAsc/IR/Attributes.td#L25-L41) 定义 `KernelArgument` 枚举（`Explicit=0` / `FftsAddr=1`）与 `KernelArgumentAttr`。回忆 u6-l4：`LegalizeKernelArgs` 给 kernel 形参打 `emitasc.kernel_arg` 属性——[lib/Dialect/Asc/Transforms/LegalizeKernelArgs.cpp:35-56](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/Transforms/LegalizeKernelArgs.cpp#L35-L56) 就是生产端；Python 侧的 [python/src/IR.cpp:65-83](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/src/IR.cpp#L65-L83)（`getKernelArgAttrs`）是消费端，Launcher 据此决定参数 ABI 中谁是显式参数、谁是隐藏的 `ffts_addr`。**一个枚举属性把后端标记传到运行时，EmitAsc 方言在这里充当了「属性命名空间」**。属性名字符串常量定义在 [include/ascir/Dialect/EmitAsc/Utils/Attributes.h:19](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/EmitAsc/Utils/Attributes.h#L19)。

**前端如何创建这些操作**（三个入口，全部在 JIT 编译期）：

1. 指针加法：[ir_value.py:46-51](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/ir_value.py#L46-L51) 中 `GlobalAddress.__add__` 先把偏移量经 `arith.index_cast` 转成 `index` 类型，再创建 `emitasc.ptr_offset`。你在 01_add 里写的 `x + offset`，落进 IR 就是这一条。

```python
offset_index = builder.create_arith_IndexCastOp(offset.to_ir(), builder.get_index_type())
handle = builder.create_emitasc_PtrOffsetOp(self.to_ir(), offset_index)
```

2. 逐字代码逃生舱：[ops.py:17-29](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/ops.py#L17-L29) 的 `asc.inline(code, args)` 把一段 C 代码包成 `emitasc.verbatim`，可选地把若干 IR 值绑到 `$0`、`$1` 占位符上；`before_function=True` 时还会把插入点临时挪到函数头部再还原。

3. Struct 三件套：[struct.py:194-198](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/struct.py#L194-L198)（`__getattrjit__` 读字段 → `emitasc.member`）、[struct.py:200-209](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/struct.py#L200-L209)（`__setattrjit__` 写字段 → `emitasc.set_member`）、[struct.py:242-245](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/struct.py#L242-L245)（`create_local` → `emitasc.copy_struct`）、[struct.py:222-229](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/struct.py#L222-L229)（`get_ir_type` → `emitasc.py_struct` 类型）。

这些 `create_emitasc_*` 方法不在 TableGen 自动生成之列，而是手写在 [python/src/OpBuilder.cpp:810-832](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/src/OpBuilder.cpp#L810-L832)（`create_emitasc_CopyStructOp/MemberOp/PtrOffsetOp/SetMemberOp/VerbatimOp`）与 [OpBuilder.cpp:386-393](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/src/OpBuilder.cpp#L386-L393)（`get_emitasc_PyStructType`）——因为 EmitAsc 的 Op 太少，手写比走 `-gen-pybind-defs` 管线更省事。

**Pass 侧的生产者**。[DeclarePyStructPass.cpp:65-90](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Dialect/Asc/Transforms/DeclarePyStructPass.cpp#L65-L90)：收集全模块出现过的 `PyStructType`，去重后用 `ImplicitLocOpBuilder` 在模块体开头逐个 `builder.create<emitasc::DeclarePyStructOp>(pyStruct)`。于是「声明结构体」这个动作就以 IR 节点形式固化在模块头部，发射层只管照打。

#### 4.1.4 代码实践

**实践目标**：亲手制造一条 `emitasc.verbatim`，观察它从 Python 到 C 的完整旅程。

**操作步骤**：

1. 复制 `examples/01_add/add.py` 为 `add_inline.py`（放在任意可运行目录）。
2. 在 kernel 函数体第一行插入一句（**示例代码**）：

```python
asc.inline("// === hello from python frontend ===")
```

3. 设置 `PYASC_DUMP_PATH=/tmp/pyasc_dump`，在 Model 模式下运行。
4. 打开导出的 `codegen.mlir` 与 `ascendc.cpp`。

**需要观察的现象**：

- `codegen.mlir` 中 kernel 入口附近出现 `emitasc.verbatim "// === hello from python frontend ==="` 一条 IR；
- `ascendc.cpp` 中对应函数体内出现一行一模一样的注释。

**预期结果**：两处内容一致，证明 verbatim 是「先入 IR、再被发射」而非直接字符串拼接；若传 `args=(某个张量句柄,)` 并在 code 里写 `$0`，dump 中可见操作数被绑定。**待本地验证**（本讲义写作环境未运行 NPU/仿真器）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `emitasc.ptr_offset` 要实现 `ViewLikeOpInterface`，而 `emitasc.verbatim` 不需要？

**答案**：`ptr_offset` 语义上是「在既有指针上做视图偏移」，产生的新 memref 是原 memref 的视图，MLIR 的 View 体系（及配套的折叠 `hasFolder`）能对它做规范化，比如偏移为 0 时折叠回原值；`verbatim` 是逐字文本，没有值语义，不参与任何规范化。

**练习 2**：`emitasc.kernel_arg` 属性挂在什么操作上？谁读它？

**答案**：挂在 kernel `func.func` 的形参上（`LegalizeKernelArgs` 打标，含末尾追加的 `ffts_addr` 隐藏参数）；由 Python 侧 `getKernelArgAttrs`（python/src/IR.cpp:65-83）读出，交给 Launcher 决定参数打包顺序。

**练习 3**：EmitAsc 方言为什么注册 `PermissiveInlinerInterface`？

**答案**：Device 子函数会被内联进 kernel（u4-l4）。内联合法性按方言接口检查，若 EmitAsc 未声明允许内联，含 `emitasc.*` 的子函数体就无法被搬进 kernel 函数，整条内联链路会断。

### 4.2 EmitAsc.cpp：十二个操作的发射实现

#### 4.2.1 概念说明

`lib/Target/AscendC/EmitAsc.cpp` 是 EmitAsc 方言的消费者，为 12 个操作各写一个 `printOperation` 重载。它与 u6-l5 讲过的 Asc 发射完全同构：同一个 `CodeEmitter`、同一套 `emitAssignPrefix`/`getOrCreateName` 原语，只是服务对象换成了 C 形状操作。

#### 4.2.2 核心流程

每个发射函数的套路固定：

1. 若操作有结果 → `emitAssignPrefix` 先打印 `T vN = `（[CodeEmitter.cpp:347-363](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/CodeEmitter.cpp#L347-L363)，内部委托 `emitVariableDeclaration` [CodeEmitter.cpp:334-345](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/CodeEmitter.cpp#L334-L345)）；
2. 打印操作符或函数形态，操作数一律经 `getOrCreateName` 取唯一变量名；
3. 分号由外层 `emitOperation` 统一补（[Translation.cpp:291](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/Translation.cpp#L291)）。

#### 4.2.3 源码精读

**接线**：12 个 `emitasc::*` 操作登记在白名单元组的 EmitAsc 段——[Translation.cpp:93-96](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/Translation.cpp#L93-L96)，紧挨着上游 `emitc`（L76）、`func`（L78）、`scf`（L80）、`memref`（L82）、`arith`（L84-89）、`math`（L91-92）各段。`emitOperation`（[Translation.cpp:271-293](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/Translation.cpp#L271-L293)）用 `TypeSwitch` 把操作分派到对应重载。**这印证了本讲的核心图景：ascendc、emitasc 与六个通用方言在发射层完全平权。**

**四个代表性实现**：

1. **成员访问的 `.` 与 `->` 二选一**（[EmitAsc.cpp:64-75](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/EmitAsc.cpp#L64-L75)）：`member` 操作根据 `base` 是不是 `MemRefType`（即指针）决定打印 `->` 还是 `.`。C 里两套语法的选择被编码成一次类型判断。

2. **结构体声明**（[EmitAsc.cpp:95-110](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/EmitAsc.cpp#L95-L110)）：`declare_py_struct` 打印 `#pragma pack(push, 8)`、逐字段 `类型 名字;`、`#pragma pack(pop)`。与前端 ctypes 侧的 `_pack_ = 8`（u3-l3、u6-l4）成对，保证 Host 打包与设备侧布局逐字节对齐。

3. **可变变量**（[EmitAsc.cpp:164-187](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/EmitAsc.cpp#L164-L187)）：`variable` 打印 `T v[shape]{init};`——静态初始化直接打属性值，动态初始化打操作数变量名。注意 IR 里它是 `memref<1xT>`，发射成「长度为 1 的数组」，绕开 C 标量与 SSA 单赋值的冲突。

4. **verbatim 的占位符替换**（[EmitAsc.cpp:189-228](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/EmitAsc.cpp#L189-L228)）：带 `args` 时，用 `std::from_chars` 手工扫描代码串里的 `$数字` 并替换成对应操作数的变量名（`$0` → 第一个实参的 `v3` 之类）。没有 args 则原样输出。

另一个有意思的实现是 `copy_struct`（[EmitAsc.cpp:35-54](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/EmitAsc.cpp#L35-L54)）：它不生成 `memcpy`，而是生成一个逐字节的 `for` 循环，并根据源 memref 的 memory space 打印 `__gm__` 等地址空间修饰——因为结构体可能跨 GM 与 UB，通用 `memcpy` 不可用。

#### 4.2.4 代码实践

**实践目标**：用现成 lit 用例验证你对 EmitAsc 发射的理解，不需要跑任何硬件。

**操作步骤**：

1. 打开 [test/Target/AscendC/emitasc.mlir](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Target/AscendC/emitasc.mlir)。首行 RUN 命令是 `ascir-translate -mlir-to-ascendc %s | FileCheck %s`。
2. 逐个对照「IR 输入 → CHECK 期望输出」。例如第 16-20 行的输入：

```text
func.func @call_opaque_test(%arg0: i32, %arg1: i32) -> i32 {
    %0 = emitasc.call_opaque "add" (%arg0, %arg1) : (i32, i32) -> i32
    emitasc.call_opaque "empty" () : () -> ()
    return %0 : i32
}
```

   期望输出（CHECK 第 11-15 行）是：

```cpp
int32_t call_opaque_test(int32_t v1, int32_t v2) {
  int32_t v3 = add(v1, v2);
  empty();
  return v3;
}
```

3. 若本地已用 `PYASC_SETUP_DEVTOOLS=1` 构建（u1-l2、u7-l5），可直接运行 `ascir-translate -mlir-to-ascendc test/Target/AscendC/emitasc.mlir` 肉眼查看完整输出；没有构建则纯阅读即可。

**需要观察的现象**：`emitasc.ptr_offset` 的两条输入（[emitasc.mlir:36-39](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Target/AscendC/emitasc.mlir#L36-L39)）分别用静态偏移 `512` 和动态偏移 `%arg1`，输出分别是 `v1 + 512` 与 `v1 + v2`——对应发射代码里 `dynamicOffset` 与 `staticOffsetAttr` 两个分支（EmitAsc.cpp:135-139）。

**预期结果**：你能不看 CHECK 行，先自己写出期望 C 代码再对答案；`variable` 用例应产出 `int32_t v2[1]{512};`。**待本地验证**（未运行 ascir-translate 的输出以本地为准）。

#### 4.2.5 小练习与答案

**练习 1**：`emitasc.variable` 的 IR 结果类型为什么是 `memref<1xT>` 而不是 `T`？

**答案**：C 可变变量要被多次赋值、多次读取，与 SSA 单赋值冲突。用 `memref<1xT>`（长度 1 的数组）承载，每次改值都是一次对数组的 store，读取是 load，语义上绕开了 SSA 限制；发射时打印成 `T v[1]{init}`。

**练习 2**：`copy_struct` 为什么生成逐字节循环而不用 `memcpy`？

**答案**：结构体可能位于不同地址空间（GM/UB），发射代码会按 memref 的 memory space 打印 `__gm__` 等修饰，逐字节 `reinterpret_cast<uint8_t*>` 拷贝跨空间安全；`memcpy` 在设备侧跨地址空间不可用。

### 4.3 External 六文件：通用 MLIR 方言的 C 发射

#### 4.3.1 概念说明

前端生成的 IR 中，除了 `ascendc.*`（算子 API）和 `emitasc.*`（C 形状辅助），还有大量**上游通用方言**操作：`func.func` 承载函数、`scf.for/if/while` 承载控制流、`arith.addi/cmpi/...` 承载标量运算、`math.sqrt/exp` 承载数学函数、`memref.load/store` 承载内存读写、`emitc.include` 承载头文件。`lib/Target/AscendC/External/` 下六个文件按方言一一对应地实现它们的发射。称其「External」是因为这些方言定义在 pyasc 之外（MLIR 上游）。

#### 4.3.2 核心流程

六个文件的分工与映射关系：

| 文件 | 处理的操作 | 典型 C 输出 |
| --- | --- | --- |
| Func.cpp | `func.func / call / return / constant` | 函数签名、`f(args)`、`return x` |
| Scf.cpp | `scf.for / if / while / condition / yield / index_switch` | `for(;;){}`、`if(){}else{}`、`while(true)` |
| Arith.h/.cpp | 约 40 个 `arith.*` 标量运算 | `a + b`、`a % b`、`(a+b-1)/b`、三目 |
| Math.h/.cpp | 约 17 个 `math.*` 函数 | `sqrt(x)`、`AscendC::Exp(x)`、`a*b+c` |
| MemRef.cpp | `memref.alloca / load / store / cast` | `T v[N]`、`m[i]`、`m[i]=v` |
| Emitc.cpp | `emitc.include / verbatim / cast / constant / variable` | `#include <...>`、逐字文本 |

两处架构手法值得学习：

- **模板 + `LogicalResultForT` 白名单**：Arith/Math 的几十个二元/一元操作共用一个函数模板，返回类型用 `LogicalResultForT<OpType, 允许列表...>` 约束（[Arith.h:18-24](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Target/Asc/External/Arith.h#L18-L24)），编译期限定重载范围，再用 `if constexpr` 按具体 Op 选操作符。
- **特例内联在模板里**：`ceildivsi` 没有对应 C 运算符，直接打印公式 `(a + b - 1) / b`（[Arith.h:29-33](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Target/Asc/External/Arith.h#L29-L33)）；`min/max` 家族打印成三目表达式（[Arith.h:34-47](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Target/Asc/External/Arith.h#L34-L47)）；`math.fma` 打印 `a * b + c`（[Math.cpp:15-25](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/External/Math.cpp#L15-L25)）。

#### 4.3.3 源码精读

**Func.cpp——函数骨架与 u6-l4 的衔接**。[Func.cpp:53-114](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/External/Func.cpp#L53-L114) 是 `func.func` 的发射。核心判断在 L63-67：

```cpp
bool isMainFunction = functionOp->hasAttr(ascendc::attr::global);
os << (isMainFunction ? "extern \"C\"  __global__ " : "__inline__ __attribute__((always_inline)) ");
os << "__aicore__ ";
```

带 `ascendc.global` 属性的函数打印成 Kernel 入口（`extern "C" __global__ __aicore__`），否则打印成 `__inline__ always_inline __aicore__` 的内联函数——这正是 u6-l5 结尾「Pass 只种属性、发射层才落纸」的落点，也是 u4-l4「真内联交给毕昇编译器」的物理形式。函数体要求单块（L56-58，多块需要顶部声明变量，当前报错），随后逐形参打印类型与名字（L70-77）、逐操作发射（L99-109）。`func.call`（[Func.cpp:23-36](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/External/Func.cpp#L23-L36)）直接打印 `callee(v1, v2)`。

**Arith——谓词与三目**。比较操作 `arith.cmpi` 的谓词映射在 [Arith.cpp:53-86](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/External/Arith.cpp#L53-L86)：`eq→==`、`ne→!=`、`sle/ule→<=`、`slt/ult→<`……注意有符号与无符号谓词（`sle` 与 `ule`）打印成**同一个 C 运算符**——C 的比较运算符行为由操作数类型决定，而操作数类型已由 `emitType` 按符号语义映射（`CodeEmitter::shouldMapToUnsigned`，[CodeEmitter.cpp:257](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/CodeEmitter.cpp#L257) 附近），所以谓词本身无需区分。`cmpf` 同理（[Arith.cpp:88-128](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/External/Arith.cpp#L88-L128)），但 `ORD/UNO/AlwaysTrue` 等无 C 对应的谓词直接 `llvm_unreachable`。`select` 打印三目 `cond ? t : f`（[Arith.cpp:140-149](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/External/Arith.cpp#L140-L149)）；`index_cast` 与扩展/截断族打印 `static_cast<T>(x)`（[Arith.cpp:151-163](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/External/Arith.cpp#L151-L163)、[Arith.h:76-87](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Target/Asc/External/Arith.h#L76-L87)）；`bitcast` 打印 `*reinterpret_cast<T*>(&x)`（[Arith.cpp:130-138](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/External/Arith.cpp#L130-L138)）。

**MemRef 与 Emitc**。`memref.load/store` 打印下标表达式 `m[i]` / `m[i] = v`（[MemRef.cpp:27-49](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/External/MemRef.cpp#L27-L49)）；`memref.alloca` 打印数组声明（L15-25）。`emitc.include` 按 `isStandardInclude` 打印 `#include <...>` 或 `#include "..."`（[Emitc.cpp:55-67](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/External/Emitc.cpp#L55-L67)）——这就是 u6-l4 里 GenerateBoilerplate「按特征 Op 插入 `emitc.include`」的消费端：**include 列表完全由 IR 决定**。

#### 4.3.4 代码实践

**实践目标**：不运行任何东西，用 FileCheck 用例反向测验「IR → C」映射能力。

**操作步骤**：

1. 打开 [test/Target/AscendC/arith.mlir](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Target/AscendC/arith.mlir)。
2. 遮住 CHECK 注释，只看 L27-42 的输入（`arith.addi/subi/muli/divsi/remsi/ceildivsi/shli/shrsi/shrui/andi/ori/xori/divui`），自己在纸上写出 13 行期望输出。
3. 对照 L11-25 的 CHECK：`+`、`-`、`*`、`/`、`%`、`(v1 + v2 - 1) / v2`、`<<`、`>>`（两遍）、`&`、`|`、`^`、`/`。
4. 再看 L57-68 的 `cmpi` 十谓词用例，核对 L44-55 的输出（注意 `sge/uge` 都打印 `>=`）。

**需要观察的现象**：`ceildivsi` 是唯一没有单一 C 运算符的操作；位移 `shrsi`（有符号）与 `shrui`（无符号）输出相同 `>>`，正确性依赖操作数类型的符号映射。

**预期结果**：全部命中即通过；这是仓库自带的黄金用例，`ascir-translate` 行为与 CHECK 严格一致（由 CI lit 保证）。

#### 4.3.5 小练习与答案

**练习 1**：`math.absf` 发射成什么？为什么不用 `fabs`？

**答案**：三目表达式 `(x > 0) ? x : -x`（[Math.h:27-31](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Target/Asc/External/Math.h#L27-L31)）。设备侧 C++ 环境不保证标准库 `fabs` 可用，自拼三目最稳。而 `math.exp/cos/sin/log` 等打印成 `AscendC::Exp(...)` 等**昇腾私有实现**（Math.h:34-55）——通用方言的发射也会落到昇腾命名空间。

**练习 2**：`emitc.include` 与 `emitasc.verbatim` 都能输出「任意文本」，为什么不合并成一个？

**答案**：`emitc.include` 是结构化操作（有 `is_standard_include` 布尔属性，决定 `<>` 与 `""`），Pass 能识别并去重；`verbatim` 是无结构逃生舱，只该在确无对应 Op 时使用。语义分层让优化 Pass 有抓手。

### 4.4 从 Python for 到 C for：scf.for 的完整降级路径

#### 4.4.1 概念说明

这是本讲的收官模块，把前面所有环节串成一条链。`scf.for` 的 IR 形态：

```text
%result = scf.for %iv = %lb to %ub step %step iter_args(%arg = %init) -> (T) {
  ... 使用 %iv、%arg，可能重定义 %arg 的"新值" ...
  scf.yield %new : T
}
```

SSA 世界里「循环变量每圈取新值」由块参数表达：每圈开始时 `%arg` 绑定上一圈 `yield` 的值。C 世界里没有块参数，只有可反复赋值的变量。**发射算法就是把 φ 语义翻译成「声明在前、循环尾赋值」**：

\[ \text{SSA：} v_{k+1} = \text{body}(v_k) \quad\Longrightarrow\quad \text{C：} \underbrace{T\ v = init}_{\text{预声明}};\ \underbrace{\text{for}\{...\ v = body(v);\}}_{\text{循环尾回写}} \]

#### 4.4.2 核心流程

[Scf.cpp:26-93](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/External/Scf.cpp#L26-L93) 的 `printOperation(scf::ForOp)` 分七步：

1. 为每个**循环结果**预声明变量：`T v;`（L34-38）；
2. 为每个 **iter_arg** 打印初始化：`T vN = vInit;`（L40-47）；
3. 打印 C for 头：`for (T iv = lb; iv < ub; iv += step) {`（L49-56）；
4. 发射循环体（**跳过最后一个操作**，即 yield，L66-71）；
5. 把 yield 的操作数**回写**给 iter_args：`vArg = vYield;`（L73-77）；
6. 关闭花括号（L79）；
7. 循环结束后把 iter_args **拷给结果变量**：`vResult = vArg;`（L84-91）。

配套细节：

- `emitBlock`（[Scf.cpp:15-24](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/External/Scf.cpp#L15-L24)）跳过零操作数的 `scf.yield`（无意义的尾巴）；
- `needsSemicolon`（[Common.h:60-68](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Target/Asc/Common.h#L60-L68)）规定 `scf.if/for/index_switch/yield` 后不补分号——语句块自带花括号；
- `scf.while` 的翻译更激进：打印成 `while (true) { ... }`，把 `scf.condition` 翻成 `if (!cond) { 结果回填; break; }`（[Scf.cpp:159-174](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/External/Scf.cpp#L159-L174)、[Scf.cpp:176-208](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/External/Scf.cpp#L176-L208)）——「先执行再判断退出」。

#### 4.4.3 源码精读

以 01_add 为例走全链。Python 侧关键行（[examples/01_add/add.py:49-50](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/01_add/add.py#L49-L50)）：

```python
for i in range(TILE_NUM * BUFFER_NUM):
    buf_id = i % BUFFER_NUM
```

逐环节对应：

1. **FunctionVisitor**（u4-l3）：识别 `range(...)`，边界物化为 int32 常量，生成 `scf.for`；`i` 成为归纳变量（body 块参数），无 iter_args（`buf_id` 每圈重新定义，不跨圈携带——若跨圈改写外层变量才会产生 iter_args，见 u4-l2 的 BlockInOut 记账）。
2. **Pass 流水线**（u6-l1/u6-l2）：EraseSync/HoistQueBind/InsertSync 等改写 `ascendc.*` 部分，`scf.for` 结构原样保留到 `ascir.mlir`。
3. **发射层**：`arith.constant`（边界 0 与 16）经 [Arith.cpp:20-26](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/External/Arith.cpp#L20-L26) → `printConstantOp`（[Common.cpp:19-42](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/Common.cpp#L19-L42)）打印 `constexpr int32_t vN = 16;`（非 fp16 一律加 `constexpr`）；`scf.for` 走上述七步算法；`i % BUFFER_NUM` 是 `arith.remsi` → `vA % vB`。

最终 C 形态（示意，变量名以实际 dump 为准）：

```cpp
constexpr int32_t v1 = 16;
for (int32_t v2 = 0; v2 < v1; v2 += 1) {
  int32_t v3 = v2 % 2;
  // ...ascendc.DataCopy / ascendc.Add 等算子调用...
}
```

对照黄金用例：[test/Target/AscendC/scf.mlir:16-27](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Target/AscendC/scf.mlir#L16-L27) 展示了 `scf.while` 无结果版本的期望输出——`while (true) { ... if (!v3) { break; } ... }`，正是「condition → if-break」策略的直观样例。

再补一环闭环：`while` 的 IR 由前端什么生成？u2-l6/u5-l6 提过 Matmul 的 `Iterate` 用 `scf.while` 手工搭循环（MatmulIterator 自建 `scf.while` 并 save/restore 插入点）——通用控制流 IR 不只来自 Python `for`，也来自高阶 API 内部构造。

#### 4.4.4 代码实践

**实践目标**：验证「`range` 生成循环、`static_range` 完全展开」的分岔，以及 `scf.for` 的 C 形态。

**操作步骤**：

1. 设置 `PYASC_DUMP_PATH`，Model 模式运行原版 01_add，保存 `ascir.mlir` 与 `ascendc.cpp`。
2. 把 [add.py:49](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/01_add/add.py#L49) 的 `range` 改为 `asc.static_range`（记得 `import asc` 已有），再运行一次并另存 dump。
3. 对比两份 `ascir.mlir`：一个含 `scf.for`，一个循环消失、循环体按 16 次顺序展开。
4. 对比两份 `ascendc.cpp`：一个是一条 C `for`，一个是 16 段重复语句；对比文件行数。

**需要观察的现象**：展开版 IR 与 C 代码显著膨胀（约 16 倍于循环体的体量）；`static_range` 版没有任何 `for` 关键字。

**预期结果**：与 u4-l3 的结论一致——`range` 的循环在设备上真正执行（运行时迭代），`static_range` 在编译期摊平。16 次展开会拉长编译时间与代码体积，但省去循环控制开销；这是「编译时间/体积 vs 运行时分支开销」的经典取舍。**待本地验证**（展开倍数以实际 TILE_NUM×BUFFER_NUM 为准）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `scf.for` 的结果变量必须**在循环前**声明，而不能在循环后？

**答案**：C 要求先声明后使用；循环体内可能引用循环结果吗？不会——但结果变量在循环**结束后**被赋值（第 7 步 `vResult = vArg;`），而其类型在进入作用域时就必须可见；更重要的是 SSA 语义要求结果在 `for` 之后可用，若声明在循环体内就成了局部变量。预声明 + 后赋值是 SSA 多出口值到 C 的标准翻译。

**练习 2**：`scf.for` 发射为什么跳过循环体的最后一个操作？

**答案**：最后一个操作必是 `scf.yield`。它不产生 C 语句，其语义（「本圈结束时的迭代变量值」）已由第 5 步的回写赋值 `vArg = vYield;` 显式表达；若照常发射会打出冗余内容。

**练习 3**：`scf.condition` 为什么翻译成 `if (!cond) { ...; break; }` 而不是 `while (cond)`？

**答案**：`scf.while` 的 before/after 两区域中，条件判断之后还有算子要执行；直接 `while (cond)` 会把 before 区域剩余操作排除在循环外。「`while (true)` + 尾部 if-break」保证 before/after 区域所有操作的相对顺序与 IR 一致，是保守而正确的翻译。

## 5. 综合实践

**任务：制作「Python 行 / IR 操作 / C 代码行」三列对照表。**

1. **准备**：设置 `PYASC_DUMP_PATH`，Model 模式运行 `examples/01_add/add.py`，得到 `codegen.mlir`、`ascir.mlir`、`ascendc.cpp` 三份产物。
2. **选材**：在 kernel 里挑出以下 6 个 Python 语句/表达式（都在 [examples/01_add/add.py:29-69](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/01_add/add.py#L29-L69)）：
   - L31 `offset = asc.get_block_idx() * block_length`
   - L35 `x_gm.set_global_buffer(x + offset, block_length)`
   - L39 `tile_length = block_length // TILE_NUM // BUFFER_NUM`
   - L49 `for i in range(TILE_NUM * BUFFER_NUM):`
   - L50 `buf_id = i % BUFFER_NUM`
   - L53 `asc.data_copy(x_local[buf_id * tile_length:], ...)`
3. **填表**：为每一行在 `ascir.mlir` 中找到对应 IR 操作（提示：`ascendc.` 前缀查 u5-l1 的「四名合一」反查法；`x + offset` 是 `emitasc.ptr_offset`；`//` 整除是 `arith.divsi`；循环是 `scf.for`；`%` 是 `arith.remsi`；切片是 `ascendc.LocalTensorSubIndexOp` 一族），再在 `ascendc.cpp` 中找到生成的 C 语句，抄录变量名。
4. **扩展（可选，示例代码，待本地验证）**：写一个只含整数累加的新 kernel（如 `acc = 0; for i in range(8): acc = acc + i * 2`，最后把 `acc` 写入 GM），它会产生**带 iter_args 的 `scf.for`**，正好覆盖第 2、5、7 步的回写赋值路径——这是 01_add 覆盖不到的分支：

```text
int32_t v_acc;                 // 步骤1：结果预声明（示意）
int32_t v1 = 0;                // 步骤2：iter_arg 初始化
for (int32_t v2 = 0; ...) {
    int32_t v3 = v2 * 2;
    int32_t v4 = v1 + v3;
    v1 = v4;                   // 步骤5：yield 回写
}
v_acc = v1;                    // 步骤7：iter_args 拷给结果
```

**验收标准**：三列对照表每行都能闭环；对 `acc` 版 kernel，能指出 C 输出中哪一行来自第 1 步、哪两行分别来自第 5 步与第 7 步。

## 6. 本讲小结

- **EmitAsc 是「C 形状」概念的专用方言**：12 个操作承载指针算术、成员访问、结构体声明、按名调用、逐字文本等没有 Ascend C API 对应物的语义；它与上游 `emitc` 分工并存，且没有独立降级 Pass——与 ascendc/通用方言并列进入统一发射。
- **EmitAsc 操作有三个生产者**：Python 前端（`GlobalAddress.__add__`、`asc.inline`、Struct 三件套）、Transforms Pass（`DeclarePyStructPass`、`LegalizeKernelArgs`）、以及手写在 OpBuilder.cpp 的 pybind 绑定；`emitasc.kernel_arg` 属性是后端向 Launcher 回传参数 ABI 的通道。
- **External 六文件按方言拆分**：Func（函数骨架，`ascendc.global` 属性决定 `extern "C" __global__` 与 `always_inline` 两种命运）、Scf（控制流）、Arith/Math（标量运算与数学函数，模板 + `LogicalResultForT` 白名单量产）、MemRef（数组与下标）、Emitc（include/逐字）。
- **`scf.for` → C for 的七步算法**是 SSA 到命令式的机械翻译：结果预声明、iter_args 初始化、for 头、体（跳过 yield）、yield 回写、关括号、iter_args 拷给结果；`scf.while` 翻成 `while (true)` + `if (!cond) break`。
- **黄金用例就在仓库里**：`test/Target/AscendC/{emitasc,arith,scf,func,memref,math,emitc}.mlir` 用 FileCheck 锁定了每个映射，是无需硬件的最佳练习素材。
- 一个常见误区澄清：min/max/absf/ceildivsi 等「没有 C 运算符」的操作并不失败，而是**内联展开成三目或公式**；真正失败的是白名单外的操作（`unable to find printer for op`）。

## 7. 下一步学习建议

本讲讲完，第 6 单元（Pass 优化与 Ascend C 代码生成）就完整了：你已经具备从 Python 源码到 C 语句的全链路追踪能力。接下来：

- **u7-l5（开发者工具）**：学习用 `ascir-opt` 单独跑 Pass、用 `ascir-translate` 手工翻译 `.mlir`，把本讲的 lit 用例变成可交互的实验场。
- **u7-l6（测试体系与贡献流程）**：`test/Target/AscendC/` 下这些 FileCheck 用例正是贡献新接口时必须补的回归；了解新增一个 Ascend C 接口要在 language、td、TableGen、发射、测试五层落文件。
- **源码延伸阅读**：对照读 [lib/Target/AscendC/CodeEmitter.cpp](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/CodeEmitter.cpp) 的类型映射表（`emitTypeMapper`，L164 附近把 `emitasc::PyStructType` 映射为结构体名打印），理解类型系统如何接入发射层；再读 [test/Dialect/AscendC/Transforms/insert-sync.mlir](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Dialect/AscendC/Transforms/insert-sync.mlir) 观察含 `scf.for` 的 IR 在同步重建后的形态。
