# 离散正弦变换 dst / dstn

## 1. 本讲目标

本讲承接 u3-l1（离散余弦变换 DCT），把同一套思路搬到它的「姊妹变换」——离散正弦变换（Discrete Sine Transform, DST）。读完本讲你应该能够：

- 说出 `dst`/`idst`/`dstn`/`idstn` 四个函数的签名、参数含义和默认行为。
- 写出 DST 四种 type 的数学定义，并解释每种 type 背后的**边界假设（奇对称延拓）**，从而理解 DST 与 DCT 的根本差异。
- 看懂 DST 如何与 DCT **共用同一套内核** `_r2r`/`_r2rn`，只靠 `functools.partial` 切换 `forward` 与 `transform` 参数就派生出全部 8 个函数（DCT 4 个 + DST 4 个）。
- 理解 `norm` 三模式与 `orthogonalize` 在 DST 上的行为，以及 `idst` 为何能通过「type 翻转 + 方向翻转」复用 `dst` 内核。
- 用 DST 做一次「系数截断 + 重建」的信号压缩小实验，观察能量保留比例。

---

## 2. 前置知识

本讲默认你已经掌握以下内容（来自前面几讲）：

- **四层调用链**（来自 u1-l2、u3-l1）：公共 API 层（`_realtransforms.py`）只写签名与 docstring，函数体仅 `return (Dispatchable(x, np.ndarray),)`；真正计算在 `_realtransforms_backend.py` 的 `_execute` → `_duccfft` 内核 → C 扩展 `pyduccfft`。
- **DCT 的机制**（来自 u3-l1）：`dct`/`idct`/`dctn`/`idctn` 由 `_r2r`/`_r2rn` 经 `functools.partial` 派生；`norm` 三模式由 `_NORM_MAP` 映射成整数 `inorm`，再用 `2 - inorm` 在正逆间翻转；`orthogonalize` 修正首尾元素使变换矩阵满足 \(O^\top O = I\)。
- **正交变换与能量守恒**：正交变换（如 `norm='ortho'` 下的 DCT/DST）不改变向量的 2-范数（能量），这是「截断系数做压缩」能够衡量能量损失的数学基础。

> 一句话回顾：DCT 假设信号在边界做**偶对称（镜像）延拓**，DST 则假设信号做**奇对称（反镜像）延拓**。两者数学结构几乎相同，所以代码也几乎相同——这正是本讲要反复强调的核心。

### DST 的直觉

如果你把一段信号在两端「反着折回去」再求傅里叶级数，由于信号在折叠点是**奇函数**，级数里只会出现**正弦项**（sin），不会出现余弦项（cos）。这就是 DST 的物理来源：

\[ \text{奇对称延拓的信号} \implies \text{频域只剩正弦分量} \]

正因为假设了奇对称，**DST 天然适合描述「两端取值为 0」的信号**——例如两端固定（钉死）的振动弦、有限差分求解泊松方程时的内部点。这就是它和 DCT（两端自由/导数为零）的分界线。

---

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|------|------|
| [`_realtransforms.py`](_realtransforms.py) | 公共 API 层。`dst`/`idst`/`dstn`/`idstn` 的签名、docstring（含数学定义）与 `Dispatchable` 分派声明。 |
| [`_realtransforms_backend.py`](_realtransforms_backend.py) | 后端桥接层。`_execute` 把任意数组库的输入转成 numpy，调用 `_duccfft` 内核，再转回原命名空间。 |
| [`_duccfft/realtransforms.py`](_duccfft/realtransforms.py) | 计算核心的 Python 封装。`_r2r`/`_r2rn` 是 DCT 与 DST **共用**的 1-D / N-D 内核；`dst` 等由 `functools.partial` 派生。 |
| [`_duccfft/helper.py`](_duccfft/helper.py) | 预处理工具：`_asfarray`（浮点化）、`_fix_shape`（截断/补零）、`_normalization`（norm 映射）、`_workers`（并行数解析）。 |
| `tests/test_real_transforms.py` | DST 的测试，可作为「行为如何被验证」的参考。 |

四层调用链（以 `dst(x)` 为例，与 DCT 完全同构）：

```
scipy.fft.dst                      # 公共 API（_realtransforms.py）
   └─ uarray 分派（domain="numpy.scipy.fft"）
        └─ _realtransforms_backend.dst   # _execute 桥接
             └─ _duccfft.dst             # functools.partial(_r2r, True, pfft.dst)
                  └─ _r2r(...)           # 预处理 + type 翻转
                       └─ pfft.dst(...)  # C 扩展 pyduccfft.dst
```

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **DST 的数学定义与四种 type**（边界假设，与 DCT 对比）
2. **dst / dstn / idst / idstn 的公共签名与四层分派**
3. **norm 归一化与 type 翻转**（idst 如何复用 dst 内核）

---

### 4.1 DST 的数学定义与四种 type

#### 4.1.1 概念说明

和 DCT 一样，DST 理论上有 8 种 type，SciPy 只实现了前 4 种。四种 type 的差异完全来自**奇对称延拓时，折叠点的位置（整数点还是半整数点）以及边界是否带偏移**。

关键直觉：

- **Type I**：关于 \(n=-1\) 和 \(n=N\) 奇对称（折叠点在**区间外一个整数处**）。
- **Type II**：输入关于 \(n=-1/2\) 与 \(n=N-1/2\) 奇对称（折叠点在**半整数处**），输出关于 \(k=-1\) 奇、\(k=N-1\) 偶。
- **Type III**：输入关于 \(n=-1\) 奇、关于 \(n=N-1\) 偶（II 的「转置」/对偶）。
- **Type IV**：输入关于 \(n=-1/2\) 奇、关于 \(n=N-1/2\) 偶（半整数偏移，最「密集」）。

| type | 折叠位置 | 自逆？ | orthogonalize 起作用？ |
|------|----------|--------|------------------------|
| I    | 整数偏移（-1 与 N） | 是（差因子 \(2(N+1)\)） | 否（已近似正交） |
| II   | 半整数（输入侧）   | 否（III 是其逆） | 是（除 `y[-1]` 以 √2） |
| III  | 整数+半整数混合    | 否（II 是其逆） | 是（乘 `x[-1]` 以 √2） |
| IV   | 半整数（输入+输出）| 是（差因子 \(2N\)） | 否（已近似正交） |

注意与 DCT 的对照：DCT 中 **type II 与 III 互逆、type I 与 IV 自逆**；DST 中 **type II 与 III 同样互逆，而 type I 与 IV 自逆**。结构高度一致，区别只在「余弦 → 正弦」「偶对称 → 奇对称」。

#### 4.1.2 核心流程

DST 各 type 的定义（`norm="backward"` 下，即正向不缩放、逆向除以 N）：

**Type I**（要求输入长度 > 1，假设关于 \(n=-1\)、\(n=N\) 奇对称）：

\[
y_k = 2 \sum_{n=0}^{N-1} x_n \sin\!\left(\frac{\pi (k+1)(n+1)}{N+1}\right)
\]

未归一化的 DST-I 自身成对地为逆，差一个因子 \(2(N+1)\)。

**Type II**（假设关于 \(n=-1/2\)、\(n=N-1/2\) 奇对称）：

\[
y_k = 2 \sum_{n=0}^{N-1} x_n \sin\!\left(\frac{\pi (k+1)(2n+1)}{2N}\right)
\]

**Type III**（输入关于 \(n=-1\) 奇、关于 \(n=N-1\) 偶）：

\[
y_k = (-1)^k x_{N-1} + 2 \sum_{n=0}^{N-2} x_n \sin\!\left(\frac{\pi (2k+1)(n+1)}{2N}\right)
\]

未归一化的 DST-III 是 DST-II 的逆（差因子 \(2N\)）。

**Type IV**（输入关于 \(n=-1/2\) 奇、关于 \(n=N-1/2\) 偶）：

\[
y_k = 2 \sum_{n=0}^{N-1} x_n \sin\!\left(\frac{\pi (2k+1)(2n+1)}{4N}\right)
\]

未归一化的 DST-IV 自身成对地为逆（差因子 \(2N\)）。

四类公式的**伪代码共性**：对每个输出下标 \(k\)，把输入 \(x_n\) 与一组正弦基函数做内积，区别只在正弦项的**相位（采样点位置）**。这与 DCT 的「余弦基 + 不同采样点」是对偶的——所以底层能用同一套快速算法实现。

#### 4.1.3 源码精读

四种 type 的数学定义全部写在 `dst` 公共函数的 docstring 里，是最权威的参考。这是 Type I 与 Type II 的定义：

[_realtransforms.py:L568-L597](_realtransforms.py#L568-L597) — DST docstring 中 Type I、Type II 的数学公式与边界假设说明。例如 Type II 注明「DST-II assumes the input is odd around \(n=-1/2\) and \(n=N-1/2\)」，正是上文「半整数折叠」的来源。

[_realtransforms.py:L599-L632](_realtransforms.py#L599-L632) — DST docstring 中 Type III、Type IV 的数学公式。注意 Type IV 注明「orthogonalize has no effect here, as the DST-IV matrix is already orthogonal up to a scale factor of 2N」，这与 type I 一样，`orthogonalize` 只对 type II/III 生效。

docstring 还给了一个直白的例子，揭示了 DST 的「边界为 0」直觉：

[_realtransforms.py:L636-L644](_realtransforms.py#L636-L644) — `dst([1, -1, 1, -1], type=2)` 得到 `[0, 0, 0, 8]`。前三个系数为 0，说明这个特定输入几乎完全落在最后一个正弦基上。

#### 4.1.4 代码实践

**实践目标**：亲手验证四种 type 的自逆 / 互逆关系。

**操作步骤**：

```python
# 示例代码
import numpy as np
from scipy.fft import dst, idst

rng = np.random.default_rng(0)
x = rng.standard_normal(8)

for t in (1, 2, 3, 4):
    # 默认 norm="backward"：dst 不缩放，idst 除以 N
    y = idst(dst(x, type=t), type=t)
    print(f"type {t}: 重建误差 = {np.max(np.abs(y - x)):.2e}")
```

**需要观察的现象**：四种 type 的重建误差都应是接近机器精度的小数（约 1e-15）。type 1 与 type 4「自逆」、type 2 与 type 3「互逆」，但因为 `idst` 内部已经做了 type 翻转（详见 4.3），所以**对调用者而言**四种 type 都满足 `idst(dst(x, t), t) ≈ x`。

**预期结果**：四行重建误差均在 1e-15 量级。

> 待本地验证：若你看到某个 type 误差明显偏大，请检查是否误传了 `n`（截断/补零）导致 `idst` 与 `dst` 的长度不一致。

#### 4.1.5 小练习与答案

**练习 1**：把 `x` 的首尾两端设为 0（即 `x[0]=x[-1]=0`），分别算 `dst` 和 `dct` 的系数范数，哪个更稀疏？为什么？

> **参考答案**：DST 更稀疏。因为 DST 的奇对称延拓假设两端为 0，首尾本就为 0 的信号与该假设天然吻合，能量集中在少数正弦基上；而 DCT 假设两端导数为 0（自由端），与「两端为 0」不符，能量会更分散。

**练习 2**：DST-I 要求输入长度 `> 1`。试着调用 `dst(np.array([3.0]), type=1)`，会发生什么？

> **参考答案**：会抛出异常（C 层校验输入点数 > 1）。这与 docstring 中 Type I 的 `.. note:: The DST-I is only supported for input size > 1.` 一致。

---

### 4.2 dst / dstn / idst / idstn 的公共签名与四层分派

#### 4.2.1 概念说明

`scipy.fft` 把 DST 拆成「1-D 版本」和「N-D 版本」两组共 4 个函数：

| 函数 | 维度 | 默认变换轴/形 | 说明 |
|------|------|----------------|------|
| `dst`  | 1-D | `axis=-1`，长度 `n` | 单轴正变换 |
| `idst` | 1-D | `axis=-1`，长度 `n` | 单轴逆变换 |
| `dstn` | N-D | `axes` 默认全部轴，形 `s` | 多轴正变换 |
| `idstn`| N-D | `axes` 默认全部轴，形 `s` | 多轴逆变换 |

`dst` 可以理解为 `dstn` 在「单条轴」上的便捷封装——但注意，**它不是在 Python 层调用 `dstn`**，而是在底层都用同一个 `_r2r`/`_r2rn` 内核，分别走 1-D 与 N-D 两条预处理路径（`_fix_shape_1d` vs `_init_nd_shape_and_axes + _fix_shape`）。

这四个函数的签名与 DCT 一一对应，参数含义完全相同（详见 u3-l1）：

- `type ∈ {1,2,3,4}`，默认 2；
- `n`（1-D）/ `s`（N-D）：截断或补零后的目标长度；
- `axis`（1-D）/ `axes`（N-D）：变换轴；
- `norm ∈ {"backward","ortho","forward"}`，默认 None≡backward；
- `overwrite_x`、`workers`、`orthogonalize`。

#### 4.2.2 核心流程

一次 `dstn(x)` 调用的四层穿透（与 DCT 同构）：

1. **公共 API 层** `scipy.fft.dstn` 被 `@_dispatch` 装饰，函数体执行 `return (Dispatchable(x, np.ndarray),)`，向 uarray 声明「请把 `x` 当作可替换的 numpy 数组分派给我」。
2. **分派层** uarray 按 domain `numpy.scipy.fft` 在已注册后端里查找名为 `dstn` 的实现，命中默认的 `_ScipyBackend`。
3. **后端桥接层** `_realtransforms_backend.dstn` 调用 `_execute(_duccfft.dstn, ...)`：用 `array_namespace(x)` 记录原数组库，`np.asarray(x)` 转 numpy，算完再用 `xp.asarray(y)` 转回。
4. **计算核心层** `_duccfft.dstn` 其实是 `functools.partial(_r2rn, True, pfft.dst)`，进入 `_r2rn` 做预处理（`_asfarray` → `_init_nd_shape_and_axes` → `_fix_shape` → type 翻转 → `_normalization`），最后调用 C 扩展 `pyduccfft.dst`。

#### 4.2.3 源码精读

公共 API 层——`dstn` 的签名与 docstring，函数体只是分派声明：

[_realtransforms.py:L143-L204](_realtransforms.py#L143-L204) — `dstn` 公共函数定义。注意装饰器 `@xp_capabilities(cpu_only=True, allow_dask_compute=True)` 标注 DST 只在 CPU 上跑（因为 ducc 是 CPU 内核），且允许 Dask 延迟计算；函数体 `return (Dispatchable(x, np.ndarray),)` 不含任何计算。

后端桥接层——`_execute` 的统一封装（DCT/DST 共用）：

[_realtransforms_backend.py:L8-L15](_realtransforms_backend.py#L8-L15) — `_execute`：先用 `array_namespace(x)` 拿到原数组库 `xp`，再 `np.asarray(x)` 转 numpy，调用传入的 ducc 内核 `duccfft_func`，最后 `xp.asarray(y)` 转回。这一层把「任意数组库」与「只懂 numpy 的 ducc 内核」解耦。

后端层四个 DST 函数都是 `_execute` 的一行转发：

[_realtransforms_backend.py:L30-L39](_realtransforms_backend.py#L30-L39) — `dstn`/`idstn` 后端实现，分别转发到 `_duccfft.dstn`/`_duccfft.idstn`。

[_realtransforms_backend.py:L54-L63](_realtransforms_backend.py#L54-L63) — `dst`/`idst` 后端实现，分别转发到 `_duccfft.dst`/`_duccfft.idst`。

计算核心层的派生——`dst`/`idst`/`dstn`/`idstn` 全部由 `functools.partial` 派生：

[_duccfft/realtransforms.py:L53-L56](_duccfft/realtransforms.py#L53-L56) — `dst = functools.partial(_r2r, True, pfft.dst)`、`idst = functools.partial(_r2r, False, pfft.dst)`。`forward=True` 是正变换，`pfft.dst` 指定用「正弦内核」（而 DCT 用 `pfft.dct`）。这是 DST 与 DCT 共用内核的关键：**唯一差别就是第二个参数 `transform` 传 `pfft.dst` 还是 `pfft.dct`**。

[_duccfft/realtransforms.py:L106-L109](_duccfft/realtransforms.py#L106-L109) — N-D 版本同理：`dstn`/`idstn` 由 `_r2rn` + `pfft.dst` 派生。

#### 4.2.4 代码实践

**实践目标**：确认 `dst` 与 `dstn` 在单轴情形下数值等价，并理解 `n`/`s` 的截断与补零。

**操作步骤**：

```python
# 示例代码
import numpy as np
from scipy.fft import dst, dstn

x = np.arange(12, dtype=float).reshape(3, 4)

# dst 沿默认 axis=-1
a = dst(x)
# dstn 仅沿最后一条轴（axes=(-1,)），应与 dst 数值相同
b = dstn(x, axes=(-1,))
print("dst 与 dstn(axis=-1) 是否一致:", np.allclose(a, b))

# 截断：n=2 只取前 2 个点
print("dst(x, n=2) 形状:", dst(x, n=2).shape)
# 补零：n=6 末尾补 0
print("dst(x, n=6) 形状:", dst(x, n=6).shape)
```

**需要观察的现象**：第一行打印 `True`，说明 1-D `dst` 确实是「单轴 `dstn`」；后两行说明 `n` 控制输出长度（2 或 6），与输入长度 4 无关。

**预期结果**：
```
dst 与 dstn(axis=-1) 是否一致: True
dst(x, n=2) 形状: (3, 2)
dst(x, n=6) 形状: (3, 6)
```

#### 4.2.5 小练习与答案

**练习 1**：对一个 `(4, 5, 6)` 的数组调用 `dstn`，不传 `axes`，会在哪些轴上变换？输出形状是什么？

> **参考答案**：默认在**全部轴**上变换，输出形状仍为 `(4, 5, 6)`（因为 `s` 也默认为原形状，不截断不补零）。这来自 `_init_nd_shape_and_axes`：`s=None, axes=None` 时取 `shape = list(x.shape)`、`axes = range(x.ndim)`。

**练习 2**：为什么后端 `dst` 和 `dstn` 不直接调用 `np.fft`，而要转一道 `_execute`？

> **参考答案**：因为 `_execute` 要支持数组标准（array API）——当输入是 CuPy、PyTorch 等非 numpy 数组时，`array_namespace` 记录原库，`np.asarray` 转成 numpy 喂给只懂 numpy 的 ducc 内核，算完再 `xp.asarray` 转回原类型。这一层让 DST 对外表现为「跨数组库」，对内只用 numpy 内核。

---

### 4.3 norm 归一化与 type 翻转：idst 如何复用 dst 内核

#### 4.3.1 概念说明

`idst` 并不是一个独立实现的函数——它复用 `dst` 的内核，只做两件事：

1. **type 翻转**：type II ↔ type III 互换。因为「未归一化的 DST-III 恰是 DST-II 的逆（差一个因子）」，所以求 `idst(x, type=2)` 实际上是用 `type=3` 的内核来算；反之 `idst(x, type=3)` 用 `type=2` 的内核。type I 和 type IV 自逆，不翻转。
2. **方向翻转（norm 翻转）**：`_normalization` 用 `2 - inorm` 把归一化从「正向」翻到「逆向」，从而把 `1/N` 缩放从 idst 一侧挪到正确位置。

这与 DCT 的 `idct` 机制**完全相同**（见 u3-l1）。换句话说，DCT 和 DST 在「正逆变换如何复用内核」上用了同一套代码模式——这正是 `_r2r`/`_r2rn` 能同时服务两类变换的原因。

`norm` 三模式的含义：

- `"backward"`（默认）：`dst` 不缩放，`idst` 除以 N。
- `"forward"`：`dst` 除以 N，`idst` 不缩放（与 backward 互换）。
- `"ortho"`：正逆都乘同一个总因子，使变换矩阵正交（\(O^\top O = I\)），能量守恒。

`orthogonalize` 只对 **type II / III** 生效（type I/IV 的矩阵本身已近似正交）：
- Type II 正交化：`y[-1]` 除以 \(\sqrt{2}\)；
- Type III 正交化：`x[-1]` 乘以 \(\sqrt{2}\)。

#### 4.3.2 核心流程

`_r2r`（1-D 内核）的处理顺序：

```
输入 x
  ↓ _asfarray(x)              # 浮点化、对齐、本机字节序
  ↓ _datacopied 判断是否已拷贝
  ↓ _normalization(norm, forward)   # norm 字符串 → 整数；逆向再 2-inorm
  ↓ _workers(workers)         # 解析并行数
  ↓ 若 forward==False：type 在 {2,3} 间翻转   # ← idst 复用 dst 内核的关键
  ↓ 若给定 n：_fix_shape_1d 截断/补零
  ↓ 若输入是复数：实部、虚部分别变换
  ↓ 否则：调用 pfft.dst(...)（C 扩展）
```

N-D 版本 `_r2rn` 几乎一样，只是把 `_fix_shape_1d` 换成 `_init_nd_shape_and_axes + _fix_shape`，并且复数分支沿 `axes` 多轴变换。

`_normalization` 的整数映射（DCT/DST 共用）：

\[
\text{inorm} = \begin{cases} 0 & \text{None 或 "backward"} \\ 1 & \text{"ortho"} \\ 2 & \text{"forward"} \end{cases}, \qquad
\text{返回} = \begin{cases} \text{inorm} & \text{正向} \\ 2 - \text{inorm} & \text{逆向} \end{cases}
\]

这样 `dst(norm=backward) ↔ idst(norm=forward)`、`dst(norm=forward) ↔ idst(norm=backward)`、`ortho` 在两个方向都映射到 1，保证正逆配对恒可逆。

#### 4.3.3 源码精读

`_r2r` 中的 type 翻转（idst 复用 dst 内核的核心）：

[_duccfft/realtransforms.py:L24-L28](_duccfft/realtransforms.py#L24-L28) — `if not forward: type = 3 if type==2 else 2`。当 `forward=False`（即 `idst`/`idct`）时，把 type 在 2、3 间对调。这正是「DST-II 的逆是 DST-III」这一数学事实在代码里的体现。type 1、4 自逆，无需处理。

`_normalization` 与 `_NORM_MAP`：

[_duccfft/helper.py:L181-L192](_duccfft/helper.py#L181-L192) — `_NORM_MAP = {None: 0, 'backward': 0, 'ortho': 1, 'forward': 2}`；`_normalization` 查表得到 `inorm`，逆向时返回 `2 - inorm`。非法字符串抛 `ValueError`。注意这是 DCT/DST **共用**的归一化逻辑。

`_r2r` 的整体结构与最终调用：

[_duccfft/realtransforms.py:L8-L45](_duccfft/realtransforms.py#L8-L45) — 完整的 `_r2r`。注意第 39-43 行的复数分支：DST 对复数输入会把实部、虚部**分别**做一次实数 DST 再拼回——因为 DST 本质是实数到实数的变换，复数输入只能「拆开分别算」。最后第 45 行调用 `transform(...)`（即 `pfft.dst`），把 `orthogonalize` 透传给 C 层。

`_r2rn`（N-D）的对应片段：

[_duccfft/realtransforms.py:L59-L98](_duccfft/realtransforms.py#L59-L98) — `_r2rn`。第 81-85 行做与 `_r2r` 完全相同的 type 翻转；第 92-96 行同样的复数分支，但沿 `axes`（可能是多轴）变换。

截断/补零的预处理（`_fix_shape`）：

[_duccfft/helper.py:L146-L170](_duccfft/helper.py#L146-L170) — `_fix_shape`：对每条变换轴，若输入长度 ≥ 目标则切片截断（返回视图、不拷贝），否则先建全零数组再把输入拷进去（补零、拷贝）。第 161-162 行「无需补零」时直接返回 `x[index]`，避免无谓拷贝。

#### 4.3.4 代码实践

**实践目标**：验证 type 翻转确实发生在 `idst` 内部，并感受 `norm='ortho'` 的能量守恒。

**操作步骤**：

```python
# 示例代码
import numpy as np
from scipy.fft import dst, idst

rng = np.random.default_rng(1)
x = rng.standard_normal(8)

# (1) idst 内部做了 type 翻转：idst(x, type=2) 数值上等价于
#     先用 type=3 的 dst 内核，再施加逆向归一化。
#     直接对调用者验证可逆性即可：
print("type2 可逆:", np.allclose(idst(dst(x, 2), 2), x))
print("type3 可逆:", np.allclose(idst(dst(x, 3), 3), x))

# (2) norm='ortho' 能量守恒：||dst(x, ortho)|| == ||x||
y_ortho = dst(x, norm='ortho')
print("能量比 ||y||/||x|| =", np.linalg.norm(y_ortho) / np.linalg.norm(x))

# (3) orthogonalize 默认在 ortho 下开启；显式关闭会破坏正交性
y_no_orth = dst(x, norm='ortho', orthogonalize=False)
print("关闭 orthogonalize 后能量比 =", np.linalg.norm(y_no_orth) / np.linalg.norm(x))
```

**需要观察的现象**：
- 第 (1) 行两行都为 `True`。
- 第 (2) 行能量比应非常接近 1（正交变换保范数）。
- 第 (3) 行能量比**不再**接近 1——因为 `orthogonalize=False` 让变换矩阵不再正交，能量不再守恒（这正是 docstring 里那条 warning 的含义：`norm="ortho"` 下默认开启 `orthogonalize` 才能真正正交）。

**预期结果**：能量比 (2) ≈ 1.0；能量比 (3) 偏离 1。

> 待本地验证：不同随机种子下 (3) 的偏离程度不同，但应明显大于 (2)。

#### 4.3.5 小练习与答案

**练习 1**：`idst(dst(x, type=2, norm="forward"), type=2, norm="backward")` 是否等于 `x`？为什么？

> **参考答案**：等于 `x`（在数值精度内）。因为 `forward` 让 `dst` 提前除以 N，而 `idst` 在 `backward` 下本就要除以 N——但 `_normalization` 在逆向时返回 `2 - inorm`，会把 `forward` 的 idst 映射成 `backward` 的 dst 同侧缩放，使正逆缩放互补。本质上「正向 forward + 逆向 backward」与「正向 backward + 逆向 forward」是同一对可逆配对。

**练习 2**：为什么 `dst(x, type=1)` 和 `dst(x, type=4)` 即使指定 `orthogonalize=True` 也不会改变结果？

> **参考答案**：因为 DST-I 与 DST-IV 的变换矩阵本身已经（在差一个标量因子的意义下）正交，docstring 明确写明「orthogonalize has no effect here」。`orthogonalize` 只对 type II/III 的首尾元素做 √2 修正，对 I/IV 无事可做。

---

## 5. 综合实践：用 DST 做信号压缩

**任务**：构造一个满足「正弦边界条件」的信号（两端为 0），用 `dstn` 变换到频域，**只保留前 k 个系数**（截断高频），再用 `idstn` 重建，观察能量保留比例与重建误差随 k 的变化。

```python
# 示例代码
import numpy as np
from scipy.fft import dstn, idstn

N = 64
n = np.arange(N)
# 两端为 0 的平滑信号：一个低频正弦 + 一点高频抖动
x = np.sin(np.pi * n / (N - 1)) + 0.2 * np.sin(7 * np.pi * n / (N - 1))
x[0] = x[-1] = 0.0  # 严格满足正弦边界

X = dstn(x, norm='ortho')           # 正交 DST，能量守恒
total_energy = np.sum(X**2)

for k in (4, 8, 16, 32, N):
    Xk = X.copy()
    Xk[k:] = 0                      # 截断：只保留前 k 个系数
    x_rec = idstn(Xk, norm='ortho')
    retained = np.sum(Xk**2) / total_energy
    err = np.max(np.abs(x_rec - x))
    print(f"k={k:3d}  能量保留={retained:6.3f}  最大重建误差={err:.2e}")
```

**需要观察的现象**：随着 k 增大，能量保留比例从远低于 1 逐步逼近 1，重建误差同步下降。由于信号本身是低频的、且满足 DST 的边界假设，**只需很少的系数（小 k）就能保留绝大部分能量**——这正是 DST 适合「两端固定」信号压缩的原因。

**延伸思考**：把同一个信号也用 `dctn` 压缩一遍，对比相同 k 下的能量保留比例。你会发现因为信号两端为 0（不满足 DCT 的「自由端」假设），DCT 需要更多系数才能达到同样的重建质量——这就直观回答了「何时该用 DST 而非 DCT」。

**预期结果**：待本地验证（具体数值依赖信号与 N，但趋势应是「k 很小即可高能量保留」）。

---

## 6. 本讲小结

- DST 是 DCT 的姊妹变换：把信号的**奇对称延拓**傅里叶级数里只剩的「正弦项」离散化，天然适合「两端为 0」的信号（固定端振动、泊松方程内部点）。
- 四种 type 的差异完全来自**奇对称折叠点的位置（整数 vs 半整数）**；type II/III 互逆，type I/IV 自逆，结构与 DCT 高度同构。
- `dst`/`idst`/`dstn`/`idstn` 的函数体仅 `return (Dispatchable(x, np.ndarray),)`，是分派声明；真正计算走「公共 API → uarray → `_execute` 桥接 → `_duccfft` → `pyduccfft`」四层链路。
- DCT 与 DST **共用** `_r2r`/`_r2rn` 内核，唯一区别是 `functools.partial` 的第二个参数传 `pfft.dct` 还是 `pfft.dst`。
- `idst` 通过 **type 翻转（2↔3）+ norm 方向翻转（`2 - inorm`）** 复用 `dst` 内核，与 `idct` 机制完全相同。
- `norm='ortho'` 配合默认 `orthogonalize=True`（仅对 type II/III）使变换矩阵正交、能量守恒，是做系数截断压缩的前提。

---

## 7. 下一步学习建议

- **继续向下钻计算核心**：本讲止步于 `_r2r`/`_r2rn` 调用 `pfft.dst`。下一讲 **u3-l3（实变换后端 `_execute`）** 会更细致地剖析 `_realtransforms_backend._execute` 如何桥接 numpy 与任意数组库，并对比它与 `_basic_backend` 的异同。
- **补全预处理细节**：本讲提到的 `_asfarray`、`_fix_shape`、`_normalization`、`_workers` 在 **u5-l2（输入预处理）** 有专门讲解，建议在那里深入 `_fix_shape` 的截断/补零实现。
- **并行与多线程**：`workers` 参数的实际效果在 **u5-l3（并行 workers）** 展开，可结合 DST 的复数分支理解「实部/虚部分别变换」如何受 workers 影响。
- **验证型阅读**：打开 [`tests/test_real_transforms.py`](tests/test_real_transforms.py)，对照其中的 DST 测试断言，验证你对 type 翻转与 norm 翻转的理解是否与官方测试一致。
