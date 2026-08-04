# 公共 Python API 与模块懒加载

## 1. 本讲目标

学完本讲后，你应该能够：

- 说出 `import vllm` 之后可以直接使用的核心公共对象有哪些（`LLM`、`SamplingParams`、`ModelRegistry`、`RequestOutput`、`CompletionOutput` 等）。
- 理解 vLLM 用 `MODULE_ATTRS` 字典 + 模块级 `__getattr__` 实现的「懒加载」机制，并能解释它为什么能加快启动、降低内存。
- 看懂 `vllm/outputs.py` 中两类输出数据结构（请求级 `RequestOutput`、单条完成 `CompletionOutput`）的字段与关系。
- 用 `sys.modules` 自己验证一次「访问 `vllm.LLM` 才会真正导入对应子模块」。

本讲承接 [u1-l3 仓库目录结构总览](u1-l3-repo-structure.md)：上一讲我们在目录层面知道 `vllm/` 是主包、`vllm/__init__.py` 用懒加载避免拖入全部重型依赖；本讲就钻进这个 `__init__.py`，把「公共 API 表面」拆开讲透。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**什么是模块级 `__getattr__`？**
正常情况下，`import vllm` 会执行 `vllm/__init__.py` 里的全部顶层语句，模块对象的属性也在这一刻就被确定。Python 3.7 起（PEP 562）允许在普通模块里定义一个 `__getattr__(name)` 函数：当你访问一个「模块里并不存在的属性」时，Python 不会立刻报错，而是先调用这个 `__getattr__`。这就给了我们一个机会——**把真正耗时的导入推迟到「第一次被访问」的那一刻**。

**为什么要懒加载？**
vLLM 是一个庞大的项目：在线服务、分布式通信、CUDA 内核、量化、多模态……如果 `import vllm` 就把这些全拖进来，一次简单的脚本启动都要等好几秒、吃掉大量内存，还要提前初始化 CUDA/NCCL。但很多用户只需要其中一小部分（例如只离线跑个 `LLM.generate`）。懒加载让「导入」和「使用」解耦：**用到谁才加载谁**。

**公共 API 与输出结构为什么重要？**
无论后面学在线服务（`vllm serve`）还是离线推理（`vllm.LLM`），你拿到手的结果几乎都是 `RequestOutput`，而它里面装着若干个 `CompletionOutput`。先把这两个数据结构认清，后续读任何调用链都不会在「结果长什么样」上卡壳。

> 术语提示：「懒加载（lazy import）」「PEP 562」「dataclass」「TYPE_CHECKING」会在下文反复出现，不熟悉的术语我会在第一次出现时解释。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [`vllm/__init__.py`](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/__init__.py) | 主包入口。定义 `MODULE_ATTRS` 字典、模块级 `__getattr__`、`__all__`，是懒加载的核心。 |
| [`vllm/version.py`](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/version.py) | 独立的版本库，提供 `__version__`，必须在其他模块之前导入。 |
| [`vllm/env_override.py`](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/env_override.py) | 在「任何其他模块」之前设置环境变量、给 PyTorch 打补丁；`import vllm` 时最先执行。 |
| [`vllm/outputs.py`](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/outputs.py) | 输出数据结构：`CompletionOutput`、`RequestOutput` 等。 |
| [`vllm/entrypoints/llm.py`](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/llm.py) | 离线推理入口 `LLM` 类（懒加载目标之一）。 |
| [`vllm/sampling_params.py`](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/sampling_params.py) | 采样参数 `SamplingParams`（懒加载目标之一）。 |
| [`vllm/model_executor/models/__init__.py`](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/model_executor/models/__init__.py) | 导出 `ModelRegistry`（懒加载目标之一）。 |

---

## 4. 核心概念与源码讲解

### 4.1 `import vllm` 到底执行了什么

#### 4.1.1 概念说明

很多人以为「`import vllm` = 把整个 vLLM 全部加载进内存」。这是本讲要纠正的第一个误解。事实上，`import vllm` 只做了**两件必须尽早做的事**：

1. 读取版本号（`__version__`）——很多日志、兼容性判断要用。
2. 设置环境变量、给 PyTorch 打补丁（`env_override`）——这些必须在 `import torch` 之前或之时完成，晚了就无效。

至于 `LLM`、`SamplingParams`、`ModelRegistry` 这些「公共对象」，在 `import vllm` 这一刻**并没有被导入**。它们只是被「登记」在了一张表里，等你真正去访问时才加载。下一节（4.2）专门讲这张表，本节先把「最早执行的两件事」讲清楚。

#### 4.1.2 核心流程

`import vllm` 触发 `vllm/__init__.py` 自上而下执行，关键顺序是：

1. **最先**导入版本库：`from .version import __version__`。
2. 导入环境覆盖库：`import vllm.env_override`（它会在内部 `import torch` 并设置一批环境变量）。
3. 构造 `MODULE_ATTRS` 字典（只是造一张表，**不触发任何重型导入**）。
4. 定义模块级 `__getattr__`（定义函数本身也不会触发导入）。
5. 定义 `__all__`（仅是字符串列表）。

注意：第 3、4、5 步全是「声明」性质的代码，开销极小。真正昂贵的子模块，要等到第 4.2 节的 `__getattr__` 被调用时才会进入解释器。

#### 4.1.3 源码精读

文件开头先把版本库单独最先导入，并加了 `# isort:skip` 注释，强调「顺序不能被格式化工具打乱」：

[vllm/__init__.py:5-7](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/__init__.py#L5-L7) —— 注释说明「`version.py` 必须是独立库，且永远最先导入」，这对某些定制化场景至关重要。

[vllm/__init__.py:11-14](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/__init__.py#L11-L14) —— 在任何其他模块之前导入 `vllm.env_override`，确保环境变量在其他模块导入前就已生效。

`version.py` 自身设计得非常克制——它只读取 `_version.py`（构建时生成），读不到就退化为 `"dev"`，绝不会因为取版本号而拖入重型依赖：

[vllm/version.py:4-12](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/version.py#L4-L12) —— 用 `try/except` 保护版本读取，失败时给出 `"dev"` 兜底，保证 `import vllm` 不会因为版本文件缺失而崩溃。

`env_override.py` 的职责是「趁早」：例如它会把 `PYTORCH_NVML_BASED_CUDA_CHECK=1` 等环境变量设好，避免后续 `torch.cuda.is_available()` 误触发 CUDA 初始化：

[vllm/env_override.py:94-113](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/env_override.py#L94-L113) —— 注释明确「这些设置在每次 `import vllm` 时都会执行」，是全进程生效的公共配置。

> 小结：`import vllm` = 「读版本 + 设环境」，不等于「加载全部模型/引擎代码」。这一点是理解后续懒加载的前提。

#### 4.1.4 代码实践

**实践目标**：确认 `import vllm` 之后，重型公共对象尚未被导入。

**操作步骤**（在已安装 vLLM 的环境里执行；若本地无 GPU/未安装，见下方「离线替代」）：

```python
import sys
import vllm

print(vllm.__version__)                       # 1. 能正常打印版本号
print("vllm.entrypoints.llm" in sys.modules)  # 2. 预期 False：LLM 所在模块还没加载
print("vllm.sampling_params" in sys.modules)  # 3. 预期 False
```

**需要观察的现象**：第 2、3 行都应打印 `False`——说明仅 `import vllm` 并没有把这些子模块拉进来。

**预期结果**：版本号正常输出；两个 `in sys.modules` 判断均为 `False`。

**离线替代（无法运行时）**：直接对照 [vllm/__init__.py:16-39](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/__init__.py#L16-L39) 阅读 `MODULE_ATTRS`，确认这张表里只是字符串映射、没有任何 `import` 语句，从而得出同样结论。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `version.py` 要用 `try/except` 包住版本读取，而不是直接 `from ._version import __version__`？

> **参考答案**：`_version.py` 是构建时生成的文件，开发环境或某些打包方式下可能不存在。直接导入会在文件缺失时让整个 `import vllm` 崩溃；用 `try/except` 兜底为 `"dev"`，保证主包永远能被导入。

**练习 2**：`import vllm.env_override` 为什么必须排在「任何其他模块」之前？

> **参考答案**：因为它要设置环境变量（如 `LD_LIBRARY_PATH`、`PYTORCH_NVML_BASED_CUDA_CHECK`）并给 PyTorch 打补丁。这些操作有时间窗口约束——一旦 `torch` 被其他模块导入并完成了 CUDA 初始化，再改环境变量就晚了。所以必须抢在前面。

---

### 4.2 懒加载机制：`MODULE_ATTRS` + `__getattr__`

#### 4.2.1 概念说明

这是本讲的核心模块。vLLM 的做法可以概括成一句话：**用一张「名字 → 模块:属性」的映射表 + 一个模块级 `__getattr__`，把每个公共对象的导入推迟到第一次被访问时。**

- `MODULE_ATTRS` 是一个普通字典，键是「对外暴露的名字」（如 `"LLM"`），值是一个形如 `".entrypoints.llm:LLM"` 的字符串（「哪个模块的哪个属性」）。
- `__getattr__(name)` 在访问到模块里不存在的属性时被调用：它在表里查到名字，解析出模块和属性，动态导入并返回。

这种「先登记、后加载」的设计，让 `import vllm` 又快又轻，同时保留了对静态类型检查器的友好（通过 `TYPE_CHECKING` 块，见 4.2.3）。

#### 4.2.2 核心流程

当你写下 `from vllm import LLM` 或 `vllm.LLM` 时，发生的事是：

```
访问 vllm.LLM
   │  Python 在 vllm 模块的命名空间里找不到 LLM（因为从没真正导入过）
   ▼
调用模块级 __getattr__("LLM")
   │  在 MODULE_ATTRS 中查到 ".entrypoints.llm:LLM"
   ▼
split(":")  →  module_name=".entrypoints.llm", attr_name="LLM"
   ▼
import_module(".entrypoints.llm", "vllm")   # 此刻才真正导入 vllm.entrypoints.llm
   ▼
getattr(module, "LLM")                       # 取出 LLM 类
   ▼
返回给调用者，并把结果缓存进模块命名空间（后续访问不再触发 __getattr__）
```

> 补充：`from vllm import X` 这种写法也会触发模块级 `__getattr__`（这是 PEP 562 的行为），所以两种写法的懒加载效果一致。

#### 4.2.3 源码精读

先看映射表本体——注意它的值全是字符串，构造这张表本身不会触发任何重型导入：

[vllm/__init__.py:16-39](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/__init__.py#L16-L39) —— `MODULE_ATTRS` 字典。键是公共名字，值是 `"模块路径:属性名"`。例如 `"LLM": ".entrypoints.llm:LLM"` 表示「`LLM` 这个名字来自 `vllm.entrypoints.llm` 模块里的 `LLM` 属性」。表里还包含 `SamplingParams`、`ModelRegistry`、`RequestOutput`、`CompletionOutput` 等本讲要讲的对象。

表里有几条值得特别留意：

[vllm/__init__.py:22](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/__init__.py#L22) —— `"LLM": ".entrypoints.llm:LLM"`，离线推理入口。

[vllm/__init__.py:26-27](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/__init__.py#L26-L27) —— `"ModelRegistry"` 与 `"SamplingParams"` 的映射。

[vllm/__init__.py:31,36](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/__init__.py#L31-L36) —— `CompletionOutput`、`RequestOutput` 都来自 `.outputs` 模块。

接下来是「静态类型」与「运行时」的分流。`typing.TYPE_CHECKING` 在运行时为 `False`，在 mypy/IDE 等静态分析时为 `True`：

[vllm/__init__.py:41-62](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/__init__.py#L41-L62) —— `if typing.TYPE_CHECKING:` 分支里写了完整的 `from ... import ...`。这些语句**只在静态分析时执行**，运行时完全不跑。这样既能让 IDE 补全、mypy 检查类型，又不会在运行时拖入依赖。

真正的运行时逻辑在 `else:` 分支里，即模块级 `__getattr__`：

[vllm/__init__.py:63-73](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/__init__.py#L63-L73) —— `__getattr__(name)`：若 `name` 在 `MODULE_ATTRS` 中，就按 `"模块:属性"` 拆分，用 `import_module(module_name, __package__)` 动态导入（注意第二个参数 `__package__`，它让相对路径 `.entrypoints.llm` 能正确解析为 `vllm.entrypoints.llm`），再用 `getattr(module, attr_name)` 取出对象；否则抛出标准的 `AttributeError`。

最后是 `__all__`：

[vllm/__init__.py:76-101](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/__init__.py#L76-L101) —— `__all__` 声明了 `from vllm import *` 会导出的名字。它和 `MODULE_ATTRS` 的键基本对应，是「公共 API 官方清单」。

> 设计要点：`MODULE_ATTRS`（登记）+ `__getattr__`（按需加载）+ `TYPE_CHECKING`（静态类型）三者配合，实现了「启动快、内存省、类型友好」三不误。

#### 4.2.4 代码实践

**实践目标**：亲眼看到「访问 `vllm.LLM` 之前模块未加载，访问之后模块已加载」。

**操作步骤**：

```python
import sys
import vllm

# 访问前
print("before:", "vllm.entrypoints.llm" in sys.modules)  # 预期 False

LLM = vllm.LLM   # 这一行才真正触发 import_module("vllm.entrypoints.llm")

# 访问后
print("after :", "vllm.entrypoints.llm" in sys.modules)  # 预期 True
print("再次访问:", vllm.LLM is LLM)                       # 预期 True（已缓存到模块命名空间）
```

**需要观察的现象**：第一次访问 `vllm.LLM` 时会有一个可感知的导入延迟（可能几百毫秒到数秒，取决于机器），这就是 `import_module` 在工作；第二次访问几乎瞬时，因为属性已经被写入模块命名空间、`__getattr__` 不再被调用。

**预期结果**：`before` 为 `False`，`after` 为 `True`，`再次访问` 为 `True`。若本地无法运行，可对照 [vllm/__init__.py:65-73](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/__init__.py#L65-L73) 的 `__getattr__` 逻辑推导出同样结论。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `MODULE_ATTRS` 里 `"LLM"` 这一行删掉，再执行 `vllm.LLM`，会发生什么？

> **参考答案**：`__getattr__("LLM")` 在表里查不到 `"LLM"`，走 `else` 分支，抛出 `AttributeError: module vllm has no attribute LLM`。这证明了公共 API 完全由这张表驱动。

**练习 2**：`__getattr__` 里为什么要传第二个参数 `__package__` 给 `import_module`？

> **参考答案**：因为表里存的是**相对路径**（如 `.entrypoints.llm`）。`import_module` 只有在知道「相对于哪个包」时，才能把相对路径解析成绝对路径 `vllm.entrypoints.llm`。`__package__` 在 `vllm/__init__.py` 中正是 `"vllm"`。

---

### 4.3 三大公共对象：`LLM` / `SamplingParams` / `ModelRegistry`

#### 4.3.1 概念说明

`MODULE_ATTRS` 表里登记了很多名字，但对初学者来说，最先打交道的通常是这三个：

- **`LLM`**：离线推理的入口类。你给它一个模型名和若干 prompt，它返回生成结果。它是「同步、进程内」使用 vLLM 最简单的方式（在线服务 `vllm serve` 在 u2-l2 讲）。
- **`SamplingParams`**：描述「怎么采样」的参数对象（温度、top_p、最大 token 数等）。它和 `LLM` 是配合使用的——`LLM.generate(prompts, sampling_params)`。
- **`ModelRegistry`**：模型注册表。vLLM 支持海量 HuggingFace 模型，靠的就是它把「HF 架构名」映射到「vLLM 内部的模型实现类」。

这三个对象本身都很大，各自有专门的讲义（`LLM`/`SamplingParams` 在第二单元，`ModelRegistry` 在 u6-l1）。本节只交代「它们是谁、在哪里、怎么被懒加载进来」，建立认知坐标，不深入细节。

#### 4.3.2 核心流程

三者的「被发现」路径完全一样，都走 4.2 节的懒加载：

```
vllm.LLM           →  __getattr__("LLM")           →  导入 vllm.entrypoints.llm
vllm.SamplingParams →  __getattr__("SamplingParams") →  导入 vllm.sampling_params
vllm.ModelRegistry  →  __getattr__("ModelRegistry")  →  导入 vllm.model_executor.models
```

被导入后，它们各自的真实定义位置是：

- `LLM` 定义在 `vllm/entrypoints/llm.py`。
- `SamplingParams` 定义在 `vllm/sampling_params.py`。
- `ModelRegistry` 在 `vllm/model_executor/models/__init__.py` 里从 `.registry` 子模块转出。

#### 4.3.3 源码精读

先看 `LLM` 类的定义位置与它的核心方法 `generate`：

[vllm/entrypoints/llm.py:67-70](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/llm.py#L67-L70) —— `class LLM(...)`，文档说明它「根据给定的 prompt 和采样参数生成文本，内部包含 tokenizer、（可能是分布式的）语言模型……」。

[vllm/entrypoints/llm.py:414-420](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/llm.py#L414-L420) —— `generate` 方法签名：接收 `prompts` 和 `sampling_params`，返回值是 `RequestOutput` 列表（见 4.4 节）。这里能看到 `LLM` 与 `SamplingParams` 的协作关系。

再看 `SamplingParams` 的几个常用字段（完整讲解留给 u2-l4）：

[vllm/sampling_params.py:199](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/sampling_params.py#L199) —— `class SamplingParams(...)` 定义。

[vllm/sampling_params.py:213-262](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/sampling_params.py#L213-L262) —— 几个关键字段：`n`（生成几条）、`temperature`（温度）、`top_p`（核采样）、`top_k`（top-k 采样）、`max_tokens`（最多生成多少 token）。这些字段的默认值（如 `temperature=1.0`、`top_p=1.0`）就定义在这里。

最后看 `ModelRegistry` 是怎么被转出的：

[vllm/model_executor/models/__init__.py:24](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/model_executor/models/__init__.py#L24) —— `from .registry import ModelRegistry`。`MODEL_ATTRS` 里登记的 `".model_executor.models:ModelRegistry"` 正是指向这里。它把「HF 架构名 → vLLM 实现类」的映射集中管理（深入机制见 u6-l1）。

#### 4.3.4 代码实践

**实践目标**：用一条调用链把三个对象串起来，直观感受它们的协作（**离线阅读型实践**，不需要真的跑模型）。

**操作步骤**：

1. 打开 [vllm/entrypoints/llm.py:414-420](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/entrypoints/llm.py#L414-L420)，确认 `generate` 的第二个参数类型是 `SamplingParams`。
2. 打开 [vllm/sampling_params.py:213-262](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/sampling_params.py#L213-L262)，记录 5 个字段的默认值。
3. 在 `vllm/__init__.py` 的 `MODULE_ATTRS` 里找到这三个名字，确认它们的 `"模块:属性"` 字符串与上面读到的文件一致。

**需要观察的现象**：你会看到 `LLM`（用谁生成）依赖 `SamplingParams`（怎么生成），而 `LLM` 内部构造模型时又依赖 `ModelRegistry`（生成用哪个模型实现）——三者构成「用什么、怎么用、用什么模型」的三角。

**预期结果**：能用自己的话画出 `LLM ↔ SamplingParams ↔ ModelRegistry` 的关系图。

**如果本地可运行**（可选）：用一个极小模型快速感受（真实首次推理见 u2-l1，本讲不强求）：

```python
# 示例代码（非项目原有，仅示意三者协作；需本地具备 GPU 与模型权重）
from vllm import LLM, SamplingParams

llm = LLM(model="facebook/opt-125m")
sp = SamplingParams(temperature=0.7, max_tokens=16)
print(llm.generate(["你好，vLLM"], sp))
```

#### 4.3.5 小练习与答案

**练习 1**：`vllm.SamplingParams` 和 `vllm.LLM` 都没有在 `__init__.py` 顶部 `import`，那 IDE 是怎么知道它们存在的、还能自动补全？

> **参考答案**：靠 [vllm/__init__.py:41-62](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/__init__.py#L41-L62) 的 `if typing.TYPE_CHECKING:` 分支。它在静态分析时执行 `import`，为 IDE/mypy 提供类型信息，但运行时不执行，所以不影响懒加载。

**练习 2**：`ModelRegistry` 登记的值是 `.model_executor.models:ModelRegistry`，但 `ModelRegistry` 的真正定义并不在 `models/__init__.py` 本体，这矛盾吗？

> **参考答案**：不矛盾。`models/__init__.py` 里有一行 `from .registry import ModelRegistry`（见上面精读），所以访问 `vllm.model_executor.models.ModelRegistry` 时拿到的就是 `registry` 子模块里的 `ModelRegistry`。`__getattr__` 只要导入 `models` 包，`__init__.py` 自然会把这个名字带出来。

---

### 4.4 输出数据结构：`RequestOutput` 与 `CompletionOutput`

#### 4.4.1 概念说明

无论走离线 `LLM.generate` 还是后续的在线流式接口，你拿到的结果几乎都是 `RequestOutput`。它代表「一次请求的输出」，内部装着一个列表 `outputs`，列表里每个元素是一个 `CompletionOutput`，代表「这次请求生成的第几条候选」。

为什么要分两层？因为 vLLM 支持一次请求生成多条候选（`SamplingParams.n > 1`）：

- **`RequestOutput`**：请求级。承载请求 id、原始 prompt、prompt token id、是否完成，以及「若干条候选」。
- **`CompletionOutput`**：候选级。承载这一条候选的文本、token id、累计对数概率、结束原因等。

此外，`outputs.py` 里还有面向 embedding / classification / scoring 等任务的同构输出类（`PoolingRequestOutput` 等），结构思路一致，本讲聚焦生成场景的两个核心类。

#### 4.4.2 核心流程

一次生成请求返回时，数据是这样组织的：

```
RequestOutput
├── request_id            (这次请求的唯一 id)
├── prompt / prompt_token_ids   (原始输入)
├── finished               (整条请求是否结束)
└── outputs: list[CompletionOutput]
        ├── CompletionOutput(index=0, text="第一条候选", token_ids=[...], finish_reason="stop")
        ├── CompletionOutput(index=1, text="第二条候选", ...)   # 当 n>1 时
        └── ...
```

判断「这条候选是否结束」用 `CompletionOutput.finished()`，它只看 `finish_reason is not None`；判断「整条请求是否结束」用 `RequestOutput.finished`。

> 流式提示：在线流式场景下，每个 step 会产出一个「增量」`RequestOutput`，`RequestOutput.add()` 负责把后续增量合并进来（累加文本、token id 等）。

#### 4.4.3 源码精读

先看候选级 `CompletionOutput`——它是一个 `@dataclass`，字段集中、含义清晰：

[vllm/outputs.py:21-48](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/outputs.py#L21-L48) —— `CompletionOutput` 数据类。关键字段：`index`（候选序号）、`text`（生成文本）、`token_ids`（生成的 token id 序列）、`cumulative_logprob`（累计对数概率）、`logprobs`（若请求了则返回每步 top 词的对数概率）、`finish_reason`（结束原因，如 `"stop"`/`"length"`）、`stop_reason`（具体由哪个 stop 串/token 触发）。

> 名词解释：`@dataclass` 是 Python 标准库提供的装饰器，能自动生成 `__init__`、`__repr__` 等方法，适合「字段集合」式的数据类。

判断结束的辅助方法很简单：

[vllm/outputs.py:50-51](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/outputs.py#L50-L51) —— `finished()` 直接返回 `self.finish_reason is not None`。即只要设置了结束原因，就视为这条候选已结束。

再看请求级 `RequestOutput`。它没有用 `@dataclass`，而是手写 `__init__`，原因是它要做「向前兼容」处理（吸收未来版本可能新增的参数）：

[vllm/outputs.py:85-110](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/outputs.py#L85-L110) —— `RequestOutput` 的文档字符串，列出全部字段含义：`request_id`、`prompt`、`prompt_token_ids`、`prompt_logprobs`、`outputs`（即 `list[CompletionOutput]`）、`finished`，以及 metrics、lora、encoder、缓存命中等附加信息。

[vllm/outputs.py:112-150](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/outputs.py#L112-L150) —— `__init__` 实现。注意末尾的 `**kwargs` 与开头的告警：当传入了未知参数时，会 `logger.warning_once` 提示「忽略多余参数」。这是**向前兼容**设计——新版本代码即使在旧版本 vLLM 上跑也不会因多传字段而崩溃。

合并增量的方法体现了流式聚合逻辑：

[vllm/outputs.py:152-181](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/outputs.py#L152-L181) —— `add(next_output, aggregate)`：按 `index` 匹配候选。若 `aggregate=True`，就把新文本拼到旧文本后面、token id 与 logprobs 也往后追加；若 `aggregate=False`，则直接替换。这是流式输出把每步增量拼接成完整结果的关键。

最后是一个常量哨兵，用于标记「流式输入已结束」：

[vllm/outputs.py:200-208](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/outputs.py#L200-L208) —— `STREAM_FINISHED`：一个 `finished=True`、空 outputs 的 `RequestOutput` 实例，作为「请求完成」的统一信号。

#### 4.4.4 代码实践

**实践目标**：根据源码，手写一个 `RequestOutput`/`CompletionOutput` 的关系图，并能解释每个字段含义（**源码阅读型实践**）。

**操作步骤**：

1. 打开 [vllm/outputs.py:21-48](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/outputs.py#L21-L48)，把 `CompletionOutput` 的每个字段抄下来。
2. 打开 [vllm/outputs.py:85-150](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/outputs.py#L85-L150)，对照 `RequestOutput.__init__`，确认 `outputs` 字段的类型是 `list[CompletionOutput]`。
3. 自己画一张树状图（参考 4.4.2 的结构）。

**需要观察的现象**：你会发现 `RequestOutput` 的 `outputs` 是一个列表——这正是「一次请求、多条候选」的体现；而每条候选是否结束由 `CompletionOutput.finished()` 判断。

**预期结果**：能不看源码说出「请求输出包含若干候选输出，候选输出里有 text/token_ids/finish_reason」。

**可选验证**（本地可运行时）：实际跑一次 `llm.generate(..., SamplingParams(n=2))`，打印返回的 `RequestOutput`，观察 `len(request_output.outputs) == 2`，且每条 `outputs[i].index == i`。

#### 4.4.5 小练习与答案

**练习 1**：`CompletionOutput.finished()` 为什么只判断 `finish_reason is not None`，而不是判断 `text` 是否为空？

> **参考答案**：模型完全可能生成出空文本却仍已结束（例如第一步就预测出 EOS），也可能生成了文本但还没结束。结束与否只与「是否触发了停止条件」有关，`finish_reason` 正是这一信息的载体，而 `text` 长度与结束状态没有必然关系。

**练习 2**：`RequestOutput.__init__` 为什么要保留 `**kwargs` 并对未知参数告警，而不是直接拒绝？

> **参考答案**：为了**向前兼容**。新版本 vLLM 可能在 `RequestOutput` 里新增字段；如果旧版本代码（如用户自己写的后处理脚本）把这些新字段传给旧版 vLLM 的 `RequestOutput`，硬拒绝会直接报错。保留 `**kwargs` + 告警，能让旧版本「忽略但不崩溃」，代码注释里也写明了这一点。

---

## 5. 综合实践

设计一个把本讲四个模块串起来的小任务：**给 vLLM 的公共 API 画一张「懒加载档案」**。

任务要求：

1. **列清单**：从 [vllm/__init__.py:16-39](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/__init__.py#L16-L39) 的 `MODULE_ATTRS` 中，挑出本讲涉及的 5 个对象（`LLM`、`SamplingParams`、`ModelRegistry`、`RequestOutput`、`CompletionOutput`），制成一张表：`公共名字 | 来源模块 | 来源属性 | 真实定义文件`。
2. **画流程**：画出「`import vllm` → 访问 `vllm.LLM` → 返回 `LLM` 类」的完整调用链，标出 `__getattr__`、`MODULE_ATTRS`、`import_module`、`getattr` 四个环节。
3. **做验证**：若本地已装 vLLM，运行 4.1.4 与 4.2.4 的两段脚本，截下 `before/after` 的 `sys.modules` 变化；若无法运行，则用 4.2.3 的源码逻辑文字推导出「访问前未加载、访问后已加载」。
4. **理输出**：根据 [vllm/outputs.py:21-48](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/outputs.py#L21-L48) 与 [vllm/outputs.py:85-150](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/outputs.py#L85-L150)，写一段话说明：为什么 `RequestOutput.outputs` 是列表，以及它与 `SamplingParams.n` 的关系。

完成后，你应当能用一句话回答：**「`import vllm` 为什么又快又省？」**——因为公共对象都登记在 `MODULE_ATTRS` 里，靠模块级 `__getattr__` 在首次访问时才按需导入。

## 6. 本讲小结

- `import vllm` 只做两件必做之事：读取 `__version__`、执行 `env_override` 设置环境变量与 PyTorch 补丁；它**不会**立刻加载 `LLM` 等重型对象。
- 懒加载由 `MODULE_ATTRS`（名字 → `"模块:属性"` 字符串的映射表）+ 模块级 `__getattr__`（查表 → `import_module` → `getattr`）配合实现。
- `if typing.TYPE_CHECKING:` 分支为 IDE/mypy 提供类型信息但不参与运行，做到「静态类型 + 运行时懒加载」两全。
- 三大公共对象：`LLM`（离线推理入口）、`SamplingParams`（采样参数）、`ModelRegistry`（HF 架构名 → vLLM 实现类的注册表）。
- 输出结构分两层：请求级 `RequestOutput`（含 `outputs: list[CompletionOutput]`、`finished`、向前兼容的 `**kwargs`）与候选级 `CompletionOutput`（`text`/`token_ids`/`finish_reason`）。
- 可以用 `sys.modules` 直接验证「访问 `vllm.LLM` 前后，对应子模块是否被加载」。

## 7. 下一步学习建议

- 想真正跑一次推理？进入 **u2-l1 离线推理：LLM 类与 generate/chat**，亲手调用 `LLM.generate`，并把返回的 `RequestOutput` / `CompletionOutput` 与本讲学到的字段一一对照。
- 想了解服务端？看 **u2-l2 vllm CLI 与 serve 启动在线服务**，理解 `vllm serve` 如何在不直接用 `LLM` 类的情况下对外提供服务。
- 对采样参数的每个字段感兴趣？进入 **u2-l4 SamplingParams 采样参数入门**，深入 `temperature`/`top_p`/`top_k` 的作用。
- 想了解模型是如何被「找到」的？记下 `ModelRegistry` 这个名字，等到 **u6-l1 模型注册机制** 再深入它背后的 `_ModelRegistry` 与懒注册设计。
- 建议同时通读一遍 [vllm/__init__.py](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/__init__.py) 全文（仅 100 余行），它是后续所有讲义都会反复回到的「入口」。
