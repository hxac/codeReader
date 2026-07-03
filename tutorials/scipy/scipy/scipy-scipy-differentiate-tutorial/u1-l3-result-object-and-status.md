# 讲义 u1-l3：结果对象 _RichResult 与状态码

## 1. 本讲目标

上一讲我们学会了「黑盒」地调用 `derivative`、调几个关键参数、并从返回结果里读出 `df`（导数估计）和 `error`（误差估计）。本讲我们要把那个返回结果对象彻底看明白。

学完本讲，你应当能够：

- 说出 `derivative` 返回对象上的每一个属性（`success` / `status` / `df` / `error` / `nit` / `nfev` / `x`）的含义；
- 看懂 `status` 的每一个取值（`0` / `-1` / `-2` / `-3` / `-4` / `1`）代表算法发生了什么；
- 用 `success` 或 `status == 0` 准确判断「这一次求导到底成没成」，并据此决定是否信任 `df`。

本讲仍然是「会用」层面的入门内容，不展开差分权重的内部推导（那是 u2 的事），只聚焦「结果对象长什么样、状态码怎么读」。

## 2. 前置知识

- 你已经会基本调用 `scipy.differentiate.derivative`，知道它返回一个类似字典的对象（见 u1-l2）。
- 了解一点 Python 的「鸭子类型」：一个对象只要支持点号属性访问 `obj.attr`，我们就能像用普通对象一样用它，哪怕它底层其实是个字典。
- 关键术语回顾（来自 u1-l2）：
  - **收敛（converge）**：随着步长逐轮缩小，导数估计值稳定下来，相邻两轮估计之差（即 `error`）已经足够小。
  - **截断误差 / 消去误差**：步长偏大带来截断误差，步长偏小又因浮点「相消」带来消去误差，二者此消彼长，是 `status=-1`（误差回升）的根因。
  - **逐元素（elementwise）**：`derivative` 对数组里的每个元素独立迭代、独立判断收敛，所以同一个返回对象里，不同元素可以处在不同的 `status`。

## 3. 本讲源码地图

本讲涉及的文件集中在两处：子包本身的核心实现，以及它依赖的两个共享工具。

| 文件 | 在本讲的作用 |
| --- | --- |
| [`scipy/differentiate/_differentiate.py`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py) | `derivative` 的全部实现：docstring 里写死了返回属性与状态码的「官方说明」，`check_termination` 里写死了状态码的判定逻辑。 |
| [`scipy/_lib/_elementwise_iterative_method.py`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_elementwise_iterative_method.py) | 通用逐元素迭代框架（简称 `eim`）。定义了状态码常量、`_loop` 主循环、以及最终结果对象的拼装逻辑。`derivative` 把迭代托付给它。 |
| [`scipy/_lib/_util.py`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_util.py) | 定义 `_RichResult` 这个「可属性访问、可美化打印」的字典容器，也就是 `derivative` 真正返回的对象类型。 |

记住一条主线：**`derivative` 自己只判定 `0` / `-1` / `-3` 三种状态，而 `-2`（达到 maxiter）和 `-4`（被 callback 叫停）是由共享的 `eim._loop` 框架在循环结束时统一赋值的**。这个分工是本讲的关键。

---

## 4. 核心概念与源码讲解

### 4.1 返回对象 _RichResult 与它的属性

#### 4.1.1 概念说明

`derivative` 返回的不是普通数组，而是一个「富结果对象」`_RichResult`。它的设计目标有两个：

1. **像对象一样好用**：可以用 `res.df`、`res.status` 这种点号写法直接取值，比 `res["df"]` 顺手。
2. **像字典一样可枚举**：底层其实就是个 `dict`，所以也能 `res.keys()`、`dict(res)`，方便程序化处理。

一个结果对象上固定有 7 个属性（`derivative` 的情况）：

| 属性 | 类型 | 含义 |
| --- | --- | --- |
| `success` | 布尔数组 | 该元素是否成功收敛（等价于 `status == 0`）。 |
| `status` | 整数数组 | 退出状态码，详见 4.2。 |
| `df` | 浮点数组 | 导数估计值；未收敛或失败时可能是 `NaN`。 |
| `error` | 浮点数组 | 误差估计，即相邻两轮 `df` 之差的绝对值。 |
| `nit` | 整数数组 | 该元素实际执行的迭代轮数。 |
| `nfev` | 整数数组 | 该元素对 `f` 的求值点数（函数调用「点数」，不是「次数」）。 |
| `x` | 浮点数组 | 求导点（与 `args`、`step_direction` 广播后的值）。 |

> 提示：这些属性的形状与 `x` 广播后的形状一致；当 `x` 是标量时，属性也都是 0 维数组，可用 `float(res.df)` 取成 Python 浮点数。

#### 4.1.2 核心流程

结果对象并不是 `derivative` 里手写的，而是由 `eim._loop` 在迭代过程中逐步「填」出来的：

1. 进入 `_loop` 前，`derivative` 先建好一个内部工作对象 `work`，里面放着 `df`、`error`、`status`、`nit`、`nfev`、`x` 等每轮都会更新的量。
2. `_loop` 拿到一张「属性对照表」`res_work_pairs`，它声明「最终结果的哪个属性 ← 取自 `work` 的哪个属性」。
3. 每当某个元素满足终止条件，框架就把该元素在 `work` 里的最新值拷进最终结果对象 `res` 的对应位置；`success` 字段由框架按 `status == 0` 自动计算并补上。
4. 循环结束后，`_prepare_result` 把所有数组重塑回原始广播形状，并按 `_order_keys` 指定的顺序美化打印。

#### 4.1.3 源码精读

`_RichResult` 本身就是一个 `dict` 子类，靠三个魔法方法把「字典语义」和「属性语义」打通。点号读写最终都落到字典的 `__getitem__` / `__setitem__` 上：

[_differentiate 所在仓库 `scipy/_lib/_util.py` 的 `_RichResult` 基类：L943-L952](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_util.py#L943-L952) —— 这段代码定义了 `_RichResult`：`__getattr__` 让 `res.df` 等价于 `res["df"]`；`__setattr__ = dict.__setitem__` 让 `res.df = v` 等价于 `res["df"] = v`。所以它「既是字典又是对象」。

[ `_RichResult.__repr__` 美化打印：L954-L982](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_util.py#L954-L982) —— 这段代码决定了 `print(res)` 时各属性的出场顺序：它先按一张固定的 `order_keys` 优先级表排序。对我们而言最重要的是 `success` 会被排在最前面（这一优先级在 `_prepare_result` 里通过 `_order_keys` 显式注入，见下方）。

`derivative` 里「属性对照表」就一行，它直接决定了最终结果上有哪些字段、以及打印顺序：

[`_differentiate.py` 的 `res_work_pairs`：L446-L447](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L446-L447) —— 这段代码声明了 6 对映射 `('status','status')`、`('df','df')`、`('error','error')`、`('nit','nit')`、`('nfev','nfev')`、`('x','x')`。注释明确说「`success` 会被自动 prepend」。这正是返回对象有 7 个属性的来源。

[`_differentiate.py` 初始化 `df` 为 NaN、`nit`/`nfev` 计数器：L405-L420](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L405-L420) —— 这段代码先把 `df` 初始化成全 `NaN`，再把 `nit=0`、`nfev=1`（那个 1 来自 `_initialize` 里为了输入校验而预先做的一次函数求值）。这就解释了为什么失败的元素 `df` 往往是 `NaN`、以及 `nfev` 的最小值是 1。

最终结果对象的拼装在 `eim._loop` 一侧。三处关键：

[`eim._loop` 初始化结果字典：L220-L225](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_elementwise_iterative_method.py#L220-L225) —— 这段代码按 `res_work_pairs` 为每个属性预分配全 0 数组，并**额外**补上 `success`、`status`、`nit`、`nfev` 的初始容器。注意 `status` 初值是 `_EINPROGRESS`（即 `1`）。

[`eim._update_active` 计算 `success`：L318-L319`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_elementwise_iterative_method.py#L318-L319) —— 这段代码用 `update_dict['success'] = work.status == 0` 生成 `success`。**这是「成功判断」的唯一来源**：`success` 永远等价于 `status == 0`，4.3 节会专门展开。

[`eim._prepare_result` 设定打印顺序：L356-L357`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_elementwise_iterative_method.py#L356-L357) —— 这段代码把 `res['_order_keys']` 设成 `['success'] + res_work_pairs 的左列`，于是 `print(res)` 时属性按 `success, status, df, error, nit, nfev, x` 的顺序出现。

#### 4.1.4 代码实践

**实践目标**：亲眼看到返回对象「既是字典又是对象」，并确认 7 个属性都在。

**操作步骤**：

```python
import numpy as np
from scipy.differentiate import derivative

res = derivative(np.exp, 1.0)

# (1) 点号访问：当对象用
print("df =", float(res.df))
print("error =", float(res.error))

# (2) 字典访问：当字典用
print("keys =", list(res.keys()))

# (3) 直接打印，观察美化输出的属性顺序
print(res)
```

**需要观察的现象**：`res.df` 与 `res["df"]` 给出同一个值；`res.keys()` 能列出属性名；直接 `print(res)` 时，最先打印的是 `success`，随后依次是 `status`、`df`、`error`、`nit`、`nfev`、`x`。

**预期结果**：`df` 应非常接近 \( e \approx 2.718281828 \)；`success` 为 `True`；打印顺序与上节 `_order_keys` 一致。具体数值待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `res.df = 3.0` 这样的赋值不会报错，反而会改变结果？

**参考答案**：因为 `_RichResult` 把 `__setattr__` 直接绑定到了 `dict.__setitem__`（见 `scipy/_lib/_util.py` 第 951 行），点号赋值本质就是往字典里写一个键 `df`。这也是为什么文档反复强调 callback「不得修改 `res`」——它太容易被改动了。

**练习 2**：`res.nfev` 最小为什么不会是 0？

**参考答案**：`derivative` 在 `_differentiate.py` 第 420 行把 `nfev` 初始化为 `1`，对应 `_initialize` 里为了输入校验和确定输出形状而预先做的那一次函数求值（详见 `_differentiate.py` 第 392–398 行的注释）。所以即使一次迭代都没真正跑，`nfev` 也至少是 1。

---

### 4.2 status 状态码：derivative 如何决定停与不停

#### 4.2.1 概念说明

`status` 是一个整数数组，描述「这个元素的迭代是因为什么原因停下来的」。`derivative` 的 docstring 给出了官方语义（见下方源码精读）。汇总如下：

| status | 常量名 | 含义 | 由谁赋值 |
| --- | --- | --- | --- |
| `0` | `_ECONVERGED` | 收敛到指定容差。 | `derivative` 的 `check_termination` |
| `-1` | `_EERRORINCREASE` | 误差估计回升（疑似相消），提前终止。 | `derivative` 的 `check_termination` |
| `-2` | `_ECONVERR` | 达到 `maxiter` 仍未收敛。 | `eim._loop`（循环结束时） |
| `-3` | `_EVALUEERR` | 遇到非有限值（`NaN` / `inf`）。 | `derivative` 的 `check_termination` |
| `-4` | `_ECALLBACK` | 被 `callback` 主动叫停。 | `eim._loop`（循环结束时） |
| `1` | `_EINPROGRESS` | 进行中（只在 `callback` 里看得到）。 | `eim._loop` 初值 |

要点：`-2` 和 `-4` **不在** `derivative` 自己的代码里出现，而是由共享框架 `eim._loop` 在主循环收尾时统一兜底赋值——因为「达到最大迭代」和「被 callback 叫停」是所有逐元素迭代算法共有的终止方式，框架统一处理。

#### 4.2.2 核心流程

`derivative` 每轮迭代的判定顺序（在 `check_termination` 里）是：

1. **收敛判定**：若误差足够小，即满足
   \[ \text{error} < \text{atol} + \text{rtol}\cdot|\text{df}|, \]
   则 `status = 0` 并标记停止。
2. **非有限值判定**（仅在 `nit > 0` 时生效）：若 `x` 或 `df` 出现 `NaN`/`inf`，则把 `df` 置为 `NaN`、`status = -3` 并停止。
3. **误差回升判定**：若本轮误差比上一轮大了一个数量级（`error > 10 * error_last`），则 `status = -1` 并停止——这是为了避免步长小到被浮点相消主导。

只要上述任一条件命中，该元素就被移出「活跃集」，不再参与后续迭代。若一直跑到 `maxiter` 仍无任何条件命中，框架在循环末尾把剩余活跃元素的 `status` 统一改成 `-2`（或 callback 叫停时改成 `-4`）。

判定优先级的伪代码：

```
if error < atol + rtol*|df|:          status = 0      # 收敛
elif (not finite(x)) or (not finite(df)): status = -3 # 非有限值
elif error > 10*error_last:           status = -1     # 误差回升
# 循环结束仍未停 → 框架兜底
if 仍活跃 and 未被callback叫停:        status = -2     # 达到 maxiter
if 被callback叫停:                    status = -4
```

#### 4.2.3 源码精读

先看 docstring 里写死的「官方说明书」，这是最权威的状态码定义：

[`_differentiate.py` docstring 中 `success` / `status` 的定义：L170-L181](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L170-L181) —— 这段文档明确列出：`0` 收敛、`-1` 误差增大、`-2` 达到最大迭代、`-3` 遇到非有限值、`-4` 被 callback 终止、`1` 仅在 callback 中表示「进行中」。读源码不确定时，回来对照这段最可靠。

再看 `derivative` 自己定义的那个「专属」状态码常量：

[`_differentiate.py` 顶部的 `_EERRORINCREASE`：L9-L9](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L9-L9) —— `_EERRORINCREASE = -1`。`derivative` 只「自定义」了这一个状态码（误差回升）；其余 `0` / `-2` / `-3` / `-4` 都复用 `eim` 里的常量。

通用常量定义在 `eim` 顶部：

[`eim` 的状态码常量：L21-L27](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_elementwise_iterative_method.py#L21-L27) —— 这段代码定义了 `_ESIGNERR=-1`、`_ECONVERR=-2`、`_EVALUEERR=-3`、`_ECALLBACK=-4`、`_EINPUTERR=-5`、`_ECONVERGED=0`、`_EINPROGRESS=1`。注意这是「通用」框架的命名：在 `derivative` 语境下，`-1` 被重新解释成「误差回升」（用 `_EERRORINCREASE` 别名），`-2` 是「达到 maxiter」，`-3` 是「非有限值」。

`derivative` 的判定逻辑本体（三段）：

[`check_termination` 收敛判定：L566-L568](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L566-L568) —— `i = work.error < work.atol + work.rtol*abs(work.df)`，命中即 `status = _ECONVERGED (0)`。这就是收敛判据 \( \text{error} < \text{atol} + \text{rtol}\cdot|\text{df}| \) 的代码原文。

[`check_termination` 非有限值判定：L570-L574](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L570-L574) —— 这段代码在 `nit > 0` 时检测「`x` 与 `df` 是否都有限」，只要有一个不有限且该元素尚未停止，就把 `df` 置 `NaN`、`status = _EVALUEERR (-3)`。注意它用 `| stop` 排除已经判定收敛的元素，避免误杀。

[`check_termination` 误差回升判定：L583-L585](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L583-L585) —— `i = (work.error > work.error_last*10) & ~stop`，命中即 `status = _EERRORINCREASE (-1)`。注释解释了动机：浮点相消会让太小的步长反而增大误差，这条启发式用于在「误差开始反弹」时及时止损。

最后是框架兜底赋的 `-2` / `-4`：

[`eim._loop` 循环结束时的兜底状态：L278-L278](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_elementwise_iterative_method.py#L278-L278) —— `work.status = ... .set(_ECALLBACK if cb_terminate else _ECONVERR)`。退出主循环后，所有「仍然活跃」（既没收敛、也没非有限值/误差回升）的元素，在这里被统一标记为 `-4`（被 callback 叫停）或 `-2`（跑满 maxiter）。这就是 `-2` / `-4` 不在 `derivative` 代码里出现的根本原因。

#### 4.2.4 代码实践

**实践目标**：触发 `-3`（非有限值）这一个 `derivative` 自己判定的状态码，验证它与源码逻辑一致。

**操作步骤**：

```python
import numpy as np
from scipy.differentiate import derivative

# 一个永远返回 NaN 的函数
def f_nan(x):
    return np.full_like(x, np.nan)

res = derivative(f_nan, 1.0)
print("status =", int(res.status))   # 预期 -3
print("success =", bool(res.success))# 预期 False
print("df =", float(res.df))         # 预期 nan
```

**需要观察的现象**：因为 `f` 返回 `NaN`，`post_func_eval` 算出的 `df` 也是 `NaN`；在 `check_termination` 的非有限值分支（`nit > 0`）命中后，`status` 被设为 `-3`，`df` 被显式置为 `NaN`。

**预期结果**：`status == -3`、`success == False`、`df` 为 `nan`。这一结果可直接由 `_differentiate.py` 第 570–574 行推出（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `derivative` 的代码里搜不到给 `status` 赋 `-2` 的地方，却仍可能返回 `-2`？

**参考答案**：`-2`（`_ECONVERR`）由共享框架 `eim._loop` 在主循环结束时统一兜底赋值（`_elementwise_iterative_method.py` 第 278 行）。当某个元素跑满 `maxiter` 轮都没命中收敛/非有限值/误差回升任何一个条件时，框架就把它的 `status` 设为 `-2`。`derivative` 因此不必自己处理「达到最大迭代」。

**练习 2**：误差回升判定里为什么是 `error > 10 * error_last`，而不是 `error > error_last`？

**参考答案**：在稳定收敛阶段，`error` 本就会随步长缩减而单调下降；偶尔的小幅波动（比如舍入造成的抖动）不应触发终止。只有当误差**显著**回升（这里取「大一个数量级」）时，才更可靠地说明已经进入浮点相消主导的区域，此时提前停在误差最小的那一轮更有意义。

---

### 4.3 success 与失败判断：一次求导到底成没成

#### 4.3.1 概念说明

实际使用中，我们最关心的往往是一个问题：**这次求导的 `df` 能不能用？** 答案由 `success`（或等价的 `status == 0`）给出。

- `success == True`（即 `status == 0`）：算法按指定容差收敛，`df` 可信，`error` 是其保守的误差上界。
- `success == False`：算法没正常收敛。此时要根据 `status` 进一步分流：
  - `status == -2`：通常是因为 `order` 太低或 `maxiter` 太小，可调大 `order` / `maxiter`，或放宽 `tolerances`；
  - `status == -1`：步长已小到被相消误差主导，往往说明已经接近该精度下能达到的最好结果，`df` 通常仍然接近真值（可结合 `error` 判断）；
  - `status == -3`：函数本身返回了 `NaN`/`inf`，多半是定义域或数值范围问题（可结合 `step_direction` 做单侧差分，见 u4-l2）；
  - `status == -4`：你自己的 `callback` 主动叫停的。

> 重要：`success` 与 `status` 在数组场景下也是逐元素的。对一个数组 `x` 调用一次 `derivative`，可能有的元素 `success=True`、有的 `success=False`，需要用布尔掩码分别处理。

#### 4.3.2 核心流程

判断一次求导是否成功的标准套路：

```
res = derivative(f, x)
ok = np.asarray(res.success)          # 或 np.asarray(res.status) == 0
if np.all(ok):
    信任 res.df
else:
    按 res.status 分类处理失败元素
    （-2 调参 / -1 看是否可接受 / -3 查定义域 / -4 自查 callback）
```

要特别注意两点「坑」：

1. **`df` 可能是 `NaN`**：未收敛或 `-3` 时，`df` 往往是 `NaN`（初始值就是 `NaN`，见 4.1.3）。所以「先看 `success` 再用 `df`」是安全习惯。
2. **真导数恰为 0 时的「假失败」**：当真导数正好是 0（如鞍点），默认 `rtol` 判据 `error < atol + rtol*|df|` 里的 `rtol*|df|` 项趋于 0，只剩极小的默认 `atol`（最小正规数），很难满足，于是常常以 `-2` 收场。这种情况应显式给一个合理的 `atol`（如 `tolerances=dict(atol=1e-12)`），见 u4-l3。

#### 4.3.3 源码精读

`success` 的唯一来源在前文已引用，这里再强调它的「等价性」：

[`eim._update_active` 中 `success = (status == 0)`：L318-L319](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_elementwise_iterative_method.py#L318-L319) —— `update_dict['success'] = work.status == 0`。也就是说 `success` 完全是 `status == 0` 的逐元素布尔值，没有任何额外信息。判断成败时，用 `res.success` 和用 `res.status == 0` 完全等价。

`df` 的初始值与失败值都是 `NaN`，呼应 4.3.2 提到的「坑」：

[`_differentiate.py` 把 `df` 初始化为 `NaN`：L405-L405](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L405-L405) —— `df = xp.full_like(f, xp.nan)`。再加上 `check_termination` 在 `-3` 分支里把 `df` 显式置 `NaN`（第 572 行），就解释了为什么失败元素的 `df` 是 `NaN`。

默认容差与「零导数陷阱」的官方说明：

[`_differentiate.py` docstring 关于默认容差与零导数的说明：L108-L111 与 L227-L230](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L227-L230) —— 文档明确：默认 `atol` 是「该 dtype 的最小正规数」、默认 `rtol` 是「该 dtype 精度的平方根」；并警告在真导数恰为 0 的点，默认容差很难满足，建议显式指定 `atol`（如 `1e-12`）以改善收敛。这正是 4.3.2 所说「假失败」的官方依据。

#### 4.3.4 代码实践

**实践目标**：体会「`success=False` 但 `df` 仍可能接近真值」的情形，并学会用 `success` 掩码分流处理。

**操作步骤**：

```python
import numpy as np
from scipy.differentiate import derivative

# 对 f(x)=x**3 求导，真导数在 x=1 处为 3。低 order 会拖慢收敛。
res = derivative(lambda x: x**3, 1.0, order=2, maxiter=10)

ok = np.asarray(res.success)
print("status =", int(res.status), " success =", bool(ok))
print("df =", float(res.df), " 真值 = 3.0")

# 安全用法：先看 success，再决定是否信任 df
df = float(res.df)
print("df =", df, "（可信）" if bool(ok) else "（不可信，请查 status）")
```

**需要观察的现象**：`order=2` 时收敛很慢（每轮误差约缩减为 \(1/2^2=1/4 \)，而 `order=8` 是 \(1/2^8=1/256 \)），默认 `maxiter=10` 往往不足以达到默认容差，于是 `status` 很可能是 `-2`、`success` 为 `False`。

**预期结果**：`status` 预期为 `-2`、`success` 为 `False`（具体取决于本地精度与容差，待本地验证）；尽管如此，`df` 通常仍接近真值 `3.0`——这正是「失败不等于一无是处」的体现。把 `order` 调回默认 `8` 再跑一次，`success` 应变回 `True`。

#### 4.3.5 小练习与答案

**练习 1**：写一段代码，对数组 `x = np.array([0.0, 1.0])`（其中 `x=0` 处 `sin` 的真导数为 1，而 `(x-1)**3` 在 `x=1` 处真导数为 0）分别求导，并用 `success` 掩码把「可信」与「不可信」的 `df` 分开打印。

**参考答案**：

```python
import numpy as np
from scipy.differentiate import derivative

# 对两个不同函数分别演示；这里用同一个 f 演示逐元素掩码
f = lambda x: np.sin(x)
res = derivative(f, np.array([0.0, 1.0]))
ok = np.asarray(res.success)
print("可信 df:", np.asarray(res.df)[ok])
print("不可信 status:", np.asarray(res.status)[~ok])
```
对真导数为 0 的点（如 `(x-1)**3` 在 `x=1`），不设 `atol` 时该元素常落入 `~ok`；显式给 `tolerances=dict(atol=1e-12)` 后即可让它收敛。

**练习 2**：`res.success` 和 `np.asarray(res.status) == 0` 会不会给出不同结果？

**参考答案**：不会。`success` 就是框架按 `work.status == 0` 计算出来的（`_elementwise_iterative_method.py` 第 319 行），二者逐元素完全相同。选哪个只是风格问题：`success` 更易读，`status == 0` 则在和其它状态码混用时更直观。

---

## 5. 综合实践

把本讲三个最小模块串起来，做一次「状态码全景」实验：在**一次**实践脚本里分别触发 `0`、`-2`、`-3` 三种状态，并打印各自的 `status` 与 `success`，从而验证你对源码判定的理解。

**实践目标**：构造三种典型场景，让 `derivative` 分别返回 `status=0`（收敛）、`status=-2`（达到 maxiter）、`status=-3`（非有限值），并解释每种结果由源码的哪一段产生。

**操作步骤**：

```python
import numpy as np
from scipy.differentiate import derivative

def show(name, res):
    s = int(np.asarray(res.status))
    ok = bool(np.asarray(res.success))
    df = float(np.asarray(res.df))
    print(f"{name:20s} status={s:3d}  success={ok}  df={df}")

# (1) 收敛良好：exp 在 x=1，默认参数
show("exp default", derivative(np.exp, 1.0))

# (2) 触达 maxiter：order=2 收敛极慢，默认 maxiter=10 不够
show("exp order=2", derivative(np.exp, 1.0, order=2))

# (3) 非有限值：函数返回 NaN
def f_nan(x):
    return np.full_like(x, np.nan)
show("nan function", derivative(f_nan, 1.0))
```

**需要观察的现象与对应源码**：

- **(1) `exp default`**：`status=0`、`success=True`、`df≈e`。由 `check_termination` 的收敛分支（`_differentiate.py` 第 566–568 行）判定。
- **(2) `exp order=2`**：每轮误差约缩减为 \(1/\text{step\_factor}^{\text{order}}=1/2^2=1/4 \)，10 轮后仍达不到默认容差 \( \text{rtol}\approx\sqrt{\varepsilon}\approx1.5\times10^{-8} \)，于是被 `eim._loop` 兜底标为 `status=-2`（`_elementwise_iterative_method.py` 第 278 行），`success=False`。
- **(3) `nan function`**：`df` 为 `NaN`，命中非有限值分支，`status=-3`、`success=False`（`_differentiate.py` 第 570–574 行）。

**预期结果**：三种 `status` 分别为 `0` / `-2` / `-3`，`success` 分别为 `True` / `False` / `False`。其中 `order=2` 是否真会触达 `-2` 取决于本地浮点精度与默认容差——若你的环境里它意外收敛了，可把 `maxiter` 调小（如 `maxiter=3`）来稳定复现 `-2`。具体数值待本地验证。

**进阶**：再加一组「误差回升」实验——用一个在步长极小时会被相消误差主导的函数、或显式 `tolerances=dict(atol=0, rtol=0)` 强制跑满 `maxiter`，观察是否能在某些元素上看到 `status=-1`（对应 `_differentiate.py` 第 583–585 行）。把四种状态凑齐，你就完整复现了 `derivative` 的全部退出路径。

## 6. 本讲小结

- `derivative` 返回一个 `_RichResult`：本质是 `dict`，但支持点号属性访问；固定有 `success`、`status`、`df`、`error`、`nit`、`nfev`、`x` 七个属性，打印顺序由 `_order_keys` 决定。
- `status` 共 6 种取值：`0` 收敛、`-1` 误差回升、`-2` 达到 maxiter、`-3` 非有限值、`-4` 被 callback 叫停、`1` 进行中（仅 callback 内）。
- 关键分工：`0` / `-1` / `-3` 由 `derivative` 自己的 `check_termination` 判定；`-2` / `-4` 由共享框架 `eim._loop` 在循环结束时统一兜底赋值。
- `success` 永远等价于 `status == 0`，是判断「`df` 能不能用」的第一道闸门；失败时按 `status` 分流处理。
- 失败元素的 `df` 常常是 `NaN`（初始值即为 `NaN`），所以「先看 `success` 再用 `df`」是安全习惯。
- 真导数恰为 0 的点（鞍点）容易因默认容差过严而「假失败」（`-2`），需显式设置合理的 `atol`。

## 7. 下一步学习建议

到这里，你已经能熟练「黑盒」使用 `derivative` 并读懂它的结果对象与状态码。接下来的 u2 单元将进入**白盒**阶段：

- **u2-l1（输入校验 `_derivative_iv`）** 会拆解 `derivative` 在进入迭代前如何校验 `f`、`tolerances`、`maxiter`、`order`、`step_direction` 等参数——届时你会看到这些参数的合法范围是如何被强制保证的。
- **u2-l5（收敛判断与终止 `check_termination`）** 是本讲 4.2 的深入版，会逐行讲解三条终止判据背后的数值原理（尤其是误差回升与相消误差的关系）。
- 如果你更关心应用，可先跳到 **u4-l3（数值精度与调参）**，系统理解「截断误差 vs 消去误差」、大 `|x|` 下的步长选择，以及零导数陷阱的应对。

建议下一步直接进入 u2-l1，把 `derivative` 从「会用」推进到「读懂」。
