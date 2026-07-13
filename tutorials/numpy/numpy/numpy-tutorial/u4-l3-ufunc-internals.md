# ufunc 内部实现与类型解析

## 1. 本讲目标

上一篇（u4-l2）我们在 C 层拆开了 `ndarray` 的内存模型。本讲把镜头转向 ndarray 的「计算引擎」——ufunc。

读完本讲，你应该能够：

- 说清 `PyUFuncObject` 这个 C 结构体里 `nin`/`nout`/`functions`/`types`/`ntypes`/`identity`/`type_resolver`/`vectorcall` 等字段的含义，以及它们如何对应到 Python 层的 `np.add.nin`、`np.add.types` 等属性。
- 跟踪一次 `np.add(a, b)` 调用从 Python 入口到内层 C 循环的完整路径：vectorcall → 参数解析 → 类型提升与循环选择 → 描述符解析 → 内层循环执行。
- 理解「类型解析（type resolution）」要解决什么问题，以及默认解析器 `PyUFunc_DefaultTypeResolver` 如何在 `functions[]`/`types[]` 表里线性搜索一条匹配的内层循环。
- 认识新一代的 `ArrayMethod` 机制：它如何用一张 `_loops` 字典 + promoter 取代了旧的线性搜索，又如何把旧的 `functions[]`/`types[]` 包装成 ArrayMethod 继续工作。

本讲是后续 u4-l4（归约）、u4-l5（SIMD 分发）、u8-l3（自定义 dtype）的理论地基。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**第一，ufunc 不是函数，是对象。** 普通的 Python 函数调用 `f(a, b)` 时，解释器直接执行一段字节码。而 `np.add(a, b)` 里的 `np.add` 是一个 C 层的 `PyUFuncObject` 实例——它「持有」一组针对不同数据类型写好的 C 内层循环（kernel），调用时根据输入的实际类型挑出一条来跑。所以 ufunc 更像一张「类型 → 循环」的分发表，外加逐元素 + 广播的执行框架。

**第二，一个 ufunc 背后有多条循环。** 以 `np.add` 为例：两个 `float64` 相加、两个 `int8` 相加、两个 `complex128` 相加，用的是三段不同的 C 代码。为什么不能写一段通用代码？因为不同类型的位宽、是否有符号、是否需要溢出处理都不同；并且对 `int8` 这种窄类型，专用循环能装进 SIMD 通道一次算多个。所以 ufunc 把「同一个数学运算」针对「每种支持的类型」都注册了一条独立循环。

**第三，类型解析就是「挑循环」。** 给定两个输入数组，ufunc 必须先决定：用哪条循环？输出是什么类型？输入要不要先转换（cast）到循环期望的类型？这一步就叫类型解析。它必须在真正计算之前完成，因为计算的内存布局、输出数组的分配都依赖它的结论。

承接 u4-l1：ufunc 的 `nin`/`nout`/`identity`/`types` 属性、`frompyfunc` 的 object 循环，以及 `reduce` 的存在性都由 `identity` 决定——这些属性的根源就在本讲要拆的 `PyUFuncObject` 结构体里。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `numpy/_core/include/numpy/ufuncobject.h` | 定义 `PyUFuncObject` 结构体、内层循环函数指针类型、`identity` 枚举常量。 |
| `numpy/_core/src/umath/ufunc_object.c` | ufunc 的「大脑」：vectorcall 入口、参数解析、调用 ArrayMethod 分发、内层循环执行、构造函数，以及 `nin`/`types`/`identity` 等 Python 属性的 getter。 |
| `numpy/_core/src/umath/ufunc_type_resolution.c` | 旧式（legacy）类型解析：`PyUFunc_DefaultTypeResolver` 在 `functions[]`/`types[]` 表里线性搜索匹配循环；含 `np.add` 专用的 `PyUFunc_AdditionTypeResolver`。 |
| `numpy/_core/src/umath/dispatching.cpp` | 新式 ArrayMethod 分发：`_loops` 字典查找、promoter 调用、以及找不到时回退到旧式解析器的桥梁。 |
| `numpy/_core/include/numpy/dtype_api.h` | `ArrayMethod` 的公开结构：`PyArrayMethod_Spec`、slots（`NPY_METH_resolve_descriptors` 等）、`PyArrayMethod_ResolveDescriptors` 回调签名。 |

## 4. 核心概念与源码讲解

### 4.1 PyUFuncObject 结构：一张类型→循环的分发表

#### 4.1.1 概念说明

`PyUFuncObject` 是 ufunc 在 C 层的真身。你可以把它理解成一张「分发表 + 执行框架」：

- **分发表**：`functions[]` 是一组函数指针，每条指向一段针对特定类型写的 C 内层循环；`types[]` 记录每条循环接受的输入/输出类型编号；`ntypes` 是循环条数。
- **执行框架**：`nin`/`nout` 说明有几个输入几个输出；`identity` 说明做归约时的单位元；`type_resolver` 是「挑循环」的函数指针；`vectorcall` 是 Python 调用 ufunc 时的入口。

ufunc 一旦在模块初始化时构造好，这些字段基本就固定了——每次调用只是在这张表上查、跑，结构体本身不变。

#### 4.1.2 核心流程

一个 ufunc 的「静态画像」可以画成下表（以 `np.add` 为例，nin=2, nout=1, nargs=3）：

```
PyUFuncObject (np.add)
├─ nin=2, nout=1, nargs=3, identity=PyUFunc_Zero(0)
├─ ntypes = N            # 注册的循环条数
├─ functions[N]          # 每条是一个 PyUFuncGenericFunction 函数指针
├─ types[N * nargs]      # 每条循环占 nargs 个字节，记录 (in0,in1,out) 的类型编号
├─ data[N]               # 传给每条循环的附加数据（通常 NULL）
├─ type_resolver ────────► PyUFunc_DefaultTypeResolver / PyUFunc_AdditionTypeResolver
├─ vectorcall ───────────► ufunc_generic_vectorcall
└─ _loops / _dispatch_cache  # 新式 ArrayMethod 分发表与缓存（后讲）
```

`types[]` 的排布是关键：它是一个长度为 `nargs * ntypes` 的 `char` 数组，第 `i` 条循环的类型编号存放在 `types[i*nargs .. i*nargs+nargs-1]`。对 `np.add`，第 `i` 条循环的签名就是 `(types[3i], types[3i+1]) -> types[3i+2]`。

#### 4.1.3 源码精读

先看内层循环的函数指针类型——所有 `functions[]` 里的函数都长这个样子：

[ufuncobject.h:L16-L20](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/include/numpy/ufuncobject.h#L16-L20) 定义了 `PyUFuncGenericFunction`：接收参数指针数组 `args`、各维长度 `dimensions`、各参数步长 `strides`、以及附加数据 `innerloopdata`。这就是「按 strides 推进指针、逐元素计算」的最通用签名——它本身不知道类型，类型由「哪条循环被选中」隐含决定。

再看结构体本体：

[ufuncobject.h:L103-L139](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/include/numpy/ufuncobject.h#L103-L139) 集中了分发表的核心字段。逐个对照：

- `nin`/`nout`/`nargs`（L112-L113）：输入数、输出数、总操作数（恒等于 `nin+nout`，源码注释也困惑为何要冗余存一份）。
- `identity`（L120）：归约单位元，取值见下方枚举。
- `functions`（L123）：`PyUFuncGenericFunction *`，指向内层循环函数指针数组。
- `data`（L125）：与 `functions` 一一对应的附加数据指针数组。
- `ntypes`（L127）：`functions`/`data`/`types` 的条数。
- `types`（L136）：`const char *`，长度 `nargs * ntypes`，存类型编号。

接下来是执行框架字段：

[ufuncobject.h:L172-L190](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/include/numpy/ufuncobject.h#L172-L190) 中，`type_resolver`（L176）是类型解析函数指针；`vectorcall`（L187）是 Python 3.8+ 的快速调用入口。注意 L182-L185 的注释：这个字段原本在 1.7 预留给「新式内层循环选择器」但从未实现，所以旧的选择器被称作 "legacy"。

`identity` 字段的取值在头文件里定义成枚举常量：

[ufuncobject.h:L273-L303](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/include/numpy/ufuncobject.h#L273-L303) 列出了全部取值：`PyUFunc_Zero`（加法单位元 0）、`PyUFunc_One`（乘法单位元 1）、`PyUFunc_MinusOne`（按位与的单位元）、`PyUFunc_None`（无单位元、不可重排，故不能多轴归约）、`PyUFunc_ReorderableNone`（无单位元但可重排）、`PyUFunc_IdentityValue`（单位元是一个具体值，存于 `identity_value` 字段）。这正解释了 u4-l1 讲过的「减法的 identity 是 None，所以不能 reduce」。

最后两块「新式机制」字段先记下，4.4 节细讲：

[ufuncobject.h:L226-L232](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/include/numpy/ufuncobject.h#L226-L232) 新增了 `_dispatch_cache`（解析结果缓存）和 `_loops`（Ordered dict：`DType 元组 -> (DType 元组, ArrayMethod/Promoter)`）。

#### 4.1.4 代码实践

**实践目标**：验证 `np.add` 的 Python 属性确实来自 `PyUFuncObject` 的 C 字段。

**操作步骤**：

1. 在能 `import numpy` 的环境中运行：
   ```python
   import numpy as np
   a = np.add
   print("nin    =", a.nin)       # 期望 2
   print("nout   =", a.nout)      # 期望 1
   print("nargs  =", a.nargs)     # 期望 3
   print("ntypes =", a.ntypes)    # 注册的循环条数（待本地验证具体值）
   print("identity =", a.identity)# 期望 0（PyUFunc_Zero）
   print("types  =", a.types)     # 形如 ['??->?', 'bb->b', ..., 'OO->O']
   ```
2. 对照下面源码确认这些属性都是「现算」出来的 getter，而非实例变量。

**需要观察的现象**：`a.types` 是一个字符串列表，每项形如 `dd->d`（两个 double 相加得 double）。`identity` 是整数 `0`。

**预期结果**：`nin=2, nout=1, nargs=3, identity=0`。`types` 列表里能看到 `dd->d`（float64 加法）、`bb->b`（int8 加法）、`OO->O`（object 加法）等条目。`ntypes` 与 `len(a.types)` 相等。

**关于精确取值**：`ntypes`/`types` 的完整列表依赖编译时注册的循环集合（不同平台 `long`/`longlong` 可能重合），具体条数与内容**待本地验证**。下一节会解释这些字符串是怎么从 `ufunc->types` 数组格式化出来的。

#### 4.1.5 小练习与答案

**练习 1**：`np.multiply.identity` 和 `np.subtract.identity` 分别是什么？为什么后者不能做 `reduce`？

**答案**：`np.multiply.identity == 1`（`PyUFunc_One`），`np.subtract.identity is None`（`PyUFunc_None`，无单位元）。减法没有单位元且运算不可交换，多轴归约需要可重排性，故 `reduce` 对减法无意义。

**练习 2**：`np.add.types` 里每一项 `xx->y` 的三个字符分别来自结构体的哪个字段？

**答案**：来自 `types[]` 数组。第 `i` 条循环占 `nargs=3` 个字节：前两个 `xx` 是 `types[3i]`、`types[3i+1]`（两个输入类型编号），`y` 是 `types[3i+2]`（输出类型编号）。

---

### 4.2 vectorcall 与循环选择：一次调用的完整路径

#### 4.2.1 概念说明

结构体是静态的，调用是动态的。本节跟踪 `np.add(a, b)` 从 Python 调用落到内层 C 循环的完整链路。核心结论：**ufunc 的调用是一条流水线**，类型解析（挑循环）只是其中一环，前后还各有一环。

这条流水线的关键在于：现代 NumPy 已经把「挑循环」从旧的线性搜索迁移到了基于 `ArrayMethod` 的新机制（4.4 节）。但旧的 `functions[]`/`types[]` 表和 `type_resolver` 仍然作为兜底保留着。所以一次调用里，新旧两条路径是交织的。

#### 4.2.2 核心流程

`np.add(a, b)` 的执行路径可以概括为五步：

```
np.add(a, b)
   │  (Python 调用 ufunc 对象)
   ▼
① ufunc_generic_vectorcall        # 入口，转发到 fastcall
   ▼
② ufunc_generic_fastcall          # 主控
   ├─ 参数解析：拆出 in/out/where/casting/signature/dtype 等
   ├─ PyUFunc_CheckOverride       # __array_function__/__array_ufunc__ 重载检查
   ├─ convert_ufunc_arguments     # 把输入转成 operands + 提取 DType
   ▼
③ promote_and_get_ufuncimpl       # 【挑循环】类型提升 + 选 ArrayMethod（dispatching.cpp）
   ▼
④ resolve_descriptors             # 确定 loop 用的具体 descr（dtype 实例）
   ▼
⑤ PyUFunc_GenericFunctionInternal # 执行：get_strided_loop → 内层循环
```

第 ③ 步是本讲的主线（4.3、4.4 节展开），其余各步先看清位置与职责。

#### 4.2.3 源码精读

**入口**。Python 3.8+ 用 vectorcall 协议调用 ufunc，入口就是结构体里的 `vectorcall` 字段：

[ufunc_object.c:L4905-L4914](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/umath/ufunc_object.c#L4905-L4914) 的 `ufunc_generic_vectorcall` 只是规范化 `len_args` 后转发给 `ufunc_generic_fastcall`。

**主控函数**。`ufunc_generic_fastcall` 是整条流水线的中枢：

[ufunc_object.c:L4560-L4564](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/umath/ufunc_object.c#L4560-L4564) 是它的签名。函数很长（约 330 行），我们只看流水线的三步关键调用。

第 ③ 步——挑循环（本讲核心）：

[ufunc_object.c:L4829-L4842](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/umath/ufunc_object.c#L4829-L4842) 先调 `promote_and_get_ufuncimpl` 拿到 `PyArrayMethodObject *ufuncimpl`（即选中的循环实现），再调 `resolve_descriptors` 算出每个操作数在本次调用中实际使用的 `descr`。注意 L4825-L4827 的注释明确说：类型解析这一步目前还在 ufunc 层做，将来可能下放到 ArrayMethod。

第 ⑤ 步——执行：

[ufunc_object.c:L4847-L4856](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/umath/ufunc_object.c#L4847-L4856) 按 `core_enabled`（是否 gufunc）分流到 `PyUFunc_GenericFunctionInternal` 或 `PyUFunc_GeneralizedFunctionInternal`。

**执行内部**。`PyUFunc_GenericFunctionInternal` 负责把选中的 ArrayMethod 真正跑起来：

[ufunc_object.c:L2192-L2232](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/umath/ufunc_object.c#L2192-L2232) 先取 buffersize/errormask（来自线程局部存储，对应 `np.seterr`），设置输入/输出迭代器标志，再构造一个 `PyArrayMethod_Context`（L2229-L2232），把 `caller`（ufunc 本身）和 `method`（选中的 ArrayMethod）挂上去——这个 context 后面会传给内层循环。

[ufunc_object.c:L2250-L2273](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/umath/ufunc_object.c#L2250-L2273) 是普通（非 masked）分支：先 `check_for_trivial_loop` 看能否走「无迭代器」的快速通道（`try_trivial_single_output_loop`），否则落回通用的 `execute_ufunc_loop`。后者会用 nditer 驱动 `ufuncimpl->get_strided_loop(...)` 取到真正的 strided 内层循环函数并逐块执行。

**构造时如何装好这些函数指针**。回到 ufunc 的诞生地，看 `vectorcall` 和 `type_resolver` 是何时被赋值的：

[ufunc_object.c:L5026-L5031](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/umath/ufunc_object.c#L5026-L5031) 在 `PyUFunc_FromFuncAndDataAndSignatureAndIdentity`（所有内置 ufunc 的构造函数）里：`vectorcall = &ufunc_generic_vectorcall`、`type_resolver = &PyUFunc_DefaultTypeResolver`。也就是说，**默认情况下每个 ufunc 都用同一套调用入口和同一套默认类型解析器**；像 `np.add` 这样有特殊需求的，会在构造后把 `type_resolver` 改写成 `PyUFunc_AdditionTypeResolver`（见 4.3 节）。

#### 4.2.4 代码实践

**实践目标**：跟踪一次 `np.add` 调用，确认「挑循环」发生在「执行」之前，并观察类型解析的产物（输出 dtype）。

**操作步骤**：

1. 运行下面的「源码阅读型」跟踪，对照本节的五步流水线在源码里找到每一步对应的函数：
   ```python
   import numpy as np
   # ① 入口：np.add 是 ufunc 对象，其 vectorcall 指向 ufunc_generic_vectorcall
   print(type(np.add))            # <class 'numpy.ufunc'>
   # ③ 挑循环 + 提升的产物：输出 dtype 由类型解析决定
   print((np.arange(3, dtype=np.int8) + np.arange(3, dtype=np.int16)).dtype)
   print((np.arange(3, dtype=np.int8) + 1).dtype)   # NEP 50: Python 标量不提升精度
   print((np.arange(3, dtype=np.int8) + np.int8(1)).dtype)
   ```
2. 对照 `ufunc_generic_fastcall`（L4560 起）的代码，在脑中把上面三行 `+` 运算分别走一遍五步。

**需要观察的现象**：第一行的输出 dtype 是 `int16`（两数组提升到较宽者）；第二行是 `int8`（Python `1` 是弱标量，NEP 50 不提升精度）；第三行是 `int8`（同为 int8）。

**预期结果**：`int16`、`int8`、`int8`。这三个 dtype 就是第 ③④ 步类型解析的最终产物。

**关于运行**：若本地环境未构建 NumPy，以上具体 dtype 结论**待本地验证**；但它们可由 u2-l3 讲过的 NEP 50 规则直接推出，源码层面则由 4.3、4.4 节的解析器决定。

#### 4.2.5 小练习与答案

**练习 1**：`ufunc_generic_fastcall` 里，`PyUFunc_CheckOverride`（L4760 附近）放在「挑循环」之前还是之后？为什么必须这样？

**答案**：放在之前。重载协议（`__array_ufunc__`）允许非 NumPy 数组完全接管一次 ufunc 调用；如果有重载命中，本次调用根本不该走 NumPy 自己的类型解析与内层循环。所以必须先检查重载，命中就直接返回，不进入第 ③④⑤ 步。

**练习 2**：`PyUFunc_GenericFunctionInternal` 为什么要先 `check_for_trivial_loop` 再决定是否走 `try_trivial_single_output_loop`？

**答案**：nditer 是通用但较重的迭代器。当输入是一维/标量、形状平凡、对齐良好时，可以绕过 nditer 直接调一次内层循环，省掉迭代器构造开销。`check_for_trivial_loop` 判断是否满足这条「平凡」条件，满足才走快速通道，否则用通用的 `execute_ufunc_loop`。

---

### 4.3 默认类型解析：在 functions/types 表里挑循环

#### 4.3.1 概念说明

类型解析要回答两个问题：**(a) 用哪条内层循环？(b) 每个操作数的精确 dtype（descr）是什么？** 旧式解析器把两者揉在一起：在 `functions[]`/`types[]` 表里线性搜索，找到第一条「输入能安全 cast 到、输出能安全 cast 出」的循环，把它的类型签名当作答案。

文件开头的注释坦白交代了这套机制的地位：

[ufunc_type_resolution.c:L1-L22](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/umath/ufunc_type_resolution.c#L1-L22) 说明本文件的类型解析被视为 **legacy**：新机制（NEP 43）把类型解析和提升拆成两步、且只依赖 DType/descriptor 而非 operands。旧机制依赖实际数组（operands），仍被调用、仍关键，但理论上「整文件最终可删除」。

#### 4.3.2 核心流程

默认解析器 `PyUFunc_DefaultTypeResolver` 的逻辑：

```
输入：ufunc, casting, operands[nin+nout], type_tup(可空), out_dtypes[](待填)
│
├─ 扫描 operands：若有 object 数组 → any_object=1
├─ input_casting = min(casting, NPY_SAFE_CASTING)   # 输入端至少 safe
│
├─ 若 type_tup == NULL（用户没指定 dtype=）：
│     └─ linear_search_type_resolver     # 线性搜索最佳循环
│        ├─ 先查 userloops（用户用 PyUFunc_ReplaceLoopBySignature 注册的）
│        └─ 再 for i in ntypes：逐条用 ufunc_loop_matches 试配
│           ├─ 命中 → set_ufunc_loop_data_types 填 out_dtypes，返回
│           └─ 全不中 → 抛 "ufunc 'x' not supported for the input types"
│
└─ 若 type_tup != NULL（用户指定了 dtype=）：
      └─ type_tuple_type_resolver          # 按指定类型找循环
```

「线性搜索」的核心是逐条比较：第 `i` 条循环的类型签名是 `types[i*nargs .. ]`，把每个输入 operand 的 dtype 能否安全 cast 到签名里的类型，用 `ufunc_loop_matches` 判定。

**优化**：很多 ufunc（含 `np.add`）的签名都是 `xx->x` 形式（所有输入输出同类型），这时不必线性搜索——直接用 `PyArray_ResultType` 算出结果类型即可。这就是 `PyUFunc_SimpleUniformOperationTypeResolver`，也是 `linear_search_type_resolver` 注释里提到却「未实现」的快速通道，现在以独立函数形式存在。

#### 4.3.3 源码精读

**默认解析器入口**：

[ufunc_type_resolution.c:L298-L337](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/umath/ufunc_type_resolution.c#L298-L337) 的 `PyUFunc_DefaultTypeResolver`。L309-L315 扫描 object；L323 把输入端 casting 钳到 `NPY_SAFE_CASTING`（注释 L317-L322 解释：否则循环选择代码可能给 float64 输入选了 float32 循环）；L325-L334 按 `type_tup` 是否为空二分流。

**线性搜索主体**：

[ufunc_type_resolution.c:L2010-L2084](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/umath/ufunc_type_resolution.c#L2010-L2084) 的 `linear_search_type_resolver`。L2027 先决定是否启用「最小标量」规则（NEP 50 之前的值-based 提升，现在主要给弱标量用）；L2030-L2043 先查 `userloops`（用户注册的自定义循环）；L2062-L2084 是核心双重循环：外层遍历 `ntypes` 条循环，L2063 取第 `i` 条的类型签名 `self->types + i*self->nargs`，L2070 调 `ufunc_loop_matches` 试配，命中（返回 1）就用 `set_ufunc_loop_data_types` 填 `out_dtypes` 返回。L2086-L2108 是没找到时的两种报错（输出不可 cast vs 完全无匹配）。

注意 L2045-L2060 的长注释：作者承认这套线性搜索「本可以快得多」，并勾勒了 `xx->x` 模式的快速通道——这正是下面这个函数做的事。

**`xx->x` 快速通道（np.add 实际走的路径）**：

[ufunc_type_resolution.c:L537-L610](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/umath/ufunc_type_resolution.c#L537-L610) 的 `PyUFunc_SimpleUniformOperationTypeResolver`。L558-L565 检测自定义/对象 dtype，命中则退回 `PyUFunc_DefaultTypeResolver`（走线性搜索）；L608 对 nin>1 的常见情况直接调 `PyArray_ResultType(ufunc->nin, operands, 0, NULL)` 算出结果 dtype——一步到位，无需遍历 `ntypes`。这就是 `np.add` 处理 `int8 + int16` 时拿到 `int16` 的源头。

**np.add 的专用解析器**：

[ufunc_type_resolution.c:L805-L823](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/umath/ufunc_type_resolution.c#L805-L823) 的 `PyUFunc_AdditionTypeResolver`。L819-L823 是关键：只要不涉及 datetime/timedelta/string，就委托给上面的 `PyUFunc_SimpleUniformOperationTypeResolver`。只有涉及时间增量、日期时间、字符串这些「带元数据的类型」时（L825 起），才需要 `PyArray_PromoteTypes` 做特殊提升（例如 `m8[<A>] + m8[<B>] => m8[gcd(<A>,<B>)]`）。这说明 `np.add` 的 `type_resolver` 字段在构造后被改写成了这个函数，而非默认的 `PyUFunc_DefaultTypeResolver`。

#### 4.3.4 代码实践

**实践目标**：用 `np.add.types` 列出它注册的全部 `(in0, in1, out)` 组合，并对照源码说明「挑循环」如何发生。

**操作步骤**：

1. 运行：
   ```python
   import numpy as np
   add = np.add
   print("ntypes =", add.ntypes)
   for s in add.types:
       print(s)
   ```
2. 挑出 `dd->d`（float64 加法）这一条，回答：当执行 `np.array([1.], dtype=np.float64) + np.array([2.], dtype=np.float64)` 时，解析器如何选中它？
3. 再思考：执行 `np.int8(3) + np.int16(4)` 时，输出是 `int16`。可这里并没有一条 `int8,int16->int16` 的循环——那循环是怎么选中的？

**需要观察的现象**：`add.types` 列出形如 `??->?`、`bb->b`、`hh->h`、`ff->f`、`dd->d`、`OO->O` 等条目，每项的输入两端类型相同（`xx->x` 模式）。

**预期结果与分析**：
- 对 `float64 + float64`：`PyUFunc_AdditionTypeResolver` →（非 datetime/string）→ `PyUFunc_SimpleUniformOperationTypeResolver` → `PyArray_ResultType` 算出 `float64`，于是选中签名 `dd->d` 对应的那条 C 循环。
- 对 `int8 + int16`：同样走快速通道，`ResultType` 把两者提升为 `int16`；输入 int8 被 safe-cast 到 int16 后，跑 `hh->h` 循环，输出 int16。**所以循环并不是按「输入类型字面匹配」选的，而是按「提升后的结果类型」选的**——这正是 `xx->x` 快速通道的本质。

**关于精确取值**：`add.types` 的完整列表与 `ntypes` 数值依赖编译时注册情况，**待本地验证**；但「每项 `xx->x` 同型」这一规律可由源码（`ufunc_get_types` 直接读 `ufunc->types`）确认。

**附：`types` 属性的格式化来源**。每个字符串是 `ufunc_get_types` 现场拼出来的：

[ufunc_object.c:L6818-L6853](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/umath/ufunc_object.c#L6818-L6853) 遍历 `ntypes`，对每条循环用 `_typecharfromnum` 把类型编号转成字符（如 `NPY_DOUBLE`→`'d'`），中间插 `->`，拼成 `in0in1->out`。`_typecharfromnum` 的实现见 [ufunc_object.c:L6735-L6743](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/umath/ufunc_object.c#L6735-L6743)，本质是取 `PyArray_DescrFromType(num)->type` 字符。`nin`/`nout`/`ntypes`/`identity` 等属性则由更简单的 getter 直接 `PyLong_FromLong(ufunc->字段)` 返回，登记在 [ufunc_object.c:L6879-L6906](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/umath/ufunc_object.c#L6879-L6906) 的 `ufunc_getset` 表里。`identity` getter 走 `PyUFunc_GetDefaultIdentity`（[ufunc_object.c:L1661-L1694](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/umath/ufunc_object.c#L1661-L1694)），把 C 枚举 `PyUFunc_Zero` 翻译成 Python 整数 `0`、`PyUFunc_None` 翻译成 `None`。

#### 4.3.5 小练习与答案

**练习 1**：`linear_search_type_resolver` 里 `input_casting` 为何要被钳到 `NPY_SAFE_CASTING`？如果允许 `unsafe` 会出什么问题？

**答案**：循环选择时若允许 unsafe，可能给 `float64` 输入选中 `float32` 循环，直接丢精度。所以输入端始终要求至少 safe casting——unsafe 只在「输出写回用户提供的 out 数组」那一步才可能放行，由 `output_casting` 单独把关。

**练习 2**：`np.add` 处理 `int8 + int16` 时，`add.types` 里并没有 `int8,int16->int16` 这条循环，为什么还能算出 `int16` 结果？

**答案**：因为 `np.add` 用的是 `PyUFunc_SimpleUniformOperationTypeResolver`，它不逐条匹配输入字面类型，而是先用 `PyArray_ResultType` 把输入提升为 `int16`，再按结果类型选 `hh->h` 循环；int8 输入先被 safe-cast 到 int16 再进入循环。`xx->x` 模式让「结果类型」即可定位循环，无需为每种输入组合都注册一条循环。

---

### 4.4 ArrayMethod：新式循环机制

#### 4.4.1 概念说明

前面三节都建立在旧的 `functions[]`/`types[]` 表上。但读到这里你会发现旧机制有几个硬伤：

1. **线性搜索慢**：每次调用都要遍历 `ntypes` 条循环试配（虽然快速通道缓解了部分情况）。
2. **依赖 operands 而非 DType**：解析要看实际数组的值（值-based 提升），难以缓存、难以扩展自定义 dtype。
3. **类型解析与提升混在一起**：难以表达「我先提升到某类型，再选循环」这种两段式逻辑。

NumPy 从 1.20 起按 NEP 43 引入 **ArrayMethod** 重写这套机制。核心思想：

- 把「一条循环」抽象成一个 `ArrayMethod` 对象，它知道自己服务哪些 DType、如何 resolve descriptors、如何交出 strided loop。
- 用一张 `_loops` 字典（`DType 元组 -> (DType 元组, ArrayMethod 或 promoter)`）做多重分派，结果可缓存。
- 引入 **promoter**：当没有直接匹配的循环时，调用 promoter 计算出「应该提升到什么 DType」，再递归查找。
- 旧的 `functions[]`/`types[]` 被**包装**成一个 legacy ArrayMethod，作为最后兜底——这就是新旧机制能共存的原因。

#### 4.4.2 核心流程

`dispatching.cpp` 顶部注释把新机制概括为五步：

```
1. signature（dtype=/signature= 固定的 DType）覆盖 operand_DTypes
2. 查 _dispatch_cache：命中 → 跳到 4
3. resolve_implementation_info：遍历 _loops，找「最佳匹配」的 ArrayMethod/promoter
   （匹配条件：循环登记的 DType 是 operand_DType 的父类）
4. 若第 3 步找到的是 promoter：调用它修改 operand_DTypes，回到第 2 步
5. 拿到最终 ArrayMethod，把它的登记 DType 拷进 signature，供内层循环使用
```

其中第 3 步的「最佳匹配」用 DType 子类关系判定：若一个循环登记的 `(Float64, Float64)->Float64`，而输入是 `(Float32, Float32)`，因为 Float64 不是 Float32 的父类，所以不匹配；反之 `Floating` 这类抽象 DType 才能匹配多种具体浮点类型。

当第 3、4 步都找不到时，回退到旧机制：调用 `ufunc->type_resolver`（即 4.3 节的函数）做线性搜索，把结果再包装回 DType 维度。

#### 4.4.3 源码精读

**总览注释**：

[dispatching.cpp:L1-L36](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/umath/dispatching.cpp#L1-L36) 完整描述了 `_loops`、`_dispatch_cache`、`operand_DTypes`、`signature` 的角色与五步流程。L9-L10 点明 `_loops` 是有序字典，值是 `(dtypes, ArrayMethod)` 或 `(dtypes, promoter)`。

**主查找函数**（4.2 节第 ③ 步调用的 `promote_and_get_ufuncimpl` 的内核）：

[dispatching.cpp:L869-L916](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/umath/dispatching.cpp#L869-L916) 的 `promote_and_get_info_and_ufuncimpl`。三步清晰可见：
- L884-L891：查 `_dispatch_cache`，命中且是 ArrayMethod（非 promoter）就直接返回——这就是「第一次解析后，后续同类型调用飞快」的原因。
- L897-L916：缓存未命中则调 `resolve_implementation_info` 全量搜索，成功且是 ArrayMethod 就写入缓存返回。
- L918-L940 之后：若拿到的是 promoter，调 `call_promoter_and_recurse` 递归。

**全量搜索**：

[dispatching.cpp:L307-L319](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/umath/dispatching.cpp#L307-L319) 的 `resolve_implementation_info` 取 `PyDict_Values(ufunc->_loops)` 快照（L315，注释说快照对并发添加安全），然后遍历每条 `(dtypes, ArrayMethod/promoter)` 用 DType 子类关系打分，选出「最佳匹配」。

**回退到旧机制的桥梁**：

[dispatching.cpp:L718-L757](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/umath/dispatching.cpp#L718-L757) 的 `legacy_promote_using_legacy_type_resolver`。L743-L745 直接调用 `ufunc->type_resolver(...)`——也就是 4.3 节的 `PyUFunc_DefaultTypeResolver` / `PyUFunc_AdditionTypeResolver`。L732-L737 注释解释：这里用 `NPY_UNSAFE_CASTING` 是因为提升/分派阶段不关心 cast 安全性（那由后续 `resolve_descriptors` 把关）。这个函数把旧解析器算出的 `out_descrs` 翻译回 DType，让旧机制的结果能塞进新的 `_loops`/缓存体系。这是新旧两套机制真正的缝合点。

**ArrayMethod 的公开结构**：

[dtype_api.h:L139-L146](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/include/numpy/dtype_api.h#L139-L146) 定义 `PyArrayMethod_Spec`：`name`、`nin`/`nout`、`casting`、`flags`、`dtypes`（这条循环服务的 DType 数组）、`slots`（用 Python limited API 风格的 slot 机制挂回调）。

[dtype_api.h:L163-L173](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/include/numpy/dtype_api.h#L163-L173) 列出 slot ID：`NPY_METH_resolve_descriptors`（2，算 loop 用的 descr）、`NPY_METH_strided_loop`（5，通用 strided 内层循环）、`NPY_METH_contiguous_loop`（6，连续内存专用更快循环）、`NPY_METH_unaligned_strided_loop`（7，未对齐专用）等。这些 slot 正好对应 4.2 节 `get_strided_loop` 取出来的那批循环函数。

`resolve_descriptors` 回调签名：

[dtype_api.h:L185-L194](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/include/numpy/dtype_api.h#L185-L194) 的 `PyArrayMethod_ResolveDescriptors`：接收 method、登记 DType、输入 descr（输出端可为 NULL）、待填的 `loop_descrs`，返回 cast 安全级别。这就是 4.2 节第 ④ 步 `resolve_descriptors`（[ufunc_object.c:L4167](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/umath/ufunc_object.c#L4167)）最终调到的东西——它把「DType 级别的选择」落实为「descr 实例级别的选择」（比如从 `float64` DType 和具体 endian/metadata 得到确切的 `descr`）。

**注册新循环的入口**：

[dispatching.cpp:L137-L139](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/umath/dispatching.cpp#L137-L139) 的 `PyUFunc_AddLoopFromSpec` 是给 ufunc 追加 ArrayMethod 循环的公开 API（内部转 `PyUFunc_AddLoopFromSpec_int`，L144）。例如 `np.add` 的字符串加法循环就是通过它在 [string_ufuncs.cpp:L1493-L1494](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/umath/string_ufuncs.cpp#L1493-L1494) 注册的——这些循环**不会**出现在 `np.add.types` 里（因为 `types` 只读旧 `functions[]`/`types[]`），但会进入 `_loops` 字典参与新式分派。

#### 4.4.4 代码实践

**实践目标**：感受 `_dispatch_cache` 的存在——同类型重复调用应命中缓存，避免重复类型解析。

**操作步骤**（源码阅读型 + 运行观察）：

1. 运行：
   ```python
   import numpy as np
   a = np.arange(1000, dtype=np.float64)
   b = np.arange(1000, dtype=np.float64)
   # 第一次调用：触发完整类型解析，结果写入 ufunc._dispatch_cache
   _ = a + b
   # 后续同类型调用：命中缓存，直接拿到 ArrayMethod
   _ = a + b
   ```
2. 对照源码说明：第一次 `a + b` 走了 `promote_and_get_info_and_ufuncimpl` 的哪几个分支？第二次呢？
3. 思考：为什么 `np.add` 的字符串循环（`numpy.strings.add` / string dtype）不出现在 `np.add.types`，却仍能正确执行 `'a' + 'b'`（对 string 数组）？

**需要观察的现象**：两次 `a + b` 都返回正确结果，但第二次理论上有更低的分派开销。

**预期结果与分析**：
- 第一次：`_dispatch_cache` 未命中（L884 返回 NULL）→ `resolve_implementation_info` 全量搜索（L898）→ 找到 float64 的 ArrayMethod → 写入缓存（L909-L914）。
- 第二次：`_dispatch_cache` 命中且是 ArrayMethod（L887-L890）→ 直接返回，跳过全量搜索。
- 字符串加法：通过 `PyUFunc_AddLoopFromSpec` 注册进 `_loops` 字典，走新式分派；而 `np.add.types` 只读旧的 `ufunc->types` 数组，自然看不到它。这说明 **`types` 属性只反映 legacy 循环，不反映全部循环**。

**关于运行**：缓存命中无法从 Python 层直接观测（无公开 API 暴露 `_dispatch_cache`），上述为源码层面的推断；若需验证可构建带 `NPY_UF_DBG_TRACING` 的 NumPy 观察调试输出，**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：新式分派里「最佳匹配」靠什么判定一条循环是否适配输入？为什么这比旧的线性搜索更适合扩展自定义 dtype？

**答案**：靠 DType 的子类关系——循环登记的 DType 必须是输入 DType 的（父）类。旧机制靠类型编号字面匹配 + casting 试配，无法表达「我的自定义 DType 是 `Floating` 的子类，所以所有浮点循环都该能用」这种关系；新机制用 DType 元类继承天然支持。

**练习 2**：`legacy_promote_using_legacy_type_resolver` 为什么用 `NPY_UNSAFE_CASTING` 调用旧解析器？这样做安全吗？

**答案**：因为这一步只做「提升/分派」——决定用哪条循环、提升到什么 DType，cast 安全性在此无关紧要。真正的安全检查在后续 `resolve_descriptors`（返回 cast safety）和输出写回时进行。所以用 unsafe 不会让不安全转换漏检，只是让旧解析器「别因为 casting 限制而拒绝给出提升建议」。

---

## 5. 综合实践

把本讲四个模块串起来，完成一次「调用链全跟踪」。

**任务**：选取 `np.add`，对下面这一次调用，写出从 Python 入口到内层循环的完整链路，并标注每一步对应的源码位置与字段/函数：

```python
import numpy as np
out = np.add(np.arange(3, dtype=np.int8), np.arange(3, dtype=np.int16))
print(out.dtype)   # 期望 int16
```

**要求**：

1. 指出 `np.add` 这个对象的 C 类型，以及它的 `vectorcall`、`type_resolver` 字段分别指向哪个函数（提示：`np.add` 用的是 `PyUFunc_AdditionTypeResolver`，不是默认解析器）。说明 `vectorcall`/`type_resolver` 是在哪个构造函数里赋默认值的。
2. 按本讲 4.2 节的五步流水线，列出每一步调用的函数名与源码行号。
3. 解释第 ③ 步：新式分派（`promote_and_get_ufuncimpl`）先查 `_dispatch_cache`，未命中时走 `resolve_implementation_info`；若新机制找不到，最终回退到 `legacy_promote_using_legacy_type_resolver` 调用 `ufunc->type_resolver`。对 `int8 + int16` 这一具体输入，说明实际走的是哪条路径、`PyUFunc_AdditionTypeResolver` 如何委托给 `PyUFunc_SimpleUniformOperationTypeResolver`、后者又如何用 `PyArray_ResultType` 得到 `int16`。
4. 解释第 ④ 步：`resolve_descriptors` 把 DType 级选择落实为 descr 级；第 ⑤ 步 `PyUFunc_GenericFunctionInternal` 如何经 `get_strided_loop` 取到 strided 内层循环并执行。
5. 用 `np.add.types` 确认确实没有 `int8,int16->int16` 这条 legacy 循环，印证「循环按结果类型而非输入字面类型选择」。

**预期产出**：一份调用链文档，形如：

```
np.add(int8_arr, int16_arr)
 → ufunc_generic_vectorcall            [ufunc_object.c:4905]
 → ufunc_generic_fastcall              [ufunc_object.c:4560]
   ├─ 参数解析 / CheckOverride          [ufunc_object.c:4614, 4760]
   ├─ promote_and_get_ufuncimpl        [ufunc_object.c:4829]
   │   → promote_and_get_info_and_ufuncimpl  [dispatching.cpp:869]
   │     （缓存未命中 → resolve_implementation_info [dispatching.cpp:308]
   │        → 未直接命中 → 回退 legacy_promote_using_legacy_type_resolver
   │           [dispatching.cpp:719] → ufunc->type_resolver =
   │           PyUFunc_AdditionTypeResolver [ufunc_type_resolution.c:805]
   │           → PyUFunc_SimpleUniformOperationTypeResolver [ufunc_type_resolution.c:537]
   │           → PyArray_ResultType 得 int16）
   ├─ resolve_descriptors              [ufunc_object.c:4167 / 4838]
   └─ PyUFunc_GenericFunctionInternal  [ufunc_object.c:2192]
       → execute_ufunc_loop → get_strided_loop → int16 加法内层循环
```

具体路径中「新机制是否直接命中还是回退 legacy」取决于 `np.add` 的 `_loops` 注册情况，**待本地验证**；但无论走哪条，最终都通过 `PyArray_ResultType` 把 `int8` 提升为 `int16` 并选中 `hh->h` 循环。

## 6. 本讲小结

- `PyUFuncObject` 是一张「类型→循环」分发表加执行框架：`functions[]`/`types[]`/`ntypes` 描述循环集合，`nin`/`nout`/`identity`/`type_resolver`/`vectorcall` 描述如何执行；Python 层的 `np.add.nin`、`np.add.types`、`np.add.identity` 等属性都是对这套字段的薄封装 getter。
- 一次 ufunc 调用是一条五步流水线：vectorcall 入口 → `ufunc_generic_fastcall` 主控（参数解析 + 重载检查）→ `promote_and_get_ufuncimpl` 挑循环 → `resolve_descriptors` 定 descr → `PyUFunc_GenericFunctionInternal` 执行内层循环。
- 旧式类型解析在 `functions[]`/`types[]` 表上线性搜索匹配循环，`PyUFunc_DefaultTypeResolver` 是入口；`xx->x` 模式的 ufunc（含 `np.add`）走 `PyUFunc_SimpleUniformOperationTypeResolver` 快速通道，用 `PyArray_ResultType` 一步算出结果类型，无需遍历。
- 循环是按「提升后的结果类型」而非「输入字面类型」选择的：`int8 + int16` 提升到 `int16` 后跑 `hh->h` 循环，所以 `types` 表里不必为每种输入组合都注册一条。
- 新式 ArrayMethod 机制用 `_loops` 字典做 DType 多重分派、用 promoter 处理提升、用 `_dispatch_cache` 缓存解析结果；旧的 `functions[]`/`types[]` 被包装成 legacy ArrayMethod，经 `legacy_promote_using_legacy_type_resolver` 桥接，新旧机制共存。
- `np.add.types` 只反映 legacy 循环，不反映经 `PyUFunc_AddLoopFromSpec` 注册进 `_loops` 的新式循环（如字符串加法）。

## 7. 下一步学习建议

- **u4-l4 归约、累积与方法分发**：本讲的 `identity` 字段和 `type_resolver` 在归约里有特殊作用（reduce 需要单位元、需要把单输入提升到循环类型），建议接着读 `numpy/_core/src/umath/reduction.c` 与 `_methods.py`，看 `ndarray.sum()` 如何复用本讲的分派机制。
- **u4-l5 广播、SIMD 与性能优化**：本讲多次提到 `get_strided_loop` 与 `NPY_METH_contiguous_loop`/`unaligned_strided_loop` 等 slot——下一讲会展示这些 strided loop 如何被 `.dispatch.c` 按不同 CPU 特性生成多份 SIMD 版本，并讲解运行时 CPU 分发。
- **u8-l3 自定义 dtype 与 DType API**：若你想自己注册一条 ufunc 循环，本讲的 `PyUFunc_AddLoopFromSpec` + `PyArrayMethod_Spec` + `NPY_METH_resolve_descriptors` 就是入口；建议结合 `numpy/_core/src/umath/_scaled_float_dtype.c` 或 `_rational_tests.c` 实操。
- **延伸阅读**：NEP 43（新式 ufunc/DType 分派设计）、`numpy/_core/src/umath/dispatching.cpp` 顶部注释、以及 `doc/source/dev/internals.code-explanations.rst` 中关于 ufunc 的章节。
