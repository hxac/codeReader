# 数据获取核心：data_fetcher 与 fetch_data

## 1. 本讲目标

在前两讲里，我们已经把 `scipy.datasets` 的目录结构、公开 API、以及「首次下载 → SHA256 校验 → 缓存命中」的整体链路梳理清楚了。但那时的 `fetch_data` 还是一个黑盒：数据到底从哪来、用什么协议下、下载时带了什么请求头、下载完返回什么？

本讲就打开这个黑盒。读完本讲，你应当能够：

1. 看懂模块级单例 `data_fetcher` 是如何用 `pooch.create(...)` 一次性配置好缓存路径、`base_url`、`registry`、`urls` 四个关键参数的。
2. 理解 `fetch_data(dataset_name)` 这个「薄助手」如何把「检查 pooch 是否安装 → 构造下载器 → 调用 `fetch` → 返回本地文件路径」串起来。
3. 说清楚 `pooch.HTTPDownloader` 与自定义 `User-Agent` 请求头的作用，以及为什么 SciPy 要在请求头里带上自己的版本号（对应代码里引用的 GitHub issue #21879）。

## 2. 前置知识

在进入源码前，先用通俗语言过一遍本讲涉及的几个外部概念。

- **Pooch**：一个第三方 Python 库，专门用来「下载 + 缓存 + 校验」科研数据文件。`scipy.datasets` 把联网、缓存、哈希校验这些脏活全部外包给它，自己只关心「拿到文件后怎么解析」。
- **单例（singleton）**：指「整个模块只在导入时创建一次、之后全局复用」的对象。`data_fetcher` 就是一个单例——配置一次，到处使用，避免每次调用数据集函数都重新构造。
- **`pooch.create(...)` 的产物**：它返回一个 `pooch.Pooch` 实例。可以把它理解为一个「带有缓存目录、远程地址簿、哈希登记表的下载管家」。
- **registry / registry_urls**：两张字典。`registry` 是「文件名 → SHA256 哈希」，用来校验下载是否完整；`registry_urls` 是「文件名 → 完整远程 URL」，用来决定从哪下载。
- **`User-Agent` 请求头**：HTTP 请求中的一个字段，用来告诉服务器「我是谁、用什么客户端」。Python 自带的 `urllib` 默认会带上类似 `Python-urllib/3.x` 的 User-Agent，而某些服务器会拒绝这种默认标识的请求。
- **`HTTPDownloader`**：Pooch 提供的「HTTP 下载器」，封装了「发请求 → 收字节流 → 写文件」的细节，并允许你自定义请求头、超时、进度条等。
- **可选依赖的降级**：pooch 不是 SciPy 的必装依赖。如果用户没装 pooch，`import scipy.datasets` 不应报错；只有真正调用数据集函数时才提示安装。这套模式在上一讲的 `u1-l3` 里你已经见过它的「症状」，本讲会看到它的「源头」。

## 3. 本讲源码地图

本讲只涉及两个文件，但它们正好覆盖了「配置」与「执行」两层：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [_fetchers.py](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_fetchers.py) | 获取层：构造 `data_fetcher` 单例、提供 `fetch_data` 助手、实现三个数据集函数 | `pooch.create` 配置、`fetch_data` 流程、`HTTPDownloader` 与 User-Agent |
| [_registry.py](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_registry.py) | 注册表层：三张映射表，被 `_fetchers.py` 依赖 | `registry`（哈希）、`registry_urls`（URL）如何喂给 `pooch.create` |

> 小提示：`_registry.py` 是「地基」——它不 import 任何人；`_fetchers.py` 是「住户」——它从地基里取 `registry` 和 `registry_urls`。这种「被依赖者不反向依赖」的单向关系，是本子模块能保持清晰的关键。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：① 模块级 `data_fetcher` 单例的创建；② `fetch_data(dataset_name)` 助手；③ `pooch.HTTPDownloader` 与 User-Agent 头。

### 4.1 模块级 data_fetcher 单例的创建

#### 4.1.1 概念说明

`data_fetcher` 是一个「下载管家」单例。为什么需要单例？因为缓存目录、远程地址簿、哈希登记表这些配置在整个程序运行期间都是固定的，没必要每调用一次 `ascent()` 就重建一个下载管家。所以在 `_fetchers.py` 被 import 的那一刻，配置就一次性完成，之后所有数据集函数共用这同一个对象。

这个对象同时还承担了「可选依赖降级」的职责：如果用户没装 pooch，单例就退化为 `None`，而不是在 import 阶段直接炸掉整个 `scipy.datasets`。

#### 4.1.2 核心流程

`data_fetcher` 的创建流程可以概括为：

```text
导入 _fetchers.py
   │
   ├─ try: import pooch
   │     │
   │     └─ 成功 → data_fetcher = pooch.create(
   │                    path=缓存目录,
   │                    base_url="https://github.com/scipy/",
   │                    registry=registry,        # 来自 _registry.py
   │                    urls=registry_urls)       # 来自 _registry.py
   │
   └─ except ImportError:
         pooch = None
         data_fetcher = None      # 优雅降级，import 不报错
```

`pooch.create` 的四个参数含义：

- `path`：本地缓存目录。`pooch.os_cache("scipy-data")` 会按操作系统惯例选一个目录（macOS 在 `~/Library/Caches`、Linux 在 `~/.cache`、Windows 在 `%LOCALAPPDATA%` 下），这一点上一讲 `u1-l3` 已经讲过。
- `base_url`：远程地址的「前缀」。它是必填参数，但代码注释明确说明：**每个文件的真实地址其实由 `urls`（即 `registry_urls`）单独覆盖**，所以这里的 `base_url` 实际上没怎么派上用场，只是 pooch 的强制要求。
- `registry`：文件名 → SHA256 的字典，pooch 用它做下载完整性校验。
- `urls`：文件名 → 完整 URL 的字典，pooch 用它定位每个文件的真实下载地址（覆盖 `base_url`）。

#### 4.1.3 源码精读

先看降级与单例创建的整体（含 `try/except`）：

[_fetchers.py:8-26 — 可选依赖降级与 data_fetcher 单例创建](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_fetchers.py#L8-L26)

这段代码做了两件事：① `try/except ImportError` 把 pooch 变成可选依赖，缺失时 `pooch` 与 `data_fetcher` 都置为 `None`；② pooch 存在时，用 `pooch.create(...)` 一次性配置好下载管家。

再单独聚焦 `pooch.create` 的四参数配置：

[_fetchers.py:14-26 — pooch.create 的 path/base_url/registry/urls 四参数](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_fetchers.py#L14-L26)

注意第 20-23 行的注释：「`base_url` 是必填参数，尽管我们用 registry 里的逐文件 `urls` 覆盖了它」——这是理解本段的关键：**真正决定下载地址的是 `urls=registry_urls`，`base_url` 只是个占位**。

喂给 `pooch.create` 的两张表来自 `_registry.py`：

[_registry.py:8-12 — registry：文件名 → SHA256 哈希](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_registry.py#L8-L12)

这是 pooch 用来做完整性校验的「指纹表」。文件顶部的注释还贴心地告诉你哈希怎么算：`openssl sha256 <filename>`。

[_registry.py:14-18 — registry_urls：文件名 → 完整远程 URL](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_registry.py#L14-L18)

注意 URL 的规律：每个数据集都存放在一个独立仓库 `scipy/dataset-<name>` 的 `main` 分支下，文件名与仓库内的文件名一致。这正是 `__init__.py` 模块文档里描述的 `dataset-<name>` 命名约定。

#### 4.1.4 代码实践

**实践目标**：亲手观察 `data_fetcher` 这个单例的内部结构，验证它确实「带着」缓存目录、registry、urls 三样东西。

**操作步骤**（已安装 pooch 的环境下）：

1. 在 Python 里 `import scipy.datasets._fetchers as F`。
2. 打印 `F.data_fetcher`，观察它是一个 `pooch.Pooch` 对象。
3. 分别查看 `F.data_fetcher.path`（缓存目录）、`F.data_fetcher.registry`（哈希表）、`F.data_fetcher.urls`（URL 表）。
4. 对照 `_registry.py`，确认 `registry` / `urls` 与源码里的两张字典完全一致。

**需要观察的现象**：`data_fetcher.path` 应指向一个以 `scipy-data` 结尾的本地目录；`registry` 是一个包含 `ascent.dat / ecg.dat / face.dat` 三个键、值为 SHA256 字符串的字典。

**预期结果**：三个数据集的文件名在 `registry` 与 `urls` 里都能找到，且 `registry` 里的哈希值与 `_registry.py` 源码逐字相同。

> 如果当前环境未装 pooch，`F.data_fetcher` 会是 `None`，这一步请标注「待本地验证（需先安装 pooch）」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `data_fetcher` 要在模块导入时（`try/except` 的 `else` 分支里）就创建，而不是放进某个函数里按需创建？

**参考答案**：因为这些配置是全局固定不变的（缓存目录、地址簿、哈希表都不会变），在导入时创建一次、全局复用，既避免重复构造的开销，也让「pooch 是否可用」这个状态在导入时就确定下来，便于后续函数用 `data_fetcher is None` 做降级判断。

**练习 2**：`pooch.create` 既接收 `base_url` 又接收 `urls`，二者哪个真正决定了每个文件的下载地址？

**参考答案**：`urls`（即 `registry_urls`）。`base_url` 是 pooch 的必填参数，但代码注释明确说明逐文件的 `urls` 会覆盖它；实际下载时 pooch 优先使用 `urls` 里该文件名对应的完整 URL。

---

### 4.2 fetch_data(dataset_name) 助手

#### 4.2.1 概念说明

三个数据集函数 `ascent / electrocardiogram / face` 都需要做同一件事：**把数据文件下载到本地（或命中缓存），拿到本地文件路径**。这部分逻辑完全相同，于是被抽成一个公共助手 `fetch_data(dataset_name)`。它是一个「薄薄的封装」：自身不做解析，只负责「拿到文件路径」，真正的解析（pickle / npz / bz2）留给各自的数据集函数（那是下一讲 `u2-l3` 的内容）。

`fetch_data` 还有两个值得注意的设计：

1. **降级检查**：进入函数第一件事就是判断 `data_fetcher is None`，是则抛出友好的 `ImportError`，提示用 pip/conda 安装 pooch。这正是上一讲 `u1-l3` 里「调用时报错、导入时不报错」的源头。
2. **可注入的 `data_fetcher` 默认参数**：函数签名是 `def fetch_data(dataset_name, data_fetcher=data_fetcher)`，把模块级单例绑成默认值。这相当于留了一个「口子」——测试时可以传一个自定义的 `data_fetcher`，从而在离线、隔离环境下做测试（这一点在 `u3-l3` 测试剖析里会用到）。

#### 4.2.2 核心流程

`fetch_data` 的执行流程：

```text
fetch_data(dataset_name)
   │
   ├─ if data_fetcher is None:
   │      raise ImportError("Missing optional dependency 'pooch' ...")
   │
   ├─ 构造 downloader = pooch.HTTPDownloader(
   │        headers={"User-Agent": f"SciPy {scipy.__version__}"})
   │
   └─ return data_fetcher.fetch(dataset_name, downloader=downloader)
          │
          └─ pooch 内部：
               缓存命中且哈希一致 → 直接返回本地路径
               否则 → 用 downloader 联网下载 → 校验 SHA256 → 写入缓存 → 返回路径
```

`pooch.Pooch.fetch(filename, downloader=...)` 的关键行为：它返回的是**下载到本地的文件完整路径**（一个字符串），而不是文件内容。所以数据集函数拿到这个路径后，还要自己用 `open` / `pickle.load` / `np.load` 去读取——这就是为什么 `ascent()` 里有 `fname = fetch_data("ascent.dat")` 紧跟着 `open(fname, 'rb')`。

关于默认参数的一个 Python 细节：默认参数在**函数定义时**求值一次。所以 `data_fetcher=data_fetcher` 把「定义那一刻的模块级单例」固化进了默认值。如果导入时 pooch 缺失，这个默认值就是 `None`，于是函数体里的 `is None` 检查正好兜住，抛出 `ImportError`。

#### 4.2.3 源码精读

`fetch_data` 的完整实现：

[_fetchers.py:29-39 — fetch_data 助手：降级检查 + 构造下载器 + 调用 fetch](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_fetchers.py#L29-L39)

逐行解读：

- 第 30-33 行：降级检查。`data_fetcher is None` 时抛 `ImportError`，文案直接告诉用户用 pip 或 conda 装 `pooch`。
- 第 34-37 行：构造带自定义 User-Agent 的 HTTP 下载器（细节见 4.3 节）。第 34 行的注释 `# https://github.com/scipy/scipy/issues/21879` 点明了这一改动的原因。
- 第 38-39 行：注释强调「`fetch` 返回的是下载文件的**完整路径**」，随后调用 `data_fetcher.fetch(dataset_name, downloader=downloader)` 并 `return`。

再看一个真实的调用点——`ascent()` 如何使用 `fetch_data`：

[_fetchers.py:76-80 — ascent() 调用 fetch_data 拿到路径后再用 pickle 解析](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_fetchers.py#L76-L80)

注意第 76 行 `fname = fetch_data("ascent.dat")` 的 `fname` 是一个**本地路径字符串**，第 78 行才 `open(fname, 'rb')` 真正读取内容。这就印证了「`fetch_data` 只负责拿路径、不负责解析」的职责划分。

#### 4.2.4 代码实践

**实践目标**：跟踪一条真实调用链，体会 `fetch_data` 作为「公共助手」的位置。

**操作步骤**：

1. 阅读 `ascent()`（_fetchers.py 第 42-80 行）、`electrocardiogram()`（第 83-180 行）、`face()`（第 183-225 行）三个函数。
2. 找到它们各自调用 `fetch_data(...)` 的那一行，记录传入的文件名。
3. 在一张纸上画出统一形状的调用链：`数据集函数 → fetch_data(文件名) → data_fetcher.fetch(...) → 返回本地路径 → 各自解析`。

**需要观察的现象**：三个函数调用 `fetch_data` 时传入的文件名分别是 `ascent.dat`、`ecg.dat`、`face.dat`，恰好对应 `_registry.py` 里 `registry` 的三个键。

**预期结果**：三个数据集函数的「前半段」（拿到路径）完全相同，都走 `fetch_data`；「后半段」（解析路径）各不相同（pickle / npz / bz2）。这正是 `fetch_data` 被抽成公共助手的意义。

**结果说明**：本实践是源码阅读型，不依赖运行；如果你在装好 pooch 的环境里运行 `scipy.datasets.ascent()`，第一次会触发联网下载，第二次走缓存，这与上一讲 `u1-l3` 的现象一致。

#### 4.2.5 小练习与答案

**练习 1**：`fetch_data` 为什么要把 `data_fetcher` 设计成「带默认值的参数」，而不是直接在函数体里引用模块级的 `data_fetcher`？

**参考答案**：这样测试代码可以传入一个自定义的 `data_fetcher`（比如指向 `tmp_path` 的隔离缓存目录），从而在不联网、不污染真实缓存的前提下测试 `fetch_data` 的行为。这是一种「为可测试性留口子」的设计，具体用法会在 `u3-l3` 测试剖析里看到。

**练习 2**：假设用户没装 pooch，调用 `scipy.datasets.ascent()` 时，错误是在哪一行、以什么类型抛出的？

**参考答案**：在 `fetch_data` 的第 30-33 行，以 `ImportError` 抛出。因为导入时 `data_fetcher` 被置为 `None`，并被绑成 `fetch_data` 的默认参数；`ascent()` 调用 `fetch_data("ascent.dat")` 时，函数体第一件事就是 `if data_fetcher is None: raise ImportError(...)`。

---

### 4.3 pooch.HTTPDownloader 与 User-Agent 头

#### 4.3.1 概念说明

`pooch.HTTPDownloader` 是 Pooch 提供的 HTTP 下载器，封装了「发 HTTP 请求 → 接收字节流 → 写入本地文件」的全过程。它可以接受一些可选项，其中最关键的一个是 `headers`——允许你自定义 HTTP 请求头。

为什么要自定义请求头？这要回到 HTTP 协议里的 `User-Agent` 字段。它是请求头之一，作用是告诉服务器「发起请求的客户端是什么」。Python 标准库 `urllib` 发请求时，默认会带一个类似 `Python-urllib/3.x` 的 User-Agent。问题在于：很多服务器（包括常见的静态资源 CDN）会把这种「裸 Python」的默认 User-Agent 当作可疑流量，直接返回 `403 Forbidden`，导致下载失败。

`_fetchers.py` 第 34 行的注释 `# https://github.com/scipy/scipy/issues/21879` 指向的就是这类问题：在某个时间点，使用默认 User-Agent 的下载请求被服务器拒绝，于是 SciPy 改为显式设置一个 User-Agent。SciPy 选择把自己的名字和版本号写进去：

```python
headers={"User-Agent": f"SciPy {sys.modules['scipy'].__version__}"}
```

这样做有两个好处：① 避开对默认 `Python-urllib` User-Agent 的拦截；② 让服务器端日志能识别出「这是 SciPy 发出的请求」，便于统计与排障。

> 关于 issue #21879 的精确细节（具体是哪个服务器、哪个时间点开始拦截、对应的修复 PR 编号），代码里只留了 issue 链接作为索引。本讲依据通用的 HTTP/CDN 拦截原理与代码注释说明动机；如需逐字核实，建议打开该 issue 链接对照（标注「待官方 issue 核实」）。

#### 4.3.2 核心流程

带 User-Agent 的下载流程：

```text
进入 fetch_data
   │
   ├─ downloader = pooch.HTTPDownloader(
   │        headers={"User-Agent": f"SciPy {scipy.__version__}"})
   │
   └─ data_fetcher.fetch(dataset_name, downloader=downloader)
          │
          └─ 缓存未命中时：
               downloader 实际发起 HTTP GET，
               请求头里带上 "User-Agent: SciPy x.y.z"
               → 服务器返回文件字节
               → pooch 校验 SHA256
               → 写入缓存
               → 返回本地路径
```

注意一个细节：**`HTTPDownloader` 只在「真正需要联网下载」时才会用到**。如果文件已经在缓存里且哈希一致，`pooch.fetch` 会直接返回本地路径，根本不会构造/发起 HTTP 请求——也就是说 User-Agent 这条逻辑只在「首次下载」或「缓存损坏需要重新下载」时生效。

#### 4.3.3 源码精读

User-Agent 相关的核心三行：

[_fetchers.py:34-39 — 构造带 SciPy 版本号 User-Agent 的 HTTPDownloader 并传给 fetch](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_fetchers.py#L34-L39)

逐行解读：

- 第 34 行：注释，指向 issue #21879，是这一段改动的「索引」。
- 第 35-37 行：构造 `pooch.HTTPDownloader`，传入 `headers` 字典，其中 `User-Agent` 的值是用 f-string 拼出的 `f"SciPy {sys.modules['scipy'].__version__}"`。
- 第 39 行：把 `downloader` 作为关键字参数传给 `data_fetcher.fetch`。

这里用到一个稍巧的写法：`sys.modules['scipy'].__version__`。为什么不直接 `import scipy` 再取 `scipy.__version__`？因为 `_fetchers.py` 本身就在 `scipy.datasets` 包内部，在包的子模块导入阶段，顶层 `scipy` 包可能尚未完全初始化完毕；通过 `sys.modules['scipy']` 取已注册的模块对象，是一种更稳健的「在包内部拿到自身版本号」的方式（避免潜在的循环导入问题）。

#### 4.3.4 代码实践

**实践目标**：自己用 `pooch.HTTPDownloader(headers={...})` 下载一个小文件，验证自定义 User-Agent 确实生效，并与默认请求头对比。

**操作步骤**（以下为示例代码，需自行在装好 pooch 的环境里运行）：

```python
# 示例代码：验证自定义 User-Agent 生效
import pooch

# 1) 带自定义 User-Agent 的下载器
my_downloader = pooch.HTTPDownloader(
    headers={"User-Agent": "my-tutorial-client/0.1"}
)

# 2) 用 pooch.retrieve 做一次单文件下载（会返回本地路径）
#    下面用一个公开的小文本文件作演示；如该地址不可用，可换成任意支持 HTTPS 的直链文件。
url = "https://raw.githubusercontent.com/scipy/dataset-ascent/main/ascent.dat"
local_path = pooch.retrieve(
    url=url,
    known_hash=None,          # 演示用，生产环境应填 SHA256 以做校验
    fname="ascent_demo.dat",
    downloader=my_downloader,
)
print("下载到本地路径：", local_path)
```

**需要观察的现象**：

1. 程序能正常下载并打印出本地路径，说明带自定义 User-Agent 的请求被服务器接受。
2. 如果你有办法抓包（或临时把 User-Agent 改成一个会被拦截的值，例如某些服务器拒绝的标识），可以对比「自定义 UA 成功 vs. 异常 UA 失败」的差异。

**对比默认请求头的进阶步骤**：用 `pooch.HTTPDownloader()`（不传 `headers`）再下载一次，理论上使用的是 urllib 默认的 `Python-urllib/...`；观察它在目标服务器上是否同样成功。若某些服务器对默认 UA 返回 403、而对自定义 UA 返回 200，就复现了 issue #21879 的核心场景。

**预期结果**：自定义 User-Agent 的下载能成功完成并返回本地路径。

**结果说明**：具体能否成功取决于目标服务器当时的策略与网络环境，若未能复现请标注「待本地验证」。注意 `known_hash=None` 仅用于演示；真实代码（`registry`）都会填入 SHA256 做严格校验，切勿在生产中省略。

#### 4.3.5 小练习与答案

**练习 1**：为什么把 User-Agent 设成 `f"SciPy {scipy.__version__}"`，而不是随便一个字符串比如 `"abc"`？

**参考答案**：① 一个非默认、看起来像「正经客户端」的 User-Agent 能避开服务器对 `Python-urllib` 默认标识的拦截；② 带上 SciPy 的名字和版本号，可以让服务器端日志识别请求来源，便于统计 SciPy 的使用情况与排查问题，这是一种「负责任地表明身份」的做法。

**练习 2**：`HTTPDownloader` 与 User-Agent 相关的逻辑，在「第二次调用 `ascent()`（缓存已命中）」时还会执行吗？

**参考答案**：会「构造」`downloader` 对象（因为 `fetch_data` 每次都会执行到那几行），但不会「真正发起 HTTP 请求」。因为 `pooch.fetch` 发现缓存里已有该文件且 SHA256 一致，会直接返回本地路径，下载器只是被创建出来却不会被实际用来联网。所以 User-Agent 真正生效的时机只有「首次下载」或「缓存损坏需要重下」。

---

## 5. 综合实践

把本讲的三个最小模块串起来：自己写一个「迷你版」的 `data_fetcher` + `fetch_data`，模仿 SciPy 的真实结构，下载并校验一个小文件。

**任务**：完成下面这个脚本，让它能下载 `ascent.dat`、做 SHA256 校验、并打印本地路径。

```python
# 示例代码：迷你版 data_fetcher + fetch_data
import sys
import pooch
from scipy.datasets._registry import registry, registry_urls   # 直接复用官方注册表

# 1) 仿照 _fetchers.py，用 pooch.create 配置一个「自己的」下载管家
my_fetcher = pooch.create(
    path=pooch.os_cache("my-tutorial-cache"),   # 用一个独立缓存目录，避免污染 scipy-data
    base_url="https://github.com/scipy/",       # 必填占位
    registry=registry,                          # 复用官方哈希表
    urls=registry_urls,                         # 复用官方 URL 表
)

# 2) 仿照 fetch_data，构造带 User-Agent 的下载器并 fetch
def my_fetch_data(dataset_name, fetcher=my_fetcher):
    downloader = pooch.HTTPDownloader(
        headers={"User-Agent": f"SciPy-tutorial {sys.version.split()[0]}"}
    )
    return fetcher.fetch(dataset_name, downloader=downloader)

# 3) 跑一遍
if __name__ == "__main__":
    path = my_fetch_data("ascent.dat")
    print("拿到本地路径：", path)
    print("缓存目录：", my_fetcher.path)
```

**验收要点**：

1. 第一次运行应联网下载，结束后打印出本地路径；第二次运行应直接命中缓存（不再联网）。
2. 故意把 `registry` 里 `ascent.dat` 的哈希改错一位再运行，观察 pooch 会因为校验失败而报错——这印证了 4.1 节里 registry 的完整性校验作用。
3. 在脚本里 `print(my_fetcher.registry)` 与 `print(my_fetcher.urls)`，对照 `_registry.py`，确认它们就是你导入的那两张表。

**结果说明**：本实践依赖联网与 pooch，若环境受限请标注「待本地验证」。改坏哈希后具体报错文案以本地实际输出为准。

## 6. 本讲小结

- `data_fetcher` 是一个**模块级单例**，在 `_fetchers.py` 导入时由 `pooch.create(path, base_url, registry, urls)` 一次性配置好；pooch 缺失时它退化为 `None`，实现「导入不报错、用到才报错」的优雅降级。
- 喂给 `pooch.create` 的两张表来自 `_registry.py`：`registry`（文件名→SHA256，做校验）与 `registry_urls`（文件名→完整 URL，决定真实下载地址，覆盖 `base_url`）。
- `fetch_data(dataset_name)` 是一个**薄助手**：先做降级检查，再构造带自定义 User-Agent 的下载器，最后调用 `data_fetcher.fetch(...)` 返回**本地文件路径**（而非内容）；它还通过「`data_fetcher` 作为默认参数」为测试留出了注入接口。
- `pooch.HTTPDownloader(headers={"User-Agent": ...})` 用于自定义 HTTP 请求头，避免默认 `Python-urllib` 标识被服务器拦截（对应 issue #21879），并附带 SciPy 版本号便于服务端识别；它只在真正联网下载时生效，缓存命中时不会实际发起请求。
- 职责划分清晰：`fetch_data` 只负责「拿路径」，三个数据集函数各自负责「解析路径」（pickle / npz / bz2），后者是下一讲 `u2-l3` 的主题。

## 7. 下一步学习建议

本讲把「下载」这一半讲透了，接下来的学习路径：

1. **`u2-l2` 注册表三件套与 SHA256 校验**：本讲多次提到 `registry` / `registry_urls` / `method_files_map`，下一讲会专门拆解这三张表的分工，并演示如何用 `openssl sha256` 自己算哈希、理解 pooch 的校验细节。
2. **`u2-l3` 三种数据集的加载与转换**：本讲只讲到 `fetch_data` 返回路径为止，下一讲会接着讲 `ascent / face / electrocardiogram` 如何把路径上的原始文件（pickle / bz2 / npz）解析成 `ndarray`，包括 ecg 的 ADC 换算公式。
3. **`u3-l1` 可选依赖的降级处理模式**：本讲看到的 `try/except ImportError → None` 只是其中一例，那一讲会横向对比 `_fetchers / _utils / _download_all` 三处降级实现的异同。
4. **延伸阅读**：可直接打开 `pooch` 官方文档，对照阅读 `Pooch.create`、`Pooch.fetch`、`HTTPDownloader` 的 API 说明，把本讲对参数的解读与官方描述相互印证。
