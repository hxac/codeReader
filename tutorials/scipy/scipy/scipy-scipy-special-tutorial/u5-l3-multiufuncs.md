# _multiufuncs.py：MultiUFunc 与多输出函数

## 1. 本讲目标

u5-l1、u5-l2 讲完了「老的」正交多项式接口：`roots_*`（高斯求积节点）与 `eval_*`（逐元素求值）。本讲把镜头转向 SciPy 里**更新、也更特殊**的一组函数：

- `legendre_p` / `legendre_p_all`
- `assoc_legendre_p` / `assoc_legendre_p_all`
- `sph_legendre_p` / `sph_legendre_p_all`
- `sph_harm_y` / `sph_harm_y_all`

这 8 个函数都有同一个特点：**它们面向用户的「单个函数名」背后，其实是一整组底层 ufunc / gufunc**——按 `diff_n`（要几阶导数）、`norm`（是否归一化）等开关动态选用其中一个；其中带 `_all` 后缀的还会**一次性返回从 0 阶到 n 阶的全部结果**，输出多出一个「阶数」维度。

ufunc 的类型分发只看输入 dtype，**看不到 `diff_n`、`norm` 这种布尔/整数开关**（这是 u4-l4 薄包装讲里反复强调过的）。所以 SciPy 用一个纯 Python 的 `MultiUFunc` 类把这些开关「翻译成选哪一个 ufunc」，再把结果**重新塑形**成对用户友好的多输出形式。

读完本讲，你应当能够：

- 说清楚 `MultiUFunc` 如何把「一个 ufunc」或「一组 ufunc（tuple / dict）」聚合成**单一可调用对象**，并在调用时按关键字参数 `_resolve_ufunc` 解析到具体内核。
- 解释 `_all` 系列的**多输出语义**：输出形状里的 `(n+1,)`、`(2m+1,)` 维度从哪来，`diff_n` 又如何额外引入「值 / 梯度 / Hessian」多组返回。
- 区分 `_special_ufuncs`（普通 ufunc，逐点求值）与 `_gufuncs`（广义 ufunc，含随 `n` 变化的输出核心维度）这两套 C++ 扩展的分工，理解为什么 `_all` 必须走 gufunc。

本讲精读 [`_multiufuncs.py`](_multiufuncs.py)，并对照 C++ 注册侧 [`_special_ufuncs.cpp`](_special_ufuncs.cpp)、[`_gufuncs.cpp`](_gufuncs.cpp) 与构建侧 [`meson.build`](meson.build)。

## 2. 前置知识

阅读本讲前，最好已经掌握（u2-l1、u5-l2 已建立）：

- **ufunc 的本质与限制**：ufunc 按 dtype 做多类型分发、逐元素求值、可广播；但它的分发**只看 dtype，看不到布尔/整数关键字参数**，输出形状也只能由输入广播决定。
- **gufunc（广义 ufunc）**：在 ufunc 基础上引入「核心维度（core dimension）」。普通 ufunc 是纯逐元素，gufunc 则允许在「核心维度」上做聚合或展开。签名 `"()->(3)"` 表示标量进、长度 3 的核心维度出；`"(),()->(np1,mpmp1,1)"` 表示两个标量进、含命名核心维度 `(np1, mpmp1)` 的块出。
- **正交多项式的递推求值**：`eval_*` 用三项递推稳定地算出 \(P_n(z)\)，无需系数表示。

本讲用到几个新概念，先用大白话解释：

- **多类型分发 vs 多内核分发**：ufunc 的「多类型分发」是同一个 ufunc 内部按 `f/d/F/D` 选 loop；本讲的「多内核分发」是 `MultiUFunc` 在**多个独立的 ufunc 对象之间**挑选——挑哪个由 `diff_n`、`norm` 这类业务开关决定，与 dtype 无关。
- **导数族（derivative family）**：对一个标量函数 \(f(z)\)，把它的 0 阶（值）、1 阶（导数）、2 阶（二阶导 / Hessian）打包成一组返回。`diff_n` 就是「请顺便给我到几阶导」的开关。
- **全阶返回（all-orders）**：`legendre_p(3, z)` 只给 \(P_3(z)\)；`legendre_p_all(3, z)` 一次性给 \(P_0(z), P_1(z), P_2(z), P_3(z)\) 一整串。后者多出来的「阶数」轴就是 gufunc 的输出核心维度。

## 3. 本讲源码地图

| 文件 / 区域 | 行号区间 | 本讲关注什么 |
| --- | --- | --- |
| `_multiufuncs.py` — 模块导入 | 7–11 | 4 个 ufunc 来自 `_special_ufuncs`，4 个 gufunc 来自 `_gufuncs` |
| 同上 — `MultiUFunc.__init__` | 25–57 | 校验「一组 ufunc 输入类型一致」、登记各种回退钩子 |
| 同上 — `_override_*` 装饰器族 | 63–83 | 如何用装饰器注入 `key`/`resolve_out_shapes`/`finalize_out` |
| 同上 — `_resolve_ufunc` | 85–92 | 按 `_key(**kwargs)` 在 tuple/dict 里挑 ufunc |
| 同上 — `__call__` | 94–141 | 解析 → 算输出形状/dtype → 喂给底层 ufunc → 收尾塑形 |
| 同上 — `sph_legendre_p`（tuple 范例） | 144–196 | 最简单的「按 diff_n 选 ufunc + moveaxis」模板 |
| 同上 — `legendre_p_all` | 511–568 | `_all` 系列：`resolve_out_shapes` 决定 `(n+1,)` 轴 |
| 同上 — `sph_harm_y`（复数多输出） | 571–663 | `force_complex_output` + 梯度/Hessian 拆解 |
| 同上 — `sph_harm_y_all` | 665–744 | 阶数 + 阶 + 导数块的复合输出形状 |
| `_special_ufuncs.cpp` — `legendre_p` | 975–989 | C++ 侧把 3 个 gufunc 包成 `(N,N,N)` tuple |
| 同上 — `sph_harm_y` | 1330–1347 | 同上，签名 `(),(),(),()->(k,k)` 含导数块 |
| `_gufuncs.cpp` — `legendre_p_all` | 138–158 | gufunc 签名 `()->(np1,k)`，输出核心维度随 `n` 变 |
| 同上 — `sph_harm_y_all` | 282–296 | 签名 `(),()->(np1,mpmp1,k,k)`，含 map_dims 回调 |
| 同上 — `*_map_dims` 辅助 | 34–58 | 把「核心维度大小」告诉 NumPy |
| `meson.build` — 两个扩展模块 | 34–52 | `_special_ufuncs` 与 `_gufuncs` 各自独立编译 |
| `__init__.py` — 导入 `_multiufuncs` | 800–801 | 8 个函数如何被提到顶层命名空间 |
| `__init__.py` — 文档串条目 | 479–486 | 这组函数在函数目录里的官方定位 |

一句话总览：**面向用户的「一个函数名」其实是 `MultiUFunc` 实例；实例内部装着一组 ufunc（tuple 按 `diff_n` 索引、dict 按 `(norm, diff_n)` 索引），调用时按关键字参数挑出具体内核、再由 `resolve_out_shapes`/`finalize_out` 把结果整形成对用户友好的多输出形态**。这组底层 ufunc/gufunc 分别住在 `_special_ufuncs`（逐点）和 `_gufuncs`（含随 `n` 变化的输出轴）两个 C++ 扩展里。

## 4. 核心概念与源码讲解

### 4.1 MultiUFunc 聚合：把一组 ufunc 包成单一可调用对象

#### 4.1.1 概念说明

设想你要给用户暴露一个函数 `legendre_p(n, z, *, diff_n=0)`，其中 `diff_n` 取 0、1、2 分别表示「只算值」「算值+一阶导」「算值+一阶导+二阶导」。底层 C++ 为这三个情形各写了一个独立的 gufunc 内核（导数阶数不同，核心维度大小 1/2/3 不同）。于是 C++ 侧把这三个 gufunc 打包成一个 **3 元 tuple** 注册到模块里。

问题来了：ufunc 的多类型分发机制**只看 dtype，看不到 `diff_n` 这个整数关键字**。`diff_n=1` 和 `diff_n=2` 在 dtype 上毫无区别（输入都是同一个 `z`），ufunc 自己挑不出该用哪个内核。

`MultiUFunc` 就是来解决这个「按业务关键字选内核」问题的中间层。它的职责有三：

1. **持有**一组 ufunc（单个、tuple、或 dict）；
2. 在调用时根据关键字参数**挑出**正确的那个 ufunc；
3. 把挑出来的 ufunc 的原始输出，**整形**成用户期望的形状与返回结构。

#### 4.1.2 核心流程

`MultiUFunc.__call__` 的执行流程（伪代码）：

```text
def __call__(self, *args, **kwargs):
    kwargs = default_kwargs | kwargs                 # 合并默认开关
    args   += ufunc_default_args(**kwargs)           # 补上底层需要的额外位置参数
    ufunc   = self._resolve_ufunc(**kwargs)          # ★ 按关键字挑内核
    ufunc_args = [asarray(a) for a in args[-ufunc.nin:]]   # 取最后 nin 个数组实参

    if 已配置 resolve_out_shapes:                    # 多输出 / _all 情形
        shapes = resolve_out_shapes(标量参数..., *数组形状, nout, **kwargs)
        dtypes = 推断输出 dtype（必要时强制复数）
        out    = 预分配的空数组元组
        ufunc_kwargs['out'] = out                    # 让 ufunc 写入预分配数组

    out = ufunc(*ufunc_args, **ufunc_kwargs)         # 真正调用底层内核
    if 已配置 finalize_out:
        out = finalize_out(out)                      # ★ moveaxis / 拆梯度Hessian
    return out
```

两个关键钩子是 `_resolve_ufunc`（选内核）与 `finalize_out`（整形），分别由 `_override_key` 和 `_override_finalize_out` 装饰器注入。

#### 4.1.3 源码精读

先看构造与校验。`MultiUFunc.__init__` 接受三种形态的 `ufunc_or_ufuncs`：单个 `np.ufunc`、一个 tuple/list、或一个 dict，见 [_multiufuncs.py:25-57](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_multiufuncs.py#L25-L57)。当传入的是「一组」时，它做了一项**关键校验**——要求这一组里所有 ufunc 的**输入类型必须完全一致**：

```python
seen_input_types = set()
for ufunc in ufuncs_iter:
    if not isinstance(ufunc, np.ufunc):
        raise ValueError("All ufuncs must have type `numpy.ufunc`.")
    seen_input_types.add(frozenset(x.split("->")[0] for x in ufunc.types))
if len(seen_input_types) > 1:
    raise ValueError("All ufuncs must take the same input types.")
```

这段（[_multiufuncs.py:38-45](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_multiufuncs.py#L38-L45)）取每个 ufunc 的 `.types`（形如 `['ll->d', 'dd->d', ...]`），用 `split("->")[0]` 截出输入侧（`'ll'`、`'dd'`），冻成集合塞进 `seen_input_types`。若组内出现不一致，说明这「一组」ufunc 接受的输入不同，那就无法用一个统一的 `MultiUFunc` 签名对外暴露，直接报错。这是对「同组内核应能互换」的强约束。

再看选内核的 `_resolve_ufunc`，见 [_multiufuncs.py:85-92](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_multiufuncs.py#L85-L92)：

```python
def _resolve_ufunc(self, **kwargs):
    if isinstance(self._ufunc_or_ufuncs, np.ufunc):
        return self._ufunc_or_ufuncs          # 单内核：原样返回
    ufunc_key = self._key(**kwargs)            # 计算索引键
    return self._ufunc_or_ufuncs[ufunc_key]    # tuple[key] 或 dict[key]
```

- **单 ufunc**：直接返回，连 `_key` 都不用。
- **tuple**（如 `sph_legendre_p`、`legendre_p`、`sph_harm_y` 及它们的 `_all`）：`_key` 返回一个整数 `diff_n ∈ {0,1,2}`，用 `tuple[diff_n]` 取内核。
- **dict**（如 `assoc_legendre_p`）：`_key` 返回一个二元组 `(norm, diff_n)`，用 `dict[(norm, diff_n)]` 取内核。

`_key` 函数由 `_override_key` 装饰器逐个函数定制。最简单的 `sph_legendre_p` 见 [_multiufuncs.py:183-191](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_multiufuncs.py#L183-L191)：它先用 `_nonneg_int_or_fail`（[_input_validation.py:4-17](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_input_validation.py#L4-L17)）把 `diff_n` 规整为非负整数，限制在 0–2，然后**原样返回 `diff_n` 作为 tuple 索引**。

C++ 侧怎么造出这个 tuple？看 `_special_ufuncs.cpp` 里的 `legendre_p` 注册，[_special_ufuncs.cpp:975-989](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_special_ufuncs.cpp#L975-L989)：

```cpp
PyObject *legendre_p = Py_BuildValue(
    "(N, N, N)",                       // ← 3 元 tuple
    xsf::numpy::gufunc({...}, "legendre_p", nullptr, "(),()->(1)", ...),  // diff_n=0
    xsf::numpy::gufunc({...}, "legendre_p", nullptr, "(),()->(2)", ...),  // diff_n=1
    xsf::numpy::gufunc({...}, "legendre_p", nullptr, "(),()->(3)", ...)); // diff_n=2
```

`Py_BuildValue("(N, N, N)", ...)` 用格式串 `(N,N,N)` 把三个 gufunc 对象拼成一个 Python tuple（`N` 表示「 steals  一个新的对象引用」）。三个 gufunc 的签名 `(),()->(1|2|3)` 末尾那个数字正是导数块的核心维度大小。于是 Python 侧 `from ._special_ufuncs import legendre_p` 拿到的 `legendre_p` 本身就是个 3 元 tuple，再被 `MultiUFunc(legendre_p, ...)` 包裹。

#### 4.1.4 代码实践

**实践目标**：亲手验证 `MultiUFunc` 内部的 tuple 结构与索引机制，理解「面向用户一个名、底层三个内核」。

**操作步骤**：

1. 在已安装 SciPy 的环境里运行下面的脚本（**示例代码**，非项目原有）：

   ```python
   import scipy.special as sc
   from scipy.special import _multiufuncs as mu

   # 1) legendre_p 是 MultiUFunc 实例
   print(type(sc.legendre_p))                 # <class '...MultiUFunc'>

   # 2) 它内部装着一个 tuple，长度 = 3（对应 diff_n 0/1/2）
   inner = sc.legendre_p._ufunc_or_ufuncs
   print(type(inner), len(inner))             # <class 'tuple'> 3

   # 3) 每个 entry 都是 numpy.ufunc（gufunc 也是 ufunc 的子类化对象）
   for i, u in enumerate(inner):
       print(i, type(u), u.nout)
   ```

2. 接着对比 `diff_n=0` 与手动取 `tuple[0]` 调用，结果应一致：

   ```python
   import numpy as np
   z = np.array([0.0, 0.5, 1.0])
   a = sc.legendre_p(3, z)                      # diff_n=0，经 MultiUFunc 选 tuple[0]
   b = inner[0](3, z)                           # 直接调第 0 个内核
   print(np.allclose(a, b))                     # True
   ```

**需要观察的现象**：第 1 步打印出 `MultiUFunc` 与长度 3 的 tuple；第 2 步两个结果完全一致，证明 `MultiUFunc` 确实只是「按 `diff_n` 在 tuple 里挑了第 0 个」。

**预期结果**：`type(sc.legendre_p)` 是 `MultiUFunc`；`a` 与 `b` 数值相等。

> 注：`_ufunc_or_ufuncs` 是「下划线前缀」的内部属性，仅用于学习观察，不属于公开 API，未来版本可能改名。

#### 4.1.5 小练习与答案

**练习 1**：`assoc_legendre_p` 用的是 dict 而非 tuple。请说出它的索引键是什么，为什么需要两个分量。

**参考答案**：键是 `(norm, diff_n)`（见 [_multiufuncs.py:335-343](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_multiufuncs.py#L335-L343)）。因为 `assoc_legendre_p` 有两个互相独立的业务开关——`norm`（归一化与否）与 `diff_n`（导数阶数）——它们的组合 \(3 \times 2 = 6\) 个情形各对应一个内核，tuple 的单一下标装不下，故用二元组作 dict 键。

**练习 2**：若有人新加了一个 `diff_n=3` 的内核并把它塞进 tuple 末尾，但不改 `_override_key`，调用 `legendre_p(3, 0.5, diff_n=3)` 会发生什么？

**参考答案**：`_override_key` 会先在 [_multiufuncs.py:498-502](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_multiufuncs.py#L498-L502) 检查 `0 <= diff_n <= 2` 并抛 `NotImplementedError`，根本不会走到 tuple 索引。也就是说 `_override_key` 同时承担「输入校验」与「索引计算」两职，是内核选择的唯一闸门。

---

### 4.2 多输出语义：diff_n 导数族与 _all 全阶返回

#### 4.2.1 概念说明

`MultiUFunc` 之所以叫「multi」，不只是因为它聚合了**多个内核**，还因为它要处理**多组输出**。本模块的多输出有两层含义：

- **导数族多输出**：`diff_n >= 1` 时，函数要同时返回「值」和「导数（梯度）」，`diff_n = 2` 还要再返回 Hessian。返回值从单个数组变成元组。
- **全阶多输出**：`_all` 后缀的函数返回从 0 阶到 n 阶（球谐还要从 −m 到 +m 阶）的**一整片**结果，输出数组多出一个甚至两个「阶数轴」。

这两层叠加，输出形状会变得相当复杂，这也是为什么 `MultiUFunc` 需要一套 `resolve_out_shapes` + `finalize_out` 机制来动态算形状并收尾整形。

#### 4.2.2 核心流程

以 `sph_harm_y`（球谐函数）为例，它定义为

\[
Y_n^m(\theta,\phi) = \sqrt{\frac{2n+1}{4\pi}\frac{(n-m)!}{(n+m)!}}\, P_n^m(\cos\theta)\, e^{im\phi}
\]

`diff_n` 控制是否额外给出对 \((\theta,\phi)\) 的导数。底层 gufunc 的签名（[_special_ufuncs.cpp:1330-1347](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_special_ufuncs.cpp#L1330-L1347)）末尾的核心维度块大小 `k` 随 `diff_n` 变：

| diff_n | gufunc 签名 | 核心块形状 | 用户得到 |
| --- | --- | --- | --- |
| 0 | `(),(),(),()->(1,1)` | \(1\times1\) | 单个数组（值） |
| 1 | `(),(),(),()->(2,2)` | \(2\times2\) | `(值, 梯度)` |
| 2 | `(),(),(),()->(3,3)` | \(3\times3\) | `(值, 梯度, Hessian)` |

底层数组多出来的 \(k\times k\) 块里，`[0,0]` 是值，`[1,:]`/`[:,1]` 是梯度两分量，`[2,:]`/`[:,2]` 是 Hessian 四分量。`finalize_out` 负责把这块「解包」成对用户友好的元组，见 [_multiufuncs.py:652-662](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_multiufuncs.py#L652-L662)。

`_all` 系列则额外多出阶数轴。`legendre_p_all(n, z)` 的输出形状是 `(diff_n+1, n+1, ...)`：第一轴是导数阶、第二轴是 0..n 各阶、`...` 是 `z` 广播后的形状。这个 `(n+1,)` 轴由 gufunc 签名 `()->(np1,k)` 里的核心维度 `np1`（意为 n+1）提供，其大小随标量 `n` 而变——这正是它必须是 **gufunc**（而非普通 ufunc）的原因：普通 ufunc 的输出形状只能由输入广播决定，无法凭空多出一个「长度 = n+1」的轴。

#### 4.2.3 源码精读

**形状的来源**：`resolve_out_shapes` 钩子负责告诉 `MultiUFunc`「底层 ufunc 该往什么形状的数组里写」。看 `legendre_p_all` 的实现 [_multiufuncs.py:559-563](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_multiufuncs.py#L559-L563)：

```python
@legendre_p_all._override_resolve_out_shapes
def _(n, z_shape, nout, diff_n):
    n = _nonneg_int_or_fail(n, 'n', strict=False)
    return nout * ((n + 1,) + z_shape + (diff_n + 1,),)
```

它的参数是 `MultiUFunc.__call__` 在 [_multiufuncs.py:106-110](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_multiufuncs.py#L106-L110) 传进来的：标量参数 `n`、数组参数 `z` 的形状 `z_shape`、输出个数 `nout`、以及关键字 `diff_n`。返回值是一个 tuple，每个元素描述一个输出数组的形状——这里是 `(n+1,) + z_shape + (diff_n+1,)`，即「阶数轴 + 输入广播轴 + 导数块轴」。`nout * (...)` 把这个形状复制 `nout` 份（多输出时每个输出同形）。

**dtype 的推断与强制复数**：在 `__call__` 里 [_multiufuncs.py:116-129](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_multiufuncs.py#L116-L129)，若 ufunc 支持 `resolve_dtypes` 就用它精确推断；否则取输入的 `result_type`，非浮点则兜底 `float64`。球谐函数结果必为复数，所以 `sph_harm_y` 构造时传了 `force_complex_output=True`，于是在 [_multiufuncs.py:127-129](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_multiufuncs.py#L127-L129) 把每个输出 dtype 与 `1j` 做 `result_type` 强行提升成复数——即便用户只传了实数 `theta, phi`，输出仍是 `complex128`。

**结果的整形（moveaxis）**：底层 gufunc 把「导数块」放在**最后一根轴**（因为签名 `->(...,k)` 里 `k` 在最后），但用户文档承诺导数阶在**第一根轴**（`（diff_n+1, n+1, ...)`）。`finalize_out` 用一句 `np.moveaxis(out, -1, 0)` 把末轴搬到最前，见 [_multiufuncs.py:566-568](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_multiufuncs.py#L566-L568)。这是「底层布局」与「用户接口」之间的桥梁。

**球谐的解包**：`sph_harm_y` 的 `finalize_out` 更复杂，因为它要把 \(k\times k\) 的导数块拆成「值 / 梯度 / Hessian」，[_multiufuncs.py:652-662](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_multiufuncs.py#L652-L662)：

```python
@sph_harm_y._override_finalize_out
def _(out):
    if (out.shape[-1] == 1):
        return out[..., 0, 0]                                  # diff_n=0：只取值
    if (out.shape[-1] == 2):
        return out[..., 0, 0], out[..., [1, 0], [0, 1]]        # 值 + 梯度(d/dθ, d/dφ)
    if (out.shape[-1] == 3):
        return (out[..., 0, 0], out[..., [1, 0], [0, 1]],
            out[..., [[2,1],[1,0]], [[0,1],[1,2]]])            # 值 + 梯度 + Hessian 四分量
```

它按末轴长度（即 `diff_n+1`）分支：取 `[0,0]` 为值，取 `[1,0]`、`[0,1]` 为两个一阶偏导，再取 `[2,0]`、`[1,1]`、`[0,2]` 等为二阶偏导组成 Hessian。注意 `[..., [1,0], [0,1]]` 这种花式索引会在末两轴上各取两点，正好挑出梯度向量。

#### 4.2.4 代码实践

**实践目标**：观察 `_all` 函数一次返回「全阶」结果的输出形状，并验证 `diff_n` 如何改变返回结构。

**操作步骤**（**示例代码**，非项目原有）：

```python
import numpy as np
import scipy.special as sc

theta = np.array([0.3, 0.6, 0.9])      # 3 个极角
phi   = np.array([0.0, 1.0, 2.0])      # 3 个方位角

# (1) 全阶返回：n=2, m=0, diff_n=0
y = sc.sph_harm_y_all(2, 0, theta, phi)
print(y.shape)                         # (3, 1, 3)  → (n+1=3, 2m+1=1, 广播=3)

# (2) 改成 m=1，阶数轴 2m+1=3
y2 = sc.sph_harm_y_all(2, 1, theta, phi)
print(y2.shape)                        # (3, 3, 3)  → (n+1, 2m+1, 广播)

# (3) diff_n=1：返回 (值, 梯度) 两个数组（注意是逐点版 sph_harm_y，不是 _all）
val, grad = sc.sph_harm_y(2, 0, theta, phi, diff_n=1)
print(val.shape, grad.shape)           # (3,) (3, 2)  → 梯度对 (θ,φ) 两分量
```

**需要观察的现象**：

- 第 1 步 `sph_harm_y_all(2, 0, ...)` 一次给出 \(l=0,1,2\) 三阶（且 \(m=0\) 一列）共 3 个球谐值，形状 `(3, 1, 3)`，对应文档承诺的 `(n+1, 2m+1, ...)`。
- 第 2 步把 `m` 提到 1，第二轴变成 `2*1+1=3`（对应 \(m=-1,0,1\)）。
- 第 3 步逐点版 `sph_harm_y` 配 `diff_n=1` 返回**两个**数组：值和梯度，梯度末轴长度 2 对应 \((\partial/\partial\theta, \partial/\partial\phi)\)。

**预期结果**：三个 `print` 分别输出 `(3, 1, 3)`、`(3, 3, 3)`、`(3,) (3, 2)`。

> 说明：第 1 步 `m=0` 时 `2m+1=1`，阶数轴退化为 1，看起来「只有 m=0 一列」——若想直观看到全部 (l,m) 组合，建议同时跑第 2 步 `m=1`。

#### 4.2.5 小练习与答案

**练习 1**：`legendre_p_all(5, z, diff_n=2)` 的输出形状是什么（设 `z` 形状为 `(4,)`）？

**参考答案**：`(3, 6, 4)`。即 `(diff_n+1, n+1, ...z的形状...) = (3, 6, 4)`。第一轴 3 = 值/一阶导/二阶导，第二轴 6 = \(P_0..P_5\)，末轴 4 = `z`。

**练习 2**：为什么 `sph_harm_y` 在 `force_complex_output=True` 下，即便输入 `theta, phi` 都是实数，输出仍是复数？

**参考答案**：因为球谐定义里含因子 \(e^{im\phi}\)，结果恒为复数（[_multiufuncs.py:580-583](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_multiufuncs.py#L580-L583)）。`__call__` 在 [_multiufuncs.py:127-129](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_multiufuncs.py#L127-L129) 用 `np.result_type(1j, dtype)` 把预分配数组强提升为复数，否则 gufunc 往实数组里写复数结果会丢虚部。

---

### 4.3 gufunc 支撑：_special_ufuncs 与 _gufuncs 的分工

#### 4.3.1 概念说明

本模块顶部 [`_multiufuncs.py:7-11`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_multiufuncs.py#L7-L11) 的两组导入，揭示了 8 个函数的两个来源：

```python
from ._special_ufuncs import (legendre_p, assoc_legendre_p,
                              sph_legendre_p, sph_harm_y)            # 逐点 4 个
from ._gufuncs import (legendre_p_all, assoc_legendre_p_all,
                       sph_legendre_p_all, sph_harm_y_all)           # 全阶 4 个
```

这是两条由 u3-l4/u8-l3 介绍过的「C++ 直注册路径」生成的扩展模块，分工清晰：

- **`_special_ufuncs`**：装「逐点」版本（不带 `_all`）。给定具体的 \((n, m, z,\theta,\phi)\)，算出**单个**函数值（及导数）。底层签名如 `legendre_p` 的 `(),()->(k)`：两个标量输入（`n` 与 `z`）+ 长度 `k` 的导数块输出，**没有随 `n` 变化的输出核心维度**，行为接近普通 ufunc，只是多了一个导数块轴。
- **`_gufuncs`**：装「全阶」版本（带 `_all`）。给定标量 `n`，**展开**成 0..n 各阶的一整串结果。底层签名如 `legendre_p_all` 的 `()->(np1,k)`：核心维度 `np1`（= n+1）**随输入 `n` 变化**，这是普通 ufunc 做不到的，必须用 gufunc。

一句话：**「逐点求一个」放 `_special_ufuncs`，「按 n 展开一串」放 `_gufuncs`**。

#### 4.3.2 核心流程

gufunc 与普通 ufunc 的关键差别，在于**输出核心维度的大小如何确定**。普通 ufunc 的输出形状由输入广播决定；gufunc 多了「核心维度」，其大小要么从输入里读，要么由一个回调告诉 NumPy。

`_gufuncs.cpp` 里的 `legendre_p_all` 注册见 [_gufuncs.cpp:138-158](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_gufuncs.cpp#L138-L158)，签名 `"()->(np1,1)"` 中 `np1` 是输出核心维度。它的实际大小（= `n+1`）由 `new_legendre_map_dims` 回调（[_gufuncs.cpp:41](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_gufuncs.cpp#L41)）在运行时给出。`xsf::numpy::gufunc` 把这套机制封进了 C++ 模板，于是能把 `xsf::legendre_p_all`（一个会往 `std::mdspan` 里写一整串结果的 C++ 函数）直接暴露成 Python 可调用的 gufunc。

构建上，这两个扩展模块各自独立编译，见 [meson.build:34-52](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/meson.build#L34-L52)：

```meson
py3.extension_module('_special_ufuncs',
  ['_special_ufuncs.cpp', '_special_ufuncs_docs.cpp', 'sf_error.cc'],
  dependencies: [xsf_dep, np_dep], ...)

py3.extension_module('_gufuncs',
  ['_gufuncs.cpp', '_gufuncs_docs.cpp', 'sf_error.cc'],
  dependencies: [xsf_dep, np_dep], ...)
```

两者源码列表几乎对称（都靠 `xsf_dep` 提供 C++ 内核、靠 `sf_error.cc` 提供错误处理），各自挂一份独立的 `*_docs.cpp`（文档串外置，见 u8-l3）。它们**都不走** `functions.json` → `_generate_pyx.py` 那条声明式生成路径（u3-l1～u3-l3），而是直接在 C++ 里用 `xsf::numpy::ufunc`/`gufunc` 注册——这也是 u3-l4 讲的「两条注册路径」中的 C++ 直注册路径。

#### 4.3.3 源码精读

对比同名的「逐点」与「全阶」注册，差别一目了然。

**逐点** `sph_harm_y`（[_special_ufuncs.cpp:1330-1347](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_special_ufuncs.cpp#L1330-L1347)）签名 `(),(),(),()->(k,k)`：四个标量输入 \((n,m,\theta,\phi)\)，输出 \(k\times k\) 导数块，**无随 `n`/`m` 变化的轴**。

**全阶** `sph_harm_y_all`（[_gufuncs.cpp:282-296](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_gufuncs.cpp#L282-L296)）签名 `(),()->(np1,mpmp1,k,k)`：两个标量 \((\theta,\phi)\)（`n`、`m` 不进核心循环，而是用来定 `np1`/`mpmp1` 的大小），输出含两个**随 `n`、`m` 变化**的核心维度 `np1`(=n+1)、`mpmp1`(=2m+1)，再加 \(k\times k\) 导数块。这两个核心维度的大小由 `sph_harm_map_dims` 回调（[_gufuncs.cpp:51-54](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_gufuncs.cpp#L51-L54)）填充：`new_dims[0] = dims[0]; new_dims[1] = dims[1];`，即把「阶数对」转成实际轴长。

这就是为什么 `sph_harm_y_all(n, m, theta, phi)` 能在 Python 侧一次拿到 `(n+1, 2m+1, ...)` 形状的数组——`(n+1)`、`(2m+1)` 这两根轴是 gufunc 凭空「展开」出来的，而 `MultiUFunc` 那侧的 `resolve_out_shapes`（[_multiufuncs.py:723-731](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_multiufuncs.py#L723-L731)）正好用同样的公式 `(n+1, 2*abs(m)+1, ...)` 预分配好缓冲区，两边对齐。

#### 4.3.4 代码实践

**实践目标**：体会「逐点版」与「全阶版」的输出差异，并验证 `_all` 的结果与逐点逐个算一致。

**操作步骤**（**示例代码**，非项目原有；思路来自 [`tests/test_sph_harm.py:35-48`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/tests/test_sph_harm.py#L35-L48)）：

```python
import numpy as np
import scipy.special as sc

n_max, m_max = 3, 2
theta = np.linspace(0, np.pi, 4)
phi   = np.linspace(0, 2*np.pi, 4)

# (A) 全阶：一次拿全部 (l, m)
y_all = sc.sph_harm_y_all(n_max, m_max, theta, phi)   # 形状 (4, 5, 4) = (n+1, 2m+1, 广播)

# (B) 逐点：手动枚举 (l, m) 再广播
n = np.arange(n_max + 1)[:, None, None]               # 让 n 落在第一轴
m = np.concatenate([np.arange(m_max + 1), np.arange(-m_max, 0)])  # -m..m
m = m[None, :, None]                                  # 让 m 落在第二轴
y_pt = sc.sph_harm_y(n, m, theta, phi)                # 广播成同形状

print(y_all.shape, y_pt.shape)
print(np.allclose(y_all, y_pt, rtol=1e-5))            # True
```

**需要观察的现象**：`(A)` 一次调用就拿到 `(n+1, 2m+1, ...)` 的整片结果；`(B)` 需要手动把 `n`、`m` 摆到对应轴上再靠广播逐点算；两者数值一致。

**预期结果**：两个形状都是 `(4, 5, 4)`，`allclose` 为 `True`。这正说明 `_all` 版本只是把「枚举 + 广播 + 逐点」这一套在 C++ gufunc 内核里更高效地做了一遍。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `sph_harm_y_all` 也放进 `_special_ufuncs` 而不是 `_gufuncs`，会丢失什么能力？

**参考答案**：会无法「按 `n` 展开成 `(n+1,)` 轴」。`_special_ufuncs` 用的是普通 ufunc 机制（输出形状只由输入广播决定），无法凭空多出一根长度等于 `n+1` 的核心维度轴。`_all` 系列必须靠 gufunc 的核心维度 + `map_dims` 回调才能把标量 `n` 展开成数组。

**练习 2**：`_gufuncs.cpp` 里为什么有一组 `legendre_map_dims` / `assoc_legendre_map_dims` / `sph_harm_map_dims` 形形色色的回调？

**参考答案**：因为不同函数的输出核心维度个数和含义不同——`legendre_p_all` 只有一个 `np1` 轴；`assoc_legendre_p_all` 有 `np1`、`mpmp1` 两个轴；`sph_harm_y_all` 也是两个但语义不同（阶 + 阶）。每个回调负责把自己函数的核心维度大小填进 `new_dims`（见 [_gufuncs.cpp:34-58](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_gufuncs.cpp#L34-L58)），让 NumPy 知道输出该分配多大。

---

## 5. 综合实践

把本讲三块知识（MultiUFunc 聚合、多输出语义、gufunc 支撑）串成一个跟踪任务：**追踪一次 `sph_harm_y_all(2, 1, 0.5, 0.3, diff_n=1)` 调用的完整数据流**。

1. **选内核**：`diff_n=1` 经 [_multiufuncs.py:707-715](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_multiufuncs.py#L707-L715) 的 `_override_key` 校验并返回 `1`，`_resolve_ufunc` 据此从 tuple 取出第 1 个 gufunc（即 `_gufuncs.cpp:288-291` 注册的签名 `(),()->(np1,mpmp1,2,2)` 那个）。
2. **算形状**：`resolve_out_shapes`（[_multiufuncs.py:723-731](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_multiufuncs.py#L723-L731)）按 `(n+1, 2m+1, 广播, diff_n+1, diff_n+1)` 算出 `(3, 3, 2, 2)`，`force_complex_output=True` 把 dtype 提成 `complex128`，预分配缓冲。
3. **调内核**：把 `theta=0.5`、`phi=0.3` 喂给选中的 gufunc，它内部对 `n=0..2`、`m=-1..1` 全部算一遍，写入缓冲。
4. **收尾解包**：`finalize_out`（[_multiufuncs.py:734-744](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_multiufuncs.py#L734-L744)）按末轴长度 2 走 `diff_n=1` 分支，把 \(2\times2\) 块拆成「值」与「梯度」，分别返回。

请你在本地实际调用：

```python
import scipy.special as sc
val, grad = sc.sph_harm_y_all(2, 1, 0.5, 0.3, diff_n=1)
print(val.shape, grad.shape)    # 应为 (3, 3) 与 (3, 3, 2)
```

并对照上面四步，解释 `val.shape = (3, 3)`（阶 \(l=0,1,2\) × 阶 \(m=-1,0,1\)）与 `grad.shape = (3, 3, 2)`（多出 2 = 对 \(\theta,\phi\) 的两个偏导）的来历。若运行环境暂未编译 SciPy 源码，可标注「待本地验证」，但请务必先在源码层面把四步对应关系指清楚。

## 6. 本讲小结

- `MultiUFunc` 是一个**纯 Python 中间层**，把「面向用户的一个函数名」背后的一组 ufunc（单个、tuple 按 `diff_n` 索引、或 dict 按 `(norm, diff_n)` 索引）聚合成单一可调用对象；它在 `_resolve_ufunc` 里按 `_key(**kwargs)` 挑内核，绕过了「ufunc 只看 dtype、看不到业务开关」的限制。
- 构造时它会**校验组内所有 ufunc 输入类型一致**（[_multiufuncs.py:38-45](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_multiufuncs.py#L38-L45)），保证这一组内核对外有统一签名、可互换。
- **多输出语义**有两层：`diff_n` 引入「值/梯度/Hessian」导数族（底层用 \(k\times k\) 块编码，`finalize_out` 解包成元组）；`_all` 引入「阶数轴」`(n+1,)` / `(2m+1,)`，输出形状由 `resolve_out_shapes` 动态计算、`moveaxis` 把导数块从末轴搬到首轴。
- **gufunc 支撑**：逐点版 `legendre_p` 等住 `_special_ufuncs`（普通 ufunc）；全阶版 `legendre_p_all` 等必须住 `_gufuncs`（广义 ufunc），因为「按 `n` 展开成长度 `n+1` 的轴」是普通 ufunc 做不到的，需要 gufunc 的输出核心维度 + `map_dims` 回调。
- 这 8 个函数走的是 u3-l4/u8-l3 讲的 **C++ 直注册路径**（`xsf::numpy::ufunc`/`gufunc`），**不经** `functions.json` → `_generate_pyx.py`；文档串外置在 `*_docs.cpp`。
- 最终它们经 [__init__.py:800-801](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/__init__.py#L800-L801) 的 `from ._multiufuncs import *` 提到 `scipy.special` 顶层，并计入 `__all__`（[__init__.py:825](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/__init__.py#L825)）。

## 7. 下一步学习建议

- **深入 C++ 直注册机制**：本讲只在「分工」层面用了 `_special_ufuncs.cpp` / `_gufuncs.cpp` 的注册语句；`xsf::numpy::ufunc` / `gufunc` 模板到底如何把 C++ 函数变成 Python ufunc、`map_dims` 回调如何接入 NumPy 的 gufunc 协议，请接着读 **u8-l3（_special_ufuncs.cpp / _gufuncs.cpp：新的 ufunc 注册路径）**。
- **理解错误如何贯通**：本模块两个扩展都 link 了 `sf_error.cc`，特殊函数出错时如何跨越 C++→Python 边界发告警，见 **u7-l1（sf_error 的 C→Python 桥）**。
- **对比「老」正交多项式接口**：把本讲的 `legendre_p` / `legendre_p_all` 与 u5-l2 的 `eval_legendre` / `roots_legendre` 对照阅读，体会 SciPy 为什么引入这一套新的、带导数与全阶返回的接口（更现代、数值更稳、API 更统一）。
- **自己加一个 `MultiUFunc`**（进阶）：参考 `sph_legendre_p` 的最小模板（单 tuple + `moveaxis`），尝试为一个已有 ufunc 写一个带 `diff_n` 风格开关的薄包装，验证你对 `_override_key` / `_override_finalize_out` 的理解。
