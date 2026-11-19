# 项目总览：OLMo 是什么

> 本讲是 OLMo 学习手册的第一讲，对应大纲 `u1-l1`。学习手册其余讲义均位于 `OLMo-tutorial/` 目录。

## 1. 本讲目标

学完本讲，你应该能够：

1. 用一两句话说清 OLMo 项目的定位：它是 AI2（Allen Institute for Artificial Intelligence，艾伦人工智能研究院）开源语言模型的**训练与使用一体化框架**，并且把「数据、训练代码、中间与最终 checkpoint」全部公开，供科学界完整复现。
2. 说清这个仓库提供的五大核心能力：预训练（两阶段）、微调、HuggingFace 推理、量化、评估，并知道分别从哪个入口使用。
3. 了解 OLMo-2 系列 1B / 7B / 13B / 32B 模型、官方训练配置（`configs/official-*`）与 HuggingFace checkpoint 三者之间的对应关系，以及「32B 已迁移到 OLMo-core 仓库训练」这一现状。
4. 读懂 `pyproject.toml` 中的依赖声明、可选依赖组（extras）和动态版本机制。
5. 理解 `olmo/__init__.py` 这个包入口做了什么，`check_install()` 是如何报告版本号的。
6. 完成第一次动手：安装本项目并运行安装自检命令。

## 2. 前置知识

本讲不需要你已经懂深度学习实现细节，但以下概念最好有个印象：

- **语言模型（Language Model）**：给定前文 token，预测下一个 token 概率分布的模型。OLMo 训练的就是这类模型（decoder-only Transformer，与 GPT / Llama 同类）。
- **token 与 tokenizer**：文本被切分成的最小单元叫 token，负责切分的组件叫 tokenizer。本手册单元 2 会专门讲 OLMo 的 tokenizer 封装。
- **预训练 / 微调**：预训练（pretraining）指在海量数据上从零训练；微调（fine-tuning）指在已有权重上继续训练以适配新任务。
- **checkpoint**：训练过程中保存下来的模型权重（以及优化器等状态）快照。OLMo 的一个特点是**每 1000 步就发布一个 checkpoint**，方便研究者研究训练动态。
- **PyTorch 与 pip**：PyTorch 是本项目的深度学习框架；`pip` 是 Python 的包管理器。`pyproject.toml` 是现代 Python 项目的打包与依赖描述文件。
- **HuggingFace（HF）生态**：事实上的开源模型分发标准，`transformers` 库可以用统一 API 加载各家的模型。OLMo 通过 `hf_olmo` 包接入了这一生态。
- **开源科学复现理念**：很多「开源模型」只开源最终权重，训练数据、代码、中间状态不公开，导致结果难以复现。OLMo 的卖点是把**数据配方、训练配置、代码、全部 checkpoint**都放出来——"designed by scientists, for scientists"（为科学家设计，供科学家使用）。

## 3. 本讲源码地图

本讲涉及的关键文件如下（永久链接基于当前 HEAD `090253da`）：

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/README.md) | 项目门面：安装方式、两阶段预训练说明、各尺寸模型的配置与 checkpoint 对照表、推理/量化/评估入口 |
| [pyproject.toml](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/pyproject.toml) | 打包与依赖声明：包名 `ai2-olmo`、运行依赖、`dev`/`train`/`all` 可选依赖组、动态版本、被打包的目录范围 |
| [olmo/\_\_init\_\_.py](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/olmo/__init__.py) | 包入口：把 `config`、`model`、`tokenizer` 三个子模块的公开符号提升到顶层，并提供 `check_install()` 自检函数 |
| [olmo/version.py](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/olmo/version.py) | 版本号定义（`VERSION = "0.6.0"`），被 `pyproject.toml` 的动态版本机制读取 |

辅助了解（本讲只看声明，不深入实现）：

- [olmo/config.py:31-61](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/olmo/config.py#L31-L61)：`__all__` 导出 `TrainConfig`、`ModelConfig` 等配置类。
- [olmo/model.py:59-68](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/olmo/model.py#L59-L68)：`__all__` 导出 `OLMo` 等模型类。
- [olmo/tokenizer.py:15](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/olmo/tokenizer.py#L15)：`__all__ = ["Tokenizer"]`。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**项目定位**、**依赖与版本**、**包入口**。

### 4.1 模块一：项目定位

#### 4.1.1 概念说明

OLMo（Open Language Model，开放语言模型）是 AI2 开发的**开源语言模型训练与使用仓库**。它不只是一个「放权重的仓库」，而是包含完整训练框架（`olmo/` 包 + `scripts/train.py` 入口）、官方训练配置（`configs/`）、数据准备脚本，以及 HuggingFace 集成（`hf_olmo/`）的一体化项目。

它的核心理念是**科学可复现**：不只发布最终模型，还发布训练数据（`OLMo-mix-1124`、`Dolmino-mix-1124`）、每 1000 步一个的中间 checkpoint、完整训练配置和 WandB 训练日志。这样研究者可以精确研究「模型是怎么被练出来的」。

需要注意这个仓库的**现状**：README 顶部有一条醒目的公告——本仓库已不再活跃，最新的 OLMo 发布（包括 32B 的训练）迁移到了 [OLMo-core](https://github.com/allenai/OLMo-core) 仓库。但这**不影响本仓库的学习价值**：

- 1B / 7B / 13B 的完整训练链路（配置、数据、checkpoint）仍然在本仓库；
- 本仓库的代码结构清晰、依赖较轻，是学习「一个大模型训练框架长什么样」的绝佳教材；
- 后续讲义（单元 3~8）解剖的模型、训练器、检查点、评估代码都在本仓库的 `olmo/` 包内。

#### 4.1.2 核心流程

OLMo 仓库的能力地图可以概括为「两种用法、五个能力」：

```text
用法 A：训练 / 微调（本仓库原生链路）
  scripts/train.py + configs/*.yaml
      → olmo/（模型、数据、优化器、Trainer）
      → checkpoint（OLMo 原生格式）
      → scripts/unshard.py 等工具合并 / 转换

用法 B：推理 / 使用（接入 HuggingFace 生态）
  hf_olmo/（OLMoForCausalLM 等薄包装）
      → AutoModelForCausalLM.from_pretrained("allenai/OLMo-2-...")
      → generate() 生成文本 / load_in_8bit 量化推理
```

五个核心能力及其入口：

| 能力 | 入口 | 本手册对应讲义 |
| --- | --- | --- |
| 预训练（两阶段） | `scripts/train.py` + `configs/official-*` | 单元 1、4 |
| 微调 | 同训练入口，换配置与初始 checkpoint | 单元 4、6 |
| HF 推理 | `hf_olmo/` + `transformers` | 单元 7 |
| 量化（int8 / GPTQ） | README 示例 + `inference/compression/` | 单元 7 |
| 评估 | `olmo/eval/` + OLMo Eval / olmes 外部仓库 | 单元 8 |

其中「两阶段训练」是 OLMo-2 的标准做法：

- **Stage 1**：在约 4T~5T token 的大规模、以网页为主的数据（`OLMo-mix-1124`）上训练，是计算量最大的阶段；
- **Stage 2**：换到较小规模、高质量、定向筛选的数据（`Dolmino-mix-1124`，1B/7B 约 50B token，13B 达 300B）上继续训练；7B/13B 还会用不同随机种子训练多次后做权重平均（"model soup"）。

#### 4.1.3 源码精读

**（1）仓库现状公告与一句话定位。** README 开头先声明仓库迁移，再用一句话给出定位：

- [README.md:25](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/README.md#L25)：公告——本仓库相对最新发布已过时、不再活跃，最新发布在 OLMo-core。
- [README.md:27](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/README.md#L27)：项目一句话定位——「OLMo 是训练与使用 AI2 最先进开源语言模型的仓库，由科学家设计、为科学家服务」。

**（2）两阶段训练与模型矩阵。**

- [README.md:45-49](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/README.md#L45-L49)：说明预训练分两个阶段，Stage 1 用 `OLMo-mix-1124`，Stage 2 用 `Dolmino-mix-1124`。
- [README.md:51-61](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/README.md#L51-L61)：checkpoint 总表——1B/7B/13B/32B 四个尺寸分别给出 OLMo 格式（Stage 1 与 Stage 2）和 HuggingFace 格式的链接；并且说明「至少每 1000 步发布一个 checkpoint」。注意 [README.md:61](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/README.md#L61) 特别注明：**32B 是用新训练器（OLMo-core）训练的**，要训练/微调 32B 需去 OLMo-core 仓库。

**（3）复现训练的入口命令。**

- [README.md:63-81](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/README.md#L63-L81)：复现训练的标准命令 `torchrun --nproc_per_node=8 scripts/train.py {path_to_train_config}`，以及用 `--setting=value`、`--setting3.subsetting1=value` 在命令行覆盖配置项的机制。
- [README.md:87-95](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/README.md#L87-L95)：Mac（Apple Silicon）上不用 torchrun，直接 `python scripts/train.py configs/tiny/OLMo-20M.yaml --save_overwrite`，并要求 PyTorch 2.5.x。这个 tiny 配置正是下一讲 `u1-l3` 的主角。

**（4）推理与量化入口。**

- [README.md:159-182](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/README.md#L159-L182)：HuggingFace 推理示例——`AutoModelForCausalLM.from_pretrained("allenai/OLMo-2-0425-1B")` 后 `generate()`，以及 `pipeline("text-generation", ...)` 的替代写法。
- [README.md:184-190](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/README.md#L184-L190)：量化——`load_in_8bit=True`（依赖 `bitsandbytes`），并提示量化模型对输入类型敏感，建议显式 `.to('cuda')`。

**（5）评估与部署。**

- [README.md:192-194](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/README.md#L192-L194)：更多评估工具在 OLMo Eval 与 olmes 两个独立仓库。
- [README.md:196-208](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/README.md#L196-L208)：用 `scripts/olmo2_modal_openai.py` 在 Modal.com 上托管一个 OpenAI 兼容 API 的示例。

#### 4.1.4 代码实践

**实践：从 README 提取「模型 × 配置 × checkpoint」对照关系（源码阅读型）**

1. **实践目标**：不看任何二手资料，仅凭 README 建立对项目产出物的全局认知，并找到后续讲义要用的入口命令。
2. **操作步骤**：
   1. 打开 [README.md](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/README.md)，定位 L51-L61 的 checkpoint 总表。
   2. 为 **OLMo-2 1B** 这一行找到三个链接：Stage 1 的 CSV（checkpoint 清单）、Stage 2 的 CSV、HuggingFace 模型页，分别点开看一眼内容形态（CSV 里是逐 checkpoint 的链接列表；HF 页是模型卡片）。
   3. 记下 7B 与 13B 各自 Stage 1 使用的训练配置文件路径（形如 `configs/official-XXXX/OLMo2-XB-stage1.yaml`）。
   4. 对比 L68（`torchrun` 多卡命令）与 L89（Mac 上 `python` 命令）两种启动方式。
3. **需要观察的现象**：CSV 清单中 checkpoint 的命名规律——形如 `stepXXXXXXX-tokensXXXXB`，即「全局步数-已见 token 数」编码了训练进度。
4. **预期结果**：你能回答「1B 的 Stage 2 用了哪个配置文件」「32B 为什么不在本仓库训练」「Mac 上启动训练用什么命令」这三个问题。
5. 本实践为纯阅读型，不涉及运行，无「待本地验证」项。

#### 4.1.5 小练习与答案

**练习 1**：为什么说 OLMo 是「为科学家设计」的？给出 README 中的证据。

**参考答案**：README L27 明确写 "designed by scientists, for scientists"；其体现是 L51 的承诺——至少每 1000 个训练步发布一个 checkpoint（OLMo 与 HF 两种格式），加上数据集（`OLMo-mix-1124`/`Dolmino-mix-1124`）、训练配置（`configs/official-*`）与 WandB 日志全部公开，使训练全过程可复现、可研究。

**练习 2**：OLMo-2 的「两阶段训练」分别用什么数据、目标是什么？

**参考答案**：Stage 1 在约 4T~5T token 的大规模网页数据 `OLMo-mix-1124` 上训练（计算量最大）；Stage 2 在更小但高质量的定向数据 `Dolmino-mix-1124` 上继续训练（1B/7B 约 50B token，13B 最高 300B），7B/13B 还会用多个随机种子各训一次再做权重平均（soup）。

**练习 3**：想在 8 卡 GPU 机器上复现 7B 的 Stage 1 训练，命令是什么？

**参考答案**：`torchrun --nproc_per_node=8 scripts/train.py configs/official-1124/OLMo2-7B-stage1.yaml`（路径来自 README L105 的表格；如有需要可按 L77-81 的方式追加 `--key=value` 覆盖配置项）。

### 4.2 模块二：依赖与版本

#### 4.2.1 概念说明

`pyproject.toml` 是 Python 官方标准的打包描述文件。理解它，你才能回答三个实际问题：

- 装这个项目需要什么？（运行依赖）
- 为什么推荐 `pip install -e .[all]` 而不是只 `pip install ai2-olmo`？（可选依赖组）
- 装出来的版本号从哪来？（动态版本）

几个术语解释：

- **`-e`（editable）安装**：以「可编辑」方式从源码安装，你在仓库里改代码，安装环境立即生效——训练/微调场景推荐这种方式（README L33-39）。
- **extras（可选依赖组）**：`dev`（开发工具：ruff/mypy/black/pytest…）、`train`（训练才需要的：wandb/torchmetrics/safetensors/datasets…）、`all = dev + train`。装 PyPI 版本 `ai2-olmo` 只带运行依赖，训练相关包要自己补。
- **动态版本（dynamic version）**：版本号不在 `pyproject.toml` 里写死，而是构建时从 `olmo/version.py` 的 `VERSION` 变量读取，保证「代码里的版本」与「发布的包版本」永远一致。

#### 4.2.2 核心流程

安装与版本解析的流程：

```text
pip install -e .[all]  （或 pip install ai2-olmo）
   │
   ├─ setuptools 读取 pyproject.toml [project]
   ├─ 检查 requires-python（>=3.10）
   ├─ 解析 dependencies（torch>=2.1、numpy<2、ai2-olmo-core>=1.8.0 …）
   ├─ [all] → 追加 dev + train 两组 extras
   ├─ 动态版本：执行/解析 olmo/version.py，取 VERSION = "0.6.0"
   └─ 按 [tool.setuptools.packages.find] 打包 olmo* / hf_olmo* / olmo_data*
      （scripts、tests、docs、inference、evaluation 等目录不进包）
```

值得注意的依赖细节：

| 依赖 | 用途猜测（结合后续讲义验证） |
| --- | --- |
| `torch>=2.1` | 深度学习框架，模型与训练器全部建在其上 |
| `numpy<2` | token 数组（memmap 数据集）用 numpy 表示；钉在 <2 保证兼容性 |
| `ai2-olmo-core>=1.8.0` | 新训练器的基础库，本仓库部分基础原语由它提供 |
| `omegaconf` | 配置系统核心（单元 1 第 4 讲 `u1-l4` 详解） |
| `boto3` / `google-cloud-storage` | 直接读写 S3 / GCS 上的数据与 checkpoint |
| `tokenizers` / `transformers` | 分词与 HuggingFace 生态集成 |
| `cached_path>=1.6.2` | 统一的远端资源下载缓存 |
| `importlib_resources` | 从包内分发 tokenizer 等数据资产（`olmo_data` 包） |

#### 4.2.3 源码精读

**（1）包名、Python 版本与运行依赖。**

- [pyproject.toml:5-13](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/pyproject.toml#L5-L13)：项目发布名是 `ai2-olmo`（不是 `olmo`），`dynamic = ["version"]` 声明版本动态生成，`requires-python = ">=3.10"`。
- [pyproject.toml:15-28](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/pyproject.toml#L15-L28)：运行依赖清单，即上表 11 项。

**（2）可选依赖组。**

- [pyproject.toml:30-59](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/pyproject.toml#L30-L59)：`dev`（L31-42，代码检查与测试工具）、`train`（L43-53，wandb、safetensors、datasets 等训练期依赖）、`all`（L54-56，等于 `ai2-olmo[dev,train]`）、`figures`（L57-59，matplotlib）。这解释了 README 为什么推荐训练用户从源码装 `.[all]`：PyPI 裸装不含 `train` 组。

**（3）动态版本机制。**

- [pyproject.toml:72-73](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/pyproject.toml#L72-L73)：`version = { attr = "olmo.version.VERSION" }`——构建时读取 `olmo/version.py` 里的 `VERSION`。
- [olmo/version.py:1-11](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/olmo/version.py#L1-L11)：`_MAJOR="0"`、`_MINOR="6"`、`_PATCH="0"`，拼出 `VERSION = "0.6.0"`（当前 HEAD 下）。注释还说明了 `_SUFFIX` 用于 nightly 构建的 `.dev$DATE` 后缀。

**（4）哪些目录会被打包。**

- [pyproject.toml:75-91](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/pyproject.toml#L75-L91)：`include = ["olmo*", "hf_olmo*", "olmo_data*"]`，exclude 列表排除了 `tests`、`test_fixtures`、`docs`、`scripts`、`inference`、`evaluation` 等。也就是说：**pip 装到的 `ai2-olmo` 只包含三个包**——核心库 `olmo`、HF 集成 `hf_olmo`、数据资产 `olmo_data`；训练入口脚本 `scripts/train.py` 只存在于仓库中（所以复现训练要 clone 仓库）。这一细节将在下一讲 `u1-l2`（仓库目录结构）展开。

**（5）附带的测试配置。**

- [pyproject.toml:163-178](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/pyproject.toml#L163-L178)：pytest 配置——测试路径 `tests/`，注册了 `gpu` marker（GPU 专属测试可用 marker 过滤）。单元 8 会用到。

#### 4.2.4 代码实践

**实践：用包元数据 API 透视已安装的 ai2-olmo**

1. **实践目标**：不打开 `pyproject.toml`，直接从安装后的包元数据里读出版本号与依赖清单，验证「动态版本」确实生效。
2. **操作步骤**：
   1. 先完成 4.3.4 的安装（本实践依赖它；也可对任何已装 `ai2-olmo` 的环境执行）。
   2. 运行：
      ```bash
      python -c "import importlib.metadata as m; print(m.version('ai2-olmo'))"
      python -c "import importlib.metadata as m; print('\n'.join(m.requires('ai2-olmo') or []))"
      ```
   3. 对照 [pyproject.toml:15-28](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/pyproject.toml#L15-L28) 逐行核对依赖清单。
3. **需要观察的现象**：第一条命令打印的版本号应等于 [olmo/version.py](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/olmo/version.py) 中 `VERSION` 的值；`requires()` 输出中 extras 依赖会带 `extra == "dev"` / `extra == "train"` 标记。
4. **预期结果**：当前 HEAD 源码中 `VERSION = "0.6.0"`，因此从源码 `pip install -e .` 后应打印 `0.6.0`（若装的是 PyPI 发布版，版本号以该发布为准）。运行结果**待本地验证**。
5. 若 `m.requires('ai2-olmo')` 返回 `None`，说明该环境没有安装 `ai2-olmo` 的发行元数据，请先做 4.3.4。

#### 4.2.5 小练习与答案

**练习 1**：`pip install ai2-olmo` 与 `pip install -e .[all]` 装出的东西有何差别？

**参考答案**：前者从 PyPI 安装发行版，只含 11 个运行依赖声明的最小集合，且不含仓库里的 `scripts/` 等未打包目录；后者从本地源码可编辑安装，额外带上 `dev`（ruff/mypy/black/pytest 等）和 `train`（wandb/torchmetrics/safetensors/datasets 等）两组可选依赖，且改源码立即生效——所以训练/微调场景 README 推荐后者。

**练习 2**：为什么 `pyproject.toml` 的 `[project]` 里没有 `version = "..."`？版本号从哪来？

**参考答案**：因为声明了 `dynamic = ["version"]`（L7），实际版本在构建时按 `[tool.setuptools.dynamic]` 的 `version = { attr = "olmo.version.VERSION" }`（L72-73）从 `olmo/version.py` 的 `VERSION` 变量读取，当前为 `0.6.0`。好处是代码内打印的版本与发布的包版本永远一致。

**练习 3**：`numpy` 为什么被约束为 `numpy<2`？

**参考答案**：`pyproject.toml` L16 写明 `"numpy<2"`。numpy 2.x 与 numpy 1.x 存在 ABI/API 不兼容，OLMo 的 memmap 数据链路（`numpy` 数组直读为 token id）及其依赖生态在 numpy 2 下未经验证，故显式锁在 1.x。（具体兼容性结论属推断，以仓库后续更新为准。）

### 4.3 模块三：包入口

#### 4.3.1 概念说明

`olmo/__init__.py` 是 `import olmo` 时最先执行的文件，它决定了「哪些名字能从包顶层直接导入」。它只做两件事：

1. **star 导入三个核心子模块**：`config`（全部配置类）、`model`（模型结构）、`tokenizer`（分词器）。因此你可以写 `from olmo import OLMo`、`from olmo import TrainConfig`，而不用写更长的 `from olmo.model import OLMo`。
2. **提供 `check_install()` 自检函数**：打印已安装的 OLMo 版本号，可选断言 CUDA 可用。这是官方给出的「验证安装是否成功」的手段（也是本讲的代码实践任务）。

注意 `train.py`（Trainer）、`data/`、`checkpoint.py`、`beam_search.py` 等**不在**顶层 star 导入之列，需要按子模块路径导入，例如 `from olmo.train import Trainer`。这个设计让顶层入口保持轻量。

#### 4.3.2 核心流程

`import olmo` 时发生的事：

```text
import olmo
  ├─ 执行 olmo/__init__.py
  │    ├─ from .config import *     → 顶层可见 TrainConfig、ModelConfig、
  │    │                              TokenizerConfig、OptimizerConfig、…（见 config.py __all__）
  │    ├─ from .model import *      → 顶层可见 OLMo、OLMoBlock、RotaryEmbedding、
  │    │                              RMSLayerNorm、…（见 model.py __all__）
  │    └─ from .tokenizer import *  → 顶层可见 Tokenizer
  └─ 之后才可调用 olmo.check_install()
       ├─ cuda=True 时：断言 torch.cuda.is_available()，通过则先打印 "CUDA available"
       └─ 打印 f"OLMo v{VERSION} installed"（VERSION 来自 .version）
```

star 导入遵循各子模块的 `__all__` 白名单，而不是「导出一切」：

- [olmo/config.py:31-61](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/olmo/config.py#L31-L61) 列出 30 个配置符号（含 `ModelConfig`、`TrainConfig`、`DataConfig`、`WandbConfig`、`FSDPConfig` 等）；
- [olmo/model.py:59-68](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/olmo/model.py#L59-L68) 起列出模型符号（`OLMoBlock`、`RotaryEmbedding`、`RMSLayerNorm`、`SwiGLU` 等，`OLMo` 类定义在 [olmo/model.py:1070](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/olmo/model.py#L1070)）；
- [olmo/tokenizer.py:15](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/olmo/tokenizer.py#L15) 只导出 `Tokenizer`。

#### 4.3.3 源码精读

`olmo/__init__.py` 全文只有 16 行，逐段看：

- [olmo/\_\_init\_\_.py:1-3](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/olmo/__init__.py#L1-L3)：三条 star 导入，把 `config`、`model`、`tokenizer` 的公开符号提升到 `olmo` 顶层。
- [olmo/\_\_init\_\_.py:6-15](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/olmo/__init__.py#L6-L15)：`check_install(cuda: bool = False)` 的实现——函数体内才 `import torch` 和 `from .version import VERSION`（延迟导入，避免包加载时就强制初始化 torch）；`cuda=True` 时先 `assert torch.cuda.is_available()`，再打印 `"CUDA available"`；最后无条件打印 `f"OLMo v{VERSION} installed"`。
- [olmo/version.py:10-11](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/olmo/version.py#L10-L11)：`VERSION_SHORT = "0.6"` 与 `VERSION = "0.6.0"` 的拼接逻辑。

由此可以**从源码推断** `check_install()` 的输出：当前 HEAD 下不带参数调用会打印一行 `OLMo v0.6.0 installed`；在 GPU 机器上以 `check_install(cuda=True)` 调用会先打印 `CUDA available` 再打印版本行。

#### 4.3.4 代码实践

**实践：安装 OLMo 并运行官方自检（本讲核心实践）**

1. **实践目标**：把项目装进一个可用的 Python 环境，并用官方提供的 `check_install()` 验证安装成功、记录版本号。
2. **操作步骤**：
   1. （推荐）创建独立环境并先装 PyTorch（README L31 要求按操作系统选择对应安装方式）：
      ```bash
      python -m venv .venv && source .venv/bin/activate
      # 按你的平台从 https://pytorch.org 选择命令安装 torch，然后再执行下面任一安装
      ```
   2. **方式 A（源码可编辑安装，训练/微调推荐，来自 [README.md:35-39](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/README.md#L35-L39)）**：
      ```bash
      git clone https://github.com/allenai/OLMo.git
      cd OLMo
      pip install -e .[all]
      ```
      （在 zsh 等 shell 中建议写成 `pip install -e '.[all]'`，避免方括号被 shell 解释。）
   3. **方式 B（PyPI 安装，仅使用模型/轻量场景，来自 [README.md:40-43](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/README.md#L40-L43)）**：
      ```bash
      pip install ai2-olmo
      ```
   4. 运行自检并记录输出：
      ```bash
      python -c "from olmo import check_install; check_install()"
      ```
   5. （可选，GPU 机器）`python -c "from olmo import check_install; check_install(cuda=True)"`。
3. **需要观察的现象**：终端打印一行 `OLMo v<版本号> installed`。使用方式 A 且装的是当前 HEAD 源码时，版本号应为 `0.6.0`（直接来自 [olmo/version.py:5-11](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/olmo/version.py#L5-L11)）；使用方式 B 时为 PyPI 最新发布版号。若报 `ModuleNotFoundError: No module named 'olmo'`，说明装到了别的环境。
4. **预期结果**：把打印出的版本号记到你的学习笔记里——后续讲义所有永久链接均基于当前 HEAD（`090253da`），版本号可作为环境快照的一部分。本实践的安装耗时与可能的依赖冲突因机器而异，**运行结果待本地验证**。
5. 常见问题：`pip install -e .[all]` 会连 `dev`+`train` 全部装上，若网络受限可退回 `pip install -e .`（只装运行依赖，够跑前几讲的阅读型实践，跑训练则需补 `train` 组）。

#### 4.3.5 小练习与答案

**练习 1**：`from olmo import Trainer` 会成功吗？为什么？正确的导入方式是什么？

**参考答案**：不会。`olmo/__init__.py`（L1-3）只 star 导入了 `config`、`model`、`tokenizer` 三个模块，而 `Trainer` 定义在 `olmo/train.py` 中（见 [olmo/train.py:62](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/olmo/train.py#L62) 的 `__all__ = ["SpeedMonitor", "LRMonitor", "Trainer"]`），未被提升到顶层。应写 `from olmo.train import Trainer`。

**练习 2**：`check_install(cuda=True)` 在没有 GPU 的机器上会发生什么？依据是哪几行代码？

**参考答案**：会抛出 `AssertionError: CUDA is not available!`。依据 [olmo/\_\_init\_\_.py:11-13](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/olmo/__init__.py#L11-L13)：`cuda=True` 分支先执行 `assert torch.cuda.is_available(), "CUDA is not available!"`，断言失败即中断，不会打印后续版本行。

**练习 3**：为什么 `check_install` 里 `import torch` 写在函数体内而不是文件顶部？

**参考答案**：这是延迟导入（lazy import）。`olmo/__init__.py` 在 `import olmo` 时就会执行，如果把 `import torch` 放顶部，那么任何「只想用配置类等轻量符号」的场景也会被迫先加载较重的 torch；放进函数体后，只有真正调用 `check_install()` 才加载 torch，降低了包入口的导入开销（见 [olmo/\_\_init\_\_.py:6-9](https://github.com/allenai/OLMo/blob/090253dac6688f2532509daa7aa2eb5fae50e956/olmo/__init__.py#L6-L9)）。

## 5. 综合实践

**综合任务：给 OLMo 做一次「安装体检」并写一份环境档案**

把本讲三个模块串起来，产出一份可长期保存的环境笔记 `OLMo-notes/env.md`（自建笔记文件，不属于本讲义）：

1. **安装**：按 4.3.4 的方式 A 或 B 安装本项目。
2. **版本确认**：运行 `python -c "from olmo import check_install; check_install()"`，记录打印的版本号，并用 4.2.4 的 `importlib.metadata.version('ai2-olmo')` 交叉核对两者是否一致（都应指向同一个安装）。
3. **依赖体检**：运行 `python -c "import importlib.metadata as m; print(len(m.requires('ai2-olmo') or []), 'requirements')"` 数出依赖条数，并挑出其中 `torch`、`omegaconf`、`transformers` 三项的版本：`python -c "import torch, transformers; print(torch.__version__, transformers.__version__)"`（omegaconf 用 `python -c "import omegaconf; print(omegaconf.__version__)"`）。
4. **能力定位**：在笔记中用自己的话写 5 行，分别对应 README 里的五大能力（预训练/微调/HF 推理/量化/评估）各自的入口。
5. **预期结果**：笔记中至少包含——版本号、Python 与 torch 版本、依赖条数、五大能力入口。所有运行结果**待本地验证**；本任务不涉及修改任何源码。

## 6. 本讲小结

- OLMo 是 AI2 的开源语言模型训练与使用一体化框架，核心卖点是数据、配置、代码、逐 1000 步 checkpoint 全公开的可复现理念（"designed by scientists, for scientists"）。
- 仓库现状：README 公告其已不活跃，最新的 OLMo 发布与 32B 训练迁移到了 OLMo-core；但 1B/7B/13B 完整训练链路仍在本仓库，且代码结构适合学习。
- 五大能力与入口：预训练/微调 `scripts/train.py` + `configs/`；HF 推理与量化 `hf_olmo/` + `transformers`；评估 `olmo/eval/` 与外部 olmes/OLMo Eval。
- 打包名是 `ai2-olmo`，Python ≥ 3.10，torch ≥ 2.1，numpy < 2；`pip install -e .[all]` 才包含 dev+train 可选依赖；版本号由 `olmo/version.py` 动态提供（当前 HEAD 为 0.6.0）。
- pip 包只含 `olmo`、`hf_olmo`、`olmo_data` 三个包；`scripts/`、`tests/`、`inference/` 等不发布——所以复现训练必须 clone 仓库。
- `olmo/__init__.py` 通过 star 导入把 `config`/`model`/`tokenizer` 的符号提升到顶层，并提供 `check_install()` 安装自检。

## 7. 下一步学习建议

下一讲 `u1-l2`（仓库目录结构与代码地图）会把本讲末尾提到的目录职责逐一展开：`olmo/`、`olmo_data/`、`scripts/`、`configs/`、`hf_olmo/`、`inference/`、`evaluation/`、`tests/` 各自放什么，以及 `pyproject.toml` 打包范围与目录结构的对应关系。

在进入下一讲前，建议你先自行浏览：

- `configs/tiny/OLMo-20M.yaml`（下一讲的运行主角，也是 `u1-l3` 跑通训练的配置）；
- `olmo/` 目录下的文件名（`model.py`、`train.py`、`data/`、`optim.py`、`checkpoint.py`…），尝试仅凭文件名猜测职责，下一讲验证你的猜测。
