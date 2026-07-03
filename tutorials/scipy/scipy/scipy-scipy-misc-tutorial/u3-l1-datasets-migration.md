# 数据集迁移路径：scipy.misc → scipy.datasets

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 `scipy.datasets` 的「按需下载 + 本地缓存 + SHA256 校验」三件套是怎么协作的；
- 精读 `scipy/datasets/__init__.py`、`_fetchers.py`、`_registry.py` 三个文件，理解 pooch 与 registry 的分工；
- 把旧的 `from scipy.misc import face / ascent / electrocardiogram` 一一迁移到 `scipy.datasets`；
- 用一句话解释：**为什么新方式不再把 `.dat` 文件打进 SciPy 安装包**。

本讲是 `scipy.misc` 退役系列的「去向篇」。前置讲义（u1-l3）已经告诉我们：旧 `scipy.misc` 是个杂物箱，数据集迁去了 `scipy.datasets`。本讲就负责把「迁去之后长什么样、为什么要这样迁」讲透。

## 2. 前置知识

本讲默认你已经掌握以下概念（前几讲已建立）：

- **弃用（deprecation）与 `DeprecationWarning`**：一个 API 被标记「即将删除」但暂时保留，访问时发出警告。`scipy.misc` 当前就是这种状态。
- **桩文件（stub）**：`scipy/misc/__init__.py` 现在只剩一个 `warnings.warn(...)`，见 [scipy/misc/__init__.py:L1-L7](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/misc/__init__.py#L1-L7)，它本身没有任何功能。
- **可选依赖（optional dependency）**：一个库「能用但不是必须」的依赖。`pooch` 对 `scipy.datasets` 就是可选依赖——没装也能 import scipy，只是下不了数据。

本讲会用到的几个新术语，先给直觉：

- **按需下载（lazy / on-demand fetch）**：数据不在你 `pip install` 时下载，而是你**第一次调用** `face()` 时才联网拉取。
- **本地缓存（cache）**：下过的文件存在系统缓存目录里，下次调用直接读本地，不再联网。
- **registry（注册表）**：一张「文件名 → SHA256 哈希」的映射表，用来**校验**下载下来的文件有没有被篡改、有没有损坏。
- **pooch**：一个第三方 Python 库，专门负责「下载 + 缓存 + 哈希校验」这套脏活累活。`scipy.datasets` 把这些全部委托给 pooch。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲角色 |
|------|------|---------|
| `scipy/datasets/__init__.py` | 子模块的公开入口，导出 `face/ascent/electrocardiogram` 及工具函数 | 公开 API 与文档说明 |
| `scipy/datasets/_fetchers.py` | 三个数据集函数的真身 + pooch 抓取器 + `fetch_data` | 迁移后代码的「核心引擎」 |
| `scipy/datasets/_registry.py` | 数据集注册表：SHA256 哈希、下载 URL、方法名映射 | 校验与下载源 |
| `scipy/datasets/_utils.py` | `clear_cache`，清理本地缓存 | 实践时用来重置缓存 |
| `scipy/misc/__init__.py` | 当前的弃用桩文件（对比材料的「现状」端） | 旧入口现状 |
| `scipy/misc/_common.py`（**v1.9.0 历史版本**） | 旧 `face/ascent/electrocardiogram` 的真身 | 对比材料的「过去」端 |

> 说明：最后两行一个是「现在还在的桩」，一个是「用 `git show v1.9.0:scipy/misc/_common.py` 取回的历史源码」。本讲靠这「一旧一新」的逐行对比，把迁移讲清楚。

---

## 4. 核心概念与源码讲解

### 4.1 迁移的核心思想：从「打包进包」到「按需下载」

#### 4.1.1 概念说明

在旧版 SciPy（≤ 1.9）里，`scipy.misc.face()` 的工作方式是：**`.dat` 文件随 SciPy 一起被打进安装包**，调用时直接从包目录读盘。这带来三个问题：

1. **安装包臃肿**：`face.dat`、`ascent.dat`、`ecg.dat` 这些示例数据对绝大多数只想要计算功能的用户毫无用处，却每次 `pip install scipy` 都要下载，白白增加几十 MB。
2. **数据无法独立更新**：想换一张示例图就得发一个新版本的 SciPy。
3. **职责混乱**：这正是一般「杂物箱模块」的通病——计算库和示例数据混在一个包里（详见前置讲义 u1-l3 对「catch-all」的批评）。

迁移到 `scipy.datasets` 后，思路整个反过来：**SciPy 安装包里不再带任何 `.dat`**，数据被放进 GitHub 上独立的 `dataset-<name>` 仓库；用户第一次调用时由 pooch 联网下载，校验后缓存到系统缓存目录，之后都读本地。

#### 4.1.2 核心流程

```
旧（scipy.misc，v1.9）：
    face() ──直接读──> <安装包目录>/scipy/misc/data/face.dat   （随 wheel 发布，必然存在）

新（scipy.datasets，当前 HEAD）：
    face() ──> fetch_data("face.dat")
                   │
                   ├── pooch 在 os_cache("scipy-data") 找缓存 ──命中──> 返回本地路径
                   │
                   └── 未命中 ──> 从 registry_urls 取 URL 联网下载
                                      │
                                      └── 用 registry 的 SHA256 校验 ──> 写入缓存 ──> 返回本地路径
```

#### 4.1.3 源码精读：新旧两端的「数据来源」差异

先看**旧**代码（v1.9.0 历史版本，`scipy/misc/_common.py` 中的 `face`），注意它如何定位 `.dat`：

```python
# v1.9.0 历史代码（示例，非当前 HEAD）
def face(gray=False):
    import bz2, os
    with open(os.path.join(os.path.dirname(__file__), 'face.dat'), 'rb') as f:
        rawdata = f.read()
    data = bz2.decompress(rawdata)
    face = frombuffer(data, dtype='uint8')
    face.shape = (768, 1024, 3)
    ...
```

关键点是 `os.path.join(os.path.dirname(__file__), 'face.dat')`——`__file__` 是 `_common.py` 自身，所以 `face.dat` 就**躺在 `scipy/misc/` 包目录里**，随安装包发布。完整历史文件见 [scipy/misc/_common.py（v1.9.0 标签）](https://github.com/scipy/scipy/blob/v1.9.0/scipy/misc/_common.py)。

再看**新**代码（当前 HEAD，`scipy/datasets/_fetchers.py`），同样的 bz2 + `frombuffer`，唯一换掉的是「`.dat` 从哪来」：

```python
# 当前 HEAD（scipy/datasets/_fetchers.py）
def face(gray=False):
    import bz2
    fname = fetch_data("face.dat")        # ← 这里换成「下载+缓存」
    with open(fname, 'rb') as f:
        rawdata = f.read()
    face_data = bz2.decompress(rawdata)
    face = frombuffer(face_data, dtype='uint8').reshape((768, 1024, 3))
    ...
```

见 [scipy/datasets/_fetchers.py:L216-L221](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/datasets/_fetchers.py#L216-L221)。

> **一句话总结迁移的本质**：解压与组装数组的逻辑（bz2 / pickle / np.load）几乎原样保留，唯一改的是把「读包目录里的 `.dat`」换成「调 `fetch_data(...)` 让 pooch 去下/读缓存」。后续 4.5 会逐函数给出三组对照。

#### 4.1.4 代码实践（源码阅读型）

**目标**：亲手验证「`.dat` 不再打进安装包」。

1. 在本仓库运行 `git show v1.9.0:scipy/misc/_common.py`，找到 `face()`，确认它用 `os.path.dirname(__file__)` 读本地 `face.dat`。
2. 运行 `git show de190e7fde9d3d34400dbfe1eeacc9fc6d29cede:scipy/datasets/_fetchers.py`，找到 `face()`，确认它改用 `fetch_data("face.dat")`。
3. 在你**当前已安装的** SciPy 里，`python -c "import scipy.datasets, os; print(os.path.dirname(scipy.datasets.__file__))"`，列一下这个目录，看看里面有没有任何 `.dat` 文件。

**预期结果**：步骤 1 显示本地读盘；步骤 2 显示联网抓取；步骤 3 的目录里**不应**出现 `face.dat`/`ascent.dat`/`ecg.dat`——它们已经被「赶出」了安装包。如果步骤 3 看到了 `.dat`，说明你装的是很旧版本的 SciPy。**待本地验证**（步骤 3 依赖你的安装环境）。

#### 4.1.5 小练习与答案

**练习 1**：如果把旧 `scipy.misc` 的 `.dat` 文件继续留在安装包里，对「只用到 `scipy.linalg` 解线性方程组、从不碰示例数据」的用户会造成什么具体影响？

> **参考答案**：这位用户每次 `pip install / conda install scipy` 都会多下载几十 MB 的示例数据，磁盘和带宽被白白占用，升级也更慢。这正是按需下载要解决的核心问题。

---

### 4.2 公开入口与文档：scipy/datasets/__init__.py

#### 4.2.1 概念说明

`__init__.py` 是一个 Python 包的「门面」：当你 `import scipy.datasets` 时，解释器执行的就是这个文件。它本身不实现功能，只做两件事——**导出公开 API** 和**用文档字符串讲清这套机制怎么用**。

#### 4.2.2 核心流程

```
import scipy.datasets
   └─ 执行 __init__.py
        ├─ from ._fetchers import face, ascent, electrocardiogram   ← 三个数据集函数
        ├─ from ._download_all import download_all                  ← 批量下载工具
        ├─ from ._utils import clear_cache                          ← 清缓存工具
        └─ 定义 __all__ = ['ascent','electrocardiogram','face','download_all','clear_cache']
```

#### 4.2.3 源码精读

公开 API 的导出与 `__all__` 见 [scipy/datasets/__init__.py:L80-L85](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/datasets/__init__.py#L80-L85)：

```python
from ._fetchers import face, ascent, electrocardiogram
from ._download_all import download_all
from ._utils import clear_cache

__all__ = ['ascent', 'electrocardiogram', 'face',
           'download_all', 'clear_cache']
```

也就是说，用户层面只用记五个名字：三个数据集（`ascent`/`face`/`electrocardiogram`）加两个工具（`download_all` 批量预下载、`clear_cache` 清缓存）。

真正讲清「这套机制怎么运作」的是文件顶部的文档字符串，见 [scipy/datasets/__init__.py:L40-L54](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/datasets/__init__.py#L40-L54)。它说明了三件事：

1. 数据集文件托管在 GitHub 的 `scipy/dataset-<name>` 仓库里（例如 `face` 在 `https://github.com/scipy/dataset-face`）；
2. `scipy.datasets` 依赖 **pooch** 来抓取；
3. 维护着一张「文件名 → SHA256 + repo URL」的 registry，pooch 用它来校验下载；下完一次后，文件存到系统缓存目录 `scipy-data` 下。

缓存目录随平台不同，见 [scipy/datasets/__init__.py:L56-L68](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/datasets/__init__.py#L56-L68)：

| 平台 | 缓存目录 |
|------|---------|
| macOS | `~/Library/Caches/scipy-data` |
| Linux / Unix | `~/.cache/scipy-data`（或 `XDG_CACHE_HOME` 的值） |
| Windows | `C:\Users\<user>\AppData\Local\<AppAuthor>\scipy-data\Cache` |

> **术语解释**：`__all__` 是 Python 的约定，列出「`from package import *` 时会导出的名字」。它既是给用户的公开契约，也常被静态分析工具用来判断哪些是「公开 API」。

#### 4.2.4 代码实践

**目标**：验证 `scipy.datasets` 的公开 API 与文档字符串。

1. 运行 `python -c "import scipy.datasets as d; print(sorted(d.__all__))"`，对照上面的 `__all__` 列表，确认五个名字都在。
2. 运行 `python -c "import scipy.datasets as d; print(d.face.__module__)"`，应输出 `scipy.datasets._fetchers`，说明 `face` 的真身在 `_fetchers.py`。
3. 运行 `python -c "import scipy.datasets as d; print(d.__doc__[:200])"`，读一读文档字符串开头。

**预期结果**：`__all__` 含五个名字；`face.__module__` 指向 `scipy.datasets._fetchers`；文档字符串开头正是本节引用的那段「How dataset retrieval and storage works」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `scipy.datasets` 要把 `face/ascent/electrocardiogram` 的实现写在 `_fetchers.py`（带下划线前缀）里，而 `__init__.py` 只负责 `import` 进来？

> **参考答案**：下划线前缀表示「私有模块」，是约定俗成的「内部实现」标记。把实现与「门面」分离，让 `__init__.py` 保持清爽、只暴露公开 API；用户也不该直接 `import scipy.datasets._fetchers`，而应从 `scipy.datasets` 顶层取用。这种「门面 + 内部模块」分层在大型库里很常见。

**练习 2**：`scipy.datasets.__all__` 里**没有** `fetch_data`，但 `fetch_data` 其实是整个下载机制的入口。这说明什么？

> **参考答案**：`fetch_data` 是**内部辅助函数**（在 `_fetchers.py` 里定义），不属于公开 API，所以不进 `__all__`。用户只需调 `face()` 这类高层数据集函数，下载细节被封装掉了。这也意味着 `fetch_data` 的签名将来可以自由改动，不受公开 API 稳定性承诺约束。

---

### 4.3 按需下载引擎：scipy/datasets/_fetchers.py

#### 4.3.1 概念说明

`_fetchers.py` 是整个迁移的「心脏」。它做三件事：

1. 在 **import 时**创建一个 pooch 抓取器 `data_fetcher`（配置好缓存目录、registry、下载 URL）；
2. 提供 **`fetch_data(dataset_name)`**：命中缓存就返回本地路径，否则下载 + 校验 + 缓存；
3. 定义三个数据集函数 `ascent`/`electrocardiogram`/`face`，每个都先 `fetch_data` 拿到本地文件路径，再用 numpy 读成数组。

注意 pooch 是**可选依赖**：没装时 `import pooch` 会失败，代码会优雅地把 `data_fetcher` 设为 `None`，等到真正调用 `fetch_data` 时才抛 `ImportError`，而不是在 `import scipy.datasets` 时就崩。

#### 4.3.2 核心流程

```
import scipy.datasets._fetchers
   ├─ try: import pooch                          ← 可选依赖
   │    else: data_fetcher = pooch.create(       ← 构造抓取器（import 时一次性建好）
   │              path = pooch.os_cache("scipy-data"),   ← 缓存目录
   │              registry = registry,                    ← SHA256 校验表
   │              urls = registry_urls)                   ← 每个文件的下载 URL
   │    except ImportError: data_fetcher = None
   │
   └─ def fetch_data(dataset_name, data_fetcher=data_fetcher):
          if data_fetcher is None: raise ImportError("缺少 pooch")   ← 惰性报错
          downloader = pooch.HTTPDownloader(headers={"User-Agent": ...})
          return data_fetcher.fetch(dataset_name, downloader=downloader)
                                              ↑ pooch 在这里完成「查缓存 / 下载 / 校验」
```

#### 4.3.3 源码精读

**① 可选依赖与抓取器创建**，见 [scipy/datasets/_fetchers.py:L8-L26](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/datasets/_fetchers.py#L8-L26)：

```python
try:
    import pooch
except ImportError:
    pooch = None
    data_fetcher = None
else:
    data_fetcher = pooch.create(
        path=pooch.os_cache("scipy-data"),   # 各 OS 的默认缓存目录
        base_url="https://github.com/scipy/",
        registry=registry,                    # 来自 _registry.py
        urls=registry_urls                    # 来自 _registry.py
    )
```

注意 `try/except/else`：`else` 分支在「成功 import」时才执行，负责构造抓取器。`pooch.os_cache("scipy-data")` 就是上节那张缓存目录表里路径的来源——pooch 帮你按 OS 选好位置。

**② `fetch_data` 入口**，见 [scipy/datasets/_fetchers.py:L29-L39](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/datasets/_fetchers.py#L29-L39)：

```python
def fetch_data(dataset_name, data_fetcher=data_fetcher):
    if data_fetcher is None:
        raise ImportError("Missing optional dependency 'pooch' ...")
    # https://github.com/scipy/scipy/issues/21879
    downloader = pooch.HTTPDownloader(
        headers={"User-Agent": f"SciPy {sys.modules['scipy'].__version__}"}
    )
    return data_fetcher.fetch(dataset_name, downloader=downloader)
```

两个细节值得注意：

- **惰性报错**：`data_fetcher` 作为默认参数绑定到函数对象上（`def fetch_data(..., data_fetcher=data_fetcher)`），pooch 缺失时它是 `None`，但只有**真正调用** `fetch_data` 时才 `raise ImportError`。这样 `import scipy.datasets` 永远成功。
- **自定义 User-Agent 头**：注释指向 issue #21879——某些网络环境会拦截「无名」请求，所以 pooch 下载时统一带上 `User-Agent: SciPy <版本>`。这是个很真实的工程细节。

`data_fetcher.fetch(...)` 是 pooch 的核心方法：查缓存命中就直接返回路径；否则按 `urls` 下载、按 `registry` 的 SHA256 校验、写入缓存、再返回路径。

**③ 三个数据集函数**（节选关键行）。`ascent()` 见 [scipy/datasets/_fetchers.py:L76-L80](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/datasets/_fetchers.py#L76-L80)；`electrocardiogram()` 见 [scipy/datasets/_fetchers.py:L175-L180](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/datasets/_fetchers.py#L175-L180)；`face()` 见 [scipy/datasets/_fetchers.py:L216-L225](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/datasets/_fetchers.py#L216-L225)。

```python
def ascent():                       # 其它两个结构一致
    import pickle
    fname = fetch_data("ascent.dat")        # 拿到本地路径
    with open(fname, 'rb') as f:
        ascent = array(pickle.load(f))      # 用 pickle 反序列化成 numpy 数组
    return ascent
```

每个函数都是「`fetch_data` 拿路径 → 用 numpy/pickle/bz2 读数组」这个固定套路，区别只在读取格式（`ascent` 用 pickle、`face` 用 bz2、`ecg` 用 `np.load`）。

> **关于 `@xp_capabilities(out_of_scope=True)`**：三个函数上面都有这个装饰器，它是 SciPy 数组 API 标注的一部分，标记「这些函数不在 array API 标准的支持范围内」。本讲不用深究，知道它是个标注、不影响返回值即可。

#### 4.3.4 代码实践

**目标**：观察「按需下载 + 缓存」的懒加载行为。

1. `pip install pooch`（若未装）。
2. 运行：
   ```python
   import time, scipy.datasets
   t0 = time.time(); face = scipy.datasets.face(); t1 = time.time()
   print("首次调用耗时：", t1 - t0, "秒")
   t2 = time.time(); face2 = scipy.datasets.face(); t3 = time.time()
   print("第二次调用耗时：", t3 - t2, "秒")
   ```
3. 观察两次耗时差异，并打印 `scipy.datasets.face.__wrapped__` 是否存在（探索装饰器细节，可选）。

**需要观察的现象**：第一次调用明显慢（要联网下载 `face.dat` 并校验）；第二次极快（直接读缓存）。

**预期结果**：首次耗时通常远大于第二次。如果两次都慢，可能是缓存目录不可写，pooch 每次都重新下载。**待本地验证**（取决于网络）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `fetch_data` 的 `data_fetcher` 要写成**默认参数**（`def fetch_data(dataset_name, data_fetcher=data_fetcher)`），而不是直接在函数体里用模块级 `data_fetcher`？

> **参考答案**：写成默认参数会在**函数定义时**把当前 `data_fetcher` 绑定到函数对象上。这是 SciPy 的小优化/约定：避免每次调用都做全局名查找，也让「抓取器在 import 时建好一次」的事实更清晰。即便不这么写，行为也基本等价（模块级 `data_fetcher` 同样在 import 时定型）。

**练习 2**：如果用户没装 pooch，`import scipy.datasets` 会报错吗？`scipy.datasets.face()` 呢？

> **参考答案**：`import scipy.datasets` **不会**报错——`try/except ImportError` 把缺失吞掉，`data_fetcher=None`。但 `scipy.datasets.face()` **会**在内部调用 `fetch_data` 时抛 `ImportError("Missing optional dependency 'pooch' ...")`。这正是「惰性报错」的好处：不碰数据集就完全不受影响。

---

### 4.4 校验与下载源：scipy/datasets/_registry.py

#### 4.4.1 概念说明

`_registry.py` 是一张纯数据表，没有任何逻辑代码，但它承担两件要紧事：

1. **完整性校验**：`registry` 给每个文件一个 SHA256 哈希。pooch 下完文件后会重算哈希、和这张表比对——任何传输损坏或被中间人篡改都会被揪出来。
2. **下载源定位**：`registry_urls` 告诉 pooch「每个文件去哪个 URL 下」。这把「数据」和「SciPy 代码」彻底解耦——换数据只需改这张表，不必动 SciPy 主版本。

还有一个 `method_files_map`，给 `clear_cache` 用，把「数据集方法名」映射到「它依赖的文件名」。

#### 4.4.2 核心流程

```
_fetchers.py 创建抓取器时传入：
    registry    = { "face.dat": "<sha256>", ... }    ← 校验用
    urls        = registry_urls = { "face.dat": "https://.../face.dat", ... }  ← 下载用

pooch.fetch("face.dat") 内部：
    1. 查 path(os_cache) 下有没有 face.dat 且 sha256 匹配 registry ── 命中 ──> 返回路径
    2. 未命中 ──> 从 urls["face.dat"] 下载 ──> 算 sha256 比对 registry["face.dat"]
                                                       ├─ 匹配 ──> 存缓存，返回路径
                                                       └─ 不匹配 ──> 抛错（校验失败）

clear_cache([scipy.datasets.face]) 内部：
    查 method_files_map["face"] = ["face.dat"] ──> 到缓存目录删掉 face.dat
```

#### 4.4.3 源码精读

**① 校验表 `registry`**，见 [scipy/datasets/_registry.py:L8-L12](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/datasets/_registry.py#L8-L12)：

```python
registry = {
    "ascent.dat": "03ce124c1afc880f87b55f6b061110e2e1e939679184f5614e38dacc6c1957e2",
    "ecg.dat":    "f20ad3365fb9b7f845d0e5c48b6fe67081377ee466c3a220b7f69f35c8958baf",
    "face.dat":   "9d8b0b4d081313e2b485748c770472e5a95ed1738146883d84c7030493e82886"
}
```

文件顶部注释 `# To generate the SHA256 hash, use the command openssl sha256 <filename>` 告诉维护者这些哈希是怎么算出来的。

**② 下载源 `registry_urls`**，见 [scipy/datasets/_registry.py:L14-L18](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/datasets/_registry.py#L14-L18)：

```python
registry_urls = {
    "ascent.dat": "https://raw.githubusercontent.com/scipy/dataset-ascent/main/ascent.dat",
    "ecg.dat":    "https://raw.githubusercontent.com/scipy/dataset-ecg/main/ecg.dat",
    "face.dat":   "https://raw.githubusercontent.com/scipy/dataset-face/main/face.dat"
}
```

注意 URL 的命名规律：`scipy/dataset-<name>`，每个数据集一个独立 GitHub 仓库——这正是 `__init__.py` 文档字符串里说的「stored within individual GitHub repositories」。

**③ 方法名映射 `method_files_map`**，见 [scipy/datasets/_registry.py:L22-L26](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/datasets/_registry.py#L22-L26)：

```python
method_files_map = {
    "ascent": ["ascent.dat"],
    "electrocardiogram": ["ecg.dat"],
    "face": ["face.dat"]
}
```

这张表被 `_utils.py` 的 `clear_cache` 用到：见 [scipy/datasets/_utils.py:L13-L58](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/datasets/_utils.py#L13-L58)，它通过 `dataset.__name__`（如 `"face"`）查出要删的文件名，再去缓存目录删。

> **术语解释**：**SHA256** 是一种密码学哈希函数，输入任意文件都能输出一段固定长度（64 个十六进制字符）的「指纹」。文件哪怕只改一个比特，指纹都会彻底不同。所以用「下载后重算的指纹」对比「registry 里登记的指纹」就能判断文件是否完整、是否被篡改。

#### 4.4.4 代码实践

**目标**：亲手验证 SHA256 校验，并体验 `clear_cache`。

1. 先调一次 `scipy.datasets.face()` 触发下载缓存。
2. 在缓存目录（Linux 下 `~/.cache/scipy-data/face.dat`）对下载好的文件算哈希：
   ```bash
   openssl sha256 ~/.cache/scipy-data/face.dat
   ```
3. 把输出的哈希和 `registry["face.dat"]`（即 `9d8b0b4d...82886`）比对，应**完全一致**。
4. 用 `scipy.datasets.clear_cache([scipy.datasets.face])` 清掉它，再 `ls ~/.cache/scipy-data/`，确认 `face.dat` 没了。

**预期结果**：步骤 3 两个哈希相同，证明 pooch 下载的文件与 registry 登记一致；步骤 4 清完后文件消失。**待本地验证**（缓存路径随 OS 变化）。

#### 4.4.5 小练习与答案

**练习 1**：假设维护者更新了 `face.dat` 的图片，但**忘了同时更新** `registry["face.dat"]` 的哈希。用户调用 `scipy.datasets.face()` 会发生什么？

> **参考答案**：pooch 会下载到**新**文件，但用**旧**哈希去校验——两者不匹配，pooch 抛出校验失败错误，用户拿不到数据。这说明 registry 的哈希必须和数据文件严格同步更新，是维护这套机制最容易踩的坑。

**练习 2**：`method_files_map` 里每个方法目前只对应一个文件（如 `"face": ["face.dat"]`）。为什么值要设计成**列表**而不是单个字符串？

> **参考答案**：为了前瞻性地支持「一个数据集方法依赖多个文件」的情况（比如将来某个数据集要同时下载数据文件 + 元数据文件）。设计成列表，未来扩展时不必改 `clear_cache` 的逻辑。

---

### 4.5 逐函数迁移对比：face / ascent / electrocardiogram

这一节把三组「旧 vs 新」放在一起，作为动手迁移的速查表。三者的共同点都是：**只把「定位 `.dat` 的那一行」从本地路径换成 `fetch_data(...)`，其余读取逻辑不变。**

| 函数 | 旧（v1.9 `scipy.misc`）数据来源 | 新（`scipy.datasets`）数据来源 | 读取格式 |
|------|--------------------------------|-------------------------------|---------|
| `face()` | `os.path.join(os.path.dirname(__file__), 'face.dat')` | `fetch_data("face.dat")` | bz2 解压 + `frombuffer` |
| `ascent()` | `os.path.join(os.path.dirname(__file__),'ascent.dat')` | `fetch_data("ascent.dat")` | `pickle.load` |
| `electrocardiogram()` | `os.path.join(os.path.dirname(__file__), "ecg.dat")` | `fetch_data("ecg.dat")` | `np.load` |

以 `electrocardiogram` 为例（新代码，[scipy/datasets/_fetchers.py:L175-L180](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/datasets/_fetchers.py#L175-L180)）：

```python
fname = fetch_data("ecg.dat")              # 新：下载/缓存
with load(fname) as file:
    ecg = file["ecg"].astype(int)
ecg = (ecg - 1024) / 200.0                 # ADC 原始值转毫伏
return ecg
```

旧 v1.9 版本里，唯一不同的就是把第一行换成 `file_path = os.path.join(os.path.dirname(__file__), "ecg.dat")`，后面 `np.load` 与毫伏换算公式 \( \text{ecg}_{\text{mV}} = (\text{raw} - 1024)/200 \) 完全一致。

> **迁移结论**：对**数据集类**函数（`face`/`ascent`/`electrocardiogram`），迁移就是把 `from scipy.misc import face` 改成 `from scipy.datasets import face`，调用方式（参数、返回值）**零改动**。这要感谢迁移时刻意保留了函数签名。注意：`derivative`/`central_diff_weights` **没有**这种平滑对应物，它们被彻底删除（见下一讲 u3-l3 综合实践）。

---

## 5. 综合实践

**任务**：写一段脚本，模拟「把旧的 scipy.misc 数据集用法迁移到 scipy.datasets」的完整流程，并验证缓存机制。

```python
# migrate_datasets_demo.py（示例代码）
import warnings, os
import scipy.datasets

# (1) 屏蔽残留的 scipy.misc 弃用警告，防止旧依赖污染输出
warnings.simplefilter("ignore", DeprecationWarning)

# (2) 用新 API 取代旧的 scipy.misc.face / electrocardiogram
face = scipy.datasets.face()                  # 旧：scipy.misc.face()
ecg  = scipy.datasets.electrocardiogram()     # 旧：scipy.misc.electrocardiogram()
print("face:", face.shape, face.dtype)        # 期望 (768, 1024, 3) uint8
print("ecg :", ecg.shape, ecg.dtype)          # 期望 (108000,) float64

# (3) 定位缓存目录，确认 .dat 不在安装包而在缓存里
from scipy.datasets._utils import _clear_cache
import platformdirs
cache_dir = platformdirs.user_cache_dir("scipy-data")
print("缓存目录：", cache_dir)
print("缓存内文件：", os.listdir(cache_dir))

# (4) 清缓存后再次调用，观察「重新下载」的耗时变化
import time
scipy.datasets.clear_cache([scipy.datasets.face, scipy.datasets.electrocardiogram])
t0 = time.time(); scipy.datasets.face(); print("清缓存后首次调用耗时：", time.time() - t0, "秒")
```

**操作步骤与预期**：

1. 确保已 `pip install scipy pooch platformdirs`。
2. 运行脚本。第一段应打印 `face: (768, 1024, 3) uint8` 与 `ecg: (108000,) float64`。
3. 缓存目录应类似 `~/.cache/scipy-data`（Linux）或 `~/Library/Caches/scipy-data`（macOS），里面能看到 `face.dat`、`ecg.dat`、`ascent.dat` 等。
4. 清缓存后再次调用会重新下载，耗时显著高于命中缓存的情况。

**思考题（写进你的笔记）**：步骤 3 列出的 `.dat` 文件位于**系统缓存目录**，而不是 `scipy` 安装目录。结合本讲内容，用两三句话解释：为什么 SciPy 团队宁愿让用户「首次调用时联网」，也不愿把这些 `.dat` 重新打进安装包？

> **参考要点**：① 计算库与示例数据解耦，避免绝大多数不碰数据的用户为示例数据付带宽与磁盘代价；② 数据可独立于 SciPy 版本更新（只动 GitHub 上的 `dataset-<name>` 仓库与 registry 哈希）；③ 让 `scipy.misc` 这个杂物箱彻底瘦身，是「职责清晰」架构取向的一部分。

> **待本地验证**：脚本依赖网络与可选依赖 pooch/platformdirs；在无网环境会失败——此时可手动把 `dataset-<name>` 仓库内容放进缓存目录离线使用（见 `scipy/datasets/__init__.py` 文档字符串 L71-L75 的说明）。

---

## 6. 本讲小结

- **迁移的本质**：`face`/`ascent`/`electrocardiogram` 的数据读取逻辑（bz2/pickle/np.load）几乎原样保留，唯一换掉的是「`.dat` 从哪来」——从「打包进 `scipy/misc/` 包目录」变成「`fetch_data(...)` 经 pooch 按需下载 + 缓存」。
- **公开入口** `scipy/datasets/__init__.py` 只导出五个名字（三个数据集 + `download_all` + `clear_cache`），并用文档字符串讲清 pooch / registry / 缓存目录的运作。
- **下载引擎** `_fetchers.py`：import 时建好 pooch 抓取器（`os_cache` + registry + urls），pooch 作为可选依赖实现「惰性报错」；`fetch_data` 还带上了 `User-Agent` 头（issue #21879）应对网络拦截。
- **校验与下载源** `_registry.py` 是纯数据表：`registry`（SHA256 校验）、`registry_urls`（每文件一个 `dataset-<name>` 仓库 URL）、`method_files_map`（供 `clear_cache` 用）。
- **为什么不再打包 `.dat`**：让计算库与示例数据解耦，给「不碰数据的用户」减负，并允许数据独立于 SciPy 版本更新。
- **迁移成本**：数据集类函数零改动平移（只换 import 来源）；但 `derivative`/`central_diff_weights` 没有对应物，属下一讲的范畴。

---

## 7. 下一步学习建议

- **下一讲（u3-l2）** 会回到 `scipy/misc/__init__.py` 的 git 历史，讲它曾经如何用 **PEP 562 模块级 `__getattr__`** 对 `face`/`ascent`/`derivative` 等名字做「按名字分流弃用提示」的访问控制——这正是把数据集访问「重定向」到 `scipy.datasets` 的旧机制，与本讲配套阅读会非常通透。
- **综合实践（u3-l3）** 会把数据集迁移（本讲）与 `derivative` 的替代（手写有限差分或外部库 findiff/numdifftools）合在一起，完成一段旧脚本的完整迁移，并用 `python -W error` 在 CI 里兜底。
- 继续阅读的真实源码：`scipy/datasets/_download_all.py`（批量预下载，离线场景很有用）和 `scipy/datasets/_utils.py` 的 `clear_cache` 实现，巩固本讲对缓存的理解。
