# 测试设计剖析

> 本讲是「专家层」的第三篇。请确认你已学完 **u2-l3（三种数据集的加载与转换）**——知道 `ascent`/`face`/`electrocardiogram` 各自返回什么 shape、dtype、统计量；以及 **u2-l4（缓存清理机制）**——知道公开的 `clear_cache` 只是个薄壳，真正干活的是带两个「可注入参数」`cache_dir` 与 `method_map` 的内部函数 `_clear_cache`。本讲就站在这两个认知之上，去看 `scipy.datasets` 是**怎么被测试的**。

---

## 1. 本讲目标

学完本讲，你应当能够：

1. 读懂 `tests/test_data.py` 里的**模块级 `autouse` fixture**：理解它为什么能在所有测试跑之前**自动**调用一次 `download_all()` 预热缓存，从而把「联网下载」与「逐个断言」两件事彻底解耦。
2. 掌握 `_has_hash` 这个一行级辅助函数：它如何用 `pooch.file_hash` 对**已经落盘的缓存文件**做一次独立的 SHA256 复核，和注册表 `registry` 里的期望哈希比对。
3. 学会 `test_clear_cache` 的**离线隔离**手法：用 pytest 内置的 `tmp_path` 临时目录 + 一个自己捏造的 `dummy_method_map`，把真实缓存目录和真实 `method_files_map` 完全架空，从而在没有网络、不污染本机的前提下测遍 `_clear_cache` 的所有分支。

---

## 2. 前置知识

本讲是「读测试」，所以先补几个测试框架本身的概念，用大白话过一遍：

- **pytest 的 fixture（测试夹具）**：你可以把 fixture 理解成「测试的前置/后置准备」。声明一个 fixture 后，pytest 会在合适的时机帮你调用它，把它 `return`/`yield` 的东西喂给需要的测试。本讲最关键的是 fixture 的两个修饰属性：
  - `scope='module'`：这个 fixture 的「有效期」是**整个 `.py` 文件**——一个模块里不管有多少个测试，fixture 只在**进入该模块时执行一次**，所有测试共享这一次的结果。
  - `autouse=True`：「自动套用」——不需要任何测试在参数列表里显式声明它，它也会**自动**在范围内生效。
- **`yield` 分割的 setup/teardown**：一个 fixture 函数里，`yield` 之前的代码是「setUp（前置）」，会在测试开始前跑；`yield` 之后的代码是「tearDown（后置）」，会在测试结束后跑。如果后置没什么要做的，`yield` 后面就空着——本讲的 fixture 就是这种「只有前置」的形态。
- **`tmp_path`**：pytest 内置的 fixture，每次测试自动给你一个**独一无二、测完自动删除**的临时目录（`pathlib.Path` 对象）。它让测试可以放心地写文件、建目录，而不用关心清理。
- **`pytest.importorskip("pooch")`**：如果 `pooch` 没装，pytest 会把整个模块的测试**整体跳过**（而不是报错失败）；装了就把 `pooch` 模块对象返回给你。这是「可选依赖」在测试侧的标准处理（复习 u3-l1）。
- **`pooch.file_hash(path)`**：pooch 提供的工具，读取 `path` 指向的文件，算出它的 SHA256 哈希（一个 64 位十六进制字符串），返回给你。注意它只**算哈希**、不下载，是对**本地文件**的操作。
- **`get_ident()`**（来自标准库 `threading`）：返回当前线程的一个数字标识符。本讲会看到测试用它造一个「线程专属」的子目录。

> 关键复习（来自 u2-l4）：`_clear_cache(datasets, cache_dir=None, method_map=None)` 比 `clear_cache` 多了 `cache_dir` 和 `method_map` 两个带默认值的参数。默认时它们分别指向「pooch 的真实缓存目录」和「`_registry.method_files_map`」；但**测试可以传自己的值**把它俩替换掉。这正是 `test_clear_cache` 能做到离线隔离的根本原因。

---

## 3. 本讲源码地图

本讲以一个测试文件为主角，把它和生产代码串起来：

| 文件 | 在本讲里的角色 |
|---|---|
| `scipy/datasets/tests/test_data.py` | **主角**。整篇讲义都在剖析它：模块级 fixture、`_has_hash`、`TestDatasets` 类、`test_clear_cache`。 |
| `scipy/datasets/_registry.py` | **数据源**。测试里到处用到的 `registry`（文件名→SHA256）与 `method_files_map`（方法名→文件列表）都在这里。 |
| `scipy/datasets/_utils.py` | **被测对象之一**。`_clear_cache` 的分支逻辑是 `test_clear_cache` 的检验目标。 |
| `scipy/datasets/_download_all.py` | **被 fixture 调用**。`download_all()` 被 autouse fixture 当作「预热」来用。 |

辅助参考（不在本讲精读范围）：

- `scipy/datasets/_fetchers.py`：`data_fetcher.path` 这一行被测试取出来当作缓存目录（即 `data_dir`）。
- `scipy/datasets/tests/meson.build`：声明 `test_data.py` 是一个带 `install_tag: 'tests'` 的测试源文件，构建系统据此安装但不随主包发布。

---

## 4. 核心概念与源码讲解

### 4.1 模块一：`TestDatasets` 与 `autouse` fixture

#### 4.1.1 概念说明

`scipy.datasets` 的数据集函数（`ascent`/`face`/`electrocardiogram`）有一个特点：**第一次调用要联网下载**（复习 u1-l3）。如果在测试里每个用例都各自触发一次下载，会有两个麻烦：

1. **慢且脆弱**：网络时好时坏，CI 上经常因为下载失败而误报；而且下载很慢。
2. **重复**：三个数据集函数、若干个断言，本来可以共享同一份已下载的文件。

测试文件用一个非常聪明的办法解决了这两个问题：写一个 **`autouse` 且 `scope='module'` 的 fixture**，让它在**整个模块的任何测试开始之前**，先调用一次 `download_all()` 把所有数据文件**预热**到缓存目录里。之后真正跑测试时，每个数据集函数再去取文件，就**全是本地缓存命中**——既不联网、又很快。

这个 fixture 本身的名字虽然叫 `test_download_all`，但它**不是一个真正意义上的「测试」**——它没有 `assert`，它纯粹是一个「借了 fixture 语法、自动执行一次下载」的前置准备。这一点初读时容易看走眼，请特别注意。

#### 4.1.2 核心流程

`autouse` + `scope='module'` 的执行时序可以这样理解：

```
pytest 收集到 test_data.py 模块
        │
        ▼
【模块进入】触发 autouse + module 级 fixture：test_download_all
        │
        │  setUp 阶段：调用 download_all()   ← 联网！把 ascent.dat/face.dat/ecg.dat 下到缓存目录
        │
        yield  ← 把控制权交给测试
        │
        ▼
开始跑模块内的各个测试（test_existence_all / test_ascent / test_face / test_electrocardiogram）
        │
        │  这些测试里调用 ascent()/face()/electrocardiogram() → 全部命中本地缓存，不再联网
        │
        ▼
【模块结束】fixture 的 yield 之后没有代码，tearDown 为空
```

关键点有三：

1. **只下一次**：因为 `scope='module'`，无论模块里有几个测试，`download_all()` 只执行一次。
2. **自动执行**：因为 `autouse=True`，没有任何测试需要把它写进参数列表，它自己就生效。
3. **下载目录与缓存目录是同一个**：`download_all()` 不传参时默认下到 `pooch.os_cache('scipy-data')`（见 [_download_all.py:L54-L55](_download_all.py#L54-L55)），而 `data_fetcher.path` 也是 `pooch.os_cache("scipy-data")`（见 [_fetchers.py:L18](_fetchers.py#L18)）。所以预热下好的文件，正好就是后续数据集函数会去命中的那批文件。

#### 4.1.3 源码精读

先看模块顶部的导入与一个关键的跳过指令：

[test_data.py:L1-L13](tests/test_data.py#L1-L13) —— 导入被测对象与跳过策略。注意第 10 行 `pooch = pytest.importorskip("pooch")`：pooch 没装就**整模块跳过**；第 13 行 `data_dir = data_fetcher.path` 把缓存目录取出来存成模块级常量，后面的哈希校验会反复用到它。

接着是本模块的核心——那个 `autouse` fixture：

[test_data.py:L23-L32](tests/test_data.py#L23-L32) —— `TestDatasets` 类内的 `test_download_all` fixture。逐行看：

- 第 25 行 `@pytest.fixture(scope='module', autouse=True)`：两个修饰属性同时加上，含义见 4.1.2。
- 第 27 行注释 `# This fixture requires INTERNET CONNECTION`：明确提醒它需要联网，这也是为什么 pooch 没装时整个文件会被 `importorskip` 跳过。
- 第 30 行 `download_all()`：唯一的 setUp 动作——把所有数据文件预热到缓存目录。
- 第 32 行 `yield`：把控制权让给测试；`yield` 之后没有代码，意味着没有 tearDown。

> 小提醒：这个 fixture 的方法名 `test_download_all` 以 `test_` 开头，pytest 默认会把它**收集为测试**；但它同时是 fixture，pytest 会按 fixture 来对待它（不会作为一个独立测试点去运行它的函数体，而是按 `autouse` 规则注入）。这是 pytest 里一个略让人迷惑的命名，读代码时请把它**当作 fixture 理解**，不要被名字误导。

再看 `TestDatasets` 类里的几个真正断言型测试，它们都依赖刚才预热好的缓存：

[test_data.py:L34-L36](tests/test_data.py#L34-L36) —— `test_existence_all`：断言缓存目录里**至少**有 `len(registry)` 个文件。用 `>=` 而非 `==`，是为了容许缓存目录里同时存在一些历史遗留文件，体现了测试的「宽容」。

[test_data.py:L38-L43](tests/test_data.py#L38-L43) —— `test_ascent`：断言 `ascent().shape == (512, 512)`（复习 u2-l3），并用 `_has_hash` 复核 `ascent.dat` 的哈希。

[test_data.py:L52-L62](tests/test_data.py#L52-L62) —— `test_electrocardiogram`：最严格的一个，除了 shape 还断言 `dtype`、均值 `-0.16510875`、标准差 `0.5992473991177294`（这正是 u2-l3 讲过的「黄金数值」），最后再做一次哈希复核。

注意一个细节：`test_existence_all` 上方有个 `@pytest.mark.fail_slow(10)`（[test_data.py:L34](tests/test_data.py#L34)），意思是「如果这个测试失败，至少要跑满 10 秒才算失败」——这是一种防止「下载慢被误判为失败」的保护性标记。

#### 4.1.4 代码实践

> 这是一个**源码阅读型实践**，不需要你写新代码，而是动手观察 fixture 的行为。

1. **实践目标**：亲眼看到「`download_all()` 只在模块开始时执行一次、之后的测试全部命中缓存」。
2. **操作步骤**：
   - 在 `download_all` 函数体内临时想象加一行 `print(">>> download_all called")`（**只是想象，不要真去改源码**）；同样在 `fetch_data` 内想象加一行 `print(">>> fetch_data called")`。
   - 用 `pytest -s -k "test_ascent or test_face" scipy/datasets/tests/test_data.py` 运行（`-s` 关闭输出捕获）。
3. **需要观察的现象**：`>>> download_all called` 只打印**一次**（模块进入时）；而 `>>> fetch_data called` 在每个数据集测试里都会打印——但请注意，由于缓存已命中，`fetch_data` 内部走的是 pooch 的「缓存命中」分支，**并不会真正发起 HTTP 请求**。
4. **预期结果**：下载动作集中、前置、只发生一次；各测试函数对数据集的调用是「读本地」。
5. 如果无法本地联网验证，「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：如果把 fixture 的 `scope='module'` 改成 `scope='function'`，会发生什么？是好是坏？

> **参考答案**：`scope='function'` 会让 fixture 在**每一个测试函数**之前都执行一次。在本场景里，这意味着 `download_all()` 会被调用多次（每个 `test_*` 之前一次）。由于 pooch 会校验缓存里已有文件的哈希、命中就不再重新下载，所以**功能上不会坏**，但会多做若干次「哈希校验」的无谓开销，拖慢测试。因此 `scope='module'` 是更合适的选择。

**练习 2**：为什么这个 fixture 必须配合 `pytest.importorskip("pooch")` 一起用？

> **参考答案**：fixture 里调用了 `download_all()`，而 `download_all` 在 pooch 缺失时会抛 `ImportError`（复习 u3-l2）。如果不在文件顶部 `importorskip`，那么在没装 pooch 的环境里这个 autouse fixture 一执行就会让整个模块**报错失败**；加上 `importorskip` 后，pooch 缺失时整个模块会被**静默跳过**，符合「可选依赖」的优雅降级原则。

---

### 4.2 模块二：`_has_hash` 文件哈希校验

#### 4.2.1 概念说明

数据集文件下载时，pooch 已经用 `registry` 里的 SHA256 做过一次校验（复习 u2-l2）。那为什么测试里还要**再算一次哈希**？

答案是**纵深防御（defense in depth）+ 显式断言**。测试关心的不是「pooch 说它校验过」，而是「此刻**落盘在缓存目录里的这个文件**，其内容确实等于注册表所声明的那个哈希」。于是测试写了一个极简的辅助函数 `_has_hash`，对**本地路径**重新算一次 `pooch.file_hash`，和 `registry[文件名]` 比对。

这个函数只有 5 行，但它体现了测试的一个重要理念：**不盲目信任被测系统内部的隐式校验，而是用独立的、可见的方式复核关键不变量**。

#### 4.2.2 核心流程

`_has_hash(path, expected_hash)` 的逻辑极简：

```
def _has_hash(path, expected_hash):
    if 文件不存在(path):
        return False                      # 没下载下来，自然不匹配
    return pooch.file_hash(path) == 期望哈希   # 算本地哈希，比对
```

它的返回值是一个布尔，测试里直接 `assert _has_hash(...)`。两个失败模式都被它覆盖：

- **文件根本不存在**（比如下载失败、被误删）→ 返回 `False` → 断言失败。
- **文件存在但内容不对**（损坏、被篡改、版本错位）→ 哈希不等 → 返回 `False` → 断言失败。

#### 4.2.3 源码精读

[test_data.py:L16-L20](tests/test_data.py#L16-L20) —— `_has_hash` 的全部实现。第 18 行先处理「路径不存在」直接返回 `False`；第 20 行用 `pooch.file_hash(path)` 算出本地文件的实际哈希，与传入的 `expected_hash` 比较。

它被三处使用，模式完全一致，以 `test_ascent` 为例：

[test_data.py:L42-L43](tests/test_data.py#L42-L43) —— `assert _has_hash(os.path.join(data_dir, "ascent.dat"), registry["ascent.dat"])`。这里 `data_dir` 是模块级常量（缓存目录），`registry["ascent.dat"]` 是 [_registry.py:L9](_registry.py#L9) 里登记的那个 64 位哈希串。`face.dat`（[test_data.py:L49-L50](tests/test_data.py#L49-L50)）与 `ecg.dat`（[test_data.py:L61-L62](tests/test_data.py#L61-L62)）的校验写法完全对称，对应的哈希分别在 [_registry.py:L11](_registry.py#L11) 与 [_registry.py:L10](_registry.py#L10)。

> 一句话点透：`_has_hash` 把「文件名」这把钥匙，同时插进两个锁——一个锁是**磁盘上的实际文件**（`os.path.join(data_dir, 文件名)`），另一个锁是**注册表里的期望哈希**（`registry[文件名]`）。两把锁对上了，才说明「缓存里的文件确实是我们声明的那一份」。

#### 4.2.4 代码实践

1. **实践目标**：亲手用 `pooch.file_hash` 算一个本地文件的哈希，体会 `_has_hash` 在做什么。
2. **操作步骤**：先 `python -c "import scipy.datasets as d; d.ascent()"` 触发一次下载；然后在 Python 里执行：

   ```python
   import pooch, os
   from scipy.datasets._fetchers import data_fetcher
   from scipy.datasets._registry import registry
   p = os.path.join(data_fetcher.path, "ascent.dat")
   print(pooch.file_hash(p) == registry["ascent.dat"])   # 应为 True
   print(pooch.file_hash(p))                             # 打印实际哈希
   ```
3. **需要观察的现象**：第一个 `print` 输出 `True`；第二个 `print` 输出与 [_registry.py:L9](_registry.py#L9) 完全一致的 64 位字符串。
4. **预期结果**：你刚刚手动复现了 `test_ascent` 里 `_has_hash` 那一行断言的全部工作。
5. 若本机无法联网下载数据，「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：如果有人篡改了缓存目录里的 `ascent.dat`（往后面追加一个字节），`test_ascent` 会怎么失败？

> **参考答案**：`ascent()` 调用时，pooch 在「缓存命中」路径下会**重新校验哈希**，发现不匹配，从而抛出异常（pooch 会尝试重新下载修复）。即便假设 pooch 没拦住，`_has_hash` 这一行也会因为 `pooch.file_hash(path) != registry["ascent.dat"]` 返回 `False`，导致 `assert` 失败。这正是 `_has_hash` 作为「第二道防线」的价值。

**练习 2**：`_has_hash` 为什么不直接 `raise`，而是返回布尔值？

> **参考答案**：因为它是一个**辅助断言**函数，职责是「告诉我这个文件符不符合期望」，而不是「替我做决定」。把判断结果以布尔返回，调用方可以用 `assert _has_hash(...)` 写出最直白的断言；同时它也能在非断言语境下被复用（比如条件判断）。这是「辅助函数保持纯粹、把决策权留给调用方」的常见写法。

---

### 4.3 模块三：`test_clear_cache` 的离线隔离测试

#### 4.3.1 概念说明

u2-l4 讲过，`_clear_cache` 是一棵「五步决策树」，分支很多：单个数据集 vs 多个数据集、单个文件 vs 多个文件、方法不存在抛 `ValueError`、`datasets=None` 整目录删除、目录不存在则友好返回……要测遍这些分支，**最怕的就是污染真实缓存**——你总不希望跑一次测试就把用户机器上的 `scipy-data` 缓存清空了吧？

`test_clear_cache` 用一套优雅的手法把这个难题彻底化解：

1. 用 pytest 内置的 `tmp_path` 拿一个**一次性临时目录**，在里面伪造一个「假缓存目录」`dummy_cache_dir`。
2. 在里面**手动创建**几个空文件（`data0.dat`、`data1.dat`……），假装它们是缓存好的数据文件。
3. 再**自己捏一张** `dummy_method_map`（方法名→文件名列表），结构和真实的 `method_files_map` 一模一样，但内容是假的。
4. 把这两个假东西通过 `_clear_cache` 的「可注入参数」`cache_dir` 和 `method_map` **塞进去**，于是 `_clear_cache` 完全在一个「楚门的世界」里运行——不碰真实缓存、不碰真实注册表、不需要网络。

这一切之所以可行，**完全归功于 u2-l4 强调过的一个设计**：`_clear_cache` 特意把 `cache_dir` 和 `method_map` 暴露成了带默认值的参数。公开的 `clear_cache` 没有这两个参数，所以**没法**这样测；测试瞄准的就是内部函数 `_clear_cache`。

#### 4.3.2 核心流程

`test_clear_cache` 的骨架可以概括成「搭台 → 逐分支演戏 → 收尾」：

```
# —— 搭台：造一个假的缓存目录 + 假的方法表 ——
dummy_basepath = tmp_path / ... / "dummy_cache_dir"      # 假缓存目录
dummy_method_map = {"data0": ["data0.dat"], ..., "data3": ["data3.dat"]}
在每个 dataN.dat 路径写一个空文件

# —— 演戏 1：传「单个 callable」，清 data0 ——
_clear_cache(datasets=data0, cache_dir=dummy_basepath, method_map=dummy_method_map)
断言 data0.dat 已删

# —— 演戏 2：传「列表」，清多个 ——
_clear_cache(datasets=[data1, data2], ...)
断言 data1.dat / data2.dat 已删

# —— 演戏 3：一个方法对应多个文件 ——
dummy_method_map["data4"] = ["data4_0.dat", "data4_1.dat"]   # 一对多
_clear_cache(datasets=[data4], ...)
断言 data4_0.dat / data4_1.dat 都已删

# —— 演戏 4：方法名查不到 ——
_clear_cache(datasets=[data5], ...)   # data5 不在表里
断言抛 ValueError

# —— 收尾：datasets=None 整目录删除 ——
_clear_cache(datasets=None, cache_dir=dummy_basepath)
断言 dummy_basepath 整个目录已不存在
```

注意一个关键细节：测试里定义的「假数据集方法」是**真正的空函数**：

```python
def data0():
    pass
```

为什么是函数而不是字符串？因为 [_utils.py:L39](_utils.py#L39) 里 `_clear_cache` 是用 `dataset.__name__` 来取方法名的——它要求传入的是**可调用对象**（[test_data.py 处处 `def dataN(): pass`](tests/test_data.py#L82) 的 `__name__` 恰好是 `"data0"`/`"data1"`……，正好对上 `dummy_method_map` 的键）。这是把「函数对象当键」的设计在测试侧的呼应。

#### 4.3.3 源码精读

先看「搭台」部分，注意那个用 `get_ident()` 包了一层的子目录：

[test_data.py:L65-L78](tests/test_data.py#L65-L78) —— `test_clear_cache` 的入参与假环境搭建。第 67 行 `thread_basepath = tmp_path / str(get_ident())` 在 `tmp_path` 下又套了一层「以线程 id 命名」的子目录，再在第 68 行 `mkdir()`；第 70-71 行在其下创建 `dummy_cache_dir`。第 74-78 行用一个循环造出 `data0`~`data3` 四个方法，每个映射到一个 `.dat` 文件，并把空字符串写进对应文件（`write_text("")`）。`get_ident()` 这一层嵌套，推测是为了在并行/线程化测试运行时进一步隔离，避免多个执行流踩到同一棵子树（确切动机「待确认」，但隔离意图是明确的）。

再看四个分支的演戏。**演戏 1（单个 callable）**：

[test_data.py:L80-L86](tests/test_data.py#L80-L86) —— 定义空函数 `data0`，把它**直接**（不是放进列表）作为 `datasets` 传给 `_clear_cache`，验证 `_clear_cache` 内部「单个也归一化成列表」的分支（对应 [_utils.py:L34-L36](_utils.py#L34-L36)），然后断言 `data0.dat` 已被删除。

**演戏 2（列表，多个方法）**：

[test_data.py:L88-L97](tests/test_data.py#L88-L97) —— 一次传 `[data1, data2]`，断言两个文件都被删，覆盖「循环删除列表里每个方法对应文件」的主分支（对应 [_utils.py:L37-L57](_utils.py#L37-L57)）。

**演戏 3（一个方法对应多个文件）**：

[test_data.py:L99-L111](tests/test_data.py#L99-L111) —— 这是测 `method_files_map` 「一对多」能力的关键用例：先临时给 `dummy_method_map` 加一项 `"data4": ["data4_0.dat", "data4_1.dat"]`，再清 `data4`，断言**两个**文件都被删。它验证了 [_utils.py:L46-L54](_utils.py#L46-L54) 里那个「`for data_filepath in data_filepaths:`」的内层循环——一个方法名可能对应不止一个文件。

**演戏 4（方法名查不到 → ValueError）**：

[test_data.py:L113-L119](tests/test_data.py#L113-L119) —— 定义 `data5`（不在 `dummy_method_map` 里），用 `with pytest.raises(ValueError):` 包住调用，断言 [_utils.py:L40-L44](_utils.py#L40-L44) 的「查不到方法名就抛 `ValueError`」分支确实被触发。注意：这一步**也是本讲「代码实践」要让你独立复刻的那个场景**（见 4.3.4）。

**收尾（datasets=None → 整目录删除）**：

[test_data.py:L121-L123](tests/test_data.py#L121-L123) —— 不传 `method_map`（默认回退到真实 `method_files_map`，但因为 `datasets=None` 走的是整目录删除分支，根本不会查表），断言整个 `dummy_basepath` 被 `shutil.rmtree` 掉（对应 [_utils.py:L30-L32](_utils.py#L30-L32)）。

> 把这五段连起来看，你会发现 `test_clear_cache` 几乎**逐分支覆盖**了 `_clear_cache` 的整棵决策树，而且全程没有联网、没有触碰真实缓存——这就是「可注入参数」带来的可测性红利。

#### 4.3.4 代码实践

> 本讲的核心实践。请你**亲手写一个独立的测试函数**，复刻 4.3.3 里「演戏 4」那个场景：清理一个不存在的方法时，`_clear_cache` 应抛 `ValueError`。
>
> 说明：这个场景在 `test_data.py` 里**已经内嵌在 `test_clear_cache` 中**（[test_data.py:L113-L119](tests/test_data.py#L113-L119)）。本实践的目的是让你把这一小段**抽成一个独立、聚焦的测试**，练熟「`tmp_path` + dummy `method_map`」这套离线隔离套路。

1. **实践目标**：写一个 `test_clear_cache_unknown_method_raises(tmp_path)`，在伪造的离线环境里，断言 `_clear_cache(datasets=[某个不在表里的方法], cache_dir=假目录, method_map=假表)` 抛 `ValueError`。
2. **操作步骤**：
   - 在 `tests/test_data.py` 末尾**追加**一个新函数（或在自己的临时脚本里），照搬「搭台」三件套：`tmp_path` → 建子目录 → 写一个空文件 → 捏一张只含一个方法的 `dummy_method_map`。
   - 定义一个名字**故意不在表里**的空函数，例如 `def ghost(): pass`。
   - 用 `with pytest.raises(ValueError):` 包住 `_clear_cache(...)` 调用。
   - 运行：`pytest -k test_clear_cache_unknown_method_raises scipy/datasets/tests/test_data.py`。
3. **需要观察的现象**：测试通过（`ValueError` 被如期抛出）。可以临时把 `pytest.raises` 去掉，观察 `_clear_cache` 抛出的报错文案（正是 [_utils.py:L41-L44](_utils.py#L41-L44) 那段「Dataset method ghost doesn't exist...」）。
4. **预期结果**：你得到一个干净、独立、离线的测试，它只验证一件事——「方法名查不到时抛 `ValueError`」。
5. **参考答案（示例代码，非项目原有代码）**：

   ```python
   # 示例代码：追加到 tests/test_data.py 末尾
   def test_clear_cache_unknown_method_raises(tmp_path):
       # 搭台：伪造一个缓存目录 + 一张只含一个方法的假表
       cache_dir = tmp_path / "dummy_cache_dir"
       cache_dir.mkdir()
       (cache_dir / "real.dat").write_text("")
       dummy_method_map = {"real": ["real.dat"]}   # 只登记了 "real"

       # ghost 不在 dummy_method_map 里
       def ghost():
           pass

       # 期望：抛 ValueError
       with pytest.raises(ValueError):
           _clear_cache(datasets=[ghost], cache_dir=cache_dir,
                        method_map=dummy_method_map)
   ```

6. 如果你不方便运行 pytest，「待本地验证」——但**这段参考答案的逻辑可以直接对照 [_utils.py:L40-L44](_utils.py#L40-L44) 推理出来**，无需联网。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `test_clear_cache` 测的是 `_clear_cache`（带下划线），而不是公开的 `clear_cache`？

> **参考答案**：因为公开的 `clear_cache(datasets=None)` **没有** `cache_dir` 和 `method_map` 这两个参数（见 [_utils.py:L60-L80](_utils.py#L60-L80)），它内部硬编码去用真实的 `method_files_map` 和 platformdirs 解析出的真实缓存目录。如果拿它来测，要么得碰真实缓存（危险），要么得 mock 全局对象（麻烦）。而 `_clear_cache` 把这两个依赖**参数化**了，测试只需传自己的假值就能隔离——所以测试瞄准内部函数是有意为之。

**练习 2**：`test_clear_cache` 里的假数据集方法为什么都写成 `def data0(): pass` 这种空函数，而不是直接传字符串 `"data0"`？

> **参考答案**：因为 `_clear_cache` 内部用 `assert callable(dataset)` 强制要求传入的是可调用对象，再用 `dataset.__name__` 取方法名去查 `method_map`（见 [_utils.py:L38-L39](_utils.py#L38-L39)）。传字符串会在 `assert callable(...)` 处直接失败。定义空函数 `def dataN(): pass` 既满足「可调用」，又让 `__name__` 恰好等于方法名字符串，一举两得。

**练习 3**：收尾那一步 `_clear_cache(datasets=None, cache_dir=dummy_basepath)` 没有传 `method_map`，会不会出问题？

> **参考答案**：不会。因为 `datasets=None` 时，`_clear_cache` 走的是「整目录 `shutil.rmtree`」分支（[_utils.py:L30-L32](_utils.py#L30-L32)），**根本不会去查 `method_map`**。`method_map` 默认会回退成真实的 `method_files_map`，但在这个分支里它压根没被用到，所以安全。

---

## 5. 综合实践

把本讲三个模块串起来，做一个贯穿小任务：**为「新增一个数据集」补一份最小测试方案**（呼应下一讲 u3-l4 的扩展实战，但这里只关注「怎么测」）。

假设你打算新增一个叫 `dummy` 的数据集，对应文件 `dummy.dat`，返回一个固定 shape 的数组。请你设计：

1. **预热**：`TestDatasets` 里的 autouse fixture 需要改动吗？为什么？
   > 提示：`download_all()` 遍历的是 `registry`。只要 `dummy.dat` 被登记进 [_registry.py:L8-L12](_registry.py#L8-L12) 的 `registry` 与 `registry_urls`，fixture 调用 `download_all()` 时就会**自动**把它一起预热，**fixture 本身不用改**。这正是「注册表驱动」的好处。
2. **断言**：仿照 [test_data.py:L38-L43](tests/test_data.py#L38-L43)，给 `dummy` 写一个 `test_dummy`，断言它的 shape，并用 `_has_hash` 复核 `dummy.dat` 的哈希。请写出关键两行。
   > 参考答案（示例代码）：
   > ```python
   > def test_dummy(self):
   >     assert_equal(dummy().shape, (你的期望 shape))
   >     assert _has_hash(os.path.join(data_dir, "dummy.dat"),
   >                      registry["dummy.dat"])
   > ```
3. **清理**：`dummy` 的缓存清理已经**被 `_clear_cache` 的通用逻辑覆盖**了吗？需要新写测试吗？
   > 提示：`test_clear_cache` 测的是 `_clear_cache` 的**通用分支**，与具体方法名无关。只要你把 `"dummy": ["dummy.dat"]` 登记进 `method_files_map`（[_registry.py:L22-L26](_registry.py#L22-L26)），清理逻辑就自动生效，**通常不需要为新数据集单独写清理测试**。
4. **离线**：上述断言型测试需要联网吗？
   > 答：`test_dummy` 依赖 fixture 预热，**间接需要联网**（首次）；而 `test_clear_cache` 那一类用 `tmp_path` 的测试**完全离线**。请把这两类测试在脑海里清晰区分开。

完成这个综合实践后，你应该能体会到 `scipy.datasets` 测试体系的**两层结构**：一层是「依赖网络、靠 fixture 预热、断言数据内容」的在线测试（`TestDatasets` 类），另一层是「完全离线、靠 `tmp_path` + 可注入参数、断言内部逻辑」的隔离测试（`test_clear_cache`）。

---

## 6. 本讲小结

- **`autouse` + `scope='module'` fixture**（[test_data.py:L25-L32](tests/test_data.py#L25-L32)）在模块进入时**自动、只执行一次**地调用 `download_all()` 预热缓存，把「联网下载」从各个测试里剥离出去，让后续断言全部命中本地缓存。
- **`_has_hash`**（[test_data.py:L16-L20](tests/test_data.py#L16-L20)）是一个 5 行的复核辅助：用 `pooch.file_hash` 对**落盘文件**重算 SHA256，与 `registry` 比对，作为「下载内容正确性」的第二道独立防线。
- **`TestDatasets` 类**用 `assert_equal` / `assert_almost_equal` 锁定每个数据集的 shape、dtype 与「黄金统计量」（如 ecg 的均值/标准差），把 u2-l3 讲过的转换结果固化成回归基准。
- **`test_clear_cache`** 示范了离线隔离的范本：`tmp_path` 一次性临时目录 + 自捏的 `dummy_method_map` + `_clear_cache` 的两个可注入参数 `cache_dir`/`method_map`，不联网、不污染真实缓存，逐分支覆盖整棵决策树。
- **为什么能离线测**：根本原因是 u2-l4 里 `_clear_cache`（而非 `clear_cache`）把缓存目录和方法表**参数化**了——这是「为可测性而设计」的典型范例。
- 测试整体呈**两层结构**：在线内容断言（`TestDatasets`）+ 离线逻辑隔离（`test_clear_cache`），各司其职。

---

## 7. 下一步学习建议

- **承接本讲，进入 u3-l4（扩展实战：新增一个数据集）**：那里会把本讲的「测试方案」与注册表、fetcher、`method_files_map`、`@xp_capabilities` 装饰器、meson 安装声明**全部串起来**，让你亲手走一遍「新增数据集 → 同步改注册表 → 写 fetcher → 补测试 → 纳入构建」的全流程。本讲的「综合实践」其实就是 u3-l4 的「测试切片」。
- **横向阅读**：如果你想再看几个「`autouse` fixture 做模块级预热」的真实例子，可以在 SciPy 仓库里搜索 `autouse=True` 与 `scope='module'` 的组合，对比不同子模块对这套手法的运用。
- **深入 pooch**：本讲只用到了 `pooch.file_hash` 与 `pooch.retrieve`/`Pooch.fetch` 的表面。若想理解哈希校验、断点、缓存策略的细节，建议直接阅读 pooch 官方文档中 `Pooch.fetch` 与 `file_hash` 两节，回头再读 `tests/test_data.py` 会有「豁然开朗」之感。
