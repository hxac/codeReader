# 缓存清理机制

## 1. 本讲目标

在前几讲里，我们走完了数据集「登记 → 下载 → 校验 → 解封装」的完整链路：u2-l1 打开了下载黑盒 `fetch_data`，u2-l2 拆解了注册表三件套，u2-l3 看了三种文件格式如何被加载成 ndarray。但还有一件事我们一直没碰——**下载到本地的那些缓存文件，怎么清理？**

本讲就来填上这块拼图。我们将聚焦 `scipy.datasets.clear_cache` 这个对外工具方法，一路追到它的内部实现 `_clear_cache`，看清楚缓存目录是怎么被定位的、又是怎么按「单个 / 多个 / 全部」三种粒度被清掉的。

学完本讲，你应该能够：

- 掌握 `clear_cache` 的公开用法：传单个可调用对象、传列表/元组、或传 `None` 清全部。
- 画出 `_clear_cache` 的**分支决策树**，说清楚它在每种入参下分别走哪条路径、打印什么提示、删什么文件。
- 解释为什么 `_clear_cache` 不直接 `import pooch`，而是用 `platformdirs` 去重建 pooch 用过的那个 `scipy-data` 缓存路径，以及为什么 `ImportError` 的提示文案里写的是 `pooch`。

本讲只讲「怎么删」，不涉及「怎么下」（下载见 u2-l1）、也不涉及注册表三件套本身的构造（见 u2-l2）。我们只会**复用** u2-l2 里那张 `method_files_map` 表。

## 2. 前置知识

### 2.1 缓存（cache）是什么，为什么需要清理

前几讲反复出现一个事实：数据集文件**不随源码发布**，第一次调用某数据集函数时才联网下载，下载后落在本地一个叫 `scipy-data` 的目录里，之后命中缓存就不再重复下载。这个本地目录就是**缓存**。

缓存的代价是**占用磁盘**。三个数据集加起来不大，但在以下场景你会想把它们清掉：

- 想强制重新下载（比如怀疑缓存损坏，或升级了 SciPy 想换新版数据）。
- 在 CI / 容器里跑完测试，想清干净回收空间。
- 单纯想删掉某一个数据集的缓存、保留其余的。

`clear_cache` 就是干这件事的。它和 `ascent / face / electrocardiogram`（取数据）是**相反方向**的操作——一个写缓存，一个删缓存。

### 2.2 缓存路径回顾：pooch.os_cache 与 platformdirs

u1-l3 已经讲过缓存目录随平台而异（macOS 在 `~/Library/Caches`、Linux 在 `~/.cache`、Windows 在 `%LOCALAPPDATA%`）。它是怎么算出来的？

在**下载侧**，[_fetchers.py:L18](_fetchers.py#L18) 把缓存路径交给 pooch：

```python
path=pooch.os_cache("scipy-data"),
```

`pooch.os_cache("scipy-data")` 内部正是借助 `platformdirs`（旧名 `appdirs`）这个库，按各平台惯例选址，再拼上 `scipy-data` 这个子目录。

关键点在于：**清理侧不重新发明轮子**。`_clear_cache` 没有自己硬编码路径，而是用同一个 `platformdirs` 把这条路径**重建**出来，从而保证「下载写到哪」和「清理去哪删」指向**同一个目录**。这是本讲最值得体会的设计对称性。

### 2.3 你需要记住的 method_files_map

u2-l2 讲过第三张表 `method_files_map`，它把**方法名**映射到**文件列表**：

[_registry.py:L22-L26](_registry.py#L22-L26)

```python
method_files_map = {
    "ascent": ["ascent.dat"],
    "electrocardiogram": ["ecg.dat"],
    "face": ["face.dat"]
}
```

本讲里 `_clear_cache` 会把这张表当作「方法名 → 该删哪些缓存文件」的查表依据。如果你还不熟悉这张表，建议先回看 u2-l2 的 4.3 节。

## 3. 本讲源码地图

| 文件 | 本讲关注的部分 | 作用 |
| --- | --- | --- |
| [_utils.py](_utils.py) | 全文（仅 81 行） | **本讲主角**：`clear_cache` 公开入口 + `_clear_cache` 内部分支 + `platformdirs` 降级 |
| [_registry.py](_registry.py) | L22-L26 `method_files_map` | 提供「方法名 → 文件列表」的查表依据（u2-l2 已讲透） |
| [__init__.py](__init__.py) | L82 导入 + L84-L85 `__all__` | 把 `clear_cache` 登记为公开 API |
| [_fetchers.py](_fetchers.py) | L14-L26 `pooch.create`，尤其 L18 `os_cache` | 下载侧选址，与清理侧对称 |
| [tests/test_data.py](tests/test_data.py) | L65-L123 `test_clear_cache` | 用 `tmp_path` + 自定义 `method_map` 离线测试所有分支 |

## 4. 核心概念与源码讲解

### 4.1 clear_cache：公开入口（一层薄壳）

#### 4.1.1 概念说明

`clear_cache` 是面向用户的公开方法。它的代码量极小——几乎只是一个**转发层（thin wrapper）**：接收用户参数，立刻交给内部 `_clear_cache`。它本身不含任何分支逻辑、不碰文件系统。

这种「公开薄壳 + 内部实现」的分层，我们在 u1-l2 已经见过：公开函数定义在带下划线的私有模块里，门面 `__init__.py` 只负责重新导出。这里更进一步——**同一个文件内部**也做了薄壳/实现分离：

- `clear_cache`（公开，固定行为）签名只有 `datasets` 一个参数。
- `_clear_cache`（内部，可测试）多出 `cache_dir`、`method_map` 两个**可注入参数**，专为单元测试留口子。

为什么要这样拆？因为公开用户永远只想清理「真实的 `scipy-data` 缓存」，而测试时你绝不能去删用户真实的缓存目录——测试必须把清理动作隔离到一个临时目录里。两个可注入参数就是这道隔离阀门。

#### 4.1.2 核心流程

公开调用到内部实现的转发流程：

```text
用户调用 datasets.clear_cache(datasets=???)
        │
        ▼
clear_cache(datasets=None)            # 固定签名：只接收 datasets
        │
        ▼
_clear_cache(datasets)                # cache_dir / method_map 用默认值
        │  （cache_dir 默认走 platformdirs，method_map 默认走 method_files_map）
        ▼
   进入 4.2 的分支决策树
```

注意一个**不对称**：`clear_cache` 只把 `datasets` 透传给 `_clear_cache`，`cache_dir` 和 `method_map` 始终保持默认。这就是「公开 API 永远锁定到真实的 scipy-data 缓存与真实的注册表」的原因——用户没有、也不需要绕开这两个默认值的途径。

#### 4.1.3 源码精读

公开入口的定义在文件末尾，[_utils.py:L60-L80](_utils.py#L60-L80)：

```python
@xp_capabilities(out_of_scope=True)
def clear_cache(datasets=None):
    """
    Cleans the SciPy datasets cache directory.

    Parameters
    ----------
    datasets : callable or list/tuple of callable or None
        Dataset whose cached files are to be removed. If None (default), all cached
        files are removed.
    ...
    """
    _clear_cache(datasets)
```

四个观察：

1. **签名只有一个参数** `datasets=None`。默认值 `None` 就是「清全部」。
2. **函数体只有一行** `_clear_cache(datasets)`——纯转发，没有任何额外逻辑。
3. **文档字符串就是公开合约**。它明确告诉用户 `datasets` 可以是「单个 callable、callable 的 list/tuple、或 `None`」，并给出了示例（注意示例里 `clear_cache([datasets.ascent])` 传的是**函数对象本身**，不是字符串 `"ascent"`）。
4. **`@xp_capabilities(out_of_scope=True)` 装饰器**把这个函数标记为「不在数组 API（array API）适配范围内」。`clear_cache` 返回的是 `None`（不返回任何数组），谈论它的数组 API 合规性没有意义，所以被标记为 `out_of_scope`。这个装饰器的完整含义与所有数据集函数的统一标注，将在 u3-l4 详细讲解。

`clear_cache` 是怎么变成公开 API 的？[__init__.py:L82](__init__.py#L82) 把它从私有模块导入门面：

```python
from ._utils import clear_cache
```

并在 [__init__.py:L84-L85](__init__.py#L84-L85) 登记 `__all__`：

```python
__all__ = ['ascent', 'electrocardiogram', 'face',
           'download_all', 'clear_cache']
```

这与 u1-l1 讲过的「名字对外可见 = 相对导入 + `__all__` 登记」完全一致。`clear_cache` 与 `download_all` 并列为两个**工具方法**（一个清理、一个批量下载），区别于三个**数据集方法**。

#### 4.1.4 代码实践

**实践目标**：通过阅读源码与文档字符串，理解 `clear_cache` 是一层纯转发薄壳，并确认它的公开合约。

**操作步骤**：

1. 在能联网、已安装 pooch 的环境里，打开 Python 交互窗口，查看函数签名与文档：
   ```python
   import scipy.datasets
   help(scipy.datasets.clear_cache)
   ```
2. 阅读打印出的 docstring，定位 `Parameters` 一节里对 `datasets` 三种合法取值的描述。
3. 对照本节贴出的 [_utils.py:L60-L80](_utils.py#L60-L80) 源码，确认函数体确实只有 `_clear_cache(datasets)` 一行。
4. 在 `__init__.py` 里确认 `clear_cache` 同时满足「被导入」与「在 `__all__` 里」两个条件。

**需要观察的现象**：

- `help` 输出的参数说明里，明确写出 `callable or list/tuple of callable or None`，且默认 `None`。
- 函数体没有任何文件操作、没有任何 `if` 分支——所有复杂度都在 `_clear_cache` 里。

**预期结果**：你能用自己的话说出「`clear_cache` 是个零逻辑的转发壳，它存在的意义是**固定**公开签名、把可测试性留给带下划线的 `_clear_cache`」，并能指出 `cache_dir` / `method_map` 这两个参数为什么不出现在公开签名里（因为公开用户永远操作真实缓存，只有测试需要注入）。

> 说明：`help()` 的输出取决于你本地安装的 SciPy 版本，本讲引用的是 HEAD `5f09bd71` 的源码（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `clear_cache` 的公开签名里没有 `cache_dir` 参数，而 `_clear_cache` 却有？

**参考答案**：因为公开用户清理的永远是「真实的 `scipy-data` 缓存」，路径应由库自己用 `platformdirs` 算出来，不应暴露给用户。而 `_clear_cache` 多出的 `cache_dir`（以及 `method_map`）是**为单元测试留的注入阀门**——测试要把它指向一个临时目录、用一份假的 `method_map`，从而在不碰用户真实缓存、不联网的前提下验证各分支。

**练习 2**：`clear_cache` 的 docstring 示例里写的是 `datasets.clear_cache([datasets.ascent])`，如果有人误写成 `datasets.clear_cache(["ascent"])`（传字符串），会发生什么？

**参考答案**：内部 `_clear_cache` 会对每个元素 `assert callable(dataset)`（见 4.2.3 的 [_utils.py:L38](_utils.py#L38)）。字符串不是可调用对象，因此会触发 `AssertionError`。这正解释了为什么合约要求传**函数对象本身**——清理逻辑要靠函数的 `__name__` 属性去查表。

---

### 4.2 _clear_cache：内部分支决策树

#### 4.2.1 概念说明

`_clear_cache` 是缓存清理的**真正大脑**。它接收三个参数：

- `datasets`：要清理什么（`None` 表示清全部）。
- `cache_dir`：去哪个目录清（默认 `None`，由 `platformdirs` 算出，4.3 节详讲）。
- `method_map`：方法名 → 文件列表的查表（默认 `None`，用 `_registry.py` 的 `method_files_map`）。

它的核心是一棵**分支决策树**：先把两个可注入参数补上默认值，再判断「缓存目录存不存在」，最后按 `datasets` 是 `None` 还是具体方法列表，分别走「整目录删除」或「逐文件删除」两条路。

理解这棵树的关键是抓住三条主线：

1. **目录级 vs 文件级**：`datasets=None` 时整目录 `rmtree`；指定方法时只删该方法的若干文件。
2. **方法名 → 文件名**：靠 `method_map`（默认 `method_files_map`）翻译，方法名查不到就 `ValueError`。
3. **容错提示**：目录不存在、文件不存在时都**不报错**，只打印一句「Nothing to clear」——清理是幂等的、对用户友好的。

#### 4.2.2 核心流程

完整的分支决策树（这是本讲最重要的一张图）：

```text
_clear_cache(datasets, cache_dir=None, method_map=None)
        │
        ├─① method_map 缺省？  → method_map = method_files_map   # 补默认查表
        │
        ├─② cache_dir 缺省？  → 见 4.3：platformdirs 算路径
        │      └─ platformdirs 也没装？ → raise ImportError（提示装 pooch）
        │
        ├─③ cache_dir 不存在？ → print "doesn't exist. Nothing to clear."，return
        │
        ├─④ datasets is None？ → print "Cleaning the cache directory ..."
        │      └─ shutil.rmtree(cache_dir)          【整目录删除：清全部】
        │
        └─⑤ 否则（清理指定方法）：
              ├─ datasets 不是 list/tuple？ → datasets = [datasets]   # 归一化为列表
              └─ for dataset in datasets:
                    ├─ assert callable(dataset)
                    ├─ name = dataset.__name__
                    ├─ name 不在 method_map？ → raise ValueError      【防传错方法】
                    ├─ files = method_map[name]                       # 取文件列表
                    └─ for file in files:
                          └─ cache_dir/file 存在？ → os.remove + 提示
                                                   ↘ 不存在 → print "Nothing to clear"
```

五条主线一一对应源码里的代码块（见 4.2.3）。建议你把这棵树和源码对照着读两遍——这是 `clear_cache` 一切行为的源头。

注意几个**对称设计**：

- ③ 和 ⑤ 的「不存在」分支都选择**打印而非报错**，保证「无论缓存是否在，调用都不崩」。
- ④ 用 `shutil.rmtree` 删**目录**，⑤ 用 `os.remove` 删**单个文件**——粒度不同，工具也不同。
- ⑤ 里 `method_map` 默认是 `method_files_map`，正因为它的值是**列表**，「一个方法对应多个文件」的删除路径天然成立（u2-l2 已铺垫，测试里有专门用例）。

#### 4.2.3 源码精读

整个 `_clear_cache` 在 [_utils.py:L13-L57](_utils.py#L13-L57)。我们按决策树的五步拆开看。

**① 补 method_map 默认值**，[_utils.py:L13-L16](_utils.py#L13-L16)：

```python
def _clear_cache(datasets, cache_dir=None, method_map=None):
    if method_map is None:
        # Use SciPy Datasets method map
        method_map = method_files_map
```

`method_files_map` 在文件顶部 [_utils.py:L3](_utils.py#L3) 由 `from ._registry import method_files_map` 引入。

**② 补 cache_dir 默认值**（4.3 节细讲），[_utils.py:L17-L24](_utils.py#L17-L24)：

```python
    if cache_dir is None:
        # Use default cache_dir path
        if platformdirs is None:
            # platformdirs is pooch dependency
            raise ImportError("Missing optional dependency 'pooch' required "
                              "for scipy.datasets module. Please use pip or "
                              "conda to install 'pooch'.")
        cache_dir = platformdirs.user_cache_dir("scipy-data")
```

注意：这个 `ImportError` **只在 `cache_dir is None` 分支里**才会触发。如果调用方显式传了 `cache_dir`（测试就这么干），`platformdirs` 哪怕没装也不会被碰到——这正是测试能离线运行的秘诀。

**③ 缓存目录不存在直接返回**，[_utils.py:L26-L28](_utils.py#L26-L28)：

```python
    if not os.path.exists(cache_dir):
        print(f"Cache Directory {cache_dir} doesn't exist. Nothing to clear.")
        return
```

**④ datasets 为 None：整目录删除**，[_utils.py:L30-L32](_utils.py#L30-L32)：

```python
    if datasets is None:
        print(f"Cleaning the cache directory {cache_dir}!")
        shutil.rmtree(cache_dir)
```

`shutil.rmtree` 递归删除整个目录树。走到这里意味着「用户调用了 `clear_cache()` 不带参数，要清空所有数据集缓存」。

**⑤ 清理指定方法：归一化 + 查表 + 逐文件删**，[_utils.py:L33-L57](_utils.py#L33-L57)：

```python
    else:
        if not isinstance(datasets, list | tuple):
            # single dataset method passed should be converted to list
            datasets = [datasets, ]
        for dataset in datasets:
            assert callable(dataset)
            dataset_name = dataset.__name__  # Name of the dataset method
            if dataset_name not in method_map:
                raise ValueError(f"Dataset method {dataset_name} doesn't "
                                 "exist. Please check if the passed dataset "
                                 "is a subset of the following dataset "
                                 f"methods: {list(method_map.keys())}")

            data_files = method_map[dataset_name]
            data_filepaths = [os.path.join(cache_dir, file)
                              for file in data_files]
            for data_filepath in data_filepaths:
                if os.path.exists(data_filepath):
                    print("Cleaning the file "
                          f"{os.path.split(data_filepath)[1]} "
                          f"for dataset {dataset_name}")
                    os.remove(data_filepath)
                else:
                    print(f"Path {data_filepath} doesn't exist. "
                          "Nothing to clear.")
```

这段把前面几讲的知识点全串起来了：

- [_utils.py:L34-L36](_utils.py#L34-L36)：单个 callable 被包成单元素列表，于是「传一个」和「传一个列表」后续走同一套循环——这就是为什么 `clear_cache(datasets.ascent)` 与 `clear_cache([datasets.ascent])` 等价。
- [_utils.py:L38](_utils.py#L38)：`assert callable(dataset)` 兜底，挡住字符串等错误入参（呼应 4.1.5 的练习 2）。
- [_utils.py:L39](_utils.py#L39)：`dataset.__name__` 取出方法名（如 `"ascent"`）。这是「必须传函数对象」的根本原因。
- [_utils.py:L40-L44](_utils.py#L40-L44)：方法名查不到就 `ValueError`，并把合法方法名列表 `list(method_map.keys())` 拼进报错信息，方便用户自查。
- [_utils.py:L46](_utils.py#L46)：`data_files = method_map[dataset_name]`——这就是 u2-l2 第三张表在此处的**唯一消费点**。例如查 `"electrocardiogram"` 得到 `["ecg.dat"]`，于是删的是 `ecg.dat`（方法名 ≠ 文件名，靠这张表弥合）。
- [_utils.py:L49-L57](_utils.py#L49-L57)：逐文件 `os.remove`；不存在则打印「Nothing to clear」而不报错。`os.path.split(...)[1]` 只取文件名用于提示，更友好。

#### 4.2.4 代码实践（源码阅读型）

**实践目标**：通过阅读 `test_clear_cache`，看测试如何用「注入 `cache_dir` + 自定义 `method_map`」把五条分支全部离线覆盖，并据此理解每个分支的预期行为。

**操作步骤**：

1. 打开 [tests/test_data.py:L65-L123](tests/test_data.py#L65-L123)，通读 `test_clear_cache`。注意它用了 pytest 内置的 `tmp_path` fixture（一个一次性临时目录，测试结束自动清理），所以全程**不碰真实缓存、不联网**。
2. 定位它在 `tmp_path` 下手工造出的「假缓存目录 + 假 method_map」，[tests/test_data.py:L67-L78](tests/test_data.py#L67-L78)：
   ```python
   dummy_basepath = thread_basepath / "dummy_cache_dir"
   dummy_basepath.mkdir()
   dummy_method_map = {}
   for i in range(4):
       dummy_method_map[f"data{i}"] = [f"data{i}.dat"]
       data_filepath = dummy_basepath / f"data{i}.dat"
       data_filepath.write_text("")
   ```
3. 把测试里的五次 `_clear_cache(...)` 调用，逐一对应到 4.2.2 决策树的五条分支：
   - [L82-L86](tests/test_data.py#L82-L86)：传单个 callable `data0` → 命中⑤的「归一化为列表」分支，删 `data0.dat`。
   - [L94-L97](tests/test_data.py#L94-L97)：传列表 `[data1, data2]` → ⑤的多方法循环。
   - [L101-L111](tests/test_data.py#L101-L111)：`dummy_method_map["data4"] = ["data4_0.dat", "data4_1.dat"]` → ⑤的「一个方法多文件」分支。
   - [L115-L119](tests/test_data.py#L115-L119)：传 `data5`（不在 map 里）→ 命中⑤的 `ValueError` 分支，用 `pytest.raises(ValueError)` 断言抛错。
   - [L122-L123](tests/test_data.py#L122-L123)：传 `None` → 命中④，`rmtree` 整个 `dummy_basepath`。

**需要观察的现象**：

- 测试**从不调用公开的 `clear_cache`**，而是直接调内部的 `_clear_cache`，并显式传 `cache_dir=dummy_basepath`、`method_map=dummy_method_map`——这正是 4.1.5 练习 1 所说的「注入阀门」的用途。
- 因为传了 `cache_dir`，[_utils.py:L17-L24](_utils.py#L17-L24) 那段 `platformdirs` 逻辑被整个跳过，测试不依赖 pooch 是否真的装了（注意文件顶部还有 `pooch = pytest.importorskip("pooch")`，但那只决定整个测试文件要不要跑）。

**预期结果**：你能对着决策树，逐条解释测试为什么要这么构造数据，并能说出「`_clear_cache` 的两个可注入参数是这段离线测试得以成立的前提」。

> 说明：本实践为**源码阅读型**，不需要运行命令；若想实际跑该用例，可执行 `python -m pytest scipy/datasets/tests/test_data.py::test_clear_cache`（需安装 pooch 与 pytest，待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：如果缓存目录里**某个方法的文件已经被人手动删掉了**，再调用 `clear_cache([datasets.face])` 会抛异常吗？

**参考答案**：不会。决策树⑤的 [_utils.py:L49-L57](_utils.py#L49-L57) 对每个文件先 `os.path.exists` 判断：存在才 `os.remove`，不存在只打印 `f"Path {data_filepath} doesn't exist. Nothing to clear."`。所以「文件已不在」是优雅降级，不报错。

**练习 2**：为什么 `_clear_cache` 在 `datasets is None`（清全部）时用 `shutil.rmtree(cache_dir)` 删整个目录，而不是遍历 `method_map` 逐个删文件？

**参考答案**：因为「全部」意味着缓存里**任何文件**都该清掉，包括将来可能新增、暂未登记进 `method_map` 的文件。`rmtree` 直接抹掉整个目录，天然覆盖所有内容，不依赖注册表的完整性；而逐文件删只能删 `method_map` 里登记过的，会有遗漏风险。两条路径的粒度选择（目录级 vs 文件级）正是为了匹配「全部」与「指定」两种语义。

---

### 4.3 platformdirs：缓存路径的兜底解析

#### 4.3.1 概念说明

4.2 的决策树第②步提到，`cache_dir` 缺省时要靠 `platformdirs` 把缓存路径算出来。本节就专门拆这一步。

这里有一个**反直觉但很关键**的设计：`_clear_cache` 通篇**没有 `import pooch`**。清理缓存严格来说只需要知道「目录在哪 + 删文件」，并不需要 pooch 的下载/校验能力。那它怎么知道目录在哪？答案是直接用 `platformdirs`——正是 pooch 内部 `os_cache` 所依赖的同一个库——把那条路径**重建**出来。

于是出现了一个微妙的依赖关系：

- 清理侧真正需要的可选依赖是 **`platformdirs`**。
- 但 `platformdirs` 是 **`pooch` 的依赖**（源码注释 [_utils.py:L20](_utils.py#L20) 写得很直白：`# platformdirs is pooch dependency`）。也就是说，只要用户装了 `pooch`，`platformdirs` 一般也跟着装上了。
- 所以当 `platformdirs` 缺失时，报错文案里写的是 `'pooch'` 而非 `'platformdirs'`——因为「装 pooch」是用户视角更熟悉、也更正确的动作（装了 pooch 自然就有了 platformdirs）。

这是「**用户友好优先**」的错误信息设计：报错指向用户真正该安装的那个包。

#### 4.3.2 核心流程

缓存路径的解析与对称性：

```text
【下载侧 _fetchers.py】
pooch.create(path=pooch.os_cache("scipy-data"), ...)
                │
                └─ pooch.os_cache 内部 → platformdirs 选址 + "scipy-data"

【清理侧 _utils.py】
cache_dir 缺省
   └─ platformdirs 是否为 None？
         ├─ 是 → raise ImportError("... install 'pooch' ...")
         └─ 否 → cache_dir = platformdirs.user_cache_dir("scipy-data")
                       │
                       └─ 与下载侧指向【同一个】scipy-data 目录
```

两侧用同一个 `platformdirs`、同一个 `"scipy-data"` 参数，保证「写到哪」与「去哪删」**完全对齐**。这正是 u1-l3 提到的各平台缓存路径（macOS `~/Library/Caches/scipy-data`、Linux `~/.cache/scipy-data`、Windows `%LOCALAPPDATA%\...\scipy-data\Cache`）的统一来源。

> 说明：`pooch.os_cache` 与 `platformdirs.user_cache_dir` 在不同 pooch / platformdirs 版本上拼接出的子目录结构（尤其是 Windows 下的 `Cache` 后缀）可能略有差异；两侧只要用同一套库版本就会保持一致。具体路径请以本地实际生成为准（待本地验证）。

#### 4.3.3 源码精读

`platformdirs` 的可选依赖降级在文件顶部 [_utils.py:L7-L10](_utils.py#L7-L10)：

```python
try:
    import platformdirs
except ImportError:
    platformdirs = None  # type: ignore[assignment]
```

这是 u3-l1 会系统讲解的「try-import-or-None」降级惯用法：导入失败不直接报错，而是把名字置为 `None`，留到真正用到时再抛友好的 `ImportError`。

真正用到它的地方就是 4.2.3 拆过的 [_utils.py:L17-L24](_utils.py#L17-L24)，关键两行是：

```python
if platformdirs is None:
    # platformdirs is pooch dependency
    raise ImportError("Missing optional dependency 'pooch' required "
                      "for scipy.datasets module. Please use pip or "
                      "conda to install 'pooch'.")
cache_dir = platformdirs.user_cache_dir("scipy-data")
```

把这段和下载侧的 [_fetchers.py:L18](_fetchers.py#L18) 并排看，对称性一目了然：

```python
# 下载侧：pooch 借 platformdirs 选址
path=pooch.os_cache("scipy-data"),
```

两者都用 `"scipy-data"` 作为应用名传给 `platformdirs`，所以算出来的是同一个目录。

还有一个常被忽略的细节：**报错的触发位置**。`ImportError` 只在 `cache_dir is None` 这个 `if` 内部（[_utils.py:L19-L23](_utils.py#L19-L23)）。这意味着——如果一个调用者**显式传了 `cache_dir`**，那么即使 `platformdirs` 没装，这段代码也根本不会被执行到，自然不会报错。测试 `test_clear_cache` 正是利用这一点（它每次都传 `cache_dir=dummy_basepath`），所以即使 `platformdirs` 缺失，测试逻辑本身也能跑（前提是测试文件没被顶部的 `pytest.importorskip("pooch")` 跳过）。

最后对照 `__init__.py` 模块文档里给出的缓存路径说明，[__init__.py:L56-L68](__init__.py#L56-L68) 明确写了三个平台的位置，与本节解析出的路径一致——文档与实现互为印证。

#### 4.3.4 代码实践

**实践目标**：亲手看到 `platformdirs.user_cache_dir("scipy-data")` 算出的路径，并确认它与 pooch 实际写缓存的目录一致。

**操作步骤**：

1. 在已安装 pooch 的环境里，先用一个数据集触发下载，生成缓存：
   ```python
   import scipy.datasets
   arr = scipy.datasets.ascent()   # 首次会联网下载 ascent.dat
   ```
2. 分别用两条途径打印缓存路径，对比是否一致：
   ```python
   import platformdirs
   print("platformdirs :", platformdirs.user_cache_dir("scipy-data"))

   from scipy.datasets._fetchers import data_fetcher
   print("pooch path   :", data_fetcher.path)
   ```
3. 用系统命令确认上一步算出的目录里确实有 `ascent.dat`（Linux 示例）：
   ```bash
   ls -la "$HOME/.cache/scipy-data"
   ```
4. （可选）设一个自定义 `XDG_CACHE_HOME`（仅 Linux/macOS 受影响）再重复第 2 步，观察路径变化。

**需要观察的现象**：

- 第 2 步两个打印值**指向同一个目录**——证明下载侧与清理侧用同一个 `platformdirs` 选址。
- 第 3 步能在该目录下看到 `ascent.dat`（以及可能存在的 `ecg.dat` / `face.dat`）。
- 第 4 步（若做）路径会随 `XDG_CACHE_HOME` 改变，说明选址确实受平台/环境变量驱动。

**预期结果**：你能说清楚「`clear_cache` 默认清理的目录 = `platformdirs.user_cache_dir("scipy-data")` = `data_fetcher.path`（即 `pooch.os_cache("scipy-data")`）」，三者同源。

> 说明：不同操作系统下第 2、3 步的具体路径不同（见 u1-l3 与 `__init__.py` 文档）；Windows 下 pooch/platformdirs 可能额外拼接 `Cache` 子目录，以本地实际输出为准（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：`_clear_cache` 既不 `import pooch`，又能在 `platformdirs` 缺失时报「请安装 pooch」，这是否矛盾？

**参考答案**：不矛盾。清理逻辑本身只需要 `platformdirs` 来算路径，不需要 pooch 的下载能力；所以源码只 `import platformdirs`。但 `platformdirs` 是 `pooch` 的依赖，用户视角下「装 pooch」是更自然、也必然能带上 `platformdirs` 的动作，所以报错文案指向 `pooch`。这是「报错指向用户真正该装的包」的友好设计，源码注释 `# platformdirs is pooch dependency` 点明了这层关系。

**练习 2**：假如某用户只装了 `platformdirs`、没装 `pooch`，调用 `scipy.datasets.clear_cache()`（清全部，缓存目录存在）会发生什么？

**参考答案**：顶部 [_utils.py:L7-L10](_utils.py#L7-L10) 的 `import platformdirs` 会成功（`platformdirs` 不是 `None`）；于是 `cache_dir is None` 分支里不会抛 `ImportError`，而是正常算出路径；接着决策树走③/④，若该目录存在就 `shutil.rmtree` 删除。也就是说——**清理侧并不强依赖 pooch**，只要 `platformdirs` 在，`clear_cache` 就能工作。但实践中用户通常是「装 pooch 顺带得到 platformdirs」，所以这种「只有 platformdirs 没 pooch」的组合很少见。

---

## 5. 综合实践

**实践目标**：把本讲三个最小模块（公开入口、内部分支、路径解析）串成一条完整的动手链路——先生成缓存，再分别用「单个 / 全部」两种粒度清理，并沿途观察每一步的提示信息，最后复现一次「方法名查不到」的 `ValueError`。

**操作步骤**：

1. **准备环境**：确认已安装 pooch（`pip install pooch`），并在能联网的环境里启动 Python。
2. **生成缓存**：调用一次 `ascent` 触发下载，并记录缓存目录：
   ```python
   import scipy.datasets as datasets
   import platformdirs
   from scipy.datasets._fetchers import data_fetcher

   arr = datasets.ascent()
   cache_dir = data_fetcher.path
   print("缓存目录：", cache_dir)
   import os
   print("ascent.dat 是否存在：", os.path.exists(os.path.join(cache_dir, "ascent.dat")))
   ```
3. **清理单个数据集**（注意传的是**函数对象**的列表）：
   ```python
   datasets.clear_cache([datasets.ascent])
   ```
   仔细阅读打印的提示，它应当形如 `Cleaning the file ascent.dat for dataset ascent`。
4. **再清一次同一个**，观察「文件已不存在」的容错提示：
   ```python
   datasets.clear_cache([datasets.ascent])
   ```
   此时缓存里的 `ascent.dat` 已被第 3 步删掉，预期打印 `... doesn't exist. Nothing to clear.`，且**不报错**。
5. **重新生成后再清全部**：先重新 `datasets.face()` 下载 `face.dat`，然后不带参数清全部：
   ```python
   datasets.face()
   datasets.clear_cache()
   ```
   预期打印 `Cleaning the cache directory ...!`，随后整个 `scipy-data` 目录被删除。
6. **验证「全部已清」**：再次不带参数调用，观察决策树③的提示：
   ```python
   datasets.clear_cache()
   ```
   预期打印 `Cache Directory ... doesn't exist. Nothing to clear.`（目录刚被 `rmtree` 删了）。
7. **复现 ValueError**（小心：这会真的尝试清理，但因为方法名是假的，会在删文件之前就抛错，不会误删）：
   ```python
   def not_a_real_dataset():
       pass
   try:
       datasets.clear_cache([not_a_real_dataset])
   except ValueError as e:
       print("捕获到 ValueError：", e)
   ```
   预期报错信息里会把合法方法名列表 `['ascent', 'electrocardiogram', 'face']` 打印出来，提示你传错了。

**需要观察的现象**：

| 步骤 | 命中决策树分支 | 预期提示/行为 |
| --- | --- | --- |
| 2 | （下载侧，不在决策树内） | 生成 `ascent.dat` |
| 3 | ⑤ 逐文件删除 | `Cleaning the file ascent.dat for dataset ascent` |
| 4 | ⑤ 文件不存在 | `... doesn't exist. Nothing to clear.`（不报错） |
| 5 | ④ 整目录删除 | `Cleaning the cache directory ...!` |
| 6 | ③ 目录不存在 | `Cache Directory ... doesn't exist. Nothing to clear.` |
| 7 | ⑤ 方法名查不到 | 抛 `ValueError`，含合法方法名列表 |

**预期结果**：你将亲眼看到 `_clear_cache` 决策树的 ③④⑤ 三条分支被依次触发，并确认「清理是幂等且对用户友好的（不存在的目录/文件都不报错）」。同时你会体会到：第 3 步传 `[datasets.ascent]` 与传 `datasets.ascent`（单个 callable）效果相同——因为 [_utils.py:L34-L36](_utils.py#L34-L36) 会把单个 callable 归一化为列表。

> 说明：本实践依赖联网下载与已安装的 pooch；各步打印的绝对路径因平台而异。若处于断网环境，可改为只做第 7 步（`ValueError` 不依赖缓存存在）或参考 4.2.4 的离线源码阅读实践（待本地验证）。

## 6. 本讲小结

- `clear_cache` 是一层**纯转发薄壳**（[_utils.py:L60-L80](_utils.py#L60-L80)）：公开签名只有 `datasets`，函数体仅一行 `_clear_cache(datasets)`；复杂度与可测试性全部下沉到带下划线的 `_clear_cache`，后者多出 `cache_dir` / `method_map` 两个**可注入参数**。
- `_clear_cache` 是一棵**五步决策树**：补 `method_map` 默认值 → 补 `cache_dir` 默认值 → 目录不存在则提示返回 → `datasets is None` 则 `shutil.rmtree` 整目录 → 否则归一化为列表、按 `method_map` 逐文件 `os.remove`。
- 「方法名 → 文件名」的翻译唯一发生在 [_utils.py:L46](_utils.py#L46) `data_files = method_map[dataset_name]`，这是 u2-l2 第三张表 `method_files_map` 在缓存清理中的**唯一消费点**；方法名取自函数 `__name__`，所以必须传**函数对象本身**。
- 清理是**幂等且友好**的：目录不存在、文件不存在、方法名查不到三种异常情形里，前两者只打印「Nothing to clear」不报错，只有方法名查不到才抛 `ValueError`（并把合法方法名列表写进报错）。
- `clear_cache` **不 import pooch**，而是用 `platformdirs.user_cache_dir("scipy-data")` 重建 pooch 用过的同一条缓存路径；`platformdirs` 是 pooch 的依赖，故缺失时报错文案指向 `pooch`，且该 `ImportError` 只在 `cache_dir is None` 分支才会触发——这正是测试能用 `tmp_path` 离线隔离的秘诀。

## 7. 下一步学习建议

- 本讲标志着第二单元（核心机制）的收尾。进入第三单元前，建议你回头把 u2-l1 / u2-l2 / u2-l3 / 本讲连起来想一遍：**注册表三件套描述数据 → fetch_data 下载数据 → 各 loader 解封装数据 → clear_cache 清理数据**，这就是 `scipy.datasets` 的完整生命周期。
- 下一单元 **u3-l1「可选依赖的降级处理模式」**会把本讲看到的 `try: import platformdirs except ImportError: platformdirs = None`（[_utils.py:L7-L10](_utils.py#L7-L10)）系统化，对比 `_fetchers` / `_utils` / `_download_all` 三处降级实现的异同，讲清「用到才报错」的设计动机。
- 对测试设计感兴趣的读者，可直接结合本讲 4.2.4 阅读完整的 [tests/test_data.py:L65-L123](tests/test_data.py#L65-L123)，并在 **u3-l3「测试设计剖析」**里看到更系统的解读（autouse fixture、`_has_hash`、`dummy_method_map` 隔离测试）。
- 想把缓存清理与「新增一个数据集」连起来实战，可看 **u3-l4「扩展实战：新增一个数据集」**——你会发现新增数据集时必须同步更新 `method_files_map`，否则 `clear_cache` 永远清不到新数据集的文件。
