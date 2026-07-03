# scipy.io 项目概览与定位

## 1. 本讲目标

本讲是整个 `scipy.io` 学习手册的第一篇。读完本讲后，你应该能够：

- 说出 `scipy.io` 是什么、它在 SciPy 生态中扮演什么角色；
- 列出 `scipy.io` 支持的科学数据文件格式，以及每种格式对应的读写函数；
- 读懂 `scipy/io/__init__.py` 如何通过一行行 `import` 拼装出公共 API；
- 解释 `__all__` 是怎么自动生成的，以及为什么有些名字是函数、有些名字是子模块；
- 区分 `scipy.io` 与 NumPy 内置 IO 例程（`np.save` / `np.load` 等）的分工。

本篇是「概览篇」，不深入任何一个格式的二进制细节，目的是先建立全局地图。从第二篇开始才会逐个格式深入源码。

## 2. 前置知识

在开始之前，请确认你理解下面几个基础概念。如果某个概念陌生，本讲会用通俗方式再解释一遍。

- **文件格式（file format）**：计算机把数据保存到磁盘上时遵循的「排版规则」。同一份矩阵数据，可以存成纯文本（每行一个数字），也可以存成二进制（按字节紧凑排列）。不同软件（MATLAB、IDL、Fortran 程序）各有自己的格式，`scipy.io` 的任务就是「翻译」这些格式。
- **文本格式 vs 二进制格式**：文本格式可以用记事本打开读懂（如 Matrix Market 的 `.mtx`）；二进制格式打开是乱码，必须按字节规则解析（如 MATLAB 的 `.mat`、WAV 音频）。两者各有取舍：文本可读性好但体积大，二进制紧凑但需要专门的解析器。
- **数组 / 矩阵（array / matrix）**：科学计算的基本数据结构。`scipy.io` 读出来的绝大多数数据都是 NumPy 的 `ndarray`。
- **Python 的 `import` 机制**：`from .matlab import loadmat` 表示「从同级子包 `matlab` 里取出 `loadmat` 这个名字，放到当前模块」。理解这一点，才能看懂 `__init__.py` 在做什么。
- **SciPy 与 NumPy 的关系**：NumPy 提供多维数组和基础数值运算；SciPy 在 NumPy 之上提供更高级的科学计算模块（积分、优化、信号处理、统计、文件 IO 等）。`scipy.io` 就是 SciPy 负责文件读写的那一块。

如果你对 NumPy 数组还不熟，建议先花十分钟了解 `np.array`、`ndarray.dtype`、`ndarray.shape` 三个概念，本讲后续会用到。

## 3. 本讲源码地图

本讲只涉及一个关键文件，但它是整个 `scipy.io` 的「总入口」，所有公共功能都从这里向外暴露。

| 文件 | 作用 |
| --- | --- |
| `scipy/io/__init__.py` | `scipy.io` 子包的入口与公共 API 声明。它的文档字符串（docstring）按格式分类列出了所有功能；它的末尾用若干 `import` 把各格式的读写函数汇聚到顶层命名空间，并用 `__all__` 声明公共 API 边界。 |

后续讲义会逐步展开的真实实现文件（本讲只「点名」，不深入）：

- `scipy/io/matlab/`：MATLAB `.mat` 文件子系统（最大的子系统）。
- `scipy/io/_netcdf.py`：NetCDF3 读写。
- `scipy/io/_fortran.py`：Fortran 无格式顺序文件。
- `scipy/io/_fast_matrix_market/` 与 `scipy/io/_mmio.py`：Matrix Market 读写。
- `scipy/io/_idl.py`：IDL `.sav` 文件读取。
- `scipy/io/_harwell_boeing/`：Harwell-Boeing 稀疏矩阵格式。
- `scipy/io/wavfile.py`：WAV 音频（独立子模块 `scipy.io.wavfile`）。
- `scipy/io/arff/`：ARFF 数据集格式（独立子模块 `scipy.io.arff`）。

## 4. 核心概念与源码讲解

### 4.1 scipy.io 的定位：一个「格式读写器集合」

#### 4.1.1 概念说明

很多科学计算工作者手里有来自不同工具的数据：同事给的 MATLAB `.mat`、气象数据 NetCDF `.nc`、旧 Fortran 程序吐出来的二进制文件、稀疏矩阵库用的 Harwell-Boeing 文件……这些格式互不兼容，每种都需要专门的解析逻辑。

`scipy.io` 就是 SciPy 为这类需求提供的**「多格式读写器集合」**。它的核心思路是：

> 不发明新的文件格式，而是为已有的科学数据格式各提供一个 Python 读写器，并把它们统一挂在 `scipy.io` 这个命名空间下。

这一点非常重要：`scipy.io` 内部各格式之间**彼此独立**。WAV 的读写代码和 MATLAB 的读写代码没有任何共享逻辑，它们只是恰好住在同一个子包里。这也是为什么本手册后面会「一个格式一篇讲义」地分别讲解。

需要特别强调 `scipy.io` 与 NumPy 内置 IO 的分工：

- **NumPy IO** 负责「NumPy 自己的数据」：`.npy` / `.npz`（`np.save` / `np.load` / `np.savez`）、以及通用文本（`np.loadtxt` / `np.genfromtxt`）。
- **scipy.io** 负责「来自外部科学软件的数据」：MATLAB、NetCDF、IDL、Harwell-Boeing、ARFF、WAV 等 NumPy 不专门处理的格式。

`scipy.io` 的文档字符串里也专门挂了一个 `seealso` 链接，把读者引导到 NumPy IO 例程，明确表示两者是互补关系。

#### 4.1.2 核心流程

从「用户视角」看 `scipy.io`，使用流程非常线性：

```
用户拿到一个科学数据文件
        │
        ▼
根据文件后缀/来源，选择 scipy.io 里对应的函数
        │
        ├── .mat      → loadmat / savemat / whosmat
        ├── .mtx      → mmread / mmwrite / mminfo
        ├── .nc/.cdf  → netcdf_file
        ├── Fortran   → FortranFile
        ├── .sav      → readsav
        ├── H-B       → hb_read / hb_write
        ├── .wav      → scipy.io.wavfile.read / write
        └── .arff     → scipy.io.arff.loadarff
        │
        ▼
读出 NumPy ndarray（或写出 ndarray 到文件）
```

从「源码视角」看，这些函数并不是在 `__init__.py` 里实现的，而是：

```
scipy/io/__init__.py
        │  (一行行 from ... import ...)
        ▼
把分散在各子模块 / 子包里的函数「汇聚」到 scipy.io 顶层命名空间
        │
        ▼
用户只要 import scipy.io 就能用 sio.loadmat / sio.mmread ...
```

所以 `__init__.py` 本质上是一张「总目录」。

#### 4.1.3 源码精读

`__init__.py` 开头是一段很长的文档字符串，它本身就是 `scipy.io` 的官方文档入口。文档字符串第一句就点明了定位：SciPy 提供了许多模块、类、函数来读写多种文件格式。

模块定位说明（[scipy/io/__init__.py:L13-L16](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/__init__.py#L13-L16)）：这段代码（docstring 部分）说明了 `scipy.io` 的总体职责，并明确把 NumPy IO 作为「参见」推荐给读者，体现两者的互补关系。

> 提示：Python 里用三引号 `"""..."""` 包起来、且放在模块/函数/类最开头的字符串叫「文档字符串（docstring）」。Sphinx 等文档工具会自动抓取它生成官方文档，所以 `__init__.py` 顶部的 docstring 同时承担了「代码注释」和「官方文档源」两个角色。

#### 4.1.4 代码实践

这是一个「环境检查 + 文档浏览」型实践，帮助你确认本地环境就绪，并亲眼看到 `scipy.io` 的官方文档就写在源码里。

1. **实践目标**：确认 `scipy.io` 可正常导入，并理解 docstring 既是注释也是文档。
2. **操作步骤**：
   - 在装好 SciPy 的环境里启动 Python 交互环境；
   - 执行：
     ```python
     import scipy.io as sio
     print(sio.__doc__[:200])   # 打印文档字符串前 200 个字符
     print(sio.__file__)        # 看看你实际加载的是哪个 __init__.py
     ```
3. **需要观察的现象**：
   - `sio.__doc__` 的开头应该出现 `Input and output (scipy.io)` 这样的标题；
   - `sio.__file__` 应指向你本机 SciPy 安装目录下的 `scipy/io/__init__.py`。
4. **预期结果**：能看到文档片段和文件路径，说明 `scipy.io` 入口加载的就是我们正在读的这个 `__init__.py`。如果你的 SciPy 版本较旧，看到的行号/内容可能与本讲略有差异——以你本机的源码为准。
5. 如果执行失败（如 `ModuleNotFoundError: No module named 'scipy'`），请先安装 SciPy；本步骤标记「待本地验证」直到你能成功导入。

#### 4.1.5 小练习与答案

**练习 1**：`scipy.io` 会自己发明新的文件格式吗？它的设计哲学是什么？

> **参考答案**：不会。`scipy.io` 是「已有科学数据格式的读写器集合」，它为 MATLAB、NetCDF 等外部格式各提供 Python 读写器，而不是定义新格式。各格式之间相互独立。

**练习 2**：`scipy.io` 和 `numpy.save` / `numpy.load` 是竞争关系吗？

> **参考答案**：不是，是互补关系。NumPy IO 负责 `.npy` / `.npz` 和通用文本；`scipy.io` 负责来自外部科学软件（MATLAB、NetCDF、IDL 等）的专有格式。源码文档字符串里也专门用 `seealso` 把读者引向 NumPy IO 例程。

### 4.2 支持的文件格式与对应的读写函数

#### 4.2.1 概念说明

`scipy.io` 的文档字符串按格式分了若干个区块（autosummary），每个区块对应一类文件格式以及它的读写函数。归纳起来，`scipy.io` 主要覆盖以下科学数据格式：

| 格式类别 | 典型后缀 | 来源 / 用途 | 顶层读写入口 |
| --- | --- | --- | --- |
| MATLAB | `.mat` | MATLAB 工作区变量 | `loadmat` / `savemat` / `whosmat` |
| NetCDF（v3） | `.nc` / `.cdf` | 气象、海洋等网格数据 | `netcdf_file` / `netcdf_variable` |
| Fortran 无格式顺序文件 | 自定义 | 旧 Fortran 程序输出 | `FortranFile`（含异常类） |
| Matrix Market | `.mtx` | 稀疏/稠密矩阵交换 | `mminfo` / `mmread` / `mmwrite` |
| IDL save | `.sav` | IDL 语言变量导出 | `readsav` |
| Harwell-Boeing | `.rua` / `.rb` 等 | 稀疏矩阵标准交换 | `hb_read` / `hb_write` |
| WAV 音频 | `.wav` | 音频波形 | 子模块 `scipy.io.wavfile` 的 `read` / `write` |
| ARFF | `.arff` | WEKA 机器学习数据集 | 子模块 `scipy.io.arff` 的 `loadarff` |

需要注意一个**重要区别**：

- 前 6 类格式（MATLAB、NetCDF、Fortran、Matrix Market、IDL、Harwell-Boeing）的读写函数被**直接 re-export 到 `scipy.io` 顶层**，所以你可以写 `scipy.io.loadmat(...)`、`scipy.io.mmread(...)`。
- WAV 和 ARFF 则各自住在自己的子模块里（`scipy.io.wavfile`、`scipy.io.arff`），通常写成 `scipy.io.wavfile.read(...)`、`scipy.io.arff.loadarff(...)`。

之所以有这个区别，是因为 WAV 和 ARFF 本身就带有较多辅助类型（如 `WavFileWarning`、`MetaData`、`ArffError` 等），用一个独立子模块来组织更清晰。这一点在 `__init__.py` 的 `import` 语句里会看得一清二楚。

> 名词解释：**re-export（再导出）**。一个模块 A 从模块 B `import` 了一些名字，于是这些名字也成了 A 的一部分，对 A 的使用者可见，这叫「再导出」。`scipy.io.__init__.py` 把各子模块的函数 re-export 到顶层，用户才不用写一长串子模块路径。

#### 4.2.2 核心流程

`__init__.py` 用 6 行 `from ... import ...` 把不同格式的函数「拉」到顶层。每行都遵循相同模式：

```
from <实现位置> import <要暴露的函数/类名>
```

完整映射如下（行号对应本讲使用的 HEAD）：

| 格式 | 实现位置 | 被暴露的名字 | import 所在行 |
| --- | --- | --- | --- |
| MATLAB | `.matlab`（子包） | `loadmat, savemat, whosmat` | L102 |
| NetCDF | `._netcdf` | `netcdf_file, netcdf_variable` | L105 |
| Fortran | `._fortran` | `FortranFile, FortranEOFError, FortranFormattingError` | L108 |
| Matrix Market | `._fast_matrix_market` | `mminfo, mmread, mmwrite` | L110 |
| IDL | `._idl` | `readsav` | L111 |
| Harwell-Boeing | `._harwell_boeing`（子包） | `hb_read, hb_write` | L112 |
| WAV / ARFF / 其它 | `.`（同目录子模块） | `arff, harwell_boeing, idl, mmio, netcdf, wavfile`（子模块） | L115 |

注意两个细节：

1. 带下划线前缀的模块（如 `_netcdf`、`_idl`、`_fortran`、`_fast_matrix_market`）是**私有实现模块**；不带前缀的（如 `mmio.py`、`idl.py`、`netcdf.py`、`harwell_boeing.py`、`wavfile.py`）在 SciPy 2.0 中将变成「弃用包装」。这套公共/私有命名约定会在第 4.3 节和后续 u1-l2、u4-l2 讲义里详述。
2. MATLAB 的实现位于 `matlab/` **子包**（目录），而 Matrix Market 的入口位于 `_fast_matrix_market/` 子包——说明一个格式可以由多个文件、甚至 C++/Cython 后端共同实现。

#### 4.2.3 源码精读

文档字符串按格式分块列出所有公共功能。先看 MATLAB 区块（[scipy/io/__init__.py:L18-L28](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/__init__.py#L18-L28)）：这段 docstring 说明 `loadmat`/`savemat`/`whosmat` 支持 MATLAB 版本 4 到 7.1，并提示更底层的读写工具在 `scipy.io.matlab` 子包里。

接下来是真正的 `import` 汇聚区。

MATLAB 三个函数从 `matlab` 子包导入（[scipy/io/__init__.py:L101-L102](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/__init__.py#L101-L102)）：这一行把 MATLAB 子系统的三个公共函数挂到 `scipy.io` 顶层。注意 `from .matlab import ...` 同时也会让 `matlab` 子包本身可作为 `scipy.io.matlab` 访问。

NetCDF 两个对象从私有模块 `_netcdf` 导入（[scipy/io/__init__.py:L104-L105](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/__init__.py#L104-L105)）：暴露文件对象 `netcdf_file` 与变量对象 `netcdf_variable`。

Fortran 文件对象及两个异常类从 `_fortran` 导入（[scipy/io/__init__.py:L107-L108](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/__init__.py#L107-L108)）：注意它不仅导出了 `FortranFile`，还导出了两个配套异常 `FortranEOFError`、`FortranFormattingError`，方便用户 `try/except` 精确捕获读取错误。

Matrix Market 三个函数从 `_fast_matrix_market` 子包导入（[scipy/io/__init__.py:L110](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/__init__.py#L110)）：自 SciPy 1.12 起，Matrix Market 改用了 C++ 后端 `fast_matrix_market`（见后续 u4-l1 讲义），但顶层函数名 `mminfo/mmread/mmwrite` 保持不变，向后兼容。

IDL 与 Harwell-Boeing 各自一行导入（[scipy/io/__init__.py:L111-L112](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/__init__.py#L111-L112)）：`readsav` 负责 IDL `.sav`；`hb_read`/`hb_write` 负责 Harwell-Boeing 稀疏矩阵。

最后，把若干子模块作为名字导入（[scipy/io/__init__.py:L114-L115](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/__init__.py#L114-L115)）：这里 `from . import arff, harwell_boeing, idl, mmio, netcdf, wavfile` 把 6 个子模块挂到顶层，注释明确写着它们是「弃用命名空间，将在 v2.0.0 移除」。WAV 和 ARFF 的实际函数（`wavfile.read`、`arff.loadarff`）就是通过这些子模块访问的。

#### 4.2.4 代码实践（本讲主实践）

这是本讲的核心实践，对应任务规格里要求的「打印 `__all__` 并为每个公共名字注释它对应的格式」。

1. **实践目标**：亲手列出 `scipy.io` 的全部公共 API，并理解每个名字分别属于哪种格式、是函数还是子模块。
2. **操作步骤**：
   ```python
   import scipy.io as sio

   # 1) 打印公共 API 清单
   for name in sio.__all__:
       obj = getattr(sio, name)
       kind = type(obj).__name__   # function / module / type ...
       print(f"{name:24s} -> {kind}")
   ```
3. **需要观察的现象**：你会看到两类条目——一类是 `function` / `type`（真正的读写函数与异常类），另一类是 `module`（子模块）。
4. **预期结果（参考注释）**：下面这张表是基于源码（`__init__.py` 的 import 语句）推断出的注释，供你对照。**请以你本机实际输出为准**；如果你的 SciPy 版本与本讲 HEAD 不同，清单可能略有出入（标记「待本地验证」直到你跑过）。

   | 公共名字 | 类别 | 对应格式 / 说明 |
   | --- | --- | --- |
   | `loadmat` | 函数 | MATLAB：读取 `.mat` |
   | `savemat` | 函数 | MATLAB：写入 `.mat` |
   | `whosmat` | 函数 | MATLAB：列出 `.mat` 内变量 |
   | `matlab` | 子模块 | MATLAB 子系统（底层读写工具所在） |
   | `netcdf_file` | 类 | NetCDF3：文件对象 |
   | `netcdf_variable` | 类 | NetCDF3：变量对象 |
   | `netcdf` | 子模块 | NetCDF 的弃用命名空间包装 |
   | `FortranFile` | 类 | Fortran 无格式顺序文件对象 |
   | `FortranEOFError` | 异常类 | Fortran：正常的文件末尾 |
   | `FortranFormattingError` | 异常类 | Fortran：格式不正确 |
   | `mminfo` | 函数 | Matrix Market：查询矩阵信息 |
   | `mmread` | 函数 | Matrix Market：读矩阵 |
   | `mmwrite` | 函数 | Matrix Market：写矩阵 |
   | `mmio` | 子模块 | Matrix Market 的弃用命名空间包装 |
   | `readsav` | 函数 | IDL：读取 `.sav` |
   | `idl` | 子模块 | IDL 的弃用命名空间包装 |
   | `hb_read` | 函数 | Harwell-Boeing：读稀疏矩阵 |
   | `hb_write` | 函数 | Harwell-Boeing：写稀疏矩阵 |
   | `harwell_boeing` | 子模块 | Harwell-Boeing 的弃用命名空间包装 |
   | `wavfile` | 子模块 | WAV 音频读写子模块（`read`/`write`） |
   | `arff` | 子模块 | ARFF 数据集子模块（`loadarff`） |

   > 解读：顶层直接暴露读写**函数**的格式有 6 类（MATLAB、NetCDF、Fortran、Matrix Market、IDL、Harwell-Boeing）；WAV 与 ARFF 则通过子模块访问。同时你会看到 6 个「弃用命名空间子模块」（`netcdf`、`mmio`、`idl`、`harwell_boeing`、`wavfile`、`arff`），它们在 SciPy 2.0 将被移除，不建议新代码直接 `import` 它们。
5. **延伸观察**：试着 `print(len(sio.__all__))`，数一数公共名字的总数；再想想为什么 `test` 这个名字（见 4.3.3）没有出现在 `__all__` 里。

#### 4.2.5 小练习与答案

**练习 1**：如果想读写一个 `.mtx` 稀疏矩阵，应该用 `scipy.io` 里的哪几个函数？它们从哪个模块导入？

> **参考答案**：用 `mminfo`（查信息）、`mmread`（读）、`mmwrite`（写）。它们从 `_fast_matrix_market` 子包导入，见 `__init__.py` 第 110 行。

**练习 2**：WAV 和 ARFF 为什么不像 MATLAB 那样在顶层暴露 `read`/`loadarff` 函数？

> **参考答案**：它们各自住在独立子模块 `scipy.io.wavfile` 和 `scipy.io.arff` 里，因为这些格式还带有较多辅助类型（如 `WavFileWarning`、`MetaData`、`ArffError`），用子模块组织更清晰。`__init__.py` 第 115 行通过 `from . import ... wavfile, arff` 把子模块挂到顶层。

**练习 3**：`from .matlab import loadmat` 这行代码除了让 `loadmat` 可用，还带来了什么副作用？

> **参考答案**：它同时让 `matlab` 子包本身可作为 `scipy.io.matlab` 访问，因此 `matlab` 这个名字也会出现在 `scipy.io` 的命名空间（进而出现在 `__all__`，见 4.3）。

### 4.3 `__all__` 的生成原理与公共 API 边界

#### 4.3.1 概念说明

在 Python 里，`__all__` 是一个模块的「公共 API 名单」：

- 它告诉 `from scipy.io import *` 应该导入哪些名字；
- 它也向用户和工具声明：「列表里的名字是稳定公开的，其它（尤其是以 `_` 开头的）是私有的，不要直接用」。

很多项目会手工维护 `__all__`，每加一个函数就改一次列表。`scipy.io` 用了一个更省事、也更聪明的写法：**自动生成**。理解这一行代码，是理解整个 `scipy.io` 公共 API 边界的关键。

同时，`__init__.py` 末尾还藏着一个 `test` 对象——它是 SciPy 统一的测试入口，但有趣的是它**并不在** `__all__` 里。理解「为什么不在」，能帮你彻底搞懂 `__all__` 的生成时机。

#### 4.3.2 核心流程

`scipy.io` 的 `__all__` 生成逻辑：

```
在执行到第 117 行时，调用内置 dir()
        │
        ▼
dir() 返回当前模块命名空间里所有已绑定的名字
（即前面 6 行 import 引入的函数/类/子模块）
        │
        ▼
过滤掉以 '_' 开头的名字（私有）
        │
        ▼
得到 __all__
```

关键点：`dir()` 返回的是**「执行到这一行时」**已经存在的名字。因此：

- 在第 117 行**之前** import 的名字（`loadmat`、`mmread`、各子模块等）会进入 `__all__`；
- 在第 117 行**之后**才赋值的名字（如第 120 行的 `test`）**不会**进入 `__all__`。

这就是为什么 `test` 虽然存在于 `scipy.io`，却不出现在 `__all__` 里——它定义得太晚了。

#### 4.3.3 源码精读

自动生成 `__all__` 的核心一行（[scipy/io/__init__.py:L117](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/__init__.py#L117)）：`__all__ = [s for s in dir() if not s.startswith('_')]`。`dir()` 不带参数时返回当前作用域的名字列表；列表推导式过滤掉所有以 `_` 开头的私有名字，剩下的就是公共 API。这种写法的好处是：新增一个格式的 import 后，无需再手工改 `__all__`。

末尾的统一测试入口（[scipy/io/__init__.py:L119-L121](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/__init__.py#L119-L121)）：这里引入 `PytestTester` 并创建 `test = PytestTester(__name__)`，于是用户可以用 `scipy.io.test()` 跑 `scipy.io` 子包的全部测试。随后 `del PytestTester` 把临时名字删掉，避免它污染命名空间。由于这几行在第 117 行之后执行，`test` 不在 `__all__` 里——这是一个有意为之的细节。

回顾弃用命名空间的导入（[scipy/io/__init__.py:L114-L115](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/__init__.py#L114-L115)）：注释 `# Deprecated namespaces, to be removed in v2.0.0` 明确了 `mmio / idl / netcdf / harwell_boeing / arff / wavfile` 这些「不带下划线」的模块只是过渡用的包装。它们之所以仍出现在 `__all__`，是因为名字不以 `_` 开头——但这并不代表它们是推荐用法（详见 u1-l2、u4-l2）。

> 小结一句话：**`__all__` 是「按命名约定」自动得出的公共名单**——不带下划线就算公共，带下划线就算私有。这套约定贯穿整个 SciPy 2.0 的模块重构。

#### 4.3.4 代码实践

这是一个「源码阅读 + 现场验证」型实践，目标是让你亲眼看懂 `__all__` 的生成规则。

1. **实践目标**：验证 `__all__` 确实等于「所有不以 `_` 开头的名字」，并解释 `test` 为何缺席。
2. **操作步骤**：
   ```python
   import scipy.io as sio

   # A. 用和源码同样的规则，自己重算一遍 __all__
   recomputed = [s for s in dir(sio) if not s.startswith('_')]
   # 注意：dir(sio) 里还包含 test（因为它现在已被赋值），所以会比 sio.__all__ 多
   print("in dir but not in __all__:", set(recomputed) - set(sio.__all__))

   # B. 检查 test 是否存在、是否在 __all__
   print("has test attr:", hasattr(sio, "test"))
   print("test in __all__:", "test" in sio.__all__)
   ```
3. **需要观察的现象**：
   - 集合差集里应该出现 `test`（可能还有 `matlab` 等子模块，取决于本机版本）；
   - `test` 作为属性存在，但不在 `__all__`。
4. **预期结果**：`has test attr` 为 `True`，`test in __all__` 为 `False`。这正好印证「`__all__` 在第 117 行就定稿了，而 `test` 在第 120 行才创建」。具体输出以本机为准（待本地验证）。
5. **思考（选做）**：如果把 `__all__ = ...` 那一行挪到文件最末尾，`__all__` 会发生什么变化？（答案见下面练习。）

#### 4.3.5 小练习与答案

**练习 1**：为什么 `netcdf_file` 在 `__all__` 里，而它来源的模块 `_netcdf` 不在？

> **参考答案**：`__all__` 的过滤条件是「名字不以 `_` 开头」。`netcdf_file` 不带下划线，是公共 API；`_netcdf` 以 `_` 开头，是私有实现模块，被过滤掉了。这正体现了「下划线前缀 = 私有」的约定。

**练习 2**：如果把第 117 行的 `__all__ = [...]` 移到文件最末尾（第 121 行之后），`__all__` 会多出哪个名字？为什么？

> **参考答案**：会多出 `test`。因为到文件末尾时 `test` 已经在第 120 行被赋值，`dir()` 就能捕获到它。这反过来证明了「`__all__` 的内容取决于它被求值时命名空间里已有的名字」。

**练习 3**：`from . import arff, harwell_boeing, idl, mmio, netcdf, wavfile` 让这些子模块出现在 `__all__` 里，这是否意味着推荐大家直接 `import scipy.io.mmio`？

> **参考答案**：不推荐。源码注释明确说它们是「Deprecated namespaces, to be removed in v2.0.0」。它们出现在 `__all__` 只是因为名字不带下划线，符合自动生成规则；但语义上它们是过渡用的弃用包装。新代码应直接用 `scipy.io.mmread` 等顶层函数（详见 u4-l2）。

## 5. 综合实践

把本讲学到的「定位、格式映射、`__all__` 机制」串起来，完成下面这个小任务：

**任务：给 `scipy.io` 画一张「公共 API 全景图」。**

1. 运行：
   ```python
   import scipy.io as sio
   names = sio.__all__
   ```
2. 对 `names` 里的每一个名字，用 `getattr(sio, name)` 判断它是函数、类还是子模块（提示：用 `inspect.isfunction`、`inspect.isclass`，或直接看 `type(obj).__name__` 是否为 `module`）。
3. 把结果分成三组：
   - **MATLAB / NetCDF / Fortran / Matrix Market / IDL / Harwell-Boeing** 的顶层函数与类；
   - **WAV / ARFF** 的子模块；
   - **弃用命名空间子模块**（`mmio`、`idl`、`netcdf`、`harwell_boeing`、`arff`、`wavfile`）。
4. 用一句话写下你对下面问题的回答：
   - 「为什么 `scipy.io` 顶层既有函数、又有子模块？」
   - 「`__all__` 是手工写死的，还是自动算出来的？依据是哪一行源码？」

**预期产出**：一张分组表 + 两句结论。结论应当类似：

- 「函数是把 6 类格式的读写入口直接 re-export 到顶层；子模块则是把 WAV/ARFF 及若干弃用包装挂在顶层，方便 `scipy.io.wavfile.read` 这种用法。」
- 「`__all__` 是自动算出来的，依据是 `__init__.py` 第 117 行的 `__all__ = [s for s in dir() if not s.startswith('_')]`。」

如果你暂时不方便运行，可以纯靠阅读 `__init__.py` 的 6 行 import + 第 117 行完成分组（标记「待本地验证」运行部分）。

## 6. 本讲小结

- `scipy.io` 是 SciPy 的「多格式科学数据读写器集合」，不发明新格式，而是为 MATLAB、NetCDF、Fortran、Matrix Market、IDL、Harwell-Boeing、WAV、ARFF 等已有格式提供 Python 读写器。
- 它与 NumPy IO 互补：NumPy 管 `.npy`/`.npz` 与通用文本，`scipy.io` 管来自外部科学软件的专有格式（源码 docstring 里有 `seealso` 指向 NumPy IO）。
- `scipy/io/__init__.py` 是「总目录」，用 6 行 `from ... import ...` 把各格式的读写函数汇聚到顶层命名空间。
- 顶层直接暴露读写函数的有 6 类格式（MATLAB、NetCDF、Fortran、Matrix Market、IDL、Harwell-Boeing）；WAV 与 ARFF 通过子模块访问。
- `__all__` 不是手写的，而是由第 117 行 `[s for s in dir() if not s.startswith('_')]` 自动生成，体现「不带下划线 = 公共」的 SciPy 约定。
- `test` 对象（由 `PytestTester` 创建）虽存在于 `scipy.io`，但因在第 117 行之后才赋值，所以不在 `__all__` 里。

## 7. 下一步学习建议

本讲建立了全局地图，接下来建议：

1. **先学 u1-l2《目录结构、构建方式与公共 API 约定》**：搞清楚 `scipy/io/meson.build` 怎么构建各子模块、以及 `_` 前缀私有模块与弃用命名空间包装的关系。这是理解整个 `scipy.io` 工程结构的基础。
2. **再学 u1-l3《快速上手：导入与各格式最小读写示例》**：跑通第一个 WAV round-trip，建立「输入—处理—输出」的体感。
3. **进入第二单元前**，建议自己用本讲的 `__all__` 清单挑一个最感兴趣的格式（例如 MATLAB 或 Matrix Market），先看一眼对应的实现文件（如 `_mmio.py` 或 `matlab/_mio.py`），带着问题进入后续讲义。
4. 想直接理解公共/私有模块弃用约定的读者，可以提前跳读 u4-l2，但建议在学完 u1-l2 之后再读会更顺畅。
