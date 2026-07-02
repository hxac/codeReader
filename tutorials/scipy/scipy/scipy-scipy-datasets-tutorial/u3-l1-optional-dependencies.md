# 可选依赖的降级处理模式

> 讲义 id：`u3-l1` ｜ 阶段：advanced ｜ 依赖：`u2-l1`、`u2-l4`

## 1. 本讲目标

学完本讲后，你应当能够：

1. 说清楚什么是「可选依赖（optional dependency）」，以及为什么一个库要把它「降级」而不是「强制安装」。
2. 掌握 `try: import xxx / except ImportError: xxx = None` 这一 Python 惯用法，理解它把「失败」从导入时推迟到使用时的设计意图。
3. 结合真实源码，逐行读懂 `scipy.datasets` 在 `_fetchers.py`、`_utils.py`、`_download_all.py` 三处对 `pooch`、`platformdirs` 的降级实现。
4. 对比这三处降级的相同点与不同点，并能独立判断「在 pooch 缺失的环境里调用某个函数会发生什么、报什么错」。
5. 仿照该模式，为自己的一段代码加上对某个可选库的降级保护。

本讲是承接 `u2-l1`（`fetch_data` 黑盒）与 `u2-l4`（缓存清理）的「专家视角」补充：前两讲你已经知道这些函数「用到 pooch / platformdirs」，本讲回答「当它们不在时会怎样、为什么是这样」。

## 2. 前置知识

在进入源码之前，先用通俗语言把几个术语讲清楚。

**依赖（dependency）**：你的代码 `import` 了别的库，那个库就是你的依赖。`scipy.datasets` 需要 `pooch` 来联网下载数据集文件，所以 `pooch` 是它的依赖。

**可选依赖（optional dependency）**：有些功能没有某个库也能正常工作，只有「用到那个特定功能」时才需要它。对 `scipy.datasets` 来说：

- 你只是 `import scipy.datasets` —— 不需要 `pooch`；
- 你调用 `scipy.datasets.face()` 去真正下载数据 —— 才需要 `pooch`。

把这类「用到才需要」的库称为可选依赖。它在 `pyproject.toml` 里通常写在 `[project.optional-dependencies]` 下，而不是强制的 `[project.dependencies]`。

**ImportError**：Python 在 `import` 一个找不到的模块时抛出的异常。本讲的降级模式正是「抓住这个异常」而不是让它直接冒泡。

**try/except**：Python 的异常捕获语法。

```python
try:
    import pooch        # 尝试导入
except ImportError:     # 抓住「导入失败」
    pooch = None        # 不报错，只是把它记成 None
```

**传递依赖（transitive dependency）**：如果 `pooch` 自己又依赖 `platformdirs`，那么你装了 `pooch`，`platformdirs` 通常也就跟着有了。后面你会看到 `_utils.py` 正是利用了这一点。

**两种失败策略的对比**——这是本讲的核心直觉：

| 策略 | 行为 | 用户体验 |
| --- | --- | --- |
| 导入即失败（fail-fast / eager） | `import scipy.datasets` 时就因为缺 `pooch` 而崩溃 | 连「不打算下载数据」的用户也用不了 |
| 用到才报错（lazy） | 导入永远成功，只在真正调用下载时才报错 | 不下载数据的用户完全无感；要下载的用户得到一条清晰提示 |

`scipy.datasets` 选择了后者。理解「为什么选后者」是本讲的一条主线。

## 3. 本讲源码地图

本讲聚焦三个文件的「导入段 + 用到时的检查段」：

| 文件 | 降级的对象 | 降级后记成 | 在哪里「用到才报错」 |
| --- | --- | --- | --- |
| [_fetchers.py](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_fetchers.py) | `pooch` | `pooch = None` 且 `data_fetcher = None` | `fetch_data` 函数体开头 |
| [_utils.py](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_utils.py) | `platformdirs` | `platformdirs = None` | `_clear_cache` 解析缓存目录时 |
| [_download_all.py](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_download_all.py) | `pooch` | `pooch = None` | `download_all` 函数体开头 |

三个文件都不约而同地做了同一件事：**导入时静默吞掉 ImportError，把模块置为 `None`；在使用处检查这个 `None`，若为 `None` 则抛出一条统一的、友好的 `ImportError`**。本讲就是要把这套「统一模式」拆透。

## 4. 核心概念与源码讲解

### 4.1 核心概念：try-import-or-None 惯用法与「用到才报错」

#### 4.1.1 概念说明

这个惯用法的名字可以概括为 **try-import-or-None**：尝试导入，导入失败就把它设成 `None`。它把「依赖是否存在」这件事从一个「二选一的崩溃」变成了一个「运行时才知道的状态」。

它解决的核心问题是：**如何让一个库既能优雅地运行在「缺依赖」的环境里，又能在用户真正需要那个依赖时给出明确提示。**

设想反面：如果 `scipy.datasets` 在文件顶部直接写 `import pooch`，那么任何人 `import scipy.datasets` 都会立刻因为缺 `pooch` 而崩溃。但 `scipy` 是一个庞大的科学计算库，很多用户只用 `scipy.linalg`、`scipy.signal`，根本碰不到数据集功能——为这部分用户强制安装 `pooch`、甚至因为没装而让整个 `import scipy` 链路受影响，是不合理的。

惯用法把「能不能用」拆成两段：

- **导入段（import-time，静默）**：只判断「依赖在不在」，不判断「该不该报错」。
- **使用段（call-time，报错）**：真正要用依赖时才检查 `None`，此时报错才「合情合理」。

#### 4.1.2 核心流程

整个模式可以用两个阶段、一个哨兵值（sentinel）`None` 来描述：

```
┌─────────────────────────────┐
│ 阶段一：模块导入时（静默）     │
│  try: import DEP             │
│  except ImportError:         │
│      DEP = None   ← 哨兵值    │
└─────────────────────────────┘
            │
            │  模块加载完成，没有崩溃
            ▼
┌─────────────────────────────┐
│ 阶段二：函数被调用时（报错）   │
│  def f():                    │
│      if DEP is None:         │
│          raise ImportError(  │
│            "…请安装…")        │
│      # 真正使用 DEP           │
└─────────────────────────────┘
```

判定的状态机只有两种状态：

- 若 `DEP is None` → 进入「不可用」分支，抛友好 `ImportError`；
- 否则 → 正常使用。

注意一个关键设计：**哨兵值选 `None` 而不是 `False`**。原因是 `None` 在 Python 里天然表示「没有这个东西」，而且 `if DEP is None` 的写法比 `if DEP is False` 在语义上更准确——我们想表达的是「这个对象不存在」，而不是「它是一个布尔假值」。`is None` 也是 Python 官方推荐的「判空」写法，性能与可读性都好。

#### 4.1.3 源码精读

三处降级的「导入段」结构几乎一模一样。先看 [_fetchers.py:L8-L13](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_fetchers.py#L8-L13) ——这段代码在导入 `pooch` 失败时，同时把 `pooch` 和 `data_fetcher` 都置为 `None`（`data_fetcher` 是稍后真正干活的 pooch 单例）：

```python
try:
    import pooch
except ImportError:
    pooch = None
    data_fetcher = None
else:
    data_fetcher = pooch.create(...)   # 成功才创建单例
```

再看 [_utils.py:L7-L10](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_utils.py#L7-L10)，对 `platformdirs` 的处理更加简洁——只置 `None`，没有 `else` 分支（因为它不需要在导入时立即创建对象）：

```python
try:
    import platformdirs
except ImportError:
    platformdirs = None
```

以及 [_download_all.py:L12-L15](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_download_all.py#L12-L15)，又是一模一样的 `pooch` 降级：

```python
try:
    import pooch
except ImportError:
    pooch = None
```

三个导入段的共同点：**短、对称、只用 `ImportError`（而不是裸 `except:`）**。这一点很重要——只用 `ImportError` 意味着「我只想吞掉『模块不存在』这一种错误」；如果 `pooch` 存在但它自身有 bug（比如 `SyntaxError`、`AttributeError`），那些错误仍会正常冒泡，不会被误吞，方便排查。

#### 4.1.4 代码实践

**实践目标**：用一个「假的可选依赖」最小化复现这套惯用法，直观感受「导入不报错、用到才报错」。

**操作步骤**：

1. 新建一个文件 `optdemo.py`（示例代码，非项目源码），写入：

   ```python
   # 示例代码
   try:
       import nonexistent_lib  # 一个肯定不存在的模块
   except ImportError:
       nonexistent_lib = None

   def use_it():
       if nonexistent_lib is None:
           raise ImportError("缺少可选依赖 nonexistent_lib，请先安装。")
       return nonexistent_lib.something()
   ```

2. 在能运行 Python 的环境里执行：

   ```bash
   python -c "import optdemo; print('导入成功，没报错')"
   python -c "import optdemo; optdemo.use_it()"
   ```

**需要观察的现象**：第一条命令正常打印「导入成功」，证明导入段是静默的；第二条命令抛出 `ImportError` 且文案正是你在函数里写的那句，证明报错被推迟到了调用时。

**预期结果**：第二条命令的输出形如 `ImportError: 缺少可选依赖 nonexistent_lib，请先安装。`。这就是「用到才报错」的最小形态。（不同 Python 版本 traceback 前缀略有差异，以本地实际输出为准。）

#### 4.1.5 小练习与答案

**练习 1**：如果把 `except ImportError:` 改成裸 `except:`，会有什么潜在危害？

**参考答案**：裸 `except:` 会吞掉**所有**异常。如果 `pooch` 这个包确实存在，但它在自身导入过程中抛了别的错误（例如它依赖的另一个子模块有 `SyntaxError`），裸 `except` 也会把它当成「pooch 不存在」静默置成 `None`，导致后面报「请安装 pooch」，可实际上用户已经装了——错误信息会非常误导。限定 `except ImportError:` 才能保证只处理「模块找不到」这一种真实情况。

**练习 2**：为什么哨兵值用 `None` 而不是用一个布尔变量 `has_pooch = False`？

**参考答案**：用 `None` 可以让「未安装」和「安装了的对象」用同一个名字 `pooch` 表达，代码里只需 `if pooch is None` 一处判断，且后续 `pooch.create(...)`、`pooch.HTTPDownloader(...)` 等调用天然地复用这个名字；若改用布尔标志位，则需要同时维护 `pooch` 对象和 `has_pooch` 两个变量，容易写错。`None` 还能直接作为函数的默认参数（见 4.2 节 `data_fetcher=data_fetcher`），这是布尔变量做不到的。

---

### 4.2 `_fetchers.py`：pooch 降级与 data_fetcher=None

#### 4.2.1 概念说明

`_fetchers.py` 是数据集获取层（见 `u1-l2`）。它对 `pooch` 的降级比另两处多了一层考虑：pooch 不仅仅是个「被调用的工具」，它还要在导入时**立刻**创建一个配置好的单例 `data_fetcher`（缓存路径、base_url、registry、urls 都要在这里绑好，详见 `u2-l1`）。

这就带来一个问题：如果 pooch 不存在，`data_fetcher = pooch.create(...)` 这一步根本没法执行。于是 `_fetchers.py` 的降级必须**同时**处理两个名字：把 `pooch` 和 `data_fetcher` 一起置 `None`。这是三处降级里最「重」的一处。

#### 4.2.2 核心流程

```
import pooch
  ├─ 成功 → else 分支 → data_fetcher = pooch.create(... 配置齐全 ...)
  └─ 失败(ImportError) → pooch = None; data_fetcher = None
                                   │
                                   ▼
            fetch_data(dataset_name) 被调用
                                   │
                ┌──────────────────┴──────────────────┐
                ▼                                     ▼
        data_fetcher is None?              否 → 正常下载，返回本地路径
        是 → raise ImportError(友好提示)
```

注意 `fetch_data` 的签名里把 `data_fetcher` 设成了**默认参数**：`def fetch_data(dataset_name, data_fetcher=data_fetcher)`。这个写法在 `u2-l1` 里提过——它为测试留了「注入一个假的 fetcher」的口子。从降级角度看，它同时也意味着：`data_fetcher` 在函数定义时就绑定好了「当前 pooch 是否可用」的结论（`None` 或真实单例）。

#### 4.2.3 源码精读

导入段 [_fetchers.py:L8-L26](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_fetchers.py#L8-L26)，关键是 `try/except/else` 三段式——`else` 分支只有在 `import pooch` 成功时才执行，正好用来创建那个依赖 pooch 的单例：

```python
try:
    import pooch
except ImportError:
    pooch = None
    data_fetcher = None
else:
    data_fetcher = pooch.create(           # 成功才创建
        path=pooch.os_cache("scipy-data"),
        base_url="https://github.com/scipy/",
        registry=registry,
        urls=registry_urls
    )
```

使用段 [_fetchers.py:L29-L33](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_fetchers.py#L29-L33)，在 `fetch_data` 函数体最开头做哨兵检查，这是三个数据集函数（`ascent/face/electrocardiogram`）共同必经的关卡：

```python
def fetch_data(dataset_name, data_fetcher=data_fetcher):
    if data_fetcher is None:
        raise ImportError("Missing optional dependency 'pooch' required "
                          "for scipy.datasets module. Please use pip or "
                          "conda to install 'pooch'.")
```

这条 `ImportError` 文案值得记住，因为它是用户最常看到的一条——后面你会发现三处报错用的几乎都是它。

#### 4.2.4 代码实践

**实践目标**：体会「`ascent/face/electrocardiogram` 都必经 `fetch_data`，因此 pooch 缺失时它们都会在同一条 `ImportError` 上失败」。

**操作步骤**：

1. 准备一个没装 pooch 的虚拟环境（命令本身以你本地的 venv 工具为准，**待本地验证**）：

   ```bash
   python -m venv /tmp/nopooch
   /tmp/nopooch/bin/python -m pip install numpy
   ```

2. 把当前 `scipy/datasets` 目录放进该环境的 `site-packages`（或用 `PYTHONPATH` 指过去），然后执行：

   ```bash
   /tmp/nopooch/bin/python -c "
   from scipy.datasets import ascent, face, electrocardiogram
   print('三个函数都导入成功了，没有报错')
   for fn in (ascent, face, electrocardiogram):
       try:
           fn()
       except ImportError as e:
           print(fn.__name__, '->', type(e).__name__, ':', e)
   "
   ```

**需要观察的现象**：第一行打印说明导入段是静默的；随后三个函数**各自**抛出同一条 `ImportError: Missing optional dependency 'pooch' ...`。

**预期结果**：三条报错文案完全一致——因为它们都汇聚到 [_fetchers.py:L30-L33](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_fetchers.py#L30-L33) 的同一个检查点。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `_fetchers.py` 用 `try/except/else`，而 `_utils.py` 只用 `try/except`（没有 `else`）？

**参考答案**：`_fetchers.py` 在导入成功后需要**立刻**执行一段依赖 pooch 的代码（`pooch.create(...)` 创建单例），这段代码必须放进 `else` 分支，否则当 pooch 为 `None` 时它会崩溃。`_utils.py` 导入 `platformdirs` 后并不需要在导入时立刻创建任何对象——它只在 `_clear_cache` 被调用时才用 `platformdirs.user_cache_dir(...)`，所以没有 `else` 分支，逻辑照样成立。

**练习 2**：把 `def fetch_data(dataset_name, data_fetcher=data_fetcher)` 改成函数体里 `data_fetcher = data_fetcher`（全局查找）会有什么不同？

**参考答案**：写成默认参数，意味着 `data_fetcher` 的值在**函数定义时**就被绑定（此时它可能是 `None`，也可能是真实单例）；测试时可以通过显式传参覆盖它。如果改成运行时去模块全局找 `data_fetcher`，行为在「普通调用」下等价，但会丢失「调用方显式注入一个 fetcher」的能力，不利于单元测试隔离（`u2-l1` 已强调过这个注入口子的价值）。

---

### 4.3 `_utils.py`：platformdirs 降级与「提示 pooch」的细节

#### 4.3.1 概念说明

`_utils.py` 提供缓存清理（见 `u2-l4`）。它的降级对象不是 `pooch`，而是 `platformdirs`——一个用来「跨平台定位缓存目录」的小库。这里有两个值得注意的细节：

1. **清理侧故意不 import pooch**：`_clear_cache` 只需要知道「缓存目录在哪」，不需要联网，所以它用 `platformdirs` 而不是 `pooch` 来重建 pooch 当初用过的同一条缓存路径（详见 `u2-l4`）。
2. **缺失时报的却是 pooch**：尽管 try 的是 `platformdirs`，报错文案却提示用户安装 `pooch`。这是因为 `platformdirs` 是 `pooch` 的传递依赖——装了 pooch 几乎必然有 platformdirs，所以「缺 platformdirs」在实际中就等价于「缺 pooch」。

#### 4.3.2 核心流程

```
import platformdirs
  ├─ 成功 → platformdirs 是真实模块
  └─ 失败 → platformdirs = None
                │
                ▼
   _clear_cache(datasets, cache_dir=None, ...)
                │
        cache_dir is None?   ← 调用方没显式给路径
                │ 是
                ▼
        platformdirs is None?
        ├─ 是 → raise ImportError("…'pooch'…")   ← 注意提示的是 pooch
        └─ 否 → cache_dir = platformdirs.user_cache_dir("scipy-data")
```

关键点：**只有当 `cache_dir` 没被显式传入时**，才会走到「需要 platformdirs」的分支。这也是 `_clear_cache` 比 `clear_cache` 多出来的一个可注入参数——测试时可以传一个临时目录，完全绕开 platformdirs（见 `u3-l3` 测试剖析）。

#### 4.3.3 源码精读

导入段 [_utils.py:L7-L10](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_utils.py#L7-L10)，极简的 try/except，没有 else：

```python
try:
    import platformdirs
except ImportError:
    platformdirs = None
```

使用段 [_utils.py:L13-L24](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_utils.py#L13-L24)。注意第 21 行的注释 `# platformdirs is pooch dependency` 一语点破了「为什么提示 pooch」：

```python
def _clear_cache(datasets, cache_dir=None, method_map=None):
    ...
    if cache_dir is None:
        # Use default cache_dir path
        if platformdirs is None:
            # platformdirs is pooch dependency
            raise ImportError("Missing optional dependency 'pooch' required "
                              "for scipy.datasets module. Please use pip or "
                              "conda to install 'pooch'.")
        cache_dir = platformdirs.user_cache_dir("scipy-data")
```

这条报错文案和 `_fetchers.py` 里的**逐字相同**——这是有意为之的统一性：无论用户从哪个入口撞上「缺依赖」，看到的都是同一条可操作的安装提示。

#### 4.3.4 代码实践

**实践目标**：验证「清理缓存也会因为缺依赖而报错」，并对照报错文案与 `_fetchers.py` 是否一致。

**操作步骤**（延续 4.2.4 的无 pooch 环境，**待本地验证**）：

```bash
/tmp/nopooch/bin/python -c "
from scipy.datasets import clear_cache
try:
    clear_cache()          # 不传 datasets，触发清全部
except ImportError as e:
    print('clear_cache ->', type(e).__name__, ':', e)
"
```

**需要观察的现象**：`clear_cache()` 内部转发到 `_clear_cache(None)`，由于没传 `cache_dir` 且 `platformdirs is None`，于是抛出 ImportError。

**预期结果**：报错文案与 4.2.4 里 `face()` 等看到的**完全一致**（都是 `Missing optional dependency 'pooch' ...`）。这一致性正是上节说的「统一安装提示」。

#### 4.3.5 小练习与答案

**练习 1**：既然 `_clear_cache` 里 try 的是 `platformdirs`，为什么不在报错里写「请安装 platformdirs」？

**参考答案**：因为 `platformdirs` 是 `pooch` 的传递依赖。用户真正需要的是能正常使用 `scipy.datasets` 的全部功能，而那需要 `pooch`（pooch 会自动带上 platformdirs）。提示「装 platformdirs」反而会让用户装了一个「只能解决清理、不能解决下载」的库；提示「装 pooch」一次性解决所有问题，更符合用户意图。

**练习 2**：如何在不安装 platformdirs 的前提下，让 `_clear_cache` 不报错地跑起来？

**参考答案**：显式传入 `cache_dir` 参数，绕开「需要 platformdirs 解析路径」的分支。例如 `_clear_cache([some_method], cache_dir="/tmp/fake_cache", method_map={...})`。这正是 `u3-l3` 测试里用 `tmp_path` + 自定义 `method_map` 离线测试清理逻辑的原理。

---

### 4.4 `_download_all.py`：pooch 降级

#### 4.4.1 概念说明

`_download_all.py`（见 `u3-l2`）提供批量下载入口 `download_all`，以及一个命令行 `main()`。它的降级对象又是 `pooch`，模式与 `_fetchers.py` 几乎一样——但它有一个**别处没有的隐患**值得专门讲：`main()` 里 argparse 的默认值在 pooch 缺失时可能以「不友好」的方式崩溃。本节既是讲模式，也是教你「读源码时要留意默认值求值时机」。

#### 4.4.2 核心流程

```
import pooch
  ├─ 成功 → pooch 可用
  └─ 失败 → pooch = None
                │
   ┌────────────┴─────────────┐
   ▼                          ▼
download_all(path)         main()  (脚本入口)
   │                          │
 pooch is None?              add_argument(default=pooch.os_cache(...))
 ├─ 是 → ImportError          │  ← 若 pooch 为 None，此处 None.os_cache(...)
 └─ 否 → 循环下载              │     会抛 AttributeError，先于友好报错！
```

也就是说：从「`download_all()` 作为函数被调用」的路径看，降级是完整的；但从「`python _download_all.py` 当脚本跑」的路径看，pooch 缺失时会在 argparse 默认值处先栽倒。

#### 4.4.3 源码精读

导入段 [_download_all.py:L12-L15](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_download_all.py#L12-L15)：

```python
try:
    import pooch
except ImportError:
    pooch = None
```

使用段 [_download_all.py:L50-L53](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_download_all.py#L50-L53)，与 `_fetchers.py` 的检查同构：

```python
if pooch is None:
    raise ImportError("Missing optional dependency 'pooch' required "
                      "for scipy.datasets module. Please use pip or "
                      "conda to install 'pooch'.")
```

现在看那个隐患。[_download_all.py:L64-L70](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_download_all.py#L64-L70) 的 `main()`：

```python
def main():
    parser = argparse.ArgumentParser(description='Download SciPy data files.')
    parser.add_argument("path", nargs='?', type=str,
                        default=pooch.os_cache('scipy-data'),   # ← 求值时机在此
                        help="...")
    args = parser.parse_args()
    download_all(args.path)
```

`default=pooch.os_cache('scipy-data')` 是在 `main()` 被调用、执行到这一行时才求值的。若 `pooch is None`，它等价于 `None.os_cache('scipy-data')`，会抛 `AttributeError`，**早于** `download_all` 内部那条友好的 `ImportError`。所以「脚本路径 + 缺 pooch」这个组合，实际报的是 `AttributeError`，而不是统一的安装提示。这是一个真实的、读源码才能发现的细节（行为以本地验证为准）。

#### 4.4.4 代码实践

**实践目标**：对比「函数路径」与「脚本路径」在 pooch 缺失时的不同表现，加深对「默认值求值时机」的理解。

**操作步骤**（沿用无 pooch 环境，**待本地验证**）：

1. 函数路径：

   ```bash
   /tmp/nopooch/bin/python -c "
   from scipy.datasets import download_all
   try:
       download_all('.')
   except ImportError as e:
       print('download_all(函数) ->', type(e).__name__, ':', e)
   "
   ```

2. 脚本路径（把 `_download_all.py` 当脚本跑）：

   ```bash
   /tmp/nopooch/bin/python /path/to/scipy/datasets/_download_all.py
   ```

**需要观察的现象**：

- 第 1 步抛出友好的 `ImportError: Missing optional dependency 'pooch' ...`。
- 第 2 步抛出的却是 `AttributeError`（大致形如 `'NoneType' object has no attribute 'os_cache'`），且发生在 `main()` 里。

**预期结果**：两条路径报错类型不同——函数路径走的是 4.4.3 的友好检查；脚本路径撞上 argparse 默认值求值。如果第 2 步在你的环境里表现不同（例如 Python 版本差异），以本地实际 traceback 为准。

#### 4.4.5 小练习与答案

**练习 1**：如果要让「脚本路径」在缺 pooch 时也报友好的 `ImportError`，最小改动是什么？

**参考答案**：在 `main()` 开头加一个与 `download_all` 相同的哨兵检查，例如：

```python
def main():
    if pooch is None:
        raise ImportError("Missing optional dependency 'pooch' ...")
    parser = argparse.ArgumentParser(...)
    ...
```

这样在执行到 `add_argument(default=pooch.os_cache(...))` 之前就提前抛出友好错误。注意这只是教学性的「示例改动」，本讲不修改项目源码。

**练习 2**：为什么 `download_all` 里的检查写在函数体开头，而不是写在 `if path is None:` 分支里（毕竟那里才第一次用 pooch）？

**参考答案**：因为 `pooch` 在 `download_all` 里除了 `path is None` 时用 `pooch.os_cache`，还会在循环里用 `pooch.retrieve` 和 `pooch.HTTPDownloader`——无论 `path` 是否为 `None`，后面都必然用到 pooch。把检查前置到函数体开头，可以「一次检查、覆盖全函数」，避免在多个分支里重复写哨兵判断，也让报错时机更早、更可预测。

---

### 4.5 三处实现的横向对比与设计权衡

#### 4.5.1 概念说明

把三处降级放在一起看，能提炼出 `scipy.datasets` 在「可选依赖」这件事上的统一设计哲学，也能看到一处**不一致**（脚本路径的 argparse 默认值）。这种「横向对比」是源码阅读的高阶训练：不只是读懂每一处，而是看出它们之间的同与不同，进而判断哪些是「模式」、哪些是「可改进的细节」。

#### 4.5.2 核心流程

对比表：

| 维度 | `_fetchers.py` | `_utils.py` | `_download_all.py` |
| --- | --- | --- | --- |
| 降级对象 | `pooch` | `platformdirs` | `pooch` |
| 导入段是否带 `else` | 是（创建 `data_fetcher` 单例） | 否 | 否 |
| 哨兵变量 | `pooch` + `data_fetcher`（两个） | `platformdirs`（一个） | `pooch`（一个） |
| 使用段检查位置 | `fetch_data` 函数体开头 | `_clear_cache` 内、`cache_dir is None` 分支里 | `download_all` 函数体开头 |
| 报错文案 | `Missing optional dependency 'pooch'...` | 同左（尽管 try 的是 platformdirs） | 同左 |
| 是否有「漏网」路径 | 无 | 无（显式传 `cache_dir` 可绕过，属设计） | 有：`main()` 的 argparse 默认值 |

三处共同遵守的「合约」：

1. **导入静默**：`import scipy.datasets` 永远不因缺可选依赖而失败；
2. **统一文案**：报错都用同一条「请用 pip 或 conda 安装 pooch」；
3. **只抓 ImportError**：不误吞其他异常；
4. **哨兵用 None**：用 `is None` 判断。

#### 4.5.3 源码精读

文案的统一性，对比 [_fetchers.py:L31-L33](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_fetchers.py#L31-L33)、[_utils.py:L21-L23](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_utils.py#L21-L23)、[_download_all.py:L51-L53](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_download_all.py#L51-L53) 即可印证——三段字符串逐字相同。这是「让用户无论从哪个入口撞墙，都看到同一条出路」的有意设计。

而「漏网」则集中在 [_download_all.py:L67](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_download_all.py#L67)——这一行 `default=pooch.os_cache('scipy-data')` 在 `pooch is None` 时会抛 `AttributeError`，破坏了「统一友好报错」的合约。它之所以「漏网」，是因为降级检查只加在了「最常用的函数入口」`download_all` 里，而 `main()` 这条脚本入口被忽略了。

#### 4.5.4 代码实践

**实践目标**：把三处降级「一次性跑通」，亲手验证对比表里的结论。

**操作步骤**（无 pooch 环境，**待本地验证**）：把以下脚本存为 `probe.py` 并运行：

```python
# 示例代码
from scipy.datasets import face, clear_cache
try:
    from scipy.datasets import download_all
except Exception as e:
    download_all = None
    print("import download_all 失败:", e)

def probe(label, fn):
    try:
        fn()
    except ImportError as e:
        print(f"{label}: ImportError -> {e}")
    except Exception as e:
        print(f"{label}: {type(e).__name__} -> {e}")

probe("face",         lambda: face())
probe("clear_cache",  lambda: clear_cache())
if download_all:
    probe("download_all", lambda: download_all("."))
```

**需要观察的现象**：前三个都应抛 `ImportError` 且文案一致；从而坐实「三处友好报错文案相同」。

**预期结果**：三行输出里的 `ImportError` 文案完全一致。

#### 4.5.5 小练习与答案

**练习 1**：如果把这三处降级全部删掉（即直接 `import pooch` / `import platformdirs`），对普通用户最直接的影响是什么？

**参考答案**：任何 `import scipy.datasets`（乃至依赖该子模块的 `import scipy` 路径）都会在缺 pooch 的环境里立刻崩溃，连「我根本不打算下载数据」的用户也被牵连。这正是「用到才报错」策略要避免的情形，也是本讲反复强调的设计动机。

**练习 2**：三处报错文案完全相同是巧合还是设计？如何判断？

**参考答案**：是设计。判断依据有二：(1) 文案逐字相同，包括「Please use pip or conda to install 'pooch'.」这样的细节；(2) `_utils.py` 明明 try 的是 `platformdirs`，文案却仍写 'pooch'，且第 21 行注释 `# platformdirs is pooch dependency` 解释了这一选择。这说明作者是刻意让「无论从哪条路径缺依赖，用户都看到同一条最可操作的安装提示」。

## 5. 综合实践

把本讲的知识串起来：**给一段自己的代码加上「完整的」可选依赖降级保护**，并确保它经得起「导入静默 + 用到报错」两条检验。

任务背景：假设你写了一个小工具 `mytool.py`，它有一个核心功能 `plot_thing()` 依赖可选库 `matplotlib`，另一个功能 `serve_thing()` 依赖可选库 `flask`。要求：

1. 不装 `matplotlib` 也能 `import mytool` 成功；
2. 不装 `flask` 也能 `import mytool` 成功；
3. 调用 `plot_thing()` 时，缺 `matplotlib` 才报一条友好的 `ImportError`；
4. 调用 `serve_thing()` 时，缺 `flask` 才报一条友好的 `ImportError`；
5. 文案风格仿照 `scipy.datasets`（`Missing optional dependency '<name>' ... Please use pip or conda to install '<name>'.`）。

**参考实现**（示例代码）：

```python
# 示例代码：mytool.py
try:
    import matplotlib
except ImportError:
    matplotlib = None

try:
    import flask
except ImportError:
    flask = None


def plot_thing():
    if matplotlib is None:
        raise ImportError(
            "Missing optional dependency 'matplotlib' required for "
            "mytool.plot_thing. Please use pip or conda to install "
            "'matplotlib'."
        )
    import matplotlib.pyplot as plt
    plt.plot([1, 2, 3])
    plt.show()


def serve_thing():
    if flask is None:
        raise ImportError(
            "Missing optional dependency 'flask' required for "
            "mytool.serve_thing. Please use pip or conda to install 'flask'."
        )
    app = flask.Flask(__name__)

    @app.route("/")
    def index():
        return "hi"
    return app
```

**验证步骤**（**待本地验证**）：

1. 在一个既没装 matplotlib 也没装 flask 的环境里：

   ```bash
   python -c "import mytool; print('导入成功')"      # 应成功
   python -c "import mytool; mytool.plot_thing()"    # 应抛 ImportError 提示 matplotlib
   python -c "import mytool; mytool.serve_thing()"   # 应抛 ImportError 提示 flask
   ```

2. 只装 matplotlib：

   ```bash
   python -m pip install matplotlib
   python -c "import mytool; mytool.plot_thing()"    # 不再报 ImportError（可能弹图）
   python -c "import mytool; mytool.serve_thing()"   # 仍抛 ImportError 提示 flask
   ```

**自检清单**（对照本讲要点）：

- [ ] 导入段是否用了 `except ImportError:`（而不是裸 `except:`）？
- [ ] 哨兵值是否用 `None`，使用段是否用 `is None` 判断？
- [ ] 报错文案是否清晰指出了缺哪个库、怎么装？
- [ ] 是否做到了「导入静默、用到才报错」？

完成后再回头读一遍 [_fetchers.py:L8-L33](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/datasets/_fetchers.py#L8-L33)，你会发现自己的实现和 SciPy 的做法几乎如出一辙——这就说明你已经掌握了这套惯用法。

## 6. 本讲小结

- **可选依赖**是「用到才需要」的库；`scipy.datasets` 把 `pooch`、`platformdirs` 都当作可选依赖处理。
- 核心惯用法是 **try-import-or-None**：`try: import DEP / except ImportError: DEP = None`，导入时静默，把失败推迟到使用时。
- 选择「用到才报错」而非「导入即失败」，是为了让不下载数据的用户完全无感，同时给需要下载的用户一条清晰、统一的安装提示。
- 三处降级高度同构：`_fetchers.py` 多一个 `else` 分支来创建 `data_fetcher` 单例；`_utils.py` try 的是 `platformdirs` 但报错提示 `pooch`（因为前者是后者的传递依赖）；`_download_all.py` 的函数路径降级完整，但 `main()` 的 argparse 默认值在 pooch 缺失时会抛 `AttributeError`，是一处值得注意的「漏网」细节。
- 三处友好报错的文案**逐字相同**，是有意为之的统一设计。
- 哨兵值统一用 `None`、判断统一用 `is None`、异常统一只抓 `ImportError`——这三条是可复用到你自己代码里的「合约」。

## 7. 下一步学习建议

- 接下来阅读 `u3-l2`（download_all 与命令行脚本），它会展开本讲提到的 `__spec__.parent` 双导入技巧与 `download_all` 的批量下载循环，并再次碰到 pooch 降级——届时你可以带着本讲的「漏网细节」去审视脚本入口。
- 若想看「降级如何被测试覆盖」，跳到 `u3-l3`（测试设计剖析），重点看 `_clear_cache` 如何通过 `cache_dir=` / `method_map=` 两个可注入参数，在不安装 platformdirs 的情况下被离线测试。
- 延伸阅读：对比 SciPy 其他子模块（如 `scipy._lib`）对可选依赖的处理，看看它们是否也采用同一套 try-import-or-None 模式；这是一种在整个 SciPy 生态里反复出现的设计语言。
