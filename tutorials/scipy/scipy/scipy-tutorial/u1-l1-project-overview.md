# SciPy 项目总览与生态定位

## 1. 本讲目标

本讲是整套 SciPy 学习手册的第一篇，面向「听说过 SciPy、但从没读过它源码」的读者。学完本讲后，你应该能够：

- 说清楚 SciPy **是什么**、**解决什么问题**，以及它在整个 Python 科学计算生态里处在什么位置。
- 说清楚 SciPy 与 **NumPy** 的关系：谁依赖谁、谁负责什么。
- 在不查文档的前提下，**背出 SciPy 的 17 个公开子包**，并能用一句话描述每个子包的用途。
- 看懂 `scipy/__init__.py` 这一个文件：它是整个库的「总开关」，负责导入校验、版本检查、延迟加载。
- 在自己的电脑上跑通一个最小实践：安装 SciPy、循环导入全部子包、读取版本号。

本讲**不会**深入任何算法（积分、优化、线性代数等留到后面单元），只建立全局认知。这一步很重要——后面所有讲义都建立在这个「地图」之上。

## 2. 前置知识

本讲几乎不需要数学背景，但你需要了解以下几个概念：

### 2.1 什么是「科学计算」

科学计算（scientific computing）指的是用计算机来**做数学**：解方程、求积分、拟合曲线、处理信号、做统计分析、解微分方程等等。这些任务在物理、工程、金融、生物、机器学习等领域无处不在。

举例：

- 你有一组带噪声的实验数据点，想把它们拟合成一条光滑曲线 → **插值 / 拟合**。
- 你要解一个大型线性方程组 \(Ax = b\) → **线性代数**。
- 你要模拟一个弹簧振子的运动 → **常微分方程求解**。
- 你要对一段音频做滤波、看它的频率成分 → **信号处理 + 傅里叶变换**。

这些事情每一件单独写都很麻烦，而且对**速度**要求很高（数据量大）。SciPy 就是把这些「常用但难写、且需要高性能」的数值计算封装成一组稳定、好用的 Python 接口。

### 2.2 什么是 NumPy 数组

NumPy 提供了一个核心数据结构 `ndarray`（N 维数组），它是一块**连续内存**里的同类型数值集合，可以用下标高效访问，并支持**向量化运算**（一次操作整个数组，而不是逐元素循环）。

例如，给定两个等长数组 `a` 和 `b`，`a + b` 会逐元素相加，且底层是 C 语言级别的循环，比 Python 的 `for` 循环快得多。

> 一句话记忆：**NumPy 提供「数组」这个数据结构，SciPy 在这之上提供「算法」。**

### 2.3 什么是「子包」（subpackage）

一个 Python 项目可以拆成很多个「包」（package，就是一个目录，里面有 `__init__.py`）。SciPy 把不同领域的算法分到不同的子目录里，每个目录就是一个子包。例如：

- `scipy/linalg/` —— 线性代数
- `scipy/optimize/` —— 优化
- `scipy/stats/` —— 统计

你只需要 `import scipy.linalg` 就能用线性代数的函数。这种「按领域分目录」的设计，让你不用一次性加载整个库。

### 2.4 什么是「延迟导入」（lazy import）

「延迟导入」指的是：当用户执行 `import scipy` 时，**并不立刻**把 `scipy.linalg`、`scipy.stats` 等所有子包全部加载进内存；只有当用户真正用到某个子包（比如写了 `scipy.linalg.solve(...)`）时，才去加载它。

好处是：`import scipy` 很快、很省内存。我们会在第 4.3 节看到 SciPy 具体怎么实现它。

## 3. 本讲源码地图

本讲只看「最顶层」的三个文件，它们决定了 SciPy 给外界的第一印象：

| 文件 | 作用 | 你要从中读到什么 |
| --- | --- | --- |
| `README.rst` | 项目的「门面」说明，面向新用户和贡献者 | SciPy 的官方定位、它和 NumPy 的关系、安装入口 |
| `scipy/__init__.py` | 整个库的**包入口文件**，每次 `import scipy` 都会执行它 | 17 个子包列表、公共 API、版本检查、延迟导入机制 |
| `doc/source/index.rst` | 官方文档首页（用 reStructuredText 写成） | 文档的整体结构：用户指南 / API 参考 / 构建指南 / 开发指南 |

> 提示：`.rst` 是 reStructuredText 的缩写，一种类似 Markdown 的标记语言，SciPy 官方文档用它来写。`__init__.py` 是 Python 包的「身份证」文件——只要一个目录里有 `__init__.py`，Python 就把它当成一个包。

下面分三个最小模块逐一精读。

## 4. 核心概念与源码讲解

### 4.1 SciPy 是什么：定位与核心价值

#### 4.1.1 概念说明

先给一个一句话定义：

> **SciPy 是一个开源的、面向数学/科学/工程的 Python 计算库，它把一组常用且高性能的数值算法（统计、优化、积分、线性代数、傅里叶变换、信号与图像处理、ODE 求解器等）封装成易用的 Python 接口。**

这个定义里有几个关键词值得拆开理解：

- **开源（open-source）**：源码公开、免费、可自由使用，社区共同维护。
- **面向数学/科学/工程**：它的目标用户是「要算数」的人，不是通用 Web 开发。
- **高性能**：底层算法大量用 C、C++、Fortran 实现，再用 Cython / f2py 包装成 Python 可调用的形式。所以「写起来像 Python，跑起来像 C」。
- **易用的接口**：你不用关心底层是 Fortran 还是 C，只要调函数、传 NumPy 数组就行。

**SciPy 解决的核心问题**：让科研和工程师不必「每次都重新造轮子」。想求一个积分？`scipy.integrate.quad` 一行搞定。想解一个最小化问题？`scipy.optimize.minimize` 一行搞定。这些函数都经过了严格测试、长期维护，并且性能经过调优。

#### 4.1.2 核心流程

从「用户视角」看，使用 SciPy 的典型流程是：

1. 准备好数据，通常是一个 **NumPy 数组**（`np.ndarray`）。
2. 根据问题领域，选择对应的**子包**（统计找 `stats`、优化找 `optimize`……）。
3. 调用子包里的某个**函数**，传入数组，得到结果（通常也是数组）。
4. 解释、可视化或保存结果。

用伪代码表示：

```
输入: 原始数据（列表 / 文件 / 传感器读数）
  ↓ 转成 NumPy 数组 np.array(...)
所属领域? → 选择 scipy.<子包>
  ↓ 调用对应函数，如 scipy.optimize.minimize(f, x0)
输出: 结果（标量 / 数组 / 对象）
```

这个「数据是 NumPy 数组 → 进 SciPy 函数 → 出结果」的模式贯穿整个库，也是后面所有讲义的主线。

#### 4.1.3 源码精读

我们先看项目「门面」`README.rst` 里对 SciPy 的官方定义：

[README.rst:25-28](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/README.rst#L25-L28) — 这三行是 SciPy 的官方一句话定位：一个面向数学、科学、工程的开源软件，包含统计、优化、积分、线性代数、傅里叶变换、信号与图像处理、ODE 求解器等模块。

紧接着，README 说明了 SciPy 与 NumPy 的关系：

[README.rst:43-50](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/README.rst#L43-L50) — 这一段明确写出：SciPy 是**建立在 NumPy 数组之上**的（"built to work with NumPy arrays"），并提供许多易用且高效的数值例程（如数值积分、优化）。两者组合可以在所有主流操作系统上运行、安装快捷、免费。

这段话直接回答了本讲的一个核心问题：「SciPy 和 NumPy 是什么关系？」答案就是：**NumPy 是地基（数组），SciPy 是地基上的楼（算法）**。没有 NumPy，SciPy 无法工作。

README 还给出了安装入口：

[README.rst:52-53](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/README.rst#L52-L53) — 指向官方安装指南 `https://scipy.org/install/`，这是新用户安装 SciPy 的起点。

再来看官方文档首页 `doc/source/index.rst`，它用同样的措辞定位 SciPy，并展示了文档的四大板块：

[doc/source/index.rst:22-23](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/doc/source/index.rst#L22-L23) — 文档首页对 SciPy 的定位描述，与 README 一致。

[doc/source/index.rst:25-102](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/doc/source/index.rst#L25-L102) — 用一个 2×2 的卡片网格（grid）展示了 SciPy 文档的四大入口：**User guide（用户指南）**、**API reference（接口参考）**、**Building from source（从源码构建）**、**Developer guide（开发指南）**。后续学习时，遇到不懂的函数优先查「API reference」，想理解概念优先查「User guide」。

#### 4.1.4 代码实践

**实践目标**：亲手从 README 和文档首页找到「SciPy 是什么」和「怎么安装」的官方说法。

**操作步骤**：

1. 打开本仓库根目录的 `README.rst`，找到第 25–28 行，把那句英文定位翻译成中文，写在本子或笔记里。
2. 在 `README.rst` 第 43–50 行，划出描述「SciPy 与 NumPy 关系」的那一句。
3. 打开 `doc/source/index.rst`，数一下首页一共展示了几个文档入口卡片（应该是 4 个）。
4. 访问 README 中给出的安装链接 `https://scipy.org/install/`，阅读官方推荐的安装方式（通常是 `pip install scipy` 或 `conda install scipy`）。

**需要观察的现象**：

- README 的措辞与文档首页的措辞高度一致——这说明项目在不同位置对「自己是什么」有统一表述。
- 文档被分成了「概念（用户指南）」和「接口（API 参考）」两条线，这是科学计算库的常见文档组织方式。

**预期结果**：你能用自己的话，向一个没听过 SciPy 的人解释「SciPy 是什么、装在哪、和 NumPy 什么关系」。

> 说明：以上是源码阅读型实践，不需要运行代码。安装步骤的实际运行结果取决于你的操作系统和 Python 环境，可标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：SciPy 的官方发音是什么？它和「SciPy 库」是同一个东西吗？

> **答案**：官方发音是 **"Sigh Pie"**（参见 [README.rst:25](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/README.rst#L25)）。它就是本仓库所实现的那个 Python 科学计算库本身。

**练习 2**：如果 NumPy 突然消失了，SciPy 还能正常工作吗？为什么？

> **答案**：不能。因为 SciPy 是「built to work with NumPy arrays」（[README.rst:43-44](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/README.rst#L43-L44)），它的输入输出都基于 `ndarray`。NumPy 是 SciPy 的硬依赖（hard dependency）。

---

### 4.2 子包功能地图：17 个公开子包

#### 4.2.1 概念说明

SciPy 不像普通的小库那样「一个文件搞定」，它是一个**巨型库**，按领域拆成了 **17 个公开子包**。每个子包负责一类问题。

为什么不放在一个命名空间下？因为：

1. **关注点分离**：做统计的人不想被迫加载信号处理的代码。
2. **控制内存与启动时间**：配合延迟导入，只加载你用到的部分。
3. **便于维护**：不同子包由不同领域的专家维护，互不干扰。

记住这 17 个子包，等于在脑子里装了一张「SciPy 地图」，遇到问题能快速定位该去哪个子包找工具。

#### 4.2.2 核心流程

子包的组织方式可以理解为两层：

```
scipy (顶层包)
 ├── cluster      聚类
 ├── constants    物理常数
 ├── datasets     示例数据集
 ├── ...          （共 17 个）
 └── stats        统计
```

用户使用时按需导入：

```python
import scipy.stats          # 只加载统计
import scipy.optimize       # 只加载优化
```

每个子包内部又有自己的结构（函数、类、甚至子-子包，如 `scipy.sparse.linalg`），但这些细节留到后续讲义。本讲只看「顶层 17 个」这张地图。

> 一个常被忽略的细节：公开子包的**列表本身**就是源码里写死的。也就是说，「SciPy 有哪些公开子包」这个问题，答案就藏在 `scipy/__init__.py` 里。下一小节我们直接读它。

#### 4.2.3 源码精读

`scipy/__init__.py` 的文件开头是一段**文档字符串（docstring）**，它在 ``Subpackages`` 标题下列出了所有子包及其一句话用途：

[scipy/__init__.py:8-28](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/__init__.py#L8-L28) — 文件 docstring 里的「Subpackages」段落，逐行列出每个子包名和它的功能描述。这是子包地图的「权威清单」。

但 docstring 只是给人看的注释，机器真正用的是下面这个 `submodules` 列表：

[scipy/__init__.py:95-113](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/__init__.py#L95-L113) — `submodules` 这个 Python 列表，把 17 个子包名以字符串形式存下来，是延迟导入逻辑实际遍历的数据源。

> 这两处必须保持一致：docstring 写给文档生成器和人看，`submodules` 写给 `__getattr__` 逻辑用。如果你将来要给 SciPy 加一个新子包，这两处（加上 `__all__`）都得改。

下面这张表把 17 个子包整理成「功能地图」。描述直接取自上面 docstring 的原文翻译：

| 子包 | 一句话用途 | 典型用途举例 |
| --- | --- | --- |
| `cluster` | 向量量化 / K-means 聚类 | 把一组点分成若干簇 |
| `constants` | 物理与数学常数及单位 | 光速、普朗克常数 |
| `datasets` | 数据集获取方法 | 加载内置示例图片 |
| `differentiate` | 有限差分数值求导 | 数值上计算函数导数 |
| `fft` | 离散傅里叶变换 | 分析信号的频率成分 |
| `fftpack` | 旧版离散傅里叶变换（legacy） | 兼容老代码，新代码请用 `fft` |
| `integrate` | 积分例程 | 数值积分、解 ODE |
| `interpolate` | 插值工具 | 由离散数据点构造光滑曲线 |
| `io` | 数据输入输出 | 读写 MATLAB / Matrix Market / wav 文件 |
| `linalg` | 线性代数例程 | 解方程、矩阵分解 |
| `ndimage` | 多维图像处理 | 图像滤波、形态学、测量 |
| `optimize` | 优化工具 | 求最小值、曲线拟合、线性规划 |
| `signal` | 信号处理工具 | 滤波器设计、卷积 |
| `sparse` | 稀疏矩阵 | 大规模稀疏数据的存储与运算 |
| `spatial` | 空间数据结构与算法 | KD-Tree、凸包、Delaunay 三角化 |
| `special` | 特殊函数 | 贝塞尔函数、伽马函数 |
| `stats` | 统计函数 | 概率分布、假设检验 |

**关于「为什么是 17 个」的一个历史细节**：老版本的 SciPy 曾有第 18 个公开子包 `scipy.odr`（正交距离回归）。源码里至今保留了对它的处理：

[scipy/__init__.py:130-134](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/__init__.py#L130-L134) — `__getattr__` 里对 `odr` 的特殊处理：它在 SciPy 1.17 被弃用（deprecated），在 **1.19 被移除（removed）**，并提示用户改用独立的 `odrpack` 包。这就是当前公开子包数稳定在 17 的原因。这个例子也展示了 SciPy 如何管理「公共 API 的生命周期」——先弃用、再删除，并给出替代方案。

#### 4.2.4 代码实践

**实践目标**：写一个脚本，循环导入全部 17 个子包，确认它们都能正常加载，并打印每个子包的来源文件路径。

**操作步骤**：

1. 确认已安装 SciPy（`pip install scipy`）。新建文件 `explore_subpackages.py`：

   ```python
   # 示例代码
   import importlib
   import scipy

   print("scipy 版本:", scipy.__version__)

   # 这 17 个名字来自 scipy/__init__.py 的 submodules 列表
   submodules = [
       'cluster', 'constants', 'datasets', 'differentiate',
       'fft', 'fftpack', 'integrate', 'interpolate', 'io',
       'linalg', 'ndimage', 'optimize', 'signal', 'sparse',
       'spatial', 'special', 'stats',
   ]

   for name in submodules:
       mod = importlib.import_module(f'scipy.{name}')
       print(f"{name:14s} -> {getattr(mod, '__file__', '(内置/无 __file__)')}")
   ```

2. 运行：`python explore_subpackages.py`。

**需要观察的现象**：

- 17 个子包全部打印出一行，没有抛出 `ImportError`。
- 每个子包的 `__file__` 指向 `site-packages/scipy/<name>/__init__.py` 之类的路径。
- 第一行打印的版本号字符串（具体值取决于你安装的版本）。

**预期结果**：你得到一份「17 个子包 → 安装位置」的对照表，确认本机 SciPy 完整可用。如果某个子包报错，多半是安装不完整，需要重装。

> 说明：运行结果依赖你的本地环境。若脚本无法运行，可标注「待本地验证」，并改为只阅读 `scipy/__init__.py:95-113` 的 `submodules` 列表来核对 17 个名字。

#### 4.2.5 小练习与答案

**练习 1**：下面两个子包都和「傅里叶变换」有关：`fft` 和 `fftpack`。新代码应该用哪个？为什么还留着另一个？

> **答案**：新代码用 `fft`。`fftpack` 是**旧版（legacy）**实现，保留是为了兼容老代码（见 [scipy/__init__.py:16-17](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/__init__.py#L16-L17) 的描述对比）。

**练习 2**：`scipy.odr` 还能用吗？如果不能，官方建议用什么替代？

> **答案**：不能。它已在 SciPy 1.19 被移除（[scipy/__init__.py:130-134](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/__init__.py#L130-L134)），官方建议改用独立的 `odrpack` 包。

**练习 3**：你要做「求一个函数的最小值」，应该去哪个子包？

> **答案**：`scipy.optimize`（用途是 "Optimization Tools"）。

---

### 4.3 顶层包入口：公共 API、版本校验与延迟导入

#### 4.3.1 概念说明

`scipy/__init__.py` 这个文件虽然不长，但它承担了「库总开关」的职责。每次你敲下 `import scipy`，Python 都会**完整执行**一遍这个文件。它做了几件关键的事：

1. **导入并校验 NumPy 版本**：SciPy 只兼容特定版本范围内的 NumPy，版本不符会警告。
2. **校验编译扩展模块是否可用**：SciPy 大量功能依赖 C/Fortran 编译出的扩展模块（`.so`/`.pyd`），如果安装损坏，这里会给出清晰报错。
3. **暴露公共 API**：在 `scipy` 顶层命名空间里直接提供少数几个对象（如 `__version__`、`test`、`show_config`）。
4. **实现延迟导入**：通过 `__getattr__` 钩子，让 `scipy.linalg` 这种访问「用到才加载」。

理解这四点，你就理解了「`import scipy` 到底发生了什么」。

#### 4.3.2 核心流程

`import scipy` 时的执行流程（对应源码顺序）：

```
1. from numpy import __version__ as __numpy_version__
        ↓ 拿到本机 NumPy 版本
2. from scipy.__config__ import show as show_config
        ↓ 加载构建配置（BLAS/LAPACK 来源等）
3. from scipy.version import version as __version__
        ↓ 拿到 SciPy 版本字符串
4. NumPy 版本范围检查 (2.0.0 <= 版本 < 9.9.99)
        ↓ 不在范围内 → 发出 UserWarning
5. from scipy._lib._ccallback import LowLevelCallable
        ↓ 第一个扩展模块导入；若失败说明安装损坏
6. from scipy._lib._testutils import PytestTester  →  test
        ↓ 提供 scipy.test() 入口
7. 定义 submodules / __all__
8. 定义 __getattr__  →  延迟加载子包
```

其中第 8 步是「延迟导入」的关键。Python 在访问一个模块里**不存在的属性**时，会去调用模块级的 `__getattr__(name)` 函数（这是 PEP 562 引入的特性）。SciPy 利用它：当你写 `scipy.linalg` 时，由于顶层并没有真正 `import linalg`，Python 就调用 `__getattr__('linalg')`，它发现 `linalg` 在 `submodules` 列表里，于是动态 `importlib.import_module('scipy.linalg')` 并返回。

好处：`import scipy` 时这 17 个子包一个都没加载，只有你点名的那个才会被加载。

#### 4.3.3 源码精读

**第 1 步：拿到 NumPy 版本并导入构建配置。**

[scipy/__init__.py:41-52](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/__init__.py#L41-L52) — 先 `import importlib`（供后面延迟导入用），然后从 NumPy 取版本号；接着尝试 `from scipy.__config__ import show as show_config`。这里的 `try/except` 还有一个贴心的报错：如果你**在 SciPy 源码目录里**直接 `import scipy`，会因为找不到编译产物而失败，于是给出明确提示「请退出源码目录再启动 Python」。

**第 2 步：拿到 SciPy 版本。**

[scipy/__init__.py:55](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/__init__.py#L55) — `from scipy.version import version as __version__`。

> 注意：`scipy/version.py` 这个文件**不在 git 仓库里**，而是在**构建时由 meson-python 动态生成**。所以你在源码树里看不到它，只有构建/安装后才会存在。这解释了为什么本机 `scipy.__version__` 的具体值取决于你装的是哪个发行版本。

**第 3 步：NumPy 版本范围检查。**

[scipy/__init__.py:63-74](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/__init__.py#L63-L74) — SciPy 用一个内部 vendored 的版本解析器（`scipy/_external/packaging_version`）来比较版本号。这里规定 `np_minversion = '2.0.0'`、`np_maxversion = '9.9.99'`：即本版 SciPy 要求 NumPy 版本 \(\geq 2.0.0\) 且 \(< 9.9.99\)。超出范围会发出 `UserWarning`。这是「SciPy 依赖 NumPy」在代码层面的硬约束。

**第 4 步：校验编译扩展模块。**

[scipy/__init__.py:77-87](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/__init__.py#L77-L87) — 注释里明确说：这是 SciPy 内部**第一次导入编译扩展模块**（`scipy._lib._ccallback.LowLevelCallable`）。如果安装损坏（比如编译扩展缺失），会在这里失败，于是给出「安装似乎损坏，请重装」的友好报错。这是一种「尽早失败、清晰报错」的工程实践。

**第 5 步：提供 `test` 入口。**

[scipy/__init__.py:90-92](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/__init__.py#L90-L92) — 从 `_lib._testutils` 导入 `PytestTester`，实例化后赋给顶层名字 `test`。所以你可以直接 `scipy.test()` 来跑 SciPy 自带的测试。之后用 `del PytestTester` 把类本身从命名空间删掉，只留下实例 `test`，保持顶层干净。

**第 6 步：公共 API 定义。**

[scipy/__init__.py:115-120](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/__init__.py#L115-L120) — `__all__` 把「17 个子包名」加上 4 个顶层对象（`LowLevelCallable`、`test`、`show_config`、`__version__`）合在一起，构成 SciPy 顶层命名空间的**完整公共 API**。docstring 第 30–37 行也对这 4 个顶层对象做了说明（[scipy/__init__.py:30-37](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/__init__.py#L30-L37)）。

**第 7 步：延迟导入钩子。**

[scipy/__init__.py:127-141](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/__init__.py#L127-L141) — 这是延迟导入的核心。函数逻辑：
- 若 `name` 在 `submodules` 里 → `importlib.import_module(f'scipy.{name}')` 动态加载并返回；
- 若 `name == "odr"` → 抛出明确的 `AttributeError`，说明已移除；
- 否则 → 尝试从 `globals()` 取（覆盖 `__version__` 等顶层对象），取不到就抛 `AttributeError`。

正因为有它，`import scipy` 才又快又省内存。

#### 4.3.4 代码实践

**实践目标**：亲手验证「延迟导入」和「版本校验」这两个机制，看懂 `import scipy` 背后发生了什么。

**操作步骤**：

1. **验证延迟导入**：在一个全新的 Python 解释器里执行：

   ```python
   # 示例代码
   import sys
   import scipy                # 只导入顶层，不应加载子包

   before = set(sys.modules)
   _ = scipy.linalg            # 第一次访问，触发延迟导入
   after = set(sys.modules)

   newly_loaded = sorted(m for m in (after - before) if m.startswith("scipy.linalg"))
   print("访问 scipy.linalg 后新加载的模块数:", len(newly_loaded))
   print(newly_loaded[:5], "...")   # 只看前几个，避免刷屏
   ```

2. **读取版本与构建配置**：

   ```python
   # 示例代码
   import scipy
   print("SciPy 版本:", scipy.__version__)
   scipy.show_config()        # 打印 BLAS/LAPACK 等构建信息
   ```

3. **触发 odr 的友好报错**（验证第 4.2 节的弃用逻辑）：

   ```python
   # 示例代码
   import scipy
   scipy.odr   # 预期抛 AttributeError，说明 odr 已移除
   ```

**需要观察的现象**：

- 第 1 步：在 `import scipy` 之后、访问 `scipy.linalg` 之前，`sys.modules` 里应该**没有** `scipy.linalg`；访问之后才会出现一批 `scipy.linalg.*` 模块。这就是「用到才加载」。
- 第 2 步：`show_config()` 会打印一段关于 BLAS/LAPACK 来源（如 OpenBLAS）的信息，具体内容「待本地验证」。
- 第 3 步：访问 `scipy.odr` 抛出 `AttributeError`，错误信息提到 1.17 弃用、1.19 移除。

**预期结果**：你能解释「为什么 `import scipy` 很快」，并亲眼看到延迟导入的「按需加载」效果。

> 说明：以上脚本是示例代码，运行结果依赖本机环境。`show_config()` 的具体输出与你安装 SciPy 时的 BLAS/LAPACK 后端有关，可标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`scipy.__version__` 的值是从哪个文件来的？为什么你在 git 源码树里找不到这个文件？

> **答案**：来自 `from scipy.version import version`（[scipy/__init__.py:55](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/__init__.py#L55)）。`scipy/version.py` 是**构建时由 meson-python 动态生成**的，所以不在 git 仓库里。

**练习 2**：本版 SciPy 要求 NumPy 版本在什么范围？超出会怎样？

> **答案**：要求 \(\geq 2.0.0\) 且 \(< 9.9.99\)（[scipy/__init__.py:64-73](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/__init__.py#L64-L73)）。超出会发出 `UserWarning`，但不会直接崩溃。

**练习 3**：为什么 `scipy._lib._ccallback.LowLevelCallable` 的导入被特意注释成「第一次导入扩展模块」？

> **答案**：因为这是 `import scipy` 流程中**第一个真正依赖编译产物**的导入（[scipy/__init__.py:77-87](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/scipy/__init__.py#L77-L87)）。如果安装损坏（扩展模块缺失），会在这里第一个失败，于是可以给出「安装损坏，请重装」的清晰报错，而不是让用户在后续某个莫名其妙的地方踩错。

---

## 5. 综合实践

把本讲的三个模块串起来，完成下面这个「SciPy 全景侦察」小任务：

**任务**：编写一个脚本 `scipy_recon.py`，输出一份「SciPy 全景报告」，包含以下信息：

1. **库的定位**：打印 README 里对 SciPy 的一句话定位（你可以手动把 [README.rst:25-28](https://github.com/scipy/scipy/blob/814922d57caa1ad6e3410ba65102eb7b9b080dd3/README.rst#L25-L28) 的内容作为字符串写进去）。
2. **版本信息**：打印 `scipy.__version__` 和 NumPy 版本 `numpy.__version__`，并判断当前 NumPy 是否落在 SciPy 要求的 `[2.0.0, 9.9.99)` 区间内。
3. **子包地图**：遍历 `scipy.__init__.py` 里的 `submodules` 列表（共 17 个），对每个子包：
   - 触发延迟导入（`importlib.import_module`）；
   - 打印「子包名 → 你的一句话用途」（用途可以参考本讲第 4.2.3 节的表格）。
4. **延迟导入验证**：在遍历前后比较 `sys.modules`，报告「这次遍历一共新加载了多少个 `scipy.*` 模块」。

**参考骨架**（示例代码，用途描述请你对照表格自行补全）：

```python
# 示例代码
import importlib
import sys

import numpy as np
import scipy
from scipy._external.packaging_version.version import parse, Version

# 1. 定位
POSITIONING = (
    'SciPy is an open-source software for mathematics, science, '
    'and engineering.'
)
print("【定位】", POSITIONING)

# 2. 版本信息
print("【版本】SciPy:", scipy.__version__, "| NumPy:", np.__version__)
ok = Version('2.0.0') <= parse(np.__version__) < Version('9.9.99')
print("【NumPy 版本是否合格】", ok)

# 3. 子包地图 + 4. 延迟导入验证
before = set(sys.modules)
submodules = scipy.__init__.__doc__  # 仅示意；实际请用下方写死的列表
submodules = [
    'cluster', 'constants', 'datasets', 'differentiate',
    'fft', 'fftpack', 'integrate', 'interpolate', 'io',
    'linalg', 'ndimage', 'optimize', 'signal', 'sparse',
    'spatial', 'special', 'stats',
]
print("【子包地图】")
for name in submodules:
    importlib.import_module(f'scipy.{name}')
    print(f"  {name}")
after = set(sys.modules)
loaded = sorted(m for m in (after - before) if m.startswith('scipy.'))
print(f"【延迟导入】本次共新加载 {len(loaded)} 个 scipy.* 模块")
```

**检查清单**：

- [ ] 报告第 1 部分的定位与 README 一致。
- [ ] 报告第 2 部分能正确判断 NumPy 是否合格。
- [ ] 报告第 3 部分列出 17 个子包，无 `ImportError`。
- [ ] 报告第 4 部分给出一个非零的「新加载模块数」，证明子包确实是延迟加载的。

> 说明：脚本中用到的 `scipy._external.packaging_version` 是 SciPy 内部 vendored 的版本解析器，仅为演示「和源码里一样的版本比较方式」。生产代码里你通常用 `packaging.version` 即可。运行结果「待本地验证」。

完成这个综合实践后，你就真正建立了本讲开篇承诺的「全局地图」。

## 6. 本讲小结

- **SciPy 是什么**：一个开源的、面向数学/科学/工程的 Python 计算库，把统计、优化、积分、线性代数、傅里叶变换、信号/图像处理、ODE 求解等高性能数值算法封装成易用的 Python 接口。
- **与 NumPy 的关系**：NumPy 是地基（`ndarray` 数组），SciPy 是地基上的楼（算法）。SciPy 硬依赖 NumPy，并在 `import` 时校验其版本落在 `[2.0.0, 9.9.99)`。
- **17 个公开子包**：`cluster / constants / datasets / differentiate / fft / fftpack / integrate / interpolate / io / linalg / ndimage / optimize / signal / sparse / spatial / special / stats`，每个负责一类问题；旧的第 18 个 `odr` 已在 1.19 移除。
- **包入口 `scipy/__init__.py`** 是「库总开关」：负责导入配置、版本校验、扩展模块健康检查、暴露公共 API（`__version__` / `test` / `show_config` / `LowLevelCallable`）。
- **延迟导入**：通过模块级 `__getattr__` 钩子实现「用到才加载」，让 `import scipy` 又快又省内存。
- **权威清单在哪里**：子包清单同时写在 docstring（给人/文档看）和 `submodules` 列表（给逻辑用）两处，二者必须一致。

## 7. 下一步学习建议

本讲只建立了「全景地图」。建议接下来按以下顺序继续：

1. **下一讲 `u1-l2 构建系统与从源码编译运行`**：搞清楚 SciPy 这种「Python + C/C++/Cython/Fortran」多语言项目是怎么被编译出来的，理解 `pyproject.toml` 和 `meson.build`。这对后面读任何子包的源码都是前置知识。
2. **再下一讲 `u1-l3 目录结构与包入口、延迟导入`**：在已经理解 `scipy/__init__.py` 的基础上，进一步看包的整体目录组织和 `PytestTester` 等机制。
3. **`u1-l4 开发工作流`**：学会用 `spin` 命令构建、测试、跑文档，为动手实验做好准备。
4. **阅读建议**：在进入任何具体子包之前，先通读第 2 单元「共享基础设施 `_lib`」——因为后续几乎每个子包都依赖 `_lib` 里的工具函数、`uarray` 后端分发和 Array API 抽象。
