# 目录结构与各文件职责

> 本讲承接 [u1-l1 项目定位与对外 API](./u1-l1-project-overview.md)。
> 上一讲我们把 `__init__.py` 比作整个子模块的「门面」，并明确了五个公开函数的分工。
> 本讲不再讨论「门面长什么样」，而是拆开墙壁，带你逐一认识门面背后的每一个房间——也就是 `scipy/datasets/` 目录下的每一个文件，搞清楚它们各自承担什么职责、彼此如何连接。

## 1. 本讲目标

学完本讲，你应当能够：

- 说出 `scipy/datasets/` 目录下 5 个 Python 源文件各自的职责分工（`__init__` / `_fetchers` / `_registry` / `_utils` / `_download_all`）。
- 看懂 `meson.build` 与 `tests/meson.build` 如何声明「要被安装的 Python 源文件」，以及 `subdir('tests')` 如何把构建递归到子目录。
- 理解一个关键的组织原则：**公开函数全都定义在带下划线的私有模块里，却通过 `__init__.py` 被重新导出为公开 API**——并能解释这种「内部私有 + 门面公开」写法的好处。
- 画出这个子模块的目录结构与文件依赖关系图。

## 2. 前置知识

本讲用到几个 Python 与构建系统的基础概念，先用大白话过一遍：

- **模块（module）与包（package）**：一个 `.py` 文件就是一个模块；一个含有 `__init__.py` 的目录就是一个包。`scipy/datasets/` 目录加上里面的 `__init__.py`，就构成了 `scipy.datasets` 这个包。
- **下划线前缀的「私有」约定**：Python 没有真正的访问控制关键字，但社区有一个约定——以 `_` 开头的模块名（如 `_fetchers`）表示「这是内部实现细节，请勿从外部直接 `import`」。它不是强制的，但是一种强约定。
- **门面（facade）**：把内部多个模块汇总到一个对外的入口（`__init__.py`），对外只暴露一组稳定的公开函数，把内部组织结构的变动藏在门面背后。
- **相对导入**：包内部模块之间互相引用时，写成 `from ._fetchers import ...`，开头的 `.` 表示「当前包」。这样包被改名或整体迁移时不用改导入语句。
- **`__all__` 公开合约**：一个字符串列表，显式声明「从本包 `import *` 时只会带出哪些名字」，也是文档工具识别公开接口的依据。
- **Meson**：SciPy 使用的构建系统。`meson.build` 是它的配置文件，用一种类 Python 的语法描述「要构建/安装什么」。

如果你对上一讲建立的「门面 + `__all__` 公开合约 + 可选依赖 Pooch + registry」这几个概念还不熟，建议先回看 u1-l1，本讲会在它们的基础上继续展开。

## 3. 本讲源码地图

本讲涉及的关键文件如下（路径均相对于 `scipy/datasets/`）：

| 文件 | 角色 | 一句话职责 |
| --- | --- | --- |
| [`__init__.py`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/__init__.py) | 公开门面 | 用相对导入汇集私有模块的函数，用 `__all__` 声明公开 API |
| [`_fetchers.py`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_fetchers.py) | 获取层 | 定义 `ascent` / `face` / `electrocardiogram` 与下载助手 `fetch_data` |
| [`_registry.py`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_registry.py) | 注册表层 | 维护文件名→SHA256、文件名→远程 URL、方法名→文件列表三张映射表 |
| [`_utils.py`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_utils.py) | 工具层 | 提供公开的 `clear_cache` 与内部的 `_clear_cache` |
| [`_download_all.py`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_download_all.py) | 批量下载 | 提供 `download_all` 与可独立运行的命令行入口 `main()` |
| [`meson.build`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/meson.build) | 构建声明 | 声明需要安装的 Python 源文件，并递归进入 `tests` 子目录 |
| [`tests/meson.build`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/tests/meson.build) | 测试构建声明 | 声明测试用的 Python 源文件，并打上 `tests` 安装标签 |

整个子模块的目录结构如下：

```
scipy/datasets/
├── __init__.py          # 公开门面：汇集并导出公开 API
├── _fetchers.py         # 获取层：三个数据集函数 + fetch_data
├── _registry.py         # 注册表层：三张映射表（地基文件，被大家依赖）
├── _utils.py            # 工具层：clear_cache / _clear_cache
├── _download_all.py     # 批量下载：download_all + CLI main()
├── meson.build          # 构建声明：声明源文件、递归进 tests
└── tests/
    ├── __init__.py      # 空文件，把 tests 标记为一个 Python 包
    ├── meson.build      # 测试构建声明
    └── test_data.py     # 单元测试用例
```

仔细观察可以发现一条贯穿全篇的依赖线索：`_registry.py` 是「地基」，它不依赖本目录任何其他文件；而 `_fetchers.py`、`_utils.py`、`_download_all.py` 都从它那里取数据；最后 `__init__.py` 把三个带下划线的模块汇总成对外接口。这条线索在第 4 节会反复出现。

## 4. 核心概念与源码讲解

本讲把目录拆成 6 个最小模块来逐个认识。注意：本讲只讲「**每个文件负责什么、长什么样**」，至于 `fetch_data` 如何下载、SHA256 如何校验、缓存如何清理等**机制细节**，会有专门的后续讲义（u2 单元）深入，本讲只点到为止。

### 4.1 公开门面层：`__init__.py`

#### 4.1.1 概念说明

`__init__.py` 是整个包的入口，也是上一讲所说的「门面」。它自身几乎不含业务逻辑——既不下载文件，也不算哈希。它只做三件事：

1. 用相对导入把私有模块里的函数「请」进来。
2. 用 `__all__` 把这些函数登记为公开 API。
3. 顶部写一段长文档字符串，向用户与文档生成工具说明这个包是干什么的。

之所以把所有重活都放到下划线私有模块里，而让 `__init__` 保持「干净」，是为了让公开接口稳定：哪怕将来内部文件被拆分、改名、重写，只要 `__init__` 里导出的名字不变，用户的代码就不会坏。

#### 4.1.2 核心流程

`__init__.py` 在被 `import scipy.datasets` 时执行，流程非常直接：

```
1. 解释器执行模块顶部的大段文档字符串（仅作为文档，无副作用）
2. 执行三行相对导入 → 把 5 个函数引入当前命名空间
3. 执行 __all__ 赋值 → 登记公开合约
4. 末尾挂载一个 test() 函数（PytestTester，便于在交互环境跑测试）
```

#### 4.1.3 源码精读

先看门面最核心的「请人进来 + 登记造册」这几行：

[`__init__.py:L80-L85`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/__init__.py#L80-L85) —— 三行相对导入分别从 `_fetchers`（数据集方法）、`_download_all`（批量下载）、`_utils`（缓存清理）引入函数，紧接着 `__all__` 把它们登记为公开 API。注意三个导入目标**全部是带下划线的私有模块**，这正是「内部私有 + 门面公开」的体现。

文件末尾还有一段 SciPy 各子模块通用的测试挂钩：

[`__init__.py:L88-L90`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/__init__.py#L88-L90) —— 引入 `PytestTester`，把它绑定成包级别的 `test()`，随后 `del PytestTester` 删掉名字本身。这样用户在交互式环境里执行 `scipy.datasets.test()` 就能跑本包的测试，但 `PytestTester` 这个类名本身不会泄漏到包的公开命名空间。

文件开头那大段文字（第 1–77 行）是模块文档字符串，它在上一讲已经详细介绍过「数据如何获取与存储」，这里不再重复。

#### 4.1.4 代码实践

这是一个**可运行的内省实践**，能让你亲眼看到「公开函数其实住在私有模块里」。

1. **实践目标**：用 Python 内省验证每个公开函数的真实出处。
2. **操作步骤**：在装好 SciPy（建议同时装好 `pooch`，但不装也不影响本实践）的环境里运行：

   ```python
   # 示例代码
   import scipy.datasets as d

   print("公开 API (__all__):", d.__all__)
   print("---")
   for name in d.__all__:
       obj = getattr(d, name)
       print(f"{name:20s} 实际定义在 -> {obj.__module__}")
   ```

3. **需要观察的现象**：每个公开名字的 `__module__` 属性会指向一个带下划线的私有模块。
4. **预期结果**：大致会打印出

   ```
   公开 API (__all__): ['ascent', 'electrocardiogram', 'face', 'download_all', 'clear_cache']
   ---
   ascent               实际定义在 -> scipy.datasets._fetchers
   electrocardiogram    实际定义在 -> scipy.datasets._fetchers
   face                 实际定义在 -> scipy.datasets._fetchers
   download_all         实际定义在 -> scipy.datasets._download_all
   clear_cache          实际定义在 -> scipy.datasets._utils
   ```

   也就是说：`ascent/face/electrocardiogram` 三个数据集方法同住在 `_fetchers`；`download_all` 在 `_download_all`；`clear_cache` 在 `_utils`。`__init__` 只是「中转站」。
5. 如果无法确定运行结果，标注「待本地验证」——但本实践只依赖 SciPy 自身，结果稳定可复现。

#### 4.1.5 小练习与答案

- **练习 1**：如果把 `__init__.py` 里的 `__all__` 整段删掉，`import scipy.datasets as d; d.face()` 还能正常工作吗？`from scipy.datasets import *` 呢？
  - **答案**：`d.face()` 仍可用，因为相对导入已经把 `face` 引入了命名空间，`__all__` 只控制 `import *` 与文档识别，不影响显式 `getattr`。但 `from scipy.datasets import *` 的行为会改变——没有 `__all__` 时，`*` 会导入所有不以下划线开头的名字（行为更「脏」），这正是需要 `__all__` 的原因。
- **练习 2**：为什么 `PytestTester` 在被绑定成 `test` 之后还要 `del PytestTester`？
  - **答案**：为了让 `PytestTester` 这个类名本身不出现在 `scipy.datasets` 的命名空间里，保持门面干净——用户只需要 `test()` 这个动作，不需要看到这个工具类。

### 4.2 数据获取层：`_fetchers.py`

#### 4.2.1 概念说明

`_fetchers.py` 是整个子模块里**最重**的一个文件，承担「把远程文件取下来、解封装成 ndarray」的全部脏活。它定义了三个对外公开的数据集方法 `ascent`、`face`、`electrocardiogram`，以及一个内部下载助手 `fetch_data`，还维护了一个模块级的下载器单例 `data_fetcher`。

之所以单独抽出一个文件，是因为这三个数据集方法有大量共性逻辑（都要先下载、都要走 pooch），把共性逻辑集中在 `_fetchers` 里、再让 `__init__` 只挑出公开函数导出，是一种清晰的关注点分离。

#### 4.2.2 核心流程

`_fetchers.py` 在被导入时的流程：

```
1. 导入 numpy 工具与 _registry 里的两张表（registry / registry_urls）
2. 导入 xp_capabilities 装饰器（用于标记「不在数组 API 适配范围」）
3. try 导入 pooch：
     成功 → 用 pooch.create(...) 建一个模块级单例 data_fetcher
     失败 → pooch = None, data_fetcher = None（降级，用到时才报错）
4. 定义 fetch_data(dataset_name) 助手
5. 定义 ascent / electrocardiogram / face 三个数据集方法
```

#### 4.2.3 源码精读

文件头部的「可选依赖降级 + 单例创建」是这个文件的骨架：

[`_fetchers.py:L8-L26`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_fetchers.py#L8-L26) —— `try: import pooch` 配合 `except ImportError`，把缺失依赖的情况降级为 `pooch = None; data_fetcher = None`；导入成功时则用 `pooch.create(...)` 一次性配好缓存路径、`base_url`、registry 和 urls，生成一个模块级单例 `data_fetcher`。注意它从 `._registry` 取了 `registry` 与 `registry_urls` 两张表——这就是 4.1 末尾说的「大家依赖 `_registry` 这块地基」。

下载助手定义在这里：

[`_fetchers.py:L29-L39`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_fetchers.py#L29-L39) —— `fetch_data(dataset_name)` 先检查 `data_fetcher` 是否为 `None`（若否则抛出友好的 ImportError），再用带自定义 `User-Agent` 头的 `pooch.HTTPDownloader` 执行真正的下载，返回下载到本地缓存后的文件绝对路径。三个数据集函数都会先调用它拿到文件路径，再做各自的解封装。

三个数据集方法的定义边界（本讲只需看清「它们住在这里、且都披着同一个装饰器」，机制细节留给后续讲义）：

[`_fetchers.py:L42-L43`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_fetchers.py#L42-L43) —— `ascent` 的定义，注意它头上那行 `@xp_capabilities(out_of_scope=True)` 装饰器，标记此函数不在数组 API（Array API）适配范围内。`electrocardiogram`（第 83 行）与 `face`（第 183 行）也各自带着同一个装饰器，原因相同：它们返回的是固定的小型演示数据，不参与跨数组库的通用适配。

> 三个数据集函数各自如何解封装（pickle / bz2 / npz + ADC 换算）将在 **u2-l3 三种数据集的加载与转换** 中专门讲解，本讲只让你知道「它们都住在 `_fetchers.py`」。

#### 4.2.4 代码实践

这是一个**源码阅读型实践**，帮助你熟悉 `_fetchers.py` 的整体形状。

1. **实践目标**：在不细读函数体的前提下，快速定位文件里每个顶层定义的位置。
2. **操作步骤**：打开 [`_fetchers.py`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_fetchers.py)，用编辑器搜索以下关键字，记录各自所在行号：
   - `def fetch_data`
   - `def ascent`
   - `def electrocardiogram`
   - `def face`
   - `@xp_capabilities(out_of_scope=True)`
3. **需要观察的现象**：你会看到同一个装饰器 `@xp_capabilities(out_of_scope=True)` 出现了 3 次，分别压在三个数据集函数头上。
4. **预期结果**：`fetch_data` 在第 29 行；`ascent` 在第 42–43 行（装饰器 + def）；`electrocardiogram` 在第 83 行；`face` 在第 183 行；装饰器共出现 3 次。
5. 结论：`_fetchers.py` 把「下载」与「三个数据集」放在同一个文件，结构清晰、一目了然。

#### 4.2.5 小练习与答案

- **练习 1**：`_fetchers.py` 顶部 `try: import pooch except ImportError: ...` 的设计目的是什么？
  - **答案**：让 SciPy 在没有安装可选依赖 `pooch` 的环境里也能**成功导入** `scipy.datasets`（不会一 `import` 就崩）；真正的报错被推迟到用户真正调用数据集函数、走到 `fetch_data` 检查 `data_fetcher is None` 时才抛出。这就是 u1-l1 提到的「用到才报错」模式。
- **练习 2**：`data_fetcher` 是局部变量还是模块级单例？为什么要做成单例？
  - **答案**：它是模块级单例（定义在 import 时）。做成单例是为了让三个数据集函数共享同一份 pooch 配置（同一条缓存路径、同一张 registry），避免每次调用都重建配置对象。

### 4.3 数据注册表层：`_registry.py`

#### 4.3.1 概念说明

`_registry.py` 是整个子模块的「地基文件」——它不 import 本目录任何其他文件，反过来却被 `_fetchers`、`_utils`、`_download_all` 三个文件依赖。它本质上就是三张字典，把「数据文件」与它的「校验哈希」「远程地址」「归属的方法名」绑在一起。

把这三张表集中在一个独立文件里有两大好处：第一，新增一个数据集时只需改这一处（再加上写一个 fetcher 函数）；第二，`_download_all.py` 在当作独立脚本运行时也能直接拿到这些表，而不必拉起整个 SciPy（见 4.5）。

#### 4.3.2 核心流程

`_registry.py` 没有执行流程，它只**声明数据**。三张表的关系如下：

```
registry          : 文件名 ──> SHA256 哈希        （用于校验下载完整性）
registry_urls     : 文件名 ──> 远程下载地址        （告诉 pooch 去哪下）
method_files_map  : 方法名 ──> [文件名, ...]        （告诉 clear_cache 某方法对应哪些缓存文件）
```

其中 `registry` 与 `registry_urls` 用相同的「文件名」作键串联，`method_files_map` 则用「方法名」（即公开函数名）指向一个或多个文件名。

#### 4.3.3 源码精读

第一张表——文件名与 SHA256 哈希的映射：

[`_registry.py:L8-L12`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_registry.py#L8-L12) —— `registry` 字典把三个数据文件（`ascent.dat` / `ecg.dat` / `face.dat`）映射到各自的 SHA256 哈希值。文件顶部注释（第 6–7 行）还贴心地告诉维护者：生成哈希的命令是 `openssl sha256 <filename>`。pooch 下载完文件后会用这张表做完整性校验。

第二张表——文件名与远程地址的映射：

[`_registry.py:L14-L18`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_registry.py#L14-L18) —— `registry_urls` 把每个文件指向 `raw.githubusercontent.com/scipy/dataset-<name>/main/<file>` 这样的地址。注意它符合上一讲提到的命名约定：每个数据集住在独立的 `dataset-<name>` 仓库里。

第三张表——方法名与文件列表的映射：

[`_registry.py:L22-L26`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_registry.py#L22-L26) —— `method_files_map` 把公开方法名（`ascent` / `electrocardiogram` / `face`）映射到一个文件名列表。这张表主要服务 `_utils.py` 的缓存清理逻辑——给定一个方法名，就能查出要删哪些缓存文件。注意值是**列表**，意味着将来一个方法可以对应多个数据文件。

> 三张表如何被 pooch 用于校验、如何被 `clear_cache` 用于定位文件，将在 **u2-l2 注册表三件套与 SHA256 校验** 中深入。

#### 4.3.4 代码实践

1. **实践目标**：建立「方法名 ↔ 文件名 ↔ 仓库」三者的一一对应直觉。
2. **操作步骤**：在 Python 里直接 import 这张表（它不依赖 pooch，一定能导入）：

   ```python
   # 示例代码
   from scipy.datasets._registry import registry, registry_urls, method_files_map

   for method, files in method_files_map.items():
       fname = files[0]
       print(f"方法 {method:20s} -> 文件 {fname:12s} "
             f"-> 仓库 dataset-{fname.split('.')[0]} "
             f"-> 哈希前 8 位 {registry[fname][:8]}")
   ```

3. **需要观察的现象**：方法名、文件名、仓库名、哈希一一对应，且仓库名就是文件名去掉扩展名。
4. **预期结果**：打印类似

   ```
   方法 ascent               -> 文件 ascent.dat  -> 仓库 dataset-ascent  -> 哈希前 8 位 03ce124c
   方法 electrocardiogram    -> 文件 ecg.dat     -> 仓库 dataset-ecg     -> 哈希前 8 位 f20ad336
   方法 face                 -> 文件 face.dat    -> 仓库 dataset-face    -> 哈希前 8 位 9d8b0b4d
   ```
5. 注意：从外部直接 import `_registry` 这种带下划线的私有模块，**仅用于学习观察**，正式代码应当走公开 API。

#### 4.3.5 小练习与答案

- **练习 1**：为什么 `method_files_map` 的值是列表（如 `["ascent.dat"]`）而不是单个字符串？
  - **答案**：为了预留「一个方法对应多个数据文件」的能力。当前每个方法恰好只对应一个文件，但列表结构让将来扩展（比如一个方法加载训练集+测试集两个文件）无需改数据结构。
- **练习 2**：`_registry.py` 依赖本目录的哪些其他文件？
  - **答案**：一个都不依赖。它是「地基」文件，只包含纯数据声明，因此谁都能安全地 import 它——这正是 `_download_all.py` 能在脱离完整 SciPy 构建的情况下独立运行的关键（见 4.5）。

### 4.4 缓存清理工具：`_utils.py`

#### 4.4.1 概念说明

`_utils.py` 专门负责「善后」——把缓存目录里的数据文件清掉。它对外暴露一个公开函数 `clear_cache`，内部把所有分支逻辑放在 `_clear_cache` 里。和 `_fetchers` 一样，它也采用「可选依赖降级」模式，只不过它降级的是 `platformdirs`（pooch 的一个依赖，用于跨平台定位缓存目录）。

#### 4.4.2 核心流程

`clear_cache(datasets=None)` 的对外语义：

```
datasets=None            -> 清空整个缓存目录（rmtree）
datasets=<单个 callable>  -> 只清这一个方法对应的缓存文件
datasets=[f1, f2, ...]   -> 清多个方法对应的缓存文件
```

内部 `_clear_cache` 会借助 `_registry` 里的 `method_files_map` 把方法名翻译成缓存文件路径，再做删除。

#### 4.4.3 源码精读

文件顶部的降级处理：

[`_utils.py:L7-L10`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_utils.py#L7-L10) —— `try: import platformdirs except ImportError: platformdirs = None`，与 `_fetchers` 里对 `pooch` 的降级手法完全一致。注意注释里特别说明：「platformdirs is pooch dependency」——也就是说装了 pooch 通常就会带上 platformdirs。

对外公开函数与内部实现：

[`_utils.py:L60-L80`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_utils.py#L60-L80) —— `clear_cache(datasets=None)` 是对外公开入口（同样披着 `@xp_capabilities(out_of_scope=True)`），它的函数体只有一行 `_clear_cache(datasets)`，把所有脏活委托给内部函数。这种「公开薄壳 + 内部实现」的写法让公开签名保持简单稳定。

内部实现定义在这里：

[`_utils.py:L13-L57`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_utils.py#L13-L57) —— `_clear_cache(datasets, cache_dir=None, method_map=None)` 是真正的实现。它接受两个可注入的参数（`cache_dir` 与 `method_map`），默认值分别来自 `platformdirs.user_cache_dir("scipy-data")` 和 `method_files_map`。这种「默认值指向真实路径/真实表，但可被测试替换」的设计，正是 `tests/test_data.py` 能离线测试缓存清理的关键（详见 u3-l3 测试设计剖析）。

> `_clear_cache` 的三条分支（None / 单个 / 多个）以及 platformdirs 兜底逻辑，将在 **u2-l4 缓存清理机制** 中逐行讲解。

#### 4.4.4 代码实践

1. **实践目标**：通过阅读 `_clear_cache` 的签名，理解它为什么留了两个「可注入」参数。
2. **操作步骤**：阅读 [`_utils.py:L13-L24`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_utils.py#L13-L24)，回答：当 `cache_dir` 和 `method_map` 都不给时，它们分别回退到什么默认值？
3. **需要观察的现象**：你能看到两个参数各自走 `if ... is None:` 分支后回退到默认实现。
4. **预期结果**：`method_map` 默认回退到 `method_files_map`（来自 `_registry`）；`cache_dir` 默认回退到 `platformdirs.user_cache_dir("scipy-data")`，且当 `platformdirs is None` 时抛 ImportError。
5. 思考题（不必运行）：如果测试想在不碰真实缓存目录的前提下验证 `_clear_cache`，它会传什么？——答案：传一个临时的 `cache_dir`（如 pytest 的 `tmp_path`）和一个自定义的 `method_map`。

#### 4.4.5 小练习与答案

- **练习 1**：`clear_cache` 和 `_clear_cache` 谁是公开的、谁是私有的？为什么这样分？
  - **答案**：`clear_cache`（不带下划线）是公开的，签名简单（只有一个 `datasets` 参数）；`_clear_cache`（带下划线）是内部实现，多了 `cache_dir` 和 `method_map` 两个用于测试注入的参数。这样公开接口保持简洁，内部细节与可测性需求都被藏在私有函数里。
- **练习 2**：`_utils.py` 为什么降级的是 `platformdirs` 而不是 `pooch`？
  - **答案**：因为缓存清理这一步只需要「定位缓存目录」的能力，而这件事由 `platformdirs` 提供；它不需要 pooch 的下载能力。`platformdirs` 恰好是 pooch 的依赖，所以「装了 pooch 就大概率有 platformdirs」，但代码仍按需独立降级，互不耦合。

### 4.5 批量下载工具：`_download_all.py`

#### 4.5.1 概念说明

`_download_all.py` 有两个与众不同之处：

1. 它的 `download_all(path)` 不像三个数据集方法那样「下载并返回 ndarray」，而是「把所有数据文件原样下载到指定目录」，主要服务测试前置与离线预填充缓存。
2. 它**既能作为模块被 import，也能作为独立脚本被 `python _download_all.py <dir>` 直接运行**——文件顶部那行注释 `Run: python _download_all.py <download_dir>` 和 `This doesn't require a full scipy build.` 就是这个意思。

#### 4.5.2 核心流程

```
作为模块 import 时：  __spec__.parent 非空 -> 走相对导入 from . import _registry
作为脚本直接运行时：  __spec__.parent 为 None/空 -> 走绝对导入 import _registry
随后定义 download_all(path)：遍历 _registry.registry，用 pooch.retrieve 逐个下载
最后定义 main()：用 argparse 解析命令行参数，调用 download_all(args.path)
```

#### 4.5.3 源码精读

文件顶部说明用途与运行方式：

[`_download_all.py:L1-L7`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_download_all.py#L1-L7) —— 模块文档字符串明确写了「平台无关、下载全部数据文件、不需要完整 scipy 构建、运行方式 `python _download_all.py <download_dir>`」。这解释了为什么它要能脱离完整构建独立运行——因为它常常被 CI 用来预下载测试数据。

用来区分「脚本 vs 模块」的小技巧：

[`_download_all.py:L18-L23`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_download_all.py#L18-L23) —— 通过判断 `__spec__.parent` 是否为 `None`/空串来选择导入方式：当作为包内模块运行时 `__spec__.parent` 指向父包，走相对导入 `from . import _registry`；当作为顶层脚本直接运行时 `__spec__.parent` 为 `None`，走绝对导入 `import _registry`。这样同一段代码在两种运行方式下都能正确拿到 `_registry`。

批量下载实现与命令行入口：

[`_download_all.py:L26-L61`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_download_all.py#L26-L61) —— `download_all(path)` 遍历 `_registry.registry` 的每一项，用 `pooch.retrieve(...)` 下载到 `path`（默认是系统缓存目录）。

[`_download_all.py:L64-L74`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_download_all.py#L64-L74) —— `main()` 用 `argparse` 解析一个可选的位置参数 `path`（默认仍是 `pooch.os_cache('scipy-data')`），再调用 `download_all(args.path)`；`if __name__ == "__main__": main()` 守卫让它能被 `python _download_all.py` 直接驱动。

> `download_all` 的批量下载细节与 `__spec__.parent` 双导入技巧将在 **u3-l2 download_all 与命令行脚本** 中深入剖析。

#### 4.5.4 代码实践

1. **实践目标**：理解同一个文件为什么能有「模块」与「脚本」两种身份。
2. **操作步骤**：阅读 [`_download_all.py:L18-L23`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_download_all.py#L18-L23) 与文件顶部文档字符串，然后在你的项目里（**不必真的运行**）设想两种调用：
   - 作为模块：`from scipy.datasets import download_all`，此时走的是哪一支？
   - 作为脚本：`python _download_all.py ./data`，此时走的是哪一支？
3. **需要观察的现象**：两段代码只是导入 `_registry` 的写法不同（相对 vs 绝对），其余逻辑完全共用。
4. **预期结果**：模块身份走 `from . import _registry`（else 分支）；脚本身份走 `import _registry`（if 分支）。这种「一份代码、两种入口」的设计省去了维护两份下载脚本。
5. 如果你想真的运行它，需要先 `pip install pooch`，再执行 `python -m scipy.datasets._download_all ./data`（模块方式）或把文件复制到可执行路径后 `python _download_all.py ./data`（脚本方式）；无法确定运行环境时标注「待本地验证」。

#### 4.5.5 小练习与答案

- **练习 1**：`download_all` 下载到哪里？返回什么？
  - **答案**：下载到 `path` 指定的目录，`path=None` 时默认下载到 `pooch.os_cache('scipy-data')`。它**不返回 ndarray**，只负责把原始文件落到磁盘——这是它和 `ascent/face/electrocardiogram` 的根本区别。
- **练习 2**：为什么 `_download_all.py` 要用 `__spec__.parent` 做条件判断，而不是直接写死相对导入？
  - **答案**：因为它要兼顾「作为包内模块 import」和「作为顶层脚本直接 `python` 运行」两种身份。直接运行时不存在「父包」，相对导入 `from . import ...` 会失败，必须退化为绝对导入。`__spec__.parent` 正是 Python 用来区分这两种身份的官方依据。

### 4.6 构建声明：`meson.build` 与 `tests/meson.build`

#### 4.6.1 概念说明

前面 5 个 `.py` 文件是「源码」，但 SciPy 用 Meson 构建，源码文件并不会自动被安装到用户的 site-packages——必须有人在 `meson.build` 里**显式声明**「这些文件需要安装」。`scipy/datasets/` 下有两个 `meson.build`：一个声明主源码，一个（在 `tests/` 下）声明测试源码。

#### 4.6.2 核心流程

```
scipy/datasets/meson.build:
  1. 用 python_sources 列表列出 5 个要安装的源文件
  2. 调用 py3.install_sources(...) 把它们安装到 subdir 'scipy/datasets'
  3. subdir('tests') -> 递归去读 tests/meson.build

tests/meson.build:
  1. 列出测试源文件 (__init__.py + test_data.py)
  2. 安装到 subdir 'scipy/datasets/tests'，并打上 install_tag: 'tests'
```

#### 4.6.3 源码精读

主构建文件——列出 5 个源文件并安装，再递归进 tests：

[`meson.build:L1-L14`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/meson.build#L1-L14) —— `python_sources` 列表依次是 `__init__.py`、`_fetchers.py`、`_registry.py`、`_download_all.py`、`_utils.py`（注意它和 4.1–4.5 讲的五个文件**一一对应**，缺一不可，否则装出来的包会少文件）。`py3.install_sources(python_sources, subdir: 'scipy/datasets')` 把它们安装到目标包目录；最后的 `subdir('tests')` 让 Meson 递归进入 `tests/` 子目录继续处理那里的 `meson.build`。

测试构建文件——同样的写法，但多了一个 `install_tag`：

[`tests/meson.build:L1-L10`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/tests/meson.build#L1-L10) —— 列出 `__init__.py`（空文件，仅用于把 tests 标记为包）和 `test_data.py`，安装到 `scipy/datasets/tests`，并打上 `install_tag: 'tests'`。这个标签让发行版打包者可以选择「只装 SciPy 不装测试」，从而减小发行体积——这也是为什么测试文件要单独声明，而不是混在主 `meson.build` 里。

#### 4.6.4 代码实践

1. **实践目标**：体会「源码文件必须在 `meson.build` 里登记才会被安装」。
2. **操作步骤**：对照阅读 [`meson.build:L1-L7`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/meson.build#L1-L7) 的 `python_sources` 列表与本讲 4.1–4.5 介绍过的 5 个源文件。
3. **需要观察的现象**：列表里的 5 个文件名与本讲讲过的 5 个文件**完全一致、顺序也一致**。
4. **预期结果**：你能得出结论——如果将来新增一个源文件（例如 `_new_dataset.py`），就必须同时把它加进这个 `python_sources` 列表，否则用户 `pip install` 后该文件不会出现在安装目录里。这正是 u3-l4「扩展实战：新增一个数据集」需要改动的集成点之一。
5. 这是纯阅读实践，不需要运行 Meson。

#### 4.6.5 小练习与答案

- **练习 1**：为什么 `tests/` 下的 `meson.build` 要单独写一个，而不是把测试文件直接列在主 `meson.build` 里？
  - **答案**：为了给测试文件打上 `install_tag: 'tests'`，让打包者能按需剥离测试、减小发行体积；同时通过 `subdir('tests')` 递归，保持目录结构与构建声明结构一致，便于维护。
- **练习 2**：`tests/__init__.py` 是个 0 字节的空文件，为什么也要被列进 `tests/meson.build` 的 `python_sources`？
  - **答案**：因为它把 `tests/` 目录标记为一个 Python 包，让 `scipy.datasets.test()` 能通过 PytestTester 正确发现并运行 `test_data.py` 里的用例。文件虽空，作用不可或缺。

## 5. 综合实践

把本讲所有内容串起来，完成下面这个「画图 + 解释」的小任务：

**任务**：为本子模块画一张**目录结构与依赖关系图**，并解释「公开函数为什么住在私有模块里」。

### 步骤

1. **画目录结构图**：照搬第 3 节的目录树，但在每个文件后面用你自己的话**补一句职责说明**（不要照抄本讲表格，用自己的语言重写）。

2. **画文件依赖关系图**（用箭头表示「A 依赖 B」）。正确答案应当形如：

   ```
   __init__.py ──imports──> _fetchers.py ──imports──> _registry.py
                ──imports──> _download_all.py ──imports──> _registry.py
                ──imports──> _utils.py ──imports──> _registry.py
   ```

   关键结论：`_registry.py` 是被三方依赖的「地基」，自身不依赖任何人。

3. **用一段话解释**：为什么 `ascent / face / electrocardiogram / download_all / clear_cache` 这五个公开函数全都定义在带下划线的私有模块（`_fetchers` / `_download_all` / `_utils`）里，却能在 `__init__` 中被当作公开 API 使用？请至少包含以下三点：
   - 下划线前缀表达了什么约定？（内部实现细节，不对外稳定承诺）
   - `__init__.py` 通过哪两个动作把它们「公开化」？（相对导入 + `__all__` 登记）
   - 这种「内部私有 + 门面公开」分离带来了什么好处？（公开接口稳定、内部可自由重构、可选依赖可降级、便于测试注入）

4. **用一个可运行命令验证你的解释**：运行 4.1.4 的内省脚本，把 `obj.__module__` 的输出贴在你的作业里，作为「公开函数住在私有模块」的证据。

### 预期结果

- 一张带职责注释的目录结构图。
- 一张显示 `_registry.py` 为地基的依赖关系图。
- 一段涵盖上述三点的解释。
- 一段 `obj.__module__` 的实际输出，证明五个公开函数确实分别来自 `_fetchers` / `_download_all` / `_utils`。

## 6. 本讲小结

- `scipy/datasets/` 由 5 个 Python 源文件 + 2 个 `meson.build`（含 `tests/`）组成，体量小、结构清晰。
- `_registry.py` 是「地基」：它只含三张字典（`registry` / `registry_urls` / `method_files_map`），不依赖本目录任何文件，却被其余三个文件共同依赖。
- `_fetchers.py` 是最重的文件：含下载助手 `fetch_data`、模块级单例 `data_fetcher` 与三个公开数据集方法。
- `_utils.py` 负责缓存清理：公开的 `clear_cache` 是薄壳，真正的分支逻辑在内部 `_clear_cache`，且留有 `cache_dir` / `method_map` 两个可测试注入的参数。
- `_download_all.py` 比较特殊：既能作为模块 import，也能作为独立脚本运行，靠 `__spec__.parent` 区分两种身份。
- `meson.build` 必须显式登记每个源文件才会被安装；`tests/meson.build` 额外用 `install_tag: 'tests'` 让测试可被按需剥离。新增文件时这两处都要同步。
- 贯穿全篇的组织原则：**公开函数定义在带下划线的私有模块里，再由 `__init__.py` 通过相对导入 + `__all__` 公开化**——这让公开接口稳定、内部可自由重构。

## 7. 下一步学习建议

本讲只让你认识了「每个文件长什么样、负责什么」。接下来建议：

- **动手运行**：先做 [u1-l3 第一次运行与缓存初探](./u1-l3-first-run-and-cache.md)，亲手调用 `ascent/face/electrocardiogram`，观察联网下载与本地缓存命中，建立感性认识。
- **深入机制**：进入 u2 单元，按真实调用链逐层拆解：
  - **u2-l1** 深入 `data_fetcher` 与 `fetch_data`（本讲 4.2 提到的下载单例与助手）。
  - **u2-l2** 三张注册表与 SHA256 校验（本讲 4.3 的三张字典如何被使用）。
  - **u2-l3** 三种数据集各自的加载与转换（pickle / bz2 / npz+ADC）。
  - **u2-l4** 缓存清理机制（本讲 4.4 的 `_clear_cache` 三条分支）。
- **专家视角**：u3 单元会讲可选依赖降级模式、`download_all` 与 CLI 脚本技巧、测试设计剖析，并以「新增一个数据集」综合实战收尾——届时你会再次回到 `meson.build` 与 `_registry.py`，把它们当作扩展点来改。
