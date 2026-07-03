# 离散余弦变换 dct/dctn：type、norm 与 orthogonalize

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清 `scipy.fft` 中 `dct` / `idct` / `dctn` / `idctn` 四个函数的参数含义，以及它们之间的封装与互逆关系。
- 写出 DCT 四种 type（I/II/III/IV）的数学定义，并解释为什么「DCT-II 的逆是 DCT-III」、而 type I 与 IV「自逆」。
- 区分 `norm` 的三种取值 `backward` / `ortho` / `forward`，并看懂源码里 `_NORM_MAP` 与 `2 - inorm` 这套整数翻转是如何同时兼顾方向与归一化的。
- 理解 `orthogonalize` 正交化选项的默认行为（`norm="ortho"` 时默认开启，否则关闭），并明白这个默认值最终是在 C 扩展层决定的。
- 跟踪一次 `dct(x)` 调用，从公共 API 穿过 uarray 分派、`_execute` 数组桥接、最终落到 `pyduccfft.dct` 的完整四层链路。

## 2. 前置知识

本讲承接 [u2-l1](u2-l1-complex-fft.md)，假设你已经熟悉以下概念（若不熟悉，请先读 u2-l1）：

- **四层调用链**：公共 API（`_basic.py` / `_realtransforms.py`）→ uarray 分派 → 后端（`_xxx_backend.py`）→ ducc 核心（`_duccfft`，最终 C 扩展 `pyduccfft`）。
- **`Dispatchable` 与分派协议**：公共函数体只 `return (Dispatchable(x, np.ndarray),)`，这是「分派声明」而非计算代码。
- **`norm` 三模式**：`backward` / `ortho` / `forward`，正逆变换通过缩放因子的归属来配对。

此外补充两个本讲专用的直觉概念：

- **离散余弦变换（DCT）**：和 FFT 一样是把信号分解成「基函数」的叠加，但 DCT 的基函数全部是余弦波 \(\cos(\cdot)\)，输入输出都是**实数**。DCT 特别适合处理「能量集中在低频」的实信号（如图像、音频），因此被 JPEG、MP3 等压缩标准广泛采用。
- **边界条件与 type**：DCT 的「类型」本质上是**对信号两端做了何种对称延拓**的假设。延拓方式不同，余弦波的采样位置就不同，于是衍生出 type I~IV（理论上还有 V~VIII，但 SciPy 只实现前四种）。
- **正交矩阵**：一个方阵 \(O\) 若满足 \(O^\top O = I\)（即 \(O @ O.T = \mathrm{eye}(N)\)），就称为正交矩阵。正交矩阵对应的变换**保持向量长度（2-范数）不变**，且其逆等于其转置。`orthogonalize` 选项的目标就是把 DCT 的系数矩阵改造成正交矩阵。

> 小提示：本讲只讲**余弦**变换（DCT）。**正弦**变换（DST）的源码结构与 DCT 几乎完全对称（同一份 `_r2r` / `_r2rn`、同一个 `partial` 派生手法），留给 [u3-l2](u3-l2-dst.md) 专题展开。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
|------|------|-----------|
| [_realtransforms.py](_realtransforms.py) | 公共 API 层 | `dct` / `idct` / `dctn` / `idctn` 的签名、docstring（含四种 type 的数学定义）、`@_dispatch` 分派声明 |
| [_realtransforms_backend.py](_realtransforms_backend.py) | 后端层 | `_execute` 统一封装：`array_namespace` → `np.asarray` → 调 `_duccfft` → 转回原命名空间 |
| [_duccfft/realtransforms.py](_duccfft/realtransforms.py) | ducc 核心（Python 薄壳） | `_r2r` / `_r2rn` 的预处理逻辑：类型翻转、形状修正、复数拆分；`functools.partial` 派生 8 个函数 |
| [_duccfft/helper.py](_duccfft/helper.py) | ducc 核心（预处理） | `_normalization`、`_NORM_MAP`、`_asfarray`、`_fix_shape` |
| [_duccfft/pyduccfft.cxx](_duccfft/pyduccfft.cxx) | C 扩展（真正算 DCT） | `dct` 函数；`orthogonalize` 的默认值正是在这里由 `inorm==1` 决定 |
| [_backend.py](_backend.py) | 分派层 | `_ScipyBackend.__ua_function__` 按方法名在三个 backend 模块间查找实现 |

永久链接的 HEAD 固定为 `5f09bd719ca35a5c4de9644a097d379e5b3b4165`，下文所有链接均基于此。

---

## 4. 核心概念与源码讲解

### 4.1 dct / dctn / idct / idctn：公共签名与四层分派

#### 4.1.1 概念说明

`scipy.fft` 一共暴露 4 个余弦变换函数：

| 函数 | 维度 | 方向 | 说明 |
|------|------|------|------|
| `dct` | 1-D | 正变换 | 沿单条轴做 DCT |
| `idct` | 1-D | 逆变换 | `dct` 的逆 |
| `dctn` | N-D | 正变换 | 沿多条轴做 DCT |
| `idctn` | N-D | 逆变换 | `dctn` 的逆 |

它们的关系是：**`dct` 是 `dctn` 在「单轴」情形下的封装**（后端 `dct` 直接转调 `_duccfft.dct`，而 `_duccfft.dct` 又是 `_r2rn` 的 1-D 版本派生而来，4.2 节会看到）。这一点和 `fft` 之于 `fftn` 的关系完全一致（参见 [u2-l3](u2-l3-multidimensional-fft.md)）。

#### 4.1.2 核心流程：一次 `dct(x)` 的四层穿透

```text
scipy.fft.dct(x)                      # 公共 API：_realtransforms.py
   │  return (Dispatchable(x, np.ndarray),)   ← 只声明分派，不算
   ▼
uarray 按 domain "numpy.scipy.fft" 路由
   ▼
_ScipyBackend.__ua_function__         # _backend.py：按方法名 'dct' 查找
   │  getattr(_realtransforms_backend, 'dct')
   ▼
_realtransforms_backend.dct           # 后端：_execute(_duccfft.dct, ...)
   │  x = np.asarray(x); ... ; return xp.asarray(y)   ← 数组桥接
   ▼
_duccfft.dct  (= functools.partial(_r2r, True, pfft.dct))   # ducc 核心
   │  _asfarray / _fix_shape_1d / _normalization / 类型翻转
   ▼
pyduccfft.dct(tmp, type, (axis,), norm, out, workers, orthogonalize)   # C 扩展
```

记住这条链路，本讲后面三节都在拆解链路末端的数学细节。

#### 4.1.3 源码精读

**公共签名（以 `dctn` 为例）。** 注意它套了两个装饰器：外层 `@xp_capabilities(cpu_only=True, allow_dask_compute=True)` 声明数组标准能力（实变换被标记为 `cpu_only`，详见 [u6-l2](u6-l2-xp-capabilities-array-api.md)），内层 `@_dispatch` 把普通函数变成可分派的多方法：

[_realtransforms.py:9-12](_realtransforms.py#L9-L12) — `dctn` 的装饰器与签名（`s` 控制各轴输出长度，`axes` 控制变换轴，`orthogonalize` 是**仅关键字**参数）：

```python
@xp_capabilities(cpu_only=True, allow_dask_compute=True)
@_dispatch
def dctn(x, type=2, s=None, axes=None, norm=None, overwrite_x=False,
         workers=None, *, orthogonalize=None):
```

函数体只有一行，**不做任何计算**，只是把输入 `x` 包成 `Dispatchable` 返回，告诉 uarray「请替换这个参数后再分派给我」：

[_realtransforms.py:71-72](_realtransforms.py#L71-L72) — `dctn` 的函数体就是分派声明：

```python
    """
    ...
    """
    return (Dispatchable(x, np.ndarray),)
```

> 一个容易踩坑的细节：`dctn` 的 `orthogonalize` 在 `*` 之后、是**仅关键字参数**；而 `dct`（1-D 版）的 `orthogonalize` 前面**没有 `*`**，是「位置或关键字」参数。对比两处签名：

[_realtransforms.py:276-277](_realtransforms.py#L276-L277) — `dct` 的签名，`orthogonalize=None` 没有 `*` 前缀，可按位置传：

```python
def dct(x, type=2, n=None, axis=-1, norm=None, overwrite_x=False, workers=None,
        orthogonalize=None):
```

**分派层查找。** `_ScipyBackend` 按方法名 `dct` 依次在三个 backend 模块里 `getattr`，命中 `_realtransforms_backend.dct`：

[_backend.py:19-29](_backend.py#L19-L29) — 默认后端按方法名路由：

```python
    @staticmethod
    def __ua_function__(method, args, kwargs):
        fn = getattr(_basic_backend, method.__name__, None)
        if fn is None:
            fn = getattr(_realtransforms_backend, method.__name__, None)
        if fn is None:
            fn = getattr(_fftlog_backend, method.__name__, None)
        if fn is None:
            return NotImplemented
        return fn(*args, **kwargs)
```

**后端 `_execute` 数组桥接。** 这是 `_realtransforms_backend.py` 的全部精华——无论你传进来的是 numpy、CuPy 还是其它数组库，都先 `np.asarray` 转成 numpy 喂给 ducc 核心，算完再用 `xp.asarray` 转回**调用方原来的命名空间**：

[_realtransforms_backend.py:8-15](_realtransforms_backend.py#L8-L15) — `_execute` 把任意数组库的输入桥接到 ducc 核心再转回：

```python
def _execute(duccfft_func, x, type, s, axes, norm, 
             overwrite_x, workers, orthogonalize):
    xp = array_namespace(x)
    x = np.asarray(x)
    y = duccfft_func(x, type, s, axes, norm,
                       overwrite_x=overwrite_x, workers=workers,
                       orthogonalize=orthogonalize)
    return xp.asarray(y)
```

注意 `array_namespace(x)`（[第 10 行](_realtransforms_backend.py#L10)）必须在 `np.asarray` **之前**调用——一旦转成 numpy 数组，原始命名空间信息就丢了，所以先记下 `xp`。8 个公共函数（dct/dctn/idct/idctn + dst 四个）全是 `_execute` 的一行转调，例如：

[_realtransforms_backend.py:18-21](_realtransforms_backend.py#L18-L21) — `dctn` 后端就是一行 `_execute`：

```python
def dctn(x, type=2, s=None, axes=None, norm=None,
         overwrite_x=False, workers=None, *, orthogonalize=None):
    return _execute(_duccfft.dctn, x, type, s, axes, norm, 
                    overwrite_x, workers, orthogonalize)
```

#### 4.1.4 代码实践

**实践目标**：亲眼确认公共函数体不含计算逻辑，并验证四层链路的可逆性。

**操作步骤**：

```python
import numpy as np
import scipy.fft as spfft

# 1. 观察公共函数的「真身」：解包 uarray 多方法后能看到原函数体
print(spfft.dct)                 # 一个 multimethod 对象
# scipy.fft 的 dct 是 uarray 包装的；其 __wrapped__ 才是 _realtransforms.dct

# 2. 跑一个最简单的可逆性检查
rng = np.random.default_rng(0)
y = rng.standard_normal((16, 16))
print(np.allclose(y, spfft.idctn(spfft.dctn(y))))   # 预期 True

# 3. 对比 dct 与 dctn：dct 是 dctn 的单轴封装
a = rng.standard_normal(4)
print(np.allclose(spfft.dct(a), spfft.dctn(a)))     # 预期 True（都默认沿最后一条轴）
```

**需要观察的现象**：第 2、3 步都应打印 `True`，说明正逆配对恒等、`dct` 与 `dctn` 在单轴上行为一致。

**预期结果**：三行依次为 `True`、`True`（两次）。若你对 `spfft.dct` 直接 `print`，看到的是一个 uarray multimethod 而非普通 `function`，这正是 `@_dispatch` 的效果。

> 说明：上述对 `__wrapped__` 的观察属于「源码阅读型实践」，运行结果待本地验证（不同 scipy 版本下 uarray 的内部属性名可能略有差异）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `_execute` 必须先 `xp = array_namespace(x)` 再 `x = np.asarray(x)`，而不能反过来？

**参考答案**：`np.asarray(x)` 会把任意数组库的数组（如 CuPy 的 GPU 数组）转成 numpy 数组，转完之后原数组就不再携带「我来自 CuPy」的命名空间信息。若先转换再取 `array_namespace`，`xp` 会变成 numpy 命名空间，最后 `return xp.asarray(y)` 就无法把结果转回 GPU，跨后端语义就坏了。所以必须先记下原始 `xp`。

**练习 2**：`_ScipyBackend.__ua_function__` 为什么要按 `_basic_backend` → `_realtransforms_backend` → `_fftlog_backend` 的顺序依次 `getattr`？

**参考答案**：因为三个 backend 模块的函数名互不重叠（FFT 系在 `_basic_backend`，DCT/DST 系在 `_realtransforms_backend`，fht/ifht 在 `_fftlog_backend`），按方法名查找即可唯一定位实现。任一模块都没有该方法时返回 `NotImplemented`，让 uarray 去尝试别的已注册后端。

---

### 4.2 DCT 四种 type：数学定义与 idct 的「类型翻转」

#### 4.2.1 概念说明

DCT 共有四种 type，差别在于**余弦波的采样位置**（即边界对称延拓的方式）。SciPy 的默认是 **type 2**——业界提到「the DCT」一般指的就是 DCT-II，JPEG 用的也是它。

四种 type 的关键关系（务必记住）：

- **type II 与 type III 互为逆**：未归一化的 DCT-III 是 DCT-II 的逆，差一个因子 \(2N\)。
- **type I 与 type IV 各自自逆**：未归一化时，DCT-I 的逆还是 DCT-I（差因子），DCT-IV 同理。

这就解释了源码里一个关键设计：**`idct` 并不是一个独立的算法，而是「调用同一个内核，但把 type 翻转」**。

#### 4.2.2 核心流程：四种 type 的数学定义

以下公式均取 `norm="backward"`（正变换不缩放），\(N\) 为输入长度（沿变换轴）。

**Type I**（要求 \(N > 1\)）：

\[
y_k = x_0 + (-1)^k x_{N-1} + 2 \sum_{n=1}^{N-2} x_n \cos\!\left( \frac{\pi k n}{N-1} \right)
\]

**Type II**（默认）：

\[
y_k = 2 \sum_{n=0}^{N-1} x_n \cos\!\left( \frac{\pi k(2n+1)}{2N} \right)
\]

**Type III**：

\[
y_k = x_0 + 2 \sum_{n=1}^{N-1} x_n \cos\!\left( \frac{\pi(2k+1)n}{2N} \right)
\]

**Type IV**：

\[
y_k = 2 \sum_{n=0}^{N-1} x_n \cos\!\left( \frac{\pi(2k+1)(2n+1)}{4N} \right)
\]

> 直觉上：type I 的余弦在「整数点」采样（对称延拓无平移）；type II 输入做了「半样本平移」、输出不移动；type III 反过来；type IV 输入输出都做「四分之一样本平移」。平移越多，基函数彼此越「错开」，矩阵越接近正交（见 4.4 节）。

#### 4.2.3 源码精读

**类型翻转是 `idct` 的核心。** `_r2r` 是 1-D DCT/DST 的统一实现。当 `forward=False`（即 `idct`）时，把 `type` 做 2↔3 互换，type 1 和 4 保持不变——这正对应「2/3 互逆、1/4 自逆」：

[_duccfft/realtransforms.py:8-28](_duccfft/realtransforms.py#L8-L28) — `_r2r` 的前半段：预处理 + 逆变换时的类型翻转：

```python
def _r2r(forward, transform, x, type=2, n=None, axis=-1, norm=None,
         overwrite_x=False, workers=None, orthogonalize=None):
    """Forward or backward 1-D DCT/DST ..."""
    tmp = _asfarray(x)
    overwrite_x = overwrite_x or _datacopied(tmp, x)
    norm = _normalization(norm, forward)
    workers = _workers(workers)

    if not forward:
        if type == 2:
            type = 3
        elif type == 3:
            type = 2
```

`_r2rn`（N-D 版）的翻转逻辑完全相同（[第 81-85 行](_duccfft/realtransforms.py#L81-L85)）。

**`functools.partial` 派生 8 个函数。** 真正算 DCT 的只有 `_r2r`（1-D）和 `_r2rn`（N-D）两个函数；`dct`/`idct`/`dctn`/`idctn`（以及 DST 的四个）全部由 `partial` 绑定前两个参数派生而来。第一个参数 `forward`（`True`/`False`）决定方向，第二个参数 `transform`（`pfft.dct`/`pfft.dst`）决定是余弦还是正弦：

[_duccfft/realtransforms.py:48-56](_duccfft/realtransforms.py#L48-L56) — 1-D 的四个函数全是 `partial` 派生，并手动设置 `__name__`：

```python
dct = functools.partial(_r2r, True, pfft.dct)
dct.__name__ = 'dct'  # pyrefly:ignore[missing-attribute]
idct = functools.partial(_r2r, False, pfft.dct)
idct.__name__ = 'idct'  # pyrefly:ignore[missing-attribute]

dst = functools.partial(_r2r, True, pfft.dst)
dst.__name__ = 'dst'  # pyrefly:ignore[missing-attribute]
idst = functools.partial(_r2r, False, pfft.dst)
idst.__name__ = 'idst'  # pyrefly:ignore[missing-attribute]
```

N-D 版同理（[第 101-109 行](_duccfft/realtransforms.py#L101-L109)），`dctn = functools.partial(_r2rn, True, pfft.dct)` 等。手动赋值 `__name__` 是因为 `partial` 对象默认的 `__name__` 不友好，显式设置后利于调试与文档系统识别。

**最后落到 C 扩展。** 实数路径的最终调用是：

[_duccfft/realtransforms.py:45](_duccfft/realtransforms.py#L45) — 实数输入的真正计算调用（`transform` 即 `pyduccfft.dct`）：

```python
    return transform(tmp, type, (axis,), norm, out, workers, orthogonalize)
```

**一个绝佳的「DCT-I = FFT」示例。** docstring 给出的例子最能说明 type I 的本质：对实、偶对称信号 `[4, 3, 5, 10, 5, 3]`，其 FFT 结果 `[30, -8, 6, -2, 6, -8]` 也是偶对称的；而 DCT-I 只取前半 `[4, 3, 5, 10]`，正好算出 FFT 结果的前半 `[30, -8, 6, -2]`：

[_realtransforms.py:414-419](_realtransforms.py#L414-L419) — docstring 示例：DCT-I 等价于实偶信号的 FFT：

```python
    >>> from scipy.fft import fft, dct
    >>> import numpy as np
    >>> fft(np.array([4., 3., 5., 10., 5., 3.])).real
    array([ 30.,  -8.,   6.,  -2.,   6.,  -8.])
    >>> dct(np.array([4., 3., 5., 10.]), 1)
    array([ 30.,  -8.,   6.,  -2.])
```

这也解释了为什么 DCT 能比 FFT「省一半」：它隐式利用了对称性，只算非冗余的一半。

#### 4.2.4 代码实践

**实践目标**：验证「idct 是类型翻转」这一设计，亲手看到 `idct(x, type=2)` 内部其实跑了 type 3。

**操作步骤**（源码阅读 + 数值验证）：

```python
import numpy as np
import scipy.fft as spfft

rng = np.random.default_rng(42)
x = rng.standard_normal(8)

# 1. idct(type=2) 应等于「type=3 的 dct 再做 backward 归一化」
#    直观验证：idct 是 dct 的逆
y = spfft.dct(x, type=2)
x_rec = spfft.idct(y, type=2)
print("idct 还原:", np.allclose(x, x_rec))           # 预期 True

# 2. 四种 type 各自正逆配对都应可逆
for t in (1, 2, 3, 4):
    xt = rng.standard_normal(8)
    err = np.max(np.abs(xt - spfft.idct(spfft.dct(xt, type=t), type=t)))
    print(f"type={t} 往返误差: {err:.2e}")            # 都应接近 0
```

**需要观察的现象**：四种 type 的往返误差都应在 \(10^{-15}\) 量级（浮点精度内）。

**预期结果**：`idct 还原: True`；四行误差均约 `1e-15`。

> 进阶观察（待本地验证）：在 REPL 里 `import scipy.fft._duccfft.realtransforms as rt; print(rt.idct.func, rt.idct.args)`，可看到 `idct` 的 `func` 是 `_r2r`、`args` 以 `False`（forward）开头，印证它是 `partial` 派生而非独立函数。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `idct` 对 type 1 和 type 4 **不做**类型翻转？

**参考答案**：因为 DCT-I 和 DCT-IV 在未归一化意义上各自自逆（DCT-I 的逆仍是 DCT-I，DCT-IV 同理，仅差一个常数因子）。翻转只在「互逆对」之间进行：2↔3。源码里 `if not forward:` 分支只处理 `type==2` 和 `type==3`，type 1/4 不进任何分支，原样保留。

**练习 2**：四种 type 中，哪一个的余弦采样点同时平移了输入和输出（即分子里同时出现 \((2k+1)\) 和 \((2n+1)\)）？

**参考答案**：type IV。其公式 \(y_k = 2\sum x_n \cos\!\big(\frac{\pi(2k+1)(2n+1)}{4N}\big)\) 同时含 \((2k+1)\)（输出半样本平移）和 \((2n+1)\)（输入半样本平移）。这种「双向半样本平移」使 DCT-IV 的矩阵天然接近正交（详见 4.4）。

---

### 4.3 norm 三模式：`_NORM_MAP` 与 `2 - inorm` 翻转

#### 4.3.1 概念说明

`norm` 控制缩放因子（\(1/N\) 或 \(1/\sqrt{N}\)）**放在正变换还是逆变换**。三种模式：

| `norm` | 正变换 `dct` | 逆变换 `idct` | 含义 |
|--------|------------|--------------|------|
| `"backward"`（默认） | 不缩放 | 除以 \(N\) | 缩放归到逆向（「反向」） |
| `"ortho"` | 除以 \(\sqrt{N}\) | 除以 \(\sqrt{N}\) | 正逆对称，同因子 |
| `"forward"` | 除以 \(N\) | 不缩放 | 缩放归到正向（「前向」） |

其中 \(N\) 是「逻辑长度」：type I 为 \(2(N_\text{轴}-1)\)，type II/III/IV 为 \(2N_\text{轴}\)（多轴时取各变换轴的乘积）。关键性质：**任意模式下，`idct(dct(x)) == x` 恒成立**——区别只在缩放归属。

#### 4.3.2 核心流程：一个整数编码同时表达「方向」与「归一化」

ducc 核心不认字符串，只认整数 `inorm`（0/1/2）。源码用一个绝妙的小技巧：**正逆方向用 `2 - inorm` 翻转**。

```text
用户传 norm 字符串
   │  _NORM_MAP:  None/"backward"→0, "ortho"→1, "forward"→2
   ▼
inorm = _NORM_MAP[norm]
   │  若是逆变换(forward=False)：inorm = 2 - inorm
   ▼
传给 pyduccfft 的整数 inorm（0=不缩放, 1=÷√N, 2=÷N）
```

为什么 `2 - inorm` 能工作？以 `norm="backward"` 为例：

- `dct`（forward=True）：`inorm = 0` → 不缩放 ✓
- `idct`（forward=False）：`inorm = 2 - 0 = 2` → 除以 \(N\) ✓

再看 `norm="forward"`：

- `dct`：`inorm = 2` → 除以 \(N\) ✓
- `idct`：`inorm = 2 - 2 = 0` → 不缩放 ✓

而 `norm="ortho"` 时 `inorm = 1`，`2 - 1 = 1` 不变，正逆都除以 \(\sqrt{N}\) ✓。

#### 4.3.3 源码精读

**映射表与翻转。** `_NORM_MAP` 把字符串/None 映射成整数；`_normalization` 在逆方向上做 `2 - inorm` 翻转：

[_duccfft/helper.py:181-192](_duccfft/helper.py#L181-L192) — 字符串到整数的映射 + 方向翻转：

```python
_NORM_MAP = {None: 0, 'backward': 0, 'ortho': 1, 'forward': 2}


def _normalization(norm, forward):
    """Returns the pyduccfft normalization mode from the norm argument"""
    try:
        inorm = _NORM_MAP[norm]
        return inorm if forward else (2 - inorm)
    except KeyError:
        raise ValueError(
            f'Invalid norm value {norm!r}, should '
            'be "backward", "ortho" or "forward"') from None
```

注意 `None` 与 `"backward"` 共享编码 `0`——这就是为什么「不传 `norm`」等价于「`norm="backward"`」。

**在 `_r2r` 中被调用。** `_normalization(norm, forward)` 的返回值最终作为 `norm` 传给 C 扩展：

[_duccfft/realtransforms.py:21](_duccfft/realtransforms.py#L21) — `_r2r` 调用 `_normalization`，把字符串换成整数：

```python
    norm = _normalization(norm, forward)
```

**docstring 里的官方说明。** 公共 `dct` 的 Notes 一节明确写了三种模式的缩放归属：

[_realtransforms.py:330-334](_realtransforms.py#L330-L334) — 三种 `norm` 的缩放规则文档：

```python
    For ``norm="backward"``, there is no scaling on `dct` and the `idct` is
    scaled by ``1/N`` where ``N`` is the "logical" size of the DCT. For
    ``norm="forward"`` the ``1/N`` normalization is applied to the forward
    `dct` instead and the `idct` is unnormalized.
```

> 这套 `2 - inorm` 翻转手法与 [u2-l1](u2-l1-complex-fft.md) 里复数 FFT 的归一化完全同源——`scipy.fft` 全家族共用同一套 `_normalization` 逻辑。实变换只是把 `forward` 的语义换成了「是否做类型翻转」。

#### 4.3.4 代码实践

**实践目标**：数值验证三种 `norm` 模式下正逆配对的可逆性，并观察 `ortho` 模式下正逆缩放因子相等。

**操作步骤**：

```python
import numpy as np
import scipy.fft as spfft

rng = np.random.default_rng(7)
x = rng.standard_normal(8)

for mode in ("backward", "ortho", "forward"):
    y = spfft.dct(x, norm=mode)
    x_rec = spfft.idct(y, norm=mode)
    # 正逆配对应当恒等
    print(f"{mode:9s} 往返 allclose: {np.allclose(x, x_rec)}")

# 观察 ortho 模式下 dct 与 idct 的「能量保持」（正交变换保 2-范数）
y_ortho = spfft.dct(x, norm="ortho")
print("||x||  =", np.linalg.norm(x))
print("||y||  =", np.linalg.norm(y_ortho))   # 应与 ||x|| 几乎相等（正交变换）
```

**需要观察的现象**：三种模式都打印 `allclose: True`；`ortho` 模式下 \(\|y\| \approx \|x\|\)（这是 4.4 节「正交化」的数值签名）。

**预期结果**：三行 `True`；`||x||` 与 `||y||` 数值几乎相同（差异在 \(10^{-15}\) 量级）。

#### 4.3.5 小练习与答案

**练习 1**：用户调用 `dct(x)`（不传 `norm`）。请问最终传给 C 扩展的 `inorm` 是多少？若是 `idct(x)` 呢？

**参考答案**：不传 `norm` 时 `norm=None`，`_NORM_MAP[None] = 0`。`dct` 是正变换（`forward=True`），`_normalization` 直接返回 `0`，即不缩放。`idct` 是逆变换（`forward=False`），返回 `2 - 0 = 2`，即除以 \(N\)。这与 `norm="backward"` 完全一致。

**练习 2**：若把 `_normalization` 里的 `2 - inorm` 改成 `inorm`（不翻转），会发生什么？

**参考答案**：逆变换将不再补上缩放因子。以 `norm="backward"` 为例，`idct` 的 `inorm` 会变成 `0`（不缩放），于是 `idct(dct(x))` 会比 `x` 大 \(N\) 倍，正逆不再互逆，可逆性被破坏。`2 - inorm` 正是让缩放因子在正逆之间正确「交接」的关键。

---

### 4.4 orthogonalize：正交化选项与「默认值由 C 层决定」

#### 4.4.1 概念说明

`orthogonalize` 是 SciPy 1.8 引入的关键字参数，控制是否对 DCT 的系数矩阵做**正交化修正**，使其满足 \(O^\top O = I\)（正交矩阵）。

为什么需要它？看 type II 的矩阵：即便配上 `norm="ortho"`（整体除以 \(\sqrt{N}\)），由于 DCT-II 的第一行（\(k=0\)）与其它行的「能量」不均，矩阵仍**不是**严格正交的。`orthogonalize=True` 通过额外修正首尾元素（乘/除 \(\sqrt{2}\)）把矩阵补成正交。

**默认行为（务必记住）**：

> `orthogonalize` 在 `norm="ortho"` 时**默认为 `True`**，其它模式下**默认为 `False`**。

并且——这个默认值**不是在 Python 层实现的，而是在 C 扩展层**根据 `inorm` 自动决定的。

#### 4.4.2 核心流程：每种 type 的正交化修正

正交化对四种 type 的具体修正（摘自 C 扩展 docstring）：

| Type | orthogonalize=True 时的额外操作 | 与 `norm="ortho"` 组合后 |
|------|-------------------------------|------------------------|
| I | 输入首尾乘 \(\sqrt{2}\)，输出首尾除 \(\sqrt{2}\) | 矩阵正交 |
| II | 输出第 0 个除以 \(\sqrt{2}\) | 矩阵正交 |
| III | 输入第 0 个乘 \(\sqrt{2}\) | 矩阵正交 |
| IV | 无操作（天然正交） | 已正交（差常数因子） |

`type IV` 的矩阵在配上 \(\sqrt{N}\) 缩放后**本身就已正交**，所以 `orthogonalize` 对它无效。

**默认值的传递链**：

```text
公共 API: orthogonalize=None   （未显式指定）
   ▼ 一路透传：_execute(orthogonalize=None) → _r2r(orthogonalize=None) → transform(..., orthogonalize=None)
   ▼
C 扩展 pyduccfft.dct(..., ortho_obj=None)
   │  bool ortho = (inorm==1);          ← 默认：仅当 ortho 归一化时为 True
   │  if (!ortho_obj.is_none())         ← 用户显式传了值就覆盖
   │      ortho = ortho_obj.cast<bool>();
   ▼
ducc0::dct(..., ortho, ...)   真正执行正交化与否
```

#### 4.4.3 源码精读

**默认值在 C 层决定——这是本节最重要的源码点。** `pyduccfft.dct` 的第二行 `bool ortho=inorm==1;` 意味着：当且仅当 `inorm==1`（即 `norm="ortho"`）时默认正交化；而 `ortho_obj`（Python 侧的 `orthogonalize`）非空时覆盖该默认：

[_duccfft/pyduccfft.cxx:258-268](_duccfft/pyduccfft.cxx#L258-L268) — C 扩展 `dct`：默认 `ortho` 由 `inorm==1` 决定，显式参数可覆盖：

```cpp
NpArr dct(const CNpArr &in, int type, const OptAxes &axes_,
  int inorm, const OptNpArr &out_, size_t nthreads, const py::object &ortho_obj)
  {
  bool ortho=inorm==1;
  if (!ortho_obj.is_none())
    ortho=ortho_obj.cast<bool>();

  if ((type<1) || (type>4)) throw std::invalid_argument("invalid DCT type");
  DISPATCH(in, f64, f32, flong, dct_internal, (in, axes_, type, inorm, out_,
    nthreads, ortho))
  }
```

因为 Python 公共 API 的默认是 `orthogonalize=None`，且 `_execute`、`_r2r` 都把它**原样透传**（不替换为布尔），所以到达 C 层时 `ortho_obj.is_none()` 为真，于是 `ortho` 取 `inorm==1`。这就是「`norm="ortho"` 时默认正交化、否则不」的实现根源——**没有任何 Python 代码写 `if norm=='ortho'`**。

**C 扩展 docstring 的权威说明。** 正交化对每种 type 的具体操作直接写在 C docstring 里：

[_duccfft/pyduccfft.cxx:542-548](_duccfft/pyduccfft.cxx#L542-L548) — C 层 docstring 列出各 type 的正交化步骤：

```cpp
    Making the transform orthogonal involves the following additional steps
    for every 1D sub-transform:
      Type 1 : multiply first and last input value by sqrt(2)
               divide first and last output value by sqrt(2)
      Type 2 : divide first output value by sqrt(2)
      Type 3 : multiply first input value by sqrt(2)
      Type 4 : nothing
```

同时它给出了 `inorm` 与「逻辑长度」\(N\) 的关系（[第 534-541 行](_duccfft/pyduccfft.cxx#L534-L541)）：type 1 的 \(n_i = 2(\text{轴长}-1)\)，type 2/3/4 的 \(n_i = 2\cdot\text{轴长}\)，\(N\) 为各变换轴 \(n_i\) 之积；`inorm=1` 除以 \(\sqrt{N}\)、`inorm=2` 除以 \(N\)。

**公共 docstring 里的默认值声明。** 公共 `dct` 的参数说明明确写了默认行为：

[_realtransforms.py:301-305](_realtransforms.py#L301-L305) — `orthogonalize` 参数说明，标注默认值与引入版本：

```python
    orthogonalize : bool, optional
        Whether to use the orthogonalized DCT variant (see Notes).
        Defaults to ``True`` when ``norm="ortho"`` and ``False`` otherwise.

        .. versionadded:: 1.8.0
```

**一个重要警告。** 正交化会**破坏 DCT 与直接傅里叶变换的对应关系**。docstring 明确警告：对 type 1/2/3，`norm="ortho"` 默认开启的正交化会让结果偏离「直接 DFT」语义，若需要恢复对应关系须显式传 `orthogonalize=False`：

[_realtransforms.py:321-324](_realtransforms.py#L321-L324) — 警告：`norm="ortho"` 的正交化会破坏与 DFT 的对应：

```python
    .. warning:: For ``type in {1, 2, 3}``, ``norm="ortho"`` breaks the direct
                 correspondence with the direct Fourier transform. To recover
                 it you must specify ``orthogonalize=False``.
```

#### 4.4.4 代码实践

**实践目标**：数值验证正交化是否真的让 DCT 矩阵满足 \(O^\top O = I\)，并对比「默认开启」与「显式关闭」的差别。

**操作步骤**：

```python
import numpy as np
import scipy.fft as spfft

N = 8
e = np.eye(N)

# 构造 type-II DCT 的系数矩阵：对单位矩阵的每一列做 dct
# ortho + 默认正交化
O_ortho = spfft.dct(e, type=2, norm="ortho")               # orthogonalize 默认 True
# ortho 但显式关闭正交化
O_noorth = spfft.dct(e, type=2, norm="ortho", orthogonalize=False)

print("默认正交化  O.T @ O 是否= I :", np.allclose(O_ortho.T @ O_ortho, np.eye(N)))
print("关闭正交化  O.T @ O 是否= I :", np.allclose(O_noorth.T @ O_noorth, np.eye(N)))

# 观察默认正交化对第 0 行的修正：除以 sqrt(2)
print("首行比值 (默认/关闭):", O_ortho[0, 0] / O_noorth[0, 0])   # 预期 ≈ 1/sqrt(2) ≈ 0.7071
```

**需要观察的现象**：

1. 默认正交化时，\(O^\top O = I\) 成立（`True`）。
2. 关闭正交化时，\(O^\top O \neq I\)（`False`）——首行能量偏大。
3. 首行比值约为 \(1/\sqrt{2} \approx 0.7071\)，对应「type II 把输出第 0 个除以 \(\sqrt{2}\)」。

**预期结果**：依次为 `True`、`False`、`0.7071...`。

> 进阶（待本地验证）：把 `type=2` 换成 `type=4`，会发现「默认」与「关闭」结果完全相同——因为 type IV 天然正交，`orthogonalize` 对它无效。

#### 4.4.5 小练习与答案

**练习 1**：用户调用 `dct(x, norm="backward")`（不传 `orthogonalize`）。最终 C 层的 `ortho` 是 `True` 还是 `False`？为什么？整个判断过程中有任何 Python `if` 在比较 `norm=="ortho"` 吗？

**参考答案**：`False`。因为 `norm="backward"` 对应 `inorm=0`，C 层 `bool ortho=inorm==1` 即 `0==1` 为 `False`；又因用户没传 `orthogonalize`，`ortho_obj` 为 `None`，不会进入覆盖分支。整个过程**没有任何 Python 代码**比较 `norm=="ortho"`——默认行为完全由 C 层 `inorm==1` 这一行决定，Python 只负责把 `None` 透传到底。

**练习 2**：为什么 docstring 要警告「`norm="ortho"` 会破坏与直接傅里叶变换的对应关系」？怎样恢复？

**参考答案**：因为「直接 DFT」对应的 DCT 定义（如 type II 的标准公式）本身不带正交化修正；而 `norm="ortho"` 默认开启了 `orthogonalize=True`，额外对首/尾元素乘除 \(\sqrt{2}\)，改变了系数，于是结果不再严格等于「DFT 的实偶对称子集」。要恢复对应关系，需显式传 `orthogonalize=False`，此时只保留 \(\sqrt{N}\) 整体缩放、不做正交化修正。

---

## 5. 综合实践：用 DCT-II 做「能量压缩」实验

把本讲四个模块串起来，完成一个经典应用：**用 DCT 做信号/图像的低通压缩**，体会 type、norm、orthogonalize 三者的协作。

**任务**：对一段实信号做 DCT-II，丢弃高频系数后再 `idct` 重建，观察能量保留比与重建误差，并对比 `orthogonalize` 开/关。

```python
import numpy as np
import scipy.fft as spfft

rng = np.random.default_rng(123)

# 1. 造一段「低频为主」的实信号：一个慢正弦 + 少量噪声
N = 256
n = np.arange(N)
x = np.cos(2 * np.pi * n / N * 3) + 0.1 * rng.standard_normal(N)

# 2. 用 type=2 + ortho（默认正交化）变换
for orth in (True, False):
    y = spfft.dct(x, type=2, norm="ortho", orthogonalize=orth)
    # 3. 只保留前 K 个低频系数（频域能量压缩）
    K = 32
    y_compressed = y.copy()
    y_compressed[K:] = 0
    x_rec = spfft.idct(y_compressed, type=2, norm="ortho", orthogonalize=orth)

    # 4. 能量保留比（ortho 下 DCT 保范数，可直接用系数能量）
    energy_kept = np.sum(y_compressed[:K]**2) / np.sum(y**2)
    rel_err = np.linalg.norm(x - x_rec) / np.linalg.norm(x)
    print(f"orthogonalize={orth}: 保留前 {K}/{N} 系数, "
          f"频域能量保留 {energy_kept*100:.1f}%, 时域相对误差 {rel_err*100:.2f}%")

# 5. 验证 ortho+orthogonalize 下变换矩阵的正交性（4.4 节结论）
O = spfft.dct(np.eye(N), type=2, norm="ortho")   # 默认 orthogonalize=True
print("DCT-II(ortho) 矩阵正交:", np.allclose(O.T @ O, np.eye(N)))
```

**需要观察的现象**：

- 只保留 \(32/256 = 12.5\%\) 的系数，频域能量保留率应非常高（>95%，因为信号本就低频），时域相对误差很小（个位数百分比）——这就是 DCT 用于 JPEG/MPEG 压缩的原理。
- `orthogonalize=True` 与 `False` 重建的**时域相对误差应当相同**（因为正/逆都用了同一个 `orth` 值，正交化在正逆配对中相互抵消）；但「频域能量保留率」在 `orthogonalize=True` 时更有意义，因为此时系数能量直接对应信号能量（正交变换保范数）。
- 最后一行打印 `True`，呼应 4.4 节的数值验证。

**预期结果**：两次循环的时域相对误差接近相等且较小；能量保留率高；矩阵正交性为 `True`。具体百分比待本地验证（依赖随机噪声）。

> 这个实验把本讲的四件事连成一线：`dct`/`idct` 的正逆配对（4.1）、type II 的选择（4.2）、`norm="ortho"` 的对称缩放（4.3）、以及 `orthogonalize` 带来的范数保持（4.4）。

## 6. 本讲小结

- `dct`/`idct`/`dctn`/`idctn` 的函数体只是 `return (Dispatchable(x, np.ndarray),)` 的分派声明；真正计算在四层链路深处，最终落到 C 扩展 `pyduccfft.dct`。
- DCT 四种 type 差在余弦采样位置（边界对称延拓方式），默认 type 2；`idct` 不是独立算法，而是复用同一内核、对 type 做 2↔3 翻转（type 1/4 自逆）。
- `norm` 三模式 `backward`/`ortho`/`forward` 由 `_NORM_MAP` 映射成整数 `inorm`，并用 `2 - inorm` 在正逆方向间翻转，使任意模式下 `idct(dct(x)) == x`。
- `orthogonalize` 通过修正首尾元素（乘/除 \(\sqrt{2}\)）把 DCT 矩阵补成正交（\(O^\top O = I\)）；type IV 天然正交故无需修正。
- **关键源码细节**：`orthogonalize` 的默认值（`norm="ortho"` 时为 `True`，否则 `False`）**不是 Python 判断的**，而是由 C 扩展 `bool ortho=inorm==1` 一行决定，Python 仅把 `None` 透传到底。
- 后端 `_execute` 用 `array_namespace` → `np.asarray` → `xp.asarray` 的往返，把任意数组库的输入桥接到 ducc 核心再转回，使 DCT 天然支持数组标准（但被 `cpu_only` 限制在 CPU）。

## 7. 下一步学习建议

- **[u3-l2 离散正弦变换 dst/dstn](u3-l2-dst.md)**：DST 与 DCT 共享同一份 `_r2r` / `_r2rn` 与 `partial` 派生手法，差别只在边界条件（奇对称）与 type 的自逆/互逆规则。读完本讲再读 DST 会非常轻松。
- **[u3-l3 实变换后端 _execute](u3-l3-realtransforms-backend.md)**：深入 `_execute` 与 `array_namespace` 的桥接机制，理解实变换为何被标记为 `cpu_only`，以及它和 `_basic_backend` 的异同。
- **若对正交变换的数学意兴未尽的读者**：可对照本讲的 \(O^\top O = I\) 验证，去读 [_duccfft/pyduccfft.cxx](_duccfft/pyduccfft.cxx) 里 `norm_fct` 与 `ducc0::dct` 的调用，看 C++ 层如何把 `inorm`、`ortho`、`type` 三者组合成最终的缩放与采样方案。
