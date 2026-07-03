# 卷积/相关接口与 mode/boundary 语义

## 1. 本讲目标

本讲是「卷积与相关」单元（单元 3）的第一篇，也是整个 scipy.signal 里最常被调用的一组函数的入门。

读完本讲，你应当能够：

- 说清**卷积（convolve）**与**相关（correlate）**在数学上只差一个「翻转」，并能从源码看出 scipy 如何用这一关系让两者互相复用。
- 准确描述 `mode` 三种取值 `full` / `same` / `valid` 决定的**输出尺寸**，以及它们对应的整数编码。
- 准确描述二维卷积 `boundary` 三种取值 `fill` / `wrap` / `symm` 决定的**边界延拓**方式，并看懂 scipy 把这些字符串打包进一个整数的「位域」技巧。
- 理解内部辅助函数 `_valfrommode`、`_bvalfromboundary`、`_inputs_swap_needed` 如何把人类可读的字符串参数，翻译成底层 C 内核 `_sigtools._convolve2d` 需要的数字标志。

本讲只覆盖**直接法（method='direct'）**与接口语义；频域卷积 `fftconvolve`、自动方法选择 `choose_conv_method` 留给本单元后续讲义（u3-l2、u3-l3）。

## 2. 前置知识

本讲依赖你在 **u2-l4（经典窗函数的实现细节）** 中建立的认知：窗函数（Hann、Hamming、Kaiser……）本质上是一段短的、有限长的加权系数序列。把这样一段「核」沿信号滑动、逐点相乘求和，就得到了本讲要讲的**卷积/相关**运算。

用一句话衔接：**窗是「静态的核」，卷积/相关是「让核动起来」的机制**。例如 `convolve` 的官方示例正是「把方波信号与一个 Hann 窗做卷积」来平滑信号。

此外，你还需要以下最基础的概念：

- **离散序列**：用下标索引的数值数组，如 `x[0], x[1], …, x[N-1]`。
- **复共轭（conjugate）**：复数 `a+bj` 的共轭是 `a-bj`，记作 `y*`。实数序列的共轭是它自身。
- **位运算（bit operation）**：把多个小整数「挤」进同一个整数的不同二进制位，是 C 语言里常见的轻量级「打包」手段。本讲会用到左移 `<<` 与按位与 `&`。
- **shape / ndim**：NumPy 数组的形状与维数，本讲里「1-D」「2-D」「N-D」会反复出现。

如果你还不知道 `scipy.signal` 的函数是怎么从私有模块「编织」到公开命名空间的，建议先读 **u1-l4（公共命名空间与 API 导出链路）**；本讲里出现的 `correlate`、`convolve` 等都定义在私有模块 `_signaltools.py` 中。

## 3. 本讲源码地图

本讲几乎全部围绕一个文件展开，再带两个 C/C++ 头文件用于理解「位打包」：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [_signaltools.py](_signaltools.py) | 卷积/相关/滤波等核心算法的纯 Python 实现 | `correlate`、`convolve`、`convolve2d`、`correlate2d` 及三个内部映射辅助函数 |
| [_sigtools.hh](_sigtools.hh) | 编译扩展 `_sigtools` 的共享 C++ 头文件 | 模式/边界/翻转/类型的「位掩码」常量定义 |
| [_sigtoolsmodule.cc](_sigtoolsmodule.cc) | `_sigtools` 扩展的 C 实现 | `_convolve2d` 如何解析打包后的整数 `flag` |

一句话概括它们的协作：**Python 层把字符串参数翻译成整数，C 层用一个整数同时携带「模式 + 边界 + 是否翻转 + 数据类型」四类信息。**

> 说明：本讲引用的行号基于当前 HEAD `ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10`。

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：

1. **卷积与相关：定义与「翻转」差异**（`correlate`、`convolve`、`_reverse_and_conj`）
2. **输出尺寸与 `mode` 模式**（`_valfrommode`、`_modedict`、`correlation_lags`、`_centered`）
3. **二维边界处理与位打包**（`convolve2d`、`correlate2d`、`_bvalfromboundary`、`_boundarydict`、`_sigtools.hh`）
4. **内核统一与输入交换**（`_inputs_swap_needed`、`_np_conv_ok`、`_sigtools._convolve2d` 的 flip 标志）

---

### 4.1 卷积与相关：定义与「翻转」差异

#### 4.1.1 概念说明

**卷积**衡量的是「一个信号经过某个系统（核/滤波器）后的输出」。对两个离散序列 \(x\)（长 \(N\)）和 \(h\)（长 \(M\)），线性卷积定义为：

\[
(x * h)[n] \;=\; \sum_{m} x[m]\, h[n-m]
\]

注意核 \(h\) 的下标是 \(n-m\)，也就是**先把 \(h\)「左右翻转」再滑动**。

**（互）相关**衡量的是「一个信号与另一个信号在不同位移上的相似度」：

\[
(x \star y)[k] \;=\; \sum_{m} x[m]\, y^{*}[m-k]
\]

注意这里 \(y\) 的下标是 \(m-k\)（带个负号但没有整体翻转），并且对复数情形取共轭 \(y^{*}\)。

把两个式子放在一起对比，就能得到本讲最重要的一个直觉：

> **相关 = 卷积一个「翻转并取共轭」过的核。**

即：

\[
(x \star y)[k] \;=\; (x * \mathrm{rev\text{-}conj}(y))[k]
\]

其中 \(\mathrm{rev\text{-}conj}(y)\) 表示把 \(y\) 在所有维度上翻转（reverse）并对复数取共轭（conjugate）。这正是 scipy 在源码里反复利用的等价关系——**让 `correlate` 和 `convolve` 互相复用同一套底层计算**。

对**实数**序列（最常见的情形），共轭不起作用，差异就只剩「翻转」。所以对同一个实数核 `h`：

- `convolve(x, h)`：先把 `h` 翻转，再滑动相乘求和。
- `correlate(x, h)`：不翻转 `h`，直接滑动相乘求和。

#### 4.1.2 核心流程

scipy 用「互递归」实现了上面那个等价关系，流程非常对称：

```
correlate(in1, in2, method='fft'/'auto')
   └──> convolve(in1, _reverse_and_conj(in2), mode, method)   # 相关 = 卷积(翻转核)

convolve(in1, in2, method='direct')            # 直接法分支
   └──> correlate(volume, _reverse_and_conj(kernel), mode, 'direct')  # 卷积 = 相关(翻转核)
```

也就是说：

- 走 **FFT/自动** 路径时，`correlate` 把问题转交给 `convolve`（因为频域做卷积更快，详见 u3-l2）。
- 走 **direct** 路径时，`convolve` 反过来把问题转交给 `correlate`（因为直接法的「N-D 相关」C 内核 `_sigtools._correlateND` 已经写好，详见 4.4 与 u3-l4）。

二者之间唯一的「胶水」就是 `_reverse_and_conj`：把数组在所有维度翻转，并对复浮点取共轭。

#### 4.1.3 源码精读

先看公共入口 `correlate` 的关键分支（签名与文档省略，直接看实现）：

[_signaltools.py:235-257](_signaltools.py#L235-L257) —— `correlate` 取得数组命名空间后，对 `fft`/`auto` 方法直接转交给 `convolve`，把 `in2` 先「翻转+共轭」：

```python
    xp = array_namespace(in1, in2)
    in1 = xp.asarray(in1)
    in2 = xp.asarray(in2)
    ...
    # this either calls fftconvolve or this function with method=='direct'
    if method in ('fft', 'auto'):
        return convolve(in1, _reverse_and_conj(in2, xp), mode, method)
```

再看 `convolve` 的 `direct` 分支，它做的是完全对称的事：

[_signaltools.py:1530-1539](_signaltools.py#L1530-L1539) —— 直接法卷积转交给 `correlate`，把核先「翻转+共轭」：

```python
    elif method == 'direct':
        # fastpath to faster numpy.convolve for 1d inputs when possible
        if _np_conv_ok(volume, kernel, mode, xp):
            ...
            out = np.convolve(a_volume, a_kernel, mode)
            return xp.asarray(out)
        return correlate(volume, _reverse_and_conj(kernel, xp), mode, 'direct')
```

把这两段对照看，就能体会到「卷积 ↔ 相关」的对称美：**谁走直接法，谁就把对方的输入翻转一次。**

而「翻转 + 共轭」本身实现得很短：

[_signaltools.py:1180-1196](_signaltools.py#L1180-L1196) —— `_reverse_and_conj`：用 `slice(None, None, -1)` 在所有维度反向切片（Torch 后端因不支持负步长切片而改用 `flip`），再对复浮点取共轭：

```python
def _reverse_and_conj(x, xp):
    """Reverse array `x` in all dimensions and perform the complex conjugate"""
    if not is_torch(xp):
        reverse = (slice(None, None, -1),) * x.ndim
        x_rev = x[reverse]
    else:
        x_rev = xp.flip(x)
    if xp.isdtype(x.dtype, 'complex floating'):
        return xp.conj(x_rev)
    else:
        return x_rev
```

> 小贴士：`slice(None, None, -1)` 就是 Python 里 `arr[::-1]` 的数组化写法，表示「这一维从头到尾、步长 -1」，即反转。`x.ndim` 个这样的切片拼成元组，就能反转所有维度。

#### 4.1.4 代码实践

**实践目标**：用同一个一维实数核，亲手验证「卷积翻转、相关不翻转」。

**操作步骤**：

1. 取一个有特征的小核，比如 `h = [0, 1, 0.5]`（注意它**不对称**，这样才能看出翻转差异），取信号 `x = [1, 2, 3]`。
2. 分别调用 `signal.convolve(x, h)` 与 `signal.correlate(x, h)`（默认 `mode='full'`）。
3. 手算一遍验证：卷积要先把 `h` 翻转成 `[0.5, 1, 0]` 再滑动。

**需要观察的现象**：两个结果应当不同，且 `correlate(x, h)` 应当等于 `convolve(x, h[::-1])`。

**预期结果**（核为实数，无共轭；可手算核对）：

- `convolve([1,2,3], [0,1,0.5])`：把核翻转为 `[0.5, 1, 0]`，滑动求和得 `[0.5, 2.0, 3.5, 3.0, 0.0]`。
- `correlate([1,2,3], [0,1,0.5])`：核不翻转，滑动求和得 `[0.0, 3.0, 3.5, 2.0, 0.5]`。

可以看到两者正好是彼此的**反转**——这正是「翻转」差异的直接体现。

> 说明：上面结果是按定义手算的，建议本地运行 `signal.convolve` / `signal.correlate` 对照确认（若环境已编译 `_sigtools`，结果应与手算一致；若不便运行，记为「待本地验证」）。

#### 4.1.5 小练习与答案

**练习 1**：如果把核换成对称的 `h = [1, 2, 1]`，`convolve(x, h)` 与 `correlate(x, h)` 还会不同吗？为什么？

**答案**：相同。因为对称核翻转后等于自身，`_reverse_and_conj(h) = h`，于是卷积与相关退化为同一运算。这也是为什么很多「平滑核」（如箱型、对称窗）做卷积和相关看不出差别。

**练习 2**：复数信号 `y = [1j, 2, 3]`，`correlate(x, y)` 比「直接对 `y` 反转」多做了哪一步？

**答案**：多了**取共轭**。`_reverse_and_conj` 在检测到 `complex floating` dtype 时会调用 `xp.conj`。这一点在匹配滤波（matched filter）等需要内积的场合很重要。

---

### 4.2 输出尺寸与 `mode` 模式

#### 4.2.1 概念说明

卷积/相关时，核会「探出」信号的边界。输出到底取多大、从哪里截取，由 `mode` 参数决定。scipy 提供三种模式（对 1-D 输入长 \(N\)、核长 \(M\)，且通常 \(N \ge M\)）：

| `mode` | 输出长度 | 含义 |
| --- | --- | --- |
| `'full'`（默认） | \(N + M - 1\) | 所有「核与信号有重叠」的位移都保留，含依赖零填充的边缘点 |
| `'same'` | \(N\) | 与**第一个输入** `in1` 等长，取 `'full'` 的居中段 |
| `'valid'` | \(\max(N,M) - \min(N,M) + 1\) | 只保留「核完全落在信号内部、不碰任何零填充」的位移 |

直觉记忆：

- **full**：能算的全算出来 → 最长。
- **same**：输出与输入同长 → 最常用于「滤波后还想和原信号对齐画图」。
- **valid**：只算「干净的」→ 最短，且**要求两个输入中至少有一个在每一维都不小于另一个**（否则没有完全重叠的位置）。

> 注意一个坑：`'same'` 模式下，当输入为**偶数**长度时，`correlate` 与 `correlate2d` 的输出会有 1 个样本的索引偏移（见 `correlate` 文档 [_signaltools.py:177-178](_signaltools.py#L177-L178)）。这是因为「居中截取」在偶数长度下没有唯一中心点。

#### 4.2.2 核心流程

scipy 不把 `mode` 字符串直接传给 C 内核，而是先映射成一个小整数：

```
mode 字符串 ──(_modedict / _valfrommode)──> 整数 {0,1,2}
                                              0=VALID, 1=SAME, 2=FULL
```

这个整数同时承担两个职责：

1. **决定输出 shape**：`full` → 各维 `i+j-1`；`valid` → `i-j+1`；`same` → 与 `in1` 同形。
2. **作为整数编码传给 C 内核**（见 4.3 的位打包）。

#### 4.2.3 源码精读

模式字典与映射函数位于文件开头，极为简短：

[_signaltools.py:45-56](_signaltools.py#L45-L56) —— `_modedict` 把三个字符串映射到整数；`_valfrommode` 用字典查找把字符串转成整数，遇到非法值抛 `ValueError`：

```python
_modedict = {'valid': 0, 'same': 1, 'full': 2}
...
def _valfrommode(mode):
    try:
        return _modedict[mode]
    except KeyError as e:
        raise ValueError("Acceptable mode flags are 'valid',"
                         " 'same', or 'full'.") from e
```

`convolve2d` / `correlate2d` 都通过 `_valfrommode` 拿到这个整数（见 4.3）。

而 `correlate` 走的是一条**故意不同**的路：

[_signaltools.py:248-253](_signaltools.py#L248-L253) —— `correlate` 不调用 `_valfrommode`，而是直接查 `_modedict`，注释明确说明原因：

```python
    # Don't use _valfrommode, since correlate should not accept numeric modes
    try:
        val = _modedict[mode]
    except KeyError as e:
        raise ValueError("Acceptable mode flags are 'valid',"
                         " 'same', or 'full'.") from e
```

这是一个历史包袱相关的细节：早期 `_correlateND` 内核接受**数字**模式（如直接传 `2`），而现在的 Python `correlate` 不再允许用户传数字，只接受字符串。由于 `_modedict` 的键是字符串，传整数（如 `mode=2`）会触发 `KeyError` → `ValueError`，从而被拒绝。这段注释就是在提醒维护者：「别为了图省事改成接受数字」。

输出 shape 的计算可以在 `correlate` 的 direct 分支里看得很清楚：

[_signaltools.py:281-300](_signaltools.py#L281-L300) —— `valid` 用 `i-j+1`，`full`/`same` 用 `i+j-1` 先建零填充缓冲，再按 mode 选 `out` 的形状：

```python
        if mode == 'valid':
            ps = [i - j + 1 for i, j in zip(in1.shape, in2.shape)]
            out = np.empty(ps, a_in1.dtype)
            z = _sigtools._correlateND(a_in1, a_in2, out, val)
        else:
            ps = [i + j - 1 for i, j in zip(in1.shape, in2.shape)]
            in1zpadded = np.zeros(ps, a_in1.dtype)
            ...
            if mode == 'full':
                out = np.empty(ps, a_in1.dtype)
            elif mode == 'same':
                out = np.empty(in1.shape, a_in1.dtype)
            z = _sigtools._correlateND(in1zpadded, a_in2, out, val)
```

配套的两个小工具也值得一看：

- [_signaltools.py:315-411](_signaltools.py#L315-L411) `correlation_lags`：返回每个输出样本对应的「位移（lag）」索引。例如 `full` 模式下 lag 范围是 `np.arange(-in2_len + 1, in1_len)`，正好对应 \([-(M-1),\, N-1]\)。它常与 `correlate` 配套，用 `lags[np.argmax(corr)]` 找出「最匹配的位移」。
- [_signaltools.py:414-421](_signaltools.py#L414-L421) `_centered`：从 `full` 输出里截取居中段得到 `same` 输出，本质是 `startind = (currshape - newshape) // 2` 的切片（频域卷积路径会用它）。

#### 4.2.4 代码实践

**实践目标**：对照 `_valfrommode`，亲眼看 `mode` 字符串如何映射为整数，并验证三种模式的输出长度公式。

**操作步骤**：

1. 在 Python 里导入内部映射（它们是私有函数，但可访问）：
   ```python
   from scipy.signal._signaltools import _valfrommode, _modedict
   print(_modedict)                 # 看整张映射表
   print(_valfrommode('full'))      # 期望 2
   print(_valfrommode('same'))      # 期望 1
   print(_valfrommode('valid'))     # 期望 0
   ```
2. 取 `x = range(8)`（\(N=8\)）、`h = [1, 1, 1]`（\(M=3\)），分别用三种模式调用 `signal.correlate(x, h)`，打印每个结果的 `len()`。
3. 用公式核对：`full`→\(8+3-1=10\)；`same`→\(8\)；`valid`→\(8-3+1=6\)。

**需要观察的现象**：`_valfrommode` 的返回值与上表完全一致；三个输出长度等于公式预测值。

**预期结果**：`_modedict == {'valid': 0, 'same': 1, 'full': 2}`；输出长度分别为 `10 / 8 / 6`。

> 说明：`_valfrommode` / `_modedict` 是私有实现细节，未来版本可能调整，本步骤仅用于理解原理，不建议在生产代码里依赖。

#### 4.2.5 小练习与答案

**练习 1**：`mode='valid'` 时，如果 `in1` 比 `in2` 还短，会发生什么？

**答案**：会抛 `ValueError`。因为 `valid` 要求「至少有一个输入在每一维都不小于另一个」，否则不存在「核完全落在信号内部」的位移。这个检查在 `_inputs_swap_needed` 里完成（见 4.4）。

**练习 2**：`mode='same'` 的输出为什么是「与 `in1` 同长」而不是「与较大输入同长」？

**答案**：这是 scipy 的约定（与 NumPy 的 `'same'` 不同，NumPy 用较大输入的尺寸，这也是 `_np_conv_ok` 要做特判的原因之一，见 4.4）。这样设计让 `same` 模式天然适合「滤波后与原信号逐点对齐」的用法。

---

### 4.3 二维边界处理与位打包

#### 4.3.1 概念说明

到二维（及更高维）情形，`convolve2d` / `correlate2d` 多了一个 `boundary` 参数：当核「探出」图像边界时，**外面那些「不存在」的像素该取什么值？** 三种选择：

| `boundary` | 别名 | 行为 | 类比 NumPy `np.pad` |
| --- | --- | --- | --- |
| `'fill'`（默认） | `'pad'` | 用 `fillvalue`（默认 0）填充外部 | `'constant'` |
| `'wrap'` | `'circular'` | 环绕：把图像当成周期重复 | `'wrap'` |
| `'symm'` | `'symmetric'` | 镜像反射：沿边界把图像翻转延拓 | `'reflect'`（偶对称） |

直觉上：

- **fill**：边界外是「黑」（0 或指定值）→ 边缘会变暗/受填充值拉扯。
- **wrap**：图像左右上下首尾相接 → 适合本身就是周期性的信号（如 360° 全景图）。
- **symm**：边界外是镜像 → 边缘过渡更平滑，**常用于图像梯度/边缘检测**（`convolve2d` 文档示例就是用 `boundary='symm'` 算 Scharr 梯度）。

> 二者还有一个共享参数 `fillvalue`：仅在 `boundary='fill'` 时生效，决定填充的数值。

#### 4.3.2 核心流程：把字符串「打包」进一个整数

C 内核 `_sigtools._convolve2d` 不接受字符串，只接受整数。scipy 的做法是：**把 mode、boundary、是否翻转、数据类型四类信息，各自占据同一个整数的不同二进制位**，拼成一个 `flag` 传进去。

这是经典的「位域（bit-field）」打包。我们可以画出这个整数的位布局（低位在右）：

```
 位:   9 8 7 6 5   |    4    |  3 2  |  1 0
       └─类型──┘   └是否翻转┘ └边界┘ └模式┘
     (typenum<<5)   FLIP_MASK  BOUNDARY  OUTSIZE
                                        _MASK=3
                                        VALID=0/SAME=1/FULL=2
                              _MASK=12 (位2-3)
                              PAD=0 / REFLECT=4 / CIRCULAR=8
```

Python 侧负责「填低 4 位」：

```
val  = _valfrommode(mode)              # 占位 0-1：0/1/2
bval = _bvalfromboundary(boundary)     # 占位 2-3：0/4/8（已左移 2 位）
```

C 侧再把高位（类型、翻转）补齐并解析：

```
flag = mode + boundary + (typenum << TYPE_SHIFT) + (flip != 0) * FLIP_MASK
```

#### 4.3.3 源码精读

先看 Python 侧的边界字典与映射函数：

[_signaltools.py:47-64](_signaltools.py#L47-L64) —— `_boundarydict` 给每个字符串一个「原始码」，`_bvalfromboundary` 再 `<< 2` 把它推进位 2-3：

```python
_boundarydict = {'fill': 0, 'pad': 0, 'wrap': 2, 'circular': 2, 'symm': 1,
                 'symmetric': 1, 'reflect': 4}
...
def _bvalfromboundary(boundary):
    try:
        return _boundarydict[boundary] << 2
    except KeyError as e:
        raise ValueError("Acceptable boundary flags are 'fill', 'circular' "
                         "(or 'wrap'), and 'symmetric' (or 'symm').") from e
```

逐一核对三种**可用**边界如何落到 C 的常量上（`'fill'→0<<2=0`、`'wrap'→2<<2=8`、`'symm'→1<<2=4`）：

| boundary 字符串 | 原始码 | `<<2` 后 | 对应 C 常量 | 含义 |
| --- | --- | --- | --- | --- |
| `'fill'` / `'pad'` | 0 | 0 | `PAD` | 填充 |
| `'wrap'` / `'circular'` | 2 | 8 | `CIRCULAR` | 环绕 |
| `'symm'` / `'symmetric'` | 1 | 4 | `REFLECT` | 镜像 |

> 注：字典里还有一项 `'reflect': 4`，它 `<<2` 后等于 16，会撞上位 4（`FLIP_MASK`），并不是 `convolve2d` 的合法边界（C 内核会拒绝它）。`convolve2d`/`correlate2d` 文档只暴露 `fill`/`wrap`/`symm` 三种；此处列为「待确认的遗留项」，不建议使用。

C 侧的掩码常量定义在共享头里，干净利落：

[_sigtools.hh:6-18](_sigtools.hh#L6-L18) —— 位掩码与模式/边界常量：

```cpp
#define BOUNDARY_MASK 12      // 位 2-3
#define OUTSIZE_MASK 3        // 位 0-1
#define FLIP_MASK  16         // 位 4
#define TYPE_MASK  (32+64+128+256+512)  // 位 5-9
#define TYPE_SHIFT 5

#define FULL  2
#define SAME  1
#define VALID 0

#define CIRCULAR 8
#define REFLECT  4
#define PAD      0
```

最后看 C 内核如何用这个打包好的整数。`_convolve2d` 的函数签名直接暴露了它的参数顺序：

[_sigtoolsmodule.cc:678](_sigtoolsmodule.cc#L678) —— 文档字符串说明参数含义：

```c
static char doc_convolve2d[] = "out = _convolve2d(in1, in2, flip, mode, boundary, fillvalue)";
```

注意第 3 个参数是 `flip`，第 4 是 `mode`，第 5 是 `boundary`。Python 调用时正是按这个顺序传参。然后 C 用 `mode & OUTSIZE_MASK` 提取低 2 位来决定输出尺寸：

[_sigtoolsmodule.cc:740-766](_sigtoolsmodule.cc#L740-L766) —— 用掩码从打包整数里取出模式，计算各维输出长度：

```c
    switch(mode & OUTSIZE_MASK) {
    case VALID:  ... aout_dimens[i] = DIMS(ain1)[i] - DIMS(ain2)[i] + 1; ...
    case SAME:   ... aout_dimens[i] = DIMS(ain1)[i]; ...
    case FULL:   ... aout_dimens[i] = DIMS(ain1)[i] + DIMS(ain2)[i] - 1; ...
    }
```

而把四类信息拼成最终 `flag` 的那一行，是整个位打包的「合龙」之处：

[_sigtoolsmodule.cc:772-773](_sigtoolsmodule.cc#L772-L773) —— 把模式、边界、类型、翻转拼进一个整数交给底层运算：

```c
    flag = mode + boundary + (typenum << TYPE_SHIFT) + \
      (flip != 0) * FLIP_MASK;
```

#### 4.3.4 代码实践

**实践目标**：用 `convolve2d` 对比 `boundary='fill'` 与 `'wrap'` 在图像边缘的差异，并对照 `_valfrommode`/`_bvalfromboundary` 说明字符串如何映射为整数编码。

**操作步骤**：

1. 复用测试里的小数组（这些期望值来自 [_signaltools.py 测试 test_2d_arrays](_signaltools.py#L295-L302)，是权威参考）：
   ```python
   import numpy as np
   from scipy import signal
   a = np.array([[1, 2, 3], [3, 4, 5]])
   b = np.array([[2, 3, 4], [4, 5, 6]])
   ```
2. 算默认（full + fill）结果，对照测试期望：
   ```python
   signal.convolve2d(a, b)
   # 期望（来自 test_2d_arrays）:
   # [[ 2,  7, 16, 17, 12],
   #  [10, 30, 62, 58, 38],
   #  [12, 31, 58, 49, 30]]
   ```
3. 改成 `boundary='wrap'` 再算一次，对照测试期望（见 [test_wrap_boundary](_signaltools.py#L358-L365)）：
   ```python
   signal.convolve2d(a, b, 'full', 'wrap')
   # 期望:
   # [[80, 80, 74, 80, 80],
   #  [68, 68, 62, 68, 68],
   #  [80, 80, 74, 80, 80]]
   ```
4. 用内部函数查看这次调用传给 C 的整数编码：
   ```python
   from scipy.signal._signaltools import _valfrommode, _bvalfromboundary
   print(_valfrommode('full'), _bvalfromboundary('wrap'))   # 期望 2 和 8
   ```

**需要观察的现象**：

- `fill` 结果的**四角**明显偏小（因为边缘靠 0 填充，乘积少），而 `wrap` 结果在边缘处数值明显更大、更「均匀」（因为环绕带来了图像对侧的非零像素参与计算）。
- 传给 C 的 `mode=2`（FULL）、`boundary=8`（CIRCULAR），与上面位布局完全吻合。

**预期结果**：两组数值分别等于测试里给出的期望矩阵；`_valfrommode('full')==2`、`_bvalfromboundary('wrap')==8`。

> 说明：测试期望值是 SciPy 官方用例，可信；若本地环境已编译 `_sigtools`，运行结果应精确一致。绘制成图像（`imshow`）能更直观看到边缘差异。

#### 4.3.5 小练习与答案

**练习 1**：`boundary='symm'` 对应的 C 常量是哪个？`_bvalfromboundary('symm')` 返回多少？

**答案**：对应 `REFLECT`（=4）。`_bvalfromboundary('symm')` = `_boundarydict['symm'] << 2` = `1 << 2` = `4`。

**练习 2**：为什么 `OUTSIZE_MASK` 是 3，而 `BOUNDARY_MASK` 是 12？

**答案**：3 = 二进制 `0011`，只保留最低 2 位（模式有 3 个取值 0/1/2，正好用 2 位）；12 = 二进制 `1100`，只保留位 2-3（边界码 0/4/8 正好占这两位）。掩码的「1 的位置」对应它要提取的位段。

**练习 3**：`flag = mode + boundary + ...` 用的是加号 `+` 而不是按位或 `|`，安全吗？

**答案**：在本设计的位布局下安全，因为 mode（位 0-1）、boundary（位 2-3）、flip（位 4）、type（位 5-9）四段**互不重叠**，没有共享的 1 位，所以「加法」与「按位或」结果相同。前提是各段严格不越界；这也是 `_bvalfromboundary` 必须先 `<<2` 把边界码推到对的位置的原因。

---

### 4.4 内核统一与输入交换

#### 4.4.1 概念说明

前面三节讲了「算什么」（卷积/相关）、「算多大」（mode）、「边缘怎么办」（boundary）。本节回答最后两个工程问题：

1. **同一个 C 内核，怎么同时服务卷积与相关？**
   答案是给内核一个 `flip` 标志：`flip=1` 时内核自己翻转核（做卷积），`flip=0` 时不翻转（做相关）。对复数情形，相关还要在 Python 侧先对核取共轭。

2. **相关不是可交换的，`valid` 模式下两个输入谁先谁后会影响结果吗？**
   会。而且底层 `_correlateND` 在「第二个输入比第一个大」时既慢又可能在 `valid` 下失败。所以 scipy 用 `_inputs_swap_needed` 判断**是否需要把两个输入交换**，交换后还要「撤销」其影响。

另外，对 1-D 输入，scipy 会优先走 NumPy 自带的更快实现（`np.convolve` / `np.correlate`），即 `_np_conv_ok` 这条「快速通道」。

#### 4.4.2 核心流程

**A. 同一内核服务卷积与相关（2-D 路径）**

```
convolve2d(in1, in2):   _sigtools._convolve2d(in1, in2,        flip=1, val, bval, fill)   # 翻转核
correlate2d(in1, in2):  _sigtools._convolve2d(in1, in2.conj(), flip=0, val, bval, fill)   # 不翻转，Python 侧共轭
```

注意命名：底层函数名就叫 `_convolve2d`，但通过 `flip` 标志，它既能卷积也能相关——**相关只是「不翻转 + 共轭」的卷积**。

**B. 输入交换（`_inputs_swap_needed`）**

```
_inputs_swap_needed(mode, shape1, shape2):
  - 若 mode != 'valid'  -> 直接返回 False（无需交换）
  - 若 mode == 'valid':
      检查 shape1 是否在所有指定维都 >= shape2（ok1）
      检查 shape2 是否在所有指定维都 >= shape1（ok2）
      若两者都不成立 -> 抛 ValueError（valid 要求一个能「包住」另一个）
      返回 not ok1   # 即「shape1 没法包住 shape2 时，需要交换」
```

交换后，`correlate` / `correlate2d` 还要把结果「翻转+共轭」或 `[::-1,::-1]` 撤销影响；而 `convolve` 因为可交换，撤销是「免费的」（顺序不影响结果）。

**C. 1-D 快速通道（`_np_conv_ok`）**

```
若两个输入都是 1-D：
  - full/valid 模式  -> 直接用 np.convolve / np.correlate（更快）
  - same 模式        -> 仅当 in1 >= in2 时才用（因为 NumPy 的 same 用较大输入尺寸，与 scipy 语义不同）
```

#### 4.4.3 源码精读

`_inputs_swap_needed` 的实现就是上面流程的直译：

[_signaltools.py:67-98](_signaltools.py#L67-L98) —— 仅在 `valid` 模式下判断是否需要交换；要求「一个输入在每一维都不小于另一个」：

```python
def _inputs_swap_needed(mode, shape1, shape2, axes=None):
    if mode != 'valid':
        return False
    if not shape1:
        return False
    if axes is None:
        axes = range(len(shape1))
    ok1 = all(shape1[i] >= shape2[i] for i in axes)
    ok2 = all(shape2[i] >= shape1[i] for i in axes)
    if not (ok1 or ok2):
        raise ValueError("For 'valid' mode, one must be at least "
                         "as large as the other in every dimension")
    return not ok1
```

`correlate` 如何使用它（以及为何还要额外处理 `full`）：

[_signaltools.py:271-306](_signaltools.py#L271-L306) —— `full` 模式下，若 `in2` 比 `in1` 大也交换（为性能，因为 `_correlateND` 在「第二大」时更慢），并在最后用 `_reverse_and_conj` 撤销；`same` 模式**不交换**（因为输出形状取决于 `in1`）：

```python
        swapped_inputs = ((mode == 'full') and (xp_size(in2) > xp_size(in1)) or
                          _inputs_swap_needed(mode, in1.shape, in2.shape))
        if swapped_inputs:
            in1, in2 = in2, in1
        ...
        z = _sigtools._correlateND(in1zpadded, a_in2, out, val)
        z = xp.asarray(z)
        if swapped_inputs:
            # Reverse and conjugate to undo the effect of swapping inputs
            z = _reverse_and_conj(z, xp)
        return z
```

> 这也解释了上一节练习里「valid 要求一个能包住另一个」的来源：就是这里的 `raise ValueError`。

`convolve2d` 与 `correlate2d` 对同一内核 `_sigtools._convolve2d` 的两种调用方式，是理解「flip 标志」的最佳对照：

[_signaltools.py:1869-1875](_signaltools.py#L1869-L1875) —— `convolve2d`：传 `flip=1`，核原样：

```python
    if _inputs_swap_needed(mode, in1.shape, in2.shape):
        in1, in2 = in2, in1
    val = _valfrommode(mode)
    bval = _bvalfromboundary(boundary)
    out = _sigtools._convolve2d(in1, in2, 1, val, bval, fillvalue)
    return xp.asarray(out)
```

[_signaltools.py:1967-1977](_signaltools.py#L1967-L1977) —— `correlate2d`：传 `flip=0`，核在 Python 侧取共轭 `in2.conj()`；若交换过输入，结果用 `out[::-1, ::-1]`（180° 翻转）撤销：

```python
    swapped_inputs = _inputs_swap_needed(mode, in1.shape, in2.shape)
    if swapped_inputs:
        in1, in2 = in2, in1
    val = _valfrommode(mode)
    bval = _bvalfromboundary(boundary)
    out = _sigtools._convolve2d(in1, in2.conj(), 0, val, bval, fillvalue)
    if swapped_inputs:
        out = out[::-1, ::-1]
    return xp.asarray(out)
```

> 对比两段：**唯一区别是第 3 个参数 `1` vs `0`（翻转与否），以及第 2 个参数是否 `.conj()`**。一个 C 内核，靠这两个小差异同时承担卷积与相关——这是本讲最值得记住的工程技巧。

最后看 1-D 快速通道：

[_signaltools.py:1199-1213](_signaltools.py#L1199-L1213) —— `_np_conv_ok`：仅在「都是 1-D」且模式语义与 NumPy 一致时，才允许走 `np.convolve`/`np.correlate`：

```python
def _np_conv_ok(volume, kernel, mode, xp):
    if volume.ndim == kernel.ndim == 1:
        if mode in ('full', 'valid'):
            return True
        elif mode == 'same':
            return xp_size(volume) >= xp_size(kernel)
    else:
        return False
```

注意 `same` 分支的特判：因为 NumPy 的 `'same'` 用**较大**输入的尺寸，而 scipy 用**第一个**输入的尺寸，只有当 `volume >= kernel` 时两者才一致，才能安全走 NumPy。这正是 4.2 练习 2 提到的语义差异在源码里的体现。

#### 4.4.4 代码实践

**实践目标**：跟踪 `_inputs_swap_needed` 与 flip 标志，验证「交换输入不改变 valid 结果」与「同一内核做卷积/相关」。

**操作步骤**：

1. 用 [test_valid_mode](_signaltools.py#L305-L315) 里的数组，正反两次调用 `convolve2d(..., 'valid')`：
   ```python
   e = np.array([[2,3,4,5,6,7,8],[4,5,6,7,8,9,10]])  # 2x7（大）
   f = np.array([[1,2,3],[3,4,5]])                   # 2x3（小）
   signal.convolve2d(e, f, 'valid')   # 大在前
   signal.convolve2d(f, e, 'valid')   # 小在前 -> 触发 _inputs_swap_needed
   ```
2. 直接查看本次调用是否需要交换：
   ```python
   from scipy.signal._signaltools import _inputs_swap_needed
   _inputs_swap_needed('valid', f.shape, e.shape)   # 期望 True（f 包不住 e，需交换）
   _inputs_swap_needed('valid', e.shape, f.shape)   # 期望 False
   _inputs_swap_needed('full',  f.shape, e.shape)   # 期望 False（非 valid 不交换）
   ```
3. 用同一对 `(in1, in2)` 分别调 `convolve2d` 与 `correlate2d`，体会「同一内核、flip 不同」。

**需要观察的现象**：第 1 步两次 `valid` 调用结果**完全相同**（期望矩阵 `[[62,80,98,116,134]]`，见测试），证明交换输入被正确处理。第 2 步三个布尔值符合预期。

**预期结果**：两次 valid 输出相等且等于 `[[62,80,98,116,134]]`；`_inputs_swap_needed` 返回 `True / False / False`。

> 说明：期望值取自官方测试 [_signaltools.py 测试 test_valid_mode](_signaltools.py#L305-L315)，可信；本地运行应精确一致（标记「待本地验证」仅针对你尚未实际执行的环境）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `convolve2d` 交换输入后**不需要**像 `correlate2d` 那样做 `out[::-1, ::-1]` 撤销？

**答案**：因为卷积满足交换律 \(x*h = h*x\)，输入顺序不影响结果。而相关不满足交换律（\(\text{correlate}(x,y) \neq \text{correlate}(y,x)\)，二者是反转关系），所以交换后必须撤销。

**练习 2**：`correlate2d` 用 `flip=0` 调用 `_convolve2d`，那它在哪里完成了「卷积到相关」所需的「不翻转 + 共轭」？

**答案**：「不翻转」由 `flip=0` 在 C 内核里实现；「共轭」由 Python 侧的 `in2.conj()` 实现。两者合起来，就让一个名为 `_convolve2d` 的内核算出了相关。

**练习 3**：对一对 1-D 数组，`signal.convolve` 与 `np.convolve` 在 `mode='same'` 下结果一定相同吗？

**答案**：不一定。仅当 `volume >= kernel` 时（即 `_np_conv_ok` 返回 True），scipy 才走 `np.convolve`，结果一致；若 `volume < kernel`，scipy 仍按「与第一个输入同长」的语义计算，与 NumPy（与较大输入同长）不同。

---

## 5. 综合实践

把本讲四个模块串起来，完成一个小型「图像滤波 + 边界对比」任务。

**任务**：对一张小图分别用卷积核做平滑，体会 `mode` 与 `boundary` 的联合影响，并对照源码解释结果。

**步骤**：

1. 准备一张 6×6 的小图（中心亮、四周暗）与一个 3×3 的箱型平滑核：
   ```python
   import numpy as np
   from scipy import signal
   img = np.zeros((6, 6))
   img[2:4, 2:4] = 1.0
   kernel = np.ones((3, 3)) / 9
   ```
2. 用四种组合各做一次 `convolve2d`，观察输出形状与边缘：
   ```python
   for mode in ['full', 'same', 'valid']:
       for boundary in ['fill', 'wrap', 'symm']:
           out = signal.convolve2d(img, kernel, mode=mode, boundary=boundary)
           print(mode, boundary, out.shape)
   ```
3. 重点对比 `mode='same'` 下三种 `boundary` 的输出**四角**数值：`fill` 会让角变暗（0 填充稀释），`wrap` 会从对角「借」来亮度，`symm` 介于两者之间但更平滑。
4. 用本讲学到的映射函数，写下这次 `same + symm` 调用传给 C 内核的整数编码：
   ```python
   from scipy.signal._signaltools import _valfrommode, _bvalfromboundary
   print(_valfrommode('same'), _bvalfromboundary('symm'))   # 期望 1 和 4
   ```
   解释：`mode=1`（SAME，占位 0-1）、`boundary=4`（REFLECT，占位 2-3），合起来 `flag` 低 4 位 = `1 + 4 = 5`。
5. （进阶）把核换成上一讲（u2-l4）学过的 `signal.windows.hamming(9)`（1-D），用 `signal.convolve(img_some_1d, win, mode='same')` 平滑一行信号，体会「窗作为卷积核」的用法——这正是 `convolve` 文档示例的思路。

**预期产物**：

- 一张表格，列出 9 种 `mode×boundary` 组合的输出形状（`full`→8×8、`same`→6×6、`valid`→4×4）。
- 对 `same` 模式三种边界，角点数值的差异说明。
- `same+symm` 的整数编码 `1` 与 `4`，以及对 `flag` 低 4 位 = 5 的解释。

> 说明：输出形状可由本讲公式直接推出，无需运行即确定；具体角点数值建议本地运行后记录（标记「待本地验证」）。

## 6. 本讲小结

- **卷积与相关只差一个「翻转」**：相关 = 卷积（翻转并取共轭过的核）。scipy 用 `_reverse_and_conj` 让 `correlate` 与 `convolve` 互递归复用（[4.1](_signaltools.py#L1530-L1539)）。
- **`mode` 三模式决定输出尺寸**：`full`（\(N+M-1\)）、`same`（与 `in1` 同长）、`valid`（\(|N-M|+1\)）。字符串经 `_valfrommode`/`_modedict` 映射为整数 `0/1/2`（[4.2](_signaltools.py#L45-L56)）。
- **`boundary` 三模式决定边缘延拓**：`fill`/`wrap`/`symm`。字符串经 `_bvalfromboundary`/`_boundarydict` 映射并 `<<2` 为 `0/8/4`（PAD/CIRCULAR/REFLECT）（[4.3](_signaltools.py#L47-L64)）。
- **位打包**：C 内核用一个整数同时携带「模式+边界+翻转+类型」，由 `_sigtools.hh` 的掩码（`OUTSIZE_MASK=3`、`BOUNDARY_MASK=12`、`FLIP_MASK=16`）切分（[4.3](_sigtools.hh#L6-L18)）。
- **同一内核服务卷积与相关**：`_sigtools._convolve2d` 靠 `flip` 标志（1 翻转/0 不翻转）+ Python 侧 `.conj()` 区分两种运算（[4.4](_signaltools.py#L1869-L1977)）。
- **输入交换**：`_inputs_swap_needed` 仅在 `valid` 模式下判定是否交换两个输入以保证正确性与性能，相关在交换后需撤销，卷积则不需要（[4.4](_signaltools.py#L67-L98)）。

## 7. 下一步学习建议

本讲只覆盖了「直接法」与接口语义。建议按以下顺序继续：

1. **u3-l2（fftconvolve 与 oaconvolve）**：看 `method='fft'/'auto'` 走的频域卷积如何用 FFT 把复杂度从 \(O(NM)\) 降到 \(O(N\log N)\)，以及 `oaconvolve` 如何分块处理超长信号。
2. **u3-l3（choose_conv_method）**：理解 `method='auto'` 如何根据 `_conv_ops`/`_fftconv_faster` 在直接法与 FFT 法之间自动抉择——本讲里反复出现的 `method='auto'` 默认值，决策逻辑全在那里。
3. **u3-l4（_sigtools 与 N-D 相关）**：深入 C 内核 `_correlate_nd.cc`/`_sigtoolsmodule.cc`，看本讲调用的 `_sigtools._correlateND` 与 `_sigtools._convolve2d` 的底层循环是如何逐元素累加的。
4. 读完本单元后，可进入**单元 4（数字滤波）**：`lfilter` 等滤波器内部也大量复用本讲建立的「核滑动 + 边界处理」思路，但会引入状态向量与差分方程。
