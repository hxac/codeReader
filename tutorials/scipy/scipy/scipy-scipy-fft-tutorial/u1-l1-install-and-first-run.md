# 导入、运行与第一次调用

## 1. 本讲目标

前两讲（u1-l1、u1-l2）我们一直在「读地图」：知道了 `scipy.fft` 是什么、有哪些公共函数、目录怎么分层。这一讲我们要**从读地图转向动手跑**——把 `scipy.fft` 真正装上、导入进来、完成第一次 `fft` 调用，并读懂屏幕上那一串复数到底意味着什么。

学完本讲你应该能够：

1. 在自己的 Python 环境里安装并正确导入 `scipy.fft`。
2. 构造一个 8 点复指数信号，调用 `scipy.fft.fft`，并解释输出里那个 `8.0` 的峰值从何而来。
3. 用 `ifft(fft(x))` 验证正/逆变换的可逆性，理解默认归一化 `norm="backward"` 的缩放约定。
4. 用 `scipy.fft.test()` 跑通子包自带的测试，并说清楚这行代码背后 `PytestTester` 做了什么。

这是整个学习路线里第一次真正执行代码。把这一步跑通，后面所有讲义的实践任务你才有地方落地。

## 2. 前置知识

本讲是 beginner 级别，假设你已经具备前两讲建立的认知：

- **公共 API 全景（u1-l1）**：`scipy.fft` 对外暴露了 FFT、DCT/DST、Hankel、helper、backend 五大类函数；`__init__.py` 顶部的文档字符串只是「功能分类」，真正的对外契约是 `__all__`。
- **四层架构（u1-l2）**：一次 `scipy.fft.fft(x)` 调用要穿过「公共 API → uarray 分派 → 后端 → ducc 核心」四层，最终落到 C 扩展 `pyduccfft`。本讲主要站在**用户视角**看结果，遇到「函数体为何是空壳」时会回到这个模型。

再补充三个本讲会用到的通用概念：

- **包管理（pip / conda）**：`scipy.fft` 不是独立发布的包，它是 `scipy` 这个大包里的一个子包。所以「安装 scipy.fft」其实就是「安装 scipy」。
- **离散傅里叶变换（DFT）与复指数信号**：DFT 把一段离散信号分解成不同频率的复指数分量。如果你喂给它的恰好是一个「纯净」的某个频率的复指数，那么输出里只会在对应频率的位置上出现一个尖峰——这是本讲读懂 `fft` 输出的关键直觉。
- **pytest**：Python 事实标准的测试框架。`scipy.fft.test()` 内部就是调用 `pytest` 来跑测试的，所以我们顺带要了解一点 pytest 的概念。

## 3. 本讲源码地图

本讲围绕下面四个文件展开。前两个是 `scipy.fft` 子包自身的入口与签名，后两个分别解释 `test()` 的实现与可逆性测试。

| 文件 | 作用 |
|------|------|
| [`__init__.py`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/__init__.py) | 子包入口：把各子模块函数搬进命名空间，并把 `test` 注册成 `PytestTester` |
| [`_basic.py`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic.py) | `fft`/`ifft` 等函数的对外签名 + docstring（含本讲要跑的那个 8 点示例） |
| [`scipy/_lib/_testutils.py`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_testutils.py) | `PytestTester` 类的实现，解释 `scipy.fft.test()` 如何转化为一次 pytest 调用 |
| [`tests/test_basic.py`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/tests/test_basic.py) | `scipy.fft` 的测试集，本讲引用其中的可逆性测试 `test_identity` |

> 注意：`_testutils.py` 不在 `scipy/fft/` 目录下，而在 `scipy/_lib/`，它是整个 SciPy 共用的测试工具，`scipy.fft` 只是「借用」它。

## 4. 核心概念与源码讲解

### 4.1 安装与导入：scipy.fft 的入口

#### 4.1.1 概念说明

很多人第一次用 `scipy.fft` 会卡在两个小问题上：

1. **怎么安装？** `scipy.fft` 没有独立的 PyPI 包，它随 `scipy` 一起发布。所以安装命令就是装 `scipy` 本身。
2. **为什么 `import scipy` 之后还要 `import scipy.fft`？** 在 Python 里，`import scipy` 并不保证把所有子包都加载进来；要使用某个子包，最稳妥、也是 SciPy 文档示例里统一采用的做法是**显式导入该子包**：`import scipy.fft`。

本讲要跑的所有代码都建立在这两步之上。

#### 4.1.2 核心流程

从零到能调用，三步：

```
1. 安装：    pip install scipy     （或 conda install scipy）
2. 导入：    import scipy.fft
3. 调用：    scipy.fft.fft(...)
```

当你写下 `import scipy.fft` 时，Python 会执行 `scipy/fft/__init__.py` 这个入口文件。它在 u1-l2 里被形容为「搬运清单」——本身不写算法，只做两件事：

- 把 `_basic`、`_realtransforms`、`_fftlog`、`_helper`、`_backend`、`_duccfft.helper` 这些子模块里的函数搬进 `scipy.fft` 命名空间；
- 把 `test` 这个名字绑定到一个 `PytestTester` 实例上，让你以后能 `scipy.fft.test()`。

#### 4.1.3 源码精读

入口文件的「搬运」部分——正是它决定了 `scipy.fft` 里能看到哪些函数：

[`__init__.py:L86-L97`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/__init__.py#L86-L97) —— 把五大类函数从各子模块搬进命名空间；注意最后两行：后端控制函数来自 `_backend`，`set_workers/get_workers` 来自计算核心 `_duccfft.helper`。

```python
from ._basic import (fft, ifft, fft2, ifft2, fftn, ifftn,
    rfft, irfft, rfft2, irfft2, rfftn, irfftn,
    hfft, ihfft, hfft2, ihfft2, hfftn, ihfftn)
from ._realtransforms import dct, idct, dst, idst, dctn, idctn, dstn, idstn
from ._fftlog import fht, ifht, fhtoffset
from ._helper import (next_fast_len, prev_fast_len, fftfreq,
    rfftfreq, fftshift, ifftshift)
from ._backend import (set_backend, skip_backend, set_global_backend, register_backend)
from ._duccfft.helper import set_workers, get_workers
```

入口文件末尾三行则是本讲的另一个重点——把 `test` 注册成可调用对象，并立刻「删掉」`PytestTester` 这个名字，避免它污染 `scipy.fft` 的公共命名空间：

[`__init__.py:L112-L114`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/__init__.py#L112-L114) —— 从共用的测试工具库导入 `PytestTester`，用当前模块名 `__name__`（即 `"scipy.fft"`）构造一个实例并命名为 `test`，然后 `del` 掉 `PytestTester` 本身。

```python
from scipy._lib._testutils import PytestTester
test = PytestTester(__name__)
del PytestTester
```

这三行的精妙之处在于：用户最终看到的 `scipy.fft.test` 是一个**绑定了具体模块名的对象**，而不是一个类。我们会在 4.4 拆解它内部如何工作。

#### 4.1.4 代码实践（验证导入）

**目标**：确认 `scipy.fft` 已正确安装、能被导入，并对照 `__all__` 检查公共 API。

**操作步骤**：

1. 在终端确认 scipy 已安装（可选，也可直接进 Python 验证）：

   ```bash
   python -c "import scipy; print(scipy.__version__)"
   ```

2. 进入 Python（或脚本里），显式导入子包并查看它对外暴露的名字：

   ```python
   # 示例代码：验证导入与公共 API
   import scipy.fft
   print(type(scipy.fft))            # 应为 module
   print(len(scipy.fft.__all__))     # __all__ 里公共名字的个数
   print('fft' in scipy.fft.__all__) # True
   ```

**需要观察的现象**：

- `import scipy.fft` 不报错，说明子包加载成功。
- `scipy.fft.__all__` 与 [`__init__.py:L99-L109`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/__init__.py#L99-L109) 里列出的名字一致（u1-l1 已统计过共 41 个名字）。
- `scipy.fft.test` 是一个可调用对象（`callable(scipy.fft.test)` 为 `True`），但 `scipy.fft.PytestTester` 已经不存在（被 `del` 了）。

**预期结果**：导入成功，`__all__` 非空，`test` 可调用。如果你用的是非常旧的 SciPy（< 1.4），`scipy.fft` 根本不存在——那就需要升级 scipy。

> 待本地验证：在你的环境里打印 `scipy.__version__`，确认 ≥ 1.4；`callable(scipy.fft.test)` 是否为 `True`。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `__init__.py` 在注册完 `test` 之后要写一行 `del PytestTester`？

**答案**：因为 `PytestTester` 只是「工具」，不是 `scipy.fft` 要对外暴露的公共 API。如果不删，用户写 `from scipy.fft import *` 时就会把 `PytestTester` 也带出来，污染命名空间；而 `test` 是真正要留给用户用的入口，所以保留它。参见 [`__init__.py:L112-L114`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/__init__.py#L112-L114)。

**练习 2**：`scipy.fft.set_workers` 这个公共函数是从哪个模块搬进来的？这说明了什么？

**答案**：从 [`__init__.py:L97`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/__init__.py#L97) 的 `from ._duccfft.helper import set_workers, get_workers` 可见，它来自**计算核心** `_duccfft.helper`。这说明「计算核心层」也会向上贡献公共 API，再次印证 u1-l2 的四层架构不是单向的。

---

### 4.2 第一次调用 fft：构造信号并读懂输出

#### 4.2.1 概念说明

`fft`（Fast Fourier Transform，快速傅里叶变换）是计算 DFT（Discrete Fourier Transform，离散傅里叶变换）的高效算法。对一段长度为 \(n\) 的复数序列 \(x[0..n-1]\)，它的 DFT 定义为：

\[ y[k] = \sum_{j=0}^{n-1} x[j]\, e^{-2\pi i\, k j / n}, \qquad k = 0, 1, \dots, n-1 \]

直觉上：\(y[k]\) 衡量的是「频率为 \(k/n\) 的那个复指数分量」在原信号里有多强。

如果我们喂给 `fft` 的恰好是一个**纯净的单频率复指数信号**：

\[ x[j] = e^{+2\pi i\, j / n} \]

（即频率为 \(1/n\)，在 \(n\) 个采样点里正好走完一整圈），那么代入公式：

\[ y[k] = \sum_{j=0}^{n-1} e^{2\pi i\, j / n}\, e^{-2\pi i\, k j / n} = \sum_{j=0}^{n-1} e^{2\pi i\, j (1-k) / n} \]

- 当 \(k = 1\)：每一项都是 \(e^{0} = 1\)，求和得 \(n\)（本例 \(n=8\)，所以 \(y[1]=8\)）。
- 当 \(k \neq 1\)：这是一个等比级数，求和恰好为 \(0\)（因为 \(e^{2\pi i\,(1-k)}=1\)，分子为 0）。

所以输出应该「除了第 1 个位置是 8，其余全是 0（浮点误差量级）」。这正是我们要观察的现象。

#### 4.2.2 核心流程

把上面的数学翻译成代码，对应 `_basic.py` docstring 里给出的官方示例：

```
1. 构造信号：  x = np.exp(2j * np.pi * np.arange(8) / 8)   # 频率为 1 的复指数
2. 调用变换：  y = scipy.fft.fft(x)
3. 解读输出：  y[1] ≈ 8（峰值），其余 y[k] ≈ 0
```

读懂输出还要知道一件事：**`fft` 输出的频率不是从低到高排列，而是「先正频、后负频」交错排列**。对 8 点变换，结果位置对应的频率依次是：

```
索引:  0    1    2    3    4    5    6    7
频率:  0    1    2    3   -4   -3   -2   -1
```

所以索引 1 对应「正频率 1」，我们的信号能量正落在这里。索引 4 是 Nyquist（±4 混叠点）。若想把零频挪到正中央，需要用 `fftshift`（u2-l4 会讲）。

#### 4.2.3 源码精读

`fft` 的对外签名长这样——注意它的函数体几乎「什么都不做」，这是 u1-l2 讲过的四层架构留下的特征：

[`_basic.py:L25-L28`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic.py#L25-L28) —— `fft` 被 `@_dispatch` 装饰成可分派的 uarray 多方法；真正计算不在这一层。

```python
@xp_capabilities(allow_dask_compute=True)
@_dispatch
def fft(x, n=None, axis=-1, norm=None, overwrite_x=False, workers=None, *, plan=None):
    """Compute the 1-D discrete Fourier Transform. ..."""
```

它的函数体只声明「可被后端替换的参数是 `x`」，不算 FFT（计算交给第四层的 `pyduccfft`）：

[`_basic.py:L168`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic.py#L168) —— 函数体仅返回一个 `Dispatchable`，把 `x` 标记为可替换。

```python
    return (Dispatchable(x, np.ndarray),)
```

docstring 里给出了 DFT 的等价公式与本讲要跑的示例：

[`_basic.py:L100-L102`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic.py#L100-L102) —— 一维 `fft` 的数学等价定义，正是 4.2.1 那个公式的代码化。

```python
    If ``x`` is a 1d array, then the `fft` is equivalent to ::

        y[k] = np.sum(x * np.exp(-2j * np.pi * k * np.arange(n)/n))
```

官方示例与它的输出（注意第 2 个元素是 `8.0`，其余是 `1e-16` 量级的浮点噪声）：

[`_basic.py:L146-L152`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic.py#L146-L152) —— 构造 8 点复指数、调用 `fft`，输出在索引 1 处出现峰值 8。

```
>>> import scipy.fft
>>> import numpy as np
>>> scipy.fft.fft(np.exp(2j * np.pi * np.arange(8) / 8))
array([-2.33486982e-16+1.14423775e-17j,  8.00000000e+00-1.25557246e-15j,
        2.33486982e-16+2.33486982e-16j,  0.00000000e+00+1.22464680e-16j,
       -1.14423775e-17+2.33486982e-16j,  0.00000000e+00+5.20784380e-16j,
        1.14423775e-17+1.14423775e-17j,  0.00000000e+00+1.22464680e-16j])
```

频率排列的说明也在 docstring 里：

[`_basic.py:L104-L109`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic.py#L104-L109) —— 解释 8 点结果的频率依次为 `[0, 1, 2, 3, -4, -3, -2, -1]`，并提示用 `fftshift` 居中。

```
    The frequency term ``f=k/n`` is found at ``y[k]``. At ``y[n/2]`` we reach
    the Nyquist frequency and wrap around to the negative-frequency terms. So,
    for an 8-point transform, the frequencies of the result are
    [0, 1, 2, 3, -4, -3, -2, -1]. ...
```

> 补充一个性能细节：当 \(n\) 是 2 的幂时 FFT 最高效；对于难以分解的长度（比如素数），`scipy.fft` 会自动改用 Bluestein 算法，保证复杂度始终是 \(O(n\log n)\)。这一点写在 [`_basic.py:L96-L98`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic.py#L96-L98) 里，所以本例哪怕把 8 换成素数也不会慢得离谱。

#### 4.2.4 代码实践（核心任务：验证峰值）

**目标**：亲手跑通官方示例，验证「频率为 1 的复指数 → 输出在索引 1 出现峰值 8」。

**操作步骤**：

```python
# 示例代码：第一次 fft 调用
import numpy as np
import scipy.fft

# 1) 构造 8 点复指数信号：频率为 1（在 8 个点里走完一圈）
x = np.exp(2j * np.pi * np.arange(8) / 8)

# 2) 做正向 FFT
y = scipy.fft.fft(x)

# 3) 解读输出
print(y)                       # 看整体：只有 y[1] 应该显著
print("峰值索引:", np.argmax(np.abs(y)))   # 期望 1
print("峰值大小:", np.abs(y[1]))           # 期望 8.0
print("其余最大幅值:", np.max(np.abs(np.delete(y, 1))))  # 期望 ~0（浮点误差）
```

**需要观察的现象**：

- `y[1]` 约等于 `8+0j`，其余 7 个元素幅值都在 `1e-15` 量级（不是精确 0，是浮点舍入噪声）。
- `np.argmax(np.abs(y))` 返回 `1`，对应「正频率 1」那个位置。

**预期结果**：与 [`_basic.py:L149-L152`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic.py#L149-L152) 的 docstring 输出一致——索引 1 处为 `8.0`，其余近似为 0。

> 待本地验证：把信号频率从 `1` 改成 `2`（`np.exp(2j*np.pi*2*np.arange(8)/8)`），峰值应出现在索引 `2`；改成 `-1` 等价频率（用 `7`，因为 `e^{2πi·7j/8}=e^{-2πi·j/8}`），峰值应出现在索引 `7`（即「负频率 -1」的位置）。

#### 4.2.5 小练习与答案

**练习 1**：为什么输出里除 `y[1]` 之外都不是精确的 `0`，而是 `1e-16` 量级？

**答案**：因为浮点运算有舍入误差。数学上等比级数求和应为 0，但 `np.exp` 与求和过程中的浮点累加会留下机器精度量级（double 约 `1e-16`）的残差。这正是后续验证可逆性时要用 `np.allclose`（带容差）而不是 `==` 的原因。

**练习 2**：如果把信号长度从 8 改成 12（仍取频率 1 的复指数 `np.exp(2j*np.pi*np.arange(12)/12)`），峰值大小会变成多少、出现在哪个索引？

**答案**：峰值大小等于长度 \(n=12\)，出现在索引 `1`。因为 4.2.1 的推导对任意 \(n\) 都成立：\(y[1]=\sum_{j=0}^{n-1}1=n\)。这能帮你建立「`fft` 峰值高度 = 信号长度 × 该频率分量的幅度」的直觉。

---

### 4.3 验证可逆性：ifft(fft(x)) ≈ x

#### 4.3.1 概念说明

`ifft`（inverse FFT）是 `fft` 的逆运算。在默认归一化模式 `norm="backward"` 下，约定是：

- **正向 `fft` 不做缩放**：\(y[k] = \sum_j x[j]\,e^{-2\pi i k j/n}\)；
- **逆向 `ifft` 除以 \(n\)**：\(z[j] = \tfrac{1}{n}\sum_k y[k]\,e^{+2\pi i k j/n}\)。

把这两个式子串起来：

\[ \text{ifft}(\text{fft}(x))[j] = \frac{1}{n}\sum_{k=0}^{n-1}\left(\sum_{m=0}^{n-1} x[m]\,e^{-2\pi i k m/n}\right) e^{+2\pi i k j/n} \]

交换求和顺序，内层对 \(k\) 求和的复指数只有在 \(j=m\) 时等于 \(n\)、否则为 0（正交性），于是：

\[ \text{ifft}(\text{fft}(x))[j] = \frac{1}{n}\,x[j]\cdot n = x[j] \]

所以 `ifft(fft(x))` 在数值精度内等于 `x`。这就是本节要验证的「可逆性」。

`norm` 还有另外两种模式（`"ortho"` 两边各除 \(\sqrt{n}\)；`"forward"` 把 \(1/n\) 挪到正向）。它们的「正逆配对」同样可逆，只是缩放落点不同——u2-l1 会专门讲。

#### 4.3.2 核心流程

```
1. 取一段任意复信号 x
2. 正向：  Y = scipy.fft.fft(x)
3. 逆向：  x2 = scipy.fft.ifft(Y)
4. 比较：  np.allclose(x, x2)  →  True
```

注意第 4 步**必须用 `np.allclose`** 而不是 `==`，因为浮点误差会让两者差 `1e-15` 量级。

#### 4.3.3 源码精读

`ifft` 的对外签名与 `fft` 几乎一样，docstring 开宗明义写明了可逆关系：

[`_basic.py:L173-L180`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic.py#L173-L180) —— `ifft` 的签名与「`ifft(fft(x)) == x` 到数值精度」的承诺。

```python
def ifft(x, n=None, axis=-1, norm=None, overwrite_x=False, workers=None, *,
         plan=None):
    """
    Compute the 1-D inverse discrete Fourier Transform.
    ...
    ``ifft(fft(x)) == x`` to within numerical accuracy.
```

项目自带的测试集就用这个性质做断言。在 `tests/test_basic.py` 的 `TestFFT.test_identity` 里，对一批「2 的幂 + 素数」长度都验证了正逆配对的可逆性：

[`tests/test_basic.py:L42-L51`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/tests/test_basic.py#L42-L51) —— 对复信号验证 `ifft(fft(x))`、对实信号验证 `irfft(rfft(xr), i)` 都还原回原信号；长度特意覆盖幂与素数。

```python
class TestFFT:
    @make_xp_test_case(fft.ifft, fft.fft, fft.rfft, fft.irfft)
    def test_identity(self, xp):
        maxlen = 512
        x = xp.asarray(random(maxlen) + 1j*random(maxlen))
        xr = xp.asarray(random(maxlen))
        # Check some powers of 2 and some primes
        for i in [1, 2, 16, 128, 512, 53, 149, 281, 397]:
            xp_assert_close(fft.ifft(fft.fft(x[0:i])), x[0:i])
            xp_assert_close(fft.irfft(fft.rfft(xr[0:i]), i), xr[0:i])
```

这里有两个值得学的工程细节：

- 它**同时测了幂（2,16,128,512）和素数（53,149,281,397）**长度——素数长度会触发 4.2.3 提到的 Bluestein 算法，所以这条测试也保护了那条代码路径。
- 它用的是 `xp_assert_close`（带容差的近似相等），而不是精确相等——和我们在实践里用 `np.allclose` 是同一个道理。

#### 4.3.4 代码实践（验证可逆性）

**目标**：在本讲 4.2 的信号基础上，验证 `ifft(fft(x))` 数值上等于 `x`，并观察 `norm` 三种模式的缩放差异。

**操作步骤**：

```python
# 示例代码：验证 fft/ifft 可逆性
import numpy as np
import scipy.fft

x = np.exp(2j * np.pi * np.arange(8) / 8)

# 1) 默认 norm="backward"：ifft(fft(x)) 应回到 x
x_back = scipy.fft.ifft(scipy.fft.fft(x))
print("backward 可逆:", np.allclose(x, x_back))   # True

# 2) 观察三种 norm 下 fft 的缩放（固定一个随机复信号）
rng = np.random.default_rng(0)
s = rng.standard_normal(30) + 1j*rng.standard_normal(30)
y_default = scipy.fft.fft(s)                       # = backward
y_ortho   = scipy.fft.fft(s, norm="ortho")         # 除以 sqrt(30)
y_forward = scipy.fft.fft(s, norm="forward")       # 除以 30
print("ortho 缩放:", np.allclose(y_ortho, y_default / np.sqrt(30)))   # True
print("forward 缩放:", np.allclose(y_forward, y_default / 30))        # True

# 3) 关键：任一 norm 下，ifft(fft(s, norm=N), norm=N) 都还原 s
for N in ("backward", "ortho", "forward"):
    assert np.allclose(s, scipy.fft.ifft(scipy.fft.fft(s, norm=N), norm=N))
print("三种 norm 均可逆: True")
```

**需要观察的现象**：

- `np.allclose(x, x_back)` 返回 `True`，且两者最大差异在 `1e-15` 量级。
- `ortho` / `forward` 只是改变了 `fft` 输出的整体幅度（分别除以 \(\sqrt{n}\) 和 \(n\)），但**只要正逆用同一个 `norm`，就一定可逆**。

**预期结果**：三行 `True`，最后打印「三种 norm 均可逆: True」。

> 待本地验证：把 `np.allclose` 换成精确相等 `np.array_equal`，观察是否变成 `False`——以此亲身体会浮点误差的存在。

#### 4.3.5 小练习与答案

**练习 1**：默认 `norm="backward"` 下，为什么是 `ifft` 除以 \(n\)，而不是 `fft` 除以 \(n\)？

**答案**：这是历史约定（与 `numpy.fft` 一致）：正向变换「只求和、不缩放」，把 \(1/n\) 的归一化留到逆向变换。好处是正向 `fft` 的结果直接就是「各频率分量的加权和」，比如 4.2 里峰值高度就等于信号长度 \(n\)，直觉清晰；代价是做「频谱分析」时要记得 `ifft` 端有缩放。参见 [`_basic.py:L48-L52`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic.py#L48-L52) 对 `norm` 的说明。

**练习 2**：`tests/test_basic.py:test_identity` 为什么要同时测素数长度的可逆性，而不是只测 2 的幂？

**答案**：因为 2 的幂走的是普通 Cooley–Tukey FFT 路径，而素数长度会触发 Bluestein 算法（见 [`_basic.py:L96-L98`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic.py#L96-L98)）。同时测两者，才能保证两条不同的计算路径都满足可逆性。这是测试设计里「覆盖不同实现分支」的典型做法。

---

### 4.4 运行测试：PytestTester

#### 4.4.1 概念说明

很多 Python 包都附带测试，但调用方式五花八门。SciPy 给所有子包统一提供了一个便捷入口：每个子包都有一个 `test()` 方法，调用它就只跑**这个子包**的测试。所以：

- `scipy.test()` 跑整个 SciPy 的测试（很慢）；
- `scipy.fft.test()` 只跑 `scipy/fft/` 下的测试（快得多）。

这个 `test` 不是普通函数，而是 4.1 里看到的 `PytestTester(__name__)` 实例。它的本质是：**一个记住了「自己属于哪个模块」的对象，被调用时把该模块的测试交给 pytest 去跑**。

#### 4.4.2 核心流程

`scipy.fft.test()` 被调用时，内部大致经历：

```
1. 用记下的模块名 "scipy.fft" 从 sys.modules 取出模块对象
2. 取模块的磁盘路径（即 scipy/fft/ 的安装位置）
3. 组装 pytest 命令行参数：
   - 默认 ['--showlocals', '--tb=short']
   - label="fast" → 追加 ["-m", "not slow"]   （跳过慢测试）
   - 追加 ['--pyargs', 'scipy.fft']
4. 调用 pytest.main(pytest_args) 执行
5. 返回 (退出码 == 0)，即「全部通过则 True」
```

它还接受几个常用参数：`label`（`"fast"`/`"full"`，控制是否跑慢测试）、`verbose`（详细度）、`doctests`（是否跑 docstring 里的示例）、`tests`（指定只跑某些模块）、`parallel`（用 pytest-xdist 并行）。

#### 4.4.3 源码精读

`PytestTester` 定义在共用的工具库里。它的 `__call__` 方法就是把上面流程翻译成代码：

[`scipy/_lib/_testutils.py:L63-L142`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_testutils.py#L63-L142) —— `PytestTester` 类：`__init__` 记下模块名，`__call__` 把它转成一次 `pytest.main` 调用并返回布尔结果。

```python
class PytestTester:
    def __init__(self, module_name):
        self.module_name = module_name

    def __call__(self, label="fast", verbose=1, extra_argv=None, doctests=False,
                 coverage=False, tests=None, parallel=None):
        import pytest
        module = sys.modules[self.module_name]
        module_path = os.path.abspath(module.__path__[0])
        pytest_args = ['--showlocals', '--tb=short']
        ...
        if label == "fast":
            pytest_args += ["-m", "not slow"]
        ...
        if tests is None:
            tests = [self.module_name]
        pytest_args += ['--pyargs'] + list(tests)
        try:
            code = pytest.main(pytest_args)
        except SystemExit as exc:
            code = exc.code
        return (code == 0)
```

读这段能学到几个细节：

- `tests` 默认就是 `[self.module_name]`，也就是 `["scipy.fft"]`——这正是「只跑本子包」的实现方式（靠 `--pyargs scipy.fft` 让 pytest 自己解析模块路径）。
- `label="fast"` 翻译成 pytest 的 marker 过滤 `-m "not slow"`，所以默认会跳过标了 `@pytest.mark.slow` 的测试。想全跑就 `scipy.fft.test("full")`。
- 返回值是 `(code == 0)`：pytest 退出码为 0 表示全部通过，所以 `test()` 返回 `True` 即代表绿。

我们 4.3 引用过的可逆性测试 `test_identity`，正是这个 `test()` 会收集并执行的众多测试之一（见 [`tests/test_basic.py:L42-L51`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/tests/test_basic.py#L42-L51)）。

#### 4.4.4 代码实践（跑测试）

**目标**：用 `scipy.fft.test()` 跑通子包测试，并学会「只跑某一个测试」的精细控制。

**操作步骤**：

1. 最简单的调用（默认只跑 fast 测试）：

   ```python
   import scipy.fft
   ok = scipy.fft.test()
   print("全部通过:", ok)   # True 表示绿
   ```

2. 如果嫌全跑太慢，可以用 `extra_argv` 把范围缩小到某个具体测试（这是 pytest 的 `-k` 表达式）：

   ```python
   # 示例代码：只跑名字里含 identity 的测试
   ok = scipy.fft.test(extra_argv=["-k", "identity"])
   ```

3. 想看到 docstring 示例也被验证（比如 4.2 的 8 点示例就是 `fft` 的 docstring 示例），打开 `doctests`：

   ```python
   ok = scipy.fft.test(doctests=True, extra_argv=["-k", "fft"])
   ```

**需要观察的现象**：

- 第 1 步会打印 pytest 的收集与执行结果，末尾出现类似 `=== no tests ran in ...` 或 `=== N passed in ...s ===` 的总结行；返回值 `ok` 为 `True`。
- 第 2 步只收集到少量测试（名字含 `identity` 的），执行更快。
- 第 3 步会额外执行 `_basic.py` 里 `fft` 的 docstring 示例（即 [`_basic.py:L146-L152`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic.py#L146-L152) 那段），相当于自动帮你验证本讲 4.2 的结果。

**预期结果**：测试全部通过，`ok` 为 `True`。

> 待本地验证：完整 `scipy.fft.test()` 的耗时与通过数取决于你的机器与 scipy 版本；若个别测试因环境（如缺 pytest-xdist）被跳过，属正常现象，看输出的 `skipped` 计数即可。

> 另一种等价做法：在命令行直接用 pytest 跑测试目录，效果与 `scipy.fft.test()` 相近：
> ```bash
> python -m pytest --pyargs scipy.fft -m "not slow"
> ```

#### 4.4.5 小练习与答案

**练习 1**：`scipy.fft.test()` 默认会不会运行标了 `@pytest.mark.slow` 的测试？想让它们也跑该怎么办？

**答案**：默认**不会**。因为 `__call__` 里 `label="fast"` 会追加 `["-m", "not slow"]`（见 [`_testutils.py:L118-L119`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_testutils.py#L118-L119)），把带 `slow` 标记的排除掉。想全跑就传 `label="full"`：`scipy.fft.test("full")`。

**练习 2**：为什么 `scipy.fft.test()` 只跑 `scipy/fft/` 下的测试，而不是整个 SciPy 的？

**答案**：因为 `__init__.py` 用 `PytestTester(__name__)` 创建实例时，`__name__` 就是 `"scipy.fft"`；`__call__` 又把 `tests` 默认设为 `[self.module_name]`（见 [`_testutils.py:L123-L124`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_testutils.py#L123-L124)），最终传给 pytest 的是 `--pyargs scipy.fft`，pytest 就只解析并收集这一个子包下的测试。

## 5. 综合实践：一个完整的「导入→变换→验证→测试」脚本

把本讲四个模块串起来，写一个能从头跑到尾的小脚本 `my_first_fft.py`（写在你自己的工作目录即可，不要写进讲义目录）。它综合考察：导入是否成功、能否构造信号并读懂 `fft` 输出、能否验证可逆性、以及能否调用子包测试。

```python
# 示例代码：my_first_fft.py —— 综合实践
import numpy as np
import scipy.fft

# --- 第 1 步：导入自检 ---
assert callable(scipy.fft.fft), "scipy.fft 未正确导入"
print("[1] 导入成功，scipy 版本:", np.__doc__ is not None or "ok")

# --- 第 2 步：构造 8 点复指数，做 fft，验证峰值 ---
x = np.exp(2j * np.pi * np.arange(8) / 8)
y = scipy.fft.fft(x)
peak_idx = int(np.argmax(np.abs(y)))
assert peak_idx == 1, f"峰值应在索引 1，实际 {peak_idx}"
assert np.isclose(np.abs(y[1]), 8.0), f"峰值应=8，实际 {np.abs(y[1])}"
print(f"[2] fft 峰值在索引 {peak_idx}，大小 {np.abs(y[1]):.1f}（期望 8.0）")

# --- 第 3 步：验证可逆性（默认 backward + 另两种 norm）---
assert np.allclose(x, scipy.fft.ifft(scipy.fft.fft(x)))
rng = np.random.default_rng(0)
s = rng.standard_normal(30) + 1j*rng.standard_normal(30)
for N in ("backward", "ortho", "forward"):
    assert np.allclose(s, scipy.fft.ifft(scipy.fft.fft(s, norm=N), norm=N))
print("[3] ifft(fft(x)) ≈ x，三种 norm 均可逆")

# --- 第 4 步：跑子包测试（只挑 identity，避免太久）---
ok = scipy.fft.test(extra_argv=["-k", "identity"])
print("[4] scipy.fft 相关测试通过:", ok)
```

**验收标准**：

1. 脚本不抛异常跑完，四行 `[1]..[4]` 全部打印。
2. 第 2 步断言通过，证明你读懂了「频率 1 → 索引 1 峰值 8」。
3. 第 3 步三种 norm 全部可逆。
4. 第 4 步 `ok` 为 `True`——这一步等于让 SciPy 自己的测试替你确认本讲学到的行为是正确的。

> 待本地验证：若你的环境里 `scipy.fft.test(extra_argv=["-k","identity"])` 收集到的用例数为 0，多半是 pytest 的 `-k` 表达式与版本有关；可改成不传 `extra_argv` 直接 `scipy.fft.test()` 全跑，或改用命令行 `python -m pytest --pyargs scipy.fft -k identity`。

## 6. 本讲小结

- 安装 `scipy.fft` 就是安装 `scipy`；使用时推荐显式 `import scipy.fft`，入口文件 [`__init__.py`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/__init__.py) 只做「搬运函数 + 注册 `test`」两件事。
- 8 点复指数 `np.exp(2j*np.pi*np.arange(8)/8)` 经 `fft` 后，输出在索引 1 处出现峰值 8，其余为浮点噪声——这印证了 DFT 公式 \(y[k]=\sum_j x[j]e^{-2\pi i k j/n}\)。
- `fft` 输出按「先正频、后负频」排列（8 点对应频率 `[0,1,2,3,-4,-3,-2,-1]`），索引 1 即「正频率 1」。
- 默认 `norm="backward"` 下 `ifft(fft(x))` 数值上等于 `x`；验证要用 `np.allclose` 而非 `==`，项目测试 [`tests/test_basic.py:test_identity`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/tests/test_basic.py#L42-L51) 用的也是带容差比较。
- `scipy.fft.test()` 是一个 `PytestTester` 实例（见 [`_testutils.py:L63-L142`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_testutils.py#L63-L142)），内部把 `"scipy.fft"` 翻译成一次 `pytest.main` 调用；默认 `label="fast"` 会跳过慢测试。
- 至此你完成了从「读源码」到「跑代码」的跨越：四层架构（u1-l2）在运行时对外是透明的，你只需调用 `scipy.fft.fft` 即可。

## 7. 下一步学习建议

本讲只让 `fft` 跑起来、并验证了最基本的可逆性。接下来建议：

1. **深入 `fft` 的参数语义**：进入 u2-l1《复数一维 FFT：fft/ifft 与 norm、n、axis》，那里会逐个拆解 `n`（截断/补零）、`axis`、`norm` 三模式、`workers`、`plan`，并解释本讲一带而过的 Bluestein 算法。
2. **理解「空壳函数如何变成计算」**：本讲多次提到 `fft` 函数体只是 `return (Dispatchable(...),)`。想看清这背后的魔法，可直接跳到 u4-l1《uarray 多方法与 `_dispatch`》。
3. **跑一次完整的子包测试**：用 `scipy.fft.test()` 全量跑一遍，观察哪些测试覆盖了你将来要学的 `rfft`/`dct`/`fht`，提前建立全局印象。
4. **想看「计算核心」**：u5 单元会进入 `_duccfft`，揭示本讲那行 `scipy.fft.fft(x)` 最终是如何落到 C 扩展 `pyduccfft` 的。

一句话记住本讲：**`import scipy.fft` 之后，一次 `fft` 调用就能把一段复指数信号变成频域里的一个尖峰，而 `scipy.fft.test()` 让 SciPy 自己证明这一切可逆。**
