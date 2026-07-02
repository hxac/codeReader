# 三种数据集的加载与转换

## 1. 本讲目标

在上一讲 `u2-l1` 里，我们打开了 `fetch_data` 这个黑盒，看清楚了数据文件是如何联网下载、SHA256 校验、落到本地缓存目录的。但当时我们刻意停在了一句话上：**`fetch_data` 返回的是本地文件路径，而不是数组本身**。从「一个磁盘上的文件」到「一个 `numpy.ndarray`」，中间这一段「解封装 + 转换」的活儿，就是本讲的主角。

`scipy.datasets` 一共只有三个数据集方法：`ascent`、`face`、`electrocardiogram`。有趣的是，它们各自用的是**完全不同的文件格式**和**完全不同的本地加载手段**。读完本讲，你应当能够：

1. 看懂三个数据集函数如何统一遵循「先用 `fetch_data` 拿到本地路径，再各自解封装原始文件」的两段式结构。
2. 掌握 `pickle.load`、`bz2.decompress`、`np.load` 三种本地加载手段分别对应什么文件格式、解决什么问题。
3. 读懂 `electrocardiogram` 中把 ADC 原始 `uint16` 换算成毫伏（mV）的公式 `(ecg - 1024) / 200.0` 的物理含义，并理解为什么这里要先 `astype(int)`。

## 2. 前置知识

在进入源码前，先用通俗语言过一遍本讲涉及的几个概念。

- **`fetch_data(dataset_name)`**：上一讲 `u2-l1` 讲过的「薄助手」。它做完整性校验与（必要时）联网下载，返回下载文件在本地缓存中的**绝对路径字符串**。本讲的所有讨论都从「我们已经拿到了这个路径」开始。
- **`registry`**：上一讲 `u2-l2` 讲过的「文件名 → SHA256 哈希」字典。pooch 用它在下载/缓存命中时做完整性校验。本讲会反复提到三个文件名 `ascent.dat`、`ecg.dat`、`face.dat`，它们正是 `registry` 的键。
- **序列化（serialization）**：把内存里的 Python 对象（比如一个嵌套列表）转成可以存进文件、之后还能原样读回来的字节流。`pickle` 是 Python 标准库自带的序列化方案。
- **压缩（compression）**：用更少的字节表示同一份数据。`bz2` 是 Python 标准库提供的一种压缩算法，对应 `.bz2` 文件。本讲里 `face.dat` 存的就是「bz2 压缩后的原始图像字节」。
- **`numpy.ndarray`**：SciPy 全家桶通用的多维数组类型。三个数据集函数最终都返回 `ndarray`，差别只在 `dtype`（元素类型）和 `shape`（形状）。
- **`frombuffer`**：numpy 提供的函数，把一段**原始字节流**按指定的 `dtype` 重新解释成一个一维数组。这是「字节流 → 数组」的最低层手段，没有任何文件头解析。
- **npz 文件**：numpy 的「压缩存档」格式，一个 `.npz` 文件里可以同时装多个命名数组。读取时像操作字典一样用名字取数组。
- **ADC（模数转换器）**：Analog-to-Digital Converter，把连续的模拟电压信号采样、量化成一串整数。心电信号在仪器端先被 ADC 转成一串「ADC 计数值」存下来，我们在软件端要把它**反推**回真实的毫伏电压。

## 3. 本讲源码地图

本讲仍然聚焦在这两个文件，但视角从「配置/获取」转向「解析/转换」：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [_fetchers.py](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_fetchers.py) | 实现三个数据集函数 `ascent / face / electrocardiogram` | 三种文件格式各自的解封装与后处理逻辑 |
| [_registry.py](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_registry.py) | 维护三张映射表 | 三个数据文件名（`ascent.dat` / `ecg.dat` / `face.dat`）如何与方法名对应 |

> 关键铺垫：三个函数长得几乎一样——开头都是 `fname = fetch_data("xxx.dat")`。真正的差异**全在这一行之后**。本讲就是逐行拆解「这一行之后」发生了什么。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：① 三个函数共有的两段式结构；② `ascent` 的 pickle 加载；③ `face` 的 bz2 解压、`frombuffer`/reshape 与灰度转换；④ `electrocardiogram` 的 `np.load` 与 ADC 换算。

### 4.1 三个函数共有的两段式结构

#### 4.1.1 概念说明

虽然三个数据集函数返回的数组千差万别（一张灰度图、一张彩色图、一段一维心电信号），但它们的实现都遵循同一个骨架：

1. **第一段——拿路径**：调用 `fetch_data("xxx.dat")`，确保文件已经在本地缓存里（上一讲 `u2-l1` 的全部内容），拿到它的本地路径。
2. **第二段——解封装**：用 Python/numpy 的标准工具把这个**文件**读进内存、转换成 `ndarray`。

为什么要把这两段分开？因为「下载/校验/缓存」是所有数据集共享的通用逻辑（pooch 负责），而「解析文件格式」是每个数据集各自的私事。把它们解耦之后，pooch 这一层可以独立演进，数据集这一层也可以独立修改，互不干扰。

#### 4.1.2 核心流程

三个函数的共同骨架可以用伪代码表示：

```text
def 某数据集():
    fname = fetch_data("xxx.dat")   # 第一段：拿本地路径（pooch 负责）
    # ---- 第二段：各自解封装（本讲重点）----
    with open(fname, ...) as f:     # 打开文件
        ...                         # 反序列化 / 解压 / np.load
    ...                             # 必要的后处理（reshape、灰度、ADC 换算…）
    return ndarray
```

差别只在第二段的「打开方式」和「后处理」上。下表先给一个全局对照，后面三节再分别展开。

| 函数 | 文件名 | 文件格式 | 读取手段 | 后处理 | 最终 dtype / shape |
| --- | --- | --- | --- | --- | --- |
| `ascent` | `ascent.dat` | pickle | `pickle.load` | `array(...)` | `uint8` / `(512, 512)` |
| `face` | `face.dat` | bz2 压缩原始字节 | `bz2.decompress` + `frombuffer` | `reshape`，可选灰度 | `uint8` / `(768, 1024, 3)` |
| `electrocardiogram` | `ecg.dat` | npz 存档 | `np.load` | `astype(int)` + ADC 换算 | `float64` / `(108000,)` |

#### 4.1.3 源码精读

三个函数顶部的 `@xp_capabilities(out_of_scope=True)` 装饰器与 `fname = fetch_data(...)` 这一行是它们共同的「外貌特征」：

```python
# _fetchers.py 第 42-43 行：ascent 的装饰器与函数头
@xp_capabilities(out_of_scope=True)
def ascent():
```

> [ascent 装饰器与函数定义 _fetchers.py:L42-L43](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_fetchers.py#L42-L43) —— `@xp_capabilities(out_of_scope=True)` 标记这些数据集函数**不在**「数组 API 适配范围」内（数组 API 是另一套跨框架的数组标准，属于高级话题，本讲不展开）；`ascent()` 不接受参数。

`face` 与 `electrocardiogram` 顶上也挂着同样的装饰器：

> [face 的装饰器与签名 _fetchers.py:L183-L184](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_fetchers.py#L183-L184) —— `face(gray=False)`，唯一可选参数 `gray` 控制是否返回灰度图。

> [electrocardiogram 的装饰器与签名 _fetchers.py:L83-L84](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_fetchers.py#L83-L84) —— `electrocardiogram()` 不接受参数，返回一维心电信号。

而 `_registry.py` 里，三个文件名正是 `registry` 的键，也通过 `method_files_map` 与方法名一一对应：

> [registry：三个文件名及其 SHA256 _registry.py:L8-L12](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_registry.py#L8-L12) —— `fetch_data("ascent.dat")` 里的字符串 `"ascent.dat"` 就是从这张表的键来的；pooch 用右边的 SHA256 做校验。

> [method_files_map：方法名到文件名的映射 _registry.py:L22-L26](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_registry.py#L22-L26) —— 注意 `electrocardiogram` 方法对应的文件名是 `ecg.dat`，方法名与文件名**并不相同**，这张表就是用来弥合这个命名差异的（`u2-l4` 讲缓存清理时会再次用到它）。

#### 4.1.4 代码实践

**实践目标**：用肉眼确认三个函数都遵循「先 `fetch_data` 拿路径、再各自解析」的两段式结构。

**操作步骤**：

1. 打开 [_fetchers.py](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_fetchers.py)，分别定位 `ascent`（第 43 行）、`electrocardiogram`（第 84 行）、`face`（第 184 行）三个函数体。
2. 在每个函数体里找到 `fname = fetch_data(...)` 这一行，记下它各自传入的字符串。
3. 把这三个字符串与 `_registry.py` 的 `registry`（第 8-12 行）的键做对照。

**需要观察的现象**：三个函数传入的字符串分别是 `"ascent.dat"`、`"ecg.dat"`、`"face.dat"`，恰好是 `registry` 的三个键；而 `fetch_data(...)` 这一行的下方，三个函数立刻分道扬镳，出现了 `import pickle`、`import bz2`、`load(...)` 三种完全不同的写法。

**预期结果**：你会直观地看到「第一段相同、第二段各异」——这正是本讲要拆解的全部内容。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `fetch_data` 只返回路径、不直接返回数组？
> **参考答案**：因为「下载/校验/缓存」是三个数据集共享的通用职责，而「把文件解析成数组」是每个数据集各自的私事。返回路径正好把这两层干净地切开：pooch 只管把文件放到磁盘上，数据集函数自己决定怎么读。

**练习 2**：方法名 `electrocardiogram` 和它实际读取的文件名 `ecg.dat` 不一样，这会带来什么影响？
> **参考答案**：调用 `fetch_data` 时必须用文件名 `ecg.dat`（因为这是 `registry` 的键）；而做缓存清理等「按方法名」操作时需要的是 `electrocardiogram`。`_registry.py` 的 `method_files_map`（第 22-26 行）正是用来在「方法名」与「文件名」之间做翻译的桥梁。

---

### 4.2 ascent：pickle.load 反序列化

#### 4.2.1 概念说明

`ascent` 返回的是一张经典的 \(512 \times 512\) 灰度测试图（常用于演示滤波、卷积等图像算法）。它的文件 `ascent.dat` 用的是 **pickle 格式**——也就是 Python 标准库 `pickle` 序列化后的产物。

什么是 pickle？你可以把它理解为「把一个 Python 对象冻干成字节流存进文件，以后再用 `pickle.load` 原样复活」。在 `ascent` 这个例子里，文件里冻干的是一个**嵌套的 Python 列表**（一个长度 512 的列表，每个元素又是一个长度 512 的列表，元素是 0~255 的整数）。`numpy.array(...)` 再把这个嵌套列表组装成一个 `uint8` 的二维数组。

为什么用 pickle 而不是直接存 `.npy`？这是**历史原因**：这张图最早来自 `scipy.misc.ascent`，当年就是用 pickle 存的；为了保持字节级一致、不破坏旧缓存，`scipy.datasets.ascent` 沿用了同一份文件。

#### 4.2.2 核心流程

```text
fname = fetch_data("ascent.dat")     # 拿到本地路径
打开 fname（二进制读模式 'rb'）
   │
   └─ pickle.load(f)                 # 把字节流复活成 Python 嵌套列表
         │
         └─ array(...)               # 嵌套列表 → ndarray，dtype 推断为 uint8
```

注意 pickle 读出来的是**普通 Python 对象**（嵌套 `list`），还不是 numpy 数组；是最后那层 `array(...)` 把它「升级」成了 `ndarray`。

#### 4.2.3 源码精读

`ascent` 的解析部分只有短短三行，但信息量不小：

> [ascent 的 pickle 加载 _fetchers.py:L71-L80](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_fetchers.py#L71-L80) —— 函数体内：`import pickle`（局部导入）→ `fetch_data("ascent.dat")` 拿路径 → `open(...,'rb')` 二进制读 → `pickle.load(f)` 反序列化 → `array(...)` 转 ndarray → 返回。

关键点逐条说明：

- 第 71 行 `import pickle` 写在**函数体内**而不是文件顶部。这是 Python 的一种风格选择：`pickle` 只在这个函数里用到，就近导入能让依赖关系一目了然。`face` 里的 `import bz2`（第 216 行）也是同理。
- 第 78 行 `open(fname, 'rb')`：**必须用二进制模式 `'rb'`**。pickle 存的是字节流，文本模式 `'r'` 会触发解码导致损坏。
- 第 79 行 `array(pickle.load(f))`：`pickle.load(f)` 先还原出嵌套列表，外层的 `numpy.array(...)`（文件顶部第 3 行 `from numpy import array` 已导入）把它转成 `ndarray`。由于元素都是 0~255 的整数，numpy 自动推断出 `dtype=uint8`，与文档示例 `ascent.max()` 返回 `np.uint8(255)` 一致。

#### 4.2.4 代码实践

**实践目标**：验证 `ascent()` 的返回值确实是 `uint8`、形状 `(512, 512)`，并理解 `pickle.load` 与 `array(...)` 各自的角色。

**操作步骤**：

```python
# 示例代码
import scipy.datasets
import pickle
import numpy as np

arr = scipy.datasets.ascent()
print("shape:", arr.shape, "dtype:", arr.dtype, "max:", arr.max())

# 进阶：直接看 pickle.load 的中间产物（需要先拿到缓存路径）
from scipy.datasets._fetchers import fetch_data
fname = fetch_data("ascent.dat")
with open(fname, 'rb') as f:
    raw = pickle.load(f)          # 这是「升级成 ndarray 之前」的 Python 对象
print("raw 的类型:", type(raw))   # 应为 list
print("raw 的长度:", len(raw))    # 应为 512
print("首行长度:", len(raw[0]))   # 应为 512
print("array 后:", np.array(raw).shape, np.array(raw).dtype)
```

**需要观察的现象**：`arr.shape` 为 `(512, 512)`，`arr.dtype` 为 `uint8`，`arr.max()` 为 `255`；而 `pickle.load` 的中间产物 `raw` 是一个普通 `list`（不是 ndarray），长度 512，每行又是一个长度 512 的列表。

**预期结果**：你会清楚地看到「`pickle.load` 产出 Python 列表 → `array(...)` 把它变成 `uint8` 的 ndarray」这一两步转换。

> 如果本地未安装可选依赖 `pooch`，或当前环境无法联网，`fetch_data("ascent.dat")` 会抛出 `ImportError` / 下载错误——这正是上一讲 `u2-l1` 讲过的「用到才报错」与下载机制。本实践**待本地验证**具体数值。

#### 4.2.5 小练习与答案

**练习 1**：如果把第 78 行的 `open(fname, 'rb')` 改成 `open(fname, 'r')`（去掉 `b`），会发生什么？
> **参考答案**：pickle 文件是二进制字节流，用文本模式 `'r'` 打开会触发字符解码，遇到非合法 UTF-8 字节会抛 `UnicodeDecodeError`；即便侥幸不报错，读出的内容也被破坏，`pickle.load` 会失败。所以二进制模式 `'rb'` 不可省。

**练习 2**：既然有更「原生」的 `.npy` 格式，为什么 `ascent` 还用 pickle？
> **参考答案**：纯历史原因。这张图源自旧的 `scipy.misc.ascent`，当年用 pickle 存储。`scipy.datasets` 复用了同一份字节文件，以保证哈希值与历史缓存不变；改格式收益很小、破坏兼容性代价大，因此沿用至今。

---

### 4.3 face：bz2 解压 + frombuffer/reshape + 可选灰度

#### 4.3.1 概念说明

`face` 返回的是一张 \(768 \times 1024\) 的浣熊彩色照片（RGB 三通道）。它的文件 `face.dat` 既不是 pickle，也不是 numpy 格式，而是一段 **bz2 压缩后的原始图像字节**。

可以把它想象成：把图像的所有像素按 `行优先`、`RGB` 的顺序一字排开，每个通道占 1 字节（`uint8`），得到一长串原始字节，再用 bz2 压缩存盘。读取时就要反过来走三步：

1. **解压**：`bz2.decompress` 把压缩字节还原成原始字节流。
2. **重组**：`numpy.frombuffer` 把这段字节流按 `uint8` 解释成一维数组，再 `reshape` 成 `(768, 1024, 3)`。
3. **可选灰度**：若调用方传 `gray=True`，按人眼亮度感知把 RGB 三通道加权合并成一个单通道灰度图。

`frombuffer` 是这里最底层的一步——它不做任何「文件头解析」，纯粹是「把字节流按指定 dtype 切成数组」。正因如此，它要求你**自己负责**给出正确的 `dtype` 和 `reshape` 后的形状。

#### 4.3.2 核心流程

```text
fname = fetch_data("face.dat")
打开 fname（'rb'）→ f.read()            # 读入压缩字节 rawdata
   │
   └─ bz2.decompress(rawdata) → face_data  # 解压成原始字节流
         │
         └─ frombuffer(face_data, dtype='uint8')  # 字节 → 一维 uint8 数组
               │
               └─ reshape((768, 1024, 3)) → face  # 一维 → 三维彩色图

若 gray=True：
   gray = 0.21*R + 0.71*G + 0.07*B → astype('uint8')  # 加权灰度
```

灰度转换的加权公式为：

\[ \text{gray} = 0.21\,R + 0.71\,G + 0.07\,B \]

三个系数之和 \(0.21 + 0.71 + 0.07 = 1.0\)，之所以不平均（各占 \(1/3\)），是因为**人眼对绿色最敏感、对蓝色最不敏感**，这套经验权重能让灰度图的明暗更接近人眼的真实感受。

#### 4.3.3 源码精读

`face` 的解析部分把上述三步写得非常紧凑：

> [face 的 bz2 解压与 reshape _fetchers.py:L216-L221](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_fetchers.py#L216-L221) —— `import bz2`（第 216 行局部导入）→ `fetch_data("face.dat")` 拿路径 → `f.read()` 读压缩字节 → `bz2.decompress` 解压 → `frombuffer(..., dtype='uint8')` 转一维数组 → `reshape((768, 1024, 3))` 成彩色图。

逐行说明：

- 第 218-219 行 `with open(fname, 'rb') as f: rawdata = f.read()`：把整个压缩文件一次性读进 `rawdata`（一个 `bytes` 对象）。注意这里**先读字节、再解压**，两步分开。
- 第 220 行 `face_data = bz2.decompress(rawdata)`：bz2 解压。解压后 `face_data` 仍是 `bytes`，但长度应该等于 \(768 \times 1024 \times 3 = 2{,}359{,}296\) 字节。
- 第 221 行 `frombuffer(face_data, dtype='uint8').reshape((768, 1024, 3))`：`frombuffer`（文件顶部第 3 行 `from numpy import frombuffer` 已导入）按 `uint8` 把字节流切成一维数组，紧接着 `reshape` 成 `(高, 宽, 通道)` 的三维数组。`reshape` 能成功的前提是字节总数恰好等于 \(768 \times 1024 \times 3\)，否则会抛 `ValueError`。

灰度分支则更靠后：

> [face 的灰度转换 _fetchers.py:L222-L225](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_fetchers.py#L222-L225) —— 当 `gray is True` 时，用 `0.21/0.71/0.07` 对三个通道做加权求和，再 `.astype('uint8')` 截回 8 位整数；最后无论是否灰度都 `return face`。

注意第 222 行的判断写的是 `if gray is True:`（用 `is` 而非 `==`）。对布尔值而言二者等价，但 `is` 更明确地表达了「只接受真布尔 `True`」的意图。

#### 4.3.4 代码实践

**实践目标**：验证 `face()` 默认返回彩色图 `(768, 1024, 3)`、`face(gray=True)` 返回灰度图 `(768, 1024)`，并核验解压后的字节总数恰好等于像素总数。

**操作步骤**：

```python
# 示例代码
import scipy.datasets
import bz2

color = scipy.datasets.face()
gray  = scipy.datasets.face(gray=True)
print("color:", color.shape, color.dtype)   # 期望 (768, 1024, 3) uint8
print("gray :", gray.shape,  gray.dtype)    # 期望 (768, 1024)    uint8

# 进阶：核验 bz2 解压后的字节总数
from scipy.datasets._fetchers import fetch_data
fname = fetch_data("face.dat")
with open(fname, 'rb') as f:
    rawdata = f.read()
decompressed = bz2.decompress(rawdata)
expected = 768 * 1024 * 3
print("解压后字节数:", len(decompressed), "预期:", expected,
      "一致:", len(decompressed) == expected)
```

**需要观察的现象**：彩色图形状 `(768, 1024, 3)`、灰度图形状 `(768, 1024)`，两者 `dtype` 均为 `uint8`；解压后的字节数恰好等于 \(768 \times 1024 \times 3 = 2{,}359{,}296\)。

**预期结果**：字节数与预期完全一致，这正是第 221 行 `reshape((768, 1024, 3))` 能成功的前提。本实践**待本地验证**（依赖 `pooch` 与联网下载）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `face` 要「先 `f.read()` 读字节、再 `bz2.decompress`」，而不是像 `pickle.load` 那样直接把文件对象传进去？
> **参考答案**：`bz2.decompress` 接收的是一段完整的 `bytes`，不是文件对象；而 `pickle.load` 接收的是文件对象（它自己会按需读）。两种库的 API 设计不同，所以调用方式也不同。

**练习 2**：灰度系数为什么是 `0.21 / 0.71 / 0.07` 而不是各 \(1/3\)？
> **参考答案**：这是基于人眼亮度感知的经验权重——人眼对绿光最敏感、对蓝光最不敏感，所以绿色权重最大、蓝色最小。加权后的明暗比简单平均更接近视觉感受。三个系数之和恰好为 1.0，保证整体亮度不偏移。

**练习 3**：如果把第 221 行的 `reshape((768, 1024, 3))` 改成 `reshape((1024, 768, 3))`，图像会怎样？
> **参考答案**：由于总元素数相同，`reshape` 不会报错，但行/列被互换，图像会被「转置」——浣熊会变成横躺的。这正说明 `reshape` 只改变维度的解释方式，不改变底层数据顺序。

---

### 4.4 electrocardiogram：np.load(npz) + ADC 换算

#### 4.4.1 概念说明

`electrocardiogram` 返回的是一段 5 分钟长的心电信号，采样率 360 Hz，共 \(360 \times 5 \times 60 = 108000\) 个采样点。它的文件 `ecg.dat` 实际上是一个 **npz 存档**（numpy 的多数组压缩格式），里面以名字 `"ecg"` 存着一串原始的 `uint16` 整数。

最关键、也最容易被忽略的一步是 **ADC 换算**。心电信号在仪器端是模拟电压，经过 ADC（模数转换器）后被量化成一串「ADC 计数值」存盘。我们在软件端拿到的是这串**整数计数值**，必须反推回真实的毫伏（mV）电压才能用于分析。源码里的换算公式是：

\[ \text{ecg}_{\text{mV}} = \frac{\text{raw} - 1024}{200.0} \]

这里有两个常量，源码注释把它们叫做 `adc_zero` 与 `adc_gain`：

- **`adc_zero = 1024`（零点偏移）**：ADC 计数值 `1024` 对应真实电压 \(0\,\text{mV}\)。原始数据是 `uint16`（无符号，范围 0~65535），所以「零电压」并不在 0，而在中点附近的 1024。减去它，就是把坐标原点平移到「真正的 0 V」。
- **`adc_gain = 200.0`（增益）**：每 \(200\) 个 ADC 计数对应 \(1\,\text{mV}\)，也就是仪器的转换灵敏度。除以它，就把「计数值」换算成了「毫伏」。

这套 `baseline=1024`、`gain=200` 的参数，正好对应 PhysioNet MIT-BIH 心律失常数据库（本数据源自其中的 record 208）的标准 WFDB 约定。

#### 4.4.2 核心流程

```text
fname = fetch_data("ecg.dat")
with load(fname) as file:                # np.load 打开 npz 存档
    ecg = file["ecg"].astype(int)        # 取出名为 "ecg" 的数组，转成有符号 int

# ADC 换算：(ecg - adc_zero) / adc_gain
ecg = (ecg - 1024) / 200.0               # 计数值 → 毫伏(mV)，结果为 float64
return ecg
```

**为什么要先 `astype(int)` 再做减法？** 这是本节最值得记住的一个细节。原始数组是 `uint16`（无符号 16 位整数）。如果直接拿 `uint16` 减 `1024`，凡是小于 1024 的样本（对应真实负电压）都会**下溢回绕**成巨大的正数（比如 \(5 - 1024\) 会变成 \(65535 - 1019 = 64516\)），彻底毁掉信号。先 `.astype(int)` 把它转成有符号整数，减法才能正确产生负值。

`load`（即 `numpy.load`）在文件顶部第 3 行已导入（`from numpy import array, frombuffer, load`）。当传入的是 `.npz` 文件时，它返回一个 `NpzFile`，可以像字典一样用 `file["ecg"]` 取出其中的命名数组；`with ... as file` 则确保文件句柄用完即关。

#### 4.4.3 源码精读

`electrocardiogram` 的解析部分只有四行，但每一行都不可省：

> [electrocardiogram 的 npz 读取与 ADC 换算 _fetchers.py:L175-L180](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_fetchers.py#L175-L180) —— `fetch_data("ecg.dat")` 拿路径 → `load(fname)` 打开 npz → `file["ecg"].astype(int)` 取数组并转有符号整数 → `(ecg - 1024) / 200.0` 做 ADC 换算 → 返回。

逐行说明：

- 第 175 行 `fname = fetch_data("ecg.dat")`：和前两个函数一样，先拿到本地路径。注意文件名是 `ecg.dat` 而非 `electrocardiogram.dat`。
- 第 176 行 `with load(fname) as file:`：`load` 是 `numpy.load`。虽然文件后缀是 `.dat`，但内容是 npz 格式，`np.load` 靠**文件头魔数**而非后缀来识别格式，所以能正确打开。
- 第 177 行 `ecg = file["ecg"].astype(int)`：从 npz 里取出名为 `"ecg"` 的数组（原始 `uint16`），`.astype(int)` 转成有符号整数，为下一步减法做准备。
- 第 178-179 行 `# Convert raw output of ADC to mV: (ecg - adc_zero) / adc_gain` 与 `ecg = (ecg - 1024) / 200.0`：注释直接点明了换算意图——把 ADC 原始输出转成 mV，`adc_zero=1024`、`adc_gain=200.0`。除以 `200.0`（浮点）会让整个数组提升为 `float64`，文档示例里 `ecg.mean()` 得到 `-0.16510875` 这样的浮点数正是因为这一步。

文档示例明确给出了换算后的统计量，可作为验证基准：

> [electrocardiogram 文档示例中的统计量 _fetchers.py:L124-L125](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_fetchers.py#L124-L125) —— `ecg.shape, ecg.mean(), ecg.std()` 的期望输出为 `((108000,), -0.16510875, 0.5992473991177294)`，这就是本讲综合实践要核对的「黄金数值」。

#### 4.4.4 代码实践

**实践目标**：调用 `electrocardiogram()`，验证其均值、标准差与文档示例（约 `-0.16510875` / `0.5992473991177294`）一致，并用注释解释 ADC 换算公式的含义。

**操作步骤**：

```python
# 示例代码：验证 electrocardiogram 的统计量，并解释 ADC 换算
import numpy as np
from scipy.datasets import electrocardiogram

ecg = electrocardiogram()

# 1) 形状与采样点数核对：360 Hz * 5 分钟 * 60 秒 = 108000
print("shape:", ecg.shape, "| dtype:", ecg.dtype)
assert ecg.shape == (108000,), "采样点数应为 108000"

# 2) 与文档示例的「黄金数值」比对
print("mean:", ecg.mean(), "| std:", ecg.std())
np.testing.assert_allclose(ecg.mean(), -0.16510875,   atol=1e-6)
np.testing.assert_allclose(ecg.std(),  0.5992473991177294, atol=1e-9)

# 3) 注释解释 ADC 换算公式的含义：
#    原始 ecg.dat 存的是 ADC 计数值（uint16）。
#    (raw - 1024) / 200.0 的含义：
#      - 1024 是 adc_zero（零点偏移）：计数值 1024 对应 0 mV；
#      - 200.0 是 adc_gain（增益）：每 200 个计数对应 1 mV；
#    两步合起来把「ADC 计数值」反推回「毫伏电压」。
#    先 .astype(int) 是为了避免 uint16 减法在负值处下溢回绕。
```

**需要观察的现象**：`ecg.shape` 为 `(108000,)`、`dtype` 为 `float64`；`ecg.mean()` 约为 `-0.16510875`，`ecg.std()` 约为 `0.5992473991177294`，两条 `assert_allclose` 均通过。

**预期结果**：运行无断言失败，说明 ADC 换算后的统计量与官方文档完全一致，反向印证了 `(ecg - 1024) / 200.0` 的正确性。本实践**待本地验证**（依赖 `pooch` 与联网下载 `ecg.dat`）。

#### 4.4.5 小练习与答案

**练习 1**：如果删掉第 177 行的 `.astype(int)`，直接对 `uint16` 做 `(ecg - 1024) / 200.0`，`ecg.mean()` 会变成什么样？
> **参考答案**：`uint16` 减法在样本值小于 1024 时会下溢回绕成接近 65535 的大正数，导致大量本应为负的样本被算成很大的正值，`mean()` 会显著偏大甚至接近几百，完全偏离 `-0.165`。这正是源码必须先 `.astype(int)` 的原因。

**练习 2**：已知采样率为 360 Hz、时长 5 分钟，请验证返回数组的长度应为 108000。
> **参考答案**：\(360 \times 60 \times 5 = 360 \times 300 = 108000\)，与 `ecg.shape == (108000,)` 吻合。

**练习 3**：`ecg.dat` 后缀是 `.dat`，为什么能用 `numpy.load` 打开？
> **参考答案**：`numpy.load` 靠文件内容的**魔数（magic bytes）**识别格式，而不是看后缀名。`ecg.dat` 实际存的是 npz 格式，文件头是 `"PK"`（zip 魔数），`np.load` 据此正确识别并返回 `NpzFile`。后缀只是给人和 `registry` 看的标签。

---

## 5. 综合实践

设计一个把三个数据集串起来的小任务，帮助你把本讲的知识整体回顾一遍。

**任务**：写一个脚本，依次加载三个数据集，打印它们的 `shape` 与 `dtype`，并用一张对照表回答「这个数据集用了哪种文件格式、哪种读取手段、做了哪种后处理」。最后回答一个综合问题：**三者为什么不能用同一种方式加载？**

**参考脚本**：

```python
# 示例代码：三种数据集加载综合对照
import scipy.datasets as d

cases = [
    ("ascent",          d.ascent(),                      "pickle",                "array(...)"),
    ("face (color)",    d.face(),                        "bz2 + frombuffer",      "reshape((768,1024,3))"),
    ("face (gray)",     d.face(gray=True),               "bz2 + frombuffer",      "reshape + 加权灰度"),
    ("electrocardiogram", d.electrocardiogram(),         "np.load(npz)",          "(ecg-1024)/200.0 ADC 换算"),
]

print(f"{'name':<18}{'shape':<22}{'dtype':<10}{'format':<20}{'postprocess'}")
for name, arr, fmt, post in cases:
    print(f"{name:<18}{str(arr.shape):<22}{str(arr.dtype):<10}{fmt:<20}{post}")
```

**需要观察的现象**：四行输出的 `shape` / `dtype` 分别符合 `(512,512) uint8`、`(768,1024,3) uint8`、`(768,1024) uint8`、`(108000,) float64`；后处理列一目了然地区分了三种解析方式。

**思考要点（综合问题）**：三者文件格式各不相同——`ascent.dat` 是历史遗留的 pickle、`face.dat` 是高压缩比的 bz2 字节流（适合体积大的彩色图）、`ecg.dat` 是 numpy 原生的 npz（适合一维数值序列）。pooch 只负责把文件**原样**搬到本地，并不关心里面是什么；**「用什么方式解析」完全由每个数据集根据自己的文件特点决定**。这就是为什么 `scipy.datasets` 把「获取」与「解析」分成两段、且解析方式各不相同——它尊重了每个数据集的历史与格式现实，而不是强求统一。

> 本实践依赖 `pooch` 与首次联网下载，**待本地验证**。

## 6. 本讲小结

- 三个数据集函数都遵循**两段式结构**：先用 `fetch_data("xxx.dat")` 拿到本地路径（pooch 负责），再各自用不同手段把文件解析成 `ndarray`。
- `ascent` 用 **`pickle.load`** 反序列化一个嵌套 Python 列表，再用 `array(...)` 升级为 `uint8` 的 `(512, 512)` 数组——这是历史遗留格式。
- `face` 先 `f.read()` 读压缩字节，用 **`bz2.decompress`** 解压，再用 **`frombuffer(...,'uint8').reshape((768,1024,3))`** 重组为彩色图；`gray=True` 时按 \(0.21R+0.71G+0.07B\) 加权转灰度。
- `electrocardiogram` 用 **`np.load`** 打开 npz 存档取出 `"ecg"` 数组，先 `.astype(int)` 避免 `uint16` 减法下溢，再做 **ADC 换算** `(ecg - 1024) / 200.0`（`adc_zero=1024`、`adc_gain=200`）把计数值反推回毫伏。
- 三种文件格式（pickle / bz2 / npz）对应三种读取手段，体现了「pooch 只管搬运、解析方式由各数据集自己定」的解耦设计。
- 验证 ADC 换算正确性的「黄金数值」是文档示例里的 `mean ≈ -0.16510875`、`std ≈ 0.5992473991177294`。

## 7. 下一步学习建议

- 本讲只讲「怎么把文件读成数组」，没有讲「怎么把缓存**删掉**」。下一讲 **`u2-l4` 缓存清理机制** 会进入 `_utils.py`，讲 `clear_cache` 与 `_clear_cache` 如何借助 `_registry.py` 的 `method_files_map`（本讲 4.1 节提到的「方法名↔文件名」桥梁）按方法名定位并删除缓存文件。
- 如果你对 `fetch_data` 内部「下载/校验/缓存命中」的细节还想再看一遍，建议回头重读 **`u2-l1`**；对 `registry` / `registry_urls` / `method_files_map` 三张表的分工想再确认，可重读 **`u2-l2`**。
- 进阶方向：阅读 [tests/test_data.py](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/tests/test_data.py)，看测试如何断言这三个数据集的 `shape` / `dtype` / 统计量与文件哈希，这将是你理解「数据集不可变契约」的最佳材料（对应专家层 `u3-l3` 测试设计剖析）。
