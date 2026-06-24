# 目录结构与包导出

## 1. 本讲目标

本讲带你从「文件层面」认识 DualPipe。读完本讲后，你应该能够：

1. 说出仓库里每个目录和文件分别是干什么的，知道哪些文件会被打包安装、哪些只是示例与文档。
2. 记住 `dualpipe` 包对外的五个公共 API 符号：`DualPipe`、`DualPipeV`、`WeightGradStore`、`set_p2p_tensor_shapes`、`set_p2p_tensor_dtype`，并知道它们各自来自哪个源码文件。
3. 画出 `dualpipe` 包内四个模块（`comm.py` / `utils.py` / `dualpipe.py` / `dualpipev.py`）之间的 `import` 依赖关系，理解「分层」设计。

本讲不深入任何算法细节，只解决「项目长什么样、对外暴露什么、内部怎么组织」这三件事。算法本身留给后续单元。

## 2. 前置知识

- **Python 包（package）**：一个含有 `__init__.py` 的目录就是一个包。`import dualpipe` 时，Python 会先执行 `dualpipe/__init__.py`，这个文件决定了「别人 `import dualpipe` 之后能直接拿到哪些名字」。
- **`__all__`**：一个列表，声明「`from dualpipe import *` 时要导出哪些符号」。它既是文档，也是对外 API 的正式清单。
- **重新导出（re-export）**：一个模块在自己文件里 `from X import Y`，纯粹是为了让使用者能从一个统一入口（通常是包根）拿到 `Y`，而不用关心 `Y` 真正定义在哪个子模块里。`__init__.py` 最常见的用法就是做这种「门面（facade）」。
- **`setup.py` 与打包**：`setup.py` 是旧式 Python 打包脚本，其中的 `packages=["dualpipe"]` 告诉打包工具「把 `dualpipe` 这个目录作为一个包安装」。

前两讲你已经知道了 DualPipe 是双向流水线并行算法、能用 `python examples/example_dualpipe.py` 运行示例。本讲把这些「用法」对应回「代码文件」。

## 3. 本讲源码地图

下表列出本讲涉及的关键文件与职责（基于仓库实际被 git 跟踪的文件清单）：

| 文件 | 角色 | 本讲关注点 |
|------|------|-----------|
| `dualpipe/__init__.py` | 包入口 / 公共 API 门面 | 五个导出符号从哪来 |
| `dualpipe/comm.py` | P2P 通信辅助层 | 定义 `set_p2p_tensor_shapes` / `set_p2p_tensor_dtype` |
| `dualpipe/utils.py` | 通用工具层 | 定义 `WeightGradStore` |
| `dualpipe/dualpipe.py` | DualPipe 引擎 | 定义 `DualPipe` 类 |
| `dualpipe/dualpipev.py` | DualPipeV 引擎 | 定义 `DualPipeV` 类 |
| `setup.py` | 打包脚本 | 声明 `packages=["dualpipe"]` |
| `README.md` | 项目说明 | Quick Start、Requirements |
| `examples/*.py` | 示例脚本 | 不被打包，仅作演示 |
| `images/*.png` | 调度示意图 | 文档配图 |

仓库里还有 `LICENSE`（许可证）和 `.gitignore`（git 忽略规则），它们与代码逻辑无关，本讲略过。

## 4. 核心概念与源码讲解

### 4.1 仓库目录布局

#### 4.1.1 概念说明

一个「能被安装的 Python 库」通常由两部分组成：

- **包代码**：真正会被 `import` 的代码，放在以包名命名的目录里（这里是 `dualpipe/`）。
- **外围文件**：打包脚本、说明文档、示例、图片等，它们帮助人类理解和使用项目，但不会被装进别人的 Python 环境里。

DualPipe 把这两部分分得很清楚：算法代码全在 `dualpipe/` 目录里，外围的东西（`setup.py`、`README.md`、`examples/`、`images/`）都放在仓库根目录下，互不混淆。

#### 4.1.2 核心流程

仓库的目录树大致如下（按目录分组）：

```
deepseek-ai-DualPipe/          # 仓库根目录
├── dualpipe/                  # 【包代码】唯一会被安装的目录
│   ├── __init__.py            #   包入口 + 公共 API 门面
│   ├── comm.py                #   P2P 通信辅助
│   ├── utils.py               #   通用工具（WeightGradStore 等）
│   ├── dualpipe.py            #   DualPipe 引擎
│   └── dualpipev.py           #   DualPipeV 引擎
├── examples/                  # 【示例】不会被安装
│   ├── example_dualpipe.py
│   └── example_dualpipev.py
├── images/                    # 【文档配图】README 引用的调度图
│   ├── dualpipe.png
│   └── dualpipev.png
├── setup.py                   # 【打包脚本】
├── README.md                  # 【项目说明】
├── LICENSE                    # 【许可证】
└── .gitignore                 # 【git 忽略规则】
```

判断「某个文件会不会被安装」的依据不在目录树里，而在 `setup.py` 里：打包脚本显式声明了 `packages=["dualpipe"]`，意思是「只有 `dualpipe/` 这一个目录作为包被安装」。`examples/` 和 `images/` 没有出现在 `packages` 列表里，所以它们只是仓库的一部分，不会进到用户的 `site-packages`。

#### 4.1.3 源码精读

打包声明在 `setup.py` 中，关键就是 `packages=["dualpipe"]` 这一行：

[setup.py:15-19](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/setup.py#L15-L19) —— 这段配置把包名设为 `dualpipe`，版本号用 `1.0.0` 加上一个 git 短哈希后缀，并明确只打包 `dualpipe` 这一个目录。`examples/` 与 `images/` 不在其中，故不会被安装。

README 的 Quick Start 也印证了「示例脚本独立于包」这一点：它直接以脚本路径运行，而不是 `import`：

[README.md:38-47](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/README.md#L38-L47) —— README 用 `python examples/example_dualpipe.py` 这种「运行脚本」的方式调用，而不是 `python -c "import dualpipe"`，说明示例是仓库自带的演示程序，与可安装的包本体是分开的两类东西。

> 说明：上面目录树的内容来自仓库实际被 git 跟踪的文件清单（可用 `git ls-files` 得到），不是凭空构造的。

#### 4.1.4 代码实践

**实践目标**：用工具亲自确认「哪些文件属于包，哪些只是外围」。

**操作步骤**：

1. 在仓库根目录运行 `git ls-files`，查看被 git 跟踪的全部文件。
2. 再运行 `python setup.py --version`（或查看 `setup.py` 源码），确认包名与打包范围。

**需要观察的现象**：

- `git ls-files` 输出里会同时出现 `dualpipe/*`、`examples/*`、`images/*`、`setup.py`、`README.md` 等，说明它们都在仓库里。
- 但 `setup.py` 的 `packages` 只写了 `["dualpipe"]`，说明安装后用户环境里只会多出 `dualpipe/` 这一个包，不会有 `examples` 或 `images`。

**预期结果**：你能够清楚区分「包代码」与「外围文件」两组，并理解为什么 `import dualpipe` 能工作、而 `import examples` 不行。

**待本地验证**：若环境中尚未安装打包工具或 git，以上命令可能不可用，请以实际输出为准。

#### 4.1.5 小练习与答案

**练习 1**：如果有人想给仓库加一个「训练脚本」`train.py`，放在哪里合适？放在 `dualpipe/` 里好还是仓库根目录好？

**参考答案**：若是给最终用户当库使用的核心逻辑，才放进 `dualpipe/`；若是「如何使用这个库」的示例或一次具体训练的入口脚本，更适合放在仓库根目录或 `examples/` 下，保持包本体精简。DualPipe 把示例统一放在 `examples/`，是同样的思路。

**练习 2**：`images/dualpipe.png` 会不会出现在 `pip install dualpipe` 安装后的 `site-packages/dualpipe/` 里？

**参考答案**：不会。因为 `setup.py` 的 `packages=["dualpipe"]` 只打包 `dualpipe/` 目录，`images/` 不在打包范围内。`images/` 只是 README 引用的文档配图。

---

### 4.2 `__init__.py` 的五个公共导出符号

#### 4.2.1 概念说明

`dualpipe/__init__.py` 是这个包的「门面」。它的职责不是实现算法，而是把散落在各子模块里的「对外要用的名字」集中起来，让使用者只需要写一句 `import dualpipe` 就能拿到全部公共 API。

它一共对外暴露 **五个**符号。理解这一节的关键，是建立「导出名 → 真正定义它的源码文件」这张映射表。这五个符号分别是：

| 导出符号 | 定义所在文件 | 它是什么 |
|---------|------------|---------|
| `DualPipe` | `dualpipe/dualpipe.py` | 双向流水线引擎（核心类） |
| `DualPipeV` | `dualpipe/dualpipev.py` | V 型变体引擎 |
| `WeightGradStore` | `dualpipe/utils.py` | 零气泡用的权重梯度缓存类 |
| `set_p2p_tensor_shapes` | `dualpipe/comm.py` | 设置 P2P 通信张量形状 |
| `set_p2p_tensor_dtype` | `dualpipe/comm.py` | 设置 P2P 通信张量数据类型 |

这五个名字构成了 DualPipe 对外 API 的「全部正式清单」——因为它们被写进了 `__all__`。

#### 4.2.2 核心流程

`__init__.py` 的执行流程非常简单，可以概括为三步：

1. 设置包版本号 `__version__`。
2. 从四个子模块里 `import` 进五个名字（重新导出）。
3. 用 `__all__` 列表正式声明「这些才是公共 API」。

当用户执行 `import dualpipe` 时，Python 触发上述流程，于是 `dualpipe.DualPipe`、`dualpipe.WeightGradStore` 等名字就都可用了。值得注意的是，`DualPipe` 与 `DualPipeV` 来自不同文件，说明它们是两套独立引擎；而两个 `set_p2p_*` 函数都来自 `comm.py`，因为它们都是通信层配置。

#### 4.2.3 源码精读

整个门面文件只有十几行，一次就能读完：

[dualpipe/__init__.py:1-17](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/__init__.py#L1-L17) —— 第 1 行设版本号；第 3–9 行从四个子模块把五个名字 `import` 进来（即「重新导出」）；第 11–17 行的 `__all__` 用字符串列出这五个公共 API，字符串而非类对象的形式也正好对应了最近一次提交 `fix: Use strings in __all__ instead of class objects` 的修复。

再看每个名字的「定义现场」，确认它们确实来自对应文件：

- [dualpipe/dualpipe.py:11](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L11) —— `class DualPipe(nn.Module)`，`DualPipe` 在此定义。
- [dualpipe/dualpipev.py:11](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipev.py#L11) —— `class DualPipeV(nn.Module)`，`DualPipeV` 在此定义。
- [dualpipe/comm.py:11-13](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/comm.py#L11-L13) 与 [dualpipe/comm.py:16-18](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/comm.py#L16-L18) —— `set_p2p_tensor_shapes` 与 `set_p2p_tensor_dtype` 两个配置函数。
- [dualpipe/utils.py:8](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/utils.py#L8) —— `class WeightGradStore`，零气泡机制的核心类。

> 小提示：因为重新导出的关系，`dualpipe.DualPipe` 和 `dualpipe.dualpipe.DualPipe` 是同一个对象。但公共 API 的「正确」写法是从包根导入（`from dualpipe import DualPipe`），不要直接去碰子模块路径——后者是实现细节，将来可能变动。

#### 4.2.4 代码实践

**实践目标**：用 Python 自省（introspection）亲自验证「五个导出符号分别来自哪个文件」。

**操作步骤**（需已安装 PyTorch 与 dualpipe）：

```bash
python -c "
import dualpipe
for name in dualpipe.__all__:
    obj = getattr(dualpipe, name)
    mod = getattr(obj, '__module__', None)
    print(f'{name:25s} -> {mod}')
"
```

**需要观察的现象**：每行打印一个导出名及其 `__module__` 属性，`__module__` 会告诉你它真正定义在哪个模块。

**预期结果**（待本地验证）：

```
DualPipe                  -> dualpipe.dualpipe
DualPipeV                 -> dualpipe.dualpipev
WeightGradStore           -> dualpipe.utils
set_p2p_tensor_shapes     -> dualpipe.comm
set_p2p_tensor_dtype      -> dualpipe.comm
```

这张输出与 4.2.1 的映射表完全对应，说明 `__init__.py` 只是把已有的名字重新挂到包根上，本身没有新定义任何东西。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `DualPipe` 和 `DualPipeV` 是两个不同的导出，而不是一个带参数的类？

**参考答案**：因为它们是两套调度策略不同的引擎——`DualPipe` 是双向流水线，`DualPipeV` 是「切半」的 V 型变体（README 里有专门章节）。两者拓扑与喂入方式差异较大，所以分别放在 `dualpipe.py` 与 `dualpipev.py`，作为两个独立类导出。

**练习 2**：如果只看 `__all__`，能知道一共有几个公共 API 吗？为什么 `__all__` 用字符串而不是直接写类名？

**参考答案**：能，`__all__` 里列了 5 个字符串，就是 5 个公共 API。用字符串（而非类对象）可以避免在定义 `__all__` 时就强制触发相关导入、也避免循环引用问题；最近一次提交 `fix: Use strings in __all__ instead of class objects` 正是为了修正这一点。

---

### 4.3 包内模块的 `import` 依赖关系

#### 4.3.1 概念说明

光知道「五个符号来自哪个文件」还不够。要真正看懂 DualPipe 的代码组织，还需要理解四个子模块之间**谁依赖谁**。这决定了你阅读源码的顺序：应该先读「不依赖别人」的底层模块，再读建在它们之上的引擎。

DualPipe 的内部依赖呈现清晰的**分层**结构：

- **底层（叶子模块）**：`comm.py` 和 `utils.py`。它们只依赖 PyTorch，不依赖包内任何其他模块。
- **引擎层**：`dualpipe.py` 和 `dualpipev.py`。它们都依赖底层的 `comm` 和 `utils`，但互相不依赖。
- **门面层**：`__init__.py`。它依赖上面所有四个模块，把它们汇总成公共 API。

这种「叶子 → 引擎 → 门面」的单向依赖，意味着没有循环依赖，阅读顺序很明确。

#### 4.3.2 核心流程

把四个子模块当作节点，`A → B` 表示「A `import` 了 B」，得到的依赖图如下：

```
                  __init__.py   （门面层：重新导出全部 5 个符号）
                 /     |      \
                v      v       v
          dualpipe.py  dualpipev.py      （引擎层：两套调度，互不依赖）
                \      |      /
                 v     v     v
              comm.py    utils.py         （底层叶子：只依赖 PyTorch）
```

具体来说：

- `comm.py`：开头只 `import torch`、`import torch.distributed`，**没有**任何 `dualpipe.*` 导入——它是叶子。
- `utils.py`：开头只 `import torch`、`from torch.autograd import Variable`，同样**没有**包内导入——它也是叶子。
- `dualpipe.py`：`import dualpipe.comm as comm` 且 `from dualpipe.utils import ...`——依赖 `comm` 与 `utils`。
- `dualpipev.py`：导入语句与 `dualpipe.py` 完全一致——也依赖 `comm` 与 `utils`，与 `dualpipe.py` 之间没有依赖。
- `__init__.py`：从四个子模块分别导入——处于最顶层。

因为依赖是单向的，所以包可以安全地从底层往上层逐步加载，不会出现「A 要先加载 B、B 又要先加载 A」的死锁。

#### 4.3.3 源码精读

引擎层的导入语句最能说明分层关系。两个引擎文件的导入**完全相同**：

[dualpipe/dualpipe.py:7-8](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L7-L8) —— `import dualpipe.comm as comm` 与 `from dualpipe.utils import WeightGradStore, run_backward, scatter, gather`，说明 `DualPipe` 引擎同时需要通信层 `comm` 和工具层 `utils` 里的多个工具。

[dualpipe/dualpipev.py:7-8](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipev.py#L7-L8) —— `DualPipeV` 的导入与上一行一模一样，这暗示两套引擎共享同一套底层基础设施，只是上层调度策略不同。

再看两个叶子模块的开头，确认它们不依赖包内任何东西：

[dualpipe/comm.py:1-4](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/comm.py#L1-L4) —— 只有 `typing`、`torch`、`torch.distributed`，没有 `dualpipe.*`。

[dualpipe/utils.py:1-5](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/utils.py#L1-L5) —— 只有 `queue`、`typing`、`torch`、`torch.autograd`，同样没有 `dualpipe.*`。

门面层则把四个模块串起来，已在 4.2.3 引用过的 [dualpipe/__init__.py:1-17](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/__init__.py#L1-L17) 就是依赖图的最顶层节点。

#### 4.3.4 代码实践

**实践目标**：亲手绘制 `dualpipe` 包内四个模块的 `import` 依赖图，并标注五个导出符号的来源。这是本讲的主实践。

**操作步骤**：

1. 打开四个文件，只看每个文件**最开头的导入区**（前 10 行左右）。
2. 对每个文件，记录「它 `import` 了哪些 `dualpipe.*` 模块」。
3. 用箭头画出「依赖者 → 被依赖者」，形成一张分层图。
4. 在每个模块节点旁，标注「它定义了哪个导出符号」。

**参考答案（依赖图 + 符号标注）**：

```
                    dualpipe/__init__.py
                    （门面：__all__ 汇总 5 个符号）
                   /        |          \
                  v         v           v
        dualpipe/dualpipe.py   dualpipe/dualpipev.py
        定义: DualPipe          定义: DualPipeV
              \                /
               \   都依赖      /
                v              v
        dualpipe/comm.py      dualpipe/utils.py
        定义: set_p2p_        定义: WeightGradStore
              tensor_shapes         （还含 run_backward /
              set_p2p_               scatter / gather / 等，
              tensor_dtype            但只有 WeightGradStore 被导出）
```

依赖关系总结（仅列包内依赖）：

| 模块 | 包内依赖 | 定义并被导出的符号 |
|------|---------|------------------|
| `comm.py` | 无（叶子） | `set_p2p_tensor_shapes`、`set_p2p_tensor_dtype` |
| `utils.py` | 无（叶子） | `WeightGradStore` |
| `dualpipe.py` | `comm`、`utils` | `DualPipe` |
| `dualpipev.py` | `comm`、`utils` | `DualPipeV` |
| `__init__.py` | 上述全部 | （自身不定义，只重新导出） |

**需要观察的现象**：图中箭头始终「自上而下」从门面/引擎指向底层叶子，没有任何回头箭头——这就是「无循环依赖」的直观体现。

**预期结果**：你得到一张三层（叶子 → 引擎 → 门面）的单向依赖图，并能指着图说出每个导出符号来自哪一层、哪个文件。这也直接给出了后续讲义的**推荐阅读顺序**：先读 `comm.py`、`utils.py`（第 2 单元），再读 `dualpipe.py`（第 3 单元），最后读 `dualpipev.py`（第 4 单元）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `dualpipe.py` 不直接 `import dualpipev`（反之亦然）？

**参考答案**：因为两者是平行的两套引擎，共享底层 `comm`/`utils`，但彼此独立。互相导入不仅没必要，还可能制造循环依赖。把它们设计成「都依赖底层、互不依赖」的兄弟节点，是清晰的分层。

**练习 2**：`utils.py` 里其实定义了好几个东西（`WeightGradStore`、`run_backward`、`scatter`、`gather`、`chunk_tensor`、`cat_tensor`），但 `__init__.py` 只导出了 `WeightGradStore`。这说明什么？

**参考答案**：说明 `run_backward`、`scatter`、`gather` 等是**内部工具**，主要供 `dualpipe.py` / `dualpipev.py` 这两个引擎使用，不属于面向用户的公共 API。只有 `WeightGradStore` 因为用户在写自定义 `overlapped_forward_backward` 时需要配合使用（见 README 的 Note），才被提升为公共导出。这体现了「公共 API 精简」的设计原则。

## 5. 综合实践

**任务**：把本讲三个模块串起来，输出一份「DualPipe 包结构速查卡」。

请用你自己的方式（文本、表格或图都行）整理出以下三块内容，确保不看本讲也能复述：

1. **目录分类**：把 `git ls-files` 列出的文件分成「包代码 / 示例 / 文档配图 / 外围配置」四类。
2. **API 清单**：列出五个公共导出符号，并写出每个符号的定义文件。
3. **依赖图**：画出四个子模块的分层依赖图，标出哪些是叶子、哪些是引擎、哪个是门面。

**验收标准**：

- 你能回答「`pip install dualpipe` 之后，`site-packages` 里会出现哪些目录？」（答：只有 `dualpipe/`。）
- 你能回答「`from dualpipe import DualPipe` 时，`DualPipe` 实际是从哪个文件来的？」（答：`dualpipe/dualpipe.py`。）
- 你能说出阅读源码时应该先读哪个文件、后读哪个文件，并给出依赖上的理由。（答：先 `comm.py`/`utils.py`，再 `dualpipe.py`，最后 `dualpipev.py`，因为依赖单向。）

> 待本地验证：若你已装好环境，可结合 4.2.4 的自省脚本，把你整理的清单与程序实际输出对照，确保没有记错。

## 6. 本讲小结

- DualPipe 仓库分为「包代码 `dualpipe/`」与「外围文件（`setup.py`、`README.md`、`examples/`、`images/`）」；只有 `dualpipe/` 因 `setup.py` 的 `packages=["dualpipe"]` 而被安装。
- `dualpipe/__init__.py` 是包的门面，对外暴露**五个**公共 API：`DualPipe`、`DualPipeV`、`WeightGradStore`、`set_p2p_tensor_shapes`、`set_p2p_tensor_dtype`。
- 五个导出符号分别来自 `dualpipe.py`、`dualpipev.py`、`utils.py`、`comm.py`（两个 `set_p2p_*` 都来自 `comm.py`），可用 `__module__` 自省验证。
- 包内依赖是清晰的三层单向结构：叶子层（`comm.py`、`utils.py`）→ 引擎层（`dualpipe.py`、`dualpipev.py`）→ 门面层（`__init__.py`），无循环依赖。
- 这张依赖图同时给出了后续源码的**推荐阅读顺序**：先底层工具，再引擎，最后变体。
- `utils.py` 里虽有多个工具函数，但只有 `WeightGradStore` 被提升为公共导出，体现了「公共 API 精简」的原则。

## 7. 下一步学习建议

本讲让你看清了「骨架」。接下来请进入**第 2 单元（公共基础设施）**，按依赖图自底向上阅读：

1. **u2-l2 通信层 comm.py**：先读叶子模块 `comm.py`，理解 `set_p2p_tensor_shapes` / `set_p2p_tensor_dtype` 与 `append_irecv` / `append_isend` 如何累积 P2P 操作。
2. **u2-l3 scatter/gather 与 u2-l4 WeightGradStore**：再读另一个叶子模块 `utils.py`，理解微批次切分与零气泡缓存。
3. 读完底层后，再进入**第 3 单元**剖析 `dualpipe.py` 引擎，最后在**第 4 单元**对比 `dualpipev.py`。

一句话：本讲建立的是「地图」，下一讲开始按地图走进第一座「建筑」——`comm.py` 通信层。
