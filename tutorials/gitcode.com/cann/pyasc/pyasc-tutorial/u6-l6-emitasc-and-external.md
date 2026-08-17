# u6-l6 EmitAsc 方言与外部方言降级

## 1. 本讲目标

学完本讲，你应该能够：

1. 回答一个架构问题：**为什么 pyasc 不直接从 Asc 方言打印 C 代码，而要引入一个 EmitAsc 中间方言**，以及 EmitAsc 与 MLIR 官方 `emitc` 方言的分工。
2. 说出 EmitAsc 全部 12 个 Operation 各自对应的 C++ 语法（指针偏移、成员访问、`reinterpret_cast`、可变变量、结构体声明与拷贝、原生函数调用、原样文本）。
3. 掌握 `External/` 目录下 Arith、Math、Scf、Func、MemRef、Emitc 六个文件如何把通用 MLIR 方言操作翻译成 C 表达式，并能在源码中定位 `arith.constant`、`scf.for`、`func.call` 的发射函数。
4. 独立跟踪一条完整链路：Python `for i in range(...)` → `scf.for` IR → ascendc.cpp 中的 `for (;;)` 语句，并写出「Python 行 / IR 操作 / C 代码行」三列对照表。

本讲是第 6 单元（Pass 优化与 Ascend C 代码生成）的收尾讲，承接 u6-l5 已经建立的 Translation 分发、CodeEmitter 变量命名等结论，不再重复。

## 2. 前置知识

### 2.1 一份 IR 里其实混着好几种方言

回顾 u5-l1 的结论：ASC-IR 是一个 MLIR 模块，其中**镜像 Ascend C API 的部分**用自研 `ascendc` 方言（dump 时前缀为 `ascendc.`），而**通用语义**直接复用 MLIR 上游方言：

| 上游方言 | 表达的语义 | 典型操作 |
|---|---|---|
| `func` | 函数、调用、返回 | `func.func`、`func.call`、`func.return` |
| `scf` | 结构化控制流 | `scf.for`、`scf.if`、`scf.while`、`scf.yield` |
| `arith` | 标量整数/浮点算术 | `arith.constant`、`arith.addi`、`arith.cmpi` |
| `math` | 数学函数 | `math.fma`、`math.copysign` |
| `memref` | 内存引用 | `memref.alloca`、`memref.load`、`memref.store` |
| `emitc` | MLIR 官方的「C 语法桥梁」 | `emitc.include`、`emitc.verbatim` |

这意味着发射层（Translation）要面对**三类客户**：ascendc Op（u6-l5 已讲，约数百个）、上游方言 Op、以及本讲的主角——自研 `emitasc` Op。

### 2.2 SSA 值与 C 变量的鸿沟

MLIR 是 SSA（静态单赋值）形式：每个值只定义一次。而 C 是可变变量语言。两者翻译的通用手法是「**先声明、后赋值**」：

- 每个有结果的 Op 发射为 `类型 变量 = 表达式;`（`emitAssignPrefix` 负责左半边，见 [lib/Target/AscendC/CodeEmitter.cpp:L347-L363](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/CodeEmitter.cpp#L347-L363)，u6-l5 已精读）。
- 循环携带依赖在 MLIR 里靠 `iter_args`/`scf.yield` 接线；翻成 C 时变成「循环前声明变量、循环体内赋值、必要时循环后再拷贝一次」——这是 4.4 节的核心戏码。

### 2.3 术语预备

- **发射（emit）**：把 IR 操作翻译成 C/C++ 源文本的动作，入口是 `printOperation` 重载族。
- **`LogicalResult`**：MLIR 的成败返回值，`success()`/`failure()`；发射失败会沿调用链上传并在 Python 侧抛异常（u6-l5）。
- **`FAIL_OR`**：pyasc 定义的一个宏，展开后执行表达式并在失败时提前 `return failure()`，让发射代码保持线性书写。
- **`getOrCreateName(Value)`**：CodeEmitter 的变量命名服务，首次见到某个 IR 值时通过 EmitNameStack 生成 `v3` 这样的唯一可读名字并缓存（[lib/Target/AscendC/CodeEmitter.cpp:L238-L247](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/CodeEmitter.cpp#L238-L247)）；常量则命名为 `c45_i32` 这类「c 值 _ 类型」形式（依据见 4.3 节测试输出）。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [include/ascir/Dialect/EmitAsc/IR/Ops.td](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/EmitAsc/IR/Ops.td#L22-L170) | EmitAsc 方言 12 个 Operation 的 TableGen 定义 |
| [include/ascir/Dialect/EmitAsc/IR/Dialect.td](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/EmitAsc/IR/Dialect.td#L16-L30) | 方言声明（名字 `emitasc`，命名空间 `mlir::emitasc`） |
| [include/ascir/Dialect/EmitAsc/IR/Types.td](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/EmitAsc/IR/Types.td#L22-L25) | `PyStruct` 类型定义（Python 结构体的 IR 表示） |
| [include/ascir/Dialect/EmitAsc/IR/Attributes.td](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/EmitAsc/IR/Attributes.td#L25-L41) | `kernel_arg` 枚举属性（供 Launcher 区分显式/隐藏参数） |
| [lib/Target/AscendC/EmitAsc.cpp](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/EmitAsc.cpp#L23-L228) | EmitAsc 12 个 Op 的 C++ 发射实现 |
| [lib/Target/AscendC/Translation.cpp](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/Translation.cpp#L72-L96) | `PrintableOpTypes` 白名单元组（本讲关注其前段：Builtin/EmitC/Func/SCF/MemRef/Arith/Math/EmitAsc） |
| [lib/Target/AscendC/External/Arith.cpp](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/External/Arith.cpp#L20-L163) 与 [include/ascir/Target/Asc/External/Arith.h](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Target/Asc/External/Arith.h#L18-L101) | arith 操作到 C 表达式的映射（模板 + 特化两段） |
| [lib/Target/AscendC/External/Scf.cpp](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/External/Scf.cpp#L15-L208) | scf 控制流到 C for/if/switch/while 的映射 |
| [lib/Target/AscendC/External/Func.cpp](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/External/Func.cpp#L15-L114) | func 函数/调用/返回的发射（含 Kernel 样板前缀） |
| [lib/Target/AscendC/External/Math.cpp](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/External/Math.cpp#L15-L36)、[MemRef.cpp](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/External/MemRef.cpp#L15-L59)、[Emitc.cpp](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/External/Emitc.cpp#L15-L67) | math/memref/emitc 的发射（较薄，浏览即可） |
| [lib/Target/AscendC/Common.cpp](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/Common.cpp#L19-L42) | `printConstantOp`：三种常量 Op 的公共发射 |
| [python/asc/codegen/function_visitor.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L479-L516) | 前端 `visit_For`：`range` → `scf.for` |
| [python/asc/language/core/ir_value.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/ir_value.py#L255-L264) | `apply_binary_op`：运算符 → `create_arith_*Op` |
| [python/asc/language/core/ops.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/ops.py#L17-L29) | `asc.inline`：EmitAsc 的用户逃生舱 |
| [test/Target/AscendC/emitasc.mlir](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Target/AscendC/emitasc.mlir#L9-L101)、[arith.mlir](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Target/AscendC/arith.mlir#L9-L38)、[scf.mlir](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Target/AscendC/scf.mlir#L16-L37) | 官方 IR→C 对照样本（FileCheck 断言的就是期望 C 输出） |

## 4. 核心概念与源码讲解

### 4.1 EmitAsc 方言：为什么需要它

#### 4.1.1 概念说明

先看问题。Asc 方言的设计哲学是「一条 Op 镜像一条 Ascend C API 调用」（u5-l3），比如 `ascendc.AddL2Op` 对应 `AscendC::Add(...)`。但最终生成的 ascendc.cpp 是一份完整 C++ 源文件，里面除了 API 调用，还有大量**C++ 语言本身的语法**：

- 指针加偏移：`x_gm_ptr + offset`（01_add 第 35 行的 `x + offset`）；
- 取结构体成员：`config.tile_length`、`config->field`；
- 类型双关：`reinterpret_cast<...>`；
- 可变局部变量：SSA 世界没有「变量」，但 C 循环需要；
- 声明结构体：`#pragma pack(push, 8) struct Xxx {...};`
- 调用任意 C++ 函数：既非 ascendc API 又非 func.call 的情况；
- 原样嵌入文本：`#include` 之外的杂项样板。

这些语法没有对应的 Ascend C API，硬造几百个 Asc Op 既污染方言又违背「Asc 只镜像 API」的边界。pyasc 的解法是三管齐下：

1. **通用语义**（算术、控制流、函数）→ 直接复用上游 `arith`/`scf`/`func` 方言，发射层在 `External/` 目录统一翻译（4.3、4.4 节）；
2. **官方已有桥梁** → 复用 `emitc` 方言（`emitc.include` 等）；
3. **emitc 没有的、pyasc 特有的 C++ 语法** → 自研 **EmitAsc 方言**补齐。

一句话定位：**EmitAsc 是「贴近 C 语法的低层桥梁」，补齐 emitc 覆盖不到、而 Asc 方言又不该管的那些 C++ 语法节点。**

一个容易误解的点：EmitAsc **不是**由某个 Pass 从 Asc 方言「降级」出来的。它的 Op 有三个来源：

- **前端直接创建**：`GlobalAddress.__add__` 创建 `emitasc.ptr_offset`，Struct 成员读写创建 `emitasc.member`/`set_member`，`asc.inline()` 创建 `emitasc.verbatim`；
- **Pass 种入**：`DeclarePyStructPass` 在模块里插入 `emitasc.declare_py_struct`（u6-l4），`LegalizeKernelArgs` 使用 `emitasc.kernel_arg` 属性；
- **手写 IR 测试**：`test/Target/AscendC/emitasc.mlir` 直接手写 emitasc IR 验证发射。

#### 4.1.2 核心流程

EmitAsc 一条 Op 从创建到变成 C 代码的路径：

```text
Python 前端 / Pass
    │  builder.create_emitasc_XxxOp(...)        (pybind 绑定，python/src/OpBuilder.cpp)
    ▼
ASC-IR 中的 emitasc.xxx 操作                    (与 scf/arith/ascendc 混居同一模块)
    │  Compiler.run 跑完 Pass 后调用 ir_to_ascendc
    ▼
Translation.emitOperation 的 TypeSwitch 命中     (PrintableOpTypes 白名单，Translation.cpp:93-96)
    │  mlir::emitasc::printOperation(emitter, op) 重载
    ▼
CodeEmitter 提供的类型/命名/缩进服务 → C++ 文本
```

#### 4.1.3 源码精读

**（1）方言声明**。EmitAsc 方言名字是 `emitasc`，C++ 命名空间 `mlir::emitasc`，见 [include/ascir/Dialect/EmitAsc/IR/Dialect.td:L16-L30](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/EmitAsc/IR/Dialect.td#L16-L30)（这段声明了方言并开放默认的类型/属性打印解析器）。它没有 `Asc_Dialect` 那样的 Op 注册宏全家桶，注册逻辑在 `lib/Dialect/EmitAsc/IR/Types.cpp` 等手写文件中完成。

**（2）12 个 Operation 一览**。基类模板 `EmitAsc_Op` 见 [include/ascir/Dialect/EmitAsc/IR/Ops.td:L22-L25](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/EmitAsc/IR/Ops.td#L22-L25)，每个 Op 的定义、参数与 C++ 对应关系整理如下（行号均为 Ops.td 中的定义位置）：

| Op（IR 名） | 定义行 | 对应 C++ 语法 | 典型来源 |
|---|---|---|---|
| `emitasc.call_opaque` | [L27-L39](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/EmitAsc/IR/Ops.td#L27-L39) | `callee(args...)` 按名字调用任意 C++ 函数 | 发射层辅助 |
| `emitasc.copy_struct` | [L41-L46](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/EmitAsc/IR/Ops.td#L41-L46) | 局部结构体定义 + 逐字节 memcpy | Struct `create_local()` |
| `emitasc.declare_py_struct` | [L48-L52](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/EmitAsc/IR/Ops.td#L48-L52) | `#pragma pack(push,8) struct {...};` | DeclarePyStructPass |
| `emitasc.dereference` | [L54-L59](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/EmitAsc/IR/Ops.td#L54-L59) | `T& r = *p;` | 发射层辅助 |
| `emitasc.member` | [L61-L67](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/EmitAsc/IR/Ops.td#L61-L67) | `obj.field` / `obj->field` | Struct `__getattrjit__` |
| `emitasc.member_ptr` | [L69-L84](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/EmitAsc/IR/Ops.td#L69-L84) | `(T*)&base->member;` | 发射层辅助 |
| `emitasc.member_ref` | [L86-L101](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/EmitAsc/IR/Ops.td#L86-L101) | `T& r = base->member;` | 发射层辅助 |
| `emitasc.set_member` | [L103-L108](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/EmitAsc/IR/Ops.td#L103-L108) | `obj.field = value;` | Struct `__setattrjit__` |
| `emitasc.ptr_offset` | [L110-L122](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/EmitAsc/IR/Ops.td#L110-L122) | `base + offset`（静态或动态偏移） | `GlobalAddress.__add__` |
| `emitasc.reinterpret_cast` | [L124-L131](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/EmitAsc/IR/Ops.td#L124-L131) | `reinterpret_cast<T>(src)` | 张量类型双关 |
| `emitasc.variable` | [L133-L165](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/EmitAsc/IR/Ops.td#L133-L165) | `T v[N]{init};`（可变变量的 SSA 化） | 需要可变语义处 |
| `emitasc.verbatim` | [L167-L170](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/EmitAsc/IR/Ops.td#L167-L170) | 原样输出文本，支持 `$0 $1` 占位替换 | `asc.inline()` |

三个值得停留的细节：

- **`ptr_offset` 的双形态**：偏移既可以是编译期 `IndexAttr`（`staticOffset`）也可以是运行时 `Optional<Index>` 操作数（`dynamicOffset`），还挂了 `ViewLikeOpInterface` 并 `hasFolder`（允许在 Fold 阶段做简化）——这是整个 Ops.td 里接口最丰富的一个 Op，因为它承载了 01_add 中 `x + offset` 这种最高频的指针运算。
- **`variable` 的「memref 包装」 trick**：注意它的结果类型约束是 `AnyNon0RankedMemRef`，builder 里把初始化值包成 `MemRefType::get(1, type)`（[L145-L160](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/EmitAsc/IR/Ops.td#L145-L160)）。SSA 世界表达「一个可变槽位」的标准做法就是给它一个地址（一维数组），发射时再还原成 `T v[1]{init};`。
- **`PyStruct` 类型与 `kernel_arg` 属性**：[include/ascir/Dialect/EmitAsc/IR/Types.td:L22-L25](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/EmitAsc/IR/Types.td#L22-L25) 定义了 `!emitasc.py_struct<"名字", [成员类型], [成员名]>` 类型，参数化的成员表让一个类型表达任意 Python Struct；[include/ascir/Dialect/EmitAsc/IR/Attributes.td:L25-L41](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/EmitAsc/IR/Attributes.td#L25-L41) 定义了 `explicit`/`ffts_addr` 两值枚举属性，即 u6-l4 讲过的 kernel 参数 ABI 标记——可以看到 EmitAsc 方言同时还承载了「标注发射层元信息」的职责。

**（3）前端创建点之一：指针偏移**。01_add 第 35 行 `x + offset` 中 `x` 是 `GlobalAddress`，其 `__add__` 实现：

```python
@require_jit
def __add__(self, offset: "RuntimeInt") -> GlobalAddress:
    offset = materialize_ir_value(offset, KT.int_)
    builder = global_builder.get_ir_builder()
    offset_index = builder.create_arith_IndexCastOp(offset.to_ir(), builder.get_index_type())
    handle = builder.create_emitasc_PtrOffsetOp(self.to_ir(), offset_index)
    return GlobalAddress(handle, self.dtype)
```

见 [python/asc/language/core/ir_value.py:L45-L51](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/ir_value.py#L45-L51)。注意它**混用两个方言**：先用 `arith.index_cast` 把偏移转成 `index` 类型，再交给 `emitasc.ptr_offset` 做指针加法——这正是「一个表达式、多方言协作」的缩影（u2-l3 讲过 GlobalAddress 加法是指针偏移而非算术加法，此处即其 IR 落点）。

**（4）前端创建点之二：Struct 成员访问**。[python/asc/language/core/struct.py:L194-L211](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/struct.py#L194-L211) 中 `__getattrjit__` 生成 `emitasc.member`、`__setattrjit__` 生成 `emitasc.set_member`；[L223-L233](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/struct.py#L223-L233) 用 `get_emitasc_PyStructType` 构造 IR 类型，[L242-L246](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/struct.py#L242-L246) 的 `create_local` 生成 `emitasc.copy_struct`（u3-l3 讲过的 Struct「设备侧本地副本」）。

**（5）pybind 绑定**。`create_emitasc_*` 方法是 OpBuilder.cpp 中**手写**的绑定（它们不属于 `-gen-pybind-defs` 生成的 ascendc 族），见 [python/src/OpBuilder.cpp:L819-L834](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/src/OpBuilder.cpp#L819-L834)，依次绑定 `PtrOffsetOp`、`SetMemberOp`、`VerbatimOp`——呼应 u5-l5 的结论：需要枚举翻译或多结果打包的方法手写，规整 ascendc Op 走生成。

**（6）注册进发射白名单**。[lib/Target/AscendC/Translation.cpp:L93-L96](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/Translation.cpp#L93-L96) 把 12 个 emitasc Op 全部列进 `PrintableOpTypes` 元组，与 emitc/func/scf/memref/arith/math 平起平坐地排在 ascendc 之前；未登记的 Op 会落入 `emitOperation` 的 Default 分支并报 "unable to find printer for op"（[Translation.cpp:L271-L293](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/Translation.cpp#L271-L293)，u6-l5 已精读）。另外 [bin/ascir-translate.cpp:L35-L48](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/bin/ascir-translate.cpp#L35-L48) 注册翻译工具时把 `emitasc::EmitAscDialect` 与 arith/scf/func/memref/math/emitc 一并插入方言注册表——这份清单就是「发射层接受哪些方言」的权威答案。

#### 4.1.4 代码实践

**实践 A：用 `asc.inline` 亲眼看 EmitAsc 生效。**

1. 实践目标：验证 `emitasc.verbatim` 把文本原样送进 ascendc.cpp。
2. 操作步骤：
   - 参考 [examples/01_add/add.py](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/01_add/add.py#L28-L69) 复制一份最小 kernel（Model 模式即可，无需 NPU）；
   - 在 kernel 体内加一行 `asc.inline("// hello from emitasc")`（`inline` 的定义见 [python/asc/language/core/ops.py:L17-L29](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/ops.py#L17-L29)，经 `asc.language.__init__` 导出为 `asc.inline`）；
   - 设置 `PYASC_DUMP_PATH=.` 后运行，打开导出的 `codegen.mlir` 与 `ascendc.cpp`。
3. 需要观察的现象：`codegen.mlir` 中出现 `emitasc.verbatim "// hello from emitasc"` 操作；`ascendc.cpp` 中该字符串原样出现。
4. 预期结果：文本逐字出现在两处。**待本地验证**（本讲写作环境未运行编译器）。
5. 进阶：把调用改成 `asc.inline("// value = $0", args=(tile_length,))`，观察 `$0` 是否被替换为该 IR 值的 C 变量名（替换逻辑见 4.2 节 `VerbatimOp`）。注意 `inline` 是逃生舱，注入的 C 代码 correctness 由你自己负责。

#### 4.1.5 小练习与答案

**练习 1**：为什么不把 `ptr_offset`、`member` 这些做进 Asc 方言？

**参考答案**：Asc 方言的边界是「镜像 Ascend C API」（u5-l1/u5-l3 的命名规则 `ascendc.类名.成员函数` 都围绕 API 展开）。`+`、`->`、`reinterpret_cast` 是 C++ 语言语法而非 Ascend C 库接口，塞进 Asc 会破坏「一条 Op 一条 API」的可读性与 TableGen 生成机制（Asc Op 都带 `getAPIName` 供发射层拼接 API 名）。独立成 EmitAsc 让两个方言各自保持单一职责。

**练习 2**：`emitasc.variable` 为什么把结果类型定为 `memref<1xT>` 而不是直接的 `T`？

**参考答案**：MLIR 是 SSA，单值只能赋值一次；「可变变量」需要一个稳定的地址语义。包装成 `memref<1xT>` 后，对变量的每次写都变成对同一地址的 store，类型系统层面就表达了可变性；发射层再把这层包装剥掉，还原成 C 的 `T v[1]{init};`（数组形式 + 初始化列表）。

**练习 3**：`emitasc` 与 `emitc` 都有 `verbatim`，为什么两个都要留？

**参考答案**：`emitc` 是 MLIR 官方方言，`emitc.verbatim` 只做纯文本输出；`emitasc.verbatim` 在此基础上增加了 `$0/$1/...` 占位符与 IR 值绑定（`args` 变长操作数），能把 IR 值的 C 变量名嵌进文本。两者能力不同、来源不同（`emitc.include` 由 Pass 插入，`emitasc.verbatim` 主要服务 `asc.inline`），因此并存。

### 4.2 EmitAsc 的发射实现

#### 4.2.1 概念说明

`lib/Target/AscendC/EmitAsc.cpp` 为 12 个 Op 各写一个 `mlir::emitasc::printOperation` 重载（声明集中在 [include/ascir/Target/Asc/EmitAsc.h:L24-L46](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Target/Asc/EmitAsc.h#L24-L46)）。与 ascendc Op 的发射相比，这里的手写代码更多、生成代码更少——因为 EmitAsc Op 数量有限且形态各异，TableGen 化收益不大。本模块挑五个最有代表性的精读。

#### 4.2.2 核心流程

每个重载的套路一致：

1. 若有结果：`FAIL_OR(emitter.emitAssignPrefix(...))` 或 `emitType` 先打印左值；
2. 用 `emitter.getOrCreateName(...)` 取操作数的 C 变量名；
3. 把 C++ 语法文本拼进 `os`；
4. 返回 `success()`，分号由外层 `emitOperation` 统一补（多行语句的 Op 自带换行，分号位置见各实现）。

#### 4.2.3 源码精读

**（1）`CallOpaqueOp`——按名字调用**（[lib/Target/AscendC/EmitAsc.cpp:L23-L33](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/EmitAsc.cpp#L23-L33)）：

```cpp
FAIL_OR(emitter.emitAssignPrefix(*op.getOperation()));
os << op.getCallee() << '(';
llvm::interleaveComma(op.getOperands(), os, [&](Value operand) { os << emitter.getOrCreateName(operand); });
os << ')';
```

`callee` 是字符串属性，允许带命名空间甚至模板参数（td 注释要求「demangled，构造函数用父类名表示」），操作数逐个打印成逗号列表。对照测试 [test/Target/AscendC/emitasc.mlir:L16-L20](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Target/AscendC/emitasc.mlir#L16-L20)：`emitasc.call_opaque "add" (%arg0, %arg1)` 发射为 `int32_t v3 = add(v1, v2);`。

**（2）`PtrOffsetOp`——指针加偏移**（[lib/Target/AscendC/EmitAsc.cpp:L130-L141](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/EmitAsc.cpp#L130-L141)）：打印 `base + N`，`N` 取动态操作数或静态属性二选一。测试 [emitasc.mlir:L36-L40](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Target/AscendC/emitasc.mlir#L36-L40) 中静态 `512` 与动态 `%arg1` 两种写法分别得到 `int32_t* v3 = v1 + 512;` 与 `int32_t* v4 = v1 + v2;`。

**（3）`VariableOp`——可变变量落地**（[lib/Target/AscendC/EmitAsc.cpp:L164-L187](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/EmitAsc.cpp#L164-L187)）：先 `emitType` 打印元素类型，再打印变量名和形状维度（`[1]`），最后 `{init}` 打印初始化列表——静态初始化走 `emitAttribute`（直接印常量），动态初始化印初始化表达式的变量名。测试 [emitasc.mlir:L56-L60](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Target/AscendC/emitasc.mlir#L56-L60) 的期望输出正是 `int32_t v2[1]{512};`、`int32_t v3[1]{v1};`。

**（4）`DeclarePyStructOp`——结构体声明**（[lib/Target/AscendC/EmitAsc.cpp:L95-L110](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/EmitAsc.cpp#L95-L110)）：从 `PyStructType` 属性里取名字、成员类型表、成员名表，逐成员 `emitType` + 名字，整体包在 `#pragma pack(push, 8) ... #pragma pack(pop)` 里——与前端 ctypes 打包结构体的 `_pack_ = 8` 成对（u6-l4 已讲过这对约定），两端不一致会导致 Host/Device 布局错位。期望输出见 [emitasc.mlir:L62-L78](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Target/AscendC/emitasc.mlir#L62-L78)。

**（5）`VerbatimOp`——带占位符的原样输出**（[lib/Target/AscendC/EmitAsc.cpp:L189-L228](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/EmitAsc.cpp#L189-L228)）：无 `args` 时直接输出整段文本；有 `args` 时手工扫描字符串，把 `$<数字>` 替换成对应 IR 值的 C 变量名（用 `std::from_chars` 解析下标，越界或解析失败则原样保留 `$`）。这就是 4.1.4 实践 A 进阶步骤的依据。

**（6）`CopyStructOp`——最「重」的一个**（[lib/Target/AscendC/EmitAsc.cpp:L35-L54](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/EmitAsc.cpp#L35-L54)）：它不发一行调用，而是**生成一小段 C 代码**——先声明局部结构体，再写一个按字节循环的拷贝，源端还会根据 memref 的 memory space 打出 `__gm__` 之类的地址空间修饰（经 `symbolizeAddressSpace` 反查枚举）。期望输出见 [emitasc.mlir:L79-L91](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Target/AscendC/emitasc.mlir#L79-L91)。这解释了 u3-l3 的一处现象：Struct 参数从 GM 拷到本地走的不是 `data_copy`，而是这段编译期展开的逐字节循环。

其余 `Member/MemberPtr/MemberRef/SetMember/Dereference/ReinterpretCast` 的实现模式与上述同构（成员访问依据 base 是否 memref 决定 `->` 还是 `.`，见 [EmitAsc.cpp:L64-L75](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/EmitAsc.cpp#L64-L75)），建议按上表自行通读。

#### 4.2.4 代码实践

**实践 B：把官方测试当「对照字典」用。**

1. 实践目标：不运行编译器也能掌握 emitasc Op 的输入输出形态。
2. 操作步骤：打开 [test/Target/AscendC/emitasc.mlir](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Target/AscendC/emitasc.mlir#L9-L101)，该文件每个函数上方 `// CHECK:` 注释就是 FileCheck 断言的期望 C 输出；对照 4.1.3 的表格，把每个 CHECK 块与 Ops.td 定义、EmitAsc.cpp 实现三方对齐。
3. 需要观察的现象：CHECK 行里的变量名规律——普通值是 `v1、v2...`，常量是 `c45_i32`（值_类型）；`__gm__` 修饰出现在 memory space 为 GM 的参数上。
4. 预期结果：整理出 12 行的「Op → 期望 C 输出」表。若本地已按 u7-l5 的方式构建 devtools，可执行 `ascir-translate -mlir-to-ascendc test/Target/AscendC/emitasc.mlir` 直接得到输出（**待本地验证**）。

#### 4.2.5 小练习与答案

**练习 1**：`DeclarePyStructOp` 发射的 `#pragma pack(push, 8)` 若改成 4，哪一层会先出错？

**参考答案**：不会是编译错误，而是**运行期数据错位**。Host 侧 ctypes 结构体按 `_pack_ = 8` 打包参数 blob（u3-l3、u6-l4），Device 侧按 8 字节对齐解析；两端 pack 不一致时成员偏移错开，数值悄悄读错。这属于「约定型耦合」，无任何编译期校验。

**练习 2**：`CopyStructOp` 为什么不直接生成 `memcpy` 或结构体赋值 `auto local = *gm_ptr;`？

**参考答案**：源结构体位于 `__gm__`（全局内存）地址空间，目标是本地变量，两类地址空间在昇腾编译器里不能直接解引用赋值；逐字节 `reinterpret_cast<uint8_t*>` 循环是绕开地址空间类型限制的保守写法。同时逐字节方式让源端可以打地址空间修饰（`reinterpret_cast<__gm__ uint8_t*>`），保持两端各自合法。

### 4.3 External 发射：scf/arith/math/memref/emitc 到 C 的映射

#### 4.3.1 概念说明

`lib/Target/AscendC/External/` 下六个文件专门翻译**上游方言**，目录名「External」即「Asc 之外的外部方言」。它们的定位与 Asc/EmitAsc 发射不同：

- Asc Op：一条 Op ≈ 一条 API 调用，大量由 TableGen 生成（u6-l5）；
- External Op：一条 Op ≈ **一个 C 表达式或语句**，例如 `arith.addi` → `a + b`。

其中 arith 是最厚的一块，采用「**头文件模板 + cpp 特化**」两段式组织；scf/func 负责控制流与函数骨架（放到 4.4 一起讲）；math/memref/emitc 很薄。

#### 4.3.2 核心流程

以 `z = a + b`（int）为例的完整链条：

```text
Python: buf_id = i % BUFFER_NUM
  └─ PlainValue.__mod__ → apply_binary_op(self, other, "RemSI", None)      (ir_value.py:96)
       └─ create_arith_RemSIOp(lhs_ir, rhs_ir)                              (ir_value.py:263)
            ▼  (Pass 流水线不改写算术，原样到达发射)
Translation.emitOperation → TypeSwitch 命中 arith::RemSIOp
  └─ printOperation 模板 (Arith.h) → emitAssignPrefix → "lhs % rhs"
       ▼
C 代码: int32_t v9 = v3 % c2;
```

常量则多一跳：Python 立即数 `2` 经 `materialize_ir_value` → `convert_value` → `builder.get_i32(2)`（[python/asc/language/core/ir_value.py:L366-L396](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/language/core/ir_value.py#L366-L396)），后者在 C++ 侧 `create<arith::ConstantOp>`（[python/src/OpBuilder.cpp:L523-L527](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/src/OpBuilder.cpp#L523-L527)）。

#### 4.3.3 源码精读

**（1）二元运算的「一个模板管 24 个 Op」**。[include/ascir/Target/Asc/External/Arith.h:L18-L74](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Target/Asc/External/Arith.h#L18-L74) 用函数模板 + 返回类型 `LogicalResultForT<BinaryOpType, ...24 个 Op 类型...>` 的 SFINAE 手法，让**一个函数体**服务 `AddI/SubI/MulI/DivSI/RemSI/AndI/OrI/XOrI/ShLI/ShRSI/ShRUI/...` 全体规则二元运算：先 `isScalarOperation` 校验、`emitAssignPrefix` 打左值，再用 `if constexpr` 链按 Op 类型挑选 `+ - * / % & | ^ << >>` 符号。两个特例值得注意：

- `CeilDivSIOp`（向上取整除）没有对应 C 运算符，展开为 `(lhs + rhs - 1) / rhs`（[Arith.h:L29-L33](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Target/Asc/External/Arith.h#L29-L33)）；
- `MaximumF/MinimumF/MaxSI/MinSI` 等最大最小族展开为三目表达式 `((a > b) ? (a) : (b))`（[Arith.h:L34-L47](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Target/Asc/External/Arith.h#L34-L47)）。

**（2）Arith.cpp 里的特化**。[lib/Target/AscendC/External/Arith.cpp:L53-L86](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/External/Arith.cpp#L53-L86) 的 `CmpIOp` 把 10 种谓词（`eq/ne/sle/ult/...`）switch 成 6 个 C 符号（有符号/无符号折叠为同一符号）；`SelectOp`（[L140-L149](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/External/Arith.cpp#L140-L149)）发射三目 `cond ? t : f`；`IndexCastOp`（[L151-L163](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/External/Arith.cpp#L151-L163)）发射 `static_cast<T>(v)`；`MulUIExtendedOp`（[L28-L51](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/External/Arith.cpp#L28-L51)）是唯一双结果 Op，发射两条语句：低位 `a * b`、高位用 64 位中间量右移得到。

**（3）常量的公共出口**。`arith::ConstantOp`、`func::ConstantOp`、`emitc::ConstantOp`、`emitc::VariableOp` 四者共用 [lib/Target/AscendC/Common.cpp:L19-L42](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/Common.cpp#L19-L42) 的 `printConstantOp`：先打 `constexpr` 前缀——**但 float16 例外**（半精度常量不能 `constexpr`，见开头的位宽判断），再走 `emitAssignPrefix` + `emitAttribute` 打印字面值。浮点字面值的打印细节在 [CodeEmitter.cpp:L288-L312](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/CodeEmitter.cpp#L288-L312)：有限值按语义加 `(float)`/`(double)` 前缀且不截断尾零，NaN/无穷用 `0.f/0.f`、`__builtin_inff()` 合成——这些写法都是为了在目标编译器上精确复现位级相同的常量。

**（4）arith.mlir：现成的映射表**。[test/Target/AscendC/arith.mlir:L9-L38](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Target/AscendC/arith.mlir#L9-L38) 一口气断言了 13 个二元运算的期望输出（`addi→+`、`subi→-`、`muli→*`、`divsi→/`、`remsi→%`、`ceildivsi→(a+b-1)/b`、`shli→<<`、`shrsi/shrui→>>`、`andi→&`、`ori→|`、`xori→^`、`divui→/`），[L40-L68](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Target/AscendC/arith.mlir#L40-L68) 是 10 个比较谓词。这张表就是「IR 操作 → C 代码」的权威字典。

**（5）math/memref/emitc 速览**。math 与 arith 同构，也是「头文件模板 + cpp 特化」两段：[include/ascir/Target/Asc/External/Math.h:L18-L77](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Target/Asc/External/Math.h#L18-L77) 的 unary 模板管 13 个一元数学 Op、`Atan2Op` 单独成模板，绝大多数映射为 `AscendC::Xxx(...)` 命名空间调用（昇腾上标量数学复用向量 API 基建），三个例外值得记：`sqrt` 映射为普通 `sqrt(...)`、`exp2` 展开为 `Exp(x * Log(2))`、`absf` 展开为三目表达式；Math.cpp 只放非模板的 `FmaOp`（→ `a * b + c`）与 `CopySignOp`（→ 三目表达式）（[lib/Target/AscendC/External/Math.cpp:L15-L36](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/External/Math.cpp#L15-L36)）。MemRef.cpp 四条：`alloca` 打数组声明、`load/store` 打 `mem[i]`、`cast` 打 `reinterpret_cast`（[lib/Target/AscendC/External/MemRef.cpp:L15-L59](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12a6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/External/MemRef.cpp#L15-L59)）。Emitc.cpp 四条：常量、C 风格括号强转、verbatim、`#include`（标准头打 `<>`、其余打 `""`，[lib/Target/AscendC/External/Emitc.cpp:L55-L67](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/External/Emitc.cpp#L55-L67)）——u6-l4 讲过的「include 由 Pass 种入 emitc.include、发射层落纸」即此。

**（6）类型到 C 类型**。所有 `emitType` 的总入口在 [lib/Target/AscendC/CodeEmitter.cpp:L835-L854](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/CodeEmitter.cpp#L835-L854)：先查 `emitTypeMapper`（TypeID → 专属发射器，PyStruct、各 Tensor 类型等），未命中再按 Integer/Float/MemRef 兜底。整数最终在 [L798-L800](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/CodeEmitter.cpp#L798-L800) 拼成 `int32_t`/`uint32_t`（按符号性），`index` 类型固定为 `uint32_t`（[L405-L408](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/CodeEmitter.cpp#L405-L408)）。

#### 4.3.4 代码实践

**实践 C：整理你的算术映射表。**

1. 实践目标：把 arith → C 的映射内化成可查表。
2. 操作步骤：
   - 通读 [Arith.h:L18-L87](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Target/Asc/External/Arith.h#L18-L87) 与 [arith.mlir](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Target/AscendC/arith.mlir#L9-L68)；
   - 自制两列对照表：左列 Python 表达式（`a + b`、`a // b`、`a % b`、`ceildiv(a,b)`、`a << b`、`a > b`），中间列写出你预测的 IR 操作名（提示：u4-l2 的四张运算符翻译表 + 本讲 4.3.2），右列写出预测的 C 表达式；
   - 与 Arith.h 的 `if constexpr` 链逐条核对。
3. 需要观察的现象：`ceildiv`（u2-l5 提及的 `asc.ceildiv`，NameScope builtins 白名单成员）与普通 `//` 在 C 输出上的差别；`a > b` 的结果是 `bool`（i1）而非 int。
4. 预期结果：约 10 行的对照表，全部能在 Arith.h/Arith.cpp 中指出依据行。

#### 4.3.5 小练习与答案

**练习 1**：为什么二元运算模板要放在头文件（Arith.h）而不是 Arith.cpp？

**参考答案**：函数模板必须对每个实例化点可见。24 个 Op 类型在 `Translation.cpp` 的 TypeSwitch 里被实例化，模板定义必须随头文件到达那里；而 Arith.cpp 只放非模板的普通函数（CmpI/Select 等），声明在头、定义在 cpp 即可。

**练习 2**：`arith.cmpi ult`（无符号小于）和 `slt`（有符号小于）的 C 输出都是 `<`，语义靠什么保证？

**参考答案**：靠**操作数类型**。发射层把 `uint32_t a < b` 与 `int32_t a < b` 打成同样的 `<` 符号，无符号/有符号语义由 C 类型系统承载；而 IR 里整数类型的符号性（`ui32` vs `i32`）在 `emitType` 时已经分流成 `uint32_t`/`int32_t`（CodeEmitter.cpp:798-800）。即「谓词差异下沉为类型差异」。

**练习 3**：`math.sqrt` 与 `math.exp` 走哪条路发射？两者输出有何区别？

**参考答案**：都走 [External/Math.h:L18-L60](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Target/Asc/External/Math.h#L18-L60) 的 unary 模板（不在 Math.cpp——那里只有非模板的 Fma/CopySign）。`math.sqrt` 是该模板里的少数例外，发射为普通 C 函数 `sqrt(x)`；而 `math.exp` 发射为 `AscendC::Exp(x)` 命名空间调用。区别的原因：昇腾侧大多标量数学要复用向量 API 基建（带命名空间），个别（如 sqrt）直接用平台 C 库函数即可。

### 4.4 scf/func 到 C 的映射：跟踪一个 for 循环的完整降级

#### 4.4.1 概念说明

本模块回答学习目标里的第三条：**Python `for` 循环如何一步步变成 C `for` 语句**。这条链横跨前端（u4 单元讲过其 AST 侧）与发射层（本讲），第一次把它们首尾接起来。同时覆盖 `func.call`/`func.return`/`func.func` 三个函数级操作的发射——其中 FuncOp 正是 ascendc.cpp 里 `extern "C" __global__ __aicore__` 样板的落纸点（u6-l4 讲过属性是谁种的，这里看谁打印）。

#### 4.4.2 核心流程

**前端侧（发生 JIT 编译期）**：`visit_For`（[python/asc/codegen/function_visitor.py:L479-L516](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/codegen/function_visitor.py#L479-L516)）：

1. 只接受 `range` / `asc.static_range`，`for-else` 直接拒绝；
2. `static_range` 走 `handle_static_range` 编译期完全展开（不产生循环 IR，u4-l3）；
3. `range` 的 start/stop/step 经 `materialize_ir_value` 落成 `int32` 常量（或运行时值）；
4. `compute_inout` 分析循环体改写了哪些外层变量，作为块进块出值（iter_args）；
5. `create_scf_ForOp` 建循环，循环变量存进 NameScope，循环末尾补 `create_scf_YieldOp` 回传块出值。

**发射侧（translate 阶段）**：`printOperation(CodeEmitter&, scf::ForOp)`（[lib/Target/AscendC/External/Scf.cpp:L26-L93](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/External/Scf.cpp#L26-L93)）按固定五步发射：

```text
① 为每个循环结果变量预声明：      int32_t v5;
② 为每个 iterArg 打初始化行：     int32_t v6 = v1;
③ 打 C for 头（+=step）：         for (int32_t v3 = c0; v3 < c16; v3 += c1) {
④  循环体（跳过末尾 yield）…
⑤  循环体末尾回拷 yield→iterArg：  v6 = v9;
                                  }
⑥ 循环后回拷 iterArg→result：     v5 = v6;
```

这个「**预声明 + 三次拷贝**」结构就是 SSA→C 的翻译核心：MLIR 里 `iter_args`/`yield` 表达的循环携带依赖，在 C 里变成了「变量先声明、循环内改、循环后取」。

#### 4.4.3 源码精读

**（1）`scf::ForOp` 发射**。[Scf.cpp:L26-L93](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/External/Scf.cpp#L26-L93)。几个细节：

- 循环头三段式打印在 [L49-L56](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/External/Scf.cpp#L49-L56)：`for (T i = lb; i < ub; i += step) {`——注意步进固定打成 `+=`，且边界比较固定为 `<`（所以 `range(a, b, -1)` 这类负步进会生成语义错误的 C 循环，属于当前实现的约束，**待确认**是否前端已拦截）；
- 循环体跳过末尾 yield（[L62-L71](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/External/Scf.cpp#L62-L71) 的注释解释了原因：yield 的职责被⑤⑥两次显式赋值取代）；
- 结果为空时（如 01_add 的循环，`i`/`buf_id` 都只在体内用）①⑥自动退化为空，只剩一个纯 `for` 头。

**（2）无携带依赖时的实际形态（推导示例）**。01_add 第 49-50 行：

```python
for i in range(TILE_NUM * BUFFER_NUM):   # TILE_NUM=8, BUFFER_NUM=2 → range(16)
    buf_id = i % BUFFER_NUM
```

前端先算 `TILE_NUM * BUFFER_NUM`（编译期 Python 值相乘得 16，u2-l3 讲过纯 Python 常量间运算不产生 IR），故 `range(16)` 的三个边界都是常量。预期 IR 与 C 输出（依据上述源码推导，**示意，待本地验证**）：

```text
IR:                                     C（示意）:
%c0 = arith.constant 0 : i32            constexpr int32_t c0_i32 = 0;
%c16 = arith.constant 16 : i32          constexpr int32_t c16_i32 = 16;
%c1 = arith.constant 1 : i32            constexpr int32_t c1_i32 = 1;
%v3 = arith.remisi %v_i, %c2            （循环体内）
scf.for %i = %c0 to %c16 step %c1 {     for (int32_t v_i = c0_i32; v_i < c16_i32;
  ...                                       v_i += c1_i32) {
}                                         }
```

**（3）`scf::YieldOp` 与 `emitBlock`**。[Scf.cpp:L143-L157](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/External/Scf.cpp#L143-L157)：零操作数的 yield（纯终结符）在 `emitBlock`（[L15-L24](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/External/Scf.cpp#L15-L24)）里被静默跳过；带操作数的 yield 打印成一组 `result = operand;` 赋值，并检查操作数确实在作用域内。

**（4）其余控制流**。`IfOp`（[L95-L121](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/External/Scf.cpp#L95-L121)）：同样先预声明结果变量，再打 `if (...) { } else { }`——u4-l3 讲过的「运行期 if 结果按名字并集合并」在这里落地成 C 的先声明后赋值。`WhileOp`（[L176-L208](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/External/Scf.cpp#L176-L208)）+ `ConditionOp`（[L159-L174](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/External/Scf.cpp#L159-L174)）：MLIR 的 before/do 双区域被翻译成 `while (true) { if (!cond) { ...; break; } ... }`——条件反转 + break，测试样例见 [scf.mlir:L16-L37](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Target/AscendC/scf.mlir#L16-L37)。`IndexSwitchOp`（[L123-L141](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/External/Scf.cpp#L123-L141)）直译 `switch/case/default`，样例见 [scf.mlir:L100-L129](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Target/AscendC/scf.mlir#L100-L129)。

**（5）func 三件套**。[Func.cpp:L53-L114](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/External/Func.cpp#L53-L114) 的 `FuncOp` 发射函数骨架：开 `CodeEmitter::Scope`（变量作用域压栈），依据 `ascendc.global` 属性二选一前缀——Kernel 打 `extern "C"  __global__ __aicore__`，Device 子函数打 `__inline__ __attribute__((always_inline)) __aicore__`（u4-l4 讲过的内联策略在此落纸）；随后依次发射返回类型、函数名、参数表、（多块时的）块标签与块参数声明、逐操作体。`CallOp`（[L23-L36](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/External/Func.cpp#L23-L36)）发射 `callee(args)`，有结果时先 `emitAssignPrefix`；`ReturnOp`（[L38-L51](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/External/Func.cpp#L38-L51)）发射 `return`/`return v`，多返回值未实现（直接 `llvm_unreachable`）。

**（6）限制：单块函数**。`FuncOp` 开头即检查 `getBlocks().size() > 1` 则报错 "needs variables declared at top"（[L55-L58](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/External/Func.cpp#L55-L58)）——pyasc 前端只生成结构化控制流（scf），不做 CFG 级分支，因此函数体天然单块，这条防线确保意外混入多块 IR 时快速失败。

#### 4.4.4 代码实践

**实践 D：亲手跑通三列对照（本讲核心实践）。**

1. 实践目标：对 01_add 完成学习目标 4 的三列对照表。
2. 操作步骤：
   - 环境就绪后（u1-l2/u1-l4），执行：
     ```bash
     cd examples/01_add
     PYASC_DUMP_PATH=/tmp/pyasc_dump python3 add.py -r Model
     ```
   - 打开 `/tmp/pyasc_dump` 下的 `codegen.mlir`（Pass 前）与 `ascendc.cpp`；
   - 针对 kernel 里这五行逐行追踪（[examples/01_add/add.py:L31-L50](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/examples/01_add/add.py#L31-L50)）：
     1. 第 31 行 `offset = asc.get_block_idx() * block_length`；
     2. 第 35 行 `x_gm.set_global_buffer(x + offset, block_length)` 中的 `x + offset`；
     3. 第 39 行 `tile_length = block_length // TILE_NUM // BUFFER_NUM`；
     4. 第 49 行 `for i in range(TILE_NUM * BUFFER_NUM)`；
     5. 第 50 行 `buf_id = i % BUFFER_NUM`。
3. 需要观察的现象：每条 Python 语句在 `codegen.mlir` 中对应哪些操作（预期：①`ascendc.GetBlockIdxOp` + `arith.muli`/`index_cast`；②`arith.index_cast` + `emitasc.ptr_offset` + `ascendc.SetGlobalBufferOp`；③两条 `arith.divsi`；④三个 `arith.constant` + `scf.for`；⑤`arith.remsi`），以及这些操作在 `ascendc.cpp` 中生成的行（预期：①乘法赋值行；②指针加法行；③两条除法赋值行；④`constexpr` 常量行 + `for (;;)` 头；⑤取模赋值行）。
4. 预期结果：形如下式的表格（C 列为依据本讲源码推导的示意，具体变量名/行号**待本地验证**）：

   | Python 行 | IR 操作 | C 代码（示意） |
   |---|---|---|
   | `offset = asc.get_block_idx() * block_length` | `ascendc.get_block_idx` + `arith.muli` | `uint32_t v2 = v1 * v_arg;` |
   | `x + offset` | `arith.index_cast` + `emitasc.ptr_offset` | `__gm__ float* v3 = v_ptr + v2;` |
   | `block_length // 8 // 2` | `arith.divsi` ×2 | `int32_t v4 = v_arg / c8;` 等 |
   | `for i in range(16)` | `arith.constant` ×3 + `scf.for` | `for (int32_t v6 = c0; v6 < c16; v6 += c1) {` |
   | `buf_id = i % 2` | `arith.remsi` | `int32_t v7 = v6 % c2;` |

5. 附加验证：把 `TILE_NUM` 改成 4 重跑，确认 `scf.for` 上界常量与 C 头部的 `c8/c16` 相应变化；再换成 `asc.static_range` 版本（u4-l3 实践做过），确认 `ascendc.cpp` 中循环消失、语句按份展开。

#### 4.4.5 小练习与答案

**练习 1**：`scf.for` 的结果变量为什么必须在循环前声明、循环后赋值，而不能像 C 一样「在循环体内最后一次赋值自然生效」？

**参考答案**：MLIR 的 ForOp 是表达式，其 result 在循环结束后作为 SSA 值被后续操作使用；C 里没有对应物，只能拆成「提前声明变量（供后续引用）+ 循环体内更新 + 循环后从 iterArg 拷贝」三步。若只在循环体内赋值，当循环零次执行时 C 变量未初始化，而 SSA 语义要求 result 恒有定义（iterArg 初值），所以循环后的拷贝不可省。

**练习 2**：`func.return` 带多个操作数会怎样？这和前端哪条规则呼应？

**参考答案**：发射层 `llvm_unreachable` 直接崩溃级失败（Func.cpp:49）。前端侧呼应 u4-l3/u4-l4 的规则：return 只能在函数顶层、Kernel 不能返回对象、返回值至多一个——前端约束保证了发射层不会遇到多返回值。

**练习 3**：Kernel 函数与 Device 子函数的 C 前缀差异是什么？分别由哪个属性驱动？

**参考答案**：Kernel 是 `extern "C"  __global__ __aicore__`（外部可见、可被 Launcher 按 ABI 调起），Device 子函数是 `__inline__ __attribute__((always_inline)) __aicore__`（私有、强制内联）。驱动属性是 `ascendc.global`——由前端 `make_global()` 在 `visit_FunctionDef` 打上（function_visitor.py:536-537），FuncOp 发射函数读取它选择前缀（Func.cpp:63-67）。这正是 u6-l4「Pass/前端种属性、发射层落纸」模式的又一实例。

## 5. 综合实践

**任务：为 01_add 产出一份《Python → IR → C 全链路追踪报告》。**

把 4.4.4 实践 D 扩充成完整报告，要求覆盖本讲全部三个模块：

1. **准备**：`PYASC_DUMP_PATH=/tmp/pyasc_dump python3 examples/01_add/add.py -r Model`，收集 `codegen.mlir`、`ascir.mlir`、`ascendc.cpp` 三份产物。
2. **External 部分**：从 `ascendc.cpp` 中挑出 5 条由 arith 发射的行（至少含一条除法、一条取模、一条比较或常量），在 `codegen.mlir` 中找到对应 `arith.*` 操作，在 Arith.h/Arith.cpp 中指出依据行号，写进三列对照表。
3. **scf/func 部分**：定位 `scf.for` 及其生成的 C `for` 头，记录归纳变量、上下界、步进在 IR 与 C 两侧的名字；定位 kernel 函数的 `extern "C" __global__` 前缀与 `func.return` 生成的 `return`。
4. **EmitAsc 部分**：在 `codegen.mlir` 中搜索 `emitasc.` 前缀，预期至少能找到 `set_global_buffer(x + offset, ...)` 附近的 `emitasc.ptr_offset`（来自 `GlobalAddress.__add__`）；把该操作的 IR 文本、Ops.td 定义行、EmitAsc.cpp 发射行、最终 C 行写成一行四列记录。
5. **扩展实验**：向 kernel 添加 `asc.inline("// traced by u6-l6")` 与一个 `asc.ConstExpr[int]` 形参（参考 u2-l1），重跑并记录：verbatim 文本出现在哪一级产物、ConstExpr 值变化后哪些 C 行随之改变、缓存是否失效（对照 u3-l8）。
6. **交付**：报告以三列对照表为主体，每个条目标注源码永久链接；无法本地运行的部分明确标注「待本地验证」。

若本地没有可运行环境，可将 2、3 两步降级为「纸面版」：以 [test/Target/AscendC/arith.mlir](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Target/AscendC/arith.mlir#L9-L38)、[scf.mlir](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Target/AscendC/scf.mlir#L16-L37)、[emitasc.mlir](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/test/Target/AscendC/emitasc.mlir#L9-L101) 的 CHECK 注释为「已验证输出」，同样能完成对照表。

## 6. 本讲小结

- **EmitAsc 是自研的「贴近 C 语法的低层桥梁」**：12 个 Op 补齐 emitc 覆盖不到、Asc 方言又不该管的 C++ 语法（指针偏移、成员访问、reinterpret_cast、可变变量、PyStruct 声明与拷贝、原生调用、原样文本），它不是 Pass 降级的产物，而是前端直接创建、Pass 种入、测试手写三个来源共用。
- **发射层面对三类客户、一套机制**：ascendc Op（TableGen 生成为主）、上游方言（External/ 六文件手写翻译）、emitasc Op（12 个手写重载），全部经 `PrintableOpTypes` 白名单 + TypeSwitch 分发，未登记即 "unable to find printer for op"。
- **External 的组织模式**：arith 用「头文件模板管 24 个规则二元运算 + cpp 特化不规则者」，比较/选择/ casts 各自特化，常量四类 Op 共用 `printConstantOp`（`constexpr` 前缀、float16 例外、NaN/Inf 特造字面量）。
- **SSA→C 的通用手法是「先声明、后赋值」**：`emitAssignPrefix` 服务单结果 Op；`scf.for` 的 iter_args/yield 被翻译成「结果预声明 + iterArg 初始化 + 循环尾回拷 + 循环后回拷」四次动作；`while` 用 `while(true)+if(!cond)break` 表达；`index_switch` 直译 switch。
- **`func.func` 是样板的落纸点**：`ascendc.global` 属性决定 Kernel（`extern "C" __global__ __aicore__`）与 Device 子函数（always_inline）两种前缀；多块函数被显式拒绝，因为前端只产结构化控制流。
- **调 C 输出的检索口诀**：先查 test/Target/AscendC/*.mlir 的 CHECK 注释（现成对照），再查 External/*.cpp（上游方言）→ EmitAsc.cpp（emitasc）→ Ops.td（定义），三步内必中。

## 7. 下一步学习建议

本讲完成后，第 6 单元（Pass 优化与 Ascend C 代码生成）全部结束，你已经打通「Python 源码 → AST → ASC-IR → Pass → Ascend C → C 代码行」的全链路。接下来进入第 7 单元：

1. **u7-l5（开发者工具）**：动手构建 `ascir-opt`/`ascir-translate`，用 `ascir-translate -mlir-to-ascendc` 手工翻译一份 .mlir，把本讲的「纸面版」实践升级为可执行验证——这是巩固本讲最直接的后续。
2. **u7-l1（Matmul 高阶 API）**：观察 `ascendc.matmul` 一族 Op 如何依赖本讲的 emitasc.ptr_offset/member 完成对象式 API 的发射，检验你对「语句级 API 调用 + C 语法胶水」混合发射的理解。
3. **u7-l6（测试与贡献）**：若想为 pyasc 新增发射能力，本讲的 test/Target/AscendC 目录就是贡献格式范本——新 Op 必须同时补 td 定义、printOperation 与 CHECK 测试，三件缺一不可。
