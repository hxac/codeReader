# 第一次运行与缓存初探

## 1. 本讲目标

前两讲我们读了 `scipy.datasets` 的「门面」(`__init__.py`) 和「目录结构」，但还停留在纸面。本讲要真正把代码跑起来。

读完本讲，你应当能够：

1. 安装可选依赖 `pooch`，并成功调用 `ascent / face / electrocardiogram` 三个数据集函数，拿到 `numpy.ndarray`。
2. 说清「首次联网下载 → SHA256 校验 → 写入本地缓存 → 之后命中缓存不再重复下载」这条完整流程。
3. 在 macOS / Linux / Windows 上准确找到名为 `scipy-data` 的缓存目录的绝对路径。
4. 知道当 `pooch` 这个可选依赖缺失时，调用会怎样失败、失败信息长什么样。

## 2. 前置知识

- **数据集（dataset）**：一段被 SciPy 用来做演示、测试、教学示例的「现成数据」。例如一张 512×512 的灰度图、一段心电图信号。调用一个函数就能拿到它，你不需要自己去找文件。
- **可选依赖（optional dependency）**：SciPy 核心功能不需要它，但某个子功能（这里就是 `scipy.datasets` 的下载能力）必须有它。`pooch` 就是这样的可选依赖——不装也能 `import scipy`，但调用数据集函数会报错。
- **Pooch**：一个第三方 Python 库，专门用来「下载并缓存数据文件」。它替我们处理了 HTTP 下载、断点、哈希校验、本地缓存目录选择等脏活累活。
- **SHA256 哈希**：对一个文件算出来的一串固定长度的字符（64 个十六进制字符），可以看作文件的「指纹」。下载完文件后重算指纹、和预先记录的指纹比对，若一致就说明文件没被篡改、也没传坏。这就是「完整性校验」。
- **缓存（cache）**：把第一次下载的文件存到本地某个目录，下次再要时直接从本地读，不再走网络。本讲的 `scipy-data` 目录就是缓存目录。

> 本讲依赖 u1-l1（公开 API）和 u1-l2（各文件职责）。你应当已经知道 `ascent / face / electrocardiogram` 是定义在 `_fetchers.py` 里、经 `__init__.py` 导出的公开函数。

## 3. 本讲源码地图

| 文件 | 在本讲的作用 |
| --- | --- |
| [`__init__.py`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/__init__.py) | 模块文档字符串里明确写了三个平台的缓存路径，是我们「定位缓存目录」的权威依据。 |
| [`_fetchers.py`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_fetchers.py) | 三个数据集函数、`fetch_data` 下载助手、`pooch` 的可选依赖降级、`pooch.os_cache("scipy-data")` 缓存路径设置，全在这里。 |
| [`_registry.py`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_registry.py) | 提供 `registry`（SHA256 哈希表）和 `registry_urls`（远程地址），是「下载哪个文件、校验什么哈希」的数据来源。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：①三个数据集函数怎么调用；②缓存目录是怎么决定的；③`pooch` 缺失时的表现。

### 4.1 三个数据集函数的调用方式

#### 4.1.1 概念说明

`scipy.datasets` 对外暴露三个「数据集方法」：`ascent`、`face`、`electrocardiogram`。它们的使用方式被刻意设计得极简——**像调用一个无参函数那样调用它**，它就返回一个 `numpy.ndarray`：

- `ascent()` → 512×512 的 8 位灰度图（uint8）。
- `face()` → 768×1024×3 的彩色浣熊脸部图（uint8），可选参数 `gray=True` 转灰度。
- `electrocardiogram()` → 长度 108000 的一维浮点数组，一段 5 分钟、360 Hz 采样的心电图（单位 mV）。

这三者背后的共同机制是：**第一次调用时联网下载、之后走本地缓存**。模块文档字符串把这条流程讲得很直白。

#### 4.1.2 核心流程

一次 `scipy.datasets.ascent()` 调用的总流程：

```text
ascent()
  └─ fetch_data("ascent.dat")          # 下载数据文件，返回本地路径
       └─ data_fetcher.fetch(...)      # pooch 负责
            ├─ 缓存目录里已有 ascent.dat？
            │     是 → 直接返回本地路径（不联网）
            │     否 → 联网下载 → 算 SHA256 与 registry 比对 → 写入缓存 → 返回本地路径
  └─ open(fname).pickle.load(...)      # 用本地工具读取文件内容
  └─ return numpy.ndarray
```

关键点：**「下载」和「读取」是两步**。`fetch_data` 只负责把文件放到本地并返回路径；至于这个文件是 pickle、bz2 还是 npz，由各自的函数用对应的 Python 工具去读。本讲我们聚焦「下载 + 缓存」这一步，「读取」细节留到 u2-l3。

#### 4.1.3 源码精读

模块文档字符串对「调用方式」和「下载后存哪」的说明（中文要点已在旁注）：

[__init__.py:L40-L54](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/__init__.py#L40-L54)
这段说明：数据集文件存在遵循 `dataset-<name>` 命名的独立 GitHub 仓库；`scipy.datasets` 依赖 Pooch，由 Pooch 用这些仓库来取回文件；并维护了一份「文件名 → SHA256 哈希 + 仓库 url」的注册表，Pooch 用它来校验下载；下载一次后，文件被存到系统缓存目录下的 `scipy-data` 里。

`ascent()` 函数体（最能体现「下载即返回路径、之后走缓存」的注释）：

[_fetchers.py:L71-L80](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_fetchers.py#L71-L80)
注释明确写道：「第一次运行时文件会自动下载，返回下载文件的路径；之后 Pooch 在本地缓存里找到它，不再重复下载。」`fetch_data("ascent.dat")` 拿到路径 `fname` 后，再用 `pickle.load` 读取并转成 `numpy.array` 返回。

另外两个函数的下载入口与之同构，只是文件名和读取方式不同：

[_fetchers.py:L175-L176](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_fetchers.py#L175-L176)
`electrocardiogram()` 调用 `fetch_data("ecg.dat")`，随后用 `np.load` 读取。

[_fetchers.py:L217](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_fetchers.py#L217)
`face()` 调用 `fetch_data("face.dat")`，随后用 `bz2.decompress` 解压。

#### 4.1.4 代码实践

**实践目标**：确认三个数据集函数都能成功调用，并观察各自的 shape/dtype。

**操作步骤**：

1. 确认已安装可选依赖：`python -c "import pooch; print(pooch.__version__)"`。若无输出且报 `ModuleNotFoundError`，先安装：`pip install pooch`。
2. 新建脚本 `try_datasets.py`：

   ```python
   # 示例代码
   import scipy.datasets as d

   a = d.ascent()
   f = d.face()
   e = d.electrocardiogram()

   print("ascent :", a.shape, a.dtype)
   print("face   :", f.shape, f.dtype)
   print("ecg    :", e.shape, e.dtype)
   ```

3. 运行：`python try_datasets.py`。

**需要观察的现象**：第一次运行时终端会有进度条 / 下载提示（Pooch 在联网拉取三个文件）；运行结束后打印三行 shape/dtype。

**预期结果**：

```text
ascent : (512, 512) uint8
face   : (768, 1024, 3) uint8
ecg    : (108000,) float64
```

> 若你的网络环境受限导致下载失败，可参考本讲 4.3 以及文档里「手动把文件放进缓存目录」的说明。具体数值（如 ecg 的 dtype 因 ADC 换算为 float）如与本地不一致，以本地实际输出为准，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：`ascent()` 返回的数组 `a`，`a.max()` 应该是多少？为什么？

**参考答案**：`255`。因为 ascent 是 8 位灰度图（uint8），8 位无符号整数最大值是 \(2^8 - 1 = 255\)。文档示例里就写成 `np.uint8(255)`。

**练习 2**：三个函数里，哪一个是「一维信号」而非图像？怎么从返回 shape 判断？

**参考答案**：`electrocardiogram()`。它返回 shape 为 `(108000,)` 的一维数组；而 `ascent` 是 `(512, 512)` 二维、`face` 是 `(768, 1024, 3)` 三维（高×宽×3 通道）。

---

### 4.2 缓存目录是怎么决定的：pooch.os_cache("scipy-data")

#### 4.2.1 概念说明

「下载完的文件存到哪？」这个看似简单的问题，其实涉及操作系统的惯例：不同平台有不同的「标准缓存目录」约定。比如 macOS 习惯放在 `~/Library/Caches`，Linux 遵循 XDG 规范放在 `~/.cache`，Windows 放在 `%LOCALAPPDATA%` 下。

`scipy.datasets` 不自己发明路径，而是把这个决定委托给 Pooch 的 `os_cache()` 函数：传入一个应用名 `"scipy-data"`，Pooch 会在当前操作系统的标准缓存位置下创建一个叫 `scipy-data` 的子目录，作为数据集的缓存根目录。

#### 4.2.2 核心流程

缓存路径的解析流程：

```text
pooch.os_cache("scipy-data")
  └─ 读取操作系统类型 + 相关环境变量（如 Linux 的 XDG_CACHE_HOME）
  └─ 返回该平台标准缓存目录下的 "scipy-data" 子目录绝对路径
```

各平台结果（来自模块文档字符串，是权威来源）：

| 平台 | 缓存目录 |
| --- | --- |
| macOS | `~/Library/Caches/scipy-data` |
| Linux / Unix | `~/.cache/scipy-data`（若设了 `XDG_CACHE_HOME` 环境变量则用它的值） |
| Windows | `C:\Users\<user>\AppData\Local\<AppAuthor>\scipy-data\Cache` |

#### 4.2.3 源码精读

缓存根目录在模块级单例 `data_fetcher` 创建时被设定：

[_fetchers.py:L14-L26](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_fetchers.py#L14-L26)
`pooch.create(...)` 里 `path=pooch.os_cache("scipy-data")` 这一行就是缓存路径的来源；注释解释：Pooch 用 appdirs 在每个平台选合适的缓存目录。同时还配了 `base_url`、`registry`（哈希表）、`urls`（远程地址表）。

各平台缓存路径的权威文字记录在模块文档字符串里：

[__init__.py:L56-L68](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/__init__.py#L56-L68)
这段明确列出 macOS / Linux / Windows 三种平台的缓存路径，并提示「缓存位置随平台而异」。

注意：缓存目录里存的就是原始数据文件本身，文件名和 `registry` 里的键一致（`ascent.dat`、`ecg.dat`、`face.dat`）：

[_registry.py:L8-L12](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_registry.py#L8-L12)
`registry` 字典的键 `ascent.dat / ecg.dat / face.dat` 既是远程文件名，也是落入缓存目录后的本地文件名。

#### 4.2.4 代码实践

**实践目标**：定位本机的 `scipy-data` 缓存绝对路径，并亲眼看到 `ascent.dat` 被写进去。

**操作步骤**：

1. 先确保至少跑过一次 `scipy.datasets.ascent()`（见 4.1.4），让缓存生成。
2. 在 Python 里查询缓存路径（示例代码）：

   ```python
   # 示例代码：打印 pooch 选择的缓存目录
   import pooch
   print(pooch.os_cache("scipy-data"))
   ```

3. 到该目录下列出文件，确认存在 `ascent.dat`：

   ```bash
   ls -l <上面打印的目录>
   ```

**需要观察的现象**：第 2 步打印出一个绝对路径；第 3 步能看到 `ascent.dat`，且文件大小非零。

**预期结果**：
- Linux 上路径形如 `/home/<user>/.cache/scipy-data`，目录下有 `ascent.dat`。
- macOS 上路径形如 `/Users/<user>/Library/Caches/scipy-data`。
- Windows 上路径形如 `C:\Users\<user>\AppData\Local\...\scipy-data\Cache`。

> 不同 Pooch / platformdirs 版本、是否设置 `XDG_CACHE_HOME`，都会影响最终路径。**以本机第 2 步实际打印为准**。具体平台子目录差异待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：在 Linux 上，如果你执行了 `export XDG_CACHE_HOME=/tmp/mycache`，再调用 `ascent()`，`ascent.dat` 会下载到哪？

**参考答案**：会下载到 `/tmp/mycache/scipy-data/ascent.dat`。因为 Pooch 的 `os_cache` 在 Linux 上遵循 XDG 规范，优先使用 `XDG_CACHE_HOME` 环境变量的值作为缓存根，再拼接 `scipy-data` 子目录。

**练习 2**：为什么 `scipy.datasets` 要把缓存路径的选择交给 Pooch，而不是自己写死一个路径？

**参考答案**：因为「正确的缓存位置」是和操作系统强相关的惯例（macOS/Linux/Windows 各不相同，还涉及 XDG 等规范），自己硬编码容易出错且要持续维护多平台逻辑。Pooch（及其底层 appdirs/platformdirs）专门解决「按平台选标准目录」这件事，交给它能保证跨平台行为正确、且随系统规范演进。

---

### 4.3 pooch 可选依赖缺失时的表现

#### 4.3.1 概念说明

`pooch` 是**可选依赖**：你可以正常 `import scipy`、用大部分子模块，唯独 `scipy.datasets` 的「下载」能力需要它。SciPy 采用了一种常见且友好的处理方式——**导入时不报错、用到时才报错**：

- `import scipy.datasets` 永远成功，即使没装 pooch。
- 只有当你真正调用 `ascent()` / `face()` / `electrocardiogram()`（它们内部要下载）时，才会抛出 `ImportError`，并给出「请安装 pooch」的明确提示。

这种「延迟到使用点才报错」的设计，好处是：不安装 pooch 的用户也能 import 整个 scipy，不会被一个用不到的子模块拖累。

#### 4.3.2 核心流程

降级与报错的判定流程：

```text
import _fetchers
  └─ try: import pooch
     ├─ 成功 → data_fetcher = pooch.create(...)   # 正常单例
     └─ 失败(ImportError) → pooch=None; data_fetcher=None   # 降级

调用 fetch_data(name)
  └─ data_fetcher is None?  →  raise ImportError("Missing optional dependency 'pooch' ...")
  └─ 否则正常下载
```

注意：`fetch_data` 有个默认参数 `data_fetcher=data_fetcher`，它会在**函数定义时**就把当时的 `data_fetcher` 绑定进默认值。所以即便后来安装了 pooch，`fetch_data` 用到的仍是定义时的那个（可能是 `None`）。本讲我们只关注「没装 pooch 时报错」这一行为，这个默认参数绑定的细节留到 u2-l1 深入。

#### 4.3.3 源码精读

可选依赖降级的 try/except：

[_fetchers.py:L8-L13](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_fetchers.py#L8-L13)
`try: import pooch`，失败则 `except ImportError: pooch = None; data_fetcher = None`；成功才走 `else` 分支创建 `data_fetcher`。这就是「导入即降级、不立刻报错」的实现。

用到时才抛友好错误的逻辑：

[_fetchers.py:L29-L33](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_fetchers.py#L29-L33)
`fetch_data` 第一件事就是判断 `if data_fetcher is None:`，若是则 `raise ImportError("Missing optional dependency 'pooch' required for scipy.datasets module. Please use pip or conda to install 'pooch'.")`。报错文案直接告诉用户用 pip 或 conda 安装。

#### 4.3.4 代码实践

**实践目标**：亲眼看到「没装 pooch 时的报错长什么样」。

**操作步骤**（二选一）：

- **方式 A（推荐，隔离环境）**：建一个干净的虚拟环境，**不装 pooch**，但装好 numpy 和一个可 import 的 scipy（或直接在能 import scipy 的环境里 `pip uninstall pooch`）：

  ```bash
  python -m venv /tmp/nopooch
  source /tmp/nopooch/bin/activate
  # 安装一个不含 pooch 的 scipy（仅用于观察报错）
  pip install scipy
  pip uninstall -y pooch 2>/dev/null || true
  python -c "import scipy.datasets as d; print(d.ascent().shape)"
  ```

- **方式 B（不修改环境，纯阅读）**：如果你不方便隔离环境，就直接阅读 [_fetchers.py:L29-L33](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_fetchers.py#L29-L33)，并理解：`import scipy.datasets` 本身不会触发任何下载，只有 `d.ascent()` 内部走到 `fetch_data` 才会判定 `data_fetcher is None`。

**需要观察的现象**：方式 A 下，`import scipy.datasets` 成功（不报错），但最后一行 `d.ascent()` 抛出 `ImportError`。

**预期结果**（报错文案）：

```text
ImportError: Missing optional dependency 'pooch' required for scipy.datasets module. Please use pip or conda to install 'pooch'.
```

> 实际是否复现取决于你能否构造出「scipy 可 import 但 pooch 缺失」的环境。若做不到，方式 B 的阅读型实践同样达成理解目标，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 SciPy 选择「`import scipy.datasets` 不报错、调用时才报错」，而不是在 import 阶段就直接报错？

**参考答案**：为了让不需要数据集的用户不被一个用不到的可选依赖阻塞。如果 import 阶段就报错，那么任何 `import scipy`（间接触发子模块加载）的用户都得装 pooch，违背「可选依赖」的初衷。延迟到调用点报错，只在真正需要下载时才要求安装，体验更友好。

**练习 2**：报错类型为什么用 `ImportError` 而不是 `RuntimeError` 或 `ValueError`？

**参考答案**：因为根因是「缺少一个本该 import 的模块」。Python 社区约定：缺失依赖用 `ImportError`（或其子类 `ModuleNotFoundError`），这样用户和工具一看异常类型就知道是「装包」问题，而不是代码逻辑或数据取值错误。报错文案再补充具体的安装命令，进一步降低排错成本。

---

## 5. 综合实践

把本讲三个模块串起来，完成一次「下载 → 定位缓存 → 验证离线命中」的完整链路。

**任务**：验证「第二次调用确实走本地缓存、不再联网」。

**操作步骤**：

1. 准备脚本（示例代码）：

   ```python
   # 示例代码
   import scipy.datasets as d
   import pooch, time

   print("缓存目录:", pooch.os_cache("scipy-data"))

   t0 = time.time()
   img = d.ascent()
   t1 = time.time()
   print("第 1 次调用耗时 %.3fs, shape=%s" % (t1 - t0, img.shape))
   ```

2. **第一次运行**：`python demo.py`。记录耗时 `t1`，并到打印出的缓存目录确认 `ascent.dat` 已生成。
3. **断网**（关闭网络 / 拔网线 / 关 WiFi），再次运行：`python demo.py`。
4. （可选）清空缓存后再断网运行，对比行为：

   ```bash
   rm -rf <缓存目录>     # 或在 Python 里 d.clear_cache([d.ascent])
   # 断网后运行 python demo.py，观察是否会失败
   ```

**需要观察的现象与预期结果**：

| 步骤 | 现象 | 说明 |
| --- | --- | --- |
| 第 1 次运行（联网） | 终端有下载进度，`ascent.dat` 出现在缓存目录 | 首次下载 + SHA256 校验 + 写缓存 |
| 第 2 次运行（断网） | **依然成功返回**，无下载进度，耗时显著变短 | 命中本地缓存，不再联网 |
| 清缓存后断网运行 | 抛出连接错误（下载失败） | 缓存被清、又无法联网，下载这一步无法完成 |

> 第 2 次断网运行「仍然成功」正是缓存机制的核心证据。若你的断网方式不彻底（如走了代理），可能仍能联网，需确保真正离线。各步骤具体耗时以本地为准，**待本地验证**。

## 6. 本讲小结

- 三个数据集函数 `ascent / face / electrocardiogram` 调用方式统一：无参（`face` 可选 `gray`）调用，返回 `numpy.ndarray`；返回前先经 `fetch_data` 把文件下载到本地。
- 完整流程是：**首次联网下载 → 用 `registry` 里的 SHA256 做完整性校验 → 写入本地缓存 → 之后命中缓存不再重复下载**；下载与读取是两步。
- 缓存根目录由 `pooch.os_cache("scipy-data")` 决定，遵循各平台惯例：macOS 为 `~/Library/Caches/scipy-data`、Linux 为 `~/.cache/scipy-data`（受 `XDG_CACHE_HOME` 影响）、Windows 在 `%LOCALAPPDATA%` 下。
- 缓存目录里的文件名就是 `registry` 的键：`ascent.dat / ecg.dat / face.dat`。
- `pooch` 是可选依赖，采用「导入即降级（置 `None`）、调用时才抛 `ImportError`」的友好模式；报错文案会提示用 pip/conda 安装。
- 受限网络环境下，可手动把数据文件放进缓存目录来离线使用。

## 7. 下一步学习建议

本讲我们只把 `fetch_data` 当作「会下载、会缓存」的黑盒用了。下一阶段的讲义会打开这个黑盒：

- **u2-l1（数据获取核心：data_fetcher 与 fetch_data）**：深入 `pooch.create` 的 `path/base_url/registry/urls` 参数、`HTTPDownloader` 与自定义 `User-Agent` 头的来龙去脉，并解释本讲 4.3.2 提到的 `data_fetcher=data_fetcher` 默认参数绑定细节。
- **u2-l2（注册表三件套与 SHA256 校验）**：把本讲一笔带过的 `registry / registry_urls / method_files_map` 三张表讲透，看清 SHA256 是怎么参与校验的。
- **u2-l4（缓存清理机制）**：讲 `clear_cache` 如何按单个 / 多个 / 全部数据集清理本讲生成的缓存文件。

建议你先确保本讲的三个数据集都能在本机跑通、缓存目录能找到，再进入 u2。
