# 目录结构与库入口

## 1. 本讲目标

学完本讲后，你应该能够：

- 画出 `src/llmcompressor` 的顶层目录树，并说出 `entrypoints`、`core`、`modifiers`、`pipelines`、`recipe`、`datasets`、`observers`、`modeling`、`utils` 等子包各自的职责。
- 看懂 `src/llmcompressor/__init__.py`（包入口）暴露了哪些公开 API，以及一个容易踩坑的细节：`__all__` 与「真正能 `import` 的名字」并不完全一致。
- 准确说出 `oneshot` 函数定义在哪个文件、哪一行，并理解 `entrypoints` 子包是「压缩流程入口的集合」。
- 建立「功能 → 子包」的定位直觉，为后续阅读任意压缩流程源码打下基础。

本讲是 u1-l1（项目定位与核心概念）的直接延续：u1-l1 让你建立了 `modifier / recipe / oneshot` 三个高层概念，本讲带你打开 `src/llmcompressor` 这个「柜子」，看清每个抽屉里装的是什么。

## 2. 前置知识

### 2.1 Python 包与 `__init__.py`

Python 里一个目录只要包含 `__init__.py` 就是一个**包（package）**，目录里的其他 `.py` 文件是它的**子模块**。`__init__.py` 就是这个包的「门面」：

- 当你写 `import llmcompressor` 时，Python 执行的是 `src/llmcompressor/__init__.py`。
- 这个门面文件里 `from ... import ...` 进来的名字，就成了 `llmcompressor.xxx` 可以直接访问的公开 API。

所以**读一个库的 `__init__.py`，就是读这个库的「公开接口清单」**。这是快速理解任何 Python 库的第一步。

### 2.2 `__all__` 是什么

`__all__` 是一个列表，只控制一件事：`from llmcompressor import *` 时会导入哪些名字。它**不会**阻止你用 `llmcompressor.oneshot` 访问那些没写进 `__all__` 的名字。本讲你会看到 llm-compressor 正是利用了这一点。

### 2.3 回顾：oneshot / recipe / modifier

- **oneshot**：最常用的入口函数，一次性完成「校准 + 压缩」。
- **recipe**：压缩配方，是一个或多个 modifier 的有序集合。
- **modifier**：单个压缩动作（量化、剪枝、平滑等）。

这三个词在 u1-l1 已建立，本讲只关心它们「住在哪个目录」。

## 3. 本讲源码地图

本讲只读两个最关键的「门面」文件，以及用来定位的入口文件：

| 文件 | 作用 |
| --- | --- |
| [`src/llmcompressor/__init__.py`](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/__init__.py) | 整个库的包入口，定义了 `oneshot`、`active_session` 等顶层 API |
| [`src/llmcompressor/entrypoints/__init__.py`](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/__init__.py) | 「入口集合」子包的门面，汇聚 `Oneshot / oneshot / model_free_ptq / pre_process / post_process` |
| [`src/llmcompressor/entrypoints/oneshot.py`](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/oneshot.py) | `Oneshot` 类与 `oneshot()` 函数的真正定义处 |
| [`src/llmcompressor/core/session_functions.py`](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/core/session_functions.py) | 全局会话函数 `create_session / active_session / reset_session` 的定义处 |

> 说明：`version.py` 在 `__init__.py` 里被 `from .version import __version__, version` 引用，但它是安装时生成的文件，不在 git 仓库中跟踪，所以本讲不展开。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：先看清**顶层目录全貌**（4.1），再精读**包入口 `__init__.py`**（4.2），最后聚焦**入口子包 `entrypoints`**（4.3）。

### 4.1 顶层包 `llmcompressor` 的目录划分

#### 4.1.1 概念说明

llm-compressor 是一个「积木型」的库：它把压缩流程拆成若干职责单一的子包，每个子包解决一类问题。理解目录划分，等于拿到了一张「功能索引表」——以后想找某个功能，先定位子包，再进子包找具体文件，而不是在几百个文件里盲目搜索。

#### 4.1.2 核心流程：目录树与职责

下面这张目录树只画出**顶层子包**和有代表性的二级文件（真实存在于当前 HEAD），帮助你建立结构印象。完整文件列表可以在本地用 `git ls-files src/llmcompressor` 查看。

```
src/llmcompressor/
├── __init__.py          # 包入口：暴露 oneshot / active_session 等顶层 API
├── logger.py            # 日志配置
├── version.py           # 版本号（安装时生成，不在 git 中）
│
├── entrypoints/         # 【入口集合】压缩流程的对外入口
│   ├── oneshot.py           #    Oneshot 类 + oneshot() 函数
│   ├── utils.py             #    pre_process / post_process
│   └── model_free/          #    无模型定义量化(model_free_ptq)
│
├── core/                # 【核心引擎】Session / State / Lifecycle / Events
│   ├── session.py           #    CompressionSession 会话容器
│   ├── state.py             #    State: model/data/hardware
│   ├── lifecycle.py         #    CompressionLifecycle 生命周期
│   ├── session_functions.py #    全局 create_session/active_session
│   └── events/              #    Event / EventType 事件系统
│
├── modifiers/           # 【压缩算法】所有 modifier 实现与工厂
│   ├── modifier.py          #    Modifier 抽象基类
│   ├── factory.py           #    ModifierFactory 自动发现与注册
│   ├── interface.py         #    ModifierInterface 接口
│   ├── quantization/        #    量化(QuantizationModifier / Mixin / calibration)
│   ├── gptq/                #    GPTQ 算法
│   ├── pruning/             #    剪枝(SparseGPT/Wanda/Magnitude/REAP)
│   ├── smoothquant/         #    SmoothQuant
│   ├── transform/           #    变换类(AWQ/SmoothQuant transform/QUIP)
│   └── ...
│
├── pipelines/           # 【校准管线】编排校准流程
│   ├── registry.py          #    管线注册表与 from_modifiers 选择
│   ├── sequential/          #    逐层(子图)校准，最常用
│   ├── independent/         #    每个 modifier 独立一个校准 epoch
│   ├── basic/               #    基础整模型校准
│   └── data_free/           #    无需校准数据(如 RTN)
│
├── recipe/              # 【压缩配方】recipe 的解析/序列化/校验
│   ├── recipe.py            #    Recipe 数据模型
│   └── metadata.py          #    模型/层/数据集元数据
│
├── datasets/            # 【校准数据】把数据集/dataloader 统一成校准输入
│   └── utils.py             #    get_calibration_dataloader 等
│
├── observers/           # 【张量统计】MinMax/MSE/IMatrix 等 observer
│   ├── base.py
│   ├── min_max.py
│   ├── mse.py
│   ├── imatrix.py
│   └── fusion.py
│
├── modeling/            # 【模型建模】融合/线性化/MoE/特殊架构适配
│   ├── fuse.py              #    层融合
│   ├── offset_norm.py       #    校准时的 norm 偏移
│   ├── moe/                 #    MoE 线性化与专家校准上下文
│   ├── deepseekv32/         #    DeepSeek-V3.2 专用建模
│   └── patch/               #    特定模型 patch
│
├── transformers/        # 【HF 适配】数据集与 transformers 工具的桥接
│   ├── data/                #    内置数据集(c4/wikitext 等)
│   └── utils.py
│
├── args/                # 【参数系统】Model/Dataset/Recipe 参数类 + parse_args
│   ├── model_arguments.py
│   ├── dataset_arguments.py
│   └── recipe_arguments.py
│
├── pytorch/             # 【PyTorch 工具】PyTorch 相关辅助
│
└── utils/               # 【通用工具】dev/dist/helpers/transformers 通用函数
    ├── dev.py               #    设备搬运(GPU/CPU/offload)
    ├── dist.py              #    分布式通信(DDP)
    └── helpers.py
```

每个子包门面文件里都有一段 docstring 说明它的职责，下面挑「练习要求」的几个子包，给出真实 docstring 的一句话提炼：

| 子包 | 一句话职责（取自各自 `__init__.py` 的 docstring） |
| --- | --- |
| `entrypoints` | 提供模型压缩工作流的入口（oneshot 压缩、训练、前后处理工具） |
| `core` | 提供核心压缩框架：管理会话(session)、跟踪状态(state)、处理事件(events)、提供生命周期钩子(lifecycle) |
| `modifiers` | 提供压缩动作系统：基类、工厂模式、接口，用于量化/剪枝等优化技术 |
| `pipelines` | 编排不同压缩策略的校准管线(basic/sequential/independent/data-free 等) |
| `recipe` | 定义和管理压缩工作流的配方(recipe)，支持分阶段(stage)执行 |
| `datasets` | 校准数据工具：格式化校准数据、创建 dataloader、切分数据集 |
| `observers` | 监控与分析压缩过程的张量统计（min-max/MSE/IMatrix 等 observer） |
| `modeling` | 压缩前的模型准备与融合(fuse)、模块准备、结构优化 |

#### 4.1.3 源码精读

以 `pipelines` 子包为例，它的门面 [`src/llmcompressor/pipelines/__init__.py`](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/__init__.py) 用「导入即注册」的方式，把所有具体管线拉进命名空间：

```python
# populate registry
from .basic import *
from .data_free import *
from .independent import *
from .registry import *
from .sequential import *
```

[`src/llmcompressor/pipelines/__init__.py:13-17`](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/pipelines/__init__.py#L13-L17) 这几行 `import *` 的副作用是触发各子包把自己注册进 `registry`。也就是说：**导入 `pipelines` 包本身，就完成了管线注册**。这种「门面导入 = 自动注册」的模式在 `modifiers`（通过 `ModifierFactory` 遍历）和 `observers`（`from .base import *` 等）里也常见。

再看 `modifiers` 的门面 [`src/llmcompressor/modifiers/__init__.py:10-12`](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/modifiers/__init__.py#L10-L12)，它只暴露三个最核心的名字：

```python
from .factory import ModifierFactory
from .interface import ModifierInterface
from .modifier import Modifier
```

注意：这里**没有**导入 `QuantizationModifier` 或 `GPTQModifier`。具体的算法 modifier 是由 `ModifierFactory` 在运行时遍历 `modifiers/` 的子包自动发现的（这是 u2-l4 的主题）。这是一个关键的目录设计思想：**门面只暴露骨架，具体算法靠工厂动态收集**。

#### 4.1.4 代码实践：手画目录树

1. **实践目标**：把抽象的「目录划分」变成自己笔下的一张可定位的地图。
2. **操作步骤**：
   - 在项目根目录执行 `git ls-files src/llmcompressor`，拿到完整文件清单。
   - 参考本讲 4.1.2 的树，自己重新画一遍顶层结构（不要照抄，凭理解写）。
   - 对 `entrypoints / core / modifiers / pipelines / recipe / datasets / observers / modeling` 八个核心子包，各用一句中文写出职责（提示：可以打开各自的 `__init__.py` 看 docstring）。
3. **需要观察的现象**：你会发现几乎所有压缩功能都能归到这八个子包之一；找不到归属的功能，多半在 `utils/` 或 `transformers/`。
4. **预期结果**：得到一张与 4.1.2 类似但由你自己组织的目录树，以及八句职责说明。
5. 运行命令的输出需要**待本地验证**（取决于你本地是否已 `git clone` 完整仓库）。

#### 4.1.5 小练习与答案

**练习 1**：如果你要新增一个叫 `HQQModifier` 的量化算法，应该把代码放在哪个子包下？
**答案**：放在 `src/llmcompressor/modifiers/quantization/`（或新建 `modifiers/hqq/`）下。因为 `ModifierFactory` 会遍历 `modifiers/` 的子包自动发现以 `Modifier` 结尾的类，放对位置就能被自动注册（详见 u2-l4）。

**练习 2**：`pipelines/__init__.py` 里的 `from .sequential import *` 为什么重要？
**答案**：它的副作用是触发 `sequential` 子包把自己注册进管线注册表 `registry`。如果删掉这行，`SequentialPipeline` 就不会被自动注册，`CalibrationPipeline.from_modifiers` 将无法选中它。

---

### 4.2 包入口 `__init__.py` 暴露了哪些 API

#### 4.2.1 概念说明

`import llmcompressor` 之后，到底有哪些名字可以用？答案全在 [`src/llmcompressor/__init__.py`](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/__init__.py) 这个文件里。它是整个库对外的「总门面」，把散落在各子包的功能「提升」到顶层，方便用户直接调用。

#### 4.2.2 核心流程：三组导入

这个门面文件把公开 API 分成三组导入：

1. **版本与日志**：`__version__`、`version`、`configure_logger`、`logger`、`LoggerConfig`（来自 `.logger` 和 `.version`）。
2. **全局会话**：`active_session`、`callbacks`、`create_session`、`reset_session`（来自 `core.session_functions`）。
3. **压缩入口**：`Oneshot`、`oneshot`、`model_free_ptq`（来自 `entrypoints`）。

这里有一个**关键陷阱**需要特别讲解：这三组里只有第一组写进了 `__all__`，后两组没有。这意味着：

- `from llmcompressor import *` 只会拿到版本与日志相关名字；
- 但 `llmcompressor.oneshot`、`llmcompressor.active_session` 这些**依然可以正常访问**，因为它们已经被 import 到了模块命名空间。

所以判断「哪些是公开 API」时，**不要只看 `__all__`，要看文件里实际 import 了哪些名字**。

#### 4.2.3 源码精读

先看门面的开头，定义 `__all__` 并导入版本与日志（注意第 10 行的 `# ruff: noqa`，它关闭了代码检查，因为后面的 import 出现在 `__all__` 之后，正常会被 linter 报「模块级 import 未置于文件顶部」）：

[`src/llmcompressor/__init__.py:12-21`](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/__init__.py#L12-L21)

```python
from .logger import LoggerConfig, configure_logger, logger
from .version import __version__, version

__all__ = [
    "__version__",
    "version",
    "configure_logger",
    "logger",
    "LoggerConfig",
]
```

接着是后两组「提升到顶层」的导入，它们**没有**出现在 `__all__` 里，但仍是官方公开 API：

[`src/llmcompressor/__init__.py:23-29`](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/__init__.py#L23-L29)

```python
from llmcompressor.core.session_functions import (
    active_session,
    callbacks,
    create_session,
    reset_session,
)
from llmcompressor.entrypoints import Oneshot, oneshot, model_free_ptq
```

为什么把会话函数也提到顶层？因为它们是用户在「手动驱动压缩」时最常用的控制点：`active_session()` 拿到当前全局会话，`create_session()` 新建并切换会话，`reset_session()` 把会话重置。这三个函数的真正定义在 `core/session_functions.py`：

[`src/llmcompressor/core/session_functions.py:36-52`](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/core/session_functions.py#L36-L52) 定义了上下文管理器 `create_session`，它在进入时新建一个 `CompressionSession` 并设为当前活跃会话，退出时恢复原会话；

[`src/llmcompressor/core/session_functions.py:55-60`](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/core/session_functions.py#L55-L60) 的 `active_session()` 则简单地返回当前线程本地存储里的会话（若没有则回退到一个全局默认会话 `_global_session`）。

> 这部分属于「核心引擎」，u2-l1 会深入讲 `CompressionSession` 与 `State`。本讲只需记住：**这些会话函数被提到了顶层，是公开 API 的一部分**。

#### 4.2.4 代码实践：打印顶层公开 API

1. **实践目标**：亲手验证「`__all__` 与实际可访问名字不一致」这个现象。
2. **操作步骤**：写一段小脚本（**示例代码**，非项目原有代码）：

   ```python
   import llmcompressor

   # 1) __all__ 里有什么
   print("__all__ =", llmcompressor.__all__)

   # 2) 实际能访问、但不在 __all__ 里的公开 API
   for name in ["oneshot", "Oneshot", "model_free_ptq",
                "active_session", "create_session", "reset_session"]:
       obj = getattr(llmcompressor, name, None)
       print(f"{name:20s} -> {obj}")
   ```
3. **需要观察的现象**：`__all__` 只打印出 5 个版本/日志相关名字；但 `oneshot`、`active_session` 等都能取到真实对象（不是 `None`）。
4. **预期结果**：`oneshot` 指向一个函数对象，`active_session` 也是函数对象；`__all__` 列表里**不**包含它们。这印证了 4.2.2 的结论。
5. 由于依赖你本地已安装 `llmcompressor`，具体输出**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `__init__.py` 顶部要写 `# ruff: noqa`？
**答案**：因为 `from ... import ...`（第 23–29 行）出现在 `__all__` 赋值之后，没有放在文件最顶部，会被 ruff 的 E402 规则（module level import not at top of file）报警。`# ruff: noqa` 关闭了对整个文件的这类检查。

**练习 2**：执行 `from llmcompressor import *` 后，能用 `oneshot` 吗？
**答案**：不能直接用。因为 `oneshot` 不在 `__all__` 里，`import *` 不会把它带进来。你需要显式写 `from llmcompressor import oneshot`，或用 `import llmcompressor; llmcompressor.oneshot(...)`。

---

### 4.3 入口子包 `entrypoints`：压缩流程的入口集合

#### 4.3.1 概念说明

`entrypoints`（入口）子包是「压缩流程对外入口」的集合。u1-l2 你已经用过 `oneshot`，本节告诉你它住在哪、还有哪些「兄弟入口」。理解这个子包，就理解了「用户能直接调用的压缩动作都有哪些」。

#### 4.3.2 核心流程：三个入口 + 两个工具

`entrypoints/__init__.py` 汇聚了五样东西：

| 名字 | 类型 | 来自 | 用途 |
| --- | --- | --- | --- |
| `Oneshot` | 类 | `.oneshot` | 一次性压缩的类封装 |
| `oneshot` | 函数 | `.oneshot` | 一次性压缩的便捷函数（最常用） |
| `model_free_ptq` | 函数 | `.model_free` | 无模型定义的权重量化 |
| `pre_process` | 函数 | `.utils` | 压缩前预处理 |
| `post_process` | 函数 | `.utils` | 压缩后处理（保存等） |

其中 `oneshot` 是日常最常用的；`model_free_ptq` 适用于「没有 HuggingFace 模型定义、只有 safetensors 权重文件」的超大模型场景（u5-l3 详解）；`pre_process / post_process` 是 `Oneshot` 内部三阶段中的首尾两段（u1-l4 详解）。

#### 4.3.3 源码精读

`entrypoints` 的门面非常简洁，只有三行 import：

[`src/llmcompressor/entrypoints/__init__.py:10-12`](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/__init__.py#L10-L12)

```python
from .oneshot import Oneshot, oneshot
from .model_free import model_free_ptq
from .utils import post_process, pre_process
```

**重点：定位 `oneshot` 函数的定义位置。**

很多人以为 `oneshot` 定义在 `__init__.py` 里，其实不是。门面只是「转发」。顺着 `from .oneshot import oneshot`，真正的定义在 `entrypoints/oneshot.py`：

- `Oneshot` 类定义在 [`src/llmcompressor/entrypoints/oneshot.py:48`](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/oneshot.py#L48)（`class Oneshot:`）。
- `oneshot()` 函数定义在 [`src/llmcompressor/entrypoints/oneshot.py:306`](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/oneshot.py#L306)（`def oneshot(`）。

所以**「oneshot 函数定义所在的文件」就是 `src/llmcompressor/entrypoints/oneshot.py`**。`oneshot()` 这个便捷函数内部其实是构造一个 `Oneshot` 实例并调用它，这部分逻辑是 u1-l4 的主题。

`model_free_ptq` 同理，门面 `from .model_free import model_free_ptq` 指向 `model_free` 子包，其真正定义在 [`src/llmcompressor/entrypoints/model_free/__init__.py:41`](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/model_free/__init__.py#L41)（`def model_free_ptq(`），并在第 38 行声明 `__all__ = ["model_free_ptq"]`。

#### 4.3.4 代码实践：定位 `oneshot` 的定义

1. **实践目标**：学会「顺着门面 import 找到真正定义」的源码定位技巧。
2. **操作步骤**：
   - 打开 [`src/llmcompressor/entrypoints/__init__.py`](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/__init__.py)，看到 `from .oneshot import Oneshot, oneshot`。
   - 顺着它打开 `src/llmcompressor/entrypoints/oneshot.py`，定位第 48 行（`class Oneshot:`）和第 306 行（`def oneshot(`）。
   - 用编辑器或 `grep` 在 `oneshot.py` 里找到 `def oneshot`，阅读它的参数列表（`model`、`recipe`、`dataset` 等），对照 u1-l2 你用过的调用。
3. **需要观察的现象**：`oneshot()` 函数体里会创建 `Oneshot(...)` 实例并调用，印证「函数是类的薄包装」。
4. **预期结果**：你能向别人解释「`import llmcompressor; llmcompressor.oneshot` 这个名字，定义在 `entrypoints/oneshot.py` 第 306 行」。
5. 行号可能随版本变化，若你本地 HEAD 与本讲不一致，以本地为准（**待确认**）。

#### 4.3.5 小练习与答案

**练习 1**：`llmcompressor.oneshot` 和 `llmcompressor.entrypoints.oneshot` 是同一个对象吗？
**答案**：是。前者是顶层 `__init__.py` 通过 `from llmcompressor.entrypoints import ... oneshot` 转发上来的，后者是 `entrypoints/__init__.py` 通过 `from .oneshot import oneshot` 转发上来的，两者最终指向 `entrypoints/oneshot.py` 里的同一个函数对象。

**练习 2**：如果想做「没有模型定义」的量化，应该用哪个入口？
**答案**：用 `model_free_ptq`（定义在 `entrypoints/model_free/__init__.py:41`）。它直接对 safetensors 权重文件做权重量化，不需要实例化完整模型，适合超大模型（详见 u5-l3）。

---

## 5. 综合实践

把本讲三个模块串起来，完成一个「源码定位小任务」：

1. **读门面，列 API**：打开 [`src/llmcompressor/__init__.py`](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/__init__.py)，用一张表把「公开 API 名字 → 它的来源子包 → 它的真正定义文件」列出来。例如：

   | API | 来源子包 | 真正定义文件 |
   | --- | --- | --- |
   | `oneshot` | `entrypoints` | `entrypoints/oneshot.py:306` |
   | `active_session` | `core` | `core/session_functions.py:55` |
   | `model_free_ptq` | `entrypoints` | `entrypoints/model_free/__init__.py:41` |
   | `logger` | （顶层） | `logger.py` |

   自己补全 `create_session`、`reset_session`、`Oneshot`、`callbacks`、`__version__` 几行。

2. **画目录树 + 写职责**：参考 4.1.4，画出你自己的 `src/llmcompressor` 顶层目录树，并为 `entrypoints / core / modifiers / pipelines / recipe / datasets / observers / modeling` 各写一句中文职责。

3. **定位校验**：在终端运行 `python -c "import llmcompressor; print(llmcompressor.oneshot)"`，确认它是一个函数对象；再运行 `python -c "import llmcompressor; print(llmcompressor.oneshot.__module__)"`，确认打印出的模块路径指向 `llmcompressor.entrypoints.oneshot`，从而验证你在第 1 步的定位是否正确。

   > 预期 `__module__` 输出形如 `llmcompressor.entrypoints.oneshot`，这与本讲给出的定义文件一致；若未安装库则**待本地验证**。

完成这个综合实践后，你就拥有了「拿到任何一个 `llmcompressor.xxx` 名字，都能反查到它真正定义在哪个文件、属于哪个子包」的能力——这是后续阅读所有压缩流程源码的基础。

## 6. 本讲小结

- `src/llmcompressor` 由职责单一的子包组成：`entrypoints`（入口）、`core`（引擎）、`modifiers`（算法）、`pipelines`（校准管线）、`recipe`（配方）、`datasets`（数据）、`observers`（统计）、`modeling`（建模），加上 `args / utils / transformers / pytorch` 等辅助包。
- 包入口 [`src/llmcompressor/__init__.py`](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/__init__.py) 把三组 API 提升到顶层：版本/日志、全局会话（`active_session` 等）、压缩入口（`oneshot` 等）。
- **关键陷阱**：只有版本/日志名字进了 `__all__`，但 `oneshot`、`active_session` 等仍是可访问的公开 API——判断公开 API 要看实际 import，不能只看 `__all__`。
- `entrypoints` 是「压缩流程入口的集合」，门面只做转发：`oneshot` 真正定义在 [`src/llmcompressor/entrypoints/oneshot.py:306`](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/oneshot.py#L306)，`model_free_ptq` 在 [`src/llmcompressor/entrypoints/model_free/__init__.py:41`](https://github.com/vllm-project/llm-compressor/blob/2d7a7ea058793447faa40b75d285c7ce2111c11f/src/llmcompressor/entrypoints/model_free/__init__.py#L41)。
- 「门面导入 = 自动注册」是常见设计：`pipelines/__init__.py` 的 `import *` 负责触发各管线注册，`modifiers` 则靠 `ModifierFactory` 运行时遍历子包发现算法。
- 掌握「顺着门面 import 反查真正定义文件」的技巧，就掌握了在几百个文件里快速定位的钥匙。

## 7. 下一步学习建议

- **接着读 oneshot 内部**：本讲只定位了 `oneshot` 的定义，下一讲 **u1-l4（oneshot 入口与三阶段生命周期）** 会带你精读 `Oneshot` 类的 `pre_process → apply_recipe_modifiers → post_process` 三阶段，那是 oneshot 的真正核心。
- **想提前理解会话**：本讲提到的 `active_session / create_session` 属于核心引擎，建议在学完 u1-l4 后进入 **u2 单元**，从 **u2-l1（CompressionSession 与 State）** 开始读 `core/` 子包。
- **想理解目录里的算法子包**：等学完 `core` 与 `modifiers` 基类（u2），再按 u3/u4 的顺序进入 `quantization`、`gptq` 等算法子包，那时这张目录地图的每一格都会变得清晰。
