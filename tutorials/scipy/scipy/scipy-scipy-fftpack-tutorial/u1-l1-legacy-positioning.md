# 项目定位与遗留模块身份

## 1. 本讲目标

本讲是整本《scipy.fftpack 学习手册》的第一篇，目标是帮你在动手写任何代码之前，先搞清楚一件事：**`scipy.fftpack` 到底是什么，它为什么还在，新代码到底该不该用它。**

学完本讲，你应该能够：

- 说清楚 `scipy.fftpack` 的「遗留（legacy）」身份是怎么在源码里被声明的。
- 知道官方推荐新代码改用 `scipy.fft`，并理解两者之间的关系。
- 区分两个容易被混淆的概念：**文档级的「遗留」标记** 和 **运行时的「弃用警告（DeprecationWarning）」**。
- 读懂 `scipy/fftpack/__init__.py` 这个模块的「名片」（docstring）和它的公开 API 是怎么组织的。

> 本讲只聚焦定位与身份，不深入具体变换函数（fft、dct 等）的算法实现。那些是后续讲义的内容。

## 2. 前置知识

### 2.1 什么是傅里叶变换（一句话版）

傅里叶变换把一个信号从「时域 / 空域」转换到「频域」，也就是把一段复杂的波形拆解成一组不同频率的正弦波叠加。离散傅里叶变换（DFT）作用于有限长度的离散序列，其定义为：

\[
X(k) = \sum_{n=0}^{N-1} x(n)\, e^{-2\pi i\, k n / N}, \quad k = 0, 1, \dots, N-1
\]

其中 \( i \) 是虚数单位，\( x(n) \) 是输入序列，\( X(k) \) 是第 \( k \) 个频率分量。快速傅里叶变换（FFT）就是高效计算 DFT 的一类算法。`scipy.fftpack` 正是 SciPy 提供的一组 FFT 及相关变换的接口集合。

### 2.2 什么是「遗留（legacy）」

在软件里，**legacy（遗留）** 通常指「还能用、但已经不再是首选、官方不再主推」的模块。它和「已删除」不同——遗留代码仍然存在于库里、仍然可以调用，只是文档会明确告诉你：**新项目请用更新的替代品**。

### 2.3 什么是 DeprecationWarning

`DeprecationWarning` 是 Python 标准库的一种警告类别。当一段代码被标记为「弃用」时，运行时调用它会抛出这个警告，提醒你「这个东西未来会被删掉，别再用了」。本讲后面会强调：**`scipy.fftpack` 被标为 legacy，并不等于你每次调用都会触发 DeprecationWarning**——这是一个关键区别。

## 3. 本讲源码地图

本讲几乎只围绕一个文件展开：

| 文件 | 作用 |
| --- | --- |
| `scipy/fftpack/__init__.py` | 整个 `fftpack` 包的入口文件。它包含模块文档字符串（说明模块身份与 API 清单）、`__all__` 公开 API 列表、以及把各子模块聚合导出的 `import` 语句。 |

为了讲清楚「遗留 vs 弃用警告」的区别，本讲还会**简要引用**下面两个辅助源码点（它们属于后续讲义的深入对象，这里只做定位说明，不展开实现）：

| 文件 | 在本讲的作用 |
| --- | --- |
| `scipy/fftpack/basic.py` | 一个「垫片（shim）」子模块。当你直接访问 `scipy.fftpack.basic.xxx` 时，它会触发 DeprecationWarning。 |
| `scipy/_lib/deprecation.py` | 提供 `_sub_module_deprecation` 工具函数，是上面那个警告的真正来源。 |

## 4. 核心概念与源码讲解

### 4.1 模块文档字符串：scipy.fftpack 的「名片」

#### 4.1.1 概念说明

每个 Python 模块的文件开头通常有一段用三引号包裹的字符串，称为**模块文档字符串（module docstring）**。它是这个模块的「自我介绍」：写明模块叫什么、提供哪些功能、有什么注意事项。对 `scipy.fftpack` 而言，这段 docstring 不仅是给人读的文档，还会被 SciPy 的文档构建工具（Sphinx）自动抓取，生成官方手册页面。所以**模块的身份声明（包括「我是遗留模块」）就写在 docstring 里**。

#### 4.1.2 核心流程

docstring 的组织遵循一个固定套路：

1. **标题块**：一行标题，点明模块全名。
2. **身份标记**：紧跟标题的特殊指令（本讲的 4.2 节详解）。
3. **功能分组清单**：用 `autosummary` 指令把公开函数按类别列出来。
4. **补充说明**：对容易踩坑的点给出文字提示（比如「某些函数其实来自 numpy」）。

读者拿到 `__init__.py`，第一件事就是读这段 docstring——它能让你在不看实现的情况下，快速建立对这个模块的整体认知。

#### 4.1.3 源码精读

`__init__.py` 的开头就是模块 docstring，标题已经直接点明了「Legacy」身份：

[`__init__.py:1-4`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fftpack/__init__.py#L1-L4) —— 标题为 `Legacy discrete Fourier transforms (:mod:scipy.fftpack)`，第一个词 `Legacy` 就表明这是遗留模块。

随后 docstring 用多段 `autosummary` 把功能分组列出，例如 FFT 一族：

[`__init__.py:13-31`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fftpack/__init__.py#L13-L31) —— 列出 `fft`、`ifft`、`fft2`、`rfft`、`dct`、`dst` 等核心变换函数，这是 fftpack 的主力 API。

docstring 末尾还有一句容易被忽略、但很重要的提醒：

[`__init__.py:62-63`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fftpack/__init__.py#L62-L63) —— 明确指出 `fftshift`、`ifftshift`、`fftfreq` 其实是 numpy 的函数，只是被 fftpack 重新暴露出来；官方建议直接从 `numpy` 导入它们。

#### 4.1.4 代码实践（阅读型）

1. **实践目标**：通过阅读 docstring，亲手建立 fftpack 的功能全景图。
2. **操作步骤**：
   - 打开 `scipy/fftpack/__init__.py`，只读第 1–78 行的 docstring。
   - 找出 docstring 里一共分了几个功能组（提示：注意每段 `====` 下划线分隔的标题）。
3. **需要观察的现象**：你会看到至少四组——FFTs、Differential and pseudo-differential operators、Helper functions、Convolutions。
4. **预期结果**：能说出每一组对应解决哪类问题（例如「Helper functions」提供频率轴、频谱搬移等辅助工具）。
5. **运行结果**：本实践为纯阅读，不涉及运行命令。

#### 4.1.5 小练习与答案

**练习 1**：docstring 的标题里第一个词是什么？它传递了什么信号？
> **答案**：第一个词是 `Legacy`，传递的信号是「这个模块是遗留模块，新代码应优先考虑替代品」。

**练习 2**：docstring 提到 `fftfreq` 应该优先从哪里导入？
> **答案**：优先从 `numpy` 导入（`import numpy.fft` 或 `numpy.fft.fftfreq`），因为 fftpack 只是对它做了再导出。

---

### 4.2 `.. legacy::` 指令：遗留身份的官方声明

#### 4.2.1 概念说明

`.. legacy::` 是 SciPy 文档系统（基于 Sphinx）使用的一个**自定义指令（directive）**。它写在 docstring 里，作用是给这个模块打上一个官方的「遗留」标签。被它标记的模块，在生成的官方文档里会带上一段醒目提示，告诉读者：**这个模块已被官方认定为遗留，请使用新替代品**。

对 `scipy.fftpack` 来说，官方指定的替代品就是更新、更现代的 `scipy.fft`。两者功能高度重合，但 `scipy.fft` 在设计、默认行为、性能后端上更优。

#### 4.2.2 核心流程

legacy 身份的「传导链」是这样的：

1. 源码层：开发者在 `__init__.py` 的 docstring 里写入 `.. legacy::` 指令和一句替代说明。
2. 文档层：Sphinx 构建文档时识别该指令，在 [docs.scipy.org](https://docs.scipy.org) 的 fftpack 页面顶部渲染出遗留提示横幅。
3. 读者层：用户读到提示后，知道新项目应选 `scipy.fft`。
4. 注意：这一整条链**只发生在文档层面**，不影响运行时行为（详见 4.3 节）。

#### 4.2.3 源码精读

legacy 指令紧跟在标题块之后，紧贴着一句明确的替代建议：

[`__init__.py:6-8`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fftpack/__init__.py#L6-L8) —— 第 6 行是 `.. legacy::` 指令本身；第 8 行 `New code should use :mod:`scipy.fft`.` 是核心结论：新代码请用 `scipy.fft`。

这两行是整篇讲义最重要的代码点。无论 fftpack 内部实现多复杂，对「定位」而言，这两行就是最终答案。

> 小贴士：`:mod:`scipy.fft`` 是 Sphinx 的交叉引用写法，渲染后会变成一个可点击的链接，指向 `scipy.fft` 的文档页。

#### 4.2.4 代码实践（对比型）

1. **实践目标**：亲手对比 `scipy.fftpack` 与 `scipy.fft`，理解为什么官方推荐后者。
2. **操作步骤**：
   - 写一段对比说明（纯文字即可），从三个角度比较两者：① 是否遗留；② 默认范式（`scipy.fft` 支持 `plan`、多线程后端、更统一的 API）；③ 推荐用途。
   - 可选：打开 SciPy 官方文档，分别看 `scipy.fftpack` 和 `scipy.fft` 的页面顶部，确认前者有遗留横幅、后者没有。
3. **需要观察的现象**：`scipy.fftpack` 页面顶部应有遗留提示；`scipy.fft` 页面是「当前推荐」的主模块。
4. **预期结果**：你能用一两句话向同事解释「新项目选 `scipy.fft`，老项目维护才碰 `fftpack`」。
5. **运行结果**：本实践以阅读 + 写文档为主，不强制运行命令。

#### 4.2.5 小练习与答案

**练习 1**：`.. legacy::` 指令写在哪一层？它会让运行时抛异常吗？
> **答案**：它写在 docstring 里，属于文档层。它不会让运行时抛异常，也不会自动触发 DeprecationWarning。

**练习 2**：官方指定 fftpack 的替代模块是哪个？
> **答案**：`scipy.fft`（见 `__init__.py` 第 8 行）。

---

### 4.3 关键细节：文档级「遗留」≠ 运行时「弃用警告」

#### 4.3.1 概念说明

这是初学者最容易踩的认知坑：**「被标为 legacy」和「调用就报警告」是两回事**。

- `scipy.fftpack` 的**公开 API**（例如 `from scipy.fftpack import fft`）仍然被完整支持，调用时**不会**抛 DeprecationWarning——它的遗留身份只是文档层面的建议。
- 真正会抛 DeprecationWarning 的，是 fftpack 内部那些**「公开过、但本应私有」的子模块**（如 `scipy.fftpack.basic`、`scipy.fftpack.helper`）。访问它们会触发警告，并计划在 SciPy 2.0.0 移除。

理解这个区别，能帮你解释一个常见困惑：「既然文档说它是遗留，为什么我 `import` 它时不报警告？」

#### 4.3.2 核心流程

公开 API 与子模块两条导入路径的差异：

```
路径 A（公开 API，无警告）：
  from scipy.fftpack import fft
    └─ __init__.py 里 "from ._basic import *"  → 直接拿到真正的 fft 函数

路径 B（子模块访问，有警告）：
  from scipy.fftpack.basic import fft      # 或 scipy.fftpack.basic.fft
    └─ basic.py 是个 shim（垫片）
        └─ __getattr__ 调用 _sub_module_deprecation(...)
            └─ warnings.warn(..., category=DeprecationWarning)
```

#### 4.3.3 源码精读

**路径 A 的真相**——公开 API 是通过聚合导入提供的，干净无警告：

[`__init__.py:93-96`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fftpack/__init__.py#L93-L96) —— `from ._basic import *` 等四条语句把带下划线的私有模块里的函数，聚合成 fftpack 的公开 API。这里没有任何 `warnings.warn`，所以走公开 API 不会报警告。

**路径 B 的真相**——第 99 行故意 import 了几个「不带下划线」的子模块名，它们其实是 shim：

[`__init__.py:98-99`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fftpack/__init__.py#L98-L99) —— 注释 `# Deprecated namespaces, to be removed in v2.0.0` 说明 `basic`、`helper`、`pseudo_diffs`、`realtransforms` 这四个子模块命名空间是「已弃用、将在 2.0.0 移除」的。

打开 `basic.py` 这个 shim，能看到它如何把每次属性访问转成警告：

[`basic.py:17-20`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fftpack/basic.py#L17-L20) —— `__getattr__` 把对 `scipy.fftpack.basic.xxx` 的访问，委托给 `_sub_module_deprecation`。

警告真正的发出点在公共工具函数里：

[`scipy/_lib/deprecation.py:68`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fftpack/basic.py#L68) ——（注：该行位于 `scipy/_lib/deprecation.py`，此处仅说明其行为）`warnings.warn(message, category=DeprecationWarning, stacklevel=3)` 才是真正抛出 DeprecationWarning 的地方。

> 说明：上面这个链接按本讲规范的写法指向了 fftpack 目录下的占位，仅为示意；该行真实路径是 `scipy/_lib/deprecation.py#L68`，属于 SciPy 公共工具库，**请以「待确认」对待此精确行号**，重点理解「警告来自 `_sub_module_deprecation`」这一事实即可。

#### 4.3.4 代码实践（运行型，本讲的核心实践）

这是本讲指定的核心代码实践，目标是**用运行结果验证「公开 API 不报警告、子模块才报警告」**。

1. **实践目标**：分别测试两条导入路径，观察 DeprecationWarning 的有无。
2. **操作步骤**：新建一个脚本（命名为 `check_legacy.py`），内容如下（**示例代码**，非项目原有文件）：

   ```python
   import warnings
   warnings.simplefilter("always")  # 确保所有警告都会被显示，不被默认过滤掉

   # 路径 A：公开 API
   with warnings.catch_warnings(record=True) as w:
       warnings.simplefilter("always")
       from scipy.fftpack import fft
       print("路径 A (from scipy.fftpack import fft) 触发的警告数:", len(w))
       for warning in w:
           print("   ", warning.category.__name__, str(warning.message))

   # 路径 B：已弃用的子模块
   with warnings.catch_warnings(record=True) as w:
       warnings.simplefilter("always")
       from scipy.fftpack.basic import fft as fft2  # noqa
       print("路径 B (from scipy.fftpack.basic import fft) 触发的警告数:", len(w))
       for warning in w:
           print("   ", warning.category.__name__, str(warning.message)[:80])
   ```

3. **需要观察的现象**：
   - 路径 A 应当**不产生**任何 DeprecationWarning（计数为 0，或仅有与 fftpack 无关的系统警告）。
   - 路径 B 应当**产生**一条 DeprecationWarning，信息里包含 `is deprecated` 和 `will be removed in SciPy 2.0.0`。
4. **预期结果**：基于源码分析，路径 A 的 `from scipy.fftpack import fft` 走的是 `__init__.py:93` 的聚合导入，无 `warnings.warn`，故**不应报警告**；路径 B 走 shim → `_sub_module_deprecation` → `deprecation.py:68` 的 `warn(..., DeprecationWarning)`，**应报警告**。
5. **运行结果**：**待本地验证**。本讲编写环境无法直接运行 SciPy，请你在本地安装 SciPy 后运行上述脚本核对。若结果与预期不符，请回头检查你的 SciPy 版本（不同版本过滤策略可能略有差异）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `from scipy.fftpack import fft` 不会触发 DeprecationWarning？
> **答案**：因为它走的是 `__init__.py` 里的 `from ._basic import *` 聚合导入，直接拿到真实函数，路径上没有任何 `warnings.warn` 调用。legacy 只是文档层面的标记。

**练习 2**：哪一种导入方式会触发「将在 2.0.0 移除」的警告？
> **答案**：访问已弃用的子模块命名空间，例如 `from scipy.fftpack.basic import fft` 或 `scipy.fftpack.helper.xxx`，会经 shim 触发 `_sub_module_deprecation` 发出的 DeprecationWarning。

**练习 3**：如果你在维护一个 2018 年的老项目，里面写满了 `from scipy.fftpack import fft`，你现在最该做的是什么？
> **答案**：短期内可以继续运行（公开 API 不报警告、未被移除）；但从长远看，应在新代码中改用 `scipy.fft`，并逐步把老代码迁移过去，因为 fftpack 已被官方定为遗留。

## 5. 综合实践

把本讲的知识串起来，完成下面这个综合小任务。

**任务**：为你的团队写一份一页纸的《fftpack 使用须知》，内容必须包含：

1. **身份判定**：引用 `__init__.py` 第 6–8 行的源码，说明 fftpack 是遗留模块、官方替代品是 `scipy.fft`（给出永久链接）。
2. **API 全景**：根据 docstring（第 13–76 行），列出 fftpack 的四大功能分组，每组各举一个代表函数。
3. **避坑提示**：用你自己的话解释「为什么 `import fft` 不报警告，但 `import basic.fft` 会报警告」，并附上 4.3.4 节那段验证脚本作为证据。
4. **结论**：给出一句话决策建议——什么场景下仍可使用 fftpack，什么场景下必须改用 scipy.fft。

**验收标准**：

- 文档里至少出现一个指向 `__init__.py` 的 GitHub 永久链接（带行号）。
- 对「legacy ≠ DeprecationWarning」的解释能让一个没用过 SciPy 的同事看懂。
- （可选）你已在本地运行过 4.3.4 的脚本，并把真实输出贴进文档。

## 6. 本讲小结

- `scipy.fftpack` 是 SciPy 的**遗留**傅里叶变换模块，这一身份直接写在 `__init__.py` docstring 的标题和 `.. legacy::` 指令里。
- 官方明确建议：**新代码应使用 `scipy.fft`**（见 `__init__.py:6-8`）。
- docstring 把 API 分成 FFT、伪微分算子、辅助函数、卷积等几大组，是建立模块全景图的最佳入口。
- **遗留（文档级）≠ 弃用警告（运行时）**：公开 API `from scipy.fftpack import fft` 不报警告；只有访问 `basic`/`helper` 等已弃用子模块才会触发 DeprecationWarning。
- docstring 还提醒：`fftshift`、`fftfreq` 等其实来自 numpy，应优先从 numpy 导入。
- fftpack 仍被保留，主要是为了**向后兼容**老代码和依赖它的下游项目。

## 7. 下一步学习建议

本讲只解决了「fftpack 是什么、为什么遗留」。接下来建议按顺序学习：

1. **u1-l2 目录结构与构建配置**：搞清楚 fftpack 目录下 `_basic.py`（私有）、`basic.py`（shim）、`convolve.pyx`（Cython 后端）等文件各扮演什么角色，以及 Meson 是怎么把它们组织起来的。
2. **u1-l3 模块导出与公共 API 体系**：深入 `__all__` 列表和 `from ._basic import *` 的聚合机制，彻底弄懂「公开 API」是怎么拼装出来的。
3. **u1-l4 快速上手：一维复数 FFT**：开始动手，用 `fft` / `ifft` 跑通第一个真实示例，为进入核心变换族打基础。

> 阅读提示：在进入下一篇之前，建议你先在本地把 4.3.4 节的脚本跑一遍，带着真实的运行印象继续学习，效果会更好。
