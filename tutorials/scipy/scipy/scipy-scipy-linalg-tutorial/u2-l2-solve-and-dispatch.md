# 线性方程组求解 solve 与 assume_a 调度

## 1. 本讲目标

在上一讲（u1-l4）你已经用 `solve` 解过一次线性方程组 `A @ x = b`，并知道 `scipy.linalg` 的整体架构是「Python 薄层做校验与错误聚合 + 编译后端 `_batched_linalg` 做数值计算」。本讲我们钻进 `solve` 的内部，搞清楚一个关键问题：

> 同样是 `solve(A, b)`，为什么传入「对称正定矩阵」和「普通稠密矩阵」时，底层会走完全不同的求解器？这个「选哪条路」的决定，是在哪里、依据什么做出的？

学完本讲，你应当能够：

- 说清 `solve` 的 `assume_a` 参数有哪些取值，以及它如何把一个字符串翻译成给后端的结构编码。
- 区分「自动检测结构」（`assume_a=None`）与「手动指定结构」（显式给字符串）两条路径，并理解为什么手动指定更省时也更危险。
- 解释 `lower`、`transposed`、`check_finite`、`overwrite_a/overwrite_b` 这些公共参数在 `solve` 中的具体作用与边界条件。
- 描述 `_format_emit_errors_warnings` 如何把批量求解时每一片（slice）的「奇异 / LAPACK 内部错误 / 病态」分类汇总，翻译成一句 `LinAlgError` 或 `LinAlgWarning`。
- 理解 `_datacopied` 这个小工具如何判断「输入数据是否已被拷贝」，并据此决定能否安全地原地覆写。

本讲覆盖的最小模块：**`solve`**、**`_format_emit_errors_warnings`**、**`_datacopied`**。

## 2. 前置知识

如果你看过上一讲，下面这些词应该不陌生，这里再点一句它们的角色：

- **线性方程组**：形如 \(A x = b\)，其中 \(A\) 是 \(N\times N\) 方阵，\(b\) 是右端项，\(x\) 是未知解。求解就是找出 \(x\)。
- **结构化矩阵**：矩阵并非「随机稠密」，而是有规律——例如对角阵只在主对角线上非零、三角阵的某一半全零、对称阵满足 \(A=A^{T}\)、Hermitian 阵满足 \(A=A^{H}\)（\(H\) 表示共轭转置）、正定阵的所有特征值都为正。不同结构对应不同且更快的 LAPACK 例程。
- **`check_finite`**：是否用 `asarray_chkfinite` 拦截输入里的 NaN/Inf（详见 u1-l4）。
- **`overwrite_a/overwrite_b`**：是否允许后端把结果直接写回输入数组以省一次拷贝、换性能。
- **`LinAlgError` / `LinAlgWarning`**：前者表示求解失败（如奇异矩阵），后者表示「能解但精度可能受损」（如病态）。二者都在 `_misc.py` 定义，`LinAlgError` 实际复用 NumPy 的同名异常。
- **批量维度（batch dimensions）**：`A` 可以是 `(..., N, N)`，前面的 `...` 被当作「一摞矩阵」。本讲会在错误聚合处用到这个概念，批量的完整机制留到 u8-l1。
- **带宽 / 对称判定**：上一讲（u2-l1）讲过 `bandwidth` 返回 `(lower, upper)`、`issymmetric/ishermitian` 判定结构。本讲的自动检测正是复用了「算带宽 + 判对称」这套能力（只是搬到了 C++ 后端里跑）。

一句话复习：**`solve` 的全部聪明，都体现在「先认出 \(A\) 是什么结构，再挑最快的专用求解器」这件事上。**

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|------|------|-----------|
| [_basic.py](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py) | 基础求解族（`solve`、`inv`、`det`、`lstsq` 等）的 Python 薄层实现 | `solve` 的参数校验、`assume_a`→结构编码、调用后端、错误聚合入口 |
| [_misc.py](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_misc.py) | 杂项工具：`norm`、`bandwidth`、`_datacopied`、`LinAlgWarning` | `_datacopied` 的拷贝检测逻辑 |
| [src/_linalg_solve.hh](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/src/_linalg_solve.hh) | C++ 批量后端里 `solve` 的真正实现（各结构的 slice 求解器 + 自动检测 + 分派） | 自动检测顺序、`switch` 分派（帮助理解 Python 层 `structure` 编码的后果） |
| [src/_common_array_utils.hh](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/src/_common_array_utils.hh) | C++ 公共工具，定义结构枚举 `St` | `St` 枚举的取值表，印证 Python 侧编码 |

> 说明：`src/_linalg_solve.hh` 与 `src/_common_array_utils.hh` 属于编译后端（u1-l3 讲过 `_batched_linalg` 是 C++ 扩展）。本讲以 Python 层为主战场，但「调度真正发生在哪」必须看到 C++ 层才能讲清楚，故补充引用，行号均对应当前 HEAD。

## 4. 核心概念与源码讲解

### 4.1 solve：从 assume_a 到结构分派

#### 4.1.1 概念说明

`solve(a, b)` 解的是 \(A x = b\)。乍看它只是「解方程」，但它最大的价值是**结构自适应**：

- 如果你**什么都不说**（`assume_a=None`，这也是默认值），它会**自动识别** \(A\) 的结构——是对角、三对角、三角、对称、Hermitian、正定，还是普通稠密——然后挑一个最贴切的专用求解器。
- 如果你**明确告知**结构（例如 `assume_a='pos'` 表示对称正定），它会**跳过识别**，直接走对应专用求解器。这样更快，但前提是你没说错——说错了会得到错误答案（详见下面的 docstring 例子）。

为什么要这么费劲？因为不同结构的 LAPACK 例程在速度和稳定性上差异巨大：

| 结构（assume_a） | 典型 LAPACK 例程 | 相对开销 |
|------|------|------|
| 对角 `diagonal` | 逐元素除法 | 极低 |
| 三对角 `tridiagonal` | `?gtsv` | 低 |
| 正定 `pos` | Cholesky `?potrf/?potrs` | 低（无主元，稳定的前提是确实正定） |
| 对称 `sym` / Hermitian `her` | `?sytrf/?hetrf` | 中 |
| 三角 `upper/lower triangular` | `?trtrs` | 低 |
| 普通 `gen` | LU 主元 `?getrf/?getrs` | 高（最通用） |

> 注：`?` 是 LAPACK 的类型前缀占位符（`s/d/c/z` 对应单/双精度实/复），由后端按 dtype 自动替换。这部分在第 7 单元（u7-l1）会详细讲。

所以 `assume_a` 的本质，是给后端一个**整数编码**，告诉它「别再猜了，就是这个结构」；而 `None` 则编码成「请你猜」。

#### 4.1.2 核心流程

`solve` 的 Python 层可以概括成「**校验 → 归一化 → 编码 → 委派 → 汇报**」五步：

```
solve(a, b, assume_a=None, lower=False, transposed=False, ...):
  1. assume_a 字符串  →  structure 整数编码   (字典查表；None→-1 表示「请你猜」)
  2. a, b 转 ndarray、check_finite 拦 NaN/Inf、dtype/内存对齐归一化
  3. 形状校验：A 必须方阵；b 与 A 行数对齐；处理 b 为 1-D 的兼容；广播批量维度
  4. 快路径：空数组 / 标量(1×1) 直接返回，不走后端
  5. overwrite_* 门控（见 4.3）
  6. 委派：x, err_lst = _batched_linalg._solve(a1, b1, structure, lower, transposed, ...)
  7. 汇报：若 err_lst 非空 → _format_emit_errors_warnings(err_lst)（见 4.2）
  8. 返回 x（1-D 输入对应 1-D 输出）
```

真正「认结构 + 选例程」发生在第 6 步的 C++ 后端 `_batched_linalg._solve` 里。当 `structure == -1`（即 `assume_a=None`）时，后端**逐片（per-slice）**地做检测；当 `structure` 是一个明确的正整数时，后端直接跳过检测。

后端的自动检测顺序（在 C++ 里）大致是：

```
对每一片矩阵：
  若 structure != -1（用户已指定）：直接用，不猜
  否则（structure == -1）：
    先算带宽 (lower_band, upper_band)
      upper==0 且 lower==0        → 对角
      upper==1 且 lower==1 且 n>3 → 三对角
      lower==0                    → 上三角
      upper==0                    → 下三角
      否则判对称/Hermitian：
        Hermitian 或 (实对称)     → 先试 Cholesky(正定)，失败再回退 sym/her
        复对称(非 Hermitian)      → sym
        都不是                    → gen(普通稠密)
```

注意一个重要细节：自动检测把「实对称 / 复 Hermitian」**乐观地先当作正定去试 Cholesky**，因为正定求解最快；只有 Cholesky 失败了才回退到 `?sytrf/?hetrf`。这就是为什么 `solve` 对一个对称正定矩阵常常「自动」就很快。

#### 4.1.3 源码精读

**(a) 函数签名与 assume_a 选项表**

`solve` 的签名与文档列出了全部合法结构（[_basic.py:L57-L59](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py#L57-L59)），docstring 中的选项表（[_basic.py:L68-L78](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py#L68-L78)）即对应 9 种结构。注意 `assume_a=None` 时「checks are performed to identify structure」——这正是自动检测的入口。

**(b) 字符串 → 结构编码（关键！）**

进入函数体第一件事，就是把 `assume_a` 字符串查表翻译成整数 `structure`（[_basic.py:L187-L200](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py#L187-L200)）。关键片段：

```python
# keep the numbers in sync with C
structure = {
    None: -1,
    'general': 0, 'gen': 0,
    'diagonal': 11,
    'tridiagonal': 31,
    'banded': 41,
    'upper triangular': 21,
    'lower triangular': 22,
    'pos' : 101, 'positive definite': 101,
    'sym' : 201, 'symmetric': 201,
    'her' : 211, 'hermitian': 211,
}.get(assume_a, 'unknown')
if structure == 'unknown':
    raise ValueError(f'{assume_a} is not a recognized matrix structure')
```

要点：

- `None → -1` 是「请你猜」的信号；其余每个字符串都对应一个固定正整数。
- 注释 `# keep the numbers in sync with C` 提醒：这些数字必须和 C++ 后端的枚举一一对应。后端的枚举定义在 [_common_array_utils.hh:L911-L924](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/src/_common_array_utils.hh#L911-L924)：

```cpp
// Structure tags; python side maps assume_a strings to these values
enum St : Py_ssize_t
{
    NONE = -1,
    GENERAL = 0,
    DIAGONAL = 11,
    TRIDIAGONAL = 31,
    BANDED = 41,
    UPPER_TRIANGULAR = 21,
    LOWER_TRIANGULAR = 22,
    POS_DEF = 101,
    SYM = 201,
    HER = 211
};
```

  对比可见 `11/31/41/21/22/101/201/211` 完全一致。Python 层只是把字符串「翻译」成这个枚举值，真正的调度在 C++。

- 同义词合并：`'gen'` 与 `'general'` 都映射到 `0`，`'pos'` 与 `'positive definite'` 都映射到 `101`，便于书写。
- 非法值会被 `.get(..., 'unknown')` 捕获并抛 `ValueError`，避免把垃圾整数塞给后端。

**(c) 校验、形状对齐、快路径**

接着是把 `a, b` 转 ndarray、`check_finite` 拦截、dtype/对齐归一化（[_basic.py:L202-L209](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py#L202-L209)）；强制 `A` 是方阵（[_basic.py:L211-L214](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py#L211-L214)）。

`transposed=True` 在复数情形下直接拒绝（[_basic.py:L217-L220](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py#L217-L220)），因为求解 \(A^{T}x=b\) 与 \(A^{H}x=b\) 对复数有歧义，目前尚未实现。

为兼容 NumPy 的 `dot` 习惯，1-D 的 `b`（长度 N）会被临时当作 N×1 列向量处理（[_basic.py:L223-L225](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py#L223-L225)），最后再压回 1-D 返回。

空数组与 1×1 标量有专门快路径，根本不进后端：标量情形直接 `b / a`，且 `a==0` 时直接抛 `LinAlgError`（[_basic.py:L237-L249](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py#L237-L249)）。

**(d) overwrite 门控 + 委派 + 汇报**

真正的「重活」只有两行（[_basic.py:L257-L262](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py#L257-L262)）：

```python
# heavy lifting
x, err_lst = _batched_linalg._solve(
    a1, b1, structure, lower, transposed, overwrite_a, overwrite_b
)

if err_lst:
    _format_emit_errors_warnings(err_lst)
```

也就是说：Python 层把 `structure`、`lower`、`transposed`、`overwrite_*` 一股脑交给 C++ 后端 `_solve`，后端返回解 `x` **和**一个错误清单 `err_lst`；只要清单非空，就交给 `_format_emit_errors_warnings` 翻译成异常或告警（见 4.2）。

**(e) 后端的自动检测与分派（C++，帮助理解）**

后端 `_solve` 在 `structure == St::NONE`（即 -1）时，对每一片矩阵按带宽→对称性顺序检测（[_linalg_solve.hh:L591-L624](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/src/_linalg_solve.hh#L591-L624)），随后用 `switch(slice_structure)` 把检测/指定的结果分派到对应的 slice 求解器（[_linalg_solve.hh:L628-L680](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/src/_linalg_solve.hh#L628-L680)）。关键点：**检测是逐片独立进行的**，所以一摞矩阵里每片可以被认成不同结构——这正是 docstring 里「each of the two slices will be independently discovered」的含义。

#### 4.1.4 代码实践

> 实践目标：直观感受 `assume_a` 如何改变求解路径，并体会「手动指定错了会怎样」。

操作步骤（可写成一个脚本运行）：

```python
# 示例代码：请自行运行，观察输出
import numpy as np
from scipy.linalg import solve
import warnings

rng = np.random.default_rng(0)
# 构造一个对称正定矩阵：M^T @ M 必然对称正定
B = rng.standard_normal((5, 5))
A = B.T @ B
b = rng.standard_normal(5)

# 1) 不指定结构：后端会自动检测出它是对称正定
x_auto = solve(A, b)
print("auto 残差 ||A x - b|| =", np.linalg.norm(A @ x_auto - b))

# 2) 明确指定为正定：跳过检测，直接走 Cholesky
x_pos = solve(A, b, assume_a='pos')
print("pos  残差 ||A x - b|| =", np.linalg.norm(A @ x_pos - b))
print("auto vs pos 解的最大差 =", np.max(np.abs(x_auto - x_pos)))   # 应接近 0

# 3) 故意指定错误结构：把一个非对角阵当成对角阵
A2 = rng.standard_normal((4, 4))
b2 = rng.standard_normal(4)
x_wrong = solve(A2, b2, assume_a='diagonal')   # 后端只看对角元，忽略 off-diagonal
print("错误指定 residual =", np.linalg.norm(A2 @ x_wrong - b2))    # 会很大！
```

需要观察的现象：

1. 第 1、2 步的残差都应很小，且两组解几乎一致——说明自动检测与显式 `'pos'` 走的是等价（或更快）的路径。
2. 第 3 步残差会很大，证明**手动指定错误结构不会报错，只会给出错误答案**——这正是 `assume_a` 的「承诺」语义：你说它是什么，后端就信什么，不再核对。

预期结果：第 1、2 步解差约为 `1e-15` 量级；第 3 步残差与 `||b||` 同量级（即完全没解对）。若你的环境未安装 SciPy，则为「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：`assume_a` 取 `'sym'` 和 `'positive definite'` 时，对应的 `structure` 整数分别是多少？它们在后端 `St` 枚举里叫什么？

答案：`'sym'` → `201`，对应 `St::SYM`；`'positive definite'` → `101`，对应 `St::POS_DEF`。

**练习 2**：为什么对**复数**矩阵调用 `solve(A, b, transposed=True)` 会抛 `NotImplementedError`？

答案：因为对复矩阵，「转置 \(A^{T}\)」与「共轭转置 \(A^{H}\)」是两回事，存在歧义，而当前实现尚未区分这两种语义，故在 [_basic.py:L217-L220](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py#L217-L220) 直接拒绝；实数矩阵则不受影响（\(A^{T}=A^{H}\)）。

**练习 3**：自动检测把「实对称」矩阵乐观地先当成 `POS_DEF` 试 Cholesky。如果它其实对称但**不**正定，会发生什么？

答案：Cholesky（`potrf`）会失败，后端检测到失败后**回退**到对称求解器 `?sytrf/?hetrf`（见 [_linalg_solve.hh:L656-L679](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/src/_linalg_solve.hh#L656-L679) 的 `posdef_fallback` 分支），最终仍能正确求解，只是多花了一次试错。

---

### 4.2 _format_emit_errors_warnings：批量求解的错误聚合

#### 4.2.1 概念说明

后端 `_solve` 返回的不只是解 `x`，还有一个**错误清单** `err_lst`——当输入是批量（一摞矩阵）时，每一片都可能有自己的状态：有的正常、有的奇异、有的病态、有的内部 LAPACK 报错。

`solve` 的 Python 层不可能对每一片单独抛一次异常（那样批量求解就失去意义），于是用一个汇总函数 `_format_emit_errors_warnings` 把整摞的状态**分类聚合**，再决定：

- 只要**有任何一片奇异** → 抛一个 `LinAlgError`，列出所有奇异片的下标；
- 只要**有任何一片** LAPACK 内部出错 → 抛 `ValueError`；
- 只要**有任何一片病态**（不致命，但精度受损）→ 发一个 `LinAlgWarning`。

这套「先攒着、最后一次性汇报」的设计，是 `scipy.linalg` 批量后端（u8 单元）的通用错误处理范式，`inv`、`lstsq` 也复用同一个函数。

#### 4.2.2 核心流程

```
_format_emit_errors_warnings(err_lst):     # err_lst: 每片一个字典 dct
  遍历每一片 dct，按三个布尔/数值字段分桶：
    dct["is_singular"]        True  → 记入 singular 列表(存片下标 i)
    dct["lapack_info"] < 0          → 记入 lapack_err 列表(存描述串)
    dct["is_ill_conditioned"] True  → 记入 ill_cond 列表(存 rcond 值)
  汇报(优先级从高到低)：
    singular 非空 → raise LinAlgError("A singular matrix detected: slice(s) ...")
    lapack_err 非空 → raise ValueError("Internal LAPACK errors: ...")
    ill_cond  非空 → warnings.warn(..., LinAlgWarning)
```

注意优先级：**奇异是最严重的，先抛**；如果既奇异又病态，你只会看到 `LinAlgError`。三桶都空则什么都不发（求解全部成功）。

每个 `dct` 的字段含义（对应后端 `SliceStatus`）：

| 字段 | 含义 |
|------|------|
| `is_singular` | 该片矩阵奇异（如 LU 出现零主元），无法求解 |
| `lapack_info` | LAPACK 返回的 `info`；`<0` 表示调用参数非法（属内部错误），`>0` 通常表示数值奇异 |
| `is_ill_conditioned` | 该片条件数很差（`rcond` 很小），能解但精度无保证 |
| `rcond` | 倒数条件数估计，越接近 0 越病态 |

#### 4.2.3 源码精读

整个函数非常短，定义在 [_basic.py:L27-L54](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py#L27-L54)：

```python
def _format_emit_errors_warnings(err_lst):
    """Format/emit errors/warnings from a lowlevel batched routine.
    See inv, solve.
    """
    singular, lapack_err, ill_cond = [], [], []
    for i, dct in enumerate(err_lst):
        if dct["is_singular"]:
            singular.append(i)
        if dct["lapack_info"] < 0:
            lapack_err.append(f"slice {i} emits lapack info={dct['lapack_info']}")
        if dct["is_ill_conditioned"]:
            ill_cond.append(f"slice {i} has rcond = {dct['rcond']}")

    if singular:
        raise LinAlgError(
            f"A singular matrix detected: slice(s) {singular} are singular."
        )
    if lapack_err:
        raise ValueError(f"Internal LAPACK errors: {','.join(lapack_err)}.")
    if ill_cond:
       warnings.warn(
            f"An ill-conditioned matrix detected: {','.join(ill_cond)}.",
            LinAlgWarning,
            stacklevel=3
        )
```

阅读要点：

- `singular` 只存**下标** `i`（因为奇异只需知道是哪几片），而 `lapack_err`、`ill_cond` 存**带信息的字符串**（便于诊断具体的 `info` / `rcond`）。
- docstring 里 `See inv, solve.` 一句点明它是被 `solve`（[_basic.py:L261-L262](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py#L261-L262)）和 `inv`（[_basic.py:L1122-L1123](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py#L1122-L1123)）共用的；`lstsq` 也走同一入口（[_basic.py:L1445-L1446](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py#L1445-L1446)）。
- `stacklevel=3` 是为了让 `warnings.warn` 把告警的「来源行」指向**用户调用 `solve` 的那一行**，而不是这个内部函数自己——这是 Python `warnings` 的常见调参技巧。

#### 4.2.4 代码实践

> 实践目标：分别触发「奇异」与「病态」两种汇报，体会聚合行为。

```python
# 示例代码
import numpy as np
from scipy.linalg import solve, LinAlgError
import warnings

# --- A) 触发 LinAlgError：批量里混入一个奇异片 ---
A_ok = np.array([[4.0, 1.0], [1.0, 3.0]])      # 正定，正常
A_bad = np.array([[1.0, 2.0], [2.0, 4.0]])     # 第二行 = 2×第一行，奇异
batch = np.stack([A_ok, A_bad])                # shape (2, 2, 2)
b = np.array([1.0, 1.0])
try:
    solve(batch, b)
except LinAlgError as e:
    print("捕获到:", e)   # 预期提到 slice(s) [1] 奇异

# --- B) 触发 LinAlgWarning：构造一个病态(接近奇异)矩阵 ---
hillb = np.array([[1, 1/2, 1/3],
                  [1/2, 1/3, 1/4],
                  [1/3, 1/4, 1/5]], dtype=float)   # Hilbert 片段，著名病态
with warnings.catch_warnings(record=True) as w:
    warnings.simplefilter("always")
    x = solve(hillb, np.array([1.0, 1.0, 1.0]))
    for wi in w:
        print("告警类别:", wi.category.__name__, "|", wi.message)
```

需要观察的现象：

- A 段：即便第一片正常，只要第二片奇异，整次调用就抛 `LinAlgError`，且消息里**点名** `slice(s) [1]`——这就是「聚合后统一汇报」。
- B 段：病态矩阵能解出 `x`，但同时发出一条 `LinAlgWarning`（`stacklevel` 让它看起来源自你的 `solve(...)` 行）。

预期结果：A 段打印含 `slice(s) [1]` 的异常；B 段打印一条 `LinAlgWarning`。若环境无 SciPy，则为「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：如果同一片矩阵**既**奇异**又**病态，用户最终看到的是异常还是告警？

答案：看到 `LinAlgError`。因为 `singular` 检查在 `ill_cond` 之前，一旦 `singular` 非空就 `raise`，函数直接结束，不会再走到 `warnings.warn`。

**练习 2**：`lapack_info < 0` 和 `lapack_info > 0` 在本函数里待遇为何不同？

答案：本函数**只**把 `lapack_info < 0`（参数非法等内部错误）汇总成 `ValueError`；`> 0`（通常表示数值奇异）不会进 `lapack_err` 桶——奇异与否是由 `is_singular` 这个布尔字段单独标记的。

---

### 4.3 _datacopied：拷贝检测与 overwrite

#### 4.3.1 概念说明

上一讲（u1-l4）提到 `overwrite_a/overwrite_b` 是「允许后端把结果写回输入数组」的性能开关。但这里有个隐患：如果用户传进来的数组**还另有引用**（比如它是某个大数组的一个视图），后端原地覆写就会**污染用户的数据**。

于是需要一个判断：**「`asarray(original)` 之后得到的新数组，到底和 `original` 共享内存，还是已经是独立副本？」** 这就是 `_datacopied(arr, original)` 的职责。它返回 `True` 表示「已经拷贝过了，可以安全覆写」；返回 `False` 表示「还共享着，不能贸然覆写」。

`solve` 主函数本身用的是另一套更严格的门控（见 4.3.3），但它的同族函数 `solve_triangular`、`solve_banded`、`solveh_banded` 都直接用 `_datacopied` 来决定 `overwrite_b`。理解它，就理解了整个结构化求解族的「省拷贝」机制。

#### 4.3.2 核心流程

```
_datacopied(arr, original):    # 前提：arr = asarray(original)
  if arr is original:                 → False   (同一对象，肯定没拷贝)
  if original 不是 ndarray 但有 __array__: → False (保守起见，假设可能共享)
  return arr.base is None             → True 表示 arr 是全新独立数组(已拷贝)
```

关键概念是 NumPy 的 `.base` 属性：一个数组若是由 `asarray` **拷贝**而来，它的 `.base` 为 `None`（自己就是数据所有者）；若是**视图**（切片、转置等），`.base` 会指向原始数据块。所以 `arr.base is None` 正是「我是独立副本」的判据。

#### 4.3.3 源码精读

`_datacopied` 定义在 [_misc.py:L184-L194](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_misc.py#L184-L194)：

```python
def _datacopied(arr, original):
    """
    Strict check for `arr` not sharing any data with `original`,
    under the assumption that arr = asarray(original)
    """
    if arr is original:
        return False
    if not isinstance(original, np.ndarray) and hasattr(original, '__array__'):
        return False
    return arr.base is None
```

三个分支的用意：

1. `arr is original`：连对象都同一个，显然没拷贝 → `False`。
2. `original` 不是 `np.ndarray` 但带 `__array__`（例如 `numpy.matrix` 或别的数组协议对象）：保守返回 `False`，因为这类对象与转换结果之间可能共享缓冲区，难以保证。
3. 兜底用 `arr.base is None`：这是 NumPy 下「我是数据所有者」的可靠信号。

**它在哪被用？** `solve_triangular` 里这样写（[_basic.py:L368](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py#L368)）：

```python
overwrite_b = overwrite_b or _datacopied(b1, b)
```

含义是：如果 `_asarray_validated` 已经把 `b` 拷贝成了 `b1`，那 `b1` 本来就是后端可以随便写的副本，于是把 `overwrite_b` 置 `True`，省去后端再来一次拷贝。`solve_banded`（[_basic.py:L497](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py#L497)、[_basic.py:L508](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py#L508)）、`solveh_banded`（[_basic.py:L647-L648](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py#L647-L648)）用法相同。

**那 `solve` 主函数呢？** 它没直接调 `_datacopied`，而是用一条更严的「与」门控（[_basic.py:L253-L254](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py#L253-L254)）：

```python
overwrite_a = overwrite_a and (a1.ndim == 2) and (a1.flags["F_CONTIGUOUS"])
overwrite_b = overwrite_b and (b1.ndim <= 2) and (b1.flags["F_CONTIGUOUS"])
```

即只有同时满足「用户主动允许 + 恰好二维 + Fortran 列主序连续」三个条件，`overwrite_*` 才真正生效。原因是 LAPACK 默认按列主序工作，只有 Fortran 连续的二维数组才能被原地复用，否则仍需拷贝。`inv`（[_basic.py:L1105](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py#L1105)）用的是同一套门控。

> 小结：`_datacopied` 回答「拷没拷过」，`flags["F_CONTIGUOUS"]` 回答「布局能不能原地用」。两者都是为了让 `overwrite_*` 在「绝不污染用户数据」的前提下尽量省一次拷贝。

#### 4.3.4 代码实践

> 实践目标：直接观察 `_datacopied` 在不同输入下的返回值，理解 `.base` 的含义。

```python
# 示例代码（直接调用内部函数；它以 _ 开头，属实现细节，这里仅用于学习）
import numpy as np
from scipy.linalg._misc import _datacopied

a = np.array([[1.0, 2.0], [3.0, 4.0]])     # C 序 ndarray
a_view = a[0]                               # a 的视图

# 1) asarray 一个已是 ndarray 且同 dtype 的对象：不拷贝
b1 = np.asarray(a)
print(_datacopied(b1, a))   # 预期 False（b1 is a 同对象 / base 非空）

# 2) asarray 时强制类型转换：必然拷贝
b2 = np.asarray(a, dtype=np.complex128)
print(_datacopied(b2, a))   # 预期 True（新副本，base is None）

# 3) 传入 list：asarray 会新建数组
b3 = np.asarray([[1, 2], [3, 4]])
print(_datacopied(b3, [[1, 2], [3, 4]]))    # 预期 True

# 4) 看看 .base：拷贝出的数组 base 为 None，视图的 base 指向原数组
print("b2.base is None ?", b2.base is None)   # True
print("a_view.base is a ?", a_view.base is a) # True
```

需要观察的现象：拷贝出来的数组 `.base is None`，`_datacopied` 返回 `True`；视图或同一对象返回 `False`。这与上面「能否安全覆写」的结论一致。

预期结果：依次打印 `False / True / True / True / True`。若环境无 SciPy，则为「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：用户传入一个 Python `list`，`_asarray_validated` 会把它变成 ndarray。此时 `_datacopied(arr, original_list)` 返回什么？为什么这反而让 `overwrite_b` 可以安全置真？

答案：返回 `True`。因为 list 不持有任何 NumPy 缓冲区，`asarray` 必然新建一个独立数组（`.base is None`），后端覆写它绝不会污染用户的原始数据，所以把它当可覆写副本是安全的。

**练习 2**：`solve` 主函数为什么**不**用 `_datacopied`，而改用 `flags["F_CONTIGUOUS"]` 门控？

答案：因为 `solve` 委派给的后端 `_batched_linalg._solve` 需要按 Fortran 列主序原地工作；即使数据已被拷贝，只要它不是 F 连续的二维数组，后端也无法直接复用其缓冲区。所以 `solve` 多检查一道布局条件，比单纯判断「拷没拷过」更贴合后端的真实约束。

## 5. 综合实践

把本讲三个最小模块串起来，完成下面这个「带诊断的批量求解」小任务：

**场景**：你拿到一摞 3 个 4×4 矩阵 `batch`（shape `(3, 4, 4)`）和右端 `b`。你怀疑其中有的正定、有的对称但非正定、有的普通稠密。请你：

1. **先用自动检测**求解 `solve(batch, b)`，检查是否抛异常或告警；若有，依据 `_format_emit_errors_warnings` 的输出判断是哪几片出了什么问题。
2. 对其中你**确信**是正定的那一片（用 `np.allclose(batch[i], batch[i].T)` 验证对称、再用 Cholesky `scipy.linalg.cholesky` 尝试以验证正定），**单独**用 `solve(batch[i], b, assume_a='pos')` 求解，对比与自动检测的结果。
3. 对一个普通稠密片，分别用 `assume_a='gen'` 和 `assume_a=None` 求解，验证答案一致。
4. （选做）把其中一个矩阵换成它的转置视图（非 F 连续），调用 `solve` 并思考：此时 `overwrite_a=True` 是否真的会原地覆写？结合 4.3.3 的门控条件给出判断。

参考骨架（请自行补全并运行）：

```python
# 示例代码（骨架，请补全）
import numpy as np
from scipy.linalg import solve, cholesky, LinAlgError
import warnings

rng = np.random.default_rng(42)
M = rng.standard_normal((4, 4))
pos = M.T @ M                       # 对称正定
sym = M + M.T                       # 对称(未必正定)
gen = rng.standard_normal((4, 4))   # 普通
batch = np.stack([pos, sym, gen])
b = rng.standard_normal(4)

# 1) 自动检测批量求解，捕获可能的异常/告警
#    ... 你的代码：try/except LinAlgError + catch_warnings ...

# 2) 对正定片用 assume_a='pos'
#    ... 你的代码 ...

# 3) 对普通片比较 'gen' 与 None
#    ... 你的代码 ...
```

预期：正定片两种方式解一致；普通片 `'gen'` 与自动检测一致；自动检测会把 `pos` 片识别为正定、把 `sym`/`gen` 分别识别为对称/普通。第 4 问的判断：转置视图不是 F 连续的二维数组，门控 `a1.flags["F_CONTIGUOUS"]` 为假，`overwrite_a` 会被「与」成 `False`，**不会**原地覆写。若环境无 SciPy，相关数值结果为「待本地验证」。

## 6. 本讲小结

- `solve` 的 Python 层只做**校验、编码、委派、汇报**：它把 `assume_a` 字符串查表翻译成整数 `structure`（`None→-1` 表示自动检测），再交给 C++ 后端 `_batched_linalg._solve`。
- `structure` 的取值与 C++ 枚举 `St`（[_common_array_utils.hh:L911-L924](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/src/_common_array_utils.hh#L911-L924)）一一对应；注释 `# keep the numbers in sync with C` 提醒两侧必须同步。
- 自动检测（`assume_a=None`）在后端**逐片**进行：先算带宽判对角/三对角/三角，再判对称/Hermitian，并把实对称或复 Hermitian **乐观先试 Cholesky**，失败再回退。显式指定 `assume_a` 则**跳过检测**——更快，但说错结构会得到**错误答案而非报错**。
- `lower` 仅对 `sym/her/pos` 生效（选上/下三角），`transposed` 解 \(A^{T}x=b\) 但对复矩阵未实现。
- `_format_emit_errors_warnings` 把批量求解的每片状态按「奇异 > LAPACK 内部错误 > 病态」优先级**聚合汇报**：奇异抛 `LinAlgError`、内部错误抛 `ValueError`、病态发 `LinAlgWarning`，是 `solve/inv/lstsq` 共用的错误范式。
- `_datacopied` 用 `.base is None` 判断「是否已拷贝」，让结构化求解族（`solve_triangular/banded/h_banded`）能安全地把已拷贝的输入标记为可覆写；`solve` 主函数则用更严的「二维 + F 连续」门控来约束 `overwrite_*`。

## 7. 下一步学习建议

- **下一讲 u2-l3（矩阵求逆、行列式与最小二乘）**：会复用本讲的 `structure` 编码（`inv` 的字典见 [_basic.py:L1108-L1117](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py#L1108-L1117)）和同一个 `_format_emit_errors_warnings`，建议对照阅读，体会「同一套错误聚合 + 结构编码」如何服务于不同运算。
- **u2-l4（带状/三角/Toeplitz/Circulant 结构化求解器）**：本讲提到的 `solve_triangular/solve_banded/solveh_banded` 会在那里展开，届时你会更清楚 `_datacopied` 在其中的具体作用。
- **u8-l1 / u8-l2（批量线性代数与 C++ 后端）**：本讲的 `err_lst`、逐片检测、`_solve_assume_banded` 等都属批量后端范畴，那里会从 Python 接口到 C++ 实现完整打通。
- **想动手验证**：可以把本讲任意「自动检测」例子里的矩阵改成 `np.asfortranarray(...)`，观察 `solve(..., overwrite_a=True)` 的行为是否如 4.3.3 所述变化。
