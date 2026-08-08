# 包结构与模块导出

## 1. 本讲目标

上一篇（u1-l1）我们已经知道 DFlash 是一个用于投机解码的「块扩散（block diffusion）草稿模型」，并通过 `pyproject.toml` 的可选依赖分组理解了四种后端的隔离安装方式。本讲我们把镜头拉近，钻进 **dflash 这个 Python 包本身**，看清它的目录、文件、以及它是如何把 API 暴露给使用者的。

学完本讲，你应当能够：

- 说出 dflash 包里四个源码文件（`__init__.py` / `model.py` / `model_mlx.py` / `benchmark.py`）各自的职责。
- 解释 `__init__.py` 里 `__all__` 与 `__getattr__` 的协作：`__all__` 声明公开契约，`__getattr__` 实现「按需导入」的懒加载。
- 理解为什么 dflash 要用懒加载，以及它如何让 `import dflash` 既快又不会被某个后端的重依赖拖垮。
- 说清顶层公开的四个 API：`DFlashDraftModel` / `extract_context_feature` / `sample` / `load_and_process_dataset`，以及它们分别来自哪个子模块。
- 看懂 `pyproject.toml` 里 `[tool.setuptools.packages.find]` 是如何把 `dflash/` 目录识别为一个可安装包的。

## 2. 前置知识

在继续之前，确认你理解下面几个概念（不熟悉的术语下面都会解释）：

- **Python 包（package）**：一个含有 `__init__.py` 的目录。`import dflash` 时，Python 会执行 `dflash/__init__.py`，这个文件决定了「导入 dflash 这个名字时会发生什么」。
- **模块（module）**：一个 `.py` 文件。`dflash/model.py` 对应模块 `dflash.model`。
- **`__all__`**：一个写在模块顶层的列表，声明「这个模块对外公开哪些名字」。它影响 `from dflash import *` 的行为，也充当一份给读者的「公开 API 清单」。
- **模块级 `__getattr__`（PEP 562）**：从 Python 3.7 起，可以在模块里定义一个 `__getattr__(name)` 函数。当你访问「模块里原本不存在的属性」时，Python 会调用它。这正是懒加载的实现机制。dflash 要求 `requires-python = ">=3.10"`，完全满足。
- **懒加载（lazy import）**：把 `import` 推迟到「真正用到的时候」才执行，而不是模块一加载就全量导入。好处是启动快、按需付费、避免引入用不到的重依赖。

如果你已经读完 u1-l1，那么「DFlash = 草稿模型 + 目标模型配合加速」和「四种后端依赖冲突、要用独立虚拟环境安装」这两点是本讲理解懒加载动机的基础。

## 3. 本讲源码地图

本讲只聚焦「包的骨架」，涉及的真实源码很少，但每一处都会反复用到：

| 文件 | 行数级别 | 作用 | 本讲关注点 |
| --- | --- | --- | --- |
| [dflash/__init__.py](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/__init__.py) | 约 25 行 | 包入口，定义 `__all__` 与 `__getattr__` | 全篇核心 |
| [pyproject.toml](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/pyproject.toml) | 约 38 行 | 项目元数据、依赖、包发现配置 | `packages.find` |
| [dflash/model.py](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py) | 约 300+ 行 | Transformers/PyTorch 参考实现 | 只看它「被懒加载」 |
| [dflash/model_mlx.py](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model_mlx.py) | 约 400+ 行 | Apple MLX 实现 | 只看它「不在 `__all__` 里」 |
| [dflash/benchmark.py](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/benchmark.py) | 约 500+ 行 | 评测 CLI | 只看它「贡献一个导出」 |

> 说明：本讲对 `model.py` / `model_mlx.py` / `benchmark.py` 只做「职责定位」，不深入它们的算法。这三个文件的内部机制分别属于第二、三单元的讲义。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

1. dflash 包的目录结构与四个源码文件
2. `__all__`：顶层公开 API 契约
3. `__getattr__`：按需导入的懒加载分发
4. `pyproject.toml`：包发现与可选依赖

### 4.1 dflash 包的目录结构与四个源码文件

#### 4.1.1 概念说明

dflash 是一个非常「小而密」的项目：整个可安装包只有 **4 个 Python 文件**，全部位于 `dflash/` 目录下，没有更深层的子包。这一点可以用只读 git 命令验证：

```bash
git ls-files dflash/
# 输出：
# dflash/__init__.py
# dflash/benchmark.py
# dflash/model.py
# dflash/model_mlx.py
```

四个文件的分工很清晰，可以按「角色」来记：

| 文件 | 角色 | 一句话职责 |
| --- | --- | --- |
| `__init__.py` | 门面（facade） | 声明公开 API，并用懒加载把请求转发到真正的实现文件 |
| `model.py` | Transformers 实现 | 用 PyTorch + HuggingFace Transformers 复用 Qwen3 组件，给出草稿模型的「参考实现」（CPU/GPU 可跑，适合读源码） |
| `model_mlx.py` | Apple MLX 实现 | 用 MLX + mlx_lm 实现，专为 Apple 芯片优化，还额外支持 Qwen3.5 的混合架构（GatedDeltaNet） |
| `benchmark.py` | 评测工具 | 提供数据集下载/缓存、CLI、多后端评测运行器与加速比指标 |

注意一个关键设计：**两种实现（Transformers 与 MLX）是彼此独立的**。`model.py` 依赖 `torch` + `transformers`，`model_mlx.py` 依赖 `mlx` + `mlx_lm`，这两套依赖互不兼容（回顾 u1-l1/u1-l2：四种后端要用独立虚拟环境隔离安装）。正因如此，包入口绝不能「一上来就同时 import 两个实现」——这就是后面懒加载要解决的问题。

#### 4.1.2 核心流程

当用户写下 `import dflash` 时，Python 的导入流程大致是：

1. 在 `sys.path` 里找到 `dflash` 目录。
2. 执行 `dflash/__init__.py`。
3. 把执行后得到的「模块对象」绑定到名字 `dflash`。

关键在于第 2 步：`__init__.py` 里**几乎没有顶层 import**（没有 `from .model import ...`），所以执行它非常轻量、非常快，也不会触发 torch/transformers/mlx 这些重依赖的加载。真正「拿东西」的动作被推迟到了属性访问时（见 4.3）。

可以用下面这张「导入时间线」理解：

```text
import dflash            # 执行 __init__.py（很轻），不加载 torch / mlx
dflash.DFlashDraftModel  # 触发 __getattr__ → from .model import ... （此刻才加载 torch/transformers）
```

#### 4.1.3 源码精读

先确认四个文件确实存在且各司其职。

- 包入口：[`dflash/__init__.py`](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/__init__.py) —— 全文只有 `__all__` 和 `__getattr__`，没有任何子模块的顶层 import，是「门面」。
- Transformers 实现的关键符号都集中在 [`dflash/model.py`](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L1-L21) 的开头：它 import 了 `torch` 和 `transformers.models.qwen3.modeling_qwen3` 的一整套 Qwen3 组件（如 `Qwen3RMSNorm`、`Qwen3MLP`、`Qwen3PreTrainedModel` 等）。文件里定义了三个会被导出的对象：
  - `extract_context_feature`（[model.py:39](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L39)）：把目标模型多层的隐藏状态拼接成草稿模型的上下文特征。
  - `sample`（[model.py:48](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L48)）：根据 logits 做采样（`temperature<1e-5` 走 `argmax`，否则走温度 `multinomial`）。
  - `DFlashDraftModel`（[model.py:302](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L302)）：草稿模型本体，继承自 `Qwen3PreTrainedModel`，因此天然能用 `from_pretrained` 加载权重。
- MLX 实现见 [`dflash/model_mlx.py`](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model_mlx.py#L1-L17)，开头 import 的是 `mlx.core` / `mlx.nn` 与 `mlx_lm` 的一系列组件；它的配置类是 `DFlashConfig`（[model_mlx.py:29](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model_mlx.py#L29)），模型类也叫 `DFlashDraftModel`（[model_mlx.py:132](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model_mlx.py#L132)，但这是 `mlx.nn.Module`，与 `model.py` 里那个同名但不同类）。
- 评测工具 [`dflash/benchmark.py`](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/benchmark.py#L1-L21) 开头 import 了 `argparse`、`requests`、`loguru`、`rich`、`tqdm`、`numpy` 等，定义了数据集配置表 `DATASETS`（[benchmark.py:28](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/benchmark.py#L28)）与会被导出的 `load_and_process_dataset`（[benchmark.py:84](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/benchmark.py#L84)）。

> 提示：两个实现里都有叫 `DFlashDraftModel` 的类，但它们分属 `dflash.model` 与 `dflash.model_mlx` 两个模块。顶层 `dflash.DFlashDraftModel` 指向的是 **Transformers 版**（见 4.3 的分发逻辑）。

#### 4.1.4 代码实践

**源码阅读型实践**（不需要 GPU 或额外依赖）：

1. 实践目标：在仓库里亲自核对四个文件的存在与体量，建立「小而密」的直观印象。
2. 操作步骤：在项目根目录执行 `git ls-files dflash/`，再用 `wc -l dflash/*.py` 查看每个文件的行数。
3. 需要观察的现象：输出应正好是 4 个文件；`__init__.py` 行数最少（二十几行），其余三个都在几百行量级。
4. 预期结果：与上面表格的「角色」一一对应。**待本地验证**（命令本身的输出不依赖任何环境，可放心运行）。

#### 4.1.5 小练习与答案

**练习 1**：为什么不把 Transformers 实现和 MLX 实现合并到同一个文件里？

> 参考答案：两者依赖的框架（`torch`/`transformers` 与 `mlx`/`mlx_lm`）互不兼容，且分别面向不同硬件（GPU 与 Apple 芯片）。合并会让单个文件同时背上两套冲突的重依赖，违背「独立虚拟环境隔离安装」的原则（回顾 u1-l2）。拆成两个文件后，包入口再用懒加载按需导入，就能保证用户只加载自己装了依赖的那一份。

**练习 2**：顶层 `dflash.DFlashDraftModel` 指向的是哪个实现？

> 参考答案：Transformers 实现（`dflash.model.DFlashDraftModel`）。依据是 `__getattr__` 的分发逻辑（见 4.3）。MLX 用户需要直接 `import dflash.model_mlx` 来获取 MLX 版的 `DFlashDraftModel`。

### 4.2 `__all__`：顶层公开 API 契约

#### 4.2.1 概念说明

`__all__` 是一份「白名单」：它声明「本模块对外公开哪些名字」。它有两个作用：

1. 控制 `from dflash import *` 时会被导入哪些名字。
2. 更重要的——作为给读者的**文档**：一眼就能看出这个包的「公开 API 面」有多大。

dflash 的 `__all__` 只列了 **4 个名字**，意味着整个包对外承诺的顶层 API 非常小：

```python
__all__ = [
    "DFlashDraftModel",
    "extract_context_feature",
    "load_and_process_dataset",
    "sample",
]
```

这四个名字的含义：

| 名字 | 来源 | 用途 |
| --- | --- | --- |
| `DFlashDraftModel` | `dflash.model` | 草稿模型类（Transformers 版），加载权重、做块式起草 |
| `extract_context_feature` | `dflash.model` | 从目标模型多层隐藏状态拼接上下文特征 |
| `sample` | `dflash.model` | 对 logits 做温度采样（argmax / multinomial） |
| `load_and_process_dataset` | `dflash.benchmark` | 下载并加载评测数据集（带 JSONL 缓存） |

注意：`model_mlx.py` **不在** `__all__` 里，也没有任何符号被顶层导出。MLX 是一条「旁路」，需要用户显式 `import dflash.model_mlx`。

#### 4.2.2 核心流程

`__all__` 本身只是一个普通列表，它的「约束力」来自 Python 对它的约定：

- 执行 `from dflash import *` 时，Python 会查找 `dflash.__all__`，只导入其中列出的名字（并且每个名字都会触发一次 `__getattr__`，因为它们在命名空间里并不存在）。
- 静态检查工具（IDE、linter）通常把 `__all__` 视为公开 API 标记，对未列入的名字访问给出提示。

一个重要事实：**`__all__` 并不会真的把这些名字「放进」模块命名空间**。它只是声明意图；真正「把名字绑定到对象」的工作由 4.3 的 `__getattr__` 在属性被访问时完成。

#### 4.2.3 源码精读

`__all__` 定义在 [`dflash/__init__.py:1-6`](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/__init__.py#L1-L6)。这段代码本身没有任何 import，所以加载它不会带入任何重依赖——这就是公开契约「轻量」的来源。

你可以把它读作一句话承诺：**「dflash 这个包对外只保证这四个名字可用，其余都算内部实现。」** 因此，`build_target_layer_ids`、`dflash_generate`、MLX 版的 `DFlashConfig` 等虽然存在于源码中，但都不属于稳定顶层 API，使用它们时应当走子模块路径（如 `from dflash.model import build_target_layer_ids`）。

#### 4.2.4 代码实践

**观察型实践**：

1. 实践目标：直观感受 `__all__` 对 `import *` 的过滤作用。
2. 操作步骤：在装好某个后端依赖的环境里（任意一个都可），进入 Python：
   ```python
   import dflash
   print(dflash.__all__)
   ```
3. 需要观察的现象：打印出 `['DFlashDraftModel', 'extract_context_feature', 'load_and_process_dataset', 'sample']`。
4. 预期结果：清单内容与源码一致；注意此时仍未真正加载 torch（访问 `__all__` 不会触发 `__getattr__`）。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：如果用户写 `from dflash import build_target_layer_ids`，能成功吗？

> 参考答案：能，但**不是因为 `__all__`**。`from dflash import X` 形式的具名导入，会先尝试在 `dflash` 命名空间找 `X`，找不到就调用 `__getattr__("build_target_layer_ids")`。而 `__getattr__` 只识别 `__all__` 里的四个名字，对其它名字一律 `raise AttributeError`（见 4.3.3），所以这次导入会**失败**。要拿到 `build_target_layer_ids`，应写成 `from dflash.model import build_target_layer_ids`，直接从子模块取。

**练习 2**：为什么 MLX 的符号一个都不在 `__all__` 里？

> 参考答案：MLX 依赖 `mlx`/`mlx_lm`，与 Transformers 后端的 `torch`/`transformers` 冲突。如果把 MLX 符号也放进顶层 `__all__`，那么在任何环境下访问这些顶层名字都可能要求 MLX 已安装，破坏「按后端隔离安装」的设计。让 MLX 用户显式 `import dflash.model_mlx`，就能把 MLX 的重依赖完全隔离在「只有 MLX 用户才会触发」的路径里。

### 4.3 `__getattr__`：按需导入的懒加载分发

#### 4.3.1 概念说明

这是本讲最核心的机制。模块级 `__getattr__`（PEP 562）让你**拦截「访问模块里不存在的属性」这一动作**。dflash 利用它实现了懒加载：

- `__init__.py` 顶层**没有** `from .model import ...`，所以 `DFlashDraftModel` 等名字一开始并不存在于 `dflash` 的命名空间。
- 当你写 `dflash.DFlashDraftModel` 时，Python 发现命名空间里没有这个名字，于是调用 `__getattr__("DFlashDraftModel")`。
- `__getattr__` 在此时才执行真正的 `from .model import ...`，把对象拿出来返回。

这样做的好处：

1. **启动快**：`import dflash` 不加载 torch/transformers/mlx，毫秒级完成。
2. **按需付费**：只有真正用到某 API 时才付加载代价；用不到就不加载。
3. **隔离冲突依赖**：在一个只装了 `.[mlx]` 的环境里，只要你不去访问 Transformers 路径的顶层 API，就不会触发 `model.py`（也就不会因为缺 `torch` 而报错）。

#### 4.3.2 核心流程

`__getattr__` 的分发逻辑可以画成一张简单的决策图：

```text
访问 dflash.<name>
        │
        ├─ name == "load_and_process_dataset" ?
        │        └─ 是 → from .benchmark import load_and_process_dataset → 返回它
        │
        ├─ name ∈ {DFlashDraftModel, extract_context_feature, sample} ?
        │        └─ 是 → from .model import 三个名字 → 按字典取出 name 对应的对象 → 返回
        │
        └─ 其它 → raise AttributeError
```

注意两个细节：

- **第一次访问后才缓存**：`__getattr__` 里用 `return` 把对象交还给 Python，但模块命名空间里并不会自动写入这个名字。不过 Python 解释器层面，`__getattr__` 返回的对象会作为该属性访问表达式的结果。后续是否再次进入 `__getattr__` 取决于实现，但对使用者而言，访问得到的就是同一个类/函数对象。
- **批量导入、按名取一个**：对 `model` 这一支，代码一次性 `from .model import DFlashDraftModel, extract_context_feature, sample`（三个都进了局部作用域），再用字典 `{"...": ...}[name]` 取出被请求的那一个返回。

#### 4.3.3 源码精读

完整的 `__getattr__` 见 [`dflash/__init__.py:9-24`](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/__init__.py#L9-L24)。逐段看：

**benchmark 分支**（[`__init__.py:10-13`](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/__init__.py#L10-L13)）：只有 `load_and_process_dataset` 这一个名字走这条路，命中后才 `from .benchmark import ...`。也就是说，不调评测 API，就完全不会加载 `benchmark.py`（及其 `datasets`/`requests`/`rich` 等依赖）。

**model 分支**（[`__init__.py:15-22`](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/__init__.py#L15-L22)）：三个 Transformers API 共用一条路。命中其中任意一个，都会触发 `from .model import DFlashDraftModel, extract_context_feature, sample`——这一刻 `torch` 与 `transformers` 才被真正加载。

**兜底报错**（[`__init__.py:24`](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/__init__.py#L24)）：不在白名单里的名字直接 `raise AttributeError`，给出清晰的错误信息。这也是为什么 `from dflash import build_target_layer_ids` 会失败——它走到了这一行。

把这段分发逻辑与 4.2 的 `__all__` 对照，你会发现二者**名字集合完全一致**：`__all__` 声明「我能提供这四个」，`__getattr__` 实现「这四个分别在哪儿拿」。它们一唱一和，构成了完整的公开 API 机制。

#### 4.3.4 代码实践

**观察型实践（核心任务）**：验证 `__getattr__` 确实是「首次访问属性时才被调用」。

1. 实践目标：用日志/计数器证明懒加载发生在「属性访问时」而非「import 时」。
2. 操作步骤（任选其一）：
   - **方法 A（monkey-patch 计数）**：进入 Python，先 `import dflash`，再给 `__getattr__` 包一层计数器：
     ```python
     import dflash
     orig = dflash.__getattr__
     calls = []
     def traced(name):
         calls.append(name)
         return orig(name)
     dflash.__getattr__ = traced

     print("导入后、访问前，calls =", calls)         # 期望 []
     _ = dflash.DFlashDraftModel                     # 触发一次
     print("访问 DFlashDraftModel 后，calls =", calls)  # 期望 ['DFlashDraftModel']
     ```
   - **方法 B（看 sys.modules）**：`import dflash` 后立刻 `print("transformers" in sys.modules)`（期望 `False`）；访问 `dflash.DFlashDraftModel` 后再查一次（期望 `True`）。
3. 需要观察的现象：访问属性**之前**，`__getattr__` 没被调用过、`transformers` 没被加载；访问属性**之后**，调用计数增加、`transformers` 出现在 `sys.modules`。
4. 预期结果：证明 `import dflash` 本身是轻量的，重依赖加载被推迟到首次属性访问。需要已安装 Transformers 后端依赖（`.[transformers]`）才能成功访问 `dflash.DFlashDraftModel`；否则这一步会抛 `ModuleNotFoundError: No module named 'torch'`（这恰好印证了懒加载的隔离效果）。**待本地验证**。

> 说明：方法 A 里替换 `dflash.__getattr__` 之所以有效，是因为 Python 在属性缺失时会查找模块对象的 `__getattr__` 属性。把它替换成 `traced` 后，后续缺失属性的访问都会先经过你的计数器。

#### 4.3.5 小练习与答案

**练习 1**：在一个只安装了 `.[mlx]`（没有 torch）的环境里，执行 `import dflash` 会报错吗？执行 `dflash.DFlashDraftModel` 呢？

> 参考答案：`import dflash` **不会报错**，因为 `__init__.py` 不 import torch。但 `dflash.DFlashDraftModel` **会报错**（`ModuleNotFoundError: No module named 'torch'`），因为这一步触发 `__getattr__` → `from .model import ...` → `model.py` 顶层 `import torch` 失败。这正体现了懒加载的价值：把「缺依赖」的报错推迟到真正需要该后端时，而不是一导入包就崩。

**练习 2**：如果想让顶层也暴露 `build_target_layer_ids`，需要改哪些地方？

> 参考答案：两处。①在 `__all__` 里加上 `"build_target_layer_ids"`；②在 `__getattr__` 里给它加一条分发（或把它并入现有的 `model` 分支集合），让它走 `from .model import build_target_layer_ids`。只改 `__all__` 不改 `__getattr__` 的话，访问时会落到兜底的 `raise AttributeError`。

### 4.4 pyproject.toml：包发现与可选依赖

#### 4.4.1 概念说明

`pyproject.toml` 是现代 Python 项目的「项目元数据 + 构建配置」入口。对 dflash 来说，它干了三件与本讲相关的事：

1. **声明项目身份**：名字 `dflash`、版本 `0.1.0`、描述、最低 Python 版本 `>=3.10`。
2. **告诉构建工具「包在哪」**：用 `[tool.setuptools.packages.find]` 自动发现 `dflash` 这个包。
3. **声明依赖**：核心依赖（所有后端都需要）写在 `dependencies`，四种后端的重依赖写在 `optional-dependencies` 的四个分组里。

其中第 2 点——`packages.find`——是本模块的重点，它解释了「为什么 `dflash/` 目录会被打包成可安装的 `dflash` 包」。

#### 4.4.2 核心流程

当用户执行 `uv pip install -e .`（可编辑安装）或 `pip install .` 时，setuptools 的构建流程大致是：

1. 读取 `pyproject.toml`，得知用 setuptools 构建（`[tool.setuptools.*`）。
2. 根据 `[tool.setuptools.packages.find]` 的 `include = ["dflash*"]`，在项目根目录下扫描名字匹配 `dflash*` 的目录（含 `__init__.py` 的才算包）。
3. 找到 `dflash/` → 识别为一个包，包名 `dflash`。
4. 安装时把 `dependencies` 里的依赖一并装上；`optional-dependencies` 只在显式指定 `.[组名]` 时才装。

`include = ["dflash*"]` 的作用是把发现范围**限定**到 `dflash` 包及其可能的子包（`dflash.xxx`），避免把仓库里其它无关目录（例如测试目录、本教程目录）误识别为包。

#### 4.4.3 源码精读

项目身份与核心依赖见 [`pyproject.toml:1-14`](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/pyproject.toml#L1-L14)。核心依赖故意保持「轻」：`rich`、`loguru`、`numpy`、`tqdm`、`datasets`、`requests`、`huggingface-hub`——这些都是「通用工具」，不带任何深度学习框架。注意 `torch`/`transformers`（model.py 需要）与 `mlx`/`mlx_lm`（model_mlx.py 需要）**都不在**核心依赖里，它们被放进可选分组。

包发现配置见 [`pyproject.toml:16-17`](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/pyproject.toml#L16-L17)：

```toml
[tool.setuptools.packages.find]
include = ["dflash*"]
```

可选依赖分组见 [`pyproject.toml:19-38`](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/pyproject.toml#L19-L38)，分别是 `transformers`、`sglang`、`vllm`、`mlx`，每组对应一种后端的重依赖（回顾 u1-l2）。把这些重依赖挡在核心之外，正是 4.3 懒加载能成立的前提：因为核心安装不带 torch/mlx，所以 `import dflash` 必须做到「不碰 torch/mlx」，否则裸安装就会崩。

> 串起来看：**`packages.find` 让 `dflash/` 成为可安装包 → 核心依赖故意不含框架 → `__init__.py` 用懒加载避免顶层 import 框架 → 四个 API 按需从子模块取**。这是一条贯穿「打包—依赖—导出」的完整设计链。

#### 4.4.4 代码实践

**操作型实践**（与本讲「综合实践」联动）：

1. 实践目标：把 dflash 以可编辑模式装进一个干净虚拟环境，验证包发现配置生效。
2. 操作步骤：
   ```bash
   # 建议用一个独立虚拟环境（回顾 u1-l2 的隔离原则）
   uv pip install -e .
   python -c "import dflash; print(dflash.__all__)"
   ```
3. 需要观察的现象：安装成功；`import dflash` 不报错；打印出四个名字的 `__all__`。
4. 预期结果：证明 `[tool.setuptools.packages.find]` 正确识别了 `dflash` 包。若你在装的时候没带任何 `.[后端]`，`import dflash` 依然成功（因为核心依赖里没有框架），但访问 `dflash.DFlashDraftModel` 会因缺 torch 而报错——这恰好复现了 4.3.5 练习 1。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：如果把 `include = ["dflash*"]` 改成 `include = ["*"]`，会有什么潜在问题？

> 参考答案：`"*"` 会让 setuptools 把项目根目录下**所有**含 `__init__.py` 的目录都当成包来发现/打包，可能把测试目录、工具脚本目录甚至本教程目录误识别为包，污染安装产物、增大体积，甚至引发命名冲突。用 `"dflash*"` 把范围限定在 dflash 包内，是更安全的做法。

**练习 2**：为什么 `torch` 不放在核心 `dependencies` 里？

> 参考答案：因为四种后端的重依赖互不兼容（torch vs mlx 等），且体积大、安装慢、强绑定硬件。若把 torch 放进核心依赖，那么即便只想用 MLX 后端的 Apple 芯片用户，也被迫要装一份用不上的 torch。把它放进 `optional-dependencies.transformers`，由用户用 `.[transformers]` 按需安装，再配合懒加载，就实现了「装哪个用哪个、互不干扰」。

## 5. 综合实践

把本讲的「包发现 + 懒加载」串成一个小任务，作为收尾。

**任务**：在一个新建的虚拟环境里安装 dflash，并用证据回答两个问题——(a) `import dflash` 是否加载了 torch？(b) `dflash.DFlashDraftModel` 是从哪个子模块、在哪一刻被加载的？

**建议步骤**（需装好某个后端依赖；以下以 Transformers 为例）：

1. 新建虚拟环境并按 u1-l2 的隔离原则安装：
   ```bash
   uv pip install -e ".[transformers]"
   ```
2. 进入 Python，先证明「导入即轻量」：
   ```python
   import sys, dflash
   print("torch loaded on import?", "torch" in sys.modules)   # 期望 False
   print("public API:", dflash.__all__)
   ```
3. 给 `__getattr__` 套上计数器，证明「按需加载」：
   ```python
   orig = dflash.__getattr__
   calls = []
   dflash.__getattr__ = lambda n: (calls.append(n), orig(n))[1]
   _ = dflash.DFlashDraftModel
   print("getattr calls:", calls)                              # 期望 ['DFlashDraftModel']
   print("torch loaded now?", "torch" in sys.modules)          # 期望 True
   print("source module:", dflash.DFlashDraftModel.__module__) # 期望 'dflash.model'
   ```
4. 最后验证「未导出的名字会走兜底报错」：
   ```python
   try:
       dflash.build_target_layer_ids
   except AttributeError as e:
       print("AttributeError:", e)   # 期望提示 module 'dflash' has no attribute ...
   ```

**需要观察的现象与预期结果**：

- 步骤 2：导入时 torch **未**加载；`__all__` 为四个名字。
- 步骤 3：访问 `dflash.DFlashDraftModel` 触发了一次 `__getattr__`，此后 torch 才出现在 `sys.modules`；`__module__` 表明它来自 `dflash.model`。
- 步骤 4：访问未公开名字抛出 `AttributeError`。

如果你没装任何后端依赖（只做了 `uv pip install -e .`），步骤 3 的 `dflash.DFlashDraftModel` 会改为抛 `ModuleNotFoundError: No module named 'torch'`——这本身也是一个有效结论，证明了懒加载的依赖隔离效果。两种情形都属于「待本地验证」，请以你机器上的真实输出为准。

## 6. 本讲小结

- dflash 包只有 4 个文件：`__init__.py`（门面）、`model.py`（Transformers 实现）、`model_mlx.py`（MLX 实现）、`benchmark.py`（评测工具），没有更深的子包。
- 顶层公开 API 只有 4 个：`DFlashDraftModel`、`extract_context_feature`、`sample`（来自 `dflash.model`）与 `load_and_process_dataset`（来自 `dflash.benchmark`），由 `__all__` 声明。
- `__init__.py` 用模块级 `__getattr__`（PEP 562）实现懒加载：`import dflash` 不加载任何深度学习框架，只有访问具体 API 时才 `from .model / .benchmark import ...`。
- `__getattr__` 是一张分发表：`load_and_process_dataset` 走 benchmark 分支；其余三个走 model 分支；不在表里的名字一律 `raise AttributeError`。
- 懒加载与 `pyproject.toml` 的设计互相成就：核心依赖不含 torch/mlx（重依赖放在 `optional-dependencies` 四个分组里），所以「import 不碰框架」必须成立，否则裸安装即崩。
- `[tool.setuptools.packages.find]` 的 `include = ["dflash*"]` 把发现范围限定在 dflash 包，避免误打包无关目录。

## 7. 下一步学习建议

本讲建立的是「包骨架」的认知。接下来建议：

- **想立刻用起来** → 进入 u1-l4《动手跑通第一次生成》，把 README 的 Transformers / MLX 示例跑通，第一次看到 `DFlashDraftModel` 真正工作。
- **想读懂 Transformers 实现** → 进入第二单元 u2-l1《投机解码全局视图与生成控制流》，从 `dflash/model.py` 的 `dflash_generate` 切入，建立推理主链路的心智模型；后续 u2-l2~u2-l5 会逐层拆解草稿架构、块扩散注意力、验证接受循环与权重加载。
- **关心 MLX 或评测** → 第三单元（u3）专门覆盖 MLX 实现（含滑动窗口、缓存回滚、混合模型 GDN 状态捕获）与 benchmark 评测框架。

一句话提醒：本讲只解释了「包怎么导出」，没有解释「导出的这些 API 内部到底怎么加速生成」。后者正是后续讲义要回答的核心问题。
