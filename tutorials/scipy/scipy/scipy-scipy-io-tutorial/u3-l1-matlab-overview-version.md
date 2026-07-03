# 讲义标题：MATLAB 子包总览与版本判定

## 1. 本讲目标

本讲是进入 MATLAB `.mat` 文件子系统的**第一讲**。学完后你应该能够：

- 说清 `scipy.io.matlab` 子包里都有哪些模块、对外暴露了哪些公共名字。
- 解释一个 `.mat` 文件被「打开」的完整过程：从文件名／文件对象，到一个可读的字节流。
- 读懂 `_get_matfile_version` 如何仅凭文件头的前若干字节，把文件判定为 **v4 / v5 / v7.3** 三种版本之一。
- 理解 `mat_reader_factory` 这个「工厂函数」如何根据版本号，把字节流分发给 `MatFile4Reader` 或 `MatFile5Reader`，并对 v7.3 抛出明确错误。

本讲**不**展开任何版本内部的具体读写细节（那是 u3-l4 ~ u3-l6 的任务），只关注「认识子包 → 打开文件 → 判定版本 → 选对 reader」这条入口链路。

## 2. 前置知识

阅读本讲前，建议你已经具备以下认知（均在 u1 单元建立）：

- **私有实现模块 vs 弃用包装模块**：`scipy.io` 用 `_` 前缀（如 `_mmio.py`）标记真正的实现，不带前缀的同名文件（如 `mmio.py`）是 SciPy 2.0 将要移除的弃用包装。MATLAB 子包里也存在同样的命名（`_miobase.py`/`miobase.py` 等）。
- **re-export**：一个子包的 `__init__.py` 用 `from ._xxx import ...` 把内部模块的名字提升到子包顶层，从而可以直接 `from scipy.io.matlab import loadmat`。
- **文件对象（file-like object）**：任何实现了 `read`/`write`/`seek`/`tell`/`close` 等方法的对象都算，包括 `open()` 返回的真实文件句柄，也包括 `io.BytesIO` 这类内存流。

下面补充两个本讲要用到的新术语：

- **`.mat` 文件版本**：MATLAB 的 `.mat` 不是一种格式，而是一族格式。历史上主要有三类：
  - **v4（Level 1.0）**：最古老，每个变量自带一个 20 字节小头，没有全局文件头。
  - **v5（即 v6 / v7，主版本号都是 1）**：当前主流，有一个 128 字节全局头，支持压缩、cell、struct 等复合类型。
  - **v7.3**：本质是 HDF5 文件，SciPy **不**实现它的读写（需借助 `h5py`）。
- **工厂模式（factory）**：调用方不直接 `new` 具体的 reader，而是把文件交给一个「工厂函数」，由工厂根据探测到的文件特征返回**合适类型**的对象。`mat_reader_factory` 就是这种模式。

## 3. 本讲源码地图

本讲涉及三个核心源码文件，它们构成 MATLAB 读取的入口层：

| 文件 | 作用 |
|------|------|
| [scipy/io/matlab/__init__.py](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/matlab/__init__.py) | 子包入口，汇聚公共 API、定义 `__all__`、转发弃用命名空间。 |
| [scipy/io/matlab/_mio.py](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/matlab/_mio.py) | 提供三个公共函数 `loadmat`/`savemat`/`whosmat`、文件打开工具 `_open_file`/`_open_file_context`、以及工厂函数 `mat_reader_factory`。 |
| [scipy/io/matlab/_miobase.py](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/matlab/_miobase.py) | 提供版本判定 `matfile_version`/`_get_matfile_version`、异常与警告类、抽象基类 `MatFileReader`/`MatVarReader`。 |

调用链一览（读取一个文件时）：

```
loadmat / whosmat  ──►  _open_file_context  ──►  _open_file  ──► (字节流)
        │                                                      │
        └────────────►  mat_reader_factory  ◄───────────────────┘
                               │
                      _get_matfile_version  (判定 0/1/2)
                               │
                ┌──────────────┼──────────────┐
                ▼              ▼              ▼
         MatFile4Reader  MatFile5Reader   v7.3 → NotImplementedError
```

## 4. 核心概念与源码讲解

### 4.1 MATLAB 子包总览与公共导出

#### 4.1.1 概念说明

`scipy.io.matlab` 是一个**子包（subpackage）**，而不是单个文件。它内部按职责拆成多个模块：

- `_mio.py`：公共入口函数 `loadmat`/`savemat`/`whosmat` 与工厂。
- `_mio4.py`：v4 的 reader/writer。
- `_mio5.py`：v5 的 reader/writer。
- `_mio5_params.py`：v5 的类型映射与若干 ndarray 子类（`mat_struct`/`MatlabObject` 等）。
- `_miobase.py`：所有版本共享的基类、版本判定、异常。
- `_streams.pyx` / `_mio5_utils.pyx` / `_mio_utils.pyx`：Cython 底层（见 u3-l7）。
- `_byteordercodes.py`：字节序字符串到 numpy 码的转换（见 u3-l2）。

子包对外的「公共名字」由 `__init__.py` 统一汇聚，并在 `__all__` 中显式列出。与之并存的，是一组不带 `_` 前缀的**弃用命名空间模块**（`mio.py`、`mio5.py`、`miobase.py` 等），它们只是为兼容旧 `import` 路径而存在，将在 SciPy 2.0 移除——这与 u1-l2 讲到的全局弃用约定是同一套机制。

#### 4.1.2 核心流程

`__init__.py` 做三件事：

1. 从各 `_` 前缀实现模块**重新导出**公共名字（函数、类）。
2. 显式定义 `__all__`，告诉 `from scipy.io.matlab import *` 应该带走哪些名字。
3. `import` 一批弃用命名空间模块，使旧路径 `scipy.io.matlab.mio` 仍可被访问（会触发 `DeprecationWarning`）。

#### 4.1.3 源码精读

子包入口的三段 re-export：

[scipy/io/matlab/__init__.py:47-55](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/matlab/__init__.py#L47-L55) —— 从 `_mio`、`_mio5`、`_mio5_params`、`_miobase` 把公共名字提到子包顶层，并 `import` 八个弃用命名空间模块。

紧接着显式声明的公共清单：

[scipy/io/matlab/__init__.py:57-62](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/matlab/__init__.py#L57-L62) —— `__all__` 列出 `loadmat`/`savemat`/`whosmat`、`matfile_version`、四个异常/警告类、`mat_struct`、`varmats_from_mat` 以及三个 ndarray 子类 `MatlabObject`/`MatlabOpaque`/`MatlabFunction`。注意 `matfile_version`（版本判定）也在这里被列为公共 API。

> 关键观察：MATLAB 子包的 `__all__` 是**手写枚举**的（不像 `scipy/io/__init__.py` 那样用列表推导自动生成），这样能精确控制哪些名字算公共、哪些（如 `MatFileReader` 基类、`_get_matfile_version`）保持内部。

#### 4.1.4 代码实践

1. **目标**：亲眼确认子包导出了哪些公共名字。
2. **步骤**：执行下面的 Python 代码。
3. **预期现象**：打印出的列表与本讲 4.1.3 中 `__all__` 的内容一致。
4. **预期结果**：列表里包含 `loadmat`、`savemat`、`whosmat`、`matfile_version` 等 12 个名字；**不**包含 `_get_matfile_version`、`mat_reader_factory`（它们以下划线开头，是私有的）。

```python
# 示例代码
import scipy.io.matlab as sim
print(sim.__all__)
```

#### 4.1.5 小练习与答案

**练习 1**：`scipy.io.matlab.matfile_version` 是公共函数，但 `scipy.io.matlab._get_matfile_version` 不是，依据是什么？

**答案**：依据是子包 `__init__.py` 的 `__all__` 清单——只有出现在 `__all__` 里的名字才是公共 API；`_get_matfile_version` 以下划线开头且不在 `__all__` 中，属于内部实现细节。

**练习 2**：为什么 `__init__.py` 第 53-55 行还要 `import` 一批不带下划线的 `mio`/`mio5`/`miobase` 等模块？

**答案**：它们是弃用命名空间包装模块，仅为兼容「旧的 `from scipy.io.matlab.mio import ...` 写法」而保留，访问时会通过 `__getattr__` 触发 `DeprecationWarning`，并将在 SciPy 2.0 移除（详见 u4-l2）。

---

### 4.2 文件打开：`_open_file` 与 `_open_file_context`

#### 4.2.1 概念说明

调用 `loadmat('a.mat')` 时，参数可能有三类形态：

- 一个**文件名字符串**（带或不带 `.mat` 后缀）；
- 一个**已打开的文件对象**（例如用户自己 `open(...)` 或 `io.BytesIO()`）；
- 一个**文件路径对象**。

`_open_file` 的职责是把这些异质输入**统一**成一个「已打开、可读的字节流」。它的核心逻辑是：

1. 先判断传入的对象**本身**是不是已经具备所需方法（如 `read`）。如果是，直接原样返回（说明调用者已经打开好了，函数不应重复打开，也不应负责关闭）。
2. 否则尝试 `open(file_like, mode)`。
3. 若 `open` 失败且对象是字符串、且 `appendmat=True`，就补上 `.mat` 后缀再试一次。

`_open_file_context` 则把 `_open_file` 包成一个**上下文管理器**：用 `with` 语法保证「由本函数打开的文件，在退出 `with` 块时一定被关闭」，而「调用者自己传入的文件」则不关（关闭权归调用者）。

#### 4.2.2 核心流程

```
_open_file(file_like, appendmat, mode)
   │
   ├─ 计算本模式需要的方法集合 reqs（如 {'read'} / {'read','write'}）
   ├─ 若 reqs ⊆ dir(file_like)：  →  return (file_like, opened=False)   # 已是文件对象
   ├─ try: open(file_like, mode)  →  return (f, opened=True)
   └─ except OSError:
         若是字符串 and appendmat and 不以 .mat 结尾：
             file_like += '.mat'; open(...)  →  return (f, opened=True)
         否则：raise OSError('Reader needs file name or open file-like object')
```

返回值是一个二元组 `(字节流, opened)`，`opened` 告诉调用方「这个流是不是我开的」，从而决定退出时是否要 `close()`。

#### 4.2.3 源码精读

上下文管理器：保证按需关闭。

[scipy/io/matlab/_mio.py:19-26](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/matlab/_mio.py#L19-L26) —— `_open_file_context` 用 `try/finally`，仅当 `opened=True`（即文件是本函数打开的）时才在 `finally` 里 `close()`。

核心打开逻辑：

[scipy/io/matlab/_mio.py:29-53](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/matlab/_mio.py#L29-L53) —— 关键三步：第 39 行用 `reqs.issubset(dir(file_like))` 探测「是否已是文件对象」；第 43 行直接 `open`；第 46-49 行在 `OSError` 后对字符串补 `.mat` 再试。

> 这个 `reqs.issubset(dir(file_like))` 的写法很巧妙：它不关心对象具体是什么类型，只关心它**有没有**所需的 `read`/`write` 方法。这就是 Python 的「鸭子类型（duck typing）」——`io.BytesIO`、`gzip.GzipFile`、真实文件句柄都能被一视同仁。

#### 4.2.4 代码实践

1. **目标**：验证 `_open_file` 对「无后缀文件名 + `appendmat=True`」会自动补 `.mat`。
2. **步骤**：准备一个名为 `demo.mat` 的任意 v5 文件（可用 `savemat` 生成），然后调用 `_open_file('demo', appendmat=True)`。
3. **预期现象**：即使不写 `.mat`，也能成功打开，返回 `opened=True`。
4. **预期结果**：函数返回一个二元组，第二项为 `True`，第一项是一个可读文件对象。

```python
# 示例代码
import numpy as np
from scipy.io import savemat
from scipy.io.matlab._mio import _open_file, _open_file_context

savemat('demo.mat', {'a': np.arange(5)})      # 生成 demo.mat

stream, opened = _open_file('demo', appendmat=True)   # 注意没写 .mat
print('opened =', opened)                     # 预期 True
print('可读字节 =', stream.read(4))           # 预期 b'MATL'（v5 头文本开头）
stream.close()

# 再演示「自己传入已打开对象」时 opened=False、且不会被 context 关闭
import io
bio = io.BytesIO(open('demo.mat', 'rb').read())
with _open_file_context(bio, appendmat=True) as f:
    print('上下文内 opened 行为：f is bio ?', f is bio)   # 预期 True
print('退出 with 后 bio 仍可读 ?', bio.readable())        # 预期 True（未被关闭）
```

#### 4.2.5 小练习与答案

**练习 1**：如果调用者把一个**已打开的文件对象**传给 `_open_file`，返回的 `opened` 是什么？为什么？

**答案**：`opened=False`。因为第 39 行检测到该对象已具备 `read` 方法，直接原样返回；函数没有真正调用 `open()`，因此也不应在退出时 `close()`——关闭权归调用者。

**练习 2**：`_open_file_context` 用 `try/finally` 而不是直接调用 `f.close()`，目的是什么？

**答案**：保证即便 `with` 块内抛异常，文件也能被关闭（资源不泄漏）；同时只在 `opened=True` 时才关闭，避免误关调用者自己管理的文件。

---

### 4.3 版本判定：`_get_matfile_version` 的三段式判定

#### 4.3.1 概念说明

这是本讲最核心的算法。给定一个字节流，`_get_matfile_version` 要在不读取全部内容的前提下，仅凭**文件头的几个字节**判断版本，返回主版本号（0=v4，1=v5，2=v7.3）和次版本号。

它依赖两种格式在文件头上的本质差异：

- **v4 没有全局头**，文件一开始就是第一个变量的 20 字节变量头。这个变量头的前 4 字节是 `MOPT`（机器选项），其中**几乎总会包含一个值为 0 的字节**（编码了数值类型/字节序）。这就是「v4 文件前 4 字节里有 0」这条启发式判据的来源。
- **v5 / v7.3 有 128 字节全局头**，其中：
  - 前 116 字节是一段描述文本（如 `MATLAB 5.0 MAT-file ...`）；
  - 字节 124-125 是一个 2 字节**版本整数**（通常 `0x0100`）；
  - 字节 126-127 是 2 字节**字节序探针**：内容是字符 `'IM'`（小端）或 `'MI'`（大端）。读这 2 字节就能同时知道「文件是哪种字节序」以及「版本整数里哪边是高位」。

注意：v5 和 v7.3 的头都长得像 `MATLAB 5.0 ...`，二者的区分**不在**头文本，而在字节 124-125 的版本值——v7.3 的主版本号为 2。

> 公共函数 `matfile_version`（在 `__all__` 中）只是 `_get_matfile_version` 的「带文件名解析」的薄包装：它先用 `_open_file_context` 打开文件，再把流交给 `_get_matfile_version`。

#### 4.3.2 核心流程

`_get_matfile_version(fileobj)` 的判定分三段：

```
1) 读前 20 字节 hdr_bytes
     ├─ 不足 20 字节         → raise MatReadError("truncated")
     └─ 20 字节全为 0         → raise MatReadError("corrupt")

2) 看前 4 字节 mopt_ints：
     ├─ 含 0                 → 回卷到 0，返回 (0, 0)          # 判为 v4
     └─ 不含 0               → 继续（判为 v5/v7.3）

3) seek(124)，读 4 字节 tst_str（版本2B + 字节序探针2B）：
     maj_ind = (tst_str[2] == 'I') ? 1 : 0
     maj_val = tst_str[maj_ind]
     min_val = tst_str[1 - maj_ind]
     若 maj_val ∈ {1, 2}     → 返回 (maj_val, min_val)         # 1=v5, 2=v7.3
     否则                     → raise ValueError("Unknown mat file type")
```

v5 头的关键 4 字节布局（偏移 124 起）：

| 字节 | 小端文件内容 | 大端文件内容 | 含义 |
|------|------------|------------|------|
| 124  | `0x00` | `0x01` | 版本整数（低/高字节） |
| 125  | `0x01` | `0x00` | 版本整数（高/低字节） |
| 126  | `0x49 'I'` | `0x4D 'M'` | 字节序探针第 1 字符 |
| 127  | `0x4D 'M'` | `0x49 'I'` | 字节序探针第 2 字符 |

算法的精妙之处：**用字节序探针里 `'I'` 的位置，反推出版本整数的高位字节在哪一端**。设版本整数为 \(V\)，它由高位字节 \(V_{\text{hi}}\) 与低位字节 \(V_{\text{lo}}\) 组成：

\[
\text{maj\_ind} = \begin{cases} 1 & \text{若 tst\_str}[2] = \text{'I'} \ (\text{小端}) \\ 0 & \text{若 tst\_str}[2] = \text{'M'} \ (\text{大端}) \end{cases}
\]

\[
\text{maj\_val} = \text{tst\_str}[\text{maj\_ind}], \qquad \text{min\_val} = \text{tst\_str}[1-\text{maj\_ind}]
\]

小端文件里 `'I'` 在 `tst_str[2]`，于是 `maj_ind=1`，主版本取 `tst_str[1]=0x01`、次版本取 `tst_str[0]=0x00`，得到 `(1, 0)`；大端文件里 `'I'` 在 `tst_str[3]`，于是 `maj_ind=0`，主版本取 `tst_str[0]=0x01`、次版本取 `tst_str[1]=0x00`，同样得到 `(1, 0)`。**一套代码同时处理两种字节序，无需分支**。

#### 4.3.3 源码精读

公共包装函数（接收文件名，内部打开后委托）：

[scipy/io/matlab/_miobase.py:183-222](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/matlab/_miobase.py#L183-L222) —— `matfile_version` 用 `_open_file_context` 打开文件，再调用 `_get_matfile_version`。docstring 注明它会**把读指针重置到 0**（副作用）。

判定算法本体：

[scipy/io/matlab/_miobase.py:231-256](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/matlab/_miobase.py#L231-L256) —— 三段式判定：第 237-239 行检测全零（损坏）；第 240-243 行用「前 4 字节含 0」判 v4；第 247-253 行读偏移 124 的 4 字节、用 `'I'` 位置解出主次版本；第 254-256 行只接受主版本 1 或 2。

常数 `_HDR_N_BYTES = 20`（要读的头字节数）：

[scipy/io/matlab/_miobase.py:228](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/matlab/_miobase.py#L228) —— 这 20 字节正是 v4 单个变量的变量头长度，因此同时满足「判 v4（前 4 字节）」和「验证文件非空/非全零」两个目的。

> 一处设计取舍：判定 v4 用的是**启发式**（前 4 字节含 0），并非严格解析 v4 头。绝大多数真实 v4 文件都满足该特征，但理论上一个「恰好前 4 字节都不为 0 的 v4 文件」会被误判为 v5——不过这在实践中极罕见，换取的是「无需完整解析头就能快速分流」的简洁。

#### 4.3.4 代码实践

1. **目标**：对真实的 v4 与 v5 测试文件，分别读出主次版本号，并与文档承诺对照。
2. **步骤**：用子包自带的测试数据，分别调用公共 `matfile_version` 和内部 `_get_matfile_version`。
3. **预期现象**：v4 文件返回 `(0, 0)`；v5 文件返回 `(1, 0)`。
4. **预期结果**：见代码注释。**待本地验证**（下列结果由源码逻辑推得，请在本地实际运行确认）。

```python
# 示例代码
import os
import scipy.io as sio
import scipy.io.matlab as sim
from scipy.io.matlab._miobase import _get_matfile_version

data_dir = os.path.join(os.path.dirname(sio.__file__),
                        'matlab', 'tests', 'data')
v4 = os.path.join(data_dir, 'testdouble_4.2c_SOL2.mat')   # v4 文件
v5 = os.path.join(data_dir, 'testdouble_7.4_GLNX86.mat')  # v5 文件

# (a) 用公共函数 matfile_version（直接传文件名）
print('v4 ->', sim.matfile_version(v4))   # 预期 (0, 0)
print('v5 ->', sim.matfile_version(v5))   # 预期 (1, 0)

# (b) 用内部函数 _get_matfile_version（需自己传已打开的字节流）
with open(v5, 'rb') as f:
    print('v5 raw ->', _get_matfile_version(f))   # 预期 (1, 0)
```

> 进阶观察：用十六进制工具查看 v5 文件，偏移 124-127 处应能看到 `00 01 49 4D`（小端）或 `01 00 4D 49`（大端）。例如 `big_endian.mat` 对应后者，`little_endian.mat` 对应前者（**待本地验证**）。

#### 4.3.5 小练习与答案

**练习 1**：一个**空文件**传给 `_get_matfile_version` 会怎样？

**答案**：读到的 `hdr_bytes` 长度小于 20，命中第 235-236 行，抛 `MatReadError("Mat file appears to be truncated")`。

**练习 2**：为什么 v5 和 v7.3 用的是**同一段**判定代码（读偏移 124 的 4 字节）？

**答案**：因为 v7.3 文件也带一个「MATLAB 5.0 ...」风格的 128 字节头，区别只在于偏移 124-125 的版本整数的**主版本值**——v5 是 1，v7.3 是 2。同一段代码 `if maj_val in (1, 2)` 自然把它们都正确分类。

**练习 3**：若一个文件的 4 字节探针里既没有 `'I'` 也没有合理的版本号，会发生什么？

**答案**：`maj_val` 既不是 1 也不是 2，命中第 256 行，抛 `ValueError('Unknown mat file type, ...')`。

---

### 4.4 工厂分发：`mat_reader_factory`

#### 4.4.1 概念说明

知道版本号之后，下一步是**为这个版本选一个合适的 reader**。这正是工厂函数 `mat_reader_factory` 的工作：

1. 用 4.2 节的 `_open_file` 把输入统一成字节流；
2. 用 4.3 节的 `_get_matfile_version` 探测版本；
3. 按主版本号 `mjv` 分发：
   - `mjv == 0` → `MatFile4Reader`
   - `mjv == 1` → `MatFile5Reader`
   - `mjv == 2` → v7.3，**抛 `NotImplementedError`**，提示用 `h5py`；
   - 其它 → `TypeError`。

返回值是 `(reader 实例, file_opened)`，`file_opened` 来自 `_open_file`，告诉上层是否需要关闭文件。

`loadmat` 和 `whosmat` 都不直接构造 reader，而是统一经由 `mat_reader_factory`——这样「文件打开 + 版本判定 + reader 选择」这三步只在**一个地方**实现，避免了重复与不一致。

#### 4.4.2 核心流程

```
mat_reader_factory(file_name, appendmat=True, **kwargs)
   │
   ├─ byte_stream, file_opened = _open_file(file_name, appendmat)
   ├─ mjv, mnv = _get_matfile_version(byte_stream)
   │
   ├─ mjv == 0 → return MatFile4Reader(byte_stream, **kwargs), file_opened
   ├─ mjv == 1 → return MatFile5Reader(byte_stream, **kwargs), file_opened
   ├─ mjv == 2 → raise NotImplementedError("...use HDF reader...e.g. h5py")
   └─ else     → raise TypeError(f"Did not recognize version {mjv}")
```

`**kwargs` 透传的是 reader 的读取选项（如 `squeeze_me`、`struct_as_record`、`byte_order` 等），这些是 u3-l2、u3-l3 的内容，本讲只需知道「它们原样塞给 reader 构造器」。

#### 4.4.3 源码精读

工厂函数本体：

[scipy/io/matlab/_mio.py:56-79](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/matlab/_mio.py#L56-L79) —— 第 69 行打开文件、第 70 行判定版本、第 71-79 行四路分发。注意 v7.3（`mjv == 2`）不是返回 reader，而是直接抛 `NotImplementedError` 并点名 `h5py`。

两个调用方的对比（都走工厂）：

[scipy/io/matlab/_mio.py:246-249](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/matlab/_mio.py#L246-L249) —— `loadmat` 在 `_open_file_context` 上下文里调用 `mat_reader_factory`，再 `MR.get_variables(...)`。

[scipy/io/matlab/_mio.py:401-404](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/matlab/_mio.py#L401-L404) —— `whosmat` 同样走工厂，但调用的是 `ML.list_variables()`（只列变量名/形状/类型，不真正读数据）。

> 设计要点：`loadmat` 第 248 行写作 `MR, _ = mat_reader_factory(...)`（丢弃 `file_opened`），是因为文件关闭已由外层 `with _open_file_context(...)` 兜底；`whosmat` 第 402 行则保留 `ML, file_opened = ...`。两者都依赖工厂统一处理「打开+判定+分发」。

#### 4.4.4 代码实践

1. **目标**：验证工厂确实把 v4 文件分发给 `MatFile4Reader`、v5 文件分发给 `MatFile5Reader`。
2. **步骤**：调用 `mat_reader_factory`，检查返回对象的类名。
3. **预期现象**：v4 → `MatFile4Reader`；v5 → `MatFile5Reader`。
4. **预期结果**：见代码注释。**待本地验证**。

```python
# 示例代码
import os
import scipy.io as sio
from scipy.io.matlab._mio import mat_reader_factory

data_dir = os.path.join(os.path.dirname(sio.__file__),
                        'matlab', 'tests', 'data')
files = {
    'v4': os.path.join(data_dir, 'testdouble_4.2c_SOL2.mat'),
    'v5': os.path.join(data_dir, 'testdouble_7.4_GLNX86.mat'),
}
for tag, fname in files.items():
    MR, opened = mat_reader_factory(fname)
    print(tag, '->', type(MR).__name__, '| opened =', opened)
    # 预期：
    #   v4 -> MatFile4Reader | opened = True
    #   v5 -> MatFile5Reader | opened = True
    MR.mat_stream.close()   # 工厂打开的文件，由我们负责关闭
```

> 进阶：试着构造一个 v7.3 文件并调用 `mat_reader_factory`，应得到 `NotImplementedError`，提示 `Please use HDF reader for matlab v7.3 files, e.g. h5py`。子包测试数据 `matlab/tests/data/testhdf5_7.4_GLNX86.mat` 即是一个 HDF5/v7.3 样本（**待本地验证**）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `loadmat` 可以写 `MR, _ = mat_reader_factory(...)`，丢弃第二个返回值？

**答案**：因为 `loadmat` 是在 `with _open_file_context(file_name, appendmat)` 块里调用工厂的，文件的关闭由该上下文管理器在退出时统一负责；`file_opened` 这个布尔值对 `loadmat` 本身已无用处，故丢弃。

**练习 2**：`mat_reader_factory` 对 v7.3 文件返回什么？

**答案**：不返回任何 reader，而是抛 `NotImplementedError`，并在消息中建议改用 HDF5 读取库（如 `h5py`），因为 SciPy 不实现 v7.3/HDF5 接口。

**练习 3**：如果把 `squeeze_me=True` 传给 `whosmat`，这个参数最终流向哪里？

**答案**：`whosmat` 的 `**kwargs` 被透传给 `mat_reader_factory`，再经工厂的 `**kwargs` 传给 `MatFile4Reader`/`MatFile5Reader` 的构造器（即 `MatFileReader.__init__` 的读取选项）。不过 `whosmat` 只调用 `list_variables()`，多数读取选项对「仅列变量」并无实际影响。

## 5. 综合实践

把本讲的四个最小模块串起来，完成一次「打开 → 判版本 → 选 reader → 列变量」的完整追踪。这个任务覆盖了子包导出、文件打开、版本判定与工厂分发全部知识点。

**任务**：写一段脚本，对 `matlab/tests/data` 下的一个 v4 文件和一个 v5 文件，分别用**三种粒度**的 API 完成等价操作，并对比输出。

```python
# 示例代码：综合实践
import os
import scipy.io as sio
import scipy.io.matlab as sim
from scipy.io.matlab._mio import mat_reader_factory, _open_file
from scipy.io.matlab._miobase import _get_matfile_version

data_dir = os.path.join(os.path.dirname(sio.__file__),
                        'matlab', 'tests', 'data')
samples = {
    'v4': 'testdouble_4.2c_SOL2.mat',
    'v5': 'testdouble_7.4_GLNX86.mat',
}

for tag, name in samples.items():
    path = os.path.join(data_dir, name)
    print(f'===== {tag}: {name} =====')

    # 粒度 1：高层 API whosmat（内部自动完成打开+判版本+工厂+列变量）
    print('whosmat       :', sio.whosmat(path))

    # 粒度 2：公共 matfile_version（只看版本号）
    print('matfile_ver   :', sim.matfile_version(path))

    # 粒度 3：手动拆解三步 —— 打开 → 判版本 → 工厂
    stream, opened = _open_file(path, appendmat=True)
    mjv, mnv = _get_matfile_version(stream)     # 注意：会把指针重置到 0
    MR, _ = mat_reader_factory(path)             # 工厂内部会再次打开+判定
    print('manual ver    :', (mjv, mnv))
    print('reader class  :', type(MR).__name__)
    print('list_variables:', MR.list_variables())
    stream.close(); MR.mat_stream.close()
    print()
```

**观察要点**：

1. v4 与 v5 在三处 API 上分别给出 `(0,0)`/`(1,0)` 与 `MatFile4Reader`/`MatFile5Reader`，三者互相印证。
2. `whosmat` 的输出（变量名、形状、类型字符串）与 `list_variables()` 完全一致——证明 `whosmat` 内部就是「工厂 + `list_variables`」。
3. `_get_matfile_version` 调用后流指针被重置到 0（docstring 已声明这一副作用），这也是工厂能接着用同一个流继续读的原因。

**预期结果**（由源码逻辑推得，**待本地验证**）：

- v4 文件：版本 `(0, 0)`，reader 为 `MatFile4Reader`，`whosmat` 返回形如 `[('testdouble', (1, 16), 'double')]` 的列表。
- v5 文件：版本 `(1, 0)`，reader 为 `MatFile5Reader`，`whosmat` 返回形如 `[('testdouble', (1, 9), 'double')]` 的列表。

## 6. 本讲小结

- `scipy.io.matlab` 是一个按职责拆分的**子包**，公共名字由 `__init__.py` 的 `__all__` 精确枚举；不带 `_` 前缀的 `mio`/`miobase` 等是 SciPy 2.0 将移除的弃用包装。
- `_open_file` 用鸭子类型把「文件名」与「已打开的文件对象」统一成字节流，并通过 `opened` 标志区分「谁该负责关闭」；`_open_file_context` 把它包成安全的 `with` 上下文。
- `_get_matfile_version` 用**三段式**判版本：读前 20 字节查截断/损坏 → 前 4 字节含 0 即 v4 → 否则读偏移 124 的版本整数与字节序探针，用 `'I'` 的位置同时解出主次版本号（1=v5，2=v7.3）。
- `mat_reader_factory` 是工厂：打开文件 → 判版本 → 按主版本号分发到 `MatFile4Reader`/`MatFile5Reader`，对 v7.3 抛 `NotImplementedError` 并建议 `h5py`。
- `loadmat`/`whosmat` 都不直接构造 reader，而是统一经由工厂，从而「打开+判定+选择」只实现一次。

## 7. 下一步学习建议

本讲只打通了「入口链路」，还没有真正读取任何矩阵数据。建议按以下顺序继续：

1. **u3-l2 MatFileReader 基类与字节序处理**：先读 `_miobase.py` 里的抽象基类 `MatFileReader`/`MatVarReader`、四个异常/警告类，以及 `_byteordercodes.py` 如何把 `'native'`/`'BIG'` 等字符串转成 numpy 字节码。这是理解所有 reader 共性的前提。
2. **u3-l3 loadmat / savemat / whosmat 主流程**：精读 `_mio.py` 三个公共函数的完整调用链，重点理解 `struct_as_record`、`squeeze_me`、`simplify_cells` 等读取选项如何改变返回结构。
3. 之后再按 u3-l4（v4）→ u3-l5（v5 读）→ u3-l6（v5 写）的顺序逐版本深入，最后用 u3-l7 收口 Cython 底层。
4. 若对「工厂分发后 reader 内部如何按 `mjv` 走不同解析路径」感兴趣，可在读完 u3-l2 后，对照本讲 `mat_reader_factory` 的四路 `if/elif`，追踪 `MatFile4Reader` 与 `MatFile5Reader` 各自的 `matrix_getter_factory`。
