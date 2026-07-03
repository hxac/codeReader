# 综合实践：把旧代码从 scipy.misc 迁移出去

## 1. 本讲目标

本讲是整个 `scipy.misc` 学习手册的**收官实战**。前面七讲我们搞清楚了一件事：`scipy.misc` 是一个正在退役的「墓碑桩模块」，真实功能早就被搬空了。现在我们要把这件事变成**动手能力**——拿到一段仍在用 `scipy.misc` 的旧脚本，亲手把它迁移到现代等价物上。

读完本讲，你应当能够：

1. 对旧脚本里的每一个 `scipy.misc.xxx` 名字，**判断它该去哪里**（迁往 `scipy.datasets`、彻底删除、还是改用第三方库/手写实现）。
2. 把数据集调用 `scipy.misc.face / ascent / electrocardiogram` **零成本迁到** `scipy.datasets`。
3. 明白为什么 `scipy.misc.derivative / central_diff_weights` **没有任何 scipy 内置替代**，并能**手写一个中心差分**完成等价迁移。
4. 用 `python -W error::DeprecationWarning` 在迁移完成后做**兜底校验**，确保残留的弃用警告在 CI 中无所遁形。

> 本讲依赖前置讲义 **u3-l1（数据集迁移路径）** 和 **u1-l3（历史职能与退役原因）**。如果你还没读过，建议先扫一眼它们的结论。

---

## 2. 前置知识

本讲默认你已经理解以下概念（前七讲已建立）：

- **弃用桩（stub）**：`scipy/misc/` 下三个 `.py` 文件，结构相同，仅调用 `warnings.warn(...)` 发出 `DeprecationWarning`，不含任何函数。
- **`stacklevel=2`**：让警告归因到「触发导入的那一行用户代码」，而非桩文件自身。
- **PEP 562 模块级 `__getattr__`**：旧版 `scipy.misc` 曾用它拦截 `scipy.misc.face` 等访问并给出弃用提示；**当前版本已删除**，因此桩模块不会「兜住」任何名字。
- **按需下载（on-demand fetch）+ 本地缓存 + SHA256 校验**：`scipy.datasets` 用 `pooch` 取代旧的「把 `.dat` 打包进安装包」方式。
- **`central_diff_weights` / `derivative`**：历史上提供有限差分权重的两个数值工具。

如果你对其中某项不熟，本讲会用一两句话带过，重点放在**迁移动作**上。

本讲还会用到两个工具，先做个一句话科普：

- **`-W` 命令行选项**：Python 解释器的警告过滤器开关，例如 `python -W error` 会把所有警告升级成异常，`-W error::DeprecationWarning` 只升级弃用警告。
- **有限差分（finite difference）**：用若干离散点上的函数值近似导数的数值方法。中心差分（central difference）是用「左右对称」的点构造公式，精度通常高于单向差分。

---

## 3. 本讲源码地图

本讲涉及的关键文件（含一个**历史版本**，需用 `git show` 取回）：

| 文件 | 角色 | 本讲如何使用 |
| --- | --- | --- |
| `scipy/misc/__init__.py` | 当前的弃用桩 | 证明「旧名字在当前 HEAD 不存在」，迁移不可拖延 |
| `scipy/datasets/__init__.py` | 数据集模块的公开入口 | 看 `face/ascent/electrocardiogram` 的导出与缓存路径文档 |
| `scipy/datasets/_fetchers.py` | 数据集下载引擎 | 精读 `face()` / `fetch_data()` / `pooch.create()` |
| `scipy/misc/_common.py`（**已删除**） | 历史上的数值工具真身 | 用 `git show 43fc97efa8^:scipy/misc/_common.py` 还原 `derivative` 的签名与弃用提示 |

> 注意：`scipy/misc/_common.py` 在当前 HEAD 已经不存在（被 PR #21864 删除）。本讲引用它只为对照「旧接口长什么样、SciPy 当时建议怎么迁移」，引用方式是 `git show <commit>^:<path>`，而不是永久链接。

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：先建一棵**迁移决策树**，再分别迁移**数据集**与**数值工具**，最后用**警告兜底**收尾。

### 4.1 迁移决策树：先分类，再动手

#### 4.1.1 概念说明

迁移最大的陷阱是「一刀切」：以为所有 `scipy.misc.xxx` 都换成一个新地址就完事了。实际上，`scipy.misc` 历史上是个**杂物箱**，里面的东西**去向各不相同**：

- 数据集类（`face / ascent / electrocardiogram`）→ 有明确新家 `scipy.datasets`，调用方式几乎零改动，只换 `import` 来源。
- 数值工具类（`derivative / central_diff_weights`）→ **没有 scipy 新家**，被彻底删除，需改用第三方库或手写有限差分。
- 通用工具子模块 `common`、文档辅助子模块 `doccer` → 内容早被搬走（`factorial/comb` 去 `scipy.special`、`doccer` 去 `scipy/_lib`），旧引用应**直接删除**，不要试图在新地方找同名函数。

所以第一步永远是**列清单 + 分类**，而不是直接动键盘。

#### 4.1.2 核心流程

迁移决策可以用下面这段伪代码概括（`name` 是旧脚本里 `scipy.misc.<name>` 的 `<name>`）：

```text
for name in 旧脚本用到的 scipy.misc 名字:
    if name in {"face", "ascent", "electrocardiogram"}:
        # 数据集：换 import 来源，调用点不变
        目标 = "scipy.datasets." + name
        动作 = "替换 import，保留调用"
    elif name in {"derivative", "central_diff_weights"}:
        # 数值工具：无 scipy 替代
        目标 = "numdifftools / findiff / 手写有限差分"
        动作 = "重写该调用"
    elif name == "common":
        目标 = "（factorial/comb 已在 scipy.special）"
        动作 = "删除引用，按需改用 scipy.special"
    elif name == "doccer":
        目标 = "scipy._lib.doccer（私有，不建议外部依赖）"
        动作 = "删除外部引用"
    else:
        动作 = "未知名字，多半是更早版本残留，逐个确认"
```

> 经验法则：**能换 import 的优先换 import**（数据集），**需要重写逻辑的单独评估**（数值工具），**早已搬空的直接删**（common/doccer）。

#### 4.1.3 源码精读

决策树的依据，藏在两个文件的对比里。

**当前 `scipy/misc/__init__.py` 全文只有 6 行**，且不定义任何数据集或工具名字：

[scipy/misc/__init__.py:1-6](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/misc/__init__.py#L1-L6) — 整个模块在导入时只做一件事：发出一条 `DeprecationWarning`，然后什么也不定义。

```python
import warnings
warnings.warn(
    "scipy.misc is deprecated and will be removed in 2.0.0",
    DeprecationWarning,
    stacklevel=2
)
```

这意味着：在当前 HEAD 下，`scipy.misc.face`、`scipy.misc.derivative` 这些名字**根本不存在于模块属性表里**。又因为当前版本删除了旧版用来兜底的 PEP 562 模块级 `__getattr__`（详见 u3-l2），解释器不会「兜住」这些访问并给出友好提示——访问它们会**直接抛 `AttributeError`**。换句话说，旧脚本不是「能用但会警告」，而是「**直接崩**」。这就是迁移不可拖延的硬证据。

**对照地，`scipy/datasets/__init__.py` 才是数据集的新家**：

[scipy/datasets/__init__.py:80-85](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/datasets/__init__.py#L80-L85) — 显式从 `_fetchers` 导入三个数据集函数，并把它们写进 `__all__`。

```python
from ._fetchers import face, ascent, electrocardiogram
from ._download_all import download_all
from ._utils import clear_cache

__all__ = ['ascent', 'electrocardiogram', 'face',
           'download_all', 'clear_cache']
```

这五条 `__all__` 就是「公开承诺」：数据集在新家只暴露 `ascent / electrocardiogram / face`，外加两个缓存管理工具 `download_all / clear_cache`。注意这里**没有** `derivative / central_diff_weights`——印证了「数值工具在 scipy 里没有新家」。

#### 4.1.4 代码实践

**实践目标**：亲眼确认旧名字在当前 HEAD 已不可用，从而理解迁移的紧迫性。

**操作步骤**：

1. 写一个三行探针脚本 `probe.py`：

   ```python
   import scipy.misc          # 触发弃用警告
   print(hasattr(scipy.misc, "face"))        # 数据集代表
   print(hasattr(scipy.misc, "derivative"))  # 数值工具代表
   ```

2. 运行 `python probe.py`，观察输出。

**需要观察的现象**：

- 终端打印一条 `DeprecationWarning: scipy.misc is deprecated and will be removed in 2.0.0`。
- 两行 `hasattr` 的结果**都是 `False`**。

**预期结果**：两个名字都不可用，证明无论数据集还是数值工具，旧入口都已失效。迁移不是「锦上添花」，而是「修复崩溃」。

> 待本地验证：不同 SciPy 构建对默认警告过滤器的展示略有差异（默认 `DeprecationWarning` 只在 `__main__` 显示）。若没看到警告文字，可用 `python -W always probe.py` 强制显示，或用 `warnings.catch_warnings(record=True)` 捕获（见 u1-l2）。

#### 4.1.5 小练习与答案

**练习 1**：为什么在当前 HEAD 下，`scipy.misc.face` 抛的是 `AttributeError` 而不是 `DeprecationWarning`？

**参考答案**：因为当前 `scipy/misc/__init__.py` 只在**导入时**发一次模块级警告，并没有定义 `face` 这个属性；旧版用来「在访问未知名字时兜底发弃用提示」的 PEP 562 模块级 `__getattr__` 已被删除。所以访问 `scipy.misc.face` 走的是 Python 默认的属性查找失败路径，直接 `AttributeError`。

**练习 2**：旧脚本里若同时出现 `scipy.misc.face()` 与 `scipy.misc.derivative(...)`，哪一条能「换个 import」就迁完？哪一条必须重写？

**参考答案**：`face` 换成 `scipy.datasets.face` 即可，调用点不变；`derivative` 没有 scipy 替代，必须重写（手写有限差分或改用 numdifftools/findiff）。

---

### 4.2 数据集迁移：scipy.misc.face / ascent / electrocardiogram → scipy.datasets

#### 4.2.1 概念说明

三个数据集函数是迁移里**最省事**的一类。它们的新家 `scipy.datasets` 提供了**同名的、同签名的**函数：`face(gray=False)`、`ascent()`、`electrocardiogram()`。所以迁移动作本质上只有一步：

> 把 `from scipy.misc import face` 改成 `from scipy.datasets import face`（或 `import scipy.misc` → `import scipy.datasets`），调用点的 `face(...)` 一字不改。

背后的机制变化（u3-l1 已详述）：旧方式把 `face.dat`/`ascent.dat`/`ecg.dat` 这些二进制文件**打进 scipy 安装包**，随包一起发布；新方式改为「**首次调用时用 pooch 按需下载 + 本地缓存 + SHA256 校验**」，把计算库和示例数据解耦。对调用者而言，这一变化**完全透明**——除了第一次调用会联网。

#### 4.2.2 核心流程

数据集调用的运行时流程：

```text
scipy.datasets.face(gray=False)
  └─> fetch_data("face.dat")
        ├─ 若 pooch 缺失  ─> raise ImportError("Missing optional dependency 'pooch' ...")
        ├─ 若本地缓存命中 ─> 直接返回缓存路径（不联网）
        └─ 否则            ─> 从 dataset-face 仓库下载，校验 SHA256，写入 os_cache("scipy-data")
  └─> 用 bz2 解压、reshape 成 (768, 1024, 3) 的 uint8 数组返回
```

注意两个「坑」：

- **pooch 是可选依赖**：若环境没装 `pooch`，调用会抛 `ImportError` 而不是静默失败。这正是「惰性报错」的设计——只有真用到数据集才要求装 `pooch`。
- **首次调用需要联网**：在无网络环境下，可手动把数据集仓库内容放进缓存目录（见下文 4.2.3 的文档路径）。

#### 4.2.3 源码精读

[scipy/datasets/_fetchers.py:183-225](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/datasets/_fetchers.py#L183-L225) — `face()` 的实现：先 `fetch_data("face.dat")` 拿到本地文件路径，再 `bz2` 解压、`reshape` 成 (768, 1024, 3)。

关键片段（节选）：

```python
def face(gray=False):
    import bz2
    fname = fetch_data("face.dat")
    with open(fname, 'rb') as f:
        rawdata = f.read()
    face_data = bz2.decompress(rawdata)
    face = frombuffer(face_data, dtype='uint8').reshape((768, 1024, 3))
    if gray is True:
        face = (0.21 * face[:, :, 0] + 0.71 * face[:, :, 1] +
                0.07 * face[:, :, 2]).astype('uint8')
    return face
```

下载逻辑在 `fetch_data` 里：

[scipy/datasets/_fetchers.py:29-39](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/datasets/_fetchers.py#L29-L39) — 若 `data_fetcher is None`（即没装 pooch）则抛 `ImportError`；否则用带 `User-Agent` 的 `HTTPDownloader` 拉取，返回下载后的**本地完整路径**。

```python
def fetch_data(dataset_name, data_fetcher=data_fetcher):
    if data_fetcher is None:
        raise ImportError("Missing optional dependency 'pooch' required "
                          "for scipy.datasets module. ...")
    downloader = pooch.HTTPDownloader(
        headers={"User-Agent": f"SciPy {sys.modules['scipy'].__version__}"}
    )
    return data_fetcher.fetch(dataset_name, downloader=downloader)
```

而 `data_fetcher` 在模块导入时由 `pooch.create` 一次性创建：

[scipy/datasets/_fetchers.py:14-26](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/datasets/_fetchers.py#L14-L26) — 指定缓存目录为 `os_cache("scipy-data")`，注册 `registry`（SHA256 校验表）与 `registry_urls`（每个文件对应的 `dataset-<name>` 仓库地址）。

```python
data_fetcher = pooch.create(
    path=pooch.os_cache("scipy-data"),
    base_url="https://github.com/scipy/",
    registry=registry,
    urls=registry_urls
)
```

缓存目录的平台差异，文档里写得很清楚（这对你「定位缓存」很有用）：

[scipy/datasets/__init__.py:56-68](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/datasets/__init__.py#L56-L68) — macOS 为 `~/Library/Caches/scipy-data`；Linux/Unix 为 `~/.cache/scipy-data`（或 `XDG_CACHE_HOME` 环境变量）；Windows 为 `C:\Users\<user>\AppData\Local\<AppAuthor>\scipy-data\Cache`。

#### 4.2.4 代码实践

**实践目标**：把一个使用旧 `scipy.misc.face()` 的脚本迁移到 `scipy.datasets.face()`，并验证返回值与缓存。

**操作步骤**：

1. 旧脚本（**示例代码**，模拟待迁移代码）：

   ```python
   # legacy_face.py —— 待迁移
   import scipy.misc
   img = scipy.misc.face()
   print(img.shape, img.dtype)
   ```

   在当前 HEAD 上运行它会先发 `DeprecationWarning`，随后在 `scipy.misc.face` 处抛 `AttributeError`。

2. 迁移后的脚本：

   ```python
   # migrated_face.py
   import scipy.datasets
   img = scipy.datasets.face()
   print(img.shape, img.dtype)
   ```

3. 运行 `python migrated_face.py`（首次会联网下载 `face.dat`）。

**需要观察的现象**：

- 首次运行时终端有下载进度（pooch 输出）。
- 之后再次运行几乎瞬间返回（命中缓存）。

**预期结果**（取自 `face()` 的 docstring，权威）：

- `img.shape == (768, 1024, 3)`，`img.dtype == uint8`。
- 在你平台对应的缓存目录下出现 `face.dat`。

> 待本地验证：若环境未安装 `pooch`，会得到 `ImportError: Missing optional dependency 'pooch' ...`。这是**预期行为**，用 `pip install pooch` 解决，而非迁移错误。

#### 4.2.5 小练习与答案

**练习 1**：迁移 `face()` 时，调用点 `img = scipy.misc.face()` 需要改吗？

**参考答案**：不需要。`scipy.datasets.face` 与旧 `scipy.misc.face` 同名同签名（都接受 `gray=False`）。只需把数据来源从 `scipy.misc` 换成 `scipy.datasets`，调用点 `face()` 一字不改。

**练习 2**：为什么 `scipy.datasets` 选「按需下载」而不是继续把 `face.dat` 打进安装包？

**参考答案**：把示例数据打进安装包会让 scipy 安装体积膨胀，且数据无法独立于 scipy 版本更新；按需下载 + 缓存 + 哈希校验把「计算库」与「示例数据」解耦，数据可放在独立的 `dataset-<name>` 仓库随时更新，也避免了给所有用户强加一份他们未必需要的数据。

---

### 4.3 数值工具迁移：scipy.misc.derivative / central_diff_weights → 手写有限差分

#### 4.3.1 概念说明

这是迁移里**最需要动脑**的一类。`scipy.misc.derivative` 和 `scipy.misc.central_diff_weights` **在 scipy 里没有任何替代品**——无论是公开 API 还是私有实现。

这一点很容易被误解。历史上它们确实委托给一个私有模块 `scipy._lib._finite_differences`（旧 `_common.py` 里就是 `from scipy._lib._finite_differences import _central_diff_weights, _derivative`）。但那次大清理（PR #21864）把**这套私有实现也一并删除了**。我们可以用一条 grep 当场证伪「它还在」：

```bash
# 在 scipy 仓库根目录执行
grep -rn "def _central_diff_weights\|def _derivative" scipy/
# 期望输出：空（当前 HEAD 已无任何定义）
```

我已在本讲的源码探查阶段确认：当前 HEAD 下，`scipy/_lib/_finite_differences.py` 这个文件**已不存在**，`def _central_diff_weights` / `def _derivative` 在整个 `scipy/` 目录里**搜不到任何匹配**。所以你**不能**「偷偷 import `scipy._lib._finite_differences`」来绕过迁移——那是个不存在的模块。

那 SciPy 当年建议怎么迁？答案写死在旧函数的弃用消息里。还原旧 `_common.py` 可以看到：

```bash
git show 43fc97efa8^:scipy/misc/_common.py
```

其中 `derivative` 的装饰器原文是（节选自上面的 `git show` 输出）：

```python
@_deprecated(msg="scipy.misc.derivative is deprecated in "
                 "SciPy v1.10.0; and will be completely removed in "
                 "SciPy v1.12.0. You may consider using "
                 "findiff: https://github.com/maroba/findiff or "
                 "numdifftools: https://github.com/pbrod/numdifftools")
def derivative(func, x0, dx=1.0, n=1, args=(), order=3):
    ...
```

也就是说，**官方推荐**改用第三方库 [findiff](https://github.com/maroba/findiff) 或 [numdifftools](https://github.com/pbrod/numdifftools)。

为什么 scipy 不内置替代？因为有限差分是个**有专门库做得更好**的领域（支持任意阶、任意网格、自动步长控制、复步差分等）。scipy 当年那两个函数只是薄封装，留下来反而是维护负担。**但如果你只是想求一阶导数**，完全没必要引第三方库——十几行手写代码就够了（见 4.3.4）。

#### 4.3.2 核心流程

`scipy.misc.derivative(func, x0, dx, n, args, order)` 的本质是：在 `x0` 附近取 `order` 个等距点，用**中心差分权重**加权求和来近似 `func` 的 `n` 阶导数。

对最常见的 **一阶导数（`n=1`）**，二阶精度（`order=3`）的中心差分公式是：

\[ f'(x_0) \approx \frac{f(x_0+h) - f(x_0-h)}{2h} \]

其截断误差为 \( O(h^2) \)（主项 \( -\dfrac{h^2}{6}f'''(x_0) \)）。注意：对 `order=3, n=1` 而言，这个三点公式的权重正好是 \( w=[-\tfrac{1}{2},\,0,\,\tfrac{1}{2}] \)，与旧 `central_diff_weights(3)` 返回值一致。

更一般地，对 \( N_p \) 个等距点、一阶导数的中心差分权重，可由** Vandermonde 线性方程组**求得：设采样偏移 \( x_k = k - h_o \)，\( h_o=\lfloor N_p/2\rfloor \)，要求权重 \( w \) 满足

\[ \sum_{k=0}^{N_p-1} w_k\, x_k^{\,j} = \begin{cases}1 & j=1\\0 & j\neq 1\end{cases},\quad j=0,1,\dots,N_p-1 \]

解这个 \( N_p\times N_p \) 方程组即得权重 \( w \)，再用

\[ f'(x_0) \approx \frac{1}{h}\sum_{k=0}^{N_p-1} w_k\, f\!\bigl(x_0 + (k-h_o)\,h\bigr) \]

求值。当 `n>1`（高阶导数）或需要非均匀网格、自动步长时，建议直接上 numdifftools/findiff，别手写。

#### 4.3.3 源码精读

由于当前 HEAD 已无相关源码可引，这里以**历史版本**为对照材料。运行下面的命令可看到旧 `derivative` 的完整签名与「转发给私有实现」的事实：

```bash
git show 43fc97efa8^:scipy/misc/_common.py | sed -n '1,20p'
```

关键点：

- 旧 `_common.py` 顶部 `from scipy._lib._finite_differences import _central_diff_weights, _derivative` —— 真身在那个私有模块。
- `derivative` 函数体只有一行 `return _derivative(func, x0, dx, n, args, order)`，签名是 `derivative(func, x0, dx=1.0, n=1, args=(), order=3)`。
- `central_diff_weights(Np, ndiv=1)` 同样是 `return _central_diff_weights(Np, ndiv)`。

而旧 docstring 给了一个**可用来核对手写实现是否正确**的标准例子：

```python
>>> from scipy.misc import derivative
>>> def f(x):
...     return x**3 + x**2
>>> derivative(f, 1.0, dx=1e-6)
4.9999999999217337
```

解析上 \( f'(x)=3x^2+2x \)，\( f'(1)=5 \)，数值结果 4.9999999… 与之吻合（微小偏差来自浮点舍入）。我们手写的中心差分应当复现这个结果。

`central_diff_weights(3)` 的旧例子则给出权重 \( w=[-0.5,\,0,\,0.5] \) 的用法（用它加权 \( f \) 在三个点上的值再除以步长）——这也是 4.3.4 的核验基准。

#### 4.3.4 代码实践

**实践目标**：手写一个与旧 `scipy.misc.derivative(func, x0, dx, n=1, order=3)` 等价的一阶导数函数，并用旧 docstring 的例子核验。

**操作步骤**：

1. 新建 `myderivative.py`（**示例代码**，纯标准库 + numpy）：

   ```python
   import numpy as np

   def central_diff_weights(Np):
       """Np 点（奇数）一阶导数的中心差分权重，等价旧 central_diff_weights(Np)。"""
       ho = Np // 2
       x = np.arange(Np) - ho                      # 偏移: -ho,...,0,...,ho
       V = np.vander(x, Np, increasing=True)       # V[j,k] = x[k]**j
       rhs = np.zeros(Np); rhs[1] = 1.0            # 一阶导数: 系数 = 1
       return np.linalg.solve(V, rhs)              # 解 Vandermonde 方程组

   def derivative(func, x0, dx=1.0, n=1, order=3):
       """一阶导数(n=1)的中心差分，等价旧 scipy.misc.derivative(func, x0, dx, n=1, order=3)。"""
       if n != 1:
           raise NotImplementedError("n>1 请改用 numdifftools / findiff")
       w = central_diff_weights(order)
       ho = order // 2
       vals = [func(x0 + (k - ho) * dx) for k in range(order)]
       return float(np.dot(w, vals) / dx)
   ```

2. 核验脚本（**示例代码**）：

   ```python
   from myderivative import central_diff_weights, derivative

   # 核验 1：权重应与旧 central_diff_weights(3) 一致
   print(central_diff_weights(3))           # 期望 [-0.5, 0, 0.5]

   # 核验 2：复现旧 docstring 例子
   f = lambda x: x**3 + x**2
   print(derivative(f, 1.0, dx=1e-6))       # 期望 ≈ 5（旧值 4.9999999999217337）
   ```

3. 运行 `python 核验脚本.py`。

**需要观察的现象**：第一行打印 `[-0.5 0. 0.5]`；第二行打印一个无限接近 5 的值。

**预期结果**：

- `central_diff_weights(3)` ≈ `[-0.5, 0.0, 0.5]`。
- `derivative(f, 1.0, dx=1e-6)` ≈ `5.0`（与旧 docstring 的 `4.9999999999217337` 在浮点精度内一致）。

> 待本地验证：具体末位数字因平台浮点实现略有差异，但整数部分应为 5。若你的旧脚本用到 `n>1`（二阶及以上导数）或 `order` 很大，请改用 numdifftools/findiff，不要扩展上面的玩具实现——高阶有限差分的步长选择和稳定性是专门的课题。

#### 4.3.5 小练习与答案

**练习 1**：为什么不能「为了少改代码」直接 `from scipy._lib._finite_differences import _derivative`？

**参考答案**：因为在当前 HEAD，`scipy/_lib/_finite_differences.py` 这个文件连同其中的 `_derivative` / `_central_diff_weights` 已经被 PR #21864 **彻底删除**（本讲已用 grep 确认整个 `scipy/` 目录无任何 `def _derivative`）。这条 import 会直接 `ModuleNotFoundError`。即便它还在，带下划线前缀也是私有 API，scipy 不承诺稳定。

**练习 2**：手写的 `derivative` 里，把 `dx` 取得「越小越精确」对吗？

**参考答案**：不对。中心差分有 \( O(h^2) \) 的**截断误差**（随 \( h \) 减小而减小），但浮点运算又有**舍入误差**（随 \( h \) 减小、两个相近数相减而放大）。两者此消彼长，存在一个最优步长；一味缩小 `dx` 反而会让结果变差。这正是旧 docstring「Decreasing the step size too small can result in round-off error」要提醒的事，也是推荐用专门库（它们能自适应选步长）的理由之一。

---

### 4.4 收尾：屏蔽残留警告与 python -W error 兜底

#### 4.4.1 概念说明

迁移「自己的调用」不难，难的是**确认全链路干净**。真实项目里，弃用警告常常来自**间接依赖**：你某个第三方库 `import foo`，而 `foo` 内部还在 `import scipy.misc`，于是你明明没写 `scipy.misc`，CI 里却冒出弃用警告。处理分两个阶段：

- **迁移期**：用 `warnings.catch_warnings` / `filterwarnings` 临时静音，避免淹没日志，但**只针对「确实暂时无法改」的来源**，并留 TODO。
- **验收期**：用 `python -W error::DeprecationWarning` 把弃用警告升级成异常。一旦脚本能在这个开关下**干净退出**，就证明它（及其直接依赖）已不再触发任何 scipy 弃用警告。

> 注意：`-W error::DeprecationWarning` 也会捕获**其他库**的弃用警告，不只是 scipy 的。这通常正是你想要的——把所有「未来要塌的坑」提前暴露。

#### 4.4.2 核心流程

Python 警告过滤的优先级是「**后注册的、更具体的规则优先**」。常用三层：

```text
1. 命令行开关（最粗）：
   python -W error::DeprecationWarning script.py      # 所有 DeprecationWarning → 异常
   python -W ignore::DeprecationWarning script.py     # 静音所有弃用警告

2. 代码内全局过滤（迁移期临时用，尽量窄）：
   warnings.filterwarnings("ignore", category=DeprecationWarning,
                           module="legacy_pkg")        # 只静音来自 legacy_pkg 的

3. 上下文管理器（最精确，作用域最小）：
   with warnings.catch_warnings():
       warnings.simplefilter("ignore")
       import legacy_pkg                                # 仅这一段静音
```

> 真实库的实践可参考 u1-l2 提到的 `scipy/_lib/tests/test_public_api.py`：它用 `filterwarnings("ignore", "scipy.misc", DeprecationWarning)` 精确屏蔽来自 `scipy.misc` 的弃用警告，避免自身测试被噪音干扰——这正是「窄过滤」的好范例。

#### 4.4.3 源码精读

为什么要兜底？因为只要 `scipy.misc` 这个桩模块还在，**任何一处** `import scipy.misc` 就会触发它那 6 行警告：

[scipy/misc/__init__.py:1-6](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/misc/__init__.py#L1-L6) — 导入即执行 `warnings.warn("scipy.misc is deprecated and will be removed in 2.0.0", DeprecationWarning, stacklevel=2)`。

也就是说，迁移的目标可量化为：**在你的脚本及其直接依赖里，再也找不到任何会触发上面这行 `warnings.warn` 的导入路径**。`-W error::DeprecationWarning` 就是这个目标的可执行断言——干净通过 = 迁移完成；抛异常 = 还有漏网之鱼。

#### 4.4.4 代码实践

**实践目标**：对迁移后的脚本做「警告兜底校验」，证明它不再触发任何 `DeprecationWarning`。

**操作步骤**：

1. 假设你已完成 4.2、4.3 的迁移，得到 `migrated.py`（只用 `scipy.datasets`，不再碰 `scipy.misc`）。

2. 普通运行：`python migrated.py` —— 观察是否还有 `scipy.misc` 相关警告（理想情况下没有）。

3. 兜底运行：

   ```bash
   python -W error::DeprecationWarning migrated.py
   ```

**需要观察的现象**：

- 普通运行：终端**不出现** `scipy.misc is deprecated ...` 字样。
- 兜底运行：脚本**正常跑完并退出**，没有抛出 `DeprecationWarning`（被升级成的）异常。

**预期结果**：迁移干净的脚本在 `-W error::DeprecationWarning` 下**零异常退出**，退出码 0。

> 待本地验证：若兜底运行抛了异常，看 traceback 里的 `warnings.warn` 来源——`stacklevel=2` 会把归因指向**真正触发导入的那一行**（见 u2-l1），顺藤摸瓜就能找到残留的 `import scipy.misc`（或间接依赖）。修掉它再跑，直到干净。

#### 4.4.5 小练习与答案

**练习 1**：`python -W error` 与 `python -W error::DeprecationWarning` 有何区别？迁移 scipy.misc 时该用哪个？

**参考答案**：前者把**所有**警告升级成异常（包括 `RuntimeWarning`、`FutureWarning` 等，容易误伤）；后者只升级 `DeprecationWarning`。迁移 scipy.misc 时，目的是「揪出所有弃用入口」，用 `-W error::DeprecationWarning` 更精准，不会被无关的运行时警告干扰。

**练习 2**：迁移期你想临时静音某个第三方库 `legacy_pkg` 内部触发的 `scipy.misc` 警告，哪种过滤方式最安全？

**参考答案**：用**带 `module` 参数的 `filterwarnings`**（如 `warnings.filterwarnings("ignore", category=DeprecationWarning, module="legacy_pkg")`），作用面窄、可定位；最忌讳用 `simplefilter("ignore")` 全局静音——那会把你**自己代码**里残留的 `scipy.misc` 警告也一起藏起来，违背「迁移期只静音确实暂时改不了的来源」的原则。

---

## 5. 综合实践

现在把四个最小模块串起来，完成本手册的**毕业任务**。

**任务背景**：你在接手一个老项目，发现一个脚本 `legacy_demo.py` 长这样（**示例代码**，模拟待迁移代码）：

```python
# legacy_demo.py —— 待迁移的旧脚本
import scipy.misc

img = scipy.misc.face()                                   # 数据集
slope = scipy.misc.derivative(lambda x: x**3 + x**2,
                              1.0, dx=1e-6)               # 数值工具

print("face shape:", img.shape)
print("slope at x=1:", slope)
```

在当前 HEAD 上，这个脚本会先打印一条 `DeprecationWarning`，然后在 `scipy.misc.face` 处抛 `AttributeError` 崩溃。

**你的任务**：把它完整迁移为 `migrated_demo.py`，满足三个验收标准：

1. 运行 `python migrated_demo.py` 能正确输出结果，**不再出现** `scipy.misc` 相关警告。
2. `face` 走 `scipy.datasets.face()`；`derivative` 用本讲 4.3.4 手写的 `derivative`（或 numdifftools）。
3. 用 `python -W error::DeprecationWarning migrated_demo.py` 运行，**零异常退出**。

**参考迁移方案**（**示例代码**）：

```python
# migrated_demo.py
import warnings
import numpy as np
import scipy.datasets                       # 数据集新家

# —— 手写的一阶中心差分（等价旧 scipy.misc.derivative, n=1, order=3）——
def central_diff_weights(Np):
    ho = Np // 2
    x = np.arange(Np) - ho
    V = np.vander(x, Np, increasing=True)
    rhs = np.zeros(Np); rhs[1] = 1.0
    return np.linalg.solve(V, rhs)

def derivative(func, x0, dx=1.0, order=3):
    w = central_diff_weights(order)
    ho = order // 2
    vals = [func(x0 + (k - ho) * dx) for k in range(order)]
    return float(np.dot(w, vals) / dx)
# ————————————————————————————————————————————————————————————————

img = scipy.datasets.face()                 # 取代 scipy.misc.face()
slope = derivative(lambda x: x**3 + x**2,
                  1.0, dx=1e-6)             # 取代 scipy.misc.derivative(...)

print("face shape:", img.shape)             # 期望 (768, 1024, 3)
print("slope at x=1:", slope)               # 期望 ≈ 5.0
```

**验收步骤**：

1. `python migrated_demo.py`
   - 期望输出 `face shape: (768, 1024, 3)` 和 `slope at x=1: 5.0...`。
   - 期望终端**没有** `scipy.misc is deprecated` 字样。
2. `python -W error::DeprecationWarning migrated_demo.py`
   - 期望**零异常退出**，退出码 0。
3. （可选挑战）把 `derivative` 改用 [numdifftools](https://github.com/pbrod/numdifftools) 实现（`import numdifftools as nd; slope = nd.Derivative(f)(1.0)`），对比两种实现的数值结果。

> 待本地验证：首次运行 `scipy.datasets.face()` 需联网下载 `face.dat`；无网络环境需先手动把 `dataset-face` 仓库内容放进缓存目录（见 4.2.3 的平台路径）。`numdifftools` 需 `pip install numdifftools`，属可选挑战。

**迁移清单（交付物）**：用一张表总结你这次迁移改了什么，便于 Code Review：

| 旧调用 | 迁移后 | 改动类型 |
| --- | --- | --- |
| `import scipy.misc` | `import scipy.datasets` | 换 import 来源 |
| `scipy.misc.face()` | `scipy.datasets.face()` | 调用点不变 |
| `scipy.misc.derivative(...)` | 手写 `derivative(...)` / numdifftools | 重写逻辑 |

---

## 6. 本讲小结

- **先分类再动手**：`scipy.misc` 的旧名字去向不同——数据集换 `import` 即可，数值工具必须重写，common/doccer 直接删。
- **旧脚本不是「能跑+警告」，而是「直接崩」**：当前 HEAD 的 `scipy/misc/__init__.py` 不定义 `face/derivative`，也没有 PEP 562 兜底，访问即 `AttributeError`——迁移是修崩溃，不是优化。
- **数据集迁移最省事**：`scipy.misc.face` → `scipy.datasets.face`，同名同签名，调用点零改动；代价是首次联网下载（pooch + 缓存 + SHA256）。
- **数值工具无 scipy 替代**：`derivative/central_diff_weights` 连私有实现 `_finite_differences.py` 都被删了；官方建议 findiff/numdifftools，一阶导数也可手写中心差分。
- **中心差分可手写**：一阶导数 \( f'(x_0)\approx\dfrac{f(x_0+h)-f(x_0-h)}{2h} \)，权重可由 Vandermonde 方程组求得；高阶/自适应请用专门库。
- **用 `-W error::DeprecationWarning` 兜底**：脚本能在此开关下零异常退出，即证明迁移完成；迁移期静音警告务必「窄过滤」，别全局 `simplefilter("ignore")`。

---

## 7. 下一步学习建议

本讲是 `scipy.misc` 学习手册的**最后一篇**，到这里你已经走完了一个 Python 模块从「杂物箱」到「墓碑桩」再到「被迁出代码」的**完整退役生命周期**。接下来建议：

1. **回头通读全册**：把 u1→u3 九篇连起来读一次，把它当作「**如何读懂并治理一个正在退役的模块**」的范本——这套方法（读目录→看桩→挖 git 历史→分流向→迁移+兜底）可迁移到任何弃用治理任务。
2. **深入 `scipy.datasets`**：本讲只用到 `face`，建议继续读 `_registry.py`（SHA256 校验表与 `dataset-<name>` 仓库映射）、`_download_all.py`、`_utils.clear_cache`，理解 pooch 的缓存与校验机制。
3. **试试有限差分专门库**：用 [numdifftools](https://github.com/pbrod/numdifftools) 或 [findiff](https://github.com/maroba/findiff) 重写本讲的高阶导数场景，体会「专门库 > scipy 薄封装」的设计取舍。
4. **学一套通用弃用治理工具**：回顾 u2-l2 的 `scipy/_lib/deprecation.py`（`_deprecated` 装饰器、`_NoValue` 哨兵、`_sub_module_deprecation` 软弃用），试着在自己的项目里复刻一个「先警告、跨版本、再删除」的弃用流程。
5. **关注 SciPy 2.0.0**：本手册写就时 `scipy.misc` 计划在 **SciPy 2.0.0 完全移除**。届时桩模块连那 6 行警告都会消失，`import scipy.misc` 会直接 `ModuleNotFoundError`——届时可回来更新本册，把「墓碑桩」章节改写成「墓志铭」。
