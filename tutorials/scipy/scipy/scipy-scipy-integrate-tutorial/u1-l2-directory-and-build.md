# 目录结构、子包组织与构建方式

## 1. 本讲目标

本讲带你从「硬盘上的文件」角度看懂 `scipy.integrate`。学完后你应该能够：

- 说出 `integrate` 目录在物理上分成哪几层，每一层各负责什么。
- 区分四类文件：**纯 Python 模块**、**C 扩展模块**、**子包**、**加载桩（stub）**。
- 读懂 `meson.build` 里 `py3.extension_module`、`py3.install_sources`、`subdir` 三种指令各做什么。
- 自己动手在 `meson.build` 里查出某个扩展模块是由哪些 C 源文件编译而来的，并画出对照表。

## 2. 前置知识

本讲承接 [u1-l1 项目定位与全景](u1-l1-project-overview.md)。你已经知道：`integrate` 的公开 API 都从 `__init__.py` 导出，这些名字分别来自 `_quadrature.py`、`_quadpack_py.py`、`_ivp/` 等模块。本讲要回答的是一个更底层的问题：**这些 `.py` 文件和 `.c` 文件在硬盘上是怎么摆放的？SciPy 又是怎么把它们「装配」成一个可以 `import` 的 `scipy.integrate` 包的？**

回答这个问题，需要先建立三点直觉：

- **为什么需要「构建」**：Python 代码（`.py`）可以直接被 `import`，但 SciPy 里有大量用 C / Fortran 写的数值核心（追求运行速度）。这些 C 代码**不能**直接被 Python 导入，必须先用编译器编译成「扩展模块」——一个共享库文件（Linux 上是 `.so`，Windows 上是 `.pyd`），Python 才能用 `import` 加载它。把「源文件变成可用的包」的这套流程，就叫**构建（build）**。

- **Meson 是什么**：Meson 是 SciPy 选用的构建系统，构建规则写在叫 `meson.build` 的文件里，描述「源文件 → 编译目标」的映射。它类似 C 语言的 Makefile 或 C++ 的 CMake，但语法更简洁、对 Python 更友好。SciPy 在构建脚本里用一个叫 `py3` 的辅助对象来处理 Python 特有的构建任务。

- **`py3` 提供的两个关键方法**：
  - `py3.extension_module(名字, 源文件列表, ...)` —— 把 C 源**编译**成一个 Python 扩展模块（生成 `.so`）。
  - `py3.install_sources(文件列表, subdir, ...)` —— 把纯 Python 文件（`.py`）**原样拷贝**到安装目录，不编译。

  另外还有 `subdir('子目录名')` 指令，让 Meson **递归进入**那个子目录，去执行该目录里自己的 `meson.build`。

> 一句话总结构建过程：**编译 C → 装配扩展，拷贝 Py → 装配包，subdir → 递归装配子包。**

## 3. 本讲源码地图

| 文件 | 类别 | 作用 |
|------|------|------|
| `scipy/integrate/meson.build` | 构建配置 | 顶层装配图纸：声明 4 个 C 扩展 + 1 个测试扩展，递归构建子包，安装顶层纯 Python 文件 |
| `scipy/integrate/_ivp/meson.build` | 构建配置 | `_ivp` 子包（ODE 初值求解器，纯 Python）的安装清单 |
| `scipy/integrate/_rules/meson.build` | 构建配置 | `_rules` 子包（cubature 积分规则，纯 Python）的安装清单 |
| `scipy/integrate/tests/meson.build` | 构建配置 | 测试文件安装清单 |
| `scipy/integrate/src/*.c` | C 源 | 由 Fortran 翻译而来的数值核心（vode/zvode/lsoda/dop），被编译进扩展模块 |
| `scipy/integrate/_ivp/*.py` | 纯 Python 子包 | `solve_ivp` 及各求解器（RK45/Radau/BDF/LSODA 等）的实现 |
| `scipy/integrate/_rules/*.py` | 纯 Python 子包 | cubature 用的积分规则基类与具体规则 |
| `scipy/integrate/vode.py` 等 | 加载桩 | 仅为兼容老导入路径而存在的「弃用垫片」 |

## 4. 核心概念与源码讲解

本讲按四个最小模块拆分：顶层 `meson.build`、`_ivp` 子包、`_rules` 子包、`src/` C 源目录。

### 4.1 meson.build 构建配置

#### 4.1.1 概念说明

`scipy/integrate/meson.build` 是整个 `integrate` 子包的「装配图纸」。它只做三件事，但每件都不可或缺：

1. 声明哪些 C 源要被**编译**成扩展模块（`extension_module`）。
2. 声明哪些子目录有自己的 `meson.build` 需要**递归处理**（`subdir`）。
3. 声明哪些纯 Python 文件要**原样安装**（`install_sources`）。

#### 4.1.2 核心流程

```
meson.build（顶层，scipy/integrate/）
  ├─ extension_module ×5    →  编译成 .so 扩展模块
  │     _quadpack / _odepack / _vode / _dop / _test_multivariate
  ├─ subdir ×3              →  递归进入子目录执行其 meson.build
  │     _ivp / _rules / tests
  └─ install_sources ×1     →  原样安装顶层纯 Python 文件
        __init__.py / _bvp.py / _quadpack_py.py / dop.py ...
```

三段顺序就是构建时的执行顺序：先编译扩展，再递归子包，最后安装散落的顶层 `.py`。

#### 4.1.3 源码精读

**第一段：5 个扩展模块的声明。** 每个块都给出「模块名 + C 源列表 + 依赖」。

[meson.build:L1-L6](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/meson.build#L1-L6) 把 `__quadpack.c`（连同头文件 `__quadpack.h`）编译成 `_quadpack` 扩展。它依赖 `np_dep`（NumPy 的 C 数组 API）和 `ccallback_dep`（SciPy 的底层回调机制，用于 `LowLevelCallable`）。

[meson.build:L8-L14](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/meson.build#L8-L14) 把 `_odepackmodule.c`（胶水层）和 `src/lsoda.c`（数值核心）一起编译成 `_odepack`。注意它额外依赖 `lapack_dep`（线性代数库 BLAS/LAPACK）。

[meson.build:L16-L22](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/meson.build#L16-L22) 把 `_dzvodemodule.c` + `src/vode.c` + `src/zvode.c` 编译成 `_vode`，同样依赖 `lapack_dep`。

[meson.build:L24-L30](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/meson.build#L24-L30) 把 `_dopmodule.c` + `src/dop.c` 编译成 `_dop`，依赖只有 `np_dep` 和 `ccallback_dep`，**不需要** `lapack_dep`。

[meson.build:L32-L38](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/meson.build#L32-L38) 编译一个仅供测试用的扩展 `_test_multivariate`（来自 `tests/_test_multivariate.c`），并用 `install_tag: 'tests'` 标记它只在测试场景下安装。

这里有一个值得注意的细节：`_quadpack` 和 `_dop` 不依赖 `lapack_dep`，而 `_odepack` 和 `_vode` 依赖。这说明前者的数值算法内部不需要解线性方程组，后者需要（刚性 ODE 求解器要反复解线性系统）。

**第二段：递归子目录。**

[meson.build:L41-L43](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/meson.build#L41-L43) 让 Meson 依次进入 `_ivp`、`_rules`、`tests` 三个子目录，执行它们各自目录里的 `meson.build`。子包的构建规则就这样被「挂」到了主构建树上。

**第三段：安装顶层纯 Python 文件。**

[meson.build:L45-L64](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/meson.build#L45-L64) 把顶层的十几个 `.py` 文件（如 `_bvp.py`、`_quadpack_py.py`、`_cubature.py`）以及几个加载桩（`dop.py`、`vode.py` 等）原样安装到 `scipy/integrate/` 目录。注意这里**没有** `_ivp/*.py` 和 `_rules/*.py`——它们由各自的子包 `meson.build` 负责。唯一一个非 `.py` 的条目 `src/LICENSE_DOP` 是 DOP 算法的许可证文本，随包一起分发以满足开源协议要求。

#### 4.1.4 代码实践（本讲主实践）

**目标**：亲手从 `meson.build` 里提取出「扩展模块 → C 源文件」对照表，验证你对构建配置的理解。

**操作步骤**：

1. 打开 [meson.build:L1-L38](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/meson.build#L1-L38)。
2. 对每一个 `py3.extension_module(...)` 调用，记下两样东西：
   - 第一个参数（模块名，带引号的字符串，如 `'_vode'`）。
   - 第二个参数（源文件列表，方括号里的内容）。
3. 把结果整理成下表（参考答案见 4.1.5）。

**需要观察的现象**：

- `_vode` 这一项的源列表里有 **3 个**文件，比其他都多——因为实数版 `vode.c` 和复数版 `zvode.c` 共用一个胶水层 `_dzvodemodule.c`。
- 只有 `_odepack` 和 `_vode` 的 `dependencies` 里出现了 `lapack_dep`。

**预期结果**：你应该得到一张 4 行（不含测试扩展）的对照表，每行形如「`_vode` ← `_dzvodemodule.c` + `src/vode.c` + `src/zvode.c`」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `_quadpack` 扩展的源文件是 `__quadpack.c`（两个下划线开头，就在顶层目录），而不是放在 `src/` 下？

**参考答案**：`__quadpack.c` 是为 `quad` 这一个函数专门写的 C/Fortran 桥接胶水，自包含且只服务 `quad`，所以直接放在顶层；而 `src/` 下放的是体积更大、来源更「古老」（由 Fortran 翻译而来）的通用数值核心（vode/lsoda/dop），多个公开函数会复用。下划线命名也表明它是私有内部文件。

**练习 2**：如果把 `_odepack` 从 `dependencies` 里删掉 `lapack_dep`，会发生什么？

**参考答案**：编译阶段很可能会失败，提示找不到 LAPACK/BLAS 符号（如 `dgetrf_` 之类的线性代数例程），因为 `lsoda.c` 内部会调用 LAPACK。`lapack_dep` 正是把 SciPy 依赖的线性代数库链接进扩展。

---

### 4.2 _ivp 子包

#### 4.2.1 概念说明

`_ivp` 是 **Initial Value Problem（初值问题）** 的缩写，它是一个**子包（subpackage）**——也就是一个带自己 `__init__.py` 的目录，里面装着新版 ODE 求解器的全部实现。关键特点是：它**完全是纯 Python**，没有任何 C 扩展。所以它的 `meson.build` 只做 `install_sources`，不做 `extension_module`。

#### 4.2.2 核心流程

```
_ivp/（子包）
  ├─ __init__.py            从各模块汇总导出 solve_ivp / RK45 / Radau / BDF / LSODA / OdeSolver ...
  ├─ ivp.py                 solve_ivp 主函数
  ├─ base.py                OdeSolver 基类、DenseOutput
  ├─ rk.py                  RK23 / RK45 / DOP853（显式 Runge-Kutta）
  ├─ radau.py               Radau（隐式，刚性）
  ├─ bdf.py                 BDF（隐式多步，刚性）
  ├─ lsoda.py               LSODA（自动刚度切换）
  ├─ common.py              公共工具（容差校验、数值雅可比等）
  ├─ dop853_coefficients.py DOP853 的系数表
  ├─ meson.build            安装清单（纯 Python）
  └─ tests/                 子包自己的测试目录
```

这些求解器的「重活」（如 LSODA 底层的 ODEPACK）确实会调用扩展模块，但 `_ivp` 这个子包**自己**不编译任何东西，它通过 `import` 去使用顶层编译好的 `_vode` / `_odepack` 等扩展。

#### 4.2.3 源码精读

[_ivp/meson.build:L1-L13](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/_ivp/meson.build#L1-L13) 把 `_ivp` 下的 8 个 `.py` 文件安装到 `scipy/integrate/_ivp/`，仅此而已——没有 `extension_module`，印证了「纯 Python 子包」的判断。

[_ivp/meson.build:L15](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/_ivp/meson.build#L15) 再用 `subdir('tests')` 递归构建 `_ivp/tests/`。

[_ivp/__init__.py:L1-L9](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/_ivp/__init__.py#L1-L9) 是这个子包的「汇总出口」：它从各模块把 `solve_ivp`、各求解器类、基类 `OdeSolver`、`OdeSolution` 等重新导出，使外部只需写 `from scipy.integrate._ivp import solve_ivp`（实际更常见的是直接用 `scipy.integrate.solve_ivp`，由顶层 `__init__.py` 中转）。

#### 4.2.4 代码实践

**目标**：把 `_ivp/__init__.py` 里的导出语句和「文件 → 类/函数」对应起来。

**操作步骤**：

1. 打开 [_ivp/__init__.py](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/_ivp/__init__.py#L1-L9)。
2. 逐行读 `from .xxx import yyy`，判断每个名字来自哪个文件、属于哪一类（主函数 / 显式方法 / 隐式方法 / 基类）。

**需要观察的现象**：`RK23/RK45/DOP853` 都来自同一行 `from .rk import ...`，说明它们共用 `rk.py` 这个文件实现。

**预期结果**：得到一张形如「`Radau` ← `radau.py`、`BDF` ← `bdf.py`、`OdeSolver` ← `base.py`」的映射表。

#### 4.2.5 小练习与答案

**练习**：`_ivp` 子包里没有任何 `.c` 文件，但 `LSODA` 求解器底层依赖 Fortran 写的 ODEPACK。这个「依赖」是怎么建立的？

**参考答案**：`_ivp/lsoda.py`（纯 Python）在运行时去 `import` 顶层编译好的扩展模块（`_odepack` 等），由扩展承担实际的 Fortran 数值计算。`_ivp` 自己只负责「调度与包装」，所以不需要编译。

---

### 4.3 _rules 子包

#### 4.3.1 概念说明

`_rules` 是 **数值积分规则（cubature rules）** 子包，服务于高维积分函数 `cubature`。和 `_ivp` 一样，它也是纯 Python 子包，构建方式完全相同。它定义了「如何在给定节点上估算积分值及其误差」的一组规则类。

#### 4.3.2 核心流程

```
_rules/（子包）
  ├─ __init__.py            汇总导出各类规则
  ├─ _base.py               Rule / FixedRule / NestedFixedRule / ProductNestedFixed 基类
  ├─ _gauss_kronrod.py      Gauss-Kronrod 规则（含误差估计）
  ├─ _gauss_legendre.py     Gauss-Legendre 规则
  └─ _genz_malik.py         Genz-Malik 多维规则
```

#### 4.3.3 源码精读

[_rules/meson.build:L1-L9](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/_rules/meson.build#L1-L9) 同样只有 `install_sources`，列出 5 个 `.py` 文件。注意这里**没有** `subdir('tests')`——`_rules` 没有独立的测试子目录，它的测试写在顶层 `tests/test_cubature.py` 里。

[_rules/__init__.py:L1-L12](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/_rules/__init__.py#L1-L12) 汇总导出三类规则：基类（`Rule`、`FixedRule` 等）、多维规则 `GenzMalikCubature`、两个一维规则 `GaussKronrodQuadrature` 与 `GaussLegendreQuadrature`，最后用与 `__init__.py` 相同的 `__all__` 惯例自动收集公开名字。

#### 4.3.4 代码实践

**目标**：列出 `_rules` 子包对外暴露的全部规则类，并按「基类 / 一维规则 / 多维规则」分类。

**操作步骤**：阅读 [_rules/__init__.py](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/_rules/__init__.py#L1-L12)，把 `from ._xxx import ...` 涉及的名字分类。

**需要观察的现象**：所有公开名字都不是下划线开头，而文件名（`_base.py` 等）都是下划线开头——这正是「私有文件、公开名字」的命名约定。

**预期结果**：得到分类表，例如「基类：Rule/FixedRule/NestedFixedRule/ProductNestedFixed；一维：GaussKronrodQuadrature/GaussLegendreQuadrature；多维：GenzMalikCubature」。

#### 4.3.5 小练习与答案

**练习**：对比 `_ivp/meson.build` 和 `_rules/meson.build`，二者结构上最大的区别是什么？

**参考答案**：`_ivp` 末尾有 `subdir('tests')`（它有独立测试目录 `_ivp/tests/`），而 `_rules` 没有（其测试在顶层 `tests/`）。两者都用 `py3.install_sources` 安装纯 Python 文件，都不编译扩展。

---

### 4.4 src C 源目录

#### 4.4.1 概念说明

`src/` 目录放的是 `integrate` 里最「重」的数值核心。这些文件**最初是用 Fortran 写的**（来自经典的 ODEPACK、VODE、DOP853 等库），后来被翻译成 C 以便统一编译。重要认知是：`src/*.c` **本身不是扩展模块**，它们只是「原料」，会被顶层 `meson.build` 的 `extension_module` 当作源文件之一，编译进某个 `.so` 扩展里。

#### 4.4.2 核心流程

```
src/（C 源「仓库」）
  ├─ vode.c   ┐
  ├─ zvode.c  ├─→  作为源文件，编译进 _vode 扩展（实数 + 复数 ODE）
  ├─ lsoda.c  ──→  编译进 _odepack 扩展（自动刚度切换）
  ├─ dop.c    ──→  编译进 _dop 扩展（DOP853 高阶显式）
  ├─ *.h      ──→  各自的头文件
  ├─ blaslapack_declarations.h  ──→  LAPACK/BLAS 例程的统一声明（_odepack/_vode 用到）
  └─ LICENSE_DOP  ──→  DOP 算法许可证（被 install_sources 一并分发）
```

注意：`__quadpack.c` 不在 `src/` 里，它在顶层目录、自成一体地编译成 `_quadpack`。

#### 4.4.3 源码精读

源文件与扩展的对应关系，全部由顶层 `meson.build` 决定（已在 4.1.3 逐块读过）。归纳成「原料 → 成品」：

| 扩展模块（成品 `.so`） | C 源文件（原料） | 是否在 `src/` 下 |
|---|---|---|
| `_vode` | `_dzvodemodule.c` + `src/vode.c` + `src/zvode.c` | 部分在 |
| `_odepack` | `_odepackmodule.c` + `src/lsoda.c` | 部分在 |
| `_dop` | `_dopmodule.c` + `src/dop.c` | 部分在 |
| `_quadpack` | `__quadpack.c`（+ `__quadpack.h`） | 不在 |

这里 `_dzvodemodule.c`、`_odepackmodule.c`、`_dopmodule.c` 是「胶水层（glue）」：它们用 Python C-API 把上面的 Python 调用翻译成对 `src/*.c` 里 Fortran 风格函数的调用。

另外，顶层还有 5 个名字像模块、实则很短的文件：`vode.py`、`dop.py`、`lsoda.py`、`odepack.py`、`quadpack.py`。以 [vode.py:L1-L16](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/vode.py#L1-L16) 为例，第一行注释就点明了它的本质：

> `# This file is not meant for public use and will be removed in SciPy v2.0.0.`

这些是**加载桩（deprecation shim）**。早期 SciPy 允许 `from scipy.integrate.odepack import odeint` 这种写法，但官方希望用户只从 `scipy.integrate` 顶层命名空间导入。于是这些桩文件用 `__getattr__` 拦截老路径的导入，发出弃用警告后，再从真正的私有模块（如 `_quadpack_py`）取值返回。[odepack.py:L1-L18](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/odepack.py#L1-L18) 就是把老路径 `odepack.odeint` 重定向到私有模块 `_odepack_py` 的例子。它们被 `install_sources` 安装，纯粹是为了向后兼容。

#### 4.4.4 代码实践

**目标**：把「公开函数 → 扩展模块 → C 源」整条链路串起来。

**操作步骤**：

1. 在本仓库用编辑器全局搜索（或在 [__init__.py](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/__init__.py) 里）确认：`odeint` 来自 `_odepack_py.py`、`quad` 来自 `_quadpack_py.py`。
2. 结合 4.4.3 的成品表，补全链路。例如：`odeint`（在 `_odepack_py.py`）→ 运行时调用扩展 `_odepack` → 编译自 `_odepackmodule.c` + `src/lsoda.c`。
3. 用同样方式推导 `quad` 的链路。

**需要观察的现象**：`quad` 的链路更短——`quad`（在 `_quadpack_py.py`）→ 扩展 `_quadpack` → 编译自 `__quadpack.c`（单文件，无 `src/` 依赖）。

**预期结果**：得到一张三列链路表（公开函数 / 调用的扩展 / 扩展的 C 源）。**待本地验证**：若你想确认 `_odepack_py.py` 里确实 `import _odepack`，可在该文件中搜索 `from scipy.integrate import _odepack` 之类的语句核对。

#### 4.4.5 小练习与答案

**练习 1**：`src/` 下有 `vode.c` 和 `zvode.c` 两个文件，前缀差异暗示了什么？

**参考答案**：`z` 前缀表示 **complex（复数）** 版本。`vode.c` 解实值 ODE，`zvode.c` 解复值 ODE；二者共用同一个胶水层 `_dzvodemodule.c`，一起编译进 `_vode` 扩展。

**练习 2**：加载桩文件（如 `vode.py`）会不会被编译？为什么还要单独列出它？

**参考答案**：不会编译，它是纯 Python，由 `install_sources` 原样安装。单独列出是为了让老的 `from scipy.integrate.vode import ...` 路径仍可导入（并触发弃用警告），保证向后兼容，等 SciPy 2.0 再移除。

---

## 5. 综合实践

把本讲四块知识串起来，画一张 **`integrate` 物理结构全景图**，要求包含三层信息：

1. **目录层**：`scipy/integrate/` 顶层、`_ivp/`、`_rules/`、`src/`、`tests/`。
2. **构建动作层**：每个目录分别用 `extension_module` / `install_sources` / `subdir` 中的哪一个（提示：顶层三者都用，`_ivp` 与 `_rules` 用后两者，`src/` 自己没有 `meson.build`、是被顶层的 `extension_module` 引用的）。
3. **依赖链路层**：任选 2 个公开函数（如 `quad`、`odeint`），画出「公开函数 → 私有 `.py` → 扩展 `.so` → C 源」的完整箭头。

完成后，用一句话向自己解释：**为什么 `_ivp` 和 `_rules` 是纯 Python 子包，而 `src/` 里的 C 却要被编进顶层的扩展？**（参考答案：纯 Python 负责易变的算法逻辑与 API 设计，C 扩展负责性能敏感、来源古老的数值核心，二者通过运行时 `import` 解耦。）

## 6. 本讲小结

- `scipy.integrate` 在物理上分四层：顶层散落的 `.py`、纯 Python 子包 `_ivp`/`_rules`、`src/` 下的 C 数值核心、以及 `tests/`。
- 顶层 `meson.build` 用三种 Meson 指令完成装配：`extension_module`（编译 C → `.so`）、`subdir`（递归子包）、`install_sources`（原样安装 `.py`）。
- 四个核心扩展 `_quadpack`/`_odepack`/`_vode`/`_dop` 分别由固定的 C 源列表编译；其中 `_odepack` 与 `_vode` 需要 `lapack_dep`，另外两个不需要。
- `_ivp`（ODE 初值求解器）与 `_rules`（cubature 积分规则）都是纯 Python 子包，自己的 `meson.build` 只做 `install_sources`。
- `src/*.c` 是由 Fortran 翻译来的数值核心，本身不构成扩展，而是作为源文件被顶层的 `extension_module` 编译进 `.so`。
- 顶层的 `vode.py`/`odepack.py` 等是加载桩，仅为兼容老导入路径并触发弃用警告，将在 SciPy 2.0 移除。

## 7. 下一步学习建议

你现在掌握了 `integrate` 的「骨架」。下一步建议学习 [u1-l3 上手运行：导入、调用与第一个示例](u1-l3-getting-started.md)，从用户视角实际 `import scipy.integrate` 并调用 `quad`、`solve_ivp`、`trapezoid`，把本讲看到的文件名和「运行时真正发生的事」对应起来。之后再按学习路线进入具体模块（固定样本积分、自适应积分、ODE 求解器等）的源码精读。

> 想深入构建系统本身，可在学完本讲后跳读 [scipy/integrate/meson.build](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/meson.build#L1-L64)，并对照后续专家层讲义 u12-l1（meson 构建与扩展注册）。
