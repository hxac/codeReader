# 顶层 shim 包与安装入口

## 1. 本讲目标

本讲聚焦仓库根目录的「门面」层。读完本讲，你应该能够：

- 说清 `pip install ktransformers` 真正往环境里装的是什么，以及为什么。
- 读懂根目录的 `ktransformers.py`、`setup.py`、`pyproject.toml`、`version.py` 四个文件各自的职责。
- 解释 `[sft]` 与 `[sglang]` 两个 extras（可选依赖）的作用，并知道何时该用 `ktransformers[sft]`。
- 理解 `has_sft_support()` 的探测原理，能据此判断当前环境是否可做微调。
- 解释版本号 `0.6.3.post1` 是如何被顶层包和 `kt-kernel` 共享的。

本讲是承接 [u1-l2 仓库目录结构] 的最后一块入门拼图：u1-l2 告诉你「代码都在 `kt-kernel/`」，本讲告诉你「为什么根目录还留着这几个文件，它们如何把安装请求转发给 `kt-kernel`」。

## 2. 前置知识

在学习本讲前，建议你先了解几个打包相关的基础概念。这些概念不复杂，但理解它们会让后面的源码一目了然。

### 2.1 什么是「shim（垫片）」包

Shim 直译为「垫片」。在软件里，shim 是一个**只负责转发请求、自身几乎不含逻辑**的薄层。KTransformers 顶层包就是一个 shim：它叫 `ktransformers`，但真正干活的运行时叫 `kt-kernel`。顶层包存在的意义是：让用户可以用熟悉的 `pip install ktransformers`，而不用记住新的包名 `kt-kernel`。

### 2.2 Python 包的两种「名字」

一个常见的混淆点：发布名和导入名可以不一样。

| | 发布名（pip 用） | 导入名（`import` 用） |
|---|---|---|
| 顶层门面包 | `ktransformers`（连字符/普通） | `ktransformers` |
| 真正的运行时 | `kt-kernel`（连字符） | `kt_kernel`（下划线） |

pip 安装时用连字符名（`pip install kt-kernel`），但 Python 里 `import` 时用下划线名（`import kt_kernel`）。这是因为 Python 的模块名不能含连字符，而 PyPI 包名习惯用连字符。setuptools 会通过 `package-dir` 配置把两者对应起来，后面会看到。

### 2.3 什么是 extras（可选依赖）

`extras` 是 pip 的「可选功能依赖」机制。写法是包名后加方括号：

- `pip install ktransformers`：只装核心。
- `pip install ktransformers[sft]`：核心 + 微调所需依赖。
- `pip install ktransformers[sglang]`：核心 + 推理服务引擎依赖。

它让同一个包能面向「只想推理」和「想微调」的两类用户，而不强迫所有人都装一份庞大的依赖。

### 2.4 `importlib.metadata` 是什么

这是 Python 标准库里用来**读取已安装包元信息**的模块。装好一个包后，它的版本号、依赖等信息会被记录在环境的元数据里。`importlib.metadata.version("ktransformers")` 就是去查这个记录。本讲的 `ktransformers.py` 会用它来获取版本号。

## 3. 本讲源码地图

本讲涉及四个根目录文件，它们都很短，合起来构成「安装转发」链路。

| 文件 | 行数 | 作用 |
|---|---|---|
| [version.py](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/version.py) | 6 | 全仓库唯一的版本号来源，被顶层和 kt-kernel 共享。 |
| [ktransformers.py](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/ktransformers.py) | 36 | 顶层 import 入口，提供 `__version__` 和 `has_sft_support()`。 |
| [setup.py](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/setup.py) | 30 | 顶层打包脚本，声明「安装 ktransformers ⇒ 安装 kt-kernel」及两个 extras。 |
| [pyproject.toml](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/pyproject.toml) | 25 | 顶层包的 PEP 621 元数据，标记 version/dependencies 为 dynamic，并声明顶层模块。 |

此外会对照引用 [kt-kernel/setup.py](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/setup.py) 中读取共享版本号的一小段，用来印证「版本号单源」设计。

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：**shim 模块**、**extras 依赖**、**版本管理**。

### 4.1 shim 模块：`ktransformers.py`

#### 4.1.1 概念说明

shim 模块要解决的问题是：仓库运行时已经搬到 `kt-kernel/`，但项目对外仍希望沿用 `ktransformers` 这个老名字（用户文档、历史脚本、搜索引擎里的资料都认这个名字）。如果直接让根目录空着，`import ktransformers` 会报 `ModuleNotFoundError`；如果在根目录再复制一份完整代码，又会和 `kt-kernel/` 产生两份维护负担。

折中方案就是 shim：根目录只放一个极薄的 `ktransformers.py`，它只暴露极少量信息（版本号、能力探测），**不重复任何运行时逻辑**。真正的推理/微调能力通过 `pip install` 依赖关系拉取 `kt-kernel` 提供。这样 `import ktransformers` 能成功，而重量级代码只有一份。

#### 4.1.2 核心流程

当用户在装好包的环境里执行 `import ktransformers` 时，发生的事：

1. Python 定位到顶层模块 `ktransformers.py` 并执行它。
2. 模块尝试用 `importlib.metadata.version("ktransformers")` 读取**已安装元数据**里的版本号。
3. 若读到（正常安装情况），把结果赋给 `__version__`。
4. 若读不到（例如直接在源码目录里跑、未真正安装），回退去读同目录下的 `version.py` 文件。
5. 模块定义 `has_sft_support()` 函数，但**不立即执行**它（惰性探测）。
6. `__all__` 声明对外只导出 `__version__` 和 `has_sft_support` 两个名字。

#### 4.1.3 源码精读

先看模块顶部的 docstring，它一句话点明了 shim 的存在意义：

[ktransformers.py:1-6](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/ktransformers.py#L1-L6) —— 说明运行时内核位于 `kt-kernel`，SFT 通过 `ktransformers[sft]` 激活。

接下来是版本读取的两条路径。第一条是「回退路径」——直接读 `version.py`：

[ktransformers.py:14-18](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/ktransformers.py#L14-L18) —— `_read_repo_version()` 用 `Path(__file__).resolve().with_name("version.py")` 找到同目录的版本文件，`exec` 执行它并取出 `__version__` 变量。

> 小贴士：`exec` 把文件内容当代码跑，结果塞进字典 `ns`，再从 `ns["__version__"]` 取值。这是 Python 里「读取一个只含赋值的配置文件」的常见手法，比 `import` 它更不容易产生副作用。

第二条是「主路径」——优先用已安装元数据：

[ktransformers.py:21-24](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/ktransformers.py#L21-L24) —— 先 `version("ktransformers")` 查元数据；抛 `PackageNotFoundError` 时才退回 `_read_repo_version()`。这样 `pip install` 装好的版本号是权威的，源码目录裸跑也能拿到值。

最后是 SFT 能力探测函数：

[ktransformers.py:27-32](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/ktransformers.py#L27-L32) —— `has_sft_support()` 尝试 `import kt_kernel.sft`，成功返回 `True`，任何异常都返回 `False`。

注意它捕获的是裸 `Exception` 且用 `# noqa: F401` 抑制「导入了却没用」的告警——因为这里的目的就是**探测能否导入**，导入本身即是结果。

> 为什么用「尝试导入」而不是去查依赖列表？因为「某个包是否装了」不等于「SFT 链路是否真能跑起来」。直接试着 import 最贴近真实可用性：只要 `kt_kernel.sft` 子包及其全部传递依赖能干净加载，就认为 SFT 可用。SFT 代码里会用到 `transformers`（来自 `[sft]` extra 提供的 `transformers-kt` 分支）等依赖，若这些缺失，导入链就会在某处断掉，函数返回 `False`。

#### 4.1.4 代码实践

**实践目标**：亲手验证 shim 模块的两条版本读取路径，并对比「已安装」与「源码裸跑」两种情形。

**操作步骤**：

1. 在仓库根目录直接执行（模拟源码裸跑、未安装）：
   ```bash
   cd <仓库根目录>
   python -c "import ktransformers; print(ktransformers.__version__)"
   ```
2. 观察输出：这里 `importlib.metadata.version("ktransformers")` 很可能抛 `PackageNotFoundError`，于是走 `_read_repo_version()` 回退路径，从 `version.py` 读到 `0.6.3.post1`。
3. （可选）如果你已经 `pip install` 过该包，重复同一条命令，看版本号是否一致——此时走的是元数据主路径。

**需要观察的现象**：两种路径都应打印同一个版本号 `0.6.3.post1`，因为最终都追溯到 `version.py`。

**预期结果**：输出 `0.6.3.post1`。

**待本地验证**：若你的环境里同时残留着旧版 `ktransformers` 元数据，主路径可能返回旧版本号；这是主路径与回退路径少数会分歧的情形，可借机体会「为何要优先信任元数据」。

#### 4.1.5 小练习与答案

**练习 1**：如果把根目录的 `version.py` 删掉，且环境里从未 `pip install` 过 `ktransformers`，`import ktransformers` 会发生什么？

**参考答案**：模块加载时 `version("ktransformers")` 抛 `PackageNotFoundError`，进入 `_read_repo_version()`；该函数 `read_text()` 找不到 `version.py` 会抛 `FileNotFoundError`，导致整个 `import ktransformers` 失败。可见 `version.py` 是 shim 的硬依赖。

**练习 2**：`has_sft_support()` 为什么写成函数，而不是一个在 import 时就计算好的布尔常量？

**参考答案**：因为 SFT 依赖可能在「import ktransformers 之后」才被装上（例如先 `pip install ktransformers`，后来又 `pip install ktransformers[sft]`）。写成函数意味着每次调用都**实时探测**当前环境，能反映安装后的最新状态；常量则会在首次 import 时被固化，错过后续变化。

---

### 4.2 extras 依赖：`setup.py` 与 `pyproject.toml`

#### 4.2.1 概念说明

shim 模块解决了「import 得到」，但还没解决「装得到」。这部分由 `setup.py` 负责：它声明**安装顶层包时必须连带安装 `kt-kernel`**，以及两个可选功能各自需要的额外包。

`pyproject.toml` 则是现代 Python 打包的元数据入口（PEP 621）。它把 `version`、`dependencies`、`optional-dependencies` 三项标记为 `dynamic`，意思是「这些值不在 toml 里写死，而由 `setup.py` 在构建时动态提供」。这样版本号和依赖列表只有一处真相（`setup.py` + `version.py`），不会出现两处不一致。

#### 4.2.2 核心流程

构建顶层 wheel 时：

1. pip 读 `pyproject.toml` 的 `[build-system]`，得知用 setuptools 构建。
2. setuptools 调用 `setup.py`。
3. `setup.py` 用 `exec` 读 `version.py` 得到版本号 `_v`（例如 `0.6.3.post1`）。
4. `setup.py` 调用 `setup()`，传入：
   - `version=_v`
   - `install_requires=[f"kt-kernel=={_v}"]` —— 用**相同版本号**钉死 kt-kernel。
   - `extras_require={"sft": [...], "sglang": [...]}` —— 两个可选依赖组。
5. 最终 wheel 的元数据里记下：装 `ktransformers` 必装 `kt-kernel==0.6.3.post1`。

用户安装时：

- `pip install ktransformers` → 解析依赖 → 装 `kt-kernel==0.6.3.post1`。
- `pip install ktransformers[sft]` → 额外装 `transformers-kt==5.6.0.post1`、`accelerate-kt==1.14.0.post1`。
- `pip install ktransformers[sglang]` → 额外装 `sglang-kt==0.6.3.post1`。

#### 4.2.3 源码精读

先看 `setup.py` 如何读取版本号，与 shim 里的 `_read_repo_version()` 是同一套手法：

[setup.py:10-13](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/setup.py#L10-L13) —— 定位 `version.py`，`exec` 出 `__version__`，存入 `_v`。

核心的 `setup()` 调用，集中体现了「转发安装」与「可选能力」：

[setup.py:15-29](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/setup.py#L15-L29) —— 三件事一目了然：

- `version=_v`：顶层包版本随 `version.py`。
- `install_requires=[f"kt-kernel=={_v}"]`：**这是整条转发链的关键一行**。它用 f-string 把版本号插值进依赖声明，保证顶层包与 kt-kernel 版本严格对齐。`==` 是「严格等于」，意味着装 `ktransformers 0.6.3.post1` 就一定拉 `kt-kernel 0.6.3.post1`。
- `extras_require`：`sft` 组钉 `transformers-kt==5.6.0.post1` 与 `accelerate-kt==1.14.0.post1`（这两个是上游 HuggingFace `transformers`/`accelerate` 的 KTransformers 定制分支，固定到具体补丁号）；`sglang` 组钉 `sglang-kt=={_v}`（与主包同版本）。

再看 `pyproject.toml` 的元数据与动态声明：

[pyproject.toml:5-16](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/pyproject.toml#L5-L16) —— `name = "ktransformers"`，`dynamic = ["version", "dependencies", "optional-dependencies"]` 明示这三项交给 `setup.py` 动态提供；`requires-python = ">=3.11"` 给出 Python 版本下限。

最后一条很关键——它说明为什么根目录的 `ktransformers.py` 会被打进 wheel：

[pyproject.toml:21-24](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/pyproject.toml#L21-L24) —— `py-modules = ["ktransformers"]` 告诉 setuptools「把根目录的 `ktransformers.py` 作为一个顶层模块打进发布包」。注释解释了动机：这样发布包可被 `import`，而不必在仓库根目录再维护一个平行的源码包目录。这正是 shim 能在安装后被 import 到的原因。

#### 4.2.4 代码实践

**实践目标**：在不真正联网安装的前提下，看清顶层 wheel 的依赖关系是如何被声明的。

**操作步骤**：

1. 阅读上一节引用的 [setup.py:15-29](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/setup.py#L15-L29)。
2. 在本地执行（只解析、不安装，`--dry-run` 让 pip 只打印计划）：
   ```bash
   pip install --dry-run "ktransformers[sft]" 2>/dev/null | head -40
   ```
   若离线/无 PyPI 访问，可改为直接读 wheel 的 METADATA：
   ```bash
   # 若本地已构建 wheel（如 dist/ktransformers-*.whl）：
   python -c "import zipfile,sys,glob; f=glob.glob('dist/ktransformers-*.whl')[0]; m=[n for n in zipfile.ZipFile(f).namelist() if n.endswith('METADATA')][0]; print(''.join(l for l in zipfile.ZipFile(f).read(m).decode().splitlines(keepends=True) if l.startswith(('Name:','Version:','Requires-Dist:'))))"
   ```

**需要观察的现象**：应能看到形如 `Requires-Dist: kt-kernel==0.6.3.post1`、`Requires-Dist: transformers-kt==5.6.0.post1; extra == "sft"`、`Requires-Dist: sglang-kt==0.6.3.post1; extra == "sglang"` 的行。

**预期结果**：METADATA 里 `Name: ktransformers`、`Version: 0.6.3.post1`，且核心依赖只有 `kt-kernel`，其余依赖都带 `extra == "..."` 条件标记。

**待本地验证**：若没有本地 wheel，`--dry-run` 走的是 PyPI；若网络不通则只能依据源码推断，结论同样成立但需标注「依据源码，未实跑」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `install_requires` 用 `kt-kernel=={_v}`（严格等于）而不是 `kt-kernel>={_v}`（大于等于）？

**参考答案**：顶层 shim 与 kt-kernel 共享同一套接口约定（shim 通过 `import kt_kernel.sft` 直接依赖其内部结构），两者必须**完全匹配**。用 `>=` 可能拉到一个接口已变化的新版 kt-kernel，导致 `has_sft_support()` 或后续 API 行为不一致。`==` 把顶层包和运行时锁死在同一版本，避免「装了但配不上」。

**练习 2**：`transformers-kt` 和 `accelerate-kt` 为什么钉的是固定补丁号（如 `5.6.0.post1`），而 `sglang-kt` 用 `{_v}` 跟随主版本？

**参考答案**：这是项目对稳定性的取舍。`transformers-kt`/`accelerate-kt` 是对 HuggingFace 上游的定制分支，接口相对独立，钉死补丁号可避免上游意外升级破坏 SFT；而 `sglang-kt` 与 kt-kernel 同属一套发布节奏，版本号一起走，故用 `{_v}` 与主包对齐。

---

### 4.3 版本管理：单源真相（Single Source of Truth）

#### 4.3.1 概念说明

当仓库里有多个包（顶层 `ktransformers`、运行时 `kt-kernel`）时，最怕的就是**版本号散落在多处**：顶层写一个、kt-kernel 写一个，发版时忘了同步，就会出现「顶层 0.6.3 拉到 kt-kernel 0.6.2」的错配。

KTransformers 的做法是「单源真相」：全仓库只有 [version.py](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/version.py) 一个地方写版本号，所有打包脚本都来读它。发版时只改这一个文件。

#### 4.3.2 核心流程

版本号的产生与消费链路：

1. 发版工程师把 [version.py](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/version.py) 里的 `__version__` 改成新值（例如从 `0.6.2` 改成 `0.6.3.post1`）。
2. 构建顶层包时，`setup.py`（[setup.py:10-13](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/setup.py#L10-L13)）读它 → 顶层包版本 = 该值 → `kt-kernel==该值`。
3. 构建 kt-kernel 时，`kt-kernel/setup.py` 也读同一个文件 → kt-kernel 版本 = 同一个值。
4. 运行时 `import ktransformers`，`ktransformers.py` 先查元数据、再回退读 `version.py` → 用户看到的 `__version__` 也是同一个值。

于是「顶层版本 = kt-kernel 版本 = import 显示的版本」三者天然一致，因为它们来自同一个源头。

#### 4.3.3 源码精读

版本号的唯一真相，就在这 6 行：

[version.py:1-6](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/version.py#L1-L6) —— docstring 明确写「Shared across the top-level package and kt-kernel」（顶层包与 kt-kernel 共享），`__version__ = "0.6.3.post1"`。

顶层 `setup.py` 已经在 4.2 节看过它如何消费这个值。下面看 kt-kernel 那一侧如何**跨目录**读到同一个文件：

[kt-kernel/setup.py:743-750](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/setup.py#L743-L750) —— 关键是 `Path(__file__).resolve().parent.parent / "version.py"`：从 `kt-kernel/setup.py` 往上跳两级（`kt-kernel/` → 仓库根），定位到根目录的 `version.py`。找不到时退回一个默认值 `0.6.1`，并允许用环境变量 `CPUINFER_VERSION` 覆盖（用于测试场景）。

> 这种「`parent.parent` 往上找」的写法把两个子项目的版本号硬绑在一起。好处是单源；代价是 kt-kernel 的构建隐式依赖「仓库根有 version.py」，所以从仓库里单独抠出 `kt-kernel/` 目录去构建会退回到默认版本——一个值得注意的耦合点。

把消费方与源头对照，就能画出版本数据流：

| 消费者 | 读取方式 | 出处 |
|---|---|---|
| 顶层 `setup.py` | `exec(version.py)` | [setup.py:10-13](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/setup.py#L10-L13) |
| 顶层 `ktransformers.py`（回退） | `exec(version.py)` | [ktransformers.py:14-18](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/ktransformers.py#L14-L18) |
| kt-kernel `setup.py` | 读 `../version.py` | [kt-kernel/setup.py:743-750](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/setup.py#L743-L750) |

#### 4.3.4 代码实践

**实践目标**：验证「改一处，处处变」的单源特性。

**操作步骤**：

1. 记录当前版本：
   ```bash
   cd <仓库根目录>
   python -c "exec(open('version.py').read()); print(__version__)"
   ```
2. 用临时副本做实验，**不要改原文件**：
   ```bash
   cp version.py /tmp/version.bak
   sed -i 's/0.6.3.post1/9.9.9.test/' version.py
   python -c "import ktransformers; print(ktransformers.__version__)"
   ```
3. 立刻还原：
   ```bash
   cp /tmp/version.bak version.py
   python -c "exec(open('version.py').read()); print(__version__)"
   ```

**需要观察的现象**：第 2 步执行 `import ktransformers` 后，若环境未正式安装（走回退路径），应打印 `9.9.9.test`；第 3 步还原后回到 `0.6.3.post1`。

**预期结果**：版本号随 `version.py` 单一改动而联动变化，证明它是唯一源头。

**待本地验证**：若环境已正式安装 `ktransformers`，`import` 走的是元数据主路径，不会反映 `version.py` 的临时改动（会继续显示安装时的版本号）。这恰好印证了 4.1 节「主路径优先信任元数据」的设计——实验时请注意区分两条路径。

> ⚠️ 本实践会临时修改源文件，务必在第 3 步还原，且不要提交该改动。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `version.py` 放在仓库根目录，而不是放在 `kt-kernel/` 或顶层包目录里？

**参考答案**：因为它要被**顶层包和 kt-kernel 两个子项目共享**。放在根目录，两边都能用相对路径平等地访问（顶层 `setup.py` 用同目录、kt-kernel `setup.py` 用 `parent.parent`）。放进任一子项目都会让另一个子项目的访问变得别扭，破坏「单源、对称」的意图。

**练习 2**：`kt-kernel/setup.py` 在读不到 `version.py` 时退回 `0.6.1`。这个回退值可能带来什么风险？

**参考答案**：若有人把 `kt-kernel/` 单独拷出去构建，会得到版本号 `0.6.1` 的 kt-kernel，而顶层包期望的是 `kt-kernel==0.6.3.post1`。版本不匹配会导致 `pip install ktransformers` 拒绝安装这个 kt-kernel（`==` 不满足），或在使用中出现接口不一致。回退值只是「构建不至于崩」的兜底，并非正确性的保证。

---

## 5. 综合实践

把本讲三个模块串起来，完成一次「端到端」的认知验证。

**任务**：作为新接手 KTransformers 的开发者，你需要向团队说明「用户执行 `pip install ktransformers[sft]` 之后，环境里到底多了什么、`import ktransformers` 时会发生什么」。请按下列步骤完成一份一页纸说明（文字或图均可）：

1. **画出依赖关系图**：以 `ktransformers` 为中心，画出 `install_requires` 拉出的 `kt-kernel`，以及 `[sft]`/`[sglang]` 两个 extras 分别拉出的包。标注每个版本号约束的来源（都指向 `version.py`）。
2. **列出 import 时的执行步骤**：参考 4.1.2，按顺序写出 `import ktransformers` 触发的版本读取与函数定义过程，标明主路径与回退路径的分叉点。
3. **运行验证命令**（本讲的核心实践任务）：
   ```bash
   python -c "import ktransformers; print(ktransformers.__version__, ktransformers.has_sft_support())"
   ```
   记录输出的两个值，并解释：
   - `__version__` 这个值是从哪条路径来的（元数据 or `version.py`）？依据是什么？
   - `has_sft_support()` 的布尔结果意味着什么？如果它是 `False`，要让 SFT 可用应执行哪条 pip 命令？
4. **给出结论**：用一句话总结「为什么 `pip install ktransformers` 实际装的是 kt-kernel」。

**预期结果示例**（供对照，具体值以你本地为准）：

- 若未装 `[sft]` extra：`0.6.3.post1 False`。
- 若装了 `ktransformers[sft]`：`0.6.3.post1 True`。

**待本地验证**：`has_sft_support()` 的真实取值取决于你是否安装了 SFT 所需的 `transformers-kt`/`accelerate-kt`，请以本地实际输出为准，不要照抄示例。

## 6. 本讲小结

- 顶层 `ktransformers` 是一个 **shim（垫片）包**：自身几乎不含逻辑，只通过 `install_requires` 把安装请求转发给真正的运行时 `kt-kernel`。
- `pip install ktransformers` 实际装的是 `kt-kernel==<同版本>`，这由 [setup.py](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/setup.py#L15-L29) 的 `install_requires=[f"kt-kernel=={_v}"]` 决定。
- 两个 extras 分管两类可选能力：`[sft]` 拉微调依赖（`transformers-kt`、`accelerate-kt`），`[sglang]` 拉推理服务引擎（`sglang-kt`）。
- `has_sft_support()` 通过**尝试 `import kt_kernel.sft`** 来实时探测当前环境是否具备 SFT 能力，而非静态查依赖列表。
- 版本号遵循**单源真相**：全仓库只有 [version.py](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/version.py#L1-L6) 一处定义，顶层 `setup.py`、`ktransformers.py`、`kt-kernel/setup.py` 三处都读它，保证三者版本天然一致。
- `pyproject.toml` 的 `py-modules = ["ktransformers"]` 是 shim 能被打进 wheel、被 `import` 到的关键。

## 7. 下一步学习建议

本讲讲清了「装的是什么」，接下来你应该了解「怎么装」和「装完怎么用」：

- **构建与安装细节**：进入单元 2。建议先读 [u2-l1 安装方式总览](u2-l1-installation-overview.md)，对比 PyPI 预编译 wheel 与源码 `./install.sh` 两条路径；再看 [u2-l2 构建系统与 CPU 指令集配置](u2-l2-build-cmake-cpu.md)，理解 `kt-kernel/CMakeLists.txt` 的构建选项。
- **运行时 CPU 变体**：本讲提到 `import kt_kernel`，但它如何挑选 AMX/AVX 变体？见 [u2-l3 运行时 CPU 变体检测与加载](u2-l3-cpu-variant-detect.md)。
- **直接验证**：现在就可以执行综合实践里的那条 `python -c` 命令，带着真实输出进入下一讲，学习会更有抓手。
