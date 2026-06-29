# _lib._util 核心工具函数集

## 1. 本讲目标

本讲是「共享基础设施 _lib」单元的第一讲。学完后你应当能够：

- 识别 `scipy/_lib/_util.py` 中那些被多个子包反复复用的通用工具函数，并能说出它们解决的是哪一类「重复的样板问题」。
- 理解 `_lazyselect` 这类「惰性分支选择」机制的工作原理，以及它为什么比直接调用 `np.select` 更省计算。
- 掌握 SciPy 用来做输入校验（整数校验、NaN 处理、稀疏识别）和数组后端兼容（Array API / `array_namespace`）的一组工具。
- 看懂 `_RichResult`、`getfullargspec_no_self`、`_transition_to_rng` 等工具在 stats、optimize、linalg 等子包中如何被使用。

本讲不要求你改源码，所有实践都是「阅读 + 调用」型，安全可重复。

## 2. 前置知识

阅读本讲前，建议你先具备以下认知（来自 u1-l1、u1-l3）：

- **子包与延迟导入**：SciPy 顶层包 `scipy/__init__.py` 用 PEP 562 的模块级 `__getattr__` 把 17 个子包登记在 `submodules` 里，用到才加载。
- **`_lib` 是什么**：`scipy/_lib/` 是 SciPy 内部的「私有共享库」，存放被几乎所有子包复用的基础设施（工具函数、Array API 适配、文档工具、uarray 等）。它不在公开 API 中，但却是理解 SciPy 内部一致性的关键。
- **Python 装饰器与 `functools.wraps`**：本讲会频繁出现「装饰器 + 包装函数」的模式，用于在不动业务逻辑的前提下统一处理参数重命名、警告、签名修补等横切关注点。
- **NumPy 随机数演进（SPEC 7）**：SciPy 正在从 `numpy.random.RandomState` 迁移到 `numpy.random.Generator`，本讲的 RNG 工具正是为这一过渡服务的。
- **Array API 标准**：一个让 NumPy / CuPy / PyTorch / JAX / Dask 共享同一套数组接口的规范，SciPy 通过 `array_namespace()` 抽象数组来源（详见 u2-l3）。

一个贯穿全讲的直觉：**`_util.py` 的本质是「把 SciPy 各子包里重复出现的样板代码提炼成共享函数」**。你会在 stats、optimize、linalg、integrate、ndimage 里反复看到同一个 `_util` 函数被 import——这就是它在起作用。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `scipy/_lib/_util.py` | 核心工具函数集：RNG 处理、惰性分支、输入校验、可调用封装、结果容器、反射/签名工具。本讲的主战场。 |
| `scipy/_lib/_sparse.py` | 极小的稀疏类型抽象基类 `SparseABC` 与判断函数 `issparse`，被 `_asarray_validated` 等校验逻辑依赖。 |
| `scipy/_lib/_array_api.py` | Array API 适配层，提供 `_asarray`、`array_namespace`、`xp_size` 等。`_util.py` 中的 `_contains_nan`、`_get_nan` 直接依赖它来兼容非 NumPy 数组。 |

> 说明：本讲重点精读 `_util.py` 与 `_sparse.py`，`_array_api.py` 只在「输入校验」模块里点出其被依赖的关键入口（完整讲解见 u2-l3）。

## 4. 核心概念与源码讲解

我们把 `_util.py` 拆成 5 个相对独立的最小模块来学。

---

### 4.1 随机数种子与 RNG 工具集

#### 4.1.1 概念说明

SciPy 里大量函数需要随机数：`stats` 的重采样、`optimize` 的差分进化/双退火、`cluster` 的 k-means 初始化等。这些函数都要面对同一个问题：

> 用户传进来的「随机源」可能是 `None`、整数、`np.random.RandomState` 实例，也可能是新的 `np.random.Generator` 实例。怎么把它们统一成一个「可调用的随机数发生器」？

如果每个子包各写一套判断逻辑，就会产生大量重复且容易出错的样板代码。`_util.py` 把这套逻辑提炼成几个共享函数：

- `check_random_state(seed)`：把任意 seed 规范化成一个 `RandomState`/`Generator` 实例（旧式 API，返回 RandomState 单例或新实例）。
- `rng_integers(gen, ...)`：统一 `RandomState.randint` 和 `Generator.integers` 两套不一致的随机整数接口。
- `_transition_to_rng(old_name, ...)`：一个装饰器，帮子包把旧参数名（如 `random_state`、`seed`）平滑迁移到新参数名 `rng`（SPEC 7）。
- `_rng_spawn(rng, n)`：从一个父 RNG 派生出若干个相互独立的子 RNG（用于并行/分块采样）。

#### 4.1.2 核心流程

`check_random_state` 的分派逻辑可以用伪代码描述：

```
输入 seed
if seed is None 或 seed is np.random:
    返回全局 RandomState 单例（np.random.mtrand._rand）
elif seed 是整数:
    返回 np.random.RandomState(seed)   # 新实例
elif seed 是 RandomState 或 Generator 实例:
    直接返回该实例
else:
    报 ValueError
```

`_transition_to_rng` 装饰器更复杂，它的核心是判断 PRNG（伪随机数发生器）以哪三种方式之一被传入：

1. 旧关键字（如 `random_state=...`）；
2. 新关键字 `rng=...`；
3. 位置参数（在 `position_num` 指定的位置）。

三者只能选其一，否则报「多值冲突」。然后它把旧名映射到 `rng`，并在 `end_version` 指定时按 SPEC 7 发出 `DeprecationWarning`/`FutureWarning`。最后，它还会用 `_rng_desc` 模板把函数文档里的 `rng` 参数说明替换成统一文案。

#### 4.1.3 源码精读

`check_random_state` 把四种 seed 形态统一为实例——注意它对 `None`/`np.random` 返回的是**全局单例**（这意味着不传 seed 时多个调用共享同一随机流）：

[scipy/_lib/_util.py:332-359](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_util.py#L332-L359) —— 把 `None`/`int`/`RandomState`/`Generator` 四种输入规范化为可用的随机源。

`rng_integers` 统一了「RandomState 用 `randint`、Generator 用 `integers`」的不一致，并处理 `endpoint`（是否包含上界）：

[scipy/_lib/_util.py:665-726](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_util.py#L665-L726) —— 用 `isinstance(gen, np.random.Generator)` 分支，分别走 `integers` 与 `randint` 两条路径。

`_transition_to_rng` 装饰器是 SPEC 7 迁移的核心。看它如何统计三种传入方式并拒绝冲突：

[scipy/_lib/_util.py:229-245](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_util.py#L229-L245) —— `as_old_kwarg + as_new_kwarg + as_pos_arg > 1` 即抛 `TypeError`，保证 PRNG 只能以一种方式传入。

迁移完成后，装饰器还会**改写被装饰函数的签名**（把旧参数名重新加回关键字参数），让 IDE 补全和老代码仍能工作：

[scipy/_lib/_util.py:307-326](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_util.py#L307-L326) —— 用 `inspect.signature(...).replace(...)` 动态给包装函数挂上一个 `KEYWORD_ONLY` 的旧参数，并用 `_rng_desc` 替换文档。

谁在用它？`stats._fit` 与 `optimize._dual_annealing`、`optimize._basinhopping` 都同时 import 了 `check_random_state` 和 `_transition_to_rng`：

[scipy/stats/_fit.py:6](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/stats/_fit.py#L6) —— `from scipy._lib._util import check_random_state, _transition_to_rng`。

#### 4.1.4 代码实践

1. **实践目标**：亲手把四种形态的 seed 喂给 `check_random_state`，观察返回的是单例还是新实例。
2. **操作步骤**：写一段脚本（示例代码，非项目原有）：

   ```python
   # 示例代码
   import numpy as np
   from scipy._lib._util import check_random_state, rng_integers, _rng_spawn

   r1 = check_random_state(None)            # 全局单例
   r2 = check_random_state(42)              # 新 RandomState 实例
   r3 = check_random_state(np.random.default_rng(7))  # 透传 Generator

   print(r1 is np.random.mtrand._rand)      # 期望 True（单例）
   print(type(r2).__name__)                 # 期望 RandomState
   print(type(r3).__name__)                 # 期望 Generator

   # rng_integers 统一两种后端的整数采样
   print(rng_integers(r3, 0, 10, size=3))   # Generator 路径
   print(rng_integers(r2, 0, 10, size=3))   # RandomState 路径

   # 从一个 Generator 派生 3 个独立子 RNG
   children = _rng_spawn(np.random.default_rng(0), 3)
   print(len(children), type(children[0]).__name__)  # 期望 3 Generator
   ```
3. **需要观察的现象**：`r1` 与全局单例是同一对象；`rng_integers` 在两种后端上都能正常返回整数数组。
4. **预期结果**：输出依次为 `True`、`RandomState`、`Generator`，以及两组 3 元整数数组，最后是 `3 Generator`。
5. 若环境无法 import 私有模块（理论上 SciPy 安装后可 import `scipy._lib._util`），则标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `check_random_state(None)` 返回的是全局单例而不是每次新建实例？这样做有什么好处和风险？

> **答案**：好处是省去构造开销，并让「不显式传 seed」的多次调用共享同一随机流，便于在不改代码的前提下用 `np.random.seed` 全局复现；风险是全局可变状态会让并行或多次调用之间相互污染随机序列，所以需要可复现的科研代码应显式传整数 seed 或 Generator 实例。

**练习 2**：`_transition_to_rng` 在用户**同时**传了 `random_state=` 和 `rng=` 时会发生什么？

> **答案**：装饰器统计 `as_old_kwarg + as_new_kwarg + as_pos_arg > 1`，判定为多值冲突，抛出 `TypeError`，提示只能指定其中一个。

---

### 4.2 惰性分支选择 _lazyselect 与数值小工具

#### 4.2.1 概念说明

很多数学函数在不同定义域要用不同公式。例如某些特殊函数在小 `x` 时用级数展开、在大 `x` 时用渐近展开。最朴素的写法是用 `np.select`：

```python
np.select([x < 3, x > 3], [x**2, x**3], default=0)
```

问题在于：`np.select` 的 `choicelist` 是**已经算好的数组**——它会把**所有分支都先算一遍**，再按条件挑选。如果某个分支的公式很贵（或在某些区域会数值溢出），先全算一遍就既浪费又危险。

`_lazyselect` 把 `choicelist` 从「数组」换成「函数」：只有当某个条件确为真时，才把对应的函数应用到满足条件的元素上。这就是「惰性分支选择」。

本模块还包含一个小工具 `float_factorial(n)`：返回 `math.factorial(n)` 的浮点结果，但 \( n \ge 171 \) 时直接返回 `np.inf`（因为 \( 171! \) 已超出双精度浮点的表示范围）。

#### 4.2.2 核心流程

`_lazyselect(condlist, choicelist, arrays, default=0)` 的执行流程：

```
1. 把所有 arrays 广播到同一形状，得到统一 dtype 的 out（初值=default）
2. 对每一对 (条件 cond, 函数 func)：
   a. 若 cond 全为 False，跳过（短路优化）
   b. 把 cond 与 arrays 广播对齐
   c. 用 np.extract 抽取满足 cond 的元素
   d. 只对这些元素调用 func(*temp)
   e. 用 np.place 把结果写回 out 对应位置
3. 返回 out
```

关键点：步骤 2c–2d 只对**满足条件的元素**求值，未满足条件的元素从不进入 `func`。

#### 4.2.3 源码精读

`_lazyselect` 的实现，注意第 85 行的「全 False 短路」和第 88 行的「只 extract 满足条件的元素」：

[scipy/_lib/_util.py:53-90](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_util.py#L53-L90) —— `choicelist` 是函数而非数组，逐条件惰性求值。

它在 stats 里被大量用于「分段定义」的连续分布密度函数。例如 `_continuous_distns.py` 在多个地方用 `_lazyselect` 选择不同区间的公式：

[scipy/stats/_continuous_distns.py:23](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/stats/_continuous_distns.py#L23) —— `from scipy._lib._util import _lazyselect`。

`float_factorial` 用阈值 171 截断，避免大数阶乘溢出成乱码：

[scipy/_lib/_util.py:103-108](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_util.py#L103-L108) —— `n < 171` 时返回 `float(math.factorial(n))`，否则 `np.inf`。

谁用它？`signal._filter_design` 和 `signal._savitzky_golay` 都 import 了 `float_factorial`（设计窗函数/Savitzky-Golay 时需要阶乘系数）。

#### 4.2.4 代码实践

1. **实践目标**：对比 `np.select` 与 `_lazyselect`，验证后者「只对满足条件的元素调用函数」。
2. **操作步骤**（示例代码）：

   ```python
   # 示例代码
   import numpy as np
   from scipy._lib._util import _lazyselect, float_factorial

   x = np.arange(6)

   def loud_square(x):
       print(f"  square called with {x}")
       return x**2

   def loud_cube(x):
       print(f"  cube called with {x}")
       return x**3

   print("np.select 风格：两个分支都会先全算（这里用函数模拟惰性版）")
   out = _lazyselect([x < 3, x > 3], [loud_square, loud_cube], (x,), default=0)
   print("结果：", out)

   print("float_factorial(170) =", float_factorial(170))
   print("float_factorial(171) =", float_factorial(171))  # 期望 inf
   ```
3. **需要观察的现象**：`loud_square` 只收到 `[0 1 2]`，`loud_cube` 只收到 `[4 5]`，元素 `3`（落入 default）从未被任何函数处理。
4. **预期结果**：结果数组为 `[0 1 4 0 16 25]`；`float_factorial(171)` 为 `inf`。
5. 若 import 失败则标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：假设某个分支公式在 `x<0` 时会取对数 `np.log(x)`（对负数会 warning/nan）。用 `np.select` 和 `_lazyselect` 分别实现，哪个更安全？为什么？

> **答案**：`_lazyselect` 更安全。`np.select` 会先对全部元素计算 `choicelist` 里的每个数组（包括对负数算 `log`），从而产生 warning 或 nan；`_lazyselect` 只把正数元素传给含 `log` 的分支，负数元素根本不进入该函数，从而避免无效计算。

**练习 2**：`float_factorial` 为什么选 171 作为阈值？请用双精度浮点的最大值解释。

> **答案**：双精度浮点能表示的最大有限值约为 \( 1.8 \times 10^{308} \)，而 \( 170! \approx 7.26 \times 10^{306} \) 仍可表示，\( 171! \approx 1.24 \times 10^{309} \) 已溢出为 `inf`。因此以 171 为分界，提前返回 `inf` 比「先算出 inf 再转 float」更清晰可控。

---

### 4.3 数组输入校验、稀疏识别与 Array API 兼容

#### 4.3.1 概念说明

几乎所有 SciPy 数值函数的第一步都是「把用户输入变成一个干净的 ndarray」。这步看似简单，却要回答一连串问题：

- 输入是不是稀疏矩阵？（很多稠密例程不接受稀疏输入，要给友好报错。）
- 输入有没有 NaN/Inf？要不要拒绝？
- 输入是整数 dtype 但算法需要浮点，要不要自动转？
- 输入可能来自 CuPy/Torch/JAX，怎么不写死 NumPy？

`_util.py` 提供了对应的工具：

- `_asarray_validated(a, ...)`：统一校验 array-like，可开关稀疏/掩码/object/有限性检查，并可强制转浮点。
- `_validate_int(k, name, minimum)`：用 `operator.index` 严格校验「标量整数」参数（拒绝 `2.5`、`True` 之外的伪整数）。
- `normalize_axis_index(axis, ndim)`：把负 axis 规范化并做越界检查（抛 `numpy.exceptions.AxisError`）。
- `_contains_nan(a, nan_policy, ...)`：判断数组是否含 NaN，并按 `nan_policy`（`propagate`/`raise`/`omit`）决定行为——且通过 `xp` 参数兼容非 NumPy 数组。
- `_get_nan(*data, ...)`：返回与输入 dtype/设备匹配的 NaN（Array API 友好）。

而稀疏识别由更底层的 `_sparse.py` 提供：`issparse(x)` 判断 `x` 是否为稀疏数组/矩阵。`_asarray_validated` 在 `sparse_ok=False` 时正是用它来拦截稀疏输入的。

#### 4.3.2 核心流程

`_asarray_validated` 的校验流水线：

```
1. 若 sparse_ok=False 且 issparse(a)：抛 ValueError（提示改用 scipy.sparse.linalg）
2. 若 mask_ok=False 且是 masked array：抛 ValueError
3. 选 toarray = np.asarray_chkfinite（check_finite=True）或 np.asarray
4. a = toarray(a)
5. 若 objects_ok=False 且 dtype=='O'：抛 ValueError（object 数组）
6. 若 as_inexact=True 且不是浮点 dtype：转成 float64
7. 返回 a
```

`_contains_nan` 的策略分支：

```
若 nan_policy 不是 {propagate,raise,omit} 之一：抛 ValueError
若数组大小为 0：返回 False
按 dtype 取检测路径：
  - 实浮点：contains_nan = xp.isnan(xp.max(a))   # max 遇 NaN 才返回 NaN，比 any(isnan) 省内存
  - 复浮点：分别检测 real/imag 的 max
  - object（仅 NumPy）：逐元素扫描
  - 其它：返回 False（整数等不可能含 NaN）
若 policy=='raise' 且含 NaN：抛 ValueError
若 policy=='omit' 且非 NumPy xp（且不允许）：抛错
返回 contains_nan
```

注意 `xp.max(a)` 这个技巧：对浮点数组，`max` 只有在存在 NaN 时才返回 NaN，因此 `isnan(max(a))` 等价于「是否含 NaN」，却比 `any(isnan(a))` 省一次完整布尔数组。

#### 4.3.3 源码精读

`_asarray_validated` 的完整校验链，第 397–402 行就是用 `issparse` 拦截稀疏输入：

[scipy/_lib/_util.py:362-414](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_util.py#L362-L414) —— 串联稀疏/掩码/有限性/object/浮点五道校验。

`_contains_nan` 的策略分派与 `xp.max` 技巧：

[scipy/_lib/_util.py:770-827](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_util.py#L770-L827) —— 通过 `array_namespace(a)` 拿到 `xp`，使 NaN 检测对 CuPy/Torch 等同样适用。

`_validate_int` 用 `operator.index` 严格把关（`2.0` 会被接受，`2.5` 会被拒）：

[scipy/_lib/_util.py:417-442](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_util.py#L417-L442) —— `operator.index(k)` 是 Python「真整数」判定的标准做法。

`normalize_axis_index` 做负索引规范化与越界检查：

[scipy/_lib/_util.py:906-914](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_util.py#L906-L914) —— 越界抛 `AxisError`，负数加 `ndim` 转正。

稀疏识别 `issparse` 极简实现，本质是 `isinstance(x, SparseABC)`：

[scipy/_lib/_sparse.py:10-42](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_sparse.py#L10-L42) —— `SparseABC` 是所有稀疏数组/矩阵共同基类，`issparse` 只需一次 `isinstance`。

谁在校验上复用这些工具？`optimize._minpack_py` 同时 import 了 `_asarray_validated` 与 `_contains_nan`；`stats._stats_py` import 了 `_get_nan`、`_rename_parameter`、`_contains_nan`；`linalg._decomp` import 了 `_asarray_validated`。这是典型的「跨子包复用同一校验逻辑」。

#### 4.3.4 代码实践

1. **实践目标**：触发 `_asarray_validated` 的不同校验分支，体会它如何给出友好报错。
2. **操作步骤**（示例代码）：

   ```python
   # 示例代码
   import numpy as np
   from scipy._lib._util import _asarray_validated, _validate_int, _contains_nan
   import scipy.sparse as sp

   # (1) 正常输入
   print(_asarray_validated([1.0, 2.0, 3.0]))

   # (2) 含 NaN：默认 check_finite=True 会被拦
   try:
       _asarray_validated([1.0, np.nan, 3.0])
   except ValueError as e:
       print("被拦：", e)

   # (3) 稀疏输入被拦（提示用 scipy.sparse.linalg）
   try:
       _asarray_validated(sp.csr_array([[1, 0], [0, 2]]))
   except ValueError as e:
       print("被拦：", e)

   # (4) _validate_int 拒绝非整数
   try:
       _validate_int(2.5, "k")
   except TypeError as e:
       print("被拦：", e)

   # (5) _contains_nan 的 raise 策略
   print(_contains_nan(np.array([1.0, np.nan]), nan_policy="propagate"))  # 期望 True
   ```
3. **需要观察的现象**：步骤 (1) 返回干净的 `ndarray`；(2)(3)(4) 分别因 NaN、稀疏、非整数被友好拦截；(5) 返回 `True`。
4. **预期结果**：按上述注释依次打印数组、三条拦截信息、`True`。
5. 若环境无法 import 私有模块，则标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`_contains_nan` 为什么用 `xp.isnan(xp.max(a))` 而不是 `xp.any(xp.isnan(a))` 来检测实浮点数组的 NaN？

> **答案**：两者语义等价（浮点 `max` 只要存在 NaN 就返回 NaN），但 `max` 只产生一个标量，而 `any(isnan(a))` 要先构造一个与 `a` 同大的布尔数组再做归约，内存与带宽开销更大。对大数组这个差异很明显。

**练习 2**：`_validate_int(2.0, "k")` 会通过吗？为什么？

> **答案**：会通过。`operator.index(2.0)` 在 Python 中对「值为整数的浮点」是合法的（返回 2），所以 `2.0` 被接受；但 `2.5` 不是整数索引，会抛 `TypeError`。

---

### 4.4 可调用对象封装、并行 MapWrapper 与回调机制

#### 4.4.1 概念说明

optimize 和 integrate 经常需要把用户传进来的目标函数/被积函数「包装」一下再交给底层求解器，原因有三：

1. **可 pickle（序列化）**：差分进化、双退火等多进程优化要把目标函数发到子进程，原始的可调用对象（如 lambda 捕获了不可序列化的状态）无法 pickle。`_FunctionWrapper` / `_ScalarFunctionWrapper` 把函数和附加参数打包成可序列化的对象。
2. **统一并行**：用户可能传 `workers=4` 或一个 `multiprocessing.Pool`。`MapWrapper` 把这些统一成一个「map-like 可调用」，让算法主体无需关心是串行还是并行。
3. **回调与终止**：优化迭代每步可能要回调用户函数，且允许用户通过抛 `StopIteration` 提前终止。`_call_callback_maybe_halt` 封装了「调用回调 + 捕获 StopIteration 转终止信号」的逻辑。

相关工具：

- `_FunctionWrapper(f, args)`：把 `f(x, *args)` 封装成 `_FunctionWrapper(x)`，支持 pickle。
- `_ScalarFunctionWrapper(f, args)`：额外保证返回值是真标量，并计数 `nfev`（函数求值次数）。
- `MapWrapper(pool)`：把 `int`/`-1`/map-like 统一成 `__call__(func, iterable)` 的并行 map。
- `_workers_wrapper(func)`：装饰器，自动把用户的 `workers` 参数包进 `MapWrapper` 上下文。
- `_call_callback_maybe_halt(callback, res)`：安全调用回调，返回是否应停止。

#### 4.4.2 核心流程

`MapWrapper.__init__(pool)` 的分派：

```
if pool 是 callable（map-like）：直接用它
else（pool 是数字）：
    选择 start method（POSIX 上避免 fork 死锁，优先 forkserver）
    if pool == -1：用全部 CPU 建池
    elif pool == 1：不并行，用内置 map
    elif pool > 1：用指定进程数建池
    else：报错
__enter__/__exit__：若是自建池则负责 close/terminate/join
__call__(func, iterable)：转发给 self._mapfunc
```

`_call_callback_maybe_halt` 的流程：

```
if callback is None：返回 False（不停）
try:
    callback(res)
    return False
except StopIteration:
    记录 callback.stop_iteration = True
    return True   # 通知算法主循环停止
```

#### 4.4.3 源码精读

`MapWrapper` 如何把 `int`/`-1`/map-like 统一成并行 map，并处理 POSIX fork 死锁：

[scipy/_lib/_util.py:557-639](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_util.py#L557-L639) —— 第 588–592 行把「默认 fork 导致的死锁」问题通过回退到 `forkserver` 来规避。

`_FunctionWrapper` 与 `_ScalarFunctionWrapper` 的封装，后者在第 542 行对输入 `x` 做 `np.copy` 以防用户改写：

[scipy/_lib/_util.py:518-555](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_util.py#L518-L555) —— `_ScalarFunctionWrapper` 还在 `__call__` 里累计 `nfev` 并强制返回标量。

`_call_callback_maybe_halt` 把 `StopIteration` 当作「用户请求终止」的信号：

[scipy/_lib/_util.py:917-940](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_util.py#L917-L940) —— 这是 optimize 多个求解器（SLSQP、dogbox、trf）共用的回调协议。

谁在用？`optimize._differentialevolution` 同时 import 了 `check_random_state`、`MapWrapper`、`_FunctionWrapper`；`integrate._quad_vec` 与 `integrate._cubature` import 了 `MapWrapper`；`optimize._lsq.least_squares` 用 `_workers_wrapper` 装饰来支持 `workers` 参数。

#### 4.4.4 代码实践

1. **实践目标**：用 `MapWrapper` 体验「把数字自动变成并行池」，并观察它如何转发 `map`。
2. **操作步骤**（示例代码）：

   ```python
   # 示例代码
   from scipy._lib._util import MapWrapper, _call_callback_maybe_halt

   # pool=1 走内置 map（串行）
   with MapWrapper(1) as mw:
       print(list(mw(lambda x: x * x, [1, 2, 3])))   # 期望 [1, 4, 9]

   # 模拟回调终止协议
   class State:
       def __init__(self): self.n = 0
       def __call__(self, res):
           self.n += 1
           if self.n >= 3:
               raise StopIteration      # 请求停止

   cb = State()
   for i in range(10):
       if _call_callback_maybe_halt(cb, res=i):
           print(f"在第 {cb.n} 次回调后停止")
           break
   ```
3. **需要观察的现象**：`MapWrapper(1)` 退化为串行 `map`；回调累计到第 3 次时 `_call_callback_maybe_halt` 返回 `True`，主循环随即停止。
4. **预期结果**：第一段打印 `[1, 4, 9]`；第二段打印 `在第 3 次回调后停止`。
5. 多进程路径（`pool>1`）在某些受限环境可能无法启动，若如此请标注「待本地验证」，但 `pool=1` 的串行路径通常稳定。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `MapWrapper` 在 POSIX 上要避免默认的 `fork` 启动方式？

> **答案**：`fork` 会复制父进程的整个内存与锁状态，若父进程里持有某些锁（如 NumPy/OpenBLAS 的内部锁或线程池），子进程继承后可能死锁。`MapWrapper` 因此在未显式指定 start method 时回退到 `forkserver`，规避这一经典陷阱。

**练习 2**：`_call_callback_maybe_halt` 为什么要捕获 `StopIteration` 而不是别的异常？

> **答案**：`StopIteration` 在 Python 中是「迭代结束」的语义信号，SciPy 复用它来表达「用户希望提前终止优化」。捕获它并返回「应停止」布尔值，既不需要新增异常类型，又能让用户在回调里用熟悉的 `raise StopIteration` 优雅终止。

---

### 4.5 结果容器 _RichResult 与反射/签名工具

#### 4.5.1 概念说明

SciPy 的求解类函数（`minimize`、`root`、`linprog`、`curve_fit` 等）都要返回一个「带很多字段、又好看」的结果对象。如果各自实现，字段顺序、打印格式会乱。`_RichResult` 提供了一个统一的基类：

- 它继承自 `dict`，所以既是字典又能用属性访问（`res.x`、`res.fun`）。
- 它有定制的 `__repr__`，按预设顺序打印关键字段（`message`、`success`、`status`、`fun`、`x`...），并自动隐藏冗余字段（`slack`、`con`、`crossover_nit`）。
- 最典型的子类就是 `optimize.OptimizeResult`。

本模块还包括一组「反射/签名」工具，解决「让包装函数的签名和文档看起来像原函数」的问题：

- `getfullargspec_no_self(func)` / `wrapped_inspect_signature(func)`：兼容不同 Python 版本的签名获取，且对绑定方法**不**列出 `self`。
- `_rename_parameter(old_name, new_name, dep_version)`：装饰器，平滑重命名关键字参数（旧名仍可用，并按版本发 `DeprecationWarning`）。
- `_apply_over_batch(*argdefs)`：装饰器工厂，给 `linalg` 函数自动加上「批量维度」支持（把高维输入当作一批低维切片循环处理），并自动往文档里追加 batch 说明。

#### 4.5.2 核心流程

`_RichResult.__repr__` 的排序逻辑：

```
预设字段顺序 order_keys = ['message','success','status','fun',...]
omit_keys = {'slack','con','crossover_nit','_order_keys'}  # 隐藏冗余
对 self.items()：
    丢弃 omit_keys 中的键
    按 order_keys 中的下标排序（不在表里的排最后）
用 _dict_formatter 漂亮打印（数字用 10 字符科学计数法）
```

`_rename_parameter` 装饰器：

```
if old_name in kwargs:
    若指定 dep_version：发 DeprecationWarning（提示将在 X.Y+2.Z 移除）
    若同时也有 new_name：抛 TypeError（多值冲突）
    把 kwargs[old_name] 改名到 kwargs[new_name]
调用原函数
```

`_apply_over_batch` 的批量处理（`linalg` 用得最多）：

```
1. 把声明的数组参数按 ndim 拆成「batch 形状 + core 形状」
2. 若完全没有 batch 维度：直接调原函数（快速路径）
3. 否则：广播各数组到统一 batch 形状
4. 对 batch 形状的每个 index，取出对应切片调原函数
5. 把每片结果 stack 回 batch 形状返回
6. 自动给文档追加 _batch_note 说明
```

#### 4.5.3 源码精读

`_RichResult` 既像 dict 又像对象，`__getattr__`/`__setattr__` 把属性访问转发到字典项：

[scipy/_lib/_util.py:943-985](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_util.py#L943-L985) —— `__setattr__ = dict.__setitem` 让 `res.x = 1` 等价于 `res['x'] = 1`。

`OptimizeResult` 正是它的子类：

[scipy/optimize/_optimize.py:112](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/optimize/_optimize.py#L112) —— `class OptimizeResult(_RichResult)`。

`getfullargspec_no_self` 兼容 Python 3.14+ 的签名获取（PEP 649/749 注解延迟求值）：

[scipy/_lib/_util.py:30-46](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_util.py#L30-L46) —— `wrapped_inspect_signature` 在 3.14+ 用 `annotationlib.Format.FORWARDREF` 处理未定义注解。

[scipy/_lib/_util.py:463-515](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_util.py#L463-L515) —— `getfullargspec_no_self` 基于签名重建 `FullArgSpec`，且对绑定方法不列 `self`。

`_rename_parameter` 的旧→新关键字迁移：

[scipy/_lib/_util.py:830-882](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_util.py#L830-L882) —— `dep_version` 经 `Y+2` 计算得出最终移除版本，写入警告文案。

`_apply_over_batch` 给 linalg 函数自动加批量维度的核心循环：

[scipy/_lib/_util.py:1134-1175](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_util.py#L1134-L1175) —— 先尝试「无 batch」快速路径，否则按 `np.ndindex(batch_shape)` 逐片调用并 `stack` 回去。

谁在用？`stats._distn_infrastructure` 用 `getfullargspec_no_self` 来内省分布方法的参数；`optimize._nonlin`、`optimize._minpack_py` 也用它；`linalg._decomp`、`linalg._matfuncs` 用 `_apply_over_batch` 与 `_deprecate_dtypes`。

#### 4.5.4 代码实践

1. **实践目标**：亲手用 `_RichResult` 构造一个结果对象，观察它的属性访问与漂亮打印；再用 `_rename_parameter` 给一个函数做参数改名。
2. **操作步骤**（示例代码）：

   ```python
   # 示例代码
   import warnings
   from scipy._lib._util import _RichResult, _rename_parameter

   # (1) _RichResult：既是 dict 又能用属性访问
   res = _RichResult(success=True, status=0, x=[1.0, 2.0], fun=3.14,
                     message="converged", _private=999)
   print(res.success)        # 属性访问 -> True
   print(res['fun'])         # 字典访问 -> 3.14
   print(res)                # 定制 __repr__，按顺序、隐藏 _private

   # (2) _rename_parameter：把旧名 n 改成新名 num
   @_rename_parameter('n', 'num', dep_version='1.20.0')
   def f(num):
       return num * 2

   with warnings.catch_warnings(record=True) as w:
       warnings.simplefilter("always")
       print(f(n=5))         # 旧名仍可用，但会发 DeprecationWarning
       print("警告数：", len([x for x in w if issubclass(x.category, DeprecationWarning)]))
   print(f(num=5))           # 新名无警告
   ```
3. **需要观察的现象**：`res` 既能用 `.` 也能用 `[]` 访问；打印时字段按预设顺序排列且隐藏下划线字段；用旧名 `n=` 调用会触发 `DeprecationWarning` 但仍返回正确结果。
4. **预期结果**：第一段打印 `True`、`3.14` 及格式化结果；第二段打印 `10`、`警告数：1`（或更多，取决于环境）；最后打印 `10`。
5. 若 import 失败则标注「待本地验证」。

#### 4.5.5 小练习与答案

**练习 1**：`_RichResult` 同时继承 `dict` 又定义 `__getattr__`/`__setattr__`，这样做相比普通 `dataclass` 有什么好处？

> **答案**：结果对象的字段是动态的（不同求解器返回不同字段），用 dict 作底层存储天然支持「任意键」，而 `__getattr__`/`__setattr__` 又让这些键可以用 `res.fun` 这种属性语法访问，兼顾了灵活性与易用性；定制 `__repr__` 还能保证跨子包的输出风格一致。

**练习 2**：`_rename_parameter('old','new', dep_version='1.20.0')` 会在哪个版本「移除」旧参数？这个版本号是怎么算出来的？

> **答案**：会在 `1.22.0` 移除。装饰器把 `dep_version` 的次版本号 `Y` 加 2（`1.20.0` → `1.22.0`）作为最终移除版本，写进 `DeprecationWarning` 文案，给用户两个次版本的迁移窗口。

---

## 5. 综合实践

把本讲的知识串起来，完成一个「跨子包复用调查 + 实际调用」的小任务：

**任务**：用 `grep`/`Grep` 在 `scipy/` 下统计 `_util.py` 的工具函数被哪些子包引用，找出**至少 3 个被 `optimize` 与 `stats` 同时引用**的函数，然后写一个脚本分别调用它们并解释用途。

**参考步骤**：

1. 在仓库根目录运行（只读命令）：

   ```bash
   # 示例命令
   grep -rn "from scipy._lib._util import" scipy/optimize scipy/stats | sort
   ```

2. 你应当能发现这些「跨包共享」函数（验证依据）：
   - `check_random_state`：`optimize/_dual_annealing.py`、`optimize/_differentialevolution.py`、`stats/_distn_infrastructure.py`、`stats/_resampling.py` 等。
   - `_RichResult`：`optimize/_optimize.py`（`OptimizeResult` 的基类）、`stats/_continued_fraction.py`、`stats/_distribution_infrastructure.py`。
   - `_contains_nan`：`optimize/_minpack_py.py`、`stats/_stats_py.py`、`stats/_morestats.py`。
   - `getfullargspec_no_self`：`optimize/_nonlin.py`、`optimize/_minpack_py.py`、`stats/_distn_infrastructure.py`。

3. 写一个脚本（示例代码），分别演示这三类：

   ```python
   # 示例代码
   import numpy as np
   from scipy._lib._util import check_random_state, _RichResult, _contains_nan

   # (A) check_random_state：optimize 的随机优化器与 stats 的重采样都用它
   rng = check_random_state(42)
   print("RNG:", type(rng).__name__, rng.randint(0, 10, size=3))

   # (B) _RichResult：optimize.OptimizeResult 与 stats 的若干结果对象都继承它
   res = _RichResult(success=True, x=np.array([1.0, 2.0]), fun=0.5, message="ok")
   print(res)

   # (C) _contains_nan：optimize.curve_fit 底层与 stats 检验都用它判 NaN
   print("含 NaN？", _contains_nan(np.array([1.0, np.nan, 2.0]), nan_policy="propagate"))
   ```

4. **关于 `special` 的重要说明**：你可能注意到 `scipy/special` 几乎不直接 import `_util`——因为 `special` 的核心是编译好的 C/Cython ufunc（经 `xsf` 子项目生成，见 u3-l2），Python 层很薄。所以 `_util` 的「跨包共享」主要体现在 optimize、stats、linalg、integrate、ndimage、signal 等 Python 代码量大的子包之间，special 是个有意的例外。这一点在调查时如实记录即可，不要硬凑「special 也引用」的结论。

**预期产出**：一份小报告，列出 3 个跨 optimize/stats 共享的函数、各自的 import 位置（文件:行号）、一句话用途，以及上面脚本的运行输出。如果某些命令在受限环境无法运行，标注「待本地验证」。

## 6. 本讲小结

- `scipy/_lib/_util.py` 是 SciPy 的「私有工具箱」，把各子包重复出现的样板逻辑（RNG 处理、输入校验、函数封装、结果容器、签名兼容）提炼成共享函数。
- **RNG 工具**：`check_random_state`/`rng_integers` 统一四种 seed 形态；`_transition_to_rng` 装饰器按 SPEC 7 把旧参数名平滑迁移到 `rng`，并动态修补函数签名与文档。
- **惰性分支**：`_lazyselect` 把 `np.select` 的「数组分支」换成「函数分支」，只对满足条件的元素求值，省算且更安全。
- **输入校验**：`_asarray_validated` 串联稀疏/掩码/有限性/object/浮点五道校验；`_contains_nan` 用 `xp.max` 技巧高效检测 NaN 且兼容 Array API；`_sparse.issparse` 用 `isinstance(x, SparseABC)` 一行识别稀疏类型。
- **可调用封装**：`_FunctionWrapper`/`_ScalarFunctionWrapper` 保证可 pickle 与标量返回；`MapWrapper` 把 `int`/`-1`/map-like 统一成并行 map（并规避 POSIX fork 死锁）；`_call_callback_maybe_halt` 用 `StopIteration` 表达「用户请求终止」。
- **结果与反射**：`_RichResult`（`OptimizeResult` 的基类）兼顾 dict 灵活性与属性访问；`getfullargspec_no_self`/`_rename_parameter`/`_apply_over_batch` 让包装函数保持正确的签名、文档与批量语义。
- **诚实结论**：`special` 因核心是编译 ufunc，几乎不直接用 `_util`；这些工具的复用热点是 optimize/stats/linalg/integrate/ndimage/signal 等 Python 代码量大的子包。

## 7. 下一步学习建议

- **继续本单元**：下一讲 **u2-l2（uarray 多方法与后端分发）** 会讲 `_lib/uarray.py` 与 `scipy/fft/_backend.py`，理解 SciPy 如何把同一函数分发到不同后端——与本讲的 `array_namespace` 思路互补。
- **Array API 深入**：本讲只点了 `_array_api.py` 的 `_asarray`/`array_namespace`，完整机制（CuPy/Torch/JAX/Dask、`SCIPY_ARRAY_API` 开关）见 **u2-l3**。
- **回调与低层调用**：若你对「Python 回调被 C 底层高效调用」感兴趣，接着读 **u2-l4（LowLevelCallable）**。
- **源码延伸阅读**：想看 `_util` 在真实算法里的用法，可读 `scipy/optimize/_differentialevolution.py`（同时用了 `check_random_state`/`MapWrapper`/`_FunctionWrapper`/`_transition_to_rng`）与 `scipy/stats/_continuous_distns.py`（大量 `_lazyselect` 分段公式）。
