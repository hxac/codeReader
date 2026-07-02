# download_all 与命令行脚本

> 本讲是「专家层」的第二篇。请确认你已学完 **u2-l2（注册表三件套）**，知道 `_registry.py` 里 `registry` / `registry_urls` / `method_files_map` 三张表分别是什么。本讲会站在它们之上，看一个"批量消费"这些表的工具脚本。

---

## 1. 本讲目标

学完本讲，你应当能够：

1. 说出 `download_all(path)` 是如何**遍历注册表**、用 `pooch.retrieve` 把所有数据文件一次性下载到指定目录的，并把它和单个数据集函数（`ascent` / `face` / `electrocardiogram`）的下载方式做对比。
2. 读懂 `if __spec__.parent is None or __spec__.parent == '':` 这一行**双导入分支**的原理，理解它为什么能让同一个 `.py` 文件既能当模块 `import`、又能当独立脚本 `python xxx.py` 运行。
3. 看懂 `main()` 用 `argparse` 暴露出的命令行入口，并能亲手把 `_download_all.py` 当脚本跑起来，把三个数据文件下到任意目录。

---

## 2. 前置知识

本讲要用到下面几个概念，先用大白话过一遍：

- **模块（module） vs 脚本（script）**：同一个 `.py` 文件，被 `import` 进来时叫"模块"，被 `python 文件名.py` 直接运行时叫"脚本"。这两者看到的"自己是谁"并不相同，导入写法也可能不同——这是本讲的一个核心难点。
- **`pooch.retrieve`**：pooch 提供的一个**无状态函数**，你给它一个 `url`、一个 `known_hash`、一个文件名 `fname` 和目标目录 `path`，它就把文件下载下来、校验哈希、放到指定位置。可以理解为"一次性的下载工"。
- **`pooch.create` / `Pooch.fetch`**（复习 u2-l1）：先 `pooch.create(...)` 配置好一个"知道整张注册表"的实例对象 `data_fetcher`，之后只需 `data_fetcher.fetch(文件名)` 就能按名取文件。这是**有状态**的方式。
- **`__spec__`**：Python 的 `importlib` 在加载一个模块时，会给它绑定一个 `ModuleSpec` 对象，存在模块的 `__spec__` 属性里。它像模块的"身份证"，记录了模块的完全限定名、父包是谁、从哪个文件加载等元信息。`__spec__.parent` 就是"父包的名字"。
- **`argparse`**：Python 标准库里写命令行工具的常用模块，用它可以方便地定义"接受哪些参数、默认值是什么、帮助文本是什么"。

---

## 3. 本讲源码地图

本讲只涉及两个文件，但它们的角色差别很大：

| 文件 | 在本讲里的角色 |
|---|---|
| `scipy/datasets/_download_all.py` | **主角**。整篇讲义都在剖析它：批量下载函数 `download_all`、双导入技巧、命令行入口 `main`。 |
| `scipy/datasets/_registry.py` | **数据源**。`download_all` 遍历的 `registry`（文件名→SHA256）和 `registry_urls`（文件名→下载地址）都在这里。 |

辅助参考（不在本讲精读范围，但会拿来对比）：

- `scipy/datasets/_fetchers.py`：里面有"按需单个下载"的 `fetch_data` 和 `data_fetcher`，本讲会拿它和 `download_all` 做对照。
- `scipy/datasets/tests/test_data.py`：里面用一个 `autouse` 的 module 级 fixture 调用了 `download_all()`，是理解"为什么需要批量下载"的最佳现实例子。

---

## 4. 核心概念与源码讲解

### 4.1 模块一：`download_all(path)` 批量下载

#### 4.1.1 概念说明

前面几讲里，你每次想用数据，都是**单独调用**某个数据集函数：`ascent()` 会下载 `ascent.dat`、`face()` 会下载 `face.dat`……这是"**按需、单个、走到固定缓存目录**"的下载方式（由 `_fetchers.py` 里的 `fetch_data` + 模块级单例 `data_fetcher` 完成，详见 u2-l1）。

但有些场景你需要**一次性把所有数据文件都拉下来**：

- **CI / 测试机预热**：测试机上网络不稳定，希望提前把所有数据下好，真正跑测试时就全是本地缓存命中，不再联网。`tests/test_data.py` 里的 module 级 fixture 干的就是这件事。
- **离线环境打包**：要把数据文件预置进一个镜像或某台无网机器的目录里。

`download_all(path)` 就是为此而生：它**遍历整张注册表**，把每一个已知的数据文件都下载到**你指定的目录**（不一定是缓存目录，可以是 `.`、`/tmp/dl` 等任意路径）。它的返回值是 `None`——它只负责"搬运文件"这个副作用，不读取、不解析文件内容。

#### 4.1.2 核心流程

`download_all(path)` 的执行流程可以概括成四步：

1. **降级检查**：如果可选依赖 `pooch` 没装，立刻抛出友好的 `ImportError`（与 `fetch_data` 文案一致）。
2. **确定目标目录**：`path` 为 `None` 时，默认用 `pooch.os_cache('scipy-data')`（即各平台的缓存目录）；否则用你传进来的目录。
3. **构造下载器**：创建一个带自定义 `User-Agent` 头的 `pooch.HTTPDownloader`，避免被服务器当成默认 `Python-urllib` 拦截（背景见 u2-l1 提到的 issue #21879）。
4. **遍历注册表逐个下载**：对 `_registry.registry` 里的每一对 `(文件名, SHA256)`，从 `_registry.registry_urls` 取出该文件的真实 URL，调用 `pooch.retrieve(...)` 下载、校验、落盘。

伪代码：

```
def download_all(path=None):
    if pooch is None:
        raise ImportError(...)          # 1. 降级检查
    if path is None:
        path = pooch.os_cache('scipy-data')   # 2. 默认目录
    downloader = pooch.HTTPDownloader(headers={"User-Agent": "SciPy"})  # 3. 下载器
    for 文件名, 哈希 in _registry.registry.items():                       # 4. 遍历
        pooch.retrieve(
            url=_registry.registry_urls[文件名],
            known_hash=哈希,
            fname=文件名,
            path=path,
            downloader=downloader,
        )
```

#### 4.1.3 源码精读

先看函数签名与降级检查。注意它被 `@xp_capabilities(out_of_scope=True)` 装饰，标记该函数**不在数组 API（Array API）标准化的范围内**——因为它根本不返回数组（返回 `None`），所以无所谓"是否兼容数组 API"。

[scipy/datasets/_download_all.py:26-27](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_download_all.py#L26-L27)：装饰器与函数定义，`download_all(path=None)` 的 `path` 默认为 `None`。

[scipy/datasets/_download_all.py:50-55](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_download_all.py#L50-L55)：这段同时做了"降级检查"和"确定默认目录"两件事。`pooch is None` 时抛出与 `fetch_data` **逐字相同**的 `ImportError`（这是有意为之的统一文案，详见 u3-l1）；`path is None` 时回退到 `pooch.os_cache('scipy-data')`。

[scipy/datasets/_download_all.py:57](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_download_all.py#L57)：构造下载器。注意这里的 `User-Agent` 是字符串 `"SciPy"`（**不带版本号**）；而 `_fetchers.py` 里 `fetch_data` 用的是 `f"SciPy {版本号}"`（**带版本号**）。这是两处实现的一个细微不一致，值得留意。

[scipy/datasets/_download_all.py:58-61](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_download_all.py#L58-L61)：**本函数的核心**。`for dataset_name, dataset_hash in _registry.registry.items()` 遍历注册表的每一项，再用 `pooch.retrieve(...)` 下载。这里用的是 **`pooch.retrieve`（无状态函数）**，而不是 `_fetchers.py` 里的 **`data_fetcher.fetch`（有状态实例方法）**——后者的对比见 4.1.4 的代码实践。

它消费的两张表都来自注册表文件：

[scipy/datasets/_registry.py:8-12](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_registry.py#L8-L12)：`registry`，文件名→SHA256。`download_all` 遍历的就是这张表的键值对。

[scipy/datasets/_registry.py:14-18](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_registry.py#L14-L18)：`registry_urls`，文件名→下载地址。`download_all` 用 `registry_urls[dataset_name]` 取每个文件的真实 URL。

#### 4.1.4 代码实践

**实践目标**：把三个数据文件批量下载到一个**自选目录**（不是缓存目录），并对比 `download_all` 与单个数据集函数的下载方式。

**操作步骤**：

1. 确认装了 pooch：`python -c "import pooch; print(pooch.__version__)"`。
2. 在能联网的机器上，进入一个临时目录，执行：

   ```python
   # practice_download_all.py
   import os
   from scipy.datasets import download_all

   target = "./my_data"          # 自选目录，不一定是缓存目录
   os.makedirs(target, exist_ok=True)
   download_all(target)          # 批量下载到 target
   print(sorted(os.listdir(target)))
   ```

3. 运行 `python practice_download_all.py`。

**需要观察的现象**：

- 运行结束后，`./my_data` 目录下应出现 `ascent.dat`、`ecg.dat`、`face.dat` 三个文件（与 `_registry.registry` 的键一一对应）。
- 再次运行同一脚本：因为目标目录里已有文件且哈希匹配，`pooch.retrieve` 不会重新下载（无网络流量）。

**预期结果**：`./my_data` 内容为 `['ascent.dat', 'ecg.dat', 'face.dat']`。

**与单个数据集函数的对比**（把下表填出来，是本实践的关键产出）：

| 维度 | `download_all(path)` | `ascent()` / `face()` / `electrocardiogram()` |
|---|---|---|
| 下载用到的 pooch API | `pooch.retrieve`（无状态函数） | `data_fetcher.fetch`（已配置实例的方法） |
| 触发方式 | 主动遍历整张注册表，批量 | 按需调用单个函数，单个 |
| 目标目录 | **任意** `path`（默认才是缓存） | 固定为 `pooch.os_cache('scipy-data')` |
| 返回值 | `None`（只产生副作用） | `numpy.ndarray` |
| 是否读取/解析文件内容 | 否（只搬运） | 是（pickle / bz2 / npz，见 u2-l3） |

> ⚠️ 网络可用性：以上命令依赖联网下载，实际能否跑通取决于网络环境。若离线，请改做本讲 4.3.4 的"源码阅读型实践"。

#### 4.1.5 小练习与答案

**练习 1**：`download_all` 为什么用 `pooch.retrieve` 而不是直接复用 `_fetchers.py` 里的 `data_fetcher.fetch`？

> **参考答案**：因为 `data_fetcher` 在 `pooch.create(...)` 时把 `path` 写死成了缓存目录 `pooch.os_cache("scipy-data")`，无法把文件下到**任意目录**。而 `download_all` 的核心卖点就是"下到你指定的 `path`"，所以它必须用接受 `path` 参数的 `pooch.retrieve`。此外 `download_all` 要**遍历所有**文件，用无状态的 `retrieve` 在循环里逐个调用更直接。

**练习 2**：如果把 `_registry.registry` 里多加了一条假记录，`download_all` 会怎样？

> **参考答案**：循环会尝试用 `registry_urls[假文件名]` 取 URL。如果 `registry_urls` 里也有对应条目，`pooch.retrieve` 会去下、然后用假文件的 `known_hash` 校验，校验失败时 pooch 会抛出哈希不匹配的异常；如果 `registry_urls` 里没有对应条目，则会抛 `KeyError`。这说明 `registry` 与 `registry_urls` 必须按键对齐（详见 u2-l2）。

---

### 4.2 模块二：`__spec__.parent` 双导入分支

#### 4.2.1 概念说明

打开 `_download_all.py`，你会看到一段对初学者很"诡异"的代码：

```python
if __spec__.parent is None or __spec__.parent == '':
    # Running as python script, use absolute import
    import _registry  # type: ignore
else:
    # Running as python module, use relative import
    from . import _registry
```

它要解决的问题是：**让同一个 `.py` 文件既能被 `import`、又能被 `python 文件名.py` 直接运行**，而这两种模式下，导入"隔壁的 `_registry`"的写法是**不一样**的。

- 当 `_download_all` 被 `import`（比如 `from scipy.datasets import download_all`）：它是包 `scipy.datasets` 里的一个模块，隔壁的 `_registry` 只能用**相对导入** `from . import _registry` 找到。
- 当它被 `python _download_all.py` 直接运行：它以 `__main__` 身份执行，**没有父包**，相对导入 `from . import _registry` 会直接报错；但此时 Python 会把"脚本所在目录"放进 `sys.path`，所以隔壁的 `_registry.py` 可以用**绝对导入** `import _registry` 当成顶层模块来抓。

这段 `if/else` 就是在运行时判断"我现在是哪种身份"，从而选对导入写法。文件顶部的文档字符串点明了它支持脚本运行：

[scipy/datasets/_download_all.py:1-7](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_download_all.py#L1-L7)：模块文档字符串，明说这是一个"平台无关、不需要完整 scipy 构建"的脚本，并给出运行方式 `python _download_all.py <download_dir>`。

#### 4.2.2 核心流程

判断依据是 `__spec__.parent`：

- `__spec__` 是 `importlib` 加载模块时绑定的 `ModuleSpec` 对象（模块的"身份证"）。
- `__spec__.name` 是模块的**完全限定名**；`__spec__.parent` 是它的**父包名**（即 name 去掉最后一段 `.` 之后的部分）。

两种身份下的取值：

| 运行方式 | `__spec__.name` | `__spec__.parent` | 走哪个分支 | 导入写法 |
|---|---|---|---|---|
| `from scipy.datasets import download_all`（包内模块） | `scipy.datasets._download_all` | `scipy.datasets`（非空） | `else` | `from . import _registry` |
| `python _download_all.py`（独立脚本） | `__main__` | `None` 或 `''`（无父包） | `if` | `import _registry` |

之所以要**同时判断 `is None` 和 `== ''`**，是为了稳妥地覆盖"独立脚本、没有父包"这一类情况在不同 Python 版本下的两种可能取值。

决策流程（伪代码）：

```
if __spec__.parent 没有父包 (None 或 ''):   # 脚本模式
    import _registry                          # 绝对导入：靠 sys.path[0] = 脚本所在目录
else:                                         # 模块模式
    from . import _registry                   # 相对导入：靠父包 scipy.datasets 定位
```

> 📌 一个容易踩的坑：为什么不能"永远用绝对导入 `import _registry`"省事？——因为 scipy 作为**已安装的包**，`scipy/datasets/` 这个内部目录**并不在 `sys.path` 上**，`_registry` 也不是顶层模块，绝对导入会 `ModuleNotFoundError`。反之，为什么不能"永远用相对导入"？——因为脚本模式下没有父包，相对导入会 `ImportError: attempted relative import with no known parent package`。所以这个运行时分支是**必须的**，不是装饰。

#### 4.2.3 源码精读

[scipy/datasets/_download_all.py:18-23](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_download_all.py#L18-L23)：双导入分支本体。注释 `# type: ignore` 是因为静态检查器（mypy）看到绝对导入 `import _registry` 会以为这是顶层模块而报警，但运行时它在脚本模式下确实能通过 `sys.path` 找到，故显式忽略。

注意：**这个技巧只影响"怎么拿到 `_registry`"，不影响 `_registry` 本身的内容**。无论走哪个分支，拿到的都是同一个定义了三张表的模块对象。

#### 4.2.4 代码实践（源码阅读型）

**实践目标**：亲眼看清 `__spec__.parent` 在两种运行模式下取值不同，并验证两种导入写法各自"只在对应的模式下才成立"。

**操作步骤**（无需联网，纯本地）：

1. **模块模式**下观察取值。执行：

   ```bash
   python -c "import scipy.datasets._download_all as m; print(m.__spec__.name, '|', m.__spec__.parent)"
   ```

   预期输出形如：`scipy.datasets._download_all | scipy.datasets`。`parent` 非空 → 走 `else` → 相对导入。

2. **脚本模式**下观察取值。先 `cd` 到 `scipy/datasets/` 目录（让 `_registry.py` 成为脚本目录里的兄弟文件），再执行：

   ```bash
   python -c "import sys; sys.argv=['x']; exec(open('_download_all.py').read())" 2>/dev/null; echo "---"
   ```

   更直接的办法（在你的本地副本里临时调试）：在 `_download_all.py` 顶部加一行 `print("PARENT =", repr(__spec__.parent))`，然后分别用 `python _download_all.py` 和 `python -c "import scipy.datasets._download_all"` 各跑一次，对比打印值，看完再删掉这行。

**需要观察的现象**：

- 模块模式下 `__spec__.parent == 'scipy.datasets'`（非空）。
- 脚本模式下 `__spec__.parent` 为 `None` 或 `''`。

**预期结果**：两种模式下 `__spec__.parent` 取值不同，且各自正好匹配 `if/else` 里对应的导入写法——这就是双导入分支能"两边都工作"的根因。

> ⚠️ `__spec__` 在脚本模式下的确切取值（`None` 还是 `''`）可能因 Python 版本而异，因此源码才同时检查两者。具体在你机器上是哪个值，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：如果把这段 `if/else` 直接换成 `from . import _registry`，会发生什么？

> **参考答案**：包内 `import` 仍然正常；但 `python _download_all.py` 脚本模式会立刻报 `ImportError: attempted relative import with no known parent package`，因为脚本以 `__main__` 身份运行、没有父包，相对导入无从定位 `.`。

**练习 2**：`__spec__.parent` 的值是怎么算出来的？

> **参考答案**：它是模块完全限定名 `__spec__.name` 去掉最后一段的结果。例如 `name = 'scipy.datasets._download_all'`，按最后一个 `.` 切分，父包名就是 `scipy.datasets`。对顶层模块（名字里没有 `.`，或脚本模式下没有父包），`parent` 就是 `''` 或 `None`。

---

### 4.3 模块三：`main()` 与 argparse 命令行入口

#### 4.3.1 概念说明

有了 4.2 的双导入分支，`_download_all.py` 已经可以当脚本运行了。但脚本需要一个"入口"——当用户敲 `python _download_all.py /some/dir` 时，谁来解析 `/some/dir` 这个命令行参数、再把它传给 `download_all`？

这就是 `main()` 的职责：它用标准库 `argparse` 定义命令行接口，把位置参数 `path` 解析出来，转交给 `download_all`。最后用一个所有 Pythonista 都熟悉的守卫把 `main()` 挂到"直接运行"的入口上：

```python
if __name__ == "__main__":
    main()
```

#### 4.3.2 核心流程

`main()` 的流程：

1. 创建 `argparse.ArgumentParser`，带一句 `description`。
2. 用 `add_argument("path", nargs='?', ...)` 添加一个**可选的位置参数**：`nargs='?'` 表示"最多给一个、也可以不给"。
3. `parser.parse_args()` 解析命令行，得到 `args.path`。
4. 调用 `download_all(args.path)` 执行真正的批量下载。

参数解析的几种情况：

| 用户命令 | `args.path` 取值 | 效果 |
|---|---|---|
| `python _download_all.py` （不给参数） | `default` 值 | 下到默认目录（缓存目录） |
| `python _download_all.py /tmp/dl` | `/tmp/dl` | 下到 `/tmp/dl` |

#### 4.3.3 源码精读

[scipy/datasets/_download_all.py:64-70](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_download_all.py#L64-L70)：`main()` 本体。`nargs='?'` 让 `path` 成为可选位置参数；`default=pooch.os_cache('scipy-data')` 是不给参数时的默认目录。

[scipy/datasets/_download_all.py:73-74](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_download_all.py#L73-L74)：经典的脚本守卫 `if __name__ == "__main__": main()`。它的含义是：**只有当本文件被直接运行时**（此时 `__name__` 等于 `"__main__"`）才执行 `main()`；当本文件被 `import` 时 `__name__` 是模块名，`main()` 不会自动跑——这正是它能安全地被 `from ._download_all import download_all` 导入而不触发下载的原因。

> ⚠️ **一个值得注意的"漏网"细节**（承接 u3-l1）：`main()` 里 `default=pooch.os_cache('scipy-data')` 是在**调用 `main()` 时立刻求值**的（不是惰性的）。如果 pooch 没装（`pooch is None`），这一行会先抛 `AttributeError: 'NoneType' object has no attribute 'os_cache'`，**抢在** `download_all` 内部那个友好的 `ImportError` 之前。也就是说：未装 pooch 时，直接 `python _download_all.py` 得到的是一个不那么友好的 `AttributeError`，而不是 `download_all` 里那段统一文案的 `ImportError`。这是 CLI 这条路径上降级处理不够彻底的一处小瑕疵。

#### 4.3.4 代码实践

**实践目标**：把 `_download_all.py` 当作**独立脚本**运行，把三个数据文件下载到指定目录；同时验证 `main()` 不会在 `import` 时被触发。

**操作步骤**：

1. 找到文件位置（在已安装的 scipy 里）：

   ```bash
   python -c "import scipy.datasets._download_all as m; print(m.__file__)"
   ```

2. 进入该文件所在目录（这样脚本模式下的绝对导入 `import _registry` 才能找到兄弟文件 `_registry.py`），然后运行：

   ```bash
   python _download_all.py ./cli_data
   ```

3. 另写一行验证 `import` 不触发下载：

   ```bash
   python -c "from scipy.datasets import download_all; print('imported ok, no download happened')"
   ```

**需要观察的现象**：

- 步骤 2 运行后，`./cli_data` 出现三个 `.dat` 文件。
- 步骤 3 只打印 `imported ok, no download happened`，**没有任何下载动作**（因为 `if __name__ == "__main__"` 守卫把 `main()` 挡住了）。

**预期结果**：脚本模式下载成功；模块模式纯导入不触发下载。两者对比正好体现 `__name__ == "__main__"` 守卫的作用。

> ⚠️ 步骤 2 依赖联网下载，能否跑通取决于网络环境。离线时仍可做步骤 3 的守卫验证（不需要网络）。

#### 4.3.5 小练习与答案

**练习 1**：`parser.add_argument("path", nargs='?', ...)` 里的 `nargs='?'` 如果改成 `nargs='+'`，用户体验会有什么变化？

> **参考答案**：`nargs='?'` 表示"0 个或 1 个"参数，所以 `python _download_all.py`（不带参数）合法、走 `default`；改成 `nargs='+'`（"至少 1 个"）后，不带参数运行就会报错退出，用户必须显式给一个目录，失去了"不传参就用默认缓存目录"的便利。

**练习 2**：为什么 `import _download_all` 不会触发下载，而 `python _download_all.py` 会？

> **参考答案**：因为下载逻辑挂在 `if __name__ == "__main__": main()` 守卫之后。`import` 时模块的 `__name__` 是它的模块名（不是 `"__main__"`），守卫条件为假，`main()` 不执行；只有直接 `python xxx.py` 运行时，`__name__` 才等于 `"__main__"`，`main()` 才被调用。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个"**看懂并复述这条命令的完整执行链**"的小任务：

**任务**：用户在 `scipy/datasets/` 目录下敲下 `python _download_all.py /tmp/mycache`，请你说清楚从按下回车到三个文件落盘之间，本讲涉及的代码依次发生了什么。

**要求产出一份"执行链"说明，至少覆盖以下节点**（每一步都要点出对应的源码位置或概念）：

1. Python 以 `__main__` 身份加载该文件 → `__spec__.parent` 为 `None`/`''` → 走 `if` 分支 → `import _registry`（靠 `sys.path[0]` = 脚本目录找到兄弟文件）。
2. 执行到 `if __name__ == "__main__":` 守卫 → 条件为真 → 调用 `main()`。
3. `main()` 用 argparse 解析出 `args.path = "/tmp/mycache"` → 调用 `download_all("/tmp/mycache")`。
4. `download_all` 内：pooch 降级检查通过 → `path` 非 `None` 直接用 → 构造带 `User-Agent: SciPy` 的下载器。
5. 遍历 `_registry.registry` 的三对 `(文件名, SHA256)`，逐个用 `registry_urls[文件名]` 取 URL、`pooch.retrieve(...)` 下载并校验，落到 `/tmp/mycache`。

**进阶追问**（写进你的说明里）：

- 这条链路里，哪一步如果 `pooch` 没装会先报错？（提示：注意 4.3.3 提到的 `default=pooch.os_cache(...)` 求值时机。）
- 这条链路和 `scipy.datasets.face()` 的执行链，在哪一步开始"分叉"？（提示：一个是遍历注册表 + `pooch.retrieve`，一个是 `data_fetcher.fetch` + bz2 解析。）

> ⚠️ 若你想实跑这条命令验证，需要联网且 pooch 已安装；离线时本任务可纯靠源码阅读完成（属于"源码阅读型综合实践"）。

---

## 6. 本讲小结

- `download_all(path)` 是**注册表驱动的批量下载工具**：遍历 `_registry.registry`，用无状态的 `pooch.retrieve` 把每个文件下到**任意指定目录**（默认才是缓存目录），返回 `None`——只搬运、不解析。
- 它和单个数据集函数的下载方式形成鲜明对比：后者用**有状态**的 `data_fetcher.fetch`、固定下到缓存目录、最后还返回解析好的 `ndarray`。
- `if __spec__.parent is None or __spec__.parent == '':` 这段**双导入分支**，让 `_download_all.py` 既能被包内 `import`（走相对导入 `from . import _registry`），又能被 `python xxx.py` 直接运行（走绝对导入 `import _registry`）。两种模式的导入写法互不兼容，所以这个运行时分支是**必需的**。
- `main()` 用 `argparse` 暴露一个可选位置参数 `path`，并通过 `if __name__ == "__main__": main()` 守卫挂载到脚本入口——这保证 `import` 该模块时不会触发下载。
- 一处细节：`main()` 里 `default=pooch.os_cache('scipy-data')` 是**立即求值**的，pooch 缺失时会先抛 `AttributeError`，抢先于 `download_all` 内部那个友好的 `ImportError`——这是 CLI 降级处理的一处小瑕疵（承接 u3-l1）。

---

## 7. 下一步学习建议

- **想看 `download_all` 在真实工程里怎么被用**：去读 `tests/test_data.py` 里的 `autouse` module 级 fixture（本讲下一讲 **u3-l3 测试设计剖析** 会专门讲它如何用 `download_all()` 预热缓存、再用 `_has_hash` 校验落盘文件）。
- **想理解 `@xp_capabilities(out_of_scope=True)` 装饰器的全貌**：它和数组 API 迁移、meson 构建声明一起，在 **u3-l4 扩展实战：新增一个数据集** 里有完整讨论。
- **想再巩固 `pooch.retrieve` vs `data_fetcher.fetch` 的差异**：回头对照 **u2-l1（data_fetcher 与 fetch_data）**，把"有状态实例"和"无状态函数"两种 pooch 用法并排看一遍。
- **想深入 `__spec__` / `__name__` 这些模块元信息**：可以阅读 Python 官方文档关于 `importlib`、`ModuleSpec`（PEP 451）以及 `__main__` 的章节，理解 import 系统如何决定一个文件的"身份"。
