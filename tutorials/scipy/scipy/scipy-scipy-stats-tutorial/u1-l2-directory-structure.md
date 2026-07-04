# 目录结构与模块组织

## 1. 本讲目标

上一讲（u1-l1）我们建立了「scipy.stats 是什么」的全局认知，知道它覆盖分布、统计、检验、重采样等众多领域。但当你真正打开 `scipy/stats/` 目录时，会看到近百个文件——有的叫 `_stats_py.py`，有的叫 `_stats.pyx`，还有 `_levy_stable/`、`_unuran/` 这样的子目录，以及一大堆 `distributions.py`、`stats.py`、`mstats.py` 这种「短名字」文件。它们之间是什么关系？哪些是给人用的、哪些是给机器编译的？

本讲学完后，你应该能够：

1. 识别 `scipy/stats/` 目录下四类文件的命名规律与职责划分（公共入口、子领域实现、编译扩展、子包）。
2. 读懂 `meson.build`，说出哪些文件会被「编译成扩展模块」，哪些只是「原样复制」的纯 Python 文件。
3. 读懂 `__init__.py` 的导入结构，理解它如何把分散在几十个 `_xxx.py` 里的函数「聚合」成统一的 `scipy.stats` 命名空间。
4. 定位任意一个统计功能（例如 `ttest_ind`、`norm`、`sobol_indices`）所对应的真实源码文件。

---

## 2. 前置知识

阅读本讲前，建议你已经：

- 读过 u1-l1，知道 scipy.stats 的功能地图与边界声明。
- 知道 Python 包（package）的基本概念：`__init__.py` 是一个目录成为「可导入包」的标志，包内模块通过 `from .xxx import yyy` 互相引用。
- 大致听说过「编译扩展」：Python 本身是解释执行的语言，但科学计算库常常用 C/C++/Cython 把性能关键部分编译成机器码，生成 `.so`（Linux）/`.pyd`（Windows）文件，再像普通模块一样 `import`。
- 听说过 Meson：它是 SciPy 用来代替旧版 `setup.py` 的构建系统，配置写在 `meson.build` 文件里。

> 名词解释：
> - **Cython**：一种让 Python 代码带上类型声明、再翻译成 C 代码编译的语言。`.pyx` 是 Cython 源文件，`.pxd` 是它的「头文件」（声明类型供其他 `.pyx` 引用）。
> - **扩展模块（extension module）**：被编译成机器码、但仍能被 Python `import` 的模块。导入时拿到的是编译产物，不是源码。
> - **纯 Python 模块**：以 `.py` 源码形式直接被解释执行的模块。

---

## 3. 本讲源码地图

本讲涉及的关键文件（均可点击链接查看真实源码）：

| 文件 | 角色 | 本讲用途 |
|------|------|----------|
| [`__init__.py`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/__init__.py) | 公共入口与命名空间聚合器 | 讲解导入结构（最小模块 1） |
| [`meson.build`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/meson.build) | 构建配置 | 区分编译扩展与纯 Python（最小模块 2） |
| [`_result_classes.py`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_result_classes.py) | 结果类文档聚合 | 说明「私有为王、按需暴露」的设计 |
| [`distributions.py`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/distributions.py) | 分布命名空间聚合（shim） | 说明历史遗留的薄壳文件 |
| [`_levy_stable/meson.build`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_levy_stable/meson.build)、[`_unuran/meson.build`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_unuran/meson.build)、[`_rcont/meson.build`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_rcont/meson.build) | 子包构建配置 | 说明子包如何自带 C/C++ 扩展 |

---

## 4. 核心概念与源码讲解

### 4.1 目录全景：四类文件与命名规律

#### 4.1.1 概念说明

打开 `scipy/stats/`，你会被文件数量吓到。但只要按下「命名规律」分类，整个目录立刻清晰起来。stats 目录的文件可以归为四类：

1. **公共入口与聚合壳**：`__init__.py`，以及 `distributions.py`、`mstats.py`、`qmc.py`、`contingency.py` 这种「短名字」文件。它们本身几乎不写算法，只负责 `import` 别处定义好的东西。
2. **子领域实现文件**：以下划线开头、`_xxx.py` 命名，例如 `_stats_py.py`、`_morestats.py`、`_hypotests.py`、`_continuous_distns.py`。这些才是真正写统计算法的地方。
3. **编译扩展源**：`.pyx`（Cython）、`.pxd`（Cython 头）、`.c`/`.cpp`/`.h`（C/C++），例如 `_stats.pyx`、`_sobol.pyx`、`_biasedurn.pyx`。它们会被编译成机器码。
4. **子包**：以目录形式存在、自带 `meson.build`，例如 `_levy_stable/`、`_unuran/`、`_rcont/`，以及测试目录 `tests/`。

#### 4.1.2 核心流程

判断一个文件属于哪一类的「决策树」：

```text
看到一个文件名
├── 是 .pyx / .pxd / .c / .cpp / .h 吗？
│     → 编译扩展源（第 3 类），构建后会变成 .so
├── 是子目录吗？
│     → 子包（第 4 类），看它自己的 meson.build
├── 以 _ 开头的 .py 吗？
│     → 子领域实现文件（第 2 类），被 __init__ 重新导出
└── 是不带 _ 的短名字 .py（如 distributions.py / stats.py）？
      → 聚合壳或遗留 shim（第 1 类）
```

> 关键直觉：**下划线前缀 `_` 在 Python 里约定俗成表示「私有/内部」**。stats 把所有真正的实现藏在 `_xxx.py` 里，对外只暴露 `scipy.stats.xxx`。这也是为什么 u1-l1 提到 `__all__` 的生成方式是「过滤掉以 `_` 开头的名字」。

#### 4.1.3 源码精读

先看一个最有代表性的「聚合壳」—— [`distributions.py`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/distributions.py#L1-L18)。这个文件短到只有十几行，却聚合了**全部**概率分布：

[distributions.py:L8-L18](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/distributions.py#L8-L18) —— 这段代码把 `_continuous_distns`、`_discrete_distns`、`_distn_infrastructure`、`_levy_stable` 里定义的所有分布，全部「搬运」进 `distributions` 命名空间。可以看到它**自身没有定义任何一个分布**，只是 `from ... import *`。这就是「聚合壳」的典型形态。

注意第 8 行的注释 `# For backwards compatibility e.g. pymc expects distributions.__all__`——这告诉我们，某些壳文件存在的原因是**历史兼容**：早期用户写 `from scipy.stats.distributions import norm`，为了不破坏这些代码，壳文件被保留下来。

#### 4.1.4 代码实践

**实践目标**：亲手验证「四类文件」的分类法。

**操作步骤**：

1. 在 `scipy/stats/` 目录下，分别对四类文件用 `ls` 观察：
   - `ls *.pyx *.pxd` —— 看编译扩展源；
   - `ls -d */` —— 看子包；
   - `ls _*.py | head` —— 看子领域实现；
   - `ls *.py | grep -v '^_'` —— 看聚合壳与遗留 shim。
2. 打开 [`distributions.py`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/distributions.py) 全文，确认它没有任何 `def`/`class` 定义新内容，全是 `import`。

**需要观察的现象**：聚合壳文件体积都很小（几百字节到几 KB），而 `_continuous_distns.py` 高达 413 KB——后者才是「真实现」。

**预期结果**：你会清楚地看到「壳很薄、实现很厚」的对比，从而理解为什么 stats 要把实现藏进下划线文件。

#### 4.1.5 小练习与答案

**练习 1**：`stats.py` 和 `_stats_py.py` 哪个是「真实现」？为什么？

> **答案**：`_stats_py.py` 是真实现（下划线前缀、体积巨大）。`stats.py` 是遗留 shim，打开它会看到注释 `# This file is not meant for public use and will be removed in SciPy v2.0.0`，它只是把访问重定向到 `_stats_py` 等私有模块并发出弃用警告。

**练习 2**：`_sobol_direction_numbers.npz` 这个文件属于哪一类？它是给人读的还是给机器用的？

> **答案**：它是 Sobol 序列的方向数表（NumPy 压缩存档），属于「数据文件」。它既不是 Python 也不是扩展源，构建时被 `install_sources` 原样复制，运行时被 `_sobol` 扩展模块加载。下一节会在 `meson.build` 里看到它的安装声明。

---

### 4.2 __init__.py 的导入结构：如何聚合出统一命名空间（最小模块 1）

#### 4.2.1 概念说明

`scipy.stats` 给用户的体验是：`from scipy.stats import norm, ttest_ind, gaussian_kde` 一把抓。但实际上，这些对象定义在十几个不同的 `_xxx.py` 文件里。把它们「装进同一个袋子」交给用户的，正是 [`__init__.py`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/__init__.py)。

`__init__.py` 在 stats 里扮演的角色是**纯路由层**：

- 它顶部是一段超长的模块文档字符串（即 u1-l1 讲过的「功能地图」，约 596 行）。
- 文档字符串之后，是一连串 `from ._xxx import ...` 语句，把各子领域实现「搬运」进来。
- 最后用一行 `__all__` 自动生成公开 API，并挂上测试入口 `test()`。

它**几乎不包含任何算法逻辑**——这是一个非常重要的设计原则：入口文件保持「薄」，方便维护与阅读。

#### 4.2.2 核心流程

`__init__.py` 的执行流程可以概括为三步：

```text
① 导入警告/错误类
        │
② 逐个 from ._子领域 import *，把实现「搬进」当前命名空间
   （此时 dir() 里堆满了 norm、ttest_ind、describe 等）
        │
③ __all__ = [所有不以 _ 开头的名字]   ← 自动公开 API
   挂上 PytestTester 作为 test() 入口
```

第 ③ 步最巧妙：维护者**不需要手写 `__all__` 列表**。只要在第 ② 步用 `import *` 把名字搬进来，再剔除下划线开头的私有名，剩下的就是公开 API。这样新增一个分布时，根本不用改 `__init__.py` 的 `__all__`。

#### 4.2.3 源码精读

先看导入语句的起点与几个代表性条目（位于文档字符串之后）：

[\_\_init\_\_.py:L598-L617](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/__init__.py#L598-L617) —— 这段是导入区的开头。可以看到：

- 第 598-599 行先导入警告/异常类（`ConstantInputWarning`、`FitError` 等）；
- 第 600 行 `from ._stats_py import *` 是**最大的一块**，搬运了 `describe`、`gmean`、`ttest_*`、`pearsonr`、`f_oneway` 等几十个基础统计函数；
- 第 602 行 `from .distributions import *` 套娃式地再通过 4.1 讲过的 `distributions.py` 壳，把全部概率分布搬进来；
- 第 610 行 `from ._multivariate import *` 搬运多元分布（`multivariate_normal` 等）；
- 第 614-615 行搬运重采样三件套（`bootstrap`、`permutation_test`、`monte_carlo_test`）。

再看导入区的尾部，以及新一代分布基础设施的引入：

[\_\_init\_\_.py:L625-L631](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/__init__.py#L625-L631) —— 第 625-627 行从 `_distribution_infrastructure` 引入新一代分布工厂（`make_distribution`、`Mixture`、`truncate` 等），第 628 行从 `_new_distributions` 引入新的内置分布（`Normal`、`Binomial`）。注意这里**没有用 `import *`**，而是显式列出名字——这是因为这些模块还在演进，维护者希望精确控制暴露哪些 API。

最后看「自动公开 API」与测试入口这两段压轴代码：

[\_\_init\_\_.py:L634-L644](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/__init__.py#L634-L644) —— 这里有三件事值得注意：

- 第 635-637 行导入了一组「弃用命名空间」（`biasedurn, kde, morestats, mstats_basic, mstats_extras, mvn, stats`），注释明确写着「to be removed in v2.0.0」——这些就是 4.1 提到的历史遗留壳，导入进来是为了让老代码 `scipy.stats.stats.gmean` 仍能工作（并触发弃用警告）。
- 第 640 行 `__all__ = [s for s in dir() if not s.startswith("_")]` 是前面讲过的「自动剔除下划线」逻辑，是整个公开 API 的来源。
- 第 642-644 行挂上 `PytestTester`：这就是为什么用户可以调用 `scipy.stats.test()` 跑测试（详见 u1-l3）。

> 设计要点：因为 `__all__` 是动态从 `dir()` 生成的，所以「哪些名字公开」完全取决于第 ② 步 `import` 了什么。这种「以导入驱动 API」的设计让新增功能极低成本，但也意味着**读 `__init__.py` 的导入区，就等于读 stats 的完整公开 API 清单**。

#### 4.2.4 代码实践

**实践目标**：验证「`__init__.py` 导入区 = 公开 API 清单」这一论断。

**操作步骤**：

1. 打开本讲引用的 [`__init__.py` 导入区](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/__init__.py#L598-L631)，挑一个函数名，例如第 619 行 `from ._mannwhitneyu import mannwhitneyu` 里的 `mannwhitneyu`。
2. 在装有 SciPy 的环境里运行：

   ```python
   import scipy.stats as stats
   print("mannwhitneyu" in stats.__all__)   # 预期 True
   print(stats.mannwhitneyu.__module__)     # 预期指向 scipy.stats._mannwhitneyu
   ```

**需要观察的现象**：第二个打印会显示该函数的「真实出生地」是 `scipy.stats._mannwhitneyu`，而不是 `scipy.stats` 本身——证明 `__init__.py` 只是搬运工。

**预期结果**：`mannwhitneyu` 在 `__all__` 里为 `True`，且 `__module__` 指向私有实现文件。如果环境里没装 SciPy，则标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `__init__.py` 对 `_distribution_infrastructure` 用显式 `import` 名字（第 625-627 行），而对 `_stats_py` 用 `import *`（第 600 行）？

> **答案**：`_stats_py` 是成熟的、API 已稳定的基础模块，全部导出即可；`_distribution_infrastructure` 是较新的「新一代分布基础设施」，内部还有许多不应暴露的辅助类（如 `_Domain`、`_Parameter`），显式列出名字可以精确控制只暴露 `make_distribution`、`Mixture` 等公共对象，避免内部实现泄漏成公共 API。

**练习 2**：第 640 行 `__all__ = [s for s in dir() if not s.startswith("_")]` 执行时，`dir()` 里为什么会**已经**包含 `norm`、`ttest_ind` 这些名字？

> **答案**：因为 Python 是从上到下顺序执行 `__init__.py` 的。第 600-631 行的那些 `from ._xxx import *` 语句已经先于第 640 行执行，把 `norm`、`ttest_ind` 等名字绑定到了当前模块的命名空间，所以到第 640 行时 `dir()` 已经能看到它们。

---

### 4.3 meson.build 构建配置：编译扩展与纯 Python 文件清单（最小模块 2）

#### 4.3.1 概念说明

如果说 `__init__.py` 决定了「运行时哪些名字能用」，那么 [`meson.build`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/meson.build) 决定了「安装时哪些文件进入用户的 Python 环境、以什么形态进入」。这是理解 stats 目录的另一半钥匙。

`meson.build` 里有两种截然不同的安装指令：

- `py3.extension_module('名字', 源文件, ...)`：把源文件**编译**成一个扩展模块（`.so`）。源通常是 `.pyx` 或 `.py`（Pythran）。
- `py3.install_sources([文件列表], ...)`：把文件**原样复制**到安装目录，不做任何编译。纯 Python 文件、数据文件、`.pxd`/`.pyi` 都走这条路。

只要分清这两条指令，就能立刻回答「这个文件是纯 Python 还是编译扩展」。

#### 4.3.2 核心流程

`meson.build` 的整体结构（从上到下）：

```text
① 声明 Cython 头依赖（_stats_pxd）、定义 cython 生成器
② 用 extension_module 声明 7 个编译扩展：
     _stats, _ansari_swilk_statistics, _sobol, _qmc_cy,
     _biasedurn, _stats_pythran(可选), _qmvnt_cy
③ install_sources：安装数据文件（_sobol_direction_numbers.npz）
④ install_sources：原样安装所有纯 Python .py（一大段清单）
⑤ install_sources：安装 .pxd 头、.pyi 类型存根
⑥ subdir：进入子包 _levy_stable / _unuran / _rcont 和 tests
```

理解这条主线后，stats 目录里**每一个文件**的归属都能在 `meson.build` 里找到对应行——这就是它作为「目录宪法」的威力。

#### 4.3.3 源码精读

**第一类：编译扩展。** 先看最典型的 Cython 扩展 `_stats`：

[meson.build:L16-L23](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/meson.build#L16-L23) —— 这段把 `_stats.pyx` 编译成名为 `_stats` 的扩展模块，安装到 `scipy/stats` 子目录。`cython_args`/`cython_c_args` 是编译选项，`np_dep` 是对 NumPy 头文件的依赖。安装后用户得到的是 `_stats.cpython-3xx-xxx.so`，而**不是** `_stats.pyx`。

再看一个更复杂的——`_biasedurn`，它把 Cython 与多个 C++ 源文件链接在一起：

[meson.build:L58-L75](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/meson.build#L58-L75) —— 这段把 `_biasedurn.pyx`（Cython）与 `biasedurn/fnchyppr.cpp`、`biasedurn/impls.cpp` 等一组 C++ 文件（非中心超几何分布的采样算法）一起编译，产物是 `_biasedurn` 扩展。注意 `cpp_args: ['-DR_BUILD', ...]` 和 `include_directories: ['libnpyrandom']`——这说明它依赖同目录下的 `libnpyrandom/` C 库。

整个 `meson.build` 用 `extension_module` 声明的扩展共 **7 个**（第 16、25、34、49、58、78、99 行）。下表汇总：

| 扩展名 | 源 | 性质 |
|--------|----|----|
| `_stats` | `_stats.pyx` | Cython（秩统计等） |
| `_ansari_swilk_statistics` | `_ansari_swilk_statistics.pyx` | Cython（Shapiro-Wilk 等） |
| `_sobol` | `_sobol.pyx` | Cython（Sobol 方向数） |
| `_qmc_cy` | `_qmc_cy.pyx` | Cython++（QMC 差异性） |
| `_biasedurn` | `_biasedurn.pyx` + `biasedurn/*.cpp` | Cython++ + C++（非中心超几何采样） |
| `_stats_pythran` | `_stats_pythran.py` | Pythran（可选，见下） |
| `_qmvnt_cy` | `_qmvnt_cy.pyx` | Cython++（多元正态/t CDF） |

注意第 6 个 `_stats_pythran` 是**有条件编译**的：

[meson.build:L77-L96](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/meson.build#L77-L96) —— `if use_pythran` 分支用 Pythran 把 `_stats_pythran.py` 编译成扩展；`else` 分支则降级为「原样安装」纯 Python 版本。这是一个优雅的「能编译就加速、不能编译就回退」的设计。

**第二类：纯 Python 原样安装。** 这是 `meson.build` 里最长的一段：

[meson.build:L109-L173](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/meson.build#L109-L173) —— 这段 `install_sources` 列表把所有 `_xxx.py` 实现文件（`_stats_py.py`、`_continuous_distns.py`、`_distn_infrastructure.py`、`_hypotests.py` …）以及聚合壳（`distributions.py`、`contingency.py`、`mstats.py`、`qmc.py`、`stats.py` 等）原样复制到安装目录。**只要一个 `.py` 文件出现在这里、且不在任何 `extension_module` 里，它就是纯 Python。**

> 关键判据：判断「某文件是纯 Python 还是编译扩展」，只需在 `meson.build` 里搜该文件名：
> - 出现在 `extension_module(...)` 的源参数里 → 编译扩展；
> - 只出现在 `install_sources([...])` 里 → 纯 Python；
> - 既出现又伴随 `.pyx`/`.c` → 该 `.py` 是被 Pythran 编译的特例。

**第三类：数据与辅助文件。**

[meson.build:L43-L47](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/meson.build#L43-L47) 安装 Sobol 数据表；[meson.build:L175-L180](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/meson.build#L175-L180) 安装 `.pxd` Cython 头（供下游扩展引用）；[meson.build:L182-L187](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/meson.build#L182-L187) 安装 `.pyi` 类型存根（供 IDE 与类型检查器使用，如 `_sobol.pyi`、`_qmc_cy.pyi`）。

#### 4.3.4 代码实践

**实践目标**：依据 `meson.build` 给出 stats 主目录的「编译扩展 vs 纯 Python」分类表（这正是本讲的总体实践任务）。

**操作步骤**：

1. 打开 [`meson.build`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/meson.build)。
2. 用下面的命令在仓库里直接统计两类声明：

   ```bash
   # 列出所有编译扩展的名字
   grep -n "extension_module" meson.build
   # 数一下原样安装的纯 Python 文件个数
   sed -n '109,173p' meson.build | grep "\.py" | wc -l
   ```

3. 对照本节给出的 7 个扩展表，核对你的 `grep` 结果。

**需要观察的现象**：`grep extension_module` 会输出 7 行（第 16/25/34/49/58/78/99 行），与上表完全对应；纯 Python 清单则包含约 60 个 `.py` 文件。

**预期结果**：你应当能独立产出一张「文件 → 类别」的对照表。若 `grep` 在你的环境里因换行/格式略有出入，以本讲链接的行号为准。

#### 4.3.5 小练习与答案

**练习 1**：`_stats_py.py` 是编译扩展吗？依据是什么？

> **答案**：不是。它是纯 Python。依据是：它**只**出现在 `meson.build` 的 `install_sources` 大清单里（第 109-173 行），没有出现在任何 `extension_module` 的源参数中。注意不要和编译扩展 `_stats`（来自 `_stats.pyx`）混淆——两者名字相近但形态不同。

**练习 2**：`_stats.pyx` 和 `_stats_pxd`（第 1-5 行）是什么关系？为什么 `.pxd` 要单独 `fs.copyfile`？

> **答案**：`_stats.pyx` 是 Cython 源，编译后产出 `_stats` 扩展；`.pxd` 是 Cython 的「头文件」，声明可供**其他** `.pyx` 文件 `cimport` 的类型。第 1-5 行的 `_stats_pxd` 列表把这些 `.pxd` 标记为编译期依赖（`depends`），是为了在 `.pxd` 改动时触发依赖它的扩展重新编译——这是增量构建的正确性保证，与运行时无关。

---

### 4.4 子包与底层 C/C++ 扩展：_levy_stable / _unuran / _rcont

#### 4.4.1 概念说明

stats 主目录的 `meson.build` 末尾有四行 `subdir`：

[meson.build:L189-L193](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/meson.build#L189-L193) —— 这告诉 Meson「进入 `_levy_stable/`、`_unuran/`、`_rcont/` 三个子包，以及 `tests/`，分别执行它们各自的 `meson.build`」。

为什么这三个功能要单独成包？因为它们都依赖**大型外部 C/C++ 代码**或**复杂的多文件算法**，放进主目录会让文件混杂难管。把它们独立成包，既隔离了复杂度，也让各自的构建配置自成一体。

#### 4.4.2 核心流程

三个子包的共同模式：

```text
子包目录/
├── __init__.py        ← 对外暴露的纯 Python 入口（通常很薄）
├── meson.build        ← 子包自己的构建配置
├── *.pyx / *.pyx + C源 ← 被编译成扩展模块
└── （可选）c_src/ 或外部库源码
```

主 `meson.build` 用 `subdir` 把控制权交给子包，子包再各自声明自己的 `extension_module`。用户最终通过 `scipy.stats.levy_stable`、`scipy.stats.sampling`（对应 `_unuran`）等接口访问。

#### 4.4.3 源码精读

**`_levy_stable`**（Lévy 稳定分布，依赖特征函数数值积分 + C 加速）：

[_levy_stable/meson.build:L7-L20](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_levy_stable/meson.build#L7-L20) —— 先用 `static_library('_levyst', ['c_src/levyst.c', ...])` 把 C 源编成静态库，再用 `extension_module('levyst', cython_gen.process('levyst.pyx'), ..., link_with: _levyst)` 把 `levyst.pyx` 与该静态库链接成 `levyst` 扩展。这是「C 内核 + Cython 外壳」的经典组合。

**`_unuran`**（UNURAN 通用随机数生成，依赖外部 UNURAN C 库）：

[_unuran/meson.build:L22-L36](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_unuran/meson.build#L22-L36) —— `unuran_sources = unuran.get_variable('unuran_sources')` 从 SciPy 顶层声明的 UNURAN 依赖里取出 C 源列表，再与 `unuran_wrapper.pyx` 一起编译成 `unuran_wrapper` 扩展。注意它用了自定义的 `unuran_cython_gen`（依赖 `_lib_pxd`、`_stats_pxd`、`_unuran_pxd`），因为 UNURAN 的回调机制依赖 SciPy 的 `_ccallback` 基础设施。

**`_rcont`**（列联表随机生成）：

[_rcont/meson.build:L9-L22](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_rcont/meson.build#L9-L22) —— 把 `_rcont.c` 与上级 `libnpyrandom/logfactorial.c`、`libnpyrandom/distributions.c` 一起和 `rcont.pyx` 编译成 `rcont` 扩展。可以看到它用相对路径 `../libnpyrandom/...` 复用了主目录的 C 工具库。

> 小结：三个子包各自产出 1 个扩展（`levyst`、`unuran_wrapper`、`rcont`）。加上 4.3 节主目录的 7 个，整个 stats 包**至少**编译出 10 个扩展模块（`_stats_pythran` 视环境而定）。

#### 4.4.4 代码实践

**实践目标**：定位每个子包产出的扩展模块名，并找到它的对外 Python 入口。

**操作步骤**：

1. 分别打开三个子包的 `meson.build`（链接见本节），找到其中的 `extension_module('名字', ...)`。
2. 打开 `_levy_stable/__init__.py`、`_unuran/__init__.py`、`_rcont/__init__.py`，看它们如何把编译产物包一层纯 Python 接口暴露出去。

**需要观察的现象**：每个子包都遵循「C/C++ 内核 → Cython 包装（`.pyx`）→ `__init__.py` 暴露」的三层结构。

**预期结果**：你会确认 `_levy_stable` 产出 `levyst`、`_unuran` 产出 `unuran_wrapper`、`_rcont` 产出 `rcont`，它们分别服务于 `scipy.stats.levy_stable`、`scipy.stats.sampling`、列联表工具。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `_unuran` 不像 `_levy_stable` 那样把 C 源直接放在子包里，而是用 `unuran.get_variable('unuran_sources')`？

> **答案**：因为 UNURAN 是一个**独立的外部 C 库**，SciPy 在顶层统一管理它的获取与配置（作为系统依赖或 vendored 源），通过 Meson 对象的 `get_variable` 把源列表传给各处使用方。这与 `_levy_stable` 自带 `c_src/` 的「自包含」做法不同，反映了「自带代码」与「依赖外部库」两种集成方式的区别。

**练习 2**：用户调用 `scipy.stats.levy_stable.rvs(...)` 时，最终执行的是纯 Python 还是编译代码？

> **答案**：核心数值计算走的是编译扩展 `levyst`（由 `levyst.pyx` + `c_src/levyst.c` 生成），但调度、参数校验、与其他 stats 分布的接口一致性由 `_levy_stable/__init__.py` 里的纯 Python 代码负责。两者协作完成一次调用。

---

### 4.5 tests 目录与结果类

#### 4.5.1 概念说明

最后两块拼图：**测试**与**结果类**。

- `tests/` 目录：stats 有 44 个测试相关条目，是保证这么多统计函数数值正确性的基石。测试目录有自己的 `meson.build`（被主 `meson.build` 第 193 行 `subdir('tests')` 引入），还有共享的 `common_tests.py` 和 `data/` 参考数据。
- `_result_classes.py`：一个很特殊的「文档专用」文件。很多假设检验返回专门的结果对象（`TtestResult`、`PearsonRResult`、`FitResult` 等），这些类是私有的，但用户会拿到它们的实例。这个文件存在的唯一目的，是让 Sphinx 文档系统能为这些结果类生成参考页，**而不把它们塞进主文档页面**。

#### 4.5.2 核心流程

`_result_classes.py` 的工作机制：

```text
各 _xxx.py 定义私有结果类（如 _stats_py.TtestResult）
        │
_result_classes.py 用 from ._xxx import 把它们集中
        │
Sphinx 根据 _result_classes.py 的 autosummary 生成文档页
        │
__init__.py 文档字符串末尾用 toctree 指向 stats._result_classes
（但类本身仍不在 stats 公开 API 中实例化入口）
```

#### 4.5.3 源码精读

[_result_classes.py:L1-L4](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_result_classes.py#L1-L4) —— 第 1-3 行的注释开门见山：「This module exists only to allow Sphinx to generate docs for the result objects... _without_ adding them to the main stats documentation page」。这是理解整个文件存在意义的钥匙。

[_result_classes.py:L33-L40](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_result_classes.py#L33-L40) —— 这里把分散在 `_binomtest`、`_odds_ratio`、`_hypotests`、`_multicomp`、`_stats_py`、`_fit`、`_survival` 里的结果类汇集到一处。注意它们各自的真实定义仍在原文件——`_result_classes.py` 同样只是「搬运工」，和 4.1 的聚合壳同构，只不过它的服务对象是文档生成器而非用户。

`__init__.py` 文档字符串末尾对它有一段明确说明：

[\_\_init\_\_.py:L584-L595](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/__init__.py#L584-L595) —— 这里用 `.. warning::` 声明这些结果类「是私有的，之所以列出来是因为它们会被其他统计函数返回；不支持用户直接 import 和实例化」。这呼应了「私有为王、按需暴露」的设计哲学。

#### 4.5.4 代码实践

**实践目标**：验证「结果类定义在别处，`_result_classes.py` 只是文档聚合」。

**操作步骤**：

1. 打开 [`_result_classes.py`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_result_classes.py#L33-L40)，看到第 38 行 `from ._stats_py import PearsonRResult, TtestResult`。
2. 用搜索工具在 `_stats_py.py` 里定位 `class TtestResult` 的真实定义处（可用 `Grep` 搜 `class TtestResult`）。
3. 对比两处，确认 `_result_classes.py` 没有 `class TtestResult:` 的定义体，只有 `import`。

**需要观察的现象**：`TtestResult` 的真正定义（带属性、方法）在 `_stats_py.py`，而 `_result_classes.py` 里只有一行导入。

**预期结果**：你会清楚地看到「文档聚合 ≠ 实现定义」，进一步印证 stats 一以贯之的分层风格。

#### 4.5.5 小练习与答案

**练习 1**：用户应该写 `from scipy.stats import TtestResult` 然后手动 `TtestResult(...)` 吗？

> **答案**：不应该。如 `__init__.py` 第 585-589 行的警告所述，这些结果类是私有的，不支持用户直接 import 和实例化。正确做法是调用 `scipy.stats.ttest_ind(...)` 等函数，**接收**它返回的结果对象，再访问其 `.statistic`、`.pvalue`、`.confidence_interval()` 等属性/方法。

**练习 2**：`tests/common_tests.py` 这类共享测试文件为什么重要？

> **答案**：stats 有上百个分布和几十个检验函数，它们共享大量相同的行为契约（如都接受 `axis`/`nan_policy`、都返回特定结构）。`common_tests.py` 把这些共同契约抽成可复用的测试基类/混入，避免在每个分布的测试里重复编写，既保证一致性又降低维护成本。这是大型统计库保证数值正确性的关键工程手段。

---

## 5. 综合实践

**任务**：制作一份《scipy/stats 目录宪法解读报告》。

请综合本讲全部内容，完成下面三张表，并把它们写进一份简短笔记：

1. **编译扩展清单**：依据 [`meson.build`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/meson.build) 的 `extension_module` 声明（4.3 节）和三个子包的 `meson.build`（4.4 节），列出 stats 编译出的全部扩展模块名、其源文件、使用的语言（Cython / Cython++ / Pythran / C）。

2. **代表性功能的源码定位**：对下面 5 个用户常用对象，分别给出它们「真正被定义」的源码文件（提示：从 [`__init__.py` 导入区](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/__init__.py#L598-L631) 反查）：
   - `norm`（正态分布）
   - `ttest_ind`（两样本 t 检验）
   - `gaussian_kde`（核密度估计）
   - `multivariate_normal`（多元正态）
   - `sobol_indices`（灵敏度分析）

3. **文件归类决策**：对 `_sobol.pyx`、`_continuous_distns.py`、`stats.py`、`_sobol_direction_numbers.npz`、`_result_classes.py` 这 5 个文件，逐个说明它属于 4.1 节四类文件中的哪一类、依据是 `meson.build` 的哪条指令或 `__init__.py` 的哪段导入。

**参考答案要点**：

- 第 1 表应包含主目录 7 个扩展（`_stats`、`_ansari_swilk_statistics`、`_sobol`、`_qmc_cy`、`_biasedurn`、`_stats_pythran`、`_qmvnt_cy`）+ 子包 3 个（`levyst`、`unuran_wrapper`、`rcont`）。
- 第 2 表参考定位：`norm` 来自 `_continuous_distns.py`（经 `distributions.py` 壳聚合）；`ttest_ind` 来自 `_stats_py.py`；`gaussian_kde` 来自 `_kde.py`；`multivariate_normal` 来自 `_multivariate.py`；`sobol_indices` 来自 `_sensitivity_analysis.py`。
- 第 3 表归类：`_sobol.pyx`→编译扩展源；`_continuous_distns.py`→子领域实现（纯 Python）；`stats.py`→遗留 shim（弃用）；`_sobol_direction_numbers.npz`→数据文件；`_result_classes.py`→文档专用聚合（纯 Python，但不属用户 API）。

---

## 6. 本讲小结

- `scipy/stats/` 的文件可分为四类：**公共入口/聚合壳**、**子领域实现（`_xxx.py`）**、**编译扩展源（`.pyx`/`.c`/`.cpp`）**、**子包**；下划线前缀约定「私有实现」。
- [`__init__.py`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/__init__.py) 是纯路由层：顶部文档 + 一连串 `from ._xxx import`，再用 `__all__ = [s for s in dir() if not s.startswith("_")]` 自动生成公开 API——**读导入区等于读 API 清单**。
- [`meson.build`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/meson.build) 用 `extension_module` 编译扩展（主目录 7 个）、用 `install_sources` 原样安装纯 Python 与数据文件——**搜文件名落在哪条指令里，就知道它是编译产物还是纯 Python**。
- 三个子包 `_levy_stable`/`_unuran`/`_rcont` 各自产出 1 个扩展（`levyst`/`unuran_wrapper`/`rcont`），体现「C/C++ 内核 + Cython 外壳」的分层。
- 部分短名字文件（`stats.py`、`kde.py` 等）是 v2.0 将移除的**弃用 shim**，仅为兼容老代码而保留。
- `_result_classes.py` 是「文档专用聚合」，把散落各处的私有结果类集中起来供 Sphinx 生成文档，本身不定义任何类。

---

## 7. 下一步学习建议

掌握了目录结构后，建议下一步：

1. **学 u1-l3（如何导入与运行）**：亲手 `import scipy.stats`，跑通第一个统计函数与 `stats.test()`，把「目录认知」变成「运行体验」。
2. **挑一个实现文件通读**：从体量较小、职责单一的文件入手，例如 [`_variation.py`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_variation.py) 或 [`_common.py`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_common.py)，熟悉 stats 内部的代码风格。
3. **进入 u2（描述性统计）**：正式开始读 `_stats_py.py` 里的 `describe`、`gmean` 等函数，把本讲建立的「文件定位能力」用于追踪真实算法。
4. **若对构建感兴趣**：对比读 [`meson.build`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/meson.build) 与 SciPy 顶层构建配置，理解 `extension_module`、`install_sources`、`subdir` 如何在整个项目里协同——这是后续 u17-l1（Cython 扩展）的预备知识。
