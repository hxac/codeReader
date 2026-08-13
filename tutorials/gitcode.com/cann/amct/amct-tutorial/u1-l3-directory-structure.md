# 仓库目录结构与代码组织

## 1. 本讲目标

学完本讲，你应当能够：

- 看懂 AMCT 仓库的顶层目录划分，说出 `amct_pytorch` / `amct_ops` / `examples` / `tests` / `docs` 各自的职责。
- 理解 `amct_pytorch` 内部 `algorithms` / `cli` / `common` / `configs` / `quantization` / `workflows` 等二级目录的分层逻辑。
- 区分仓库里同时存在的两条主线：**LLM 训练后量化（PTQ）主流程** 与 **classic 经典图压缩流程**。
- 读懂 `amct_pytorch/__init__.py` 的包导出与「懒加载」机制，理解为什么 `import amct_pytorch` 在只跑 LLM 量化时不需要 onnx 等重依赖。
- 自己动手产出一份数据准确的「目录速查表」，作为后续阅读源码的地图。

## 2. 前置知识

本讲承接 [u1-l1](u1-l1-project-overview.md) 建立的全局认知。回顾几个关键点：

- **AMCT** 是昇腾 NPU 原生的模型量化压缩工具包，部署链路是三段式：浮点模型 → AMCT 量化 → 昇腾 NPU 低比特推理。
- 仓库代码分两大块：`amct_pytorch`（核心量化源码）与 `amct_ops`（独立 NPU 自定义算子层，Ascend C kernel 实现），二者职责分离。
- 关键术语：**量化**（把高比特浮点权重/激活压成低比特表示）、**NPU/昇腾**（华为 AI 处理器）、**PTQ**（训练后量化）、**LLM**（大语言模型）。

本讲要回答的核心问题是：**这套能力在仓库里是如何按目录组织的？我该去哪里找东西？** 我们不读具体算法实现，只建立「代码地图」。

> 小贴士：读源码的第一步不是钻进某个函数，而是先看目录。目录名往往就是设计者给你的「功能分类标签」。

## 3. 本讲源码地图

| 文件 / 目录 | 作用 |
|------|------|
| [README.md](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/README.md) | 项目门面，含一版「精简目录结构图」 |
| [AGENTS.md](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/AGENTS.md) | 给 agent 的工作指南，含一版更贴近开发的「目录用途表」 |
| [amct_pytorch/__init__.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/__init__.py) | 包入口，定义对外导出的符号与懒加载逻辑 |
| [amct_pytorch/classic/__init__.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/classic/__init__.py) | classic 经典流程的子包入口 |

---

## 4. 核心概念与源码讲解

### 4.1 顶层目录全景：从 README 目录结构图读起

#### 4.1.1 概念说明

一个开源仓库的「目录结构图」通常是维护者精心挑选过的**精简视图**：它会列出最重要的几个目录，但不会把每个角落都展示出来。AMCT 的 README 里就有这样一张图，它是最快建立全局认知的入口。

但要注意：**README 的目录图 ≠ 仓库的全部内容**。它是教学性质的「主干」，真实仓库里还有不少 README 没画出来的目录和文件。本模块教你「先看 README 建立主干认知，再用 `ls` 补全细节」。

#### 4.1.2 核心流程

阅读一个陌生仓库目录的建议步骤：

1. 先读 README 的目录结构图，建立**主干心智模型**（哪些是源码、哪些是文档、哪些是构建脚本）。
2. 再读 AGENTS.md / CONTRIBUTING.md 这类工程文档，它们常给更贴近开发的目录说明。
3. 用 `ls -1` 或 `tree -L 2` 在本地列出真实目录，**对照** README 找出差异。
4. 对差异项（README 没列的目录）单独查证用途，补全自己的地图。

#### 4.1.3 源码精读

README 的「目录结构」小节给出了 AMCT 的主干目录图：

[README.md:129-149](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/README.md#L129-L149) —— 这是项目对外展示的精简目录树，重点标出了 `amct_pytorch` 下的 7 个二级目录（`algorithms`/`cli`/`common`/`configs`/`experimental`/`quantization`/`workflows`），以及 `amct_ops`/`examples`/`tests`/`docs` 等顶层目录与三个构建相关文件（`build.sh`/`setup.py`/`requirements.txt`）。

AGENTS.md 则给了一张更偏「工程实操」的目录用途表：

[AGENTS.md:97-112](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/AGENTS.md#L97-L112) —— 这张表比 README 多点出几个「开发时才关心」的目录，例如 `amct_pytorch/experimental/`（试验特性）、`amct_pytorch/classic/graph_based/`（基于计算图的压缩优化）、`cmake/`（CMake 构建配置）、`.clang-format`（C/C++ 代码格式化配置）。

把两份说明对照来看，可以把顶层目录职责归纳为下表：

| 顶层目录 / 文件 | 职责 | 出处 |
|------|------|------|
| `amct_pytorch/` | PyTorch 量化压缩核心源码（本手册的绝对主角） | README + AGENTS |
| `amct_ops/` | AMCT 自定义 NPU 算子（Ascend C kernel） | README + AGENTS |
| `examples/` | 端到端样例与调用示例（脚本 + 模型 walkthrough） | README + AGENTS |
| `tests/` | 单元测试 | README + AGENTS |
| `docs/` | 工具文档（概念、API、算法介绍等） | README + AGENTS |
| `cmake/` | CMake 构建配置 | AGENTS |
| `build.sh` | 工程编译脚本 | README + AGENTS |
| `setup.py` | Python 包打包入口 | README + AGENTS |
| `requirements.txt` | Python 第三方依赖 | README + AGENTS |

> 注意：真实仓库根目录下还有 README 没列出的东西，例如 `scripts/`、`install_graph.sh`、`pyproject.toml`、`version.info`、`README_en.md` 等。这正说明了「README 是精简视图」。

#### 4.1.4 代码实践

**实践目标**：亲手验证「README 目录图是精简视图」，找出 README 没列出的顶层条目。

**操作步骤**：

1. 在仓库根目录执行 `ls -1`，列出所有顶层文件和目录。
2. 把输出与 [README.md:131-149](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/README.md#L131-L149) 的目录图逐项比对。
3. 标记出两类差异：① README 没画、但仓库里存在的条目；② README 画了、你一开始没注意的条目。

**需要观察的现象**：README 的目录图只画了约 10 个顶层条目，而 `ls -1` 会列出 20+ 项。

**预期结果**：你会至少发现这些 README 未列出的顶层条目：`scripts/`、`install_graph.sh`、`pyproject.toml`、`version.info`、`README_en.md`、`CMakeLists.txt`、`OAT.xml`、`classify_rule.yaml`、`CONTRIBUTING.md` 等。

> 待本地验证：如果你所在的仓库检出与本文写作时（HEAD `ba53a0f`）不同，差异清单可能略有变化，以本地 `ls -1` 的实际输出为准。

#### 4.1.5 小练习与答案

**练习 1**：README 目录图把 `amct_pytorch` 拆成了哪几个二级目录？分别一句话说明用途。

**参考答案**：共 7 个——`algorithms`（量化算法实现）、`cli`（命令行入口）、`common`（通用工具、模型和数据处理）、`configs`（量化配置模板）、`experimental`（实验特性）、`quantization`（量化数据类型与基础模块）、`workflows`（LLM 量化、评估和部署流程）。

**练习 2**：AGENTS.md 的目录用途表比 README 多提到了哪几个 `amct_pytorch` 子目录？

**参考答案**：多提到了 `amct_pytorch/experimental/`（试验特性）和 `amct_pytorch/classic/graph_based/`（基于计算图的压缩优化）。这说明 AGENTS.md 更贴近开发视角。

---

### 4.2 两条主线：LLM PTQ 主流程 vs Classic 经典图压缩

#### 4.2.1 概念说明

这是本讲最重要、也是初学者最容易困惑的一点：**`amct_pytorch` 里同时住着两套量化体系**。

- **LLM PTQ 主流程**：面向大语言模型的训练后量化，按「评估 → 提取校准数据 → PTQ 求解 → 部署导出」四阶段运行（对应 `eval` / `extract_ptq_data` / `ptq` / `deploy` 四条命令）。这是 AMCT 当前主推、也是本手册第 3~7 单元要深挖的主线。
- **Classic 经典图压缩流程**：更早期的、基于计算图的量化压缩能力（含张量分解、通道剪枝、图模式算子替换等），通过 `quantize` / `convert` / `algorithm_register` 这类函数式 API 暴露。

两者**不是替代关系**，而是并存：LLM 场景走新主流程，传统 CV/小模型图压缩走 classic。弄清「我现在看的目录属于哪条线」，才不会在读源码时迷路。

#### 4.2.2 核心流程

判断一个目录属于哪条主线的快速判据：

```text
if 目录 in {workflows, cli/llm, common/models, algorithms/quant, quantization, configs}:
    → LLM PTQ 主流程（新）
elif 目录 in {classic/quantize_op, classic/deploy_op, classic/optimizer, classic/graph_based}:
    → Classic 经典图压缩（旧）
elif 目录是 amct_pytorch 根下的 ptq.py / eval.py / extract_ptq_data.py / deploy.py:
    → LLM PTQ 主流程的「顶层 -m 入口」（供 python -m amct_pytorch.ptq 调用）
```

#### 4.2.3 源码精读

**① LLM PTQ 主流程的顶层入口文件**

在 `amct_pytorch/` 根目录下（README 目录图没画出），有四个对应四阶段的顶层模块入口：

- `amct_pytorch/eval.py` —— 评估阶段入口
- `amct_pytorch/extract_ptq_data.py` —— 提取 PTQ 校准数据入口
- `amct_pytorch/ptq.py` —— PTQ 训练后量化入口
- `amct_pytorch/deploy.py` —— 部署导出入口

它们支持 `python -m amct_pytorch.<cmd>` 的调用方式，是 LLM 量化的总入口（[u1-l4](u1-l4-first-quant-cli.md) 会展开）。

**② LLM PTQ 的编排目录 `workflows/`**

`amct_pytorch/workflows/` 下恰好有四个 Workflow 文件，与上面四个入口一一对应：

```text
workflows/
├── llm_eval.py
├── llm_extract_ptq_data.py
├── llm_ptq.py
└── llm_deploy.py
```

这是 LLM PTQ 主流程的「编排骨架」。

**③ Classic 经典流程的子包 `classic/`**

[amct_pytorch/classic/__init__.py:17-18](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/classic/__init__.py#L17-L18) —— classic 子包对外只导出三个符号：`quantize`、`convert`、`algorithm_register`，这正是 classic 经典流程的函数式 API。

`classic/` 内部进一步分为：

| classic 二级目录 | 职责 |
|------|------|
| `classic/quantize_op/` | 训练态「伪量化」模块（如 LinearAWQuant） |
| `classic/deploy_op/` | NPU 部署态算子模块（成对配套） |
| `classic/optimizer/` | 图优化 pass（量化算子插入 / 替换为 NPU 算子） |
| `classic/graph_based/` | 基于计算图的压缩（张量分解、通道剪枝等） |

> 记忆诀窍：**classic 里 `quantize_op` 和 `deploy_op` 是成对的**——前者用于训练时的伪量化，后者用于部署时的真实 NPU 算子，一个算法往往两个目录各有一个对应模块。

**④ 一个关键佐证：`__init__.py` 的导入也能看出两条线**

[amct_pytorch/__init__.py:35-48](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/__init__.py#L35-L48) —— 包入口**立刻（eager）**导入的是 `amct_pytorch.classic`（quantize/convert/algorithm_register）和 `amct_pytorch.common.config`（一系列 `*_CFG` 量化配置常量）。也就是说，`import amct_pytorch` 默认带进来的「顶层 API」其实是 **classic 经典流程** 的接口与配置，而不是 LLM PTQ 的 Workflow 类。这从侧面印证了两条线的并存。

#### 4.2.4 代码实践

**实践目标**：在本地仓库里把两条主线各自的「入口」位置找出来，加深物理路径印象。

**操作步骤**：

1. 执行 `ls -1 amct_pytorch/*.py`，列出根目录下的 Python 文件，找出 LLM PTQ 的四个 `-m` 入口。
2. 执行 `ls -1 amct_pytorch/classic/`，确认 classic 子包下的四个二级目录。
3. 执行 `ls -1 amct_pytorch/workflows/`，确认 LLM PTQ 的四个 Workflow 文件。

**需要观察的现象**：四条 LLM PTQ 命令各有「一个根入口 `.py` + 一个 `workflows/llm_*.py`」；classic 则集中在 `classic/` 子包内。

**预期结果**：

- 根入口：`eval.py` / `extract_ptq_data.py` / `ptq.py` / `deploy.py`。
- workflows：`llm_eval.py` / `llm_extract_ptq_data.py` / `llm_ptq.py` / `llm_deploy.py`。
- classic：`deploy_op` / `graph_based` / `optimizer` / `quantize_op` 四个目录 + `quantize.py` / `__init__.py`。

#### 4.2.5 小练习与答案

**练习 1**：假设你要给一个大语言模型做训练后量化，应该看 `amct_pytorch/classic/` 还是 `amct_pytorch/workflows/`？为什么？

**参考答案**：看 `amct_pytorch/workflows/`（以及根目录的 `ptq.py` 等入口）。因为大模型 PTQ 走的是新的 LLM PTQ 主流程，`classic/` 是早期基于计算图的经典压缩流程，二者并存但定位不同。

**练习 2**：classic 子包的 `quantize_op` 与 `deploy_op` 为什么是「成对」出现的？

**参考答案**：`quantize_op` 提供训练/校准时的「伪量化」模块（前向模拟量化误差），`deploy_op` 提供真正跑在昇腾 NPU 上的部署算子。一个算法需要两种形态，所以成对配套。

---

### 4.3 amct_pytorch 包的导出与懒加载机制

#### 4.3.1 概念说明

`amct_pytorch/__init__.py` 是整个包的「门面」。它决定了 `import amct_pytorch` 时会发生什么、哪些符号能被 `amct_pytorch.xxx` 直接访问。

这里有一个值得学习的设计：**懒加载（lazy import）**。它的动机是——classic 的「图压缩」能力依赖 onnx 和编译过的 protobuf，是相当重的依赖；但很多用户（特别是只跑 LLM 量化的用户）根本用不到图压缩，却被迫在 `import amct_pytorch` 时就加载这些重依赖，既慢又容易因缺依赖而报错。

AMCT 的解法是：**轻量符号立刻导入，重量符号按需懒加载**。这样 LLM-only 的用户 `import amct_pytorch` 时完全不会碰 onnx/protobuf。

#### 4.3.2 核心流程

`import amct_pytorch` 时的执行流程：

```text
1. 解释器执行 __init__.py
2. 定义 __all__（声明对外公开的符号清单）        ← 仅声明，不触发导入
3. 立刻(eager) import：
   - amct_pytorch.classic → quantize / convert / algorithm_register
   - amct_pytorch.common.config → 一组 *_CFG 配置常量
4. 注册模块级 __getattr__(name)（PEP 562 机制）
   —— 此刻还不导入 graph_based

# 之后用户访问 amct_pytorch.create_quant_config（一个 graph_based 符号）时：
5. 命中 __getattr__("create_quant_config")
6. importlib.import_module("amct_pytorch.classic.graph_based.amct_pytorch")  ← 此刻才加载 onnx/protobuf
7. getattr(_graph, name) 取出符号
8. globals()[name] = value  ← 缓存，后续访问不再走 __getattr__
```

第 8 步的缓存很关键：PEP 562 的 `__getattr__` 本身不做缓存，所以代码手动把解析到的符号写回模块全局命名空间，保证第二次访问直接命中、不再重复导入。

#### 4.3.3 源码精读

**① `__all__`：对外公开的符号清单**

[amct_pytorch/__init__.py:18-33](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/__init__.py#L18-L33) —— 列出了 `quantize` / `convert` / `algorithm_register` 三个 classic 函数式 API，以及 `INT4_AWQ_WEIGHT_QUANT_CFG` / `INT8_SMOOTHQUANT_CFG` / `HIFP8_CAST_CFG` 等一组「开箱即用」的量化配置常量。这些就是 `from amct_pytorch import *` 能拿到的全部公开符号。

**② eager 导入：classic 接口与配置常量**

[amct_pytorch/__init__.py:35-48](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/__init__.py#L35-L48) —— 这两个 `from ... import ...` 在 `import amct_pytorch` 时**立即执行**，分别从 `classic` 和 `common.config` 把符号搬进包命名空间。注意这里**没有**导入 `graph_based`。

**③ 懒加载：`__getattr__` 与设计说明**

[amct_pytorch/__init__.py:51-55](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/__init__.py#L51-L55) —— 这段注释直接讲明了设计意图（原文意译）：

> classic 的图压缩接口（`create_quant_config` 等）位于 `graph_based` 子包，会拉入 onnx 和编译过的 protobuf。用 PEP 562 的 `__getattr__` 懒加载它们，是为了让 LLM-only 的工作流（`amct_pytorch.cli.llm` / `common.models`）可以无痛 `import amct_pytorch` 而不需要这些重依赖；如果确实缺依赖，也会在使用点「大声报错」，而不是在 import 时被悄悄吞掉。

[amct_pytorch/__init__.py:56-71](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/__init__.py#L56-L71) —— 这是懒加载的实现，关键三步：

```python
def __getattr__(name):
    if name.startswith("__"):
        raise AttributeError(name)
    import importlib
    # ① 按需加载重的 graph_based 模块
    _graph = importlib.import_module("amct_pytorch.classic.graph_based.amct_pytorch")
    try:
        value = getattr(_graph, name)        # ② 取出被请求的符号
    except AttributeError:
        raise AttributeError(f"module 'amct_pytorch' has no attribute {name!r}") from None
    globals()[name] = value                   # ③ 手动缓存，下次直接命中
    return value
```

> 名词解释：**PEP 562** 是 Python 3.7+ 的特性，允许在模块级别定义 `__getattr__`，使得「访问模块上一个不存在的属性」时触发自定义逻辑，而不是立刻报错。这里用它实现了「用到才导入」。

#### 4.3.4 代码实践

**实践目标**：亲手观察懒加载的效果，验证「不访问 graph_based 符号就不会加载 onnx 相关模块」。

**操作步骤**（源码阅读型实践，无需 NPU 环境）：

1. 在已安装 AMCT 的 Python 环境里，先执行一次干净导入并检查重依赖是否已加载：

   ```python
   import sys
   import amct_pytorch              # 仅触发 eager 导入
   print("onnx loaded?", "onnx" in sys.modules)
   ```

2. 接着访问一个 graph_based 的符号（如 `create_quant_config`，前提是环境里装了 onnx），再次检查：

   ```python
   _ = amct_pytorch.create_quant_config   # 触发 __getattr__
   print("onnx loaded now?", "onnx" in sys.modules)
   ```

**需要观察的现象**：第 1 步打印应为 `False`（onnx 未加载）；第 2 步打印应为 `True`（访问图压缩符号后才加载）。

**预期结果**：验证了「LLM-only 用户 `import amct_pytorch` 不需要 onnx」的设计目标。

> 待本地验证：如果环境未安装 onnx，第 2 步会在 `importlib.import_module(...)` 处抛出依赖缺失错误——这正是注释所说「在使用点大声报错」的预期行为。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `__getattr__` 里要写 `globals()[name] = value` 这一行？去掉会怎样？

**参考答案**：PEP 562 的模块级 `__getattr__` 不会缓存结果，每次访问都会重新触发。把这行写回模块全局命名空间后，第二次访问该属性会直接命中已有的全局变量，不再走 `__getattr__`、不再重复 `importlib.import_module`。去掉会导致每次访问都重新解析，浪费时间。

**练习 2**：`from amct_pytorch import *` 能拿到 LLM PTQ 的 `LlmPtqWorkflow` 类吗？为什么？

**参考答案**：不能。`__all__`（见 [amct_pytorch/__init__.py:18-33](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/__init__.py#L18-L33)）里只列了 classic 的三个函数和一组配置常量，没有 Workflow 类。`from amct_pytorch import *` 只会导入 `__all__` 中声明的符号；要用 Workflow 类需要显式从 `amct_pytorch.workflows.llm_ptq` 导入。

**练习 3**：`__getattr__` 开头为什么要有 `if name.startswith("__"): raise AttributeError(name)`？

**参考答案**：Python 解释器在 import 期间会探测各种 dunder 属性（如 `__path__`、`__spec__` 等）。如果不拦截以 `__` 开头的名字，这些探测会被误导向 `graph_based` 的加载逻辑，既浪费又可能引发意外。提前抛 `AttributeError` 让解释器走默认的模块属性查找路径。

---

## 5. 综合实践

**任务**：产出一份数据准确的「AMCT 目录速查表」，把本讲知识串起来。

**操作步骤**：

1. 在仓库根目录执行 `ls -1 amct_pytorch/`，列出 `amct_pytorch` 下的**全部**二级目录与文件。
2. 与 [README.md:133-140](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/README.md#L133-L140) 中 README 列出的 7 个二级目录对照。
3. 为**每一个**实际存在的二级目录写一句话用途说明，要求：
   - README 列出的目录：直接采用 README 的描述。
   - README 没列出的目录（如 `classic` / `nn` / `quantize_op` / `tensor_decompose`）：结合本讲和 AGENTS.md 自己补一句。
4. 给每个二级目录打上标签：**「LLM PTQ 主流程」** / **「Classic 经典图压缩」** / **「公共基础」**。
5. 整理成一张 Markdown 表格，作为你后续阅读源码的随身地图。

**预期结果示例**（结构，具体描述请自己填写并本地核对）：

| 二级目录 | 是否在 README 图中 | 用途（一句话） | 所属主线 |
|------|:---:|------|------|
| `algorithms/` | ✅ | 量化算法实现 | LLM PTQ |
| `classic/` | ❌ | （自行补全） | Classic |
| `cli/` | ✅ | 命令行入口 | LLM PTQ |
| `common/` | ✅ | 通用工具、模型和数据处理 | 公共基础 |
| `configs/` | ✅ | 量化配置模板 | LLM PTQ |
| `workflows/` | ✅ | LLM 量化、评估和部署流程 | LLM PTQ |
| ... | ... | ... | ... |

> 完成后，你应该能闭着眼睛回答：「我要改 PTQ 流程去哪？我要加个经典图压缩 pass 去哪？我要加个新量化数据类型去哪？」——这就达到了本讲的目标。

## 6. 本讲小结

- AMCT 仓库顶层由 `amct_pytorch`（核心量化源码）、`amct_ops`（NPU 自定义算子）、`examples`/`tests`/`docs`（样例/测试/文档）与构建脚本（`build.sh`/`setup.py`/`requirements.txt`）组成。
- README 的目录结构图是**精简视图**，真实仓库还有 `scripts/`、`pyproject.toml`、`version.info` 等未画出的条目；阅读时应「先看 README 建主干，再用 `ls` 补细节」。
- `amct_pytorch` 内部并存两条主线：**LLM PTQ 主流程**（`workflows/` + 根目录四入口 + `cli/llm` + `algorithms/quant` + `quantization` + `configs`）与 **Classic 经典图压缩**（`classic/` 下的 `quantize_op`/`deploy_op`/`optimizer`/`graph_based`），二者定位不同、并行存在。
- `amct_pytorch/__init__.py` 默认 eager 导入的是 classic 接口与配置常量；重的 graph_based（依赖 onnx/protobuf）通过 PEP 562 的 `__getattr__` **懒加载**，使 LLM-only 用户无需这些重依赖。
- 模块级 `__getattr__` 通过 `globals()[name] = value` 手动缓存解析结果，避免重复导入。
- 面对陌生代码，先建立「目录地图」再钻细节，是最高效的源码阅读习惯。

## 7. 下一步学习建议

- 下一篇 [u1-l4 一站式量化初体验：四条 CLI 命令](u1-l4-first-quant-cli.md) 会带你看懂 `examples/*.sh` 脚本，把本讲提到的 `eval` / `extract_ptq_data` / `ptq` / `deploy` 四条命令真正跑通串联。
- 如果你对构建打包好奇，可以补读 [u1-l2 环境准备、源码构建与安装](u1-l2-build-and-install.md)，理解 `build.sh` 如何把本讲的 `amct_pytorch` 目录打成可安装的 tar.gz。
- 进阶阶段（第 3 单元起）会逐一深入本讲提到的 `cli/` / `workflows/` / `common/models/` 等目录；建议届时回看本讲的「目录速查表」作为导航。
- 想提前感受 classic 经典流程的读者，可以先浏览 [amct_pytorch/classic/optimizer/model_optimizer.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/classic/optimizer/model_optimizer.py) 里的图优化 pass 编排，体会与 LLM PTQ 流程不同的「图模式」风格。
