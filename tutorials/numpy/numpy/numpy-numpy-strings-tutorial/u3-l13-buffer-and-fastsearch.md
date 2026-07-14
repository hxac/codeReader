# string_buffer 与 string_fastsearch：字符处理原语

> 本讲属于「专家层」第 2 篇（u3-l13），承接 u3-l12（C++ ufunc 循环注册）。上一篇我们看清了「循环如何挂到 ufunc 上」；本讲下钻一层，看清循环体内部反复调用的两个字符级原语头文件。

## 1. 本讲目标

学完本讲，你应当能够：

- 说出 `string_buffer.h` 中 `ENCODING` 枚举三个取值的含义，并能解释**定长 ufunc 为什么只用其中两个**（`ASCII` 与 `UTF32`）。
- 读懂 `getchar<enc>` 的三个模板特化，解释 ASCII（1 字节）、UTF32（4 字节）、UTF8（变长 1–4 字节）读取一个字符时字节数的差异来源。
- 理解 `IMPLEMENTED_UNARY_FUNCTIONS` 枚举 + `codepoint_is*` 一组分类函数如何被 `unary_loop` 统一调度，支撑 `isalpha` 等判断。
- 看懂 `string_fastsearch.h` 如何用「Bloom 过滤 + Boyer-Moore-Horspool + Two-Way」三档策略，为 `find`/`index`/`count` 等 ufunc 提供高性能子串搜索。
- 把本讲原语与上一篇的循环注册、更早 u2-l7 的查找函数串成一条完整调用链。

## 2. 前置知识

本讲是纯 C++ 头文件精读，但概念上并不难。你只需要先建立这几个直觉：

- **码点（codepoint）与字节（byte）不是一回事。** 一个字符在内存里可能占 1 个字节（ASCII），也可能占 4 个字节（UTF32），还可能占 1–4 个字节（UTF8）。NumPy 的字符串运算大多按「码点」计数，而内存按「字节」搬运——这两者的换算正是本讲的中心问题。
- **C++ 模板特化（template specialization）。** 一个主模板 `getchar<enc>` 只是个声明；为 `ASCII`/`UTF32`/`UTF8` 分别写出 `template<>` 版本，编译器就会在编译期为每种编码生成一份独立、可内联的高效代码。本讲会反复看到「一份逻辑、三套特化」的写法。
- **子串搜索的朴素复杂度是 \(O(n\cdot m)\)。** 把长度为 \(m\) 的「针（needle）」在长度为 \(n\) 的「草垛（haystack）」里逐位对齐比对，最坏要做约 \(n\cdot m\) 次比较。`fastsearch` 的全部功夫就是用各种「跳过」技巧把这个常数和最坏情况压下去。
- **与 Python 层的对应（来自 u2-l7）。** `np.strings.find`/`index`/`count` 的 Python 包装极薄，真正干活的 C 函数就是本讲的 `string_find`/`string_index`/`string_count`，它们最终都落到 `fastsearch`。

> 名词速查：**haystack** = 被搜索的主串；**needle** = 要找的子串；**Bloom 过滤器（bloom filter）** = 用一个整数的若干比特位近似记录「哪些字符出现过」，能用 \(O(1)\) 快速排除「这个字符根本不在 needle 里」。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲用到什么 |
|------|------|--------------|
| `numpy/_core/src/umath/string_buffer.h` | 字符级原语总汇：编码抽象、码点读取、字符分类、缓冲区游标 `Buffer`、以及 `string_find`/`string_count`/`tailmatch` 等高层字符串算法 | `ENCODING`、`getchar`、`codepoint_is*`、`IMPLEMENTED_UNARY_FUNCTIONS`、`Buffer`、`unary_loop`、`string_find` |
| `numpy/_core/src/umath/string_fastsearch.h` | 从 CPython 移植的子串搜索引擎 | `fastsearch` 调度器、`default_find`、`two_way`、Bloom 宏、`CheckedIndexer` |
| `numpy/_core/src/umath/string_ufuncs.cpp` | 定长（bytes_/str_）ufunc 循环注册（u3-l12 主角） | 证明它只用 `ASCII`/`UTF32` 两种编码 |
| `numpy/_core/src/umath/stringdtype_ufuncs.cpp` | 变长 StringDType 专用循环 | 证明 `UTF8` 只在这里出现 |
| `numpy/_core/src/multiarray/stringdtype/utf8_utils.h` | UTF8 编解码工具 | `getchar<UTF8>` 依赖的 `utf8_char_to_ucs4_code`、`num_bytes_for_utf8_character` |

## 4. 核心概念与源码讲解

### 4.1 ENCODING 枚举：三种字符串编码的统一标签

#### 4.1.1 概念说明

NumPy 的字符串有三种 dtype（详见 u1-l2）：

| dtype | `dtype.char` | 内存编码 | 本讲标签 |
|-------|--------------|----------|----------|
| `bytes_` | `'S'` | 每字符 1 字节（ASCII/Latin1） | `ENCODING::ASCII` |
| `str_` | `'U'` | 每字符固定 4 字节（UCS4） | `ENCODING::UTF32` |
| `StringDType` | `'T'` | UTF-8，每字符 1–4 字节（变长） | `ENCODING::UTF8` |

`string_buffer.h` 一上来就定义一个 `enum class ENCODING`，把这三者收拢成同一个编译期标签。**它的全部意义在于：让同一套字符串算法写一次、按编码编译三份**，从而既不牺牲性能（每种编码走自己的最优路径），又不重复代码。

#### 4.1.2 核心流程

- 算法函数写成模板 `template <ENCODING enc> ... string_find(Buffer<enc> ...) { ... }`。
- 注册循环时，按 dtype 决定用 `string_find<ENCODING::ASCII>` 还是 `string_find<ENCODING::UTF32>`（见 u3-l12 的 `init_ufunc`）。
- `Buffer<enc>` 这个游标类内部根据 `enc` 把「前进一个字符」「读取一个码点」翻译成正确的字节数——算法主体只写「字符级」逻辑，完全不用关心一个字符占几个字节。

一句话：`ENCODING` 是「编译期分发开关」，把编码差异封在底层、让上层算法保持统一。

#### 4.1.3 源码精读

枚举定义本身极简：

[`numpy/_core/src/umath/string_buffer.h:24-26`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_buffer.h#L24-L26) —— 定义 `ASCII`、`UTF32`、`UTF8` 三个标签。

真正说明「定长 ufunc 只用其中两个」的证据在注册端。`string_ufuncs.cpp` 全文搜不到一次 `ENCODING::UTF8`——它只为 `ASCII` 与 `UTF32` 注册循环。例如查找族：

[`numpy/_core/src/umath/string_ufuncs.cpp:1605-1618`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_ufuncs.cpp#L1605-L1618) —— 把 `string_find<enc>` 等 5 个函数分别收集成 `findlike_ascii_functions[]` 和 `findlike_utf32_functions[]` 两个数组，唯独没有 utf8。

而 `UTF8` 出现在另一个文件：

[`numpy/_core/src/umath/stringdtype_ufuncs.cpp:912-913`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/stringdtype_ufuncs.cpp#L912-L913) —— 在变长 StringDType 的查找循环里用 `Buffer<ENCODING::UTF8>`。这说明 **UTF8 这条线是 StringDType 专属**，定长路径完全不走它。

#### 4.1.4 代码实践

1. **目标**：亲手验证「定长 ufunc 只用 ASCII/UTF32」。
2. **步骤**：用 Grep 在 `string_ufuncs.cpp` 里搜 `ENCODING::UTF8`，再在 `stringdtype_ufuncs.cpp` 里搜同一个串。
3. **观察**：前者 0 次命中，后者大量命中。
4. **预期结果**：定长与变长各管两种/一种编码，互不交叉。
5. 说明：本实践为源码阅读型，结论可在本仓库直接复现（「待本地验证」的是 Grep 工具是否就绪，结论本身由源码确定）。

#### 4.1.5 小练习与答案

- **练习**：为什么 `str_`（UCS4）的枚举名叫 `UTF32` 而不是 `UCS4`？
- **答案**：UCS4 在内存里就是「每个码点固定占 32 位（4 字节）」，与「UTF-32 定长编码」的存储布局完全一致，所以从「按字节读取」的角度二者等价，取 `UTF32` 这个强调「4 字节定长」的名字更贴合本文件的关注点。
- **练习**：若将来新增一种「每字符 2 字节」的定长 dtype，本文件要改哪些地方？
- **答案**：在 `ENCODING` 里加一个枚举值（如 `UTF16`），为 `getchar`、所有 `codepoint_is*`、`Buffer` 的每个 `switch(enc)` 分支补上对应特化/分支，并在注册端为新 dtype 实例化 `string_*<ENCODING::UTF16>` 循环。

---

### 4.2 getchar 模板：按编码读取一个码点

#### 4.2.1 概念说明

`getchar` 是整个头文件最底层的原语：**给定一个字节指针 `buf`，读出「当前字符」的码点，并通过出参 `*bytes` 告知调用方「这个字符占了几个字节」**。它是 `Buffer::operator*()`（解引用游标）和前进游标的基础。

它的设计是「主模板声明 + 三个全特化」，每种编码各自给出最高效的实现。

#### 4.2.2 核心流程

读取一个字符的字节数差异：

- **ASCII**：恒为 1 字节。直接把首个字节转成码点。
- **UTF32**：恒为 4 字节。把首 4 字节当成一个 `npy_ucs4`（32 位整数）读出。
- **UTF8**：变长 1–4 字节。由首字节的高位决定长度——前导字节用查表（LUT）算出长度，再连同后续字节解码出一个码点。

UTF8 长度判定的数学依据是 UTF-8 编码规范：首字节以 `0xxxxxxx` 开头为 1 字节、`110xxxxx` 为 2 字节、`1110xxxx` 为 3 字节、`11110xxx` 为 4 字节。NumPy 用一个 32 项查找表按 `c[0] >> 3`（首字节高 5 位）索引得出长度。

#### 4.2.3 源码精读

主模板只是个声明：

[`numpy/_core/src/umath/string_buffer.h:41-43`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_buffer.h#L41-L43) —— 声明 `getchar<enc>`，统一返回 `npy_ucs4`、写出参 `*bytes`。

ASCII 特化——最简，1 字节：

[`numpy/_core/src/umath/string_buffer.h:46-52`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_buffer.h#L46-L52) —— `*bytes = 1`，把首字节当码点返回。

UTF32 特化——固定 4 字节：

[`numpy/_core/src/umath/string_buffer.h:55-61`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_buffer.h#L55-L61) —— `*bytes = 4`，按 `npy_ucs4`（32 位）直接解引用。

UTF8 特化——变长，委托给 utf8 工具：

[`numpy/_core/src/umath/string_buffer.h:63-70`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_buffer.h#L63-L70) —— 调 `utf8_char_to_ucs4_code`，返回值既是码点写入、字节数也由该函数填回 `*bytes`。

字节数到底怎么算？看 `getchar<UTF8>` 依赖的工具：

[`numpy/_core/src/multiarray/stringdtype/utf8_utils.h:11-20`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/multiarray/stringdtype/utf8_utils.h#L11-L20) —— `num_bytes_for_utf8_character` 用一张 32 项 `LENGTHS_LUT`，按 `c[0] >> 3` 取长度（1/2/3/4，0 表示非法首字节）。这就是「UTF8 一个字符占几个字节」的全部答案。

#### 4.2.4 代码实践

1. **目标**：直观感受三种编码下「一个字符的字节数」差异（用 Python 侧观察，因为这些是 C 内部函数，不便直接调用）。
2. **步骤**：

   ```python
   import numpy as np
   # 同样的 'a' 字符，看三种 dtype 的 itemsize
   for dt in [np.bytes_, np.str_, np.dtypes.StringDType()]:
       a = np.array(['a'], dtype=dt)
       print(dt, 'itemsize =', a.itemsize)  # 字节宽度
   # 一个中文 '中' 在不同编码下：
   for dt in [np.str_, np.dtypes.StringDType()]:
       a = np.array(['中'], dtype=dt)
       print(dt, 'itemsize =', a.itemsize, 'len =', np.strings.str_len(a))
   ```
3. **观察**：`bytes_` 的 'a' 是 1 字节；`str_` 的 'a' 是 4 字节（UCS4 定长）；`str_` 的 '中' 仍是 4 字节（定长！与字符无关）；StringDType 的 '中' 字节数 > 'a'（UTF8 变长）。
4. **预期结果**：`str_` 的 itemsize 永远是「字符数 × 4」；StringDType 的字节数随字符的实际 UTF8 长度变化。这与 `getchar` 三特化的 `*bytes` 取值一一对应。
5. 说明：本实践「待本地验证」的是具体 itemsize 数值，结论方向由源码确定。

#### 4.2.5 小练习与答案

- **练习**：为什么 `getchar<ASCII>` 和 `getchar<UTF32>` 都是「常数字节」，而 `getchar<UTF8>` 必须「读首字节再决定」？
- **答案**：ASCII/UTF32 是定长编码，字符宽度与内容无关；UTF8 是变长编码，宽度编码在首字节的高位里，必须先读首字节、查表才知道该连读几个后续字节。
- **练习**：`getchar<UTF8>` 为什么不直接用 `num_bytes_for_utf8_character`，而是调 `utf8_char_to_ucs4_code`？
- **答案**：`getchar` 的职责是「读出一个码点」，不仅要算长度，还要把 1–4 个字节**解码**成单个码点；`utf8_char_to_ucs4_code` 同时完成「算长度 + 解码」两件事并填回 `*bytes`，正好满足需求。

---

### 4.3 codepoint_is* 与 IMPLEMENTED_UNARY_FUNCTIONS：字符分类原语

#### 4.3.1 概念说明

`isalpha`、`isdigit`、`isspace`……这些「判断一个码点属于哪一类」的函数，在 Python 里是 `str` 的方法，在 NumPy 里是逐元素的 ufunc。C 层实现它们需要一个底层原语：**给定一个码点，返回它是否属于某类**。这就是 `codepoint_is*` 家族。

因为「ASCII 字符集」和「Unicode 全集」的判断规则不同（ASCII 只认 128 个字符，Unicode 要查大表），这些分类函数同样按 `ENCODING` 做模板特化。

#### 4.3.2 核心流程

- `codepoint_isalpha<ASCII>` 调 NumPy 自己的 `NumPyOS_ascii_isalpha`（轻量，只判字节范围）。
- `codepoint_isalpha<UTF32>` / `<UTF8>` 都调 CPython 的 `Py_UNICODE_ISALPHA`（查 Unicode 属性表，支持全语种）。
- 上一层 `Buffer<enc>::isalpha()` 把「逐字符判断」拼成「整个字符串判断」：只要有一个字符不满足，整个串就返回 `false`。

`IMPLEMENTED_UNARY_FUNCTIONS` 枚举（`ISALPHA`/`ISDIGIT`/`...`）是一个「函数选择标签」，配合 `call_buffer_member_function` 这个分发器，让 `unary_loop` 这一个模板能同时服务多个 is* 判断——避免为每个 is* 各写一遍「遍历 + 短路」逻辑。

#### 4.3.3 源码精读

枚举列出所有已实现的一元判断：

[`numpy/_core/src/umath/string_buffer.h:28-39`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_buffer.h#L28-L39) —— `IMPLEMENTED_UNARY_FUNCTIONS`，成员与 `isalpha`/`isdigit` 等一一对应，外加 `STR_LEN`。

`codepoint_isalpha` 的三个特化，正好体现「ASCII 走轻量、Unicode 走 Py 宏」的分野：

[`numpy/_core/src/umath/string_buffer.h:76-95`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_buffer.h#L76-L95) —— ASCII 用 `NumPyOS_ascii_isalpha`；UTF32/UTF8 用 `Py_UNICODE_ISALPHA`。

> 注意一个细节：`codepoint_isnumeric` 与 `codepoint_isdecimal` **没有按 enc 特化**（见 [string_buffer.h:247-257](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_buffer.h#L247-L257)），它们是无模板的普通函数，因为这两个判断只对 Unicode 有意义、没有 ASCII 简化版。

分发器把「枚举标签」翻译成「具体调用哪个 codepoint 函数」：

[`numpy/_core/src/umath/string_buffer.h:695-713`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_buffer.h#L695-L713) —— `call_buffer_member_function` 用 `switch(f)` 选 `codepoint_isalpha<enc>`/`isdigit`/…

`unary_loop` 是「遍历 + 短路」的通用骨架，所有简单 is* 共用它：

[`numpy/_core/src/umath/string_buffer.h:489-513`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_buffer.h#L489-L513) —— 取长度、逐字符 `cbmf(tmp)` 判断，任一为假立即返回 `false`。

`Buffer::isalpha()` 一行转交：

[`numpy/_core/src/umath/string_buffer.h:515-519`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_buffer.h#L515-L519) —— `return unary_loop<IMPLEMENTED_UNARY_FUNCTIONS::ISALPHA>();`

#### 4.3.4 代码实践

1. **目标**：观察 ASCII 与 Unicode 编码下 `isalpha` 结果不同（对应 `NumPyOS_ascii_isalpha` 与 `Py_UNICODE_ISALPHA` 的差异）。
2. **步骤**：

   ```python
   import numpy as np
   # '中' 是 Unicode 字母，但不是 ASCII 字母
   s_u = np.array(['中'], dtype=np.str_)           # UTF32 → Py_UNICODE_ISALPHA
   s_b = np.array([b'a'], dtype=np.bytes_)         # ASCII → NumPyOS_ascii_isalpha
   print('str_ 中文 isalpha:', np.strings.isalpha(s_u))   # True（Unicode 认）
   print('bytes_ ascii isalpha:', np.strings.isalpha(s_b)) # True
   ```
3. **观察**：`str_` 下的中文被判为字母，因为走的是 Unicode 属性表；`bytes_` 走 ASCII 表，只认 128 个字符。
4. **预期结果**：`'中'` 在 `str_`/StringDType 下 `isalpha=True`。
5. 说明：「待本地验证」具体布尔输出。

#### 4.3.5 小练习与答案

- **练习**：`isalpha`/`isdigit`/`isspace` 三个判断为什么能共用同一段 `unary_loop`？
- **答案**：它们都是「逐字符判断、任一为假则整串为假」的同一控制流；差异只在「单字符怎么判」，而这部分被 `call_buffer_member_function` + 枚举标签抽走了，所以遍历骨架只需一份。
- **练习**：为什么 `islower`/`isupper`/`istitle` **没有**用 `unary_loop`，而是各自手写了循环？
- **答案**：这三个判断不是简单的「每个字符都满足某条件」，还涉及「串里是否存在大小写字符（cased）」等额外状态，逻辑更复杂（见 [string_buffer.h:557-632](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_buffer.h#L557-L632)），无法套用 `unary_loop` 的短路骨架。

---

### 4.4 Buffer 模板：把编码差异封装成统一游标

#### 4.4.1 概念说明

有了 `getchar` 和 `codepoint_is*` 这些「单字符」原语后，还需要一个「字符串」抽象来承载逐字符遍历、比较、拷贝。`Buffer<enc>` 就是这个游标类。它的核心价值：**让上层算法用统一的「`buf++` 前进一个字符、`*buf` 读一个码点、`buf1 - buf2` 算字符距离」语法写字符串逻辑，而把「一个字符到底几个字节」全部藏进模板里。**

#### 4.4.2 核心流程

`Buffer` 只有两个字段：`buf`（当前字节指针）和 `after`（缓冲区尾后指针）。它重载了一批运算符：

- `operator*()`：解引用，读当前码点（内部调 `getchar<enc>`）。
- `operator++/--`、`operator+=/-=`：前进/后退「若干字符」——ASCII 加字节数、UTF32 加「字符数×4」、UTF8 按实际字符宽度累加。
- `num_codepoints()`：算字符数（ASCII/UTF32 从尾部向前跳过 `\0` 填充；UTF8 委托 `num_codepoints_for_utf8_bytes`）。
- `buffer_memcpy/memset/memcmp`：按编码正确的单位搬运/填充/比较字节。

关键设计是：**对 ASCII，1 字符 = 1 字节，运算最简；对 UTF32，1 字符 = 4 字节，所有指针运算乘 4；对 UTF8，1 字符 = 变长字节，前进时要逐字符查表。** 这套差异通过 `switch(enc)` 体现在每个运算符里。

#### 4.4.3 源码精读

解引用运算符——`getchar` 的唯一上层调用点：

[`numpy/_core/src/umath/string_buffer.h:380-385`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_buffer.h#L380-L385) —— `operator*()` 调 `getchar<enc>`，丢掉 `bytes` 出参（解引用只取码点）。

前进运算符，三种编码三种行为：

[`numpy/_core/src/umath/string_buffer.h:315-332`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_buffer.h#L315-L332) —— `operator+=`：ASCII 直接 `buf += rhs`；UTF32 乘 4；UTF8 逐字符 `num_bytes_for_utf8_character` 累加。

字符计数（注意 ASCII/UTF32 要剥掉定长缓冲尾部的 `\0` 填充）：

[`numpy/_core/src/umath/string_buffer.h:279-301`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_buffer.h#L279-L301) —— `num_codepoints`：从尾部回扫跳过 `\0`（定长 dtype 用零填充对齐宽度），UTF8 则直接数码点。

正是有了这套统一抽象，`string_find` 等算法才能写出与编码无关的「字符级」主体，只在需要原始字节指针时（如交给 `fastsearch`）才取 `.buf`。

#### 4.4.4 代码实践

1. **目标**：理解 `num_codepoints` 与 `str_len` ufunc 的关系，看清「字符数」是怎么数出来的。
2. **步骤**：阅读 `string_str_len_loop`（在 string_ufuncs.cpp）如何对每个元素构造 `Buffer` 并调用 `num_codepoints()`；再用 Python 验证尾部零填充不影响长度。
3. **观察**：`np.array(['ab'], dtype='U5')` 的 itemsize 是 20（5 字符 × 4 字节），但 `str_len` 返回 2。
4. **预期结果**：`str_len` 数的是真实码点数，而非缓冲宽度——这正是 `num_codepoints` 剥零填充的意义。
5. 说明：源码阅读型实践，「待本地验证」具体数值。

#### 4.4.5 小练习与答案

- **练习**：`Buffer::operator+=` 对 UTF8 为什么不能像 UTF32 那样「指针 += rhs × 常数」？
- **答案**：UTF8 每个字符宽度不同，必须逐字符用 `num_bytes_for_utf8_character` 查出该字符的字节数再累加，没有统一常数可乘。
- **练习**：`num_codepoints` 对定长 dtype 为什么要「从尾部回扫 `\0`」？
- **答案**：定长 dtype 用 `\0` 把短串填充到固定宽度（如 `'ab'` 存成 `'ab\0\0\0'`）；这些填充不是真实字符，必须从尾部剥掉才算出正确的字符数。

---

### 4.5 string_fastsearch：加速 find/index/count 的子串搜索

#### 4.5.1 概念说明

`string_buffer.h` 里的 `string_find`/`string_rfind`/`string_count` 是查找族的高层入口，但真正干「在草垛里找针」苦力的是 `string_fastsearch.h` 里的 `fastsearch`。这个文件整段移植自 CPython 的 `stringlib`，是业界成熟的子串搜索引擎。

它的核心思想是「**跳过不可能匹配的位置**」，分三档策略自适应：

1. **单字符 needle**：直接调 `find_char`/`rfind_char`/`countchar`（长串还会用 `memchr` 加速）。
2. **短串/中等规模**：`default_find`，用 Bloom 过滤器做「坏字符」快速跳过——\(O(n)\) 均摊、实现简单。
3. **大串且 needle 占比不高**：`two_way`（Crochemore-Perrin 两路算法），保证最坏 \(O(n)\)、最优 \(O(n/m)\)，但预处理有 \(O(m)\) 启动成本，所以只在规模够大时才值得。

#### 4.5.2 核心流程

`fastsearch` 的调度逻辑（节选自源码注释与代码）：

```
若 n < m（草垛比针短）              → 直接返回 -1
若 m <= 1（空或单字符）              → find_char / rfind_char / countchar
若 反向搜索 (FAST_RSEARCH)          → default_rfind
否则（正向 find/count）:
   若 n < 2500 或 (m<100 且 n<30000) 或 m < 6   → default_find      （Bloom+Horspool）
   否则 若 needle 占比 < ~33%                      → two_way_find/count（最坏 O(n)）
   否则                                            → adaptive_find     （先 default，命中率过高再切 two_way）
```

**Bloom 过滤**用单个 `unsigned long`（位宽随 `LONG_BIT` 取 32/64/128）做位图：把 needle 的每个字符按 `ch & (WIDTH-1)` 置位。比对时若「草垛下一字符」不在位图里，就能整段跳过 `m` 个位置——这是 Horspool 跳过的依据。

**两路算法（Two-Way）**先用 `factorize`（基于 `lex_search`）把 needle 在「临界分解点」切成左右两半，匹配时先比右半、再比左半，借助周期性分析保证线性最坏复杂度。

三种模式用三个宏区分：

[`numpy/_core/src/umath/string_fastsearch.h:39-51`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_fastsearch.h#L39-L51) —— `FAST_COUNT`/`FAST_SEARCH`/`FAST_RSEARCH` 分别表示计数/正向查找/反向查找。

#### 4.5.3 源码精读

调度入口，看清三档自适应的阈值：

[`numpy/_core/src/umath/string_fastsearch.h:1256-1315`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_fastsearch.h#L1256-L1315) —— `fastsearch`：先处理边界（`n<m`、`m<=1`），再按规模在 `default_find`/`two_way`/`adaptive_find`/`default_rfind` 间分流。

Bloom 过滤宏——「坏字符跳过」的基础：

[`numpy/_core/src/umath/string_fastsearch.h:84-99`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_fastsearch.h#L84-L99) —— `STRINGLIB_BLOOM_ADD` 置位、`STRINGLIB_BLOOM` 查位。

`default_find` 的主循环（Bloom + Horspool 经典实现）：

[`numpy/_core/src/umath/string_fastsearch.h:981-1040`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_fastsearch.h#L981-L1040) —— 先建 Bloom 与 gap，再从尾部字符对齐比对：命中尾部即逐字核对；不命中则按「下一字符是否在 Bloom」决定跳 `m` 还是跳 `gap`。

两路算法的主体：

[`numpy/_core/src/umath/string_fastsearch.h:736-873`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_fastsearch.h#L736-L873) —— `two_way`：分「needle 周期/非周期」两条路径，先右半后左半比对，借助 `table`（坏字符位移表）与 `period`/`gap` 跳过。

而 `string_buffer.h` 这边的高层入口 `string_find` 怎么用 `fastsearch`？它先处理 `len2==1`（单字符走 `find_char`）和 start/end 边界，多字符才转交 `fastsearch`：

[`numpy/_core/src/umath/string_buffer.h:910-936`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_buffer.h#L910-L936) —— `string_find` 的多字符分支：按编码把「字节指针 + 字节长度」交给 `fastsearch(..., FAST_SEARCH)`，UTF8 还要把返回的字节索引换算回字符索引。

`string_count` 同理，只是模式换成 `FAST_COUNT`：

[`numpy/_core/src/umath/string_buffer.h:1089-1104`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_buffer.h#L1089-L1104) —— 三种编码各自调 `fastsearch(..., FAST_COUNT)`。

#### 4.5.4 代码实践

1. **目标**：用 NumPy 侧调用体会「find/count 走的就是 fastsearch 这条路」，并用大草垛感受它确实很快。
2. **步骤**：

   ```python
   import numpy as np, time
   hay = np.array(['a'*10_000_000 + 'needle'], dtype=np.dtypes.StringDType())
   ndl = np.array(['needle'], dtype=np.dtypes.StringDType())
   t = time.perf_counter()
   pos = np.strings.find(hay, ndl)
   print('pos =', pos, 'elapsed_ms =', (time.perf_counter()-t)*1000)
   print('count =', np.strings.count(hay, ndl))
   ```
3. **观察**：在千万级字符里找一次子串仍只需毫秒级——这正是 Two-Way \(O(n)\) 最坏复杂度的体现。
4. **预期结果**：`pos` 等于 `'a'*10_000_000` 的长度，`count=1`，耗时很短。
5. 说明：具体耗时「待本地验证」，结论方向（线性时间）由算法确定。

#### 4.5.5 小练习与答案

- **练习**：为什么 `fastsearch` 对小串（`n<2500`）宁可走 `default_find` 而不用更「高级」的 Two-Way？
- **答案**：Two-Way 需要 \(O(m)\) 的预处理（`factorize`、建位移表），小串上这笔启动成本得不偿失；`default_find` 的 Bloom+Horspool 几乎零启动、均摊 \(O(n)\)，小串反而更快。
- **练习**：`adaptive_find` 相比 `default_find` 多了什么？
- **答案**：它在 `default_find` 基础上统计「命中次数（hits）」，一旦发现「比对了很多字符却没匹配」（`hits > m/4` 且剩余 `>2000`），就判定 needle 可能频繁出现、转用 Two-Way 以保证最坏线性——即「先用便宜的，必要时升级到有保证的」。

---

## 5. 综合实践

**任务：画一条从 Python 到 fastsearch 的完整调用链，并解释编码分叉点。**

以 `np.strings.find(np.array(['hello'], dtype=np.str_), 'll')` 为例，完成下面三件事：

1. **追链路**（源码阅读型）。按顺序定位并阅读这些位置，把每一跳的「文件:函数」串起来：
   - Python 包装：`numpy/_core/strings.py` 的 `find`（u2-l7 已讲，复习它如何把 `end=None` 归一化为 `MAX` 哨兵并转交 `_find_ufunc`）。
   - 循环分发：`numpy/_core/src/umath/string_ufuncs.cpp` 的 `string_findlike_loop<enc>`（[L312-L346](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_ufuncs.cpp#L312-L346)）——注意它从 `context->method->static_data` 取出具体的 `string_find<enc>` 函数指针。
   - 算法主体：`string_buffer.h` 的 `string_find`（[L846-L936](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_buffer.h#L846-L936)）。
   - 引擎：`string_fastsearch.h` 的 `fastsearch`（[L1256-L1315](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_fastsearch.h#L1256-L1315)）。

2. **标分叉**。把同一调用换成三种 dtype，填下表（关键看「编码标签」与「getchar 字节数」两列）：

   | 输入 dtype | ENCODING | getchar 读取字节数 | 走的循环文件 | fastsearch 的 char_type |
   |------------|----------|-------------------|--------------|-------------------------|
   | `bytes_` ('S') | ASCII | 1 | string_ufuncs.cpp | `char` |
   | `str_` ('U') | UTF32 | 4 | string_ufuncs.cpp | `npy_ucs4` |
   | StringDType ('T') | UTF8 | 1–4（变长） | stringdtype_ufuncs.cpp | `char`（按字节） |

3. **回答两个问题**：
   - 为什么 `str_` 输入下 `fastsearch` 收到的 `char_type` 是 `npy_ucs4`，而 StringDType 下是 `char`？（提示：看 `string_find` 的 UTF32 分支传 `(npy_ucs4*)start_loc`，UTF8 分支传字节指针。）
   - `string_find` 在 UTF8 下多做了哪一步「字节索引→字符索引」的换算？为什么 UTF32 不用做？（提示：见 [string_buffer.h:917-921](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/umath/string_buffer.h#L917-L921) 的 `utf8_character_index` 调用。）

> 交付物：一张调用链图 + 上面这张填好的表 + 两个问题的中文解答。这个任务把本讲的 `ENCODING`/`getchar`/`fastsearch` 与 u3-l12 的循环注册、u2-l7 的 Python 包装完全打通。

## 6. 本讲小结

- `ENCODING`（`ASCII`/`UTF32`/`UTF8`）是编译期分发开关：定长 ufunc（`string_ufuncs.cpp`）只用 `ASCII` 与 `UTF32`，`UTF8` 仅服务于 StringDType（`stringdtype_ufuncs.cpp`）。
- `getchar<enc>` 的三个特化回答了「一个字符占几字节」：ASCII 恒 1、UTF32 恒 4、UTF8 变长 1–4（由首字节查表 `num_bytes_for_utf8_character` 决定）。
- `codepoint_is*` 按 `enc` 特化（ASCII 走 `NumPyOS_ascii_*`、Unicode 走 `Py_UNICODE_*`），`IMPLEMENTED_UNARY_FUNCTIONS` + `call_buffer_member_function` + `unary_loop` 让 `isalpha` 等共用同一遍历骨架。
- `Buffer<enc>` 把编码差异藏进运算符重载（`++`/`*`/`num_codepoints`），让上层算法写成与编码无关的「字符级」逻辑。
- `string_fastsearch.h` 移植自 CPython，用 Bloom+Horspool（`default_find`）、Two-Way（`two_way`）、自适应（`adaptive_find`）三档策略，支撑 `find`/`index`/`count` 的线性最坏时间子串搜索。
- 本讲原语是 u3-l12 注册的循环体内部真正干活的部分；与 u2-l7 的 Python 查找函数首尾相接，构成完整调用链。

## 7. 下一步学习建议

- **继续下探 StringDType 路径**：读 u3-l14（StringDType 专用 ufunc 循环），重点看 `stringdtype_ufuncs.cpp` 如何用 `Buffer<ENCODING::UTF8>` 与本讲的 `string_find`/`fastsearch` 协作，以及 `utf8_utils` 里 `find_start_end_locs`/`utf8_character_index` 如何处理 UTF8 的「字符索引 ↔ 字节索引」双向换算。
- **横向对照 CPython**：`string_fastsearch.h` 整体移植自 CPython `Objects/stringlib/fastsearch.h`、Two-Way 注释里指向的 `stringlib_find_two_way_notes.txt`。建议去 CPython 仓库对照阅读，能更深刻理解 `factorize`/`two_way` 的数学基础（临界分解定理）。
- **补齐 `string_buffer.h` 的其余算法**：本讲只精读了查找与分类原语，该头文件还含 `string_replace`、`string_pad`/`string_zfill`、`string_expandtabs`、`string_partition`、`string_lrstrip_*` 等。它们分别对应 u2-l6/u2-l9/u2-l10/u2-l11 讲过的 Python 函数，可逐个回看「Python 包装 ↔ C 算法」的对应关系。
- **回到测试**：结合 u3-l16，阅读 `test_strings.py` 中针对 `find`/`count`/`isalpha` 的用例，验证你对本讲编码分叉与 fastsearch 行为的理解是否与实测一致。
