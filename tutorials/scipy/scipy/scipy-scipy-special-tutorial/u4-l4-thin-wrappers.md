# 薄包装与装饰器:lambertw 与 spherical_bessel

## 1. 本讲目标

学完本讲后,你应该能够:

- 理解「薄包装(thin wrapper)」这一设计模式:为什么有些 `scipy.special` 函数本身不是 ufunc,却几乎只是转发给一个底层 ufunc。
- 读懂 `_lambertw.py` 中对分支参数 `k` 的类型强制(`dtype="long"`),并说清它为什么必须这么做。
- 读懂 `_spherical_bessel.py` 中 `derivative` 布尔参数如何分派到 `_xxx` 与 `_xxx_d` 两组底层 ufunc。
- 读懂 `use_reflection` 装饰器如何按 DLMF 10.47(v) 处理负实轴反射,并解释 `spherical_jn`(+1)与 `spherical_yn`(-1)的符号差异。
- 解释为什么 `spherical_kn` 不能用简单的实数反射,而改用复数反射 `spherical_kn_reflection`。

## 2. 前置知识

本讲是 U4「纯 Python 包装层」的第四篇。前三篇给出了三种「为何不用 ufunc」的理由:

- u4-l1:`comb`/`factorial` 等——精确结果位数无界,只能用 Python 任意精度整数。
- u4-l2:`jn_zeros`/`jvp` 等——输出长度依赖标量整数参数,而非由广播决定。
- u4-l3:`logsumexp`/`softmax`——需要跨元素聚合,破坏了 ufunc 的「逐元素」前提。

本讲给出第四种,也是最贴近 ufunc 的一种:**函数本身确实逐元素、确实由 ufunc 干活,但需要在调用前后做一点点参数预处理**(强制类型、分支选择、负实轴反射)。这一点点预处理 ufunc 自己做不了,于是用一层极薄的 Python 包装把底层 ufunc 包起来。我们把这种结构称为**薄包装**。

复习两个关键词:

- **ufunc(通用函数)**:NumPy 中按 dtype 分发、逐元素求值、可批量、可广播的 C 级对象(见 u2-l1)。本讲里出现的 `_lambertw`、`_spherical_jn`、`_spherical_jn_d` 等带下划线前缀的名字,都是底层 ufunc,不直接对用户公开。
- **装饰器(decorator)**:Python 语法糖,`@deco` 写在函数定义上方等价于 `fun = deco(fun)`。本讲的 `use_reflection` 是一个**带参数的装饰器工厂**(`use_reflection(+1)` 先返回一个真正的装饰器,再用它装饰函数)。

还要用到一条数学事实——**球贝塞尔函数的负实轴反射公式**(DLMF 10.47(v)):

\[
j_n(-z) = (-1)^n\, j_n(z), \qquad
y_n(-z) = (-1)^{n+1}\, y_n(z)
\]

直觉上:球贝塞尔函数在实轴上有确定的奇偶性,`n` 的奇偶决定了 `f(-z)` 与 `f(z)` 是相等还是反号。这正是 `use_reflection` 要自动处理的事。

## 3. 本讲源码地图

本讲只涉及两个文件,外加两个用于交叉印证的文件:

| 文件 | 角色 |
| --- | --- |
| `scipy/special/_lambertw.py` | 最简单的薄包装:把 `k` 强制成 `long` 后转发给 `_lambertw` ufunc。 |
| `scipy/special/_spherical_bessel.py` | 四个球贝塞尔函数 `spherical_jn/yn/in/kn`,含 `derivative` 分支与 `use_reflection` 装饰器。 |
| `scipy/special/_special_ufuncs.cpp` | 交叉印证:`_lambertw` ufunc 在此用 C++ 直接注册,签名暴露 `k` 必须是整数。 |
| `scipy/special/_ufuncs.pyi` | 交叉印证:类型桩列出 `_spherical_jn` 等八个底层 ufunc 的身份。 |

## 4. 核心概念与源码讲解

### 4.1 薄包装模式:以 lambertw 为例

#### 4.1.1 概念说明

**薄包装**指这样一种结构:用户面对的「公共函数」是一个普通 Python 函数,它做极少量的预处理或后处理,真正的数值计算交给一个底层 ufunc。它的典型动机有:

1. **参数整形**:底层 ufunc 的某个输入必须是特定 dtype(例如整数),但用户更自然地传 Python `int`、`float` 甚至数组,需要先转换。
2. **语义糖**:用更友好的参数名或默认值包装一个签名不那么直观的 ufunc。
3. **多 ufunc 分派**:根据某个开关(如 `derivative`)在两个底层 ufunc 之间二选一。
4. **结果改写**:对负实轴、奇异点等做反射或修正(见 4.3)。

`lambertw` 属于第 1 类。Lambert W 函数 `W(z)` 是 `w*exp(w)` 的反函数,有多条分支,用整数 `k` 索引。用户调用 `lambertw(z, k=0, tol=1e-8)`,而底层 ufunc `_lambertw(z, k, tol)` 要求 `k` 是整数类型。于是包装层只做一件事:把 `k` 转成 `long` 整数,再转发。

#### 4.1.2 核心流程

```text
用户: lambertw(z, k=0, tol=1e-8)
   │
   ├── k = np.asarray(k, dtype="long")   # 强制成 long 整数(可标量、可数组)
   │
   └── return _lambertw(z, k, tol)        # 转发给底层 ufunc,其余交给广播/逐元素
```

要点:

- 预处理只针对 `k`;`z` 和 `tol` 原样透传,它们的类型由 ufunc 自己的多类型分发处理(`z` 可以是实数或复数)。
- `k` 被转成数组后并不丢失标量语义:NumPy 的 `np.asarray(0, dtype="long")` 得到一个 0 维数组,ufunc 仍能正常广播。

#### 4.1.3 源码精读

整个 `_lambertw.py` 的实现部分只有两行(前面大段都是文档字符串):

[_lambertw.py:L146-L149](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_lambertw.py#L146-L149) — 这里先用 `np.asarray(k, dtype=np.dtype("long"))` 把分支索引 `k` 强制成 `long` 整数,再调用底层 ufunc `_lambertw(z, k, tol)`。注释里那句 `TODO: special expert should inspect this interception` 表明维护者自己也还在斟酌「在这里拦截 `k` 是不是最佳位置」。

为什么必须是 `"long"`?看底层注册就一目了然。`lambertw` 并不在 `functions.json` 里(回顾 u3-l4:它走的是 `_special_ufuncs.cpp` 的「直接 C++ 注册」路径):

[_special_ufuncs.cpp:L344-L347](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_special_ufuncs.cpp#L344-L347) — `_lambertw` 用 `xsf::numpy::ufunc(...)` 注册了两个 loop:`Dld_D` 与 `Flf_F`。

这两个类型串(回顾 u2-l1、u3-l1 的类型码)解读如下:

| loop 串 | 输入类型 | 输出类型 | 含义 |
| --- | --- | --- | --- |
| `Dld_D` | `D`(double 复数), `l`(long 整数), `d`(double) | `D`(double 复数) | 双精度复数版 |
| `Flf_F` | `F`(float 复数), `l`(long 整数), `f`(float) | `F`(float 复数) | 单精度复数版 |

第二个输入位永远是 `l`(long 整数)——这就是分支索引 `k` 的类型。底层**没有**为浮点 `k` 注册任何 loop。如果包装层不做强制,用户传 `k=0`(Python `int` 还好)或更糟的 `k=0.0`、`k=np.array([0.0,1.0])`(浮点数组)时,ufunc 的类型分发会找不到匹配 loop 而报错或得到错误结果。所以 `np.asarray(k, dtype="long")` 是一道**类型护栏**。

#### 4.1.4 代码实践

1. **实践目标**:验证 `lambertw` 是薄包装,并观察去掉 `k` 的类型强制会发生什么。
2. **操作步骤**:

   ```python
   import numpy as np
   from scipy.special import lambertw
   from scipy.special._ufuncs import _lambertw   # 直接拿到底层 ufunc

   # (a) 确认公共函数返回值满足定义 w*exp(w)==z
   w = lambertw(1.0)
   print(w, w*np.exp(w))            # 应得到 1.0

   # (b) 让 k 是浮点数组,直接喂给底层 ufunc
   z = np.array([1.0, 2.0])
   k_float = np.array([0.0, 1.0])   # 故意用 float
   try:
       print(_lambertw(z, k_float, 1e-8))
   except Exception as e:
       print("直接喂浮点 k 报错:", type(e).__name__, e)

   # (c) 对照:把 k 强制成 long 后就没问题
   k_long = k_float.astype("long")
   print(_lambertw(z, k_long, 1e-8))
   ```

3. **需要观察的现象**:步骤 (b) 大概率抛出 `UFuncTypeError`(没有合适的 loop);步骤 (c) 正常返回。
4. **预期结果**:印证了「`k` 必须是 long 整数,包装层正是为此而存在」。
5. 本实践的具体异常类型与文案可能随 NumPy 版本变化,若未抛异常也应看到结果不可用——**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**:为什么 `lambertw` 的文档把 `tol` 列为参数,而底层 `_lambertw` 的 loop 串里 `tol` 对应的是 `d`/`f`(浮点)而不是整数?

**参考答案**:`tol` 是 Halley 迭代的收敛容差,本就是浮点数;只有 `k`(分支索引)是整数语义。这正说明「类型强制只针对 `k`」是合理且最小化的。

**练习 2**:`lambertw(1)` 返回的是 `(0.567...+0j)`,即一个复数。既然输入 `1` 是实数,为什么输出是复数?

**参考答案**:底层 loop 是 `Dld_D`/`Flf_F`,输入输出都是**复数**类型。ufunc 在「实数输入→复数 loop」时会先把实数提升为复数再计算,故返回复数。这是底层实现的选择(便于统一处理多分支),薄包装层并不改变它。

---

### 4.2 derivative 分支:在两个底层 ufunc 之间二选一

#### 4.2.1 概念说明

`_spherical_bessel.py` 定义了四个用户函数:`spherical_jn`、`spherical_yn`、`spherical_in`、`spherical_kn`(球贝塞尔函数的第一、二类,及变形球贝塞尔函数的第一、二类)。它们都有一个布尔参数 `derivative`:

- `derivative=False`(默认):返回函数值本身。
- `derivative=True`:返回函数的导数。

底层为「函数值」和「导数」分别实现了**两个独立的 ufunc**:例如 `_spherical_jn`(函数值)与 `_spherical_jn_d`(导数)。于是这里的薄包装职责很清晰:读取 `derivative` 开关,在两个底层 ufunc 之间二选一。

注意这八个底层 ufunc 的存在可以直接从类型桩看出:

[_ufuncs.pyi:L280-L287](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_ufuncs.pyi#L280-L287) — 这里集中列出 `_spherical_in/_spherical_in_d`、`_spherical_jn/_spherical_jn_d`、`_spherical_kn/_spherical_kn_d`、`_spherical_yn/_spherical_yn_d` 共八个 ufunc,每对都是「函数值 + 导数」。

#### 4.2.2 核心流程

以 `spherical_jn` 为例,函数体的核心是:

```text
用户: spherical_jn(n, z, derivative=False)
   │
   ├── n = np.asarray(n, dtype="long")   # 阶数 n 也强制成 long 整数
   │
   ├── if derivative:  return _spherical_jn_d(n, z)   # 导数 ufunc
   └── else:           return _spherical_jn(n, z)     # 函数值 ufunc
```

两个细节:

- **`n` 同样被强制成 `long`**:和 `lambertw` 的 `k` 一个道理——阶数 `n` 是整数语义,底层 ufunc 的 `n` 输入位是整数类型。这是薄包装的「参数整形」职责在球贝塞尔函数上的再次体现。
- **分派是互斥的**:每次调用只走一个底层 ufunc,不会同时计算函数值与导数。

#### 4.2.3 源码精读

[_spherical_bessel.py:L123-L127](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_spherical_bessel.py#L123-L127) — `spherical_jn` 的实现体:先把 `n` 转成 `long` 数组,再用 `if derivative` 在 `_spherical_jn_d` 与 `_spherical_jn` 之间二选一。

其余三个函数(`spherical_yn`/`spherical_in`/`spherical_kn`)的实现体结构完全同构,只是换成了各自的底层 ufunc 名:

- `spherical_yn`:[_spherical_bessel.py:L214-L218](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_spherical_bessel.py#L214-L218) 分派到 `_spherical_yn(_d)`。
- `spherical_in`:[_spherical_bessel.py:L304-L308](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_spherical_bessel.py#L304-L308) 分派到 `_spherical_in(_d)`。
- `spherical_kn`:[_spherical_bessel.py:L401-L405](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_spherical_bessel.py#L401-L405) 分派到 `_spherical_kn(_d)`。

这套「`derivative` 选 `_xxx` vs `_xxx_d`」的分派,与 `n` 的整数强制,共同构成了球贝塞尔薄包装的**第二、第三层职责**(第一层是 4.3 的反射装饰器)。

#### 4.2.4 代码实践

1. **实践目标**:用解析递推关系验证 `derivative=True` 确实走了 `_spherical_jn_d` 这条导数分支,且结果正确。
2. **操作步骤**(直接取自 `spherical_jn` 的官方文档示例):

   ```python
   import numpy as np
   from scipy.special import spherical_jn

   x = np.arange(1.0, 2.0, 0.01)
   # 导数分支(走 _spherical_jn_d)
   lhs = spherical_jn(3, x, derivative=True)
   # 用解析关系 j_n'(z) = j_{n-1}(z) - (n+1)/z * j_n(z),n=3
   rhs = spherical_jn(2, x) - 4.0/x * spherical_jn(3, x)
   print(np.allclose(lhs, rhs))   # 应为 True
   ```

3. **需要观察的现象**:`allclose` 返回 `True`,说明 `derivative=True` 给出的导数与 DLMF 10.51.E2 的解析关系一致。
4. **预期结果**:打印 `True`。这正是导数分支 `_spherical_jn_d` 与函数值分支 `_spherical_jn` 各自正确、又满足递推关系的证据。

#### 4.2.5 小练习与答案

**练习 1**:如果把 `derivative` 设计成底层 ufunc 的一个布尔输入位(像 `n` 那样),而不是在 Python 层分派,会有什么坏处?

**参考答案**:那样每个 `spherical_*` 只需要一个 ufunc,但内层循环要在每次逐元素求值时判断 `derivative` 分支,增加分支开销;且导数与函数值的数值实现可能差异较大,硬塞进同一个 loop 不一定干净。拆成 `_xxx`/`_xxx_d` 两个 ufunc、在 Python 层二选一,既保持每个 ufunc 内层循环简单,也让类型签名更清晰。

**练习 2**:`spherical_jn(3, x, derivative=True)` 与 `spherical_jn(3, x)` 调用的是同一个 ufunc 吗?

**参考答案**:不是。前者调用 `_spherical_jn_d`,后者调用 `_spherical_jn`,是两个不同的底层 ufunc。

---

### 4.3 use_reflection 反射装饰器:负实轴的符号处理

#### 4.3.1 概念说明

球贝塞尔函数在负实轴上的行为由 DLMF 10.47(v) 的反射公式刻画。简言之,对实数 `z`,有:

\[
j_n(-z) = (-1)^n\, j_n(z), \qquad
y_n(-z) = (-1)^{n+1}\, y_n(z)
\]

也就是说,只要能算正实轴上的值,负实轴的值就能用一个**符号因子**推出来。然而底层 ufunc `_spherical_jn` 等是为「实 `z ≥ 0` 或复 `z`」准备的,直接喂负实数未必给出正确结果(曾引发 gh-14582 的 bug)。

`use_reflection` 装饰器统一解决这件事:在调用真正的函数之前,先看 `z` 的实部符号——

- 复数 `z`:直接放行,复数路径「天然正确」;
- 实数 `z ≥ 0`:直接放行;
- 实数 `z < 0`:走反射分支,用 `f(-z)` 乘以符号因子还原 `f(z)`。

之所以把这段逻辑写成**装饰器**(而不是复制到四个函数里),正是因为四个函数的反射逻辑高度同构、只差一个符号常数。装饰器把「不变的流程」与「每个函数专属的符号」解耦。

#### 4.3.2 核心流程

`use_reflection` 是一个**带参数的装饰器工厂**,有两个互斥的配置项:

- `sign_n_even`:指定「当阶数 `n` 为偶数时」反射的符号(`+1` 或 `-1`),走「实数标准反射」。
- `reflection_fun`:指定一个自定义反射函数,用于反射公式更复杂的场合(目前仅 `spherical_kn` 用到)。

被装饰函数(记为 `fun`,即真正的 `spherical_jn` 等)执行时,实际调用的是装饰器返回的 `wrapper`:

```text
wrapper(n, z, derivative=False):
    z = np.asarray(z)
    if z 是复数类型:
        return fun(n, z, derivative)                 # 复数路径天然正确
    # 实数路径,按 z.real 的符号分流:
    apply_where(z.real >= 0, (n, z),
        true_fn  = (n,z) -> fun(n, z, derivative),    # 正实轴直接算
        false_fn = (n,z) -> reflection(n, z, derivative))  # 负实轴走反射
```

其中「实数标准反射」`standard_reflection(n, z, derivative)`(此时 `z<0`)的符号计算是:

\[
\text{sign} = \begin{cases} \text{sign\_n\_even}, & n \text{ 偶} \\ -\text{sign\_n\_even}, & n \text{ 奇} \end{cases}
\]

再由链式法则:对 `f(-z)` 求导会多出一个负号,故

\[
\text{sign} \leftarrow -\text{sign} \quad (\text{当 } \text{derivative}=\text{True})
\]

最终 `standard_reflection` 返回 `fun(n, -z, derivative) * sign`(注意 `-z>0`,落在底层 ufunc 的安全区)。

把四个函数的配置对照一下,符号差异的来源就一目了然:

| 函数 | 装饰器配置 | `sign_n_even` | 反射关系 | 含义 |
| --- | --- | --- | --- | --- |
| `spherical_jn` | `@use_reflection(+1)` | `+1` | \(j_n(-z)=(-1)^n j_n(z)\) | 偶阶 `+`,奇阶 `-` |
| `spherical_yn` | `@use_reflection(-1)` | `-1` | \(y_n(-z)=(-1)^{n+1} y_n(z)\) | 偶阶 `-`,奇阶 `+` |
| `spherical_in` | `@use_reflection(+1)` | `+1` | \(i_n(-z)=(-1)^n i_n(z)\) | 同 `jn` |
| `spherical_kn` | `@use_reflection(reflection_fun=...)` | — | 复数反射 | 见 4.3.5 |

`jn` 与 `yn` 的符号差异,根源就是数学上 \(j_n\) 的反射带 \((-1)^n\),而 \(y_n\) 带 \((-1)^{n+1}\),两者正好差一个负号——这映射到代码里就是 `sign_n_even` 从 `+1` 变 `-1`。

#### 4.3.3 源码精读

先看装饰器本体与「标准反射」:

[_spherical_bessel.py:L9-L35](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_spherical_bessel.py#L9-L35) — `use_reflection(sign_n_even, reflection_fun)` 返回 `decorator`,`decorator` 内定义了 `standard_reflection`(按 `n` 奇偶与 `derivative` 计算符号,调用 `fun(n, -z, derivative) * sign`)和 `wrapper`(按 `z` 是否复数、实部正负分流)。注释明确引用 DLMF 10.47(v)。

几个关键点逐条对应到代码:

- [第 17 行](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_spherical_bessel.py#L17):`sign = np.where(n % 2 == 0, sign_n_even, -sign_n_even)` —— `n` 偶取 `sign_n_even`,`n` 奇取其相反数。
- [第 19 行](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_spherical_bessel.py#L19):`sign = -sign if derivative else sign` —— 链式法则给导数补一个负号。
- [第 21 行](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_spherical_bessel.py#L21):`return fun(n, -z, derivative) * sign` —— 在 `-z`(即正实轴)上求值再乘符号。
- [第 27-28 行](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_spherical_bessel.py#L27-L28):复数 `z` 直接 `return fun(n, z, derivative)`,注释 `complex dtype just works`。
- [第 30-33 行](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_spherical_bessel.py#L30-L33):用 `xpx.apply_where(z.real >= 0, ...)` 按实部正负分流,末尾 `[()]` 把 0 维数组还原成标量(保住「标量输入→标量输出」的 ufunc 风格)。

再看四个函数如何挂上不同的配置:

- `spherical_jn`:[第 38 行](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_spherical_bessel.py#L38) `@use_reflection(+1)`。
- `spherical_yn`:[第 130 行](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_spherical_bessel.py#L130) `@use_reflection(-1)`。
- `spherical_in`:[第 221 行](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_spherical_bessel.py#L221) `@use_reflection(+1)`。
- `spherical_kn`:[第 318 行](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_spherical_bessel.py#L318) `@use_reflection(reflection_fun=spherical_kn_reflection)`。

测试侧,`test_negative_real_gh14582` 正是这条反射逻辑的回归测试,它把实数 `z` 的结果与「`z+0j` 复数求值后取实部」逐一比对:

[test_spherical_bessel.py:L395-L407](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/tests/test_spherical_bessel.py#L395-L407) — 对四个函数 × {`derivative` 真/假} 参数化,断言 `fun(n, z)` ≈ `fun(n, z+0j).real`。这等于声明:「实数反射的结果应当与复数路径的实部一致」。

#### 4.3.4 代码实践(本讲指定实践)

1. **实践目标**:解释 `spherical_jn`(+1)与 `spherical_yn`(-1)的符号差异来源;验证 `spherical_jn(3, -x)` 与反射公式一致;说明 `spherical_kn` 为何改用复数反射。
2. **操作步骤**:

   ```python
   import numpy as np
   from scipy.special import spherical_jn, spherical_yn, spherical_kn

   # (a) 验证 jn 的反射:j_n(-x) ?= (-1)^n * j_n(x),取 n=3(奇)
   x = np.linspace(0.5, 5.0, 20)
   lhs = spherical_jn(3, -x)
   rhs = (-1)**3 * spherical_jn(3, x)        # = -j_3(x)
   print("jn 反射一致:", np.allclose(lhs, rhs))   # 预期 True

   # (b) 验证 yn 的反射:y_n(-x) ?= (-1)^(n+1) * y_n(x),取 n=3(奇)
   lhs_y = spherical_yn(3, -x)
   rhs_y = (-1)**(3+1) * spherical_yn(3, x)  # = +y_3(x)
   print("yn 反射一致:", np.allclose(lhs_y, rhs_y)) # 预期 True

   # (c) 对比 jn / yn 在同一 n 下的符号差异
   #     jn: sign_n_even=+1  -> 偶阶 +, 奇阶 -
   #     yn: sign_n_even=-1  -> 偶阶 -, 奇阶 +
   n = 3
   print("jn 奇阶符号 :", np.sign(spherical_jn(n, -1.0) / spherical_jn(n, 1.0)))  # -1
   print("yn 奇阶符号 :", np.sign(spherical_yn(n, -1.0) / spherical_yn(n, 1.0)))  # +1
   ```

3. **需要观察的现象**:(a)、(b) 都打印 `True`; 中 jn 的比值为 `-1`、yn 的比值为 `+1`,正好对应 `sign_n_even` 从 `+1` 变 `-1`。
4. **预期结果**:印证反射公式与装饰器配置一致——jn 奇阶反号、yn 奇阶同号,二者符号恰好相反,根源就是 `(-1)^n` 与 `(-1)^{n+1}` 差一个负号。

#### 4.3.5 spherical_kn 为何改用复数反射

`spherical_kn` 没有用「实数标准反射 + 一个符号常数」,而是传了 `reflection_fun=spherical_kn_reflection`。看它的实现:

[_spherical_bessel.py:L311-L315](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_spherical_bessel.py#L311-L315) — `spherical_kn_reflection` 直接把 `z` 提升为复数 `z + 0j` 调用 `spherical_kn`,再取实部。注释解释:变形第二类球贝塞尔函数 \(k_n\) 的反射公式比其余三者复杂得多(其定义涉及支割线,负实轴上有虚部),实数符号反射不适用。

原因剖析:

- \(j_n, y_n, i_n\) 在实轴上有干净的奇偶性,反射是「同一个函数值乘 ±1」。
- \(k_n(z)\)(即 \(\sqrt{\pi/(2z)}\,K_{n+1/2}(z)\))依赖的 \(K_\nu(z)\) 在负实轴上有支割线,沿负实轴的值带有虚部,简单的 `±f(|z|)` 无法还原。最稳妥、也最简单的办法是直接在复数域算 `f(z+0j)` 再取实部——这正是 `spherical_kn_reflection` 的做法。代码注释里也说,这条路径未来很可能在 C++ 里重写。

这与 4.3.3 的回归测试呼应:`test_negative_real_gh14582` 对四个函数一视同仁地断言 `fun(n,z) ≈ fun(n, z+0j).real`——对 `kn` 而言,这个等式之所以成立,正是因为它在反射分支里**真的就是**这么算的。

#### 4.3.6 小练习与答案

**练习 1**:为什么 `standard_reflection` 在 `derivative=True` 时要给 `sign` 再乘一个 `-1`?

**参考答案**:反射把 `z<0` 的问题改写成 `f(z) = sign * f(-z)`。两边对 `z` 求导,右边链式法则带出一个对 `(-z)` 求导的负号:`f'(z) = -sign * f'(-z)`。所以求导分支的符号因子比函数值分支多一个负号。

**练习 2**:如果有人把 `spherical_yn` 上的 `@use_reflection(-1)` 误改成 `@use_reflection(+1)`,哪个测试最可能抓住这个回归?

**参考答案**:`test_negative_real_gh14582`。它会比较 `spherical_yn(n, z)` 与 `spherical_yn(n, z+0j).real`,在负实轴上符号错了会直接 `assert_allclose` 失败。这也说明该测试是反射逻辑的关键护栏。

**练习 3**:`wrapper` 末尾的 `[()]` 起什么作用?去掉它会怎样?

**参考答案**:`xpx.apply_where` 返回一个 ndarray;当输入是标量(0 维)时,`[()]` 把 0 维数组解包成 NumPy 标量,使「标量进→标量出」与底层 ufunc 的行为保持一致。去掉它,标量输入会得到 0 维数组,虽然值正确,但改变了返回类型,可能破坏依赖标量返回的下游代码。

---

## 5. 综合实践

把本讲三层知识串起来,完成一个「自定义薄包装 + 反射装饰器」的小任务。

**任务**:模仿 `_spherical_bessel.py` 的风格,为假想的「简化球贝塞尔第一类」写一个带反射的薄包装,并验证它在负实轴上的行为。

1. **目标**:理解「薄包装 = 参数整形 + ufunc 转发 + 可选装饰器」的整体结构。
2. **步骤**:

   ```python
   import numpy as np
   import scipy._external.array_api_extra as xpx
   from scipy.special import spherical_jn   # 借用现成实现当 "底层 ufunc"

   # 1) 写一个带参数的反射装饰器(简化版,只处理 sign_n_even)
   def my_reflection(sign_n_even):
       def decorator(fun):
         def standard_reflection(n, z, derivative):
             sign = np.where(n % 2 == 0, sign_n_even, -sign_n_even)
             sign = -sign if derivative else sign
             return fun(n, -z, derivative) * sign
         def wrapper(n, z, derivative=False):
             z = np.asarray(z)
             if np.issubdtype(z.dtype, np.complexfloating):
                 return fun(n, z, derivative)
             return xpx.apply_where(z.real >= 0, (n, z),
                 lambda n, z: fun(n, z, derivative),
                 lambda n, z: standard_reflection(n, z, derivative))[()]
         return wrapper
       return decorator

   # 2) 用它包一层 spherical_jn,验证与原函数完全等价
   @my_reflection(+1)
   def my_jn(n, z, derivative=False):
       n = np.asarray(n, dtype="long")
       return spherical_jn(n, z, derivative)   # 这里相当于 "转发底层 ufunc"

   # 3) 在正、负实轴上对照
   x = np.linspace(-5, 5, 50)
   n = 3
   print("正负实轴等价:", np.allclose(my_jn(n, x), spherical_jn(n, x)))
   print("导数分支等价:", np.allclose(my_jn(n, x, True), spherical_jn(n, x, True)))
   ```

3. **观察与预期**:两条 `allclose` 都应为 `True`——说明你自己实现的反射装饰器与库内置的 `use_reflection(+1)` 行为一致,且 `derivative` 分支也被正确透传。
4. **反思题**:如果把 `@my_reflection(+1)` 改成 `@my_reflection(-1)`,`my_jn` 在负实轴上会怎样偏离真值?(答:奇阶 `n` 的符号会反掉,偏离一个 `-1` 因子。)

## 6. 本讲小结

- **薄包装**是第四种「为何用纯 Python」的模式:函数本身逐元素、由底层 ufunc 干活,只需在调用前后做一点点参数预处理。
- `lambertw` 的全部职责是把分支索引 `k` 强制成 `long` 整数,因为底层 `_lambertw` 的两个 loop(`Dld_D`/`Flf_F`)第二个输入位固定是 `l`。
- `spherical_*` 的 `derivative` 布尔参数在 `_xxx`(函数值)与 `_xxx_d`(导数)两个底层 ufunc 之间二选一;同时阶数 `n` 也被强制成 `long`。
- `use_reflection` 是带参数的装饰器工厂,按 DLMF 10.47(v) 处理负实轴:复数放行、正实轴直接算、负实轴用 `f(-z)*sign` 还原。
- `spherical_jn`(+1)与 `spherical_yn`(-1)的符号差异,根源于 \(j_n(-z)=(-1)^n j_n(z)\) 与 \(y_n(-z)=(-1)^{n+1} y_n(z)\) 恰好差一个负号;导数分支再多一个链式法则带来的负号。
- `spherical_kn` 因 \(K_\nu\) 在负实轴有支割线,改用复数反射 `spherical_kn_reflection`(在 `z+0j` 上求值取实部);`test_negative_real_gh14582` 是这套反射逻辑的共同回归护栏。

## 7. 下一步学习建议

- **横向对比**:`_basic.py` 里的 `jvp/yvp` 等「普通贝塞尔导数函数」(u4-l2)与本讲的球贝塞尔 `derivative` 分支对照阅读,体会「解析递推 vs 双 ufunc 分派」两种导数实现策略。
- **纵向下钻**:本讲的底层 ufunc(`_lambertw`、`_spherical_jn` 等)从哪来?建议进入 U6 `cython_special`,看同一批内核如何以「标量 + 指针」签名暴露给 Cython 用户;再到 U8 看 `_special_ufuncs.cpp` 中 `xsf::numpy::ufunc` 的直接 C++ 注册路径。
- **装饰器与 Array API**:`use_reflection` 用到的 `xpx.apply_where` 是 `scipy._external.array_api_extra` 的一部分。学完 U10 的 Array API 多后端支持后,可以回头体会「为什么反射逻辑要用 `apply_where` 而不是 `np.where`」——前者能更好地兼容非 NumPy 后端的逐元素分流。
