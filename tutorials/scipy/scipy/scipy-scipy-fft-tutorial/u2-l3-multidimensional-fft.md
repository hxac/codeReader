# 多维变换：fftn / fft2 与 s、axes 控制

## 1. 本讲目标

本讲承接 u2-l1（一维复数 FFT）建立的「四层调用链 + `norm` 三模式 + `Dispatchable` 分派」认知，把变换从「一条轴」推广到「多条轴」。

学完后你应该能够：

- 说清楚 `fftn` / `ifftn` 如何在任意多维数组的任意几条轴上做变换，以及为什么 N-D FFT 可以「拆成多次 1-D FFT」。
- 解释 `fft2` / `ifft2` 与 `fftn` / `ifftn` 的关系——它们本质是同一函数，只是默认 `axes` 不同。
- 精确控制 `s`（各轴输出长度）与 `axes`（变换哪些轴）这两个参数，并理解它们之间「按位置配对」的微妙之处。
- 理解 `rfftn` / `irfftn` 这种多维实变换为什么「最后一轴」是特殊的。
- 沿着 `fftn` 的调用链一路读到 ducc 计算核心，看懂 `_init_nd_shape_and_axes` 与 `_fix_shape` 如何把 `s` / `axes` 翻译成实际的截断与补零。

---

## 2. 前置知识

在进入多维之前，请确认你已经掌握 u2-l1 的这些结论（本讲直接复用，不再重复证明）：

- **四层调用链**：公共 API（`_basic.py`）只声明分派协议，函数体只 `return (Dispatchable(x, np.ndarray),)`；真正计算在后端（`_basic_backend.py`）→ ducc 核心（`_duccfft/`）→ C 扩展 `pyduccfft`。
- **`n` 参数**：一维 `fft(x, n)` 中，`n` 小于输入长度则截断（取前 `n` 个），大于则末尾补零。
- **`norm` 三模式**：`backward`（默认）/ `ortho` / `forward`，正逆配对恒可逆，区别仅在于 `1/N` 缩放归属。
- **可逆性约定**：`ifft(fft(x)) ≈ x`，用 `np.allclose` 验证。

本讲引入两个新术语：

- **可分离性（separability）**：多维 DFT 的求和可以按轴拆开，等价于沿每条轴依次做一维 DFT。这是 N-D FFT「快」的数学根基。
- **按位置配对（positional pairing）**：`s` 和 `axes` 是用 `zip` 逐元素对应的，`s[i]` 控制的是 `axes[i]` 这条轴，而不是「第 i 条绝对轴」。

> 一个高频误解：docstring 里写「`s[0]` refers to axis 0」，这只在 `axes` 取默认值（全部轴）时才成立。一旦你自己指定 `axes`，`s[i]` 对应的就是 `axes[i]`，源码里是 `zip(shape, axes)` 配对的，后面 4.3 会用源码证死这一点。

---

## 3. 本讲源码地图

本讲横跨四层，下表给出每个文件的职责与本讲会精读的位置：

| 文件 | 层次 | 作用 | 本讲关注 |
| --- | --- | --- | --- |
| [`_basic.py`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic.py) | 公共 API | 对外签名 + docstring + 分派声明 | `fftn`/`ifftn`/`fft2`/`ifft2`/`rfftn`/`irfftn` 的签名与默认 `axes` |
| [`_basic_backend.py`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic_backend.py) | 后端 | 把分派后的调用路由到 ducc 或 `xp.fft` | `_execute_nD` 的多轴分流、`fft2` 如何委托给 `fftn` |
| [`_duccfft/basic.py`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/basic.py) | 计算核心（Python 包装） | 预处理 + 调 C 扩展 | `c2cn`/`r2cn`/`c2rn` 三个多维内核、`functools.partial` 派生 |
| [`_duccfft/helper.py`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/helper.py) | 计算核心（预处理工具） | 形状/轴标准化、截断补零 | `_init_nd_shape_and_axes`、`_fix_shape` |

阅读建议：先看 4.1 建立「N-D = 多次 1-D」的直觉，再按 4.3 的 `s`/`axes` 精读 helper，最后用 4.4 的实变换收尾。本讲的「主轴」是 `s` 与 `axes` 的控制语义。

---

## 4. 核心概念与源码讲解

### 4.1 fftn / ifftn：N-D 变换与可分离性

#### 4.1.1 概念说明

`fftn`（N-D FFT）把一维 DFT 推广到任意多维数组、任意几条轴上。以二维为例，二维 DFT 定义为：

\[
X[k_1, k_2] \;=\; \sum_{n_1=0}^{N_1-1}\sum_{n_2=0}^{N_2-2} x[n_1, n_2]\,
\exp\!\bigl(-2\pi j\,(k_1 n_1/N_1 + k_2 n_2/N_2)\bigr)
\]

关键是指数项可以**因式分解**：

\[
\exp\!\bigl(-2\pi j\,(k_1 n_1/N_1 + k_2 n_2/N_2)\bigr)
= \exp(-2\pi j\,k_1 n_1/N_1)\cdot \exp(-2\pi j\,k_2 n_2/N_2)
\]

于是双重求和可以拆成「先沿轴 2 做一维 DFT，再沿轴 1 做一维 DFT」：

\[
X = \text{FFT}_{\text{axis}=1}\bigl(\text{FFT}_{\text{axis}=0}\bigl(x\bigr)\bigr)
\]

这就是**可分离性**：N-D DFT 等价于沿每条变换轴依次做一次 1-D DFT，且**顺序无关**。这也是 N-D FFT 复杂度只有 \(O(N\log N)\)（而非 \(O(N^2)\)）的原因——它复用了一维 FFT 的快速算法。

`ifftn` 是 `fftn` 的逆，满足 `ifftn(fftn(x)) ≈ x`。

#### 4.1.2 核心流程

一次 `fftn(x)` 的执行链（默认 `s=None, axes=None`，即对所有轴变换）：

1. **公共 API**：`_basic.fftn` 只返回 `Dispatchable(x, np.ndarray)`，触发 uarray 分派（详见 u4-l1）。
2. **后端**：`_basic_backend.fftn` 调 `_execute_nD('fftn', _duccfft.fftn, x, s=s, axes=axes, ...)`。
3. **numpy 直连**：若 `x` 是 numpy 数组，`_execute_n1D` 直接 `np.asarray(x)` 后调用 `_duccfft.fftn`（即 `partial(c2cn, True)`）。
4. **核心 `c2cn`**：`_init_nd_shape_and_axes(tmp, s, axes)` 标准化形状与轴 → `_fix_shape(tmp, shape, axes)` 截断/补零 → `_normalization` 映射 norm → `pfft.c2c(tmp, axes, forward, norm, out, workers)` 真正计算。
5. **C 扩展**：`pyduccfft.c2c` 对每条指定轴依次执行 1-D FFT。

伪代码：

```
fftn(x, s=None, axes=None):
    shape, axes = _init_nd_shape_and_axes(x, s, axes)   # 标准化
    x = _fix_shape(x, shape, axes)                      # 各轴截断/补零
    return pfft.c2c(x, axes, forward=True, ...)         # 逐轴 1-D FFT
```

#### 4.1.3 源码精读

**公共签名与分派声明**——注意 `s` 与 `axes` 默认都是 `None`：

[`_basic.py:L627-L630`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic.py#L627-L630) 是 `fftn` 的两个装饰器和函数头；函数体只有一行 [`_basic.py:L730`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic.py#L730) `return (Dispatchable(x, np.ndarray),)`——和一维 `fft` 一样，它只是「分派协议声明」，不写算法。docstring 里对 `s`、`axes` 的语义说明集中在 [`_basic.py:L642-L653`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic.py#L642-L653)，是本讲最权威的参数说明。

**后端的多轴分流**——`_execute_nD` 是所有 N-D 变换（`fftn`/`ifftn`/`rfftn`/`irfftn`）的统一入口：

[`_basic_backend.py:L52-L74`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic_backend.py#L52-L74) 做三件事：(1) numpy 数组走 duccfft 直连（L55-L58），把 `s`、`axes` 原样传下去；(2) 非 numpy 且数组库带 `xp.fft` 时，改用 `xp_func(x, s=s, axes=axes, norm=norm)`（L61-L70）；(3) 否则退回「转 numpy 算完再转回」（L72-L74）。`fftn` 在后端的绑定见 [`_basic_backend.py:L113-L116`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic_backend.py#L113-L116)。

**ducc 核心的多维内核 `c2cn`**——这是真正干活的地方：

[`_duccfft/basic.py:L126-L149`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/basic.py#L126-L149) 依次执行：`_asfarray` 浮点化 → `_init_nd_shape_and_axes(tmp, s, axes)` 标准化（L136）→ `_fix_shape` 截断/补零（L143）→ `_normalization` 映射 norm → `pfft.c2c(tmp, axes, forward, norm, out, workers)`（L149）。注意 L140-L141 的 `if len(axes) == 0: return x`——如果规范化后没有任何轴要变换，就直接返回原数组（恒等变换）。

`fftn`/`ifftn` 由同一个 `c2cn` 用 `functools.partial` 派生，靠 `forward` 区分方向（与一维 `c2c` 完全同构）：

[`_duccfft/basic.py:L152-L155`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/basic.py#L152-L155) `fftn = functools.partial(c2cn, True)` 与 `ifftn = functools.partial(c2cn, False)`，并各自设好 `__name__`。最终计算落到 `pfft.c2c`（C 扩展 `pyduccfft`），它对 `axes` 里的每条轴依次做 1-D FFT——这正是可分离性的代码体现。

> 测试侧印证：[`tests/test_basic.py:L107-L115`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/tests/test_basic.py#L107-L115) 的 `test_fftn` 直接断言 `fft.fftn(x) == fft.fft(fft.fft(fft.fft(x, axis=2), axis=1), axis=0)`，把「N-D = 多次 1-D」写成了回归测试。

#### 4.1.4 代码实践

**实践目标**：亲手验证可分离性——`fftn` 等于沿各轴依次做一维 `fft`，并验证正逆可逆。

**操作步骤**：

```python
import numpy as np
import scipy.fft

rng = np.random.default_rng(0)
x = rng.standard_normal((4, 5, 6)) + 1j * rng.standard_normal((4, 5, 6))

# 1) 一次性 N-D 变换
Y = scipy.fft.fftn(x)

# 2) 沿每条轴依次做 1-D FFT（顺序无关，这里按 axis 2 -> 1 -> 0）
Z = scipy.fft.fft(scipy.fft.fft(scipy.fft.fft(x, axis=2), axis=1), axis=0)

print("fftn == 逐轴 fft:", np.allclose(Y, Z))         # 预期 True

# 3) 正逆可逆
print("ifftn(fftn(x)) == x:", np.allclose(scipy.fft.ifftn(Y), x))  # 预期 True
```

**需要观察的现象**：两次打印都应为 `True`；改变第 2 步的轴顺序（例如 `axis=0` → `axis=2` → `axis=1`），结果仍与 `Y` 一致，说明顺序无关。

**预期结果**：两个 `True`。若改为 `x` 为实数组，结论同样成立（`fftn` 对实数组也照算，只是没用上 Hermitian 对称性的加速）。

#### 4.1.5 小练习与答案

**练习 1**：对一个 `(2, 3, 4)` 的复数组，`fftn(x)` 输出形状是什么？为什么？

> **答案**：输出形状仍是 `(2, 3, 4)`。因为 `s=None` 表示各轴输出长度等于输入长度（既不截断也不补零）。N-D 变换不改变数组维度，只改变每条轴上的数值。

**练习 2**：把 `fftn` 沿轴的执行顺序从 `2→1→0` 改成 `0→2→1`，结果会变吗？用一句话解释。

> **答案**：不会变。因为可分离性保证沿不同轴的 1-D DFT 可交换顺序（指数项可因式分解、求和可重排）。这正是 `c2cn` 能「逐轴依次算」的数学依据。

---

### 4.2 fft2 / ifft2：fftn 的二维便捷封装

#### 4.2.1 概念说明

`fft2` 不是一套独立算法，它就是 `fftn` 的一个「换了默认 `axes` 的快捷方式」。区别只有一处：

| 函数 | 默认 `axes` | 含义 |
| --- | --- | --- |
| `fftn` | `None`（= 全部轴） | 对数组所有轴做变换 |
| `fft2` | `(-2, -1)` | 只对**最后两条**轴做变换 |

这一点 docstring 写得非常直白：[`_basic.py:L905`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic.py#L905) 「`fft2` is just `fftn` with a different default for `axes`」。

为什么需要 `fft2`？图像、谱分析里最常见的就是二维矩阵（灰度图、时频图），固定变换「最后两轴」最省心，于是封了一层糖。`ifft2` 同理是 `ifftn` 的二维封装（见 [`_basic.py:L1010`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic.py#L1010)）。

#### 4.2.2 核心流程

```
fft2(x, s=None, axes=(-2,-1)):
    -> 实际上直接调用 fftn(x, s, axes, ...)
        -> _execute_nD('fftn', _duccfft.fftn, x, s=s, axes=axes, ...)
            -> c2cn(...)  # 与 4.1 完全相同的路径
```

无论你调 `fft2` 还是 `fftn`（带 `axes=(-2,-1)`），最终都进同一个 `c2cn` 内核，连参数都一样。

#### 4.2.3 源码精读

**公共层**——`fft2` 的默认 `axes` 写死在签名里：

[`_basic.py:L840`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic.py#L840) `def fft2(x, s=None, axes=(-2, -1), ...)`。与 `fftn` 的 `def fftn(x, s=None, axes=None, ...)`（[`_basic.py:L629`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic.py#L629)）唯一区别就是 `axes` 默认值。函数体同样是 `return (Dispatchable(x, np.ndarray),)`（[`_basic.py:L935`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic.py#L935)）。

**后端层**——`fft2` 直接转手给 `fftn`，没有任何额外逻辑：

[`_basic_backend.py:L126-L128`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic_backend.py#L126-L128) `def fft2(...): return fftn(x, s, axes, norm, overwrite_x, workers, plan=plan)`。这就是「`fft2` 只是默认值不同的 `fftn`」在代码层面的铁证——后端连函数体都只是转调。`ifft2` 同样转调 `ifftn`（[`_basic_backend.py:L131-L133`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic_backend.py#L131-L133)）。

> 后果：`fft2` 在 3-D 数组上只变换最后两轴（第 0 轴原样保留），这是「2」字的真正含义——「两条轴」，不是「二维数组」。在更高维数组上它依然只动最后两轴。

#### 4.2.4 代码实践

**实践目标**：证明 `fft2(x)` ≡ `fftn(x)`（对二维），并观察 `fft2` 在三维数组上「只动最后两轴」。

**操作步骤**：

```python
import numpy as np
import scipy.fft

# (a) 二维：fft2 与 fftn 默认行为一致吗？
A = np.random.default_rng(1).standard_normal((6, 7))
print("2-D fft2 == fftn:", np.allclose(scipy.fft.fft2(A), scipy.fft.fftn(A)))  # True

# fft2 也等价于沿两条轴各做一次 1-D fft
print("2-D fft2 == 逐轴:",
      np.allclose(scipy.fft.fft2(A),
                  scipy.fft.fft(scipy.fft.fft(A, axis=1), axis=0)))  # True

# (b) 三维：fft2 只变换最后两轴
B = np.random.default_rng(2).standard_normal((3, 4, 5)) + 0j
R = scipy.fft.fft2(B)
print("fft2 on 3-D 输出形状:", R.shape)        # (3, 4, 5)
# 等价于 fftn 只指定最后两轴
print("fft2 == fftn(axes=(-2,-1)):",
      np.allclose(R, scipy.fft.fftn(B, axes=(-2, -1))))            # True
```

**需要观察的现象**：三个比较全部 `True`；三维输入下 `fft2` 输出形状不变，且与 `fftn(B, axes=(-2,-1))` 一致。

**预期结果**：全部 `True`，`R.shape == (3, 4, 5)`。这印证 `fft2` 不要求输入是二维，它只是「固定变换最后两轴」。

#### 4.2.5 小练习与答案

**练习 1**：调用 `fft2(x)` 时，docstring 说未给 `s` 就用 `axes` 指定轴上的输入长度。那么对 `x.shape == (3, 4, 5)` 调 `fft2(x)`，哪两条轴的长度被用作变换长度？

> **答案**：第 `-2`（长度 4）和第 `-1`（长度 5）两条轴。第 0 轴（长度 3）不在 `axes=(-2,-1)` 中，完全不参与变换。

**练习 2**：`ifft2(fft2(x))` 与 `ifftn(fftn(x))` 对同一个二维数组 `x`，结果是否数值相等？

> **答案**：相等。两者都是「正变换后立即逆变换」，且 `fft2`/`ifft2` 与 `fftn`/`ifftn` 走完全相同的 `c2cn` 内核，只是默认 `axes` 不同。对二维数组默认 `axes` 又恰好一致，故数值相等（均在 `allclose` 精度内）。

---

### 4.3 s 与 axes：多轴形状与轴控制（本讲重点）

#### 4.3.1 概念说明

`fftn` 有两个核心控制参数，理解它们是驾驭多维变换的钥匙：

- **`axes`**：一个整数序列，指定**变换哪几条轴**。没列在 `axes` 里的轴原样保留、不参与变换。负数索引允许（`-1` 是最后一条轴）。
- **`s`**：一个整数序列，指定**各变换轴的输出长度**。它就是把一维 `fft(x, n)` 里的 `n` 推广到「每条轴一个 n」。某轴上 `s` 小于输入长度则**截断**（取前 s 个），大于则**补零**（末尾填 0）。

两者的**配对关系是最容易踩坑的点**：`s` 与 `axes` 是**按位置 `zip` 配对**的，即 `s[i]` 控制 `axes[i]` 这条轴，而不是「第 i 条绝对轴」。

默认规则（在 `_init_nd_shape_and_axes` 里实现）：

- `s=None` 且 `axes=None`：变换**全部**轴，各轴输出长度 = 输入长度。
- 只给 `s`、不给 `axes`：`axes` 默认为**最后 `len(s)` 条**轴。
- 只给 `axes`、不给 `s`：各指定轴的输出长度 = 该轴输入长度。
- `s` 与 `axes` 同时给：长度必须相等，否则 `ValueError`。

#### 4.3.2 核心流程

`_init_nd_shape_and_axes` 把用户输入标准化成 `(shape, axes)` 两个规整的列表，规则用伪代码表示：

```
_init_nd_shape_and_axes(x, s, axes):
    若 axes 给定:
        axes = [a + ndim if a<0 else a for a in axes]   # 负索引转正
        校验：所有 a 在 [0, ndim) 内，且互不重复
    若 s 给定:
        校验：len(axes) == len(s)（若 axes 也给定）
        若 axes 未给定: axes = 最后 len(s) 条轴
        shape = [s[i] 对应 axes[i]]                     # 按位置配对
    若都未给定:
        shape = x.shape; axes = 全部轴
    校验：所有 shape 元素 >= 1
    返回 (shape, axes)
```

随后 `_fix_shape(x, shape, axes)` 对每条指定轴做截断或补零，把数组修整成目标形状，再送进 C 内核。

#### 4.3.3 源码精读

**按位置配对的铁证**——`zip(shape, axes)`：

[`_duccfft/helper.py:L100`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/helper.py#L100) `shape = [x.shape[a] if s == -1 else s for s, a in zip(shape, axes)]`。这一行同时证明两件事：(1) `s` 与 `axes` 按位置 `zip` 配对；(2) `s` 里写 `-1` 表示「该轴沿用输入长度」（一个很有用的占位符）。

**默认 axes 的推导**——「只给 s 就用最后几条轴」：

[`_duccfft/helper.py:L95-L98`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/helper.py#L95-L98) 当只给 `shape` 未给 `axes` 时，先校验 `len(shape) <= x.ndim`，再令 `axes = range(x.ndim - len(shape), x.ndim)`——即「最后 `len(s)` 条轴」。

**「都未给定 → 全部轴」分支**：

[`_duccfft/helper.py:L101-L103`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/helper.py#L101-L103) `shape = list(x.shape); axes = range(x.ndim)`。这就是 `fftn(x)` 不带参数时变换全部轴的来源。

**两类输入校验**：

- 长度不一致：[`_duccfft/helper.py:L92-L94`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/helper.py#L92-L94) `if axes and len(axes) != len(shape): raise ValueError(...)`。
- 轴越界/重复/负数：[`_duccfft/helper.py:L82-L87`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/helper.py#L82-L87)，先 `a + x.ndim if a < 0` 转正，再查 `a >= x.ndim or a < 0`、再查 `len(set(axes)) != len(axes)`（去重）。
- 形状非正：[`_duccfft/helper.py:L107-L109`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/helper.py#L107-L109) `if any(s < 1 for s in shape)`。

**`_fix_shape` 如何实现截断/补零**——这是 `s` 生效的最后一环：

[`_duccfft/helper.py:L146-L170`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/helper.py#L146-L170) 对每条 `(n, ax)` 构造一个多维切片：若输入该轴长度 ≥ n，则切 `slice(0, n)`（截断，返回视图不拷贝）；否则先切到原长度并标记 `must_copy=True`。若无需补零，直接返回视图（L161-L162）；若需补零，则新建一个全零数组 `z`，把截取到的数据拷进去（L164-L170）。这条逻辑和一维 `_fix_shape_1d` 完全同构，只是推广到多轴。

> 想把任意值统一成 int 序列，靠 [`_duccfft/helper.py:L22-L44`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/helper.py#L22-L44) 的 `_iterable_of_int`：单个整数会被包成单元素元组（L35-L36），序列元素逐一 `operator.index`。

#### 4.3.4 代码实践

**实践目标**：构造 `(8, 16, 32)` 三维数组，只对第 0、2 轴变换；并验证 `s` 的截断/补零、`s`-`axes` 按位置配对、长度不一致报错。

**操作步骤**：

```python
import numpy as np
import scipy.fft

rng = np.random.default_rng(3)
x = rng.standard_normal((8, 16, 32)) + 0j

# (1) 只变换第 0、2 轴，第 1 轴(长度16)原样保留
Y = scipy.fft.fftn(x, axes=(0, 2))
print("axes=(0,2) 输出形状:", Y.shape)        # 预期 (8, 16, 32)

# 等价于：先沿 axis=2 做 fft，再沿 axis=0 做 fft（axis=1 不动）
manual = scipy.fft.fft(scipy.fft.fft(x, axis=2), axis=0)
print("== 逐轴(axis 2 then 0):", np.allclose(Y, manual))   # True

# (2) s 配合 axes：给第 0 轴补零到 12、第 2 轴截断到 16
Y2 = scipy.fft.fftn(x, s=(12, 16), axes=(0, 2))
print("s=(12,16),axes=(0,2) 形状:", Y2.shape)  # 预期 (12, 16, 16)
# 注意：s[0]=12 控制 axes[0]=0 轴；s[1]=16 控制 axes[1]=2 轴（按位置配对！）

# (3) 验证「按位置配对」：把 axes 顺序换成 (2, 0)，s 不变
Y3 = scipy.fft.fftn(x, s=(12, 16), axes=(2, 0))
print("s=(12,16),axes=(2,0) 形状:", Y3.shape)  # 预期 (8, 16, 12)
# 现在 s[0]=12 控制 axis=2(原32->12 截断)，s[1]=16 控制 axis=0(原8->16 补零)

# (4) 只给 s 不给 axes -> 默认用「最后 len(s) 条轴」
Y4 = scipy.fft.fftn(x, s=(4, 4))
print("s=(4,4) 无 axes 形状:", Y4.shape)        # 预期 (8, 4, 4)，动最后两轴

# (5) s 与 axes 长度不一致 -> 报错
try:
    scipy.fft.fftn(x, s=(4, 4, 4), axes=(0, 1))
except ValueError as e:
    print("长度不一致报错:", e)
```

**需要观察的现象**：
- (1) 输出形状 `(8, 16, 32)`，第 1 轴长度 16 原封不动。
- (2) 输出 `(12, 16, 16)`：轴 0 从 8 补零到 12，轴 2 从 32 截断到 16。
- (3) 输出 `(8, 16, 12)`：与 (2) 不同！证明 `s` 是按 `axes` 的位置配对的，调换 `axes` 顺序会改变 `s` 作用对象。
- (4) 输出 `(8, 4, 4)`：只给 `s` 时动最后两条轴。
- (5) 抛出 `ValueError`。

**预期结果**：见上述每条注释。第 (3) 步是理解本讲的关键——同一段 `s=(12,16)`，因 `axes` 顺序不同而产生不同形状。

> 待本地验证：第 (3) 步的形状判断依赖你对「按位置配对」的理解，运行后请核对 `Y3.shape` 是否确为 `(8, 16, 12)`。

#### 4.3.5 小练习与答案

**练习 1**：对 `x.shape == (8, 16, 32)`，调用 `fftn(x, s=(20,))`（`s` 只有一个元素，不给 `axes`）。变换哪条轴？输出形状是什么？

> **答案**：变换**最后 1 条**轴（axis 2，因为「只给 s 用最后 len(s) 条轴」）。该轴从 32 补零到 20。输出形状 `(8, 16, 20)`。

**练习 2**：`fftn(x, s=(10, 20), axes=(2, 0))` 中，`s[0]=10` 控制哪条轴？

> **答案**：控制 `axes[0]=2` 这条轴（即第 2 轴），因为 `s` 与 `axes` 是 `zip` 按位置配对，`s[i]` 对应 `axes[i]`。`s[1]=20` 控制 `axes[1]=0`（第 0 轴）。

**练习 3**：把 `axes` 写成 `(0, 0)`（重复轴）会发生什么？依据是哪段源码？

> **答案**：抛 `ValueError("all axes must be unique")`。依据是 [`_duccfft/helper.py:L86-L87`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/helper.py#L86-L87) 的 `if len(set(axes)) != len(axes)` 去重检查。

---

### 4.4 rfftn / irfftn：多维实变换与「最后一轴」的特殊性

#### 4.4.1 概念说明

`rfftn` 是 `rfft`（u2-l2）的多维推广，输入是**实数组**。它利用 Hermitian 对称性省掉一半频谱，但多维情形下有一条关键约定：

> **只有「最后一条变换轴」走实变换（`r2c`，输出半谱）；其余变换轴走普通复变换（`c2c`，输出全谱）。**

这句话直接来自 docstring：[`_basic.py:L1043-L1046`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic.py#L1043-L1046)「By default, all axes are transformed, with the real transform performed over the **last axis**, while the remaining transforms are complex」。

因此 `rfftn` 输出形状里，只有**最后一条变换轴**长度变为 `s[-1]//2 + 1`，其余变换轴长度按 `s`（或输入长度）不变。

`irfftn` 是 `rfftn` 的逆。和一维 `irfft` 一样，它**无法从半谱推断原始实信号的长度**，所以必须由调用者通过 `s[-1]` 告知「最后一条轴原本有多长」。这就是为什么 round-trip 要写成 `irfftn(rfftn(x), x.shape)` 而不是 `irfftn(rfftn(x))`。

`rfft2`/`irfft2` 同样只是默认 `axes=(-2,-1)` 的 `rfftn`/`irfftn` 封装（见 [`_basic.py:L1180`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic.py#L1180)）。

#### 4.4.2 核心流程

`rfftn` 的内核是 `r2cn`，`irfftn` 的内核是 `c2rn`：

```
rfftn(x, s, axes):                       # r2cn(forward=True)
    校验 x 为实数组
    shape, axes = _init_nd_shape_and_axes(x, s, axes)
    x = _fix_shape(x, shape, axes)
    return pfft.r2c(x, axes, forward=True, ...)   # 最后一轴走实变换

irfftn(x, s, axes):                      # c2rn(forward=False)
    shape, axes = _init_nd_shape_and_axes(x, s, axes)
    若 s 未给定: shape[-1] = (输入最后一轴长度 - 1) * 2   # 按偶数猜
    lastsize = shape[-1]
    shape[-1] = shape[-1]//2 + 1          # 半谱所需输入长度
    x = _fix_shape(x, shape, axes)
    return pfft.c2r(x, axes, lastsize, forward=False, ...)
```

注意 `irfftn` 里对「最后一轴」的特殊处理：先记录目标实输出长度 `lastsize`，再把 `shape[-1]` 折半 `+1` 得到半谱所需输入点数。

#### 4.4.3 源码精读

**`r2cn`：多维实变换内核**——校验实输入、复用 `_init_nd_shape_and_axes` 与 `_fix_shape`：

[`_duccfft/basic.py:L157-L177`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/basic.py#L157-L177)。L165-L166 校验 `np.isrealobj(tmp)` 否则 `TypeError("x must be a real sequence")`；L168 标准化形状轴；L173-L174 要求至少变换 1 条轴；最终 L177 `return pfft.r2c(tmp, axes, forward, norm, None, workers)`。`rfftn = functools.partial(r2cn, True)` 见 [`_duccfft/basic.py:L180`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/basic.py#L180)。

**`c2rn`：多维逆实变换内核**——最后一轴的 Hermitian 处理：

[`_duccfft/basic.py:L186-L218`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/basic.py#L186-L218)。关键三步：L205-L206 当 `s` 未给定时 `shape[-1] = (x.shape[axes[-1]] - 1) * 2`（按偶数长度猜原实信号长度）；L212 `lastsize = shape[-1]` 记录真实输出长度；L213 `shape[-1] = (shape[-1] // 2) + 1` 折算成半谱输入点数；最后 L218 `return pfft.c2r(tmp, axes, lastsize, forward, norm, None, workers)` 把 `lastsize` 交给 C 内核去恢复实信号。`irfftn = functools.partial(c2rn, False)` 见 [`_duccfft/basic.py:L223`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/basic.py#L223)。

**后端绑定**——`rfftn`/`irfftn` 同样经 `_execute_nD`：

[`_basic_backend.py:L136-L139`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic_backend.py#L136-L139) 是 `rfftn`，[`_basic_backend.py:L147-L150`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic_backend.py#L147-L150) 是 `irfftn`。注意 `complex_funcs` 集合（[`_basic_backend.py:L19`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic_backend.py#L19)）里含 `'irfftn'` 但**不含** `'rfftn'`——在非 numpy、走 `xp.fft` 的路径上，`irfftn` 会尝试把 float 输入转成 complex 再算（因为半谱逆变换预期复数输入），而 `rfftn` 预期实输入，无需此兜底（详见 u6-l1）。

#### 4.4.4 代码实践

**实践目标**：观察 `rfftn`「只有最后一条变换轴变半谱」，并验证 `irfftn(rfftn(x), x.shape) ≈ x`。

**操作步骤**：

```python
import numpy as np
import scipy.fft

rng = np.random.default_rng(4)
x = rng.standard_normal((8, 16, 32))   # 实数组

# (1) rfftn：默认变换全部轴，最后一轴(axis2)输出半谱
R = scipy.fft.rfftn(x)
print("rfftn 输出形状:", R.shape)
# 轴0=8(全谱), 轴1=16(全谱), 轴2=32//2+1=17(半谱) -> (8, 16, 17)

# (2) 与「先 rfft 最后一轴，再 fft 前两轴」等价
manual = scipy.fft.fft(scipy.fft.fft(scipy.fft.rfft(x, axis=2), axis=1), axis=0)
print("rfftn == rfft(last)+fft(rest):", np.allclose(R, manual))   # True

# (3) round-trip：必须把原始形状传给 irfftn
xx = scipy.fft.irfftn(R, s=x.shape)
print("irfftn(rfftn(x), x.shape) == x:", np.allclose(xx, x))      # True

# (4) 只变换前两轴：那么「最后一轴」就是 axes 里的最后一条(这里 axis=1)
R2 = scipy.fft.rfftn(x, axes=(0, 1))
print("rfftn(axes=(0,1)) 形状:", R2.shape)
# 轴0=8(全), 轴1=16//2+1=9(半谱,因为它是 axes 的最后一条), 轴2=32(不动) -> (8, 9, 32)
```

**需要观察的现象**：
- (1) `R.shape == (8, 16, 17)`：只有最后一条变换轴变成 `32//2+1=17`。
- (2) `True`，印证「最后一轴走 `r2c`，其余走 `c2c`」。
- (3) `True`，且必须显式传 `s=x.shape`。
- (4) `R2.shape == (8, 9, 32)`：「最后一条变换轴」随 `axes` 改变——这里变成 axis 1，所以 axis 1 折半，axis 2 完全不动。

**预期结果**：见每条注释。第 (4) 步最能体现「最后一轴」指的是**变换轴集合里的最后一条**，而非数组的绝对最后一轴。

> 待本地验证：第 (4) 步 `R2.shape` 是否为 `(8, 9, 32)`，这是「最后变换轴走实变换」的直接推论。

#### 4.4.5 小练习与答案

**练习 1**：`rfftn(x)` 对 `x.shape == (10, 20)`（实数组）的输出形状是什么？

> **答案**：`(10, 11)`。默认变换全部轴；最后一条变换轴（axis 1）走实变换，长度 `20//2+1=11`；axis 0 走复变换，长度保持 10。

**练习 2**：为什么 `irfftn(rfftn(x))`（不带 `s`）一般还原不出原始 `x`？而 `irfftn(rfftn(x), s=x.shape)` 可以？

> **答案**：因为 `rfftn` 的半谱没有记录原始实信号在最后一条轴上的长度，`irfftn` 只能按偶数猜测（`s[-1] = (m-1)*2`，见 [`_duccfft/basic.py:L205-L206`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/basic.py#L205-L206)）。若原始最后一条轴长度恰好是偶数且与猜测一致，能还原；若是奇数或被猜错，长度就对不上。显式传 `s=x.shape` 把真实长度告诉 `irfftn`，才能精确还原。

**练习 3**：`rfft2` 与 `rfftn` 的关系，和 `fft2` 与 `fftn` 的关系是否相同？

> **答案**：相同。`rfft2` 也只是 `rfftn` 换了默认 `axes=(-2,-1)`（见 [`_basic.py:L1138`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic.py#L1138) 与 Notes [`_basic.py:L1180`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic.py#L1180)），后端同样直接转调（见 [`_basic_backend.py:L142-L144`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic_backend.py#L142-L144)）。「2」字都是指「固定变换最后两轴」，而非「输入必须是二维」。

---

## 5. 综合实践

把本讲四条线索（可分离性、`fft2`≈`fftn`、`s`/`axes` 控制、实变换的最后一轴）串成一个小任务。

**任务**：给定一个 `(8, 16, 32)` 的实数组 `x`，完成下面四件事并解释每一步的输出形状。

```python
import numpy as np
import scipy.fft

rng = np.random.default_rng(42)
x = rng.standard_normal((8, 16, 32))

# 1) 只对第 0、2 轴做复变换（第 1 轴不动），并用 s 把轴 0 补零到 10、轴 2 截断到 16
A = scipy.fft.fftn(x, s=(10, 16), axes=(0, 2))
assert A.shape == (10, 16, 16), A.shape

# 2) 验证 A 等价于「先沿 axis=2 截断到16做 fft，再沿 axis=0 补零到10做 fft」
#    提示：用 scipy.fft.fft(..., n=..., axis=...) 分两步实现，再 allclose
step1 = scipy.fft.fft(x, n=16, axis=2)   # axis 2: 32 -> 16 截断
step2 = scipy.fft.fft(step1, n=10, axis=0)  # axis 0: 8 -> 10 补零
assert np.allclose(A, step2)

# 3) 用 fft2 在二维切片 x[0]（形状 (16,32)）上做二维 FFT，并确认它等于 fftn(x[0])
B  = scipy.fft.fft2(x[0])
B2 = scipy.fft.fftn(x[0])
assert np.allclose(B, B2)

# 4) 对整个 x 做实变换 rfftn，再用 irfftn 还原（注意要传 s=x.shape）
R  = scipy.fft.rfftn(x)
xx = scipy.fft.irfftn(R, s=x.shape)
assert np.allclose(xx, x)
print("全部断言通过；rfftn 输出形状:", R.shape)   # 预期 (8, 16, 17)
```

**要回答的问题**（写在你的笔记里）：

1. 第 1 步 `A.shape` 为什么是 `(10, 16, 16)`？三个维度分别对应什么？（提示：`s[0]=10→axes[0]=0`，`s[1]=16→axes[1]=2`，轴 1 不在 `axes` 中保持 16。）
2. 第 2 步为什么「先截断 axis 2、再补零 axis 0」的顺序可以与 `fftn` 一次性结果对上？（提示：可分离性、顺序无关。）
3. 第 4 步 `R.shape` 为什么是 `(8, 16, 17)`？（提示：只有最后一条变换轴 axis 2 走实变换：`32//2+1=17`。）

**预期结果**：全部断言通过，`R.shape == (8, 16, 17)`。如果第 1 步形状不符，请重看 4.3 的「按位置配对」；如果第 4 步还原失败，请确认 `irfftn` 是否传了 `s=x.shape`。

---

## 6. 本讲小结

- **N-D FFT 是可分离的**：`fftn(x)` 等价于沿每条变换轴依次做 1-D `fft`，顺序无关；数学根因是 DFT 指数核可因式分解，使多维求和能按轴拆开。这条性质把 N-D 复杂度压到 \(O(N\log N)\)，也是 `c2cn` 内核「逐轴 1-D FFT」的依据。
- **`fft2`/`ifft2` 只是默认值不同的 `fftn`/`ifftn`**：唯一区别是 `axes` 默认从 `None`（全部轴）变成 `(-2, -1)`（最后两轴）；后端 `fft2` 甚至直接转调 `fftn`，无独立算法。
- **`s` 与 `axes` 按位置 `zip` 配对**：`s[i]` 控制 `axes[i]` 这条轴，而非第 i 条绝对轴；这是多维变换最容易踩的坑，源码铁证在 `_init_nd_shape_and_axes` 的 `zip(shape, axes)`。
- **`s` 的单轴语义同一维 `n`**：某轴上 `s` 小于输入长度则截断（切片，免拷贝），大于则末尾补零（新建数组），由 `_fix_shape` 实现。
- **默认规则**：只给 `s` → 动最后 `len(s)` 条轴；都不给 → 动全部轴；`s` 与 `axes` 同时给则长度必须相等，否则 `ValueError`。
- **`rfftn` 只有「最后一条变换轴」走实变换（输出 `s[-1]//2+1` 半谱），其余轴走复变换**；`irfftn` 必须由 `s[-1]` 告知原始实信号长度才能精确还原。

---

## 7. 下一步学习建议

- **进入 u2-l4（辅助函数）**：本讲多次提到「输出按先正频后负频排列」，要让零频回到中心，就需要 `fftshift`/`ifftshift`；要给变换后的多维数组配频率刻度，就需要 `fftfreq`/`rfftfreq`。这些都在 `_helper.py`，是本讲的自然下游。
- **想深入 `s`/`axes` 的实现**：重读 [`_duccfft/helper.py`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/helper.py) 的 `_init_nd_shape_and_axes` 与 `_fix_shape`，尝试手写一个简化版 `_fix_shape`，对照截断/补零与「是否拷贝」的判定。
- **想理解多轴如何并行**：本讲的 `workers` 参数会在 u5-l3（`set_workers`/`get_workers`/`threading.local`）展开——它正是把「沿非变换轴的独立 1-D FFT」切片并行执行的开关。
- **想看跨后端的 N-D 分流**：`_execute_nD` 的三条路径（numpy 直连 / `xp.fft` / 转 numpy 回退）将在 u6-l1 系统讲解，届时你会明白 `complex_funcs` 集合里为何有 `'irfftn'` 却没有 `'rfftn'`。
