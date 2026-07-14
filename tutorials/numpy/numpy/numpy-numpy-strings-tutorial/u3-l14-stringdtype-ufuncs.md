# StringDType（'T'）专用 ufunc 循环

## 1. 本讲目标

本讲承接 u3-l12（定长字符串 `bytes_`/`str_` 的 C++ 循环注册），把视线转向第三种、也是结构最特殊的一种字符串 dtype——变长的 **StringDType**（`dtype.char == 'T'`）。读完本讲你应当能够：

- 说清 StringDType 的「变长存储」为何不能复用定长 ufunc 的缓冲区套路，从而必须有一套独立的 C/C++ 循环（`stringdtype_ufuncs.cpp`）。
- 看懂注册总入口 `init_stringdtype_ufuncs` 如何把几十条循环与 promoter 挂到 umath 模块的私有 ufunc 上，并能与 u3-l12 的 `init_string_ufuncs` 做对照。
- 把 Python 包装层里的 `if a.dtype.char == "T": ...` 分支，与 C 层对应的 `*_strided_loop` 一一对应起来，画出「Python 哪个分支 → C 哪个文件」的两条调用链。
- 说出 `dtype.c`（dtype 本体与 setitem/getitem）、`casts.cpp`（StringDType 与其它 dtype 的双向类型转换）、`utf8_utils.c`（UTF-8 编解码原语）这三件配套在 StringDType 子系统里的分工。

## 2. 前置知识

本讲假设你已学完 u1-l2（三种字符串 dtype）、u2-l6（`multiply` 的双分支与溢出保护）、u3-l12（定长字符串 C++ 循环注册）。需要 recall 的关键认知：

- **三种 dtype 与 `char=='T'`**：`numpy.strings` 处理三种字符串 dtype，其中 `StringDType` 的 `dtype.char == 'T'`，内部以 **UTF-8 动态存储**，长度可变；`bytes_`（`'S'`）与 `str_`（`'U'`）是定长的。Python 包装层常用 `if a.dtype.char == "T":` 给变长类型走一条单独的快速路径。
- **u2-l6 的 `multiply` 双分支**：定长分支要先用 `str_len` 预算输出宽度、做 `sys.maxsize` 溢出保护、显式开 `out`，再交给 C 层 `_multiply_ufunc`；而 `char=='T'` 分支只有一句 `return a * i`，把宽度预算与溢出检查全部下放给 C 层。本讲就是要解释这句「下放」到底落到了哪里。
- **u3-l12 的三层注册**：一个 ufunc 的每种「输入 dtype 组合 → 实现」是一条 `PyArrayMethod`，注册就是「往已存在的 ufunc 对象上挂一条循环」。StringDType 的注册用的是同一套机制，只是循环函数、resolve_descriptors、promoter 都另写一套。

本讲会反复出现几个概念，先用一句话解释：

- **packed_static_string（打包字符串）**：StringDType 数组里**每个元素**的内存表示。它本身只占 `2 * sizeof(size_t)` 字节（一个长度 + 一个指针/标记），真正的字符串字节在堆上。访问前必须先 `NpyString_load`「解包」拿到 `(size, buf)`，写回时要 `NpyString_pack`「打包」。
- **allocator（分配器）**：每个 StringDType 实例自带一个堆分配器，且分配器有自己的锁。循环里读写堆字符串前必须 `NpyString_acquire_allocators` 抢锁，结束后 `NpyString_release_allocators` 放锁——这是变长循环区别于定长循环最显眼的样板代码。
- **promoter（提升器）**：当 ufunc 收到的输入 dtype 组合**没有直接注册的循环**时，promoter 负责把它们「提升」成一组有循环的 dtype（例如把 `str_` 提升成 `StringDType`），再配合 cast 完成运算。
- **GIL**：Python 全局解释器锁。分配器锁持有期间不能调用需要 GIL 的 Python API，所以循环里报错统一走 `npy_gil_error`（它会在合适时机获取 GIL）。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [numpy/_core/src/umath/stringdtype_ufuncs.cpp](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/stringdtype_ufuncs.cpp) | 本讲主角：StringDType 全部 ufunc 的 `strided_loop`、`resolve_descriptors`、promoter 与注册总入口 `init_stringdtype_ufuncs` 都在这里。 |
| [numpy/_core/src/umath/stringdtype_ufuncs.h](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/stringdtype_ufuncs.h) | 对外只导出一个 C 符号 `init_stringdtype_ufuncs`。 |
| [numpy/_core/src/multiarray/stringdtype/dtype.c](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/multiarray/stringdtype/dtype.c) | StringDType 本体：实例创建 `new_stringdtype_instance`、`setitem`/`getitem`、NA（缺失值）处理都在这里。 |
| [numpy/_core/src/multiarray/stringdtype/casts.cpp](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/multiarray/stringdtype/casts.cpp) | StringDType 与 Unicode/Bool/各整数/datetime/bytes 等的双向类型转换表 `get_casts()`，是 promoter 能工作的前提。 |
| [numpy/_core/src/multiarray/stringdtype/utf8_utils.c](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/multiarray/stringdtype/utf8_utils.c) | UTF-8 编解码原语：按字节判断一个字符占几字节、码点与字节互转等。 |
| [numpy/_core/include/numpy/ndarraytypes.h](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/include/numpy/ndarraytypes.h) | 定义 `PyArray_StringDTypeObject` 结构体与 `npy_packed_static_string`/`npy_static_string`。 |
| [numpy/_core/strings.py](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py) | Python 包装层，以 `multiply`、`center` 为例展示 `char=='T'` 分支如何把工作整体下放给 C 层。 |

> 说明：本讲引用的 C/C++ 源码位于 `numpy/_core/src/umath/` 与 `numpy/_core/src/multiarray/`，不在 `numpy/strings/` 门面包内；永久链接使用仓库根相对路径以保证可直接打开。

## 4. 核心概念与源码讲解

### 4.1 为什么 StringDType 需要一套独立的循环

#### 4.1.1 概念说明

回顾 u3-l12：定长字符串（`bytes_`/`str_`）的循环之所以能高效，是因为**每个元素占用的字节数固定且已知**——`str_` 一个元素恒为 `itemsize` 字节、`str_len` 个字符恒占 `itemsize = 字符数 × 4` 字节。循环函数只需按固定 `stride` 步进、在预分配好的连续缓冲区里直接读写即可，根本不用关心内存分配。

StringDType 彻底打破了这两条假设：

1. **每个元素长度不同**。`np.array(["a", "你好", "x"], dtype=np.dtypes.StringDType())` 里三个元素的字节长度分别是 1、6、1（UTF-8 下一个汉字 3 字节）。数组里的「格子」无法等宽。
2. **真正的字节在堆上**。数组本体每个位置只存一个固定大小的「句柄」`npy_packed_static_string`，字符串内容按需在堆上分配/扩容。

后果是：定长循环那套「Python 层预算一个统一宽度、开一个定长 `out`、循环里直接 `memcpy`」的套路（u2-l5 的「输出 dtype 路径 A」）对 StringDType 完全失效——你**无法在循环开始前知道输出多宽**，因为输出宽度取决于每个元素各自的运算结果。于是 Python 层干脆不预算、不预分配，把「算宽度 + 分配 + 写入」整套打包成一条 C 循环。

#### 4.1.2 核心流程：变长元素在内存里长什么样

```
StringDType 数组（n 个元素，元素等宽只是「句柄」等宽）
┌──────────┬──────────┬──────────┐
│ handle 0 │ handle 1 │ handle 2 │   每个 handle = npy_packed_static_string
└────┬─────┴────┬─────┴────┬─────┘     (2 * sizeof(size_t) 字节)
     │          │          │
     ▼          ▼          ▼
   堆: "a"    堆: "你好"   堆: "x"      真正的 UTF-8 字节，长度各异
```

访问一个元素的标准三步：

1. `NpyString_acquire_allocators(...)` —— 抢分配器锁。
2. `NpyString_load(allocator, packed, &static_string)` —— 把 `packed`（句柄）解包成 `npy_static_string{size, buf}`，并返回是否为 null（NA）。
3. 用 `buf`/`size` 干活；要写回时 `load_new_string`（申请新堆块）或 `NpyString_pack`（打包回句柄）。

#### 4.1.3 源码精读

先看「句柄」与「解包结果」的类型定义。`npy_packed_static_string` 是一个**不透明**结构（只能通过 API 访问），而 `npy_static_string` 是解包后的 `{size, buf}`：

[numpy/_core/include/numpy/ndarraytypes.h:1429-1445](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/include/numpy/ndarraytypes.h#L1429-L1445) —— 注释明确写道「A 'packed' encoded string. The string data must be accessed by first unpacking the string.」紧随其后的 `npy_static_string{ size; const char *buf; }` 才是可读的解包形态。

再看 StringDType 实例本身长什么样——它比普通 dtype 多出一堆字段，最关键的是末尾的 `allocator`：

[numpy/_core/include/numpy/ndarraytypes.h:1448-1469](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/include/numpy/ndarraytypes.h#L1448-L1469) —— `PyArray_StringDTypeObject` 在 `PyArray_Descr_fields base` 之上挂了 `na_object`（缺失值对象）、`coerce`/`has_nan_na`/`has_string_na`（NA 行为开关）、`default_string`（NA 当作普通字符串时的替身）以及最后的 `npy_string_allocator *allocator`。这个 `allocator` 就是循环里要抢锁的对象。

实例创建时，`new_stringdtype_instance` 会顺便建好分配器：

[numpy/_core/src/multiarray/stringdtype/dtype.c:27-45](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/multiarray/stringdtype/dtype.c#L27-L45) —— 用 `NpyString_new_allocator(PyMem_RawMalloc, PyMem_RawFree, PyMem_RawRealloc)` 建一个基于原始堆内存的分配器。注意它用的是 `PyMem_Raw*`（**不需要 GIL**），这正是循环可以在持有分配器锁时安全分配内存的前提。

#### 4.1.4 代码实践

**目标**：亲手验证 StringDType 的「句柄等宽、内容不等宽」。

```python
# 示例代码
import numpy as np

a = np.array(["a", "你好", "x"], dtype=np.dtypes.StringDType())
print(a.dtype, a.dtype.char)        # <T  T
print(a.dtype.itemsize)             # 16（64 位下 2 * sizeof(size_t) = 2*8）
print([type(s) for s in a])         # 三个都是 str，但底层字节数不同
```

1. 观察 `dtype.itemsize`：它是**句柄**的宽度（`2 * sizeof(size_t)`），与三个字符串实际字节数（1/6/1）无关——这印证了「格子等宽、内容不等宽」。
2. 对比 `np.array(["a", "你好", "x"]).dtype.itemsize`（默认 `str_`）：`str_` 的 itemsize 是 `最大字符数 × 4`，所有元素被补齐到同一宽度。

需要观察的现象：StringDType 的 `itemsize` 不随内容变化，`str_` 的 `itemsize` 等于「最长那条」乘 4。预期结果：StringDType 数组的 `itemsize` 恒为 16（待本地验证 64 位平台），而等价的 `str_` 数组 itemsize 为 `2 × 4 = 8`。

#### 4.1.5 小练习与答案

**练习 1**：为什么定长 `str_` 的循环函数里几乎看不到 `malloc`/`free`，而 StringDType 的循环里到处是 `load_new_string` / `NpyString_pack`？

> **答案**：`str_` 数组的存储就是一段等宽的连续缓冲区，元素直接就地读写，无需动态分配；StringDType 每个元素的内容在堆上且长度可变，运算结果（如 `multiply` 后变长）必须申请新堆块再打包回句柄。

**练习 2**：循环函数开头为什么必须调用 `NpyString_acquire_allocators`？

> **答案**：StringDType 的堆内存由实例自带的 `allocator` 管理，分配器有锁；NumPy 可能在多线程（如 `gufunc` 并行）下运行同一条循环，抢锁是为了避免多个线程同时改写同一分配器的内部状态。

---

### 4.2 注册总入口：init_stringdtype_ufuncs

#### 4.2.1 概念说明

StringDType 的全部循环注册集中在一个函数 `init_stringdtype_ufuncs`，它和 u3-l12 的 `init_string_ufuncs` 是**平行的两套**：前者服务 `'T'`，后者服务 `'S'`/`'U'`。二者都遵循 u3-l12 总结的「往已存在 ufunc 上挂循环」原则——umath 模块早已把 `add`、`multiply`、`str_len`、`_center`、`_strip_whitespace`、`_slice` 等 ufunc 对象放进模块字典，`init_stringdtype_ufuncs` 拿到字典后按名字取出 ufunc，再为它们注册 StringDType 专用的循环。

唯一的对外 C 符号就一个：

[numpy/_core/src/umath/stringdtype_ufuncs.h:8-9](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/stringdtype_ufuncs.h#L8-L9) —— 只声明 `init_stringdtype_ufuncs(PyObject* umath)`，其余所有 loop/resolve/promoter 都是 `static`，外部不可见。

#### 4.2.2 核心流程：注册了哪些循环

`init_stringdtype_ufuncs` 内部按函数族逐段注册，可以整理成下表（每行都来自源码里一段 `init_ufunc(...)` 调用）：

| 函数族 | 注册的 ufunc 名 | 循环函数 | 说明 |
|--------|----------------|----------|------|
| 比较（6 件套） | `equal`/`not_equal`/`less`/`less_equal`/`greater_equal`/`greater` | `string_comparison_strided_loop`（共用，靠 static_data 区分） | 输出 bool |
| 缺失判断 | `isnan` | `string_isnan_strided_loop` | 输出 bool |
| 一元分类（9 个） | `isalpha`/`isdecimal`/`isdigit`/`isnumeric`/`isspace`/`isalnum`/`istitle`/`isupper`/`islower` | `string_bool_output_unary_strided_loop`（共用，靠成员函数指针区分） | 输出 bool |
| 长度 | `str_len` | `string_strlen_strided_loop` | 输出 intp |
| 最值 | `minimum`/`maximum` | `minimum_maximum_strided_loop`（共用，靠 invert 区分） | 输出 StringDType |
| 拼接 | `add` | `add_strided_loop` | 输出 StringDType |
| 重复 | `multiply` | `multiply_right_strided_loop`/`multiply_left_strided_loop`（模板，按整数类型） | 输出 StringDType |
| 查找（5 件套） | `find`/`rfind`/`index`/`rindex`/`count` | `string_findlike_strided_loop`（共用，靠函数指针区分） | 输出整数 |
| 前后缀 | `startswith`/`endswith` | `string_startswith_endswith_strided_loop`（共用，靠 FRONT/BACK 区分） | 输出 bool |
| 裁剪空白 | `_lstrip_whitespace`/`_rstrip_whitespace`/`_strip_whitespace` | `string_lrstrip_whitespace_strided_loop`（共用，靠 STRIPTYPE 区分） | 输出 StringDType |
| 裁剪字符 | `_lstrip_chars`/`_rstrip_chars`/`_strip_chars` | `string_lrstrip_chars_strided_loop`（共用） | 输出 StringDType |
| 替换 | `_replace` | `string_replace_strided_loop` | 输出 StringDType |
| 制表 | `_expandtabs` | `string_expandtabs_strided_loop` | 输出 StringDType |
| 对齐 | `_center`/`_ljust`/`_rjust` | `center_ljust_rjust_strided_loop`（共用，靠 JUSTPOSITION 区分） | 输出 StringDType |
| 填零 | `_zfill` | `zfill_strided_loop` | 输出 StringDType |
| 切分 | `_partition`/`_rpartition` | `string_partition_strided_loop`（共用，靠 FRONT/BACK 区分） | 输出 3 个 StringDType |
| 切片 | `_slice` | `slice_strided_loop` | 输出 StringDType |

注意一个贯穿全表的规律：**一类函数共用一个 loop 函数**，差异（左/右/居中、找首/找末、六种比较）全部塞进 `context->method->static_data` 这份静态数据里——这与 u3-l12 里 `_center/_ljust/_rjust` 共用一个 loop 的手法完全一致，只是这里用在了变长循环上。

#### 4.2.3 源码精读

注册中层 helper `init_ufunc` 与 u3-l12 同名函数几乎一模一样，差别仅在 static_data 的 slot：

[numpy/_core/src/umath/stringdtype_ufuncs.cpp:2472-2511](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/stringdtype_ufuncs.cpp#L2472-L2511) —— 它按 `ufunc_name` 取出 ufunc，构造 `PyArrayMethod_Spec`（填 nin/nout/dtypes），再挂三个 slot：`NPY_METH_resolve_descriptors`、`NPY_METH_strided_loop`、以及关键的 `_NPY_METH_static_data`（这份 static_data 就是上表「靠 XX 区分」的来源）。最后调 `PyUFunc_AddLoopFromSpec_int(ufunc, &spec, 1)` 完成挂载，第三个参数 `1` 即 u3-l12 解释的 `priv=1`（携带私有 slot 的必要条件）。

`add_promoter` 则负责挂 promoter：

[numpy/_core/src/umath/stringdtype_ufuncs.cpp:2514-2557](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/stringdtype_ufuncs.cpp#L2514-L2557) —— 把 promoter 函数包成 capsule、连同 dtype 组合元组一起通过 `PyUFunc_AddPromoter` 挂到 ufunc 上。

再看总入口里几个有代表性的注册段。比较族的批量注册：

[numpy/_core/src/umath/stringdtype_ufuncs.cpp:2635-2659](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/stringdtype_ufuncs.cpp#L2635-L2659) —— 注意 `comparison_ufunc_eq_lt_gt_results[6*3]` 这张表：每个比较运算用 3 个 bool（eq/lt/gt 各自的输出）来编码，6 个运算共 18 项；循环运行时从 `static_data` 读出这 3 个 bool 就能复用同一个 `string_comparison_strided_loop` 实现全部 6 种比较。这里还顺带给每个比较挂了 object/unicode promoter，使得 `str_`、object 数组也能参与比较（会被提升到 StringDType）。

`multiply` 因为要支持「字符串 × 整数」和「整数 × 字符串」两个方向、且整数类型多样，注册稍特殊——用一个宏 `INIT_MULTIPLY` 为 `int64`/`uint64` 各注册左右两条循环：

[numpy/_core/src/umath/stringdtype_ufuncs.cpp:2559-2580](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/stringdtype_ufuncs.cpp#L2559-L2580)（宏定义），[numpy/_core/src/umath/stringdtype_ufuncs.cpp:2771-2792](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/stringdtype_ufuncs.cpp#L2771-L2792)（实例化 + 给其余整数 dtype 挂 `string_multiply_promoter`）。

#### 4.2.4 代码实践

**目标**：源码阅读型实践——在 `init_stringdtype_ufuncs` 里清点 `multiply` 到底注册了几条循环。

1. 打开 [numpy/_core/src/umath/stringdtype_ufuncs.cpp:2771](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/stringdtype_ufuncs.cpp#L2771)。
2. 展开 `INIT_MULTIPLY(Int64, int64)` 与 `INIT_MULTIPLY(UInt64, uint64)`：每个宏注册 2 条循环（right、left），共 **4 条**直接注册的循环。
3. 其余整数类型（`int8/16/32`、`uint8/16/32`、Python `int` 等）由两个 promoter（[2781](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/stringdtype_ufuncs.cpp#L2781) 与 [2790](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/stringdtype_ufuncs.cpp#L2790)）经 `string_multiply_promoter` 提升到 `int64`/`uint64` 再命中上面 4 条循环。

需要观察的现象：`multiply` 的循环只对 `int64`/`uint64` 这两个整数 dtype 真正写了 C 循环，其余整数 dtype 靠 promoter + cast 兜底。预期结果：`np.strings.multiply(s, np.int8(3))` 与 `np.strings.multiply(s, np.int64(3))` 走的是同一条 `multiply_right_strided_loop<npy_int64>` 循环（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：`init_stringdtype_ufuncs` 与 `init_string_ufuncs` 注册的循环会冲突吗（它们都往同一个 `multiply` ufunc 上挂循环）？

> **答案**：不会冲突。一条循环由「ufunc + 输入 dtype 组合」唯一确定。`init_string_ufuncs` 挂的是 `(StringDType 之外, ...)` 组合（如 `(Unicode, Unicode, Unicode)`），`init_stringdtype_ufuncs` 挂的是含 `StringDType` 的组合（如 `(StringDType, Int64, StringDType)`）。NumPy 按实际输入 dtype 选循环，两套各管各的。

**练习 2**：为什么 `_center`/`_ljust`/`_rjust` 能共用 `center_ljust_rjust_strided_loop` 一个函数？

> **答案**：三者的差异只有「填充加在哪一侧」，由 `JUSTPOSITION::{CENTER,LEFT,RIGHT}` 枚举表示，作为 `static_data` 挂在各自的 ArrayMethod 上；循环运行时从 `context->method->static_data` 读出位置，调用统一的 `string_pad`。这是「一个 loop + static_data 区分行为」的典型复用。

---

### 4.3 变长循环的写法范式：以 multiply 与 center 为例

#### 4.3.1 概念说明

尽管 4.2 表里有十几个循环函数，它们都套用同一个骨架。掌握两条代表性循环（`multiply`——一元输出、长度成倍增长；`center`——带 fillchar、需算填充宽度），其余皆可举一反三。变长循环相对定长循环多出的三件固定工作：

1. **抢/放分配器锁**：`NpyString_acquire_allocators` / `release`。
2. **逐元素 load → 计算 → load_new_string/pack**：无法批量 memcpy，每个元素各自分配堆块。
3. **NA（缺失值）处理**：StringDType 可带 `na_object`（如 `pd.NA` 或 nan-like），循环里要按 `has_nan_na`/`has_string_na` 决定是传播 NA、用 `default_string` 替身，还是报错。

同时，u2-l6 留下的悬念——`char=='T'` 分支为何敢把溢出检查也下放——答案就在 `multiply_loop_core`：它在 C 层用 `npy_mul_with_overflow_size_t` 做了等价的溢出保护。

#### 4.3.2 核心流程：变长 strided loop 的统一骨架

```
xxx_strided_loop(context, data, dimensions, strides, auxdata):
    从 context->descriptors 拿各 dtype，从 static_data 拿行为开关
    allocators = acquire_allocators(所有 descr)        # 抢锁
    while N--:                                          # 逐元素
        NpyString_load(...)  → s{size,buf}, is_null     # 解包输入
        处理 NA（传播 / 替身 / 报错）
        计算新尺寸 newsize，做溢出检查
        load_new_string(out, newsize) 或就地 malloc      # 申请输出堆块
        把结果字节写入 buf
        NpyString_pack(out, buf, newsize)               # 打包回句柄
        步进 in/out 指针
    release_allocators(allocators)                      # 放锁
    return 0
fail:
    release_allocators(allocators); return -1
```

#### 4.3.3 源码精读

先看最常用的输入加载宏 `LOAD_TWO_INPUT_STRINGS`，它把「解包两个输入 + 错误处理」打包成一行：

[numpy/_core/src/umath/stringdtype_ufuncs.cpp:31-42](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/stringdtype_ufuncs.cpp#L31-L42) —— 解包 `in1`/`in2` 两个句柄得到 `s1`/`s2`，并记录是否为 null；加载失败（返回 -1）则 `goto fail`。`add`、`比较`、`find`、`strip_chars` 等所有双输入循环都靠它起步。

再看 `multiply` 的核心——这正是 u2-l6 里 `return a * i`（`char=='T'` 分支）的真正落点：

[numpy/_core/src/umath/stringdtype_ufuncs.cpp:135-144](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/stringdtype_ufuncs.cpp#L135-L144) —— 读出重复因子 `factor`，用 `npy_mul_with_overflow_size_t(&newsize, cursize, factor)` 同时算出 `newsize = cursize * factor` 并检测溢出；若溢出或 `newsize > PY_SSIZE_T_MAX`，抛 `OverflowError`。这与 Python 定长分支里的 `sys.maxsize` 检查异曲同工，只是下沉到了 C 层。

[numpy/_core/src/umath/stringdtype_ufuncs.cpp:167-171](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/stringdtype_ufuncs.cpp#L167-L171) —— 真正的重复：循环 `factor` 次，每次 `memcpy` 一份原串。注释点明「这里不可能再溢出，因为上面已经检查过 `cursize * factor`」。

而 `center`/`ljust`/`rjust` 三胞胎共用一个循环，展示「带 fillchar + 算填充宽度」的写法：

[numpy/_core/src/umath/stringdtype_ufuncs.cpp:1751-1774](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/stringdtype_ufuncs.cpp#L1751-L1774) —— 先用 `Buffer<ENCODING::UTF8>` 包装输入，`num_codepoints()` 数码点；把 `width` 与原长取大；再用 `npy_mul_sizes_with_overflow` 算填充字节数（`fillchar 字节数 × (width - 原长)`）并加回原串字节，溢出则报错。注意这里**每个元素各自算各自的 newsize**——这正是变长循环能省掉 Python 层「全数组统一宽度」预算的原因。

最后回到 Python 层，确认 `multiply` 与 `center` 的 `char=='T'` 分支确实把活儿整体下放：

[numpy/_core/strings.py:197-210](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L197-L210) —— `multiply`：`char=='T'` 时直接 `return a * i`（注释「delegate to stringdtype loops that also do overflow checking」），**不预算宽度、不查溢出、不开 out**；定长分支才走 `str_len` 预算 + `sys.maxsize` 检查 + 显式 `out`。

[numpy/_core/strings.py:748-757](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L748-L757) —— `center`：`np.result_type(a, fillchar).char == "T"` 时 `return _center(a, width, fillchar)`，**不传 out**；定长分支才构造 `out_dtype = f"{char}{width.max()}"` 并预分配 `out`。两条分支调的是同一个 `_center` ufunc 对象，区别仅在「有没有预算宽度并预分配」——实际落到哪条 C 循环由 dtype 在 ufunc 分发时决定。

#### 4.3.4 代码实践（本讲核心实践）

**目标**：选 `center`，对比它在 StringDType 输入与 `str_` 输入下「Python 走哪个分支、C 落到哪个文件」，写出两条调用链对照表。

**操作步骤**（源码阅读 + 运行验证）：

1. 在 Python 里构造两种输入，观察行为一致但 dtype 不同：

```python
# 示例代码
import numpy as np

u = np.array(['a', 'bb'])                                   # str_ ('U')
t = np.array(['a', 'bb'], dtype=np.dtypes.StringDType())    # StringDType ('T')

print(np.strings.center(u, 4))   # dtype='<U4'
print(np.strings.center(t, 4))   # dtype '<T'
```

2. 阅读源码确定两条链。对照下表填写（答案已给出，请逐格在源码里找到依据）：

| 环节 | `str_` 输入（定长） | StringDType 输入（变长） |
|------|---------------------|--------------------------|
| Python 层分支判定 | `np.result_type(a, fillchar).char == "T"` 为假，走 else（[strings.py:751-757](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L751-L757)） | `.char == "T"` 为真，提前 `return _center(a, width, fillchar)`（[strings.py:748-749](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L748-L749)） |
| 是否预算输出宽度 | 是：`out_dtype = f"{char}{width.max()}"` | 否：宽度由 C 层逐元素计算 |
| 是否预分配 `out` | 是：`np.empty_like(..., dtype=out_dtype)` | 否：C 层逐元素 `load_new_string` |
| 调用的 ufunc 对象 | 同一个 `_center`（[strings.py:23](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L23)） | 同一个 `_center` |
| 命中的 C 循环文件 | `string_ufuncs.cpp`（u3-l12，ASCII/UTF32 ENCODING 模板） | `stringdtype_ufuncs.cpp`（本讲，`center_ljust_rjust_strided_loop`） |
| 命中的 resolve_descriptors | 定长的 `string_*_resolve_descriptors`（强制要求 `out`） | [center_ljust_rjust_resolve_descriptors](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/stringdtype_ufuncs.cpp#L1637-L1676)（`out` 为空时自动 new 一个 StringDType） |

3. 用同一个 `width` 数组同时给两种输入运算，确认数值结果一致（仅 dtype 不同）：

```python
# 示例代码
w = np.array([4, 5])
assert list(np.strings.center(u, w)) == list(np.strings.center(t, w))
```

**需要观察的现象**：两条调用链在 Python 层的「分叉点」是 `result_type(...).char == "T"`；分叉之后，定长链多做「预算宽度 + 预分配」，变长链直接调用且把这两件事下放给 C。

**预期结果**：两种输入得到逐元素相等的字符串内容，仅 `dtype` 不同（`<U5` vs `<T`）。`center` 这一个 ufunc 对象背后挂着**两条不同文件的循环**，由输入 dtype 决定走哪条。

#### 4.3.5 小练习与答案

**练习 1**：为什么 Python 层 `multiply` 的 `char=='T'` 分支敢写 `return a * i`，而不再做 `sys.maxsize` 溢出检查？这个检查丢了吗？

> **答案**：没丢，只是搬到了 C 层 `multiply_loop_core`（[cpp:138-144](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/stringdtype_ufuncs.cpp#L138-L144)）里用 `npy_mul_with_overflow_size_t` 实现。因为变长输出的宽度本就要在 C 层逐元素计算，溢出检查顺手就做了，无需 Python 层重复。

**练习 2**：`center` 的两条链最终调的是同一个 Python 名 `_center`，为何能落到不同 C 文件的循环？

> **答案**：`_center` 是一个 ufunc 对象，它身上挂着多条 ArrayMethod（循环）。NumPy 在调用时根据实际输入的 dtype 组合做分发（dispatching）：含 `StringDType` 的组合命中 `stringdtype_ufuncs.cpp` 的循环，纯 Unicode 的组合命中 `string_ufuncs.cpp` 的循环。Python 名相同不等于底层循环相同。

---

### 4.4 三件配套：dtype.c / casts.cpp / utf8_utils.c

#### 4.4.1 概念说明

`stringdtype_ufuncs.cpp` 能正常工作，离不开另外三个文件的支撑，分工如下：

- **`dtype.c`**：StringDType 作为 dtype 的「本体」实现——如何 new 一个实例、如何把 Python 对象写进数组（`setitem`）、如何从数组读出 Python 对象（`getitem`）、NA 如何判定与传播。循环里调用的 `new_stringdtype_instance`、`stringdtype_common_na_coerce`、`stringdtype_effective_na_descr`、`load_new_string` 都定义于此。
- **`casts.cpp`**：StringDType 与其它 dtype（Unicode、Bool、各整数、datetime/timedelta、bytes、void）之间的双向类型转换。它注册的 cast 是 4.2 里那些 promoter 能成立的**前提**——promoter 把 `str_` 提升成 `StringDType` 之后，真正把字节从 UCS4 转成 UTF-8 的是这里的 cast。
- **`utf8_utils.c`**：UTF-8 编解码原语。StringDType 内部以 UTF-8 存字节，但「字符数」「第 N 个字符」等语义要按码点算，这层转换由它提供。

#### 4.4.2 核心流程

```
用户: np.strings.center(str_array, 4)   # 输入是 str_ ('U')
   │
   │  ufunc 分发：输入 (Unicode, Int64, Unicode)，无直接循环
   ▼
promoter (string_center_ljust_rjust_promoter)
   │  提升成 (StringDType, Int64, StringDType)
   ▼
casts.cpp: UnicodeToString cast          # 把 UCS4 字节逐元素转成 UTF-8 堆串
   │
   ▼
stringdtype_ufuncs.cpp: center_ljust_rjust_strided_loop
   │  循环内用 Buffer<ENCODING::UTF8> + utf8_utils 数码点
   ▼
输出 StringDType 数组
```

#### 4.4.3 源码精读

**dtype.c** 的 `setitem`/`getitem` 揭示了「Python 对象 ↔ packed_static_string」的边界。`setitem` 把一个 Python 对象写进数组位置：

[numpy/_core/src/multiarray/stringdtype/dtype.c:378-431](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/multiarray/stringdtype/dtype.c#L378-L431) —— 先用 `na_eq_cmp` 判断该对象是否等于 `na_object`，是则 `NpyString_pack_null`（写一个 null 句柄）；否则 `as_pystring` + `PyUnicode_AsUTF8AndSize` 取 UTF-8 字节，再 `NpyString_pack` 打包。注意它严格遵循「先比较、后抢锁、抢锁期间不碰 GIL」的纪律。

`getitem` 反过来，把句柄读成 Python 对象：

[numpy/_core/src/multiarray/stringdtype/dtype.c:433-467](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/multiarray/stringdtype/dtype.c#L433-L467) —— `NpyString_load` 解包；若 is_null 且带 NA 则返回 `na_object`，否则用 `PyUnicode_FromStringAndSize(sdata.buf, sdata.size)` 把 UTF-8 字节重建成 Python `str`。

**casts.cpp** 用一张大表登记所有转换。入口 `get_casts()` 返回一个 `PyArrayMethod_Spec *` 数组：

[numpy/_core/src/multiarray/stringdtype/casts.cpp:2150](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/multiarray/stringdtype/casts.cpp#L2150) —— 注释 `int num_casts = 43;` 说明默认注册 43 条 cast（再按平台整数宽度条件加若干）。其中 [numpy/_core/src/multiarray/stringdtype/casts.cpp:2165-2174](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/multiarray/stringdtype/casts.cpp#L2165-L2174) 是 `UnicodeToStringCastSpec`（`Unicode → StringDType`，`NPY_SAME_KIND_CASTING`）——正是上面流程图里把 `str_` 转 StringDType 的那条。

**utf8_utils.c** 提供编解码原语。最常用的是「看首字节判字符长度」的查表函数（定义在头文件里、被循环内联调用）：

[numpy/_core/src/multiarray/stringdtype/utf8_utils.h:11-20](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/multiarray/stringdtype/utf8_utils.h#L11-L20) —— `num_bytes_for_utf8_character` 用一张 32 项查找表，按首字节高 5 位直接给出该字符占 1/2/3/4 字节。`center_ljust_rjust_strided_loop` 里算填充宽度时就靠它确定 `fillchar` 的字节数。

而把一个 UTF-8 字符解码成 UCS4 码点的完整实现：

[numpy/_core/src/multiarray/stringdtype/utf8_utils.c:14-39](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/multiarray/stringdtype/utf8_utils.c#L14-L39) —— `utf8_char_to_ucs4_code` 按首字节落进四个区间，分别拼装出 1/2/3/4 字节序列对应的码点，并返回消耗的字节数。注意它「不做校验，假定输入是合法 UTF-8」——因为写入时已保证合法，循环里追求极致性能。

#### 4.4.4 代码实践

**目标**：观察 promoter + cast 在 `add` 混合 `str_` 与 StringDType 输入时的作用。

```python
# 示例代码
import numpy as np

u = np.array(['x'])                                  # str_
t = np.array(['y'], dtype=np.dtypes.StringDType())  # 'T'

r = u + t                  # 等价于 np.strings.add(u, t)
print(r, r.dtype)          # <T  —— 提升到 StringDType
```

1. 运行后确认结果 dtype 是 `<T`：说明混合输入被 [all_strings_promoter](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/stringdtype_ufuncs.cpp#L1024-L1053) 提升到 StringDType（只要任一操作数是 StringDType 就用 StringDType）。
2. 追源码：`add` 在 `init_stringdtype_ufuncs` 里只注册了 `(StringDType, StringDType, StringDType)` 循环（[cpp:2732](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/stringdtype_ufuncs.cpp#L2732)），但额外挂了 `(Unicode, StringDType, StringDType)` 等 promoter（[cpp:2749-2758](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/stringdtype_ufuncs.cpp#L2749-L2758)）。promoter 把 `str_` 操作数提升后，由 `casts.cpp` 的 `UnicodeToStringCastSpec` 真正转码，再进 `add_strided_loop`。

**需要观察的现象**：混合 `str_` 与 StringDType 做运算，结果总是 StringDType，不会回退到 `str_`。

**预期结果**：`r` 为 `array("xy", dtype=<T)`（待本地验证）。这说明 promoter 把「无直接循环的组合」路由到了「有循环的组合 + cast」。

#### 4.4.5 小练习与答案

**练习 1**：`setitem` 里为什么先调用 `na_eq_cmp` 比较缺失值，再去抢分配器锁？顺序能反过来吗？

> **答案**：不能。`na_eq_cmp` 会调用 Python 的比较协议（需要 GIL），而分配器锁持有期间不应调用需要 GIL 的 Python API（注释 [dtype.c:385-387](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/multiarray/stringdtype/dtype.c#L385-L387) 明确说明）。所以必须先在锁外完成比较，拿到结果后再抢锁打包。

**练习 2**：如果没有 `casts.cpp` 里的 `UnicodeToStringCastSpec`，`np.strings.center(str_array, 4)` 还能跑吗？

> **答案**：不能（或会落到另一条很不一样的路径）。`center` 在 StringDType 上只注册了含 `StringDType` 的循环；`str_` 输入要命中它，必须先被 promoter 提升成 `StringDType`，而「把 `str_` 的 UCS4 字节实际转成 UTF-8 堆串」正是 cast 干的活。没有 cast，promoter 提升后无处真正转码。

---

## 5. 综合实践

**任务**：把 4.3.4 的对照表扩展成一份「端到端调用链图」，并把 `multiply` 也补上，最终产出一张覆盖两个函数、两种输入的完整对照表。

步骤：

1. 选 `multiply` 与 `center` 两个函数，对每个函数分别用 `str_` 输入与 StringDType 输入各跑一遍，记录输出 `dtype` 与数值结果。

```python
# 示例代码
import numpy as np

funcs = {
    'multiply': (lambda a: np.strings.multiply(a, 3)),
    'center':   (lambda a: np.strings.center(a, 5)),
}
for name, f in funcs.items():
    u = np.array(['a', 'bb', 'ccc'])
    t = np.asarray(u, dtype=np.dtypes.StringDType())
    ru, rt = f(u), f(t)
    print(name, 'U ->', ru.dtype, list(ru.astype(object)))
    print(name, 'T ->', rt.dtype, list(rt.astype(object)))
    assert list(ru.astype(object)) == list(rt.astype(object))  # 两种输入数值相等
```

2. 对每个函数，在源码里定位以下四点并填入表格：
   - Python 层分叉判定（`char=='T'` 那一行的文件:行号）；
   - 定长分支是否预算宽度 / 预分配 `out`；
   - 命中的 C 循环文件（`string_ufuncs.cpp` vs `stringdtype_ufuncs.cpp`）与循环函数名；
   - 命中的 `resolve_descriptors`（注意 `multiply` 在两套里都要求/构造 `out`，`center` 同理）。

3. **延伸思考**（不必写代码）：若要给 StringDType 新增一个目前只有定长版本的字符串 ufunc（假设叫 `_foo`），按照本讲的范式，你需要在 `stringdtype_ufuncs.cpp` 里新增哪几样东西？

   参考答案要点：① 一个 `foo_strided_loop`（套用 4.3.2 骨架）；② 一个 `foo_resolve_descriptors`（决定输出 dtype，通常 `out` 为空时 `new_stringdtype_instance`）；③ 在 `init_stringdtype_ufuncs` 里调 `init_ufunc(umath, "_foo", ...)` 注册；④ 若要支持 `str_`/object 混合输入，再加 promoter（必要时配合 `casts.cpp` 已有的 cast）。

## 6. 本讲小结

- StringDType（`'T'`）的元素是「定宽句柄 + 堆上变长 UTF-8 字节」，输出宽度无法在循环前预知，因此定长 ufunc 的「预算宽度 + 预分配 `out`」套路失效，必须有一套独立的变长循环（`stringdtype_ufuncs.cpp`）。
- 变长循环的统一骨架是：抢分配器锁 → 逐元素 `load`/计算/`pack` → 放锁；并统一处理 NA（`has_nan_na`/`has_string_na`）。`npy_packed_static_string`/`npy_static_string` 与实例自带的 `allocator` 是这套机制的三块基石。
- `init_stringdtype_ufuncs` 与 u3-l12 的 `init_string_ufuncs` 平行，往同一批 ufunc 上挂含 `StringDType` 的循环；大量「一族函数共用一个 loop + static_data 区分行为」的复用手法在此重演。
- Python 层 `if a.dtype.char == "T":` 分支把「宽度预算 + 溢出检查 + 开 `out`」整体下放给 C 层（如 `multiply` 的 `return a * i` 直接命中 `multiply_loop_core` 的 C 层溢出检查）；定长分支才在 Python 层做这些。
- 同一个 ufunc 对象（如 `_center`）背后可挂多条循环，由输入 dtype 在 ufunc 分发时决定落 `string_ufuncs.cpp` 还是 `stringdtype_ufuncs.cpp`。
- 三件配套各司其职：`dtype.c` 管 dtype 本体与 setitem/getitem/NA；`casts.cpp` 提供 StringDType 与其它 dtype 的双向转换，是 promoter 能工作的前提；`utf8_utils.c` 提供 UTF-8 编解码原语，支撑「按码点计数/切片」的语义。

## 7. 下一步学习建议

- 阅读本讲配套的 `dtype.c` 全文，重点看 `stringdtype_common_na_coerce` 与 `stringdtype_effective_na_descr`——它们解释了 4.3 里所有循环开头那段「NA 三分支」的判定来源。
- 结合 u3-l13（`string_buffer.h` / `string_fastsearch.h`），对照本讲循环里大量出现的 `Buffer<ENCODING::UTF8>`：理解变长循环如何复用定长循环同款的字符处理原语，只是编码特化成了 UTF8。
- 继续下一篇 u3-l15（`_vec_string` 的 C 实现），看那些**尚未**演化成 ufunc 的函数（`upper`/`lower`/`mod` 等）为何走完全不同的「逐元素调 Python 方法」路径，与本讲的 ufunc 路径形成对照。
- 若想动手，可按「综合实践」第 3 步的清单，在本地分支上模拟「为 StringDType 新增一个 ufunc 循环」的全流程（只写注册与骨架、编译观察），以巩固本讲的注册范式。
