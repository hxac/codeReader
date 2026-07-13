# 索引机制：基础索引与花式索引

## 1. 本讲目标

学完本讲后，你应该能够：

- 区分**基础索引（basic indexing）**与**高级/花式索引（advanced/fancy indexing）**这两大类操作。
- 准确判断任意一次 `arr[...]` 取数**何时返回视图、何时返回拷贝**，并能用 `np.shares_memory` 验证。
- 在 C 层定位索引的统一入口 `array_subscript`，看懂它如何用一个位掩码（`HAS_INTEGER`/`HAS_BOOL`/`HAS_FANCY` …）把不同索引分发到不同实现。
- 读懂 `get_view_from_index`（视图怎么算出来）与 `array_boolean_subscript`/`PyArray_MapIterNew`（拷贝怎么造出来）。
- 顺着赋值链 `array_assign_subscript` → `mapiter_set`，看懂花式索引**赋值**的缓冲搬运实现，以及缓冲区刷新失败时类型转换错误如何传播。
- 复现并理解一个真实的回归修复（PR [#31975](https://github.com/numpy/numpy/pull/31975) / gh-31974）：当目标数组超过一个缓冲区块时，花式索引赋值中的类型转换错误曾被静默吞掉，现在会被正确抛出。
- 熟练使用 `np.ix_`、`np.r_`、`np.c_`、`np.s_` 这些索引辅助器，并理解它们和原始切片语法的关系。

本讲承接 [u2-l1](u2-l1-array-creation.md) 建立的「视图与拷贝」「`array_like`」概念，进一步钻进 `arr[...]` 这个最常见操作的实现内部。

## 2. 前置知识

在进入源码前，先用通俗语言把几个关键词讲清楚。

- **视图（view）**：不复制底层数据，只新建一个 `ndarray` 头（`shape`/`strides`/`data` 指针），让两份数组共享同一块内存。改其一，另一个跟着变。在 [u1-l4](u1-l4-ndarray-first-look.md) 我们已经看到 `strides` 单位是字节、转置只改 `strides` 不动数据——那正是视图。
- **拷贝（copy）**：新开一块内存，把数据逐元素搬过去。两份数组此后互不相干。
- **基础索引**：用**整数**、**切片对象**（`slice(start, stop, step)`）、**省略号**（`...`，即 `Ellipsis`）、**`None`**（`np.newaxis`）组成的索引。它**永远返回视图**（标量除外）。
- **高级/花式索引**：用**整数数组**或**布尔数组**作为索引。它**永远返回拷贝**。
- **映射协议（mapping protocol）**：Python 里 `obj[key]` 由类型的 `mp_subscript` 槽位处理；`obj[key] = val` 由 `mp_ass_subscript` 处理。NumPy 的 `ndarray` 把这两个槽位都接到了 `mapping.c`。
- **`.c.src` 模板文件**：`lowlevel_strided_loops.c.src` 不是直接编译的 C 文件，而是构建期模板。`@name@`、`@isget@`、`@num_fancy@`、`@elsize@` 这类 `@变量@` 占位符会被展开器替换成具体值，`/**begin repeat**/.../**end repeat**/` 块会被复制多份。最终它生成出 `mapiter_get`（取数）和 `mapiter_set`（赋值）两个真实函数——同一个模板，靠 `@isget@` 开关分别产出取/赋两套实现。
- **迭代器缓冲（iterator buffering）**：当花式索引搬运需要做类型转换（如 `object`→`int`），且源、目标对齐/尺寸不匹配时，NumPy 的 `NpyIter` 会开启**缓冲模式**：用一块固定大小的内存（默认 `NPY_BUFSIZE = 8192` 个元素）作为中转，分批把数据搬进缓冲区、转换、再搬出。这把一次大搬运切成了多个“缓冲区块”。本讲的 #31975 修复正发生在“刷新下一个缓冲区块”这一步。

一个元素在 N 维数组里的字节偏移由下式决定（`strides` 单位是字节）：

\[
\text{offset} = \sum_{i=0}^{ndim-1} \text{index}_i \cdot \text{strides}_i
\]

基础索引的精髓就是：**不真的去算每个元素的偏移再搬数据，而是改写 `shape`/`strides`/`data` 这三个小量，让“新数组”的同一个公式自然落在正确的内存位置上**。下一节我们会看到 C 源码正是这么做的。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [numpy/_core/numeric.py](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/numeric.py) | Python 层的索引辅助函数：`argwhere`、`flatnonzero`、`normalize_axis_tuple`、`indices`。它们在切片语义之上提供“生成下标”和“规整轴参数”的能力。 |
| [numpy/_core/src/multiarray/mapping.c](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/src/multiarray/mapping.c) | C 层映射协议的全部实现：统一入口 `array_subscript`、索引解析 `prepare_index`、视图构造 `get_view_from_index`、布尔索引 `array_boolean_subscript`、花式索引迭代器 `PyArray_MapIterNew`。 |
| [numpy/_core/src/multiarray/mapping.h](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/src/multiarray/mapping.h) | 索引类型位掩码 `HAS_INTEGER`/`HAS_BOOL`/`HAS_FANCY` … 的定义，以及 `PyArrayMapIterObject` 与 `npy_index_info` 结构体。 |
| [numpy/_core/src/multiarray/lowlevel_strided_loops.c.src](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/src/multiarray/lowlevel_strided_loops.c.src) | 构建期模板，展开成 `mapiter_get`/`mapiter_set`——花式索引取数与赋值的底层逐元素搬运循环。`mapiter_set` 的**缓冲赋值路径**就是 #31975 修复所在之处。 |
| [numpy/lib/_index_tricks_impl.py](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/lib/_index_tricks_impl.py) | 索引辅助器 `ix_`、`r_`、`c_`、`s_`、`index_exp` 的纯 Python 实现。 |
| [numpy/_core/tests/test_indexing.py](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/tests/test_indexing.py) | 索引行为的官方测试集，是验证“视图还是拷贝”的权威依据。 |

> 提示：`arr[...]` 的“引擎”几乎全在 C 层（`mapping.c`），`numeric.py` 里**没有**一个叫 `slice` 的函数。`numeric.py` 提供的是“围绕索引”的 Python 工具，本讲把它们和 C 引擎一起讲清楚。

---

## 4. 核心概念与源码讲解

### 4.1 索引的统一入口：C 层映射协议 array_subscript

#### 4.1.1 概念说明

当你写下 `arr[key]`，Python 不会去调某个 Python 函数，而是直接查找 `ndarray` 类型的 `mp_subscript` 槽位。NumPy 在这个槽位上注册的就是 C 函数 `array_subscript`。换句话说，**所有形式的索引——切片、整数、布尔数组、整数数组、`...`、`None`——都从同一个大门进来**，再由 `array_subscript` 内部分流。

这个“分流”是本讲最重要的关节：它决定了你这次取数走的是“视图路径”还是“拷贝路径”。

#### 4.1.2 核心流程

`array_subscript` 的执行可以分为三步：

1. **解析索引**：调用 `prepare_index` 把 Python 传来的 `key`（可能是单个对象或元组）拆成一个 `npy_index_info indices[]` 数组，同时返回一个**位掩码** `index_type`，标注这次索引里出现了哪些种类。
2. **按掩码分流**：用 `if/else if` 链对照 `index_type`，选择最快的实现路径。
3. **善后**：释放临时引用，返回结果。

位掩码的定义在 `mapping.h`，每个常量是 2 的幂，可以按位或组合：

```c
#define HAS_INTEGER 1      // 出现了整数
#define HAS_NEWAXIS 2      // 出现了 None / np.newaxis
#define HAS_SLICE 4        // 出现了 slice
#define HAS_ELLIPSIS 8     // 出现了 ...
#define HAS_FANCY 16       // 出现了整数数组（花式索引）
#define HAS_BOOL 32        // 出现了“整组布尔掩码”这一特殊情形
#define HAS_SCALAR_ARRAY 64
#define HAS_0D_BOOL (HAS_FANCY | 128)
```

分流策略可以浓缩成下面这张表：

| `index_type` 命中 | 走的函数 | 结果 |
| --- | --- | --- |
| 仅 `HAS_INTEGER`（每维一个整数） | `get_item_pointer` + `PyArray_Scalar` | 返回**标量**（Python 标量，非数组） |
| 仅 `HAS_BOOL`（单个布尔数组，形状与被索引数组相同） | `array_boolean_subscript` | 返回 1-D **拷贝** |
| 仅 `HAS_ELLIPSIS`（就是 `arr[...]`） | `PyArray_View` | 返回**视图** |
| 含 `HAS_SLICE`/`HAS_NEWAXIS`/`HAS_ELLIPSIS`/`HAS_INTEGER` 但**不含** `HAS_FANCY` | `get_view_from_index` | 返回**视图** |
| 含 `HAS_FANCY`（整数数组，或非整组布尔） | `PyArray_MapIterNew` + `mapiter_get` | 返回**拷贝** |

记住结论：**切片/整数/省略号/None 这一族（基础索引）走视图；整数数组和布尔数组（花式索引）走拷贝。**

#### 4.1.3 源码精读

先看槽位注册。`array_as_mapping` 把三个 C 函数绑到 `ndarray` 的映射协议上——取数、赋值、取长度各一个：

[numpy/_core/src/multiarray/mapping.c:2214-2218](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/src/multiarray/mapping.c#L2214-L2218) — `mp_subscript` 槽位接到 `array_subscript`，`mp_ass_subscript` 接到 `array_assign_subscript`。

位掩码常量定义在头文件里：

[numpy/_core/src/multiarray/mapping.h:7-21](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/src/multiarray/mapping.h#L7-L21) — 7 个 `HAS_*` 位标志，注释里特别提醒 `HAS_FANCY` 可与 `HAS_0D_BOOL` 混用，比较时要注意用 `&` 还是 `==`。

接着是入口函数本体。先看它如何解析索引并取出掩码：

[numpy/_core/src/multiarray/mapping.c:1519-1525](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/src/multiarray/mapping.c#L1519-L1525) — `prepare_index(...)` 把 `op`（即你写的 `key`）解析进 `indices[]`，回填 `index_num`/`ndim`/`fancy_ndim`，返回值就是 `index_type` 位掩码；`< 0` 表示解析失败（比如浮点数当索引）。

`prepare_index` 只是个薄包装，真正干活的是 `prepare_index_noarray`：

[numpy/_core/src/multiarray/mapping.c:771-779](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/src/multiarray/mapping.c#L771-L779) — 把被索引数组的 `ndim`/`dims` 补上后转交给 `prepare_index_noarray`。

然后是分流链。先看“纯整数 → 标量”这一支：

[numpy/_core/src/multiarray/mapping.c:1527-1537](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/src/multiarray/mapping.c#L1527-L1537) — `HAS_INTEGER` 命中时，`get_item_pointer` 算出那个元素的字节地址，`PyArray_Scalar` 把它包装成 Python 标量返回。注意它直接 `return`，不走 `finish` 清理，因为整数索引不需要额外引用。

“纯省略号 → 视图”这一支：

[numpy/_core/src/multiarray/mapping.c:1547-1557](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/src/multiarray/mapping.c#L1547-L1557) — `arr[...]` 走 `PyArray_View(self, NULL, NULL)`，得到一个**全新的视图对象**（不是 `self` 本身）。代码注释里有一句很关键的 TODO：以前 `arr[...]` 直接返回 `self`，现在为了语义一致（保证 `arr[...] += 1` 之类操作的行为可预测）改为返回视图。

“基础索引 → 视图”这一支（含切片/省略号/None/整数，但不含花式）：

[numpy/_core/src/multiarray/mapping.c:1566-1588](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/src/multiarray/mapping.c#L1566-L1588) — 调 `get_view_from_index` 算出视图；只要掩码里没有 `HAS_FANCY`，这个 `view` 就是最终结果，`Py_INCREF` 后直接返回。

“花式索引 → 拷贝”这一支：

[numpy/_core/src/multiarray/mapping.c:1645-1655](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/src/multiarray/mapping.c#L1645-L1655) — 构造 `PyArrayMapIterObject`（迭代器），把前面算好的 `view` 作为“子空间”传进去，最终由 `mapiter_get` 把数据搬运到新数组 `mit->extra_op` 里。

中间还有一个值得注意的快速通道：当索引**恰好是一个**一维整数数组（`index_type == HAS_FANCY && index_num == 1`）时，NumPy 会跳过通用迭代器，走 `mapiter_trivial_get` 这条更快的路径：

[numpy/_core/src/multiarray/mapping.c:1596-1643](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/src/multiarray/mapping.c#L1596-L1643) — 先检查索引数组是否“平凡可迭代”、`itemsize` 是否等于 `npy_intp`、是否无符号对齐等；满足才分配结果数组并调 `mapiter_trivial_get`。这是 `arr[[0,2,3]]` 这类常见操作很快的原因。

#### 4.1.4 代码实践

**目标**：用 `git grep` 在源码里亲手定位分流链，把“掩码 → 分支”的对应关系落到行号上。

**步骤**：

1. 在仓库根目录执行 `grep -n "index_type ==" numpy/_core/src/multiarray/mapping.c`。
2. 再执行 `grep -n "index_type &" numpy/_core/src/multiarray/mapping.c`。
3. 对照本节表格，把每条命中映射到“走哪个函数、返回视图还是拷贝”。

**需要观察的现象**：`==` 形式只出现在几个特殊分支（纯整数、纯布尔、纯省略号、单花式快速通道），而 `&` 形式（按位与）出现在“是否含花式”“是否含切片”等组合判断里——这正反映了位掩码的设计意图：**用 `==` 判定唯一形态，用 `&` 判定是否包含某类索引**。

**预期结果**：你会看到大约 4 处 `==` 和若干处 `&`，分别对应表中各行。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `arr[...]` 在新版 NumPy 里返回的是视图而不是 `arr` 本身？请引用源码注释说明。

**参考答案**：`mapping.c:1549-1553` 的注释写道：“TODO: Should this be a view or not? … Before, it was just self for a single ellipsis.” 也就是旧实现直接返回 `self`，新实现改用 `PyArray_View` 返回视图，以保证 `arr[...] += 1` 这类操作的语义一致。

**练习 2**：`HAS_FANCY` 的值是 16，`HAS_0D_BOOL` 是 `HAS_FANCY | 128`。如果代码写 `index_type == HAS_FANCY` 来判断“是否为纯花式”，0D 布尔的情形会被误判吗？

**参考答案**：会。`HAS_0D_BOOL = 16 | 128 = 144`，所以 `144 == 16` 为假，0D 布尔不会落入“纯花式快速通道”。这正是 `mapping.h:11` 注释“be careful when to use & or ==”的含义——判断“是否含花式”要用 `index_type & HAS_FANCY`，判断“是否恰好纯花式”才用 `==`，且要意识到 0D 布尔这一特例。

---

### 4.2 基础索引与切片语义（返回视图）

#### 4.2.1 概念说明

基础索引由四类组件拼成：

- **整数** `i`：选中第 `i` 个位置，**消去**这一维（`a[0]` 把 2-D 变 1-D）。
- **切片** `start:stop:step`：在某一维上取一段，**保留**这一维。
- **省略号** `...`：自动补足到完整维数，等价于若干个 `:`。
- **`None` / `np.newaxis`**：**插入**一个长度为 1 的新维。

基础索引的关键性质是：**它只改写 `shape`、`strides` 和 `data` 起始指针，从不搬运数据**。因此结果总是视图（标量除外），改结果会改原数组。

Python 层 `numeric.py` 没有实现切片“引擎”（那是 C 干的），但它提供了围绕索引的几个常用工具：`argwhere`/`flatnonzero` 用 `nonzero` 生成下标数组，`normalize_axis_tuple` 把轴参数规整成非负整数元组，`indices` 生成网格下标。理解它们能帮你把“我要哪些位置”转换成“索引对象”。

#### 4.2.2 核心流程

切片 `start:stop:step` 作用在某一维（原步长 `S`、原长度 `D`）时，C 层这样把视图算出来（`n_steps` 为取到的元素数）：

\[
n\_steps = \left\lceil \frac{\max(\text{stop}-\text{start},\,0)}{\text{step}} \right\rceil,\quad
\text{新 data 指针} += S \cdot \text{start},\quad
\text{新 stride} = S \cdot \text{step},\quad
\text{新 shape} = n\_steps
\]

也就是说：起点平移 `data` 指针、步长乘进 `stride`、长度变成实际命中数。整数的处理更简单——`data += S * i`，且这一维消失（不写入 `new_shape`）。`None` 则写入一个 `shape=1, stride=0` 的新维。

伪代码：

```
data_ptr = arr.data
for 每个索引分量:
    if 整数 i:   data_ptr += S * i;            # 该维消失
    if 切片:     data_ptr += S * start;
                 new_strides.append(S * step);
                 new_shape.append(n_steps);    # 该维保留
    if None:     new_strides.append(0);
                 new_shape.append(1);          # 插入新维
    if ...:      把跳过的若干维原样拷进 new_shape/new_strides
用 (new_shape, new_strides, data_ptr) 新建一个视图
```

#### 4.2.3 源码精读

C 引擎的核心是 `get_view_from_index`，它逐个分量地改写 `data_ptr`/`new_strides`/`new_shape`：

[numpy/_core/src/multiarray/mapping.c:864-946](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/src/multiarray/mapping.c#L864-L946) — 函数开头声明 `new_strides`/`new_shape`/`data_ptr`，结尾用 `PyArray_NewFromDescr_int` 创建视图。重点看 `switch (indices[i].type)` 这段：

[numpy/_core/src/multiarray/mapping.c:877-930](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/src/multiarray/mapping.c#L877-L930) —
- `HAS_INTEGER`（879-889）：`data_ptr += STRIDE * value`，`new_dim += 0`（维数不增加，即消去该维）。
- `HAS_ELLIPSIS`（890-897）：把跳过的维原样拷入。
- `HAS_SLICE`（898-916）：先用 `PySlice_GetIndicesEx` 把切片解成 `start/stop/step/n_steps`，再 `data_ptr += STRIDE*start`、`new_strides = STRIDE*step`、`new_shape = n_steps`。
- `HAS_NEWAXIS`（917-921）：`new_strides=0, new_shape=1`。

视图的最终创建在末尾，注意它把 `self` 设为 `base`：

[numpy/_core/src/multiarray/mapping.c:932-944](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/src/multiarray/mapping.c#L932-L944) — `PyArray_NewFromDescr_int(...)` 用算好的 `new_dim/new_shape/new_strides/data_ptr` 造视图，并把 `(PyObject *)self` 作为 base，这就是为什么 `arr[:].base is arr`。

纯整数索引走的是更简单的 `get_item_pointer`，它就是本文开篇那个偏移公式的逐维累加：

[numpy/_core/src/multiarray/mapping.c:830-843](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/src/multiarray/mapping.c#L830-L843) — `*ptr = PyArray_BYTES(self)`，循环里 `*ptr += PyArray_STRIDE(self, i) * indices[i].value`，正是 \(\text{offset} = \sum_i \text{index}_i \cdot \text{strides}_i\)。

再看 Python 层的索引辅助函数。`flatnonzero` 返回扁平化后非零位置的下标，常用来当整数数组索引：

[numpy/_core/numeric.py:675-714](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/numeric.py#L675-L714) — 主体只有一行 `return np.nonzero(np.ravel(a))[0]`，文档示例 `x.ravel()[np.flatnonzero(x)]` 正是把结果当花式索引用。

`argwhere` 把非零位置按“每个元素一行”排好，但文档明确警告它的输出**不能**直接当索引：

[numpy/_core/numeric.py:620-668](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/numeric.py#L620-L668) — 实现为 `transpose(nonzero(a))`，形状 `(N, ndim)`；文档注释 “The output of `argwhere` is not suitable for indexing arrays. For this purpose use `nonzero(a)` instead.” 这是因为 `nonzero` 返回的是“每维一组下标”（可直接做花式索引），而 `argwhere` 是“每元素一组坐标”。

`normalize_axis_tuple` 是把 `axis` 参数（可能是 `int`、`list`、`tuple`）规整成非负整数元组的内部工具，几乎所有接受 `axis` 的函数都靠它：

[numpy/_core/numeric.py:1429-1483](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/numeric.py#L1429-L1483) — 先把单个 `int` 包成 `[axis]`，再逐个调 `normalize_axis_index` 把负轴转正并查越界，最后用 `set` 判重。

`indices` 生成网格下标，是 `mgrid`/`ogrid` 的底层表亲：

[numpy/_core/numeric.py:1728-1826](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/numeric.py#L1728-L1826) — 对每一维用 `arange(dim).reshape(...)` 造一个“只在第 i 维变化”的数组；`sparse=True` 时返回元组（形状形如 `(1,..,dim,..,1)`），可直接拿来当花式索引。

#### 4.2.4 代码实践

**目标**：用源码阅读型实践验证“切片只是改写 shape/strides/data”。

**步骤**：

1. 阅读上面的 [mapping.c:898-916](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/src/multiarray/mapping.c#L898-L916) 切片分支。
2. 在 Python 里构造 `a = np.arange(12, dtype=np.int64).reshape(3,4)`，手动预测 `b = a[1:, ::2]` 的 `shape`、`strides`、`b.base is a`。
3. 计算：原 `strides=(32, 8)`（int64 每元素 8 字节，3×4 行主序）；切片 `1:` 使 `data += 32*1`、第 0 维 shape 变 2；`::2` 使第 1 维 stride 变 `8*2=16`、shape 变 `ceil(4/2)=2`。
4. 运行验证：

```python
import numpy as np
a = np.arange(12, dtype=np.int64).reshape(3, 4)
b = a[1:, ::2]
print(b.shape, b.strides, b.base is a)   # 预期 (2, 2) (32, 16) True
b[0, 0] = 99
print(a[1, 0])                            # 预期 99，证明是视图
```

**需要观察的现象**：`b.strides` 与你手算一致；`b.base is a` 为 `True`；修改 `b` 后 `a` 对应位置也变。

**预期结果**：`(2, 2) (32, 16) True`，且 `a[1, 0]` 变为 99。这正是 `get_view_from_index` 只改写三个量、不搬数据的效果。

#### 4.2.5 小练习与答案

**练习 1**：`a[None]` 和 `a[np.newaxis]` 为什么等价？它在 `get_view_from_index` 里走哪个 `case`，写入的 `new_strides` 和 `new_shape` 各是多少？

**参考答案**：`np.newaxis` 就是 `None`。在 `get_view_from_index` 的 `switch` 里走 `HAS_NEWAXIS` 分支（[mapping.c:917-921](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/src/multiarray/mapping.c#L917-L921)），写入 `new_strides=0`、`new_shape=1`——一个长度为 1、步长为 0 的新维，不占用真实内存跨度。

**练习 2**：`np.argwhere(x > 0)` 的结果形状是 `(N, ndim)`，为什么不能直接 `x[np.argwhere(x > 0)]` 当索引？

**参考答案**：因为花式索引要求“每个被索引维提供一组下标”（即 `nonzero` 返回的形式，是 `ndim` 个一维数组）。`argwhere` 返回的是 `(N, ndim)` 的二维数组，若直接当索引，NumPy 会把它当成对**第一维**的整数数组索引（形状变成 `(N, ndim) + ...`），语义完全不同。正确做法是 `x[np.nonzero(x > 0)]` 或 `x[x > 0]`（后者走布尔花式）。

---

### 4.3 高级（花式）索引：整数数组与布尔索引（返回拷贝）

#### 4.3.1 概念说明

花式索引分两种：

- **整数数组索引**：用一个（或多个）整数数组指定要取的下标，如 `a[[0, 2, 0]]`、`a[[0,1], [2,3]]`。
- **布尔索引**：用布尔数组当掩码，如 `a[a > 0]`、`a[mask]`。

无论哪种，结果都是**拷贝**——新开内存、逐元素搬运。这是因为花式索引选取的元素在内存里通常不连续（甚至重复），无法用一套 `shape/strides` 表达成视图。

有一个**特例**要分清：当**单个布尔数组**的形状与被索引数组**完全相同**时，`prepare_index` 会把它归类成 `HAS_BOOL`（而非 `HAS_FANCY`），走一条专门的 `array_boolean_subscript` 路径，结果是 1-D 拷贝。而 `a[mask, :]`（布尔数组只对应第一维）或 `a[[0,2]]`（整数数组）则归类成 `HAS_FANCY`，走通用迭代器。

#### 4.3.2 核心流程

**布尔掩码（整组，`HAS_BOOL`）**：

1. `count_boolean_trues` 数出掩码里 `True` 的个数 `N`。
2. 新分配一个长度为 `N` 的 1-D 数组。
3. 用 `NpyIter` 同时遍历原数组和掩码，凡掩码为 `True` 就把对应元素拷进结果。

**花式（`HAS_FANCY`）**：

1. `PyArray_MapIterNew` 构造迭代器，把多个整数索引数组**广播**到同一形状（这就是 `a[[0,1],[2,3]]` 取对角元素的原理）。
2. 若索引数组之间存在未被索引的“剩余维”，构造一个 `subspace` 视图。
3. `mapiter_get` 按广播后的每个位置，把对应元素（或子块）拷进结果数组 `extra_op`。
4. 若高级索引在原数组里是**连续**位置，`PyArray_MapIterSwapAxes` 会把结果轴转回原位（这就是“连续高级索引保留位置、分离高级索引移到最前”规则的实现）。

#### 4.3.3 源码精读

整组布尔掩码路径的入口与分配：

[numpy/_core/src/multiarray/mapping.c:959-976](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/src/multiarray/mapping.c#L959-L976) — 先 `count_boolean_trues` 算出 `size`，再 `PyArray_NewFromDescr(..., 1, &size, ...)` 分配一个**新的一维数组** `ret`。注意维度硬编码为 1——布尔掩码的结果永远是 1-D。

整组布尔路径在 `array_subscript` 里的命中点：

[numpy/_core/src/multiarray/mapping.c:1539-1545](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/src/multiarray/mapping.c#L1539-L1545) — `index_type == HAS_BOOL` 时调 `array_boolean_subscript`，结果直接 `goto finish`。这条路径不经过 `MapIter`，是专门为“整组布尔掩码”做的优化。

通用花式路径在 `array_subscript` 里的命中点：

[numpy/_core/src/multiarray/mapping.c:1645-1672](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/src/multiarray/mapping.c#L1645-L1672) — `PyArray_MapIterNew(...)` 把解析好的 `indices`、`view`（子空间）、`self` 组装进 `mit`；若 `num_fancy > 1`（多个高级索引需要广播）或 `size == 0`，先调 `PyArray_MapIterCheckIndices` 做越界检查；最后 `NpyIter_Reset` 启动外层迭代器。

`MapIter` 的核心结构体定义说明了它如何同时持有原数组、结果数组、子空间和三套迭代器：

[numpy/_core/src/multiarray/mapping.h:28-107](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/src/multiarray/mapping.h#L28-L107) — 字段 `array`/`extra_op`/`subspace` 分别是被索引数组、结果数组、子空间视图；`outer`/`extra_op_iter`/`subspace_iter` 是三套 `NpyIter`；`consec` 记录“高级索引是否连续、插在哪一轴”，决定是否需要 `PyArray_MapIterSwapAxes` 把轴转回。

结果轴回转的逻辑（连续高级索引保留位置）：

[numpy/_core/src/multiarray/mapping.c:107-143](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/src/multiarray/mapping.c#L107-L143) — `PyArray_MapIterSwapAxes` 根据 `consec` 决定是否、以及把哪些轴转回去；注释里说清了 “MapIter always puts the advanced (array) indices first … But if they are consecutive, will insert/transpose them back before returning”。这正是 `a[[0,1], :, [2,3]]`（高级索引不连续→结果高级维移到最前）与 `a[:, [0,1], [2,3]]`（连续→保留中间）行为差异的来源。

Python 层没有“布尔索引函数”，但 `nonzero` 是布尔索引的近亲——`a[mask]`（整组布尔）等价于先 `nonzero(mask)` 再整数数组索引。`nonzero` 本身是 C 实现，由 `multiarray` 再导出。

#### 4.3.4 代码实践

**目标**：对比“整组布尔”与“花式”两条路径，并用 `shares_memory` 证明都是拷贝。

**步骤**：

```python
import numpy as np
a = np.arange(25).reshape(5, 5)

# (1) 整组布尔掩码：形状与 a 相同 → HAS_BOOL 路径，结果 1-D
mask = (a % 7 == 0)
r1 = a[mask]
print(r1.shape, np.shares_memory(a, r1))   # (n,) False

# (2) 布尔数组只对应第一维 → HAS_FANCY 路径，结果保留剩余维
row_mask = np.array([True, False, True, False, False])
r2 = a[row_mask, :]
print(r2.shape, np.shares_memory(a, r2))   # (2, 5) False

# (3) 整数数组索引 → HAS_FANCY 路径
r3 = a[[0, 2, 0]]                          # 注意 0 重复
print(r3.shape, np.shares_memory(a, r3))   # (3, 5) False

# (4) 对照：基础切片 → 视图
r4 = a[1:3, :]
print(np.shares_memory(a, r4))             # True
```

**需要观察的现象**：前三个 `shares_memory` 全为 `False`（拷贝），最后一个是 `True`（视图）；`r1` 被“拍扁”成 1-D，而 `r2`/`r3` 保留 2-D 形状。

**预期结果**：`(n,) False`、`(2, 5) False`、`(3, 5) False`、`True`。`r3` 还能看出整数数组索引允许重复下标（`0` 出现两次），这是视图做不到的——视图不可能让同一元素在结果里出现两遍。

#### 4.3.5 小练习与答案

**练习 1**：`a[a > 0]` 和 `a[(a > 0,)]`（注意第二个是元组）结果一样吗？为什么？

**参考答案**：一样。单个布尔数组 `a[a > 0]` 在 `unpack_indices` 里被当成“标量索引”处理（不是元组），最终仍被 `prepare_index` 识别为整组布尔掩码 `HAS_BOOL`；`(a > 0,)` 是单元素元组，也指向同一个布尔数组。两者走同一条 `array_boolean_subscript` 路径，结果相同。

**练习 2**：为什么 `a[[0,1], :, [2,3]]` 的结果会把高级索引维放到**最前面**，而 `a[:, [0,1], [2,3]]` 的高级索引维保留在**中间**？

**参考答案**：这是 `PyArray_MapIterSwapAxes` + `consec` 字段的规则（[mapping.c:107-143](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/src/multiarray/mapping.c#L107-L143)）。当多个高级索引在原数组里**不连续**（中间隔着 `:` 维），NumPy 无法保持它们的相对位置，统一把高级索引维移到结果最前；当它们**连续**时，`consec` 记下位置，结果里把高级维转回原位。前者 `[[0,1], ..., [2,3]]` 被 `:` 隔开故移到最前，后者 `[..., [0,1], [2,3], ...]` 连续故保留中间。

**练习 3**：用一句话解释为什么花式索引“必须”返回拷贝，而基础索引可以返回视图。

**参考答案**：基础索引选出的元素在内存里仍可由一套 `(shape, strides, data)` 线性描述（每个维度等步长），所以能做成视图；花式索引选出的元素位置任意（可重复、可不连续），无法用单一 `strides` 表达，只能逐元素拷到新数组。

---

### 4.4 索引辅助器：ix_、r_、c_、s_

#### 4.4.1 概念说明

直接写 `a[[0,1], [2,3]]` 时，NumPy 把两个整数数组**广播成相同形状**后逐配对——这取的是 `(0,2),(1,3)` 这种“对角”位置，而不是“所有行×所有列”的笛卡尔积。当你真正想要笛卡尔积（比如 `a` 的行 `{0,1}` 与列 `{2,3}` 的全部 4 个组合）时，就要用 `np.ix_` 把它们 reshape 成可广播的“开放网格”。

另外三个辅助器则把“切片语法”变成可传递、可拼接的对象：

- `np.s_[...]`：把方括号里的索引表达式原样转成 `slice` 对象或元组，**不取数**，方便你把索引存起来复用。
- `np.index_exp[...]`：同 `s_`，但总是包成元组。
- `np.r_[...]` / `np.c_[...]`：不是索引器，而是**构造器**——把切片/数组沿指定轴拼接成新数组。

#### 4.4.2 核心流程

`ix_` 的算法很简单，对第 `k` 个一维序列（长度 `n`，共 `nd` 个序列）：

\[
\text{shape} = (\underbrace{1,\ldots,1}_{k},\ n,\ \underbrace{1,\ldots,1}_{nd-k-1})
\]

即“除了第 `k` 维是 `n`，其余都是 1”。这样 N 个数组互相广播时，正好生成 `n_0 \times n_1 \times \cdots` 的笛卡尔积。若输入是布尔序列，先用 `nonzero()` 转成整数下标。

`r_`/`c_` 的 `__getitem__` 把方括号里的内容当成索引元组逐项翻译：`slice` → `arange`（步长为虚数时 → `linspace`）、标量 → 自身、数组 → 升维，最后用 `concatenate` 拼起来。`c_` 等价于 `r_['-1,2,0', ...]`，即沿最后一轴拼接、至少升到 2-D、1-D 数组转成列。

#### 4.4.3 源码精读

`ix_` 的全部实现只有十几行：

[numpy/lib/_index_tricks_impl.py:31-104](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/lib/_index_tricks_impl.py#L31-L104) — 逐个参数 `new`：转 `ndarray`（[93-94](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/lib/_index_tricks_impl.py#L93-L94)）；若是布尔则 `new.nonzero()` 取下标（[100-101](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/lib/_index_tricks_impl.py#L100-L101)）；最后 `reshape((1,)*k + (new.size,) + (1,)*(nd-k-1))`（[102](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/lib/_index_tricks_impl.py#L102)）。文档示例 `a[np.ix_([1,3],[2,5])]` 返回 `[[a[1,2],a[1,5]],[a[3,2],a[3,5]]]`，正是笛卡尔积。

`r_`/`c_` 的核心是 `AxisConcatenator.__getitem__`：

[numpy/lib/_index_tricks_impl.py:343-442](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/lib/_index_tricks_impl.py#L343-L442) — 逐项翻译：`slice` 且 `step` 是复数时走 `linspace`（[373-375](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/lib/_index_tricks_impl.py#L373-L375)），否则 `arange`（[376-377](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/lib/_index_tricks_impl.py#L376-L377)）；首项字符串是“特殊指令”（如 `'0,2'` 设 axis/ndmin，[382-405](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/lib/_index_tricks_impl.py#L382-L405)）；最后 `self.concatenate(tuple(objs), axis=axis)`（[435](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/lib/_index_tricks_impl.py#L435)）。

`r_` 与 `c_` 只是两个预设了不同 `axis`/`ndmin`/`trans1d` 的实例：

[numpy/lib/_index_tricks_impl.py:452-553](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/lib/_index_tricks_impl.py#L452-L553) — `RClass.__init__` 调 `AxisConcatenator.__init__(self, 0)`，沿第 0 轴拼；`r_ = RClass()`。

[numpy/lib/_index_tricks_impl.py:556-587](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/lib/_index_tricks_impl.py#L556-L587) — `CClass.__init__` 调 `AxisConcatenator.__init__(self, -1, ndmin=2, trans1d=0)`，沿最后一轴拼、至少 2-D、1-D 转成列；`c_ = CClass()`。

`s_` 与 `index_exp` 极简，只是把方括号语法转成对象：

[numpy/lib/_index_tricks_impl.py:723-781](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/lib/_index_tricks_impl.py#L723-L781) — `IndexExpression.__getitem__`：`maketuple=True` 时把非元组包成单元素元组，否则原样返回；`index_exp = IndexExpression(maketuple=True)`、`s_ = IndexExpression(maketuple=False)`（[780-781](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/lib/_index_tricks_impl.py#L780-L781)）。`np.s_[2::2]` 直接得到 `slice(2, None, 2)`。

#### 4.4.4 代码实践

**目标**：用 `ix_` 取笛卡尔积，并对照直接花式索引的差异。

**步骤**：

```python
import numpy as np
a = np.arange(25).reshape(5, 5)

# 直接花式索引：广播配对，取 (0,2) 和 (1,3) 两个点
print(a[[0, 1], [2, 3]])          # [2, 8]

# ix_：笛卡尔积，取 {0,1} × {2,3} 共 4 个点
print(a[np.ix_([0, 1], [2, 3])])
# [[ 2  3]
#  [ 7  8]]

# ix_ 也接受布尔序列（等价于 nonzero）
print(a[np.ix_([True, True, False, False, False], [2, 3])])

# s_ 把切片存成对象，可复用
idx = np.s_[1:3, ::2]
print(idx)                         # (slice(1, 3, None), slice(None, None, 2))
print(a[idx])                      # 等价于 a[1:3, ::2]
```

**需要观察的现象**：`a[[0,1],[2,3]]` 得到 1 维的两个元素（对角配对），而 `a[np.ix_([0,1],[2,3])]` 得到 2×2 的块（笛卡尔积）；`np.s_[...]` 不取数，只生成 `slice` 元组。

**预期结果**：第一行 `[2 8]`；第二行 `[[2 3],[7 8]]`；`idx` 打印为 `(slice(1, 3, None), slice(None, None, 2))`。

> 待本地验证：布尔序列经 `ix_` 的 `nonzero()` 转换后，与直接传整数列表的结果是否完全一致（应当一致）。

#### 4.4.5 小练习与答案

**练习 1**：`np.r_[1:5, 10, 20]` 和 `np.r_[1:5:4j]` 分别得到什么？写出推导。

**参考答案**：`1:5` 走 `arange(1,5,1)` → `[1,2,3,4]`；`10`、`20` 是标量直接保留；三者沿 axis=0 拼接 → `[1,2,3,4,10,20]`。`1:5:4j` 的步长是虚数 4j，走 `linspace(1,5,num=4)`（含端点）→ `[1., 2.333, 3.667, 5.]`。依据是 [AxisConcatenator.__getitem__](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/lib/_index_tricks_impl.py#L373-L377)。

**练习 2**：`np.c_[[1,2,3], [4,5,6]]` 的形状是什么？为什么 1-D 数组变成了列？

**参考答案**：形状 `(3, 2)`，结果 `[[1,4],[2,5],[3,6]]`。因为 `CClass` 用 `ndmin=2, trans1d=0` 初始化（[CClass.__init__](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/lib/_index_tricks_impl.py#L583-L584)），1-D 数组先升到 2-D 并把那一维放到第 0 轴（成为列），再沿最后一轴（`axis=-1`）拼接。

**练习 3**：`np.s_[2::2]` 与 `np.index_exp[2::2]` 的返回值有何不同？各自适合什么场景？

**参考答案**：`s_[2::2]` 返回 `slice(2, None, 2)`（单个 slice 对象），`index_exp[2::2]` 返回 `(slice(2, None, 2),)`（单元素元组）。`s_` 适合拼接到已有索引里（如 `a[1, *s_[2::2]]`），`index_exp` 适合直接作为完整索引元组传递或拼接（如 `idx = np.index_exp[2::2] + np.index_exp[0]`）。依据 [IndexExpression.__getitem__](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/lib/_index_tricks_impl.py#L773-L777)。

---

### 4.5 花式索引赋值的缓冲搬运与错误传播（#31975）

#### 4.5.1 概念说明

前面四节我们几乎只看「取数」(`arr[key]`)。但 `arr[key] = val`（赋值）走的是**同一个** `prepare_index` 解析、**同一套** `HAS_*` 分流，只是最终落到映射协议的另一半——`mp_ass_subscript` 槽位接的 `array_assign_subscript`。赋值与取数共享索引解析，但「搬运数据」这一步的实现在不同的 C 函数里：

- 取数：`mapiter_get`（把原数组的元素搬进新结果数组）。
- 赋值：`mapiter_set`（把 `val` 的元素搬进原数组的对应位置）。

这两个函数其实由**同一个模板** `mapiter_@name@` 生成——`@name@` 在构建期展开成 `get` 或 `set`，模板内部用 `@isget@` 开关切换「源/目标谁是谁」。这样新增或维护一处搬运逻辑，就能同时服务取数与赋值。

赋值比取数多一个关键能力：**类型转换**。当你写 `dst_array[index] = val`，而 `val` 的元素类型不能直接放进 `dst_array`（例如把 `object` 数组赋给 `int` 数组），NumPy 会在搬运时做一次逐元素转换。这个转换可能**失败**——比如把 Python 的 `None` 转成整数。正确行为应当是：转换失败 → 抛出异常、整个赋值中止。而 PR [#31975](https://github.com/numpy/numpy/pull/31975) 修复的，正是这条「失败 → 抛异常」的链路在一个边界情况下被**静默吞掉**的 bug（gh-31974）。

#### 4.5.2 核心流程

`mapiter_set` 内部分两条搬运路径（由 `mit->subspace_iter` 是否为空决定）：

1. **缓冲逐元素路径（`subspace_iter == NULL`）**：用于「逐个下标搬一个元素」的情形（如 `a[idx1d] = vals`，没有剩余子空间）。因为可能要做类型转换，这条路径开启**迭代器缓冲**：搬运被切成若干个缓冲区块（默认每块 8192 个元素），每个块在 `mit->outer_next(mit->outer)` 刷新时把数据搬进缓冲区、转换好、再供循环体取用。**#31975 的 bug 就在这条路径里。**
2. **嵌套子空间路径**：用于「下标选的是子块」的情形（如 `a[[0,1], :] = vals`）。这条路径每轮迭代后已有现成的 `if (needs_api && PyErr_Occurred()) return -1;` 检查。

缓冲路径的循环骨架（赋值侧）长这样：

```
do {
    if (needs_api && PyErr_Occurred())    // ① 检查「上一轮」刷新是否留下错误
        return -1;
    count = *counter;
    while (count--) {
        ...
        if (cast_info->func(...) < 0)      // ② 单元素转换失败，立即返回 -1
            return -1;
    }
} while (mit->outer_next(mit->outer));     // ③ 刷新下一个缓冲区块（转换在此发生，可能设置错误）
```

问题出在 ① 的位置：它在 `do` 循环体的**顶部**，只能捕获「上一轮 ③ 刷新」遗留的错误。如果出错的那次刷新恰好是**最后一轮**——`outer_next` 设置了错误、又返回「迭代结束」（假值）——循环就此退出，① 再也不会被执行，错误就这么悬着；函数随后一路落到末尾的 `return 0`，把「成功」报告给了调用方。

为什么「只有数组超过一个缓冲区块」才会触发？因为：第一块在迭代器 `reset` 时就填好了（即便它出错，循环体第一次进入时 ① 还会检查到）；只有当数据量超过单块、需要中途再次 `outer_next` 刷新时，才可能出现「最后一次刷新既失败、又返回结束」的窗口。这正是回归测试取 `N = 20000`（> 8192）的原因。

**修复**：在 `mapiter_@name@` 的两个分支汇合处、紧挨着 `return 0` 之前，加一道**无条件**的兜底检查：

```
/* Check if the above iteration ended with an error */
if (PyErr_Occurred()) {
    return -1;
}
return 0;
```

注意它**不**带 `needs_api` 前置条件——只要函数返回时还有任何挂起的错误，就一律转成 `-1` 失败返回。调用方 `array_assign_subscript` 见到 `result < 0` 才会走 `goto fail`，把异常真正抛出。

#### 4.5.3 源码精读

先看模板函数签名与返回值约定（成功 0、失败 -1）：

[numpy/_core/src/multiarray/lowlevel_strided_loops.c.src:1859-1862](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/src/multiarray/lowlevel_strided_loops.c.src#L1859-L1862) — `mapiter_@name@` 接收 `mit`、`cast_info`、`flags`、`is_aligned`；`@name@` 在构建期展开为 `get`/`set`，故这一个模板同时定义了取数与赋值两套搬运。

缓冲逐元素路径的入口与注释：

[numpy/_core/src/multiarray/lowlevel_strided_loops.c.src:1895-1902](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/src/multiarray/lowlevel_strided_loops.c.src#L1895-L1902) — `mit->subspace_iter == NULL` 分支，注释点明 “Item by item copy situation, the operand is buffered so use a cast to copy”，并按需补上 `needs_api`。这就是 #31975 bug 所在的路径。

循环体顶部的「上一轮错误」检查（仅赋值侧、仅 `needs_api` 时生效）：

[numpy/_core/src/multiarray/lowlevel_strided_loops.c.src:1940-1949](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/src/multiarray/lowlevel_strided_loops.c.src#L1940-L1949) — `#if !@isget@` 守卫下的 `if (needs_api && PyErr_Occurred()) return -1;`，位于 `do` 顶部。它捕获的是**上一次** `outer_next` 刷新遗留的错误，捕获不到「最后一次刷新即结束」的情形。

循环体里逐元素转换、失败立即返回：

[numpy/_core/src/multiarray/lowlevel_strided_loops.c.src:1998-2004](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/src/multiarray/lowlevel_strided_loops.c.src#L1998-L2004) — 赋值侧转换调用 `cast_info->func(...)` 返回 `< 0` 时 `NPY_END_THREADS; return -1;`。注意参数顺序 `{outer_ptrs[i], self_ptr}`（源在前、目标在后）；取数侧在 [1982-1983](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/src/multiarray/lowlevel_strided_loops.c.src#L1982-L1983)，顺序相反。

刷新下一个缓冲区块（循环条件）：

[numpy/_core/src/multiarray/lowlevel_strided_loops.c.src:2010](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/src/multiarray/lowlevel_strided_loops.c.src#L2010) — `} while (mit->outer_next(mit->outer));`。这一句既推进外层迭代、又刷新缓冲区；类型转换就在刷新时发生。当它返回假值（迭代结束）的同时挂着一个错误时，循环退出，顶部检查 ① 便够不着了。

对照：嵌套子空间路径在每轮迭代后也有同样的错误检查：

[numpy/_core/src/multiarray/lowlevel_strided_loops.c.src:2125-2127](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/src/multiarray/lowlevel_strided_loops.c.src#L2125-L2127) — `if (needs_api && PyErr_Occurred()) return -1;`。它位于 `do` 循环体内部、每次子空间搬运之前，但同样受 `needs_api` 门控，覆盖不到「最后一次 `outer_next` 刷新失败」的边角。

**本次修复的核心**——两个分支汇合处、`return 0` 之前的兜底检查：

[numpy/_core/src/multiarray/lowlevel_strided_loops.c.src:2158-2161](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/src/multiarray/lowlevel_strided_loops.c.src#L2158-L2161) — `/* Check if the above iteration ended with an error */` 紧跟 `if (PyErr_Occurred()) { return -1; }`。这道检查**无条件**（不带 `needs_api`），同时覆盖缓冲路径与子空间路径，堵住了「最后一次刷新失败却被 `return 0` 吞掉」的窗口。

最后看调用方如何利用这个返回值。赋值侧的调用点：

[numpy/_core/src/multiarray/mapping.c:2175-2178](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/src/multiarray/mapping.c#L2175-L2178) — `int result = mapiter_set(mit, &cast_info, meth_flags, is_aligned); if (result < 0) { goto fail; }`。修复前 `mapiter_set` 会带着挂起错误返回 0，于是 `result < 0` 为假、`goto fail` 被跳过、异常永不抛出；修复后返回 -1，`goto fail` 正常执行、异常被抛出。取数侧的对应调用在 [mapping.c:1723](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/src/multiarray/mapping.c#L1723)。

回归测试（把 gh-31974 钉死）：

[numpy/_core/tests/test_indexing.py:780-790](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/tests/test_indexing.py#L780-L790) — `test_fancy_assign_buffered_cast_error`：`N = 20000` 的 `int` 目标数组，把一个 `object` 值数组（第 9000 位塞 `None`）按 `np.arange(N)` 做花式索引赋值，断言抛出 `TypeError`。`9000` 落在第二个缓冲区块内（8192–16383），正好命中「中途刷新失败」的窗口。

#### 4.5.4 代码实践

**目标**：亲手复现 gh-31974，确认类型转换错误现在会被抛出而不是被吞掉。

**步骤**：

```python
import numpy as np

N = 20000
dst = np.zeros(N, dtype=int)
vals = np.arange(N, dtype=object)   # 全是 int 的 object 数组
vals[9000] = None                   # 在第二个缓冲区块里埋一个无法转 int 的值

# 花式索引赋值：等价于逐元素 object -> int 转换搬运
dst[np.arange(N)] = vals
```

**需要观察的现象**：在**已包含 #31975 修复**的 NumPy（HEAD `4e7f3b33` 及之后）上，`dst[np.arange(N)] = vals` 应抛出 `TypeError`（`None` 无法转 `int`）。把 `N` 改成远小于缓冲区块（如 `N = 100`，并把坏值放到任意位），由于数据不跨缓冲区块、循环体顶部检查 ① 就能抓到，修复前后都会抛错——所以这个 bug 的关键就在「超过一个缓冲区块」这一边界。

**预期结果**：

- 修复后的 NumPy：抛出 `TypeError`。
- 修复前的 NumPy（PR #31975 之前）：赋值「成功」返回、无异常，`dst` 里第 9000 位附近保持 0，而一个挂起的错误被留在解释器状态里，可能在后续任意 Python 调用中突然冒出来。

> 待本地验证：可用 `python -c "import numpy; print(numpy.__version__)"` 核对版本，或直接跑官方测试 `python -m pytest "numpy/_core/tests/test_indexing.py::TestMultiindexing::test_fancy_assign_buffered_cast_error"` 确认在当前构建上通过。

#### 4.5.5 小练习与答案

**练习 1**：为什么测试要把 `N` 取到 `20000`、把坏值放在 `9000`，而不是放在第 0 位？

**参考答案**：迭代器缓冲区默认 `NPY_BUFSIZE = 8192`。第 0 位落在**第一块**（0–8191），这一块在迭代器 `reset` 时就填好，错误会在循环体第一次进入时被顶部检查 ①（[lowlevel_strided_loops.c.src:1940-1949](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/src/multiarray/lowlevel_strided_loops.c.src#L1940-L1949)）捕获并正常抛出，复现不了 bug。`9000` 落在**第二块**（8192–16383），需要中途 `outer_next` 刷新才会碰到——这正是「最后一次刷新失败却被 `return 0` 吞掉」的窗口，故必须让数据超过一个缓冲区块。

**练习 2**：修复加的兜底检查为什么**不**带 `needs_api` 门控，而 ① 处的检查要带？

**参考答案**：① 处的检查在每轮循环顶部执行，带 `needs_api` 是为了在不涉及 Python API（纯数值搬运）时省掉 `PyErr_Occurred()` 的开销。但函数末尾的兜底检查（[lowlevel_strided_loops.c.src:2158-2161](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/src/multiarray/lowlevel_strided_loops.c.src#L2158-L2161)）是**每个函数仅执行一次**的安全网，目的是「绝不让一个挂着错误的函数谎报成功」——这种正确性保证比省一次 `PyErr_Occurred()` 重要得多，所以它无条件检查。

**练习 3**：`mapiter_get` 与 `mapiter_set` 为什么能共用一个 `.c.src` 模板？`@isget@` 在源码里控制了哪两处「方向相反」的行为？

**参考答案**：因为取数与赋值的搬运结构完全对称——都是「沿花式下标迭代、对每个位置搬一个元素」——只有「源与目标的指针方向相反」「循环顶部是否需要额外错误检查」这两点不同。模板用 `@isget@` 开关切换：转换调用的参数顺序（赋值侧 `{outer_ptrs[i], self_ptr}` 源在前，[lowlevel_strided_loops.c.src:1998](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/src/multiarray/lowlevel_strided_loops.c.src#L1998)；取数侧 `{self_ptr, outer_ptrs[i]}`，[1982](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/src/multiarray/lowlevel_strided_loops.c.src#L1982)），以及 ① 处的「上一轮错误」检查只在赋值侧（`#if !@isget@`）出现。一份模板、两套实例，避免重复维护。

---

## 5. 综合实践

**任务**：构造一个 5×5 数组，分别用基础切片、布尔掩码、整数数组取出行与列，并用 `np.shares_memory` 判断每个结果是视图还是拷贝，最后把结论与 C 层分支对应起来。

```python
import numpy as np
a = np.arange(25, dtype=np.int64).reshape(5, 5)

# A. 基础切片：取第 1~2 行、偶数列 → 视图
view_rc = a[1:3, ::2]
print("A 切片   ", view_rc.shape, "shares=", np.shares_memory(a, view_rc))

# B. 整组布尔掩码（形状与 a 相同）→ HAS_BOOL，1-D 拷贝
mask = (a % 6 == 0)
bool_all = a[mask]
print("B 整组布尔", bool_all.shape, "shares=", np.shares_memory(a, bool_all))

# C. 布尔数组只选行（第一维）→ HAS_FANCY，保留列维的拷贝
row_sel = np.array([True, False, True, False, True])
bool_row = a[row_sel, :]
print("C 行布尔 ", bool_row.shape, "shares=", np.shares_memory(a, bool_row))

# D. 整数数组选行（含重复）→ HAS_FANCY，拷贝
int_row = a[[0, 2, 0, 4]]
print("D 整数行 ", int_row.shape, "shares=", np.shares_memory(a, int_row))

# E. ix_ 笛卡尔积选行×列 → HAS_FANCY，拷贝
cart = a[np.ix_([0, 2], [1, 3, 4])]
print("E ix_    ", cart.shape, "shares=", np.shares_memory(a, cart))
```

**预期输出与对应分支**：

| 用例 | 形状 | shares_memory | C 层分支 |
| --- | --- | --- | --- |
| A 基础切片 | (2, 2) | True | `get_view_from_index`（视图） |
| B 整组布尔 | (n,) | False | `array_boolean_subscript`（1-D 拷贝） |
| C 行布尔 | (3, 5) | False | `PyArray_MapIterNew`（花式拷贝） |
| D 整数行 | (4, 5) | False | `mapiter_trivial_get` 快速通道或 `MapIter` |
| E ix_ 笛卡尔积 | (2, 3) | False | `PyArray_MapIterNew`（多花式广播） |

**进阶验证**：对 A 修改 `view_rc[0, 0] = -1`，检查 `a[1, 0]` 是否也变（应变，视图）；对其余四个做同样修改不应影响 `a`（拷贝）。再用 `view_rc.base is a` 进一步确认视图的 `base` 指向原数组（对应 [mapping.c:932-944](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/src/multiarray/mapping.c#L932-L944) 把 `self` 设为 base）。

**进阶验证 ②（#31975 缓冲赋值错误传播）**：把上面的「视图 vs 拷贝」结论再推进一步——验证花式索引**赋值**在跨缓冲区块时的类型转换错误能被抛出：

```python
N = 20000
dst = np.zeros(N, dtype=int)
vals = np.arange(N, dtype=object)
vals[9000] = None          # 第二个缓冲区块内埋一个无法转 int 的值
try:
    dst[np.arange(N)] = vals
    print("未抛异常 → 该 NumPy 早于 #31975")
except TypeError as e:
    print("抛出 TypeError → #31975 已生效：", type(e).__name__)
```

预期：在 HEAD `4e7f3b33` 及之后的 NumPy 上抛出 `TypeError`（对应 4.5 节 [lowlevel_strided_loops.c.src:2158-2161](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/src/multiarray/lowlevel_strided_loops.c.src#L2158-L2161) 的兜底检查 → [mapping.c:2175-2178](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/src/multiarray/mapping.c#L2175-L2178) 的 `goto fail`）。把 `N` 改成 `100`（不跨缓冲区块），修复前后都会抛错——这正是该 bug 仅在「超过一个缓冲区块」时才出现的原因。

> 若本地 NumPy 未从源码构建，以上行为在任意正式发布版均成立，可直接用 `pip install numpy` 验证。

## 6. 本讲小结

- 所有 `arr[key]` 都从同一个 C 入口 `array_subscript` 进来，它先用 `prepare_index` 把 `key` 解析成 `npy_index_info[]` 并返回一个 `HAS_*` 位掩码，再按掩码分流。
- **基础索引**（整数/切片/省略号/`None`）走 `get_view_from_index`：只改写 `shape`/`strides`/`data` 指针，结果**是视图**；纯整数索引走 `get_item_pointer` 返回标量。
- **花式索引**（整数数组、非整组布尔）走 `PyArray_MapIterNew` + `mapiter_get`：新分配内存逐元素搬运，结果**是拷贝**；单个一维整数数组有 `mapiter_trivial_get` 快速通道。
- **赋值** `arr[key] = val` 共用同一套 `prepare_index` 解析，最终落到 `array_assign_subscript` → `mapiter_set`（与 `mapiter_get` 同源于 `mapiter_@name@` 模板）。当需要类型转换、且数据超过一个缓冲区块时，PR #31975 在 `mapiter_set` 末尾加了无条件 `if (PyErr_Occurred()) return -1;` 兜底，确保「缓冲刷新时的转换错误」不再被 `return 0` 静默吞掉，而是经调用方 `goto fail` 正确抛出。
- **整组布尔掩码**（形状与被索引数组相同）是特例，走 `array_boolean_subscript`，结果**是 1-D 拷贝**。
- 连续的高级索引在结果中保留位置、分离的高级索引被移到最前，由 `PyArray_MapIterSwapAxes` + `consec` 字段决定。
- `np.ix_` 用 `(1,..,n,..,1)` 的开放网格制造笛卡尔积；`np.r_`/`np.c_` 是基于 `__getitem__` 的数组构造器；`np.s_`/`np.index_exp` 把切片语法转成可复用的 `slice` 对象。

## 7. 下一步学习建议

- 下一讲 [u3-l2 形状操作：reshape、transpose 与轴](u3-l2-shape-manipulation.md) 会继续在“只改视图不改数据”的主线上展开，把 `reshape`/`transpose`/`swapaxes` 的 `strides` 操作与本讲的 `get_view_from_index` 串联起来。
- 想深入“视图何时被迫变成拷贝”的边界，可阅读 [numpy/_core/tests/test_indexing.py](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/tests/test_indexing.py) 中的 `test_everything_returns_views`、`test_ellipsis_index`、`test_boolean_indexing_onedim`，它们用断言把视图/拷贝行为钉死。
- 对赋值侧（`arr[key] = val`）感兴趣的话，可顺着 `array_as_mapping.mp_ass_subscript` → `array_assign_subscript`（[mapping.c:1832](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/src/multiarray/mapping.c#L1832)）阅读，它和取数共享同一套 `prepare_index` 解析；赋值的逐元素搬运在 `mapiter_set`，即本讲 4.5 节深入剖析的 [lowlevel_strided_loops.c.src](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/src/multiarray/lowlevel_strided_loops.c.src#L1859-L1862) 模板。
- 想确认 PR #31975 的行为，可运行回归测试 [test_indexing.py 的 `test_fancy_assign_buffered_cast_error`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/tests/test_indexing.py#L780-L790)，它把「缓冲刷新失败」这一边界钉死。
- 后续 [u3-l3 视图、拷贝与 strides 技巧](u3-l3-views-copies-strides.md) 与 [u3-l4 广播机制原理与实现](u3-l4-broadcasting.md) 会把本讲的 strides/视图概念推向 `as_strided` 与广播（stride=0）。
