# Array API 数组后端覆盖（CuPy/Torch/JAX/Dask）

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 Python Array API 标准是什么，以及 SciPy 为什么需要它。
- 解释「数组类型进、数组类型出」（array type in equals array type out）这条核心设计原则。
- 读懂 `array_namespace` 如何从一段输入数组推断出对应的命名空间 `xp`，以及 `SCIPY_ARRAY_API` 开关在其中的作用。
- 掌握 `_asarray` 为什么是「SciPy 版的 `np.asarray`」，它补上了标准里没有的 `order` / `check_finite` / `subok`。
- 区分本讲的 Array API 分发与上一讲（u2-l2）的 uarray 分发，知道哪些函数能被后端拦截、哪些不能。
- 能够设置 `SCIPY_ARRAY_API=1`，用 `array-api-strict` 数组调用一个 `scipy.stats` 函数，并验证输入输出数组类型一致。

## 2. 前置知识

在进入源码前，先用通俗语言建立三个直觉。

**① 什么是「命名空间（namespace）」？** 在 NumPy 里，你写 `np.sum(x)`、`np.asarray(x)`，这里的 `np` 就是命名空间——一个装着一堆数组操作函数的模块。CuPy 的命名空间叫 `cupy`、PyTorch 的是 `torch`、JAX 的是 `jax.numpy`、Dask 的是 `dask.array`。问题在于：同样叫 `sum`，不同库的函数名、参数名并不完全一致（例如旧版 NumPy 用 `np.concatenate`，而标准用 `concat`）。

**② 什么是 Python Array API 标准？** 它是一份跨库约定（规范见 <https://data-apis.org/array-api/latest/index.html>），规定了一个「标准命名空间」应当提供哪些函数、哪些参数名。只要一个库实现了这套标准，你就可以用同一套 `xp.sum`、`xp.asarray`、`xp.concat` 代码，在 NumPy / CuPy / PyTorch / JAX / Dask 之间无缝切换。在这份标准里，标准命名空间习惯记作 `xp`。

**③ 为什么 SciPy 要管这件事？** 传统上 SciPy 的函数内部写死了 `np.xxx`，所以传入一个 PyTorch 张量，SciPy 也只会按 NumPy 处理（或干脆报错）。Array API 标准让 SciPy 可以做到：你传一个 PyTorch 张量进来，我就在内部用 PyTorch 的命名空间算，最后还你一个 PyTorch 张量。这就是「数组类型进、数组类型出」。本讲承接 u2-l1 中提到的 `array_namespace`，并和 u2-l2 的 uarray 形成「两种分发范式」的对照。

## 3. 本讲源码地图

本讲聚焦两个核心文件，辅以若干佐证文件：

| 文件 | 作用 |
| --- | --- |
| [scipy/_lib/_array_api_override.py](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_array_api_override.py) | 定义 `SCIPY_ARRAY_API` / `SCIPY_DEVICE` 环境变量、`_validate_array_cls` 与 `array_namespace`——分发的真正入口。 |
| [scipy/_lib/_array_api.py](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_array_api.py) | 在 `array_namespace` 之上构建的 `xp_*` 辅助函数家族（`_asarray`、`xp_vector_norm`、`xp_assert_close`、`xp_capabilities` 等）。 |
| [scipy/_external/_array_api_compat_vendor.py](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_external/_array_api_compat_vendor.py) | 把 SciPy 自己的 `array_namespace` 注入上游 vendored 包的「覆盖钩子」。 |
| [scipy/stats/_quantile.py](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/stats/_quantile.py) | 一个真实使用 `array_namespace` / `xp_promote` 的统计函数，作为「实战范例」。 |
| [scipy/conftest.py](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/conftest.py) | 测试侧的 `xp` fixture：如何枚举各后端跑同一份测试。 |
| [doc/source/dev/api-dev/array_api.rst](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/doc/source/dev/api-dev/array_api.rst) | 官方开发文档，给出设计原则与「如何给一个函数加支持」的标准范式。 |

> 关于上游依赖的位置：`array-api-compat` 与 `array-api-extra` 是两个外部项目，以 git submodule 形式放在 `subprojects/array_api_compat` 与 `subprojects/array_api_extra`，构建时被呈现为可导入的 `scipy._external.array_api_compat` / `scipy._external.array_api_extra`，不引入新的运行期依赖。官方文档（见上表最后一行）仍把它描述为「位于 `scipy/_lib` 下」，这一表述已略滞后于当前目录结构。

---

## 4. 核心概念与源码讲解

### 4.1 设计原则与 `SCIPY_ARRAY_API` 总开关

#### 4.1.1 概念说明

SciPy 对 Array API 的支持遵循一份 RFC，其最高原则只有一句话：

> *array type in equals array type out*（数组类型进，数组类型出）。

也就是说，函数不该悄悄改变数组的「身份」。你传 CuPy 进去，就该拿到 CuPy 出来；不能在中间偷偷转成 NumPy 再返回——那样会让 GPU 计算退化为 CPU 计算，是隐蔽且严重的性能陷阱。

同时，启用 Array API 支持还附带「更严格的输入校验」：会拒绝 `np.matrix`、`np.ma.MaskedArray` 以及 `object` dtype 的数组。这套更严格的行为会破坏向后兼容（有人可能确实在传 `np.matrix`），因此 SciPy 把它藏在一个环境变量后面，作为「逐步迁移、合入主干」的过渡手段。

#### 4.1.2 核心流程

整体启用流程可以画成下面这样：

```
用户设置 SCIPY_ARRAY_API=1  →  导入 scipy
        │
        ▼
array_namespace(输入数组) 读取 SCIPY_ARRAY_API
        │
        ├─ 未设置（默认）：直接返回 np_compat（兼容版 NumPy 命名空间），跳过一切校验
        │                   —— 这就是「不开开关时行为完全不变」的关键
        │
        └─ 已设置：逐个检查输入数组类型
                   ├─ 已知坏类型（matrix/MaskedArray）→ 报错
                   ├─ 纯 NumPy + Python 标量/列表     → 返回 np_compat
                   └─ 含非 NumPy 的 Array API 数组     → 调 array_api_compat.array_namespace 得到对应 xp
```

#### 4.1.3 源码精读

总开关的定义只有两行，位于 `_array_api_override.py`：

[scipy/_lib/_array_api_override.py:L26-L29](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_array_api_override.py#L26-L29) —— 这两行从环境变量读取 `SCIPY_ARRAY_API`（默认 `False`）与 `SCIPY_DEVICE`（默认 `"cpu"`，仅测试套件用）。

```python
SCIPY_ARRAY_API: str | bool = os.environ.get("SCIPY_ARRAY_API", False)
SCIPY_DEVICE = os.environ.get("SCIPY_DEVICE", "cpu")
```

注意 `SCIPY_ARRAY_API` 的类型是 `str | bool`：它既可以是非空字符串（`"1"`、`"true"`、`"all"`，或一个 JSON 数组来圈定子集后端），未设置时则是 `False`。后续所有判断都靠「`if SCIPY_ARRAY_API:` 的真值」来决定走哪条路。

官方文档对这条原则与开关的说明见：

[doc/source/dev/api-dev/array_api.rst:L15-L21](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/doc/source/dev/api-dev/array_api.rst#L15-L21) —— 点明核心原则是「数组类型进、数组类型出」，并附带更严格的输入校验。

[doc/source/dev/api-dev/array_api.rst:L30-L38](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/doc/source/dev/api-dev/array_api.rst#L30-L38) —— 说明必须在 `import scipy` **之前**设置 `SCIPY_ARRAY_API=1`，并强调这是个临时过渡开关，不打算长期保留。

这两个细节解释了为什么你在实践中经常看到这样的命令行写法（必须在同一行先设变量再起 Python，保证导入前变量已就位）：

```bash
SCIPY_ARRAY_API=1 python my_script.py
```

#### 4.1.4 代码实践

1. **实践目标**：直观感受「开关是否设置，会改变 `array_namespace` 的返回值」。
2. **操作步骤**：在两个独立的 Python 进程里分别执行下面这段「示例代码」（非项目原有代码）。

   ```python
   # 示例代码：观察 SCIPY_ARRAY_API 对 array_namespace 的影响
   from scipy._lib._array_api import array_namespace, SCIPY_ARRAY_API
   import numpy as np

   x = np.array([1.0, 2.0, 3.0])
   xp = array_namespace(x)
   print("SCIPY_ARRAY_API =", SCIPY_ARRAY_API)
   print("namespace =", xp.__name__)
   ```

   分别用 `python demo.py` 与 `SCIPY_ARRAY_API=1 python demo.py` 运行。
3. **需要观察的现象**：两种情况下 `SCIPY_ARRAY_API` 的值不同。
4. **预期结果**：未设置时打印 `SCIPY_ARRAY_API = False`、`namespace = ...numpy`（兼容版 NumPy 命名空间）；设置后打印 `SCIPY_ARRAY_API = 1`、命名空间仍是 NumPy（因为输入就是 NumPy 数组，只是这次「经过了完整校验」）。
5. 若你的环境中 `scipy._lib._array_api` 因私有路径无法直接导入，可改用 `python -c "..."` 形式，或待本地验证。

#### 4.1.5 小练习与答案

- **练习 1**：为什么 `SCIPY_ARRAY_API` 要设计成「环境变量」而不是一个普通函数参数？
  - **参考答案**：因为它会改变**整个 SciPy 的输入校验严格程度**（拒绝 `np.matrix` 等），属于全局行为切换；若做成函数参数，几乎每个函数都要多一个参数，且容易和「函数本来就该拒绝某些输入」的语义混淆。用环境变量可以做到「迁移期内整个生态一起开/关」，最终目标是合入主干后彻底移除该开关。
- **练习 2**：文档说该变量「必须在导入 SciPy 前设置」，为什么？
  - **参考答案**：因为 `SCIPY_ARRAY_API` 在模块导入时就被读取并固化为模块级常量（见 4.1.3 的源码）。如果先 `import scipy` 再 `os.environ[...] = "1"`，常量已经定型，不会重新读取。

---

### 4.2 `array_namespace`：从输入数组推断 `xp`

#### 4.2.1 概念说明

`array_namespace` 是整个 Array API 机制的「心脏」。它的职责很纯粹：**给我一个或多个输入数组，我告诉你该用哪个命名空间 `xp` 来操作它们。**

它的判断依据是「看数组本身是什么类型」——这和上一讲 u2-l2 的 uarray 截然不同。uarray 是「用户显式声明用哪个后端」，靠注册表；而 Array API 是「代码看数组自己携带的身份」，数组本身就是分发依据。这也意味着：只要库实现了 Array API 标准，SciPy 不需要为它写任何注册代码就能识别它。

为了在不同后端间做出快速、正确的分类，SciPy 把每个数组的类型先归类成一个枚举标签，再做后续处理。

#### 4.2.2 核心流程

`array_namespace` 的工作分三步：

```
对每个输入数组 array：
   1. _validate_array_cls(type(array)) → 得到一个 _ArrayClsInfo 枚举标签
        ├─ skip       ：Python 标量（int/float/bool/None），直接忽略
        ├─ numpy      ：NumPy 数组（或被视作 NumPy 兼容的稀疏数组）
        ├─ array_like ：list / tuple，会被强制转成 NumPy 数组
        └─ unknown    ：其它；若它是 Array API 对象则按非 NumPy 后端处理
   2. 按标签分桶：NumPy 类放 numpy_arrays 桶，非 NumPy 的 Array API 对象放 api_arrays 桶
   3. 汇总：
        ├─ 只有 NumPy / array_like（api_arrays 为空）→ 直接返回 np_compat（省一次调用，性能优化）
        └─ 否则交给 array_api_compat.array_namespace 跨桶统一推断（混用非法时由它报错）
```

其中第一步用一个带 `lru_cache` 的函数完成分类：

#### 4.2.3 源码精读

先看分类标签的定义与分类函数：

[scipy/_lib/_array_api_override.py:L32-L36](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_array_api_override.py#L32-L36) —— `_ArrayClsInfo` 枚举，四个标签分别对应「跳过 / NumPy / array_like / 未知」。

[scipy/_lib/_array_api_override.py:L39-L70](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_array_api_override.py#L39-L70) —— `_validate_array_cls`：按 `isinstance` 链逐项判定。注意几个关键设计：

```python
@lru_cache(100)
def _validate_array_cls(cls: type, sparse_ok=False) -> _ArrayClsInfo:
    if issubclass(cls, list | tuple):
        return _ArrayClsInfo.array_like
    if issubclass(cls, SparseABC):              # 稀疏数组：默认拒绝
        if not sparse_ok: raise ValueError(...)
        return _ArrayClsInfo.numpy              # 允许时视作 NumPy 兼容
    if issubclass(cls, np.ma.MaskedArray): raise TypeError(...)  # 已知坏类型
    if issubclass(cls, np.matrix): raise TypeError(...)
    if issubclass(cls, np.ndarray | np.generic): return _ArrayClsInfo.numpy
    if issubclass(cls, int | float | complex | bool | type(None)):
        return _ArrayClsInfo.skip               # Python 标量直接跳过
    return _ArrayClsInfo.unknown
```

要点：① `@lru_cache(100)` 缓存「类型→标签」的判定结果，因为同一个数组类型会被反复判定，缓存能显著降低开销；② `np.float64` / `np.complex128` 同时也是 Python `float` / `complex` 的子类，所以 `np.generic` 的判断**必须**放在标量判断之前（源码注释 L64-L66 明确强调了这一顺序陷阱）；③ 稀疏数组默认报错，只有调用方显式传 `sparse_ok=True`（如 `scipy.sparse.linalg` 的函数）才放行。

再看 `array_namespace` 主体：

[scipy/_lib/_array_api_override.py:L73-L156](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_array_api_override.py#L73-L156) —— 整个分发入口。三处最关键的代码点：

```python
if not SCIPY_ARRAY_API:           # L111-L113：未开开关时的「快速通道」
    return np_compat              # 直接返回兼容版 NumPy 命名空间，跳过所有校验
```

这五句是「不开开关就零开销、零行为变化」的根本保障——也是默认安装下 SciPy 性能不受影响的原因。

```python
if array.dtype.kind in 'iufcb':   # L124：i=整数 u=无符号 f=浮点 c=复数 b=布尔
    numpy_arrays.append(array)    # 只接受数值/布尔 dtype
```

```python
if not api_arrays:                # L151-L152：全是 NumPy/array_like 时的性能优化
    return np_compat              # 不去调用 array_api_compat，省一次函数调用
return array_api_compat.array_namespace(*numpy_arrays, *api_arrays)  # L156
```

最后一行把「跨后端统一推断」交给上游 `array_api_compat.array_namespace`；如果用户混用了不同后端的数组（例如一个 NumPy 一个 CuPy），也是由它来抛出错误（见 L154-L155 的注释）。

#### 4.2.4 代码实践

1. **实践目标**：亲手触发 `array_namespace` 对不同输入的分桶行为，观察它在哪些情况下报错。
2. **操作步骤**：在已设置 `SCIPY_ARRAY_API=1` 的环境里运行下面这段「示例代码」（需先 `pip install array-api-strict`）。

   ```python
   # 示例代码：观察 array_namespace 的分桶与报错
   import os; os.environ.setdefault("SCIPY_ARRAY_API", "1")  # 若已从命令行设置可省
   from scipy._lib._array_api import array_namespace
   import numpy as np
   import array_api_strict as xp_strict

   print(array_namespace(np.array([1, 2, 3])).__name__)            # NumPy
   print(array_namespace([1, 2, 3]).__name__)                      # 列表 → NumPy
   print(array_namespace(xp_strict.asarray([1.0, 2.0])).__name__)  # 非NumPy后端

   for bad in [np.asmatrix([[1, 2]]), np.ma.asarray([1, 2])]:
       try:
           array_namespace(bad)
       except TypeError as e:
           print("被拒绝:", type(bad).__name__, "->", e)
   ```

3. **需要观察的现象**：前三行都能打印出一个命名空间名；后两个会抛 `TypeError`。
4. **预期结果**：`np.matrix` 与 `np.ma.MaskedArray` 被严格校验拒绝，错误信息形如「Inputs of type `numpy.matrix` are not supported.」（见源码 L58-L59）。注意：上面用 `os.environ.setdefault` 在导入 **之后** 设置其实已晚，真正生效需在命令行用 `SCIPY_ARRAY_API=1 python ...` 启动——这正是练习 4.1.5 第 2 题的用意。
5. 若 `array-api-strict` 未安装，第三行会 `ImportError`；可仅运行前两行与拒绝逻辑，标注「待本地验证」其余部分。

#### 4.2.5 小练习与答案

- **练习 1**：为什么 `_validate_array_cls` 要用 `@lru_cache(100)`？「100」会不会太小？
  - **参考答案**：因为同一个数组类型（如 `numpy.ndarray`、`torch.Tensor`）在一次 SciPy 调用链里会被反复送进来判定，而「类型→标签」是纯函数、无副作用，非常适合缓存。100 个槽位对「现实里实际会用到的数组类型数量」绰绰有余（NumPy + Cupy + Torch + JAX + Dask + 各自标量，远不到 100）。
- **练习 2**：如果同时传入一个 NumPy 数组和一个 PyTorch 张量，会发生什么？
  - **参考答案**：两者会被分别放入 `numpy_arrays` 与 `api_arrays` 两个桶，最终走到 L156 调用 `array_api_compat.array_namespace(*numpy_arrays, *api_arrays)`。混用不同后端是不允许的，由上游函数抛出错误（L154-L155 注释）。

---

### 4.3 `_asarray`：SciPy 版的 `np.asarray`

#### 4.3.1 概念说明

Array API 标准里有一个 `xp.asarray`，但它**不包含** NumPy 特有的 `order`（内存布局）、`check_finite`（检查 NaN/Inf）、`subok`（允许子类透传）等参数。而 SciPy 大量函数恰恰依赖这些参数（比如线性代数要求 C/F 连续布局、积分要求输入有限）。

于是 SciPy 写了一个 `_asarray`，它在「Array API 标准的 `xp.asarray`」之上补回了这些 SciPy 语义：对 NumPy 后端走 NumPy 原生 API（支持 `order`），对其它后端走标准的 `xp.asarray`，并把 `order` 静默忽略（因为标准里没有这个概念）。

#### 4.3.2 核心流程

```
_asarray(array, dtype, order, copy, xp, check_finite, subok)
   │
   ├─ 若未传 xp：先 array_namespace(array) 推断 xp
   ├─ 若 xp 是 NumPy：
   │     ├─ copy=True  → np.array(array, order, dtype, subok)
   │     ├─ subok=True → np.asanyarray(array, order, dtype)   # 保留子类
   │     └─ 否则       → np.asarray(array, order, dtype)
   ├─ 若 xp 是其它后端：
   │     └─ xp.asarray(array, dtype, copy)  # order 在此被忽略
   │           失败时再尝试一次「强转命名空间」兜底
   └─ check_finite=True → _check_finite(array, xp)：有 NaN/Inf 就抛 ValueError
```

#### 4.3.3 源码精读

[scipy/_lib/_array_api.py:L76-L120](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_array_api.py#L76-L120) —— `_asarray` 的完整实现。三个关键代码点：

```python
if xp is None:
    xp = array_namespace(array)          # L100-L101：未指定 xp 时现场推断
```

```python
if is_numpy(xp):                         # L102-L109：NumPy 分支，支持 order/subok
    if copy is True:
        array = np.array(array, order=order, dtype=dtype, subok=subok)
    elif subok:
        array = np.asanyarray(array, order=order, dtype=dtype)
    else:
        array = np.asarray(array, order=order, dtype=dtype)
else:                                    # L110-L115：非 NumPy 分支，走标准 asarray
    try:
        array = xp.asarray(array, dtype=dtype, copy=copy)
    except TypeError:
        coerced_xp = array_namespace(xp.asarray(3))
        array = coerced_xp.asarray(array, dtype=dtype, copy=copy)
```

注意 L110-L115 的 `try/except TypeError`：某些后端的 `asarray` 不接受 `copy=` 关键字，此时 SciPy 会先用 `xp.asarray(3)` 探出一个「原生」命名空间再重试，这是一种向后兼容的兜底。

最后是有限性检查：

[scipy/_lib/_array_api.py:L70-L74](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_array_api.py#L70-L74) —— `_check_finite`：用 `xp.isfinite` + `xp.all` 判断，**全程通过 `xp` 调用**，因此对任何 Array API 后端都成立，而不是写死 `np.isfinite`。这正是「用 `xp` 取代 `np`」这一原则的缩影。

#### 4.3.4 代码实践

1. **实践目标**：体会 `_asarray` 在 NumPy 后端下对 `order` 的支持，以及 `check_finite` 的拦截。
2. **操作步骤**：运行下面这段「示例代码」。

   ```python
   # 示例代码：_asarray 的 order 与 check_finite
   from scipy._lib._array_api import _asarray
   import numpy as np

   a = np.arange(6).reshape(2, 3)
   c_cont = _asarray(a, order='C')          # C 连续
   f_cont = _asarray(a, order='F')          # Fortran 连续
   print("C 连续:", c_cont.flags['C_CONTIGUOUS'])
   print("F 连续:", f_cont.flags['F_CONTIGUOUS'])

   try:
       _asarray(np.array([1.0, np.nan, 3.0]), check_finite=True)
   except ValueError as e:
       print("被拦截:", e)
   ```

3. **需要观察的现象**：两种内存布局标志一真一假；含 NaN 的输入触发 `ValueError`。
4. **预期结果**：`C 连续: True`、`F 连续: True`（注意 `a` 转 F 后 `F_CONTIGUOUS` 为 True），并打印「array must not contain infs or NaNs」。
5. 若私有路径不可导入，可改为阅读源码理解，标注「待本地验证」。

#### 4.3.5 小练习与答案

- **练习 1**：为什么 `order` 参数对非 NumPy 后端「静默忽略」而不是报错？
  - **参考答案**：`order`（内存布局）是 NumPy 特有概念，Array API 标准里没有。绝大多数数值算法对内存布局只是「性能偏好」而非「正确性要求」，所以对非 NumPy 后端忽略它既能保持调用方代码不变，又不会引入错误。如果某函数真的强依赖布局（如对接 Fortran 列主序代码），它会先转成 NumPy 再处理（见 4.5 的编译代码桥接范式）。
- **练习 2**：`_asarray` 里的 `subok=True` 对应原来的哪个 NumPy 函数？
  - **参考答案**：对应 `np.asanyarray`——它会保留输入的子类（如矩阵子类）。在 NumPy 分支里 `subok=True` 走的就是 `np.asanyarray`（L106-L107）。

---

### 4.4 `xp_*` 辅助家族与两种分发的边界

#### 4.4.1 概念说明

围绕 `array_namespace`，`_array_api.py` 沉淀了一整套 `xp_*` 辅助函数，把「以前写死 `np.xxx`」的地方统一改写成「`xp.xxx`」。它们的共同特征是：**第一个动作几乎都是 `xp = array_namespace(x)`**，然后用这个 `xp` 调标准函数。

更重要的是，本讲的 Array API 分发和上一讲 u2-l2 的 uarray 分发，是两种**互补但不同**的机制，必须分清边界：

| 维度 | uarray（u2-l2） | Array API（本讲） |
| --- | --- | --- |
| 谁来决定后端 | 用户显式 `set_backend` | 代码看输入数组类型 |
| 分发依据 | 注册表 / domain | 数组自身携带的命名空间 |
| 被拦截的对象 | 「多方法」(multimethod)，如 `scipy.fft.fft` | 普通函数体内部改用 `xp.` 调用 |
| 典型代表 | `fft.fft` / `dct` / `fht` | `stats.trim_mean` 等大量 `.py` 函数 |
| 拦截不到的 | `fftfreq` / `fftshift`（用 array_namespace） | —— |

回忆 u2-l2 的结论：`fftfreq`、`fftshift` 这类「纯数组操作」函数走的是 `array_namespace`（即本讲机制），所以**无法被 uarray 后端拦截**——这正是两种分发的天然分界线。

#### 4.4.2 核心流程

以 `xp_vector_norm`（向量范数）为例，它展示了「同一函数在开关开/关时走两条路」的典型结构：

```
xp_vector_norm(x, axis, keepdims, ord)
   ├─ xp = array_namespace(x)                  # 先确定命名空间
   ├─ if SCIPY_ARRAY_API:                      # 开关开着
   │     ├─ 若 xp 有 linalg 扩展 → xp.linalg.vector_norm(...)   # 走标准扩展
   │     └─ 否则只支持 ord=2 → xp.sum(xp.conj(x)*x)**0.5       # 退化实现
   └─ else:                                    # 开关关着（默认）
         └─ np.linalg.norm(x, ...)             # 维持向后兼容的老路径
```

#### 4.4.3 源码精读

先看「确定命名空间」这个所有 `xp_*` 函数的共同起点，以及 `xp_vector_norm` 的双分支：

[scipy/_lib/_array_api.py:L449-L472](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_array_api.py#L449-L472) —— `xp_vector_norm`。注意 L456 的 `if SCIPY_ARRAY_API:` 把函数劈成两半：

```python
xp = array_namespace(x) if xp is None else xp   # L454：共同起点
if SCIPY_ARRAY_API:                              # L456：开了开关
    if hasattr(xp, 'linalg'):
        return xp.linalg.vector_norm(x, axis=axis, keepdims=keepdims, ord=ord)
    else:
        if ord != 2: raise ValueError(...)       # 退化实现只支持 2 范数
        return xp.sum(xp.conj(x) * x, axis=axis, keepdims=keepdims)**0.5
else:                                            # L470-L472：没开开关
    return np.linalg.norm(x, ord=ord, axis=axis, keepdims=keepdims)
```

这里的退化实现对应欧几里得范数的数学定义，对复数组也成立（用 `conj` 保证复数正确）：

\[
\|x\|_2 = \sqrt{\sum_i \overline{x_i}\, x_i}
\]

行内范数则记作 \( \|x\|_2 = \sqrt{x^{\!*} x} \)。

再看一个体现「Array API 也能跨库委托」的函数 `scipy_namespace_for`：

[scipy/_lib/_array_api.py:L426-L445](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_array_api.py#L426-L445) —— 它把一个非 NumPy 后端映射到「该生态里对应 SciPy 的命名空间」：CuPy → `cupyx.scipy`、JAX → `jax.scipy`、PyTorch → `xp` 自身。这让 SciPy 在某些场景下可以把计算整体委托给对方生态里已有的 SciPy 移植实现，而不必自己重写。

最后是导入关系，说明 `array_namespace` / `SCIPY_ARRAY_API` 真正定义在 `_array_api_override.py`、被 `_array_api.py` 复用：

[scipy/_lib/_array_api.py:L45-L47](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_array_api.py#L45-L47) —— 从 `_array_api_override` 导入 `array_namespace, SCIPY_ARRAY_API, SCIPY_DEVICE`。这就是为什么本讲把这两个文件视为一对：一个提供「原始分发 + 开关」，另一个在它之上构建「辅助生态」。

#### 4.4.4 代码实践

1. **实践目标**：对照「开关开/关」两种情况下 `xp_vector_norm` 的执行路径差异。
2. **操作步骤**：用两份独立进程（一份 `SCIPY_ARRAY_API=1`，一份不设）运行下面「示例代码」。

   ```python
   # 示例代码：观察 xp_vector_norm 的两条路径
   from scipy._lib._array_api import xp_vector_norm, SCIPY_ARRAY_API
   import numpy as np
   x = np.array([3.0, 4.0])
   print("SCIPY_ARRAY_API =", bool(SCIPY_ARRAY_API))
   print("norm =", xp_vector_norm(x))
   ```

3. **需要观察的现象**：两次结果数值一致（都是 5.0），但内部走的分支不同。
4. **预期结果**：未开开关时走 `np.linalg.norm`（L472）；开启后走 `xp.linalg.vector_norm` 或退化实现。你无法从返回值看出差异，但可以在源码 L456 与 L470 各加一行 `print`（仅用于学习，勿提交）来确认分支。
5. 本实践属于「源码阅读 + 行为推断」型，若不便改源码，标注「待本地验证」分支命中情况。

#### 4.4.5 小练习与答案

- **练习 1**：`xp_vector_norm` 在退化实现里为什么要用 `xp.conj(x) * x` 而不是 `x * x`？
  - **参考答案**：为了正确处理复数数组。复向量的 2 范数是 \( \sqrt{\sum \overline{x_i}x_i} \)（即模长平方和），必须对其中一个因子取共轭；若直接 `x*x` 会得到 \( x_i^2 \)，对复数是错的。用标准 `conj` 又保证了跨后端可用。
- **练习 2**：请用一句话说清 uarray 与 Array API 的分界。
  - **参考答案**：uarray 拦截的是被注册为「多方法」的少数函数（由用户选后端）；Array API 改造的是普通 `.py` 函数体（由输入数组类型决定 `xp`）。`fftfreq` 这类走 `array_namespace` 的函数，uarray 拦截不到。

---

### 4.5 实战串联：一个统计函数如何用上 Array API

#### 4.5.1 概念说明

前面四个模块都在讲「零件」。本模块把它们装到一起，看一个真实 SciPy 函数是怎么按官方文档的范式（见 [array_api.rst:L178-L192](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/doc/source/dev/api-dev/array_api.rst#L178-L192)）使用这些零件的。官方给的「加支持三步法」是：

1. **输入校验**：`xp = array_namespace(arr); arr = xp.asarray(arr)`；
2. **用 `xp` 取代 `np`**：函数体内所有 `np.xxx` 改成 `xp.xxx`；
3. **桥接编译代码**：调用 C/Cython/Fortran 前先 `np.asarray(x)` 转回 NumPy，算完再 `xp.asarray(y)` 转回去。

此外，函数还会用 `@xp_capabilities` 装饰器声明自己支持哪些后端/设备，这个装饰器会自动往 docstring 里插入一张「能力表」（NumPy/CuPy/PyTorch/JAX/Dask 在 CPU/GPU 上的支持情况）。

#### 4.5.2 核心流程

以 `scipy.stats` 里带权分位数的输入校验函数 `_quantile_iv` 为例：

```
_quantile_iv(x, p, method, axis, nan_policy, keepdims, weights)
   │
   ├─ xp = array_namespace(x, p, weights)         # 一次推断所有输入的共同命名空间
   ├─ 用 xp.isdtype / xp.asarray 校验 dtype
   ├─ x, p, weights = xp_promote(x, p, weights, force_floating=True, xp=xp)  # 统一类型提升
   ├─ p = xp.asarray(p, device=xp_device(x))      # 让 p 落在与 x 相同的设备上
   └─ 后续全部用 xp.sort / xp.take_along_axis / xp.moveaxis ... 计算
```

#### 4.5.3 源码精读

[scipy/stats/_quantile.py:L19-L20](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/stats/_quantile.py#L19-L20) —— 函数第一行就是 `xp = array_namespace(x, p, weights)`，把三个输入一次性交给 `array_namespace` 推断共同命名空间。这正是官方范式第 1 步。

[scipy/stats/_quantile.py:L49-L50](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/stats/_quantile.py#L49-L50) —— 用 `xp_promote(...)` 做类型提升（强制浮点），再用 `xp.asarray(p, device=xp_device(x))` 把 `p` 放到与 `x` 相同的设备上。注意 `device=` 是 Array API 标准里跨设备（CPU/GPU）的关键参数，NumPy 原生 `asarray` 没有它。

再看一个声明了能力的公开函数：

[scipy/stats/_stats_py.py:L3648-L3650](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/stats/_stats_py.py#L3648-L3650) —— `scipy.stats.trim_mean` 头上的 `@xp_capabilities(marray=True)` 装饰器。它声明该函数支持非 NumPy 后端（且支持 MArray 掩码数组），并会自动在 docstring 里追加一张「Array API Standard Support」能力表。

最后看测试侧如何「用同一份测试跑遍所有后端」：

[scipy/conftest.py:L318-L319](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/conftest.py#L318-L319) —— `xp` fixture：参数化在 `xp_available_backends` 上，每个后端跑一遍测试。

[scipy/conftest.py:L351-L353](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/conftest.py#L351-L353) —— 关键一行 `xp = array_namespace(xp.empty(0))`：把原始后端模块（如 `torch`）经 `array_namespace` 包一层，得到带兼容别名（`concat` 等）的命名空间，再交给测试函数使用。

[scipy/conftest.py:L200-L211](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/conftest.py#L200-L211) —— 只有当 `SCIPY_ARRAY_API` 为真时，才尝试 `import array_api_strict` 并把它加入后端列表（并要求版本 ≥ 2.3、设定 `api_version='2025.12'`）。这就是为什么练习里必须先设开关——否则 `xp` fixture 只会跑 NumPy 一个后端。

#### 4.5.4 代码实践（本讲主实践）

1. **实践目标**：设置 `SCIPY_ARRAY_API=1`，用 `array-api-strict` 数组调用 `scipy.stats.trim_mean`，验证「数组类型进、数组类型出」。
2. **操作步骤**：

   先安装严格后端（如已安装可跳过）：

   ```bash
   pip install array-api-strict
   ```

   把下面这段「示例代码」存为 `demo_trim.py`，**务必用 `SCIPY_ARRAY_API=1` 启动**：

   ```python
   # 示例代码：用 array-api-strict 数组调用 scipy.stats.trim_mean
   import array_api_strict as xp_strict
   import numpy as np
   from scipy.stats import trim_mean

   # 构造一个 2D 严格数组，每列是一组样本
   x = xp_strict.asarray(
       np.array([[1., 10.],
                 [2., 20.],
                 [3., 30.],
                 [100., 4.],     # 离群点，会被 trim 掉一部分
                 [5., 50.]])
   )
   print("输入类型:", type(x).__name__, "命名空间:", xp_strict.__name__)

   # 沿 axis=0 截掉两端各 20% 后求均值
   m = trim_mean(x, proportiontocut=0.2, axis=0)
   print("输出类型:", type(m).__name__)
   print("结果:", m)
   print("输入输出命名空间一致:", type(x).__name__ == type(m).__name__)
   ```

   运行：

   ```bash
   SCIPY_ARRAY_API=1 python demo_trim.py
   ```

3. **需要观察的现象**：输入与输出数组的类型相同（都是 `array_api_strict` 的数组），结果是一个长度为 2 的 1D 严格数组。
4. **预期结果**：`输入输出命名空间一致: True`，打印出的 `m` 是 `array_api_strict` 数组而不是 NumPy 数组。这验证了「数组类型进、数组类型出」。
5. 如果你想进一步对比：把启动命令换成不带 `SCIPY_ARRAY_API=1`，观察 `trim_mean` 是否仍接受严格数组（此时 `array_namespace` 走快速通道，行为可能不同或报错）——这正是「开关控制整套严格校验」的直观体现。若 `array-api-strict` 不可用，可改用 `torch`（CPU 张量）重复本实验；若两者都无，标注「待本地验证」。

#### 4.5.5 小练习与答案

- **练习 1**：为什么 `_quantile_iv` 里要先 `xp = array_namespace(x, p, weights)` 一次传入三个参数，而不是分三次调用？
  - **参考答案**：因为多个输入必须共享**同一个**命名空间（不能一个 NumPy 一个 Torch）。一次传入三个，`array_namespace` 就能同时检查它们是否一致、并推断出公共命名空间；若不一致，由上游 `array_api_compat.array_namespace` 报错。分三次调用则无法发现「混用」错误。
- **练习 2**：`@xp_capabilities` 装饰器除了「声明能力」，还会产生什么用户可见的效果？
  - **参考答案**：它会读取函数的能力配置，自动在 docstring 里追加一段「Array API Standard Support」说明和一张各后端在 CPU/GPU 上的支持情况表（由 `_make_capabilities_note` 生成，见 `_array_api.py` 的 L839-L939），让用户一眼看出该函数支持哪些数组库。

---

## 5. 综合实践

把本讲的知识串成一个完整的小任务：**写一个「后端无关」的最小统计脚本，并验证它真的后端无关。**

任务要求：

1. 准备一段计算逻辑：给定一个 1D 数组 `x`，计算它的截尾均值 `trim_mean(x, 0.1)` 与「去均值后的向量 2 范数」。其中范数请用本讲学过的 `xp_vector_norm`，不要用 `np.linalg.norm`。
2. 把同一段逻辑分别作用在 **NumPy 数组** 与 **`array-api-strict` 数组** 上（两个输入数值相同）。
3. 用 `SCIPY_ARRAY_API=1` 启动，验证：
   - 两种输入下，输出数组的类型分别与各自的输入类型一致（即「类型进、类型出」）；
   - 两者的**数值结果**在浮点误差范围内相等（可以把严格数组结果转回 NumPy 再比较，转回工具可参考 `_xp_copy_to_numpy`，见 [scipy/_lib/_array_api.py:L151-L190](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/_lib/_array_api.py#L151-L190)）。
4. 回答：你的范数计算里，`xp_vector_norm` 在两种输入下分别走了 4.4 里哪一条分支？为什么？

完成这个任务，你就把「开关 → `array_namespace` → `_asarray` / `xp_*` → 真实函数」整条链路打通了。

## 6. 本讲小结

- SciPy 通过 Python Array API 标准，让同一套代码能在 NumPy / CuPy / PyTorch / JAX / Dask 上运行，核心原则是「数组类型进、数组类型出」。
- 一切由环境变量 `SCIPY_ARRAY_API=1`（导入前设置）开启；不开时 `array_namespace` 直接返回兼容版 NumPy 命名空间并跳过所有校验，保证零行为变化、零性能损耗。
- `array_namespace` 是分发的心脏：它用带 `lru_cache` 的 `_validate_array_cls` 把每个输入数组归类，分桶后推断出公共命名空间 `xp`，并顺便完成「拒绝 `matrix`/`MaskedArray`/非数值 dtype」的严格校验。
- `_asarray` 是 SciPy 版的 `np.asarray`，补回了标准里没有的 `order` / `check_finite` / `subok`；整个 `xp_*` 家族的共同起点都是 `xp = array_namespace(x)`。
- 本讲的 Array API 分发（代码看数组）与上一讲 u2-l2 的 uarray 分发（用户选后端）是两种互补机制；`fftfreq` 等走 `array_namespace` 的普通函数不在 uarray 拦截范围内。
- 测试侧用 `conftest.py` 的 `xp` fixture 把同一份测试参数化到所有可用后端上，并用 `@xp_capabilities` 声明并文档化每个函数的后端支持矩阵。

## 7. 下一步学习建议

- 想看「Array API 在某个子包里的落地进度」，可阅读 `doc/source/dev/api-dev/array_api_modules_tables/` 下的各子包能力表（例如 `signal.rst`），了解哪些函数已迁移、哪些仍 `np_only`。
- 想深入「跨库委托」，可结合本讲的 `scipy_namespace_for`，去读 `cupyx.scipy` 与 `jax.scipy` 的对应实现，理解 SciPy 如何把整段计算交给对方生态。
- 下一讲 u2-l4 将转向另一个底层机制 `LowLevelCallable`——它解决的是「让 C/Fortran 底层代码高效回调 Python」，与本讲的「让 Python 高效调用各后端数组」正好是一对镜像问题，建议对照学习。
- 若你对「公共 API 治理」感兴趣，可提前跳读 u13-l4，那里会讨论 Array API 标准化与 sparray 迁移在架构层面的整体取舍。
