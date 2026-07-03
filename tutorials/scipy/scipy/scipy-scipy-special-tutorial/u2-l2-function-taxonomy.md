# 函数分类总览：从 `_ufuncs.pyi` 与文档读懂 250+ 函数

## 1. 本讲目标

u2-l1 已经讲清楚一件事：`scipy.special` 里**几乎所有函数都是 ufunc**，它们共享类型分发、广播、`out=`/`where=` 等机制。但这只回答了「这些函数**怎么算**」。本讲要回答另一个同样重要的问题：**这 250 多个函数到底分成哪几类、各自管什么、我该怎么找到我要的那一个？**

面对 250+ 个名字（`airy`、`elliprf`、`hyp2f1`、`bdtr`、`gammainccinv`、`eval_chebyt`……），初学者最容易迷路。本讲把 `scipy.special` 当成一家「数学函数超市」，带你读懂它的**货架布局**。

学完本讲，你应该能够：

- 说出 `special` 的**约 20 个函数家族**及其代表性函数，并能把一个数学问题（求积分、求分布分位数、求多项式值……）对号入座到某个家族。
- 理解「**Raw statistical functions（原始统计函数）**」这一大类的特殊性：它们本质是概率分布的 CDF / 生存函数 / 分位数，以逐元素 ufunc 形式暴露；并能讲清它们与 `scipy.stats`「友好版本」的分工。
- 掌握一套**定位流程**：拿到一个需求，如何用模块文档字符串（按章节组织的人类目录）配合 `_ufuncs.pyi` 的 `__all__`（机器可见的 ufunc 清单）快速锁定正确的函数。

> 一句话定位：`special` 的函数不是一盘散沙，而是**按数学家族分章节陈列**的；读懂章节布局 + 一套统计函数的命名约定，你就能在 250+ 个函数里 10 秒内找到目标。

## 2. 前置知识

- **特殊函数（special function）**：在数学物理、概率统计、数论里反复出现的「有名」函数，如 Gamma 函数 \(\Gamma(x)\)、误差函数 \(\mathrm{erf}(x)\)、Bessel 函数 \(J_\nu(x)\)、超几何函数 \({}_2F_1\)。它们大多没有初等闭式，需要专门的数值算法。`scipy.special` 就是把这些算法集中起来。
- **ufunc 与逐元素**（承接 u2-l1）：绝大多数 `special` 函数是 NumPy ufunc，即「按类型分发、逐元素求值、可批量」。本讲把它当作既定事实，不再解释机制，只关注「**这些 ufunc 怎么分类**」。
- **命名空间的拼装**（承接 u1-l4）：`scipy.special` 这个统一货架是由 `_ufuncs`、`_basic`、`_orthogonal`、`_multiufuncs`、`_logsumexp` 等多个子模块拼出来的，`__all__` 决定哪些名字对外公开。本讲会用这个事实来解释「**为什么有些函数在 `_ufuncs.pyi` 里找不到**」。
- **CDF / 生存函数 / 分位数**：对概率分布而言，累积分布函数 CDF \(F(x)=P(X\le x)\)；生存函数 SF \(=1-F(x)\)；分位数函数是 CDF 的反函数（给定概率求阈值）。这是读懂 4.2 统计家族命名约定的基础。

> 名词速查：**autosummary** 是 Sphinx 文档的一个指令，`__init__.py` 文档字符串里大量出现的 `.. autosummary::` 块，就是「这一节列出的函数清单」，渲染后变成官方文档里可点击的函数表。它正是我们绘制分类地图的原材料。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲怎么用它 |
|------|------|--------------|
| [`__init__.py`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/__init__.py) | 包入口。前 783 行是按**函数家族分章节**的文档字符串，是分类地图的唯一权威来源 | 4.1 提取全部家族章节；4.2 读「Raw statistical functions」的 `seealso`；4.3 读「非 ufunc」警告段落 |
| [`_ufuncs.pyi`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_ufuncs.pyi) | `_ufuncs` 扩展模块的**类型桩**；`__all__` 列出该模块导出的全部名字 | 4.2 / 4.3 作为「机器可见的 ufunc 清单」，与文档章节做对照、做差集 |

> 说明：本讲**只读这两个文件**。函数具体怎么实现、调用哪个 C/C++ 后端，属于 u3（代码生成）与 u8（C++ 后端）单元的内容，本讲不展开。

## 4. 核心概念与源码讲解

### 4.1 函数家族分类：`special` 的「货架」是怎么摆的

#### 4.1.1 概念说明

如果你把 `special` 的 250+ 函数按字母排，会得到一堵毫无结构的墙。但模块作者没有这么做——他们把函数按**数学家族**分章节陈列，每个家族对应一类数学对象或一类应用场景。这就是 `__init__.py` 顶部那段超长文档字符串的真正用途：它不是说明书，而是**一张分类地图**。

理解这张地图的好处是：你不需要记住 250 个名字，只需要记住「我的问题属于哪个家族」，然后在那个家族的十几个函数里挑一个。例如：

- 「我要算一个定积分 \(\int_0^x e^{-t^2}\,dt\)」→ 这是**误差函数族**，找 `erf`。
- 「我要算 \(\Gamma(0.5)\)」→ 这是 **Gamma 相关族**，找 `gamma`。
- 「我要算柱坐标下波动方程的解」→ 这是 **Bessel 函数族**，找 `jv`/`yv`。

#### 4.1.2 核心流程

文档字符串把函数分成约 20 个一级家族（章节标题下用一行 `----` 标记）。下面按「数学亲缘关系」把它们归并成 6 个大组，方便记忆：

| 大组 | 家族（章节） | 代表函数 | 典型用途 |
|------|--------------|----------|----------|
| **微分方程解** | Airy / Bessel / 球 Bessel / Struve / Kelvin / 抛物柱面 / Mathieu / 旋转椭球波 | `airy`、`jv`、`spherical_jn`、`struve`、`kelvin`、`pbdv` | 数学物理方程的级数/渐近解 |
| **特殊积分** | 椭圆函数与积分 / 误差函数与 Fresnel / Gamma 相关 / 超几何 | `elliprf`、`erf`、`fresnel`、`gamma`、`betainc`、`hyp2f1` | 无法用初等函数表达的定积分与特殊值 |
| **多项式族** | Legendre / 正交多项式 / 椭球调和 | `legendre_p`、`sph_harm_y`、`eval_legendre`、`roots_legendre` | 求积、展开、球面调和 |
| **概率统计**（原始统计函数） | 二项/Beta/F/Gamma/负二项/非中心 F-t/正态/Poisson/Student-t/卡方/非中心卡方/Kolmogorov 分布、Box-Cox、信息论、Sigmoidal | `bdtr`、`ndtr`、`fdtri`、`entr`、`expit` | 分布的 CDF/分位数、熵、损失 |
| **组合 / 数论 / 其他** | 组合数学 / Lambert W / 其他 | `comb`、`factorial`、`lambertw`、`zeta`、`agm`、`binom` | 阶乘组合、特殊方程的根 |
| **便利函数** | 便利函数 | `logsumexp`、`log1p`、`expm1`、`exprel`、`sinc`、`xlogy` | 数值稳定的小工具 |

如何使用这张表？三步：

1. 把你的数学问题**翻译成家族关键词**（「这是某个分布的分位数吗？」「这是某个微分方程的解吗？」）。
2. 在上表锁定大组与家族。
3. 进 [`__init__.py`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/__init__.py) 对应章节，看 `autosummary` 里每个函数的一句话简介，挑出你要的。

#### 4.1.3 源码精读

文档的开篇契约先定下基调——**默认是 ufunc，例外才警告**：

> [`__init__.py:13-19`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/__init__.py#L13-L19)：说明「下面几乎所有函数都接受 NumPy 数组、遵循广播规则、本质是 ufunc；不接受数组的函数会在所属小节用警告标出」。这句话是整个分类地图的阅读规则。

随后就是一连串「章节标题 + `autosummary` 清单」的陈列。看第一个家族 **Airy 函数**：

> [`__init__.py:48-58`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/__init__.py#L48-L58)：标题 `Airy functions` 下用 `----` 标记为一级章节，`autosummary` 列出 `airy`、`airye`、`ai_zeros`、`bi_zeros`、`itairy`，每个后面跟一句话功能描述。这就是一个家族的完整「货架标签」。

再看 **Gamma 相关**家族，它是典型的「一个数学对象衍生出一整组函数」：

> [`__init__.py:413-442`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/__init__.py#L413-L442)：一个家族里同时有 `gamma`（Gamma 函数 \(\Gamma(x)=\int_0^\infty t^{x-1}e^{-t}\,dt\)）、`gammaln`（其对数，数值更稳）、`gammainc`/`gammaincc`（正则化下/上不完全 Gamma）、`gammaincinv`/`gammainccinv`（它们的反函数）、`beta`/`betaln`/`betainc`（Beta 及不完全 Beta）、`psi`/`digamma`/`polygamma`（ digamma 及高阶）、`poch`（升阶乘）。注意 `psi` 与 `digamma` 是**同一个函数的两个名字**（`digamma` 是 `psi` 的别名）。

观察一个细节：章节层级用下划线符号区分——`====` 标记文档总标题，`----` 标记**一级家族**，`^^^^` 标记家族下的**子类**。例如 Bessel 家族下就有「零点」「快速版本」「积分」「导数」「球 Bessel」「Riccati-Bessel」等若干 `^^^^` 子节。读文档时，先扫 `----` 找家族，再扫 `^^^^` 缩小范围。

#### 4.1.4 代码实践

**实践目标**：亲手验证「分类地图」确实存在，并统计每个家族的函数数量。

**操作步骤**：

```python
# taxonomy_walk.py —— 示例代码
import re
import scipy.special as sc

# 1) 读 __init__.py 的文档字符串
doc = sc.__doc__

# 2) 用正则把"一级章节标题"抓出来（标题下一行全是 '-' 的那行）
lines = doc.splitlines()
sections = []
for i, line in enumerate(lines):
    nxt = lines[i + 1] if i + 1 < len(lines) else ""
    # 一级家族：下一行是 >=4 个连字符、且本行非空
    if line.strip() and set(nxt.strip()) == {"-"} and len(nxt.strip()) >= 4:
        sections.append(line.strip())

print("一级章节数（家族数）:", len(sections))
for s in sections:
    print("  -", s)
```

**需要观察的现象**：脚本会打印出约 20 个一级章节名，应包含 `Airy functions`、`Elliptic functions and integrals`、`Bessel functions`、`Raw statistical functions`、`Gamma and related functions`、`Error function and Fresnel integrals`、`Orthogonal polynomials`、`Hypergeometric functions`、`Combinatorics`、`Convenience functions` 等，与本节表格一致。

**预期结果（参考值）**：一级章节数约为 21（含 `Error handling` 与 `Available functions` 两个非函数家族的元章节；纯函数家族约 19~20 个）。具体数字以你本地的实际输出为准——**待本地验证**。

> 注意：我没有在你的环境里运行这段脚本（沙箱不允许执行 Python）。上面的章节名是我直接读 [`__init__.py`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/__init__.py) 文档字符串得出的；数量请你运行后核对。

#### 4.1.5 小练习与答案

**练习 1**：`voigt_profile`（光谱线型）属于哪个家族？为什么它和 `erf`、`wofz` 放在一起？

> **答案**：属于「Error function and Fresnel integrals」家族（见 [`__init__.py:443-461`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/__init__.py#L443-L461)）。因为 Voigt 线型是高斯与洛伦兹线型的卷积，在数学上由复误差函数（Faddeeva 函数 `wofz`）的实部给出，与 `erf`/`wofz` 共享同一族底层算法。

**练习 2**：`eval_legendre`、`roots_legendre`、`legendre`（一个 `orthopoly1d` 对象）三者都属于「正交多项式」家族，但来源不同。请说出它们各自解决什么问题。

> **答案**：`eval_legendre` 是**逐元素求值**某阶 Legendre 多项式的 ufunc；`roots_legendre` 返回**高斯求积的节点与权重**（不是 ufunc，来自 `_orthogonal.py`）；`legendre` 返回**多项式系数对象** `orthopoly1d`，适合低阶代数运算。三者对应「求值 / 求积 / 系数」三种不同需求，详见 u5 单元。

---

### 4.2 原始统计函数家族：`Xdtr` 命名约定与 `scipy.stats` 的关系

#### 4.2.1 概念说明

在所有家族里，「**Raw statistical functions（原始统计函数）**」是最特殊的一大类，也是初学者最困惑的一类——它一口气占了文档十几个子节，函数名又高度相似（`bdtr`、`bdtrc`、`bdtri`、`bdtrik`、`bdtrin`……）。理解它的关键有两点：

1. **它是什么**：这些函数是各概率分布的 **CDF（累积分布函数）、生存函数 SF（= 1 − CDF）和分位数（CDF 的反函数）**，以**逐元素 ufunc** 的形式暴露。换句话说，它们是「**把统计计算拆成最低层的标量运算**」。

2. **它和 `scipy.stats` 什么关系**：文档在这个家族标题正下方放了一句 `seealso`，指向 `scipy.stats`，称后者为「**Friendly versions（友好版本）**」。

> [`__init__.py:217-220`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/__init__.py#L217-L220)：`Raw statistical functions` 标题下紧跟 `.. seealso:: :mod:`scipy.stats`: Friendly versions of these functions.`。这一句确立了二者的分工。

分工可以这样理解：

- `special.bdtr` / `fdtr` / `ndtr` ……：**底层、逐元素、无状态**。你直接传入数值，拿回数值。适合需要大批量、逐元素、或要嵌进自己算法里的场景。
- `scipy.stats.binom` / `f` / `norm` ……：**高层、面向对象、有状态**。每个分布是一个对象，带 `.cdf()`、`.ppf()`（分位数）、`.sf()`、`.mean()`、`.rvs()`（采样）等方法，还自动处理参数检验、广播、命名。适合做统计分析、建模。

二者**算的是同一批分布的同一批量**（例如 `special.bdtr(k, n, p)` 与 `scipy.stats.binom.cdf(k, n, p)` 给出同一个二项分布 CDF 值），区别只在「**裸计算 vs 友好封装**」。需要快速逐元素算 CDF/分位数就用 `special`；需要完整统计工作流就用 `stats`。

#### 4.2.2 核心流程

这一族的函数名遵循一套**高度规律的命名约定**，一旦看懂就能「望文生义」。模式是：

\[
\texttt{<分布字母>dtr} \;+\; \text{(可选的后缀)}
\]

- `dtr`：distribution，即 **CDF 本体**。
- `c`：**complementary**，补 CDF = **生存函数 SF**。
- `i`：**inverse**，反函数 = **分位数**；默认是「对取值变量 \(x\) 求逆」。
- 额外的尾字母：表示**对其他参数求逆**（因为一个分布往往有多个参数，每个都能被「反过来求」）。

以**二项分布**为例（见 [`__init__.py:222-232`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/__init__.py#L222-L232)）：

| 函数 | 含义 | 对应的「反」谁 |
|------|------|----------------|
| `bdtr(k, n, p)` | 二项 CDF \(P(X\le k)\) | —（本体） |
| `bdtrc(k, n, p)` | 生存函数 \(P(X>k)\) | —（补） |
| `bdtri(k, n, p)` | 给定 \(k,n\)，求使 CDF = p 的 **p** | 反 **p**（概率） |
| `bdtrik(k, y, n)` | 求 **k** | 反 **k**（成功次数） |
| `bdtrin(k, y, p)` | 求 **n** | 反 **n**（试验次数） |

这套约定在整个统计家族里**高度一致**。再举几例对照：

| 函数 | 所属分布 | 含义 |
|------|----------|------|
| `fdtr` / `fdtrc` / `fdtri` / `fdtridfd` | F 分布 | CDF / SF / 反 p / 反分母自由度 dfd |
| `chdtr` / `chdtrc` / `chdtri` / `chdtriv` | 卡方分布 | CDF / SF / 反 / 反自由度 v |
| `ncfdtr` / `ncfdtridfd` / `ncfdtridfn` / `ncfdtri` / `ncfdtrinc` | 非中心 F | CDF / 反 dfd / 反 dfn / 反 p / 反非中心 nc |
| `ndtr` / `ndtri` / `log_ndtr` / `ndtri_exp` | 标准正态 | CDF / 分位数 / CDF 的对数 / 反 log_ndtr |
| `pdtr` / `pdtrc` / `pdtri` / `pdtrik` | Poisson | CDF / SF / 反 m / 反 k |

尾字母对照表（速查）：

| 尾字母 | 反的是哪个参数 |
|--------|----------------|
| `i`（无额外字母，如 `bdtri`） | 主取值变量 / 概率 p |
| `ik` | k（计数） |
| `in` | n（次数/试验数） |
| `iv` | v（自由度） |
| `idf` / `idfd` / `idfn` | 自由度（分母 dfd / 分子 dfn） |
| `inc` | nc（非中心参数） |
| `imn` / `isd` | 均值 mn / 标准差 sd |
| `ix` | x |

> 记忆口诀：**看到 `dtr` 就是分布 CDF，`c` 是补，`i` 开头都是「反过来求」，尾字母告诉求的是哪个参数**。

#### 4.2.3 源码精读

先看统计家族在文档里的整体位置与 `seealso`：

> [`__init__.py:217-232`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/__init__.py#L217-L232)：`Raw statistical functions` 一级标题下，第一个子节就是 `Binomial distribution`（`^^^^` 标记），`autosummary` 列出 `bdtr`、`bdtrc`、`bdtri`、`bdtrik`、`bdtrin` 五个函数，正好印证上面的命名表。其上的 `seealso` 把读者引向 `scipy.stats`。

再确认这些函数在类型桩里的身份——它们确实都是 ufunc：

> [`_ufuncs.pyi:294-298`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_ufuncs.pyi#L294-L298)：`bdtr`、`bdtrc`、`bdtri`、`bdtrik`、`bdtrin` 逐个被声明为 `np.ufunc`。这与「原始统计函数 = 逐元素 ufunc」的说法一致。

注意一个对照点：同样是「分布相关」，**信息论函数**（`entr`、`rel_entr`、`kl_div`、`huber`、`pseudo_huber`）虽然也在统计家族附近（见 [`__init__.py:400-410`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/__init__.py#L400-L410)），但它们算的是**熵与散度**（如 KL 散度 \(D_{KL}(p\|q)\)），不是某个分布的 CDF，所以不遵循 `Xdtr` 命名约定。同样，`expit`/`logit`/`log_expit`（Sigmoidal，[`__init__.py:380-388`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/__init__.py#L380-L388)）是 logistic 函数族，也与 `Xdtr` 无关。**命名约定只在「分布的 CDF/SF/分位数」这一组函数内成立**，不要过度推广。

#### 4.2.4 代码实践

**实践目标**：亲手验证「原始统计函数与 `scipy.stats` 算同一个量」。

**操作步骤**：

```python
# stats_vs_special.py —— 示例代码
import scipy.special as sc
import scipy.stats as st

# 二项分布 Binom(n=10, p=0.3) 在 k=4 处的 CDF
k, n, p = 4, 10, 0.3
print("special.bdtr      =", sc.bdtr(k, n, p))
print("stats.binom.cdf   =", st.binom.cdf(k, n, p))

# 用 bdtri 反过来求 p：给定 k,n 和目标 CDF 值，求 p
target = sc.bdtr(k, n, p)
print("special.bdtri     =", sc.bdtri(k, n, target))   # 应回到 ~0.3

# 标准正态：special.ndtr vs stats.norm.cdf
print("special.ndtr(1.0) =", sc.ndtr(1.0))
print("stats.norm.cdf(1) =", st.norm.cdf(1.0))
```

**需要观察的现象**：`special.bdtr` 与 `stats.binom.cdf` 输出**完全相同**；`bdtri` 把 CDF 值代回去应恢复出原始 `p`；`ndtr(1.0)` 与 `norm.cdf(1)` 也相同（约为 0.8413）。

**预期结果（参考值）**：`bdtr(4,10,0.3) ≈ 0.8497`；`ndtr(1.0) ≈ 0.8413447`；`bdtri` 回代应得到 `≈ 0.3`。这些是数学上的确定值，但请以你本地运行为准——**待本地验证**。

> 注意：与 4.1.4 一样，我没有在你的环境运行此脚本。等价关系是 `special` 与 `stats` 文档共同保证的（`bdtr` 是 `binom` CDF 的底层实现之一），请运行核对数值。

#### 4.2.5 小练习与答案

**练习 1**：`nctdtrit` 这个名字，按命名约定应该反的是哪个参数？它属于哪个分布？

> **答案**：`nct` = 非中心 t 分布（noncentral t），`dtr` = CDF，尾部的 `it` = inverse w.r.t. **t**（即对取值 t 求逆，给出分位数）。所以 `nctdtrit` 是「非中心 t 分布的分位数函数」。对照 [`__init__.py:290-299`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/__init__.py#L290-L299)。

**练习 2**：为什么 `entr`、`kl_div` 不叫 `Xdtr`？

> **答案**：因为它们不是某个概率分布的 CDF/SF/分位数，而是**信息论量**（熵、KL 散度、损失）。`Xdtr` 命名约定只约束「分布的累积量及其反函数」这一组，信息论函数有自己独立的命名（见 [`__init__.py:400-410`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/__init__.py#L400-L410)）。

---

### 4.3 两张互补的目录：文档章节 vs `_ufuncs.pyi.__all__`，如何快速定位函数

#### 4.3.1 概念说明

要快速定位函数，你需要知道 `special` 其实有**两张并列的「目录」**，用途不同：

1. **人类目录**：[`__init__.py`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/__init__.py) 顶部的文档字符串。它按家族分章节、每章带 `autosummary`，是**最完整**的清单——包括所有 ufunc **和** 少数非 ufunc（零点、序列函数）。它是官方网页文档的源材料。

2. **机器目录**：[`_ufuncs.pyi`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_ufuncs.pyi) 里的 `__all__` 列表（L5–L243）。它只列 `_ufuncs` 这个扩展模块导出的名字，**全是 ufunc**，但**不含** `_basic`、`_orthogonal`、`_logsumexp` 等其它子模块的函数。

关键洞见：**人类目录 ⊋ 机器目录**。也就是说：

- 凡在 `_ufuncs.pyi.__all__` 里的名字，一定是 ufunc。
- 但有些函数（如 `jn_zeros`、`comb`、`logsumexp`、`roots_legendre`）能在 `special` 命名空间里调用、也出现在人类目录里，却**不在** `_ufuncs.pyi.__all__` 里——因为它们来自别的子模块，而且往往不是 ufunc。

这套差异的根源在 u1-l4 讲过：`special.__all__` 是 `_ufuncs.__all__ + _basic.__all__ + _orthogonal.__all__ + _multiufuncs.__all__` 再加手动补丁（见 [`__init__.py:825-841`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/__init__.py#L825-L841)）。所以 `_ufuncs.pyi.__all__` 只是其中「住在 `_ufuncs` 里」的那一摊。

#### 4.3.2 核心流程

拿到一个需求，推荐这套「三步定位法」：

1. **定家族**：用 4.1 的分类表，把问题映射到家族，在 [`__init__.py`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/__init__.py) 找到对应章节。
2. **读简介**：在该章节的 `autosummary` 里，读每个函数名后的一句话描述，锁定候选。注意章节里是否有「**The following functions do not accept NumPy arrays**」警告——若有，被警告的函数**不是 ufunc**，不能传数组。
3. **验身份**：用 `isinstance(name, np.ufunc)` 或查 `_ufuncs.pyi` 确认候选是否 ufunc；若是，用 `name.types` 看它支持哪些 dtype（是否支持复数，见 u2-l1）。

「非 ufunc 警告」是文档里最重要的导航信号之一。它在多个家族里反复出现，每出现一次就圈出一批「**只能传标量、返回序列**」的函数：

> [`__init__.py:110-116`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/__init__.py#L110-L116)：Bessel 家族里，`lmbda`（Jahnke-Emden Λ 函数）被警告「不接受 NumPy 数组」。
>
> [`__init__.py:118-135`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/__init__.py#L118-L135)：Bessel 零点子节（`jn_zeros`、`yn_zeros`、`jnp_zeros` 等）整节都不是 ufunc。
>
> [`__init__.py:463-471`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/__init__.py#L463-L471)：误差函数家族里，`erf_zeros`、`fresnelc_zeros`、`fresnels_zeros` 不是 ufunc。
>
> [`__init__.py:612-620`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/__init__.py#L612-L620)：抛物柱面函数里，`pbdv_seq`、`pbvv_seq`、`pbdn_seq` 不是 ufunc。

规律很清楚：凡是名字带 `_zeros`、`_seq`、或是「一次性返回一串」的函数（零点序列、系数序列），几乎都不是 ufunc——因为它们**输出长度依赖于参数**（如「求前 nt 个零点」），而 ufunc 要求「输入形状决定输出形状」。这是 u2-l1 讲过的「必然逐元素」约束的直接推论。

#### 4.3.3 源码精读

先看两张目录的「体量差」。机器目录 `_ufuncs.pyi.__all__`：

> [`_ufuncs.pyi:5-243`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_ufuncs.pyi#L5-L243)：一个长列表，开头三个是 `geterr`/`seterr`/`errstate`（错误控制，非数学函数），其余按字母排列，全是 ufunc 名字。这是「住在 `_ufuncs` 扩展模块里」的完整函数集。

而 `special` 真正对外公开的 API 是更宽的：

> [`__init__.py:825-841`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/__init__.py#L825-L841)：`__all__` 由四个子模块的 `__all__` 相加，再手动补上 `logsumexp`/`softmax`/`log_softmax`、`multigammaln`、`ellip_harm*`、`lambertw`、`spherical_*` 等。这些手动补的名字大多来自 `_logsumexp`/`_lambertw`/`_spherical_bessel` 等小专项模块，**既不在 `_ufuncs.pyi.__all__`，也未必是 ufunc**。

做一个对照实验，立刻看到「人类目录 ⊋ 机器目录」：

- `logsumexp`：能在 `special` 里调用（[`__init__.py:779`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/__init__.py#L779) 文档把它列在 Convenience functions），但**不在** `_ufuncs.pyi.__all__`，因为它来自 `_logsumexp.py` 且**不是 ufunc**（它要跨元素求和，违反「必然逐元素」）。
- `jn_zeros`：出现在 Bessel 零点子节（人类目录），但**不在** `_ufuncs.pyi.__all__`，因为它来自 `_basic.py` 且不是 ufunc。
- `comb`、`factorial`：在组合数学家族（[`__init__.py:713-721`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/__init__.py#L713-L721)），来自 `_basic.py`，也不在 `_ufuncs.pyi.__all__`。

反过来，`_ufuncs.pyi.__all__` 里的每个名字（除 `geterr/seterr/errstate`）都一定是 ufunc，例如：

> [`_ufuncs.pyi:341`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_ufuncs.pyi#L341)、[`L375`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_ufuncs.pyi#L375)、[`L415`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_ufuncs.pyi#L415)：`erf: np.ufunc`、`gamma: np.ufunc`、`jv: np.ufunc`——本讲综合实践要用的三个函数，都明确是 ufunc。

#### 4.3.4 代码实践

**实践目标**：写一个「函数探测器」，对一个函数名自动判断：它在不在 `special` 命名空间、是不是 ufunc、支持哪些 dtype。把本讲三块知识串起来。

**操作步骤**：

```python
# probe.py —— 示例代码
import numpy as np
import scipy.special as sc

def probe(name):
    obj = getattr(sc, name, None)
    if obj is None:
        return f"{name}: 不在 scipy.special 命名空间"
    is_uf = isinstance(obj, np.ufunc)
    types = obj.types if is_uf else []
    # 复数支持：看 types 里是否有含 'F'/'D'/'G' 的环
    has_complex = any(("F" in t) or ("D" in t) or ("G" in t) for t in types)
    return (f"{name}: ufunc={is_uf}; types={types}; 支持复数={has_complex if is_uf else 'N/A'}")

for nm in ["erf", "gamma", "jv", "hyp2f1", "logsumexp", "jn_zeros", "comb", "bdtr"]:
    print(probe(nm))
```

**需要观察的现象**：

- `erf`、`gamma`、`jv`、`hyp2f1`、`bdtr`：`ufunc=True`，并列出各自支持的 dtype 环（如 `erf` 应含 `f->f`、`d->d`、`F->F`、`D->D` 四环，说明支持复数）。
- `logsumexp`、`jn_zeros`、`comb`：`ufunc=False`——它们能调用，但不是 ufunc，印证「人类目录 ⊋ 机器目录」。

**预期结果（参考值）**：`erf` 的 `types` 为 `('f->f', 'd->d', 'F->F', 'D->D')`，支持复数；`gamma` 不支持复数输入（Gamma 函数复数版另有 `rgamma`/对数版 `loggamma`）；`logsumexp`/`jn_zeros`/`comb` 均为 `ufunc=False`。具体 dtype 环以本地运行为准——**待本地验证**。

> 注意：同前两节，我没有在你的环境运行此脚本。`erf` 挂 4 个 loop 的事实来自 u2-l1 的源码佐证（`_special_ufuncs.cpp` 注册），`logsumexp` 非ufunc 的事实来自它的实现性质；请运行核对。

#### 4.3.5 小练习与答案

**练习 1**：`hyp2f1`（高斯超几何函数）在 `_ufuncs.pyi.__all__` 里吗？它是 ufunc 吗？支持复数吗？

> **答案**：在。见 [`_ufuncs.pyi:112`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_ufuncs.pyi#L112)（`__all__` 内）与 [`L394`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_ufuncs.pyi#L394)（`hyp2f1: np.ufunc`）。它是 ufunc，且支持复数（`hyp2f1` 的 `.types` 会列出含 `D` 的复数环）。属于超几何家族（[`__init__.py:590-599`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/__init__.py#L590-L599)）。

**练习 2**：我想求「Legendre 多项式 \(P_{25}(0.3)\) 的值」。该用 `legendre(25)` 还是 `eval_legendre(25, 0.3)`？为什么？

> **答案**：用 `eval_legendre`。因为高阶（order > 20）用系数法（`legendre(25)` 返回的 `orthopoly1d`）数值不稳定，文档明确警告应改用 `eval_*`（见 [`__init__.py:583-588`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/__init__.py#L583-L588)）。这是「定位函数」之外、还要「选对接口」的典型例子，详见 u5。

---

## 5. 综合实践

**任务**：写一个 30 行左右的「`special` 函数导览器」，把本讲三块知识（分类地图 / 统计家族 / 两张目录）全部用上。

要求：

1. 维护一个 `需求 → 家族` 的小词典，例如：
   - `"二项分布的 0.95 分位数"` → 统计家族 / 二项分布
   - `"算 exp(1000)+exp(1001) 的对数和而不溢出"` → 便利函数
   - `"求 25 阶 Legendre 多项式在 0.3 处的值"` → 正交多项式
   - `"标准正态在 1.0 处的 CDF"` → 统计家族 / 正态分布
2. 对每条需求，程序应：(a) 输出建议的家族；(b) 选出具体函数名；(c) 用 `isinstance(.., np.ufunc)` 报告它是否 ufunc；(d) 若是统计家族，额外用 `scipy.stats` 算同一个量做交叉验证。
3. 把选出的函数按「在 `_ufuncs.pyi.__all__` 里 / 不在」分两类打印（提示：`_ufuncs.__all__` 可由 `import scipy.special._ufuncs as _u; _u.__all__` 取到）。

**参考实现骨架**（示例代码，需你补全并运行）：

```python
# guide.py —— 示例代码（骨架）
import numpy as np
import scipy.special as sc
import scipy.special._ufuncs as _u
import scipy.stats as st

UFUNC_NAMES = set(_u.__all__)   # 机器目录

def guide(need, family, fn_name, fn_args, stats_check=None):
    print(f"需求: {need}")
    print(f"  家族: {family}")
    obj = getattr(sc, fn_name)
    in_machine = fn_name in UFUNC_NAMES
    print(f"  函数: {fn_name} | ufunc={isinstance(obj, np.ufunc)} "
          f"| 在 _ufuncs.__all__={in_machine}")
    val = obj(*fn_args)
    print(f"  结果: {val}")
    if stats_check is not None:
        sval = stats_check(*fn_args)
        print(f"  stats 交叉验证: {sval}  (一致={np.allclose(val, sval)})")
    print()

guide("二项分布的 0.95 分位数", "统计/二项分布", "bdtri",
      (4, 10, 0.95),            # 求 p 使 bdtr(k=4,n=10,p)=0.95
      lambda k, n, p: st.binom.ppf(p, n, k))   # 注意参数顺序差异

guide("标准正态在 1.0 处的 CDF", "统计/正态", "ndtr",
      (1.0,), lambda x: st.norm.cdf(x))

guide("log-sum-exp 不溢出", "便利函数", "logsumexp",
      (np.array([1000., 1001.]),), None)
```

**需要观察的现象**：

- 统计家族的 `special` 结果与 `stats` 结果**一致**（注意 `binom` 的参数顺序与 `bdtri` 不同，骨架里已提示）。
- `logsumexp` 应标注 `ufunc=False`、`在 _ufuncs.__all__=False`，体现「人类目录 ⊋ 机器目录」。
- `bdtri`、`ndtr` 应标注 `ufunc=True`、`在 _ufuncs.__all__=True`。

**预期结果（参考值）**：`ndtr(1.0) ≈ 0.8413` 且与 `norm.cdf(1)` 一致；`logsumexp([1000,1001]) ≈ 1001.313`（不溢出）。具体值请以本地运行核对——**待本地验证**。

> 这个综合实践把三件事拧到一起：用分类地图**定家族**（4.1）、用 `Xdtr` 命名约定与 `stats` 关系**选统计函数**（4.2）、用「两张目录」+ `isinstance` **验身份**（4.3）。能跑通它，你就真正掌握了 `special` 的导航术。

## 6. 本讲小结

- `scipy.special` 的 250+ 函数**按约 20 个数学家族分章节陈列**，文档字符串（[`__init__.py`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/__init__.py)）就是这张分类地图；先「定家族」再挑函数，比在字母表里大海捞针高效得多。
- **Raw statistical functions** 是特殊的一大类：它们是各概率分布的 CDF / 生存函数 / 分位数，以**逐元素 ufunc** 暴露；遵循 `<分布>dtr [+c/i/...]` 命名约定，尾字母指明「反的是哪个参数」。
- 这些原始统计函数与 `scipy.stats` 算**同一批量**，区别是「裸计算 vs 友好封装」——文档用 `seealso`（[`__init__.py:217-220`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/__init__.py#L217-L220)）把二者关联起来。
- 存在**两张互补的目录**：人类目录（文档字符串，最全，含非 ufunc）⊋ 机器目录（[`_ufuncs.pyi.__all__`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/_ufuncs.pyi#L5-L243)，只含 `_ufuncs` 模块的 ufunc）。
- 「**The following functions do not accept NumPy arrays**」警告段落是关键导航信号：被圈出的函数（零点序列、`_seq`、`logsumexp`、`comb` 等）**不是 ufunc**，根因是它们输出长度依赖参数、违反「必然逐元素」。
- 定位函数的三步法：**定家族 → 读简介（留意警告）→ 验身份**（`isinstance(.., np.ufunc)` 或查 `.pyi` / `.types`）。

## 7. 下一步学习建议

- **进入「机制」层**：本讲只讲了函数「**怎么分类、怎么找**」。接下来 u3 单元（代码生成管线）会回答「这 250 个 ufunc 是**怎么从 `functions.json` 一键生成出来的**」——那是 `special` 的工程心脏，强烈建议先读 u3-l1（`functions.json` 声明式签名）。
- **深入统计家族**：若你对 4.2 的统计函数感兴趣，可跳到 u8 单元看它们的 C/C++ 后端——这些 `Xdtr` 多由 **Cephes / cdflib / Boost.Math** 提供（u8-l2、u8-l4）。
- **正交多项式**：4.1 提到的 `eval_*` vs `roots_*` vs `orthopoly1d` 三套接口的差异，在 u5 单元有完整讲解（u5-l2）。
- **随手练**：打开 [`__init__.py`](https://github.com/scipy/scipy/blob/278c037d07b7dc5f397965b3dbd7125a97270463/scipy/special/__init__.py)，任选一个你从没用过的家族（比如 Kelvin 函数或 Mathieu 函数），读它的 `autosummary`，挑一个函数，用本讲的「三步定位法」判断它是不是 ufunc、支持不支持复数，再算一个值看看。
