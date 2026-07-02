# 扩展实战：新增一个数据集

> 本讲是「专家层」的收官篇，也是整本手册的**综合实战**。它把你前八讲学到的所有零件**一次性串起来**：u2-l1 的 `data_fetcher`/`fetch_data` 获取层、u2-l2 的注册表三件套（`registry`/`registry_urls`/`method_files_map`）、u2-l3 的「取路径 → 解析成 ndarray」两段式结构、u2-l4 的缓存清理通用逻辑、u3-l1 的可选依赖降级、u3-l2 的 `download_all` 注册表驱动、u3-l3 的测试两层结构。本讲的任务只有一个：**如果你要给 `scipy.datasets` 新增一个数据集，到底要改哪几处、不用改哪几处、为什么。** 同时补上两个之前没展开的「生态集成点」——`@xp_capabilities(out_of_scope=True)` 装饰器与 `meson.build` 安装声明。

---

## 1. 本讲目标

学完本讲，你应当能够：

1. 背出**新增一个数据集的改动清单**：哪几处是「必改」（注册表三件套 + 新 fetcher 函数 + 公开导出 + 一条测试），哪几处是「自动受益、无需改动」（`data_fetcher`、`fetch_data`、`clear_cache`、`download_all`、autouse fixture），并能说清背后「注册表驱动」的原因。
2. 读懂 `@xp_capabilities(out_of_scope=True)` 装饰器的**两个效果**：它把函数标记为「不在数组 API 标准的适配范围内」，并通过往文档字符串注入一段说明来落地这一声明；理解为什么 `scipy.datasets` 的全部五个公开函数都要戴这顶帽子。
3. 看懂 `meson.build` 与 `tests/meson.build` 如何用 `python_sources` 列表 + `py3.install_sources` 声明「哪些源文件/测试文件要被安装」，并知道新增数据集时什么情况下需要动 meson、什么情况下完全不用动。
4. 亲手在一个**不联网、不改源码**的本地 mock 环境里，走通「注册 → fetch → 解析 → 测试」的最小闭环。

---

## 2. 前置知识

本讲是「造东西」而不是「读机制」，所以前置概念更偏工程拼装。先把两个新名词讲清楚，旧名词一句话带过：

- **Python Array API 标准**：这是一份跨后端的数组运算接口规范。理想情况下，同一段代码既能吃 NumPy 数组，也能吃 CuPy / PyTorch / JAX / Dask 等后端的数组——只要这些后端都实现了该标准。SciPy 正在逐步让自己的函数对齐这个标准（即「数组 API 兼容」）。但**不是所有函数都能/都需要对齐**：有些函数天生只产出 NumPy 数组、或本身就不在适配目标内，这时就要显式声明「我不在这个范围里」，以免被合规测试机器误测、误报。
- **`out_of_scope`（超出范围）**：`@xp_capabilities` 装饰器的一个开关。`out_of_scope=True` 的含义就是上一条说的「我主动声明不参与数组 API 标准的跨后端适配」。
- **meson 构建系统**：SciPy 用 Meson + Ninja 构建。对纯 Python 子模块（如 `scipy.datasets`）而言，meson 的主要职责是**声明哪些 `.py` 文件要被打包安装到用户机器上**。这一声明写在 `meson.build` 里，靠一个文件名列表完成。

旧名词快速复习（细节见对应讲义）：

- **注册表三件套**（u2-l2）：`registry`（文件名→SHA256，下得对不对）、`registry_urls`（文件名→URL，去哪下）、`method_files_map`（方法名→文件名列表，清理时删哪些）。
- **`data_fetcher` 单例**（u2-l1）：模块导入时由 `pooch.create(path, base_url, registry=registry, urls=registry_urls)` **一次性**装配，之后所有数据集共用。
- **`fetch_data` 两段式**（u2-l1/u2-l3）：先 `fetch_data("xxx.dat")` 拿到本地路径，再各自解析成 ndarray。
- **可选依赖降级**（u3-l1）：`try: import pooch / except ImportError: pooch = None`，导入静默、调用才报错。
- **测试两层结构**（u3-l3）：在线内容断言（`TestDatasets` + autouse fixture 预热）+ 离线逻辑隔离（`test_clear_cache` 用 `tmp_path`）。

> 一句话总览：`scipy.datasets` 是一个**高度数据驱动**的子模块——绝大多数行为由 `_registry.py` 里的三张表决定，代码本身是「通用引擎」。这意味着新增数据集的**代码改动量极小**，主要工作是「填表 + 写一个解析函数」。

---

## 3. 本讲源码地图

本讲把全模块的文件都调动起来，每 个文件在「新增数据集」这件事里扮演不同角色：

| 文件 | 在「新增数据集」里的角色 |
|---|---|
| `scipy/datasets/_registry.py` | **必改·地基**。三张表各加一条：`registry` / `registry_urls` / `method_files_map`。 |
| `scipy/datasets/_fetchers.py` | **必改·引擎**。新增一个戴 `@xp_capabilities(out_of_scope=True)` 的加载函数；`data_fetcher` / `fetch_data` 不用动。 |
| `scipy/datasets/__init__.py` | **必改·门面**（若要公开）。在 `from ._fetchers import` 与 `__all__` 里各补一项。 |
| `scipy/datasets/_utils.py` | **不用改**。`_clear_cache` 通用查 `method_files_map`，新条目自动生效。 |
| `scipy/datasets/_download_all.py` | **不用改**。`download_all` 遍历 `registry`，新文件自动被批量下载。 |
| `scipy/datasets/meson.build` | **看情况改**。仅在新增**独立 `.py` 文件**时才需把文件名加进 `python_sources` 列表。 |
| `scipy/datasets/tests/meson.build` | 同上，仅新增独立测试文件时改。 |
| `scipy/datasets/tests/test_data.py` | **必改·测试**。仿照 `test_ascent` 加一个 shape + 哈希断言；autouse fixture 不用动。 |
| `scipy/_lib/_array_api.py` | **只读参考**。`xp_capabilities` 装饰器的实现所在地，理解 `out_of_scope` 行为时查阅。 |

---

## 4. 核心概念与源码讲解

### 4.1 模块一：新增数据集的改动清单

#### 4.1.1 概念说明

回忆 u2-l2 的核心结论：数据文件**不随源码发布**，首次调用才联网下载，所以需要三张表分别回答「下得对不对」「去哪下」「清理时删哪些」。这三张表就是 `scipy.datasets` 的「**单一事实来源（single source of truth）**」——几乎所有机制都从它们派生。

这个设计带来一个直接后果：**新增一个数据集，本质上是「往三张表里各填一行 + 写一个解析函数」**，而**不是**去修改下载器、缓存管理器、批量下载脚本。换句话说，引擎是通用的、写死的；数据是个别的、配置化的。

理解这一点非常重要，因为它决定了你「该改哪里」和「不该改哪里」。很多初学者会本能地去改 `fetch_data` 或 `data_fetcher` 来支持新数据集——**那是错的**，因为它们已经是数据无关的通用代码，新数据集只要被登记进表里，就会自动被它们处理。

#### 4.1.2 核心流程

下面这张表是本讲最重要的产出——**新增一个名为 `foo`、对应文件 `foo.dat` 的数据集时，逐文件的改动判断**：

| 改动位置 | 是否需要改 | 原因 |
|---|---|---|
| `_registry.py` → `registry` | ✅ 必改 | 加 `"foo.dat": "<SHA256>"`，否则 pooch 无法校验完整性 |
| `_registry.py` → `registry_urls` | ✅ 必改 | 加 `"foo.dat": "https://...dataset-foo.../foo.dat"`，否则不知道去哪下 |
| `_registry.py` → `method_files_map` | ✅ 必改 | 加 `"foo": ["foo.dat"]`，否则 `clear_cache([datasets.foo])` 找不到要删的文件 |
| `_fetchers.py` → 新增 `foo()` 函数 | ✅ 必改 | 加载 + 解析 `foo.dat` 的逻辑只对新数据集存在 |
| `_fetchers.py` → `data_fetcher` 单例 | ❌ 不用改 | 它在导入时**一次性**读 `registry`/`registry_urls`，新条目自动被装配进去 |
| `_fetchers.py` → `fetch_data` 助手 | ❌ 不用改 | 它是数据集无关的通用「拿路径」函数 |
| `__init__.py` → 导入 + `__all__` | ✅ 改（若要公开） | 不导出则用户无法 `datasets.foo()` 访问（详见 u1-l1 的「公开合约」） |
| `_utils.py` → `clear_cache` | ❌ 不用改 | `_clear_cache` 通用遍历 `method_files_map`，新条目自动可清 |
| `_download_all.py` → `download_all` | ❌ 不用改 | 它遍历 `registry.items()`，新文件自动被批量下载（u3-l2） |
| `tests/test_data.py` → 新增 `test_foo` | ✅ 必改 | 断言新数据集的 shape + 哈希 |
| `tests/test_data.py` → autouse fixture | ❌ 不用改 | fixture 调 `download_all()`，会自动预热新文件（u3-l3） |
| `meson.build` → `python_sources` | ⚠️ 仅当新增**独立文件** | 改的是现有文件则无需动 meson；新增 `.py` 文件才需登记 |
| 外部仓库 `scipy/dataset-foo` | ✅ 必建（仓库外） | 真实数据要托管在 GitHub 上，URL 才有效 |

把这张表抽象成流程，新增数据集的「必改」路径是：

```
           ┌─────────────────────────────────────────────┐
           │  ① _registry.py 三张表各加一条（填表）        │
           └────────────────────┬────────────────────────┘
                                ▼
           ┌─────────────────────────────────────────────┐
           │  ② _fetchers.py 写 foo() 解析函数            │
           │     （戴 @xp_capabilities(out_of_scope=True)）│
           └────────────────────┬────────────────────────┘
                                ▼
           ┌─────────────────────────────────────────────┐
           │  ③ __init__.py 导入 + __all__（若要公开）     │
           └────────────────────┬────────────────────────┘
                                ▼
           ┌─────────────────────────────────────────────┐
           │  ④ tests/test_data.py 补 test_foo            │
           └────────────────────┬────────────────────────┘
                                ▼
                  （可选）meson.build 登记新独立文件
                                ▼
                  （仓库外）建立 dataset-foo 托管数据
```

而「不用改」的部分（`data_fetcher`/`fetch_data`/`clear_cache`/`download_all`/fixture）之所以自动受益，**全部归功于「注册表驱动」**：这些代码在运行时才去读那三张表，表里有什么，它们就处理什么。

#### 4.1.3 源码精读

**① 地基：三张表。** 先看新增 `foo` 要往哪里填：

[_registry.py:L8-L12](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_registry.py#L8-L12) —— `registry`。新增数据集要在这里加一行 `"foo.dat": "<openssl sha256 算出的 64 位十六进制串>"`。文件顶部 [_registry.py:L6-L7](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_registry.py#L6-L7) 的注释明确告诉你哈希怎么算：`openssl sha256 <filename>`。

[_registry.py:L14-L18](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_registry.py#L14-L18) —— `registry_urls`。注意 URL 的**命名约定**：`https://raw.githubusercontent.com/scipy/dataset-<name>/main/<name>.dat`。新增 `foo` 就要先把数据传到一个叫 `scipy/dataset-foo` 的 GitHub 仓库，再在这里登记它的 raw 地址。这套 `dataset-<name>` 约定来自模块文档字符串 [__init__.py:L43-L49](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/__init__.py#L43-L49)。

[_registry.py:L20-L26](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_registry.py#L20-L26) —— `method_files_map`。新增要加 `"foo": ["foo.dat"]`。注意键是**方法名**（`foo`）、值是**文件名列表**（复习 u2-l2：用列表是为了预留「一个方法对应多个文件」的能力）。这一项是 `clear_cache` 能找到文件的唯一依据。

**② 引擎：为什么 `data_fetcher` 不用改。** 关键证据在这一段：

[_fetchers.py:L14-L26](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_fetchers.py#L14-L26) —— `data_fetcher = pooch.create(path=..., base_url=..., registry=registry, urls=registry_urls)`。这里把 `registry` 和 `registry_urls` **整张表**作为参数喂给 pooch。也就是说，pooch 这个单例在导入时就把表里**所有**条目都登记了。你往表里加 `foo.dat`，下一次导入时 `data_fetcher` 自然就认识 `foo.dat`——**不需要任何额外接线**。这正是「不用改」的根本原因。

同理，`fetch_data` 也是数据集无关的：

[_fetchers.py:L29-L39](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_fetchers.py#L29-L39) —— `fetch_data(dataset_name)` 只接受一个文件名字符串，调用 `data_fetcher.fetch(dataset_name, ...)` 返回本地路径。它根本不关心 `dataset_name` 是 `"ascent.dat"` 还是 `"foo.dat"`——传啥下啥。所以新数据集**复用**这个函数即可。

**③ 新函数该长什么样。** 最简洁的范本是 `ascent`（最小后处理）：

[_fetchers.py:L42-L80](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_fetchers.py#L42-L80) —— `ascent()` 的骨架就是**所有新 fetcher 函数的模板**：
1. 顶部戴 `@xp_capabilities(out_of_scope=True)`（第 42 行）；
2. `fname = fetch_data("ascent.dat")` 拿路径（第 76 行）；
3. 用标准库手段把 `fname` 解析成 ndarray（第 78-79 行的 `pickle.load`）；
4. `return` 这个数组。

新增 `foo()` 只需把第 2 步换成 `"foo.dat"`、第 3 步换成 `foo.dat` 对应格式的解析逻辑（参考 u2-l3 讲过的 pickle / bz2 / npz 三种手段）。复杂一点的 `face`（带 `gray` 参数与 reshape）和 `electrocardiogram`（带 ADC 换算）分别见 [_fetchers.py:L183-L225](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_fetchers.py#L183-L225) 与 [_fetchers.py:L83-L180](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_fetchers.py#L83-L180)，可作为「带参数 / 带数值换算」时的写法参考。

**④ 门面：公开导出。** 想让用户能 `datasets.foo()` 调用，必须在两处补上：

[__init__.py:L80-L85](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/__init__.py#L80-L85) —— 第 80 行的 `from ._fetchers import face, ascent, electrocardiogram` 要加上 `foo`；第 84-85 行的 `__all__` 列表也要加上 `'foo'`。**两者缺一不可**（u1-l1 讲过：`from ... import` 让名字进入命名空间，`__all__` 才是控制 `import *` 与文档工具识别的「公开合约」）。

**⑤ 为什么 `clear_cache` / `download_all` 不用改。** 这两段代码是「不用改」结论的活证据：

[_utils.py:L13-L16](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_utils.py#L13-L16) —— `_clear_cache` 默认用 `method_map = method_files_map`（第 15-16 行）。你只要在 `method_files_map` 里加了 `"foo": ["foo.dat"]`，`clear_cache([datasets.foo])` 就能自动定位并删除 `foo.dat`，**`_utils.py` 一行都不用动**。

[_download_all.py:L58-L61](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_download_all.py#L58-L61) —— `download_all` 用 `for dataset_name, dataset_hash in _registry.registry.items():` 遍历**整张** `registry`。新加的 `foo.dat` 会自动出现在迭代里，被 `pooch.retrieve` 下下来，**`_download_all.py` 一行都不用动**。

> 一句话点透：**「必改」的四处都是「数据相关」的（填表、写解析函数、公开名字、写测试）；「不用改」的五处都是「数据无关」的通用引擎。** 这正是注册表驱动架构的红利。

#### 4.1.4 代码实践

> 这是一个**可在本地跑通、不联网、不改源码**的 mock 实践。我们用一个**本地假数据文件**走通「注册 → fetch → 解析」的闭环，体会「填表 + 写函数」之后就自动接通的快感。
>
> 关键技巧来自 u2-l1 埋下的伏笔：`fetch_data(dataset_name, data_fetcher=data_fetcher)` 的第二个参数是**可注入**的（默认绑模块级单例，但能传自己的 pooch 实例）。我们就造一个「指向本地目录、文件已就位、哈希已登记」的假 `data_fetcher`，让 `fetch_data` 在缓存命中时**直接返回本地路径、不联网**。

1. **实践目标**：用真实的 `fetch_data` 函数 + 一个本地 pooch 实例，下载/解析一个完全本地化的 `dummy.dat`，验证「只要登记进 registry，引擎就自动工作」。

2. **操作步骤**：把下面这段**示例代码**存成一个独立脚本（例如 `/tmp/mock_dataset.py`）并运行。它**不修改任何 scipy 源码**：

   ```python
   # 示例代码：本地 mock 一个新数据集的获取闭环
   import os, pickle, tempfile
   import numpy as np
   import pooch
   from scipy.datasets._fetchers import fetch_data          # 复用真实引擎
   from scipy._lib._array_api import xp_capabilities         # 复用真实装饰器

   # —— ① 准备一个本地「假数据文件」，模拟 dataset-dummy 仓库里的 dummy.dat ——
   cache = tempfile.mkdtemp()
   dummy_arr = np.arange(12, dtype='uint8').reshape(3, 4)
   dummy_path = os.path.join(cache, "dummy.dat")
   with open(dummy_path, 'wb') as f:
       pickle.dump(dummy_arr.tolist(), f)                    # 仿 ascent 用 pickle 存

   # —— ② 算 SHA256，模拟 _registry.py 里要填的那一行 ——
   dummy_hash = pooch.file_hash(dummy_path)
   print("registry 应填：", f'"dummy.dat": "{dummy_hash}",')

   # —— ③ 造一个本地 data_fetcher：文件已就位 + 哈希已登记 ——
   #    base_url 在「缓存命中」时不会被用到，给个占位即可。
   local_fetcher = pooch.create(path=cache, base_url="",
                                registry={"dummy.dat": dummy_hash})

   # —— ④ 新数据集的加载函数：戴装饰器 + 复用 fetch_data ——
   @xp_capabilities(out_of_scope=True)
   def dummy():
       """A mock dataset for tutorial purposes."""
       fname = fetch_data("dummy.dat", data_fetcher=local_fetcher)  # 注入本地实例
       with open(fname, 'rb') as f:
           return np.array(pickle.load(f))

   # —— ⑤ 跑一遍 ——
   arr = dummy()
   print("shape:", arr.shape, "dtype:", arr.dtype)
   print("equal:", np.array_equal(arr, dummy_arr))
   print("docstring 末尾被注入的说明：\n", dummy.__doc__[-200:])
   ```

3. **需要观察的现象**：
   - 脚本**全程不发起任何网络请求**（因为 `dummy.dat` 已在本地、哈希匹配，pooch 直接命中缓存）。
   - 打印的 `registry 应填` 那一行，就是一个可以**直接粘进** [_registry.py:L8-L12](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_registry.py#L8-L12) 的 `registry` 条目。
   - `shape` 为 `(3, 4)`、`dtype` 为 `uint8`、`equal` 为 `True`——说明「取路径 → 解析」闭环成功。
   - `dummy.__doc__` 末尾多出一段「Array API Standard Support」说明（这是 `@xp_capabilities` 注入的，下一节详解）。

4. **预期结果**：你用**真实的 `fetch_data` 与真实的 `xp_capabilities` 装饰器**，在完全不碰源码、完全不联网的前提下，复现了「新增数据集」的核心闭环。这证明：引擎是通用的，新增数据集真的只是「填表 + 写解析函数」。

5. 如果本机没装 `pooch`，本实践无法运行（`fetch_data` 会抛 `ImportError`，复习 u3-l1）——此时「待本地验证」，但**脚本逻辑可以直接对照 [_fetchers.py:L29-L39](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_fetchers.py#L29-L39) 与 [_fetchers.py:L42-L80](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_fetchers.py#L42-L80) 推理出来**。

#### 4.1.5 小练习与答案

**练习 1**：如果你**只**在 `_registry.py` 的 `registry` 和 `registry_urls` 加了 `foo.dat`，却**忘了**在 `method_files_map` 加 `"foo"`，哪些功能会坏、哪些不坏？

> **参考答案**：`foo()` 本身能正常下载和解析（它只依赖 `registry`/`registry_urls` 经 `data_fetcher` 工作），`download_all()` 也能下到 `foo.dat`（它遍历 `registry`）。但 `clear_cache([datasets.foo])` 会**抛 `ValueError`**——因为 [_utils.py:L40-L44](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_utils.py#L40-L44) 在 `method_map` 里查不到 `"foo"` 这个方法名。这说明三张表是**各自独立被消费**的，缺一张只影响消费它的那一个功能。

**练习 2**：为什么新增数据集时**不需要**修改 `data_fetcher = pooch.create(...)` 这一行？

> **参考答案**：因为 [_fetchers.py:L24-L25](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_fetchers.py#L24-L25) 把 `registry=registry, urls=registry_urls` 作为**整张表的引用**传给了 `pooch.create`。你在表里加的条目，会在模块**下次导入**时自动被 pooch 单例识别。`data_fetcher` 是数据无关的通用装配，无需为任何具体数据集修改。

**练习 3**：新增的 `foo()` 函数为什么必须写在 `_fetchers.py` 里，而不是 `_registry.py` 里？

> **参考答案**：职责分离。`_registry.py` 是**纯数据**（三张 dict，无逻辑、无导入依赖，是所有人依赖的地基，u1-l2 讲过）；`_fetchers.py` 是**逻辑层**（导入 pooch、定义 `data_fetcher`/`fetch_data`/各数据集函数）。把解析函数放进 `_registry.py` 会引入 pooch 依赖，破坏它的「纯数据」地位，导致循环/层次依赖。

---

### 4.2 模块二：`@xp_capabilities(out_of_scope=True)` 与数组 API 范围标记

#### 4.2.1 概念说明

你在 `_fetchers.py` 里已经看到，三个数据集函数（`ascent`/`electrocardiogram`/`face`）头顶都戴着一顶一样的帽子：

[_fetchers.py:L42](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_fetchers.py#L42) `@xp_capabilities(out_of_scope=True)`（`electrocardiogram` 在 [L83](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_fetchers.py#L83)、`face` 在 [L183](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_fetchers.py#L183)）。`_utils.py` 的 `clear_cache` 和 `_download_all.py` 的 `download_all` 也各自戴了（[_utils.py:L60](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_utils.py#L60)、[_download_all.py:L26](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_download_all.py#L26)）。也就是说，`scipy.datasets` 的**全部五个公开函数**都戴了这顶帽子。

这顶帽子解决的问题是：SciPy 在推进「数组 API 标准兼容」时，有一套**合规测试机器**会自动检查每个函数是否能在 CuPy/PyTorch/JAX/Dask 等后端上跑。但 `scipy.datasets` 的函数**天生只返回 NumPy 数组**（数据是从磁盘 pickle/bz2/npz 里硬读出来的，u2-l3 讲过），根本没有「跨后端实现」可言。如果不加声明，合规机器会拿这些函数去别的后端上跑、然后报一堆「不支持」的失败——这些失败是**噪音**，不代表真正的 bug。

`out_of_scope=True` 就是一张贴在函数脑门上的声明：「**我主动退出数组 API 标准的跨后端适配，请不要拿我去测别的后端。**」

#### 4.2.2 核心流程

这个装饰器同时做三件事，可以用伪代码概括（对应实现见 4.2.3）：

```
def xp_capabilities(*, out_of_scope=False, ...):
    if out_of_scope:
        np_only = True                          # 效果①：等同于「只支持 NumPy」

    def decorator(f):
        capabilities_table[f] = {               # 效果②：登记到能力表
            "out_of_scope": True, ...
        }
        if not np_only or out_of_scope:         # 效果③：往文档字符串注入说明
            f.__doc__ += 一段「Array API Standard Support」note
        return f
    return decorator
```

三个效果逐一拆解：

1. **效果①——等价 `np_only=True`**：`out_of_scope` 会触发 `np_only = True`。这意味着在合规测试机器眼里，这个函数「只支持 NumPy 后端」。
2. **效果②——登记到 `capabilities_table`**：装饰器把函数对象 `f` 映射到它的能力字典，存进一张全局表 `capabilities_table`。合规测试机器读这张表来决定「这个函数要不要在某某后端上跑」。`out_of_scope=True` 的函数会被识别为「不参与」。
3. **效果③——注入文档说明**：装饰器会在函数的 docstring 末尾追加一段固定的「**Array API Standard Support**」说明文字，告诉人类读者「此函数不在跨后端适配范围内」。这是「声明」的**可见落地**——既给测试机器看，也给人看。

注意一个细节：在生成给 Sphinx 文档用的能力表时，`out_of_scope=True` 会**短路**掉整张后端能力表（不再逐个列举 numpy/cupy/torch/jax/dask 的支持情况），只返回 `{"out_of_scope": True}`——因为「整体退出」就不必再细分每个后端了。

#### 4.2.3 源码精读

装饰器本体在 `scipy/_lib/_array_api.py`（这是跨子模块共享的「数组 API」基础设施，不在 `datasets` 目录内）：

[_array_api.py:L839-L863](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_array_api.py#L839-L863) —— `xp_capabilities` 的签名。注意 `out_of_scope=False` 是众多关键字参数之一（第 849 行），默认 `False`。`datasets` 里的用法是把它显式设为 `True`。

**效果①**的实现：

[_array_api.py:L884-L885](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_array_api.py#L884-L885) —— `if out_of_scope: np_only = True`。这就是「退出范围 ≈ 只支持 NumPy」的代码落点。

**效果② + 效果③**的实现：

[_array_api.py:L922-L939](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_array_api.py#L922-L939) —— 内层 `decorator(f)`。第 925 行 `capabilities_table[f] = capabilities` 把函数登记进全局能力表（效果②）；第 927 行 `if not np_only or out_of_scope:` 决定要不要注入文档说明——注意这里的条件是 `not np_only or out_of_scope`，意味着即使是 `np_only` 函数，只要同时 `out_of_scope`，也**照样注入**说明（效果③）；第 928-929 行把生成的 note 追加到 docstring。

**短路效果**（生成 Sphinx 能力表时）：

[_array_api.py:L744-L745](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_array_api.py#L744-L745) —— `if out_of_scope: return {"out_of_scope": True}`。在 `_make_sphinx_capabilities` 里，`out_of_scope` 一旦为真就直接返回，跳过后面的逐后端能力枚举。

**注入的说明文字**：

[_array_api.py:L792-L804](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_array_api.py#L792-L804) —— `_make_capabilities_note` 在检测到 `"out_of_scope"` 时，生成的那段固定说明：`` `{fun_name}` is not in-scope for support of Python Array API Standard compatible backends other than NumPy. ``（第 799-800 行）。这就是你会在 `ascent.__doc__`、`face.__doc__` 末尾看到的那段英文。

> 把这条链路串起来：你在 [_fetchers.py:L42](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_fetchers.py#L42) 写下 `@xp_capabilities(out_of_scope=True)` → 装饰器在 [_array_api.py:L884-L885](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_array_api.py#L884-L885) 把它翻译成 `np_only=True` → 在 [L925](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_array_api.py#L925) 登记、[L928-L929](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_array_api.py#L928-L929) 注入文档说明。新增数据集函数时，**照抄这顶帽子即可**。

#### 4.2.4 代码实践

> 这是一个**观察型实践**：亲眼看装饰器给文档字符串注入了什么。

1. **实践目标**：验证 `@xp_capabilities(out_of_scope=True)` 确实往 `ascent` 的 docstring 里追加了一段「Array API Standard Support」说明。
2. **操作步骤**：在装了 SciPy 的环境里执行：

   ```python
   from scipy.datasets import ascent, face, electrocardiogram, clear_cache, download_all
   for f in [ascent, face, electrocardiogram, clear_cache, download_all]:
       has_note = "Array API Standard Support" in (f.__doc__ or "")
       print(f.__name__, "->", has_note)
   ```

3. **需要观察的现象**：五个函数全部打印 `-> True`，说明它们都被注入了同一段说明。
4. **预期结果**：你证实了「全部五个公开函数都戴了同一顶 `out_of_scope` 帽子」，并且装饰器的「效果③（注入文档）」确实生效。这段说明的文字正是 [_array_api.py:L796-L803](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_array_api.py#L796-L803) 那段。
5. 若本机 SciPy 版本较旧、装饰器尚未引入，「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `download_all` 和 `clear_cache` 这两个**不返回数组**的工具函数也要戴 `@xp_capabilities(out_of_scope=True)`？

> **参考答案**：因为合规测试机器是**按函数/符号**扫描的，它不在乎函数返回什么，只在乎「这个公开符号有没有声明自己的数组 API 能力」。`download_all` 返回 `None`、`clear_cache` 也返回 `None`（[_download_all.py:L27](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_download_all.py#L27)、[_utils.py:L61](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_utils.py#L61)），它们更不可能跨后端。给它们戴上帽子，是为了让合规机器**显式跳过**它们，避免误测误报。这是一种「宁可全员声明，不留漏网之鱼」的保守策略。

**练习 2**：装饰器里的 `out_of_scope` 和 `np_only` 是什么关系？

> **参考答案**：`out_of_scope` 是 `np_only` 的「**超集理由**」。[_array_api.py:L884-L885](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_array_api.py#L884-L885) 里 `out_of_scope` 会**强制**把 `np_only` 置为 `True`，但反过来不成立。`np_only=True` 只说「只支持 NumPy」；`out_of_scope=True` 额外说了「我整体退出跨后端适配范围」，并把文档说明、能力表都对应处理（[_array_api.py:L744-L745](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_array_api.py#L744-L745) 的短路、[L792-L804](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_array_api.py#L792-L804) 的说明）。对 `datasets` 而言，用 `out_of_scope=True` 比 `np_only=True` 更准确地表达了「数据集函数本就不在适配目标内」的语义。

**练习 3**：如果你新增的 `foo()` 忘了戴这顶帽子，会立刻报错吗？

> **参考答案**：**不会立刻报错**。`foo()` 照样能正常下载、解析、返回数组。装饰器**不改变运行时行为**（它只登记能力表、改写 docstring，[L922-L939](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_array_api.py#L922-L939) 不碰函数逻辑）。但后果是：合规测试机器会拿 `foo()` 去别的数组 API 后端上试跑，产生误报失败；同时文档里也缺少那段「不在范围内」的说明，对读者不透明。所以「不戴帽子」是**隐性技术债**，不是即时故障——这也是为什么本模块要给全部公开函数统一戴上。

---

### 4.3 模块三：meson 源文件与测试安装声明

#### 4.3.1 概念说明

前两个模块讲的都是「Python 代码层面」的改动。但一个数据集要真正能被用户用上，它的源文件还必须**被打包进 SciPy 的安装产物**里——这件事由构建系统 Meson 负责。

对 `scipy.datasets` 这种**纯 Python** 子模块，Meson 不需要编译任何东西，它的全部职责就是：**显式列出「哪些 `.py` 文件要被安装到用户机器上」**。这一声明写在一个叫 `meson.build` 的文件里，靠一个文件名列表 + 一个 `py3.install_sources(...)` 调用完成。

关键认知：Meson **不会自动**安装目录下所有 `.py` 文件——它只安装你**显式写进列表**的那些。所以「新增一个独立 `.py` 文件」时，必须手动把它加进列表，否则它会被构建系统无视、不会进入安装包。

#### 4.3.2 核心流程

`scipy/datasets/` 下有两个 `meson.build`，构成「主目录声明源文件 + 子目录递归」的结构：

```
scipy/datasets/meson.build            ← 声明 5 个 Python 源文件，并递归进 tests/
        │  python_sources = ['__init__.py', '_fetchers.py', '_registry.py',
        │                    '_download_all.py', '_utils.py']
        │  py3.install_sources(python_sources, subdir: 'scipy/datasets')
        │
        └── subdir('tests')   ────────►  scipy/datasets/tests/meson.build
                                            │  python_sources = ['__init__.py', 'test_data.py']
                                            │  py3.install_sources(..., install_tag: 'tests')
```

两个关键点：

1. **`python_sources` 列表是「白名单」**：只有列在里面的文件会被安装。新增独立文件必须加进来。
2. **测试文件用 `install_tag: 'tests'` 标记**：这让测试文件在安装时被打上「tests」标签，从而可以被单独排除（例如生产环境不想装测试时）。这是「主包源文件」与「测试源文件」在安装层面的区分。

#### 4.3.3 源码精读

**主目录的 meson.build：**

[meson.build:L1-L14](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/meson.build#L1-L14) —— 逐段看：
- 第 1-7 行：`python_sources` 列表，逐一列出 5 个 Python 源文件。这就是「白名单」本体。
- 第 9-12 行：`py3.install_sources(python_sources, subdir: 'scipy/datasets')`。`py3` 是 Meson 里 Python 安装相关的对象；`install_sources` 表示「把这些文件安装」；`subdir: 'scipy/datasets'` 指明安装到用户机器上的**相对子路径**（即 `<site-packages>/scipy/datasets/`）。
- 第 14 行：`subdir('tests')` 让 Meson **递归进入** `tests/` 子目录，去读那里的 `meson.build`。这是 Meson 组织子目录的标准手法。

**测试目录的 meson.build：**

[tests/meson.build:L1-L10](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/tests/meson.build#L1-L10) —— 结构与主目录几乎一致，差别有二：
- 第 1-4 行的 `python_sources` 只列 `__init__.py` 和 `test_data.py`（测试包的入口与测试本体）。
- 第 6-10 行的 `py3.install_sources` 多了一个 `install_tag: 'tests'`，把这一批文件标记为「测试」。`subdir` 同样是 `'scipy/datasets/tests'`。

> 把这俩文件和 4.1 的改动清单对照看：**新增数据集时，如果你只是往 `_fetchers.py` / `_registry.py` / `tests/test_data.py` 这些「已在列表里」的文件追加内容，meson 完全不用动**——因为安装的是文件名，文件内容变了照样装。**只有当你新建了一个独立的 `.py` 文件**（比如把数据集函数拆到一个新模块），才需要把新文件名加进对应的 `python_sources` 列表。

#### 4.3.4 代码实践

> 这是一个**源码阅读 + 推理型实践**，不需要构建 SciPy，只需要你判断「什么情况要改 meson」。

1. **实践目标**：能准确判断各种「新增数据集」场景下，是否需要修改 `meson.build`。
2. **操作步骤**：对下面三个场景，分别回答「要不要改 meson.build / tests/meson.build，改哪里」：
   - **场景 A**：新增 `foo()` 写进**现有的** `_fetchers.py`，测试写进**现有的** `tests/test_data.py`。
   - **场景 B**：新增 `foo()` 写进一个**新建的** `scipy/datasets/_foo.py`，并在 `__init__.py` 里导入它。
   - **场景 C**：为 `foo` 新增一个**独立的**测试文件 `tests/test_foo.py`。
3. **需要观察的现象**（对照 [meson.build:L1-L7](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/meson.build#L1-L7) 与 [tests/meson.build:L1-L4](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/tests/meson.build#L1-L4) 的白名单思考）。
4. **预期结果**：
   - **场景 A**：**两个 meson.build 都不用改**。`_fetchers.py` 与 `test_data.py` 本就在白名单里。
   - **场景 B**：**要改主 `meson.build`**——把 `'_foo.py'` 加进 [meson.build:L1-L7](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/meson.build#L1-L7) 的 `python_sources` 列表，否则 `_foo.py` 不会被安装，用户 `import` 时会 `ModuleNotFoundError`。
   - **场景 C**：**要改 `tests/meson.build`**——把 `'test_foo.py'` 加进 [tests/meson.build:L1-L4](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/tests/meson.build#L1-L4) 的 `python_sources`。
5. 如果你本机配置了 SciPy 的可编辑构建（`pip install -e --no-build-isolation .`），可以真的做场景 B 并观察 `import` 报错来验证——否则「待本地验证」，但**结论可由白名单机制直接推出**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 Meson 用「白名单」而不是「自动安装目录下所有 `.py`」？

> **参考答案**：白名单让维护者对「什么进安装包」有**完全、显式**的控制——可以排除草稿、实验文件、仅开发期用的脚本等。代价是新增独立文件时要手动登记（如本节场景 B/C），但这份手动成本换来的是发布物的**可预测性**。这是构建系统里很常见的设计取舍。

**练习 2**：`install_tag: 'tests'` 的实际作用是什么？为什么主目录的源文件**不加**这个 tag？

> **参考答案**：`install_tag: 'tests'` 给安装的文件打上「测试」标签，使下游工具（如打 wheel、做精简安装）能据此**把测试文件排除**出生产环境。主目录的 `_fetchers.py`/`_registry.py` 等是**运行时必需**的源文件——用户 `import scipy.datasets` 就要用它们，绝不能被排除，所以不加 `tests` tag。这是「运行时代码」与「测试代码」在打包层面的清晰分界。

**练习 3**：如果你新增了一个 `foo()` 数据集但忘了改任何 meson 文件，且 `foo()` 写在 `_fetchers.py` 里，用户能正常 `datasets.foo()` 吗？

> **参考答案**：**能**。因为 `_fetchers.py` 早就在 [meson.build:L3](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/meson.build#L3) 的白名单里，新增的函数只是文件**内容**的增加，文件本身照样被安装。这正是 4.1 改动清单里「新增到现有文件 → meson 不用改」的依据，也是为什么本模块鼓励「把新数据集函数加进现有 `_fetchers.py`」而不是另起炉灶。

---

## 5. 综合实践

把本讲三个模块串成一个**完整闭环**：在本地模拟新增一个叫 `dummy` 的数据集，覆盖**填表 → 写函数（戴帽子）→ 公开导出 → 补测试 → 判断 meson** 全流程。**全程不修改 scipy 源码**，而是把改动以「补丁/示例」形式呈现，并在一个独立脚本里验证核心闭环。

> 这是对前面所有讲义的综合检验。如果你能独立完成下面每一步并说清「为什么」，说明你已经真正掌握了 `scipy.datasets` 的架构。

**任务**：假设要新增 `dummy`，文件 `dummy.dat`（pickle 存的 `(3,4)` uint8 数组），返回 ndarray。

**步骤 1 —— 填表（对应 [_registry.py:L8-L26](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_registry.py#L8-L26)）**。写出要在三张表各加的「补丁」（示例代码，非项目原有代码）：

```python
# registry（哈希需用 openssl sha256 dummy.dat 实算，或 pooch.file_hash）
"dummy.dat": "<64 位 SHA256>",

# registry_urls（需先把数据上传到 github.com/scipy/dataset-dummy）
"dummy.dat": "https://raw.githubusercontent.com/scipy/dataset-dummy/main/dummy.dat",

# method_files_map
"dummy": ["dummy.dat"],
```

**步骤 2 —— 写函数（对应 [_fetchers.py:L42-L80](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_fetchers.py#L42-L80) 的 ascent 模板）**：

```python
# 示例代码：追加到 _fetchers.py
@xp_capabilities(out_of_scope=True)        # 别忘了这顶帽子（4.2）
def dummy():
    """
    A mock (3, 4) uint8 dataset for tutorial purposes.
    """
    import pickle
    fname = fetch_data("dummy.dat")        # 复用通用 fetch_data，无需改它
    with open(fname, 'rb') as f:
        return array(pickle.load(f))
```

**步骤 3 —— 公开导出（对应 [__init__.py:L80-L85](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/__init__.py#L80-L85)）**：

```python
# 示例代码：__init__.py 改两处
from ._fetchers import face, ascent, electrocardiogram, dummy   # 加 dummy
__all__ = ['ascent', 'electrocardiogram', 'face',
           'download_all', 'clear_cache', 'dummy']              # 加 'dummy'
```

**步骤 4 —— 补测试（对应 [tests/test_data.py:L38-L43](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/tests/test_data.py#L38-L43) 的 test_ascent）**：

```python
# 示例代码：追加到 TestDatasets 类内
def test_dummy(self):
    assert_equal(dummy().shape, (3, 4))
    assert _has_hash(os.path.join(data_dir, "dummy.dat"),
                     registry["dummy.dat"])
```

**步骤 5 —— 判断 meson**：因为步骤 2/4 都是往**现有文件**（`_fetchers.py` / `test_data.py`）里加内容，而这两个文件已在 [meson.build:L3](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/meson.build#L3) 与 [tests/meson.build:L3](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/tests/meson.build#L3) 的白名单里，所以 **meson 完全不用改**。

**步骤 6 —— 本地验证闭环**：运行 4.1.4 那段独立脚本，确认在不联网、不改源码的前提下，「填表 + 写函数」后的 `dummy()` 能返回正确的 `(3, 4)` uint8 数组，且 docstring 被注入了 Array API 说明。

**自检清单**（能答上来就算通关）：
- 为什么不用改 `data_fetcher` / `fetch_data` / `clear_cache` / `download_all` / autouse fixture？（答：注册表驱动，它们运行时读表，新条目自动生效。）
- `@xp_capabilities(out_of_scope=True)` 不戴会立刻坏吗？（答：不会，但是隐性技术债。）
- 什么情况下才需要改 meson？（答：新建独立 `.py` 文件时。）

---

## 6. 本讲小结

- **新增数据集 = 「填三张表 + 写一个解析函数 + 公开导出 + 一条测试」**，其余通用引擎（`data_fetcher`/`fetch_data`/`clear_cache`/`download_all`/autouse fixture）**自动受益、无需改动**——这是 `scipy.datasets` 「注册表驱动」架构的最大红利（4.1）。
- **必改清单**：`_registry.py` 的 `registry`/`registry_urls`/`method_files_map`（[_registry.py:L8-L26](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_registry.py#L8-L26)）、`_fetchers.py` 的新函数、`__init__.py` 的导入与 `__all__`（[__init__.py:L80-L85](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/__init__.py#L80-L85)）、`tests/test_data.py` 的一个断言。
- **`@xp_capabilities(out_of_scope=True)`** 是「主动退出数组 API 跨后端适配」的声明，有三大效果：置 `np_only=True`（[_array_api.py:L884-L885](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_array_api.py#L884-L885)）、登记能力表并短路 Sphinx 能力枚举（[L744-L745](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_array_api.py#L744-L745)）、往 docstring 注入说明（[L792-L804](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_array_api.py#L792-L804)）；模块全部五个公开函数都戴这顶帽子（4.2）。
- **meson 用白名单安装源文件**：`python_sources` 列表 + `py3.install_sources`（[meson.build:L1-L14](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/meson.build#L1-L14)）；测试文件额外用 `install_tag: 'tests'`（[tests/meson.build:L1-L10](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/tests/meson.build#L1-L10)）；往现有文件加内容不用动 meson，**新建独立 `.py` 才需登记**（4.3）。
- **职责分离的体现**：`_registry.py` 是纯数据地基（无逻辑无依赖），`_fetchers.py` 是逻辑层（含 pooch 依赖与解析函数），新增数据集必须尊重这一分层——解析函数写进 `_fetchers.py` 而非 `_registry.py`。
- **可注入参数的红利**：`fetch_data(dataset_name, data_fetcher=data_fetcher)` 的可注入第二个参数（[_fetchers.py:L29](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_fetchers.py#L29)）让我们能在不联网、不改源码的前提下 mock 整个获取闭环（4.1.4）。

---

## 7. 下一步学习建议

- **横向对比其他子模块**：本讲是 `scipy.datasets` 的收官，但「注册表驱动 + 通用引擎 + 可选依赖降级 + meson 白名单 + xp_capabilities 声明」这套组合在 SciPy 其他子模块（如带数据文件的示例、测试数据）里也常见。挑一个你感兴趣的子模块（如 `scipy/misc` 的历史渊源、或 `scipy/signal` 的滤波器设计），对比它的目录组织与 `meson.build`，体会共性与差异。
- **深入数组 API 适配**：本讲只讲了 `out_of_scope=True` 这一侧（「退出」）。想理解「积极参与」的另一侧，建议阅读 [_array_api.py](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_array_api.py) 里 `xp_capabilities` 的其他参数（`skip_backends`/`xfail_backends`/`cpu_only` 等）和 SciPy 开发文档中的 `dev-arrayapi` 章节，看一个「真正跨后端」的函数是如何声明自己能力的。
- **上手一个真实 PR**：如果你想在真实仓库里练手，可以在 `scipy/scipy` 的 issue 列表里找「good first issue」或数据集相关的增强请求，按本讲的改动清单提一个 PR——你会经历「建 `dataset-<name>` 仓库 → 算哈希 → 填三张表 → 写函数 → 补测试 → 过 CI」的完整流程，那是对本讲知识最好的巩固。
- **回顾整本手册**：作为最后一篇，建议你回头重读 u2-l2（注册表）与本讲 4.1，体会「数据驱动架构」如何在「极小的代码改动」与「清晰的可扩展性」之间取得平衡——这是 `scipy.datasets` 留给读者最有价值的设计启示。
