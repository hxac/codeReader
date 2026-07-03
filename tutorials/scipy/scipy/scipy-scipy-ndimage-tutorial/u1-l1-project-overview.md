# 项目定位与能力总览

## 1. 本讲目标

本讲是 `scipy.ndimage` 学习手册的第一篇，目标是帮你建立「全局地图」。读完本讲后，你应该能够：

- 说清楚 `scipy.ndimage` 是什么、解决哪一类问题、在 SciPy 生态里处于什么位置。
- 识别 **Filters / Fourier filters / Interpolation / Measurements / Morphology** 五大功能域，并各举出至少 2 个代表函数。
- 打开 `__init__.py`，通过其中的 `autosummary` 分组读懂整个公开 API 的全貌。
- 理解公开 API 是如何「从五个私有模块一路装配到 `scipy.ndimage` 命名空间」的。

本讲不深入任何一个函数的实现细节——那是后续 u2~u5 各功能域讲义的任务。本讲只做一件事：让你「站在高处看清全貌」。

## 2. 前置知识

在开始之前，建议你大致了解以下概念（不必精通）：

- **NumPy ndarray**：`scipy.ndimage` 的输入输出几乎都是多维数组（N-D array）。你需要知道 `ndarray` 的 `shape`、`dtype`、`axis` 是什么意思。
- **「图像」在科学计算里的含义**：这里的「图像」不一定是照片。一个二维温度场、一个三维医学体数据（CT/MRI）、甚至一个一维信号序列，只要能表示成数组，都可以用 `ndimage` 处理。所以本包强调的是 **multidimensional（多维）**，而非局限于二维图片。
- **滤波 / 卷积的直觉**：知道「用一个小的核（kernel）在数组上滑动、在每个位置做加权求和」大致是什么意思即可，本讲不会用到公式细节。
- **Python 包的导入机制**：知道 `from package import *` 与 `__all__` 的关系，会帮助你理解后面「API 装配链」一节。

如果你对其中某些点不熟也没关系，本讲会用通俗语言重新解释。

## 3. 本讲源码地图

本讲主要围绕 **公开命名空间的入口文件** 展开，并附带提一句它的装配链条。涉及的关键文件如下：

| 文件 | 作用 | 本讲如何使用 |
| --- | --- | --- |
| [`__init__.py`](__init__.py) | `scipy.ndimage` 的入口。顶部 docstring 用 `autosummary` 把全部公开函数分成五大功能域；底部 import 完成公开 API 的装配。 | 本讲的绝对主角，逐段精读。 |
| [`_ndimage_api.py`](_ndimage_api.py) | 「裸 API」聚合模块：把五个私有实现模块的公开函数收集到一处。 | 用来解释 API 从哪里来。 |
| [`_support_alternative_backends.py`](_support_alternative_backends.py) | 在裸 API 外面包一层「数组 API 后端委托」（CuPy/JAX 等），再导出给 `__init__`。 | 用来解释 API 的最后一层封装。 |
| [`_filters.py`](_filters.py) / [`_fourier.py`](_fourier.py) / [`_interpolation.py`](_interpolation.py) / [`_measurements.py`](_measurements.py) / [`_morphology.py`](_morphology.py) | 五大功能域的真实实现。 | 本讲只引用其中个别函数签名做示例，不深入实现。 |

> 小提示：SciPy 源码里以 `_` 开头的模块是「私有的」，意味着官方不保证其接口稳定、不建议你直接 `import`。我们真正应该用的入口只有 `scipy.ndimage`。

## 4. 核心概念与源码讲解

### 4.1 全局视角：scipy.ndimage 的定位与 API 装配链

#### 4.1.1 概念说明

`scipy.ndimage` 是 SciPy 提供的 **多维图像处理（multidimensional image processing）** 子包。它解决的核心问题是：

> 给定任意维度的数组，如何在每个像素的「邻域」上做计算（滤波）、如何在任意坐标上重采样（插值）、如何统计与标记感兴趣区域（测量）、以及如何对二值/灰度数组做形态学运算（形态学）。

入口文件 [`__init__.py:1-9`](__init__.py#L1-L9) 的第一行 docstring 就点明了这一定位：

> "This package contains various functions for multidimensional image processing."

理解 `ndimage` 有两个关键视角：

1. **功能视角**：它把功能切成五大域，每个域是一组签名风格相近的函数。
2. **工程视角**：这些函数不是凭空出现在 `scipy.ndimage` 里的，而是经过一条清晰的「装配链」逐层导出。理解这条链，你以后排查「为什么找不到某个名字」「为什么行为和我预期不一样」时就有迹可循。

#### 4.1.2 核心流程：API 装配链

`scipy.ndimage.gaussian_filter` 这样的名字，经历如下三步才出现在你的 `import` 里：

```text
(1) 五个私有实现模块            _filters.py  _fourier.py  _interpolation.py
        (函数真正定义处)        _measurements.py  _morphology.py
                  │  from ._filters import *  ……
                  ▼
(2) _ndimage_api.py            把五个模块的公开函数聚合为「裸 API」
                  │  用 delegate_xp 装饰 + 加数组 API 能力声明
                  ▼
(3) _support_alternative_backends.py   带后端委托能力的 API
                  │  from ._support_alternative_backends import *
                  ▼
(4) __init__.py  →  最终的  scipy.ndimage  公开命名空间
```

要点：

- 第 (2) 步用 `__all__` 自动收集所有不以 `_` 开头的名字，所以新增一个公开函数只需在实现模块里 `def` 出来。
- 第 (3) 步是「可选层」：只有当设置了环境变量 `SCIPY_ARRAY_API` 时，CuPy/JAX 委托才真正生效；否则直接走原始函数（详见后续 u7-l1）。
- 第 (4) 步还顺手 `del` 掉了私有的中间模块名，避免它们「泄漏」到公开命名空间。

#### 4.1.3 源码精读

入口文件的导入与 `__all__` 设置在最底部，短短几行完成了整条装配链的「最后一公里」：

[`__init__.py:153-162`](__init__.py#L153-L162) —— 把装饰好的 API 引入公开命名空间，并清理私有中间模块：

```python
# bring in the public functionality from private namespaces
from ._support_alternative_backends import *

# adjust __all__ and do not leak implementation details
from . import _support_alternative_backends
__all__ = _support_alternative_backends.__all__
del _support_alternative_backends, _ndimage_api, _delegators
```

中间层 [`_ndimage_api.py:9-16`](_ndimage_api.py#L9-L16) 把五个实现模块「平铺」到一处，并用列表推导自动生成 `__all__`：

```python
from ._filters import *
from ._fourier import *
from ._interpolation import *
from ._measurements import *
from ._morphology import *

# '@' due to pytest bug, scipy/scipy#22236
__all__: list[str] = [s for s in dir() if not s.startswith(('_', '@'))]
```

最外层 [`_support_alternative_backends.py:110-122`](_support_alternative_backends.py#L110-L122) 用一个 `for` 循环对裸 API 里每个函数逐一装饰，再把结果塞进模块命名空间：

```python
for func_name in _ndimage_api.__all__:
    bare_func = getattr(_ndimage_api, func_name)
    delegator = getattr(_delegators, func_name + "_signature")
    capabilities = capabilities_dict.get(func_name, default_capabilities)
    f = capabilities(
        delegate_xp(delegator, MODULE_NAME)(bare_func)
        if SCIPY_ARRAY_API else bare_func
    )
    vars()[func_name] = f   # 加入命名空间，供 __init__ 导入
```

此外，`__init__.py` 末尾 [`__init__.py:165-170`](__init__.py#L165-L170) 还保留了一组 **已弃用的子模块命名空间**（`filters`、`fourier`、`interpolation`、`measurements`、`morphology`），它们将在 SciPy v2.0.0 移除——也就是说 `from scipy.ndimage import filters` 这种旧写法还能用，但会报弃用警告。

#### 4.1.4 代码实践

1. **实践目标**：用一行代码确认「公开 API 确实由装配链产生」。
2. **操作步骤**：
   ```python
   import scipy.ndimage as ndi
   print(len(ndi.__all__))        # 公开函数总数
   print("gaussian_filter" in ndi.__all__)
   print(hasattr(ndi, "_filters"))  # 私有模块是否被 'del' 掉了？
   ```
3. **需要观察的现象**：`__all__` 是一个较长的列表；`gaussian_filter` 在其中；而 `_filters`（带下划线的私有名）通常已不在公开命名空间里。
4. **预期结果**：第一个打印是一个正整数（大约 70+）；第二个打印 `True`；第三个打印 `False`（或对以下划线开头的名字返回 `False`）。
5. **结果**：待本地验证（具体函数总数会随版本变化）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `_ndimage_api.py` 里要用 `[s for s in dir() if not s.startswith('_')]` 来生成 `__all__`，而不是手写一份清单？

> **参考答案**：手写清单容易和实现模块「脱节」——新增一个公开函数就要记得回来改清单，漏改就会导致函数存在于模块里却不在 `__all__` 里、无法被 `import *` 带出。用 `dir()` 过滤下划线名，可以让 `__all__` 自动跟随实现模块的公开定义，降低维护成本。

**练习 2**：如果有人写 `from scipy.ndimage.filters import gaussian_filter`，会发生什么？

> **参考答案**：能导入成功（因为 [`filters.py:24-27`](filters.py#L24-L27) 用了 `_sub_module_deprecation` 做转发），但会触发 `DeprecationWarning`，提示该子模块命名空间将在 v2.0.0 移除、应改用 `scipy.ndimage` 命名空间。

---

### 4.2 Filters 滤波函数组

#### 4.2.1 概念说明

**滤波（filtering）** 是图像处理最基础的操作：用一个「核 / 邻域」在数组上滑动，每个输出像素等于对应输入邻域里的某种聚合（加权求和、取最大、取中位数……）。`ndimage` 的 Filters 组覆盖了从最底层的「自定义权重相关/卷积」到各种「现成算子」（高斯、Sobel、中值……）。

#### 4.2.2 核心流程

Filters 组的函数可以按「邻域聚合方式」粗分为几类：

```text
自定义权重类：  convolve / correlate (+ 1d 版本)
现成平滑类：    gaussian_filter / uniform_filter (+ 1d 版本)
微分/边缘类：   sobel / prewitt / laplace / gaussian_laplace / *_gradient_magnitude
秩/统计类：     rank_filter / median_filter / percentile_filter
                  minimum_filter / maximum_filter (+ 1d 版本)
回调类：        generic_filter / generic_filter1d (把邻域交给你的 Python 函数)
```

它们的共同参数风格（`input / output / mode / cval / axes / origin`）会在 u1-l4 统一讲解。

#### 4.2.3 源码精读

Filters 组的公开函数清单就是 `__init__.py` docstring 里的第一个 `autosummary` 块，见 [`__init__.py:12-42`](__init__.py#L12-L42)。其中 `-` 后面是每个函数的一句话简介，例如：

```rst
   convolve - Multidimensional convolution
   correlate1d - 1-D correlation along the given axis
   gaussian_filter
   ...
```

其中最常用的代表函数 `gaussian_filter` 的签名定义在实现模块里，见 [`_filters.py:758-760`](_filters.py#L758-L760)：

```python
def gaussian_filter(input, sigma, order=0, output=None,
                    mode="reflect", cval=0.0, truncate=4.0, *, radius=None,
                    axes=None):
```

可以看到它接受 `input`（输入数组）、`sigma`（各轴高斯标准差）、`mode`（边界处理方式）等参数——这种参数风格几乎贯穿整个 Filters 组。

#### 4.2.4 代码实践

1. **实践目标**：体会「滤波 = 邻域聚合」，观察 `sigma` 对平滑程度的影响。
2. **操作步骤**：
   ```python
   import numpy as np
   from scipy import ndimage
   a = np.array([[0, 0, 0, 0, 0],
                 [0, 9, 0, 9, 0],
                 [0, 0, 0, 0, 0],
                 [0, 9, 0, 9, 0],
                 [0, 0, 0, 0, 0]], dtype=float)
   print(ndimage.gaussian_filter(a, sigma=0.5).round(2))
   print(ndimage.gaussian_filter(a, sigma=1.0).round(2))
   ```
3. **需要观察的现象**：原本锐利的「9」尖峰被「摊开」成柔和的亮斑；`sigma` 越大，亮斑越平、越宽。
4. **预期结果**：两次输出形状都是 `(5, 5)`，但数值分布不同；`sigma=1.0` 的结果比 `sigma=0.5` 更「模糊」。
5. **结果**：待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`correlate` 和 `convolve` 有什么区别？（提示：从「权重是否翻转」入手。）

> **参考答案**：相关（correlate）直接用权重在邻域上做加权求和；卷积（convolve）会先把权重数组「翻转」再做加权求和。当权重对称时两者结果相同。精确细节会在 u2-l1 用数学推导。

**练习 2**：如果你想在每个像素的 3×3 邻域里取最大值，应该用 Filters 组里的哪个函数？

> **参考答案**：`maximum_filter`。它属于「秩/统计类」滤波器。

---

### 4.3 Fourier filters 频域函数组

#### 4.3.1 概念说明

**傅里叶滤波（Fourier filters）** 把滤波放到「频域」去做：先把数组做 FFT，在频域里乘上一个核函数，再做（或不做）IFFT 回到空域。这一组只有 4 个函数，专门用于「假定输入已经是 FFT 结果」的场景。

#### 4.3.2 核心流程

```text
频域乘法核类： fourier_gaussian / fourier_uniform / fourier_ellipsoid
                 （在频域构造高斯/均匀/椭球低通核，做逐元素乘法）
相位平移类：   fourier_shift
                 （用线性相位因子实现亚像素级平移，不改变幅度谱）
```

核心约定：这组函数 **不会替你做 FFT**——它们假定 `input` 已经是频域数据（通常是 `np.fft.fftn` 的结果），它们的任务是「构造频域核并逐元素相乘」。

#### 4.3.3 源码精读

Fourier 组的 autosummary 块见 [`__init__.py:44-53`](__init__.py#L44-L53)，共 4 个函数：

```rst
   fourier_ellipsoid
   fourier_gaussian
   fourier_shift
   fourier_uniform
```

这组的实现全部集中在单个文件 [`_fourier.py`](_fourier.py) 里（是五大域里最小的一个），详细机制留到 u2-l6。

#### 4.3.4 代码实践

1. **实践目标**：确认 Fourier 滤波函数「不替你做 FFT」这一约定。
2. **操作步骤**：
   ```python
   import numpy as np
   from scipy import ndimage
   a = np.zeros((8,))
   a[0] = 1.0                      # 一个类脉冲信号
   A = np.fft.fft(a)               # 先手动 FFT
   B = ndimage.fourier_gaussian(A, sigma=1.0)   # 在频域乘高斯核
   print(np.allclose(A, B))        # sigma 较小时，频域核接近 1 吗？
   ```
3. **需要观察的现象**：`fourier_gaussian` 接受的是复数频域数组 `A`，输出也是复数数组；当 `sigma` 很小时核接近 1，`A` 与 `B` 数值接近。
4. **预期结果**：`sigma` 很小时 `np.allclose(A, B)` 偏向 `True`；`sigma` 变大后两者差异显著。
5. **结果**：待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么说 Fourier 组是「假定输入已 FFT」？

> **参考答案**：因为这组函数的实现是在频域里构造乘法核并与输入逐元素相乘，而不是调用 FFT。所以调用者要自己先 `np.fft.fftn`，否则函数处理的就是空域数据，结果没有频域含义。

**练习 2**：`fourier_shift` 实现平移，为什么不需要改变幅度谱？

> **参考答案**：平移在频域等价于乘一个「幅度为 1、仅相位随频率线性变化」的因子（线性相位），它只改变相位谱、不改变幅度谱，因此能实现亚像素平移。

---

### 4.4 Interpolation 插值与几何变换函数组

#### 4.4.1 概念说明

**插值（interpolation）** 解决的问题是：「我想知道数组在 **非整数坐标** 上的取值」。这是几何变换（旋转、缩放、平移、仿射）的基础——把图像旋转 30° 后，新图像的每个像素都来自原图的某个非整数位置，需要用插值来估计。

#### 4.4.2 核心流程

```text
通用坐标映射：  map_coordinates   给定任意坐标，在数组上做样条插值
几何变换：      affine_transform  仿射（旋转/缩放/剪切/平移）
               geometric_transform  任意映射（含 C 回调）
便捷封装：      shift / zoom / rotate   （内部复用 affine_transform）
样条预滤波：    spline_filter / spline_filter1d   （为高阶插值准备系数）
```

样条阶数 `order`（0=最近邻、1=双线性、2~5=高阶样条）是这组函数的核心参数之一。

#### 4.4.3 源码精读

Interpolation 组的 autosummary 块见 [`__init__.py:55-68`](__init__.py#L55-L68)，共 8 个函数。实现集中在 [`_interpolation.py`](_interpolation.py)。本讲只做认识，样条预滤波的数学原理、`map_coordinates` 的坐标约定、`affine_transform` 的 matrix 四种形状等细节分别在 u3-l1 ~ u3-l4 展开。

#### 4.4.4 代码实践

1. **实践目标**：用一个最简单的几何变换（平移）体会「插值 = 在任意位置取值」。
2. **操作步骤**：
   ```python
   import numpy as np
   from scipy import ndimage
   a = np.arange(25, dtype=float).reshape(5, 5)
   print("原图:\n", a)
   print("整体右移 1、下移 1（reflect 边界）:\n", ndimage.shift(a, shift=(1, 1)))
   ```
3. **需要观察的现象**：原数组的内容整体向右下方向移动；边界处因为 `mode='reflect'`（默认）而出现镜像填充。
4. **预期结果**：输出形状仍是 `(5, 5)`；左上角元素被「挤」到右下方向。
5. **结果**：待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：`shift / zoom / rotate` 这三个便捷函数，本质上都复用了哪个更通用的函数？

> **参考答案**：都复用了 `affine_transform`（`rotate` 还涉及 `map_coordinates` 的画布处理）。它们只是把对应的仿射矩阵和 offset 构造好后再调用 `affine_transform`。

**练习 2**：`order=0` 的插值对应哪种最简单的取值方式？

> **参考答案**：最近邻插值（nearest），即把非整数坐标直接取整后取该位置的值。

---

### 4.5 Measurements 测量与连通区域函数组

#### 4.5.1 概念说明

**测量（measurements）** 关心的是「数组里有哪些区域、每个区域的统计量是多少」。典型流程是：先把二值数组里连成片的「前景」标记成不同编号（`label`），再针对每个编号区域算均值、面积、质心、极值等。这组是图像分割、目标计数的核心工具。

#### 4.5.2 核心流程

```text
区域发现：  label              把连成片的前景标成 1,2,3…
区域定位：  find_objects       给每个标签算一个 bounding-box 切片
            value_indices      按值聚合所有像素索引
区域统计：  sum_labels / mean / variance / standard_deviation
            minimum / maximum / median / extrema / center_of_mass
通用聚合：  labeled_comprehension  对每个区域跑任意函数
分割：      watershed_ift      用 marker 驱动的分水岭分割
```

#### 4.5.3 源码精读

Measurements 组的 autosummary 块见 [`__init__.py:70-92`](__init__.py#L70-L92)，是五大域里函数第二多的一组。其中代表函数 `label` 的签名与返回值约定见 [`_measurements.py:43-87`](_measurements.py#L43-L87)：

```python
def label(input, structure=None, output=None):
    ...
# Returns
# -------
# label : ndarray or int      # 标记后的整数数组
# num_features : int          # 发现了多少个连通区域
#
# 若 output 为 None，返回 (labeled_array, num_features) ；
# 若 output 为 ndarray，则原地写入并只返回 num_features。
```

注意一个初学者常踩的坑：`label` 默认返回的是 **二元组** `(labeled_array, num_features)`，而不是单个数组。

#### 4.5.4 代码实践

1. **实践目标**：用 `label` 体会「连通区域标记」，并确认它返回二元组。
2. **操作步骤**：
   ```python
   import numpy as np
   from scipy import ndimage
   b = np.array([[1, 1, 0, 0, 1],
                 [1, 0, 0, 1, 1],
                 [0, 0, 1, 0, 0],
                 [0, 1, 1, 0, 1],
                 [1, 0, 0, 1, 1]])
   labeled, n = ndimage.label(b)
   print("标记数组:\n", labeled)
   print("区域数:", n)
   ```
3. **需要观察的现象**：每个连成片的前景块被赋以同一个整数编号（1, 2, 3…），互不相连的块编号不同；`n` 等于编号总数。
4. **预期结果**：`labeled` 是整数数组，`n` 是其中不同正整数标签的个数。
5. **结果**：待本地验证（具体编号会随结构元与连通关系变化）。

#### 4.5.5 小练习与答案

**练习 1**：调用 `ndimage.label(b)` 时，如果你只写 `labeled = ndimage.label(b)`（只接一个返回值），`labeled` 实际是什么？

> **参考答案**：它其实是一个长度为 2 的元组 `(labeled_array, num_features)`，而不是数组本身。要写 `labeled, n = ndimage.label(b)` 才能分别拿到数组和区域数。

**练习 2**：`label` 的 `structure` 参数（结构元）控制什么？

> **参考答案**：控制「什么样的相邻像素算连通」。默认结构元只把上下左右（4-连通）视为相连；换成 `generate_binary_structure(2, 2)` 则连对角（8-连通）也算相连，会得到更少的区域。

---

### 4.6 Morphology 形态学函数组

#### 4.6.1 概念说明

**形态学（morphology）** 源于对二值图像的几何操作：侵蚀（erosion）让前景「收缩」、膨胀（dilation）让前景「扩张」，并由二者组合出开运算、闭运算、填孔、top-hat 等。后来又扩展到灰度图像（灰度形态学）和距离变换。这组是形状分析、噪声去除、边缘提取的常用工具。

#### 4.6.2 核心流程

```text
结构元：          generate_binary_structure / iterate_structure
                   （定义「邻域/形状」）
二值形态学：      binary_erosion / binary_dilation (+ opening/closing/
                   hit_or_miss/fill_holes/propagation 组合)
灰度形态学：      grey_erosion / grey_dilation (+ grey_opening/grey_closing)
形态学梯度/顶帽： morphological_gradient / morphological_laplace
                   white_tophat / black_tophat
距离变换：        distance_transform_edt / _cdt / _bf
                   （每个前景点到最近背景点的距离）
```

#### 4.6.3 源码精读

Morphology 组的 autosummary 块见 [`__init__.py:94-119`](__init__.py#L94-L119)，是五大域里函数最多的一组（20 个）。代表函数 `binary_dilation` 的签名见 [`_morphology.py:407`](_morphology.py#L407)：

```python
def binary_dilation(input, structure=None, iterations=1, mask=None,
                    border_value=0, origin=0, brute_force=False):
```

二值膨胀会把每个前景像素按 `structure` 定义的形状「涂大」一圈。结构元如何构造（`generate_binary_structure` 的 connectivity 公式）留到 u5-l1。

#### 4.6.4 代码实践

1. **实践目标**：用 `binary_dilation` 直观看到「前景扩张」。
2. **操作步骤**：
   ```python
   import numpy as np
   from scipy import ndimage
   b = np.array([[0, 0, 0, 0, 0],
                 [0, 0, 0, 0, 0],
                 [0, 0, 1, 0, 0],
                 [0, 0, 0, 0, 0],
                 [0, 0, 0, 0, 0]])
   print(ndimage.binary_dilation(b).astype(int))
   ```
3. **需要观察的现象**：中心那个 `1` 周围的上下左右（默认 4-连通结构元）都被「撑」成 `1`，形成一个十字。
4. **预期结果**：输出是一个 `(5, 5)` 的 0/1 数组，中心为 `1` 且其 4-邻域也变为 `1`。
5. **结果**：待本地验证。

#### 4.6.5 小练习与答案

**练习 1**：`binary_erosion` 和 `binary_dilation` 是什么关系？

> **参考答案**：它们是对偶操作。膨胀让前景变大，侵蚀让前景变小；对前景做侵蚀，等价于对背景（补集）做膨胀再取补。详见 u5-l2。

**练习 2**：`binary_dilation` 的 `iterations=3` 和连续调用 3 次 `binary_dilation(...)` 效果一样吗？

> **参考答案**：在默认结构元下效果一致——`iterations` 就是用相同结构元重复膨胀的次数，等价于迭代调用。

---

## 5. 综合实践

本任务贯穿本讲的 **Filters / Measurements / Morphology** 三个功能域，让你在一次运行里体会它们的输入输出形态差异。

1. **实践目标**：构造一个 5×5 的二维随机数组，分别调用一个滤波函数、一个测量函数、一个形态学函数，对比三类函数「输入是同一个数组、输出形态却不同」的现象。
2. **操作步骤**：
   ```python
   import numpy as np
   from scipy import ndimage

   # 1) 构造 5x5 随机数组
   rng = np.random.default_rng(42)
   a = rng.random((5, 5))

   # 2) 滤波：高斯平滑（Filters 组）—— 输入输出同形状、同 dtype
   smooth = ndimage.gaussian_filter(a, sigma=1.0)

   # 3) 测量：先二值化再连通标记（Measurements 组）
   #    label 返回 (labeled_array, num_features)
   binary = (a > 0.5)
   labeled, num = ndimage.label(binary)

   # 4) 形态学：对同一个二值图做膨胀（Morphology 组）
   dilated = ndimage.binary_dilation(binary)

   # 5) 打印三次结果
   print("== 原始数组 ==\n", a.round(2))
   print("== gaussian_filter (Filters) ==\n", smooth.round(2))
   print("== label (Measurements): 区域数 =", num, "\n", labeled)
   print("== binary_dilation (Morphology) ==\n", dilated.astype(int))
   ```
3. **需要观察的现象**：
   - `gaussian_filter` 的输出与输入 **同形状同 dtype**，是「逐像素」变换。
   - `label` 的输出是 **整数数组**，且额外返回一个「区域数」整数；值域是 `{0,1,2,…}`。
   - `binary_dilation` 的输出是 **布尔数组**（`True/False`），前景比原二值图「胖」一圈。
4. **预期结果**：三次输出形状都保持 `(5, 5)`，但 `dtype` 与语义完全不同——分别对应「平滑后的强度图」「区域编号图」「扩张后的二值图」。这正体现了三大功能域各司其职。
5. **结果**：待本地验证（具体数值会随 `rng` 种子和阈值变化，但上述「形态与 dtype 差异」的结论稳定成立）。

> **进阶（可选）**：把上面 4 个输出数组的 `.shape` 和 `.dtype` 一次性打印出来 `print(smooth.shape, smooth.dtype)` 等，你会更直观地看到三类函数在「数据类型」上的差异。

## 6. 本讲小结

- `scipy.ndimage` 是 SciPy 的 **多维图像处理** 子包，处理对象是任意维度的 NumPy 数组，不只是二维照片。
- 它的公开 API 在 `__init__.py` 顶部 docstring 里按 **五大功能域** 用 `autosummary` 分组：Filters、Fourier filters、Interpolation、Measurements、Morphology。
- 公开 API 是由一条清晰的 **装配链** 产生的：五个私有实现模块 → `_ndimage_api.py`（裸 API 聚合）→ `_support_alternative_backends.py`（加 CuPy/JAX 后端委托）→ `__init__.py`（公开命名空间）。
- 五大域各司其职：Filters 做邻域滤波、Fourier filters 做频域乘法核、Interpolation 做几何重采样、Measurements 做区域标记与统计、Morphology 做二值/灰度形态学运算。
- `label` 返回的是二元组 `(labeled_array, num_features)`，Fourier 组「不替你做 FFT」——这两点是初学者最常踩的坑。
- 旧的 `scipy.ndimage.filters` 等子模块命名空间已弃用，应统一从 `scipy.ndimage` 导入。

## 7. 下一步学习建议

本讲建立了「功能域 + 装配链」的全局视图。接下来建议：

- 想先弄懂「几乎所有函数共享的参数」（`mode`、`output`、`axes`、`origin`）从哪来、怎么用 → 学 **u1-l4 共享支撑工具**。
- 想弄懂目录结构、构建系统、C 扩展是怎么编译出来的 → 学 **u1-l2 目录结构与构建系统**。
- 想彻底理解 API 装配链每一层（含数组 API 后端委托）→ 学 **u1-l3 四层架构与公开 API 装配链**。
- 之后即可按兴趣进入各功能域：u2（滤波/傅里叶）、u3（插值）、u4（测量）、u5（形态学），最后下探 u6（C 内核）与 u7（后端委托/测试）。

建议的阅读顺序：u1-l2 → u1-l3 → u1-l4，把入门层的另外三篇补齐，再进入进阶层。
