# 构建系统：meson、cython 与 sparsetools

## 1. 本讲目标

学完本讲，你应该能够：

- 读懂 `scipy/sparse/meson.build`，说清楚「哪些 `.py` 会被安装」「哪些扩展模块会被编译」；
- 解释 `_csparsetools.pyx.in` 是怎么经过 **tempita 模板展开** 再 **Cython 编译**，最终变成一个可被 Python 导入的 `._csparsetools` 扩展模块；
- 理解 `sparsetools/` 目录下的 `*.h/*.cxx` 是 **C++ 计算内核**，以及它在构建时如何被生成为 `._sparsetools` 扩展模块；
- 在脑中画出一条「源码 → 构建产物 → `import scipy.sparse`」的完整链路。

本讲承接 [u1-l2](u1-l2-directory-and-entry.md) 已经建立的「目录分层与选文件口诀」：实现看 `_xxx.py`、公开看 `__init__.py`、**安装看 `meson.build`**。这一讲就把最后那句口诀讲透。

## 2. 前置知识

阅读本讲前，最好先建立以下几个直觉（不熟悉的术语下面会展开）：

- **构建系统（build system）**。你在终端敲 `pip install scipy` 时，并不是把一堆 `.py` 复制到你的环境里就完事——SciPy 里有大量 C、C++、Cython 写的代码，需要先用编译器「翻译 + 编译」成你的操作系统能加载的二进制扩展（Linux 上是 `.so`，Windows 上是 `.pyd`）。SciPy 现在用 **Meson** 来编排这件事。Meson 的配置文件就叫 `meson.build`。
- **扩展模块（extension module）**。Python 本身用 C 写的「CPython」规定了一套接口，凡是按这套接口编译出来的二进制库，都能被 `import` 当作普通模块用。NumPy、SciPy 里的很多 `_xxx` 模块就是这种二进制扩展，它们跑得比纯 Python 快得多。
- **Cython**。一种「Python 风格的语言」，文件后缀 `.pyx`。它会被翻译成 C 代码再编译。你能用它写出看起来像 Python、但运行起来像 C 的代码。
- **tempita**。一个极简的文本模板工具（类似 `jinja2` 的远房小表弟），用 `{{...}}` 这种标记做字符串替换/循环展开。SciPy 用它在构建期「批量生成」多份高度相似的代码。
- **dtype（数据类型）**。NumPy 里每个数组都有一个固定的元素类型，比如 `int32`、`float64`、`complex128`。稀疏矩阵的非零值也可以是这些类型。

如果你对 Meson 完全陌生，只需记住一句话：**`meson.build` 就是给 Meson 看的「菜谱」，它告诉 Meson「安装哪些文件、编译哪些扩展、还要不要进入子目录继续看别的菜谱」。**

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `meson.build` | sparse 子包构建菜谱的总入口：声明要安装的 `.py`、要编译的 `_csparsetools` 扩展，并递归进入 4 个子目录。 |
| `_csparsetools.pyx.in` | 一个 tempita 模板文件，展开后成为 `_csparsetools.pyx`（Cython 源），专门提供 LIL 格式的快速片段。 |
| `sparsetools/meson.build` | C++ 后端的构建菜谱：生成 `*_impl.h` 头文件、编译 5 个 `.cxx` 为 `_sparsetools` 扩展模块。 |
| `sparsetools/sparsetools.h` | C++ 后端的公共头文件，定义了通用的 `thunk`（类型擦除的分发）机制。 |
| `../meson.build`、`scipy/meson.build` | 上层构建菜谱，定义了 `py3`、`tempita`、`cython_gen`、`cython_c_args` 等sparse 复用的「公共变量」。 |

> 提示：本讲的永久链接基于当前 HEAD `ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10`。

## 4. 核心概念与源码讲解

### 4.1 构建全景与 Python 源的安装

#### 4.1.1 概念说明

当你在脚本里写 `import scipy.sparse` 时，CPython 会去 `site-packages/scipy/sparse/` 这个目录里找东西。这个目录是怎么来的？答案是 Meson 在构建时，按 `sparse/meson.build` 这份菜谱把两类东西放进去：

1. **纯 Python 源**：一堆 `.py` 文本文件，原样复制过去；
2. **编译产物**：用 C/C++/Cython 编译出来的二进制扩展模块（`.so` / `.pyd`）。

`sparse/meson.build` 这份菜谱就干了这两件事，再加一件事：递归进入子目录（`subdir`），让子目录里的 `meson.build` 接力。

#### 4.1.2 核心流程

可以把 `sparse/meson.build` 的整体执行顺序记成下面这条流水线：

```
sparse/meson.build
  ├─ ① custom_target('_csparsetools_pyx')   # tempita 展开 .pyx.in → .pyx
  ├─ ② extension_module('_csparsetools')     # Cython 编译 .pyx → _csparsetools.so
  ├─ ③ install_sources(python_sources)       # 复制 33 个 .py 到 site-packages/scipy/sparse
  └─ ④ subdir('sparsetools')                 # 进入子目录，编译 C++ 后端 _sparsetools
       subdir('csgraph')
       subdir('linalg')
       subdir('tests')
```

注意：Meson 菜谱里条目的「书写顺序」并不完全等于「运行时顺序」，Meson 会自己分析依赖关系并行编排。但对阅读者来说，按上面四步理解就足够了。

#### 4.1.3 源码精读

先看菜谱里负责「安装 Python 源」的部分——这是练习任务关心的第一点。

`python_sources` 是一个普通的文件名字符串列表，列出了所有要被安装的 `.py`：

[meson.build:16-50](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/meson.build#L16-L50) 定义了 `python_sources` 列表，里面共 **33 个 `.py`** 文件。

这 33 个文件分成两类，正好对应 [u1-l2](u1-l2-directory-and-entry.md) 讲过的「真实现 vs 弃用垫片」：

- **18 个真实现/入口**（带 `_` 前缀或就是 `__init__.py`）：`__init__.py`、`_base.py`、`_bsr.py`、`_compressed.py`、`_construct.py`、`_coo.py`、`_csc.py`、`_csr.py`、`_data.py`、`_dia.py`、`_dok.py`、`_extract.py`、`_index.py`、`_lil.py`、`_matrix_io.py`、`_matrix.py`、`_spfuncs.py`、`_sputils.py`；
- **15 个弃用垫片**（无 `_` 前缀）：`base.py`、`bsr.py`、`compressed.py`、`construct.py`、`coo.py`、`csc.py`、`csr.py`、`data.py`、`dia.py`、`dok.py`、`extract.py`、`lil.py`、`sparsetools.py`、`spfuncs.py`、`sputils.py`。

紧接着，`install_sources` 把它们送到目标目录：

[meson.build:53-56](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/meson.build#L53-L56) 调用 `py3.install_sources(...)`，`subdir: 'scipy/sparse'` 表示这些 `.py` 最终落在 `site-packages/scipy/sparse/` 下。

> 这里的 `py3` 不是 sparse 自己定义的，它来自上层菜谱 [meson.build:21](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/meson.build#L21)：`py3 = import('python').find_installation(pure: false)`，是 Meson 提供的「Python 安装助手」。

菜谱结尾的四个 `subdir` 让构建递归进入子目录：

[meson.build:58-61](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/meson.build#L58-L61) 依次进入 `sparsetools`、`csgraph`、`linalg`、`tests`，由各自的 `meson.build` 接管。本讲的 4.4 小节专门讲 `sparsetools` 这个子菜谱。

#### 4.1.4 代码实践

**实践目标**：亲眼看到 Meson 安装的产物，并把它们和菜谱一一对应。

**操作步骤**：

1. 在已经装好 SciPy 的环境里，找到 sparse 包的真实路径（示例代码，标注为「示例代码」）：

   ```python
   import scipy.sparse as sp, os
   print(os.path.dirname(sp.__file__))
   ```

2. 进入该目录，用 `ls` 查看里面的内容（命令行）：

   ```bash
   ls <上面打印的路径>
   ```

3. 对照 `sparse/meson.build` 的 `python_sources` 列表，确认 `_base.py`、`_csr.py` 等真实现文件是否都在。
4. 同时留意目录里以 `_csparsetools` 和 `_sparsetools` 开头的二进制文件（Linux 上形如 `_csparsetools.cpython-3xx-xxx-linux-gnu.so`）。

**需要观察的现象**：目录里既有 `.py` 文本文件，也有 `.so` 二进制文件；前者来自 `install_sources`，后者来自 `extension_module`。

**预期结果**：你会看到 33 个 `.py`（含 18 个 `_` 前缀真实现 + 15 个弃用垫片）以及至少两个 `.so`：`_csparsetools.*.so` 与 `_sparsetools.*.so`。如果在某些精简安装里看不到 `.so`，说明 SciPy 是以 wheel 方式安装的——此时 `.so` 同样存在，只是文件名带上了 Python 版本和平台标签。

> 待本地验证：不同平台（Linux/macOS/Windows）下 `.so` 的后缀不同，请以你本机实际看到的为准。

#### 4.1.5 小练习与答案

**练习 1**：`python_sources` 列表里为什么同时有 `_csr.py` 和 `csr.py`？删掉 `csr.py` 会怎样？
**答案**：`_csr.py` 是真正的 CSR 实现，`csr.py` 是 [u1-l2](u1-l2-directory-and-entry.md) 讲过的弃用垫片，靠 PEP 562 的 `__getattr__` 转发到 `_csr` 并发出 `DeprecationWarning`。删掉 `csr.py`，老代码 `from scipy.sparse import csr`（注意是模块而非类）就会直接 `ImportError`，因此 v2.0 之前都保留。

**练习 2**：如果新增一个 `_foo.py` 模块却忘了把它加进 `python_sources`，会发生什么？
**答案**：构建能成功，但 `site-packages/scipy/sparse/` 里不会有 `_foo.py`，运行时 `from scipy.sparse import _foo` 会 `ModuleNotFoundError`。这就是「安装看 `meson.build`」口诀的现实意义。

---

### 4.2 tempita 模板：从 `.pyx.in` 批量生成特化函数

#### 4.2.1 概念说明

稀疏矩阵的非零值可以是 `int32`、`float64`、`complex128` 等很多种 dtype，索引数组又可以是 `int32` 或 `int64`。如果用纯 Python 写「针对每种类型都跑得快」的代码，你会被迫写一大堆几乎一模一样、只是类型名不同的函数。

Cython 解决「类型特化」有一个很自然的办法：**写一份模板，让构建期替你把每种类型的版本都展开一遍**。SciPy 用 tempita 这个模板工具来做这件事。模板源文件后缀是 `.pyx.in`（`.in` 表示 input template），构建时被展开成真正的 `.pyx`。

`_csparsetools.pyx.in` 的文件头注释直白地说出了它的用途：

```
"""
Fast snippets for LIL matrices.
"""
```

也就是说，`_csparsetools` 这个扩展模块专门给 **LIL 格式**（`_lil.py` 导入它，见 `_lil.py` 的 `from . import _csparsetools`）提供快速的小片段（get/set 单元素、批量花式索引等）。本讲只需理解它的「模板展开」机制；LIL 格式本身的细节留到 [u2-l5](u2-l5-lil-dok-dia-format.md)。

#### 4.2.2 核心流程

模板里有两组「类型表」，以及两个「展开宏」：

```
IDX_TYPES  = { "int32": ..., "int64": ... }              # 2 种索引类型
VALUE_TYPES = { "bool_": ..., ..., "complex128": ... }   # 15 种值类型

get_dispatch(types)            # 单维展开：对每个类型生成一个函数
get_dispatch2(types, types2)   # 二维展开：对每个 (类型1, 类型2) 组合生成一个函数
define_dispatch_map(...)       # 生成一张 dtype → 特化函数 的分派字典
```

具体到 LIL 花式赋值 `lil_fancy_set`，它既要看索引 dtype（int32/int64），又要看值 dtype（15 种），于是二维展开会生成：

\[
2 \times 15 = 30 \text{ 个特化函数}
\]

每个函数名形如 `_lil_fancy_set_int32_float64`，内部用的是强类型的 C 数组视图（没有 Python 对象装箱），因此很快。运行时，通用的 `lil_fancy_set` 先读一次 `i_idx.dtype` 和 `values.dtype`，在分派字典里查到对应函数再调用——**类型分派只发生一次，循环体全是 C 速度**。

#### 4.2.3 源码精读

模板顶部的类型表：

[_csparsetools.pyx.in:11-14](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_csparsetools.pyx.in#L11-L14) 定义 `IDX_TYPES`，把 Python 层的名字 `int32`/`int64` 映射到 Cython 层的 `cnp.npy_int32`/`cnp.npy_int64`。

[_csparsetools.pyx.in:16-32](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_csparsetools.pyx.in#L16-L32) 定义 `VALUE_TYPES`，覆盖 15 种值类型（含 `bool_`、各档整数、`float32/64`、`longdouble`、`complex64/128`、`clongdouble`）。

模板里「展开成多个函数」靠 tempita 的 `{{for ...}}` 语法：

[_csparsetools.pyx.in:167-177](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_csparsetools.pyx.in#L167-L177) 对 `IDX_TYPES` 循环，为 `int32`、`int64` 各生成一个 `_lil_get_lengths_{{NAME}}` 函数，紧接着用 `define_dispatch_map` 生成把它们按 `np.dtype(np.int32)` 等键串起来的字典。

二维展开（索引 × 值）的写法：

[_csparsetools.pyx.in:298-318](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_csparsetools.pyx.in#L298-L318) 用 `get_dispatch2(IDX_TYPES, VALUE_TYPES)` 双重循环，生成 30 个 `_lil_fancy_set_{{PYIDX}}_{{PYVALUE}}` 函数，再用 `define_dispatch_map2` 生成以 `(索引dtype, 值dtype)` 元组为键的分派字典。

驱动这步展开的菜谱是 `sparse/meson.build` 最顶上的 `custom_target`：

[meson.build:1-5](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/meson.build#L1-L5) 声明 `custom_target('_csparsetools_pyx', ...)`：输入 `_csparsetools.pyx.in`，用 `tempita` 程序处理，输出 `_csparsetools.pyx`。

这里的 `tempita` 同样来自上层菜谱 [meson.build:146](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/meson.build#L146)：`tempita = find_program('scipy/_build_utils/tempita.py')`，本质就是 SciPy 仓库自带的一个小脚本，作用是「读模板、执行 `{{...}}`、吐出展开后的文本」。

#### 4.2.4 代码实践

**实践目标**：亲手算一遍模板会展开成多少个函数，体会「代码生成」的意义。

**操作步骤**：

1. 打开 [_csparsetools.pyx.in:11-32](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_csparsetools.pyx.in#L11-L32)，数一下 `IDX_TYPES` 和 `VALUE_TYPES` 各有多少项。
2. 找到 `{{for PYIDX, PYVALUE, IDX_T, VALUE_T in get_dispatch2(IDX_TYPES, VALUE_TYPES)}}`（即 [第 298 行附近](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_csparsetools.pyx.in#L298-L318)），手算它会展开成几个函数。
3. 回答：如果不做代码生成，而是运行时用 Python 的 `if dtype == ...` 来分派，会有什么性能损失？

**需要观察的现象 / 预期结果**：

- `IDX_TYPES` = 2 项（`int32`、`int64`），`VALUE_TYPES` = 15 项；
- 单维展开 `get_dispatch(IDX_TYPES)` 生成 2 个函数，`get_dispatch(VALUE_TYPES)` 生成 15 个函数；
- 二维展开 `get_dispatch2(IDX_TYPES, VALUE_TYPES)` 生成 \(2 \times 15 = 30\) 个 `_lil_fancy_set_*` 函数；
- 这 30 个函数的名字形如 `_lil_fancy_set_int32_float64`、`_lil_fancy_set_int64_complex128` 等，每个内部都是针对特定类型的强类型 C 循环，没有 Python 对象装箱。

> 待本地验证：若你想真正看到展开后的 `.pyx`，需要在本仓库用 Meson 触发一次构建，展开产物会出现在构建目录（`build/...`）里，而不是源码目录。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `IDX_TYPES` 只列了 `int32`/`int64` 两种，而 `VALUE_TYPES` 列了 15 种？
**答案**：稀疏矩阵的**索引**（行号、列号、`indptr`/`indices`）只需整数类型，且实际只需要「够用就好」的 `int32` 或 `int64`（见 [u3-l5](u3-l5-sputils-helpers.md) 的 `get_index_dtype`）；而**非零值**可以是布尔、各档整数、浮点、复数，所以值类型必须覆盖 15 种。

**练习 2**：tempita 展开发生在「构建期」还是「运行期」？这有什么好处？
**答案**：发生在**构建期**。好处是：运行期不再有「为每种类型写一份」的开销，循环体直接是编译好的强类型 C 代码；缺点是编译时间变长、编译产物变大（多出几十个特化函数）。这是「用编译时间换运行时间」的典型取舍。

---

### 4.3 Cython 扩展模块的编译

#### 4.3.1 概念说明

上一节我们得到了展开后的 `_csparsetools.pyx`（Cython 源）。但它还不能被 `import`——Cython 源要先翻译成 C 源（`.c`），再由 C 编译器编译、链接成二进制扩展模块。Meson 里负责「把 `.pyx` 变成可加载扩展」的，就是 `extension_module` 这个调用，配合一个叫 `cython_gen` 的「生成器」。

#### 4.3.2 核心流程

```
_csparsetools.pyx.in
   ──[tempita, 构建期]──▶ _csparsetools.pyx
                              ──[cython_gen.process, 构建期]──▶ _csparsetools.c
                                                                   ──[C 编译/链接]──▶ _csparsetools.so
```

注意链路是「模板 → Cython → C → 二进制」四级，前两级都发生在构建期，最终用户拿到的只有 `.py` 和 `.so`。

#### 4.3.3 源码精读

[meson.build:7-14](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/meson.build#L7-L14) 用 `py3.extension_module('_csparsetools', ...)` 编译扩展模块。第一个参数是模块名，构建产物就是 `_csparsetools`（导入名 `scipy.sparse._csparsetools`）；源是 `cython_gen.process(_csparsetools_pyx)`——把上一步 tempita 的产物喂给 Cython 生成器。

几个关键参数的含义：

- `c_args: cython_c_args`：传给 C 编译器的宏定义。`cython_c_args` 定义在上层 [scipy/meson.build:4](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/meson.build#L4)，内容是 `['-DCYTHON_CCOMPLEX=0']`（关于复数处理的兼容性开关）。
- `dependencies: np_dep`：链接 NumPy 的 C-API（Cython 代码里大量用到 `cnp.npy_int32` 等 NumPy 类型）。
- `install: true, subdir: 'scipy/sparse'`：把编译好的 `.so` 安装到 `site-packages/scipy/sparse/`。
- `link_args: version_link_args`：上层 [meson.build:133](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/meson.build#L133) 定义的平台相关链接参数。

而 `cython_gen` 本身是 SciPy 公共的一个 Meson 生成器：

[scipy/meson.build:501-505](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/meson.build#L501-L505) 定义 `cython_gen = generator(cython, arguments: cython_args, output: '@BASENAME@.c', ...)`，即「对每个 `.pyx`，调用 `cython` 生成同名 `.c`」。`@BASENAME@` 是 Meson 的占位符，表示「去掉扩展名后的文件名」。

> 一句话区分两个 `_c*` 扩展：`_csparsetools`（**c**ython）是这一节讲的、LIL 专用的 Cython 扩展；`_sparsetools`（无 `c`）是下一节讲的 C++ 后端，承担 CSR/CSC/COO/BSR/DIA 的重计算。别被名字里的 `c` 搞混——它代表 Cython，不是「C 版本」。

#### 4.3.4 代码实践

**实践目标**：确认 `_csparsetools` 确实是一个被编译的二进制扩展，并被 `_lil.py` 使用。

**操作步骤**（示例代码）：

```python
import scipy.sparse
from scipy.sparse import _lil, _csparsetools

print(type(_csparsetools))                 # 应是 <class 'module'>
print(_csparsetools.__file__)              # 应指向 .so 二进制，而非 .py
print(hasattr(_csparsetools, 'lil_get1'))  # 模板/手写都存在的函数
```

**需要观察的现象**：`_csparsetools.__file__` 指向一个 `.so`（或 `.pyd`）二进制路径，而不是 `.py` 文本。

**预期结果**：能成功导入，且 `lil_get1` 等函数存在。注意这些是「内部」下划线模块，SciPy 不保证其 API 稳定，这里只是用来观察构建产物。

> 待本地验证：不同 Python/平台下 `.so` 文件名不同，但导入语句一致。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `_csparsetools.pyx` 不直接放在源码里，而要先有一个 `.pyx.in` 模板？
**答案**：因为里面有大量「按 dtype 重复」的代码（4.2 节），手写 30+ 个几乎一样的函数既冗长又易错。用 tempita 在构建期展开，源码只维护一份模板，改类型表就能批量增删。

**练习 2**：`cython_gen.process(_csparsetools_pyx)` 里的 `_csparsetools_pyx` 是 4.2 节 `custom_target` 的产物。这说明两步之间有依赖关系，Meson 是怎么知道的？
**答案**：Meson 通过变量传递自动建立依赖——`_csparsetools_pyx` 是 `custom_target` 返回的目标对象，把它作为 `extension_module` 的源传入，Meson 就知道「必须先跑 tempita 生成 `.pyx`，再喂给 Cython」，会自动按正确顺序调度，无需手写顺序约束。

---

### 4.4 C++ 后端 sparsetools

#### 4.4.1 概念说明

如果说 `_csparsetools` 只是 LIL 的小助手，那么 `sparsetools/` 才是 sparse 子包真正的「计算心脏」。`_coo.py`、`_csr.py`、`_csc.py`、`_bsr.py`、`_dia.py`、`_compressed.py`、`_construct.py`、`_spfuncs.py` 全都 `from ._sparsetools import ...`（例如 `coo_tocsr`、`csr_matvec`、`csr_tocsc`、`csr_count_blocks`）。这些函数负责最重的数值运算：COO→CSR 转换、稀疏矩阵-向量乘、格式互转、计数等。

它们是用 **C++ 模板** 写的（`sparsetools/*.h` 是声明，运行实现集中在构建期生成的 `*_impl.h` 与若干 `.cxx`）。和 4.2 节一样，C++ 模板也面临「为每种 dtype 生成一份」的问题——只不过这里不用 tempita，而用专门的脚本 `_generate_sparsetools.py`（这个脚本本身的细节留到 [u3-l6](u3-l6-sparsetools-cpp-codegen.md)，本讲只看它在构建里的位置）。

#### 4.4.2 核心流程

`sparsetools/meson.build` 的两步：

```
① custom_target('_sparsetools_headers')
     输入: _generate_sparsetools.py
     输出: bsr_impl.h / csc_impl.h / csr_impl.h / other_impl.h / sparsetools_impl.h

② extension_module('_sparsetools')
     源: 5 个 .cxx (bsr/csc/csr/other/sparsetools) + 上面生成的 5 个 _impl.h
     产物: _sparsetools.so
```

#### 4.4.3 源码精读

C++ 后端的构建菜谱：

[sparsetools/meson.build:2-12](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/sparsetools/meson.build#L2-L12) 用 `custom_target('_sparsetools_headers', ...)` 调用 `../_generate_sparsetools.py --no-force -o @OUTDIR@`，在构建期生成 5 个 `*_impl.h` 头文件（`bsr_impl.h`、`csc_impl.h`、`csr_impl.h`、`other_impl.h`、`sparsetools_impl.h`）。这些头文件**不存在于源码树**，只在构建目录里。

[sparsetools/meson.build:14-28](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/sparsetools/meson.build#L14-L28) 用 `extension_module('_sparsetools', ...)` 把 5 个手写的 `.cxx`（`bsr.cxx`、`csc.cxx`、`csr.cxx`、`other.cxx`、`sparsetools.cxx`）与上一步生成的 `_sparsetools_headers` 一起编译、链接为 `_sparsetools` 扩展。

> 注意：这里**没有** `cython_gen`——`.cxx` 已经是 C++ 源，直接交给 C++ 编译器即可。这就是它与 `_csparsetools`（Cython）最大的流程差别。`include_directories: '../../_build_utils/src'` 是为了能找到 SciPy 公共的构建辅助头文件。

C++ 后端的公共头文件很精简，核心是一个「类型擦除的分派」机制：

[sparsetools/sparsetools.h:12-15](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/sparsetools/sparsetools.h#L12-L15) 定义了 `thunk_t`（一个函数指针类型，接收类型编号和一组 `void**` 参数）与 `call_thunk`（根据类型编号把 Python 传来的数组解释成对应 C++ 类型并调用真正的模板函数）。简单说：Python 层只传「我要 int32 + float64 这一套」，`call_thunk` 据此在编译期生成好的众多模板特化里挑一个执行。这是 C++ 侧的「运行期分派 + 编译期特化」方案，与 4.2 节 Cython 侧的分派字典思路异曲同工。

#### 4.4.4 代码实践

**实践目标**：感受 `_sparsetools` 是真正的「重计算后端」，并看到 Python 层如何依赖它。

**操作步骤**（示例代码）：

```python
from scipy.sparse import _sparsetools   # 内部 C++ 后端模块
print(_sparsetools.__file__)            # 应指向 .so
print('coo_tocsr' in dir(_sparsetools)) # COO→CSR 转换的底层函数
print('csr_matvec' in dir(_sparsetools))# 稀疏矩阵-向量乘的底层函数
```

再读源码确认调用关系：打开 [_coo.py](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_coo.py) 顶部，你会看到 `from ._sparsetools import (coo_tocsr, ...)`。这说明 COO 转 CSR 时，Python 层只是拼装数组，真正干活的 `coo_tocsr` 来自这个 C++ 扩展。

**需要观察的现象**：`_sparsetools.__file__` 指向 `.so`；`coo_tocsr`、`csr_matvec` 等大量底层函数都在里面。

**预期结果**：导入成功，`dir(_sparsetools)` 列出几十个 `coo_*`/`csr_*`/`csc_*`/`bsr_*`/`dia_*` 开头的底层函数。

> 待本地验证：`_sparsetools` 是私有模块，函数签名面向内部使用，不建议在生产代码里直接调用——本步只为观察构建产物与调用链。

#### 4.4.5 小练习与答案

**练习 1**：`sparsetools/meson.build` 里生成了 5 个 `*_impl.h`，但这些文件在源码树里找不到。为什么？
**答案**：它们是**构建期生成**的产物（由 `_generate_sparsetools.py` 根据类型表展开 C++ 模板得到），只存在于构建目录。源码树只保留「生成器」和「模板声明」，这和 4.2 节 `.pyx.in` 的思路一致——只维护一份模板，由构建系统批量生成。

**练习 2**：`_sparsetools` 用 C++ 编译，`_csparsetools` 用 Cython 编译，为什么 sparse 要同时维护两套编译机制？
**答案**：`_sparsetools` 是历史悠久的纯 C++ 模板库，性能优先、面向大量数值内核；`_csparsetools` 是后来用 Cython 补充的、面向 LIL 这种「对象数组」操作（Cython 处理 Python 对象更方便）。两者各取所长：C++ 跑数值密集循环，Cython 处理带 Python 对象的边角逻辑。

---

## 5. 综合实践

把本讲四节串起来，完成一次「构建链路追踪」：

1. **读菜谱**：打开 [sparse/meson.build](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/meson.build)，在纸上列出三件事的对应行号——(a) 安装了哪些 `.py`（`python_sources` / `install_sources`）；(b) 哪个 `custom_target` 用 tempita 展开模板；(c) 哪个 `extension_module` 编译 `_csparsetools`；以及末尾有哪些 `subdir`。

2. **追一个扩展**：以 `_csparsetools` 为例，画出它从源到 `.so` 的四级链路：`.pyx.in` →（tempita）→ `.pyx` →（`cython_gen`）→ `.c` →（C 编译）→ `.so`。在每一步旁标注由哪个菜谱条目驱动。

3. **对比两个后端**：写一张小表，对比 `_csparsetools` 与 `_sparsetools` 的：源语言、是否用模板/代码生成、生成工具、被谁导入、职责。

4. **验证产物**（可选，需本机已装 SciPy）：用 4.1.4 和 4.4.4 的小脚本，确认安装目录里两类产物（`.py` 与两个 `.so`）都到位，并把它们与菜谱一一对应。

预期：你能不查资料地说出「`import scipy.sparse` 之后，CSR 的快速运算是 `_csr.py` 调用 `_sparsetools.csr_*` 这些 C++ 函数完成的，而 LIL 的快速单元素读写是 `_lil.py` 调用 `_csparsetools` 这个 Cython 扩展完成的；这两个扩展都由 `sparse/meson.build` 这份菜谱编译并安装」。

## 6. 本讲小结

- `sparse/meson.build` 是 sparse 子包的构建总入口，做三件事：用 `install_sources` 安装 **33 个 `.py`**（18 个真实现 + 15 个弃用垫片）、用 `extension_module` 编译扩展、用 `subdir` 递归进入 `sparsetools/csgraph/linalg/tests`。
- `_csparsetools.pyx.in` 是 **tempita 模板**：构建期由 [_build_utils/tempita.py](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/_build_utils/tempita.py) 按 `IDX_TYPES`(2 项) 和 `VALUE_TYPES`(15 项) 展开，生成大量按 dtype 特化的函数（二维组合 \(2\times15=30\) 个），是「用编译时间换运行时间」的典型。
- 展开后的 `.pyx` 经 `cython_gen`（[scipy/meson.build:501](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/meson.build#L501-L505)）翻译成 C，再编译为 `_csparsetools.so`，专门给 LIL 格式提供快速片段。
- `sparsetools/` 是 **C++ 计算内核**：`_generate_sparsetools.py` 在构建期生成 `*_impl.h`，配合 5 个 `.cxx` 编译为 `_sparsetools.so`；`coo_tocsr`、`csr_matvec` 等底层函数都被各格式模块 `from ._sparsetools import`。
- `sparsetools.h` 用 `thunk_t` + `call_thunk` 实现 C++ 侧的「运行期分派 + 编译期特化」，与 Cython 侧的分派字典思路一致。
- 牢记口诀：**实现看 `_xxx.py`、公开看 `__init__.py`、安装看 `meson.build`**——本讲把最后一句落到了实处。

## 7. 下一步学习建议

- 下一讲 [u1-l4 动手：第一个稀疏数组与基本操作](u1-l4-first-sparse-array.md) 会跳出构建视角，开始真正「用」稀疏数组：用 `coo_array`、`csr_array` 做构造与运算，本讲的 `_sparsetools`/`_csparsetools` 就在这些操作背后默默工作。
- 想深入 C++ 代码生成机制的读者，可以提前跳到 [u3-l6 C++ 后端与 sparsetools 代码生成](u3-l6-sparsetools-cpp-codegen.md)，看 `_generate_sparsetools.py` 的类型码签名表与模板分发细节。
- 想了解 LIL 格式本身（即 `_csparsetools` 服务的对象）的读者，可在 [u2-l5](u2-l5-lil-dok-dia-format.md) 看到 LIL 的 `rows/data` 列表结构。
