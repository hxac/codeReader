# cython_special.pxd/.pyx：融合类型、nogil 与指针输出

## 1. 本讲目标

本讲进入 U6「cython_special：类型化的 Cython API」的第一讲。学完后你应当能够：

- 说清楚 `cython_special` 提供的函数与其在 `scipy.special` 命名空间里的 ufunc 版本（如 `airy`、`betainc`、`voigt_profile`）在**签名上的本质差异**：一个是「标量 + 指针输出」的 C 函数，另一个是「数组 + 自动 dtype 分发」的 ufunc。
- 看懂 `.pxd` 声明里的**融合类型（fused types）**——`number_t`、`Dd_number_t`、`df_number_t` 等——理解它们如何让一个函数名在编译期展开成多份针对不同数值类型的特化代码。
- 解释 `noexcept nogil` 后缀的含义与代价：为什么这些函数能在「释放 GIL」的热循环里被调用，代价是不能发 Python 告警、只能返回 `nan`。
- 把 `.pxd`（公开声明）与 `.pyx`（实现）对应起来，能从一条声明顺藤摸到它的实现体和底层 C/C++ 内核。

## 2. 前置知识

本讲假设你已经读过：

- **u1-l2 / u1-l3**：知道 `scipy.special` 的源码分三层（Python 包装 → Cython 胶水 → C/C++ 内核），且 `cython_special` 是 7 个编译扩展模块之一。
- **u2-l1**：理解 NumPy ufunc 是什么——按 dtype 分发、必然逐元素、可批量。我们这一讲要反复拿 ufunc 当「对照组」。
- **u3-l2 / u3-l3**：知道 `_ufuncs.pyx` 是由 `_generate_pyx.py` 在构建期生成的（`custom_target('cython_special')`），而 `cython_special.pyx` 是**另一份独立维护的源文件**（见 [meson.build:157-173](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/meson.build#L157-L173)，直接以字面量 `'cython_special.pyx'` 喂给 Cython 生成器，而非取自 `custom_target` 的产物）。两份 `.pyx` 共享同一批底层 C/C++ 内核。

补充几个 Cython 关键词，初学者可能不熟：

| 术语 | 通俗解释 |
|---|---|
| `cdef` | 定义一个**只给 Cython 内部用**的 C 级函数/变量，Python 看不到它。 |
| `cpdef` | 同时生成 C 版本和 Python 版本：既能被 Cython `cimport` 高速调用，也能被 Python 代码调用。 |
| `.pxd` | Cython 的「头文件」，存放对外公开的声明；别的 `.pyx` 通过 `cimport` 读它。 |
| `.pyx` | Cython 的「实现文件」，包含真正的函数体。 |
| 融合类型（fused type） | 一个「类型占位符」，等于一组具体类型的并集；Cython 会为并集中**每一种**类型各编译出一份特化函数。 |
| `nogil` | 「可以在不持有全局解释器锁（GIL）时调用」。意味着它不会碰 Python 对象，因此能在多线程并行段里调用。 |
| `noexcept` | Cython 3.x 关键字，声明该函数**不会**以「抛 Python 异常」的方式向调用者报告失败。 |

## 3. 本讲源码地图

本讲聚焦三个文件，它们是一组「头文件 + 实现 + 桩」的组合：

| 文件 | 角色 | 本讲怎么用 |
|---|---|---|
| [cython_special.pxd](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/cython_special.pxd) | 公开声明（Cython 头文件）。定义所有融合类型，并列出全部可 `cimport` 的函数签名。 | 看签名差异、看融合类型定义的主战场。 |
| [cython_special.pyx](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/cython_special.pyx) | 实现文件。顶部一段说明性文档串定义「指针输出 / 直接返回 / nan 报错」三条约定；其后是融合类型分发逻辑与对底层 `xsf_wrappers.h` 的 `extern` 声明。 | 看融合分发如何落到具体 C/C++ 内核，看 `_pywrap` 如何把多输出函数包回 Python。 |
| [cython_special.pyi](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/cython_special.pyi) | Python 类型桩。仅一句 `def __getattr__(name) -> Any: ...`，因为该模块主要供 Cython `cimport`，而非 Python 直接 `import`。 | 说明「这不是给 Python 调用者用的接口」。 |

辅助参照：[meson.build](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/meson.build)（看 `cython_special` 扩展模块如何编译）、[_ufuncs.pyi](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_ufuncs.pyi)（看同名函数的 ufunc 身份，作对照组）。

## 4. 核心概念与源码讲解

### 4.1 类型化标量签名：cython_special 与 ufunc 的本质差异

#### 4.1.1 概念说明

你在 u2-l1 已经知道：`scipy.special` 命名空间里几乎所有函数都是 NumPy ufunc——它们接受数组、按 dtype 自动选 loop、返回同形状数组。这套机制对「批量算一堆点」很友好，但有一类场景它服务不好：

> 你在自己写的 Cython/C++ 代码里，正在一个紧凑的 `for` 循环中逐标量地算几千次 `gamma(x)` 或 `airy(x)`，每次只算一个数。如果每次都要绕道「构造 NumPy 数组 → 触发 ufunc 分发 → 取回单个标量」，开销会被 Python 对象和 GIL 吃掉一大半。

`cython_special` 就是为这个场景准备的「标量直通版」：它提供与 ufunc **同名**、但签名是**纯 C 标量**的函数，可以像调用普通 C 函数那样直接调用，没有 Python 对象参与。代价是它**只算一个标量**，要批量就得自己在循环里调。

#### 4.1.2 核心流程：两种调用路径对照

```
Python 侧批量调用                          Cython 侧标量调用
─────────────────────                      ─────────────────────────────
special.airy(x_array)                      cimport scipy.special.cython_special as cs
   │  ufunc: 按 dtype 选 loop              cs.airy(x_scalar, &Ai, &Aip, &Bi, &Bip)
   │  返回 4 个数组 (Ai,Aip,Bi,Bip)            │  C 函数: 直接写 4 个 double
   └  可发 SpecialFunctionWarning              └  仅返回 nan 表示错误,不发警告
```

两条路径最终都落到**同一批 C/C++ 内核**（`special_airy`、`xsf_*` 等，见 [cython_special.pyx:1127](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/cython_special.pyx#L1127) 起的 `cdef extern from r"xsf_wrappers.h"` 块），区别只在「外面那层壳」：ufunc 壳面向数组和 Python，cython_special 壳面向标量和 C。

#### 4.1.3 源码精读

**先看约定。** [cython_special.pyx:13-16](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/cython_special.pyx#L13-L16) 顶部文档串白纸黑字写了两条规则：

```
- If a function's Python counterpart returns multiple values, then the
  function returns its outputs via pointers in the final arguments.
- If a function's Python counterpart returns a single value, then the
  function's output is returned directly.
```

翻译：Python 侧返回多个值的，C 版本把输出放在末尾的**指针参数**里；Python 侧只返回一个值的，C 版本**直接用 `return`**。

**对照三组同名函数。**

1. `voigt_profile`——单返回值，最简单。[cython_special.pxd:29](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/cython_special.pxd#L29) 声明：

   ```
   cpdef double voigt_profile(double x0, double x1, double x2) noexcept nogil
   ```

   三个 `double` 进、一个 `double` 出，直接 `return`。实现 [cython_special.pyx:1708-1710](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/cython_special.pyx#L1708-L1710) 只有一行，转发给底层 `xsf_voigt_profile`（其 `extern` 声明见 [cython_special.pyx:1318](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/cython_special.pyx#L1318)）：

   ```
   cpdef double voigt_profile(double x0, double x1, double x2) noexcept nogil:
       """See the documentation for scipy.special.voigt_profile"""
       return xsf_voigt_profile(x0, x1, x2)
   ```

   对照 [_ufuncs.pyi:513](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_ufuncs.pyi#L513)：`voigt_profile: np.ufunc`——同一个名字，ufunc 版本面向数组。

2. `betainc`——单返回值，但带**融合类型** `df_number_t`（float/double）。[cython_special.pxd:44](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/cython_special.pxd#L44)：

   ```
   cpdef df_number_t betainc(df_number_t x0, df_number_t x1, df_number_t x2) noexcept nogil
   ```

   对照 [_ufuncs.pyi:305](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_ufuncs.pyi#L305)：`betainc: np.ufunc`。ufunc 版本按输入 dtype 在运行时挑 loop；cython_special 版本则在编译期（融合类型特化，见 4.2）就定好调 float 内核还是 double 内核。

3. `airy`——**多返回值**，是本节的重点。[cython_special.pxd:31](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/cython_special.pxd#L31)：

   ```
   cdef void airy(Dd_number_t x0, Dd_number_t *y0, Dd_number_t *y1, Dd_number_t *y2, Dd_number_t *y3) noexcept nogil
   ```

   注意四件事：
   - 返回类型是 `void`（什么都不返回）；
   - 4 个输出 `Ai, Aip, Bi, Bip` 通过**末尾 4 个指针参数** `*y0..*y3` 写出；
   - 它是 `cdef`（不是 `cpdef`），意味着 Python 侧不能直接调它；
   - 输入和输出都用融合类型 `Dd_number_t`（double / double complex），实现一份代码同时服务实/复数。

   对照 [_ufuncs.pyi:292](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_ufuncs.pyi#L292)：`airy: np.ufunc`——ufunc 版本接受数组、返回 4 元组数组。

   实现 [cython_special.pyx:1716-1740](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/cython_special.pyx#L1716-L1740) 展示了「写指针」的写法（精简版）：

   ```
   cdef void airy(Dd_number_t x0, Dd_number_t *y0, ...) noexcept nogil:
       cdef npy_cdouble tmp0, tmp1, tmp2, tmp3
       if Dd_number_t is double:
           special_airy(x0, y0, y1, y2, y3)        # 实数: 直接写调用者的指针
       elif Dd_number_t is double_complex:
           special_cairy(..., &tmp0, &tmp1, &tmp2, &tmp3)  # 复数: 先写临时变量
           y0[0] = ...double_complex_from_npy_cdouble(tmp0)  # 再转换写回
       ...
   ```

   实数分支直接把调用者传进来的指针透传给 C 内核 `special_airy`（`extern` 声明见 [cython_special.pyx:1179-1180](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/cython_special.pyx#L1179-L1180)）；复数分支因为底层 C 内核用的是 NumPy 的 `npy_cdouble`、而 Cython 侧用的是 `double complex`，需要先写到临时变量再做类型桥接（见 4.3）。

**为何多返回值用指针而不是返回元组？** 三条理由，都源于「这是一个 C 函数、且要在 nogil 下跑」：

1. C 函数只能 `return` 一个值，无法返回元组；
2. 在 nogil 上下文里**没有 Python 元组对象**可构造（构造 Python 对象必须拿 GIL）；
3. 指针是零开销的：调用者在自己的栈/堆上声明好变量，把地址传进去，被调函数直接写入，没有拷贝、没有引用计数。这正是热循环想要的。

**`_pywrap` 桥：多输出函数怎么被 Python 调到？** 因为 `airy` 是 `cdef void`、Python 看不见它，所以文件里另配了一个普通 `def` 包装 [cython_special.pyx:1742-1748](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/cython_special.pyx#L1742-L1748)：

```
def _airy_pywrap(Dd_number_t x0):
    cdef Dd_number_t y0, y1, y2, y3
    airy(x0, &y0, &y1, &y2, &y3)   # 取地址传给 cdef 函数
    return y0, y1, y2, y3          # 再组成 Python 元组返回
```

它证明了两套世界的衔接：在 `def` 函数里（持有 GIL）声明标量、取地址 `&y0` 调 `cdef void` 内核、最后才组元组。注意命名空间里你用的 `special.airy` 并不是这个 `_airy_pywrap`——那个走的是 ufunc；`_pywrap` 主要服务于内部测试与类型对接。

#### 4.1.4 代码实践

> 实践目标：亲手对比 `airy`、`betainc`、`voigt_profile` 三个函数在 `cython_special.pxd`（标量）与 `_ufuncs.pyi`（ufunc）里的签名差异，并解释 `airy` 为何用指针输出。

操作步骤（纯源码阅读，无需编译）：

1. 打开 [cython_special.pxd](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/cython_special.pxd)，定位三行声明：
   - `voigt_profile`（第 29 行）
   - `airy`（第 31 行）
   - `betainc`（第 44 行）
2. 打开 [_ufuncs.pyi](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_ufuncs.pyi)，找到同名三行：`airy: np.ufunc`（292 行）、`betainc: np.ufunc`（305 行）、`voigt_profile: np.ufunc`（513 行）。
3. 做一张对照表，按「输入形态 / 输出方式 / 错误反馈」三栏填写。

需要观察的现象与预期结果：

| 函数 | cython_special 签名 | ufunc 身份 | 输出方式（cython_special） |
|---|---|---|---|
| `voigt_profile` | `cpdef double (double,double,double) noexcept nogil` | `np.ufunc` | 单值，直接 `return` |
| `betainc` | `cpdef df_number_t (df_number_t ×3) noexcept nogil` | `np.ufunc` | 单值，融合类型 `return` |
| `airy` | `cdef void (Dd_number_t, Dd_number_t *×4) noexcept nogil` | `np.ufunc` | **4 值经指针写出** |

结论应当能用自己的话答出：`airy` 用指针而非返回元组，是因为它是 `cdef void` 的 C 函数、要在 nogil 下运行，既无法 `return` 多值、也不能在无 GIL 时构造 Python 元组；指针是零开销直写调用者内存的唯一可行方案。

#### 4.1.5 小练习与答案

**练习 1**：在 `cython_special.pxd` 里，哪些函数是 `cdef void ... *` 形态？它们有什么共同特征？
**答**：如 `airy`、`airye`、`ellipj`、`fresnel`、`itairy`、`sici`、`shichi`、`pbdv` 等（见 [cython_special.pxd:31-32,70,113,144,215-217,237-238](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/cython_special.pxd#L31-L32)）。共同特征：它们的 Python 对应函数都返回**多个值**，因而按约定改用末尾指针输出、且必须是 `cdef`（非 `cpdef`）、返回 `void`。

**练习 2**：`betainc` 的 cython_special 版本是 `cpdef`，`airy` 是 `cdef`，为什么这个差异恰好对应「单返回值 vs 多返回值」？
**答**：单返回值函数可以 `return` 一个标量，天然能作为 Python 可调用对象暴露，故用 `cpdef`；多返回值函数返回 `void`、靠指针写值，无法直接做 Python 函数（Python 拿不到指针写出的结果），故只能 `cdef`，需要时再配 `_pywrap` 包装。

---

### 4.2 融合类型（fused types）：编译期多态

#### 4.2.1 概念说明

`cython_special` 想做到「函数名和 Python 侧一模一样」。但 Python 侧的 `erf` 既能吃 `float` 又能吃 `complex`；C 里 `double erf(double)` 和 `double complex cerf(double complex)` 是两个完全不同的函数。怎么用一个名字覆盖多种类型？

Cython 的答案是**融合类型（fused type）**：你声明一个「类型占位符」等于一组具体类型的并集，Cython 就为并集里**每一种类型各生成一份特化函数**，函数体里用 `if 占位符 is 某类型:` 做静态分支（这些分支在编译期就被消除，运行时零开销）。从外部看名字只有一个，从编译产物看是多个特化版本。

这与 ufunc 的「类型分发」形似神不同：ufunc 在**运行时**根据输入数组的 dtype 选 loop；融合类型在**编译期**（你 `cimport` 并指定具体类型时）就定好了调哪个特化版本。

#### 4.2.2 核心流程：一份源码 → 多份特化

```
.pxd 声明:  cpdef number_t spherical_jn(Py_ssize_t n, number_t z, ...) nogil
                         │  number_t ∈ {double complex, double}
                         ▼
.pyx 实现:  if number_t is double:      →  special_sph_bessel_j(n, z)        // 特化 A
            else:                       →  special_csph_bessel_j(n, 复数转换) // 特化 B
                         │
                         ▼  Cython 编译器为 {double, double complex} 各产出一份机器码
            调用方:  cs.spherical_jn(n, <double>x)        命中特化 A
                     cs.spherical_jn(n, <double complex>x) 命中特化 B
```

#### 4.2.3 源码精读

**五个融合类型，按「支持哪些数值类型」分工。** 全部定义在 [cython_special.pxd:2-4,11-27](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/cython_special.pxd#L2-L27)：

| 融合类型 | 成员 | 典型用途 | 例子 |
|---|---|---|---|
| `number_t` | `double complex`, `double` | 球贝塞尔函数（实/复同形） | `spherical_jn` |
| `Dd_number_t` | `double complex`, `double` | 大多数「实+复」特殊函数 | `airy`, `erf`, `gamma`, `jv` |
| `df_number_t` | `double`, `float` | 需要 float32 双胞胎的 Boost 函数 | `betainc`, `chndtr` |
| `dfg_number_t` | `double`, `float`, `long double` | 逻辑/概率便利函数 | `expit`, `logit`, `log_expit` |
| `dlp_number_t` | `double`, `long`, `Py_ssize_t` | 整数阶/计数参数 | `bdtr`, `kn`, `yn` |

注意 `number_t` 与 `Dd_number_t` 成员完全相同——它们只是**语义分组**：`number_t` 专留给 4 个球贝塞尔函数（声明在最前，见 [cython_special.pxd:6-9](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/cython_special.pxd#L6-L9)），其余实/复函数统一用 `Dd_number_t`。

**实/复分发示例：`spherical_jn`。** [cython_special.pyx:3627-3638](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/cython_special.pyx#L3627-L3638)：

```
cpdef number_t spherical_jn(Py_ssize_t n, number_t z, bint derivative=0) noexcept nogil:
    if derivative:
        if number_t is double:
            return special_sph_bessel_j_jac(n, z)
        else:
            return ...double_complex_from_npy_cdouble(special_csph_bessel_j_jac(n, ...))
    if number_t is double:
        return special_sph_bessel_j(n, z)
    else:
        return ...double_complex_from_npy_cdouble(special_csph_bessel_j(n, ...))
```

要点：
- `if number_t is double:` 是**编译期**判断，Cython 只会把命中的那支编进特化版本，另一支被丢弃——运行时没有分支开销。
- 实数分支直接返回 C 内核结果；复数分支多一步 `npy_cdouble` ↔ `double complex` 的桥接（见 4.3）。
- `derivative` 是普通 `bint`（布尔），不是融合类型——它的两个分支在运行时确实存在，但代价极小。

**float/double 分发示例：`betainc`。** [cython_special.pyx:1844-1849](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/cython_special.pyx#L1844-L1849)：

```
cpdef df_number_t betainc(df_number_t x0, df_number_t x1, df_number_t x2) noexcept nogil:
    if df_number_t is float:
        return (<float(*)(float,float,float) noexcept nogil>..._export_ibeta_float)(x0, x1, x2)
    elif df_number_t is double:
        return (<double(*)(double,double,double) noexcept nogil>..._export_ibeta_double)(x0, x1, x2)
```

这里两支分别调 Boost 导出的 float 内核 `_export_ibeta_float` 和 double 内核 `_export_ibeta_double`——正是 u3-l4 讲过的「Boost 提供 float/double 双内核」在 Cython 侧的落点。注意 `<float(*)(...)>` 这种**函数指针强转**：导出符号本身是 `void*`（见 u3-l2 的 `_ufuncs_cxx` 导出机制），这里把它强转回具体类型的函数指针再调用。

**整数阶分发示例：`bdtr`。** [cython_special.pyx:1784-1791](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/cython_special.pyx#L1784-L1791) 用 `dlp_number_t`，按 `long`/`Py_ssize_t` 调 xsf 内核、按 `double` 走 `_legacy` 的 `_unsafe` 包装——融合类型同样能表达「整数走新路径、浮点走老路径」这种调度。

#### 4.2.4 代码实践

> 实践目标：体会「声明一条、生成多份」的融合类型威力，并理解 `is` 判断是编译期而非运行期。

操作步骤（源码阅读 + 一段 Python 验证）：

1. 在 [cython_special.pxd](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/cython_special.pxd) 里数一下 `Dd_number_t` 被多少个函数用到（提示：从 `airy` 到 `yve`，覆盖了绝大多数实/复特殊函数）。
2. 打开 [cython_special.pyx:3627-3638](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/cython_special.pyx#L3627-L3638) 的 `spherical_jn`，确认实数分支调 `special_sph_bessel_j`、复数分支调 `special_csph_bessel_j`（这两个 `extern` 声明在 [cython_special.pyx:1252-1253](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/cython_special.pyx#L1252-L1253)）。
3. 用一段 Python 直观感受「同名、多类型」：在已装 SciPy 的环境运行：

   ```python
   import numpy as np, scipy.special as sc
   print(sc.spherical_jn(0, 1.0))                 # 实数 → double 特化
   print(sc.spherical_jn(0, 1.0+0j))              # 复数 → double complex 特化
   print(sc.erf(1.0), sc.erf(1.0+0j))             # erf 同名吃实/复
   ```

需要观察的现象：实数与复数输入都返回正确结果，且复数输入返回 `complex` 类型。
预期结果：实数路径与复数路径数值一致（虚部近 0 时），证明两份特化代码各司其职。（注意：你从 Python 调到的是 ufunc 版本，它内部同样按 dtype 分发；这里只是借它体会「一个名字覆盖多类型」的语义，真正的编译期融合只在 Cython `cimport` 时发生。）

#### 4.2.5 小练习与答案

**练习 1**：`df_number_t` 和 `Dd_number_t` 都含 `double`，为什么 `betainc` 用前者、`airy` 用后者？
**答**：`betainc` 的底层 Boost 内核提供 `float`+`double` 双精度但**不支持复数**，故用 `df_number_t`（double/float）；`airy` 的底层 xsf 内核同时有实数版 `special_airy` 和复数版 `special_cairy`，但不需要 float32 版，故用 `Dd_number_t`（double/double complex）。融合类型成员集精确反映了「底层提供哪几种类型内核」。

**练习 2**：函数体里的 `if number_t is double:` 和普通 `if x > 0:` 有什么本质区别？
**答**：前者是**编译期**类型判断——Cython 只为实际出现的类型组合生成代码，命中分支被内联、未命中分支被丢弃，运行时零开销；后者是**运行期**数值判断，每次调用都要执行比较。

---

### 4.3 `noexcept nogil`：释放 GIL 的安全契约

#### 4.3.1 概念说明

`cython_special.pxd` 里**每一条**声明都以 `noexcept nogil` 结尾。这两个关键字合起来是一道「安全契约」，回答一个问题：**这个函数能不能在多线程并行段（不持有 GIL）里被安全调用？**

- `nogil`：声明「我可以在不持有 GIL 时运行」。意味着函数体内**绝不碰 Python 对象**——不创建 list/tuple、不调 Python 函数、不抛 Python 异常。只有这样，调用方才能在进入 `with nogil:` 块后多线程（如 OpenMP `prange`）并行地反复调用它，而不被 GIL 串行化。
- `noexcept`：Cython 3.x 关键字，声明「我不会以抛 Python 异常的方式失败」。配合 `nogil` 是必然的——抛异常需要 GIL。

代价很直接，[cython_special.pyx:22-26](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/cython_special.pyx#L22-L26) 明说了：

```
Functions can indicate an error by returning ``nan``; however they
cannot emit warnings like their counterparts in ``scipy.special``.
```

翻译：出问题时只能返回 `nan`，**不能像 ufunc 那样发 `SpecialFunctionWarning`**。因为发告警要走 Python 信号机制、要拿 GIL，这在 `nogil` 函数里做不到。（对比 u2-l3 讲过的 ufunc 错误处理：ufunc 经 `sf_error` 跨 GIL 触发告警；cython_special 主动放弃了这条能力，换取可并行。）

#### 4.3.2 核心流程：nogil 函数的「无 Python 对象」纪律

```
调用方 (用户 .pyx):
    from scipy.special.cython_special cimport gamma
    cdef double xs[10000]; ...
    with nogil:                      # 释放 GIL
        for i in prange(10000):      # 多线程并行
            xs[i] = gamma(xi[i])     # ← 必须是 nogil 函数; 内部绝不碰 Python 对象
                              │
                              ▼
    gamma 函数体:
        return xsf_gamma(x)          # 纯 C 调用, 无 Python 对象
        # 失败时: return NAN  (不能 PyErr_*, 不能 warnings.warn)
```

要让这条链成立，函数体里凡是「实↔复」的类型桥接也都必须是无 Python 对象的值语义操作——这就是 `_complexstuff` 工具存在的原因。

#### 4.3.3 源码精读

**`_complexstuff` 桥接器。** [cython_special.pyx:1119](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/cython_special.pyx#L1119) 引入：

```
from . cimport _complexstuff
```

它在 `airy` 的复数分支（[cython_special.pyx:1725-1729](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/cython_special.pyx#L1725-L1729)）里被反复使用：

```
special_cairy(_complexstuff.npy_cdouble_from_double_complex(x0), &tmp0, ...)
y0[0] = _complexstuff.double_complex_from_npy_cdouble(tmp0)
```

为什么需要它？因为 Cython 侧的复数类型是 C99 的 `double complex`，而底层 xsf/NumPy 内核用的是 NumPy 自定义的 `npy_cdouble`（一个 `{double real, imag;}` 结构体，二进制布局未必与 C99 `double complex` 一致）。两者不能直接互传，必须显式转换。这两个转换函数是纯 C 的值拷贝——不开 Python 对象、不拿 GIL，所以 `airy` 整个函数能保持 `nogil`。这是「无 Python 对象」纪律的一个典型落地。

**`wrap_PyUFunc_getfperr`：一个「看似要拿 NumPy 状态」的 nogil 函数。** [cython_special.pyx:1112-1117](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/cython_special.pyx#L1112-L1117)：

```
cdef public int wrap_PyUFunc_getfperr() noexcept nogil:
    """
    Call PyUFunc_getfperr in a context where PyUFunc_API array is initialized;
    this avoids messing with the UNIQUE_SYMBOL #defines
    """
    return PyUFunc_getfperr()
```

它把 NumPy 的浮点错误检查函数 `PyUFunc_getfperr()`（`extern` 自 [cython_special.pyx:1109-1110](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/cython_special.pyx#L1109-L1110)）包成一个 `cdef public ... noexcept nogil` 函数，供其它模块在 nogil 上下文里调用。`cdef public` 的 `public` 表示让别的 Cython 模块能 `cimport` 它。它说明：nogil 不等于「什么 C 函数都能调」，而是「只要被调用的东西本身也是 nogil 安全、且 NumPy 的 C-API 表已初始化」就行。

**最简对照：`voigt_profile`。** [cython_special.pyx:1708-1710](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/cython_special.pyx#L1708-L1710) 是理解 `noexcept nogil` 最好的「最小例子」：纯 `double` 进出、无融合分发、无复数桥接、无指针——一行 `return xsf_voigt_profile(...)` 就完事。它和 `airy`（融合 + 复数桥接 + 4 指针输出）形成鲜明对照，但两者都挂着同一个 `noexcept nogil` 后缀，因为它们都满足同一条契约：不碰 Python 对象、失败只返 `nan`。

**编译侧的呼应。** [meson.build:157-173](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/meson.build#L157-L173) 把 `cython_special.pyx` 编成扩展模块，源码列表里既有 `xsf_wrappers.cpp`（C++ 内核包装）、`sf_error.cc`（错误码）、`dd_real_wrappers.cpp`，也 `link_with: cdflib_lib`——这些 C/C++ 件正是上面那些 `extern` 声明所指向的真实符号来源。`cpp_args: ['-DSP_SPECFUN_ERROR']` 则是给 C++ 内核设定错误处理宏（与 u7 讲的 `sf_error` 体系衔接）。

#### 4.3.4 代码实践

> 实践目标：从「错误反馈」角度实证 `noexcept nogil` 的代价——同样的「非法输入」，ufunc 版本会发告警/可配置成抛异常，cython_special 版本只能静默返回 `nan`。

操作步骤（源码阅读 + Python 验证）：

1. 阅读 [cython_special.pyx:22-26](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/cython_special.pyx#L22-L26) 的错误处理说明，确认「只能返回 nan、不能发警告」。
2. 在已装 SciPy 的环境运行：

   ```python
   import warnings, numpy as np, scipy.special as sc
   # ufunc 版本: domain error 会发告警, 且可被 errstate 改成抛异常
   with warnings.catch_warnings(record=True) as w:
       warnings.simplefilter("always")
       print("ufunc spence(-1) =", sc.spence(-1))      # 返回 nan, 但发 SpecialFunctionWarning
       print("  warnings:", len(w))
   # 对照: 假如有 cython_special 标量版, 它只会 return NAN, 没有任何告警通道
   ```

   > 说明：`cython_special` 主要供 Cython `cimport`，不直接暴露给 Python，所以这里用 ufunc 版本 `spence` 来反衬「它能发告警」这件事；cython_special 版本的对应函数（见 [cython_special.pxd:242](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/cython_special.pxd#L242)）在同样输入下只会返 `nan` 且静默。

需要观察的现象：`sc.spence(-1)` 返回 `nan`，同时触发 1 条告警。
预期结果：证实「ufunc 有告警通道、cython_special 没有」的差异，这正是 `nogil` 契约的代价。
如果无法本地运行：标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `cython_special` 的所有函数都标注 `noexcept`，而不是默认的「可能抛异常」？
**答**：因为它们都标了 `nogil`，要在无 GIL 的多线程段里调用。抛 Python 异常必须拿 GIL（异常对象是 Python 对象），与 `nogil` 直接冲突。`noexcept` 显式声明「我不会走异常通道」，让 Cython 编译器与调用方都确信它可以在 nogil 下运行。

**练习 2**：如果某个底层 C++ 内核（如 Boost）在数值错误时会抛 C++ 异常，直接把它接进 `nogil` 函数会有什么问题？
**答**：C++ 异常穿越 `noexcept` 边界会触发 `std::terminate`（程序直接崩），而且即便不崩，处理异常也需要栈展开、可能与 GIL 状态冲突。这正是 u8-l2 要讲的——Boost 接入时用自定义 `user_*_error` 策略把 C++ 异常**拦截**下来、转成 Python 信号或返回 `nan`，而不是让异常穿越 `nogil` 边界。

---

## 5. 综合实践

把本讲三个最小模块（类型化标量签名、融合类型、nogil 安全）串起来，完成一次「端到端调用链追踪」：

**任务**：选定 `spherical_yn`（第二类球贝塞尔函数），完成下面的追踪表。所有答案都能在本讲引用的源码里找到。

| 追踪环节 | 你要回答的问题 | 指路线索 |
|---|---|---|
| ① 声明 | 它在 `.pxd` 里是 `cpdef` 还是 `cdef`？返回类型是 `void` 还是融合类型？为什么？ | [cython_special.pxd:7](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/cython_special.pxd#L7) |
| ② 融合分发 | 它用哪个融合类型？实数分支调哪个 C 内核、复数分支调哪个？ | 实现 [cython_special.pyx:3640-3651](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/cython_special.pyx#L3640-L3651)；内核 `extern` 在 [cython_special.pyx:1258-1259](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/cython_special.pyx#L1258-L1259) |
| ③ `derivative` | `bint derivative` 参数是编译期分支还是运行期分支？为什么它不能也做成融合类型？ | 看实现里的 `if derivative:` |
| ④ nogil | 它是 `noexcept nogil` 吗？复数分支里的 `_complexstuff.npy_cdouble_from_double_complex` 为何不破坏 nogil？ | [cython_special.pyx:1119](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/cython_special.pyx#L1119) |
| ⑤ 对照 | 它的 ufunc 版本签名是什么？为什么 ufunc 版本能发告警而它不能？ | [_ufuncs.pyi:282-283](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_ufuncs.pyi#L282-L283)（注意 `_spherical_jn` / `_spherical_jn_d` 两个底层 ufunc） |

**参考答案要点**：
- ① `cpdef number_t spherical_yn(Py_ssize_t, number_t, bint=*) noexcept nogil`——`cpdef` 因为单返回值；返回融合类型 `number_t`（double/double complex）。
- ② 用 `number_t`；实数调 `special_sph_bessel_y`，复数调 `special_csph_bessel_y`（经 `npy_cdouble` 桥接）。
- ③ `derivative` 是运行期布尔分支（每次调用都判断），不能做融合类型——融合类型表达的是「数值类型」，布尔开关是「业务模式」，二者正交；且 `derivative` 的两支都还要再按 `number_t` 展开，做成融合会让组合爆炸。
- ④ 是 `noexcept nogil`；`_complexstuff.*` 是纯 C 值拷贝，不开 Python 对象、不拿 GIL。
- ⑤ ufunc 版本是 `np.ufunc`（实/复与导数各一份 `_spherical_jn`/`_spherical_jn_d`），按 dtype 运行时分发；ufunc 经 `sf_error` 体系（u7）跨 GIL 触发告警，而 cython_special 版本受 `nogil` 约束只能返 `nan`。

## 6. 本讲小结

- `cython_special` 提供 `scipy.special` 函数的**标量、类型化 C 版本**，与面向数组的 ufunc 版本同名但签名迥异：ufunc 吃数组、按 dtype 运行时分发；cython_special 吃 C 标量、按融合类型编译期特化。
- 签名约定（[cython_special.pyx:13-16](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/cython_special.pyx#L13-L16)）：单返回值→直接 `return`、`cpdef`；多返回值→末尾指针输出、`cdef void`（如 `airy` 的 4 个指针）。指针输出是因为 C 只能返回一个值、且 nogil 下无法构造 Python 元组。
- **融合类型**（`number_t`/`Dd_number_t`/`df_number_t`/`dfg_number_t`/`dlp_number_t`）让一个函数名在编译期展开成多份针对不同数值类型的特化代码，函数体里 `if X is double:` 是编译期分支、运行时零开销；成员集精确反映底层提供哪几种类型内核。
- `noexcept nogil` 是「可在无 GIL 多线程段调用」的安全契约，要求函数体绝不碰 Python 对象；`_complexstuff` 的纯 C 值拷贝桥接为此而存在。
- 代价是错误处理被削弱：只能返回 `nan`，**不能像 ufunc 那样发 `SpecialFunctionWarning`**（[cython_special.pyx:22-26](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/cython_special.pyx#L22-L26)）。
- `.pxd` 是公开声明、`.pyx` 是实现；多输出函数还需配 `_pywrap`（如 `_airy_pywrap`）才能从 Python 侧触达；`cython_special.pyi` 仅一个 `__getattr__` 桩，说明它本就不是给 Python 直接用的。

## 7. 下一步学习建议

- **u6-l2（下一讲）**：动手实战——写一个自己的最小 `.pyx`，`from scipy.special.cython_special cimport gamma`，在 `with nogil:` + `prange` 循环里逐标量调用，并与 `special.gamma(numpy 数组)` 的 ufunc 批量版比耗时，把本讲的「标量直通 vs 数组分发」落到性能数字上。
- **横向衔接 u7**：本讲反复提到「cython_special 不能发告警」，u7 会从 C 层 `sf_error.h/.cc` 讲清楚 ufunc 版本**是怎么**隔着 GIL 触发 `SpecialFunctionWarning` 的，正好补上这条被刻意放弃的能力的来龙去脉。
- **深挖后端 u8-l1/u8-l2**：本讲里 `special_airy`、`xsf_voigt_profile`、`_export_ibeta_double` 这些符号从哪来？u8 会带你进入 `xsf_wrappers.cpp`（`extern "C"` + 复数桥接）与 `boost_special_functions.h`（policy 改造 + 双内核）的实现现场。
- **建议精读源码**：把 [cython_special.pyx:1716-1748](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/cython_special.pyx#L1716-L1748)（`airy` + `_airy_pywrap`）和 [cython_special.pyx:3627-3678](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/cython_special.pyx#L3627-L3678)（四个球贝塞尔函数）通读一遍，它们集中体现了「融合分发 + 复数桥接 + 指针/返回双轨 + nogil」的全部要点。
