# 测试组织、数据类型矩阵与 C API 测试

> 本讲对应单元 u7（专家：后端委托与测试）的第 2 篇，承接 u6-l1（`_nd_image` 扩展模块与方法分发表）。
> 本讲在增量版本 `ce1f6477`（相对 `de190e7f`）下首次建立，新增了 `tests/test_array_likes.py`。

## 1. 本讲目标

学完本讲，你应当能够：

- 说出 `scipy/ndimage/tests/` 下每个 `test_*.py` 对应哪个功能域，以及测试规模如何分布；
- 理解 `test_datatypes.py` 如何用「dtype × dtype × order」的笛卡尔积矩阵覆盖插值函数的全部数值类型组合；
- 读懂本次新增的 `test_array_likes.py`，说清它如何用 `_assert_same_result` 把「list 输入」与「显式 `ndarray` 输入」的结果逐项比对，做一次跨全公开 API 的输入类型回归；
- 解释 `test_c_api.py` 如何用 `LowLevelCallable` 的四种变体对扩展模块做低层回调测试；
- 跟踪 `utils/generate_label_testvectors.py` 与 `tests/data/*.txt` 如何把「输入 / 结构元 / 期望结果」串成一条冻结的回归数据链。

本讲的总体观点是：**ndimage 的测试不是一堆杂乱的断言，而是一套分层的策略**——按功能域分文件、按数据类型做矩阵、按输入类型做冒烟、按回调入口测 C 边界、按冻结向量防回归。理解这套策略，比记住某一条断言更重要，因为它告诉你「该去哪里写新测试」「该用哪种既有机制」。

## 2. 前置知识

阅读本讲前，建议你已经了解：

- **pytest 的参数化与标记**：`@pytest.mark.parametrize`、`xfail`、自定义 marker。本讲多处用它们做矩阵展开和已知失败标注。
- **数组 API（array API）测试基础设施**：`scipy._lib._array_api` 提供的 `xp_assert_close`、`make_xp_test_case`、`skip_xp_backends(np_only=True)` 等。这是让同一份测试在 NumPy / CuPy / JAX 三个后端上跑同一套断言的「胶水」。本讲的 `xp` 形参就来自 `make_xp_test_case` 装饰器——它把一个普通函数包装成按后端参数化的测试。这一点与 u7-l1（数组 API 后端委托）紧密相关：委托层让 cupy 数组能进 ndimage，而测试层则负责验证委托后行为一致。
- **`array_like` 的含义**：NumPy 里凡是能被 `np.asarray` 转成数组的对象（Python `list`、`tuple`、嵌套 list 等）都叫 array_like。本讲新增的测试正是验证「ndimage 的公开函数都接受 list 输入」。
- **`_nd_image` 扩展模块**（u6-l1）：C 内核只认真正的 `ndarray`，所有 array_like 转换发生在 Python 包装层。
- **样条预滤波与 order**（u3-l1）：order 0–5 决定插值阶数，本讲的 dtype 矩阵会遍历全部 order。

一个贯穿全讲的小约定：测试里用 `xp` 表示「当前后端的数组命名空间」——在纯 NumPy 跑时 `xp` 就是 `numpy`，在 cupy 后端跑时就是 `cupy`。而本讲新增的 `test_array_likes.py` 是 **numpy-only** 的，它压根不进后端委托层。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 角色 | 规模（测试函数数） |
| --- | --- | --- |
| `tests/meson.build` | 测试文件与数据文件的安装清单 | — |
| `tests/test_filters.py` | Filters 域测试（最大） | 177 |
| `tests/test_interpolation.py` | Interpolation 域测试 | 114 |
| `tests/test_measurements.py` | Measurements 域测试（含数据向量消费） | 124 |
| `tests/test_morphology.py` | Morphology 域测试 | 153 |
| `tests/test_fourier.py` | Fourier filters 域测试 | 11 |
| `tests/test_datatypes.py` | 跨函数的 dtype 覆盖矩阵 | 2 |
| `tests/test_array_likes.py` | **本次新增**：全公开 API 的 array_like 冒烟回归 | 74 |
| `tests/test_c_api.py` | 扩展模块回调（`LowLevelCallable`）低层测试 | 3 |
| `tests/test_ni_support.py` | 共享支撑工具 `_get_output` 测试 | 3 |
| `tests/test_splines.py` | 样条滤波对矩阵解的回归测试 | 2 |
| `utils/generate_label_testvectors.py` | 生成 `data/` 下回归向量的脚本 | — |
| `tests/data/*.txt` + `tests/data/README.txt` | 冻结的 label 回归向量与说明 | — |

> 规模数字来自对每个文件统计 `def test` 的出现次数，仅作量级参考（部分测试经过 `@pytest.mark.parametrize` 还会展开成更多用例）。

一句话概括分工：**五大功能域各有一个 `test_*.py`（filters/interpolation/measurements/morphology/fourier），三个横切测试覆盖跨域性质（datatypes 看类型矩阵、array_likes 看输入类型、c_api 看 C 回调），两个支撑测试看共享工具（ni_support/splines），再加一套冻结向量（data/）保护 `label` 的回归。**

## 4. 核心概念与源码讲解

### 4.1 按功能域分文件：测试如何对应源码模块

#### 4.1.1 概念说明

ndimage 的测试目录遵循一个朴素但重要的原则：**一个功能域 = 一个 `test_*.py`**。这与源码侧的「一个功能域 = 一个 `_*.py` 包装文件」一一对应。这种对应不是巧合——它让「改了 `_filters.py` 就去 `test_filters.py` 加用例」成为肌肉记忆，也让你能从测试文件名直接反推它在测哪个模块。

除此之外，还有三类**横切测试**不绑定单一功能域：
- `test_datatypes.py`：跨多个插值函数，只关心 dtype 是否被正确接受；
- `test_array_likes.py`：跨**全部**公开 API，只关心「能不能吃 list」；
- `test_c_api.py`：只关心 C 扩展的 `LowLevelCallable` 回调入口。

最后，构建脚本 `tests/meson.build` 是这份文件清单的「单一事实来源」——它决定哪些文件会被安装、被收集。

#### 4.1.2 核心流程

测试文件的「注册」流程：

1. 开发者新建一个 `tests/test_foo.py`；
2. 把文件名加入 `tests/meson.build` 的 `python_sources` 列表；
3. `py3.install_sources` 把这些文件安装到安装目录的 `scipy/ndimage/tests/` 下（`install_tag: 'tests'` 标明只随测试安装包分发）；
4. pytest 收集时按 `test_*.py` 的命名约定自动发现其中的 `test_*` 函数。

数据文件（`data/*.txt`）走单独的第二个 `py3.install_sources` 块，安装到 `scipy/ndimage/tests/data/`。

#### 4.1.3 源码精读

`tests/meson.build` 的文件清单（本次新增 `test_array_likes.py`）：

[tests/meson.build:L1-L13](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/ndimage/tests/meson.build#L1-L13) — `python_sources` 列出了全部要安装的测试文件；本次提交把 `'test_array_likes.py'` 按字母序插入到 `'dots.png'` 与 `'test_c_api.py'` 之间，并把列表末尾补了一个逗号（仅是格式清理）。

数据文件的单独安装块：

[tests/meson.build:L15-L23](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/ndimage/tests/meson.build#L15-L23) — 三个 `label_*.txt` 被安装到 `data/` 子目录，供 `test_measurements.py` 的 `test_label_structuring_elements` 加载（见 4.5）。

横切测试的存在可以从文件名一眼读出意图：`test_datatypes`（类型）、`test_array_likes`（输入形态）、`test_c_api`（C 边界）——它们的名字描述的是「被测的性质」，而不是「被测的模块」。

#### 4.1.4 代码实践

1. **目标**：建立「改哪个模块 → 去哪个测试文件」的对应表。
2. **步骤**：
   - 打开 `tests/` 目录，把 10 个 `test_*.py` 与 u1 讲过的五大功能域对号入座；
   - 在 `tests/meson.build` 的 `python_sources` 里找到每个文件名，确认它已被注册；
   - 注意三个横切文件（`test_datatypes` / `test_array_likes` / `test_c_api`）不对应单一功能域。
3. **需要观察的现象**：功能域文件按字母序排列；`dots.png`（一张测试图）也在清单里，因为它被 `test_*` 当作输入数据加载。
4. **预期结果**：你能不看源码说出「测形态学去 `test_morphology.py`、测傅里叶去 `test_fourier.py`、测 list 输入去 `test_array_likes.py`」。
5. 待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：如果你新增了一个 `test_myfeature.py`，忘记改 `meson.build` 会怎样？
**答案**：本地开发时 pytest 仍能发现并运行它（因为收集是按文件名约定），但**安装后的 scipy 包里不会有这个文件**——只有写进 `python_sources` 的文件才被 `py3.install_sources` 安装。所以 CI 里跑「安装后再测试」时会漏掉它。这就是为什么本次提交在新增 `test_array_likes.py` 时必须同步改 `meson.build`。

**练习 2**：为什么 `dots.png` 出现在 `python_sources` 里？
**答案**：它是测试输入数据（一张点状图案图），被若干 `test_*` 函数加载。Meson 把它和 `.py` 一并安装，确保安装环境里测试能找到该图片。

---

### 4.2 dtype 覆盖矩阵：test_datatypes 的笛卡尔积

#### 4.2.1 概念说明

C 内核对每种数据类型往往有不同的代码路径（整型走整型分支、浮点走浮点分支、`order` 不同走不同的样条系数函数）。如果只测一两种 dtype，很容易漏掉「uint64 在某平台精度丢失」「float32 边界行为不同」之类的 bug。`test_datatypes.py` 的策略是**用笛卡尔积把数据 dtype、坐标 dtype、样条阶数三个维度全部组合一遍**，确保插值相关函数对 12 种数值类型都正确。

#### 4.2.2 核心流程

矩阵的构造是一个三层嵌套循环：

```
dts = (uint8, uint16, uint32, uint64,
       int8, int16, int32, int64,
       intp, uintp, float32, float64)        # 12 种

for order in range(0, 6):                     # 样条阶数 0..5
    for data_dt in dts:                        # 数据 dtype（12）
        for coord_dt in dts:                   # 坐标 dtype（12）
            用 (data_dt, coord_dt) 跑 affine_transform / map_coordinates
        用 (data_dt) 跑 shift / zoom
```

组合数为：

\[
\text{用例数} = \underbrace{6}_{\text{order}} \times \underbrace{12}_{\text{data\_dt}} \times \underbrace{12}_{\text{coord\_dt}} = 864
\]

每个组合内部至少做 4 次插值调用（affine + 两次 map_coordinates + 后续 shift/zoom），合计数千次断言。这种「暴力但完备」的矩阵正是为了堵住「某个 dtype × 某个 order」组合下的边角 bug。

#### 4.2.3 源码精读

矩阵的主体：

[tests/test_datatypes.py:L11-L44](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/ndimage/tests/test_datatypes.py#L11-L44) — 定义 `dts` 元组后，三层循环把固定数据矩阵 `data`（3×4）依次 `.astype(data_dt)`，再用 `coord_dt` 构造坐标；对每组坐标做「平移 1 的 map_coordinates 应得到 `shifted_data`」「越界 10 的 map_coordinates 应得到全 0（constant 填充）」两条断言；循环外再用 `shift` / `zoom` 复核。

已知平台失败的 `xfail` 用例：

[tests/test_datatypes.py:L47-L67](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/ndimage/tests/test_datatypes.py#L47-L67) — `@pytest.mark.xfail(True, reason="Broken on many platforms")` 主动声明「uint64 最大值插值在 win32 / s390x / arm64 上会失败」。这是 ndimage 测试策略的另一面：**对已知不可修的环境相关 bug，用 `xfail` 显式登记**，而不是默默删掉测试或任其红屏。注释里详细记录了失败原因（32 位 VC 编译器把 uint64→double 当作有符号处理）和「最后通过的 macOS 也开始挂」的历史。

#### 4.2.4 代码实践

1. **目标**：体会笛卡尔积矩阵的覆盖力度。
2. **步骤**：
   - 读 `dts` 元组，确认它覆盖了全部整型（含 `intp`/`uintp`）和两种浮点；
   - 在 `for coord_dt in dts` 这一行打断点或加 `print(order, data_dt, coord_dt)`，估算组合数；
   - 注意 `coords_m1 = idx.astype(coord_dt) - 1` 会让坐标变成负数，这正好检验有符号/无符号坐标类型在插值里的行为差异。
3. **需要观察的现象**：无符号整型坐标减 1 后会「绕回」成巨大正数吗？不会——`map_coordinates` 内部会把坐标转成浮点，所以 `uint8` 的 0−1 在转 double 后仍是 −1，越界走 constant 填充得到 0。
4. **预期结果**：所有 864 个组合（除 `xfail` 的 uint64 边界）都应通过；理解到「矩阵测试的价值在于覆盖，而非精巧」。
5. 待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么矩阵里既有 `data_dt` 又有 `coord_dt`，要分别遍历？
**答案**：因为插值函数有两类数值输入——被插值的**数据**和插值位置的**坐标**，它们走 C 内核里不同的类型转换路径。只测数据 dtype 会漏掉「坐标类型转换」的 bug（例如坐标被错误地截断）。

**练习 2**：`@pytest.mark.xfail(True, ...)` 的第一个参数 `True` 是什么意思？
**答案**：它表示「无条件预期失败」（等价于 `@pytest.mark.xfail`），即这个用例在所有平台都被预期为失败；若它意外通过，pytest 会标为 `XPASS`。用它来登记「我们知道它坏、但短期内无法跨平台修复」的用例，避免污染 CI。

---

### 4.3 array_like 输入冒烟回归：test_array_likes（本次新增）

#### 4.3.1 概念说明

本次增量（PR #25554，提交 `ce1f6477`）新增了 `tests/test_array_likes.py`。它的目标非常聚焦：**验证 ndimage 的每一个公开函数都接受 Python `list`（即 array_like）作为输入，且结果与传 `ndarray` 完全一致。**

为什么需要它？因为 C 内核（u6-l1）只认真正的 `ndarray`，所有 array_like → ndarray 的转换都发生在 Python 包装层。如果某个函数的包装层漏了 `np.asarray` 调用（或委托层在非 numpy 后端下转换逻辑出错），传 list 就会报 `AttributeError` 或给出错误结果。这个文件用 74 个几乎同构的「冒烟用例」把整面公开 API 的输入转换路径一次性守住——所以叫「冒烟（smoke）测试」：它不深究算法正确性，只求「能跑通、且与数组输入等价」。

> 文件顶部明确写明：`All cases here are numpy-only: a list's namespace is numpy.` 也就是说它**不经过 u7-l1 的 CuPy/JAX 委托层**，因为 list 的命名空间恒为 numpy。它测的是「裸 NumPy 实现路径」对 list 的接受度。

#### 4.3.2 核心流程

每个冒烟用例的套路是固定的「双调对比」：

```
1. 构造 list 形式的输入：input_list = [1, 2, 3, 4]
2. 构造对应的 ndarray：input_array = np.asarray(input_list)
3. 用 list 调函数：        result   = ndimage.foo(input_list, ...)
4. 用 array 调同一函数：   expected = ndimage.foo(input_array, ...)
5. 断言两者逐项一致：     _assert_same_result(result, expected)
```

关键设计点：

- **非数组参数的处理分两类**：本身就是数组语义的参数（如 `weights`、`coordinates`、`matrix`、`labels`、`index`）也要各备一份 list 与 array 两个版本分别传入；而非数组参数（如 `sigma`、`size`、`rank`、`shift`、`function` 回调）只需传一次、两次调用共用同一个值。
- **结果比对是多态的**：不同函数返回类型不同（数组、`(数组, 数)` 元组、`find_objects` 的 slice 列表、`value_indices` 的字典、`*_position` 的坐标元组），所以不能简单用 `np.allclose`，而要用递归的 `_assert_same_result`。

#### 4.3.3 源码精读

文件头与模块说明：

[tests/test_array_likes.py:L1-L8](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/ndimage/tests/test_array_likes.py#L1-L8) — 模块 docstring 点明「smoke test array_like inputs」与「numpy-only」两点；用 `xp_assert_close`（数组 API 断言助手）做最终数值比对。

核心比对器 `_assert_same_result`（递归多态比较）：

[tests/test_array_likes.py:L11-L40](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/ndimage/tests/test_array_likes.py#L11-L40) — 依次处理 `tuple`（如 `label` 的 `(labeled, num)`）、`list`（如 `find_objects` 返回的 slice 列表）、`dict`（如 `value_indices` 的 `{值: 坐标元组}`）、`slice`（`find_objects` 的元素）、`None`，逐层递归；叶子节点才落到 `xp_assert_close(result, expected, atol=1e-14)`。这个分发结构正是为了适配 ndimage 五花八门的返回形态——**它是「跨全 API 冒烟」能成立的基石**。

回调类参数的复用助手（非数组参数，两次调用共用）：

[tests/test_array_likes.py:L43-L63](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/ndimage/tests/test_array_likes.py#L43-L63) — `_generic_filter1d`、`_derivative`、`_derivative2`、`_vectorized_mean` 是几个 Python 回调，分别供 `generic_filter1d`、`generic_laplace`、`generic_gradient_magnitude`、`vectorized_filter` 使用。注意它们**不是数组参数**，所以两次调用传的是同一个函数对象（见下）。

一个最简的典型用例（`correlate1d`，weights 也双份）：

[tests/test_array_likes.py:L66-L73](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/ndimage/tests/test_array_likes.py#L66-L73) — `input_list` / `input_array` 与 `weights_list` / `weights_array` 各备一对，分别喂给 `correlate1d`，再断言结果一致。这是「数组位参数都双份」的标准写法。

非数组参数共用同一值的例子（`generic_laplace` 传同一个回调）：

[tests/test_array_likes.py:L120-L126](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/ndimage/tests/test_array_likes.py#L120-L126) — `derivative2 = _derivative2` 是一个函数对象，list 调用与 array 调用传的是**同一个** `derivative2`；只有 `input` 这一个数组位参数被双份化。这就回答了实践任务里的「非数组参数如何处理」：**回调、标量、size/sigma 等都不双份，只对数组位参数双份**。

一个特意只测单边输入的边界用例（`watershed_ift` 的 markers）：

[tests/test_array_likes.py:L548-L559](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/ndimage/tests/test_array_likes.py#L548-L559) — `watershed_ift` 要求 `input` 必须是 `uint8`/`uint16`（IFT 桶队列的设计约束，见 u4-l4），所以这里 `input` 直接用 `np.asarray(input_list, dtype=np.uint8)`（保持数组、不测 list），而**只把 `markers` 作为 list 来测**（`markers_list` 喂给 result、`markers_array` 喂给 expected）。这是全文件里少数「不把主输入也双份」的用例，体现了冒烟测试对函数契约差异的细致适配。

#### 4.3.4 代码实践

1. **目标**：理解「双调对比」模式与非数组参数的处理，并实际运行一个用例。
2. **步骤**：
   - 挑 `test_correlate1d_accepts_lists` 阅读其三段式（造 list/array → 双调 → 断言）；
   - 对比 `test_labeled_comprehension_accepts_lists`（4.3 节附近），看它如何把 `input`、`labels`、`index` 三个数组位参数**全部**双份化，而 `func`/`out_dtype`/`default` 只传一次；
   - 在本地运行：
     ```
     python -m pytest scipy/ndimage/tests/test_array_likes.py::test_correlate1d_accepts_lists -v
     ```
3. **需要观察的现象**：list 输入的结果与 ndarray 输入的结果数值完全一致（容差 `1e-14`）；如果故意把某个函数的包装层里的 `np.asarray` 删掉，对应的冒烟用例会立刻红。
4. **预期结果**：74 个用例全部通过；你能在脑中复述「哪些参数是数组位（要双份）、哪些不是（共用）」。
5. 待本地验证（命令是否可用取决于本地是否已编译安装 scipy）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `_assert_same_result` 要专门处理 `slice` 和 `dict`，而不是统一用 `np.allclose`？
**答案**：因为 `find_objects` 返回的是 `slice` 对象的列表（`slice` 不是数组，`np.allclose` 无法处理），而 `value_indices` 返回 `dict`。`_assert_same_result` 必须先按容器类型递归拆解，等递归到「真正的数组叶子」时才调 `xp_assert_close`。这是「跨全 API 冒烟」必须付出的多态代价。

**练习 2**：这个文件为什么不测 CuPy/JAX 后端的 list 输入？
**答案**：因为 list 的数组命名空间恒为 numpy（docstring 已点明），它根本不会触发 u7-l1 的委托层分派。要测 cupy/jax 后端，得传真正的 cupy/jax 数组——那是其它 `test_*.py`（用 `make_xp_test_case` 装饰）的职责，不是本文件的 numpy-only 冒烟范畴。

**练习 3**：`watershed_ift` 的用例为什么只把 markers 双份、不把 input 双份？
**答案**：`watershed_ift` 的 `input` 限定 `uint8`/`uint16`，用一个普通 int 嵌套 list 直接传会触发类型校验失败；为避免冒烟用例因「契约本身的要求」而非「array_like 转换 bug」失败，作者把 input 固定成 `np.asarray(..., dtype=np.uint8)`，仅验证 markers 这一个**可**为 list 的数组位参数。这体现了冒烟用例对函数契约的尊重。

---

### 4.4 C API 低层测试：test_c_api 与 LowLevelCallable

#### 4.4.1 概念说明

`generic_filter`、`generic_filter1d`、`geometric_transform` 这三个函数允许用户传一个**回调**（callback）来定义邻域运算或坐标映射（见 u2-l5、u3-l4）。回调一共有四种来源，性能与封装层次各不同：

1. 纯 Python 函数（最慢、最易写）；
2. C 函数指针（经 `scipy.ndimage._ctest` 暴露）；
3. Cython 函数（经 `scipy.ndimage._cytest` 暴露，无签名）；
4. `LowLevelCallable`（带签名，或由 capsule 构造，最快）。

`test_c_api.py` 的任务就是**把这四种回调的结果两两比对**，确保「无论用哪种回调入口，最终数值结果一致」。它本质上是在测 C 扩展层「回调桥接」的正确性，而非算法本身。

#### 4.4.2 核心流程

测试的套路是「参照实现 vs 候选实现」：

```
对每个候选回调 j（4 种之一）：
    res = ndimage.generic_filter(图像, 候选回调 j, footprint=...)
    std = ndimage.generic_filter(图像, 参照的 Python 实现, footprint=...)
    断言 res ≈ std
```

四种候选回调分别由 `FILTER1D_FUNCTIONS` / `FILTER2D_FUNCTIONS` / `TRANSFORM_FUNCTIONS` 三个列表工厂函数生成。`_ctest` / `_cytest` 是专门为测试而存在的最小 C/Cython 模块，只暴露最朴素的 filter1d/filter2d/transform 回调。

#### 4.4.3 源码精读

四种一维回调的工厂列表：

[tests/test_c_api.py:L9-L37](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/ndimage/tests/test_c_api.py#L9-L37) — `FILTER1D_FUNCTIONS` 含四项：`_ctest.filter1d`（C）、`_cytest.filter1d(with_signature=False)`（Cython 无签名）、`LowLevelCallable(_cytest.filter1d(with_signature=True))`（带签名）、`LowLevelCallable.from_cython(_cytest, "_filter1d", _cytest.filter1d_capsule(...))`（由 capsule 构造）。`FILTER2D_FUNCTIONS` / `TRANSFORM_FUNCTIONS` 结构相同。

`generic_filter1d` 的四入口比对：

[tests/test_c_api.py:L63-L84](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/ndimage/tests/test_c_api.py#L63-L84) — 内层 `filter1d` 是参照的纯 Python 实现（手动累加 `filter_size` 个邻元素求平均）；`check(j)` 用第 j 种候选回调跑一次，再与参照实现比对。外层 `for j, func in enumerate(FILTER1D_FUNCTIONS)` 把四种入口各跑一遍，`err_msg=f"#{j} failed"` 在失败时标出是第几种入口挂了。`test_generic_filter`（[L40-L60](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/ndimage/tests/test_c_api.py#L40-L60)）与 `test_geometric_transform`（[L87-L102](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/ndimage/tests/test_c_api.py#L87-L102)）是同样的模式。

#### 4.4.4 代码实践

1. **目标**：理解「参照实现 vs 候选入口」的比对思想。
2. **步骤**：
   - 读 `FILTER1D_FUNCTIONS`，把四种回调的来源（C / Cython / LowLevelCallable 两变体）列出来；
   - 读 `test_generic_filter1d`，找到「参照实现 `filter1d`」与「候选 `func`」的两次调用，确认它们用相同的 `im` 与 `filter_size`；
   - 思考：为什么不直接断言「候选回调结果等于某个手算值」，而是等于「参照 Python 实现的结果」？
3. **需要观察的现象**：四种入口的结果与参照实现逐一相等；若 capsule 构造方式写错，对应那个 `j` 会失败并报 `#j failed`。
4. **预期结果**：3 个测试（各跑 4 个入口）全过；理解到「C 回调测试的关键是消除入口差异、只比最终数值」。
5. 待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：`LowLevelCallable` 与普通 Python 函数回调相比，优势是什么？代价是什么？
**答案**：优势是性能——Python 回调每像素都要跨越 Python/C 边界并构造 Python 对象，而 `LowLevelCallable` 在 C 端直接调用，省去解释器开销，对大图快几个量级。代价是必须用 C/Cython 实现回调、并正确声明签名或提供 capsule，开发成本高得多。ndimage 同时支持两者，正是为了让用户在「易写」和「快」之间权衡。

**练习 2**：为什么参照实现 `filter1d` 要写成手动双层循环（L64-L69），而不是直接调 `np.mean`？
**答案**：因为参照实现必须严格匹配 C 回调的「逐元素、逐邻元」契约，包括「先清零、再累加、最后除以 filter_size」的顺序。用 `np.mean` 会引入不同的舍入/向量化路径，可能掩盖候选回调的细微 bug。参照实现越「笨」、越贴近回调语义，比对越有意义。

---

### 4.5 回归测试向量：generate_label_testvectors + data/

#### 4.5.1 概念说明

有些算法的正确性很难用「几行断言」讲清楚，最典型的是 `label`（连通区域标记，u4-l1）——同样的输入搭配几十种不同结构元，人工算期望结果既繁琐又易错。ndimage 的做法是**用「参考实现」一次性算出一大批期望结果，冻结成文本文件**，以后只要 `label` 的实现（特别是 Cython 内核 `_ni_label.pyx`）有改动，就拿这批冻结数据来回归。

`utils/generate_label_testvectors.py` 是生成这批数据的脚本，`tests/data/label_inputs.txt`、`label_strels.txt`、`label_results.txt` 是冻结的输出，`tests/test_measurements.py::test_label_structuring_elements` 是消费它们做回归的测试。`tests/data/README.txt` 点明了三者关系与生成脚本位置。

#### 4.5.2 核心流程

数据链的生命周期分「生成」与「消费」两阶段：

**生成阶段**（开发期手动运行一次）：

```
1. 准备 3 张 7×7 测试图（含全 1、两个对称图案）；
2. 枚举 8 个 3×3 结构元，再各做上下翻转、90° 旋转，去重后得到一批结构元；
3. 对「每张图 × 每个结构元」调用 label，收集 labeled 结果；
4. np.savetxt 把输入、结构元、结果分别写到三个 .txt。
```

**消费阶段**（每次 CI 跑测试）：

```
1. 从 data/ 读回三个 .txt，reshape 成 (n图, 7, 7) / (n结构元, 3, 3) / (n组合, 7, 7)；
2. 双重循环：对每张图 × 每个结构元，调当前 label，与冻结的 results[r] 逐元素比对；
3. 任一不符 → 回归失败。
```

这样，冻结数据成了「黄金标准」：只要重写 `_ni_label.pyx` 后某个组合的结果变了，测试立刻报错。

#### 4.5.3 源码精读

生成脚本的主体：

[utils/generate_label_testvectors.py:L5-L41](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/ndimage/utils/generate_label_testvectors.py#L5-L41) — `generate_test_vecs(infile, strelfile, resultfile)` 内：`data` 是 3 张 7×7 图（`bitimage` 把字符串网格转成布尔数组）；`strels` 先列 8 个种子结构元，再用列表拼接做 `np.flipud` 和 `np.rot90` 扩张（[L32-L33](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/ndimage/utils/generate_label_testvectors.py#L32-L33)），再用集合去重（`{t.astype(int).tobytes() for t in strels}`，[L34-L35](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/ndimage/utils/generate_label_testvectors.py#L34-L35)）；最后 `np.vstack` 拼接并用 `label(d, s)[0]`（取标记数组、丢掉 num）算全部期望结果，`np.savetxt` 落盘。

脚本入口（模块级直接执行）：

[utils/generate_label_testvectors.py:L44](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/ndimage/utils/generate_label_testvectors.py#L44) — 文件末尾直接调用 `generate_test_vecs("label_inputs.txt", "label_strels.txt", "label_results.txt")`。也就是说**这是一次性脚本**：import 即执行，生成三个文件；开发者只在工作目录手动跑一次，把产出拷进 `tests/data/`。

冻结数据与生成脚本的说明：

[tests/data/README.txt](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/ndimage/tests/data/README.txt) — 明确写「向量由 scipy 0.10.0 的 `ndimage.label` 生成，用于验证 Cython 版行为一致；生成脚本在 `../../utils/generate_label_testvectors.py`」。这条说明把「黄金标准来自哪个版本」也冻结了下来，便于将来追溯。

消费端的回归测试：

[tests/test_measurements.py:L395-L416](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/ndimage/tests/test_measurements.py#L395-L416) — `test_label_structuring_elements` 用 `np.loadtxt` 读三个 `.txt`，`reshape` 成三维数组，再用 `xp.asarray` 转成当前后端数组；双重循环 `for i in range(data.shape[0])` × `for j in range(strels.shape[0])`，对每个 `(图, 结构元)` 组合调 `ndimage.label(d, s)[0]`，与 `results[r]` 用 `xp_assert_equal(..., check_dtype=False)` 逐元素比对。注意 `check_dtype=False`——只比数值，不比 dtype，因为不同后端的标记 dtype 可能不同。

#### 4.5.4 代码实践

1. **目标**：跑通「生成 → 冻结 → 消费」的完整数据链。
2. **步骤**：
   - 读 `generate_label_testvectors.py`，理解 `strels` 经 `flipud` + `rot90` + 去重后到底有多少个结构元（去重前的规模是 8×3=24，去重后取决于对称性）；
   - 打开 `tests/data/label_inputs.txt`，确认它是 3 张 7×7 图纵向拼接（共 21 行，每行 7 个数）；
   - 读 `test_label_structuring_elements`，画出 `r += 1` 的递增顺序：外层是图、内层是结构元，所以 `results[r]` 的排列是「图0×全部结构元，图1×全部结构元，…」。
3. **需要观察的现象**：`label_inputs.txt` 第 1–7 行全是 1（第一张全 1 图），第 8 行起是对称图案；`label_strels.txt` 每行是一个 3×3 结构元展平后的 9 个数。
4. **预期结果**：本地运行 `python -m pytest scipy/ndimage/tests/test_measurements.py::test_label_structuring_elements -v` 应通过；你能在脑中复述三个 `.txt` 如何通过 reshape 与双重循环对齐。
5. 待本地验证。

#### 4.5.5 小练习与答案

**练习 1**：为什么结构元要经过 `flipud` 和 `rot90` 扩张再去重？
**答案**：连通标记对结构元的**对称性**敏感——`label` 要求结构元中心对称（u4-l1）。通过翻转和旋转生成结构元的各种朝向变体，可以覆盖「结构元不对称时行为如何」的边界情形；去重（用 `tobytes` 做集合键）是为了避免对完全相同的结构元重复算期望结果，浪费数据体积。

**练习 2**：如果将来重写了 `_ni_label.pyx` 让某个组合的结果变了，这个测试会怎样？
**答案**：`xp_assert_equal` 会在那个 `r` 处报「实际标记数组 ≠ 冻结的 `results[r]`」，测试失败。这正是冻结向量的价值——它把「历史正确答案」固化下来，任何无意改变 `label` 行为的改动都会被立即抓住。若是**有意**改变行为，开发者需重跑生成脚本更新 `.txt`，并在 PR 里说明。

---

### 4.6 辅助工具测试：test_ni_support 与 test_splines

#### 4.6.1 概念说明

除了功能域测试和横切测试，ndimage 还有两个「支撑层」测试，专门测**被几乎所有函数复用的内部工具**：

- `test_ni_support.py` 测 `_ni_support._get_output`（u1-l4 讲过的「输出数组获取」助手）——它处理 `output=None` / dtype / 已存在数组三种形态，是数十个函数的公共骨架，一旦出错会连锁影响全网。
- `test_splines.py` 测样条预滤波（u3-l1）——它不跟某个具体实现比对，而是**构造一个可解析求解的样条结点矩阵，验证 `spline_filter1d` 的输出满足该矩阵关系**，这是一种「从数学定义反推」的回归。

#### 4.6.2 核心流程

`test_ni_support` 的策略是「穷举 `_get_output` 的输入分支」：用 `@pytest.mark.parametrize` 把 dtype 指定方式（字符串 `'f4'`、类型 `np.float32`、`None` 等）全部展开，分别验证「派生 dtype」「显式 shape 覆盖输入 shape」「预分配数组原样返回」三条契约，外加复数提升与各种错误情形。

`test_splines` 的策略是「矩阵法验证」：对每个 order（0–5）和每个边界模式，构造一个 n×n 的样条结点矩阵 M，使得「样条系数 c」满足 `c @ M == 原信号`；于是只需断言 `spline_filter1d(eye) @ M == eye`（单位阵的滤波结果就是该阶样条的系数算子），就能在不手算系数的情况下验证正确性。

#### 4.6.3 源码精读

`_get_output` 的分支覆盖：

[tests/test_ni_support.py:L7-L40](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/ndimage/tests/test_ni_support.py#L7-L40) — `test_get_output_basic` 参数化 dtype，验证三件事：`_get_output(None, input_)` 从 input 派生 dtype（[L27-L29](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/ndimage/tests/test_ni_support.py#L27-L29)）、显式 `shape=` 覆盖 input 形状（[L32-L34](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/ndimage/tests/test_ni_support.py#L32-L34)）、预分配数组直接返回自身（`result is output`，[L38-L39](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/ndimage/tests/test_ni_support.py#L38-L39)）。`test_get_output_complex`（[L42-L61](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/ndimage/tests/test_ni_support.py#L42-L61)）测复数提升与告警，`test_get_output_error_cases`（[L64-L77](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/ndimage/tests/test_ni_support.py#L64-L77)）测各种 `RuntimeError`。

样条滤波的矩阵法验证：

[tests/test_splines.py:L62-L71](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/ndimage/tests/test_splines.py#L62-L71) — `test_spline_filter_vs_matrix_solution` 对 `eye = xp.eye(n)`（单位阵的每一列是一个冲激信号）沿两个轴分别做 `spline_filter1d`，再断言「滤波结果 @ 结点矩阵 == 单位阵」。结点矩阵由 `make_spline_knot_matrix`（[L25-L56](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/ndimage/tests/test_splines.py#L25-L56)）按各 order 的结点值（`get_spline_knot_values`，[L13-L22](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/ndimage/tests/test_splines.py#L13-L22)）构造。文件里还有一个回归用例 `test_spline_filter_reflect_small_n`（[L78-L85](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/ndimage/tests/test_splines.py#L78-L85)），专门针对 gh-24550「reflect 模式下小 n 的因果初始化别名 bug」。

#### 4.6.4 代码实践

1. **目标**：理解「分支穷举」与「矩阵法」两种支撑测试范式。
2. **步骤**：
   - 读 `test_get_output_basic` 的 `parametrize` 列表，数出 dtype 指定方式有几种（字符串 2 种 + 类型/dtype 3 种 + None 1 种）；
   - 读 `test_spline_filter_vs_matrix_solution`，确认它对 order ∈ {0..5}、mode ∈ {mirror, grid-wrap, reflect} 做了组合参数化；
   - 思考：为什么用单位阵 `eye` 作为输入？
3. **需要观察的现象**：`eye` 经 `spline_filter1d` 后不再是单位阵（变成了样条系数算子），但它与结点矩阵相乘后**还原**成单位阵——这就是「滤波 + 逆变换 = 恒等」的数学保证。
4. **预期结果**：`test_ni_support` 与 `test_splines` 全部通过；你理解到这两个文件保护的是「被全网复用的底层工具」，它们一旦回归，影响面最大。
5. 待本地验证。

#### 4.6.5 小练习与答案

**练习 1**：`test_get_output_basic` 为什么要测 `result is output`（用 `is` 而非 `==`）？
**答案**：因为 `_get_output` 对「已存在数组」的契约是**原样返回同一个对象**（就地复用，避免拷贝），而不是「返回一个相等的副本」。用 `is` 才能验证「对象同一性」这一性能关键契约；用 `==` 只能验证数值相等，会漏掉「悄悄拷贝了一份」的回归。

**练习 2**：样条测试为什么要同时测 axis=0 和 axis=1 两个方向（`spline_filter_axis_0` 与 `spline_filter_axis_1`）？
**答案**：因为 `spline_filter1d` 沿指定轴做一维预滤波，axis=0 时系数算子作用在矩阵的「行」，axis=1 时作用在「列」，对应的结点矩阵分别是 `M` 与 `M.T`。两个方向都测，才能确认滤波核在任意轴上都正确，而非只在某个轴上碰巧对。

---

## 5. 综合实践

把本讲的知识串起来，完成下面三段任务（对应本讲规格里的实践要求）：

**任务一：在 `test_filters.py` 里找一个 `correlate1d` 边界用例并运行。**

- 定位 [tests/test_filters.py:L652-L659](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/ndimage/tests/test_filters.py#L652-L659) 的 `test_correlate26`，它针对 gh-11661「长度为 1 的信号在 mirror 模式下被长度 5 的核卷积」的边界 bug：`convolve1d(ones(1), ones(5), mode='mirror')` 与 `correlate1d` 同款都应得到 `[5.]`。
- 运行：`python -m pytest "scipy/ndimage/tests/test_filters.py::TestTestCorrelate1d::test_correlate26" -v`（具体类名以本地为准，可用 `-k correlate26` 过滤）。
- 思考：为什么长度 1 的信号在 mirror 扩展下会成为 bug 高发区？（提示：mirror 模式要把唯一一个样本反复镜像，C 端边界扩展代码容易在这里写错下标。）

**任务二：解释 `test_array_likes.py` 如何复现全 API 的 list 输入行为。**

- 通读 `_assert_same_result`（4.3 节），说清它如何用递归多态比对适配 tuple/list/dict/slice/None 五种返回形态。
- 任选一个含多个数组位参数的用例（如 `test_labeled_comprehension_accepts_lists`，[L420-L446](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/ndimage/tests/test_array_likes.py#L420-L446)），指出哪些参数被双份化（`input`/`labels`/`index`）、哪些共用一次（`func`/`out_dtype`/`default`）。
- 解释 `watershed_ift` 用例（[L548-L559](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/ndimage/tests/test_array_likes.py#L548-L559)）为何只把 markers 双份。

**任务三：跟踪 `label` 回归数据链。**

- 读 `generate_label_testvectors.py`，说清 `label_inputs.txt`（3 张 7×7 图）、`label_strels.txt`（一批 3×3 结构元）、`label_results.txt`（每个 图×结构元 组合的标记结果）三者如何由 `np.vstack` 拼接、`np.savetxt` 落盘。
- 读 `test_measurements.py::test_label_structuring_elements`，说清消费端如何用 `reshape` + 双重循环 + `r += 1` 把三者重新对齐成「每个组合一次断言」。
- 如果你有意改坏 `_ni_label.pyx`，预测哪个 `r` 会先报错。

> 三个任务分别对应本讲的「功能域测试」「输入类型冒烟」「冻结向量回归」三大策略。完成它们，你就掌握了 ndimage 测试体系的骨架。

## 6. 本讲小结

- ndimage 测试按**功能域分文件**：五大域各一个 `test_*.py`，再加 `test_datatypes`/`test_array_likes`/`test_c_api` 三个横切测试与 `test_ni_support`/`test_splines` 两个支撑测试；`tests/meson.build` 的 `python_sources` 是文件清单的单一事实来源。
- `test_datatypes.py` 用 **dtype × dtype × order 的笛卡尔积矩阵**（约 864 组合）覆盖插值函数的全部数值类型；对已知平台 bug 用 `@pytest.mark.xfail` 显式登记。
- **本次新增**的 `test_array_likes.py` 是一份 numpy-only 冒烟回归：用「双调对比」（list 输入 vs ndarray 输入）+ 递归多态的 `_assert_same_result`，一次性守住全部 74 个公开 API 对 array_like 的接受度；非数组参数（回调、标量）只传一次，数组位参数（weights/coordinates/matrix/labels/index）才双份。
- `test_c_api.py` 用「参照 Python 实现 vs 四种回调入口（C/Cython/LowLevelCallable 两变体）」的两两比对，验证 C 扩展回调桥接的正确性。
- `utils/generate_label_testvectors.py` 把 `label` 在「3 张图 × 一批结构元」上的参考结果冻结成 `data/*.txt`，`test_label_structuring_elements` 消费它们做回归——这是保护 Cython 内核不被无意改坏的黄金标准。
- 支撑测试 `test_ni_support`（穷举 `_get_output` 分支）与 `test_splines`（用结点矩阵反推 `spline_filter1d`）守护被全网复用的底层工具，回归影响面最大。

## 7. 下一步学习建议

- **往测试纵深走**：选一个功能域测试（建议 `test_filters.py`），通读它的参数化模式（`@uses_output_array`、`@pytest.mark.parametrize`、`make_xp_test_case`），理解它如何把「一个语义测试」展开成「dtype × 后端」的大矩阵。这正是 u7-l1 后端委托在测试侧的对应物。
- **往 C 内核走**：`test_c_api.py` 的 `_ctest`/`_cytest` 模块是理解 `LowLevelCallable` 回调签名的最佳入口；结合 u6-l1 的 `methods[]` 分发表，你可以画出「Python 调用 → C 包装函数 → 回调桥接」的完整路径。
- **自己加一个冒烟用例**：如果将来 ndimage 新增了公开函数，按 `test_array_likes.py` 的双调对比模式给它补一个 `test_<name>_accepts_lists`，并记得在 `tests/meson.build` 注册——这就是本讲教给你的可立即上手的工作流。
- **回归数据维护**：若你将来修改了 `label` 的行为，按 `tests/data/README.txt` 的指引重跑 `generate_label_testvectors.py` 更新冻结向量，并在 PR 中说明行为变更理由。
