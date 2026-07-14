# 测试体系：test_defmatrix / test_interaction / linalg 回归

## 1. 本讲目标

本讲是专家层的收官篇。前面六讲（u3-l1 ~ u3-l6）我们一直在「读 `defmatrix.py`」——逐行拆解 `__getitem__`、`_collapse/_align`、`T/H/I/A/A1`、`flatten/ravel`、`subok/nditer`、`.pyi` 存根。本讲反过来：**读测试，反推被测代码的契约**。

学完后你应该能够：

1. 说清 `numpy/matrixlib/tests/` 目录下 7 个测试文件各自的**职责边界**与命名约定，知道一个新 bug 该往哪个文件加测试。
2. 看懂 `test_matrix_linalg.py` 如何用 `MatrixTestCase` 混入（mixin）复用 `numpy.linalg` 的通用测试基类，一行 `pass` 就跑几十条用例。
3. 把 `test_regression.py` 里的「Ticket #71 / #125 / #473」与具体被测行为对上号。
4. 用 `pytest` 的节点 ID（node id）选择性运行**单个测试类或单个测试函数**，并能解释 `np.matrixlib.test()` 这个一行式入口背后 `PytestTester` 做了什么。
5. 用测试断言反向验证 `defmatrix.py` 里「永远二维」「行向量 vs 列向量」「子类型保形」这几条核心不变量。

## 2. 前置知识

本讲默认你已经读过 u1-l3（`PytestTester.test` 入口）、u2-l4（`__array_finalize__` / `__array_priority__`）和 u3-l1（`__getitem__` 与「永远二维」的索引语义）。下面只补三个测试本身需要的前置概念。

**pytest 的收集约定（collection convention）。** pytest 默认只收集两类对象：文件名以 `test_*.py` / `*_test.py` 开头；其中以 `test` 开头的函数、以 `Test` 开头且不含 `__init__` 的类，其内部以 `test` 开头的方法才被当作测试。这一点在本讲有个**真实的坑**：`test_interaction.py` 里有一个函数 `like_function`（注意：没有 `test_` 前缀），它不会被 pytest 收集，等于一段「死代码」。后面 4.4 会专门点出来。

**节点 ID（node id）。** pytest 用 `路径::类名::方法名` 三段式精确定位一条用例，例如 `numpy/matrixlib/tests/test_defmatrix.py::TestNewScalarIndexing::test_dimensions`。冒号是分隔符，可只写到类名（跑整类）或只写到文件（跑整个文件）。

**`numpy.testing` 的断言家族。** 测试里反复出现的 `assert_`、`assert_equal`、`assert_array_equal`、`assert_almost_equal`、`assert_raises`、`assert_raises_regex` 都是 `numpy.testing` 提供的数组友好断言。其中 `assert_equal` 能比较 ndarray/matrix 的逐元素相等，`assert_raises(Exception, func, *args)` 用「传函数＋位置参数」的方式断言某次调用会抛指定异常——matrix 测试大量用它来锁定「构造非法输入应当抛错」的契约。

**测试与「不变量」的关系。** matrix 的所有奇特行为归根结底是三条不变量：①恒二维；②朝向正确（`axis=0`→行向量 `(1,N)`、`axis=1`→列向量 `(N,1)`、`axis=None`→标量）；③运算/视图后保持 `matrix` 子类型。测试文件就是这三条不变量的「可执行说明书」。

## 3. 本讲源码地图

| 文件 | 行数 | 职责 | 本讲精读重点 |
|---|---|---|---|
| `tests/test_defmatrix.py` | 476 行 | matrix 自身的「单元测试」：构造、属性、索引、代数、形状、模式匹配 | `TestCtor`/`TestProperties`/`TestIndexing`/`TestNewScalarIndexing` 四大组 |
| `tests/test_interaction.py` | 361 行 | matrix 与 numpy **其它模块**的交互（linalg 除外） | `TestConcatenatorMatrix`、`test_nanfunctions_matrices`、`test_iter_allocate_output_subtype` |
| `tests/test_matrix_linalg.py` | 111 行 | 用 `matrix` 当输入跑 `numpy.linalg` 的通用用例 | `MatrixTestCase` 混入、`apply_tag` + `LinalgCase` |
| `tests/test_regression.py` | 31 行 | 历史 bug 的回归测试（票据编号） | Ticket #71 / #125 / #473 / #83 |
| `tests/test_multiarray.py` | 17 行 | `ndarray.view(np.matrix)` 视图行为 | `TestView` |
| `tests/test_numeric.py` | 18 行 | 点积、对角线在 matrix 上的保形 | `TestDot`、`test_diagonal` |
| `tests/test_masked_matrix.py` | 882 行 | `MaskedArray` 与 `matrix` **多重继承** | `MMatrix`（专家向，本讲只点到为止） |
| `defmatrix.py` | 1095 行 | 被测对象本体 | `__array_finalize__`(172-193)、`__getitem__`(195-219)、`_align`(246-257)、`_collapse`(259-266) |
| `numpy/_pytesttester.py` | — | 子包级测试入口 `PytestTester` 的实现 | `PendingDeprecationWarning` 过滤、返回布尔值 |

> 说明：`numpy/_pytesttester.py` 在 `matrixlib/` 目录之外，本讲用全路径 `numpy/_pytesttester.py` 引用它。

## 4. 核心概念与源码讲解

### 4.1 测试目录的职责划分与命名约定

#### 4.1.1 概念说明

matrixlib 的测试体量很小（7 个文件，加起来不到 1500 行非空代码），却把「该测什么」切得很干净。理解这套切分，本质是理解 **matrix 的风险面**在哪里：

- matrix 的**自身正确性**（构造、索引、属性、代数） → `test_defmatrix.py`
- matrix 与 **numpy 其它子系统**一起用会不会退化 → `test_interaction.py`
- matrix 当作 **linalg 的输入**算 inv/eig/svd 对不对 → `test_matrix_linalg.py`
- 历史 **bug 是否复发** → `test_regression.py`
- 极小的**单一行为**回归（视图、点积、对角） → `test_multiarray.py` / `test_numeric.py`
- matrix 与 **MaskedArray 共存**的极端多重继承场景 → `test_masked_matrix.py`

这套分工的潜台词是：matrix 是个「被弃用但要永久兼容」的子类，它的测试重心不在「功能扩展」，而在「**回归保护**」——任何重构都不能让它已有的古怪行为悄悄变化。

#### 4.1.2 核心流程

一个新 bug 落到 matrixlib 测试时，按下面的决策树归档：

```
新 bug
 ├─ 是 matrix 自己的构造/索引/属性/代数错？      → test_defmatrix.py（加 TestXxx 类）
 ├─ 是 matrix × 其它模块（sort/kron/nan/r_/…）错？→ test_interaction.py（加 test_xxx 函数）
 ├─ 是 matrix × linalg 的数值错？                 → test_matrix_linalg.py（多半要加 LinalgCase）
 ├─ 是 matrix × MaskedArray 多重继承错？          → test_masked_matrix.py
 └─ 是来自 GitHub/邮件列表的历史票据？            → test_regression.py（注释写 Ticket #N）
```

`test_interaction.py` 顶部有一行模块文档串直接声明了这条边界：

[test_interaction.py:1-4](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/tests/test_interaction.py#L1-L4)
> 说明：它明确写出「与 MaskedArray 和 linalg 的测试放在**单独的文件**里」，这就是 `test_matrix_linalg.py` 与 `test_masked_matrix.py` 存在的原因。

#### 4.1.3 源码精读

七个文件的**测试组织形态**有两种风格，对照看就能看出分工：

- **类风格**（`test_defmatrix.py`、`test_matrix_linalg.py`、`test_multiarray.py`、`test_numeric.py`、`test_regression.py`）：用 `class TestXxx:` 把相关用例聚合，类名即「主题」。例如 `TestCtor`（构造）、`TestProperties`（属性）、`TestIndexing`（索引）、`TestAlgebra`（代数）、`TestPower`（矩阵幂）、`TestShape`（形状）、`TestPatternMatching`（PEP 634 模式匹配）。
- **函数风格**（`test_interaction.py` 的大部分）：每个用例是模块级 `def test_xxx():`，函数名常带「来源票据/gh 编号」注释，便于追溯。例如 `test_fancy_indexing` 标了 `gh-3110`，`test_partition_matrix_none` 标了 `gh-4301`。

注意 `test_interaction.py` 里大量注释写着 `# 2018-04-29: moved here from core.tests.test_xxx`——这些用例原本散落在 numpy 核心测试里，2018 年统一「搬家」到 matrixlib，因为它们只对 matrix 有意义。这种「搬家注释」是该文件最显著的命名约定。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：验证 `like_function` 这个名字陷阱。
2. **操作步骤**：打开 [test_interaction.py:121-129](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/tests/test_interaction.py#L121-L129)，注意函数名是 `like_function` 而非 `test_like_function`。
3. **需要观察的现象**：它测的是 `np.zeros_like/ones_like/empty_like` 对 matrix 的 `subok` 行为，逻辑完全正确，但**因为缺 `test_` 前缀，pytest 永远不会收集它**。
4. **预期结果**：`pytest --collect-only` 的输出里找不到 `like_function`。这是真实存在于源码中的命名疏漏，可作为「pytest 收集约定」的反面教材。
5. 若本地有环境，可运行 `python -m pytest --collect-only numpy/matrixlib/tests/test_interaction.py` 自行确认（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：如果你想给 `matrix` 的字符串构造加一个「非法字符串抛 ValueError」的回归测试，应该放进哪个文件？  
**答案**：`test_defmatrix.py` 的 `TestCtor`。事实上 `test_exceptions` 已经这么做：`assert_raises(ValueError, matrix, "invalid")`（见 [test_defmatrix.py:39-41](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/tests/test_defmatrix.py#L39-L41)）。

**练习 2**：`test_interaction.py` 里某用例注释写着 `moved here from core.tests.test_multiarray`，这种「搬家注释」的价值是什么？  
**答案**：保留历史出处，方便 `git blame` 追溯原始用例、避免别人再次把它搬回核心测试目录造成重复。

---

### 4.2 test_defmatrix.py：TestCtor / TestProperties / TestIndexing 三大组

#### 4.2.1 概念说明

`test_defmatrix.py` 是 matrix 的「主测试文件」，476 行覆盖了 u2、u3 系列讲过的几乎全部机制。它的测试类按主题切片，最核心的三个是：

- `TestCtor`：构造函数与 `bmat` 的输入分发（承接 u2-l1、u2-l3）。
- `TestProperties`：归约、属性、比较（承接 u3-l2、u3-l3）。
- `TestIndexing` / `TestNewScalarIndexing`：索引保形（承接 u3-l1）。

之所以把 `TestNewScalarIndexing` 单独拎出来（而不是塞进 `TestIndexing`），是因为它测的是一条**最微妙的契约**：标量索引下 matrix 仍要保持二维——这是 ndarray 行为最直接的分水岭，值得独立成类、独立维护。

#### 4.2.2 核心流程

`TestCtor` 的 `test_bmat_nondefault_str` 是理解 `bmat` 三条字符串路径的最佳入口，它一次性覆盖了 `ldict`/`gdict` 的全部组合：

```
bmat("A,A;A,A")                          # gdict=None → 走调用栈帧，用调用者局部 A
bmat("A,A;A,A", ldict={'A': B})          # gdict=None → ldict 被忽略！仍用栈帧的 A
bmat("A,A;A,A", gdict={'A': B})          # gdict≠None → loc_dict=None → None[col] → TypeError
bmat("A,A;A,A", ldict={'A':A}, gdict={'A':B})  # 两者都给 → A 取自 ldict
bmat("A,B;C,D", ldict={A,B}, gdict={C=B,D=A})  # 混合取，验证「先 ldict 后 gdict」
```

这条流程直接对应 `defmatrix.py` 里 `bmat` 的 `gdict is None` 分支判断与 `_from_string` 的「先 `ldict[col]` 后 `gdict[col]`」查表顺序。

#### 4.2.3 源码精读

`test_bmat_nondefault_str` 全文：

[test_defmatrix.py:43-60](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/tests/test_defmatrix.py#L43-L60)
> 说明：第 55 行只给 `ldict` 不给 `gdict`，断言结果仍是 `Aresult`（即用了调用者的 A，而非 B），证明 `ldict` 在 `gdict is None` 时被忽略；第 56 行 `assert_raises(TypeError, ...)` 锁定「只给 `gdict` 不给 `ldict`」会因 `loc_dict=None` 触发 `TypeError`。

这条测试反查回 `defmatrix.py` 里的分发逻辑与 `_from_string`：

[defmatrix.py:1095-1103](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L1095-L1103)
> 说明：`bmat` 在字符串分支里，`gdict is None` 才用 `sys._getframe().f_back` 回溯调用栈取局部/全局字典；否则把 `gdict`/`ldict` 原样下传——这就是 `ldict` 单独给会被忽略的根因。

[defmatrix.py:1015-1037](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L1015-L1037)
> 说明：`_from_string` 按 `;` 分行、`,` 与空格分块，逐 token 先查 `ldict[col]`，`KeyError` 再查 `gdict[col]`，二者都失败抛 `NameError`。当 `ldict=None` 时 `ldict[col]` 直接抛 `TypeError`（`None['A']`）——对应测试第 56 行。

`TestProperties` 侧，`test_sum` 的注释直接点明了它要守护的不变量：

[test_defmatrix.py:64-81](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/tests/test_defmatrix.py#L64-L81)
> 说明：文档串写「`matrix.sum(axis=1)` 保朝向，在 NumPy ≤ 0.9.6.2127 会失败」——这是把一条**历史 bug 的回归**写进了测试名。`sum0` 是行向量 `(1,N)`、`sum1` 带 `.T` 是列向量 `(N,1)`、`sumall` 是标量，三者同时验证「恒二维 + 朝向正确」两条不变量。

#### 4.2.4 代码实践

1. **实践目标**：用 `pytest` 只跑 `TestCtor` 这一类，确认 `test_bmat_nondefault_str` 通过。
2. **操作步骤**：在 numpy 仓库根目录运行
   ```bash
   python -m pytest "numpy/matrixlib/tests/test_defmatrix.py::TestCtor" -v
   ```
3. **需要观察的现象**：终端列出 `test_basic`、`test_exceptions`、`test_bmat_nondefault_str` 三条用例，全部 PASSED。
4. **预期结果**：3 passed。若想只跑单条，把节点 ID 续写到方法名：`...::TestCtor::test_bmat_nondefault_str`。
5. 命令本身未替你运行，待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`test_sum` 为什么把期望值写成 `matrix([3, 7, 6, 14]).T`（带 `.T`）而不是直接 `matrix([[3],[7],[6],[14]])`？  
**答案**：`.T` 写法更短、更易读，且同时验证了「`axis=1` 的结果是列向量」——如果 sum 不保朝向，`.T` 比较会失败。这是一种用「形状操作」隐式断言形状的惯用法。

**练习 2**：`TestMatrixReturn.test_instance_methods`（[test_defmatrix.py:275-311](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/tests/test_defmatrix.py#L275-L311)）用一个循环遍历 `dir(a)` 里所有可调用属性，它想抓住什么风险？  
**答案**：抓住「某个 ndarray 方法在 matrix 上意外返回了 ndarray 而非 matrix」的回归。它用一个 `excluded_methods` 白名单排除掉那些**本就该降维/脱壳**的方法（如 `argmax`、`tolist`、`item`），其余方法一律断言 `type(b) is matrix`。

---

### 4.3 test_interaction.py：matrix 与 numpy 生态的交互（含 TestConcatenatorMatrix）

#### 4.3.1 概念说明

`test_interaction.py` 回答一个问题：**matrix 喂给 numpy 的通用函数（`sort`、`kron`、`nanmin`、`r_`、`apply_along_axis`、`ediff1d`、`average`、`inner`……）时，会不会丢掉二维形状或 matrix 子类型？** 这些函数大多不是为 matrix 写的，但 matrix 靠 `__array_finalize__`、`__array_priority__`、`_wrapfunc` 委托（见 u3-l5）顽强地保住自己的不变量。本文件就是这套「顽强」的验收单。

其中 `TestConcatenatorMatrix` 验证的是 `np.r_` 这个索引技巧对象（index_tricks 里的 `RClass`）在收到 `'r'`/`'c'` 指令时返回的是不是二维 matrix。

#### 4.3.2 核心流程

`TestConcatenatorMatrix.test_matrix` 的三条断言对应三种行为：

```
np.r_['r', [1,2], [3,4]]   →  matrix([[1,2,3,4]])   行拼接，1×4
np.r_['c', [1,2], [3,4]]   →  matrix([[1],[2],[3],[4]])  列拼接，4×1
np.r_['rc', [1,2], [3,4]]  →  ValueError（'rc' 不是合法指令）
```

`np.r_` 的第一个字符串参数叫「指令串」，`'r'` 要求结果转成行矩阵、`'c'` 要求转成列矩阵。这两条指令之所以会产出 `matrix` 而非 `ndarray`，是因为 `RClass` 内部对 `'r'/'c'` 显式调用了 matrix 构造——这是 numpy 为兼容老式 MATLAB 风格留下的接口。

#### 4.3.3 源码精读

`TestConcatenatorMatrix` 全文：

[test_interaction.py:297-328](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/tests/test_interaction.py#L297-L328)
> 说明：第 306-307 行断言 `'r'`/`'c'` 的返回类型是 `np.matrix`；第 309-310 行用 `np.array(...)` 脱壳后比对数值布局（行 vs 列）；第 312 行 `assert_raises(ValueError, lambda: np.r_['rc', a, b])` 锁定非法指令串抛错；第 319-328 行 `test_matrix_builder` 验证 `np.r_['a, b; c, d']` 与 `np.bmat([[a,b],[c,d]])` 完全等价——说明 `r_` 的「矩阵构造语法」底层就是 `bmat`。

另两个高价值交互用例：

[test_interaction.py:168-206](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/tests/test_interaction.py#L168-L206)
> 说明：`test_nanfunctions_matrices` 验证 `np.nanmin/nanmax` 在 matrix 上**保类型、保朝向**（`axis=0`→`(1,3)`、`axis=1`→`(3,1)`），并覆盖「全 nan 行」时 `axis=1` 应发 `RuntimeWarning`、`axis=0` 不发的细枝末节——这正是 u3-l5 讲过的「子类安全慢路径 + `_wrapfunc` 委托给 matrix 自己的 `keepdims=True` 归约」。

[test_interaction.py:94-118](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/tests/test_interaction.py#L94-L118)
> 说明：`test_iter_allocate_output_subtype` 验证 `np.nditer` 自动分配的输出按 `__array_priority__`（matrix=10.0）选 matrix 子类型；当广播输出是 3D（`b` 为 `(1,2,2)`）时，matrix 的二维约束触发 `RuntimeError`，用 `'no_subtype'` 标志可降级回 ndarray。这条用例把 u3-l5 的两条结论一次性钉死。

#### 4.3.4 代码实践

1. **实践目标**：跑 `TestConcatenatorMatrix`，对照 `defmatrix.py` 的 `bmat` 说明它验证的保形规则。
2. **操作步骤**：
   ```bash
   python -m pytest "numpy/matrixlib/tests/test_interaction.py::TestConcatenatorMatrix" -v
   ```
3. **需要观察的现象**：`test_matrix`、`test_matrix_scalar`、`test_matrix_builder` 三条 PASSED。
4. **预期结果 / 它验证的规则**：它验证的是「**子类型保形 + 二维块布局**」这条规则——`np.r_` 的 `'r'/'c'` 指令必须返回二维 `matrix`（而非降维 ndarray），且 `'a, b; c, d'` 这种块语法必须与 `bmat` 行为一致。这与 `TestNewScalarIndexing` 验证的「索引永远二维」是同一条不变量的两个入口：一个从**索引**进，一个从**拼接构造**进。
5. 待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`test_iter_allocate_output_subtype` 里，为什么把 `b` 换成 `(1,2,2)` 就会抛 `RuntimeError`？  
**答案**：`nditer` 把输出广播成 `(1,2,2)` 的 3D，而分配子类型时默认选了 `matrix`，matrix 的 `__array_finalize__`（[defmatrix.py:179-186](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L179-L186)）对 ndim>2 且无法挤成长度为 1 的维度的情况抛 `shape too large to be a matrix.`，在 nditer 上下文中表现为 `RuntimeError`。

**练习 2**：`like_function`（无 `test_` 前缀）测的 `subok=False` 行为，如果改名成 `test_like_function`，断言 `type(c) is not np.matrix` 验证的是什么？  
**答案**：验证 `zeros_like(m, subok=False)` 会**降级**成普通 ndarray（u3-l5 的 subok 开关），与 `subok=True`（默认，保 matrix）形成对照。

---

### 4.4 test_matrix_linalg.py：MatrixTestCase 混入复用 linalg 测试基类

#### 4.4.1 概念说明

`test_matrix_linalg.py` 是整个 matrixlib 测试体系里**最巧妙**的一个文件：它只有 111 行，却能让 matrix 跑 `numpy.linalg` 几十上百条通用用例（inv / solve / eig / eigvals / svd / cond / pinv / det / lstsq / norm / qr）。秘诀在于**测试基类的混入（mixin）复用**：

- `numpy.linalg.tests.test_linalg` 里定义了一组「用例基类」`XxxCases`，它们不绑定具体输入，而是从一个 `TEST_CASES` 列表取数据。
- matrixlib 这边只需：①准备一批以 `matrix` 为输入的 `LinalgCase`；②定义 `MatrixTestCase(LinalgTestCase)` 把这批数据装进去；③让每个 `TestXxxMatrix` 同时继承 `XxxCases` 和 `MatrixTestCase`，`pass` 即可。

这是典型的「**数据与逻辑分离**」测试设计：用例逻辑写一次（在 linalg 侧），输入数据换一份（在 matrixlib 侧），排列组合自动展开。

#### 4.4.2 核心流程

```
apply_tag('square', [LinalgCase(...), ...])   # 打 'square' 标签的 matrix 输入用例
apply_tag('hermitian', [LinalgCase(...)])      # 打 'hermitian' 标签
        │
        ▼
CASES = [...]                                 # 汇总成一份输入清单
        │
        ▼
class MatrixTestCase(LinalgTestCase):         # 把 CASES 装进基类要求的 TEST_CASES
    TEST_CASES = CASES
        │
        ▼
class TestInvMatrix(InvCases, MatrixTestCase): # 多重继承：逻辑(InvCases) + 数据(MatrixTestCase)
    pass                                       # 不用写任何方法体！
```

每个 `TestXxxMatrix` 类因此自动拥有 `XxxCases` 基类里所有 `test_*` 方法，只是它们迭代的数据变成了 matrix 版的 `CASES`。

#### 4.4.3 源码精读

导入的基类清单直接说明了「复用了哪些 linalg 测试」：

[test_matrix_linalg.py:5-23](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/tests/test_matrix_linalg.py#L5-L23)
> 说明：从 `test_linalg` 一次性导入 `CondCases`、`DetCases`、`EigCases`、`EigvalsCases`、`InvCases`、`LinalgCase`、`LinalgTestCase`、`LstsqCases`、`PinvCases`、`SolveCases`、`SVDCases`、`TestQR`、`_TestNorm2D` 等基类与工具——这就是「逻辑来源」。

数据侧，`LinalgCase` 的第三、四参数就是要测的 `a`、`b` 矩阵：

[test_matrix_linalg.py:25-46](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/tests/test_matrix_linalg.py#L25-L46)
> 说明：构造了 `0x0_matrix`（空矩阵）、`matrix_b_only`（只有 b 是 matrix）、`matrix_a_and_b`（a、b 都是 matrix）、`hmatrix_a_and_b`（Hermitian 情形）等典型输入。注释明确写「No need to make generalized or strided cases for matrices」——matrix 已弃用，没必要覆盖 strided 这种边角维度。

数据装配与一组「`pass` 即用例」的类：

[test_matrix_linalg.py:50-90](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/tests/test_matrix_linalg.py#L50-L90)
> 说明：`MatrixTestCase(LinalgTestCase)` 把 `TEST_CASES = CASES` 注入；随后 `TestSolveMatrix`、`TestInvMatrix`、`TestEigvalsMatrix`、`TestEigMatrix`、`TestSVDMatrix`、`TestCondMatrix`、`TestPinvMatrix`、`TestDetMatrix`、`TestLstsqMatrix` 全部是 `class …(XxxCases, MatrixTestCase): pass`——一行 `pass` 就继承了基类的全部 `test_*` 方法。

注意 `TestLstsqMatrix` 上有一个标记：

[test_matrix_linalg.py:86-90](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/tests/test_matrix_linalg.py#L86-L90)
> 说明：`@pytest.mark.thread_unsafe(reason="residuals not calculated properly for square tests (gh-29851)")`——这是 free-threaded Python（无 GIL）引入的新标记，说明这个用例在线程不安全模式下需要串行跑。这是现代 numpy 测试体系的一个新细节。

#### 4.4.4 代码实践

1. **实践目标**：亲眼看到「一个 `pass` 类展开成几十条用例」。
2. **操作步骤**：
   ```bash
   # 只收集、不运行，看 TestInvMatrix 展开了多少条
   python -m pytest "numpy/matrixlib/tests/test_matrix_linalg.py::TestInvMatrix" --collect-only -q
   ```
3. **需要观察的现象**：`TestInvMatrix` 虽然源码里只有一个 `pass`，但收集到的用例数量远大于 0（具体数取决于 `CASES` 里 `square` 标签的用例数 × 基类里的 `test_*` 方法数）。
4. **预期结果**：每条展开后的用例形如 `TestInvMatrix::test_XXX[case0]`，`[case0]` 是参数化后缀，对应 `LinalgCase` 的名字。
5. 待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么不直接在 matrixlib 里重写一遍 inv/solve/eig 的测试，而要用混入？  
**答案**：避免重复。linalg 的数值正确性逻辑（各种 dtype、各种条件数、各种奇异情形）由 `test_linalg` 统一维护，matrixlib 只需贡献「matrix 作为输入」这一维度，排列组合由 pytest 参数化自动完成。一旦 linalg 改了断言精度，matrix 这边自动受益。

**练习 2**：`TestNormDoubleMatrix(_TestNorm2DMatrix, _TestNormDoubleBase)` 里 `_TestNorm2DMatrix` 重写了 `array = np.matrix`（[test_matrix_linalg.py:93-94](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/tests/test_matrix_linalg.py#L93-L94)），这个类属性的用途是什么？  
**答案**：基类 `_TestNorm2D`/`_TestNormDoubleBase` 用 `self.array(...)` 构造测试输入，子类把 `array` 指向 `np.matrix` 后，所有范数用例自动以 matrix 为输入运行——又一种「换数据不换逻辑」的复用手法，只不过这里用的是类属性而非 `TEST_CASES` 列表。

---

### 4.5 test_regression.py：回归票据测试（Ticket #71 / #125 / #473）

#### 4.5.1 概念说明

`test_regression.py` 只做一件事：把历史 bug 固化成「永远跑」的用例，防止同一个坑被踩第二次。每条用例的注释都带一个**票据号**（`Ticket #N`），对应 numpy 早期 issue tracker 的编号。这类文件在整个 numpy 里都很常见，是「**回归测试即文档**」思想的体现——读这些用例，等于读 matrix 的「曾经翻过的车」。

#### 4.5.2 核心流程

本文件四个测试方法对应四张票据，每张都是一条被固化下来的契约：

| 票据 | 测试方法 | 核心断言 | 修复的 bug |
|---|---|---|---|
| #71 | `test_kron_matrix` | `type(np.kron(x, x)) == type(x)` | `np.kron` 没保住 matrix 子类型，返回了 ndarray |
| #125 | `test_matrix_properties` | `type(a.real) is np.matrix`、`nonzero()` 返回 ndarray | `.real`/`.imag` 属性意外脱壳成 ndarray |
| #473 | `test_matrix_multiply_by_1d_vector` | `asmatrix(eye(2)) * ones(2)` 抛 `ValueError` | matrix 乘 1D 向量的形状冲突未被正确拒绝 |
| #83 | `test_matrix_std_argmax` | `x.std().shape == ()`、`x.argmax().shape == ()` | 无 axis 的 std/argmax 返回了数组而非 numpy 标量 |

#### 4.5.3 源码精读

整个文件只有 31 行，可以完整看清「票据即注释、断言即契约」的写法：

[test_regression.py:1-31](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/tests/test_regression.py#L1-L31)
> 说明：逐条看——
> - **#71（第 6-9 行）**：`x = np.matrix('[1 0; 1 0]')`，断言 `np.kron(x, x)` 的类型仍是 matrix。kron 内部要对子块做乘加，早期版本没走 `__array_priority__`/子类型保形，结果脱壳了。
> - **#125（第 11-18 行）**：断言 `a.real`、`a.imag` 是 matrix；但 `np.matrix([0.0]).nonzero()` 返回的两个下标数组是 **ndarray**（不是 matrix）——因为下标数组本就不是二维数据，理应脱壳。这条用例同时锁定了「保」与「不放」两种正确行为。
> - **#473（第 20-25 行）**：用嵌套函数 `mul()` 包住调用，`assert_raises(ValueError, mul)`。为什么 `asmatrix(eye(2)) * ones(2)` 会抛错？因为 `*` 是矩阵乘，`__mul__` 把 `ones(2)` 经 `asmatrix` 升成 `(1,2)` 行向量（承接 u2-l5），与 `(2,2)` 做 `np.dot` 形状不符 → `ValueError`。这正是「matrix 把 1D 强制二维」带来的、与 ndarray 截然不同的行为。
> - **#83（第 27-31 行）**：无 axis 时 `std()`/`argmax()` 的结果 shape 必须是 `()`（numpy 标量），不能是 `(1,1)` matrix——对应 `_collapse`/`_align` 里 `axis is None` 取 `self[0,0]` 的标量化收尾（见 [defmatrix.py:250-251](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L250-L251) 与 [defmatrix.py:263-264](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L263-L264)）。

#### 4.5.4 代码实践

1. **实践目标**：跑一遍回归测试，确认四张票据都被守住。
2. **操作步骤**：
   ```bash
   python -m pytest "numpy/matrixlib/tests/test_regression.py" -v
   ```
3. **需要观察的现象**：四条用例 `test_kron_matrix`、`test_matrix_properties`、`test_matrix_multiply_by_1d_vector`、`test_matrix_std_argmax` 全部 PASSED。
4. **预期结果**：4 passed。
5. 进阶：手动构造 `np.kron(np.matrix('[1 0;1 0]'), np.array([[1,2],[3,4]]))`，观察混合输入时返回类型仍是 matrix（验证 #71 修复对混合输入也成立）。待本地验证。

#### 4.5.5 小练习与答案

**练习 1**：Ticket #473 为什么用嵌套函数 `def mul(): ...` 包住再传给 `assert_raises`，而不是直接 `assert_raises(ValueError, np.dot, ...)`？  
**答案**：因为要测的是 `asmatrix(eye(2)) * ones(2)` 这个**表达式**（触发 `matrix.__mul__`），不是一个现成函数。`assert_raises` 要求可调用对象，所以用零参嵌套函数 `mul` 把表达式包成可调用体，再交给 `assert_raises(ValueError, mul)`。

**练习 2**：如果有人「修好」了 #473，让 `matrix * 1D向量` 不再抛错而是广播成功，这条测试会怎样？  
**答案**：`test_matrix_multiply_by_1d_vector` 会**失败**（期望 `ValueError` 没抛）。这正是回归测试的意义——它会阻止这种「看似友好实则破坏 matrix 语义一致性」的改动悄悄合入。

---

### 4.6 test_multiarray.py 与 test_numeric.py：视图、点积、对角

#### 4.6.1 概念说明

这两个文件各只有十几行，是 matrix 测试体系里的「边角料」，但各自钉死一条很容易被忽略的行为：

- `test_multiarray.py::TestView`：`ndarray.view(np.matrix)` 把普通数组**原地转译**成 matrix 的能力，包括带 dtype 重解释的视图。
- `test_numeric.py`：`TestDot.test_matscalar` 钉死「matrix × 标量」走 `__mul__` 的标量分支；`test_diagonal` 钉死 `matrix.diagonal()`（成员方法，保二维）与 `np.diagonal(matrix)` / `np.diag(matrix)`（顶层函数，返回一维 ndarray）的**口径差异**。

#### 4.6.2 核心流程

```
ndarray.view(np.matrix)        # 不复制内存，只换子类型 → __array_finalize__ 把形状补二维
ndarray.view(dtype='<i2', type=np.matrix)  # 同时换 dtype 与子类型，按新 dtype 重解释字节

matrix × 标量  →  __mul__ 标量分支 → 缩放，仍为 matrix
matrix.diagonal()      →  成员方法，返回 (1,N) matrix
np.diagonal(matrix)    →  顶层函数，返回 (N,) ndarray
np.diag(matrix)        →  顶层函数，返回 (N,) ndarray
```

#### 4.6.3 源码精读

`TestView` 全文：

[test_multiarray.py:1-17](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/tests/test_multiarray.py#L1-L17)
> 说明：`test_type` 验证 `x.view(np.matrix)` 得到的是 `np.matrix` 实例（视图、不复制）；`test_keywords` 更进一步——把结构化 dtype `[('a', i8), ('b', i8)]` 的数组按 `<i2` 重解释后转成 matrix，断言数值为 `[[513]]`（即两个 int8 字节 `1,2` 按 little-endian int16 读成 `1 + 2*256 = 513`），且仍是 matrix。这条用例把 `view` 的「dtype 重解释 + 子类型替换」两件事一次测完。

`test_numeric.py` 全文：

[test_numeric.py:1-18](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/tests/test_numeric.py#L1-L18)
> 说明：`TestDot.test_matscalar` 断言 `b1 * 1.0 == b1`（matrix 乘标量 1 不变）；`test_diagonal` 是关键——`b1.diagonal()` 期望是 `matrix([[1,4]])`（二维 matrix），而 `np.diagonal(b1)` 与 `np.diag(b1)` 期望是 `array([1,4])`（一维 ndarray）。同一个「取对角」语义，成员方法保二维，顶层函数脱壳成一维，差异被同时锁定。

#### 4.6.4 代码实践

1. **实践目标**：亲手验证「成员方法 vs 顶层函数」的对角差异。
2. **操作步骤**：
   ```python
   import numpy as np
   b1 = np.matrix([[1, 2], [3, 4]])
   print(type(b1.diagonal()), b1.diagonal())      # matrix, [[1 4]]
   print(type(np.diagonal(b1)), np.diagonal(b1))  # ndarray, [1 4]
   print(type(np.diag(b1)), np.diag(b1))          # ndarray, [1 4]
   ```
3. **需要观察的现象**：成员方法返回 `(1,2)` matrix；两个顶层函数返回 `(2,)` ndarray。
4. **预期结果**：与 `test_diagonal` 的断言一致。
5. 待本地验证。

#### 4.6.5 小练习与答案

**练习 1**：为什么 `np.diagonal(matrix)` 返回一维 ndarray 而 `matrix.diagonal()` 返回二维 matrix？  
**答案**：`np.diagonal` 是顶层函数，按通用 ndarray 语义设计，返回一维；`matrix.diagonal` 是被 matrix 重写/委托的成员方法，受 `__array_finalize__` 收尾影响保持二维。这是「顶层函数脱壳、成员方法保形」这一普遍规律的缩影。

**练习 2**：`test_keywords` 里 `513` 这个数字怎么来的？  
**答案**：结构化记录 `(1, 2)` 在内存里是两个 int8 字节 `0x01 0x02`，按 little-endian int16 读取就是 `0x0201 = 513`。这条测试顺带验证了 matrix 的 `view` 能正确参与 dtype 重解释。

---

### 4.7 测试入口：PytestTester 与 pytest 选择性运行

#### 4.7.1 概念说明

matrixlib 有两种跑测试的方式：

1. **子包级一行式入口**：`np.matrixlib.test()`——由 `__init__.py` 里 `test = PytestTester(__name__)` 装配（u1-l2 讲过）。它封装了 pytest，自动加上一堆 `-W ignore` 过滤，并返回布尔结果。
2. **直接用 pytest**：`python -m pytest <节点 ID>`——最灵活，能精确到单个方法，适合本讲这种「只跑一个类」的场景。

理解 `PytestTester` 的关键是看它**额外做了什么**：它不是简单转发 pytest，而是针对 numpy（尤其 matrix）的特殊性加了几层警告过滤。

#### 4.7.2 核心流程

```
np.matrixlib.test(label="fast")
   │
   ▼  PytestTester.__call__
组装 pytest_args = ["-l", "-q", "-W ignore:...", ...]
   │  其中两条专门为 matrix 加：
   │    -W ignore:the matrix subclass is not          # 压掉 PendingDeprecationWarning
   │    -W ignore:Importing from numpy.matlib is
   │
   ├─ label=="fast" → 追加 "-m not slow"
   ├─ tests is None → tests = ["numpy.matrixlib"]     # --pyargs
   ▼
pytest.main(pytest_args) → code
return code == 0                                        # 返回布尔
```

#### 4.7.3 源码精读

`PytestTester.__call__` 的关键片段（文件在 matrixlib 之外，用全路径引用）：

[numpy/_pytesttester.py:146-150](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/_pytesttester.py#L146-L150)
> 说明：注释明确写「When testing matrices, ignore their PendingDeprecationWarnings」——因为每次构造 matrix 都会发 `PendingDeprecationWarning`（u1-l1 讲过），如果不过滤，测试输出会被海量警告淹没。这两条 `-W ignore` 是 matrix 测试能「安静跑」的前提。

[numpy/_pytesttester.py:164-165](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/_pytesttester.py#L164-L165)
> 说明：默认 `label="fast"` 会追加 `-m not slow`，所以 `np.matrixlib.test()` 默认只跑非 slow 用例；要跑全量需显式 `label="full"`。

[numpy/_pytesttester.py:182-186](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/_pytesttester.py#L182-L186)
> 说明：`pytest.main` 返回退出码，`return code == 0`——所以 `np.matrixlib.test()` 返回 `True/False`，便于在脚本里 `assert np.matrixlib.test()`。

而 matrixlib 侧的装配就一行：

[__init__.py:9-12](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/__init__.py#L9-L12)
> 说明：`test = PytestTester(__name__)` 把模块名（`numpy.matrixlib`）传进去，`PytestTester` 据此定位测试目录；随后 `del PytestTester` 把类本身从命名空间删掉，避免污染子包公开 API。

#### 4.7.4 代码实践

1. **实践目标**：对比两种运行方式，并练习 pytest 节点 ID 选择。
2. **操作步骤**：
   ```bash
   # 方式 A：子包入口（默认 fast，自动过滤 matrix 警告）
   python -c "import numpy; print(numpy.matrixlib.test())"

   # 方式 B：pytest 直跑单条用例
   python -m pytest "numpy/matrixlib/tests/test_defmatrix.py::TestNewScalarIndexing::test_dimensions" -v

   # 方式 C：用 -k 关键字过滤（跑名字含 row 或 column 的用例）
   python -m pytest "numpy/matrixlib/tests/test_defmatrix.py" -k "row or column" -v
   ```
3. **需要观察的现象**：方式 A 打印一串 `… passed`，最后输出 `True`；方式 B 只跑一条 `test_dimensions`；方式 C 只跑名字匹配的若干条。
4. **预期结果**：三种方式都能让对应用例 PASSED。方式 A 与 B 的核心区别在于：A 自动加了 matrix 警告过滤与 `not slow` 标记，B 是「裸」pytest（会看到 PendingDeprecationWarning，除非也加 `-W ignore`）。
5. 待本地验证。

#### 4.7.5 小练习与答案

**练习 1**：为什么用 `python -m pytest` 直接跑 matrix 测试会刷一堆 `PendingDeprecationWarning`，而 `np.matrixlib.test()` 不会？  
**答案**：因为 `PytestTester` 在 [numpy/_pytesttester.py:146-150](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/_pytesttester.py#L146-L150) 显式追加了 `-W ignore:the matrix subclass is not`。直接用 pytest 时没有这层过滤，matrix 构造时的弃用警告就暴露出来。

**练习 2**：如何只跑「hermitian」标签的 linalg 用例？  
**答案**：`test_matrix_linalg` 里用 `apply_tag('hermitian', …)` 打了标记，可用 `python -m pytest "numpy/matrixlib/tests/test_matrix_linalg.py" -m hermitian` 按 marker 过滤；或用 `--pyargs` + `np.matrixlib.test(label="hermitian")`。

## 5. 综合实践

**任务**：本讲规格里给的核心实践——用 pytest 跑两个具体测试类，对照源码说明它们各自验证的保形规则；再到 `test_regression.py` 里把三张票据对上号。

**步骤**：

1. 在 numpy 仓库根目录执行下面两条命令（待本地验证）：
   ```bash
   python -m pytest \
     "numpy/matrixlib/tests/test_defmatrix.py::TestNewScalarIndexing" -v
   python -m pytest \
     "numpy/matrixlib/tests/test_interaction.py::TestConcatenatorMatrix" -v
   ```

2. **对照源码说明保形规则**：
   - `TestNewScalarIndexing`（[test_defmatrix.py:323-385](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/tests/test_defmatrix.py#L323-L385)）验证的是 u3-l1 讲的**「索引永远二维」规则**——核心在 [defmatrix.py:195-219](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L195-L219) 的 `__getitem__` 与 [defmatrix.py:172-175](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L172-L175) 的 `_getitem` 短路。具体地：
     - `a[0].ndim == 2`、`x[0].shape == (1,3)`：单行索引走「`index` 长度不 >1」分支，`reshape((1, sh))` 成**行向量**。
     - `x[:, 0].shape == (2,1)`：列索引满足 `n > 1 and isscalar(index[1])`，`reshape((sh, 1))` 成**列向量**。
     - `x[0, 0] == 0`：结果 0 维，由 `not isinstance(out, ndarray)` 直接返回 Python 标量。
   - `TestConcatenatorMatrix`（[test_interaction.py:297-328](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/tests/test_interaction.py#L297-L328)）验证的是**「子类型保形 + 二维块布局」规则**——`np.r_` 的 `'r'/'c'` 指令必须返回二维 `matrix`，块语法 `'a, b; c, d'` 必须与 `bmat` 等价（底层就是 [defmatrix.py:1041](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L1041) 的 `bmat`）。
   - **一句话总结对照**：前者从「**索引取值**」入口守住二维不变量，后者从「**拼接构造**」入口守住同一条不变量，两者是同一规则的两侧验收。

3. **在 `test_regression.py` 中对号入座**（[test_regression.py:1-31](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/tests/test_regression.py#L1-L31)）：
   - **Ticket #71** → `test_kron_matrix`：`np.kron(x, x)` 没保住 matrix 子类型，返回了 ndarray。修复后断言 `type(np.kron(x, x)) == type(x)`。
   - **Ticket #125** → `test_matrix_properties`：`.real`/`.imag` 属性意外脱壳成 ndarray；同时 `nonzero()` 的下标数组本就该是 ndarray，一并锁定。
   - **Ticket #473** → `test_matrix_multiply_by_1d_vector`：`matrix * 1D向量` 应抛 `ValueError`（因为 1D 被升成行向量、与矩阵 dot 形状不符）。

**预期结果**：两个测试类全部 PASSED；三张票据的 bug 描述与 4.5 节的表格一致。

## 6. 本讲小结

- matrixlib 的 7 个测试文件分工清晰：**自身行为** → `test_defmatrix`；**跨模块交互** → `test_interaction`；**linalg 数值** → `test_matrix_linalg`；**历史票据** → `test_regression`；**视图/点积/对角** → `test_multiarray`/`test_numeric`；**多重继承** → `test_masked_matrix`。
- `test_defmatrix.py` 用 `TestCtor`/`TestProperties`/`TestIndexing`/`TestNewScalarIndexing` 等类按主题切片；`test_bmat_nondefault_str` 一条用例就覆盖了 `bmat` 的 `ldict`/`gdict` 全组合，其中「只给 `gdict` 抛 TypeError、只给 `ldict` 被忽略」是最值得记住的两个边角。
- `test_interaction.py` 把「matrix 喂给通用函数（sort/kron/nan/r_/nditer）会不会退化」做成验收单；`TestConcatenatorMatrix` 验证 `np.r_` 的 `'r'/'c'` 指令保二维 matrix 子类型。注意 `like_function` 因缺 `test_` 前缀不会被收集——pytest 收集约定的真实反面教材。
- `test_matrix_linalg.py` 用「数据（`LinalgCase` + `MatrixTestCase`）与逻辑（`XxxCases` 基类）分离」的混入设计，让一行 `pass` 的 `TestInvMatrix` 等类自动跑几十条 linalg 用例；`@pytest.mark.thread_unsafe` 是 free-threaded Python 时代的新标记。
- `test_regression.py` 用四条用例固化 Ticket #71（kron 保子类型）、#125（real/imag 保类型）、#473（matrix×1D 抛 ValueError）、#83（无 axis 归约返回标量），是「回归测试即文档」。
- 两种跑法：`np.matrixlib.test()` 经 `PytestTester` 自动过滤 matrix 的 `PendingDeprecationWarning` 并返回布尔；`python -m pytest <节点ID>` 最灵活，能精确到单个方法，可用 `::Class`、`::Class::method`、`-k`、`-m` 选择。

## 7. 下一步学习建议

本讲是 matrixlib 学习手册的最后一篇。建议你：

1. **横向对照 linalg 测试基类**：打开 `numpy/linalg/tests/test_linalg.py`，找到 `InvCases`、`LinalgTestCase`、`LinalgCase`、`apply_tag` 的定义，看它们是如何用 `pytest.generate_tests` / 参数化把 `TEST_CASES` 展开成具体用例的——这是理解 4.4 节「一行 pass 跑几十条」的钥匙。
2. **深入 `test_masked_matrix.py`**：本讲只点到 `MMatrix(MaskedArray, np.matrix)` 多重继承为止。如果要做 matrix 的二次开发或理解 `__array_finalize__` 的「手动双调」（u3-l5 讲过），这个文件是最好的实战教材。
3. **把测试当调试工具**：以后遇到 matrix 的「奇怪行为」拿不准时，第一反应应是 `grep -n` 关键词到 `tests/` 目录找有没有现成用例——例如对 `ravel` 的形状存疑，直接看 `TestShape`（[test_defmatrix.py:399-453](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/tests/test_defmatrix.py#L399-L453)）的断言即可，比读文档更快、更准。
4. **回归到 ndarray 子类化**：matrixlib 是「`ndarray` 子类化」的范本，但它也是「**为何不要子类化 ndarray**」的反面教材（官方正因为这些保形复杂度而弃用 matrix）。学完 matrixlib，建议接着读 numpy 文档的 *Subclassing ndarray* 一章，把 `__array_finalize__`、`__array_wrap__`、`__array_priority__`、`__array_function__` 四件套系统串一遍。
