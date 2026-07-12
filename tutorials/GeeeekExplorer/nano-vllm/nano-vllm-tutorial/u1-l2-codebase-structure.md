# 目录结构与代码入口

## 1. 本讲目标

上一篇（u1-l1）你已经把推理跑通，并从「用户视角」认识了 `LLM` 和 `SamplingParams`。但站在引擎外面，你只看到了两个名字，还不知道**里面到底分了几个文件夹、每个文件管什么、谁调用谁**。

读完本讲，你应该能够：

1. 说清楚 `nanovllm` 包的 **顶层导出** 是怎么来的，以及 `engine / layers / models / utils` 这四个子包各自的职责。
2. 理解 `LLM` 与 `LLMEngine` 的 **继承关系**，明白为什么 `LLM` 是个空类却能用 `generate`。
3. 拿到任意一个功能需求（比如「我想改调度逻辑」「我想换一个注意力内核」），能 **快速定位到对应源码文件**。
4. 画出一张从 `LLM.generate` 到模型前向的 **模块依赖关系图**，标出沿途经过的每个文件。

本讲**不深入**任何一个子包的内部实现（那是 u2～u5 的事），只建立「俯瞰全局」的代码地图。

---

## 2. 前置知识

### 2.1 Python 的「包」与 `__init__.py`

在 Python 里，一个目录如果带一个 `__init__.py` 文件，就被当作一个 **包（package）**，里面的 `.py` 文件就是 **模块（module）**。比如：

```text
nanovllm/            ← 包
├── __init__.py      ← 包的初始化文件，import 包时会执行它
├── llm.py           ← 模块 nanovllm.llm
└── engine/          ← 子包
    └── llm_engine.py← 模块 nanovllm.engine.llm_engine
```

当你在脚本里写 `from nanovllm import LLM`，Python 会先执行 `nanovllm/__init__.py`，再从这个命名空间里找 `LLM` 这个名字。所以 `__init__.py` 里 `from ... import ...` 了什么，对外就能用什么——它就是包的 **「出口清单」**。

> 小知识：Python 3.3 之后，一个目录即使**没有** `__init__.py` 也能被当作「命名空间包」导入。nano-vllm 的 `engine / layers / models / utils` 四个子目录就都没有 `__init__.py`，正是靠这个机制工作的（后面 4.1 会验证）。

### 2.2 继承与「空子类」

Python 里 `class Child(Parent): pass` 表示 `Child` 继承 `Parent`，且**不新增、不改写**任何方法。这样 `Child` 的实例能直接用 `Parent` 定义的所有方法。这种「空子类」在工程里常用来 **换个名字对外暴露**，而不改变行为——nano-vllm 的 `LLM` 就是这么做的。

### 2.3 承接 u1-l1 的两个结论

在进入目录结构前，先回顾上一篇的两个关键结论（本讲不再重复细节）：

- `from nanovllm import LLM, SamplingParams` 能用，是因为 `__init__.py` 里导出了这两个名字。
- `LLM` 是 `LLMEngine` 的空子类，真正的构造与 `generate` 逻辑都在 `LLMEngine` 里；构造参数经 `**kwargs` 过滤后交给 `Config`。

本讲要回答的下一个问题是：`LLMEngine` 又依赖了哪些模块？它们各自住在哪个文件夹？

---

## 3. 本讲源码地图

本讲从「最外层」逐步看到「子包入口」，重点是把目录结构装进脑子里。

| 文件 | 作用 | 本讲用到的地方 |
|------|------|----------------|
| [nanovllm/__init__.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/__init__.py) | 包的导出清单 | 理解对外接口 |
| [nanovllm/llm.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/llm.py) | `LLM` 空子类 | 理解继承关系 |
| [nanovllm/engine/llm_engine.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/llm_engine.py) | `LLMEngine` 真实实现 | 理解引擎依赖了谁 |
| [pyproject.toml](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/pyproject.toml) | 包元数据、依赖、打包范围 | 确认哪些目录被打包 |

> 为了画出全局地图，4.3 还会**按名字点名**其余子包里的所有文件（每个文件只给一句职责说明，不展开实现）。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：先看包的对外接口与目录全景（4.1），再看 `LLM → LLMEngine` 的继承入口（4.2），最后把四个子包的职责一次性铺开（4.3）。

### 4.1 包的对外接口与目录全景

#### 4.1.1 概念说明

一个推理引擎再复杂，对外也要给一个「最简入口」。nano-vllm 的设计哲学是：**对外只暴露两个名字，内部按职责切分到四个子包**。

- 对外（用户脚本）：只要 `from nanovllm import LLM, SamplingParams`。
- 对内（开发者）：`engine` 管调度与执行、`layers` 管可复用的神经网络层、`models` 管具体模型结构、`utils` 管跨模块的公用工具。

这种「瘦出口 + 分层内部」的组织方式，让你在读代码时能 **先选层、再选文件**，而不必在一堆平铺的文件里乱翻。

#### 4.1.2 核心流程

当用户执行 `from nanovllm import LLM` 时，Python 的解析过程：

```text
1. 在 sys.path 里找到 nanovllm/ 目录
2. 执行 nanovllm/__init__.py
   ├── from nanovllm.llm import LLM          ← 触发加载 llm.py
   │     └── llm.py 里 from nanovllm.engine.llm_engine import LLMEngine
   │           └── 触发加载 engine/llm_engine.py（连带其 import 的子包）
   └── from nanovllm.sampling_params import SamplingParams
3. 把 LLM、SamplingParams 挂到 nanovllm 命名空间
4. 用户拿到 LLM、SamplingParams 两个名字
```

也就是说，`__init__.py` 虽然只有两行，但它像一根线，**牵出了整个引擎的依赖链**。

#### 4.1.3 源码精读

整个包的「出口清单」只有两行，见 [nanovllm/__init__.py:L1-L2](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/__init__.py#L1-L2)：

```python
from nanovllm.llm import LLM
from nanovllm.sampling_params import SamplingParams
```

- 第一行把 `LLM` 类从 `nanovllm.llm` 模块搬出来；
- 第二行把 `SamplingParams` 从 `nanovllm.sampling_params` 模块搬出来。

注意：这里**没有**导出 `Config`、`Scheduler`、`ModelRunner` 等内部类——它们是「内部实现」，不对用户公开。这也是一种隐式的 **API 边界**：用户只该用 `LLM` 和 `SamplingParams`。

再来看完整的目录树（基于实际文件，子包目录下都**没有** `__init__.py`）：

```text
nanovllm/
├── __init__.py            ← 包出口：导出 LLM、SamplingParams
├── llm.py                 ← LLM 空子类（入口）
├── config.py              ← Config 引擎配置
├── sampling_params.py     ← SamplingParams 采样参数
├── engine/                ← 子包：调度与执行
│   ├── llm_engine.py
│   ├── scheduler.py
│   ├── sequence.py
│   ├── block_manager.py
│   └── model_runner.py
├── layers/                ← 子包：可复用神经网络层
│   ├── attention.py
│   ├── linear.py
│   ├── embed_head.py
│   ├── layernorm.py
│   ├── rotary_embedding.py
│   ├── activation.py
│   └── sampler.py
├── models/                ← 子包：具体模型结构
│   └── qwen3.py
└── utils/                 ← 子包：公用工具
    ├── context.py
    └── loader.py
```

打包范围在 [pyproject.toml:L25-L27](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/pyproject.toml#L25-L27) 里声明：

```toml
[tool.setuptools.packages.find]
where = ["."]
include = ["nanovllm*"]
```

`include = ["nanovllm*"]` 表示「以 `nanovllm` 开头的所有包」都会被打包，包括 `nanovllm.engine`、`nanovllm.layers` 等子包。这就是为什么 `pip install` 之后，这些子目录会一起被装进去。

#### 4.1.4 代码实践

**目标**：亲眼确认「子包没有 `__init__.py` 也能被导入」这件事，并打印出包的真实路径。

**操作步骤**（**示例代码**，可在任意能 import nanovllm 的环境里运行；若无 GPU 装好了包，则标注「待本地验证」）：

1. 在仓库根目录启动 Python：
   ```bash
   python
   ```
2. 依次执行：
   ```python
   import nanovllm
   print("包路径:", nanovllm.__file__)
   print("导出:", [n for n in dir(nanovllm) if not n.startswith("_")])
   ```
3. 在另一个终端（或用 `os.listdir`）查看子目录：
   ```python
   import os
   pkg_dir = os.path.dirname(nanovllm.__file__)
   for sub in ["engine", "layers", "models", "utils"]:
       files = os.listdir(os.path.join(pkg_dir, sub))
       print(sub, "→", sorted(files))
   ```

**需要观察的现象**：

- `nanovllm.__file__` 指向 `.../nanovllm/__init__.py`。
- `dir(nanovllm)` 里能看到 `LLM` 和 `SamplingParams`，但**看不到** `Scheduler`、`ModelRunner`。
- 四个子目录的文件列表里都**没有** `__init__.py`。

**预期结果**：`engine` 下应出现 `['block_manager.py', 'llm_engine.py', 'model_runner.py', 'scheduler.py', 'sequence.py']`（顺序可能不同）。这印证了「命名空间包」机制确实在工作。若你尚未 `pip install -e .`，import 会失败，此时本步骤「待本地验证」，但结论可由上面的目录树直接得出。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `from nanovllm import Scheduler` 会失败？
**答案**：因为 `__init__.py`（[L1-L2](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/__init__.py#L1-L2)）只导出了 `LLM` 和 `SamplingParams`，`Scheduler` 是内部实现，没有对外暴露。要拿到它得写完整路径 `from nanovllm.engine.scheduler import Scheduler`。

**练习 2**：`pyproject.toml` 里 `include = ["nanovllm*"]` 的通配符 `*` 起什么作用？
**答案**：让打包工具把所有以 `nanovllm` 开头的包（含 `engine`、`layers`、`models`、`utils` 子包）都纳入安装范围，而不需要逐个列出。

**练习 3**：子包目录没有 `__init__.py`，为什么 `from nanovllm.engine.scheduler import Scheduler` 还能成功？
**答案**：Python 3.3+ 支持隐式命名空间包，没有 `__init__.py` 的目录也能作为包被导入（见 [pyproject.toml:L25-L27](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/pyproject.toml#L25-L27) 的打包配置与此一致）。

---

### 4.2 LLM 与 LLMEngine 的继承关系

#### 4.2.1 概念说明

上一篇你已经知道 `LLM` 是个空子类。本模块要回答的是：**这个空子类的「父亲」 `LLMEngine`，自己又依赖了哪些部件？**

理解这一点，就理解了整个引擎的「骨架」：`LLMEngine.__init__` 在构造时会把 **五个协作者** 拼装起来——配置（`Config`）、分词器（`Tokenizer`）、请求序列（`Sequence`）、调度器（`Scheduler`）、执行器（`ModelRunner`）。后续所有讲义都是围绕这五个部件展开的。

> 为什么对外叫 `LLM`、对内叫 `LLMEngine`？因为 README 明确说「API mirrors vLLM's interface」——vLLM 就是这么命名的，nano-vllm 复刻了这个习惯，方便 vLLM 用户迁移。这是**命名上的刻意复刻**，不是功能上的差异。

#### 4.2.2 核心流程

从 `LLM` 到引擎骨架的组装过程：

```text
LLM(model, **kwargs)            ← 用户调用，实际走 LLMEngine.__init__
  │
  ├─ Config(model, **过滤后的 kwargs)   ← 引擎配置（含 tensor_parallel_size 等）
  ├─ Sequence.block_size = block_size  ← 把块大小写到 Sequence 类上
  ├─ for i in 1..tp_size-1:            ← 张量并行时，spawn 拉起 worker 进程
  │      Process(target=ModelRunner, ...)
  ├─ self.model_runner = ModelRunner(...)  ← 主执行器（rank 0）
  ├─ self.tokenizer = AutoTokenizer...     ← 分词器
  └─ self.scheduler = Scheduler(config)    ← 调度器
```

构造完成后，引擎就持有 `tokenizer / scheduler / model_runner` 三个核心对象。之后 `generate` 只是反复调用 `step`，而 `step` 里会让 `scheduler` 和 `model_runner` 协作（详见 4.3 与下一讲）。

#### 4.2.3 源码精读

先看「空子类」本身，[nanovllm/llm.py:L1-L5](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/llm.py#L1-L5)：

```python
from nanovllm.engine.llm_engine import LLMEngine


class LLM(LLMEngine):
    pass
```

`pass` 说明它一个字都没加。所以 `LLM` 能力 = `LLMEngine` 能力，一字不差。

再看 `LLMEngine` 顶部的导入，就能直接读出它的依赖清单，见 [nanovllm/engine/llm_engine.py:L8-L12](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/llm_engine.py#L8-L12)：

```python
from nanovllm.config import Config
from nanovllm.sampling_params import SamplingParams
from nanovllm.engine.sequence import Sequence
from nanovllm.engine.scheduler import Scheduler
from nanovllm.engine.model_runner import ModelRunner
```

读源码的一个实用技巧：**看一个模块 import 了谁，就能猜出它的依赖边界**。这五行告诉你，`LLMEngine` 直接依赖 `config / engine.sequence / engine.scheduler / engine.model_runner` 四处，加上构造里用到的 `transformers.AutoTokenizer`（[L5](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/llm_engine.py#L5)）。

最后看构造函数把哪些对象挂到 `self` 上，[nanovllm/engine/llm_engine.py:L17-L35](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/llm_engine.py#L17-L35)（关键几行）：

```python
def __init__(self, model, **kwargs):
    ...
    config = Config(model, **config_kwargs)
    Sequence.block_size = config.kvcache_block_size
    ...
    self.model_runner = ModelRunner(config, 0, self.events)
    self.tokenizer = AutoTokenizer.from_pretrained(config.model, use_fast=True)
    config.eos = self.tokenizer.eos_token_id
    self.scheduler = Scheduler(config)
```

三个 `self.` 赋值就是引擎的「三件套」：`model_runner`（执行）、`tokenizer`（编解码）、`scheduler`（调度）。**记住这三个名字**，后面所有讲义都在围绕它们转。

> 补充一个细节：[L21](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/llm_engine.py#L21) 的 `Sequence.block_size = config.kvcache_block_size` 是**给整个 Sequence 类**设置类属性（不是某个实例），这样所有 Sequence 实例都共享同一个块大小。它把「配置」和「序列」两个模块耦合了起来，这是后面 u2/u3 会反复用到的约定。

#### 4.2.4 代码实践

**目标**：用 Python 自省（introspection）验证继承关系，并确认 `LLM` 确实没有自己的方法。

**操作步骤**（**示例代码**，需能 import nanovllm，否则「待本地验证」）：

1. 启动 Python，执行：
   ```python
   from nanovllm import LLM
   from nanovllm.engine.llm_engine import LLMEngine

   print("LLM 是 LLMEngine 的子类吗?", issubclass(LLM, LLMEngine))
   print("MRO:", [c.__name__ for c in LLM.__mro__])
   print("LLM 自己的命名空间:", list(LLM.__dict__.keys()))
   print("generate 定义在:", LLM.generate.__qualname__)
   ```

**需要观察的现象**：

- `issubclass(LLM, LLMEngine)` 为 `True`。
- `LLM.__mro__`（方法解析顺序）里 `LLM` 紧接着就是 `LLMEngine`。
- `LLM.__dict__` 里几乎没有业务方法（只有 `__doc__` 之类的内置项），说明 `generate` 不是 `LLM` 自己定义的。
- `LLM.generate.__qualname__` 应显示为 `LLMEngine.generate`，证明该方法来自父类。

**预期结果**：`generate 定义在: LLMEngine.generate` 是确定的。其余若环境未就绪则「待本地验证」，但结论可由 [llm.py:L4-L5](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/llm.py#L4-L5) 的 `pass` 直接推出。

#### 4.2.5 小练习与答案

**练习 1**：如果你想给 `LLM` 增加一个 `chat()` 方法但不改 `LLMEngine`，应该改哪个文件？为什么不会影响 `LLMEngine`？
**答案**：改 [nanovllm/llm.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/llm.py)，在 `LLM` 类里加方法即可。因为继承是单向的：`LLM` 能用 `LLMEngine` 的方法，但 `LLMEngine` 不会获得 `LLM` 新增的方法，所以引擎核心不受影响。

**练习 2**：`LLMEngine.__init__` 把 `model_runner / tokenizer / scheduler` 挂到 `self` 上。这三者分别对应「执行 / 编解码 / 调度」哪个职责？
**答案**：`model_runner`→执行（在 GPU 上跑模型），`tokenizer`→编解码（文字与 token id 互转），`scheduler`→调度（决定哪些请求、哪些 token 进批）。

**练习 3**：看 [llm_engine.py:L8-L12](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/llm_engine.py#L8-L12) 的 import，`LLMEngine` 直接依赖了哪几个 nano-vllm 模块？
**答案**：`nanovllm.config`（`Config`）、`nanovllm.engine.sequence`（`Sequence`）、`nanovllm.engine.scheduler`（`Scheduler`）、`nanovllm.engine.model_runner`（`ModelRunner`），外加 `nanovllm.sampling_params`（`SamplingParams`，类型标注用）。

---

### 4.3 四个子包职责划分（全局代码地图）

#### 4.3.1 概念说明

现在把镜头拉远，看四个子包各自管什么。nano-vllm 的分层非常干净，可以一句话概括：

| 子包 | 一句话职责 | 对应人体类比 |
|------|------------|--------------|
| `engine` | 引擎主干：调度请求、管理显存、驱动 GPU 执行 | 大脑＋心脏 |
| `layers` | 可复用的神经网络层：注意力、线性层、归一化等 | 神经元零件 |
| `models` | 具体模型结构：把零件拼成一整个 Qwen3 | 整个人体骨架 |
| `utils` | 跨模块公用工具：上下文传递、权重加载 | 血液循环／物流 |

一个关键直觉：**`engine` 管「怎么调度和执行」，`layers`/`models` 管「模型长什么样」**。前者是推理引擎的「壳」，后者是「被驱动的模型内核」。这也解释了为什么 `ModelRunner`（在 `engine` 里）会去调用 `Qwen3ForCausalLM`（在 `models` 里）——执行器驱动模型。

#### 4.3.2 核心流程

四个子包在一次推理里的协作关系（高层视角，细节留待后续讲义）：

```text
用户脚本
   │  LLM.generate(prompts, sampling_params)
   ▼
engine/llm_engine.py        ← 入口：循环 step
   │
   ├──▶ engine/scheduler.py        ← 决定本步调度哪些序列、哪些 token
   │       │
   │       └──▶ engine/block_manager.py  ← 检查/分配 KV cache 块
   │
   └──▶ engine/model_runner.py     ← 把调度结果送进 GPU 执行
           │
           ├──▶ models/qwen3.py           ← 跑 Qwen3 前向
           │       │
           │       └──▶ layers/*.py        ← Attention / Linear / Norm / ...
           │
           ├──▶ utils/context.py          ← 传递注意力元数据
           └──▶ utils/loader.py           ← 加载权重（构造时）
```

这张图你不必现在全懂，但要记住 **箭头方向**：`engine` 在上、`models/layers` 在下、`utils` 横向服务。后面每篇讲义都是在展开这张图里的某一个框。

#### 4.3.3 源码精读

下面把四个子包里的**每个文件**点名一遍，给出它定义的主要类/函数和一句职责。这是你后续「按功能定位文件」的速查表。所有行号均来自当前 HEAD。

**顶层模块**

| 文件 | 主要定义 | 职责 |
|------|----------|------|
| [__init__.py:L1-L2](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/__init__.py#L1-L2) | `LLM`, `SamplingParams`（再导出） | 包出口 |
| [llm.py:L4](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/llm.py#L4) | `LLM(LLMEngine)` | 用户入口（空子类） |
| [config.py:L7](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/config.py#L7) | `Config` | 引擎配置数据类 |
| [sampling_params.py:L5](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/sampling_params.py#L5) | `SamplingParams` | 采样参数数据类 |

**engine 子包（调度与执行）**

| 文件 | 主要定义 | 职责 |
|------|----------|------|
| [engine/llm_engine.py:L15](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/llm_engine.py#L15) | `LLMEngine` | 引擎主循环（`generate`/`add_request`/`step`） |
| [engine/scheduler.py:L8](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/scheduler.py#L8) | `Scheduler` | prefill/decode 调度、抢占、后处理 |
| [engine/sequence.py:L8](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/sequence.py#L8) | `SequenceStatus`(枚举), [`Sequence`:L14](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/sequence.py#L14) | 请求状态机与 token 计数 |
| [engine/block_manager.py:L8](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/block_manager.py#L8) | `Block`, [`BlockManager`:L26](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/block_manager.py#L26) | paged KV cache 块分配、引用计数、前缀哈希 |
| [engine/model_runner.py:L15](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py#L15) | `ModelRunner` | GPU 执行、输入张量准备、权重加载、CUDA Graph |

**layers 子包（可复用神经网络层）**

| 文件 | 主要定义 | 职责 |
|------|----------|------|
| [layers/attention.py:L43](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/attention.py#L43) | `Attention`，[`store_kvcache`:L33](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/attention.py#L33) | 注意力 + Triton 写 KV cache 内核 |
| [layers/linear.py:L12](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/linear.py#L12) | `LinearBase`/`ColumnParallelLinear`/`RowParallelLinear`/`QKVParallelLinear` 等 | 张量并行线性层 |
| [layers/embed_head.py:L9](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/embed_head.py#L9) | `VocabParallelEmbedding`, [`ParallelLMHead`:L45](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/embed_head.py#L45) | 词表嵌入与 LM Head |
| [layers/layernorm.py:L5](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/layernorm.py#L5) | `RMSNorm` | RMS 归一化 |
| [layers/rotary_embedding.py:L17](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/rotary_embedding.py#L17) | `RotaryEmbedding` | 旋转位置编码 RoPE |
| [layers/activation.py:L6](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/activation.py#L6) | `SiluAndMul` | SiLU 融合激活 |
| [layers/sampler.py:L5](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/sampler.py#L5) | `Sampler` | 概率分布→token 采样 |

**models 子包（具体模型结构）**

| 文件 | 主要定义 | 职责 |
|------|----------|------|
| [models/qwen3.py:L186](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/models/qwen3.py#L186) | `Qwen3ForCausalLM`，及 [`Qwen3Attention`:L14](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/models/qwen3.py#L14)/[`Qwen3MLP`:L91](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/models/qwen3.py#L91)/[`Qwen3DecoderLayer`:L120](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/models/qwen3.py#L120)/[`Qwen3Model`:L162](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/models/qwen3.py#L162) | Qwen3 模型前向定义 |

**utils 子包（公用工具）**

| 文件 | 主要定义 | 职责 |
|------|----------|------|
| [utils/context.py:L6](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/utils/context.py#L6) | `Context`，[`get_context`:L18](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/utils/context.py#L18)/[`set_context`:L21](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/utils/context.py#L21)/[`reset_context`:L25](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/utils/context.py#L25) | 全局注意力元数据传递 |
| [utils/loader.py:L12](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/utils/loader.py#L12) | `load_model`，[`default_weight_loader`:L8](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/utils/loader.py#L8) | 从 safetensors 加载权重 |

记住这张表的使用方式：**遇到一个功能需求，先判断它属于哪个子包，再在对应行找到文件**。例如「我想改 prefill 一次最多塞多少 token」属于 `engine`（调度）→ `engine/scheduler.py`；「我想换一个激活函数」属于 `layers` → `layers/activation.py`。

#### 4.3.4 代码实践

**目标**：用「源码阅读」的方式训练定位能力——给你几个需求，不运行代码，只靠上面的速查表找到文件并验证。

**操作步骤**（**源码阅读型实践**，无需运行）：

1. 针对下面每个需求，先在心里（或纸上）写出目标文件，再用 `Read` 工具打开对应文件确认该类/函数确实存在。
   - 需求 A：「推理时每一步挑哪些序列进批」——应该读哪个文件？
   - 需求 B：「把新生成的 Key/Value 写进显存里的 KV cache」——应该读哪个文件？
   - 需求 C：「加载 safetensors 权重并装到模型上」——应该读哪个文件？
   - 需求 D：「实现旋转位置编码」——应该读哪个文件？
2. 对每个文件，找到上表里给出的类/函数定义所在行，确认行号一致。

**需要观察的现象**：

- 需求 A 命中 `engine/scheduler.py` 的 `Scheduler`（[L8](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/scheduler.py#L8)）。
- 需求 B 命中 `layers/attention.py` 的 `store_kvcache`（[L33](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/attention.py#L33)）。
- 需求 C 命中 `utils/loader.py` 的 `load_model`（[L12](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/utils/loader.py#L12)）。
- 需求 D 命中 `layers/rotary_embedding.py` 的 `RotaryEmbedding`（[L17](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/rotary_embedding.py#L17)）。

**预期结果**：四个需求都能在速查表里直接定位到唯一文件，且行号与表一致。这一步是为了让你 **形成肌肉记忆**：以后看到某个特性，第一时间知道该翻哪个文件。

#### 4.3.5 小练习与答案

**练习 1**：如果你想新增对「Llama 模型」的支持，主要应该新建/修改哪个子包下的文件？
**答案**：`models` 子包。参考 `models/qwen3.py` 的结构，新建一个 `models/llama.py`，定义类似的 `...ForCausalLM` 即可。`engine`/`layers`/`utils` 通常不需要动，因为它们是模型无关的。

**练习 2**：`ModelRunner` 在 `engine` 子包，而它要执行的 `Qwen3ForCausalLM` 在 `models` 子包。这体现了 4.3.1 里哪种关系？
**答案**：体现了「`engine` 管怎么执行，`models` 管模型长什么样」。`ModelRunner`（执行器）调用 `Qwen3ForCausalLM`（被驱动的模型内核），二者是驱动与被驱动的关系。

**练习 3**：`utils/context.py` 的 `Context` 为什么被放在 `utils` 而不是 `layers`？
**答案**：因为它是一个 **跨模块公用** 的工具——`ModelRunner`（在 `engine`）设置它，`Attention`/`ParallelLMHead`（在 `layers`）读取它，不属于任何单一子包，所以放进中性的 `utils`。

---

## 5. 综合实践

把本讲的三个模块串成一个任务：**手绘一张从 `LLM.generate` 到模型前向的模块依赖关系图**。这是本讲的核心交付物，也是后续每篇讲义的「导航图」。

**任务背景**：你现在要给一个新同事讲解 nano-vllm 的代码结构。你需要一张图，让人一眼看出「一次推理从入口走到 GPU 前向，沿途经过了哪些文件、谁调用谁」。

**操作步骤**：

1. 通读 [nanovllm/__init__.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/__init__.py)、[nanovllm/llm.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/llm.py)、[nanovllm/engine/llm_engine.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/llm_engine.py) 三个文件（一共不到 100 行）。
2. 在纸上或画图工具里，画出下面这条调用链，**每个节点标出「文件:类/方法」**，并把 4.3.3 表里的文件按「是否在链路上」分组：

   ```text
   nanovllm/__init__.py            （导出 LLM）
        │
   nanovllm/llm.py:LLM             （空子类）
        │  继承
   nanovllm/engine/llm_engine.py:LLMEngine.generate
        │  循环里调用
        ├──▶ engine/scheduler.py:Scheduler.schedule / postprocess
        │         │
        │         └──▶ engine/block_manager.py:BlockManager   （分配/检查 KV 块）
        │
        └──▶ engine/model_runner.py:ModelRunner.run → run_model
                  │  设置上下文
                  ├──▶ utils/context.py:set_context
                  │  跑模型
                  └──▶ models/qwen3.py:Qwen3ForCausalLM.forward
                            │
                            └──▶ layers/*.py（attention/linear/layernorm/...）
   ```

3. 在图上用三种颜色/标记区分：
   - **入口与主干**：`__init__.py`、`llm.py`、`engine/llm_engine.py`。
   - **engine 协作者**：`scheduler.py`、`block_manager.py`、`model_runner.py`、`sequence.py`。
   - **被驱动的模型与层**：`models/qwen3.py`、`layers/*.py`、`utils/*.py`。
4. 在 `LLMEngine.generate` 旁边标注：它内部还会先调用 `add_request`（[llm_engine.py:L43-L47](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/llm_engine.py#L43-L47)），把 prompt 编码成 `Sequence` 并交给 `Scheduler`——这也是链路的一部分。

**需要观察并记录的现象**：

- 哪些文件**在主链路上**（generate 会直接或间接调用到）？哪些**不在**（比如 `utils/loader.py` 只在构造时用一次，不在 generate 链路上）？
- `engine` 子包里，谁调用谁？`Scheduler` 和 `ModelRunner` 是并列被 `LLMEngine.step` 调用，还是有先后？
- `layers` 里的文件是被谁调用的？（答案：被 `models/qwen3.py` 调用，而不是被 `engine` 直接调用。）

**预期结果**：你能产出一张清晰的依赖图，并能口头解释「一次 `generate` 从入口到前向经过了哪些文件」。这张图不需要你运行任何代码，是纯源码阅读型产出；具体调用细节（比如 `schedule` 内部如何调度）留待 u1-l3、u2 逐步展开，本讲**只要拓扑结构正确即可**。

> 自检：如果你的图里 `engine/llm_engine.py` 出现了两次（一次在 generate、一次在 step），说明你注意到了 `generate` → `step` 的循环结构，很好。下一篇 u1-l3 正是深入这个循环。

---

## 6. 本讲小结

- nano-vllm **对外只导出 `LLM` 和 `SamplingParams`**（[__init__.py:L1-L2](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/__init__.py#L1-L2)），内部按 `engine / layers / models / utils` 四个子包分层。
- 四个子包的职责：**`engine` 管调度与执行、`layers` 管可复用层、`models` 管具体模型结构、`utils` 管跨模块工具**。
- `LLM` 是 `LLMEngine` 的**空子类**（[llm.py:L4-L5](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/llm.py#L4-L5)），理解 `LLM` 即理解 `LLMEngine`。
- `LLMEngine.__init__` 装配了引擎「三件套」：`model_runner`（执行）、`tokenizer`（编解码）、`scheduler`（调度）（[llm_engine.py:L17-L35](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/llm_engine.py#L17-L35)）。
- 子包目录**没有 `__init__.py`**，靠 Python 3 的命名空间包机制工作；打包范围由 [pyproject.toml:L25-L27](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/pyproject.toml#L25-L27) 的 `include = ["nanovllm*"]` 决定。
- 一条主链路贯穿全局：`__init__ → llm.LLM → engine.llm_engine.generate → engine.scheduler + engine.model_runner → models.qwen3 → layers.*`，`utils.context` 横向传递元数据。

---

## 7. 下一步学习建议

你已经建立了全局代码地图，接下来：

1. **下一讲 u1-l3《从 generate 到推理主循环》**：深入 [llm_engine.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/llm_engine.py) 的 `step()`，理解「调度 → 前向 → 后处理」这个循环每一步到底做了什么、prefill 与 decode 如何交替。
2. **u1-l4《配置体系：Config 与 SamplingParams》**：把 `Config` 的每个字段（`max_num_batched_tokens`、`enforce_eager`、`tensor_parallel_size` 等）的语义和默认值讲透，配合 `bench.py` 做配置对比。
3. **阅读建议**：进入 u1-l3 前，建议先通读一遍 [nanovllm/engine/llm_engine.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/llm_engine.py)（仅 90 行）和 [nanovllm/engine/scheduler.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/scheduler.py)，对照本讲的依赖图，你会发现自己已经能看懂大半。
