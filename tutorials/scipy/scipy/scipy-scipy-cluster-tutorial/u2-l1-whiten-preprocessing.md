# whiten：按特征白化与零方差处理

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚**为什么 k-means 之前通常要先白化**（whiten），以及「白化」这个名字的来历。
- 逐行读懂 `scipy.cluster.vq.whiten` 的实现：按列求标准差、按列相除、对零方差列做兜底处理并发 `RuntimeWarning`。
- 理解 `whiten` 内部用到的三个 array API 抽象：`array_namespace(xp)`、`_asarray`、以及 `xpx.at(...).set(...)`，并明白它们为什么能让同一份代码同时跑在 NumPy、JAX、Dask 等后端上。
- 自己用纯 NumPy 复现一个与官方 `whiten` 行为（含零方差告警）完全一致的函数。

本讲只聚焦 `whiten` 这一个函数及其依赖的少量 array API 工具，不涉及 `kmeans`/`vq` 的迭代逻辑（那是后续讲义的内容）。

## 2. 前置知识

在进入源码前，先用大白话把三个概念讲清楚。

### 2.1 特征、观测与「尺度不一致」问题

在 `scipy.cluster.vq` 的约定里，输入是一张 **M×N 的观测矩阵 `obs`**：每一行是一个观测（一个样本），每一列是一个特征（一个维度）。这一点在 [vq 包文档](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/__init__.py#L51-L54) 里写得很明确：「All routines expect obs to be an M by N array」。

k-means 的核心运算是**欧氏距离**。欧氏距离对每一维是「平方后相加再开方」，这意味着**数值范围（尺度）大的特征会主导距离的计算**。举个例子：假如特征 0 是身高（米，量级 1.7），特征 1 是工资（元，量级 10000），那么两个人在欧氏距离上几乎完全由工资差决定，身高差被彻底淹没。这不是聚类算法想要的——我们通常希望每个特征被「公平对待」。

### 2.2 白化（whitening）：让每个特征单位方差

解决尺度不一致的常见办法是**白化**：把每个特征列除以它自己的标准差，让每一列都变成「单位方差」。这个名字借自信号处理里的「白噪声」——白噪声在每个频率上功率相等，白化后的数据在每个特征上方差相等。`whiten` 的 docstring 也是这么解释的（见下方源码精读）。

### 2.3 零方差列：一个必须兜底的边界

如果某一列的所有观测都相同（比如全是 5.0），那么这一列的标准差是 0。直接「除以标准差」会得到 `x / 0`，产生 `NaN` 或 `inf`，后续 k-means 就会崩掉。`whiten` 对此做了专门处理：**把零方差列的除数强行改成 1**（这样该列的值保持不变），同时发出一条 `RuntimeWarning` 提醒用户。这是本讲的一个重点。

> 术语速查：**xp** = array namespace（数组后端的命名空间，比如 `numpy`、`jax.numpy`）；**lazy array** = 惰性数组（如 Dask，先建计算图、不立即求值）；**check_finite** = 是否检查输入里有没有 NaN/Inf。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [vq/_vq_impl.py](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq_impl.py#L24-L83) | `whiten` 的真正实现（连同 `vq`/`kmeans`/`kmeans2`），是本讲的主角。 |
| [vq/__init__.py](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/__init__.py#L74-L77) | 仅做重新导出：`from ._vq_impl import ... whiten`，对外暴露公共 API。 |
| [vq/tests/test_vq.py](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/tests/test_vq.py#L81-L109) | `TestWhiten` 测试组，含零方差列的断言，是我们理解边界行为的最佳依据。 |
| scipy/_lib/_array_api_override.py | `array_namespace` 的定义，决定 `xp` 到底是哪个后端。 |
| scipy/_lib/_array_api.py | `_asarray` 的定义，是 SciPy 版的 `np.asarray`，附带 `check_finite`。 |
| scipy/_external/array_api_extra (as xpx) | 第三方库 array_api_extra，提供 `xpx.at(...).set(...)` 这种跨后端的「函数式索引赋值」。 |

> 说明：本讲的永久链接 base 指向 `scipy/cluster/`，故 `vq/` 下的文件用相对路径；`array_namespace`、`_asarray`、`xpx` 的**定义**位于 `scipy/_lib/` 与外部库中，链接用 `../` 跨出 cluster 目录（GitHub 会自动规范化）。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 `whiten`**：白化的数学动机 + 逐行实现 + 零方差兜底（主角）。
- **4.2 `array_namespace`**：如何用一个 `xp` 变量同时适配多个数组后端。
- **4.3 `xpx.at(...).set(...)`**：如何对「不可变 / 惰性」数组做「按掩码赋值」。

---

### 4.1 whiten：按列除以标准差的白化

#### 4.1.1 概念说明

白化的目标用一句话讲：**让 `obs` 的每一列方差变为 1**，从而消除特征间尺度差异，使后续欧氏距离公平对待每个特征。

设观测矩阵为 \(X\)，其第 \(j\) 列为 \(X_{:,j}\)，该列的标准差为 \(\sigma_j\)。白化定义为：

\[
W_{i,j} = \frac{X_{i,j}}{\sigma_j}, \qquad \sigma_j = \mathrm{std}(X_{:,j})
\]

验证「单位方差」：白化后第 \(j\) 列的方差为

\[
\mathrm{Var}(W_{:,j}) = \mathrm{Var}\!\left(\frac{X_{:,j}}{\sigma_j}\right) = \frac{\mathrm{Var}(X_{:,j})}{\sigma_j^2} = \frac{\sigma_j^2}{\sigma_j^2} = 1
\]

这正是 docstring 里「Each feature is divided by its standard deviation across all observations to give it unit variance」的含义。

> 为什么和 k-means 相关？k-means 的目标是让每个样本离其所属簇心尽量近，度量就是欧氏距离。若各列方差一致，距离就不会被某个大量纲列「绑架」。`vq` 的 docstring 也明确写道：「The features in `obs` should have unit variance, which can be achieved by passing them through the whiten function.」

#### 4.1.2 核心流程

`whiten` 的执行流程可以用下面这段伪代码概括：

```
xp      = 探测输入 obs 的后端命名空间(numpy / jax / dask...)
obs     = _asarray(obs, check_finite=...)      # 规整成数组,必要时查 NaN/Inf
std_dev = xp.std(obs, axis=0)                   # 每列一个标准差,长度 N
mask    = (std_dev == 0)                        # 哪些列是零方差
std_dev[mask] = 1.0                             # 兜底:零方差列除数置 1(用 xpx.at 实现)
if check_finite and mask.any():
    warn(RuntimeWarning, "这些列标准差为 0,值不会改变")
return obs / std_dev                            # 逐元素按列相除
```

三个关键点：

1. **`axis=0`**：沿着「行」方向求标准差，也就是对每一**列**（每一特征）各算一个值，结果长度等于列数 N。
2. **零方差兜底**：先把 `std_dev` 里等于 0 的位置改成 1.0，这样 `obs / std_dev` 在那些列上等价于「除以 1」，原值不变，避免 `x/0`。
3. **告警条件**：只有在 `check_finite=True` 且确实存在零方差列时才告警；惰性数组（Dask）默认不告警（见 4.2）。

#### 4.1.3 源码精读

先看导入和 `__all__`，确认 `whiten` 是从哪个模块来的、用了哪些工具：

[vq/_vq_impl.py:L1-L14](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq_impl.py#L1-L14) —— 导入 `array_namespace`/`is_lazy_array`/`_asarray`/`xp_capabilities` 等 array API 工具，并把 `array_api_extra` 取别名 `xpx`；`from . import _vq` 拉进 Cython 编译后端（本讲用不到，但 `vq`/`kmeans` 会用）。

接着是 `whiten` 的装饰器与签名：

[vq/_vq_impl.py:L24-L25](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq_impl.py#L24-L25) —— `@xp_capabilities()` 装饰器（无参）声明本函数对各个数组后端的支持情况，并自动往 docstring 里追加一张能力表；`def whiten(obs, check_finite=None)` 注意 `check_finite` 默认是 `None` 而非 `True`，这是有意为之（见下）。

核心实现只有 12 行，集中在：

[vq/_vq_impl.py:L72-L83](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq_impl.py#L72-L83) —— 逐行含义：

- **L72** `xp = array_namespace(obs)`：探测 `obs` 属于哪个后端，拿到命名空间 `xp`（比如 `numpy`）。后续所有数组运算都通过 `xp.xxx(...)` 调用，保证跨后端通用。
- **L73–L74** `if check_finite is None: check_finite = not is_lazy_array(obs)`：默认行为——**普通（eager）数组要查 NaN/Inf，惰性（lazy，如 Dask）数组不查**（因为查 finite 会强制触发整个惰性图求值，违背惰性初衷）。
- **L75** `obs = _asarray(obs, check_finite=check_finite, xp=xp)`：规整成数组；若 `check_finite=True` 则顺带抛出 NaN/Inf 错误。
- **L76** `std_dev = xp.std(obs, axis=0)`：每列标准差，长度为 N。
- **L77** `zero_std_mask = std_dev == 0`：布尔掩码，标记零方差列。
- **L78** `std_dev = xpx.at(std_dev, zero_std_mask).set(1.0)`：把掩码为 True 的位置设成 1.0（等价于 `std_dev[zero_std_mask] = 1.0`，但用函数式写法兼容惰性/不可变后端，详见 4.3）。
- **L79–L82** `if check_finite and xp.any(zero_std_mask): warnings.warn(...)`：存在零方差列且开启了 finite 检查时，发 `RuntimeWarning`，提示「这些列的值不会改变」。`stacklevel=2` 让警告指向**调用 `whiten` 的用户代码**而非 `whiten` 内部。
- **L83** `return obs / std_dev`：逐元素按列相除，得到白化结果。

docstring 里的示例输出可以直接用来验证手写实现：

[vq/_vq_impl.py:L59-L69](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq_impl.py#L59-L69) —— 给定 `features`，`whiten(features)` 返回按列除以标准差后的矩阵。

#### 4.1.4 代码实践

**实践目标**：用纯 NumPy 手写一个 `my_whiten`，复现官方 `whiten` 的「按列除以标准差」与「零方差列除数置 1 并发 `RuntimeWarning`」两条行为，再逐元素比对。

**操作步骤**（新建一个 `practice_whiten.py` 运行）：

```python
# 示例代码：手写与官方等价的 whiten
import warnings
import numpy as np
from scipy.cluster.vq import whiten

def my_whiten(obs):
    obs = np.asarray(obs, dtype=float)
    std_dev = obs.std(axis=0)               # 每列标准差
    zero_mask = (std_dev == 0)              # 零方差列掩码
    std_dev[zero_mask] = 1.0                # 兜底:除数置 1
    if zero_mask.any():                     # 复现告警
        warnings.warn("Some columns have standard deviation zero. "
                      "The values of these columns will not change.",
                      RuntimeWarning, stacklevel=2)
    return obs / std_dev

# 1) 正常情况:逐元素比对
obs_normal = np.array([[1.9, 2.3, 1.7],
                       [1.5, 2.5, 2.2],
                       [0.8, 0.6, 1.7]])
a = whiten(obs_normal)
b = my_whiten(obs_normal)
print("max abs diff (normal):", np.abs(a - b).max())

# 2) 零方差情况:第二列全为 1.0,标准差为 0
obs_zero = np.array([[1.9, 1.0, 0.74],
                     [1.5, 1.0, 0.34],
                     [0.8, 1.0, 0.97]])
with warnings.catch_warnings(record=True) as w:
    warnings.simplefilter("always")
    a2 = whiten(obs_zero)
print("官方 whiten 告警:", [str(x.message) for x in w])
print("零方差列结果是否不变:", np.allclose(a2[:, 1], obs_zero[:, 1]))
```

**需要观察的现象**：

1. 第 1 部分的 `max abs diff` 应为 `0.0`（或极小浮点误差），证明正常情况两实现一致。
2. 第 2 部分应捕获到一条 `RuntimeWarning`，文案以「Some columns have standard deviation zero」开头。
3. 零方差列（第 2 列）的白化结果应与原值完全一致（`np.allclose` 为 `True`），证明「除数置 1」确实让该列不变。

**预期结果**：`max abs diff (normal): 0.0`；官方 `whiten` 对零方差列发出 `RuntimeWarning`；零方差列原样保留。

> 若你本机 SciPy 版本与讲义 HEAD 不同，告警文案或浮点尾数可能略有差异，但行为一致即可。如未实际运行，请标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：若把 L78 的兜底删掉（即零方差列仍除以 0），对 `obs_zero` 调用 `whiten` 会得到什么？为什么这不可接受？

> **答案**：零方差列会变成 `NaN`（`x / 0.0` 对非零 x 为 `inf`，`0.0/0.0` 为 `nan`），后续 `kmeans` 的距离计算会全部污染成 `NaN`，聚类失败。兜底置 1 正是为了避免这种情况。

**练习 2**：`whiten` 是按「行」还是按「列」归一化？如果把一个 M×N 矩阵转置后再 `whiten`，结果会一样吗？

> **答案**：按**列**（`axis=0`）。转置后行变列，于是归一化的对象变了，结果一般不同。这正是为什么 `vq` 全程要求「行=观测、列=特征」的约定不能搞反。

**练习 3**：白化后每一列的方差是多少？为什么？

> **答案**：约为 1。因为 \(\mathrm{Var}(X_{:,j}/\sigma_j) = \mathrm{Var}(X_{:,j})/\sigma_j^2 = 1\)。注意 `np.std` 默认用「除以 N」的总体标准差（ddof=0），所以白化后列方差严格为 1；若你用样本标准差（ddof=1）会有微小偏差。

---

### 4.2 array_namespace：识别数组后端命名空间

#### 4.2.1 概念说明

`whiten` 里反复出现的 `xp`，是「数组后端命名空间」（array namespace）。NumPy 的命名空间是 `numpy`（也就是 `np`），JAX 的是 `jax.numpy`，Dask 的是 `dask.array`，PyTorch 的是 `torch`……它们都遵循同一套 [Python Array API 标准](https://data-apis.org/array-api/)，所以只要把后端命名空间存进 `xp` 这个变量，再用 `xp.std(...)`、`xp.any(...)` 调用，同一份代码就能跑在多个后端上。

`array_namespace(obs)` 的作用就是：**看一眼 `obs` 是哪种数组，返回它对应的 `xp`**。这是 SciPy 全库支持多后端的基础设施。

#### 4.2.2 核心流程

```
array_namespace(obs):
    if 全局开关 SCIPY_ARRAY_API 未开:
        return numpy        # 默认只走 NumPy,跳过所有合规检查(快路径)
    逐个检查输入数组的类:
        拒绝 MaskedArray / np.matrix / 非数值 dtype
        NumPy 数组与普通 list -> 归到 numpy 桶
        其它 Array API 数组(jax/dask/...) -> 归到 api 桶
    if 只有 numpy 桶:  return numpy
    else:              return array_api_compat.array_namespace(...)   # 推断公共后端
```

要点：

- 默认情况下（未设 `SCIPY_ARRAY_API=1` 环境变量）**直接返回 `numpy`**，跳过检查，保证「不用多后端就没有额外开销」。
- 一旦开启，它会校验数组类是否被支持，并最终返回一个公共命名空间。

#### 4.2.3 源码精读

[array_namespace:L73-L156](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/../_lib/_array_api_override.py#L73-L156) —— 定义在 `scipy/_lib/_array_api_override.py`（注意它跨出了 cluster 目录，所以链接里有 `../`）。关键两段：

- 开头的 docstring（L74–L110）说明它是 `array_api_compat.array_namespace` 的包装，并列出三步：查全局开关、拒绝坏类、推断命名空间。
- **快路径**（L111–L113）：`if not SCIPY_ARRAY_API: return np_compat`——绝大多数用户走这里，`xp` 就是 `numpy`。

另一个被 `whiten` 用到的 `_asarray` 也基于同样的思路：

[_asarray:L76-L120](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/../_lib/_array_api.py#L76-L120) —— SciPy 版的 `np.asarray`：对 NumPy 输入走 `np.asarray`（支持 `order`），对其它后端走 `xp.asarray`，并在 `check_finite=True` 时调用 `_check_finite` 抛 NaN/Inf 错误（L117–L118）。`whiten` 在 L75 正是用它把 `obs` 规整成数组并顺带做 finite 检查。

回到 `whiten` 自身对 `xp` 的使用：

[vq/_vq_impl.py:L72-L83](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq_impl.py#L72-L83) —— `xp.std`、`xp.any` 都通过命名空间调用，这正是 `whiten` 能跨后端运行的原因。

#### 4.2.4 代码实践

**实践目标**：直观感受 `xp` 在不同后端下是什么，并验证「未开 `SCIPY_ARRAY_API` 时 `xp` 就是 `numpy`」。

**操作步骤**（源码阅读型 + 可选运行）：

1. 在 `whiten` 的 L72 之后临时加一行调试（仅本地调试，不要提交）：
   ```python
   xp = array_namespace(obs)
   print("xp =", xp.__name__)     # 临时调试
   ```
2. 用普通 NumPy 数组调用 `whiten`，观察打印的 `xp`。
3. （进阶）设置环境变量 `SCIPY_ARRAY_API=1` 后重启 Python，再分别传入 `np.array` 与 `dask.array`（若装了 dask），观察 `xp` 变化。

**需要观察的现象**：

- 步骤 2 中，`xp` 应为 `numpy`。
- 步骤 3 中，传 dask 数组时 `xp` 应变成 dask 相关命名空间，且 `is_lazy_array(obs)` 为 `True`，于是 `check_finite` 默认变 `False`。

**预期结果**：默认 `xp = numpy`；开启全局开关并传入惰性数组后，`xp` 切换到对应后端，`check_finite` 默认关闭。如未安装 dask/jax，相关步骤标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `whiten` 要写 `xp.std(obs, axis=0)` 而不是直接 `np.std(obs, axis=0)`？

> **答案**：直接用 `np.std` 会把实现绑死在 NumPy 上，传 JAX/Dask 数组时要么报错要么悄悄转成 NumPy（破坏惰性 / 脱离 GPU）。通过 `xp` 调用则使用输入数组自身后端的 `std`，保持后端一致。

**练习 2**：`check_finite` 的默认值为什么设计成「惰性数组不查」？

> **答案**：检查 finite 需要遍历整个数组的值，对 Dask 这类惰性数组意味着立即触发整张计算图的求值（`.compute()`），抵消了惰性的好处。所以默认对惰性数组跳过该检查。

---

### 4.3 xpx.at(...).set(...)：跨后端的函数式索引赋值

#### 4.3.1 概念说明

在 NumPy 里，把数组里满足某条件的元素改成新值，最自然的写法是「索引赋值」：

```python
std_dev[zero_std_mask] = 1.0      # NumPy 原地修改
```

但这在 Array API 多后端世界里有麻烦：

- **JAX 数组是不可变的**，`arr[mask] = v` 这种原地赋值直接不被允许。
- **Dask 数组是惰性的**，原地修改会破坏计算图。

array_api_extra 库（在 SciPy 里以 `xpx` 为别名导入，见 [vq/_vq_impl.py:L9](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq_impl.py#L9)）提供了统一的「函数式索引赋值」`xpx.at`：它**不原地改数组，而是返回一个新数组**，语义上等价于索引赋值，但对所有后端都成立。可以把它理解成「跨后端、不可变版本的 `arr[mask] = value`」。

#### 4.3.2 核心流程

```
new = xpx.at(arr, mask).set(value)
# 语义等价于:
#     new = arr.copy()
#     new[mask] = value
# 但对 JAX/Dask 也成立(返回新数组,不改原数组)
```

`xpx.at` 还支持切片 / 多维索引的链式写法，例如本仓库里其它用法：

```python
xpx.at(init)[i, :].set(data[data_idx, :])     # 二维切片赋值
xpx.at(res)[:, :2].set(Z[:, :2] - 1.0)        # 多列切片赋值
```

#### 4.3.3 源码精读

`whiten` 里唯一的 `xpx.at` 用法就是零方差兜底那一行：

[vq/_vq_impl.py:L78](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq_impl.py#L78) —— `std_dev = xpx.at(std_dev, zero_std_mask).set(1.0)`：把零方差位置的标准差改成 1.0，结果重新绑定给 `std_dev`（注意是赋值回同名变量，因为 `xpx.at` 返回的是新数组）。

为了说明 `xpx.at` 是全仓库通用模式，再看两处典型用法（本讲只需了解）：

[vq/_vq_impl.py:L567](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq_impl.py#L567) —— `_kpp`（k-means++ 初始化）里用 `xpx.at(init)[i, :].set(...)` 写入第 i 行初始簇心。

[hierarchy/_hierarchy_impl.py:L1791-L1792](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L1791-L1792) —— 层次聚类里用 `xpx.at(res)[:, :2].set(...)` 构造 linkage matrix 的列。

> 说明：`xpx.at` 的**定义**在第三方库 `array_api_extra`（vendored 为 `scipy._external.array_api_extra`）中，不在本仓库 cluster 目录下，故此处只链接它的「使用点」；其语义以官方 array_api_extra 文档为准。

#### 4.3.4 代码实践

**实践目标**：体会「原地赋值」与「函数式赋值」在 NumPy 下结果相同，但函数式写法返回的是新数组。

**操作步骤**（示例代码）：

```python
# 示例代码:对比 numpy 原地赋值与 xpx.at 函数式赋值
import numpy as np
from scipy._external import array_api_extra as xpx

std = np.array([0.0, 2.0, 0.0, 5.0])
mask = (std == 0)

# (A) numpy 原地写法
a = std.copy()
a[mask] = 1.0

# (B) xpx.at 函数式写法
b = xpx.at(std, mask).set(1.0)

print("A == B :", np.array_equal(a, b))
print("原 std 未被修改:", np.array_equal(std, [0., 2., 0., 5.]))   # xpx.at 不改原数组
print("结果:", b)
```

**需要观察的现象**：

1. `A == B` 为 `True`，两种写法结果一致。
2. 原数组 `std` 在函数式写法后**未被修改**（仍是 `[0,2,0,5]`），证明 `xpx.at` 返回新数组而非原地改。

**预期结果**：`A == B : True`；原 `std` 未变；`b = [1,2,1,5]`。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `whiten` 的 L78 改写成 `std_dev[zero_std_mask] = 1.0`，在 NumPy 下能正常工作吗？为什么 SciPy 仍然选择 `xpx.at`？

> **答案**：在 NumPy 下能正常工作。但 SciPy 选择 `xpx.at` 是为了让 `whiten` 也能跑在 JAX（数组不可变，禁止原地赋值）和 Dask（惰性，原地赋值破坏计算图）上。`xpx.at` 是「一份代码适配所有后端」的关键。

**练习 2**：`xpx.at(std_dev, zero_std_mask).set(1.0)` 返回后为什么要重新赋值给 `std_dev`（即 `std_dev = ...`）？

> **答案**：因为 `xpx.at` 是函数式接口——它**返回一个新数组**，不修改原数组。若不重新赋值，原 `std_dev` 不变，后续 `obs / std_dev` 仍会除以 0。

---

## 5. 综合实践

把本讲三个最小模块串起来，完成下面这个**端到端的小任务**：

> 用 `whiten` 把一组「尺度严重不一致」的数据白化，验证白化后每列方差为 1；再证明「不白化直接做一次简单的最近簇心分配」会被大量纲列主导，而白化后不会。

建议步骤：

1. 构造一个 6×2 的数据集，两列尺度差 1000 倍：
   ```python
   import numpy as np
   rng = np.random.default_rng(0)
   obs = np.column_stack([rng.normal(1.7, 0.1, size=6),     # 身高(米)
                          rng.normal(50000, 8000, size=6)]) # 工资(元)
   ```
2. 调用 `scipy.cluster.vq.whiten(obs)` 得到 `w`，并用 `w.std(axis=0)` 验证两列标准差都≈1。
3. 取两个明显不同的簇心（比如 `w[0]` 与 `w[3]`），手算每个点到这两个簇心的欧氏距离，观察白化后两维对距离的贡献是否「旗鼓相当」。
4. 对比未白化的 `obs` 做同样计算，观察工资列如何主导距离。
5. （可选）构造一个含零方差列的版本（把第 2 列全设成同一个常数），调用 `whiten`，确认它发 `RuntimeWarning` 且该列原值不变。

**预期结果**：白化后两列标准差都约为 1；未白化时工资列几乎独自决定最近簇心归属，白化后两维权重均衡。零方差列版本触发告警且值不变。若第 3、4 步的数值比较未实际运行，请标注「待本地验证」。

## 6. 本讲小结

- `whiten` 的本质是**按列除以标准差**，让每个特征达到单位方差，消除尺度差异对欧氏距离的干扰；这正是 k-means 前要白化的原因。
- 零方差列会被 `xpx.at(std_dev, mask).set(1.0)` 把除数兜底为 1，使该列原值不变，并在开启 finite 检查时发 `RuntimeWarning`。
- `array_namespace(obs)` 返回后端命名空间 `xp`，未开 `SCIPY_ARRAY_API` 时默认就是 `numpy`；用 `xp.std`/`xp.any` 调用让 `whiten` 跨 NumPy/JAX/Dask 通用。
- `check_finite` 默认对惰性数组关闭，避免强制求值破坏惰性。
- `_asarray` 是 SciPy 版 `np.asarray`，把规整数组与 finite 检查合二为一。
- `xpx.at(...).set(...)` 是 array_api_extra 提供的「跨后端、不可变版索引赋值」，是同一份代码兼容 JAX/Dask 的关键。

## 7. 下一步学习建议

本讲只解决了 `whiten` 这一步预处理。沿着 vq 主线继续：

- **下一篇 u2-l2（vq 编码函数与 Cython 后端 _vq.vq）**：白化后的数据如何被分配到最近的码字（簇心），以及浮点输入如何走编译后端 `_vq.vq`、非浮点如何回退 `_py_vq`。
- **u2-l3（kmeans 主流程）**：把 `whiten → vq → 更新簇心` 串成完整的 k-means 迭代。
- 若想深入 array API 基础设施，可先读 `scipy/_lib/_array_api.py` 里 `xp_capabilities` 装饰器（u7-l3 会专题讲解），理解 `@xp_capabilities()` 这个装饰器到底做了什么。
