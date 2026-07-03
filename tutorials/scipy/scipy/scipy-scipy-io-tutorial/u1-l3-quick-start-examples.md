# 快速上手：导入与各格式最小读写示例

## 1. 本讲目标

本讲是 scipy.io 学习手册的第一篇「动手」讲义。前两讲（u1-l1、u1-l2）已经讲清了 scipy.io 是什么、目录怎么组织、公共/私有模块如何约定。本讲不再讲概念，而是让读者**亲手跑通一个完整的「输入 → 处理 → 输出」循环**。

学完本讲你应该能够：

1. 正确写出 `scipy.io` 及其子模块 `scipy.io.wavfile` 的导入语句，并说清楚两者关系。
2. 用 `numpy` 生成一段正弦波数据，调用 `scipy.io.wavfile.write` 写成 WAV 文件，再调用 `scipy.io.wavfile.read` 读回，验证数据一致（round-trip）。
3. 知道 SciPy 安装包里自带了一批测试数据文件，能用 `scipy.io.__file__` 定位到 `tests/data` 目录，并复用其中的 `.wav` 文件做练习。

本讲只覆盖 WAV 这一种格式，作为后续逐格式精读（u2-l1 WAV、u2-l2 Fortran……）的「热身」。WAV 的二进制细节（chunk 结构、字节序、RF64）会在 u2-l1 专门展开，本讲不深入。

## 2. 前置知识

在动手之前，先建立三个直觉。

**音频信号与采样。** 声音是连续的空气压力变化，计算机只能存离散数值。所谓「采样」就是每隔固定时间记一个数值，这个间隔的倒数叫**采样率**（sample rate，单位 Hz，即每秒采几个点），常见的 CD 音质是 44100 Hz。一段 1 秒、44100 Hz 的单声道声音，就是 44100 个数值。**声道数**（channels）指同时有几路音频，单声道是 1，立体声是 2。

**位深与 dtype。** 每个采样值用几个字节存，叫**位深**（bit depth）。16 位整数用 `numpy` 的 `int16` 存，范围是 \([-32768, 32767]\)；32 位浮点用 `float32` 存，范围通常落在 \([-1.0, +1.0]\)。scipy.io.wavfile 直接用 numpy 数组的 `dtype` 来决定写出的 WAV 是哪种格式——这是本讲最重要的一个对应关系。

一个标准正弦波的数学形式是：

\[
y(t) = A \sin(2\pi f t)
\]

其中 \(A\) 是振幅，\(f\) 是频率（Hz），\(t\) 是时间（秒）。440 Hz 就是国际标准音「A4」。

**round-trip（往返一致性）。** 把一个数组写成文件，再读回数组，理想情况下两个数组逐元素相等。验证 round-trip 是判断一个读写器是否正确最直接的方法，本讲的实践任务就围绕它展开。

如果你对 numpy 数组、dtype、`np.sin`、`np.linspace` 不熟悉，建议先花十分钟过一遍 NumPy 快速入门，再回到本讲。

## 3. 本讲源码地图

本讲只涉及两个文件，分工如下：

| 文件 | 在本讲的作用 |
| --- | --- |
| `scipy/io/__init__.py` | scipy.io 的「总目录」，用 `from . import ... wavfile` 把子模块挂到顶层，并自动生成 `__all__`。我们只看它如何把 `wavfile` 暴露出来。 |
| `scipy/io/wavfile.py` | WAV 读写器的真正实现，提供 `read` / `write` / `WavFileWarning` 三个公共名字。本讲只用到 `read` 和 `write` 两个函数。 |

另外，我们会引用 `scipy/io/tests/data/` 目录下的现成 `.wav` 测试文件，但那是数据文件，不是源码。

> 提示：`wavfile.py` 是 scipy.io 里少数「实现就在无前缀文件里」的特例（详见 u1-l2），所以 `scipy.io.wavfile` 既是子模块名，也直接包含实现代码。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：先理清导入关系，再完成 write/read 的 round-trip，最后学会用自带测试数据。

### 4.1 导入 scipy.io 与 wavfile 子模块

#### 4.1.1 概念说明

在 SciPy 的设计里，`scipy.io` 是一个**命名空间包**（namespace package），它把多种格式的读写器汇总到一个入口。WAV 比较特殊：它不是把 `read`/`write` 直接 re-export 到 `scipy.io` 顶层（不像 `loadmat`、`netcdf_file` 那样），而是保留一个**子模块** `scipy.io.wavfile`，用户通过 `scipy.io.wavfile.read(...)` 来访问。

这样做的原因是 `read`/`write`/`write` 这类名字太通用（标准库 `wave` 模块、很多其他库都有 `read`），直接放到 `scipy.io` 顶层容易引发命名冲突，所以放在子模块里更安全。

#### 4.1.2 核心流程

导入与调用的标准流程：

```
1. import scipy.io              # 拿到顶层命名空间
2. import scipy.io.wavfile      # （也可省略，下一步会触发）拿到子模块
3. scipy.io.wavfile.write(...)  # 通过子模块访问函数
4. scipy.io.wavfile.read(...)   # 同上
```

也可以写 `from scipy.io import wavfile` 或 `from scipy.io.wavfile import read, write`，效果等价，按个人风格选择。

#### 4.1.3 源码精读

先看顶层包如何把 `wavfile` 暴露出来。`__init__.py` 用一行 `from . import ...` 同时导入了多个「弃用命名空间」子模块，`wavfile` 就在其中：

[scipy/io/__init__.py:114-115](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/__init__.py#L114-L115) — 把 `arff`、`harwell_boeing`、`idl`、`mmio`、`netcdf`、`wavfile` 这几个子模块导入到顶层，使 `scipy.io.wavfile` 可用。（注释说明其中前五个是「将在 v2.0.0 移除的弃用命名空间」，而 `wavfile` 不在此列，是稳定 API。）

随后一行生成公共 API 列表：

[scipy/io/__init__.py:117](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/__init__.py#L117) — `__all__` 用列表推导式自动收录所有「不以 `_` 开头」的名字，体现了 u1-l1 讲过的「不带下划线即公共」约定。

再看 `wavfile.py` 自己声明的公共名字：

[scipy/io/wavfile.py:20-24](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/wavfile.py#L20-L24) — `__all__ = ['WavFileWarning', 'read', 'write']`，这就是该子模块对外的全部公共 API。

#### 4.1.4 代码实践

**实践目标：** 确认 `scipy.io.wavfile` 可访问，且 `read`/`write` 是函数对象。

**操作步骤：**

```python
import scipy.io
import scipy.io.wavfile as w

print(type(w))                 # 应为 module 类型
print(hasattr(w, "read"))      # True
print(hasattr(w, "write"))     # True
print(scipy.io.wavfile is w)   # True，说明两种写法指向同一对象
```

**需要观察的现象：** 最后一行打印 `True`，说明 `import scipy.io` 之后再用 `scipy.io.wavfile` 访问与显式 `import` 到的 `w` 是同一个模块对象。

**预期结果：** 三行 `True` / 一个 module 对象。如果 `scipy.io.wavfile is w` 为 `True`，说明 Python 的模块缓存机制生效（同一模块只加载一次）。

#### 4.1.5 小练习与答案

**练习 1：** 下列三种导入里，哪种会失败？

```python
(A) from scipy.io import wavfile
(B) from scipy.io.wavfile import read, write
(C) import read from scipy.io.wavfile
```

**答案：** (C) 失败，Python 没有 `import X from Y` 这种语法。正确写法是 (B) 的 `from Y import X`。(A)、(B) 都合法。

**练习 2：** 为什么 SciPy 不把 `read`/`write` 直接放到 `scipy.io` 顶层（像 `loadmat` 那样）？

**答案：** `read`/`write` 名字过于通用，容易与其他库（如标准库 `wave`、各种流式 API）冲突。放在 `scipy.io.wavfile` 子模块下能明确「这是 WAV 专用」，访问时自带命名空间前缀，安全且语义清晰。

### 4.2 wavfile.write 与 wavfile.read：完成一次 round-trip

#### 4.2.1 概念说明

这是本讲的核心。`write(filename, rate, data)` 把一个 numpy 数组按其 `dtype` 写成标准（未压缩）WAV 文件；`read(filename)` 读回，返回 `(采样率, 数据数组)` 元组。

关键认知是：**`write` 用 `data.dtype` 反推 WAV 格式**，用户不需要（也没法）显式指定「我要写 16 位 PCM」。具体映射规则（来自 `write` 内部）：

| numpy dtype | WAV 格式 | 位深 | 数值范围 |
| --- | --- | --- | --- |
| `int16` | PCM | 16 位 | \([-32768, 32767]\) |
| `int32` | PCM | 32 位 | \([-2^{31}, 2^{31}-1]\) |
| `uint8` | PCM | 8 位（无符号） | \([0, 255]\) |
| `float32` | IEEE_FLOAT | 32 位浮点 | 通常 \([-1.0, +1.0]\) |
| `float64` | IEEE_FLOAT | 64 位浮点 | 通常 \([-1.0, +1.0]\) |

声道数则由数组的维度决定：1-D 数组 → 单声道；2-D 数组（shape `(Nsamples, Nchannels)`）→ 多声道。

#### 4.2.2 核心流程

一次 round-trip 的伪代码：

```
生成数据：
    t = 在 [0, 1] 内取 samplerate 个时间点
    data = (振幅 * sin(2π * 频率 * t)).astype(int16)   # 1-D, 单声道

写出：
    write(filename, samplerate, data)
        → 校验 dtype 是否在允许列表内
        → 用 dtype 推出 format_tag / channels / bit_depth
        → 写 RIFF 头 + fmt chunk + data chunk

读回：
    rate, readback = read(filename)
        → 解析 RIFF 头（RIFF/RIFX/RF64）
        → 在 chunk 循环里找到 'fmt ' 和 'data'
        → data 区按 dtype 还原为 numpy 数组

验证：
    assert rate == samplerate
    assert np.array_equal(readback, data)   # round-trip 一致
```

#### 4.2.3 源码精读

先看 `write` 如何决定格式。它先校验 dtype 合法性：

[scipy/io/wavfile.py:851-855](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/wavfile.py#L851-L855) — 检查 `data.dtype.name` 是否在 `['float32', 'float64', 'uint8', 'int16', 'int32', 'int64']` 这个白名单里，不在就抛 `ValueError`。这就是「dtype 决定格式」的入口。

接着从 dtype 推导 WAV 头字段：

[scipy/io/wavfile.py:864-878](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/wavfile.py#L864-L878) — `dkind == 'f'` 选 `IEEE_FLOAT`，否则选 `PCM`；`data.ndim == 1` 则 `channels = 1`，否则取 `data.shape[1]`；`bit_depth = dtype.itemsize * 8`。然后用 `struct.pack` 打包 fmt chunk。

再看 `read` 的入口，它对「文件句柄 vs 路径字符串」做了统一处理：

[scipy/io/wavfile.py:717-724](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/wavfile.py#L717-L724) — 如果传入对象有 `read` 方法（文件句柄）就直接用，否则用路径 `open(..., 'rb')`；若流不支持随机定位（`seekable()` 为 False），就包一层 `SeekEmulatingReader` 模拟 seek。（这部分细节留到 u2-l1。）

`read` 最后返回采样率和数据：

[scipy/io/wavfile.py:786](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/wavfile.py#L786) — `return fs, data`，这就是 `rate, data = wavfile.read(...)` 写法的来源。注意返回顺序是**采样率在前，数据在后**，初学者很容易记反。

`write` 的 docstring 里本身就给了一个标准范例（生成 100Hz 正弦波）：

[scipy/io/wavfile.py:829-840](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/wavfile.py#L829-L840) — 官方示例：用 `np.iinfo(np.int16).max` 取 int16 最大值作振幅，`np.sin(2π·fs·t)` 生成正弦，`astype(np.int16)` 转成 16 位整型后写出。本讲实践任务正是基于这个范式。

#### 4.2.4 代码实践

**实践目标：** 用 numpy 生成 1 秒、440Hz 的正弦波，写成 `test.wav` 再读回，确认采样率、声道数、数值全部一致。

**操作步骤：**

```python
import numpy as np
from scipy.io import wavfile

samplerate = 44100
frequency = 440          # A4 音高
t = np.linspace(0., 1., samplerate)          # 44100 个时间点，跨度 1 秒
amplitude = np.iinfo(np.int16).max           # = 32767
data = (amplitude * np.sin(2. * np.pi * frequency * t)).astype(np.int16)

# 写出
wavfile.write("test.wav", samplerate, data)

# 读回
rate, readback = wavfile.read("test.wav")
print(f"采样率   = {rate} Hz")
print(f"声道数   = {1 if readback.ndim == 1 else readback.shape[1]}")
print(f"dtype    = {readback.dtype}")
print(f"样本数   = {readback.shape[0]}")
print(f"数值一致 = {np.array_equal(readback, data)}")
```

**需要观察的现象：**

- `write` 静默执行，不返回值（返回 `None`），在当前目录生成 `test.wav`，大小应为 `44100 × 2 字节 + 头部(44 字节) ≈ 88244 字节`。
- `read` 返回的 `readback` 是 1-D 数组（单声道），`dtype` 为 `int16`。

**预期结果：**

```
采样率   = 44100 Hz
声道数   = 1
dtype    = int16
样本数   = 44100
数值一致 = True
```

`数值一致 = True` 即说明 round-trip 成功——写出去的数据被无损读回。

> 说明：本步骤的「预期结果」是基于源码逻辑（`int16` 1-D 数组 → 单声道 16 位 PCM）推断的；实际数值是否逐位相等，待本地运行确认。若你想听这段声音，可以用系统播放器打开 `test.wav`，会听到一个 440Hz 的纯音蜂鸣。

#### 4.2.5 小练习与答案

**练习 1：** 如果把上例的 `data` 改成 `.astype(np.float32)`，再 `write`/`read`，`readback.dtype` 会是什么？数值还会逐位相等吗？

**答案：** `dtype` 会变成 `float32`（因为 `dkind == 'f'` 触发 `IEEE_FLOAT` 格式，32 位）。数值在合理范围（如 \([-1, 1]\)）内通常仍逐位相等，因为 WAV 的 float 编码就是 IEEE 754 原值，无缩放。但若振幅超出 float 的常规范围，`read` docstring 明确指出「Values exceeding [-1, +1] are not clipped」（见 [wavfile.py:671](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/wavfile.py#L671)），不会被截断。

**练习 2：** 想写一个**立体声** WAV，`data` 应该是什么形状？

**答案：** 形状应为 `(Nsamples, 2)`，即 2-D 数组，第二维 = 声道数。`write` 在 [wavfile.py:869-872](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/wavfile.py#L869-L872) 用 `data.ndim == 1` 判断单声道，否则取 `data.shape[1]` 作声道数。

**练习 3：** 为什么 `read` 返回 `int16` 而不是 `float`？换句话说，读回的 dtype 由什么决定？

**答案：** 由**文件本身**的 fmt chunk 里的 `format_tag` 和 `bit_depth` 决定，`read` 会据此反推 numpy dtype（见 [wavfile.py:491-508](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/wavfile.py#L491-L508)）。因为写出去的是 16 位 PCM，读回来自然还原成 `int16`，而不是 `read` 自作主张转成 float。

### 4.3 定位并使用 SciPy 自带的测试数据文件

#### 4.3.1 概念说明

做练习时经常需要「现成的样例文件」，自己造数据既麻烦又容易错。好在 SciPy 安装包里自带了一整套测试数据，放在 `scipy/io/tests/data/` 目录下，涵盖了 WAV、IDL `.sav`、Fortran `.dat`、NetCDF `.nc` 等几乎所有格式的样例文件。

但这里有个工程问题：用户机器上 SciPy 的安装路径不固定（可能是 site-packages、可能是 conda 环境、可能是开发安装），不能写死绝对路径。标准做法是用 `scipy.io.__file__`（即 `scipy.io` 包的 `__init__.py` 文件路径）作锚点，再用 `os.path.dirname` 和 `os.path.join` 拼出数据目录。

#### 4.3.2 核心流程

```
1. import scipy.io
2. data_dir = os.path.join(os.path.dirname(scipy.io.__file__), 'tests', 'data')
3. fname = os.path.join(data_dir, '某个.wav')
4. rate, data = scipy.io.wavfile.read(fname)
```

`scipy.io.__file__` 指向 `.../scipy/io/__init__.py`，`dirname` 去掉文件名得到 `.../scipy/io/`，再拼 `tests/data` 就是数据目录。

测试数据的文件名本身也编码了关键信息，命名规律大致是 `test-<采样率>Hz-<字节序>-<声道>ch-<位深>-<类型>.wav`，例如：

- `test-44100Hz-2ch-32bit-float-be.wav` —— 44100 Hz、2 声道、32 位浮点、大端（big-endian）
- `test-8000Hz-le-1ch-1byte-ulaw.wav` —— 8000 Hz、小端、单声道、1 字节（µ-law，本格式 scipy 不支持读取，仅作样本）
- `test-44100Hz-le-1ch-4bytes.wav` —— 44100 Hz、小端、单声道、4 字节（即 32 位整数）

看文件名就能挑到合适的练习素材，不用打开文件。

#### 4.3.3 源码精读

`read` 的 docstring 里就直接示范了这套定位方法（这也是本模块最值得抄的范式）：

[scipy/io/wavfile.py:686-697](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/wavfile.py#L686-L697) — 用 `pjoin(dirname(scipy.io.__file__), 'tests', 'data')` 定位数据目录，选取 `test-44100Hz-2ch-32bit-float-be.wav`，然后 `samplerate, data = wavfile.read(wav_fname)`，演示读出的声道数为 2、时长为 0.01 秒。

这个 docstring 示例本身就是 SciPy 文档测试（doctest）的一部分，意味着它能稳定跑通——`test-44100Hz-2ch-32bit-float-be.wav` 这个文件确实存在于 `scipy/io/tests/data/`（本仓库当前 HEAD 下已确认）。

`read` 之所以能接受路径字符串，靠的就是 [wavfile.py:717-724](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/wavfile.py#L717-L724) 里那段「路径 → `open(..., 'rb')`」的逻辑，所以拼出来的绝对路径可以直接喂给 `read`。

#### 4.3.4 代码实践

**实践目标：** 用自带测试数据文件练习读取，熟悉「以 `__file__` 为锚点定位数据」的套路。

**操作步骤：**

```python
import os
import numpy as np
import scipy.io
from scipy.io import wavfile

# 以 scipy.io 安装位置为锚点定位测试数据目录
data_dir = os.path.join(os.path.dirname(scipy.io.__file__), 'tests', 'data')
print("数据目录：", data_dir)

# 列出目录下所有 .wav 文件
wavs = sorted(f for f in os.listdir(data_dir) if f.endswith('.wav'))
print(f"共有 {len(wavs)} 个 .wav 文件，例如：\n" + "\n".join(wavs[:5]))

# 读取一个立体声浮点样例
fname = os.path.join(data_dir, 'test-44100Hz-2ch-32bit-float-be.wav')
rate, data = wavfile.read(fname)
print(f"\n文件: {os.path.basename(fname)}")
print(f"采样率 = {rate} Hz")
print(f"形状   = {data.shape}   # (样本数, 声道数)")
print(f"dtype  = {data.dtype}")
print(f"时长   = {data.shape[0] / rate:.4f} 秒")
```

**需要观察的现象：**

- `data_dir` 打印出一个绝对路径，形如 `.../site-packages/scipy/io/tests/data`（开发安装时可能是源码树路径）。
- 该目录下能列出二十余个 `.wav` 文件，文件名都遵循上述编码规律。
- `test-44100Hz-2ch-32bit-float-be.wav` 读出来 `data.shape` 的第二个维度是 2（立体声），`dtype` 是 `float32`，时长约 0.01 秒。

**预期结果：**

```
数据目录： /.../scipy/io/tests/data
共有 22 个 .wav 文件，例如：
test-1234Hz-le-1ch-10S-20bit-extra.wav
test-44100Hz-2ch-32bit-float-be.wav
...

文件: test-44100Hz-2ch-32bit-float-be.wav
采样率 = 44100 Hz
形状   = (441, 2)   # (样本数, 声道数)
dtype  = float32
时长   = 0.0100 秒
```

（`.wav` 文件的确切个数与目录路径取决于本地 SciPy 版本与安装方式，待本地确认；当前 HEAD 下该目录含 22 个 `.wav` 文件。）

#### 4.3.5 小练习与答案

**练习 1：** 为什么不能写 `data_dir = "/usr/lib/python3.x/site-packages/scipy/io/tests/data"` 这种硬编码路径？

**答案：** 因为 SciPy 的安装位置因操作系统、Python 发行版（CPython/conda）、虚拟环境、安装方式（pip/开发安装）而异，硬编码路径在别人的机器上几乎一定不存在。用 `scipy.io.__file__` 动态定位才能跨环境通用。

**练习 2：** 文件名 `test-8000Hz-le-3ch-5S-24bit.wav` 里，`le`、`3ch`、`5S`、`24bit` 分别表示什么？

**答案：** `le` = little-endian（小端字节序）；`3ch` = 3 声道；`5S` = 5 个样本（Sample）；`24bit` = 每个采样 24 位。这是 scipy.io 测试数据自创的命名约定，便于一眼看出文件特征。

**练习 3：** 如果想读一个**大端**（big-endian）的 WAV 来验证 `read` 能正确处理字节序，应该选哪个测试文件？

**答案：** 选文件名含 `-be-` 的，如 `test-44100Hz-be-1ch-4bytes.wav` 或 `test-44100Hz-2ch-32bit-float-be.wav`。`read` 在 [wavfile.py:565-605](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/io/wavfile.py#L565-L605) 的 `_read_riff_chunk` 里会根据 `RIFF`/`RIFX` 签名判定字节序，这部分细节在 u2-l1 展开。

## 5. 综合实践

把本讲三个模块串起来，完成一个「生成 → 写出 → 读回 → 分析」的完整任务。

**任务：** 制作一个 0.5 秒的立体声「双音」WAV——左声道 440Hz、右声道 880Hz——写出后读回，分别打印左右声道在第 0、第 100、第最后一个样本处的值，并验证左右声道频率确实不同。

**参考实现：**

```python
import numpy as np
from scipy.io import wavfile

samplerate = 44100
t = np.linspace(0., 0.5, samplerate // 2)          # 0.5 秒
amp = np.iinfo(np.int16).max

left  = (amp * np.sin(2 * np.pi * 440 * t)).astype(np.int16)
right = (amp * np.sin(2 * np.pi * 880 * t)).astype(np.int16)
stereo = np.column_stack([left, right])            # shape = (N, 2)

wavfile.write("duotone.wav", samplerate, stereo)

rate, readback = wavfile.read("duotone.wav")
print("采样率 =", rate, " 形状 =", readback.shape, " dtype =", readback.dtype)
for i in (0, 100, len(readback) - 1):
    print(f"样本 {i}: 左={readback[i,0]:>7d}  右={readback[i,1]:>7d}")

# 频率自检：对左右声道做一点 FFT，找主峰
freqs = np.fft.rfftfreq(len(readback), 1 / rate)
peak_l = freqs[np.argmax(np.abs(np.fft.rfft(readback[:, 0])))]
peak_r = freqs[np.argmax(np.abs(np.fft.rfft(readback[:, 1])))]
print(f"左声道主频 ≈ {peak_l:.0f} Hz, 右声道主频 ≈ {peak_r:.0f} Hz")
```

**预期现象：**

- `readback.shape` 第二维为 2，`dtype` 为 `int16`，验证 4.2 节「2-D 数组 → 多声道」的规则。
- 同一时刻左右声道数值不同（因为频率不同），证明声道被正确拆分。
- FFT 主峰分别接近 440 Hz 和 880 Hz，说明信号在 round-trip 后频率信息完好。

这个任务用到了 4.1 的导入约定、4.2 的 write/read round-trip、以及 numpy 的数组操作，是对本讲内容的综合检验。FFT 部分若不熟悉可跳过，仅靠「左右声道数值不同」也能验证。

## 6. 本讲小结

- `scipy.io.wavfile` 是一个**子模块**，`read`/`write` 通过 `scipy.io.wavfile.read(...)` 访问，没有被 re-export 到顶层，原因是这两个名字太通用。
- `write(filename, rate, data)` 用 **`data.dtype` 反推 WAV 格式**（float → IEEE_FLOAT，整型 → PCM），用**数组维度**决定声道数（1-D → 单声道，2-D → 多声道）。
- `read(filename)` 返回 `(采样率, 数据)` 元组，**采样率在前、数据在后**；读回的 dtype 由文件本身的 fmt chunk 决定。
- 验证读写正确性最直接的方法是 **round-trip**：`write` 出去再 `read` 回来，比较 `np.array_equal`。
- 定位测试数据的标准套路是 `os.path.join(os.path.dirname(scipy.io.__file__), 'tests', 'data')`，不要写死绝对路径。
- 测试数据文件名编码了采样率、字节序、声道、位深等信息，看文件名即可挑选合适的练习素材。

## 7. 下一步学习建议

本讲只让 WAV「跑起来」，没有拆它的二进制结构。建议按以下顺序继续：

1. **进入 u2-l1（WAV 音频文件读写 wavfile.py）**：精读 RIFF chunk 结构、`_read_fmt_chunk` / `_read_data_chunk` 的解析逻辑、`SeekEmulatingReader` 如何在不支持 seek 的流上模拟定位、以及 RF64 大文件支持。本讲的 `read`/`write` 只是入口，u2-l1 才是 WAV 的完整地图。
2. **横向对比其他格式**：学完 WAV 后，可快速浏览 u2-l3（Matrix Market）、u2-l4（ARFF）等纯文本格式，体会 scipy.io 「一格式一读写器」的设计统一性——每种格式都有独立的 `read`/`write`，但调用风格高度一致。
3. **回顾 u1-l2**：如果对 `scipy.io.wavfile` 为何是「实现就在无前缀文件里」的特例还有疑问，可重温 u1-l2 关于公共/私有模块约定的部分。
