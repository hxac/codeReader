# Ascend C 代码发射：CodeEmitter 与 Translation

## 1. 本讲目标

上一讲（u6-l4）我们看到：postprocessing 阶段的各个 Pass 把「样板代码」以 IR 属性和特殊 Op 的形式「种」进模块，而不是用 Python 拼字符串。本讲顺着这条线走完最后一公里——**ASC-IR（MLIR 模块）如何被逐个 Operation 翻译成 Ascend C 源码文本**，也就是 `PYASC_DUMP_PATH` 导出的 `ascendc.cpp` 的诞生过程。

学完本讲，你应该能够：

1. 理解「一个 Op → 一条（或一小段）Ascend C 语句」的发射模型，以及失败如何用 `LogicalResult` 逐层向上传播。
2. 掌握 `CodeEmitter` 提供的公共打印原语：类型发射、属性发射、变量声明、赋值前缀。
3. 掌握 `EmitNameStack` 与 `Scope` 如何保证生成的 C++ 变量名在嵌套作用域中不冲突。
4. 给定 dump 出的 `ascendc.cpp` 中任意一条 `AscendC::Xxx(...)` 调用，能定位到生成它的发射函数所在的源码文件，并判断它是「手写发射」还是「TableGen 生成发射」。

## 2. 前置知识

本讲是纯 C++ 后端内容，开始前请先确认理解以下几个概念（前几讲已铺垫，这里只做回顾）：

- **LogicalResult**：MLIR 的三态返回值（`success()` / `failure()`）。发射层所有函数都返回它，任何一步失败都会让整次翻译失败。宏 `FAIL_OR(expr)` 表示「若 expr 失败则立即 return failure()」，是发射代码里最常见的句式。
- **TypeSwitch**：LLVM 提供的「按 C++ 类型分派」工具，相当于针对 Operation 具体类型的 `switch`。MLIR 里遍历异构 Operation 的标准写法。
- **ScopedHashTable（作用域哈希表）**：LLVM 提供的数据结构，插入的键值对挂在「当前作用域」上，作用域栈弹出时整批消失。它天然贴合 C++ 的词法作用域，是发射器管理「IR Value → C++ 变量名」映射的关键。
- **RAII**：构造即获取资源、析构即释放。`CodeEmitter::Scope` 用它来模拟 `{ }` 的进出。
- **genEmitter / Asc* trait**（承接 u5-l4）：td 里带 `AscConstructor` / `AscMemberFunc` / `AscFunc` 任一 trait 的 Op，其发射函数由 TableGen 后端 `-gen-op-emit-defs` 自动生成；不带的 Op 走手写发射。本讲会在发射层验证这条判据。
- **APIOpInterface**（承接 u5-l3）：每个 ascendc Op 都实现该接口，提供 `getAPIName()`（如 `"Add"`、`"DataCopy"`）与 `getComment()`（注释文本）。

另外提醒一个容易混淆的点：**发射（Emit）不等于打印 IR**。`ascir-opt` 打印的是 MLIR 文本（`ascendc.add_l2 ...`），而本讲的发射器输出的是 C++ 源码（`AscendC::Add(...)`），两者共享 `printOperation` 这个名字但完全是两套东西。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `lib/Target/AscendC/Translation.cpp` | 发射总入口：`translateToAscendC`、`emitOperation` 分发器、`PrintableOpTypes` 可发射 Op 清单 |
| `include/ascir/Target/Asc/CodeEmitter.h` / `lib/Target/AscendC/CodeEmitter.cpp` | 发射器主体：类型/属性发射映射表、命名管理、`Scope` |
| `include/ascir/Target/Asc/EmitNameStack.h` / `lib/Target/AscendC/EmitNameStack.cpp` | 变量名生成栈：`v1/v2/...` 与 `c8_i32` 风格命名 |
| `include/ascir/Target/Asc/Common.h` / `lib/Target/AscendC/Common.cpp` | 发射层公共工具：`FAIL_OR`、`printMask`、模板参数打印、常量打印 |
| `include/ascir/Target/Asc/Basic/VecBinary.h` | 手写发射示例一：Add 等 20+ 个双目向量算子的 C++ 模板族 |
| `include/ascir/Target/Asc/Fwk/TQue.h` / `lib/Target/AscendC/Fwk/TQue.cpp` | 手写发射示例二：TQueBind 队列成员函数（`AllocTensor` / `DequeTensor` 等） |
| `include/ascir/Target/Asc/Basic/DataCopy.h` / `lib/Target/AscendC/Basic/DataCopy.cpp` | 手写发射示例三：DataCopySlice / CopyL0 / CopyL1 |
| `include/ascir/Target/Asc/UniversalEmitter.h` | 生成发射的运行期兜底：`autoPrintOp` 三分支模板 |
| `lib/TableGen/GenOpEmitDefs.cpp` | 生成发射的编译期源头：为带 trait 的 Op 生成 `printOperation` 函数体 |
| `lib/Target/AscendC/External/Func.cpp` | 函数级发射：`extern "C" __global__ __aicore__` 样板从哪来 |
| `python/src/Translation.cpp` / `python/asc/runtime/compiler.py` | Python 桥接：`translation.ir_to_ascendc` 的暴露与调用点 |
| `bin/ascir-translate.cpp` | 命令行翻译工具（代码实践要用） |

目录规律（承接 u1-l3 的「目录镜像」）：发射头文件 `include/ascir/Target/Asc/{Adv,Basic,Core,Fwk,External}/Xxx.h` 与实现 `lib/Target/AscendC/` 下同名目录一一对应，而这又与 `language`、Dialect 的 `IR` 目录四象限一致。检索口诀：**dump 里的 `AscendC::Xxx` 调用 → 先查 `lib/Target/AscendC/<象限>/<Api>.cpp` 有没有手写；没有就去看 td 的 trait，走生成线**。

## 4. 核心概念与源码讲解

### 4.1 Translation 入口：emitOperation 分发与失败传播

#### 4.1.1 概念说明

ASC-IR 经过全部 Pass 之后是一个标准的 `ModuleOp`。「发射」就是把这个模块树**前序遍历**，每个 Operation 交给一个与它 C++ 类型匹配的 `printOperation` 重载，由后者向输出流写出对应的 C++ 代码。

设计上有两个关键决策：

1. **白名单式分派**：不是「任何 Op 都能试着发射」，而是维护一张显式的可发射类型清单 `PrintableOpTypes`。不在清单上的 Op 直接报 `unable to find printer for op` 并整次失败。这保证了「生成的 C++ 一定来自被审过的发射函数」。
2. **结构化递归**：顶层入口只发射 ModuleOp 的直接子节点（通常是函数和 include）；函数体内部的遍历由 `func::FuncOp` 的发射函数自己递归调用 `emitOperation` 完成。分号、缩进这类「跨节点」的排版由外层统一处理。

#### 4.1.2 核心流程

```
translateToAscendC(rootOp, os)                      # 入口
  └─ emitter = CodeEmitter(os)                      # 建发射器（初始化两张映射表）
  └─ emitOperation(emitter, rootOp, 分号=false)

emitOperation(emitter, op, trailingSemicolon)
  ├─ 若 op 实现了 APIOpInterface 且 comment 非空 → 先写一行 "// comment"
  ├─ TypeSwitch<Operation*> 按 PrintableOpTypes 逐类型 Case：
  │     命中 → 调 printOperation(emitter, 具体Op类型)   # 手写或生成
  │     全部未命中 → emitOpError("unable to find printer for op") → failure
  ├─ 成功 → 追加 trailingSemicolon ? ";\n" : "\n"
  └─ 任一环节 failure → 向上传播，最终 Python 侧抛 runtime_error
```

#### 4.1.3 源码精读

**入口只有两行**——构造发射器，然后发射根节点：

- [lib/Target/AscendC/Translation.cpp:295-299](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/Translation.cpp#L295-L299)：`translateToAscendC` 创建 `CodeEmitter` 并调用 `emitOperation`，分号参数为 `false`（模块本身不是语句）。

**模块级发射**：开一个 `Scope`（作用域压栈，见 4.4），遍历模块的直接子 Op：

- [lib/Target/AscendC/Translation.cpp:58-68](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/Translation.cpp#L58-L68)：对模块里每个顶层 Operation（函数、`emitc.include` 等）调用 `emitOperation`，任何一个失败则整次翻译失败。

**可发射清单 `PrintableOpTypes`** 是一个巨型 `std::tuple` 类型列表，按注释分组：

- [lib/Target/AscendC/Translation.cpp:72-98](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/Translation.cpp#L72-L98)：先列**上游方言**——`emitc`（include/常量/verbatim）、`func`、`scf`、`memref`、`arith`、`math`，以及 pyasc 自研的贴近 C 语法的 `emitasc` 方言（u6-l6 主角）。
- [lib/Target/AscendC/Translation.cpp:98-234](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/Translation.cpp#L98-L234)：再按 Adv / Basic / Core / Fwk 四象限**手工登记手写发射的 ascendc Op**——例如 `ascendc::AddL0Op` 到 `AddL3Op` 全家族在 149-166 行、TQueBind 家族在 228-233 行。
- [lib/Target/AscendC/Translation.cpp:235-238](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/Translation.cpp#L235-L238)：关键三行——`#define GET_OP_TYPE_LIST` 后 include 生成文件 `AscendCOpEmit.h.inc`，把**所有带发射 trait 的 Op 类型**（u5-l4 讲过的 `-gen-op-emit-defs` 产物）拼进元组；末尾 `NoOp` 是哨兵，对应 70 行的无操作发射。

**分发机器**：C++ 无法直接对 tuple 里的类型循环，所以用模板元编程展开：

- [lib/Target/AscendC/Translation.cpp:240-262](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/Translation.cpp#L240-L262)：`addCases` 用 `std::index_sequence` 把元组的每个类型注册成 TypeSwitch 的一个 `Case`，回调统一是「调用对应 `printOperation` 重载」。手写登记与生成登记在这里汇合成同一条分派路径。

**主分发器 `emitOperation`**：

- [lib/Target/AscendC/Translation.cpp:271-293](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/Translation.cpp#L271-L293)：三段式——(1) 273-278 行：若 Op 实现了 `APIOpInterface` 且带注释，先输出 `// 注释` 一行，这就是 `ascendc.cpp` 里每条 API 调用上方注释的来源；(2) 279-290 行：TypeSwitch 分派，未命中时 `emitOpError("unable to find printer for op")` 返回 failure；(3) 291 行：按需补 `;` 与换行。**分号统一由这里追加**，各 `printOperation` 只写语句本体，不写分号。

**失败传播到 Python**：

- [python/src/Translation.cpp:32-39](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/src/Translation.cpp#L32-L39)：pybind 暴露的 `ir_to_ascendc` 把 IR 翻译到字符串，`translateToAscendC` 失败即抛 `runtime_error("Failed to translate IR to Ascend C")`。
- [python/asc/runtime/compiler.py:117](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/python/asc/runtime/compiler.py#L117)：Python 侧编译器在跑完 Pass 后调用 `translation.ir_to_ascendc(mod)` 拿到 `ascendc.cpp` 文本——Python 主链路并不启动独立进程，翻译在进程内完成。

**函数级发射（样板的最终落点）**：u6-l4 讲过 `GenerateBoilerplatePass` 给 Kernel 函数打 `ascendc.global` 属性，真正打印入口签名的是这里：

- [lib/Target/AscendC/External/Func.cpp:53-114](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/External/Func.cpp#L53-L114)：`printOperation(CodeEmitter&, func::FuncOp)` 先为函数开新 `Scope`，63 行读取 `ascendc::attr::global` 判断是否 Kernel；
- [lib/Target/AscendC/External/Func.cpp:66-69](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/External/Func.cpp#L66-L69)：Kernel 打 `extern "C"  __global__ __aicore__`，Device 子函数打 `__inline__ __attribute__((always_inline))`（呼应 u4-l4 的「真内联交给毕昇」）；随后发射返回类型、函数名、形参列表并进入函数体；
- [lib/Target/AscendC/External/Func.cpp:99-109](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/External/Func.cpp#L99-L109)：逐块逐 Op 递归调用 `emitOperation`，是否补分号由 `needsSemicolon` 决定（if/for 等块级语句不补）。

#### 4.1.4 代码实践

**实践目标**：在真实 dump 产物上验证「注释行、语句、分号」三个细节各自来自分发器的哪一行代码。

**操作步骤**：

1. 设置 `export PYASC_DUMP_PATH=/tmp/pyasc-dump`，在 Model 模式下运行 `examples/01_add/add.py`（方法见 u1-l4/u1-l5）。
2. 打开导出的 `ascendc.cpp`，定位到 Kernel 入口行（形如 `extern "C"  __global__ __aicore__ void ...`）。
3. 在函数体里找到一条 `AscendC::Add(...)` 调用及其上方的 `//` 注释行（若有）。
4. 对照本节源码，给三处细节标注来源：入口行 ← `Func.cpp:66`；注释行 ← `Translation.cpp:273-278`；行尾分号 ← `Translation.cpp:291`。

**需要观察的现象**：API 调用上方是否稳定出现以 `//` 开头的说明文字；`scf.for` 生成的 `for` 语句末尾**没有**分号而普通调用有。

**预期结果**：三个来源标注都能对上。若注释行缺失，说明该 Op 的 td 未填 comment 字段——这本身也是一个有效观察。

**待本地验证**：本实践需要可运行环境（Model 仿真模式即可，无需 NPU）；具体输出内容以本地 dump 为准。

#### 4.1.5 小练习与答案

**练习 1**：一个新定义的 ascendc Op 如果既没写手写发射函数、td 里也没加任何 Asc* trait，翻译时会怎样？

答案：它的 C++ 类型不在 `PrintableOpTypes` 元组里（手写清单没登记、生成的 `AscendCOpEmit.h.inc` 也不含它），TypeSwitch 走到 `Default` 分支，触发 `emitOpError("unable to find printer for op")` 返回 failure，Python 侧抛出 `Failed to translate IR to Ascend C`。

**练习 2**：为什么 `emitOperation` 里分号由外层统一追加，而不是让每个 `printOperation` 自己写？

答案：不同 Op 对分号的需求不同——普通表达式语句要 `;`，而 `scf.if`/`scf.for` 这类生成 `{ }` 块的语句不能带分号。外层用 `trailingSemicolon` 参数（配合 `needsSemicolon` 判断）集中处理，各发射函数只负责语句本体，职责单一且不会漏写/多写。

**练习 3**：`printOperation(CodeEmitter&, ascendc::NoOp)`（Translation.cpp:70）为什么直接 `return success()`？

答案：`NoOp` 是清单末尾的哨兵类型，语义就是「什么都不生成」。返回 success 让它安静地被跳过，同时保证元组永远非空、分发机器有统一收尾。

### 4.2 CodeEmitter：类型/属性发射与公共打印原语

#### 4.2.1 概念说明

各 Op 的 `printOperation` 千差万别，但它们需要的底层能力是共同的：把 MLIR **Type** 打成 C++ 类型名（`i16` → `int16_t`、`!ascendc.local_tensor<half>` → `AscendC::LocalTensor<half>`）、把 **Attribute** 打成 C++ 字面量、声明 C++ 变量、给操作数取名字。`CodeEmitter` 就是这些原语的提供者，相当于发射层的「标准库」。

它内部有两张按 `TypeID` 索引的函数映射表（类型表、属性表），复杂类型走表、简单类型走 `dyn_cast` 链、TableGen 生成的参数结构体类型走生成代码——三条路径在 `emitType` 汇合。

#### 4.2.2 核心流程

```
CodeEmitter(os)                     # 构造：填 emitTypeMapper / emitAttributeMapper
emitType(loc, type)
  ├─ 查 emitTypeMapper（TypeID 精确匹配）→ 命中则调用对应 emitAsc*Type
  ├─ IntegerType  → bool / int8_t..int64_t（含无符号判断）
  ├─ FloatType    → half / float / double
  ├─ BaseMemRefType → 先打地址空间（__gm__ 等）再打 "元素类型*"
  ├─ GEN_EMITTER: API/Types.h.inc 生成的参数结构体类型
  └─ 都不中 → emitError("cannot emit type ...") → failure
emitAttribute(loc, attr)            # Float/Integer/Opaque/SymbolRef/TypeAttr 五类
getOrCreateName(val)                # Value → C++ 变量名（4.4 详述）
emitAssignPrefix(op)                # 单结果: "T name = "；零结果: 不打；多结果: 不可达
```

#### 4.2.3 源码精读

**命名空间常量**：所有手写发射都通过它打 `AscendC::` 前缀：

- [include/ascir/Target/Asc/CodeEmitter.h:26](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Target/Asc/CodeEmitter.h#L26)：`ascNamespace = "AscendC"`。

**构造与两张映射表**：

- [lib/Target/AscendC/CodeEmitter.cpp:117-121](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/CodeEmitter.cpp#L117-L121)：构造函数初始化输出流并建表。
- [lib/Target/AscendC/CodeEmitter.cpp:123-173](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/CodeEmitter.cpp#L123-L173)：类型表登记 17 个键——`index`、上游 `emitc` 类型、以及 ascendc 的 TBuf/TBufPool/Queue/QueBind/FixpipeParams/GlobalTensor/Matmul/LocalMemAllocator/LocalTensor/PyStruct/DataCopyPadExtParams/MrgSortSrcList。
- [lib/Target/AscendC/CodeEmitter.cpp:175-192](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/CodeEmitter.cpp#L175-L192)：属性表只登记五类：`FloatAttr`、`IntegerAttr`、`emitc::OpaqueAttr`、`SymbolRefAttr`、`TypeAttr`，实现体在 194-235 行。

**几个代表性类型发射**（对应 dump 里最常见的 C++ 类型）：

- [lib/Target/AscendC/CodeEmitter.cpp:405-408](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/CodeEmitter.cpp#L405-L408)：`index` 一律打成 `uint32_t`。
- [lib/Target/AscendC/CodeEmitter.cpp:463-470](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/CodeEmitter.cpp#L463-L470)：队列类型 → `AscendC::TQue<TPosition::VECIN, 2>`——**pos/depth 被编进类型**（u2-l6 讲过它进 IR 类型），发射时原样拼回模板实参。
- [lib/Target/AscendC/CodeEmitter.cpp:494-503](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/CodeEmitter.cpp#L494-L503) 与 [lib/Target/AscendC/CodeEmitter.cpp:534-543](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/CodeEmitter.cpp#L534-L543)：`GlobalTensor<half>` / `LocalTensor<half>`——元素类型递归走 `emitType`。
- [lib/Target/AscendC/CodeEmitter.cpp:545-671](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/CodeEmitter.cpp#L545-L671)：`emitMatmulConfig` 把 `MatmulConfigAttr` 的四十多个字段逐个打成 `constexpr static MatmulConfig CFG{...};`，枚举字段翻译成 `BatchMode::NONE` 等 C++ 枚举名——这是「Attribute 携带配置、发射层还原」的最大单体案例。
- [lib/Target/AscendC/CodeEmitter.cpp:788-818](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/CodeEmitter.cpp#L788-L818)：整数/浮点基础类型映射：`i1`→`bool`，`i8/i16/i32/i64`→`intN_t`（按符号性可能 `uintN_t`）；`f16`→`half`、`f32`→`float`、`f64`→`double`。
- [lib/Target/AscendC/CodeEmitter.cpp:820-833](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/CodeEmitter.cpp#L820-L833)：memref 先按 memory space 打地址空间关键字（`__gm__` / `__ubuf__` 等，`emitAddressSpace` 在 375-403 行），再打成「元素类型 `*`」——Kernel 的 GM 指针形参就是这么来的。
- [lib/Target/AscendC/CodeEmitter.cpp:835-854](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/CodeEmitter.cpp#L835-L854)：`emitType` 总分派——查表 → 整数 → 浮点 → memref → **`GEN_EMITTER` 引入 `ascir/API/Types.h.inc`**（u5-l2 讲过的上百个参数结构体类型的生成发射宏）→ 兜底报 `cannot emit type`。

**字面量打印**：

- [lib/Target/AscendC/CodeEmitter.cpp:273-312](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/CodeEmitter.cpp#L273-L312)：`printInt`（`i1` 打 `true/false`，其余按进制转字符串）；`printFloat`（有限值前缀 `(float)`/`(double)` 以避免精度截断，NaN 打成 `(0.f / 0.f) /* nan */`，无穷打成 `__builtin_inff()`）。
- [lib/Target/AscendC/Common.cpp:19-42](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/Common.cpp#L19-L42)：`printConstantOp`——非 fp16 常量打 `constexpr` 前缀；空 `OpaqueAttr` 退化为纯变量声明；否则走 `emitAssignPrefix` + `emitAttribute`。

**变量声明与赋值前缀**：

- [lib/Target/AscendC/CodeEmitter.cpp:334-345](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/CodeEmitter.cpp#L334-L345)：`emitVariableDeclaration` 打「类型 + 空格 + 变量名」，若该名字已在作用域内则报错（防重复声明）。
- [lib/Target/AscendC/CodeEmitter.cpp:347-363](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/CodeEmitter.cpp#L347-L363)：`emitAssignPrefix`——零结果什么都不打（纯调用语句）；单结果打 `T name = `；多结果直接 `llvm_unreachable`（pyasc 的 Op 至多一个结果）。
- [lib/Target/AscendC/CodeEmitter.cpp:323-332](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/CodeEmitter.cpp#L323-L332)：`emitOperands` 逐个打印操作数变量名，若操作数不在当前作用域报 `operand value not in scope`——**SSA 的支配关系在这里被强制检查**，IR 里「用了未定义值」无法蒙混过关。

**发射层可以生成「多条语句」**，不只是单表达式——这是读发射代码前要扭转的预期：

- [include/ascir/Target/Asc/Common.h:109-121](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Target/Asc/Common.h#L109-L121)：`printMask` 为数组掩码形态的算子先生成一条局部数组声明 `uint64_t v_dst_mask_list0[] = { ... };`，再返回数组名供调用使用——静态计数器保证多次调用的数组名不冲突。
- [include/ascir/Target/Asc/Common.h:98-107](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Target/Asc/Common.h#L98-L107)：`printIsSetMaskTemplate` 打出 `AscendC::Add<元素类型, isSetMask>` 这样的模板头——**可推导的 `typename T` 从张量类型逆向取出，不可推导的 `isSetMask` 从 UnitAttr 读布尔值**，这正是 u5-l3 参数顺序约定在发射层的落地。
- [include/ascir/Target/Asc/Common.h:44-58](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Target/Asc/Common.h#L44-L58)：`FAIL_OR` 宏与 `LogicalResultForT`——后者用 SFINAE 限制函数模板只对白名单 Op 类型参与重载，防止通用模板被意外选中（下一节的 VecBinary.h 大量使用）。
- [include/ascir/Target/Asc/Common.h:64-68](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Target/Asc/Common.h#L64-L68)：`needsSemicolon`——`scf::IfOp/ForOp/IndexSwitchOp/YieldOp` 生成的代码自带块结构，不加尾分号。

#### 4.2.4 代码实践

**实践目标**：建立「dump 里的 C++ 类型 ↔ `emitType` 分支」的对照能力。

**操作步骤**：

1. 打开 4.1.4 实践中 dump 的 `ascendc.cpp`，在 Kernel 签名与函数体里收集以下 C++ 类型/字面量各至少一处：`__gm__ half*`、`AscendC::LocalTensor<half>`、`AscendC::TQue<AscendC::TPosition::VECIN, 2>`、`int32_t`、`(float)` 前缀的浮点常量。
2. 对每一处在下面四个发射函数中指认来源：`emitBaseMemRefType`（CodeEmitter.cpp:820-833）、`emitAscLocalTensorType`（534-543）、`emitAscQueueType`（463-470）、`emitIntegerType`/`emitFloatType`（788-818）。
3. 做三个纸面预测并到 dump 里验证：`index` 类型形参 → `uint32_t`；`i1` 常量 `true` → 来自 `printInt` 的 `getBoolValue` 分支；若 Kernel 里有 0.5 的 fp32 常量，输出应为 `(float)0.5...`。

**需要观察的现象**：GM 指针形参前的 `__gm__` 关键字；TQue 模板里的位置与深度；`(float)` 强转前缀是否出现。

**预期结果**：五个类型/字面量全部能在源码指认到具体分支。

**待本地验证**：dump 内容取决于示例，`01_add` 不一定同时包含全部形态；缺的项可换 `02_add_framework`（有 TQue）补齐。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `printFloat` 要给 fp32/fp64 字面量加 `(float)`/`(double)` 前缀？

答案：C++ 的浮点字面量默认是 `double`，直接写 `0.5` 赋给 `float/half` 变量可能产生双精度舍入路径；显式前缀让字面量类型与 IR 类型严格一致，避免生成的代码数值行为与 IR 语义有偏差。

**练习 2**：`emitOperands` 报 `operand value not in scope` 意味着 IR 有什么问题？

答案：该操作数对应的值在当前 C++ 作用域里没有对应变量——通常是值的使用点不被定义点支配（use 不 dominated by def），或定义它的变量随作用域弹出而失效。发射器把 SSA 支配关系转译为 C++ 作用域检查，畸形 IR 在这里被拦截。

**练习 3**：`emitType` 的四层结构（映射表 → 基础类型 → memref → GEN_EMITTER 生成）为什么要这样排？

答案：映射表按 `TypeID` 精确命中复杂自定义类型，O(1) 且集中管理；整数/浮点是开放集合（位宽组合多），用 `dyn_cast` 链更自然；memref 需要统一的地址空间处理；上百个 TableGen 生成的参数结构体类型则完全交给生成宏。分层让「手写复杂逻辑」与「批量生成」各管一段，新增简单结构体类型无需改 `CodeEmitter.cpp`。

### 4.3 发射头文件组织：手写与生成两条供给线

#### 4.3.1 概念说明

`printOperation` 的**定义**有两条供给线，最终都汇入 4.1 的 TypeSwitch：

1. **手写线**：`include/ascir/Target/Asc/<象限>/<Api>.h` 声明 + `lib/Target/AscendC/<象限>/<Api>.cpp` 实现，并且必须**手工**把 Op 类型加进 `Translation.cpp` 的 `PrintableOpTypes` 元组。适合形态特殊（多语句、数组掩码、模板参数复杂）的 Op。
2. **生成线**：td 带 `AscFunc` / `AscMemberFunc` / `AscConstructor` trait 的 Op，由 `-gen-op-emit-defs` 生成声明（进 `AscendCOpEmit.h.inc`）和定义（进 `AscendCOpEmit.cpp.inc`），构建时自动并入清单。适合参数形态规整的大批量 API。

判据**只看 td**（承接 u5-l4 的结论）：有 trait → 生成；无 trait → 手写。本节用三个真实家族验证。

#### 4.3.2 核心流程

```
PrintableOpTypes 的两个来源:
  手工列出（Translation.cpp:72-234）        ← 手写线的 Op
  GET_OP_TYPE_LIST + AscendCOpEmit.h.inc    ← 生成线的 Op（genEmitter=1）

printOperation 定义的两种产出:
  手写 .cpp（如 VecBinary.h 模板 / TQue.cpp / DataCopy.cpp）
  生成的 AscendCOpEmit.cpp.inc:
      paramTypeLists 非空 → 生成完整逐参打印代码
      paramTypeLists 为空 → 一行委托: return autoPrintOp<Op>(emitter, op);
                                └─ 按 trait 三分派:
                                   AscConstructor → 只发变量声明
                                   AscMemberFunc  → obj.ApiName(args)
                                   AscFunc        → AscendC::ApiName(args)
```

#### 4.3.3 源码精读

**手写线示例一：VecBinary.h 的模板族（`AscendC::Add` 从这来）**。

头文件里是 C++ 函数模板，靠 `LogicalResultForT` 白名单限定服务对象：

- [include/ascir/Target/Asc/Basic/VecBinary.h:23-47](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Target/Asc/Basic/VecBinary.h#L23-L47)：三个参数列表助手——L0 打 `(dst, src0, src1, mask, repeatTimes, repeatParams)`，L1 把 mask 换成数组名，L2 只剩 `(dst, src0, src1, calCount)`。**参数顺序完全遵循 u5-l3 的「运行时必选→模板必选→运行时可选→模板可选」重排**。
- [include/ascir/Target/Asc/Basic/VecBinary.h:68-80](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Target/Asc/Basic/VecBinary.h#L68-L80)：L2 形态——只打 `AscendC::Add(dst, src0, src1, calCount)`，**不带模板参数**（C++ 侧由 dst 推导 T），01_add 这类「连续 count」用法走的就是它。
- [include/ascir/Target/Asc/Basic/VecBinary.h:82-92](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Target/Asc/Basic/VecBinary.h#L82-L92)：L0 形态——先 `printIsSetMaskTemplate` 打 `AscendC::Add<half, false>` 模板头，再接参数列表。
- [include/ascir/Target/Asc/Basic/VecBinary.h:94-105](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Target/Asc/Basic/VecBinary.h#L94-L105)：L1 形态——先 `printMask` 生成掩码数组语句，再打模板头与参数。
- [include/ascir/Target/Asc/Basic/VecBinary.h:128-136](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Target/Asc/Basic/VecBinary.h#L128-L136)：L3 形态——打成运算符风格 `dst = src0.Add(src1)`，对应张量上的运算符重载（u2-l5 提过 L3 目前无 Python 前端入口）。
- 一个模板服务一族：94-105 行的 L1 模板白名单覆盖 Add/Sub/Mul/Max/Min/Or 等 13 个同构 Op，这正是 u5-l3「一个 C++ 发射模板服务约 20 个同族 Op」的出处。

**手写线示例二：TQue 队列成员函数（`queue.AllocTensor` 从这来）**。

- [include/ascir/Target/Asc/Fwk/TQue.h:23-35](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Target/Asc/Fwk/TQue.h#L23-L35)：头文件只有 7 个声明——TQueBind 家族的 6 个成员函数 Op 加 `ToQueBindOp`。
- [lib/Target/AscendC/Fwk/TQue.cpp:20-29](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/Fwk/TQue.cpp#L20-L29)：`AllocTensor` 先 `emitVariableDeclaration` 声明结果变量，再打 ` = 队列变量.AllocTensor<元素类型>()`——成员调用语法（对象在前）与 apiName 来自 Op 属性。
- [lib/Target/AscendC/Fwk/TQue.cpp:62-75](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/Fwk/TQue.cpp#L62-L75)：`DequeTensor` 带 pos 版本把 `srcUserPos/dstUserPos` 两个枚举打进模板实参——枚举经 `emitTPosition` 还原为 `AscendC::TPosition::VECIN` 形式（CodeEmitter.cpp:49-77）。
- [lib/Target/AscendC/Fwk/TQue.cpp:88-94](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/Fwk/TQue.cpp#L88-L94)：`ToQueBindOp` 打成 C++ 引用别名 `TQueBind<...> & v_new = v_old`——纯类型改写，零运行开销。

**手写线示例三：DataCopy 的特殊形态**。

- [lib/Target/AscendC/Basic/DataCopy.cpp:39-57](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/Basic/DataCopy.cpp#L39-L57)：`DataCopySliceOp` 先为 dst/src 各生成一条 `AscendC::SliceInfo xxx_slice_info[] = {...};` 局部数组语句，再发 `AscendC::DataCopy(dst, src, dstInfo, srcInfo, dimValue)`——「一个 Op 生成多条 C++ 语句」的典型。
- [lib/Target/AscendC/Basic/DataCopy.cpp:18-31](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/Basic/DataCopy.cpp#L18-L31) 与 [lib/Target/AscendC/Basic/DataCopy.cpp:59-86](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/Basic/DataCopy.cpp#L59-L86)：`CopyL0/L1` 的模板实参 `<元素类型, isSetMask>` 由 `emitCopyTemplateArgs` 从 dst 张量类型逆向推出——`typename T` 不进 IR、发射时还原（u5-l3 约定）。

**生成线示例：`AscendC::DataCopy`（L2）从这来**。

01_add 里最常用的 `data_copy(dst, src, count)` 对应 `DataCopyL2Op`：

- [include/ascir/Dialect/Asc/IR/Basic/OpDataCopy.td:83-88](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Dialect/Asc/IR/Basic/OpDataCopy.td#L83-L88)：定义带 `[AscFunc]` trait、无 `paramTypeLists`——**生成线接管**，且走「空表委托」分支。
- [lib/TableGen/GenOpEmitDefs.cpp:262-282](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/TableGen/GenOpEmitDefs.cpp#L262-L282)：`printOp` 的二岔——`paramTypeLists` 非空时逐参数生成完整的模板头/实参打印代码（187-215 行处理 infer-type/infer-element-type/enum/value 等参数类别）；为空时只生成一行委托。
- [lib/TableGen/GenOpEmitDefs.cpp:280](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/TableGen/GenOpEmitDefs.cpp#L280)：委托行 `return autoPrintOp<ascendc::XxxOp>(emitter, op);`——`DataCopyL2Op` 的发射函数体就是它。
- [include/ascir/Target/Asc/UniversalEmitter.h:73-84](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Target/Asc/UniversalEmitter.h#L73-L84)：`autoPrintOp` 按 trait 编译期三分派。
- [include/ascir/Target/Asc/UniversalEmitter.h:59-71](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Target/Asc/UniversalEmitter.h#L59-L71)：`AscFunc` 分支 `autoPrintAscFuncOp`——单结果先打 `T name = `，再打 `AscendC::DataCopy(全部操作数逗号连接)`。所以 dump 里 `AscendC::DataCopy(v_x, v_y, c8_i32);` 一行，来自**生成函数 + 运行期模板**的组合。
- [include/ascir/Target/Asc/UniversalEmitter.h:37-56](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Target/Asc/UniversalEmitter.h#L37-L56)：另两个分支——`AscConstructor` 只发变量声明（如 `DataCopyParams` 结构体构造），`AscMemberFunc` 打 `obj.ApiName(args...)` 且跳过第 0 个操作数（对象本身）。

**生成定义的并入点**（与 4.1 呼应）：

- [lib/Target/AscendC/Translation.cpp:265-270](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/Translation.cpp#L265-L270)：`GET_OP_PRINT_FUNC_LIST` + include `AscendCOpEmit.cpp.inc`，把所有生成的 `printOperation` 定义拉进本翻译单元——u5-l4 讲过的 `.h.inc/.cpp.inc` 双产物在发射侧的消费点。

#### 4.3.4 代码实践

**实践目标**：对 dump 中的一条 `AscendC::DataCopy(...)` 调用，写出从「C++ 语句」到「发射函数」的完整溯源链，并注释参数格式化逻辑。

**操作步骤**：

1. 在 `ascendc.cpp`（前两节实践已产出）中找到形如 `AscendC::DataCopy(v_dst, v_src, c...);` 的调用。
2. 在 `lib/Target/AscendC/Basic/DataCopy.cpp` 里搜索 `DataCopyL2`——**搜不到**（该文件只有 Slice/L0/L1 三个手写函数，见 [include/ascir/Target/Asc/Basic/DataCopy.h:23-27](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Target/Asc/Basic/DataCopy.h#L23-L27) 的声明清单）。
3. 查 td：`OpDataCopy.td:83-88` 确认 `DataCopyL2Op` 带 `[AscFunc]` 且无 `paramTypeLists` → 生成线 → 空表委托 → `autoPrintAscFuncOp`。
4. 手写注释（作为学习笔记，不修改源码）：在笔记里抄下这条 C++ 语句，逐参数标注——`AscendC::` 来自 `ascNamespace` 常量；`DataCopy` 来自 `op.getAPIName()`；三个实参依次来自 `emitFunctionParams` 对操作数 0、1、2 的 `getOrCreateName`；行尾分号来自 `emitOperation`。
5. 换一条 `AscendC::Add(...)` 重复上述流程，这次落点应是 `VecBinary.h:68-80` 的 L2 模板（`AddL2Op` 无 trait、手写、手工登记于 Translation.cpp:160）。

**需要观察的现象**：DataCopy 是「命名空间级函数」语法，Add 的 L2 也是命名空间级但来自手写模板；两者参数个数不同（3 vs 4）。

**预期结果**：两条语句各自完成六步溯源笔记；能说清「为什么 DataCopy 搜不到手写实现而 Add 搜得到」。

**待本地验证**：dump 中具体的变量名/常量名以本地为准。

#### 4.3.5 小练习与答案

**练习 1**：想把一个新的 Ascend C API（原型为 `Foo(Tensor dst, Tensor src0, Tensor src1, int32_t count)`）接入 pyasc，走哪条线最省事？

答案：走生成线——在 td 里定义 `AscendC_FooOp : APIOp<"foo", "Foo", [AscFunc]>`，参数列表 `(ins Tensor:$dst, Tensor:$src0, Tensor:$src1, I32:$calCount)` 且不写 `paramTypeLists`。`genEmitter` 位自动置 1，生成的 `printOperation` 委托 `autoPrintAscFuncOp`，输出 `AscendC::Foo(dst, src0, src1, count)`，无需写任何 C++ 发射代码，也无需改 `PrintableOpTypes`（自动并入 `.h.inc`）。

**练习 2**：`TQueBindAllocTensorOp` 有 `[AscMemberFunc]` 类似的成员语义，为什么它不也走生成线、而在 TQue.cpp 手写？

答案：它的输出不是简单「`T name = obj.Api(args)`」——模板实参是 `op.getTensor().getType().getElementType()`（从张量操作数取元素类型），且无运行期实参。这类「从操作数类型逆推模板」的逻辑虽然生成器也支持（`kInferElementType`），但 TQueBind 家族还涉及结果变量声明、引用绑定等特殊形态，手写更直接。选择的自由度正是「两条线并存」的意义。

**练习 3**：`AscendCOpEmit.cpp.inc` 生成的函数和手写函数同名同签名（如未来某个 Op 两条线都写了），会发生什么？

答案：非模板重载重复定义会在链接期报 duplicate symbol；若一边是模板一边是具体函数，具体函数优先。实践中靠「判据只看 td」避免两条线撞车——带 trait 的 Op 由生成器独占，不带的必须手写并手工登记，`GenOpEmitsDefs::run` 在 [lib/TableGen/GenOpEmitDefs.cpp:287-289](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/TableGen/GenOpEmitDefs.cpp#L287-L289) 按 `genEmitter` 位过滤，保证单一归属。

### 4.4 EmitNameStack 与 Scope：变量名的唯一化

#### 4.4.1 概念说明

IR 里的值是匿名的 SSA（`%0, %1...`），但 C++ 代码必须给每个中间结果一个合法且**不冲突**的变量名。pyasc 沿用 MLIR EmitC 方言翻译器的做法，用三件套解决：

- `EmitNameStack`：按「名字前缀」维护每层作用域的计数器，产出 `v1, v2, ...` 与常量专用名 `c8_i32`；
- `valueMapper` / `blockMapper`：`ScopedHashTable`，把 `Value`/`Block` 映射到已分配的 C++ 名字，随作用域进出自动增删；
- `CodeEmitter::Scope`：RAII 包装，一次构造同时完成三件事（两张表的 scope 压栈 + 名字栈压栈），保证生成的 C++ 代码与 IR 的区域嵌套严格同构。

理解这套机制后你再读 `ascendc.cpp`，那些 `v1/v2` 就不再是乱码，而是可以反向对应回 IR 值的线索。

#### 4.4.2 核心流程

```
进入函数/区域:  Scope(emitter)  →  valueMapper/blockMapper 压栈, nameStack.pushScope()
取名字:        getOrCreateName(val)
                 ├─ 已在 valueMapper → 直接返回（同一 SSA 值全程同名）
                 └─ 不在 → nameStack.getNameForEmission(val)
                        ├─ val 是算术常量 → "c{值}_i{位宽}" / "c{值}_f{位宽}" / "c{值}_idx"
                        │      并把 '.'→'_'、'-'→'m'、'+'→'p'（净化非法字符）
                        └─ 其他 → "v{递增序号}"
                 └─ 若 Location 含 NameLoc → 名字再拼 "_{位置名}" 后缀
离开区域:      ~Scope() → 三栈弹出，内层名字整批失效（外层同前缀计数不受影响）
```

#### 4.4.3 源码精读

**RAII 作用域**：

- [include/ascir/Target/Asc/CodeEmitter.h:87-99](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Target/Asc/CodeEmitter.h#L87-99)：`Scope` 构造时创建两个 `llvm::ScopedHashTableScope`（自动管理 `valueMapper`/`blockMapper` 的作用域）并 `nameStack.pushScope()`；析构逆序弹出。函数发射（Func.cpp:60）、模块发射（Translation.cpp:60）都靠它建立 C++ 词法边界。
- [include/ascir/Target/Asc/CodeEmitter.h:110-136](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Target/Asc/CodeEmitter.h#L110-136)：成员一览——`valueMapper`（Value→名字）、`blockMapper`（Block→label 名）、`nameStack`、两张发射映射表。

**名字栈的压弹语义**（关键的「不重名」设计）：

- [lib/Target/AscendC/EmitNameStack.cpp:20-36](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/EmitNameStack.cpp#L20-L36)：`pushScope` 把每个前缀计数栈的**栈顶值复制压入**（不是清零！）——进入内层作用域时计数从外层当前值继续增长；`popScope` 丢弃内层计数。因此**兄弟作用域各自产生的 `v3` 互不相干且外层绝不重号**：计数只增不减、以「历史最大值」续编。
- [lib/Target/AscendC/EmitNameStack.cpp:38-48](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/EmitNameStack.cpp#L38-L48)：`getCountStack` 惰性创建某前缀的计数栈，且新栈深度对齐当前嵌套层数——保证任意时刻 pop 不会下溢。
- [lib/Target/AscendC/EmitNameStack.h:22-34](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/include/ascir/Target/Asc/EmitNameStack.h#L22-34)：数据结构——「前缀 → 计数栈」的哈希表加一个 label 计数栈。

**名字生成规则**：

- [lib/Target/AscendC/EmitNameStack.cpp:50-80](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/EmitNameStack.cpp#L50-80)：`getNameForEmission`——`arith::ConstantOp` 定义的整数常量得到语义化名字：index 打 `c8_idx`、整型打 `c8_i32`、浮点打 `c0_5_f32`（小数点被净化成下划线）；其余值一律 `v` 前缀自增。这让 dump 里的 `c8_i32` 一眼可读出「常量 8」。
- [lib/Target/AscendC/CodeEmitter.cpp:238-247](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/CodeEmitter.cpp#L238-247)：`getOrCreateName(Value)`——先查 `valueMapper` 缓存（**同一 SSA 值在其生命周期内名字稳定**），未命中才向名字栈要新名；若该值的 Location 里带 `NameLoc`，再拼 `_{名字}` 后缀。pyasc 前端是否常态生成带名字的 Location 待本地验证（可在 dump 中观察变量名是否出现 `v5_xxx` 这类带语义后缀的形式）。
- [lib/Target/AscendC/CodeEmitter.cpp:250-255](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/CodeEmitter.cpp#L250-255)：块的 label 名 `label0, label1...`（多块函数的 goto 目标）。
- [lib/Target/AscendC/CodeEmitter.cpp:269-271](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/lib/Target/AscendC/CodeEmitter.cpp#L269-L271)：`hasValueInScope`——`emitOperands` 用它做支配检查（见 4.2 练习 2）。

#### 4.4.4 代码实践

**实践目标**：把 `ascendc.cpp` 里的变量名反向对应到 IR 值，验证命名机制。

**操作步骤**：

1. 在同一 `PYASC_DUMP_PATH` 目录下同时打开 `ascir.mlir`（Pass 后 IR）与 `ascendc.cpp`。
2. 在 `ascendc.cpp` 的 Kernel 函数体里收集：前 5 个 `v` 前缀变量名、全部 `c` 前缀常量名、（若用 02_add_framework）label 名。
3. 按出现顺序把 `v1..vN` 与 `ascir.mlir` 中同名函数体内的 SSA 值（`%0, %1...` 或带名字的值）逐个连线，做成两列对照表。
4. 验证净化规则：找一个浮点常量名，确认小数点已变下划线（如 `c0_5_f32`）；找一个 `c*_i32`，解释其「值 + 位宽」编码。

**需要观察的现象**：变量序号是否单调递增、有无跳号；同一 IR 值在前后两条语句中是否使用同一变量名（缓存生效）。

**预期结果**：得到一张至少 5 行的「C++ 变量名 ↔ IR 值」对照表，并能解释每个 `c` 名字的构成。

**待本地验证**：名字的具体形态取决于前端产生的 IR，以本地 dump 为准。

#### 4.4.5 小练习与答案

**练习 1**：两个并列的 `scf.for` 循环体内各自产生了 `v5`，会不会导致生成的 C++ 重定义？

答案：不会。`pushScope` 复制栈顶计数继续增长——第一个循环产生 `v5` 后计数停在 5，第二个循环进入时从 5 续编，得到的是 `v6` 起。计数「只增不减」保证跨兄弟作用域也不重名；而 `valueMapper` 的 ScopedHashTable 保证即便同名也活在不同 C++ 作用域里（不过本设计中根本不会同名）。

**练习 2**：为什么常量要专门生成 `c8_i32` 这种语义名，而不是也用 `v3`？

答案：两个好处——(1) 可读性：dump 里的 `AscendC::DataCopy(v1, v2, c8_i32)` 直接看出搬运 8 个元素，排查问题快；(2) 语义防御：同一个常量值 8 若被多个 `arith.constant` 定义，各得其所的名字也让「值相同但定义不同」的 SSA 区分明显。代价是名字里出现的 `-`/`+`/`.` 需净化为合法 C++ 标识符字符。

**练习 3**：`printMask`（Common.h:109-121）里的 `static int maskCounter` 是函数级静态变量，这和 EmitNameStack 的作用域计数有什么不同？有什么隐患？

答案：EmitNameStack 的计数随 `Scope` 压弹、与 C++ 词法作用域严格对齐；而 `maskCounter` 是跨整个翻译过程单调递增的静态计数，不随作用域回收。好处是绝无重名；隐患是名字里的编号与作用域无关（长时间编译的模块会得到很大的编号），且该函数若被并发调用会数据竞争——不过发射过程是单线程串行的，实际无害。这是读发射代码时值得注意的「历史约定」。

## 5. 综合实践

**任务：手工 `.mlir` → `ascir-translate` 离线翻译，验证你对发射全链路的理解。**

背景：Python 主链路的翻译在进程内完成（compiler.py:117 → `ir_to_ascendc`），但仓库还提供了命令行工具 `ascir-translate`，可以把任意一份 `.mlir` 文件独立翻译成 Ascend C——这是不用写 Python 就能做发射实验的途径。

步骤：

1. **准备 dump**：`export PYASC_DUMP_PATH=/tmp/pyasc-dump`，Model 模式运行 `examples/01_add/add.py`，得到 `codegen.mlir`（Pass 前）、`ascir.mlir`（Pass 后）、`ascendc.cpp`（发射产物）。
2. **准备工具**：若构建时未开工具，设置 `PYASC_SETUP_DEVTOOLS=1` 重新 `pip install -e .`（见 u7-l5；本次实践也可直接复用已有构建产物中的 `ascir-translate`）。
3. **离线翻译**：执行 `ascir-translate --help` 查看已注册的翻译名，然后用 `-mlir-to-ascendc`（注册名见 [bin/ascir-translate.cpp:35-37](https://github.com/gitcode.com/cann/pyasc/blob/739ef7e242c12c6a58e0ec7ec429e610b7ee988f/bin/ascir-translate.cpp#L35-L37)）把 `ascir.mlir` 翻译出来：
   ```bash
   ascir-translate -mlir-to-ascendc /tmp/pyasc-dump/ascir.mlir -o /tmp/pyasc-dump/manual.cpp
   ```
   具体参数形式以 `--help` 输出为准（**待本地验证**）。
4. **一致性检查**：`diff /tmp/pyasc-dump/manual.cpp /tmp/pyasc-dump/ascendc.cpp`——两者输入相同（Python 侧 dump 的正是跑完 Pass 的模块），预期一致或仅有无关紧要的空白差异。
5. **负向实验**：改用 `codegen.mlir`（Pass 前的 IR）翻译，观察结果——缺少 postprocessing Pass 种下的样板与合法化改写，预期翻译失败（如找不到 printer）或产物不完整；记录报错信息，并回看 u6-l1/u6-l4 解释「缺了哪个 Pass 导致」。
6. **溯源笔记**：在 `ascendc.cpp` 中挑一条 `AscendC::DataCopy(...)` 与一条 `AscendC::Add(...)`，按 4.3.4 的六步完成「C++ 语句 → 发射函数 → 参数格式化」注释；再挑一条 `AscendC::TQue<...>` 类型（02_add_framework 的 dump），注明它来自 `emitAscQueueType`。

产出物：一份包含 diff 结果、负向实验报错、三条溯源注释的实验记录。

## 6. 本讲小结

- 发射模型是「**白名单 TypeSwitch 分派 + 每 Op 一个 printOperation**」：`emitOperation` 统一打注释、补分号，未登记的 Op 报 `unable to find printer for op` 并以 `LogicalResult` 逐层失败，最终在 Python 侧变成 `Failed to translate IR to Ascend C`。
- `CodeEmitter` 是发射标准库：两张 TypeID 映射表 + 基础类型/属性打印 + 变量声明原语；`index→uint32_t`、`f16→half`、memref 带 `__gm__` 地址空间、Matmul 配置整体还原为 `constexpr static MatmulConfig CFG{...}`。
- `printOperation` 有**两条供给线**：无 trait 的 Op 手写（`include/ascir/Target/Asc` + `lib/Target/AscendC` 镜像目录，且需手工登记进 `PrintableOpTypes`）；带 Asc* trait 的 Op 由 `-gen-op-emit-defs` 生成——`paramTypeLists` 非空生成完整逐参打印，为空委托 `autoPrintOp` 三分支（构造/成员/命名空间函数）。判据只看 td。
- Kernel 入口的 `extern "C" __global__ __aicore__` 样板由 `func::FuncOp` 的发射函数依据 `ascendc.global` 属性打印——u6-l4 的 Pass 只「种属性」，发射层才「落纸」。
- 命名三件套（`EmitNameStack` + 两张 ScopedHashTable + RAII `Scope`）保证匿名 SSA 变成唯一且可读的 C++ 名字：普通值 `v1, v2...`、常量 `c8_i32`、标签 `label0`；计数「进层续编、出层丢弃」，兄弟作用域永不撞名。
- 发射单位不限于单条表达式：`printMask`、`DataCopySliceOp` 都会先产出局部数组语句再发调用——读发射代码前先建立「一个 Op 可生成一小段 C++」的预期。

## 7. 下一步学习建议

本讲把 `ascendc.cpp` 的「正文」讲完了，但清单里还有一大类 Op 没展开：`scf.for`、`arith.addi`、`func.call`、`emitc.verbatim`、`memref.load` 这些**上游方言与 EmitAsc 方言**的发射。下一讲 **u6-l6 EmitAsc 方言与外部方言降级** 将进入 `lib/Target/AscendC/External/`（Arith/Math/Scf/Func/MemRef/Emitc）与 `EmitAsc.cpp`，弄清一个 Python `for` 循环如何先变成 `scf.for`、再被发射成 C 的 `for` 语句，以及 EmitAsc 方言为何要做「贴近 C 语法」的低层桥梁。

在进入下一讲前，建议先完成本讲综合实践——特别是用 `ascir-translate` 手工翻译的环节，它建立的「IR 文本 ↔ C++ 语句」手感是读 External 目录源码的最好前置。之后可以带着一个问题去读 u7-l5：`ascir-opt` 与 `ascir-translate` 这两个工具是如何共享同一套 Pass 与发射基础设施的。
