# u3-l3 实变换后端：`_realtransforms_backend._execute`

## 1. 本讲目标

学完本讲，你应该能够：

- 说清楚 `_realtransforms_backend.py` 里那个只有几行的 `_execute` 函数，为什么能同时服务 8 个实变换函数（`dct/idct/dst/idst` 及其 `n` 变体）。
- 理解 `array_namespace` 在实变换里扮演的「数组出身识别器」角色，以及它在默认模式与 `SCIPY_ARRAY_API` 模式下的截然不同的行为。
- 把实变换后端与 `_basic_backend.py` 放在一起对比，讲出它们「同在四层架构的第三层、最终都落到 ducc」，但在**路由策略**上为何一个简单、一个复杂。

本讲只剖析一个文件的核心，外加两个对照文件，理解「后端层如何把任意数组接进来、算完、再原样送回去」这件事。

## 2. 前置知识

本讲承接 u3-l1。在那里我们建立了实变换的**四层调用链**：

```
公共 API（_realtransforms.py）
   └─ uarray 分派（domain = "numpy.scipy.fft"）
        └─ 后端层（_realtransforms_backend.py）   ← 本讲主战场
             └─ 计算核心（_duccfft/realtransforms.py 的 _r2r/_r2rn）
                  └─ C 扩展（pyduccfft.dct / pyduccfft.dst）
```

你需要记住的几个 u3-l1 结论：

- 8 个公共函数体（`_realtransforms.py`）只写 `return (Dispatchable(x, np.ndarray),)`，是**分派协议声明**，不含计算。
- ducc 内核 `_r2r` / `_r2rn` 只认 **numpy 数组**，靠 `functools.partial` 派生出 8 个函数。
- `_backend.py` 的 `_ScipyBackend.__ua_function__` 会用方法名（如 `'dctn'`）在后端层里查找同名实现。

一个新概念先放在这里：**数组命名空间（array namespace）**。不同数组库（NumPy、CuPy、PyTorch、JAX）各自是一个「命名空间」模块，里面都有 `asarray`、`fft` 等函数。给定一个数组 `x`，`array_namespace(x)` 能告诉我们它属于哪个库——这正是后端层把结果「原样送回去」所必需的信息。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
|------|------|-----------|
| [_realtransforms_backend.py](_realtransforms_backend.py) | **后端层（实变换）** | `_execute` 封装、8 个 thin wrapper、`array_namespace` 桥接 |
| [_duccfft/__init__.py](_duccfft/__init__.py) | 计算核心子包入口 | `from .realtransforms import *` 暴露出 `dctn` 等 |
| [_duccfft/realtransforms.py](_duccfft/realtransforms.py) | 计算核心实现 | `_r2r`/`_r2rn` 的位置参数签名，印证 `_execute` 透传正确 |
| [_basic_backend.py](_basic_backend.py) | 后端层（FFT，对照） | `_execute_1D` 的三分支路由，与实变换对照 |
| [_backend.py](_backend.py) | 分派后端 | `_ScipyBackend.__ua_function__` 如何按方法名找到本文件 |

## 4. 核心概念与源码讲解

### 4.1 `_execute`：八个函数共享的统一封装

#### 4.1.1 概念说明

打开 [_realtransforms_backend.py](_realtransforms_backend.py)，你会发现 8 个函数（`dct/idct/dst/idst` 与 `dctn/idctn/dstn/idstn`）长得几乎一模一样：它们都只是把参数原封不动地转交给一个叫 `_execute` 的函数，唯一不同的是第一个参数——传进去的**是哪个 ducc 内核**。

问题来了：ducc 内核（`_duccfft.dctn` 等，本质是 `_r2rn` 的 `partial`）**只吃 numpy 数组**，但 `scipy.fft.dct` 的文档承诺 `x : array_like`，也就是 Python 列表、numpy 数组，乃至（开启 `SCIPY_ARRAY_API` 后）CuPy / PyTorch 数组都得能吃下去。谁来弥合「内核只认 numpy」与「用户可喂任意数组」之间的鸿沟？

答案就是这个 `_execute`。它是一个**适配器（adapter）**，做三件事：

1. 先记住输入数组「出身」于哪个命名空间（`xp`）。
2. 把输入转成纯 numpy 数组交给 ducc 内核计算。
3. 把 numpy 结果再转回原来的命名空间返回。

这是一种典型的**高阶函数（higher-order function）**写法：把「用哪个内核」当作参数 `duccfft_func` 传进来，于是「输入/输出的数组类型转换」这套样板代码只写一次，被 8 个变换复用，避免了 8 份几乎相同的拷贝。

#### 4.1.2 核心流程

`_execute` 的执行流程可以画成一条直线：

```
_execute(duccfft_func, x, type, s, axes, norm, overwrite_x, workers, orthogonalize)
│
├─ ① xp  = array_namespace(x)      # 记下输入的「出身」命名空间
├─ ② x   = np.asarray(x)           # 转成 numpy 数组（列表、cupy 等都行）
├─ ③ y   = duccfft_func(x, ...)    # 调 ducc 内核，y 是 numpy 数组
└─ ④ return xp.asarray(y)          # 把结果转回原命名空间
```

注意第 ③ 步：`duccfft_func` 是作为**位置参数透传**调用的，`_execute` 并不关心 `type/s/axes/norm` 的具体含义，只负责搬运。这也解释了为什么 1-D 的 `dct`（参数叫 `n, axis`）和 N-D 的 `dctn`（参数叫 `s, axes`）能共用同一个 `_execute`——它们在 `_execute` 里都被塞进名叫 `s, axes` 的形参槽位，再原样传给内核，而内核的位置参数恰好与之一一对应。

#### 4.1.3 源码精读

先看 `_execute` 本体——整个文件的核心，只有 8 行：

[\_realtransforms_backend.py:L8-L15](_realtransforms_backend.py#L8-L15) 实现了上面三步流程。注意它**把 `duccfft_func` 当作第一个参数**传入，这是高阶函数的关键。

关键三行：

- [_realtransforms_backend.py:L10](_realtransforms_backend.py#L10) `xp = array_namespace(x)`：先于任何转换，抢在「输入还是原貌」时记录它的命名空间。
- [_realtransforms_backend.py:L11](_realtransforms_backend.py#L11) `x = np.asarray(x)`：此时 `x` 已变成 numpy 数组，原命名空间信息已丢失（所以才要先做第 10 行）。
- [_realtransforms_backend.py:L12-L14](_realtransforms_backend.py#L12-L14) 把剩余参数透传给内核，`overwrite_x/workers/orthogonalize` 用关键字传。
- [_realtransforms_backend.py:L15](_realtransforms_backend.py#L15) `return xp.asarray(y)`：用第 ① 步记下的 `xp` 把 numpy 结果转回去。

再看调用方。N-D 版本的 `dctn` 把 `_duccfft.dctn` 作为内核传入：

[\_realtransforms_backend.py:L18-L21](_realtransforms_backend.py#L18-L21) `dctn` 把 `(s, axes)` 喂给 `_execute`。

而 1-D 版本的 `dct` 把 `_duccfft.dct` 作为内核传入，并且把 `(n, axis)` 塞进 `_execute` 的 `(s, axes)` 槽位：

[\_realtransforms_backend.py:L42-L45](_realtransforms_backend.py#L42-L45) 注意这里 `n, axis` 对应了 `_execute` 形参里的 `s, axes`——位置对齐，名字只是借用。

文件顶部的 `__all__` 列出了这 8 个对外名字：

[\_realtransforms_backend.py:L5](_realtransforms_backend.py#L5) `_ScipyBackend` 会用这些名字去 `getattr` 找实现。

为了印证「内核只认位置参数且与 `_execute` 透传对齐」，看 ducc 内核 `_r2rn` 的签名：

[\_duccfft/realtransforms.py:L59-L69](_duccfft/realtransforms.py#L59-L69) `partial` 已绑定了前两个参数 `(forward, transform)`，所以 `_duccfft.dctn(x, type, s, axes, norm, ...)` 正好依次落到 `x, type=, s=, axes=, norm=`。1-D 内核 `_r2r` 同理，只是 `s/axes` 槽位对应 `n/axis`。

#### 4.1.4 代码实践

> 实践目标：亲手复刻 `_execute` 的三步流程，体会「记录出身 → 转 numpy → 算 → 转回」的适配器模式。

下面的**示例代码**用到了私有子包 `_duccfft` 与 `scipy._lib._array_api`，仅用于学习理解，不要在生产代码里依赖私有 API。

```python
# 示例代码：复刻 _realtransforms_backend._execute 的流程
import numpy as np
from scipy.fft._duccfft import dctn as ducc_dctn        # 私有内核：只认 numpy
from scipy._lib._array_api import array_namespace

def my_execute(x, type=2, s=None, axes=None, norm=None):
    # ① 记录输入的「出身」命名空间（必须在转换前做）
    xp = array_namespace(x)
    # ② 转成 numpy 数组，ducc 内核才能吃
    arr = np.asarray(x)
    # ③ 调内核，得到 numpy 结果
    y = ducc_dctn(arr, type, s, axes, norm)
    # ④ 转回原命名空间
    return xp.asarray(y)

if __name__ == "__main__":
    list_in = [1.0, 2.0, 3.0, 4.0]          # Python 列表
    np_in   = np.array(list_in)             # numpy 数组

    out_from_list = my_execute(list_in)
    out_from_np   = my_execute(np_in)

    # 观察输出类型：默认模式下 array_namespace 总是返回 numpy 命名空间，
    # 所以 xp.asarray 就是 np.asarray，两者输出都是 numpy.ndarray
    print(type(out_from_list))              # 预期：<class 'numpy.ndarray'>
    print(type(out_from_np))                # 预期：<class 'numpy.ndarray'>

    # 数值应当与公共 API 一致
    from scipy.fft import dctn
    print(np.allclose(out_from_np, dctn(np_in)))   # 预期：True
```

**操作步骤**：

1. 把上面脚本存为 `mock_execute.py` 并运行 `python mock_execute.py`。
2. 把第 ① 步（`xp = array_namespace(x)`）注释掉，并在第 ④ 步改用 `np.asarray(y)`，重新运行——对 numpy 输入结果不变（因为默认模式下 `xp` 本来就是 numpy）。
3. 观察现象、对比输出类型。

**预期结果**：

- 两次输出的 `type(...)` 都是 `numpy.ndarray`。
- `np.allclose` 比较为 `True`。

> 注意：默认模式下 `array_namespace` 无论如何都返回 numpy 命名空间，所以第 ④ 步对 numpy/列表输入是「无操作（no-op）」。要看到 `xp.asarray` 真正起作用（把 CPU 上的 numpy 数组搬回 CuPy/PyTorch），需要设置环境变量 `SCIPY_ARRAY_API=1` 并传入非 numpy 数组——这一点 4.2 会详细解释，具体运行结果**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `xp = array_namespace(x)` 必须放在 `x = np.asarray(x)` **之前**，而不能放到后面？

**参考答案**：因为 `np.asarray(x)` 之后，`x` 已变成 numpy 数组，原本「它是列表 / CuPy / PyTorch」的信息就丢失了，再调 `array_namespace` 只会得到 numpy 命名空间，第 ④ 步就再也无法把结果转回原来的库。先记录、后转换，是适配器的铁律。

**练习 2**：`_execute` 用了哪种设计手法，使它能用一个函数体服务 8 个不同的变换？

**参考答案**：高阶函数——把「用哪个内核」作为参数 `duccfft_func` 传进来。8 个 wrapper 各自绑定不同的内核（`_duccfft.dct / idct / dst / idst` 及 n 变体），共用同一套「记录出身 → 转 numpy → 调用 → 转回」的样板代码。

---

### 4.2 `array_namespace`：识别「数组出身」的桥接器

#### 4.2.1 概念说明

`array_namespace` 来自 `scipy._lib._array_api`（它再从 `scipy._lib._array_api_override` 转出）。给定一个数组 `x`，它返回 `x` 所属数组库的**命名空间模块**：

- numpy 数组 → 返回 numpy 命名空间（`np`）；
- CuPy 数组 → 返回 cupy 命名空间；
- PyTorch 张量 → 返回 torch 命名空间；
- Python 列表 → 返回 numpy 命名空间（列表被当作「可转成 numpy 的 array-like」）。

它是 [Python Array API 标准](https://data-apis.org/array-api/) 的核心抽象之一：不同数组库只要遵守同一套接口（都有 `asarray`、`fft`、`conj`……），代码就能在它们之间「换库不换逻辑」。

在 `_execute` 里，`array_namespace` 的角色是**桥接器**：它是函数知道「结果该用什么库重建」的唯一线索。没有它，第 ④ 步的 `xp.asarray(y)` 就无从下手——你不知道该用 `cupy.asarray` 还是 `torch.asarray`。

#### 4.2.2 核心流程

这里有一个**极易踩坑、但理解了就豁然开朗**的关键细节，源自 `array_namespace` 的实现：

```
array_namespace(x) 的行为分两种模式：

[默认模式] SCIPY_ARRAY_API 未设置（绝大多数用户）
   └─ 直接返回 numpy 命名空间，跳过一切检查
      ⇒ 对任何输入，xp 永远是 numpy
      ⇒ 第 ④ 步 xp.asarray(y) == np.asarray(y)，对 numpy 结果是 no-op

[数组标准模式] SCIPY_ARRAY_API 已设置
   └─ 真正检查 x 的类型，返回真实命名空间（cupy / torch / jax / numpy）
      ⇒ 第 ④ 步才会真正把 numpy 结果「搬回」原库
```

也就是说，**默认模式下 `_execute` 的桥接其实是「隐形的」**：`xp` 恒为 numpy，三步里第 ①④ 步对结果几乎没有影响，真正干活的只有第 ②③ 步（转 numpy + 调 ducc）。只有显式开启 `SCIPY_ARRAY_API=1`，`array_namespace` 才名副其实地「识别出身」，桥接才显形。

这种「双模式」是 scipy 兼容数组标准、又不给普通用户增加开销的折中：默认快路径零成本；需要异构数组时再开启。

#### 4.2.3 源码精读

后端文件第 1 行就导入了它：

[\_realtransforms_backend.py:L1-L3](_realtransforms_backend.py#L1-L3) `array_namespace` 与 numpy、`_duccfft` 一起被引入。

`_execute` 中两处用到 `array_namespace` / `xp`：

[\_realtransforms_backend.py:L10](_realtransforms_backend.py#L10) 记录命名空间（必须在 `np.asarray` 之前）。

[\_realtransforms_backend.py:L15](_realtransforms_backend.py#L15) 用它把结果转回原库。

`array_namespace` 的「默认模式直接返回 numpy」这一关键行为，定义在它的实现里：

[\_array_api_override.py:L111-L113](_array_api_override.py#L111-L113) 当全局开关 `SCIPY_ARRAY_API` 为假时，直接返回 numpy 兼容命名空间，跳过所有合规检查——这正是默认模式「桥接隐形」的根源。

而开启后的行为，在它的文档字符串里举例说明（`array_namespace([1, 2])` 返回 numpy）：

[\_array_api_override.py:L107-L109](_array_api_override.py#L107-L109) 列表被当作可转 numpy 的 array-like，返回 numpy 命名空间。

> 说明：上面两处 `scipy/_lib/_array_api_override.py` 的引用是为了讲清 `array_namespace` 的实现机制；该文件位于 `scipy/fft` 之外，超出本子包的永久链接 base，故以相对仓库路径的文字形式给出，行号已对齐当前 HEAD。

#### 4.2.4 代码实践

> 实践目标：用实验看清 `array_namespace` 在两种模式下的不同返回值。

```python
# 示例代码：观察 array_namespace 的返回
import os
import numpy as np
from scipy._lib._array_api import array_namespace

# 默认模式（未设 SCIPY_ARRAY_API）
xp_np   = array_namespace(np.array([1.0, 2.0]))
xp_list = array_namespace([1.0, 2.0])
print("numpy 输入 ->", xp_np.__name__)     # 预期：numpy
print("list  输入 ->", xp_list.__name__)   # 预期：numpy（默认模式总返回 numpy）
```

**操作步骤**：

1. 直接运行上面的脚本，记录两次 `__name__`。
2. 在运行前设置环境变量再跑一次：`SCIPY_ARRAY_API=1 python script.py`，观察 numpy 输入时返回是否仍为 numpy。
3.（可选，需安装 CuPy 或 PyTorch）在 `SCIPY_ARRAY_API=1` 下，构造一个 `cupy.asarray([1.0, 2.0])` 或 `torch.tensor([1.0, 2.0])`，打印 `array_namespace(...).__name__`。

**预期结果**：

- 默认模式：两次都打印 `numpy`。
- `SCIPY_ARRAY_API=1` 下：numpy 输入仍为 `numpy`；非 numpy 输入应打印对应库名（`cupy` / `torch` / `jax.numpy` 等），具体取决于你安装的库——**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：默认模式下，把 `_execute` 的第 ④ 步 `xp.asarray(y)` 直接改成 `return y`，对普通 numpy 用户有没有影响？

**参考答案**：没有影响。默认模式下 `xp` 恒为 numpy，`xp.asarray(y)` 等价于 `np.asarray(y)`，而 `y` 本就是 numpy 数组，是 no-op。但若开启 `SCIPY_ARRAY_API` 并传入 CuPy 数组，这一改就会让本该返回 CuPy 数组的结果变成 numpy 数组，类型泄漏。

**练习 2**：为什么 `array_namespace([1, 2])` 会返回 numpy 而不是报错？

**参考答案**：列表属于「可转成 numpy 的 array-like」，`array_namespace` 在校验类型时把 `list/tuple` 归类为 array-like，并最终用 numpy 兜底（见 `array_namespace` 文档与实现）。这正是 `scipy.fft.dct` 能接受 Python 列表的原因。

---

### 4.3 实变换后端 vs `_basic_backend`：同与异

#### 4.3.1 概念说明

`_realtransforms_backend.py` 与 [_basic_backend.py](_basic_backend.py) 是**同层的两个兄弟模块**：都在四层架构的「后端层」，都被 [_backend.py](_backend.py) 的 `_ScipyBackend.__ua_function__` 用方法名查找，最终都调用 `_duccfft` 内核。

[_backend.py:L20-L29](_backend.py#L20-L29) `_ScipyBackend` 按 `method.__name__` 依次在 `_basic_backend`、`_realtransforms_backend`、`_fftlog_backend` 里 `getattr` 找实现，找不到才返回 `NotImplemented`。

但它们的**路由策略**差别很大。理解这个差别，就理解了为什么实变换后端能写得这么简单。

#### 4.3.2 核心流程

二者的「同」与「异」可以用一张表概括：

| 维度 | `_realtransforms_backend._execute` | `_basic_backend._execute_1D / _execute_nD` |
|------|------------------------------------|--------------------------------------------|
| 入口 | 单一 `_execute` 统一封装 | `_execute_1D` / `_execute_nD` 两个 |
| numpy 快速路径 | 无显式判断（默认模式 `xp` 总是 numpy，等价直连） | 显式 `is_numpy(xp)` 分支，直连 ducc |
| 尝试数组库自带实现 | **从不**（无 `hasattr(xp, 'fft')` 分支） | **会**：`hasattr(xp, 'fft')` 时优先用 `xp.fft.*` |
| 找不到时回退 | （不需要，直接走 numpy+ducc） | 转 numpy → ducc → 再 `xp.asarray` 转回 |
| `workers` / `plan` | 透传给 ducc，全程可用 | 仅 numpy 路径可用；非 numpy 路径 `_validate_fft_args` 直接拒绝 |
| 复数输入处理 | 在内核 `_r2r/_r2rn` 内用 `np.iscomplexobj` 分离实虚 | 后端层用 `complex_funcs` 集合 + `try/except` 兜底 |
| 是否 `cpu_only` | 是（公共 API 标注 `@xp_capabilities(cpu_only=True)`） | 否（FFT 优先走原生 `xp.fft`，可上 GPU） |

**最关键的「异」**只有一句话：

- FFT 族有**可移植的原生实现**——数组标准定义了 `xp.fft.fft`，CuPy/PyTorch 都有，所以 `_basic_backend` 优先把活儿交给目标库自己干（GPU 上还能跑得更快）。
- 实变换族**没有可移植的原生实现**——数组标准里**根本没有 `xp.dct` / `xp.dst`**（连 `numpy.fft` 都没有），所以无论什么库的输入，实变换后端都只能「拉回 CPU 上的 numpy，用 ducc 算完，再送回去」。

这也正是 `_realtransforms.py` 里每个函数都标注 `@xp_capabilities(cpu_only=True)` 的根本原因（详见 u6-l2）：实变换注定要在 CPU 上完成，没有「上 GPU」的可能。

#### 4.3.3 源码精读

对照 `_basic_backend._execute_1D` 的三分支结构：

[\_basic_backend.py:L27-L49](_basic_backend.py#L27-L49) 它有：(1) `is_numpy(xp)` 直连 ducc； (2) `hasattr(xp, 'fft')` 时调用 `xp_func`； (3) 都不行才回退到「转 numpy → ducc → 转回」。其中第 (2) 分支用到了 `complex_funcs` 集合做复数兜底：

[\_basic_backend.py:L19](_basic_backend.py#L19) `complex_funcs` 列出那些「数组库实现可能要求复数输入」的函数名。

而实变换的 `_execute` 完全没有这些分支：

[\_realtransforms_backend.py:L8-L15](_realtransforms_backend.py#L8-L15) 始终是「记录出身 → 转 numpy → 调 ducc → 转回」一条路，没有 `is_numpy` 判断、没有 `xp.fft` 尝试、没有 `complex_funcs`。因为没有任何 `xp.dct` 可用，复杂的多分支路由在这里没有意义。

同源的「同」也很清晰：两者都把 `_duccfft` 的内核函数当参数传入，都以「位置透传 + 关键字补全」的方式调用内核。

#### 4.3.4 代码实践

> 实践目标：用阅读 + 断言的方式，验证「实变换始终经 numpy，FFT 在 numpy 输入下也经 ducc」这一同构关系。

**操作步骤**：

1. 阅读 [_basic_backend.py:L30-L33](_basic_backend.py#L30-L33) 与 [_realtransforms_backend.py:L11-L14](_realtransforms_backend.py#L11-L14)，确认在**默认模式**下两者走的其实是同一条「numpy → ducc」路径（因为默认 `xp` 总是 numpy，`is_numpy(xp)` 恒真）。
2. 写一段断言代码（**示例代码**）：

```python
# 示例代码：对照两种变换都经 ducc（默认模式）
import numpy as np
from scipy.fft import dctn, fftn

x = np.arange(16.0).reshape(4, 4)

# 实变换：idctn(dctn(x)) == x（ducc 计算）
print(np.allclose(x, dctn(np.asarray(dctn(x)))))   # 预期：True（注意 norm 默认）

# FFT：ifftn(fftn(x)) == x（同样 ducc 计算）
print(np.allclose(x, fftn(np.asarray(ifftn(np.asarray(fftn(x)))))))  # 预期：True
```

3. 思考：在默认模式下，能否仅凭「调用是否经 numpy」来区分这两种后端？（答案：不能，两者都经 numpy；真正的区别在 `SCIPY_ARRAY_API` 开启后对非 numpy 输入的处理。）

**预期结果**：两条 `allclose` 均为 `True`（FFT 那条需把 `ifftn(fftn(x))` 当整体，去掉中间多余的 `fftn`；上面写法仅示意链路，实际验证用 `np.allclose(x, ifftn(fftn(x)))` 即可——**待本地验证**精确写法）。

#### 4.3.5 小练习与答案

**练习 1**：为什么实变换后端不像 `_basic_backend` 那样写 `is_numpy` 快速路径和 `xp.fft` 分支？

**参考答案**：因为数组标准没有定义 `dct`/`dst`，没有任何目标库的 `xp.fft` 能干这活儿。所以无论输入来自哪个库，实变换都必须拉回 numpy 用 ducc 算。多分支路由没有可分流的出口，写出来也只是死代码，于是后端选择最简结构。

**练习 2**：在默认模式下（`SCIPY_ARRAY_API` 未设），`_basic_backend._execute_1D` 会走 `is_numpy` 分支吗？这对理解两种后端「其实很相似」有何帮助？

**参考答案**：会。默认模式下 `array_namespace` 总返回 numpy，`is_numpy(xp)` 恒为真，所以 FFT 在默认模式下同样走「numpy → ducc」直连路径，和实变换后端实质同构。两种后端的差异只在开启数组标准、且输入为非 numpy 数组时才显现——那时 FFT 优先用原生 `xp.fft`，实变换则被迫 round-trip 经 CPU。

---

## 5. 综合实践

把本讲的三块知识串起来，完成下面这个「自造一个简化版实变换后端」的小任务：

> **任务**：实现一个 `my_real_backend` 模块，它暴露 `dctn` 一个函数，内部完全复刻 `_execute` 的三步流程（记录 `xp` → `np.asarray` → 调 `_duccfft.dctn` → `xp.asarray` 转回）。然后：

1. 用 Python 列表、numpy 数组分别调用你的 `dctn`，断言输出类型为 `numpy.ndarray`，且数值与 `scipy.fft.dctn` 一致。
2. 解释：为什么你的实现里，第 ① 步 `array_namespace` 必须在第 ② 步 `np.asarray` 之前。
3. 对照 `_basic_backend._execute_1D`，说明如果让你给 FFT 而非实变换写后端，你需要**多加哪个分支**（提示：`hasattr(xp, 'fft')`），并解释为什么实变换后端不需要这个分支。

**验收标准**：

- 两个输入的 `type(...)` 均为 `numpy.ndarray`。
- `np.allclose` 与公共 API 比较为 `True`。
- 能用一句话讲清「记录出身必须在转换前」「实变换无需 `xp.fft` 分支」两个理由。

> 综合实践的真实运行结果（特别是涉及非默认模式或非 numpy 输入的部分）**待本地验证**；在标准 numpy 环境下的上述断言应当全部通过。

## 6. 本讲小结

- `_execute` 是一个 8 行的**高阶函数适配器**：把「用哪个 ducc 内核」当参数，统一完成「记录出身 → 转 numpy → 调内核 → 转回」四步，被 8 个实变换函数复用。
- `array_namespace` 是识别「数组出身」的桥接器；**默认模式下它总返回 numpy**（`SCIPY_ARRAY_API` 未设），所以桥接是「隐形的」，真正干活的只是「转 numpy + 调 ducc」。
- 实变换后端与 `_basic_backend` 同处一层、最终都落 ducc，但路由策略一简一繁：实变换**没有可移植的 `xp.dct`**，只能全程经 numpy，因此无需 `is_numpy` / `xp.fft` 分支，并被标注 `cpu_only=True`。
- 1-D 的 `dct` 把 `(n, axis)` 塞进 `_execute` 的 `(s, axes)` 槽位——位置对齐使一个 `_execute` 同时服务 1-D 与 N-D 变换。
- `_ScipyBackend.__ua_function__` 用方法名（如 `'dctn'`）在三个 `*_backend` 模块里 `getattr` 找实现，本文件的 8 个 wrapper 正是因此被命中。

## 7. 下一步学习建议

- **深入 ducc 内核**：`_execute` 调用的 `_duccfft.dctn` 等是 `_r2rn` 的 `partial`，其内部用 `_asfarray / _fix_shape / _normalization` 做进内核前的预处理。建议接着读 `_duccfft/helper.py`（对应 u5-l2），看「转成 numpy 之后、调 C 之前」还做了哪些形状与归一化修整。
- **对照 FFT 后端的精妙路由**：本讲反复对照的 `_basic_backend._execute_1D / _execute_nD` 三分支结构，是 u6-l1 的主题；学完它你会彻底明白「numpy 直连 / `xp.fft` / 回退」三条路各在何时触发。
- **数组标准开关**：`xp_capabilities` 装饰器与 `SCIPY_ARRAY_API` 开关如何控制后端选择，是 u6-l2 的主题；本讲的 `cpu_only=True` 正来源于此。
