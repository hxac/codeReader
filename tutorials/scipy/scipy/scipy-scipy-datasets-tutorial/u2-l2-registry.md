# 注册表三件套与 SHA256 校验

## 1. 本讲目标

在上一讲（u2-l1）里，我们打开了 `fetch_data` 这个「下载黑盒」，看到 `pooch.create(...)` 接收了两个看似重复的参数：`registry=registry` 和 `urls=registry_urls`。本讲就把目光收到这两个参数背后的数据来源——`_registry.py` 文件，逐张拆开里面定义的**三张映射表**。

学完本讲，你应该能够：

- 区分 `registry`、`registry_urls`、`method_files_map` 三张表各自存的是什么、被谁消费。
- 说清楚一条 SHA256 哈希记录是如何生成的，以及 pooch 如何用它做**下载完整性校验**（防篡改 + 防下载损坏）。
- 解释为什么一个「方法名」需要映射到「一个或多个文件名」，并能把这套映射关系跟 `_utils.py` 的缓存清理逻辑对上号。

本讲只聚焦「数据本身是如何被登记和描述的」，不涉及具体文件格式（pickle / bz2 / npz）的解析——那是下一讲 u2-l3 的主题。

## 2. 前置知识

### 2.1 什么是哈希（hash）

哈希函数把**任意长度的输入**（比如一个几 MB 的二进制文件）压缩成一个**固定长度的字符串**（叫「指纹」或「摘要」）。它有两个关键性质：

1. **确定性**：同一个文件无论算多少次，结果都一样。
2. **抗碰撞**：文件哪怕只改动 1 个字节，算出来的哈希也会**面目全非**；想人为造出一个「内容不同但哈希相同」的文件，在计算上几乎不可能。

**SHA256** 是一种常见的哈希算法，输出 256 位、即 64 个十六进制字符。本讲里你会看到形如下面的字符串：

```
03ce124c1afc880f87b55f6b061110e2e1e939679184f5614e38dacc6c1957e2
```

### 2.2 为什么数据集需要「注册表」

`scipy.datasets` 的数据文件并不随 SciPy 源码一起发布，而是放在公网（GitHub）上，**第一次调用时才联网下载**。既然要从网络拿文件，就必须回答三个问题：

| 问题 | 由哪张表回答 |
| --- | --- |
| 这个文件**正确的样子**是什么？（如何确认下载没出错、没被篡改） | `registry`（哈希） |
| 这个文件**去哪里下载**？ | `registry_urls`（远程地址） |
| 用户调用的某个**方法名**，对应缓存里的**哪些文件**？ | `method_files_map`（方法-文件映射） |

这三张表合起来就是本讲的「注册表三件套」。它们都是普通的 Python `dict`，没有任何魔法——**注册表的本质就是把「文件名」这个唯一钥匙，映射到三种不同的描述信息上**。

### 2.3 你需要记住的上下文

- `_registry.py` 是整个子模块的「地基」：它**不被任何业务逻辑依赖的具体细节，反而被多方依赖**（`_fetchers.py`、`_utils.py`、`_download_all.py` 都 `from ._registry import ...`）。这一点在 u1-l2 已讲过。
- `pooch.create(...)` 在 `_fetchers.py` 导入时就把这三张表里的两张（`registry` 与 `registry_urls`）一次性交给 pooch，构造出模块级单例 `data_fetcher`（见 u2-l1）。

## 3. 本讲源码地图

| 文件 | 本讲关注的部分 | 作用 |
| --- | --- | --- |
| [_registry.py](_registry.py) | 三张 dict 的定义 | **本讲主角**：登记所有数据集的哈希、URL、方法-文件映射 |
| [_fetchers.py](_fetchers.py) | L4 导入 + L14-26 `pooch.create` | 把 `registry` / `registry_urls` 喂给 pooch，是哈希校验的**消费方** |
| [_utils.py](_utils.py) | L3 导入 + L13-58 `_clear_cache` | 把 `method_files_map` 当作「方法名 → 文件名」的查表依据 |
| [_download_all.py](_download_all.py) | L18-23 + L58-61 | 遍历 `registry`、用 `registry_urls` 批量下载，是三件套的另一处消费方 |
| [tests/test_data.py](tests/test_data.py) | L16-20 `_has_hash` | 测试里直接用 `registry` 的哈希值做断言，印证校验机制 |

## 4. 核心概念与源码讲解

### 4.1 registry：文件名 → SHA256 哈希表

#### 4.1.1 概念说明

`registry` 是三件套里**最核心**的一张表：它把每个数据文件名映射到该文件内容的 SHA256 哈希。它的职责只有一个——**描述「正确的文件长什么样」**。

只要手里有这张表，任何时候拿到一个文件，算一下它的 SHA256，再和表里的值比一比，就能判断：

- 文件**是否完整**（下载过程中没有损坏、截断）。
- 文件**是否被篡改**（没有被中间人换成恶意内容）。

这正是 pooch 做「下载完整性校验（integrity check）」的全部依据。

#### 4.1.2 核心流程

SHA256 校验在 pooch 内部大致经历这样的流程（本讲只讲数据层面，下载器细节见 u2-l1）：

```text
调用 fetch_data("ascent.dat")
        │
        ▼
data_fetcher.fetch("ascent.dat")
        │
        ├─ 缓存里已有 ascent.dat？
        │     └─ 算它的 SHA256，和 registry["ascent.dat"] 比对
        │           ├─ 相等 → 命中缓存，直接返回本地路径（不再下载）
        │           └─ 不等 → 视为无效，触发（重新）下载
        │
        └─ 需要下载：
              ├─ 从 registry_urls["ascent.dat"] 拉取文件
              ├─ 算下载结果的 SHA256，和 registry["ascent.dat"] 比对
              │     ├─ 相等 → 校验通过，写入缓存，返回路径
              │     └─ 不等 → 抛错（文件损坏或被篡改）
```

换句话说，`registry` 里的哈希被用了**两次**：一次用来识别「缓存里那份是不是好货」，一次用来验收「刚下载下来的那份是不是好货」。无论哪种，只要哈希对不上，pooch 都不会把文件当成可信数据交给你。

> 说明：上述「哈希不匹配时的具体重试/报错策略」是 pooch 的内部实现细节，不同版本行为可能略有差异。本讲只强调**数据层面**的结论：哈希是可信性的唯一判据。具体报错形式请以本地安装的 pooch 版本为准（待本地验证）。

#### 4.1.3 源码精读

文件开头有一段对维护者很重要的注释，点明了哈希是怎么算出来的：

[_registry.py:L6-L12](_registry.py#L6-L12) —— 用 `openssl sha256 <filename>` 生成哈希，然后登记进 `registry` 字典：

```python
# To generate the SHA256 hash, use the command
# openssl sha256 <filename>
registry = {
    "ascent.dat": "03ce124c1afc880f87b55f6b061110e2e1e939679184f5614e38dacc6c1957e2",
    "ecg.dat": "f20ad3365fb9b7f845d0e5c48b6fe67081377ee466c3a220b7f69f35c8958baf",
    "face.dat": "9d8b0b4d081313e2b485748c770472e5a95ed1738146883d84c7030493e82886"
}
```

注意三点：

1. **键就是文件名**：`ascent.dat` / `ecg.dat` / `face.dat`。这也是将来落在缓存目录里的文件名（见 u1-l3）。
2. **值是 64 位十六进制字符串**：每两个十六进制字符代表 1 字节，64 字符 = 32 字节 = 256 位，正好是 SHA256 的输出长度。
3. **注释就是文档**：`openssl sha256 <filename>` 这条命令是新增数据集时的标准操作（4.4 综合实践和 u3-l4 会用到）。

这张表随后在 [_fetchers.py:L4](_fetchers.py#L4) 被导入：

```python
from ._registry import registry, registry_urls
```

并在 [_fetchers.py:L14-L26](_fetchers.py#L14-L26) 交给 `pooch.create`，其中 `registry=registry`（[_fetchers.py:L24](_fetchers.py#L24)）就是 pooch 用来做完整性校验的「标准答案」。

测试代码则把这套校验机制讲得更直白——[tests/test_data.py:L16-L20](tests/test_data.py#L16-L20) 定义了一个 `_has_hash` 辅助函数，本质就是「算文件哈希，和期望值比是否相等」：

```python
def _has_hash(path, expected_hash):
    """Check if the provided path has the expected hash."""
    if not os.path.exists(path):
        return False
    return pooch.file_hash(path) == expected_hash
```

随后每个数据集测试都用 `registry[...]` 作为期望哈希做断言，例如 [tests/test_data.py:L42-L43](tests/test_data.py#L42-L43)：

```python
assert _has_hash(os.path.join(data_dir, "ascent.dat"),
                 registry["ascent.dat"])
```

这条断言的含义是：「缓存里的 `ascent.dat`，算出来的哈希必须等于 `registry` 里登记的值」。这等价于在测试层面**复现了一遍 pooch 的完整性校验**。

#### 4.1.4 代码实践

**实践目标**：亲手算出一个文件的 SHA256，体会「文件名 → 哈希」这条记录是怎么来的，并验证哈希的「抗碰撞」性质。

**操作步骤**：

1. 任意准备一个小文件，比如把上面这段注释存成 `demo.txt`：
   ```bash
   echo "hello scipy datasets" > demo.txt
   ```
2. 用源码注释里推荐的命令计算哈希：
   ```bash
   openssl sha256 demo.txt
   ```
   记录输出的 64 位十六进制串。
3. 用 Python 的 `hashlib` 再算一次，对照结果是否一致：
   ```python
   import hashlib
   print(hashlib.sha256(open("demo.txt", "rb").read()).hexdigest())
   ```
4. 在 `demo.txt` 末尾加一个空格再存盘，重新执行第 2 步。

**需要观察的现象**：

- 第 2 步与第 3 步的哈希值**完全相同**（两种工具算的是同一个东西）。
- 第 4 步改了 1 个字符后，哈希值**彻底改变**（几乎每一位都不同）。

**预期结果**：你会得到一条形如 `{"demo.txt": "<64位十六进制>"}` 的记录，格式与 `registry` 完全一致；并直观感受到「哪怕改 1 字节，哈希也面目全非」。

> 说明：`openssl` / `hashlib` 的具体输出值取决于你写入的字节内容，本讲不预填具体哈希值（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `registry` 里 `ascent.dat` 的哈希值**故意改错一位**，再次调用 `scipy.datasets.ascent()` 会发生什么？

**参考答案**：缓存里若已有正确的 `ascent.dat`，pooch 用错的哈希去比对会发现「不匹配」，从而认为缓存无效，触发重新下载；下载完成后用错的哈希验收依然不匹配，最终抛出哈希校验失败的错误。结论：哈希记录错误会直接导致数据集**无法被信任**。

**练习 2**：`registry` 里的值为什么是 64 个字符，而不是 32 或 128？

**参考答案**：SHA256 输出 256 位。用十六进制表示时，每 4 位对应 1 个十六进制字符，因此 256 / 4 = 64 个字符。

---

### 4.2 registry_urls：文件名 → 远程地址表

#### 4.2.1 概念说明

`registry_urls` 回答的是「去哪里下载」。它把每个文件名映射到该文件在 GitHub 上的**完整原始地址**（`raw.githubusercontent.com`）。

回顾 u2-l1 的一个关键细节：`pooch.create` 里有个 `base_url="https://github.com/scipy/"` 参数，但它只是个**占位符**。真正决定下载地址的是这张 `registry_urls` 表——pooch 会优先用 `urls`（即 `registry_urls`）里给出的完整地址，从而**覆盖**掉 `base_url + 文件名` 的默认拼接规则。

#### 4.2.2 核心流程

文件名 → 下载地址 的解析流程：

```text
fetch_data("ecg.dat")
   └─ pooch 查 urls 表（即 registry_urls）
         └─ registry_urls["ecg.dat"]
               = "https://raw.githubusercontent.com/scipy/dataset-ecg/main/ecg.dat"
   └─ 从该地址下载，再用 registry["ecg.dat"] 校验哈希
```

这里能看到三件套**分工协作**的雏形：

- `registry_urls` 负责「**去哪下**」。
- `registry` 负责「**下得对不对**」。
- 两者都以**文件名**为共同的键，因而能一一配对。

#### 4.2.3 源码精读

[_registry.py:L14-L18](_registry.py#L14-L18) 定义了 `registry_urls`：

```python
registry_urls = {
    "ascent.dat": "https://raw.githubusercontent.com/scipy/dataset-ascent/main/ascent.dat",
    "ecg.dat": "https://raw.githubusercontent.com/scipy/dataset-ecg/main/ecg.dat",
    "face.dat": "https://raw.githubusercontent.com/scipy/dataset-face/main/face.dat"
}
```

注意 URL 的命名规律：每个数据集都对应一个**独立的 GitHub 仓库**，仓库名遵循 `dataset-<name>` 约定（与 `__init__.py` 模块文档说明一致，见 u1-l1）。例如 `face.dat` 来自 `scipy/dataset-face` 仓库。

这张表同样在 [_fetchers.py:L4](_fetchers.py#L4) 被导入，并通过 `urls=registry_urls`（[_fetchers.py:L25](_fetchers.py#L25)）交给 pooch。源码注释（[_fetchers.py:L20-L23](_fetchers.py#L20-L23)）明确点出了「`base_url` 是必填但会被 `urls` 覆盖」这一设计：

```python
# The remote data is on Github
# base_url is a required param, even though we override this
# using individual urls in the registry.
base_url="https://github.com/scipy/",
registry=registry,
urls=registry_urls
```

另一处消费方是 `_download_all.py` 的批量下载逻辑——[_download_all.py:L58-L61](_download_all.py#L58-L61) 遍历 `registry` 拿到「文件名 + 哈希」，再用 `registry_urls[dataset_name]` 取地址：

```python
for dataset_name, dataset_hash in _registry.registry.items():
    pooch.retrieve(url=_registry.registry_urls[dataset_name],
                   known_hash=dataset_hash,
                   fname=dataset_name, path=path, downloader=downloader)
```

这段代码极其精炼地展示了三件套里前两张表的**配对使用方式**：`registry` 提供 `known_hash`（校验），`registry_urls` 提供 `url`（下载）。pooch 的 `retrieve` 函数正是靠 `(url, known_hash)` 这对参数完成「下载 + 校验」一体的。

#### 4.2.4 代码实践

**实践目标**：通过阅读源码，把「文件名 → 远程地址」这条映射关系人工复原，理解 `registry` 与 `registry_urls` 如何以文件名为键配对。

**操作步骤**：

1. 打开 [_registry.py](_registry.py)，在纸上画一张三列表的对照表：`文件名 | registry 哈希(前8位) | registry_urls 地址`。
2. 把三个文件（`ascent.dat` / `ecg.dat` / `face.dat`）逐行填满。
3. 打开浏览器，访问其中任意一个 `registry_urls` 地址（例如 face 的那个），观察返回的是**原始二进制文件**而非网页。
4. 阅读上面的 `_download_all.py:L58-L61` 代码片段，确认 `registry.items()` 遍历出的 `dataset_name` 能同时作为 `registry_urls` 和缓存文件名（`fname`）的键。

**需要观察的现象**：

- 三张表的**键（文件名）完全一致**，且都是 `ascent.dat` / `ecg.dat` / `face.dat`。
- 浏览器访问 `registry_urls` 地址会触发文件下载，而不是显示 GitHub 网页——因为 `raw.githubusercontent.com` 返回的是文件原始内容。

**预期结果**：你会得到一张配对整齐的对照表，并能用自己的话说出「`registry` 与 `registry_urls` 共享同一组键，因此能在 `_download_all` 里用同一次遍历同时取到哈希和地址」。

#### 4.2.5 小练习与答案

**练习 1**：既然每个数据集都在独立的 `dataset-<name>` 仓库里，为什么 `pooch.create` 还要保留那个看似没用的 `base_url`？

**参考答案**：因为 `base_url` 是 `pooch.create` 的**必填参数**。本项目通过 `urls=registry_urls` 为每个文件提供完整地址来覆盖它，但参数本身不能省略；源码注释也明确写了 "base_url is a required param, even though we override this"。

**练习 2**：如果要新增一个数据集 `foo.dat`，存放在 `scipy/dataset-foo` 仓库，`registry_urls` 该新增哪一行？

**参考答案**：新增 `"foo.dat": "https://raw.githubusercontent.com/scipy/dataset-foo/main/foo.dat"`，遵循 `dataset-<name>` 与 `main` 分支的既有约定。

---

### 4.3 method_files_map：方法名 → 文件列表映射

#### 4.3.1 概念说明

前两张表的键都是**文件名**。但用户实际调用的是**方法名**：`scipy.datasets.ascent()`、`scipy.datasets.face()`、`scipy.datasets.electrocardiogram()`。注意 `electrocardiogram` 这个方法名和它对应的文件 `ecg.dat` **并不同名**。

`method_files_map` 就是补上这一层「方法名 → 文件名」翻译的第三张表。它的值是**列表**（而非单个字符串），这一点暗示了一个重要设计：**一个方法原则上可以对应多个数据文件**。

这张表目前唯一的消费者是 `_utils.py` 里的缓存清理逻辑——它需要知道「清理某个方法时，该删缓存里的哪些文件」。

#### 4.3.2 核心流程

缓存清理时，方法名到文件的解析流程（详见 u2-l4）：

```text
clear_cache([datasets.ascent])
   └─ 取 datasets.ascent.__name__  →  "ascent"
   └─ 查 method_files_map["ascent"]  →  ["ascent.dat"]
   └─ 对列表里每个文件：
         cache_dir/ascent.dat 存在？删掉 : 提示「不存在」
```

关键点：

- **方法名取自函数的 `__name__` 属性**，而不是用户传入的字符串，因此 `clear_cache` 要求传入的是**可调用对象本身**（`datasets.ascent`），而非字符串 `"ascent"`。
- 如果方法名不在 `method_files_map` 里，会抛 `ValueError`（防止用户传错方法）。
- 因为值是列表，所以「一个方法对应多个文件」的清理路径天然支持（测试里专门测了这条分支，见 4.3.3）。

#### 4.3.3 源码精读

[_registry.py:L20-L26](_registry.py#L20-L26) 定义了 `method_files_map`，注释里画出了 `<method_name> : ["filename1", ...]` 的格式：

```python
# dataset method mapping with their associated filenames
# <method_name> : ["filename1", "filename2", ...]
method_files_map = {
    "ascent": ["ascent.dat"],
    "electrocardiogram": ["ecg.dat"],
    "face": ["face.dat"]
}
```

三个观察：

1. `"electrocardiogram"` 对应 `"ecg.dat"`——**方法名与文件名不同名**，这正是需要这张映射表的根本原因。
2. 当前每个方法的值都是**单元素列表**，但格式上已为「多文件」留好扩展空间。
3. 这张表的键是方法名，与前两张表（键是文件名）**处于不同的命名空间**，不可混淆。

它在 [_utils.py:L3](_utils.py#L3) 被导入：

```python
from ._registry import method_files_map
```

并在 [_utils.py:L13-L16](_utils.py#L13-L16) 作为 `_clear_cache` 的默认 `method_map`：

```python
def _clear_cache(datasets, cache_dir=None, method_map=None):
    if method_map is None:
        # Use SciPy Datasets method map
        method_map = method_files_map
```

注意 `method_map` 是个**可注入参数**（默认值为 `method_files_map`，但允许调用方传入自定义映射）。测试正是利用这一点，用一个 `dummy_method_map` 做离线隔离测试。

真正「方法名 → 文件列表」的查表发生在 [_utils.py:L39-L48](_utils.py#L39-L48)：

```python
dataset_name = dataset.__name__  # Name of the dataset method
if dataset_name not in method_map:
    raise ValueError(f"Dataset method {dataset_name} doesn't "
                     "exist. ...")

data_files = method_map[dataset_name]
data_filepaths = [os.path.join(cache_dir, file)
                  for file in data_files]
```

这段代码把三件套里第三张表的用途体现得淋漓尽致：拿到方法名 → 查表得文件列表 → 拼成缓存绝对路径 → 逐个删除。

测试 [tests/test_data.py:L99-L111](tests/test_data.py#L99-L111) 还专门验证了「一个方法对应多个文件」的分支——它构造了一个 `dummy_method_map["data4"] = ["data4_0.dat", "data4_1.dat"]`，确认两个文件都会被清理：

```python
dummy_method_map["data4"] = ["data4_0.dat", "data4_1.dat"]
_clear_cache(datasets=[data4], cache_dir=dummy_basepath,
             method_map=dummy_method_map)
assert not os.path.exists(dummy_basepath/"data4_0.dat")
assert not os.path.exists(dummy_basepath/"data4_1.dat")
```

这说明 `method_files_map` 用「列表」作值并不是过度设计，而是被测试明确覆盖的真实能力。

#### 4.3.4 代码实践

**实践目标**：理解「方法名 → 文件名」这层翻译，并验证方法名与文件名不必相同。

**操作步骤**：

1. 在 Python 里执行（不需要联网，只是查表）：
   ```python
   from scipy.datasets._registry import method_files_map
   for method, files in method_files_map.items():
       print(method, "->", files)
   ```
2. 对照 [_fetchers.py](_fetchers.py) 里三个数据集函数的定义（`ascent`、`electrocardiogram`、`face`），确认函数名与 `method_files_map` 的键一一对应。
3. 注意 `electrocardiogram`（方法名）与 `ecg.dat`（文件名）的**不同名**关系，并思考：如果没有这张表，`clear_cache` 要怎么知道清理 `electrocardiogram` 时该删哪个文件？

**需要观察的现象**：

- 打印结果中 `electrocardiogram -> ['ecg.dat']`，直观体现「方法名 ≠ 文件名」。
- 三个键与 `_fetchers.py` 里三个被 `@xp_capabilities` 装饰的函数名完全吻合。

**预期结果**：你能清楚说明 `method_files_map` 解决的是「用户视角的方法名」与「存储视角的文件名」之间的**命名鸿沟**，并且它专为缓存清理场景服务。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `method_files_map` 的值是**列表**而不是单个字符串？

**参考答案**：为了支持「一个方法对应多个数据文件」的场景。当前三个数据集各只对应一个文件，但测试（`test_clear_cache`）专门构造了 `["data4_0.dat", "data4_1.dat"]` 的双文件用例来覆盖这条分支，说明这是被设计保留的真实能力，便于将来扩展。

**练习 2**：如果用户调用 `datasets.clear_cache([datasets.electrocardiogram])`，`_clear_cache` 内部最终会去缓存目录删除哪个文件？

**参考答案**：会取 `datasets.electrocardiogram.__name__` 得到 `"electrocardiogram"`，查 `method_files_map["electrocardiogram"]` 得到 `["ecg.dat"]`，因此删除的是缓存目录下的 `ecg.dat`。

---

## 5. 综合实践

**实践目标**：把三件套串起来——亲手为一个「本地文件」造一套完整的注册表记录，并验证 pooch 风格的哈希校验逻辑。

**操作步骤**：

1. 准备一个本地文件并计算其 SHA256（综合 4.1 的命令与代码）：
   ```bash
   echo "registry trio demo" > mydata.txt
   openssl sha256 mydata.txt
   ```
   假设得到哈希 `H`（64 位十六进制串）。
2. 仿照 `registry` 的格式，写出属于 `mydata.txt` 的「三件套」记录（示例代码，非项目原有代码）：
   ```python
   # 示例代码：仿造 scipy.datasets 的注册表三件套
   my_registry = {
       "mydata.txt": H,                       # 来自上一步 openssl 输出
   }
   my_registry_urls = {
       "mydata.txt": "https://example.com/dataset-mydata/main/mydata.txt",
   }
   my_method_files_map = {
       "my_dataset": ["mydata.txt"],
   }
   ```
3. 用 `hashlib` 写一个最小校验函数，复刻 `tests/test_data.py` 里 `_has_hash` 的逻辑（示例代码）：
   ```python
   import hashlib, os
   def has_hash(path, expected):
       if not os.path.exists(path):
           return False
       actual = hashlib.sha256(open(path, "rb").read()).hexdigest()
       return actual == expected
   ```
4. 调用 `has_hash("mydata.txt", my_registry["mydata.txt"])`，确认返回 `True`；然后把文件内容改一个字符再调用，确认返回 `False`。
5. 用一句话说明：如果 pooch 要下载 `mydata.txt`，它会用 `my_registry_urls["mydata.txt"]` **取地址**、用 `my_registry["mydata.txt"]` **做校验**，而 `my_method_files_map["my_dataset"]` 则在**清理缓存**时用来把方法名翻译成文件名。

**需要观察的现象**：

- 第 4 步：未改动文件时校验通过（`True`）；改动后校验失败（`False`）——这正是 pooch 判断「下载是否可信」的核心机制。
- 三张表以**文件名**（前两张）或**方法名**（第三张）为键，各司其职、互不混淆。

**预期结果**：你将得到一套与 `_registry.py` 结构完全一致的迷你注册表，并能口述三件套在「下载—校验—清理」三个环节中分别扮演的角色。

> 说明：本实践不需要联网，也不依赖 pooch；步骤 2 的 URL 与步骤 3 的函数都是**示例代码**，用于复刻机制，并非 SciPy 源码。具体哈希值 `H` 取决于你写入 `mydata.txt` 的内容（待本地验证）。

## 6. 本讲小结

- `_registry.py` 用三张普通 `dict` 构成「注册表三件套」，是整个 `scipy.datasets` 子模块被多方依赖的**地基**。
- `registry`：**文件名 → SHA256 哈希**，是 pooch 做**下载完整性校验**（防损坏、防篡改）的唯一判据，哈希由 `openssl sha256 <filename>` 生成。
- `registry_urls`：**文件名 → 远程地址**，决定「去哪下」；它通过 `urls=` 参数**覆盖** `pooch.create` 里那个仅作占位的 `base_url`。
- `method_files_map`：**方法名 → 文件列表**，专为缓存清理服务，弥合「用户方法名（如 `electrocardiogram`）」与「存储文件名（如 `ecg.dat`）」之间的命名鸿沟，并用列表形式预留「一对多」能力。
- 三张表分工清晰：`registry_urls` 管「去哪下」、`registry` 管「下得对不对」、`method_files_map` 管「清理时删哪些文件」；前两者以文件名为键配对使用，第三者以方法名为键。
- 测试代码（`_has_hash`、`dummy_method_map`）从校验与清理两个角度，把三件套的机制都覆盖到了。

## 7. 下一步学习建议

- 下一讲 **u2-l3「三种数据集的加载与转换」**将回答：`fetch_data` 拿回本地文件路径之后，`ascent` / `face` / `electrocardiogram` 各自如何把原始文件（pickle / bz2 / npz）**解封装**成 ndarray。届时你会看到 `registry` 里的三个文件名分别对应三种不同的本地加载手段。
- 如果你对缓存清理更感兴趣，可以直接跳到 **u2-l4「缓存清理机制」**，那里会完整拆解 `_clear_cache` 如何使用本讲的 `method_files_map`。
- 想从更高层看「三件套如何被批量消费」，可阅读 [_download_all.py:L58-L61](_download_all.py#L58-L61)，它用一次 `registry.items()` 遍历同时取到哈希与地址，是理解三件套协作的最精炼片段。
