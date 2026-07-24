# 目录结构与源码地图

## 1. 本讲目标

在前两讲里，我们已经知道 SynthID Text 是什么（u1-l1），以及怎么安装、跑 Notebook、跑测试（u1-l2）。本讲不再讲"是什么"和"怎么跑"，而是帮你建立一张**源码地图**：仓库里到底有哪些文件、每个文件负责什么、它们分别属于"水印施加"还是"水印检测"、各自用的是哪个深度学习框架。

学完本讲，你应当能够：

- 说出 `src/synthid_text/` 下 9 个 Python 文件各自的职责。
- 区分哪些文件走 PyTorch 链路（水印施加）、哪些走 JAX/Flax 链路（检测）。
- 看懂本项目用"src 布局 + 命名空间包"组织代码的方式。
- 快速定位测试文件、Notebook 与数据文件。
- 记住一条贯穿全项目的学习原则：**当文档与源码冲突时，以源码为准。**

## 2. 前置知识

本讲是纯"认路"，不要求你懂水印算法。但有两个前置概念需要先建立直觉（它们来自 u1-l1）：

- **水印施加（watermarking）**：在 LLM 生成文本时，把隐藏的统计信号嵌进去。本项目这一侧用 **PyTorch**，因为它要挂在 HuggingFace Transformers 模型上。
- **水印检测（detection）**：读取一段文本，判断它是否带水印，输出一个 `[0,1]` 的分数。本项目这一侧用 **JAX/Flax**。

两侧之间靠一个共同的数据结构"**g 值**"打通：施加侧生成 g 值，检测侧消费 g 值。你暂时把 g 值理解成"一段二进制指纹"就够了，后面 u2 系列会专门讲它。

> 一个名词解释：**框架（framework）**指 PyTorch、JAX、Flax 这些做张量计算与神经网络建模的基础库。一个项目同时用两套框架并不常见，所以弄清"哪个文件属于哪一侧、用哪套框架"是阅读本项目的第一步。

## 3. 本讲源码地图

本讲涉及的关键文件如下（全部为仓库中真实存在的文件）：

| 文件 | 一句话职责 | 所属侧 / 框架 |
| --- | --- | --- |
| `pyproject.toml` | 构建、依赖、可选依赖分组 | 工程配置 |
| `src/synthid_text/logits_processing.py` | 水印施加内核：logits 处理器、g 值与掩码计算 | 水印施加 / PyTorch |
| `src/synthid_text/synthid_mixin.py` | HuggingFace 集成：Mixin、Gemma/GPT-2 子类、默认配置 | 水印施加 / PyTorch |
| `src/synthid_text/hashing_function.py` | 共享哈希函数 `accumulate_hash`（LCG） | 公共工具 / PyTorch |
| `src/synthid_text/g_value_expectations.py` | g 值的理论期望值公式 | 公共工具 / 纯 Python |
| `src/synthid_text/detector_mean.py` | Mean / Weighted Mean 打分（免训练） | 检测 / JAX |
| `src/synthid_text/detector_bayesian.py` | 贝叶斯检测器：数据处理、训练、打分 | 检测 / JAX+Flax |
| `src/synthid_text/torch_testing.py` | 测试辅助：选 GPU/CPU 设备 | 测试辅助 / PyTorch |
| `src/synthid_text/logits_processing_test.py` | 水印施加相关测试 | 测试 / PyTorch |
| `src/synthid_text/synthid_mixin_test.py` | HuggingFace Mixin 相关测试 | 测试 / PyTorch |
| `notebooks/*.ipynb` | 端到端演示与集成测试 Notebook | 示例 |
| `data/human_eval.jsonl` | 人工评估数据 | 数据 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块，分别对应任务要求的三件事：**src 包结构**、**PyTorch 文件 vs JAX 文件**、**测试与 Notebook 位置**。

### 4.1 src 包结构

#### 4.1.1 概念说明

本项目采用 Python 的 **src 布局（src layout）**：真正的代码不在仓库根目录，而是放在 `src/` 下面的包目录里。这里包名是 `synthid_text`，所以你在代码里看到的所有 import 都是 `from synthid_text import xxx` 的形式。

一个特别值得注意的细节：**这个包里没有 `__init__.py` 文件**。在传统的 Python 包里，`__init__.py` 用来把一个目录"声明"为包；没有它，则按 PEP 420 成为**命名空间包（namespace package）**——Python 依然能把目录当成包来 import，但不强制要求有 `__init__.py`。这意味着 `synthid_text` 目录下 9 个 `.py` 文件是"平铺"的兄弟模块，互相之间用 `from synthid_text import <模块名>` 引用。

> 对初学者：你不需要现在理解命名空间包的全部细节，只要记住两件事——(1) 没看到 `__init__.py` 不是错误；(2) import 路径前缀统一是 `synthid_text`。

#### 4.1.2 核心流程

一个源码文件从"躺在磁盘上"到"能被 import、能被测试"经历这样的过程：

```text
src/synthid_text/<module>.py
        │  (1) pip install -e . 让 setuptools 发现 src 布局)
        ▼
可被 `from synthid_text import <module>` 导入
        │  (2) pytest 自动发现文件名形如 *_test.py 的模块)
        ▼
其中的 TestCase 类被执行
```

要点：

- **打包发现**：`pyproject.toml` 声明构建后端是 `setuptools.build_meta`（见 4.1.3）。setuptools 在 src 布局下能自动发现 `synthid_text` 包。
- **测试发现**：`pytest` 默认把文件名匹配 `*_test.py` 的模块当作测试文件（本项目的测试文件就叫 `logits_processing_test.py`、`synthid_mixin_test.py`）。

#### 4.1.3 源码精读

先看构建配置，它决定了包怎么被组装出来。注意它**没有显式写 `[tool.setuptools.packages.find]`**，所以走的是 setuptools 的自动发现：

- [pyproject.toml:5-13](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/pyproject.toml#L5-L13)：声明项目名 `synthid-text`、版本、README 与 License 文件位置。中文说明：这是包的"身份证"。
- [pyproject.toml:1-3](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/pyproject.toml#L1-L3)：构建系统要求 `setuptools>=68.0`。中文说明：选用了较新版本的 setuptools，保证 src 布局自动发现的行为稳定。

再看包里到底有哪些文件。下面这 9 个就是 `src/synthid_text/` 下的全部 `.py`（已确认目录下**没有 `__init__.py`**）：

- [src/synthid_text/hashing_function.py](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/hashing_function.py)：模块文档字符串写的是 "Hashing function implementation."（见 [:16](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/hashing_function.py#L16)）。中文说明：共享的哈希函数实现。
- [src/synthid_text/logits_processing.py:16](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L16)：文档字符串 "Logit processor for supporting watermarking in HF model."。中文说明：水印施加的核心处理器。
- [src/synthid_text/synthid_mixin.py:16](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/synthid_mixin.py#L16)：文档字符串 "SynthID watermarked mixin class."。中文说明：把水印挂到 HuggingFace 模型上的 Mixin。
- [src/synthid_text/g_value_expectations.py:16](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/g_value_expectations.py#L16)：文档字符串 "Expected g-value for watermarking."。中文说明：g 值的理论期望公式。
- [src/synthid_text/detector_mean.py:16](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_mean.py#L16)：文档字符串 "Code for Mean and Weighted Mean scoring functions."。中文说明：免训练的均值打分。
- [src/synthid_text/detector_bayesian.py:16](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L16)：文档字符串 "Bayesian detector class."。中文说明：贝叶斯检测器。
- [src/synthid_text/torch_testing.py:16](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/torch_testing.py#L16)：文档字符串 "Utility function for testing with torch."。中文说明：测试辅助函数。
- [src/synthid_text/logits_processing_test.py](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing_test.py) 与 [src/synthid_text/synthid_mixin_test.py](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/synthid_mixin_test.py)：两个测试模块。

#### 4.1.4 代码实践（源码阅读型）

**实践目标**：亲手确认包结构，并发现一个"文档与源码不一致"的真实案例。

**操作步骤**：

1. 列出包内文件。在仓库根目录执行（这是只读命令，不会改动任何东西）：

   ```python
   # 在已安装本包的环境里运行
   import synthid_text, os
   pkg_dir = os.path.dirname(synthid_text.__file__)
   print(sorted(os.listdir(pkg_dir)))
   ```

   预期会看到 9 个 `.py` 文件，且**没有** `__init__.py`。

2. 发现文档不一致：README 在检测示例里写的是

   ```python
   from synthid_text import train_detector_bayesian
   ...
   detector, loss = train_detector_bayesian.optimize_model(...)
   ```

   对应链接指向 [README.md:332](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/README.md#L332) 的 `./src/synthid_text/train_detector_bayesian.py`。但你刚才列出的 9 个文件里**根本没有** `train_detector_bayesian.py`。

3. 去 [src/synthid_text/detector_bayesian.py:986](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L986) 找真正的训练入口——它叫 `BayesianDetector.train_best_detector`，是一个**类方法**，而不是 README 说的模块级函数 `optimize_model`。

**需要观察的现象**：

- 包目录里确实没有 `train_detector_bayesian.py`。
- README 的那段示例代码如果照抄，会报 `ModuleNotFoundError`。

**预期结果**：你会得出结论——"文档里提到的文件/函数可能过时，真实训练入口在 `detector_bayesian.py` 里"。这正是本讲想强调的原则：**以源码为准**。运行结果若与本地环境有出入，请以本地为准（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `from synthid_text import logits_processing` 能成立，即使目录里没有 `__init__.py`？

> **参考答案**：因为 `synthid_text` 是 PEP 420 命名空间包，Python 不要求 `__init__.py` 也能把目录识别为可导入的包。

**练习 2**：README 提到的 `train_detector_bayesian.optimize_model` 在当前代码里不存在。真正的训练入口是哪个？它的调用形态是模块级函数还是类方法？

> **参考答案**：真正的入口是 `detector_bayesian.py` 中的 `BayesianDetector.train_best_detector`（以及它调用的 `train_best_detector_given_g_values`），是**类方法**，不是模块级函数 `optimize_model`。

---

### 4.2 PyTorch 文件 vs JAX 文件

#### 4.2.1 概念说明

本项目最容易被初学者忽略、却最关键的结构事实是：**水印施加和检测用的是两套不同的深度学习框架**。

- 水印施加要"贴着 HuggingFace Transformers 的生成流程"工作，而 Transformers 是 PyTorch 生态，所以这一侧全用 **PyTorch（`torch`、`transformers`）**。
- 检测侧（含训练）是 DeepMind 的惯用栈，用 **JAX + Flax（`jax`、`jax.numpy`、`flax.linen`、`optax`）**。
- 两侧通过 **g 值**这个 numpy/tensor 中间产物衔接：施加侧用 PyTorch 算出 g 值，转成 numpy 后交给检测侧的 JAX 函数打分。

> 名词：**Flax** 是基于 JAX 的神经网络库（类似 PyTorch 之于 Python 的关系）；**optax** 是 JAX 生态的优化器库（Adam 等就在这里）。

判断一个文件属于哪一侧，最可靠的办法不是看文件名，而是**看它的 import**。下一节我们就用 import 当证据。

#### 4.2.2 核心流程

把 9 个文件按"侧 × 框架"分类如下：

```text
水印施加（PyTorch）           检测（JAX/Flax）              公共/其它
├─ logits_processing.py       ├─ detector_mean.py           ├─ hashing_function.py (torch)
├─ synthid_mixin.py           └─ detector_bayesian.py*      ├─ g_value_expectations.py (纯 Python)
└─ (依赖 transformers)                                      ├─ torch_testing.py (torch, 测试辅助)
                                                            └─ *_test.py (测试)
```

> `*` 标注：`detector_bayesian.py` 同时 import 了 `torch`，但这并不表示它是"PyTorch 侧"——它 import torch 只是为了**预处理从施加侧传过来的 PyTorch 张量**（截断、填充、转 numpy）。它的建模与训练核心仍是 JAX/Flax。

#### 4.2.3 源码精读

用每个文件顶部的 import 行作为"框架归属"的硬证据：

- [src/synthid_text/logits_processing.py:18-22](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing.py#L18-L22)：`import torch`、`import transformers`、`from synthid_text import hashing_function`。中文说明：典型 PyTorch 水印施加模块，且依赖自家 `hashing_function`。
- [src/synthid_text/synthid_mixin.py:18-24](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/synthid_mixin.py#L18-L24)：`import immutabledict`、`import torch`、`import transformers`、`from synthid_text import logits_processing`。中文说明：PyTorch 侧，且调用 `logits_processing`。
- [src/synthid_text/hashing_function.py:18](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/hashing_function.py#L18)：只 `import torch`。中文说明：共享工具，用 PyTorch 张量实现 LCG 哈希（[:21-51](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/hashing_function.py#L21-L51)）。
- [src/synthid_text/detector_mean.py:18-19](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_mean.py#L18-L19)：`from typing import Optional`、`import jax.numpy as jnp`。中文说明：纯 JAX 的免训练打分（`mean_score` [:22-41](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_mean.py#L22-L41)、`weighted_mean_score` [:44-77](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_mean.py#L44-L77)）。
- [src/synthid_text/detector_bayesian.py:24-31](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L24-L31)：`import flax.linen as nn`、`import jax`、`import jax.numpy as jnp`、`import optax`、`import torch`。中文说明：JAX/Flax 检测与训练内核，torch 仅用于数据预处理。
- [src/synthid_text/g_value_expectations.py:16-19](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/g_value_expectations.py#L16-L19)：**没有任何深度学习 import**，只有一个纯数学函数。中文说明：理论工具，不依赖任何框架。

#### 4.2.4 代码实践（源码阅读型）

**实践目标**：理解"一个 JAX 文件里为什么会出现 torch"。

**操作步骤**：

1. 打开 [src/synthid_text/detector_bayesian.py:761](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L761) 的 `BayesianDetector.process_raw_model_outputs`，看它的参数类型注解里出现了 `torch.tensor`（例如 [:37-39](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/detector_bayesian.py#L37-L39) 的 `pad_to_len` 接收 `torch.tensor`）。
2. 思考：为什么一个"检测器"要接收 PyTorch 张量？

**需要观察的现象**：检测侧的输入数据（g 值、token 序列）是从**施加侧（PyTorch）**的模型输出里来的，所以需要一个"中间处理层"先用 torch 处理（截断、填充、对齐掩码），再转成 numpy/jnp 交给 JAX 做打分与训练。

**预期结果**：你能用一句话解释——"`detector_bayesian.py` 里出现 `import torch` 不是因为它用 PyTorch 建模，而是因为它要消费施加侧产生的 PyTorch 张量"。这是个理解整条数据流的关键点（u1-l4 会把这条数据流串起来）。

#### 4.2.5 小练习与答案

**练习 1**：如果只看文件名，`detector_mean.py` 和 `detector_bayesian.py` 都像"检测"，怎么快速判断它们的框架归属？

> **参考答案**：看文件顶部的 import。`detector_mean.py` import 的是 `jax.numpy`；`detector_bayesian.py` import 的是 `flax.linen`、`jax`、`optax`。两者都是 JAX 侧。

**练习 2**：`hashing_function.py` 用 PyTorch，但它被检测侧间接用到了吗？

> **参考答案**：检测侧不直接 import 它，但检测要消费的 g 值，其计算过程（在 `logits_processing.py` 里）依赖 `hashing_function.accumulate_hash`。所以它是贯穿两侧的公共工具，只是"被调用点"在 PyTorch 侧。

---

### 4.3 测试与 Notebook 位置

#### 4.3.1 概念说明

除了源码，仓库里还有三类"非源码但很重要"的位置：

- **测试文件**：与被测模块同目录、文件名以 `_test.py` 结尾。这是 `pytest` 的默认发现约定。
- **Notebook（`.ipynb`）**：放在 `notebooks/` 下，是项目对外的"主入口"——README 反复指向的那个端到端示例就是一个 Notebook。
- **数据文件**：`data/human_eval.jsonl`，论文里做人工评估用的数据。

> 名词：**TestCase** 是 `absltest`/`unittest` 风格的测试类，里面的 `test_xxx` 方法就是一条条用例；**mock** 是用一个假对象替换真对象，用来验证"某函数是否真的被调用了"（本项目贝叶斯检测无关，但 mixin 测试用到）。

#### 4.3.2 核心流程

测试与演示的运行路径：

```text
pytest .                       notebooks/*.ipynb
  │ 自动收集 *_test.py            │ 用 Jupyter 打开，逐 cell 运行
  ▼                              ▼
执行 TestCase.test_xxx          端到端：加载水印模型 → 生成 → 重算 g 值 → 打分
  │                              │
  └─ GitHub CI (.github/workflows/ci.yaml) 也用 pytest -v 跑这套
```

CI 在 u1-l2 已经讲过，这里只需记住：CI 跑的就是这些 `*_test.py`。

#### 4.3.3 源码精读

- 测试文件与测试类：
  - [src/synthid_text/logits_processing_test.py:124](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing_test.py#L124)：`LogitsProcessorCorrectnessTest`（正确性测试，含 g 值均匀性、与理论期望对比等）。中文说明：验证水印施加是否"无偏"。
  - [src/synthid_text/logits_processing_test.py:319](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing_test.py#L319)：`LogitsProcessorTest`（功能性测试）。
  - [src/synthid_text/logits_processing_test.py:29](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/logits_processing_test.py#L29)：`does_mean_g_value_matches_theoretical`——把实测 g 值均值与 `g_value_expectations.py` 的理论值对比。
  - [src/synthid_text/synthid_mixin_test.py:61](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/synthid_mixin_test.py#L61)：`SynthidMixinTest`。中文说明：验证 Mixin 确实把水印接进了采样循环。
  - [src/synthid_text/synthid_mixin_test.py:34](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/synthid_mixin_test.py#L34)：`MockSynthIDModel`——一个用 mock 拼出来的最小可测模型。
- 测试辅助：[src/synthid_text/torch_testing.py:21-26](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/torch_testing.py#L21-L26)：`torch_device()`，自动选 CUDA 或 CPU。中文说明：让测试在有/无 GPU 的机器上都能跑。
- Notebook（非源码，但属于本讲"地图"的一部分）：
  - [notebooks/synthid_text_huggingface_integration.ipynb](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/notebooks/synthid_text_huggingface_integration.ipynb)：README 指向的端到端主示例。
  - [notebooks/testing_huggingface_integration.ipynb](https://github.com/google-deepmind-synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/notebooks/testing_huggingface_integration.ipynb)：集成测试用的 Notebook。
- 数据：[data/human_eval.jsonl](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/data/human_eval.jsonl)：人工评估数据，README "Human Data" 一节用到。

#### 4.3.4 代码实践（可操作）

**实践目标**：在不真正运行测试的前提下，看看 pytest 能发现哪些测试用例。

**操作步骤**：

1. 按 u1-l2 安装 `test` 依赖：`pip install '.[test]'`。
2. 在仓库根目录执行只收集、不运行的命令：

   ```shell
   pytest --collect-only -q
   ```

**需要观察的现象**：终端会列出所有被发现的测试节点（`*test.py` 里的 `TestCase.test_xxx`）。

**预期结果**：你能看到来自 `logits_processing_test.py` 与 `synthid_mixin_test.py` 的若干用例被收集；这也反过来印证了"文件名 `_test.py` 后缀 + `TestCase` 类"这套约定在起作用。具体用例数量取决于环境与参数化配置，待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`torch_testing.py` 不是测试文件（不以 `_test.py` 结尾），它为什么放在 `synthid_text/` 包里？

> **参考答案**：因为它是被测试文件 **import** 的公共辅助（提供 `torch_device()`），需要随包一起被安装、可被测试模块导入，所以放在包内而不是测试目录外。

**练习 2**：README 主推的"运行示例"是命令行脚本还是 Notebook？

> **参考答案**：是 Notebook——`notebooks/synthid_text_huggingface_integration.ipynb`。这也是为什么 u1-l2 强调 `notebook-local` 这一组可选依赖。

---

## 5. 综合实践

**任务**：画一张完整的源码地图，把 `src/synthid_text/` 下的 **9 个文件**按四类归类，并标出每类用到的深度学习框架。

**操作步骤**：

1. 用 4.1.4 里的 Python 片段列出 9 个文件。
2. 对每个文件，打开它的顶部 import，判断框架（torch / jax+flax / 纯 Python）。
3. 按下表把每个文件归入一类。先自己填，再对照参考答案。

**参考答案（源码地图）**：

| 分类 | 文件 | 框架 |
| --- | --- | --- |
| 水印施加 | `logits_processing.py`、`synthid_mixin.py` | PyTorch（`torch`、`transformers`） |
| 检测 | `detector_mean.py`、`detector_bayesian.py` | JAX / Flax（`jax`、`flax.linen`、`optax`） |
| 公共工具 | `hashing_function.py`（torch）、`g_value_expectations.py`（纯 Python）、`torch_testing.py`（torch，测试辅助） | torch / 纯 Python |
| 测试 | `logits_processing_test.py`、`synthid_mixin_test.py` | PyTorch（依赖被测的 torch 模块） |

**进阶思考**（不用写代码）：`hashing_function.py` 属于"公共工具"，但它只在 PyTorch 侧被直接 import。请用一句话解释它为何仍算"公共"——参考 4.2.5 的答案：它产生的哈希结果最终决定了两侧共享的 g 值。

## 6. 本讲小结

- 本项目的代码都在 `src/synthid_text/` 下，共 9 个 `.py` 文件，**没有 `__init__.py`**，靠 PEP 420 命名空间包工作。
- 框架是天然的"分水岭"：**水印施加用 PyTorch，检测用 JAX/Flax**；判断依据是看文件顶部的 import，而不是看文件名。
- `logits_processing.py` 是最核心的文件，它既负责水印施加，又提供检测所需的 g 值与掩码计算——是两侧的"交汇点"。
- 测试文件以 `_test.py` 结尾、与被测模块同目录；`torch_testing.py` 是被测试 import 的公共辅助，所以也在包内。
- 项目主入口是 `notebooks/` 下的 Notebook，而不是某个命令行脚本。
- **文档会过时，源码不会**：README 里的 `train_detector_bayesian.optimize_model` 在当前代码中不存在，真正入口是 `BayesianDetector.train_best_detector`。

## 7. 下一步学习建议

有了这张地图，下一步建议进入 u1-l4《端到端流程总览》，它会用 Notebook 的真实代码把"加载水印模型 → 生成 → 重算 g 值 → 打分"串成一条完整数据流，让你在深入单个文件前先看到全貌。

在阅读后续讲义时，建议把本讲的"源码地图表"放在手边：每读到一篇讲义，就在表里把对应文件"点亮"一次。例如读到 u3 系列时重点看 `logits_processing.py`，读到 u5/u6 系列时重点看 `detector_mean.py` / `detector_bayesian.py`。这样你始终知道"我现在站在地图的哪个位置"。
