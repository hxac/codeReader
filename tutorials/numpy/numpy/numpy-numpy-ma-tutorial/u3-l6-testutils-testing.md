# 测试体系与 testutils 工具

## 1. 本讲目标

本讲是专家层的「测试与质量保障」专题。读完后你应该能够：

- 说清楚为什么直接用 `a == b` 比较两个掩码数组不够，以及 `numpy.ma.testutils` 提供的断言是怎么弥补的。
- 掌握 `assert_array_equal` / `assert_equal` 的「合并掩码」机制，以及它由此带来的**宽松比较**特性——并知道为什么必须再配一个 `assert_mask_equal`。
- 会用 `approx` / `almost` 做带容差的掩码数值比较。
- 了解 `numpy/ma/tests/` 目录的组织方式，能照着 `test_core.py` 的风格自己写一条 pytest 用例，并通过 `np.ma.test()` 跑起来。

本讲承接 u2-l1 建立的概念：`nomask` 单例、`mask_or`、`getmask`、`filled`，以及「屏蔽位」的语义。如果你对这几个词还生疏，建议先回顾 u2-l1 与 u1-l4。

## 2. 前置知识

在进入断言源码前，先统一三个会被反复用到的事实（均来自前面的讲义）：

1. **`nomask` 是 `False` 单例**：表示「没有屏蔽」。`getmask(a)` 在数组无屏蔽时返回的就是 `nomask`（即 `False`），而不是一个全 `False` 的布尔数组。这关系到后面 `assert_mask_equal` 为什么先要做身份判断。
2. **`mask_or(m1, m2)` 按位取或**：把两个掩码合并成「任意一侧被屏蔽则屏蔽」的新掩码，且对 `nomask` 有短路优化。
3. **`filled(a, v)`**：把屏蔽位替换成 `v`，返回一个**普通 ndarray**（剥离了 mask）。

一个容易踩的坑：直接对两个 `MaskedArray` 写 `a == b`，得到的是一个**仍带掩码的逐元素比较数组**，而不是单个布尔结论，更不能直接喂给 `assert`。掩码数组需要「掩码感知」的断言，这就是 `testutils.py` 存在的理由。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关心什么 |
|---|---|---|
| `numpy/ma/testutils.py` | 掩码感知断言工具箱 | 全部断言函数的源码 |
| `numpy/ma/tests/test_core.py` | `core.py` 的测试主体（19 个测试类、300+ 方法） | 测试组织风格、断言的真实用法 |
| `numpy/ma/tests/test_regression.py` | Ticket/Issue 驱动的回归测试 | 最小测试写法范例 |
| `numpy/ma/__init__.py` | 包入口 | `test = PytestTester(__name__)` 提供 `np.ma.test()` |

测试目录其余文件（`test_extras.py` / `test_mrecords.py` / `test_subclassing.py` / `test_deprecations.py` / `test_old_ma.py` / `test_arrayobject.py`）按模块一一对应，第 4.4 节会集中讲组织方式。

## 4. 核心概念与源码讲解

### 4.1 assert_equal / assert_array_equal（掩码感知）

#### 4.1.1 概念说明

普通 `numpy.testing.assert_array_equal` 不认识 mask：它只看 `.data`。如果两个掩码数组在某个位置一个是「正常值 5」、另一个是「被屏蔽」，普通断言会用 5 去比 `mask` 标记下藏着的原始值，得到荒谬的结论。

`numpy.ma.testutils` 重新实现了一套同名断言，核心思想只有一句话：**先合并两侧掩码，再比较「填充之后」的纯数组**。其中 `assert_array_compare` 是所有数组级断言的总入口，`assert_array_equal` 只是它的一个特化。

#### 4.1.2 核心流程

`assert_array_compare(comparison, x, y, ..., fill_value=True)` 的执行过程：

1. `m = mask_or(getmask(x), getmask(y))` —— 取两侧掩码的并集，任意一侧屏蔽即屏蔽。
2. 用这个**同一份** `m` 重新包住 `x` 和 `y`（`keep_mask=False` 表示覆盖各自原掩码）。
3. 分别 `x.filled(fill_value)`、`y.filled(fill_value)`，把屏蔽位填成同一个值。
4. 把两个纯 ndarray 交给 `np.testing.assert_array_compare` 做普通逐元素比较。

关键推论（也是本讲最重要的结论）：**在合并掩码为真的位置上，`x` 和 `y` 都被填成了同一个 `fill_value`，于是这些位置恒为「相等」**。换句话说，`assert_array_equal` 对「一侧屏蔽」的位置采取**宽松策略**——只要另一侧的值不影响，就算过。

> ⚠️ 这意味着 `assert_array_equal(a, b)` 通过，**并不能**保证 `a` 与 `b` 的 mask 完全一致。一个位置 `a` 屏蔽、`b` 未屏蔽且取任意值，断言照样通过。要严格比较 mask，必须用 4.2 节的 `assert_mask_equal`。

`assert_equal` 则是更上层的分发器：标量 / 字典 / 列表 / 字符串逐路径处理后，最终落到 `assert_array_equal`。

#### 4.1.3 源码精读

先看总入口 `assert_array_compare`，注意第 210 行的 `mask_or` 与第 219 行的转交：

合并两侧掩码、用同一份掩码重包两侧、最后填充后比较（[testutils.py:L201-L223](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/testutils.py#L201-L223)）：

```python
def assert_array_compare(comparison, x, y, err_msg='', verbose=True, header='',
                         fill_value=True):
    # Allocate a common mask and refill
    m = mask_or(getmask(x), getmask(y))
    x = masked_array(x, copy=False, mask=m, keep_mask=False, subok=False)
    y = masked_array(y, copy=False, mask=m, keep_mask=False, subok=False)
    ...
    # OK, now run the basic tests on filled versions
    return np.testing.assert_array_compare(comparison,
                                           x.filled(fill_value),
                                           y.filled(fill_value), ...)
```

第 211–212 行的 `keep_mask=False` 是「宽松」的根源：它让 `y` 也戴上 `x` 的屏蔽位，于是 `y.filled(fill_value)` 在该位置也被替换成 `fill_value`，与 `x` 一致。

`assert_array_equal` 只是把比较函数固定成 `operator.__eq__`（[testutils.py:L226-L233](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/testutils.py#L226-L233)）：

```python
def assert_array_equal(x, y, err_msg='', verbose=True):
    assert_array_compare(operator.__eq__, x, y,
                         err_msg=err_msg, verbose=verbose,
                         header='Arrays are not equal')
```

上层分发器 `assert_equal`：处理字典/列表/标量后，在末尾落到 `assert_array_equal`（[testutils.py:L114-L150](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/testutils.py#L114-L150)）。其中 L138–L142 对「一侧是 `masked` 单例、另一侧不是」的情况抛 `ValueError`，这是对全局单例的特别保护（`masked` 单例见 u3-l3）。

最后看真实用法。`test_core.py` 里比较两个 ufunc 结果时，**数据与掩码分开断言**（[tests/test_core.py:L2668-L2671](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/tests/test_core.py#L2668-L2671)）：

```python
ur = uf(*args)        # 普通 ufunc 作用于掩码数组
mr = mf(*args)        # 掩码版 ufunc
assert_equal(ur.filled(0), mr.filled(0), f)        # 只比数据
assert_mask_equal(ur.mask, mr.mask, err_msg=f)     # 再单独比掩码
```

这里 `ur.filled(0)` 已经把屏蔽位填成 0、剥离了掩码，所以 `assert_equal` 比的是「纯数据」；掩码是否一致交给下一行的 `assert_mask_equal`。这正是绕开「宽松策略」的标准写法。

#### 4.1.4 代码实践

实践目标：亲手验证 `assert_array_equal` 的宽松行为。

操作步骤（保存为 `~/ma_demo.py` 后用 `python -i` 交互，或直接进 REPL）：

```python
# 示例代码
import numpy as np
from numpy.ma.testutils import assert_array_equal, assert_mask_equal

a = np.ma.array([1.0, 2.0, 3.0], mask=[False, True,  False])
b = np.ma.array([1.0, 9.9, 3.0], mask=[False, False, False])

# a[1] 被屏蔽、b[1]=9.9 未屏蔽 —— 数据并不相同
assert_array_equal(a, b)   # 注意：这一句【不会】抛异常！
print("assert_array_equal 通过：宽松策略放过了 mask 不一致的位置")
```

需要观察的现象：第二个位置 `a` 屏蔽、`b` 是 9.9，两者显然「不同」，但断言通过——因为合并掩码后该位置两侧都被填成同一个值。

预期结果：脚本打印「通过」信息而不抛 `AssertionError`。这反向说明了 4.2 节 `assert_mask_equal` 的必要性。若把 `assert_array_equal(a, b)` 换成 `assert_mask_equal(a.mask, b.mask)`，则会因为 `a.mask` 第二位为 `True`、`b.mask` 第二位为 `False` 而抛异常。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `assert_array_compare` 里第 211–212 行的 `keep_mask=False` 改成 `keep_mask=True`，宽松特性还能成立吗？为什么？

> **答案**：不能。`keep_mask=True` 会保留 `x`、`y` 各自的原始掩码，于是 `x.filled()` 只填 `x` 的屏蔽位、`y.filled()` 只填 `y` 的屏蔽位。当某位置仅一侧屏蔽时，另一侧仍是原始数据，二者不再恒等，宽松放行失效——这其实更「严格」，但不符合本模块「两侧屏蔽位同等对待」的设计意图。

**练习 2**：`assert_equal(np.ma.masked, np.ma.masked)` 会通过吗？`assert_equal(np.ma.masked, 0)` 呢？

> **答案**：前者通过（两侧都是 `masked` 单例，不进 L138 的 `ValueError` 分支）。后者在 [testutils.py:L138-L142](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/testutils.py#L138-L142) 命中 `actual is masked` 而 `desired is not masked`，抛 `ValueError`。

---

### 4.2 assert_mask_equal（严格掩码比较）

#### 4.2.1 概念说明

`assert_mask_equal(m1, m2)` 专门用来比较**两个掩码本身**是否完全一致。它与 `assert_array_equal` 的关系是互补的：

- `assert_array_equal`：比较数据，对屏蔽位宽松。
- `assert_mask_equal`：比较掩码，要求逐位严格相同。

二者合在一起，才能完整断言「两个掩码数组完全相等」。

#### 4.2.2 核心流程

`assert_mask_equal` 只有三步，但顺序很关键：

1. 若 `m1 is nomask`，断言 `m2 is nomask`。
2. 若 `m2 is nomask`，断言 `m1 is nomask`。
3. 调 `assert_array_equal(m1, m2)` 逐位比较。

为什么先把 `nomask` 挑出来？因为 `nomask` 是布尔 `False` 单例，**不是一个布尔数组**。如果直接对 `nomask` 调 `assert_array_equal`，它会被 `np.asanyarray(False)` 包成 0 维数组，与一个 N 维全 `False` 掩码比较会出问题（形状/语义都不对）。所以前两步用身份判断 `is nomask` 把「无屏蔽」这种特例先归一化，再让第 3 步只处理「两侧都是真实布尔数组」的情形。

#### 4.2.3 源码精读

`assert_mask_equal` 全文（[testutils.py:L285-L294](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/testutils.py#L285-L294)）：

```python
def assert_mask_equal(m1, m2, err_msg=''):
    """
    Asserts the equality of two masks.

    """
    if m1 is nomask:
        assert_(m2 is nomask)
    if m2 is nomask:
        assert_(m1 is nomask)
    assert_array_equal(m1, m2, err_msg=err_msg)
```

注意这里的 `assert_` 是从 `numpy.testing` 导入的简单真值断言（见文件顶部 [testutils.py:L13-L19](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/testutils.py#L13-L19) 的 re-export）。还要留意一个细节：这两条 `if` 不是 `elif`，也不是带 `else` 的——当两侧都不是 `nomask` 时，它们都静默跳过，控制权落到第 294 行的 `assert_array_equal`。当一侧是 `nomask` 而另一侧不是时，对应的 `assert_` 会失败并抛 `AssertionError`。

`nomask` 与 `getmask` 来自 core（顶部导入 [testutils.py:L21](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/testutils.py#L21)）：

```python
from .core import filled, getmask, mask_or, masked, masked_array, nomask
```

#### 4.2.4 代码实践

实践目标：体会「`nomask` 与全 `False` 数组」的区别，以及 `assert_mask_equal` 如何守住这条边界。

操作步骤（REPL）：

```python
# 示例代码
import numpy as np
from numpy.ma.testutils import assert_mask_equal
from numpy.ma.core import getmask, nomask

a = np.ma.array([1, 2, 3])                 # 无屏蔽
print(getmask(a) is nomask)                # True —— getmask 返回单例
print(getmask(a))                          # False（即 nomask）

b = np.ma.array([1, 2, 3], mask=[0,0,0])   # 显式全 False 的真实布尔数组
print(getmask(b) is nomask)                # False —— 这次是真实数组
assert_mask_equal(getmask(a), getmask(b))  # 抛 AssertionError！
```

需要观察的现象：`getmask(a)` 是 `nomask`（`False`），`getmask(b)` 是 `shape=(3,)` 的全 `False` 数组。虽然「语义上」都没屏蔽任何元素，但 `assert_mask_equal` 会因第一行 `if m1 is nomask: assert_(m2 is nomask)` 失败而抛异常。

预期结果：前四行打印 `True / False / True / False`；最后一行抛 `AssertionError`。这正是「`nomask` 与全 `False` 数组在掩码体系里是两种不同的内部表示」（见 u2-l1）的可观测体现。如果想让断言通过，需要先对 `a` 做一次会真实化掩码的操作，或对 `b` 调 `shrink_mask()`。

#### 4.2.5 小练习与答案

**练习 1**：`assert_mask_equal` 的两条 `if` 能否合并成 `assert_(m1 is nomask) == (m2 is nomask)`？会有什么不同？

> **答案**：语义上接近，但可读性差且行为不完全等价。原写法在「一侧 `nomask` 一侧不是」时直接抛错，清晰；合并写法依赖 `==` 比较，且仍需保证「两侧都不是 `nomask` 时落到 `assert_array_equal`」。保持原顺序更安全。

**练习 2**：为什么 `assert_mask_equal` 用 `is nomask` 而不是 `== nomask`？

> **答案**：`nomask` 是 `False` 单例，`==` 会触发广播比较：任意「全 `False` 的真实布尔数组」`== False` 都得到全 `True`，于是 `m == nomask` 对真实数组也「为真」（在 `bool(...)` 意义下），无法区分单例与真实数组。`is` 做的是身份判断，只有真正的 `nomask` 单例才匹配，这正是 u2-l1 强调的「`is nomask` 做 O(1) 身份判断」。

---

### 4.3 approx / almost（容差比较）

#### 4.3.1 概念说明

浮点运算有舍入误差，逐位相等不可靠，需要容差。`testutils` 提供两个底层函数：

- `approx(a, b, rtol, atol)`：相对 + 绝对容差，返回逐元素的布尔数组。
- `almost(a, b, decimal)`：按小数位数比较，返回逐元素的布尔数组。

它们都默认 `fill_value=True`，即「两侧都屏蔽的位置视为相等」。`assert_array_almost_equal` / `assert_array_approx_equal` 在内部就是用一个闭包把它们接进 `assert_array_compare`。

#### 4.3.2 核心流程

`approx` 的算法：

1. `m = mask_or(getmask(a), getmask(b))` 合并掩码。
2. `d1 = filled(a)`、`d2 = filled(b)`：先各自用默认填充值剥成普通数组。
3. 若任一侧是 object dtype，退化为逐元素 `np.equal`。
4. 否则把 `d1`、`d2` 重新套上合并掩码 `m`，分别 `filled(...).astype(np.float64)`，统一转成 64 位浮点（避免低精度比较的假阳性）。
5. 判据 \(|x - y| \leq \mathrm{atol} + \mathrm{rtol}\cdot |y|\)，逐元素得到布尔数组并 `ravel`。

`almost` 几乎一样，只是把判据换成按小数位：四舍五入到 `decimal` 位后判断 \(|x - y| \leq 10^{-\text{decimal}}\)。

#### 4.3.3 源码精读

`approx`（[testutils.py:L45-L66](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/testutils.py#L45-L66)）：

```python
def approx(a, b, fill_value=True, rtol=1e-5, atol=1e-8):
    m = mask_or(getmask(a), getmask(b))
    d1 = filled(a)
    d2 = filled(b)
    if d1.dtype.char == "O" or d2.dtype.char == "O":
        return np.equal(d1, d2).ravel()
    x = filled(masked_array(d1, copy=False, mask=m), fill_value).astype(np.float64)
    y = filled(masked_array(d2, copy=False, mask=m), 1).astype(np.float64)
    d = np.less_equal(umath.absolute(x - y), atol + rtol * umath.absolute(y))
    return d.ravel()
```

注意第 61–64 行的小细节：`x` 在屏蔽位填 `fill_value`（默认 `True`，对浮点即 `1.0`），`y` 在屏蔽位填 `1`。由于合并掩码 `m` 同时作用于两侧，两侧屏蔽位都被填掉、不参与「真实数据」比较，所以这里 `x` 用 `fill_value`、`y` 用 `1` 不会破坏结果——非屏蔽位才是真正被比较的。

`almost`（[testutils.py:L69-L87](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/testutils.py#L69-L87)）与 `approx` 同构，差异只在第 86 行的判据：

```python
d = np.around(np.abs(x - y), decimal) <= 10.0 ** (-decimal)
```

再看它们如何被「断言化」。`assert_array_almost_equal` 把 `almost` 包进闭包 `compare`，交给 `assert_array_compare`（[testutils.py:L261-L272](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/testutils.py#L261-L272)）：

```python
def assert_array_almost_equal(x, y, decimal=6, err_msg='', verbose=True):
    def compare(x, y):
        return almost(x, y, decimal)
    assert_array_compare(compare, x, y, err_msg=err_msg, verbose=verbose,
                         header='Arrays are not almost equal')
```

于是 `assert_array_almost_equal` 同样继承了 4.1 节的「合并掩码 + 填充」框架，只是把 `operator.__eq__` 换成了带容差的 `almost`。

#### 4.3.4 代码实践

实践目标：验证 `approx` 在「两侧都屏蔽」与「一侧屏蔽」时的不同行为。

操作步骤（REPL）：

```python
# 示例代码
import numpy as np
from numpy.ma.testutils import approx

x = np.ma.array([1.0,      2.0, 3.0], mask=[False, True,  False])
y = np.ma.array([1.000001, 9.9, 3.0], mask=[False, True,  False])
print(approx(x, y))   # 第二位两侧都屏蔽 -> 视为相等(True)；其余按容差判断
```

需要观察的现象：第二位两边都屏蔽，`approx` 在该位返回 `True`（合并掩码后两侧同填）；第一位 `|1.0 - 1.000001| = 1e-6`，在默认 `atol=1e-8, rtol=1e-5` 下，\(1\text{e-}6 \leq 1\text{e-}8 + 1\text{e-}5\times|1.000001|\approx 1\text{e-}5\) 成立，故为 `True`。

预期结果：打印 `[ True  True  True]`。若把 `y[0]` 改成 `1.1`（差 0.1，远超容差），第一位会变 `False`。**待本地验证**：不同 numpy 版本默认 `rtol/atol` 不变，结果应稳定。

#### 4.3.5 小练习与答案

**练习 1**：`approx` 为什么要把 `x`、`y` 都 `astype(np.float64)`？

> **答案**：避免低精度 dtype（如 `float32`）在 `x - y`、`absolute` 时放大相对误差，造成「本应相等却被判不等」的假阳性。统一升到 `float64` 给容差比较一个稳定的数值平台。

**练习 2**：`approx` 默认 `fill_value=True`。如果把某次调用写成 `approx(a, b, fill_value=False)`，对「两侧都屏蔽」的位置会发生什么？

> **答案**：`fill_value=False` 改变的是屏蔽位被填的值（`x` 填 `False` 即 0，`y` 填 1）。由于屏蔽位两侧填的值不再相同，`|x-y|` 在该位可能为 1，超过容差而返回 `False`——即「屏蔽位视为不等」。这与 docstring「`fill_value=True` 时屏蔽值视为相等，否则视为不等」一致。

---

### 4.4 tests 目录组织

#### 4.4.1 概念说明

`numpy/ma/tests/` 是一套标准的 pytest 测试套件，**与源码模块一一对应**：`core.py` ↔ `test_core.py`、`extras.py` ↔ `test_extras.py`、`mrecords.py` ↔ `test_mrecords.py`。回归与弃用分别有专属文件。理解这套组织方式，是为了让你能快速定位「某个行为的测试在哪」以及「该往哪加新测试」。

#### 4.4.2 核心流程（文件分工）

| 文件 | 测试对象 | 规模 | 典型测试类 |
|---|---|---|---|
| `test_core.py` | `core.py`：`MaskedArray`、掩码 ufunc、归约等 | 19 类 / 313 方法 | `TestMaskedArray`、`TestUfuncs`、`TestFillingValues` |
| `test_extras.py` | `extras.py`：`average`/`median`/集合运算等 | 14 类 / 91 方法 | `TestAverage`、`TestArraySetOps` |
| `test_mrecords.py` | `mrecords.py`：字段级屏蔽 | 3 类 / 25 方法 | `TestMRecords` |
| `test_subclassing.py` | 子类化与 `mvoid` | 15 方法 | `TestSubclassing` |
| `test_regression.py` | Ticket/Issue 回归 | 13 方法 | `TestRegression` |
| `test_deprecations.py` | 弃用路径 | 3 类 | `TestArgsort`、`TestMinimumMaximum` |
| `test_old_ma.py` | 旧 `ma` 模块兼容 | — | — |
| `test_arrayobject.py` | 数组对象零散用例 | — | 顶层 `test_*` 函数 |

注：方法数为近似值，随版本变化。

测试入口由包的 `__init__.py` 提供（[__init__.py:L50-L53](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/__init__.py#L50-L53)）：

```python
from numpy._pytesttester import PytestTester

test = PytestTester(__name__)
del PytestTester
```

这使得 `np.ma.test()` 可以直接运行整个子包的测试；末尾 `del PytestTester` 是为了不把这个名字泄漏到 `np.ma` 命名空间。

#### 4.4.3 源码精读：最小测试范例

`test_regression.py` 是最干净的入门范本——顶层 import、一个类、多个 `test_*` 方法，每个方法对应一个历史 ticket（[tests/test_regression.py:L1-L10](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/tests/test_regression.py#L1-L10)）：

```python
import numpy as np
from numpy.testing import assert_, assert_array_equal


class TestRegression:
    def test_masked_array_create(self):
        # Ticket #17
        x = np.ma.masked_array([0, 1, 2, 3, 0, 4, 5, 6],
                               mask=[0, 0, 0, 1, 1, 1, 0, 0])
        assert_array_equal(np.ma.nonzero(x), [[1, 2, 6, 7]])
```

`test_core.py` 则展示了「把测试用的构造数据抽成 `_create_data` 辅助方法、断言从 `numpy.ma.testutils` 导入」的大规模组织风格（[tests/test_core.py:L134-L143](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/tests/test_core.py#L134-L143)）：

```python
from numpy.ma.testutils import (
    assert_,
    assert_almost_equal,
    assert_array_equal,
    assert_equal,
    assert_equal_records,
    assert_mask_equal,
    assert_not_equal,
    fail_if_equal,
)
```

可以看到，真实测试**同时**导入了 `assert_array_equal`（比数据）和 `assert_mask_equal`（比掩码），印证了 4.1 / 4.2 两节的互补关系。

#### 4.4.4 代码实践

实践目标：把整套测试跑起来，并精确定位某一个测试。

操作步骤：

1. 跑整个子包：在仓库根目录执行 `python -c "import numpy.ma; numpy.ma.test()"`，或直接 `python -m pytest numpy/ma/tests/`。
2. 只跑 `test_core.py` 里的一个类：`python -m pytest numpy/ma/tests/test_core.py::TestUfuncs`。
3. 只跑一个方法：`python -m pytest numpy/ma/tests/test_core.py::TestUfuncs::test_basic1d`。

需要观察的现象：第 1 步输出大量 `.` 或 `PASSED`；第 2、3 步用例数依次减少，证明 pytest 的 `文件::类::方法` 三级寻址生效。

预期结果：在当前 HEAD（`b21650c4f6`）下，这些用例应全部通过。若你的环境缺少编译好的 numpy 扩展，`np.ma.test()` 可能无法运行——此时改用 `python -m pytest` 并以「待本地验证」记录实际输出。

#### 4.4.5 小练习与答案

**练习 1**：新发现一个 `extras.py` 的 bug，应该把回归测试加到哪个文件？为什么？

> **答案**：功能性 bug 加到 `tests/test_extras.py` 对应的测试类（如 `TestArraySetOps`）；如果是为了锁住某个已修复的 GitHub issue 不再复发，则更适合加到 `tests/test_regression.py`，并在注释里写明 issue 号，与该文件「每条测试对应一个 ticket」的风格一致。

**练习 2**：`np.ma.test()` 是怎么找到所有测试文件的？

> **答案**：`PytestTester(__name__)` 把 `numpy.ma` 作为根包交给 pytest，pytest 按约定收集其下的 `tests/` 目录里所有 `test_*.py` 文件、`Test*` 类、`test_*` 方法。无需手动注册。

---

## 5. 综合实践

把本讲的四个模块串起来：写一个完整的 pytest 文件，**故意暴露 `assert_array_equal` 的宽松陷阱，再用 `assert_mask_equal` 补上**。

在仓库根目录新建 `numpy/ma/tests/test_my_demo.py`（仅用于练习，练习后可删除）：

```python
# 示例代码（练习用，非项目原有文件）
import numpy as np
import pytest
from numpy.ma.testutils import (
    assert_array_equal, assert_mask_equal, assert_array_almost_equal,
)


def _pair():
    # 两个数组：第二个位置一个屏蔽、一个未屏蔽
    a = np.ma.array([1.0, 2.0, 3.0], mask=[False, True,  False])
    b = np.ma.array([1.0, 9.9, 3.0], mask=[False, False, False])
    return a, b


def test_data_equal_but_mask_differs_passes_lax_assertion():
    a, b = _pair()
    # 数据层面：b[1]=9.9 与 a[1]（屏蔽）—— 宽松策略下居然「相等」
    assert_array_equal(a, b)


def test_mask_strict_check_catches_the_difference():
    a, b = _pair()
    with pytest.raises(AssertionError):
        assert_mask_equal(a.mask, b.mask)


def test_tolerance_and_merged_mask():
    x = np.ma.array([1.0, 2.0], mask=[False, True])
    y = np.ma.array([1.0000001, 9.9], mask=[False, True])  # 第二位两侧都屏蔽
    assert_array_almost_equal(x, y, decimal=5)              # 容差内 + 屏蔽位放行
```

操作步骤与现象：

1. `python -m pytest numpy/ma/tests/test_my_demo.py -v`。
2. **预期结果**：三条测试全部通过。
   - 第 1 条通过，验证了 4.1 节「宽松策略」——这正是初学者最容易误以为「断言过了就万事大吉」的陷阱。
   - 第 2 条通过（用 `pytest.raises` 捕获了 `AssertionError`），证明只要掩码不同，`assert_mask_equal` 必然报警——这是 4.2 节的严格性。
   - 第 3 条通过，验证了 4.3 节「合并掩码 + 容差」。

3. **思考题**：如果把第 1 条的 `assert_array_equal(a, b)` 当作「a 与 b 完全相等」的唯一判据，会在什么场景下埋下 bug？

> 参考结论：在「两个掩码数组应严格一致」的回归测试中（例如 ufunc 两条路径 `ma.op` 与 `np.op` 的等价性验证，见 [tests/test_core.py:L2668-L2671](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/tests/test_core.py#L2668-L2671)），单用 `assert_array_equal` 会漏掉掩码不一致的回归。正确做法是数据与掩码分开断言，正如 `test_core.py` 所示范。

练习完成后请删除 `test_my_demo.py`，避免污染测试套件。

## 6. 本讲小结

- `numpy.ma.testutils` 之所以存在，是因为普通 `numpy.testing` 断言不认识 mask；其核心套路是「合并两侧掩码 → 用同一份掩码重包 → 填充 → 比较纯 ndarray」。
- `assert_array_compare`（[testutils.py:L201-L223](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/testutils.py#L201-L223)）是总入口，`assert_array_equal` / `assert_array_less` / `assert_array_almost_equal` 都只是换一个比较函数的特化。
- **重要陷阱**：`assert_array_equal` 对「一侧屏蔽」的位置宽松放行，因此它通过 ≠ 掩码一致；严格比较掩码必须用 `assert_mask_equal`。
- `assert_mask_equal`（[testutils.py:L285-L294](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/testutils.py#L285-L294)）先用 `is nomask` 归一化「无屏蔽」特例，再逐位比较，能区分 `nomask` 单例与全 `False` 数组。
- `approx` / `almost` 提供「合并掩码 + 容差」的浮点比较，是 `assert_array_almost_equal` / `assert_array_approx_equal` 的底层引擎。
- 测试按源码模块一一对应组织在 `numpy/ma/tests/`，`np.ma.test()`（[__init__.py:L52](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/__init__.py#L52)）由 `PytestTester` 提供入口，真实写法参考 `test_core.py` / `test_regression.py`。

## 7. 下一步学习建议

- **横向对比**：把本讲的 `assert_array_compare` 与 `numpy.testing._private.utils.assert_array_compare` 对照阅读，体会 ma 版本多了「合并掩码」一层后的取舍。
- **向上一层**：阅读 `tests/test_core.py::TestUfuncs`（约 [tests/test_core.py:L2636](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/tests/test_core.py#L2636) 起），看一个测试类如何系统化地遍历所有 ufunc、并用 `assert_equal` + `assert_mask_equal` 双断言锁定「两条调用路径等价」（呼应 u2-l4 / u2-l5）。
- **回归视角**：浏览 `tests/test_regression.py`，学习「每条测试对应一个 ticket/issue」的最小回归测试范式。
- **手册内衔接**：本讲是 u3 的测试专题；若想理解被测对象本身的高级机制，可继续 u3-l1（硬/软/共享掩码）、u3-l2（子类化与 mvoid）。
