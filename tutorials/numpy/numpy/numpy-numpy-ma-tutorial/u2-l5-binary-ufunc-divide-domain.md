# 掩码二元运算与除法域

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 `numpy.ma` 里 `add`、`subtract`、`multiply`、`divide` 这类二元运算是「怎么被改造成掩码版本」的——即 `_MaskedBinaryOperation` 这个包装器的 `__call__` 做了什么，以及它和上一讲的 `_MaskedUnaryOperation` 有什么本质区别。
- 解释**普通二元运算**的结果 mask 为何只来自两侧输入 mask 的 `logical_or`（**不**自动屏蔽结果里的 `inf`/`nan`），而**带域的二元运算**（`divide`/`floor_divide`/`remainder`/`fmod`）的结果 mask 却有**四个来源**。
- 读懂 `_DomainSafeDivide` 那一行看起来「反直觉」的判断式 `|a|*tiny >= |b|`，并能解释它为何能精确地屏蔽「除以（近）零」。
- 区分两个填充值 `fillx` / `filly` 的两条用途：一是注册进全局表 `ufunc_fills` 供 `__array_wrap__` 路径使用，二是作为 `reduce` / `accumulate` 的**幺元（identity element）**。
- 会用 `reduce` / `outer` / `accumulate` 做掩码归约，并指出掩码归约用的是 `logical_and`、带域的二元运算**没有** `reduce`/`outer`/`accumulate`、比较类运算的 `reduce` 被**显式置为 `None`** 这些设计取舍。

## 2. 前置知识

本讲假设你已经掌握下面这些概念（来自前置讲义）：

- **掩码数组三件套**：`data`（全部原始值，含坏值）、`mask`（同形状布尔数组，`True` 表示屏蔽，无屏蔽时压缩为单例 `nomask`）、`fill_value`。见 u1-l4。
- **`nomask` 单例**：代表「无屏蔽」的省内存标记，就是 `False`；全库用 `is nomask` 做 O(1) 身份判断，而 `getmaskarray` 永远返回同形状全 False 数组。见 u2-l1。
- **`getdata` / `getmask` / `getmaskarray` / `filled`**：模块级取值与填充函数。见 u1-l4、u2-l1。
- **一元掩码 ufunc 包装器 `_MaskedUnaryOperation` 与「域(domain)」概念**：上一讲的核心。本讲是它的「二元版本」与「域」在除法上的应用。**建议先读完 u2-l4 再读本讲。** 见 u2-l4。
- **两条调用路径**：`ma.op(a, b)` 走包装器的 `__call__`；`np.op(a, b)`（`a`/`b` 是掩码数组）走 `MaskedArray.__array_wrap__` 钩子。两者共用同一份全局注册表 `ufunc_domain` / `ufunc_fills`。见 u2-l4。

补充两个本讲会用到的术语：

- **二元 ufunc（binary ufunc）**：接受两个数组输入的 ufunc，如 `umath.add`、`umath.divide`。NumPy 的 `umath` 模块为每个运算符提供了底层 C 实现。
- **幺元（identity element）**：使某运算「等于没算」的特殊值。加法的幺元是 `0`（`x + 0 = x`），乘法的幺元是 `1`（`x * 1 = x`）。归约 `reduce` 必须知道幺元才能从轴的第一个元素之前「空着」起步。

## 3. 本讲源码地图

本讲全部内容集中在 `numpy/ma/core.py` 一个文件里：

| 代码位置 | 作用 |
|---|---|
| [_DomainSafeDivide（core.py:891-909）](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L891-L909) | 「安全除法」的域检查器。被 `divide`/`floor_divide`/`remainder`/`fmod` 共用，负责屏蔽除以（近）零。 |
| [_MaskedBinaryOperation（core.py:1029-1107）](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L1029-L1107) | 普通二元运算包装器。`__call__` 用 `logical_or` 合并两侧 mask。 |
| [reduce / outer / accumulate（core.py:1109-1172）](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L1109-L1172) | 归约、外积、累加三个方法，定义在 `_MaskedBinaryOperation` 上。 |
| [_DomainedBinaryOperation（core.py:1175-1247）](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L1175-L1247) | 带域的二元运算包装器。`__call__` 在普通二元版基础上额外叠加 domain 检查。**没有** reduce/outer/accumulate。 |
| [二元 ufunc 注册区（core.py:1289-1324）](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L1289-L1324) | 把 `umath.add` 等逐个包成掩码版并赋值给模块级名字；`divide` 等除法族用 `_DomainedBinaryOperation`。 |
| [get_masked_subclass（core.py:693-717）](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L693-L717) | 决定运算结果应当是哪个（子）类：取两侧「最年轻」的 MaskedArray 子类。 |
| [__array_wrap__（core.py:3156-3201）](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3156-L3201) | 另一条调用路径：`np.divide(a, b)` 时 NumPy 走这个钩子，查 `ufunc_fills` 取 `filly` 填充域违例位置。 |

## 4. 核心概念与源码讲解

### 4.1 二元掩码运算包装器：`_MaskedBinaryOperation` 与 mask 的 `logical_or` 合并

#### 4.1.1 概念说明

上一讲我们看的是一元包装器 `_MaskedUnaryOperation`：一个输入、一个 `data`、一个 mask。二元运算的难点在于——**现在有两个输入**。

`ma.add(x, y)` 要同时考虑 `x` 和 `y`：`x` 的某个位置被屏蔽了、`y` 的某个位置被屏蔽了、或者两边都没屏蔽。结果的 mask 该怎么定？`numpy.ma` 的规则非常朴素且符合直觉：

> **只要参与运算的任意一侧在某位置被屏蔽，结果在该位置就被屏蔽。**

用布尔逻辑写出来就是**按位或**（`logical_or`）：`result_mask = mask_x | mask_y`。这就是本模块的核心——`_MaskedBinaryOperation.__call__` 干的活。

这里有一个**容易被忽略、但非常关键**的差异，值得提前点明（4.1.3 会用源码证实）：

- 上一讲的一元包装器，结果 mask 有**三个来源**：结果非有限、输入越域、输入已屏蔽。
- 本讲的**普通**二元包装器 `_MaskedBinaryOperation`，结果 mask **只有两个来源**：`mask_a` 和 `mask_b`。它**不检查结果是否为 `inf`/`nan`**。
- 也就是说：`ma.add(huge, huge)` 即使溢出成 `inf`，那个位置也**不会被自动屏蔽**（只要两个输入都没屏蔽）。要想让「算出来是坏值」也被屏蔽，得用**带域的**包装器（4.3 节）。

#### 4.1.2 核心流程

`__call__(a, b, *args, **kwargs)` 的执行流程伪代码：

```
da, db = getdata(a), getdata(b)        # 取出两侧原始数据
result = self.f(da, db, ...)           # 先照常算（屏蔽位照算，可能产生垃圾）

ma, mb = getmask(a), getmask(b)
m = ma | mb                            # ★ 核心：两侧 mask 取或合并
                                       # 含 nomask 短路优化（见 4.1.3）

if result 是标量:
    return masked if m else result     # 标量结果：屏蔽则返回 masked 单例

# result 是数组：
np.copyto(result, da, where=m)         # 把屏蔽位的 result 回填成 da（避免暴露垃圾值）
masked_result = result.view(get_masked_subclass(a, b))   # 包装成 MaskedArray（子）类
masked_result._mask = m
masked_result._update_from(a 或 b)     # 搬运 fill_value 等簿记属性
return masked_result
```

注意最后一步 `get_masked_subclass(a, b)`：结果类型不是写死的 `MaskedArray`，而是两侧里「最年轻」的子类。这让用户自定义的 `MaskedArray` 子类在参与运算后仍保留其类型（详见 4.4 与 u3-l2）。

#### 4.1.3 源码精读

先看构造函数，它定义了 `fillx`/`filly`（4.2 节详述）并把该 ufunc 登记进两张全局表：

[core.py:1049-1060](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L1049-L1060) — `_MaskedBinaryOperation.__init__`。`ufunc_domain[mbfunc] = None` 表示「普通二元运算没有域」，`ufunc_fills[mbfunc] = (fillx, filly)` 把两个填充值记进全局表。

```python
def __init__(self, mbfunc, fillx=0, filly=0):
    super().__init__(mbfunc)
    self.fillx = fillx
    self.filly = filly
    ufunc_domain[mbfunc] = None          # 普通二元运算：无域
    ufunc_fills[mbfunc] = (fillx, filly) # 登记填充值，供 __array_wrap__ 取用
```

然后是 `__call__` 主体——本模块的主角。注意 mask 合并那段有**三条路径**，本质都是为了在「某侧是 `nomask`」时省一次 `logical_or`：

[core.py:1062-1107](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L1062-L1107) — `_MaskedBinaryOperation.__call__`：取两侧 data 照常算；用 `logical_or` 合并两侧 mask（含 nomask 短路）；标量返回 `masked` 单例；数组则 `copyto` 把屏蔽位回填成 `da`，再 `view` 成子类、挂上 mask。

```python
def __call__(self, a, b, *args, **kwargs):
    (da, db) = (getdata(a), getdata(b))                 # 取两侧 data
    with np.errstate():
        np.seterr(divide='ignore', invalid='ignore')
        result = self.f(da, db, *args, **kwargs)        # 照常算，屏蔽位也会算
    (ma, mb) = (getmask(a), getmask(b))                 # 取两侧 mask（可能 nomask）
    if ma is nomask:
        if mb is nomask:
            m = nomask                                  # 两边都没屏蔽：直接 nomask
        else:
            m = umath.logical_or(getmaskarray(a), mb)   # a 无屏蔽，b 有
    elif mb is nomask:
        m = umath.logical_or(ma, getmaskarray(b))       # a 有屏蔽，b 无
    else:
        m = umath.logical_or(ma, mb)                    # 两边都有：取或

    if not result.ndim:                                 # 标量结果
        if m:
            return masked
        return result

    if m is not nomask and m.any():                     # 数组结果：屏蔽位回填 da
        try:
            np.copyto(result, da, casting='unsafe', where=m)
        except Exception:
            pass

    masked_result = result.view(get_masked_subclass(a, b))  # 包装成（子）类
    masked_result._mask = m
    if isinstance(a, MaskedArray):
        masked_result._update_from(a)
    elif isinstance(b, MaskedArray):
        masked_result._update_from(b)
    return masked_result
```

这段代码证实了 4.1.1 的论断：**`m` 只来自 `mask_a | mask_b`，全程没有任何 `~isfinite(result)` 检查**。所以 `ma.add` 不会因为「和溢出成 `inf`」而屏蔽；只有输入本身被屏蔽的位置，结果才被屏蔽。

`np.copyto(result, da, casting='unsafe', where=m)` 这一行值得单独解释：屏蔽位上 `result` 已经被 `self.f` 算出了某个垃圾值（比如两个屏蔽数相加得到某个无意义数），这里把屏蔽位的 `result` **强行改回左侧输入 `da` 的原值**。这样虽然 `.data` 在屏蔽位上是「假数据」，但至少是「一个确定、可控、不抛异常的值」，便于后续可能的无警告计算。`try/except` 包裹是为了在某些极端类型下 `copyto` 失败时也不至于崩掉——注释写得很坦白：「any errors, just abort; impossible to guarantee masked values」。

> **对比记忆**：一元包装器屏蔽位回填的是原始 `data`（u2-l4）；普通二元包装器屏蔽位回填的也是左侧 `da`；带域二元包装器（4.3）回填的是 `da` 但走 `multiply`/`can_cast` 的另一条路径。三者目的相同——屏蔽位上别留 `nan`/`inf` 以免触发警告。

#### 4.1.4 代码实践

**实践目标**：亲眼看到「普通二元运算的 mask 只来自两侧输入 mask，与结果是否 `inf`/`nan` 无关」。

**操作步骤**：

1. 构造两个**都不含屏蔽**的数组，让它们的和**溢出成 `inf`**，用 `ma.add` 相加，检查结果 mask 与 `.data`。
2. 构造两个掩码数组，一个在某位置屏蔽、另一个不屏蔽，相加，验证 mask 在该位置为 `True`。

```python
import numpy as np
import numpy.ma as ma

# 步骤 1：和会溢出成 inf，但两个输入都没屏蔽
big = np.array([1e308, 1.0])
r1 = ma.add(big, big)
print("r1.data =", r1.data)      # 预期 [inf, 2.0]
print("r1.mask =", r1.mask)      # 预期 False（nomask 展开为全 False）——证明不因 inf 而屏蔽

# 步骤 2：一侧屏蔽
a = ma.array([1.0, 2.0, 3.0], mask=[0, 1, 0])
b = ma.array([10.0, 20.0, 30.0])
r2 = ma.add(a, b)
print("r2.data =", r2.data)      # 屏蔽位回填成 da=2.0
print("r2.mask =", r2.mask)     # 预期 [False, True, False]
```

**需要观察的现象**：

- 步骤 1 中 `1e308 + 1e308 = inf`，但 `r1.mask` 全是 `False`——`ma.add` **没有**因为结果是 `inf` 而屏蔽。
- 步骤 2 中 `a` 在索引 1 被屏蔽、`b` 没有屏蔽，结果在索引 1 被**继承性屏蔽**；该位置的 `.data` 是 `da` 的值 `2.0`（而不是 `2.0+20.0=22.0`），因为 `copyto` 把屏蔽位回填成了左侧 `da`。

**预期结果**：`r1.mask` 为 `nomask`（打印为 `False`）；`r2.mask` 为 `[False, True, False]`，`r2.data` 的屏蔽位为 `2.0`。具体打印格式（`inf` 的大小写、`nomask` 是否显示为 `False`）待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：`ma.add(a, b)` 中，若 `a` 全屏蔽、`b` 全不屏蔽，结果 mask 是什么？为什么 `.data` 的屏蔽位等于 `a.data` 而不是 `a.data + b.data`？

> **答案**：结果 mask 全为 `True`（`mask_a | mask_b`，`mask_a` 全 `True`）。`.data` 屏蔽位等于 `a.data`，因为 `np.copyto(result, da, where=m)` 把所有屏蔽位的 `result` 强行改回了左侧输入 `da` 的值。

**练习 2**：`a = ma.array([1e308]); print(ma.add(a, a).mask)`。结果是 `True` 还是 `False`？用本节结论解释。

> **答案**：`False`（`nomask`）。`1e308 + 1e308 = inf`，但普通二元运算**不检查**结果是否有限，mask 只来自两侧输入 mask，两侧都没屏蔽，故结果不屏蔽。

---

### 4.2 fillx / filly：填充值的两条用途

#### 4.2.1 概念说明

构造 `_MaskedBinaryOperation` 时可以传两个填充值：`fillx`（第一个参数的填充值，默认 `0`）和 `filly`（第二个参数的填充值，默认 `0`）。乍看平淡无奇，但它们服务于**两个完全不同的机制**，混淆它们是初学常见误区：

1. **「`np.op(masked_array, ...)` 路径」的填充契约**：这两个值被登记进全局表 `ufunc_fills`。当你写 `np.divide(a, b)`（而不是 `ma.divide`）时，NumPy 走 `__array_wrap__` 钩子，从 `ufunc_fills` 取值来填充域违例的位置。这是和 u2-l4 一脉相承的「两条路径共享一张表」设计。

2. **「`reduce` / `accumulate`」的幺元**：归约时屏蔽位会被 `filled(target, self.filly)` 用 `filly` 填掉再参与累加。为了让归约结果「等于屏蔽位不存在」，`filly` 必须是运算的**幺元**——加法用 `0`、乘法用 `1`。这就是为什么 `multiply` 注册时要特意写 `fillx=1, filly=1`（见 4.2.3）。

注意：在 `_MaskedBinaryOperation.__call__`（4.1 节的 `ma.add(a,b)` 路径）里，`fillx`/`filly` **并没有被直接使用**——那条路径靠 `copyto` 把屏蔽位回填成 `da`。它们的价值完全体现在上面这两条「别处」。

#### 4.2.2 核心流程

两条用途的流向：

```
用途①（np 路径）：
  np.divide(a, b)  ──>  __array_wrap__  ──>  查 ufunc_fills[divide]
                                              取二元组的「最后一个」= filly
                                              用 filly 填充域违例的 result 位置

用途②（reduce/accumulate）：
  ma.add.reduce(target)  ──>  filled(target, self.filly)   # 屏蔽位换成 filly=0
                              self.f.reduce(...)            # 普通 ufunc 归约
                              mask 用 logical_and 归约（见 4.4）
```

为什么「取最后一个」？因为二元运算的第二个参数通常是「被除数」「除数」这类**引发域违例的那一侧**（除法的危险来自除数 `b`），所以用 `filly` 填充最合理。源码在 `__array_wrap__` 里就是这么写的（见 4.2.3）。

#### 4.2.3 源码精读

先看 `multiply` 为什么要特意传 `1, 1`：

[core.py:1290-1293](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L1290-L1293) — 二元 ufunc 注册区。`add`/`subtract` 用默认的 `fillx=0, filly=0`；`multiply` 特意传 `1, 1`（乘法幺元），`arctan2` 传 `0.0, 1.0`。

```python
add = _MaskedBinaryOperation(umath.add)                  # fillx=0, filly=0
subtract = _MaskedBinaryOperation(umath.subtract)
multiply = _MaskedBinaryOperation(umath.multiply, 1, 1)  # ★ 乘法幺元 1，供 reduce
arctan2 = _MaskedBinaryOperation(umath.arctan2, 0.0, 1.0)
```

再看 `__array_wrap__` 如何**取二元组的最后一个元素**当作填充值（用途①的落点）：

[core.py:3176-3185](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3176-L3185) — `__array_wrap__` 里取域填充值：二元域取 `ufunc_fills[func][-1]`（即 `filly`），一元域取整个 `ufunc_fills[func]`，然后用它 `copyto` 填充域违例位置。

```python
try:
    # Binary domain: take the last value
    fill_value = ufunc_fills[func][-1]      # ★ 二元域取 filly（除法取 1）
except TypeError:
    # Unary domain: just use this one
    fill_value = ufunc_fills[func]
except KeyError:
    # Domain not recognized, use fill_value instead
    fill_value = self.fill_value

np.copyto(result, fill_value, where=d)
```

用途②（reduce 里把屏蔽位换成 `filly`）的源码在 4.4.3 节贴出，核心一行是 `t = filled(target, self.filly)`。

#### 4.2.4 代码实践

**实践目标**：体会「`filly` 是幺元」对归约的影响，并对比 `add.reduce` 与 `multiply.reduce`。

**操作步骤**：

```python
import numpy.ma as ma

a = ma.array([1, 2, 3, 4], mask=[0, 1, 0, 0])   # 索引 1 屏蔽
print("add.reduce      =", ma.add.reduce(a))      # 1+0+3+4 = 8（屏蔽位填 filly=0）
print("multiply.reduce =", ma.multiply.reduce(a)) # 1*1*3*4 = 12（屏蔽位填 filly=1）
print("结果 mask 是否为 True？", ma.add.reduce(a).mask)
```

**需要观察的现象**：

- `add.reduce` 把屏蔽位当成 `0`，结果 `1+0+3+4 = 8`。
- `multiply.reduce` 把屏蔽位当成 `1`（因为 `multiply` 的 `filly=1`），结果 `1*1*3*4 = 12`。
- 两者结果的 mask 都是 `False`——因为「只要轴上有一个元素未屏蔽，归约结果就不屏蔽」（4.4 详述）。

**预期结果**：`add.reduce` 给 `8`，`multiply.reduce` 给 `12`，结果 mask 均为 `False`。若你把 `multiply` 也按默认 `filly=0` 注册，乘积就会变成 `0`——这正是源码特意传 `1, 1` 的原因。

#### 4.2.5 小练习与答案

**练习 1**：如果有人错误地把 `multiply` 注册成 `_MaskedBinaryOperation(umath.multiply)`（不传 `1, 1`），`ma.multiply.reduce(ma.array([2,3,4], mask=[0,1,0]))` 会得到什么？

> **答案**：会得到 `0`。因为 `filly` 取默认 `0`，屏蔽位被当成 `0`，`2*0*4 = 0`。正确的注册传 `filly=1`，屏蔽位当 `1`，得 `2*1*4 = 8`。这就是 `fillx`/`filly` 作为「幺元」的用途。

**练习 2**：`np.divide(a, b)` 走 `__array_wrap__`，域违例位置被填成什么值？为什么是 `filly` 而不是 `fillx`？

> **答案**：填成 `ufunc_fills[divide][-1]` = `filly` = `1`。因为除法的域违例来自**除数**（第二个参数 `b`），用 `filly` 填充最贴近「把危险的那一侧替换成安全值」的语义；源码用 `[-1]` 取二元组最后一个正是此意。

---

### 4.3 带域的二元运算：`_DomainedBinaryOperation` 与 `_DomainSafeDivide`

#### 4.3.1 概念说明

4.1 节的 `_MaskedBinaryOperation` 有一个明显短板：它**只传播输入 mask，不识别「运算本身在某位置非法」**。对于加法这无所谓（两个有限数相加总有定义）；但对于**除法**，`1 / 0` 在浮点里是 `inf`、`0 / 0` 是 `nan`——这些位置本该被屏蔽，可 `_MaskedBinaryOperation` 不会管，因为两侧输入都没屏蔽。

`numpy.ma` 的解决方案是**第二类包装器**：`_DomainedBinaryOperation`。它在 `_MaskedBinaryOperation` 的基础上额外绑定一个**域检查器**（domain）。除法族的域检查器是 `_DomainSafeDivide`，它的职责只有一句话：

> **当除数（近）零时，把该位置标为「域违例」，从而屏蔽结果。**

于是 `ma.divide(a, b)` 的结果 mask 有**四个来源**（比一元的三个还多一个）：

1. 结果非有限（`~isfinite`）；
2. 左输入 `a` 被屏蔽；
3. 右输入 `b` 被屏蔽；
4. **域违例**（除数近零）——这一项是 `_DomainedBinaryOperation` 相对 `_MaskedBinaryOperation` 独有的。

只要这四者任一为真，结果该位置就被屏蔽。

#### 4.3.2 核心流程

`_DomainSafeDivide.__call__(a, b)` 的判断式（核心，看似反直觉）：

```
tolerance 默认 = np.finfo(float).tiny   # 最小正浮点数，约 2.2e-308
返回:  |a| * tolerance >= |b|            # ★ True 表示「域违例 → 屏蔽」
```

直观理解：**当 `|b|` 小到连 `|a| * tiny` 都不比它小**，就认为 `b`「实际上等于零」，除法无意义，屏蔽。例如：

- `b = 0`：`|a| * tiny >= 0` 恒为真 → 屏蔽。✓（除以零）
- `b = 1e-400`（下溢成 `0.0`）：同样屏蔽。✓
- `b = 1.0`：`|a| * tiny >= 1.0` 几乎不可能为真（除非 `|a|` 极大）→ 不屏蔽。✓

这个阈值判据的好处是：它不依赖「严格等于零」这种脆弱判断，而是用一个相对阈值 `|a|*tiny` 来捕捉「除数小到结果必然溢出/失精」的所有情况。

`_DomainedBinaryOperation.__call__` 的伪代码：

```
da, db = getdata(a), getdata(b)
result = self.f(da, db)                  # 照常除，屏蔽位也照除（产生 inf/nan）

m = ~isfinite(result)                    # 来源①：结果是 nan/inf
m |= getmask(a)                          # 来源②：a 屏蔽
m |= getmask(b)                          # 来源③：b 屏蔽
domain = ufunc_domain.get(self.f)
if domain is not None:
    m |= domain(da, db)                  # 来源④：域违例（_DomainSafeDivide）

if 标量: return masked if m else result

# 数组：屏蔽位先清 0，再尽可能加回 da（见 4.3.3 的 multiply/can_cast 技巧）
np.copyto(result, 0, where=m)
masked_da = m * da
if can_cast(masked_da.dtype, result.dtype, 'safe'):
    result += masked_da
return result 包装成 MaskedArray，挂 mask=m
```

#### 4.3.3 源码精读

先看 `_DomainSafeDivide` 的全部实现，注意它对 `tolerance` 的**惰性求值**和 `np.asarray` 转换：

[core.py:891-909](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L891-L909) — `_DomainSafeDivide`：`tolerance` 延迟到首次调用才取 `np.finfo(float).tiny`（注释说明这是为了缩短 numpy 的导入时间）；用 `np.asarray` 转换输入以避免从 `__array_wrap__` 回调 ma ufunc（标量会失败）；核心判据 `|a|*tolerance >= |b|`。

```python
class _DomainSafeDivide:
    """Define a domain for safe division."""
    def __init__(self, tolerance=None):
        self.tolerance = tolerance

    def __call__(self, a, b):
        # 延迟取 tolerance，减少 numpy 导入耗时
        if self.tolerance is None:
            self.tolerance = np.finfo(float).tiny
        # 用 asarray 避免 __array_wrap__ 回调 ma ufunc（标量会失败）
        a, b = np.asarray(a), np.asarray(b)
        with np.errstate(all='ignore'):
            return umath.absolute(a) * self.tolerance >= umath.absolute(b)
```

再看 `_DomainedBinaryOperation.__call__` 的主体——四个 mask 来源与屏蔽位回填技巧：

[core.py:1207-1247](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L1207-L1247) — `_DomainedBinaryOperation.__call__`：结果 mask = 非有限 `|` mask_a `|` mask_b `|` domain(da,db)；屏蔽位先 `copyto(result, 0)` 清零，再用 `m*da` 在能安全转型时加回，避免暴露 `inf`/`nan`。

```python
def __call__(self, a, b, *args, **kwargs):
    (da, db) = (getdata(a), getdata(b))
    with np.errstate(divide='ignore', invalid='ignore'):
        result = self.f(da, db, *args, **kwargs)
    # 结果 mask 的四个来源
    m = ~umath.isfinite(result)            # ① 结果非有限
    m |= getmask(a)                        # ② a 屏蔽
    m |= getmask(b)                        # ③ b 屏蔽
    # 应用域
    domain = ufunc_domain.get(self.f, None)
    if domain is not None:
        m |= domain(da, db)                # ④ 域违例（_DomainSafeDivide）
    if not m.ndim:                         # 标量
        if m:
            return masked
        else:
            return result
    # 数组：屏蔽位回填
    try:
        np.copyto(result, 0, casting='unsafe', where=m)   # 先清零
        masked_da = umath.multiply(m, da)                 # 用乘法（避免 * 的覆盖语义）
        if np.can_cast(masked_da.dtype, result.dtype, casting='safe'):
            result += masked_da                            # 能安全转型才加回 da
    except Exception:
        pass

    masked_result = result.view(get_masked_subclass(a, b))
    masked_result._mask = m
    ...
    return masked_result
```

回填那段（`copyto(result, 0)` 后 `result += m*da`）比 `_MaskedBinaryOperation` 的 `copyto(result, da)` 更曲折，原因是除法结果常是浮点、而 `da` 可能是整型，**直接 `copyto` 会类型不符**。于是先清零、再用 `m*da`（屏蔽位为 `da`、非屏蔽位为 `0`）、仅当能 `safe` 转型时才 `+=`。注释「avoid using `*` since this may be overlaid」指的是用 `umath.multiply` 而非 Python 的 `*`，因为 `*` 在掩码上下文里可能被改写。

最后看注册区，确认除法族统一挂 `_DomainSafeDivide`：

[core.py:1316-1324](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L1316-L1324) — 四个带域二元运算都用 `_DomainedBinaryOperation(..., _DomainSafeDivide(), 0, 1)`，即 `fillx=0, filly=1`；`true_divide` 是 `divide` 的别名，`mod` 是 `remainder` 的别名。

```python
divide = _DomainedBinaryOperation(umath.divide, _DomainSafeDivide(), 0, 1)
true_divide = divide                       # divide 的别名
floor_divide = _DomainedBinaryOperation(umath.floor_divide, _DomainSafeDivide(), 0, 1)
remainder = _DomainedBinaryOperation(umath.remainder, _DomainSafeDivide(), 0, 1)
fmod = _DomainedBinaryOperation(umath.fmod, _DomainSafeDivide(), 0, 1)
mod = remainder                            # remainder 的别名
```

注意 `divide` 的 `filly=1`：这正是 4.2 节「`np.divide` 路径用 `filly=1` 填充域违例」的来源——除法的域违例位在 `__array_wrap__` 路径被填成 `1`，而 `ma.divide` 路径（`__call__`）则把屏蔽位回填成 `da`。**两条路径 mask 完全一致，仅屏蔽位的 `.data` 取值不同**——这和 u2-l4 里 `ma.sqrt` 与 `np.sqrt` 的关系如出一辙。

#### 4.3.4 代码实践

**实践目标**：用 `ma.divide` 验证除零位置被屏蔽，并对比 `np.divide`（走 `__array_wrap__`）在屏蔽位 `.data` 上的差异。

**操作步骤**：

```python
import numpy as np
import numpy.ma as ma

a = ma.array([1.0, 2.0, 3.0])
b = ma.array([0.0, 1.0, 0.0])          # 索引 0、2 是除零

# 路径①：ma.divide（走 __call__）
r1 = ma.divide(a, b)
print("ma.divide  data =", r1.data)    # 屏蔽位回填 da：预期 [1.0, 2.0, 3.0]
print("ma.divide  mask =", r1.mask)    # 预期 [True, False, True]

# 路径②：np.divide（走 __array_wrap__，屏蔽位填 filly=1）
r2 = np.divide(a, b)
print("np.divide  mask =", r2.mask)    # 预期与 r1.mask 一致：[True, False, True]
# 两条路径 mask 相同，但屏蔽位的 .data 取值不同
```

**需要观察的现象**：

- 索引 0、3 处除数为 `0`，被 `_DomainSafeDivide` 判为域违例而屏蔽，`mask` 为 `True`。
- 索引 1 除数 `1.0`，正常，结果 `2.0/1.0 = 2.0`，不屏蔽。
- `ma.divide` 与 `np.divide` 的 `mask` **完全相同**（两条路径共享 `ufunc_domain`）；但屏蔽位的 `.data` 不同——前者回填 `da`（`1.0`/`3.0`），后者填 `filly=1`。

**预期结果**：两次 `mask` 均为 `[True, False, True]`。`ma.divide` 屏蔽位 `data` 为 `[1.0, 2.0, 3.0]`，`np.divide` 屏蔽位 `data` 取值不同（域违例位被 `copyto(result, 1, where=d)` 填成 `1.0`）。具体数值待本地验证。

> **注意**：`a / b`（运算符）等价于 `np.divide(a, b)` 还是 `ma.divide(a, b)`？当 `a`、`b` 都是普通 ndarray 时走 `np.divide`；当至少一侧是 `MaskedArray` 时，`MaskedArray` 的 `__truediv__` 会确保走掩码路径。精确边界建议读 `core.py` 中 `__truediv__` 的实现确认。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `_DomainSafeDivide` 用 `|a| * tiny >= |b|` 而不是简单的 `b == 0`？

> **答案**：`b == 0` 只能抓住严格等于零的除数，抓不住 `1e-400`（下溢成 `0.0`）或极小但非零、会导致结果溢出 `inf` 的除数。`|a|*tiny >= |b|` 是一个**相对阈值**判据：当除数小到「结果必然溢出或严重失精」就屏蔽，更稳健地覆盖了所有危险情形。

**练习 2**：`_DomainedBinaryOperation` 的结果 mask 有四个来源，而 `_MaskedBinaryOperation` 只有 `mask_a | mask_b` 两个。多出来的两个是什么？为什么普通二元运算不需要它们？

> **答案**：多出来的是「结果非有限 `~isfinite(result)`」和「域违例 `domain(da,db)`」。普通二元运算（加、减、乘、比较）在两个有限、合法输入下结果总是有限且有定义，不需要这两项；除法则会因除零产生 `inf`/`nan`，必须靠这两项把坏结果屏蔽掉。

---

### 4.4 归约、外积与累加：reduce / outer / accumulate

#### 4.4.1 概念说明

ufunc 除了「逐元素运算」，还有三个高阶方法：`reduce`（沿轴归约）、`outer`（外积）、`accumulate`（累加/前缀归约）。`numpy.ma` 在 `_MaskedBinaryOperation` 上为前两个提供了掩码版，第三个（`accumulate`）也提供了但有一个重要**缺陷**。

关键设计取舍（这一节的核心结论）：

- **`reduce` 的 mask 用 `logical_and` 归约**：一个归约结果位置**只有在轴上所有元素都被屏蔽时**才被屏蔽。换句话说，「只要有一个元素是好的，归约结果就是好的」。这符合统计直觉（求和时跳过坏值即可）。
- **`reduce` 先用 `filly` 填掉屏蔽位**：屏蔽位被当成幺元（加法当 `0`、乘法当 `1`）参与普通 ufunc 归约，从而「等于不存在」。
- **带域的二元运算（`divide` 等）没有 `reduce`/`outer`/`accumulate`**——它们的类 `_DomainedBinaryOperation` 根本没定义这些方法（文档明说 "They have no reduce, outer or accumulate"）。
- **比较类运算（`equal`/`less`/`greater`…）的 `reduce` 被显式置为 `None`**——对它们做归约没有意义。
- **`accumulate` 不传播 mask**：它只 `filled(target, filly)` 后调用底层 `accumulate`，**完全不算 mask**，结果 mask 可能不正确。这是已知的实现简化。

#### 4.4.2 核心流程

`reduce` 伪代码（核心两步：填幺元 + 归约 mask）：

```
t = filled(target, self.filly)          # 屏蔽位换成 filly（幺元）
if 无屏蔽:
    tr = self.f.reduce(t, axis)          # 普通 ufunc 归约
    mr = nomask
else:
    tr = self.f.reduce(t, axis, dtype)   # 普通归约（数据）
    mr = logical_and.reduce(m, axis)     # ★ mask 用「与」归约：全屏蔽才屏蔽
if 标量: return masked if mr else tr
return tr.view(tclass) 挂 mask=mr
```

`outer` 伪代码（mask 用 `logical_or.outer`）：

```
d = self.f.outer(da, db)                 # 数据外积
m = logical_or.outer(ma, mb)             # ★ mask 用「或」外积：任一屏蔽则屏蔽
copyto(d, da, where=m)
return d.view(subclass) 挂 mask=m
```

`accumulate` 伪代码（注意：**不算 mask**）：

```
t = filled(target, self.filly)
result = self.f.accumulate(t, axis)      # 只归约数据，不归约 mask
return result.view(tclass)               # mask 未正确处理
```

#### 4.4.3 源码精读

先看 `reduce`，关注「填幺元」和「mask 用 `logical_and`」两处：

[core.py:1109-1136](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L1109-L1136) — `_MaskedBinaryOperation.reduce`：先用 `filled(target, self.filly)` 把屏蔽位换成幺元；数据走 `self.f.reduce`，mask 走 `logical_and.reduce`（全屏蔽才屏蔽）；标量返回 `masked` 单例。

```python
def reduce(self, target, axis=0, dtype=None):
    tclass = get_masked_subclass(target)
    m = getmask(target)
    t = filled(target, self.filly)            # ★ 屏蔽位换成 filly（幺元）
    if t.shape == ():
        t = t.reshape(1)
        if m is not nomask:
            m = make_mask(m, copy=True).reshape((1,))

    if m is nomask:
        tr = self.f.reduce(t, axis)
        mr = nomask
    else:
        tr = self.f.reduce(t, axis, dtype=dtype)
        mr = umath.logical_and.reduce(m, axis)   # ★ mask：与归约，全屏蔽才屏蔽

    if not tr.shape:
        if mr:
            return masked
        else:
            return tr
    masked_tr = tr.view(tclass)
    masked_tr._mask = mr
    return masked_tr
```

再看 `outer`（mask 用 `logical_or.outer`，与 `reduce` 的 `logical_and` 形成对照）：

[core.py:1138-1161](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L1138-L1161) — `_MaskedBinaryOperation.outer`：数据走 `self.f.outer`，mask 走 `logical_or.outer`（任一屏蔽则屏蔽）。

```python
def outer(self, a, b):
    (da, db) = (getdata(a), getdata(b))
    d = self.f.outer(da, db)
    ma = getmask(a)
    mb = getmask(b)
    if ma is nomask and mb is nomask:
        m = nomask
    else:
        ma = getmaskarray(a)
        mb = getmaskarray(b)
        m = umath.logical_or.outer(ma, mb)      # ★ mask：或外积
    if (not m.ndim) and m:
        return masked
    if m is not nomask:
        np.copyto(d, da, where=m)
    if not d.shape:
        return d
    masked_d = d.view(get_masked_subclass(a, b))
    masked_d._mask = m
    return masked_d
```

然后是 `accumulate`——注意它**没有处理 mask**：

[core.py:1163-1172](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L1163-L1172) — `_MaskedBinaryOperation.accumulate`：只 `filled(target, filly)` 后调用底层 `accumulate`，**不计算 mask**，结果 mask 可能不正确。

```python
def accumulate(self, target, axis=0):
    tclass = get_masked_subclass(target)
    t = filled(target, self.filly)
    result = self.f.accumulate(t, axis)         # ★ 只归约数据，不算 mask
    masked_result = result.view(tclass)
    return masked_result
```

最后看几个注册期的「特例」——比较类置 `None`、`alltrue`/`sometrue` 是 `reduce` 的别名：

[core.py:1294-1313](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L1294-L1313) — 比较类运算（`equal`/`not_equal`/`less_equal`/`greater_equal`/`less`/`greater`）注册后立刻把 `.reduce` 置为 `None`，禁止归约；`alltrue = logical_and.reduce`、`sometrue = logical_or.reduce` 是历史别名。

```python
equal = _MaskedBinaryOperation(umath.equal)
equal.reduce = None                          # ★ 比较类禁止 reduce
not_equal = _MaskedBinaryOperation(umath.not_equal)
not_equal.reduce = None
# ... less_equal / greater_equal / less / greater 同样 .reduce = None
logical_and = _MaskedBinaryOperation(umath.logical_and)
alltrue = _MaskedBinaryOperation(umath.logical_and, 1, 1).reduce   # ★ 别名
logical_or = _MaskedBinaryOperation(umath.logical_or)
sometrue = logical_or.reduce                                      # ★ 别名
```

把四种「reduce 的命运」汇总成一张表：

| 运算 | `reduce` 是否可用 | 原因 |
|---|---|---|
| `add` / `subtract` / `multiply` | 可用 | 普通 `_MaskedBinaryOperation`，`filly` 是幺元 |
| `equal` / `less` / `greater` / … | **`None`（禁用）** | 比较「归约」无数学意义，注册时显式置空 |
| `divide` / `floor_divide` / `remainder` / `fmod` | **不存在** | `_DomainedBinaryOperation` 类根本没定义该方法 |
| `alltrue` / `sometrue` | 可用（本质是 `reduce`） | 分别是 `logical_and.reduce` / `logical_or.reduce` 的别名 |

#### 4.4.4 代码实践

**实践目标**：验证「`reduce` 的 mask 用 `logical_and`：全屏蔽才屏蔽」，并演示 `outer` 的 mask 合并。

**操作步骤**：

```python
import numpy as np
import numpy.ma as ma

# 1) reduce：沿 axis=1 归约，看「全屏蔽才屏蔽」
x = ma.array([[1, 2, 3],
              [4, 5, 6],
              [7, 8, 9]], mask=[[0,0,0],
                                [1,1,1],   # 整行屏蔽
                                [0,1,0]])
rowsum = ma.add.reduce(x, axis=1)
print("rowsum data =", rowsum.data)   # 预期 [6, ?, 24]（屏蔽行填 filly=0 求和得 0，但被屏蔽）
print("rowsum mask =", rowsum.mask)   # 预期 [False, True, False]

# 2) outer：两个一维数组外积，mask 用 logical_or.outer
a = ma.array([1, 2],    mask=[0, 1])
b = ma.array([10, 20],  mask=[1, 0])
o = ma.add.outer(a, b)
print("outer data =\n", o.data)
print("outer mask =\n", o.mask)       # 任一侧屏蔽则屏蔽

# 3) accumulate：观察它不正确处理 mask 的「缺陷」
acc = ma.add.accumulate(ma.array([1,2,3], mask=[0,1,0]))
print("accumulate =", acc)             # 结果是 [1, 3, 6]，mask 未随屏蔽传播
```

**需要观察的现象**：

- `rowsum`：第 0 行全不屏蔽 → `1+2+3=6`、mask `False`；第 1 行**整行屏蔽** → mask `True`（`logical_and` 全真才真）；第 2 行只有中间屏蔽 → `7+0+9=16`、mask `False`。**这正印证了「全屏蔽才屏蔽」**。
- `outer`：`a` 的屏蔽位（索引 1）和 `b` 的屏蔽位（索引 0）在外积矩阵中分别让整行/整列被屏蔽，对角交叉点也被屏蔽——`logical_or.outer` 的效果。
- `accumulate`：结果是 `[1, 3, 6]`，中间本应受屏蔽影响的累加值并没有体现 mask——**这是 `accumulate` 不算 mask 的已知行为**。

**预期结果**：`rowsum.mask = [False, True, False]`，`rowsum.data = [6, 0, 16]`（屏蔽行的 `data` 是 `0+0+0=0` 但 `mask=True`）。`accumulate` 返回 `[1, 3, 6]` 且 mask 不正确。具体数值待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：`ma.add.reduce` 中，为何 mask 用 `logical_and` 归约而不是 `logical_or`？

> **答案**：因为「归约」是把轴上多个元素**合并成一个**。若用 `logical_or`，只要轴上有一个屏蔽元素，整个归约结果就被屏蔽——这会让「99 个好值 + 1 个坏值」的求和也变成坏值，违背「跳过坏值」的初衷。`logical_and` 表示「**全部**元素都屏蔽，归约结果才屏蔽」，符合统计语义。

**练习 2**：下列三个调用，哪些会成功、哪些会报错？`(a) ma.divide.reduce(x)`、`(b) ma.equal.reduce(x)`、`(c) ma.add.reduce(x)`。

> **答案**：只有 `(c)` 成功。`(a)` 失败——`divide` 是 `_DomainedBinaryOperation`，没有 `reduce` 方法；`(b)` 失败——`equal.reduce` 被显式置为 `None`，调用会抛 `TypeError`；`(c)` 成功——`add` 是普通 `_MaskedBinaryOperation`，`reduce` 可用。

**练习 3**：`ma.add.accumulate` 的结果 mask 可靠吗？为什么？

> **答案**：不可靠。源码（core.py:1163-1172）显示 `accumulate` 只做了 `filled(target, filly)` 后调用底层 `accumulate`，**全程没有计算 mask**，返回的 `masked_result` 没有挂上正确的 mask。这是实现的已知简化，使用时需自行注意。

---

## 5. 综合实践

把本讲四个模块串起来，完成一个「带缺失值的温度数据处理」小任务。

**背景**：你有两天的温度读数 `t1`、`t2`（摄氏度），其中部分读数是无效值（传感器故障，记为 `999` 或 `nan`）。要求：

1. 用合适的方式屏蔽无效值（复习 u1-l3）。
2. 求两天温度的**差值** `t1 - t2`（用 `ma.subtract`），验证：只要任一天的某时刻被屏蔽，差值该位置就被屏蔽。
3. 计算**逐时刻的温差占比** `(t1 - t2) / t2`（用 `ma.divide`），验证：当 `t2` 为 `0`（或近零）时该位置被 `_DomainSafeDivide` 屏蔽，而不是产生 `inf`。
4. 用 `ma.add.reduce` 沿时间轴求每天的「有效读数之和」，验证屏蔽位被当幺元 `0` 跳过。
5. 分别用 `ma.divide(...)` 和 `np.divide(...)` 两种方式算第 3 步，对比它们 `mask` 是否一致、屏蔽位 `.data` 是否不同。

```python
import numpy as np
import numpy.ma as ma

t1 = ma.array([20.0, 22.0, 999.0, 25.0, 0.0])      # 999 是故障
t1 = ma.masked_equal(t1, 999.0)                    # 屏蔽 999
t2 = ma.array([20.0, 0.0,  21.0, 25.0, 10.0])      # t2[1]=0 会触发除法域

# 第 2 步：差值
diff = ma.subtract(t1, t2)
print("diff =", diff)                  # t1 屏蔽位(索引2) -> diff 屏蔽

# 第 3 步：温差占比（除法，t2=0 处屏蔽）
ratio_ma = ma.divide(ma.subtract(t1, t2), t2)
ratio_np = np.divide(ma.subtract(t1, t2), t2)
print("ratio_ma mask =", ratio_ma.mask)
print("ratio_np mask =", ratio_np.mask)  # 两者 mask 应一致

# 第 4 步：每天的有效和（这里把两天看成沿新轴堆叠后 reduce）
two_days = ma.stack([t1, t2])          # shape (2, 5)
daysum = ma.add.reduce(two_days, axis=0)
print("daysum =", daysum)             # 屏蔽位当 0 跳过
```

**你需要能回答的检验问题**：

- `diff` 在索引 2 是否屏蔽？（是，因为 `t1` 在那里屏蔽。）在索引 1 呢？（不，`t2=0` 不影响减法。）
- `ratio_ma` 在哪些位置屏蔽？（索引 1：`t2=0` 触发除法域；索引 2：分子含屏蔽位。）
- `ratio_ma.mask` 与 `ratio_np.mask` 是否完全相同？（是。）
- `daysum` 在索引 2 的值与 mask 是什么？（屏蔽位被 `filly=0` 填掉再相加，但因两侧之一屏蔽，`logical_and` 在堆叠维度上不全屏蔽——需结合具体 mask 判断结果是否屏蔽。）

具体输出待本地验证。完成本任务意味着你已经把「mask 合并 → 除法域 → 归约 → 两条调用路径」四个最小模块融会贯通。

## 6. 本讲小结

- **`_MaskedBinaryOperation.__call__`** 用 `logical_or` 合并两侧 mask；普通二元运算的结果 mask **只来自** `mask_a | mask_b`，**不**自动屏蔽结果中的 `inf`/`nan`。屏蔽位用 `np.copyto(result, da, where=m)` 回填成左侧 `da`。
- **`fillx`/`filly`** 有两条用途：① 登记进全局表 `ufunc_fills`，供 `np.op(masked_array)` 路径的 `__array_wrap__` 取用（二元域取 `[-1]` 即 `filly`）；② 作为 `reduce`/`accumulate` 的**幺元**（加法 `0`、乘法 `1`），这就是 `multiply` 要特意传 `1, 1` 的原因。
- **`_DomainedBinaryOperation`** 在普通二元版上叠加 domain 检查，结果 mask 有**四个来源**：`~isfinite(result)`、`mask_a`、`mask_b`、`domain(da,db)`。除法族统一挂 `_DomainSafeDivide`。
- **`_DomainSafeDivide`** 的判据 `|a| * tiny >= |b|` 用相对阈值屏蔽「除以（近）零」，比 `b == 0` 更稳健；`tolerance` 惰性取 `np.finfo(float).tiny` 以缩短 numpy 导入时间。
- **`reduce`** 先 `filled(target, filly)` 填幺元，数据走 `self.f.reduce`、mask 走 **`logical_and`** 归约（全屏蔽才屏蔽）；**`outer`** 的 mask 走 **`logical_or.outer`**（任一屏蔽则屏蔽）；**`accumulate`** 只归约数据、**不算 mask**，是已知简化。
- **三类「reduce 不可用」**：比较类（`equal`/`less`/…）`.reduce = None` 被显式禁用；带域除法族（`divide`/`floor_divide`/`remainder`/`fmod`）所在类 `_DomainedBinaryOperation` 根本没有该方法；`alltrue`/`sometrue` 实为 `logical_and.reduce`/`logical_or.reduce` 的别名。

## 7. 下一步学习建议

本讲把二元 ufunc 的掩码化讲完了。建议接下来：

- **阅读归约/统计方法**：本讲的 `reduce` 是 ufunc 层的归约；`MaskedArray` 还提供了方法层的 `sum`/`mean`/`var`/`std`/`cumsum`/`cumprod` 以及 `min`/`max`/`argmin`/`argmax`，它们如何结合 `fill_value`（u2-l3）跳过屏蔽值。这对应讲义 **u2-l7「归约、统计与排序」**，是本讲 `reduce` 的上层应用。
- **阅读 extras 的统计工具**：`average`/`median`/`cov`/`corrcoef` 等更高阶统计建立在 `divide`、`multiply` 之上，可在 **u2-l8「extras 实用工具函数集」** 中看到它们如何复用本讲的二元运算。
- **回看 `__array_wrap__` 全貌**：本讲多次提到「两条调用路径」，若想彻底弄清 `np.op(masked_array)` 为何能自动屏蔽，可重读 u2-l2 与 u2-l4 中 `__array_wrap__`（core.py:3156-3201）的完整实现，把 `ufunc_domain`/`ufunc_fills` 这两张表的「契约」理解透。
- **进阶**：当你需要自定义一个带域的掩码运算，或想理解「`masked` 单例参与二元运算如何传播」，可继续阅读 **u3-l3「masked 单例与掩码数组打印」**——`masked` 正是二元运算标量屏蔽分支（`return masked`）的产物。
