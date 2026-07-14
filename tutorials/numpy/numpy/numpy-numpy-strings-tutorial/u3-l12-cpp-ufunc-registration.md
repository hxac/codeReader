# C++ ufunc 循环注册：string_ufuncs.cpp

## 1. 本讲目标

本讲是专家层的第一篇，从 Python 包装层（u2 系列）下探到 C++ 实现层。读完本讲你应当能够：

- 说清 `init_string_ufuncs` 这个总入口把定长字符串（`bytes_`/`str_`）的 C 循环挂载到 umath 模块私有 ufunc 上的完整链路。
- 掌握三层注册架构：底层 `add_loop` → 中层 `init_ufunc`/`init_mixed_type_ufunc`（以及比较族专用的 `add_loops` 变参模板）→ 顶层 `init_string_ufuncs`。
- 看懂 `ENCODING`（ASCII/UTF32）模板如何让一份循环代码同时服务 `bytes_` 与 `str_`，以及 `NPY_OBJECT` 哨兵如何按编码替换成真实 dtype。
- 理解 `resolve_descriptors` 在「输出 dtype 决策」中的三种典型写法（按输入求和、强制要求 `out`、输出等于输入）。
- 能够在源码中定位 `_center`/`_ljust`/`_rjust` 共用同一个 loop 函数的证据，并解释 center 为何能支持「ASCII fillchar 配 UTF32 输入」。

## 2. 前置知识

本讲假设你已学完 u2 系列（尤其 u2-l5「通用辅助函数与 dtype 分发套路」与 u2-l9「对齐与填充类」）。需要 recall 的关键认知：

- **三种字符串 dtype**：变长 `StringDType`（`dtype.char == 'T'`，由 `stringdtype_ufuncs.cpp` 单独处理，不在本讲范围）、定长 `bytes_`（`'S'`，C 层称 ASCII/Bytes）、定长 `str_`（`'U'`，UCS4，C 层称 UTF32/Unicode）。本讲只讲定长两种。
- **输出 dtype 路径 A**：对齐/填充类函数因输出宽度无法从输入 dtype 推断，由 Python 层用 `str_len` 量尺寸、开好定长 `out`，再交给 C 层 ufunc 写入。这正是本讲 `string_center_ljust_rjust_resolve_descriptors` 强制要求 `out` 的根源。
- **ufunc 与 ArrayMethod**：NumPy 2.x 里，一个 ufunc 的每种「输入 dtype 组合 → 实现」是一条 `PyArrayMethod`，由 `PyArrayMethod_Spec` 描述，包含 `strided_loop`（步进循环函数指针）与可选的 `resolve_descriptors`（输出描述符解析）等 slot。

几个本讲会反复出现的 C/C++ 概念，先用一句话解释：

- **模板（template）**：C++ 用 `template <ENCODING enc>` 让同一份函数源码按不同编码编译出多个实例，避免为 ASCII/UTF32 各写一遍。
- **变参模板 + 递归继承**：`add_loops` 用 `COMP...` 参数包 + 特化递归展开，把 6 个比较运算批量注册。
- **static_data**：每条循环可挂一份静态数据（如「左对齐/右对齐/居中」枚举），循环运行时从 `context->method->static_data` 取出，从而让一个 loop 函数服务多种行为。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [numpy/_core/src/umath/string_ufuncs.cpp](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_ufuncs.cpp) | 本讲主角：定长字符串全部 ufunc 循环、`resolve_descriptors`、promoter 与注册入口 `init_string_ufuncs` 都在这里。 |
| [numpy/_core/src/umath/string_ufuncs.h](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_ufuncs.h) | 对外只导出 `init_string_ufuncs` 与 `_umath_strings_richcompare` 两个 C 符号。 |
| [numpy/_core/src/umath/umathmodule.c](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/umathmodule.c) | umath 模块初始化处，在模块字典 `d` 上调用 `init_string_ufuncs(d)`。 |
| [numpy/_core/src/umath/dispatching.cpp](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/dispatching.cpp) | 提供 `PyUFunc_AddLoopFromSpec_int`，把一条 spec 真正挂到 ufunc 上。 |
| [numpy/_core/src/multiarray/array_method.c](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/multiarray/array_method.c) | `PyArrayMethod_FromSpec_int` 的实现，解释 `priv` 标志为何必须是 1。 |
| [numpy/_core/strings.py](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py) | Python 包装层，以 `center` 为例对照 C 层注册的输入/输出约定。 |

> 说明：本讲引用的 C/C++ 源码位于 `numpy/_core/src/umath/` 与 `numpy/_core/src/multiarray/`，不在 `numpy/strings/` 门面包内；永久链接使用仓库根相对路径以保证可直接打开。

## 4. 核心概念与源码讲解

### 4.1 注册总入口：init_string_ufuncs 与三层注册架构

#### 4.1.1 概念说明

`numpy.strings` 里那些「直接复用型」与「Python 包装型」函数（u2-l6/u2-l9），最终都要落到 C 层的 ufunc 循环。这些循环的注册集中在一个函数 `init_string_ufuncs`。它的职责不是「创建 ufunc」，而是「往已存在的 ufunc 上挂循环」——umath 模块在更早的阶段已经把 `add`、`multiply`、`str_len`、`_center`、`_strip_whitespace` 等 ufunc 对象放进了模块字典，`init_string_ufuncs` 拿到字典后，按名字逐个取出 ufunc，再为它们注册定长字符串专用的循环。

整个注册是**三层架构**：

1. 顶层 `init_string_ufuncs`：枚举所有字符串函数，为每个函数调用中层 helper。
2. 中层 `init_ufunc` / `init_mixed_type_ufunc`（以及比较族专用的 `add_loops`）：构造 `PyArrayMethod_Spec`（填 nin/nout、dtype 列表、slots），再调底层。
3. 底层 `add_loop`：按名字从字典取出 ufunc，把循环函数指针塞进 spec 的 `slots[0].pfunc`，调 `PyUFunc_AddLoopFromSpec_int` 完成挂载。

#### 4.1.2 核心流程

```
umathmodule.c: PyInit_umath 阶段
  └─ init_string_ufuncs(d)            # d 是 umath 模块字典
       ├─ init_comparison(umath)      # 比较族：用 add_loops 批量注册 6×2 条
       ├─ init_ufunc("add", ...)      # add：ASCII + UTF32 各一条
       ├─ init_ufunc("multiply", ...) # str×int 与 int×str，各 ASCII + UTF32
       ├─ init_ufunc("str_len", ...)  # 长度
       ├─ init_ufunc("isalpha", ...)  # 一族判断函数（循环里用 static_data 挑方法）
       ├─ init_ufunc("find", ...)     # findlike 五件套
       ├─ init_ufunc("_replace", ...)
       ├─ init_ufunc("startswith"/"endswith", ...)
       ├─ init_ufunc("_lstrip_whitespace"/...)  # strip 空白分支
       ├─ init_ufunc("_lstrip_chars"/...)        # strip 字符集分支
       ├─ init_ufunc("_expandtabs_length"/"_expandtabs", ...)
       ├─ init_mixed_type_ufunc("_center"/"_ljust"/"_rjust", ...)  # 双编码混合
       ├─ init_ufunc("_zfill", ...)
       ├─ init_ufunc("_partition_index"/"_rpartition_index", ...)
       └─ init_ufunc("_slice", ...)
```

每个 `init_ufunc` / `init_mixed_type_ufunc` 内部最后都汇聚到 `add_loop`，`add_loop` 再调 `PyUFunc_AddLoopFromSpec_int(ufunc, spec, 1)`。

#### 4.1.3 源码精读

umath 模块初始化时调用入口，传入模块字典 `d`：

[umathmodule.c:274-276](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/umathmodule.c#L274-L276) —— 在逻辑/比较 ufunc 的 promoter 安装完之后，调用 `init_string_ufuncs(d)`；紧接着才会调用 `init_stringdtype_ufuncs(m)` 处理变长 `'T'` 类型（那是 u3-l14 的内容）。

入口函数签名与第一行可看出它操作的是「按名字取 ufunc」的字典：

[string_ufuncs.cpp:1481-1492](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_ufuncs.cpp#L1481-L1492) —— `init_string_ufuncs(PyObject *umath)` 接收的就是模块字典；开头定义一个可复用的 `dtypes[]` 缓冲，并声明「用 `NPY_OBJECT` 作为哨兵，稍后按编码替换成 `NPY_STRING` 或 `NPY_UNICODE`」。

`NPY_OBJECT` 哨兵的替换发生在中层 `init_ufunc` 里，这是 ENCODING 模板机制的关键：

[string_ufuncs.cpp:1372-1382](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_ufuncs.cpp#L1372-L1382) —— 遍历每个参数位：若 typenum 是 `NPY_OBJECT` 且 `enc==UTF32` 则替换为 `PyArray_UnicodeDType`（`'U'`），若 `enc==ASCII` 则替换为 `PyArray_BytesDType`（`'S'`），否则原样使用。因此同一个 `init_ufunc(..., ENCODING::ASCII, ...)` 加 `init_ufunc(..., ENCODING::UTF32, ...)` 两次调用，就分别为 `bytes_` 与 `str_` 各注册了一条循环。

以最简单的 `str_len` 为例，它对两种编码各注册一次、且不需要 `resolve_descriptors`（传 `NULL`）：

[string_ufuncs.cpp:1536-1547](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_ufuncs.cpp#L1536-L1547) —— `str_len` 输入是字符串（`NPY_OBJECT` 哨兵）、输出是默认整数（`NPY_DEFAULT_INT`），循环分别是 `string_str_len_loop<ASCII>` 与 `<UTF32>`，`resolve_descriptors` 与 `static_data` 均为 `NULL`。输出 dtype 由默认解析器决定，无需自定义。

#### 4.1.4 代码实践

**实践目标**：把「总入口 → 中层 → 底层」这条链路在 `str_len` 上走一遍，确认每一段代码确实存在。

**操作步骤**（源码阅读型）：

1. 打开 `string_ufuncs.cpp`，定位 `init_string_ufuncs`（约 1481 行），找到 `str_len` 的两处 `init_ufunc(...)` 调用（1538、1543 行）。
2. 跟进 `init_ufunc`（1357 行），确认它把 `NPY_OBJECT` 哨兵按 `ENCODING` 替换为 Bytes/Unicode，组装 `PyArrayMethod_Spec`，最后调 `add_loop`（1406 行）。
3. 跟进 `add_loop`（1231 行），确认它用 `PyObject_GetItem(umath, name)` 按名字 `"str_len"` 取出 ufunc，再调 `PyUFunc_AddLoopFromSpec_int(ufunc, spec, 1)`。
4. 打开 `dispatching.cpp`（143 行）的 `PyUFunc_AddLoopFromSpec_int`，确认它调 `PyArrayMethod_FromSpec_int(spec, priv)` 造出 ArrayMethod，再 `PyUFunc_AddLoop` 挂到 ufunc。

**需要观察的现象**：`str_len` 这一条链路上，`resolve_descriptors` 始终是 `NULL`，说明输出 dtype 不需要自定义解析——因为输出是整数，宽度与输入字符串无关。

**预期结果**：你应当能在四个文件里各定位到一处代码，串成 `init_string_ufuncs → init_ufunc → add_loop → PyUFunc_AddLoopFromSpec_int` 的调用链。运行时可执行 `python -c "import numpy as np; print(np.strings.str_len(np.array(['abc','de'])))"`，应输出 `[3 2]`。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `init_string_ufuncs` 接收的是「模块字典」而不是「模块对象」？

**答案**：因为底层 `add_loop` 用 `PyObject_GetItem(umath, name)` 按名字取 ufunc，字典正好支持 `GetItem`；这样注册代码无需关心模块对象的属性协议，直接按名查表即可。见 [string_ufuncs.cpp:1232-1249](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_ufuncs.cpp#L1232-L1249)。

**练习 2**：`str_len` 的 `init_ufunc` 调用里 `resolve_descriptors` 传了 `NULL`，这会在 `init_ufunc` 内部产生什么差别？

**答案**：`init_ufunc` 在 [string_ufuncs.cpp:1388-1393](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_ufuncs.cpp#L1388-L1393) 判断 `resolve_descriptors != NULL` 才填 `NPY_METH_resolve_descriptors` slot；传 `NULL` 则该 slot 留空，使用 ufunc 的默认输出解析器（对 `str_len` 这种输出与输入宽度无关的函数足够了）。

### 4.2 底层注册原语：add_loop 与 PyUFunc_AddLoopFromSpec_int

#### 4.2.1 概念说明

`add_loop` 是整个文件里最底层的注册原语，所有中层 helper 最终都调它。它做三件事：按名字从 umath 字典取出 ufunc 对象、把传入的循环函数指针写进 spec 的第一个 slot、调用 `PyUFunc_AddLoopFromSpec_int` 完成挂载。注意它**不创建 ufunc**，只往已存在的 ufunc 上「加一条循环」。

`PyUFunc_AddLoopFromSpec_int` 是 NumPy ufunc 子系统的通用注册函数（不在 string_ufuncs.cpp 内，而在 `dispatching.cpp`），它把 spec 转成一个 `PyBoundArrayMethodObject`（绑定好 dtype 的 ArrayMethod），再以 `(dtypes_tuple, method)` 二元组的形式调 `PyUFunc_AddLoop` 注册到 ufunc。它带一个 `priv` 标志，本讲所有调用都传 `1`。

#### 4.2.2 核心流程

```
add_loop(umath, "str_len", &spec, string_str_len_loop<ASCII>)
  ├─ ufunc = PyObject_GetItem(umath, "str_len")   # 按名取 ufunc
  ├─ spec->slots[0].pfunc = (void*)loop            # 把循环指针塞进 strided_loop slot
  └─ PyUFunc_AddLoopFromSpec_int(ufunc, spec, 1)   # priv=1
        ├─ bmeth = PyArrayMethod_FromSpec_int(spec, priv=1)  # 造 ArrayMethod，允许私有 slot
        ├─ dtypes = tuple(bmeth->dtypes)
        ├─ info = (dtypes, bmeth->method)
        └─ PyUFunc_AddLoop(ufunc, info, 0)          # 真正挂到 ufunc
```

`priv=1` 的必要性：本讲的循环普遍使用 `_NPY_METH_static_data` 这个**私有 slot** 携带静态数据（如 `JUSTPOSITION`、`STRIPTYPE`、`STARTPOSITION`、buffer 方法指针）。`PyArrayMethod_FromSpec_int` 的注释明确写道：「Some slots are currently considered private, if not true [即 priv=0], these will be rejected」——见 [array_method.c:420-432](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/multiarray/array_method.c#L420-L432)。因此必须传 `priv=1`，否则带 `static_data` 的 spec 会被拒绝。

#### 4.2.3 源码精读

`add_loop` 全文很短，是理解整条链路的最小切入点：

[string_ufuncs.cpp:1231-1249](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_ufuncs.cpp#L1231-L1249) —— 注释说「This function replaces the strided loop with the passed in one, and registers it with the given ufunc」：用 `PyObject_GetItem` 按名取 ufunc，把 `loop` 写进 `spec->slots[0].pfunc`（即 `NPY_METH_strided_loop` 槽），再调 `PyUFunc_AddLoopFromSpec_int(ufunc, spec, 1)`。

通用注册函数把 spec 落地为 ufunc 上的一条循环：

[dispatching.cpp:143-172](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/dispatching.cpp#L143-L172) —— 公共版 `PyUFunc_AddLoopFromSpec` 只是转调 `PyUFunc_AddLoopFromSpec_int(ufunc, spec, 0)`；带 `priv` 的内部版先用 `PyArrayMethod_FromSpec_int(spec, priv)` 造出绑定 dtype 的 ArrayMethod，把 dtype 列表打包成 tuple，再与 method 一起交给 `PyUFunc_AddLoop`。这一步之后，ufunc 就多了一条「(输入 dtype 组合) → 该循环」的实现。

`init_ufunc` 在调 `add_loop` 前组装的 slots 正好体现了 `static_data` 与 `resolve_descriptors` 两个可选 slot：

[string_ufuncs.cpp:1384-1393](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_ufuncs.cpp#L1384-L1393) —— `slots[0]` 是 `NPY_METH_strided_loop`（指针由 `add_loop` 后填），`slots[1]` 是私有 `_NPY_METH_static_data`（故需要 `priv=1`），`slots[2]` 视情况填 `NPY_METH_resolve_descriptors`，`slots[3]` 是终止符 `{0, nullptr}`。

#### 4.2.4 代码实践

**实践目标**：确认 `priv=1` 与 `_NPY_METH_static_data` 的因果关系。

**操作步骤**（源码阅读型）：

1. 在 `string_ufuncs.cpp` 中搜索 `_NPY_METH_static_data`，列出所有用到 static_data 的函数族（提示：startswith/endswith 用 `STARTPOSITION`、strip 用 `STRIPTYPE`、center/ljust/rjust 用 `JUSTPOSITION`、isalpha 等用 buffer 方法指针）。
2. 打开 [array_method.c:420-432](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/multiarray/array_method.c#L420-L432) 阅读注释，确认 `private` 标志的作用是放行私有 slot。
3. 反问：若把 `add_loop` 里的 `PyUFunc_AddLoopFromSpec_int(ufunc, spec, 1)` 改成 `0`，带 static_data 的循环会怎样？

**需要观察的现象**：所有依赖 `context->method->static_data` 的循环，其注册链路上 `_NPY_METH_static_data` slot 都非空，且都经 `priv=1` 注册。

**预期结果**：得出结论——`priv=1` 是这些字符串循环能携带「行为枚举」静态数据的必要条件。**待本地验证**：若你本地能编译 NumPy，可临时把某处 `1` 改 `0` 观察启动报错（不要提交该改动）。

#### 4.2.5 小练习与答案

**练习 1**：`add_loop` 为什么要 `Py_DECREF(ufunc)`？

**答案**：`PyObject_GetItem` 返回的是新引用，用完后必须解引用以避免内存泄漏；见 [string_ufuncs.cpp:1239-1248](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_ufuncs.cpp#L1239-L1248)。

**练习 2**：`PyUFunc_AddLoopFromSpec`（公共版）与 `PyUFunc_AddLoopFromSpec_int(..., 1)`（本讲用的内部版）有何区别？

**答案**：公共版转调内部版但 `priv=0`，会拒绝私有 slot；内部版 `priv=1` 放行 `_NPY_METH_static_data` 等私有 slot。本讲循环依赖 static_data，故必须用 `priv=1`。见 [dispatching.cpp:136-144](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/dispatching.cpp#L136-L144)。

### 4.3 比较族的模板批量注册：add_loops 变参模板

#### 4.3.1 概念说明

比较运算有 6 个（`equal`/`not_equal`/`less`/`less_equal`/`greater`/`greater_equal`），又要支持 ASCII 与 UTF32 两种编码，共 12 条循环。如果手写 12 次 `init_ufunc` 会非常重复。`add_loops` 是一个 C++ **变参模板 + 递归特化** 的结构体，专门把一个 `COMP...` 参数包展开，逐个调 `add_loop` 注册。这样 `init_comparison` 只需声明两个 `using`（ASCII 版与 UTF32 版），各传 6 个 `COMP` 枚举，就能批量注册。

`COMP` 是一个 `enum class`，`comp_name` 把它映射成 ufunc 名字字符串（`equal`、`less` 等）。循环本体 `string_comparison_loop<rstrip, comp, enc>` 是三参模板，靠 `comp` 模板参数区分六种比较语义。

#### 4.3.2 核心流程

`add_loops<rstrip, enc, COMP...>` 的递归展开：

```
add_loops<false, ASCII, EQ,NE,LT,LE,GT,GE>::operator()(umath, &spec)
  → add_loop(umath, "equal", spec, string_comparison_loop<false, EQ, ASCII>)
  → add_loops<false, ASCII, NE,LT,LE,GT,GE>::operator()(umath, &spec)   # 递归余下
    → add_loop(umath, "not_equal", ...)
    → ... 直到参数包为空，命中「空包」特化，返回 0
```

两个特化：
- **空包特化**（终止）：`add_loops<rstrip, enc>`，直接返回 0。
- **递归特化**：`add_loops<rstrip, enc, comp, comps...>`，注册 `comp` 这一个，再递归 `add_loops<rstrip, enc, comps...>`。

`init_comparison` 用 `using` 起别名，先注册 6 个 ASCII 比较，再把 `dtypes[0]/[1]` 换成 Unicode 注册 6 个 UTF32 比较。

#### 4.3.3 源码精读

`COMP` 枚举与名字映射：

[string_ufuncs.cpp:28-45](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_ufuncs.cpp#L28-L45) —— `enum class COMP { EQ, NE, LT, LE, GT, GE }`；`comp_name` 把枚举值映射成 ufunc 在字典里的名字。`add_loops` 注册时就是用这个名字去字典取 ufunc。

变参模板递归结构：

[string_ufuncs.cpp:1252-1274](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_ufuncs.cpp#L1252-L1274) —— 先声明主模板 `template<bool rstrip, ENCODING enc, COMP...> struct add_loops;`；空包特化返回 0；递归特化取首元素 `comp`，把 `string_comparison_loop<rstrip, comp, enc>` 作为循环指针调 `add_loop(umath, comp_name(comp), spec, loop)`，再递归处理 `comps...`。

`init_comparison` 用别名触发批量注册：

[string_ufuncs.cpp:1296-1316](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_ufuncs.cpp#L1296-L1316) —— 构造一个共享 `spec`（nin=2, nout=1, dtypes={String, String, Bool}）；用 `using string_looper = add_loops<false, ASCII, EQ,NE,LT,LE,GT,GE>` 起别名并调用，注册 6 条 ASCII 比较；再把 `dtypes[0]/[1]` 换成 Unicode，用 `ucs_looper`（UTF32 版）注册 6 条 UTF32 比较。注意比较族 `rstrip=false`（`numpy.strings` 不剥空白，`numpy.char` 的 rstrip 行为走另一条路径 `_umath_strings_richcompare`，见 [string_ufuncs.cpp:1933-1947](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_ufuncs.cpp#L1933-L1947)）。

#### 4.3.4 代码实践

**实践目标**：理解变参模板如何把「6 个比较 × 2 编码 = 12 条循环」压缩成两次别名调用。

**操作步骤**（源码阅读型）：

1. 在 [string_ufuncs.cpp:1252-1274](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_ufuncs.cpp#L1252-L1274) 画出递归展开：`add_loops<false, ASCII, EQ,NE,LT,LE,GT,GE>` 会展开成 6 次 `add_loop` 调用，分别用名字 `equal`/`not_equal`/`less`/`less_equal`/`greater`/`greater_equal`。
2. 确认每次 `add_loop` 取到的 ufunc 名字来自 `comp_name(comp)`，循环指针来自 `string_comparison_loop<rstrip, comp, enc>`。
3. 运行验证：`python -c "import numpy as np; a=np.array(['a','b']); b=np.array(['a','c']); print(np.strings.equal(a,b), np.strings.less(a,b))"`，应输出 `[ True False] [ True False]`。

**需要观察的现象**：6 个比较 ufunc 共用同一份 `string_comparison_loop` 模板源码，仅靠 `comp` 模板参数与 `comp_name` 区分行为与名字。

**预期结果**：12 条循环由两次 `using ... ()` 调用注册完成，证明变参模板把重复代码压到了最低。

#### 4.3.5 小练习与答案

**练习 1**：`add_loops` 为什么需要「空包特化」？

**答案**：递归特化每次剥掉首元素 `comp`，剩余 `comps...` 越来越短；当参数包为空时，递归特化无法匹配（没有首元素可取），必须用空包特化终止递归并返回 0。见 [string_ufuncs.cpp:1255-1260](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_ufuncs.cpp#L1255-L1260)。

**练习 2**：`init_comparison` 里 ASCII 与 UTF32 两次注册，`spec` 是否复用了同一个？

**答案**：是。同一个 `spec` 对象（`dtypes` 指向同一数组）被两次调用复用，只是第二次把 `dtypes[0]/[1]` 从 `String` 改成了 `Unicode`。`add_loop` 内部只读 `spec` 并把它交给 `PyUFunc_AddLoopFromSpec_int`（后者会复制所需信息），所以复用安全。见 [string_ufuncs.cpp:1310-1316](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_ufuncs.cpp#L1310-L1316)。

### 4.4 双编码模板与 center/ljust/rjust 共用循环

#### 4.4.1 概念说明

`center`/`ljust`/`rjust` 三个函数的填充逻辑高度相似，区别只在「填充加在哪一侧」（居中/左/右）。C 层没有为它们写三个 loop，而是写了一个 `string_center_ljust_rjust_loop`，用 `JUSTPOSITION` 枚举（`CENTER`/`LEFT`/`RIGHT`）作为 `static_data` 传入，循环运行时取出该枚举决定填充位置。这就是「一个 loop 函数服务三种行为」。

更关键的是，这个 loop 是**双编码模板** `template <ENCODING bufferenc, ENCODING fillenc>`：`bufferenc` 是输入字符串的编码，`fillenc` 是填充字符的编码。两者可以不同——这正是 center 能支持「ASCII fillchar 配 UTF32 输入」的根本原因。由于输入与填充字符的 dtype 可以不同，这里不能用 `init_ufunc`（它要求所有字符串位同为 ASCII 或同为 UTF32），而要用 `init_mixed_type_ufunc`，显式给出每个位的真实 typenum。

#### 4.4.2 核心流程

注册阶段为每个函数（`_center`/`_ljust`/`_rjust`）注册 4 种编码组合：

| 组合 | bufferenc（输入串） | fillenc（填充字符） | 输入 dtype | 填充 dtype | 输出 dtype | 注册行号 |
|------|------|------|------|------|------|------|
| 1 | ASCII | ASCII | `S` | `S` | `S` | 1793-1799 |
| 2 | ASCII | UTF32 | `S` | `U` | `S` | 1803-1809 |
| 3 | UTF32 | UTF32 | `U` | `U` | `U` | 1813-1819 |
| 4 | UTF32 | ASCII | `U` | `S` | `U` | 1823-1829 |

要点：**输出 dtype 跟随 `bufferenc`（输入串），不跟随 fillchar**。组合 4（UTF32 输入 + ASCII fillchar → Unicode 输出）就是「ASCII fillchar 配 UTF32 输入」的合法路径；组合 2（ASCII 输入 + UTF32 fillchar → bytes 输出）虽也注册，但循环体内有守卫：若 fillchar 码点 > 0x7F 则抛 `ValueError`，因为 bytes 输出存不下非 ASCII 字符。

循环运行时（以组合 4 为例）：

```
string_center_ljust_rjust_loop<UTF32, ASCII>(context, data, dimensions, strides)
  pos = *(JUSTPOSITION*)context->method->static_data   # CENTER/LEFT/RIGHT
  对每个元素:
    buf  = Buffer<UTF32>(in1, elsize1)   # 输入串按 4 字节/字符读
    fill = Buffer<ASCII>(in3, elsize3)   # 填充字符按 1 字节读
    outbuf = Buffer<UTF32>(out, outsize) # 输出按 4 字节/字符写
    # 因 bufferenc=ASCII && fillenc=UTF32 才检查；组合4不触发该守卫
    len = string_pad(buf, width, *fill, pos, outbuf)
    outbuf.buffer_fill_with_zeros_after_index(len)
```

#### 4.4.3 源码精读

双编码模板循环本体：

[string_ufuncs.cpp:537-576](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_ufuncs.cpp#L537-L576) —— `template <ENCODING bufferenc, ENCODING fillenc>` 双参模板；从 `context->method->static_data` 取 `JUSTPOSITION pos`；用 `Buffer<bufferenc>` 包输入与输出、`Buffer<fillenc>` 包填充字符；第 559 行的守卫 `if (bufferenc == ENCODING::ASCII && fillenc == ENCODING::UTF32 && *fill > 0x7F)` 仅对组合 2 生效，抛 `ValueError("non-ascii fill character is not allowed when buffer is ascii")`；最后调 `string_pad` 填充并把余下位置清零。

三个名字共用一个 loop 的证据——注册块：

[string_ufuncs.cpp:1781-1834](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_ufuncs.cpp#L1781-L1834) —— `center_ljust_rjust_names[] = {"_center", "_ljust", "_rjust"}` 与 `padpositions[] = {CENTER, LEFT, RIGHT}` 两个数组并行；`for (i=0..2)` 循环里，三个名字都调用同一个 `string_center_ljust_rjust_loop<...>` 模板实例，**唯一不同的是 `&padpositions[i]` 这份 static_data**。这就是「共用同一个 loop 函数」的直接证据：函数指针相同，行为差异完全由 static_data 携带。

四种编码组合各自由 `init_mixed_type_ufunc` 注册（因为它允许输入与填充字符 dtype 不同）：

[string_ufuncs.cpp:1789-1829](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_ufuncs.cpp#L1789-L1829) —— 组合 1 `dtypes={STRING,INT64,STRING,STRING}` + `<ASCII,ASCII>`；组合 2 `dtypes={STRING,INT64,UNICODE,STRING}` + `<ASCII,UTF32>`；组合 3 `dtypes={UNICODE,INT64,UNICODE,UNICODE}` + `<UTF32,UTF32>`；组合 4 `dtypes={UNICODE,INT64,STRING,UNICODE}` + `<UTF32,ASCII>`。注意 `dtypes[1]` 恒为 `NPY_INT64`（宽度），nin=3（串、宽度、填充字符）、nout=1。

`init_mixed_type_ufunc` 与 `init_ufunc` 的差别就在 dtype 处理：

[string_ufuncs.cpp:1420-1442](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_ufuncs.cpp#L1420-L1442) —— 注释明说「allows for mixed string dtypes in its parameters ... the typenums are always the correct ones」，即不做 `NPY_OBJECT` 哨兵替换，每个位用显式 typenum。这样输入（Unicode）与填充字符（String）才能各走各的编码。

输出 dtype 决策——`resolve_descriptors` 强制要求 `out`：

[string_ufuncs.cpp:1020-1056](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_ufuncs.cpp#L1020-L1056) —— 若 `given_descrs[3]`（输出位）为 `NULL`，抛 `TypeError("The 'out' kwarg is necessary. Use the version in numpy.strings without it.")`；否则只对四个描述符做 `ensure_canonical`，返回 `NPY_NO_CASTING`。这与 u2-l9 讲的「Python 层用 `str_len` 量尺寸、开好定长 `out` 再传入」完全对应——C 层拒绝自己猜输出宽度。

Python 包装层确实在调 C 前 `astype` 了 fillchar 并开好 `out`：

[strings.py:748-757](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L748-L757) —— 非 `'T'` 分支里 `fillchar = fillchar.astype(a.dtype, copy=False)`（把 fillchar 强制成与输入同 dtype）、`out_dtype = f"{a.dtype.char}{width.max()}"`、`out = np.empty_like(a, shape=shape, dtype=out_dtype)`，最后 `_center(a, width, fillchar, out=out)`。也就是说，走标准 `np.strings.center` 时 fillchar 会被同化，混合编码组合主要在直接调用私有 ufunc `_center` 时才会命中；docstring 也明确记录了「a 与 fillchar 可不同 dtype，但 a 为 'S' 时 fillchar 不能含非 ASCII」——见 [strings.py:719-721](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L719-L721)。

#### 4.4.4 代码实践

**实践目标**（本讲指定的核心实践）：在源码中定位 `_center`/`_ljust`/`_rjust` 共用同一个 loop 函数的证据，画出 ENCODING 与字符集组合下的循环注册表，说明为何 center 能支持 ASCII fillchar 配 UTF32 输入。

**操作步骤**（源码阅读型 + 可运行示例）：

1. **定位共用证据**：打开 [string_ufuncs.cpp:1781-1834](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_ufuncs.cpp#L1781-L1834)，确认 `for (i=0..2)` 内三个名字都调用 `string_center_ljust_rjust_loop<...>`，唯一变量是 `&padpositions[i]`；再打开 [string_ufuncs.cpp:543](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_ufuncs.cpp#L543) 确认循环从 `context->method->static_data` 取 `JUSTPOSITION`。
2. **画注册表**：按本节 4.4.2 的四行表格，把每个组合的 `<bufferenc, fillenc>`、`dtypes`、输出 dtype、行号填出来，标注哪一行是「UTF32 输入 + ASCII fillchar」（组合 4）、哪一行带非 ASCII 守卫（组合 2，line 559）。
3. **解释 ASCII fillchar + UTF32 输入**：组合 4 的输出是 Unicode（每字符 4 字节），可容纳任意码点；ASCII fillchar 码点 ≤ 0x7F，作为 UTF32 字符合法、无信息丢失，故 center 支持该组合。反过来组合 2 输出是 bytes，存不下 > 0x7F 的 fillchar，故有守卫。
4. **可运行验证**（来自 docstring 真实示例）：
   ```python
   import numpy as np
   c = np.array(['a1b2','1b2a','b2a1','2a1b'])  # dtype '<U4'
   print(np.strings.center(c, width=9))
   # 预期: array(['   a1b2  ', '   1b2a  ', '   b2a1  ', '   2a1b  '], dtype='<U9')
   print(np.strings.center(c, width=9, fillchar='*'))
   # 预期: array(['***a1b2**', '***1b2a**', '***b2a1**', '***2a1b**'], dtype='<U9')
   ```
   这里输入是 `str_`（UTF32）、`fillchar='*'`（ASCII），Python 层会把 fillchar `astype('U')` 同化，最终命中组合 3 `<UTF32,UTF32>`；若直接用私有 ufunc 且让 fillchar 保持 `bytes_`，则命中组合 4 `<UTF32,ASCII>`。

**需要观察的现象**：三个函数共享同一份循环源码；四行注册表覆盖了输入/填充编码的全部组合；输出 dtype 恒随输入串编码。

**预期结果**：得到一张四行注册表与一份「共用 loop + static_data 区分行为」的证据链，并能用「输出能否容纳 fillchar 码点」解释守卫只加在组合 2 上。

#### 4.4.5 小练习与答案

**练习 1**：如果把 `string_center_ljust_rjust_loop` 改成单编码模板 `template <ENCODING enc>`（即输入与填充同编码），会损失什么能力？

**答案**：会无法注册组合 2 与组合 4（输入与填充字符 dtype 不同的情形），即无法支持「ASCII fillchar 配 UTF32 输入」或反向混合。这也是为什么该 loop 必须双编码模板并用 `init_mixed_type_ufunc` 注册。见 [string_ufuncs.cpp:537-558](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_ufuncs.cpp#L537-L558)。

**练习 2**：为什么守卫 `*fill > 0x7F` 只在 `bufferenc==ASCII && fillenc==UTF32` 时检查，而不在组合 4（`UTF32,ASCII`）检查？

**答案**：组合 4 输出是 Unicode，可容纳任意码点，ASCII fillchar（≤ 0x7F）天然合法，无需检查；组合 2 输出是 bytes，只能存 ≤ 0x7F 的字符，故必须拦截非 ASCII fillchar。见 [string_ufuncs.cpp:559-562](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_ufuncs.cpp#L559-L562)。

**练习 3**：`center`/`ljust`/`rjust` 三者的 `resolve_descriptors` 与 `promoter` 是否也共用？

**答案**：是。三者都用同一个 `string_center_ljust_rjust_resolve_descriptors`（[1020-1056](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_ufuncs.cpp#L1020-L1056)）与同一个 `string_center_ljust_rjust_promoter`（[1005-1017](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_ufuncs.cpp#L1005-L1017)），在循环里通过 `padpositions[i]` 区分；promoter 会把 fillchar 与输出 dtype 都设成与输入相同。

### 4.5 resolve_descriptors 与输出 dtype 决策：以 strip 为代表

#### 4.5.1 概念说明

`resolve_descriptors` 是每条 ArrayMethod 可选的 slot，职责是：给定调用方提供的输入/输出描述符 `given_descrs`，产出循环真正使用的 `loop_descrs`，并返回一个 `NPY_CASTING` 安全级别。对字符串 ufunc 而言，它最核心的作用是**决定输出 dtype 的宽度**——而定长字符串的输出宽度往往无法仅凭输入 dtype 推断（取决于运行时数据，如对齐宽度、分隔符位置、重复次数）。本讲在源码里能归纳出三种典型写法：

- **A. 按输入求和算宽度**：`add` 把输出 elsize 设为两个输入 elsize 之和。
- **B. 强制要求 `out`**：`multiply`/`center`/`zfill`/`partition` 拒绝自动推断，要求调用方提供 `out`（由 Python 层量好尺寸）。
- **C. 输出等于输入**：`strip` 族输出不会比输入长，直接令输出 dtype = 输入 dtype，不预算宽度、不要求 `out`。

`string_strip_whitespace_resolve_descriptors` 是 C 类的代表，也是本讲指定的最小模块之一。它极短：把输出描述符设为与输入相同的规范描述符，返回 `NPY_NO_CASTING`。

#### 4.5.2 核心流程

strip 族的注册（u2-l10 已讲 Python 层双分支，这里看 C 层）：

```
# 空白分支（chars=None）：1 输入 1 输出
init_ufunc("_lstrip_whitespace"/"_rstrip_whitespace"/"_strip_whitespace",
           1, 1, dtypes, ASCII/UTF32,
           string_lrstrip_whitespace_loop<enc>,
           string_strip_whitespace_resolve_descriptors,   # ← C 类
           &striptypes[i])                                  # LEFT/RIGHT/BOTH

# 字符集分支（给定 chars）：2 输入 1 输出
init_ufunc("_lstrip_chars"/"_rstrip_chars"/"_strip_chars",
           2, 1, dtypes, ASCII/UTF32,
           string_lrstrip_chars_loop<enc>,
           string_strip_chars_resolve_descriptors,
           &striptypes[i])
```

`string_strip_whitespace_resolve_descriptors` 内部：

```
loop_descrs[0] = ensure_canonical(given_descrs[0])   # 规范化输入
loop_descrs[1] = loop_descrs[0]                      # 输出 = 输入（Py_INCREF）
return NPY_NO_CASTING                                # 无类型转换
```

因为剥空白只会让字符串变短或不变，输出宽度上界就是输入宽度，故可直接复用输入 dtype，既不必算宽度也不必要求 `out`。这与对齐函数（路径 A，要求 `out`）形成鲜明反差。

#### 4.5.3 源码精读

C 类 resolve_descriptors——strip 空白分支：

[string_ufuncs.cpp:806-823](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_ufuncs.cpp#L806-L823) —— `string_strip_whitespace_resolve_descriptors` 把 `loop_descrs[0]` 设为输入的规范描述符，再 `Py_INCREF` 后令 `loop_descrs[1] = loop_descrs[0]`（输出与输入同 dtype），返回 `NPY_NO_CASTING`。没有 `out` 检查、没有宽度计算——这是「输出等于输入」最干净的样子。

对照 A 类——`add` 按输入求和算宽度：

[string_ufuncs.cpp:726-768](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_ufuncs.cpp#L726-L768) —— `result_itemsize = given_descrs[0]->elsize + given_descrs[1]->elsize`；若溢出或超过 `NPY_MAX_INT` 则报错；否则 `loop_descrs[2] = PyArray_DescrNew(loop_descrs[0])` 并 `loop_descrs[2]->elsize += loop_descrs[1]->elsize`，即输出宽度 = 两输入宽度之和。`add` 能自己算宽度，故不要求 `out`。

对照 B 类——`multiply` 强制要求 `out`：

[string_ufuncs.cpp:771-803](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_ufuncs.cpp#L771-L803) —— 若 `given_descrs[2]`（输出）为 `NULL`，抛 `TypeError("The 'out' kwarg is necessary ... Use numpy.strings.multiply ...")`；否则仅 `ensure_canonical` 三个描述符。因为重复次数 `i` 是运行时参数，C 层无法仅凭输入 dtype 推断输出宽度，所以把量尺寸的责任交给 Python 层（u2-l6 讲的 `sys.maxsize` 溢出保护）。

strip 族的注册块——6 个 ufunc（3 方向 × 2 模式）：

[string_ufuncs.cpp:1693-1741](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_ufuncs.cpp#L1693-L1741) —— `strip_whitespace_names[] = {"_lstrip_whitespace","_rstrip_whitespace","_strip_whitespace"}` 配 `striptypes[] = {LEFTSTRIP,RIGHTSTRIP,BOTHSTRIP}`，三方向共用 `string_lrstrip_whitespace_loop`、靠 `&striptypes[i]` 区分；字符集分支 `strip_chars_names[]` 同理共用 `string_lrstrip_chars_loop`。两条分支各注册 ASCII 与 UTF32，共 12 条循环。

#### 4.5.4 代码实践

**实践目标**：用 `resolve_descriptors` 的写法把字符串 ufunc 分成三类，并验证 strip 走的是「输出等于输入」。

**操作步骤**（源码阅读型 + 可运行示例）：

1. 在 [string_ufuncs.cpp:806-823](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_ufuncs.cpp#L806-L823) 确认 strip 空白分支不检查 `out`、不计算宽度、输出 = 输入。
2. 对照 [726-768](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_ufuncs.cpp#L726-L768)（add，A 类）与 [771-803](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_ufuncs.cpp#L771-L803)（multiply，B 类），归纳三类。
3. 运行验证：`python -c "import numpy as np; a=np.array(['  ab  ']); print(repr(np.strings.strip(a)), np.strings.strip(a).dtype)"`，应得到 `'ab'` 且 dtype 与输入相同（`<U6`，未缩窄——定长 dtype 宽度是字段容量，不会随内容缩短）。

**需要观察的现象**：strip 输出 dtype 的宽度与输入一致（不会变成更窄的 `<U2`），因为 `resolve_descriptors` 直接复用了输入描述符；只是内容里空白被剥掉、尾部补零。

**预期结果**：strip 的输出 dtype == 输入 dtype，印证 C 类「输出等于输入」；而 `add` 输出会变宽（A 类）、`multiply` 必须由 Python 层提供 `out`（B 类）。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `strip` 的 `resolve_descriptors` 可以不要求 `out`，而 `center` 必须要求？

**答案**：strip 只会缩短或保持长度，输出宽度上界就是输入宽度，直接复用输入 dtype 即可；center 要把字符串填充到指定 `width`，输出宽度可能大于输入，且取决于运行时 `width` 参数，C 层无法凭输入 dtype 推断，故要求 Python 层量好尺寸并传入 `out`。对照 [806-823](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_ufuncs.cpp#L806-L823) 与 [1020-1056](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_ufuncs.cpp#L1020-L1056)。

**练习 2**：`string_strip_whitespace_resolve_descriptors` 返回 `NPY_NO_CASTING` 表示什么？

**答案**：表示循环不会做任何类型转换（输入与输出同 dtype、同规范形式），调度器据此跳过不必要的转换路径。`ensure_canonical` 只是把字节序/规范表示统一，不改变 dtype 本身。见 [string_ufuncs.cpp:819-822](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_ufuncs.cpp#L819-L822)。

**练习 3**：`add` 的 `resolve_descriptors` 里为什么要检查 `result_itemsize > NPY_MAX_INT || result_itemsize < 0`？

**答案**：两个 elsize 求和可能溢出（`npy_intp` 有符号，溢出后可能变负），需在产出非法宽度前拦截并报 `TypeError`，避免后续分配越界。见 [string_ufuncs.cpp:734-746](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_ufuncs.cpp#L734-L746)。

## 5. 综合实践

**任务**：以 `np.strings.center(np.array(['a1b2'], dtype='<U4'), 9, '*')` 为线索，把本讲四层知识串成一条端到端调用链，并产出一张「函数 → 注册层 → 循环 → resolve_descriptors 类别」对照表。

**步骤**：

1. **Python 层**（u2-l9 回顾）：阅读 [strings.py:736-757](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L736-L757)，确认 `center` 检测到非 `'T'` 后，执行 `fillchar.astype(a.dtype)`、`out_dtype='U9'`、`empty_like(...)`、`_center(a, width, fillchar, out=out)`。
2. **promoter 路由**：阅读 [string_ufuncs.cpp:1005-1017](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_ufuncs.cpp#L1005-L1017)，确认 `string_center_ljust_rjust_promoter` 把 fillchar 与输出 dtype 都设成与输入相同（Unicode），因而命中组合 3 `<UTF32,UTF32>`。
3. **resolve_descriptors**：阅读 [1020-1056](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_ufuncs.cpp#L1020-L1056)，确认 `given_descrs[3]`（out）非空、四个描述符被 `ensure_canonical`，返回 `NPY_NO_CASTING`——属 B 类（强制要求 `out`）。
4. **注册层回溯**：沿 [1781-1834](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_ufuncs.cpp#L1781-L1834) → `init_mixed_type_ufunc` → [add_loop:1231-1249](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_ufuncs.cpp#L1231-L1249) → [PyUFunc_AddLoopFromSpec_int:143-172](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/dispatching.cpp#L143-L172)，还原「这条循环是在 `init_string_ufuncs` 里被挂到 `_center` ufunc 上的」。
5. **循环执行**：阅读 [string_center_ljust_rjust_loop:537-576](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_ufuncs.cpp#L537-L576)，确认它从 `static_data` 取 `JUSTPOSITION::CENTER`，对每个元素调 `string_pad` 后清零尾部。

**产出**：一张对照表，至少包含 `center`、`add`、`multiply`、`strip` 四行，列出：注册用哪个 helper（`init_ufunc`/`init_mixed_type_ufunc`/`add_loops`）、循环模板、`resolve_descriptors` 类别（A/B/C）、是否要求 `out`、输出宽度由谁决定。

**预期结果**：

| 函数 | 注册 helper | 循环模板 | resolve_descriptors 类别 | 要求 out? | 输出宽度由谁决定 |
|------|------|------|------|------|------|
| `center` | `init_mixed_type_ufunc` | `string_center_ljust_rjust_loop<b,f>` | B | 是 | Python 层 `str_len` 量尺寸 |
| `add` | `init_ufunc` | `string_add_loop<enc>` | A | 否 | 两输入 elsize 之和 |
| `multiply` | `init_ufunc` | `string_multiply_*_loop<enc>` | B | 是 | Python 层预算 + 溢出检查 |
| `strip` | `init_ufunc` | `string_lrstrip_whitespace_loop<enc>` | C | 否 | 等于输入宽度 |

## 6. 本讲小结

- `init_string_ufuncs` 是定长字符串 ufunc 循环的注册总入口，由 `umathmodule.c` 在 umath 模块字典上调用；它不创建 ufunc，只往已存在的 ufunc 上挂循环。
- 注册是三层架构：底层 `add_loop`（按名取 ufunc、塞循环指针、调 `PyUFunc_AddLoopFromSpec_int`）→ 中层 `init_ufunc`/`init_mixed_type_ufunc`（组装 spec）→ 顶层 `init_string_ufuncs`（枚举函数）；比较族额外用 `add_loops` 变参模板批量注册。
- `ENCODING`（ASCII/UTF32）模板让一份循环服务 `bytes_` 与 `str_`；`init_ufunc` 用 `NPY_OBJECT` 哨兵按编码替换成 `String`/`Unicode`，`init_mixed_type_ufunc` 则用显式 typenum 支持输入与填充字符 dtype 不同。
- `priv=1` 是字符串循环能携带 `_NPY_METH_static_data`（如 `JUSTPOSITION`/`STRIPTYPE`）的必要条件；`_center`/`_ljust`/`_rjust` 共用同一个 `string_center_ljust_rjust_loop`，靠 static_data 区分行为。
- `string_center_ljust_rjust_loop` 是双编码模板 `<bufferenc, fillenc>`，注册了 4 种编码组合；组合 4（UTF32 输入 + ASCII fillchar → Unicode 输出）使 center 能支持「ASCII fillchar 配 UTF32 输入」，组合 2 则有非 ASCII 守卫。
- `resolve_descriptors` 有三种写法：A 类按输入求和（`add`）、B 类强制要求 `out`（`multiply`/`center`）、C 类输出等于输入（`string_strip_whitespace_resolve_descriptors`）。

## 7. 下一步学习建议

- **u3-l13 string_buffer 与 string_fastsearch**：本讲反复出现的 `Buffer<ENCODING>`、`getchar`、`string_pad` 都定义在 `string_buffer.h`，`ENCODING` 枚举也在那里；下一讲深入字符处理原语，理解「按字节读一个码点」的三种特化。
- **u3-l14 StringDType（'T'）专用 ufunc 循环**：本讲只覆盖定长 S/U；变长 `'T'` 走 `stringdtype_ufuncs.cpp`，Python 层 `char=='T'` 分支直接委托给它。建议对照 `init_stringdtype_ufuncs` 与本讲 `init_string_ufuncs` 的异同。
- **u3-l15 _vec_string 的 C 实现**：本讲的 ufunc 路径覆盖不到 `upper`/`mod`/`decode` 等函数，它们走 `_vec_string`；下一讲看通用桥的 C 侧实现。
- **延伸阅读**：可顺带读 `numpy/_core/src/umath/dispatching.cpp` 的 `PyUFunc_AddLoop` 与 promoter 机制，理解 ufunc 如何根据输入 dtype 选中最优循环。
