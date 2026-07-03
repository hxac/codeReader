# Array API 后端支持与跨后端实现

## 1. 本讲目标

前几讲我们一直把 `derivative` 当作一个“NumPy 函数”来读：到处都是 `np.xxx`、原地索引赋值 `a[mask] = b`。但实际上 `scipy.differentiate` 是 SciPy 中**跨后端（Array API）支持最彻底**的子包之一——同一份代码既能吃 NumPy 数组，也能在设了开关后吃 PyTorch、JAX、CuPy、Dask 数组。本讲就拆解这层“后端无关”是怎么做到的。读完本讲你应该能够：

1. 理解 **Array API 标准**与“命名空间 `xp`”抽象，说清楚 `array_namespace` 如何根据全局开关 `SCIPY_ARRAY_API` 决定返回 NumPy 还是其它后端，以及 `xp_promote` / `xp_copy` 在其中扮演的角色。
2. 掌握 `xpx.at(...)[idx].set(...)` 这种**函数式索引赋值**写法，理解它为什么能替代 NumPy 的原地 `a[idx] = b`，以及它对不可变（immutable）后端（如 JAX）为何是必需的。
3. 读懂 `@xp_capabilities(...)` 装饰器：它如何把“支持/跳过哪些后端”这一信息**同时**用于两件事——给函数 docstring 自动注入一张支持矩阵表，以及驱动测试自动生成 `pytest.mark.skip_xp_backends` 标记。

> 本讲承接 **u2-l6（eim._loop 框架）**：u2-l6 讲的是算法骨架，本讲讲的是骨架里的每一块“肉”如何写成与后端无关的代码。建议先完成 u2 系列再读本讲。

## 2. 前置知识

### 2.1 什么是 Array API 标准

[Python Array API 标准](https://data-apis.org/array-api/latest/purpose_and_scope.html) 是一组社区约定的**数组库统一接口**。它规定：无论底层是 NumPy、PyTorch、JAX 还是 CuPy，只要实现方都提供同名同语义的函数（`xp.asarray`、`xp.concat`、`xp.astype`、`xp.broadcast_to`、`xp.isfinite`……），上层代码就能“写一遍、跑多家”。

关键抽象是**命名空间（namespace）**：把后端模块本身取个别名叫 `xp`，于是 `xp.asarray(...)` 在 NumPy 后端就是 `np.asarray`，在 torch 后端就是 `torch.asarray`。`scipy.differentiate` 里你会在每个函数开头看到一行 `xp = array_namespace(x)`，从此之后该函数内部几乎不再出现裸 `np.`（除了少数“明知是标量、与后端无关”的校验）。

### 2.2 不可变（immutable / functional）数组后端

NumPy 数组是**可变**的：`a[0] = 1` 会就地修改 `a`。但 JAX 的 `jax.Array`、以及 Array API 标准本身，都倾向于**不可变**语义——数组一旦创建就不能改，要“修改”只能**生成一个新数组**。原因是 JAX 这类后端要在 GPU/TPU 上做追踪与自动微分，原地写回会破坏追踪链。

因此 NumPy 里随手写的 `a[mask] = b`，在跨后端代码里必须改成“返回新数组”的等价写法。这正是 `xpx.at` 存在的理由（见 4.2）。

### 2.3 跨后端是“可选开启”的：`SCIPY_ARRAY_API`

SciPy 默认**不开**跨后端：为了不拖慢绝大多数只用 NumPy 的用户，默认情况下所有数组都按 NumPy 处理。需要用其它后端时，设置环境变量 `SCIPY_ARRAY_API=1` 并传入对应类型的数组即可。这层“开关”就藏在 `array_namespace` 里（见 4.1）。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [`scipy/differentiate/_differentiate.py`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py) | `derivative` / `jacobian` / `hessian` 的实现。顶部导入跨后端工具；函数体里大量出现 `xp.*` 与 `xpx.at(...)`；三个公开函数都用 `@xp_capabilities` 装饰 |
| [`scipy/_lib/_array_api.py`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_array_api.py) | SciPy 内部的 Array API 工具集：定义 `xp_copy`、`xp_promote`、`xp_capabilities`、`make_xp_test_case` 等，并维护全局能力表 `xp_capabilities_table` |
| [`scipy/_lib/_array_api_override.py`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_array_api_override.py) | `array_namespace` 的真正实现：读取 `SCIPY_ARRAY_API` 开关、校验数组类、分发到 NumPy 或其它后端命名空间 |

---

## 4. 核心概念与源码讲解

### 4.1 `array_namespace` 后端抽象与 `xp_promote` / `xp_copy`

#### 4.1.1 概念说明

要让一份代码跨后端，第一步是把“具体后端”抽象成一个变量 `xp`。`array_namespace(x)` 的职责就是：**看一眼输入数组 `x`，告诉我它是哪家后端的，把那家后端的模块对象返回出来**。

拿到 `xp` 之后，代码里所有数组操作都改写为 `xp.xxx` 形式。但有两类操作光有 `xp` 还不够顺手，需要额外封装：

- **类型提升与广播**：不同输入可能是整数、浮点、Python 标量，需要统一提升到某个公共 dtype（必要时强制转浮点），可选地广播到同形状。`xp_promote` 干这件事。
- **拷贝**：跨后端地“复制一个数组”。不同后端的拷贝语义不同（NumPy 老版本 `np.asarray` 甚至不支持 `copy=` 关键字），`xp_copy` 屏蔽了这些差异。

#### 4.1.2 核心流程

`derivative` 的输入校验函数 `_derivative_iv` 第一行就取出 `xp`：

```text
xp = array_namespace(x)          # 识别后端
... 此后该函数内：xp.asarray / xp.broadcast_arrays ...
```

之后整个 `derivative` 主流程里，凡是创建/变换数组的地方都用 `xp.*`：`xp.full_like`、`xp.broadcast_to`、`xp.reshape`、`xp.astype`、`xp.sign`、`xp.concat`、`xp.isfinite`、`xp.abs`…… 全部来自 `xp`，因此换后端时这些调用会自动指向对应实现。

`array_namespace` 内部的分发逻辑（简化伪代码）：

```text
if not SCIPY_ARRAY_API:          # 默认：开关未开
    return np_compat             # 直接返回 NumPy 兼容命名空间，跳过一切校验
# 开关开了：把输入分成 numpy 数组与非 numpy 的 API 数组
# 若全是 numpy/array-like → 返回 np_compat（快路径）
# 否则交给 array_api_compat.array_namespace(...) 推断公共后端
```

`jacobian` / `hessian` 同样在开头取 `xp` 并用 `xp_promote` 把 `x` 强制提升为浮点：

```text
xp = array_namespace(x)
x0 = xp_promote(x, force_floating=True, xp=xp)
```

`force_floating=True` 很关键：差分法要算 \(f(x+h)\)，若 `x` 是整数数组，`x+h` 仍是整数、扰动会被截断掉，因此必须先转浮点。

#### 4.1.3 源码精读

`derivative` 顶部一次性导入全部跨后端工具：

[scipy/differentiate/_differentiate.py:6-7](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L6-L7) —— 从 `scipy._lib._array_api` 导入 `array_namespace`、`xp_copy`、`xp_promote`、`xp_capabilities`，并把 `array_api_extra` 别名为 `xpx`。这是本讲三个模块共同的“弹药库”。

[scipy/differentiate/_differentiate.py:14](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L14) —— `_derivative_iv` 第一行 `xp = array_namespace(x)`，整个 `derivative` 的后端身份在此确定。

[scipy/_lib/_array_api_override.py:111-113](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_array_api_override.py#L111-L113) —— `array_namespace` 的开关快路径：`if not SCIPY_ARRAY_API: return np_compat`。这就是“默认不跨后端、零开销”的实现。注释（L93-94）也讲清了：开关关闭时直接返回 NumPy 命名空间并跳过所有合规校验。

[scipy/_lib/_array_api.py:123-148](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_array_api.py#L123-L148) —— `xp_copy` 的实现：先 `array_namespace` 推断后端，再委托给内部的 `_asarray(x, copy=True, xp=xp)`。注释提到它绕开 `np.copy` 的 `subok`/`order`，且兼容“老版 NumPy 的 `np.asarray` 不支持 `copy=` 关键字”这一历史包袱。

[scipy/_lib/_array_api.py:540-605](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_array_api.py#L540-L605) —— `xp_promote`：把若干参数提升到统一 dtype（`xp_result_type`），可选 `broadcast=True` 广播到同形状、`force_floating=True` 强制浮点；`None` 参数被跳过。docstring（L547-548）点明它“通常紧跟在 `array_namespace` 之后调用”。

[scipy/differentiate/_differentiate.py:921-922](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L921-L922) —— `jacobian` 开头 `xp = array_namespace(x)` 加 `x0 = xp_promote(x, force_floating=True, xp=xp)`，是“先认后端、再强制浮点”的标准起手式。`hessian` 在 [L1111-1112](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L1111-L1112) 做同样的事。

#### 4.1.4 代码实践

**目标**：亲手观察 `SCIPY_ARRAY_API` 开关如何改变 `array_namespace` 的返回，并体会 `xp_promote` 的强制浮点效果。

**操作步骤**（纯 NumPy 环境即可，不需要安装 torch/jax）：

1. 写一个脚本 `probe_xp.py`（示例代码）：

   ```python
   import os
   import numpy as np
   from scipy._lib._array_api_override import array_namespace
   from scipy._lib._array_api import xp_promote

   x = np.asarray([1, 2, 3])          # 整数数组
   xp = array_namespace(x)
   print("namespace:", xp.__name__)
   x0 = xp_promote(x, force_floating=True, xp=xp)
   print("dtype after promote:", x0.dtype)
   ```

2. 先用默认环境运行：`python probe_xp.py`。
3. 再带开关运行：`SCIPY_ARRAY_API=1 python probe_xp.py`。

**需要观察的现象**：

- 两次运行 `namespace` 的输出都应类似 `array_api_compat.numpy`（因为输入是 NumPy 数组，两路都快路径返回 NumPy 命名空间）。
- `dtype` 在两次运行中都应从 `int64` 变成 `float64`，验证 `force_floating=True` 的作用——这也解释了为什么 `jacobian` 必须先 `xp_promote` 再做差分。

**预期结果**：namespace 为 NumPy 兼容命名空间；dtype 由整数提升为 float64。若你本地装有 PyTorch/JAX，可把第 4 行改成 `import torch; x = torch.asarray([1,2,3])` 并用 `SCIPY_ARRAY_API=1` 运行，会看到 namespace 变成 torch 相关命名空间（待本地验证：取决于是否安装了对应后端）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `_derivative_iv` 里校验容差时用的是裸 `np.asarray`/`np.issubdtype`（见 [L28-33](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L28-L33)），而不是 `xp.asarray`？

**答案**：因为 `atol`/`rtol`/`step_factor` 是**纯 Python 标量**，与数组后端无关（源码注释 L26 明说“tolerances are floats, not arrays; OK to use NumPy”）。用 NumPy 校验标量既快又简单，不需要走后端抽象。

**练习 2**：`jacobian` 为什么必须 `force_floating=True`？如果不提升，对一个整数 `x` 会出什么问题？

**答案**：差分法要计算 \(x+h\) 并观察 \(f\) 的微小变化。若 `x` 是整数 dtype，`x+h`（`h` 是浮点步长）虽然会自动转浮点，但更关键的是扰动注入、权重相乘等中间量必须全程浮点；`force_floating=True` 从源头保证 `x0` 是浮点，避免整数截断吃掉差分信号。

---

### 4.2 `xpx.at`：函数式索引赋值

#### 4.2.1 概念说明

`xpx` 是 `scipy._external.array_api_extra` 的别名（见导入 [L7](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L7)）。`array_api_extra` 是 SciPy 生态里“补 Array API 标准缺口”的库，`xpx.at` 就是其中最常用的工具之一。

它的作用是**用函数式风格完成 NumPy 的原地索引赋值**。对照表：

| NumPy 原地写法（可变） | `xpx.at` 等价写法（不可变，返回新数组） |
|---|---|
| `a[mask] = b` | `a = xpx.at(a)[mask].set(b)` |
| `a[i] *= -1` | `a = xpx.at(a)[i].multiply(-1)` |
| `a[i, j] = v` | `a = xpx.at(a)[i, j].set(v)` |

两种写法语义一致，区别在于：

- NumPy 版**就地修改** `a`，对 JAX 这种不可变后端无效（会报错或无效）。
- `xpx.at` 版**返回一个新数组**，必须用 `a = ...` 接住；它内部会针对不同后端选择最优实现（NumPy 后端下其实就是就地写回再返回，几乎没有额外开销）。

`xpx.at` 支持多种“方法”：`.set`（赋值）、`.add`、`.subtract`、`.multiply`、`.divide`、`.min`、`.max` 等，对应 NumPy ufunc 的 `at` 方法语义。索引 `[]` 里既可以是**布尔掩码**，也可以是**整数/花式索引**。

#### 4.2.2 核心流程

`derivative` 主流程里几乎所有“往数组某个子集写值”的操作都改写成了 `xpx.at`。典型模式：

```text
x_eval = xp.zeros((n_points, n_new), dtype=...)   # 先建一个全零“画布”
x_eval = xpx.at(x_eval)[ir].set(...)              # 右侧点填进去
x_eval = xpx.at(x_eval)[ic].set(...)              # 中央点填进去
x_eval = xpx.at(x_eval)[il].set(...)              # 左侧点填进去
```

注意每次都要 `x_eval = ...` 重新接住返回值——因为 `xpx.at` 不就地改 `x_eval`，而是返回改好的新数组。这种“画布 + 分块掩码写入”的写法，配合 `pre_func_eval`/`post_func_eval` 里 `il/ic/ir/io` 四个布尔掩码（左/中/右/单侧），把中心差分和单侧差分统一到同一份代码里。

#### 4.2.3 源码精读

`derivative` 初始化时，用布尔掩码把“非正初始步长”标记为 `nan`（边界保护）：

[scipy/differentiate/_differentiate.py:417](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L417) —— `h0 = xpx.at(h0)[h0 <= 0].set(xp.nan)`：这正是 NumPy `h0[h0 <= 0] = np.nan` 的函数式等价。索引是**布尔掩码** `h0 <= 0`。

`pre_func_eval` 里构建求值点矩阵（见 u2-l3 详讲），三行 `xpx.at` 分别填右/中/左三类方向的点：

[scipy/differentiate/_differentiate.py:488-492](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L488-L492) —— 先 `x_eval = xp.zeros(...)` 建画布，再用 `ir/ic/il` 三个布尔掩码 `.set(...)` 分块写入。每行都重新接住返回值。

`post_func_eval` 里更新历史函数值缓存 `work.fs` 和导数估计 `work.df`，最后还用 `.multiply(-1)` 纠正左侧符号：

[scipy/differentiate/_differentiate.py:541-549](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L541-L549) —— `work.fs = xpx.at(work.fs)[ic].set(work_fc)` 等四行 `.set`，以及 `work.df = xpx.at(work.df)[il].multiply(-1)`。`.multiply(-1)` 等价于 NumPy `work.df[il] *= -1`，是“反射函数 trick”纠正单侧左差的符号（u2-l4 / u4-l2 讲过原理）。

`check_termination` 里根据收敛/非有限/误差回升三类判据，用 `xpx.at` 把对应元素的 `status` 和 `stop` 掩码改写：

[scipy/differentiate/_differentiate.py:567-585](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L567-L585) —— `work.status = xpx.at(work.status)[i].set(eim._ECONVERGED)`、`stop = xpx.at(stop)[i].set(True)` 等，逐条用布尔掩码 `i` 写状态码。

`jacobian` 的扰动注入用的是**二维整数（花式）索引**，而不是布尔掩码：

[scipy/differentiate/_differentiate.py:938-939](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L938-L939) —— `xph = xp_copy(xp.broadcast_to(xph, new_shape), xp=xp)` 先复制出 `m×m` 扰动矩阵（必须 `xp_copy`，因为 `broadcast_to` 返回只读视图），再 `xph = xpx.at(xph)[i, i].set(x)` 把扰动值写到对角线 `[i, i]`（`i = xp.arange(m)`）。这等价于 NumPy 的 `xph[i, i] = x`。

#### 4.2.4 代码实践

**目标**：用 NumPy 验证 `xpx.at` 与原地索引赋值语义完全一致，并直观感受“必须接住返回值”。

**操作步骤**（示例代码）：

```python
import numpy as np
import scipy._external.array_api_extra as xpx

a = np.zeros(5)
mask = np.array([True, False, True, False, True])

# NumPy 原地写法
b = np.zeros(5); b[mask] = 7.0

# xpx.at 函数式写法
c = np.zeros(5)
c2 = xpx.at(c)[mask].set(7.0)   # 注意：c 本身未被修改！

print("c  (原数组):", c)          # 仍全 0
print("c2 (返回值):", c2)         # [7. 0. 7. 0. 7.]
print("与原地一致:", np.array_equal(b, c2))

# multiply 等价于 *=
d = np.ones(4); d[1:3] *= -1
e = np.ones(4); e2 = xpx.at(e)[1:3].multiply(-1)
print("multiply 一致:", np.array_equal(d, e2))
```

**需要观察的现象**：

- `c` 原数组保持全零，只有返回值 `c2` 被填入 7——证明 `xpx.at` 不就地修改。
- `b` 与 `c2` 完全相等；`d` 与 `e2` 完全相等——证明语义等价。

**预期结果**：两组“一致”判断都打印 `True`；`c` 全零、`c2` 为 `[7. 0. 7. 0. 7.]`。这印证了 4.2.3 里“为什么每行都要 `x = xpx.at(x)...` 重新接住”。

#### 4.2.5 小练习与答案

**练习 1**：在 `_differentiate.py` 中，`jacobian` 写对角线用的是 `xpx.at(xph)[i, i].set(x)`（花式索引），而 `derivative` 写求值点用的是布尔掩码 `xpx.at(x_eval)[ir].set(...)`。为什么 `jacobian` 不能也用布尔掩码？

**答案**：`jacobian` 要把扰动写到 `m×m` 矩阵的**对角线** `[0,0],[1,1],...,[m-1,m-1]`，这是一个“坐标成对”的花式索引模式，自然用 `xpx.at(xph)[i, i].set(x)`。布尔掩码适合“按条件选元素”，而对角线是按位置选，花式索引更直接。

**练习 2**：如果把 `post_func_eval` 里的 `work.df = xpx.at(work.df)[il].multiply(-1)` 改成 NumPy 风格 `work.df[il] *= -1`，在 NumPy 后端下能跑通吗？在 JAX 后端下呢？

**答案**：NumPy 后端下能跑通（语义等价）。但在 JAX 等不可变后端下，`work.df[il] *= -1` 这种就地写回要么报错、要么无效，因为 JAX 数组不支持 `__setitem__`。这正是 `xpx.at` 存在的意义——它对每个后端选择合法的实现。

---

### 4.3 `@xp_capabilities`：声明后端支持与跳过原因

#### 4.3.1 概念说明

光把代码写成 `xp.*` / `xpx.at` 还不够——有些后端就是**不支持**某些操作（比如 `array_api_strict` 不支持花式索引赋值、`dask.array` 不支持布尔索引赋值）。`derivative` 的实现强依赖 `xpx.at`，所以这两个后端跑不动。

`@xp_capabilities` 装饰器就是用来**集中声明**“这个函数支持哪些后端、跳过哪些后端、为什么跳过”。它的两大用途：

1. **文档**：自动给函数 docstring 追加一张“Array API Standard Support”支持矩阵表（NumPy/CuPy/PyTorch/JAX/Dask × CPU/GPU），告诉用户该怎么测。
2. **测试**：把声明写入一张全局能力表 `xp_capabilities_table`，测试通过 `make_xp_test_case(derivative)` 读这张表，自动生成对应的 `pytest.mark.skip_xp_backends(...)` / `xfail_xp_backends(...)` 标记。

#### 4.3.2 核心流程

`derivative` 头部的装饰器（简化）：

```text
_array_api_strict_skip_reason = 'Array API does not support fancy indexing assignment.'
_dask_reason = 'boolean indexing assignment'

@xp_capabilities(skip_backends=[('array_api_strict', _array_api_strict_skip_reason),
                                ('dask.array', _dask_reason)], jax_jit=False)
def derivative(...): ...
```

装饰器内部做两件事（见 `xp_capabilities` 源码）：

```text
capabilities = dict(skip_backends=..., xfail_backends=..., cpu_only=..., np_only=...,
                    jax_jit=..., allow_dask_compute=..., ...)
xp_capabilities_table[f] = capabilities          # ① 登记到全局表，供测试读取
note = _make_capabilities_note(...)              # ② 生成支持矩阵表
f.__doc__ += note                                 #    注入 docstring
```

测试侧，`make_xp_test_case(derivative)` 读表后，对每个 `skip_backends` 条目生成一个 `pytest.mark.skip_xp_backends(mod_name, reason=reason)` 标记。于是 `tests/test_differentiate.py` 里只需写 `@make_xp_test_case(derivative)`，就能自动在 `array_api_strict` / `dask.array` 后端上跳过相关测试，并带上跳过原因。

#### 4.3.3 源码精读

[scipy/differentiate/_differentiate.py:61-66](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L61-L66) —— 定义两个跳过原因字符串常量，并装饰 `derivative`。两个原因直接揭示了**为什么跳过**：

- `array_api_strict` 跳过原因：`'Array API does not support fancy indexing assignment.'`（标准未规定花式索引赋值）。
- `dask.array` 跳过原因：`'boolean indexing assignment'`（Dask 的惰性计算不支持布尔索引赋值）。

这两个原因恰好对应 4.2 里 `xpx.at` 的两类用法：`jacobian` 的 `xpx.at(xph)[i, i].set(x)` 是**花式索引**赋值（撞 `array_api_strict` 的缺口），`derivative` 的 `xpx.at(h0)[h0 <= 0].set(...)` 等是**布尔索引**赋值（撞 Dask 的缺口）。`jax_jit=False` 表示该函数**不参与 `jax.jit` 编译测试**——推测原因是 `derivative` 含数据相关的迭代终止（`check_termination` 根据每轮误差动态决定哪些元素停机），这种 Python 层 while 循环难以被 jit 静态追踪（待确认：源码未直接注释原因，此为合理推断）。

同样的装饰器也用在 `jacobian` 与 `hessian` 上：

[scipy/differentiate/_differentiate.py:721-722](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L721-L722) —— `jacobian` 的 `@xp_capabilities`，跳过后端与原因完全相同（因为它内部调用 `derivative`，继承同样的 `xpx.at` 限制）。

[scipy/differentiate/_differentiate.py:951-952](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L951-L952) —— `hessian` 同理。

[scipy/_lib/_array_api.py:839-939](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_array_api.py#L839-L939) —— `xp_capabilities` 装饰器定义。注意它返回一个 `decorator(f)`，内部把 `capabilities` 存进 `capabilities_table[f]`（L925），并用 `FunctionDoc` 重写 `f.__doc__`（L926-936）。docstring（L864-880）说明它两大效果：① 给测试生成 SKIP/XFAIL 标记；② 给 docstring 自动加支持矩阵表。

[scipy/_lib/_array_api.py:791-836](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_array_api.py#L791-L836) —— `_make_capabilities_note`：生成注入 docstring 的那段 “Array API Standard Support” 文本，含一张 NumPy/CuPy/PyTorch/JAX/Dask × CPU/GPU 的表格，并提示用户用 `SCIPY_ARRAY_API=1` 提供非 NumPy 数组来测试（L817）。

[scipy/_lib/_array_api.py:1147-1150](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_array_api.py#L1147-L1150) —— `make_xp_pytest_marks` 里把 `skip_backends` / `xfail_backends` 翻译成 `pytest.mark.skip_xp_backends(...)` / `xfail_xp_backends(...)` 标记。这就是“声明 → 测试自动跳过”的接驳点。

[scipy/_lib/_array_api.py:1164](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_array_api.py#L1164) —— `xp_capabilities_table = {}`：全局能力表，所有被 `@xp_capabilities` 装饰的函数都在导入时登记进来。

测试侧的使用入口：

[scipy/differentiate/tests/test_differentiate.py:17](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/tests/test_differentiate.py#L17) —— `@make_xp_test_case(derivative)`：读 `xp_capabilities_table[derivative]`，自动给下面的测试类加上对 `array_api_strict`/`dask.array` 的 `skip_xp_backends` 标记。`jacobian`、`hessian` 在 L486、L640 同样使用。

#### 4.3.4 代码实践

**目标**：核实 `@xp_capabilities` 真的把支持矩阵表注入了 `derivative` 的 docstring，并解释 `array_api_strict` 与 `dask.array` 被跳过的根因。

**操作步骤**：

1. 运行脚本（示例代码）：

   ```python
   from scipy.differentiate import derivative
   doc = derivative.__doc__
   # 截取 Notes 里 Array API 那段
   idx = doc.find("Array API Standard Support")
   print(doc[idx:idx+400])
   ```

2. 在 `_differentiate.py` 里检索 `xpx.at` 的全部用法（参考命令）：

   ```bash
   grep -n "xpx.at" _differentiate.py
   ```

**需要观察的现象**：

- 第 1 步应打印出一张支持矩阵表（含 NumPy/CuPy/PyTorch/JAX/Dask 行），证明装饰器在导入时已改写 docstring。
- 第 2 步会列出约十多处 `xpx.at`，归纳后可发现它们分两类索引：**布尔掩码**（如 `[h0 <= 0]`、`[ir]`、`[ic]`、`[i]`）与**花式/整数索引**（如 `[i, i]`）。

**预期结果 / 解释**：

- `array_api_strict` 被跳过的根因：它不支持**花式索引赋值**（`fancy indexing assignment`）。`jacobian` 的 `xpx.at(xph)[i, i].set(x)` 正是花式索引赋值，标准未要求支持，故跳过。
- `dask.array` 被跳过的根因：它不支持**布尔索引赋值**（`boolean indexing assignment`）。`derivative` 里大量 `xpx.at(...)[布尔掩码].set(...)`（如 `[h0 <= 0]`、`[ir]`、`[i]`）依赖布尔索引赋值，Dask 的惰性图不支持，故跳过。

一句话：**两个后端被跳过，本质都是因为 `xpx.at` 所依赖的索引赋值能力在这两个后端上不可用**——这正是把跳过原因字符串放在装饰器上的价值所在。

#### 4.3.5 小练习与答案

**练习 1**：`jacobian` 和 `hessian` 的 `@xp_capabilities` 与 `derivative` 完全相同（同样跳过 `array_api_strict` 和 `dask.array`）。为什么不需要为它们单独写不同的跳过声明？

**答案**：因为 `jacobian` 内部直接调用 `derivative`（`hessian` 又调用 `jacobian`），它们复用同一套 `xpx.at` 索引赋值机制，限制完全继承自 `derivative`。声明一致是正确的——只要 `derivative` 跑不动的后端，`jacobian`/`hessian` 同样跑不动。

**练习 2**：假设未来 `array_api_strict` 加上了花式索引赋值支持，为了让 `derivative` 支持它，需要改哪些地方？

**答案**：只需把 `derivative`（以及 `jacobian`/`hessian`）装饰器里 `skip_backends` 列表中的 `('array_api_strict', ...)` 条目删掉即可——代码本体已经是后端无关的 `xpx.at` 写法，无需改算法。这也体现了“能力声明与实现解耦”的好处：声明一改，测试自动跟进。

---

## 5. 综合实践

把三个模块串起来，完成规格里给定的主实践任务：

**任务**：在 `_differentiate.py` 中检索所有 `xpx.at` 的用法，归纳它替代了 NumPy 的哪些原地索引操作；再阅读装饰 `derivative` 的 `@xp_capabilities` 的 `skip_backends` 参数，说明为何 `array_api_strict` 与 `dask.array` 被跳过。

**操作步骤**：

1. **检索与归类**：执行 `grep -n "xpx.at" _differentiate.py`，把每一处按“索引类型 × 方法”填入下表（示例答案）：

   | 位置（行） | 索引类型 | `.方法` | 等价的 NumPy 原地写法 |
   |---|---|---|---|
   | L417 | 布尔掩码 `h0<=0` | `.set(nan)` | `h0[h0<=0] = nan` |
   | L490-492 | 布尔掩码 `ir/ic/il` | `.set(...)` | `x_eval[ir] = ...` |
   | L542-543 | 布尔掩码 `ic/io` | `.set(...)` | `work.fs[ic] = ...` |
   | L547-548 | 布尔掩码 `ic/io` | `.set(...)` | `work.df[ic] = ...` |
   | L549 | 布尔掩码 `il` | `.multiply(-1)` | `work.df[il] *= -1` |
   | L567-585 | 布尔掩码 `i` | `.set(...)` | `work.status[i] = ...` |
   | L939 | 花式索引 `[i,i]` | `.set(x)` | `xph[i,i] = x` |

2. **归纳**：`xpx.at` 替代的 NumPy 原地操作主要是两类——`a[布尔或花式索引] = 值`（`.set`）与 `a[索引] op= 值`（`.multiply`/`.add` 等）。它把这些“可变”操作翻译成“不可变、返回新数组”的形式，从而兼容 JAX 等后端。

3. **解释跳过原因**：读 [L61-66](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L61-L66) 的两个常量：
   - `array_api_strict`：`'Array API does not support fancy indexing assignment.'`——对应 L939 的花式索引赋值 `[i,i]`。
   - `dask.array`：`'boolean indexing assignment'`——对应 L417/L490-492/L542-549/L567-585 的大量布尔索引赋值。

4. **闭环验证**：因为 `derivative` 强依赖 `xpx.at`，而 `xpx.at` 在这两个后端上分别缺花式/布尔索引赋值能力，所以必须在 `@xp_capabilities` 里声明跳过，让 `make_xp_test_case` 自动生成 `skip_xp_backends` 标记。

**预期结果**：你应能用一句话讲清——“跳过的不是算法，而是 `xpx.at` 这类索引赋值在对应后端的缺失；声明写在装饰器上，测试与文档都从这一处自动派生。”

> 若本地装有 `array_api_strict` 或 `dask.array`，可尝试 `SCIPY_ARRAY_API=1` 下对它们调用 `derivative(np.exp, ...)`，预期会因索引赋值不支持而失败——这正反证了跳过声明的必要性（待本地验证）。

## 6. 本讲小结

- **后端抽象的第一步是 `xp = array_namespace(x)`**：默认（`SCIPY_ARRAY_API` 未设）走 NumPy 快路径、零开销；开关开启后才分发到 torch/jax/cupy/dask。`derivative`/`jacobian`/`hessian` 内部一律用 `xp.*`，不再出现裸 `np.`（标量校验除外）。
- **`xp_promote` / `xp_copy` 是两个高频跨后端小工具**：前者统一 dtype 并可强制浮点（`jacobian`/`hessian` 开头的 `force_floating=True` 必不可少）、可选广播；后者屏蔽各后端拷贝语义差异，且在 `jacobian` 里把 `broadcast_to` 的只读视图复制成可写矩阵。
- **`xpx.at(...)[idx].set/multiply(...)` 是 NumPy 原地索引赋值的函数式替代**：返回新数组、不改原数组，因此兼容 JAX 等不可变后端；索引既可是布尔掩码（`derivative` 主流程）也可是花式索引（`jacobian` 对角线）。
- **`@xp_capabilities` 把“能力声明”集中化**：同时驱动两件事——给 docstring 注入支持矩阵表、给 `xp_capabilities_table` 登记供测试读取，由 `make_xp_test_case` 自动生成 `skip_xp_backends` 标记。
- **`array_api_strict` 与 `dask.array` 被跳过的根因都是 `xpx.at` 依赖的索引赋值**：前者缺花式索引赋值、后者缺布尔索引赋值。跳过原因直接写在装饰器常量里，一查便知。

## 7. 下一步学习建议

- **横向对照同框架的其它函数**：`scipy._lib._elementwise_iterative_method` 还支撑着 `scipy.optimize._chandrupatla`（`bracket_root`/`find_root`）、`scipy.integrate.tanhsinh` 等。它们都用同样的 `pre_func_eval`/`post_func_eval`/`check_termination` 钩子 + `xp.*`/`xpx.at` 写法，读其中一个就能巩固本讲的跨后端模式。
- **深入 `array_api_extra`**：本讲只用了 `xpx.at`。`scipy._external.array_api_extra` 还提供 `xpx.apply_along_axis`、`xpx.at` 的更多方法、以及测试用的 `lazy_xp_function`（即 `jax_jit`/`allow_dask_compute` 的实现处）。阅读它的源码能让你彻底理解 `jax_jit=False` 的确切含义。
- **阅读测试侧**：`scipy/differentiate/tests/test_differentiate.py` 顶部 `@make_xp_test_case(derivative)`（[L17](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/tests/test_differentiate.py#L17)）与 `conftest.py` 里的 `xp` fixture，是把“能力声明”落到“跨后端参数化测试”的完整链路，建议结合 u4-l5（测试体系）一起读。
- **试着加一个后端声明**：找一个你熟悉的后端限制场景，给某个函数的 `@xp_capabilities` 加一条 `xfail_backends`，观察 docstring 表格与测试标记的变化——这是检验你是否真正理解本讲的最佳方式。
